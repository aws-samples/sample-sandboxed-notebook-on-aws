"""
Data Catalog — schema catalog for the notebook's data sources.

Part of: app.notebook (application layer)

Two kinds of entries, discovered by two different owners:
  - CLOUD sources (S3, DynamoDB, Athena, Aurora) — discovered BY THE PROXY
    (which owns cloud enumeration + schema derivation) and PUSHED to the VM
    fully-formed, either in the launch run-hook payload or on a panel refresh
    (POST /data-catalog/resync). The VM stores them verbatim; it does NOT query
    AWS to discover cloud schemas itself.
  - LOCAL /tmp files — discovered ON THE VM (only the VM can read files on its
    own filesystem). Reconciled live on upload/delete.

The merged set is exposed via GET /data-catalog and consumed by the SQL engine
(for routing db.schema.table → the right backend) and the Data Sources panel.
"""

import os
import json
import glob
import time
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ColumnInfo:
    name: str
    dtype: str = "string"
    sample: str = ""
    nullable: bool = True


@dataclass
class CatalogEntry:
    source_type: str         # "s3", "dynamodb", "athena", "aurora", "local"
    source_id: str           # unique identifier (uri, table name, file path)
    display_name: str        # human-readable name
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count: int | None = None
    size: str = ""
    status: str = "pending"  # "pending", "discovered", "error"
    error: str = ""
    metadata: dict = field(default_factory=dict)  # extra info (bucket, database, etc.)


# Non-local source types — the ones the PROXY owns and pushes. Everything that
# isn't one of these (i.e. "local") is VM-owned and preserved across pushes.
_CLOUD_SOURCE_TYPES = ("s3", "dynamodb", "athena", "aurora")


# ── Local /tmp file discovery scope — the SINGLE SOURCE OF TRUTH ────────────
# What counts as a user "local file" on the VM: data files anywhere under /tmp
# (recursive), plus model artifacts at /tmp top-level and under /tmp/models/
# (NOT recursively everywhere — avoids sweeping up stray .pkl caches libraries
# drop deep in /tmp). Defined ONCE here and reused by BOTH the Data Sources panel
# discovery (_discover_local_files, below) and the S3 checkpoint
# (app/platform/checkpoint/manager.py imports local_tmp_file_globs) so what the
# panel SHOWS and what a restore BRINGS BACK can never drift.
_LOCAL_DATA_EXTS = ('*.csv', '*.parquet', '*.json', '*.xlsx', '*.xls', '*.txt')
_LOCAL_MODEL_EXTS = ('*.joblib', '*.jbl', '*.pkl', '*.pickle', '*.sav', '*.onnx')


def local_tmp_file_globs() -> list[str]:
    """Glob patterns for every /tmp file we treat as a notebook local file:
    data files recursively under /tmp; model artifacts at /tmp top-level and
    under /tmp/models/. Shared by discovery AND the checkpoint save/dirty-flag.

    NOTE: this returns PER-FILE globs and MUST stay per-file — the S3 checkpoint
    (app/platform/checkpoint/manager.py) imports it to save EVERY individual part
    file. The partition-folder rollup below is a PRESENTATION/catalog concern only
    (rollup_local_files); it never narrows the checkpoint's file set."""
    globs = [f'/tmp/**/{ext}' for ext in _LOCAL_DATA_EXTS]
    globs += [f'/tmp/{ext}' for ext in _LOCAL_MODEL_EXTS]
    globs += [f'/tmp/models/**/{ext}' for ext in _LOCAL_MODEL_EXTS]
    return globs


# ── Partition-folder rollup (presentation/catalog ONLY) ─────────────────────
# Local analogue of the S3 provider's "parquet_prefix" rollup
# (proxy/platform/datasources/s3.py `_ref_from_prefix_dataset`): a /tmp SUBDIR of
# part files should surface as ONE dataset row (a directory/glob read), not one
# row per part. The in-product producer of such a folder is helpers.export
# `to_local_prefix`, which always writes part-0000.<ext>, part-0001.<ext>, … .
#
# DETECTION RULE (CONSERVATIVE — matches a real to_local_prefix export, never
# merges arbitrary /tmp files):
#   A directory is treated as a partitioned dataset ONLY when it is a SUBDIRECTORY
#   of /tmp (never the /tmp ROOT itself) AND it contains 2+ files that
#     • share the SAME extension (parquet is the primary case; extensionless
#       UNLOAD-style parts are grouped as ext=""), AND
#     • every one of those same-extension files has a part-like stem —
#       part-* or chunk-* (case-insensitive).
#   A directory with two UNRELATED files (e.g. sales.csv + targets.csv, no
#   part-/chunk- stem) is NOT merged; its files stay standalone. Top-level /tmp
#   files are NEVER merged. This mirrors S3 keying off a FIXED known prefix.
_PART_STEM_PREFIXES = ("part-", "chunk-")


def _is_part_name(filename: str) -> bool:
    """True if a leaf filename looks like a partition part (part-*/chunk-*),
    case-insensitive. Works for extensionless parts (UNLOAD) too."""
    low = filename.lower()
    return any(low.startswith(p) for p in _PART_STEM_PREFIXES)


def _file_ext(filename: str) -> str:
    """Lower-cased extension of a leaf filename, or '' if none."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def rollup_local_files(
    filepaths: list[str], root: str = "/tmp"
) -> tuple[list[str], list[dict]]:
    """Partition the flat list of absolute /tmp file paths into standalone files
    and rolled-up partition folders.

    PURE function (no glob; the only I/O is os.path.getsize/getmtime on the paths
    passed in, so it is unit-testable with synthesized paths). Applies the
    DETECTION RULE documented above.

    Returns (standalone_files, partition_folders) where:
      - standalone_files: list[str] of absolute paths kept as individual rows.
      - partition_folders: list[dict], one per detected dataset directory, each:
          {
            "dir":          <abs dir>,
            "display_name": "<leaf>/",            # e.g. "web_sessions_parts/"
            "source_id":    "<abs dir>/",         # trailing slash (mirrors S3)
            "parts":        [<abs part paths>],   # sorted
            "first_part":   <abs path>,           # schema source (first part)
            "size_bytes":   <summed size>,
            "mtime":        <newest mtime>,
            "ext":          <shared ext or "">,
          }
    """
    root_norm = os.path.normpath(root)
    # Group candidate paths by their parent directory.
    by_dir: dict[str, list[str]] = {}
    for fp in filepaths:
        by_dir.setdefault(os.path.dirname(fp), []).append(fp)

    standalone: list[str] = []
    folders: list[dict] = []

    for dirpath, paths in by_dir.items():
        # Never roll up the /tmp ROOT itself — only SUBDIRECTORIES of it.
        is_subdir = os.path.normpath(dirpath) != root_norm
        # Candidate parts: 2+ files that share one extension AND are all part-like.
        part_paths = [p for p in paths if _is_part_name(os.path.basename(p))]
        exts = {_file_ext(os.path.basename(p)) for p in part_paths}
        is_partitioned = is_subdir and len(part_paths) >= 2 and len(exts) == 1

        if not is_partitioned:
            standalone.extend(paths)
            continue

        parts_sorted = sorted(part_paths)
        # Any non-part files sharing the dir stay standalone (don't swallow them).
        standalone.extend(p for p in paths if p not in set(part_paths))

        size_bytes = 0
        newest = 0.0
        for p in parts_sorted:
            try:
                size_bytes += os.path.getsize(p)
                m = os.path.getmtime(p)
                if m > newest:
                    newest = m
            except OSError:
                continue

        leaf = os.path.basename(os.path.normpath(dirpath))
        folders.append({
            "dir": dirpath,
            "display_name": f"{leaf}/",
            "source_id": f"{dirpath}/" if not dirpath.endswith("/") else dirpath,
            "parts": parts_sorted,
            "first_part": parts_sorted[0],
            "size_bytes": size_bytes,
            "mtime": newest,
            "ext": next(iter(exts)),
        })

    return standalone, folders


# A .json file up to this size is parsed whole to tell a JSON array/object from
# JSON Lines. Larger files are only tried as JSON Lines (a sample of 5 lines).
_JSON_FULL_READ_MAX_BYTES = 5 * 1024 * 1024
_SAMPLE_ROWS = 5


def _columns_from_df(df) -> list[ColumnInfo]:
    return [ColumnInfo(name=str(c), dtype=str(df[c].dtype),
                       sample=str(df[c].iloc[0])[:50] if len(df) > 0 else "")
            for c in df.columns]


def _count_lines(filepath: str) -> int:
    """Line count read in binary chunks (no decoding, constant memory). A last
    line without a trailing newline counts too, like iterating the text file."""
    lines = 0
    last = b""
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last and last != b"\n":
        lines += 1
    return lines


def _human_size(size_bytes: int) -> str:
    """Short human-readable byte count (mirrors the per-file formatting used in
    discovery and the S3 provider)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# Module-level handle to the process's active DataCatalog. Set in __init__ so
# standalone helpers (app/notebook/helpers.py read_athena) can consult the catalog
# WITHOUT a FastAPI request/app.state — e.g. to auto-resolve which Athena
# data-catalog a database belongs to (S3 Tables federated catalog). The VM runs a
# single DataCatalog, so a plain module global is sufficient.
_ACTIVE_CATALOG: "DataCatalog | None" = None


def get_active_catalog() -> "DataCatalog | None":
    """The process's active DataCatalog, or None before one is constructed."""
    return _ACTIVE_CATALOG


class DataCatalog:
    """
    In-memory data catalog. Cloud entries are pushed by the proxy; local /tmp
    entries are self-discovered on the VM.

    Usage:
        catalog = DataCatalog()
        catalog.load_cloud_entries(entries)  # proxy-pushed, fully-formed
        catalog.start_local_discovery()      # scan /tmp in the background
        catalog.get_all()                    # merged view
    """

    def __init__(self):
        self._entries: dict[str, CatalogEntry] = {}  # source_id → entry
        self._lock = threading.Lock()
        # Local entries only: source_id → file signature at its last read
        # ((size, mtime_ns) for a file, (total size, newest mtime, part count) for
        # a partition folder). Guarded by _lock.
        self._local_sigs: dict[str, tuple] = {}
        self._discovery_thread: threading.Thread | None = None
        self._discovery_complete = False
        self._on_complete_callback = None  # Called when discovery finishes
        # Signature of the cloud entries at the last time we fired the ready
        # callback (which persists the catalog to S3). The proxy re-pushes the SAME
        # cloud set on every Data Sources refresh/resync, so without this guard each
        # resync re-flushed an identical data_catalog.json to S3 — many redundant
        # writes per session (visible as repeated "Flushed data catalog to S3").
        # None until the first load so the initial flush always happens.
        self._last_cloud_sig: str | None = None
        # Register as the process's active catalog so standalone helpers can find
        # it (last constructed wins; the VM only ever builds one).
        global _ACTIVE_CATALOG
        _ACTIVE_CATALOG = self

    def athena_db_catalog_map(self) -> dict[str, str]:
        """Map { database -> Athena data-catalog name } for every athena entry that
        declares a non-default catalog (S3 Tables federated catalog). A database
        absent from the map uses the default AwsDataCatalog. Used by read_athena to
        auto-resolve the federated catalog from a database name — so users never
        need to pass catalog= or know the internal catalog name."""
        out: dict[str, str] = {}
        with self._lock:
            for e in self._entries.values():
                if e.source_type != "athena":
                    continue
                meta = e.metadata or {}
                db = meta.get("database")
                cat = meta.get("catalog")
                if db and cat:
                    out[db] = cat
        return out

    @property
    def is_discovering(self) -> bool:
        return self._discovery_thread is not None and self._discovery_thread.is_alive()

    @property
    def discovery_complete(self) -> bool:
        return self._discovery_complete

    # ─── Cloud entries (proxy-pushed, fully-formed) ──────────────────────────

    def load_cloud_entries(self, entries: list[dict]):
        """Replace all CLOUD entries with a proxy-pushed, fully-formed set,
        preserving local /tmp entries. Called at launch (from the run-hook) and on
        every panel refresh (POST /data-catalog/resync).

        Each incoming entry is the dict shape emitted by the proxy's
        registry.discover_full_catalog() (== this class's get_all() entry shape):
          {source_type, source_id, display_name, columns:[{name,dtype,sample,
           nullable}], row_count, size, status, error, metadata}

        Local entries are never touched here — the VM owns those. After loading we
        fire the discovery-complete callback so the merged catalog is re-persisted
        to S3 (survives a later interrupted suspend/checkpoint).
        """
        entries = entries or []
        with self._lock:
            # Drop existing cloud entries; keep local ones.
            for sid in [s for s, e in self._entries.items() if e.source_type in _CLOUD_SOURCE_TYPES]:
                del self._entries[sid]
            # Insert the pushed cloud entries.
            for ent in entries:
                stype = ent.get("source_type")
                sid = ent.get("source_id")
                if not stype or not sid:
                    continue
                cols = [
                    ColumnInfo(
                        name=c.get("name", ""),
                        dtype=c.get("dtype", "string"),
                        sample=c.get("sample", ""),
                        nullable=c.get("nullable", True),
                    )
                    for c in (ent.get("columns") or [])
                ]
                self._entries[sid] = CatalogEntry(
                    source_type=stype,
                    source_id=sid,
                    display_name=ent.get("display_name", sid),
                    columns=cols,
                    row_count=ent.get("row_count"),
                    size=ent.get("size", ""),
                    status=ent.get("status", "discovered"),
                    error=ent.get("error", ""),
                    metadata=ent.get("metadata", {}) or {},
                )
        self._discovery_complete = True
        logger.info(f"📊 Data catalog: loaded {len(entries)} proxy-pushed cloud entries")
        # Only persist to S3 when the merged catalog ACTUALLY changed. The proxy
        # re-pushes the identical cloud set on every panel refresh/resync, so
        # firing the flush callback unconditionally rewrote the same
        # data_catalog.json to S3 repeatedly. See _maybe_flush.
        self._maybe_flush(reason="cloud entries loaded")

    def _catalog_signature(self) -> str:
        """Stable content signature of the WHOLE catalog (cloud + local). Used to
        suppress redundant S3 flushes when a resync re-pushes an identical cloud
        set, or a local re-scan finds no changes. Takes the lock itself, so callers
        must NOT hold it."""
        import hashlib
        with self._lock:
            items = []
            for sid in sorted(self._entries.keys()):
                e = self._entries[sid]
                cols = [(c.name, c.dtype, c.nullable) for c in (e.columns or [])]
                items.append((
                    e.source_type, e.source_id, e.display_name,
                    e.row_count, e.size, e.status, e.error, tuple(cols),
                ))
        return hashlib.sha1(repr(items).encode("utf-8"), usedforsecurity=False).hexdigest()

    def _maybe_flush(self, reason: str = ""):
        """Fire the discovery-complete callback (which persists the catalog to S3)
        ONLY when the merged catalog changed since the last flush. This is the
        single guarded flush path shared by cloud-load and local-discovery, so an
        identical re-push or a no-op re-scan never rewrites S3. The first call
        (self._last_cloud_sig is None) always flushes."""
        new_sig = self._catalog_signature()
        if new_sig == self._last_cloud_sig:
            logger.info(f"   ↩ Catalog unchanged since last flush ({reason}) — skipping S3 re-persist")
            return
        self._last_cloud_sig = new_sig
        if self._on_complete_callback:
            # The callback persists the catalog to S3 — a boto3 S3 client init +
            # PUT that measured ~3.5s on a freshly-resumed pool spare, and it ran
            # SYNCHRONOUSLY inside load_cloud_entries() → /rebind, blocking the
            # pool grant for that whole time. The catalog is already in memory and
            # fully usable the moment load_cloud_entries() returns; the S3 persist
            # is only a durability optimization (survive an interrupted suspend),
            # NOT something the grant/launch must wait for. So fire it on a daemon
            # thread and return immediately — the rebind no longer blocks on S3.
            def _flush():
                try:
                    self._on_complete_callback()
                except Exception as e:
                    logger.warning(f"   Catalog ready callback failed: {e}")
            threading.Thread(
                target=_flush, daemon=True, name="data-catalog-s3-flush"
            ).start()

    # ─── Local discovery (VM-owned) ──────────────────────────────────────────

    def start_local_discovery(self):
        """Kick off background discovery of local /tmp files (non-blocking).
        Cloud entries come from load_cloud_entries(), not from here."""
        if self.is_discovering:
            logger.warning("Local discovery already in progress — skipping")
            return
        self._discovery_thread = threading.Thread(
            target=self._run_local_discovery,
            daemon=True,
            name="data-catalog-local-discovery",
        )
        self._discovery_thread.start()

    def _run_local_discovery(self):
        """Background thread: scan /tmp, then fire the ready callback."""
        start = time.time()
        try:
            self._discover_local_files()
        except Exception as e:
            logger.warning(f"   Local file discovery failed: {e}")
        self._discovery_complete = True
        logger.info(f"📊 Data catalog: local discovery complete ({time.time() - start:.1f}s)")
        self._maybe_flush(reason="local discovery")

    def _glob_local_files(self, root: str = "/tmp") -> list[str]:
        """Glob every local file under `root` using the shared per-file globs.
        `root` is injectable ONLY so the offline test can point discovery at a
        tmpdir; production callers pass nothing and scan /tmp exactly as before.
        local_tmp_file_globs() itself is unchanged (the checkpoint imports it)."""
        seen: list[str] = []
        seen_set: set[str] = set()
        for pattern in local_tmp_file_globs():
            # local_tmp_file_globs() hardcodes '/tmp'; retarget to `root` for tests
            # without touching the shared (checkpoint-owned) helper.
            if root != "/tmp":
                pattern = pattern.replace("/tmp", root, 1)
            for filepath in glob.glob(pattern, recursive=True):
                if filepath not in seen_set:
                    seen_set.add(filepath)
                    seen.append(filepath)
        return seen

    def _schema_for_local_file(self, filepath: str):
        """Read columns + row_count for a single local data file. Returns
        (columns, row_count). Best-effort; raises on unreadable files (the caller
        logs and skips). Factored out so both standalone files and a partition
        folder's FIRST part reuse the identical per-ext read.

        Reads stay small: parquet uses the footer metadata (row count, schema) and
        a 5-row batch, csv a 5-row sample plus a binary line count, json a whole
        parse only up to _JSON_FULL_READ_MAX_BYTES."""
        import pandas as pd

        columns = []
        row_count = None
        if filepath.endswith('.csv'):
            df = pd.read_csv(filepath, nrows=_SAMPLE_ROWS)
            columns = _columns_from_df(df)
            row_count = max(_count_lines(filepath) - 1, 0)  # minus header
        elif filepath.endswith('.parquet'):
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(filepath)
            row_count = pf.metadata.num_rows
            batch = next(pf.iter_batches(batch_size=_SAMPLE_ROWS), None)
            sample = (batch.to_pandas() if batch is not None
                      else pf.schema_arrow.empty_table().to_pandas())
            columns = _columns_from_df(sample)
        elif filepath.endswith('.json'):
            columns, row_count = self._schema_for_json_file(filepath)
        elif filepath.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(filepath, nrows=_SAMPLE_ROWS)
            columns = _columns_from_df(df)
        # .txt (and model artifacts) have no tabular reader: the entry is listed
        # with zero columns on purpose, so the panel still shows the file.
        return columns, row_count

    @staticmethod
    def _schema_for_json_file(filepath: str):
        """(columns, row_count) for a JSON array/object or a JSON Lines file."""
        import pandas as pd

        data = None
        if os.path.getsize(filepath) <= _JSON_FULL_READ_MAX_BYTES:
            try:
                with open(filepath, "rb") as f:
                    data = json.load(f)
            except ValueError:
                data = None  # not one JSON document: JSON Lines (or invalid)
        if isinstance(data, list):
            df = pd.DataFrame(data[:_SAMPLE_ROWS])
            return _columns_from_df(df), len(data)
        if isinstance(data, dict):
            try:
                df = pd.DataFrame(data)  # dict of columns / dict of records
            except ValueError:
                df = pd.DataFrame([data])  # one flat object = one row
            return _columns_from_df(df.head(_SAMPLE_ROWS)), len(df)
        df = pd.read_json(filepath, lines=True, nrows=_SAMPLE_ROWS)
        return _columns_from_df(df), _count_lines(filepath)

    def _discover_local_files(self, root: str = "/tmp"):
        """Discover schemas for local data files in /tmp.

        RECONCILES the local set: files present on disk are (re)discovered, and
        local entries whose file no longer exists are PRUNED. This is essential
        after a delete — otherwise a removed /tmp file lingers in the catalog
        forever (add-only scan), so every consumer (the Data Sources panel, the
        entity docs, and especially the Workbook Intel source count + report)
        keeps showing phantom files. Only 'local' entries are touched; cloud
        entries (pushed by the proxy) are left untouched.

        Partition FOLDERS (a /tmp subdir of part-*/chunk-* files — see
        rollup_local_files) collapse into ONE 'local' entry for the DATASET
        (source_id = the directory WITH a trailing slash, metadata flagged
        is_prefix/parquet_prefix just like an S3 parquet_prefix), with the schema
        inferred from the FIRST part. Standalone files keep one entry each.

        `root` is injectable for offline testing only; prod passes nothing.
        """
        # Discovery scope (data files recursive under /tmp; model artifacts at
        # /tmp top-level + /tmp/models/) is defined ONCE in local_tmp_file_globs()
        # and shared with the S3 checkpoint, so the panel and a restore agree.
        all_files = self._glob_local_files(root)
        standalone, folders = rollup_local_files(all_files, root=root)

        # Track every source_id we (re)built so the prune step can drop entries
        # whose underlying file(s) are gone. Partition folders are keyed by their
        # trailing-slash dir; standalone files by their path.
        seen_ids: set[str] = set()

        def _unchanged(source_id: str, sig: tuple) -> bool:
            """True (and the entry is kept as is) when the file signature matches
            the one recorded at its last read, so an upload/delete re-scan does not
            re-read every other file."""
            with self._lock:
                if self._local_sigs.get(source_id) == sig and source_id in self._entries:
                    seen_ids.add(source_id)
                    return True
            return False

        # ── Standalone files: one entry each ──
        for filepath in standalone:
            try:
                source_id = filepath
                filename = os.path.basename(filepath)
                st = os.stat(filepath)
                sig = (st.st_size, st.st_mtime_ns)
                if _unchanged(source_id, sig):
                    continue
                size_bytes = st.st_size
                size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024*1024):.1f} MB"

                columns, row_count = self._schema_for_local_file(filepath)

                # Build the entry fully BEFORE taking the lock, then hold the
                # lock only for the O(1) dict insert. The pandas reads above
                # (and especially the first-ever `import pandas` on a resumed
                # VM) can take seconds; holding self._lock across them stalls
                # EVERY other catalog operation — most importantly the pool
                # /rebind path (load_cloud_entries → _catalog_signature), which
                # was measured blocking 1.2–3.7s purely waiting on this lock.
                # The lock guards the in-memory dict, never I/O.
                entry = CatalogEntry(
                    source_type="local", source_id=source_id,
                    display_name=filename, columns=columns,
                    row_count=row_count, size=size_str,
                    status="discovered",
                )
                with self._lock:
                    self._entries[source_id] = entry
                    self._local_sigs[source_id] = sig
                seen_ids.add(source_id)
            except Exception as e:
                logger.debug(f"   Skipping local file {filepath}: {e}")

        # ── Partition folders: ONE dataset entry each (mirrors S3 rollup) ──
        for folder in folders:
            try:
                source_id = folder["source_id"]  # dir with trailing slash
                sig = (folder["size_bytes"], folder["mtime"], len(folder["parts"]))
                if _unchanged(source_id, sig):
                    continue
                # Schema from the FIRST part (mirrors S3 get_schema prefix branch).
                columns, _ = self._schema_for_local_file(folder["first_part"])
                entry = CatalogEntry(
                    source_type="local", source_id=source_id,
                    display_name=folder["display_name"], columns=columns,
                    row_count=None, size=_human_size(folder["size_bytes"]),
                    status="discovered",
                    metadata={"kind": "parquet_prefix", "is_prefix": True,
                              "queryable": True},
                )
                with self._lock:
                    self._entries[source_id] = entry
                    self._local_sigs[source_id] = sig
                seen_ids.add(source_id)
            except Exception as e:
                logger.debug(f"   Skipping local partition folder {folder.get('dir')}: {e}")

        # Prune local entries whose file(s) are gone (deleted since the last scan).
        # A standalone entry's source_id IS the file. A partition-folder entry's
        # source_id ends with '/', so it is kept while ANY part still exists on
        # disk (do NOT prune it just because the '/'-suffixed path is not a file).
        def _folder_still_present(dir_sid: str) -> bool:
            d = dir_sid.rstrip("/")
            try:
                return os.path.isdir(d) and any(
                    os.path.isfile(os.path.join(d, n)) for n in os.listdir(d)
                )
            except OSError:
                return False

        with self._lock:
            stale = []
            for sid, e in self._entries.items():
                if e.source_type != "local" or sid in seen_ids:
                    continue
                if sid.endswith("/"):
                    if not _folder_still_present(sid):
                        stale.append(sid)
                elif not os.path.exists(sid):
                    stale.append(sid)
            for sid in stale:
                del self._entries[sid]
                self._local_sigs.pop(sid, None)
        if stale:
            logger.info(f"   Pruned {len(stale)} deleted local file(s)/folder(s) from catalog: {[os.path.basename(s.rstrip('/')) for s in stale]}")

    def refresh_local_files(self):
        """Re-scan local /tmp files (called after file upload/delete). Fires the
        discovery-complete callback afterward so the updated catalog is eagerly
        re-persisted (e.g. flushed to S3) — a just-uploaded file's schema then
        survives even if the next namespace checkpoint is interrupted."""
        def _run():
            try:
                self._discover_local_files()
            except Exception as e:
                logger.warning(f"   Local file refresh failed: {e}")
            # Guarded flush: after a file upload/delete the local set usually DID
            # change, so this normally flushes — but a no-op re-scan (e.g. a
            # refresh that found nothing new) won't rewrite S3.
            self._maybe_flush(reason="local file refresh")
        threading.Thread(target=_run, daemon=True).start()

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_all(self) -> dict:
        """Return the full catalog as a serializable dict."""
        from app.notebook.dtypes import normalize_dtype

        with self._lock:
            entries = []
            for entry in self._entries.values():
                entries.append({
                    "source_type": entry.source_type,
                    "source_id": entry.source_id,
                    "display_name": entry.display_name,
                    "columns": [{"name": c.name, "dtype": normalize_dtype(c.dtype), "sample": c.sample, "nullable": c.nullable} for c in entry.columns],
                    "row_count": entry.row_count,
                    "size": entry.size,
                    "status": entry.status,
                    "error": entry.error,
                    "metadata": entry.metadata,
                })
            return {
                "entries": entries,
                "total": len(entries),
                "discovered": sum(1 for e in self._entries.values() if e.status == "discovered"),
                "pending": sum(1 for e in self._entries.values() if e.status == "pending"),
                "errors": sum(1 for e in self._entries.values() if e.status == "error"),
                "discovery_complete": self._discovery_complete,
                "is_discovering": self.is_discovering,
            }

    def get_schema(self, source_id: str) -> dict | None:
        """Return schema for a specific source, or None if not found."""
        from app.notebook.dtypes import normalize_dtype

        with self._lock:
            entry = self._entries.get(source_id)
            if not entry:
                return None
            return {
                "source_type": entry.source_type,
                "source_id": entry.source_id,
                "display_name": entry.display_name,
                "columns": [{"name": c.name, "dtype": normalize_dtype(c.dtype), "sample": c.sample, "nullable": c.nullable} for c in entry.columns],
                "row_count": entry.row_count,
                "size": entry.size,
                "status": entry.status,
                "error": entry.error,
                "metadata": entry.metadata,
            }
