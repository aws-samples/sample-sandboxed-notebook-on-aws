#!/usr/bin/env python3
# test-kind: offline
"""
PoolManager GRANT test suite (Phase 3 — FEAT-002, plan.md item 9).

Exercises PoolManager.grant() against FakeVmBackend + a temp SQLite file:
happy path (claim-youngest -> resume -> rebind -> granted, returns
{vm_id,endpoint}, real session fields present in the rebind identity_payload,
fill signaled); restore path (restore_state called AFTER rebind, with
artifacts_bucket present in the rebind payload); POOL_EMPTY -> PoolEmpty/409;
youngest-first with staggered ages; failure injection on resume/rebind/restore
each -> spare terminated+deleted, NOT left granting, NOT half-bound, surfaced
as GrantError, no dangling granting row.

Also asserts the HARD INVARIANT (design §13.5) that a grant FAILURE's cleanup
(terminate+delete the claimed spare) ALSO triggers an immediate demand-aware
backfill — the pool must not stay below target after a failed grant either.

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_pool_grant.py
"""

import asyncio
import os
import sys

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


def _req(arch="arm64", size=2048, session_id="real-sess-1", **extra):
    return {"arch": arch, "size": size, "session_id": session_id, **extra}


# ── Section 1: happy path ───────────────────────────────────────────────────
def test_happy_path():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    result = run_async(manager.grant(_req(session_id="real-sess-happy",
                                          notebook_id="nb-1", notebook_name="My Notebook")))

    check("grant() returns a dict with vm_id and endpoint",
          "vm_id" in result and "endpoint" in result)

    names = [name for name, _ in backend.calls]
    check("grant happy path calls resume then rebind in order",
          names.index("resume") < names.index("rebind"))

    rebind_call = next(args for name, args in backend.calls if name == "rebind")
    payload = rebind_call["identity_payload"]
    check("rebind identity_payload carries the real session_id",
          payload["session_id"] == "real-sess-happy")
    check("rebind identity_payload carries notebook fields from the request",
          payload["notebook_id"] == "nb-1" and payload["notebook_name"] == "My Notebook")

    rows = storage.pool_vm_list()
    granted = [r for r in rows if r["vm_id"] == result["vm_id"]]
    check("the claimed row is in state 'granted' after a successful grant",
          granted and granted[0]["state"] == "granted")
    check("the granted row's real_session_id matches the request",
          granted[0]["real_session_id"] == "real-sess-happy")

    check("a successful grant signals the fill loop for demand-aware backfill",
          manager._refill_signal.is_set())


# ── Section 2: restore path — restore_state AFTER rebind + artifacts_bucket ─
def test_restore_path_after_rebind_with_artifacts_bucket():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    run_async(manager.grant(_req(
        session_id="real-sess-restore",
        restore_from="prior-session-xyz",
        artifacts_bucket="my-artifacts-bucket",
    )))

    names = [name for name, _ in backend.calls]
    check("restore_state is called when restore_from is present",
          "restore_state" in names)
    check("restore_state is called AFTER rebind",
          names.index("rebind") < names.index("restore_state"))

    rebind_call = next(args for name, args in backend.calls if name == "rebind")
    check("artifacts_bucket is present in the rebind payload (set BEFORE restore)",
          rebind_call["identity_payload"].get("artifacts_bucket") == "my-artifacts-bucket")

    restore_call = next(args for name, args in backend.calls if name == "restore_state")
    check("restore_state receives the correct restore_from_session",
          restore_call["restore_from_session"] == "prior-session-xyz")


def test_restore_skipped_without_restore_from():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    run_async(manager.grant(_req(session_id="real-sess-no-restore")))

    names = [name for name, _ in backend.calls]
    check("restore_state is NOT called when restore_from is absent",
          "restore_state" not in names)


# ── Section 3: POOL_EMPTY -> PoolEmpty ──────────────────────────────────────
def test_pool_empty():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())  # target 0 -> nothing to grant

    try:
        run_async(manager.grant(_req()))
        raised = None
    except Exception as e:
        raised = e
    check("grant() with no available spare raises PoolEmpty specifically",
          isinstance(raised, PoolEmpty))


# ── Section 4: youngest-first with staggered ages ──────────────────────────
def test_youngest_first_staggered():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=3, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("setup: fill produced exactly 3 spares for the staggered-age check",
          len(rows) == 3)

    # Backdate them to distinct, known ages via direct storage access
    # (claim_youngest reads launched_at from the DB row, not the backend).
    from datetime import datetime, timedelta, timezone
    ages = [300, 150, 10]  # oldest, middle, youngest
    conn_rows = sorted(rows, key=lambda r: r["vm_id"])
    for row, age in zip(conn_rows, ages):
        new_launched_at = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        storage.pool_vm_backdate(row["vm_id"], launched_at=new_launched_at)
    youngest_vm_id = conn_rows[2]["vm_id"]

    result = run_async(manager.grant(_req(session_id="real-sess-youngest")))
    check("grant() claims the YOUNGEST spare first",
          result["vm_id"] == youngest_vm_id)


# ── Section 5: resume failure -> terminated+deleted, GrantError, no dangle ──
def test_resume_failure_cleanup():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())
    spare_id = storage.pool_vm_list(architecture="arm64", size_mib=2048)[0]["vm_id"]

    backend.fail_resume_times(1)
    try:
        run_async(manager.grant(_req(session_id="real-sess-resume-fail")))
        raised = None
    except Exception as e:
        raised = e
    check("a resume failure surfaces as GrantError", isinstance(raised, GrantError))

    rows = storage.pool_vm_list()
    check("the failed spare is deleted from pool_vms (no dangling row at all)",
          spare_id not in {r["vm_id"] for r in rows})
    check("no row is left in 'granting' state after the failure",
          not any(r["state"] == "granting" for r in rows))
    backend_vms = run_async(backend.list_all())
    check("the failed spare's backend VM is terminated",
          spare_id not in {h.vm_id for h in backend_vms})


# ── Section 5b: resume returns DEAD -> terminated+deleted, GrantError ──────
def test_resume_returns_dead_cleanup():
    """resume() can come back without raising yet leave the VM DEAD (design
    §3.1's fault surface). grant() must treat this the same as a resume
    failure: terminate+delete the spare, never leave it 'granting' or
    'granted', and surface GrantError — never hand back a half-bound VM."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())
    spare_id = storage.pool_vm_list(architecture="arm64", size_mib=2048)[0]["vm_id"]

    backend.fail_resume_returns_dead_times(1)
    try:
        run_async(manager.grant(_req(session_id="real-sess-resume-dead")))
        raised = None
    except Exception as e:
        raised = e
    check("a resume-returns-dead surfaces as GrantError", isinstance(raised, GrantError))

    rows = storage.pool_vm_list()
    check("the dead spare is deleted from pool_vms (no dangling row at all)",
          spare_id not in {r["vm_id"] for r in rows})
    check("no row is left in 'granting' state after resume-returns-dead",
          not any(r["state"] == "granting" for r in rows))
    check("the spare is NOT left half-bound in 'granted' either",
          not any(r["state"] == "granted" and r["vm_id"] == spare_id for r in rows))

    names = [name for name, _ in backend.calls]
    check("rebind is NEVER called when resume comes back dead",
          "rebind" not in names)


# ── Section 6: rebind failure -> terminated+deleted, GrantError, no dangle ──
def test_rebind_failure_cleanup():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())
    spare_id = storage.pool_vm_list(architecture="arm64", size_mib=2048)[0]["vm_id"]

    backend.fail_next_rebind()
    try:
        run_async(manager.grant(_req(session_id="real-sess-rebind-fail")))
        raised = None
    except Exception as e:
        raised = e
    check("a rebind failure surfaces as GrantError", isinstance(raised, GrantError))

    rows = storage.pool_vm_list()
    check("the failed spare is deleted from pool_vms", spare_id not in {r["vm_id"] for r in rows})
    check("no row is left 'granting' after a rebind failure",
          not any(r["state"] == "granting" for r in rows))
    check("the spare is NOT left half-bound in 'granted' either",
          not any(r["state"] == "granted" and r["vm_id"] == spare_id for r in rows))


# ── Section 7: restore failure -> terminated+deleted, GrantError, no dangle ─
def test_restore_failure_cleanup():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())
    spare_id = storage.pool_vm_list(architecture="arm64", size_mib=2048)[0]["vm_id"]

    backend.fail_next_restore_state()
    try:
        run_async(manager.grant(_req(session_id="real-sess-restore-fail",
                                     restore_from="prior-sess")))
        raised = None
    except Exception as e:
        raised = e
    check("a restore_state failure surfaces as GrantError", isinstance(raised, GrantError))

    rows = storage.pool_vm_list()
    check("the failed spare is deleted from pool_vms after a restore failure",
          spare_id not in {r["vm_id"] for r in rows})
    check("no row is left 'granting' after a restore failure",
          not any(r["state"] == "granting" for r in rows))


# ── Section 8: grant-failure cleanup ALSO triggers immediate backfill ──────
def test_grant_failure_triggers_immediate_backfill_signal():
    """Hard invariant: when a claimed spare is torn down due to a resume/
    rebind/restore failure, it also LEFT the pool — the tier must be
    backfilled immediately, not left below target until the next periodic
    sweep. This asserts the SAME _refill_signal mechanism grant() success
    and recycle_once() use is also triggered on the grant-failure path."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    backend.fail_resume_times(1)
    check("_refill_signal starts unset", not manager._refill_signal.is_set())
    try:
        run_async(manager.grant(_req(session_id="real-sess-signal-check")))
    except GrantError:
        pass
    check("a grant failure's cleanup sets _refill_signal immediately "
          "(the pool must not stay below target after a failed grant)",
          manager._refill_signal.is_set())

    # And the tier actually converges back to target once fill_once() runs.
    run_async(manager.fill_once())
    rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("the tier converges back to target (1) after the failed-grant backfill",
          len(rows) == 1)


# ── Section 9: two-arch pool — a grant only ever gets a spare of its arch ──
def test_two_arch_grant_matches_arch():
    """With arm64 and x86_64 spares side by side, an x86_64 grant must get the
    x86_64 spare and an arm64 grant must never be handed it (the image's CPU
    architecture can't change after launch)."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64", "x86_64"],
                          sizes_by_arch={"arm64": [2048], "x86_64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    x86_rows = storage.pool_vm_list(architecture="x86_64", size_mib=2048)
    arm_rows = storage.pool_vm_list(architecture="arm64", size_mib=2048)
    check("setup: fill produced one spare per arch",
          len(x86_rows) == 1 and len(arm_rows) == 1)
    x86_id = x86_rows[0]["vm_id"] if x86_rows else None
    arm_id = arm_rows[0]["vm_id"] if arm_rows else None

    x86_grant = run_async(manager.grant(_req(arch="x86_64", session_id="real-sess-x86")))
    check("an x86_64 grant gets the x86_64 spare", x86_grant["vm_id"] == x86_id)

    arm_grant = run_async(manager.grant(_req(arch="arm64", session_id="real-sess-arm")))
    check("an arm64 grant gets the arm64 spare, never the x86_64 one",
          arm_grant["vm_id"] == arm_id and arm_grant["vm_id"] != x86_id)

    # Only x86_64 spares left (refill not yet run): an arm64 grant must not
    # fall back to another arch's spare.
    run_async(manager.fill_once())
    for r in storage.pool_vm_list(architecture="arm64", size_mib=2048):
        if r["state"] == "available":
            storage.pool_vm_delete(r["vm_id"])
    try:
        stolen = run_async(manager.grant(_req(arch="arm64", session_id="real-sess-arm-2")))
        raised = None
    except Exception as e:
        stolen, raised = None, e
    check("an arm64 grant with only x86_64 spares available raises PoolEmpty "
          "(no cross-arch fallback)", isinstance(raised, PoolEmpty) and stolen is None)
    left = [r for r in storage.pool_vm_list(architecture="x86_64", size_mib=2048)
            if r["state"] == "available"]
    check("the x86_64 spare stays available after the refused arm64 grant",
          len(left) == 1)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== PoolManager GRANT ===")
    print("\n-- happy path --")
    test_happy_path()
    print("\n-- restore path: restore_state AFTER rebind + artifacts_bucket --")
    test_restore_path_after_rebind_with_artifacts_bucket()
    print("\n-- restore skipped without restore_from --")
    test_restore_skipped_without_restore_from()
    print("\n-- POOL_EMPTY -> PoolEmpty --")
    test_pool_empty()
    print("\n-- youngest-first with staggered ages --")
    test_youngest_first_staggered()
    print("\n-- resume failure cleanup --")
    test_resume_failure_cleanup()
    print("\n-- resume returns dead cleanup --")
    test_resume_returns_dead_cleanup()
    print("\n-- rebind failure cleanup --")
    test_rebind_failure_cleanup()
    print("\n-- restore failure cleanup --")
    test_restore_failure_cleanup()
    print("\n-- grant-failure cleanup triggers immediate backfill signal --")
    test_grant_failure_triggers_immediate_backfill_signal()
    print("\n-- two-arch pool: grants match arch --")
    test_two_arch_grant_matches_arch()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL GRANT CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_grant():
    _checks.clear()
    for fn in (test_happy_path, test_restore_path_after_rebind_with_artifacts_bucket,
               test_restore_skipped_without_restore_from, test_pool_empty,
               test_youngest_first_staggered, test_resume_failure_cleanup,
               test_resume_returns_dead_cleanup,
               test_rebind_failure_cleanup, test_restore_failure_cleanup,
               test_grant_failure_triggers_immediate_backfill_signal,
               test_two_arch_grant_matches_arch):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool grant failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
