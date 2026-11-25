#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager MULTI-INSTANCE SAFETY test suite (FEAT-001).

Exercises the advisory-locked maintenance sweeps and the adopt-on-start
behaviour that make the pool_vms ledger safe to share across multiple
pool-manager instances:

  - a wrapped fill/recycle/reconcile sweep SKIPS (returns False, mutates
    nothing) while the "__pool_maintenance__" session lock is held, and RUNS
    once the lock is free;
  - the lock is released after every sweep, even when the sweep raises;
  - two managers over ONE shared SQLite DB never double-fill — while A holds
    the lock, B's sweep is skipped and the shared pool stops at target;
  - start() ADOPTS a pre-existing shared pool (locked reconcile+fill) instead
    of wiping it.

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_multitask.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from proxy.storage.sqlite_db import SqliteStorage  # noqa: E402

from tests.pool._poolkit import _run_sqlite, build_config, build_manager, mk_aged_row  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.manager import POOL_MAINTENANCE_LOCK_KEY  # noqa: E402


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── Section 1: a wrapped fill skips while the lock is held, runs once free ──
def test_second_fill_skips_while_lock_held():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=2, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    # Someone else already holds the maintenance lock.
    held = storage.try_session_lock(POOL_MAINTENANCE_LOCK_KEY)

    ran = run_async(manager._run_locked_maintenance("fill_loop", manager.fill_once))
    check("a wrapped fill returns False while the maintenance lock is held",
          held and ran is False)
    check("a skipped fill launches nothing",
          not any(name == "launch" for name, _ in backend.calls))
    check("a skipped fill leaves the shared pool empty",
          len(storage.pool_vm_list()) == 0)

    # Release and run again — now it must actually fill to target.
    storage.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)
    ran2 = run_async(manager._run_locked_maintenance("fill_loop", manager.fill_once))
    rows = storage.pool_vm_list()
    check("once the lock is free a wrapped fill returns True", ran2 is True)
    check("a freed fill tops the pool up to target (2)", len(rows) == 2)


# ── Section 2: recycle + reconcile also skip while the lock is held ─────────
def test_recycle_and_reconcile_skip_while_lock_held():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    # An aged 'available' row recycle would normally terminate+delete, and a
    # ghost row (no backend VM) reconcile would normally delete.
    vm = run_async(backend.launch("arm64", 2048, "pool-aged"))
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="available",
                age_seconds=10_000, endpoint=vm.endpoint)
    mk_aged_row(storage, "ghost-held", arch="arm64", size=2048, state="available")
    manager.config.max_age_hours = 0.001  # make the aged row recycle-eligible

    storage.try_session_lock(POOL_MAINTENANCE_LOCK_KEY)

    recycled = run_async(manager._run_locked_maintenance("recycle_loop", manager.recycle_once))
    reconciled = run_async(manager._run_locked_maintenance("reconcile_loop", manager.reconcile_once))
    check("a wrapped recycle_once returns False while the lock is held", recycled is False)
    check("a wrapped reconcile_once returns False while the lock is held", reconciled is False)
    rows_held = {r["vm_id"] for r in storage.pool_vm_list()}
    check("a skipped recycle/reconcile mutates nothing (both rows survive)",
          vm.vm_id in rows_held and "ghost-held" in rows_held)

    # Free the lock — both sweeps now run and do their work.
    storage.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)
    recycled2 = run_async(manager._run_locked_maintenance("recycle_loop", manager.recycle_once))
    reconciled2 = run_async(manager._run_locked_maintenance("reconcile_loop", manager.reconcile_once))
    rows_free = {r["vm_id"] for r in storage.pool_vm_list()}
    check("once free, recycle_once runs and removes the aged spare",
          recycled2 is True and vm.vm_id not in rows_free)
    check("once free, reconcile_once runs and removes the ghost row",
          reconciled2 is True and "ghost-held" not in rows_free)


# ── Section 3: the lock is released after each sweep ────────────────────────
def test_lock_released_after_each_sweep():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    run_async(manager._run_locked_maintenance("fill_loop", manager.fill_once))
    reacquired = storage.try_session_lock(POOL_MAINTENANCE_LOCK_KEY)
    check("the maintenance lock is re-acquirable immediately after a wrapped sweep",
          reacquired is True)
    storage.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)


# ── Section 4: the lock is released even if the sweep raises ────────────────
def test_lock_released_even_on_exception():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    async def boom():
        raise RuntimeError("sweep blew up")

    raised = False
    try:
        run_async(manager._run_locked_maintenance("fill_loop", boom))
    except RuntimeError:
        raised = True
    check("a raising sweep propagates its exception out of _run_locked_maintenance",
          raised)
    reacquired = storage.try_session_lock(POOL_MAINTENANCE_LOCK_KEY)
    check("the maintenance lock is still released (re-acquirable) after a sweep raises",
          reacquired is True)
    storage.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)


# ── Section 5: two managers, one DB, no double-fill ─────────────────────────
def test_two_managers_one_db_no_double_fill():
    # Manager A owns the shared DB (its _run_sqlite set sq.DB_PATH + cleared
    # the in-process held-locks set).
    storage_a = _run_sqlite()
    config_a = build_config(size_per_image=2, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager_a = build_manager(FakeVmBackend(), storage_a, config_a)

    # Manager B is a SEPARATE instance over the SAME DB file. Build its storage
    # as a BARE SqliteStorage() reading the module-global sq.DB_PATH — a second
    # _run_sqlite() call would clear _HELD_SESSION_LOCKS and void A's lock.
    storage_b = SqliteStorage()
    storage_b.initialize()
    config_b = build_config(size_per_image=2, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager_b = build_manager(FakeVmBackend(), storage_b, config_b)

    # A holds the maintenance lock across B's attempt (simulating A mid-sweep);
    # B's concurrent wrapped fill must be skipped so the SHARED pool is not
    # filled twice. The SQLite lock is an in-process set, so A's and B's
    # storages contend on the same module-global state.
    a_held = storage_a.try_session_lock(POOL_MAINTENANCE_LOCK_KEY)
    b_ran = run_async(manager_b._run_locked_maintenance("fill_loop", manager_b.fill_once))
    check("manager A acquires the shared maintenance lock", a_held is True)
    check("manager B's wrapped fill is skipped while A holds the shared lock",
          b_ran is False)
    check("manager B launched nothing while skipped",
          not any(name == "launch" for name, _ in manager_b.backend.calls))

    # A releases and fills the shared pool exactly once to target.
    storage_a.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)
    a_ran = run_async(manager_a._run_locked_maintenance("fill_loop", manager_a.fill_once))
    shared_rows = storage_a.pool_vm_list()
    check("manager A's wrapped fill runs once the lock is free", a_ran is True)
    check("the shared pool is filled to target ONCE (2), not 2x by two instances",
          len(shared_rows) == 2)


# ── Section 6: start() adopts an existing shared pool ───────────────────────
def test_start_adopts_existing_pool():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=2, arches=["arm64"], sizes_by_arch={"arm64": [2048]})

    # Seed a healthy, consistent pool at target: DB rows + matching backend VMs,
    # as a FIRST instance would have left behind.
    seeded = []
    for _ in range(2):
        vm = run_async(backend.launch("arm64", 2048, "pool-preexisting"))
        mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="available",
                    endpoint=vm.endpoint)
        seeded.append(vm.vm_id)

    manager = build_manager(backend, storage, config)

    async def scenario():
        await manager.start()
        try:
            return storage.pool_vm_list()
        finally:
            await manager.stop()

    rows = run_async(scenario())
    vm_ids = {r["vm_id"] for r in rows}
    check("start() ADOPTS the pre-existing shared pool rows (none wiped)",
          all(vm_id in vm_ids for vm_id in seeded))
    check("start() leaves the adopted pool at/above target (2) — no double-fill",
          len(rows) >= 2 and len(rows) == 2)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager MULTI-INSTANCE SAFETY ===")
    print("\n-- a wrapped fill skips while the lock is held --")
    test_second_fill_skips_while_lock_held()
    print("\n-- recycle + reconcile skip while the lock is held --")
    test_recycle_and_reconcile_skip_while_lock_held()
    print("\n-- the lock is released after each sweep --")
    test_lock_released_after_each_sweep()
    print("\n-- the lock is released even on exception --")
    test_lock_released_even_on_exception()
    print("\n-- two managers, one DB, no double-fill --")
    test_two_managers_one_db_no_double_fill()
    print("\n-- start() adopts an existing shared pool --")
    test_start_adopts_existing_pool()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL MULTI-INSTANCE SAFETY CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_multitask():
    _checks.clear()
    for fn in (test_second_fill_skips_while_lock_held,
               test_recycle_and_reconcile_skip_while_lock_held,
               test_lock_released_after_each_sweep,
               test_lock_released_even_on_exception,
               test_two_managers_one_db_no_double_fill,
               test_start_adopts_existing_pool):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool multi-instance safety failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
