"""DATA LOADING — Aurora PostgreSQL via the RDS Data API (HTTPS/443)."""

import os

import boto3
import pandas as pd

from ._common import _log_read
from ._common import _REGION, _AURORA_CLUSTER_ARN, _AURORA_SECRET_ARN, _AURORA_DB


# =============================================================================
# DATA LOADING — Aurora PostgreSQL via the RDS Data API (HTTPS/443)
# =============================================================================
# (Categorized under DATA LOADING so read_aurora sits with the other readers in
# the Snippets panel / autocomplete — the category is derived from this header by
# completion_catalog._category_map_from_source. The private _aurora_* helpers
# below are underscore-prefixed and excluded from the catalog.)
# A MicroVM cannot open a 5432 socket (managed egress allows only 80/443/53), so
# Aurora is queried over the RDS Data API: rds-data:ExecuteStatement to the
# cluster ARN, authenticating with the READ-ONLY secret ARN. Both ARNs + the
# default database are injected at launch (AURORA_DATA_API_* env). The read-only
# user can only SELECT, which is exactly what notebooks need.

# Data API returns each cell as a single-key "field" dict tagging the SQL type,
# e.g. {"longValue": 3}, {"stringValue": "x"}, {"isNull": true}. This maps the
# field key → a Python value. blobValue is bytes; arrayValue is a nested struct
# we surface as-is (rare for notebook SELECTs).
def _aurora_field_value(field: dict):
    """Unwrap one RDS Data API field dict into a native Python value."""
    if not field or field.get("isNull"):
        return None
    # Exactly one of these keys is present per field.
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field:
            return field[key]
    if "arrayValue" in field:
        return field["arrayValue"]  # nested arrays: pass through (uncommon)
    # Unknown shape — return the raw dict rather than guessing.
    return field


# Postgres type name (from Data API columnMetadata typeName) → coercion applied to
# the unwrapped column so downstream math/dates work. Best-effort; unknown types
# stay as returned. Data API already returns numbers as numbers, so this mainly
# fixes NUMERIC/DECIMAL (returned as string) and timestamps (returned as string).
_PG_INT_TYPES = {"int2", "int4", "int8", "smallint", "integer", "bigint", "serial", "bigserial"}
_PG_FLOAT_TYPES = {"float4", "float8", "real", "double precision", "numeric", "decimal", "money"}
_PG_BOOL_TYPES = {"bool", "boolean"}
_PG_DATETIME_TYPES = {"timestamp", "timestamptz", "timestamp with time zone",
                      "timestamp without time zone", "date"}


def _coerce_pg_dtypes(df: "pd.DataFrame", type_names: list) -> "pd.DataFrame":
    """Coerce an Aurora result frame to real dtypes using the query's Postgres
    column type names. Never raises — a column that won't convert is left as-is."""
    if df.empty or not type_names:
        return df
    for name, tname in zip(df.columns, type_names):
        t = (tname or "").strip().lower()
        try:
            if t in _PG_INT_TYPES:
                df[name] = pd.to_numeric(df[name], errors="coerce").astype("Int64")
            elif t in _PG_FLOAT_TYPES:
                df[name] = pd.to_numeric(df[name], errors="coerce")
            elif t in _PG_BOOL_TYPES:
                df[name] = df[name].astype("boolean")
            elif t in _PG_DATETIME_TYPES:
                df[name] = pd.to_datetime(df[name], errors="coerce")
            # text/varchar/json/jsonb/uuid/etc. → leave as-is
        except Exception:
            pass
    return df


def _aurora_execute(sql: str, database: str = None, cluster_arn: str = None,
                    secret_arn: str = None, region: str = None,
                    parameters: list = None) -> "pd.DataFrame":
    """Run a SQL statement over the RDS Data API and return a DataFrame.

    Shared low-level executor used by read_aurora(), the SQL engine's Aurora
    runner, and catalog schema discovery. Reads AURORA_DATA_API_* from the
    environment at CALL time (so launch-time injection is respected). Raises a
    clear error if Aurora isn't configured. `parameters` is an optional list of
    RDS Data API SqlParameter dicts for parameterized queries."""
    region = region or os.environ.get("AWS_REGION", _REGION)
    cluster_arn = cluster_arn or os.environ.get("AURORA_DATA_API_CLUSTER_ARN", _AURORA_CLUSTER_ARN)
    secret_arn = secret_arn or os.environ.get("AURORA_DATA_API_SECRET_ARN", _AURORA_SECRET_ARN)
    database = database or os.environ.get("AURORA_DATA_API_DATABASE", _AURORA_DB)

    if not cluster_arn or not secret_arn:
        raise RuntimeError(
            "Aurora is not configured for this notebook: AURORA_DATA_API_CLUSTER_ARN "
            "and AURORA_DATA_API_SECRET_ARN are unset. Provision the cluster with "
            "scripts/setup_aurora_postgres.sh and set the AURORA_DATA_API_* values "
            "in scripts/config.sh."
        )

    client = boto3.client("rds-data", region_name=region)
    kwargs = {
        "resourceArn": cluster_arn,
        "secretArn": secret_arn,
        "database": database,
        "sql": sql,
        "includeResultMetadata": True,
    }
    if parameters:
        kwargs["parameters"] = parameters

    # Retry on DatabaseResumingException: the cluster is Serverless v2 with
    # scale-to-zero (min 0 ACU), so it auto-pauses when idle and the FIRST query
    # after a pause fails while it wakes (~5-30s). Without this, a read_aurora()
    # call against an idle cluster would fail outright. Linear backoff, bounded
    # (3s,6s,9s,12s,15s ≈ 45s max) so a genuinely-down cluster still errors.
    import time as _time
    _resume_attempts, _resume_base = 5, 3.0
    resp = None
    for _attempt in range(_resume_attempts):
        try:
            resp = client.execute_statement(**kwargs)
            break
        except client.exceptions.DatabaseResumingException:
            if _attempt < _resume_attempts - 1:
                _time.sleep(_resume_base * (_attempt + 1))
            else:
                raise

    # columnMetadata is present because includeResultMetadata=True.
    col_meta = resp.get("columnMetadata", []) or []
    columns = [c.get("label") or c.get("name") or f"col_{i}" for i, c in enumerate(col_meta)]
    type_names = [c.get("typeName", "") for c in col_meta]

    records = resp.get("records", []) or []
    rows = [[_aurora_field_value(f) for f in rec] for rec in records]

    # Fall back to positional column names if metadata was somehow empty but rows exist.
    if not columns and rows:
        columns = [f"col_{i}" for i in range(len(rows[0]))]

    df = pd.DataFrame(rows, columns=columns) if columns else pd.DataFrame(rows)
    df = _coerce_pg_dtypes(df, type_names)

    # TRUNCATION SIGNAL: the RDS Data API caps each ExecuteStatement response at
    # ~1MB. When a SELECT's result exceeds that, the API returns only the rows that
    # fit — SILENTLY (no error, no flag). We can't see the server-side total, so we
    # infer a likely cap-hit by measuring how close the returned records are to the
    # 1MB ceiling. If we're within ~5% of 1MB, the result was almost certainly
    # truncated by the API (not by a user/auto LIMIT). Callers read df.attrs to warn.
    # Best-effort + never raises; the size estimate uses the raw Data API records
    # (their verbose typed-JSON envelope is what actually counts against the cap).
    approx_bytes = 0
    try:
        import sys as _sys
        approx_bytes = _sys.getsizeof(records) + sum(
            _sys.getsizeof(f.get(k)) if isinstance(f, dict) else 0
            for rec in records for f in rec for k in (f if isinstance(f, dict) else ())
        )
    except Exception:
        approx_bytes = 0
    # 1 MiB is the documented cap; treat ≥ ~0.95 MiB of returned data as "likely
    # capped by the Data API" (as opposed to a smaller result that genuinely fit).
    _DATA_API_CAP_BYTES = 1024 * 1024
    df.attrs["aurora_row_count"] = len(df)
    df.attrs["aurora_api_capped"] = approx_bytes >= int(_DATA_API_CAP_BYTES * 0.95)
    return df


@_log_read("aurora")
def read_aurora(sql: str, database: str = None, parameters: list = None) -> pd.DataFrame:
    """
    Run a SQL query against the Aurora PostgreSQL cluster via the RDS Data API and
    return the results as a DataFrame. Read-only (the notebook uses a SELECT-only
    database user).

    The cluster/secret/database are configured at the platform level; you normally
    only pass `sql`. Pass `database` to target a different database on the cluster.

    Example:
        df = read_aurora("SELECT id, name, tag FROM notebooks LIMIT 100")
        df = read_aurora("SELECT count(*) FROM public.notebook_runs")

    NOTE: the RDS Data API caps each response at ~1 MB and truncates a larger
    result SILENTLY (no error). When this read lands at/near that cap, a warning is
    printed because rows were almost certainly dropped — add a tighter WHERE/LIMIT,
    aggregate in SQL, or use unload_athena_to_s3 for a large result set.
    """
    df = _aurora_execute(sql, database=database, parameters=parameters)
    # Surface the silent 1 MB Data API truncation that _aurora_execute detected
    # (df.attrs["aurora_api_capped"]). Without this the user sees a complete-looking
    # frame that is actually missing rows. Printed (not raised) so the partial result
    # is still usable, with actionable guidance.
    if df.attrs.get("aurora_api_capped"):
        print(
            f"⚠️  read_aurora: this result hit the RDS Data API's ~1 MB response cap "
            f"({len(df)} rows returned) and was almost certainly TRUNCATED — rows "
            f"beyond the cap are missing. Narrow the query (tighter WHERE/LIMIT), "
            f"aggregate in SQL, or use unload_athena_to_s3(...) for a large result."
        )
    return df


@_log_read("aurora")
def aurora_tables(schema: str = "public", database: str = None) -> pd.DataFrame:
    """
    List the tables and views in an Aurora PostgreSQL schema as a DataFrame
    (columns: schema, table, type). Discovery helper — use it to find what you can
    query with read_aurora. Read from information_schema (no extra permission).

    schema: the Postgres schema to list (default "public").
    database: target a different database on the cluster (default: configured DB).

    Example:
        aurora_tables()
        aurora_tables("analytics")
    """
    # Parameterized via the RDS Data API so the schema value can't inject SQL.
    params = [{"name": "schema", "value": {"stringValue": schema}}]
    sql = (
        "SELECT table_schema AS schema, table_name AS table, table_type AS type "
        "FROM information_schema.tables WHERE table_schema = :schema "
        "ORDER BY table_name"
    )
    return _aurora_execute(sql, database=database, parameters=params)


@_log_read("aurora")
def aurora_schema(table: str, schema: str = "public", database: str = None) -> pd.DataFrame:
    """
    Show the columns of an Aurora PostgreSQL table/view as a DataFrame (columns:
    column, type, nullable, default, position). Discovery helper — use it to see
    what you can SELECT before writing read_aurora. Read from information_schema.

    Accepts "table" or "schema.table"; an explicit schema= wins over a prefix.

    Example:
        aurora_schema("notebooks")
        aurora_schema("analytics.events")
    """
    # Allow a "schema.table" convenience form.
    if '.' in table and schema == "public":
        schema, table = table.split('.', 1)
    params = [
        {"name": "schema", "value": {"stringValue": schema}},
        {"name": "tbl", "value": {"stringValue": table}},
    ]
    sql = (
        "SELECT column_name AS column, data_type AS type, is_nullable AS nullable, "
        "column_default AS default, ordinal_position AS position "
        "FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = :tbl "
        "ORDER BY ordinal_position"
    )
    return _aurora_execute(sql, database=database, parameters=params)
