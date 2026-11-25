"""
CheckpointManager — S3 checkpoint/restore for a MicroVM session.

Part of: app.platform.checkpoint (infrastructure layer)

Saves executor state (namespace + local files + packages + metadata) to S3 on
termination/suspend, and restores it when launching a new MicroVM from a session
checkpoint. Uses the DELTA layout (only changed objects re-uploaded per save).
Includes per-step timing for performance analysis.

S3 structure (DELTA layout — only changed objects re-uploaded per save):
  sessions/{session_id}/manifest.json        — var manifest {name: {hash,size,object}}
  sessions/{session_id}/vars/{name}.pkl      — one object per large variable
  sessions/{session_id}/vars/__small__.pkl   — batched small variables
  sessions/{session_id}/files_manifest.json  — file manifest {name: {size,mtime_ns,object}}
  sessions/{session_id}/files/{name}.gz      — one gzip object per large /tmp file
  sessions/{session_id}/files/__small__.tar.gz — batched small /tmp files
  sessions/{session_id}/requirements.txt     — runtime-installed packages
  sessions/{session_id}/metadata.json        — session info
  sessions/{session_id}/dag.json             — notebook DAG doc + runtime section
                                               {clock, vars, runs} + checkpoint id
  sessions/{session_id}/imports.json         — import ledger (replayed on restore)

dag.json replaces provenance.json. An old provenance.json may still exist in S3;
it is only READ, as a fallback when dag.json is absent (it is never written).
metadata.json carries two sizes: checkpoint_total_kb = the WHOLE saved state a
restore brings back (every var pickle + every tracked /tmp file + the JSON tail,
logical/uncompressed bytes); checkpoint_size_kb = only the var-pickle bytes
uploaded by THIS save (a delta; kept for compatibility, not the checkpoint size).
"""

import os
import io
import tarfile
import glob
import json
import subprocess
import logging
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import botocore.config

from . import nspickle
from .archive import _safe_join, _safe_extractall
from .deadline import SaveCancelled, check as _deadline_check
from .namespace_filter import (
    _drop_injected_from_restored,
    _filter_namespace_for_save,
    _sanitize_restored_namespace,
)
from app.notebook.dag_contract import DAG_MAX_BYTES, DAG_SCHEMA
from .delta import (
    DEFAULT_SPOOL_DIR,
    DELTA_SMALL_VAR_BYTES,
    DELTA_SMALL_FILE_BYTES,
    build_var_blobs,
    disk_free_bytes,
    mem_available_bytes,
    not_saved_kind,
    reference_order,
    release_blobs,
    _hash_bytes,
    file_signature,
)

logger = logging.getLogger(__name__)

RESTORE_MAX_WORKERS = 16

# Managed upload for a spooled var pickle: multipart from 16 MiB, 16 MiB parts,
# 8 parts in flight (about 128 MiB of buffers), read from the spool file.
_SPOOL_UPLOAD_CHUNK = 16 * 1024 * 1024
_SPOOL_UPLOAD_MULTIPART_THRESHOLD = _SPOOL_UPLOAD_CHUNK
_SPOOL_UPLOAD_CONCURRENCY = 8

# Ranged parallel GET for a large var object at restore: from 64 MiB, 16 MiB
# ranges, 8 in flight per object, read in 1 MiB chunks into one preallocated
# buffer. _RANGED_SLOTS caps the range requests in flight across the process:
# with the RESTORE_MAX_WORKERS outer GETs that stays within the 32 connections
# of _CHECKPOINT_S3_CONFIG.
_RANGED_GET_THRESHOLD = 64 * 1024 * 1024
_RANGED_GET_PART = 16 * 1024 * 1024
_RANGED_GET_CONCURRENCY = 8
_RANGED_READ_CHUNK = 1024 * 1024
_RANGED_PART_ATTEMPTS = 3
_RANGED_PART_BACKOFF = (0.2, 0.5)
_RANGED_SLOTS = threading.BoundedSemaphore(16)


class _RangedGetFailed(Exception):
    """A ranged download could not produce the object's bytes (the caller
    falls back to a single GET)."""


def _split_ranges(size: int, part: int) -> list:
    """[(start, end_inclusive)] covering 0..size-1 once, in order."""
    return [(s, min(s + part, size) - 1) for s in range(0, size, part)]


def _ranged_get(s3, bucket: str, key: str, size: int, expected_hash=None) -> bytearray:
    """Download `key` (manifest size `size`) with parallel ranged GETs into one
    preallocated bytearray. Part 0 runs first: its ContentRange total must equal
    `size` and its ETag pins the later parts (IfMatch). Each part is checked
    against its ContentRange and byte count and retried (up to
    _RANGED_PART_ATTEMPTS); parts are hashed in order while the others still
    download, and the sha256 must equal `expected_hash` when given. Raises
    _RangedGetFailed (or the client's error) on any failure."""
    import hashlib
    import time as _time

    ranges = _split_ranges(size, _RANGED_GET_PART)
    if not ranges:
        raise _RangedGetFailed("empty object")
    buf = bytearray(size)
    view = memoryview(buf)
    sha = hashlib.sha256()
    lock = threading.Lock()
    state = {"next": 0, "hashing": False}
    done: set = set()
    etag = None

    def _hash_ready(i):
        # One thread hashes at a time, in part order; the others go back to
        # downloading instead of waiting on the lock.
        with lock:
            done.add(i)
            if state["hashing"]:
                return
            state["hashing"] = True
        while True:
            with lock:
                j = state["next"]
                if j not in done:
                    state["hashing"] = False
                    return
                state["next"] = j + 1
            s, e = ranges[j]
            sha.update(view[s:e + 1])

    def _content_range(resp, start, end):
        cr = resp.get("ContentRange") or ""
        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr.strip())
        if not m:
            raise _RangedGetFailed("no ContentRange")
        if int(m.group(3)) != size:
            raise _RangedGetFailed("object size differs from the manifest")
        if int(m.group(1)) != start or int(m.group(2)) != end:
            raise _RangedGetFailed("ContentRange differs from the requested range")

    def _fetch(i):
        nonlocal etag
        start, end = ranges[i]
        stop = end + 1
        for attempt in range(_RANGED_PART_ATTEMPTS):
            if attempt:
                _time.sleep(_RANGED_PART_BACKOFF[min(attempt - 1, len(_RANGED_PART_BACKOFF) - 1)])
            try:
                with _RANGED_SLOTS:
                    kw = {"Bucket": bucket, "Key": key, "Range": f"bytes={start}-{end}"}
                    if i:
                        kw["IfMatch"] = etag
                    resp = s3.get_object(**kw)
                    _content_range(resp, start, end)
                    if i == 0:
                        etag = resp.get("ETag")
                        if not etag:
                            raise _RangedGetFailed("no ETag")
                    body = resp["Body"]
                    off = start
                    while off < stop:
                        chunk = body.read(min(_RANGED_READ_CHUNK, stop - off))
                        if not chunk:
                            break
                        n = len(chunk)
                        if n > stop - off:
                            raise _RangedGetFailed("part longer than requested")
                        view[off:off + n] = chunk
                        off += n
                    del chunk
                    if off != stop:
                        raise EOFError("short part")
                break
            except (_RangedGetFailed, TypeError):
                raise   # not transient (a mismatch, or a client without Range)
            except Exception:
                if attempt + 1 >= _RANGED_PART_ATTEMPTS:
                    raise
        _hash_ready(i)

    try:
        _fetch(0)
        if len(ranges) > 1:
            ex = ThreadPoolExecutor(max_workers=_RANGED_GET_CONCURRENCY)
            try:
                for f in [ex.submit(_fetch, i) for i in range(1, len(ranges))]:
                    f.result()
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        if state["next"] != len(ranges):
            raise _RangedGetFailed("not every part was hashed")
        if expected_hash and sha.hexdigest() != expected_hash:
            raise _RangedGetFailed("hash differs from the manifest")
    finally:
        view.release()
    return buf


def _spool_dir() -> str:
    """Directory for streamed var pickles (CHECKPOINT_SPOOL_DIR, default
    /tmp/.checkpoint-spool: /tmp is on disk in the VM, not in RAM)."""
    return os.environ.get("CHECKPOINT_SPOOL_DIR") or DEFAULT_SPOOL_DIR


def _large_object_key(name: str, committed: str | None) -> str:
    """Key suffix for a CHANGED large var: whichever of vars/{name}.pkl and
    vars/{name}.alt.pkl the committed manifest does not reference, so an upload
    never overwrites an object the previous checkpoint still needs. (A variable
    name has no dot, so neither key can collide with another variable's key.)"""
    primary = f"vars/{name}.pkl"
    return f"vars/{name}.alt.pkl" if committed == primary else primary


def _put_blob(s3, bucket: str, key: str, blob: dict, deadline=None) -> None:
    """Upload one var blob: bytes with put_object, a spool file of at least
    _SPOOL_UPLOAD_MULTIPART_THRESHOLD with an explicit multipart upload
    (_multipart_upload: deadline checked before every part, aborted on any
    failure), a smaller one with put_object. A client without
    create_multipart_upload (a test double) gets the managed upload_file when
    it has it, else the file's bytes through put_object."""
    _deadline_check(deadline, "upload")
    path = blob.get("path")
    if not path:
        s3.put_object(Bucket=bucket, Key=key, Body=blob["bytes"])
        return
    size = blob.get("size")
    if size is None:
        size = os.path.getsize(path)
    if hasattr(s3, "create_multipart_upload"):
        if size >= _SPOOL_UPLOAD_MULTIPART_THRESHOLD:
            _multipart_upload(s3, bucket, key, path, size, deadline)
        else:
            with open(path, "rb") as f:
                s3.put_object(Bucket=bucket, Key=key, Body=f.read())
        return
    if hasattr(s3, "upload_file"):
        from boto3.s3.transfer import TransferConfig
        s3.upload_file(path, bucket, key, Config=TransferConfig(
            multipart_threshold=_SPOOL_UPLOAD_MULTIPART_THRESHOLD,
            multipart_chunksize=_SPOOL_UPLOAD_CHUNK,
            max_concurrency=_SPOOL_UPLOAD_CONCURRENCY))
        return
    with open(path, "rb") as f:
        s3.put_object(Bucket=bucket, Key=key, Body=f.read())


def _multipart_upload(s3, bucket: str, key: str, path: str, size: int, deadline=None) -> None:
    """Multipart upload of a spool file: parts of _SPOOL_UPLOAD_CHUNK, at most
    _SPOOL_UPLOAD_CONCURRENCY in flight (so at most that many part buffers),
    the deadline checked before each part. Any failure or cancel aborts the
    upload (best effort, logged) and re-raises, so no part is left billed and
    nothing is written at `key`."""
    from concurrent.futures import FIRST_COMPLETED, wait
    upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    n_parts = max(1, -(-size // _SPOOL_UPLOAD_CHUNK))

    def _part(num: int) -> dict:
        _deadline_check(deadline, "upload")
        with open(path, "rb") as f:
            f.seek((num - 1) * _SPOOL_UPLOAD_CHUNK)
            body = f.read(_SPOOL_UPLOAD_CHUNK)
        resp = s3.upload_part(Bucket=bucket, Key=key, PartNumber=num, UploadId=upload_id, Body=body)
        return {"PartNumber": num, "ETag": resp["ETag"]}

    try:
        parts: list = []
        with ThreadPoolExecutor(max_workers=_SPOOL_UPLOAD_CONCURRENCY) as ex:
            pending: set = set()
            try:
                for num in range(1, n_parts + 1):
                    _deadline_check(deadline, "upload")
                    if len(pending) >= _SPOOL_UPLOAD_CONCURRENCY:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        parts.extend(fut.result() for fut in done)
                    pending.add(ex.submit(_part, num))
                for fut in pending:
                    parts.append(fut.result())
            except BaseException:
                for fut in pending:
                    fut.cancel()
                raise
        _deadline_check(deadline, "upload")
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])})
    except BaseException as e:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            logger.warning(f"   Delta: aborted the multipart upload of {key} ({type(e).__name__})")
        except Exception as ae:
            logger.warning(f"   Delta: could not abort the multipart upload of {key}: {ae}")
        raise

# vars/__small__.pkl layouts:
#   1 (older saves): dill dict {name: live object}.
#   2 (current):     dill dict {_SMALL_FORMAT_KEY: 2, "vars": {name: per-var pickle
#                    bytes}}. Holds only bytes, so loading the batch itself never
#                    runs user code; each var is unpickled on its own at restore.
SMALL_BATCH_FORMAT = 2
_SMALL_FORMAT_KEY = "__small_format__"


def _encode_small_batch(var_bytes: dict) -> bytes:
    """Format-2 small batch from {name: already-pickled bytes} (no round trip)."""
    import dill
    return dill.dumps({_SMALL_FORMAT_KEY: SMALL_BATCH_FORMAT, "vars": dict(var_bytes)})


def _decode_small_batch(b: bytes) -> tuple:
    """(format, {name: value}) for either layout: format 1 -> live objects,
    format 2 -> per-var pickle bytes (the caller loads each one)."""
    import dill
    doc = dill.loads(b)
    if isinstance(doc, dict) and doc.get(_SMALL_FORMAT_KEY) == SMALL_BATCH_FORMAT \
            and isinstance(doc.get("vars"), dict):
        return SMALL_BATCH_FORMAT, doc["vars"]
    return 1, doc

# ─── Shared checkpoint S3 client ──────────────────────────────────────────────
# ONE S3 client for every checkpoint path (save, restore, flush_packages,
# flush_data_catalog, the /rebind pre-warm), so connections + resolved creds are
# reused instead of re-built per call (boto3.client() is ~tens of ms and each new
# client re-dials TLS). Config (moved from restore): a 32-slot connection pool for
# the 16-way parallel GETs/PUTs, plus explicit connect/read timeouts + adaptive
# retries: on a FRESHLY-RESUMED pool VM the first S3 call pays a cold
# TLS/credential handshake that occasionally STALLS for many seconds (we observed
# a 20 KB object taking ~19s — a hung connection, not transfer time). Without a
# connect/read timeout botocore would wait indefinitely on that stall; with one,
# the hung attempt is cut and RETRIED on a fresh connection ("adaptive" also backs
# off on throttling), so a cold-connection hiccup costs a few seconds of retry
# instead of a ~19s hang.
_CHECKPOINT_S3_CONFIG = botocore.config.Config(
    max_pool_connections=32,
    connect_timeout=5,
    read_timeout=15,
    retries={"max_attempts": 4, "mode": "adaptive"},
    # TCP keepalive: the VM's egress path (NAT/connector) silently drops a TCP
    # connection idle past its timeout; boto3 then reuses the DEAD socket and
    # hangs until read_timeout before retrying — the documented cause of a
    # first-GET stall (langchain-aws#502/#819, botocore#3140). Keepalive probes
    # keep the socket alive so it's never reused dead. (On the suspend/resume path
    # the frozen socket can still be dead on thaw — that's covered by the short
    # read_timeout + retry above re-dialing a fresh connection — but keepalive
    # fixes the plain-idle backfill-spare case outright.)
    tcp_keepalive=True,
)
_ckpt_lock = threading.Lock()
_ckpt_client = None
_ckpt_creator = None  # the boto3.client callable that built _ckpt_client


_IMPORT_LINE_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.]+))")


def _import_already_loaded(line: str, loaded=None):
    """For the restore timing log: is what this import-ledger line loads ALREADY in
    memory (preloaded in the image snapshot)? Returns True/False, or None if the line
    isn't a recognisable import. Logging only — never raises.
      'import pandas as pd'       -> 'pandas' in sys.modules
      'import plotly.express'     -> 'plotly.express' in sys.modules
      'from scipy import stats'   -> 'scipy.stats' in sys.modules, or `stats` already
                                     an attribute of the loaded scipy (a plain name);
                                     a lazy submodule not yet imported counts as cold.
    `loaded` (a frozenset of module names taken at restore() start) judges against
    that snapshot instead of the live sys.modules, so a module the var unpickle
    imported moments earlier is reported as cold, not preloaded."""
    try:
        m = _IMPORT_LINE_RE.match(line or "")
        if not m:
            return None
        mods = sys.modules if loaded is None else loaded
        if m.group(2):
            return m.group(2) in mods
        pkg = m.group(1)
        nm = re.match(r"^\s*from\s+[\w.]+\s+import\s+\(?\s*(\w+)", line)
        name = nm.group(1) if nm else ""
        if loaded is not None:
            sub = f"{pkg}.{name}"
            if sub in sys.modules:
                return sub in loaded
            mod = sys.modules.get(pkg)
            return pkg in loaded and mod is not None and name in vars(mod)
        if f"{pkg}.{name}" in sys.modules:
            return True
        mod = sys.modules.get(pkg)
        return mod is not None and name in vars(mod)
    except Exception:
        return None


def get_checkpoint_s3_client():
    """Return the process-wide checkpoint S3 client, building it on first use.

    The client caches credentials, so app.platform.hooks._reset_boto3_credentials()
    drops it (reset_checkpoint_s3_client) at /run, /resume and /rebind — a client
    built under the image-build role never survives into a session. It is also
    rebuilt if boto3.client itself was replaced (test seams monkeypatch it)."""
    global _ckpt_client, _ckpt_creator
    import boto3
    creator = boto3.client
    client = _ckpt_client
    if client is not None and _ckpt_creator is creator:
        return client
    with _ckpt_lock:
        if _ckpt_client is None or _ckpt_creator is not boto3.client:
            _ckpt_creator = boto3.client
            _ckpt_client = boto3.client("s3", config=_CHECKPOINT_S3_CONFIG)
        return _ckpt_client


def reset_checkpoint_s3_client():
    """Drop the shared checkpoint S3 client (next use rebuilds it with fresh creds)."""
    global _ckpt_client, _ckpt_creator
    with _ckpt_lock:
        _ckpt_client = None
        _ckpt_creator = None


def _user_variable_totals(namespace) -> tuple[int | None, int | None]:
    """(count, shallow bytes) of USER variables, with the exact filter GET
    /vm-status uses (app.notebook.vm_status.user_variable_totals), so the card
    shows the same number for a suspended VM as for a running one. Imported
    lazily (vm_status pulls in fastapi); (None, None) if unavailable."""
    try:
        from app.notebook.vm_status import user_variable_totals
        return user_variable_totals(namespace)
    except Exception:
        return None, None


def _scan_tmp_checkpoint_files() -> list[str]:
    """De-duplicated list of real /tmp files the checkpoint tracks, using the
    SINGLE source-of-truth glob set from data_catalog.local_tmp_file_globs() — so
    what the Data Sources panel SHOWS and what a restore BRINGS BACK can't drift.
    (data files recursive under /tmp; model artifacts at /tmp top-level +
    /tmp/models/.) De-dup matters: a top-level model matches more than one glob,
    and recursive data globs overlap. Order-stable, best-effort (never raises).

    data_catalog is imported lazily (not at module load): the restore path already
    imports it lazily too (step 4), and a top-level import would risk a cycle."""
    try:
        from app.notebook.data_catalog import local_tmp_file_globs
        patterns = local_tmp_file_globs()
    except Exception:
        # Fail-safe fallback mirroring the shared globs, so a checkpoint still
        # captures /tmp files even if the catalog module can't be imported.
        _d = ('*.csv', '*.parquet', '*.json', '*.xlsx', '*.xls', '*.txt')
        _m = ('*.joblib', '*.jbl', '*.pkl', '*.pickle', '*.sav', '*.onnx')
        patterns = ([f'/tmp/**/{e}' for e in _d]
                    + [f'/tmp/{e}' for e in _m]
                    + [f'/tmp/models/**/{e}' for e in _m])
    seen: dict[str, None] = {}
    for pattern in patterns:
        try:
            for fp in glob.glob(pattern, recursive=True):
                if os.path.isfile(fp):
                    seen.setdefault(fp, None)
        except Exception:
            continue
    return list(seen.keys())


class CheckpointManager:
    """
    Manages S3 checkpoint/restore for a MicroVM session.

    Holds references to the executor and session_state so they don't
    need to be passed to every method call.
    """

    def __init__(self, executor, session_state: dict, *, skip_display_objects: bool = True, data_catalog=None,
                 dag_store=None):
        self._executor = executor
        self._session_state = session_state
        self._data_catalog = data_catalog
        # app.notebook.dag_store.DagStore (proxy-pushed DAG doc) or None (tests,
        # older wiring): saved into dag.json and set on restore.
        self._dag_store = dag_store
        self._bucket: str | None = None
        self._skip_display_objects = skip_display_objects
        self._user_installed_packages: list[str] = []  # Packages installed via /install endpoint
        self.last_save_timings: dict = {}
        self.last_save_sizes: dict = {}    # bytes written to S3 per step (last save)
        self.last_restore_timings: dict = {}
        # Package re-install outcome of the last restore (served at GET /restore-report):
        # {verified, arch, python, packages_total, packages_failed: [{name, reason}]}
        # or {verified: False, reason}. None until a restore ran on this VM.
        self.last_restore_report: dict | None = None
        self._pkg_result: dict = {}
        self.last_save_summary: dict = {}  # counts/size/timing of the last save (for hook logging)
        # Variables the last save attempt left out: [{name, reason, kind}], kind
        # "memory" / "disk" (would not fit) or "unpicklable".
        self.last_not_saved: list = []
        # Pre-flight probes for the streaming save (seams for tests).
        self._mem_available = mem_available_bytes
        self._disk_free = disk_free_bytes
        self.last_checkpoint_at: float = 0  # Unix timestamp of last successful save
        # Real restored-namespace count + pkl KB from the last restore (for the
        # greppable RESTORE summary line). "?" until a restore with a manifest runs.
        self._last_restore_var_count = "?"
        self._last_restore_pkl_kb = "?"
        # Names of vars the last restore skipped because their pickle wouldn't load.
        self._last_restore_skipped_vars: list = []
        # The same, with the reason: [{name, reason}] (added to last_restore_report
        # as "vars_not_restored").
        self.last_restore_not_restored: list = []
        # Restored vars that embed a referenced class or function by value (a
        # reference cycle broken at save time): [{name, by_value: [names]}]
        # (added to last_restore_report as "refs_by_value").
        self.last_restore_by_value: list = []
        # One save at a time: a suspend save that outlived its hook keeps the lock
        # until it reaches its next cancellation point and aborts.
        self._save_lock = threading.Lock()
        # Outcome of the last /suspend hook save (set by app.platform.hooks):
        # {status: ok|skipped|timed_out|failed, reason, duration_s, bytes,
        #  saved_at, budget_s, committed}. None until a suspend ran on this VM.
        self.last_suspend_save: dict | None = None
        # Progress of the running/last save: {phase, uploaded_bytes, committed}
        # (committed: manifest.json of this save was written).
        self.last_save_progress: dict = {}
        # Dirty tracking: fingerprint of the state captured at the last successful
        # save. is_dirty() compares the current fingerprint against this so a
        # suspend with no changes since the last checkpoint skips the S3 write.
        # None means "never saved" → always dirty.
        self._saved_fingerprint: tuple | None = None
        # Delta checkpoint: the manifest {name: {"hash","size","object"}} of the
        # last successful save, so the next save only re-uploads changed vars.
        # In-memory cache; the authoritative copy is manifest.json in S3 (we reload
        # it if empty, e.g. after a proxy/VM restart).
        self._saved_manifest: dict = {}
        # Delta file save: manifest {name: {"size","mtime_ns","object"}} of the last
        # save, so only changed/new /tmp files are re-uploaded (rest are skipped).
        self._saved_file_manifest: dict = {}
        # The S3 prefix each in-memory manifest above was written to / restored
        # from. When the next save targets the SAME prefix, our own last-written
        # manifest is used instead of re-GETting it from S3 (see _load_manifest).
        self._saved_manifest_prefix: str | None = None
        self._saved_file_manifest_prefix: str | None = None

    def record_package_install(self, package: str):
        """Track a user-installed package (called by /install on success). Keyed by
        base name so a re-install/upgrade replaces the prior pin instead of adding a
        duplicate (which would otherwise install two conflicting specs on restore)."""
        if not package:
            return
        base = self._pkg_base(package)
        self._user_installed_packages = [p for p in self._user_installed_packages if self._pkg_base(p) != base]
        self._user_installed_packages.append(package)
        logger.info(f"   Recorded user package install: {package} (total: {len(self._user_installed_packages)})")

    def record_package_uninstall(self, package: str):
        """Drop a package from the tracked list (called by /install uninstall on
        success) so an uninstalled package doesn't reappear after rotation/restore."""
        if not package:
            return
        base = self._pkg_base(package)
        before = len(self._user_installed_packages)
        self._user_installed_packages = [p for p in self._user_installed_packages if self._pkg_base(p) != base]
        if len(self._user_installed_packages) != before:
            logger.info(f"   Recorded user package uninstall: {package} (total: {len(self._user_installed_packages)})")

    def flush_packages(self) -> bool:
        """Immediately persist ONLY the user-installed package list to S3
        (sessions/{session_id}/requirements.txt).

        Called right after a successful /install or /uninstall so the (tiny)
        package list is durable the instant it changes — independent of the heavy,
        interruptible namespace checkpoint. Without this, a package could be lost:
        the namespace suspend-checkpoint that would have captured it can be frozen
        mid-upload (it writes requirements.txt only as a late step, after megabytes
        of pickles), leaving restore to fall back to an earlier checkpoint that
        predates the install. This write is a few bytes, so it completes fast.

        Best-effort: returns True on success, False otherwise (never raises — a
        failed flush must not break the install response). No-op if checkpointing
        isn't enabled or there's no session yet.
        """
        if not self._session_state.get("checkpoint_enabled"):
            return False
        session_id = self._session_state.get("session_id")
        if not session_id:
            return False
        try:
            s3 = get_checkpoint_s3_client()
            prefix = f"sessions/{session_id}"
            key = f"{prefix}/requirements.txt"
            if self._user_installed_packages:
                body = '\n'.join(self._user_installed_packages)
                s3.put_object(Bucket=self.bucket, Key=key, Body=body)
                logger.info(f"   📦 Flushed {len(self._user_installed_packages)} package(s) to s3://{self.bucket}/{key}")
            else:
                # Last package was uninstalled — remove the object so restore
                # doesn't reinstall a now-removed package.
                try:
                    s3.delete_object(Bucket=self.bucket, Key=key)
                except Exception:
                    pass
                logger.info(f"   📦 Package list now empty — removed {key}")
            return True
        except Exception as e:
            logger.warning(f"   Package flush failed (will retry on next checkpoint): {e}")
            return False

    def flush_data_catalog(self) -> bool:
        """Immediately persist the discovered data catalog to S3
        (sessions/{session_id}/data_catalog.json).

        Wired to DataCatalog's discovery-complete callback so the schemas land the
        moment background discovery finishes — independent of the heavy, interruptible
        namespace checkpoint. Losing the catalog isn't a correctness bug (the new VM
        can re-discover), but re-discovery is slow and costs S3/DynamoDB/Glue calls;
        a mid-suspend freeze before the checkpoint's (late) catalog step would force
        that. This write is small, so it completes fast.

        Best-effort: returns True on success, False otherwise (never raises — a
        failed flush just means the catalog rides along in the next checkpoint).
        No-op if checkpointing isn't enabled, there's no session, or discovery
        hasn't completed.
        """
        if not self._session_state.get("checkpoint_enabled"):
            return False
        session_id = self._session_state.get("session_id")
        if not session_id:
            return False
        if not (self._data_catalog and self._data_catalog.discovery_complete):
            return False
        try:
            s3 = get_checkpoint_s3_client()
            catalog_data = json.dumps(self._data_catalog.get_all())
            s3.put_object(
                Bucket=self.bucket,
                Key=f"sessions/{session_id}/data_catalog.json",
                Body=catalog_data,
                ContentType="application/json",
            )
            logger.info(f"   📊 Flushed data catalog to s3://{self.bucket}/sessions/{session_id}/data_catalog.json ({len(catalog_data)} B)")
            return True
        except Exception as e:
            logger.warning(f"   Data catalog flush failed (will retry on next checkpoint): {e}")
            return False

    @staticmethod
    def _pkg_base(spec: str) -> str:
        """Base distribution name from a spec like 'pandas==2.0' or 'reqs[all]>=1'."""
        import re as _re
        return _re.split(r'[<>=!~\[]', (spec or "").strip(), maxsplit=1)[0].strip().lower()

    def _current_fingerprint(self) -> tuple:
        """A cheap fingerprint of everything a checkpoint would persist.

        - executor._prov_clock: a monotonic counter that advances on EVERY
          variable create/modify/delete and SQL write (see executor). This is
          our proxy for "namespace changed since last save".
        - the sorted user-installed package list: covers /install + uninstall,
          which don't touch the provenance clock.

        Comparing this tuple to the fingerprint captured at the last successful
        save tells us whether a re-save is warranted. It is intentionally cheap
        (no serialization) so it is safe to call on the /suspend hot path.
        """
        prov_clock = getattr(self._executor, "_prov_clock", None)
        pkgs = tuple(sorted(self._user_installed_packages))
        # Also fold in the /tmp data-file set. The provenance clock only tracks
        # namespace mutations, so a file-only change — crucially a file DELETION,
        # or an upload that doesn't touch any variable — would otherwise leave the
        # fingerprint unchanged and is_dirty() would skip the save, meaning the S3
        # object for a deleted file would never be removed. Signature is cheap
        # (os.stat only, no reads): sorted (name, size, mtime_ns).
        files_sig = self._tmp_files_signature()
        return (prov_clock, pkgs, files_sig)

    def _tmp_files_signature(self) -> tuple:
        """Cheap signature of the checkpointed /tmp file set: sorted
        (relpath, size, mtime_ns). Used by the dirty-flag so file additions/
        changes/DELETIONS trigger a save — INCLUDING model artifacts and nested
        files, matching exactly what save() persists (shared scan). Keyed by path
        relative to /tmp (not basename) so a change to /tmp/models/x.joblib is a
        distinct entry from /tmp/x.joblib. os.stat only — no reads. Never raises."""
        sig = []
        try:
            for fp in _scan_tmp_checkpoint_files():
                try:
                    st = os.stat(fp)
                    sig.append((os.path.relpath(fp, "/tmp"), st.st_size, st.st_mtime_ns))
                except OSError:
                    continue
        except Exception:
            return ()
        return tuple(sorted(sig))

    def is_dirty(self) -> bool:
        """True if state changed since the last successful save (or never saved).

        NOTE: this is a conservative proxy. It can over-report (e.g. a cell that
        only read a variable still bumps the clock) — that just causes a
        harmless extra save. It should not under-report for normal notebook use,
        because every namespace mutation path advances _prov_clock. If the
        executor doesn't expose a clock for some reason, we treat as dirty.
        """
        if self._saved_fingerprint is None:
            return True
        if getattr(self._executor, "_prov_clock", None) is None:
            return True
        return self._current_fingerprint() != self._saved_fingerprint

    @property
    def bucket(self) -> str:
        """Get the S3 bucket name (from session config or constructed from account)."""
        if self._bucket:
            return self._bucket

        # Use bucket name passed from the proxy via runHookPayload
        if self._session_state.get("artifacts_bucket"):
            self._bucket = self._session_state["artifacts_bucket"]
            return self._bucket

        # Fallback: construct from account ID + region
        import boto3
        region = os.environ.get("AWS_REGION", "us-west-2")
        sts = boto3.client("sts")
        account_id = sts.get_caller_identity()["Account"]
        self._bucket = f"microvm-sandbox-artifacts-{account_id}-{region}"
        return self._bucket

    # ─── Delta namespace save (Option A) ──────────────────────────────────────

    def _load_manifest(self, s3, prefix: str) -> tuple:
        """Load the last-saved var manifest from S3 (authoritative), falling back
        to the in-memory cache. Returns (vars_dict, small_batch_hash). vars_dict is
        {} if none (→ first delta save uploads all).

        At most ONE S3 GET per save: if the in-memory manifest was written to (or
        restored from) this SAME prefix, it is returned without a GET. That is no
        new hazard — concurrent writers to one session prefix were never safe
        (read → upload → manifest-write is not atomic), and a save to a DIFFERENT
        prefix (e.g. /checkpoint-save with an override session_id) still reads S3."""
        if self._saved_manifest and self._saved_manifest_prefix == prefix:
            mem = {k: v for k, v in self._saved_manifest.items() if not k.startswith("__")}
            return mem, self._saved_manifest.get("__small_hash__")
        try:
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/manifest.json")
            man = json.loads(resp["Body"].read().decode("utf-8"))
            if isinstance(man, dict) and "vars" in man:
                return man["vars"], man.get("__small_hash__")
        except s3.exceptions.NoSuchKey:
            pass
        except Exception as e:
            logger.warning(f"   Delta: manifest load warning ({e}) — treating as first save (upload all)")
        # In-memory fallback (e.g. same manager across saves without re-reading S3).
        if self._saved_manifest:
            mem = {k: v for k, v in self._saved_manifest.items() if not k.startswith("__")}
            return mem, self._saved_manifest.get("__small_hash__")
        return {}, None

    def _save_namespace_delta(self, s3, prefix, namespace_to_save, step_timings, step_sizes,
                              *, checkpoint_id: str | None = None, deadline=None) -> int:
        """Per-variable delta save. Uploads only changed var objects + a manifest.

        Layout under sessions/{id}/:
          vars/{name}.pkl        — one object per LARGE var (> DELTA_SMALL_VAR_BYTES);
                                   a changed var alternates with vars/{name}.alt.pkl
                                   (see _large_object_key)
          vars/__small__.pkl     — ONE object batching all SMALL vars' pickle bytes
                                   (format 2, see _encode_small_batch)
          manifest.json          — {"vars": {name: {"hash","size","object"}},
                                    "not_saved": {name: {"reason","kind"[,"kept_previous"]}}, ...}
                                   A var skipped for memory/disk keeps its previous
                                   "vars" entry and object (kept_previous: true).

        checkpoint_id (keyword-only): when given it is written into manifest.json
        as "checkpoint_id" so restore can tell whether dag.json belongs to the
        same save (additive key; older readers ignore it).

        deadline (keyword-only, the suspend save): a SaveDeadline checked while
        pickling and before every upload and the manifest put; SaveCancelled
        propagates and manifest.json is not written.

        Returns the number of bytes actually uploaded to S3 this save (the delta).
        """
        import time as _time
        t0 = _time.perf_counter()

        # 1. Pickle each var independently + hash, streaming every large pickle to
        #    a spool file (build_var_blobs(spool_dir=...)). Vars that can't be
        #    pickled solo, or would not fit in memory or on disk, are skipped and
        #    recorded as not saved; everything else is still saved.
        #    Functions defined in cells are bound to the LIVE executor namespace
        #    (not the filtered copy), so that dict is the one written as the
        #    notebook-namespace marker (see nspickle).
        #    The previous manifest is read first: a shared value keeps the
        #    canonical name it had there (see nspickle._value_owner).
        prev_loaded = self._load_manifest(s3, prefix)
        blobs, skipped = build_var_blobs(namespace_to_save, spool_dir=_spool_dir(),
                                         mem_available=self._mem_available, disk_free=self._disk_free,
                                         namespace=getattr(self._executor, "_namespace", None),
                                         deadline=deadline, prev_manifest=prev_loaded[0])
        try:
            return self._upload_namespace_delta(s3, prefix, blobs, skipped, t0, step_timings, step_sizes,
                                                checkpoint_id=checkpoint_id, deadline=deadline,
                                                prev_loaded=prev_loaded)
        finally:
            release_blobs(blobs)

    def _upload_namespace_delta(self, s3, prefix, blobs, skipped, t0, step_timings, step_sizes,
                                *, checkpoint_id: str | None = None, deadline=None,
                                prev_loaded: tuple | None = None) -> int:
        """Upload half of _save_namespace_delta (blobs already built and hashed).

        Commit order, so a failure at any step leaves the previous checkpoint
        intact: (a) changed large vars go to a key the committed manifest does not
        reference (_large_object_key alternates between two keys per var), (b) the
        small batch, (c) manifest.json, (d) only then the objects the new manifest
        no longer references are deleted."""
        import time as _time
        not_saved = [{"name": n, "reason": str(r)[:300], "kind": not_saved_kind(str(r))} for n, r in skipped]
        self.last_not_saved = not_saved
        if skipped:
            for item in not_saved:
                if item["kind"] == "unpicklable":
                    continue
                logger.warning(f"   Delta: variable '{item['name']}' NOT SAVED: {item['reason']}")
            logger.info(f"   Delta: skipped {len(skipped)} var(s): {skipped[:10]}")
        _by_value = {n: b["by_value"] for n, b in blobs.items() if b.get("by_value")}
        if _by_value:
            logger.warning(f"   Delta: reference cycle between variables; "
                           f"embedded by value: {_by_value}")

        # 2. Split into large (own object) vs small (batched into __small__.pkl).
        small_names = [n for n, m in blobs.items() if m["size"] <= DELTA_SMALL_VAR_BYTES]
        large_names = [n for n, m in blobs.items() if m["size"] > DELTA_SMALL_VAR_BYTES]

        step_timings["serialize"] = _time.perf_counter() - t0
        step_sizes["serialize"] = sum(m["size"] for m in blobs.values())  # logical size

        # 3. Diff against previous manifest → decide uploads/deletes.
        t0 = _time.perf_counter()
        prev, prev_small_hash = prev_loaded if prev_loaded is not None else self._load_manifest(s3, prefix)

        # A var skipped because it would not fit (memory/disk) keeps its previous
        # saved copy: its committed manifest entry is carried into the new
        # manifest, so its object is neither deleted nor dropped from restore.
        # (An unpicklable var has no usable previous copy to keep by design.)
        carried: dict = {}
        for item in not_saved:
            pm = prev.get(item["name"])
            if item["kind"] in ("memory", "disk") and isinstance(pm, dict) and pm.get("object") \
                    and pm.get("hash"):
                carried[item["name"]] = dict(pm)
        carried_small = sorted(n for n, m in carried.items() if m["object"] == "vars/__small__.pkl")

        # The batched small blob is itself content-addressed by the combined hash of
        # its members, so it only re-uploads when some small var actually changed.
        def _small_hash(members):
            return _hash_bytes(("|".join(
                f"{n}:{blobs[n]['hash'] if n in blobs else carried[n]['hash']}" for n in sorted(members)
            )).encode()) if members else None

        small_manifest_hash = _small_hash(small_names + carried_small)
        carried_small_bytes: dict = {}
        if carried_small and small_manifest_hash != prev_small_hash:
            # The batch is rewritten, so the kept small members need their old
            # pickle bytes from the committed batch (format 2). If they can't be
            # read, they are dropped, as before this carry existed.
            try:
                fmt, old = _decode_small_batch(
                    s3.get_object(Bucket=self.bucket, Key=f"{prefix}/vars/__small__.pkl")["Body"].read())
            except Exception as e:
                fmt, old = None, {}
                logger.warning(f"   Delta: could not read the previous small batch ({e})")
            for n in carried_small:
                if fmt == SMALL_BATCH_FORMAT and isinstance(old.get(n), (bytes, bytearray)):
                    carried_small_bytes[n] = old[n]
                else:
                    carried.pop(n, None)
                    logger.warning(f"   Delta: previous copy of '{n}' could not be kept")
            carried_small = sorted(carried_small_bytes)
            small_manifest_hash = _small_hash(small_names + carried_small)
        for item in not_saved:
            if item["name"] in carried:
                item["kept_previous"] = True
                logger.warning(f"   Delta: kept the previously saved copy of '{item['name']}'")

        uploaded_bytes = 0
        uploaded_objs = 0
        bucket = self.bucket  # resolve once (may hit STS) before fanning out

        # 4a. Large vars: upload those whose hash changed vs prev.
        changed_large = [n for n in large_names if prev.get(n, {}).get("hash") != blobs[n]["hash"]]

        # 4. Assemble the NEW manifest (name -> hash/size/object-key-suffix). An
        #    unchanged large var keeps its committed object; a changed one gets the
        #    key the committed manifest does NOT reference, so the previous
        #    checkpoint stays loadable until the new manifest replaces it.
        new_manifest: dict = {}
        for n in large_names:
            obj = (prev[n]["object"] if n not in changed_large and prev.get(n, {}).get("object")
                   else _large_object_key(n, prev.get(n, {}).get("object")))
            new_manifest[n] = {"hash": blobs[n]["hash"], "size": blobs[n]["size"], "object": obj}
        for n in small_names:
            new_manifest[n] = {"hash": blobs[n]["hash"], "size": blobs[n]["size"], "object": "vars/__small__.pkl"}
        # By-name class/function references (nspickle): "refs" lists the
        # variables this one references, which the restore loads first;
        # "by_value" the ones it embeds by value to break a reference cycle.
        for n in large_names + small_names:
            for k in ("refs", "by_value"):
                if blobs[n].get(k):
                    new_manifest[n][k] = list(blobs[n][k])
        for n, m in carried.items():
            new_manifest[n] = m

        # 4b. Small vars: re-upload the batched object only if the combined hash changed.
        #     The batch holds each var's ALREADY-PICKLED bytes (format 2), not live
        #     objects: re-loading every blob to re-pickle a dict made save() fail as
        #     a whole when one var pickled but wouldn't unpickle (and re-ran its
        #     __setstate__ on every save). Restore loads each entry on its own.
        small_payload = None
        if (small_names or carried_small) and small_manifest_hash != prev_small_hash:
            small_payload = _encode_small_batch({**carried_small_bytes,
                                                 **{n: blobs[n]["bytes"] for n in small_names}})

        # Upload changed large vars IN PARALLEL on the shared client's pool (a
        # spooled pickle is uploaded from its file, never read into memory).
        # .result() on EVERY future: the first failure propagates (after in-flight
        # PUTs settle), so neither the small batch nor manifest.json below is
        # written, and the committed manifest still points at its own objects.
        if changed_large:
            # Pickles of _SPOOL_UPLOAD_CHUNK or more go one at a time (each is a
            # multipart upload with its own threads); the rest share the pool. That
            # bounds the upload buffers to about 16 x 16 MiB + 8 x 16 MiB.
            big = [n for n in changed_large if blobs[n]["size"] >= _SPOOL_UPLOAD_CHUNK and blobs[n].get("path")]
            rest = [n for n in changed_large if n not in big]
            with ThreadPoolExecutor(max_workers=RESTORE_MAX_WORKERS) as _ex:
                _futures = [
                    _ex.submit(_put_blob, s3, bucket, f"{prefix}/{new_manifest[n]['object']}", blobs[n],
                               deadline)
                    for n in rest
                ]
                for _f in _futures:
                    _f.result()
            for n in big:
                _put_blob(s3, bucket, f"{prefix}/{new_manifest[n]['object']}", blobs[n], deadline)
            for n in changed_large:
                uploaded_bytes += blobs[n]["size"]
                uploaded_objs += 1
            self.last_save_progress["uploaded_bytes"] = uploaded_bytes
        # The commit point for a save with a deadline: past it, nothing the
        # committed manifest references has been touched (changed large vars went
        # to alternate keys), so stopping here keeps the previous checkpoint. The
        # small batch has one fixed key that the committed manifest also uses, so
        # once it is written the manifest below is written too (no check between):
        # a new batch must not be left under an old manifest.
        _deadline_check(deadline, "manifest")
        # The small batch has one fixed key (older images read only that key), so
        # it is written after every large object, right before the manifest.
        if small_payload is not None:
            s3.put_object(Bucket=bucket, Key=f"{prefix}/vars/__small__.pkl", Body=small_payload)
            uploaded_bytes += len(small_payload)
            uploaded_objs += 1
            self.last_save_progress["uploaded_bytes"] = uploaded_bytes

        # 5. Write manifest.json LAST (atomic swap): restore only trusts a manifest
        #    that references fully-uploaded objects. Stash the small-batch hash so the
        #    next save can tell whether the batched object needs rewriting.
        manifest_doc = {
            "format": "delta-v1",
            "vars": new_manifest,
            "__small_hash__": small_manifest_hash,
            # Informational: the layout THIS save writes for vars/__small__.pkl. The
            # batch is unchanged (not re-uploaded) when its hash matches, so an
            # older format-1 object can still be live; restore detects the format
            # from the object itself (_decode_small_batch).
            "small_format": SMALL_BATCH_FORMAT,
            "count": len(new_manifest),
        }
        if checkpoint_id is not None:
            manifest_doc["checkpoint_id"] = checkpoint_id
        if not_saved:
            # Additive key (older readers ignore it): the variables this save left
            # out, and why. One with kept_previous is still in "vars" with its
            # previous saved copy; the others are not restored.
            manifest_doc["not_saved"] = {i["name"]: {k: i[k] for k in ("reason", "kind", "kept_previous") if k in i}
                                         for i in not_saved}
        body = json.dumps(manifest_doc)
        s3.put_object(Bucket=self.bucket, Key=f"{prefix}/manifest.json", Body=body, ContentType="application/json")
        self.last_save_progress["committed"] = True

        # 6. Only now (the new manifest is committed) delete what it no longer
        #    references: the previous object of each changed large var, the objects
        #    of removed vars, and the small batch when no small var remains. Each
        #    delete is best-effort (a leftover object is unreferenced, not wrong).
        keep = {m["object"] for m in new_manifest.values()}
        stale = {m.get("object") for n, m in prev.items()
                 if not n.startswith("__") and isinstance(m, dict) and m.get("object")}
        stale = {k for k in stale - keep if k.startswith("vars/") and k != "vars/__small__.pkl"}
        if not small_names and not carried_small and prev_small_hash is not None:
            stale.add("vars/__small__.pkl")
        removed = [n for n in prev.keys() if n not in blobs and n not in carried and not n.startswith("__")]
        for key in sorted(stale):
            try:
                s3.delete_object(Bucket=self.bucket, Key=f"{prefix}/{key}")
            except Exception:
                pass
        # Best-effort: remove a stale legacy whole-namespace pkl so restore prefers delta.
        try:
            s3.delete_object(Bucket=self.bucket, Key=f"{prefix}/checkpoint.pkl")
        except Exception:
            pass

        self._saved_manifest = dict(new_manifest)
        self._saved_manifest["__small_hash__"] = small_manifest_hash  # keep for in-mem diff
        self._saved_manifest_prefix = prefix

        step_timings["upload_pkl"] = _time.perf_counter() - t0
        step_sizes["upload_pkl"] = uploaded_bytes
        logger.info(
            f"   Delta: {len(new_manifest)} vars total; uploaded {uploaded_objs} object(s), "
            f"{uploaded_bytes/1024:.1f} KB (of {step_sizes['serialize']/1024:.1f} KB logical); "
            f"deleted {len(removed)} removed var(s)"
        )
        return uploaded_bytes

    # ─── Delta /tmp file save (per-file objects) ──────────────────────────────

    def _load_file_manifest(self, s3, prefix: str) -> tuple:
        """Load the last-saved FILE manifest from S3 (authoritative), falling back
        to the in-memory cache. Returns (files_dict, small_sig); files_dict is {}
        if none (→ first save uploads all).

        At most ONE S3 GET (the old code GOT files_manifest.json twice — once for
        the files, once for __small_sig__). Like _load_manifest, an in-memory
        manifest written to / restored from this SAME prefix is used without a GET."""
        if self._saved_file_manifest and self._saved_file_manifest_prefix == prefix:
            mem = {k: v for k, v in self._saved_file_manifest.items() if k != "__small_sig__"}
            return mem, self._saved_file_manifest.get("__small_sig__")
        try:
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/files_manifest.json")
            man = json.loads(resp["Body"].read().decode("utf-8"))
            if isinstance(man, dict) and "files" in man:
                return man["files"], man.get("__small_sig__")
        except s3.exceptions.NoSuchKey:
            pass
        except Exception as e:
            logger.warning(f"   File delta: manifest load warning ({e}) — treating as first save (upload all)")
        if self._saved_file_manifest:
            return dict(self._saved_file_manifest), self._saved_file_manifest.get("__small_sig__")
        return {}, None

    def _save_files_delta(self, s3, prefix, data_files, step_timings, step_sizes, *, deadline=None) -> tuple:
        """Per-file delta save of /tmp data files. Uploads only changed/new files.

        Layout under sessions/{id}/:
          files/{name}           — one gzip'd object per LARGE file
          files/__small__.tar.gz — a single tar.gz of all SMALL files (batched)
          files_manifest.json    — {"files": {name: {"size","mtime_ns","object"}}}

        Change detection uses (size, mtime_ns) — no file read for unchanged files.
        Returns (uploaded_bytes, file_count).
        """
        import gzip as _gzip
        import time as _time
        t0 = _time.perf_counter()

        # 1. Scan disk → signatures (cheap; os.stat only). Key by the path
        # RELATIVE to /tmp (e.g. "models/x.txt"), so nested files are preserved
        # distinctly and the restore writes them back to the same subdir. (Was
        # os.path.basename, which flattened the tree and collided same-named
        # files across dirs.) os.path.relpath keeps a top-level file's key as
        # just "x.csv", so existing flat checkpoints map unchanged.
        disk: dict = {}
        for fp in data_files:
            try:
                size, mtime_ns = file_signature(fp)
                rel = os.path.relpath(fp, "/tmp")
                disk[rel] = {"path": fp, "size": size, "mtime_ns": mtime_ns}
            except OSError:
                continue

        small = {n: m for n, m in disk.items() if m["size"] <= DELTA_SMALL_FILE_BYTES}
        large = {n: m for n, m in disk.items() if m["size"] > DELTA_SMALL_FILE_BYTES}

        # Combined signature of the small set → the batched object only re-uploads
        # when some small file changed/was added/removed.
        small_sig = _hash_bytes(
            ("|".join(f"{n}:{small[n]['size']}:{small[n]['mtime_ns']}" for n in sorted(small))).encode()
        ) if small else None

        # 2. Build the new manifest (object key per file).
        new_manifest: dict = {}
        for n, m in large.items():
            new_manifest[n] = {"size": m["size"], "mtime_ns": m["mtime_ns"], "object": f"files/{n}.gz"}
        for n, m in small.items():
            new_manifest[n] = {"size": m["size"], "mtime_ns": m["mtime_ns"], "object": "files/__small__.tar.gz"}

        # 3. Diff vs previous manifest.
        prev, prev_small_sig = self._load_file_manifest(s3, prefix)

        uploaded_bytes = 0
        uploaded_objs = 0
        bucket = self.bucket  # resolve once (may hit STS) before fanning out

        # 3a. Large files: (re)upload those whose signature changed. gzip each
        #     (compresslevel=1: far faster than the default 9 for a modest size
        #     cost; still standard gzip, so restore's gzip.decompress is unchanged).
        #     read → compress → PUT runs per file IN PARALLEL; .result() on every
        #     future so the first failure propagates and files_manifest.json below
        #     is NOT written.
        changed_large = [
            n for n, m in large.items()
            if not ((p := prev.get(n)) and p.get("size") == m["size"] and p.get("mtime_ns") == m["mtime_ns"])
        ]

        def _upload_large_file(n):
            _deadline_check(deadline, "files")
            with open(large[n]["path"], "rb") as f:
                body = _gzip.compress(f.read(), compresslevel=1)
            s3.put_object(Bucket=bucket, Key=f"{prefix}/files/{n}.gz", Body=body)
            return len(body)

        if changed_large:
            with ThreadPoolExecutor(max_workers=RESTORE_MAX_WORKERS) as _ex:
                _futures = [_ex.submit(_upload_large_file, n) for n in changed_large]
                _sizes = [_f.result() for _f in _futures]
            uploaded_bytes += sum(_sizes)
            uploaded_objs += len(_sizes)

        # 3b. Small files: rewrite the batched tar.gz only if the small set changed.
        #     Past this deadline check the files manifest below is written too
        #     (the tar has one fixed key the committed files manifest also uses).
        _deadline_check(deadline, "files")
        if small and small_sig != prev_small_sig:
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w:gz", compresslevel=1) as tar:
                for n, m in small.items():
                    tar.add(m["path"], arcname=n)
            tar_buf.seek(0)
            body = tar_buf.read()
            s3.put_object(Bucket=self.bucket, Key=f"{prefix}/files/__small__.tar.gz", Body=body)
            uploaded_bytes += len(body)
            uploaded_objs += 1
        elif not small and prev_small_sig is not None:
            try:
                s3.delete_object(Bucket=self.bucket, Key=f"{prefix}/files/__small__.tar.gz")
            except Exception:
                pass

        # 4. Write files_manifest.json LAST (atomic swap).
        doc = {"format": "file-delta-v1", "files": new_manifest,
               "__small_sig__": small_sig, "count": len(new_manifest)}
        s3.put_object(Bucket=self.bucket, Key=f"{prefix}/files_manifest.json",
                      Body=json.dumps(doc), ContentType="application/json")

        # 4b. Only now (the new files manifest is committed) delete the objects of
        #     files removed since the last save (large ones only; small files live
        #     in the batched object which is rewritten wholesale), so a save that
        #     stops earlier deletes nothing the committed files manifest uses.
        removed = [n for n in prev.keys() if not n.startswith("__") and n not in disk]
        for n in removed:
            if prev[n].get("object") == f"files/{n}.gz":
                try:
                    s3.delete_object(Bucket=self.bucket, Key=f"{prefix}/files/{n}.gz")
                except Exception:
                    pass
        # Remove a stale legacy tarball so restore prefers the delta layout.
        try:
            s3.delete_object(Bucket=self.bucket, Key=f"{prefix}/files.tar.gz")
        except Exception:
            pass

        self._saved_file_manifest = dict(new_manifest)
        self._saved_file_manifest["__small_sig__"] = small_sig
        self._saved_file_manifest_prefix = prefix

        step_timings["archive_files"] = _time.perf_counter() - t0
        step_sizes["archive_files"] = uploaded_bytes
        # On-disk size of EVERY tracked file (unchanged ones included): part of
        # the whole-saved-state size (checkpoint_total_kb). Kept off step_sizes,
        # whose sum is the bytes WRITTEN by this save.
        self._last_files_total_bytes = sum(m["size"] for m in disk.values())
        logger.info(f"   File delta: {len(disk)} files total; uploaded {uploaded_objs} object(s), "
                    f"{uploaded_bytes/1024:.1f} KB; deleted {len(removed)} removed file(s)")
        return uploaded_bytes, len(disk)

    # ─── Delta namespace/file restore (inverse of save) ───────────────────────

    # Sentinel key used by _restore_namespace_delta's worker pool to flag the
    # small-batch future's result so the merge step knows to iterate small_names
    # into the namespace, rather than assigning a single large var by name.
    _SMALL_BATCH = object()

    def _restore_namespace_delta(self, s3, prefix, var_meta, small_names, step_timings, step_sizes) -> dict:
        """Per-variable delta restore: parallel-download large var objects AND the
        batched small-vars object in the SAME pool, so the small-batch GET
        overlaps the large downloads instead of running after them. Deserializes
        each with the checkpoint unpickler (nspickle). Inverse of
        _save_namespace_delta. Mutates step_timings/step_sizes in place; returns
        the assembled namespace dict.

        The values go into the returned temporary dict, but the
        notebook-namespace marker is bound to the LIVE executor namespace
        (self._executor._namespace), so every restored function, method and
        class body function has that dict as its __globals__. The caller must
        adopt the values into that same dict (never a copy of it).

        Load order (by-name class and function references, see nspickle): a
        var with "refs" in the manifest is downloaded with the others but
        unpickled only after every download, in reference order (a class before
        its subclasses, instances and the functions that reference it). A
        reference resolves to the value restored under that name; when that
        var is not loaded yet it is loaded on demand first, so the order can
        never bind a wrong object. A reference to a var that could not be
        restored fails only the var that holds it (listed in
        last_restore_not_restored with the name it references)."""
        import time as _time

        t0 = _time.perf_counter()
        large_names = [n for n, m in var_meta.items() if m.get("object") != "vars/__small__.pkl"]
        namespace = {}
        total_bytes = 0
        live = getattr(self._executor, "_namespace", None)
        if live is None:
            logger.debug("   Restore: executor has no namespace; functions get a new globals dict")
            live = {}

        skipped: list = []   # vars whose pickle would not load (name only, never the value)
        not_restored: list = []   # [{name, reason}] for the same vars
        refs_of = {n: [r for r in (m.get("refs") or ()) if isinstance(r, str)]
                   for n, m in var_meta.items() if isinstance(m, dict)}
        pending: dict = {}   # name -> pickle bytes, unpickled after the downloads
        loading: set = set()

        class _Deferred(Exception):
            """A by-name reference met while loading in the download pool."""

        def _defer(kind, name):
            raise _Deferred(name)

        def _load_one(n, b, resolve):
            """Unpickle one var: ("ok", value), ("deferred", None) when it holds a
            by-name reference and is being loaded in the download pool, or
            ("failed", None): a failure skips THAT var with a warning naming it
            and the error type (never the value) instead of failing the restore."""
            try:
                return "ok", nspickle.loads(b, live, resolve)
            except _Deferred:
                return "deferred", None
            except nspickle.MissingReference as e:
                reason = e.reason
            except Exception as e:
                reason = f"could not unpickle: {type(e).__name__}"
            logger.warning(f"   Restore: skipped variable '{n}' ({reason})")
            not_restored.append({"name": n, "reason": reason})
            return "failed", None

        def _take(n, b, status, obj):
            if status == "ok":
                namespace[n] = obj
            elif status == "deferred":
                pending[n] = b
            else:
                skipped.append(n)

        def _resolve(kind, name):
            """A by-name reference: the var restored under `name`, loaded now
            when it is still pending."""
            if name not in namespace and name in pending and name not in loading:
                _load_pending(name)
            try:
                return nspickle.resolver_for(namespace)(kind, name)
            except nspickle.MissingReference as e:
                if name in loading:
                    raise nspickle.MissingReference(kind, name, e.reason.replace(
                        "which was not restored", "which is part of a reference cycle")) from None
                raise

        def _load_pending(n):
            loading.add(n)
            tu = _time.perf_counter()
            try:
                status, obj = _load_one(n, pending.pop(n), _resolve)
            finally:
                loading.discard(n)
                if n in large_stats:
                    large_stats[n][1] += _time.perf_counter() - tu
            _take(n, None, status, obj)

        large_stats: dict = {}   # name -> [get_s, unpickle_s, bytes, ranged] (vars >= _RANGED_GET_THRESHOLD)

        def _fetch_var(n):
            # An S3 error (e.g. NoSuchKey) still propagates: a missing object is
            # a broken checkpoint, not a bad variable.
            key = f"{prefix}/{var_meta[n]['object']}"
            size = var_meta[n].get("size", 0)
            if not (isinstance(size, int) and size >= _RANGED_GET_THRESHOLD):
                obj = s3.get_object(Bucket=self.bucket, Key=key)
                b = obj["Body"].read()
                if refs_of.get(n):
                    return n, b, ("deferred", None)
                return n, b, _load_one(n, b, _defer)
            tg = _time.perf_counter()
            b, ranged = None, False
            try:
                b, ranged = _ranged_get(s3, self.bucket, key, size, var_meta[n].get("hash")), True
            except Exception as e:
                logger.warning(f"   Restore: ranged download of variable '{n}' failed "
                               f"({type(e).__name__}); using a single GET")
            if b is None:
                # Outside the except block, so the failed buffer is already freed.
                b = s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            stats = large_stats[n] = [_time.perf_counter() - tg, 0.0, len(b), ranged]
            if refs_of.get(n):
                return n, b, ("deferred", None)
            tu = _time.perf_counter()
            result = _load_one(n, b, _defer)
            stats[1] += _time.perf_counter() - tu
            return n, b, result

        def _fetch_small_batch():
            obj = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/vars/__small__.pkl")
            b = obj["Body"].read()
            return self._SMALL_BATCH, b, _decode_small_batch(b)

        # Submit large vars AND (if present) the small batch into the SAME pool so
        # the small GET overlaps the large downloads rather than following them.
        # The small batch is fetched whenever small_names is non-empty, regardless
        # of whether any large objects exist (preserves today's unconditional fetch).
        if large_names or small_names:
            with ThreadPoolExecutor(max_workers=RESTORE_MAX_WORKERS) as _ex:
                _futures = [_ex.submit(_fetch_var, n) for n in large_names]
                if small_names:
                    _futures.append(_ex.submit(_fetch_small_batch))
                for _f in as_completed(_futures):
                    n, b, value = _f.result()
                    total_bytes += len(b)
                    if n is self._SMALL_BATCH:
                        fmt, small_dict = value
                        for sn in small_names:
                            if sn not in small_dict:
                                continue
                            if fmt == 1:
                                # Format 1 (older saves): live objects, already loaded.
                                namespace[sn] = small_dict[sn]
                            elif refs_of.get(sn):
                                pending[sn] = small_dict[sn]
                            else:
                                _take(sn, small_dict[sn], *_load_one(sn, small_dict[sn], _defer))
                    else:
                        _take(n, b, *value)
        # The vars that hold by-name references, in reference order (on demand
        # for anything the manifest did not list).
        for n in reference_order(pending, refs_of):
            if n in pending:
                _load_pending(n)
        if large_stats:
            _rows = sorted(large_stats.items(), key=lambda kv: kv[1][0] + kv[1][1], reverse=True)
            _per = "; ".join(
                f"{name} get={g*1000:.0f}ms unpickle={u*1000:.0f}ms {sz/1048576:.1f}MB{' ranged' if r else ''}"
                for name, (g, u, sz, r) in _rows)
            logger.info(f"   Restore vars: get={sum(v[0] for v in large_stats.values())*1000:.0f}ms "
                        f"unpickle={sum(v[1] for v in large_stats.values())*1000:.0f}ms; "
                        f"per var (slowest first): {_per}")
        step_timings["download_pkl"] = _time.perf_counter() - t0
        step_sizes["download_pkl"] = total_bytes
        self._last_restore_skipped_vars = sorted(skipped)
        self.last_restore_not_restored = sorted(not_restored, key=lambda i: i["name"])
        # Variables that embed a referenced class or function by value (a
        # reference cycle was broken at save time): that copy is not the
        # restored class or function of the same name.
        self.last_restore_by_value = sorted(
            ({"name": n, "by_value": list(var_meta[n]["by_value"])} for n in namespace
             if isinstance(var_meta.get(n), dict) and var_meta[n].get("by_value")),
            key=lambda i: i["name"])
        return namespace

    def _restore_files_delta(self, s3, prefix, fmeta, step_timings, step_sizes) -> int:
        """Per-file delta restore: parallel-download large file objects (gunzip +
        write to /tmp) AND fetch the batched small-files tar.gz in the SAME pool,
        so the small-tar GET overlaps the large downloads instead of running
        after them. The small tar's bytes are fetched in the worker; extraction
        happens on the main thread after the pool joins. Inverse of
        _save_files_delta. Mutates step_timings/step_sizes in place; returns
        total bytes restored."""
        import gzip as _gzip
        import time as _time

        t0 = _time.perf_counter()
        small = [n for n, m in fmeta.items() if m.get("object") == "files/__small__.tar.gz"]
        large = [n for n, m in fmeta.items() if m.get("object") != "files/__small__.tar.gz"]
        total = 0
        small_tar_bytes = None

        def _fetch_file(n):
            obj = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/{fmeta[n]['object']}")
            raw = _gzip.decompress(obj["Body"].read())
            # `n` is a filename from the checkpoint manifest — validate it
            # stays under /tmp before writing (guards against traversal/absolute).
            dest = _safe_join("/tmp", n)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(raw)
            return len(raw)

        def _fetch_small_tar():
            obj = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/files/__small__.tar.gz")
            return obj["Body"].read()

        # Submit large files AND (if present) the small tar's GET into the SAME
        # pool so it overlaps the large downloads rather than following them.
        # The small tar is fetched whenever `small` is non-empty, regardless of
        # whether any large files exist (preserves today's unconditional fetch).
        if large or small:
            with ThreadPoolExecutor(max_workers=RESTORE_MAX_WORKERS) as _ex:
                _futures = {_ex.submit(_fetch_file, n): n for n in large}
                _small_future = _ex.submit(_fetch_small_tar) if small else None
                for _f in as_completed(_futures):
                    total += _f.result()
                if _small_future is not None:
                    small_tar_bytes = _small_future.result()

        # Extract the small tar on the main thread after the pool joins (keeps
        # the archive module's extraction off worker threads; large-file writes
        # already happened concurrently inside their own workers above).
        if small_tar_bytes is not None:
            total += len(small_tar_bytes)
            buf = io.BytesIO(small_tar_bytes)
            with tarfile.open(fileobj=buf, mode="r:gz") as tar:
                _safe_extractall(tar, "/tmp")
        step_timings["download_files"] = _time.perf_counter() - t0
        step_timings["extract_files"] = 0  # unchanged: original never times extraction separately
        step_sizes["download_files"] = total
        return total

    def _restore_packages(self, s3, prefix: str) -> float:
        """Install user-installed packages from the checkpoint's requirements.txt
        (simple list — no diffing needed). Inverse of the packages step of save().

        Runs on a background thread from restore() so pip install (a subprocess —
        does not hold the parent GIL) overlaps the namespace/file download+
        deserialize work. FAIL-SOFT exactly as before: a missing requirements.txt
        is "nothing to install" (not an error); a batch pip failure/timeout falls
        back to per-package best-effort; a package that won't install logs a
        warning and does NOT raise. Returns the wall-clock duration in seconds
        (for step_timings["packages"]) — never raises. When every line is an exact
        pin that is already installed, pip is not run at all; otherwise pip runs in
        the pre-imported pip helper (falling back to `python -m pip`), see
        _restore_packages_pip.
        """
        import time as _time
        import platform as _platform
        from app.notebook.pip_diagnostics import normalize_arch
        from app.platform.pip_helper import unsatisfied_requirements

        t0 = _time.perf_counter()
        _py = f"{sys.version_info.major}.{sys.version_info.minor}"
        _arch = normalize_arch(_platform.machine())
        result = {"verified": True, "arch": _arch, "python": _py, "packages_total": 0,
                  "packages_failed": []}
        self._pkg_result = result
        try:
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/requirements.txt")
            requirements = resp["Body"].read().decode("utf-8")
            if requirements.strip():
                packages = [p.strip() for p in requirements.strip().split('\n') if p.strip()]
                if packages:
                    result["packages_total"] = len(packages)
                    # Exact pins (name==version) that are already installed need no
                    # pip at all (~1 ms check vs a pip start that costs 1-30 s on a
                    # fresh VM). Any other spec, or a missing / other-version pin,
                    # sends the WHOLE list through pip exactly as before.
                    _t_chk = _time.perf_counter()
                    try:
                        _need = unsatisfied_requirements(packages)
                    except Exception as _ce:  # never let the shortcut break a restore
                        logger.warning(f"   Package pin check failed ({_ce}) — running pip")
                        _need = list(packages)
                    if not _need:
                        logger.info(f"   ✓ All {len(packages)} user packages already installed at the "
                                    f"pinned versions — skipping pip "
                                    f"({(_time.perf_counter() - _t_chk) * 1e3:.1f}ms): {', '.join(packages)}")
                    else:
                        logger.info(f"   Installing {len(packages)} user packages: {', '.join(packages)} "
                                    f"(pin check {(_time.perf_counter() - _t_chk) * 1e3:.0f}ms)")
                        self._restore_packages_pip(packages, result, _arch, _py)
                    # Track them in our list for future checkpoints
                    self._user_installed_packages = packages
                    if _need:
                        logger.info(f"   ✓ Installed {len(packages)} packages")
        except s3.exceptions.NoSuchKey:
            # No requirements.txt in the checkpoint — either the user installed
            # nothing, or a save was interrupted before it was written. Log it so
            # a "package missing after restore" report is diagnosable (packages are
            # now flushed to S3 at /install time, so this should be rare).
            logger.info("   Packages: no requirements.txt in checkpoint — none to restore")
        except Exception as e:
            logger.warning(f"   Package restore warning: {e}")
            self._pkg_result = {"verified": False, "reason": f"could not read the package list: {e}"}
        return _time.perf_counter() - t0

    # Flags for every restore pip call: no self-version check (an extra pypi.org
    # request on every fresh VM, whose state file lives in the empty ~/.cache/pip),
    # never prompt, no progress bar, and --no-compile: measured as a net win
    # (VM, statsmodels+bottle: install -1.37 s, first import +0.5-0.65 s, net about
    # -0.8 s; locally net -1.0 to -2.6 s). The .pyc files are then written by the
    # first import instead. No --prefer-binary: it changed nothing measurable
    # (every pin has a wheel) and can pick an older version for a non-exact spec.
    # See .agents/tasks/pip-prewarm/impl-notes.md. --log (added per call) records pip's
    # timestamped verbose log, so the INFO timeline below can split a slow restore
    # into interpreter start / resolve / download / install. --quiet stays last
    # before the packages: stdout stays as quiet as before.
    _RESTORE_PIP_FLAGS = ("--disable-pip-version-check", "--no-input", "--progress-bar", "off",
                          "--no-compile")

    @staticmethod
    def _restore_pip_run(pkgs: list, timeout: float, label: str):
        """One restore pip call: the pre-imported pip helper (app.platform.pip_helper)
        when it is running, else `python -m pip` exactly as before. Logs the elapsed
        time, the path taken and pip's key log lines at INFO. Returns
        (returncode | None, stdout, stderr, timed_out). A subprocess error other than
        a timeout propagates (as before). The caller holds pip_lock()."""
        import tempfile
        import time as _time
        from app.platform import pip_helper

        fd, log_path = tempfile.mkstemp(prefix="pip-restore-", suffix=".log")
        os.close(fd)
        args = (["install", *CheckpointManager._RESTORE_PIP_FLAGS, "--log", log_path, "--quiet"]
                + list(pkgs))
        t_wall, t0 = _time.time(), _time.perf_counter()
        via = "helper"
        try:
            res = pip_helper.run_via_helper(args, timeout=timeout)
            if res is not None:
                rc, out, err, timed_out = res.returncode, res.stdout, res.stderr, res.timed_out
            else:
                via = "subprocess"
                try:
                    proc = subprocess.run([sys.executable, "-m", "pip"] + args,
                                          capture_output=True, text=True, timeout=timeout)
                    rc, out, err, timed_out = proc.returncode, proc.stdout or "", proc.stderr or "", False
                except subprocess.TimeoutExpired as te:
                    rc, timed_out = None, True
                    out = (te.stdout.decode("utf-8", "replace") if isinstance(te.stdout, bytes)
                           else (te.stdout or ""))
                    err = (te.stderr.decode("utf-8", "replace") if isinstance(te.stderr, bytes)
                           else (te.stderr or ""))
            ms = (_time.perf_counter() - t0) * 1e3
            status = "timeout" if timed_out else f"rc={rc}"
            logger.info(f"   pip {label} via {via}: {status} {ms:.0f}ms")
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    lines = pip_helper.pip_log_key_lines(f.read(), t_wall)
            except OSError:
                lines = []
            if lines:
                logger.info(f"   pip {label} timeline:\n      " + "\n      ".join(lines))
            if (timed_out or rc != 0) and (out or err).strip():
                logger.info(f"   pip {label} output tail:\n{(out + err)[-1500:]}")
            return rc, out, err, timed_out
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    def _restore_packages_pip(self, packages: list, result: dict, arch: str, py: str):
        """pip part of _restore_packages: one batch call, then a per-package
        best-effort pass if the batch fails. Failures go into result["packages_failed"]
        exactly as before. Holds the VM-wide pip lock so a concurrent /install cannot
        race on site-packages."""
        from app.notebook.pip_diagnostics import describe_pip_failure, base_package_name
        from app.platform.pip_helper import pip_lock

        # Scale the timeout with package count, and fall back to a
        # per-package best-effort pass so one heavy or conflicting pin
        # doesn't block the rest. (The batch timeout was a fixed 60s —
        # shorter than a single interactive install's 120s — so a heavy
        # package could checkpoint successfully yet fail to restore.)
        batch_timeout = min(900, 180 + 120 * len(packages))
        with pip_lock():
            _rc, _out, _err, _timed_out = self._restore_pip_run(packages, batch_timeout, "batch")
            batch_ok = not _timed_out and _rc == 0
            if _timed_out:
                logger.warning(f"   Batch install timed out after {batch_timeout}s — retrying per-package")
            elif not batch_ok:
                logger.warning(f"   Batch install returned {_rc} — retrying per-package")
            if batch_ok:
                return
            for _pkg in packages:
                _name = base_package_name(_pkg) or _pkg
                try:
                    _rc, _out, _err, _timed_out = self._restore_pip_run([_pkg], 180, _name)
                except Exception as _pe:
                    logger.warning(f"   Package restore: {_pkg} failed — {_pe}")
                    result["packages_failed"].append({"name": _name, "reason": str(_pe)})
                    continue
                if _timed_out or _rc != 0:
                    _diag = describe_pip_failure(
                        _pkg, _out + _err, _err, timed_out=_timed_out, returncode=_rc,
                        arch=arch, python_version=py, timeout_s=180)
                    logger.warning(f"   Package restore: {_pkg} failed [{_diag['kind']}] — {_diag['message'][:300]}")
                    result["packages_failed"].append({"name": _name, "reason": _diag["message"]})

    def _adopt_namespace(self, namespace: dict):
        """Sanitize the dill-restored namespace (drop live runtime objects; values
        are adopted as unpickled, never copied) then merge into the live executor namespace. The heavy
        lifting is in the module-level _sanitize_restored_namespace (pure +
        unit-tested); this method just applies it to the executor."""
        # Entries an older image saved for injected names (the display() shim, a
        # closure over a deserialized copy of the old executor; the helpers) never
        # replace the live executor's own objects. Nothing is deleted from S3: the
        # next save simply no longer lists them.
        _injected = _drop_injected_from_restored(namespace, self._injected_objects())
        if _injected:
            logger.debug(f"   restore: kept the live injected name(s), skipped the "
                         f"checkpointed copy: {sorted(_injected)}")
        namespace, dropped = _sanitize_restored_namespace(
            namespace, live_namespace=getattr(self._executor, "_namespace", None))
        if dropped:
            logger.warning(f"   restore: dropped {len(dropped)} live runtime "
                           f"object(s) that can't cross a process boundary "
                           f"(would deadlock): {dropped}")
        self._executor._namespace.update(namespace)

    def _injected_objects(self) -> dict:
        """{name: object} the executor injects into the namespace: its helpers
        (executor._injected_helpers()) and the current display() shim
        (executor._display_fn). {} for an executor without them (test fakes).
        Never raises."""
        ex = self._executor
        out = {}
        try:
            fn = getattr(ex, "_injected_helpers", None)
            if callable(fn):
                out.update(fn() or {})
        except Exception:
            pass
        shim = getattr(ex, "_display_fn", None)
        if shim is not None:
            out["display"] = shim
        return out

    def _runtime_snapshot(self) -> dict:
        """Runtime section {clock, vars, runs, snapshot} for dag.json. NEVER raises.

        1. runtime_section(lock_timeout=2.0) → snapshot "locked";
        2. RuntimeSectionBusy (a cell holds _exec_lock) → up to 3 best-effort
           runtime_section_unlocked() copies (RuntimeError = dict changed size
           during iteration) → "unlocked", logged at warning;
        3. anything else / 3 failed attempts → clock 0, vars = shallow copy of
           get_var_provenance() (or {}), runs {} → "failed", logged at warning;
        4. an executor without runtime_section (test fakes) → vars from
           get_var_provenance(), "locked".
        The 2 s bound keeps a running cell from delaying a save by its run time."""
        ex = self._executor

        def _fallback_vars():
            try:
                return dict(ex.get_var_provenance() or {})
            except Exception:
                return {}

        if not hasattr(ex, "runtime_section"):
            return {"clock": 0, "vars": _fallback_vars(), "runs": {}, "snapshot": "locked"}
        try:
            from app.notebook.executor import RuntimeSectionBusy
        except Exception:  # pragma: no cover — executor module always importable on the VM
            RuntimeSectionBusy = TimeoutError
        last_exc = None
        try:
            return {**ex.runtime_section(lock_timeout=2.0), "snapshot": "locked"}
        except RuntimeSectionBusy as e:
            last_exc = e
            for _ in range(3):
                try:
                    section = ex.runtime_section_unlocked()
                    logger.warning("   runtime snapshot taken without _exec_lock (lock busy)")
                    return {**section, "snapshot": "unlocked"}
                except RuntimeError as e2:
                    last_exc = e2
                except Exception as e2:
                    last_exc = e2
                    break
        except Exception as e:
            last_exc = e
        logger.warning(f"   runtime snapshot failed ({type(last_exc).__name__}: {last_exc}); "
                       f"saving provenance-only runtime")
        return {"clock": 0, "vars": _fallback_vars(), "runs": {}, "snapshot": "failed"}

    def _build_dag_snapshot(self, session_id: str, checkpoint_id: str) -> dict:
        """dag.json body: the proxy-pushed doc (or empty doc fields), the runtime
        section and the checkpoint stamp. Taken at the very start of save()."""
        doc = None
        if self._dag_store is not None:
            try:
                doc = self._dag_store.snapshot()
            except Exception as e:
                logger.warning(f"   DAG store snapshot failed ({e}); saving empty doc fields")
                doc = None
        if not isinstance(doc, dict):
            doc = {"schema": DAG_SCHEMA, "version": 0, "nodes": [], "edges": [], "issues": []}
        return {
            **doc,
            "runtime": self._runtime_snapshot(),
            "checkpoint": {
                "id": checkpoint_id,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "microvm_id": self._session_state.get("microvm_id"),
            },
        }

    def save(self, session_id: str, *, deadline=None):
        """Checkpoint current state to S3 (see _save_locked), one save at a time.

        deadline: a SaveDeadline (the /suspend hook) or None (/terminate,
        /checkpoint-save: unbounded, as before). With a deadline the save stops
        at its next cancellation point once the budget is spent and raises
        SaveCancelled: multipart uploads are aborted, spool files deleted, and
        manifest.json is not written, so the previous checkpoint stays
        committed and the state stays dirty. SaveCancelled(phase="lock") means
        a previous save still held the lock when the budget ran out."""
        if deadline is None:
            self._save_lock.acquire()
        elif not self._save_lock.acquire(timeout=max(deadline.remaining(), 0)):
            raise SaveCancelled("a previous save is still running", "lock")
        try:
            self.last_save_progress = {"phase": "start", "uploaded_bytes": 0, "committed": False}
            return self._save_locked(session_id, deadline)
        finally:
            self._save_lock.release()

    def _save_locked(self, session_id: str, deadline=None):
        """
        Checkpoint current state to S3 (namespace + files + packages + metadata).

        Called from BOTH lifecycle hooks (see app/platform/hooks.py):
          • /suspend  — the primary path: a VM killed while SUSPENDED never fires
                        /terminate, so we save here before the freeze.
          • /terminate — the RUNNING → terminate path (explicit close / rotation).
        Callers gate this behind is_dirty() so unchanged state isn't re-saved.
        Runs synchronously; /suspend invokes it via asyncio.to_thread and awaits
        it to completion (never fire-and-forget — the VM freezes on hook return).
        """
        import time as _time

        # 0. FIRST (before namespace filtering): checkpoint id + DAG/runtime
        #    snapshot, so dag.json's runtime and the pickled values come from the
        #    same instant. The id goes into manifest.json AND dag.json; restore
        #    compares them to detect a dag.json left over from another save.
        checkpoint_id = uuid.uuid4().hex
        dag_snapshot = self._build_dag_snapshot(session_id, checkpoint_id)

        s3 = get_checkpoint_s3_client()
        prefix = f"sessions/{session_id}"
        step_timings = {}
        step_sizes = {}  # bytes written to S3 per step (parallels step_timings)

        # 1. Serialize the executor namespace (per-variable DELTA — see
        #    _save_namespace_delta). Each var is pickled independently; unpicklable
        #    ones (e.g. _csv.writer, DB connections) are skipped individually.
        # Always exclude here: modules (re-imported via ledger), __dunder__ keys,
        # and IO/file handles (pickling them truncates the underlying file).
        t0 = _time.perf_counter()

        # Log what's in the namespace
        all_keys = [k for k in self._executor._namespace.keys() if not (k.startswith("__") and k.endswith("__"))]
        logger.info(f"   Serializing namespace... ({len(all_keys)} keys: {all_keys[:20]})")

        # Decide what to serialize (excludes modules / IO handles / live runtime
        # objects / display objects). Extracted to a pure, unit-tested helper.
        namespace_to_save, _skipped = _filter_namespace_for_save(
            self._executor._namespace, skip_display_objects=self._skip_display_objects,
            injected=self._injected_objects(),
        )
        if _skipped["injected"]:
            logger.debug(f"   Skipped injected names (display shim / helpers): "
                         f"{len(_skipped['injected'])}")
        if _skipped["module"]:
            logger.info(f"   Skipped modules: {_skipped['module']}")
        if _skipped["io"]:
            logger.info(f"   Skipped IO/file handles (unpicklable, truncate on pickle): {_skipped['io']}")
        if _skipped["runtime"]:
            logger.info(f"   Skipped live runtime objects (locks/pools/clients — deadlock on restore): {_skipped['runtime']}")
        if _skipped["display"]:
            logger.info(f"   Skipped display objects: {_skipped['display']}")
        logger.info(f"   To serialize: {len(namespace_to_save)} vars — {list(namespace_to_save.keys())[:20]}")

        # ── Per-variable DELTA save: pickle each var independently, hash it, and
        #    upload only the ones whose content changed since the last save. Small
        #    vars are batched into one object to avoid a PUT-per-tiny-var explosion.
        #    (Unpicklable vars are skipped individually inside _save_namespace_delta.)
        self.last_save_progress["phase"] = "vars"
        uploaded_bytes = self._save_namespace_delta(
            s3, prefix, namespace_to_save, step_timings, step_sizes,
            checkpoint_id=checkpoint_id, deadline=deadline,
        )
        checkpoint_bytes_len = uploaded_bytes  # bytes actually written this save

        # 2b. Archive local /tmp files (per-file DELTA). Saves EXACTLY what the
        # Data Sources panel discovers (shared _scan_tmp_checkpoint_files): data
        # files recursively under /tmp, plus model artifacts at /tmp top-level and
        # under /tmp/models/. Files are keyed by their path RELATIVE to /tmp (e.g.
        # "models/churn.joblib"), not basename — basename collided same-named files
        # across dirs onto one manifest entry and restored them to the wrong place.
        # The restore side rebuilds the nested path from this relative key
        # (_restore_files_delta → _safe_join("/tmp", n) + makedirs).
        t0 = _time.perf_counter()
        logger.info("   Archiving local files (data + models, recursive /tmp)...")
        data_files = _scan_tmp_checkpoint_files()

        # DELTA: per-file objects + files_manifest.json — only changed/new files are
        # re-uploaded (unchanged skipped via (size,mtime); removed files' objects
        # deleted). Sets step_timings["archive_files"] + step_sizes["archive_files"].
        self.last_save_progress["phase"] = "files"
        _fbytes, _fcount = self._save_files_delta(s3, prefix, data_files, step_timings, step_sizes,
                                                  deadline=deadline)
        self.last_save_progress["uploaded_bytes"] = uploaded_bytes + _fbytes

        # 3–7. The small trailing PUTs (requirements.txt, data_catalog.json,
        # dag.json, imports.json) are independent of each other, so they run
        # IN PARALLEL on the shared client. Each task measures its own step time/
        # size under the SAME keys as before; results are applied in the old fixed
        # order. metadata.json is written LAST, after all of them, so its
        # save_timings_ms covers every earlier step. Failure semantics: a
        # requirements/data_catalog/dag failure raises (metadata not written, the
        # dirty fingerprint not advanced → the next suspend retries); an imports
        # failure stays a warning.
        bucket = self.bucket  # resolve once (may hit STS) before fanning out

        # 3. Save user-installed packages (just the list — no pip freeze needed)
        def _save_packages():
            t0 = _time.perf_counter()
            if self._user_installed_packages:
                packages_content = '\n'.join(self._user_installed_packages)
                s3.put_object(Bucket=bucket, Key=f"{prefix}/requirements.txt", Body=packages_content)
                size = len(packages_content.encode("utf-8"))
                logger.info(f"   Packages: {len(self._user_installed_packages)} user-installed saved, {size} B")
            else:
                size = 0
                logger.info("   Packages: none installed by user (skipping)")
            return _time.perf_counter() - t0, size

        # 5. Save data catalog (discovered schemas — avoids re-discovery on new VM)
        def _save_data_catalog():
            t0 = _time.perf_counter()
            size = 0
            if self._data_catalog and self._data_catalog.discovery_complete:
                catalog_data = json.dumps(self._data_catalog.get_all())
                s3.put_object(Bucket=bucket, Key=f"{prefix}/data_catalog.json", Body=catalog_data, ContentType="application/json")
                size = len(catalog_data.encode("utf-8"))
                logger.info(f"   Data catalog: saved ({size} bytes)")
            return _time.perf_counter() - t0, size

        # 6. Save the DAG document + runtime section (dag.json, replaces
        # provenance.json) so "Defined in", the run ledger and the notebook DAG
        # survive rotation/restore. Snapshot taken at the start of save(). A
        # failure RAISES (like requirements/data_catalog).
        def _save_dag():
            t0 = _time.perf_counter()
            _dag_body = json.dumps(dag_snapshot, ensure_ascii=False, separators=(",", ":"),
                                   default=str).encode("utf-8")
            size = len(_dag_body)
            if size > DAG_MAX_BYTES:
                # S3 has no such limit and restore reads it; only possible through a
                # huge runtime section. Still written.
                logger.warning(f"   DAG: dag.json is {size} B (> {DAG_MAX_BYTES} B cap) — writing anyway")
            s3.put_object(
                Bucket=bucket,
                Key=f"{prefix}/dag.json",
                Body=_dag_body,
                ContentType="application/json",
            )
            _rt = dag_snapshot.get("runtime") or {}
            logger.info(f"   DAG: saved ({len(dag_snapshot.get('nodes') or [])} nodes, "
                        f"{len(_rt.get('vars') or {})} vars, runtime={_rt.get('snapshot')}, {size} B)")
            return _time.perf_counter() - t0, size

        # 7. Save the import ledger (binding-name → import line). Modules can't be
        # pickled, so the namespace pickle drops them; on restore we replay these
        # lines to re-establish `import pandas as pd` etc. so the user's existing
        # cells don't break with NameError after a transparent recovery.
        def _save_imports():
            t0 = _time.perf_counter()
            size = 0
            try:
                imports = self._executor.get_imports()
                if imports:
                    _imp_body = json.dumps(imports)
                    s3.put_object(
                        Bucket=bucket,
                        Key=f"{prefix}/imports.json",
                        Body=_imp_body,
                        ContentType="application/json",
                    )
                    size = len(_imp_body.encode("utf-8"))
                    logger.info(f"   Imports: saved ({len(imports)} bindings, {size} B)")
            except Exception as e:
                logger.warning(f"   Imports save warning: {e}")
            return _time.perf_counter() - t0, size

        _tail_steps = [
            ("packages", _save_packages),
            ("data_catalog", _save_data_catalog),
            ("dag", _save_dag),
            ("imports", _save_imports),
        ]
        self.last_save_progress["phase"] = "tail"
        _deadline_check(deadline, "tail")
        with ThreadPoolExecutor(max_workers=6) as _ex:
            _tail_futures = [(k, _ex.submit(fn)) for k, fn in _tail_steps]
            _tail_results = [(k, _f.result()) for k, _f in _tail_futures]
        for k, (elapsed, size) in _tail_results:
            step_timings[k] = elapsed
            step_sizes[k] = size

        # Size of the WHOLE saved state a restore needs (logical, uncompressed):
        # the pickled size of every saved var (unchanged ones included —
        # step_sizes["serialize"] is the manifest total) + the on-disk size of
        # every tracked /tmp file + the JSON tail rewritten on every save.
        # Excludes metadata.json, the two manifests (a few KB) and an older
        # data_catalog.json left from a save whose discovery had completed.
        # Not added to step_sizes (their sum is the bytes WRITTEN this save).
        checkpoint_total_bytes = (
            step_sizes.get("serialize", 0)
            + getattr(self, "_last_files_total_bytes", 0)
            + sum(step_sizes.get(k, 0) for k in ("packages", "data_catalog", "dag", "imports"))
        )
        checkpoint_total_kb = round(checkpoint_total_bytes / 1024, 1)

        # 4. Save metadata LAST (includes timing breakdown for analysis)
        t0 = _time.perf_counter()
        # variables_count stays the whole saved namespace (back-compat);
        # user_variables_* match GET /vm-status var_count/var_bytes.
        _user_vars, _user_bytes = _user_variable_totals(self._executor._namespace)
        metadata = {
            "session_id": session_id,
            "microvm_id": self._session_state.get("microvm_id"),
            "checkpointed_at": datetime.now(timezone.utc).isoformat(),
            "execution_count": self._executor.get_stats()["execution_count"],
            "variables_count": len(namespace_to_save),
            "user_variables_count": _user_vars,
            "user_variables_bytes": _user_bytes,
            "files_count": len(data_files),
            # Var-pickle bytes uploaded by THIS save (a delta; 0 after a
            # no-change save). Kept for compatibility — NOT the checkpoint size.
            "checkpoint_size_kb": round(checkpoint_bytes_len / 1024, 1),
            # The whole saved state (see checkpoint_total_bytes above).
            "checkpoint_total_kb": checkpoint_total_kb,
            "save_timings_ms": {k: round(v * 1000, 1) for k, v in step_timings.items()},
            # Variables this save left out ([{name, reason, kind}]); a restore
            # does not bring them back.
            "not_saved": list(self.last_not_saved),
        }
        _meta_body = json.dumps(metadata, indent=2)
        self.last_save_progress["phase"] = "metadata"
        _deadline_check(deadline, "metadata")
        s3.put_object(
            Bucket=bucket,
            Key=f"{prefix}/metadata.json",
            Body=_meta_body,
            ContentType="application/json",
        )
        step_timings["metadata"] = _time.perf_counter() - t0
        step_sizes["metadata"] = len(_meta_body.encode("utf-8"))

        total_time = sum(step_timings.values())
        total_bytes = sum(v for k, v in step_sizes.items() if k != "serialize")  # serialize size == pkl, don't double-count
        logger.info(f"   ✅ Checkpoint complete: s3://{self.bucket}/{prefix}/ ({total_time*1000:.0f}ms total, {total_bytes/1024:.1f} KB written)")
        # Per-step size + timing, in a fixed order, so each step is trackable.
        _order = ["serialize", "upload_pkl", "archive_files", "packages", "metadata", "data_catalog", "dag", "imports"]
        _parts = []
        for k in _order:
            if k in step_timings:
                _kb = step_sizes.get(k, 0) / 1024
                _parts.append(f"{k}={step_timings[k]*1000:.0f}ms/{_kb:.1f}KB")
        logger.info(f"   ⏱  Per-step (time/size): {', '.join(_parts)}")
        self.last_save_timings = {k: round(v * 1000, 1) for k, v in step_timings.items()}
        self.last_save_timings["total_ms"] = round(total_time * 1000, 1)
        self.last_save_sizes = {k: v for k, v in step_sizes.items()}
        self.last_save_sizes["total_bytes"] = total_bytes
        self.last_checkpoint_at = _time.time()
        # Summary of the last save, surfaced by the lifecycle hooks in a single
        # greppable log line so suspend-saves vs terminate-saves are trackable in
        # CloudWatch (filter on "CHECKPOINT" — see hooks.py).
        self.last_save_summary = {
            "session_id": session_id,
            "vars": len(namespace_to_save),
            "user_vars": _user_vars,
            "files": len(data_files),
            "packages": len(self._user_installed_packages),
            "size_kb": round(total_bytes / 1024, 1),
            "pkl_kb": round(checkpoint_bytes_len / 1024, 1),
            "total_kb": checkpoint_total_kb,  # == metadata checkpoint_total_kb
            "total_ms": round(total_time * 1000, 1),
            "step_ms": {k: round(v * 1000, 1) for k, v in step_timings.items()},
            "step_kb": {k: round(v / 1024, 1) for k, v in step_sizes.items() if k != "serialize"},
            "not_saved": list(self.last_not_saved),
        }
        self.last_save_progress["phase"] = "done"
        # Record what we just persisted so a subsequent suspend with no further
        # changes can skip re-saving (dirty-flag). Safe to capture now because
        # save() runs synchronously and nothing mutates the namespace during it.
        # A variable left out because it did not fit in memory or on disk keeps
        # the state dirty, so the next save retries it (an unpicklable one would
        # fail again, so it does not).
        if not any(i.get("kind") in ("memory", "disk") for i in self.last_not_saved):
            self._saved_fingerprint = self._current_fingerprint()

    def _set_restored(self, doc, session_id, runtime_source, consistent):
        if self._dag_store is None:
            return
        try:
            self._dag_store.set_restored(doc, session_id, runtime_source, consistent)
        except Exception as e:
            logger.warning(f"   DAG store set_restored failed: {e}")

    def _restore_dag(self, s3, prefix: str, session_id: str, manifest_checkpoint_id):
        """Restore step "dag" (design §6.2). Never raises.

        dag.json present and valid → consistent = (dag.json checkpoint id ==
        manifest checkpoint_id; missing on either side → False). Consistent →
        the whole runtime section; inconsistent → vars + clock only (the run
        ledger may describe another save). dag.json missing/unparseable →
        provenance.json → set_var_provenance; both missing → nothing."""
        doc = None
        try:
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/dag.json")
            doc = json.loads(resp["Body"].read().decode("utf-8"))
            if not (isinstance(doc, dict) and doc.get("schema") == DAG_SCHEMA
                    and isinstance(doc.get("runtime"), dict)):
                logger.warning("   dag.json invalid (schema/runtime) — treating as missing")
                doc = None
        except s3.exceptions.NoSuchKey:
            logger.info("   No dag.json found — trying provenance.json")
            doc = None
        except Exception as e:
            logger.warning(f"   dag.json restore warning ({e}) — treating as missing")
            doc = None

        if doc is not None:
            runtime = doc.get("runtime") or {}
            ckpt = doc.get("checkpoint")
            doc_id = ckpt.get("id") if isinstance(ckpt, dict) else None
            consistent = bool(doc_id) and bool(manifest_checkpoint_id) and doc_id == manifest_checkpoint_id
            try:
                if consistent:
                    self._executor.set_runtime_section(runtime)
                else:
                    logger.warning("   dag.json checkpoint id does not match manifest.json — "
                                   "loading vars + clock without the run ledger")
                    self._executor.set_runtime_section({**runtime, "runs": {}})
            except Exception as e:
                logger.warning(f"   Runtime section restore warning: {e}")
            stored = {k: v for k, v in doc.items() if k != "runtime"}
            self._set_restored(stored, session_id, "dag.json", consistent)
            _vars = runtime.get("vars") if isinstance(runtime.get("vars"), dict) else {}
            logger.info(f"   DAG: restored from dag.json ({len(doc.get('nodes') or [])} nodes, "
                        f"{len(_vars)} vars, consistent={consistent})")
            return

        try:
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/provenance.json")
            provenance = json.loads(resp["Body"].read().decode("utf-8"))
            self._executor.set_var_provenance(provenance)
            self._set_restored(None, session_id, "provenance.json", None)
            logger.info(f"   Provenance: restored from provenance.json fallback ({len(provenance)} vars)")
            return
        except s3.exceptions.NoSuchKey:
            logger.info("   No provenance found (skipping)")
        except Exception as e:
            logger.warning(f"   Provenance restore warning: {e}")
        self._set_restored(None, session_id, None, None)

    def restore(self, session_id: str):
        """
        Restore executor state from a previous S3 checkpoint.
        Called from the /run lifecycle hook when restoreFromSession is set.
        """
        # Modules in memory before this restore (the snapshot's preloads + anything
        # the session imported). The import-ledger timing label judges against it,
        # so a module the var unpickle pulled in is labelled [cold], not [preloaded].
        _mods_at_start = frozenset(sys.modules)
        import time as _time
        import traceback as _tb

        # Shared checkpoint S3 client (32-slot pool for the 16-way parallel GETs
        # below, bounded connect/read timeouts + adaptive retries, tcp_keepalive —
        # see _CHECKPOINT_S3_CONFIG for why each setting matters on a cold VM).
        s3 = get_checkpoint_s3_client()
        prefix = f"sessions/{session_id}"
        step_timings = {}
        step_sizes = {}  # internal plumbing for the delta restore helpers; not logged directly

        # 0. Kick off packages install (GET requirements.txt + pip install) on a
        #    background thread, right away. pip install is a subprocess — it does
        #    not hold the parent GIL — so it genuinely overlaps the namespace/file
        #    download+deserialize work below. HARD GATE: the future is joined
        #    further down, BEFORE _adopt_namespace() merges restored vars into the
        #    live namespace (a var whose class lives in a user-installed package
        #    could fail/misbehave if unpickled before that package exists) and
        #    BEFORE step 6 (imports-replay), which already required packages to be
        #    installed first. This is STRICTLY SAFER than before: today packages
        #    install AFTER adoption; here adoption waits for packages to finish.
        #    FAIL-SOFT is preserved inside _restore_packages (never raises).
        self.last_restore_report = None
        self.last_restore_not_restored = []
        self.last_restore_by_value = []
        self._pkg_result = {}
        _pkg_pool = ThreadPoolExecutor(max_workers=1)
        _pkg_future = _pkg_pool.submit(self._restore_packages, s3, prefix)

        # 1. Restore namespace (DELTA layout only: manifest.json + vars/*).
        #   • NO manifest.json → nothing was ever checkpointed → fresh session,
        #     SUCCESS, skip quietly.
        #   • manifest exists but an object fails to load (corrupt/partial) → REAL
        #     failure, re-raise so /restore-state returns non-200 and reactive
        #     recovery fails loud instead of handing back an empty VM.

        try:
            t0 = _time.perf_counter()
            logger.info("   Restoring namespace...")
            try:
                _m = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/manifest.json")
                manifest_doc = json.loads(_m["Body"].read().decode("utf-8"))
            except s3.exceptions.NoSuchKey:
                manifest_doc = None

            if not manifest_doc:
                logger.info("   No namespace checkpoint found — fresh session, nothing to restore")
                step_timings["download_pkl"] = 0
                step_timings["deserialize"] = 0
                # No vars to adopt, but still respect HARD GATE 1/2: packages must
                # be joined before anything downstream (step 6 imports) runs.
                step_timings["packages"] = _pkg_future.result()
            else:
                # ── DELTA RESTORE: download per-var objects listed in the manifest ──
                var_meta = manifest_doc.get("vars", {})
                small_names = [n for n, m in var_meta.items() if m.get("object") == "vars/__small__.pkl"]
                namespace = self._restore_namespace_delta(s3, prefix, var_meta, small_names, step_timings, step_sizes)
                total_bytes = step_sizes.get("download_pkl", 0)
                # HARD GATE 1: join packages BEFORE adopting the namespace, so a
                # restored var backed by a user-installed package's class only
                # becomes visible to user code once that package is importable.
                step_timings["packages"] = _pkg_future.result()
                t0 = _time.perf_counter()
                self._adopt_namespace(namespace)
                step_timings["deserialize"] = _time.perf_counter() - t0
                # Prime the in-memory manifest so the FIRST save after restore is a
                # correct delta (only re-uploads vars the user changes post-restore).
                self._saved_manifest = dict(var_meta)
                self._saved_manifest["__small_hash__"] = manifest_doc.get("__small_hash__")
                self._saved_manifest_prefix = prefix
                # Capture the REAL restored-namespace count + downloaded pkl size for
                # the greppable RESTORE summary line below (get_stats() only counts
                # user vars, and last_restore_sizes doesn't exist — so the summary
                # was showing the wrong count and pkl_kb=?).
                self._last_restore_var_count = len(namespace)
                self._last_restore_pkl_kb = round(total_bytes / 1024, 1)
                logger.info(f"   Restored {len(namespace)} variables via DELTA manifest "
                            f"({total_bytes/1024:.1f} KB, download: {step_timings['download_pkl']*1000:.0f}ms, "
                            f"deserialize: {step_timings['deserialize']*1000:.0f}ms)")
                if self._last_restore_skipped_vars:
                    logger.warning(f"   Restore: {len(self._last_restore_skipped_vars)} variable(s) "
                                   f"could not be unpickled and were skipped: "
                                   f"{self._last_restore_skipped_vars[:10]}")
        except s3.exceptions.NoSuchKey:
            # A referenced var object is missing → manifest points at absent data.
            # Treat as a real failure (fail loud) rather than silently partial.
            logger.error(f"   Namespace manifest referenced a missing object: {_tb.format_exc()}")
            raise
        except Exception as e:
            logger.error(f"   Namespace checkpoint exists but FAILED to load: {e}\n{_tb.format_exc()}")
            raise
        finally:
            # Namespace is fail-LOUD: on an abort here (raise above) we didn't
            # join packages, so don't block the raise on pip finishing — let it
            # complete in the background and release the pool's worker thread
            # without waiting. On the success path this is a cheap no-op (the
            # pool is already idle once _pkg_future.result() returned above).
            _pkg_pool.shutdown(wait=False)

        # 2. Restore local files (DELTA layout only: files_manifest.json + files/*).
        #    No manifest → no files to restore (skip). Missing referenced object →
        #    logged (files are best-effort; a missing data file shouldn't abort the
        #    whole restore the way a missing namespace object does).
        try:
            t0 = _time.perf_counter()
            logger.info("   Restoring files...")
            try:
                _fm = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/files_manifest.json")
                files_doc = json.loads(_fm["Body"].read().decode("utf-8"))
            except s3.exceptions.NoSuchKey:
                files_doc = None

            if not files_doc:
                logger.info("   No files manifest found (skipping)")
                step_timings["download_files"] = 0
                step_timings["extract_files"] = 0
            else:
                # ── DELTA RESTORE: download per-file objects listed in the manifest ──
                fmeta = files_doc.get("files", {})
                total = self._restore_files_delta(s3, prefix, fmeta, step_timings, step_sizes)
                # Prime the in-memory file manifest so the first save after restore
                # is a correct delta (only re-uploads files changed post-restore).
                self._saved_file_manifest = dict(fmeta)
                self._saved_file_manifest["__small_sig__"] = files_doc.get("__small_sig__")
                self._saved_file_manifest_prefix = prefix
                logger.info(f"   Files restored via DELTA manifest: {len(fmeta)} files, "
                            f"{total/1024:.1f} KB (download: {step_timings['download_files']*1000:.0f}ms)")
        except Exception as e:
            logger.error(f"   Failed to restore files: {e}\n{_tb.format_exc()}")

        # 3. Packages: already kicked off at the top of restore() (step 0) and
        #    joined above (HARD GATE 1, before _adopt_namespace). step_timings
        #    ["packages"] is populated at one of those two join points — nothing
        #    further to do here. This also satisfies HARD GATE 2 (packages before
        #    imports, step 6): the join already happened earlier in this method,
        #    which strictly precedes step 6 below.

        # 4. Restore data catalog (pre-discovered schemas)
        try:
            t0 = _time.perf_counter()
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/data_catalog.json")
            catalog_json = json.loads(resp["Body"].read().decode("utf-8"))
            if self._data_catalog and catalog_json.get("entries"):
                from app.notebook.data_catalog import CatalogEntry, ColumnInfo
                for entry_data in catalog_json["entries"]:
                    entry = CatalogEntry(
                        source_type=entry_data["source_type"],
                        source_id=entry_data["source_id"],
                        display_name=entry_data["display_name"],
                        columns=[ColumnInfo(**c) for c in entry_data.get("columns", [])],
                        row_count=entry_data.get("row_count"),
                        size=entry_data.get("size", ""),
                        status=entry_data.get("status", "discovered"),
                        metadata=entry_data.get("metadata", {}),
                    )
                    self._data_catalog._entries[entry.source_id] = entry
                self._data_catalog._discovery_complete = True
                logger.info(f"   Data catalog: restored {len(catalog_json['entries'])} entries")
            step_timings["data_catalog"] = _time.perf_counter() - t0
        except s3.exceptions.NoSuchKey:
            logger.info("   No data catalog found (will re-discover)")
            step_timings["data_catalog"] = 0
        except Exception as e:
            logger.warning(f"   Data catalog restore warning: {e}")
            step_timings["data_catalog"] = 0

        # 5. Restore the DAG + runtime section from dag.json (provenance.json is
        #    the read-only fallback for checkpoints written before dag.json).
        manifest_checkpoint_id = manifest_doc.get("checkpoint_id") if isinstance(manifest_doc, dict) else None
        self._restore_dag(s3, prefix, session_id, manifest_checkpoint_id)

        # 6. Replay the import ledger so module bindings (pd, np, ...) that the
        # pickle could not carry are re-established in the namespace. Without this,
        # a user's existing cells break with NameError after a transparent recovery.
        # Packages were installed before step 1's adoption (started at step 0,
        # joined at HARD GATE 1, see step 3), so these imports resolve.
        # Best-effort per line: one bad import must not fail the whole restore.
        try:
            t0 = _time.perf_counter()
            resp = s3.get_object(Bucket=self.bucket, Key=f"{prefix}/imports.json")
            imports = json.loads(resp["Body"].read().decode("utf-8"))
            replayed, failed = 0, []
            line_ms = []   # (ms, line, was_preloaded) per replayed binding
            for _name, _line in imports.items():
                # Was the line's top-level module already in sys.modules (preloaded in
                # the image snapshot)? If so the replay should be ~0ms; a slow line that
                # was NOT preloaded is a candidate for app/server.py's preload block.
                _pre = _import_already_loaded(_line, _mods_at_start)
                _t = _time.perf_counter()
                try:
                    exec(_line, self._executor._namespace)
                    replayed += 1
                except Exception as _ie:
                    failed.append(f"{_line} ({type(_ie).__name__})")
                line_ms.append(((_time.perf_counter() - _t) * 1000, _line, _pre))
            # Per-line timing, slowest first, so a slow restore names its culprit.
            if line_ms:
                line_ms.sort(key=lambda x: x[0], reverse=True)
                logger.info("   Imports per line: " + "; ".join(
                    f"{ms:.0f}ms {ln!r}"
                    + {True: " [preloaded]", False: " [cold]"}.get(pre, "")
                    for ms, ln, pre in line_ms))
            # Remember the ledger so the NEXT checkpoint re-persists these bindings
            # even if the user doesn't re-run the import cells on the new VM.
            self._executor.set_imports(imports)
            step_timings["imports"] = _time.perf_counter() - t0
            logger.info(f"   Imports: replayed {replayed}/{len(imports)} bindings"
                        + (f"; failed: {failed}" if failed else ""))
        except s3.exceptions.NoSuchKey:
            logger.info("   No import ledger found (skipping)")
            step_timings["imports"] = 0
        except Exception as e:
            logger.warning(f"   Imports restore warning: {e}")
            step_timings["imports"] = 0

        total_time = sum(step_timings.values())
        logger.info(f"   ✅ Session restored from: {session_id} ({total_time*1000:.0f}ms total)")
        logger.info(f"   ⏱  Breakdown: download_pkl={step_timings.get('download_pkl',0)*1000:.0f}ms, deserialize={step_timings.get('deserialize',0)*1000:.0f}ms, download_files={step_timings.get('download_files',0)*1000:.0f}ms, extract={step_timings.get('extract_files',0)*1000:.0f}ms, packages={step_timings.get('packages',0)*1000:.0f}ms, imports={step_timings.get('imports',0)*1000:.0f}ms")
        self.last_restore_report = dict(self._pkg_result) if self._pkg_result else None
        if isinstance(self.last_restore_report, dict) and self.last_restore_not_restored:
            # Variables whose pickle would not load (for example an object saved
            # by an older image with a different function format): [{name, reason}].
            self.last_restore_report["vars_not_restored"] = list(self.last_restore_not_restored)
        if isinstance(self.last_restore_report, dict) and self.last_restore_by_value:
            self.last_restore_report["refs_by_value"] = list(self.last_restore_by_value)
        self.last_restore_timings = {k: round(v * 1000, 1) for k, v in step_timings.items()}
        self.last_restore_timings["total_ms"] = round(total_time * 1000, 1)

        # Single greppable line mirroring the CHECKPOINT line (filter on "RESTORE").
        # vars = the actual number of names restored into the namespace (not just
        # user vars); pkl_kb = the pickle bytes downloaded. Makes "why was my first
        # cell slow after idle" answerable.
        _vars = getattr(self, "_last_restore_var_count", "?")
        _pkl_kb = getattr(self, "_last_restore_pkl_kb", "?")
        logger.info(
            f"   ✅ RESTORE session={session_id} vars={_vars} "
            f"pkl_kb={_pkl_kb} total_ms={self.last_restore_timings['total_ms']} "
            f"step_ms={self.last_restore_timings} "
            f"← s3://{self.bucket}/sessions/{session_id}/"
        )
        # Just-restored state matches S3, so treat as clean: a /suspend right
        # after restore (with no edits) should skip the redundant re-save.
        self._saved_fingerprint = self._current_fingerprint()
