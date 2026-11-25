#!/usr/bin/env python3
"""
Generic SQLite -> Aurora PostgreSQL sync worker (RDS Data API writer).

STANDALONE, LOCAL-DEV-ONLY SERVICE. This is a throwaway bridge, deliberately NOT
part of the proxy: it exists only for the local topology where the proxy writes
to SQLite while Aurora is a read replica. Once the proxy runs against Aurora
directly in the cloud (STORAGE_BACKEND=postgres), this sync is unnecessary and
the entire `sync/` package can be deleted with no impact on the proxy. It is
launched by local_install.sh AFTER the other services are up, and it imports
nothing from `proxy/` so it stays cleanly separable.

Local SQLite is the write source of truth; Aurora is a queryable replica read by
notebooks over the Data API (the v_nb_* views). This worker keeps Aurora in step
with local activity. It runs LOCALLY and reaches Aurora over the RDS Data API
(HTTPS/443) — the only path available from a laptop outside the cluster's VPC —
authenticating as the READ-WRITE user.

Design (state-based upserts + tombstone-based deletes):
  • Upserts are STATE-based: each operational table carries a monotonic watermark
    column (updated_at, or generated_at for workbook_intel). Each cycle selects
    rows whose watermark is strictly greater than the per-table cursor
    (_sync_cursor), upserts them into Aurora (INSERT ... ON CONFLICT DO UPDATE),
    and advances that table's cursor to the max watermark it pushed.
  • Deletes are TOMBSTONE-based: an AFTER DELETE trigger on each ROOT table records
    the deleted row's PK (as JSON) into _sync_deletes(seq, table_name, pk_json).
    Each cycle replays tombstones with seq > _sync_delete_cursor.last_seq (in
    child->parent order) and advances the delete cursor.
  • CASCADE-CHILDREN (any table whose FK has on_delete=CASCADE — here
    notebook_cells, notebook_cell_versions) carry NO delete tombstones. They are
    synced PARENT-SCOPED: whenever a parent row's watermark advances, the worker
    replaces that parent's child rows in Aurora (delete-by-parent + reinsert
    current). Parent deletions are handled by Aurora's own ON DELETE CASCADE when
    the parent tombstone is replayed.
  • The schema (which tables, PKs, FK graph, cascade-children, topo order) is
    DISCOVERED generically at runtime from SQLite PRAGMAs — the worker is not
    hard-coded to the notebook schema.
  • IDLE CYCLES MAKE NO AURORA CALL: the work (rows past the cursors, tombstones
    past the delete cursor) is read from SQLite first; when there is none the
    cycle returns "idle" before any Data API call, so Serverless v2 can auto-pause.
    A full reconcile cycle (identity re-discovery, begin/commit, a row-count drift
    check that only logs) still runs at process start and every
    SYNC_RECONCILE_INTERVAL_SEC (default 3600; 0 = startup only).
  • Each cycle is ONE Aurora transaction (beginTransaction ... commitTransaction).
    The SQLite cursors (_sync_cursor / _sync_delete_cursor) and the _sync_run
    audit row are advanced ONLY AFTER the Aurora commit succeeds, so a failed
    cycle re-does the same work next time (at-least-once, idempotent via upsert).

Bookkeeping tables (SQLite-only, created by sqlite_db.SYNC_SCHEMA):
  _sync_deletes(seq PK, table_name, pk_json, deleted_at)
  _sync_cursor(table_name PK, last_watermark, last_synced_at)
  _sync_delete_cursor(id=1, last_seq)
  _sync_run(run_id PK, started_at, finished_at, status, rows_upserted, rows_deleted, error)

Usage:
  python3 -m sync.sync_to_aurora --once            # one cycle, then exit
  python3 -m sync.sync_to_aurora --interval 60     # loop every 60s
  python3 -m sync.sync_to_aurora --dry-run --once  # read + log, no Aurora writes

Env (sourced from scripts/config.sh by the launcher):
  AURORA_DATA_API_CLUSTER_ARN     resourceArn for rds-data
  AURORA_DATA_API_RW_SECRET_ARN   secretArn of the READ-WRITE user (falls back to
                                  AURORA_DATA_API_SECRET_ARN for dry-run/read-only)
  AURORA_DATA_API_DATABASE        database name (default microvm_db)
  AWS_REGION                      default us-west-2
  SYNC_RECONCILE_INTERVAL_SEC     full reconcile cadence in loop mode (default 3600)
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

logger = logging.getLogger("sync_to_aurora")


def _is_connectivity_error(exc: BaseException) -> bool:
    """True if `exc` (or a cause in its chain) is a transient network/DNS failure
    reaching AWS — a laptop losing wifi/VPN, DNS not resolving, endpoint
    unreachable. These are EXPECTED and self-healing (the next cycle retries), so
    we log them as a single concise WARNING instead of a full boto/urllib3
    traceback. Genuinely unexpected errors still get logger.exception()."""
    try:
        import botocore.exceptions as bce
        connectivity = (
            getattr(bce, "EndpointConnectionError", ()),
            getattr(bce, "ConnectTimeoutError", ()),
            getattr(bce, "ReadTimeoutError", ()),
            getattr(bce, "ConnectionError", ()),
        )
    except Exception:
        connectivity = ()
    import socket
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if connectivity and isinstance(e, connectivity):
            return True
        if isinstance(e, socket.gaierror):  # DNS resolution failure
            return True
        e = e.__cause__ or e.__context__
    return False

# Source SQLite DB path. Self-contained (no import from proxy/) so this service
# stays cleanly separable/deletable. Defaults to the proxy's operational DB at
# <repo_root>/proxy/data/microvm.db (this file lives at <repo_root>/sync/), and
# can be overridden with MICROVM_DB_PATH for tests or non-default layouts.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get(
    "MICROVM_DB_PATH", os.path.join(_REPO_ROOT, "proxy", "data", "microvm.db"))

# ── Data API resume-retry (Serverless v2 scale-to-zero auto-pause), mirrored from
# proxy/platform/datasources/aurora.py so the worker survives a cold cluster. ──
_RESUME_MAX_ATTEMPTS = 6
_RESUME_BACKOFF_BASE = 3.0  # seconds; delay = base * attempt#

# ── Idle early-return + reconcile cadence ──
# A cycle first reads SQLite (local, free) for rows past each table's cursor and
# tombstones past the delete cursor. When there are none it returns BEFORE any
# Data API call, so an idle app makes no Aurora calls and Serverless v2 can
# auto-pause. This cannot cause drift: the early return happens exactly when the
# full cycle would have written nothing to Aurora either.
# A RECONCILE cycle still runs the full path (identity re-discovery, begin/commit,
# a _sync_run row) plus a one-statement row-count drift check, at process start
# and then every SYNC_RECONCILE_INTERVAL_SEC (default 3600 s; 0 = startup only).
# The drift check only logs: it reports tables whose Aurora row count differs
# from SQLite (an out-of-band Aurora change, a reset cluster, rows the poison/
# oversize guard skipped) so an operator can reset that table's _sync_cursor.
# It never rewrites or deletes data, so upsert/delete semantics are unchanged.
_RECONCILE_INTERVAL_SEC = int(os.environ.get("SYNC_RECONCILE_INTERVAL_SEC", "3600") or 0)
# Per-process state: identity tables per (cluster, database), and the monotonic
# time of the last successful reconcile cycle (None = not yet in this process).
_IDENTITY_CACHE: dict[tuple, set] = {}
_PROCESS_STATE: dict[str, Optional[float]] = {"last_reconcile": None}


def _reconcile_due(now: Optional[float] = None) -> bool:
    last = _PROCESS_STATE["last_reconcile"]
    if last is None:
        return True
    if _RECONCILE_INTERVAL_SEC <= 0:
        return False
    return (time.monotonic() if now is None else now) - last >= _RECONCILE_INTERVAL_SEC


# Data API hard limits we chunk around: a single request is capped at ~1MB and a
# batchExecuteStatement takes at most 1000 parameter-sets. We stay well under both.
_MAX_BATCH_ROWS = 250          # parameter-sets per batchExecuteStatement
_MAX_REQUEST_BYTES = 900_000   # ~1MB request cap, with headroom for envelope

# Tables the worker must NEVER treat as operational (bookkeeping / non-synced).
_SKIP_TABLE_PREFIXES = ("_sync_", "sqlite_")

# Exact table names to exclude from the SQLite->Aurora sync. These are transient
# operational state owned by a sibling service, not durable analytics/history, so
# they have no Aurora counterpart and must not be replicated.
#   - pool_vms: the VM Pool Manager's warm-spare ledger. SQLite is its source of
#     truth; rows churn on every grant/backfill. There is no pool_vms table in the
#     Aurora schema, and syncing it would only add churn with no analytical value.
_SKIP_TABLE_NAMES = ("pool_vms",)

# Columns that are BIGINT in Postgres but stored as TEXT/INT in SQLite — must be
# sent to the Data API as longValue, not stringValue, or Postgres rejects them.
# Keyed by (table, column); notebook_id is BIGINT everywhere it appears.
_BIGINT_COLUMNS = {
    ("notebooks", "id"),
    ("notebook_cells", "notebook_id"),
    ("notebook_runs", "notebook_id"),
    ("notebook_versions", "notebook_id"),
    ("vm_sessions", "notebook_id"),
    ("agent_sessions", "notebook_id"),
    ("agent_messages", "notebook_id"),
    ("notebook_env", "notebook_id"),
    ("notebook_dag", "notebook_id"),
    # BIGINT PK columns of tables the delete-by-PK path prunes (metrics cleanup /
    # state-log pruning). Their PK values are stringified during PK-set diffing, so
    # without these hints a DELETE ... WHERE id = :id would send text and Postgres
    # rejects "bigint = text". Same bug class as agent_messages.message_id below.
    ("vm_metrics", "id"),
    ("vm_state_log", "id"),
    # agent_messages.message_id is INTEGER in Postgres and part of the PK. The
    # delete-by-PK path stringifies PK values (PK-set diffing compares string
    # tuples), so without this hint message_id would be sent as text and Postgres
    # rejects "integer = text" on the DELETE ... WHERE message_id = :message_id.
    ("agent_messages", "message_id"),
}

# Columns that are TIMESTAMPTZ in Postgres and ISO-8601 TEXT in SQLite — send with
# a TIMESTAMP typeHint so the Data API parses the string into timestamptz.
# notebook_runs.started_at / finished_at are TEXT on BOTH sides (the v_nb_runs view
# casts them), so they are DELIBERATELY excluded here.
_TIMESTAMP_COLUMNS = {
    ("notebooks", "created_at"), ("notebooks", "updated_at"),
    ("notebook_cells", "updated_at"),
    ("notebook_runs", "created_at"), ("notebook_runs", "updated_at"),
    ("notebook_versions", "created_at"), ("notebook_versions", "updated_at"),
    ("notebook_cell_versions", "updated_at"),
    ("vm_sessions", "launched_at"), ("vm_sessions", "terminated_at"), ("vm_sessions", "updated_at"),
    ("vm_metrics", "timestamp"), ("vm_metrics", "updated_at"),
    ("vm_state_log", "timestamp"), ("vm_state_log", "updated_at"),
    ("agent_sessions", "created_at"), ("agent_sessions", "updated_at"),
    ("agent_messages", "created_at"), ("agent_messages", "updated_at"),
    ("workbook_intel", "generated_at"),
    ("data_source_entities", "last_discovered_at"), ("data_source_entities", "updated_at"),
    ("local_file_entities", "last_discovered_at"), ("local_file_entities", "updated_at"),
    ("session_registry", "updated_at"),
    ("notebook_env", "updated_at"),
    # notebook_dag.built_at is TEXT on BOTH sides, so only updated_at is listed.
    ("notebook_dag", "updated_at"),
}


# ============================================================================
# Task #1 — Aurora Data API writer
# ============================================================================

class AuroraDataApiWriter:
    """Thin RDS Data API write client: transactions + batched, parameterized
    upserts/deletes with size-aware chunking and auto-pause resume retry.

    Identifiers (table/column names) are validated + quoted (never bound — the
    Data API can't bind identifiers). Values are always bound as named parameters.
    """

    def __init__(self, cluster_arn: str, secret_arn: str, database: str,
                 region: str, dry_run: bool = False):
        self.cluster_arn = cluster_arn
        self.secret_arn = secret_arn
        self.database = database
        self.region = region
        self.dry_run = dry_run
        self._client = None if dry_run else boto3.client("rds-data", region_name=region)
        self._txn_id: Optional[str] = None
        # Tables whose PK is a GENERATED ALWAYS AS IDENTITY column. Inserting an
        # explicit value into such a column REQUIRES `OVERRIDING SYSTEM VALUE`
        # (Postgres rejects it otherwise). We preserve source PKs (they're FK join
        # keys), so we must override. Discovered once from Aurora (generic — not
        # hardcoded to a table name). BIGSERIAL columns (nextval default) are NOT
        # identity and accept explicit inserts, so they're excluded.
        self._identity_always: set[str] = set()

    # ---- low-level execute with resume retry ----
    def _call(self, fn_name: str, **kwargs):
        """Invoke an rds-data call, retrying only the auto-pause resume race."""
        fn = getattr(self._client, fn_name)
        last_err = None
        for attempt in range(_RESUME_MAX_ATTEMPTS):
            try:
                return fn(**kwargs)
            except self._client.exceptions.DatabaseResumingException as e:
                last_err = e
                if attempt < _RESUME_MAX_ATTEMPTS - 1:
                    delay = _RESUME_BACKOFF_BASE * (attempt + 1)
                    logger.info(f"Aurora resuming from auto-pause; retry "
                                f"{attempt + 1}/{_RESUME_MAX_ATTEMPTS} in {delay:.0f}s")
                    time.sleep(delay)
            # Any other exception propagates immediately (not a resume race).
        raise last_err

    def _base_kwargs(self) -> dict:
        kw = {"resourceArn": self.cluster_arn, "secretArn": self.secret_arn,
              "database": self.database}
        if self._txn_id:
            kw["transactionId"] = self._txn_id
        return kw

    def discover_identity_tables(self, refresh: bool = False) -> set[str]:
        """Find tables with a GENERATED ALWAYS AS IDENTITY column (need OVERRIDING
        SYSTEM VALUE on explicit-PK inserts). One information_schema query, cached
        ONCE PER PROCESS (keyed by cluster + database) so the loop, which builds a
        fresh writer every cycle, does not re-query Aurora each time. `refresh=True`
        (the reconcile cycle) re-reads it so a schema change is picked up."""
        if self.dry_run:
            return self._identity_always
        key = (self.cluster_arn, self.database)
        cached = _IDENTITY_CACHE.get(key)
        if cached is not None and not refresh:
            self._identity_always = set(cached)
            return self._identity_always
        resp = self._call(
            "execute_statement",
            resourceArn=self.cluster_arn, secretArn=self.secret_arn, database=self.database,
            sql=("SELECT table_name FROM information_schema.columns "
                 "WHERE table_schema='public' AND is_identity='YES' "
                 "AND identity_generation='ALWAYS'"))
        self._identity_always = {
            rec[0].get("stringValue") for rec in resp.get("records", []) if rec}
        _IDENTITY_CACHE[key] = set(self._identity_always)
        return self._identity_always

    def count_rows(self, tables: list[str]) -> dict[str, int]:
        """Row count per table in Aurora, in ONE statement (UNION ALL). Used only by
        the reconcile cycle's drift check; never called on an idle cycle."""
        if self.dry_run or not tables:
            return {}
        for t in tables:
            _validate_ident(t)
        sql = " UNION ALL ".join(
            f"SELECT '{t}' AS t, count(*) AS n FROM \"{t}\"" for t in tables)
        resp = self._call("execute_statement", resourceArn=self.cluster_arn,
                          secretArn=self.secret_arn, database=self.database, sql=sql)
        out: dict[str, int] = {}
        for rec in resp.get("records", []) or []:
            if len(rec) >= 2:
                out[rec[0].get("stringValue")] = int(rec[1].get("longValue", 0))
        return out

    # ---- transaction lifecycle ----
    def begin(self) -> None:
        if self.dry_run:
            logger.info("[dry-run] begin transaction")
            return
        resp = self._call("begin_transaction", resourceArn=self.cluster_arn,
                           secretArn=self.secret_arn, database=self.database)
        self._txn_id = resp["transactionId"]

    def commit(self) -> None:
        if self.dry_run:
            logger.info("[dry-run] commit transaction")
            return
        if not self._txn_id:
            return
        self._call("commit_transaction", resourceArn=self.cluster_arn,
                   secretArn=self.secret_arn, transactionId=self._txn_id)
        self._txn_id = None

    def rollback(self) -> None:
        if self.dry_run:
            self._txn_id = None
            logger.info("[dry-run] rollback transaction")
            return
        if not self._txn_id:
            return
        try:
            self._call("rollback_transaction", resourceArn=self.cluster_arn,
                       secretArn=self.secret_arn, transactionId=self._txn_id)
        except Exception as e:
            logger.warning(f"rollback failed (txn may already be gone): {e}")
        finally:
            self._txn_id = None

    # ---- statement execution ----
    def _execute(self, sql: str, parameter_sets: list[list[dict]]) -> None:
        """Run one statement for one-or-many parameter sets. Uses
        batchExecuteStatement for multiple sets, executeStatement for a single set
        (batch requires >=1 but we keep single-row writes on the simpler call)."""
        if self.dry_run:
            logger.info(f"[dry-run] SQL x{len(parameter_sets)}: {sql[:120]}")
            return
        kw = self._base_kwargs()
        kw["sql"] = sql
        if len(parameter_sets) == 1:
            kw["parameters"] = parameter_sets[0]
            self._call("execute_statement", **kw)
        else:
            kw["parameterSets"] = parameter_sets
            self._call("batch_execute_statement", **kw)

    def execute_raw(self, sql: str, parameters: Optional[list[dict]] = None):
        """Run a single ad-hoc statement (used for parent-scoped child deletes)."""
        if self.dry_run:
            logger.info(f"[dry-run] SQL: {sql[:160]}")
            return None
        kw = self._base_kwargs()
        kw["sql"] = sql
        if parameters:
            kw["parameters"] = parameters
        return self._call("execute_statement", **kw)

    # ---- upsert ----
    def batch_upsert(self, table: str, columns: list[str], pk_cols: list[str],
                     rows: list[dict]) -> int:
        """INSERT ... ON CONFLICT (pk) DO UPDATE, batched + size-chunked.
        `rows` is a list of {column: python_value}. Returns rows attempted.

        POISON-ROW GUARD: the RDS Data API executes a whole batch as ONE statement,
        so a single row with a BIGINT column holding non-integer text makes Aurora
        reject the ENTIRE batch (SQLState 42804) and fails the sync cycle. We drop
        such unsyncable rows up front (logged) so one bad row can't wedge the rest.
        Real data never hits this (ids are numeric); it guards against bad/legacy
        or test-injected values."""
        if not rows:
            return 0
        _clean_rows = []
        _skipped = 0
        for r in rows:
            reason = _row_unsyncable_reason(table, columns, r)
            if reason is None:
                _clean_rows.append(r)
            else:
                _skipped += 1
                logger.warning("sync: skipping unsyncable %s row (%s) — fix the source data", table, reason)
        if _skipped:
            logger.warning("sync: %s — skipped %d unsyncable row(s) this batch (kept %d)",
                           table, _skipped, len(_clean_rows))
        rows = _clean_rows
        if not rows:
            return 0
        _validate_ident(table)
        for c in columns:
            _validate_ident(c)
        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(f":{c}" for c in columns)
        conflict = ", ".join(f'"{c}"' for c in pk_cols)
        # A GENERATED ALWAYS identity PK rejects explicit values without this.
        overriding = "OVERRIDING SYSTEM VALUE " if table in self._identity_always else ""
        # Update every non-PK column on conflict.
        non_pk = [c for c in columns if c not in pk_cols]
        if non_pk:
            set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in non_pk)
            do = f"DO UPDATE SET {set_clause}"
        else:
            do = "DO NOTHING"
        sql = (f'INSERT INTO "{table}" ({col_list}) {overriding}VALUES ({placeholders}) '
               f'ON CONFLICT ({conflict}) {do}')

        total = 0
        for chunk in _chunk_rows(table, columns, rows):
            param_sets = [_row_to_params(table, columns, r) for r in chunk]
            self._execute(sql, param_sets)
            total += len(chunk)
        return total

    # ---- delete by PKs ----
    def delete_by_pks(self, table: str, pk_dicts: list[dict]) -> int:
        """DELETE rows matching a list of PK dicts ({col: val}). Batched by the
        SAME pk-shape (all rows here share one table => same key columns)."""
        if not pk_dicts:
            return 0
        _validate_ident(table)
        pk_cols = list(pk_dicts[0].keys())
        for c in pk_cols:
            _validate_ident(c)
        where = " AND ".join(f'"{c}" = :{c}' for c in pk_cols)
        sql = f'DELETE FROM "{table}" WHERE {where}'
        total = 0
        for chunk in _chunk_rows(table, pk_cols, pk_dicts):
            param_sets = [_row_to_params(table, pk_cols, r) for r in chunk]
            self._execute(sql, param_sets)
            total += len(chunk)
        return total


# ---- Data API value/param helpers ----

def _validate_ident(name: str) -> None:
    """Guard against SQL injection via identifiers (which can't be bound). Table
    and column names must be simple identifiers."""
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")


def _normalize_ts(value: Any) -> Optional[str]:
    """Normalize a SQLite timestamp into a form the Data API TIMESTAMP typeHint
    accepts. SQLite holds a MIX: ISO-8601 with 'T' (our updated_at watermark
    default, strftime) AND the legacy space-separated 'YYYY-MM-DD HH:MM:SS'
    (datetime('now') defaults on created_at etc.). The Data API's TIMESTAMP parser
    wants 'YYYY-MM-DD HH:MM:SS[.fff]' WITHOUT a 'T' and WITHOUT a timezone suffix,
    so we convert ISO-T (and any trailing tz like '+00:00' or 'Z') to that form."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Drop timezone designator: trailing 'Z', or a +HH:MM / -HH:MM offset (but not
    # the date's own hyphens — only a tz offset in the time portion).
    if s.endswith("Z"):
        s = s[:-1]
    # A tz offset appears after the time (position > 10). Find a +/- after 'T'/space.
    tsep = max(s.find("T"), s.find(" "))
    if tsep != -1:
        tzpos = -1
        for i in range(tsep + 1, len(s)):
            if s[i] in "+-":
                tzpos = i
                break
        if tzpos != -1:
            s = s[:tzpos]
    # Normalize the date/time separator 'T' -> space.
    s = s.replace("T", " ")
    return s


def _field(table: str, col: str, value: Any) -> dict:
    """Wrap one Python value into a Data API named-parameter dict."""
    if value is None:
        return {"name": col, "value": {"isNull": True}}
    key = (table, col)
    if key in _BIGINT_COLUMNS:
        # Empty/blank string on a (nullable) BIGINT column ⇒ SQL NULL, not an error.
        # Sources sometimes persist "" to mean "no value" (e.g. a chat turn with no
        # notebook_id). Coercing "" to int fails, and sending it as text made Aurora
        # reject the ENTIRE batch (SQLState 42804), wedging every table's sync behind
        # one poison row. Map blank → NULL so the row is valid and the cycle proceeds.
        if isinstance(value, str) and value.strip() == "":
            return {"name": col, "value": {"isNull": True}}
        # BIGINT column: coerce to int. If the SQLite value isn't integer-like
        # (e.g. bad/legacy data), DON'T raise — a single malformed row must not
        # crash the whole sync cycle. Log once and send the raw string so the
        # Data API rejects just that row (surfaced as a per-row error) instead of
        # wedging every table's sync behind an unhandled ValueError.
        try:
            return {"name": col, "value": {"longValue": int(value)}}
        except (TypeError, ValueError):
            logger.warning("sync: %s.%s expected integer but got %r — sending as text "
                           "(row will be rejected by Aurora; fix the source data)", table, col, value)
            return {"name": col, "value": {"stringValue": str(value)}}
    if key in _TIMESTAMP_COLUMNS:
        norm = _normalize_ts(value)
        if norm is None:
            return {"name": col, "value": {"isNull": True}}
        return {"name": col, "value": {"stringValue": norm}, "typeHint": "TIMESTAMP"}
    if isinstance(value, bool):
        return {"name": col, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": col, "value": {"longValue": value}}
    if isinstance(value, float):
        return {"name": col, "value": {"doubleValue": value}}
    return {"name": col, "value": {"stringValue": str(value)}}


def _row_to_params(table: str, columns: list[str], row: dict) -> list[dict]:
    return [_field(table, c, row.get(c)) for c in columns]


def _row_unsyncable_reason(table: str, columns: list[str], row: dict):
    """Return a human-readable reason this row CANNOT be synced (so the caller can
    skip it), or None if it's fine. A BIGINT column holding a non-blank value that
    can't be coerced to int is the poison case: the RDS Data API runs a whole
    batch_upsert as ONE execute_statement, so including such a value aborts the
    ENTIRE batch (SQLState 42804) — wedging every other row behind it. Dropping the
    one bad row (and letting its watermark NOT advance) is strictly safer than
    stalling the cycle. Blank strings on a BIGINT are fine (they map to SQL NULL).

    OVERSIZE GUARD (generic, any table): a single row whose estimated parameter-set
    size exceeds _MAX_REQUEST_BYTES can never fit in one Data API request (~1MB),
    so it would be rejected along with its whole batch. Such a row is skipped —
    NEVER truncated. Because the table cursor still advances past it, it is only
    re-evaluated when the row is next updated (e.g. a later, smaller notebook_dag
    rebuild bumps updated_at); until then Aurora keeps its previous version."""
    size = _estimate_row_bytes(table, columns, row)
    if size > _MAX_REQUEST_BYTES:
        return (f"row is ~{size} bytes, over the {_MAX_REQUEST_BYTES}-byte Data API "
                f"request limit (skipped, not truncated)")
    for c in columns:
        if (table, c) not in _BIGINT_COLUMNS:
            continue
        v = row.get(c)
        if v is None:
            continue
        if isinstance(v, bool):  # bool is an int subclass but not a valid id
            return f"{c}={v!r} (bool, not a valid BIGINT)"
        if isinstance(v, int):
            continue
        if isinstance(v, str) and v.strip() == "":
            continue  # blank → NULL, handled by _field
        try:
            int(v)
        except (TypeError, ValueError):
            return f"{c}={v!r} is not integer-coercible for a BIGINT column"
    return None


def _estimate_row_bytes(table: str, columns: list[str], row: dict) -> int:
    """Rough serialized size of one parameter set (for the ~1MB request cap)."""
    n = 0
    for c in columns:
        v = row.get(c)
        if v is None:
            n += len(c) + 16
        else:
            n += len(c) + len(str(v)) + 24
    return n


def _chunk_rows(table: str, columns: list[str], rows: list[dict]):
    """Yield row chunks bounded by BOTH the 250-set count and the ~1MB size cap."""
    chunk: list[dict] = []
    size = 0
    for r in rows:
        rb = _estimate_row_bytes(table, columns, r)
        if chunk and (len(chunk) >= _MAX_BATCH_ROWS or size + rb > _MAX_REQUEST_BYTES):
            yield chunk
            chunk, size = [], 0
        chunk.append(r)
        size += rb
    if chunk:
        yield chunk


# ============================================================================
# Task #2 — generic SQLite schema introspection + FK topo-sort
# ============================================================================

class TableInfo:
    def __init__(self, name: str):
        self.name = name
        self.columns: list[str] = []
        self.pk_cols: list[str] = []
        self.parents: list[str] = []       # tables this one references
        self.is_cascade_child = False      # has an FK with on_delete=CASCADE
        self.watermark = "updated_at"      # overridden for workbook_intel

    def __repr__(self):
        return (f"<TableInfo {self.name} pk={self.pk_cols} parents={self.parents} "
                f"cascade_child={self.is_cascade_child} wm={self.watermark}>")


def introspect(conn: sqlite3.Connection) -> dict[str, TableInfo]:
    """Discover operational tables, their columns, PKs, FK parents, cascade-child
    flag, and watermark column — entirely from SQLite PRAGMAs (generic)."""
    tables: dict[str, TableInfo] = {}
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for name in names:
        if any(name.startswith(p) for p in _SKIP_TABLE_PREFIXES):
            continue
        if name in _SKIP_TABLE_NAMES:
            continue
        ti = TableInfo(name)
        # Columns + PK (pk>0 marks key columns; composite PKs use pk=1,2,...).
        cols = list(conn.execute(f"PRAGMA table_info({name})"))
        ti.columns = [c["name"] for c in cols]
        pk_sorted = sorted([c for c in cols if c["pk"]], key=lambda c: c["pk"])
        ti.pk_cols = [c["name"] for c in pk_sorted]
        # FKs: parents + cascade-child detection.
        for fk in conn.execute(f"PRAGMA foreign_key_list({name})"):
            ti.parents.append(fk["table"])
            if (fk["on_delete"] or "").upper() == "CASCADE":
                ti.is_cascade_child = True
        # Watermark column: updated_at everywhere, generated_at for workbook_intel.
        if "updated_at" in ti.columns:
            ti.watermark = "updated_at"
        elif "generated_at" in ti.columns:
            ti.watermark = "generated_at"
        else:
            ti.watermark = None  # no watermark => not state-synced (skipped)
        tables[name] = ti
    return tables


def topo_order(tables: dict[str, TableInfo]) -> list[str]:
    """Parents-before-children order (Kahn). Only edges among known operational
    tables count; references to non-synced tables are ignored."""
    known = set(tables)
    indeg = {t: 0 for t in tables}
    children: dict[str, list[str]] = {t: [] for t in tables}
    for t, ti in tables.items():
        for p in ti.parents:
            if p in known and p != t:
                indeg[t] += 1
                children[p].append(t)
    queue = sorted([t for t, d in indeg.items() if d == 0])
    order: list[str] = []
    while queue:
        t = queue.pop(0)
        order.append(t)
        for ch in sorted(children[t]):
            indeg[ch] -= 1
            if indeg[ch] == 0:
                queue.append(ch)
    if len(order) != len(tables):
        # Cycle (shouldn't happen with this schema) — fall back to name order so
        # the worker still runs deterministically.
        logger.warning("FK graph has a cycle; falling back to name order")
        return sorted(tables)
    return order


# ============================================================================
# Task #3 — the sync engine (one cycle = one Aurora transaction)
# ============================================================================

def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_upsert_cursor(conn, table: str) -> Optional[str]:
    row = conn.execute("SELECT last_watermark FROM _sync_cursor WHERE table_name=?",
                       (table,)).fetchone()
    return row["last_watermark"] if row else None


def _rows_since(conn, ti: TableInfo, cursor: Optional[str]) -> list[dict]:
    """Rows whose watermark is strictly greater than the cursor (all rows if the
    cursor is unset), ordered by watermark so the max is deterministic."""
    wm = ti.watermark
    if cursor is None:
        sql = f'SELECT * FROM "{ti.name}" ORDER BY "{wm}" ASC'
        params: tuple = ()
    else:
        sql = f'SELECT * FROM "{ti.name}" WHERE "{wm}" > ? ORDER BY "{wm}" ASC'
        params = (cursor,)
    return [dict(r) for r in conn.execute(sql, params)]


def _child_tables_of(parent: str, tables: dict[str, TableInfo]) -> list[TableInfo]:
    """Cascade-child tables that reference `parent` directly."""
    return [ti for ti in tables.values()
            if ti.is_cascade_child and parent in ti.parents]


def _resp_pk_tuples(resp, pk_cols: list[str]) -> set:
    """Unwrap a Data API SELECT response (rows of the PK columns, in order) into a
    set of string-normalized PK tuples for set-difference against SQLite PKs. A
    dry-run writer returns None from execute_raw, so treat that as 'no rows'."""
    if not resp:
        return set()
    out = set()
    for rec in resp.get("records", []) or []:
        vals = []
        for f in rec:
            if f.get("isNull"):
                vals.append("None")
                continue
            v = None
            for k in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if k in f:
                    v = f[k]
                    break
            vals.append(str(v))
        out.add(tuple(vals))
    return out


def _pk_exists(conn, table: str, pk: dict) -> bool:
    """True if a row with this PK still exists in the SQLite source (i.e. the
    tombstoned key was later re-created, so its delete must NOT be replayed)."""
    if not pk:
        return False
    where = " AND ".join(f'"{c}" = ?' for c in pk.keys())
    row = conn.execute(
        f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1', tuple(pk.values())).fetchone()
    return row is not None


class SyncEngine:
    def __init__(self, writer: AuroraDataApiWriter):
        self.writer = writer

    def run_cycle(self, reconcile: Optional[bool] = None) -> dict:
        """Execute one sync cycle in a single Aurora transaction. Returns a
        summary dict {status, rows_upserted, rows_deleted, error}.

        status "idle": SQLite has nothing past the cursors and no reconcile is due,
        so the cycle returned before any Aurora call (see _RECONCILE_INTERVAL_SEC).
        `reconcile` forces (True) or suppresses (False) the reconcile path; None
        means "when due" (process start, then every interval)."""
        conn = _sqlite_conn()
        started = _now_iso()
        rows_up = 0
        rows_del = 0
        # New per-table watermark maxima to persist AFTER commit.
        new_cursors: dict[str, str] = {}
        new_delete_seq: Optional[int] = None
        try:
            tables = introspect(conn)
            order = topo_order(tables)

            # Read the work from SQLite FIRST (local, no AWS): rows past each
            # table's cursor and tombstones past the delete cursor. Same queries
            # the passes below used to run inline, so the selected work is the same.
            pending: dict[str, list[dict]] = {}
            for t in order:
                ti = tables[t]
                if not ti.watermark:
                    continue
                rows = _rows_since(conn, ti, _get_upsert_cursor(conn, t))
                if rows:
                    pending[t] = rows
            delete_cursor = conn.execute(
                "SELECT last_seq FROM _sync_delete_cursor WHERE id=1").fetchone()["last_seq"]
            tombstones = list(conn.execute(
                "SELECT seq, table_name, pk_json FROM _sync_deletes WHERE seq > ? ORDER BY seq",
                (delete_cursor,)))

            do_reconcile = _reconcile_due() if reconcile is None else reconcile
            if not pending and not tombstones and not do_reconcile:
                # Nothing changed: no Data API call at all, no _sync_run row.
                logger.debug("sync idle: nothing to sync (no Aurora calls)")
                return {"status": "idle", "rows_upserted": 0,
                        "rows_deleted": 0, "error": None}

            # Identity tables (cached per process; re-read on a reconcile cycle) so
            # explicit-PK inserts use OVERRIDING SYSTEM VALUE where required.
            self.writer.discover_identity_tables(refresh=do_reconcile)

            self.writer.begin()

            # ---- 1) OWN-WATERMARK UPSERTS for EVERY table, in parent->child order ----
            # Every table — including cascade-children — is upserted on its OWN
            # watermark (rows whose updated_at/generated_at exceeds its cursor).
            # Topo order guarantees a parent is upserted before its children, so
            # FK constraints hold. This is what syncs INDEPENDENTLY-created child
            # rows (e.g. a notebook_version created by the auto/pre_ai/manual path,
            # or its notebook_cell_versions) that do NOT bump the parent notebook's
            # watermark — the case the earlier parent-scoped-only design missed.
            #
            # `advanced_parents` records, per table, the PKs whose rows advanced this
            # cycle — used by the parent-scoped REMOVAL reconcile (pass 2) to detect
            # child rows deleted without a tombstone (the notebook_cells wholesale
            # delete+reinsert on save).
            advanced_parents: dict[str, list] = {}
            for t in order:
                ti = tables[t]
                rows = pending.get(t)
                if not rows:
                    continue
                # FK SAFETY: a cascade-child can advance in a cycle where its parent
                # does NOT (the child's updated_at moved past the child cursor, but
                # the parent's did not move past the parent cursor — e.g. a version
                # snapshot whose header row was already synced-and-cursor-advanced in
                # an earlier cycle, while its cell_versions land later or get
                # rewritten on restore). Topo order alone can't cover that: it only
                # guarantees the parent is upserted BEFORE the child WHEN the parent
                # was also selected this cycle. If it wasn't, the child INSERT hits a
                # missing parent and Postgres rejects the whole batch (SQLState 23503,
                # e.g. notebook_cell_versions_version_group_id_fkey). Before upserting
                # the child rows, backfill the exact parent rows they reference from
                # SQLite — idempotent (ON CONFLICT DO UPDATE) and only a handful of
                # rows — so the FK target always exists in the same transaction.
                if ti.is_cascade_child:
                    rows_up += self._backfill_parents(conn, ti, tables, rows)
                n = self.writer.batch_upsert(t, ti.columns, ti.pk_cols, rows)
                rows_up += n
                new_cursors[t] = max(r[ti.watermark] for r in rows)
                advanced_parents[t] = [tuple(r[pk] for pk in ti.pk_cols) for r in rows]

            # ---- 2) PARENT-SCOPED REMOVAL reconcile (cascade-children only) ----
            # Own-watermark upserts (pass 1) sync inserts + updates but CANNOT see a
            # child row that was DELETED in SQLite without a tombstone — the
            # notebook_cells case, where a save deletes+reinserts ALL of a notebook's
            # cells (removed cells simply don't come back, and cascade-children carry
            # no delete triggers). For each cascade-child whose PARENT advanced this
            # cycle, delete the Aurora child rows for that parent that NO LONGER exist
            # in SQLite. This is a targeted DELETE (not the old delete-all+reinsert),
            # so it doesn't fight pass 1 and adds no churn for append-only children
            # (notebook_versions / notebook_cell_versions have nothing to prune).
            for t in order:
                ti = tables[t]
                if not ti.is_cascade_child or not ti.watermark:
                    continue
                parent = _single_parent(ti, tables)
                if not parent:
                    continue
                link_col = _child_link_column(conn, ti, parent)
                parent_pks = advanced_parents.get(parent.name, [])
                if not parent_pks:
                    continue
                for pk_tuple in parent_pks:
                    pval = pk_tuple[0]
                    # Current child PKs in SQLite for this parent.
                    live = {tuple(str(r[c]) for c in ti.pk_cols) for r in conn.execute(
                        f'SELECT * FROM "{ti.name}" WHERE "{link_col}" = ?', (pval,))}
                    # Child PKs Aurora currently holds for this parent.
                    pkcols = ", ".join(f'"{c}"' for c in ti.pk_cols)
                    pfield = _field(ti.name, link_col, pval)
                    pfield["name"] = "p"
                    resp = self.writer.execute_raw(
                        f'SELECT {pkcols} FROM "{ti.name}" WHERE "{link_col}" = :p',
                        [pfield])
                    aurora_pks = _resp_pk_tuples(resp, ti.pk_cols)
                    # Delete from Aurora any child PK that is gone from SQLite.
                    stale = [dict(zip(ti.pk_cols, pk)) for pk in aurora_pks if pk not in live]
                    if stale:
                        rows_del += self.writer.delete_by_pks(ti.name, stale)
                # No cursor bump here — pass 1 already advanced this table's cursor
                # from its own-watermark upserts.

            # ---- 3) DELETE replay (child->parent order), skipping cascade-children ----
            # (tombstones were read up front, before any Aurora call)
            if tombstones:
                new_delete_seq = max(t["seq"] for t in tombstones)
                # Group by table, then apply in child->parent (reverse topo) order.
                # SKIP any tombstone whose PK is STILL LIVE in SQLite: that row was
                # deleted and then RE-CREATED (e.g. a session_registry entry removed
                # on VM death and re-added on relaunch). Its fresh state was already
                # upserted above; replaying the stale delete would wrongly remove it.
                by_table: dict[str, list[dict]] = {}
                for tomb in tombstones:
                    t_name = tomb["table_name"]
                    pk = json.loads(tomb["pk_json"])
                    if _pk_exists(conn, t_name, pk):
                        continue  # recreated since the delete — keep the live row
                    by_table.setdefault(t_name, []).append(pk)
                for t in reversed(order):
                    if t not in by_table:
                        continue
                    ti = tables.get(t)
                    # Cascade-children are removed by Aurora's ON DELETE CASCADE when
                    # their parent is deleted — and they carry no tombstones anyway.
                    if ti and ti.is_cascade_child:
                        continue
                    rows_del += self.writer.delete_by_pks(t, by_table[t])
                # Any tombstoned table not in the topo set (defensive) — apply last.
                for t, pks in by_table.items():
                    if t not in order:
                        rows_del += self.writer.delete_by_pks(t, pks)

            # ---- 4) COMMIT, then advance SQLite cursors + audit ----
            self.writer.commit()

            # A dry-run performs NO Aurora writes, so it must NOT advance the
            # cursors or record a real _sync_run — doing so would falsely mark data
            # as synced and cause the next REAL run to skip it. Report what it WOULD
            # have done and leave all bookkeeping untouched.
            if self.writer.dry_run:
                logger.info(f"[dry-run] would upsert={rows_up} delete={rows_del} "
                            f"(cursors NOT advanced)")
                return {"status": "dry-run", "rows_upserted": rows_up,
                        "rows_deleted": rows_del, "error": None}

            self._advance_cursors(conn, new_cursors, new_delete_seq)
            self._record_run(conn, started, "ok", rows_up, rows_del, None)
            conn.commit()
            if do_reconcile:
                first = _PROCESS_STATE["last_reconcile"] is None
                _PROCESS_STATE["last_reconcile"] = time.monotonic()
                self._log_drift(conn, tables, order)
                logger.info(f"sync ok ({'startup' if first else 'periodic'} reconcile): "
                            f"upserted={rows_up} deleted={rows_del}")
            elif rows_up or rows_del:
                logger.info(f"sync ok: upserted={rows_up} deleted={rows_del}")
            else:
                # Work was selected but nothing was written (e.g. every row skipped
                # by the poison/oversize guard, or tombstones for re-created rows).
                logger.debug(f"sync ok: upserted={rows_up} deleted={rows_del}")
            return {"status": "ok", "rows_upserted": rows_up,
                    "rows_deleted": rows_del, "error": None}

        except Exception as e:
            # A network/DNS blip reaching AWS is expected + self-healing (next cycle
            # retries), so log it as ONE concise line — not a multi-line traceback.
            # Everything else is genuinely unexpected and gets the full traceback.
            if _is_connectivity_error(e):
                logger.warning("sync skipped: network unreachable, will retry")
            else:
                logger.exception("sync cycle failed")
            self.writer.rollback()
            # Do NOT advance cursors — next cycle retries the same work. Record the
            # failure for observability (skip in dry-run: it must not touch state).
            if not self.writer.dry_run:
                try:
                    self._record_run(conn, started, "error", rows_up, rows_del, str(e))
                    conn.commit()
                except Exception:
                    pass
            return {"status": "error", "rows_upserted": rows_up,
                    "rows_deleted": rows_del, "error": str(e)}
        finally:
            conn.close()

    def _backfill_parents(self, conn, child: TableInfo,
                          tables: dict[str, TableInfo], child_rows: list[dict]) -> int:
        """Ensure the operational parent rows referenced by `child_rows` exist in
        Aurora (idempotent upsert), so a child INSERT can never hit a missing FK
        target — even when the parent's own watermark didn't advance this cycle.

        Walks UP the FK chain (parent, grandparent, ...) so a child whose parent is
        itself a child of a not-yet-synced grandparent is fully covered. Only
        operational (introspected) parents are touched; references to non-synced
        tables are ignored. Returns the number of parent rows upserted."""
        upserted = 0
        # (child_table, link_column, parent_table) FK edges to resolve, seeded from
        # the rows we're about to write; we push grandparents as we discover them.
        # `_visited` guards against pathological cycles (our schema has none).
        pending: list[tuple[TableInfo, list[dict]]] = [(child, child_rows)]
        visited: set[str] = set()
        while pending:
            ctab, crows = pending.pop()
            for fk in conn.execute(f"PRAGMA foreign_key_list({ctab.name})"):
                parent = tables.get(fk["table"])
                if parent is None or parent.name == ctab.name:
                    continue  # non-synced or self ref
                link_col = fk["from"]          # child column holding the parent PK
                parent_key = fk["to"] or (parent.pk_cols[0] if parent.pk_cols else None)
                if not parent_key:
                    continue
                # Distinct, non-null parent-key values referenced by these child rows.
                key_vals = {r[link_col] for r in crows
                            if link_col in r and r[link_col] is not None}
                if not key_vals:
                    continue
                # Fetch those parent rows from SQLite and upsert them into Aurora.
                # Chunk the IN-list so a large fan-in can't blow the SQL length.
                fetched: list[dict] = []
                vals = list(key_vals)
                for i in range(0, len(vals), 500):
                    slice_vals = vals[i:i + 500]
                    ph = ", ".join("?" for _ in slice_vals)
                    fetched.extend(dict(r) for r in conn.execute(
                        f'SELECT * FROM "{parent.name}" '
                        f'WHERE "{parent_key}" IN ({ph})', slice_vals))
                if not fetched:
                    continue
                upserted += self.writer.batch_upsert(
                    parent.name, parent.columns, parent.pk_cols, fetched)
                # Recurse UP: this parent may itself reference a grandparent that
                # also hasn't synced yet. Guard against re-walking the same table.
                if parent.name not in visited:
                    visited.add(parent.name)
                    pending.append((parent, fetched))
        return upserted

    def _log_drift(self, conn, tables: dict[str, TableInfo], order: list[str]) -> None:
        """Reconcile-only drift check: compare per-table row counts (one Aurora
        statement) and log tables that differ. Best effort and log-only: it never
        changes data or cursors, and a failure here never fails the cycle."""
        synced = [t for t in order if tables[t].watermark]
        try:
            aurora = self.writer.count_rows(synced)
        except Exception as e:
            logger.warning(f"sync reconcile: drift check skipped ({e})")
            return
        if not aurora:
            return
        drift = []
        for t in synced:
            local = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            remote = aurora.get(t)
            if remote is not None and remote != local:
                drift.append(f"{t} sqlite={local} aurora={remote}")
        if drift:
            logger.warning("sync reconcile: row-count drift (SQLite is the source of "
                           "truth; delete a table's _sync_cursor row to re-push it): "
                           + "; ".join(drift))

    def _advance_cursors(self, conn, new_cursors: dict[str, str],
                         new_delete_seq: Optional[int]) -> None:
        now = _now_iso()
        for table, wm in new_cursors.items():
            conn.execute(
                """INSERT INTO _sync_cursor (table_name, last_watermark, last_synced_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(table_name) DO UPDATE SET
                       last_watermark = excluded.last_watermark,
                       last_synced_at = excluded.last_synced_at""",
                (table, wm, now))
        if new_delete_seq is not None:
            conn.execute("UPDATE _sync_delete_cursor SET last_seq = ? WHERE id = 1",
                         (new_delete_seq,))

    def _record_run(self, conn, started: str, status: str, up: int, deleted: int,
                    err: Optional[str]) -> None:
        conn.execute(
            """INSERT INTO _sync_run (started_at, finished_at, status, rows_upserted, rows_deleted, error)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (started, _now_iso(), status, up, deleted, err))


def _single_parent(ti: TableInfo, tables: dict[str, TableInfo]) -> Optional[TableInfo]:
    """The one operational parent of a cascade-child (our cascade-children each
    have exactly one cascade parent)."""
    for p in ti.parents:
        if p in tables:
            return tables[p]
    return None


def _child_link_column(conn, child: TableInfo, parent: TableInfo) -> str:
    """The child column that FK-references the parent's PK (the 'from' side of the
    matching foreign_key_list row)."""
    for fk in conn.execute(f"PRAGMA foreign_key_list({child.name})"):
        if fk["table"] == parent.name:
            return fk["from"]
    # Fallback: assume a same-named column.
    return parent.pk_cols[0]


# ============================================================================
# Task #4 — CLI: run_once / run_forever
# ============================================================================

def _build_writer(dry_run: bool) -> AuroraDataApiWriter:
    region = os.environ.get("AWS_REGION", "us-west-2")
    cluster = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
    # Prefer the READ-WRITE secret; fall back to the RO secret only for dry-run
    # (read-only exploration) — a real write with the RO secret would fail.
    secret = os.environ.get("AURORA_DATA_API_RW_SECRET_ARN", "")
    if not secret:
        if dry_run:
            secret = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
            logger.warning("AURORA_DATA_API_RW_SECRET_ARN unset; using RO secret for dry-run")
        else:
            raise SystemExit(
                "AURORA_DATA_API_RW_SECRET_ARN is not set — the sync worker writes "
                "base tables and needs the READ-WRITE Data API secret. Set it in "
                "scripts/config.sh (ARN of microvm/proxy-db/readwrite).")
    database = os.environ.get("AURORA_DATA_API_DATABASE",
                              os.environ.get("AURORA_DB_NAME", "microvm_db"))
    if not cluster:
        raise SystemExit("AURORA_DATA_API_CLUSTER_ARN is not set — cannot reach Aurora.")
    return AuroraDataApiWriter(cluster, secret, database, region, dry_run=dry_run)


def run_once(dry_run: bool = False, reconcile: Optional[bool] = None) -> dict:
    """One cycle. reconcile=None: reconcile when due (the loop); True: always
    (the --once CLI); see _RECONCILE_INTERVAL_SEC. Status "idle" = no Aurora call."""
    writer = _build_writer(dry_run)
    return SyncEngine(writer).run_cycle(reconcile=reconcile)


def run_forever(interval_sec: int, dry_run: bool = False) -> None:
    logger.info(f"sync worker started (interval={interval_sec}s, dry_run={dry_run})")
    while True:
        start = time.time()
        try:
            run_once(dry_run=dry_run)
        except SystemExit:
            raise
        except Exception:
            # A cycle should never crash the loop — run_cycle already records the
            # error; this is a last-resort guard around setup failures.
            logger.exception("sync cycle raised outside run_cycle; continuing")
        elapsed = time.time() - start
        time.sleep(max(1.0, interval_sec - elapsed))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SQLite -> Aurora Postgres sync worker (RDS Data API)")
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between sync cycles in loop mode (default 60)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit (default is to loop)")
    parser.add_argument("--dry-run", action="store_true",
                        help="read SQLite + log intended writes, but do not write Aurora")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        result = run_once(dry_run=args.dry_run, reconcile=True)
        return 0 if result["status"] in ("ok", "idle", "dry-run") else 1
    run_forever(args.interval, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
