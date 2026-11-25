#!/usr/bin/env python3
# test-kind: offline
"""
_checkpoint_session_vm wait schedule + /health guard (restore-speedups FEAT-005,
site 15 of .agents/tasks/resume-wait-history/report.md: SAFE ONLY WITH A GUARD).

The save path used to sleep 2 s before each of 12 state reads. It now uses the
shared schedule (proxy/platform/resume_wait.py) with the unchanged 24 s budget,
and, only when this call woke the VM, probes GET /health (same auth header as
the save, every 0.25 s for up to 2 s) before the DAG flush and the save POST.
Checks:
  - SUSPENDED: resume, RUNNING on the 2nd read at 0.55 s, /health 200 at once,
    then flush, then POST /checkpoint-save; the probe carries the auth header;
  - /health 502, 502, 200: 3 probes 0.25 s apart, then the save;
  - /health never 200 (502 or exceptions): ~2 s of probing, then the save
    still goes out and succeeds;
  - RUNNING on the first read: no resume, no wait, no probe (unchanged);
  - never RUNNING: exactly 24 s of waiting, no probe, the save still goes out
    (unchanged); a dead state during the wait does not end it early (unchanged);
  - dead on the first read: no wait, no save (unchanged).
Fake clock + fake sleep, fake vm_manager, spy VM HTTP client and DAG flush.
No AWS, no network.
Run:
    .venv/bin/python tests/proxy/test_checkpoint_wait_schedule.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import proxy.platform.resume_wait as rw  # noqa: E402
import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from proxy.notebook import dag_service  # noqa: E402
from proxy.platform import http_clients  # noqa: E402

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
        self._first_done = False
        self.read_times = []
        self.resume_calls = []

    async def get_vm_state(self, microvm_id):
        if not self._first_done:
            self._first_done = True
            return self._first
        self.read_times.append(self._ft.now - 1000.0)
        return self._states.pop(0) if self._states else self._first

    async def resume_vm(self, microvm_id):
        self.resume_calls.append(microvm_id)

    async def get_auth_token(self, microvm_id):
        return "tok-ckpt"


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.headers = {}

    def json(self):
        return self._body


class _SpyClient:
    def __init__(self, ft, health):
        self._ft = ft
        self._health = list(health)  # per GET /health: status int or exception
        self.events = []  # (kind, t, extra)

    async def get(self, url, headers=None, timeout=None):
        self.events.append(("GET", self._ft.now - 1000.0, {"url": url, "headers": headers, "timeout": timeout}))
        item = self._health.pop(0) if self._health else 502
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)

    async def post(self, url, headers=None, content=None, timeout=None):
        self.events.append(("POST", self._ft.now - 1000.0, {"url": url}))
        return _Resp(200, {"saved": True})


def _run(first, states, health=()):
    ft = _FakeTime()
    vm = _VmManager(ft, first, states)
    client = _SpyClient(ft, health)
    orig = (rw._clock, rw._sleep, dag_service.flush, dag_service.use_vm_manager, http_clients.vm_client,
            os.environ.get("SESSION_PERSISTENCE_MODE"))

    async def _flush(session_id, vm_id=None, endpoint=None, timeout=None):
        client.events.append(("FLUSH", ft.now - 1000.0, {}))
        return True
    rw._clock, rw._sleep = ft.clock, ft.sleep
    dag_service.flush = _flush
    dag_service.use_vm_manager = lambda m: None
    http_clients.vm_client = lambda: client
    os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
    try:
        res = asyncio.run(microvm_mod._checkpoint_session_vm(vm, "vm-c1", "vm-c1.ep", "sess-ckpt-wait"))
    finally:
        (rw._clock, rw._sleep, dag_service.flush, dag_service.use_vm_manager, http_clients.vm_client) = orig[:5]
        if orig[5] is None:
            os.environ.pop("SESSION_PERSISTENCE_MODE", None)
        else:
            os.environ["SESSION_PERSISTENCE_MODE"] = orig[5]
    return res, vm, ft, client


def _kinds(client):
    return [e[0] for e in client.events]


def test_woke_and_healthy():
    res, vm, ft, c = _run("SUSPENDED", ["PENDING", "RUNNING"], [200])
    check("save succeeded", res.get("success") is True, res)
    check("resume once", vm.resume_calls == ["vm-c1"], vm.resume_calls)
    check("reads at 0.3, 0.55 s", len(vm.read_times) == 2 and _close(vm.read_times[0], 0.3)
          and _close(vm.read_times[1], 0.55), vm.read_times)
    check("order: GET /health, FLUSH, POST", _kinds(c) == ["GET", "FLUSH", "POST"], _kinds(c))
    g = c.events[0][2]
    check("probe hits /health with the save's auth header",
          g["url"] == "https://vm-c1.ep/health" and g["headers"] == {"X-aws-proxy-auth": "tok-ckpt"}, g)
    check("probe per-try timeout <= 1 s", g["timeout"] is not None and g["timeout"] <= 1.0, g["timeout"])
    check("save sent at 0.55 s", _close(c.events[-1][1], 0.55), c.events[-1][1])
    check("budget unchanged at 24 s", microvm_mod.CHECKPOINT_RESUME_WAIT_BUDGET_SEC == 24.0)


def test_health_retries():
    res, vm, ft, c = _run("SUSPENDED", ["RUNNING"], [502, 502, 200])
    gets = [e[1] for e in c.events if e[0] == "GET"]
    check("3 probes", len(gets) == 3, gets)
    check("probes 0.25 s apart", _close(gets[1] - gets[0], 0.25) and _close(gets[2] - gets[1], 0.25), gets)
    check("then FLUSH + POST", _kinds(c)[-2:] == ["FLUSH", "POST"], _kinds(c))
    check("save succeeded", res.get("success") is True, res)


def test_health_never_ok():
    for label, health in (("502 forever", [502] * 100), ("exceptions forever", [RuntimeError("conn")] * 100)):
        res, vm, ft, c = _run("SUSPENDED", ["RUNNING"], health)
        gets = [e[1] for e in c.events if e[0] == "GET"]
        probe_span = gets[-1] - gets[0]
        check(f"{label}: probing bounded to ~2 s", 1.5 <= probe_span <= 2.0 + 1e-9 and len(gets) <= 9,
              (probe_span, len(gets)))
        check(f"{label}: save still sent and succeeds",
              _kinds(c)[-2:] == ["FLUSH", "POST"] and res.get("success") is True, (_kinds(c), res))
        check(f"{label}: save after ~2 s of probing", _close(c.events[-1][1], 0.3 + 2.0), c.events[-1][1])


def test_running_first_read():
    res, vm, ft, c = _run("RUNNING", [], [200])
    check("RUNNING first: no resume, no sleep", vm.resume_calls == [] and ft.sleeps == [], (vm.resume_calls, ft.sleeps))
    check("RUNNING first: no probe (guard only after a wake)", _kinds(c) == ["FLUSH", "POST"], _kinds(c))


def test_never_running_and_dead():
    res, vm, ft, c = _run("SUSPENDED", [], [200])
    check("never RUNNING: waited exactly 24 s", _close(ft.now - 1000.0, 24.0), ft.now - 1000.0)
    check("never RUNNING: 33 reads", len(vm.read_times) == 33, len(vm.read_times))
    check("never RUNNING: no probe, save still forwarded", _kinds(c) == ["FLUSH", "POST"], _kinds(c))

    res, vm, ft, c = _run("SUSPENDED", ["PENDING", "TERMINATED"], [200])
    check("dead during wait: loop does not exit early (unchanged)", _close(ft.now - 1000.0, 24.0),
          ft.now - 1000.0)

    res, vm, ft, c = _run("TERMINATED", [], [200])
    check("dead first read: no wait, no save", res.get("success") is False and c.events == [] and ft.sleeps == [],
          (res, c.events, ft.sleeps))


_SECTIONS = (test_woke_and_healthy, test_health_retries, test_health_never_ok, test_running_first_read,
             test_never_running_and_dead)

if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
