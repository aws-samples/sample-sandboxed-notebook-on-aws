#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager ORPHAN RECOVERY test suite (Phase 3 — FEAT-002, plan.md item 11).

Exercises PoolManager.reconcile_once() against FakeVmBackend + a temp SQLite
file: a DB row with no backend VM is deleted; a backend VM with no pool_vms row
is LEFT ALONE (it is a proxy notebook VM, not a pool orphan — the pool never
reaps VMs it didn't create); a stale 'granting' row (older than the
grant-timeout) is terminated+deleted; reconcile is clean/no-op at startup when
the DB and backend already agree, including a pre-populated-divergent DB that
converges after exactly one reconcile_once() call.

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_orphans.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config, build_manager, mk_aged_row  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.vm_backend import RUNNING  # noqa: E402


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


def _backdate_updated_at(storage, vm_id, seconds_ago):
    """Force a pool_vms row's updated_at into the past (mk_aged_row only
    backdates launched_at; the stale-'granting' check keys off updated_at)."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    storage.pool_vm_backdate(vm_id, updated_at=old)


# ── Section 1: DB row with no backend VM -> deleted ────────────────────────
def test_db_row_no_backend_vm_deleted():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    # A pool_vms row exists but no corresponding backend VM was ever launched.
    mk_aged_row(storage, "vm-ghost-row", arch="arm64", size=2048, state="available")

    run_async(manager.reconcile_once())

    rows = storage.pool_vm_list()
    check("a DB row with no backing backend VM is deleted by reconcile_once()",
          "vm-ghost-row" not in {r["vm_id"] for r in rows})


# ── Section 2: backend VM with no DB row -> LEFT ALONE (not the pool's) ────
def test_backend_vm_no_db_row_left_alone():
    """A backend VM with no pool_vms row is NOT a pool orphan — it is almost
    always a live proxy notebook VM (cold-launch fallback / non-pooled launch).
    The lambda-microvms API has no tags and list_microvms returns no marker, so
    the pool CANNOT distinguish its own VMs from the proxy's at the control
    plane; the pool_vms table is the only authority. reconcile_once() must
    therefore NEVER terminate a VM it has no record of — doing so kills a user's
    running session (the reconnect/restore-popup bug)."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    # Simulate a proxy-owned notebook VM: exists on the backend, no pool_vms row.
    notebook_vm = backend.inject_backend_only_vm(arch="arm64", size_mib=2048, state=RUNNING)

    run_async(manager.reconcile_once())

    backend_vms = {h.vm_id for h in run_async(backend.list_all())}
    check("a backend VM with no pool_vms row is LEFT ALONE (not terminated) — "
          "the pool never reaps VMs it didn't create",
          notebook_vm.vm_id in backend_vms)


# ── Section 3: stale 'granting' row -> terminated+deleted ──────────────────
def test_stale_granting_row_terminated_and_deleted():
    """A row STUCK in 'granting' longer than the grant timeout (its updated_at
    — the moment it entered 'granting' — is old) is a crashed/abandoned grant
    and must be torn down."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=60.0)

    vm = run_async(backend.launch("arm64", 2048, "pool-stale"))
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="granting",
                age_seconds=120, endpoint=vm.endpoint)
    # Force updated_at (not just launched_at) to be old — i.e. the row has been
    # STUCK in 'granting' past the timeout, which is the real stale condition.
    _backdate_updated_at(storage, vm.vm_id, seconds_ago=120)

    run_async(manager.reconcile_once())

    rows = storage.pool_vm_list()
    check("a 'granting' row STUCK past the grant-timeout (old updated_at) is deleted",
          vm.vm_id not in {r["vm_id"] for r in rows})
    backend_vms = run_async(backend.list_all())
    check("the stuck granting row's backend VM is terminated too",
          vm.vm_id not in {h.vm_id for h in backend_vms})


# ── Section 3b: REGRESSION — a warm spare granted after a long idle must NOT
# be torn down mid-grant (old launched_at, FRESH updated_at). This is the bug
# that killed a VM 6s into a slow grant and produced the reconnect popup.
def test_recently_granting_old_vm_not_reaped():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=60.0)

    vm = run_async(backend.launch("arm64", 2048, "pool-warm-then-granted"))
    # launched_at is OLD (the spare sat warm in the pool for 10 minutes) ...
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="granting",
                age_seconds=600, endpoint=vm.endpoint)
    # ... but it only ENTERED 'granting' just now (mk_aged_row's set_state bumped
    # updated_at to now), so the grant is fresh and in-flight. Do NOT backdate
    # updated_at here — that's the whole point.

    run_async(manager.reconcile_once())

    rows = storage.pool_vm_list()
    check("a freshly-granting row whose VM is OLD (long-warm spare) is NOT reaped "
          "— reconcile measures granting-staleness from updated_at, not launched_at",
          vm.vm_id in {r["vm_id"] for r in rows})
    backend_vms = run_async(backend.list_all())
    check("the long-warm spare's backend VM is NOT terminated mid-grant",
          vm.vm_id in {h.vm_id for h in backend_vms})


def test_fresh_granting_row_untouched():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    vm = run_async(backend.launch("arm64", 2048, "pool-fresh-granting"))
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="granting",
               age_seconds=5, endpoint=vm.endpoint)  # well under the timeout

    run_async(manager.reconcile_once())

    rows = storage.pool_vm_list()
    check("a 'granting' row younger than the grant-timeout is left alone",
          vm.vm_id in {r["vm_id"] for r in rows})
    row = next(r for r in rows if r["vm_id"] == vm.vm_id)
    check("the fresh granting row's state is unchanged", row["state"] == "granting")


# ── Section 4: clean/no-op reconcile when DB and backend already agree ────
def test_clean_noop_when_agreeing():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=3, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())  # DB and backend agree perfectly after a normal fill

    rows_before = {r["vm_id"]: r["state"] for r in storage.pool_vm_list()}
    backend_before = {h.vm_id for h in run_async(backend.list_all())}

    run_async(manager.reconcile_once())

    rows_after = {r["vm_id"]: r["state"] for r in storage.pool_vm_list()}
    backend_after = {h.vm_id for h in run_async(backend.list_all())}

    check("reconcile_once() is a no-op on pool_vms rows when DB and backend "
          "already agree", rows_before == rows_after)
    check("reconcile_once() is a no-op on backend VMs when DB and backend "
          "already agree", backend_before == backend_after)


def test_startup_reconcile_is_clean():
    """start() runs reconcile_once() first (design §5.1) — confirm it does
    not disturb a freshly-started, empty, consistent pool."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=2, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    async def scenario():
        await manager.start()
        try:
            rows = storage.pool_vm_list()
            return rows
        finally:
            await manager.stop()

    rows = run_async(scenario())
    check("start()'s initial reconcile_once()+fill_once() leaves a clean "
          "pool at target (2) with no orphans introduced", len(rows) == 2)
    check("every row from a clean startup is 'available'",
          all(r["state"] == "available" for r in rows))


# ── Section 5: pre-populated-divergent DB converges after ONE reconcile ───
def test_divergent_db_converges_after_one_reconcile():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64", "x86_64"],
                          sizes_by_arch={"arm64": [2048], "x86_64": [4096]})
    manager = build_manager(backend, storage, config)

    # Build a deliberately divergent starting state:
    #  - 2 ghost DB rows (no backend VM)        -> deleted
    #  - 2 non-pool backend VMs (no DB row)      -> LEFT ALONE (proxy notebook VMs)
    #  - 1 stale granting row past the timeout   -> torn down
    #  - 1 consistent pair (DB row + matching backend VM, 'available') -> survives
    mk_aged_row(storage, "ghost-1", arch="arm64", size=2048, state="available")
    mk_aged_row(storage, "ghost-2", arch="x86_64", size=4096, state="launching")

    # These stand in for proxy-owned notebook VMs: on the backend, no pool_vms row.
    notebook1 = backend.inject_backend_only_vm(arch="arm64", size_mib=2048, vm_id="notebook-be-1")
    notebook2 = backend.inject_backend_only_vm(arch="x86_64", size_mib=4096, vm_id="notebook-be-2")

    stale_vm = run_async(backend.launch("arm64", 2048, "pool-stale-div"))
    mk_aged_row(storage, stale_vm.vm_id, arch="arm64", size=2048, state="granting",
               age_seconds=500, endpoint=stale_vm.endpoint)
    # STUCK granting: force updated_at old too (the real stale condition — a
    # grant that entered 'granting' long ago and never completed).
    _backdate_updated_at(storage, stale_vm.vm_id, seconds_ago=500)

    consistent_vm = run_async(backend.launch("arm64", 2048, "pool-consistent"))
    mk_aged_row(storage, consistent_vm.vm_id, arch="arm64", size=2048, state="available",
               endpoint=consistent_vm.endpoint)

    manager.grant_timeout_seconds = 120.0  # stale_vm's 500s age exceeds this

    run_async(manager.reconcile_once())

    rows_after = {r["vm_id"] for r in storage.pool_vm_list()}
    backend_after = {h.vm_id for h in run_async(backend.list_all())}

    check("divergence converges: both ghost DB rows are gone after ONE reconcile",
          "ghost-1" not in rows_after and "ghost-2" not in rows_after)
    check("divergence converges: non-pool backend VMs (no DB row) are LEFT ALONE "
          "— reconcile never reaps VMs the pool didn't create",
          notebook1.vm_id in backend_after and notebook2.vm_id in backend_after)
    check("divergence converges: the stale granting row is torn down after ONE reconcile",
          stale_vm.vm_id not in rows_after and stale_vm.vm_id not in backend_after)
    check("divergence converges: the one CONSISTENT pair survives untouched",
          consistent_vm.vm_id in rows_after and consistent_vm.vm_id in backend_after)

    # A second reconcile on the now-converged state must be a true no-op.
    rows_before_second = {r["vm_id"]: r["state"] for r in storage.pool_vm_list()}
    run_async(manager.reconcile_once())
    rows_after_second = {r["vm_id"]: r["state"] for r in storage.pool_vm_list()}
    check("a second reconcile_once() on the now-converged state changes nothing",
          rows_before_second == rows_after_second)


# ── Section 6: start()'s locked reconcile cleans stale rows, keeps healthy ─
def test_start_cleans_stale_rows_via_reconcile():
    """FEAT-001: a starting instance ADOPTS a shared pool — its locked startup
    reconcile_once() is the first-line janitor (cold_start_cleanup no longer
    exists). A stale ghost row and a stale 'granting' row are cleaned; a
    healthy consistent row (DB row + matching backend VM) survives."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    # Stale ghost: DB row with no backing backend VM.
    mk_aged_row(storage, "stale-ghost", arch="arm64", size=2048, state="available")

    # Stale granting: a row STUCK in 'granting' past the grant-timeout. The
    # stale condition is an old UPDATED_AT (when it entered 'granting'), not an
    # old launched_at — mk_aged_row only backdates launched_at and set_state
    # bumps updated_at to NOW, so force updated_at old too (mirrors
    # test_stale_granting_row_terminated_and_deleted).
    stale_vm = run_async(backend.launch("arm64", 2048, "pool-stale-granting"))
    mk_aged_row(storage, stale_vm.vm_id, arch="arm64", size=2048, state="granting",
                age_seconds=500, endpoint=stale_vm.endpoint)
    _backdate_updated_at(storage, stale_vm.vm_id, seconds_ago=500)

    # Healthy consistent pair: DB row + matching backend VM, must survive.
    healthy_vm = run_async(backend.launch("arm64", 2048, "pool-healthy"))
    mk_aged_row(storage, healthy_vm.vm_id, arch="arm64", size=2048, state="available",
                endpoint=healthy_vm.endpoint)

    async def scenario():
        await manager.start()
        try:
            return storage.pool_vm_list()
        finally:
            await manager.stop()

    rows_after = {r["vm_id"] for r in run_async(scenario())}
    check("start()'s startup reconcile deletes the stale ghost row",
          "stale-ghost" not in rows_after)
    check("start()'s startup reconcile tears down the stale 'granting' row",
          stale_vm.vm_id not in rows_after)
    check("start()'s startup reconcile leaves the healthy consistent row untouched",
          healthy_vm.vm_id in rows_after)


# ── Section 7: orphaned-'granted' reaper ───────────────────────────────────
# A pool_vms row stuck in 'granted' after its session rotated onto a different
# VM (or ended) is otherwise cleaned up only by the T10 "backend VM gone" loop
# or a manual drain, so the AWS VM bills SUSPENDED until the 8h max-lifetime and
# /status counts it above target. reconcile_once() reaps such rows — but ONLY
# when the owning session is no longer on this VM. All state is seeded THROUGH
# the storage interface (never raw SQL) to prove the interface-only constraint.

def _seed_granted_row(storage, backend, vm_id_session, age_updated_at=None):
    """Launch a backend VM, create its pool_vms row and flip it to 'granted'
    owned by `session_id`, all via the storage interface. Returns the VmHandle.
    If age_updated_at is given, backdate updated_at that many seconds into the
    past so the row is past the grant grace window."""
    vm, session_id = vm_id_session
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="available",
                endpoint=vm.endpoint)
    storage.pool_vm_set_state(vm.vm_id, "granted", real_session_id=session_id)
    if age_updated_at is not None:
        _backdate_updated_at(storage, vm.vm_id, seconds_ago=age_updated_at)
    return vm


def test_granted_rotated_away_reaped():
    """ROTATED-AWAY: the granted row's session now points at a DIFFERENT, live
    VM (registry + vm_sessions). The granted row's VM is reclaimed."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    session_id = "sess-rotated"
    # VM A: the stale granted spare the session has moved off of.
    vm_a = run_async(backend.launch("arm64", 2048, "pool-a"))
    _seed_granted_row(storage, backend, (vm_a, session_id), age_updated_at=300)
    # VM B: where the session actually is now (live, pointed at by registry).
    vm_b = run_async(backend.launch("arm64", 2048, "pool-b"))
    storage.session_registry_upsert(session_id, vm_b.vm_id, vm_b.endpoint)
    storage.vm_session_create(microvm_id=vm_b.vm_id, session_id=session_id,
                              architecture="arm64", memory_mib=2048)

    run_async(manager.reconcile_once())

    rows = {r["vm_id"] for r in storage.pool_vm_list()}
    backend_vms = {h.vm_id for h in run_async(backend.list_all())}
    check("ROTATED-AWAY: the orphaned granted row (session moved to another VM) "
          "is deleted", vm_a.vm_id not in rows)
    check("ROTATED-AWAY: the orphaned granted row's backend VM is terminated",
          vm_a.vm_id not in backend_vms)
    check("ROTATED-AWAY: the VM the session is actually on is untouched",
          vm_b.vm_id in backend_vms)


def test_granted_session_gone_reaped():
    """SESSION-GONE: no registry entry AND no non-terminated vm_sessions row for
    the owning session. The granted row is reclaimed."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    session_id = "sess-gone"
    vm_a = run_async(backend.launch("arm64", 2048, "pool-gone"))
    _seed_granted_row(storage, backend, (vm_a, session_id), age_updated_at=300)
    # The session once had a VM session row on vm_a, but it is now TERMINATED,
    # and there is no session_registry entry → the session has ended.
    storage.vm_session_create(microvm_id=vm_a.vm_id, session_id=session_id,
                              architecture="arm64", memory_mib=2048)
    storage.vm_session_update_state(vm_a.vm_id, "TERMINATED")

    run_async(manager.reconcile_once())

    rows = {r["vm_id"] for r in storage.pool_vm_list()}
    backend_vms = {h.vm_id for h in run_async(backend.list_all())}
    check("SESSION-GONE: the orphaned granted row (session ended, no live VM) "
          "is deleted", vm_a.vm_id not in rows)
    check("SESSION-GONE: the orphaned granted row's backend VM is terminated",
          vm_a.vm_id not in backend_vms)


def test_granted_still_on_it_not_reaped():
    """STILL-ON-IT (critical safety invariant): the session's CURRENT VM id is
    the granted row's own vm_id. The row is LEFT ALONE regardless of age —
    reconcile must NEVER reclaim a VM a session is actively using."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    session_id = "sess-active"
    vm_a = run_async(backend.launch("arm64", 2048, "pool-active"))
    # Backdate FAR past the grace window — age alone must not trigger a reap.
    _seed_granted_row(storage, backend, (vm_a, session_id), age_updated_at=100000)
    # The session is CURRENTLY on vm_a (registry + a live vm_sessions row).
    storage.session_registry_upsert(session_id, vm_a.vm_id, vm_a.endpoint)
    storage.vm_session_create(microvm_id=vm_a.vm_id, session_id=session_id,
                              architecture="arm64", memory_mib=2048)

    run_async(manager.reconcile_once())

    rows = storage.pool_vm_list()
    backend_vms = {h.vm_id for h in run_async(backend.list_all())}
    check("STILL-ON-IT: a granted row whose session is actively on this VM is "
          "NOT reaped, regardless of age (critical safety invariant)",
          vm_a.vm_id in {r["vm_id"] for r in rows})
    row = next((r for r in rows if r["vm_id"] == vm_a.vm_id), None)
    check("STILL-ON-IT: the surviving row stays in state 'granted'",
          row is not None and row["state"] == "granted")
    check("STILL-ON-IT: the actively-used backend VM is NOT terminated",
          vm_a.vm_id in backend_vms)


def test_granted_within_grace_window_not_reaped():
    """GRACE WINDOW: the mismatch signal WOULD fire (session rotated away), but
    the row's updated_at is still inside the grant grace window. It is NOT reaped
    — a just-granted row still being adopted (registry not yet written) must be
    protected mid-adoption."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    session_id = "sess-fresh-rotated"
    vm_a = run_async(backend.launch("arm64", 2048, "pool-fresh-a"))
    # Do NOT backdate updated_at — the row just entered 'granted'.
    _seed_granted_row(storage, backend, (vm_a, session_id), age_updated_at=None)
    # Session points at a different live VM, so the mismatch signal is satisfied.
    vm_b = run_async(backend.launch("arm64", 2048, "pool-fresh-b"))
    storage.session_registry_upsert(session_id, vm_b.vm_id, vm_b.endpoint)

    run_async(manager.reconcile_once())

    rows = {r["vm_id"] for r in storage.pool_vm_list()}
    backend_vms = {h.vm_id for h in run_async(backend.list_all())}
    check("GRACE WINDOW: a mismatched granted row NEWER than the grace window is "
          "NOT reaped (protected mid-adoption)", vm_a.vm_id in rows)
    check("GRACE WINDOW: its backend VM is NOT terminated",
          vm_a.vm_id in backend_vms)


def test_granted_null_session_id_skipped():
    """NULL real_session_id: a 'granted' row with no owning session is SKIPPED —
    the reaper must not throw and must not reap it (defensive)."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config, grant_timeout_seconds=120.0)

    vm_a = run_async(backend.launch("arm64", 2048, "pool-null"))
    mk_aged_row(storage, vm_a.vm_id, arch="arm64", size=2048, state="available",
                endpoint=vm_a.endpoint)
    # Flip to 'granted' with real_session_id explicitly cleared to NULL.
    storage.pool_vm_set_state(vm_a.vm_id, "granted", real_session_id=None)
    _backdate_updated_at(storage, vm_a.vm_id, seconds_ago=300)

    raised = False
    try:
        run_async(manager.reconcile_once())
    except Exception:
        raised = True

    rows = {r["vm_id"] for r in storage.pool_vm_list()}
    backend_vms = {h.vm_id for h in run_async(backend.list_all())}
    check("NULL real_session_id: reconcile_once() does not raise", not raised)
    check("NULL real_session_id: the granted row with no session is LEFT ALONE",
          vm_a.vm_id in rows)
    check("NULL real_session_id: its backend VM is NOT terminated",
          vm_a.vm_id in backend_vms)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager ORPHAN RECOVERY ===")
    print("\n-- DB row, no backend VM -> deleted --")
    test_db_row_no_backend_vm_deleted()
    print("\n-- backend VM, no DB row -> LEFT ALONE (not the pool's) --")
    test_backend_vm_no_db_row_left_alone()
    print("\n-- stale 'granting' row (old updated_at) -> terminated+deleted --")
    test_stale_granting_row_terminated_and_deleted()
    print("\n-- REGRESSION: long-warm spare freshly granting (old launched_at, fresh updated_at) NOT reaped --")
    test_recently_granting_old_vm_not_reaped()
    print("\n-- fresh 'granting' row untouched --")
    test_fresh_granting_row_untouched()
    print("\n-- clean/no-op reconcile when DB and backend agree --")
    test_clean_noop_when_agreeing()
    print("\n-- startup reconcile is clean --")
    test_startup_reconcile_is_clean()
    print("\n-- pre-populated-divergent DB converges after ONE reconcile --")
    test_divergent_db_converges_after_one_reconcile()
    print("\n-- start()'s locked reconcile cleans stale rows, keeps healthy --")
    test_start_cleans_stale_rows_via_reconcile()

    print("\n-- orphaned 'granted' reaper: ROTATED-AWAY -> reaped --")
    test_granted_rotated_away_reaped()
    print("\n-- orphaned 'granted' reaper: SESSION-GONE -> reaped --")
    test_granted_session_gone_reaped()
    print("\n-- orphaned 'granted' reaper: STILL-ON-IT (safety invariant) -> NOT reaped --")
    test_granted_still_on_it_not_reaped()
    print("\n-- orphaned 'granted' reaper: GRACE WINDOW -> NOT reaped --")
    test_granted_within_grace_window_not_reaped()
    print("\n-- orphaned 'granted' reaper: NULL real_session_id -> skipped --")
    test_granted_null_session_id_skipped()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL ORPHAN RECOVERY CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_orphans():
    _checks.clear()
    for fn in (test_db_row_no_backend_vm_deleted, test_backend_vm_no_db_row_left_alone,
               test_stale_granting_row_terminated_and_deleted,
               test_recently_granting_old_vm_not_reaped, test_fresh_granting_row_untouched,
               test_clean_noop_when_agreeing, test_startup_reconcile_is_clean,
               test_divergent_db_converges_after_one_reconcile,
               test_start_cleans_stale_rows_via_reconcile,
               test_granted_rotated_away_reaped, test_granted_session_gone_reaped,
               test_granted_still_on_it_not_reaped,
               test_granted_within_grace_window_not_reaped,
               test_granted_null_session_id_skipped):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool orphan recovery failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
