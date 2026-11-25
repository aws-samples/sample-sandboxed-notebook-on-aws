"""
Shared offline test helpers for the pool_manager/ test suite (Phase 3 —
FEAT-002). NOT a standalone test file — no `main()`, nothing runs it directly.

Provides:
  _run_sqlite()      — the exact temp-SQLite reset pattern from
                        tests/pool/test_pool_storage.py (a temp SQLite file IS
                        the real Phase-2 storage; no mocking of StorageBackend).
  mk_aged_row()       — insert a pool_vms row directly via storage, optionally
                        backdated by age_seconds (no sleeping needed in tests).
  build_config()      — a pool_manager.config.PoolConfig for tests, without
                        touching os.environ.
  build_manager()     — a pool_manager.manager.PoolManager wired to a given
                        backend + storage + config.
"""

import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.storage.sqlite_db as sq  # noqa: E402
from proxy.storage.sqlite_db import SqliteStorage  # noqa: E402

from pool_manager.config import PoolConfig  # noqa: E402
from pool_manager.manager import PoolManager  # noqa: E402


def _run_sqlite(db_path=None):
    """Fresh SQLite backend. If db_path is None, uses a private temp dir (not
    shared across threads/processes). If given, points at that exact file —
    used by concurrency tests so multiple threads/instances share one DB
    file. Mirrors tests/pool/test_pool_storage.py::_run_sqlite exactly."""
    if db_path is None:
        tmp = tempfile.mkdtemp(prefix="pool_sqlite_")
        db_path = os.path.join(tmp, "pool.db")
    sq.DB_DIR = os.path.dirname(db_path)
    sq.DB_PATH = db_path
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    storage = SqliteStorage()
    storage.initialize()
    return storage


def mk_aged_row(storage, vm_id, arch="arm64", size=2048, state="available",
                 age_seconds=0, endpoint=None, fake_session_id=None):
    """Create a pool_vms row directly through storage (bypassing the backend),
    optionally backdating launched_at by age_seconds. Returns the row dict."""
    fake_session_id = fake_session_id or f"pool-{vm_id}"
    endpoint = endpoint or f"fake://{vm_id}"
    launched_at = None
    if age_seconds:
        launched_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    row = storage.pool_vm_create(vm_id, arch, size, endpoint, fake_session_id,
                                 launched_at=launched_at, state="launching")
    if state != "launching":
        storage.pool_vm_set_state(vm_id, state)
        row = storage.pool_vm_list(state=state, architecture=arch, size_mib=size)
        row = next(r for r in row if r["vm_id"] == vm_id)
    return row


def build_config(**overrides) -> PoolConfig:
    """A PoolConfig for tests without touching os.environ. Defaults mirror
    load_config()'s defaults; pass overrides as kwargs (e.g. arches=['arm64'],
    sizes_by_arch={'arm64':[2048]}, size_per_image=2, fill_concurrency=4)."""
    defaults = dict(
        enabled=True,
        size_per_image=2,
        size_overrides={},
        max_age_hours=2.0,
        port=8090,
        fill_concurrency=4,
        backend="fake",
        arches=["arm64"],
        sizes_by_arch={"arm64": [2048]},
    )
    defaults.update(overrides)
    return PoolConfig(**defaults)


def build_manager(backend, storage, config=None, **manager_kwargs) -> PoolManager:
    """A PoolManager wired to the given backend/storage/config. If config is
    omitted, build_config() defaults are used."""
    config = config or build_config()
    return PoolManager(backend=backend, storage=storage, config=config, **manager_kwargs)
