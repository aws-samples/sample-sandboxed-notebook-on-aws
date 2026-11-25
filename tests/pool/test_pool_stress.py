#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager CONCURRENCY / STRESS test suite (Phase 3 — FEAT-002, plan.md
item 10). Drives the REAL async PoolManager (not mocked) with FakeVmBackend
+ a temp SQLite file under asyncio.gather / ThreadPoolExecutor-style load:

  - 50 concurrent grant() vs 20 spares -> exactly 20 succeed with DISTINCT
    vm_ids, 30 get PoolEmpty, no ledger corruption, no granting leaks.
  - thundering-herd drain-and-refill -> graceful PoolEmpty then demand-aware
    backfill to target.
  - sustained churn (fill+recycle+grants concurrently via asyncio.gather) ->
    invariants hold: available <= target, no double-grant, no orphaned
    granting, recycled spares were < max-age, and the pool CONVERGES back to
    target per tier after any transient dip (design §13.5 applies to every
    exit path).

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_stress.py
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config, build_manager  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.manager import GrantError, PoolEmpty  # noqa: E402


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── Section 1: 50 concurrent grants vs 20 spares ───────────────────────────
def test_50_concurrent_grants_vs_20_spares():
    async def scenario():
        backend = FakeVmBackend()
        storage = _run_sqlite()
        config = build_config(size_per_image=20, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.fill_once()

        async def one_grant(i):
            try:
                result = await manager.grant({"arch": "arm64", "size": 2048,
                                             "session_id": f"sess-{i}-{uuid.uuid4().hex[:6]}"})
                return ("ok", result["vm_id"])
            except PoolEmpty:
                return ("empty", None)
            except GrantError as e:
                return ("grant_error", str(e))

        results = await asyncio.gather(*(one_grant(i) for i in range(50)))
        return storage, results

    storage, results = run_async(scenario())

    successes = [r for r in results if r[0] == "ok"]
    empties = [r for r in results if r[0] == "empty"]
    errors = [r for r in results if r[0] == "grant_error"]

    check(f"exactly 20 of 50 concurrent grants succeed (got {len(successes)})",
          len(successes) == 20)
    check(f"the remaining 30 get PoolEmpty (got {len(empties)})", len(empties) == 30)
    check("no grant errors occurred (fake backend has no faults injected)",
          len(errors) == 0)

    vm_ids = [r[1] for r in successes]
    check("all 20 successful grants have DISTINCT vm_ids (no double-grant)",
          len(set(vm_ids)) == len(vm_ids) == 20)

    rows = storage.pool_vm_list()
    check("no ledger corruption: pool_vms still has exactly 20 rows total",
          len(rows) == 20)
    check("no granting leaks: zero rows left in 'granting' state",
          not any(r["state"] == "granting" for r in rows))
    check("all 20 rows ended up 'granted'",
          all(r["state"] == "granted" for r in rows))
    granted_ids = {r["vm_id"] for r in rows}
    check("the granted vm_ids in storage match the ones returned to callers",
          granted_ids == set(vm_ids))


# ── Section 2: thundering herd drain-and-refill ────────────────────────────
def test_thundering_herd_drain_and_refill():
    async def scenario():
        backend = FakeVmBackend()
        storage = _run_sqlite()
        config = build_config(size_per_image=10, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.fill_once()

        async def one_grant(i):
            try:
                await manager.grant({"arch": "arm64", "size": 2048,
                                    "session_id": f"herd-{i}-{uuid.uuid4().hex[:6]}"})
                return "ok"
            except PoolEmpty:
                return "empty"

        # First wave: drain all 10 spares plus 20 extra requests that must
        # gracefully 409/PoolEmpty.
        first_wave = await asyncio.gather(*(one_grant(i) for i in range(30)))
        drained_ok = sum(1 for r in first_wave if r == "ok")
        drained_empty = sum(1 for r in first_wave if r == "empty")

        # Demand-aware backfill: at least one grant set _refill_signal; run
        # fill_once() (as fill_loop() would promptly do) to realize it.
        await manager.fill_once()
        rows_after_backfill = storage.pool_vm_list(architecture="arm64", size_mib=2048)

        return drained_ok, drained_empty, rows_after_backfill

    drained_ok, drained_empty, rows_after_backfill = run_async(scenario())

    check(f"thundering herd: exactly 10 of the first wave succeed (got {drained_ok})",
          drained_ok == 10)
    check(f"thundering herd: the other 20 gracefully get PoolEmpty (got {drained_empty})",
          drained_empty == 20)

    available_or_launching = [r for r in rows_after_backfill if r["state"] in ("available", "launching")]
    check("after demand-aware backfill, the tier returns to its target (10) "
          f"of available/launching spares (got {len(available_or_launching)})",
          len(available_or_launching) == 10)


# ── Section 3: sustained churn invariants + convergence to target ─────────
def test_sustained_churn_invariants():
    async def scenario():
        backend = FakeVmBackend()
        storage = _run_sqlite()
        target = 15
        max_age_hours = 1.0
        config = build_config(size_per_image=target, max_age_hours=max_age_hours,
                              fill_concurrency=5,
                              arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.fill_once()

        # Seed a handful of pre-aged spares (older than max_age_hours) so the
        # recycle loop has real work to do during the churn, mixed in with
        # the fresh ones fill_once() just created.
        aged_rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)[:4]
        for row in aged_rows:
            from datetime import datetime, timedelta, timezone
            backdated = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            storage.pool_vm_backdate(row["vm_id"], launched_at=backdated)
        aged_ids = {r["vm_id"] for r in aged_rows}

        async def grant_worker(i):
            try:
                await manager.grant({"arch": "arm64", "size": 2048,
                                    "session_id": f"churn-{i}-{uuid.uuid4().hex[:6]}"})
            except PoolEmpty:
                pass
            except GrantError:
                pass

        # Run fill, recycle, and a wave of grants CONCURRENTLY, repeated over
        # several cycles to simulate sustained churn. Track exactly which
        # vm_ids recycle_once() terminates (not grant()) via a wrapper around
        # the backend's terminate(), since both grant-failure cleanup and
        # recycle terminate VMs and only the row's prior state tells them apart.
        recycled_vm_ids = set()
        orig_terminate = backend.terminate

        async def tracking_terminate(vm):
            row_before = next(
                (r for r in storage.pool_vm_list() if r["vm_id"] == vm.vm_id), None)
            if row_before is not None and row_before["state"] == "available":
                recycled_vm_ids.add(vm.vm_id)
            return await orig_terminate(vm)

        backend.terminate = tracking_terminate

        for _cycle in range(4):
            await asyncio.gather(
                manager.fill_once(),
                manager.recycle_once(),
                *(grant_worker(i) for i in range(8)),
            )

            # Invariant checks after every cycle:
            rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
            available = [r for r in rows if r["state"] == "available"]
            granting = [r for r in rows if r["state"] == "granting"]
            granted_vm_ids = [r["vm_id"] for r in rows if r["state"] == "granted"]

            if len(available) > target:
                return {"error": f"available ({len(available)}) exceeded target ({target})"}
            if granting:
                return {"error": f"orphaned granting rows after a cycle: {granting}"}
            if len(set(granted_vm_ids)) != len(granted_vm_ids):
                return {"error": "double-grant detected: duplicate vm_id in granted state"}

        # One more fill pass lets the pool fully converge after the last cycle's dip.
        await manager.fill_once()
        final_rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
        final_live = [r for r in final_rows if r["state"] in ("available", "launching", "granting")]

        return {
            "error": None,
            "final_live_count": len(final_live),
            "target": target,
            "aged_ids": aged_ids,
            "recycled_vm_ids": recycled_vm_ids,
        }

    result = run_async(scenario())

    check("sustained churn: no invariant violation across any cycle "
          f"(error={result.get('error')})", result.get("error") is None)

    if result.get("error") is None:
        check("sustained churn: pool CONVERGES back to target per tier "
              f"(live count {result['final_live_count']} == target {result['target']})",
              result["final_live_count"] == result["target"])
        check("sustained churn: at least one spare was actually recycled "
              "during churn (the recycle loop had real work)",
              len(result["recycled_vm_ids"]) > 0)
        check("sustained churn: every recycled spare was from the pre-aged "
              "set (i.e. genuinely >= max-age, not a fresh one)",
              result["recycled_vm_ids"] <= result["aged_ids"])


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager CONCURRENCY / STRESS ===")
    print("\n-- 50 concurrent grants vs 20 spares --")
    test_50_concurrent_grants_vs_20_spares()
    print("\n-- thundering herd drain-and-refill --")
    test_thundering_herd_drain_and_refill()
    print("\n-- sustained churn invariants + convergence to target --")
    test_sustained_churn_invariants()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL STRESS CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_stress():
    _checks.clear()
    for fn in (test_50_concurrent_grants_vs_20_spares, test_thundering_herd_drain_and_refill,
               test_sustained_churn_invariants):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool stress failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
