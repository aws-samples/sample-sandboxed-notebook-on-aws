#!/usr/bin/env python3
# test-kind: offline
"""
MicroVM lifecycle hook timeouts vs the suspend save budget.

Reads the `--hooks` JSON from scripts/build_all_images.sh (the only place the
hook timeouts are set) and SUSPEND_SAVE_BUDGET_SECONDS /
SUSPEND_SAVE_ABORT_GRACE_SECONDS from app/platform/hooks.py (with ast, no
import), and asserts:
  - suspendTimeoutInSeconds == 60 (a hook that runs past it terminates the VM);
  - the budget is at least 10 s below the suspend timeout;
  - budget + grace < the suspend timeout (the hook returns before it);
  - every *TimeoutInSeconds is at most 60 (the service cap);
  - resume 10, run 60, terminate 60 (unchanged).
HOOKS_SCRIPT=<path> checks another copy of the build script (used to show the
test fails with the old suspend timeout of 10 s).
Run:
    .venv/bin/python tests/build/test_hook_timeouts.py
"""
import ast
import json
import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.environ.get("HOOKS_SCRIPT") or os.path.join(REPO_ROOT, "scripts", "build_all_images.sh")
HOOKS_PY = os.path.join(REPO_ROOT, "app", "platform", "hooks.py")

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def hooks_json() -> dict:
    with open(SCRIPT) as f:
        text = f.read()
    found = re.findall(r"--hooks\s+'(\{.*?\})'", text)
    check("build script has exactly one --hooks JSON", len(found) == 1, f"found {len(found)}")
    return json.loads(found[0]) if found else {}


def module_constants(path: str) -> dict:
    with open(path) as f:
        tree = ast.parse(f.read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, (int, float)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = float(node.value.value)
    return out


def main() -> int:
    hooks = hooks_json().get("microvmHooks", {})
    consts = module_constants(HOOKS_PY)
    budget = consts.get("SUSPEND_SAVE_BUDGET_SECONDS")
    grace = consts.get("SUSPEND_SAVE_ABORT_GRACE_SECONDS")
    suspend = hooks.get("suspendTimeoutInSeconds")
    print(f"suspendTimeoutInSeconds={suspend} budget={budget} grace={grace}")
    check("hooks.py defines SUSPEND_SAVE_BUDGET_SECONDS", budget is not None)
    check("hooks.py defines SUSPEND_SAVE_ABORT_GRACE_SECONDS", grace is not None)
    check("suspendTimeoutInSeconds == 60", suspend == 60, f"got {suspend}")
    if budget is not None and isinstance(suspend, (int, float)):
        check("suspend timeout - budget >= 10 s", suspend - budget >= 10, f"{suspend} - {budget}")
        if grace is not None:
            check("budget + grace < suspend timeout", budget + grace < suspend, f"{budget} + {grace} vs {suspend}")
    timeouts = {k: v for k, v in hooks.items() if k.endswith("TimeoutInSeconds")}
    check("every hook timeout <= 60 s (service cap)", all(v <= 60 for v in timeouts.values()), str(timeouts))
    check("resumeTimeoutInSeconds == 10", hooks.get("resumeTimeoutInSeconds") == 10)
    check("runTimeoutInSeconds == 60", hooks.get("runTimeoutInSeconds") == 60)
    check("terminateTimeoutInSeconds == 60", hooks.get("terminateTimeoutInSeconds") == 60)
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
