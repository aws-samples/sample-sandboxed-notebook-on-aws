#!/usr/bin/env python3
# test-kind: offline
"""
POST /resume wait-for-RUNNING schedule (restore-speedups FEAT-005, site 13 of
.agents/tasks/resume-wait-history/report.md: SAFE).

The route used to sleep 5 s before each of 6 GetMicrovm reads. It now uses the
shared schedule in proxy/platform/resume_wait.py: read at 0.3 s, then every
0.25 s until 3 s, then every 1.0 s, last read at the unchanged 30 s budget;
after a failed read the delay doubles, capped at 2 s. Checks:
  - the schedule values of resume_poll_delay;
  - RUNNING on the 2nd read is returned at 0.55 s of fake time;
  - an always-PENDING VM returns "resuming" after exactly 30 s (39 reads);
  - a GetMicrovm error still fails the route with 502 (unchanged);
  - a ResumeMicrovm error still returns 502 before any read (unchanged).
Fake clock + fake sleep, fake vm_manager. No AWS, no network, no real sleeps.
Run:
    .venv/bin/python tests/proxy/test_resume_route_wait_schedule.py
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
    def __init__(self, ft, states, resume_exc=None):
        self._ft = ft
        self._states = list(states)  # per read: a state string or an exception
        self.read_times = []
        self.resume_calls = []
        self._resume_exc = resume_exc

    async def get_session_vm_async(self, session_id):
        return {"vm_id": "vm-r1", "endpoint": "vm-r1.ep"}

    async def resume_vm(self, microvm_id):
        self.resume_calls.append(microvm_id)
        if self._resume_exc:
            raise self._resume_exc

    async def get_microvm_detail(self, microvm_id):
        self.read_times.append(self._ft.now - 1000.0)
        item = self._states.pop(0) if self._states else "PENDING"
        if isinstance(item, BaseException):
            raise item
        return {"state": item}


class _Req:
    def __init__(self, vm_manager):
        self.headers = {"X-Session-Id": "sess-resume-wait"}

        class _S:
            pass
        self.app = _S()
        self.app.state = _S()
        self.app.state.vm_manager = vm_manager


def _run(states, resume_exc=None):
    ft = _FakeTime()
    vm = _VmManager(ft, states, resume_exc)
    orig = rw._clock, rw._sleep
    rw._clock, rw._sleep = ft.clock, ft.sleep
    try:
        result = asyncio.run(microvm_mod.resume_session(_Req(vm)))
    finally:
        rw._clock, rw._sleep = orig
    return result, vm, ft


def _status(result):
    return getattr(result, "status_code", 200 if isinstance(result, dict) else None)


def test_schedule_values():
    d = rw.resume_poll_delay
    check("read 1 waits 0.3 s", d(1, 0.0) == 0.3)
    check("read 2 waits 0.25 s", d(2, 0.3) == 0.25)
    check("still 0.25 s at 2.99 s", d(12, 2.99) == 0.25)
    check("1.0 s from 3 s on", d(13, 3.0) == 1.0 and d(30, 20.0) == 1.0)
    check("failed read doubles", d(3, 0.8, 0.25, True) == 0.5)
    check("failed read never below the normal delay", d(20, 5.0, 0.1, True) == 1.0)
    check("failed doubling caps at 2.0 s", d(20, 5.0, 1.5, True) == 2.0)
    check("good read ignores prev_delay", d(20, 5.0, 2.0, False) == 1.0)
    check("route budget unchanged at 30 s", microvm_mod.RESUME_ROUTE_WAIT_BUDGET_SEC == 30.0)


def test_running_on_second_read():
    r, vm, ft = _run(["PENDING", "RUNNING"])
    check("running returned", isinstance(r, dict) and r.get("status") == "running", r)
    check("resume called once", vm.resume_calls == ["vm-r1"], vm.resume_calls)
    check("reads at 0.3, 0.55 s", len(vm.read_times) == 2 and _close(vm.read_times[0], 0.3)
          and _close(vm.read_times[1], 0.55), vm.read_times)
    check("no 5 s sleep", 5 not in ft.sleeps, ft.sleeps)


def test_budget_unchanged():
    r, vm, ft = _run([])  # PENDING forever
    elapsed = ft.now - 1000.0
    check("never RUNNING: 'resuming'", isinstance(r, dict) and r.get("status") == "resuming", r)
    check("waited exactly 30 s", _close(elapsed, 30.0), elapsed)
    check("last read at the 30 s budget", _close(vm.read_times[-1], 30.0), vm.read_times[-1])
    check("39 reads (12 fast, 27 slow)", len(vm.read_times) == 39, len(vm.read_times))
    check("fast steps until 3 s, then 1 s",
          all(_close(b - a, 0.25) for a, b in zip(vm.read_times[:12], vm.read_times[1:12]))
          and _close(vm.read_times[12] - vm.read_times[11], 1.0), vm.read_times[:14])


def test_errors_unchanged():
    r, vm, ft = _run(["PENDING", RuntimeError("GetMicrovm boom")])
    check("poll error: 502", _status(r) == 502, r)
    check("poll error: no retry after it", len(vm.read_times) == 2, vm.read_times)
    r, vm, ft = _run(["RUNNING"], resume_exc=RuntimeError("must be SUSPENDED"))
    check("resume error: 502", _status(r) == 502, r)
    check("resume error: no reads, no sleeps", vm.read_times == [] and ft.sleeps == [], (vm.read_times, ft.sleeps))


_SECTIONS = (test_schedule_values, test_running_on_second_read, test_budget_unchanged, test_errors_unchanged)

if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
