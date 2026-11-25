"""
DynamoDB schema provider — infers schema by sampling items.
DynamoDB is schemaless, so we scan a few items and union all keys.
"""

import os
import time
import logging
from typing import Optional
from decimal import Decimal

import boto3

from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300  # 5 minutes


class DynamoDBSchemaProvider(DataSourceProvider):
    """Schema provider for DynamoDB tables (schema inferred from samples)."""

    display_name = "DynamoDB"
    icon = "dynamodb"
    supports_sql = True

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        # Discovery scope: only surface tables whose name contains one of these
        # hints (keeps unrelated account tables out of the panel). Declarative
        # via config.sh (DATASOURCE_DDB_NAME_FILTERS); default preserves behavior.
        self._name_filters = [
            h.strip() for h in os.environ.get("DATASOURCE_DDB_NAME_FILTERS", "microvm,demo,ecommerce").split(",")
            if h.strip()
        ]
        self._cache: dict[str, tuple[float, SourceSchema]] = {}

    @property
    def source_type(self) -> str:
        return "dynamodb"

    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """Enumerate DynamoDB tables whose name matches a configured hint.

        The boto3 enumeration (list_tables + describe_table per table) is blocking,
        so it runs via asyncio.to_thread — otherwise it would stall the proxy's
        single event loop for the whole scan (mirrors Aurora.discover)."""
        import asyncio

        def _enumerate() -> list[SourceRef]:
            refs: list[SourceRef] = []
            try:
                ddb = aws_clients.get_client("dynamodb", region_name=self._region)
                for table_name in ddb.list_tables().get("TableNames", []):
                    if self._name_filters and not any(h in table_name for h in self._name_filters):
                        continue
                    item_count = 0
                    try:
                        desc = ddb.describe_table(TableName=table_name)
                        item_count = desc["Table"].get("ItemCount", 0)
                    except Exception:
                        pass
                    refs.append(SourceRef(
                        source_type="dynamodb",
                        source_id=table_name,
                        display_name=table_name,
                        detail=f"{item_count} items",
                        row_count=item_count,
                        extra={
                            "name": table_name,
                            "table_name": table_name,
                            "item_count": item_count,
                            "region": self._region,
                        },
                    ))
            except Exception as e:
                logger.warning(f"DynamoDB discovery failed: {e}")
            return refs

        return await asyncio.to_thread(_enumerate)

    def reader_docs(self) -> list[str]:
        return [
            "read_dynamodb(table_name, limit=None) -> df     # Scan DynamoDB table into DataFrame",
            'read_dynamodb_query(table_name, key, value, sort_key=None, sort_value=None) -> df  # Query by partition key (+optional sort key); pass key NAME and VALUE',
        ]

    def sql_syntax_docs(self) -> list[str]:
        return [
            'DynamoDB tables: SELECT * FROM dynamodb."table-name" LIMIT 10 '
            "(tries PartiQL server-side first, falls back to scan+DuckDB for JOINs/GROUP BY)",
        ]

    def _get_schema_blocking(self, source_id: str) -> Optional[SourceSchema]:
        """Blocking DynamoDB describe/scan + schema inference — always called via
        asyncio.to_thread from the async get_schema(), never on the event loop."""
        try:
            dynamodb = boto3.resource("dynamodb", region_name=self._region)
            table = dynamodb.Table(source_id)

            # Get table info
            item_count = table.item_count or 0
            size_bytes = table.table_size_bytes or 0

            if size_bytes < 1024:
                size = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size = f"{size_bytes / 1024:.1f} KB"
            else:
                size = f"{size_bytes / (1024 * 1024):.1f} MB"

            # Scan a few items to infer schema
            resp = table.scan(Limit=10)
            items = resp.get("Items", [])

            # Union all keys across sampled items
            column_types: dict[str, tuple[str, str]] = {}  # name → (type, sample)
            for item in items:
                for key, value in item.items():
                    if key not in column_types:
                        dtype = self._infer_type(value)
                        sample = self._format_sample(value)
                        column_types[key] = (dtype, sample)

            # Build columns, put key schema first
            key_names = [k["AttributeName"] for k in table.key_schema] if table.key_schema else []
            columns = []
            # Add key columns first
            for key_name in key_names:
                if key_name in column_types:
                    dtype, sample = column_types.pop(key_name)
                    columns.append(ColumnInfo(name=key_name, dtype=dtype, sample=sample, nullable=False))
            # Add remaining columns
            for name, (dtype, sample) in sorted(column_types.items()):
                columns.append(ColumnInfo(name=name, dtype=dtype, sample=sample))

            return SourceSchema(
                source_type="dynamodb",
                source_id=source_id,
                display_name=source_id,
                columns=columns,
                row_count=item_count,
                size=size,
            )

        except Exception as e:
            logger.warning(f"DynamoDB schema lookup failed for {source_id}: {e}")
            return None

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """
        Infer schema by scanning a few items.
        source_id: table name (e.g., 'microvm-demo-data')
        """
        if source_id in self._cache:
            ts, schema = self._cache[source_id]
            if time.time() - ts < CACHE_TTL_SEC:
                return schema

        import asyncio
        # Offload blocking boto3 so it never runs on the single proxy event loop.
        schema = await asyncio.to_thread(self._get_schema_blocking, source_id)
        if schema is not None:
            self._cache[source_id] = (time.time(), schema)
        return schema

    def _get_preview_blocking(self, source_id: str, limit: int) -> list[dict]:
        """Blocking DynamoDB scan — always called via asyncio.to_thread from the
        async get_preview(), never on the event loop."""
        try:
            dynamodb = boto3.resource("dynamodb", region_name=self._region)
            table = dynamodb.Table(source_id)
            resp = table.scan(Limit=limit)
            items = resp.get("Items", [])
            # Convert Decimal to float for JSON serialization
            return [self._convert_decimals(item) for item in items]
        except Exception as e:
            logger.warning(f"DynamoDB preview failed for {source_id}: {e}")
            return []

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """Scan first N items."""
        import asyncio
        # Offload blocking boto3 so it never runs on the single proxy event loop.
        return await asyncio.to_thread(self._get_preview_blocking, source_id, limit)

    def get_python_snippet(self, source_id: str) -> str:
        var_name = source_id.replace('-', '_')
        return (
            f"# Scan DynamoDB table into DataFrame\n"
            f"{var_name} = read_dynamodb('{source_id}')\n"
            f"{var_name}.head()"
        )

    def get_sql_snippet(self, source_id: str) -> str:
        return f'SELECT * FROM dynamodb."{source_id}" LIMIT 10'

    @staticmethod
    def _infer_type(value) -> str:
        """Infer a simple type name from a DynamoDB value."""
        if isinstance(value, Decimal):
            return "float" if '.' in str(value) else "int"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "float" if isinstance(value, float) else "int"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "list"
        if isinstance(value, dict):
            return "map"
        return "string"

    @staticmethod
    def _format_sample(value) -> str:
        """Format a value as a short sample string."""
        if isinstance(value, Decimal):
            return str(float(value)) if '.' in str(value) else str(int(value))
        s = str(value)
        return s[:50] if len(s) > 50 else s

    @staticmethod
    def _convert_decimals(item: dict) -> dict:
        """Convert Decimal values to float/int for JSON serialization."""
        result = {}
        for key, value in item.items():
            if isinstance(value, Decimal):
                result[key] = float(value) if '.' in str(value) else int(value)
            else:
                result[key] = value
        return result
