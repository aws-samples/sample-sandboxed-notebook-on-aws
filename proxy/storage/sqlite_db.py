"""
SQLite implementation of the StorageBackend interface.

This is the default backend for local development. Stores data in a single
SQLite file at proxy/data/microvm.db.

For shared/cloud deployment use the Postgres backend (postgres_db.py,
STORAGE_BACKEND=postgres), which implements the same StorageBackend interface.
"""

import os
import json
import uuid
import sqlite3
import threading
import logging
from typing import Optional
from datetime import datetime, timezone
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

# Thin alias of the shared helper (proxy/storage/_timestamps.py): normalizes any
# stored timestamp text (isoformat, '...Z', or datetime('now')'s naive
# 'YYYY-MM-DD HH:MM:SS') to the ISO-8601 UTC string Postgres also returns.
_iso_utc = iso_utc

logger = logging.getLogger(__name__)

# Database file path. STORAGE_DB_PATH overrides it (then DB_DIR follows the
# override's directory); unset keeps the single default proxy/data/microvm.db.
DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "microvm.db")
if os.environ.get("STORAGE_DB_PATH"):
    DB_PATH = os.environ["STORAGE_DB_PATH"]
    DB_DIR = os.path.dirname(os.path.abspath(DB_PATH))

# Thread-local connections (SQLite doesn't allow cross-thread sharing)
_local = threading.local()


SCHEMA = """
-- Notebooks: replaces browser localStorage. `id` is a DB-generated integer
-- surrogate PK (AUTOINCREMENT) — the frontend no longer mints ids, so ids are
-- globally unique even across a shared/clustered backend. session_id (a client
-- UUID, stable for the notebook's lifetime) remains the durable external handle;
-- `id` is the fast numeric join key used by every FK below and the v_nb_* views.
CREATE TABLE IF NOT EXISTS notebooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    tag TEXT DEFAULT 'Drafts',
    session_id TEXT,
    microvm_id TEXT,
    checkpoint_enabled INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Notebook cells: one row per cell (replaces the old single cells_json blob on
-- the notebooks table). Keyed by the cell's PERMANENT id so provenance resolves
-- across reload/restore; `position` preserves notebook order. Deleting a notebook
-- cascades to its cells. Note: base64 `image` output is intentionally NOT stored
-- (largest field); only the S3 LOCATION of the chart PNG is (image_s3_key, a key
-- under notebook-outputs/<notebook_id>/…, validated by _cell_fields.valid_image_key).
-- No migration adds image_s3_key: an older DB fails fast in initialize() and needs
-- local_install.sh --AuroraSetup --AuroraReset --WarmPool (wipes the DBs).
CREATE TABLE IF NOT EXISTS notebook_cells (
    notebook_id INTEGER NOT NULL,
    cell_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    type TEXT DEFAULT 'code',
    code TEXT DEFAULT '',
    output TEXT,
    error TEXT,
    html TEXT,
    output_variable TEXT,
    execution_number INTEGER,
    execution_time REAL,
    last_executed_code TEXT,
    ai_explanation TEXT,
    ai_explanation_edited INTEGER DEFAULT 0,
    ai_explanation_source TEXT,
    generated TEXT,
    peak_mem_mb REAL,
    peak_cpu_pct REAL,
    executed_at TEXT,  -- ISO-8601 client clock when the cell last finished
    image_s3_key TEXT,  -- S3 key of the cell's chart PNG (notebook-outputs/…); never bytes
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- sync watermark
    PRIMARY KEY (notebook_id, cell_id),
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notebook_cells_pos ON notebook_cells(notebook_id, position);

-- Per-notebook DAG document (proxy/notebook/dag.py). One row per notebook; cascade
-- child of notebooks (no sync tombstone trigger, like notebook_cells). version is
-- stamped by storage (previous + 1) under a per-notebook write lock; dag_json is
-- the whole document as compact JSON (read by the v_nb_dag_* views).
CREATE TABLE IF NOT EXISTS notebook_dag (
    notebook_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0,
    built_at TEXT NOT NULL,
    dag_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);

-- Notebook Runs: one row per execution run (a single-cell run, a Run All, or a
-- run-from-cell). Write-only history so run summaries are queryable + durable
-- across devices; the live sidebar still derives its subline from cell state.
CREATE TABLE IF NOT EXISTS notebook_runs (
    run_id TEXT PRIMARY KEY,
    notebook_id INTEGER,
    session_id TEXT,
    microvm_id TEXT,                      -- VM the run executed on (join key)
    trigger TEXT DEFAULT 'single',       -- 'single' | 'run_all' | 'run_from'
    started_at TEXT,
    finished_at TEXT,
    cells_executed INTEGER DEFAULT 0,
    cells_errored INTEGER DEFAULT 0,
    cells_interrupted INTEGER DEFAULT 0,  -- cells stopped by the user (distinct from errored)
    total_exec_ms REAL DEFAULT 0,
    -- Per-cell breakdown for this run: [{cell_id, label, type, status, exec_ms}].
    -- Powers the per-cell drill-down in the run-history UI. Stored as a JSON blob
    -- (read only for one run at a time — no cross-run per-cell queries needed).
    -- Each entry may also carry the @param values that cell declared
    -- ({label: value}); the UI accumulates these across the cells in scope to
    -- show what a run/cell was parameterized with (US vs CA, horizon=45).
    run_cells_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))  -- sync watermark
);
CREATE INDEX IF NOT EXISTS idx_notebook_runs_notebook
    ON notebook_runs(notebook_id, finished_at DESC);

-- Notebook Versions (document history): a HEADER row per version group. Each group
-- is a point-in-time snapshot of the notebook's cell DOCUMENT (code + structure),
-- for "restore this version" + per-cell revert. Distinct from notebook_runs
-- (execution events) and the S3/VM checkpoint (kernel runtime). Append-only;
-- created only at deliberate checkpoints (manual save / a separate auto-version
-- interval / before an AI edit) — NOT on the per-keystroke autosave. Deleting a
-- notebook cascades to its versions (and cell rows cascade from the header).
CREATE TABLE IF NOT EXISTS notebook_versions (
    version_group_id TEXT PRIMARY KEY,
    notebook_id INTEGER NOT NULL,
    label TEXT,                          -- optional user-given name
    trigger TEXT DEFAULT 'manual',       -- 'manual' | 'auto' | 'pre_ai' | 'pre_restore'
    cell_count INTEGER DEFAULT 0,        -- denormalized count of snapshot rows
    fingerprint_hash TEXT,               -- hash of the versioned document (dedup non-manual snapshots)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- sync watermark
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);
-- NOTE: the idx_notebook_versions_fp index is created in _migrate() (AFTER the
-- ADD COLUMN), not here — on an EXISTING db the table already exists so the
-- CREATE TABLE above is a no-op and fingerprint_hash wouldn't exist yet when this
-- script runs, which would make an index-on-that-column statement fail.
CREATE INDEX IF NOT EXISTS idx_notebook_versions_notebook
    ON notebook_versions(notebook_id, created_at DESC);

-- Notebook Cell Versions: the lean per-cell snapshot rows for a version group.
-- Shaped like notebook_cells but DOCUMENT-ONLY — we deliberately omit runtime
-- fields (output/error/html, execution_number/time, last_executed_code, dataflow
-- vars_read/written, stale_reason): those regenerate on re-run and would make a
-- restored version show stale state from a different point. `created_at` is NOT
-- duplicated here — per-cell history joins to notebook_versions for the time.
CREATE TABLE IF NOT EXISTS notebook_cell_versions (
    version_group_id TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    type TEXT DEFAULT 'code',
    code TEXT DEFAULT '',
    output_variable TEXT,                -- SQL result var name (part of cell identity)
    ai_explanation TEXT,                 -- user/AI note on the cell
    ai_explanation_source TEXT,          -- 'ai' | 'user'
    generated TEXT,                      -- was the code AI-generated
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- sync watermark
    PRIMARY KEY (version_group_id, cell_id),
    FOREIGN KEY (version_group_id) REFERENCES notebook_versions(version_group_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notebook_cell_versions_cell
    ON notebook_cell_versions(cell_id);

-- VM Sessions: tracks each MicroVM lifecycle
CREATE TABLE IF NOT EXISTS vm_sessions (
    microvm_id TEXT PRIMARY KEY,
    notebook_id INTEGER,
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
    launched_at TEXT,
    terminated_at TEXT,
    idle_timeout_sec INTEGER,
    max_duration_sec INTEGER,
    checkpoint_enabled INTEGER DEFAULT 0,
    burst_mb_seconds REAL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))  -- sync watermark
    -- running_secs / suspended_secs / total_cost_usd intentionally REMOVED:
    -- they were derived aggregates. running/suspended are now reconstructed on
    -- read from vm_state_log via v_vm_sessions, and dollar cost is computed in
    -- Python (price_quantities). burst_mb_seconds stays — it's the only cost
    -- quantity NOT derivable from the state log (accumulated from metric polls).
);

-- VM Metrics: time-series data
CREATE TABLE IF NOT EXISTS vm_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    microvm_id TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_pct REAL,
    mem_pct REAL,
    mem_used_mb REAL,
    disk_pct REAL,
    disk_used_mb REAL,
    net_bytes_sent INTEGER,
    net_bytes_recv INTEGER,
    processes INTEGER,
    uptime_sec REAL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))  -- sync watermark
);

-- VM State Log: audit trail of state transitions
CREATE TABLE IF NOT EXISTS vm_state_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    microvm_id TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    old_state TEXT,
    new_state TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))  -- sync watermark
);

-- Agent sessions: durable chat-agent conversation history per notebook (the
-- Strands SessionManager backing store; replaces the old frontend-written
-- ai_sessions blob). One row per session_id (1:1 with a notebook). session_json
-- and agent_json hold the serialized Strands Session / SessionAgent dicts.
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    notebook_id INTEGER,
    agent_id TEXT,                       -- pinned to "AI Assistant" (1:1 model)
    session_json TEXT DEFAULT '{}',      -- serialized Strands Session dict
    agent_json TEXT DEFAULT '{}',        -- serialized Strands SessionAgent dict
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))  -- sync watermark
);

-- Agent messages: one row per (session_id, agent_id, message_id). message_json is
-- the serialized Strands SessionMessage dict. Cascade-child of agent_sessions, so
-- (like notebook_cells) it carries NO delete trigger — Aurora's ON DELETE CASCADE
-- removes them when the parent session row is deleted/replayed.
CREATE TABLE IF NOT EXISTS agent_messages (
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    notebook_id INTEGER,
    message_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- sync watermark
    PRIMARY KEY (session_id, agent_id, message_id),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, agent_id, message_id);

CREATE TABLE IF NOT EXISTS workbook_intel (
    session_id TEXT PRIMARY KEY,
    s3_key TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    version INTEGER NOT NULL DEFAULT 1
);

-- Notebook environment: the PERSISTED half of the "inject secrets/env into a VM"
-- feature. One row per notebook holding a JSON blob of { env_vars: {KEY: value},
-- secrets: [{name, arn, envVar, secretKey?}] }. We store secret *references*
-- (ARNs), never resolved secret VALUES — the VM's /run hook resolves them from
-- Secrets Manager at launch/restore under its own execution role. The proxy
-- re-sends this blob in every /launch and /resume body so the env survives
-- suspend/resume and relaunch. Cascade-deleted with the parent notebook.
CREATE TABLE IF NOT EXISTS notebook_env (
    notebook_id INTEGER PRIMARY KEY,
    env_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- sync watermark
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);

-- Global data-source entities: S3 files, Athena tables, DynamoDB tables.
-- Shared across all sessions/users — NOT session-scoped like workbook_intel.
CREATE TABLE IF NOT EXISTS data_source_entities (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    doc_s3_key TEXT,
    change_signal TEXT,
    status TEXT DEFAULT 'pending',
    last_discovered_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))  -- sync watermark
);

-- Local file entities: uploaded /tmp files, unique per session (NOT shared
-- like data_source_entities). Same shape/purpose, scoped by session_id.
CREATE TABLE IF NOT EXISTS local_file_entities (
    session_id TEXT NOT NULL,
    filepath TEXT NOT NULL,
    doc_s3_key TEXT,
    change_signal TEXT,
    status TEXT DEFAULT 'pending',
    last_discovered_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- sync watermark
    PRIMARY KEY (session_id, filepath)
);

-- Session registry: session_id → current VM. Authoritative "which VM serves
-- this session?" mapping. Single-instance today, but kept in the DB so a
-- horizontally-scaled proxy fleet can share it (see StorageBackend interface).
CREATE TABLE IF NOT EXISTS session_registry (
    session_id TEXT PRIMARY KEY,
    vm_id TEXT NOT NULL,
    endpoint TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Pool VMs: the VM Pool Manager's durable ledger of pre-booted spares. Owned by
-- the separate-process pool manager (pool_manager/) — the proxy only reads this
-- table. state is one of
-- 'launching'|'available'|'granting'|'granted'|'dead'. launched_at is the UTC
-- ISO clock both the 8h AWS lifetime and the age-recycle loop (§5.2) key off.
CREATE TABLE IF NOT EXISTS pool_vms (
    vm_id            TEXT PRIMARY KEY,
    architecture     TEXT NOT NULL,
    size_mib         INTEGER NOT NULL,
    endpoint         TEXT NOT NULL,
    fake_session_id  TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'launching',
    real_session_id  TEXT,
    launched_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_vms_pick
    ON pool_vms(architecture, size_mib, state, launched_at);

CREATE INDEX IF NOT EXISTS idx_vm_metrics_lookup
    ON vm_metrics(microvm_id, timestamp DESC);

-- Drives v_vm_sessions' per-VM interval reconstruction: the view partitions by
-- microvm_id and orders by id (LEAD over the transition log). (microvm_id, id)
-- lets the log be read per-VM in id order without a full scan + temp sort as the
-- log grows.
CREATE INDEX IF NOT EXISTS idx_vm_state_log_vm
    ON vm_state_log(microvm_id, id);

CREATE INDEX IF NOT EXISTS idx_vm_sessions_notebook
    ON vm_sessions(notebook_id);

-- Per-session cost rollup: get_session_cost() sums every VM that carried a
-- given session_id (original + all recovery/rotation replacements).
CREATE INDEX IF NOT EXISTS idx_vm_sessions_session
    ON vm_sessions(session_id);

-- Partial index for the "active VMs" filter (vm_session_list_active + the UI's
-- non-terminated cost view). PARTIAL (only non-terminated rows) so it stays
-- small even as terminated rows accumulate — over time most rows are TERMINATED,
-- and we only ever query the minority. A plain state index wouldn't help a
-- `state != 'TERMINATED'` scan; this indexes exactly the rows we select.
CREATE INDEX IF NOT EXISTS idx_vm_sessions_active
    ON vm_sessions(state) WHERE state != 'TERMINATED';

-- v_vm_sessions: single-source-of-truth read view. running_secs/suspended_secs
-- are RECONSTRUCTED from vm_state_log (summing each state interval between
-- consecutive transitions; the last non-TERMINATED row is measured to now),
-- centralizing the interval-sum that used to live only in Python
-- (CostTracker.compute). Exposes objective QUANTITIES only — dollar cost stays
-- in Python (pricing rates are config, not schema). This view is now the SOLE
-- source of running_secs/suspended_secs (the base-table cache columns were
-- dropped); only burst_mb_seconds is passed through from vm_sessions.
DROP VIEW IF EXISTS v_vm_sessions;
CREATE VIEW v_vm_sessions AS
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
        (julianday(
            COALESCE(next_ts, CASE WHEN state = 'TERMINATED' THEN start_ts ELSE 'now' END)
         ) - julianday(start_ts)) * 86400.0 AS secs
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
    ROUND(COALESCE(a.running_secs, 0)) AS running_secs,
    ROUND(COALESCE(a.suspended_secs, 0)) AS suspended_secs,
    COALESCE(a.resume_count, 0) AS resume_count,
    COALESCE(a.suspend_count, 0) AS suspend_count
FROM vm_sessions s
LEFT JOIN agg a ON a.microvm_id = s.microvm_id;

-- ============================================================================
-- CURATED READ-ONLY VIEWS (v_nb_*)  —  the notebook-facing contract
-- ============================================================================
-- These are the ONLY objects exposed to the read-only notebook user (the AI
-- agent + SQL cells query these, never the base tables). They:
--   • present a STABLE column contract that's identical in SQLite and Postgres
--     (only the view BODIES differ by dialect — JSON unnest + time math),
--   • normalize units (runs carry ms AND derived seconds; cell exec_time is
--     seconds at source),
--   • EXCLUDE heavy/sensitive bodies (code, output, error, html) so a query can
--     never leak cell contents or result data — only structure + timings.
-- They are GLOBAL (all notebooks); the agent filters by session_id / notebook
-- name against the view. Kept in lock-step with the Postgres definitions in
-- postgres_db.py — change both together.
-- Every view here is DROP VIEW IF EXISTS + CREATE VIEW (SQLite has no CREATE OR
-- REPLACE VIEW), so an EXISTING database picks up a changed view definition on
-- the next proxy start. Views hold no data — the base tables are untouched.

-- v_nb_notebook: one row per notebook — header + light rollups.
-- The notebook's primary key is exposed as `notebook_id` (NOT `id`) so the join
-- key has ONE name across every v_nb_* view (v_nb_notebook.notebook_id =
-- v_nb_runs.notebook_id = v_nb_cells.notebook_id, ...).
DROP VIEW IF EXISTS v_nb_notebook;
CREATE VIEW v_nb_notebook AS
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
FROM notebooks n;

-- v_nb_cells: current cell document — structure + timing only (NO code/output/
-- error/html). execution_time is stored in SECONDS on the base table. Dataflow
-- (reads/writes/sources) lives in the v_nb_dag_* views below.
DROP VIEW IF EXISTS v_nb_cells;
CREATE VIEW v_nb_cells AS
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
FROM notebook_cells c;

-- v_nb_runs: one row per execution run. total_exec_ms is milliseconds (source);
-- total_exec_sec is the derived seconds. duration_sec is wall-clock from the
-- start/finish timestamps. status is a friendly rollup.
DROP VIEW IF EXISTS v_nb_runs;
CREATE VIEW v_nb_runs AS
SELECT
    r.run_id,
    r.notebook_id,
    r.session_id,
    r.microvm_id,
    r.trigger,
    r.started_at,
    r.finished_at,
    CASE
        WHEN r.started_at IS NOT NULL AND r.finished_at IS NOT NULL
        THEN ROUND((julianday(r.finished_at) - julianday(r.started_at)) * 86400.0, 3)
        ELSE NULL
    END AS duration_sec,
    r.cells_executed,
    r.cells_errored,
    r.cells_interrupted,
    r.total_exec_ms,
    ROUND(r.total_exec_ms / 1000.0, 3) AS total_exec_sec,
    CASE
        WHEN r.cells_errored > 0 THEN 'error'
        WHEN r.cells_interrupted > 0 THEN 'interrupted'
        ELSE 'ok'
    END AS status,
    r.created_at
FROM notebook_runs r;

-- v_nb_cell_runs: per-cell timing, one row per cell per run — UNNESTS the
-- run_cells_json blob ([{cell_id,label,type,status,exec_ms}, ...]). This is the
-- view that answers "which cell is slowest", "cell execution times", etc.
-- SQLite dialect: json_each + json_extract.
DROP VIEW IF EXISTS v_nb_cell_runs;
CREATE VIEW v_nb_cell_runs AS
SELECT
    r.run_id,
    r.notebook_id,
    r.session_id,
    json_extract(j.value, '$.cell_id') AS cell_id,
    json_extract(j.value, '$.label')   AS label,
    json_extract(j.value, '$.type')    AS type,
    json_extract(j.value, '$.status')  AS status,
    json_extract(j.value, '$.exec_ms') AS exec_ms,
    ROUND(CAST(json_extract(j.value, '$.exec_ms') AS REAL) / 1000.0, 3) AS exec_sec,
    -- Per-cell client-clock execution span (ISO strings) for the timeline, and the
    -- guest-sampled peak resource use. All read from the run_cells_json blob; NULL
    -- for runs recorded before these fields were captured.
    json_extract(j.value, '$.started_at')   AS cell_started_at,
    json_extract(j.value, '$.finished_at')  AS cell_finished_at,
    json_extract(j.value, '$.peak_mem_mb')  AS peak_mem_mb,
    json_extract(j.value, '$.peak_cpu_pct') AS peak_cpu_pct,
    r.finished_at
FROM notebook_runs r,
     json_each(r.run_cells_json) j;

-- v_nb_versions: one row per document version snapshot.
DROP VIEW IF EXISTS v_nb_versions;
CREATE VIEW v_nb_versions AS
SELECT
    v.version_group_id,
    v.notebook_id,
    v.label,
    v.trigger,
    CASE WHEN v.trigger = 'baseline' THEN 1 ELSE 0 END AS is_baseline,
    v.cell_count,
    v.created_at
FROM notebook_versions v;

-- v_nb_version_cells: per-cell snapshot rows for a version, joined to the header
-- for the version's created_at/notebook_id. Document structure only (NO code).
DROP VIEW IF EXISTS v_nb_version_cells;
CREATE VIEW v_nb_version_cells AS
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
JOIN notebook_versions v ON v.version_group_id = cv.version_group_id;

-- v_nb_session_cost: per-SESSION VM cost QUANTITIES (NO dollars — pricing rates
-- are Python/config, not schema, so the notebook applies them via the
-- estimate_session_cost() helper). A session can be served by MULTIPLE VMs over
-- its life (rotation/recovery), so this aggregates across every vm_session row
-- carrying the session_id. running_secs/suspended_secs are reconstructed from
-- vm_state_log exactly as v_vm_sessions does (sum each state interval between
-- consecutive transitions; the last non-TERMINATED interval is measured to now),
-- then summed per session. burst_mb_seconds sums the per-VM accumulated column.
-- memory_mib is the representative (max) tier across the session's VMs, and
-- architecture is one of the session's VMs' arch (they share a tier normally).
-- This is the SINGLE row a notebook needs to compute session cost. SQLite dialect:
-- julianday() for interval seconds (Postgres uses EXTRACT(EPOCH ...)).
DROP VIEW IF EXISTS v_nb_session_cost;
CREATE VIEW v_nb_session_cost AS
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
        (julianday(
            COALESCE(next_ts, CASE WHEN state = 'TERMINATED' THEN start_ts ELSE 'now' END)
         ) - julianday(start_ts)) * 86400.0 AS secs
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
    -- Denormalized name (survives notebook deletion). MAX() picks a non-NULL name
    -- if any VM in the session carried one; NULL for pre-migration sessions.
    MAX(s.notebook_name) AS notebook_name,
    COUNT(*) AS vm_count,
    ROUND(COALESCE(SUM(p.running_secs), 0)) AS running_secs,
    ROUND(COALESCE(SUM(p.suspended_secs), 0)) AS suspended_secs,
    ROUND(COALESCE(SUM(s.burst_mb_seconds), 0), 1) AS burst_mb_seconds,
    COALESCE(SUM(p.resume_count), 0) AS resume_count,
    COALESCE(SUM(p.suspend_count), 0) AS suspend_count,
    MAX(s.memory_mib) AS memory_mib,
    MAX(s.architecture) AS architecture
FROM vm_sessions s
LEFT JOIN per_vm p ON p.microvm_id = s.microvm_id
WHERE s.session_id IS NOT NULL
GROUP BY s.session_id;

-- ── Per-notebook DAG views (notebook_dag.dag_json unnested) ─────────────────
-- Column names/order identical to the Postgres definitions. JSON-array columns
-- (*_json) are compact JSON text here and jsonb text in Postgres (compare parsed).
-- v_nb_dag_nodes: one row per DAG node (code/sql cell; markdown cells are not nodes).
DROP VIEW IF EXISTS v_nb_dag_nodes;
CREATE VIEW v_nb_dag_nodes AS
SELECT d.notebook_id,
       json_extract(n.value, '$.cell_id')                    AS cell_id,
       CAST(json_extract(n.value, '$.position') AS INTEGER)  AS position,
       CAST(json_extract(n.value, '$.level') AS INTEGER)     AS level,
       json_extract(n.value, '$.status')                     AS status,
       json_extract(n.value, '$.facts_origin')               AS facts_origin,
       json_extract(n.value, '$.reads')                      AS reads_json,
       json_extract(n.value, '$.writes')                     AS writes_json,
       json_extract(n.value, '$.mutates')                    AS mutates_json,
       CASE WHEN json_type(n.value, '$.sources') = 'array'
            THEN json_extract(n.value, '$.sources') END      AS sources_json,
       json_extract(n.value, '$.cell_type')                  AS cell_type,
       json_extract(n.value, '$.executed_at')                AS executed_at,
       CAST(json_extract(n.value, '$.last_run_seq') AS INTEGER) AS last_run_seq,
       json_extract(n.value, '$.stale_reason.kind')          AS stale_kind,
       d.version AS dag_version, d.built_at
FROM notebook_dag d, json_each(d.dag_json, '$.nodes') AS n;

-- v_nb_dag_edges: one row per dependency edge (from produces var, to uses it).
DROP VIEW IF EXISTS v_nb_dag_edges;
CREATE VIEW v_nb_dag_edges AS
SELECT d.notebook_id,
       json_extract(e.value, '$.from')   AS from_cell_id,
       json_extract(e.value, '$.to')     AS to_cell_id,
       json_extract(e.value, '$.var')    AS var_name,
       json_extract(e.value, '$.kind')   AS kind,
       json_extract(e.value, '$.origin') AS origin,
       json_extract(e.value, '$.use')    AS use_kind,
       CASE WHEN json_extract(e.value, '$.backward') THEN 1 ELSE 0 END AS is_backward,
       d.version AS dag_version
FROM notebook_dag d, json_each(d.dag_json, '$.edges') AS e;

-- v_nb_dag_issues: one row per dependency issue (cycle, out_of_order, ...).
DROP VIEW IF EXISTS v_nb_dag_issues;
CREATE VIEW v_nb_dag_issues AS
SELECT d.notebook_id,
       json_extract(i.value, '$.cell_id')     AS cell_id,
       json_extract(i.value, '$.code')        AS code,
       json_extract(i.value, '$.severity')    AS severity,
       json_extract(i.value, '$.var')         AS var_name,
       json_extract(i.value, '$.detail')      AS detail,
       json_extract(i.value, '$.related')     AS related_json,
       json_extract(i.value, '$.source.kind') AS source_kind,
       json_extract(i.value, '$.source.id')   AS source_id,
       d.version AS dag_version
FROM notebook_dag d, json_each(d.dag_json, '$.issues') AS i;

-- v_nb_dag_lineage: all (transitive) upstream cells per cell. UNION (not UNION
-- ALL) de-duplicates the finite (notebook, cell, upstream) pairs, so the
-- recursion terminates on cycles without a depth cap (a cell on a cycle lists
-- itself as its own upstream).
DROP VIEW IF EXISTS v_nb_dag_lineage;
CREATE VIEW v_nb_dag_lineage AS
WITH RECURSIVE e(notebook_id, from_id, to_id) AS (
    SELECT DISTINCT d.notebook_id, json_extract(x.value, '$.from'), json_extract(x.value, '$.to')
    FROM notebook_dag d, json_each(d.dag_json, '$.edges') AS x
),
up(notebook_id, cell_id, upstream_cell_id) AS (
    SELECT notebook_id, to_id, from_id FROM e
    UNION
    SELECT up.notebook_id, up.cell_id, e.from_id
    FROM up JOIN e ON e.notebook_id = up.notebook_id AND e.to_id = up.upstream_cell_id
)
SELECT notebook_id, cell_id, upstream_cell_id FROM up;
"""


# ============================================================================
# SYNC SCHEMA (SQLite-ONLY)  —  bookkeeping for the generic SQLite→Aurora sync
# ============================================================================
# These objects exist ONLY on SQLite (the local operational store / sync SOURCE).
# They are NEVER created on Postgres (Aurora is the sync TARGET and must stay
# clean — no tombstones, no triggers). All names use the `_sync_` prefix so the
# sync bookkeeping is visually separated from operational tables and easy to
# exclude/reset independently.
#
# Model (see the design discussion): the sync is STATE-based for inserts/updates
# (watermark = each table's updated_at, or generated_at for workbook_intel) and
# TOMBSTONE-based for deletes. A per-table AFTER DELETE trigger records the
# deleted row's primary key (as JSON, so single AND composite PKs are handled
# uniformly) into _sync_deletes. The generic worker replays upserts (rows where
# watermark > cursor) then deletes (tombstones where seq > delete_cursor),
# advancing both cursors after a successful Aurora transaction.
SYNC_SCHEMA = """
-- Delete tombstones: one row per deleted operational row. pk_json holds the PK
-- as a JSON object ({"id":42} or {"notebook_id":1,"cell_id":"c3"}) so the worker
-- can build a WHERE clause for single or composite keys uniformly. `seq` is a
-- monotonic cursor the worker advances (replay WHERE seq > delete_cursor).
CREATE TABLE IF NOT EXISTS _sync_deletes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    pk_json TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sync_deletes_seq ON _sync_deletes(seq);

-- Per-table UPSERT cursor: the max watermark (updated_at/generated_at) the worker
-- has successfully replayed for each table, plus the wall-clock of that cycle.
-- Keyed by table_name so a lagging/failed table retries from its own last-good
-- point without re-scanning the others.
CREATE TABLE IF NOT EXISTS _sync_cursor (
    table_name TEXT PRIMARY KEY,
    last_watermark TEXT,
    last_synced_at TEXT
);

-- DELETE cursor: single row holding the max _sync_deletes.seq successfully
-- applied to Aurora. A 1-row table (id fixed at 1) keeps it trivially readable.
CREATE TABLE IF NOT EXISTS _sync_delete_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seq INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO _sync_delete_cursor (id, last_seq) VALUES (1, 0);

-- Sync run audit log: one row per sync cycle for observability (when the last
-- good sync ran, what failed). Cheap insurance for a background replicator.
CREATE TABLE IF NOT EXISTS _sync_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT,                 -- 'ok' | 'error' | 'partial'
    rows_upserted INTEGER DEFAULT 0,
    rows_deleted INTEGER DEFAULT 0,
    error TEXT
);

-- ── AFTER DELETE triggers: ONE PER ROOT (non-cascade-child) TABLE ───────────
-- Each records the deleted row's PK into _sync_deletes as a JSON object. Single-PK
-- tables emit {"pk":val}; composite-PK tables emit every key column so the worker's
-- delete can match.
--
-- DELIBERATELY OMITTED: cascade-child tables (notebook_cells, notebook_cell_versions).
-- These are children via an ON DELETE CASCADE FK, so the sync worker does NOT need
-- per-child delete tombstones for them:
--   • Parent deletion — when the worker deletes a parent (notebooks /
--     notebook_versions) in Aurora, Aurora's matching ON DELETE CASCADE removes the
--     children automatically. A per-child tombstone would be redundant.
--   • In-place child changes — notebook_cells is delete+reinserted wholesale on
--     EVERY save (_write_cells), which would otherwise emit N delete tombstones per
--     save (measured: thousands of rows of pure churn). The worker instead syncs
--     these parent-scoped: when the parent's updated_at advances, it replaces that
--     parent's child rows in Aurora. Removed cells are caught there; unchanged cells
--     re-sync via their updated_at watermark. So NO delete tracking is needed here.
-- This split is DERIVABLE generically: a table is a cascade-child iff
-- PRAGMA foreign_key_list(t) has an fk with on_delete='CASCADE'. The worker computes
-- that from the FK graph it already discovers for topological ordering, so the trigger
-- set here matches the rule "tombstone root tables only".
CREATE TRIGGER IF NOT EXISTS trg_sync_del_notebooks
AFTER DELETE ON notebooks BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('notebooks', json_object('id', OLD.id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_notebook_runs
AFTER DELETE ON notebook_runs BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('notebook_runs', json_object('run_id', OLD.run_id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_notebook_versions
AFTER DELETE ON notebook_versions BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('notebook_versions', json_object('version_group_id', OLD.version_group_id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_vm_sessions
AFTER DELETE ON vm_sessions BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('vm_sessions', json_object('microvm_id', OLD.microvm_id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_vm_metrics
AFTER DELETE ON vm_metrics BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('vm_metrics', json_object('id', OLD.id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_vm_state_log
AFTER DELETE ON vm_state_log BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('vm_state_log', json_object('id', OLD.id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_agent_sessions
AFTER DELETE ON agent_sessions BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('agent_sessions', json_object('session_id', OLD.session_id));
END;
-- NOTE: agent_messages is a cascade-child of agent_sessions (FK ON DELETE CASCADE),
-- so it deliberately has NO delete trigger — Aurora removes its rows via cascade
-- when the parent agent_sessions row is deleted (same pattern as notebook_cells).

CREATE TRIGGER IF NOT EXISTS trg_sync_del_workbook_intel
AFTER DELETE ON workbook_intel BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('workbook_intel', json_object('session_id', OLD.session_id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_data_source_entities
AFTER DELETE ON data_source_entities BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('data_source_entities', json_object('source_id', OLD.source_id));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_local_file_entities
AFTER DELETE ON local_file_entities BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('local_file_entities', json_object('session_id', OLD.session_id, 'filepath', OLD.filepath));
END;

CREATE TRIGGER IF NOT EXISTS trg_sync_del_session_registry
AFTER DELETE ON session_registry BEGIN
    INSERT INTO _sync_deletes (table_name, pk_json)
    VALUES ('session_registry', json_object('session_id', OLD.session_id));
END;
"""


def _schema_stmt(start: str, end: str) -> str:
    """Slice one statement out of SCHEMA, from `start` through the first `end`
    after it. Raises at import if SCHEMA no longer contains it."""
    i = SCHEMA.index(start)
    return SCHEMA[i:SCHEMA.index(end, i) + len(end)]


# The subset of SCHEMA the standalone pool_manager needs (initialize_pool_manager_
# schema): its pool_vms ledger + pick index, and the session_registry /
# vm_sessions tables it reads. Sliced out of SCHEMA rather than copied, so the
# DDL cannot drift. All CREATE ... IF NOT EXISTS: never drops or alters a table.
POOL_MANAGER_SCHEMA = "\n".join([
    _schema_stmt("CREATE TABLE IF NOT EXISTS vm_sessions (", "\n);"),
    _schema_stmt("CREATE TABLE IF NOT EXISTS session_registry (", "\n);"),
    _schema_stmt("CREATE TABLE IF NOT EXISTS pool_vms (", "\n);"),
    _schema_stmt("CREATE INDEX IF NOT EXISTS idx_pool_vms_pick", ";"),
]) + "\n"


# In-process recovery-lock state (SQLite = single-instance semantics).
# A module-level lock guards a set of session_ids currently "held". This gives
# correct mutual exclusion within one proxy process, which is all SQLite ever
# needs. Postgres uses pg_try_advisory_lock for cross-instance exclusion.
_LOCK_GUARD = threading.Lock()
_HELD_SESSION_LOCKS: set[str] = set()


class SqliteStorage(StorageBackend):
    """SQLite-based storage backend for local development."""

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection."""
        if not hasattr(_local, "conn") or _local.conn is None:
            os.makedirs(DB_DIR, exist_ok=True)
            _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _local.conn.row_factory = sqlite3.Row
            _local.conn.execute("PRAGMA journal_mode=WAL")
            _local.conn.execute("PRAGMA foreign_keys=ON")
            _local.conn.execute("PRAGMA busy_timeout=5000")
        return _local.conn

    @contextmanager
    def _db(self):
        """Context manager for database operations with auto-commit."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ============================================================
    # LIFECYCLE
    # ============================================================

    def initialize(self, connection_string: str = "") -> None:
        """Initialize SQLite database. connection_string is ignored (uses local file).
        SCHEMA uses CREATE TABLE/INDEX IF NOT EXISTS (existing tables + data are left
        untouched) and DROP VIEW IF EXISTS + CREATE VIEW for every view (so a changed
        view definition is picked up on the next start). Safe to run on every startup."""
        os.makedirs(DB_DIR, exist_ok=True)
        with self._db() as conn:
            # Fail fast (not a migration) BEFORE any DDL: an existing notebook_cells
            # without image_s3_key is a pre-chart-persistence DB.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(notebook_cells)").fetchall()}
            if cols and "image_s3_key" not in cols:
                raise RuntimeError(SCHEMA_RESET_MESSAGE)
            conn.executescript(SCHEMA)
            self._migrate(conn)
            # Sync bookkeeping tables + AFTER DELETE tombstone triggers. SQLite-only
            # (the local operational store is the sync SOURCE; Aurora is the TARGET
            # and never carries these). Runs AFTER the operational SCHEMA so the
            # triggers' target tables already exist. All IF NOT EXISTS → idempotent.
            conn.executescript(SYNC_SCHEMA)
        logger.info(f"Database initialized: {DB_PATH}")

    def initialize_pool_manager_schema(self) -> None:
        """Create the pool_manager's tables if missing (see StorageBackend). Runs
        only POOL_MANAGER_SCHEMA: no view rebuild, no migration, no sync triggers."""
        os.makedirs(DB_DIR, exist_ok=True)
        with self._db() as conn:
            conn.executescript(POOL_MANAGER_SCHEMA)
        logger.info(f"pool_manager schema ensured: {DB_PATH}")

    def _migrate(self, conn) -> None:
        """Idempotent, additive migrations for DBs created before a column existed.
        SQLite's CREATE TABLE IF NOT EXISTS won't add new columns to an existing
        table, so add them here. ADD COLUMN raises 'duplicate column name' when the
        column is already there; only that error is tolerated, anything else is
        logged."""
        add_columns = [
            ("notebook_versions", "fingerprint_hash", "TEXT"),
        ]
        for table, col, coltype in add_columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    logger.warning(f"migration ADD COLUMN {table}.{col} failed: {e}")
            except Exception:
                logger.exception(f"migration ADD COLUMN {table}.{col} failed")
        # Index the dedup column AFTER the ADD COLUMN above, so it's safe on an
        # existing DB where the column was just added (can't live in SCHEMA — that
        # runs before this migration).
        # idx_vm_metrics_ts drives metrics_cleanup's global `timestamp < cutoff`
        # delete (the existing idx_vm_metrics_lookup leads with microvm_id, so it
        # can't serve it).
        for name, ddl in (
            ("idx_notebook_versions_fp",
             "CREATE INDEX IF NOT EXISTS idx_notebook_versions_fp "
             "ON notebook_versions(notebook_id, fingerprint_hash)"),
            ("idx_vm_metrics_ts",
             "CREATE INDEX IF NOT EXISTS idx_vm_metrics_ts ON vm_metrics(timestamp)"),
        ):
            try:
                conn.execute(ddl)
            except Exception:
                logger.exception(f"migration CREATE INDEX {name} failed")

    # ============================================================
    # NOTEBOOKS
    # ============================================================

    # ---- Per-cell (de)serialization helpers ----

    @staticmethod
    def _cell_to_row(notebook_id: str, position: int, c: dict) -> tuple:
        """Flatten a client cell dict into a notebook_cells row tuple. `image`
        (base64) is intentionally dropped; only a validated `imageS3Key` (this
        notebook's/cell's own key) is kept. Missing id is tolerated (skipped by caller)."""
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

    _CELL_COLUMNS = (
        "notebook_id, cell_id, position, type, code, output, error, html, "
        "output_variable, execution_number, execution_time, last_executed_code, "
        "ai_explanation, ai_explanation_edited, ai_explanation_source, generated, "
        "peak_mem_mb, peak_cpu_pct, executed_at, image_s3_key"
    )
    _CELL_COLUMN_COUNT = len([c for c in _CELL_COLUMNS.split(",") if c.strip()])

    @staticmethod
    def _row_to_cell(r) -> dict:
        """Reassemble a client cell dict from a notebook_cells row."""
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

    def _write_cells(self, conn, notebook_id: str, cells: list) -> None:
        """Replace ALL cell rows for a notebook (delete + re-insert in order).
        Cells without an id are skipped (can't be addressed/restored)."""
        conn.execute("DELETE FROM notebook_cells WHERE notebook_id = ?", (notebook_id,))
        # Positions are numbered over the KEPT cells (gap-free), so they match
        # the DAG document's node.position (built from the same kept list).
        kept = [c for c in (cells or []) if c.get("id") is not None]
        rows = [self._cell_to_row(notebook_id, i, c) for i, c in enumerate(kept)]
        if rows:
            placeholders = "(" + ", ".join(["?"] * self._CELL_COLUMN_COUNT) + ")"
            conn.executemany(
                f"INSERT INTO notebook_cells ({self._CELL_COLUMNS}) VALUES {placeholders}",
                rows,
            )

    def _read_cells(self, conn, notebook_id: str) -> list:
        rows = conn.execute(
            "SELECT * FROM notebook_cells WHERE notebook_id = ? ORDER BY position ASC",
            (notebook_id,),
        ).fetchall()
        return [self._row_to_cell(r) for r in rows]

    # ---- Per-notebook DAG document (notebook_dag) ----

    def _dag_rmw(self, conn, notebook_id, build, with_cells: bool):
        """Read-modify-write of the notebook_dag row on `conn`, inside the CALLER's
        transaction (which must already hold the SQLite write lock: BEGIN IMMEDIATE
        in notebook_dag_update, the preceding UPDATE/INSERT in notebook_update /
        notebook_create). Returns (found, DagWrite|None): found=False (and None)
        when the notebook does not exist. Nothing is written when build returned
        None, the rebuilt doc is unchanged (graph and runtime hashes equal) or
        too large to store; the DagWrite then has wrote=False and doc=prev."""
        nb = conn.execute("SELECT id FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        if not nb:
            return False, None
        nid = nb["id"]
        row = conn.execute(
            "SELECT version, built_at, dag_json FROM notebook_dag WHERE notebook_id = ?",
            (nid,),
        ).fetchone()
        prev = load_stored_doc(row["dag_json"], row["version"], row["built_at"], nid) if row else None
        if with_cells:
            new = build(prev, self._read_cells(conn, nid))
        else:
            new = build(prev)
        if new is None:
            return True, unchanged_write(prev)
        w, text = prepare_write(new, prev, row["version"] if row else 0, nid)
        if text is None:
            return True, w
        doc = w.doc
        conn.execute(
            "INSERT INTO notebook_dag (notebook_id, version, built_at, dag_json, updated_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(notebook_id) DO UPDATE SET version=excluded.version, "
            "built_at=excluded.built_at, dag_json=excluded.dag_json, "
            "updated_at=excluded.updated_at",
            (nid, doc["version"], doc["built_at"], text),
        )
        return True, w

    def notebook_dag_get(self, notebook_id) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT notebook_id, version, built_at, dag_json FROM notebook_dag "
                "WHERE notebook_id = ?", (notebook_id,),
            ).fetchone()
        if not row:
            return None
        return load_stored_doc(row["dag_json"], row["version"], row["built_at"],
                               row["notebook_id"])

    def notebook_dag_write(self, notebook_id, build, *, with_cells: bool = False):
        with self._db() as conn:
            # Take the write lock BEFORE reading so concurrent RMWs serialize
            # (busy_timeout makes the others wait instead of failing).
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            _found, w = self._dag_rmw(conn, notebook_id, build, with_cells)
        return w

    def notebook_dag_update(self, notebook_id, build, *, with_cells: bool = False) -> Optional[dict]:
        w = self.notebook_dag_write(notebook_id, build, with_cells=with_cells)
        return w.doc if w else None

    def notebook_id_for_session(self, session_id) -> Optional[int]:
        if not session_id:
            return None
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id FROM notebooks WHERE session_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 2",
                (session_id,),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            logger.debug("notebook_id_for_session: session %s is linked to more than one "
                         "notebook; using the most recently updated (id %s)",
                         session_id, rows[0]["id"])
        return int(rows[0]["id"])

    def notebook_create(self, name: str, description: str = "",
                        tag: str = "Drafts", cells: list = None,
                        session_id: str = None, dag_build=None) -> dict:
        """Create a notebook and RETURN it with the DB-generated integer `id`.

        The id is assigned by the DB (AUTOINCREMENT), not the caller — this is the
        create-returns-id contract the frontend relies on (POST first, adopt the
        returned id). session_id is optional (the client's stable UUID handle).
        dag_build (optional) runs in the same transaction after the cells are
        written; the stored doc is returned under "dag"."""
        now = datetime.now(timezone.utc).isoformat()
        dag = None
        with self._db() as conn:
            cursor = conn.execute(
                "INSERT INTO notebooks (name, description, tag, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, description, tag, session_id, now, now)
            )
            notebook_id = cursor.lastrowid
            self._write_cells(conn, notebook_id, cells or [])
            if dag_build is not None:
                _found, w = self._dag_rmw(conn, notebook_id, dag_build, False)
                dag = w.doc if w else None
        result = {"id": notebook_id, "name": name, "description": description,
                  "tag": tag, "session_id": session_id,
                  "cells": echo_cells_with_valid_keys(notebook_id, cells),
                  "created_at": now}
        if dag is not None:
            result["dag"] = dag
        return result

    def notebook_update(self, notebook_id: str, *, dag_build=None, **kwargs) -> bool:
        allowed = {"name", "description", "tag", "session_id", "microvm_id", "checkpoint_enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        has_cells = "cells" in kwargs
        if not updates and not has_cells:
            return False
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [notebook_id]
        with self._db() as conn:
            cursor = conn.execute(f"UPDATE notebooks SET {set_clause} WHERE id = ?", values)
            if cursor.rowcount == 0:
                return False  # notebook doesn't exist — caller handles upsert
            if has_cells:
                self._write_cells(conn, notebook_id, kwargs["cells"] or [])
                # Same transaction: the UPDATE above already holds the write lock,
                # so a _write_cells failure rolls back the DAG write too.
                if dag_build is not None:
                    self._dag_rmw(conn, notebook_id, dag_build, False)
        return True

    def notebook_cell_image_keys(self, notebook_id) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT cell_id, image_s3_key FROM notebook_cells "
                "WHERE notebook_id = ? AND image_s3_key IS NOT NULL AND image_s3_key <> ''",
                (notebook_id,),
            ).fetchall()
        return {str(r["cell_id"]): r["image_s3_key"] for r in rows}

    def notebook_cell_image_keys_all(self, notebook_id) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT cell_id, image_s3_key, html FROM notebook_cells "
                "WHERE notebook_id = ? AND ((image_s3_key IS NOT NULL AND image_s3_key <> '') "
                "OR substr(html, 1, 14) = ?)",
                (notebook_id, OUTPUTS_ENVELOPE_PREFIX),
            ).fetchall()
        out = {}
        for r in rows:
            keys = cell_all_image_keys(notebook_id, r["cell_id"], r["image_s3_key"], r["html"])
            if keys:
                out[str(r["cell_id"])] = keys
        return out

    def notebook_get(self, notebook_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
            if not row:
                return None
            cells = self._read_cells(conn, notebook_id)
        return self._row_to_notebook(row, cells)

    def notebook_list(self) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM notebooks ORDER BY updated_at DESC").fetchall()
            result = []
            for r in rows:
                cells = self._read_cells(conn, r["id"])
                result.append(self._row_to_notebook(r, cells))
        return result

    # ---- Notebook-owned session cascade (shared by notebook_delete and
    # session_intel_delete). Private helpers: they run inside the caller's
    # transaction on the caller's connection. ----

    @staticmethod
    def _notebook_session_ids(conn, notebook_id) -> list:
        """Every session id this notebook has had. notebooks.session_id is NULL
        after /terminate (notebook_clear_session), so the kept history tables
        (vm_sessions, notebook_runs) and agent_sessions are consulted too."""
        rows = conn.execute(
            """SELECT session_id FROM notebooks WHERE id = ?
               UNION SELECT session_id FROM vm_sessions WHERE notebook_id = ?
               UNION SELECT session_id FROM agent_sessions WHERE notebook_id = ?
               UNION SELECT session_id FROM notebook_runs WHERE notebook_id = ?""",
            (notebook_id, notebook_id, notebook_id, notebook_id),
        ).fetchall()
        return sorted({r[0] for r in rows if r[0]})

    @staticmethod
    def _session_used_elsewhere(conn, session_id, notebook_id) -> bool:
        """True if another SURVIVING notebook (row still in notebooks) references
        session_id. Two notebooks can share one session id, so its rows are then
        not this notebook's to delete."""
        row = conn.execute(
            """SELECT 1 FROM notebooks WHERE session_id = ? AND id <> ?
               UNION ALL SELECT 1 FROM vm_sessions x JOIN notebooks n ON n.id = x.notebook_id
                   WHERE x.session_id = ? AND x.notebook_id <> ?
               UNION ALL SELECT 1 FROM agent_sessions x JOIN notebooks n ON n.id = x.notebook_id
                   WHERE x.session_id = ? AND x.notebook_id <> ?
               UNION ALL SELECT 1 FROM notebook_runs x JOIN notebooks n ON n.id = x.notebook_id
                   WHERE x.session_id = ? AND x.notebook_id <> ?
               LIMIT 1""",
            (session_id, notebook_id) * 4,
        ).fetchone()
        return row is not None

    @staticmethod
    def _delete_session_intel(conn, session_id) -> int:
        """Delete the workbook_intel + local_file_entities rows of one session."""
        n = conn.execute("DELETE FROM workbook_intel WHERE session_id = ?", (session_id,)).rowcount
        n += conn.execute("DELETE FROM local_file_entities WHERE session_id = ?", (session_id,)).rowcount
        return n

    def notebook_delete(self, notebook_id: str) -> bool:
        with self._db() as conn:
            # The session ids this notebook OWNS (it had them, and no other surviving
            # notebook references them). Computed BEFORE any delete: agent_sessions
            # is one of the sources. Works after /terminate cleared
            # notebooks.session_id, because vm_sessions / notebook_runs are kept.
            owned = [
                sid for sid in self._notebook_session_ids(conn, notebook_id)
                if not self._session_used_elsewhere(conn, sid, notebook_id)
            ]

            # Explicit child deletes in case PRAGMA foreign_keys isn't honored on this
            # connection. Version cell rows first (they FK the version headers), then
            # the headers, then live cells, then the notebook itself.
            conn.execute(
                """DELETE FROM notebook_cell_versions WHERE version_group_id IN
                   (SELECT version_group_id FROM notebook_versions WHERE notebook_id = ?)""",
                (notebook_id,),
            )
            conn.execute("DELETE FROM notebook_versions WHERE notebook_id = ?", (notebook_id,))
            conn.execute("DELETE FROM notebook_cells WHERE notebook_id = ?", (notebook_id,))
            conn.execute("DELETE FROM notebook_dag WHERE notebook_id = ?", (notebook_id,))
            conn.execute("DELETE FROM notebook_env WHERE notebook_id = ?", (notebook_id,))

            # Notebook-scoped orphans with NO FK cascade. Cleaned here so deleting a
            # notebook doesn't leak them. We deliberately PRESERVE cost/run history
            # (notebook_runs, vm_sessions, vm_state_log, vm_metrics) and never touch
            # the global data_source_entities catalog or pool_vms.
            #   - agent_sessions: keyed by notebook_id (agent_messages first, in case
            #     the ON DELETE CASCADE pragma isn't honoured).
            #   - per OWNED session id: workbook_intel, local_file_entities, its
            #     agent_sessions/agent_messages row (even one with notebook_id NULL)
            #     and session_registry (only with no live VM).
            conn.execute(
                """DELETE FROM agent_messages WHERE session_id IN
                   (SELECT session_id FROM agent_sessions WHERE notebook_id = ?)""",
                (notebook_id,),
            )
            conn.execute("DELETE FROM agent_sessions WHERE notebook_id = ?", (notebook_id,))
            for sid in owned:
                self._delete_session_intel(conn, sid)
                conn.execute("DELETE FROM agent_messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (sid,))
                # session_registry is the LIVE routing map. Only remove it if the
                # session has no still-alive VM — otherwise we'd cut routing to a VM
                # that's still running (deleting a notebook does not terminate its VM).
                alive = conn.execute(
                    """SELECT 1 FROM vm_sessions
                       WHERE session_id = ? AND state != 'TERMINATED' LIMIT 1""",
                    (sid,),
                ).fetchone()
                if not alive:
                    conn.execute("DELETE FROM session_registry WHERE session_id = ?", (sid,))

            cursor = conn.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        return cursor.rowcount > 0

    def session_intel_delete(self, session_id: str) -> int:
        if not session_id:
            return 0
        with self._db() as conn:
            return self._delete_session_intel(conn, session_id)
    def notebook_owned_session_ids(self, notebook_id: str) -> list[str]:
        # Same OWNED rule as notebook_delete (read-only; call it before the delete).
        with self._db() as conn:
            return [
                sid for sid in self._notebook_session_ids(conn, notebook_id)
                if not self._session_used_elsewhere(conn, sid, notebook_id)
            ]

    def notebook_latest_session_id(self, notebook_id: str) -> Optional[str]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT session_id FROM vm_sessions
                    WHERE notebook_id = ? AND session_id IS NOT NULL AND session_id <> ''
                    -- Latest = still alive, else most recently terminated. NOT
                    -- launched_at alone: a pool-granted VM's launched_at is its
                    -- MINT time, older than a cold launch it replaced.
                    ORDER BY (terminated_at IS NULL) DESC, terminated_at DESC, launched_at DESC""",
                (notebook_id,),
            ).fetchall()
            seen = set()
            for (sid,) in rows:
                if sid in seen:
                    continue
                seen.add(sid)
                # Only the LATEST session counts: an older one was replaced on
                # purpose (e.g. a fresh launch), so never fall back past it.
                return None if self._session_used_elsewhere(conn, sid, notebook_id) else sid
        return None

    def notebook_clear_session(self, session_id: str) -> bool:
        # Clear the live-session link when the notebook's VM is TERMINATED so
        # v_nb_notebook.session_id (the "is this open?" signal) stops reporting a
        # dead notebook as open. Keyed by session_id (what the terminate path has in
        # hand). Bumps updated_at so the SQLite→Aurora sync carries the clear.
        if not session_id:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE notebooks SET session_id = NULL, microvm_id = NULL, updated_at = ? "
                "WHERE session_id = ?",
                (now, session_id),
            )
        return cursor.rowcount > 0

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
        # unparseable (Postgres's v_nb_runs casts these columns to timestamptz).
        started_at = iso_utc(started_at)
        finished_at = iso_utc(finished_at)
        with self._db() as conn:
            # Re-using a run_id (same rule as postgres): keep the original
            # created_at, take every other column from this call. An UPSERT, not
            # INSERT OR REPLACE, so the row is updated in place and the sync
            # worker sees a newer updated_at watermark (no delete+insert).
            conn.execute(
                """INSERT INTO notebook_runs
                   (run_id, notebook_id, session_id, microvm_id, trigger, started_at, finished_at,
                    cells_executed, cells_errored, cells_interrupted, total_exec_ms, run_cells_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (run_id) DO UPDATE SET
                     notebook_id = excluded.notebook_id,
                     session_id = excluded.session_id,
                     microvm_id = excluded.microvm_id,
                     trigger = excluded.trigger,
                     started_at = excluded.started_at,
                     finished_at = excluded.finished_at,
                     cells_executed = excluded.cells_executed,
                     cells_errored = excluded.cells_errored,
                     cells_interrupted = excluded.cells_interrupted,
                     total_exec_ms = excluded.total_exec_ms,
                     run_cells_json = excluded.run_cells_json,
                     updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
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

    def notebook_run_list(self, notebook_id: str, limit: int = 20,
                          trigger: str = None) -> list[dict]:
        with self._db() as conn:
            if trigger:
                rows = conn.execute(
                    """SELECT * FROM notebook_runs WHERE notebook_id = ? AND trigger = ?
                       ORDER BY finished_at DESC NULLS LAST LIMIT ?""",
                    (notebook_id, trigger, max(0, int(limit)))
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM notebook_runs WHERE notebook_id = ?
                       ORDER BY finished_at DESC NULLS LAST LIMIT ?""",
                    (notebook_id, max(0, int(limit)))
                ).fetchall()
        return [self._run_row_to_dict(r) for r in rows]

    @staticmethod
    def _run_row_to_dict(row) -> dict:
        """Row → run dict, parsing the run_cells_json blob into a `run_cells` list.
        created_at/updated_at come back as ISO-8601 UTC (created_at's table default
        is datetime('now')'s naive 'YYYY-MM-DD HH:MM:SS')."""
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

    def notebook_run_list_for_vms(self, microvm_ids: list[str],
                                  limit: int = 200) -> list[dict]:
        ids = [m for m in (microvm_ids or []) if m]
        if not ids:
            return []
        marks = ", ".join(["?"] * len(ids))
        with self._db() as conn:
            rows = conn.execute(
                f"""SELECT {_mf.RUN_LIGHT_COLUMNS} FROM notebook_runs
                    WHERE microvm_id IN ({marks})
                    ORDER BY COALESCE(started_at, finished_at) DESC NULLS LAST LIMIT ?""",
                (*ids, max(0, int(limit))),
            ).fetchall()
        return [_mf.normalize_run(dict(r)) for r in rows]

    # ---- Notebook versions (document history) ----

    # Column list for notebook_cell_versions (document-only subset of notebook_cells).
    _CELL_VERSION_COLUMNS = (
        "version_group_id, cell_id, position, type, code, "
        "output_variable, ai_explanation, ai_explanation_source, generated"
    )

    @staticmethod
    def _cell_version_row(version_group_id: str, position: int, c: dict) -> tuple:
        """Flatten a client cell dict into a notebook_cell_versions row tuple —
        the DOCUMENT subset only (no runtime output/exec/dataflow fields)."""
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
        """Reassemble a client cell dict from a notebook_cell_versions row. Shaped so
        it can be handed straight to notebook_update(cells=...) on restore."""
        return {
            "id": r["cell_id"],
            "type": r["type"] or "code",
            "code": r["code"] or "",
            "outputVariable": r["output_variable"],
            "aiExplanation": r["ai_explanation"],
            "aiExplanationSource": r["ai_explanation_source"],
            "generated": r["generated"],
        }

    @staticmethod
    def _version_header_to_dict(row) -> dict:
        """Header row -> dict with created_at normalized to ISO-8601 UTC, matching
        the Postgres backend so the frontend parses timestamps identically."""
        d = dict(row)
        d["created_at"] = _iso_utc(d.get("created_at"))
        if "updated_at" in d:
            d["updated_at"] = _iso_utc(d["updated_at"])
        return d

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
        with self._db() as conn:
            conn.execute(
                """INSERT INTO notebook_versions
                   (version_group_id, notebook_id, label, trigger, cell_count, fingerprint_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (vgid, notebook_id, label, trigger, cell_count, fp_hash, created_at),
            )
            rows = [self._cell_version_row(vgid, i, c) for i, c in enumerate(snap)]
            if rows:
                placeholders = "(" + ", ".join(["?"] * 9) + ")"
                conn.executemany(
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
        """Return the most-recent version header for this notebook whose stored
        fingerprint_hash matches, or None. Powers O(1) dedup of non-manual snapshots."""
        with self._db() as conn:
            row = conn.execute(
                """SELECT * FROM notebook_versions
                   WHERE notebook_id = ? AND fingerprint_hash = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (notebook_id, fp_hash),
            ).fetchone()
        return self._version_header_to_dict(row) if row else None

    def notebook_version_list(self, notebook_id: str, limit: int = 50) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT * FROM notebook_versions WHERE notebook_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (notebook_id, max(0, int(limit))),
            ).fetchall()
        return [self._version_header_to_dict(r) for r in rows]

    def notebook_version_get(self, version_group_id: str) -> Optional[dict]:
        with self._db() as conn:
            header = conn.execute(
                "SELECT * FROM notebook_versions WHERE version_group_id = ?",
                (version_group_id,),
            ).fetchone()
            if not header:
                return None
            cell_rows = conn.execute(
                """SELECT * FROM notebook_cell_versions WHERE version_group_id = ?
                   ORDER BY position ASC""",
                (version_group_id,),
            ).fetchall()
        d = self._version_header_to_dict(header)
        d["cells"] = [self._row_to_version_cell(r) for r in cell_rows]
        return d

    def notebook_cell_versions(self, notebook_id: str, cell_id: str,
                               limit: int = 20) -> list[dict]:
        # Per-cell history — join cell rows to their header for time/label/trigger.
        with self._db() as conn:
            rows = conn.execute(
                """SELECT cv.*, v.created_at AS created_at, v.label AS label,
                          v.trigger AS trigger
                   FROM notebook_cell_versions cv
                   JOIN notebook_versions v ON v.version_group_id = cv.version_group_id
                   WHERE v.notebook_id = ? AND cv.cell_id = ?
                   ORDER BY v.created_at DESC LIMIT ?""",
                (notebook_id, cell_id, max(0, int(limit))),
            ).fetchall()
        out = []
        for r in rows:
            cell = self._row_to_version_cell(r)
            cell.update({
                "version_group_id": r["version_group_id"],
                "created_at": iso_utc(r["created_at"]),
                "label": r["label"],
                "trigger": r["trigger"],
            })
            out.append(cell)
        return out

    def notebook_version_prune(self, notebook_id: str, keep: int = 50) -> int:
        # Prune only 'auto' versions beyond the newest `keep`; keep manual/pre_ai/
        # pre_restore forever. Cell rows cascade with the header.
        with self._db() as conn:
            stale = conn.execute(
                """SELECT version_group_id FROM notebook_versions
                   WHERE notebook_id = ? AND trigger = 'auto'
                   ORDER BY created_at DESC LIMIT -1 OFFSET ?""",
                (notebook_id, keep),
            ).fetchall()
            ids = [r["version_group_id"] for r in stale]
            for vgid in ids:
                conn.execute("DELETE FROM notebook_cell_versions WHERE version_group_id = ?", (vgid,))
                conn.execute("DELETE FROM notebook_versions WHERE version_group_id = ?", (vgid,))
        return len(ids)

    def notebook_version_delete(self, notebook_id: str, version_group_id: str) -> bool:
        # Scope the delete by notebook_id so a wrong id can't remove another
        # notebook's version. Delete cell rows explicitly first (PRAGMA
        # foreign_keys may not be honored on every connection), then the header.
        with self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM notebook_versions WHERE version_group_id = ? AND notebook_id = ?",
                (version_group_id, notebook_id),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "DELETE FROM notebook_cell_versions WHERE version_group_id = ?",
                (version_group_id,),
            )
            cur = conn.execute(
                "DELETE FROM notebook_versions WHERE version_group_id = ? AND notebook_id = ?",
                (version_group_id, notebook_id),
            )
        return cur.rowcount > 0

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
            "created_at": iso_utc(row["created_at"]),
            "updated_at": iso_utc(row["updated_at"]),
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
        now = datetime.now(timezone.utc).isoformat()
        # launched_at is the VM's true MINT time. For a cold launch the caller
        # omits it and it defaults to now (minted now). For a pooled spare the
        # caller passes the pool's mint ISO so the AWS 8h-lifetime countdown is
        # honest (the spare may have sat warm in the pool before adoption).
        effective_launched_at = launched_at or now
        # prior_suspended_since: a pooled spare sat SUSPENDED (snapshot storage)
        # from its mint time until this grant. When it's a valid ISO strictly
        # older than now, seed the state-log with that pool-residency SUSPENDED
        # interval so v_vm_sessions reconstructs it (two rows). Guard against a
        # missing/unparseable/not-older value → fall back to the single
        # NULL->RUNNING row (cold-launch behavior, no spurious interval).
        seed_suspended = self._valid_prior_suspended(prior_suspended_since, now)
        with self._db() as conn:
            # Re-registering an existing microvm_id (same rule as postgres): keep
            # burst_mb_seconds and image_arn, clear terminated_at (the row is
            # RUNNING again), take every other column from this launch. An
            # UPSERT, not INSERT OR REPLACE, so the row is updated in place and
            # the sync worker sees a newer updated_at watermark.
            conn.execute(
                """INSERT INTO vm_sessions
                   (microvm_id, notebook_id, notebook_name, session_id, memory_mib, architecture, state, endpoint, launched_at, idle_timeout_sec, max_duration_sec, checkpoint_enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (microvm_id) DO UPDATE SET
                     notebook_id = excluded.notebook_id,
                     notebook_name = excluded.notebook_name,
                     session_id = excluded.session_id,
                     memory_mib = excluded.memory_mib,
                     architecture = excluded.architecture,
                     state = 'RUNNING',
                     endpoint = excluded.endpoint,
                     launched_at = excluded.launched_at,
                     terminated_at = NULL,
                     idle_timeout_sec = excluded.idle_timeout_sec,
                     max_duration_sec = excluded.max_duration_sec,
                     checkpoint_enabled = excluded.checkpoint_enabled,
                     updated_at = excluded.updated_at""",
                (microvm_id, notebook_id, notebook_name, session_id, memory_mib, architecture or "arm64", endpoint, effective_launched_at, idle_timeout_sec, max_duration_sec, 1 if checkpoint_enabled else 0, now)
            )
            if seed_suspended is not None:
                # Two rows, SUSPENDED first (smaller id) so the view's LEAD()
                # ordered by id reconstructs: [mint..now] SUSPENDED, [now..] RUNNING.
                conn.execute(
                    "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?, ?, NULL, 'SUSPENDED')",
                    (microvm_id, seed_suspended)
                )
                conn.execute(
                    "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?, ?, 'SUSPENDED', 'RUNNING')",
                    (microvm_id, now)
                )
            else:
                conn.execute(
                    "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?, ?, NULL, 'RUNNING')",
                    (microvm_id, now)
                )
        return {"microvm_id": microvm_id, "state": "RUNNING", "launched_at": effective_launched_at}

    @staticmethod
    def _valid_prior_suspended(prior_suspended_since, now_iso):
        """Return the ISO mint time to seed a SUSPENDED interval with, or None.

        None/empty, unparseable, or NOT strictly older than now → None (seed
        nothing; the caller falls back to the single NULL->RUNNING row). Shared
        guard so both the sqlite and (parity) postgres seeding use identical
        acceptance rules — a bad value can never produce a zero/negative or
        inverted interval."""
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
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            # Take the write lock BEFORE reading the old state so concurrent
            # transitions serialize (busy_timeout makes the others wait): only the
            # writer that still sees old_state != new_state logs the transition,
            # so vm_state_log never gets a duplicate row for one change.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM vm_sessions WHERE microvm_id = ?", (microvm_id,)).fetchone()
            # No session row → nothing to transition. Without this guard, every poll
            # for a VM with no vm_sessions row (e.g. an orphaned in-memory VM after a
            # DB reset) writes a spurious old_state=NULL -> new_state row to
            # vm_state_log forever (old_state=None never equals new_state, so the
            # dedup below can't fire and the UPDATE touches 0 rows). Skip it.
            if row is None:
                return
            old_state = row["state"]
            if old_state == new_state:
                return
            conn.execute("UPDATE vm_sessions SET state = ?, updated_at = ? WHERE microvm_id = ?", (new_state, now, microvm_id))
            conn.execute(
                "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?, ?, ?, ?)",
                (microvm_id, now, old_state, new_state)
            )
            if new_state == "TERMINATED":
                conn.execute("UPDATE vm_sessions SET terminated_at = ?, updated_at = ? WHERE microvm_id = ?", (now, now, microvm_id))

    def vm_session_mark_terminated(self, microvm_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            # Write lock before the read, same as vm_session_update_state.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM vm_sessions WHERE microvm_id = ?", (microvm_id,)).fetchone()
            if not row or row["state"] == "TERMINATED":
                return False  # unknown session or already terminated — no-op
            old_state = row["state"]
            conn.execute("UPDATE vm_sessions SET state = 'TERMINATED', terminated_at = ?, updated_at = ? WHERE microvm_id = ?", (now, now, microvm_id))
            conn.execute(
                "INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?, ?, ?, 'TERMINATED')",
                (microvm_id, now, old_state)
            )
        return True

    def vm_session_update_burst(self, microvm_id: str, burst_mb_seconds: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            # Only write (and bump the sync watermark) when the value ACTUALLY
            # changes. The cost poller calls this every /instances cycle for each
            # tracked VM, usually with an unchanged burst_mb_seconds; without this
            # guard every poll would bump updated_at and the sync worker would
            # re-upsert the row forever (watermark churn on an idle VM). `IS NOT`
            # is SQLite's null-safe inequality, so NULL->value still writes.
            conn.execute(
                "UPDATE vm_sessions SET burst_mb_seconds = ?, updated_at = ? "
                "WHERE microvm_id = ? AND burst_mb_seconds IS NOT ?",
                (burst_mb_seconds, now, microvm_id, burst_mb_seconds)
            )

    def vm_session_get(self, microvm_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM vm_sessions WHERE microvm_id = ?", (microvm_id,)).fetchone()
        return iso_utc_cols(dict(row), VM_SESSION_TS) if row else None

    def vm_session_list_active(self) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM vm_sessions WHERE state != 'TERMINATED' ORDER BY launched_at DESC").fetchall()
        return [iso_utc_cols(dict(r), VM_SESSION_TS) for r in rows]

    def vm_session_list_for_session(self, session_id: str) -> list[dict]:
        # All VMs (any state) that served this session — original + rotation/recovery
        # replacements — oldest first, so log merging reads them chronologically.
        # Uses idx_vm_sessions_session.
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM vm_sessions WHERE session_id = ? ORDER BY launched_at ASC",
                (session_id,),
            ).fetchall()
        return [iso_utc_cols(dict(r), VM_SESSION_TS) for r in rows]

    def vm_state_log_get(self, microvm_id: str) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT new_state, timestamp FROM vm_state_log WHERE microvm_id = ? ORDER BY id ASC",
                (microvm_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # Filter-first per-VM cost query. Same CTE chain and column expressions as
    # v_vm_sessions (which stays unchanged for analytics/tests), but vm_state_log
    # is filtered BEFORE the LEAD window / GROUP BY, so SQLite searches
    # idx_vm_state_log_vm for just the requested VMs instead of materializing
    # `agg` for every VM ever logged (a WHERE on the view binds to the outer side
    # of the LEFT JOIN and is not pushed into the aggregate). The window is
    # PARTITION BY microvm_id, so filtering by microvm_id first cannot change any
    # per-VM result. {log_filter}/{where} are fixed SQL fragments with `?`
    # placeholders only — never data; every id/session is a bound parameter.
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
        (julianday(
            COALESCE(next_ts, CASE WHEN state = 'TERMINATED' THEN start_ts ELSE 'now' END)
         ) - julianday(start_ts)) * 86400.0 AS secs
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
    ROUND(COALESCE(a.running_secs, 0)) AS running_secs,
    ROUND(COALESCE(a.suspended_secs, 0)) AS suspended_secs,
    s.burst_mb_seconds,
    COALESCE(a.resume_count, 0) AS resume_count,
    COALESCE(a.suspend_count, 0) AS suspend_count{extra_cols}
FROM vm_sessions s
LEFT JOIN agg a ON a.microvm_id = s.microvm_id
WHERE {where}
"""
    _COST_QTY_CHUNK = 500  # stay well under SQLite's bound-variable limit

    def vm_cost_quantities_many(self, microvm_ids: list[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(i for i in (microvm_ids or []) if i))
        if not ids:
            return {}
        out: dict[str, dict] = {}
        with self._db() as conn:
            for start in range(0, len(ids), self._COST_QTY_CHUNK):
                chunk = ids[start:start + self._COST_QTY_CHUNK]
                ph = ",".join("?" * len(chunk))
                sql = self._COST_QTY_FILTERED_SQL.format(
                    log_filter=f"l.microvm_id IN ({ph})",
                    extra_cols="",
                    where=f"s.microvm_id IN ({ph})",
                )
                for row in conn.execute(sql, (*chunk, *chunk)).fetchall():
                    out[row["microvm_id"]] = dict(row)
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
            log_filter="l.microvm_id IN (SELECT microvm_id FROM vm_sessions WHERE session_id = ?)",
            extra_cols=",\n    s.launched_at,\n    s.terminated_at",
            where="s.session_id = ?",
        ) + "ORDER BY s.launched_at ASC"
        with self._db() as conn:
            rows = conn.execute(sql, (session_id, session_id)).fetchall()
        return [dict(row) for row in rows]

    # ============================================================
    # SESSION REGISTRY
    # ============================================================

    def session_registry_upsert(self, session_id: str, vm_id: str, endpoint: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                """INSERT INTO session_registry (session_id, vm_id, endpoint, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET vm_id = ?, endpoint = ?, updated_at = ?""",
                (session_id, vm_id, endpoint, now, vm_id, endpoint, now)
            )

    def session_registry_get(self, session_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT vm_id, endpoint FROM session_registry WHERE session_id = ?",
                (session_id,)
            ).fetchone()
        return {"vm_id": row["vm_id"], "endpoint": row["endpoint"]} if row else None

    def session_registry_delete(self, session_id: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM session_registry WHERE session_id = ?", (session_id,))

    def session_registry_all(self) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT session_id, vm_id, endpoint FROM session_registry"
            ).fetchall()
        return [
            {"session_id": r["session_id"], "vm_id": r["vm_id"], "endpoint": r["endpoint"]}
            for r in rows
        ]

    # ============================================================
    # DISTRIBUTED SESSION LOCK (in-process for SQLite)
    # ============================================================

    def try_session_lock(self, session_id: str) -> bool:
        """Non-blocking acquire. Correct single-process mutual exclusion: the
        first caller for a session_id wins; subsequent callers get False until
        release_session_lock() is called."""
        with _LOCK_GUARD:
            if session_id in _HELD_SESSION_LOCKS:
                return False
            _HELD_SESSION_LOCKS.add(session_id)
            return True

    def release_session_lock(self, session_id: str) -> None:
        """Release the lock. No-op if not held (safe to call in a finally block)."""
        with _LOCK_GUARD:
            _HELD_SESSION_LOCKS.discard(session_id)

    # ============================================================
    # POOL VMS
    # ============================================================

    def pool_vm_create(self, vm_id: str, architecture: str, size_mib: int,
                       endpoint: str, fake_session_id: str,
                       launched_at: str = None, state: str = "launching") -> dict:
        launched_at = launched_at or datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                """INSERT INTO pool_vms
                   (vm_id, architecture, size_mib, endpoint, fake_session_id,
                    state, real_session_id, launched_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vm_id, architecture, size_mib, endpoint, fake_session_id,
                 state, None, launched_at, now)
            )
        return {"vm_id": vm_id, "architecture": architecture, "size_mib": size_mib,
                "endpoint": endpoint, "fake_session_id": fake_session_id,
                "state": state, "real_session_id": None,
                "launched_at": launched_at, "updated_at": now}

    def pool_vm_set_state(self, vm_id: str, state: str,
                          real_session_id=_UNSET) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        set_clause = "state = ?, updated_at = ?"
        params = [state, now]
        if real_session_id is not _UNSET:
            set_clause += ", real_session_id = ?"
            params.append(real_session_id)
        params.append(vm_id)
        with self._db() as conn:
            cursor = conn.execute(
                f"UPDATE pool_vms SET {set_clause} WHERE vm_id = ?", params
            )
        return cursor.rowcount > 0

    def pool_vm_claim_youngest(self, architecture: str, size_mib: int,
                               real_session_id: str) -> Optional[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            # One atomic UPDATE ... RETURNING: returns exactly the row this
            # statement claimed (no re-SELECT that could pick another 'granting' row).
            row = conn.execute(
                """UPDATE pool_vms
                      SET state = 'granting', real_session_id = ?, updated_at = ?
                    WHERE vm_id = (
                        SELECT vm_id FROM pool_vms
                         WHERE architecture = ? AND size_mib = ? AND state = 'available'
                         ORDER BY launched_at DESC
                         LIMIT 1
                    )
                    AND state = 'available'
                    RETURNING *""",
                (real_session_id, now, architecture, size_mib)
            ).fetchone()
        return iso_utc_cols(dict(row), POOL_VM_TS) if row else None

    def pool_vm_list(self, state: str = None, architecture: str = None,
                     size_mib: int = None) -> list[dict]:
        clauses = []
        params = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if architecture is not None:
            clauses.append("architecture = ?")
            params.append(architecture)
        if size_mib is not None:
            clauses.append("size_mib = ?")
            params.append(size_mib)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db() as conn:
            rows = conn.execute(f"SELECT * FROM pool_vms{where}", params).fetchall()
        return [iso_utc_cols(dict(r), POOL_VM_TS) for r in rows]

    def pool_vm_count_by_size(self, architecture: str, size_mib: int,
                              states: tuple = ("available", "launching")) -> int:
        placeholders = ", ".join(["?"] * len(states))
        with self._db() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS c FROM pool_vms
                     WHERE architecture = ? AND size_mib = ? AND state IN ({placeholders})""",
                [architecture, size_mib, *states]
            ).fetchone()
        return row["c"]

    def pool_vm_delete(self, vm_id: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM pool_vms WHERE vm_id = ?", (vm_id,))

    def pool_vm_backdate(self, vm_id: str, launched_at: str = None,
                         updated_at: str = None) -> bool:
        """Test seam (see interface): not used by production code."""
        sets, params = [], []
        if launched_at is not None:
            sets.append("launched_at = ?")
            params.append(launched_at)
        if updated_at is not None:
            sets.append("updated_at = ?")
            params.append(updated_at)
        if not sets:
            return False
        params.append(vm_id)
        with self._db() as conn:
            cur = conn.execute(
                f"UPDATE pool_vms SET {', '.join(sets)} WHERE vm_id = ?", params)
            return cur.rowcount > 0

    def pool_vm_list_older_than(self, max_age_seconds: float,
                                state: str = "available",
                                since_column: str = "launched_at") -> list[dict]:
        """Rows in `state` whose age exceeds max_age_seconds, where age is
        measured from `since_column`.

        since_column choice is CRITICAL and differs by caller:
          - "launched_at" (default): the VM's immutable mint time. Correct for
            the age-recycle loop (a spare older than POOL_MAX_AGE_HOURS must be
            recycled regardless of state transitions).
          - "updated_at": the time the row LAST changed state. Correct for the
            stale-'granting' check: a granting row is stale only if it has been
            STUCK in 'granting' longer than the grant timeout — NOT if the
            underlying VM happens to be old. Using launched_at here would flag
            every spare that sat warm in the pool longer than the grant timeout
            the instant it is claimed, and reconcile would tear it down
            mid-grant (observed: a 6s grant on a 2-min-old spare got killed)."""
        col = since_column if since_column in ("launched_at", "updated_at") else "launched_at"
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM pool_vms WHERE state = ?", (state,)
            ).fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for r in rows:
            d = dict(r)
            since_dt = datetime.fromisoformat(d[col])
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            age = (now - since_dt).total_seconds()
            if age > max_age_seconds:
                result.append(iso_utc_cols(d, POOL_VM_TS))
        return result

    # ============================================================
    # METRICS
    # ============================================================

    def metrics_record(self, microvm_id: str, cpu_pct: float, mem_pct: float,
                       mem_used_mb: float, disk_pct: float, disk_used_mb: float,
                       net_bytes_sent: int, net_bytes_recv: int,
                       processes: int, uptime_sec: float,
                       recorded_at: float = None) -> None:
        if recorded_at is not None:
            now = datetime.fromtimestamp(float(recorded_at), timezone.utc).isoformat()
        else:
            now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                """INSERT INTO vm_metrics (microvm_id, timestamp, cpu_pct, mem_pct, mem_used_mb,
                   disk_pct, disk_used_mb, net_bytes_sent, net_bytes_recv, processes, uptime_sec)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (microvm_id, now, cpu_pct, mem_pct, mem_used_mb, disk_pct, disk_used_mb,
                 net_bytes_sent, net_bytes_recv, processes, uptime_sec)
            )

    def metrics_get_latest(self, microvm_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM vm_metrics WHERE microvm_id = ? ORDER BY timestamp DESC LIMIT 1",
                (microvm_id,)
            ).fetchone()
        return iso_utc_cols(dict(row), METRICS_TS) if row else None

    def metrics_get_history(self, microvm_id: str, minutes: int = 5) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT timestamp, cpu_pct, mem_pct, disk_pct, net_bytes_sent, net_bytes_recv
                   FROM vm_metrics
                   WHERE microvm_id = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
                   ORDER BY timestamp ASC""",
                (microvm_id, f"-{minutes} minutes")
            ).fetchall()
        return [dict(r) for r in rows]

    # Sample time as epoch seconds rounded to ms. julianday() carries ~1e-5 s
    # float error, so round BEFORE bucketing (Postgres uses the same rounding).
    _METRICS_EPOCH_SQL = "ROUND((julianday(timestamp) - 2440587.5) * 86400.0, 3)"

    @staticmethod
    def _metrics_window(start_epoch: float, end_epoch: float) -> tuple:
        """[floor(start), floor(end) + 1) as int epochs plus the matching UTC
        'YYYY-MM-DDTHH:MM:SS' prefixes. Stored timestamps are ISO-8601 UTC
        ('...T..:..:..[.ffffff]+00:00'), so the prefix compare is exact and
        index-friendly (same trick as metrics_cleanup)."""
        lo, hi = _mf.window_bounds(start_epoch, end_epoch)
        fmt = "%Y-%m-%dT%H:%M:%S"
        return (lo, hi,
                datetime.fromtimestamp(lo, timezone.utc).strftime(fmt),
                datetime.fromtimestamp(hi, timezone.utc).strftime(fmt))

    def metrics_get_summary(self, microvm_id: str, start_epoch: float,
                            end_epoch: float) -> Optional[dict]:
        _lo, _hi, lo_s, hi_s = self._metrics_window(start_epoch, end_epoch)
        ep = self._METRICS_EPOCH_SQL
        where = "microvm_id = ? AND timestamp >= ? AND timestamp < ?"
        params = (microvm_id, lo_s, hi_s)
        with self._db() as conn:
            agg = conn.execute(
                f"""SELECT COUNT(*) AS samples, MIN({ep}) AS first_at, MAX({ep}) AS last_at,
                       AVG(cpu_pct) AS cpu_avg, MAX(cpu_pct) AS cpu_max,
                       AVG(mem_used_mb) AS mem_used_avg, MAX(mem_used_mb) AS mem_used_max,
                       MAX(mem_pct) AS mem_pct_max, MAX(disk_pct) AS disk_pct_max,
                       MAX(disk_used_mb) AS disk_used_max
                    FROM vm_metrics WHERE {where}""",
                params,
            ).fetchone()
            if not agg or not agg["samples"]:
                return None
            out = dict(agg)
            for col, key in (("cpu_pct", "cpu_max_at"), ("mem_used_mb", "mem_used_max_at"),
                             ("disk_pct", "disk_pct_max_at")):
                row = conn.execute(
                    f"""SELECT {ep} AS at FROM vm_metrics
                        WHERE {where} AND {col} IS NOT NULL
                        ORDER BY {col} DESC, timestamp ASC LIMIT 1""",
                    params,
                ).fetchone()
                out[key] = row["at"] if row else None
        return _mf.normalize_summary(out)

    def metrics_get_bucketed(self, microvm_id: str, start_epoch: float,
                             end_epoch: float, bucket_sec: int) -> list[dict]:
        lo, _hi, lo_s, hi_s = self._metrics_window(start_epoch, end_epoch)
        bucket_sec = max(1, int(bucket_sec))
        ep = self._METRICS_EPOCH_SQL
        with self._db() as conn:
            rows = conn.execute(
                f"""SELECT CAST(({ep} - ?) / ? AS INTEGER) AS bucket,
                       MIN({ep}) AS t_first, MAX({ep}) AS t_last, COUNT(*) AS samples,
                       AVG(cpu_pct) AS cpu_avg, MAX(cpu_pct) AS cpu_max,
                       AVG(mem_used_mb) AS mem_used_avg, MAX(mem_used_mb) AS mem_used_max,
                       MAX(mem_pct) AS mem_pct_max, MAX(disk_pct) AS disk_pct_max,
                       MAX(disk_used_mb) AS disk_used_max
                    FROM vm_metrics
                    WHERE microvm_id = ? AND timestamp >= ? AND timestamp < ?
                    GROUP BY bucket ORDER BY bucket ASC""",
                (lo, float(bucket_sec), microvm_id, lo_s, hi_s),
            ).fetchall()
        return [_mf.normalize_bucket(dict(r)) for r in rows]

    def metrics_get_burst(self, microvm_id: str, baseline_mb: float) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                """WITH s AS (
                       SELECT {ep} AS t, mem_used_mb AS u, id FROM vm_metrics
                       WHERE microvm_id = :mid AND mem_used_mb > 0),
                    w AS (
                       SELECT t, u, LAG(t) OVER (ORDER BY t, id) AS pt,
                              LAG(u) OVER (ORDER BY t, id) AS pu FROM s)
                    SELECT COUNT(*) AS samples,
                       SUM(CASE WHEN u > :base THEN 1 ELSE 0 END) AS burst_samples,
                       SUM(CASE WHEN u > :base AND pt IS NOT NULL THEN t - pt ELSE 0 END) AS burst_seconds,
                       SUM(CASE WHEN u > :base AND pt IS NOT NULL
                                THEN (u - :base) * (t - pt) ELSE 0 END) AS burst_mb_seconds,
                       SUM(CASE WHEN u > :base AND (pu IS NULL OR pu <= :base)
                                THEN 1 ELSE 0 END) AS burst_episodes,
                       MAX(u) AS peak_mem_mb
                    FROM w""".replace("{ep}", self._METRICS_EPOCH_SQL),
                {"mid": microvm_id, "base": float(baseline_mb)},
            ).fetchone()
        return _mf.normalize_burst(dict(row)) if row else None
    def metrics_cleanup(self, hours: int = 24) -> None:
        # metrics_record stores ISO-8601 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00' (UTC),
        # so compare against the same 'T'-separated UTC prefix — datetime('now')
        # yields 'YYYY-MM-DD HH:MM:SS' and ' ' < 'T' made every same-day row
        # compare as newer than the cutoff.
        with self._db() as conn:
            conn.execute(
                "DELETE FROM vm_metrics WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                (f"-{hours} hours",)
            )

    # ============================================================
    # AGENT SESSIONS (Strands SessionManager backing store)
    # ============================================================

    def _ts(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def agent_session_create(self, session_id: str, session: dict, notebook_id: str = None) -> None:
        now = self._ts()
        with self._db() as conn:
            # Idempotent create: keep the existing row (and its agent_json) if present.
            conn.execute(
                """INSERT INTO agent_sessions (session_id, notebook_id, session_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       session_json = excluded.session_json,
                       notebook_id  = COALESCE(excluded.notebook_id, agent_sessions.notebook_id),
                       updated_at   = excluded.updated_at""",
                (session_id, notebook_id, json.dumps(session), now, now),
            )

    def agent_session_read(self, session_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT session_json FROM agent_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row["session_json"] or "{}")

    def agent_create(self, session_id: str, agent_id: str, agent: dict) -> None:
        now = self._ts()
        with self._db() as conn:
            conn.execute(
                """UPDATE agent_sessions SET agent_id = ?, agent_json = ?, updated_at = ?
                   WHERE session_id = ?""",
                (agent_id, json.dumps(agent), now, session_id),
            )

    def agent_read(self, session_id: str, agent_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT agent_json FROM agent_sessions WHERE session_id = ? AND agent_id = ?",
                (session_id, agent_id),
            ).fetchone()
        if not row or row["agent_json"] in (None, "", "{}"):
            return None
        return json.loads(row["agent_json"])

    def agent_update(self, session_id: str, agent_id: str, agent: dict) -> None:
        now = self._ts()
        with self._db() as conn:
            conn.execute(
                """UPDATE agent_sessions SET agent_id = ?, agent_json = ?, updated_at = ?
                   WHERE session_id = ?""",
                (agent_id, json.dumps(agent), now, session_id),
            )

    def agent_message_create(self, session_id: str, agent_id: str, message_id: int,
                             message: dict, notebook_id: str = None) -> None:
        now = self._ts()
        with self._db() as conn:
            conn.execute(
                """INSERT INTO agent_messages
                       (session_id, agent_id, message_id, notebook_id, message_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, agent_id, message_id) DO UPDATE SET
                       message_json = excluded.message_json,
                       updated_at   = excluded.updated_at""",
                (session_id, agent_id, message_id, notebook_id, json.dumps(message), now, now),
            )

    def agent_message_read(self, session_id: str, agent_id: str, message_id: int) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                """SELECT message_json FROM agent_messages
                   WHERE session_id = ? AND agent_id = ? AND message_id = ?""",
                (session_id, agent_id, message_id),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["message_json"])

    def agent_message_update(self, session_id: str, agent_id: str, message_id: int, message: dict) -> None:
        now = self._ts()
        with self._db() as conn:
            conn.execute(
                """UPDATE agent_messages SET message_json = ?, updated_at = ?
                   WHERE session_id = ? AND agent_id = ? AND message_id = ?""",
                (json.dumps(message), now, session_id, agent_id, message_id),
            )

    def agent_message_list(self, session_id: str, agent_id: str,
                           limit: Optional[int] = None, offset: int = 0) -> list[dict]:
        sql = ("""SELECT message_json FROM agent_messages
                  WHERE session_id = ? AND agent_id = ?
                  ORDER BY message_id ASC LIMIT ? OFFSET ?""")
        # SQLite uses -1 for "no limit". Negative limit/offset clamp to 0 (same
        # result as Postgres).
        lim = -1 if limit is None else max(0, int(limit))
        with self._db() as conn:
            rows = conn.execute(sql, (session_id, agent_id, lim, max(0, int(offset)))).fetchall()
        return [json.loads(r["message_json"]) for r in rows]

    def agent_session_delete(self, session_id: str) -> None:
        # agent_messages rows are removed by ON DELETE CASCADE.
        with self._db() as conn:
            conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_id,))

    def agent_transcript_get(self, session_id: str) -> list:
        from ._transcript import messages_to_bubbles
        with self._db() as conn:
            rows = conn.execute(
                """SELECT message_json FROM agent_messages
                   WHERE session_id = ? ORDER BY message_id ASC""",
                (session_id,),
            ).fetchall()
        return messages_to_bubbles([json.loads(r["message_json"]) for r in rows])

    # ============================================================
    # WORKBOOK INTEL
    # ============================================================

    def workbook_intel_save(self, session_id: str, s3_key: str) -> None:
        """Save or update workbook intel metadata."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                """INSERT INTO workbook_intel (session_id, s3_key, generated_at, version)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(session_id) DO UPDATE SET s3_key = ?, generated_at = ?, version = version + 1""",
                (session_id, s3_key, now, s3_key, now)
            )

    def workbook_intel_get(self, session_id: str) -> dict | None:
        """Get workbook intel metadata for a session."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT session_id, s3_key, generated_at, version FROM workbook_intel WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if row:
                return {"session_id": row[0], "s3_key": row[1], "generated_at": row[2], "version": row[3]}
            return None

    # ============================================================
    # NOTEBOOK ENV (persisted env vars + secret references)
    # ============================================================

    def notebook_env_get(self, notebook_id) -> dict:
        """Return the persisted env for a notebook: {"env_vars": {...}, "secrets": [...]}.
        Always returns a well-formed dict (empty defaults) so callers never branch on None.

        Env var VALUES are stored SEALED (AES-GCM, per-notebook subkey); we decrypt
        them here so callers get plaintext. Secret REFERENCES are stored/returned
        as-is (they never held a value). See proxy.platform.env_crypto."""
        from proxy.platform.env_crypto import unseal_env_vars
        with self._db() as conn:
            row = conn.execute(
                "SELECT env_json FROM notebook_env WHERE notebook_id = ?",
                (notebook_id,)
            ).fetchone()
        data = {}
        if row and row[0]:
            try:
                data = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                data = {}
        return {
            "env_vars": unseal_env_vars(notebook_id, data.get("env_vars", {}) or {}),
            "secrets": data.get("secrets", []) or [],
        }

    def notebook_env_set(self, notebook_id, env_vars: dict, secrets: list,
                         merge_env: bool = False,
                         remove_env_keys: list | None = None) -> None:
        """Replace (or MERGE) the persisted env for a notebook. Env var VALUES are
        SEALED (AES-GCM, per-notebook subkey) before write so a DB reader sees
        ciphertext, never the plaintext value. Secret REFERENCES (arn/envVar/secretKey)
        are stored as-is — they never carry a value (the VM resolves it at launch).
        Fails closed: if the master key is unavailable, seal_env_vars raises and
        nothing is written.

        merge_env=False (default): env_vars is the whole new set (historic replace).
        merge_env=True: start from the existing DECRYPTED env, drop any key in
        remove_env_keys, overlay env_vars, then seal+write — so re-entering one key
        updates just that key and leaves the others intact. Secrets are ALWAYS a
        whole-object replace."""
        from proxy.platform.env_crypto import seal_env_vars
        now = datetime.now(timezone.utc).isoformat()
        incoming = dict(env_vars or {})
        if merge_env:
            # Reuse the unseal path in notebook_env_get so we merge against plaintext.
            existing = self.notebook_env_get(notebook_id).get("env_vars", {}) or {}
            merged = dict(existing)
            for k in (remove_env_keys or []):
                merged.pop(k, None)
            merged.update(incoming)
            incoming = merged
        sealed_env = seal_env_vars(notebook_id, incoming)
        blob = json.dumps({"env_vars": sealed_env, "secrets": secrets or []})
        with self._db() as conn:
            conn.execute(
                """INSERT INTO notebook_env (notebook_id, env_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(notebook_id) DO UPDATE SET env_json = ?, updated_at = ?""",
                (notebook_id, blob, now, blob, now)
            )

    def notebook_env_list(self) -> list:
        """All notebook envs, decrypted. See interface docstring."""
        from proxy.platform.env_crypto import unseal_env_vars
        with self._db() as conn:
            rows = conn.execute(
                "SELECT notebook_id, env_json FROM notebook_env ORDER BY notebook_id"
            ).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["env_json"]) if r["env_json"] else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            out.append({
                "notebook_id": r["notebook_id"],
                "env_vars": unseal_env_vars(r["notebook_id"], data.get("env_vars", {}) or {}),
                "secrets": data.get("secrets", []) or [],
            })
        return out

    # ============================================================
    # GLOBAL DATA SOURCE ENTITIES
    # ============================================================

    def entity_upsert(self, source_id: str, source_type: str, doc_s3_key: str = None,
                      change_signal: dict = None, status: str = None) -> None:
        """Create or partially update a global entity's discovery metadata.
        Only fields explicitly passed (non-None) are changed on an existing row —
        this lets callers mark status='discovering' without clobbering a
        previously-saved doc_s3_key/change_signal if that attempt later fails.
        One INSERT ... ON CONFLICT statement, so two concurrent first-time upserts
        of the same source_id cannot both INSERT (no IntegrityError)."""
        now = datetime.now(timezone.utc).isoformat()
        signal = json.dumps(change_signal) if change_signal is not None else None
        with self._db() as conn:
            conn.execute(
                """INSERT INTO data_source_entities
                   (source_id, source_type, doc_s3_key, change_signal, status, last_discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       source_type = excluded.source_type,
                       doc_s3_key = COALESCE(?, doc_s3_key),
                       change_signal = COALESCE(?, change_signal),
                       status = COALESCE(?, status),
                       last_discovered_at = excluded.last_discovered_at,
                       updated_at = ?""",
                (source_id, source_type, doc_s3_key, signal, status or "pending", now,
                 doc_s3_key, signal, status, now)
            )

    def entity_get(self, source_id: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM data_source_entities WHERE source_id = ?", (source_id,)
            ).fetchone()
        if not row:
            return None
        d = iso_utc_cols(dict(row), ENTITY_TS)
        d["change_signal"] = json.loads(d["change_signal"]) if d.get("change_signal") else None
        return d

    def entity_list(self, source_type: str = None) -> list[dict]:
        with self._db() as conn:
            if source_type:
                rows = conn.execute(
                    "SELECT * FROM data_source_entities WHERE source_type = ? ORDER BY source_id",
                    (source_type,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM data_source_entities ORDER BY source_id").fetchall()
        result = []
        for row in rows:
            d = iso_utc_cols(dict(row), ENTITY_TS)
            d["change_signal"] = json.loads(d["change_signal"]) if d.get("change_signal") else None
            result.append(d)
        return result

    # ============================================================
    # LOCAL FILE ENTITIES (session-scoped — uploaded /tmp files)
    # ============================================================

    def local_entity_upsert(self, session_id: str, filepath: str, doc_s3_key: str = None,
                            change_signal: dict = None, status: str = None) -> None:
        """Create or partially update a local file's discovery metadata for one session.
        Same partial-update semantics (and single-statement upsert) as entity_upsert."""
        now = datetime.now(timezone.utc).isoformat()
        signal = json.dumps(change_signal) if change_signal is not None else None
        with self._db() as conn:
            conn.execute(
                """INSERT INTO local_file_entities
                   (session_id, filepath, doc_s3_key, change_signal, status, last_discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, filepath) DO UPDATE SET
                       doc_s3_key = COALESCE(?, doc_s3_key),
                       change_signal = COALESCE(?, change_signal),
                       status = COALESCE(?, status),
                       last_discovered_at = excluded.last_discovered_at,
                       updated_at = ?""",
                (session_id, filepath, doc_s3_key, signal, status or "pending", now,
                 doc_s3_key, signal, status, now)
            )

    def local_entity_get(self, session_id: str, filepath: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM local_file_entities WHERE session_id = ? AND filepath = ?",
                (session_id, filepath)
            ).fetchone()
        if not row:
            return None
        d = iso_utc_cols(dict(row), ENTITY_TS)
        d["change_signal"] = json.loads(d["change_signal"]) if d.get("change_signal") else None
        return d

    def local_entity_list(self, session_id: str) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM local_file_entities WHERE session_id = ? ORDER BY filepath",
                (session_id,)
            ).fetchall()
        result = []
        for row in rows:
            d = iso_utc_cols(dict(row), ENTITY_TS)
            d["change_signal"] = json.loads(d["change_signal"]) if d.get("change_signal") else None
            result.append(d)
        return result
