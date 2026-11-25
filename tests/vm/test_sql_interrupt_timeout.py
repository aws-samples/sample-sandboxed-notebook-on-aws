#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review A-B-06, A-B-08): SQL cells get the Python-cell timeout
(EXECUTION_TIMEOUT_SECONDS, same error text) and POST /interrupt.

- Amazon Athena (fake client that never finishes): timeout and interrupt both end
  the cell with the Python-cell text and call stop_query_execution exactly once;
  a poll failure also stops the query once.
- DuckDB: a long cross join on the shared connection is stopped by interrupt and
  by the timeout (con.interrupt()); the next query on the same connection works.
- Amazon Aurora (no cancel call in the Data API): the injected KeyboardInterrupt
  ends the cell, and no pending async exception reaches the next call on the
  same pooled worker thread.
- The /interrupt route answers "interrupted" while a SQL cell runs, "idle" after.

Needs duckdb (tests/requirements-dev.txt). Run: .venv/bin/python tests/vm/test_sql_interrupt_timeout.py
Exits non-zero on failure; exits 0 with SKIP when duckdb is unavailable.
"""
import asyncio
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
try:
    import duckdb  # noqa: F401
except ImportError:
    print("SKIP  duckdb not installed")
    sys.exit(0)
import logging  # noqa: E402
import boto3  # noqa: E402
from app.notebook import sql_engine as sm  # noqa: E402
from app.notebook.executor import SandboxExecutor, TIMEOUT_ERROR, INTERRUPT_ERROR  # noqa: E402
logging.disable(logging.WARNING)
sm._DUCKDB_HTTPFS_LOADED = True  # never INSTALL httpfs (network) in this test

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


# One worker thread, reused for every call: a leaked async exception would hit
# the next call on it (as on the asyncio.to_thread pool).
POOL = ThreadPoolExecutor(max_workers=1)


def run_sql(ex, sql, timeout_s):
    os.environ["EXECUTION_TIMEOUT_SECONDS"] = str(timeout_s)
    return POOL.submit(sm._run_sql_cell_blocking, ex, None, sql, "out", "c-sql")


def wait_for(pred, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── Fake Athena client ───────────────────────────────────────────────────────
class FakeAthena:
    def __init__(self, fail_after=None):
        self.polls = 0
        self.stops = []
        self.fail_after = fail_after
        self.state = "RUNNING"

    def start_query_execution(self, **kw):
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        self.polls += 1
        if self.fail_after is not None and self.polls > self.fail_after:
            raise RuntimeError("throttled by fake Athena")
        return {"QueryExecution": {"Status": {"State": self.state}}}

    def stop_query_execution(self, QueryExecutionId):
        self.stops.append(QueryExecutionId)
        self.state = "CANCELLED"
        return {}


fake = FakeAthena()
boto3.client = lambda service, **kw: fake
_real_routing = sm._get_routing_catalogs
ATHENA_SQL = "SELECT * FROM salesdb.orders"


def athena_routing(dc):
    return {"salesdb": {"orders"}}, {}, {}


print("\nAmazon Athena")
sm._get_routing_catalogs = athena_routing
ex = SandboxExecutor()
fake = FakeAthena()
t0 = time.monotonic()
res = run_sql(ex, ATHENA_SQL, 1).result(timeout=20)
took = time.monotonic() - t0
check("timeout: cell fails", res.get("success") is False, res)
check("  error is the Python-cell timeout text", res.get("error") == TIMEOUT_ERROR.format(seconds=1),
      res.get("error"))
check("  stop_query_execution called once", fake.stops == ["q-1"], fake.stops)
check("  ends near the 1 s budget", took < 4, f"{took:.1f}s")
check("  executor idle afterwards", ex._sql_run is None and ex.interrupt() is False)

fake = FakeAthena()
fut = run_sql(ex, ATHENA_SQL, 30)
check("interrupt: query is polling", wait_for(lambda: fake.polls >= 1))
check("  executor.interrupt() returns True", ex.interrupt() is True)
check("  a second interrupt() does not fire again", ex.interrupt() is False)
res = fut.result(timeout=10)
check("  error is the Python-cell interrupt text", res.get("error") == INTERRUPT_ERROR, res.get("error"))
check("  stop_query_execution called once", fake.stops == ["q-1"], fake.stops)

fake = FakeAthena(fail_after=2)
res = run_sql(ex, ATHENA_SQL, 30).result(timeout=10)
check("poll failure: engine error surfaced", "throttled by fake Athena" in (res.get("error") or ""),
      res.get("error"))
check("  stop_query_execution called once", fake.stops == ["q-1"], fake.stops)
sm._get_routing_catalogs = _real_routing

# ── DuckDB on the shared connection ──────────────────────────────────────────
print("\nDuckDB")
LONG = "SELECT count(*) AS n FROM range(100000000) a, range(100000000) b"
ex = SandboxExecutor()
fut = run_sql(ex, LONG, 30)
check("interrupt: cell is running", wait_for(lambda: ex._sql_run is not None))
time.sleep(0.3)
check("  executor.interrupt() returns True", ex.interrupt() is True)
res = fut.result(timeout=10)
check("  error is the Python-cell interrupt text", res.get("error") == INTERRUPT_ERROR, res.get("error"))
res = run_sql(ex, "SELECT 42 AS a", 30).result(timeout=10)
check("  next query on the shared connection works",
      res.get("success") is True and int(ex._namespace["out"]["a"][0]) == 42, res.get("error"))

t0 = time.monotonic()
res = run_sql(ex, LONG, 1).result(timeout=20)
took = time.monotonic() - t0
check("timeout: error is the Python-cell timeout text",
      res.get("error") == TIMEOUT_ERROR.format(seconds=1), res.get("error"))
check("  ends near the 1 s budget", took < 4, f"{took:.1f}s")
res = run_sql(ex, "SELECT 7 AS b", 30).result(timeout=10)
check("  next query works", res.get("success") is True, res.get("error"))

# ── Amazon Aurora: no cancel call, the injected KeyboardInterrupt ends it ────
print("\nAmazon Aurora (Data API, no cancel)")
aurora_calls = []


def slow_aurora(sql, region, database=None):
    aurora_calls.append(sql)
    while True:  # Python-level wait: the async exception lands here
        time.sleep(0.01)


sm._run_aurora_query = slow_aurora
sm._get_routing_catalogs = lambda dc: ({}, {("appdb", "public"): {"products"}}, {})
ex = SandboxExecutor()
fut = run_sql(ex, "SELECT * FROM appdb.public.products", 30)
check("interrupt: Aurora call running", wait_for(lambda: bool(aurora_calls)))
check("  executor.interrupt() returns True", ex.interrupt() is True)
res = fut.result(timeout=10)
check("  error is the Python-cell interrupt text", res.get("error") == INTERRUPT_ERROR, res.get("error"))
res = run_sql(ex, "SELECT * FROM appdb.public.products", 1).result(timeout=20)
check("timeout: error is the Python-cell timeout text",
      res.get("error") == TIMEOUT_ERROR.format(seconds=1), res.get("error"))
sm._get_routing_catalogs = _real_routing


def plain_sleep():
    time.sleep(0.3)
    return "ok"


check("  no pending async exception on the reused worker thread",
      POOL.submit(plain_sleep).result(timeout=5) == "ok")
res = run_sql(ex, "SELECT 1 AS c", 30).result(timeout=10)
check("  next SQL cell works", res.get("success") is True, res.get("error"))

# ── Python-cell texts are the same constants ─────────────────────────────────
print("\nPython cell parity")
os.environ["EXECUTION_TIMEOUT_SECONDS"] = "1"
r = POOL.submit(ex.execute, "while True:\n    pass").result(timeout=20)
check("Python-cell timeout text matches", r.error == TIMEOUT_ERROR.format(seconds=1), r.error)

# ── /interrupt route ─────────────────────────────────────────────────────────
print("\n/interrupt route")
try:
    from app.notebook import routes as nb_routes
except Exception as e:  # pragma: no cover - only when the route module can't import
    nb_routes = None
    check("import app.notebook.routes", False, repr(e))
if nb_routes is not None:
    sm._run_aurora_query = slow_aurora
    sm._get_routing_catalogs = lambda dc: ({}, {("appdb", "public"): {"products"}}, {})
    aurora_calls.clear()
    ex = SandboxExecutor()
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(executor=ex)))
    fut = run_sql(ex, "SELECT * FROM appdb.public.products", 30)
    wait_for(lambda: bool(aurora_calls))
    body = asyncio.run(nb_routes.interrupt_execution(req))
    check("POST /interrupt during a SQL cell -> interrupted", body.get("status") == "interrupted", body)
    fut.result(timeout=10)
    body = asyncio.run(nb_routes.interrupt_execution(req))
    check("POST /interrupt after it -> idle", body.get("status") == "idle", body)
    sm._get_routing_catalogs = _real_routing

POOL.shutdown(wait=True)
print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
