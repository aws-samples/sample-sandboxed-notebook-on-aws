#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review A-B-02): a trailing LIMIT is pushed into the DynamoDB
scan ONLY for a plain single-table projection. GROUP BY / ORDER BY / WHERE /
COUNT / DISTINCT / window / JOIN queries scan the full table and DuckDB applies
the LIMIT, so results equal the same SQL over the FULL item set.

boto3's DynamoDB Table (paged scan with LastEvaluatedKey) and client (PartiQL,
forced to fail) are stubbed, as is the Aurora Data API. Needs duckdb (the VM has
it): when the .venv lacks it, put a duckdb wheel on PYTHONPATH, e.g.
    pip install --target /tmp/duck --no-deps duckdb
    PYTHONPATH=.:/tmp/duck .venv/bin/python tests/vm/test_dynamodb_limit_pushdown.py
Exits non-zero on failure; exits 0 with SKIP when duckdb is unavailable.
"""
import os
import random
import sys
from decimal import Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
try:
    import duckdb
except ImportError:
    print("SKIP  duckdb not installed")
    sys.exit(0)
import logging  # noqa: E402
import boto3  # noqa: E402
import pandas as pd  # noqa: E402
from app.notebook import sql_engine as sm  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402
logging.disable(logging.WARNING)
sm._DUCKDB_HTTPFS_LOADED = True  # never INSTALL httpfs (network) in this test

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


# ── 300 deterministic items in a shuffled (non-sorted) storage order ──────────
ITEMS = [{"item_id": f"I{i:04d}", "product_id": f"P{(i * 7) % 20:02d}",
          "rating": Decimal((i * 13) % 5 + 1)} for i in range(300)]
random.Random(42).shuffle(ITEMS)
PAGE = 64
TABLE = "ecommerce-reviews"
REF = f'dynamodb."{TABLE}"'
scan_calls = []


class FakeTable:
    item_count = len(ITEMS)

    def scan(self, Limit=None, ExclusiveStartKey=None):
        scan_calls.append({"Limit": Limit, "start": ExclusiveStartKey})
        start = int(ExclusiveStartKey["pos"]) if ExclusiveStartKey else 0
        n = min(PAGE, Limit) if Limit else PAGE
        page = ITEMS[start:start + n]
        resp = {"Items": [dict(it) for it in page]}
        if start + n < len(ITEMS):
            resp["LastEvaluatedKey"] = {"pos": start + n}
        return resp


class FakeResource:
    def Table(self, name):
        assert name == TABLE, name
        return FakeTable()


class FakeClient:
    def execute_statement(self, **kw):
        raise RuntimeError("PartiQL forced to fail in this test")


boto3.resource = lambda service, **kw: FakeResource()
boto3.client = lambda service, **kw: FakeClient()

PRODUCTS = pd.DataFrame({"product_id": [f"P{i:02d}" for i in range(20)],
                         "name": [f"Product {i}" for i in range(20)]})
AURORA_REF = "appdb.public.products"
aurora_sql_seen = []


def fake_aurora(sql, region, database=None):
    aurora_sql_seen.append(sql)
    df = PRODUCTS.copy()
    df.attrs["aurora_api_capped"] = False
    return df


sm._run_aurora_query = fake_aurora
_real_routing = sm._get_routing_catalogs


def full_df():
    df = pd.DataFrame([dict(it) for it in ITEMS])
    df["rating"] = df["rating"].astype(float)
    return df


def oracle(sql):
    """The same SQL in DuckDB over the FULL item set (and the Aurora table)."""
    con = duckdb.connect()
    con.register("_full_reviews", full_df())
    con.register("_full_products", PRODUCTS)
    q = sql.replace(REF, "_full_reviews").replace(AURORA_REF, "_full_products")
    return con.execute(q).df()


def run(sql, aurora=False):
    sm._scan_dynamodb_table._cache = {}
    sm._scan_dynamodb_table._cache_ts = {}
    scan_calls.clear()
    sm._get_routing_catalogs = ((lambda dc: ({}, {("appdb", "public"): {"products"}}, {}))
                                if aurora else _real_routing)
    ex = SandboxExecutor()
    res = sm._run_sql_cell_blocking(ex, None, sql, "out", "c-sql", bind_output=True)
    return res, ex._namespace.get("out")


def same(a, b):
    a = a.reset_index(drop=True)
    b = b.reset_index(drop=True)
    if list(a.columns) != list(b.columns) or len(a) != len(b):
        return False
    return a.astype(str).equals(b.astype(str))


FULL_PAGES = -(-len(ITEMS) // PAGE)


def full_scan():
    return len(scan_calls) == FULL_PAGES and all(c["Limit"] is None for c in scan_calls)


# 1. bare SELECT * LIMIT 5: pushdown, one scan with Limit=5
print("\nplain projection")
sql = f"SELECT * FROM {REF} LIMIT 5"
res, out = run(sql)
check("SELECT * LIMIT 5 succeeds", res.get("success") is True, res.get("error"))
check("  exactly 1 scan call with Limit=5", [c["Limit"] for c in scan_calls] == [5], scan_calls)
check("  equals DuckDB over the full set", out is not None and same(out[["item_id", "product_id", "rating"]],
                                                                       oracle(sql)[["item_id", "product_id", "rating"]]))
sql = f"SELECT item_id, rating * 2 AS r2 FROM {REF} LIMIT 7"
res, out = run(sql)
check("projection with expressions LIMIT 7: pushed down", [c["Limit"] for c in scan_calls] == [7], scan_calls)
check("  equals DuckDB over the full set", out is not None and same(out, oracle(sql)))
PAGE_SAVED = PAGE
PAGE = 4
sql = f"SELECT item_id FROM {REF} LIMIT 10"
res, out = run(sql)
check("pushdown pages on until n items when a page stops short",
      out is not None and len(out) == 10 and [c["Limit"] for c in scan_calls] == [10, 6, 2], scan_calls)
check("  equals DuckDB over the full set", out is not None and same(out, oracle(sql)))
PAGE = PAGE_SAVED

# 2. queries that need the whole table
print("\nfull-table queries (no pushdown)")
CASES = [
    ("GROUP BY ... ORDER BY ... LIMIT 5",
     f"SELECT product_id, COUNT(*) AS n, AVG(rating) AS avg_r FROM {REF} "
     f"GROUP BY product_id ORDER BY n DESC, product_id LIMIT 5"),
    ("ORDER BY ... LIMIT 10 without WHERE",
     f"SELECT item_id, rating FROM {REF} ORDER BY item_id LIMIT 10"),
    ("WHERE on a non-key attribute LIMIT 5 (PartiQL failed)",
     f"SELECT item_id, rating FROM {REF} WHERE rating >= 4 LIMIT 5"),
    ("COUNT(*) LIMIT 1", f"SELECT COUNT(*) AS n FROM {REF} LIMIT 1"),
    ("window function LIMIT 3", f"SELECT COUNT(*) OVER () AS total FROM {REF} LIMIT 3"),
    ("OFFSET", f"SELECT item_id FROM {REF} LIMIT 3 OFFSET 290"),
]
for label, sql in CASES:
    res, out = run(sql)
    check(f"{label}: succeeds", res.get("success") is True, res.get("error"))
    check(f"  full paged scan, no Limit ({FULL_PAGES} calls)", full_scan(), scan_calls)
    check("  equals DuckDB over the full set", out is not None and same(out, oracle(sql)),
          (None if out is None else out.head(3).to_dict("records"), oracle(sql).head(3).to_dict("records")))
res, out = run(f"SELECT COUNT(*) AS n FROM {REF} LIMIT 1")
check("COUNT(*) is the full 300", out is not None and int(out["n"][0]) == 300, out)
sql = f"SELECT DISTINCT product_id FROM {REF} LIMIT 15"
res, out = run(sql)
check("DISTINCT LIMIT 15: full paged scan", full_scan(), scan_calls)
check("  15 distinct products, as over the full set",
      out is not None and len(out) == len(oracle(sql)) == 15 and out["product_id"].is_unique, out)
sql = (f"SELECT product_id, COUNT(*) AS n FROM {REF} GROUP BY product_id "
       f"ORDER BY n DESC, product_id LIMIT 5")
res, out = run(sql)
check("live-check query shape: counts are not all 1", out is not None and int(out["n"].max()) > 1, out)

# 3. mixed branch: DynamoDB + Aurora join with LIMIT
print("\nmixed DynamoDB + Aurora")
sql = (f"SELECT r.item_id, p.name FROM {REF} r JOIN {AURORA_REF} p "
       f"ON r.product_id = p.product_id ORDER BY r.item_id LIMIT 7")
res, out = run(sql, aurora=True)
check("join succeeds via the mixed branch", res.get("success") is True, res.get("error"))
check("  Aurora side materialized", bool(aurora_sql_seen))
check("  DynamoDB side fully scanned", full_scan(), scan_calls)
check("  equals DuckDB over the full set", out is not None and same(out, oracle(sql)))
sql = (f"SELECT r.item_id FROM {REF} r WHERE r.product_id IN "
       f"(SELECT product_id FROM {AURORA_REF}) LIMIT 4")
res, out = run(sql, aurora=True)
check("Aurora in a subquery: DynamoDB side fully scanned", full_scan(), scan_calls)
check("  equals DuckDB over the full set", out is not None and same(out, oracle(sql)))
sm._get_routing_catalogs = _real_routing

# 4. cache: a pushed-down scan bypasses (and never fills) the 5-minute cache
print("\nscan cache")
sm._scan_dynamodb_table._cache = {}
sm._scan_dynamodb_table._cache_ts = {}
scan_calls.clear()
sm._scan_dynamodb_table(TABLE, "us-west-2", limit=5)
check("limited scan is not cached", sm._scan_dynamodb_table._cache == {})
sm._scan_dynamodb_table(TABLE, "us-west-2")
n_after_full = len(scan_calls)
sm._scan_dynamodb_table(TABLE, "us-west-2")
check("full scan is cached (second call no scan)", len(scan_calls) == n_after_full)
sm._scan_dynamodb_table(TABLE, "us-west-2", limit=5)
check("limited scan bypasses the cache", len(scan_calls) == n_after_full + 1)

# 5. the helper on its own
print("\n_dynamo_scan_limit")
HELPER = [
    (f"SELECT * FROM {REF} LIMIT 5", 5),
    (f"SELECT a, b AS c FROM {REF} LIMIT 5;", 5),
    (f"SELECT * FROM {REF}", None),
    (f"SELECT * FROM {REF} WHERE a = 1 LIMIT 5", None),
    (f"SELECT a, COUNT(*) FROM {REF} GROUP BY a LIMIT 5", None),
    (f"SELECT * FROM {REF} ORDER BY a LIMIT 5", None),
    (f"SELECT a FROM {REF} GROUP BY a HAVING COUNT(*) > 1 LIMIT 5", None),
    (f"SELECT COUNT(*) FROM {REF} LIMIT 1", None),
    (f"SELECT DISTINCT a FROM {REF} LIMIT 3", None),
    (f"SELECT a, ROW_NUMBER() OVER (ORDER BY a) FROM {REF} LIMIT 3", None),
    (f"SELECT * FROM {REF} x JOIN df y ON x.a = y.a LIMIT 3", None),
    (f"SELECT * FROM {REF}, df LIMIT 3", None),
    (f"SELECT * FROM (SELECT * FROM {REF}) LIMIT 3", None),
    (f"SELECT (SELECT 1) AS one FROM {REF} LIMIT 3", None),
    (f"WITH c AS (SELECT * FROM {REF}) SELECT * FROM c LIMIT 3", None),
    (f"SELECT * FROM {REF} UNION ALL SELECT * FROM {REF} LIMIT 3", None),
    (f"SELECT * FROM {REF} LIMIT 3 OFFSET 2", None),
    (f"SELECT UNNEST([1, 2]) FROM {REF} LIMIT 3", None),
    (f"SELECT * FROM {REF} QUALIFY a > 1 LIMIT 3", None),
    (f"SELECT * FROM {REF} LIMIT 0", None),
    ("not sql ((", None),
]
for sql, want in HELPER:
    got = sm._dynamo_scan_limit(sql)
    check(f"{want!s:>4}  {sql}", got == want, got)

print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
