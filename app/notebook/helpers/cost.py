"""UTILITIES — session cost: price the v_nb_session_cost quantities into dollars."""

import os
import json


# =============================================================================
# UTILITIES — session cost: price the v_nb_session_cost quantities into dollars
# =============================================================================
# (Categorized under UTILITIES so estimate_session_cost groups with the other
# utility helpers in the Snippets panel / autocomplete — the category comes from
# this header via completion_catalog. The panel merges all UTILITIES blocks into
# one group regardless of physical position. Private _pricing_table/_rates_for_arch
# are underscore-prefixed and excluded from the catalog.)
# The v_nb_* views expose objective QUANTITIES (running/suspended seconds, burst
# MB-seconds, memory tier) but NOT dollars — pricing rates are config, not schema,
# so cost is computed HERE from those quantities. This is a faithful port of the
# proxy's single pricing function (proxy/platform/cost_tracker.py::price_quantities);
# keep the two in lock-step so notebook-side and platform-side dollars never diverge.
#
# Rates come from MICROVM_PRICING_JSON (injected into the VM at launch, same value
# the proxy uses), read at CALL time so a rate change takes effect without a rebuild.

# Default memory tier (MiB) when a row has no memory_mib: DEFAULT_MEMORY_MIB, set
# in scripts/config.sh and passed into the VM at launch, same value the proxy uses.
# 2048 only when the variable is absent or invalid (matches config.sh's default).
_FALLBACK_MEMORY_MIB = 2048


def _default_memory_mib() -> int:
    try:
        v = int(os.environ.get("DEFAULT_MEMORY_MIB", _FALLBACK_MEMORY_MIB))
    except (TypeError, ValueError):
        return _FALLBACK_MEMORY_MIB
    return v if v > 0 else _FALLBACK_MEMORY_MIB


# Last-resort fallback (arm64/Graviton), identical to cost_tracker._FALLBACK_RATES.
_COST_FALLBACK_RATES = {
    "vcpu_per_sec": 0.0000276944,
    "memory_per_gb_sec": 0.0000036667,
    "snapshot_per_gb_month": 0.08,
    "snapshot_read_per_gb": 0.00155,
    "snapshot_write_per_gb": 0.00380,
}


def _pricing_table() -> dict:
    """Load the per-arch pricing table from MICROVM_PRICING_JSON (read at call
    time). Mirrors cost_tracker._load_pricing: on missing/invalid JSON, fall back
    to the built-in arm64 rates keyed by the default arch."""
    default_arch = os.environ.get("DEFAULT_IMAGE_ARCH", "arm64")
    raw = os.environ.get("MICROVM_PRICING_JSON", "")
    if raw:
        try:
            table = json.loads(raw)
            if isinstance(table, dict) and table:
                return table
        except (json.JSONDecodeError, ValueError):
            pass
    return {default_arch: dict(_COST_FALLBACK_RATES)}


def _rates_for_arch(arch: str = None) -> dict:
    """Rates for an arch → default arch → hard fallback (mirrors cost_tracker)."""
    table = _pricing_table()
    default_arch = os.environ.get("DEFAULT_IMAGE_ARCH", "arm64")
    return table.get(arch or default_arch) or table.get(default_arch) or _COST_FALLBACK_RATES


def _num(value, default=0.0):
    """Coerce a usage quantity to float, mapping NULL/invalid to `default`. The
    quantities often arrive straight from the v_nb_session_cost view (via
    estimate_session_cost(**row)), where a column can be SQL NULL — which pandas
    hands back as pd.NA / NaN / None. A bare `float(value or default)` RAISES on
    pd.NA ('boolean value of NA is ambiguous'), so we null-check explicitly here —
    keeping estimate_session_cost total (never raises) for every caller."""
    if value is None:
        return float(default)
    try:
        import math
        f = float(value)
        return float(default) if math.isnan(f) else f
    except (TypeError, ValueError):
        # pd.NA raises TypeError on float(); any non-numeric falls back too.
        return float(default)


def estimate_session_cost(running_secs: float = 0.0, suspended_secs: float = 0.0,
                          burst_mb_seconds: float = 0.0, memory_mib: int = None,
                          architecture: str = None,
                          resume_count: int = 0, suspend_count: int = 0) -> dict:
    """
    Estimate a MicroVM session's cost in USD from its usage QUANTITIES — the exact
    three-component model the platform bills on:

      1. COMPUTE (running):  vCPU-seconds + GB-seconds while the VM is RUNNING
      2. STORAGE (suspended): GB-seconds of snapshot storage while SUSPENDED
      3. BURST:              vCPU/GB-seconds accrued ABOVE the memory tier

    Get the quantities from the `v_nb_session_cost` view (one row per session), then
    price them here. Rates come from the platform pricing table (same rates the
    proxy uses); pass `architecture` to price a non-default arch.

    Args:
        running_secs:     seconds the VM(s) spent RUNNING (v_nb_session_cost.running_secs)
        suspended_secs:   seconds SUSPENDED (v_nb_session_cost.suspended_secs)
        burst_mb_seconds: accumulated MB-seconds of memory burst (v_nb_session_cost.burst_mb_seconds)
        memory_mib:       memory tier in MiB (v_nb_session_cost.memory_mib); None/0 =
                          the platform default tier (DEFAULT_MEMORY_MIB)
        architecture:     'arm64' | 'x86_64' (v_nb_session_cost.architecture); default arch if None

    Returns a dict:
        {running_cost_usd, suspended_cost_usd, burst_cost_usd, snapshot_read_cost_usd,
         snapshot_write_cost_usd, total_cost_usd, running_secs, suspended_secs,
         memory_gb, burst_mb_seconds, burst_gb_seconds, burst_vcpu_seconds,
         resume_count, suspend_count}

    Example (price the current notebook's session):
        q = read_aurora("SELECT running_secs, suspended_secs, burst_mb_seconds, "
                        "memory_mib, architecture FROM v_nb_session_cost "
                        "WHERE session_id = '<this-session-id>'")
        cost = estimate_session_cost(**q.iloc[0].to_dict())
        print(cost["total_cost_usd"])
    """
    rates = _rates_for_arch(architecture)
    price_vcpu_per_sec = rates["vcpu_per_sec"]
    price_memory_per_gb_sec = rates["memory_per_gb_sec"]
    price_snapshot_per_gb_sec = rates["snapshot_per_gb_month"] / (30 * 24 * 3600)
    price_snapshot_read_per_gb = rates.get("snapshot_read_per_gb", _COST_FALLBACK_RATES["snapshot_read_per_gb"])
    price_snapshot_write_per_gb = rates.get("snapshot_write_per_gb", _COST_FALLBACK_RATES["snapshot_write_per_gb"])

    running_secs = _num(running_secs, 0.0)
    suspended_secs = _num(suspended_secs, 0.0)
    # Counts arrive from the view and may be SQL NULL / pd.NA — coerce through _num.
    resume_count = int(_num(resume_count, 0.0))
    suspend_count = int(_num(suspend_count, 0.0))
    # memory_mib: NULL/0/invalid → the platform default tier (DEFAULT_MEMORY_MIB).
    default_mib = float(_default_memory_mib())
    memory_gb = _num(memory_mib, default_mib) / 1024.0
    if memory_gb <= 0:
        memory_gb = default_mib / 1024.0
    vcpus = memory_gb / 2.0  # 2 GB : 1 vCPU

    running_cost = (
        vcpus * running_secs * price_vcpu_per_sec +
        memory_gb * running_secs * price_memory_per_gb_sec
    )
    suspended_cost = memory_gb * suspended_secs * price_snapshot_per_gb_sec

    burst_mb_seconds = _num(burst_mb_seconds, 0.0)
    burst_gb_seconds = burst_mb_seconds / 1024.0
    burst_vcpu_seconds = burst_gb_seconds / 2.0
    burst_cost = (
        burst_vcpu_seconds * price_vcpu_per_sec +
        burst_gb_seconds * price_memory_per_gb_sec
    )
    # Snapshot DATA charges: one READ per transition into RUNNING (cold launch +
    # every resume), one WRITE per transition into SUSPENDED; GB = the memory tier.
    snapshot_read_cost = resume_count * memory_gb * price_snapshot_read_per_gb
    snapshot_write_cost = suspend_count * memory_gb * price_snapshot_write_per_gb
    total_cost = running_cost + suspended_cost + burst_cost + snapshot_read_cost + snapshot_write_cost

    return {
        "running_secs": round(running_secs),
        "suspended_secs": round(suspended_secs),
        "running_cost_usd": round(running_cost, 6),
        "suspended_cost_usd": round(suspended_cost, 6),
        "burst_cost_usd": round(burst_cost, 6),
        "snapshot_read_cost_usd": round(snapshot_read_cost, 6),
        "snapshot_write_cost_usd": round(snapshot_write_cost, 6),
        "total_cost_usd": round(total_cost, 6),
        "memory_gb": memory_gb,
        "burst_mb_seconds": round(burst_mb_seconds, 1),
        "burst_gb_seconds": round(burst_gb_seconds, 2),
        "burst_vcpu_seconds": round(burst_vcpu_seconds, 2),
        "resume_count": int(resume_count or 0),
        "suspend_count": int(suspend_count or 0),
    }


def session_cost() -> dict:
    """
    Cost of THIS notebook's session so far, in USD — the zero-argument one-liner.
    Resolves the current session id automatically (injected at VM launch), reads its
    usage QUANTITIES from the `v_nb_session_cost` view, and prices them with
    estimate_session_cost(). No ids to copy, no SQL to write.

        c = session_cost()
        print(c["total_cost_usd"])

    Returns the SAME dict estimate_session_cost() does (running/suspended/burst and
    total USD, plus the underlying quantities), or {"error": "..."} when cost can't
    be resolved — Aurora not configured (launch the proxy with --AuroraSetup), or no
    analytics row for this session yet (the SQLite→Aurora sync lags ~a minute after a
    fresh launch; retry shortly).

    For a DIFFERENT or older session, or a printed summary with the notebook name,
    use this_notebook(session_id="…"). To price arbitrary quantities by hand, call
    estimate_session_cost(...) directly.
    """
    import os as _os

    cluster = _os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
    secret = _os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
    if not (cluster and secret):
        return {"error": "Session cost needs Aurora analytics, which isn't "
                         "configured. Launch the proxy with --AuroraSetup."}

    session_id = _os.environ.get("MICROVM_SESSION_ID") or ""
    if not session_id:
        return {"error": "No session id is available in this runtime, so the "
                         "current session's cost can't be resolved."}

    # read_aurora lives in the aurora helper module; import at call time to avoid a
    # package import cycle (cost is imported early in helpers/__init__).
    from .aurora import read_aurora

    try:
        q = read_aurora(
            "SELECT running_secs, suspended_secs, burst_mb_seconds, memory_mib, "
            "architecture FROM v_nb_session_cost WHERE session_id = :sid",
            parameters=[{"name": "sid", "value": {"stringValue": session_id}}],
        )
    except Exception as e:
        return {"error": f"Could not read session usage from Aurora ({type(e).__name__}). "
                         f"The cluster may be resuming — retry in a few seconds."}

    if q is None or len(q) == 0:
        return {"error": "No analytics row for this session yet — the SQLite→Aurora "
                         "sync hasn't caught up (it lags ~a minute after launch). "
                         "Retry shortly."}

    return estimate_session_cost(**q.iloc[0].to_dict())
