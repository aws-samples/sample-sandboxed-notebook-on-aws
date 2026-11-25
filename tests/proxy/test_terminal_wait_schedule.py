#!/usr/bin/env python3
# test-kind: offline
"""
Terminal _ensure_vm_awake wait schedule (restore-speedups FEAT-005, site 14 of
.agents/tasks/resume-wait-history/report.md: SAFE).

The terminal used to sleep 2 s before each of 15 state reads. It now uses the
shared schedule (proxy/platform/resume_wait.py) with the unchanged 30 s budget.
Checks:
  - SUSPENDED: resume once, RUNNING on the 2nd read noticed at 0.55 s -> True;
  - a dead state during the wait -> False (unchanged);
  - never RUNNING -> True ("attach anyway") after exactly 30 s (unchanged);
  - a read exception / UNKNOWN backs off (doubling, capped at 2 s) and keeps going;
  - RUNNING on the first read: no resume, no sleep, no status line;
  - dead on the first read -> False without waiting.
Fake clock + fake sleep, fake vm_manager and WebSocket. No AWS, no network.
Run:
    .venv/bin/python tests/proxy/test_terminal_wait_schedule.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import proxy.platform.resume_wait as rw  # noqa: E402
import proxy.platform.routes.terminal as term  # noqa: E402

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
    def __init__(self, ft, first, states):
        self._ft = ft
        self._first = first
        self._states = list(states)
        self.read_times = []
        self.resume_calls = []
        self._first_done = False

    async def get_vm_state(self, microvm_id):
        if not self._first_done:
            self._first_done = True
            return self._first
        self.read_times.append(self._ft.now - 1000.0)
        item = self._states.pop(0) if self._states else "SUSPENDED"
        if isinstance(item, BaseException):
            raise item
        return item

    async def resume_vm(self, microvm_id):
        self.resume_calls.append(microvm_id)


class _Ws:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)


def _run(first, states):
    ft = _FakeTime()
    vm = _VmManager(ft, first, states)
    ws = _Ws()
    orig = rw._clock, rw._sleep
    rw._clock, rw._sleep = ft.clock, ft.sleep
    try:
        ok = asyncio.run(term._ensure_vm_awake(vm, "vm-t1", ws))
    finally:
        rw._clock, rw._sleep = orig
    return ok, vm, ft, ws


def test_running_on_second_read():
    ok, vm, ft, ws = _run("SUSPENDED", ["PENDING", "RUNNING"])
    check("awake -> True", ok is True)
    check("resume called once", vm.resume_calls == ["vm-t1"], vm.resume_calls)
    check("reads at 0.3, 0.55 s", len(vm.read_times) == 2 and _close(vm.read_times[0], 0.3)
          and _close(vm.read_times[1], 0.55), vm.read_times)
    check("waking line streamed", any("Waking" in s for s in ws.sent), ws.sent)
    check("budget unchanged at 30 s", term.RESUME_WAIT_BUDGET_SEC == 30.0)


def test_dead_exit():
    for dead in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        ok, vm, ft, ws = _run("SUSPENDED", ["PENDING", dead, "RUNNING"])
        check(f"{dead} during wait -> False", ok is False)
        check(f"{dead}: stops at that read", len(vm.read_times) == 2, vm.read_times)
    ok, vm, ft, ws = _run("TERMINATED", [])
    check("dead first read -> False, no wait", ok is False and ft.sleeps == [] and vm.resume_calls == [])


def test_budget_attach_anyway():
    ok, vm, ft, ws = _run("SUSPENDED", [])
    check("never RUNNING -> True (attach anyway)", ok is True)
    check("waited exactly 30 s", _close(ft.now - 1000.0, 30.0), ft.now - 1000.0)
    check("39 reads", len(vm.read_times) == 39, len(vm.read_times))


def test_error_backoff():
    ok, vm, ft, ws = _run("SUSPENDED", [RuntimeError("x"), "UNKNOWN", "RUNNING"])
    check("errors then RUNNING -> True", ok is True)
    check("backoff sleeps 0.3, 0.6, 1.2", len(ft.sleeps) == 3 and _close(ft.sleeps[0], 0.3)
          and _close(ft.sleeps[1], 0.6) and _close(ft.sleeps[2], 1.2), ft.sleeps)
    ok, vm, ft, ws = _run("SUSPENDED", [RuntimeError("x")] * 100)
    check("errors forever: sleeps capped at 2 s", max(ft.sleeps) <= 2.0 + 1e-9, max(ft.sleeps))
    check("errors forever: still bounded by 30 s, True", ok is True and _close(ft.now - 1000.0, 30.0),
          ft.now - 1000.0)


def test_running_first_read():
    ok, vm, ft, ws = _run("RUNNING", [])
    check("RUNNING first: True, no resume, no sleep, no line",
          ok is True and vm.resume_calls == [] and ft.sleeps == [] and ws.sent == [])


_SECTIONS = (test_running_on_second_read, test_dead_exit, test_budget_attach_anyway,
             test_error_backoff, test_running_first_read)

if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
