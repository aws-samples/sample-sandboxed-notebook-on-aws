#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for POST /ai/explain-lineage (proxy.notebook.routes.ai).

No network / AWS: the Bedrock client and the credential probe are monkeypatched.
Covers: two-sentence trimming, LRU cache hit, cache miss on changed input, prompt
contents (columns + source ids), code truncation, model-error degradation (not
cached), no-credentials degradation, 400 on empty steps, and the inference config.

Run:  .venv/bin/python tests/ai/test_explain_lineage_route.py   (exit 0 = pass)
"""
import asyncio
import copy
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# The default model comes only from BEDROCK_MODEL_ID (scripts/config.sh); set a
# test value before the proxy AI modules read it at import.
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-5-5")
_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class FakeRequest:
    def __init__(self, body=None, raw_error=False):
        self._body = body
        self._raw_error = raw_error

    async def json(self):
        if self._raw_error:
            raise json.JSONDecodeError("bad", "x", 0)
        return self._body


class FakeClient:
    def __init__(self, text="Reads orders from the catalog. Sums revenue by region. Extra sentence.",
                 raise_exc=None):
        self.calls = []
        self.text = text
        self.raise_exc = raise_exc

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return {"output": {"message": {"content": [{"text": self.text}]}}}


BASE = {
    "output": {"var": "by_region", "kind": "DataFrame",
               "columns": ["region", "total_revenue"], "rows": 4, "cols": 2},
    "steps": [
        {"badge": "[1]", "type": "sql",
         "code": "SELECT * FROM microvm_demo_db.orders"},
        {"badge": "[2]", "type": "code",
         "code": "by_region = orders.groupby('region')['revenue'].sum().reset_index()"},
    ],
    "sources": [{"kind": "athena", "id": "microvm_demo_db.orders"}],
}


def _prompt(call):
    return call["messages"][0]["content"][0]["text"]


def main() -> int:
    from proxy.notebook.routes import ai as ai_routes
    from proxy.notebook.ai.constants import (
        LINEAGE_MAX_CODE_CHARS, LINEAGE_MAX_TOKENS, LINEAGE_TEMPERATURE,
        LINEAGE_SUMMARY_CHARS,
    )
    from modelcaps import build_inference_config

    orig_client = ai_routes._get_bedrock_client
    orig_creds = ai_routes._ai_credentials_available
    client = FakeClient()
    ai_routes._get_bedrock_client = lambda: client
    ai_routes._ai_credentials_available = lambda: True

    def run(body, **kw):
        return asyncio.run(ai_routes.ai_explain_lineage(FakeRequest(body, **kw)))

    try:
        print("== 1. summary + cache ==")
        ai_routes._lineage_cache.clear()
        r1 = run(copy.deepcopy(BASE))
        check("summary is the full model reply (no truncation in code)",
              r1.get("summary") == "Reads orders from the catalog. Sums revenue by region. Extra sentence.",
              repr(r1))
        check("first call not cached", r1.get("cached") is False)
        r2 = run(copy.deepcopy(BASE))
        check("identical second call is cached", r2.get("cached") is True and r2["summary"] == r1["summary"])
        check("converse called exactly once", len(client.calls) == 1, str(len(client.calls)))

        changed = copy.deepcopy(BASE)
        changed["steps"][1]["code"] += "\nby_region = by_region.head(3)"
        r3 = run(changed)
        check("changed code → new model call", len(client.calls) == 2 and r3.get("cached") is False)

        print("== 2. prompt contents + request params ==")
        p = _prompt(client.calls[0])
        check("prompt contains output column names", "region" in p and "total_revenue" in p)
        check("prompt contains source id", "microvm_demo_db.orders" in p)
        check("prompt contains step badge + code", "[2]" in p and "groupby('region')" in p)
        check("prompt contains output var + shape", "by_region" in p and "4 rows x 2 columns" in p)
        check("prompt states the char budget", f"at most {LINEAGE_SUMMARY_CHARS} characters" in p)
        call = client.calls[0]
        check("modelId is LINEAGE_MODEL_ID", call["modelId"] == ai_routes.LINEAGE_MODEL_ID)
        check("inferenceConfig per build_inference_config",
              call["inferenceConfig"] == build_inference_config(
                  ai_routes.LINEAGE_MODEL_ID, max_tokens=LINEAGE_MAX_TOKENS,
                  temperature=LINEAGE_TEMPERATURE),
              repr(call["inferenceConfig"]))

        big = copy.deepcopy(BASE)
        big["steps"][1]["code"] = "x = 1\n" + ("Z" * (LINEAGE_MAX_CODE_CHARS * 2))
        run(big)
        bp = _prompt(client.calls[-1])
        check("oversized code truncated to LINEAGE_MAX_CODE_CHARS",
              bp.count("Z") == LINEAGE_MAX_CODE_CHARS - len("x = 1\n"), str(bp.count("Z")))

        print("== 3. graceful failures ==")
        ai_routes._lineage_cache.clear()
        bad = FakeClient(raise_exc=RuntimeError("boom"))
        ai_routes._get_bedrock_client = lambda: bad
        r4 = run(copy.deepcopy(BASE))
        check("model error → summary None + error",
              r4.get("summary") is None and r4.get("error") == "Summary unavailable", repr(r4))
        check("model error not cached", len(ai_routes._lineage_cache) == 0)

        ai_routes._get_bedrock_client = lambda: client
        ai_routes._ai_credentials_available = lambda: False
        r5 = run(copy.deepcopy(BASE))
        check("no credentials → summary None", r5.get("summary") is None and "error" in r5)
        ai_routes._ai_credentials_available = lambda: True

        r6 = run({"output": {}, "steps": [], "sources": []})
        check("empty steps → 400", getattr(r6, "status_code", None) == 400)
        r7 = run(None, raw_error=True)
        check("bad JSON → 400", getattr(r7, "status_code", None) == 400)
    finally:
        ai_routes._get_bedrock_client = orig_client
        ai_routes._ai_credentials_available = orig_creds
        ai_routes._lineage_cache.clear()

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("RESULT: all checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
