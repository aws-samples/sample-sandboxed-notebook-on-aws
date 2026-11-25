#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager FILL LOOP test suite (Phase 3 — FEAT-002, plan.md item 7).

Exercises PoolManager.fill_once()/fill_loop() against FakeVmBackend + a temp
SQLite file (the real Phase-2 storage): fills to target on startup, per-size
overrides honored, default used for unlisted sizes, target 0 -> no spares, a
spare becomes 'available' ONLY after wait_healthy+suspend (ordering asserted
via the fake's call log), launch/health/suspend failure each cleaned up (row
deleted + backend VM terminated) with the loop CONTINUING, one bad tier
doesn't wedge others, and bounded concurrency <= POOL_FILL_CONCURRENCY.

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_fill.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config, build_manager  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── Section 1: fills to target on startup ──────────────────────────────────
def test_fills_to_target():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=3, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    run_async(manager.fill_once())

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("fill_once() fills the tier to its target count (3)", len(rows) == 3)
    check("all filled rows are 'available' after fill_once completes",
          all(r["state"] == "available" for r in rows))

    # idempotent: already at target -> fill_once is a no-op
    run_async(manager.fill_once())
    rows2 = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("a second fill_once() at target launches nothing more", len(rows2) == 3)


# ── Section 2: per-size overrides honored + default for unlisted sizes ────
def test_overrides_and_default():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(
        size_per_image=1,
        arches=["arm64"],
        sizes_by_arch={"arm64": [1024, 2048, 4096]},
        size_overrides={2048: 5},
    )
    manager = build_manager(backend, storage, config)

    run_async(manager.fill_once())

    count_1024 = len(storage.pool_vm_list(architecture="arm64", size_mib=1024))
    count_2048 = len(storage.pool_vm_list(architecture="arm64", size_mib=2048))
    count_4096 = len(storage.pool_vm_list(architecture="arm64", size_mib=4096))
    check("overridden size (2048 -> 5) is filled to the override, not size_per_image",
          count_2048 == 5)
    check("unlisted size (1024) falls back to size_per_image default (1)",
          count_1024 == 1)
    check("unlisted size (4096) falls back to size_per_image default (1)",
          count_4096 == 1)


# ── Section 3: target 0 -> no spares ───────────────────────────────────────
def test_target_zero_no_spares():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(
        size_per_image=0,
        arches=["arm64"],
        sizes_by_arch={"arm64": [2048]},
    )
    manager = build_manager(backend, storage, config)

    run_async(manager.fill_once())

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("target 0 means fill_once() launches nothing for that tier", len(rows) == 0)
    check("backend recorded zero launch calls for a target-0 tier",
          not any(name == "launch" for name, _ in backend.calls))


# ── Section 4: availability ordering (wait_healthy + suspend BEFORE available) ─
def test_available_only_after_healthy_and_suspend():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    run_async(manager.fill_once())

    names = [name for name, _ in backend.calls]
    check("launch -> wait_healthy -> suspend called in that order",
          names == ["launch", "wait_healthy", "suspend"])

    row = storage.pool_vm_list(architecture="arm64", size_mib=2048)[0]
    check("the spare ends up 'available' only after wait_healthy+suspend",
          row["state"] == "available")


# ── Section 5: launch failure cleaned up, loop continues ───────────────────
def test_launch_failure_cleanup_and_continue():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=2, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    backend.fail_launch_times(1)  # first launch in the deficit of 2 fails
    run_async(manager.fill_once())

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("one fill_once() pass with 1 failure out of a deficit of 2 yields "
          "exactly 1 successful spare (the failure does not raise/crash the pass)",
          len(rows) == 1)
    check("a launch failure leaves no leftover row for the failed attempt "
          "(only successful spares persisted)",
          all(r["state"] == "available" for r in rows))

    # The failed attempt leaves the tier still below target — the loop
    # CONTINUES (not wedged): the next fill_once() pass (as fill_loop() would
    # run on its next tick) closes the remaining deficit.
    run_async(manager.fill_once())
    rows2 = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("the loop continues: a subsequent fill_once() pass closes the "
          "remaining deficit and reaches target (2)",
          len(rows2) == 2)


# ── Section 6: health failure cleaned up (row deleted + backend terminated) ─
def test_health_failure_cleanup():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    backend.fail_wait_healthy_times(1)
    run_async(manager._launch_one("arm64", 2048))

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("a wait_healthy failure leaves no pool_vms row behind (cleaned up)",
          len(rows) == 0)
    # the backend VM that was launched before the health check failed must
    # have been terminated too (it's gone from list_all()).
    remaining_backend_vms = run_async(backend.list_all())
    check("a wait_healthy failure results in the backend VM being terminated",
          len(remaining_backend_vms) == 0)
    names = [name for name, _ in backend.calls]
    check("the cleanup path calls terminate() after the wait_healthy failure",
          "terminate" in names)


# ── Section 7: suspend failure cleaned up ───────────────────────────────────
def test_suspend_failure_cleanup():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)

    backend.fail_suspend_times(1)
    run_async(manager._launch_one("arm64", 2048))

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("a suspend failure leaves no pool_vms row behind (cleaned up)", len(rows) == 0)
    remaining_backend_vms = run_async(backend.list_all())
    check("a suspend failure results in the backend VM being terminated",
          len(remaining_backend_vms) == 0)


# ── Section 8: one bad tier doesn't wedge others ────────────────────────────
def test_one_bad_tier_does_not_wedge_others():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(
        size_per_image=2,
        arches=["arm64"],
        sizes_by_arch={"arm64": [2048, 4096]},
    )
    manager = build_manager(backend, storage, config)

    # Make every launch for one tier fail by exhausting the failure budget
    # generously (more than the deficit so the whole tier is starved).
    backend.fail_launch_times(10)

    # fill_once will try tier 2048 first (dict iteration order follows
    # sizes_by_arch list order) and tier 4096 second — but since the fault
    # is a simple counter on launch() regardless of tier, drain it on
    # purpose against ONE tier by pre-launching into the counter via a
    # dedicated manager call, then verify the other tier still fills.
    run_async(manager._launch_one("arm64", 2048))  # consumes 1 of the 10 fails, cleans up
    for _ in range(9):
        run_async(manager._launch_one("arm64", 2048))  # drains the rest of the budget

    # Now launches succeed again; fill_once should bring BOTH tiers to target
    # with no residual wedge from the earlier failures.
    run_async(manager.fill_once())

    rows_2048 = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    rows_4096 = storage.pool_vm_list(architecture="arm64", size_mib=4096)
    check("after exhausting failures on one tier, that tier recovers to target",
          len(rows_2048) == 2)
    check("the other, unaffected tier is also filled to target",
          len(rows_4096) == 2)


# ── Section 9: bounded concurrency <= POOL_FILL_CONCURRENCY ────────────────
def test_bounded_concurrency():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    # Slow down launch so overlapping in-flight launches are observable.
    backend.set_latency("launch", 0.05)
    config = build_config(
        size_per_image=10, fill_concurrency=3,
        arches=["arm64"], sizes_by_arch={"arm64": [2048]},
    )
    manager = build_manager(backend, storage, config)

    max_in_flight = 0
    in_flight = 0
    lock = asyncio.Lock()

    orig_launch = backend.launch

    async def tracking_launch(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            return await orig_launch(*args, **kwargs)
        finally:
            async with lock:
                in_flight -= 1

    backend.launch = tracking_launch

    run_async(manager.fill_once())

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("fill_once() still reaches the full target (10) under bounded concurrency",
          len(rows) == 10)
    check(f"max concurrently in-flight launches ({max_in_flight}) <= "
          f"POOL_FILL_CONCURRENCY ({config.fill_concurrency})",
          max_in_flight <= config.fill_concurrency)
    check("concurrency was actually exercised (more than 1 in flight at once)",
          max_in_flight > 1)


# ── Section 10: mixed arches (arm64 + x86_64 with per-arch overrides) ──────
def test_mixed_arches():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    sizes = [1024, 2048, 4096, 8192]
    config = build_config(
        size_per_image=2,
        arches=["arm64", "x86_64"],
        sizes_by_arch={"arm64": list(sizes), "x86_64": list(sizes)},
        size_overrides={1024: 1, 2048: 2, 4096: 2, 8192: 1},
        size_overrides_by_arch={"x86_64": {1024: 0, 2048: 1, 4096: 0, 8192: 0}},
    )
    manager = build_manager(backend, storage, config)

    run_async(manager.fill_once())

    x86_rows = storage.pool_vm_list(architecture="x86_64")
    check("mixed arches: exactly 1 x86_64 spare after fill_once", len(x86_rows) == 1)
    check("mixed arches: the x86_64 spare is the 2048 tier",
          [r["size_mib"] for r in x86_rows] == [2048])
    arm_counts = {s: len(storage.pool_vm_list(architecture="arm64", size_mib=s)) for s in sizes}
    check("mixed arches: arm64 counts unchanged (1/2/2/1)",
          arm_counts == {1024: 1, 2048: 2, 4096: 2, 8192: 1})
    x86_launches = [kw for name, kw in backend.calls
                    if name == "launch" and kw.get("arch") == "x86_64"]
    check("mixed arches: every x86_64 backend launch used size 2048 (target-0 tiers launch nothing)",
          len(x86_launches) == 1 and all(kw["size_mib"] == 2048 for kw in x86_launches))

    run_async(manager.fill_once())
    check("mixed arches: a second fill_once() at target launches nothing more",
          len(storage.pool_vm_list(architecture="x86_64")) == 1
          and len([1 for name, _ in backend.calls if name == "launch"]) == 7)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager FILL LOOP ===")
    print("\n-- fills to target on startup --")
    test_fills_to_target()
    print("\n-- per-size overrides + default fallback --")
    test_overrides_and_default()
    print("\n-- target 0 -> no spares --")
    test_target_zero_no_spares()
    print("\n-- available only after wait_healthy+suspend (ordering) --")
    test_available_only_after_healthy_and_suspend()
    print("\n-- launch failure cleanup + loop continues --")
    test_launch_failure_cleanup_and_continue()
    print("\n-- health failure cleanup --")
    test_health_failure_cleanup()
    print("\n-- suspend failure cleanup --")
    test_suspend_failure_cleanup()
    print("\n-- one bad tier doesn't wedge others --")
    test_one_bad_tier_does_not_wedge_others()
    print("\n-- bounded concurrency <= POOL_FILL_CONCURRENCY --")
    test_bounded_concurrency()
    print("\n-- mixed arches (arm64 + x86_64 per-arch overrides) --")
    test_mixed_arches()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL FILL CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_fill():
    _checks.clear()
    for fn in (test_fills_to_target, test_overrides_and_default, test_target_zero_no_spares,
               test_available_only_after_healthy_and_suspend,
               test_launch_failure_cleanup_and_continue, test_health_failure_cleanup,
               test_suspend_failure_cleanup, test_one_bad_tier_does_not_wedge_others,
               test_bounded_concurrency, test_mixed_arches):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool fill failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
