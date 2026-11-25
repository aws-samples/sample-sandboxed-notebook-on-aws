"""
Abstract storage interface for the Sandboxed Notebook on AWS proxy.

All storage backends must implement this class. The proxy code interacts
only with these methods — never with database-specific APIs directly.

Implemented backends: SQLite (sqlite_db.py, default) and Postgres (postgres_db.py).
To add a new backend:
  1. Create a new file (e.g., <name>_db.py)
  2. Create a class that extends StorageBackend
  3. Implement all abstract methods
  4. Register it in __init__.py
"""

from abc import ABC, abstractmethod
from typing import Callable, Optional

_UNSET = object()  # sentinel: "don't touch real_session_id" vs "set it to NULL"


class StorageBackend(ABC):
    """Abstract interface for all storage operations."""

    # ============================================================
    # LIFECYCLE
    # ============================================================

    @abstractmethod
    def initialize(self, connection_string: str = "") -> None:
        """Initialize the storage backend.
        
        Args:
            connection_string: Database connection URL. Empty for SQLite (uses local file).
                             For Postgres: "postgresql://user:pass@host:5432/db_name"
        """
        ...

    @abstractmethod
    def initialize_pool_manager_schema(self) -> None:
        """Create ONLY the tables the standalone pool_manager service uses
        (pool_vms + its pick index, and the session_registry / vm_sessions
        tables it reads), if they are missing. Every statement is
        CREATE ... IF NOT EXISTS with the same DDL as initialize(): an existing
        table, its columns and its rows are never dropped, altered or rewritten,
        and no migration runs. Safe to call on every pool_manager startup,
        including against a DB the proxy already initialized."""
        ...

    # ============================================================
    # NOTEBOOKS
    # ============================================================

    # Cell dict shape (notebook_create `cells=`, notebook_update(cells=...),
    # and the `cells` list returned by notebook_get / notebook_list). Every
    # backend persists these keys one row per cell in notebook_cells:
    #   id, type, code, output, error, html, outputVariable,
    #   executionNumber (int|None), executionTime (milliseconds, float|None),
    #   lastExecutedCode, aiExplanation, aiExplanationEdited (bool),
    #   aiExplanationSource, generated,
    #   peakMemMb (float|None), peakCpuPct (float|None),
    #   executedAt (ISO-8601 string|None, client clock at finish),
    #   imageS3Key (str|None): S3 key of the cell's chart PNG. Stored only when
    #     it is this notebook's/cell's own key (notebook-outputs/<nid>/<cell>/
    #     <sha256>.png, _cell_fields.valid_image_key); anything else → None.
    # `image` (base64 bytes) is never persisted (returned as None). notebook_cell_versions
    # stays document-only (no runtime fields). Dataflow facts (reads, writes,
    # sources, staleness) are NOT cell fields: they live in the per-notebook DAG
    # document (notebook_dag_get / notebook_dag_update below).

    @abstractmethod
    def notebook_create(self, name: str, description: str = "",
                        tag: str = "Drafts", cells: list = None,
                        session_id: str = None,
                        dag_build: Optional[Callable[..., Optional[dict]]] = None) -> dict:
        """Create a new notebook. The integer `id` is DB-generated (the caller
        does NOT supply it). Returns the created notebook dict INCLUDING its new
        `id` (create-returns-id). session_id is the client's optional stable UUID.
        `cells` follow the cell dict shape documented above.

        dag_build: optional `build(prev) -> doc | None` run in the SAME transaction
        right after the cells are written (prev is None — a new notebook has no DAG
        row). The returned doc is stamped and stored exactly as notebook_dag_update
        does; the result dict then carries it under "dag". A failure writing the
        cells rolls back both. Exceptions from dag_build propagate (callers wrap
        it so a DAG failure never fails the save)."""
        ...

    @abstractmethod
    def notebook_update(self, notebook_id: str, *,
                        dag_build: Optional[Callable[..., Optional[dict]]] = None,
                        **kwargs) -> bool:
        """Update notebook fields. Returns True if a row was modified.
        `cells=` replaces all cells; each follows the cell dict shape above
        (runtime fields peakMemMb/peakCpuPct/executedAt included).

        dag_build: optional `build(prev) -> doc | None`, run ONLY when `cells` is
        present, in the SAME transaction after the cells are written (prev = the
        stored DAG doc or None). Stamped/stored with the notebook_dag_update rules.
        A failure writing the cells rolls back both. Exceptions from dag_build
        propagate (callers wrap it)."""
        ...

    @abstractmethod
    def notebook_dag_get(self, notebook_id) -> Optional[dict]:
        """Stored DAG doc (dict) or None (no row / unparseable / schema != 1). The
        doc's version/built_at/notebook_id always equal the row's."""
        ...

    @abstractmethod
    def notebook_dag_update(self, notebook_id, build: Callable[..., Optional[dict]], *,
                            with_cells: bool = False) -> Optional[dict]:
        """Atomic read-modify-write of a notebook's DAG doc under a per-notebook
        lock. build(prev) -> new doc or None (no change).

        with_cells=True: storage reads the notebook's cells (same row mapper as
        notebook_get) INSIDE the locked transaction and calls build(prev, cells),
        so a builder never combines a fresh prev with cells read before a
        concurrent save.

        Stamping and the change check (proxy/storage/_dag_doc.py::stamp_doc):
        graph_hash/runtime_hash (app.notebook.dag_contract) of the built doc are
        compared with the stored ones —
          * both equal → NOTHING is written (no SQL, updated_at untouched);
          * graph changed → version = (row.version if row else 0) + 1 (the first
            stored version is 1; version 0 exists only on the never-stored empty
            doc) and built_at = now (UTC ISO ms 'Z');
          * runtime-only change → version/built_at/trigger kept;
          * runtime_version = prev + 1 when the runtime changed (1 on first write).
        A written row stores the FULL doc in dag_json and sets updated_at (so the
        Aurora sync, which keys on updated_at, replicates runtime-only writes).
        A serialized doc larger than dag_contract.DAG_MAX_BYTES is NOT written
        (logged at error level, prev returned; nothing is truncated).

        Returns the stored doc (prev when nothing was written), or None if the
        notebook does not exist. Exceptions from build propagate (callers wrap)."""
        ...

    @abstractmethod
    def notebook_dag_write(self, notebook_id, build: Callable[..., Optional[dict]], *,
                           with_cells: bool = False):
        """notebook_dag_update, returning the whole outcome: a
        proxy.storage._dag_doc.DagWrite(prev, doc, wrote, graph_changed,
        runtime_changed, vm_changed, nbytes), or None if the notebook does not
        exist. build returning None → DagWrite(prev, prev, False, False, False,
        False, None). Same lock, same stamping rules."""
        ...

    @abstractmethod
    def notebook_id_for_session(self, session_id) -> Optional[int]:
        """The notebook id linked to a session: SELECT id FROM notebooks WHERE
        session_id = ? ORDER BY updated_at DESC, id DESC LIMIT 2 — the first row
        wins (debug log when two rows match). None if no row or session_id is
        falsy."""
        ...

    @abstractmethod
    def notebook_cell_image_keys(self, notebook_id) -> dict:
        """{cell_id (str): image_s3_key} for every cell of the notebook with a
        non-empty stored chart key (empty dict for an unknown notebook)."""
        ...

    @abstractmethod
    def notebook_cell_image_keys_all(self, notebook_id) -> dict:
        """{cell_id (str): [key, ...]} for every cell of the notebook that
        references at least one chart image: the cell's image_s3_key (the LAST
        image, legacy column) first, then the keys listed in the cell's
        `{"__outputs__":1,"items":[...]}` html envelope. Deduplicated; only keys
        valid for that notebook and cell (_cell_fields.valid_image_key). Read-only;
        the orphan diff and delete cleanup use it so every image of every cell is
        accounted for. Empty dict for an unknown notebook."""
        ...

    @abstractmethod
    def notebook_get(self, notebook_id: str) -> Optional[dict]:
        """Get a single notebook by ID. Returns None if not found. The
        notebook's `cells` use the cell dict shape documented above."""
        ...

    @abstractmethod
    def notebook_list(self) -> list[dict]:
        """List all notebooks, ordered by updated_at descending."""
        ...

    @abstractmethod
    def notebook_delete(self, notebook_id: str) -> bool:
        """Delete a notebook. Returns True if deleted. Also deletes its
        notebook_dag / notebook_env rows explicitly (besides the FK cascade) and
        its agent_sessions rows, all in the same transaction.

        Session cascade (ownership rule): the notebook's session ids are the union
        of notebooks.session_id (NULL after /terminate), vm_sessions.session_id,
        agent_sessions.session_id and notebook_runs.session_id for this notebook.
        A session id is OWNED unless another surviving notebook references it
        through any of those. For each owned id: delete its workbook_intel,
        local_file_entities and agent_sessions/agent_messages rows, and its
        session_registry row only when no non-TERMINATED vm_sessions row exists.
        Never deleted: run/cost history (notebook_runs, vm_sessions, vm_state_log,
        vm_metrics), the global data_source_entities catalog, pool_vms."""
        ...

    @abstractmethod
    def session_intel_delete(self, session_id: str) -> int:
        """Delete the workbook_intel and local_file_entities rows for session_id
        (their docs live under sessions/<sid>/ in S3, which DELETE /sessions/{id}
        removes). Returns the number of rows deleted; 0 for a falsy id. Never
        touches notebook, chat, run or registry rows."""
        ...

    @abstractmethod
    def notebook_owned_session_ids(self, notebook_id: str) -> list[str]:
        """The session ids notebook_delete would treat as OWNED by notebook_id:
        the same union of sources (notebooks / vm_sessions / agent_sessions /
        notebook_runs) minus any session another SURVIVING notebook references
        (the same _session_used_elsewhere rule). Sorted. Read-only. Call it
        BEFORE notebook_delete (agent_sessions is one of the sources and is
        deleted by it); the caller uses it to remove the owned sessions' S3
        checkpoint prefixes after the DB delete commits. [] for an unknown id."""
        ...

    @abstractmethod
    def notebook_latest_session_id(self, notebook_id: str) -> Optional[str]:
        """The session_id of this notebook's most recent VM (vm_sessions row with
        this notebook_id: a live row first, else the latest terminated_at, then
        launched_at; a pooled VM's launched_at is its mint time), or None. Skips a session another
        SURVIVING notebook references (the _session_used_elsewhere rule). Read-only.
        Used to re-link a notebook whose notebooks.session_id was cleared by
        /terminate (notebook_clear_session) so a reopen can still restore its
        checkpoint."""
        ...

    @abstractmethod
    def notebook_clear_session(self, session_id: str) -> bool:
        """Clear a notebook's live-session link (session_id + microvm_id → NULL),
        keyed by its session_id. Called when the notebook's VM is TERMINATED so the
        curated v_nb_notebook view (which exposes notebooks.session_id) stops
        reporting a closed/dead notebook as "open". Bumps updated_at so the change
        syncs to Aurora. Returns True if a row was modified, False if no notebook
        currently carries that session_id (no-op). NEVER called on SUSPEND — a
        suspended VM is still an open, recoverable session."""
        ...

    # ============================================================
    # NOTEBOOK RUNS (execution history — one row per run)
    # ============================================================

    @abstractmethod
    def notebook_run_create(self, run_id: str, notebook_id: str = None,
                            session_id: str = None, microvm_id: str = None,
                            trigger: str = "single",
                            started_at: str = None, finished_at: str = None,
                            cells_executed: int = 0, cells_errored: int = 0,
                            cells_interrupted: int = 0,
                            total_exec_ms: float = 0.0,
                            run_cells: list = None) -> dict:
        """Record one execution run. trigger is 'single' | 'run_all' | 'run_from'.
        microvm_id is the VM the run executed on (joins to vm_sessions/metrics).
        cells_interrupted counts cells the user stopped (distinct from errored).
        run_cells is the per-cell breakdown, stored as an opaque JSON blob for the
        drill-down. Each entry is {cell_id, label, type, status, exec_ms, ...} and
        may also carry a `params` map ({label: value}) of the @param values that
        cell declared, which the UI accumulates across the cells in scope.
        started_at/finished_at are normalized to ISO-8601 UTC before storing; an
        unparseable value is stored as NULL. Returns the created run dict (with
        the normalized timestamps).
        An existing run_id is updated in place: the original created_at is kept
        and every other column takes the new value (both backends)."""
        ...

    @abstractmethod
    def notebook_run_list(self, notebook_id: str, limit: int = 20,
                          trigger: str = None) -> list[dict]:
        """List a notebook's runs, most recent first. If trigger is given
        ('single'|'run_all'|'run_from'), return only runs of that trigger — so
        callers can get the last N of one kind (apples-to-apples). Runs with a
        NULL finished_at sort LAST. A negative limit is treated as 0 (no rows),
        identically in both backends. created_at/updated_at are ISO-8601 UTC
        strings; started_at/finished_at are stored ISO-8601 UTC (NULL when the
        client value was unparseable)."""
        ...

    @abstractmethod
    def notebook_run_list_for_vms(self, microvm_ids: list[str],
                                  limit: int = 200) -> list[dict]:
        """Runs executed on any of these VMs, newest first by
        COALESCE(started_at, finished_at), at most `limit`. Light rows (no
        run_cells): run_id, notebook_id, session_id, microvm_id, trigger,
        started_at, finished_at, cells_executed, cells_errored,
        cells_interrupted, total_exec_ms. [] for an empty list. NULL sort keys
        sort LAST; a negative limit is treated as 0."""
        ...

    # ============================================================
    # NOTEBOOK VERSIONS (document version history — code snapshots)
    #
    # Point-in-time snapshots of a notebook's CELL DOCUMENT (code + structure),
    # for "restore this version" and per-cell revert. This is DISTINCT from:
    #   - notebook_cells    : the live working copy (overwritten by autosave).
    #   - notebook_runs      : execution-event summaries (timings/status).
    #   - the S3/VM checkpoint: kernel RUNTIME state (namespace/packages), not code.
    #
    # A version is a HEADER row (notebook_versions) grouping a set of lean cell
    # snapshot rows (notebook_cell_versions) via version_group_id. We version the
    # DOCUMENT only — code, type, position, output_variable, ai_explanation
    # (+source), generated — NOT runtime outputs/dataflow (those regenerate on run).
    # Append-only; created only at deliberate checkpoints (manual save, a separate
    # auto-version interval, or before an AI edit), never on the per-keystroke
    # autosave. `trigger` is 'manual' | 'auto' | 'pre_ai' | 'pre_restore'.
    # ============================================================

    @abstractmethod
    def notebook_version_create(self, notebook_id: str, cells: list,
                                label: str = None, trigger: str = "manual",
                                version_group_id: str = None) -> dict:
        """Snapshot the given cells as a new version group (header + cell rows,
        written atomically). `cells` are the notebook's CURRENT cells (the caller
        reads them from the live store). Each cell dict may carry: id, type, code,
        outputVariable, aiExplanation, aiExplanationSource, generated (runtime
        fields are ignored). Returns the header dict
        {version_group_id, notebook_id, label, trigger, created_at, cell_count}."""
        ...

    @abstractmethod
    def notebook_version_list(self, notebook_id: str, limit: int = 50) -> list[dict]:
        """List a notebook's version headers, most recent first (no cell rows).
        A negative limit is treated as 0 (no rows)."""
        ...

    @abstractmethod
    def notebook_version_find_by_fingerprint(self, notebook_id: str,
                                             fp_hash: str) -> Optional[dict]:
        """Most-recent version header for this notebook whose stored
        fingerprint_hash matches, or None. Lets the route dedup a non-manual
        snapshot with one indexed lookup instead of re-reading every version's
        cells. Fingerprint is computed by proxy.storage.versioning.fingerprint_hash."""
        ...

    @abstractmethod
    def notebook_version_get(self, version_group_id: str) -> Optional[dict]:
        """Get one version: the header plus its `cells` (ordered by position, shaped
        so they can be handed straight to notebook_update(cells=...)). None if not
        found."""
        ...

    @abstractmethod
    def notebook_cell_versions(self, notebook_id: str, cell_id: str,
                               limit: int = 20) -> list[dict]:
        """Per-cell history: every snapshot of ONE cell across version groups, most
        recent first (joined to the header for created_at/label/trigger). Powers
        per-cell revert. Returns [{version_group_id, created_at, label, trigger,
        code, type, ...}]. A negative limit is treated as 0 (no rows)."""
        ...

    @abstractmethod
    def notebook_version_prune(self, notebook_id: str, keep: int = 50) -> int:
        """Prune old AUTO versions beyond `keep` (never delete manual/pre_ai/
        pre_restore). Returns the number of version groups removed. Cell rows
        cascade-delete with their header."""
        ...

    @abstractmethod
    def notebook_version_delete(self, notebook_id: str, version_group_id: str) -> bool:
        """Delete one version group (any trigger) and its cell rows. Scoped by
        notebook_id so a mismatched id can't delete another notebook's version.
        Returns True if a version was removed, False if not found."""
        ...

    # ============================================================
    # VM SESSIONS
    # ============================================================

    @abstractmethod
    def vm_session_create(self, microvm_id: str, notebook_id: str = None,
                          session_id: str = None, memory_mib: int = None,
                          endpoint: str = None, idle_timeout_sec: int = None,
                          max_duration_sec: int = None,
                          checkpoint_enabled: bool = False,
                          architecture: str = "arm64",
                          notebook_name: str = None,
                          launched_at: str = None,
                          prior_suspended_since: str = None) -> dict:
        """Record a new VM session on launch. Returns session dict.
        architecture (arm64/x86_64) selects pricing rates + same-arch restore.
        notebook_name is denormalized onto the row so session cost history stays
        attributable to a human name even after the notebook itself is deleted
        (notebook_delete preserves vm_sessions but drops the notebooks row).
        launched_at is the UTC ISO mint time; when None, defaults to now — pass the
        pool mint time for a pooled VM so lifetime/countdown reflect the real AWS
        clock (the 8h max-lifetime starts at mint, not at adoption).
        prior_suspended_since is the UTC ISO mint time a POOLED spare sat SUSPENDED
        in the warm pool before this grant. When given AND strictly older than now,
        the state-log is seeded with TWO rows (NULL->SUSPENDED at that mint time,
        then SUSPENDED->RUNNING at now) so v_vm_sessions reconstructs the
        pool-residency SUSPENDED interval (snapshot-storage cost the spare already
        incurred). When None (cold launch) — or unparseable / not strictly older
        than now — the EXISTING single NULL->RUNNING row at now is written
        (unchanged, no spurious zero-length suspended interval). The vm_sessions row
        itself is always state='RUNNING' with launched_at = the effective mint time;
        only the vm_state_log seeding changes.
        An existing microvm_id is updated in place (both backends): burst_mb_seconds
        and image_arn are kept, terminated_at is cleared to NULL, and every other
        column takes the new value. The state-log rows above are appended as for a
        new row."""
        ...

    @abstractmethod
    def vm_session_update_state(self, microvm_id: str, new_state: str) -> None:
        """Update VM state and log the transition. Atomic: the old state is read
        under a write/row lock, so concurrent callers produce exactly one
        vm_state_log row per real change and none when the state is unchanged
        or the session row does not exist."""
        ...

    @abstractmethod
    def vm_session_mark_terminated(self, microvm_id: str) -> bool:
        """Mark a VM session TERMINATED (state + terminated_at + a state-log
        transition), idempotently. Used when the proxy discovers a VM is dead
        (e.g. gone from the control plane on restart) so vm_session_list_active()
        stops returning it. Returns True if a row was updated, False if the session
        is unknown or already terminated (a no-op). Atomic like
        vm_session_update_state: of concurrent callers only one returns True and
        writes the state-log row."""
        ...

    @abstractmethod
    def vm_session_update_burst(self, microvm_id: str, burst_mb_seconds: float) -> None:
        """Persist a VM's accumulated burst_mb_seconds. This is the ONLY cost
        quantity stored on vm_sessions — running/suspended seconds are
        reconstructed from vm_state_log via v_vm_sessions, and dollar cost is
        computed in Python. Called periodically + on graceful shutdown so the
        live-accumulated burst isn't lost on handoff."""
        ...

    @abstractmethod
    def vm_session_get(self, microvm_id: str) -> Optional[dict]:
        """Get a VM session by microvm_id. Returns None if not found."""
        ...

    @abstractmethod
    def vm_session_list_active(self) -> list[dict]:
        """List all non-terminated VM sessions."""
        ...

    @abstractmethod
    def vm_session_list_for_session(self, session_id: str) -> list[dict]:
        """List ALL VM sessions (any state) that have served a given session_id —
        the original VM plus every rotation/reactive-recovery replacement — ordered
        by launched_at ascending. Used to show the full log history of a notebook
        across VM swaps (each VM has its own CloudWatch stream/group)."""
        ...

    @abstractmethod
    def vm_state_log_get(self, microvm_id: str) -> list[dict]:
        """Get state transition log for a VM. Returns list of {new_state, timestamp}."""
        ...

    @abstractmethod
    def vm_cost_quantities(self, microvm_id: str) -> Optional[dict]:
        """Read one VM's cost QUANTITIES from the v_vm_sessions view — the single
        source of truth (running/suspended seconds are reconstructed in SQL from
        vm_state_log). Returns {state, memory_mib, running_secs, suspended_secs,
        burst_mb_seconds} or None. Dollar cost is computed in Python (pricing is
        config, not schema)."""
        ...

    @abstractmethod
    def vm_cost_quantities_for_session(self, session_id: str) -> list[dict]:
        """Read cost quantities for EVERY VM that served ``session_id``.

        Includes terminated and rotation/recovery predecessor VMs — this is the
        durable server-side source for a notebook's cumulative session cost. Each
        row comes from v_vm_sessions and includes {microvm_id, state, memory_mib,
        architecture, running_secs, suspended_secs, burst_mb_seconds, launched_at,
        terminated_at}. Dollar values remain Python/config derived so pricing stays
        centralized in CostTracker.price_quantities()."""
        ...

    def vm_cost_quantities_many(self, microvm_ids: list[str]) -> dict[str, dict]:
        """Batch form of ``vm_cost_quantities``: the same per-VM dict, keyed by
        microvm_id. Ids with no vm_sessions row are absent from the result.

        Non-abstract so existing backends/fakes keep working; the SQL backends
        override it with ONE filter-first statement (per chunk) instead of N."""
        return {i: q for i in dict.fromkeys(microvm_ids or [])
                if (q := self.vm_cost_quantities(i)) is not None}

    # ============================================================
    # SESSION REGISTRY  (session_id → current VM)
    #
    # The authoritative "which VM serves this session?" mapping, shared across
    # all proxy instances. The MicrovmManager keeps an in-process L1 cache, but
    # the DB is the source of truth so a horizontally-scaled proxy fleet stays
    # consistent (instance B sees the VM that instance A just relinked to).
    # ============================================================

    @abstractmethod
    def session_registry_upsert(self, session_id: str, vm_id: str, endpoint: str) -> None:
        """Create or update the session → {vm_id, endpoint} mapping."""
        ...

    @abstractmethod
    def session_registry_get(self, session_id: str) -> Optional[dict]:
        """Return {vm_id, endpoint} for a session, or None if unknown."""
        ...

    @abstractmethod
    def session_registry_delete(self, session_id: str) -> None:
        """Remove a session's registry entry (VM terminated)."""
        ...

    @abstractmethod
    def session_registry_all(self) -> list[dict]:
        """Return every session mapping as [{session_id, vm_id, endpoint}, ...].

        Used by startup reconciliation to drop rows whose VM is no longer live
        (otherwise a proxy restart inherits orphan mappings that 502 forever).
        """
        ...

    # ============================================================
    # DISTRIBUTED SESSION LOCK  (recovery mutual-exclusion)
    #
    # Guarantees that at most ONE proxy instance recovers a given session at a
    # time (launch new VM + restore from S3 + relink), even when several
    # instances see the VM die concurrently. On Postgres this is a
    # pg_try_advisory_lock keyed by hash(session_id); on SQLite it is an
    # in-process lock (correct for single-instance/local dev).
    # ============================================================

    @abstractmethod
    def try_session_lock(self, session_id: str) -> bool:
        """Try to acquire the session's recovery lock WITHOUT blocking.
        Returns True if acquired (caller is the sole recoverer), False if another
        holder has it. Must be released with release_session_lock()."""
        ...

    @abstractmethod
    def release_session_lock(self, session_id: str) -> None:
        """Release a previously-acquired session recovery lock. Safe to call even
        if not held (no-op)."""
        ...

    # ============================================================
    # POOL VMS  (VM Pool Manager ledger)
    #
    # pool_vms is a DIFFERENT table from vm_sessions / session_registry, owned
    # and written by the separate-process VM Pool Manager (pool_manager/). It is
    # the pool's durable queue: the DB table IS the queue. The proxy only reads
    # it (pool_vm_list: /instances seeds spare costs, the VM detail card tells a
    # spare from a notebook VM); both backends implement the same ledger.
    # ============================================================

    @abstractmethod
    def pool_vm_create(self, vm_id: str, architecture: str, size_mib: int,
                       endpoint: str, fake_session_id: str,
                       launched_at: str = None, state: str = "launching") -> dict:
        """Insert a new pool_vms row (a freshly launched spare). launched_at
        defaults to now (UTC ISO) if not given. Returns the created row dict."""
        ...

    @abstractmethod
    def pool_vm_set_state(self, vm_id: str, state: str,
                          real_session_id=_UNSET) -> bool:
        """Update one pool_vms row's state (+ updated_at). real_session_id is
        UNTOUCHED unless explicitly passed (including passing None to clear
        it) — the _UNSET sentinel distinguishes 'don't touch' from 'set to
        NULL'. Returns True if a row was modified, False if vm_id is unknown."""
        ...

    @abstractmethod
    def pool_vm_claim_youngest(self, architecture: str, size_mib: int,
                               real_session_id: str) -> Optional[dict]:
        """Atomically claim the YOUNGEST (max launched_at) 'available' spare
        of (architecture, size_mib), flipping it to 'granting' and setting
        real_session_id, in one compare-and-swap (see design §4.1). Returns
        the claimed row dict, or None if no spare of that tier is available
        (a pool/buffer miss — rowcount 0). Never returns two different rows
        for concurrent calls contending on the same tier."""
        ...

    @abstractmethod
    def pool_vm_list(self, state: str = None, architecture: str = None,
                     size_mib: int = None) -> list[dict]:
        """List pool_vms rows, optionally filtered by any combination of
        state / architecture / size_mib. No filters -> all rows."""
        ...

    @abstractmethod
    def pool_vm_count_by_size(self, architecture: str, size_mib: int,
                              states: tuple = ("available", "launching")) -> int:
        """Count pool_vms rows for one (architecture, size_mib) tier whose
        state is in `states`. Used by the fill loop to compute the deficit
        against the configured target."""
        ...

    @abstractmethod
    def pool_vm_delete(self, vm_id: str) -> None:
        """Delete one pool_vms row (after terminate, on recycle, or on grant-
        flow failure cleanup). No-op if vm_id is unknown."""
        ...

    @abstractmethod
    def pool_vm_list_older_than(self, max_age_seconds: float,
                                state: str = "available",
                                since_column: str = "launched_at") -> list[dict]:
        """List pool_vms rows in the given state whose age exceeds
        max_age_seconds, measuring age from `since_column` (UTC-ISO parsed and
        compared to now). since_column must be "launched_at" or "updated_at":
          - "launched_at" (default): mint time — the age-recycle loop (§5.2).
          - "updated_at": last state-change time — the stale-'granting' check,
            so a warm spare that sat in the pool longer than the grant timeout
            isn't wrongly torn down the instant it is claimed."""
        ...

    @abstractmethod
    def pool_vm_backdate(self, vm_id: str, launched_at: str = None,
                         updated_at: str = None) -> bool:
        """Test seam: shifts pool_vms timestamps (ISO strings) for age-based
        logic tests; not used by production code. Only the arguments passed
        are changed. Returns True if a row was modified."""
        ...
    # ============================================================
    # WORKBOOK INTEL
    # ============================================================

    @abstractmethod
    def workbook_intel_save(self, session_id: str, s3_key: str) -> None:
        """Save or update workbook intel metadata (s3_key + bump version)."""
        ...

    @abstractmethod
    def notebook_env_get(self, notebook_id) -> dict:
        """Get a notebook's persisted env: {"env_vars": {...}, "secrets": [...]}.
        Returns empty defaults (never None) when nothing is stored."""
        ...

    @abstractmethod
    def notebook_env_set(self, notebook_id, env_vars: dict, secrets: list,
                         merge_env: bool = False,
                         remove_env_keys: list | None = None) -> None:
        """Replace (or MERGE) a notebook's persisted env. Stores secret references only.

        merge_env=False (default): whole-object replace of env_vars (the historic
        behaviour) — env_vars becomes the complete new set.
        merge_env=True: read the existing decrypted env_vars, drop any key listed in
        remove_env_keys, then overlay the passed env_vars ({**existing, **env_vars}),
        and seal+write the result — so a UI that only re-enters one key updates just
        that key and leaves the others intact. remove_env_keys is only honoured under
        merge_env=True. Secrets are ALWAYS a whole-object replace regardless of
        merge_env (they carry no value, so there is nothing to preserve)."""
        ...

    @abstractmethod
    def notebook_env_list(self) -> list:
        """List every notebook's persisted env, decrypted:
        [{"notebook_id": int, "env_vars": {...}, "secrets": [...]}]. For admin/
        inspection use — goes through the backend so it works on SQLite AND Aurora."""
        ...

    @abstractmethod
    def workbook_intel_get(self, session_id: str) -> Optional[dict]:
        """Get workbook intel metadata for a session, or None."""
        ...

    # ============================================================
    # METRICS
    # ============================================================

    @abstractmethod
    def metrics_record(self, microvm_id: str, cpu_pct: float, mem_pct: float,
                       mem_used_mb: float, disk_pct: float, disk_used_mb: float,
                       net_bytes_sent: int, net_bytes_recv: int,
                       processes: int, uptime_sec: float,
                       recorded_at: float = None) -> None:
        """Record a metrics snapshot for a VM. recorded_at (epoch seconds)
        overrides the sample time (tests/backfill); default None = now."""
        ...

    @abstractmethod
    def metrics_get_latest(self, microvm_id: str) -> Optional[dict]:
        """Get the most recent metrics snapshot for a VM."""
        ...

    @abstractmethod
    def metrics_get_history(self, microvm_id: str, minutes: int = 5) -> list[dict]:
        """Get metrics history for sparkline charts."""
        ...

    @abstractmethod
    def metrics_get_summary(self, microvm_id: str, start_epoch: float,
                            end_epoch: float) -> Optional[dict]:
        """Aggregate of one VM's samples in the window, or None when there are none.

        Window (identical in every backend): samples with
        floor(start_epoch) <= timestamp < floor(end_epoch) + 1 (whole UTC seconds).
        Returns {samples, first_at, last_at, cpu_avg, cpu_max, cpu_max_at,
        mem_used_avg, mem_used_max, mem_used_max_at, mem_pct_max, disk_pct_max,
        disk_pct_max_at, disk_used_max}. Times are epoch floats (3 decimals);
        *_at is the EARLIEST sample holding that peak."""
        ...

    @abstractmethod
    def metrics_get_bucketed(self, microvm_id: str, start_epoch: float,
                             end_epoch: float, bucket_sec: int) -> list[dict]:
        """Downsampled samples for history charts: one row per NON-EMPTY time
        bucket of width bucket_sec (>= 1) counted from floor(start_epoch),
        ascending by bucket. Same window as metrics_get_summary. Empty buckets
        are omitted (callers render them as gaps, never interpolate).
        Row: {bucket (int index), t_first, t_last (epoch floats), samples,
        cpu_avg, cpu_max, mem_used_avg, mem_used_max, mem_pct_max,
        disk_pct_max, disk_used_max}."""
        ...

    @abstractmethod
    def metrics_get_burst(self, microvm_id: str, baseline_mb: float) -> Optional[dict]:
        """Burst totals over one VM's whole stored sample history, by the cost
        tracker's rule (CostRecord.record_burst_sample). Only samples with
        mem_used_mb > 0 count (the others are ignored, also as the "previous"
        sample). None when the VM has no such sample. Else
        {samples, burst_samples, burst_seconds, burst_mb_seconds, burst_episodes,
        peak_mem_mb}: for each sample above baseline_mb that has a previous
        sample, add (t - t_prev) seconds and (used - baseline_mb) * (t - t_prev)
        MB-seconds (the first sample bills nothing); an episode is a sample
        above the baseline whose previous sample is missing or at/below it."""
        ...
    @abstractmethod
    def metrics_cleanup(self, hours: int = 24) -> None:
        """Delete metrics older than N hours."""
        ...

    # ============================================================
    # AGENT SESSIONS (Strands SessionManager backing store)
    #
    # Durable conversation history for the chat agent. Replaces the old
    # frontend-written ai_sessions blob. The agent (via Strands'
    # RepositorySessionManager) is the WRITER of record: it persists every
    # message as it's added and rehydrates the conversation on cold start.
    #
    # Two tables, accessed ONLY through these methods (never raw SQL upstream):
    #   agent_sessions  — one row per session_id (1:1 with a notebook): holds the
    #                     serialized Strands Session + SessionAgent dicts.
    #   agent_messages  — one row per (session_id, agent_id, message_id): the
    #                     serialized Strands SessionMessage dict. FK to
    #                     agent_sessions ON DELETE CASCADE.
    #
    # agent_id: Strands keys messages by (session_id, agent_id, message_id) to allow
    # multiple agents per session. Our model is 1:1 — one session = one chat agent —
    # so agent_id is pinned to a single stable value ("AI Assistant", see
    # constants.CHAT_AGENT_ID). The column is kept so multi-agent-per-session would
    # work later without a schema change.
    #
    # All payloads are plain dicts (the Strands types' to_dict()/from_dict()),
    # so the storage layer stays Strands-agnostic: it stores/returns dicts + JSON.
    # ============================================================

    @abstractmethod
    def agent_session_create(self, session_id: str, session: dict, notebook_id: str = None) -> None:
        """Create the session row (no-op/idempotent if it already exists).
        `session` is the serialized Strands Session dict."""
        ...

    @abstractmethod
    def agent_session_read(self, session_id: str) -> Optional[dict]:
        """Return the serialized Session dict, or None if the session doesn't exist."""
        ...

    @abstractmethod
    def agent_create(self, session_id: str, agent_id: str, agent: dict) -> None:
        """Create the agent row within a session. `agent` is the serialized
        SessionAgent dict. One agent per session in our 1:1 model."""
        ...

    @abstractmethod
    def agent_read(self, session_id: str, agent_id: str) -> Optional[dict]:
        """Return the serialized SessionAgent dict, or None if not found."""
        ...

    @abstractmethod
    def agent_update(self, session_id: str, agent_id: str, agent: dict) -> None:
        """Update (upsert) the serialized SessionAgent dict for a session's agent."""
        ...

    @abstractmethod
    def agent_message_create(self, session_id: str, agent_id: str, message_id: int,
                             message: dict, notebook_id: str = None) -> None:
        """Append a message. `message` is the serialized SessionMessage dict;
        `message_id` is its monotonically increasing index in the conversation."""
        ...

    @abstractmethod
    def agent_message_read(self, session_id: str, agent_id: str, message_id: int) -> Optional[dict]:
        """Return the serialized SessionMessage dict for one message, or None."""
        ...

    @abstractmethod
    def agent_message_update(self, session_id: str, agent_id: str, message_id: int, message: dict) -> None:
        """Update one message in place (used for redaction)."""
        ...

    @abstractmethod
    def agent_message_list(self, session_id: str, agent_id: str,
                           limit: Optional[int] = None, offset: int = 0) -> list[dict]:
        """List serialized SessionMessage dicts for an agent, ordered by message_id,
        with pagination (offset/limit). Offset is a MESSAGE COUNT from the start.
        limit=None means no limit; a negative limit is treated as 0 (no rows) and
        a negative offset as 0, identically in both backends."""
        ...

    @abstractmethod
    def agent_session_delete(self, session_id: str) -> None:
        """Delete a session and (via cascade) all of its messages. Used by
        'new conversation' / clear, and by notebook deletion."""
        ...

    @abstractmethod
    def agent_transcript_get(self, session_id: str) -> list:
        """Return the conversation as UI chat bubbles for rendering:
        [{"role": "user"|"assistant", "content": "<text>"}], with consecutive
        assistant text (across tool round-trips) MERGED into one bubble per turn
        and toolUse/toolResult blocks dropped. Empty list if no session.

        This is the single read path the frontend uses to load a transcript; the
        translation from Strands message format lives in the backend so both
        SQLite and Postgres return the identical UI shape."""
        ...

    # ============================================================
    # GLOBAL DATA SOURCE ENTITIES
    #
    # Discovery metadata for user-non-specific data sources (S3 files, Athena
    # tables, DynamoDB tables) — see proxy/notebook/ai/entity_discovery.py.
    # Keyed by source_id, not session_id: this is shared across all sessions.
    # ============================================================

    @abstractmethod
    def entity_upsert(self, source_id: str, source_type: str, doc_s3_key: str = None,
                      change_signal: dict = None, status: str = None) -> None:
        """Create or partially update a global entity's discovery metadata.
        Only fields explicitly passed (non-None) are changed on an existing row
        (source_type and last_discovered_at are always refreshed). A single
        atomic upsert: concurrent first-time calls for one source_id never raise
        a unique-key error."""
        ...

    @abstractmethod
    def entity_get(self, source_id: str) -> Optional[dict]:
        """Get discovery metadata for one entity. Returns None if never seen."""
        ...

    @abstractmethod
    def entity_list(self, source_type: str = None) -> list[dict]:
        """List all known entities, optionally filtered by source_type."""
        ...

    # ============================================================
    # LOCAL FILE ENTITIES
    #
    # Same purpose as the global entities above, but for uploaded /tmp files
    # — unique to one session, not shared across workbooks.
    # ============================================================

    @abstractmethod
    def local_entity_upsert(self, session_id: str, filepath: str, doc_s3_key: str = None,
                            change_signal: dict = None, status: str = None) -> None:
        """Create or partially update a local file's discovery metadata for one
        session. Same partial, atomic upsert semantics as entity_upsert."""
        ...

    @abstractmethod
    def local_entity_get(self, session_id: str, filepath: str) -> Optional[dict]:
        """Get discovery metadata for one local file in one session. Returns None if never seen."""
        ...

    @abstractmethod
    def local_entity_list(self, session_id: str) -> list[dict]:
        """List all known local file entities for one session."""
        ...
