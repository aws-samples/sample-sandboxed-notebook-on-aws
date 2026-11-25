"""Backend-neutral helpers for the vm_metrics history queries.

Shared by sqlite_db.py and postgres_db.py so both backends use the same window
bounds and return identically shaped/typed rows (metrics_get_summary,
metrics_get_bucketed, notebook_run_list_for_vms).
"""

import math

# Light notebook_runs columns (everything except the run_cells_json blob).
RUN_LIGHT_COLUMNS = (
    "run_id, notebook_id, session_id, microvm_id, trigger, started_at, finished_at, "
    "cells_executed, cells_errored, cells_interrupted, total_exec_ms"
)

_SUMMARY_TIME_KEYS = ("first_at", "last_at", "cpu_max_at", "mem_used_max_at", "disk_pct_max_at")
_SUMMARY_VALUE_KEYS = ("cpu_avg", "cpu_max", "mem_used_avg", "mem_used_max", "mem_pct_max",
                       "disk_pct_max", "disk_used_max")
_BUCKET_TIME_KEYS = ("t_first", "t_last")
_BUCKET_VALUE_KEYS = ("cpu_avg", "cpu_max", "mem_used_avg", "mem_used_max", "mem_pct_max",
                      "disk_pct_max", "disk_used_max")


def window_bounds(start_epoch: float, end_epoch: float) -> tuple:
    """Whole-second window [lo, hi): lo = floor(start), hi = floor(end) + 1."""
    lo = int(math.floor(float(start_epoch)))
    hi = int(math.floor(float(end_epoch))) + 1
    return lo, hi


def _time(v):
    return None if v is None else round(float(v), 3)


def _num(v):
    return None if v is None else float(v)


def normalize_summary(row: dict) -> dict:
    out = {"samples": int(row.get("samples") or 0)}
    for k in _SUMMARY_TIME_KEYS:
        out[k] = _time(row.get(k))
    for k in _SUMMARY_VALUE_KEYS:
        out[k] = _num(row.get(k))
    return out


def normalize_bucket(row: dict) -> dict:
    out = {"bucket": int(row["bucket"]), "samples": int(row.get("samples") or 0)}
    for k in _BUCKET_TIME_KEYS:
        out[k] = _time(row.get(k))
    for k in _BUCKET_VALUE_KEYS:
        out[k] = _num(row.get(k))
    return out


def normalize_burst(row: dict):
    """metrics_get_burst row → typed dict, or None when no usable sample."""
    n = int(row.get("samples") or 0)
    if n == 0:
        return None
    return {
        "samples": n,
        "burst_samples": int(row.get("burst_samples") or 0),
        "burst_seconds": float(row.get("burst_seconds") or 0.0),
        "burst_mb_seconds": float(row.get("burst_mb_seconds") or 0.0),
        "burst_episodes": int(row.get("burst_episodes") or 0),
        "peak_mem_mb": float(row.get("peak_mem_mb") or 0.0),
    }
def normalize_run(row: dict) -> dict:
    d = dict(row)
    for k in ("cells_executed", "cells_errored", "cells_interrupted"):
        d[k] = int(d.get(k) or 0)
    d["total_exec_ms"] = float(d.get("total_exec_ms") or 0.0)
    return d
