"""
Global Data Source Entity Discovery.

Part of: proxy.notebook.ai

Phase 1 of the Data Intel redesign (see design discussion): enumerates the
data sources available to all users — S3 files, Athena tables, DynamoDB
tables — and generates one markdown profile document per entity: an
AI-inferred business description, schema, describe()-style stats, null/
duplicate checks, and other data quality flags.

Also includes the local-file counterpart (further down in this file): local
/tmp files are workbook-scoped (unique to one session), so they're profiled
separately — via the session's own live VM instead of independent boto3
calls — but reuse the exact same markdown-generation step. See the "LOCAL
FILE ENTITIES" section below. Both feed into Workbook Intel generation
(proxy/notebook/ai/workbook_intel.py, "Plan A").

Global entity discovery is entirely independent of any user session or
MicroVM — it runs directly in the proxy process using boto3 + pandas on
small samples, plus a single one-shot Bedrock call per entity (no agent/
tool-calling loop) to turn the computed stats into a readable profile.

Storage:
    Global:  S3 data-catalog/entities/{sanitized_source_id}.md
             SQLite data_source_entities table (see proxy/storage/sqlite_db.py)
    Local:   S3 sessions/{session_id}/local-entities/{filename}.md
             SQLite local_file_entities table, keyed by (session_id, filepath)
    Both track a per-source-type "change signal" so re-running this only
    re-profiles entities that are missing or have changed since last discovered.

Concurrency: bounded by a small thread pool (default 8 for global, 3 for
local since local discovery shares the session's own live VM) so we never
fire more than a handful of concurrent Bedrock/VM calls at once. Bedrock
throttling (not thread count) is the real ceiling; the client retries with
backoff, so occasional throttles self-heal.

Run standalone:
    python -m proxy.notebook.ai.entity_discovery
    FORCE_REDISCOVER=1 python -m proxy.notebook.ai.entity_discovery   # ignore change signals

Or import discover_all_global_entities() from a route or future scheduler.
"""

import os
import io
import json
import logging
import concurrent.futures
from decimal import Decimal

import boto3

from proxy.notebook.ai.prompts import ENTITY_DISCOVERY_PROMPT, KB_ENTITY_DISCOVERY_PROMPT
from proxy.platform.errors import emit_error_metric
# Per-model inference params: entity-discovery runs under the proxy (repo root is
# importable), so it uses the canonical modelcaps package directly — same as the
# proxy's own call sites. This drops `temperature` for models that reject it
# (e.g. Haiku 5.5) and disables verbose thinking, instead of hardcoding params.
from modelcaps import build_inference_config, build_additional_request_fields
# Model + region from the proxy's single definition (BEDROCK_MODEL_ID / BEDROCK_REGION).
from proxy.notebook.ai.constants import AI_MODEL_ID, AI_REGION, require_model_id
logger = logging.getLogger(__name__)

# Bounded concurrency for parallel per-entity discovery. Each task makes one Bedrock
# call (the dominant cost), so the practical ceiling is Bedrock throttling, not threads.
# 8 clears the ~12-entity workload in ~2 waves while staying under Claude's per-account
# TPS (firing all 12 at once tends to trigger ThrottlingException + backoff, which is
# slower, not faster). Override with ENTITY_DISCOVERY_CONCURRENCY if your account has
# higher/lower Bedrock limits.
MAX_CONCURRENT_DISCOVERY = int(os.environ.get("ENTITY_DISCOVERY_CONCURRENCY", "8"))

# How many rows/items to sample per entity for profiling (keeps this cheap and fast —
# we're profiling for a catalog description, not doing exhaustive analysis)
SAMPLE_ROW_LIMIT = 500

# NOTE: Source enumeration + scope (S3 prefixes, DynamoDB name filters, Athena
# databases) now live in the data source provider registry
# (proxy/platform/datasources) and are shared with the /datasources endpoints
# and the Data Sources panel. See list_global_entities() below.

_bedrock_client = None


def _get_bedrock_client():
    """Cached Bedrock runtime client for one-shot (non-agent) calls."""
    global _bedrock_client
    if _bedrock_client is None:
        from botocore.config import Config
        from proxy.notebook.ai.constants import BEDROCK_MAX_RETRIES, BEDROCK_READ_TIMEOUT, BEDROCK_CONNECT_TIMEOUT
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=AI_REGION,
            config=Config(
                retries={"max_attempts": BEDROCK_MAX_RETRIES, "mode": "standard"},
                read_timeout=BEDROCK_READ_TIMEOUT,
                connect_timeout=BEDROCK_CONNECT_TIMEOUT,
            ),
        )
    return _bedrock_client


# Source types that are registered + queryable but are INTENTIONALLY not AI-profiled
# (no columns/rows/business description to generate — profiling would be meaningless):
#   • local            — session-scoped /tmp files, profiled via the local-file path
#   • cloudwatch        — log groups: schemaless event streams, not a table
#   • sagemaker_endpoint— a callable model, not a dataset
# These are skipped SILENTLY. The build_entities() `else` branch still logs for a
# genuinely UNKNOWN type (a newly registered source with no handling yet), which is a
# real "add a profiling branch" signal — not the noise these would otherwise produce.
NON_PROFILED_SOURCE_TYPES = frozenset({"local", "cloudwatch", "sagemaker_endpoint"})


# =============================================================================
# STEP 1: ENUMERATE GLOBAL ENTITIES (pure boto3 — no VM, no pandas needed)
# =============================================================================

def list_global_entities(bucket: str, athena_db: str, region: str) -> list[dict]:
    """
    Enumerate all user-non-specific data sources via the data source provider
    registry (S3 files, Athena tables, DynamoDB tables). Does NOT include local
    /tmp files (they're session-scoped and profiled separately).

    Enumeration + scope now come from the registry providers (their discover()
    methods, scoped by the DATASOURCE_* env vars) — the single source of truth
    shared with the /datasources endpoints and the Data Sources panel. This
    function just maps each generic SourceRef into the type-specific entity dict
    shape the change-signal + profiling steps below expect.

    Returns a list of entity dicts: {source_id, source_type, ...type-specific fields}
    """
    from proxy.platform.datasources import registry

    refs = registry.discover_all_sync()
    entities: list[dict] = []
    for r in refs:
        st = r.source_type
        extra = r.extra or {}
        # Intentionally-not-profiled types (local /tmp files, CloudWatch log groups,
        # SageMaker endpoints) are skipped SILENTLY — there's nothing tabular to
        # profile. Keeps the discovery log clean (CloudWatch surfaces one group per
        # VM tier, so this would otherwise log a skip line for each).
        if st in NON_PROFILED_SOURCE_TYPES:
            continue

        if st == "s3":
            # A PREFIX dataset (Athena UNLOAD run / seeded partitioned Parquet folder,
            # built by s3.py's _ref_from_prefix_dataset) is a SET of part files surfaced
            # as ONE source — source_id ends in '/', there's no single `extension`/`key`,
            # and extra carries kind='parquet_prefix' + is_prefix=True. The flat
            # single-file branch below would skip it (extension is None) and couldn't
            # profile it anyway (get_object on a prefix 404s). Admit it here as its own
            # 's3_prefix' entity so compute_change_signal + fetch_and_profile read the
            # part files (see their s3_prefix branches). This is what gives the parquet
            # prefix (e.g. samples/web_sessions_parquet/) the ✨ entity-doc sparkle.
            if extra.get("is_prefix") or extra.get("kind") == "parquet_prefix":
                entities.append({
                    "source_id": r.source_id,       # s3://bucket/prefix/  (trailing slash)
                    "source_type": "s3_prefix",
                    "bucket": extra.get("bucket"),
                    "prefix": extra.get("key"),     # the prefix (ends with '/'), see _ref_from_prefix_dataset
                })
                continue
            # Keep the previous extension filter (only tabular files are profiled).
            if extra.get("extension") not in ("csv", "parquet", "json"):
                continue
            entities.append({
                "source_id": r.source_id,           # s3://bucket/key
                "source_type": "s3",
                "bucket": extra.get("bucket"),
                "key": extra.get("key"),
                "extension": extra.get("extension"),
            })
        elif st == "dynamodb":
            # source_id must carry the "dynamodb." prefix so it matches the VM
            # data-catalog convention used when cross-referencing entity docs.
            name = extra.get("table_name") or extra.get("name") or r.source_id
            entities.append({
                "source_id": f"dynamodb.{name}",
                "source_type": "dynamodb",
                "table_name": name,
            })
        elif st == "athena":
            entities.append({
                "source_id": r.source_id,           # {database}.{table}
                "source_type": "athena",
                "database": extra.get("database"),
                "table_name": extra.get("table_name") or extra.get("name"),
                # S3 Tables carry the federated Athena data-catalog name so the
                # sample query can target it (else SCHEMA_NOT_FOUND). Empty/absent
                # for regular Glue tables (default AwsDataCatalog).
                "catalog": extra.get("catalog", ""),
                "table_format": extra.get("table_format", ""),
                "s3_location": extra.get("s3_location", ""),
                "glue_update_time": extra.get("glue_update_time"),
            })
        elif st == "aurora":
            # source_id is the 3-part {database}.{schema}.{table} — the SAME id the
            # catalog, the Data Sources panel, and _enrich_catalog_entries use, so
            # the generated doc cross-references correctly (sparkle shows on the view).
            entities.append({
                "source_id": r.source_id,           # {database}.{schema}.{table}
                "source_type": "aurora",
                "database": extra.get("database"),
                "schema": extra.get("schema"),
                "table_name": extra.get("table_name") or extra.get("name"),
                "column_count": extra.get("column_count"),
            })
        elif st == "bedrock_kb":
            # source_id is the bare KB id (e.g. "AVSDQZX2CR") — the SAME id the
            # provider emits and _enrich_catalog_entries looks up (no prefix, unlike
            # DynamoDB), so the doc keys line up and the sparkle shows on the KB row.
            # A KB is retrieve-only (no rows/columns): fetch_and_profile handles it
            # via control-plane facts + sampled retrievals, not pandas/SQL.
            entities.append({
                "source_id": r.source_id,           # KB id
                "source_type": "bedrock_kb",
                "kb_id": extra.get("kb_id") or r.source_id,
                "kb_name": r.display_name,
            })
        else:
            # An UNKNOWN source type — registered as queryable but with no profiling
            # branch here AND not in NON_PROFILED_SOURCE_TYPES (the intentionally-
            # skipped set, handled silently at the top of the loop). This is a real
            # "someone added a source type and forgot to wire profiling" signal, so
            # we log it. If the type genuinely has nothing to profile, add it to
            # NON_PROFILED_SOURCE_TYPES; otherwise add a fetch/profile branch.
            logger.info(f"[entity-discovery] Skipping unsupported source type for profiling: {st}")

    return entities


# =============================================================================
# STEP 2: CHEAP CHANGE-SIGNAL FINGERPRINTING (no data read required)
# =============================================================================

def compute_change_signal(entity: dict, region: str) -> dict:
    """
    Compute a cheap, source-type-specific fingerprint used to detect whether
    an entity needs re-discovery, without reading/profiling the actual data.

    - S3: ETag + LastModified (the same signal AWS Glue's own incremental
      crawler uses to detect changed objects cheaply).
    - Athena: Glue's table UpdateTime only reflects DDL/table-definition
      changes, not new data files, so we also check the underlying S3
      location's most recent object LastModified — without running a
      (billed) Athena query.
    - DynamoDB: no cheap native "changed since" signal exists without
      enabling DynamoDB Streams (a heavier CDC mechanism, worth adding when
      this moves to the cloud). ItemCount/TableSizeBytes from describe_table
      are free but only refresh ~every 6h — a weak best-effort proxy.
    """
    source_type = entity["source_type"]

    if source_type == "s3":
        s3 = boto3.client("s3", region_name=region)
        head = s3.head_object(Bucket=entity["bucket"], Key=entity["key"])
        return {
            "etag": head.get("ETag", "").strip('"'),
            "last_modified": head["LastModified"].isoformat(),
            "size_bytes": head.get("ContentLength", 0),
        }

    if source_type == "s3_prefix":
        # A SET of part files under one prefix (UNLOAD run / seeded Parquet folder).
        # No single object to head — fingerprint the collection: part count, total
        # size, and the newest LastModified. Any added/removed/rewritten part moves
        # one of these, so an unchanged folder is cheaply skipped on re-run.
        s3 = boto3.client("s3", region_name=region)
        part_count = 0
        total_size = 0
        latest = None
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=entity["bucket"], Prefix=entity["prefix"]):
            for o in page.get("Contents", []):
                if o["Key"].endswith("/"):
                    continue  # skip the folder placeholder key itself
                part_count += 1
                total_size += o.get("Size", 0)
                lm = o.get("LastModified")
                if lm and (latest is None or lm > latest):
                    latest = lm
        return {
            "part_count": part_count,
            "size_bytes": total_size,
            "last_modified": latest.isoformat() if latest else None,
        }

    if source_type == "athena":
        signal = {"glue_update_time": entity.get("glue_update_time")}
        location = entity.get("s3_location", "")
        if location.startswith("s3://"):
            loc_bucket, _, prefix = location[5:].partition("/")
            try:
                s3 = boto3.client("s3", region_name=region)
                resp = s3.list_objects_v2(Bucket=loc_bucket, Prefix=prefix, MaxKeys=100)
                latest = max((o["LastModified"] for o in resp.get("Contents", [])), default=None)
                signal["data_last_modified"] = latest.isoformat() if latest else None
            except Exception:
                signal["data_last_modified"] = None
        return signal

    if source_type == "dynamodb":
        ddb = boto3.client("dynamodb", region_name=region)
        desc = ddb.describe_table(TableName=entity["table_name"])["Table"]
        return {
            "item_count": desc.get("ItemCount", 0),
            "table_size_bytes": desc.get("TableSizeBytes", 0),
        }

    if source_type == "aurora":
        # The v_nb_* views are stable schema objects (their definition rarely
        # changes), and there's no cheap native "data changed" signal over the Data
        # API without extra queries. Use the fully-qualified name + column count as
        # the fingerprint: re-profile only when the view's shape changes (a schema
        # migration) rather than on every data write. Both are already known from
        # discovery (no extra query/data read here — keeps change detection cheap).
        return {
            "source_id": entity.get("source_id"),
            "column_count": entity.get("column_count"),
        }

    if source_type == "bedrock_kb":
        # A KB's content changes only when it's re-ingested. The cheap native
        # signals are the KB's own updatedAt and the latest ingestion job's
        # updatedAt + doc counts (all from free control-plane calls, no vector
        # reads). Re-profile only when one of those moves — so a launch re-run
        # skips an unchanged KB, just like S3/Athena.
        kb_id = entity.get("kb_id") or entity["source_id"]
        agent = boto3.client("bedrock-agent", region_name=region)
        signal = {"kb_id": kb_id}
        try:
            kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
            signal["kb_updated_at"] = str(kb.get("updatedAt", ""))
            signal["status"] = kb.get("status", "")
        except Exception:
            pass
        try:
            for ds in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries", []):
                dsid = ds.get("dataSourceId")
                jobs = agent.list_ingestion_jobs(
                    knowledgeBaseId=kb_id, dataSourceId=dsid, maxResults=1
                ).get("ingestionJobSummaries", [])
                if jobs:
                    j = jobs[0]
                    stats = j.get("statistics", {}) or {}
                    signal[f"ingest_{dsid}"] = {
                        "updated_at": str(j.get("updatedAt", "")),
                        "status": j.get("status", ""),
                        "docs": stats.get("numberOfDocumentsScanned"),
                    }
        except Exception:
            pass
        return signal

    return {}


def _entity_needs_discovery(entity: dict, region: str, storage, force: bool) -> tuple[bool, dict]:
    """Returns (needs_discovery, current_change_signal)."""
    current_signal = compute_change_signal(entity, region)
    if force:
        return True, current_signal
    existing = storage.entity_get(entity["source_id"])
    if not existing or existing.get("status") != "ready":
        return True, current_signal
    stored_signal = existing.get("change_signal") or {}
    return (stored_signal != current_signal), current_signal


# =============================================================================
# STEP 3: PROFILE A SAMPLE OF THE ENTITY'S DATA
# =============================================================================

def _convert_dynamo_decimals(items: list[dict]) -> list[dict]:
    """DynamoDB returns Decimal for all numeric attributes — convert to
    float/int so pandas treats them as numeric columns instead of object,
    otherwise describe()/numeric profiling silently skips them."""
    def convert(v):
        if isinstance(v, Decimal):
            return float(v) if v % 1 else int(v)
        if isinstance(v, list):
            return [convert(x) for x in v]
        if isinstance(v, dict):
            return {k: convert(x) for k, x in v.items()}
        return v
    return [{k: convert(v) for k, v in item.items()} for item in items]


def _profile_dataframe(df) -> dict:
    """Compute describe/nulls/duplicates/dtypes stats for a sampled DataFrame.
    All values are cast to native Python types so the result is directly
    JSON-serializable (numpy scalar types are not)."""
    total_rows = len(df)
    null_counts = df.isnull().sum()

    from app.notebook.dtypes import normalize_dtype

    stats = {
        "sampled_rows": total_rows,
        "columns": [
            {
                "name": col,
                "dtype": normalize_dtype(str(df[col].dtype)),
                "null_pct": round(float(null_counts[col]) / total_rows * 100, 1) if total_rows else 0.0,
                "unique_count": int(df[col].nunique()),
                "sample_values": [str(v) for v in df[col].dropna().unique()[:5].tolist()],
            }
            for col in df.columns
        ],
        "duplicate_row_pct": round(float(df.duplicated().sum()) / total_rows * 100, 1) if total_rows else 0.0,
    }

    numeric_df = df.select_dtypes(include="number")
    if not numeric_df.empty:
        desc = numeric_df.describe().to_dict()
        stats["numeric_describe"] = {
            col: {k: (round(float(v), 3) if isinstance(v, (int, float)) else v) for k, v in vals.items()}
            for col, vals in desc.items()
        }

    return stats


def _query_athena_sample(database: str, table: str, region: str, catalog: str = ""):
    """Run a bounded Athena query and return the result as a DataFrame."""
    import time
    import pandas as pd

    athena = boto3.client("athena", region_name=region)
    sts = boto3.client("sts", region_name=region)
    account = sts.get_caller_identity()["Account"]
    bucket = os.environ.get("ARTIFACT_BUCKET", f"microvm-sandbox-artifacts-{account}-{region}")
    workgroup = os.environ.get("ATHENA_WORKGROUP", "microvm-demo")

    # S3 Tables live in a federated Athena data catalog reached via the execution
    # context (catalog + namespace); the table must be referenced by its PLAIN name
    # (the inline "catalog"."db"."table" form doesn't parse). Regular Glue tables
    # (no catalog) use the default AwsDataCatalog and the "database.table" form.
    #
    # RANDOM sample (ORDER BY rand()) — NOT a plain LIMIT. A plain LIMIT returns the
    # first physical rows, which for many tables correlates with a column (e.g. our
    # order_events is written in status-phase batches, so the first 500 rows are all
    # 'placed/paid' with empty carrier/tracking). That biased head makes the profiler
    # wrongly report low cardinality / "empty" columns. rand() gives a representative
    # cross-section of every value. Cheap on our demo-scale tables.
    tbl_ref = table if catalog else f"{database}.{table}"
    query_context = {"Database": database}
    if catalog:
        query_context["Catalog"] = catalog
    query_sql = f"SELECT * FROM {tbl_ref} ORDER BY rand() LIMIT {SAMPLE_ROW_LIMIT}"
    response = athena.start_query_execution(
        QueryString=query_sql,
        QueryExecutionContext=query_context,
        WorkGroup=workgroup,
        ResultConfiguration={"OutputLocation": f"s3://{bucket}/athena-results/"},
    )
    query_id = response["QueryExecutionId"]

    state = "RUNNING"
    status = None
    for _ in range(60):  # up to ~30s
        status = athena.get_query_execution(QueryExecutionId=query_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)

    if state != "SUCCEEDED":
        reason = (status["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
                  if status else "Timed out waiting for query")
        raise RuntimeError(f"Athena query failed: {reason}")

    results = athena.get_query_results(QueryExecutionId=query_id)
    col_info = results["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    columns = [c["Name"] for c in col_info]
    col_types = {c["Name"]: c.get("Type", "varchar") for c in col_info}
    rows = [[f.get("VarCharValue", "") for f in row["Data"]] for row in results["ResultSet"]["Rows"][1:]]
    df = pd.DataFrame(rows, columns=columns)

    # Cast columns to their declared Athena types so profiling sees correct dtypes
    # (Athena query results return everything as strings — without this, the entity
    # profile would flag every numeric column as "type mismatch: stored as string")
    for col, atype in col_types.items():
        atype_lower = atype.lower()
        try:
            if atype_lower in ("integer", "int", "bigint", "smallint", "tinyint"):
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
            elif atype_lower in ("double", "float", "decimal", "real"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            elif atype_lower in ("boolean",):
                df[col] = df[col].map({"true": True, "false": False})
            elif atype_lower in ("date", "timestamp") or atype_lower.startswith("timestamp"):
                # Athena DATE/TIMESTAMP columns arrive as strings in query results; cast to
                # native datetime so the profile reports a datetime dtype (not a false
                # "stored as string" type_mismatch).
                df[col] = pd.to_datetime(df[col], errors="coerce")
        except Exception:
            pass  # leave as string if cast fails

    return df


def _query_aurora_sample(database: str, schema: str, table: str, region: str):
    """Run a bounded SELECT over the RDS Data API (as the READ-ONLY user — the same
    AURORA_DATA_API_* creds MicroVMs use) and return the result as a DataFrame.

    Uses includeResultMetadata so we get column names + types to build a typed
    frame. The Serverless-v2 cluster may be auto-paused (scale-to-zero), so we
    retry briefly through DatabaseResumingException like the rest of the app."""
    import time
    import pandas as pd

    cluster_arn = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
    secret_arn = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
    if not (cluster_arn and secret_arn):
        raise RuntimeError("Aurora Data API not configured (AURORA_DATA_API_* unset)")

    client = boto3.client("rds-data", region_name=region)
    # Quote identifiers so schema/table names are always valid; the RO user can
    # only SELECT the curated views, so this is inherently scoped.
    # RANDOM sample (ORDER BY random()) not a plain LIMIT — a plain LIMIT returns the
    # first physical rows, which can correlate with a column and bias the profile
    # (same reasoning as the Athena path). Postgres random() is cheap at demo scale.
    sql = f'SELECT * FROM "{schema}"."{table}" ORDER BY random() LIMIT {SAMPLE_ROW_LIMIT}'

    resp = None
    for attempt in range(6):  # ~ up to ~30s of resume wait
        try:
            resp = client.execute_statement(
                resourceArn=cluster_arn,
                secretArn=secret_arn,
                database=database,
                sql=sql,
                includeResultMetadata=True,
            )
            break
        except client.exceptions.DatabaseResumingException:
            time.sleep(5)
        except Exception as e:
            # Some SDK versions surface resume as a generic error string.
            if "resuming" in str(e).lower() and attempt < 5:
                time.sleep(5)
                continue
            raise
    if resp is None:
        raise RuntimeError("Aurora cluster did not resume in time")

    col_meta = resp.get("columnMetadata", []) or []
    columns = [c.get("label") or c.get("name") or f"col_{i}" for i, c in enumerate(col_meta)]

    def _field_value(field: dict):
        if not field or field.get("isNull"):
            return None
        for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
            if key in field:
                return field[key]
        return None

    rows = [[_field_value(f) for f in rec] for rec in resp.get("records", []) or []]
    df = pd.DataFrame(rows, columns=columns) if columns else pd.DataFrame(rows)

    # The Data API already returns native longValue/doubleValue/booleanValue for
    # numeric/bool columns, so pandas infers correct dtypes — no string-cast pass
    # needed like Athena. Best-effort numeric coercion for any all-string numerics.
    for col in df.columns:
        if df[col].dtype == object:
            coerced = pd.to_numeric(df[col], errors="coerce")
            if coerced.notna().sum() == df[col].notna().sum() and df[col].notna().any():
                df[col] = coerced
    return df


def fetch_and_profile(entity: dict, region: str) -> dict:
    """Fetch a bounded sample of the entity's data and compute profiling stats."""
    import pandas as pd

    source_type = entity["source_type"]

    if source_type == "s3":
        s3 = boto3.client("s3", region_name=region)
        obj = s3.get_object(Bucket=entity["bucket"], Key=entity["key"])
        body = obj["Body"].read()
        ext = entity["extension"]
        # Read the FULL file, then take a RANDOM sample (not the first N rows). A
        # head-only sample (nrows / .head()) biases the profile when row order
        # correlates with a column (e.g. a file sorted/partitioned by date or
        # status). Demo files are small, so reading fully then sampling is cheap;
        # random_state fixes the sample so re-profiling an unchanged file is stable.
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(body))
        elif ext == "parquet":
            df = pd.read_parquet(io.BytesIO(body))
        elif ext == "json":
            df = pd.read_json(io.BytesIO(body), lines=True)
        else:
            raise ValueError(f"Unsupported S3 extension: {ext}")
        if len(df) > SAMPLE_ROW_LIMIT:
            df = df.sample(n=SAMPLE_ROW_LIMIT, random_state=42).reset_index(drop=True)
        row_count_note = f"Profile based on a RANDOM sample of up to {SAMPLE_ROW_LIMIT} rows (file size: {obj.get('ContentLength', 0):,} bytes)."

    elif source_type == "s3_prefix":
        # A PREFIX of part files surfaced as ONE dataset (Athena UNLOAD run / seeded
        # partitioned Parquet folder). There's no single object to get_object — list
        # the parts and read each into a frame, then concat. Parts are Parquet:
        # UNLOAD writes them EXTENSIONLESS, the seeded folder uses .parquet, and a few
        # datasets may be .csv — detect per part by extension (default parquet for
        # extensionless, mirroring s3.py's prefix schema/snippet logic). Reading is
        # bounded by stopping once we've gathered > SAMPLE_ROW_LIMIT rows, then a
        # random sample is taken (same debiasing as the single-file path).
        s3 = boto3.client("s3", region_name=region)
        prefix = entity["prefix"]
        bucket = entity["bucket"]
        parts = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                k = o["Key"]
                if k.endswith("/") or o.get("Size", 0) == 0:
                    continue  # folder placeholder / empty marker
                parts.append(k)
        if not parts:
            raise ValueError(f"No part files under prefix s3://{bucket}/{prefix}")
        frames = []
        rows_so_far = 0
        total_bytes = 0
        parts_read = 0
        for k in parts:
            pobj = s3.get_object(Bucket=bucket, Key=k)
            pbody = pobj["Body"].read()
            total_bytes += len(pbody)
            leaf = k.rsplit("/", 1)[-1]
            pext = leaf.rsplit(".", 1)[-1].lower() if "." in leaf else "parquet"
            try:
                if pext == "csv":
                    pdf = pd.read_csv(io.BytesIO(pbody))
                elif pext in ("parquet", "pq"):
                    pdf = pd.read_parquet(io.BytesIO(pbody))
                elif pext == "json":
                    pdf = pd.read_json(io.BytesIO(pbody), lines=True)
                else:
                    # Extensionless (or unknown) → Athena UNLOAD convention: Parquet.
                    pdf = pd.read_parquet(io.BytesIO(pbody))
            except Exception as e:
                logger.warning(f"[entity-discovery] prefix part unreadable, skipping s3://{bucket}/{k}: {e}")
                continue
            frames.append(pdf)
            parts_read += 1
            rows_so_far += len(pdf)
            # Enough rows to take a representative sample — stop reading more parts.
            if rows_so_far >= SAMPLE_ROW_LIMIT:
                break
        if not frames:
            raise ValueError(f"No readable part files under prefix s3://{bucket}/{prefix}")
        df = pd.concat(frames, ignore_index=True)
        if len(df) > SAMPLE_ROW_LIMIT:
            df = df.sample(n=SAMPLE_ROW_LIMIT, random_state=42).reset_index(drop=True)
        row_count_note = (f"Profile based on a RANDOM sample of up to {SAMPLE_ROW_LIMIT} rows "
                          f"read from {parts_read} of {len(parts)} Parquet part file(s) under the "
                          f"prefix ({total_bytes:,} bytes read).")

    elif source_type == "athena":
        df = _query_athena_sample(entity["database"], entity["table_name"], region,
                                  catalog=entity.get("catalog", ""))
        row_count_note = f"Profile based on a RANDOM sample of up to {SAMPLE_ROW_LIMIT} rows queried via Athena (ORDER BY rand())."

    elif source_type == "dynamodb":
        ddb = boto3.resource("dynamodb", region_name=region)
        table = ddb.Table(entity["table_name"])
        resp = table.scan(Limit=SAMPLE_ROW_LIMIT)
        df = pd.DataFrame(_convert_dynamo_decimals(resp.get("Items", [])))
        row_count_note = f"Profile based on a sample of up to {SAMPLE_ROW_LIMIT} items via DynamoDB scan."

    elif source_type == "aurora":
        df = _query_aurora_sample(
            entity["database"], entity["schema"], entity["table_name"], region
        )
        row_count_note = (
            f"Profile based on a RANDOM sample of up to {SAMPLE_ROW_LIMIT} rows "
            f"queried via the Aurora RDS Data API (read-only, ORDER BY random())."
        )

    elif source_type == "bedrock_kb":
        # A KB is retrieve-only — no rows/columns to profile with pandas. Instead we
        # gather control-plane facts (name/status/embedding/ingestion) and, most
        # importantly, the FILTERABLE METADATA (fields + sample values/ranges) by
        # sampling retrievals. Returned directly (not via _profile_dataframe).
        return _profile_bedrock_kb(entity.get("kb_id") or entity["source_id"], region)

    else:
        raise ValueError(f"Unsupported source_type: {source_type}")

    stats = _profile_dataframe(df)
    stats["sample_note"] = row_count_note
    return stats


# Bedrock adds its own bookkeeping keys to every chunk's metadata; they are not
# user-defined filterable attributes, so we exclude them from the profile.
_KB_INTERNAL_META_PREFIX = "x-amz-"

# How many chunks to sample (across several broad queries) when characterizing the
# KB's filterable metadata. High enough to see every distinct value in a demo-sized
# KB, bounded so profiling stays fast/cheap.
_KB_SAMPLE_QUERIES = ["product", "features", "quality material design", "price value", "overview specification"]
_KB_SAMPLE_PER_QUERY = 100


def _profile_bedrock_kb(kb_id: str, region: str) -> dict:
    """Build a profiling stats dict for a Bedrock Knowledge Base.

    Fully generic on `kb_id` — works for any KB the provider surfaces. Combines:
      • control-plane facts (GetKnowledgeBase, ListDataSources/ListIngestionJobs):
        name, description, status, embedding model, document count, ingestion health.
      • FILTERABLE METADATA discovered by sampling retrievals: for each user-defined
        metadata field, its inferred type and sample values (categoricals) or numeric
        range. This is the most useful thing for a KB — it's what read_bedrock_kb /
        ask_bedrock_kb `filter=` operates on.
    """
    agent = boto3.client("bedrock-agent", region_name=region)
    runtime = boto3.client("bedrock-agent-runtime", region_name=region)

    stats: dict = {"kb_id": kb_id}
    quality_signals: list[dict] = []

    # --- Control-plane facts --------------------------------------------------
    try:
        kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        stats["name"] = kb.get("name", "")
        stats["description"] = kb.get("description", "")
        stats["status"] = kb.get("status", "")
        emb = (
            kb.get("knowledgeBaseConfiguration", {})
            .get("vectorKnowledgeBaseConfiguration", {})
            .get("embeddingModelArn", "")
        )
        # Show just the model name, not the full ARN.
        stats["embedding_model"] = emb.split("/")[-1] if emb else ""
        if stats["status"] and stats["status"] != "ACTIVE":
            quality_signals.append({
                "type": "kb_not_active", "severity": "high",
                "detail": f"Knowledge base status is {stats['status']} (expected ACTIVE).",
            })
    except Exception as e:
        logger.warning(f"[entity-discovery] KB {kb_id}: get_knowledge_base failed: {e}")

    # --- Data sources + ingestion health -------------------------------------
    data_sources = []
    total_docs = 0
    try:
        for ds in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries", []):
            dsid = ds.get("dataSourceId")
            entry = {"id": dsid, "name": ds.get("name", ""), "status": ds.get("status", "")}
            try:
                jobs = agent.list_ingestion_jobs(
                    knowledgeBaseId=kb_id, dataSourceId=dsid, maxResults=1
                ).get("ingestionJobSummaries", [])
                if jobs:
                    j = jobs[0]
                    jstats = j.get("statistics", {}) or {}
                    entry["last_ingestion_status"] = j.get("status", "")
                    entry["documents"] = jstats.get("numberOfDocumentsScanned")
                    entry["documents_failed"] = jstats.get("numberOfDocumentsFailed")
                    total_docs += jstats.get("numberOfDocumentsScanned") or 0
                    if (jstats.get("numberOfDocumentsFailed") or 0) > 0:
                        quality_signals.append({
                            "type": "ingestion_failures", "severity": "medium",
                            "detail": f"{jstats['numberOfDocumentsFailed']} document(s) failed to index "
                                      f"in data source '{entry['name']}'.",
                        })
                    if j.get("status") and j.get("status") not in ("COMPLETE",):
                        quality_signals.append({
                            "type": "ingestion_incomplete", "severity": "medium",
                            "detail": f"Last ingestion for '{entry['name']}' is {j.get('status')}.",
                        })
            except Exception as e:
                logger.warning(f"[entity-discovery] KB {kb_id}: ingestion jobs for {dsid} failed: {e}")
            data_sources.append(entry)
    except Exception as e:
        logger.warning(f"[entity-discovery] KB {kb_id}: list_data_sources failed: {e}")
    stats["data_sources"] = data_sources
    stats["document_count"] = total_docs
    if total_docs == 0:
        quality_signals.append({
            "type": "empty_kb", "severity": "high",
            "detail": "No documents are indexed in this knowledge base.",
        })

    # --- Filterable metadata (sampled) ---------------------------------------
    # No native "distinct values" API exists, so sample chunks across a few broad
    # queries and collect the user-defined metadata attributes we see.
    cat_values: dict[str, set] = {}
    num_values: dict[str, list] = {}
    docs_seen: set = set()
    try:
        for q in _KB_SAMPLE_QUERIES:
            next_token, pulled = None, 0
            while pulled < _KB_SAMPLE_PER_QUERY:
                kwargs = {
                    "knowledgeBaseId": kb_id,
                    "retrievalQuery": {"text": q},
                    "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": _KB_SAMPLE_PER_QUERY}},
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = runtime.retrieve(**kwargs)
                results = resp.get("retrievalResults", [])
                for r in results:
                    md = r.get("metadata", {}) or {}
                    for k, v in md.items():
                        if k.startswith(_KB_INTERNAL_META_PREFIX):
                            continue  # Bedrock bookkeeping key, not user metadata
                        if isinstance(v, bool):
                            cat_values.setdefault(k, set()).add(str(v))
                        elif isinstance(v, (int, float)):
                            num_values.setdefault(k, []).append(v)
                        else:
                            cat_values.setdefault(k, set()).add(str(v))
                    # Track distinct documents (best-effort, via product_id-like id keys).
                    doc_id = md.get("product_id") or md.get("x-amz-bedrock-kb-source-uri")
                    if doc_id:
                        docs_seen.add(doc_id)
                pulled += len(results)
                next_token = resp.get("nextToken")
                if not next_token:
                    break
    except Exception as e:
        logger.warning(f"[entity-discovery] KB {kb_id}: metadata sampling failed: {e}")

    # Shape the filterable-metadata summary: categorical fields get distinct sample
    # values; numeric fields get a min–max range. Keeps the doc actionable for
    # building read_bedrock_kb(filter=...) clauses.
    MAX_SAMPLE_VALUES = 12
    filterable = []
    for field in sorted(set(cat_values) | set(num_values)):
        if field in num_values and num_values[field]:
            xs = num_values[field]
            filterable.append({
                "field": field,
                "type": "number",
                "min": min(xs),
                "max": max(xs),
                "distinct_count": len(set(xs)),
            })
        else:
            vals = sorted(cat_values.get(field, set()))
            filterable.append({
                "field": field,
                "type": "string",
                "distinct_count": len(vals),
                "sample_values": vals[:MAX_SAMPLE_VALUES],
                "truncated": len(vals) > MAX_SAMPLE_VALUES,
            })
    stats["filterable_metadata"] = filterable
    if not filterable:
        quality_signals.append({
            "type": "no_filterable_metadata", "severity": "low",
            "detail": "No document metadata was found — metadata filtering "
                      "(filter=...) will not narrow results for this KB.",
        })

    stats["quality_flags"] = quality_signals
    stats["sample_note"] = (
        f"Profile based on control-plane metadata plus a sample of retrieved chunks "
        f"across {len(_KB_SAMPLE_QUERIES)} broad queries "
        f"(~{len(docs_seen)} distinct documents observed). A Knowledge Base is "
        f"retrieve-only, so there are no rows/columns to profile."
    )
    return stats


# =============================================================================
# STEP 4: GENERATE THE PER-ENTITY MARKDOWN (one-shot Bedrock call, no tools)
# =============================================================================

def generate_entity_markdown(entity: dict, stats: dict) -> dict:
    """
    Call Bedrock once (no agent/tool loop — all facts are already computed
    and handed in as text) to turn the stats into a business-readable
    profile. Returns {"business_description", "quality_flags", "markdown"}.
    """
    from proxy.notebook.ai.constants import AGENT_TEMPERATURE

    # A Bedrock KB is retrieve-only — use the KB-tailored prompt (metadata-centric)
    # rather than the tabular one. Same JSON output contract either way.
    is_kb = entity["source_type"] == "bedrock_kb"
    template = KB_ENTITY_DISCOVERY_PROMPT if is_kb else ENTITY_DISCOVERY_PROMPT

    fmt_kwargs = dict(
        source_id=entity["source_id"],
        source_type=entity["source_type"],
        stats_json=json.dumps(stats, indent=2, default=str),
    )
    if not is_kb:
        # The exact identifier a query author must put in a FROM clause / read tool.
        # For athena/aurora/dynamodb/local sources source_id is already the correct
        # fully-qualified name (db.table, namespace.table, dynamodb.<table>, local.<file>),
        # so it IS the right FROM identifier. Pass it explicitly so the model grounds the
        # "Query as:" line on it verbatim instead of re-deriving (and possibly mangling) it.
        fmt_kwargs["query_identifier"] = entity["source_id"]

    prompt = template.format(**fmt_kwargs)

    client = _get_bedrock_client()
    # Build params per-model: temperature is included only when the model accepts it
    # (dropped for 5.x Claude, which deprecates it), and maxTokens is clamped to the
    # model's real ceiling. Thinking is disabled for models that support the knob.
    converse_kwargs = dict(
        modelId=require_model_id(AI_MODEL_ID),
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=build_inference_config(
            AI_MODEL_ID, max_tokens=2048, temperature=AGENT_TEMPERATURE
        ),
    )
    _extra = build_additional_request_fields(AI_MODEL_ID, disable_thinking=True)
    if _extra:
        converse_kwargs["additionalModelRequestFields"] = _extra
    response = client.converse(**converse_kwargs)
    raw_text = response["output"]["message"]["content"][0]["text"].strip()

    # Strip markdown code fences if present (same pattern used in workbook_intel.py)
    if "```json" in raw_text:
        raw_text = raw_text.split("```json")[1].split("```")[0].strip()
    elif raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

    json_text = raw_text
    if not json_text.startswith("{"):
        first, last = json_text.find("{"), json_text.rfind("}")
        if first != -1 and last > first:
            json_text = json_text[first:last + 1]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.warning(f"[entity-discovery] JSON parse failed for {entity['source_id']}: {e}")
        return {"business_description": "", "quality_flags": [], "markdown": raw_text}


# =============================================================================
# STEP 5: SAVE DOC TO S3 + METADATA TO SQLITE
# =============================================================================

def _sanitize_source_id(source_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in source_id)


def save_entity_doc(source_id: str, result: dict, bucket: str, region: str) -> str:
    """Save the full entity discovery result (business_description + quality_flags +
    markdown) as JSON to S3. Consumers can fetch the full doc or just the lightweight
    fields (business_description, quality_flags) depending on their use case."""
    s3 = boto3.client("s3", region_name=region)
    s3_key = f"data-catalog/entities/{_sanitize_source_id(source_id)}.json"
    payload = {
        "source_id": source_id,
        "business_description": result.get("business_description", ""),
        "quality_flags": result.get("quality_flags", []),
        "markdown": result.get("markdown", ""),
    }
    s3.put_object(
        Bucket=bucket, Key=s3_key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return s3_key


# =============================================================================
# ORCHESTRATION
# =============================================================================

def discover_entity(entity: dict, bucket: str, region: str, storage, force: bool = False) -> dict:
    """Discover (or skip, if unchanged) a single entity end to end."""
    source_id = entity["source_id"]
    try:
        needs_discovery, change_signal = _entity_needs_discovery(entity, region, storage, force)
        if not needs_discovery:
            return {"source_id": source_id, "status": "skipped_unchanged"}

        storage.entity_upsert(source_id, entity["source_type"], status="discovering")

        stats = fetch_and_profile(entity, region)
        result = generate_entity_markdown(entity, stats)
        doc_s3_key = save_entity_doc(source_id, result, bucket, region)

        storage.entity_upsert(
            source_id, entity["source_type"],
            doc_s3_key=doc_s3_key, change_signal=change_signal, status="ready",
        )
        logger.info(f"[entity-discovery] Discovered {source_id} -> {doc_s3_key}")
        return {"source_id": source_id, "status": "discovered", "doc_s3_key": doc_s3_key}

    except Exception as e:
        logger.error(f"[entity-discovery] Failed for {source_id}: {e}")
        emit_error_metric("entity-discovery", "discover_global_entity", type(e).__name__, 500)
        try:
            storage.entity_upsert(source_id, entity["source_type"], status="error")
        except Exception:
            pass
        return {"source_id": source_id, "status": "error", "error": str(e)}


def discover_all_global_entities(bucket: str, athena_db: str, region: str, storage,
                                  force: bool = False,
                                  max_workers: int = MAX_CONCURRENT_DISCOVERY) -> dict:
    """
    Enumerate all global entities and discover (or skip) each one, bounded by
    a small thread pool so we never fire more than max_workers concurrent
    Bedrock calls at once.
    """
    entities = list_global_entities(bucket, athena_db, region)
    logger.info(f"[entity-discovery] Found {len(entities)} global entities")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(discover_entity, e, bucket, region, storage, force): e for e in entities}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    summary = {
        "total": len(results),
        "discovered": sum(1 for r in results if r["status"] == "discovered"),
        "skipped_unchanged": sum(1 for r in results if r["status"] == "skipped_unchanged"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }
    logger.info(f"[entity-discovery] Done: {summary['discovered']} discovered, "
                f"{summary['skipped_unchanged']} unchanged, {summary['errors']} errors")
    return summary


# =============================================================================
# LOCAL FILE ENTITIES (session-scoped — uploaded /tmp files)
#
# Same overall shape as global entity discovery, but:
#   - Enumerated via the session's own VM catalog (GET /datasources/catalog),
#     not boto3, since these files only exist on that one VM's disk.
#   - Profiled by running a deterministic pandas snippet on that SAME VM via
#     the existing generic execute endpoint (the same mechanism the agent's
#     execute_code tool uses) — no new VM-side code or image rebuild needed.
#   - Saved under sessions/{session_id}/local-entities/ in S3 and tracked in
#     the local_file_entities table, keyed by (session_id, filepath) instead
#     of a global source_id.
# =============================================================================

# Local files share a single VM's executor. That executor redirects sys.stdout and
# is NOT thread-safe, which historically forced sequential profiling (concurrency=1)
# to avoid interleaved /execute calls returning empty/garbled output.
#
# This is now safe to parallelize: the VM-side /execute endpoint (app/notebook/
# code_engine.py) serializes every execution under an asyncio.Lock, so even if the
# proxy fires concurrent profiling requests, the VM runs them one at a time (no stdout
# race). The parallelism still wins because the per-file Bedrock doc-generation calls
# (the slow part) overlap across threads while VM execution is serialized VM-side.
LOCAL_DISCOVERY_CONCURRENCY = 3


def _proxy_headers(session_id: str) -> dict:
    """Headers for the proxy calls below. Attaches the per-run shared secret
    (X-Proxy-Auth) when PROXY_AUTH_TOKEN is set so these server-to-server calls
    pass the proxy's auth gate; loopback-only + this header protect the proxy."""
    headers = {"X-Session-Id": session_id, "Content-Type": "application/json"}
    token = os.environ.get("PROXY_AUTH_TOKEN")
    if token:
        headers["X-Proxy-Auth"] = token
    return headers


def _list_local_files_from_catalog(proxy_url: str, session_id: str) -> list[dict]:
    """Fetch the session's VM data catalog and return only local (/tmp) file entries.

    READ-ONLY (refresh=false): this only enumerates local entries, so it skips the
    proxy's resync push + cloud re-discovery. That push was pointless here and was
    the cause of the "Failed to fetch catalog for local files: timed out" warnings
    during post-upload deltas (it contended with the VM's in-progress upload). A
    read-only fetch is fast; the timeout below is just a safety ceiling for a cold VM.
    """
    import httpx
    try:
        resp = httpx.get(
            f"{proxy_url}/datasources/catalog",
            headers=_proxy_headers(session_id),
            params={"refresh": "false"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return []
        catalog = resp.json()
        return [e for e in catalog.get("entries", []) if e.get("source_type") == "local"]
    except Exception as e:
        emit_error_metric("entity-discovery", "_list_local_files_from_catalog", type(e).__name__, 504)
        logger.warning(
            f"[entity-discovery] Failed to fetch catalog for local files "
            f"(read-only): {type(e).__name__}: {e!r}"
        )
        return []


def _execute_on_vm(proxy_url: str, session_id: str, code: str, timeout: float = 60.0) -> str:
    """Run code on the session's VM via the proxy's generic execute endpoint (the
    same mechanism the agent's execute_code tool uses) and return stdout. Raises on failure."""
    import httpx
    resp = httpx.post(
        f"{proxy_url}/proxy/execute",
        headers=_proxy_headers(session_id),
        # origin="internal": the VM runs the profiler in a per-run throwaway scratch,
        # so its helpers (_df, _stats, ...) never land in the user namespace.
        json={"code": code, "origin": "internal"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"VM execute HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"VM execute failed: {data.get('error', 'unknown error')}")
    output = data.get("output", "")
    # If success=True but output is empty and there's an error field, the code
    # partially failed (e.g. exception on the last line after some output)
    if not output and data.get("error"):
        raise RuntimeError(f"VM execute returned empty output with error: {data['error']}")
    return output


_STATS_MARKER = "__ENTITY_STATS_JSON__"


def _build_local_profile_code(filepath: str, sample_limit: int) -> str:
    """Deterministic pandas snippet (no LLM involved) that computes the same
    profiling shape as _profile_dataframe, run remotely on the session's VM."""
    return f'''
import pandas as _pd, json as _json
_path = {filepath!r}
_ext = _path.rsplit(".", 1)[-1].lower() if "." in _path else ""
if _ext == "csv":
    _df = _pd.read_csv(_path, nrows={sample_limit})
elif _ext == "parquet":
    _df = _pd.read_parquet(_path).head({sample_limit})
elif _ext == "json":
    try:
        _df = _pd.read_json(_path, lines=True).head({sample_limit})
    except Exception:
        _df = _pd.read_json(_path).head({sample_limit})
elif _ext in ("xlsx", "xls"):
    _df = _pd.read_excel(_path).head({sample_limit})
else:
    _df = _pd.read_csv(_path, nrows={sample_limit})

def _norm_dtype(_d):
    _d = str(_d).lower().strip()
    if _d == "object": return "string"
    if _d.startswith("float"): return "float"
    if _d.startswith("int") or _d.startswith("uint"): return "int"
    if _d.startswith("bool"): return "bool"
    if _d.startswith("datetime"): return "datetime"
    if _d in ("string", "str"): return "string"
    return _d

_total = len(_df)
_nulls = _df.isnull().sum()
_stats = {{
    "sampled_rows": _total,
    "columns": [
        {{
            "name": _c,
            "dtype": _norm_dtype(_df[_c].dtype),
            "null_pct": round(float(_nulls[_c]) / _total * 100, 1) if _total else 0.0,
            "unique_count": int(_df[_c].nunique()),
            "sample_values": [str(_v) for _v in _df[_c].dropna().unique()[:5].tolist()],
        }}
        for _c in _df.columns
    ],
    "duplicate_row_pct": round(float(_df.duplicated().sum()) / _total * 100, 1) if _total else 0.0,
}}
_numeric = _df.select_dtypes(include="number")
if not _numeric.empty:
    _desc = _numeric.describe().to_dict()
    _stats["numeric_describe"] = {{
        _col: {{_k: (round(float(_v), 3) if isinstance(_v, (int, float)) else _v) for _k, _v in _vals.items()}}
        for _col, _vals in _desc.items()
    }}
print({_STATS_MARKER!r} + _json.dumps(_stats, default=str))
'''


def profile_local_file_via_vm(proxy_url: str, session_id: str, filepath: str) -> dict:
    """Profile a local /tmp file by running a deterministic pandas snippet on the
    session's own VM. Requires no new VM-side code — reuses the generic execute endpoint.
    Retries once if the first attempt returns empty output (can happen if the VM
    executor is momentarily busy from a prior request)."""
    import time as _time
    code = _build_local_profile_code(filepath, SAMPLE_ROW_LIMIT)

    for attempt in range(2):
        output = _execute_on_vm(proxy_url, session_id, code)
        idx = output.find(_STATS_MARKER)
        if idx != -1:
            break
        if attempt == 0:
            logger.warning(f"[entity-discovery] Profiling {filepath}: empty output on first attempt, retrying in 2s...")
            _time.sleep(2)

    if idx == -1:
        raise RuntimeError(f"Profiling output missing expected marker after 2 attempts: {output[:300]}")

    json_str = output[idx + len(_STATS_MARKER):].strip()
    decoder = json.JSONDecoder()
    try:
        stats, _ = decoder.raw_decode(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse profiling stats JSON: {e}\nRaw: {json_str[:500]}")
    stats["sample_note"] = (
        f"Profile based on a sample of up to {SAMPLE_ROW_LIMIT} rows read directly "
        f"from this workbook's VM."
    )
    return stats


def _local_change_signal(entry: dict) -> dict:
    """Cheap fingerprint for a local file, sourced from the VM catalog entry
    (already computed by app/notebook/data_catalog.py — no extra VM round-trip)."""
    return {"size": entry.get("size"), "row_count": entry.get("row_count")}


def save_local_entity_doc(session_id: str, filepath: str, result: dict, bucket: str, region: str) -> str:
    """Save local file entity doc as JSON (same format as global entities)."""
    s3 = boto3.client("s3", region_name=region)
    s3_key = f"sessions/{session_id}/local-entities/{_sanitize_source_id(os.path.basename(filepath))}.json"
    payload = {
        "source_id": filepath,
        "business_description": result.get("business_description", ""),
        "quality_flags": result.get("quality_flags", []),
        "markdown": result.get("markdown", ""),
    }
    s3.put_object(
        Bucket=bucket, Key=s3_key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return s3_key


def discover_local_file(entry: dict, session_id: str, proxy_url: str, bucket: str, region: str,
                        storage, force: bool = False) -> dict:
    """Discover (or skip, if unchanged) one local file end to end."""
    filepath = entry["source_id"]  # the VM catalog uses the raw /tmp path as source_id for local files
    try:
        current_signal = _local_change_signal(entry)
        if not force:
            existing = storage.local_entity_get(session_id, filepath)
            if (existing and existing.get("status") == "ready"
                    and (existing.get("change_signal") or {}) == current_signal):
                return {"source_id": filepath, "status": "skipped_unchanged"}

        storage.local_entity_upsert(session_id, filepath, status="discovering")

        stats = profile_local_file_via_vm(proxy_url, session_id, filepath)
        pseudo_entity = {"source_id": f"local.{os.path.basename(filepath)}", "source_type": "local"}
        result = generate_entity_markdown(pseudo_entity, stats)
        doc_s3_key = save_local_entity_doc(session_id, filepath, result, bucket, region)

        storage.local_entity_upsert(session_id, filepath, doc_s3_key=doc_s3_key,
                                    change_signal=current_signal, status="ready")
        logger.info(f"[entity-discovery] Discovered local file {filepath} "
                    f"(session {session_id[:8]}...) -> {doc_s3_key}")
        return {"source_id": filepath, "status": "discovered", "doc_s3_key": doc_s3_key}

    except Exception as e:
        logger.error(f"[entity-discovery] Local file discovery failed for {filepath}: {e}")
        emit_error_metric("entity-discovery", "discover_local_file", type(e).__name__, 500)
        try:
            storage.local_entity_upsert(session_id, filepath, status="error")
        except Exception:
            pass
        return {"source_id": filepath, "status": "error", "error": str(e)}


def discover_all_local_files(session_id: str, proxy_url: str, bucket: str, region: str, storage,
                             force: bool = False, max_workers: int = LOCAL_DISCOVERY_CONCURRENCY) -> dict:
    """
    Ensure every local /tmp file in this session has an up-to-date profile doc.
    Call this before generating a workbook's insight so local-file docs are
    fresh by the time they're read.
    """
    entries = _list_local_files_from_catalog(proxy_url, session_id)
    if not entries:
        return {"total": 0, "discovered": 0, "skipped_unchanged": 0, "errors": 0, "results": []}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(discover_local_file, e, session_id, proxy_url, bucket, region, storage, force): e
            for e in entries
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    return {
        "total": len(results),
        "discovered": sum(1 for r in results if r["status"] == "discovered"),
        "skipped_unchanged": sum(1 for r in results if r["status"] == "skipped_unchanged"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


# =============================================================================
# SHARED DOC-FETCHING HELPER — used by proxy.notebook.ai.workbook_intel to pull
# already-discovered docs (global or local) into the Workbook Intel prompt.
# =============================================================================

def get_entity_doc_markdown(source_id: str, bucket: str, region: str, storage,
                            session_id: str = None) -> str | None:
    """
    Fetch the markdown content for one entity doc, global or local.

    Pass session_id to look up a local /tmp file (scoped to that session);
    omit it to look up a global entity (S3/Athena/DynamoDB).
    Returns None if the doc doesn't exist yet or isn't ready.
    """
    meta = storage.local_entity_get(session_id, source_id) if session_id else storage.entity_get(source_id)
    if not meta or meta.get("status") != "ready" or not meta.get("doc_s3_key"):
        return None
    try:
        s3 = boto3.client("s3", region_name=region)
        obj = s3.get_object(Bucket=bucket, Key=meta["doc_s3_key"])
        return obj["Body"].read().decode("utf-8")
    except Exception as e:
        logger.warning(f"[entity-discovery] Failed to fetch doc for {source_id}: {e}")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    region = os.environ.get("AWS_REGION", "us-west-2")
    account_id = os.environ.get("ACCOUNT_ID", "")
    if not account_id:
        try:
            account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        except Exception:
            account_id = "unknown"
    bucket = os.environ.get("ARTIFACT_BUCKET", f"microvm-sandbox-artifacts-{account_id}-{region}")
    athena_db = os.environ.get("ATHENA_DB", "microvm_demo_db")
    force = os.environ.get("FORCE_REDISCOVER", "").lower() in ("1", "true", "yes")

    from proxy.storage import storage as _storage
    _storage.initialize()

    print(f"Discovering global entities in bucket={bucket}, athena_db={athena_db}, region={region} (force={force})")
    summary = discover_all_global_entities(bucket, athena_db, region, _storage, force=force)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    for r in summary["results"]:
        suffix = f" -> {r.get('doc_s3_key')}" if r.get("doc_s3_key") else (f" ({r.get('error')})" if r.get("error") else "")
        print(f"  [{r['status']}] {r['source_id']}{suffix}")
