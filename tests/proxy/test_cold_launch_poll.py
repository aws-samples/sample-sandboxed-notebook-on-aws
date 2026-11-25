#!/usr/bin/env python3
# test-kind: offline
"""
Cold-launch wait-for-RUNNING schedule (restore-time-breakdown fix 1).

launch_microvm's wait loop after a cold run_vm (VM not from the pool) polls
right away, then after 1.0 s, then every 0.5 s, within a 60 s elapsed budget and
at most COLD_LAUNCH_MAX_POLLS polls. Checks:
  - the schedule values (1.0, then 0.5 steady, throttle backoff capped at 2 s);
  - a VM RUNNING on the 3rd poll is noticed at 1.5 s of fake time;
  - the pool-grant path never enters the loop;
  - a dead state stops the wait, terminates and returns 502;
  - an always-PENDING VM times out after ~60 s of fake time: 502 + terminate;
  - throttling / connectivity poll errors back off instead of failing; any
    other poll error still fails the launch as before;
  - the explicit /resume route uses the shared resume schedule, not this one.
Fake clock + fake sleep, fake vm_manager (tests/pool/test_proxy_launch_grant).
No AWS, no network, no real sleeps.
Run:
    .venv/bin/python tests/proxy/test_cold_launch_poll.py
"""
import asyncio
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import botocore.exceptions as bce  # noqa: E402

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from tests.pool import test_proxy_launch_grant as tl  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {str(detail)[:400]}" if not cond else ""))
    if not cond:
        _fails.append(label)


class _FakeTime:
    """Fake monotonic clock; sleep() advances it instead of waiting."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now


class _VmManager(tl._FakeVmManager):
    def __init__(self, ft, states):
        super().__init__()
        self._ft = ft
        self._states = list(states)  # per poll: a state string or an exception
        self.poll_times = []
        self.terminated = []
        self.token_deleted = []
        outer = self

        class _TC:
            async def delete(self, vm_id):
                outer.token_deleted.append(vm_id)
        self.token_cache = _TC()

    async def get_microvm_detail_launch(self, microvm_id):
        self.poll_times.append(self._ft.now - 1000.0)
        item = self._states.pop(0) if self._states else "PENDING"
        if isinstance(item, BaseException):
            raise item
        return {"state": item}

    async def terminate_vm(self, microvm_id):
        self.terminated.append(microvm_id)


def _launch(states, pool_enabled=False, grant=None):
    ft = _FakeTime()
    vm = _VmManager(ft, states)
    with tl.EnvSandbox():
        os.environ["POOL_ENABLED"] = "true" if pool_enabled else "false"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        restore = tl._patch_module(vm, tl._FakeAStorage())
        tl._FakeAsyncClient.behavior = grant or (lambda url, json: tl._FakeResponse(409, {"error": "POOL_EMPTY"}))
        orig_sleep, orig_clock = asyncio.sleep, microvm_mod._cold_launch_clock

        async def _sleep(s, *a, **k):
            ft.sleeps.append(s)
            ft.now += s
            await orig_sleep(0)
        microvm_mod.asyncio.sleep = _sleep
        microvm_mod._cold_launch_clock = ft.clock
        try:
            result = asyncio.run(microvm_mod.launch_microvm(tl._build_request(vm, session_id="sess-cold-poll")))
        finally:
            microvm_mod.asyncio.sleep = orig_sleep
            microvm_mod._cold_launch_clock = orig_clock
            restore()
    return result, vm, ft


def _status(result):
    return getattr(result, "status_code", 200 if isinstance(result, dict) else None)


def test_schedule_values():
    d = microvm_mod.cold_launch_poll_delay
    check("delay after poll 1 is 1.0 s", d(1) == 1.0)
    check("delay after poll 2 is 0.5 s", d(2) == 0.5)
    check("steady delay stays 0.5 s", all(d(n) == 0.5 for n in range(3, 200)))
    check("throttled after poll 1: 1.0 s", d(1, 0.0, True) == 1.0)
    check("throttled doubles: 0.5 -> 1.0", d(3, 0.5, True) == 1.0)
    check("throttled doubles: 1.0 -> 2.0", d(3, 1.0, True) == 2.0)
    check("throttled is capped at 2.0 s", d(3, 2.0, True) == 2.0 and d(3, 8.0, True) == 2.0)
    check("back to 0.5 s after a good poll", d(4, 2.0, False) == 0.5)
    check("budget is 60 s", microvm_mod.COLD_LAUNCH_WAIT_BUDGET_SEC == 60.0)
    check("poll cap <= 130", 0 < microvm_mod.COLD_LAUNCH_MAX_POLLS <= 130)


def test_running_on_third_poll():
    r, vm, ft = _launch(["PENDING", "PENDING", "RUNNING"])
    check("RUNNING on poll 3: launch succeeds", isinstance(r, dict) and r.get("status") == "running", r)
    check("exactly 3 polls", len(vm.poll_times) == 3, vm.poll_times)
    check("polls at 0, 1.0, 1.5 s of fake time", vm.poll_times == [0.0, 1.0, 1.5], vm.poll_times)
    check("noticed within 1.5 s", (ft.now - 1000.0) <= 1.5 + 1e-9, ft.now - 1000.0)
    check("no 5 s sleep", 5 not in ft.sleeps, ft.sleeps)
    check("not terminated", vm.terminated == [])


def test_pool_grant_skips_loop():
    def _grant(url, json):
        return tl._FakeResponse(200, {"vm_id": "pool-vm-1", "endpoint": "pool-ep-1"})
    r, vm, ft = _launch(["PENDING"], pool_enabled=True, grant=_grant)
    check("pool grant: adopted VM returned", isinstance(r, dict) and r.get("microvmId") == "pool-vm-1", r)
    check("pool grant: run_vm not called", vm.run_vm_calls == [])
    check("pool grant: cold-launch poll never called", vm.poll_times == [], vm.poll_times)
    check("pool grant: no cold-launch sleeps", ft.sleeps == [], ft.sleeps)


def test_dead_state_exit():
    for dead in ("TERMINATED", "TERMINATING", "FAILED"):
        r, vm, ft = _launch(["PENDING", dead])
        check(f"{dead}: 502", _status(r) == 502, r)
        check(f"{dead}: stops after 2 polls", len(vm.poll_times) == 2, vm.poll_times)
        check(f"{dead}: VM terminated + token dropped",
              vm.terminated == ["cold-vm-1"] and vm.token_deleted == ["cold-vm-1"])


def test_timeout_after_budget():
    r, vm, ft = _launch([])  # PENDING forever
    elapsed = ft.now - 1000.0
    check("timeout: 502", _status(r) == 502, r)
    check("timeout: waited ~60 s of fake time", 59.5 <= elapsed <= 60.0 + 1e-9, elapsed)
    check("timeout: last poll at the deadline", abs(vm.poll_times[-1] - 60.0) < 1e-9, vm.poll_times[-1])
    check("timeout: poll count bounded", len(vm.poll_times) <= microvm_mod.COLD_LAUNCH_MAX_POLLS,
          len(vm.poll_times))
    check("timeout: VM terminated + token dropped",
          vm.terminated == ["cold-vm-1"] and vm.token_deleted == ["cold-vm-1"])


def test_transient_backoff():
    throttle = bce.ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetMicrovm")
    conn = bce.EndpointConnectionError(endpoint_url="https://lambda.example")
    r, vm, ft = _launch([throttle, conn, "RUNNING"])
    check("transient: launch still succeeds", isinstance(r, dict) and r.get("status") == "running", r)
    check("transient: 3 polls", len(vm.poll_times) == 3, vm.poll_times)
    check("transient: backoff sleeps 1.0 then 2.0", ft.sleeps == [1.0, 2.0], ft.sleeps)
    check("transient: not terminated", vm.terminated == [])

    r, vm, ft = _launch([throttle] * 500)
    check("throttled forever: 502 after budget", _status(r) == 502, r)
    check("throttled forever: sleeps capped at 2 s", max(ft.sleeps) <= 2.0, max(ft.sleeps))
    check("throttled forever: VM terminated", vm.terminated == ["cold-vm-1"])

    other = bce.ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetMicrovm")
    r, vm, ft = _launch([other, "RUNNING"])
    check("non-transient error: launch fails as before (502)", _status(r) == 502, r)
    check("non-transient error: no retry", len(vm.poll_times) == 1, vm.poll_times)


def test_resume_route_unchanged():
    src = inspect.getsource(microvm_mod.resume_session)
    check("/resume uses the shared resume schedule (FEAT-005)", "resume_wait.wait_until_running" in src)
    check("/resume keeps its 30 s budget", microvm_mod.RESUME_ROUTE_WAIT_BUDGET_SEC == 30.0)
    check("/resume does not use the cold-launch schedule", "cold_launch_poll_delay" not in src)
    launch_src = inspect.getsource(microvm_mod.launch_microvm)
    check("launch loop no longer sleeps a fixed 5 s", "asyncio.sleep(5)" not in launch_src)
    grant_src = inspect.getsource(microvm_mod._try_pool_grant)
    check("_try_pool_grant does not use the cold-launch schedule", "cold_launch_poll_delay" not in grant_src)


_SECTIONS = (test_schedule_values, test_running_on_third_poll, test_pool_grant_skips_loop,
             test_dead_state_exit, test_timeout_after_budget, test_transient_backoff,
             test_resume_route_unchanged)


def test_cold_launch_poll():
    _fails.clear()
    for fn in _SECTIONS:
        fn()
    assert not _fails, f"failed checks: {_fails}"


if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
