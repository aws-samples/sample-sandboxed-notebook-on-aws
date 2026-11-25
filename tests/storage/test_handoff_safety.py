#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for ECS+ALB session-handoff safety (no AWS, no MicroVM, no proxy
process). Two properties are proven:

  (1) Cost is handoff-safe. When a task is drained/replaced, the only value that
      lives ONLY in memory is CostTracker's burst_mb_seconds. persist_all() must
      flush it, and a fresh CostTracker on the next task must reconstruct the
      same cost via load_from_db (running/suspended seconds come back from
      vm_state_log; burst comes back from the persisted column).

  (2) The graceful-shutdown handler flushes ALL cost and closes active terminal
      WebSocket relays — so a rolling deploy neither loses burst cost nor
      hard-cuts terminals.

Run:
    python3 tests/storage/test_handoff_safety.py
"""

import os
import sys
import time
import asyncio
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


# ── tiny harness ─────────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _fresh_sqlite():
    """Point the SQLite backend at a throwaway temp DB and return the storage."""
    import proxy.storage.sqlite_db as sq
    import threading
    tmp = tempfile.mkdtemp(prefix="handoff_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "h.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    s = sq.SqliteStorage()
    s.initialize()
    return s


# ── (1) cost handoff round-trip ──────────────────────────────────────────────
def test_cost_handoff():
    print("\n=== Cost handoff safety (persist_all -> load_from_db) ===")
    from proxy.platform.cost_tracker import CostTracker

    storage = _fresh_sqlite()
    vm = "vm-handoff-1"
    # A real session row + RUNNING state-log entry (this is what load_from_db reads
    # to reconstruct running/suspended seconds on the next task).
    storage.vm_session_create(vm, session_id="sess-h", memory_mib=4096,
                              endpoint="http://e", checkpoint_enabled=True)

    # ---- Task A accumulates cost in memory ----
    a = CostTracker()
    a.load_from_db(storage)  # picks up the RUNNING transition from the log
    check("task A loaded the running VM from DB", vm in a.tracked_ids)

    # Accumulate burst MB-seconds (the ONLY in-memory-only value). One sample
    # above the 4096 MiB baseline, ~1s after the last poll -> deterministic overage.
    rec = a._records[vm]
    rec._last_burst_poll = time.time() - 1.0
    a.record_burst(vm, used_mb=4096 + 512)   # +512 MB over baseline for ~1s
    burst_before = a.get_cost(vm)["burst_mb_seconds"]
    check("task A accumulated burst_mb_seconds > 0", burst_before > 0)

    # running_secs is DERIVED from the state-log timestamps, which are the DB's
    # source of truth (load_from_db recomputes from the log, NOT from the
    # persisted running_secs column). So the handoff-safe assertion is that Task B
    # sees the SAME derived running_secs as Task A — both read the same log.
    run_a = a.get_cost(vm)["running_secs"]

    # ---- Graceful drain: flush everything ----
    n = a.persist_all(storage)
    check("persist_all flushed 1 record", n == 1)

    # DB now holds the burst (the value that would otherwise be lost).
    db_row = storage.vm_session_get(vm)
    check("DB burst_mb_seconds persisted",
          abs(db_row["burst_mb_seconds"] - burst_before) < 0.5)

    # ---- Task B (new ECS task) reconstructs from DB ----
    b = CostTracker()
    b.load_from_db(storage)
    check("task B reloaded the VM", vm in b.tracked_ids)
    cost_b = b.get_cost(vm)
    check("task B burst_mb_seconds preserved across handoff",
          abs(cost_b["burst_mb_seconds"] - burst_before) < 0.5)
    # running_secs is re-derived from the shared state-log => matches Task A
    # (within a small delta for wall-clock elapsed between the two compute() calls).
    check("task B running_secs matches Task A (re-derived from state-log, handoff-safe)",
          abs(cost_b["running_secs"] - run_a) <= 1)

    # ---- Prove the gap that persist_all closes: burst is lost WITHOUT a flush ----
    storage2 = _fresh_sqlite()
    storage2.vm_session_create(vm, session_id="sess-h", memory_mib=4096,
                               endpoint="http://e", checkpoint_enabled=True)
    c = CostTracker()
    c.load_from_db(storage2)
    c._records[vm]._last_burst_poll = time.time() - 1.0
    c.record_burst(vm, used_mb=4096 + 512)
    # NO persist_all here (simulates a hard crash mid-interval).
    d = CostTracker()
    d.load_from_db(storage2)
    check("without flush, burst delta is lost (motivates persist_all)",
          d.get_cost(vm)["burst_mb_seconds"] == 0)


# ── (2) graceful shutdown handler ────────────────────────────────────────────
class _FakeWS:
    def __init__(self):
        self.closed = False
        self.close_args = None

    async def close(self, code=1000, reason=""):
        self.closed = True
        self.close_args = (code, reason)


def _install_no_aws_guard(calls):
    """Make ANY botocore API call fail loudly (recorded in ``calls``) so the
    shutdown test provably never reaches AWS. Returns an undo callable."""
    import botocore.client

    orig = botocore.client.BaseClient._make_api_call

    def _blocked(self, operation_name, api_params):
        calls.append(f"{self.meta.service_model.service_name}.{operation_name}")
        raise AssertionError(f"real AWS call in an offline test: {operation_name}")

    botocore.client.BaseClient._make_api_call = _blocked

    def _undo():
        botocore.client.BaseClient._make_api_call = orig
    return _undo


def test_shutdown_handler():
    print("\n=== Graceful shutdown via FastAPI lifespan (flush cost + close relays) ===")
    aws_calls = []
    undo_aws_guard = _install_no_aws_guard(aws_calls)

    # Isolate the DB BEFORE importing proxy.server (its module body calls
    # storage.initialize()). We import the shared singleton and repoint it.
    storage = _fresh_sqlite()

    import proxy.storage as storage_pkg
    storage_pkg.storage = storage  # so server's `from proxy.storage import storage` alias matches

    # Importing server is heavier (pulls routers); do it now that DB is safe.
    import importlib
    import proxy.server as server
    importlib.reload(server)  # re-run module body against the patched storage
    # server.storage is its own bound name; point it at our temp DB too.
    server.storage = storage
    from proxy.platform import http_clients
    from proxy.platform.datasources import registry

    # A tracked VM (RUNNING state-log row) for the lifespan startup to load.
    vm = "vm-shutdown-1"
    storage.vm_session_create(vm, session_id="sess-s", memory_mib=4096, endpoint="http://e")

    # Startup steps that would reach AWS are no-op'd for this offline test (the
    # lifespan itself is driven unmodified): registry restore (get_microvm per
    # VM), the background registry reconcile, and the catalog pre-warm fan-out.
    mgr = server.app.state.vm_manager
    mgr.restore_session_registry_from_db = lambda: None

    async def _no_reconcile():
        return None

    async def _no_catalog():
        return []

    mgr.reconcile_session_registry = _no_reconcile
    orig_discover = registry.discover_full_catalog
    registry.discover_full_catalog = _no_catalog

    fake_ws = _FakeWS()
    server.app.state.active_terminals = {fake_ws}
    tracker = mgr.cost_tracker
    seen = {}

    async def _drive():
        # Enter/exit the app's lifespan exactly as the ASGI server does.
        async with server.app.router.lifespan_context(server.app):
            await asyncio.sleep(0)  # let the startup background tasks start
            seen["loaded"] = vm in tracker.tracked_ids
            seen["tasks"] = [getattr(server.app.state, a) for a in
                             ("housekeeping_task", "reconcile_task", "catalog_prewarm_task")]
            seen["clients"] = [http_clients.vm_client(), http_clients.aux_client()]
            # Burst accrues while serving; never persisted before shutdown.
            tracker._records[vm]._last_burst_poll = time.time() - 1.0
            tracker.record_burst(vm, used_mb=4096 + 256)
            seen["burst"] = tracker.get_cost(vm)["burst_mb_seconds"]

    try:
        asyncio.run(_drive())
    finally:
        registry.discover_full_catalog = orig_discover
        undo_aws_guard()

    check("startup: lifespan loaded the tracked VM from DB", seen.get("loaded"))
    burst_before = seen.get("burst", 0)
    check("shutdown: seeded un-persisted burst > 0", burst_before > 0)
    tasks = seen.get("tasks") or []
    check("shutdown: housekeeping task cancelled",
          bool(tasks) and tasks[0] is not None and (tasks[0].cancelled() or tasks[0].done()))
    check("shutdown: reconcile + catalog pre-warm tasks finished",
          len(tasks) == 3 and all(t is not None and t.done() for t in tasks[1:]))
    check("shutdown: terminal relay closed", fake_ws.closed is True)
    check("shutdown: relay closed with going-away code 1001",
          fake_ws.close_args and fake_ws.close_args[0] == 1001)
    clients = seen.get("clients") or []
    check("shutdown: shared httpx clients closed",
          len(clients) == 2 and all(c.is_closed for c in clients)
          and http_clients._vm is None and http_clients._aux is None)

    db_row = storage.vm_session_get(vm)
    check("shutdown: burst flushed to DB",
          abs(db_row["burst_mb_seconds"] - burst_before) < 0.5)
    check("offline: NO AWS API call attempted", not aws_calls)
    if aws_calls:
        print(f"     attempted AWS calls: {aws_calls}")


def main():
    test_cost_handoff()
    test_shutdown_handler()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nHANDOFF SAFETY: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL HANDOFF-SAFETY CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
