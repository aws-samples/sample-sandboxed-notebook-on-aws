#!/usr/bin/env python3
# test-kind: offline
"""
Per-cell runtime fields round-trip through the storage interface.

Covers the notebook_cells columns peak_mem_mb, peak_cpu_pct and executed_at
(plus the existing execution fields) on a temp SQLite DB, and statically checks
that the Postgres backend defines the identical column set, maps rows the same
way, and that neither backend adds them via a migration. The dataflow columns
(vars_read_json, vars_written_json, stale_reason_json, sources_read_json) were
removed — dataflow lives in the notebook_dag document — and must be absent from
both backends.

Run:
    python3 tests/storage/test_cell_runtime_fields.py
"""
import os
import re
import sys
import tempfile
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

# ── tiny test harness (same style as test_storage_conformance) ───────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


NEW_COLUMNS = ["peak_mem_mb", "peak_cpu_pct", "executed_at", "image_s3_key"]
# Dataflow columns dropped from notebook_cells (now in notebook_dag).
REMOVED_COLUMNS = ["vars_read_json", "vars_written_json", "stale_reason_json",
                   "sources_read_json"]
REMOVED_KEYS = ["varsRead", "varsWritten", "staleReason", "sourcesRead"]

# The v_nb_cells column contract — identical names/order in BOTH backends (the
# committed list; new columns would be appended for CREATE OR REPLACE VIEW).
V_NB_CELLS_COLUMNS = [
    "notebook_id", "cell_id", "position", "type", "execution_number",
    "execution_time_sec", "is_generated", "has_error", "ai_explanation_source",
    "peak_mem_mb", "peak_cpu_pct", "executed_at", "has_image",
]

FULL_CELL = {
    "id": "c-full",
    "type": "code",
    "code": "df = load()",
    "output": "ok",
    "outputVariable": "df",
    "executionNumber": 7,
    "executionTime": 1.25,
    "lastExecutedCode": "df = load()",
    "peakMemMb": 512.5,
    "peakCpuPct": 87.25,
    "executedAt": "2025-01-02T03:04:05.678Z",
}
RUNTIME_KEYS = ["executionNumber", "executionTime", "lastExecutedCode",
                "peakMemMb", "peakCpuPct", "executedAt"]


def _temp_sqlite():
    import proxy.storage.sqlite_db as sq
    tmp = tempfile.mkdtemp(prefix="cellrt_sqlite_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "cellrt.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    s = sq.SqliteStorage()
    s.initialize()
    return s


def _pg_cell_columns_from_schema(stmts):
    """Regex-parse the column names of Postgres CREATE TABLE notebook_cells."""
    stmt = next(s for s in stmts if "CREATE TABLE IF NOT EXISTS notebook_cells" in s)
    body = stmt.split("CREATE TABLE IF NOT EXISTS notebook_cells (", 1)[1]
    cols = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--") or line.startswith(")"):
            continue
        if re.match(r"(PRIMARY KEY|FOREIGN KEY|UNIQUE|CONSTRAINT|CHECK)\b", line):
            continue
        m = re.match(r"([a-z_][a-z0-9_]*)\s", line)
        if m:
            cols.append(m.group(1))
    return cols


def _sqlite_roundtrip():
    print("\n[SQLite] runtime fields round-trip")
    s = _temp_sqlite()
    nb = s.notebook_create(name="rt", cells=[])
    nid = str(nb["id"])
    cells = [
        # Legacy dataflow keys from an old client are accepted and ignored.
        dict(FULL_CELL, varsRead=["x"], varsWritten=["df"],
             staleReason={"kind": "upstream", "vars": ["x"]},
             sourcesRead=[{"kind": "s3", "id": "s3://bucket/data.csv"}]),
        {"id": "c-junk", "code": "3", "peakMemMb": True, "peakCpuPct": float("nan")},
    ]
    check("notebook_update(cells=...) succeeds", s.notebook_update(nid, cells=cells))

    got = s.notebook_get(nid)
    by_id = {c["id"]: c for c in got["cells"]}
    full = by_id.get("c-full", {})
    for k in RUNTIME_KEYS:
        check(f"notebook_get: {k} round-trips", full.get(k) == FULL_CELL[k])
    check("peakMemMb is float", isinstance(full.get("peakMemMb"), float))
    check("executedAt is str", isinstance(full.get("executedAt"), str))
    check("removed dataflow keys are not returned",
          not any(k in full for k in REMOVED_KEYS))

    listed = next(n for n in s.notebook_list() if str(n["id"]) == nid)
    lfull = next(c for c in listed["cells"] if c["id"] == "c-full")
    check("notebook_list returns identical runtime fields",
          all(lfull.get(k) == full.get(k) for k in RUNTIME_KEYS))

    junk = by_id["c-junk"]
    check("bool peakMemMb -> None", junk["peakMemMb"] is None)
    check("non-finite peakCpuPct -> None", junk["peakCpuPct"] is None)
    check("missing executedAt -> None", junk["executedAt"] is None)

    # Raw SQL only for schema/view introspection.
    with s._db() as conn:
        sqlite_cols = [r[1] for r in conn.execute("PRAGMA table_info(notebook_cells)").fetchall()]
        view_row = conn.execute(
            "SELECT peak_mem_mb, peak_cpu_pct, executed_at FROM v_nb_cells "
            "WHERE notebook_id = ? AND cell_id = 'c-full'", (nid,)).fetchone()
        sqlite_view_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(v_nb_cells)").fetchall()]
    check("v_nb_cells exposes peak_mem_mb, peak_cpu_pct, executed_at",
          view_row is not None and tuple(view_row) ==
          (FULL_CELL["peakMemMb"], FULL_CELL["peakCpuPct"], FULL_CELL["executedAt"]))
    check("v_nb_cells column order (SQLite)", sqlite_view_cols == V_NB_CELLS_COLUMNS)
    _sqlite_view_refresh()
    return sqlite_cols


def _sqlite_view_refresh():
    """An EXISTING DB with an OLD v_nb_cells definition picks up the new one on the
    next initialize() — view is dropped + recreated, base-table rows untouched."""
    import proxy.storage.sqlite_db as sq
    s = _temp_sqlite()
    nb = s.notebook_create(name="refresh", cells=[])
    nid = str(nb["id"])
    s.notebook_update(nid, cells=[FULL_CELL])
    with s._db() as conn:
        conn.execute("DROP VIEW v_nb_cells")
        conn.execute("CREATE VIEW v_nb_cells AS SELECT c.notebook_id, c.cell_id "
                     "FROM notebook_cells c")
    s.initialize()  # simulate the next proxy start on the same DB file
    with s._db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(v_nb_cells)").fetchall()]
        n_rows = conn.execute("SELECT COUNT(*) FROM notebook_cells WHERE notebook_id = ?",
                              (nid,)).fetchone()[0]
    check("re-initialize recreates stale v_nb_cells with the new columns",
          cols == V_NB_CELLS_COLUMNS)
    check("re-initialize leaves notebook_cells data untouched", n_rows == 1)
    check("SQLite SCHEMA has no CREATE VIEW IF NOT EXISTS (views always refreshed)",
          "CREATE VIEW IF NOT EXISTS" not in sq.SCHEMA)


def _pg_view_columns(stmts, view):
    """Regex-parse the output column names of a Postgres CREATE OR REPLACE VIEW."""
    stmt = next(s for s in stmts if f"CREATE OR REPLACE VIEW {view} AS" in s)
    select = stmt.split("SELECT", 1)[1].rsplit("FROM", 1)[0]
    cols = []
    for line in select.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        m = re.search(r"\bAS\s+([a-z_][a-z0-9_]*)$", line) or re.match(
            r"[a-z_]+\.([a-z_][a-z0-9_]*)$", line)
        if m:
            cols.append(m.group(1))
    return cols


def _schema_parity(sqlite_cols):
    print("\n[Schema] SQLite <-> Postgres parity")
    import proxy.storage.postgres_db as pg
    from proxy.storage.sqlite_db import SqliteStorage
    check("importing postgres_db did not need psycopg2", "psycopg2" not in sys.modules)

    pg_cols = _pg_cell_columns_from_schema(pg._SCHEMA_STATEMENTS)
    check("notebook_cells column names/order identical", sqlite_cols == pg_cols)
    check("both include the runtime columns",
          all(c in sqlite_cols and c in pg_cols for c in NEW_COLUMNS))
    check("removed dataflow columns absent from notebook_cells in both backends",
          not any(c in sqlite_cols or c in pg_cols for c in REMOVED_COLUMNS))
    from proxy.storage.sqlite_db import SCHEMA as _SQ_SCHEMA
    check("removed dataflow columns absent from both schemas' text",
          not any(c in _SQ_SCHEMA or any(c in s for s in pg._SCHEMA_STATEMENTS)
                  for c in REMOVED_COLUMNS))
    check("v_nb_cells column names/order identical (Postgres == SQLite contract)",
          _pg_view_columns(pg._SCHEMA_STATEMENTS, "v_nb_cells") == V_NB_CELLS_COLUMNS)
    check("_CELL_COLUMNS identical across backends",
          SqliteStorage._CELL_COLUMNS == pg.PostgresStorage._CELL_COLUMNS)

    P = pg.PostgresStorage
    cols = [c.strip() for c in P._CELL_COLUMNS.split(",") if c.strip()]
    row = P._cell_to_row("1", 0, FULL_CELL)
    check("_cell_to_row arity == _CELL_COLUMNS", len(row) == len(cols) == P._CELL_COLUMN_COUNT)
    back = P._row_to_cell(dict(zip(cols, row)))
    check("Postgres _row_to_cell(_cell_to_row) round-trips runtime fields",
          all(back.get(k) == FULL_CELL[k] for k in RUNTIME_KEYS))
    check("Postgres _row_to_cell returns no removed dataflow keys",
          not any(k in back for k in REMOVED_KEYS))
    check("_row_to_cell key sets identical across backends",
          set(back) == set(SqliteStorage._row_to_cell(dict(zip(cols, row)))))

    for path in ("proxy/storage/sqlite_db.py", "proxy/storage/postgres_db.py"):
        text = open(os.path.join(ROOT, path)).read()
        bad = [c for c in NEW_COLUMNS
               if re.search(r"ADD COLUMN[^\n]*\b" + c + r"\b", text)
               or re.search(r"\(\s*\"notebook_cells\"\s*,\s*\"" + c + "\"", text)]
        check(f"{os.path.basename(path)}: no migration for new columns", not bad)


def test_cell_runtime_fields():
    """pytest entry point: the parity check needs the SQLite column list the
    roundtrip produces, so both run in order inside one test."""
    _checks.clear()
    _schema_parity(_sqlite_roundtrip())
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    cols = _sqlite_roundtrip()
    _schema_parity(cols)
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
