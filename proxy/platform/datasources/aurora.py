"""
Aurora PostgreSQL schema provider — discovers tables + columns over the RDS
Data API (HTTPS/443), so the proxy can derive Aurora schemas itself and push
them fully-formed to the VM (the VM never touches Aurora for discovery).

Auth is the READ-ONLY secret (SELECT-only), the same credential MicroVMs use.
Everything is gated on the Aurora Data API being configured (AURORA_DATA_API_*
env, sourced from config.sh); when unset, discover()/get_schema() no-op so
non-Aurora deployments behave exactly as before.
"""

import os
import time
import logging
from typing import Optional


from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300  # 5 minutes
# How long get_preview's allowlist may reuse this instance's last discover()
# result instead of re-enumerating (see _known_source_ids).
KNOWN_IDS_TTL_SEC = 60

# Retry policy for the Serverless v2 scale-to-zero auto-pause resume race. The
# first Data API call after an idle period raises DatabaseResumingException while
# the cluster wakes (~5-30s). Retry with linear backoff: 3s, 6s, 9s, 12s, 15s
# (max ~45s total) — comfortably covers a normal resume, still bounded.
_RESUME_MAX_ATTEMPTS = 5
_RESUME_BACKOFF_BASE = 3.0  # seconds; delay = base * attempt#

# Postgres internal schemas we never surface as user data sources.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


class AuroraSchemaProvider(DataSourceProvider):
    """Schema provider for Aurora PostgreSQL tables via the RDS Data API."""

    display_name = "Aurora PostgreSQL"
    icon = "aurora"
    supports_sql = True

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        self._cluster_arn = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
        self._secret_arn = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
        self._database = os.environ.get(
            "AURORA_DATA_API_DATABASE", os.environ.get("AURORA_DB_NAME", "microvm_db")
        )
        self._cache: dict[str, tuple[float, SourceSchema]] = {}
        # (timestamp, {source_id}) from this instance's last discover() — see
        # _known_source_ids.
        self._known_ids: tuple[float, set] | None = None

    @property
    def source_type(self) -> str:
        return "aurora"

    @property
    def _configured(self) -> bool:
        """Aurora is usable only when both the cluster and secret ARNs are set."""
        return bool(self._cluster_arn and self._secret_arn)

    # --- Low-level Data API executor -------------------------------------
    def _execute(self, sql: str, parameters: list = None) -> dict:
        """Run one statement over the Data API and return the raw response.
        includeResultMetadata=True so callers get columnMetadata for names/types.

        Retries on DatabaseResumingException: this cluster is Serverless v2 with
        scale-to-zero (min 0 ACU), so it auto-pauses when idle and the FIRST query
        after a pause fails while it wakes (~5-30s). Without this retry, the very
        first schema/discovery/preview call after an idle period returns nothing
        and the Data Sources panel shows an empty schema. Bounded backoff so a
        genuinely-down cluster still fails in reasonable time."""
        client = aws_clients.get_client("rds-data", region_name=self._region)
        kwargs = {
            "resourceArn": self._cluster_arn,
            "secretArn": self._secret_arn,
            "database": self._database,
            "sql": sql,
            "includeResultMetadata": True,
        }
        if parameters:
            kwargs["parameters"] = parameters

        last_err = None
        for attempt in range(_RESUME_MAX_ATTEMPTS):
            try:
                return client.execute_statement(**kwargs)
            except client.exceptions.DatabaseResumingException as e:
                # Cluster is waking from auto-pause — wait and retry.
                last_err = e
                if attempt < _RESUME_MAX_ATTEMPTS - 1:
                    delay = _RESUME_BACKOFF_BASE * (attempt + 1)
                    logger.info(f"Aurora resuming from auto-pause; retry "
                                f"{attempt + 1}/{_RESUME_MAX_ATTEMPTS} in {delay:.0f}s")
                    time.sleep(delay)
            except Exception:
                # Any other error is not a resume race — surface it immediately.
                raise
        # Exhausted resume retries — raise the last resuming error.
        raise last_err

    @staticmethod
    def _field_value(field: dict):
        """Unwrap one Data API field dict into a native Python value."""
        if not field or field.get("isNull"):
            return None
        for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
            if key in field:
                return field[key]
        return None

    def _rows(self, resp: dict) -> list[list]:
        """Unwrap a Data API response's records into a list of value-lists."""
        return [[self._field_value(f) for f in rec] for rec in resp.get("records", []) or []]

    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """Enumerate user tables + views across all non-system schemas in the
        configured database. One information_schema query, run off-loop so the
        blocking boto3 call doesn't stall the event loop."""
        if not self._configured:
            return []
        import asyncio

        params = [{"name": f"s{i}", "value": {"stringValue": s}}
                  for i, s in enumerate(_SYSTEM_SCHEMAS)]
        placeholders = ", ".join(f":{p['name']}" for p in params)
        sql = (
            "SELECT table_schema, table_name "
            "FROM information_schema.tables "
            f"WHERE table_schema NOT IN ({placeholders}) "
            "AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_schema, table_name"
        )
        try:
            resp = await asyncio.to_thread(self._execute, sql, params)
        except Exception as e:
            logger.warning(f"Aurora discovery failed: {e}")
            return []

        # Per-(schema,table) column counts in ONE grouped query, so each row can
        # show a "N cols" subtitle like Athena — without a per-table round-trip.
        # information_schema.tables carries no column info, so we aggregate
        # information_schema.columns once and look each table up. Best-effort: if
        # this fails we just omit the count (detail falls back to empty).
        col_counts: dict[tuple, int] = {}
        try:
            count_sql = (
                "SELECT table_schema, table_name, COUNT(*) AS n "
                "FROM information_schema.columns "
                f"WHERE table_schema NOT IN ({placeholders}) "
                "GROUP BY table_schema, table_name"
            )
            cresp = await asyncio.to_thread(self._execute, count_sql, params)
            for crow in self._rows(cresp):
                cs, cn, n = crow[0], crow[1], crow[2]
                if cs and cn and n is not None:
                    col_counts[(cs, cn)] = int(n)
        except Exception as e:
            logger.warning(f"Aurora column-count query failed (subtitles omitted): {e}")

        refs: list[SourceRef] = []
        for row in self._rows(resp):
            schema, name = row[0], row[1]
            if not schema or not name:
                continue
            ncols = col_counts.get((schema, name))
            refs.append(SourceRef(
                source_type="aurora",
                source_id=f"{self._database}.{schema}.{name}",
                display_name=f"{schema}.{name}",
                # Subtitle mirrors Athena's "N cols"; the schema is shown in the
                # panel via the display_name (schema.table) + the database folder,
                # so it doesn't belong in the subtitle.
                detail=(f"{ncols} cols" if ncols is not None else ""),
                extra={
                    "database": self._database,
                    "schema": schema,
                    "name": name,
                    "table_name": name,
                    "column_count": ncols,
                    "region": self._region,
                },
            ))
        # Remember this enumeration for get_preview's allowlist (see
        # _known_source_ids) so a catalog discovery doesn't re-enumerate per table.
        self._known_ids = (time.time(), {r.source_id for r in refs})
        return refs

    async def _known_source_ids(self) -> set:
        """Source ids this provider instance enumerated within the last
        KNOWN_IDS_TTL_SEC, else a fresh discover(). During a full-catalog discovery
        get_schema() → get_preview() runs once per table right after discover() on
        the SAME instance; without this each one re-ran the whole enumeration (N+1).
        The allowlist is unchanged: ids are still checked against this provider's
        own discovered set, so a forged id is still rejected."""
        memo = self._known_ids
        if memo is not None and time.time() - memo[0] < KNOWN_IDS_TTL_SEC:
            return memo[1]
        return {r.source_id for r in await self.discover()}

    def reader_docs(self) -> list[str]:
        return [
            "read_aurora(sql) -> df                          # Run Aurora PostgreSQL SQL (read-only), return DataFrame",
        ]

    def sql_syntax_docs(self) -> list[str]:
        return [
            "Aurora tables: SELECT * FROM database.schema.table LIMIT 10 "
            "(3-part name; routed to Aurora via the RDS Data API)",
        ]

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """
        Get schema for an Aurora table.
        source_id: 'database.schema.table' (3-part) or 'schema.table' (default db).
        """
        if not self._configured:
            return None
        if source_id in self._cache:
            ts, schema = self._cache[source_id]
            if time.time() - ts < CACHE_TTL_SEC:
                return schema

        database, schema_name, table_name = self._parse_source_id(source_id)

        import asyncio
        params = [
            {"name": "sch", "value": {"stringValue": schema_name}},
            {"name": "tbl", "value": {"stringValue": table_name}},
        ]
        sql = (
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = :sch AND table_name = :tbl "
            "ORDER BY ordinal_position"
        )
        try:
            resp = await asyncio.to_thread(self._execute, sql, params)
        except Exception as e:
            logger.warning(f"Aurora schema lookup failed for {source_id}: {e}")
            return None

        from app.notebook.dtypes import normalize_dtype
        columns = []
        for row in self._rows(resp):
            col_name, data_type, is_nullable = row[0], row[1], row[2]
            if not col_name:
                continue
            columns.append(ColumnInfo(
                name=col_name,
                dtype=normalize_dtype(data_type or "string"),
                nullable=(str(is_nullable).upper() == "YES"),
            ))

        schema = SourceSchema(
            source_type="aurora",
            source_id=f"{database}.{schema_name}.{table_name}",
            display_name=f"{schema_name}.{table_name}",
            columns=columns,
            row_count=None,  # exact count needs a scan; skipped for a cheap discovery
            size="",
        )

        # Best-effort sample values from a single row.
        try:
            preview = await self.get_preview(source_id, limit=1)
            if preview:
                row = preview[0]
                for col in schema.columns:
                    if col.name in row and row[col.name] is not None:
                        col.sample = str(row[col.name])[:50]
        except Exception:
            pass

        self._cache[source_id] = (time.time(), schema)
        return schema

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """Return first N rows as dicts (SELECT * ... LIMIT n over the Data API)."""
        if not self._configured:
            return []
        import asyncio

        # Allowlist the id against the discovered catalog so an unknown/forged id
        # can never reach the quoted-identifier SQL below.
        known = await self._known_source_ids()
        if source_id not in known:
            logger.warning(f"Aurora preview rejected unknown source_id: {source_id}")
            return []

        _db, schema_name, table_name = self._parse_source_id(source_id)
        # Reject double-quote injection into the quoted identifiers.
        if '"' in schema_name or '"' in table_name:
            return []
        # Identifiers can't be bound as Data API parameters; quote them to be safe.
        safe = f'"{schema_name}"."{table_name}"'
        sql = f"SELECT * FROM {safe} LIMIT {int(limit)}"
        try:
            resp = await asyncio.to_thread(self._execute, sql)
        except Exception as e:
            logger.warning(f"Aurora preview failed for {source_id}: {e}")
            return []

        col_meta = resp.get("columnMetadata", []) or []
        cols = [c.get("label") or c.get("name") or f"col_{i}" for i, c in enumerate(col_meta)]
        rows = []
        for values in self._rows(resp):
            rows.append({cols[i]: values[i] for i in range(min(len(cols), len(values)))})
        return rows

    def get_python_snippet(self, source_id: str) -> str:
        _db, schema_name, table_name = self._parse_source_id(source_id)
        var = f"{schema_name}_{table_name}".replace("-", "_")
        return (
            f"# Query Aurora PostgreSQL (read-only) via the Data API\n"
            f"{var} = read_aurora(\"SELECT * FROM {schema_name}.{table_name} LIMIT 100\")\n"
            f"{var}.head()"
        )

    def get_sql_snippet(self, source_id: str) -> str:
        database, schema_name, table_name = self._parse_source_id(source_id)
        return f"SELECT * FROM {database}.{schema_name}.{table_name} LIMIT 100"

    def _parse_source_id(self, source_id: str) -> tuple[str, str, str]:
        """Parse 'db.schema.table' (3-part), 'schema.table' (2-part, default db),
        or bare 'table' (default db + public) into (database, schema, table)."""
        parts = source_id.split(".")
        if len(parts) >= 3:
            return parts[0], parts[1], ".".join(parts[2:])
        if len(parts) == 2:
            return self._database, parts[0], parts[1]
        return self._database, "public", source_id
