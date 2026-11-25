"""
Time and size of a VM's memory burst, for the Live MicroVMs card's Cost tab
(Burst row) and the Resources tab ("Burst: … billed above baseline").
Part of: proxy.platform (Smart MicroVM Service layer)

Derived from the stored vm_metrics samples through the storage interface
(metrics_get_burst) with the cost tracker's own rule
(CostRecord.record_burst_sample): for each sample above the tier memory that has
a previous sample, the whole gap to that previous sample is billed at the NEWER
sample's overage. No schema change, no new column, no VM call: DB only, so it
can never wake a suspended VM or reset an idle timer.

Caveats (by design):
  * vm_metrics keeps METRICS_RETENTION_HOURS of samples (168 h via
    scripts/config.sh; 24 h if the proxy runs without it). A VM lives at most
    8 h, so its whole life is covered either way; if samples are gone the time
    is simply unknown (the fields are omitted, the UI shows the old text).
  * The first sample has no previous one, so it bills nothing (as in the
    tracker).
  * After a proxy restart the tracker's first poll bills nothing; this
    derivation bills the gap from the last stored sample. Rare, tiny.

Fields merged into the per-VM cost dict (only when a burst time is known):
  burst_seconds         seconds billed above the tier (rounded to 0.1 s)
  burst_peak_over_mb    peak used memory minus the tier (only when > 0)
  burst_episodes        number of crossings above the tier
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

BURST_CACHE_SECONDS = 10.0
_CACHE_MAX = 128
# {(microvm_id, baseline_mb): (monotonic_ts, fields)}
_CACHE: dict[tuple, tuple] = {}


def clear_cache() -> None:
    _CACHE.clear()


def burst_fields(stats, baseline_mb: float) -> dict:
    """Pure: metrics_get_burst() result → cost-dict fields ({} when unknown)."""
    if not stats or not stats.get("burst_samples"):
        return {}
    seconds = float(stats.get("burst_seconds") or 0.0)
    if seconds <= 0:
        return {}
    out = {"burst_seconds": round(seconds, 1)}
    over = float(stats.get("peak_mem_mb") or 0.0) - float(baseline_mb)
    if over > 0:
        out["burst_peak_over_mb"] = round(over, 1)
    episodes = int(stats.get("burst_episodes") or 0)
    if episodes >= 1:
        out["burst_episodes"] = episodes
    return out


def _cache_get(key, now_mono: float):
    hit = _CACHE.get(key)
    if not hit:
        return None
    if now_mono - hit[0] > BURST_CACHE_SECONDS:
        _CACHE.pop(key, None)
        return None
    return hit[1]


def _cache_put(key, fields: dict, now_mono: float) -> None:
    if len(_CACHE) >= _CACHE_MAX and key not in _CACHE:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[key] = (now_mono, fields)


async def burst_fields_for(astorage, microvm_id: str, baseline_mb: float,
                           now_mono: float | None = None) -> dict:
    """Cached (10 s) DB read → burst_fields. Never raises ({} on any error)."""
    now = time.monotonic() if now_mono is None else now_mono
    key = (microvm_id, float(baseline_mb))
    hit = _cache_get(key, now)
    if hit is not None:
        return hit
    try:
        stats = await astorage.metrics_get_burst(microvm_id, float(baseline_mb))
    except Exception as e:  # noqa: BLE001 - the card falls back to the old text
        logger.debug("burst time unavailable for %s: %r", microvm_id, e)
        return {}
    fields = burst_fields(stats, baseline_mb)
    _cache_put(key, fields, now)
    return fields


async def add_burst_fields(astorage, costs: dict) -> None:
    """Merge burst fields into each cost dict in `costs` ({microvm_id: cost})
    whose burst_mb_seconds > 0. Mutates the dicts; one concurrent DB read per VM."""
    todo = [(mid, c) for mid, c in costs.items()
            if isinstance(c, dict) and (c.get("burst_mb_seconds") or 0) > 0]
    if not todo:
        return
    results = await asyncio.gather(*[
        burst_fields_for(astorage, mid, round((c.get("memory_gb") or 4.0) * 1024))
        for mid, c in todo])
    for (_mid, c), fields in zip(todo, results):
        c.update(fields)
