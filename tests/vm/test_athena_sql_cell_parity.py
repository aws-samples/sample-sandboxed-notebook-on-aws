#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: a SQL cell on Athena and read_athena return the SAME frame for the
same result set (NULL as missing/None, never ""; dtypes from Athena's declared
column types, not guessed by pd.to_numeric). Fake Athena client, no AWS.
Needs duckdb (imports sql_engine). Run: .venv/bin/python tests/vm/test_athena_sql_cell_parity.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["AWS_REGION"] = "us-west-2"
os.environ["ARTIFACT_BUCKET"] = "art-bkt"
try:
    import duckdb  # noqa: F401
except ImportError:
    print("SKIP  duckdb not installed")
    sys.exit(0)
import logging  # noqa: E402
import boto3  # noqa: E402
import pandas as pd  # noqa: E402
from app.notebook import sql_engine as sm  # noqa: E402
import app.notebook.helpers as H  # noqa: E402

logging.disable(logging.CRITICAL)
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


COLS = [{"Name": "id", "Type": "bigint"}, {"Name": "price", "Type": "double"},
        {"Name": "name", "Type": "varchar"}, {"Name": "n_nullable", "Type": "integer"}]


def row(*vals):
    return {"Data": [({"VarCharValue": v} if v is not None else {}) for v in vals]}


PAGES = [
    {"ResultSet": {"Rows": [row("id", "price", "name", "n_nullable"),
                            row("1", "1.5", "a", "7"), row("2", None, None, None)],
                   "ResultSetMetadata": {"ColumnInfo": COLS}}, "NextToken": "1"},
    {"ResultSet": {"Rows": [row("3", "3.25", "", "9")],
                   "ResultSetMetadata": {"ColumnInfo": COLS}}},
]


class FakeAthena:
    def start_query_execution(self, **kw):
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, QueryExecutionId, NextToken=None):
        return PAGES[0 if NextToken is None else 1]


boto3.client = lambda service, **kw: FakeAthena()

sql_df = sm._run_athena_query_sync("SELECT * FROM salesdb.orders", "wg", "us-west-2")
read_df = H.read_athena("SELECT * FROM salesdb.orders", database="salesdb")

try:
    pd.testing.assert_frame_equal(sql_df, read_df)
    check("assert_frame_equal(SQL cell, read_athena)", True)
except AssertionError as e:
    check("assert_frame_equal(SQL cell, read_athena)", False, str(e)[:300])
check("SQL cell: NULL varchar is missing, an empty string stays \"\"",
      pd.isna(sql_df["name"].iloc[1]) and sql_df["name"].iloc[2] == "")
check("SQL cell: nullable integer keeps its type (not strings)",
      str(sql_df["n_nullable"].dtype) == "Int64" and sql_df["n_nullable"].iloc[2] == 9,
      sql_df["n_nullable"].dtype)
check("SQL cell: double column is float64", str(sql_df["price"].dtype) == "float64")
check("SQL cell: no empty-string NULLs anywhere",
      not (sql_df.astype(object) == "").iloc[[0, 1]].any().any())

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
