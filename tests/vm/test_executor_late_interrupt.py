#!/usr/bin/env python3
# test-kind: offline
"""
Offline regression test: a timeout or Stop (interrupt()) that arrives after a
Python cell left exec() never escapes SandboxExecutor.execute().

The windows are widened deterministically by busy-waiting inside the executor's
own error formatting (_build_error_debug) and its finally cleanup
(_ResourceSampler.stop). Before the fix the injected KeyboardInterrupt landed
there: execute() raised instead of returning an ExecutionResult, and in the
finally case it left sys.stdout swapped and _exec_thread_id set.
Also checks one injection per run: a second Stop returns False.
Run: .venv/bin/python tests/vm/test_executor_late_interrupt.py
Exits non-zero on failure.
"""
import io
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
import app.notebook.executor as E  # noqa: E402
logging.disable(logging.WARNING)

REAL_STDOUT, REAL_STDERR = sys.stdout, sys.stderr
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""),
          file=REAL_STDOUT, flush=True)
    if not cond:
        _fails.append(label)


def busy(seconds):
    # Pure-Python loop: a pending async exception is delivered here.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def busy_until(event, cap=10.0):
    end = time.monotonic() + cap
    while not event.is_set() and time.monotonic() < end:
        pass


ORIG_BED = E._build_error_debug
ORIG_STOP = E._ResourceSampler.stop


def run_case(name, code, patch, trigger):
    """Run `code` in a fresh executor on a fresh 1-worker pool with `patch`
    installed. patch(entered, go) returns (install, uninstall); trigger(ex,
    entered, go) runs on the main thread and returns interrupt()'s value or None.
    Returns (result or exception, trigger value, executor, pool)."""
    ex = E.SandboxExecutor()
    pool = ThreadPoolExecutor(max_workers=1)
    entered, go = threading.Event(), threading.Event()
    install, uninstall = patch(entered, go)
    install()
    try:
        fut = pool.submit(ex.execute, code, name)
        fired = trigger(ex, entered, go)
        try:
            out = fut.result(30)
        except BaseException as e:  # noqa: BLE001 — the bug is a KeyboardInterrupt here
            out = e
    finally:
        uninstall()
        stdout_swapped = sys.stdout is not REAL_STDOUT
        sys.stdout, sys.stderr = REAL_STDOUT, REAL_STDERR
    return out, fired, ex, pool, stdout_swapped


def patch_error_debug(stop_mode):
    def factory(entered, go):
        def slow(e):
            entered.set()
            if stop_mode:
                busy_until(go)
                busy(0.2)
            else:
                busy(E.execution_timeout_seconds() + 1.0)
            return ORIG_BED(e)

        def install():
            E._build_error_debug = slow

        def uninstall():
            E._build_error_debug = ORIG_BED
        return install, uninstall
    return factory


def patch_sampler_stop(stop_mode):
    def factory(entered, go):
        def slow(self):
            entered.set()
            if stop_mode:
                busy_until(go)
                busy(0.2)
            else:
                busy(E.execution_timeout_seconds() + 1.0)
            return ORIG_STOP(self)

        def install():
            E._ResourceSampler.stop = slow

        def uninstall():
            E._ResourceSampler.stop = ORIG_STOP
        return install, uninstall
    return factory


def stop_when_entered(ex, entered, go):
    entered.wait(10)
    fired = ex.interrupt()
    go.set()
    return fired


def no_trigger(ex, entered, go):
    return None


def is_result(out):
    return isinstance(out, E.ExecutionResult)


def describe(out):
    if is_result(out):
        return f"success={out.success} error={out.error[:80]!r}"
    return f"raised {type(out).__name__}"


def after_checks(tag, ex, pool, stdout_swapped):
    check(f"{tag}: sys.stdout restored after the run", not stdout_swapped)
    check(f"{tag}: _exec_thread_id cleared", ex._exec_thread_id is None)
    check(f"{tag}: idle interrupt() returns False", ex.interrupt() is False)
    try:
        r = pool.submit(ex.execute, "y = 2\nprint('next ok')", "next").result(15)
        ok = is_result(r) and r.success and r.output.strip() == "next ok"
        check(f"{tag}: next cell on the same pool thread runs", ok, describe(r))
    except BaseException as e:  # noqa: BLE001
        check(f"{tag}: next cell on the same pool thread runs", False, f"raised {type(e).__name__}")
    sys.stdout, sys.stderr = REAL_STDOUT, REAL_STDERR
    pool.shutdown(wait=False)


os.environ["EXECUTION_TIMEOUT_SECONDS"] = "30"

# A: Stop while the executor formats the cell's own error.
print("  A: Stop during _build_error_debug", file=REAL_STDOUT)
out, fired, ex, pool, sw = run_case("a", "raise ValueError('cell error')",
                                    patch_error_debug(True), stop_when_entered)
check("A: execute() returns an ExecutionResult", is_result(out), describe(out))
check("A: the result is the cell's own ValueError",
      is_result(out) and not out.success and out.error.startswith("ValueError: cell error"),
      describe(out))
check("A: interrupt() after exec() ended returns False", fired is False, f"returned {fired}")
after_checks("A", ex, pool, sw)

# B: Stop while the executor runs its finally cleanup (sampler.stop).
print("  B: Stop during the finally cleanup", file=REAL_STDOUT)
out, fired, ex, pool, sw = run_case("b", "x = 1", patch_sampler_stop(True), stop_when_entered)
check("B: execute() returns an ExecutionResult", is_result(out), describe(out))
check("B: the cell succeeded", is_result(out) and out.success, describe(out))
check("B: interrupt() after exec() ended returns False", fired is False, f"returned {fired}")
after_checks("B", ex, pool, sw)

os.environ["EXECUTION_TIMEOUT_SECONDS"] = "1"

# C: timeout timer expires while the executor formats the cell's own error.
print("  C: timeout during _build_error_debug", file=REAL_STDOUT)
out, fired, ex, pool, sw = run_case("c", "raise ValueError('cell error')",
                                    patch_error_debug(False), no_trigger)
check("C: execute() returns an ExecutionResult", is_result(out), describe(out))
check("C: the result is the cell's own ValueError (no timeout text)",
      is_result(out) and not out.success and out.error.startswith("ValueError: cell error"),
      describe(out))
after_checks("C", ex, pool, sw)

# D: timeout timer expires during the finally cleanup.
print("  D: timeout during the finally cleanup", file=REAL_STDOUT)
out, fired, ex, pool, sw = run_case("d", "x = 1", patch_sampler_stop(False), no_trigger)
check("D: execute() returns an ExecutionResult", is_result(out), describe(out))
check("D: the cell succeeded", is_result(out) and out.success, describe(out))
after_checks("D", ex, pool, sw)

# E: one injection per run. A second Stop returns False; the cell reports the
# interrupt text, and a timeout after the Stop does not change it.
os.environ["EXECUTION_TIMEOUT_SECONDS"] = "30"
print("  E: second Stop on a running cell", file=REAL_STDOUT)
ex = E.SandboxExecutor()
pool = ThreadPoolExecutor(max_workers=1)
fut = pool.submit(ex.execute, "import time\nt = time.monotonic()\n"
                              "while time.monotonic() - t < 20:\n    pass\n", "e")
deadline = time.monotonic() + 10
while ex._exec_thread_id is None and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(0.1)
first = ex.interrupt()
second = ex.interrupt()
try:
    out = fut.result(15)
except BaseException as e:  # noqa: BLE001
    out = e
sys.stdout, sys.stderr = REAL_STDOUT, REAL_STDERR
check("E: first interrupt() returns True", first is True, f"returned {first}")
check("E: second interrupt() returns False", second is False, f"returned {second}")
check("E: the cell ends with the interrupt text",
      is_result(out) and not out.success and out.error == E.INTERRUPT_ERROR, describe(out))
after_checks("E", ex, pool, False)

if _fails:
    print(f"{len(_fails)} FAILED: {_fails}", file=REAL_STDOUT)
    sys.stdout.flush()
    os._exit(1)
print("ALL PASS", file=REAL_STDOUT)
sys.stdout.flush()
os._exit(0)
