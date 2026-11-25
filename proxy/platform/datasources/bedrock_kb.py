"""
Bedrock Knowledge Base data source provider.

Surfaces a configured Amazon Bedrock Knowledge Base (backed by Amazon S3 Vectors)
as a first-class notebook Data Source, alongside Athena/S3/DynamoDB/Aurora. Unlike
those, a Knowledge Base is RETRIEVE-ONLY — a vector store you query semantically,
not a tabular source you SQL against — so:

  • supports_sql = False        (excluded from SQL-cell routing + sql_syntax_docs)
  • get_schema() has NO columns (a KB has documents/chunks, not rows/columns)

The VM reads it via the read_bedrock_kb(query) / ask_bedrock_kb(query) helpers
(app/notebook/helpers.py), which call the bedrock-agent-runtime Retrieve /
RetrieveAndGenerate APIs. This provider handles discovery + panel metadata + the
insert snippet; the actual retrieval runs on the VM.

Gated on BEDROCK_KB_ID (sourced from scripts/.kb_env via config.sh at launch):
when unset, discover() no-ops so non-KB deployments behave exactly as before.
BEDROCK_KB_ID is a COMMA-SEPARATED list — each id is surfaced as its own Data
Source, so multiple Knowledge Bases can coexist with no code change.
"""

import os
import logging
from typing import Optional


from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)


class BedrockKBProvider(DataSourceProvider):
    """Data source provider for an Amazon Bedrock Knowledge Base (S3 Vectors)."""

    display_name = "Bedrock Knowledge Base"
    icon = "bedrock_kb"
    supports_sql = False          # retrieve-only — not a SQL-queryable table
    requires_vm_execution = False  # discovery is a proxy-side control-plane call

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        # The configured KB(s) to expose. BEDROCK_KB_ID is a COMMA-SEPARATED list so
        # a deployment can surface more than one Knowledge Base — each id becomes its
        # own Data Source (and its own entity-intel doc / sparkle) with no code change.
        # Blank => no KB data source (the default without --WithKnowledgeBase).
        self._kb_ids = [
            k.strip() for k in os.environ.get("BEDROCK_KB_ID", "").split(",") if k.strip()
        ]

    @property
    def source_type(self) -> str:
        return "bedrock_kb"

    @property
    def _kb_id(self) -> str:
        """First configured KB id (used as the default target when a caller doesn't
        specify a source_id, e.g. get_schema). Empty string when none configured."""
        return self._kb_ids[0] if self._kb_ids else ""

    @property
    def _configured(self) -> bool:
        return bool(self._kb_ids)

    def _client(self):
        # bedrock-agent is the control plane (GetKnowledgeBase / ListKnowledgeBases).
        return aws_clients.get_client("bedrock-agent", region_name=self._region)

    # --- Auto-discovery ---------------------------------------------------
    def _discover_one(self, kb_id: str) -> Optional[SourceRef]:
        """Build a SourceRef for ONE configured KB id. Returns None (and logs) if the
        id can't be resolved, so a single bad id never hides the others."""
        try:
            resp = self._client().get_knowledge_base(knowledgeBaseId=kb_id)
            kb = resp.get("knowledgeBase", {})
            name = kb.get("name") or kb_id
            status = kb.get("status") or ""
            detail = "Knowledge Base"
            if status and status != "ACTIVE":
                detail = f"Knowledge Base ({status})"
            return SourceRef(
                source_type="bedrock_kb",
                source_id=kb_id,
                display_name=name,
                detail=detail,
                extra={
                    "kb_id": kb_id,
                    "status": status,
                    # Region surfaced so the panel/metadata can show where it lives
                    # (mirrors how other providers pass region through `extra`).
                    "region": self._region,
                },
            )
        except Exception as e:  # pragma: no cover — isolated; a bad KB id just hides it
            logger.warning(f"Bedrock KB discovery failed for {kb_id}: {e}")
            return None

    def _discover_blocking(self) -> list[SourceRef]:
        """Blocking bedrock-agent get_knowledge_base per configured id — always
        called via asyncio.to_thread from the async discover(), never on the event
        loop."""
        refs = [self._discover_one(kb_id) for kb_id in self._kb_ids]
        return [r for r in refs if r is not None]

    def _kb_name(self, kb_id: str) -> str:
        """Blocking bedrock-agent get_knowledge_base name lookup — always called via
        asyncio.to_thread from the async get_schema(), never on the event loop.
        Falls back to the id itself if the lookup fails."""
        try:
            resp = self._client().get_knowledge_base(knowledgeBaseId=kb_id)
            kb = resp.get("knowledgeBase", {})
            return kb.get("name") or kb_id
        except Exception as e:
            logger.warning(f"Bedrock KB schema lookup failed for {kb_id}: {e}")
            return kb_id

    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """Return one source PER configured KB id (or [] when unconfigured).

        We deliberately surface ONLY the KB(s) named in BEDROCK_KB_ID rather than
        every KB in the account, so the panel shows the workspace's configured KBs
        and nothing else. Add another id to BEDROCK_KB_ID (comma-separated) and it
        appears automatically — no code change, and entity-discovery profiles it too."""
        if not self._configured:
            return []
        import asyncio
        # Offload blocking boto3 so it never runs on the single proxy event loop.
        return await asyncio.to_thread(self._discover_blocking)

    # --- Schema / preview -------------------------------------------------
    # The columns a read_bedrock_kb(...) retrieval RETURNS — one row per matched
    # chunk. A KB has no stored table schema, but surfacing the RESULT shape here
    # makes the panel's schema expander informative (and matches what the reader
    # actually produces), instead of showing an empty/no-schema entry.
    _RESULT_COLUMNS = [
        ("text", "string", "The matched product-spec chunk text"),
        ("score", "float", "Relevance score (higher = closer match)"),
        ("source_uri", "string", "Source document URI (e.g. the S3 object)"),
        ("source_type", "string", "Source location type (S3, WEB, ...)"),
        ("metadata", "json", "Document metadata (category, brand, product_id, ...)"),
    ]

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """A Knowledge Base stores document chunks, not a table — but retrieving from
        it yields a well-defined result shape. Return THAT as the schema so the panel
        shows the columns a read_bedrock_kb(...) call produces (text/score/source_uri/
        source_type/metadata), which is the useful thing to show for a KB."""
        if not self._configured:
            return None
        import asyncio
        # Offload blocking boto3 so it never runs on the single proxy event loop.
        name = await asyncio.to_thread(self._kb_name, source_id or self._kb_id)
        return SourceSchema(
            source_type="bedrock_kb",
            source_id=source_id or self._kb_id,
            display_name=name,
            columns=[ColumnInfo(name=n, dtype=t, sample=desc) for (n, t, desc) in self._RESULT_COLUMNS],
            row_count=None,
            size="",
        )

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """No inline preview for a KB — there are no rows to show. The user previews
        content by running a retrieval (read_bedrock_kb) in a cell."""
        return []

    # --- Snippet generation ----------------------------------------------
    def get_python_snippet(self, source_id: str) -> str:
        return (
            "# Semantic search the Bedrock Knowledge Base (returns one row per chunk:\n"
            "# text, score, source_uri, source_type, metadata)\n"
            'results = read_bedrock_kb("your question here", top_k=5)\n'
            "results.head()\n\n"
            "# Filter by metadata and/or a minimum confidence score:\n"
            '# results = read_bedrock_kb("skincare", filter={"category": "Beauty"}, min_score=0.38)\n\n'
            "# Or get a synthesized, cited answer (RetrieveAndGenerate):\n"
            '# answer = ask_bedrock_kb("your question here")\n'
            "# print(answer['answer'])"
        )

    def get_sql_snippet(self, source_id: str) -> str:
        """A Knowledge Base is retrieve-only (supports_sql=False), so it's never
        SQL-queryable. The panel hides the SQL insert button for it, but if a SQL
        snippet is ever requested anyway, return an actionable comment pointing at
        the Python helper rather than the generic 'no snippet' default."""
        return (
            "-- A Bedrock Knowledge Base is not SQL-queryable. Use a Python cell:\n"
            '--   read_bedrock_kb("your question", top_k=5)   # retrieved chunks\n'
            '--   ask_bedrock_kb("your question")             # synthesized answer'
        )

    # --- AI assistant metadata -------------------------------------------
    def reader_docs(self) -> list[str]:
        return [
            'read_bedrock_kb(query, top_k=5, filter=None, min_score=None) -> df   # Semantic search a Bedrock KB; one row per chunk (text/score/source_uri/source_type/metadata). filter=metadata filter; min_score=drop low-score rows',
            'ask_bedrock_kb(query, filter=None) -> dict   # RetrieveAndGenerate: a synthesized ANSWER grounded in the KB, with citations ({answer, citations})',
        ]

    # supports_sql is False, so sql_syntax_docs() stays the inherited empty list —
    # a KB is never routed through the SQL engine.
