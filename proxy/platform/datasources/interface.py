"""
DataSourceProvider — abstract interface for all data source interactions.

Each data source type (Athena, DynamoDB, S3, Local) implements this interface.
A provider owns the FULL lifecycle for its type:
  - discover():          enumerate the sources of this type (auto-discovery)
  - get_schema():        columns / types / samples for one source
  - get_preview():       first N rows
  - get_python_snippet / get_sql_snippet: ready-to-run insert code
  - class metadata:      display_name, icon, capability flags (for the UI)
  - AI metadata:         reader_docs / sql_syntax_docs (for the assistant prompt)

Adding a new source type = implement this interface + register the class in
registry.py. Everything else (endpoints, panel rendering, entity discovery,
AI prompt) derives from the registry.

Concurrency contract:
  The proxy runs on a SINGLE asyncio event loop — any blocking call made on it
  stalls every in-flight request.
  - ASYNC methods (discover, get_schema, get_preview) MUST NOT block the loop.
    Any blocking SDK/IO call (boto3, pandas file reads, time.sleep polling) must
    be offloaded via asyncio.to_thread, or the method must be natively async
    (e.g. local's async httpx calls to the VM).
  - SYNC methods (is_deletable, delete, supports_lifecycle, lifecycle_action,
    get_python_snippet, get_sql_snippet, reader_docs, sql_syntax_docs) MAY block.
    It is the CALLER's responsibility to invoke the I/O-performing ones (delete,
    lifecycle_action, and any check that does I/O — e.g. SageMaker's
    supports_lifecycle reads the endpoint config) via asyncio.to_thread from
    async code. Pure checks/generators (string/prefix tests, snippets, docs) can
    be called inline.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    """Schema information for a single column."""
    name: str
    dtype: str  # 'string', 'int', 'float', 'date', 'boolean', 'json', etc.
    sample: str = ""  # A sample value for display
    nullable: bool = True


@dataclass
class SourceSchema:
    """Schema for a data source."""
    source_type: str  # 'athena', 'dynamodb', 's3', 'local'
    source_id: str  # Table name, file path, S3 key, etc.
    display_name: str  # Human-readable name
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count: int | None = None  # None if unknown
    size: str = ""  # Human-readable size (e.g., "45 KB", "1.2 MB")


@dataclass
class SourceRef:
    """
    A discovered source instance (result of provider.discover()), before any
    schema fetch. This is the generic, type-agnostic descriptor the frontend
    and entity discovery consume.

    Attributes:
        source_type: provider identifier ('s3', 'dynamodb', 'athena', ...)
        source_id:   canonical id used by get_schema/get_snippet (e.g. an S3 URI,
                     'database.table' for Athena, table name for DynamoDB)
        display_name: short human label shown in the panel
        detail:      short secondary line (e.g. "13 KB", "200 items", "8 cols")
        size:        human-readable size where known
        row_count:   row/item count where cheaply known
        extra:       type-specific fields (bucket, key, uri, database, region...)
                     kept so callers that still need type details can read them.
    """
    source_type: str
    source_id: str
    display_name: str
    detail: str = ""
    size: str = ""
    row_count: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "display_name": self.display_name,
            "detail": self.detail,
            "size": self.size,
            "row_count": self.row_count,
            **self.extra,
        }


class DataSourceProvider(ABC):
    """
    Abstract interface for data source interactions.

    Each implementation handles one source type (Athena, DynamoDB, S3, Local).
    See the module docstring's "Concurrency contract": async methods must never
    block the event loop; I/O-performing sync methods are run by callers via
    asyncio.to_thread.
    """

    # --- Class-level metadata (used by the frontend + AI, no instance needed) ---
    display_name: str = ""          # e.g. "Amazon S3" — falls back to source_type
    icon: str = "database"          # frontend icon key (see DataSourcesPanel icon map)
    supports_sql: bool = True       # can this source be queried from a SQL cell?
    requires_vm_execution: bool = False  # schema/discovery needs code run on the VM (local files)

    @property
    @abstractmethod
    def source_type(self) -> str:
        """The source type identifier (e.g., 'athena', 'dynamodb', 's3', 'local')."""

    # --- Auto-discovery ---------------------------------------------------
    async def discover(self, session_id: str = None) -> list["SourceRef"]:
        """
        Enumerate all sources of this type that are in scope.

        Default returns [] — appropriate for types whose sources are discovered
        elsewhere (e.g. local /tmp files are enumerated on the VM). Cloud
        providers (S3, DynamoDB, Athena) override this with boto3 enumeration.
        """
        return []

    async def discover_session(self, session_id: str) -> list["SourceRef"]:
        """
        Enumerate ONLY the session-scoped sources of this type (the subset of
        discover(session_id) that varies by session), excluding the shared ones
        that discover(session_id=None) already returns.

        The registry caches the SHARED cloud catalog (discover(session_id=None))
        once for every caller and overlays this per call, so a session sees its
        own sources and never another session's. Default returns [] — correct for
        every provider whose discover() ignores session_id. S3 overrides it for
        its user-data/<session_id>/ promoted-file overlay.
        """
        return []

    # --- Schema / preview -------------------------------------------------
    @abstractmethod
    async def get_schema(self, source_id: str, session_id: str = None) -> SourceSchema | None:
        """
        Return schema (columns, types, sample values) for a specific source.
        Returns None if the source doesn't exist or can't be accessed.

        Args:
            source_id: Identifier for the source (table name, file path, S3 URI)
            session_id: Optional session context (required for local files on the VM)
        """

    @abstractmethod
    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """
        Return first N rows as list of dicts (for inline preview).
        Returns empty list if unavailable.
        """

    # --- Snippet generation ----------------------------------------------
    def get_python_snippet(self, source_id: str) -> str:
        """Generate Python code to load this source into a DataFrame."""
        return f"# No Python snippet available for {source_id}"

    def get_sql_snippet(self, source_id: str) -> str:
        """Generate SQL query for this source."""
        return f"-- No SQL snippet available for {source_id}"

    # --- AI assistant metadata -------------------------------------------
    # These let the assistant prompt (proxy/notebook/ai/prompts.py) be generated
    # from the registry instead of hardcoding per-source facts. Return one line
    # per entry. The VM still implements the actual helper functions; these just
    # document them so the prompt stays in sync with the registered providers.
    def reader_docs(self) -> list[str]:
        """Lines describing the built-in reader helper(s) for this source, e.g.
        'read_athena(sql, database=...) -> df   # Run Athena SQL, return DataFrame'."""
        return []

    def sql_syntax_docs(self) -> list[str]:
        """Lines describing SQL-cell syntax for this source (empty if !supports_sql)."""
        return []

    # --- Deletion (optional; panel "delete file/source") -----------------
    # Part of the provider contract so the DELETE /datasources/s3-file route (and
    # any future per-source delete) can call these on ANY provider generically.
    # Defaults make deletion OPT-IN: a provider that doesn't override is_deletable
    # reports nothing as deletable, so delete() is never reached for it.
    def is_deletable(self, source_id: str) -> bool:
        """True if this source may be deleted from the panel. Default: False
        (deletion is opt-in; override in providers that support it, e.g. S3 for
        user-owned prefixes). The delete route MUST check this before delete()."""
        return False

    def delete(self, source_id: str) -> None:
        """Delete the given source. Callers MUST validate is_deletable() first.
        Raises on failure so the route can surface an error. Default: unsupported.
        May block (SDK I/O): callers in async code MUST run it via
        asyncio.to_thread."""
        raise NotImplementedError(
            f"{self.source_type} provider does not support deletion"
        )

    # --- Lifecycle (optional; panel "stop / start") ----------------------
    # For sources that can be paused/resumed WITHOUT being deleted (e.g. an
    # instance-backed SageMaker endpoint: stop tears down the running endpoint to
    # halt billing but keeps its config+model so start can recreate it). Opt-in
    # like deletion — a provider that doesn't override supports_lifecycle reports
    # no lifecycle actions, so lifecycle_action() is never reached for it.
    def supports_lifecycle(self, source_id: str) -> bool:
        """True if this source supports stop/start lifecycle actions. Default:
        False. The lifecycle route MUST check this before lifecycle_action()."""
        return False

    def lifecycle_action(self, source_id: str, action: str) -> str:
        """Perform a lifecycle action ('stop' | 'start') on the source. Callers
        MUST validate supports_lifecycle() first. Returns a short status string;
        raises on failure so the route can surface an error. Default: unsupported.
        May block (SDK I/O): callers in async code MUST run it via
        asyncio.to_thread."""
        raise NotImplementedError(
            f"{self.source_type} provider does not support lifecycle actions"
        )
