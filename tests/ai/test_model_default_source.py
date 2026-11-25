#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the default model id source (code-review C-I-01, C-I-02).

scripts/config.sh (BEDROCK_MODEL_ID, BEDROCK_KB_GENERATION_MODEL) is the only
place that names the default models. Covers:
  - no string literal naming a Claude model ("claude-") in proxy/, agent/ai_assistant/,
    app/ or batch/ non-test code, except INTEL_MODEL_ID, the vendored modelcaps
    copies and docstrings (AST scan, so comments are ignored);
  - proxy and agent constants read BEDROCK_MODEL_ID (fresh interpreter each);
    LINEAGE_MODEL_ID / EXPLAIN_MODEL_ID default to it and keep their overrides;
  - unset BEDROCK_MODEL_ID: AI_MODEL_ID is empty and require_model_id raises a
    clear error naming BEDROCK_MODEL_ID;
  - launch_env_vars() forwards BEDROCK_MODEL_ID only when the proxy has it.

Run:
    .venv/bin/python tests/ai/test_model_default_source.py
"""
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


_SCAN_DIRS = ["proxy", os.path.join("agent", "ai_assistant"), "app", "batch"]
_SKIP_DIRS = {"__pycache__", ".venv", "modelcaps", "node_modules", "cdk"}
# The one model pinned in code (Workbook Intel), see proxy/notebook/ai/constants.py.
_ALLOWED_TARGETS = {"INTEL_MODEL_ID"}


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _allowed_constants(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _ALLOWED_TARGETS for t in node.targets):
            for sub in ast.walk(node.value):
                out.add(id(sub))
    return out


def scan_literals():
    hits = []
    for base in _SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), path)
                skip = _docstring_nodes(tree) | _allowed_constants(tree)
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and "claude-" in node.value and id(node) not in skip):
                        hits.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    return hits


_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + "/agent")
from proxy.notebook.ai import constants as pc
from ai_assistant import constants as ac
out = {"proxy": pc.AI_MODEL_ID, "agent": ac.AI_MODEL_ID,
       "lineage": pc.LINEAGE_MODEL_ID, "explain": pc.EXPLAIN_MODEL_ID}
for name, mod in (("proxy_raise", pc), ("agent_raise", ac)):
    try:
        mod.require_model_id(mod.AI_MODEL_ID)
        out[name] = None
    except RuntimeError as e:
        out[name] = str(e)
print(json.dumps(out))
"""


def _probe(extra_env):
    env = {k: v for k, v in os.environ.items()
           if k not in ("BEDROCK_MODEL_ID", "LINEAGE_MODEL_ID", "EXPLAIN_MODEL_ID")}
    env.setdefault("AWS_REGION", "us-west-2")
    env.update(extra_env)
    r = subprocess.run([sys.executable, "-c", _PROBE, ROOT], env=env,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"_error": r.stderr[-800:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_env_source():
    print("\n-- env is the only source --")
    model = "us.anthropic.claude-haiku-5-5"
    d = _probe({"BEDROCK_MODEL_ID": model})
    check("proxy AI_MODEL_ID = BEDROCK_MODEL_ID", d.get("proxy") == model, str(d))
    check("agent AI_MODEL_ID = BEDROCK_MODEL_ID", d.get("agent") == model, str(d))
    check("LINEAGE_MODEL_ID defaults to BEDROCK_MODEL_ID", d.get("lineage") == model, str(d))
    check("EXPLAIN_MODEL_ID defaults to BEDROCK_MODEL_ID", d.get("explain") == model, str(d))
    check("require_model_id passes when set",
          d.get("proxy_raise") is None and d.get("agent_raise") is None, str(d))

    d = _probe({"BEDROCK_MODEL_ID": model, "LINEAGE_MODEL_ID": "us.amazon.nova-lite-v1:0",
                "EXPLAIN_MODEL_ID": "us.amazon.nova-pro-v1:0"})
    check("LINEAGE_MODEL_ID / EXPLAIN_MODEL_ID overrides kept",
          d.get("lineage") == "us.amazon.nova-lite-v1:0"
          and d.get("explain") == "us.amazon.nova-pro-v1:0", str(d))

    d = _probe({})
    check("unset: proxy and agent AI_MODEL_ID empty",
          d.get("proxy") == "" and d.get("agent") == "", str(d))
    check("unset: require_model_id raises naming BEDROCK_MODEL_ID",
          "BEDROCK_MODEL_ID" in (d.get("proxy_raise") or "")
          and "BEDROCK_MODEL_ID" in (d.get("agent_raise") or ""), str(d))


def test_launch_env():
    print("\n-- VM launch env --")
    from proxy.platform.microvm_manager import MicrovmManager
    mgr = MicrovmManager()
    mgr.get_artifacts_bucket = lambda: None
    saved = {k: os.environ.get(k) for k in ("BEDROCK_MODEL_ID", "BEDROCK_KB_ID",
                                            "BEDROCK_KB_GENERATION_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        try:
            env = mgr.launch_env_vars()
        except Exception as e:  # noqa: BLE001
            check("launch_env_vars() callable offline", False, repr(e))
            return
        check("unset: BEDROCK_MODEL_ID not forwarded", "BEDROCK_MODEL_ID" not in env)
        os.environ["BEDROCK_MODEL_ID"] = "us.anthropic.claude-haiku-5-5"
        os.environ["BEDROCK_KB_ID"] = "KBTEST"
        env = mgr.launch_env_vars()
        check("set: BEDROCK_MODEL_ID forwarded as is",
              env.get("BEDROCK_MODEL_ID") == "us.anthropic.claude-haiku-5-5", str(env.get("BEDROCK_MODEL_ID")))
        check("KB without BEDROCK_KB_GENERATION_MODEL: not forwarded",
              "BEDROCK_KB_GENERATION_MODEL" not in env)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    print("\n-- source scan --")
    hits = scan_literals()
    check("no claude- model literal outside INTEL_MODEL_ID/modelcaps/docstrings",
          not hits, ", ".join(hits))
    test_env_source()
    test_launch_env()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
