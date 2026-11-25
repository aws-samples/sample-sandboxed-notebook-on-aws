"""
Athena schema provider — uses AWS Glue catalog for fast schema discovery.
"""

import os
import time
import logging
from typing import Optional


from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)
# Last logged S3 Tables count per "<catalog>/<namespace>" (log-on-change only).
_S3T_LAST_COUNT: dict = {}

# Cache TTL for Glue catalog lookups
CACHE_TTL_SEC = 300  # 5 minutes
# How long get_preview's allowlist may reuse this instance's last discover()
# result instead of re-enumerating (see _known_source_ids).
KNOWN_IDS_TTL_SEC = 60


class AthenaSchemaProvider(DataSourceProvider):
    """Schema provider for Athena tables via Glue catalog."""

    display_name = "Athena"
    icon = "athena"
    supports_sql = True

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        # ATHENA_DB is the DEFAULT database for query execution (db.table prefix,
        # read_athena default, SQL engine catalog) — a single value.
        self._database = os.environ.get("ATHENA_DB", "microvm_demo_db")
        # Discovery scope is separate: an optional comma-separated allowlist of
        # Glue databases to surface. When empty, discovery auto-lists EVERY Glue
        # database the role can access (a role may have many).
        self._database_allowlist = [
            d.strip() for d in os.environ.get("DATASOURCE_ATHENA_DATABASES", "").split(",")
            if d.strip()
        ]
        self._workgroup = os.environ.get("ATHENA_WORKGROUP", "microvm-demo")
        self._cache: dict[str, tuple[float, SourceSchema]] = {}
        # (timestamp, {source_id}) from this instance's last discover() — see
        # _known_source_ids.
        self._known_ids: tuple[float, set] | None = None
        # --- S3 Tables (Apache Iceberg) federated catalog ------------------
        # S3 Tables live in a SEPARATE Glue catalog ("s3tablescatalog/<bucket>")
        # that Athena reaches via a registered data catalog + query execution
        # context (NOT the default AwsDataCatalog). We surface them through THIS
        # same provider (source_type "athena") but tag each ref with its catalog
        # + table_format so the VM query path targets the right catalog and the
        # panel can render them distinctly. All three must be set (they are, in
        # config.sh) for S3 Tables discovery to run; empty = feature off.
        self._s3t_bucket = os.environ.get("S3TABLES_BUCKET", "").strip()
        self._s3t_namespace = os.environ.get("S3TABLES_NAMESPACE", "").strip()
        self._s3t_catalog = os.environ.get("S3TABLES_ATHENA_CATALOG", "").strip()
        self._account_id = os.environ.get("ACCOUNT_ID", "").strip()

    @property
    def _s3tables_enabled(self) -> bool:
        """S3 Tables surfacing is on only when the bucket, namespace, Athena
        catalog name, and account id are all configured."""
        return bool(self._s3t_bucket and self._s3t_namespace
                    and self._s3t_catalog and self._account_id)

    def _s3tables_glue_catalog_id(self) -> str:
        """Nested Glue CatalogId for the federated child catalog, e.g.
        '111122223333:s3tablescatalog/microvm-tables-111122223333'. Glue's
        get_databases/get_tables/get_table accept this as CatalogId."""
        return f"{self._account_id}:s3tablescatalog/{self._s3t_bucket}"

    def _databases_to_scan(self, glue) -> list[str]:
        """Resolve which Glue databases discovery should enumerate.
        Explicit allowlist wins; otherwise auto-list all accessible databases,
        falling back to the default ATHENA_DB if listing isn't permitted."""
        if self._database_allowlist:
            return self._database_allowlist
        try:
            databases = []
            paginator = glue.get_paginator("get_databases")
            for page in paginator.paginate():
                databases.extend(db["Name"] for db in page.get("DatabaseList", []))
            return databases or [self._database]
        except Exception as e:
            logger.warning(f"Athena get_databases failed; falling back to ATHENA_DB: {e}")
            return [self._database]

    @property
    def source_type(self) -> str:
        return "athena"

    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """Enumerate tables across every in-scope Glue database (see
        _databases_to_scan), plus S3 Tables. All the Glue calls are blocking, so the
        whole enumeration runs via asyncio.to_thread to keep the proxy's single
        event loop free (mirrors Aurora.discover)."""
        import asyncio
        refs = await asyncio.to_thread(self._discover_blocking, session_id)
        # Remember this enumeration for get_preview's allowlist (see
        # _known_source_ids) so a catalog discovery doesn't re-enumerate per table.
        self._known_ids = (time.time(), {r.source_id for r in refs})
        return refs

    async def _known_source_ids(self) -> set:
        """Source ids this provider instance enumerated within the last
        KNOWN_IDS_TTL_SEC, else a fresh discover(). During a full-catalog discovery
        get_schema() → get_preview() runs once per table right after discover() on
        the SAME instance; without this each one re-ran the whole Glue + S3 Tables
        enumeration (N+1 — the repeated 'S3 Tables discovery' log lines). The
        allowlist is unchanged: ids are still checked against this provider's own
        discovered set, so a forged id is still rejected."""
        memo = self._known_ids
        if memo is not None and time.time() - memo[0] < KNOWN_IDS_TTL_SEC:
            return memo[1]
        return {r.source_id for r in await self.discover()}

    def _discover_blocking(self, session_id: str = None) -> list[SourceRef]:
        """Blocking Glue/S3-Tables enumeration — always called via to_thread from
        the async discover(), never directly on the event loop."""
        refs: list[SourceRef] = []
        try:
            glue = aws_clients.get_client("glue", region_name=self._region)
            for database in self._databases_to_scan(glue):
                try:
                    paginator = glue.get_paginator("get_tables")
                    tables = []
                    for page in paginator.paginate(DatabaseName=database):
                        tables.extend(page.get("TableList", []))
                except Exception as e:
                    logger.warning(f"Athena discovery failed for database {database}: {e}")
                    continue
                for table in tables:
                    columns = table.get("StorageDescriptor", {}).get("Columns", [])
                    name = table["Name"]
                    update_time = table.get("UpdateTime")
                    refs.append(SourceRef(
                        source_type="athena",
                        source_id=f"{database}.{name}",
                        display_name=name,
                        detail=f"{len(columns)} cols",
                        extra={
                            "name": name,
                            "table_name": name,
                            "database": database,
                            "columns": [{"name": c["Name"], "type": c["Type"]} for c in columns],
                            "column_count": len(columns),
                            "region": self._region,
                            # Change-signal metadata consumed by entity discovery:
                            "s3_location": table.get("StorageDescriptor", {}).get("Location", ""),
                            "glue_update_time": update_time.isoformat() if update_time else None,
                        },
                    ))
        except Exception as e:
            logger.warning(f"Athena discovery failed: {e}")

        # --- S3 Tables (Iceberg) in the federated s3tablescatalog child catalog.
        # Enumerated separately because they live in a different Glue catalog
        # (addressed by nested CatalogId). Tagged with catalog + table_format so
        # downstream (VM query routing, panel visuals) can treat them distinctly.
        # A failure here must be LOUD: a swallowed error silently drops the S3
        # Tables from the panel (they "vanish"), which is confusing to debug — so
        # log at ERROR with the exception type + repr, not a soft warning.
        if self._s3tables_enabled:
            try:
                s3t_refs = self._discover_s3tables()
                refs.extend(s3t_refs)
                # INFO only when the count changes (first discovery included);
                # every discovery repeats it, so the unchanged case is DEBUG.
                where = f"{self._s3t_catalog}/{self._s3t_namespace}"
                changed = _S3T_LAST_COUNT.get(where) != len(s3t_refs)
                _S3T_LAST_COUNT[where] = len(s3t_refs)
                (logger.info if changed else logger.debug)(
                    f"S3 Tables discovery: {len(s3t_refs)} table(s) in {where}"
                )
            except Exception as e:
                logger.error(
                    f"S3 Tables discovery FAILED (tables will be missing from the "
                    f"panel): {type(e).__name__}: {e!r}"
                )
        return refs

    def _discover_s3tables(self) -> list[SourceRef]:
        """Enumerate the tables in the S3 Tables namespace via the federated Glue
        child catalog (nested CatalogId). Each ref reuses source_type 'athena'
        (so all the cloud-catalog plumbing applies) but carries:
          - catalog:      the Athena data-catalog NAME the VM must set as the query
                          execution context (S3TABLES_ATHENA_CATALOG).
          - database:     the namespace (acts as the 'database' in source_id).
          - table_format: 'iceberg' — the panel's distinct-visual discriminator.
        source_id stays 2-part 'namespace.table' (the namespace is unique vs the
        Glue db name, so references are unambiguous)."""
        refs: list[SourceRef] = []
        glue = aws_clients.get_client("glue", region_name=self._region)
        cat_id = self._s3tables_glue_catalog_id()
        paginator = glue.get_paginator("get_tables")
        tables = []
        for page in paginator.paginate(CatalogId=cat_id, DatabaseName=self._s3t_namespace):
            tables.extend(page.get("TableList", []))
        for table in tables:
            name = table["Name"]
            columns = table.get("StorageDescriptor", {}).get("Columns", [])
            update_time = table.get("UpdateTime")
            refs.append(SourceRef(
                source_type="athena",
                source_id=f"{self._s3t_namespace}.{name}",
                display_name=name,
                # Just the column count — the "Iceberg" badge already marks the
                # format, so appending "· Iceberg" here would show it twice.
                detail=f"{len(columns)} cols",
                extra={
                    "name": name,
                    "table_name": name,
                    "database": self._s3t_namespace,
                    "catalog": self._s3t_catalog,
                    "table_format": "iceberg",
                    "columns": [{"name": c["Name"], "type": c["Type"]} for c in columns],
                    "column_count": len(columns),
                    "region": self._region,
                    "s3_location": table.get("StorageDescriptor", {}).get("Location", ""),
                    "glue_update_time": update_time.isoformat() if update_time else None,
                },
            ))
        return refs

    def reader_docs(self) -> list[str]:
        return [
            f'read_athena(sql, database="{self._database}") -> df  # Run Athena SQL, return DataFrame',
        ]

    def sql_syntax_docs(self) -> list[str]:
        docs = [
            f"Athena tables: SELECT * FROM {self._database}.table_name LIMIT 10 (uses database.table format)",
        ]
        if self._s3tables_enabled:
            docs.append(
                f"S3 Tables (Iceberg): SELECT * FROM {self._s3t_namespace}.table_name LIMIT 10 "
                f"(same database.table form; routed to the federated S3 Tables catalog; "
                f"supports Iceberg time-travel, e.g. ... FOR TIMESTAMP AS OF ...)"
            )
        return docs

    def _is_s3tables_source(self, database: str) -> bool:
        """True when this database is the S3 Tables namespace (so schema/preview
        must target the federated child catalog, not the default AwsDataCatalog)."""
        return self._s3tables_enabled and database == self._s3t_namespace

    def _get_schema_blocking(self, database: str, table_name: str) -> Optional[SourceSchema]:
        """Blocking Glue get_table + SourceSchema assembly — always called via
        asyncio.to_thread from the async get_schema(), never on the event loop.
        Returns the SourceSchema WITHOUT sample values (those are populated by the
        async caller via get_preview)."""
        glue = aws_clients.get_client("glue", region_name=self._region)
        # S3 Tables live in the federated child catalog — address it by nested
        # CatalogId; regular Athena tables use the account's default catalog.
        get_table_kwargs = {"DatabaseName": database, "Name": table_name}
        if self._is_s3tables_source(database):
            get_table_kwargs["CatalogId"] = self._s3tables_glue_catalog_id()
        resp = glue.get_table(**get_table_kwargs)
        table = resp["Table"]

        columns = []
        for col in table.get("StorageDescriptor", {}).get("Columns", []):
            columns.append(ColumnInfo(
                name=col["Name"],
                dtype=self._map_athena_type(col.get("Type", "string")),
                nullable=True,
            ))
        # Partition keys are also columns
        for col in table.get("PartitionKeys", []):
            columns.append(ColumnInfo(
                name=col["Name"],
                dtype=self._map_athena_type(col.get("Type", "string")),
                nullable=True,
            ))

        # Try to get row count from table parameters
        params = table.get("Parameters", {})
        row_count = None
        if "recordCount" in params:
            try:
                row_count = int(params["recordCount"])
            except (ValueError, TypeError):
                pass

        size = params.get("sizeKey", params.get("totalSize", ""))
        if size:
            try:
                size_bytes = int(size)
                if size_bytes < 1024:
                    size = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    size = f"{size_bytes / 1024:.1f} KB"
                else:
                    size = f"{size_bytes / (1024 * 1024):.1f} MB"
            except (ValueError, TypeError):
                size = ""

        return SourceSchema(
            source_type="athena",
            source_id=f"{database}.{table_name}",
            display_name=table_name,
            columns=columns,
            row_count=row_count,
            size=size,
        )

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """
        Get schema for an Athena table.
        source_id: 'table_name' or 'database.table_name'
        """
        # Check cache
        if source_id in self._cache:
            ts, schema = self._cache[source_id]
            if time.time() - ts < CACHE_TTL_SEC:
                return schema

        # Parse database.table
        if '.' in source_id:
            database, table_name = source_id.split('.', 1)
        else:
            database = self._database
            table_name = source_id

        try:
            import asyncio
            # Offload blocking boto3 so it never runs on the single proxy event loop.
            schema = await asyncio.to_thread(self._get_schema_blocking, database, table_name)

            # Populate sample values from a quick query
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

        except Exception as e:
            logger.warning(f"Athena schema lookup failed for {source_id}: {e}")
            return None

    def _get_preview_blocking(self, database: str, table_name: str, limit: int) -> list[dict]:
        """Blocking Athena start/poll/get-results — always called via
        asyncio.to_thread from the async get_preview(), never on the event loop.
        The poll loop uses time.sleep (not asyncio.sleep) because it runs in a
        worker thread with no event loop."""
        athena = aws_clients.get_client("athena", region_name=self._region)

        start_kwargs = {"WorkGroup": self._workgroup}
        if self._is_s3tables_source(database):
            # S3 Tables: address the table through the federated Athena data
            # catalog + namespace via the execution context, with a plain
            # (unqualified) table name — the "catalog"."db"."table" inline form
            # does NOT parse for the federated catalog.
            sql = f'SELECT * FROM "{table_name}" LIMIT {limit}'
            start_kwargs["QueryExecutionContext"] = {
                "Catalog": self._s3t_catalog,
                "Database": database,
            }
        else:
            sql = f'SELECT * FROM "{database}"."{table_name}" LIMIT {limit}'
        exec_resp = athena.start_query_execution(QueryString=sql, **start_kwargs)
        execution_id = exec_resp["QueryExecutionId"]

        # Poll for completion (max 15s)
        state = None
        for _ in range(30):
            status = athena.get_query_execution(QueryExecutionId=execution_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.5)

        if state != "SUCCEEDED":
            return []

        results = athena.get_query_results(QueryExecutionId=execution_id)
        rows_data = results.get("ResultSet", {}).get("Rows", [])
        if len(rows_data) < 2:
            return []

        # First row is headers
        headers = [col.get("VarCharValue", "") for col in rows_data[0].get("Data", [])]
        rows = []
        for row in rows_data[1:]:
            row_dict = {}
            for i, cell in enumerate(row.get("Data", [])):
                if i < len(headers):
                    row_dict[headers[i]] = cell.get("VarCharValue", "")
            rows.append(row_dict)
        return rows

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """Run a LIMIT query via Athena and return rows."""
        # Allowlist the id against the discovered catalog so an unknown/forged id
        # can never reach the quoted-identifier SQL below.
        known = await self._known_source_ids()
        if source_id not in known:
            logger.warning(f"Athena preview rejected unknown source_id: {source_id}")
            return []
        if '.' in source_id:
            database, table_name = source_id.split('.', 1)
        else:
            database = self._database
            table_name = source_id
        # Reject double-quote injection into the quoted identifiers.
        if '"' in database or '"' in table_name:
            return []

        try:
            import asyncio
            # Offload blocking boto3 so it never runs on the single proxy event loop.
            return await asyncio.to_thread(self._get_preview_blocking, database, table_name, limit)
        except Exception as e:
            logger.warning(f"Athena preview failed for {source_id}: {e}")
            return []

    def get_python_snippet(self, source_id: str) -> str:
        # Identical for regular Athena AND S3 Tables (Iceberg): reference the table
        # by database.table. read_athena auto-resolves the federated catalog from
        # the database, so no catalog=/database= plumbing leaks into user code.
        if '.' in source_id:
            database, table_name = source_id.split('.', 1)
        else:
            database = self._database
            table_name = source_id
        return (
            f"# Query Athena table\n"
            f"{table_name} = read_athena(\"SELECT * FROM {database}.{table_name} LIMIT 100\")\n"
            f"{table_name}.head()"
        )

    def get_sql_snippet(self, source_id: str) -> str:
        # For both regular Athena and S3 Tables the SQL cell uses the 2-part
        # database.table form: the VM SQL router resolves the S3 Tables namespace
        # to its federated catalog automatically (Option B — nested under Athena).
        if '.' in source_id:
            return f"SELECT * FROM {source_id} LIMIT 100"
        return f"SELECT * FROM {self._database}.{source_id} LIMIT 100"

    @staticmethod
    def _map_athena_type(athena_type: str) -> str:
        """Map Athena/Hive types to simple display types."""
        t = athena_type.lower()
        if t in ("string", "varchar", "char"):
            return "string"
        if t in ("int", "integer", "bigint", "smallint", "tinyint"):
            return "int"
        if t in ("double", "float", "decimal"):
            return "float"
        if t in ("date",):
            return "date"
        if t in ("timestamp",):
            return "datetime"
        if t in ("boolean",):
            return "boolean"
        if t.startswith("array") or t.startswith("map") or t.startswith("struct"):
            return "json"
        return t
