"""
Per-VM detail for the Live MicroVMs card (vm-card-state).

Part of: proxy.platform (Smart MicroVM Service layer)

build_vm_detail() assembles GET /instances/{microvm_id}/detail from data the
proxy already has (vm_sessions, vm_state_log, pool_vms, the cost tracker, the
last /instances view) plus:
  - checkpoint metadata from S3 (sessions/<sid>/metadata.json + requirements.txt),
    cached 60 s per session;
  - the VM's own GET /vm-status, ONLY when the control plane says the VM is
    RUNNING right now. A suspended MicroVM auto-resumes on traffic
    (idlePolicy.autoResumeEnabled), so calling it would wake it and cost money.

Every dependency is injected so the assembly is testable offline. Each part is
computed in its own try/except: one failure gives null for that part only.

Payload (every key always present; a part that is unknown or failed is null):
  microvm_id, fetched_at (epoch)
  state         control-plane state (RUNNING / SUSPENDED / ...), else the DB row's
  kind          "notebook" | "pool_spare"
  image         {name, version, latest_version, outdated, base: {name, arn, version} | null}
  architecture  arm64 | x86_64
  python        interpreter version from /vm-status, else learned per image
  usage         cost_tracker.usage_stats(microvm_id)
  vm_status     "live" | "unavailable" | "skipped_not_running" | "not_applicable" (pool spare)
  Notebook VMs only:
  checkpoint    {applies, saved_at, vars, vars_approx, size_kb, source: "vm" | "s3" | "none"}
                plus last_suspend_save {status: ok|skipped|timed_out|failed, reason,
                duration_s, bytes, saved_at, committed} from the live /vm-status, only
                while the VM is RUNNING and it reported one (the outcome of its
                last /suspend hook save; after a suspend it shows on the next resume)
  dirty         bool, unsaved changes since the last checkpoint (live VM only)
  variables     {untracked, source: "vm"}
  packages      {names, total, source: "vm" | "checkpoint"}
  origin        {source: "pool" | "fresh" | null, restored, restored_checkpoint_at}
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PERSISTING_MODES = ("eternal", "checkpoint")
VM_STATUS_TIMEOUT_SEC = 3.0
S3_CACHE_SECONDS = 60.0
# A VM-reported save and the S3 metadata.json within this many seconds are the
# same checkpoint (the VM stamps its time just after the metadata write).
SAME_SAVE_WINDOW_SEC = 120.0
# Checkpoint size shown on the card = the WHOLE saved state (vm-card-regroup):
# /vm-status last_save.total_kb while RUNNING, else metadata.json
# checkpoint_total_kb. checkpoint_size_kb / last_save.size_kb / pkl_kb are the
# bytes written by ONE save (deltas) and are never shown. An older checkpoint
# without the total gets no size (never a wrong one, never 0 B).

# Python version per (image_arn, image_version), learned from a live /vm-status,
# so suspended VMs and pool spares of the same image can show it.
_PY_BY_IMAGE: dict[tuple, str] = {}
# Base image per (image_arn, image_version): (monotonic_ts, {"arn","version"} | None).
# Image versions are immutable, so a found base is kept for good; a miss or a
# failure is retried after BASE_RETRY_SEC.
_BASE_BY_IMAGE: dict[tuple, tuple] = {}
BASE_RETRY_SEC = 300.0
# sessions/<sid> S3 reads: {sid: (monotonic_ts, {"metadata":..., "requirements":...})}
_S3_CACHE: dict[str, tuple] = {}
# Checkpoint time a VM was restored from, captured once per VM.
_RESTORED_AT: dict[str, str] = {}


def _positive_kb(value) -> float | None:
    """A size in KB that is a real number > 0, else None (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _not_saved(value) -> list:
    """Variables a save left out, as [{name, reason, kind}] (at most 20, strings
    capped), from /vm-status last_save.not_saved or metadata.json not_saved."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and item.get("name"):
            entry = {"name": str(item["name"])[:200], "reason": str(item.get("reason") or "")[:300],
                     "kind": str(item.get("kind") or "")}
            if item.get("kept_previous") is True:
                entry["kept_previous"] = True
            out.append(entry)
        if len(out) >= 20:
            break
    return out


_SUSPEND_STATUSES = ("ok", "skipped", "timed_out", "failed")


def _suspend_outcome(value) -> dict | None:
    """/vm-status last_suspend_save, normalized; None when absent or unknown."""
    if not isinstance(value, dict) or value.get("status") not in _SUSPEND_STATUSES:
        return None
    dur, nbytes = value.get("duration_s"), value.get("bytes")
    return {
        "status": value["status"],
        "reason": str(value.get("reason") or "")[:300],
        "duration_s": float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) else None,
        "bytes": int(nbytes) if isinstance(nbytes, int) and not isinstance(nbytes, bool) else None,
        "saved_at": str(value["saved_at"]) if value.get("saved_at") else None,
        "committed": value.get("committed") is True,
    }


def _iso_to_epoch(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _epoch_to_iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _version_key(v):
    """'4' → (4,), '1.2.3' → (1, 2, 3); anything else → None."""
    if v is None:
        return None
    parts = str(v).strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_outdated(vm_version, latest_version) -> bool:
    a, b = _version_key(vm_version), _version_key(latest_version)
    return bool(a is not None and b is not None and a < b)


def image_name_from_arn(arn: str | None) -> str | None:
    if not arn:
        return None
    return str(arn).split(":")[-1].split("/")[-1] or None


def invalidate_session_cache(session_id: str | None) -> None:
    """Drop a session's cached S3 checkpoint read. Called when the VM suspends or
    a checkpoint is saved, so the next detail read shows the new save at once
    instead of a 'No checkpoint yet' snapshot cached up to S3_CACHE_SECONDS before."""
    if session_id:
        _S3_CACHE.pop(session_id, None)


async def _s3_cached(s3_read, session_id: str, now_mono: float, state: str | None = None):
    """Cached sessions/<sid> read. An entry is reused only while the VM's state is
    the same as when it was read: a RUNNING→SUSPENDED transition is exactly when
    the suspend checkpoint lands, so a read cached while RUNNING must not hide it
    (it showed 'No checkpoint yet' after an idle-suspend save)."""
    hit = _S3_CACHE.get(session_id)
    if (hit is not None and now_mono - hit[0] < S3_CACHE_SECONDS
            and (len(hit) < 3 or hit[2] == state)):
        return hit[1]
    value = await s3_read(session_id)
    _S3_CACHE[session_id] = (now_mono, value, state)
    return value


def _prefer_user_count(user_count, total_count) -> tuple:
    """(count, approx). The user-variable count (same filter as /vm-status) when
    the checkpoint recorded it; else the older whole-namespace total, flagged
    approx so the card doesn't call it 'variables'."""
    if user_count is not None:
        return user_count, False
    return total_count, total_count is not None


def _clean_requirements(lines) -> list[str] | None:
    if lines is None:
        return None
    out = []
    for line in lines:
        s = str(line).strip()
        if s and not s.startswith("#"):
            out.append(s)
    return sorted(out)


async def _base_cached(base_image, image_arn: str, image_version, now_mono: float):
    """Cached base-image lookup → {"name", "arn", "version"} | None."""
    key = (str(image_arn), str(image_version))
    hit = _BASE_BY_IMAGE.get(key)
    if hit is not None and (hit[1] is not None or now_mono - hit[0] < BASE_RETRY_SEC):
        value = hit[1]
    else:
        try:
            value = await base_image(str(image_arn), str(image_version))
        except Exception as e:
            logger.debug(f"vm detail base image lookup failed: {type(e).__name__}")
            value = None
        if not (isinstance(value, dict) and value.get("arn") and value.get("version") not in (None, "")):
            value = None
        _BASE_BY_IMAGE[key] = (now_mono, value)
    if value is None:
        return None
    return {"name": image_name_from_arn(value["arn"]), "arn": value["arn"],
            "version": str(value["version"])}


async def build_vm_detail(vm_manager, microvm_id: str, *, astorage, persistence_mode: str,
                          s3_read, vm_status_fetch, latest_versions, last_instance: dict | None = None,
                          base_image=None, now=time.time, monotonic=time.monotonic) -> dict:
    """Assemble the detail payload described in the module docstring.
    `last_instance` is routes/microvm.py's last /instances view per VM (that
    module owns and fills the dict). Never raises for a per-part failure."""
    last_instance = last_instance if last_instance is not None else {}
    out = {
        "microvm_id": microvm_id, "state": None, "kind": "notebook",
        "image": None, "architecture": None, "python": None, "usage": None,
        "origin": None, "checkpoint": None, "dirty": None, "variables": None,
        "packages": None, "vm_status": "not_applicable", "fetched_at": now(),
    }
    local = vm_manager.active_microvms.get(microvm_id) or {}

    # 1. Row, kind, state, image.
    row = None
    try:
        row = await astorage.vm_session_get(microvm_id)
    except Exception:
        row = None
    session_id = (row or {}).get("session_id") or local.get("session_id")
    notebook_id = (row or {}).get("notebook_id")
    if not row or not (session_id or notebook_id):
        try:
            spares = await astorage.pool_vm_list(state="available")
            if any(r.get("vm_id") == microvm_id for r in spares or []):
                out["kind"] = "pool_spare"
        except Exception:
            pass

    seen = last_instance.get(microvm_id)
    if seen is None:
        try:
            d = await vm_manager.get_microvm_detail(microvm_id)
            seen = {"state": d.get("state"), "image_arn": d.get("imageArn"),
                    "image_version": d.get("imageVersion")}
        except Exception:
            seen = {}
    out["state"] = seen.get("state") or (row or {}).get("state")
    out["architecture"] = seen.get("architecture") or (row or {}).get("architecture") or local.get("architecture")
    image_arn, image_version = seen.get("image_arn"), seen.get("image_version")

    try:
        if image_arn:
            name = image_name_from_arn(image_arn)
            latest_map = await latest_versions() or {}
            base_arn = str(image_arn)
            latest = latest_map.get(base_arn) or latest_map.get(name)
            if latest is None and base_arn.count(":") > 6:
                latest = latest_map.get(base_arn.rsplit(":", 1)[0])  # arn with a version suffix
            out["image"] = {
                "name": name,
                "version": str(image_version) if image_version is not None else None,
                "latest_version": str(latest) if latest is not None else None,
                "outdated": is_outdated(image_version, latest),
                "base": None,
            }
    except Exception as e:
        logger.debug(f"vm detail image part skipped: {type(e).__name__}")
    if out["image"] is not None and base_image is not None and image_version is not None:
        try:
            out["image"]["base"] = await _base_cached(base_image, image_arn, image_version, monotonic())
        except Exception as e:
            logger.debug(f"vm detail base image skipped: {type(e).__name__}")

    is_notebook = out["kind"] == "notebook"

    # 2. Checkpoint metadata from S3 (only when checkpoints apply).
    applies = bool(is_notebook and persistence_mode in PERSISTING_MODES
                   and (row or {}).get("checkpoint_enabled") and session_id)
    s3 = None
    if applies:
        try:
            s3 = await _s3_cached(s3_read, session_id, monotonic(), out.get("state"))
        except Exception:
            s3 = None
    meta = (s3 or {}).get("metadata") or None
    reqs = _clean_requirements((s3 or {}).get("requirements"))

    # 3. Live VM status: RUNNING (control plane, right now) and healthy only.
    live = None
    if is_notebook:
        out["vm_status"] = "skipped_not_running"
        try:
            healthy = (local.get("_502_strikes") or 0) < 3
            cp_state = await vm_manager.get_vm_state(microvm_id) if healthy else None
        except Exception:
            cp_state = None
        if cp_state == "RUNNING":
            out["state"] = "RUNNING"
            try:
                live = await asyncio.wait_for(vm_status_fetch(microvm_id), VM_STATUS_TIMEOUT_SEC)
            except Exception:
                live = None
            if isinstance(live, dict):
                out["vm_status"] = "live"
                if live.get("python") and image_arn:
                    _PY_BY_IMAGE[(image_arn, image_version)] = live["python"]
            else:
                live = None
                out["vm_status"] = "unavailable"
        elif cp_state:
            out["state"] = cp_state

    # 4. Fields.
    try:
        out["python"] = (live or {}).get("python") or _PY_BY_IMAGE.get((image_arn, image_version))
    except Exception:
        pass

    try:
        out["usage"] = vm_manager.cost_tracker.usage_stats(microvm_id)
    except Exception:
        out["usage"] = None

    if not is_notebook:
        return out

    try:
        if applies:
            s3_at = _iso_to_epoch((meta or {}).get("checkpointed_at"))
            vm_at = (live or {}).get("last_checkpoint_at")
            # The VM stamps last_checkpoint_at right AFTER it writes metadata.json,
            # so the same save looks a few seconds "newer" on the VM. Treat saves
            # within SAME_SAVE_WINDOW_SEC as one and use the S3 metadata (saved_at
            # and, on newer images, the user-variable count); the size prefers the
            # live total while RUNNING (see the size rule at SAME_SAVE_WINDOW_SEC).
            same_save = (vm_at and s3_at is not None
                         and abs(float(vm_at) - s3_at) <= SAME_SAVE_WINDOW_SEC)
            if vm_at and not same_save and (s3_at is None or float(vm_at) > s3_at):
                ls = (live or {}).get("last_save") or {}
                vars_n, approx = _prefer_user_count(ls.get("user_vars"), ls.get("vars"))
                # total_kb = the whole saved state; size_kb/pkl_kb are deltas of
                # that one save, never shown. An older image has no total → None.
                out["checkpoint"] = {"applies": True, "saved_at": _epoch_to_iso(vm_at),
                                     "vars": vars_n, "vars_approx": approx,
                                     "size_kb": _positive_kb(ls.get("total_kb")),
                                     "not_saved": _not_saved(ls.get("not_saved")),
                                     "source": "vm"}
            elif meta:
                vars_n, approx = _prefer_user_count(meta.get("user_variables_count"),
                                                    meta.get("variables_count"))
                # Live total for the same save while RUNNING (no extra VM call:
                # the /vm-status fetch above), else the metadata total. Never
                # checkpoint_size_kb (a delta) → an older checkpoint gets None.
                live_total = (_positive_kb(((live or {}).get("last_save") or {}).get("total_kb"))
                              if live is not None and same_save else None)
                out["checkpoint"] = {"applies": True, "saved_at": (meta or {}).get("checkpointed_at"),
                                     "vars": vars_n, "vars_approx": approx,
                                     "size_kb": live_total or _positive_kb(meta.get("checkpoint_total_kb")),
                                     "not_saved": _not_saved(meta.get("not_saved")),
                                     "source": "s3"}
            else:
                out["checkpoint"] = {"applies": True, "saved_at": None, "vars": None,
                                     "vars_approx": False, "size_kb": None, "source": "none"}
    except Exception:
        out["checkpoint"] = None
    try:
        sus = _suspend_outcome(live.get("last_suspend_save")) if live is not None else None
        if sus is not None and isinstance(out.get("checkpoint"), dict):
            out["checkpoint"]["last_suspend_save"] = sus
    except Exception:
        pass

    if live is not None and live.get("checkpoint_enabled") and isinstance(live.get("dirty"), bool):
        out["dirty"] = live["dirty"]

    try:
        # Only the untracked count is shown now (the "N untracked" link on the
        # Checkpoint row); the count/size live in the checkpoint block.
        if live is not None:
            out["variables"] = {"untracked": live.get("untracked"), "source": "vm"}
    except Exception:
        out["variables"] = None

    try:
        if live is not None and isinstance(live.get("packages"), list):
            out["packages"] = {"names": live["packages"],
                               "total": live.get("packages_total", len(live["packages"])), "source": "vm"}
        elif reqs:
            out["packages"] = {"names": reqs, "total": len(reqs), "source": "checkpoint"}
    except Exception:
        out["packages"] = None

    try:
        source = None
        log = await astorage.vm_state_log_get(microvm_id)
        if log:
            source = "pool" if log[0].get("new_state") == "SUSPENDED" else "fresh"
        if source is None and local.get("origin") in ("pool", "fresh"):
            source = local["origin"]
        launched_epoch = _iso_to_epoch((row or {}).get("launched_at")) or _iso_to_epoch(local.get("launched_at"))
        if live is not None:
            restored = bool(live.get("restored"))
        elif local.get("restored_from"):
            restored = True
        else:
            restored = False
            if applies and session_id:
                rows = await astorage.vm_session_list_for_session(session_id)
                for r in rows or []:
                    if r.get("microvm_id") == microvm_id:
                        continue
                    other = _iso_to_epoch(r.get("launched_at"))
                    if other is not None and (launched_epoch is None or other < launched_epoch):
                        restored = True
                        break
        restored_at = _RESTORED_AT.get(microvm_id)
        if restored and restored_at is None and meta:
            s3_at = _iso_to_epoch(meta.get("checkpointed_at"))
            vm_saved = (live or {}).get("last_checkpoint_at")
            # The S3 checkpoint is the one restored from while this VM hasn't saved
            # since: either the VM says so, or the save predates this VM.
            if s3_at is not None and ((live is not None and not vm_saved)
                                      or (launched_epoch is not None and s3_at < launched_epoch)):
                restored_at = meta.get("checkpointed_at")
                _RESTORED_AT[microvm_id] = restored_at
        if source is not None or restored:
            out["origin"] = {"source": source, "restored": restored,
                             "restored_checkpoint_at": restored_at if restored else None}
    except Exception:
        out["origin"] = None

    return out
