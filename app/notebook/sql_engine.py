"""
SQL execution engine with intelligent auto-routing.

Part of: app.notebook (application layer)

Transparently routes SQL queries to the appropriate engine based on
unambiguous source detection:

Source Detection Rules (each source type has a unique syntax pattern):
  - LOCAL_FILE:  FROM '/tmp/file.csv'           — quoted path with extension
  - S3_FILE:     FROM read_csv('s3://...')       — read_* function with s3:// URL
  - ATHENA:      FROM database.table_name       — dot-separated, matches the discovered
                                                  Athena catalog (AWS Glue or S3 Tables)
  - AURORA:      FROM db.schema.table           — matches a discovered Amazon Aurora table
  - DYNAMODB:    FROM dynamodb."table-name"     — explicit dynamodb. prefix
  - DATAFRAME:   FROM variable_name             — bare name matching namespace DataFrame

Routing Logic:
  - UNLOAD (SELECT ...) TO 's3://...'                        → Athena UNLOAD (rows go to S3)
  - All sources are DuckDB-compatible (local, S3, DataFrame) → Pure DuckDB
  - All sources are Athena tables                             → Pure Athena (one catalog, or
                                                                federated across catalogs)
  - All sources are Aurora tables                             → Aurora over the RDS Data API
  - Single DynamoDB table (simple query)                     → PartiQL (server-side)
  - Mixed (any remote + any local)                           → Materialize remote → DuckDB

Timeout and interrupt: each SQL cell runs under _SqlRunGuard, with the same
EXECUTION_TIMEOUT_SECONDS budget and error texts as a Python cell. POST /interrupt
reaches it through executor.interrupt().

Endpoint:
  POST /execute-sql - Execute SQL with auto-routing
"""

import os
import re
import ctypes
import asyncio
import threading
import time as _time
import logging
from enum import Enum
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .executor import (
    max_display_rows, build_df_table_payload, execution_timeout_seconds,
    TIMEOUT_ERROR, INTERRUPT_ERROR,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sql-engine"])


# ─── Persistent DuckDB Connection ─────────────────────────────────────────────
# httpfs INSTALL/LOAD is the classic first-S3-query cost (~4.7s cold). A fresh
# duckdb.connect() per query re-pays it every time. We keep ONE module-level
# connection with httpfs installed+loaded once, and only re-apply S3 credentials
# per query (cheap; handles STS token refresh on long-lived VMs).
_DUCKDB_CON = None
_DUCKDB_HTTPFS_LOADED = False


def _get_duckdb_connection():
    """Return the shared DuckDB connection, creating + loading httpfs once."""
    global _DUCKDB_CON, _DUCKDB_HTTPFS_LOADED
    import duckdb

    if _DUCKDB_CON is None:
        _DUCKDB_CON = duckdb.connect()
    if not _DUCKDB_HTTPFS_LOADED:
        try:
            _DUCKDB_CON.execute("INSTALL httpfs; LOAD httpfs;")
            _DUCKDB_HTTPFS_LOADED = True
        except Exception as e:
            logger.warning(f"httpfs install/load warning: {e}")
    return _DUCKDB_CON


def warmup_duckdb():
    """Pre-create the shared connection and install httpfs at snapshot time so the
    first real query never pays the ~4.7s cold cost. Safe to call repeatedly."""
    try:
        _get_duckdb_connection()
    except Exception as e:
        logger.warning(f"DuckDB warmup failed: {e}")


# ─── Timeout / interrupt guard (see run_guard.py) ─────────────────────────────
from .run_guard import _RUN, _SqlRunGuard, _current_guard  # noqa: F401  (re-exported)
# ─── Source Classification ────────────────────────────────────────────────────

class SourceType(Enum):
    LOCAL_FILE = "local_file"      # '/tmp/file.csv' — DuckDB handles directly
    S3_FILE = "s3_file"            # read_csv('s3://...') — DuckDB handles via httpfs
    ATHENA = "athena"              # database.table — needs Athena or materialization
    AURORA = "aurora"              # database.schema.table — Aurora via RDS Data API
    DYNAMODB = "dynamodb"          # dynamodb."table" — needs PartiQL or scan
    DATAFRAME = "dataframe"        # bare variable name — DuckDB handles directly


@dataclass
class SourceRef:
    """A detected source reference in the SQL."""
    source_type: SourceType
    full_ref: str       # As it appears in the SQL (for rewriting)
    table_name: str     # Bare table/file name (for materialization aliases)
    database: str = ""  # Aurora: the database part of db.schema.table
    schema: str = ""    # Aurora: the schema part of db.schema.table
    # Athena only: the non-default Athena data catalog for this table (S3 Tables
    # federated catalog, e.g. "s3tables_microvm"). Empty = default AwsDataCatalog.
    catalog: str = ""


# ─── Routing catalogs (read from the shared DataCatalog) ─────────────────────
# SQL-cell routing resolves each FROM/JOIN reference against the SAME map that
# backs the Data Sources panel: the VM's DataCatalog. Cloud entries in that map
# are discovered + pushed by the PROXY (authoritative for cloud schemas); local
# /tmp entries are VM-discovered. There is deliberately NO separate Glue cache
# here anymore — one map, one source of truth, refreshed on panel refresh.


def _get_routing_catalogs(data_catalog) -> tuple[dict[str, set[str]], dict[tuple, set[str]], dict[str, str]]:
    """Read the Athena and Aurora routing maps from the shared DataCatalog.

    Returns (athena_catalogs, aurora_catalogs, athena_db_catalog):
      - athena_catalogs: { database: {table names} } from source_type=="athena"
        entries (source_id "database.table").
      - aurora_catalogs: { (database, schema): {table names} } from
        source_type=="aurora" entries (source_id "database.schema.table").
      - athena_db_catalog: { database: <athena data-catalog name> } for the
        databases that live in a NON-default Athena catalog (S3 Tables: the
        federated "s3tablescatalog" registered as e.g. "s3tables_microvm"). A db
        absent from this map uses the default AwsDataCatalog. This is how a plain
        2-part "namespace.table" reference gets routed to the federated catalog.

    All empty when the catalog is unavailable/empty, so callers transparently
    skip that backend (and pure-DuckDB / DataFrame routing still works).
    """
    athena: dict[str, set[str]] = {}
    aurora: dict[tuple, set[str]] = {}
    athena_db_catalog: dict[str, str] = {}
    if data_catalog is None:
        return athena, aurora, athena_db_catalog
    try:
        for entry in data_catalog.get_all().get("entries", []):
            stype = entry.get("source_type")
            meta = entry.get("metadata", {}) or {}
            sid = entry.get("source_id", "")
            parts = sid.split(".")
            if stype == "athena":
                # source_id "database.table" (2-part). Prefer the db from metadata;
                # the table is the last dotted segment.
                db = meta.get("database") or (parts[0] if len(parts) >= 2 else None)
                if db and len(parts) >= 2:
                    athena.setdefault(db, set()).add(parts[-1])
                    # Record the non-default Athena catalog for S3 Tables so the
                    # executor sets the right QueryExecutionContext.Catalog.
                    cat = meta.get("catalog")
                    if db and cat:
                        athena_db_catalog[db] = cat
            elif stype == "aurora":
                # source_id "database.schema.table"
                db = meta.get("database")
                schema = meta.get("schema")
                if not (db and schema) and len(parts) == 3:
                    db, schema, _ = parts
                table = parts[-1] if parts else None
                if db and schema and table:
                    aurora.setdefault((db, schema), set()).add(table)
    except Exception as e:
        logger.warning(f"Could not build routing catalogs from DataCatalog: {e}")
    return athena, aurora, athena_db_catalog


# ─── Scalar variable interpolation ────────────────────────────────────────────

# {identifier} placeholders — Python-f-string-like. We only substitute names that
# resolve to a SCALAR in the namespace (int/float/bool/str); DataFrames are queried
# by name (registered as tables), never interpolated, so a {df} is left untouched.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


def _interpolate_scalars(sql: str, namespace: dict) -> tuple[str, list[str]]:
    """Substitute {var} placeholders in the SQL with scalar values from the namespace.

    Returns (interpolated_sql, used_vars). `used_vars` is the sorted list of scalar
    variables that were actually substituted — these are the SQL cell's scalar READS
    (wired into variables_read so reactive execution tracks them).

    Rules:
      - numbers (int/float) and bools → inserted literally (bool → TRUE/FALSE).
      - strings → the RAW value is inserted at the placeholder site; the user writes
        the quotes in their SQL (WHERE region = '{region}'). Embedded single quotes
        are doubled ('' ) so a value like O'Brien can't break out of the string.
      - a {name} that isn't a scalar in the namespace (DataFrame, missing, or a
        non-scalar type) is left AS-IS — DataFrames are handled by table registration,
        and an unknown placeholder surfaces as a normal SQL error rather than a silent
        wrong substitution.
    """
    used: set[str] = set()

    def _sub(m):
        name = m.group(1)
        if name not in namespace:
            return m.group(0)  # leave untouched (could be a df/table or a typo)
        val = namespace[name]
        # bool is a subclass of int — check it first.
        if isinstance(val, bool):
            used.add(name)
            return "TRUE" if val else "FALSE"
        if isinstance(val, (int, float)):
            used.add(name)
            return repr(val)
        if isinstance(val, str):
            used.add(name)
            return val.replace("'", "''")  # escape quotes; user supplies the surrounding quotes
        # Not a supported scalar (DataFrame, list, None, etc.) — leave the placeholder.
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, sql), sorted(used)


# ─── Source Detection ─────────────────────────────────────────────────────────

def _classify_sources(sql: str, athena_catalogs: dict[str, set[str]], namespace_dataframes: set[str],
                      aurora_catalogs: dict[tuple, set[str]] = None,
                      athena_db_catalog: dict[str, str] = None) -> list[SourceRef]:
    """
    Parse the SQL and classify every FROM/JOIN source into its type.

    Detection is deterministic because each source type has a unique syntax /
    catalog match:
      - Local file:  quoted path '/tmp/...' with file extension
      - S3 file:     read_csv/read_json/read_parquet('s3://...')
      - DynamoDB:    dynamodb."table-name" or dynamodb.table_name
      - Aurora:      database.schema.table (3-part, matches the Aurora catalog)
      - Athena:      database.table (2-part, matches the Glue catalog)
      - DataFrame:   bare identifier matching a namespace DataFrame

    Aurora vs Athena disambiguation is by catalog membership + arity, so users
    write natural, portable SQL (no magic prefixes): a 3-part name that exists in
    the Aurora catalog routes to Aurora; a 2-part name that exists in the Glue
    catalog routes to Athena (preserving every existing notebook). `aurora_catalogs`
    maps (database, schema) → {table names}.
    """
    aurora_catalogs = aurora_catalogs or {}
    sources: list[SourceRef] = []

    # Strip SQL comments before classification to avoid false detections
    # Remove single-line comments (-- ...)
    sql_clean = re.sub(r'--[^\n]*', '', sql)
    # Remove multi-line comments (/* ... */)
    sql_clean = re.sub(r'/\*.*?\*/', '', sql_clean, flags=re.DOTALL)

    # 1. Detect S3 files: read_csv('s3://...'), read_json('s3://...'), read_parquet('s3://...')
    s3_pattern = re.compile(
        r'\bread_(csv|json|parquet)\s*\(\s*[\'"]s3://[^\'"]+[\'"]\s*\)',
        re.IGNORECASE
    )
    for m in s3_pattern.finditer(sql_clean):
        sources.append(SourceRef(
            source_type=SourceType.S3_FILE,
            full_ref=m.group(0),
            table_name=m.group(0),  # DuckDB handles the full expression
        ))

    # 1b. Detect local files via DuckDB function: read_csv('/tmp/...'), read_json('/tmp/...')
    local_func_pattern = re.compile(
        r'\bread_(csv|json|parquet)\s*\(\s*[\'"](/tmp/[^\'"]+)[\'"]\s*\)',
        re.IGNORECASE
    )
    for m in local_func_pattern.finditer(sql_clean):
        sources.append(SourceRef(
            source_type=SourceType.LOCAL_FILE,
            full_ref=m.group(0),
            table_name=os.path.basename(m.group(2)),
        ))

    # 2. Detect local files: '/tmp/file.ext'
    local_file_pattern = re.compile(
        r"(?:FROM|JOIN)\s+['\"](/tmp/[^'\"]+)['\"]",
        re.IGNORECASE
    )
    for m in local_file_pattern.finditer(sql_clean):
        sources.append(SourceRef(
            source_type=SourceType.LOCAL_FILE,
            full_ref=f"'{m.group(1)}'",
            table_name=os.path.basename(m.group(1)),
        ))

    # 3. Detect DynamoDB: dynamodb."table-name" or dynamodb.table_name
    dynamo_pattern = re.compile(
        r'(?:FROM|JOIN)\s+(dynamodb\.(?:"[^"]+"|[a-zA-Z_][\w\-]*))',
        re.IGNORECASE
    )
    for m in dynamo_pattern.finditer(sql_clean):
        full_ref = m.group(1)
        name_part = full_ref.split('.', 1)[1]
        table_name = name_part.strip('"')
        sources.append(SourceRef(
            source_type=SourceType.DYNAMODB,
            full_ref=full_ref,
            table_name=table_name,
        ))

    # 3b. Detect Aurora tables: <database>.<schema>.<table> (3-part) that exist in
    #     the Aurora catalog. Runs BEFORE the 2-part Athena pass so a fully-qualified
    #     Aurora reference is claimed first and never half-matched as db.table.
    aurora_claimed_spans: list[tuple[int, int]] = []
    if aurora_catalogs:
        aurora_pattern = re.compile(
            r'(?:FROM|JOIN)\s+([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)',
            re.IGNORECASE
        )
        for m in aurora_pattern.finditer(sql_clean):
            db_part, schema_part, table_part = m.group(1), m.group(2), m.group(3)
            full_ref = f"{db_part}.{schema_part}.{table_part}"
            # Case-insensitive (db, schema) lookup, then confirm the table exists.
            tables = next(
                (t for (db, sch), t in aurora_catalogs.items()
                 if db.lower() == db_part.lower() and sch.lower() == schema_part.lower()),
                None,
            )
            if tables and table_part in tables:
                sources.append(SourceRef(
                    source_type=SourceType.AURORA,
                    full_ref=full_ref,
                    table_name=table_part,
                    database=db_part,
                    schema=schema_part,
                ))
                # Record the matched db.schema span so the 2-part Athena pass below
                # won't re-match the "db.schema" prefix of this reference.
                aurora_claimed_spans.append((m.start(1), m.end(2)))

    # 4. Detect Athena tables: <database>.<table> for ANY configured Glue database
    #    (must match that database's Glue catalog).
    if athena_catalogs:
        db_alt = "|".join(re.escape(db) for db in athena_catalogs.keys())
        athena_pattern = re.compile(
            r'(?:FROM|JOIN)\s+((' + db_alt + r')\.([a-zA-Z_]\w*))',
            re.IGNORECASE
        )
        for m in athena_pattern.finditer(sql_clean):
            full_ref = m.group(1)   # e.g. "microvm_demo_db.sales_data"
            db_part = m.group(2)    # e.g. "microvm_demo_db"
            bare_name = m.group(3)  # e.g. "sales_data"
            # Skip if this match falls inside a span already claimed by a 3-part
            # Aurora reference (i.e. it's really the db.schema prefix of db.schema.table).
            mstart = m.start(1)
            if any(s <= mstart < e for (s, e) in aurora_claimed_spans):
                continue
            # A 3-part reference also leaves a trailing ".table" after the 2-part
            # match; skip when the char right after the match is a dot.
            after = sql_clean[m.end(1):m.end(1) + 1]
            if after == ".":
                continue
            # Resolve the matched database case-insensitively, then confirm the table exists.
            tables = next((t for db, t in athena_catalogs.items() if db.lower() == db_part.lower()), None)
            if tables and bare_name in tables:
                # Resolve the db's Athena catalog (federated S3 Tables) case-insensitively.
                cat = ""
                if athena_db_catalog:
                    cat = next((c for db, c in athena_db_catalog.items()
                                if db.lower() == db_part.lower()), "")
                sources.append(SourceRef(
                    source_type=SourceType.ATHENA,
                    full_ref=full_ref,
                    table_name=bare_name,
                    database=db_part,
                    catalog=cat,
                ))

    # 5. Detect DataFrames: bare identifier after FROM/JOIN (not already classified)
    # Excludes: paths, function calls, dynamodb., database.table
    bare_ref_pattern = re.compile(
        r'(?:FROM|JOIN)\s+([a-zA-Z_]\w*)\b'
        r'(?!\s*\()'           # Not followed by ( → not a function call
        r'(?!\s*\.)',          # Not followed by . → not database.table
        re.IGNORECASE
    )
    already_classified = {s.full_ref.lower() for s in sources}
    for m in bare_ref_pattern.finditer(sql_clean):
        ref = m.group(1)
        # Skip SQL keywords and already-classified refs
        if ref.lower() in ('select', 'from', 'join', 'where', 'group', 'order', 'having',
                           'limit', 'offset', 'union', 'intersect', 'except', 'as',
                           'on', 'and', 'or', 'not', 'in', 'between', 'like',
                           'dynamodb', 'read_csv', 'read_json', 'read_parquet'):
            continue
        if ref.lower() in already_classified:
            continue
        if ref in namespace_dataframes:
            sources.append(SourceRef(
                source_type=SourceType.DATAFRAME,
                full_ref=ref,
                table_name=ref,
            ))

    return sources


# ─── Lineage: source identifiers ──────────────────────────────────────────────
# Identifiers ONLY (table names, s3:// URIs, paths) — never SQL text or literals.

_SQL_KEYWORDS = {
    'select', 'from', 'join', 'where', 'group', 'order', 'having', 'limit',
    'offset', 'union', 'intersect', 'except', 'as', 'on', 'and', 'or', 'not',
    'in', 'between', 'like', 'lateral', 'unnest', 'values', 'only', 'table',
}
# Functions whose argument syntax contains FROM but is not a table reference.
_FROM_FUNCS = {'extract', 'substring', 'substr', 'trim', 'overlay', 'position'}
_IDENT_PART = r'(?:"[^"]+"|[A-Za-z_][\w$]*)'
_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+(' + _IDENT_PART + r'(?:\s*\.\s*' + _IDENT_PART + r'){0,2})',
    re.IGNORECASE,
)
_CTE_NAME_RE = re.compile(
    r'(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)(' + _IDENT_PART + r')\s*(?:\([^)]*\)\s*)?AS\s*\(',
    re.IGNORECASE,
)


def _enclosing_call(sql: str, pos: int) -> str:
    """Lower-cased name of the function call whose parentheses enclose `pos`
    ('' when not inside a call)."""
    depth = 0
    i = pos - 1
    while i >= 0:
        ch = sql[i]
        if ch == ')':
            depth += 1
        elif ch == '(':
            if depth == 0:
                m = re.search(r'([A-Za-z_]\w*)\s*$', sql[:i])
                return m.group(1).lower() if m else ''
            depth -= 1
        i -= 1
    return ''


def table_refs_in_sql(sql: str) -> list[str]:
    """Return the FROM/JOIN table identifiers in `sql` ("t", "db.t", "cat.db.t"),
    quotes stripped, deduped in order. Skips function calls (read_csv(…)), quoted
    path literals, dynamodb.* refs, SQL keywords and CTE names. Comments and string
    literals are removed first, so no literal value can ever leak into a result."""
    if not isinstance(sql, str) or not sql:
        return []
    clean = re.sub(r'--[^\n]*', '', sql)
    clean = re.sub(r'/\*.*?\*/', '', clean, flags=re.DOTALL)
    clean = re.sub(r"'(?:[^']|'')*'", "''", clean)
    ctes = {m.group(1).strip('"').lower() for m in _CTE_NAME_RE.finditer(clean)}
    out: list[str] = []
    seen: set[str] = set()
    for m in _TABLE_REF_RE.finditer(clean):
        rest = clean[m.end(1):].lstrip()
        if rest.startswith('('):
            continue  # function call, e.g. read_parquet(...)
        parts = [p.strip('"') for p in re.findall(_IDENT_PART, m.group(1))]
        if not parts or any(not p for p in parts):
            continue
        if parts[0].startswith('/') or parts[0].lower() == 'dynamodb':
            continue
        if len(parts) == 1 and (parts[0].lower() in _SQL_KEYWORDS or parts[0].lower() in ctes):
            continue
        if _enclosing_call(clean, m.start()) in _FROM_FUNCS:
            continue
        ref = ".".join(parts)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def sources_to_refs(sources: list[SourceRef]) -> list[dict]:
    """Map classified SQL sources to lineage [{kind, id}] identifiers (deduped).
    DataFrame sources are excluded (they are notebook variables, not sources)."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for s in sources or []:
        kind = ident = None
        if s.source_type == SourceType.ATHENA:
            kind, ident = "athena", f"{s.database}.{s.table_name}"
        elif s.source_type == SourceType.AURORA:
            kind, ident = "aurora", f"{s.database}.{s.schema}.{s.table_name}"
        elif s.source_type == SourceType.DYNAMODB:
            kind, ident = "dynamodb", s.table_name
        elif s.source_type == SourceType.S3_FILE:
            m = re.search(r"s3://[^'\"\s)]+", s.full_ref or "")
            kind, ident = "s3", (m.group(0) if m else None)
        elif s.source_type == SourceType.LOCAL_FILE:
            m = re.search(r"['\"]([^'\"]+)['\"]", s.full_ref or "")
            kind, ident = "local", (m.group(1) if m else (s.full_ref or "").strip("'\""))
        if not kind or not ident:
            continue
        key = (kind, ident)
        if key not in seen:
            seen.add(key)
            out.append({"kind": kind, "id": ident})
    return out


# ─── Aurora result-bound helpers ──────────────────────────────────────────────

# Matches a trailing LIMIT clause (optionally with OFFSET) so we don't double-limit
# a query the user already bounded.
_TRAILING_LIMIT_RE = re.compile(r'\bLIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*;?\s*$', re.IGNORECASE)


def _aurora_max_rows() -> int:
    """Row cap for Aurora Data API queries (env AURORA_MAX_ROWS, default 25000).
    The Data API caps each response at ~1MB; this bounds a query so the result is
    complete up to the cap instead of silently truncating. Read at call time so a
    launch-time env change is respected."""
    try:
        n = int(os.environ.get("AURORA_MAX_ROWS", "25000"))
        return n if n > 0 else 25000
    except (TypeError, ValueError):
        return 25000


def _aurora_apply_limit(sql: str, cap: int) -> tuple[str, bool]:
    """If `sql` has no trailing LIMIT, append `LIMIT cap`. Returns
    (possibly_modified_sql, injected) where `injected` is True when we added one.
    A query that already has its own LIMIT is left untouched (user intent wins)."""
    stripped = sql.rstrip().rstrip(";").rstrip()
    if _TRAILING_LIMIT_RE.search(sql):
        return sql, False
    return f"{stripped}\nLIMIT {cap}", True


# ─── Main Endpoint ────────────────────────────────────────────────────────────

@router.post("/execute-sql")
async def execute_sql(request: Request):
    """
    Execute a SQL query with intelligent auto-routing between DuckDB, Athena, and DynamoDB.

    Request body:
        {
            "sql": "SELECT * FROM microvm_demo_db.sales_data LIMIT 10",
            "output_variable": "result"  (optional, name for the result DataFrame)
        }

    Thin wrapper over run_sql_cell (the reusable per-cell runner), so /execute-batch
    can run SQL cells through the identical path and get the identical result dict.

    SERIALIZATION: acquires the SAME app-state execution lock that /execute and
    /execute-batch use, so a standalone SQL cell can NEVER run concurrently with a
    Python /execute (or an agent/entity-stats probe). This matters because a code
    /execute swaps the process-global sys.stdout to a capture buffer on a worker
    thread; if a SQL cell ran at the same time, its DuckDB/boto3 output (or a stray
    DataFrame repr) would land on the REAL fd → the VM log. The lock is acquired
    HERE (the standalone entry point), not inside run_sql_cell — because
    /execute-batch already holds this lock when it calls run_sql_cell, and an
    asyncio.Lock is NOT reentrant (acquiring it there would deadlock the batch).
    """
    from .code_engine import _get_execute_lock

    body = await request.json()
    sql = (body.get("sql", "") or "").strip()
    output_var = body.get("output_variable", "result")
    cell_id = body.get("cell_id")  # frontend cell id, recorded as variable provenance
    # An agent test run (Fix SQL verify, origin "agent") never binds its result
    # into the user namespace nor records a provenance write / run ledger entry;
    # the response is unchanged (it still echoes output_variable).
    bind_output = body.get("origin", "") != "agent"
    if not sql:
        return JSONResponse(status_code=400, content={"error": "No SQL provided"})
    lock = _get_execute_lock(request)
    async with lock:
        # capture_runtime only from this route: the dict carries the runtime
        # section {clock, vars, runs} on success and error (batch frames do not).
        return await run_sql_cell(request, sql, output_var, cell_id, capture_runtime=True,
                                  bind_output=bind_output)


async def run_sql_cell(request: Request, sql: str, output_var: str = "result",
                       cell_id: str = None, capture_runtime: bool = False,
                       bind_output: bool = True) -> dict:
    """Run ONE SQL cell and return the /execute-sql result dict.

    Extracted from the /execute-sql handler so the batch endpoint can run SQL cells
    through the exact same routing + result-shaping logic. Returns the same dict the
    single endpoint returns (success/output/html/error/execution_time_ms/peak_*/
    row_count/column_count/size_bytes/output_variable/engine/variables_read/created/
    sources_read — the last also on the error dict: [{kind, id}] identifiers only).

    The blocking work (DuckDB / Athena polling / Aurora Data API / DynamoDB,
    result shaping) runs in a worker thread via asyncio.to_thread so the event
    loop keeps serving /health, /metrics, etc. during a long query. The caller
    still holds the shared execute lock across this await (execute_sql /
    execute_batch), so SQL cells keep serializing with /execute and batch cells.
    """
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    data_catalog = getattr(request.app.state, "data_catalog", None)
    session_state["request_count"] += 1

    return await asyncio.to_thread(
        _run_sql_cell_blocking, executor, data_catalog, sql, output_var, cell_id,
        capture_runtime=capture_runtime, bind_output=bind_output,
    )


def _run_sql_cell_blocking(executor, data_catalog, sql: str, output_var: str = "result",
                           cell_id: str = None, capture_runtime: bool = False,
                           bind_output: bool = True) -> dict:
    """Synchronous body of run_sql_cell (runs in a worker thread). Returns the
    same result dict (success and error shapes) as before the off-loop move.

    After the query (success AND error) the run ledger records the SQL text this
    cell executed — BEFORE the runtime section is taken, so result["runtime"]
    (only when capture_runtime, the /execute-sql route) already contains it.
    cell_code_hash strips SQL inside the hash, so stripped and raw text agree.

    bind_output=False (agent test runs): the result is not stored in the
    namespace, no provenance write and no run ledger entry; same result dict.

    The query runs under a _SqlRunGuard (timeout + interrupt, see that class)."""
    guard = _SqlRunGuard(executor)
    t0 = _time.perf_counter()
    guard.start()
    try:
        try:
            result = _run_sql_cell_core(executor, data_catalog, sql, output_var, cell_id,
                                        bind_output=bind_output, guard=guard)
        finally:
            guard.finish()
    except KeyboardInterrupt:
        # The guard's single injected KeyboardInterrupt landed outside the query's
        # own handler (between statements or inside finish()).
        guard.finish()
        logger.warning(f"  ✗ SQL ERROR (len={len(sql or '')})")
        logger.warning(f"    {guard.error_message()}")
        result = {
            "success": False, "output": None, "html": None,
            "error": guard.error_message(),
            "execution_time_ms": round((_time.perf_counter() - t0) * 1000, 1),
            "peak_mem_mb": None, "peak_cpu_pct": None, "sources_read": [],
        }
    record_run = getattr(executor, "record_run", None)
    if record_run is not None and bind_output:
        record_run(cell_id, "sql", sql, result.get("success") is True)
    if capture_runtime and hasattr(executor, "runtime_section"):
        result["runtime"] = executor.runtime_section()
    return result


def _run_sql_cell_core(executor, data_catalog, sql: str, output_var: str = "result",
                       cell_id: str = None, bind_output: bool = True,
                       guard: "_SqlRunGuard" = None) -> dict:
    """Query routing + result shaping for one SQL cell (see _run_sql_cell_blocking).
    guard: when it fired (timeout / interrupt), the error dict carries its text
    instead of the engine's own exception message."""
    import pandas as pd

    logger.info(f"▶ Executing SQL (len={len(sql)})")

    # Sample peak CPU/memory for the duration of the query (guest side), so SQL cells
    # carry the same per-cell resource breakdown as code cells in run history. No-op
    # if psutil is unavailable; stopped in BOTH the success and error paths below.
    from .executor import _ResourceSampler
    sampler = _ResourceSampler()
    sampler.start()

    start = _time.perf_counter()
    sources_read: list = []
    try:
        # --- Configuration ---
        athena_workgroup = os.environ.get("ATHENA_WORKGROUP", "microvm-demo")
        aws_region = os.environ.get("AWS_REGION", "us-west-2")

        # --- Step 0: Interpolate {var} scalar placeholders from the namespace ---
        # e.g. `LIMIT {noofOrders}` or `WHERE region = '{region}'`. Done BEFORE source
        # classification so the resolved SQL is what everything downstream sees. The
        # substituted scalar names are the cell's scalar READS (for reactive tracking).
        sql, scalar_reads = _interpolate_scalars(sql, executor._namespace)

        # --- Step 1: Get the Athena + Aurora routing maps from the shared
        #     DataCatalog (the SAME map that backs the Data Sources panel). Cloud
        #     entries were discovered + pushed by the proxy; routing an SQL cell is
        #     just a lookup of db.table / db.schema.table against this map. Both maps
        #     are empty when nothing's been discovered yet, so detection cleanly
        #     no-ops and pure-DuckDB/DataFrame queries still route correctly.
        athena_catalogs, aurora_catalogs, athena_db_catalog = _get_routing_catalogs(
            data_catalog
        )

        # --- Step 2: Classify all sources in the SQL ---
        namespace_dataframes = {k for k, v in executor._namespace.items() if isinstance(v, pd.DataFrame)}
        sources = _classify_sources(sql, athena_catalogs, namespace_dataframes, aurora_catalogs,
                                    athena_db_catalog)
        # Lineage: source IDENTIFIERS this cell reads (never the SQL). Best-effort.
        try:
            sources_read = sources_to_refs(sources)
        except Exception:
            sources_read = []

        # Group by type
        source_types = {s.source_type for s in sources}
        athena_sources = [s for s in sources if s.source_type == SourceType.ATHENA]
        aurora_sources = [s for s in sources if s.source_type == SourceType.AURORA]
        dynamo_sources = [s for s in sources if s.source_type == SourceType.DYNAMODB]
        duckdb_types = {SourceType.LOCAL_FILE, SourceType.S3_FILE, SourceType.DATAFRAME}

        # --- Step 3: Route execution ---
        engine_used = "duckdb"
        result_df = None
        # Extra warnings appended to the cell output (e.g. Aurora Data API caps).
        extra_warnings: list[str] = []
        # Destination surfaced to the user for an UNLOAD cell (set in that branch).
        unload_dest: str = None

        if _is_unload_statement(sql):
            # UNLOAD (SELECT …) TO 's3://…' WITH (…): Athena writes the result
            # STRAIGHT TO S3 — the rows never enter the VM. This breaks the normal
            # "return rows" contract, so it bypasses every source-routing branch
            # below (which all call get_query_results). We run it through the SHARED
            # row-free runner in readers.py and return a small file-list DataFrame.
            engine_used = "athena-unload"
            result_df, unload_dest = _run_unload_cell(sql, aws_region)

        elif source_types <= duckdb_types or len(sources) == 0:
            # Pure DuckDB: all sources are local files, S3 files, or DataFrames
            engine_used = "duckdb"
            result_df = _run_duckdb_query(sql, executor)

        elif source_types == {SourceType.ATHENA}:
            # Pure Athena: all tables are Athena. They may all be in the default
            # AwsDataCatalog, or all in ONE federated catalog (S3 Tables), in which
            # case we run with that catalog's execution context and rewrite the
            # federated "namespace.table" refs to plain table names. If the Athena
            # sources SPAN more than one catalog (e.g. a default table JOINed with an
            # S3 Tables table), we fully-qualify each ref and let Athena federate the
            # join server-side (see the else branch) — NOT DuckDB materialization.
            distinct_catalogs = {s.catalog for s in athena_sources}
            if len(distinct_catalogs) == 1:
                engine_used = "athena"
                the_catalog = next(iter(distinct_catalogs))  # "" = default catalog
                exec_sql = sql
                exec_db = ""
                if the_catalog:
                    # Federated (S3 Tables): rewrite every "namespace.table" → "table"
                    # and run within the namespace as the Database in the context.
                    fed_db = athena_sources[0].database
                    exec_db = fed_db
                    for src in athena_sources:
                        exec_sql = re.sub(r'\b' + re.escape(src.full_ref) + r'\b',
                                          src.table_name, exec_sql)
                    result_df = _run_athena_query_sync(
                        exec_sql, athena_workgroup, aws_region,
                        catalog=the_catalog, database=exec_db)
                else:
                    result_df = _run_athena_query_sync(exec_sql, athena_workgroup, aws_region)
            else:
                # Athena sources span multiple catalogs (e.g. a default-catalog Glue
                # table JOINed with an S3 Tables / Iceberg table). Athena FEDERATES
                # this in ONE server-side query when each table is referenced by its
                # fully-qualified "catalog"."database"."table" name — so we rewrite
                # each source's ref to that 3-part form and run natively, exactly like
                # read_athena does. (Previously we materialized each table and joined
                # in DuckDB; native federation is faster and pulls no data into the VM.)
                engine_used = "athena"
                exec_sql = sql
                for src in athena_sources:
                    cat = src.catalog or "AwsDataCatalog"
                    qualified = f'"{cat}"."{src.database}"."{src.table_name}"'
                    exec_sql = re.sub(r'\b' + re.escape(src.full_ref) + r'\b',
                                      qualified, exec_sql)
                # Every ref is now self-qualified, so the execution-context catalog is
                # irrelevant; run in the default AwsDataCatalog context.
                result_df = _run_athena_query_sync(exec_sql, athena_workgroup, aws_region)

        elif source_types == {SourceType.AURORA}:
            # Pure Aurora: all sources are Aurora tables. Postgres accepts the
            # 3-part db.schema.table syntax as schema.table within the connected
            # database, so we strip the leading "db." and send the SQL over the
            # Data API unchanged otherwise.
            engine_used = "aurora"
            aurora_sql = sql
            for src in aurora_sources:
                aurora_sql = re.sub(
                    r'\b' + re.escape(src.full_ref) + r'\b',
                    f"{src.schema}.{src.table_name}",
                    aurora_sql,
                )
            # Bound the result to stay under the Data API's ~1MB cap: append
            # LIMIT AURORA_MAX_ROWS only when the user's query has no LIMIT of its
            # own (their intent wins). This makes the returned rows complete up to
            # the cap instead of silently truncating.
            cap = _aurora_max_rows()
            aurora_sql, _limit_injected = _aurora_apply_limit(aurora_sql, cap)
            # All Aurora sources share the connected database (the default). If a
            # non-default database is referenced, target it explicitly.
            target_db = aurora_sources[0].database or None
            result_df = _run_aurora_query(aurora_sql, aws_region, database=target_db)
            # Warn if the result was capped — either by our injected LIMIT (row cap
            # reached) or by the Data API's 1MB ceiling (wide rows truncate below
            # the cap). df.attrs is set by _aurora_execute.
            _n = len(result_df)
            if result_df.attrs.get("aurora_api_capped"):
                extra_warnings.append(
                    f"⚠️ Aurora result hit the Data API ~1MB limit ({_n:,} rows returned) — "
                    f"the result is INCOMPLETE. Add a WHERE filter or select fewer columns; "
                    f"for large extracts use the proxy's psycopg2 path."
                )
            elif _limit_injected and _n >= cap:
                extra_warnings.append(
                    f"⚠️ Aurora result capped at {cap:,} rows (AURORA_MAX_ROWS) — "
                    f"add your own LIMIT or a WHERE filter to see the rest."
                )

        elif source_types == {SourceType.DYNAMODB} and len(dynamo_sources) == 1:
            # Single DynamoDB table — try PartiQL first
            table_name = dynamo_sources[0].table_name
            full_ref = dynamo_sources[0].full_ref
            partiql_sql = sql.replace(full_ref, f'"{table_name}"')

            partiql_result = _try_partiql(partiql_sql, table_name, aws_region)
            if partiql_result is not None:
                engine_used = "dynamodb-partiql"
                result_df = partiql_result
            else:
                # Fallback: scan table into DataFrame, then DuckDB
                engine_used = "duckdb+dynamodb"
                # Push the LIMIT into the scan only for a plain projection; a
                # GROUP BY / ORDER BY / WHERE / ... needs the full table.
                dynamo_limit = _dynamo_scan_limit(sql)
                safe_alias = f"_dynamo_{table_name.replace('-', '_')}"
                materialized = {safe_alias: _scan_dynamodb_table(table_name, aws_region, limit=dynamo_limit)}
                rewritten_sql = sql.replace(full_ref, safe_alias)
                result_df = _run_duckdb_query(rewritten_sql, executor, materialized)

        else:
            # Mixed sources — materialize remote tables (Athena, DynamoDB) into DataFrames, then DuckDB
            engine_parts = []
            materialized = {}
            rewritten_sql = sql

            # Materialize Athena tables
            if athena_sources:
                engine_parts.append("athena")
                for src in athena_sources:
                    # Alias includes the db so identically-named tables in different DBs don't collide.
                    safe_alias = "_athena_" + re.sub(r'\W', '_', src.full_ref)
                    if src.catalog:
                        # S3 Tables (federated catalog): plain table name + catalog context.
                        athena_sql = f"SELECT * FROM {src.table_name}"
                        materialized[safe_alias] = _run_athena_query_sync(
                            athena_sql, athena_workgroup, aws_region,
                            catalog=src.catalog, database=src.database)
                    else:
                        # Use the source's OWN database (full_ref = db.table), not a single default,
                        # so tables from any configured database materialize correctly.
                        athena_sql = f"SELECT * FROM {src.full_ref}"
                        materialized[safe_alias] = _run_athena_query_sync(athena_sql, athena_workgroup, aws_region)
                    rewritten_sql = re.sub(r'\b' + re.escape(src.full_ref) + r'\b', safe_alias, rewritten_sql)

            # Materialize Aurora tables (full SELECT over the Data API → DataFrame)
            if aurora_sources:
                engine_parts.append("aurora")
                cap = _aurora_max_rows()
                for src in aurora_sources:
                    # Bound the materialized pull to stay under the Data API's ~1MB
                    # cap. A mixed query does `SELECT * FROM table` to pull the whole
                    # Aurora table into DuckDB for the join — without a LIMIT this
                    # would silently truncate at 1MB, so a join would run on partial
                    # data. Cap it and warn if the cap (or the 1MB ceiling) is hit.
                    aurora_sql = f"SELECT * FROM {src.schema}.{src.table_name} LIMIT {cap}"
                    safe_alias = "_aurora_" + re.sub(r'\W', '_', src.full_ref)
                    target_db = src.database or None
                    mdf = _run_aurora_query(aurora_sql, aws_region, database=target_db)
                    materialized[safe_alias] = mdf
                    rewritten_sql = re.sub(r'\b' + re.escape(src.full_ref) + r'\b', safe_alias, rewritten_sql)
                    _mn = len(mdf)
                    if mdf.attrs.get("aurora_api_capped"):
                        extra_warnings.append(
                            f"⚠️ Aurora table '{src.schema}.{src.table_name}' hit the Data API "
                            f"~1MB limit ({_mn:,} rows materialized) — the join used INCOMPLETE data. "
                            f"Filter the Aurora side (a WHERE on the source table) to fit."
                        )
                    elif _mn >= cap:
                        extra_warnings.append(
                            f"⚠️ Aurora table '{src.schema}.{src.table_name}' materialized at the "
                            f"{cap:,}-row cap (AURORA_MAX_ROWS) — the join may be missing rows. "
                            f"Filter the Aurora side to fit under the cap."
                        )

            # Materialize DynamoDB tables
            if dynamo_sources:
                engine_parts.append("dynamodb")
                for src in dynamo_sources:
                    safe_alias = f"_dynamo_{src.table_name.replace('-', '_')}"
                    dynamo_limit = None
                    if len(dynamo_sources) == 1 and not athena_sources and not aurora_sources:
                        dynamo_limit = _dynamo_scan_limit(sql)
                    materialized[safe_alias] = _scan_dynamodb_table(src.table_name, aws_region, limit=dynamo_limit)
                    rewritten_sql = rewritten_sql.replace(src.full_ref, safe_alias)

            engine_used = "duckdb+" + "+".join(engine_parts)
            result_df = _run_duckdb_query(rewritten_sql, executor, materialized)

        elapsed_ms = (_time.perf_counter() - start) * 1000
        sampler.stop()

        # --- Step 4: Store result and format output ---
        if bind_output and output_var and output_var.isidentifier():
            executor._namespace[output_var] = result_df
            executor.record_var_write(output_var, cell_id)

        # Def-use metadata for the reactive dependency graph. A SQL cell READS every
        # namespace DataFrame it queries (already classified as DATAFRAME sources —
        # e.g. `... FROM XX JOIN t`) and WRITES its output variable. Emitting these
        # makes SQL cells first-class graph nodes, so a chain
        # param → XX (df) → SQL(reads XX, writes YY) → readers-of-YY stays connected.
        df_reads = {s.table_name for s in sources if s.source_type == SourceType.DATAFRAME}
        # Union DataFrame sources with the scalar {var} placeholders that were
        # interpolated — both are genuine reads this SQL cell depends on.
        sql_reads = sorted(df_reads | set(scalar_reads))
        sql_writes = [output_var] if (output_var and output_var.isidentifier()) else []

        row_count = len(result_df)
        col_count = len(result_df.columns)
        # In-memory size of the result frame (deep=True counts object/string data,
        # not just pointers). Cheap for the modest result sizes we return.
        try:
            size_bytes = int(result_df.memory_usage(deep=True).sum())
        except Exception:
            size_bytes = None
        max_rows = max_display_rows()
        # Serialize to the same JSON table payload the executor emits, so SQL results
        # flow through the client-side DataGrid (search/sort/pagination) like any other
        # DataFrame. build_df_table_payload handles the row cap + truncation metadata.
        html = build_df_table_payload(result_df)
        if row_count > max_rows:
            output = f"{row_count} rows × {col_count} columns (showing first {max_rows})"
        else:
            output = f"{row_count} rows × {col_count} columns"

        # Engine indicator
        engine_label = {
            "duckdb": "🦆 DuckDB",
            "athena": "⚡ Athena",
            "aurora": "🐘 Aurora",
            "duckdb+athena": "⚡🦆 Athena → DuckDB",
            "duckdb+aurora": "🐘🦆 Aurora → DuckDB",
            "duckdb+dynamodb": "🔶🦆 DynamoDB (scan) → DuckDB",
            "dynamodb-partiql": "🔶 DynamoDB (PartiQL)",
            "duckdb+athena+dynamodb": "⚡🔶🦆 Athena+DynamoDB → DuckDB",
            "duckdb+athena+aurora": "⚡🐘🦆 Athena+Aurora → DuckDB",
            "athena-unload": "⚡→📦 Athena UNLOAD → S3",
        }
        output = f"{output}  •  {engine_label.get(engine_used, engine_used)}"

        # For an UNLOAD cell, the result frame lists the files; the useful headline is
        # WHERE the data landed. Surface the destination prominently.
        if engine_used == "athena-unload" and unload_dest:
            output = (f"Wrote {row_count} file(s) → {unload_dest}  •  "
                      f"{engine_label['athena-unload']}")

        # Large DynamoDB table warning
        for src in dynamo_sources:
            cache_key = f"{src.table_name}:{aws_region}"
            if hasattr(_scan_dynamodb_table, '_cache') and cache_key in _scan_dynamodb_table._cache:
                cached_size = len(_scan_dynamodb_table._cache[cache_key])
                if cached_size > 10000:
                    output += f"\n⚠️ DynamoDB '{src.table_name}' has {cached_size:,} rows — consider adding LIMIT"

        # Aurora Data API cap / truncation warnings (collected during routing).
        for w in extra_warnings:
            output += f"\n{w}"

        executor._execution_count += 1

        # Log SQL success with KEY INFO only — engine, result shape, output var,
        # latency, and the query LENGTH. We deliberately do NOT log the SQL text
        # itself: a query is user data (may embed literals/PII), and logs should
        # carry identifiers/metrics, not the payload.
        logger.info(f"  ✓ SQL OK ({elapsed_ms:.0f}ms) {row_count}×{col_count} engine={engine_used} → {output_var} (len={len(sql or '')})")

        return {
            "success": True,
            "output": output,
            "html": html,
            "error": None,
            "execution_time_ms": round(elapsed_ms, 1),
            "peak_mem_mb": round(sampler.peak_mem_mb, 1),
            "peak_cpu_pct": round(sampler.peak_cpu_pct, 1),
            "execution_number": executor._execution_count,
            "row_count": row_count,
            "column_count": col_count,
            "size_bytes": size_bytes,
            "output_variable": output_var,
            "engine": engine_used,
            "variables_read": sql_reads,       # namespace DataFrames this query reads
            "variables_created": sql_writes,   # the output variable it writes
            "sources_read": sources_read,      # [{kind, id}] source identifiers (lineage)
        }

    except (Exception, KeyboardInterrupt) as e:
        elapsed_ms = (_time.perf_counter() - start) * 1000
        sampler.stop()
        error_msg = guard.error_message() if guard is not None and guard.fired else str(e)
        # Key info only — query LENGTH, not the SQL text (user data). The error
        # message itself (below) is the diagnostic.
        logger.warning(f"  ✗ SQL ERROR ({elapsed_ms:.0f}ms) (len={len(sql or '')})")
        logger.warning(f"    {error_msg}")
        return {
            "success": False,
            "output": None,
            "html": None,
            "error": error_msg,
            "execution_time_ms": round(elapsed_ms, 1),
            "peak_mem_mb": round(sampler.peak_mem_mb, 1),
            "peak_cpu_pct": round(sampler.peak_cpu_pct, 1),
            "sources_read": sources_read,
        }


# ─── Execution Engines ────────────────────────────────────────────────────────


def _run_duckdb_query(sql: str, executor, extra_dataframes: dict = None):
    """Execute SQL via DuckDB with namespace DataFrames + optional extras registered."""
    import pandas as pd
    import boto3

    # Shared persistent connection: httpfs INSTALL/LOAD happens once (or at warmup),
    # not per query.
    con = _get_duckdb_connection()

    # (Re)apply S3 credentials each query — cheap (a few SET statements) and keeps
    # long-lived VMs working across STS token refreshes. A FRESH boto3.Session() is
    # used each time (not the global default) so we always read the CURRENT execution-
    # role creds — this is resilient to the snapshot-frozen-creds hazard that the /run
    # hook guards against (see app/platform/hooks.py::_reset_boto3_credentials).
    try:
        region = os.environ.get('AWS_REGION', 'us-west-2')
        con.execute(f"SET s3_region = '{region}';")

        session = boto3.Session()
        creds = session.get_credentials()
        if creds:
            frozen = creds.get_frozen_credentials()
            con.execute(f"SET s3_access_key_id = '{frozen.access_key}';")
            con.execute(f"SET s3_secret_access_key = '{frozen.secret_key}';")
            if frozen.token:
                con.execute(f"SET s3_session_token = '{frozen.token}';")
    except Exception as e:
        logger.warning(f"httpfs cred setup warning: {e}")

    # Register DataFrames for this query. con.register replaces any existing view
    # of the same name, so re-registering across queries is safe. Track names to
    # unregister afterward so stale views don't leak on the shared connection.
    _registered = []

    def _safe_register(name, df):
        """Register df as a DuckDB view without letting one bad frame break the query.

        DuckDB rejects some pandas dtypes (e.g. period[M] from .dt.to_period('M')).
        Because EVERY namespace DataFrame is registered before each SQL cell, a single
        such frame used to fail all SQL cells — even ones reading an unrelated file.
        On failure, retry with Period columns (and a PeriodIndex) converted to
        timestamps; if it still fails, skip the frame (logged) so other queries run."""
        try:
            con.register(name, df)
            _registered.append(name)
            return
        except Exception as e:
            first_err = e
        try:
            fixed = df.copy(deep=False)
            converted = False
            for col in fixed.columns:
                if isinstance(fixed[col].dtype, pd.PeriodDtype):
                    fixed[col] = fixed[col].dt.to_timestamp()
                    converted = True
            if isinstance(fixed.index, pd.PeriodIndex):
                fixed.index = fixed.index.to_timestamp()
                converted = True
            if converted:
                con.register(name, fixed)
                _registered.append(name)
                return
        except Exception as e:
            first_err = e
        logger.warning(f"SQL: skipping DataFrame '{name}' (not registrable in DuckDB): {first_err}")

    # Register namespace DataFrames
    for name, value in executor._namespace.items():
        if isinstance(value, pd.DataFrame):
            _safe_register(name, value)

    # Register extra materialized DataFrames (from Athena/DynamoDB)
    if extra_dataframes:
        for name, df in extra_dataframes.items():
            _safe_register(name, df)

    # A timeout / interrupt of the cell stops the running query natively
    # (raises duckdb.InterruptException); the shared connection stays usable.
    guard = _current_guard()
    if guard is not None:
        guard.add_cancel(con.interrupt)
    try:
        result = con.execute(sql).fetchdf()
    finally:
        if guard is not None:
            guard.remove_cancel(con.interrupt)
        # Detach the DataFrames from the shared connection to avoid holding
        # references / leaking stale views between queries. Never close the con.
        for name in _registered:
            try:
                con.unregister(name)
            except Exception:
                pass
    return result


def _run_athena_query_sync(sql: str, workgroup: str, region: str,
                           catalog: str = "", database: str = ""):
    """Execute SQL via Athena and return a typed DataFrame.
    Synchronous: called from _run_sql_cell_blocking, which already runs in a
    worker thread. Runs on the shared Athena runner (helpers/_athena.py), the same
    one read_athena uses: the poll is bounded by the cell's guard budget
    (EXECUTION_TIMEOUT_SECONDS, the Python-cell timeout text), stop_query_execution
    is called at most once on timeout, interrupt or failure, and the result frame
    is the same as read_athena's (NULL is None, dtypes from Athena's declared
    column types).
    catalog/database (optional): when set, run the query in that Athena data
    catalog + database via QueryExecutionContext — required for S3 Tables in the
    federated "s3tablescatalog" catalog. Default (empty) targets AwsDataCatalog.
    The SQL must reference plain table names (the inline "catalog"."db"."table"
    form does not parse), so the caller strips the namespace prefix first."""
    from .helpers._athena import run_athena
    return run_athena(sql, workgroup=workgroup, region=region,
                      catalog=catalog or None, database=database or None)
def _is_unload_statement(sql: str) -> bool:
    """True if the SQL cell is a leading UNLOAD. Delegates to the readers.py detector
    so the SQL-cell and helper paths agree on what counts as UNLOAD."""
    from app.notebook.helpers.readers import _is_unload_statement
    return _is_unload_statement(sql)


def _run_unload_cell(sql: str, region: str):
    """Run a SQL-cell UNLOAD through the shared row-free runner and return
    (file_list_df, dest). The result frame lists the files Athena wrote (file,
    size_bytes) — NOT the data rows — so a SQL cell can UNLOAD big data to S3 without
    anything transiting VM memory. The destination is surfaced separately for the
    cell's headline output."""
    import pandas as pd
    from app.notebook.helpers.readers import _run_unload_sql_cell
    res = _run_unload_sql_cell(sql, region)
    df = pd.DataFrame(
        [{"file": f, "size_bytes": None} for f in res.files]
    ) if res.files else pd.DataFrame(columns=["file", "size_bytes"])
    # Fill sizes from the result object if we have the per-file breakdown; total is on
    # the result. Keep it simple: show the file list; total bytes rides in the log.
    return df, res.s3_uri


def _run_aurora_query(sql: str, region: str, database: str = None):
    """Execute SQL against Aurora PostgreSQL over the RDS Data API, returning a
    DataFrame. Delegates to the shared executor in helpers so cell queries, the
    read_aurora() helper, and catalog discovery all go through ONE code path
    (typed-field unwrapping + Postgres dtype coercion live there)."""
    from app.notebook.helpers import _aurora_execute
    return _aurora_execute(sql, database=database, region=region)


# Select clauses a LIMIT pushdown tolerates: the projection, the single FROM and the
# LIMIT itself ("from" / "from_" depending on the sqlglot version). Anything else
# (WHERE, GROUP BY, HAVING, ORDER BY, DISTINCT, OFFSET, JOIN, QUALIFY, WITH, ...)
# needs the whole table, so the scan must not be truncated.
_PUSHDOWN_SELECT_ARGS = frozenset({"expressions", "limit", "from", "from_"})


def _dynamo_scan_limit(sql: str):
    """The trailing `LIMIT n` that may be pushed into the DynamoDB scan, or None.

    A scan with Limit=n reads the first n items in storage order, so pushing the
    limit down is only correct when the query keeps its first n input rows as they
    are: a plain single-table projection `SELECT <cols> FROM <table> [LIMIT n]`.
    Any filter, grouping, ordering, DISTINCT, OFFSET, join, subquery, set operation,
    aggregate, window function or row-multiplying function (UNNEST) returns None, so
    the table is scanned fully and DuckDB applies the LIMIT. Parsed with sqlglot
    (shipped in the VM image); any parse problem also returns None."""
    try:
        import sqlglot
        from sqlglot import exp
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except Exception:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return None
    select = statements[0]
    if any(v for k, v in select.args.items() if k not in _PUSHDOWN_SELECT_ARGS):
        return None
    limit = select.args.get("limit")
    value = limit.args.get("expression") if isinstance(limit, exp.Limit) else None
    if not isinstance(value, exp.Literal) or value.is_string:
        return None
    try:
        n = int(value.this)
    except (TypeError, ValueError):
        return None
    if n <= 0 or len(list(select.find_all(exp.Table))) != 1:
        return None
    blocked = (exp.Subquery, exp.AggFunc, exp.Window, exp.Unnest, exp.Explode, exp.UDTF)
    for node in select.walk():
        if node is not select and (isinstance(node, exp.Select) or isinstance(node, blocked)):
            return None
    return n


def _scan_dynamodb_table(table_name: str, region: str, limit: int = None):
    """
    Scan a DynamoDB table and return all items as a DataFrame.
    Supports LIMIT pushdown and caching with 5-minute TTL.
    """
    import boto3
    import pandas as pd
    from decimal import Decimal

    CACHE_TTL_SECONDS = 300  # 5 minutes

    cache_key = f"{table_name}:{region}"
    if not hasattr(_scan_dynamodb_table, '_cache'):
        _scan_dynamodb_table._cache = {}
    if not hasattr(_scan_dynamodb_table, '_cache_ts'):
        _scan_dynamodb_table._cache_ts = {}

    # Check cache (with TTL)
    if cache_key in _scan_dynamodb_table._cache and not limit:
        cached_at = _scan_dynamodb_table._cache_ts.get(cache_key, 0)
        if _time.time() - cached_at < CACHE_TTL_SECONDS:
            cached = _scan_dynamodb_table._cache[cache_key]
            logger.info(f"🔶 DynamoDB cache hit: {table_name} ({len(cached)} rows)")
            return cached
        else:
            del _scan_dynamodb_table._cache[cache_key]
            del _scan_dynamodb_table._cache_ts[cache_key]

    from .helpers import _dynamo
    desc = None if limit else _dynamo.table_item_count(table_name, region)
    if desc and desc > 10000:
        logger.warning(f"⚠ DynamoDB table '{table_name}' has ~{desc:,} items — full scan may be slow")
    # One reader shared with read_dynamodb. fill_limit: a page can stop short of Limit
    # (1 MB page cap), so keep reading until n items. The reader checks the SQL run
    # guard before every page, so a timeout or Stop ends the scan at a page boundary.
    items = _dynamo.scan_items(table_name, region,
                               limit=limit if (limit and limit > 0) else None,
                               fill_limit=True)
    if not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)

    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, Decimal)).any():
            df[col] = df[col].astype(float)

    if not limit:
        _scan_dynamodb_table._cache[cache_key] = df
        _scan_dynamodb_table._cache_ts[cache_key] = _time.time()
        logger.info(f"🔶 DynamoDB scanned & cached: {table_name} ({len(df)} rows, TTL=5m)")
    else:
        logger.info(f"🔶 DynamoDB scanned (limit={limit}): {table_name} ({len(df)} rows)")

    return df


def _try_partiql(sql: str, table_name: str, region: str):
    """
    Try executing SQL via DynamoDB PartiQL.
    Returns a DataFrame on success, or None if PartiQL can't handle it.

    PartiQL supports: SELECT, WHERE (with partition/sort key), LIMIT (via API param).
    PartiQL does NOT support: JOIN, GROUP BY, HAVING, UNION, ORDER BY (without WHERE).
    """
    import boto3
    import pandas as pd
    from decimal import Decimal

    # PartiQL is the cheapest path (server-side on DynamoDB) but supports only
    # simple SELECT/WHERE/LIMIT. When the query uses SQL PartiQL can't run, that's
    # NOT an error — the engine deliberately falls back to a scan + DuckDB (which
    # DOES support GROUP BY/JOIN/ORDER BY). These are internal routing decisions;
    # the user sees the OUTCOME in the INFO "✓ SQL OK … engine=duckdb+dynamodb"
    # line, so log the pre-decision at DEBUG to keep the default view clean.
    unsupported = re.search(r'\b(JOIN|GROUP\s+BY|HAVING|UNION|INTERSECT|EXCEPT)\b', sql, re.IGNORECASE)
    if unsupported:
        logger.debug(f"🔶 PartiQL not applicable ({unsupported.group()}) → DuckDB scan")
        return None

    if re.search(r'\bORDER\s+BY\b', sql, re.IGNORECASE) and not re.search(r'\bWHERE\b', sql, re.IGNORECASE):
        logger.debug("🔶 PartiQL not applicable (ORDER BY without WHERE) → DuckDB scan")
        return None

    # Extract LIMIT (PartiQL uses API param, not SQL syntax)
    limit_value = None
    limit_match = re.search(r'\bLIMIT\s+(\d+)\s*$', sql, re.IGNORECASE)
    if limit_match:
        limit_value = int(limit_match.group(1))
        sql = sql[:limit_match.start()].strip()

    try:
        from .helpers import _dynamo
        rows = _dynamo.partiql_items(sql, region, limit=limit_value)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)

        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, Decimal)).any():
                df[col] = df[col].astype(float)

        logger.info(f"🔶 PartiQL success: {table_name} ({len(df)} rows)")
        return df

    except Exception as e:
        # Not user-facing: PartiQL couldn't run this query, so we fall back to the
        # scan + DuckDB path (which succeeds and is logged at INFO). DEBUG so the
        # routing detail is available without cluttering the default view.
        logger.debug(f"🔶 PartiQL not applicable for '{table_name}' ({e}) → DuckDB scan")
        return None
