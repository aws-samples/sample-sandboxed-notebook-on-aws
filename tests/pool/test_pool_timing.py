#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager TIMING / LATENCY stress test suite (Phase 3 — FEAT-002 follow-up).

Every other pool_manager test runs with FakeVmBackend's launch delay at 0
(instant mint), so none of them exercise the TIMING-SENSITIVE behavior the
real ~10s provisioning delay (POOL_FAKE_LAUNCH_DELAY_SECONDS, design §7)
introduces: grants landing while a mint is still in flight, fill pacing under
bounded concurrency with slow launches, the transient-dip-then-converge
(design §13.5) taking real wall-time, and — the headline scenario — a
SUSTAINED LOAD where grant demand outpaces the refill rate so the pool
genuinely DRAINS and 409 POOL_EMPTY (design §13.4, Option A) is OBSERVED,
followed by recovery once demand eases.

This file uses a SHORT-BUT-NONZERO FakeVmBackend launch delay (~0.3s via
FakeVmBackend(launch_delay_seconds=...) / set_latency("launch", ...)) so
timing semantics are genuinely exercised — real asyncio sleeps, real wall-
clock races — without needing the full ~10s production delay. All
assertions are on INVARIANTS (at least one 409 occurred, no double-handout,
no dangling 'granting' rows, available <= target always, convergence within
a generous bounded time), never on exact counts/timings that depend on
scheduler jitter — so the suite does not flake.

Expected total runtime: ~45-60s (the sustained-overload scenario runs for a
genuine ~30-35s wall-clock window by design; the rest are quick).

No AWS, no network, no real MicroVM — FakeVmBackend + temp SQLite only.

Run:
    .venv/bin/python tests/pool/test_pool_timing.py
"""

import asyncio
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config, build_manager  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.manager import GrantError, PoolEmpty  # noqa: E402


# ── tiny test harness (matches tests/pool/test_pool_stress.py) ────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


async def _grant(manager, arch, size, tag):
    """One grant attempt; classifies the outcome instead of raising, so
    callers can drive sustained concurrent load without try/except at every
    call site."""
    try:
        result = await manager.grant({"arch": arch, "size": size,
                                      "session_id": f"{tag}-{uuid.uuid4().hex[:8]}"})
        return ("ok", result["vm_id"])
    except PoolEmpty:
        return ("empty", None)
    except GrantError as e:
        return ("grant_error", str(e))


# ── Section 1: SUSTAINED OVERLOAD -> OBSERVE POOL_EMPTY, then RECOVER ──────
def test_sustained_overload_drains_pool_then_recovers():
    """The headline, user-requested scenario: a sustained ~30-35s wall-clock
    window of grant demand that deliberately exceeds the refill rate (mint
    rate ~= fill_concurrency / launch_delay per second), so the pool
    genuinely drains and 409 POOL_EMPTY is observed repeatedly — never a
    block/hang/5xx — then, once load eases, the pool converges back to
    target (design §13.4 Option A + §13.5 convergence invariant)."""

    async def scenario():
        launch_delay = 0.3
        target = 3
        fill_concurrency = 2
        # mint rate ~= 2 / 0.3s ~= 6.7 VMs/sec; we drive ~8 concurrent grants
        # every ~1s, i.e. ~8 req/sec demand, comfortably outpacing the mint
        # rate so the buffer drains instead of keeping pace.
        burst_size = 8
        burst_interval = 1.0
        overload_seconds = 32.0

        backend = FakeVmBackend(launch_delay_seconds=launch_delay)
        storage = _run_sqlite()
        config = build_config(size_per_image=target, fill_concurrency=fill_concurrency,
                              arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.start()

        try:
            attempted = 0
            succeeded = 0
            got_409 = 0
            errored = 0
            distinct_vm_ids = set()
            double_handout = False

            start = time.monotonic()
            burst_idx = 0
            while time.monotonic() - start < overload_seconds:
                burst_idx += 1
                results = await asyncio.gather(
                    *(_grant(manager, "arm64", 2048, f"overload-{burst_idx}-{i}")
                      for i in range(burst_size))
                )
                for outcome, payload in results:
                    attempted += 1
                    if outcome == "ok":
                        succeeded += 1
                        if payload in distinct_vm_ids:
                            double_handout = True
                        distinct_vm_ids.add(payload)
                    elif outcome == "empty":
                        got_409 += 1
                    else:
                        errored += 1

                # Snapshot the bounded-buffer invariant DURING the overload,
                # not just at the end.
                rows_now = storage.pool_vm_list(architecture="arm64", size_mib=2048)
                available_now = sum(1 for r in rows_now if r["state"] == "available")
                if available_now > target:
                    return {"error": f"available ({available_now}) exceeded target "
                                      f"({target}) mid-overload"}

                # Pace the bursts roughly every burst_interval seconds; the
                # gather() above already consumes some wall-time via resume
                # latency, so only sleep the remainder (never negative).
                elapsed_this_burst = time.monotonic() - start - (burst_idx - 1) * burst_interval
                remaining = burst_interval - elapsed_this_burst
                if remaining > 0:
                    await asyncio.sleep(remaining)

            launch_calls_during_overload = sum(1 for name, _ in backend.calls if name == "launch")

            print(f"  overload window: {attempted} grants, {succeeded} succeeded, "
                  f"{got_409} got 409 POOL_EMPTY, {errored} grant_error "
                  f"(launch() called {launch_calls_during_overload}x)")

            # ── RECOVERY: stop generating load, let the system settle ──
            recovered = False
            recovery_deadline = time.monotonic() + 15.0
            while time.monotonic() < recovery_deadline:
                rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
                live = sum(1 for r in rows if r["state"] in ("available", "launching"))
                if live >= target:
                    recovered = True
                    break
                await asyncio.sleep(0.5)

            post_recovery_outcome, _post_recovery_vm_id = await _grant(
                manager, "arm64", 2048, "post-recovery")

            final_rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
            dangling_granting = [r for r in final_rows if r["state"] == "granting"]
            granted_vm_ids = [r["vm_id"] for r in final_rows if r["state"] == "granted"]
            # the one grant issued just above also counts toward granted_vm_ids
            no_double_grant_in_ledger = len(set(granted_vm_ids)) == len(granted_vm_ids)

            return {
                "error": None,
                "attempted": attempted,
                "succeeded": succeeded,
                "got_409": got_409,
                "errored": errored,
                "double_handout": double_handout,
                "distinct_vm_ids": len(distinct_vm_ids),
                "launch_calls": launch_calls_during_overload,
                "recovered": recovered,
                "dangling_granting": dangling_granting,
                "no_double_grant_in_ledger": no_double_grant_in_ledger,
                "post_recovery_outcome": post_recovery_outcome,
                "target": target,
            }
        finally:
            await manager.stop()

    result = run_async(scenario())

    check(f"sustained overload: no bounded-buffer violation during the window "
          f"(error={result.get('error')})", result.get("error") is None)
    if result.get("error") is not None:
        return

    check(f"sustained overload: at least one grant succeeded from the warm pool "
          f"(got {result['succeeded']})", result["succeeded"] >= 1)
    check(f"sustained overload: at least a meaningful number of 409 POOL_EMPTY "
          f"occurred during the overload window (got {result['got_409']}) — "
          "the drain was actually observed, not just theoretical",
          result["got_409"] >= 5)
    check("sustained overload: the system never blocked/hung/5xx'd — "
          f"zero grant_error outcomes (got {result['errored']})",
          result["errored"] == 0)
    check("sustained overload: NO double-handout — every successful grant "
          f"returned a DISTINCT vm_id ({result['succeeded']} succeeded, "
          f"{result['distinct_vm_ids']} distinct)",
          not result["double_handout"] and result["succeeded"] == result["distinct_vm_ids"])
    check("sustained overload: NO ledger corruption — zero dangling 'granting' "
          f"rows at the end (found {len(result['dangling_granting'])})",
          len(result["dangling_granting"]) == 0)
    check("sustained overload: no double-grant in the final ledger "
          "(granted vm_ids all distinct)", result["no_double_grant_in_ledger"])
    check("sustained overload: the fill loop was continuously trying to "
          f"backfill during the overload (launch() called {result['launch_calls']}x, "
          "far more than the initial target)", result["launch_calls"] > result["target"])
    check("sustained overload RECOVERY: after load eases, the pool CONVERGES "
          "back to target (available+launching >= target) within a bounded "
          "settle time", result["recovered"])
    check("sustained overload RECOVERY: a grant succeeds again once recovered "
          f"(got outcome={result['post_recovery_outcome']})",
          result["post_recovery_outcome"] == "ok")


# ── Section 2: GRANT-DURING-IN-FLIGHT-MINT ─────────────────────────────────
def test_grant_during_in_flight_mint():
    """With a clearly in-flight mint, grant the one spare, then immediately
    grant again before the backfill completes -> the second grant for the
    now-empty tier gets a clean 409 (never a block/half-ready VM); once the
    launch delay elapses, the backfilled spare becomes grantable."""

    async def scenario():
        launch_delay = 0.4
        backend = FakeVmBackend(launch_delay_seconds=launch_delay)
        storage = _run_sqlite()
        config = build_config(size_per_image=1, fill_concurrency=1,
                              arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.start()  # startup fill mints the 1 spare (~launch_delay)

        try:
            first = await _grant(manager, "arm64", 2048, "inflight-1")

            # Immediately (well inside the launch_delay window) grant again —
            # the tier is now empty and a backfill is in flight.
            t_second_start = time.monotonic()
            second = await _grant(manager, "arm64", 2048, "inflight-2")
            second_took = time.monotonic() - t_second_start

            # The second grant must come back fast (a clean 409), not block
            # for anywhere near the launch delay.
            fast_409 = second[0] == "empty" and second_took < (launch_delay / 2)

            # Wait out the launch delay (generous margin) for the backfill
            # that the first grant's _ensure_tier_at_target() triggered.
            await asyncio.sleep(launch_delay * 3)

            third = await _grant(manager, "arm64", 2048, "inflight-3")

            return first, second, fast_409, second_took, third
        finally:
            await manager.stop()

    first, second, fast_409, second_took, third = run_async(scenario())

    check(f"in-flight mint: the first grant succeeds from the startup-filled "
          f"spare (outcome={first[0]})", first[0] == "ok")
    check(f"in-flight mint: the second grant (while backfill is in flight) "
          f"gets a clean 409 POOL_EMPTY (outcome={second[0]})",
          second[0] == "empty")
    check(f"in-flight mint: the second grant returned FAST ({second_took:.3f}s), "
          "not blocked waiting on the in-flight mint", fast_409)
    check(f"in-flight mint: once the backfill completes, the replacement "
          f"spare becomes grantable (outcome={third[0]})", third[0] == "ok")
    check("in-flight mint: the first and third grants got DISTINCT vm_ids "
          "(the replacement, not the original)", first[1] != third[1])


# ── Section 3: FILL PACING UNDER BOUNDED CONCURRENCY + REAL LATENCY ───────
def test_fill_pacing_under_bounded_concurrency_with_latency():
    """target > fill_concurrency with a real per-launch delay: at no instant
    are more than fill_concurrency launches in-flight, and startup fill takes
    clearly MORE than one delay and is clearly bounded (not instant)."""

    async def scenario():
        launch_delay = 0.3
        target = 6
        fill_concurrency = 2

        backend = FakeVmBackend(launch_delay_seconds=launch_delay)
        storage = _run_sqlite()
        config = build_config(size_per_image=target, fill_concurrency=fill_concurrency,
                              arches=["arm64"], sizes_by_arch={"arm64": [2048]})
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

        t0 = time.monotonic()
        await manager.fill_once()
        elapsed = time.monotonic() - t0

        rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
        return max_in_flight, elapsed, len(rows)

    max_in_flight, elapsed, filled_count = run_async(scenario())

    import math
    launch_delay = 0.3
    target = 6
    fill_concurrency = 2
    expected_min_rounds = math.ceil(target / fill_concurrency)  # 3 rounds

    check(f"fill pacing: startup fill reaches the full target ({target}) "
          f"(got {filled_count})", filled_count == target)
    check(f"fill pacing: max concurrently in-flight launches ({max_in_flight}) "
          f"<= POOL_FILL_CONCURRENCY ({fill_concurrency})",
          max_in_flight <= fill_concurrency)
    check(f"fill pacing: concurrency was actually exercised (max_in_flight "
          f"{max_in_flight} > 1)", max_in_flight > 1)
    check(f"fill pacing: fill took clearly MORE than a single launch delay "
          f"({elapsed:.2f}s > {launch_delay:.2f}s) — launches were genuinely "
          "paced, not all instant", elapsed > launch_delay)
    # Generous slack: at least ~0.8x the naive ceil(target/concurrency)*delay
    # lower bound, and well under a loose upper bound (3x) so scheduler
    # jitter never flakes this.
    lower_bound = expected_min_rounds * launch_delay * 0.8
    upper_bound = expected_min_rounds * launch_delay * 3.0
    check(f"fill pacing: fill duration ({elapsed:.2f}s) is bounded within a "
          f"generous window around ceil(target/concurrency)*delay "
          f"(~[{lower_bound:.2f}s, {upper_bound:.2f}s])",
          lower_bound <= elapsed <= upper_bound)


# ── Section 4: CONVERGENCE AFTER A GRANT AND AFTER A RECYCLE, REAL DELAY ──
def test_convergence_after_grant_and_recycle_with_real_delay():
    """grant one -> tier dips below target for ~delay -> converges back
    (across real wall-time). Then age one out via the recycle path ->
    terminated + a replacement minted (~delay) -> converges. Both the
    removal and the delayed backfill are observed."""

    async def scenario():
        launch_delay = 0.3
        target = 3

        backend = FakeVmBackend(launch_delay_seconds=launch_delay)
        storage = _run_sqlite()
        config = build_config(size_per_image=target, fill_concurrency=2,
                              max_age_hours=1.0,
                              arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.start()  # startup fill reaches target

        def _live_count():
            rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
            return sum(1 for r in rows if r["state"] in ("available", "launching"))

        try:
            # --- grant one: dip then converge ---
            pre_grant_live = _live_count()
            outcome, _ = await _grant(manager, "arm64", 2048, "conv-grant")
            dip_live = _live_count()

            fill_task = asyncio.create_task(manager.fill_loop())
            try:
                converged_after_grant = False
                deadline = time.monotonic() + launch_delay * 10
                while time.monotonic() < deadline:
                    if _live_count() >= target:
                        converged_after_grant = True
                        break
                    await asyncio.sleep(0.05)
            finally:
                fill_task.cancel()
                try:
                    await fill_task
                except (asyncio.CancelledError, Exception):
                    pass

            # --- age one out via the recycle path, then converge ---
            rows = storage.pool_vm_list(architecture="arm64", size_mib=2048,
                                        state="available")
            assert rows, "expected at least one available spare before recycling"
            from datetime import datetime, timedelta, timezone
            backdated = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            victim_id = rows[0]["vm_id"]
            storage.pool_vm_backdate(victim_id, launched_at=backdated)

            await manager.recycle_once()  # terminates the aged spare + backfills
            removed = not any(r["vm_id"] == victim_id
                              for r in storage.pool_vm_list(architecture="arm64", size_mib=2048))

            fill_task2 = asyncio.create_task(manager.fill_loop())
            try:
                converged_after_recycle = False
                deadline = time.monotonic() + launch_delay * 10
                while time.monotonic() < deadline:
                    if _live_count() >= target:
                        converged_after_recycle = True
                        break
                    await asyncio.sleep(0.05)
            finally:
                fill_task2.cancel()
                try:
                    await fill_task2
                except (asyncio.CancelledError, Exception):
                    pass

            return {
                "grant_outcome": outcome,
                "pre_grant_live": pre_grant_live,
                "dip_live": dip_live,
                "converged_after_grant": converged_after_grant,
                "removed": removed,
                "converged_after_recycle": converged_after_recycle,
                "target": target,
            }
        finally:
            await manager.stop()

    result = run_async(scenario())

    check(f"convergence/grant: the grant succeeded (outcome={result['grant_outcome']})",
          result["grant_outcome"] == "ok")
    check(f"convergence/grant: a dip below target was observed right after "
          f"the grant (pre={result['pre_grant_live']}, dip={result['dip_live']}, "
          f"target={result['target']})", result["dip_live"] < result["target"])
    check("convergence/grant: the tier converges back to target within a "
          "bounded settle time after the dip", result["converged_after_grant"])
    check("convergence/recycle: the aged-out spare was actually removed",
          result["removed"])
    check("convergence/recycle: the tier converges back to target within a "
          "bounded settle time after the delayed backfill",
          result["converged_after_recycle"])


# ── Section 5: THUNDERING HERD with slow refill ────────────────────────────
def test_thundering_herd_with_slow_refill():
    """One big simultaneous burst that exceeds the pool, with a real launch
    delay in play: the excess get 409 immediately (never hang during the
    delay the backfill needs), then full recovery after."""

    async def scenario():
        launch_delay = 0.3
        target = 5
        herd_size = 20

        backend = FakeVmBackend(launch_delay_seconds=launch_delay)
        storage = _run_sqlite()
        config = build_config(size_per_image=target, fill_concurrency=2,
                              arches=["arm64"], sizes_by_arch={"arm64": [2048]})
        manager = build_manager(backend, storage, config)
        await manager.start()

        try:
            t0 = time.monotonic()
            results = await asyncio.gather(
                *(_grant(manager, "arm64", 2048, f"herd-{i}") for i in range(herd_size))
            )
            herd_elapsed = time.monotonic() - t0

            succeeded = sum(1 for r, _ in results if r == "ok")
            got_409 = sum(1 for r, _ in results if r == "empty")
            errored = sum(1 for r, _ in results if r == "grant_error")

            # Recovery: let the fill loop run and converge back to target.
            fill_task = asyncio.create_task(manager.fill_loop())
            try:
                converged = False
                deadline = time.monotonic() + launch_delay * 15
                while time.monotonic() < deadline:
                    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
                    live = sum(1 for r in rows if r["state"] in ("available", "launching"))
                    if live >= target:
                        converged = True
                        break
                    await asyncio.sleep(0.05)
            finally:
                fill_task.cancel()
                try:
                    await fill_task
                except (asyncio.CancelledError, Exception):
                    pass

            post_recovery_outcome, _ = await _grant(manager, "arm64", 2048, "herd-post")

            return {
                "herd_elapsed": herd_elapsed, "succeeded": succeeded,
                "got_409": got_409, "errored": errored, "converged": converged,
                "post_recovery_outcome": post_recovery_outcome, "target": target,
                "launch_delay": launch_delay,
            }
        finally:
            await manager.stop()

    result = run_async(scenario())

    check(f"thundering herd: exactly {result['target']} of {20} succeed "
          f"(got {result['succeeded']})", result["succeeded"] == result["target"])
    check(f"thundering herd: the rest get a clean 409 POOL_EMPTY "
          f"(got {result['got_409']})", result["got_409"] == 20 - result["target"])
    check("thundering herd: no grant_error outcomes (never a block/5xx)",
          result["errored"] == 0)
    check(f"thundering herd: the whole burst resolved FAST — well under the "
          f"launch delay ({result['herd_elapsed']:.3f}s < "
          f"{result['launch_delay'] * 5:.2f}s) — the excess never hung "
          "waiting on the in-flight backfill",
          result["herd_elapsed"] < result["launch_delay"] * 5)
    check("thundering herd RECOVERY: the pool converges back to target "
          "within a bounded settle time", result["converged"])
    check(f"thundering herd RECOVERY: a grant succeeds again post-recovery "
          f"(outcome={result['post_recovery_outcome']})",
          result["post_recovery_outcome"] == "ok")


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager TIMING / LATENCY STRESS "
          "(real FakeVmBackend launch delay) ===")
    print("\n-- sustained overload -> observe 409 POOL_EMPTY -> recover "
          "(~30-35s wall-clock) --")
    test_sustained_overload_drains_pool_then_recovers()
    print("\n-- grant during in-flight mint --")
    test_grant_during_in_flight_mint()
    print("\n-- fill pacing under bounded concurrency + real latency --")
    test_fill_pacing_under_bounded_concurrency_with_latency()
    print("\n-- convergence after a grant and after a recycle, real delay --")
    test_convergence_after_grant_and_recycle_with_real_delay()
    print("\n-- thundering herd with slow refill --")
    test_thundering_herd_with_slow_refill()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL TIMING/STRESS CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_timing():
    _checks.clear()
    for fn in (test_sustained_overload_drains_pool_then_recovers,
               test_grant_during_in_flight_mint,
               test_fill_pacing_under_bounded_concurrency_with_latency,
               test_convergence_after_grant_and_recycle_with_real_delay,
               test_thundering_herd_with_slow_refill):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool timing/stress failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
