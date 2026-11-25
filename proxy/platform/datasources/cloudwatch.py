"""
CloudWatch Logs data source provider.

Surfaces Amazon CloudWatch Logs **log groups** as first-class notebook Data
Sources. Like the Bedrock KB (and unlike Athena/S3/DynamoDB/Aurora), a log group
is NOT a SQL-queryable table — it's queried with CloudWatch Logs Insights (a
pipe-based language) or raw FilterLogEvents — so:

  • supports_sql = False   (excluded from SQL-cell routing + sql_syntax_docs)
  • get_schema() reports the FIELDS CloudWatch has seen in the group (best-effort,
    via get_log_group_fields) as "columns" — a log group is schemaless beyond the
    standard @timestamp/@message, so treat these as "seen recently", not fixed.

The VM reads it via read_logs / grep_logs / tail_logs / ask_logs
(app/notebook/helpers/logs.py), which call the CloudWatch Logs APIs over HTTPS/443
(egress-safe). This provider handles discovery + panel metadata + the insert
snippet; the actual queries run on the VM.

Discovery is SCOPED by DATASOURCE_CLOUDWATCH_LOG_GROUP_PREFIXES (comma-separated,
sourced from config.sh; default '/aws/lambda-microvms/' — this platform's OWN VM
logs). Empty => no CloudWatch data source. This keeps the panel from flooding with
every account log group and matches the exec role's scoped query grant.
"""

import os
import time
import logging
from typing import Optional


from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300  # 5 minutes


class CloudWatchLogsProvider(DataSourceProvider):
    """Data source provider for Amazon CloudWatch Logs log groups."""

    display_name = "CloudWatch Logs"
    icon = "cloudwatch"
    supports_sql = False           # Insights/filter, not SQL — not SQL-cell routable
    requires_vm_execution = False  # discovery is a proxy-side control-plane call

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        # Comma-separated log-group-name prefixes to surface. Default to this
        # platform's own VM log groups so the panel is useful + scoped out of the
        # box; a deployment can widen it via config.sh. Blank => no CloudWatch source.
        raw = os.environ.get("DATASOURCE_CLOUDWATCH_LOG_GROUP_PREFIXES",
                             "/aws/lambda-microvms/")
        self._prefixes = [p.strip() for p in raw.split(",") if p.strip()]
        self._max_groups = int(os.environ.get("DATASOURCE_CLOUDWATCH_MAX_GROUPS", "50"))
        self._cache: dict[str, tuple[float, SourceSchema]] = {}

    @property
    def source_type(self) -> str:
        return "cloudwatch"

    @property
    def _configured(self) -> bool:
        return bool(self._prefixes)

    def _client(self):
        return aws_clients.get_client("logs", region_name=self._region)

    # --- Auto-discovery ---------------------------------------------------
    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """Enumerate log groups under the configured prefix(es). One
        describe_log_groups per prefix, off-loop so the blocking boto3 call doesn't
        stall the event loop. Capped at _max_groups total."""
        if not self._configured:
            return []
        import asyncio

        def _list() -> list[dict]:
            client = self._client()
            out: list[dict] = []
            seen = set()
            for prefix in self._prefixes:
                paginator = client.get_paginator("describe_log_groups")
                for page in paginator.paginate(logGroupNamePrefix=prefix):
                    for g in page.get("logGroups", []):
                        name = g.get("logGroupName")
                        if name and name not in seen:
                            seen.add(name)
                            out.append(g)
                            if len(out) >= self._max_groups:
                                return out
            return out

        try:
            groups = await asyncio.to_thread(_list)
        except Exception as e:
            logger.warning(f"CloudWatch Logs discovery failed: {e}")
            return []

        refs: list[SourceRef] = []
        for g in groups:
            name = g["logGroupName"]
            mb = (g.get("storedBytes", 0) or 0) / (1024 * 1024)
            # Short display name: drop a long common prefix so the panel is readable
            # (e.g. "/aws/lambda-microvms/notebook-...-2048" → "notebook-...-2048").
            display = name
            for prefix in self._prefixes:
                if name.startswith(prefix) and len(prefix) > 1:
                    display = name[len(prefix):] or name
                    break
            refs.append(SourceRef(
                source_type="cloudwatch",
                source_id=name,
                display_name=display,
                detail=(f"{mb:.1f} MB" if mb >= 0.05 else "log group"),
                size=(f"{mb:.1f} MB" if mb >= 0.05 else ""),
                extra={"region": self._region, "log_group": name},
            ))
        return refs

    def reader_docs(self) -> list[str]:
        return [
            "read_logs(query, log_group=None, hours=1, log_stream=None) -> df  # Run a CloudWatch Logs Insights query (fields|filter|stats...); time range is the hours/start/end args, NOT the query. OMIT log_group for THIS notebook's own logs. log_stream= narrows to streams whose name CONTAINS it (e.g. a microvm id). Returns a DataFrame.",
            "ask_logs(question, log_group=None, hours=1, log_stream=None) -> df # Plain-English → the LLM writes the Insights query, runs it, returns the DataFrame (CloudWatch is NOT SQL; this is English→Insights). OMIT log_group for this notebook's own logs; log_stream= narrows to a stream (contains match).",
            "grep_logs(pattern, log_group=None, hours=1, log_stream=None) -> df # Grep raw log events for a pattern (no aggregation) → DataFrame(timestamp, log_stream, message). OMIT log_group for this notebook's own logs; log_stream= narrows to streams containing it.",
            "tail_logs(log_group=None, lines=50, log_stream=None) -> df         # Most-recent N events (newest first). OMIT log_group for this notebook's own logs; log_stream= narrows to streams containing it.",
            "log_groups(prefix=None) -> df                     # List queryable CloudWatch log groups (name, stored_mb, retention, created).",
            "notebook_log_groups() -> list[str]                # The log group(s) that belong to THIS notebook (what read_logs/ask_logs default to when log_group is omitted).",
        ]

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """Report the fields CloudWatch has recently seen in the log group as
        'columns' (best-effort via get_log_group_fields). A log group is schemaless
        beyond @timestamp/@message, so these are 'seen recently', not fixed."""
        if not self._configured:
            return None
        if source_id in self._cache:
            ts, schema = self._cache[source_id]
            if time.time() - ts < CACHE_TTL_SEC:
                return schema

        import asyncio

        def _fields() -> list[tuple[str, float]]:
            client = self._client()
            resp = client.get_log_group_fields(logGroupName=source_id)
            return [(f.get("name", ""), f.get("percent", 0))
                    for f in resp.get("logGroupFields", []) if f.get("name")]

        try:
            fields = await asyncio.to_thread(_fields)
        except Exception as e:
            logger.warning(f"CloudWatch field discovery failed for {source_id}: {e}")
            fields = []

        # Always surface the two standard Insights fields even if discovery is empty
        # (a brand-new/empty group), so the panel + snippet are still useful.
        names = [n for n, _ in fields]
        for std in ("@timestamp", "@message"):
            if std not in names:
                fields.append((std, 100.0))

        columns = [
            ColumnInfo(
                name=name,
                dtype=("date" if name == "@timestamp" else "string"),
                sample=(f"seen in {pct:.0f}% of events" if pct else ""),
                nullable=True,
            )
            for name, pct in fields
        ]
        schema = SourceSchema(
            source_type="cloudwatch",
            source_id=source_id,
            display_name=source_id,
            columns=columns,
            row_count=None,   # events, not rows — a count needs a scan; skip
            size="",
        )
        self._cache[source_id] = (time.time(), schema)
        return schema

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """Most-recent N raw events (timestamp + message) via FilterLogEvents over
        the last 24h — a cheap peek without running an Insights query."""
        if not self._configured:
            return []
        import asyncio

        def _recent() -> list[dict]:
            client = self._client()
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - 24 * 3600 * 1000
            resp = client.filter_log_events(
                logGroupName=source_id, startTime=start_ms, endTime=end_ms,
                limit=max(1, int(limit)),
            )
            rows = []
            for e in resp.get("events", [])[:limit]:
                rows.append({
                    "@timestamp": e.get("timestamp"),
                    "@message": (e.get("message") or "").rstrip("\n")[:500],
                })
            return rows

        try:
            return await asyncio.to_thread(_recent)
        except Exception as e:
            logger.warning(f"CloudWatch preview failed for {source_id}: {e}")
            return []

    def get_python_snippet(self, source_id: str) -> str:
        return (
            f"# Query CloudWatch Logs (Insights) — time range is the hours= arg\n"
            f"logs = read_logs(\n"
            f"    'fields @timestamp, @message | sort @timestamp desc | limit 100',\n"
            f"    log_group={source_id!r}, hours=1,\n"
            f")\n"
            f"logs.head()\n"
            f"# Or ask in plain English (the LLM writes the Insights query):\n"
            f"# ask_logs('error count per hour', log_group={source_id!r}, hours=24)"
        )

    def is_deletable(self, source_id: str) -> bool:
        # Log groups are infrastructure, not user artifacts — never deletable here.
        return False
