#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: the public helper API (names and inspect.signature of everything
exported from app.notebook.helpers) must not change. The fixture
tests/vm/helpers_api_snapshot.json was captured before the data-source refactor.
Also checks the generated AI helper block / fallback JSON agree with the snapshot
and that scripts/gen_helpers_block.py --check passes.
Intentional API change: regenerate the fixture AND run scripts/gen_helpers_block.py.
Run: .venv/bin/python tests/vm/test_helpers_api_snapshot.py
"""
import inspect
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


with open(os.path.join(ROOT, "tests", "vm", "helpers_api_snapshot.json"), encoding="utf-8") as f:
    snap = json.load(f)

import app.notebook.helpers as h  # noqa: E402
from app.notebook.completion_catalog import build_helpers_meta  # noqa: E402

cur = {}
for n in sorted(dir(h)):
    if n.startswith("_"):
        continue
    o = getattr(h, n)
    if not callable(o):
        continue
    try:
        cur[n] = str(inspect.signature(o))
    except (ValueError, TypeError):
        cur[n] = None

check("no public name added or removed", sorted(cur) == sorted(snap["api"]),
      f"added={sorted(set(cur) - set(snap['api']))} removed={sorted(set(snap['api']) - set(cur))}")
changed = [n for n in snap["api"] if n in cur and cur[n] != snap["api"][n]]
check("no signature changed", not changed, changed)
for must in ("read_athena", "read_dynamodb", "read_s3_csv", "to_s3_csv", "sm_jobs",
             "unload_athena_to_s3"):
    check(f"snapshot lists {must}", must in snap["api"])

meta = [{"name": m["name"], "signature": m["signature"], "category": m["category"]}
        for m in build_helpers_meta()]
check("build_helpers_meta equals snapshot", meta == snap["meta"])

from scripts import gen_helpers_block as gen  # noqa: E402

block = gen.build_block()
missing = []
for m in snap["meta"]:
    # The block shows the cleaned signature of each helper.
    cleaned = gen._clean_signature(m["name"], m["signature"])
    if cleaned not in block:
        missing.append(m["name"])
check("every helper appears in the generated block with its signature", not missing, missing[:5])
fallback = json.loads(gen.build_fallback_json())
names = set()
stack = [fallback]
while stack:
    x = stack.pop()
    if isinstance(x, dict):
        if isinstance(x.get("name"), str):
            names.add(x["name"])
        stack.extend(x.values())
    elif isinstance(x, list):
        stack.extend(x)
check("fallback JSON lists every helper", {m["name"] for m in snap["meta"]} <= names)

r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen_helpers_block.py"), "--check"],
                   capture_output=True, text=True, cwd=ROOT)
check("gen_helpers_block.py --check exits 0", r.returncode == 0, (r.stderr or r.stdout)[-200:])

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
