"""Pure helpers for the restore flow: the explicit arch/tier choice, the
checkpoint-size-versus-tier memory check and the package re-install report
returned with /launch.

No I/O here so all are unit-testable without the proxy.
"""
import math

# The restore memory rule. The SAME numbers are in web/src/services/restoreChoice.js
# (the Connect dialog warning); tests/proxy/test_restore_choice.py fails when they differ.
#   need  = checkpoint size (MiB) x RESTORE_MEMORY_FACTOR
#   ratio = need / chosen tier MiB
#   ratio < RESTORE_WARN_RATIO -> none; ratio > RESTORE_STRONG_RATIO -> strong; else warn.
RESTORE_MEMORY_FACTOR = 2
RESTORE_WARN_RATIO = 0.5
RESTORE_STRONG_RATIO = 0.9


def _positive_number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def restore_memory_assessment(checkpoint_kb, memory_mib, factor=RESTORE_MEMORY_FACTOR,
                              warn=RESTORE_WARN_RATIO, strong=RESTORE_STRONG_RATIO):
    """Compare a checkpoint size (KB, metadata.json checkpoint_total_kb) with the
    tier a restore runs on, whatever the saved tier was. Warn only, never a block.
    Returns {level: none|warn|strong|unknown, checkpoint_kb, need_mib, tier_mib, ratio}.
    An unknown or non-positive size (or tier) gives level 'unknown'.
    """
    kb = _positive_number(checkpoint_kb)
    mem = _positive_number(memory_mib)
    out = {"level": "unknown", "checkpoint_kb": checkpoint_kb if kb else None,
           "need_mib": None, "tier_mib": memory_mib if mem else None, "ratio": None}
    if not kb or not mem:
        return out
    need = kb / 1024 * factor
    ratio = need / mem
    level = "none" if ratio < warn else "strong" if ratio > strong else "warn"
    out.update(level=level, need_mib=int(round(need)), ratio=round(ratio, 2))
    return out


def latest_vm_session(rows):
    """The VM row a restore is pinned to: the one that served the session last.

    A pool-granted VM keeps its MINT time as launched_at, so it can sort before
    an older session row; launched_at order alone then picks the wrong VM (and
    the wrong saved arch/tier). A row still running (no terminated_at) is the
    latest; otherwise the latest terminated_at wins, launched_at breaks ties.
    Returns None for an empty list.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return None
    # Equal keys keep the list order (storage returns launched_at ascending): the last wins.
    return max(enumerate(rows), key=lambda ir: (not ir[1].get("terminated_at"),
                                                ir[1].get("terminated_at") or "",
                                                ir[1].get("launched_at") or "", ir[0]))[1]


def resolve_restore_choice(body, saved_arch, saved_mem, arches, valid_for):
    """Resolve the arch and memory tier for a restore launch.

    Returns (arch, memory_mib, error_message_or_None). Only an explicit
    ``restoreOverride is True`` honors the body's arch / memoryMiB; without it
    (or with any other value) the saved values are returned unchanged. A missing
    arch or memory in an override falls back to the saved value. An explicit
    choice is never snapped to the nearest tier: it must be offered.
    """
    if (body or {}).get("restoreOverride") is not True:
        return saved_arch, saved_mem, None
    arch = body.get("arch") or saved_arch
    if arch not in arches:
        return saved_arch, saved_mem, (
            f"Unsupported architecture '{arch}'. Available: {', '.join(str(a) for a in arches)}")
    mem = body.get("memoryMiB")
    if mem is None:
        mem = saved_mem
    tiers = list(valid_for(arch))
    # bool is an int subclass; reject it explicitly.
    if mem is None:
        mem = tiers[0] if tiers else None
    if isinstance(mem, bool) or not isinstance(mem, int) or mem not in tiers:
        return saved_arch, saved_mem, (
            f"Unsupported memory size '{mem}' for {arch}. Available: {', '.join(str(t) for t in tiers)}")
    return arch, mem, None


def normalize_restore_report(raw):
    """Normalize the VM's /restore-report payload. Never raises.

    Anything malformed or missing becomes ``{verified: False, reason}``.
    """
    try:
        if not isinstance(raw, dict):
            return {"verified": False, "reason": "no restore report from the VM"}
        if raw.get("verified") is not True:
            reason = raw.get("reason")
            return {"verified": False,
                    "reason": reason if isinstance(reason, str) and reason else "VM could not verify the restore"}
        failed = []
        for item in raw.get("packages_failed") or []:
            if isinstance(item, dict) and item.get("name"):
                failed.append({"name": str(item["name"]), "reason": str(item.get("reason") or "")})
            elif isinstance(item, str) and item:
                failed.append({"name": item, "reason": ""})
        total = raw.get("packages_total")
        if isinstance(total, bool) or not isinstance(total, int):
            total = len(failed)
        return {
            "verified": True,
            "arch": raw.get("arch"),
            "python": raw.get("python"),
            "packages_total": total,
            "packages_failed": failed,
        }
    except Exception:
        return {"verified": False, "reason": "malformed restore report"}
