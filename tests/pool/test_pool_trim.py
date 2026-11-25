#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager OVER-TARGET TRIM test suite (Phase 3 — pool-over-target-trim).

Exercises the over-target trim step of PoolManager.recycle_once() against
FakeVmBackend + a temp SQLite file: when a tier holds more 'available' spares
than its target, the OLDEST excess spares are terminated+deleted while the
youngest `target` survive; tiers at/below target are untouched; 'granted' and
'granting' rows are NEVER trimmed; a target of 0 trims ALL available spares.
Uses backdated launched_at fixtures — no sleeping.

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_trim.py
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


def _terminated_ids(backend):
    """vm_ids the backend's terminate() was called on."""
    return {kw["vm_id"] for name, kw in backend.calls if name == "terminate"}


# ── Section 1: over-target trims the OLDEST excess, youngest survive ───────
def test_over_target_trims_oldest():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    # target 1 for the 2048 tier (override), well under max-age so nothing
    # ages out — only the trim acts.
    config = build_config(size_per_image=1, size_overrides={2048: 1},
                          max_age_hours=1.0, arches=["arm64"],
                          sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    # 3 available spares, distinct ages (older = larger age_seconds).
    oldest = run_async(backend.launch("arm64", 2048, "pool-oldest"))
    mk_aged_row(storage, oldest.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=300, endpoint=oldest.endpoint)
    middle = run_async(backend.launch("arm64", 2048, "pool-middle"))
    mk_aged_row(storage, middle.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=200, endpoint=middle.endpoint)
    youngest = run_async(backend.launch("arm64", 2048, "pool-youngest"))
    mk_aged_row(storage, youngest.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=100, endpoint=youngest.endpoint)

    run_async(manager.recycle_once())

    terminated = _terminated_ids(backend)
    check("the two OLDEST over-target spares are terminated",
          oldest.vm_id in terminated and middle.vm_id in terminated)
    check("the YOUNGEST spare (within target) is NOT terminated",
          youngest.vm_id not in terminated)

    remaining = {r["vm_id"] for r in storage.pool_vm_list()}
    check("the two oldest rows are deleted from pool_vms",
          oldest.vm_id not in remaining and middle.vm_id not in remaining)
    check("the youngest row survives in pool_vms", youngest.vm_id in remaining)
    check("exactly `excess`=2 spares terminated", len(terminated) == 2)


# ── Section 2: tiers at or below target are not trimmed ───────────────────
def test_at_or_below_target_no_trim():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    # 2048 tier at target (2 available), 4096 tier below target (1 available).
    # Nothing ages out, so recycle's backfill never runs (no fill top-up) —
    # the injected rows are the whole story.
    config = build_config(size_per_image=2, max_age_hours=1.0, arches=["arm64"],
                          sizes_by_arch={"arm64": [2048, 4096]})
    manager = build_manager(backend, storage, config)

    at_a = run_async(backend.launch("arm64", 2048, "pool-at-a"))
    mk_aged_row(storage, at_a.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=120, endpoint=at_a.endpoint)
    at_b = run_async(backend.launch("arm64", 2048, "pool-at-b"))
    mk_aged_row(storage, at_b.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=60, endpoint=at_b.endpoint)
    below = run_async(backend.launch("arm64", 4096, "pool-below"))
    mk_aged_row(storage, below.vm_id, arch="arm64", size=4096, state="available",
               age_seconds=60, endpoint=below.endpoint)

    run_async(manager.recycle_once())

    terminated = _terminated_ids(backend)
    check("nothing is terminated when every tier is at or below target",
          len(terminated) == 0)
    remaining = {r["vm_id"] for r in storage.pool_vm_list()}
    check("the at-target tier's spares both survive",
          at_a.vm_id in remaining and at_b.vm_id in remaining)
    check("the below-target tier's spare survives", below.vm_id in remaining)


# ── Section 3: 'granted'/'granting' are never trimmed ──────────────────────
def test_granted_granting_never_trimmed():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    # target 1; 2 available (over by 1) PLUS a granted and a granting row.
    config = build_config(size_per_image=1, size_overrides={2048: 1},
                          max_age_hours=1.0, arches=["arm64"],
                          sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    older_avail = run_async(backend.launch("arm64", 2048, "pool-avail-old"))
    mk_aged_row(storage, older_avail.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=300, endpoint=older_avail.endpoint)
    younger_avail = run_async(backend.launch("arm64", 2048, "pool-avail-young"))
    mk_aged_row(storage, younger_avail.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=100, endpoint=younger_avail.endpoint)
    granted = run_async(backend.launch("arm64", 2048, "pool-granted"))
    mk_aged_row(storage, granted.vm_id, arch="arm64", size=2048, state="granted",
               age_seconds=400, endpoint=granted.endpoint)
    granting = run_async(backend.launch("arm64", 2048, "pool-granting"))
    mk_aged_row(storage, granting.vm_id, arch="arm64", size=2048, state="granting",
               age_seconds=400, endpoint=granting.endpoint)

    run_async(manager.recycle_once())

    terminated = _terminated_ids(backend)
    check("the granted row is NEVER terminated", granted.vm_id not in terminated)
    check("the granting row is NEVER terminated", granting.vm_id not in terminated)
    check("only the single over-target available spare (oldest) is terminated",
          terminated == {older_avail.vm_id})

    remaining = {r["vm_id"] for r in storage.pool_vm_list()}
    check("the granted row is still present", granted.vm_id in remaining)
    check("the granting row is still present", granting.vm_id in remaining)
    check("the younger available spare survives (within target)",
          younger_avail.vm_id in remaining)
    check("the older available spare was deleted", older_avail.vm_id not in remaining)


# ── Section 4: target == 0 trims ALL available spares ─────────────────────
def test_target_zero_trims_all():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    # target 0 for the 2048 tier -> no pool for this tier -> trim everything.
    config = build_config(size_per_image=0, size_overrides={2048: 0},
                          max_age_hours=1.0, arches=["arm64"],
                          sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    a = run_async(backend.launch("arm64", 2048, "pool-a"))
    mk_aged_row(storage, a.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=200, endpoint=a.endpoint)
    b = run_async(backend.launch("arm64", 2048, "pool-b"))
    mk_aged_row(storage, b.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=100, endpoint=b.endpoint)

    run_async(manager.recycle_once())

    terminated = _terminated_ids(backend)
    check("both available spares are terminated when target is 0",
          a.vm_id in terminated and b.vm_id in terminated)
    check("excess == len(available): exactly 2 terminated", len(terminated) == 2)

    remaining = {r["vm_id"] for r in storage.pool_vm_list()}
    check("both rows are deleted from pool_vms",
          a.vm_id not in remaining and b.vm_id not in remaining)


# ── Section 5: mixed arches — x86_64 target-0 tier trimmed, others kept ────
def test_mixed_arches_trim():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(
        size_per_image=2, max_age_hours=1.0,
        arches=["arm64", "x86_64"],
        sizes_by_arch={"arm64": [2048], "x86_64": [1024, 2048]},
        size_overrides={2048: 2},
        size_overrides_by_arch={"x86_64": {1024: 0, 2048: 1}},
    )
    manager = build_manager(backend, storage, config)

    x86_small = run_async(backend.launch("x86_64", 1024, "pool-x86-1024"))
    mk_aged_row(storage, x86_small.vm_id, arch="x86_64", size=1024, state="available",
               age_seconds=100, endpoint=x86_small.endpoint)
    x86_main = run_async(backend.launch("x86_64", 2048, "pool-x86-2048"))
    mk_aged_row(storage, x86_main.vm_id, arch="x86_64", size=2048, state="available",
               age_seconds=100, endpoint=x86_main.endpoint)
    arm = run_async(backend.launch("arm64", 2048, "pool-arm-2048"))
    mk_aged_row(storage, arm.vm_id, arch="arm64", size=2048, state="available",
               age_seconds=100, endpoint=arm.endpoint)

    run_async(manager.recycle_once())

    terminated = _terminated_ids(backend)
    check("mixed arches: the x86_64-1024 spare (target 0) is trimmed",
          terminated == {x86_small.vm_id})
    remaining = {r["vm_id"] for r in storage.pool_vm_list()}
    check("mixed arches: the x86_64-2048 spare (target 1) is kept", x86_main.vm_id in remaining)
    check("mixed arches: the arm64 spare is untouched", arm.vm_id in remaining)
    check("mixed arches: the trimmed x86_64-1024 row is deleted", x86_small.vm_id not in remaining)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager OVER-TARGET TRIM ===")
    print("\n-- over-target trims the oldest excess, youngest survive --")
    test_over_target_trims_oldest()
    print("\n-- tiers at or below target are not trimmed --")
    test_at_or_below_target_no_trim()
    print("\n-- 'granted'/'granting' never trimmed --")
    test_granted_granting_never_trimmed()
    print("\n-- target == 0 trims all available spares --")
    test_target_zero_trims_all()
    print("\n-- mixed arches: x86_64 target-0 tier trimmed --")
    test_mixed_arches_trim()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL TRIM CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_trim():
    _checks.clear()
    for fn in (test_over_target_trims_oldest, test_at_or_below_target_no_trim,
               test_granted_granting_never_trimmed, test_target_zero_trims_all,
               test_mixed_arches_trim):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool trim failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
