#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the ONE Athena runner (app/notebook/helpers/_athena.py) and the
three paths built on it: SQL cell (sql_engine._run_athena_query_sync), read_athena
and UNLOAD (unload_athena_to_s3 and the SQL-cell UNLOAD). Fake clients, no AWS.
- result parsing: pages, NULL as None, declared dtypes, header skipped once;
- failure text per caller, terminal state never stops, one stop on timeout /
  interrupt / poll failure, catalog+database in the context, fetch="none";
- guard integration: each path records exactly one stop_query_execution on timeout
  and on interrupt, and ends near the budget.
Needs duckdb (SQL cell path). Run: .venv/bin/python tests/vm/test_athena_runner.py
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["AWS_REGION"] = "us-west-2"
os.environ.pop("ARTIFACT_BUCKET", None)
try:
    import duckdb  # noqa: F401
except ImportError:
    print("SKIP  duckdb not installed")
    sys.exit(0)
import logging  # noqa: E402
import boto3  # noqa: E402
import pandas as pd  # noqa: E402
from app.notebook import sql_engine as sm  # noqa: E402
from app.notebook import aws_clients as ac  # noqa: E402
from app.notebook.executor import SandboxExecutor, TIMEOUT_ERROR, INTERRUPT_ERROR  # noqa: E402
from app.notebook.helpers import _athena  # noqa: E402
import app.notebook.helpers as H  # noqa: E402

logging.disable(logging.CRITICAL)
sm._DUCKDB_HTTPFS_LOADED = True
_fails = []


def check(label, cond, detail=""):
    # sys.__stdout__: a Python cell run by the executor swaps sys.stdout.
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""),
          file=sys.__stdout__, flush=True)
    if not cond:
        _fails.append(label)


class FakeAthena:
    """Records calls. `state` is returned until stop_query_execution; `pages` are
    get_query_results pages (first page carries the header row)."""

    def __init__(self, state="RUNNING", pages=None, reason="boom", fail_after=None):
        self.state = state
        self.pages = pages or []
        self.reason = reason
        self.fail_after = fail_after
        self.polls = 0
        self.stops = []
        self.starts = []
        self.result_calls = []

    def start_query_execution(self, **kw):
        self.starts.append(kw)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        self.polls += 1
        if self.fail_after is not None and self.polls > self.fail_after:
            raise RuntimeError("throttled by fake Athena")
        st = {"State": self.state}
        if self.state in ("FAILED", "CANCELLED"):
            st["StateChangeReason"] = self.reason
        return {"QueryExecution": {"Status": st, "Statistics": {}}}

    def stop_query_execution(self, QueryExecutionId):
        self.stops.append(QueryExecutionId)
        self.state = "CANCELLED"
        return {}

    def get_query_results(self, QueryExecutionId, NextToken=None):
        self.result_calls.append(NextToken)
        idx = 0 if NextToken is None else int(NextToken)
        page = dict(self.pages[idx])
        if idx + 1 < len(self.pages):
            page["NextToken"] = str(idx + 1)
        return page


COLS = [{"Name": "id", "Type": "bigint"}, {"Name": "price", "Type": "double"},
        {"Name": "name", "Type": "varchar"}, {"Name": "ok", "Type": "boolean"}]


def row(*vals):
    return {"Data": [({"VarCharValue": v} if v is not None else {}) for v in vals]}


def result_pages():
    hdr = row("id", "price", "name", "ok")
    p1 = {"ResultSet": {"Rows": [hdr, row("1", "1.5", "a", "true"), row("2", None, None, "false")],
                        "ResultSetMetadata": {"ColumnInfo": COLS}}}
    p2 = {"ResultSet": {"Rows": [row("3", "3.5", "c", None)],
                        "ResultSetMetadata": {"ColumnInfo": COLS}}}
    return [p1, p2]


class FakeS3:
    def list_objects_v2(self, **kw):
        return {"KeyCount": 0}

    def head_object(self, **kw):
        return {"ContentLength": 1}


class FakeSts:
    calls = 0

    def get_caller_identity(self):
        FakeSts.calls += 1
        return {"Account": "111122223333"}


_real_client = boto3.client
_current = {"athena": None}


def install(athena):
    _current["athena"] = athena

    def fake(service, **kw):
        if service == "athena":
            return _current["athena"]
        if service == "s3":
            return FakeS3()
        if service == "sts":
            return FakeSts()
        raise AssertionError(f"unexpected client {service}")
    boto3.client = fake


def wait_for(pred, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── runner: results ──────────────────────────────────────────────────────────
print("\nRunner: results")
ac.reset_aws_clients()
fa = FakeAthena(state="SUCCEEDED", pages=result_pages())
install(fa)
df = _athena.run_athena("SELECT 1", workgroup="wg", database="db")
check("rows from 2 pages, header skipped once", len(df) == 3 and list(df["id"]) == [1, 2, 3], df)
check("  pages followed by NextToken", fa.result_calls == [None, "1"], fa.result_calls)
check("  NULL is missing (not an empty string)", pd.isna(df["name"].iloc[1]) and df["name"].iloc[1] != "" and pd.isna(df["price"].iloc[1])
      and pd.isna(df["ok"].iloc[2]))
check("  declared dtypes", str(df["id"].dtype) == "Int64" and str(df["price"].dtype) == "float64"
      and str(df["ok"].dtype) == "boolean", df.dtypes.to_dict())
check("  no stop on a finished query", fa.stops == [])
check("  Database in context, no Catalog, no output location",
      fa.starts[0]["QueryExecutionContext"] == {"Database": "db"} and "ResultConfiguration" not in fa.starts[0],
      fa.starts[0])
fa = FakeAthena(state="SUCCEEDED", pages=result_pages())
install(fa)
_athena.run_athena("SELECT 1", workgroup="wg", database="db", catalog="cat", output_location="s3://b/o/")
check("catalog + database reach QueryExecutionContext",
      fa.starts[0]["QueryExecutionContext"] == {"Catalog": "cat", "Database": "db"})
check("output_location reaches ResultConfiguration",
      fa.starts[0]["ResultConfiguration"] == {"OutputLocation": "s3://b/o/"})
fa = FakeAthena(state="SUCCEEDED", pages=result_pages())
install(fa)
r = _athena.run_athena("UNLOAD ...", workgroup="wg", fetch="none")
check('fetch="none" never calls get_query_results', r is None and fa.result_calls == [])

print("\nRunner: failures")
for state, prefix_kwargs, want in (
        ("FAILED", {}, "Athena query FAILED: boom"),
        ("CANCELLED", {}, "Athena query CANCELLED: boom"),
        ("FAILED", {"fail_prefix": "Athena query failed"}, "Athena query failed: boom")):
    fa = FakeAthena(state=state)
    install(fa)
    try:
        _athena.run_athena("SELECT 1", workgroup="wg", **prefix_kwargs)
        err = None
    except RuntimeError as e:
        err = str(e)
    check(f"{state} message {want!r}", err == want, err)
    check("  terminal state: no stop_query_execution", fa.stops == [])

os.environ["EXECUTION_TIMEOUT_SECONDS"] = "1"
fa = FakeAthena()
install(fa)
t0 = time.monotonic()
try:
    _athena.run_athena("SELECT 1", workgroup="wg")
    err = None
except RuntimeError as e:
    err = str(e)
check("timeout raises the Python-cell text", err == TIMEOUT_ERROR.format(seconds=1), err)
check("  one stop_query_execution", fa.stops == ["q-1"], fa.stops)
check("  ends near the budget", time.monotonic() - t0 < 4)

fa = FakeAthena(fail_after=2)
install(fa)
try:
    _athena.run_athena("SELECT 1", workgroup="wg")
    err = None
except RuntimeError as e:
    err = str(e)
check("poll failure surfaces and stops once", "throttled" in (err or "") and fa.stops == ["q-1"], (err, fa.stops))


class _Boom(FakeAthena):
    def get_query_execution(self, QueryExecutionId):
        raise KeyboardInterrupt()


fa = _Boom()
install(fa)
try:
    _athena.run_athena("SELECT 1", workgroup="wg")
except KeyboardInterrupt:
    pass
check("KeyboardInterrupt during the poll stops once", fa.stops == ["q-1"], fa.stops)

# ── bucket resolution: no STS per query ─────────────────────────────────────
print("\nBucket resolution")
os.environ["ARTIFACT_BUCKET"] = "art-bkt"
ac.reset_aws_clients()
FakeSts.calls = 0
fa = FakeAthena(state="SUCCEEDED", pages=result_pages())
install(fa)
H.read_athena("SELECT * FROM salesdb.orders", database="salesdb")
check("read_athena with ARTIFACT_BUCKET: zero STS calls", FakeSts.calls == 0, FakeSts.calls)
check("  output location uses the artifact bucket",
      fa.starts[0]["ResultConfiguration"]["OutputLocation"] == "s3://art-bkt/athena-results/")
del os.environ["ARTIFACT_BUCKET"]
ac.reset_aws_clients()
FakeSts.calls = 0
for _ in range(2):
    fa = FakeAthena(state="SUCCEEDED", pages=result_pages())
    install(fa)
    H.read_athena("SELECT * FROM salesdb.orders", database="salesdb")
check("ARTIFACT_BUCKET unset: one STS call for two queries", FakeSts.calls == 1, FakeSts.calls)
check("  bucket from account and region",
      fa.starts[0]["ResultConfiguration"]["OutputLocation"]
      == "s3://microvm-sandbox-artifacts-111122223333-us-west-2/athena-results/")

# ── guard integration: every Athena path records one stop ───────────────────
print("\nGuard integration")
POOL = ThreadPoolExecutor(max_workers=1)
_real_routing = sm._get_routing_catalogs
sm._get_routing_catalogs = lambda dc: ({"salesdb": {"orders"}}, {}, {})
os.environ["ARTIFACT_BUCKET"] = "microvm-sandbox-artifacts-test"
ac.reset_aws_clients()
UNLOAD_SQL = ("UNLOAD (SELECT * FROM salesdb.orders) TO "
              "'s3://microvm-sandbox-artifacts-test/out/' WITH (format = 'PARQUET')")


def sql_cell(ex, sql, timeout_s):
    os.environ["EXECUTION_TIMEOUT_SECONDS"] = str(timeout_s)
    return POOL.submit(sm._run_sql_cell_blocking, ex, None, sql, "out", "c-sql")


def python_cell(ex, code, timeout_s):
    os.environ["EXECUTION_TIMEOUT_SECONDS"] = str(timeout_s)
    return POOL.submit(ex.execute, code, "c-py")


PATHS = {
    "SQL cell": (lambda ex, t: sql_cell(ex, "SELECT * FROM salesdb.orders", t),
                 lambda res: res.get("error")),
    "SQL-cell UNLOAD": (lambda ex, t: sql_cell(ex, UNLOAD_SQL, t),
                        lambda res: res.get("error")),
    "read_athena (Python cell)": (lambda ex, t: python_cell(
        ex, "df = read_athena('SELECT * FROM salesdb.orders', database='salesdb')", t),
        lambda res: res.error),
    "unload_athena_to_s3 (Python cell)": (lambda ex, t: python_cell(
        ex, "r = unload_athena_to_s3('SELECT * FROM salesdb.orders', "
            "'s3://microvm-sandbox-artifacts-test/out2/')", t),
        lambda res: res.error),
}
for name, (start, err_of) in PATHS.items():
    print(f"  {name}")
    ex = SandboxExecutor()
    fa = FakeAthena()
    install(fa)
    t0 = time.monotonic()
    try:
        err = err_of(start(ex, 1).result(timeout=30)) or ""
    except BaseException as e:  # noqa: BLE001 — report a regression as a failed check
        err = f"raised {type(e).__name__}: {e}"
    took = time.monotonic() - t0
    check("timeout: Python-cell text", TIMEOUT_ERROR.format(seconds=1) in err, err[-200:])
    check("  one stop_query_execution", fa.stops == ["q-1"], fa.stops)
    check("  ends near the budget", took < 6, f"{took:.1f}s")

    fa = FakeAthena()
    install(fa)
    fut = start(ex, 30)
    check("interrupt: query is polling", wait_for(lambda: fa.polls >= 1))
    stopped = ex.interrupt()
    check("  interrupt() stops the cell", stopped is True)
    try:
        err = err_of(fut.result(timeout=15)) or ""
    except BaseException as e:  # noqa: BLE001 — report a regression as a failed check
        err = f"raised {type(e).__name__}: {e}"
    check("  interrupt text", INTERRUPT_ERROR in err or "KeyboardInterrupt" in err, err[-200:])
    check("  one stop_query_execution", fa.stops == ["q-1"], fa.stops)

sm._get_routing_catalogs = _real_routing
boto3.client = _real_client
os.environ.pop("ARTIFACT_BUCKET", None)

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
