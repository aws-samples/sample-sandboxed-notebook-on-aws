#!/usr/bin/env python3
# test-kind: offline
"""
Pool grant resume schedule (restore-speedups FEAT-001, plan.md item 1).
Covers:
  - resume_poll_delay schedule: 0.3 s before poll 1, 0.2 s while under 3 s,
    0.5 s after, throttled doubling capped at 2.0 s.
  - AwsVmBackend.resume (botocore Stubber, fake clock/sleep): RUNNING on poll 3
    returns "RUNNING" after sleeps [0.3, 0.2, 0.2]; a dead state is returned;
    a throttled get_microvm doubles the next delay then succeeds; a
    non-throttle ClientError raises BackendError; the timeout raises; one INFO
    line with resume_call=/resume_poll=/polls=.
  - PoolManager.grant with FakeVmBackend: no get_state call, a dead resume
    still gives GrantError + terminate + row delete, the grant INFO timing
    line, and the WARNING when resume takes more than 3 s.
No AWS, no network, no real sleeps beyond the stubbed calls.
Run:
    .venv/bin/python tests/pool/test_pool_resume_schedule.py
"""
import asyncio
import logging
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from botocore.stub import Stubber  # noqa: E402
import pool_manager.aws_backend as aws_backend  # noqa: E402
import pool_manager.manager as manager_mod  # noqa: E402
from pool_manager.aws_backend import AwsVmBackend, resume_poll_delay  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.manager import GrantError  # noqa: E402
from pool_manager.vm_backend import BackendError, RUNNING, VmHandle  # noqa: E402
from tests.pool._poolkit import _run_sqlite, build_config, build_manager  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


_REGION = "us-west-2"
_IMAGE_BASE = f"arn:aws:lambda:{_REGION}:123456789012:microvm-image:notebook-microvm"
_CONN = f"arn:aws:lambda:{_REGION}:aws:network-connector:aws-network-connector:"


def make_backend():
    return AwsVmBackend(
        region=_REGION, image_arn_base=_IMAGE_BASE,
        exec_role_arn="arn:aws:iam::123456789012:role/MicroVMSandboxExecRole",
        ingress_connector=_CONN + "HTTP_INGRESS", shell_ingress_connector=_CONN + "SHELL_INGRESS",
        egress_connector=_CONN + "INTERNET_EGRESS", idle_timeout_seconds=60,
        max_duration_seconds=28800,
    )


def _handle(vm_id="mvm-1"):
    return VmHandle(vm_id=vm_id, endpoint="vm-1.example.aws", arch="arm64", size_mib=2048,
                    launched_at="2026-01-01T00:00:00+00:00", meta={})


def _get_resp(state):
    from datetime import datetime, timezone
    return {
        "microvmId": "mvm-1", "state": state, "endpoint": "vm-1.example.aws",
        "imageArn": f"{_IMAGE_BASE}-arm64-2048", "imageVersion": "1",
        "maximumDurationInSeconds": 28800,
        "startedAt": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


class _FakeClock:
    """Fake monotonic + sleep: sleep advances the clock and records the delay."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.t += s


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _with_capture(logger_name):
    cap = _LogCapture()
    lg = logging.getLogger(logger_name)
    lg.addHandler(cap)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    return cap, lambda: (lg.removeHandler(cap), lg.setLevel(old_level))


def _run_resume(stub_setup, timeout=30.0):
    """Run backend.resume under a fake clock. Returns (result|exc, clock, logs)."""
    clock = _FakeClock()
    saved = (aws_backend._monotonic, aws_backend._sleep)
    aws_backend._monotonic, aws_backend._sleep = clock.monotonic, clock.sleep
    cap, undo = _with_capture("pool_manager.aws_backend")
    backend = make_backend()
    stub = Stubber(backend._lambda_client)
    stub.add_response("resume_microvm", {}, {"microvmIdentifier": "mvm-1"})
    stub_setup(stub)
    try:
        with stub:
            try:
                result = run_async(backend.resume(_handle(), timeout=timeout))
            except Exception as e:
                result = e
        pending_ok = True
        try:
            stub.assert_no_pending_responses()
        except AssertionError:
            pending_ok = False
    finally:
        aws_backend._monotonic, aws_backend._sleep = saved
        undo()
    return result, clock, cap.records, pending_ok


# ── schedule ────────────────────────────────────────────────────────────────
def test_schedule_values():
    check("poll 1 waits 0.3 s", resume_poll_delay(1, 0.0) == 0.3)
    check("poll 2 under 3 s waits 0.2 s", resume_poll_delay(2, 0.5) == 0.2)
    check("poll at 2.99 s still waits 0.2 s", resume_poll_delay(10, 2.99) == 0.2)
    check("poll at >= 3 s waits 0.5 s", resume_poll_delay(15, 3.0) == 0.5)
    check("throttled doubles the previous delay", resume_poll_delay(2, 0.3, 0.3, True) == 0.6)
    check("throttled never goes below the normal delay",
          resume_poll_delay(20, 5.0, 0.1, True) == 0.5)
    check("throttled doubling caps at 2.0 s", resume_poll_delay(5, 1.0, 1.5, True) == 2.0)
    check("unthrottled ignores prev_delay", resume_poll_delay(3, 1.0, 1.5, False) == 0.2)


# ── AwsVmBackend.resume ─────────────────────────────────────────────────────
def test_running_on_poll_3():
    def setup(stub):
        for st in ("RESUMING", "RESUMING", RUNNING):
            stub.add_response("get_microvm", _get_resp(st), {"microvmIdentifier": "mvm-1"})
    result, clock, logs, pending_ok = _run_resume(setup)
    check("RUNNING on poll 3 returns 'RUNNING'", result == "RUNNING")
    check("sleeps are [0.3, 0.2, 0.2] (first sleep BEFORE the first poll)",
          clock.sleeps == [0.3, 0.2, 0.2])
    check("all stubbed calls consumed (resume_microvm + 3 get_microvm)", pending_ok)
    info = [r.getMessage() for r in logs if r.levelno == logging.INFO]
    check("exactly one INFO line logged", len(info) == 1)
    line = info[0] if info else ""
    check("INFO line has resume_call=, resume_poll=, polls=3 and state=RUNNING",
          line.startswith("resume mvm-1:") and "resume_call=" in line
          and "resume_poll=700ms" in line and "polls=3" in line and "state=RUNNING" in line)


def test_dead_state_returned():
    def setup(stub):
        stub.add_response("get_microvm", _get_resp("FAILED"), {"microvmIdentifier": "mvm-1"})
    result, clock, _, pending_ok = _run_resume(setup)
    check("a dead state is returned (no raise)", result == "FAILED")
    check("dead after one poll: one 0.3 s sleep", clock.sleeps == [0.3] and pending_ok)


def test_throttled_then_success():
    def setup(stub):
        stub.add_client_error("get_microvm", service_error_code="ThrottlingException")
        stub.add_response("get_microvm", _get_resp(RUNNING), {"microvmIdentifier": "mvm-1"})
    result, clock, _, pending_ok = _run_resume(setup)
    check("throttled get_microvm is retried and resume returns RUNNING", result == "RUNNING")
    check("delay after the throttle doubles (0.3 -> 0.6)", clock.sleeps == [0.3, 0.6])
    check("throttle path consumed exactly 2 get_microvm calls", pending_ok)


def test_non_throttle_error_raises():
    def setup(stub):
        stub.add_client_error("get_microvm", service_error_code="ResourceNotFoundException")
    result, _, _, _ = _run_resume(setup)
    check("non-throttle get_microvm ClientError raises BackendError",
          isinstance(result, BackendError))


def test_timeout_raises():
    def setup(stub):
        # 0.3 + 0.2*4 = 1.1 s elapsed after poll 5 >= timeout 1.0
        for _ in range(5):
            stub.add_response("get_microvm", _get_resp("RESUMING"), {"microvmIdentifier": "mvm-1"})
    result, clock, _, pending_ok = _run_resume(setup, timeout=1.0)
    check("timeout while alive raises BackendError with last state",
          isinstance(result, BackendError) and "last state=RESUMING" in str(result))
    check("timeout stops polling at the deadline", pending_ok and len(clock.sleeps) == 5)


# ── PoolManager.grant with FakeVmBackend ────────────────────────────────────
def _req(session_id):
    return {"arch": "arm64", "size": 2048, "session_id": session_id,
            "env_vars": {"SECRET_KEY": "super-secret-value"}}


def _setup_manager():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())
    return backend, storage, manager


def test_grant_no_get_state_and_info_line():
    backend, _, manager = _setup_manager()
    backend.calls.clear()
    cap, undo = _with_capture("pool_manager.manager")
    try:
        result = run_async(manager.grant(_req("real-sess-1")))
    finally:
        undo()
    names = [n for n, _ in backend.calls]
    check("grant calls resume", "resume" in names)
    check("grant makes no get_state call", "get_state" not in names)
    info = [r.getMessage() for r in cap.records
            if r.levelno == logging.INFO and r.getMessage().startswith("grant ")]
    line = info[0] if info else ""
    check("grant INFO timing line present with resume/rebind/restore_state/total/state",
          line.startswith(f"grant {result['vm_id']} (arm64,2048mib):")
          and "resume=" in line and "rebind=" in line and "restore_state=-ms" in line
          and "total=" in line and "state=RUNNING" in line)
    check("grant INFO line never carries the request's secret values",
          all("super-secret-value" not in r.getMessage() for r in cap.records))
    check("no slow-resume WARNING on a fast resume",
          not any(r.levelno == logging.WARNING for r in cap.records))


def test_grant_dead_resume_cleanup():
    backend, storage, manager = _setup_manager()
    spare_id = storage.pool_vm_list(architecture="arm64", size_mib=2048)[0]["vm_id"]
    backend.calls.clear()
    backend.fail_resume_returns_dead_times(1)
    try:
        run_async(manager.grant(_req("real-sess-dead")))
        raised = None
    except Exception as e:
        raised = e
    check("dead resume surfaces as GrantError", isinstance(raised, GrantError))
    names = [n for n, _ in backend.calls]
    check("dead resume terminates the spare", "terminate" in names)
    check("dead resume makes no get_state call", "get_state" not in names)
    check("dead spare's row is deleted",
          spare_id not in {r["vm_id"] for r in storage.pool_vm_list()})


def test_grant_slow_resume_warning():
    backend, _, manager = _setup_manager()
    t = [500.0]
    real_resume = backend.resume

    async def slow_resume(vm, timeout):
        t[0] += 3.5
        return await real_resume(vm, timeout)

    backend.resume = slow_resume
    saved_clock = manager_mod._clock
    manager_mod._clock = lambda: t[0]
    cap, undo = _with_capture("pool_manager.manager")
    try:
        result = run_async(manager.grant(_req("real-sess-slow")))
    finally:
        manager_mod._clock = saved_clock
        undo()
    warns = [r.getMessage() for r in cap.records if r.levelno == logging.WARNING]
    check("WARNING logged when resume takes > 3 s",
          f"grant {result['vm_id']}: resume took 3.5s (>3 s)" in warns)
    check("slow grant still succeeds", "vm_id" in result)


def main():
    print("resume_poll_delay schedule:")
    test_schedule_values()
    print("AwsVmBackend.resume:")
    test_running_on_poll_3()
    test_dead_state_returned()
    test_throttled_then_success()
    test_non_throttle_error_raises()
    test_timeout_raises()
    print("PoolManager.grant:")
    test_grant_no_get_state_and_info_line()
    test_grant_dead_resume_cleanup()
    test_grant_slow_resume_warning()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL RESUME SCHEDULE CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
