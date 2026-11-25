"""
Read-only VM status for the Live MicroVMs card.

Endpoint:
  GET /vm-status -> {python, checkpoint_enabled, dirty, last_checkpoint_at,
                     last_save: {vars, pkl_kb, size_kb, total_kb, user_vars} | null, restored, restored_from,
                     var_count, var_bytes, untracked, packages: [...], packages_total,
                     last_suspend_save: {status, reason, duration_s, bytes, saved_at,
                                         budget_s, committed} | null}
last_suspend_save is the outcome of the last /suspend hook save on this VM
(status ok / skipped / timed_out / failed; see app.platform.hooks).

Cheap by design: no repr(), no value serialization, no variable names or values
in the response. Variable totals use the same skip rules as the executor's
_count_user_variables() plus sys.getsizeof (shallow, like the Variables panel's
size column). It increments no counters and never mutates notebook state.

The proxy calls this only for a RUNNING VM whose card is expanded (a suspended
MicroVM auto-resumes on traffic, so the proxy never calls it then).
"""

import asyncio
import logging
import platform
import sys

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vm-status"])

# Bounded work: the namespace walk and the untracked scan run off the event loop.
_VAR_TOTALS_TIMEOUT_SEC = 2.0
_UNTRACKED_TIMEOUT_SEC = 1.5
_PACKAGES_CAP = 50

_SKIP_TYPES = ("module", "function", "builtin_function_or_method", "type")


def user_variable_totals(namespace) -> tuple[int, int]:
    """(count, shallow bytes) of user variables in a namespace dict. Same filters
    as executor._count_user_variables(); per-value getsizeof failures are skipped.
    Also used by the checkpoint manager for metadata.json user_variables_count, so
    a suspended VM's card shows the same number as a running one."""
    count = 0
    total = 0
    for name, value in list(namespace.items()):  # snapshot
        if name.startswith("__") and name.endswith("__"):
            continue
        if name.startswith("_"):
            continue
        try:
            type_name = type(value).__name__
            getattr(type(value), "__module__", "")
        except Exception:
            continue
        if type_name in _SKIP_TYPES:
            continue
        count += 1
        try:
            total += int(sys.getsizeof(value))
        except Exception:
            pass
    return count, total


def _untracked_count(executor, dag_store):
    cell_ids = dag_store.cell_ids() if dag_store is not None else None
    result = executor.untracked(cell_ids)
    visible = (result or {}).get("visible")
    return len(visible) if isinstance(visible, list) else None


async def _bounded(fn, timeout, *args):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)


@router.get("/restore-report")
async def restore_report(request: Request):
    """Package re-install outcome of the last restore on this VM (kept in memory on
    the checkpoint manager). Read once by the proxy after a restore launch."""
    cm = getattr(request.app.state, "checkpoint_manager", None)
    report = getattr(cm, "last_restore_report", None)
    return report or {"verified": False, "reason": "no restore on this VM"}


@router.get("/vm-status")
async def vm_status(request: Request):
    try:
        state = request.app.state
        executor = state.executor
        cm = getattr(state, "checkpoint_manager", None)
        session_state = getattr(state, "session_state", {}) or {}
        dag_store = getattr(state, "dag_store", None)

        checkpoint_enabled = bool(session_state.get("checkpoint_enabled"))

        var_bytes = None
        try:
            var_count, var_bytes = await _bounded(user_variable_totals, _VAR_TOTALS_TIMEOUT_SEC,
                                                executor._namespace)
        except Exception:
            try:
                var_count = executor.get_stats().get("variables_count")
            except Exception:
                var_count = None
            var_bytes = None

        try:
            untracked = await _bounded(_untracked_count, _UNTRACKED_TIMEOUT_SEC, executor, dag_store)
        except Exception:
            untracked = None

        dirty = None
        if checkpoint_enabled and cm is not None:
            try:
                dirty = bool(await _bounded(cm.is_dirty, _UNTRACKED_TIMEOUT_SEC))
            except Exception:
                dirty = None

        last_save = None
        summary = getattr(cm, "last_save_summary", None) or {}
        if summary:
            last_save = {"vars": summary.get("vars"), "pkl_kb": summary.get("pkl_kb"),
                         "size_kb": summary.get("size_kb"),
                         # whole saved state; size_kb is
                         # the bytes WRITTEN by that save (a delta).
                         "total_kb": summary.get("total_kb"),
                         "user_vars": summary.get("user_vars"),
                         # [{name, reason, kind}] the last save left out
                         "not_saved": summary.get("not_saved") or []}

        last_at = float(getattr(cm, "last_checkpoint_at", 0) or 0)
        restored_from = getattr(dag_store, "restored_from", None)
        restored = bool(getattr(cm, "last_restore_timings", None)) or restored_from is not None

        pkgs = sorted(str(p) for p in (getattr(cm, "_user_installed_packages", None) or []))

        return {
            "python": platform.python_version(),
            "checkpoint_enabled": checkpoint_enabled,
            "dirty": dirty,
            "last_checkpoint_at": last_at or None,
            "last_save": last_save,
            "restored": restored,
            "restored_from": restored_from,
            "var_count": var_count,
            "var_bytes": var_bytes,
            "untracked": untracked,
            "packages": pkgs[:_PACKAGES_CAP],
            "packages_total": len(pkgs),
            "last_suspend_save": getattr(cm, "last_suspend_save", None),
        }
    except Exception as e:
        logger.warning(f"/vm-status failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "status unavailable"})
