#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit test: the chat agent sees a variable's LIVE columns.

Root cause being fixed: the agent's only probe of `reviews.columns` was an
approval-gated execute_code that got auto-declined, and nothing approval-free
showed the live columns, so it used the source's raw `productId` instead of the
renamed `product_id`. This pins:
  (a) inspect_variable(name): POSTs /proxy/variables/inspect {name, sample_rows:3}
      with the context's session + proxy-auth headers, renders columns + sample
      rows (values cut to 60), short not-found / old-image / error messages, and
      takes ONLY `name` (the session never comes from the model);
  (b) registration: in AGENT_TOOLS, NOT in GATED_TOOLS, passes the gate;
  (c) get_variables: /proxy/variables/redacted (404 → /proxy/variables) and
      "— columns:" when the schema fetch works, unchanged when it fails;
  (d) the per-turn [Variables…] block: caps (25 vars, 25 cols, 6000 chars), every
      name present, no blank line, priority (message / active-cell window), the
      rename hint (productId → product_id), no hint on < 2 overlap;
  (e) fallback: no schema → the names-only block; _fetch_variable_schema → None
      without variables (no HTTP), on non-200 and on an exception;
  (f) prompts: COLUMN NAMES rule + inspect_variable in BOTH prompt files, the
      misleading `reviews = read_dynamodb(...)` example gone;
  (g) the decline note string is defined and yielded (behavior is covered by
      test_chat_pending_interrupt_autodecline.py c1/c4);
  (h) get_notebook_state uses the web's `index` labels and the (active cell) mark.
No network, no model, no VM: httpx.Client is replaced by a fake.
Run:
    agent/ai_assistant/.venv/bin/python tests/ai/test_inspect_variable_schema.py
Exit 0 = all passed; 1 = at least one failed.
"""
import inspect as _inspect
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["PROXY_AUTH_TOKEN"] = "test-token"  # local dummy; never printed

from ai_assistant.tools import execution_tools as et  # noqa: E402
from ai_assistant.tools import approval_hook as ah  # noqa: E402
from ai_assistant import notebook_agent as na  # noqa: E402

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {str(detail)[:300]}" if detail and not ok else ""))


class FakeResp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else str(body))

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    """Replaces httpx.Client. routes: {(method, path_suffix): FakeResp | Exception}."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _do(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "json": json})
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return FakeResp(404, None, "")

    def get(self, url, headers=None, **kw):
        return self._do("GET", url, headers)

    def post(self, url, headers=None, json=None, **kw):
        return self._do("POST", url, headers, json)


_REAL_CLIENT = et.httpx.Client


def use(fake):
    et.httpx.Client = fake  # same module object notebook_agent imports lazily


def restore():
    et.httpx.Client = _REAL_CLIENT


REVIEWS = {
    "name": "reviews", "type": "DataFrame", "kind": "dataframe", "shape": [80, 5],
    "columns": [["verified_purchase", "bool"], ["rating", "float64"], ["reviewId", "object"],
                ["product_id", "object"], ["text", "object"]],
    "more": 0, "dtype": None, "len": None, "defined_by": "c6", "defined_at": 6, "last_exec": 6,
}
CATALOG = {"_catalog": {"entries": [{"name": "ecommerce-reviews", "status": "discovered",
                                     "columns": ["productId", "reviewId", "rating", "text"]}]}}


def frame(name, ncols=3, t=1):
    return {"name": name, "type": "DataFrame", "kind": "dataframe", "shape": [10, ncols],
            "columns": [[f"col{i}", "int64"] for i in range(ncols)], "more": 0,
            "defined_at": t, "last_exec": t}


def case_a():
    print("\n(a) inspect_variable")
    et.set_execution_context({"session_id": "S1", "proxy_url": "http://px"})
    body = dict(REVIEWS, sample=[{"verified_purchase": "True", "rating": "5.0", "reviewId": "r1",
                                  "product_id": "p1", "text": "z" * 200}],
                omitted_columns=[], truncated=False)
    fake = FakeHTTP({("POST", "/proxy/variables/inspect"): FakeResp(200, body)})
    use(fake)
    try:
        out = et.inspect_variable("reviews")
    finally:
        restore()
    c = fake.calls[0] if fake.calls else {}
    record("posts to /proxy/variables/inspect", c.get("url") == "http://px/proxy/variables/inspect", c.get("url"))
    record("json body {name, sample_rows:3}", c.get("json") == {"name": "reviews", "sample_rows": 3}, c.get("json"))
    record("X-Session-Id from the context", c.get("headers", {}).get("X-Session-Id") == "S1")
    record("X-Proxy-Auth attached", bool(c.get("headers", {}).get("X-Proxy-Auth")))
    record("renders product_id:object", "product_id:object" in out, out)
    record("renders shape", "reviews: DataFrame 80×5" in out, out)
    record("renders Sample rows", "Sample rows" in out, out)
    long_val = out.split("text=", 1)[-1].split(",")[0].strip()
    record("sample value cut to <= 60 chars", len(long_val) <= 60, len(long_val))
    record("output <= 4000 chars", len(out) <= 4000)

    for status, resp_body, expect in (
        (404, {"error": "variable not found"}, "is not defined"),
        (404, None, "not available on this VM image"),
        (504, {"error": "inspection timed out"}, "inspection timed out"),
        (500, {"error": "inspection failed"}, "inspection failed"),
    ):
        use(FakeHTTP({("POST", "/proxy/variables/inspect"): FakeResp(status, resp_body, "<html>x</html>")}))
        try:
            msg = et.inspect_variable("reviews")
        finally:
            restore()
        record(f"HTTP {status} → short message ({expect})", expect in msg and len(msg) < 200, msg)
    use(FakeHTTP({("POST", "/proxy/variables/inspect"): ConnectionError("boom secret detail")}))
    try:
        msg = et.inspect_variable("reviews")
    finally:
        restore()
    record("exception → type only", msg == "Failed to inspect variable: ConnectionError", msg)
    fake = FakeHTTP({})
    use(fake)
    try:
        msg = et.inspect_variable("_private")
    finally:
        restore()
    record("invalid name rejected locally (no HTTP)", "Invalid variable name" in msg and not fake.calls, msg)
    props = set(et.inspect_variable.tool_spec["inputSchema"]["json"].get("properties", {}))
    record("tool takes ONLY `name`", props == {"name"}, props)


class _State:
    def __init__(self):
        self._d = {}

    def get(self, k, d=None):
        return self._d.get(k, d)

    def set(self, k, v):
        self._d[k] = v


class _Agent:
    def __init__(self):
        self.state = _State()


class _Event:
    def __init__(self, name):
        self.tool_use = {"name": name, "input": {"name": "reviews"}}
        self.agent = _Agent()
        self.invocation_state = {}
        self.interrupts = []
        self.cancel_tool = False

    def interrupt(self, name, reason=None, response=None):
        self.interrupts.append(name)
        return "n"


def case_b():
    print("\n(b) registration")
    record("inspect_variable in AGENT_TOOLS", et.inspect_variable in na.AGENT_TOOLS)
    record("inspect_variable NOT in GATED_TOOLS", "inspect_variable" not in ah.GATED_TOOLS)
    record("GATED_TOOLS unchanged", set(ah.GATED_TOOLS) == {"execute_code", "install_package"}, ah.GATED_TOOLS)
    orig_auto, orig_ctx = ah._auto_execute_enabled, ah._get_context
    ah._auto_execute_enabled = lambda: False
    ah._get_context = lambda: {"_approval_channel": True}
    try:
        ev = _Event("inspect_variable")
        ah.ExecutionApprovalHook()._gate(ev)
        record("gate ON: inspect_variable passes (no interrupt, no cancel)",
               not ev.interrupts and ev.cancel_tool is False)
        ev = _Event("execute_code")
        ah.ExecutionApprovalHook()._gate(ev)
        record("gate ON: execute_code still gated", ev.interrupts == ["approve:execute_code"])
    finally:
        ah._auto_execute_enabled, ah._get_context = orig_auto, orig_ctx


def case_c():
    print("\n(c) get_variables")
    et.set_execution_context({"session_id": "S1", "proxy_url": "http://px"})
    vars_body = {"variables": {"reviews": {"type": "DataFrame", "shape": "80 rows × 5 cols", "preview": "<df>"},
                               "n": {"type": "int", "shape": "", "preview": "3"}}}
    schema = {"variables": [REVIEWS], "count": 1, "truncated": False}
    fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, vars_body),
                     ("GET", "/proxy/variables/schema"): FakeResp(200, schema)})
    use(fake)
    try:
        out = et.get_variables()
    finally:
        restore()
    record("uses /proxy/variables/redacted", fake.calls[0]["url"].endswith("/proxy/variables/redacted"))
    record("renders — columns: for a tabular var", "reviews: DataFrame 80 rows × 5 cols = <df> — columns: verified_purchase:bool"
           in out, out)
    record("non-tabular line has no columns", "  n: int = 3" in out and "n: int = 3 —" not in out, out)
    fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(404, None),
                     ("GET", "/proxy/variables"): FakeResp(200, vars_body),
                     ("GET", "/proxy/variables/schema"): FakeResp(404, None)})
    use(fake)
    try:
        out2 = et.get_variables()
    finally:
        restore()
    urls = [c["url"] for c in fake.calls]
    record("404 → falls back to /proxy/variables", urls[:2] == ["http://px/proxy/variables/redacted", "http://px/proxy/variables"], urls)
    record("schema failure → no columns, old format", "— columns:" not in out2
           and "reviews: DataFrame 80 rows × 5 cols = <df>" in out2, out2)


def _schema(entries):
    return {"variables": entries, "count": len(entries), "truncated": False}


def case_d():
    print("\n(d) per-turn [Variables…] block")
    entries = [frame(f"df{i:02d}", 40 if i == 0 else 12, t=100 - i) for i in range(30)]
    entries.append({"name": "n", "type": "int", "kind": "other", "shape": None, "columns": []})
    ctx = {"variables": ["df00", "extra_name"], "_variable_schema": _schema(entries)}
    blk = na._build_existing_vars_block(ctx, "")
    lines = blk.split("\n")
    schema_lines = [ln for ln in lines[1:] if not ln.startswith("  Other variables:")]
    record("header keeps the transcript prefix", lines[0].startswith("[Variables already defined in the notebook"))
    record("<= 25 schema lines", len(schema_lines) <= 25, len(schema_lines))
    record("40-col frame → 25 shown, +15 more", "+15 more" in blk, blk[:300])
    record("schema text <= 6000 chars", sum(len(x) + 1 for x in schema_lines) <= 6000)
    record("no blank line inside the block", "\n\n" not in blk)
    for nm in [e["name"] for e in entries] + ["extra_name"]:
        if nm not in blk:
            record(f"every name present ({nm})", False)
            break
    else:
        record("every name present (schema ∪ context)", True)
    other = next((ln for ln in lines if ln.startswith("  Other variables:")), "")
    others = [x.strip() for x in other.split(":", 1)[-1].split(",")] if other else []
    record("Other variables sorted", others == sorted(others) and "n" in others and "extra_name" in others, other)

    # priority: message mention and active-cell window beat recency
    cells = [{"index": 0, "code": "x = 1"}, {"index": 40, "code": "old_used.head()", "active": True}]
    ents = [frame(f"new{i:02d}", 3, t=100 - i) for i in range(30)] + [frame("old_msg", 3, t=1), frame("old_used", 3, t=0)]
    ctx = {"variables": [], "_variable_schema": _schema(ents), "notebook_cells": cells}
    blk = na._build_existing_vars_block(ctx, "plot old_msg by rating")
    body = blk.split("\n")[1:]
    record("message-named frame first", body[0].startswith("  old_msg:"), body[:2])
    record("active-window frame second", body[1].startswith("  old_used:"), body[:2])
    record("ranked-out frames go to Other variables", "new29" in body[-1] and body[-1].startswith("  Other variables:"))

    # rename hint
    ctx = {"variables": ["reviews"], "_variable_schema": _schema([REVIEWS]), "data_sources": CATALOG}
    blk = na._build_existing_vars_block(ctx, "")
    record("rename hint productId → product_id",
           "product_id:object (source column productId is product_id here)" in blk, blk)
    record("exact-name columns get no hint", "rating:float64 (source" not in blk)
    lone = dict(REVIEWS, columns=[["product_id", "object"], ["zzz", "int64"]])
    ctx = {"variables": ["reviews"], "_variable_schema": _schema([lone]), "data_sources": CATALOG}
    blk = na._build_existing_vars_block(ctx, "")
    record("no hint with < 2 overlapping columns", "(source column" not in blk, blk)
    # end-to-end through the message preamble (message is passed in)
    pre = na._prepend_available_data("use reviews", {"variables": ["reviews"], "_variable_schema": _schema([REVIEWS])})
    record("_prepend_available_data renders the schema line", "  reviews: DataFrame 80×5 — " in pre, pre[:400])

    # size: notebook-26-like 15 frames × 12 columns
    ents = [frame(f"frame_{i:02d}", 12, t=i) for i in range(15)]
    blk = na._build_existing_vars_block({"variables": [e["name"] for e in ents], "_variable_schema": _schema(ents)}, "")
    print(f"    (15 frames × 12 cols block: {len(blk)} chars ≈ {len(blk) // 4} tokens)")
    record("15×12 block well under the 6000-char cap", len(blk) < 6000, len(blk))


def case_e():
    print("\n(e) fallback")
    names_ctx = {"variables": ["b", "a"]}
    blk = na._build_existing_vars_block(dict(names_ctx, _variable_schema=None), "")
    record("no schema → names-only block", blk.endswith("]\n  a, b") and "inspect_variable" in blk, blk)
    record("empty schema → names-only block",
           na._build_existing_vars_block(dict(names_ctx, _variable_schema={"variables": []}), "") == blk)
    record("no vars, no schema → ''", na._build_existing_vars_block({}, "") == "")
    fake = FakeHTTP({("GET", "/proxy/variables/schema"): FakeResp(200, _schema([REVIEWS]))})
    use(fake)
    try:
        none_vars = na._fetch_variable_schema({"session_id": "S1", "variables": []})
        no_sid = na._fetch_variable_schema({"variables": ["a"]})
        record("no variables / no session → None and no HTTP call", none_vars is None and no_sid is None and not fake.calls)
        got = na._fetch_variable_schema({"session_id": "S2", "variables": ["reviews"], "proxy_url": "http://px"})
        record("200 → payload", isinstance(got, dict) and got["variables"][0]["name"] == "reviews")
        h = fake.calls[-1]["headers"] if fake.calls else {}
        record("fetch uses the turn's session + proxy auth", h.get("X-Session-Id") == "S2" and bool(h.get("X-Proxy-Auth")))
        record("fetch URL", fake.calls[-1]["url"] == "http://px/proxy/variables/schema")
    finally:
        restore()
    use(FakeHTTP({("GET", "/proxy/variables/schema"): FakeResp(500, {"error": "inspection failed"})}))
    try:
        record("non-200 → None", na._fetch_variable_schema({"session_id": "S", "variables": ["a"]}) is None)
    finally:
        restore()
    use(FakeHTTP({("GET", "/proxy/variables/schema"): TimeoutError("slow")}))
    try:
        record("exception → None", na._fetch_variable_schema({"session_id": "S", "variables": ["a"]}) is None)
    finally:
        restore()
    use(FakeHTTP({("GET", "/proxy/variables/schema"): FakeResp(200, ["not", "a", "dict"])}))
    try:
        record("bad payload → None", na._fetch_variable_schema({"session_id": "S", "variables": ["a"]}) is None)
    finally:
        restore()
    record("timeout is 3 s", et.SCHEMA_FETCH_TIMEOUT_S == 3.0)


def case_f():
    print("\n(f) prompts")
    from ai_assistant import prompts as ap
    agent_src = open(os.path.join(_REPO, "agent", "ai_assistant", "prompts.py")).read()
    proxy_src = open(os.path.join(_REPO, "proxy", "notebook", "ai", "prompts.py")).read()
    bad = 'reviews = read_dynamodb("ecommerce-reviews").head(80)'
    for label, src in (("agent prompts.py", agent_src), ("proxy prompts.py", proxy_src)):
        record(f"{label}: COLUMN NAMES rule", "- COLUMN NAMES: for a variable that already exists" in src)
        record(f"{label}: inspect_variable capability", "- inspect_variable(name):" in src)
        record(f"{label}: misleading example gone", f"`{bad}`" not in src)
        record(f"{label}: raw_reviews example", 'raw_reviews = read_dynamodb("ecommerce-reviews").head(80)' in src)
    record("assembled agent prompt has the rule", "COLUMN NAMES:" in ap.NOTEBOOK_AGENT_PROMPT
           and "inspect_variable" in ap.NOTEBOOK_AGENT_PROMPT)


def case_g():
    print("\n(g) decline note")
    record("note text", na.DECLINED_INSPECTION_NOTE == "The earlier inspection wasn't run; the answer may guess column names.")
    src = _inspect.getsource(na.chat_stream)
    record("chat_stream yields the note after the decline notice (execute_code only)",
           'if "execute_code" in decline_notice' in src and "DECLINED_INSPECTION_NOTE" in src)
    record("decline notice text unchanged",
           na._decline_notice(["execute_code"]) == "Declined the pending execute_code request because a new message was sent; it was not run.")


def case_h():
    print("\n(h) get_notebook_state cell indexes")
    et.set_execution_context({"session_id": "S1", "notebook_cells": [
        {"index": 3, "type": "code", "code": "reviews = raw.rename(columns={'productId': 'product_id'})"},
        {"index": 70, "type": "code", "code": "reviews.head()", "active": True},
        {"index": 71, "type": "markdown", "code": "# notes"},
    ]})
    out = et.get_notebook_state()
    record("index labels", "[Cell 3] Code:" in out and "[Cell 70] (active cell) Code:" in out
           and "[Cell 71] Markdown:" in out, out)
    record("window header", out.startswith("(Showing 3 cells;"), out[:80])
    record("no Code [n] counter with indexes", "Code [" not in out)
    et.set_execution_context({"session_id": "S1", "notebook_cells": [{"type": "code", "code": "a = 1"}]})
    out = et.get_notebook_state()
    record("legacy (no index) format unchanged", out.startswith("[Cell 0] Code [1]:"), out)
    record("CELL_CODE_MAX_CHARS is 300", et.CELL_CODE_MAX_CHARS == 300)


def main():
    for fn in (case_a, case_b, case_c, case_d, case_e, case_f, case_g, case_h):
        try:
            fn()
        except Exception as e:  # a crash is a failure, not a skip
            import traceback
            traceback.print_exc()
            record(f"{fn.__name__} crashed: {type(e).__name__}", False)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} passed")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
