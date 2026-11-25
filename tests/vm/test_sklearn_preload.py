#!/usr/bin/env python3
# test-kind: offline
"""
app/server.py preload block: scikit-learn is imported at module level (so it is in
the image snapshot), after dill.extend(use_dill=False), and the imports are safe to
run at image build: no network, no new threads, no boto3.
Sections:
  [ast]     the 8 module-level `import sklearn...` statements exist and come after
            the dill.extend call.
  [import]  a subprocess with every AWS_* variable removed and socket connects
            patched to raise imports the same 8 modules: exit 0, thread count
            unchanged, boto3 not imported.
Run:
    .venv/bin/python tests/vm/test_sklearn_preload.py
"""
import ast
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.path.join(REPO_ROOT, "app", "server.py")

MODULES = [
    "sklearn",
    "sklearn.ensemble",
    "sklearn.pipeline",
    "sklearn.compose",
    "sklearn.preprocessing",
    "sklearn.model_selection",
    "sklearn.metrics",
    "sklearn.linear_model",
]

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  [{detail}]" if not ok and detail else ""))


def test_ast():
    print("[ast]")
    with open(SERVER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    dill_line = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "extend"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("dill", "_dill")):
            dill_line = node.lineno
            break
    check("dill.extend call found", dill_line is not None)
    found = {}
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in MODULES:
                    found[a.name] = node.lineno
    for m in MODULES:
        check(f"module-level `import {m}`", m in found)
        if m in found and dill_line is not None:
            check(f"`import {m}` after dill.extend", found[m] > dill_line,
                  f"line {found[m]} vs {dill_line}")


_CHILD = r"""
import json, socket, sys, threading
def _deny(*a, **k):
    raise OSError("network disabled in test")
socket.socket.connect = _deny
socket.create_connection = _deny
mods = json.loads(sys.argv[1])
threads_before = threading.active_count()
boto3_before = "boto3" in sys.modules
import importlib
for m in mods:
    importlib.import_module(m)
print(json.dumps({
    "threads_before": threads_before,
    "threads_after": threading.active_count(),
    "boto3_before": boto3_before,
    "boto3_after": "boto3" in sys.modules,
}))
"""


def test_import():
    print("[import]")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    p = subprocess.run([sys.executable, "-c", _CHILD, json.dumps(MODULES)],
                       env=env, capture_output=True, text=True, timeout=300)
    check("subprocess exits 0", p.returncode == 0, p.stderr[-500:])
    if p.returncode != 0:
        return
    out = json.loads(p.stdout.strip().splitlines()[-1])
    check("thread count unchanged", out["threads_after"] == out["threads_before"], str(out))
    check("boto3 not newly imported", out["boto3_after"] == out["boto3_before"], str(out))


def main():
    test_ast()
    test_import()
    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
