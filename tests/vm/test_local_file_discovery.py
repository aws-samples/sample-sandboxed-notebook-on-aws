#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review A-B-11): local /tmp file discovery in the VM data catalog.

Against a temporary root (DataCatalog._discover_local_files(root=...)):
- parquet, csv, JSON array, JSON object, JSON Lines and .txt files are discovered
  (.txt with zero columns on purpose), with the right row counts;
- parquet is read from its footer and a 5-row batch: pd.read_parquet is patched
  to raise, discovery still succeeds;
- a second pass does not re-read unchanged files, a changed file is re-read;
- a deleted file is pruned from the catalog.

Run: .venv/bin/python tests/vm/test_local_file_discovery.py  (exits non-zero on failure)
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import logging  # noqa: E402
import pandas as pd  # noqa: E402
from app.notebook import data_catalog as dc  # noqa: E402
logging.disable(logging.WARNING)

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


def _no_full_parquet_read(*a, **k):
    raise AssertionError("pd.read_parquet must not be used by discovery")


pd.read_parquet = _no_full_parquet_read

tmp = tempfile.mkdtemp(prefix="rf16-local-")
paths = {
    "parquet": os.path.join(tmp, "sales.parquet"),
    "csv": os.path.join(tmp, "orders.csv"),
    "json_array": os.path.join(tmp, "records.json"),
    "json_object": os.path.join(tmp, "columns.json"),
    "jsonl": os.path.join(tmp, "events.json"),
    "txt": os.path.join(tmp, "notes.txt"),
}
pd.DataFrame({"id": range(1000), "amount": [i * 1.5 for i in range(1000)]}).to_parquet(
    paths["parquet"], row_group_size=100)
with open(paths["csv"], "w") as f:
    f.write("id,name\n" + "".join(f"{i},n{i}\n" for i in range(42)))
with open(paths["json_array"], "w") as f:
    json.dump([{"a": i, "b": f"x{i}"} for i in range(12)], f)
with open(paths["json_object"], "w") as f:
    json.dump({"k": [1, 2, 3], "v": ["p", "q", "r"]}, f)
with open(paths["jsonl"], "w") as f:
    f.write("".join(json.dumps({"event": i, "ok": True}) + "\n" for i in range(7)))
with open(paths["txt"], "w") as f:
    f.write("free text\n")

reads = []
_orig_schema = dc.DataCatalog._schema_for_local_file


def counting_schema(self, filepath):
    reads.append(filepath)
    return _orig_schema(self, filepath)


dc.DataCatalog._schema_for_local_file = counting_schema
cat = dc.DataCatalog()
cat._discover_local_files(root=tmp)
by_id = {e["source_id"]: e for e in cat.get_all()["entries"]}


def cols(key):
    return [c["name"] for c in by_id.get(paths[key], {}).get("columns", [])]


def rows(key):
    return by_id.get(paths[key], {}).get("row_count")


print("\nfirst pass")
check("every file discovered", all(p in by_id for p in paths.values()), sorted(by_id))
check("parquet columns from the schema", cols("parquet") == ["id", "amount"], cols("parquet"))
check("parquet row count from the footer", rows("parquet") == 1000, rows("parquet"))
check("parquet sample is the first row", by_id[paths["parquet"]]["columns"][0]["sample"] == "0")
check("csv columns", cols("csv") == ["id", "name"], cols("csv"))
check("csv row count (binary line count minus header)", rows("csv") == 42, rows("csv"))
check("JSON array columns", cols("json_array") == ["a", "b"], cols("json_array"))
check("JSON array row count", rows("json_array") == 12, rows("json_array"))
check("JSON object columns", cols("json_object") == ["k", "v"], cols("json_object"))
check("JSON object row count", rows("json_object") == 3, rows("json_object"))
check("JSON Lines columns", cols("jsonl") == ["event", "ok"], cols("jsonl"))
check("JSON Lines row count", rows("jsonl") == 7, rows("jsonl"))
check(".txt listed with zero columns", paths["txt"] in by_id and cols("txt") == [], cols("txt"))
check("each file read once", sorted(reads) == sorted(paths.values()), reads)

print("\nsecond pass")
reads.clear()
cat._discover_local_files(root=tmp)
check("unchanged files are not re-read", reads == [], reads)
check("entries kept", all(p in {e["source_id"] for e in cat.get_all()["entries"]}
                          for p in paths.values()))

time.sleep(0.01)
with open(paths["csv"], "a") as f:
    f.write("42,n42\n")
reads.clear()
cat._discover_local_files(root=tmp)
by_id = {e["source_id"]: e for e in cat.get_all()["entries"]}
check("changed file re-read (only that one)", reads == [paths["csv"]], reads)
check("  new row count", rows("csv") == 43, rows("csv"))

print("\ndelete")
os.remove(paths["jsonl"])
reads.clear()
cat._discover_local_files(root=tmp)
ids = {e["source_id"] for e in cat.get_all()["entries"]}
check("deleted file pruned", paths["jsonl"] not in ids, sorted(ids))
check("  others kept, nothing re-read", reads == [] and paths["csv"] in ids, reads)
check("  signature dropped with the entry", paths["jsonl"] not in cat._local_sigs)

shutil.rmtree(tmp, ignore_errors=True)
print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
