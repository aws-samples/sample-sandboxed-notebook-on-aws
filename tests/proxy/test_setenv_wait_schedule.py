#!/usr/bin/env python3
# test-kind: offline
"""
set-env _resume_and_wait schedule + one-wait-per-VM guard (restore-speedups
FEAT-005, site 16 of .agents/tasks/resume-wait-history/report.md: SAFE ONLY
WITH A GUARD).

The set-env wake used to sleep 2 s before each of 15 state reads. It now uses
the shared schedule (proxy/platform/resume_wait.py) with the unchanged 30 s
budget, and concurrent calls for the same microvm_id share one resume + wait
(a .env import sends one set-env per key in parallel). Checks:
  - SUSPENDED: RUNNING on the 2nd read noticed at 0.55 s;
  - a dead state during the wait is returned (unchanged);
  - never RUNNING: returns the first state after exactly 30 s (unchanged);
  - RUNNING / UNKNOWN / dead on the first read return at once (unchanged);
  - guard: 10 parallel calls on one VM -> 1 resume, one poll loop, all RUNNING;
    two different VMs get separate waits; a finished wait is not reused;
    a cancelled caller does not cancel the wait the others share;
    a failing wait raises to every caller and is not cached.
Fake clock + fake sleep, fake vm_manager. No AWS, no network.
Run:
    .venv/bin/python tests/proxy/test_setenv_wait_schedule.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import proxy.platform.resume_wait as rw  # noqa: E402
import proxy.platform.routes.microvm as microvm_mod  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {str(detail)[:400]}" if not cond else ""))
    if not cond:
        _fails.append(label)


def _close(a, b):
    return abs(a - b) < 1e-6


class _FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, s):
        self.sleeps.append(s)
        self.now += s
        await asyncio.sleep(0)


class _VmManager:
    """Per VM: first read returns `first`, later reads pop `states`."""

    def __init__(self, ft, first, states, raise_first=False):
        self._ft = ft
        self._first = first
        self._states = {}
        self._default = list(states)
        self._seen = set()
        self._raise_first = raise_first
        self.read_times = {}
        self.resume_calls = []

    async def get_vm_state(self, microvm_id):
        if microvm_id not in self._seen:
            self._seen.add(microvm_id)
            await asyncio.sleep(0)  # let parallel callers pile up
            if self._raise_first:
                raise RuntimeError("state read crashed")
            return self._first
        self.read_times.setdefault(microvm_id, []).append(self._ft.now - 1000.0)
        q = self._states.setdefault(microvm_id, list(self._default))
        return q.pop(0) if q else self._first

    async def resume_vm(self, microvm_id):
        self.resume_calls.append(microvm_id)


def _with_fake_time(ft, coro_fn):
    orig = rw._clock, rw._sleep
    rw._clock, rw._sleep = ft.clock, ft.sleep
    try:
        return asyncio.run(coro_fn())
    finally:
        rw._clock, rw._sleep = orig


def _one(first, states):
    ft = _FakeTime()
    vm = _VmManager(ft, first, states)
    st = _with_fake_time(ft, lambda: microvm_mod._resume_and_wait(vm, "vm-s1"))
    return st, vm, ft


def test_schedule():
    st, vm, ft = _one("SUSPENDED", ["PENDING", "RUNNING"])
    reads = vm.read_times.get("vm-s1", [])
    check("RUNNING returned", st == "RUNNING", st)
    check("resume once", vm.resume_calls == ["vm-s1"], vm.resume_calls)
    check("reads at 0.3, 0.55 s", len(reads) == 2 and _close(reads[0], 0.3) and _close(reads[1], 0.55), reads)
    check("budget unchanged at 30 s", microvm_mod._SETENV_RESUME_WAIT_BUDGET_SEC == 30.0)

    st, vm, ft = _one("SUSPENDED", ["PENDING", "TERMINATED", "RUNNING"])
    check("dead during wait is returned", st == "TERMINATED", st)

    st, vm, ft = _one("SUSPENDED", [])  # stays SUSPENDED
    check("never RUNNING: first state returned", st == "SUSPENDED", st)
    check("never RUNNING: exactly 30 s", _close(ft.now - 1000.0, 30.0), ft.now - 1000.0)

    for first in ("RUNNING", "UNKNOWN", "TERMINATED", "NOT_FOUND"):
        st, vm, ft = _one(first, [])
        check(f"{first} first read: returned at once", st == first and ft.sleeps == [] and vm.resume_calls == [],
              (st, ft.sleeps))


def test_single_flight_guard():
    ft = _FakeTime()
    vm = _VmManager(ft, "SUSPENDED", ["PENDING", "RUNNING"])

    async def go():
        return await asyncio.gather(*[microvm_mod._resume_and_wait(vm, "vm-s1") for _ in range(10)])
    res = _with_fake_time(ft, go)
    check("10 parallel calls all RUNNING", res == ["RUNNING"] * 10, res)
    check("one resume for the VM", vm.resume_calls == ["vm-s1"], vm.resume_calls)
    check("one poll loop (2 reads, not 20)", len(vm.read_times.get("vm-s1", [])) == 2, vm.read_times)
    check("no wait left in flight", microvm_mod._setenv_wakes == {}, microvm_mod._setenv_wakes)

    ft = _FakeTime()
    vm = _VmManager(ft, "SUSPENDED", ["RUNNING"])

    async def two_vms():
        return await asyncio.gather(microvm_mod._resume_and_wait(vm, "vm-a"),
                                    microvm_mod._resume_and_wait(vm, "vm-b"),
                                    microvm_mod._resume_and_wait(vm, "vm-a"))
    res = _with_fake_time(ft, two_vms)
    check("two VMs: separate waits", sorted(vm.resume_calls) == ["vm-a", "vm-b"] and res == ["RUNNING"] * 3,
          (vm.resume_calls, res))

    ft = _FakeTime()
    vm = _VmManager(ft, "SUSPENDED", ["RUNNING"])

    async def sequential():
        a = await microvm_mod._resume_and_wait(vm, "vm-s1")
        vm._seen.clear()  # next call reads the first state and the queue again
        vm._states.clear()
        b = await microvm_mod._resume_and_wait(vm, "vm-s1")
        return a, b
    res = _with_fake_time(ft, sequential)
    check("finished wait not reused: 2 resumes", vm.resume_calls == ["vm-s1", "vm-s1"] and res == ("RUNNING",) * 2,
          (vm.resume_calls, res))

    ft = _FakeTime()
    vm = _VmManager(ft, "SUSPENDED", ["PENDING", "PENDING", "RUNNING"])

    async def cancel_one():
        t1 = asyncio.ensure_future(microvm_mod._resume_and_wait(vm, "vm-s1"))
        t2 = asyncio.ensure_future(microvm_mod._resume_and_wait(vm, "vm-s1"))
        for _ in range(3):
            await asyncio.sleep(0)
        t1.cancel()
        r2 = await t2
        return t1.cancelled(), r2
    c1, r2 = _with_fake_time(ft, cancel_one)
    check("cancelled caller does not cancel the shared wait", c1 is True and r2 == "RUNNING", (c1, r2))
    check("still one resume", vm.resume_calls == ["vm-s1"], vm.resume_calls)

    ft = _FakeTime()
    vm = _VmManager(ft, "SUSPENDED", [], raise_first=True)
    st = _with_fake_time(ft, lambda: microvm_mod._resume_and_wait(vm, "vm-s1"))
    check("state-read crash: UNKNOWN, no wait (unchanged)", st == "UNKNOWN" and ft.sleeps == [], (st, ft.sleeps))

    # The wrapped wait itself never raises today; if it ever does, every caller
    # gets the error and nothing stays cached.
    orig_once = microvm_mod._resume_and_wait_once
    calls = []

    async def _boom(vm_manager, microvm_id):
        calls.append(microvm_id)
        await asyncio.sleep(0)
        raise RuntimeError("wake crashed")
    microvm_mod._resume_and_wait_once = _boom
    try:
        async def failing():
            return await asyncio.gather(*[microvm_mod._resume_and_wait(vm, "vm-s1") for _ in range(3)],
                                        return_exceptions=True)
        res = _with_fake_time(_FakeTime(), failing)
    finally:
        microvm_mod._resume_and_wait_once = orig_once
    check("failing wait raises to every caller", all(isinstance(r, RuntimeError) for r in res), res)
    check("failing wait ran once", calls == ["vm-s1"], calls)
    check("failing wait not cached", microvm_mod._setenv_wakes == {}, microvm_mod._setenv_wakes)


_SECTIONS = (test_schedule, test_single_flight_guard)

if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
