#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit tests for the shared run guard (app/notebook/run_guard.py):
_RunGuard (lock, timer, fire-once, drain, thread-local) and its two subclasses
_PythonRunGuard (KeyboardInterrupt injection + native cancels) and _SqlRunGuard
(native cancels, else injection).

Run: .venv/bin/python tests/vm/test_run_guard.py
Exits non-zero on failure.
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

from app.notebook import run_guard as rg  # noqa: E402
from app.notebook import run_limits  # noqa: E402
from app.notebook import executor as E  # noqa: E402
from app.notebook import sql_engine as sm  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""), flush=True)
    if not cond:
        _fails.append(label)


class Ex:
    _py_run = None
    _sql_run = None


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def elsewhere(fn, *a):
    """Call fn on another thread (like the timer / POST /interrupt do) and return
    its value; the KeyboardInterrupt it queues lands on the CALLER's thread later."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn(*a)))
    t.start()
    t.join(10)
    return box[0]


def on_worker(fn):
    """Run fn on a fresh one-thread pool; return (result_or_exception, pool)."""
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn)
    try:
        return fut.result(30), pool
    except BaseException as e:  # noqa: BLE001
        return e, pool


print("Names and layering")
check("class hierarchy", issubclass(rg._PythonRunGuard, rg._RunGuard)
      and issubclass(rg._SqlRunGuard, rg._RunGuard))
check("executor re-exports the limits",
      E.TIMEOUT_ERROR is run_limits.TIMEOUT_ERROR and E.INTERRUPT_ERROR is run_limits.INTERRUPT_ERROR
      and E.execution_timeout_seconds is run_limits.execution_timeout_seconds)
check("sql_engine re-exports", sm._SqlRunGuard is rg._SqlRunGuard and sm._RUN is rg._RUN
      and sm._current_guard is rg._current_guard and rg.current_guard is rg._current_guard)

os.environ["EXECUTION_TIMEOUT_SECONDS"] = "30"

print("Python guard: fire once, error texts")
def case_fire_once():
    ex = Ex()
    g = rg._PythonRunGuard(ex)
    g.start()
    box = []
    def fire():
        box.append((g.interrupt(), g.interrupt(), g._fire("timeout")))
    t = threading.Thread(target=fire)
    try:
        t.start()
        spin(5)  # the one injected KeyboardInterrupt lands here
    except KeyboardInterrupt:
        pass
    t.join(10)
    g.finish()
    return box[0], g.reason, g.error_message(), g.fired
r, pool = on_worker(case_fire_once)
check("first interrupt() True, second False", r == ((True, False, False), "interrupt", run_limits.INTERRUPT_ERROR, True), r)
pool.shutdown()

def case_timeout_text():
    g = rg._PythonRunGuard(Ex())
    g.start()
    try:
        elsewhere(g._fire, "timeout")
        spin(5)
    except KeyboardInterrupt:
        pass
    g.finish()
    return g.error_message()
r, pool = on_worker(case_timeout_text)
check("timeout text", r == run_limits.TIMEOUT_ERROR.format(seconds=30), r)
pool.shutdown()

print("Python guard: disarm then fire is a no-op; drain on finish")
def case_disarm():
    ex = Ex()
    g = rg._PythonRunGuard(ex)
    g.start()
    in_slot = ex._py_run is g and rg._current_guard() is g
    g.finish()
    g.finish()  # idempotent
    after = (elsewhere(g._fire, "timeout"), elsewhere(g.interrupt), g.fired)
    spin(0.3)  # nothing may land
    return in_slot, after, ex._py_run, rg._current_guard()
r, pool = on_worker(case_disarm)
check("finish() then fire/interrupt: no-op, nothing lands, slots cleared",
      r == (True, (False, False, False), None, None), r)
pool.shutdown()

def case_drain():
    # The worker blocks in a C call (Event.wait), so the injected exception stays
    # pending until it returns; finish() must clear it before then.
    g = rg._PythonRunGuard(Ex())
    g.start()
    ready, go = threading.Event(), threading.Event()
    def worker():
        ready.set()
        go.wait(10)
        spin(0.3)  # a pending KeyboardInterrupt would land here
        return "clean"
    fut = pool2.submit(worker)
    ready.wait(10)
    g._tid = pool2_tid[0]
    g.interrupt()
    g.finish()
    go.set()
    return fut.result(10)
pool2 = ThreadPoolExecutor(max_workers=1)
pool2_tid = [pool2.submit(threading.get_ident).result(10)]
try:
    r = case_drain()
except BaseException as e:  # noqa: BLE001
    r = e
check("finish() drains a pending injection", r == "clean", repr(r))
check("pooled thread is clean for the next call",
      pool2.submit(lambda: (spin(0.2), "ok")[1]).result(10) == "ok")
pool2.shutdown()

print("Python guard: native cancels are called")
def case_cancels():
    calls = []
    g = rg._PythonRunGuard(Ex())
    g.start()
    a = lambda: calls.append("a")   # noqa: E731
    b = lambda: calls.append("b")   # noqa: E731
    g.add_cancel(a)
    g.add_cancel(b)
    g.remove_cancel(b)
    try:
        elsewhere(g.interrupt)
        spin(5)
    except KeyboardInterrupt:
        pass
    late = lambda: calls.append("late")  # noqa: E731
    g.add_cancel(late)  # ignored once fired
    g.finish()
    return calls, g._cancels
r, pool = on_worker(case_cancels)
check("registered cancel called once, removed one not, late one ignored",
      r == (["a"], []), r)
pool.shutdown()

def case_cancel_raises():
    g = rg._PythonRunGuard(Ex())
    g.start()
    def bad():
        raise RuntimeError("boom")
    g.add_cancel(bad)
    try:
        ok = elsewhere(g.interrupt)
        spin(5)
    except KeyboardInterrupt:
        ok = "ki"
    g.finish()
    return ok
r, pool = on_worker(case_cancel_raises)
check("a failing cancel does not break the fire", r in (True, "ki"), r)
pool.shutdown()

print("Timer")
os.environ["EXECUTION_TIMEOUT_SECONDS"] = "1"
def case_timer():
    g = rg._PythonRunGuard(Ex())
    g.start()
    t0 = time.monotonic()
    try:
        spin(10)
    except KeyboardInterrupt:
        pass
    took = time.monotonic() - t0
    reason = g.reason
    g.finish()
    return reason, took < 5
r, pool = on_worker(case_timer)
check("timer fires the Python guard once", r == ("timeout", True), r)
pool.shutdown()
os.environ["EXECUTION_TIMEOUT_SECONDS"] = "30"

print("SQL guard: two modes")
def case_sql_native():
    calls = []
    g = rg._SqlRunGuard(Ex())
    g.start()
    g.add_cancel(lambda: calls.append("native"))
    ok = elsewhere(g.interrupt)
    spin(0.3)  # no async exception when a native cancel is registered
    g.finish()
    return ok, calls, g.reason
r, pool = on_worker(case_sql_native)
check("native cancel: called, no injection", r == (True, ["native"], "interrupt"), r)
pool.shutdown()

def case_sql_inject():
    g = rg._SqlRunGuard(Ex())
    g.start()
    got = False
    try:
        elsewhere(g.interrupt)
        spin(5)
    except KeyboardInterrupt:
        got = True
    g.finish()
    return got, g.fired
r, pool = on_worker(case_sql_inject)
check("no cancel: KeyboardInterrupt injected", r == (True, True), r)
pool.shutdown()

print("Thread-local lookup and no leakage")
def case_lookup():
    ex = Ex()
    seen = {}
    py = rg._PythonRunGuard(ex)
    py.start()
    seen["py"] = rg.current_guard() is py
    py.finish()
    seen["after_py"] = rg.current_guard()
    sq = rg._SqlRunGuard(ex)
    sq.start()
    seen["sql"] = rg.current_guard() is sq
    sq.finish()
    seen["after_sql"] = rg.current_guard()
    return seen
r, pool = on_worker(case_lookup)
check("Python guard inside a Python cell, SQL guard inside a SQL cell, None after",
      r == {"py": True, "after_py": None, "sql": True, "after_sql": None}, r)
check("other threads see no guard", rg.current_guard() is None)
pool.shutdown()

print("Executor integration")
os.environ["EXECUTION_TIMEOUT_SECONDS"] = "30"
ex = E.SandboxExecutor()
pool = ThreadPoolExecutor(max_workers=1)
r = pool.submit(ex.execute, "from app.notebook.run_guard import current_guard as _cg\n"
                "g = _cg()\nprint(type(g).__name__)", "c1").result(30)
check("inside a Python cell current_guard() is the _PythonRunGuard",
      r.success and r.output.strip() == "_PythonRunGuard", (r.output, r.error))
check("slots cleared and no guard leaks into the next cell on the reused thread",
      ex._py_run is None and pool.submit(rg.current_guard).result(10) is None)
pool.shutdown()

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
