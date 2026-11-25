#!/usr/bin/env python3
# test-kind: offline
"""
Lint (offline): direct boto3.client(...) / boto3.resource(...) calls under app/ are
only allowed in the files listed in ALLOW, each with the reason it is
exempt. Everything else goes through app.notebook.aws_clients.
Run: .venv/bin/python tests/vm/test_no_direct_boto3.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ALLOW = {
    "app/notebook/aws_clients.py": "the factory itself (the only place that builds clients for VM-side code)",
    "app/server.py": "warmup uses a throwaway boto3.Session so build-role credentials are not frozen into a cached client",
    "app/platform/hooks.py": "launch-time secretsmanager read and staged-catalog S3 fetch (platform plumbing, runs before the factory is relevant)",
    "app/platform/checkpoint/manager.py": "own cached S3 client with a custom Config and its own reset on credential rotation; STS call for the account id",
    "app/notebook/helpers/aurora.py": "rds-data client; moves behind the factory with the optional Phase 5 (QueryBackend)",
}

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


def sites(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("client", "resource")
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "boto3"):
            found.append(node.lineno)
    return found


offenders = {}
seen = set()
for dp, dn, fn in os.walk(os.path.join(ROOT, "app")):
    dn[:] = [d for d in dn if d not in ("__pycache__", ".venv", "node_modules")]
    for f in fn:
        if not f.endswith(".py"):
            continue
        full = os.path.join(dp, f)
        rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
        s = sites(full)
        if s:
            seen.add(rel)
            if rel not in ALLOW:
                offenders[rel] = s

check("no direct boto3.client/resource outside the allow-list", not offenders, offenders)
stale = sorted(p for p in ALLOW if p not in seen and p != "app/server.py")
check("allow-list has no stale entry (remove migrated files)", not stale, stale)

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
