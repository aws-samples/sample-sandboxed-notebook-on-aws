"""
S3 schema provider — reads file headers/metadata without full download.
Supports CSV (read header + sample rows), Parquet (read metadata), JSON (sample).
"""

import os
import io
import time
import logging
from typing import Optional


from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300  # 5 minutes

# File extensions that are trained-model ARTIFACTS, not queryable data. The panel
# hides the SQL insert for these and offers a model-aware Python snippet instead.
# (SageMaker model.tar.gz is matched separately by key suffix — its ext is "gz".)
_MODEL_EXTS = {"joblib", "jbl", "pkl", "pickle", "sav", "onnx"}


def _human_size(size_bytes: int) -> str:
    """Format a byte count as a short human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


class S3SchemaProvider(DataSourceProvider):
    """Schema provider for S3 files (CSV, Parquet, JSON)."""

    display_name = "S3 Bucket"
    icon = "s3"
    supports_sql = True

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        self._bucket = os.environ.get("ARTIFACT_BUCKET", "")
        # Discovery scope: comma-separated S3 prefixes to scan (one level deep).
        # Declarative via config.sh (DATASOURCE_S3_PREFIXES); defaults preserve
        # the previous hardcoded behavior.
        self._prefixes = [
            p.strip() for p in os.environ.get("DATASOURCE_S3_PREFIXES", "samples/,user-data/").split(",")
            if p.strip()
        ]
        # SageMaker trained-model artifacts live NESTED under this prefix
        # (sagemaker/<job>/output/.../model.tar.gz), so the flat one-level scan
        # above can't surface them — a dedicated deep-but-filtered scan (Scope 3 in
        # discover()) finds just the model.tar.gz files. Sourced from the same
        # SAGEMAKER_ARTIFACT_PREFIX the sm_train helper writes to. We DROP it from
        # the flat scan list (nothing lives directly under it) to avoid a wasted
        # listing.
        self._sagemaker_prefixes = []
        _sm_prefix = os.environ.get("SAGEMAKER_ARTIFACT_PREFIX", "sagemaker/").strip()
        if _sm_prefix and not _sm_prefix.endswith("/"):
            _sm_prefix += "/"
        if _sm_prefix:
            self._sagemaker_prefixes.append(_sm_prefix)
            self._prefixes = [p for p in self._prefixes if p != _sm_prefix]
        # Athena UNLOAD output is ALSO nested: unload_athena_to_s3 / SQL-cell UNLOAD
        # write unload/<run-id>/<part-files> — one folder deep — so the flat one-level
        # Scope-1 scan can't see it (nothing lives DIRECTLY under unload/). Like the
        # SageMaker prefix, pull it out of the flat list and give it a dedicated scan
        # (Scope 4) that surfaces one row per UNLOAD RUN (the run prefix), not every
        # part file. The prefix is configurable but defaults to unload/.
        self._unload_prefixes = []
        _ul_prefix = os.environ.get("DATASOURCE_S3_UNLOAD_PREFIX", "unload/").strip()
        if _ul_prefix and not _ul_prefix.endswith("/"):
            _ul_prefix += "/"
        # Only treat it as an UNLOAD prefix if it's actually a configured scan prefix
        # (so turning it off in DATASOURCE_S3_PREFIXES turns off the deep scan too).
        if _ul_prefix and _ul_prefix in self._prefixes:
            self._unload_prefixes.append(_ul_prefix)
            self._prefixes = [p for p in self._prefixes if p != _ul_prefix]
        # Pre-seeded PARTITIONED-PARQUET sample folders: a prefix of part files that
        # should surface as ONE dataset row (like an UNLOAD run), not one row per part
        # — e.g. samples/web_sessions_parquet/. The flat Scope-1 scan can't see nested
        # parts, so these get the same per-prefix rollup (Scope 5). Comma-separated,
        # configurable via DATASOURCE_S3_PARQUET_PREFIXES; defaults to the seeded one.
        self._parquet_dataset_prefixes = [
            p.strip() if p.strip().endswith("/") else p.strip() + "/"
            for p in os.environ.get(
                "DATASOURCE_S3_PARQUET_PREFIXES", "samples/web_sessions_parquet/").split(",")
            if p.strip()
        ]
        # Prefixes whose files a user may DELETE from the panel (subset of the scan
        # prefixes). Declarative via config.sh (DATASOURCE_S3_DELETABLE_PREFIXES);
        # blank disables S3 deletion. Deletes trigger a Notebook Intel deletion update.
        self._deletable_prefixes = [
            p.strip() for p in os.environ.get("DATASOURCE_S3_DELETABLE_PREFIXES", "user-data/").split(",")
            if p.strip()
        ]
        # Prefix under which chat attachments are PROMOTED (copied) to become
        # session-scoped data sources. A promoted file lands at
        #   user-data/<session_id>/<name>
        # and is discovered ONLY for that session (see discover()). This is the
        # per-session overlay on top of the flat shared user-data/ area.
        self._session_source_prefix = os.environ.get(
            "DATASOURCE_S3_SESSION_PREFIX", "user-data/"
        ).strip()
        self._cache: dict[str, tuple[float, SourceSchema]] = {}

    @property
    def source_type(self) -> str:
        return "s3"

    def _ref_from_obj(self, key: str, size_bytes: int, display_name: str = None,
                      modified=None) -> SourceRef:
        """Build a SourceRef for one S3 object key. display_name defaults to the key
        (the panel groups S3 by the display_name's folder segments); a caller can
        override it to hide a folder segment (e.g. strip the <session_id> from a
        promoted user-data/<session_id>/file.csv so it shows as user-data/file.csv)."""
        uri = f"s3://{self._bucket}/{key}"
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        # A model ARTIFACT (trained estimator / ONNX graph / SageMaker model.tar.gz)
        # is not a queryable table — the panel should offer a model Python snippet
        # (load + predict) and HIDE the SQL button (you can't SELECT from a .joblib).
        is_model = ext in _MODEL_EXTS or key.endswith("model.tar.gz")
        return SourceRef(
            source_type="s3",
            source_id=uri,
            display_name=display_name if display_name is not None else key,
            detail=_human_size(size_bytes),
            size=_human_size(size_bytes),
            extra={
                "key": key,
                "bucket": self._bucket,
                "size": _human_size(size_bytes),
                "size_bytes": size_bytes,
                "uri": uri,
                "extension": ext,
                # A model artifact is not SQL-queryable — the panel reads this to
                # hide the SQL insert button and keep only the (model-aware) Python
                # one. Regular data files (csv/parquet/json/...) stay queryable.
                "queryable": not is_model,
                "kind": "model" if is_model else "file",
                # Surfaced to the panel so it can show a delete button only for files
                # under a configured deletable prefix. Nested keys (e.g.
                # user-data/<session_id>/x.csv) match user-data/ via startswith.
                "deletable": any(key.startswith(p) for p in self._deletable_prefixes),
                # ISO timestamp of the object's last modification (from list_objects_v2),
                # so the panel can show a relative "2d ago" per file. None if unknown.
                "last_modified": modified.isoformat() if modified is not None else None,
            },
        )

    def _ref_from_prefix_dataset(self, prefix: str, size_bytes: int, modified) -> SourceRef:
        """Build a SourceRef for a PREFIX of part files surfaced as ONE dataset (not a
        single object) — e.g. an Athena UNLOAD run (unload/<id>/) or a pre-seeded
        partitioned Parquet folder (samples/web_sessions_parquet/). source_id is the
        s3:// URI WITH a trailing slash so the snippet/schema treat it as a multi-file
        prefix. Both read surfaces are offered: Python read_s3_prefix(...) and SQL
        read_parquet('s3://…/*') — the parts may be extensionless (UNLOAD) or .parquet
        (seeded), and DuckDB's read_parquet globs either, so queryable=True."""
        uri = f"s3://{self._bucket}/{prefix}"        # trailing slash preserved
        return SourceRef(
            source_type="s3",
            source_id=uri,
            display_name=prefix,                     # e.g. "unload/20261002-…/" or "samples/web_sessions_parquet/"
            detail=_human_size(size_bytes),
            size=_human_size(size_bytes),
            extra={
                "key": prefix,
                "bucket": self._bucket,
                "uri": uri,
                "size": _human_size(size_bytes),
                "size_bytes": size_bytes,
                # A PREFIX of Parquet parts: Python reads it with read_s3_prefix, and
                # it's ALSO SQL-queryable — DuckDB read_parquet('s3://…/*') globs the
                # parts — so queryable=True shows the SQL insert too (get_sql_snippet).
                "kind": "parquet_prefix",
                "is_prefix": True,
                "queryable": True,
                "deletable": any(prefix.startswith(p) for p in self._deletable_prefixes),
                "last_modified": modified.isoformat() if modified is not None else None,
            },
        )

    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """Enumerate S3 data sources visible to this session.

        Two scopes:
          1. FLAT SHARED: files directly under each configured prefix (samples/,
             user-data/, ...), one level deep — the account-wide shared area, same
             for every session.
          2. SESSION OVERLAY: when session_id is given, ALSO files directly under
             user-data/<session_id>/ (one level within that subfolder) — these are
             chat attachments the user PROMOTED for this session. They are shown
             ONLY to their own session; user-data/<other_session>/... is skipped.

        The session file's display_name strips the <session_id> segment so the panel
        shows it as user-data/<name> alongside the shared files (grouping is by
        display_name folders — see DataSourcesPanel.getGroupPath).

        The boto3 pagination is blocking, so the whole enumeration runs via
        asyncio.to_thread — otherwise it stalls the proxy's single event loop for
        the full listing (mirrors Aurora.discover)."""
        import asyncio

        if not self._bucket:
            return []

        def _enumerate() -> list[SourceRef]:
            refs: list[SourceRef] = []
            try:
                s3 = aws_clients.get_client("s3", region_name=self._region)
                paginator = s3.get_paginator("list_objects_v2")

                # ── Scope 1: flat, one level under each configured prefix ──
                for prefix in self._prefixes:
                    for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix, MaxKeys=50):
                        for obj in page.get("Contents", []):
                            key = obj["Key"]
                            if key.endswith("/"):
                                continue
                            # Only files DIRECTLY under the prefix (skip subfolders —
                            # that excludes Athena per-table dirs AND the per-session
                            # overlay, which scope 2 handles so other sessions' files
                            # stay hidden).
                            relative = key[len(prefix):]
                            if "/" in relative:
                                continue
                            refs.append(self._ref_from_obj(key, obj["Size"], modified=obj.get("LastModified")))

                # ── Scope 2: this session's promoted files under user-data/<session_id>/ ──
                refs.extend(self._session_overlay_refs(paginator, session_id))

                # ── Scope 3: SageMaker trained models (DEEP, filtered) ──
                # sm_train writes artifacts NESTED under the SageMaker prefix
                # (sagemaker/<job>/output/<job>/output/model.tar.gz), which the flat
                # one-level scan above intentionally skips. The artifact a user cares
                # about is the trained MODEL — surface just the model.tar.gz files
                # (not the intermediate input CSVs, which are training noise), grouped
                # under the prefix and labelled by the job name. This scan is DEEP
                # (unlike Scope 1), so we HARD-CAP the total objects examined via
                # PaginationConfig.MaxItems — otherwise a bucket with many past jobs
                # (each with input CSVs + model files) would list unbounded on every
                # panel refresh. MaxItems caps the TOTAL (MaxKeys only sets page size),
                # so a few hundred recent objects are enough to surface recent models.
                for sm_prefix in self._sagemaker_prefixes:
                    # deploy packages each real-time endpoint's artifact under
                    # <sm_prefix>endpoints/<name>/model.tar.gz. Those are INTERNAL
                    # serving packaging, not user-trainable models — and the live
                    # endpoints themselves are surfaced by SageMakerEndpointProvider —
                    # so skip that subtree here (otherwise they show as junk
                    # "endpoints (model)" rows). Only genuine TRAINING-job models
                    # (<sm_prefix><job>/output/.../model.tar.gz) should appear.
                    _endpoints_prefix = f"{sm_prefix}endpoints/"
                    pages = paginator.paginate(
                        Bucket=self._bucket, Prefix=sm_prefix,
                        PaginationConfig={"MaxItems": 1000, "PageSize": 200},
                    )
                    for page in pages:
                        for obj in page.get("Contents", []):
                            key = obj["Key"]
                            # Only training-job model artifacts. This also implicitly
                            # excludes the sibling preprocessor.joblib sm_train now
                            # writes at the job root (it isn't a model.tar.gz), keeping
                            # that internal packaging out of the panel.
                            if not key.endswith("/model.tar.gz"):
                                continue
                            # Skip endpoint deploy artifacts (surfaced as live
                            # endpoints by the SageMaker Endpoints provider instead).
                            if key.startswith(_endpoints_prefix):
                                continue
                            # Derive a clean label: the FIRST path segment after the
                            # prefix is the job name (sagemaker/<job>/...). Show it as
                            # "<prefix><job> (model)" so the panel groups it under the
                            # SageMaker folder with a readable, per-job name.
                            rel = key[len(sm_prefix):]
                            job_seg = rel.split("/", 1)[0] if "/" in rel else rel
                            display = f"{sm_prefix}{job_seg} (model)"
                            refs.append(self._ref_from_obj(key, obj["Size"], display_name=display,
                                                           modified=obj.get("LastModified")))

                # ── Scope 4: Athena UNLOAD output (DEEP, one row per RUN) ──
                # UNLOAD writes unload/<run-id>/<part-files>. A user thinks in terms of
                # "my UNLOAD output", not individual Parquet parts, so surface ONE row
                # per RUN (the run prefix) rather than every part file — the row points
                # at the run prefix so the panel's snippet reads it with
                # read_s3_prefix(...). We aggregate the parts of each run: newest
                # LastModified + summed size, so the row shows a sensible age/size.
                # DEEP scan, capped like Scope 3 so a bucket with many past runs can't
                # list unbounded on every refresh.
                for ul_prefix in self._unload_prefixes:
                    runs: dict[str, dict] = {}
                    pages = paginator.paginate(
                        Bucket=self._bucket, Prefix=ul_prefix,
                        PaginationConfig={"MaxItems": 2000, "PageSize": 500},
                    )
                    for page in pages:
                        for obj in page.get("Contents", []):
                            key = obj["Key"]
                            if key.endswith("/"):
                                continue
                            rel = key[len(ul_prefix):]
                            if "/" not in rel:
                                continue  # a stray file directly under unload/ (none normally)
                            run_seg = rel.split("/", 1)[0]
                            agg = runs.setdefault(
                                run_seg, {"size": 0, "modified": None})
                            agg["size"] += int(obj.get("Size") or 0)
                            m = obj.get("LastModified")
                            if m is not None and (agg["modified"] is None or m > agg["modified"]):
                                agg["modified"] = m
                    for run_seg, agg in runs.items():
                        # Source id is the run PREFIX (trailing slash) so get_schema /
                        # the snippet treat it as a multi-file prefix (read_s3_prefix),
                        # not a single object. display groups it under unload/.
                        run_key = f"{ul_prefix}{run_seg}/"
                        refs.append(self._ref_from_prefix_dataset(
                            run_key, agg["size"], agg["modified"]))

                # ── Scope 5: pre-seeded partitioned-Parquet sample datasets ──
                # A seeded folder of Parquet parts (samples/web_sessions_parquet/)
                # should read as ONE dataset, not N part rows. Each configured prefix
                # is a FIXED folder (not a parent of many runs), so roll up ALL its
                # parts into a single row (summed size, newest mtime). Skip a prefix
                # with no objects (not seeded in this environment).
                for pq_prefix in self._parquet_dataset_prefixes:
                    size_total, newest = 0, None
                    for page in paginator.paginate(
                            Bucket=self._bucket, Prefix=pq_prefix,
                            PaginationConfig={"MaxItems": 1000, "PageSize": 500}):
                        for obj in page.get("Contents", []):
                            if obj["Key"].endswith("/"):
                                continue
                            size_total += int(obj.get("Size") or 0)
                            m = obj.get("LastModified")
                            if m is not None and (newest is None or m > newest):
                                newest = m
                    if size_total > 0:
                        refs.append(self._ref_from_prefix_dataset(
                            pq_prefix, size_total, newest))
            except Exception as e:
                logger.warning(f"S3 discovery failed: {e}")
            return refs

        return await asyncio.to_thread(_enumerate)

    def _session_overlay_refs(self, paginator, session_id: str) -> list[SourceRef]:
        """Scope 2 of discover(): this session's PROMOTED files directly under
        user-data/<session_id>/ (one level). Blocking (boto3 pagination) — only
        called from inside a to_thread worker. [] when there is no session."""
        refs: list[SourceRef] = []
        if not (session_id and self._session_source_prefix):
            return refs
        sess_prefix = f"{self._session_source_prefix}{session_id}/"
        for page in paginator.paginate(Bucket=self._bucket, Prefix=sess_prefix, MaxKeys=50):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                # One level within the session folder only.
                relative = key[len(sess_prefix):]
                if "/" in relative:
                    continue
                # Display-strip the <session_id> segment: show it grouped
                # under user-data/ like the shared files (source_id keeps
                # the real key).
                display = f"{self._session_source_prefix}{relative}"
                refs.append(self._ref_from_obj(key, obj["Size"], display_name=display,
                                               modified=obj.get("LastModified")))
        return refs

    async def discover_session(self, session_id: str) -> list[SourceRef]:
        """ONLY the session overlay (Scope 2). The registry caches the shared
        catalog (discover(session_id=None)) for everyone and merges this per call,
        so a session's promoted files reach its own VM and never another's."""
        import asyncio

        if not (self._bucket and session_id and self._session_source_prefix):
            return []

        def _enumerate() -> list[SourceRef]:
            try:
                s3 = aws_clients.get_client("s3", region_name=self._region)
                return self._session_overlay_refs(s3.get_paginator("list_objects_v2"), session_id)
            except Exception as e:
                logger.warning(f"S3 session-overlay discovery failed: {e}")
                return []

        return await asyncio.to_thread(_enumerate)

    def reader_docs(self) -> list[str]:
        return [
            'read_s3_csv(bucket, key) or read_s3_csv("s3://bucket/key.csv") -> df   # Read CSV from S3 (two args OR one full S3 URI)',
            'read_s3_parquet(bucket, key) or read_s3_parquet("s3://bucket/key.parquet") -> df   # Read Parquet from S3',
            'read_s3_json(bucket, key, lines=True) or read_s3_json("s3://bucket/key.json") -> df   # Read JSON/JSONL from S3',
        ]

    def sql_syntax_docs(self) -> list[str]:
        return [
            "S3 CSV files (SQL cell only): SELECT * FROM read_csv('s3://bucket/key.csv') LIMIT 10",
            "S3 JSON files (SQL cell only): SELECT * FROM read_json('s3://bucket/key.json') LIMIT 10",
            "S3 Parquet files (SQL cell only): SELECT * FROM read_parquet('s3://bucket/key.parquet') LIMIT 10",
        ]

    def _get_schema_blocking(self, source_id: str, bucket: str, key: str) -> Optional[SourceSchema]:
        """Blocking S3 schema inference (list/head/get + pandas/pyarrow parse) —
        always called via asyncio.to_thread from the async get_schema(), never on
        the event loop. Returns the SourceSchema (or None)."""
        # A PREFIX source (an UNLOAD run, key ends with '/') is a SET of part files,
        # not one object — head_object would 404. Infer the schema from the FIRST
        # part file under the prefix instead (Athena UNLOAD writes extensionless
        # Parquet, so read it as parquet). Best-effort: no readable part → no schema.
        if key.endswith("/"):
            try:
                s3 = aws_clients.get_client("s3", region_name=self._region)
                first = None
                for obj in s3.list_objects_v2(
                        Bucket=bucket, Prefix=key, MaxKeys=50).get("Contents", []):
                    k = obj["Key"]
                    if not k.endswith("/") and (obj.get("Size") or 0) > 0:
                        first = k
                        break
                if first is None:
                    return None
                # Extensionless UNLOAD parts are Parquet; a .csv/.parquet part is
                # honored by extension.
                fext = first.rsplit(".", 1)[-1].lower() if "." in first.rsplit("/", 1)[-1] else "parquet"
                if fext == "csv":
                    columns, row_count = self._schema_from_csv(s3, bucket, first)
                else:
                    columns, row_count = self._schema_from_parquet(s3, bucket, first)
                return SourceSchema(
                    source_type="s3", source_id=source_id,
                    display_name=key, columns=columns, row_count=None,
                    size=_human_size(0),
                )
            except Exception as e:
                logger.warning(f"S3 prefix schema lookup failed for {source_id}: {e}")
                return None

        try:
            s3 = aws_clients.get_client("s3", region_name=self._region)

            # Get file size
            head = s3.head_object(Bucket=bucket, Key=key)
            size_bytes = head.get("ContentLength", 0)
            if size_bytes < 1024:
                size = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size = f"{size_bytes / 1024:.1f} KB"
            else:
                size = f"{size_bytes / (1024 * 1024):.1f} MB"

            ext = key.rsplit('.', 1)[-1].lower() if '.' in key else ""
            display_name = key.rsplit('/', 1)[-1] if '/' in key else key

            detail = size   # secondary line; model artifacts append task/classes
            if ext == "parquet":
                columns, row_count = self._schema_from_parquet(s3, bucket, key)
            elif ext == "csv":
                columns, row_count = self._schema_from_csv(s3, bucket, key)
            elif ext == "json":
                columns, row_count = self._schema_from_json(s3, bucket, key)
            elif ext == "onnx":
                columns, row_count, detail = self._schema_from_onnx(s3, bucket, key, size)
            elif ext in ("joblib", "jbl", "pkl", "pickle", "sav"):
                columns, row_count, detail = self._schema_from_sklearn(s3, bucket, key, size)
            elif key.endswith("model.tar.gz"):
                columns, row_count, detail = self._schema_from_sagemaker(s3, bucket, key, size)
            else:
                columns, row_count = [], None

            return SourceSchema(
                source_type="s3",
                source_id=f"s3://{bucket}/{key}",
                display_name=display_name,
                columns=columns,
                row_count=row_count,
                size=detail,
            )

        except Exception as e:
            logger.warning(f"S3 schema lookup failed for {source_id}: {e}")
            return None

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """
        Get schema for an S3 file.
        source_id: 's3://bucket/key' or just 'key' (uses default bucket)
        """
        if source_id in self._cache:
            ts, schema = self._cache[source_id]
            if time.time() - ts < CACHE_TTL_SEC:
                return schema

        bucket, key = self._parse_s3_path(source_id)
        if not bucket or not key:
            return None

        import asyncio
        # Offload blocking boto3 so it never runs on the single proxy event loop.
        schema = await asyncio.to_thread(self._get_schema_blocking, source_id, bucket, key)
        if schema is not None:
            self._cache[source_id] = (time.time(), schema)
        return schema

    def _get_preview_blocking(self, bucket: str, key: str, limit: int) -> list[dict]:
        """Blocking S3 get_object + pandas parse — always called via
        asyncio.to_thread from the async get_preview(), never on the event loop."""
        try:
            import pandas as pd
            s3 = aws_clients.get_client("s3", region_name=self._region)
            ext = key.rsplit('.', 1)[-1].lower() if '.' in key else ""

            if ext == "csv":
                # Use S3 Select for efficient partial read
                resp = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-65536")
                chunk = resp["Body"].read().decode("utf-8", errors="replace")
                df = pd.read_csv(io.StringIO(chunk), nrows=limit)
                return df.to_dict(orient="records")
            elif ext == "parquet":
                resp = s3.get_object(Bucket=bucket, Key=key)
                df = pd.read_parquet(io.BytesIO(resp["Body"].read())).head(limit)
                return df.to_dict(orient="records")
            elif ext == "json":
                resp = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-65536")
                chunk = resp["Body"].read().decode("utf-8", errors="replace")
                df = pd.read_json(io.StringIO(chunk), lines=True, nrows=limit)
                return df.to_dict(orient="records")
        except Exception as e:
            logger.warning(f"S3 preview failed for {key}: {e}")
        return []

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """Read first N rows from the file."""
        bucket, key = self._parse_s3_path(source_id)
        if not bucket or not key:
            return []

        import asyncio
        # Offload blocking boto3 so it never runs on the single proxy event loop.
        return await asyncio.to_thread(self._get_preview_blocking, bucket, key, limit)

    def get_python_snippet(self, source_id: str) -> str:
        bucket, key = self._parse_s3_path(source_id)

        # An UNLOAD run (or any s3:// prefix) ends with '/': it's a SET of part files,
        # not one object → read the whole prefix. Athena UNLOAD writes Parquet by
        # default, and its parts are EXTENSIONLESS, so pass format='parquet' (auto-
        # detect skips files with no extension). var name derived from the run id.
        if key.endswith("/"):
            run_id = key.rstrip("/").rsplit("/", 1)[-1]
            var_name = (run_id or "unload").replace("-", "_").replace(" ", "_")
            uri = f"s3://{bucket}/{key}"
            return (f"# Read a whole Athena UNLOAD prefix (its part files) into one DataFrame.\n"
                    f"# UNLOAD writes extensionless Parquet, so pass format='parquet'.\n"
                    f"{var_name} = read_s3_prefix('{uri}', format='parquet')\n"
                    f"{var_name}.head()")

        ext = key.rsplit('.', 1)[-1].lower() if '.' in key else "csv"
        var_name = key.rsplit('/', 1)[-1].rsplit('.', 1)[0].replace('-', '_').replace(' ', '_')

        if ext == "csv":
            return f"# Read CSV from S3\n{var_name} = read_s3_csv('{bucket}', '{key}')\n{var_name}.head()"
        elif ext == "parquet":
            return f"# Read Parquet from S3\n{var_name} = read_s3_parquet('{bucket}', '{key}')\n{var_name}.head()"
        elif ext == "json":
            return f"# Read JSON from S3\n{var_name} = read_s3_json('{bucket}', '{key}')\n{var_name}.head()"
        elif ext in ("joblib", "jbl", "pkl", "pickle", "sav"):
            # Trained-model artifact → load it into a live estimator, then SCORE with
            # it (not a DataFrame read). model_info shows the raw columns it expects;
            # predict/predict_batch feed RAW rows (a train_model Pipeline one-hots
            # internally). df below is a stand-in for your feature rows.
            model_var = var_name if var_name != "model" else "clf"
            return (f"# Load a trained model from S3 and score with it\n"
                    f"{model_var} = load_model('{bucket}', '{key}')\n"
                    f"model_info({model_var})              # features/classes it expects\n"
                    f"# preds = predict({model_var}, df)               # label per row (RAW rows)\n"
                    f"# scored = predict_batch({model_var}, df, output_col='pred')  # append a column\n"
                    f"# proba = predict_proba({model_var}, df)         # class probabilities")
        elif ext == "onnx":
            # ONNX graph → framework-free inference session. onnx_predict feeds RAW
            # rows (the encoding is baked into the graph by to_onnx).
            return (f"# Load an ONNX model from S3 and run it framework-free\n"
                    f"session = load_onnx('{bucket}', '{key}')\n"
                    f"# preds = onnx_predict(session, df)   # feed RAW rows; encoding is in the graph")
        elif ext in ("xlsx", "xls"):
            return f"# Read Excel from S3\n{var_name} = read_s3_excel('{bucket}', '{key}')\n{var_name}.head()"
        elif key.endswith("model.tar.gz"):
            # A SageMaker-trained model artifact. sm_load_model accepts the S3 URI
            # directly: the artifact is a COMPLETE scikit-learn Pipeline (preprocessing
            # baked in), so it loads with a plain joblib.load — no rewrap — and you
            # feed RAW rows to predict() exactly like a train_model() model (no job
            # handle needed). An xgboost/lightgbm estimator inside it is auto-installed.
            uri = f"s3://{bucket}/{key}"
            return (f"# Load a SageMaker-trained model straight from its S3 artifact\n"
                    f"model = sm_load_model('{uri}')\n"
                    f"model_info(model)                     # features/classes it expects\n"
                    f"# preds = predict(model, df)                    # label per row (RAW rows)\n"
                    f"# scored = predict_batch(model, df, output_col='pred')  # append a column\n"
                    f"# proba = predict_proba(model, df)              # class probabilities")
        else:
            return f"# Read s3://{bucket}/{key}\n{var_name} = read_s3_csv('{bucket}', '{key}')\n{var_name}.head()"

    def get_sql_snippet(self, source_id: str) -> str:
        bucket, key = self._parse_s3_path(source_id)

        # An UNLOAD run (or any s3:// prefix, key ends with '/') is a SET of Parquet
        # parts. DuckDB reads them with a GLOB — read_parquet('s3://…/*') — and we
        # must name read_parquet explicitly because Athena UNLOAD parts are
        # EXTENSIONLESS (no .parquet to auto-detect). This is the SQL mirror of the
        # Python read_s3_prefix(..., format='parquet') snippet.
        if key.endswith("/"):
            return (f"-- Query the whole UNLOAD run (its Parquet parts) with a glob.\n"
                    f"-- read_parquet is explicit because the parts are extensionless.\n"
                    f"SELECT * FROM read_parquet('s3://{bucket}/{key}*') LIMIT 100")

        ext = key.rsplit('.', 1)[-1].lower() if '.' in key else "csv"
        if ext == "csv":
            return f"SELECT * FROM read_csv('s3://{bucket}/{key}') LIMIT 100"
        elif ext == "parquet":
            return f"SELECT * FROM read_parquet('s3://{bucket}/{key}') LIMIT 100"
        elif ext == "json":
            return f"SELECT * FROM read_json('s3://{bucket}/{key}') LIMIT 100"
        elif ext in _MODEL_EXTS or key.endswith("model.tar.gz"):
            # A model artifact isn't SQL-queryable (the panel hides the SQL button for
            # it). If this is reached anyway, point at the Python model snippet.
            return ("-- A trained model isn't queryable with SQL.\n"
                    "-- Use the Python insert instead: load_model(...) + predict(...).")
        return f"-- Unsupported file type: {ext}"

    def _parse_s3_path(self, source_id: str) -> tuple[str, str]:
        """Parse 's3://bucket/key' or 'key' into (bucket, key)."""
        if source_id.startswith("s3://"):
            parts = source_id[5:].split("/", 1)
            return parts[0], parts[1] if len(parts) > 1 else ""
        # Assume default bucket
        return self._bucket, source_id

    # --- Deletion (panel "delete file" for user-owned prefixes) -----------
    def is_deletable(self, source_id: str) -> bool:
        """
        True only if source_id points at an object in the artifact bucket whose key
        is under a configured deletable prefix. Guards the delete endpoint so users
        can't remove sample data or arbitrary keys.
        """
        if not self._deletable_prefixes:
            return False
        bucket, key = self._parse_s3_path(source_id)
        if not key:
            return False
        if self._bucket and bucket and bucket != self._bucket:
            return False
        return any(key.startswith(p) for p in self._deletable_prefixes)

    def delete(self, source_id: str) -> None:
        """
        Delete an S3 source. Caller MUST validate is_deletable() first.
        Raises on failure (so the route can surface a 500).

        Handles BOTH shapes the panel surfaces:
          • a single object  (key does NOT end with '/')  → one delete_object, and
          • a PREFIX dataset  (key ends with '/': an Athena UNLOAD run or a seeded
            partitioned Parquet folder, built by _ref_from_prefix_dataset) → delete
            EVERY object under the prefix in batched delete_objects calls.
        The prefix case is why a delete used to appear to "hang": delete_object on a
        key ending in '/' only removes the (empty) folder marker and leaves all the
        part files, so the next discovery re-listed the dataset and the row (and its
        deleting-spinner) never cleared. Deleting the whole prefix makes the dataset
        actually disappear, so discovery no longer finds it and the row clears.
        """
        bucket, key = self._parse_s3_path(source_id)
        s3 = aws_clients.get_client("s3", region_name=self._region)
        # Drop any cached schema for the removed source (the provider instance,
        # and so its cache, is shared across requests).
        self._cache.pop(source_id, None)
        self._cache.pop(f"s3://{bucket}/{key}", None)

        if not key.endswith("/"):
            # Single object — the original fast path.
            s3.delete_object(Bucket=bucket, Key=key)
            return

        # Prefix dataset — delete all part files (and the folder marker, if any).
        # Page the listing and delete in batches of up to 1000 (the delete_objects
        # cap). Collect any per-key errors and raise so the route surfaces a 500.
        paginator = s3.get_paginator("list_objects_v2")
        batch: list[dict] = []
        errors: list[dict] = []
        deleted = 0

        def _flush():
            nonlocal deleted
            if not batch:
                return
            resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            errors.extend(resp.get("Errors", []) or [])
            deleted += len(batch)
            batch.clear()

        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    _flush()
        _flush()

        if errors:
            raise RuntimeError(f"deleted {deleted - len(errors)}/{deleted} objects under "
                               f"{source_id}; {len(errors)} failed (first: {errors[0]})")
        logger.info(f"Deleted prefix dataset {source_id} ({deleted} object(s))")

    # --- Promotion (chat attachment → session-scoped data source) ----------
    # Tabular formats that make sense to register as a queryable data source. png/
    # pdf/txt are excluded — they aren't join targets.
    PROMOTABLE_EXTS = {"csv", "tsv", "xls", "xlsx", "parquet", "json"}

    def promote_attachment(self, source_uri: str, session_id: str) -> dict:
        """COPY a chat attachment into this session's data-source area so it becomes
        a discoverable, queryable source.

        Source must be an object under attachments/<session_id>/ in THIS bucket (a
        file the same session uploaded — no cross-session or arbitrary-key copies),
        and a tabular type (PROMOTABLE_EXTS). The object is COPIED (never moved — the
        original attachment stays so the chat transcript chip still resolves) to:
            user-data/<session_id>/<name>
        deduplicating the leaf name within that session folder if it already exists.

        Returns {s3_uri, key, name} of the new object. Raises ValueError for a
        validation failure (bad prefix/type/session) and lets S3 errors propagate.
        """
        bucket, key = self._parse_s3_path(source_uri)
        if self._bucket and bucket and bucket != self._bucket:
            raise ValueError("attachment is not in this workspace's bucket")
        if not session_id:
            raise ValueError("session_id is required to promote an attachment")

        # Guard: only promote objects the SAME session uploaded as a chat attachment.
        expected_prefix = f"attachments/{session_id}/"
        if not key.startswith(expected_prefix):
            raise ValueError(
                f"can only promote a chat attachment from this session "
                f"(expected key under {expected_prefix})"
            )

        name = key.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in self.PROMOTABLE_EXTS:
            raise ValueError(
                f"only tabular files can be promoted (got .{ext}); "
                f"allowed: {', '.join(sorted(self.PROMOTABLE_EXTS))}"
            )

        s3 = aws_clients.get_client("s3", region_name=self._region)
        dest_dir = f"{self._session_source_prefix}{session_id}/"
        dest_key = self._dedupe_key(s3, dest_dir, name)

        s3.copy_object(
            Bucket=self._bucket,
            CopySource={"Bucket": self._bucket, "Key": key},
            Key=dest_key,
        )
        dest_uri = f"s3://{self._bucket}/{dest_key}"
        logger.info(f"Promoted attachment {source_uri} → {dest_uri} (session {session_id[:8]}...)")
        return {"s3_uri": dest_uri, "key": dest_key, "name": dest_key.rsplit("/", 1)[-1]}

    def _dedupe_key(self, s3, dest_dir: str, name: str) -> str:
        """Return dest_dir+name, or a suffixed variant (name (2).ext, ...) if the
        object already exists — so re-promoting the same filename doesn't overwrite."""
        base_key = f"{dest_dir}{name}"

        def _exists(k: str) -> bool:
            try:
                s3.head_object(Bucket=self._bucket, Key=k)
                return True
            except Exception:
                return False

        if not _exists(base_key):
            return base_key
        stem, dot, ext = name.rpartition(".")
        stem = stem if dot else name
        for i in range(2, 100):
            cand = f"{dest_dir}{stem} ({i}){('.' + ext) if dot else ''}"
            if not _exists(cand):
                return cand
        # Extremely unlikely fallback: timestamp suffix.
        import time as _t
        return f"{dest_dir}{stem}-{int(_t.time())}{('.' + ext) if dot else ''}"

    def _schema_from_csv(self, s3, bucket: str, key: str) -> tuple[list[ColumnInfo], Optional[int]]:
        """Read CSV header + sample rows to infer schema."""
        import pandas as pd
        try:
            resp = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-32768")
            chunk = resp["Body"].read().decode("utf-8", errors="replace")
            df = pd.read_csv(io.StringIO(chunk), nrows=5)

            columns = []
            for col in df.columns:
                dtype = self._pandas_dtype_to_simple(df[col].dtype)
                sample = str(df[col].iloc[0]) if len(df) > 0 else ""
                columns.append(ColumnInfo(name=str(col), dtype=dtype, sample=sample[:50]))

            return columns, None  # Row count unknown without full scan
        except Exception:
            return [], None

    def _schema_from_parquet(self, s3, bucket: str, key: str) -> tuple[list[ColumnInfo], Optional[int]]:
        """Read Parquet metadata for schema (no full download needed)."""
        import pyarrow.parquet as pq
        try:
            resp = s3.get_object(Bucket=bucket, Key=key)
            pf = pq.ParquetFile(io.BytesIO(resp["Body"].read()))
            schema = pf.schema_arrow
            row_count = pf.metadata.num_rows

            columns = []
            # Get sample values from first row group
            first_batch = pf.read_row_group(0).to_pandas().head(1)
            for i, field in enumerate(schema):
                dtype = self._arrow_dtype_to_simple(str(field.type))
                sample = ""
                if len(first_batch) > 0 and field.name in first_batch.columns:
                    sample = str(first_batch[field.name].iloc[0])[:50]
                columns.append(ColumnInfo(name=field.name, dtype=dtype, sample=sample))

            return columns, row_count
        except Exception:
            return [], None

    def _schema_from_json(self, s3, bucket: str, key: str) -> tuple[list[ColumnInfo], Optional[int]]:
        """Read first few lines of JSONL to infer schema."""
        import pandas as pd
        try:
            resp = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-32768")
            chunk = resp["Body"].read().decode("utf-8", errors="replace")
            df = pd.read_json(io.StringIO(chunk), lines=True, nrows=5)

            columns = []
            for col in df.columns:
                dtype = self._pandas_dtype_to_simple(df[col].dtype)
                sample = str(df[col].iloc[0]) if len(df) > 0 else ""
                columns.append(ColumnInfo(name=str(col), dtype=dtype, sample=sample[:50]))

            return columns, None
        except Exception:
            return [], None

    # --- Model-artifact schemas (input columns to feed + classes it predicts) -----
    # A trained model isn't a table, but users still want "what do I send / what does
    # it output" in the panel. We extract that from the artifact at rest:
    #   .onnx           → graph input names/types (+ stamped classes)   [no code exec]
    #   .joblib/.pkl    → sklearn feature_names_in_ / classes_          [unpickles — our bucket]
    #   model.tar.gz    → sibling preprocessor.joblib feat_cols/classes [our SM training output]
    # Each returns (columns, row_count=None, detail) where detail is the size line
    # augmented with task/classes. On any failure they degrade to (size-only, no cols).

    @staticmethod
    def _classes_detail(size: str, task: str, classes) -> str:
        """Compose the schema's secondary line: '1.7 MB · classification · classes: 0, 1'."""
        bits = [size] if size else []
        if task:
            bits.append(str(task))
        if classes:
            shown = ", ".join(str(c) for c in list(classes)[:6])
            if len(classes) > 6:
                shown += f", … (+{len(classes) - 6})"
            bits.append(f"classes: {shown}")
        return " · ".join(bits)

    # ONNX TensorProto elem_type → short display type (the codes we actually emit).
    _ONNX_ELEM_TYPES = {1: "float", 7: "int64", 8: "string", 9: "bool",
                        10: "float16", 11: "double", 6: "int32"}

    def _schema_from_onnx(self, s3, bucket: str, key: str, size: str):
        """Input columns + classes from an ONNX graph (framework-free, no code exec).
        Uses the lightweight `onnx` package (protobuf schema reader) — NOT onnxruntime,
        which the proxy deliberately doesn't bundle (it's a heavy native runtime, only
        needed to RUN a model, not read its graph). Each graph input is a raw feature
        column; classes come from the stamped microvm_classes metadata_props (present
        on to_onnx exports going forward). Degrades to size-only if `onnx` is absent."""
        try:
            import onnx  # schema/proto only — no inference runtime
            blob = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            model = onnx.load_from_string(blob)
            columns = []
            for i in model.graph.input:
                et = i.type.tensor_type.elem_type
                dtype = self._ONNX_ELEM_TYPES.get(et, "tensor")
                columns.append(ColumnInfo(name=i.name, dtype=dtype, sample="input"))
            classes = None
            meta = {p.key: p.value for p in model.metadata_props}
            if "microvm_classes" in meta:
                try:
                    import json as _json
                    classes = _json.loads(meta["microvm_classes"])
                except Exception:
                    classes = [c for c in str(meta["microvm_classes"]).split(",") if c]
            task = "classification" if classes else ""
            return columns, None, self._classes_detail(size, task, classes)
        except Exception as e:
            logger.info(f"ONNX schema read failed for {key}: {e}")
            return [], None, size

    def _schema_from_sklearn(self, s3, bucket: str, key: str, size: str):
        """Input columns + classes from a saved sklearn model (feature_names_in_ /
        classes_). This UNPICKLES the artifact server-side — acceptable because it's
        this workspace's own trusted artifact bucket; still isolated in try/except so a
        bad/foreign pickle degrades to size-only rather than raising."""
        try:
            import joblib
            blob = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            model = joblib.load(io.BytesIO(blob))
            names = getattr(model, "feature_names_in_", None)
            columns = []
            if names is not None:
                columns = [ColumnInfo(name=str(n), dtype="feature", sample="input")
                           for n in list(names)]
            classes = getattr(model, "classes_", None)
            classes = [str(c) for c in list(classes)] if classes is not None else None
            task = "classification" if classes is not None else (
                "regression" if getattr(model, "_estimator_type", "") == "regressor" else "")
            return columns, None, self._classes_detail(size, task, classes)
        except Exception as e:
            logger.info(f"sklearn schema read failed for {key}: {e}")
            return [], None, size

    def _schema_from_sagemaker(self, s3, bucket: str, key: str, size: str):
        """Input columns + classes for a SageMaker training model.tar.gz — read from
        one of two sources, depending on how the model was trained:

        1. sklearn jobs (algorithm='sklearn') write a SELF-CONTAINED Pipeline plus a
           tiny microvm_meta.json sidecar INSIDE model.tar.gz. We read the sidecar
           (feat_cols/task/classes) straight from the tar — no sibling, no unpickle,
           no code execution.
        2. XGBoost jobs write only a bare booster in model.tar.gz, so the feature
           names live in the sibling preprocessor.joblib sm_train persists at the job
           root (<sm_prefix><job>/preprocessor.joblib).

        We try the in-tar sklearn sidecar first, then fall back to the XGBoost
        sibling. Old XGBoost jobs (pre-persistence) have neither → degrade to
        size-only."""
        import json
        import tarfile

        # --- sklearn path: microvm_meta.json inside model.tar.gz --------------------
        try:
            blob = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                meta_m = next(
                    (m for m in tar.getmembers()
                     if m.isfile() and os.path.basename(m.name) == "microvm_meta.json"),
                    None,
                )
                if meta_m is not None:
                    meta = json.loads(tar.extractfile(meta_m).read().decode("utf-8"))
                    feat_cols = meta.get("feat_cols") or []
                    columns = [ColumnInfo(name=str(c), dtype="feature", sample="input")
                               for c in feat_cols]
                    classes = meta.get("classes")
                    classes = [str(c) for c in classes] if classes else None
                    task = meta.get("task") or ""
                    return columns, None, self._classes_detail(size, task, classes)
        except Exception as e:  # noqa: BLE001 — fall through to the XGBoost sibling
            logger.info(f"SageMaker sklearn-meta read skipped for {key}: {e}")

        # --- XGBoost path: sibling preprocessor.joblib at the job root --------------
        try:
            import joblib
            # Job root = everything before the first '/output/' (mirrors sm_train's prefix).
            root = key.split("/output/", 1)[0]
            pre_key = f"{root}/preprocessor.joblib"
            blob = s3.get_object(Bucket=bucket, Key=pre_key)["Body"].read()
            bundle = joblib.load(io.BytesIO(blob))
            feat_cols = bundle.get("feat_cols") or []
            columns = [ColumnInfo(name=str(c), dtype="feature", sample="input") for c in feat_cols]
            classes = bundle.get("classes")
            classes = [str(c) for c in list(classes)] if classes is not None else None
            task = bundle.get("task") or ""
            return columns, None, self._classes_detail(size, task, classes)
        except Exception as e:
            logger.info(f"SageMaker schema read failed for {key} (no sibling preprocessor?): {e}")
            return [], None, size

    @staticmethod
    def _pandas_dtype_to_simple(dtype) -> str:
        """Map pandas dtype to simple display type."""
        from app.notebook.dtypes import normalize_dtype
        return normalize_dtype(str(dtype))

    @staticmethod
    def _arrow_dtype_to_simple(dtype: str) -> str:
        """Map Arrow dtype string to simple display type."""
        from app.notebook.dtypes import normalize_dtype
        return normalize_dtype(dtype)
