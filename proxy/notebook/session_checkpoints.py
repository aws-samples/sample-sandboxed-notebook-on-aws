"""
S3 cleanup of a deleted notebook's session data (vm-card-regroup Fix B).

Part of: proxy.notebook

A notebook's sessions write S3 objects under these per-session prefixes:

    s3://<ARTIFACT_BUCKET>/sessions/<session_id>/     checkpoints (manifest, vars/,
                                                      files/, metadata.json, dag.json,
                                                      launch_catalog.json,
                                                      workbook-intel.json,
                                                      local-entities/, …)
    s3://<ARTIFACT_BUCKET>/attachments/<session_id>/  chat attachments
                                                      (POST /ai/attachments)
    s3://<ARTIFACT_BUCKET>/user-data/<session_id>/    attachments promoted to a
                                                      session data source
                                                      (DATASOURCE_S3_SESSION_PREFIX)

and, when the chat runs on a tab with no session id yet, AiChatPanel sends the
notebook id instead, so attachments can also land under

    s3://<ARTIFACT_BUCKET>/attachments/<notebook_id>/

(The notebook's chart images, notebook-outputs/<notebook_id>/, are deleted by
cell_images.delete_notebook_prefix.)

DELETE /notebooks/{id} and the local_stop_app.sh ``delete-notebooks`` fallback
both call storage.notebook_delete, which removes the DB rows of the sessions
the notebook OWNS but never touches S3. Both callers get the owned ids
(storage.notebook_owned_session_ids, BEFORE the delete: the same "no other
surviving notebook references it" rule) and, after the DB delete commits,
remove each owned session's prefixes and the notebook-id attachment prefix
here, best-effort (like the chart-image prefix delete in cell_images). Every
listing is paginated and deletes go in batches of ≤ 1000 keys.

Scope: ONLY the prefixes above, for owned sessions and this notebook id. A
session id must match SESSION_ID_RE (real ids are UUIDs) and a notebook id must
be a positive integer, so an empty, short, ``/``-, ``..``- or wildcard id can
never form a broader prefix. Never ``samples/``, the shared flat ``user-data/``
files, ``knowledge-base/``, ``models/``, ``sagemaker/``, ``unload/`` or another
session's or notebook's prefixes. ``user-data/<notebook_id>/`` is deliberately
NOT deleted: a short numeric folder there may be a user's own shared folder.

Imports: stdlib + cell_images + ai.constants only (stdlib + aws_clients at
module level), so the local_stop_app.sh db_helper can import the sync functions
without the FastAPI stack.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from proxy.notebook import cell_images
from proxy.notebook.ai.constants import ATTACHMENT_S3_PREFIX

logger = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_NOTEBOOK_ID_RE = re.compile(r"[1-9][0-9]{0,18}")


def session_checkpoint_prefix(session_id) -> str | None:
    """``sessions/<sid>/`` for a valid session id, else None (never a broader
    prefix)."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return None
    return f"sessions/{session_id}/"


def _session_source_prefix() -> str:
    """Where promoted attachments go (same env + default as the S3 data-source
    provider, proxy/platform/datasources/s3.py). '' disables that prefix."""
    return os.environ.get("DATASOURCE_S3_SESSION_PREFIX", "user-data/").strip()


def session_data_prefixes(session_id) -> list[str]:
    """Every S3 prefix keyed by this session id (see the module docstring), or
    [] for an invalid id."""
    ckpt = session_checkpoint_prefix(session_id)
    if ckpt is None:
        return []
    out = [ckpt, f"{ATTACHMENT_S3_PREFIX}/{session_id}/"]
    src = _session_source_prefix()
    if src:
        out.append(f"{src}{session_id}/")
    return out


def notebook_data_prefixes(notebook_id) -> list[str]:
    """``attachments/<nid>/`` for a positive integer notebook id, else []."""
    nid = str(notebook_id) if isinstance(notebook_id, (int, str)) else ""
    if isinstance(notebook_id, bool) or not _NOTEBOOK_ID_RE.fullmatch(nid):
        return []
    return [f"{ATTACHMENT_S3_PREFIX}/{nid}/"]


def _delete_prefix_sync(prefix: str, bucket_name: str, s3) -> int:
    """List + delete every object under `prefix` (paginated; list everything
    first, then delete in batches). Returns the number deleted."""
    keys: list = []
    token = None
    while True:
        kw = {"Bucket": bucket_name, "Prefix": prefix}
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
    for i in range(0, len(keys), cell_images.DELETE_BATCH):
        chunk = keys[i:i + cell_images.DELETE_BATCH]
        s3.delete_objects(Bucket=bucket_name,
                          Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
    return len(keys)


def _delete_prefixes_sync(prefixes, bucket_name, s3) -> int:
    """Delete each prefix; a failure on one does not stop the others. Raises the
    first error after trying all of them."""
    total, first_err = 0, None
    for p in prefixes:
        try:
            total += _delete_prefix_sync(p, bucket_name, s3)
        except Exception as e:
            first_err = first_err or e
            logger.warning("session data: prefix delete failed %s: %s", p, type(e).__name__)
    if first_err is not None:
        raise first_err
    return total


def delete_session_prefix_sync(session_id, bucket_name, s3=None) -> int:
    """List + delete every object under ``sessions/<sid>/`` only. Returns the
    number deleted. Raises ValueError for an invalid id (before any S3 call),
    cell_images.BucketUnavailable for no bucket, and S3 errors as-is."""
    prefix = session_checkpoint_prefix(session_id)
    if prefix is None:
        raise ValueError("invalid session id for a checkpoint prefix delete")
    if not bucket_name:
        raise cell_images.BucketUnavailable()
    return _delete_prefix_sync(prefix, bucket_name, s3 or cell_images._s3())


def delete_session_data_sync(session_id, bucket_name, s3=None) -> int:
    """Delete every prefix in session_data_prefixes(session_id). Returns the
    total deleted. Same raises as delete_session_prefix_sync (an S3 error is
    raised after the other prefixes were still tried)."""
    prefixes = session_data_prefixes(session_id)
    if not prefixes:
        raise ValueError("invalid session id for a session prefix delete")
    if not bucket_name:
        raise cell_images.BucketUnavailable()
    return _delete_prefixes_sync(prefixes, bucket_name, s3 or cell_images._s3())


def delete_notebook_data_sync(notebook_id, bucket_name, s3=None) -> int:
    """Delete every prefix in notebook_data_prefixes(notebook_id). Raises
    ValueError for an invalid id (before any S3 call), BucketUnavailable for no
    bucket, and S3 errors as-is."""
    prefixes = notebook_data_prefixes(notebook_id)
    if not prefixes:
        raise ValueError("invalid notebook id for a notebook prefix delete")
    if not bucket_name:
        raise cell_images.BucketUnavailable()
    return _delete_prefixes_sync(prefixes, bucket_name, s3 or cell_images._s3())


def delete_owned_session_prefixes_sync(session_ids, bucket_name, s3=None) -> dict:
    """Best-effort delete of each owned session's prefixes (all of
    session_data_prefixes). Returns {sid: deleted_count | "skipped" |
    "error: <Type>"}. Never raises."""
    out: dict = {}
    for sid in list(session_ids or []):
        if session_checkpoint_prefix(sid) is None:
            out[str(sid)] = "skipped"
            logger.warning("session data: skipped an invalid session id %r", sid)
            continue
        try:
            out[sid] = delete_session_data_sync(sid, bucket_name, s3=s3)
        except Exception as e:
            out[sid] = f"error: {type(e).__name__}"
            logger.warning("session data: prefix delete failed sid=%s: %s", sid, type(e).__name__)
    return out


async def delete_owned_session_prefixes(session_ids) -> None:
    """Async, best-effort wrapper for the proxy route (S3 off the loop).
    No bucket or no ids → no-op. Never raises."""
    try:
        ids = [s for s in (session_ids or []) if s]
        if not ids:
            return
        b = await cell_images.bucket()
        if not b:
            return
        res = await asyncio.to_thread(delete_owned_session_prefixes_sync, ids, b)
        logger.info("session data: deleted prefixes %s", res)
    except Exception as e:
        logger.warning("session data: cleanup failed: %s", type(e).__name__)


async def delete_notebook_data(notebook_id) -> None:
    """Async, best-effort delete of notebook_data_prefixes(notebook_id).
    No bucket or an invalid id → no-op. Never raises."""
    try:
        if not notebook_data_prefixes(notebook_id):
            return
        b = await cell_images.bucket()
        if not b:
            return
        n = await asyncio.to_thread(delete_notebook_data_sync, notebook_id, b)
        logger.info("session data: deleted %d object(s) for notebook %s", n, notebook_id)
    except Exception as e:
        logger.warning("session data: notebook cleanup failed nb=%s: %s",
                       notebook_id, type(e).__name__)
