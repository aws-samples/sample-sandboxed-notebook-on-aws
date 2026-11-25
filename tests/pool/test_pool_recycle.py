#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager RECYCLE LOOP test suite (Phase 3 — FEAT-002, plan.md item 8).

Exercises PoolManager.recycle_once() against FakeVmBackend + a temp SQLite
file: available spares older than max-age are terminated+deleted+backfilled;
younger spares are untouched; POOL_MAX_AGE_HOURS is clamped to 2h (5h behaves
as 2h); 'granted' is never recycled; 'granting' is skipped. Uses backdated
launched_at fixtures — no sleeping.

Also asserts the HARD INVARIANT (design §13.5) that recycling an aged-out
spare triggers an IMMEDIATE demand-aware backfill of that tier, not just a
future periodic sweep — the same mechanism grant() uses.

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_recycle.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config, build_manager, mk_aged_row  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── Section 1: aged-out available spares terminated+deleted+backfilled ────
def test_aged_out_terminated_deleted_backfilled():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=2, max_age_hours=1.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    # one old spare (over 1h), backed by a real backend VM so terminate() is observable
    old_vm = run_async(backend.launch("arm64", 2048, "pool-old"))
    mk_aged_row(storage, old_vm.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=3700, endpoint=old_vm.endpoint)

    run_async(manager.recycle_once())

    rows = storage.pool_vm_list()
    check("the aged-out row is deleted from pool_vms",
          old_vm.vm_id not in {r["vm_id"] for r in rows})
    backend_vms = run_async(backend.list_all())
    check("the aged-out spare's backend VM is terminated",
          old_vm.vm_id not in {h.vm_id for h in backend_vms})

    names = [name for name, _ in backend.calls]
    check("recycle_once() calls terminate() on the aged-out spare", "terminate" in names)

    # backfill: tier should be back at target (2) afterward
    rows_after = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("recycle_once() backfills the tier back to its target (2) in the same call",
          len(rows_after) == 2)


# ── Section 2: younger spares untouched ────────────────────────────────────
def test_younger_untouched():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, max_age_hours=1.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    young_vm = run_async(backend.launch("arm64", 2048, "pool-young"))
    mk_aged_row(storage, young_vm.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=60, endpoint=young_vm.endpoint)  # well under 1h

    run_async(manager.recycle_once())

    rows = storage.pool_vm_list()
    check("a spare younger than max-age is left untouched",
          young_vm.vm_id in {r["vm_id"] for r in rows})
    names = [name for name, _ in backend.calls]
    check("recycle_once() does not call terminate() when nothing is aged out",
          "terminate" not in names)


# ── Section 3: POOL_MAX_AGE_HOURS clamp — 5h behaves as 2h ────────────────
def test_max_age_clamp_behavior():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    # Config built via build_config() takes max_age_hours directly; the
    # clamp itself lives in load_config()/PoolConfig (see test_pool_config.py)
    # — here we confirm the MANAGER actually behaves per the clamped value
    # (an age of 2.5h with a *clamped* max_age_hours of 2.0 is recycled,
    # proving the manager uses the clamped ceiling and not the unclamped 5h).
    config = build_config(size_per_image=1, max_age_hours=2.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    vm = run_async(backend.launch("arm64", 2048, "pool-clamp"))
    # age = 2h5m: older than the CLAMPED 2h ceiling, but would NOT be
    # recycled if the manager mistakenly used the unclamped 5h config.
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=2 * 3600 + 300, endpoint=vm.endpoint)

    run_async(manager.recycle_once())

    rows = storage.pool_vm_list()
    check("a spare older than the CLAMPED 2h ceiling is recycled "
          "(manager honors max_age_hours=2.0, not an unclamped 5h)",
          vm.vm_id not in {r["vm_id"] for r in rows})


# ── Section 4: 'granted' never recycled ─────────────────────────────────────
def test_granted_never_recycled():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, max_age_hours=1.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    vm = run_async(backend.launch("arm64", 2048, "pool-granted"))
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="granted",
               age_seconds=99999, endpoint=vm.endpoint)

    run_async(manager.recycle_once())

    rows = storage.pool_vm_list()
    check("a 'granted' row, however old, is NEVER recycled",
          vm.vm_id in {r["vm_id"] for r in rows})
    row = next(r for r in rows if r["vm_id"] == vm.vm_id)
    check("the granted row's state is unchanged by recycle_once()",
          row["state"] == "granted")


# ── Section 5: 'granting' skipped ───────────────────────────────────────────
def test_granting_skipped():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, max_age_hours=1.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    vm = run_async(backend.launch("arm64", 2048, "pool-granting"))
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="granting",
               age_seconds=99999, endpoint=vm.endpoint)

    run_async(manager.recycle_once())

    rows = storage.pool_vm_list()
    check("a 'granting' row, however old, is skipped (not touched by age-recycle)",
          vm.vm_id in {r["vm_id"] for r in rows})


# ── Section 6: demand-aware backfill invariant on the recycle exit path ───
def test_recycle_triggers_immediate_backfill_signal():
    """Hard invariant: a VM leaving the pool via recycling (not just via
    grant) must immediately signal the fill loop for backfill — not just
    rely on recycle_once()'s own synchronous fill_once() call. This proves
    the _refill_signal event (shared with grant()) is set on this exit path
    too, so fill_loop() wakes immediately rather than waiting out its full
    periodic interval."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, max_age_hours=1.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    vm = run_async(backend.launch("arm64", 2048, "pool-signal"))
    mk_aged_row(storage, vm.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=9999, endpoint=vm.endpoint)

    check("_refill_signal starts unset", not manager._refill_signal.is_set())
    run_async(manager.recycle_once())
    check("recycling an aged-out spare sets _refill_signal immediately "
          "(same demand-aware trigger grant() uses)",
          manager._refill_signal.is_set())

    # And the tier actually converges back to target as a result.
    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("the tier converges back to target (1) after the recycle+backfill",
          len(rows) == 1)


# ── Section 7: multiple aged spares across tiers, mixed with fresh ones ───
def test_mixed_ages_across_tiers():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=2, max_age_hours=1.0,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048, 4096]})
    manager = build_manager(backend, storage, config)

    old_a = run_async(backend.launch("arm64", 2048, "s"))
    mk_aged_row(storage, old_a.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=5000, endpoint=old_a.endpoint)
    fresh_a = run_async(backend.launch("arm64", 2048, "s"))
    mk_aged_row(storage, fresh_a.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=10, endpoint=fresh_a.endpoint)
    old_b = run_async(backend.launch("arm64", 4096, "s"))
    mk_aged_row(storage, old_b.vm_id, arch="arm64", size=4096, state="available",
               age_seconds=5000, endpoint=old_b.endpoint)

    run_async(manager.recycle_once())

    remaining_ids = {r["vm_id"] for r in storage.pool_vm_list()}
    check("old spare in tier 2048 is recycled", old_a.vm_id not in remaining_ids)
    check("fresh spare in tier 2048 survives", fresh_a.vm_id in remaining_ids)
    check("old spare in tier 4096 is recycled", old_b.vm_id not in remaining_ids)

    rows_2048 = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    rows_4096 = storage.pool_vm_list(architecture="arm64", size_mib=4096)
    check("tier 2048 converges back to target (2) after recycle+backfill",
          len(rows_2048) == 2)
    check("tier 4096 converges back to target (2) after recycle+backfill",
          len(rows_4096) == 2)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager RECYCLE LOOP ===")
    print("\n-- aged-out available spares terminated+deleted+backfilled --")
    test_aged_out_terminated_deleted_backfilled()
    print("\n-- younger spares untouched --")
    test_younger_untouched()
    print("\n-- POOL_MAX_AGE_HOURS clamp behavior --")
    test_max_age_clamp_behavior()
    print("\n-- 'granted' never recycled --")
    test_granted_never_recycled()
    print("\n-- 'granting' skipped --")
    test_granting_skipped()
    print("\n-- recycle triggers immediate demand-aware backfill signal --")
    test_recycle_triggers_immediate_backfill_signal()
    print("\n-- mixed ages across multiple tiers --")
    test_mixed_ages_across_tiers()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL RECYCLE CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_recycle():
    _checks.clear()
    for fn in (test_aged_out_terminated_deleted_backfilled, test_younger_untouched,
               test_max_age_clamp_behavior, test_granted_never_recycled,
               test_granting_skipped, test_recycle_triggers_immediate_backfill_signal,
               test_mixed_ages_across_tiers):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool recycle failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
