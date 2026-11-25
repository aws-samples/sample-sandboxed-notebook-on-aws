#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit test (chat agent package): orphan-variable markers, exploration-scratch
labels and the chat turn's agent_scope lifecycle.

Root cause being fixed: the Workbook Intel / chat agents' execute_code probes bound
names (e.g. `dynamodb_sentiment`) straight into the user namespace; those names had
no producing cell, were listed to the chat agent as ordinary variables, and the
generated cell depended on them. The VM now runs agent probes in a scope's scratch
overlay and flags orphans (`orphan` / `orphan_reason`). This pins the agent side:
  (a) orphan_marker texts per reason; '' for a cell variable;
  (b) the per-turn [Variables…] block: an orphan (e.g. a set) is rendered FIRST with
      its marker, is NOT in "Other variables", has its own 25-line budget with a
      "More variables with NO PRODUCING CELL" overflow, and the header explains the
      marker only when orphans exist;
  (c) inspect_variable + get_variables carry the marker; with an agent_scope the
      scratch section / label appears and inspect sends `scope`; without one the
      bodies are unchanged ({name, sample_rows:3}; {code, origin});
  (d) execute_code sends agent_scope only when the context has one;
  (e) clear_agent_scope: URL, body, headers, swallows errors, no-op without a session;
  (f) chat_stream: clears its chat:<sid> scope at a NEW turn start and when the turn
      finishes (also on error), NOT when it pauses, and not at a resume start;
      the /ai/chat/sync path (chat_stream with approval_channel False) uses the
      same chat:<sid> scope and clears it at the start and the end;
  (g) prompts: both new rules in BOTH prompt files, byte-identical, no braces.
No network, no model, no VM: httpx.Client and the agent are replaced by fakes.
Run:
    agent/ai_assistant/.venv/bin/python tests/ai/test_orphan_var_marking.py
Exit 0 = all passed; 1 = at least one failed.
"""
import asyncio
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["PROXY_AUTH_TOKEN"] = "test-token"  # local dummy; never printed

from ai_assistant.tools import execution_tools as et  # noqa: E402
from ai_assistant import notebook_agent as na  # noqa: E402
from ai_assistant import prompts as ap  # noqa: E402

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {str(detail)[:300]}" if detail and not ok else ""))


class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    """Replaces httpx.Client. routes: {(method, path_suffix): FakeResp | Exception}.
    Longest matching suffix wins (so .../scratch/clear != .../scratch)."""
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
            return FakeResp(404, None, "")
        resp = max(hits, key=lambda h: len(h[0]))[1]
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, headers=None, **kw):
        return self._do("GET", url, headers)

    def post(self, url, headers=None, json=None, **kw):
        return self._do("POST", url, headers, json)


_REAL_CLIENT = et.httpx.Client


def use(fake):
    et.httpx.Client = fake


def restore():
    et.httpx.Client = _REAL_CLIENT


AGENT_MARK = " [NO PRODUCING CELL (left by an AI exploration) — do not reference in cell code]"
DELETED_MARK = " [NO PRODUCING CELL (its cell was deleted) — do not reference in cell code]"
PLAIN_MARK = " [NO PRODUCING CELL — do not reference in cell code]"

SENTIMENT = {"name": "dynamodb_sentiment", "type": "set", "kind": "other", "len": 83,
             "orphan": True, "orphan_reason": "agent", "defined_by": None}
REVIEWS = {"name": "reviews", "type": "DataFrame", "kind": "dataframe", "shape": [80, 2],
           "columns": [["product_id", "object"], ["rating", "float64"]],
           "orphan": False, "orphan_reason": None, "defined_by": "c1"}
COUNT = {"name": "n_rows", "type": "int", "kind": "other", "orphan": False}


def case_a():
    print("\n(a) orphan_marker")
    record("agent", et.orphan_marker({"orphan": True, "orphan_reason": "agent"}) == AGENT_MARK)
    record("deleted cell", et.orphan_marker({"orphan": True, "orphan_reason": "deleted cell"}) == DELETED_MARK)
    record("internal/unknown → plain", et.orphan_marker({"orphan": True, "orphan_reason": "unknown"}) == PLAIN_MARK
           and et.orphan_marker({"orphan": True, "orphan_reason": "internal"}) == PLAIN_MARK)
    record("cell variable / old image / non-dict → ''",
           et.orphan_marker({"orphan": False}) == "" and et.orphan_marker({}) == ""
           and et.orphan_marker(None) == "")


def case_b():
    print("\n(b) per-turn [Variables…] block")
    ctx = {"variables": ["reviews", "dynamodb_sentiment", "n_rows"],
           "_variable_schema": {"variables": [REVIEWS, SENTIMENT, COUNT]}}
    block = na._build_existing_vars_block(ctx, "compare")
    lines = block.split("\n")
    want = "  dynamodb_sentiment: set len 83" + AGENT_MARK
    record("orphan set line rendered with its marker", want in lines, block)
    record("orphan rendered FIRST (right after the header)", lines[1] == want, lines[:3])
    other = next((ln for ln in lines if ln.startswith("  Other variables:")), "")
    record("orphan not in 'Other variables'", "dynamodb_sentiment" not in other and "n_rows" in other, other)
    record("cell DataFrame line has no marker",
           any(ln.startswith("  reviews: DataFrame 80×2") and "NO PRODUCING CELL" not in ln for ln in lines))
    record("header explains the marker", "Variables marked [NO PRODUCING CELL" in lines[0], lines[0])
    record("one header line, no blank line", "" not in lines and lines[0].startswith("["))

    plain = na._build_existing_vars_block(
        {"variables": ["reviews"], "_variable_schema": {"variables": [REVIEWS]}}, "")
    record("no orphans → no marker sentence", "NO PRODUCING CELL" not in plain, plain[:200])

    many = [{"name": f"o{i:02d}", "type": "int", "kind": "other", "orphan": True,
             "orphan_reason": "deleted cell"} for i in range(30)]
    blk = na._build_existing_vars_block({"variables": [], "_variable_schema": {"variables": many}}, "")
    ls = blk.split("\n")
    marked = [ln for ln in ls if ln.endswith(DELETED_MARK)]
    over = next((ln for ln in ls if ln.startswith("  More variables with NO PRODUCING CELL: ")), "")
    record("orphan budget: 25 marked lines", len(marked) == 25, len(marked))
    record("overflow names listed", over == "  More variables with NO PRODUCING CELL: o25, o26, o27, o28, o29", over)
    record("no 'Other variables' line for orphans only", not any(ln.startswith("  Other variables") for ln in ls))


def case_c():
    print("\n(c) inspect_variable / get_variables markers + scratch")
    et.set_execution_context({"session_id": "S1", "proxy_url": "http://p"})
    fake = FakeHTTP({("POST", "/proxy/variables/inspect"): FakeResp(200, dict(SENTIMENT, sample=[]))})
    use(fake)
    try:
        out = et.inspect_variable("dynamodb_sentiment")
    finally:
        restore()
    record("inspect: marker on the head line", out.split("\n")[0] == "dynamodb_sentiment: set len 83" + AGENT_MARK, out)
    record("inspect: no scope → body unchanged", fake.calls[0]["json"] == {"name": "dynamodb_sentiment", "sample_rows": 3},
           fake.calls[0]["json"])

    et.set_execution_context({"session_id": "S1", "proxy_url": "http://p", "agent_scope": "chat:S1"})
    fake = FakeHTTP({("POST", "/proxy/variables/inspect"): FakeResp(200, {
        "name": "a", "type": "int", "kind": "other", "scratch": True, "orphan": False})})
    use(fake)
    try:
        out = et.inspect_variable("a")
    finally:
        restore()
    record("inspect: scope sent when set", fake.calls[0]["json"] == {"name": "a", "sample_rows": 3, "scope": "chat:S1"},
           fake.calls[0]["json"])
    record("inspect: scratch label", out == "a: int [scratch: this exploration only]", out)

    variables = {"reviews": {"type": "DataFrame", "shape": "(80, 2)", "orphan": False},
                 "dynamodb_sentiment": {"type": "set", "preview": "{1, 2}", "orphan": True,
                                        "orphan_reason": "agent"}}
    et.set_execution_context({"session_id": "S1", "proxy_url": "http://p"})
    fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": variables}),
                     ("GET", "/proxy/variables/schema"): FakeResp(200, {"variables": [REVIEWS]})})
    use(fake)
    try:
        out = et.get_variables()
    finally:
        restore()
    record("get_variables: orphan line marked",
           "  dynamodb_sentiment: set = {1, 2}" + AGENT_MARK in out.split("\n"), out)
    record("get_variables: cell variable unmarked + columns",
           any(ln.startswith("  reviews: DataFrame (80, 2) — columns: product_id:object")
               and "NO PRODUCING" not in ln for ln in out.split("\n")), out)
    record("get_variables: no scope → no scratch request",
           not any(c["url"].endswith("/proxy/variables/scratch") for c in fake.calls))

    et.set_execution_context({"session_id": "S1", "proxy_url": "http://p", "agent_scope": "chat:S1"})
    fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": variables}),
                     ("GET", "/proxy/variables/schema"): FakeResp(200, {"variables": [REVIEWS]}),
                     ("POST", "/proxy/variables/scratch"): FakeResp(200, {"variables": {
                         "a": {"type": "int", "preview": "3", "scratch": True}}, "count": 1})})
    use(fake)
    try:
        out = et.get_variables()
    finally:
        restore()
    sc = [c for c in fake.calls if c["url"].endswith("/proxy/variables/scratch")]
    record("get_variables: scratch listing requested with the scope",
           len(sc) == 1 and sc[0]["json"] == {"scope": "chat:S1"}, sc)
    record("get_variables: scratch section labelled",
           "Exploration scratch (this turn only; not notebook variables):\n  a: int = 3 [scratch: this exploration only]" in out, out)

    fake = FakeHTTP({("GET", "/proxy/variables/redacted"): FakeResp(200, {"variables": variables}),
                     ("POST", "/proxy/variables/scratch"): FakeResp(404, None)})
    use(fake)
    try:
        out = et.get_variables()
    finally:
        restore()
    record("get_variables: scratch 404 (old image) → skipped", "Exploration scratch" not in out
           and out.startswith("Current variables:"), out)
    et.set_execution_context({})


def case_d():
    print("\n(d) execute_code agent_scope")
    for ctx, want in (({"session_id": "S1", "proxy_url": "http://p"}, {"code": "x", "origin": "agent"}),
                      ({"session_id": "S1", "proxy_url": "http://p", "agent_scope": "chat:S1"},
                       {"code": "x", "origin": "agent", "agent_scope": "chat:S1"})):
        et.set_execution_context(ctx)
        fake = FakeHTTP({("POST", "/proxy/execute"): FakeResp(200, {"success": True, "output": "4\n"})})
        use(fake)
        try:
            out = et.execute_code._tool_func("x") if hasattr(et.execute_code, "_tool_func") else et.execute_code("x")
        finally:
            restore()
        record(f"execute body {'with' if 'agent_scope' in ctx else 'without'} scope",
               fake.calls[0]["json"] == want and out == "4", fake.calls[0]["json"])
    et.set_execution_context({})


def case_e():
    print("\n(e) clear_agent_scope")
    fake = FakeHTTP({("POST", "/proxy/variables/scratch/clear"): FakeResp(200, {"cleared": True})})
    use(fake)
    try:
        ok = et.clear_agent_scope("http://p", "S1", "chat:S1")
    finally:
        restore()
    c = fake.calls[0] if fake.calls else {}
    record("POST URL + body", c.get("url") == "http://p/proxy/variables/scratch/clear"
           and c.get("json") == {"scope": "chat:S1"} and ok is True, c)
    record("headers: session + proxy auth", c.get("headers", {}).get("X-Session-Id") == "S1"
           and c.get("headers", {}).get("X-Proxy-Auth") == "test-token")
    use(FakeHTTP({("POST", "/proxy/variables/scratch/clear"): RuntimeError("down")}))
    try:
        ok = et.clear_agent_scope("http://p", "S1", "chat:S1")
    finally:
        restore()
    record("swallows errors → False", ok is False)
    fake = FakeHTTP({})
    use(fake)
    try:
        ok = et.clear_agent_scope("http://p", "", "chat:S1")
    finally:
        restore()
    record("no session → no HTTP", ok is False and fake.calls == [])


class _Itr:
    def __init__(self):
        self.id = "itr-1"
        self.reason = {"tool": "execute_code", "input": {"code": "x = 1"}}
        self.name = "approval:execute_code"
        self.response = None


class _Result:
    def __init__(self, stop_reason, interrupts=None):
        self.stop_reason = stop_reason
        self.interrupts = interrupts or []


class _FakeAgent:
    """Minimal stand-in for a Strands Agent: stream_async yields a scripted turn."""
    def __init__(self):
        self.messages = []
        self._interrupt_state = None
        self.script = []
        self.raise_after = None
    def stream_async(self, prompt, **kw):
        script, boom = list(self.script), self.raise_after

        async def gen():
            for ev in script:
                yield ev
            if boom:
                raise boom
        return gen()

def case_f():
    print("\n(f) chat_stream scope lifecycle")
    agent = _FakeAgent()
    clears = []
    saved = (na.get_or_create_agent, na.clear_agent_scope, na._fetch_variable_schema)
    na.get_or_create_agent = lambda sid, ctx: agent
    na.clear_agent_scope = lambda url, sid, scope: clears.append((url, sid, scope)) or True
    na._fetch_variable_schema = lambda ctx: None

    async def run(message="hi", responses=None):
        frames, err = [], None
        ctx = {"approval_channel": True, "proxy_url": "http://p", "session_id": "nb-sess"}
        try:
            async for fr in na.chat_stream("agent-sid", message, ctx, interrupt_responses=responses):
                frames.append(fr)
        except Exception as e:  # noqa: BLE001
            err = e
        return frames, err, ctx

    try:
        agent.script = [{"data": "hello"}, {"result": _Result("end_turn")}]
        clears.clear()
        frames, err, ctx = asyncio.run(run())
        record("new finished turn: scope set on context", ctx.get("agent_scope") == "chat:agent-sid", ctx.get("agent_scope"))
        record("new finished turn: cleared at start AND at end",
               clears == [("http://p", "nb-sess", "chat:agent-sid")] * 2 and err is None, clears)

        agent.script = [{"data": "let me check"}, {"result": _Result("interrupt", [_Itr()])}]
        clears.clear()
        frames, err, _ = asyncio.run(run())
        record("paused turn: interrupt frame emitted", any(f.get("type") == "interrupt" for f in frames), frames)
        record("paused turn: cleared only at the start (scope kept for the resume)",
               clears == [("http://p", "nb-sess", "chat:agent-sid")], clears)

        agent.script = [{"data": "ran it"}, {"result": _Result("end_turn")}]
        clears.clear()
        frames, err, _ = asyncio.run(run("", [{"interruptId": "itr-1", "response": "y"}]))
        record("resume that finishes: NOT cleared at start, cleared at end",
               clears == [("http://p", "nb-sess", "chat:agent-sid")] and err is None, clears)

        agent.script = [{"data": "partial"}]
        agent.raise_after = RuntimeError("model blew up")
        clears.clear()
        frames, err, _ = asyncio.run(run())
        agent.raise_after = None
        record("turn that errors: still cleared at the end",
               isinstance(err, RuntimeError) and len(clears) == 2, (err, clears))

        # /ai/chat/sync aggregates chat_stream with approval_channel False.
        agent.script = [{"data": "answer"}, {"result": _Result("end_turn")}]
        clears.clear()
        sync_ctx = {"approval_channel": False, "proxy_url": "http://p", "session_id": "nb-sess"}

        async def run_sync():
            return [fr async for fr in na.chat_stream("agent-sid", "hi", sync_ctx)]
        frames = asyncio.run(run_sync())
        record("sync path: chat:<sid> scope and the answer streamed",
               sync_ctx.get("agent_scope") == "chat:agent-sid"
               and any("answer" in str(f.get("content", "")) for f in frames),
               (sync_ctx.get("agent_scope"), frames))
        record("sync path: scope cleared at start AND at end",
               clears == [("http://p", "nb-sess", "chat:agent-sid")] * 2, clears)
    finally:
        na.get_or_create_agent, na.clear_agent_scope, na._fetch_variable_schema = saved


def case_g():
    print("\n(g) prompts")
    agent_src = open(os.path.join(_REPO, "agent/ai_assistant/prompts.py"), encoding="utf-8").read()
    proxy_src = open(os.path.join(_REPO, "proxy/notebook/ai/prompts.py"), encoding="utf-8").read()

    def bullet(src, prefix):
        m = re.search(r"^" + re.escape(prefix) + r".*$", src, re.M)
        return m.group(0) if m else None

    for prefix in ("- EXPLORATION SCRATCH:", "- CELL CODE IS SELF-CONTAINED:", "- execute_code:"):
        a, p = bullet(agent_src, prefix), bullet(proxy_src, prefix)
        record(f"{prefix} identical in both prompt files", a is not None and a == p)
        record(f"{prefix} has no braces", a is not None and "{" not in a and "}" not in a)
    record("execute_code tool line mentions scratch", "exploration scratch" in (bullet(agent_src, "- execute_code:") or ""))
    record("assembled agent prompt carries both rules",
           "EXPLORATION SCRATCH:" in ap.NOTEBOOK_AGENT_PROMPT and "CELL CODE IS SELF-CONTAINED:" in ap.NOTEBOOK_AGENT_PROMPT
           and "never mutate a user variable in place" in ap.NOTEBOOK_AGENT_PROMPT)


if __name__ == "__main__":
    for case in (case_a, case_b, case_c, case_d, case_e, case_f, case_g):
        try:
            case()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            record(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    sys.exit(1 if failed else 0)
