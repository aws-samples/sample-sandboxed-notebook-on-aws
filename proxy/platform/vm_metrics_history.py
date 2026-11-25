"""
Past CPU / memory / disk for the Live MicroVMs card's Resources tab.
Part of: proxy.platform (Smart MicroVM Service layer)

build_metrics_history() assembles GET /instances/{microvm_id}/metrics-history
for ONE VM (the MicroVMs panel is VM-centric; the notebook-level view is the
Notebooks panel's Run History) from the DATABASE ONLY, through the storage
interface (astorage):
  - metrics_get_summary         (vm_metrics, VM lifetime → the bullet bars)
  - metrics_get_bucketed        (vm_metrics at 1 s resolution → the run groups)
  - notebook_run_list_for_vms   (notebook_runs → which run each sample belongs to)
  - vm_session_get              (state, tier memory limit)
It takes no vm_manager and no HTTP client, so it can never call the VM: a
SUSPENDED MicroVM auto-resumes on traffic and every call to a RUNNING VM resets
its idle timer.

Samples are written only while cells execute (~1/s, the UI polls
GET /instances/metrics then), so the chart's x axis is made of RUN GROUPS, not
wall-clock time: every sample belongs to exactly one group, either the
notebook run whose window contains it (± ATTACH_SLOP_SEC) or an ad-hoc "cells"
burst (contiguous samples outside every run, split at gaps > BURST_GAP_SEC).
Idle and suspended time never appear on the axis. No sample is dropped; each
group is downsampled max-preserving to <= GROUP_POINTS points.

Payload:
  {microvm_id, state, memory_limit_mb,
   summary: lifetime block (summary_block) | None,
   groups: [group, ...]   chronological, the LAST `groups` of them,
   groups_total: int      all groups in the VM lifetime,
   active_sec: float      sum of duration_sec over ALL groups,
   empty: bool, fetched_at, error?: "unavailable"}
  group = {id ("run:<run_id>" | "cells:<int first t>"), kind ("run" | "cells"),
           run_id, trigger, start, end, duration_sec, samples,
           cells_executed, cells_errored, cells_interrupted (None for cells),
           cpu_peak, cpu_avg, mem_peak_mb, mem_peak_pct,
           points: [{t, t_end, cpu, cpu_avg, mem_mb, mem_pct, disk_pct, samples}]}
"""
import logging
import math
import time

from proxy.platform.vm_detail import _iso_to_epoch

logger = logging.getLogger(__name__)

GROUPS_DEFAULT = 10          # run groups returned when the caller does not ask
GROUPS_MIN = 1
GROUPS_MAX = 20
GROUP_POINTS = 30            # max points per group after max-preserving downsampling
ATTACH_SLOP_SEC = 2          # a sample this close to a run's window belongs to the run
BURST_GAP_SEC = 10           # a gap longer than this splits ad-hoc "cells" bursts
# Max rows read per VM: the lifetime is bucketed so it fits in this many rows
# (43200 = 12 h at 1 s; a VM lives at most 8 h, so normally no bucketing).
RAW_ROWS_CAP_SEC = 43200
# How far back the lifetime summary looks. 7 days matches the installer's
# METRICS_RETENTION_HOURS=168 (scripts/config.sh); older samples are pruned anyway.
LIFETIME_FLOOR_SEC = 7 * 86400
MAX_RUNS = 200               # notebook_runs rows read to attach samples to runs
HISTORY_CACHE_SECONDS = 10.0
_CACHE_MAX = 128

# {(microvm_id, groups): (monotonic_ts, payload)}
_CACHE: dict[tuple, tuple] = {}


def clear_cache() -> None:
    _CACHE.clear()


def cache_get(key, now_mono: float | None = None):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.monotonic() if now_mono is None else now_mono) - ts > HISTORY_CACHE_SECONDS:
        _CACHE.pop(key, None)
        return None
    return payload


def cache_put(key, payload, now_mono: float | None = None) -> None:
    if len(_CACHE) >= _CACHE_MAX and key not in _CACHE:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.monotonic() if now_mono is None else now_mono, payload)


def clamp_groups(v) -> int:
    try:
        g = int(v)
    except (TypeError, ValueError):
        g = GROUPS_DEFAULT
    return max(GROUPS_MIN, min(GROUPS_MAX, g))


def _r(v, nd=3):
    return None if v is None else round(float(v), nd)


def run_windows(runs: list, lo: float, end: float) -> list[dict]:
    """notebook_runs rows → [{run_id, trigger, start, end, running, ...}] overlapping [lo, end], ascending."""
    out = []
    for r in runs or []:
        fin = _iso_to_epoch(r.get("finished_at"))
        start = _iso_to_epoch(r.get("started_at"))
        if start is None and fin is not None:
            start = fin - float(r.get("total_exec_ms") or 0) / 1000.0
        if start is None:
            continue
        stop = fin if fin is not None else start
        if stop < lo or start > end:
            continue
        out.append({
            "run_id": r.get("run_id"), "trigger": r.get("trigger"),
            "notebook_id": r.get("notebook_id"), "microvm_id": r.get("microvm_id"),
            "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
            "start": _r(start), "end": _r(stop), "running": fin is None,
            "cells_executed": int(r.get("cells_executed") or 0),
            "cells_errored": int(r.get("cells_errored") or 0),
            "cells_interrupted": int(r.get("cells_interrupted") or 0),
            "total_exec_ms": float(r.get("total_exec_ms") or 0.0),
        })
    out.sort(key=lambda w: w["start"])
    return out[-MAX_RUNS:]


def _attach_windows(windows: list[dict], now: float) -> list[tuple]:
    """[(lo, hi, window)] for sample attachment; an in-progress run extends to
    the next run's start (or now)."""
    out = []
    for i, w in enumerate(windows):
        hi = w["end"]
        if w["running"]:
            hi = windows[i + 1]["start"] if i + 1 < len(windows) else now
        out.append((w["start"] - ATTACH_SLOP_SEC, max(hi, w["start"]) + ATTACH_SLOP_SEC, w))
    return out


def _row_t(row: dict) -> float:
    return float(row.get("t_first") if row.get("t_first") is not None else row.get("t_last") or 0.0)


def _mem_pct(mem_mb, row_pct, limit):
    if mem_mb is not None and limit:
        return _r(mem_mb / limit * 100.0, 1)
    return _r(row_pct, 1)


def downsample(rows: list[dict], limit=None, n: int = GROUP_POINTS) -> list[dict]:
    """Per-second rows (ascending) → <= n points. Chunks are contiguous and
    near-equal by count; each keeps the MAX cpu / memory / disk, so no peak is
    lost, and a sample-weighted cpu_avg."""
    if not rows:
        return []
    k = len(rows)
    if k <= n:
        chunks = [[r] for r in rows]
    else:
        chunks = [rows[i * k // n:(i + 1) * k // n] for i in range(n)]
    pts = []
    for ch in chunks:
        if not ch:
            continue
        samples = sum(int(r.get("samples") or 0) for r in ch)
        cpus = [r.get("cpu_max") for r in ch if r.get("cpu_max") is not None]
        mems = [r.get("mem_used_max") for r in ch if r.get("mem_used_max") is not None]
        mpcts = [r.get("mem_pct_max") for r in ch if r.get("mem_pct_max") is not None]
        disks = [r.get("disk_pct_max") for r in ch if r.get("disk_pct_max") is not None]
        wsum = sum(float(r.get("cpu_avg")) * int(r.get("samples") or 0)
                   for r in ch if r.get("cpu_avg") is not None)
        wn = sum(int(r.get("samples") or 0) for r in ch if r.get("cpu_avg") is not None)
        mem = max(mems) if mems else None
        pts.append({
            "t": _r(_row_t(ch[0])),
            "t_end": _r(ch[-1].get("t_last") if ch[-1].get("t_last") is not None else _row_t(ch[-1])),
            "cpu": _r(max(cpus), 2) if cpus else None,
            "cpu_avg": _r(wsum / wn, 2) if wn else None,
            "mem_mb": _r(mem, 1),
            "mem_pct": _mem_pct(mem, max(mpcts) if mpcts else None, limit),
            "disk_pct": _r(max(disks), 2) if disks else None,
            "samples": samples,
        })
    return pts


def group_block(kind: str, rows: list[dict], limit, window: dict | None = None) -> dict:
    """One run group / ad-hoc burst from its rows (ascending, non-empty)."""
    t0 = _row_t(rows[0])
    t1 = max(float(r.get("t_last") if r.get("t_last") is not None else _row_t(r)) for r in rows)
    if window is not None:
        start = min(window["start"], t0)
        end = max(t1, window["end"]) if not window["running"] else t1
    else:
        start, end = t0, t1
    pts = downsample(rows, limit)
    cpus = [p["cpu"] for p in pts if p["cpu"] is not None]
    mems = [p["mem_mb"] for p in pts if p["mem_mb"] is not None]
    mpcts = [p["mem_pct"] for p in pts if p["mem_pct"] is not None]
    wsum = sum(float(r["cpu_avg"]) * int(r.get("samples") or 0) for r in rows if r.get("cpu_avg") is not None)
    wn = sum(int(r.get("samples") or 0) for r in rows if r.get("cpu_avg") is not None)
    mem_peak = max(mems) if mems else None
    w = window or {}
    return {
        "id": f"run:{w.get('run_id')}" if window is not None else f"cells:{int(t0)}",
        "kind": kind,
        "run_id": w.get("run_id"),
        "trigger": w.get("trigger"),
        "start": _r(start), "end": _r(end),
        "duration_sec": _r(max(1.0, end - start), 1),
        "samples": sum(int(r.get("samples") or 0) for r in rows),
        "cells_executed": w.get("cells_executed") if window is not None else None,
        "cells_errored": w.get("cells_errored") if window is not None else None,
        "cells_interrupted": w.get("cells_interrupted") if window is not None else None,
        "cpu_peak": _r(max(cpus), 2) if cpus else None,
        "cpu_avg": _r(wsum / wn, 2) if wn else None,
        "mem_peak_mb": _r(mem_peak, 1),
        "mem_peak_pct": (_r(mem_peak / limit * 100.0, 1) if (mem_peak is not None and limit)
                         else (_r(max(mpcts), 1) if mpcts else None)),
        "points": pts,
    }


def group_rows(rows: list[dict], windows: list[dict], now: float, limit=None) -> list[dict]:
    """Assign every row to a run (latest-starting window that contains it) or an
    ad-hoc burst; return all groups in chronological order. No row is dropped."""
    att = _attach_windows(windows, now)
    by_run: dict = {}
    order: list = []          # [("run", key) | ("cells", rows)]
    burst: list | None = None
    prev_t = None
    for row in sorted(rows or [], key=_row_t):
        t = _row_t(row)
        hit = None
        for lo, hi, w in att:
            if lo <= t <= hi and (hit is None or w["start"] >= hit["start"]):
                hit = w
        if hit is not None:
            key = (hit["run_id"], hit["start"])
            if key not in by_run:
                by_run[key] = (hit, [])
                order.append(("run", key))
            by_run[key][1].append(row)
            burst = None          # a run between two ad-hoc rows splits the burst
        else:
            if burst is None or prev_t is None or t - prev_t > BURST_GAP_SEC:
                burst = []
                order.append(("cells", burst))
            burst.append(row)
            prev_t = float(row.get("t_last") if row.get("t_last") is not None else t)
            continue
        prev_t = None
    out = []
    for kind, ref in order:
        if kind == "run":
            w, rs = by_run[ref]
            out.append(group_block("run", rs, limit, w))
        else:
            out.append(group_block("cells", ref, limit))
    out.sort(key=lambda g: g["start"])
    return out


def summary_block(s: dict | None, limit, last_disk_pct=None) -> dict | None:
    """metrics_get_summary row (or None) → the response `summary` block (VM lifetime)."""
    if not s or not s.get("samples"):
        return None
    cpu_peak, mem_peak = s.get("cpu_max"), s.get("mem_used_max")
    return {
        "samples": int(s["samples"]),
        "first_at": _r(s.get("first_at")),
        "last_at": _r(s.get("last_at")),
        "cpu": {"peak": _r(cpu_peak, 2), "peak_at": _r(s.get("cpu_max_at")),
                "avg": _r(s.get("cpu_avg"), 2)},
        "memory": {"peak_mb": _r(mem_peak, 1), "peak_at": _r(s.get("mem_used_max_at")),
                   "avg_mb": _r(s.get("mem_used_avg"), 1), "limit_mb": limit,
                   "peak_pct_of_limit": _r(mem_peak / limit * 100.0, 1)
                   if (mem_peak is not None and limit) else None},
        "disk": {"peak_pct": _r(s.get("disk_pct_max"), 2), "peak_at": _r(s.get("disk_pct_max_at")),
                 "peak_used_mb": _r(s.get("disk_used_max"), 1),
                 "last_pct": _r(last_disk_pct, 2)},
    }


def empty_payload(microvm_id, *, state, memory_limit_mb, now, error=None) -> dict:
    out = {
        "microvm_id": microvm_id, "state": state, "memory_limit_mb": memory_limit_mb,
        "summary": None, "groups": [], "groups_total": 0, "active_sec": 0,
        "empty": True, "fetched_at": now,
    }
    if error:
        out["error"] = error
    return out


def _limit_of(row: dict | None, li: dict | None):
    v = (row or {}).get("memory_mib") or (li or {}).get("memory_mib")
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


async def build_metrics_history(microvm_id: str, *, astorage, last_instance: dict,
                                groups: int = GROUPS_DEFAULT,
                                now=time.time) -> dict:
    """Assemble the metrics-history payload for ONE VM (contract: module docstring).

    Raises nothing for missing data; a storage failure on the metrics gives the
    empty payload with "error": "unavailable" (callers must not cache it)."""
    t_now = float(now())
    groups = clamp_groups(groups)
    li = (last_instance or {}).get(microvm_id) or {}
    try:
        row = await astorage.vm_session_get(microvm_id)
    except Exception as e:
        logger.warning(f"metrics-history vm_session_get failed: {type(e).__name__}")
        row = None
    state = li.get("state") or (row or {}).get("state")
    limit = _limit_of(row, li)

    base = dict(state=state, memory_limit_mb=limit, now=t_now)
    floor_lo = t_now - LIFETIME_FLOOR_SEC
    try:
        full = await astorage.metrics_get_summary(microvm_id, floor_lo, t_now)
    except Exception as e:
        logger.warning(f"metrics-history summary failed: {type(e).__name__}")
        return empty_payload(microvm_id, error="unavailable", **base)
    if not full or not full.get("samples"):
        return empty_payload(microvm_id, **base)

    lo = float(math.floor(full["first_at"]))
    end = max(float(full["last_at"]), lo)
    bucket_sec = max(1, int(math.ceil((end + 1 - lo) / RAW_ROWS_CAP_SEC)))
    try:
        rows = await astorage.metrics_get_bucketed(microvm_id, lo, end, bucket_sec)
    except Exception as e:
        logger.warning(f"metrics-history rows failed: {type(e).__name__}")
        return empty_payload(microvm_id, error="unavailable", **base)

    try:
        runs_raw = await astorage.notebook_run_list_for_vms([microvm_id], MAX_RUNS)
    except Exception as e:
        logger.warning(f"metrics-history runs failed: {type(e).__name__}")
        runs_raw = None
    windows = [w for w in run_windows(runs_raw or [], float("-inf"), float("inf"))
               if not w.get("microvm_id") or w.get("microvm_id") == microvm_id]

    all_groups = group_rows(rows or [], windows, t_now, limit)
    last_disk = next((r.get("disk_pct_max") for r in reversed(rows or [])
                      if r.get("disk_pct_max") is not None), None)
    summary = summary_block(full, limit, last_disk)
    return {
        "microvm_id": microvm_id, "state": state, "memory_limit_mb": limit,
        "summary": summary,
        "groups": all_groups[-groups:],
        "groups_total": len(all_groups),
        "active_sec": _r(sum(g["duration_sec"] for g in all_groups), 1),
        "empty": summary is None,
        "fetched_at": t_now,
    }
