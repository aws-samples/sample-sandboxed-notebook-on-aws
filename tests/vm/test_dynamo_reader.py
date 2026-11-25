#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: ONE DynamoDB reader (helpers/_dynamo.py) behind read_dynamodb,
dynamodb_schema, read_dynamodb_query and the SQL scan / PartiQL.

Fake boto3 resource/client with paged scan and query (LastEvaluatedKey). Checks:
pagination to the end, limit behaviours, Decimal (helper) vs float (SQL layer),
read_dynamodb_query follows pages (no silent 1 MB cut) and keeps its key-schema
hint, and the SQL run guard is checked between pages.

Run: .venv/bin/python tests/vm/test_dynamo_reader.py
"""
import logging
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from app.notebook import helpers as H  # noqa: E402
from app.notebook import run_guard  # noqa: E402
from app.notebook import sql_engine as sm  # noqa: E402
from app.notebook.executor import INTERRUPT_ERROR  # noqa: E402
from app.notebook.helpers import _dynamo  # noqa: E402

logging.disable(logging.WARNING)
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


ITEMS = [{"pk": f"U{i:03d}", "sk": f"S{i}", "score": Decimal(i)} for i in range(25)]
PAGE = 10
calls = {"scan": [], "query": [], "hook": None}


class FakeTable:
    item_count = len(ITEMS)

    def scan(self, **kw):
        calls["scan"].append(kw)
        if calls["hook"]:
            calls["hook"]()
        start = int(kw["ExclusiveStartKey"]["pos"]) if kw.get("ExclusiveStartKey") else 0
        n = min(PAGE, kw.get("Limit") or PAGE)
        resp = {"Items": [dict(i) for i in ITEMS[start:start + n]]}
        if start + n < len(ITEMS):
            resp["LastEvaluatedKey"] = {"pos": start + n}
        return resp

    def query(self, **kw):
        calls["query"].append(kw)
        if FakeTable.query_error:
            raise FakeTable.query_error
        start = int(kw["ExclusiveStartKey"]["pos"]) if kw.get("ExclusiveStartKey") else 0
        resp = {"Items": [dict(i) for i in ITEMS[start:start + PAGE]]}
        if start + PAGE < len(ITEMS):
            resp["LastEvaluatedKey"] = {"pos": start + PAGE}
        return resp

    query_error = None


class FakeResource:
    def Table(self, name):
        return FakeTable()


class FakeClient:
    def describe_table(self, TableName):
        return {"Table": {"AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"},
                                                   {"AttributeName": "sk", "AttributeType": "S"}],
                          "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"},
                                        {"AttributeName": "sk", "KeyType": "RANGE"}]}}

    def execute_statement(self, **kw):
        raise RuntimeError("PartiQL not available in this test")


boto3.resource = lambda *a, **k: FakeResource()
boto3.client = lambda *a, **k: FakeClient()


def reset():
    calls["scan"].clear()
    calls["query"].clear()
    calls["hook"] = None
    FakeTable.query_error = None
    sm._scan_dynamodb_table._cache = {}
    sm._scan_dynamodb_table._cache_ts = {}


# ── scan ──────────────────────────────────────────────────────────────────────
reset()
df = H.read_dynamodb("t")
check("read_dynamodb without limit reads all 3 pages", len(df) == 25 and len(calls["scan"]) == 3)
check("read_dynamodb keeps Decimal", isinstance(df["score"].iloc[0], Decimal))

reset()
df = H.read_dynamodb("t", limit=5)
check("read_dynamodb(limit=5) is one page of 5", len(df) == 5 and len(calls["scan"]) == 1
      and calls["scan"][0].get("Limit") == 5)

reset()
sentinel = object()
H.read_dynamodb("t", limit=3, filter={"score": {"gte": 1.5}})
check("read_dynamodb filter reaches the scan call",
      "FilterExpression" in calls["scan"][0] and calls["scan"][0]["Limit"] == 3)

reset()
items = _dynamo.scan_items("t", "us-west-2", limit=25, fill_limit=True)
check("fill mode reaches the limit across short pages", len(items) == 25
      and [c.get("Limit") for c in calls["scan"]] == [25, 15, 5])

# ── SQL layer: float + cache stay in the SQL scan ─────────────────────────────
reset()
sdf = sm._scan_dynamodb_table("t", "us-west-2")
check("SQL scan returns float", str(sdf["score"].dtype) == "float64" and len(sdf) == 25)
n_before = len(calls["scan"])
sm._scan_dynamodb_table("t", "us-west-2")
check("SQL scan cache hit makes no new calls", len(calls["scan"]) == n_before)

# ── query pagination ──────────────────────────────────────────────────────────
reset()
df = H.read_dynamodb_query("t", "pk", "U001")
check("read_dynamodb_query follows pages (25 items over 3 queries)",
      len(df) == 25 and len(calls["query"]) == 3)
check("2nd query carries ExclusiveStartKey", "ExclusiveStartKey" in calls["query"][1]
      and "ExclusiveStartKey" not in calls["query"][0])

reset()
FakeTable.query_error = ClientError(
    {"Error": {"Code": "ValidationException", "Message": "Query condition missed key schema element"}},
    "Query")
try:
    H.read_dynamodb_query("t", "color", "red")
    check("ValidationException hint raised", False)
except ValueError as e:
    check("ValidationException hint names the real key", "partition key 'pk'" in str(e) and "'sk'" in str(e))

# ── schema ────────────────────────────────────────────────────────────────────
reset()
sch = H.dynamodb_schema("t")
check("dynamodb_schema lists keys then sampled attrs",
      list(sch["attribute"][:2]) == ["pk", "sk"] and "score" in set(sch["attribute"])
      and calls["scan"][0].get("Limit") == 25)


# ── guard checked between pages ───────────────────────────────────────────────
class StubGuard:
    def __init__(self):
        self.fired = False

    def error_message(self):
        return INTERRUPT_ERROR


reset()
g = StubGuard()
run_guard._RUN.guard = g
calls["hook"] = lambda: setattr(g, "fired", True) if len(calls["scan"]) == 1 else None
try:
    sm._scan_dynamodb_table("t", "us-west-2")
    check("SQL scan stops when the guard fired", False)
except RuntimeError as e:
    check("SQL scan stops when the guard fired", str(e) == INTERRUPT_ERROR, str(e))
check("no page requested after the guard fired", len(calls["scan"]) == 1, calls["scan"])
run_guard._RUN.guard = None

reset()
g = StubGuard()
run_guard._RUN.guard = g
calls["hook"] = lambda: setattr(g, "fired", True) if len(calls["query"]) == 0 else None
FakeTable.query_error = None
orig_query = FakeTable.query


def query_and_fire(self, **kw):
    r = orig_query(self, **kw)
    g.fired = True
    return r


FakeTable.query = query_and_fire
try:
    _dynamo.query_items("t", object(), "us-west-2")
    check("query_items stops when the guard fired", False)
except RuntimeError:
    check("query_items stops when the guard fired", len(calls["query"]) == 1)
FakeTable.query = orig_query
run_guard._RUN.guard = None

# No guard (Python cell): nothing is checked
reset()
check("no guard: scan runs to the end", len(H.read_dynamodb("t")) == 25)

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
