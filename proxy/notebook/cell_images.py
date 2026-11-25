"""
Chart (matplotlib PNG) persistence for notebook cells.

Part of: proxy.notebook

The VM returns a cell's chart as ``ExecutionResult.image`` — a
``data:image/png;base64,…`` string. The proxy (dag_service.capture_forwarded /
BatchCapture) calls :func:`prepare` on it, adds the deterministic, content-
addressed key to the response (``image_s3_key``) and :func:`schedule_upload`\\ s
the bytes in the background. The browser persists only the key
(``notebook_cells.image_s3_key``); the bytes live in the artifacts bucket:

    s3://<ARTIFACT_BUCKET>/notebook-outputs/<notebook_id>/<cell_segment>/<sha256>.png

Objects are private (no ACL argument, SSE-S3, no presigned URLs); they leave S3
only through the X-Proxy-Auth-gated GET /notebooks/{id}/cells/{cell}/image.

Off-loop rule: the proxy is one uvicorn event loop and boto3 clients are
synchronous, so every boto3 call here runs in ``asyncio.to_thread`` and every
storage read goes through ``astorage``. Only :func:`prepare` (pure CPU, sub-ms
for a ~30 KB PNG) runs on the loop.

Imports: module level is stdlib + aws_clients + _cell_fields only, so the
local_stop_app.sh db_helper can import this for
:func:`delete_notebook_prefix_sync` without the FastAPI/DAG stack. ``astorage``
and the VM manager (``dag_service``'s ``use_vm_manager`` registration) are
imported lazily inside the async functions.

No lifecycle expiry applies to notebook-outputs/: an object lives until its
cell stops referencing it (guarded orphan delete), its notebook is deleted
(prefix delete) or the stack is reset (local_install.sh --AuroraReset).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import time
from collections import OrderedDict

from proxy.platform import aws_clients
from proxy.storage._cell_fields import image_key, notebook_image_prefix

logger = logging.getLogger(__name__)

DATA_URL_PREFIX = "data:image/png;base64,"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
RECENT_PUT_GRACE_S = 600.0
RECENT_PUT_MAX = 4096
GET_PENDING_WAIT_S = 10.0
DELETE_BATCH = 1000


class ImageMissing(Exception):
    """The object does not exist (S3 NoSuchKey)."""


class ImageCorrupt(Exception):
    """The object is over-size or not a PNG."""


class BucketUnavailable(Exception):
    """No artifacts bucket could be resolved."""


# ─── Bucket / client ──────────────────────────────────────────────────────────

_UNRESOLVED = object()
_bucket_cache = _UNRESOLVED
_bucket_warned = False


def _warn_no_bucket() -> None:
    global _bucket_warned
    if not _bucket_warned:
        _bucket_warned = True
        logger.warning("cell images: no artifacts bucket (ARTIFACT_BUCKET unset and "
                       "discovery failed); charts are shown but not persisted")


def bucket_sync():
    """ARTIFACT_BUCKET from the environment only (local_stop_app.sh helper)."""
    return os.environ.get("ARTIFACT_BUCKET") or None


async def bucket():
    """ARTIFACT_BUCKET (exported by scripts/config.sh); when unset, a one-time
    fallback to the VM manager's discovery, run in a thread so STS never blocks
    the loop. The result is cached, including a miss (one WARNING)."""
    global _bucket_cache
    if _bucket_cache is not _UNRESOLVED:
        return _bucket_cache
    b = bucket_sync()
    if not b:
        try:
            from proxy.notebook import dag_service  # lazy: no import cycle
            mgr = dag_service._vm_manager
            if mgr is not None:
                b = await asyncio.to_thread(mgr.get_artifacts_bucket)
        except Exception as e:
            logger.debug("cell images: bucket discovery failed: %s", e)
            b = None
    _bucket_cache = b or None
    if not _bucket_cache:
        _warn_no_bucket()
    return _bucket_cache


def _s3():
    return aws_clients.get_client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _is_no_such_key(e: Exception) -> bool:
    if type(e).__name__ in ("NoSuchKey", "NotFound"):
        return True
    code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code")
    return code in ("NoSuchKey", "404", "NotFound")


# ─── prepare (on the loop; pure) ──────────────────────────────────────────────

def prepare(nid, cell_id, data_url):
    """(key, png) for a cell's chart data URL, or None (left unpersisted; the
    response is unchanged and the chart still displays)."""
    if not data_url:
        return None
    if not isinstance(data_url, str) or not data_url.startswith(DATA_URL_PREFIX):
        logger.debug("cell image: not a PNG data URL (nb=%s cell=%s)", nid, cell_id)
        return None
    b64 = data_url[len(DATA_URL_PREFIX):]
    # Cheap pre-check before decoding: base64 is 4/3 the decoded size.
    if len(b64) > (MAX_IMAGE_BYTES * 4) // 3 + 4:
        logger.warning("cell image: over %d bytes, not persisted (nb=%s cell=%s)",
                       MAX_IMAGE_BYTES, nid, cell_id)
        return None
    try:
        png = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        logger.debug("cell image: bad base64 (nb=%s cell=%s)", nid, cell_id)
        return None
    if len(png) > MAX_IMAGE_BYTES:
        logger.warning("cell image: over %d bytes, not persisted (nb=%s cell=%s)",
                       MAX_IMAGE_BYTES, nid, cell_id)
        return None
    if not png.startswith(PNG_MAGIC):
        logger.debug("cell image: not PNG bytes (nb=%s cell=%s)", nid, cell_id)
        return None
    try:
        key = image_key(nid, cell_id, hashlib.sha256(png).hexdigest())
    except (TypeError, ValueError):
        return None
    return key, png


def has_image_items(outputs) -> bool:
    """True when a VM `outputs` list holds at least one image item."""
    return isinstance(outputs, list) and any(
        isinstance(it, dict) and it.get("t") == "image" for it in outputs)


def prepare_outputs(nid, cell_id, outputs):
    """Persist every image item of a VM `outputs` list: set item["key"] (the
    content-addressed key) and schedule the upload, in place. A key the VM sent
    is never trusted: it is removed first, so an image that could not be
    prepared carries no key. Returns the LAST image's key (the legacy
    image_s3_key column), or None. Never raises."""
    last = None
    try:
        for it in outputs if isinstance(outputs, list) else ():
            if not isinstance(it, dict) or it.get("t") != "image":
                continue
            it.pop("key", None)
            prep = prepare(nid, cell_id, it.get("src"))
            if prep:
                it["key"] = prep[0]
                schedule_upload(*prep)
                last = prep[0]
    except Exception:
        logger.debug("cell image: outputs capture failed", exc_info=True)
    return last


# ─── Upload ───────────────────────────────────────────────────────────────────

_background: set = set()
_pending: dict = {}                     # key -> upload task (coalesces concurrent PUTs)
_recent_put: "OrderedDict[str, float]" = OrderedDict()   # key -> monotonic time of last PUT


def _spawn(coro):
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _record_put(key: str) -> None:
    _recent_put[key] = time.monotonic()
    _recent_put.move_to_end(key)
    while len(_recent_put) > RECENT_PUT_MAX:
        _recent_put.popitem(last=False)


async def _put(key: str, png: bytes) -> None:
    b = await bucket()
    if not b:
        return  # WARNING already logged once by bucket()
    await asyncio.to_thread(
        _s3().put_object, Bucket=b, Key=key, Body=png,
        ContentType="image/png", CacheControl="private, no-store",
        ServerSideEncryption="AES256",
    )
    _record_put(key)


async def _upload(key: str, png: bytes) -> None:
    try:
        await _put(key, png)
    except Exception as e:
        logger.warning("cell image upload failed key=%s: %s", key, type(e).__name__)
    finally:
        if _pending.get(key) is asyncio.current_task():
            _pending.pop(key, None)


def schedule_upload(key: str, png: bytes):
    """Background PUT of the chart (always PUTs — idempotent, ~30 KB); a second
    call while the same key is in flight reuses that task. Never raises."""
    t = _pending.get(key)
    if t is not None and not t.done():
        return t
    try:
        t = _spawn(_upload(key, png))
    except RuntimeError:
        return None  # no running loop
    _pending[key] = t
    return t


# ─── Read ─────────────────────────────────────────────────────────────────────

def _get_sync(b: str, key: str) -> bytes:
    try:
        obj = _s3().get_object(Bucket=b, Key=key)
    except Exception as e:
        if _is_no_such_key(e):
            raise ImageMissing(key) from None
        raise
    body = obj.get("Body")
    size = obj.get("ContentLength")
    if isinstance(size, int) and size > MAX_IMAGE_BYTES:
        if body is not None and hasattr(body, "close"):
            body.close()
        raise ImageCorrupt(f"{key}: {size} bytes")
    data = body.read(MAX_IMAGE_BYTES + 1) if body is not None else b""
    if len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG_MAGIC):
        raise ImageCorrupt(key)
    return data


async def get(key: str) -> bytes:
    """The chart PNG bytes. Waits (≤ 10 s, never cancelling it) for an
    in-flight upload of the same key first. Raises ImageMissing,
    ImageCorrupt, BucketUnavailable, or the S3 error."""
    t = _pending.get(key)
    if t is not None and not t.done():
        await asyncio.wait({t}, timeout=GET_PENDING_WAIT_S)   # never cancels t
    b = await bucket()
    if not b:
        raise BucketUnavailable()
    return await asyncio.to_thread(_get_sync, b, key)


# ─── Delete ───────────────────────────────────────────────────────────────────

def _delete_sync(b: str, keys: list) -> None:
    s3 = _s3()
    for i in range(0, len(keys), DELETE_BATCH):
        chunk = keys[i:i + DELETE_BATCH]
        s3.delete_objects(Bucket=b, Delete={"Objects": [{"Key": k} for k in chunk],
                                            "Quiet": True})


async def delete_keys(nid, keys) -> None:
    """Guarded orphan delete (runs in a background task). Re-reads the
    notebook's referenced keys right before deleting and skips any key that is
    referenced again, uploading, or was PUT within RECENT_PUT_GRACE_S. A skipped
    key is a bounded leak removed with the notebook / on reset. Never raises."""
    try:
        keys = {k for k in (keys or ()) if isinstance(k, str) and k}
        if not keys:
            return
        from proxy.storage import astorage  # lazy
        referenced = {k for ks in (await astorage.notebook_cell_image_keys_all(nid)).values()
                      for k in ks}
        now = time.monotonic()
        doomed = []
        for k in sorted(keys):
            if k in referenced:
                continue
            t = _pending.get(k)
            if t is not None and not t.done():
                continue
            put_at = _recent_put.get(k)
            if put_at is not None and now - put_at < RECENT_PUT_GRACE_S:
                continue
            doomed.append(k)
        if not doomed:
            return
        b = await bucket()
        if not b:
            return
        await asyncio.to_thread(_delete_sync, b, doomed)
        logger.debug("cell images: deleted %d orphan(s) nb=%s", len(doomed), nid)
    except Exception as e:
        logger.warning("cell images: orphan delete failed nb=%s: %s", nid, type(e).__name__)


def schedule_delete_keys(nid, keys):
    try:
        return _spawn(delete_keys(nid, keys))
    except RuntimeError:
        return None


async def drain_pending(prefix: str, timeout: float = 10.0) -> None:
    """Wait (≤ timeout, never cancelling) for in-flight uploads under prefix."""
    tasks = {t for k, t in list(_pending.items()) if k.startswith(prefix) and not t.done()}
    if not tasks:
        return
    _done, still = await asyncio.wait(tasks, timeout=timeout)
    if still:
        logger.warning("cell images: %d upload(s) under %s still running after %.0fs; "
                       "they may land after the prefix delete", len(still), prefix, timeout)


def delete_notebook_prefix_sync(nid, bucket_name=None) -> int:
    """List + delete every object under notebook-outputs/<nid>/ (paginated).
    Bucket from the argument or ARTIFACT_BUCKET only. Returns the number of
    objects deleted; raises on S3 errors / no bucket (callers decide)."""
    b = bucket_name or bucket_sync()
    if not b:
        raise BucketUnavailable()
    prefix = notebook_image_prefix(nid)
    s3 = _s3()
    keys = []
    token = None
    # List everything first, then delete (never mutate while paginating).
    while True:
        kw = {"Bucket": b, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in page.get("Contents") or []
                    if str(o.get("Key", "")).startswith(prefix))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    if keys:
        _delete_sync(b, keys)
    return len(keys)


async def delete_notebook_prefix(nid) -> None:
    """Best-effort delete of a deleted notebook's charts: drain its pending
    uploads (≤ 10 s), then list + delete off the loop. Never raises."""
    try:
        prefix = notebook_image_prefix(nid)
        await drain_pending(prefix)
        b = await bucket()
        if not b:
            return
        n = await asyncio.to_thread(delete_notebook_prefix_sync, nid, b)
        logger.debug("cell images: deleted %d object(s) under %s", n, prefix)
    except Exception as e:
        logger.warning("cell images: prefix delete failed nb=%s: %s", nid, type(e).__name__)
