#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit checks for notebook_dag in the SQLite -> Aurora sync worker.

  * notebook_dag.notebook_id is registered BIGINT and updated_at TIMESTAMP (built_at
    stays TEXT on both sides), so the Data API params are typed correctly;
  * introspection picks notebook_dag up generically from a fresh SQLite schema as a
    cascade child of notebooks with PK notebook_id and watermark updated_at;
  * the generic oversize guard: a row whose estimated size exceeds
    _MAX_REQUEST_BYTES gets an unsyncable reason (skipped, never truncated), a
    normal row does not, and a dry-run batch_upsert keeps only the normal row;
  * change check (delta-plan §0.2): the worker selects notebook_dag rows by their
    own watermark (`updated_at`, _TIMESTAMP_COLUMNS; _rows_since: watermark >
    cursor). A runtime-only write (version kept, runtime_version + 1) still sets
    updated_at, so it is picked up; a no-op write touches nothing and is not.
    sync/sync_to_aurora.py needs no change for this.

No AWS calls (dry-run writer), no live DB (temp SQLite file).

Run:
    .venv/bin/python tests/sync/test_sync_dag_unit.py
"""
import os
import sqlite3
import sys
import tempfile
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


DAG_COLUMNS = ["notebook_id", "version", "built_at", "dag_json", "updated_at"]


def _registrations():
    print("\n[sync] notebook_dag type registrations")
    import sync.sync_to_aurora as s
    check("('notebook_dag','notebook_id') in _BIGINT_COLUMNS",
          ("notebook_dag", "notebook_id") in s._BIGINT_COLUMNS)
    check("('notebook_dag','updated_at') in _TIMESTAMP_COLUMNS",
          ("notebook_dag", "updated_at") in s._TIMESTAMP_COLUMNS)
    check("built_at is NOT a TIMESTAMP column (TEXT on both sides)",
          ("notebook_dag", "built_at") not in s._TIMESTAMP_COLUMNS)
    f_id = s._field("notebook_dag", "notebook_id", "14")
    check("notebook_id is sent as longValue", f_id["value"] == {"longValue": 14})
    f_ts = s._field("notebook_dag", "updated_at", "2026-05-01T10:00:00.123Z")
    check("updated_at is sent with the TIMESTAMP typeHint",
          f_ts.get("typeHint") == "TIMESTAMP"
          and f_ts["value"] == {"stringValue": "2026-05-01 10:00:00.123"})
    f_built = s._field("notebook_dag", "built_at", "2026-05-01T10:00:00.123Z")
    check("built_at is sent as plain text",
          "typeHint" not in f_built
          and f_built["value"] == {"stringValue": "2026-05-01T10:00:00.123Z"})


def _introspection():
    print("\n[sync] introspection of notebook_dag (generic, no registration)")
    import proxy.storage.sqlite_db as sq
    import sync.sync_to_aurora as s
    tmp = tempfile.mkdtemp(prefix="syncdag_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "syncdag.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    sq.SqliteStorage().initialize()
    conn = sqlite3.connect(sq.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        tables = s.introspect(conn)
        order = s.topo_order(tables)
        ti = tables.get("notebook_dag")
        check("notebook_dag discovered as an operational table", ti is not None)
        if ti is None:
            return
        check("notebook_dag PK is notebook_id", ti.pk_cols == ["notebook_id"])
        check("notebook_dag is a cascade child of notebooks",
              ti.is_cascade_child and ti.parents == ["notebooks"])
        check("notebook_dag watermark is updated_at", ti.watermark == "updated_at")
        check("notebook_dag columns match the DDL", ti.columns == DAG_COLUMNS)
        check("notebooks is upserted before notebook_dag",
              order.index("notebooks") < order.index("notebook_dag"))
        check("parent-scoped link column is notebook_id",
              s._child_link_column(conn, ti, tables["notebooks"]) == "notebook_id")
        has_trigger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='notebook_dag'"
        ).fetchone()
        check("no tombstone trigger on notebook_dag (cascade child)", has_trigger is None)
    finally:
        conn.close()


def _oversize_guard():
    print("\n[sync] generic oversize-row guard")
    import sync.sync_to_aurora as s
    normal = {"notebook_id": 1, "version": 3, "built_at": "2026-05-01T10:00:00.123Z",
              "dag_json": '{"schema":1,"nodes":[]}', "updated_at": "2026-05-01T10:00:00.123Z"}
    big = dict(normal, notebook_id=2, dag_json="x" * (s._MAX_REQUEST_BYTES + 1))
    check("normal row has no unsyncable reason",
          s._row_unsyncable_reason("notebook_dag", DAG_COLUMNS, normal) is None)
    reason = s._row_unsyncable_reason("notebook_dag", DAG_COLUMNS, big)
    check("oversized row yields an unsyncable reason",
          isinstance(reason, str) and "not truncated" in reason)
    check("the guard is generic (any table)",
          s._row_unsyncable_reason("notebook_cells", ["notebook_id", "code"],
                                   {"notebook_id": 1, "code": "y" * (s._MAX_REQUEST_BYTES + 1)})
          is not None)
    check("a row just under the limit is still synced",
          s._row_unsyncable_reason(
              "notebook_dag", ["dag_json"],
              {"dag_json": "z" * (s._MAX_REQUEST_BYTES - len("dag_json") - 24)}) is None)
    w = s.AuroraDataApiWriter("arn:cluster", "arn:secret", "db", "us-west-2", dry_run=True)
    kept = w.batch_upsert("notebook_dag", DAG_COLUMNS, ["notebook_id"], [big, normal])
    check("dry-run batch_upsert skips the oversized row and keeps the normal one", kept == 1)
    check("the oversized row object is not modified (never truncated)",
          len(big["dag_json"]) == s._MAX_REQUEST_BYTES + 1)


def _runtime_only_watermark():
    print("\n[sync] runtime-only writes advance the notebook_dag watermark; no-ops do not")
    import time
    import proxy.storage.sqlite_db as sq
    import sync.sync_to_aurora as s
    tmp = tempfile.mkdtemp(prefix="syncdag_rt_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "syncdag_rt.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    st = sq.SqliteStorage()
    st.initialize()
    nid = st.notebook_create("sync-rt", cells=[])["id"]
    base = {"schema": 1, "version": 0, "built_at": None, "run_seq": 0, "nodes": [], "edges": [],
            "issues": [], "runtime": {"clock": 0, "vars": {}, "runs": {}}, "pending": {}}
    st.notebook_dag_write(nid, lambda prev: dict(base))
    conn = sqlite3.connect(sq.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ti = s.introspect(conn)["notebook_dag"]
        cursor = max(r[ti.watermark] for r in s._rows_since(conn, ti, None))
        time.sleep(0.01)
        w = st.notebook_dag_write(nid, lambda prev: dict(prev, runtime={"clock": 3, "vars": {}, "runs": {}}))
        rows = s._rows_since(conn, ti, cursor)
        check("runtime-only write: version kept, runtime_version bumped",
              w.wrote and not w.graph_changed and w.doc["version"] == 1 and w.doc["runtime_version"] == 2)
        check("runtime-only write is selected by _rows_since (watermark > cursor)",
              len(rows) == 1 and rows[0]["notebook_id"] == nid and '"clock":3' in rows[0]["dag_json"])
        cursor = max(r[ti.watermark] for r in rows)
        time.sleep(0.01)
        w = st.notebook_dag_write(nid, lambda prev: dict(prev, trigger="noop"))
        check("no-op write: nothing written", w.wrote is False)
        check("no-op write is not selected by _rows_since", s._rows_since(conn, ti, cursor) == [])
    finally:
        conn.close()


def _run_all():
    _registrations()
    _introspection()
    _oversize_guard()
    _runtime_only_watermark()


def test_sync_dag_unit():
    _checks.clear()
    _run_all()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    _run_all()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
