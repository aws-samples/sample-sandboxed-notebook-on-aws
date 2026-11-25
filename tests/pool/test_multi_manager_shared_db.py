#!/usr/bin/env python3
# test-kind: offline
"""
MULTI-POOL-MANAGER / SHARED-DB test (offline, no AWS, FakeVmBackend only).

Validates the multi-instance-safety work: running 2–3 PoolManager instances
AGAINST ONE SHARED pool_vms ledger (the future ECS multi-task shape) must not
double-fill, double-vend, over-trim, or wipe a peer's pool. It exercises the
three coordination mechanisms we built:
  • the advisory maintenance lock (POOL_MAINTENANCE_LOCK_KEY) that serializes
    the mutating sweeps (fill / recycle / reconcile) so only one manager runs
    them at a time — any manager, one-at-a-time;
  • the atomic claim (pool_vm_claim_youngest) that makes concurrent grants
    race-safe so a spare is never handed to two sessions;
  • reconcile-as-janitor (no cold_start_cleanup) so a dead manager's rows are
    adopted/cleaned by a surviving manager, never bulk-wiped on startup.

IMPORTANT — this is a UNIT/INTEGRATION test, 100% offline:
  • Every manager uses a **FakeVmBackend** (in-memory, zero real MicroVMs, zero
    AWS). The whole point is to test the MANAGER-COORDINATION code, not to launch
    VMs. A guard asserts no real (aws) backend is ever used.
  • The N managers share ONE SqliteStorage instance (one DB file). On SQLite the
    maintenance lock is an in-process threading.Lock keyed set (_HELD_SESSION_LOCKS),
    which is exactly the right fidelity for N in-process managers sharing one DB —
    they genuinely contend on it. (In production/ECS the SAME code path uses
    Postgres pg_try_advisory_lock for cross-PROCESS exclusion; that cross-process
    path is covered by the storage conformance suite, not here.)
  • Runs with the project venv like the other pool tests:
        .venv/bin/python tests/pool/test_multi_manager_shared_db.py
    No env gate — it's part of the offline suite (no AWS, deterministic).

CLEANUP: every pool_vms row is deleted at the end (finally), so the shared temp
DB is left empty regardless of pass/fail.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from _poolkit import _run_sqlite, build_config, build_manager  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.manager import POOL_MAINTENANCE_LOCK_KEY, GrantError, PoolEmpty  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── the shared-DB, N-manager fixture ─────────────────────────────────────────

# One pool-wide target set, shared by every manager (they all read the same
# config and the same ledger). Total target = 1+2+2+1 = 6 across 4 tiers.
TIERS = {"arm64": [1024, 2048, 4096, 8192]}
OVERRIDES = {1024: 1, 2048: 2, 4096: 2, 8192: 1}
TARGET_TOTAL = sum(OVERRIDES.values())
NUM_MANAGERS = 3


def _rebind_loop_primitives(managers):
    """Rebind each manager's _refill_signal to a FRESH asyncio.Event on the
    CURRENTLY-RUNNING event loop.

    WHY THIS EXISTS (test-harness only): a PoolManager creates its
    _refill_signal (an asyncio.Event) in __init__, which runs ONCE in main()
    before any event loop exists. In production that's fine — a task has a single
    long-lived loop. But this suite drives each scenario with its own
    asyncio.run(), i.e. a FRESH event loop per scenario. An asyncio.Event binds
    to the first loop that awaits it, so a signal first used in scenario 1's loop
    is bound to a now-dead loop by scenario 8 — grant()'s .set() then never wakes
    fill_loop()'s .wait() (the demand-aware-refill path). Rebinding to a new
    Event on the live loop before we start() the loops makes each scenario behave
    exactly like a fresh process/loop. Must be called from INSIDE a running loop
    (i.e. within the scenario's async go())."""
    for mgr, _ in managers:
        mgr._refill_signal = asyncio.Event()


def _fast_intervals(managers, fill=0.15, recycle=0.2, reconcile=0.2):
    """Shrink every manager's loop intervals so start()'s REAL background loops
    (fill_loop / recycle_loop / reconcile_loop) tick in fractions of a second
    instead of the production 30s / 60s / 60s. This lets us exercise the ACTUAL
    loop machinery (locked periodic sweeps running concurrently across managers)
    deterministically — the reconcile loop that runs 'every 60s' in prod runs
    every ~0.2s here, so a few seconds of wall-clock covers many reconcile
    cycles. The intervals are plain instance attrs, so this is a safe override."""
    for mgr, _ in managers:
        mgr.fill_interval_seconds = fill
        mgr.recycle_interval_seconds = recycle
        mgr.reconcile_interval_seconds = reconcile


def _make_managers(storage, n=NUM_MANAGERS):
    """Build n PoolManager instances, all wired to the SAME shared storage AND
    the SAME shared FakeVmBackend, with an identical pool-wide config. Returns
    (managers_list, shared_backend).

    WHY ONE SHARED BACKEND: in production every pool-manager instance talks to
    the ONE AWS lambda-microvms control plane — so ANY manager can resume/
    terminate ANY VM, including a spare a different manager minted (that's the
    whole point of a shared pool). The FakeVmBackend models that control plane,
    so the N managers must share a single FakeVmBackend instance. (Giving each
    manager its own fake backend would wrongly model N separate control planes:
    a manager couldn't resume a peer-minted VM, which never happens on AWS.)
    What is NOT shared and genuinely separate per manager: the PoolManager object
    itself and its background-loop/async context — which is exactly what the
    multi-instance coordination (advisory lock + atomic claim) must make safe.
    Zero launch delay → deterministic/fast."""
    shared_backend = FakeVmBackend(launch_delay_seconds=0.0)  # FAKE — never real VMs
    managers = []
    for _ in range(n):
        config = build_config(
            arches=["arm64"],
            sizes_by_arch=TIERS,
            size_overrides=dict(OVERRIDES),
            size_per_image=2,
            fill_concurrency=4,
        )
        mgr = build_manager(shared_backend, storage, config)
        managers.append((mgr, shared_backend))
    return managers


def _available_total(storage):
    return storage.pool_vm_count_by_size  # bound helper used ad hoc below


def _counts(storage):
    """Pool-wide {state: count} across all tiers, from the shared ledger."""
    out = {}
    for row in storage.pool_vm_list():
        out[row["state"]] = out.get(row["state"], 0) + 1
    return out


def _available_count(storage):
    return sum(1 for r in storage.pool_vm_list() if r["state"] == "available")


def _available_by_tier(storage):
    """{size_mib: available_count} across the shared ledger — used to assert the
    pool matches the EXACT per-tier target shape, not just the right total."""
    out = {}
    for r in storage.pool_vm_list():
        if r["state"] == "available":
            out[r["size_mib"]] = out.get(r["size_mib"], 0) + 1
    return out


def _ledger_consistent(storage):
    """No vm_id appears twice; every row has a known state. (The ledger is a DB
    table so uniqueness is enforced by the vm_id PK, but we assert it anyway to
    catch any logic that fabricates/dupes ids.)"""
    rows = storage.pool_vm_list()
    ids = [r["vm_id"] for r in rows]
    if len(ids) != len(set(ids)):
        return False, f"duplicate vm_id in ledger: {len(ids)} rows, {len(set(ids))} unique"
    bad = [r["vm_id"] for r in rows if r["state"] not in
           ("launching", "available", "granting", "granted", "dead")]
    if bad:
        return False, f"rows in unknown state: {bad}"
    return True, ""


# ── PRE-FLIGHT: N managers up, all pointing at the same DB ───────────────────

def _preflight(storage, managers):
    """Prerequisite check (as asked): confirm 2–3 pool-manager instances exist,
    all bound to the SAME shared DB, all enabled, all on the FAKE backend (never
    real VMs), and all agreeing on the same pool-wide target."""
    print("-- pre-flight: N managers sharing one DB --")
    check(f"spun up {NUM_MANAGERS} PoolManager instances (2–3 expected)",
          2 <= len(managers) <= 3 and len(managers) == NUM_MANAGERS)

    # All managers share the EXACT same storage object → the same DB file + the
    # same in-process maintenance-lock set. This is what makes them coordinate.
    same_storage = all(m.storage is storage for m, _ in managers)
    check("every manager points at the SAME shared storage (one DB / one ledger)",
          same_storage)

    all_enabled = all(m.config.enabled for m, _ in managers)
    check("every manager is enabled", all_enabled)

    # Guard: this test must NEVER touch a real backend. Every backend is a
    # FakeVmBackend and every config.backend is 'fake'.
    all_fake = all(isinstance(b, FakeVmBackend) for _, b in managers) and \
        all(m.config.backend == "fake" for m, _ in managers)
    check("every manager uses the FAKE backend — no real MicroVMs/AWS", all_fake)

    same_target = all(
        m.config.target_for("arm64", 2048) == OVERRIDES[2048] and
        m.config.target_for("arm64", 8192) == OVERRIDES[8192]
        for m, _ in managers)
    check(f"every manager agrees on the pool-wide target (total={TARGET_TOTAL})",
          same_target)

    # The shared ledger starts empty.
    check("shared ledger starts empty", len(storage.pool_vm_list()) == 0)
    if not all(ok for _, ok in _checks):
        raise RuntimeError("pre-flight failed — see checks above")


# ── SCENARIO 1: SIMULTANEOUS start() cold boot → EXACT per-tier pool, no more ─
#    THE core question: when N managers boot AT THE SAME INSTANT (the real ECS
#    cold-start race — every task runs start()'s initial locked fill together),
#    does the shared pool end up EXACTLY the configured shape
#    {1024:1, 2048:2, 4096:2, 8192:1} = 6, and NEVER more?
#
#    This is stronger than "concurrent fill_once": it drives the ACTUAL start()
#    boot path (locked reconcile → locked fill → loop launch) on all N managers
#    via a single asyncio.gather so their starts truly overlap. The overshoot
#    guard under test is fill_once counting BOTH 'available' AND 'launching' rows
#    toward each tier's target, under POOL_MAINTENANCE_LOCK_KEY — so a second
#    booting manager sees the first's in-flight launches and adds nothing. We
#    then stop the loops immediately and assert the EXACT per-tier breakdown.

def scenario_simultaneous_start_exact_pool(storage, managers):
    print("\n-- (1) SIMULTANEOUS start() on all managers → EXACT per-tier pool, no overshoot --")
    _fast_intervals(managers)  # so the loops we briefly start tick fast, then we stop them

    async def go():
        _rebind_loop_primitives(managers)  # fresh Event on THIS scenario's loop
        # Launch every manager's start() AT ONCE — real cold-start race. gather
        # overlaps them on the one event loop; the shared maintenance lock must
        # make only one manager's initial fill actually build the pool while the
        # others' are skipped (lock held) — net result EXACTLY one target pool.
        await asyncio.gather(*[m.start() for m, _ in managers])
        # Let the (now-running) fast fill loops settle to steady state and prove
        # they HOLD at target rather than creeping over it.
        settled = await _wait_until(lambda: _available_count(storage) == TARGET_TOTAL)
        overshoot = False
        for _ in range(15):
            await asyncio.sleep(0.1)
            if _available_count(storage) > TARGET_TOTAL:
                overshoot = True
                break
        # Stop all loops so later scenarios drive the sweeps by hand.
        for m, _ in managers:
            await m.stop()
        return settled, overshoot

    settled, overshoot = run_async(go())

    by_tier = _available_by_tier(storage)
    total = len(storage.pool_vm_list())
    avail = _available_count(storage)
    print(f"     → per-tier available: {by_tier} (target {OVERRIDES}); "
          f"total rows {total}, available {avail}")

    check(f"simultaneous start converged to the pool-wide total "
          f"({avail} available == {TARGET_TOTAL})", avail == TARGET_TOTAL)
    # The EXACT shape: every tier lands on its configured override, no more/less.
    exact = all(by_tier.get(size, 0) == want for size, want in OVERRIDES.items()) \
        and set(by_tier) <= set(OVERRIDES)
    check(f"EXACT per-tier match {dict(sorted(by_tier.items()))} == "
          f"{dict(sorted(OVERRIDES.items()))} — each tier built to its quantity, "
          f"no tier over-provisioned", exact)
    check(f"NO overshoot — total ledger rows did not exceed target "
          f"({total} <= {TARGET_TOTAL}); simultaneous starts did NOT each build a "
          f"full pool (N×target avoided)", total <= TARGET_TOTAL)
    check("steady-state fill loops HELD at target after simultaneous start "
          "(never crept over target once running)", settled and not overshoot)
    ok, why = _ledger_consistent(storage)
    check(f"ledger consistent after simultaneous start ({why or 'ok'})", ok)


# ── SCENARIO 2: a manager DROPS and a NEW ECS task JOINS → it ADOPTS the pool ─
#    The explicit multi-instance design: a starting instance ADOPTS the shared
#    pool rather than wiping it (manager.start() docstring). We model the real
#    ECS event — one task dies, a fresh task is scheduled in its place against
#    the SAME shared ledger + control plane — and assert the newcomer:
#      • does NOT wipe the warm pool its dead peer already built (reconcile is a
#        janitor, not a cold-start clear);
#      • adds NOTHING when the inherited pool is already at target (fill sees the
#        existing available/launching rows and computes a zero deficit);
#      • cleans up ONLY the dead peer's genuine leftovers (a ghost row whose
#        backend VM is gone), and tops a real deficit back up to target;
#      • NEVER touches a 'granted' row (a live user session the dead peer owned).

def scenario_dropped_manager_new_task_adopts(storage, managers):
    print("\n-- (2) a manager DROPS, a NEW ECS task JOINS → adopts the pool, no wipe, no re-provision --")
    shared_backend = managers[0][1]

    # Peers A+B already built a full, healthy warm pool.
    run_async(managers[0][0]._run_locked_maintenance("peerA-fill", managers[0][0].fill_once))
    pool_before = {r["vm_id"] for r in storage.pool_vm_list() if r["state"] == "available"}
    assert len(pool_before) == TARGET_TOTAL, f"setup: expected full pool, got {len(pool_before)}"

    # The dead peer also owned a GRANTED VM (a live user session) and left behind
    # a GHOST row (its backend VM is gone — the classic crashed-task leftover).
    granted = run_async(managers[0][0].grant({"arch": "arm64", "size": 2048,
                                              "session_id": "live-user-session"}))
    granted_id = granted["vm_id"]
    # Refill the hole the grant made, so we're back at target + 1 granted.
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))

    ghost_id = "vm-dead-peer-ghost"
    storage.pool_vm_create(ghost_id, "arm64", 4096, f"fake://{ghost_id}",
                           "pool-ghost", state="launching")
    storage.pool_vm_set_state(ghost_id, "available")  # ledger says available…
    # …but it has NO backend VM (never added to shared_backend) → a true ghost.

    avail_pre_join = _available_by_tier(storage)
    ghost_present = any(r["vm_id"] == ghost_id for r in storage.pool_vm_list())
    granted_present = any(r["vm_id"] == granted_id and r["state"] == "granted"
                          for r in storage.pool_vm_list())
    check("setup: dead peer left a GHOST available row + a live GRANTED row",
          ghost_present and granted_present)

    # NOW the brand-new ECS task joins: build a fresh PoolManager on the SAME
    # shared storage + SAME shared backend and call its REAL start().
    newcomer_cfg = build_config(arches=["arm64"], sizes_by_arch=TIERS,
                                size_overrides=dict(OVERRIDES), size_per_image=2,
                                fill_concurrency=4)
    newcomer = build_manager(shared_backend, storage, newcomer_cfg)
    newcomer.fill_interval_seconds = 0.15
    newcomer.recycle_interval_seconds = 0.2
    newcomer.reconcile_interval_seconds = 0.2

    launches_before = sum(1 for c in shared_backend.calls if c[0] == "launch")

    async def go():
        _rebind_loop_primitives([(newcomer, shared_backend)])
        await newcomer.start()
        # let its startup reconcile+fill settle, then stop the loops
        await _wait_until(lambda: all(r["vm_id"] != ghost_id
                                      for r in storage.pool_vm_list()))
        await _wait_until(lambda: _available_count(storage) == TARGET_TOTAL)
        await newcomer.stop()

    run_async(go())
    launches_after = sum(1 for c in shared_backend.calls if c[0] == "launch")
    launched_during_join = launches_after - launches_before

    after_avail = {r["vm_id"] for r in storage.pool_vm_list() if r["state"] == "available"}
    # 1) The warm pool the dead peer built was NOT wiped — the still-backed
    #    spares that existed before the join survive the newcomer's start().
    survivors = pool_before & after_avail
    check("new task did NOT wipe the inherited warm pool — the dead peer's "
          "still-backed spares survived the join (adopt, not clear)",
          len(survivors) >= TARGET_TOTAL - 1)  # minus the one that got granted
    # 2) The newcomer cleaned ONLY the genuine ghost.
    check("new task's startup reconcile DELETED only the dead peer's GHOST row "
          "(backend VM gone), not healthy spares",
          all(r["vm_id"] != ghost_id for r in storage.pool_vm_list()))
    # 3) The live user session the dead peer owned is untouched.
    check("new task did NOT touch the dead peer's live GRANTED session "
          "(reconcile/fill never disturb a granted VM)",
          any(r["vm_id"] == granted_id and r["state"] == "granted"
              for r in storage.pool_vm_list()))
    # 4) It re-provisioned ONLY the real deficit (the ghost's tier), not a whole
    #    fresh pool: at most a couple of launches, never a full N-set.
    check(f"new task re-provisioned ONLY the real deficit on join "
          f"({launched_during_join} launch(es)), did NOT rebuild a full pool",
          0 <= launched_during_join <= 2)
    check(f"pool at target after the new task adopted + topped up "
          f"({_available_count(storage)} == {TARGET_TOTAL})",
          _available_count(storage) == TARGET_TOTAL)
    print(f"     → pre-join available {dict(sorted(avail_pre_join.items()))}; "
          f"launches during join: {launched_during_join}; "
          f"final available {_available_count(storage)}")

    # Cleanup the extra granted row so later scenarios start clean-ish.
    try:
        shared_backend._vms.pop(granted_id, None)
        storage.pool_vm_delete(granted_id)
    except Exception:
        pass
    run_async(newcomer._run_locked_maintenance("post-join-fill", newcomer.fill_once))


# ── SCENARIO 3: concurrent fill across managers → pool-wide target, no overshoot
#    All N managers run fill_once() at the same time against the empty shared
#    ledger. The advisory lock must serialize them so the pool converges to the
#    pool-wide TARGET_TOTAL — NOT N× the target (the double-fill overshoot).

def scenario_concurrent_fill(storage, managers):
    print("\n-- (3) concurrent fill from all managers → one pool at target, no overshoot --")

    async def go():
        # Each manager's periodic fill_once must go through the SHARED maintenance
        # lock. Running them concurrently, only the lock-holder fills each round;
        # others skip. We invoke the locked path (not raw fill_once) to mirror the
        # loops exactly.
        await asyncio.gather(*[
            m._run_locked_maintenance(f"mgr{i}-fill", m.fill_once)
            for i, (m, _) in enumerate(managers)
        ])
        # One concurrent round may leave the pool short if the lock-holder's fill
        # raced an empty read; the loops are periodic, so give a couple more
        # locked rounds to converge (still concurrent, still lock-gated).
        for _ in range(3):
            await asyncio.gather(*[
                m._run_locked_maintenance(f"mgr{i}-fill", m.fill_once)
                for i, (m, _) in enumerate(managers)
            ])

    run_async(go())
    avail = _available_count(storage)
    total = len(storage.pool_vm_list())
    print(f"     → ledger: {total} rows, {avail} available (target {TARGET_TOTAL})")
    check(f"pool converged to the pool-wide target ({avail} available == {TARGET_TOTAL})",
          avail == TARGET_TOTAL)
    check(f"NO overshoot — total rows did not exceed target "
          f"({total} <= {TARGET_TOTAL}); concurrent fills did not each launch a full set",
          total <= TARGET_TOTAL)
    ok, why = _ledger_consistent(storage)
    check(f"ledger consistent after concurrent fill ({why or 'ok'})", ok)


# ── SCENARIO 4: concurrent grants from different managers → no double-vend ───
#    With the pool full, fire a grant from EACH manager concurrently (plus more
#    than there are spares). The atomic claim must hand each 'available' spare to
#    at most one grant; overflow gets PoolEmpty. A spare one manager minted can be
#    granted by ANOTHER manager (shared pool) — that's expected and fine.

def scenario_concurrent_grants_no_double_vend(storage, managers):
    print("\n-- (4) concurrent grants across managers → unique VMs, clean PoolEmpty overflow --")
    avail_before = _available_count(storage)

    async def one_grant(mgr, i):
        # All grants target the 2048 tier (target 2) to force contention on a
        # single tier across managers; a couple also hit 4096.
        size = 2048 if i % 3 else 4096
        try:
            res = await mgr.grant({"arch": "arm64", "size": size,
                                   "session_id": f"sess-{i}"})
            return ("ok", res["vm_id"])
        except PoolEmpty:
            return ("empty", None)
        except Exception as e:  # noqa: BLE001
            return ("error", repr(e))

    async def go():
        # 12 concurrent grants (4 per manager) vs 6 spares → at most 6 succeed.
        tasks = []
        for i in range(12):
            mgr = managers[i % NUM_MANAGERS][0]  # round-robin across the 3 managers
            tasks.append(one_grant(mgr, i))
        return await asyncio.gather(*tasks)

    results = run_async(go())
    ok = [r for r in results if r[0] == "ok"]
    empty = [r for r in results if r[0] == "empty"]
    errors = [r for r in results if r[0] == "error"]
    granted_ids = [vid for _, vid in ok]

    print(f"     → {len(ok)} granted, {len(empty)} PoolEmpty, {len(errors)} error "
          f"(spares available at start: {avail_before})")
    check(f"granted count did not exceed spares available at start "
          f"({len(ok)} <= {avail_before})", len(ok) <= avail_before)
    check("every granted vm_id is UNIQUE across all managers — NO double-vend",
          len(granted_ids) == len(set(granted_ids)))
    check("overflow grants returned PoolEmpty, not an error/hang",
          len(empty) >= (12 - avail_before) and not errors)
    ok_c, why = _ledger_consistent(storage)
    check(f"ledger consistent after cross-manager grants ({why or 'ok'})", ok_c)


# ── SCENARIO 5 (edge): a manager 'crashes' mid-life — a survivor reconciles ──
#    Simulate manager A dying by DROPPING its backend's knowledge of the VMs it
#    minted (so those pool_vms rows now have no backing backend VM — exactly what
#    a crashed task leaves behind). A SURVIVING manager's locked reconcile must
#    clean those stale rows (reconcile-as-janitor), NOT a cold_start wipe, and
#    must not touch rows whose VMs are still live.

def scenario_dead_manager_reconciled_by_peer(storage, managers):
    print("\n-- (5) edge: a manager 'crashes' → a surviving manager's reconcile cleans its stale rows --")
    # Refill to a clean, full pool first.
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))
    before = _available_count(storage)

    # Simulate a crashed manager leaving ORPHANED ledger rows: its spares vanish
    # from the (shared) backend but their pool_vms rows linger. With one shared
    # backend, that's modeled by removing exactly those VMs from the backend —
    # the ledger still lists them 'available', but backend.list_all() no longer
    # reports them, which is precisely the stale-row condition reconcile keys off.
    shared_backend = managers[0][1]
    victim_rows = [r for r in storage.pool_vm_list() if r["state"] == "available"][:2]
    victim_ids = {r["vm_id"] for r in victim_rows}
    for vid in list(shared_backend._vms.keys()):
        if vid in victim_ids:
            del shared_backend._vms[vid]
    # The ledger still shows them as available (stale), but the backend is empty
    # for them → a surviving manager's reconcile should delete those rows.
    stale_present = all(any(r["vm_id"] == vid for r in storage.pool_vm_list())
                        for vid in victim_ids)
    check("stale rows (dead manager's VMs) are present in the ledger pre-reconcile",
          stale_present and len(victim_ids) == 2)

    survivor = managers[1][0]  # a DIFFERENT manager than the 'victim'
    run_async(survivor._run_locked_maintenance("survivor-reconcile", survivor.reconcile_once))

    after_ids = {r["vm_id"] for r in storage.pool_vm_list()}
    check("surviving manager's reconcile DELETED the dead manager's stale rows "
          "(reconcile-as-janitor, not a cold-start wipe)",
          not (victim_ids & after_ids))
    # Reconcile must NOT have nuked the whole shared pool — the still-live spares
    # survive (then a locked fill tops the deficit back up).
    live_survived = _available_count(storage) >= (before - 2)
    check("reconcile did NOT wipe the shared pool — the live spares survived",
          live_survived)
    run_async(survivor._run_locked_maintenance("post-reconcile-fill", survivor.fill_once))
    check(f"pool refilled to target after the crash cleanup "
          f"({_available_count(storage)} == {TARGET_TOTAL})",
          _available_count(storage) == TARGET_TOTAL)


# ── SCENARIO 6 (edge): grant races the maintenance lock being HELD ──────────
#    While a manager holds the maintenance lock (mid-sweep), a grant from another
#    manager must still succeed (grant is lock-FREE by design — the atomic claim
#    handles its own safety; grants must never block on the maintenance lock).

def scenario_grant_is_lockfree_vs_held_maintenance(storage, managers):
    print("\n-- (6) edge: grant is lock-free — succeeds while the maintenance lock is held --")
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))

    async def go():
        held = asyncio.Event()
        released = asyncio.Event()

        async def hold_maintenance(mgr):
            # Acquire the shared maintenance lock and HOLD it across a grant.
            got = mgr.storage.try_session_lock(POOL_MAINTENANCE_LOCK_KEY)
            held.set()
            try:
                await released.wait()
            finally:
                if got:
                    mgr.storage.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)
            return got

        async def grant_while_held(mgr):
            await held.wait()  # ensure the lock is held before we grant
            try:
                res = await mgr.grant({"arch": "arm64", "size": 2048,
                                       "session_id": "sess-lockfree"})
                return ("ok", res["vm_id"])
            finally:
                released.set()

        holder_task = asyncio.create_task(hold_maintenance(managers[0][0]))
        grant_res = await grant_while_held(managers[1][0])
        got = await holder_task
        return got, grant_res

    got_lock, grant_res = run_async(go())
    check("one manager successfully HELD the maintenance lock", got_lock is True)
    check("a grant from ANOTHER manager succeeded WHILE the maintenance lock was "
          "held — grant is lock-free (never blocks on maintenance)",
          grant_res[0] == "ok" and bool(grant_res[1]))


# ── SCENARIO 7 (stress): all managers hammer grant+fill+recycle concurrently ─
#    Sustained concurrent load from all N managers mixing grants, locked fills,
#    and locked recycles. Invariants after each wave: available never exceeds the
#    pool-wide target, no vm_id double-vended across the whole run, ledger stays
#    consistent, and the pool converges back to target at the end.

def scenario_stress_all_managers(storage, managers):
    print("\n-- (7) stress: all managers mixing grant + fill + recycle concurrently --")
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))

    all_granted_ids = []
    overshoot_seen = [False]
    inconsistent_seen = [""]

    async def go():
        seq = {"n": 0}

        async def grant_from(mgr):
            seq["n"] += 1
            sid = f"stress-{seq['n']}"
            try:
                res = await mgr.grant({"arch": "arm64", "size": 2048, "session_id": sid})
                all_granted_ids.append(res["vm_id"])
            except PoolEmpty:
                pass
            except Exception as e:  # noqa: BLE001
                inconsistent_seen[0] = f"grant raised {e!r}"

        for _wave in range(6):
            # Each wave: every manager does something, all at once —
            # a grant, a locked fill, and a locked recycle, interleaved.
            tasks = []
            for i, (mgr, _) in enumerate(managers):
                tasks.append(grant_from(mgr))
                tasks.append(mgr._run_locked_maintenance(f"m{i}-fill", mgr.fill_once))
                tasks.append(mgr._run_locked_maintenance(f"m{i}-recycle", mgr.recycle_once))
            await asyncio.gather(*tasks, return_exceptions=True)

            # Invariant snapshot after the wave.
            avail = _available_count(storage)
            if avail > TARGET_TOTAL:
                overshoot_seen[0] = True
            ok_c, why = _ledger_consistent(storage)
            if not ok_c:
                inconsistent_seen[0] = why

        # Final convergence: let the pool settle back to target with locked fills.
        for _ in range(4):
            await asyncio.gather(*[
                m._run_locked_maintenance(f"settle{i}", m.fill_once)
                for i, (m, _) in enumerate(managers)
            ])

    run_async(go())

    check("stress: available spares NEVER exceeded the pool-wide target in any "
          "wave (no cross-manager fill overshoot)", not overshoot_seen[0])
    check("stress: every vm_id granted across the whole run is UNIQUE "
          "(no double-vend under sustained concurrent load)",
          len(all_granted_ids) == len(set(all_granted_ids)))
    check(f"stress: ledger stayed consistent every wave "
          f"({inconsistent_seen[0] or 'ok'})", not inconsistent_seen[0])
    final_avail = _available_count(storage)
    check(f"stress: pool converged back to target at the end "
          f"({final_avail} == {TARGET_TOTAL})", final_avail == TARGET_TOTAL)


# ── main ─────────────────────────────────────────────────────────────────────

def _wait_until(predicate, timeout=8.0, poll=0.1):
    """Async-poll `predicate()` until true or timeout. Returns True if it became
    true in time. Used to wait for the background loops to converge rather than
    guessing a fixed sleep."""
    async def _w():
        import time as _t
        t0 = _t.perf_counter()
        while _t.perf_counter() - t0 < timeout:
            if predicate():
                return True
            await asyncio.sleep(poll)
        return predicate()
    return _w()


# ── SCENARIO 9: the REAL background loops, running concurrently across managers
#    This is the heart of the request: don't hand-invoke the sweeps — call
#    start() on all N managers so their ACTUAL fill_loop / recycle_loop /
#    reconcile_loop tasks run on timers (shrunk so the "every 60s" reconcile
#    ticks every ~0.2s), concurrently, all contending on the shared maintenance
#    lock + shared ledger. Then drive every loop-reachable condition and assert
#    the loops handle it. One asyncio loop drives all N managers' tasks.

def scenario_real_loops_all_conditions(storage, managers):
    print("\n-- (9) REAL loops (start→fill/recycle/reconcile on fast timers) across all managers --")
    _fast_intervals(managers)
    shared_backend = managers[0][1]

    async def go():
        _rebind_loop_primitives(managers)  # fresh Event on THIS scenario's loop
        results = {}
        # Start every manager — this runs start()'s locked startup reconcile+fill
        # AND launches the three real background-loop tasks per manager (N×3 tasks
        # on one event loop, all sharing the maintenance lock + ledger).
        for mgr, _ in managers:
            await mgr.start()
        try:
            # (a) FILL LOOP convergence: the periodic locked fills bring the shared
            # pool to the pool-wide target and HOLD it there (no overshoot) even
            # with N fill loops ticking together.
            conv = await _wait_until(lambda: _available_count(storage) == TARGET_TOTAL)
            results["fill_converged"] = conv
            # Observe for a few more ticks: available must never exceed target.
            overshoot = False
            for _ in range(15):
                await asyncio.sleep(0.1)
                if _available_count(storage) > TARGET_TOTAL:
                    overshoot = True
                    break
            results["no_overshoot_under_loops"] = not overshoot

            # (b) DEMAND-AWARE REFILL: grant a spare (any manager). grant() sets
            # the shared pool's _refill_signal on ITS manager; the fill loop then
            # backfills the deficit WITHOUT waiting the full interval. Net: after a
            # grant, the pool returns to target on its own via the loop.
            gmgr = managers[0][0]
            granted = await gmgr.grant({"arch": "arm64", "size": 2048,
                                        "session_id": "loop-demand"})
            results["grant_ok"] = bool(granted.get("vm_id"))
            refilled = await _wait_until(lambda: _available_count(storage) == TARGET_TOTAL)
            results["refilled_after_grant"] = refilled

            # (c) RECYCLE LOOP — age-out: inject an AGED available spare (older
            # than max_age) straight into the shared ledger + backend. A recycle
            # loop on SOME manager must terminate+delete it, then fill restores
            # target. We age one existing available row by rewriting its
            # launched_at far into the past via a fresh very-old row.
            from datetime import datetime, timedelta, timezone
            old_iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
            # Make it a REAL backend VM too so recycle's terminate has something to
            # act on and reconcile won't treat it as a stale ghost first.
            aged = await shared_backend.launch("arm64", 2048, "pool-aged")
            storage.pool_vm_create(aged.vm_id, "arm64", 2048, aged.endpoint,
                                   "pool-aged", launched_at=old_iso, state="launching")
            storage.pool_vm_set_state(aged.vm_id, "available")
            # The recycle loop (max_age clamp 2h) must drop this 5h-old spare.
            recycled = await _wait_until(
                lambda: all(r["vm_id"] != aged.vm_id for r in storage.pool_vm_list()))
            results["aged_spare_recycled_by_loop"] = recycled
            # And the pool is still at target afterward (recycle backfills).
            results["target_held_after_recycle"] = await _wait_until(
                lambda: _available_count(storage) == TARGET_TOTAL)

            # (d) OVER-TARGET TRIM: inject EXTRA available spares beyond target on
            # one tier; the recycle loop's over-target trim must remove the excess
            # (oldest first) back down to target.
            extra_ids = []
            for k in range(3):
                ev = await shared_backend.launch("arm64", 2048, f"pool-extra-{k}")
                storage.pool_vm_create(ev.vm_id, "arm64", 2048, ev.endpoint,
                                       f"pool-extra-{k}", state="launching")
                storage.pool_vm_set_state(ev.vm_id, "available")
                extra_ids.append(ev.vm_id)
            # Now the 2048 tier has target(2)+3 = 5 available; the loop must trim to 2.
            trimmed = await _wait_until(lambda: sum(
                1 for r in storage.pool_vm_list()
                if r["state"] == "available" and r["size_mib"] == 2048) == OVERRIDES[2048])
            results["over_target_trimmed_by_loop"] = trimmed

            # (e) RECONCILE LOOP — stale ghost: a pool_vms row whose backend VM is
            # gone (crashed-manager leftover). The reconcile loop on some manager
            # must delete it. Create a row with NO backing backend VM.
            ghost_id = "vm-ghost-reconcile-loop"
            storage.pool_vm_create(ghost_id, "arm64", 2048, f"fake://{ghost_id}",
                                   "pool-ghost", state="launching")
            storage.pool_vm_set_state(ghost_id, "available")
            # (not added to shared_backend → it's a ghost)
            ghost_gone = await _wait_until(
                lambda: all(r["vm_id"] != ghost_id for r in storage.pool_vm_list()))
            results["stale_ghost_reconciled_by_loop"] = ghost_gone

            # (f) RECONCILE LOOP — stale 'granting': a row stuck in 'granting'
            # past the grant timeout is a crashed mid-grant and must be torn down.
            # Use a short grant_timeout so the loop reaps it quickly.
            for mgr, _ in managers:
                mgr.grant_timeout_seconds = 0.3
            sg = await shared_backend.launch("arm64", 2048, "pool-staleg")
            storage.pool_vm_create(sg.vm_id, "arm64", 2048, sg.endpoint,
                                   "pool-staleg", state="launching")
            storage.pool_vm_set_state(sg.vm_id, "granting", real_session_id="dead-sess")
            await asyncio.sleep(0.5)  # let it age past the 0.3s grant timeout
            staleg_gone = await _wait_until(
                lambda: all(r["vm_id"] != sg.vm_id for r in storage.pool_vm_list()))
            results["stale_granting_reconciled_by_loop"] = staleg_gone

            # Final: after all that loop activity, the pool is consistent and at
            # target, with no overshoot.
            await _wait_until(lambda: _available_count(storage) == TARGET_TOTAL)
            ok_c, why = _ledger_consistent(storage)
            results["final_consistent"] = ok_c
            results["final_why"] = why
            results["final_at_target"] = _available_count(storage) == TARGET_TOTAL
        finally:
            # Stop every manager — cancels and awaits all background-loop tasks.
            for mgr, _ in managers:
                await mgr.stop()
        return results

    r = run_async(go())
    check("(9a) N fill loops converged the shared pool to target", r.get("fill_converged"))
    check("(9a) available never exceeded target while N fill loops ran "
          "(no cross-manager overshoot under the real loops)", r.get("no_overshoot_under_loops"))
    check("(9b) grant via a manager succeeded", r.get("grant_ok"))
    check("(9b) demand-aware refill: the fill loop restored target after a grant "
          "(without waiting the full interval)", r.get("refilled_after_grant"))
    check("(9c) recycle loop terminated+deleted an aged (>max_age) spare", r.get("aged_spare_recycled_by_loop"))
    check("(9c) pool back at target after the recycle loop backfilled", r.get("target_held_after_recycle"))
    check("(9d) recycle loop's over-target trim removed excess spares back to target",
          r.get("over_target_trimmed_by_loop"))
    check("(9e) reconcile loop deleted a stale ghost row (backend VM gone)",
          r.get("stale_ghost_reconciled_by_loop"))
    check("(9f) reconcile loop tore down a stale 'granting' row past the grant timeout",
          r.get("stale_granting_reconciled_by_loop"))
    check(f"(9) ledger consistent after all loop activity ({r.get('final_why') or 'ok'})",
          r.get("final_consistent"))
    check("(9) pool at target after start→loops→stop", r.get("final_at_target"))


# ── SCENARIO 8: ALB-style dispatch — the proxy doesn't pick a manager, a load
#    balancer does. In production the proxy POSTs to {POOL_MANAGER_URL}/grant
#    (proxy/platform/microvm_manager.py:_try_pool_grant) — a SINGLE service
#    endpoint fronting N ECS tasks behind an ALB. The ALB round-robins each
#    request to one task; the proxy has NO idea (and no say) which manager serves
#    it. The coordination code must make that routing policy IRRELEVANT to
#    correctness: whichever manager the ALB lands a grant on, no spare is ever
#    double-vended, and every manager can serve any request (shared pool).
#
#    We model the ALB as an explicit round-robin dispatcher: a tiny _Alb object
#    that cycles managers[call_index % N] exactly as an ALB with round-robin
#    target selection would. Grants are issued ONLY through the dispatcher (never
#    by hand-picking a manager), proving the end-to-end proxy→ALB→manager path.

class _Alb:
    """Minimal round-robin dispatcher that stands in for the ALB fronting the N
    pool-manager ECS tasks. forward() cycles targets[next % N] — the same policy
    an ALB uses with round-robin target selection — and records which manager
    served each call so the test can assert the load actually spread across all
    N tasks (not pinned to one), mirroring what the proxy's single
    POOL_MANAGER_URL fan-out does in production."""

    def __init__(self, managers):
        self._targets = [m for m, _ in managers]
        self._i = 0
        self.dispatch_counts = {id(m): 0 for m in self._targets}

    async def forward_grant(self, payload):
        """Pick the next manager round-robin (as the ALB would) and call grant on
        it. Returns the manager's grant result. The caller (the 'proxy') never
        chose this manager — the dispatcher did."""
        mgr = self._targets[self._i % len(self._targets)]
        self._i += 1
        self.dispatch_counts[id(mgr)] += 1
        return await mgr.grant(payload)

    def spread(self):
        """How many grants each manager served. A healthy round-robin spreads
        roughly evenly across all N managers."""
        return list(self.dispatch_counts.values())


def scenario_alb_round_robin_dispatch(storage, managers):
    print("\n-- (8) ALB round-robin dispatch (proxy→ALB→manager) → no double-vend, load spread --")
    # Full pool first (any manager fills; the ALB only fronts GRANTS, same as the
    # proxy only calls /grant — fills are the managers' own background loops).
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))
    avail_before = _available_count(storage)

    alb = _Alb(managers)

    async def go():
        # Fire TARGET_TOTAL grants (one per spare) THROUGH the ALB dispatcher. The
        # proxy picks nothing — the ALB round-robins each /grant to the next task.
        # Mix tiers so different managers serve different sizes.
        async def one(i):
            size = [1024, 2048, 2048, 4096, 4096, 8192][i % 6]
            try:
                res = await alb.forward_grant({"arch": "arm64", "size": size,
                                               "session_id": f"alb-{i}"})
                return ("ok", res["vm_id"])
            except PoolEmpty:
                return ("empty", None)
            except Exception as e:  # noqa: BLE001
                return ("error", repr(e))

        # Concurrent (an ALB fans out simultaneous requests to different tasks).
        return await asyncio.gather(*[one(i) for i in range(TARGET_TOTAL)])

    results = run_async(go())
    ok = [r for r in results if r[0] == "ok"]
    errors = [r for r in results if r[0] == "error"]
    ids = [vid for _, vid in ok]
    spread = alb.spread()

    print(f"     → {len(ok)} granted via ALB round-robin; per-manager dispatch {spread} "
          f"(spares at start: {avail_before})")
    check("ALB-dispatched grants did not exceed the spares available at start "
          f"({len(ok)} <= {avail_before}) and none errored",
          len(ok) <= avail_before and not errors)
    check("every vm_id granted via the ALB is UNIQUE — the round-robin dispatch "
          "policy did NOT cause a double-vend (any manager can serve any request)",
          len(ids) == len(set(ids)))
    # The ALB actually spread the load: with NUM_MANAGERS tasks and >= N grants,
    # EVERY manager served at least one request (round-robin, not pinned to one).
    check(f"the ALB spread grants across ALL {NUM_MANAGERS} managers "
          f"(round-robin reached every task: {spread})",
          all(c >= 1 for c in spread))
    ok_c, why = _ledger_consistent(storage)
    check(f"ledger consistent after ALB-dispatched grants ({why or 'ok'})", ok_c)


# ── SCENARIO 10: reconcile must NEVER terminate a NON-POOL backend VM ────────
#    Safety-critical invariant (reconcile_once docstring): the pool does NOT own
#    the whole account — the proxy also runs its own notebook VMs that have NO
#    pool_vms row. reconcile must act ONLY on VMs it has a row for; a backend VM
#    with no pool_vms row is a live user notebook and MUST survive. (The bug this
#    guards against terminated a user's running session and produced the
#    reconnect/restore popup.) We inject such a VM and assert reconcile leaves it.

def scenario_reconcile_spares_non_pool_vm(storage, managers):
    print("\n-- (10) safety: reconcile NEVER kills a non-pool (proxy notebook) backend VM --")
    shared_backend = managers[0][1]
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))

    # A live proxy notebook VM: exists in the backend/control plane, but the pool
    # has NO ledger row for it. reconcile must not touch it.
    notebook_vm = shared_backend.inject_backend_only_vm(arch="arm64", size_mib=2048,
                                                        vm_id="vm-user-notebook-live")
    present_before = any(v.vm_id == notebook_vm.vm_id
                         for v in run_async(shared_backend.list_all()))
    check("setup: a live NON-POOL backend VM exists with no pool_vms row",
          present_before and all(r["vm_id"] != notebook_vm.vm_id
                                 for r in storage.pool_vm_list()))

    # Run reconcile on EVERY manager (any of them could be the one that sweeps).
    for mgr, _ in managers:
        run_async(mgr._run_locked_maintenance("reconcile", mgr.reconcile_once))

    still_alive = any(v.vm_id == notebook_vm.vm_id
                      for v in run_async(shared_backend.list_all()))
    check("reconcile did NOT terminate the non-pool proxy notebook VM "
          "(it owns only VMs with a pool_vms row — a user session is never killed)",
          still_alive)
    # And it was never spuriously added to the ledger either.
    check("reconcile did NOT fabricate a pool_vms row for the non-pool VM",
          all(r["vm_id"] != notebook_vm.vm_id for r in storage.pool_vm_list()))
    # Cleanup the injected VM.
    shared_backend._vms.pop(notebook_vm.vm_id, None)


# ── SCENARIO 11: grant failure → GrantError, no dangling 'granting', backfill ─
#    When resume/rebind/restore throws after a spare is claimed, grant() must:
#    terminate the backend VM, DELETE its pool_vms row (no row stuck in
#    'granting'), signal a refill, and raise GrantError — never leak a spare.

def scenario_grant_failure_cleanup(storage, managers):
    print("\n-- (11) grant failure (rebind throws) → GrantError, no dangling 'granting', pool backfills --")
    shared_backend = managers[0][1]
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))
    before_avail = _available_count(storage)

    async def go():
        shared_backend.fail_next_rebind(1)  # the next rebind() raises
        raised = False
        claimed_id = None
        try:
            await managers[1][0].grant({"arch": "arm64", "size": 2048,
                                        "session_id": "doomed-sess"})
        except GrantError:
            raised = True
        # Find whether any row is stuck in 'granting' for that session.
        rows = storage.pool_vm_list()
        dangling = [r for r in rows if r["state"] == "granting"]
        return raised, dangling

    raised, dangling = run_async(go())
    check("grant raised GrantError when rebind failed after claiming a spare", raised)
    check("NO pool_vms row left dangling in 'granting' after the failed grant "
          "(the claimed spare's row was deleted on cleanup)", not dangling)
    # The failed grant terminated the claimed spare → tier is down one; a locked
    # fill must bring it back to target (grant() signalled the refill).
    run_async(managers[0][0]._run_locked_maintenance("backfill", managers[0][0].fill_once))
    check(f"pool backfilled the spare lost to the failed grant "
          f"({_available_count(storage)} == {TARGET_TOTAL}; was {before_avail})",
          _available_count(storage) == TARGET_TOTAL)
    ok, why = _ledger_consistent(storage)
    check(f"ledger consistent after a failed grant ({why or 'ok'})", ok)


# ── SCENARIO 12: recycle must NEVER recycle/trim a GRANTED or GRANTING VM ────
#    recycle_once touches ONLY 'available' spares. An OLD granted VM (a
#    long-running user session well past max_age) and an in-flight 'granting'
#    row must both survive the recycle loop — only idle 'available' spares age
#    out or get trimmed.

def scenario_recycle_ignores_granted(storage, managers):
    print("\n-- (12) recycle NEVER recycles/trims a granted or granting VM (only idle spares) --")
    shared_backend = managers[0][1]
    run_async(managers[0][0]._run_locked_maintenance("refill", managers[0][0].fill_once))

    # A granted VM that is OLDER than max_age (long-running user session).
    from datetime import datetime, timedelta, timezone
    very_old = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    g = run_async(shared_backend.launch("arm64", 2048, "pool-granted-old"))
    storage.pool_vm_create(g.vm_id, "arm64", 2048, g.endpoint, "pool-granted-old",
                           launched_at=very_old, state="launching")
    storage.pool_vm_set_state(g.vm_id, "granted", real_session_id="old-live-user")
    # An in-flight granting VM, also old by launched_at (but fresh by updated_at).
    gg = run_async(shared_backend.launch("arm64", 2048, "pool-granting"))
    storage.pool_vm_create(gg.vm_id, "arm64", 2048, gg.endpoint, "pool-granting",
                           launched_at=very_old, state="launching")
    storage.pool_vm_set_state(gg.vm_id, "granting", real_session_id="in-flight-user")

    # Run recycle on all managers (age-out + over-target trim).
    for mgr, _ in managers:
        run_async(mgr._run_locked_maintenance("recycle", mgr.recycle_once))

    ids_after = {r["vm_id"] for r in storage.pool_vm_list()}
    check("recycle did NOT recycle a 9h-old GRANTED VM (a live user session is "
          "never aged out — only idle 'available' spares are)", g.vm_id in ids_after)
    check("recycle did NOT tear down an in-flight GRANTING VM "
          "(recycle touches only 'available')", gg.vm_id in ids_after)
    granted_row = next(r for r in storage.pool_vm_list() if r["vm_id"] == g.vm_id)
    check("the granted VM is still in state 'granted' after recycle",
          granted_row["state"] == "granted")
    # Cleanup the two synthetic rows.
    for vid in (g.vm_id, gg.vm_id):
        shared_backend._vms.pop(vid, None)
        storage.pool_vm_delete(vid)
    run_async(managers[0][0]._run_locked_maintenance("post-fill", managers[0][0].fill_once))


# ── SCENARIO 13: multi-ARCH pool — per-arch tier targets are isolated ────────
#    Every other scenario uses a single arch. _tiers() iterates config.arches;
#    a two-arch pool (arm64 + x86_64) must build EACH arch's tiers to their own
#    target independently, with no cross-arch bleed. Uses a dedicated DB +
#    managers so it doesn't perturb the arm64-only shared fixture.

def scenario_multi_arch_isolation():
    print("\n-- (13) multi-arch pool: arm64 + x86_64 tiers each reach their own target, no bleed --")
    storage = _run_sqlite()
    backend = FakeVmBackend(launch_delay_seconds=0.0)
    tiers = {"arm64": [2048, 4096], "x86_64": [2048]}
    overrides = {2048: 1, 4096: 2}  # applies per-arch by size
    cfgs = []
    mgrs = []
    for _ in range(2):
        c = build_config(arches=["arm64", "x86_64"], sizes_by_arch=tiers,
                         size_overrides=dict(overrides), size_per_image=1,
                         fill_concurrency=4)
        cfgs.append(c)
        mgrs.append((build_manager(backend, storage, c), backend))

    async def go():
        # Concurrent locked fills across both managers until convergence.
        for _ in range(5):
            await asyncio.gather(*[
                m._run_locked_maintenance(f"m{i}", m.fill_once)
                for i, (m, _) in enumerate(mgrs)])

    run_async(go())

    def avail(arch, size):
        return sum(1 for r in storage.pool_vm_list()
                   if r["state"] == "available"
                   and r["architecture"] == arch and r["size_mib"] == size)

    try:
        check(f"arm64/2048 reached its target (={overrides[2048]})",
              avail("arm64", 2048) == overrides[2048])
        check(f"arm64/4096 reached its target (={overrides[4096]})",
              avail("arm64", 4096) == overrides[4096])
        check(f"x86_64/2048 reached its target (={overrides[2048]})",
              avail("x86_64", 2048) == overrides[2048])
        total = len([r for r in storage.pool_vm_list() if r["state"] == "available"])
        want_total = overrides[2048] + overrides[4096] + overrides[2048]
        check(f"total across both arches == sum of per-arch targets "
              f"({total} == {want_total}); no cross-arch overshoot/bleed",
              total == want_total)
    finally:
        for r in storage.pool_vm_list():
            storage.pool_vm_delete(r["vm_id"])


# ── SCENARIO 14: grant with restore_from → checkpoint restore is invoked ─────
#    The grant() restore branch (restore_from set) must call backend.restore_state
#    with the right session before handing the VM over. Covers checkpoint restore
#    on adopt — a pooled spare rebound to a session that has a prior checkpoint.

def scenario_grant_with_restore():
    print("\n-- (14) grant with restore_from → backend.restore_state invoked with the checkpoint session --")
    storage = _run_sqlite()
    backend = FakeVmBackend(launch_delay_seconds=0.0)
    cfg = build_config(arches=["arm64"], sizes_by_arch={"arm64": [2048]},
                       size_overrides={2048: 1}, size_per_image=1)
    mgr = build_manager(backend, storage, cfg)

    async def go():
        await mgr._run_locked_maintenance("fill", mgr.fill_once)
        res = await mgr.grant({"arch": "arm64", "size": 2048,
                               "session_id": "restore-sess",
                               "restore_from": "prior-checkpoint-sess"})
        # Confirm the backend recorded a restore_state call for the granted VM
        # with the requested checkpoint session.
        restored = [c for c in backend.calls if c[0] == "restore_state"]
        vm = backend._vms.get(res["vm_id"])
        return res, restored, (vm.restored_from if vm else None)

    try:
        res, restored, restored_from = run_async(go())
        check("grant with restore_from returned a vm_id", bool(res.get("vm_id")))
        check("backend.restore_state was invoked during the grant", len(restored) == 1)
        check("restore_state was called with the requested checkpoint session",
              restored_from == "prior-checkpoint-sess")
    finally:
        for r in storage.pool_vm_list():
            storage.pool_vm_delete(r["vm_id"])


# ── SCENARIO 15: _launch_one partial failure → no half-built spare leaks ─────
#    If wait_healthy (or suspend) throws mid-mint, _launch_one must terminate the
#    backend VM it created AND delete the 'launching' pool_vms row — leaving no
#    orphaned backend VM and no row stuck in 'launching'. The fill loop then
#    retries cleanly on the next sweep.

def scenario_launch_partial_failure_cleanup():
    print("\n-- (15) _launch_one partial failure (wait_healthy throws) → no leaked VM, no stuck 'launching' row --")
    storage = _run_sqlite()
    backend = FakeVmBackend(launch_delay_seconds=0.0)
    cfg = build_config(arches=["arm64"], sizes_by_arch={"arm64": [2048]},
                       size_overrides={2048: 2}, size_per_image=2)
    mgr = build_manager(backend, storage, cfg)

    async def go():
        # First fill: BOTH health checks fail → both mints must self-clean.
        backend.fail_wait_healthy_times(2)
        await mgr.fill_once()
        after_fail_rows = storage.pool_vm_list()
        after_fail_backend = await backend.list_all()
        # Second fill: no injected faults → tier recovers to target.
        await mgr.fill_once()
        return after_fail_rows, after_fail_backend

    try:
        rows_after_fail, backend_after_fail = run_async(go())
        check("after a mint whose health-check failed, NO pool_vms row is left "
              "stuck in 'launching' (the half-built spare's row was deleted)",
              all(r["state"] != "launching" for r in rows_after_fail)
              and len(rows_after_fail) == 0)
        check("after the failed mint, NO orphaned backend VM is left "
              "(the created VM was terminated on cleanup)",
              len(backend_after_fail) == 0)
        final_avail = sum(1 for r in storage.pool_vm_list() if r["state"] == "available")
        check(f"a later clean fill recovers the tier to target ({final_avail} == 2)",
              final_avail == 2)
    finally:
        for r in storage.pool_vm_list():
            storage.pool_vm_delete(r["vm_id"])


# ── SCENARIO 16: status() reports accurate per-tier target vs actual counts ──

def scenario_status_report():
    print("\n-- (16) status() reports accurate per-tier target vs actual + state counts --")
    storage = _run_sqlite()
    backend = FakeVmBackend(launch_delay_seconds=0.0)
    cfg = build_config(arches=["arm64"], sizes_by_arch={"arm64": [2048, 4096]},
                       size_overrides={2048: 2, 4096: 1}, size_per_image=1)
    mgr = build_manager(backend, storage, cfg)

    async def go():
        await mgr._run_locked_maintenance("fill", mgr.fill_once)
        # Grant one 2048 spare so a tier shows a 'granted' count too.
        await mgr.grant({"arch": "arm64", "size": 2048, "session_id": "status-sess"})
        return mgr.status()

    try:
        st = run_async(go())
        tiers = st["tiers"]
        check("status() reports enabled + backend + per-tier map",
              st["enabled"] is True and st["backend"] == "fake" and "arm64:2048" in tiers)
        t2048 = tiers["arm64:2048"]
        t4096 = tiers["arm64:4096"]
        check("status() 2048 tier shows target=2 and the granted count is reflected",
              t2048["target"] == 2 and t2048["counts"].get("granted", 0) == 1)
        check("status() 4096 tier shows target=1 and actual=1 available",
              t4096["target"] == 1 and t4096["counts"].get("available", 0) == 1)
    finally:
        for r in storage.pool_vm_list():
            storage.pool_vm_delete(r["vm_id"])


def main():
    print("\n=== MULTI-POOL-MANAGER / SHARED-DB (offline, FakeVmBackend) ===\n")
    # One shared SQLite ledger; all N managers bind to THIS storage.
    storage = _run_sqlite()
    managers = _make_managers(storage, NUM_MANAGERS)

    try:
        _preflight(storage, managers)
        scenario_simultaneous_start_exact_pool(storage, managers)
        scenario_dropped_manager_new_task_adopts(storage, managers)
        scenario_concurrent_fill(storage, managers)
        scenario_concurrent_grants_no_double_vend(storage, managers)
        scenario_dead_manager_reconciled_by_peer(storage, managers)
        scenario_grant_is_lockfree_vs_held_maintenance(storage, managers)
        scenario_stress_all_managers(storage, managers)
        scenario_alb_round_robin_dispatch(storage, managers)
        scenario_real_loops_all_conditions(storage, managers)
        scenario_reconcile_spares_non_pool_vm(storage, managers)
        scenario_grant_failure_cleanup(storage, managers)
        scenario_recycle_ignores_granted(storage, managers)
        # Self-contained scenarios (own DB + managers):
        scenario_multi_arch_isolation()
        scenario_grant_with_restore()
        scenario_launch_partial_failure_cleanup()
        scenario_status_report()
    finally:
        # CLEANUP: delete every pool_vms row so the shared DB is left empty,
        # regardless of pass/fail (as asked).
        print("\n-- cleanup: deleting all pool_vms rows --")
        try:
            rows = storage.pool_vm_list()
            for r in rows:
                storage.pool_vm_delete(r["vm_id"])
            remaining = len(storage.pool_vm_list())
            print(f"   deleted {len(rows)} row(s); pool_vms now has {remaining}")
        except Exception as e:  # noqa: BLE001
            print(f"   (cleanup) failed: {e}")

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL MULTI-MANAGER / SHARED-DB CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
