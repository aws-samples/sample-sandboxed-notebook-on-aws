"""
CloudWatch Log Streaming — SSE endpoint that tails MicroVM logs in real-time.

Part of: proxy.notebook (Notebook application layer)

Endpoint:
  GET /logs/stream  - Stream CloudWatch logs for the MicroVM linked to the session (SSE)

How it works:
  1. Resolves session_id → microvm_id → log group + log stream
  2. Polls CloudWatch FilterLogEvents every 1.5s (like `aws logs tail --follow`)
  3. Pushes new log events as SSE to the frontend
"""

import asyncio
import json
import time
import logging
import threading
from datetime import datetime, timezone

from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from proxy.platform import aws_clients
from proxy.platform.defaults import DEFAULT_MEMORY_MIB
from proxy.platform.microvm_manager import AWS_REGION, IMAGE_BASE_NAME, DEFAULT_IMAGE_ARCH

logger = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])

# CloudWatch log group prefix for MicroVM runtime logs. The image base name comes
# from config (IMAGE_BASE_NAME) — not hardcoded — so a rename stays in one place.
LOG_GROUP_PREFIX = f"/aws/lambda-microvms/{IMAGE_BASE_NAME}"

# Poll interval for CloudWatch (seconds)
POLL_INTERVAL = 1.5

# Maximum time to keep the SSE connection open (seconds)
MAX_STREAM_DURATION = 3600  # 1 hour


def _get_log_group_for_vm(vm_id: str, memory_mib: int, architecture: str = None) -> str:
    """CloudWatch log group for a VM. Images are named
    {IMAGE_BASE_NAME}-{arch}-{mem}, so the log group is arch-aware."""
    arch = architecture or DEFAULT_IMAGE_ARCH
    return f"{LOG_GROUP_PREFIX}-{arch}-{memory_mib}"


# Prefix the app/proxy error emitters write (see app/platform/errors.py). We parse
# these out of the VM's CloudWatch stream so the panel can surface per-notebook
# error metrics instead of showing them as opaque INFO lines.
_METRIC_MARKER = "APP_METRIC"


def _parse_metric_line(msg: str) -> dict | None:
    """If a log message carries an APP_METRIC structured line, return its parsed
    payload (dict); otherwise None. The emitter logs '... APP_METRIC {json}', so we
    find the marker and json.loads the remainder."""
    idx = msg.find(_METRIC_MARKER)
    if idx == -1:
        return None
    tail = msg[idx + len(_METRIC_MARKER):].strip()
    try:
        data = json.loads(tail)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _classify_level(msg: str) -> str:
    """Classify a log line's level from its content (shared by stream + history)."""
    if "ERROR" in msg or "✗" in msg or "❌" in msg:
        return "ERROR"
    if "WARNING" in msg or "⚠" in msg:
        return "WARN"
    if "DEBUG" in msg:
        return "DEBUG"
    return "INFO"


def _logs_client():
    """Shared, thread-safe cached CloudWatch Logs client (one per region)."""
    return aws_clients.get_client("logs", AWS_REGION)


async def _resolve_session_vm_targets(vm_manager, session_id: str) -> list[dict]:
    """Resolve EVERY VM that has served this session (original + all rotation /
    reactive-recovery replacements) into log-stream targets, so the panel shows a
    notebook's full history across VM swaps — not just the current VM.

    Returns a list of {"vm_id", "log_group"} (deduped), oldest VM first. Falls back
    to the single current session VM if the DB has no rows yet (e.g. brand-new VM
    whose row hasn't landed). A session's VMs can span different size tiers, so each
    resolves its OWN log group.

    Async: every DB read runs off the event loop (astorage / get_session_vm_async).
    """
    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        from proxy.storage import astorage
        rows = await astorage.vm_session_list_for_session(session_id) or []
    except Exception as e:
        logger.warning(f"vm_session_list_for_session failed for {session_id[:8]}: {e}")
        rows = []

    for row in rows:
        vm_id = row.get("microvm_id")
        if not vm_id:
            continue
        mem = row.get("memory_mib") or DEFAULT_MEMORY_MIB
        arch = row.get("architecture") or DEFAULT_IMAGE_ARCH
        group = _get_log_group_for_vm(vm_id, mem, arch)
        key = (group, vm_id)
        if key not in seen:
            seen.add(key)
            targets.append({"vm_id": vm_id, "log_group": group})

    # Fallback / ensure the CURRENT VM is included even if the DB row lags.
    current = await vm_manager.get_session_vm_async(session_id)
    if current and current.get("vm_id"):
        vm_id = current["vm_id"]
        mem, arch = await _resolve_vm_tier(vm_manager, vm_id)
        group = _get_log_group_for_vm(vm_id, mem, arch)
        key = (group, vm_id)
        if key not in seen:
            seen.add(key)
            targets.append({"vm_id": vm_id, "log_group": group})

    return targets


async def _resolve_vm_tier(vm_manager, vm_id: str) -> tuple[int, str]:
    """Resolve a VM's (memory_mib, architecture) — which together select the
    CloudWatch log group. Prefer the in-memory active_microvms entry, then the
    authoritative DB row (vm_session_get), and only then fall back to defaults.

    The DB fallback matters: a VM restored at proxy startup, or one whose
    in-memory tracking was lost, would otherwise resolve to the default tier and
    point at the WRONG log group (e.g. -2048 when the VM really runs on -1024),
    so its logs never appear even though they exist under the correct group."""
    info = vm_manager.active_microvms.get(vm_id, {}) or {}
    memory_mib = info.get("memory_mib")
    architecture = info.get("architecture")
    if not memory_mib or not architecture:
        try:
            from proxy.storage import astorage
            row = await astorage.vm_session_get(vm_id) or {}
        except Exception:
            row = {}
        memory_mib = memory_mib or row.get("memory_mib")
        architecture = architecture or row.get("architecture")
    return (memory_mib or DEFAULT_MEMORY_MIB), (architecture or DEFAULT_IMAGE_ARCH)


# Stream lookup bounds. A log group is shared by every VM of one image tier
# (pool VMs included), so the session's stream can sit far behind the newest
# ones: page newest-first up to _STREAM_LOOKUP_MAX_PAGES * _STREAM_LOOKUP_PAGE_SIZE
# streams before giving up.
_STREAM_LOOKUP_PAGE_SIZE = 50  # describe_log_streams maximum
_STREAM_LOOKUP_MAX_PAGES = 40

# Resolved stream names per (log group, vm_id). A VM keeps its stream for its
# whole life, so a hit only expires to pick up a deleted/recreated group.
_STREAM_CACHE_TTL_SECONDS = 600.0
_stream_cache: dict[tuple[str, str], tuple[str, float]] = {}
_stream_cache_lock = threading.Lock()


def _forget_log_stream(log_group: str, vm_id: str) -> None:
    """Drop a cached stream name (the group or stream no longer exists)."""
    with _stream_cache_lock:
        _stream_cache.pop((log_group, vm_id), None)


def _find_log_stream(logs_client, log_group: str, vm_id: str) -> str | None:
    """
    Find the log stream for a specific MicroVM ID.
    Stream name format: {date}[{version}]microvm-{vm_id}
    The date/version prefix varies, so no name prefix can be used: page the
    group's streams newest-first and match the vm_id substring, bounded to
    _STREAM_LOOKUP_MAX_PAGES pages. A found name is cached per (group, vm_id).
    Returns None when the VM has no stream yet (or it is beyond the bound).
    """
    key = (log_group, vm_id)
    now = time.monotonic()
    with _stream_cache_lock:
        hit = _stream_cache.get(key)
        if hit and now - hit[1] < _STREAM_CACHE_TTL_SECONDS:
            return hit[0]
    try:
        params = {
            "logGroupName": log_group,
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": _STREAM_LOOKUP_PAGE_SIZE,
        }
        for _ in range(_STREAM_LOOKUP_MAX_PAGES):
            resp = logs_client.describe_log_streams(**params)
            for stream in resp.get("logStreams", []):
                name = stream["logStreamName"]
                if vm_id in name:
                    with _stream_cache_lock:
                        _stream_cache[key] = (name, time.monotonic())
                    return name
            next_token = resp.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception as e:
        logger.warning(f"Failed to find log stream for {vm_id}: {e}")
    return None


def _poll_once(logs_client, targets: list[dict], resolved: dict, start_time: int) -> tuple[list, bool]:
    """One SSE tick's CloudWatch work (SYNC — run via asyncio.to_thread).

    Collects new events across ALL target streams into one batch. Mutates the
    per-stream `resolved` cache (only ever from this single call per tick, so
    there is no concurrent mutation). Returns (batch, saw_any_stream) where
    batch = [(ts, msg, vm_id), ...] in CloudWatch order (caller sorts).
    """
    batch: list[tuple[int, str, str]] = []  # (ts, msg, vm_id)
    saw_any_stream = False
    for t in targets:
        group, vm_id = t["log_group"], t["vm_id"]
        key = (group, vm_id)
        stream_name = resolved.get(key)
        if not stream_name:
            stream_name = _find_log_stream(logs_client, group, vm_id)
            if stream_name:
                resolved[key] = stream_name
            else:
                continue  # this VM hasn't produced logs yet
        saw_any_stream = True
        try:
            resp = logs_client.filter_log_events(
                logGroupName=group,
                logStreamNames=[stream_name],
                startTime=start_time,
                interleaved=True,
            )
        except logs_client.exceptions.ResourceNotFoundException:
            resolved.pop(key, None)  # group/stream vanished — re-resolve next pass
            _forget_log_stream(group, vm_id)
            continue
        for event in resp.get("events", []):
            batch.append((event["timestamp"], event["message"].rstrip("\n"), vm_id))
    return batch, saw_any_stream


def _collect_history(logs_client, targets: list[dict], limit: int,
                     start_time: int | None, end_time: int | None) -> tuple:
    """Page every target stream for /logs/history (SYNC — run via asyncio.to_thread).

    Each stream is paged oldest-first into a ring capped at `limit` (keeps the
    newest). Returns (merged, overflowed, more_pages, found_any_stream,
    groups_used, streams_used)."""
    MAX_PAGES = 20  # per-stream safety bound (20 * up to 500 ≈ 10k scanned each)
    merged: list[dict] = []
    overflowed = False
    more_pages = False
    found_any_stream = False
    groups_used: list[str] = []
    streams_used: list[str] = []

    for t in targets:
        group, vm_id = t["log_group"], t["vm_id"]
        stream_name = _find_log_stream(logs_client, group, vm_id)
        if not stream_name:
            continue
        found_any_stream = True
        groups_used.append(group)
        streams_used.append(stream_name)

        ring = deque(maxlen=limit)
        params = {
            "logGroupName": group,
            "logStreamNames": [stream_name],
            "interleaved": True,
            "limit": min(limit, 500),
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        pages = 0
        next_token = None
        while True:
            if next_token:
                params["nextToken"] = next_token
            try:
                resp = logs_client.filter_log_events(**params)
            except logs_client.exceptions.ResourceNotFoundException:
                break
            for ev in resp.get("events", []):
                if len(ring) == ring.maxlen:
                    overflowed = True
                # tag the source VM so the UI can label lines across a swap
                ev["_vm_id"] = vm_id
                ring.append(ev)
            next_token = resp.get("nextToken")
            pages += 1
            if not next_token or pages >= MAX_PAGES:
                break
        if next_token or pages >= MAX_PAGES:
            more_pages = True
        merged.extend(ring)

    return merged, overflowed, more_pages, found_any_stream, groups_used, streams_used


def _collect_metrics_summary(logs_client, targets: list[dict], start_time: int) -> tuple:
    """Scan every target stream for APP_METRIC lines (SYNC — run via
    asyncio.to_thread). Returns (total, by_error_type, by_handler, recent) with
    `recent` unsorted/uncapped (caller sorts + caps)."""
    by_error_type: dict[str, int] = {}
    by_handler: dict[str, int] = {}
    recent: list[dict] = []
    total = 0
    MAX_PAGES = 20
    for t in targets:
        group, vm_id = t["log_group"], t["vm_id"]
        stream_name = _find_log_stream(logs_client, group, vm_id)
        if not stream_name:
            continue
        params = {
            "logGroupName": group,
            "logStreamNames": [stream_name],
            "interleaved": True,
            "startTime": start_time,
            # Server-side filter to the marker so we scan far less: CloudWatch
            # filterPattern matches the substring in the raw message.
            "filterPattern": f'"{_METRIC_MARKER}"',
        }
        pages = 0
        next_token = None
        while True:
            if next_token:
                params["nextToken"] = next_token
            try:
                resp = logs_client.filter_log_events(**params)
            except logs_client.exceptions.ResourceNotFoundException:
                break
            for ev in resp.get("events", []):
                metric = _parse_metric_line(ev.get("message", ""))
                if not metric:
                    continue
                total += 1
                et = str(metric.get("error_type") or "Unknown")
                hd = str(metric.get("handler") or "unknown")
                by_error_type[et] = by_error_type.get(et, 0) + 1
                by_handler[hd] = by_handler.get(hd, 0) + 1
                ts = ev["timestamp"]
                recent.append({
                    "timestamp": ts,
                    "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                    "error_type": et,
                    "handler": hd,
                    "namespace": metric.get("namespace"),
                    "status": metric.get("status"),
                    "vm_id": vm_id,
                })
            next_token = resp.get("nextToken")
            pages += 1
            if not next_token or pages >= MAX_PAGES:
                break
    return total, by_error_type, by_handler, recent


@router.get("/logs/stream")
async def stream_logs(request: Request):
    """
    Stream CloudWatch logs for the MicroVM linked to the current session.
    
    Uses Server-Sent Events (SSE) — same pattern as `aws logs tail --follow`.
    Polls FilterLogEvents every 1.5s and pushes new events to the client.
    
    Headers:
        X-Session-Id: session UUID (required)
    
    Query params:
        since: Unix timestamp (ms) to start from. Default: the last 60 s.
    
    SSE events:
        data: {"type": "log", "timestamp": 1234567890, "message": "...", "level": "INFO"}
        data: {"type": "meta", "log_group": "...", "log_stream": "...", "vm_id": "..."}
        data: {"type": "error", "message": "..."}
    """
    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "X-Session-Id header required"})

    vm_manager = request.app.state.vm_manager

    # Resolve EVERY VM this session has used (original + rotation/recovery swaps),
    # so a VM change doesn't drop the notebook's earlier logs from the tail.
    targets = await _resolve_session_vm_targets(vm_manager, session_id)
    if not targets:
        return JSONResponse(status_code=404, content={"error": "No VM found for this session"})

    # Determine start time (default: 60s ago or from query param)
    since_param = request.query_params.get("since")
    if since_param:
        start_time = int(since_param)
    else:
        start_time = int((time.time() - 60) * 1000)

    logger.info(f"📋 Log stream started: session={session_id[:8]}... vms={[t['vm_id'] for t in targets]}")

    async def event_stream():
        logs_client = _logs_client()
        current_start_time = start_time
        stream_start = time.time()
        # Per-target resolved stream name cache: (group, vm_id) -> stream_name.
        resolved: dict[tuple[str, str], str] = {}
        last_target_refresh = 0.0
        current_targets = targets

        yield f"data: {json.dumps({'type': 'meta', 'session_id': session_id, 'vms': [t['vm_id'] for t in current_targets]})}\n\n"

        while True:
            if await request.is_disconnected():
                logger.info(f"📋 Log stream closed (client disconnected): session={session_id[:8]}...")
                break
            if time.time() - stream_start > MAX_STREAM_DURATION:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Stream duration limit reached (1h)'})}\n\n"
                break

            # Periodically re-resolve targets so a NEW VM created mid-stream (a
            # rotation/recovery swap while the panel is open) starts being tailed
            # without the user reopening the panel.
            if time.time() - last_target_refresh > 10:
                current_targets = await _resolve_session_vm_targets(vm_manager, session_id) or current_targets
                last_target_refresh = time.time()

            # Collect new events across ALL target streams into one batch, then emit
            # in timestamp order so a merged multi-VM tail reads chronologically.
            # The CloudWatch calls run in ONE worker thread per tick (off the loop).
            try:
                batch, saw_any_stream = await asyncio.to_thread(
                    _poll_once, logs_client, current_targets, resolved, current_start_time)

                if not saw_any_stream:
                    yield f"data: {json.dumps({'type': 'waiting', 'message': 'Waiting for log stream...'})}\n\n"
                    await asyncio.sleep(POLL_INTERVAL * 2)
                    continue

                batch.sort(key=lambda e: e[0])
                for ts, msg, vm_id in batch:
                    metric = _parse_metric_line(msg)
                    if metric is not None:
                        # Surface app/VM error metrics as a distinct event the panel
                        # can render specially (badge/counter) instead of a raw line.
                        yield f"data: {json.dumps({'type': 'metric', 'timestamp': ts, 'time': datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3], 'vm_id': vm_id, 'metric': metric.get('metric'), 'error_type': metric.get('error_type'), 'handler': metric.get('handler'), 'namespace': metric.get('namespace'), 'status': metric.get('status')})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'log', 'timestamp': ts, 'time': datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3], 'message': msg, 'level': _classify_level(msg), 'vm_id': vm_id})}\n\n"
                    current_start_time = max(current_start_time, ts + 1)

            except Exception as e:
                logger.warning(f"Log stream poll error: {e}")
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
        },
    )


@router.get("/logs/history")
async def get_log_history(request: Request):
    """
    Fetch log history (non-streaming) for the current session's MicroVM, with
    support for paging BACKWARD in time ("Load earlier logs").

    Headers:
        X-Session-Id: session UUID (required)

    Query params:
        limit: max events to return (default: 200, max: 500)
        since: Unix timestamp (ms) — lower bound (startTime). Optional.
        until: Unix timestamp (ms) — upper bound (endTime), EXCLUSIVE-ish. When
               provided, returns the MOST RECENT `limit` events strictly before
               this time. This is what the "Load earlier" button passes (the
               oldest timestamp it currently shows, minus 1ms) to fetch the
               previous page going back in time.

    Behaviour:
        - No `until`  → initial load: the most recent `limit` events (CloudWatch
          returns newest-at-tail; we take the tail).
        - With `until`→ backward page: the most recent `limit` events before
          `until`.

    Returns:
        {
          events: [ {timestamp, time, message, level}, ... ]  (oldest → newest),
          oldest: <ms|null>,      # timestamp of the earliest event returned
          newest: <ms|null>,      # timestamp of the latest event returned
          has_more: <bool>,       # whether older events likely exist before `oldest`
          log_group, log_stream, vm_id, count
        }
    """
    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "X-Session-Id header required"})

    vm_manager = request.app.state.vm_manager

    # All VMs this session used (original + rotation/recovery swaps), so history
    # spans the notebook's full lifetime across VM changes, not just the current VM.
    session_targets = await _resolve_session_vm_targets(vm_manager, session_id)
    if not session_targets:
        return JSONResponse(status_code=404, content={"error": "No VM found for this session"})

    limit = min(int(request.query_params.get("limit", "200")), 500)
    since_param = request.query_params.get("since")
    until_param = request.query_params.get("until")

    # Window bounds. `until` (endTime) is the key to backward paging; `since` is an
    # optional floor. CloudWatch treats startTime as inclusive and endTime as
    # exclusive of events at exactly endTime, so passing oldest-1 avoids overlap.
    end_time = int(until_param) if until_param else None
    start_time = int(since_param) if since_param else None
    # Initial load (no explicit window at all): bound the scan to a recent floor so
    # we don't page through the VM's entire lifetime just to show the latest lines.
    # "Load earlier" then walks further back via `until`. has_more will still be set
    # so the button appears when there's more behind this initial window.
    INITIAL_LOOKBACK_MS = 6 * 60 * 60 * 1000  # 6 hours
    auto_floor = start_time is None and end_time is None
    if auto_floor:
        start_time = int(time.time() * 1000) - INITIAL_LOOKBACK_MS

    try:
        logs_client = _logs_client()

        # Merge across every VM stream this session used. Each stream is paged the
        # same way (oldest-first, ring capped at `limit` → keep newest), then all
        # streams' rings are merged and re-capped so the result is the newest
        # `limit` events across the whole session, in timestamp order.
        # All CloudWatch paging runs in one worker thread (off the event loop).
        (merged, overflowed, more_pages, found_any_stream,
         groups_used, streams_used) = await asyncio.to_thread(
            _collect_history, logs_client, session_targets, limit, start_time, end_time)

        if not found_any_stream:
            return {"events": [], "oldest": None, "newest": None, "has_more": False,
                    "vms": [t["vm_id"] for t in session_targets],
                    "message": "No log stream found yet"}

        # Sort the merged set by timestamp and keep the newest `limit`.
        merged.sort(key=lambda e: e["timestamp"])
        if len(merged) > limit:
            overflowed = True
            merged = merged[-limit:]

        events = []
        for event in merged:
            ts = event["timestamp"]
            msg = event["message"].rstrip("\n")
            metric = _parse_metric_line(msg)
            entry = {
                "timestamp": ts,
                "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                "message": msg,
                "level": _classify_level(msg),
                "vm_id": event.get("_vm_id"),
            }
            if metric is not None:
                entry["kind"] = "metric"
                entry["metric"] = metric.get("metric")
                entry["error_type"] = metric.get("error_type")
                entry["handler"] = metric.get("handler")
                entry["namespace"] = metric.get("namespace")
                entry["status"] = metric.get("status")
            events.append(entry)

        oldest = events[0]["timestamp"] if events else None
        newest = events[-1]["timestamp"] if events else None

        has_more = bool(overflowed or more_pages)
        if not events and end_time is not None:
            has_more = True
        if auto_floor and len(events) >= 1:
            has_more = True

        return {
            "events": events,
            "oldest": oldest,
            "newest": newest,
            "has_more": has_more,
            "log_groups": sorted(set(groups_used)),
            "log_streams": streams_used,
            "vms": [t["vm_id"] for t in session_targets],
            "count": len(events),
        }

    except Exception as e:
        logger.warning(f"Log history fetch error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/logs/metrics-summary")
async def get_metrics_summary(request: Request):
    """
    Per-notebook error-metric summary for the logs panel.

    Scans this session's VM CloudWatch stream(s) — across every VM the session has
    used (original + rotation/recovery swaps) — for APP_METRIC lines emitted by the
    VM app's central error handler, and aggregates them into counts. This surfaces
    "what's gone wrong in THIS notebook" at a glance, without the user reading raw
    logs.

    Scope note: only VM-side metrics appear here (that's all that lands in these
    /aws/lambda-microvms log groups). Proxy-side metrics are intentionally NOT
    included — the notebook view should show notebook/VM errors only.

    Headers:
        X-Session-Id: session UUID (required)

    Query params:
        minutes: lookback window (default 60, max 1440).

    Returns:
        {
          "total": int,
          "by_error_type": { "<error_type>": count, ... },
          "by_handler":    { "<handler>": count, ... },
          "recent": [ {timestamp, time, error_type, handler, namespace, status, vm_id}, ... up to 20 ],
          "window_minutes": int,
          "vms": [vm_id, ...]
        }
    """
    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "X-Session-Id header required"})

    vm_manager = request.app.state.vm_manager
    targets = await _resolve_session_vm_targets(vm_manager, session_id)
    if not targets:
        return {"total": 0, "by_error_type": {}, "by_handler": {}, "recent": [],
                "window_minutes": 0, "vms": []}

    minutes = min(int(request.query_params.get("minutes", "60")), 1440)
    start_time = int((time.time() - minutes * 60) * 1000)

    try:
        logs_client = _logs_client()
        # All CloudWatch paging runs in one worker thread (off the event loop).
        total, by_error_type, by_handler, recent = await asyncio.to_thread(
            _collect_metrics_summary, logs_client, targets, start_time)

        # Newest first, cap the recent list.
        recent.sort(key=lambda e: e["timestamp"], reverse=True)
        recent = recent[:20]

        return {
            "total": total,
            "by_error_type": by_error_type,
            "by_handler": by_handler,
            "recent": recent,
            "window_minutes": minutes,
            "vms": [t["vm_id"] for t in targets],
        }

    except Exception as e:
        logger.warning(f"Metrics summary fetch error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})
