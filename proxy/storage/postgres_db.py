"""
PostgreSQL implementation of the StorageBackend interface.

This is the multi-instance / cloud backend. It mirrors SqliteStorage method-for-
method (see the conformance test suite) but targets Postgres so a horizontally-
scaled proxy fleet shares one source of truth. Two capabilities matter for a
multi-instance fleet:

  * session_registry_*  — the DB-backed "which VM serves this session?" map,
                          read-through per request so instance B sees the VM
                          that instance A just relinked to (feature B1). SQLite
                          backs it with the same session_registry table, but
                          that file is local to one host.
  * try/release_session_lock — pg_try_advisory_lock so exactly one instance
                          recovers a dead session at a time (feature B2). SQLite
                          uses an in-process set: correct within one proxy
                          process only.

Dialect differences vs SQLite (all handled below):
  * placeholders:        ?              -> %s
  * now():               datetime('now') -> NOW()  (stored as timestamptz)
  * relative time:       datetime('now', '-N minutes') -> NOW() - INTERVAL '...'
  * autoincrement:       INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL
  * upsert:              INSERT OR REPLACE -> INSERT ... ON CONFLICT DO UPDATE
  * no PRAGMAs, no executescript (statements run individually)

psycopg2 is imported lazily in initialize() so this module imports cleanly even
when the driver isn't installed (the proxy image bakes VM-side deps only; the
driver is a host/proxy dependency). Selecting STORAGE_BACKEND=postgres without
psycopg2 raises a clear, actionable error instead of an import-time crash.
"""

import os
import re
import json
import uuid
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from contextlib import contextmanager

from proxy.storage.interface import StorageBackend, _UNSET
from proxy.storage._cell_fields import (
    OUTPUTS_ENVELOPE_PREFIX, SCHEMA_RESET_MESSAGE, as_float, cell_all_image_keys,
    echo_cells_with_valid_keys, valid_image_key,
)
from proxy.storage._dag_doc import load_stored_doc, prepare_write, unchanged_write
from proxy.storage.versioning import dedupe_cells_by_id, fingerprint_hash
from proxy.storage import _metrics_fields as _mf
from proxy.storage._timestamps import (
    ENTITY_TS, METRICS_TS, POOL_VM_TS, VM_SESSION_TS, iso_utc, iso_utc_cols,
)

logger = logging.getLogger(__name__)

# Thin alias of the shared helper (proxy/storage/_timestamps.py): timestamptz
# datetime (or an ISO string) -> the ISO-8601 UTC string SQLite also returns.
_iso = iso_utc


# Schema — Postgres dialect. Individual statements (no executescript).
# initialize() runs this BEFORE _SCHEMA_STATEMENTS: (notebook_cells exists,
# notebook_cells.image_s3_key exists) in the current schema.
_IMAGE_KEY_SCHEMA_CHECK_SQL = """
SELECT
    EXISTS (SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = 'notebook_cells'),
    EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'notebook_cells'
              AND column_name = 'image_s3_key')
"""

_SCHEMA_STATEMENTS = [
    """
    -- `id` is a DB-generated integer surrogate PK (IDENTITY) — the frontend no
    -- longer mints ids, so ids are globally unique across the shared cluster.
    -- session_id (a client UUID, stable for the notebook's lifetime) remains the
    -- durable external handle; `id` is the fast numeric join key for every FK
    -- below and the v_nb_* views.
    CREATE TABLE IF NOT EXISTS notebooks (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        tag TEXT DEFAULT 'Drafts',
        session_id TEXT,
        microvm_id TEXT,
        checkpoint_enabled INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    -- Notebook cells: one row per cell (replaces the old cells_json blob). Keyed
    -- by the cell's permanent id; `position` preserves order. base64 `image` is
    -- intentionally NOT stored; only the S3 LOCATION of the chart PNG is
    -- (image_s3_key, validated by _cell_fields.valid_image_key). No migration adds
    -- it: an older DB fails fast in initialize() (needs --AuroraReset).
    CREATE TABLE IF NOT EXISTS notebook_cells (
        notebook_id BIGINT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
        cell_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        type TEXT DEFAULT 'code',
        code TEXT DEFAULT '',
        output TEXT,
        error TEXT,
        html TEXT,
        output_variable TEXT,
        execution_number INTEGER,
        execution_time DOUBLE PRECISION,
        last_executed_code TEXT,
        ai_explanation TEXT,
        ai_explanation_edited INTEGER DEFAULT 0,
        ai_explanation_source TEXT,
        generated TEXT,
        peak_mem_mb DOUBLE PRECISION,
        peak_cpu_pct DOUBLE PRECISION,
        executed_at TEXT,  -- ISO-8601 client clock when the cell last finished
        image_s3_key TEXT,  -- S3 key of the cell's chart PNG (notebook-outputs/…); never bytes
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- sync watermark
        PRIMARY KEY (notebook_id, cell_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notebook_cells_pos ON notebook_cells(notebook_id, position)",
    """
    -- Per-notebook DAG document (proxy/notebook/dag.py). One row per notebook;
    -- cascade child of notebooks. version is stamped by storage (previous + 1)
    -- under a per-notebook row lock; dag_json is the whole document as compact
    -- JSON text (read by the v_nb_dag_* views via ::jsonb).
    CREATE TABLE IF NOT EXISTS notebook_dag (
        notebook_id BIGINT PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 0,
        built_at TEXT NOT NULL,
        dag_json TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notebook_runs (
        run_id TEXT PRIMARY KEY,
        notebook_id BIGINT,
        session_id TEXT,
        microvm_id TEXT,
        trigger TEXT DEFAULT 'single',
        started_at TEXT,
        finished_at TEXT,
        cells_executed INTEGER DEFAULT 0,
        cells_errored INTEGER DEFAULT 0,
        cells_interrupted INTEGER DEFAULT 0,
        total_exec_ms DOUBLE PRECISION DEFAULT 0,
        -- Per-cell breakdown: [{cell_id, label, type, status, exec_ms}] as a JSON
        -- blob (powers the per-cell drill-down in run history).
        -- Each entry may also carry the @param values that cell declared
        -- ({label: value}); the UI accumulates these across cells in scope.
        run_cells_json TEXT DEFAULT '[]',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notebook_runs_notebook
        ON notebook_runs(notebook_id, finished_at DESC)
    """,
    """
    -- Notebook Versions (document history): HEADER row per version group — a
    -- point-in-time snapshot of the notebook's cell DOCUMENT (code + structure).
    -- Distinct from notebook_runs (execution events) and the S3/VM checkpoint
    -- (kernel runtime). Append-only; created only at deliberate checkpoints
    -- (manual / auto interval / pre-AI), NOT on the per-keystroke autosave.
    CREATE TABLE IF NOT EXISTS notebook_versions (
        version_group_id TEXT PRIMARY KEY,
        notebook_id BIGINT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
        label TEXT,
        trigger TEXT DEFAULT 'manual',       -- 'manual' | 'auto' | 'pre_ai' | 'pre_restore'
        cell_count INTEGER DEFAULT 0,
        fingerprint_hash TEXT,               -- hash of the versioned document (dedup non-manual snapshots)
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notebook_versions_notebook
        ON notebook_versions(notebook_id, created_at DESC)
    """,
    """
    -- Additive migration for DBs created before fingerprint_hash existed
    -- (CREATE TABLE IF NOT EXISTS won't add the column to an existing table).
    ALTER TABLE notebook_versions ADD COLUMN IF NOT EXISTS fingerprint_hash TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notebook_versions_fp
        ON notebook_versions(notebook_id, fingerprint_hash)
    """,
    """
    -- Notebook Cell Versions: lean per-cell snapshot rows for a version group.
    -- DOCUMENT-ONLY subset of notebook_cells (no runtime output/exec/dataflow).
    -- created_at is NOT duplicated here — per-cell history joins to the header.
    CREATE TABLE IF NOT EXISTS notebook_cell_versions (
        version_group_id TEXT NOT NULL REFERENCES notebook_versions(version_group_id) ON DELETE CASCADE,
        cell_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        type TEXT DEFAULT 'code',
        code TEXT DEFAULT '',
        output_variable TEXT,
        ai_explanation TEXT,
        ai_explanation_source TEXT,
        generated TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- sync watermark
        PRIMARY KEY (version_group_id, cell_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notebook_cell_versions_cell
        ON notebook_cell_versions(cell_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS vm_sessions (
        microvm_id TEXT PRIMARY KEY,
        notebook_id BIGINT,
        notebook_name TEXT,                 -- denormalized notebook name at launch, so
                                            -- cost history stays attributable after the
                                            -- notebooks row is deleted (notebook_delete
                                            -- preserves vm_sessions).
        session_id TEXT,
        memory_mib INTEGER,
        architecture TEXT DEFAULT 'arm64',  -- CPU arch (arm64/x86_64); selects pricing + restore image
        state TEXT DEFAULT 'PENDING',
        endpoint TEXT,
        image_arn TEXT,
        launched_at TIMESTAMPTZ,
        terminated_at TIMESTAMPTZ,
        idle_timeout_sec INTEGER,
        max_duration_sec INTEGER,
        checkpoint_enabled INTEGER DEFAULT 0,
        burst_mb_seconds DOUBLE PRECISION DEFAULT 0.0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
        -- running_secs / suspended_secs / total_cost_usd intentionally REMOVED:
        -- derived aggregates now reconstructed on read from vm_state_log via
        -- v_vm_sessions; dollar cost computed in Python. burst_mb_seconds stays
        -- (the only cost quantity not derivable from the state log).
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vm_metrics (
        id BIGSERIAL PRIMARY KEY,
        microvm_id TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        cpu_pct DOUBLE PRECISION,
        mem_pct DOUBLE PRECISION,
        mem_used_mb DOUBLE PRECISION,
        disk_pct DOUBLE PRECISION,
        disk_used_mb DOUBLE PRECISION,
        net_bytes_sent BIGINT,
        net_bytes_recv BIGINT,
        processes INTEGER,
        uptime_sec DOUBLE PRECISION,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vm_state_log (
        id BIGSERIAL PRIMARY KEY,
        microvm_id TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        old_state TEXT,
        new_state TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
    )
    """,
    """
    -- Agent sessions: durable chat-agent conversation history (Strands
    -- SessionManager backing store; replaces the old frontend-written ai_sessions
    -- blob). One row per session_id (1:1 with a notebook). session_json/agent_json
    -- hold the serialized Strands Session / SessionAgent dicts.
    CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id TEXT PRIMARY KEY,
        notebook_id BIGINT,
        agent_id TEXT,                       -- pinned to "AI Assistant" (1:1 model)
        session_json TEXT DEFAULT '{}',      -- serialized Strands Session dict
        agent_json TEXT DEFAULT '{}',        -- serialized Strands SessionAgent dict
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
    )
    """,
    """
    -- Agent messages: one row per (session_id, agent_id, message_id). message_json
    -- is the serialized Strands SessionMessage dict. Cascade-child of agent_sessions
    -- (removed via ON DELETE CASCADE; no delete tombstone, like notebook_cells).
    CREATE TABLE IF NOT EXISTS agent_messages (
        session_id TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        notebook_id BIGINT,
        message_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- sync watermark
        PRIMARY KEY (session_id, agent_id, message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, agent_id, message_id)",
    """
    CREATE TABLE IF NOT EXISTS workbook_intel (
        session_id TEXT PRIMARY KEY,
        s3_key TEXT NOT NULL,
        generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        version INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    -- Persisted per-notebook env vars + secret references (see sqlite_db.py for the
    -- full rationale). Stores secret ARNs, never resolved values. Cascade-deleted
    -- with the parent notebook.
    -- notebook_id is BIGINT to match notebooks.id. This only applies to NEW
    -- databases: CREATE TABLE IF NOT EXISTS leaves an existing (INTEGER) table
    -- as it is; changing that needs an explicit, approved migration.
    CREATE TABLE IF NOT EXISTS notebook_env (
        notebook_id BIGINT PRIMARY KEY,
        env_json TEXT NOT NULL DEFAULT '{}',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- sync watermark
        FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_source_entities (
        source_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        doc_s3_key TEXT,
        change_signal TEXT,
        status TEXT DEFAULT 'pending',
        last_discovered_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- sync watermark
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_file_entities (
        session_id TEXT NOT NULL,
        filepath TEXT NOT NULL,
        doc_s3_key TEXT,
        change_signal TEXT,
        status TEXT DEFAULT 'pending',
        last_discovered_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- sync watermark
        PRIMARY KEY (session_id, filepath)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_registry (
        session_id TEXT PRIMARY KEY,
        vm_id TEXT NOT NULL,
        endpoint TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    -- Pool VMs: the VM Pool Manager's durable ledger of pre-booted spares.
    -- Owned by the separate-process pool manager (pool_manager/) — the proxy
    -- only reads this table.
    -- state is one of 'launching'|'available'|'granting'|'granted'|'dead'.
    -- launched_at is the UTC clock both the 8h AWS lifetime and the
    -- age-recycle loop (§5.2) key off.
    CREATE TABLE IF NOT EXISTS pool_vms (
        vm_id            TEXT PRIMARY KEY,
        architecture     TEXT NOT NULL,
        size_mib         INTEGER NOT NULL,
        endpoint         TEXT NOT NULL,
        fake_session_id  TEXT NOT NULL,
        state            TEXT NOT NULL DEFAULT 'launching',
        real_session_id  TEXT,
        launched_at      TIMESTAMPTZ NOT NULL,
        updated_at       TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pool_vms_pick
        ON pool_vms(architecture, size_mib, state, launched_at)
    """,
    "CREATE INDEX IF NOT EXISTS idx_vm_metrics_lookup ON vm_metrics(microvm_id, timestamp DESC)",
    # Drives metrics_cleanup's global `timestamp < cutoff` delete (the lookup index
    # above leads with microvm_id, so it can't serve it). Same index as SQLite.
    "CREATE INDEX IF NOT EXISTS idx_vm_metrics_ts ON vm_metrics(timestamp)",
    # Drives v_vm_sessions' per-VM interval reconstruction (PARTITION BY microvm_id
    # ORDER BY id in the LEAD window over the transition log).
    "CREATE INDEX IF NOT EXISTS idx_vm_state_log_vm ON vm_state_log(microvm_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_vm_sessions_notebook ON vm_sessions(notebook_id)",
    # Per-session cost rollup: get_session_cost() sums every VM that carried a
    # given session_id (original + all recovery/rotation replacements).
    "CREATE INDEX IF NOT EXISTS idx_vm_sessions_session ON vm_sessions(session_id)",
    # Partial index for the "active VMs" filter (vm_session_list_active + the UI's
    # non-terminated cost view). PARTIAL (only non-terminated rows) so it stays
    # small even as terminated rows accumulate — most rows become TERMINATED over
    # time, and we only ever query the minority.
    "CREATE INDEX IF NOT EXISTS idx_vm_sessions_active ON vm_sessions(state) WHERE state != 'TERMINATED'",
    # ── Additive `updated_at` migrations (sync watermark) ────────────────────
    # CREATE TABLE IF NOT EXISTS won't add the column to an already-provisioned
    # Aurora, so add it explicitly (idempotent). notebooks/agent_sessions/
    # session_registry already have updated_at; workbook_intel uses generated_at.
    "ALTER TABLE notebook_cells ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE notebook_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE notebook_versions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE notebook_cell_versions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE vm_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE vm_metrics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE vm_state_log ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE data_source_entities ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE local_file_entities ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    # v_vm_sessions: single-source-of-truth read view. running_secs/suspended_secs
    # are RECONSTRUCTED from vm_state_log (summing each state interval between
    # consecutive transitions; the last non-TERMINATED row is measured to now()),
    # centralizing the interval-sum that used to live only in Python
    # (CostTracker.compute). Exposes objective QUANTITIES only — dollar cost stays
    # in Python (pricing rates are config, not schema). Postgres dialect: EXTRACT
    # (EPOCH FROM interval) since timestamps are timestamptz (SQLite uses julianday).
    """
    CREATE OR REPLACE VIEW v_vm_sessions AS
    WITH intervals AS (
        SELECT
            l.microvm_id,
            l.new_state AS state,
            l.timestamp AS start_ts,
            LEAD(l.timestamp) OVER (PARTITION BY l.microvm_id ORDER BY l.id) AS next_ts
        FROM vm_state_log l
    ),
    durations AS (
        SELECT
            microvm_id,
            state,
            EXTRACT(EPOCH FROM (
                COALESCE(next_ts, CASE WHEN state = 'TERMINATED' THEN start_ts ELSE NOW() END)
                - start_ts
            )) AS secs
        FROM intervals
    ),
    agg AS (
        SELECT
            microvm_id,
            COALESCE(SUM(CASE WHEN state = 'RUNNING'   THEN secs END), 0) AS running_secs,
            COALESCE(SUM(CASE WHEN state = 'SUSPENDED' THEN secs END), 0) AS suspended_secs,
            COALESCE(SUM(CASE WHEN state = 'RUNNING'   THEN 1 ELSE 0 END), 0) AS resume_count,
            COALESCE(SUM(CASE WHEN state = 'SUSPENDED' THEN 1 ELSE 0 END), 0) AS suspend_count
        FROM durations
        GROUP BY microvm_id
    )
    SELECT
        s.microvm_id,
        s.notebook_id,
        s.session_id,
        s.memory_mib,
        s.state,
        s.endpoint,
        s.launched_at,
        s.terminated_at,
        s.checkpoint_enabled,
        s.architecture,
        s.burst_mb_seconds,
        ROUND(COALESCE(a.running_secs, 0))   AS running_secs,
        ROUND(COALESCE(a.suspended_secs, 0)) AS suspended_secs,
        COALESCE(a.resume_count, 0)  AS resume_count,
        COALESCE(a.suspend_count, 0) AS suspend_count
    FROM vm_sessions s
    LEFT JOIN agg a ON a.microvm_id = s.microvm_id
    """,
    # ========================================================================
    # CURATED READ-ONLY VIEWS (v_nb_*) — the notebook-facing contract.
    # ========================================================================
    # The ONLY objects exposed to the read-only notebook user (AI agent + SQL
    # cells query these, never the base tables). Identical COLUMN CONTRACT to the
    # SQLite definitions in sqlite_db.py — only the bodies differ by dialect
    # (jsonb_array_elements vs json_each; EXTRACT(EPOCH) vs julianday). They
    # normalize units (runs carry ms AND derived seconds) and EXCLUDE heavy/
    # sensitive bodies (code, output, error, html). GLOBAL (all notebooks); the
    # agent filters by session_id / notebook name. Change BOTH backends together.
    # NOTE: started_at/finished_at and run_cells_json are TEXT even on Postgres
    # (ISO strings / JSON text), so we CAST them (timestamptz / jsonb) here.
    # CREATE OR REPLACE VIEW (not DROP + CREATE) is deliberate: it re-applies the
    # definition on every proxy start / setup_aurora_postgres.sh run AND keeps the
    # read-only role's per-view SELECT grants (a DROP would silently revoke them
    # until the next setup run). Postgres only allows it when existing columns keep
    # their name/type/order, so NEW view columns must be APPENDED at the end.
    # v_nb_dag_*: JSON-array columns (*_json) are jsonb text here and compact JSON
    # text in SQLite — same names/order, compare parsed values.
    """
    CREATE OR REPLACE VIEW v_nb_notebook AS
    SELECT
        n.id AS notebook_id,
        n.name,
        n.tag,
        n.description,
        n.session_id,
        n.microvm_id,
        n.created_at,
        n.updated_at,
        (SELECT COUNT(*) FROM notebook_cells c WHERE c.notebook_id = n.id) AS cell_count,
        (SELECT COUNT(*) FROM notebook_runs r WHERE r.notebook_id = n.id) AS total_runs,
        (SELECT MAX(r.finished_at) FROM notebook_runs r WHERE r.notebook_id = n.id) AS last_run_at
    FROM notebooks n
    """,
    """
    CREATE OR REPLACE VIEW v_nb_cells AS
    SELECT
        c.notebook_id,
        c.cell_id,
        c.position,
        c.type,
        c.execution_number,
        c.execution_time AS execution_time_sec,
        CASE WHEN c.generated IS NOT NULL AND c.generated NOT IN ('', 'false', '0') THEN 1 ELSE 0 END AS is_generated,
        CASE WHEN c.error IS NOT NULL AND c.error <> '' THEN 1 ELSE 0 END AS has_error,
        c.ai_explanation_source,
        c.peak_mem_mb,
        c.peak_cpu_pct,
        c.executed_at,
        CASE WHEN c.image_s3_key IS NOT NULL AND c.image_s3_key <> '' THEN 1 ELSE 0 END AS has_image
    FROM notebook_cells c
    """,
    """
    CREATE OR REPLACE VIEW v_nb_runs AS
    SELECT
        r.run_id,
        r.notebook_id,
        r.session_id,
        r.microvm_id,
        r.trigger,
        r.started_at,
        r.finished_at,
        CASE
            -- Cast only ISO-shaped text: one malformed client string must not
            -- make every v_nb_runs query fail (SQLite's julianday() gives NULL).
            WHEN r.started_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}'
             AND r.finished_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}'
            THEN ROUND(EXTRACT(EPOCH FROM (r.finished_at::timestamptz - r.started_at::timestamptz))::numeric, 3)
            ELSE NULL
        END AS duration_sec,
        r.cells_executed,
        r.cells_errored,
        r.cells_interrupted,
        r.total_exec_ms,
        ROUND((r.total_exec_ms / 1000.0)::numeric, 3) AS total_exec_sec,
        CASE
            WHEN r.cells_errored > 0 THEN 'error'
            WHEN r.cells_interrupted > 0 THEN 'interrupted'
            ELSE 'ok'
        END AS status,
        r.created_at
    FROM notebook_runs r
    """,
    """
    CREATE OR REPLACE VIEW v_nb_cell_runs AS
    SELECT
        r.run_id,
        r.notebook_id,
        r.session_id,
        j.value ->> 'cell_id' AS cell_id,
        j.value ->> 'label'   AS label,
        j.value ->> 'type'    AS type,
        j.value ->> 'status'  AS status,
        (j.value ->> 'exec_ms')::double precision AS exec_ms,
        ROUND(((j.value ->> 'exec_ms')::double precision / 1000.0)::numeric, 3) AS exec_sec,
        -- Per-cell client-clock execution span (ISO strings) for the timeline, and
        -- the guest-sampled peak resource use. All read from the run_cells_json blob;
        -- NULL for runs recorded before these fields were captured.
        j.value ->> 'started_at'  AS cell_started_at,
        j.value ->> 'finished_at' AS cell_finished_at,
        (j.value ->> 'peak_mem_mb')::double precision  AS peak_mem_mb,
        (j.value ->> 'peak_cpu_pct')::double precision AS peak_cpu_pct,
        r.finished_at
    FROM notebook_runs r
    CROSS JOIN LATERAL jsonb_array_elements(r.run_cells_json::jsonb) AS j(value)
    """,
    """
    CREATE OR REPLACE VIEW v_nb_versions AS
    SELECT
        v.version_group_id,
        v.notebook_id,
        v.label,
        v.trigger,
        CASE WHEN v.trigger = 'baseline' THEN 1 ELSE 0 END AS is_baseline,
        v.cell_count,
        v.created_at
    FROM notebook_versions v
    """,
    """
    CREATE OR REPLACE VIEW v_nb_version_cells AS
    SELECT
        v.version_group_id,
        v.notebook_id,
        cv.cell_id,
        cv.position,
        cv.type,
        cv.output_variable,
        CASE WHEN cv.generated IS NOT NULL AND cv.generated NOT IN ('', 'false', '0') THEN 1 ELSE 0 END AS is_generated,
        v.created_at
    FROM notebook_cell_versions cv
    JOIN notebook_versions v ON v.version_group_id = cv.version_group_id
    """,
    # v_nb_session_cost: per-SESSION VM cost QUANTITIES (NO dollars — pricing rates
    # are Python/config, applied by the notebook's estimate_session_cost() helper).
    # Aggregates across every VM that served the session (rotation/recovery), with
    # running/suspended reconstructed from vm_state_log exactly as v_vm_sessions.
    # Postgres dialect: EXTRACT(EPOCH FROM interval); timestamps are timestamptz.
    """
    CREATE OR REPLACE VIEW v_nb_session_cost AS
    WITH intervals AS (
        SELECT
            l.microvm_id,
            l.new_state AS state,
            l.timestamp AS start_ts,
            LEAD(l.timestamp) OVER (PARTITION BY l.microvm_id ORDER BY l.id) AS next_ts
        FROM vm_state_log l
    ),
    durations AS (
        SELECT
            microvm_id,
            state,
            EXTRACT(EPOCH FROM (
                COALESCE(next_ts, CASE WHEN state = 'TERMINATED' THEN start_ts ELSE NOW() END)
                - start_ts
            )) AS secs
        FROM intervals
    ),
    per_vm AS (
        SELECT
            microvm_id,
            COALESCE(SUM(CASE WHEN state = 'RUNNING'   THEN secs END), 0) AS running_secs,
            COALESCE(SUM(CASE WHEN state = 'SUSPENDED' THEN secs END), 0) AS suspended_secs,
            COALESCE(SUM(CASE WHEN state = 'RUNNING'   THEN 1 ELSE 0 END), 0) AS resume_count,
            COALESCE(SUM(CASE WHEN state = 'SUSPENDED' THEN 1 ELSE 0 END), 0) AS suspend_count
        FROM durations
        GROUP BY microvm_id
    )
    SELECT
        s.session_id,
        MAX(s.notebook_id) AS notebook_id,
        -- Denormalized name (survives notebook deletion). MAX() picks a non-NULL
        -- name if any VM in the session carried one; NULL for pre-migration rows.
        MAX(s.notebook_name) AS notebook_name,
        COUNT(*) AS vm_count,
        ROUND(COALESCE(SUM(p.running_secs), 0))   AS running_secs,
        ROUND(COALESCE(SUM(p.suspended_secs), 0)) AS suspended_secs,
        ROUND(COALESCE(SUM(s.burst_mb_seconds), 0)::numeric, 1) AS burst_mb_seconds,
        COALESCE(SUM(p.resume_count), 0)  AS resume_count,
        COALESCE(SUM(p.suspend_count), 0) AS suspend_count,
        MAX(s.memory_mib) AS memory_mib,
        MAX(s.architecture) AS architecture
    FROM vm_sessions s
    LEFT JOIN per_vm p ON p.microvm_id = s.microvm_id
    WHERE s.session_id IS NOT NULL
    GROUP BY s.session_id
    """,
    """
    CREATE OR REPLACE VIEW v_nb_dag_nodes AS
    SELECT d.notebook_id,
           n.value->>'cell_id'                AS cell_id,
           (n.value->>'position')::integer    AS position,
           (n.value->>'level')::integer       AS level,
           n.value->>'status'                 AS status,
           n.value->>'facts_origin'           AS facts_origin,
           (n.value->'reads')::text           AS reads_json,
           (n.value->'writes')::text          AS writes_json,
           (n.value->'mutates')::text         AS mutates_json,
           CASE WHEN jsonb_typeof(n.value->'sources') = 'array'
                THEN (n.value->'sources')::text END AS sources_json,
           n.value->>'cell_type'              AS cell_type,
           n.value->>'executed_at'            AS executed_at,
           (n.value->>'last_run_seq')::bigint AS last_run_seq,
           n.value->'stale_reason'->>'kind'   AS stale_kind,
           d.version AS dag_version, d.built_at
    FROM notebook_dag d
    CROSS JOIN LATERAL jsonb_array_elements(d.dag_json::jsonb -> 'nodes') AS n(value)
    """,
    """
    CREATE OR REPLACE VIEW v_nb_dag_edges AS
    SELECT d.notebook_id,
           e.value->>'from' AS from_cell_id, e.value->>'to' AS to_cell_id,
           e.value->>'var' AS var_name, e.value->>'kind' AS kind, e.value->>'origin' AS origin,
           e.value->>'use' AS use_kind,
           CASE WHEN (e.value->>'backward')::boolean THEN 1 ELSE 0 END AS is_backward,
           d.version AS dag_version
    FROM notebook_dag d
    CROSS JOIN LATERAL jsonb_array_elements(d.dag_json::jsonb -> 'edges') AS e(value)
    """,
    """
    CREATE OR REPLACE VIEW v_nb_dag_issues AS
    SELECT d.notebook_id,
           i.value->>'cell_id' AS cell_id, i.value->>'code' AS code,
           i.value->>'severity' AS severity, i.value->>'var' AS var_name,
           i.value->>'detail' AS detail, (i.value->'related')::text AS related_json,
           i.value->'source'->>'kind' AS source_kind, i.value->'source'->>'id' AS source_id,
           d.version AS dag_version
    FROM notebook_dag d
    CROSS JOIN LATERAL jsonb_array_elements(d.dag_json::jsonb -> 'issues') AS i(value)
    """,
    # v_nb_dag_lineage: all (transitive) upstream cells per cell. UNION (not UNION
    # ALL) de-duplicates the finite pairs, so the recursion terminates on cycles.
    """
    CREATE OR REPLACE VIEW v_nb_dag_lineage AS
    WITH RECURSIVE e(notebook_id, from_id, to_id) AS (
        SELECT DISTINCT d.notebook_id, x.value->>'from', x.value->>'to'
        FROM notebook_dag d
        CROSS JOIN LATERAL jsonb_array_elements(d.dag_json::jsonb -> 'edges') AS x(value)
    ),
    up(notebook_id, cell_id, upstream_cell_id) AS (
        SELECT notebook_id, to_id, from_id FROM e
        UNION
        SELECT up.notebook_id, up.cell_id, e.from_id
        FROM up JOIN e ON e.notebook_id = up.notebook_id AND e.to_id = up.upstream_cell_id
    )
    SELECT notebook_id, cell_id, upstream_cell_id FROM up
    """,
]


def _schema_stmt(marker: str) -> str:
    """The one _SCHEMA_STATEMENTS entry containing `marker`. Raises at import if
    there is not exactly one."""
    found = [s for s in _SCHEMA_STATEMENTS if marker in s]
    if len(found) != 1:
        raise RuntimeError(f"expected exactly one schema statement with {marker!r}, "
                           f"found {len(found)}")
    return found[0]


# The subset of _SCHEMA_STATEMENTS the standalone pool_manager needs
# (initialize_pool_manager_schema): its pool_vms ledger + pick index, and the
# session_registry / vm_sessions tables it reads. Picked from the list rather than
# copied, so the DDL cannot drift. All CREATE ... IF NOT EXISTS: never drops or
# alters a table.
_POOL_MANAGER_SCHEMA_STATEMENTS = [
    _schema_stmt("CREATE TABLE IF NOT EXISTS vm_sessions ("),
    _schema_stmt("CREATE TABLE IF NOT EXISTS session_registry ("),
    _schema_stmt("CREATE TABLE IF NOT EXISTS pool_vms ("),
    _schema_stmt("CREATE INDEX IF NOT EXISTS idx_pool_vms_pick"),
]


# Public schema accessors for tooling (scripts/setup_aurora_postgres.sh). Names
# are DERIVED from _SCHEMA_STATEMENTS, so a new table or view is picked up
# without editing a second list. Importing this module does not need psycopg2.
_VIEW_NAME_RE = re.compile(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(v_\w+)", re.IGNORECASE)
_TABLE_NAME_RE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE)


def schema_statements() -> list[str]:
    """A copy of the Postgres DDL statements initialize() runs, in order."""
    return list(_SCHEMA_STATEMENTS)


def _schema_names(pattern) -> list[str]:
    """Names matched by `pattern` across the DDL, SQL '--' comments stripped (a
    comment may mention 'CREATE TABLE IF NOT EXISTS'), in DDL order."""
    return [m.group(1) for s in _SCHEMA_STATEMENTS
            for m in pattern.finditer(re.sub(r"--[^\n]*", "", s))]


def view_names() -> list[str]:
    """Every view the schema creates (CREATE OR REPLACE VIEW v_*), in DDL order."""
    return _schema_names(_VIEW_NAME_RE)


def table_names() -> list[str]:
    """Every table the schema creates (CREATE TABLE IF NOT EXISTS), in DDL order."""
    return _schema_names(_TABLE_NAME_RE)


class PostgresStorage(StorageBackend):
    """PostgreSQL-based storage backend for multi-instance / cloud deployment."""

    def __init__(self, connection_string: str = ""):
        self._dsn = connection_string
        self._pool = None
        # Advisory locks are connection-scoped in Postgres: the SAME backend
        # connection must hold and later release the lock. We therefore pull a
        # dedicated connection out of the pool for each held session lock and
        # keep it here until release. Guarded because recovery can be triggered
        # from multiple threads.
        self._lock_conns: dict[str, object] = {}
        self._lock_guard = threading.Lock()
        if connection_string:
            self.initialize(connection_string)

    # ============================================================
    # LIFECYCLE
    # ============================================================

    def initialize(self, connection_string: str = "") -> None:
        self._open_pool(connection_string)

        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                # Fail fast (not a migration) BEFORE any DDL: an existing
                # notebook_cells without image_s3_key is a pre-chart-persistence DB
                # (CREATE OR REPLACE VIEW v_nb_cells would fail opaquely otherwise).
                cur.execute(_IMAGE_KEY_SCHEMA_CHECK_SQL)
                has_table, has_col = cur.fetchone()
                if has_table and not has_col:
                    raise RuntimeError(SCHEMA_RESET_MESSAGE)
                for stmt in _SCHEMA_STATEMENTS:
                    cur.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._putconn(conn)
        logger.info("Postgres storage initialized")

    def initialize_pool_manager_schema(self) -> None:
        """Create the pool_manager's tables if missing (see StorageBackend). Runs
        only _POOL_MANAGER_SCHEMA_STATEMENTS, in one transaction."""
        if self._pool is None:
            self._open_pool(self._dsn)
        with self._db() as (_conn, cur):
            for stmt in _POOL_MANAGER_SCHEMA_STATEMENTS:
                cur.execute(stmt)
        logger.info("Postgres pool_manager schema ensured")

    def _open_pool(self, connection_string: str = "") -> None:
        """Import psycopg2 and open the connection pool (no DDL)."""
        try:
            import psycopg2  # noqa: F401
            import psycopg2.pool
            import psycopg2.extensions
            import psycopg2.extras  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "PostgresStorage requires psycopg2. Install it in the proxy "
                "environment: pip install 'psycopg2-binary>=2.9'"
            ) from e

        dsn = connection_string or self._dsn
        if not dsn:
            raise ValueError(
                "PostgresStorage requires a connection string "
                "(STORAGE_CONNECTION=postgresql://user:pass@host:5432/db)"
            )
        self._dsn = dsn
        # Size the pool to the proxy's to_thread executor (every storage call runs
        # on one of those threads) + headroom for held advisory-lock connections,
        # intel worker threads and the entity pool. Same default as
        # proxy/server.py's executor sizing. POSTGRES_POOL_MAXCONN overrides.
        executor_default = max(16, (os.cpu_count() or 4) * 4)
        try:
            executor = int(os.environ.get("PROXY_THREAD_POOL_SIZE") or executor_default)
        except ValueError:
            executor = executor_default
        try:
            maxconn = int(os.environ.get("POSTGRES_POOL_MAXCONN") or executor + 8)
        except ValueError:
            maxconn = executor + 8
        maxconn = max(1, maxconn)
        try:
            acquire_timeout = float(os.environ.get("POSTGRES_POOL_ACQUIRE_TIMEOUT") or 30)
        except ValueError:
            acquire_timeout = 30.0
        # ThreadedConnectionPool: safe to share across the proxy's worker threads.
        # It RAISES PoolError when exhausted, so a BoundedSemaphore with the same
        # capacity gates acquisition: callers block (up to acquire_timeout) for a
        # free slot instead of failing under a burst.
        # Pin the session TimeZone to UTC so timestamptz values (and any server-side
        # text rendering) are UTC, like SQLite. A psycopg2 `options` kwarg REPLACES
        # the DSN's options key, so merge: keep whatever the DSN already sets
        # (e.g. -c search_path=...) and append the TimeZone setting.
        dsn_options = psycopg2.extensions.parse_dsn(dsn).get("options", "")
        options = f"{dsn_options} -c TimeZone=UTC".strip()
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=maxconn, dsn=dsn, options=options)
        logger.info(f"Postgres session options: {options}")
        self._conn_slots = threading.BoundedSemaphore(maxconn)
        self._acquire_timeout = acquire_timeout
        logger.info(f"Postgres connection pool maxconn={maxconn} "
                    f"(acquire_timeout={acquire_timeout}s)")

    def _getconn(self):
        """Take a pool slot (blocking up to the acquire timeout), then a connection.
        Every successful call MUST be paired with exactly one _putconn()."""
        import psycopg2.pool
        if not self._conn_slots.acquire(timeout=self._acquire_timeout):
            raise psycopg2.pool.PoolError(
                f"connection pool exhausted (waited {self._acquire_timeout}s)")
        try:
            return self._pool.getconn()
        except BaseException:
            self._conn_slots.release()
            raise

    def _putconn(self, conn, close: bool = False) -> None:
        """Return (or, with close=True, discard) a connection and free its slot."""
        try:
            self._pool.putconn(conn, close=close)
        finally:
            self._conn_slots.release()

    def _dict_cursor(self, conn):
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    @contextmanager
    def _db(self, dict_rows: bool = False):
        """Pull a pooled connection, auto-commit on success / rollback on error,
        and always return it to the pool. Yields (conn, cursor)."""
        conn = self._getconn()
        try:
            cur = self._dict_cursor(conn) if dict_rows else conn.cursor()
        except BaseException:
            self._putconn(conn)
            raise
        try:
            yield conn, cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                cur.close()
            finally:
                self._putconn(conn)

    # ============================================================
    # NOTEBOOKS
    # ============================================================

    # ---- Per-cell (de)serialization helpers ----

    _CELL_COLUMNS = (
        "notebook_id, cell_id, position, type, code, output, error, html, "
        "output_variable, execution_number, execution_time, last_executed_code, "
        "ai_explanation, ai_explanation_edited, ai_explanation_source, generated, "
        "peak_mem_mb, peak_cpu_pct, executed_at, image_s3_key"
    )
    _CELL_COLUMN_COUNT = len([c for c in _CELL_COLUMNS.split(",") if c.strip()])

    @staticmethod
    def _cell_to_row(notebook_id: str, position: int, c: dict) -> tuple:
        """Flatten a client cell dict into a notebook_cells row tuple. `image`
        (base64) is intentionally dropped; only a validated `imageS3Key` (this
        notebook's/cell's own key) is kept."""
        return (
            notebook_id,
            str(c.get("id")),
            position,
            c.get("type") or "code",
            c.get("code") or "",
            c.get("output"),
            c.get("error"),
            c.get("html"),
            c.get("outputVariable"),
            c.get("executionNumber"),
            c.get("executionTime"),
            c.get("lastExecutedCode"),
            c.get("aiExplanation"),
            1 if c.get("aiExplanationEdited") else 0,
            c.get("aiExplanationSource"),
            c.get("generated"),
            as_float(c.get("peakMemMb")),
            as_float(c.get("peakCpuPct")),
            str(c["executedAt"]) if c.get("executedAt") else None,
            valid_image_key(notebook_id, c.get("id"), c.get("imageS3Key")),
        )

    @staticmethod
    def _row_to_cell(r) -> dict:
        return {
            "id": r["cell_id"],
            "type": r["type"] or "code",
            "code": r["code"] or "",
            "output": r["output"],
            "error": r["error"],
            "html": r["html"],
            "image": None,  # base64 never persisted; the chart loads via imageS3Key
            "imageS3Key": r["image_s3_key"],
            "outputVariable": r["output_variable"],
            "executionNumber": r["execution_number"],
            "executionTime": r["execution_time"],
            "lastExecutedCode": r["last_executed_code"],
            "aiExplanation": r["ai_explanation"],
            "aiExplanationEdited": bool(r["ai_explanation_edited"]),
            "aiExplanationSource": r["ai_explanation_source"],
            "generated": r["generated"],
            "peakMemMb": r["peak_mem_mb"],
            "peakCpuPct": r["peak_cpu_pct"],
            "executedAt": r["executed_at"],
        }

    def _write_cells(self, cur, notebook_id: str, cells: list) -> None:
        """Replace ALL cell rows for a notebook (delete + re-insert in order).
        Cells without an id are skipped."""
        cur.execute("DELETE FROM notebook_cells WHERE notebook_id = %s", (notebook_id,))
        # Positions are numbered over the KEPT cells (gap-free), so they match
        # the DAG document's node.position (built from the same kept list).
        kept = [c for c in (cells or []) if c.get("id") is not None]
        rows = [self._cell_to_row(notebook_id, i, c) for i, c in enumerate(kept)]
        if rows:
            placeholders = "(" + ", ".join(["%s"] * self._CELL_COLUMN_COUNT) + ")"
            cur.executemany(
                f"INSERT INTO notebook_cells ({self._CELL_COLUMNS}) VALUES {placeholders}",
                rows,
            )

    def _read_cells(self, cur, notebook_id: str) -> list:
        cur.execute(
            "SELECT * FROM notebook_cells WHERE notebook_id = %s ORDER BY position ASC",
            (notebook_id,),
        )
        return [self._row_to_cell(r) for r in cur.fetchall()]

    # ---- Per-notebook DAG document (notebook_dag) ----

    def _dag_rmw(self, conn, notebook_id, build, with_cells: bool):
        """Read-modify-write of the notebook_dag row on `conn`, inside the CALLER's
        transaction. Serializes every DAG writer of this notebook (across proxy
        instances) with SELECT ... FOR UPDATE on the notebooks row — the row
        notebook_update updates anyway. Returns (found, DagWrite|None): found=False
        (and None) when the notebook does not exist. Nothing is written when build
        returned None, the rebuilt doc is unchanged (graph and runtime hashes
        equal) or too large to store; the DagWrite then has wrote=False, doc=prev."""
        dcur = self._dict_cursor(conn)
        try:
            dcur.execute("SELECT id FROM notebooks WHERE id = %s FOR UPDATE", (notebook_id,))
            nb = dcur.fetchone()
            if not nb:
                return False, None
            nid = nb["id"]
            dcur.execute(
                "SELECT version, built_at, dag_json FROM notebook_dag WHERE notebook_id = %s",
                (nid,),
            )
            row = dcur.fetchone()
            prev = (load_stored_doc(row["dag_json"], row["version"], row["built_at"], nid)
                    if row else None)
            if with_cells:
                new = build(prev, self._read_cells(dcur, nid))
            else:
                new = build(prev)
            if new is None:
                return True, unchanged_write(prev)
            w, text = prepare_write(new, prev, row["version"] if row else 0, nid)
            if text is None:
                return True, w
            doc = w.doc
            dcur.execute(
                "INSERT INTO notebook_dag (notebook_id, version, built_at, dag_json, updated_at) "
                "VALUES (%s, %s, %s, %s, NOW()) "
                "ON CONFLICT (notebook_id) DO UPDATE SET version = EXCLUDED.version, "
                "built_at = EXCLUDED.built_at, dag_json = EXCLUDED.dag_json, "
                "updated_at = EXCLUDED.updated_at",
                (nid, doc["version"], doc["built_at"], text),
            )
            return True, w
        finally:
            dcur.close()

    def notebook_dag_get(self, notebook_id) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT notebook_id, version, built_at, dag_json FROM notebook_dag "
                "WHERE notebook_id = %s", (notebook_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return load_stored_doc(row["dag_json"], row["version"], row["built_at"],
                               row["notebook_id"])

    def notebook_dag_write(self, notebook_id, build, *, with_cells: bool = False):
        with self._db() as (conn, cur):
            _found, w = self._dag_rmw(conn, notebook_id, build, with_cells)
        return w

    def notebook_dag_update(self, notebook_id, build, *, with_cells: bool = False) -> Optional[dict]:
        w = self.notebook_dag_write(notebook_id, build, with_cells=with_cells)
        return w.doc if w else None

    def notebook_id_for_session(self, session_id) -> Optional[int]:
        if not session_id:
            return None
        with self._db() as (conn, cur):
            cur.execute(
                "SELECT id FROM notebooks WHERE session_id = %s "
                "ORDER BY updated_at DESC, id DESC LIMIT 2",
                (session_id,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            logger.debug("notebook_id_for_session: session %s is linked to more than one "
                         "notebook; using the most recently updated (id %s)",
                         session_id, rows[0][0])
        return int(rows[0][0])

    def notebook_create(self, name: str, description: str = "",
                        tag: str = "Drafts", cells: list = None,
                        session_id: str = None, dag_build=None) -> dict:
        """Create a notebook and RETURN it with the DB-generated integer `id`.

        The id is assigned by the DB (IDENTITY via RETURNING), not the caller —
        the create-returns-id contract the frontend relies on. session_id is
        optional (the client's stable UUID handle). dag_build (optional) runs in
        the same transaction after the cells are written; the stored doc is
        returned under "dag"."""
        dag = None
        with self._db() as (conn, cur):
            cur.execute(
                "INSERT INTO notebooks (name, description, tag, session_id) "
                "VALUES (%s, %s, %s, %s) RETURNING id, created_at",
                (name, description, tag, session_id)
            )
            notebook_id, created_at = cur.fetchone()
            self._write_cells(cur, notebook_id, cells or [])
            if dag_build is not None:
                _found, w = self._dag_rmw(conn, notebook_id, dag_build, False)
                dag = w.doc if w else None
        result = {"id": notebook_id, "name": name, "description": description,
                  "tag": tag, "session_id": session_id,
                  "cells": echo_cells_with_valid_keys(notebook_id, cells),
                  "created_at": iso_utc(created_at)}
        if dag is not None:
            result["dag"] = dag
        return result

    def notebook_update(self, notebook_id: str, *, dag_build=None, **kwargs) -> bool:
        allowed = {"name", "description", "tag", "session_id", "microvm_id", "checkpoint_enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        has_cells = "cells" in kwargs
        if not updates and not has_cells:
            return False
        set_clause = ", ".join(f"{k} = %s" for k in updates) + ", updated_at = NOW()"
        values = list(updates.values()) + [notebook_id]
        with self._db() as (conn, cur):
            cur.execute(f"UPDATE notebooks SET {set_clause} WHERE id = %s", values)
            if cur.rowcount == 0:
                return False  # notebook doesn't exist — caller handles upsert
            if has_cells:
                self._write_cells(cur, notebook_id, kwargs["cells"] or [])
                # Same transaction (the UPDATE above already row-locks the
                # notebook): a _write_cells failure rolls back the DAG write too.
                if dag_build is not None:
                    self._dag_rmw(conn, notebook_id, dag_build, False)
            return True

    def notebook_cell_image_keys(self, notebook_id) -> dict:
        with self._db() as (conn, cur):
            cur.execute(
                "SELECT cell_id, image_s3_key FROM notebook_cells "
                "WHERE notebook_id = %s AND image_s3_key IS NOT NULL AND image_s3_key <> ''",
                (notebook_id,),
            )
            rows = cur.fetchall()
        return {str(r[0]): r[1] for r in rows}

    def notebook_cell_image_keys_all(self, notebook_id) -> dict:
        with self._db() as (conn, cur):
            cur.execute(
                "SELECT cell_id, image_s3_key, html FROM notebook_cells "
                "WHERE notebook_id = %s AND ((image_s3_key IS NOT NULL AND image_s3_key <> '') "
                "OR left(html, 14) = %s)",
                (notebook_id, OUTPUTS_ENVELOPE_PREFIX),
            )
            rows = cur.fetchall()
        out = {}
        for r in rows:
            keys = cell_all_image_keys(notebook_id, r[0], r[1], r[2])
            if keys:
                out[str(r[0])] = keys
        return out

    def notebook_get(self, notebook_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT * FROM notebooks WHERE id = %s", (notebook_id,))
            row = cur.fetchone()
            if not row:
                return None
            cells = self._read_cells(cur, notebook_id)
        return self._row_to_notebook(row, cells)

    def notebook_list(self) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT * FROM notebooks ORDER BY updated_at DESC")
            rows = cur.fetchall()
            result = []
            for r in rows:
                cells = self._read_cells(cur, r["id"])
                result.append(self._row_to_notebook(r, cells))
        return result

    # ---- Notebook-owned session cascade (shared by notebook_delete and
    # session_intel_delete). Private helpers: they run inside the caller's
    # transaction on the caller's cursor. ----

    @staticmethod
    def _notebook_session_ids(cur, notebook_id) -> list:
        """Every session id this notebook has had. notebooks.session_id is NULL
        after /terminate (notebook_clear_session), so the kept history tables
        (vm_sessions, notebook_runs) and agent_sessions are consulted too."""
        cur.execute(
            """SELECT session_id FROM notebooks WHERE id = %s
               UNION SELECT session_id FROM vm_sessions WHERE notebook_id = %s
               UNION SELECT session_id FROM agent_sessions WHERE notebook_id = %s
               UNION SELECT session_id FROM notebook_runs WHERE notebook_id = %s""",
            (notebook_id, notebook_id, notebook_id, notebook_id),
        )
        return sorted({r[0] for r in cur.fetchall() if r[0]})

    @staticmethod
    def _session_used_elsewhere(cur, session_id, notebook_id) -> bool:
        """True if another SURVIVING notebook (row still in notebooks) references
        session_id. Two notebooks can share one session id, so its rows are then
        not this notebook's to delete."""
        cur.execute(
            """SELECT 1 FROM notebooks WHERE session_id = %s AND id <> %s
               UNION ALL SELECT 1 FROM vm_sessions x JOIN notebooks n ON n.id = x.notebook_id
                   WHERE x.session_id = %s AND x.notebook_id <> %s
               UNION ALL SELECT 1 FROM agent_sessions x JOIN notebooks n ON n.id = x.notebook_id
                   WHERE x.session_id = %s AND x.notebook_id <> %s
               UNION ALL SELECT 1 FROM notebook_runs x JOIN notebooks n ON n.id = x.notebook_id
                   WHERE x.session_id = %s AND x.notebook_id <> %s
               LIMIT 1""",
            (session_id, notebook_id) * 4,
        )
        return cur.fetchone() is not None

    @staticmethod
    def _delete_session_intel(cur, session_id) -> int:
        """Delete the workbook_intel + local_file_entities rows of one session."""
        cur.execute("DELETE FROM workbook_intel WHERE session_id = %s", (session_id,))
        n = cur.rowcount
        cur.execute("DELETE FROM local_file_entities WHERE session_id = %s", (session_id,))
        return n + cur.rowcount

    def notebook_delete(self, notebook_id: str) -> bool:
        with self._db() as (conn, cur):
            # The session ids this notebook OWNS (it had them, and no other surviving
            # notebook references them). Computed BEFORE any delete: agent_sessions
            # is one of the sources. Works after /terminate cleared
            # notebooks.session_id, because vm_sessions / notebook_runs are kept.
            owned = [
                sid for sid in self._notebook_session_ids(cur, notebook_id)
                if not self._session_used_elsewhere(cur, sid, notebook_id)
            ]

            # Notebook-scoped orphans with NO FK cascade. Cleaned here so deleting a
            # notebook doesn't leak them. Cells/versions/cell_versions cascade via
            # their FKs. We deliberately PRESERVE cost/run history (notebook_runs,
            # vm_sessions, vm_state_log, vm_metrics) and never touch the global
            # data_source_entities catalog or pool_vms.
            #   - agent_sessions: keyed by notebook_id directly (agent_messages
            #     rows are removed via ON DELETE CASCADE).
            #   - per OWNED session id: workbook_intel, local_file_entities, its
            #     agent_sessions row (even one with notebook_id NULL) and
            #     session_registry (only with no live VM).
            cur.execute("DELETE FROM agent_sessions WHERE notebook_id = %s", (notebook_id,))
            # notebook_dag / notebook_env cascade with the notebook; deleted
            # explicitly too, matching the SQLite backend for these two tables
            # (SQLite also deletes cells/versions explicitly; here their FK
            # cascade does it). The end state is the same in both backends.
            cur.execute("DELETE FROM notebook_dag WHERE notebook_id = %s", (notebook_id,))
            cur.execute("DELETE FROM notebook_env WHERE notebook_id = %s", (notebook_id,))
            for sid in owned:
                self._delete_session_intel(cur, sid)
                cur.execute("DELETE FROM agent_sessions WHERE session_id = %s", (sid,))
                # session_registry is the LIVE routing map. Only remove it if the
                # session has no still-alive VM — otherwise we'd cut routing to a VM
                # that's still running (deleting a notebook does not terminate its VM).
                cur.execute(
                    """SELECT 1 FROM vm_sessions
                       WHERE session_id = %s AND state != 'TERMINATED' LIMIT 1""",
                    (sid,),
                )
                if cur.fetchone() is None:
                    cur.execute("DELETE FROM session_registry WHERE session_id = %s", (sid,))

            cur.execute("DELETE FROM notebooks WHERE id = %s", (notebook_id,))
            return cur.rowcount > 0

    def session_intel_delete(self, session_id: str) -> int:
        if not session_id:
            return 0
        with self._db() as (conn, cur):
            return self._delete_session_intel(cur, session_id)

    def notebook_owned_session_ids(self, notebook_id: str) -> list[str]:
        # Same OWNED rule as notebook_delete (read-only; call it before the delete).
        with self._db() as (conn, cur):
            return [
                sid for sid in self._notebook_session_ids(cur, notebook_id)
                if not self._session_used_elsewhere(cur, sid, notebook_id)
            ]

    def notebook_latest_session_id(self, notebook_id: str) -> Optional[str]:
        with self._db() as (conn, cur):
            cur.execute(
                """SELECT session_id FROM vm_sessions
                    WHERE notebook_id = %s AND session_id IS NOT NULL AND session_id <> ''
                    -- Latest = still alive, else most recently terminated. NOT
                    -- launched_at alone: a pool-granted VM's launched_at is its
                    -- MINT time, older than a cold launch it replaced.
                    ORDER BY (terminated_at IS NULL) DESC, terminated_at DESC, launched_at DESC LIMIT 1""",
                (notebook_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            sid = row[0]
            # Only the LATEST session counts (see the SQLite backend).
            return None if self._session_used_elsewhere(cur, sid, notebook_id) else sid

    def notebook_clear_session(self, session_id: str) -> bool:
        # Clear the live-session link when the notebook's VM is TERMINATED so
        # v_nb_notebook.session_id (the "is this open?" signal) stops reporting a
        # dead notebook as open. Keyed by session_id (what the terminate path has in
        # hand). Bumps updated_at (sync watermark parity with the sqlite backend).
        if not session_id:
            return False
        with self._db() as (conn, cur):
            cur.execute(
                "UPDATE notebooks SET session_id = NULL, microvm_id = NULL, updated_at = NOW() "
                "WHERE session_id = %s",
                (session_id,),
            )
            return cur.rowcount > 0

    # ---- Notebook runs (execution history) ----

    def notebook_run_create(self, run_id: str, notebook_id: str = None,
                            session_id: str = None, microvm_id: str = None,
                            trigger: str = "single",
                            started_at: str = None, finished_at: str = None,
                            cells_executed: int = 0, cells_errored: int = 0,
                            cells_interrupted: int = 0,
                            total_exec_ms: float = 0.0,
                            run_cells: list = None) -> dict:
        run_cells = run_cells or []
        run_cells_json = json.dumps(run_cells)
        # Client-supplied clock strings: store ISO-8601 UTC, or NULL when
        # unparseable (v_nb_runs casts these TEXT columns to timestamptz).
        started_at = iso_utc(started_at)
        finished_at = iso_utc(finished_at)
        with self._db() as (conn, cur):
            # Re-using a run_id (same rule as sqlite): keep the original
            # created_at, take every other column from this call.
            cur.execute(
                """INSERT INTO notebook_runs
                   (run_id, notebook_id, session_id, microvm_id, trigger, started_at, finished_at,
                    cells_executed, cells_errored, cells_interrupted, total_exec_ms, run_cells_json)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (run_id) DO UPDATE SET
                     notebook_id = EXCLUDED.notebook_id,
                     session_id = EXCLUDED.session_id,
                     microvm_id = EXCLUDED.microvm_id,
                     trigger = EXCLUDED.trigger,
                     started_at = EXCLUDED.started_at,
                     finished_at = EXCLUDED.finished_at,
                     cells_executed = EXCLUDED.cells_executed,
                     cells_errored = EXCLUDED.cells_errored,
                     cells_interrupted = EXCLUDED.cells_interrupted,
                     total_exec_ms = EXCLUDED.total_exec_ms,
                     run_cells_json = EXCLUDED.run_cells_json,
                     updated_at = NOW()""",
                (run_id, notebook_id, session_id, microvm_id, trigger, started_at, finished_at,
                 cells_executed, cells_errored, cells_interrupted, total_exec_ms, run_cells_json)
            )
        return {
            "run_id": run_id, "notebook_id": notebook_id, "session_id": session_id,
            "microvm_id": microvm_id, "trigger": trigger,
            "started_at": started_at, "finished_at": finished_at,
            "cells_executed": cells_executed, "cells_errored": cells_errored,
            "cells_interrupted": cells_interrupted, "total_exec_ms": total_exec_ms,
            "run_cells": run_cells,
        }

    @staticmethod
    def _run_row_to_dict(row) -> dict:
        """Row → run dict, parsing the run_cells_json blob into a `run_cells` list.
        created_at/updated_at (timestamptz) come back as ISO-8601 UTC strings."""
        d = dict(row)
        for col in ("created_at", "updated_at"):
            if col in d:
                d[col] = iso_utc(d[col])
        raw = d.pop("run_cells_json", None)
        try:
            d["run_cells"] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            d["run_cells"] = []
        return d

    def notebook_run_list(self, notebook_id: str, limit: int = 20,
                          trigger: str = None) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            if trigger:
                cur.execute(
                    """SELECT * FROM notebook_runs WHERE notebook_id = %s AND trigger = %s
                       ORDER BY finished_at DESC NULLS LAST LIMIT %s""",
                    (notebook_id, trigger, max(0, int(limit)))
                )
            else:
                cur.execute(
                    """SELECT * FROM notebook_runs WHERE notebook_id = %s
                       ORDER BY finished_at DESC NULLS LAST LIMIT %s""",
                    (notebook_id, max(0, int(limit)))
                )
            rows = cur.fetchall()
        return [self._run_row_to_dict(r) for r in rows]

    def notebook_run_list_for_vms(self, microvm_ids: list[str],
                                  limit: int = 200) -> list[dict]:
        ids = [m for m in (microvm_ids or []) if m]
        if not ids:
            return []
        marks = ", ".join(["%s"] * len(ids))
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                f"""SELECT {_mf.RUN_LIGHT_COLUMNS} FROM notebook_runs
                    WHERE microvm_id IN ({marks})
                    ORDER BY COALESCE(started_at, finished_at) DESC NULLS LAST LIMIT %s""",
                (*ids, max(0, int(limit))),
            )
            rows = cur.fetchall()
        return [_mf.normalize_run(dict(r)) for r in rows]

    # ---- Notebook versions (document history) ----

    _CELL_VERSION_COLUMNS = (
        "version_group_id, cell_id, position, type, code, "
        "output_variable, ai_explanation, ai_explanation_source, generated"
    )

    @staticmethod
    def _cell_version_row(version_group_id: str, position: int, c: dict) -> tuple:
        """Flatten a client cell dict into a notebook_cell_versions row — the
        DOCUMENT subset only (no runtime output/exec/dataflow fields)."""
        return (
            version_group_id,
            str(c.get("id")),
            position,
            c.get("type") or "code",
            c.get("code") or "",
            c.get("outputVariable"),
            c.get("aiExplanation"),
            c.get("aiExplanationSource"),
            c.get("generated"),
        )

    @staticmethod
    def _row_to_version_cell(r) -> dict:
        """Reassemble a client cell dict from a notebook_cell_versions row (shaped
        for notebook_update(cells=...) on restore)."""
        return {
            "id": r["cell_id"],
            "type": r["type"] or "code",
            "code": r["code"] or "",
            "outputVariable": r["output_variable"],
            "aiExplanation": r["ai_explanation"],
            "aiExplanationSource": r["ai_explanation_source"],
            "generated": r["generated"],
        }

    def notebook_version_create(self, notebook_id: str, cells: list,
                                label: str = None, trigger: str = "manual",
                                version_group_id: str = None) -> dict:
        vgid = version_group_id or str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        # De-dupe by id (first-wins) so duplicate cell ids can't violate the
        # (version_group_id, cell_id) PK and abort the whole write; cell_count is
        # derived from what we actually store. fingerprint_hash lets the route dedup
        # non-manual snapshots with one indexed lookup instead of re-reading cells.
        snap = dedupe_cells_by_id(cells)
        cell_count = len(snap)
        fp_hash = fingerprint_hash(cells)
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO notebook_versions
                   (version_group_id, notebook_id, label, trigger, cell_count, fingerprint_hash, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (vgid, notebook_id, label, trigger, cell_count, fp_hash, created_at),
            )
            rows = [self._cell_version_row(vgid, i, c) for i, c in enumerate(snap)]
            if rows:
                placeholders = "(" + ", ".join(["%s"] * 9) + ")"
                cur.executemany(
                    f"INSERT INTO notebook_cell_versions ({self._CELL_VERSION_COLUMNS}) "
                    f"VALUES {placeholders}",
                    rows,
                )
        return {
            "version_group_id": vgid, "notebook_id": notebook_id,
            "label": label, "trigger": trigger, "cell_count": cell_count,
            "fingerprint_hash": fp_hash, "created_at": created_at,
        }

    def notebook_version_find_by_fingerprint(self, notebook_id: str, fp_hash: str) -> Optional[dict]:
        """Most-recent version header for this notebook whose stored fingerprint_hash
        matches, or None. Powers O(1) dedup of non-manual snapshots."""
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                """SELECT * FROM notebook_versions
                   WHERE notebook_id = %s AND fingerprint_hash = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (notebook_id, fp_hash),
            )
            row = cur.fetchone()
        return self._version_header_to_dict(row) if row else None

    def notebook_version_list(self, notebook_id: str, limit: int = 50) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                """SELECT * FROM notebook_versions WHERE notebook_id = %s
                   ORDER BY created_at DESC LIMIT %s""",
                (notebook_id, max(0, int(limit))),
            )
            rows = cur.fetchall()
        return [self._version_header_to_dict(r) for r in rows]

    def notebook_version_get(self, version_group_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT * FROM notebook_versions WHERE version_group_id = %s",
                (version_group_id,),
            )
            header = cur.fetchone()
            if not header:
                return None
            cur.execute(
                """SELECT * FROM notebook_cell_versions WHERE version_group_id = %s
                   ORDER BY position ASC""",
                (version_group_id,),
            )
            cell_rows = cur.fetchall()
        d = self._version_header_to_dict(header)
        d["cells"] = [self._row_to_version_cell(r) for r in cell_rows]
        return d

    def notebook_cell_versions(self, notebook_id: str, cell_id: str,
                               limit: int = 20) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                """SELECT cv.*, v.created_at AS created_at, v.label AS label,
                          v.trigger AS trigger
                   FROM notebook_cell_versions cv
                   JOIN notebook_versions v ON v.version_group_id = cv.version_group_id
                   WHERE v.notebook_id = %s AND cv.cell_id = %s
                   ORDER BY v.created_at DESC LIMIT %s""",
                (notebook_id, cell_id, max(0, int(limit))),
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            cell = self._row_to_version_cell(r)
            cell.update({
                "version_group_id": r["version_group_id"],
                "created_at": _iso(r["created_at"]),
                "label": r["label"],
                "trigger": r["trigger"],
            })
            out.append(cell)
        return out

    def notebook_version_prune(self, notebook_id: str, keep: int = 50) -> int:
        # Prune 'auto' versions beyond the newest `keep`; keep manual/pre_ai/
        # pre_restore. Cell rows cascade with the header.
        with self._db() as (conn, cur):
            cur.execute(
                """SELECT version_group_id FROM notebook_versions
                   WHERE notebook_id = %s AND trigger = 'auto'
                   ORDER BY created_at DESC OFFSET %s""",
                (notebook_id, keep),
            )
            ids = [r[0] for r in cur.fetchall()]
            for vgid in ids:
                # ON DELETE CASCADE removes the cell rows.
                cur.execute("DELETE FROM notebook_versions WHERE version_group_id = %s", (vgid,))
        return len(ids)

    def notebook_version_delete(self, notebook_id: str, version_group_id: str) -> bool:
        # Scoped by notebook_id so a wrong id can't remove another notebook's
        # version. ON DELETE CASCADE removes the cell rows with the header.
        with self._db() as (conn, cur):
            cur.execute(
                "DELETE FROM notebook_versions WHERE version_group_id = %s AND notebook_id = %s",
                (version_group_id, notebook_id),
            )
            return cur.rowcount > 0

    @staticmethod
    def _version_header_to_dict(row) -> dict:
        """Header row → dict with ISO-8601 UTC created_at/updated_at (timestamptz → str)."""
        return iso_utc_cols(dict(row), ("created_at", "updated_at"))

    @staticmethod
    def _row_to_notebook(row, cells: list) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "tag": row["tag"],
            "cells": cells,
            "session_id": row["session_id"],
            "microvm_id": row["microvm_id"],
            "checkpoint_enabled": bool(row["checkpoint_enabled"]),
            "created_at": iso_utc(row.get("created_at")),
            "updated_at": iso_utc(row.get("updated_at")),
        }

    # ============================================================
    # VM SESSIONS
    # ============================================================

    def vm_session_create(self, microvm_id: str, notebook_id: str = None,
                          session_id: str = None, memory_mib: int = None,
                          endpoint: str = None, idle_timeout_sec: int = None,
                          max_duration_sec: int = None,
                          checkpoint_enabled: bool = False,
                          architecture: str = "arm64",
                          notebook_name: str = None,
                          launched_at: str = None,
                          prior_suspended_since: str = None) -> dict:
        # launched_at is the VM's true MINT time. For a cold launch the caller
        # omits it and COALESCE falls back to NOW() (minted now). For a pooled
        # spare the caller passes the pool's mint ISO so the AWS 8h-lifetime
        # countdown is honest (the spare may have sat warm before adoption). The
        # launched_at column is TIMESTAMPTZ; psycopg2 accepts an ISO-8601 string.
        now = datetime.now(timezone.utc).isoformat()
        effective_launched_at = launched_at or now
        # prior_suspended_since: pool-residency SUSPENDED time (mint->grant). Kept
        # in exact parity with the sqlite backend — when it's a valid ISO strictly
        # older than now, seed TWO state-log rows (NULL->SUSPENDED at mint, then
        # SUSPENDED->RUNNING at now) with EXPLICIT timestamps (NOT NOW(), so the two
        # rows get the intended distinct times the LEAD() reconstruction needs);
        # otherwise fall back to the single NULL->RUNNING row.
        seed_suspended = self._valid_prior_suspended(prior_suspended_since, now)
        with self._db() as (conn, cur):
            # Re-registering an existing microvm_id (same rule as sqlite): keep
            # burst_mb_seconds and image_arn, clear terminated_at (the row is
            # RUNNING again), take every other column from this launch.
            cur.execute(
                """INSERT INTO vm_sessions
                   (microvm_id, notebook_id, notebook_name, session_id, memory_mib, architecture, state, endpoint,
                    launched_at, idle_timeout_sec, max_duration_sec, checkpoint_enabled)
                   VALUES (%s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s, %s)
                   ON CONFLICT (microvm_id) DO UPDATE SET
                     notebook_id = EXCLUDED.notebook_id,
                     notebook_name = EXCLUDED.notebook_name,
                     session_id = EXCLUDED.session_id,
                     memory_mib = EXCLUDED.memory_mib,
                     architecture = EXCLUDED.architecture,
                     state = 'RUNNING',
                     endpoint = EXCLUDED.endpoint,
                     launched_at = EXCLUDED.launched_at,
                     terminated_at = NULL,
                     idle_timeout_sec = EXCLUDED.idle_timeout_sec,
                     max_duration_sec = EXCLUDED.max_duration_sec,
                     checkpoint_enabled = EXCLUDED.checkpoint_enabled,
                     updated_at = NOW()""",
                (microvm_id, notebook_id, notebook_name, session_id, memory_mib, architecture or "arm64", endpoint,
                 effective_launched_at, idle_timeout_sec, max_duration_sec, 1 if checkpoint_enabled else 0)
            )
            if seed_suspended is not None:
                # SUSPENDED first (smaller id), timestamp = mint; RUNNING next,
                # timestamp = now. Explicit timestamps (TIMESTAMPTZ accepts ISO).
                cur.execute(
                    "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (%s, %s, NULL, 'SUSPENDED')",
                    (microvm_id, seed_suspended)
                )
                cur.execute(
                    "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (%s, %s, 'SUSPENDED', 'RUNNING')",
                    (microvm_id, now)
                )
            else:
                cur.execute(
                    "INSERT INTO vm_state_log (microvm_id, old_state, new_state) VALUES (%s, NULL, 'RUNNING')",
                    (microvm_id,)
                )
        return {"microvm_id": microvm_id, "state": "RUNNING", "launched_at": effective_launched_at}

    @staticmethod
    def _valid_prior_suspended(prior_suspended_since, now_iso):
        """Return the ISO mint time to seed a SUSPENDED interval with, or None.

        Identical acceptance rules to the sqlite backend (kept in parity behind
        interface.py): None/empty, unparseable, or NOT strictly older than now →
        None (seed nothing; single NULL->RUNNING row). A bad value can never
        produce a zero/negative or inverted interval."""
        if not prior_suspended_since:
            return None
        try:
            susp = datetime.fromisoformat(str(prior_suspended_since).replace("Z", "+00:00"))
            ref = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if susp.tzinfo is None:
            susp = susp.replace(tzinfo=timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return prior_suspended_since if susp < ref else None

    def vm_session_update_state(self, microvm_id: str, new_state: str) -> None:
        with self._db() as (conn, cur):
            # Row lock for the read-check-write: a concurrent transition waits and
            # then reads the committed state, so only the writer that still sees
            # old_state != new_state logs the transition (no duplicate log rows).
            cur.execute("SELECT state FROM vm_sessions WHERE microvm_id = %s FOR UPDATE", (microvm_id,))
            row = cur.fetchone()
            # No session row → nothing to transition. Without this guard, every poll
            # for a VM with no vm_sessions row (e.g. an orphaned in-memory VM after a
            # DB reset) writes a spurious old_state=NULL row to vm_state_log forever
            # (None never equals new_state, so the dedup can't fire). Skip it.
            if row is None:
                return
            old_state = row[0]
            if old_state == new_state:
                return
            cur.execute("UPDATE vm_sessions SET state = %s, updated_at = NOW() WHERE microvm_id = %s", (new_state, microvm_id))
            cur.execute(
                "INSERT INTO vm_state_log (microvm_id, old_state, new_state) VALUES (%s, %s, %s)",
                (microvm_id, old_state, new_state)
            )
            if new_state == "TERMINATED":
                cur.execute("UPDATE vm_sessions SET terminated_at = NOW(), updated_at = NOW() WHERE microvm_id = %s", (microvm_id,))

    def vm_session_mark_terminated(self, microvm_id: str) -> bool:
        with self._db() as (conn, cur):
            # Row lock before the check, same as vm_session_update_state.
            cur.execute("SELECT state FROM vm_sessions WHERE microvm_id = %s FOR UPDATE", (microvm_id,))
            row = cur.fetchone()
            old_state = row[0] if row else None
            if not row or old_state == "TERMINATED":
                return False  # unknown session or already terminated — no-op
            cur.execute("UPDATE vm_sessions SET state = 'TERMINATED', terminated_at = NOW(), updated_at = NOW() WHERE microvm_id = %s", (microvm_id,))
            cur.execute(
                "INSERT INTO vm_state_log (microvm_id, old_state, new_state) VALUES (%s, %s, 'TERMINATED')",
                (microvm_id, old_state)
            )
        return True

    def vm_session_update_burst(self, microvm_id: str, burst_mb_seconds: float) -> None:
        with self._db() as (conn, cur):
            # Only write (and bump the sync watermark) when the value ACTUALLY
            # changes — the cost poller calls this every /instances cycle with a
            # usually-unchanged value, and an unconditional updated_at bump would
            # churn the sync watermark on an idle VM. IS DISTINCT FROM is the
            # null-safe inequality (NULL->value still writes).
            cur.execute(
                "UPDATE vm_sessions SET burst_mb_seconds = %s, updated_at = NOW() "
                "WHERE microvm_id = %s AND burst_mb_seconds IS DISTINCT FROM %s",
                (burst_mb_seconds, microvm_id, burst_mb_seconds)
            )

    def vm_session_get(self, microvm_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT * FROM vm_sessions WHERE microvm_id = %s", (microvm_id,))
            row = cur.fetchone()
        return self._normalize_session(row) if row else None

    def vm_session_list_active(self) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT * FROM vm_sessions WHERE state != 'TERMINATED' ORDER BY launched_at DESC")
            rows = cur.fetchall()
        return [self._normalize_session(r) for r in rows]

    def vm_session_list_for_session(self, session_id: str) -> list[dict]:
        # All VMs (any state) that served this session — original + rotation/recovery
        # replacements — oldest first, so log merging reads them in chronological
        # order. Uses idx_vm_sessions_session.
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT * FROM vm_sessions WHERE session_id = %s ORDER BY launched_at ASC",
                (session_id,),
            )
            rows = cur.fetchall()
        return [self._normalize_session(r) for r in rows]

    def vm_state_log_get(self, microvm_id: str) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT new_state, timestamp FROM vm_state_log WHERE microvm_id = %s ORDER BY id ASC",
                (microvm_id,)
            )
            rows = cur.fetchall()
        return [{"new_state": r["new_state"],
                 "timestamp": r["timestamp"].isoformat() if r.get("timestamp") else None}
                for r in rows]

    # Filter-first per-VM cost query: same CTE chain and column expressions as
    # v_vm_sessions (unchanged), with vm_state_log filtered BEFORE the LEAD window
    # / GROUP BY so only the requested VMs are aggregated (PARTITION BY microvm_id
    # makes this result-identical). {log_filter}/{where} are fixed SQL fragments
    # with %s placeholders only — every id/session is a bound parameter.
    _COST_QTY_FILTERED_SQL = """
    WITH intervals AS (
        SELECT
            l.microvm_id,
            l.new_state AS state,
            l.timestamp AS start_ts,
            LEAD(l.timestamp) OVER (PARTITION BY l.microvm_id ORDER BY l.id) AS next_ts
        FROM vm_state_log l
        WHERE {log_filter}
    ),
    durations AS (
        SELECT
            microvm_id,
            state,
            EXTRACT(EPOCH FROM (
                COALESCE(next_ts, CASE WHEN state = 'TERMINATED' THEN start_ts ELSE NOW() END)
                - start_ts
            )) AS secs
        FROM intervals
    ),
    agg AS (
        SELECT
            microvm_id,
            COALESCE(SUM(CASE WHEN state = 'RUNNING'   THEN secs END), 0) AS running_secs,
            COALESCE(SUM(CASE WHEN state = 'SUSPENDED' THEN secs END), 0) AS suspended_secs,
            COALESCE(SUM(CASE WHEN state = 'RUNNING'   THEN 1 ELSE 0 END), 0) AS resume_count,
            COALESCE(SUM(CASE WHEN state = 'SUSPENDED' THEN 1 ELSE 0 END), 0) AS suspend_count
        FROM durations
        GROUP BY microvm_id
    )
    SELECT
        s.microvm_id,
        s.state,
        s.memory_mib,
        s.architecture,
        ROUND(COALESCE(a.running_secs, 0))   AS running_secs,
        ROUND(COALESCE(a.suspended_secs, 0)) AS suspended_secs,
        s.burst_mb_seconds,
        COALESCE(a.resume_count, 0)  AS resume_count,
        COALESCE(a.suspend_count, 0) AS suspend_count{extra_cols}
    FROM vm_sessions s
    LEFT JOIN agg a ON a.microvm_id = s.microvm_id
    WHERE {where}
    """
    _COST_QTY_CHUNK = 500

    def vm_cost_quantities_many(self, microvm_ids: list[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(i for i in (microvm_ids or []) if i))
        if not ids:
            return {}
        sql = self._COST_QTY_FILTERED_SQL.format(
            log_filter="l.microvm_id = ANY(%s)",
            extra_cols="",
            where="s.microvm_id = ANY(%s)",
        )
        out: dict[str, dict] = {}
        with self._db(dict_rows=True) as (conn, cur):
            for start in range(0, len(ids), self._COST_QTY_CHUNK):
                chunk = ids[start:start + self._COST_QTY_CHUNK]
                cur.execute(sql, (chunk, chunk))
                for row in cur.fetchall():
                    out[row["microvm_id"]] = self._normalize_cost_qty(row)
        return out

    def vm_cost_quantities(self, microvm_id: str) -> Optional[dict]:
        return self.vm_cost_quantities_many([microvm_id]).get(microvm_id)

    def vm_cost_quantities_for_session(self, session_id: str) -> list[dict]:
        """Durable per-VM quantities for every VM that served a session.

        Same numbers as v_vm_sessions (terminated predecessors included, running/
        suspended time reconstructed from vm_state_log), so this remains correct
        after the current VM is gone or the session rotated through multiple VMs —
        but the log is filtered to this session's VMs before aggregation.
        """
        sql = self._COST_QTY_FILTERED_SQL.format(
            log_filter="l.microvm_id IN (SELECT microvm_id FROM vm_sessions WHERE session_id = %s)",
            extra_cols=",\n        s.launched_at,\n        s.terminated_at",
            where="s.session_id = %s",
        ) + "ORDER BY s.launched_at ASC"
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(sql, (session_id, session_id))
            rows = cur.fetchall()
        return [self._normalize_session(self._normalize_cost_qty(row)) for row in rows]

    @staticmethod
    def _normalize_cost_qty(row) -> dict:
        # ROUND() in Postgres returns numeric/Decimal; coerce the seconds to float
        # so callers see the same numeric types as SQLite (which returns floats).
        d = dict(row)
        for k in ("running_secs", "suspended_secs", "burst_mb_seconds"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        for k in ("resume_count", "suspend_count"):
            if d.get(k) is not None:
                d[k] = int(d[k])
        return d

    @staticmethod
    def _normalize_session(row) -> dict:
        """Convert timestamptz columns to ISO strings so callers see the same
        shape as SQLite (which stores ISO text)."""
        return iso_utc_cols(dict(row), VM_SESSION_TS)

    # ============================================================
    # SESSION REGISTRY
    # ============================================================

    def session_registry_upsert(self, session_id: str, vm_id: str, endpoint: str) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO session_registry (session_id, vm_id, endpoint, updated_at)
                   VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (session_id) DO UPDATE SET
                     vm_id = EXCLUDED.vm_id,
                     endpoint = EXCLUDED.endpoint,
                     updated_at = NOW()""",
                (session_id, vm_id, endpoint)
            )

    def session_registry_get(self, session_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT vm_id, endpoint FROM session_registry WHERE session_id = %s",
                (session_id,)
            )
            row = cur.fetchone()
        return {"vm_id": row["vm_id"], "endpoint": row["endpoint"]} if row else None

    def session_registry_delete(self, session_id: str) -> None:
        with self._db() as (conn, cur):
            cur.execute("DELETE FROM session_registry WHERE session_id = %s", (session_id,))

    def session_registry_all(self) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT session_id, vm_id, endpoint FROM session_registry")
            rows = cur.fetchall()
        return [
            {"session_id": r["session_id"], "vm_id": r["vm_id"], "endpoint": r["endpoint"]}
            for r in rows
        ]

    # ============================================================
    # DISTRIBUTED SESSION LOCK (pg_advisory_lock)
    # ============================================================
    #
    # Advisory locks are keyed by a bigint. We derive one deterministically from
    # session_id via Postgres' own hashtext() (stable within a PG major version;
    # collisions only cause two *different* sessions to serialize, which is
    # harmless — never a false "lock acquired"). The lock is SESSION-scoped (not
    # transaction-scoped) because recovery spans ~10s of async work, not one txn.
    # Session-scoped advisory locks live on the connection, so we hold a dedicated
    # connection per acquired lock and release on that SAME connection.

    def try_session_lock(self, session_id: str) -> bool:
        with self._lock_guard:
            if session_id in self._lock_conns:
                # Already held by THIS instance/thread — non-reentrant by contract.
                return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)::bigint)", (session_id,))
                acquired = cur.fetchone()[0]
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            finally:
                self._putconn(conn)
            raise
        if not acquired:
            # Another instance holds it — return the connection to the pool.
            self._putconn(conn)
            return False
        with self._lock_guard:
            self._lock_conns[session_id] = conn
        return True

    def release_session_lock(self, session_id: str) -> None:
        with self._lock_guard:
            conn = self._lock_conns.pop(session_id, None)
        if conn is None:
            return  # not held — safe no-op
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (session_id,))
            conn.commit()
        except Exception:
            # If the connection died, the advisory lock is auto-released by
            # Postgres anyway. Discard the connection (close=True) instead of
            # reusing it — via putconn so the pool slot is freed, not leaked.
            try:
                self._putconn(conn, close=True)
            except Exception:
                pass
            return
        self._putconn(conn)

    # ============================================================
    # POOL VMS
    # ============================================================

    def pool_vm_create(self, vm_id: str, architecture: str, size_mib: int,
                       endpoint: str, fake_session_id: str,
                       launched_at: str = None, state: str = "launching") -> dict:
        launched_at = launched_at or datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO pool_vms
                   (vm_id, architecture, size_mib, endpoint, fake_session_id,
                    state, real_session_id, launched_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (vm_id, architecture, size_mib, endpoint, fake_session_id,
                 state, None, launched_at, now)
            )
        return {"vm_id": vm_id, "architecture": architecture, "size_mib": size_mib,
                "endpoint": endpoint, "fake_session_id": fake_session_id,
                "state": state, "real_session_id": None,
                "launched_at": launched_at, "updated_at": now}

    def pool_vm_set_state(self, vm_id: str, state: str,
                          real_session_id=_UNSET) -> bool:
        set_clause = "state = %s, updated_at = NOW()"
        params = [state]
        if real_session_id is not _UNSET:
            set_clause += ", real_session_id = %s"
            params.append(real_session_id)
        params.append(vm_id)
        with self._db() as (conn, cur):
            cur.execute(
                f"UPDATE pool_vms SET {set_clause} WHERE vm_id = %s", params
            )
            rowcount = cur.rowcount
        return rowcount > 0

    def pool_vm_claim_youngest(self, architecture: str, size_mib: int,
                               real_session_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                """UPDATE pool_vms
                      SET state = 'granting', real_session_id = %s, updated_at = NOW()
                    WHERE vm_id = (
                        SELECT vm_id FROM pool_vms
                         WHERE architecture = %s AND size_mib = %s AND state = 'available'
                         ORDER BY launched_at DESC
                         LIMIT 1
                         FOR UPDATE SKIP LOCKED
                    )
                    AND state = 'available'
                    RETURNING vm_id, architecture, size_mib, endpoint, fake_session_id,
                              state, real_session_id, launched_at, updated_at""",
                (real_session_id, architecture, size_mib)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._normalize_pool_vm(dict(row))

    def pool_vm_list(self, state: str = None, architecture: str = None,
                     size_mib: int = None) -> list[dict]:
        clauses = []
        params = []
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        if architecture is not None:
            clauses.append("architecture = %s")
            params.append(architecture)
        if size_mib is not None:
            clauses.append("size_mib = %s")
            params.append(size_mib)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(f"SELECT * FROM pool_vms{where}", params)
            rows = cur.fetchall()
        return [self._normalize_pool_vm(dict(r)) for r in rows]

    def pool_vm_count_by_size(self, architecture: str, size_mib: int,
                              states: tuple = ("available", "launching")) -> int:
        placeholders = ", ".join(["%s"] * len(states))
        with self._db() as (conn, cur):
            cur.execute(
                f"""SELECT COUNT(*) FROM pool_vms
                     WHERE architecture = %s AND size_mib = %s AND state IN ({placeholders})""",
                [architecture, size_mib, *states]
            )
            count = cur.fetchone()[0]
        return count

    def pool_vm_delete(self, vm_id: str) -> None:
        with self._db() as (conn, cur):
            cur.execute("DELETE FROM pool_vms WHERE vm_id = %s", (vm_id,))

    def pool_vm_backdate(self, vm_id: str, launched_at: str = None,
                         updated_at: str = None) -> bool:
        """Test seam (see interface): not used by production code."""
        sets, params = [], []
        if launched_at is not None:
            sets.append("launched_at = %s")
            params.append(launched_at)
        if updated_at is not None:
            sets.append("updated_at = %s")
            params.append(updated_at)
        if not sets:
            return False
        params.append(vm_id)
        with self._db() as (conn, cur):
            cur.execute(
                f"UPDATE pool_vms SET {', '.join(sets)} WHERE vm_id = %s", params)
            return cur.rowcount > 0

    def pool_vm_list_older_than(self, max_age_seconds: float,
                                state: str = "available",
                                since_column: str = "launched_at") -> list[dict]:
        # since_column: "launched_at" (mint time — age-recycle) or "updated_at"
        # (last state change — stale-'granting' check). See the SQLite impl /
        # interface docstring for why the granting check MUST use updated_at.
        col = since_column if since_column in ("launched_at", "updated_at") else "launched_at"
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT * FROM pool_vms WHERE state = %s", (state,))
            rows = cur.fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for r in rows:
            d = self._normalize_pool_vm(dict(r))
            since_dt = datetime.fromisoformat(_iso(d[col]))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            age = (now - since_dt).total_seconds()
            if age > max_age_seconds:
                result.append(d)
        return result

    @staticmethod
    def _normalize_pool_vm(d: dict) -> dict:
        """Convert timestamptz columns to ISO strings so callers see the same
        shape as SQLite (which stores ISO text)."""
        return iso_utc_cols(d, POOL_VM_TS)

    # ============================================================
    # WORKBOOK INTEL
    # ============================================================

    def workbook_intel_save(self, session_id: str, s3_key: str) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO workbook_intel (session_id, s3_key, generated_at, version)
                   VALUES (%s, %s, NOW(), 1)
                   ON CONFLICT (session_id) DO UPDATE SET
                     s3_key = EXCLUDED.s3_key,
                     generated_at = NOW(),
                     version = workbook_intel.version + 1""",
                (session_id, s3_key)
            )

    def workbook_intel_get(self, session_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT session_id, s3_key, generated_at, version FROM workbook_intel WHERE session_id = %s",
                (session_id,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "s3_key": row["s3_key"],
            "generated_at": row["generated_at"].isoformat() if row.get("generated_at") else None,
            "version": row["version"],
        }

    # ============================================================
    # NOTEBOOK ENV (persisted env vars + secret references)
    # ============================================================

    def notebook_env_get(self, notebook_id) -> dict:
        # Env var VALUES are stored SEALED (AES-GCM, per-notebook subkey); decrypt
        # them here. Secret REFERENCES pass through unchanged. See env_crypto.
        from proxy.platform.env_crypto import unseal_env_vars
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT env_json FROM notebook_env WHERE notebook_id = %s",
                (notebook_id,)
            )
            row = cur.fetchone()
        data = {}
        if row and row.get("env_json"):
            try:
                data = json.loads(row["env_json"])
            except (json.JSONDecodeError, TypeError):
                data = {}
        return {
            "env_vars": unseal_env_vars(notebook_id, data.get("env_vars", {}) or {}),
            "secrets": data.get("secrets", []) or [],
        }

    def notebook_env_set(self, notebook_id, env_vars: dict, secrets: list,
                         merge_env: bool = False,
                         remove_env_keys: list | None = None) -> None:
        # SEAL env var values (AES-GCM, per-notebook subkey) before write so a DB
        # reader sees ciphertext only. Secret references never held a value. Fails
        # closed: seal_env_vars raises if the master key is unavailable.
        #
        # merge_env=False (default): env_vars is the whole new set (historic replace).
        # merge_env=True: start from the existing DECRYPTED env (reuse the unseal path
        # in notebook_env_get), drop any key in remove_env_keys, overlay env_vars, then
        # seal+write — so re-entering one key updates just that key and leaves the
        # others intact. Secrets are ALWAYS a whole-object replace.
        from proxy.platform.env_crypto import seal_env_vars
        incoming = dict(env_vars or {})
        if merge_env:
            existing = self.notebook_env_get(notebook_id).get("env_vars", {}) or {}
            merged = dict(existing)
            for k in (remove_env_keys or []):
                merged.pop(k, None)
            merged.update(incoming)
            incoming = merged
        sealed_env = seal_env_vars(notebook_id, incoming)
        blob = json.dumps({"env_vars": sealed_env, "secrets": secrets or []})
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO notebook_env (notebook_id, env_json, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (notebook_id) DO UPDATE SET
                     env_json = EXCLUDED.env_json,
                     updated_at = NOW()""",
                (notebook_id, blob)
            )

    def notebook_env_list(self) -> list:
        # All notebook envs, decrypted. See interface docstring.
        from proxy.platform.env_crypto import unseal_env_vars
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT notebook_id, env_json FROM notebook_env ORDER BY notebook_id"
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["env_json"]) if r.get("env_json") else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            nb = r["notebook_id"]
            out.append({
                "notebook_id": nb,
                "env_vars": unseal_env_vars(nb, data.get("env_vars", {}) or {}),
                "secrets": data.get("secrets", []) or [],
            })
        return out

    # ============================================================
    # METRICS
    # ============================================================

    def metrics_record(self, microvm_id: str, cpu_pct: float, mem_pct: float,
                       mem_used_mb: float, disk_pct: float, disk_used_mb: float,
                       net_bytes_sent: int, net_bytes_recv: int,
                       processes: int, uptime_sec: float,
                       recorded_at: float = None) -> None:
        if recorded_at is not None:
            # Explicit sample time (tests/backfill); same columns + timestamp.
            with self._db() as (conn, cur):
                cur.execute(
                    """INSERT INTO vm_metrics (microvm_id, timestamp, cpu_pct, mem_pct, mem_used_mb,
                       disk_pct, disk_used_mb, net_bytes_sent, net_bytes_recv, processes, uptime_sec)
                       VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (microvm_id, float(recorded_at), cpu_pct, mem_pct, mem_used_mb, disk_pct,
                     disk_used_mb, net_bytes_sent, net_bytes_recv, processes, uptime_sec)
                )
            return
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO vm_metrics (microvm_id, cpu_pct, mem_pct, mem_used_mb,
                   disk_pct, disk_used_mb, net_bytes_sent, net_bytes_recv, processes, uptime_sec)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (microvm_id, cpu_pct, mem_pct, mem_used_mb, disk_pct, disk_used_mb,
                 net_bytes_sent, net_bytes_recv, processes, uptime_sec)
            )

    def metrics_get_latest(self, microvm_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT * FROM vm_metrics WHERE microvm_id = %s ORDER BY timestamp DESC LIMIT 1",
                (microvm_id,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return iso_utc_cols(dict(row), METRICS_TS)

    def metrics_get_history(self, microvm_id: str, minutes: int = 5) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                """SELECT timestamp, cpu_pct, mem_pct, disk_pct, net_bytes_sent, net_bytes_recv
                   FROM vm_metrics
                   WHERE microvm_id = %s AND timestamp >= NOW() - (%s || ' minutes')::interval
                   ORDER BY timestamp ASC""",
                (microvm_id, str(minutes))
            )
            rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("timestamp") is not None and hasattr(d["timestamp"], "isoformat"):
                d["timestamp"] = d["timestamp"].isoformat()
            result.append(d)
        return result

    # Sample time as epoch seconds rounded to ms (NUMERIC), the same rounding the
    # SQLite backend applies before bucketing so both bucket identically.
    _METRICS_EPOCH_SQL = "ROUND(EXTRACT(EPOCH FROM timestamp)::numeric, 3)"

    def metrics_get_summary(self, microvm_id: str, start_epoch: float,
                            end_epoch: float) -> Optional[dict]:
        lo, hi = _mf.window_bounds(start_epoch, end_epoch)
        ep = self._METRICS_EPOCH_SQL
        where = "microvm_id = %s AND timestamp >= to_timestamp(%s) AND timestamp < to_timestamp(%s)"
        params = (microvm_id, lo, hi)
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                f"""SELECT COUNT(*) AS samples, MIN({ep})::float8 AS first_at,
                       MAX({ep})::float8 AS last_at,
                       AVG(cpu_pct)::float8 AS cpu_avg, MAX(cpu_pct)::float8 AS cpu_max,
                       AVG(mem_used_mb)::float8 AS mem_used_avg,
                       MAX(mem_used_mb)::float8 AS mem_used_max,
                       MAX(mem_pct)::float8 AS mem_pct_max, MAX(disk_pct)::float8 AS disk_pct_max,
                       MAX(disk_used_mb)::float8 AS disk_used_max
                    FROM vm_metrics WHERE {where}""",
                params,
            )
            agg = cur.fetchone()
            if not agg or not agg["samples"]:
                return None
            out = dict(agg)
            for col, key in (("cpu_pct", "cpu_max_at"), ("mem_used_mb", "mem_used_max_at"),
                             ("disk_pct", "disk_pct_max_at")):
                cur.execute(
                    f"""SELECT {ep}::float8 AS at FROM vm_metrics
                        WHERE {where} AND {col} IS NOT NULL
                        ORDER BY {col} DESC, timestamp ASC LIMIT 1""",
                    params,
                )
                row = cur.fetchone()
                out[key] = row["at"] if row else None
        return _mf.normalize_summary(out)

    def metrics_get_bucketed(self, microvm_id: str, start_epoch: float,
                             end_epoch: float, bucket_sec: int) -> list[dict]:
        lo, hi = _mf.window_bounds(start_epoch, end_epoch)
        bucket_sec = max(1, int(bucket_sec))
        ep = self._METRICS_EPOCH_SQL
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                f"""SELECT FLOOR(({ep} - %s) / %s)::BIGINT AS bucket,
                       MIN({ep})::float8 AS t_first, MAX({ep})::float8 AS t_last,
                       COUNT(*) AS samples,
                       AVG(cpu_pct)::float8 AS cpu_avg, MAX(cpu_pct)::float8 AS cpu_max,
                       AVG(mem_used_mb)::float8 AS mem_used_avg,
                       MAX(mem_used_mb)::float8 AS mem_used_max,
                       MAX(mem_pct)::float8 AS mem_pct_max, MAX(disk_pct)::float8 AS disk_pct_max,
                       MAX(disk_used_mb)::float8 AS disk_used_max
                    FROM vm_metrics
                    WHERE microvm_id = %s AND timestamp >= to_timestamp(%s)
                      AND timestamp < to_timestamp(%s)
                    GROUP BY 1 ORDER BY 1 ASC""",
                (lo, bucket_sec, microvm_id, lo, hi),
            )
            rows = cur.fetchall()
        return [_mf.normalize_bucket(dict(r)) for r in rows]

    def metrics_get_burst(self, microvm_id: str, baseline_mb: float) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                """WITH s AS (
                       SELECT ({ep})::float8 AS t, mem_used_mb::float8 AS u, id FROM vm_metrics
                       WHERE microvm_id = %(mid)s AND mem_used_mb > 0),
                    w AS (
                       SELECT t, u, LAG(t) OVER (ORDER BY t, id) AS pt,
                              LAG(u) OVER (ORDER BY t, id) AS pu FROM s)
                    SELECT COUNT(*) AS samples,
                       SUM(CASE WHEN u > %(base)s THEN 1 ELSE 0 END) AS burst_samples,
                       SUM(CASE WHEN u > %(base)s AND pt IS NOT NULL THEN t - pt ELSE 0 END)::float8 AS burst_seconds,
                       SUM(CASE WHEN u > %(base)s AND pt IS NOT NULL
                                THEN (u - %(base)s) * (t - pt) ELSE 0 END)::float8 AS burst_mb_seconds,
                       SUM(CASE WHEN u > %(base)s AND (pu IS NULL OR pu <= %(base)s)
                                THEN 1 ELSE 0 END) AS burst_episodes,
                       MAX(u)::float8 AS peak_mem_mb
                    FROM w""".replace("{ep}", self._METRICS_EPOCH_SQL),
                {"mid": microvm_id, "base": float(baseline_mb)},
            )
            row = cur.fetchone()
        return _mf.normalize_burst(dict(row)) if row else None
    def metrics_cleanup(self, hours: int = 24) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                "DELETE FROM vm_metrics WHERE timestamp < NOW() - (%s || ' hours')::interval",
                (str(hours),)
            )

    # ============================================================
    # AGENT SESSIONS (Strands SessionManager backing store)
    # ============================================================

    def agent_session_create(self, session_id: str, session: dict, notebook_id: str = None) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO agent_sessions (session_id, notebook_id, session_json, created_at, updated_at)
                   VALUES (%s, %s, %s, NOW(), NOW())
                   ON CONFLICT (session_id) DO UPDATE SET
                       session_json = EXCLUDED.session_json,
                       notebook_id  = COALESCE(EXCLUDED.notebook_id, agent_sessions.notebook_id),
                       updated_at   = NOW()""",
                (session_id, notebook_id, json.dumps(session)),
            )

    def agent_session_read(self, session_id: str) -> Optional[dict]:
        with self._db() as (conn, cur):
            cur.execute("SELECT session_json FROM agent_sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
        if not row:
            return None
        return json.loads(row[0] or "{}")

    def agent_create(self, session_id: str, agent_id: str, agent: dict) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """UPDATE agent_sessions SET agent_id = %s, agent_json = %s, updated_at = NOW()
                   WHERE session_id = %s""",
                (agent_id, json.dumps(agent), session_id),
            )

    def agent_read(self, session_id: str, agent_id: str) -> Optional[dict]:
        with self._db() as (conn, cur):
            cur.execute(
                "SELECT agent_json FROM agent_sessions WHERE session_id = %s AND agent_id = %s",
                (session_id, agent_id),
            )
            row = cur.fetchone()
        if not row or row[0] in (None, "", "{}"):
            return None
        return json.loads(row[0])

    def agent_update(self, session_id: str, agent_id: str, agent: dict) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """UPDATE agent_sessions SET agent_id = %s, agent_json = %s, updated_at = NOW()
                   WHERE session_id = %s""",
                (agent_id, json.dumps(agent), session_id),
            )

    def agent_message_create(self, session_id: str, agent_id: str, message_id: int,
                             message: dict, notebook_id: str = None) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO agent_messages
                       (session_id, agent_id, message_id, notebook_id, message_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                   ON CONFLICT (session_id, agent_id, message_id) DO UPDATE SET
                       message_json = EXCLUDED.message_json,
                       updated_at   = NOW()""",
                (session_id, agent_id, message_id, notebook_id, json.dumps(message)),
            )

    def agent_message_read(self, session_id: str, agent_id: str, message_id: int) -> Optional[dict]:
        with self._db() as (conn, cur):
            cur.execute(
                """SELECT message_json FROM agent_messages
                   WHERE session_id = %s AND agent_id = %s AND message_id = %s""",
                (session_id, agent_id, message_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def agent_message_update(self, session_id: str, agent_id: str, message_id: int, message: dict) -> None:
        with self._db() as (conn, cur):
            cur.execute(
                """UPDATE agent_messages SET message_json = %s, updated_at = NOW()
                   WHERE session_id = %s AND agent_id = %s AND message_id = %s""",
                (json.dumps(message), session_id, agent_id, message_id),
            )

    def agent_message_list(self, session_id: str, agent_id: str,
                           limit: Optional[int] = None, offset: int = 0) -> list[dict]:
        with self._db() as (conn, cur):
            if limit is None:
                cur.execute(
                    """SELECT message_json FROM agent_messages
                       WHERE session_id = %s AND agent_id = %s
                       ORDER BY message_id ASC OFFSET %s""",
                    (session_id, agent_id, max(0, int(offset))),
                )
            else:
                cur.execute(
                    """SELECT message_json FROM agent_messages
                       WHERE session_id = %s AND agent_id = %s
                       ORDER BY message_id ASC LIMIT %s OFFSET %s""",
                    (session_id, agent_id, max(0, int(limit)), max(0, int(offset))),
                )
            rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    def agent_session_delete(self, session_id: str) -> None:
        # agent_messages rows are removed by ON DELETE CASCADE.
        with self._db() as (conn, cur):
            cur.execute("DELETE FROM agent_sessions WHERE session_id = %s", (session_id,))

    def agent_transcript_get(self, session_id: str) -> list:
        from ._transcript import messages_to_bubbles
        with self._db() as (conn, cur):
            cur.execute(
                """SELECT message_json FROM agent_messages
                   WHERE session_id = %s ORDER BY message_id ASC""",
                (session_id,),
            )
            rows = cur.fetchall()
        return messages_to_bubbles([json.loads(r[0]) for r in rows])

    # ============================================================
    # GLOBAL DATA SOURCE ENTITIES
    # ============================================================

    def entity_upsert(self, source_id: str, source_type: str, doc_s3_key: str = None,
                      change_signal: dict = None, status: str = None) -> None:
        # One INSERT ... ON CONFLICT statement (no SELECT-then-INSERT race).
        # Partial update: only fields explicitly passed (non-None) overwrite the
        # existing row; last_discovered_at is always refreshed to NOW().
        signal = json.dumps(change_signal) if change_signal is not None else None
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO data_source_entities
                   (source_id, source_type, doc_s3_key, change_signal, status, last_discovered_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (source_id) DO UPDATE SET
                       source_type = EXCLUDED.source_type,
                       doc_s3_key = COALESCE(%s, data_source_entities.doc_s3_key),
                       change_signal = COALESCE(%s, data_source_entities.change_signal),
                       status = COALESCE(%s, data_source_entities.status),
                       last_discovered_at = NOW(),
                       updated_at = NOW()""",
                (source_id, source_type, doc_s3_key, signal, status or "pending",
                 doc_s3_key, signal, status)
            )

    def entity_get(self, source_id: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute("SELECT * FROM data_source_entities WHERE source_id = %s", (source_id,))
            row = cur.fetchone()
        if not row:
            return None
        d = self._normalize_entity(row)
        return d

    def entity_list(self, source_type: str = None) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            if source_type:
                cur.execute(
                    "SELECT * FROM data_source_entities WHERE source_type = %s ORDER BY source_id",
                    (source_type,)
                )
            else:
                cur.execute("SELECT * FROM data_source_entities ORDER BY source_id")
            rows = cur.fetchall()
        return [self._normalize_entity(r) for r in rows]

    @staticmethod
    def _normalize_entity(row) -> dict:
        d = dict(row)
        d["change_signal"] = json.loads(d["change_signal"]) if d.get("change_signal") else None
        return iso_utc_cols(d, ENTITY_TS)

    # ============================================================
    # LOCAL FILE ENTITIES (session-scoped)
    # ============================================================

    def local_entity_upsert(self, session_id: str, filepath: str, doc_s3_key: str = None,
                            change_signal: dict = None, status: str = None) -> None:
        # Same single-statement partial upsert as entity_upsert.
        signal = json.dumps(change_signal) if change_signal is not None else None
        with self._db() as (conn, cur):
            cur.execute(
                """INSERT INTO local_file_entities
                   (session_id, filepath, doc_s3_key, change_signal, status, last_discovered_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (session_id, filepath) DO UPDATE SET
                       doc_s3_key = COALESCE(%s, local_file_entities.doc_s3_key),
                       change_signal = COALESCE(%s, local_file_entities.change_signal),
                       status = COALESCE(%s, local_file_entities.status),
                       last_discovered_at = NOW(),
                       updated_at = NOW()""",
                (session_id, filepath, doc_s3_key, signal, status or "pending",
                 doc_s3_key, signal, status)
            )

    def local_entity_get(self, session_id: str, filepath: str) -> Optional[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT * FROM local_file_entities WHERE session_id = %s AND filepath = %s",
                (session_id, filepath)
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._normalize_entity(row)

    def local_entity_list(self, session_id: str) -> list[dict]:
        with self._db(dict_rows=True) as (conn, cur):
            cur.execute(
                "SELECT * FROM local_file_entities WHERE session_id = %s ORDER BY filepath",
                (session_id,)
            )
            rows = cur.fetchall()
        return [self._normalize_entity(r) for r in rows]
