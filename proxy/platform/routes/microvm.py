"""
MicroVM lifecycle routes — launch, terminate, suspend, resume, proxy, instances.

Part of: proxy.platform (Smart MicroVM Service layer)

Endpoints:
  POST      /launch                       - Launch a new MicroVM (pool grant first when enabled)
  POST      /terminate                    - Terminate (body microvmId; X-Session-Id optional)
  POST      /suspend                      - Suspend (X-Session-Id header)
  POST      /resume                       - Resume (X-Session-Id header)
  POST      /checkpoint-save              - Save a checkpoint of the session's VM now
  GET       /instances                    - List all active MicroVMs with state and cost
  GET       /instances/metrics            - Live metrics for one VM (microvm_id query param)
  GET       /instances/{id}/detail        - VM card detail (contract: proxy/platform/vm_detail.py)
  GET       /instances/{id}/metrics-history - Past CPU/memory/disk per run group, from the DB (never calls the VM)
  GET       /sessions/{id}/cost           - Cost of one session
  POST      /sessions/costs               - Per-session and total cost for a list of sessions
  POST      /proxy/execute-batch          - Run a batch of cells on the session's VM (streamed)
  POST      /proxy/set-env                - Inject env vars into the session's running VM
  ANY       /proxy/{path}                 - Proxy requests to a MicroVM with auth
"""

import os
import re
import time
import json
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from proxy.storage import storage, astorage
from proxy.platform.microvm_manager import (
    IMAGE_ARN, EXEC_ROLE_ARN, IMAGE_ARCHES, DEFAULT_IMAGE_ARCH,
    INGRESS_CONNECTOR, SHELL_INGRESS_CONNECTOR, EGRESS_CONNECTOR,
    _timed_log, build_user_env, adopt_pool_grant_times,
)
from proxy.platform.cost_tracker import price_quantities
from proxy.platform import http_clients
from proxy.platform.errors import json_error
from proxy.platform.restore_choice import (
    resolve_restore_choice, normalize_restore_report, latest_vm_session, restore_memory_assessment)
from proxy.platform.defaults import DEFAULT_MEMORY_MIB, arch_size_from_image
from pool_manager.config import PROXY_GRANT_RESTORE_BOUND_SECONDS
from proxy.platform import burst_time
from proxy.platform import resume_wait
from proxy.notebook import dag_service
from proxy.notebook import var_orphans

logger = logging.getLogger(__name__)

def _is_connectivity_error(exc: BaseException) -> bool:
    """True if `exc` (or a cause in its chain) is a transient network/DNS failure
    reaching AWS (wifi/VPN drop, DNS not resolving, endpoint unreachable). These
    are expected + self-healing, so we log them quietly + throttled instead of
    once per poll."""
    try:
        import botocore.exceptions as bce
        connectivity = tuple(c for c in (
            getattr(bce, "EndpointConnectionError", None),
            getattr(bce, "ConnectTimeoutError", None),
            getattr(bce, "ReadTimeoutError", None),
            getattr(bce, "ConnectionError", None),
        ) if isinstance(c, type))
    except Exception:
        connectivity = ()
    import socket
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if connectivity and isinstance(e, connectivity):
            return True
        if isinstance(e, socket.gaierror):
            return True
        e = e.__cause__ or e.__context__
    return False


# Throttle repeated "network unreachable" warnings during an outage: /instances
# is polled ~every 15s, so without this a single outage prints the same line
# dozens of times. Log at most once per this interval while connectivity is down.
_CONNECTIVITY_WARN_INTERVAL_SEC = 60
_last_connectivity_warn = 0.0


# Cold-launch wait-for-RUNNING schedule. Used ONLY by launch_microvm's wait loop
# after a cold run_vm (VM not taken from the pool). The pool grant, the explicit
# /resume route and every other poll keep their own intervals.
# A cold VM boots in ~1.4-1.9 s, so the first poll (right after run_vm) sees
# PENDING; polling again after 1.0 s, then every 0.5 s, notices RUNNING within
# ~0.5 s instead of on the next 5 s step. Budget and poll cap keep the call count
# bounded (<= 130 GetMicrovm per launch).
COLD_LAUNCH_WAIT_BUDGET_SEC = 60.0
COLD_LAUNCH_FIRST_DELAY_SEC = 1.0
COLD_LAUNCH_POLL_DELAY_SEC = 0.5
COLD_LAUNCH_MAX_DELAY_SEC = 2.0
COLD_LAUNCH_MAX_POLLS = 130
_COLD_LAUNCH_THROTTLE_CODES = frozenset({
    "ThrottlingException", "Throttling", "TooManyRequestsException",
    "RequestLimitExceeded", "ServiceUnavailableException",
})
# Indirection so tests can drive the deadline with a fake clock.
_cold_launch_clock = time.monotonic


def cold_launch_poll_delay(attempt: int, prev_delay: float = 0.0, throttled: bool = False) -> float:
    """Seconds to sleep after cold-launch poll number `attempt` (1-based).

    1.0 s after the first poll, 0.5 s after every later one. After a throttled or
    transient poll error the delay doubles from `prev_delay` (never below the
    normal delay), capped at COLD_LAUNCH_MAX_DELAY_SEC."""
    base = COLD_LAUNCH_FIRST_DELAY_SEC if attempt <= 1 else COLD_LAUNCH_POLL_DELAY_SEC
    if throttled:
        return min(COLD_LAUNCH_MAX_DELAY_SEC, max(base, prev_delay * 2))
    return base


def _is_cold_launch_poll_retryable(exc: BaseException) -> bool:
    """True for a throttling or connectivity error from a cold-launch poll: the
    loop backs off and polls again instead of failing the launch."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        if (resp.get("Error") or {}).get("Code") in _COLD_LAUNCH_THROTTLE_CODES:
            return True
    if "Throttl" in type(exc).__name__:
        return True
    return _is_connectivity_error(exc)


# Valid session-persistence modes. "eternal" and "checkpoint" persist state to
# S3; "none" is ephemeral (no save, no restore). Anything unrecognized is
# treated as "none" so a typo/legacy value fails safe to ephemeral rather than
# silently behaving like checkpoint.
PERSISTING_MODES = ("eternal", "checkpoint")


def aurora_configured() -> bool:
    """True when the Aurora RDS Data API is wired (cluster + secret ARNs set).

    Drives the client's ability to know whether Aurora-backed features (the
    analytics notebook, this_notebook() usage/cost, read_aurora over v_nb_*) will
    work — the proxy only injects these ARNs into VMs when launched with
    --AuroraSetup. Both must be present to be usable.
    """
    return bool(os.environ.get("AURORA_DATA_API_CLUSTER_ARN")
                and os.environ.get("AURORA_DATA_API_SECRET_ARN"))


def get_persistence_mode() -> str:
    """Read SESSION_PERSISTENCE_MODE, normalized to one of eternal/checkpoint/none."""
    mode = os.environ.get("SESSION_PERSISTENCE_MODE", "checkpoint")
    return mode if mode in PERSISTING_MODES else "none"


# --- VM Pool Manager integration (design §10, §13.4 Option A) --------------
# Gated by POOL_ENABLED (default false — zero change to today's cold-launch
# path when off). Read fresh on every call (not cached at import) so tests can
# monkeypatch os.environ per-test without reimporting the module — mirrors
# get_persistence_mode()'s existing pattern.
_POOL_GRANT_TIMEOUT_SECONDS = 10.0  # bounded — a slow/dead pool manager must never hang a launch. 10s (not 5s) so a slow-but-successful grant (e.g. a ~6s rebind on a freshly-resumed spare) isn't abandoned mid-flight, which wastes the granted VM AND forces a redundant cold launch.
# A RESTORE grant does resume + rebind + /restore-state (heavy S3 pull) inside the
# pool manager, which legitimately runs far longer than a plain rebind-only grant.
# Abandoning a grant the pool is still working on wastes the granted VM AND pays
# a full cold launch on top. Restore grants therefore wait for the pool's whole
# grant budget plus a margin (pool_manager/config.py, the single source of the
# step timeouts); non-restore grants keep the tight 10s so a dead pool still
# fails fast.
_POOL_GRANT_RESTORE_TIMEOUT_SECONDS = PROXY_GRANT_RESTORE_BOUND_SECONDS


def _pool_enabled() -> bool:
    return os.environ.get("POOL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


# Strong references for /launch's concurrent pre-work tasks (catalog staging,
# restore manifest HEAD + size lookup) so a task the request did not await
# (pool hit, early 400) is not garbage-collected mid-flight.
_BACKGROUND_TASKS: set = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _stage_catalog_timed(vm_manager, session_id, catalog_entries):
    """Stage the launch catalog to S3 off the event loop; the S3 key or None.
    Never raises (stage_catalog_to_s3 is best-effort already; this also covers
    the thread hop) so an un-awaited task on a pool hit never logs an error."""
    _t0 = time.perf_counter()
    try:
        return await asyncio.to_thread(
            vm_manager.stage_catalog_to_s3, session_id, catalog_entries)
    except Exception as e:
        logger.warning(f"Launch: catalog staging failed for session {session_id} "
                       f"({type(e).__name__}); VM gets no cloud catalog")
        return None
    finally:
        _timed_log("stage_catalog_s3", (time.perf_counter() - _t0) * 1000.0)


async def _checkpoint_manifest_probe(vm_manager, session_id):
    """('ok', exists) or ('err', exc) for the restore manifest HEAD. Never raises."""
    try:
        return "ok", await asyncio.to_thread(vm_manager.checkpoint_manifest_exists, session_id)
    except Exception as e:
        return "err", e


def _pool_manager_url() -> str:
    return os.environ.get("POOL_MANAGER_URL", "http://localhost:8090")


def _forward_connect_timeout() -> float:
    """Connect-phase timeout (seconds) for the per-cell execute forward. An
    unreachable RUNNING VM must fail CONNECT fast (retryable) instead of
    burning a ~31s TCP SYN-retransmit stall. Read fresh per call so tests can
    monkeypatch os.environ (mirrors get_persistence_mode)."""
    try:
        return float(os.environ.get("PROXY_FORWARD_CONNECT_TIMEOUT", "5.0"))
    except ValueError:
        return 5.0


def _forward_read_timeout() -> float:
    """Read-phase timeout (seconds): a legitimately long cell may run a while.
    Keeps the historical 120s budget; overridable via PROXY_FORWARD_READ_TIMEOUT."""
    try:
        return float(os.environ.get("PROXY_FORWARD_READ_TIMEOUT", "120.0"))
    except ValueError:
        return 120.0


# The VM's pip install gives up at 120s and then returns a classified message
# ("No prebuilt wheel for X on arm64; building from source timed out…"). With a
# 120s read budget the proxy would time out first and the message would be lost.
_INSTALL_FORWARD_READ_TIMEOUT_MIN = 180.0


def _forward_read_timeout_for(path: str) -> float:
    """Read timeout for one forwarded path: /install gets at least 180s."""
    base = _forward_read_timeout()
    if (path or "").strip("/") == "install":
        return max(base, _INSTALL_FORWARD_READ_TIMEOUT_MIN)
    return base


def _fixed_lifecycle_values() -> tuple[int, int]:
    """(idle_timeout_sec, max_duration_sec) FORCED when the pool is enabled — a
    warm-pool spare is minted with these fixed config values and a live VM's
    idle/max-duration can never change after launch, so once pooling is on the
    client's idleTimeoutSeconds/maxDurationSeconds choice is ignored everywhere
    (grant, cold-launch fallback, recovery, restore). This is the "pool
    freeze" referred to in the launch and recovery paths."""
    return (int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")),
            int(os.environ.get("MAX_LIFETIME_SECONDS", "28800")))


async def _try_pool_grant(payload: dict) -> dict | None:
    """POST {POOL_MANAGER_URL}/grant. Returns {"vm_id":..., "endpoint":...} on
    200, else None (409 POOL_EMPTY, any non-200, timeout, connection error, or
    any other exception) so the caller ALWAYS falls through to cold launch.
    Never raises."""
    if not _pool_enabled():
        return None
    # A restore grant (payload carries restore_from) does the heavy /restore-state
    # S3 pull inside the grant, so give it the larger restore budget; a plain
    # launch grant keeps the tight timeout so a dead pool still fails fast.
    grant_timeout = (_POOL_GRANT_RESTORE_TIMEOUT_SECONDS
                     if payload.get("restore_from")
                     else _POOL_GRANT_TIMEOUT_SECONDS)
    # Server-to-server auth: /grant is gated by POOL_AUTH_TOKEN (X-Proxy-Auth).
    headers = {}
    _pool_token = os.environ.get("POOL_AUTH_TOKEN")
    if _pool_token:
        headers["X-Proxy-Auth"] = _pool_token
    try:
        async with httpx.AsyncClient(timeout=grant_timeout) as client:
            resp = await client.post(f"{_pool_manager_url()}/grant", json=payload, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if "vm_id" in data and "endpoint" in data:
                return data
            logger.warning(f"Pool grant 200 but malformed body: {data!r} — falling back to cold launch")
            return None
        logger.info(f"Pool grant miss/error (status={resp.status_code}) — falling back to cold launch")
        return None
    except Exception as e:
        logger.info(f"Pool grant unreachable/failed ({e}) — falling back to cold launch")
        return None


router = APIRouter(tags=["microvm"])

# Per-arch valid memory tiers, sourced from config.sh via MICROVM_SIZES_JSON
# (e.g. {"arm64":[1024,2048,4096,8192],"x86_64":[...]}). ARM and x86_64 may
# offer different tiers, so memory is validated against the SELECTED arch's list
# — not a single global IMAGE_SIZES. Parsed once at import; falls back to the
# legacy IMAGE_SIZES env (then a hardcoded default) if the JSON is missing/bad.
_DEFAULT_SIZES = [1024, 2048, 4096, 8192]


def _load_sizes_by_arch() -> dict:
    raw = os.environ.get("MICROVM_SIZES_JSON")
    if raw:
        try:
            parsed = json.loads(raw)
            out = {a: [int(m) for m in mems] for a, mems in parsed.items() if mems}
            if out:
                return out
        except (ValueError, TypeError) as e:
            logger.warning(f"MICROVM_SIZES_JSON invalid ({e}); falling back to IMAGE_SIZES/default")
    # Legacy/global fallback so a single list still works if the JSON is absent.
    legacy = [int(s) for s in os.environ.get("IMAGE_SIZES", "1024 2048 4096 8192").split()]
    return {DEFAULT_IMAGE_ARCH: legacy or _DEFAULT_SIZES}


_SIZES_BY_ARCH = _load_sizes_by_arch()


def _valid_memories_for(architecture: str) -> list:
    """Memory tiers offered for a given arch, with safe fallbacks."""
    return (
        _SIZES_BY_ARCH.get(architecture)
        or _SIZES_BY_ARCH.get(DEFAULT_IMAGE_ARCH)
        or _DEFAULT_SIZES
    )


class _VmDead(Exception):
    """Raised by the forward helper when a 502 is confirmed to be a dead VM
    (control-plane state TERMINATED/FAILED/etc.), so the caller routes to
    reactive recovery immediately without spending time in the 502-retry loop."""
    def __init__(self, vm_id):
        self.vm_id = vm_id
        super().__init__(f"VM {vm_id} is dead")


@router.post("/proxy/execute-batch")
async def proxy_execute_batch(request: Request):
    """Streaming passthrough for the in-VM /execute-batch SSE endpoint.

    The generic /proxy/{path} forward BUFFERS the whole response (Response(content=
    resp.content)) — fine for JSON, but it would defeat the batch endpoint's per-cell
    streaming (the client wouldn't see a cell's result until ALL cells finished). This
    dedicated route resolves session→VM + auth token the same way, then STREAMS the
    text/event-stream body through with httpx's client.stream(), so per-cell frames
    reach the browser as they're produced.

    v1 scope: no mid-stream reactive recovery. A VM death mid-stream surfaces as a
    broken stream / error to the client (which falls back to a run-level error). The
    happy path (VM alive for the whole run) is what this optimizes.
    """
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")
    if not session_id:
        return json_error(400, "X-Session-Id header required")

    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        return json_error(404, "Session not found — VM may have been terminated")

    microvm_id = session_vm["vm_id"]
    endpoint = session_vm["endpoint"]
    body = await request.body()
    content_type = request.headers.get("Content-Type", "application/json")
    token = await vm_manager.get_auth_token(microvm_id)
    target_url = f"https://{endpoint}/execute-batch"
    headers = {"X-aws-proxy-auth": token, "Content-Type": content_type}

    # DAG capture (design §4.4): cell_result frames are forwarded unchanged and
    # captured, the VM's runtime frame is swallowed, and a {"type":"dag"} frame is
    # emitted right before done. A break before done applies the runs in the background.
    dag_service.use_vm_manager(vm_manager)
    capture = dag_service.BatchCapture(session_id, body, microvm_id)

    async def relay():
        # A long timeout: a big Run All can legitimately take a while. The whole run
        # is ONE request now, so this is the run budget, not a per-cell budget.
        try:
            async with http_clients.vm_client().stream(
                    "POST", target_url, headers=headers, content=body, timeout=600.0) as resp:
                async for chunk in capture.relay(resp.aiter_bytes()):
                    yield chunk
        except Exception as e:
            logger.warning(f"Proxy execute-batch stream to {microvm_id} failed: {type(e).__name__}: {e!r}")
            # Surface a terminal error frame so the client resolves the run instead of
            # hanging (mid-stream VM death / network drop — no recovery in v1).
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n".encode()
        finally:
            capture.close()

    return StreamingResponse(relay(), media_type="text/event-stream")


# Deterministic wake for the set-env path: how long we wait for a suspended VM to
# reach RUNNING before injecting. Mirrors the terminal's _ensure_vm_awake budget;
# reads follow the shared resume schedule (proxy/platform/resume_wait.py).
_SETENV_RESUME_WAIT_BUDGET_SEC = 30.0
# One in-flight wake per microvm_id: a .env import sends one set-env per key in
# parallel, and each would otherwise run its own resume + poll loop on the same
# VM (N x GetMicrovm at the fast schedule). Later callers await the first one.
_setenv_wakes: dict = {}


async def _resume_and_wait(vm_manager, microvm_id: str) -> str:
    """Single-flight wrapper around _resume_and_wait_once: concurrent set-env
    calls for the same VM share one resume + wait and get the same state."""
    loop = asyncio.get_running_loop()
    task = _setenv_wakes.get(microvm_id)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_resume_and_wait_once(vm_manager, microvm_id))
        _setenv_wakes[microvm_id] = task

        def _done(t, _id=microvm_id):
            if _setenv_wakes.get(_id) is t:
                _setenv_wakes.pop(_id, None)
            if not t.cancelled():
                t.exception()  # retrieved: no "never retrieved" log without waiters
        task.add_done_callback(_done)
    # shield: one caller's disconnect must not cancel the wake the others await.
    return await asyncio.shield(task)


async def _resume_and_wait_once(vm_manager, microvm_id: str) -> str:
    """Proactively resume a SUSPENDED VM and poll until RUNNING (HTTP-path twin of
    terminal._ensure_vm_awake, without the WebSocket status line).

    Reads authoritative control-plane state; if the VM is SUSPENDED it issues an
    explicit resume_vm and polls to RUNNING (RESUMING/PENDING are just polled; a
    SUSPENDING VM is resumed once it reads SUSPENDED). Returns the last observed state — "RUNNING" on
    success, a terminal state if the VM died, or the last transitional state if it
    didn't wake within the budget (caller can still try the forward)."""
    try:
        state = await vm_manager.get_vm_state(microvm_id)
    except Exception as e:
        logger.warning(f"set-env: could not read state for {microvm_id}: {e}")
        return "UNKNOWN"  # inconclusive — let the caller forward anyway

    if state in ("RUNNING", "UNKNOWN"):
        return state
    if state in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        return state  # genuinely gone — caller surfaces the error

    # SUSPENDED: resume now. SUSPENDING settles at SUSPENDED, so the shared wait
    # resumes once it reads SUSPENDED (also after a failed resume).
    st = await resume_wait.wake_and_wait(
        state, lambda: vm_manager.get_vm_state(microvm_id),
        lambda: vm_manager.resume_vm(microvm_id), _SETENV_RESUME_WAIT_BUDGET_SEC,
        dead_states=("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"), retry_errors=True,
        vm_id=microvm_id, who="set-env", log=logger)
    if st == "RUNNING":
        return st
    if st in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        return st
    return state  # didn't reach RUNNING in the budget — caller may still try


@router.post("/proxy/set-env")
async def proxy_set_env(request: Request):
    """Inject ONE env var into a session's LIVE VM os.environ (no relaunch).

    Unlike the generic /proxy/{path} forward (which relies on AWS auto-resume +
    a single retry racing the wake), this DETERMINISTICALLY resumes a SUSPENDED VM
    and waits for RUNNING before injecting — so adding an env var from the Secrets &
    Env panel reliably lands on a sleeping VM instead of best-effort. The value is
    ALSO persisted separately by the client (PUT /notebooks/{id}/env, sealed), so
    this live inject is purely the "apply now" half; persistence is the source of
    truth re-injected on every launch/restore.

    Body: {"key": "<NAME>", "value": "<value>"}. Requires X-Session-Id."""
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")
    if not session_id:
        return json_error(400, "X-Session-Id header required")

    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        return json_error(404, "Session not found — VM may have been terminated")

    microvm_id = session_vm["vm_id"]
    endpoint = session_vm["endpoint"]

    # Wake a sleeping VM FIRST (deterministic), then inject.
    state = await _resume_and_wait(vm_manager, microvm_id)
    if state in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        return json_error(409, "VM is no longer available", state=state)

    body = await request.body()
    content_type = request.headers.get("Content-Type", "application/json")
    try:
        token = await vm_manager.get_auth_token(microvm_id)
        resp = await http_clients.vm_client().post(
            f"https://{endpoint}/set-env",
            headers={"X-aws-proxy-auth": token, "Content-Type": content_type},
            content=body,
            timeout=30.0,
        )
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))
    except Exception as e:
        logger.warning(f"set-env forward to {microvm_id} failed: {type(e).__name__}: {e!r}")
        return json_error(502, f"set-env failed: "
                               f"{type(e).__name__}: {e!r}")


# DAG capture (design §4.3): POSTs to these VM endpoints carry facts/runtime the
# proxy folds into the notebook's DAG document.
# variables/delete (the Variables panel's "Clean up untracked" action) is
# runtime-only: it returns the VM's refreshed runtime section.
_CAPTURE = {"execute", "execute-sql", "set-variable", "reset", "variables/delete"}
# VM endpoints only the proxy's own client may call (never via the browser forward).
_PROXY_INTERNAL = {"dag"}


def _internal_target(path: str) -> bool:
    """True when the forward must be refused (403, no VM call).

    `path` is already percent-DECODED by Starlette. Characters httpx would
    re-interpret when _forward rebuilds the URL are never legitimate in a VM route
    path (no VM route declares a path parameter): '?' (query split), '#'
    (fragment split), '\\', control chars, and '%' (a decoded '%' means the
    browser double-encoded something, e.g. %2564ag or .%252e). Dot-segments are
    refused on both the decoded input and the path httpx will actually send, and
    the first SENT segment (lower-cased) must not be proxy-internal. An httpx
    parse failure fails closed."""
    if any(ch in path for ch in "?#\\%") or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return True
    try:
        sent = httpx.URL(f"https://x/{path}").path
    except Exception:
        return True
    for segs in ([s for s in path.split("/") if s], [s for s in sent.split("/") if s]):
        if any(s in (".", "..") for s in segs):
            return True
    segs = [s for s in sent.lower().split("/") if s]
    return bool(segs) and segs[0] in _PROXY_INTERNAL


@router.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_request(path: str, request: Request):
    """Generic forward to the session's MicroVM (see _proxy_request_impl), plus
    the DAG hooks: proxy-internal targets are refused before any VM call, and a
    200 POST to a capture endpoint gets its run folded into the notebook's DAG
    document (the response gains `dag`, loses the VM's `runtime`)."""
    if _internal_target(path):
        logger.info("Refused proxy-internal forward target %r", path)
        return json_error(403, "proxy-internal endpoint")
    p = path.strip("/").lower()
    # Deleted-cell flag (F1): sync the VM's DAG copy before a variables read and
    # overlay the DAG-aware "deleted cell" flags after it (see var_orphans).
    orphan_ctx = (await var_orphans.before_forward(p, request)
                  if p in var_orphans.HOOK_PATHS else None)
    resp = await _proxy_request_impl(path, request)
    if request.method == "POST" and p in var_orphans.WRITE_PATHS:
        await var_orphans.note_writes(p, request, resp)   # no-cell writer (agent vs UI/API)
    if request.method == "POST" and p in _CAPTURE and resp.status_code == 200:
        resp = await dag_service.capture_forwarded(p, request, resp)
    if request.method == "POST" and p == "install":
        # Packages panel user list: drop the cached copy; a successful uninstall
        # also leaves the in-memory /track-install record.
        from proxy.platform import user_packages
        try:
            user_packages.note_install_forward(
                request.app.state.vm_manager, request.headers.get("X-Session-Id"),
                await request.body(), resp)
        except Exception as e:
            logger.debug(f"user package bookkeeping skipped: {type(e).__name__}")
    if orphan_ctx is not None:
        resp = var_orphans.after_forward(p, resp, orphan_ctx)
    return resp


async def _proxy_request_impl(path: str, request: Request):
    """
    Proxy requests to the MicroVM serving a session.

    The caller sends only `X-Session-Id` — the proxy resolves the current VM
    endpoint internally from the session registry. This is stable across
    VM rotations (eternal mode) and works identically in checkpoint mode.

    If the session is mid-rotation (quiesced), requests are queued and replayed
    on the new VM after the swap completes.
    """
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")

    if not session_id:
        return json_error(400, "X-Session-Id header required")

    # --- Resolve session → VM from the session registry ---
    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        # Cold-start registration race: a launch for this session may be in flight
        # but register_session hasn't landed yet. The client (editor /helpers +
        # /completions-catalog loaders, panels' /variables + /files refreshers)
        # already fires as soon as it has an endpoint + "connected" tab, so these
        # race the launch's final registration step. Returning 404 immediately is
        # functionally recovered by client retries, but each failed attempt logs a
        # browser-console 404 (the browser logs any 404 the app can't suppress).
        # So: if a launch is marked in flight for this session, briefly WAIT for the
        # mapping to appear instead of 404ing. A genuinely terminated / never-
        # existed session is NOT marked launching, so it still 404s immediately.
        if vm_manager.is_session_launching(session_id):
            # Poll up to ~6s (12 × 0.5s) — longer than a normal register lands, short
            # enough that a stuck launch doesn't hang the request. Stop early the
            # moment the mapping appears or the launch marker clears (success/fail).
            for _ in range(12):
                await asyncio.sleep(0.5)
                session_vm = await vm_manager.get_session_vm_async(session_id)
                if session_vm:
                    break
                if not vm_manager.is_session_launching(session_id):
                    # Launch finished without registering (failed/rolled back) OR
                    # the marker expired — re-check once more, then give up to 404.
                    session_vm = await vm_manager.get_session_vm_async(session_id)
                    break
        if not session_vm:
            # Fix 2 (defense in depth): the session→VM mapping is genuinely gone
            # (not a launch race). _mark_vm_dead no longer unregisters eternal
            # sessions on VM death (Fix 1), so this should now be rare in eternal
            # mode — but it's still possible via a residual path (a future
            # unregister call, DB/registry eviction, a proxy restart that lost the
            # L1 cache, etc.). If a short-lived eternal-death tombstone exists for
            # this session, treat it exactly like the _VmDead branch below: build
            # request_data and recover instead of 404ing. recover_and_replay still
            # raises (→ loud 5xx) if recovery ultimately fails — no silent retry
            # loop. A session with NO tombstone (never existed, or explicitly
            # terminated by the user — unregister_session never tombstones that
            # case) still 404s immediately, in every mode.
            tombstoned_vm_id = None
            if get_persistence_mode() == "eternal":
                tombstoned_vm_id = vm_manager.pop_eternal_death_tombstone(session_id)
            if tombstoned_vm_id:
                logger.info(
                    f"Session {session_id}: mapping gone but eternal death tombstone "
                    f"found (last vm={tombstoned_vm_id}) — routing to recovery instead of 404"
                )
                body = await request.body()
                content_type = request.headers.get("Content-Type", "application/json")
                request_data = {
                    "method": request.method,
                    "path": f"/{path}",
                    "headers": {"Content-Type": content_type},
                    "body": body,
                }
                try:
                    recover_seq = vm_manager.assign_arrival_seq(session_id)
                    resp = await vm_manager.recover_and_replay(
                        session_id, request_data, seq=recover_seq, old_vm_id=tombstoned_vm_id
                    )
                    return Response(
                        content=resp.content,
                        status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"),
                    )
                except Exception as e:
                    logger.error(f"🔁 Reactive recovery (tombstone path) failed for {session_id}: {e}")
                    return Response(
                        content=json.dumps({
                            "error": "Couldn't restore your session onto a new VM after retries. "
                                     "Your work is safe in storage — please retry.",
                            "recovery": "failed", "retryable": True, "session_safe": True,
                            "session_id": session_id, "detail": str(e),
                        }),
                        status_code=503, media_type="application/json",
                    )
            return json_error(404, "Session not found — VM may have been terminated")

    microvm_id = session_vm["vm_id"]
    microvm_endpoint = session_vm["endpoint"]

    # --- Read body once (request.body() is single-use) ---
    body = await request.body()
    content_type = request.headers.get("Content-Type", "application/json")
    persistence_mode = get_persistence_mode()
    # Capture a monotonic arrival sequence NOW (at proxy entry) so that if this
    # request ends up queued for reactive recovery, it replays in true arrival
    # order — independent of how long its failed forward + state check take.
    arrival_seq = vm_manager.assign_arrival_seq(session_id) if persistence_mode == "eternal" else None

    async def _forward(vm_id: str, endpoint: str):
        """Forward the request to a specific VM. Returns the httpx Response.
        Raises _VmDead if a 502 is due to a genuinely dead VM (so the caller
        routes to recovery IMMEDIATELY — no retry wait). Raises on connection
        failure. A 502 from an alive-but-resuming VM is retried (auto-resume)."""
        token = await vm_manager.get_auth_token(vm_id)
        target_url = f"https://{endpoint}/{path}"
        headers = {"X-aws-proxy-auth": token, "Content-Type": content_type}
        # Split connect vs read: an unreachable RUNNING VM fails CONNECT fast
        # (~5s, retryable) instead of burning a ~31s TCP stall, while a
        # legitimately long-running cell keeps the full READ budget. write
        # mirrors read so a large request body upload isn't cut off; pool
        # mirrors connect so acquiring a pooled connection can't hang longer
        # than a connect.
        _read_t = _forward_read_timeout_for(path)
        _connect_t = _forward_connect_timeout()
        _timeout = httpx.Timeout(connect=_connect_t, read=_read_t,
                                 write=_read_t, pool=_connect_t)
        client = http_clients.vm_client()

        async def _send():
            # Shared client; the split timeout is applied PER REQUEST. GET/HEAD
            # get one re-send on a pre-response RemoteProtocolError/ReadError
            # (stale pooled keep-alive); every other method is sent as-is (only
            # the transport's connect-phase retries apply — never a re-send).
            if request.method.upper() in http_clients.IDEMPOTENT_METHODS:
                return await http_clients.request_idempotent(
                    client, request.method, target_url,
                    headers=headers, content=body, timeout=_timeout,
                )
            return await client.request(
                method=request.method, url=target_url, headers=headers,
                content=body, timeout=_timeout,
            )

        resp = await _send()
        if resp.status_code == 502:
            # A 502 can mean (a) the VM is auto-resuming from suspend (retry),
            # or (b) the VM is dead/terminating. Ask the authoritative control-
            # plane state BEFORE spending ~5s in the retry loop — otherwise
            # concurrent requests each burn 5s racing (and, in eternal mode,
            # reach the recovery queue out of order, breaking FIFO). This check
            # runs in ALL modes now: eternal routes _VmDead to recovery, while
            # checkpoint/none routes it to stale-mapping cleanup + a clean 404
            # (handled by the caller) — either way we skip the pointless retries
            # against a VM the control-plane says is gone.
            state = await vm_manager.get_vm_state(vm_id)
            if state in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
                raise _VmDead(vm_id)  # skip retries → dead-VM handling
            # Not dead: retry — likely mid auto-resume.
            for retry in range(2):
                wait = 2 + retry
                logger.info(f"Got 502 from {vm_id}, retry {retry + 1}/2 in {wait}s...")
                await asyncio.sleep(wait)
                resp = await _send()
                if resp.status_code != 502:
                    break
        return resp

    def _to_response(resp):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    async def _resolve_failed_state() -> str:
        """VM lifecycle state after a failed forward: 'TERMINATED' when _forward
        already state-confirmed the VM dead (vm_confirmed_dead), else a fresh
        control-plane lookup. Shared by the eternal + non-eternal failure blocks."""
        if vm_confirmed_dead:
            return "TERMINATED"
        st = await vm_manager.get_vm_state(microvm_id)
        logger.info(f"Session {session_id}: forward failed, VM {microvm_id} state={st}")
        return st

    async def _retry_forward_once():
        """One more forward against a VM the control-plane says is alive-but-resuming
        (SUSPENDED/RESUMING/…); auto-resume holds the request server-side. Sets
        response/forward_error via nonlocal. Shared by both failure blocks."""
        nonlocal response, forward_error
        logger.info(f"Session {session_id}: VM {microvm_id} resuming — retrying forward")
        try:
            response = await _forward(microvm_id, microvm_endpoint)
            forward_error = None
        except Exception as e:
            forward_error = e

    async def _reresolve_and_retry_once():
        """RUNNING/UNKNOWN fall-through only: a forward RAISED (not a 502 response)
        against a VM the control-plane still calls RUNNING. This is the transient
        'RUNNING-but-unreachable' / in-flight-stale-mapping case. Invalidate L1 so
        the re-read hits the authoritative DB, re-resolve the session->VM mapping
        (picks up a rotation/rebind), and retry the forward ONCE. Single shot: a
        genuine failure still surfaces as 502. Does NOT touch _502_strikes — the
        caller's existing bookkeeping runs once after this, and on success
        forward_error is cleared so no strike is counted."""
        nonlocal response, forward_error, microvm_id, microvm_endpoint
        vm_manager.invalidate_session_vm(session_id)
        sv = await vm_manager.get_session_vm_async(session_id)
        if not sv:
            return  # mapping gone (DB outage after invalidation) — surface the 502 as-is
        new_id, new_endpoint = sv["vm_id"], sv["endpoint"]
        logger.info(f"Session {session_id}: forward failed on RUNNING VM {microvm_id}; "
                    f"re-resolve+retry once -> {new_id}")
        try:
            response = await _forward(new_id, new_endpoint)
            forward_error = None
            microvm_id, microvm_endpoint = new_id, new_endpoint
        except Exception as e:
            forward_error = e
            # Point bookkeeping at the VM we actually just tried.
            microvm_id, microvm_endpoint = new_id, new_endpoint

    _VM_DEAD_STATES = ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND")
    _VM_RESUMING_STATES = ("SUSPENDED", "SUSPENDING", "RESUMING", "PENDING")

    # --- Recovery gate (eternal): if a reactive recovery is already in progress
    # for this session, DO NOT try to forward (the VM is dead/being replaced).
    # Enqueue immediately in arrival order via recover_and_replay so FIFO holds.
    # Without this, each concurrent request would race its own forward + state
    # check and reach the queue out of order (breaking ordered cell execution). ---
    if persistence_mode == "eternal" and vm_manager.is_recovering(session_id):
        request_data = {
            "method": request.method,
            "path": f"/{path}",
            "headers": {"Content-Type": content_type},
            "body": body,
        }
        try:
            response = await vm_manager.recover_and_replay(session_id, request_data, seq=arrival_seq)
            sv = await vm_manager.get_session_vm_async(session_id)
            if sv:
                microvm_id = sv["vm_id"]
            if microvm_id in vm_manager.active_microvms:
                vm_manager.active_microvms[microvm_id]["_502_strikes"] = 0
            return _to_response(response)
        except Exception as e:
            logger.error(f"🔁 Reactive recovery (gated) failed for {session_id}: {e}")
            return Response(
                content=json.dumps({
                    "error": "Couldn't restore your session onto a new VM after retries. "
                             "Your work is safe in storage — please retry.",
                    "recovery": "failed", "retryable": True, "session_safe": True,
                    "session_id": session_id, "detail": str(e),
                }),
                status_code=503, media_type="application/json",
            )

    # --- Attempt 1: forward to the currently-registered VM ---
    forward_error = None
    vm_confirmed_dead = False
    response = None
    try:
        response = await _forward(microvm_id, microvm_endpoint)
    except _VmDead:
        # _forward already confirmed (via control-plane state) the VM is dead and
        # skipped the retry loop. Eternal routes this to recovery; checkpoint/none
        # routes it to stale-mapping cleanup + a clean 404 (both below, keyed off
        # vm_confirmed_dead — no re-check of the control-plane needed).
        vm_confirmed_dead = True
        logger.info(f"Session {session_id}: forward hit dead VM {microvm_id}")
    except Exception as e:
        forward_error = e
        logger.warning(f"Proxy forward to {microvm_id} failed: {type(e).__name__}: {e!r}")

    # Decide whether to recover. In eternal mode:
    #   - _VmDead  → recover immediately (already state-confirmed dead).
    #   - connection error / persistent 5xx → confirm via control-plane state.
    forward_failed = forward_error is not None or (response is not None and response.status_code >= 502)
    if persistence_mode == "eternal" and (vm_confirmed_dead or forward_failed):
        vm_state = await _resolve_failed_state()
        if vm_state in _VM_DEAD_STATES:
            # VM is genuinely gone → reactive recovery, preserving FIFO order.
            # recover_and_replay() handles the launch+restore (once, under a lock)
            # AND replays this + any concurrent requests in arrival order on the
            # new VM. It raises if recovery ultimately fails, so we 5xx loudly and
            # the good S3 checkpoint is preserved for a retry.
            request_data = {
                "method": request.method,
                "path": f"/{path}",
                "headers": {"Content-Type": content_type},
                "body": body,
            }
            try:
                response = await vm_manager.recover_and_replay(session_id, request_data, seq=arrival_seq)
                forward_error = None
                sv = await vm_manager.get_session_vm_async(session_id)
                if sv:
                    microvm_id = sv["vm_id"]  # for bookkeeping
            except Exception as e:
                logger.error(f"🔁 Reactive recovery failed for {session_id}: {e}")
                # Distinguishable, UI-friendly failure: the S3 checkpoint is intact
                # (we fail closed before overwriting it), so this is retryable — the
                # next request will attempt recovery again from the same checkpoint.
                return Response(
                    content=json.dumps({
                        "error": "Couldn't restore your session onto a new VM after retries. "
                                 "Your work is safe in storage — please retry.",
                        "recovery": "failed",
                        "retryable": True,
                        "session_safe": True,
                        "session_id": session_id,
                        "detail": str(e),
                    }),
                    status_code=503,
                    media_type="application/json",
                )
        elif vm_state in _VM_RESUMING_STATES:
            # NOT dead — VM is (or will be) alive; the 502 was likely mid auto-resume.
            await _retry_forward_once()
        else:
            # RUNNING / UNKNOWN: the forward RAISED but the control-plane still says
            # the VM is alive — a transient RUNNING-but-unreachable blip or an
            # in-flight stale/rotated mapping. One re-resolve + retry (invalidates
            # L1 first); if it still fails we surface the 502 below, unchanged.
            # Guard on forward_error so a 502 *response* (already exhausted by
            # _forward's 502-RESPONSE retry loop) is NOT re-driven here — this
            # retry handles the forward RAISING, never a 502 response.
            if forward_error is not None:
                await _reresolve_and_retry_once()

    # --- Non-eternal modes (checkpoint / none): detect a DEAD VM and clear the
    # stale session→VM mapping. There is no reactive recovery here, but a 502 that
    # persists through _forward's retries is almost always a terminated/suspended-
    # out VM whose session_registry row outlived it (e.g. across a proxy restart or
    # a teardown). Without this, every client poll (/helpers, /completions-catalog,
    # /variables) keeps resolving the orphan row and 502ing forever, burning the
    # full retry budget each time. We confirm liveness via the control-plane, and
    # if the VM is genuinely gone we unregister the mapping and return a CLEAN 404
    # ("VM terminated") — the same signal a never-registered session gets, which
    # the client's warm-up loaders already handle (they stop retrying), instead of
    # a 502 the browser logs and loops on. A SUSPENDED VM is left mapped and gets
    # one more forward attempt (its own auto-resume holds the request). ---
    if persistence_mode != "eternal" and (vm_confirmed_dead or forward_failed):
        vm_state = await _resolve_failed_state()
        if vm_state in _VM_DEAD_STATES:
            # Orphan mapping → drop it so this session stops 502ing on every poll.
            vm_manager.unregister_session(session_id)
            logger.info(f"Session {session_id}: cleared stale mapping to dead VM "
                        f"{microvm_id} — returning 404 (relaunch needed)")
            return Response(
                content=json.dumps({
                    "error": "Session not found — VM has been terminated. Relaunch to continue.",
                    "vm_terminated": True,
                    "session_id": session_id,
                }),
                status_code=404,
                media_type="application/json",
            )
        if vm_state in _VM_RESUMING_STATES:
            # Alive-but-resuming → one more forward; auto-resume holds it server-side.
            await _retry_forward_once()
        elif forward_error is not None:
            # RUNNING / UNKNOWN: re-resolve + retry once (see eternal branch).
            await _reresolve_and_retry_once()

    # --- Health-strike bookkeeping + response ---
    if forward_error is not None:
        if microvm_id in vm_manager.active_microvms:
            vm_manager.active_microvms[microvm_id]["_502_strikes"] = \
                vm_manager.active_microvms[microvm_id].get("_502_strikes", 0) + 1
        return json_error(502, f"Proxy request failed: "
                               f"{type(forward_error).__name__}: {forward_error!r}")

    if microvm_id in vm_manager.active_microvms:
        if response.status_code >= 502:
            vm_manager.active_microvms[microvm_id]["_502_strikes"] = \
                vm_manager.active_microvms[microvm_id].get("_502_strikes", 0) + 1
        else:
            vm_manager.active_microvms[microvm_id]["_502_strikes"] = 0
            if response.status_code < 500:
                vm_manager.active_microvms[microvm_id]["last_active"] = time.time()
                # A served request proves the VM is RUNNING: log an auto-resume
                # that /instances polls may never see (vm-card-state, resume_count).
                await _note_vm_served(vm_manager, microvm_id)

    return _to_response(response)


async def _note_vm_served(vm_manager, microvm_id: str) -> None:
    """Record RUNNING after a successful forward when the tracker last saw the
    VM suspended. Steady state (already RUNNING) does no DB hop."""
    try:
        if vm_manager.cost_tracker.note_served(microvm_id):
            await astorage.vm_session_update_state(microvm_id, "RUNNING")
    except Exception:
        pass  # cost bookkeeping only — never fail a forwarded request


async def _persist_notebook_link(notebook_id, session_id, microvm_id, checkpoint_enabled) -> None:
    """Persist the notebook → VM link (notebooks.session_id / microvm_id /
    checkpoint_enabled) server-side at launch time.

    Reopen reads exactly these columns to decide auto-reconnect vs the 'Connect to
    Sandbox' modal. Previously only the browser's debounced autosave wrote them, and
    that autosave only re-arms on user-content edits — so open → VM linked (a pool
    grant lands in ~1-5s) → close with no edit left the row unlinked and the reopen
    showed the modal for a perfectly alive VM. /launch knows the authoritative link
    for every launch flavor (pool grant, cold launch, idempotent reuse), so write it
    here. Update-only (a missing/deleted notebook row is a no-op) and best-effort:
    it never raises, so it can never fail or roll back a launch."""
    if notebook_id is None or not microvm_id:
        return
    try:
        await astorage.notebook_update(
            str(notebook_id), session_id=session_id, microvm_id=microvm_id,
            checkpoint_enabled=1 if checkpoint_enabled else 0,
        )
    except Exception as e:
        logger.warning(f"Launch: could not persist notebook {notebook_id} → VM {microvm_id} link ({e})")
    finally:
        # Both sides of a (re)link: the notebook's previous session and this one.
        dag_service.invalidate_notebook(notebook_id)
        dag_service.invalidate_session(session_id)


@router.post("/launch")
async def launch_microvm(request: Request):
    """Launch a new MicroVM sandbox instance."""
    vm_manager = request.app.state.vm_manager
    body = await request.json() if await request.body() else {}
    notebook_name = body.get("name", f"notebook-{int(time.time())}")
    # The notebook's integer PK (DB-generated). Optional: a VM can be launched
    # before its notebook row exists, or for an ephemeral notebook. Coerce to int
    # when present so it lands in vm_sessions.notebook_id (a BIGINT FK) correctly;
    # NULL otherwise. This is DISTINCT from notebook_name (a display label) — the
    # two were previously conflated (notebook_id was set to the name string).
    notebook_id = body.get("notebookId")
    try:
        notebook_id = int(notebook_id) if notebook_id not in (None, "") else None
    except (TypeError, ValueError):
        notebook_id = None
    # CPU architecture (per-VM). Default to the configured default; validate it's
    # an arch this proxy actually offers (images built for it), else fall back.
    # Resolved BEFORE memory so we validate memory against THIS arch's tiers.
    architecture = body.get("arch") or DEFAULT_IMAGE_ARCH
    if architecture not in IMAGE_ARCHES:
        logger.warning(f"Requested arch '{architecture}' not in built arches {IMAGE_ARCHES}; "
                       f"falling back to {DEFAULT_IMAGE_ARCH}")
        architecture = DEFAULT_IMAGE_ARCH
    # Valid memory tiers for the SELECTED arch (from MICROVM_SIZES_JSON). ARM and
    # x86_64 need not offer the same tiers, so snap to the nearest tier for THIS arch.
    memory_mib = body.get("memoryMiB", DEFAULT_MEMORY_MIB)
    valid_memories = _valid_memories_for(architecture)
    if memory_mib not in valid_memories:
        memory_mib = min(valid_memories, key=lambda x: abs(x - memory_mib))
    # Idle timeout: use frontend value if provided, otherwise fall back to config
    config_idle = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60"))
    idle_timeout_sec = int(body.get("idleTimeoutSeconds", config_idle))
    # Max duration: use frontend value if provided, otherwise fall back to config
    config_max = int(os.environ.get("MAX_LIFETIME_SECONDS", "28800"))
    max_duration_sec = min(int(body.get("maxDurationSeconds", config_max)), 28800)  # Cap at AWS max (8h)

    # Pool freeze (see _fixed_lifecycle_values):
    # a warm-pool spare is minted with FIXED idle/max values and these cannot change
    # on a live VM, so once pooling is on we IGNORE the client-sent values here —
    # on BOTH the pool-grant path below and the cold-launch fallback path. Computed
    # once and reused below so the SAVED-value restore block (which runs next) can
    # also respect the freeze instead of letting a saved value defeat it.
    pool_fixed = _pool_enabled()
    if pool_fixed:
        idle_timeout_sec, max_duration_sec = _fixed_lifecycle_values()

    # Persistence mode drives whether session state is checkpointed to S3.
    # Three modes: "eternal", "checkpoint", "none". Anything that isn't a
    # recognized persisting mode is treated as "none" (ephemeral) — no S3 save,
    # no restore. checkpoint_enabled is derived from the mode (NOT hardcoded):
    # eternal + checkpoint persist; none does not.
    persistence_mode = get_persistence_mode()
    checkpoint_enabled = persistence_mode in PERSISTING_MODES

    # In "none" mode the session is ephemeral, so ignore any restore request —
    # there is no durable checkpoint to restore from.
    restore_from = body.get("restoreFromSession") if checkpoint_enabled else None

    # A launch for a known notebook WITHOUT a sessionId whose notebooks.session_id
    # /terminate cleared: re-link the notebook's last checkpointed session (never
    # another notebook's, never an older one; proxy/notebook/session_relink.py)
    # and restore it, BEFORE the same-arch pin below so the saved arch/tier win.
    relinked_sid = None
    _nb_row_loaded = False  # True once the relink lookup read the notebook row
    _nb_row = None
    if notebook_id is not None and not body.get("sessionId") and checkpoint_enabled:
        try:
            from proxy.notebook.session_relink import relinked_session_id
            _nb_row = await astorage.notebook_get(str(notebook_id))
            _nb_row_loaded = True
            relinked_sid = await relinked_session_id(_nb_row, vm_manager)
        except Exception as e:
            logger.warning(f"Launch: session re-link lookup failed for notebook {notebook_id} ({e})")
            relinked_sid = None
        if relinked_sid and not restore_from:
            restore_from = relinked_sid
            logger.info(f"Launch: notebook {notebook_id} has no linked session; re-linking its "
                        f"last checkpointed session {relinked_sid} and restoring it")

    # SAME-ARCH / SAME-TIER RESTORE (server-enforced). A checkpoint can only be
    # restored onto a VM of the SAME architecture it was saved on, and it makes no
    # sense to resurface a session on a different memory tier than the user had.
    # Rather than trust the client to send the right arch/memory on reopen, look up
    # the most-recent VM session for this session_id (via the storage INTERFACE) and
    # override arch + memory with the saved values. The client can therefore trigger
    # an auto-restore knowing only the session_id; correctness lives here. Best-effort:
    # if no prior session is found we keep the client-provided/validated values.
    # An explicit restore choice (restoreOverride: true plus arch / memoryMiB) skips the
    # pin below and is resolved after the saved values are known. Without it the saved
    # values are pinned exactly as before (auto-restore, relink, recovery).
    _restore_override = body.get("restoreOverride") is True
    _restore_memory_check = None  # set below for a restore; returned as restore_memory_check
    if restore_from:
        # Restore pre-work runs CONCURRENTLY with the saved-attributes storage read
        # below: the S3 manifest HEAD and the metadata.json size lookup depend only
        # on restore_from. Neither task raises, so the early 400 return is safe.
        _head_task = _spawn_background(_checkpoint_manifest_probe(vm_manager, restore_from))
        _size_task = _spawn_background(_restore_checkpoint_size_bounded(vm_manager, restore_from))
        _saved_arch = _saved_mem = None
        try:
            prior = await astorage.vm_session_list_for_session(restore_from)
            if prior:
                last = latest_vm_session(prior)  # last VM to serve the session (pool VMs keep their mint launched_at)
                saved_arch = last.get("architecture")
                saved_mem = last.get("memory_mib")
                if not saved_arch or not saved_mem:
                    # An older row without arch/tier: infer them from the image
                    # of that (terminated) VM, so the restore does not fall back
                    # to the request/default arch.
                    img_arch, img_mem = arch_size_from_image(last.get("image_arn"))
                    if not img_arch and last.get("microvm_id"):
                        try:
                            _d = await vm_manager.get_microvm_detail(last["microvm_id"])
                            img_arch, img_mem = arch_size_from_image((_d or {}).get("imageArn"))
                        except Exception:
                            pass
                    saved_arch = saved_arch or img_arch
                    saved_mem = saved_mem or img_mem
                    if img_arch:
                        logger.info(f"Restore: session {restore_from} has no saved arch/tier; "
                                    f"inferred {img_arch}/{img_mem} from the last VM's image")
                _saved_arch, _saved_mem = saved_arch, saved_mem
                if _restore_override:
                    pass  # resolved below by resolve_restore_choice
                elif saved_arch and saved_arch in IMAGE_ARCHES and saved_arch != architecture:
                    logger.info(f"Restore: overriding arch {architecture} → saved {saved_arch} "
                                f"for session {restore_from} (same-arch restore)")
                    architecture = saved_arch
                elif saved_arch and saved_arch not in IMAGE_ARCHES:
                    # The saved arch is no longer offered (e.g. x86_64 dropped by
                    # the region guard or IMAGE_ARCHES). Restoring on another arch
                    # is safe: user packages are re-installed by NAME on restore
                    # (so the new arch's wheels are fetched) and the dill
                    # namespace is arch-independent between arm64 and x86_64.
                    logger.warning(f"Restore: session {restore_from} was saved on {saved_arch}, "
                                   f"which this proxy no longer offers ({IMAGE_ARCHES}); "
                                   f"restoring on {architecture}")
                if saved_mem and not _restore_override:
                    # Re-snap against THIS (possibly overridden) arch's valid tiers.
                    _valid = _valid_memories_for(architecture)
                    memory_mib = saved_mem if saved_mem in _valid else min(_valid, key=lambda x: abs(x - saved_mem))
                    logger.info(f"Restore: using saved memory tier {memory_mib} MiB for session {restore_from}")
                # Reuse the FULL lifecycle policy from the prior VM, not just
                # arch/memory. On reopen the client doesn't know (or shouldn't have to
                # resend) the notebook's chosen idle-suspend and max-lifetime, so a
                # relaunch would fall back to the defaults (e.g. a VM saved with a
                # 5-min idle would come back with the 60 s default). Carry the saved values so the
                # reopened VM behaves identically to the one it replaces. Each is
                # validated independently (a missing/garbage column falls back to the
                # already-computed default) and max_duration is capped at the AWS 8h max
                # exactly like the normal path.
                # pooled ⇒ fixed values win over saved values too (pool freeze); only
                # apply the saved idle/max when the pool is NOT enforcing the freeze.
                if not pool_fixed:
                    saved_idle = last.get("idle_timeout_sec")
                    if isinstance(saved_idle, int) and saved_idle > 0:
                        idle_timeout_sec = saved_idle
                        logger.info(f"Restore: using saved idle timeout {idle_timeout_sec}s for session {restore_from}")
                    saved_max = last.get("max_duration_sec")
                    if isinstance(saved_max, int) and saved_max > 0:
                        max_duration_sec = min(saved_max, 28800)
                        logger.info(f"Restore: using saved max duration {max_duration_sec}s for session {restore_from}")
            if _restore_override:
                _from_arch = _saved_arch if _saved_arch in IMAGE_ARCHES else architecture
                _from_mem = _saved_mem or memory_mib
                _o_arch, _o_mem, _o_err = resolve_restore_choice(
                    body, _from_arch, _from_mem, IMAGE_ARCHES, _valid_memories_for)
                if _o_err:
                    return json_error(400, _o_err)
                logger.info(f"Restore: user override arch {_from_arch}->{_o_arch}, "
                            f"memory {_from_mem}->{_o_mem} for session {restore_from}")
                architecture, memory_mib = _o_arch, _o_mem
        except Exception as e:
            logger.warning(f"Restore: could not resolve saved VM attributes for {restore_from} "
                           f"({e}); using client-provided values")

        # A session_id alone does NOT mean there's anything to restore. Reopening a
        # notebook that was opened before but never produced a saved namespace still
        # carries its session_id, so without this guard every such reopen pays a
        # full /restore-state round-trip (~2s on a resumed spare) only for the VM to
        # log "No namespace checkpoint found — fresh session, nothing to restore".
        # Probe S3 for the checkpoint manifest (the exact object the VM's restore
        # keys on) and, ONLY when it's provably absent, drop restore_from so the
        # grant/launch skips restore entirely. Fail-safe: a None result (no bucket /
        # S3 error) leaves restore_from intact, so an availability blip can never
        # silently discard a user's real saved state.
        _head_status, _head_val = await _head_task
        if _head_status == "ok":
            _has_ckpt = _head_val
            if _has_ckpt is False:
                logger.info(f"Restore: no checkpoint manifest for session {restore_from} "
                            f"— skipping restore (fresh session, nothing to restore)")
                restore_from = None
        else:
            logger.warning(f"Restore: checkpoint-existence probe failed for {restore_from} "
                           f"({_head_val}); proceeding with restore")

        if restore_from:
            _restore_memory_check = await _restore_memory_check_for(
                vm_manager, restore_from, memory_mib, size=await _size_task)
        else:
            _size_task.cancel()

    # Resolve the session id, enforcing the invariant "one notebook keeps ONE
    # session_id for its whole lifetime" server-side too:
    #   1. If the client sent an explicit sessionId, honor it.
    #   2. Else, if this launch is for a known notebook (notebookId set), REUSE that
    #      notebook's stored session_id — so a caller that forgets to send sessionId
    #      (or a future/external caller) can't orphan the notebook's session-scoped
    #      state (workbook intel, checkpoints, chat, cost) by minting a fresh id.
    #   3. Else (truly new/unlinked launch), mint a new time-based id.
    session_id = body.get("sessionId")
    if not session_id and notebook_id is not None:
        try:
            # Reuse the row the relink lookup above already read (same request).
            nb = _nb_row if _nb_row_loaded else await astorage.notebook_get(str(notebook_id))
            stored_sid = (nb or {}).get("session_id") or relinked_sid
            if stored_sid:
                session_id = stored_sid
                logger.info(f"Launch: no sessionId in request — reusing notebook "
                            f"{notebook_id}'s stored session {stored_sid} (lifetime-stable id)")
        except Exception as e:
            logger.warning(f"Launch: could not resolve stored session for notebook "
                           f"{notebook_id} ({e}); will mint a new session id")
    if not session_id:
        session_id = f"{notebook_name}-{int(time.time())}"

    # IDEMPOTENT LAUNCH (one session → at most one live VM). If this session already
    # has a registered VM that is actually alive (RUNNING/SUSPENDED per the control
    # plane), DO NOT launch a second one — return the existing VM. This is the
    # server-side enforcement of the invariant: a duplicate reopen, a double-click,
    # or a launch racing a not-yet-completed close can't spawn a second VM that
    # orphans the first and churns the session. Authoritative + cheap (one
    # get_microvm). Best-effort: any lookup error falls through to a normal launch.
    try:
        existing = await vm_manager.get_session_vm_async(session_id)
        if existing and existing.get("vm_id"):
            ev_id = existing["vm_id"]
            ev_state = await vm_manager.get_vm_state(ev_id)
            # SUSPENDING/RESUMING are alive mid-transition (e.g. a reopen right
            # after a close-suspend): reuse them too rather than launch a duplicate.
            if ev_state in ("RUNNING", "SUSPENDED", "SUSPENDING", "RESUMING"):
                ev_info = vm_manager.active_microvms.get(ev_id, {}) or {}
                logger.info(f"Idempotent launch: session {session_id} already has a live VM "
                            f"{ev_id} ({ev_state}) — returning it instead of launching a duplicate")
                # Also self-heals a notebook row left unlinked by an earlier launch.
                await _persist_notebook_link(notebook_id, session_id, ev_id, checkpoint_enabled)
                # DAG attach; idempotent through synced_vm_id.
                dag_service.attach_soon(session_id, notebook_id, expect_restore=False,
                                        vm_manager=vm_manager)
                return {
                    "microvmId": ev_id,
                    "endpoint": existing.get("endpoint", ev_info.get("endpoint", "")),
                    "name": notebook_name,
                    "sessionId": session_id,
                    "status": "running",
                    "reused": True,
                    "memory_mib": ev_info.get("memory_mib") or memory_mib,
                    "architecture": ev_info.get("architecture") or architecture,
                }
    except Exception as e:
        logger.warning(f"Idempotent-launch check failed for session {session_id} "
                       f"({e}); proceeding to launch")

    # A real launch is now committed (we did NOT return an already-live VM above).
    # Mark the session as launching so /proxy/{path} forwards that race registration
    # WAIT for the mapping to land instead of hard-404ing (which litters the browser
    # console with 404s from the editor's /helpers + /completions-catalog loaders and
    # the panels' /variables + /files fetches). Cleared by register_session on
    # success, or in the rollback/except paths on failure.
    vm_manager.mark_session_launching(session_id)

    # Secrets & env vars (passed to /run hook for injection into os.environ).
    # build_user_env (shared with eternal-mode recovery and rotation in
    # microvm_manager) merges the notebook's PERSISTED env/secrets under the
    # request's, so every launch path re-injects them even when the client doesn't
    # resend (e.g. auto-restore, which historically dropped secrets). The request's
    # explicit values WIN (the user may be overriding this run); stored secrets are
    # deduped by their target envVar. It also returns user_env_keys ({name:
    # "secret"|"env"} for the USER keys only, so the in-VM list_env()/list_secret()
    # skip platform config) and merges vm_manager.launch_env_vars() (config.sh
    # runtime values: ATHENA_*, AURORA_*, ...) UNDER the user env. Best-effort: a
    # storage hiccup falls back to the request-provided values.
    env_vars, secrets, user_env_keys = await build_user_env(
        notebook_id, body.get("envVars", {}), body.get("secrets", []),
        platform_env=vm_manager.launch_env_vars(), load_env=astorage.notebook_env_get)

    # Image name is arch-suffixed: {IMAGE_ARN}-{arch}-{mem} (IMAGE_ARN is the
    # arch-agnostic base, e.g. ...:notebook-microvm).
    image_arn = f"{IMAGE_ARN}-{architecture}-{memory_mib}" if IMAGE_ARN else ""
    if not image_arn:
        return json_error(500, "MICROVM_IMAGE_ARN not configured")

    logger.info(f"Launching MicroVM for: {notebook_name} (arch: {architecture}, memory: {memory_mib} MiB, image: {image_arn}, persistence: {persistence_mode})")

    # Discover cloud data sources + their SCHEMAS on the proxy (S3/DynamoDB/Athena/
    # Aurora), fully-formed, and push them to the VM. The proxy is authoritative for
    # cloud schemas — the VM no longer discovers them (it only self-discovers local
    # /tmp files). Single shared builder (vm_manager.build_cloud_catalog_entries_async) so
    # this launch push and the panel-refresh resync can't drift. Best-effort: a
    # discovery hiccup yields [] and the VM simply has no cloud sources until the
    # user refreshes the Data Sources panel.
    # AWAIT the async builder — launch_microvm runs on the event loop, so a blocking
    # wait would freeze the loop (and any concurrent request like a streaming AI
    # chat) for the whole cloud fan-out during a launch.
    # Cache state BEFORE the call, for the timing line: hit (fresh) / stale (served
    # stale + background refresh kicked) / miss (never populated → blocks, joining
    # any in-flight refresh). Only hit/stale are expected after startup.
    from proxy.platform.datasources import registry as _catalog_registry
    _cat_state = _catalog_registry.catalog_cache_state()
    _t0 = time.perf_counter()
    catalog_entries = await vm_manager.build_cloud_catalog_entries_async(session_id=session_id)
    _timed_log(f"catalog [cache={_cat_state}, {len(catalog_entries or [])} entries]",
               (time.perf_counter() - _t0) * 1000.0)
    # Offload the artifacts-bucket resolution: the first call does a
    # one-time synchronous STS get_caller_identity; run it off the event loop so a
    # concurrent request isn't frozen. Cached after the first call, so reuse the
    # resolved name below instead of calling get_artifacts_bucket() again inline.
    _t0 = time.perf_counter()
    bucket_name = await asyncio.to_thread(vm_manager.get_artifacts_bucket)
    _timed_log("artifacts_bucket_sts", (time.perf_counter() - _t0) * 1000.0)
    # The runHookPayload is hard-capped at 4096 bytes by the RunMicrovm API, so we
    # CANNOT inline the fully-formed catalog entries (columns + samples + metadata
    # for every cloud source overflows that). Stage them in S3 and pass only the
    # small object key; the VM downloads + loads them in its /run hook. Best-effort:
    # a staging failure yields None and the VM simply has no cloud sources until the
    # user refreshes the Data Sources panel (resync push over HTTP, no 4KB limit).
    # Offload the synchronous S3 put so staging the launch catalog (which
    # can be a sizeable payload) doesn't block the event loop during a launch.
    # Non-empty catalog: stage CONCURRENTLY with the pool grant. A pool grant
    # inlines catalog_entries (no 4 KB cap on /grant), so only the cold path needs
    # the key, and it awaits the PUT before run_vm. Empty catalog: no S3 call
    # (returns None immediately), awaited as before.
    _stage_task = None
    if catalog_entries:
        _stage_task = _spawn_background(
            _stage_catalog_timed(vm_manager, session_id, catalog_entries))
        catalog_s3_key = None
    else:
        _t0 = time.perf_counter()
        catalog_s3_key = await asyncio.to_thread(
            vm_manager.stage_catalog_to_s3, session_id, catalog_entries
        )
        _timed_log("stage_catalog_s3", (time.perf_counter() - _t0) * 1000.0)

    _cold_vm_id = None
    try:
        # The execution role is what the VM assumes at RUNTIME for all AWS access
        # (S3, Glue, Athena, DynamoDB, checkpoints). If it's missing, the platform
        # launches the VM with NO credentials at all (proven: RunMicrovm returns
        # executionRoleArn=None and in-VM boto3 raises NoCredentialsError) — NOT a
        # fallback to the build role. That produces a VM where every data call and
        # every checkpoint silently fails. So require it and fail LOUDLY here rather
        # than hand the user a broken sandbox.
        if not EXEC_ROLE_ARN:
            raise RuntimeError(
                "MICROVM_EXEC_ROLE_ARN is not set — refusing to launch a VM with no "
                "execution role (it would have no AWS credentials). Ensure config.sh "
                "exports EXEC_ROLE_ARN and it is passed to the proxy."
            )

        # --- Try the VM pool FIRST (gated by POOL_ENABLED; grant-then-fallback,
        # design §10/§13.4 Option A). On a miss/error/timeout, _try_pool_grant
        # returns None and we fall straight through to the existing cold-launch
        # path below, completely unchanged. ---
        if _pool_enabled():
            grant_payload = {
                "arch": architecture, "size": memory_mib, "session_id": session_id,
                "notebook_id": notebook_id, "notebook_name": notebook_name,
                "persistence_mode": persistence_mode, "checkpoint_enabled": checkpoint_enabled,
                "artifacts_bucket": bucket_name, "env_vars": env_vars, "secrets": secrets,
                # None when catalog_entries are inlined (the VM reads the key only
                # when catalog_entries is None); the awaited key (None) when empty.
                "user_env_keys": user_env_keys, "catalog_s3_key": catalog_s3_key,
            }
            if catalog_entries:
                grant_payload["catalog_entries"] = catalog_entries
            if restore_from:
                grant_payload["restore_from"] = restore_from
            # Measured around the whole /grant call: the pool manager does claim +
            # rebind (+ /restore-state when restore_from is set) inside it and does
            # not report sub-phase timings in its response today.
            _t0 = time.perf_counter()
            granted = await _try_pool_grant(grant_payload)
            _timed_log(f"pool_grant [{'granted' if granted is not None else 'miss/fallback'}"
                       f"{', restore' if restore_from else ''}]",
                       (time.perf_counter() - _t0) * 1000.0)
            _t_adopt = time.perf_counter()
            if granted is not None:
                g_microvm_id, g_endpoint = granted["vm_id"], granted["endpoint"]
                # launched_at is the pool's MINT time, not now (the AWS 8h clock
                # started at mint): ISO for the DB row, epoch for the in-memory map
                # and the cost seed. See adopt_pool_grant_times.
                g_launched_at, g_launched_epoch = adopt_pool_grant_times(granted)
                try:
                    vm_manager.active_microvms[g_microvm_id] = {
                        "endpoint": g_endpoint, "name": notebook_name, "launched_at": g_launched_epoch,
                        "memory_mib": memory_mib, "architecture": architecture,
                        "idle_timeout_sec": idle_timeout_sec, "max_duration_sec": max_duration_sec,
                        "session_id": session_id, "_502_strikes": 0,
                        # Launch hints for the VM card's Origin row (in-memory only).
                        "origin": "pool", "restored_from": restore_from,
                    }
                    # The spare sat SUSPENDED (snapshot storage) in the pool from
                    # its MINT time (g_launched_epoch) until this grant — real AWS
                    # cost. Seed that pool-residency SUSPENDED interval into the
                    # in-memory record (SUSPENDED at mint, RUNNING now) INSTEAD OF a
                    # plain RUNNING record, so get_cost's in-memory fallback reports
                    # the suspended time too. A missing/bad mint epoch degrades to a
                    # plain RUNNING record inside record_prior_suspended.
                    vm_manager.cost_tracker.record_prior_suspended(
                        g_microvm_id, g_launched_epoch, memory_mib=memory_mib,
                        architecture=architecture)
                    # Pass the ISO mint time straight through — the DB launched_at
                    # column is TEXT/TIMESTAMPTZ ISO, NOT epoch. When g_launched_at
                    # is None (older pool), vm_session_create defaults to now.
                    # prior_suspended_since seeds the mint->grant SUSPENDED interval
                    # in vm_state_log so v_vm_sessions reconstructs the pool-residency
                    # snapshot-storage cost (two rows); only on the pool-grant path.
                    await astorage.vm_session_create(
                        microvm_id=g_microvm_id, notebook_id=notebook_id, notebook_name=notebook_name,
                        session_id=session_id, memory_mib=memory_mib, endpoint=g_endpoint,
                        idle_timeout_sec=idle_timeout_sec, max_duration_sec=max_duration_sec,
                        checkpoint_enabled=checkpoint_enabled, architecture=architecture,
                        launched_at=g_launched_at, prior_suspended_since=g_launched_at,
                    )
                    if session_id:
                        await vm_manager.register_session_async(session_id, g_microvm_id, g_endpoint)
                    logger.info(f"Pool grant adopted: {g_microvm_id} at {g_endpoint} for session {session_id}")
                    # Best-effort (never raises) → can't trigger the grant rollback below.
                    await _persist_notebook_link(notebook_id, session_id, g_microvm_id, checkpoint_enabled)
                    dag_service.attach_soon(session_id, notebook_id, expect_restore=bool(restore_from),
                                            vm_manager=vm_manager)
                    _timed_log("pool_grant_adopt (db + session register)",
                               (time.perf_counter() - _t_adopt) * 1000.0)
                    _resp = {
                        "microvmId": g_microvm_id, "endpoint": g_endpoint, "name": notebook_name,
                        "sessionId": session_id, "status": "running",
                        "memory_mib": memory_mib, "architecture": architecture,
                    }
                    if restore_from:
                        _resp["restore_report"] = await _launch_restore_report(
                            vm_manager, g_microvm_id, g_endpoint)
                        if _restore_memory_check is not None:
                            _resp["restore_memory_check"] = _restore_memory_check
                    return _resp
                except Exception as commit_err:
                    logger.error(f"Pool grant adopted {g_microvm_id} but registration failed; "
                                 f"rolling back + falling through to cold launch: {commit_err}")
                    vm_manager.active_microvms.pop(g_microvm_id, None)
                    if session_id:
                        try:
                            current = await vm_manager.get_session_vm_async(session_id)
                            if current and current.get("vm_id") == g_microvm_id:
                                vm_manager.unregister_session(session_id)
                        except Exception:
                            pass
                    # Do NOT terminate the granted VM here — it is a real pooled VM
                    # the pool manager owns; best-effort leave it for the pool's own
                    # reconciliation to sort out, and fall through to cold launch
                    # for THIS request only.

        # Cold path: the runHookPayload carries only the S3 key (4 KB cap), so the
        # staging PUT must have finished before run_vm.
        if _stage_task is not None:
            catalog_s3_key = await _stage_task
        params = {
            "imageIdentifier": image_arn,
            "ingressNetworkConnectors": [INGRESS_CONNECTOR, SHELL_INGRESS_CONNECTOR],
            "egressNetworkConnectors": [EGRESS_CONNECTOR],
            "idlePolicy": {
                "autoResumeEnabled": True,
                "maxIdleDurationSeconds": idle_timeout_sec,
                "suspendedDurationSeconds": max_duration_sec,
            },
            "maximumDurationInSeconds": max_duration_sec,
            "runHookPayload": json.dumps({
                "notebook_name": notebook_name,
                # The notebook's integer PK (matches vm_sessions.notebook_id and the
                # v_nb_* analytics views). None for an ephemeral/unlinked launch.
                "notebook_id": notebook_id,
                "session_id": session_id,
                "checkpoint_enabled": checkpoint_enabled,
                "persistence_mode": persistence_mode,
                "restore_from": restore_from,
                # VM identity for the /run log banner (tiny; well under the 4096B cap).
                # The VM has no other way to know its own tier/arch/image.
                "memory_mib": memory_mib,
                "architecture": architecture,
                "image": image_arn,
                "artifacts_bucket": bucket_name,
                "secrets": secrets,
                "env_vars": env_vars,
                # {name: "secret"|"env"} for ONLY the user-configured keys (captured
                # before the platform merge above). The hook publishes this verbatim
                # as MICROVM_USER_ENV_KEYS so list_env()/list_secret() show just the
                # user's keys — NOT the platform config also present in env_vars.
                "user_env_keys": user_env_keys,
                # Pointer to the S3-staged, proxy-discovered cloud catalog (schemas).
                # NOT inlined: runHookPayload is capped at 4096 bytes, so we pass a
                # small key and the VM downloads the fully-formed entries in /run.
                # artifact_bucket lets the VM scope its S3 read + local discovery /
                # helpers. Local /tmp files are discovered on the VM.
                "catalog_s3_key": catalog_s3_key,
                "artifact_bucket": bucket_name,
            }),
        }
        params["executionRoleArn"] = EXEC_ROLE_ARN

        response = await vm_manager.run_vm(**params)
        microvm_id = response["microvmId"]
        _cold_vm_id = microvm_id
        endpoint = response["endpoint"]
        # Arch/tier of a cold launch are known NOW, while the VM is PENDING (up to
        # ~60 s below). Keep them where GET /instances finds them until the
        # RUNNING commit writes the real records (or the launch fails), so the UI
        # never shows a default arch/size for it. No session_id: a PENDING VM is
        # not routable and must not be adopted by a tab.
        _PENDING_LAUNCH[microvm_id] = {
            "name": notebook_name, "memory_mib": memory_mib,
            "architecture": architecture, "launched_at": time.time(),
        }

        # WAIT FOR RUNNING BEFORE committing any state. Previously we registered the
        # in-memory map + DB row + session mapping and returned status="running"
        # immediately, THEN polled — so a VM that never reached RUNNING (or a later
        # exception) left an orphaned live VM registered as healthy. Now we poll
        # first, and on failure terminate + roll back so we never leak a VM or lie
        # about its state.
        reached_running = False
        state = "PENDING"
        # Up to COLD_LAUNCH_WAIT_BUDGET_SEC of elapsed time, short-first schedule
        # (cold_launch_poll_delay). Cold path only: the pool grant returned above.
        _deadline = _cold_launch_clock() + COLD_LAUNCH_WAIT_BUDGET_SEC
        _delay = 0.0
        for _attempt in range(1, COLD_LAUNCH_MAX_POLLS + 1):
            # LAUNCH client: the cold-launch wait-for-RUNNING loop is the
            # latency-critical part of a launch, so it must NOT queue behind the
            # poll client's adaptive backoff when the /instances dashboard is hot.
            _t0 = time.perf_counter()
            _throttled = False
            try:
                state_resp = await vm_manager.get_microvm_detail_launch(microvm_id)
            except Exception as poll_err:
                # Throttling / connectivity blip: back off and poll again. Any
                # other error propagates exactly as before.
                if not _is_cold_launch_poll_retryable(poll_err):
                    raise
                _throttled = True
                logger.warning(f"wait_running poll {_attempt} for {microvm_id} failed "
                               f"({type(poll_err).__name__}); backing off")
            _timed_log("wait_running poll [launch client]", (time.perf_counter() - _t0) * 1000.0)
            if not _throttled:
                state = state_resp.get("state", "PENDING")
                if state == "RUNNING":
                    reached_running = True
                    break
                # Dead states are unrecoverable — stop waiting immediately.
                if state in ("TERMINATED", "TERMINATING", "FAILED"):
                    break
            _delay = cold_launch_poll_delay(_attempt, _delay, _throttled)
            _remaining = _deadline - _cold_launch_clock()
            if _remaining <= 0:
                break
            await asyncio.sleep(min(_delay, _remaining))
        _PENDING_LAUNCH.pop(microvm_id, None)

        if not reached_running:
            # The VM never came up healthy. Terminate it so we don't leak a live
            # (billing) VM, and report failure — no in-memory/DB/session state was
            # committed above, so there's nothing else to roll back.
            logger.error(f"MicroVM {microvm_id} did not reach RUNNING (last state={state}); terminating to avoid a leak")
            try:
                await vm_manager.terminate_vm(microvm_id)
                await vm_manager.token_cache.delete(microvm_id)
            except Exception as te:
                logger.error(f"Failed to terminate un-healthy VM {microvm_id}: {te}")
            return json_error(502, f"MicroVM did not reach RUNNING (state={state})")

        # VM is RUNNING — NOW commit state. If any step below raises, the except
        # handler terminates the VM and rolls back, so we can't leave a healthy VM
        # registered against a half-written session.
        try:
            vm_manager.active_microvms[microvm_id] = {
                "endpoint": endpoint,
                "name": notebook_name,
                "launched_at": time.time(),
                "memory_mib": memory_mib,
                "architecture": architecture,
                "idle_timeout_sec": idle_timeout_sec,
                "max_duration_sec": max_duration_sec,
                "session_id": session_id,
                "_502_strikes": 0,
                # Launch hints for the VM card's Origin row (in-memory only).
                "origin": "fresh", "restored_from": restore_from,
            }
            vm_manager.cost_tracker.record(microvm_id, "RUNNING", memory_mib=memory_mib,
                                           architecture=architecture)

            await astorage.vm_session_create(
                microvm_id=microvm_id,
                notebook_id=notebook_id,
                notebook_name=notebook_name,
                session_id=session_id,
                memory_mib=memory_mib,
                endpoint=endpoint,
                idle_timeout_sec=idle_timeout_sec,
                max_duration_sec=max_duration_sec,
                checkpoint_enabled=checkpoint_enabled,
                architecture=architecture,
            )

            # Register session → VM mapping (the single source of truth for routing)
            if session_id:
                await vm_manager.register_session_async(session_id, microvm_id, endpoint)
        except Exception as commit_err:
            # Roll back everything we may have committed, then terminate the VM so a
            # registration failure can't strand a live VM.
            logger.error(f"Failed to register launched VM {microvm_id}; rolling back + terminating: {commit_err}")
            vm_manager.active_microvms.pop(microvm_id, None)
            if session_id:
                # Launch failed before/at registration — drop the launching marker so
                # the forwarder stops waiting and 404s fast (the session is not coming).
                vm_manager.clear_session_launching(session_id)
                try:
                    current = await vm_manager.get_session_vm_async(session_id)
                    if current and current.get("vm_id") == microvm_id:
                        vm_manager.unregister_session(session_id)
                except Exception:
                    pass
            try:
                await astorage.vm_session_mark_terminated(microvm_id)
            except Exception:
                pass
            try:
                await vm_manager.terminate_vm(microvm_id)
                await vm_manager.token_cache.delete(microvm_id)
            except Exception as te:
                logger.error(f"Failed to terminate VM {microvm_id} during rollback: {te}")
            return json_error(502, f"Launch registration failed: {commit_err}")

        logger.info(f"MicroVM launched: {microvm_id} at {endpoint}")

        # Mode-dependent lifecycle registration
        if persistence_mode == "eternal" and session_id:
            # Eternal mode is REACTIVE (scheduler-free): state is always safe in S3
            # (save-on-suspend + save-on-terminate), so when the VM dies the next
            # proxied request detects it and triggers reactive recovery — launch a
            # fresh VM, restore from S3, relink the session — paying a one-time
            # cold-start on that request. This survives a stateless / horizontally
            # scaled proxy (no in-process timer tied to one host).
            logger.info(f"Eternal mode (reactive): session {session_id} will auto-recover on next request if VM dies")
        elif persistence_mode == "checkpoint" and session_id:
            # Checkpoint mode: rely on AWS /terminate hook to save state.
            # No pre-checkpoint timer — ensures the LATEST state is always saved.
            logger.info(f"Checkpoint mode: session {session_id} will save on terminate hook")
        elif session_id:
            # None (ephemeral) mode: checkpoint disabled — the VM's state is never
            # persisted to S3 and there is no restore. When the VM suspends or
            # terminates, its session state is simply lost.
            logger.info(f"None mode (ephemeral): session {session_id} will NOT be checkpointed")

        await _persist_notebook_link(notebook_id, session_id, microvm_id, checkpoint_enabled)
        dag_service.attach_soon(session_id, notebook_id, expect_restore=bool(restore_from),
                                vm_manager=vm_manager)

        # The VM is already confirmed RUNNING above (we polled before committing
        # state), so it's now safe to report success.
        _resp = {
            "microvmId": microvm_id,
            "endpoint": endpoint,
            "name": notebook_name,
            "sessionId": session_id,
            "status": "running",
            # Echo the ACTUAL tier/arch the VM launched with. On a restore the server
            # may override these with the saved session's values, so the client can't
            # assume what it sent — it reads these back to keep the tab in sync.
            "memory_mib": memory_mib,
            "architecture": architecture,
        }
        if restore_from:
            _resp["restore_report"] = await _launch_restore_report(vm_manager, microvm_id, endpoint)
            if _restore_memory_check is not None:
                _resp["restore_memory_check"] = _restore_memory_check
        return _resp

    except Exception as e:
        logger.error(f"Failed to launch MicroVM: {e}")
        if _cold_vm_id:
            _PENDING_LAUNCH.pop(_cold_vm_id, None)
        # Launch aborted before registration — clear the launching marker so the
        # forwarder doesn't keep waiting on a session that will never register.
        if session_id:
            vm_manager.clear_session_launching(session_id)
        return json_error(502, f"Launch failed: {e}")


async def _resolve_session_vm(vm_manager, session_id: str) -> dict | None:
    """Resolve session → {vm_id, endpoint} using the registry, then the in-memory
    active_microvms map, then the DB — the same fallback chain terminate uses, so
    a proxy restart (which empties the registry) doesn't lose the mapping."""
    session_vm = await vm_manager.get_session_vm_async(session_id)
    if session_vm:
        return session_vm
    # Snapshot: launch_for_rotation/terminate mutate this map from worker threads.
    for vm_id, info in list(vm_manager.active_microvms.items()):
        if info.get("session_id") == session_id:
            return {"vm_id": vm_id, "endpoint": info.get("endpoint")}
    try:
        for s in await astorage.vm_session_list_active():
            if s.get("session_id") == session_id:
                return {"vm_id": s["microvm_id"], "endpoint": s.get("endpoint", "")}
    except Exception:
        pass
    return None


CHECKPOINT_RESUME_WAIT_BUDGET_SEC = 24.0  # wait for RUNNING before a save (was 12 x 2 s)
CHECKPOINT_HEALTH_PROBE_BUDGET_SEC = 2.0  # /health probe after waking, before the save
CHECKPOINT_HEALTH_PROBE_STEP_SEC = 0.25


async def _await_vm_serving(vm_manager, microvm_id: str, endpoint: str) -> bool:
    """GET the VM's /health (same auth header as the save) until 200, retrying
    every 0.25 s for up to ~2 s. Returns True on a 200, False when the budget
    runs out; never raises (the save goes ahead either way)."""
    start = resume_wait._clock()
    while True:
        remaining = CHECKPOINT_HEALTH_PROBE_BUDGET_SEC - (resume_wait._clock() - start)
        if remaining <= 1e-9:
            logger.warning(f"   checkpoint: {microvm_id} /health not 200 within "
                           f"{CHECKPOINT_HEALTH_PROBE_BUDGET_SEC:.0f}s of RUNNING; saving anyway")
            return False
        try:
            token = await vm_manager.get_auth_token(microvm_id)
            resp = await http_clients.vm_client().get(
                f"https://{endpoint}/health", headers={"X-aws-proxy-auth": token},
                timeout=max(0.25, min(1.0, remaining)))
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        remaining = CHECKPOINT_HEALTH_PROBE_BUDGET_SEC - (resume_wait._clock() - start)
        if remaining <= 1e-9:
            continue
        await resume_wait._sleep(min(CHECKPOINT_HEALTH_PROBE_STEP_SEC, remaining))


async def _checkpoint_session_vm(vm_manager, microvm_id: str, endpoint: str, session_id: str) -> dict:
    """Force a state checkpoint of the VM to S3 for `session_id`, resuming the VM
    first if it's SUSPENDED (a suspended VM can't run the save, and terminating it
    while suspended would SKIP the /terminate save hook — the exact gap we're
    closing). Reuses the same auth-token + httpx forward the /proxy path uses,
    hitting the VM's in-VM POST /checkpoint-save (which calls CheckpointManager.save).

    Returns {"success": bool, ...} and never raises. On a failure "error" says
    why (vmLost=True when the VM stopped during the save); /terminate uses
    _save_failure_reason to decide whether to keep the VM running. If the VM is
    already gone, there's nothing (newer) to save — its last suspend/terminate
    checkpoint stands.

    EPHEMERAL ('none') mode writes NOTHING to S3 — that's the mode's whole contract
    (no save, no restore). We short-circuit here BEFORE resuming/forwarding so we
    don't needlessly wake a suspended VM and pay for a checkpoint that could never
    be restored anyway.
    """
    if get_persistence_mode() == "none":
        return {"success": False, "reason": "ephemeral (none) mode — state is not checkpointed"}
    if not endpoint:
        return {"success": False, "error": "no endpoint for session"}

    # If suspended, resume so the kernel can run the save. Poll to RUNNING (bounded).
    try:
        state = await vm_manager.get_vm_state(microvm_id)
    except Exception as e:
        state = "UNKNOWN"
        logger.warning(f"   checkpoint: could not read state for {microvm_id}: {e}")

    if state in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        return {"success": False, "error": f"vm not alive (state={state})", "state": state}

    if state in ("SUSPENDED", "SUSPENDING", "RESUMING"):
        try:
            # Shared wake + schedule (resume_wait.py), same ~24 s budget:
            # resume a SUSPENDED VM, and once a SUSPENDING VM reads SUSPENDED
            # (or our resume raised) resume it from inside the wait.
            st = await resume_wait.wake_and_wait(
                state, lambda: vm_manager.get_vm_state(microvm_id),
                lambda: vm_manager.resume_vm(microvm_id), CHECKPOINT_RESUME_WAIT_BUDGET_SEC,
                vm_id=microvm_id, who="   checkpoint", log=logger)
            if st == "RUNNING":
                state = "RUNNING"
                # Guard: the save POST below has no retry and a failure makes
                # /terminate keep the VM (409), so after waking the VM confirm
                # it serves before sending it. Bounded; never fails the save.
                await _await_vm_serving(vm_manager, microvm_id, endpoint)
        except Exception as e:
            logger.warning(f"   checkpoint: resume of {microvm_id} failed: {e}")

    # Save-before-checkpoint (design §4.7): push the CURRENT DB DAG document so
    # code-only edits since the last execution reach dag.json. The VM is awake
    # here, so this wakes nothing. Bounded (5 s) and never fails the checkpoint.
    dag_service.use_vm_manager(vm_manager)
    if not await dag_service.flush(session_id, vm_id=microvm_id, endpoint=endpoint, timeout=5.0):
        logger.warning(f"   checkpoint: DAG flush for session={session_id} did not complete; "
                       f"checkpoint proceeds with the VM's last pushed document")

    # Forward the save to the VM using the same auth mechanism as /proxy.
    try:
        token = await vm_manager.get_auth_token(microvm_id)
        url = f"https://{endpoint}/checkpoint-save"
        headers = {"X-aws-proxy-auth": token, "Content-Type": "application/json"}
        body = json.dumps({"session_id": session_id})
        resp = await http_clients.vm_client().post(url, headers=headers, content=body, timeout=60.0)
        if resp.status_code == 200:
            logger.info(f"   ✅ Checkpoint-on-demand saved for session={session_id} (vm={microvm_id})")
            try:  # the VM card's cached checkpoint read is now stale
                from proxy.platform.vm_detail import invalidate_session_cache
                invalidate_session_cache(session_id)
            except Exception:
                pass
            try:
                out = {"success": True, **resp.json()}
            except Exception:
                return {"success": True}
            if out.get("success") is False:
                logger.warning(f"   checkpoint: save on {microvm_id} failed: {out.get('error')}")
            return out
        # Non-200: the Lambda MicroVM endpoint answers 502 with an empty body and
        # x-aws-proxy-error: APP_CONNECT_FAILED once the server behind it is gone,
        # which is what an out-of-memory kill during the save looks like. The VM
        # then reports TERMINATED ("Container Stopped with Exit Code: 137").
        proxy_err = resp.headers.get("x-aws-proxy-error") if getattr(resp, "headers", None) else None
        logger.warning(f"   checkpoint: VM returned {resp.status_code} for {microvm_id}"
                       f"{f' (x-aws-proxy-error: {proxy_err})' if proxy_err else ''}")
        return await _save_failure(vm_manager, microvm_id, f"HTTP {resp.status_code}"
                                   + (f", {proxy_err}" if proxy_err else ""), status=resp.status_code)
    except Exception as e:
        logger.warning(f"   checkpoint: forward to {microvm_id} failed: {e}")
        return await _save_failure(vm_manager, microvm_id, str(e) or type(e).__name__)


# After a failed save request, how long to watch the VM state (the out-of-memory
# kill reached TERMINATED about 6 s after the 502 in the investigation).
SAVE_FAILURE_STATE_POLLS = 5
SAVE_FAILURE_STATE_INTERVAL_SEC = 2.0
_DEAD_STATES = ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND")


async def _save_failure(vm_manager, microvm_id: str, what: str, *, status: int | None = None) -> dict:
    """Result for a save request that got no answer from the notebook server.
    Watches the VM state briefly: when the VM stops, the server was killed while
    saving (vmLost), most likely out of memory; otherwise the VM is still up and
    the result names the HTTP status or transport error."""
    state = None
    for i in range(SAVE_FAILURE_STATE_POLLS):
        try:
            state = await vm_manager.get_vm_state(microvm_id)
        except Exception:
            state = None
        if state in _DEAD_STATES:
            break
        if i < SAVE_FAILURE_STATE_POLLS - 1:
            await asyncio.sleep(SAVE_FAILURE_STATE_INTERVAL_SEC)
    out = {"success": False}
    if status is not None:
        out["status"] = status
    if state in _DEAD_STATES:
        out.update(vmLost=True, state=state,
                   error=("the notebook server stopped while saving, most likely because it ran out of "
                          f"memory ({what} from the MicroVM endpoint, VM state {state}). Nothing was saved "
                          "by this attempt; the previous checkpoint, if any, is unchanged"))
    else:
        out["error"] = f"the notebook server did not answer the save request ({what})"
    logger.warning(f"   checkpoint: {microvm_id}: {out['error']}")
    return out


def _save_failure_reason(ckpt: dict) -> str | None:
    """Why a pre-terminate save must keep the VM running, or None to go ahead.

    None for: a save that succeeded with nothing left out for lack of memory or
    disk, a skipped save that is not an error (ephemeral mode: "reason" only),
    a VM that was already gone before the save, and a VM lost during the save
    (there is nothing left to keep). Everything else (busy, no endpoint, the
    server's own error, no answer while the VM is still up, a variable left out
    because it would not fit) blocks the terminate."""
    if ckpt.get("success"):
        items = [i for i in (ckpt.get("not_saved") or [])
                 if isinstance(i, dict) and i.get("kind") in ("memory", "disk")]
        if not items:
            return None
        reasons = [str(i.get("reason") or i.get("name"))
                   + (" (its previously saved copy was kept)" if i.get("kept_previous") is True else "")
                   for i in items[:3]]
        more = f", and {len(items) - 3} more" if len(items) > 3 else ""
        return "; ".join(reasons) + more + ". The other variables were saved"
    if not ckpt.get("error"):
        return None
    if ckpt.get("vmLost") or ckpt.get("state") in _DEAD_STATES:
        return None
    return str(ckpt["error"])


@router.post("/checkpoint-save")
async def checkpoint_session(request: Request):
    """Explicitly checkpoint a session's VM state to S3 WITHOUT terminating.

    Called by the frontend on notebook DELETE (before terminating the VM) so the
    LATEST state is guaranteed persisted — independent of whether the VM is
    RUNNING or SUSPENDED, and without relying on the /terminate hook (which
    doesn't fire for a suspended VM). Resumes a suspended VM first, then saves.
    (Close no longer terminates — it SUSPENDS — so it relies on the suspend
    snapshot rather than this explicit save.)
    """
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")
    if not session_id:
        return json_error(400, "X-Session-Id header required")

    session_vm = await _resolve_session_vm(vm_manager, session_id)
    if not session_vm:
        # No live VM for this session — nothing newer to save; its last
        # suspend/terminate checkpoint (if any) stands. Not an error for the caller.
        return {"success": False, "reason": "no active VM for session", "sessionId": session_id}

    result = await _checkpoint_session_vm(vm_manager, session_vm["vm_id"], session_vm.get("endpoint", ""), session_id)
    return {"sessionId": session_id, "microvmId": session_vm["vm_id"], **result}


@router.post("/terminate")
async def terminate_session(request: Request):
    """Terminate a SPECIFIC MicroVM for a session.

    Requires BOTH the X-Session-Id header and a `microvmId` in the JSON body — the
    caller states exactly which VM to kill, so a stale terminate can never take down
    a VM a concurrent reopen just relinked to the session (the source of the
    close↔reopen churn). Checkpoints state to S3 BEFORE terminating (resuming a
    suspended VM first if needed) so terminating never loses work sitting only in
    VM memory. When that save fails the VM is NOT terminated: the response is 409
    {"error": "Save failed: <reason>. The VM was kept running ...", "saveFailed":
    true, "reason", "notSaved"}; body {"terminateWithoutSaving": true} skips the
    save and terminates. If the VM itself died during the save (out of memory)
    the terminate goes ahead and the 200 body carries "checkpoint": {"success":
    false, "error", "vmLost": true}. Used by DELETE and the MicroVMs panel
    (Close now SUSPENDS instead). The session→VM registry is only cleared
    if it STILL points at the VM being terminated (compare-and-swap).
    """
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")

    # TARGET A SPECIFIC VM (REQUIRED). The caller MUST send the microvmId it wants
    # to terminate — we terminate THAT VM, never "whatever is registered for the
    # session now". This closes the close↔reopen race: a slow Close whose /terminate
    # lands AFTER a reopen relaunched the session would otherwise resolve the session
    # to the NEW VM and kill it. With a mandatory id we only ever kill the intended
    # VM, and the compare-and-swap below protects the registry.
    body = {}
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    microvm_id = body.get("microvmId") or body.get("microvm_id")
    if not microvm_id:
        return json_error(400, "microvmId is required")
    # "Terminate without saving": the user chose to end the VM after a failed
    # save (or the notebook is being deleted). Skips the pre-terminate checkpoint.
    without_saving = body.get("terminateWithoutSaving") is True
    save_failure = None  # set when the VM was lost during the pre-terminate save

    # SESSION/VM MISMATCH GUARD. When the caller names a session AND the target VM
    # has a known session of its own, they must match. A mismatched pair (a tab whose
    # stale microvmId points at ANOTHER notebook's VM) would otherwise checkpoint that
    # VM's kernel into the caller's S3 session prefix (overwriting its checkpoint)
    # and kill the other notebook's VM. Refuse BEFORE any checkpoint, DAG flush or
    # terminate. Unattached VMs (no session) and matching pairs proceed as before.
    if session_id:
        vm_session = (vm_manager.active_microvms.get(microvm_id) or {}).get("session_id")
        if not vm_session:
            vm_session = (await astorage.vm_session_get(microvm_id) or {}).get("session_id")
        if vm_session and vm_session != session_id:
            logger.warning(f"   terminate: refused, microvm {microvm_id} belongs to session "
                           f"{vm_session[:8]} but the request named session {session_id[:8]}")
            return JSONResponse(status_code=409,
                                content={"error": "microvmId belongs to a different session"})

    # X-Session-Id is OPTIONAL. Terminate is keyed on the microvmId (above), so an
    # UNATTACHED VM — one with no session (e.g. orphaned after a DB reset, or a VM
    # whose session mapping was lost) — must still be terminable directly from the
    # MicroVMs panel. When no session is provided we skip the checkpoint (there's no
    # session state to save) and the compare-and-swap registry unregister (there's
    # no mapping to clear), and go straight to terminating the VM.

    # Save-before-terminate (ONLY when we have a session — an unattached VM has no
    # session state worth checkpointing). Guarantees the latest state is in S3 even
    # if the VM was SUSPENDED (whose /terminate hook would otherwise not fire).
    # A failed save KEEPS the VM running and returns 409 with the reason
    # (_save_failure_reason), unless the body sets terminateWithoutSaving. Close
    # does not come through here (it suspends), so this cannot hang a Close.
    if session_id:
        # Best-effort endpoint for the pre-terminate checkpoint: in-memory first,
        # then the session registry IF it still points at this VM (a reopen may have
        # relinked the session to a newer VM, in which case we don't want that VM's
        # endpoint).
        info = vm_manager.active_microvms.get(microvm_id) or {}
        endpoint = info.get("endpoint", "")
        if not endpoint:
            reg = await _resolve_session_vm(vm_manager, session_id)
            if reg and reg.get("vm_id") == microvm_id:
                endpoint = reg.get("endpoint", "")
        if without_saving:
            logger.info(f"   terminate: terminateWithoutSaving set for {microvm_id} — skipping the "
                        f"pre-terminate checkpoint (user choice)")
        else:
            try:
                ckpt = await _checkpoint_session_vm(vm_manager, microvm_id, endpoint, session_id)
            except Exception as e:
                ckpt = {"success": False, "error": f"the save could not run ({e})"}
            reason = _save_failure_reason(ckpt)
            if reason:
                # A failed save must not lose the in-memory state: keep the VM
                # running and tell the caller why. The user can free memory and
                # save again, or send terminateWithoutSaving to end the VM anyway.
                logger.warning(f"   terminate: pre-terminate checkpoint failed for {microvm_id} "
                               f"({reason}) — VM kept running")
                return JSONResponse(status_code=409, content={
                    "error": f"Save failed: {reason}. The VM was kept running so you can free memory or save again.",
                    "saveFailed": True,
                    "reason": reason,
                    "notSaved": [i for i in (ckpt.get("not_saved") or []) if isinstance(i, dict)],
                    "microvmId": microvm_id,
                    "sessionId": session_id,
                })
            if not ckpt.get("success"):
                if ckpt.get("vmLost"):
                    save_failure = {"success": False, "error": ckpt.get("error"), "vmLost": True}
                    logger.warning(f"   terminate: pre-terminate checkpoint lost the VM ({ckpt.get('error')}) "
                                   f"— cleaning up")
                else:
                    logger.info(f"   terminate: pre-terminate checkpoint skipped "
                                f"({ckpt.get('error') or ckpt.get('reason')}) — proceeding to terminate")

    # Decide up front (before terminating) whether the session STILL maps to this
    # VM, so that after a successful terminate we clear the RIGHT mapping. We do NOT
    # unregister yet — clearing routing before the AWS terminate succeeds would
    # orphan a still-running notebook if terminate_vm() fails.
    should_unregister = False
    if session_id:
        current = await vm_manager.get_session_vm_async(session_id)
        if current and current.get("vm_id") == microvm_id:
            should_unregister = True
        elif current:
            logger.info(f"   terminate: session {session_id} now maps to {current.get('vm_id')} "
                        f"(not the {microvm_id} being terminated) — will leave registry intact")

    try:
        # Terminate in AWS FIRST. Only once this succeeds do we tear down local
        # routing/state — otherwise a failed terminate would leave a live VM with
        # no session mapping (an orphaned, unreachable-but-billing notebook).
        await vm_manager.terminate_vm(microvm_id)
    except Exception as e:
        logger.error(f"Failed to terminate {microvm_id}: {e} — leaving session routing intact")
        return json_error(502, f"Terminate failed: {e}")

    # AWS terminate succeeded — now it's safe to clear local state. COMPARE-AND-SWAP
    # unregister: only clear the mapping if it STILL points at this VM (re-check, in
    # case a reopen relinked the session between the pre-check and now). Cleanup
    # failures here are non-fatal (the VM is already gone) — log and still report ok.
    cleared_notebook_link = False
    try:
        if should_unregister:
            recheck = await vm_manager.get_session_vm_async(session_id)
            if recheck and recheck.get("vm_id") == microvm_id:
                vm_manager.unregister_session(session_id)
                cleared_notebook_link = True
        vm_manager.active_microvms.pop(microvm_id, None)
        await vm_manager.token_cache.delete(microvm_id)
    except Exception as e:
        logger.warning(f"   terminate: post-terminate cleanup for {microvm_id} raised {e} (VM already gone)")

    # Clear the notebook's live-session link so v_nb_notebook stops reporting this
    # now-terminated notebook as "open". Gated on cleared_notebook_link — i.e. the
    # session still mapped to THIS VM (a reopen race that relinked to a new VM leaves
    # the link intact, since the notebook is open on that new VM). Best-effort: a
    # clear failure must not fail the terminate.
    if session_id and cleared_notebook_link:
        try:
            await astorage.notebook_clear_session(session_id)
        except Exception as e:
            logger.warning(f"   terminate: notebook_clear_session({session_id}) failed: {e}")
        dag_service.invalidate_session(session_id)
    # The VM is gone: drop its DAG push/attach bookkeeping (and the session's,
    # when the session still belonged to it).
    dag_service.forget_vm(microvm_id, session_id if cleared_notebook_link else None)

    logger.info(f"MicroVM terminated: {microvm_id} (session={session_id})")
    out = {"status": "terminated", "microvmId": microvm_id, "sessionId": session_id}
    if save_failure:
        out["checkpoint"] = save_failure
    return out


@router.post("/suspend")
async def suspend_session(request: Request):
    """Suspend the VM serving a session."""
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")
    if not session_id:
        return json_error(400, "X-Session-Id header required")

    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        return json_error(404, "Session not found")

    microvm_id = session_vm["vm_id"]
    try:
        await vm_manager.suspend_vm(microvm_id)
        logger.info(f"MicroVM suspend requested: {microvm_id} (session={session_id})")
        return {"status": "suspended", "microvmId": microvm_id, "sessionId": session_id}
    except Exception as e:
        logger.error(f"Failed to suspend: {e}")
        return json_error(502, f"Suspend failed: {e}")


RESUME_ROUTE_WAIT_BUDGET_SEC = 30.0  # POST /resume wait for RUNNING (was 6 x 5 s)


@router.post("/resume")
async def resume_session(request: Request):
    """Resume the suspended VM serving a session."""
    vm_manager = request.app.state.vm_manager
    session_id = request.headers.get("X-Session-Id")
    if not session_id:
        return json_error(400, "X-Session-Id header required")

    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        return json_error(404, "Session not found")

    microvm_id = session_vm["vm_id"]
    try:
        await vm_manager.resume_vm(microvm_id)
        logger.info(f"MicroVM resume requested: {microvm_id} (session={session_id})")

        # Poll until running (max 30 s) on the shared resume schedule
        # (proxy/platform/resume_wait.py). A poll error still fails the route (502).
        async def _read_state():
            return (await vm_manager.get_microvm_detail(microvm_id)).get("state", "PENDING")
        state = await resume_wait.wait_until_running(_read_state, RESUME_ROUTE_WAIT_BUDGET_SEC)
        if state == "RUNNING":
            return {"status": "running", "microvmId": microvm_id, "sessionId": session_id}

        return {"status": "resuming", "microvmId": microvm_id, "sessionId": session_id}
    except Exception as e:
        logger.error(f"Failed to resume: {e}")
        return json_error(502, f"Resume failed: {e}")



def _iso_to_epoch(value) -> float | None:
    """ISO-8601 string (or datetime) → epoch seconds; None if missing/unparseable.
    Naive values are treated as UTC (pool_vms timestamps are written in UTC)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


@router.get("/instances")
async def list_instances(request: Request):
    """List all MicroVMs in the account (running + suspended), with live state from AWS."""
    vm_manager = request.app.state.vm_manager
    # Catalog KEEP-WARM: piggy-back on the UI's ~15s /instances poll. This only
    # checks the cloud-catalog cache age and, once it is ~80% of its TTL old,
    # schedules a fire-and-forget background refresh on this loop (single-flight,
    # backed off on failure). It is NOT awaited, so this poll's latency and
    # response are unchanged — and a later /launch finds a warm catalog instead
    # of paying a ~7-10s discovery inline. Never raises.
    from proxy.platform.datasources import registry as _catalog_registry
    _catalog_registry.maybe_refresh_catalog_in_background("instances-poll")
    try:
        # Taken BEFORE the list_microvms snapshot: a RUNNING recorded after this
        # (a served forward, _note_vm_served) is newer than the snapshot.
        poll_started = time.time()
        response = await vm_manager.list_vms()
        items = response.get("items", [])

        # Warm-pool SPARES have no vm_sessions/vm_state_log rows, so their cost is
        # purely the in-memory tracker — which otherwise starts the clock at the
        # proxy's FIRST observation (after a restart every spare read ~0s running /
        # a few seconds suspended). Anchor each unseeded 'available' spare to its
        # real pool_vms history: RUNNING from launched_at (mint) until updated_at
        # (became available = suspended), SUSPENDED since. In-memory only — no DB
        # writes. seed_pool_spare is once-per-VM, so steady-state this is one
        # cheap pool_vms read per poll and no tracker changes.
        try:
            _pool_rows = await astorage.pool_vm_list(state="available")
            for _r in _pool_rows or []:
                _vid = _r.get("vm_id")
                if not _vid:
                    continue
                vm_manager.cost_tracker.seed_pool_spare(
                    _vid,
                    _iso_to_epoch(_r.get("launched_at")),
                    _iso_to_epoch(_r.get("updated_at")),
                    memory_mib=_r.get("size_mib"),
                    architecture=_r.get("architecture"),
                )
        except Exception as _e:  # cost display only — never fail the poll
            logger.debug(f"/instances: pool-spare cost seeding skipped: {_e}")

        # Cost for every VM this poll reports is read in ONE batched, off-loop
        # vm_cost_quantities_many query AFTER the loop below has written this
        # poll's record() / vm_session_update_state transitions. (It used to be
        # read before them, so the poll that first saw SUSPENDED→RUNNING returned
        # the previous resume_count/suspend_count: a one-poll lag.)
        costs: dict = {}

        def _cost_for(mid: str) -> dict:
            # A VM missing from the batch read gets the pure in-memory pricing
            # (no DB hop on the loop).
            c = costs.get(mid)
            return c if c is not None else vm_manager.cost_tracker._price(mid, None)

        persisted_ids: list[str] = []
        instances = {}
        for item in items:
            microvm_id = item.get("microvmId", "")
            state = item.get("state", "UNKNOWN")
            # The control plane EXPLICITLY reports this VM dead. That's a
            # positive assertion (not mere absence), so act immediately: mark the
            # DB row TERMINATED + CAS-unregister the session so the notebook can
            # relaunch, then drop it from the returned map (a dead VM is never a
            # listed instance). Previously TERMINATED was silently skipped (DB left
            # stale) and FAILED slipped through as if it were a normal instance.
            if state in ("TERMINATED", "TERMINATING", "FAILED"):
                _vm_missing_polls.pop(microvm_id, None)  # it's present-but-dead, not missing
                # _mark_vm_dead only acts + logs on a REAL transition, so an
                # already-TERMINATED VM the AWS list keeps returning is a silent
                # no-op (no per-poll log spam).
                await _mark_vm_dead(vm_manager, microvm_id, state, reason="/instances control-plane state")
                continue

            local_info = (vm_manager.active_microvms.get(microvm_id)
                          or _PENDING_LAUNCH.get(microvm_id) or {})
            endpoint = local_info.get("endpoint", "")

            # Always check SQLite DB for authoritative metadata (launch time, lifecycle config)
            # The local cache may have stale values after proxy restarts
            db_session = await astorage.vm_session_get(microvm_id)

            # Only fetch endpoint from AWS if we don't have it
            detail = None
            if not endpoint and state in ("RUNNING", "SUSPENDED"):
                if db_session and db_session.get("endpoint"):
                    endpoint = db_session["endpoint"]
                else:
                    try:
                        detail = await vm_manager.get_microvm_detail(microvm_id)
                        endpoint = detail.get("endpoint", "")
                    except Exception:
                        pass

            # Memory tier + architecture. The image the VM RUNS
            # ({base}-{arch}-{mem}, in the list item, else the detail call) is the
            # ground truth, so it wins whenever it parses: a VM with no record yet
            # (a cold launch still PENDING, a VM from another proxy) used to be
            # reported as the default arm64/2048 although its image said e.g.
            # x86_64-1024. Then DB (authoritative over the in-memory map, which can
            # be stale after a proxy restart) → in-memory → default.
            img_arch, img_mem = arch_size_from_image(
                item.get("imageArn") or (detail or {}).get("imageArn"))
            memory_mib = (img_mem or (db_session or {}).get("memory_mib")
                          or local_info.get("memory_mib") or DEFAULT_MEMORY_MIB)
            architecture = (img_arch or (db_session or {}).get("architecture")
                            or local_info.get("architecture") or DEFAULT_IMAGE_ARCH)

            instances[microvm_id] = {
                "endpoint": endpoint,
                # Display name: the live in-memory name only. Do NOT fall back to
                # notebook_id — that is the integer PK now, not a name (using it as
                # a name is the bug this refactor removes). Empty when unknown; the
                # UI shows the attached tab's name (and notebook_id) instead.
                "name": local_info.get("name", ""),
                # The notebook's integer PK (from vm_sessions.notebook_id), so the UI
                # can show "Name (id)". None when the VM isn't linked to a notebook.
                "notebook_id": (db_session or {}).get("notebook_id"),
                "state": state,
                "launched_at": (db_session or {}).get("launched_at") or local_info.get("launched_at"),
                "memory_mib": memory_mib,
                "architecture": architecture,
                "session_id": local_info.get("session_id") or (db_session or {}).get("session_id"),
                "idle_timeout_sec": local_info.get("idle_timeout_sec") or (db_session or {}).get("idle_timeout_sec"),
                "last_active": local_info.get("last_active"),
                "max_duration_sec": local_info.get("max_duration_sec") or (db_session or {}).get("max_duration_sec"),
                # "cost" / "session_cost" are filled after the loop (batched read).
                "unhealthy": local_info.get("_502_strikes", 0) >= 3,
                # Image the VM runs (from list_microvms; no extra call).
                "image_arn": item.get("imageArn"),
                "image_version": item.get("imageVersion"),
            }
            # Last-seen view for the per-VM detail route (no I/O).
            _LAST_INSTANCE[microvm_id] = {
                "state": state, "image_arn": item.get("imageArn"),
                "image_version": item.get("imageVersion"),
                "architecture": architecture, "memory_mib": memory_mib,
            }
            # The VM IS present in this list → it's alive; clear any accrued
            # absence strikes so a prior transient miss can't later flip it dead.
            _vm_missing_polls.pop(microvm_id, None)
            # A forward served this VM after the snapshot was taken, so the
            # snapshot's suspended/resuming state is stale. Writing it would log a
            # RUNNING → stale → RUNNING blip (an extra resume/suspend and snapshot
            # cost). Skip the write for this poll; the next poll records the real state.
            stale_snapshot = (
                state in ("SUSPENDED", "SUSPENDING", "RESUMING")
                and vm_manager.cost_tracker.served_since(microvm_id, poll_started)
            )
            if not stale_snapshot:
                vm_manager.cost_tracker.record(microvm_id, state, memory_mib=memory_mib,
                                               architecture=architecture)
                # Persist the observed state transition to the DB log so vm_state_log
                # is the COMPLETE transition history (matching the in-memory tracker).
                # vm_session_update_state dedups internally (no-op if state unchanged),
                # so this only writes a row on a real RUNNING↔SUSPENDED↔TERMINATED
                # change — which is what makes v_vm_sessions' reconstructed
                # running/suspended seconds correct (the view reads this log).
                try:
                    await astorage.vm_session_update_state(microvm_id, state)
                except Exception:
                    pass  # non-critical: in-memory tracker still has the transition
            # Persist cost (burst) to DB every poll — collected here and flushed
            # in ONE off-loop batch right after this loop.
            persisted_ids.append(microvm_id)

        # cost_tracker.persist_costs is sync (calls the backend directly), so hand
        # it the sync `storage` and run the whole batch in a worker thread. A DB
        # error must never fail the poll (persist_cost already swallows per VM).
        if persisted_ids:
            try:
                await asyncio.to_thread(vm_manager.cost_tracker.persist_costs, persisted_ids, storage)
            except Exception:
                pass

        # Include recently launched VMs not yet in AWS API response
        for microvm_id, local_info in list(vm_manager.active_microvms.items()):
            if microvm_id not in instances:
                launched_at = local_info.get("launched_at", 0)
                if time.time() - launched_at <= 60:
                    # Same arch/tier rule as listed VMs: record → DB row → default.
                    # (This entry used to carry NO architecture at all, so the UI
                    # labelled a just-launched x86_64 VM with its ARM default.)
                    _row = None
                    if not (local_info.get("architecture") and local_info.get("memory_mib")):
                        try:
                            _row = await astorage.vm_session_get(microvm_id)
                        except Exception:
                            _row = None
                    memory_mib = (local_info.get("memory_mib") or (_row or {}).get("memory_mib")
                                  or DEFAULT_MEMORY_MIB)
                    architecture = (local_info.get("architecture") or (_row or {}).get("architecture")
                                    or DEFAULT_IMAGE_ARCH)
                    instances[microvm_id] = {
                        "endpoint": local_info.get("endpoint", ""),
                        "name": local_info.get("name", ""),
                        "state": "RUNNING",
                        "launched_at": launched_at,
                        "memory_mib": memory_mib,
                        "architecture": architecture,
                        "session_id": local_info.get("session_id"),
                        "idle_timeout_sec": local_info.get("idle_timeout_sec"),
                        "last_active": local_info.get("last_active"),
                        "max_duration_sec": local_info.get("max_duration_sec"),
                    }
                    vm_manager.cost_tracker.record(microvm_id, "RUNNING", memory_mib=memory_mib,
                                                   architecture=architecture)
                    try:
                        await astorage.vm_session_update_state(microvm_id, "RUNNING")
                    except Exception:
                        pass

        # ONE batched cost read, after this poll's transitions were written, so
        # each VM's cost (resume/suspend counts, seconds) includes this poll.
        cost_ids = list(instances.keys())
        try:
            costs = await asyncio.to_thread(vm_manager.cost_tracker.get_costs_many, cost_ids)
        except Exception:
            costs = {}
        # Burst time/size (DB only, no VM call) for VMs that billed burst.
        burst_costs = {m: dict(_cost_for(m)) for m in instances}
        try:
            await burst_time.add_burst_fields(astorage, burst_costs)
        except Exception:
            pass
        for microvm_id, inst in instances.items():
            c = burst_costs[microvm_id]
            inst["cost"] = c
            if "session_cost" not in inst and "unhealthy" in inst:
                # Single-VM rollup of the SAME cost dict (listed VMs only, as before).
                inst["session_cost"] = vm_manager.cost_tracker.aggregate_session_cost([c], 1)

        # Reconcile DB-active VMs that are ABSENT from this list_vms result.
        # Absence alone is ambiguous (a transient list_vms blip can drop a live VM),
        # so we DON'T terminate on one miss and we DON'T use a wall-clock grace.
        # Instead: count consecutive misses per VM; once a VM has been absent for
        # _VM_MISSING_STRIKES consecutive polls, ASK the control plane directly and
        # only mark TERMINATED if it CONFIRMS the VM is dead. A VM that reappears (or
        # that the control plane still says is alive) resets and is left untouched —
        # so a blip can never flip a live VM. Freshly launched VMs (<60s) are given
        # grace since they may not be in the AWS list yet.
        try:
            active_sessions = await astorage.vm_session_list_active()
            listed_ids = set(instances.keys())
            active_ids = set()
            for session in active_sessions:
                mid = session["microvm_id"]
                active_ids.add(mid)
                if mid in listed_ids:
                    _vm_missing_polls.pop(mid, None)
                    continue
                # New VM not in the AWS list yet — don't count it as missing.
                if (time.time() - (session.get("launched_at_epoch") or 0)) <= 60:
                    continue
                misses = _vm_missing_polls.get(mid, 0) + 1
                _vm_missing_polls[mid] = misses
                if misses < _VM_MISSING_STRIKES:
                    continue
                # Struck out on absence — confirm with the control plane before acting.
                try:
                    cp_state = await vm_manager.get_vm_state(mid)
                except Exception:
                    cp_state = "NOT_FOUND"
                if cp_state in _DEAD_STATES:
                    await _mark_vm_dead(
                        vm_manager, mid, cp_state,
                        reason=f"/instances: absent {misses} consecutive polls, control plane confirms",
                    )
                    _vm_missing_polls.pop(mid, None)
                else:
                    # Control plane says alive (RUNNING/SUSPENDED/UNKNOWN) — the
                    # absence was a list blip. Reset so we start counting afresh.
                    _vm_missing_polls.pop(mid, None)
            # Forget miss-counts for VMs no longer active in the DB (already handled).
            for stale in [k for k in _vm_missing_polls if k not in active_ids]:
                _vm_missing_polls.pop(stale, None)
        except Exception:
            pass

        return {
            "instances": instances,
            "total_cost": vm_manager.cost_tracker.get_total_cost(),
            "persistence_mode": get_persistence_mode(),
            "aurora_configured": aurora_configured(),
        }
    except Exception as e:
        # A network/DNS blip reaching the control plane is expected + self-healing
        # (the next ~15s poll retries). Log it as ONE throttled WARNING instead of
        # an error line per poll; genuinely unexpected failures still log each time.
        if _is_connectivity_error(e):
            global _last_connectivity_warn
            now = time.time()
            if now - _last_connectivity_warn >= _CONNECTIVITY_WARN_INTERVAL_SEC:
                _last_connectivity_warn = now
                logger.warning("list_instances skipped: network unreachable, will retry")
        else:
            logger.error(f"Failed to list instances: {e}")
        return {
            # Shallow snapshot: serialization iterates the map, which worker threads mutate.
            "instances": dict(vm_manager.active_microvms),
            "total_cost": vm_manager.cost_tracker.get_total_cost(),
            "persistence_mode": get_persistence_mode(),
            "aurora_configured": aurora_configured(),
        }


# Max concurrent per-session cost queries for one POST /sessions/costs request.
_SESSION_COSTS_CONCURRENCY = 4


async def _session_cost_payload(session_id: str) -> dict:
    """Build the durable, server-calculated cost payload for one session.

    Storage returns raw per-VM quantities for every VM that has ever served this
    session (including terminated/rotated VMs). The existing price_quantities()
    function calculates each VM's cost; this helper then aggregates those cost
    objects on the server. No caller needs to calculate or sum cost client-side.
    """
    rows = await astorage.vm_cost_quantities_for_session(session_id)

    totals = {
        "running_secs": 0,
        "suspended_secs": 0,
        "running_cost_usd": 0.0,
        "suspended_cost_usd": 0.0,
        "burst_cost_usd": 0.0,
        "snapshot_read_cost_usd": 0.0,
        "snapshot_write_cost_usd": 0.0,
        "total_cost_usd": 0.0,
        "vm_count": 0,
    }
    vm_costs = {}

    for row in rows:
        microvm_id = row["microvm_id"]
        cost = price_quantities(
            running_secs=row.get("running_secs") or 0.0,
            suspended_secs=row.get("suspended_secs") or 0.0,
            burst_mb_seconds=row.get("burst_mb_seconds") or 0.0,
            memory_mib=row.get("memory_mib") or DEFAULT_MEMORY_MIB,
            architecture=row.get("architecture"),
            resume_count=row.get("resume_count") or 0,
            suspend_count=row.get("suspend_count") or 0,
        )
        # VM-id keyed for a future UI drill-down. Metadata is durable DB history,
        # so a TERMINATED predecessor remains visible in this response.
        vm_costs[microvm_id] = {
            "microvm_id": microvm_id,
            "state": row.get("state") or "UNKNOWN",
            "launched_at": row.get("launched_at"),
            "terminated_at": row.get("terminated_at"),
            "memory_mib": row.get("memory_mib") or DEFAULT_MEMORY_MIB,
            "architecture": row.get("architecture") or DEFAULT_IMAGE_ARCH,
            "cost": cost,
        }
        totals["running_secs"] += cost["running_secs"]
        totals["suspended_secs"] += cost["suspended_secs"]
        totals["running_cost_usd"] += cost["running_cost_usd"]
        totals["suspended_cost_usd"] += cost["suspended_cost_usd"]
        totals["burst_cost_usd"] += cost["burst_cost_usd"]
        totals["snapshot_read_cost_usd"] += cost["snapshot_read_cost_usd"]
        totals["snapshot_write_cost_usd"] += cost["snapshot_write_cost_usd"]
        totals["total_cost_usd"] += cost["total_cost_usd"]

    totals["vm_count"] = len(vm_costs)
    # Match CostTracker's precision contract so this endpoint and VM cards agree.
    totals["running_secs"] = round(totals["running_secs"])
    totals["suspended_secs"] = round(totals["suspended_secs"])
    for key in ("running_cost_usd", "suspended_cost_usd", "burst_cost_usd",
                "snapshot_read_cost_usd", "snapshot_write_cost_usd", "total_cost_usd"):
        totals[key] = round(totals[key], 6)

    return {
        "session_id": session_id,
        "total_cost_usd": totals["total_cost_usd"],
        "total": totals,
        "vm_costs": vm_costs,
    }


@router.get("/sessions/{session_id}/cost")
async def get_session_cost(session_id: str):
    """Return durable cumulative cost for one notebook session.

    New endpoint — does not alter GET /instances or existing API response shapes.
    Unknown/new sessions return a zero total with an empty breakdown (200), allowing
    a Notebook row to ask for its stable session id before it has ever launched a VM.
    """
    return await _session_cost_payload(session_id)


@router.post("/sessions/costs")
async def get_session_costs(request: Request):
    """Return server-calculated cumulative cost for a set of session IDs.

    Body: {"session_ids": ["session-a", "session-b", ...]}

    This is intentionally a NEW bulk read endpoint for the Notebooks-panel total
    header. It returns each singular session payload plus a server-summed aggregate,
    so the browser never adds session/VM cost figures itself.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_ids = body.get("session_ids", []) if isinstance(body, dict) else []
    if not isinstance(raw_ids, list):
        return json_error(400, "session_ids must be an array")
    # Preserve caller order, ignore blank/non-string IDs, dedupe, and cap the work.
    session_ids = list(dict.fromkeys(
        sid.strip() for sid in raw_ids if isinstance(sid, str) and sid.strip()
    ))[:100]

    # Bound the fan-out: at most _SESSION_COSTS_CONCURRENCY cost queries
    # in flight per request, so 100 ids can't occupy 100 executor threads / DB
    # connections at once. Per-request semaphore (binds to the running loop);
    # gather preserves caller order.
    sem = asyncio.Semaphore(_SESSION_COSTS_CONCURRENCY)

    async def _bounded(sid: str) -> dict:
        async with sem:
            return await _session_cost_payload(sid)

    payloads = await asyncio.gather(*[_bounded(sid) for sid in session_ids])
    session_costs = {payload["session_id"]: payload for payload in payloads}
    total = {
        "running_secs": 0,
        "suspended_secs": 0,
        "running_cost_usd": 0.0,
        "suspended_cost_usd": 0.0,
        "burst_cost_usd": 0.0,
        "total_cost_usd": 0.0,
        "session_count": 0,
        "vm_count": 0,
    }
    for payload in payloads:
        ptotal = payload["total"]
        if ptotal["vm_count"] > 0:
            total["session_count"] += 1
        for key in ("running_secs", "suspended_secs", "running_cost_usd", "suspended_cost_usd", "burst_cost_usd", "total_cost_usd", "vm_count"):
            total[key] += ptotal[key]
    total["running_secs"] = round(total["running_secs"])
    total["suspended_secs"] = round(total["suspended_secs"])
    for key in ("running_cost_usd", "suspended_cost_usd", "burst_cost_usd", "total_cost_usd"):
        total[key] = round(total[key], 6)

    return {
        "total_cost_usd": total["total_cost_usd"],
        "total": total,
        "session_costs": session_costs,
    }


_MICROVM_ID_RE = re.compile(r"microvm-[A-Za-z0-9-]{1,80}")


def _valid_microvm_id(mid: str) -> bool:
    return bool(mid) and _MICROVM_ID_RE.fullmatch(mid) is not None


async def _detail_s3_read(vm_manager, session_id: str) -> dict:
    """Read sessions/<sid>/metadata.json and requirements.txt (missing key → None)."""
    def _read():
        s3 = vm_manager._get_s3_client()
        bucket = vm_manager.get_artifacts_bucket()
        out = {"metadata": None, "requirements": None}
        if not bucket:
            return out
        for key, field in (("metadata.json", "metadata"), ("requirements.txt", "requirements")):
            try:
                body = s3.get_object(Bucket=bucket, Key=f"sessions/{session_id}/{key}")["Body"].read()
            except Exception as e:
                code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
                if code in ("NoSuchKey", "404", "NotFound"):
                    continue
                raise
            text = body.decode("utf-8", errors="replace")
            if field == "metadata":
                out["metadata"] = json.loads(text) if text.strip() else None
            else:
                out["requirements"] = text.splitlines()
        return out
    return await asyncio.to_thread(_read)


async def _detail_vm_status_fetch(vm_manager, microvm_id: str) -> dict | None:
    """GET https://<endpoint>/vm-status on a RUNNING VM (caller checked the state)."""
    info = vm_manager.active_microvms.get(microvm_id) or {}
    endpoint = info.get("endpoint")
    if not endpoint:
        row = await astorage.vm_session_get(microvm_id)
        endpoint = (row or {}).get("endpoint")
    if not endpoint:
        return None
    token = await vm_manager.get_auth_token(microvm_id)
    resp = await http_clients.request_idempotent(
        http_clients.vm_client(), "GET",
        f"https://{endpoint}/vm-status",
        headers={"X-aws-proxy-auth": token},
        timeout=3.0,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data if isinstance(data, dict) else None


async def _fetch_restore_report(vm_manager, microvm_id: str, endpoint: str) -> dict | None:
    """GET https://<endpoint>/restore-report on the VM that just restored."""
    token = await vm_manager.get_auth_token(microvm_id)
    resp = await http_clients.request_idempotent(
        http_clients.vm_client(), "GET",
        f"https://{endpoint}/restore-report",
        headers={"X-aws-proxy-auth": token},
        timeout=5.0,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data if isinstance(data, dict) else None
async def _restore_checkpoint_size_bounded(vm_manager, session_id: str):
    """(kb, source) of the session's S3 checkpoint, bounded to 3 s. Never raises:
    any failure logs a warning and gives (None, 'unknown')."""
    try:
        from proxy.notebook.session_relink import restore_checkpoint_size
        return await asyncio.wait_for(restore_checkpoint_size(vm_manager, session_id), timeout=3.0)
    except Exception as e:
        logger.warning(f"Restore memory check: size lookup failed for session {session_id} ({type(e).__name__})")
        return None, "unknown"


async def _restore_memory_check_for(vm_manager, session_id: str, memory_mib, size=None) -> dict:
    """restore_memory_check for a /launch restore: the session's real S3 checkpoint
    size against the EFFECTIVE tier (pinned default or override). Logged; warn only,
    the launch is never blocked. Bounded to 3 s; any failure gives level 'unknown'.
    `size` is a precomputed (kb, source) from _restore_checkpoint_size_bounded
    (fetched concurrently by /launch); None fetches it here."""
    if size is None:
        size = await _restore_checkpoint_size_bounded(vm_manager, session_id)
    kb, source = size
    check = dict(restore_memory_assessment(kb, memory_mib), source=source)
    msg = (f"Restore memory check: session {session_id} checkpoint {check['checkpoint_kb']} KB ({source}) "
           f"on {memory_mib} MiB: need {check['need_mib']} MiB, ratio {check['ratio']}, level {check['level']}")
    if check["level"] in ("warn", "strong"):
        logger.warning(msg + " (restore continues)")
    else:
        logger.info(msg)
    return check


async def _launch_restore_report(vm_manager, microvm_id: str, endpoint: str) -> dict:
    """Restore report for the /launch response. Fetched once, bounded to 5 s; any
    failure (old image, timeout, non-200) becomes {verified: False, reason}."""
    try:
        raw = await asyncio.wait_for(_fetch_restore_report(vm_manager, microvm_id, endpoint), timeout=5.0)
    except Exception as e:
        logger.warning(f"Restore report fetch failed for {microvm_id} ({e})")
        return {"verified": False, "reason": "could not read the restore report from the VM"}
    return normalize_restore_report(raw)
async def _detail_base_image(vm_manager, image_arn: str, image_version) -> dict | None:
    """Base image of OUR image version via control-plane GetMicrovmImageVersion
    (never the VM). → {"arn", "version"} or None."""
    ident = str(image_arn or "")
    if ident.count(":") > 6:
        ident = ident.rsplit(":", 1)[0]  # arn with a version suffix
    if not ident or image_version in (None, ""):
        return None
    client = vm_manager.get_lambda_poll_client()
    resp = await asyncio.wait_for(asyncio.to_thread(
        client.get_microvm_image_version,
        imageIdentifier=ident, imageVersion=str(image_version)), 3.0)
    arn, ver = (resp or {}).get("baseImageArn"), (resp or {}).get("baseImageVersion")
    if not arn or ver in (None, ""):
        return None
    return {"arn": arn, "version": str(ver)}


@router.get("/instances/{microvm_id}/detail")
async def get_instance_detail(microvm_id: str, request: Request):
    """Per-VM detail for an EXPANDED Live MicroVMs card (vm-card-state).

    Fetched by the UI on expand and on a VM state change only (NO periodic
    refresh: each call to a RUNNING VM resets its idle timer), never on the
    /instances poll. It calls the VM's GET /vm-status only when the control plane
    says the VM is RUNNING right now: a SUSPENDED MicroVM auto-resumes on traffic,
    so for suspended VMs it uses the checkpoint metadata in S3 instead.
    Contract: see proxy/platform/vm_detail.py (any part without data is null)."""
    if not _valid_microvm_id(microvm_id):
        return json_error(400, "invalid microvm id")
    vm_manager = request.app.state.vm_manager
    from proxy.platform import vm_detail
    try:
        known = microvm_id in _LAST_INSTANCE or microvm_id in vm_manager.active_microvms \
            or (await astorage.vm_session_get(microvm_id)) is not None
    except Exception:
        known = microvm_id in _LAST_INSTANCE or microvm_id in vm_manager.active_microvms
    if not known:
        return json_error(404, "unknown microvm")
    try:
        from proxy.platform.routes.metrics import latest_image_versions
        return await vm_detail.build_vm_detail(
            vm_manager, microvm_id,
            astorage=astorage,
            persistence_mode=get_persistence_mode(),
            s3_read=lambda sid: _detail_s3_read(vm_manager, sid),
            vm_status_fetch=lambda mid: _detail_vm_status_fetch(vm_manager, mid),
            latest_versions=lambda: latest_image_versions(vm_manager),
            base_image=lambda arn, ver: _detail_base_image(vm_manager, arn, ver),
            last_instance=_LAST_INSTANCE,
        )
    except Exception as e:
        logger.warning(f"/instances/{microvm_id}/detail failed: {type(e).__name__}")
        return {"microvm_id": microvm_id, "state": None, "kind": None, "image": None,
                "architecture": None, "python": None, "usage": None, "origin": None,
                "checkpoint": None, "dirty": None, "variables": None, "packages": None,
                "vm_status": "unavailable", "fetched_at": time.time()}


@router.get("/instances/{microvm_id}/metrics-history")
async def get_instance_metrics_history(microvm_id: str, request: Request, groups: int = 10):
    """Past CPU / memory / disk of THIS VM for the Resources tab, grouped per run
    (vm-run-chart): the last `groups` run groups (clamped 1–20) plus lifetime peaks.

    DB-ONLY, through the storage interface: it never calls the VM or the control
    plane, so it cannot wake a SUSPENDED VM or reset a RUNNING VM's idle timer.
    The UI fetches it once when the Resources tab is shown (and on its refresh
    button), never on a timer. Cached HISTORY_CACHE_SECONDS per (id, groups).
    Contract: proxy/platform/vm_metrics_history.py."""
    from proxy.platform import vm_metrics_history as vmh
    if not _valid_microvm_id(microvm_id):
        return json_error(400, "invalid microvm id")
    vm_manager = request.app.state.vm_manager
    try:
        known = microvm_id in _LAST_INSTANCE or microvm_id in vm_manager.active_microvms \
            or (await astorage.vm_session_get(microvm_id)) is not None
    except Exception:
        known = microvm_id in _LAST_INSTANCE or microvm_id in vm_manager.active_microvms
    if not known:
        return json_error(404, "unknown microvm")
    groups = vmh.clamp_groups(groups)
    key = (microvm_id, groups)
    cached = vmh.cache_get(key)
    if cached is not None:
        return cached
    try:
        payload = await vmh.build_metrics_history(
            microvm_id, astorage=astorage, last_instance=_LAST_INSTANCE, groups=groups,
        )
    except Exception as e:
        logger.warning(f"/instances/{microvm_id}/metrics-history failed: {type(e).__name__}")
        payload = vmh.empty_payload(microvm_id, state=None, memory_limit_mb=None,
                                    now=time.time(), error="unavailable")
    if not payload.get("error"):
        vmh.cache_put(key, payload)
    return payload


@router.get("/instances/metrics")
async def get_instance_metrics(microvm_id: str = None, request: Request = None):
    """Fetch real-time metrics from a specific running MicroVM."""
    vm_manager = request.app.state.vm_manager
    if not microvm_id:
        return json_error(400, "microvm_id is required")
    info = vm_manager.active_microvms.get(microvm_id)
    endpoint = info.get("endpoint") if info else None

    if not endpoint:
        try:
            detail = await vm_manager.get_microvm_detail(microvm_id)
            endpoint = detail.get("endpoint", "")
            if endpoint:
                vm_manager.active_microvms[microvm_id] = {
                    "endpoint": endpoint, "launched_at": time.time(), "memory_mib": DEFAULT_MEMORY_MIB,
                }
        except Exception:
            pass

    if not endpoint:
        return {"metrics": {}}

    try:
        token = await vm_manager.get_auth_token(microvm_id)
        resp = await http_clients.request_idempotent(
            http_clients.vm_client(), "GET",
            f"https://{endpoint}/metrics",
            headers={"X-aws-proxy-auth": token},
            timeout=5.0,
        )
        if resp.status_code == 200:
            m = resp.json()
            await astorage.metrics_record(
                microvm_id=microvm_id,
                cpu_pct=m.get("cpu", {}).get("percent", 0),
                mem_pct=m.get("memory", {}).get("percent", 0),
                mem_used_mb=m.get("memory", {}).get("used_mb", 0),
                disk_pct=m.get("disk", {}).get("percent", 0),
                disk_used_mb=m.get("disk", {}).get("used_mb", 0),
                net_bytes_sent=m.get("network", {}).get("bytes_sent", 0),
                net_bytes_recv=m.get("network", {}).get("bytes_recv", 0),
                processes=m.get("processes", 0),
                uptime_sec=m.get("uptime_sec", 0),
            )
            # Healthy response → clear any accrued strikes.
            if microvm_id in vm_manager.active_microvms:
                vm_manager.active_microvms[microvm_id]["_502_strikes"] = 0
            # Track burst usage for cost calculation
            used_mb = m.get("memory", {}).get("used_mb", 0)
            if used_mb > 0:
                vm_manager.cost_tracker.record_burst(microvm_id, used_mb)
            return {"metrics": {microvm_id: m}}
        else:
            logger.warning(f"Metrics endpoint for {microvm_id} returned {resp.status_code}")
            await _reconcile_metrics_failure(vm_manager, microvm_id)
    except Exception:
        # Network error / timeout to the VM — treat like a strike too, so a dead
        # VM (whose URL times out) gets reconciled instead of being polled forever.
        await _reconcile_metrics_failure(vm_manager, microvm_id)

    return {"metrics": {}}


# Strikes before the metrics poller gives up on a VM and (if the control plane
# confirms it's gone) drops it from active_microvms so it stops being polled. Same
# threshold the /proxy path uses for the `unhealthy` flag, so behavior is consistent.
_METRICS_DEAD_STRIKES = 3

# Control-plane states that are a POSITIVE assertion the VM is dead (not merely
# absent / mid-resume). Acting on these is safe; UNKNOWN/RUNNING/SUSPENDED are not.
_DEAD_STATES = ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND")

# Consecutive-miss tracking for VMs that are ACTIVE in the DB but ABSENT from
# a /instances list_vms result. Absence is an ambiguous signal (a transient
# list_vms blip / eventual consistency can drop a live VM for one call), so we do
# NOT terminate on a single miss. We count consecutive misses per VM; only after
# the VM has been absent from _VM_MISSING_STRIKES consecutive polls AND a confirming
# get_vm_state says it's dead do we mark it TERMINATED. Any poll that lists the VM
# resets its counter to zero, so a blip self-corrects and can never flip a live VM.
_vm_missing_polls: dict[str, int] = {}
_VM_MISSING_STRIKES = 3

# Last /instances view per VM ({state, image_arn, image_version, architecture,
# memory_mib}), filled by list_instances with no I/O. The per-VM detail route
# reads it so a card expand needs no extra control-plane list call.
_LAST_INSTANCE: dict[str, dict] = {}
# Cold launches between RunMicrovm and the RUNNING commit: {vm_id: {name,
# memory_mib, architecture, launched_at}}. Read by GET /instances only.
_PENDING_LAUNCH: dict[str, dict] = {}


async def _mark_vm_dead(vm_manager, microvm_id: str, state: str, *, reason: str) -> None:
    """A VM has been CONFIRMED dead by the control plane. Make that death
    authoritative everywhere so the session can cleanly relaunch and the UI stops
    showing a ghost:
      • Persist vm_sessions -> TERMINATED (vm_session_mark_terminated is a
        compare-and-set: it stamps terminated_at + writes a vm_state_log row + bumps
        updated_at only if the row isn't already TERMINATED, so it's idempotent and
        keeps the cost/duration views correct).
      • Drop the in-memory active_microvms entry and its cached auth token so the
        poller stops hammering a dead endpoint.
      • COMPARE-AND-SWAP unregister the session→VM mapping: only clear it if it
        STILL points at THIS dead VM. If a concurrent relaunch has already relinked
        the session to a fresh VM, leave that mapping (and the new VM) untouched —
        the same guard terminate_session uses to avoid close↔reopen races.
    Safe to call repeatedly / from multiple detection paths (metrics + /instances)."""
    # Resolve the session this VM served BEFORE we evict in-memory state (the
    # session_id lives on the active_microvms entry / the DB row).
    session_id = None
    info = vm_manager.active_microvms.get(microvm_id)
    if info:
        session_id = info.get("session_id")
    if not session_id:
        try:
            db_row = await astorage.vm_session_get(microvm_id)
            if db_row:
                session_id = db_row.get("session_id")
        except Exception:
            pass

    # vm_session_mark_terminated is a compare-and-set: it returns True only on a
    # REAL transition (row existed and wasn't already TERMINATED), False if it was
    # already terminated or unknown. Gate ALL follow-up work + the log on that flip
    # so we don't re-evict / re-unregister / re-log every 15s poll for VMs the AWS
    # list keeps returning as already-TERMINATED (the log-spam this fixes).
    try:
        transitioned = await astorage.vm_session_mark_terminated(microvm_id)
    except Exception as e:
        logger.warning(f"mark_dead: vm_session_mark_terminated({microvm_id}) failed: {e}")
        transitioned = False
    if not transitioned:
        # Already reconciled (or unknown). Still make sure it's not lingering in the
        # in-memory poll set, but do so QUIETLY — no repeated work or logging.
        vm_manager.active_microvms.pop(microvm_id, None)
        return

    vm_manager.active_microvms.pop(microvm_id, None)
    try:
        await vm_manager.token_cache.delete(microvm_id)
    except Exception:
        pass

    # COMPARE-AND-SWAP unregister: only clear the session→VM mapping if it STILL
    # points at THIS dead VM. A concurrent relaunch may have already relinked the
    # session to a fresh VM — never unregister that one.
    #
    # ETERNAL MODE EXCEPTION: this function is also reached from the /instances
    # poller (list_instances, polled ~every 15s for the dashboard), independently
    # of any /proxy/execute request for the session. In eternal mode, reactive
    # recovery is designed around the request path discovering a dead VM via the
    # STILL-INTACT session→VM mapping (_VmDead check in proxy_request, which routes
    # to recover_and_replay). If this poller unregisters the mapping first, it
    # races ahead of that request-time detection and deletes the very mapping (and,
    # via unregister_session, the in-flight recovery queue) recovery depends on —
    # proxy_request then 404s at its top-of-function guard before persistence_mode
    # is even read, making the eternal recovery branch unreachable. So: in eternal
    # mode, leave the stale mapping in place (pointing at the now-dead vm_id) and
    # let the request path's own _VmDead detection drive recovery, exactly as
    # designed. Non-eternal (checkpoint/none) behavior is unchanged — unregister so
    # the client gets a clean 404 instead of looping 502s against an orphan row.
    session_relinked = False  # true if a concurrent relaunch owns the session now
    if session_id:
        eternal = get_persistence_mode() == "eternal"
        try:
            current = await vm_manager.get_session_vm_async(session_id)
            if current and current.get("vm_id") == microvm_id:
                if eternal:
                    # Primary (Fix 1): keep the stale mapping so the request-time
                    # _VmDead check drives recovery. Also drop a short-lived
                    # tombstone (Fix 2 backstop) recording this dead vm_id, so if a
                    # residual path later clears the mapping before the next
                    # request lands (DB/registry eviction, proxy restart, a future
                    # unregister), proxy_request's 404 guard can still recover and
                    # _do_recover can resolve the dead VM's metadata from its DB row.
                    vm_manager.record_eternal_death_tombstone(session_id, microvm_id)
                    logger.info(
                        f"mark_dead: eternal mode — leaving stale mapping "
                        f"{session_id} → {microvm_id} in place for reactive recovery "
                        f"(tombstone recorded as backstop)"
                    )
                else:
                    vm_manager.unregister_session(session_id)
            elif current:
                session_relinked = True
                logger.info(
                    f"mark_dead: session {session_id} now maps to "
                    f"{current.get('vm_id')} (not dead {microvm_id}) — leaving registry"
                )
        except Exception as e:
            logger.warning(f"mark_dead: CAS-unregister for {microvm_id} failed: {e}")

    # Clear the notebook's live-session link (notebooks.session_id + microvm_id) so
    # the curated v_nb_notebook view stops reporting a now-dead notebook as "open".
    # Same compare-and-swap safety as the registry unregister above: if a concurrent
    # relaunch has already relinked the session to a fresh live VM, the notebook is
    # still open on THAT VM — don't clear its link. Best-effort: a clear failure must
    # never block VM teardown. Only reached on a REAL TERMINATED transition (this
    # whole function is gated on _DEAD_STATES), so it never fires on SUSPEND.
    if session_id and not session_relinked:
        try:
            await astorage.notebook_clear_session(session_id)
        except Exception as e:
            logger.warning(f"mark_dead: notebook_clear_session({session_id}) failed: {e}")
        dag_service.invalidate_session(session_id)
    # Dead VM: drop its DAG push/attach bookkeeping (forget_vm keeps a session
    # entry that is busy or already serves a relaunched VM).
    dag_service.forget_vm(microvm_id, None if session_relinked else session_id)

    logger.info(f"Marked VM {microvm_id} TERMINATED (state={state}, reason={reason})")


async def _reconcile_metrics_failure(vm_manager, microvm_id: str) -> None:
    """A metrics fetch to a VM failed (5xx or network error). Mirror the /proxy
    path's strike accounting so a repeatedly-failing VM is flagged `unhealthy` in
    /instances. Once it has struck out, ASK the control plane for the truth: if the
    VM is actually TERMINATED/gone, evict it from active_microvms (and its cached
    endpoint + auth token) so the metrics poller stops hammering a dead endpoint —
    the exact 502-storm that saturates the single-worker proxy. A VM that's merely
    mid-resume (still RUNNING/SUSPENDED at the control plane) is LEFT in place so a
    transient blip doesn't evict a live VM."""
    info = vm_manager.active_microvms.get(microvm_id)
    if info is None:
        return  # already gone — nothing to poll
    strikes = info.get("_502_strikes", 0) + 1
    info["_502_strikes"] = strikes
    if strikes < _METRICS_DEAD_STRIKES:
        return
    try:
        state = await vm_manager.get_vm_state(microvm_id)
    except Exception:
        state = "NOT_FOUND"
    if state in _DEAD_STATES:
        # Control plane CONFIRMS dead → make it authoritative: mark the DB row
        # TERMINATED + CAS-unregister the session (so a fresh VM can be launched),
        # in addition to evicting from active_microvms + token cache so the poller
        # stops hammering the dead endpoint (the 502-storm this guards against).
        await _mark_vm_dead(
            vm_manager, microvm_id, state,
            reason=f"metrics poll: {strikes} consecutive failures",
        )



