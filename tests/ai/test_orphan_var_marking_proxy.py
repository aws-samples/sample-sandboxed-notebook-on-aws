#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit test (proxy side): orphan-variable markers, exploration-scratch labels,
agent_scope plumbing and internal origins for the Fix / Intel agents and the
proxy/batch internal callers.

Covers:
  (a) execution_tools.orphan_marker texts (mirror of the agent package);
  (b) inspect_variable + get_variables markers; scratch label / section and the
      `scope` body field only when an agent_scope is set (old body unchanged);
  (c) run_code_recorded / run_sql_recorded send agent_scope only when set;
  (d) clear_agent_scope URL/body/headers, swallows errors, no-op without session;
  (e) _prefetch_live_variables appends the marker;
  (f) fix_error_agentic runs its tools under a fix:<12 hex> scope and clears it
      (also when the agent loop raises);
  (g) generate_intel sets intel:<intel id> as agent_scope and clears it in a
      finally (also when the agent raises);
  (h) batch.entity_discovery._execute_on_vm and the /datasources/schema
      execute_on_vm send origin "internal";
  (i) prompts: both rules in both prompt files, INTEL_PROMPT APPROACH, the
      FIX_VERIFY_DIRECTIVE scratch sentence (LIVE-kernel sentence gone) and the
      FIX_SQL_VERIFY_DIRECTIVE "not bound" note; .format() still works.
No network, no model, no VM.
Run:  .venv/bin/python tests/ai/test_orphan_var_marking_proxy.py   (exit 0 = pass)
"""
import asyncio
import contextvars
import os
import re
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ["PROXY_AUTH_TOKEN"] = "test-token"  # local dummy; never printed

import httpx  # noqa: E402
from proxy.notebook.ai import notebook_agent as na  # noqa: E402
from proxy.notebook.ai.tools import execution_tools as et  # noqa: E402

_failures = []
_n = [0]


def check(name, cond, detail=""):
    _n[0] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {str(detail)[:300]}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text
        self.content = b"x"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    """Replaces httpx.Client. routes: {(method, path_suffix): FakeResp | Exception};
    the longest matching suffix wins."""
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
        hits = [(s, r) for (m, s), r in self.routes.items() if m == method and url.endswith(s)]
        if not hits:
            return FakeResp(404, None)
        r = max(hits, key=lambda h: len(h[0]))[1]
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None, **kw):
        return self._do("GET", url, headers)

    def post(self, url, headers=None, json=None, **kw):
        return self._do("POST", url, headers, json)


class Patch:
    def __init__(self):
        self._saved = []

    def set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        self._saved.clear()


AGENT_MARK = " [NO PRODUCING CELL (left by an AI exploration) — do not reference in cell code]"
DELETED_MARK = " [NO PRODUCING CELL (its cell was deleted) — do not reference in cell code]"
PLAIN_MARK = " [NO PRODUCING CELL — do not reference in cell code]"
NOCELL_MARK = (" [NO PRODUCING CELL (set outside any cell, by the UI or an API call)"
               " — do not reference in cell code]")


def _tool(fn):
    return getattr(fn, "_tool_func", fn)


def case_a():
    print("== (a) orphan_marker ==")
    check("agent", et.orphan_marker({"orphan": True, "orphan_reason": "agent"}) == AGENT_MARK)
    check("deleted cell", et.orphan_marker({"orphan": True, "orphan_reason": "deleted cell"}) == DELETED_MARK)
    check("no cell (UI / API write)", et.orphan_marker({"orphan": True, "orphan_reason": "no cell"}) == NOCELL_MARK)
    check("unknown/internal → plain", et.orphan_marker({"orphan": True, "orphan_reason": "unknown"}) == PLAIN_MARK
          and et.orphan_marker({"orphan": True, "orphan_reason": "internal"}) == PLAIN_MARK)
    check("non-orphan / old image / None → ''", et.orphan_marker({"orphan": False}) == ""
          and et.orphan_marker({}) == "" and et.orphan_marker(None) == "")


def case_a_mirror():
    # The agent package keeps a duplicate (no proxy import). Same texts, by source.
    src = open(os.path.join(_ROOT, "agent/ai_assistant/tools/execution_tools.py"), encoding="utf-8").read()
    check("agent package mirror has the same four marker texts",
          all(m.strip() in src for m in (AGENT_MARK, DELETED_MARK, PLAIN_MARK, NOCELL_MARK)))


def case_b():
    print("== (b) inspect_variable / get_variables ==")
    p = Patch()
    try:
        et.set_execution_context({"session_id": "S1", "proxy_url": "http://p"})
        fake = FakeHTTP({("POST", "/proxy/variables/inspect"): FakeResp(200, {
            "name": "dynamodb_sentiment", "type": "set", "kind": "other", "len": 83,
            "orphan": True, "orphan_reason": "agent", "sample": []})})
        p.set(et.httpx, "Client", fake)
        out = _tool(et.inspect_variable)("dynamodb_sentiment")
        check("inspect: marker on head line",
              out.split("\n")[0] == "dynamodb_sentiment: set len 83" + AGENT_MARK, out)
        check("inspect: no scope → body is exactly {name, sample_rows:3}",
              fake.calls[0]["json"] == {"name": "dynamodb_sentiment", "sample_rows": 3}, fake.calls[0]["json"])

        et.set_execution_context({"session_id": "S1", "proxy_url": "http://p", "agent_scope": "fix:abc"})
        fake = FakeHTTP({("POST", "/proxy/variables/inspect"): FakeResp(200, {
            "name": "tmp", "type": "int", "kind": "other", "scratch": True})})
        p.set(et.httpx, "Client", fake)
        out = _tool(et.inspect_variable)("tmp")
        check("inspect: scope sent when set", fake.calls[0]["json"] == {"name": "tmp", "sample_rows": 3, "scope": "fix:abc"},
              fake.calls[0]["json"])
        check("inspect: scratch label", out == "tmp: int [scratch: this exploration only]", out)

        variables = {"orders": {"type": "DataFrame", "shape": "(3, 1)", "orphan": False},
                     "missing": {"type": "list", "preview": "[1]", "orphan": True, "orphan_reason": "deleted cell"}}
        et.set_execution_context({"session_id": "S1", "proxy_url": "http://p"})
        fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": variables})})
        p.set(et.httpx, "Client", fake)
        out = _tool(et.get_variables)()
        lines = out.split("\n")
        check("get_variables: orphan marked", "  missing: list = [1]" + DELETED_MARK in lines, out)
        check("get_variables: cell var unmarked", "  orders: DataFrame (3, 1)" in lines, out)
        check("get_variables: no scope → no scratch call",
              not any(c["url"].endswith("/proxy/variables/scratch") for c in fake.calls))

        et.set_execution_context({"session_id": "S1", "proxy_url": "http://p", "agent_scope": "intel:x"})
        fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": variables}),
                         ("POST", "/proxy/variables/scratch"): FakeResp(200, {"variables": {
                             "a": {"type": "int", "preview": "3", "scratch": True}}, "count": 1})})
        p.set(et.httpx, "Client", fake)
        out = _tool(et.get_variables)()
        sc = [c for c in fake.calls if c["url"].endswith("/proxy/variables/scratch")]
        check("get_variables: scratch requested with scope", len(sc) == 1 and sc[0]["json"] == {"scope": "intel:x"}, sc)
        check("get_variables: scratch section labelled",
              "Exploration scratch (this turn only; not notebook variables):\n  a: int = 3 [scratch: this exploration only]"
              in out, out)

        fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": variables}),
                         ("POST", "/proxy/variables/scratch"): FakeResp(404, None)})
        p.set(et.httpx, "Client", fake)
        out = _tool(et.get_variables)()
        check("get_variables: scratch 404 (old image) → skipped", "Exploration scratch" not in out, out)
    finally:
        p.undo()
        et.set_execution_context({})


def case_c():
    print("== (c) execute / execute-sql agent_scope ==")
    p = Patch()
    try:
        for ctx, scoped in (({"session_id": "S1", "proxy_url": "http://p"}, False),
                            ({"session_id": "S1", "proxy_url": "http://p", "agent_scope": "fix:abc"}, True)):
            et.set_execution_context(ctx)
            fake = FakeHTTP({("POST", "/proxy/execute"): FakeResp(200, {"success": True, "output": "4"}),
                             ("POST", "/proxy/execute-sql"): FakeResp(200, {"success": True, "row_count": 1})})
            p.set(et.httpx, "Client", fake)
            et.run_code_recorded("print(a+1)")
            et.run_sql_recorded("SELECT 1")
            want_code = {"code": "print(a+1)", "origin": "agent"}
            want_sql = {"sql": "SELECT 1", "output_variable": et.FIX_SQL_TEST_OUTPUT_VAR, "origin": "agent"}
            if scoped:
                want_code["agent_scope"] = want_sql["agent_scope"] = "fix:abc"
            label = "with" if scoped else "without"
            check(f"execute body {label} scope", fake.calls[0]["json"] == want_code, fake.calls[0]["json"])
            check(f"execute-sql body {label} scope", fake.calls[1]["json"] == want_sql, fake.calls[1]["json"])
    finally:
        p.undo()
        et.set_execution_context({})


def case_d():
    print("== (d) clear_agent_scope ==")
    p = Patch()
    try:
        fake = FakeHTTP({("POST", "/proxy/variables/scratch/clear"): FakeResp(200, {"cleared": True})})
        p.set(et.httpx, "Client", fake)
        ok = et.clear_agent_scope("http://p", "S1", "intel:x")
        c = fake.calls[0] if fake.calls else {}
        check("URL + body", c.get("url") == "http://p/proxy/variables/scratch/clear"
              and c.get("json") == {"scope": "intel:x"} and ok is True, c)
        check("headers: session + proxy auth", c.get("headers", {}).get("X-Session-Id") == "S1"
              and c.get("headers", {}).get("X-Proxy-Auth") == "test-token")
        p.set(et.httpx, "Client", FakeHTTP({("POST", "/proxy/variables/scratch/clear"): RuntimeError("down")}))
        check("swallows errors → False", et.clear_agent_scope("http://p", "S1", "intel:x") is False)
        fake = FakeHTTP({})
        p.set(et.httpx, "Client", fake)
        check("no session/scope → no HTTP", et.clear_agent_scope("http://p", "", "x") is False
              and et.clear_agent_scope("http://p", "S1", "") is False and fake.calls == [])
    finally:
        p.undo()


def case_e():
    print("== (e) _prefetch_live_variables marker ==")
    p = Patch()
    try:
        fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": {
            "orders": {"type": "DataFrame", "shape": "(3, 1)"},
            "dynamodb_sentiment": {"type": "set", "preview": "{1}", "orphan": True, "orphan_reason": "agent"}}})})
        p.set(httpx, "Client", fake)
        out = na._prefetch_live_variables("S1", "http://p")
        lines = out.split("\n")
        check("orphan line marked", "  dynamodb_sentiment: set = {1}" + AGENT_MARK in lines, out)
        check("cell var unmarked", "  orders: DataFrame (3, 1)" in lines, out)
    finally:
        p.undo()


class FakeAgent:
    """Runs scripted tool calls in a worker thread with a COPIED context (how Strands
    runs sync tools), then returns final_text or raises."""
    plan, final_text, raise_exc, seen_scopes = [], "", None, []

    def __init__(self, **kwargs):
        self.tools = {t.tool_name: t for t in kwargs.get("tools", [])}

    def __call__(self, prompt, limits=None):
        FakeAgent.seen_scopes.append(et._get_context().get("agent_scope"))
        for name, kwargs in FakeAgent.plan:
            ctx = contextvars.copy_context()
            th = threading.Thread(target=lambda: ctx.run(self.tools[name], **kwargs))
            th.start()
            th.join()
        if FakeAgent.raise_exc:
            raise FakeAgent.raise_exc
        return FakeAgent.final_text


def case_f():
    print("== (f) fix_error_agentic scope ==")
    from proxy.notebook.ai import constants as ai_constants
    for raise_exc in (None, RuntimeError("loop failed")):
        p = Patch()
        try:
            FakeAgent.plan = [("execute_code", {"code": "x = 1"})]
            FakeAgent.final_text = "EXPLANATION: ok.\n```python\nx = 1\n```"
            FakeAgent.raise_exc, FakeAgent.seen_scopes = raise_exc, []
            fake = FakeHTTP({("POST", "/proxy/execute"): FakeResp(200, {"success": True, "output": ""}),
                             ("POST", "/proxy/variables/scratch/clear"): FakeResp(200, {"cleared": True})})
            p.set(na, "Agent", FakeAgent)
            p.set(na, "get_model", lambda: object())
            p.set(na, "_prefetch_live_variables", lambda *a, **k: "")
            p.set(na, "fix_error", lambda code, error, ctx: {"fixed_code": "y", "explanation": "", **na._ONE_SHOT_VERDICT})
            p.set(et.httpx, "Client", fake)
            p.set(ai_constants, "FIX_VERIFY_MAX_ATTEMPTS", 4)
            out = na.fix_error_agentic("bad()", "NameError", {"cell_type": "code", "session_id": "S1",
                                                               "proxy_url": "http://p"})
            label = "agent raised" if raise_exc else "verified"
            scope = FakeAgent.seen_scopes[0] if FakeAgent.seen_scopes else None
            execs = [c["json"] for c in fake.calls if c["url"].endswith("/proxy/execute")]
            clears = [c["json"] for c in fake.calls if c["url"].endswith("/scratch/clear")]
            check(f"{label}: scope fix:<12 hex>", isinstance(scope, str) and re.fullmatch(r"fix:[0-9a-f]{12}", scope), scope)
            check(f"{label}: test run carries the scope", execs and all(b.get("agent_scope") == scope for b in execs), execs)
            check(f"{label}: scope cleared exactly once", clears == [{"scope": scope}], clears)
            if not raise_exc:
                check("verified result unchanged", out.get("verified") is True, out)
        finally:
            p.undo()
            et.set_execution_context({})


def case_g():
    print("== (g) generate_intel scope ==")
    import strands
    import strands.models
    from proxy.notebook.ai.intel import generate as gen

    class VMM:
        active_microvms = {}

        def get_session_vm(self, sid):
            return {"vm_id": "vm-1", "endpoint": "ep"}

    class Storage:
        def notebook_list(self):
            return []

    for raise_exc in (None, RuntimeError("model down")):
        p = Patch()
        clears = []
        try:
            FakeAgent.plan, FakeAgent.final_text = [], "not json"
            FakeAgent.raise_exc, FakeAgent.seen_scopes = raise_exc, []
            p.set(strands, "Agent", FakeAgent)
            p.set(strands.models, "BedrockModel", lambda **kw: object())
            p.set(gen, "discover_all_local_files", lambda *a, **k: {"total": 0})
            p.set(gen, "_fetch_relevant_entity_docs", lambda *a, **k: "")
            p.set(gen, "clear_agent_scope", lambda url, sid, scope: clears.append((url, sid, scope)) or True)
            gen.generate_intel("S1", VMM(), "bucket", Storage())
            label = "agent raised" if raise_exc else "finished"
            scope = FakeAgent.seen_scopes[0] if FakeAgent.seen_scopes else None
            check(f"{label}: scope intel:intel-<8 hex>-S1",
                  isinstance(scope, str) and re.fullmatch(r"intel:intel-[0-9a-f]{8}-S1", scope), scope)
            check(f"{label}: cleared once with that scope",
                  clears == [(f"http://localhost:{gen.PROXY_PORT}", "S1", scope)], clears)
        finally:
            p.undo()
            et.set_execution_context({})


def case_h():
    print("== (h) internal origins ==")
    from batch import entity_discovery as ed
    p = Patch()
    sent = {}
    try:
        def fake_post(url, headers=None, json=None, timeout=None):
            sent.update(url=url, json=json)
            return FakeResp(200, {"success": True, "output": "ok"})
        p.set(httpx, "post", fake_post)
        out = ed._execute_on_vm("http://p", "S1", "print(1)")
        check("entity_discovery._execute_on_vm sends origin internal",
              sent.get("json") == {"code": "print(1)", "origin": "internal"} and out == "ok", sent)
    finally:
        p.undo()

    from proxy.platform.routes import sessions as sess
    from proxy.platform import http_clients
    from proxy.platform.datasources import registry
    vm_kinds = [m["source_type"] for m in registry.provider_metadata() if m.get("requires_vm_execution")]
    if not vm_kinds:
        check("a VM-executed source type exists", False)
        return
    posted = []

    class AsyncClient:
        async def post(self, url, headers=None, json=None, timeout=None):
            posted.append({"url": url, "json": json})
            return FakeResp(200, {"success": True, "output": ""})

    class VMM:
        async def get_session_vm_async(self, sid):
            return {"vm_id": "vm-1", "endpoint": "ep"}

        async def get_auth_token(self, vm_id):
            return "vm-token"

    class Provider:
        def __init__(self, execute_fn):
            self.execute_fn = execute_fn

        async def get_schema(self, source_id, session_id=None):
            await self.execute_fn(session_id, "probe()")
            return None

    class App:
        class state:
            vm_manager = VMM()

    class Req:
        app = App

    async def boom(*a, **k):
        raise RuntimeError("no catalog")

    p = Patch()
    try:
        p.set(http_clients, "vm_client", lambda: AsyncClient())
        p.set(http_clients, "request_idempotent", boom)
        p.set(registry, "get_provider", lambda st, execute_fn=None: Provider(execute_fn))
        asyncio.run(sess.get_datasource_schema(vm_kinds[0], "/tmp/x.csv", Req(), session_id="S1"))
        check("/datasources/schema execute_on_vm sends origin internal",
              posted and posted[0]["json"] == {"code": "probe()", "origin": "internal"}
              and posted[0]["url"] == "https://ep/execute", posted)
    finally:
        p.undo()


def case_i():
    print("== (i) prompts ==")
    from proxy.notebook.ai import prompts as pp
    agent_src = open(os.path.join(_ROOT, "agent/ai_assistant/prompts.py"), encoding="utf-8").read()
    proxy_src = open(os.path.join(_ROOT, "proxy/notebook/ai/prompts.py"), encoding="utf-8").read()

    def bullet(src, prefix):
        m = re.search(r"^" + re.escape(prefix) + r".*$", src, re.M)
        return m.group(0) if m else None

    for prefix in ("- EXPLORATION SCRATCH:", "- CELL CODE IS SELF-CONTAINED:", "- execute_code:"):
        a, b = bullet(agent_src, prefix), bullet(proxy_src, prefix)
        check(f"{prefix} identical in both files, no braces",
              a is not None and a == b and "{" not in a and "}" not in a)
    check("proxy chat prompts carry both rules",
          "EXPLORATION SCRATCH:" in pp.NOTEBOOK_AGENT_BASE_PROMPT
          and "CELL CODE IS SELF-CONTAINED:" in pp.NOTEBOOK_AGENT_PROMPT)
    approach = pp.INTEL_PROMPT.split("APPROACH:", 1)[-1].split("PARALLEL TOOL USE:", 1)[0]
    check("INTEL_PROMPT APPROACH has both rules",
          "EXPLORATION SCRATCH:" in approach and "CELL CODE IS SELF-CONTAINED:" in approach
          and "never mutate a user variable in place" in approach)
    try:
        formatted = pp.INTEL_PROMPT.format(entity_docs="E", catalog_json="C", notebook_state="N", variables="V")
        check("INTEL_PROMPT still .format()s", "EXPLORATION SCRATCH" in formatted)
    except Exception as e:  # noqa: BLE001
        check("INTEL_PROMPT still .format()s", False, f"{type(e).__name__}: {e}")
    d = pp.FIX_VERIFY_DIRECTIVE
    check("FIX_VERIFY_DIRECTIVE: LIVE-kernel sentence replaced", "LIVE kernel" not in d
          and "exploration" in d and "scratch" in d
          and "must NOT depend on names created only by an earlier test run" in d
          and "in place" in d)
    check("FIX_SQL_VERIFY_DIRECTIVE: result not bound",
          "The test result is not bound to any variable" in pp.FIX_SQL_VERIFY_DIRECTIVE)


if __name__ == "__main__":
    for case in (case_a, case_a_mirror, case_b, case_c, case_d, case_e, case_f, case_g, case_h, case_i):
        try:
            case()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    if _failures:
        print(f"\nFAILED: {len(_failures)} check(s): {_failures}")
        sys.exit(1)
    print(f"\nALL PASS ({_n[0]} checks)")
