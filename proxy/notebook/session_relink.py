"""
Re-link a notebook to its last session after /terminate cleared the link.

POST /terminate calls notebook_clear_session, which sets notebooks.session_id
(and microvm_id) to NULL so v_nb_notebook stops reporting the notebook as open.
A notebook reopened after that (closed tab, fresh browser, lost localStorage)
then had no session id: no restore was offered, the Connect dialog only offered
a fresh launch with a new session on the default arch, and the checkpoint was
orphaned.

relinked_session_id returns the session to use instead, without writing to the
notebooks row (v_nb_notebook keeps its meaning; /launch persists the link once a
VM really runs on it):
  * only when notebooks.session_id is empty and the notebook is
    checkpoint-enabled (no change while a VM or a session is linked);
  * the session of the notebook's LATEST vm_sessions row, never an older one (a
    fresh launch replaced it on purpose) and never one another notebook uses
    (storage.notebook_latest_session_id);
  * only when that session's checkpoint manifest exists in S3 (a confirmed miss
    means nothing to restore: behave as before). An S3 error (None) counts as
    present, the same fail-safe the /launch restore probe uses.
Manifest results are cached per session for a short time: GET /notebooks is
polled by the UI and must not probe S3 for every closed notebook on every call.
"""
import asyncio
import json
import logging
import time

from proxy.storage import astorage

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_manifest_cache: dict[str, tuple[float, object]] = {}


async def _manifest_exists(vm_manager, session_id: str):
    hit = _manifest_cache.get(session_id)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    try:
        found = await asyncio.to_thread(vm_manager.checkpoint_manifest_exists, session_id)
    except Exception as e:
        logger.warning(f"session relink: manifest probe failed for {session_id} ({type(e).__name__})")
        found = None
    _manifest_cache[session_id] = (now, found)
    return found


async def relinked_session_id(nb: dict, vm_manager) -> str | None:
    """The session id a notebook row with an empty session_id should be shown
    with, or None (see the module docstring). Never raises."""
    try:
        if not nb or nb.get("session_id") or not nb.get("checkpoint_enabled"):
            return None
        sid = await astorage.notebook_latest_session_id(nb.get("id"))
        if not sid or vm_manager is None:
            return None
        if await _manifest_exists(vm_manager, sid) is False:
            return None
        return sid
    except Exception as e:
        logger.warning(f"session relink: lookup failed for notebook {nb.get('id') if nb else None} "
                       f"({type(e).__name__})")
        return None


async def session_saved_tier(session_id: str) -> tuple[str | None, int | None]:
    """(architecture, memory_mib) a restore of session_id will be pinned to: the
    session's latest vm_sessions row (the row /launch restoreFromSession reads),
    with the arch/tier parsed from that VM's image when the row lacks them.
    (None, None) when unknown. Never raises."""
    try:
        from proxy.platform.defaults import arch_size_from_image
        prior = await astorage.vm_session_list_for_session(session_id)
        if not prior:
            return None, None
        from proxy.platform.restore_choice import latest_vm_session
        last = latest_vm_session(prior)
        arch, mem = last.get("architecture"), last.get("memory_mib")
        if not arch or not mem:
            img_arch, img_mem = arch_size_from_image(last.get("image_arn"))
            arch, mem = arch or img_arch, mem or img_mem
        return arch or None, (int(mem) if mem else None)
    except Exception as e:
        logger.warning(f"session relink: saved tier lookup failed for {session_id} ({type(e).__name__})")
        return None, None
_ckpt_kb_cache: dict[str, tuple[float, int | None]] = {}


async def session_checkpoint_kb(vm_manager, session_id: str) -> int | None:
    """checkpoint_total_kb from sessions/<sid>/metadata.json (the size the VM card
    shows as Checkpoint Size), cached 60 s. None when unknown. Never raises."""
    hit = _ckpt_kb_cache.get(session_id)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    kb = None

    def _read():
        s3 = vm_manager._get_s3_client()
        bucket = vm_manager.get_artifacts_bucket()
        if not bucket:
            return None
        try:
            body = s3.get_object(Bucket=bucket, Key=f"sessions/{session_id}/metadata.json")["Body"].read()
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        text = body.decode("utf-8", errors="replace")
        return json.loads(text) if text.strip() else None

    try:
        if vm_manager is not None and session_id:
            meta = await asyncio.to_thread(_read)
            val = (meta or {}).get("checkpoint_total_kb") if isinstance(meta, dict) else None
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                kb = int(val)
    except Exception as e:
        logger.warning(f"session relink: checkpoint size lookup failed for {session_id} ({type(e).__name__})")
        kb = None
    _ckpt_kb_cache[session_id] = (now, kb)
    return kb


# List fallback for restore_checkpoint_size: at most this many pages of 1000 keys.
_CKPT_LIST_MAX_PAGES = 20
_ckpt_list_cache: dict[str, tuple[float, tuple[int | None, str]]] = {}


async def restore_checkpoint_size(vm_manager, session_id: str) -> tuple[int | None, str]:
    """(size_kb, source) of a session's saved state for the restore memory check.
    source is 'metadata' (metadata.json checkpoint_total_kb), 's3_list' (sum of
    the object sizes under sessions/<sid>/, used when metadata has no positive
    total), 's3_list_partial' (the list hit _CKPT_LIST_MAX_PAGES pages: a lower
    bound) or 'unknown' (size None). Cached 60 s. Never raises."""
    kb = await session_checkpoint_kb(vm_manager, session_id)
    if kb:
        return kb, "metadata"
    hit = _ckpt_list_cache.get(session_id)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]

    def _list():
        s3 = vm_manager._get_s3_client()
        bucket = vm_manager.get_artifacts_bucket()
        if not bucket:
            return None, "unknown"
        total, token, pages, truncated = 0, None, 0, False
        while True:
            kwargs = {"Bucket": bucket, "Prefix": f"sessions/{session_id}/", "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kwargs)
            pages += 1
            total += sum(int(o.get("Size") or 0) for o in resp.get("Contents") or [])
            token = resp.get("NextContinuationToken") if resp.get("IsTruncated") else None
            if not token:
                break
            if pages >= _CKPT_LIST_MAX_PAGES:
                truncated = True
                break
        if total <= 0:
            return None, "unknown"
        return int(round(total / 1024)), ("s3_list_partial" if truncated else "s3_list")

    result: tuple[int | None, str] = (None, "unknown")
    try:
        if vm_manager is not None and session_id:
            result = await asyncio.to_thread(_list)
    except Exception as e:
        logger.warning(f"session relink: checkpoint list-size lookup failed for {session_id} ({type(e).__name__})")
        result = (None, "unknown")
    _ckpt_list_cache[session_id] = (now, result)
    return result


async def apply_relink(nb: dict, vm_manager, with_tier: bool = False) -> dict:
    """Fill nb["session_id"] in place from relinked_session_id (response only).
    A relinked session (or, with with_tier, any linked session) also gets
    session_arch / session_memory_mib: the arch and tier its restore is pinned
    to, so the Connect dialog can show them for a tab that has no arch."""
    sid = await relinked_session_id(nb, vm_manager)
    if sid:
        nb["session_id"] = sid
        nb["session_relinked"] = True
    if nb and nb.get("session_id") and (sid or with_tier):
        arch, mem = await session_saved_tier(nb["session_id"])
        nb["session_arch"] = arch
        nb["session_memory_mib"] = mem
        if with_tier:
            nb["session_checkpoint_kb"] = await session_checkpoint_kb(vm_manager, nb["session_id"])
            nb["session_packages"] = await session_packages(vm_manager, nb["session_id"])
    return nb


_pkg_cache: dict[str, tuple[float, list | None]] = {}
# Short: the list can change with every install, and GET /notebooks/{id} is read
# only when a dialog opens or a notebook is reopened (not polled).
_PKG_CACHE_TTL_SECONDS = 10.0


async def session_packages(vm_manager, session_id: str) -> list[str] | None:
    """The user-installed package specs a restore of session_id reinstalls: the
    lines of sessions/<sid>/requirements.txt (what the VM's _restore_packages
    reads), in file order, blank and '#' lines dropped. [] when the checkpoint has
    no requirements.txt; None when unknown (S3 error, no bucket). Cached 10 s.
    Never raises. The Connect dialog shows "Reinstalling N packages (...)..."
    from this while a restore is pending."""
    hit = _pkg_cache.get(session_id)
    now = time.monotonic()
    if hit and now - hit[0] < _PKG_CACHE_TTL_SECONDS:
        return hit[1]

    def _read():
        s3 = vm_manager._get_s3_client()
        bucket = vm_manager.get_artifacts_bucket()
        if not bucket:
            return None
        try:
            body = s3.get_object(Bucket=bucket, Key=f"sessions/{session_id}/requirements.txt")["Body"].read()
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            if code in ("NoSuchKey", "404", "NotFound"):
                return []
            raise
        text = body.decode("utf-8", errors="replace")
        return [s for s in (ln.strip() for ln in text.splitlines()) if s and not s.startswith("#")]

    pkgs = None
    try:
        if vm_manager is not None and session_id:
            pkgs = await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning(f"session relink: package list lookup failed for {session_id} ({type(e).__name__})")
        pkgs = None
    _pkg_cache[session_id] = (now, pkgs)
    return pkgs
