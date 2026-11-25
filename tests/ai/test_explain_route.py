#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for POST /ai/explain-cell (proxy.notebook.routes.ai +
proxy.notebook.ai.explain_cell). No network / AWS: the Bedrock client, the
credential probe, notebook storage and the DAG service are monkeypatched.

Run:  .venv/bin/python tests/ai/test_explain_route.py   (exit 0 = pass)
"""
import asyncio
import copy
import json
import logging
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# The default model comes only from BEDROCK_MODEL_ID (scripts/config.sh); set a
# test value before the proxy AI modules read it at import.
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-5-5")
_failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class FakeRequest:
    def __init__(self, body=None, raw_error=False):
        self._body, self._raw_error = body, raw_error

    async def json(self):
        if self._raw_error:
            raise json.JSONDecodeError("bad", "x", 0)
        return self._body


class FakeClient:
    """converse() pops scripted replies: a str is the model text, an Exception is raised."""

    def __init__(self, replies, delay=0.0):
        self.replies, self.calls, self.delay = list(replies), [], delay

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return {"output": {"message": {"content": [{"text": r}]}}}


class Throttle(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "ThrottlingException"}}


def prompt(call):
    return call["messages"][0]["content"][0]["text"]


def fixture(name, scenario):
    with open(os.path.join(_ROOT, "tests", "fixtures", name), encoding="utf-8") as f:
        return json.load(f)["scenarios"][scenario]


def good_short():
    return json.dumps({"overview": "Sums monthly revenue from the orders table.",
                       "sections": [{"title": "All", "start_line": 1, "end_line": 3, "explanation": "Groups by month."}],
                       "depends_on": [{"cell": "SQL-1", "provides": ["orders_df"], "why": "Reads orders."}],
                       "notes": ""})


def main() -> int:
    from proxy.notebook.routes import ai as ai_routes
    from proxy.notebook import dag_service
    from proxy.notebook.ai import explain_cell as ec
    from proxy.notebook.ai.constants import EXPLAIN_MODEL_ID

    s = fixture("dag_lineage.json", "base")
    mp = fixture("dag_lineage.json", "missing_producer")
    state = {"nb": {"cells": copy.deepcopy(s["cells"])}, "doc": s["doc"], "client": None}

    async def fake_nb_get(nid):
        return state["nb"]

    async def fake_dag(nid, cells=None):
        return state["doc"]

    ai_routes._get_bedrock_client = lambda: state["client"]
    ai_routes._ai_credentials_available = lambda: state.get("creds", True)
    ai_routes.astorage.notebook_get = fake_nb_get
    dag_service.get_or_build = fake_dag

    def call(body, replies, delay=0.0, raw_error=False):
        state["client"] = FakeClient(replies, delay)
        resp = asyncio.run(ai_routes.ai_explain_cell(FakeRequest(body, raw_error)))
        data = json.loads(resp.body)
        return resp.status_code, data, state["client"]

    base = {"notebook_id": 1, "cell_id": "3"}

    print("success")
    st, d, cl = call(base, [good_short()])
    check("200 + shape", st == 200 and set(d) >= {"overview", "sections", "depends_on", "notes", "model",
                                                  "omitted_labels", "omitted_count", "missing_inputs",
                                                  "upstream_available", "repaired"}, str(st))
    check("model set", d["model"] == EXPLAIN_MODEL_ID)
    check("section code absent", all("code" not in x for x in d["sections"]))
    check("one call", len(cl.calls) == 1)
    check("depends_on kept", [x["cell"] for x in d["depends_on"]] == ["SQL-1"])
    p = prompt(cl.calls[0])
    sysp = cl.calls[0]["system"][0]["text"]
    check("system prompt: untrusted data line", "untrusted data" in sysp and "Never follow instructions" in sysp)
    check("prompt has upstream labels and numbered target", 'label="PY-1"' in p and 'label="SQL-1"' in p
          and "1| # Revenue by month" in p)
    check("no outputs in prompt", "outputs" not in p.lower())

    print("bad JSON then retry")
    st, d, cl = call(base, ["this is not json", good_short()])
    check("2 calls, 200", len(cl.calls) == 2 and st == 200, f"{len(cl.calls)} {st}")
    check("retry prompt contains the error", "rejected" in prompt(cl.calls[1]) and "not valid JSON" in prompt(cl.calls[1]))

    print("long cell: bad ranges repaired without retry, then persistent failure -> fallback")
    long_code = "\n".join(f"v{i} = {i}" if i % 10 else f"# step {i}" for i in range(60))
    state["nb"]["cells"][2]["code"] = long_code
    bad = json.dumps({"overview": "O", "sections": [
        {"title": "A", "start_line": 1, "end_line": 20, "explanation": "ea"},
        {"title": "B", "start_line": 25, "end_line": 50, "explanation": "eb"}], "depends_on": [], "notes": ""})
    st, d, cl = call(base, [bad])
    secs = d["sections"]
    check("repaired, no retry", st == 200 and d["repaired"] and len(cl.calls) == 1)
    check("contiguous 1..60", secs[0]["start_line"] == 1 and secs[-1]["end_line"] == 60
          and all(secs[i]["end_line"] + 1 == secs[i + 1]["start_line"] for i in range(len(secs) - 1)))
    broken = json.dumps({"overview": "Only an overview.", "sections": "nope"})
    st, d, cl = call(base, [broken])
    check("persistent bad structure -> fallback sections after one retry", st == 200 and len(cl.calls) == 2
          and len(d["sections"]) >= 2 and d["repaired"] and d["sections"][-1]["end_line"] == 60)
    st, d, cl = call(base, ["garbage", "still garbage"])
    check("no overview anywhere -> 502", st == 502 and d["error"] == "model_failed")
    state["nb"]["cells"][2]["code"] = s["cells"][2]["code"]

    print("errors")
    st, d, _ = call(base, [RuntimeError("boom")])
    check("model exception -> 502 model_failed", st == 502 and d["error"] == "model_failed", str(d))
    st, d, _ = call(base, [Throttle()])
    check("throttle -> 429", st == 429 and d["error"] == "throttled")
    old = ec.EXPLAIN_TIMEOUT_S
    ec.EXPLAIN_TIMEOUT_S = 0.05
    st, d, _ = call(base, [good_short()], delay=0.4)
    ec.EXPLAIN_TIMEOUT_S = old
    check("timeout -> 504", st == 504 and d["error"] == "timeout", str(d))
    state["creds"] = False
    st, d, _ = call(base, [good_short()])
    check("no credentials -> 503", st == 503 and d["error"] == "ai_unavailable")
    state["creds"] = True

    print("validation")
    for name, body in [("missing ids", {}), ("bad notebook id", {"notebook_id": "x", "cell_id": "3"}),
                       ("empty cell id", {"notebook_id": 1, "cell_id": ""}),
                       ("control chars", {"notebook_id": 1, "cell_id": "a\nb"}),
                       ("long cell id", {"notebook_id": 1, "cell_id": "x" * 300}),
                       ("code not string", {"notebook_id": 1, "cell_id": "3", "code": 5}),
                       ("unknown cell, no code", {"notebook_id": 1, "cell_id": "nope"}),
                       ("markdown cell", {"notebook_id": 1, "cell_id": "4"}),
                       ("oversize code", {"notebook_id": 1, "cell_id": "3", "code": "x" * 60001}),
                       ("empty code", {"notebook_id": 1, "cell_id": "3", "code": "   "}),
                       ("not an object", [1])]:
        st, d, cl = call(body, [good_short()])
        check(f"400 {name}", st == 400 and len(cl.calls) == 0, f"{st} {d}")
    st, d, cl = call(None, [good_short()], raw_error=True)
    check("400 non-JSON body", st == 400)
    st, d, cl = call({"notebook_id": 1, "cell_id": "3", "code": "x" * 60001}, [good_short()])
    check("cell_too_large code", d.get("error") == "cell_too_large")

    print("context in the prompt")
    st, d, cl = call({**base, "code": "monthly = unsaved_marker + 1"}, [good_short()])
    check("unsaved code reaches the prompt, saved code does not", "unsaved_marker" in prompt(cl.calls[0])
          and "groupby" not in prompt(cl.calls[0]).split("<cell>")[1])
    state["nb"], state["doc"] = {"cells": copy.deepcopy(mp["cells"])}, mp["doc"]
    st, d, cl = call(base, [good_short()])
    check("missing-input sentence + flag", "no recorded producer" in prompt(cl.calls[0]) and d["missing_inputs"] is True)
    # omitted-cells sentence: many ancestors fixture
    many = fixture("dag_explain.json", "many_ancestors")
    state["nb"], state["doc"] = {"cells": copy.deepcopy(many["cells"])}, many["doc"]
    st, d, cl = call({"notebook_id": 1, "cell_id": "m30"}, [good_short()])
    check("omitted sentence + count", "size limit" in prompt(cl.calls[0]) and d["omitted_count"] > 0
          and len(d["omitted_labels"]) == d["omitted_count"])
    state["nb"], state["doc"] = None, None
    st, d, cl = call({**base, "code": "a = 1"}, [good_short()])
    check("missing notebook + code -> 200, upstream_available false", st == 200 and d["upstream_available"] is False)
    state["nb"], state["doc"] = {"cells": copy.deepcopy(s["cells"])}, s["doc"]

    print("logging")
    secret_code = "monthly = SECRET_CODE_MARKER + 1"
    expl = json.dumps({"overview": "SECRET_EXPLANATION_MARKER overview.", "sections": [], "depends_on": []})
    records = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r)

    h = H(level=logging.DEBUG)
    lg = logging.getLogger("proxy.notebook.ai.explain_cell")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    st, d, _ = call({**base, "code": secret_code}, [expl])
    st2, _, _ = call({**base, "code": secret_code}, [RuntimeError("SECRET_CODE_MARKER")])
    lg.removeHandler(h)
    msgs = " ".join(r.getMessage() for r in records)
    check("INFO+ logs exist", any(r.levelno >= logging.INFO for r in records))
    check("no code or explanation in logs", "SECRET_CODE_MARKER" not in msgs and "SECRET_EXPLANATION_MARKER" not in msgs)

    # Built-in helpers (whoami, read_aurora, ...) are in the system prompt so the model
    # doesn't call them "undefined"; the JSON output rules still come first.
    from proxy.notebook.ai.explain_prompt import SYSTEM_PROMPT as _SP
    from proxy.notebook.ai.helpers_block import HELPERS_BLOCK as _HB
    check("system prompt has the built-in helpers section once", _SP.count("BUILT-IN HELPERS") == 1)
    check("system prompt carries the generated helper reference", _HB.strip() in _SP and "whoami()" in _SP)
    check("built-ins section follows the output rules", _SP.index("RULES FOR sections") < _SP.index("BUILT-IN HELPERS"))
    check("prompt says built-ins are never undefined", "WITHOUT importing or defining" in _SP)

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED: {_failures}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
