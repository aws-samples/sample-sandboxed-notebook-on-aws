#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: pool_manager creates the DB tables it needs at startup, and never
drops, recreates or alters tables that already exist.

  1. A fresh, empty SQLite file: initialize_pool_manager_schema() creates
     pool_vms (+ idx_pool_vms_pick), session_registry and vm_sessions, and
     nothing else (no proxy-only tables, views or sync triggers).
  2. The created tables have exactly the columns the proxy's full initialize()
     creates (the DDL is sliced from the same SCHEMA, so it cannot drift).
  3. A DB the proxy already initialized, with rows in every table: calling it
     (twice) leaves sqlite_master (table SQL + rootpage, so no drop/recreate)
     and every row unchanged.
  4. An existing pool_vms with an older/different shape is left exactly as is
     (no migration).
  5. create_app()'s lifespan runs it: a standalone pool_manager started on an
     empty DB serves /status instead of failing with "no such table: pool_vms".
  6. Postgres: the statement subset is the 4 CREATE ... IF NOT EXISTS
     statements (no DROP / ALTER), the method runs only those, and opens the
     pool if needed. Against a real server only when TEST_PG_DSN is set.

Run:
    .venv/bin/python tests/pool/test_pool_schema_init.py
    (or under pytest: python3 -m pytest tests/pool/test_pool_schema_init.py)
"""
import os
import sqlite3
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.storage.sqlite_db as sq  # noqa: E402
import proxy.storage.postgres_db as pg  # noqa: E402
from proxy.storage.sqlite_db import SqliteStorage  # noqa: E402
from tests.pool._poolkit import build_config  # noqa: E402

POOL_TABLES = {"pool_vms", "session_registry", "vm_sessions"}


def _fresh_storage():
    """SqliteStorage on a new temp file WITHOUT running initialize()."""
    tmp = tempfile.mkdtemp(prefix="pool_schema_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "pool.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    return SqliteStorage()


def _raw(path=None):
    return sqlite3.connect(path or sq.DB_PATH)


def _master():
    with _raw() as c:
        return sorted(c.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master").fetchall(),
            key=lambda r: (r[0], r[1]))


def _objects(kind):
    with _raw() as c:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
            (kind,))}


def _columns(table, path=None):
    with _raw(path) as c:
        return [tuple(r) for r in c.execute(f"PRAGMA table_info({table})")]


def _dump(table):
    with _raw() as c:
        return sorted(tuple(r) for r in c.execute(f"SELECT * FROM {table}"))


def test_fresh_db_gets_only_pool_tables():
    storage = _fresh_storage()
    assert _objects("table") == set()
    storage.initialize_pool_manager_schema()
    assert _objects("table") == POOL_TABLES
    assert _objects("index") == {"idx_pool_vms_pick"}
    assert _objects("view") == set()
    assert _objects("trigger") == set()
    # Usable through the storage interface straight away.
    storage.pool_vm_create("vm-a", "arm64", 2048, "fake://vm-a", "pool-vm-a")
    assert [r["vm_id"] for r in storage.pool_vm_list()] == ["vm-a"]
    assert storage.session_registry_get("nope") is None
    assert storage.vm_session_list_for_session("nope") == []


def test_columns_match_full_initialize():
    storage = _fresh_storage()
    storage.initialize_pool_manager_schema()
    pool_only = sq.DB_PATH
    full = _fresh_storage()
    full.initialize()
    for table in POOL_TABLES:
        assert _columns(table, pool_only) == _columns(table, sq.DB_PATH), table


def test_existing_tables_and_rows_untouched():
    storage = _fresh_storage()
    storage.initialize()  # the proxy's full schema, as in local mode
    storage.pool_vm_create("vm-keep", "arm64", 2048, "fake://vm-keep", "pool-vm-keep",
                           state="available")
    storage.session_registry_upsert("sess-keep", "vm-keep", "fake://vm-keep")
    storage.vm_session_create(microvm_id="vm-keep", notebook_id=1, session_id="sess-keep",
                              memory_mib=2048, endpoint="fake://vm-keep")
    nb = storage.notebook_create(name="keep-me")
    before_master = _master()
    before_rows = {t: _dump(t) for t in POOL_TABLES | {"notebooks"}}
    assert all(before_rows[t] for t in before_rows)

    storage.initialize_pool_manager_schema()
    storage.initialize_pool_manager_schema()

    assert _master() == before_master
    assert {t: _dump(t) for t in before_rows} == before_rows
    assert storage.notebook_get(nb["id"])["name"] == "keep-me"


def test_existing_table_with_other_shape_is_not_migrated():
    storage = _fresh_storage()
    with _raw() as c:
        # Older shape: lacks endpoint/fake_session_id/..., has an extra column.
        c.execute("CREATE TABLE pool_vms (vm_id TEXT PRIMARY KEY, architecture TEXT, "
                  "size_mib INTEGER, state TEXT, launched_at TEXT, legacy_col TEXT)")
        c.execute("INSERT INTO pool_vms VALUES ('vm-old', 'arm64', 2048, 'available', "
                  "'2024-01-01T00:00:00+00:00', 'x')")
    before_cols = _columns("pool_vms")
    before_rows = _dump("pool_vms")
    storage.initialize_pool_manager_schema()
    assert _columns("pool_vms") == before_cols
    assert _dump("pool_vms") == before_rows
    # The missing tables are still created.
    assert {"session_registry", "vm_sessions"} <= _objects("table")


def test_startup_creates_missing_tables():
    from fastapi.testclient import TestClient
    from pool_manager.fake_backend import FakeVmBackend
    from pool_manager.server import create_app

    storage = _fresh_storage()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    app = create_app(config=config, backend=FakeVmBackend(), storage=storage)
    with TestClient(app) as client:
        assert POOL_TABLES <= _objects("table")
        resp = client.get("/status")
        assert resp.status_code == 200, resp.text


def test_startup_keeps_existing_schema():
    from fastapi.testclient import TestClient
    from pool_manager.fake_backend import FakeVmBackend
    from pool_manager.server import create_app

    storage = _fresh_storage()
    storage.initialize()
    storage.notebook_create(name="still-here")
    before_master = _master()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    app = create_app(config=config, backend=FakeVmBackend(), storage=storage)
    with TestClient(app):
        pass
    assert _master() == before_master
    assert [r[1] for r in _dump("notebooks")] == ["still-here"]


# ── Postgres ────────────────────────────────────────────────────────────────

def test_postgres_statement_subset_is_create_if_not_exists_only():
    stmts = pg._POOL_MANAGER_SCHEMA_STATEMENTS
    assert len(stmts) == 4
    for s in stmts:
        assert s in pg._SCHEMA_STATEMENTS
        code = "\n".join(line.split("--")[0] for line in s.splitlines()).upper()
        assert "IF NOT EXISTS" in code
        assert "DROP" not in code and "ALTER" not in code
    joined = "\n".join(stmts)
    for name in ("vm_sessions (", "session_registry (", "pool_vms (", "idx_pool_vms_pick"):
        assert name in joined


def test_postgres_method_runs_only_the_subset():
    ran, opened = [], []

    class _Cur:
        def execute(self, stmt):
            ran.append(stmt)

    store = pg.PostgresStorage()
    store._dsn = "postgresql://unused"
    store._open_pool = lambda dsn="": (opened.append(dsn), setattr(store, "_pool", object()))

    @contextmanager
    def _db(dict_rows=False):
        yield None, _Cur()

    store._db = _db
    store.initialize_pool_manager_schema()
    assert opened == ["postgresql://unused"]
    assert ran == pg._POOL_MANAGER_SCHEMA_STATEMENTS
    store.initialize_pool_manager_schema()  # pool already open: not reopened
    assert opened == ["postgresql://unused"]


def test_postgres_live_idempotent():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        print("  (skipping live Postgres: TEST_PG_DSN not set)")
        return
    import psycopg2  # noqa: F401  (asked for Postgres: a missing driver should fail)
    store = pg.PostgresStorage()
    store._dsn = dsn
    store.initialize_pool_manager_schema()
    vm_id = f"vm-schema-{uuid.uuid4().hex[:8]}"
    store.pool_vm_create(vm_id, "arm64", 2048, f"fake://{vm_id}", f"pool-{vm_id}")
    try:
        store.initialize_pool_manager_schema()
        assert vm_id in {r["vm_id"] for r in store.pool_vm_list()}
    finally:
        store.pool_vm_delete(vm_id)


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  \u2713 {name}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  \u274c {name}: {e!r}")
    print(f"\nRESULT: {len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
