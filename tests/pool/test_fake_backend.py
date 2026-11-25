#!/usr/bin/env python3
# test-kind: offline
"""
pool_manager.fake_backend self-test suite (Phase 3 — FEAT-002, plan.md item 3
verify). Exercises FakeVmBackend's fault/latency injection surface directly:
each fault toggle raises/behaves, latency delays are observable, the `calls`
log records accurately, QuotaExceededError on raise_quota_exceeded_on_launch,
force_state + inject_backend_only_vm reflected in get_state/list_all.

No AWS, no network, no real MicroVM — pure in-memory FakeVmBackend.

Run:
    .venv/bin/python tests/pool/test_fake_backend.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.vm_backend import (  # noqa: E402
    DEAD,
    RUNNING,
    SUSPENDED,
    TERMINATED,
    BackendError,
    QuotaExceededError,
)


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── Section 1: happy-path lifecycle + calls log ────────────────────────────
def test_happy_path_and_calls_log():
    backend = FakeVmBackend()

    async def go():
        vm = await backend.launch("arm64", 2048, "pool-sess-1")
        await backend.wait_healthy(vm, timeout=5.0)
        await backend.suspend(vm)
        await backend.resume(vm, timeout=5.0)
        await backend.rebind(vm, {"session_id": "real-1"})
        await backend.restore_state(vm, "prior-session")
        await backend.terminate(vm)
        return vm

    vm = run_async(go())
    check("launch() returns a VmHandle with the requested arch/size",
          vm.arch == "arm64" and vm.size_mib == 2048)
    check("launch() mints a non-empty, unique vm_id", bool(vm.vm_id))

    names = [name for name, _ in backend.calls]
    check("calls log records every invocation in order",
          names == ["launch", "wait_healthy", "suspend", "resume",
                    "rebind", "restore_state", "terminate"])
    rebind_call = next(args for name, args in backend.calls if name == "rebind")
    check("calls log records rebind's identity_payload accurately",
          rebind_call["identity_payload"] == {"session_id": "real-1"})
    restore_call = next(args for name, args in backend.calls if name == "restore_state")
    check("calls log records restore_state's restore_from_session accurately",
          restore_call["restore_from_session"] == "prior-session")

    check("terminate() drops the VM from the backend's store",
          run_async(backend.get_state(vm.vm_id)) == TERMINATED)
    check("terminated VM no longer appears in list_all()",
          vm.vm_id not in {h.vm_id for h in run_async(backend.list_all())})


# ── Section 2: each fault toggle raises/behaves ─────────────────────────────
def test_fault_toggles():
    # launch failure
    backend = FakeVmBackend()
    backend.fail_launch_times(2)
    for _ in range(2):
        try:
            run_async(backend.launch("arm64", 2048, "s"))
            raised = False
        except BackendError:
            raised = True
        check("fail_launch_times: launch raises BackendError while remaining > 0", raised)
    vm = run_async(backend.launch("arm64", 2048, "s"))
    check("fail_launch_times: launch succeeds again once the counter is exhausted",
          vm is not None)

    # wait_healthy timeout
    backend2 = FakeVmBackend()
    vm2 = run_async(backend2.launch("arm64", 2048, "s"))
    backend2.fail_wait_healthy_times(1)
    try:
        run_async(backend2.wait_healthy(vm2, timeout=1.0))
        raised = False
    except BackendError:
        raised = True
    check("fail_wait_healthy_times: wait_healthy raises BackendError", raised)
    run_async(backend2.wait_healthy(vm2, timeout=1.0))  # should not raise now

    # suspend fail
    backend3 = FakeVmBackend()
    vm3 = run_async(backend3.launch("arm64", 2048, "s"))
    backend3.fail_suspend_times(1)
    try:
        run_async(backend3.suspend(vm3))
        raised = False
    except BackendError:
        raised = True
    check("fail_suspend_times: suspend raises BackendError", raised)

    # resume fail
    backend4 = FakeVmBackend()
    vm4 = run_async(backend4.launch("arm64", 2048, "s"))
    backend4.fail_resume_times(1)
    try:
        run_async(backend4.resume(vm4, timeout=1.0))
        raised = False
    except BackendError:
        raised = True
    check("fail_resume_times: resume raises BackendError", raised)

    # resume returns dead (no raise, but state becomes DEAD)
    backend5 = FakeVmBackend()
    vm5 = run_async(backend5.launch("arm64", 2048, "s"))
    backend5.fail_resume_returns_dead_times(1)
    run_async(backend5.resume(vm5, timeout=1.0))
    check("fail_resume_returns_dead_times: resume does not raise",
          True)
    check("fail_resume_returns_dead_times: VM state is DEAD after resume",
          run_async(backend5.get_state(vm5.vm_id)) == DEAD)

    # rebind raise
    backend6 = FakeVmBackend()
    vm6 = run_async(backend6.launch("arm64", 2048, "s"))
    backend6.fail_next_rebind()
    try:
        run_async(backend6.rebind(vm6, {}))
        raised = False
    except BackendError:
        raised = True
    check("fail_next_rebind: rebind raises BackendError", raised)
    run_async(backend6.rebind(vm6, {}))  # should not raise now

    # restore_state fail
    backend7 = FakeVmBackend()
    vm7 = run_async(backend7.launch("arm64", 2048, "s"))
    backend7.fail_next_restore_state()
    try:
        run_async(backend7.restore_state(vm7, "prior"))
        raised = False
    except BackendError:
        raised = True
    check("fail_next_restore_state: restore_state raises BackendError", raised)

    # terminate fail
    backend8 = FakeVmBackend()
    vm8 = run_async(backend8.launch("arm64", 2048, "s"))
    backend8.fail_terminate_times(1)
    try:
        run_async(backend8.terminate(vm8))
        raised = False
    except BackendError:
        raised = True
    check("fail_terminate_times: terminate raises BackendError", raised)
    check("fail_terminate_times: VM is NOT removed from the store on a failed terminate",
          vm8.vm_id in {h.vm_id for h in run_async(backend8.list_all())})
    run_async(backend8.terminate(vm8))  # retry succeeds
    check("terminate retry after a failed terminate succeeds and removes the VM",
          vm8.vm_id not in {h.vm_id for h in run_async(backend8.list_all())})


# ── Section 3: quota exceeded on launch ─────────────────────────────────────
def test_quota_exceeded():
    backend = FakeVmBackend()
    backend.raise_quota_exceeded_on_launch()
    try:
        run_async(backend.launch("arm64", 2048, "s"))
        raised_type = None
    except Exception as e:
        raised_type = type(e)
    check("raise_quota_exceeded_on_launch: raises QuotaExceededError specifically",
          raised_type is QuotaExceededError)
    check("QuotaExceededError is a BackendError subclass",
          issubclass(QuotaExceededError, BackendError))
    # one-shot: next launch succeeds normally
    vm = run_async(backend.launch("arm64", 2048, "s"))
    check("raise_quota_exceeded_on_launch only affects the NEXT launch call",
          vm is not None)


# ── Section 4: latency injection ────────────────────────────────────────────
def test_latency_injection():
    backend = FakeVmBackend()
    backend.set_latency("launch", 0.15)

    start = time.monotonic()
    run_async(backend.launch("arm64", 2048, "s"))
    elapsed = time.monotonic() - start
    check(f"set_latency('launch', 0.15) delays launch() by >= 0.15s (got {elapsed:.3f}s)",
          elapsed >= 0.14)

    # unaffected method has no delay
    start2 = time.monotonic()
    run_async(backend.get_state("whatever"))
    elapsed2 = time.monotonic() - start2
    check("a method with no configured latency returns promptly (< 0.1s)",
          elapsed2 < 0.1)


# ── Section 5: force_state + inject_backend_only_vm ─────────────────────────
def test_force_state_and_inject_backend_only_vm():
    backend = FakeVmBackend()
    vm = run_async(backend.launch("arm64", 2048, "s"))
    backend.force_state(vm.vm_id, SUSPENDED)
    check("force_state: get_state reflects the forced state",
          run_async(backend.get_state(vm.vm_id)) == SUSPENDED)

    orphan = backend.inject_backend_only_vm(arch="x86_64", size_mib=4096, state=RUNNING)
    check("inject_backend_only_vm: returns a VmHandle for the injected VM",
          orphan.arch == "x86_64" and orphan.size_mib == 4096)
    check("inject_backend_only_vm: the VM appears in list_all()",
          orphan.vm_id in {h.vm_id for h in run_async(backend.list_all())})
    check("inject_backend_only_vm: get_state reflects the requested state",
          run_async(backend.get_state(orphan.vm_id)) == RUNNING)

    # custom vm_id + launched_at are honored
    orphan2 = backend.inject_backend_only_vm(vm_id="vm-custom-orphan",
                                             launched_at="2020-01-01T00:00:00+00:00")
    check("inject_backend_only_vm: a custom vm_id is honored",
          orphan2.vm_id == "vm-custom-orphan")
    check("inject_backend_only_vm: a custom launched_at is honored",
          orphan2.launched_at == "2020-01-01T00:00:00+00:00")


# ── Section 6: get_state/list_all on unknown vm_id ──────────────────────────
def test_unknown_vm_handling():
    backend = FakeVmBackend()
    check("get_state on an unknown vm_id returns TERMINATED (never raises)",
          run_async(backend.get_state("vm-does-not-exist")) == TERMINATED)
    check("list_all() on an empty backend returns an empty list",
          run_async(backend.list_all()) == [])


# ── Section 7: determinism — no unseeded randomness in fault behavior ──────
def test_deterministic_no_randomness():
    backend = FakeVmBackend()
    backend.fail_launch_times(3)
    results = []
    for _ in range(3):
        try:
            run_async(backend.launch("arm64", 2048, "s"))
            results.append("ok")
        except BackendError:
            results.append("fail")
    check("fail_launch_times(3) deterministically fails exactly 3 times in a row",
          results == ["fail", "fail", "fail"])
    vm = run_async(backend.launch("arm64", 2048, "s"))
    check("the 4th launch call deterministically succeeds", vm is not None)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== pool_manager.fake_backend self-test ===")
    print("\n-- happy path + calls log --")
    test_happy_path_and_calls_log()
    print("\n-- fault toggles --")
    test_fault_toggles()
    print("\n-- quota exceeded on launch --")
    test_quota_exceeded()
    print("\n-- latency injection --")
    test_latency_injection()
    print("\n-- force_state + inject_backend_only_vm --")
    test_force_state_and_inject_backend_only_vm()
    print("\n-- unknown vm_id handling --")
    test_unknown_vm_handling()
    print("\n-- deterministic fault behavior (no randomness) --")
    test_deterministic_no_randomness()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL FAKE BACKEND CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_fake_backend():
    _checks.clear()
    for fn in (test_happy_path_and_calls_log, test_fault_toggles, test_quota_exceeded,
               test_latency_injection, test_force_state_and_inject_backend_only_vm,
               test_unknown_vm_handling, test_deterministic_no_randomness):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "fake backend failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
