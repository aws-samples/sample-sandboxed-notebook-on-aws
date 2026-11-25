#!/usr/bin/env python3
# test-kind: offline
"""
SUSPENDING-never-resumed fix (followups-batch part 1; resume-wait-history
report section 4). The shared wake (proxy/platform/resume_wait.wake_and_wait)
used by terminal _ensure_vm_awake (site 14), _checkpoint_session_vm (15) and
set-env _resume_and_wait (16) resumes once a poll reads SUSPENDED when no
resume of this call has succeeded yet.

Checks:
  - SUSPENDING first read, then SUSPENDED: exactly one resume, RUNNING soon after;
  - a stale SUSPENDED right after our successful resume: no second resume;
  - our resume raising: retried on a later SUSPENDED read, >= 1 s after the try;
  - a resume that keeps raising: at most 3 in-loop tries, >= 1 s apart;
  - RESUMING / PENDING never trigger a resume;
  - SUSPENDED first read: one resume as before;
  - budgets unchanged (30 / 24 / 30 s, 39 reads over 30 s);
  - per site: terminal, checkpoint (save still sent), set-env 10 parallel keys
    on a SUSPENDING VM share one resume.
Fake clock + fake sleep, fake vm_manager. No AWS, no network.

Run:
    .venv/bin/python tests/proxy/test_resume_suspending_reissue.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import proxy.platform.resume_wait as rw  # noqa: E402
import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
import proxy.platform.routes.terminal as term  # noqa: E402
from proxy.notebook import dag_service  # noqa: E402
from proxy.platform import http_clients  # noqa: E402

_fails = []
DEAD = ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND")


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


class _Vm:
    """First get_vm_state returns `first`; later reads pop `states` (then repeat
    `tail`). resume_vm pops `resume_results` (an exception raises; default ok).
    A successful resume switches later reads to `after_resume` when given."""

    def __init__(self, ft, first, states, tail=None, resume_results=(), after_resume=None):
        self._ft = ft
        self._first = first
        self._first_done = False
        self._states = list(states)
        self._tail = tail if tail is not None else first
        self._resume_results = list(resume_results)
        self._after_resume = after_resume
        self.read_times = []
        self.resume_times = []
        self.resume_ok_times = []

    def t(self):
        return round(self._ft.now - 1000.0, 6)

    async def get_vm_state(self, microvm_id):
        if not self._first_done:
            self._first_done = True
            await asyncio.sleep(0)  # let parallel callers pile up
            return self._first
        self.read_times.append(self.t())
        if self._after_resume is not None and self.resume_ok_times:
            q = self._after_resume
            return q.pop(0) if len(q) > 1 else q[0]
        return self._states.pop(0) if self._states else self._tail

    async def resume_vm(self, microvm_id):
        self.resume_times.append(self.t())
        item = self._resume_results.pop(0) if self._resume_results else None
        if isinstance(item, BaseException):
            raise item
        self.resume_ok_times.append(self.t())

    async def get_auth_token(self, microvm_id):
        return "tok-reissue"


def _fake(ft, coro_fn):
    orig = rw._clock, rw._sleep
    rw._clock, rw._sleep = ft.clock, ft.sleep
    try:
        return asyncio.run(coro_fn())
    finally:
        rw._clock, rw._sleep = orig


def _wake(vm, budget=30.0, retry_errors=True):
    async def go():
        first = await vm.get_vm_state("vm-x")
        return await rw.wake_and_wait(first, lambda: vm.get_vm_state("vm-x"),
                                      lambda: vm.resume_vm("vm-x"), budget,
                                      dead_states=DEAD, retry_errors=retry_errors, vm_id="vm-x",
                                      who="test")
    return go


def test_suspending_then_suspended():
    ft = _FakeTime()
    # SUSPENDING at 0.3, SUSPENDED at 0.55 (resume here), stale SUSPENDED at
    # 0.8 and 1.05 (no re-issue), PENDING, RUNNING.
    vm = _Vm(ft, "SUSPENDING", ["SUSPENDING", "SUSPENDED", "SUSPENDED", "SUSPENDED", "PENDING", "RUNNING"])
    st = _fake(ft, _wake(vm))
    check("SUSPENDING->SUSPENDED: RUNNING", st == "RUNNING", st)
    check("exactly one resume, at the first SUSPENDED read (0.55 s)",
          vm.resume_times == [0.55], vm.resume_times)
    check("stale SUSPENDED after our resume: no second resume", len(vm.resume_times) == 1, vm.resume_times)
    check("finished in about 1.55 s, not 30 s", _close(ft.now - 1000.0, 1.55), ft.now - 1000.0)

    # Stale SUSPENDED for 3 s after a successful in-loop resume: still one resume.
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDING", ["SUSPENDED"] + ["SUSPENDED"] * 12 + ["RUNNING"])
    st = _fake(ft, _wake(vm))
    check("long stale SUSPENDED: one resume only", st == "RUNNING" and vm.resume_times == [0.3],
          (st, vm.resume_times))


def test_suspended_first_unchanged():
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDED", ["SUSPENDED", "PENDING", "RUNNING"])
    st = _fake(ft, _wake(vm))
    check("SUSPENDED first: one resume before the wait", st == "RUNNING" and vm.resume_times == [0.0],
          (st, vm.resume_times))
    check("SUSPENDED first: stale SUSPENDED at 0.3 s not re-resumed", vm.read_times[0] == 0.3)
    check("SUSPENDED first: reads 0.3, 0.55, 0.8", vm.read_times == [0.3, 0.55, 0.8], vm.read_times)


def test_resume_raises_then_retried():
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDED", ["SUSPENDED"] * 6, tail="SUSPENDED",
             resume_results=[RuntimeError("ValidationException: must be in SUSPENDED")],
             after_resume=["PENDING", "RUNNING"])
    st = _fake(ft, _wake(vm))
    check("raised resume retried: RUNNING", st == "RUNNING", st)
    check("two resume calls (0.0 failed, retry at 1.05 s)", vm.resume_times == [0.0, 1.05], vm.resume_times)
    check("one success", vm.resume_ok_times == [1.05], vm.resume_ok_times)

    # In-loop resume that always raises: at most 3 in-loop tries, >= 1 s apart,
    # and the 30 s budget is unchanged.
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDING", [], tail="SUSPENDED", resume_results=[RuntimeError("throttled")] * 10)
    st = _fake(ft, _wake(vm))
    gaps = [b - a for a, b in zip(vm.resume_times, vm.resume_times[1:])]
    check("always raising: 3 tries", len(vm.resume_times) == 3, vm.resume_times)
    check("always raising: tries >= 1 s apart", all(g >= 1.0 - 1e-6 for g in gaps), gaps)
    check("always raising: ends at the budget (30 s), SUSPENDED",
          st == "SUSPENDED" and _close(ft.now - 1000.0, 30.0), (st, ft.now - 1000.0))
    check("always raising: 39 reads as before", len(vm.read_times) == 39, len(vm.read_times))

    # The checkpoint site's wait does not retry read errors; a raising resume
    # must not turn into a raised wait.
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDING", ["SUSPENDED", "RUNNING"], resume_results=[RuntimeError("x")])
    st = _fake(ft, _wake(vm, budget=24.0, retry_errors=False))
    check("raising resume never propagates out of the wait", st == "RUNNING", st)


def test_resuming_pending_never_resume():
    for first in ("RESUMING", "PENDING"):
        ft = _FakeTime()
        vm = _Vm(ft, first, [first, "PENDING", "PENDING", "RUNNING"])
        st = _fake(ft, _wake(vm))
        check(f"{first} first: RUNNING, no resume", st == "RUNNING" and vm.resume_times == [],
              (st, vm.resume_times))
        ft = _FakeTime()
        vm = _Vm(ft, first, [], tail=first)
        st = _fake(ft, _wake(vm))
        check(f"{first} forever: no resume, 30 s", vm.resume_times == [] and _close(ft.now - 1000.0, 30.0),
              (vm.resume_times, ft.now - 1000.0))


def test_no_resume_without_callable():
    # wait_until_running without `resume` (POST /resume, cold paths) is unchanged.
    ft = _FakeTime()
    reads = []

    async def read():
        reads.append(ft.now)
        return "SUSPENDED"
    st = _fake(ft, lambda: rw.wait_until_running(read, 30.0))
    check("plain wait: SUSPENDED for 30 s, 39 reads", st == "SUSPENDED" and len(reads) == 39, (st, len(reads)))


def test_budgets_unchanged():
    check("terminal 30 s", term.RESUME_WAIT_BUDGET_SEC == 30.0)
    check("checkpoint 24 s", microvm_mod.CHECKPOINT_RESUME_WAIT_BUDGET_SEC == 24.0)
    check("set-env 30 s", microvm_mod._SETENV_RESUME_WAIT_BUDGET_SEC == 30.0)
    check("/resume 30 s", microvm_mod.RESUME_ROUTE_WAIT_BUDGET_SEC == 30.0)
    # SUSPENDING then SUSPENDED forever, resume succeeds but the VM never moves:
    # one resume, full budget, then the site's fallback.
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDING", [], tail="SUSPENDED")
    st = _fake(ft, _wake(vm))
    check("stuck SUSPENDED: one resume, full 30 s",
          vm.resume_times == [0.3] and _close(ft.now - 1000.0, 30.0) and st == "SUSPENDED",
          (vm.resume_times, ft.now - 1000.0, st))


class _Ws:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)


def test_terminal_site():
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDING", ["SUSPENDING", "SUSPENDED", "PENDING", "RUNNING"])
    ws = _Ws()
    ok = _fake(ft, lambda: term._ensure_vm_awake(vm, "vm-t", ws))
    check("terminal: True", ok is True)
    check("terminal: one resume at 0.55 s", vm.resume_times == [0.55], vm.resume_times)
    check("terminal: done at 1.05 s", _close(ft.now - 1000.0, 1.05), ft.now - 1000.0)
    check("terminal: waking line still streamed", any("Waking" in s for s in ws.sent), ws.sent)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.headers = {}

    def json(self):
        return self._body


class _Client:
    def __init__(self):
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append("GET")
        return _Resp(200)

    async def post(self, url, headers=None, content=None, timeout=None):
        self.calls.append("POST")
        return _Resp(200, {"saved": True})


def test_checkpoint_site():
    for first, results in (("SUSPENDING", []), ("SUSPENDED", [RuntimeError("raised")])):
        ft = _FakeTime()
        if results:
            vm = _Vm(ft, first, [], tail="SUSPENDED", resume_results=results, after_resume=["PENDING", "RUNNING"])
        else:
            vm = _Vm(ft, first, ["SUSPENDED", "PENDING", "RUNNING"])
        client = _Client()
        orig = (dag_service.flush, dag_service.use_vm_manager, http_clients.vm_client,
                os.environ.get("SESSION_PERSISTENCE_MODE"))

        async def _flush(session_id, vm_id=None, endpoint=None, timeout=None):
            return True
        dag_service.flush = _flush
        dag_service.use_vm_manager = lambda m: None
        http_clients.vm_client = lambda: client
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        try:
            res = _fake(ft, lambda: microvm_mod._checkpoint_session_vm(vm, "vm-c", "vm-c.ep", "sess-reissue"))
        finally:
            dag_service.flush, dag_service.use_vm_manager, http_clients.vm_client = orig[:3]
            if orig[3] is None:
                os.environ.pop("SESSION_PERSISTENCE_MODE", None)
            else:
                os.environ["SESSION_PERSISTENCE_MODE"] = orig[3]
        expect = [0.3] if not results else [0.0, 1.05]
        check(f"checkpoint {first}{' (resume raised)' if results else ''}: resumes {expect}",
              vm.resume_times == expect, vm.resume_times)
        check(f"checkpoint {first}: health probe then save sent", client.calls == ["GET", "POST"], client.calls)
        check(f"checkpoint {first}: success", res.get("success") is True, res)


def test_setenv_site_shared():
    ft = _FakeTime()
    vm = _Vm(ft, "SUSPENDING", ["SUSPENDED", "PENDING", "RUNNING"])

    async def go():
        return await asyncio.gather(*[microvm_mod._resume_and_wait(vm, "vm-s") for _ in range(10)])
    res = _fake(ft, go)
    check("set-env 10 keys on SUSPENDING VM: all RUNNING", res == ["RUNNING"] * 10, res)
    check("set-env: one shared resume", vm.resume_times == [0.3], vm.resume_times)
    check("set-env: one poll loop (3 reads)", len(vm.read_times) == 3, vm.read_times)
    check("set-env: nothing left in flight", microvm_mod._setenv_wakes == {}, microvm_mod._setenv_wakes)


_SECTIONS = (test_suspending_then_suspended, test_suspended_first_unchanged, test_resume_raises_then_retried,
             test_resuming_pending_never_resume, test_no_resume_without_callable, test_budgets_unchanged,
             test_terminal_site, test_checkpoint_site, test_setenv_site_shared)

if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
