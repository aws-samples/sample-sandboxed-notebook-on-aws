#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for the AI Fix "verified" flag (proxy.notebook.ai).

verified must be True ONLY when the fix agent's LAST execute_code run succeeded AND
ran the exact code being returned. No network / Bedrock / VM: the Strands Agent,
the model, the live-variable prefetch, the one-shot Bedrock client and the
execute_code HTTP client are all replaced with fakes.

Covers:
  • compute_fix_verdict: no runs / last run failed (the real restcountries case) /
    equal code modulo whitespace + line endings / code_mismatch
  • normalize_code_for_verify is conservative (indentation still matters)
  • execution_run_record + run_code_recorded derive success from the EXECUTION
    result (success:false / error) even though the tool call returns normally
  • per-invocation recorder isolation (copied contexts share it; other contexts don't)
  • fix_error_agentic wiring with a fake agent: non-execute tools don't count, the
    code_mismatch final re-run (within budget, and skipped when the budget is spent),
    fallbacks, and the one-shot path → verified False
  • POST /ai/fix passes verify_reason / verify_error through

Run:  .venv/bin/python tests/ai/test_fix_verified.py   (exit 0 = pass)
"""
import asyncio
import contextvars
import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Never let anything reach a real AWS endpoint from this test.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
# The default model comes only from BEDROCK_MODEL_ID (scripts/config.sh); set a
# test value before the proxy AI modules read it at import.
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-5-5")
from proxy.notebook.ai import notebook_agent as na  # noqa: E402
from proxy.notebook.ai import constants as ai_constants  # noqa: E402
from proxy.notebook.ai.tools import execution_tools as et  # noqa: E402

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


RC_ERROR = ("RuntimeError: restcountries returned an error payload "
            "{'status': 400, 'message': 'Bad Request'} (line 12)")
PROBE_1 = "print(rc_response)"
PROBE_2 = "import requests\nprint(requests.get('https://restcountries.com/v5/all').status_code)"
CANDIDATE = ("import requests\n"
             "resp = requests.get('https://restcountries.com/v3.1/all')\n"
             "data = resp.json()\n"
             "if isinstance(data, dict):\n"
             "    raise RuntimeError(f'restcountries returned an error payload {data}')\n"
             "df = pd.DataFrame(data)")


def ok_run(code, output=""):
    return {"code": code, "ok": True, "error": "", "error_type": None,
            "error_lineno": None, "output": output}


def err_run(code, error):
    return {"code": code, "ok": False, "error": error, "error_type": None,
            "error_lineno": None, "output": ""}


# ── fake HTTP for execute_code ────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text
        self.content = b"x"

    def json(self):
        return self._data


class FakeHttpx:
    """Stands in for httpx.Client; `script` maps code → FakeResp (or Exception)."""

    def __init__(self, script=None, default=None):
        self.script = dict(script or {})
        self.default = default
        self.posts = []
        self.requests = []   # (url, json body, headers) per POST
        self.gets = []
        self.clears = []     # (url, json body) of scratch-clear POSTs (not execute runs)
        self.scratch_reads = []  # (url, json body) of scratch listings (get_variables)

    def Client(self, *a, **kw):  # noqa: N802 — mimics httpx.Client
        outer = self

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None):
                body = json or {}
                if url.endswith("/proxy/variables/scratch/clear"):
                    outer.clears.append((url, dict(body)))
                    return FakeResp(200, {"cleared": True})
                if url.endswith("/proxy/variables/scratch"):   # get_variables' scratch listing
                    outer.scratch_reads.append((url, dict(body)))
                    return FakeResp(200, {"variables": {}, "count": 0})
                code = body.get("code", body.get("sql", ""))
                outer.posts.append(code)
                outer.requests.append((url, dict(body), dict(headers or {})))
                r = outer.script.get(code, outer.default)
                if isinstance(r, Exception):
                    raise r
                return r

            def get(self, url, headers=None, **kw):
                outer.gets.append(url)
                return FakeResp(200, {"variables": {"df": {"type": "DataFrame"}}})

        return _C()


def ok_resp(output="ok"):
    return FakeResp(200, {"success": True, "output": output})


def err_resp(error, lineno=None):
    d = {"success": False, "error": error}
    if lineno is not None:
        d["error_lineno"] = lineno
    return FakeResp(200, d)


# ── fake Strands Agent ─────────────────────────────────────────────────────────
class FakeAgent:
    """Runs a scripted list of (tool_name, kwargs) calls, each in a worker thread
    with a COPIED context (exactly how Strands runs sync tools), then returns
    `final_text`. `raise_exc` simulates an agent-loop failure after the calls."""

    plan = []
    final_text = ""
    raise_exc = None
    last = {}   # what the most recent agent was built/called with (for assertions)

    def __init__(self, **kwargs):
        self.tools = {t.tool_name: t for t in kwargs.get("tools", [])}
        FakeAgent.last = {"tools": list(self.tools), "system_prompt": kwargs.get("system_prompt", "")}

    def __call__(self, prompt, limits=None):
        FakeAgent.last.update({"kickoff": prompt, "limits": limits})
        for name, kwargs in FakeAgent.plan:
            ctx = contextvars.copy_context()
            out = {}
            th = threading.Thread(target=lambda: out.setdefault("r", ctx.run(self.tools[name], **kwargs)))
            th.start()
            th.join()
        if FakeAgent.raise_exc:
            raise FakeAgent.raise_exc
        return FakeAgent.final_text


def answer(code):
    return f"EXPLANATION: fixed it.\n```python\n{code}\n```"


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


ONE_SHOT_CODE = "df = fixed_by_one_shot()"


def run_agentic(plan, final_text, http, raise_exc=None, max_attempts=4,
                cell_type="code", code="bad()", error="NameError: bad", session_id="sess-1"):
    p = Patch()
    try:
        FakeAgent.plan, FakeAgent.final_text, FakeAgent.raise_exc = plan, final_text, raise_exc
        p.set(na, "Agent", FakeAgent)
        p.set(na, "get_model", lambda: object())
        p.set(na, "_prefetch_live_variables", lambda *a, **k: "")
        p.set(na, "fix_error", lambda code, error, ctx: {
            "fixed_code": ONE_SHOT_CODE, "explanation": "one-shot", **na._ONE_SHOT_VERDICT})
        p.set(et, "httpx", http)
        p.set(ai_constants, "FIX_VERIFY_MAX_ATTEMPTS", max_attempts)
        return na.fix_error_agentic(code, error, {
            "cell_type": cell_type, "session_id": session_id, "proxy_url": "http://fake"})
    finally:
        p.undo()


# ── SQL fixtures (the real "AWS Data Sources" sample failure) ──────────────────
ATHENA_ERR = ("Athena query FAILED: COLUMN_NOT_FOUND: line 17:10: Column 'p.product_id1' "
              "cannot be resolved or requester is not authorized to access requested resources")
SQL_BROKEN = ("SELECT p.product_id, p.name, SUM(s.units) AS units\n"
              "FROM microvm_demo_db.sales s\n"
              "JOIN microvm_demo_db.products p ON p.product_id = s.product_id\n"
              "WHERE s.market = '{market}'\n"
              "GROUP BY p.product_id1, p.name\n"
              "HAVING SUM(s.units) >= {min_units}")
SQL_FIXED = SQL_BROKEN.replace("p.product_id1", "p.product_id")
SQL_PROBE = "SELECT * FROM microvm_demo_db.products LIMIT 5"


def sql_ok(rows=3):
    return FakeResp(200, {"success": True, "output": "…table…", "engine": "athena",
                          "row_count": rows, "column_count": 3, "output_variable": "_fix_test_result"})


def sql_err(error):
    return FakeResp(200, {"success": False, "error": error, "engine": "athena"})


def sql_answer(sql):
    return f"EXPLANATION: GROUP BY referenced a non-existent column.\n```sql\n{sql}\n```"


def run_sql(plan, final_text, http, **kw):
    return run_agentic(plan, final_text, http, cell_type="sql", code=SQL_BROKEN,
                       error=ATHENA_ERR, **kw)


def main() -> int:
    print("== 1. compute_fix_verdict ==")
    v = na.compute_fix_verdict([], "x = 1")
    check("no runs → not verified / no_run / attempts 0",
          v == {"verified": False, "verify_reason": "no_run", "verify_error": "", "attempts": 0}, str(v))

    runs = [ok_run(PROBE_1, "{'status': 400}"), ok_run(PROBE_2, "200"), err_run(CANDIDATE, RC_ERROR)]
    v = na.compute_fix_verdict(runs, CANDIDATE)
    check("restcountries: probe ok, probe ok, candidate errors → verified False",
          v["verified"] is False, str(v))
    check("restcountries → last_run_failed", v["verify_reason"] == "last_run_failed", str(v))
    check("restcountries → verify_error carries the full RuntimeError text",
          v["verify_error"] == RC_ERROR, repr(v["verify_error"]))
    check("restcountries → attempts = 3 execute_code runs", v["attempts"] == 3, str(v))

    ran = "import x\r\ny = x.f()   \r\n\r\n"
    returned = "\n\nimport x\ny = x.f()\n"
    v = na.compute_fix_verdict([err_run("a", "E: 1"), ok_run(ran)], returned)
    check("last run ok + equal modulo whitespace/CRLF → verified True",
          v["verified"] is True and v["verify_reason"] is None and v["attempts"] == 2, str(v))

    v = na.compute_fix_verdict([ok_run(CANDIDATE)], CANDIDATE + "\nprint(df.head())")
    check("last run ok but different code → code_mismatch",
          v["verified"] is False and v["verify_reason"] == "code_mismatch"
          and v["verify_error"] == "", str(v))

    v = na.compute_fix_verdict([ok_run(CANDIDATE), err_run("print(z)", "NameError: z")], CANDIDATE)
    check("earlier run of the same code ok but LAST run failed → last_run_failed",
          v["verify_reason"] == "last_run_failed", str(v))

    print("== 2. normalize_code_for_verify is conservative ==")
    n = na.normalize_code_for_verify
    check("CRLF/CR → LF", n("a\r\nb\rc") == "a\nb\nc")
    check("trailing whitespace per line stripped", n("a  \nb\t") == "a\nb")
    check("leading/trailing blank lines stripped", n("\n  \na\n\n") == "a")
    check("indentation still significant", n("if x:\n    y") != n("if x:\ny"))
    check("inner blank lines kept", n("a\n\nb") != n("a\nb"))
    check("comments are not ignored", n("a  # c") != n("a"))

    print("== 3. execution_run_record derives success from the EXECUTION result ==")
    r = et.execution_run_record("c", status_code=200, data={"success": False, "error": RC_ERROR,
                                                            "error_lineno": 12})
    check("success:false → ok False", r["ok"] is False)
    check("full error kept", r["error"] == RC_ERROR)
    check("error type parsed", r["error_type"] == "RuntimeError", str(r["error_type"]))
    check("error line kept", r["error_lineno"] == 12)
    r = et.execution_run_record("c", status_code=200, data={"success": True, "error": "ValueError: x"})
    check("success:true but error set → ok False", r["ok"] is False and r["error_type"] == "ValueError")
    r = et.execution_run_record("c", status_code=200, data={"success": True, "output": "hi"})
    check("success:true, no error → ok True", r["ok"] is True and r["output"] == "hi")
    long_body = "x" * 5000
    r = et.execution_run_record("c", status_code=502, http_text=long_body)
    check("non-200 → ok False with the FULL body", r["ok"] is False and r["error"].endswith(long_body))
    r = et.execution_run_record("c", exc=ConnectionError("refused"))
    check("transport exception → ok False", r["ok"] is False and "refused" in r["error"])
    r = et.execution_run_record("c", status_code=200, data={"success": False})
    check("failure with no error text → 'Unknown error', no type",
          r["error"] == "Unknown error" and r["error_type"] is None)

    print("== 4. run_code_recorded / execute_code tool ==")
    http = FakeHttpx({"boom()": err_resp(RC_ERROR, 3), "fine()": ok_resp("done")})
    p = Patch()
    p.set(et, "httpx", http)
    try:
        with et.record_execute_runs() as rec:
            text = et.execute_code(code="boom()")   # the decorated tool, called directly
            check("tool call returns normally with 'Error: ...' text",
                  text == f"Error: {RC_ERROR}", repr(text))
            check("…but the run is recorded as FAILED", len(rec) == 1 and rec[0]["ok"] is False
                  and rec[0]["error"] == RC_ERROR and rec[0]["error_lineno"] == 3, str(rec))
            et.run_code_recorded("fine()")
            check("successful run recorded ok with its exact code",
                  rec[-1]["ok"] is True and rec[-1]["code"] == "fine()" and rec[-1]["output"] == "done")
            # A copied context (Strands' to_thread hop) still records into THIS list.
            ctx = contextvars.copy_context()
            th = threading.Thread(target=lambda: ctx.run(et.run_code_recorded, "fine()"))
            th.start(); th.join()
            check("run from a copied context lands in the same recorder", len(rec) == 3)
        check("recorder reset on exit", et._execute_runs.get() is None)
        out = et.run_code_recorded("fine()")
        check("no recorder active → runs fine, nothing recorded", out == "done")

        # Isolation: two concurrent fixes in their own contexts don't see each other.
        results = {}
        barrier = threading.Barrier(2)

        def fix_like(name, code):
            with et.record_execute_runs() as mine:
                barrier.wait()
                et.run_code_recorded(code)
                barrier.wait()
                results[name] = [r["code"] for r in mine]

        t1 = threading.Thread(target=contextvars.copy_context().run, args=(fix_like, "a", "boom()"))
        t2 = threading.Thread(target=contextvars.copy_context().run, args=(fix_like, "b", "fine()"))
        t1.start(); t2.start(); t1.join(); t2.join()
        check("concurrent recorders are isolated",
              results == {"a": ["boom()"], "b": ["fine()"]}, str(results))
    finally:
        p.undo()

    print("== 5. fix_error_agentic wiring (fake agent, no Bedrock) ==")
    # 5a. The exact real-world scenario.
    http = FakeHttpx({PROBE_1: ok_resp("{'status': 400}"), PROBE_2: ok_resp("200"),
                      CANDIDATE: err_resp(RC_ERROR, 12)})
    out = run_agentic([("execute_code", {"code": PROBE_1}), ("execute_code", {"code": PROBE_2}),
                       ("execute_code", {"code": CANDIDATE})], answer(CANDIDATE), http)
    check("restcountries end-to-end → verified False", out.get("verified") is False, str(out))
    check("restcountries end-to-end → last_run_failed + full RuntimeError",
          out.get("verify_reason") == "last_run_failed" and "RuntimeError: restcountries returned an error payload"
          in out.get("verify_error", ""), str(out))
    check("restcountries end-to-end → attempts 3, no extra run", out.get("attempts") == 3 and len(http.posts) == 3,
          f"attempts={out.get('attempts')} posts={len(http.posts)}")
    check("response keeps fixed_code/explanation",
          out.get("fixed_code") == CANDIDATE and out.get("explanation") == "fixed it.")

    # 5b. Verified: last run is the returned code (whitespace differs).
    http = FakeHttpx({CANDIDATE + "   \n": ok_resp()}, default=err_resp("NameError: q"))
    out = run_agentic([("execute_code", {"code": "q"}), ("execute_code", {"code": CANDIDATE + "   \n"})],
                      answer(CANDIDATE), http)
    check("last run ok on the same code → verified True",
          out.get("verified") is True and out.get("verify_reason") is None and out.get("attempts") == 2, str(out))

    # 5c. Only non-execute tools → no_run.
    http = FakeHttpx()
    out = run_agentic([("get_variables", {})], answer(CANDIDATE), http)
    check("only get_variables → verified False / no_run / attempts 0",
          out.get("verified") is False and out.get("verify_reason") == "no_run"
          and out.get("attempts") == 0 and http.posts == [], str(out))

    # 5d. code_mismatch within budget → ONE final run of the returned code.
    returned = CANDIDATE + "\nprint(len(df))"
    http = FakeHttpx({CANDIDATE: ok_resp(), returned: ok_resp("250")})
    out = run_agentic([("execute_code", {"code": CANDIDATE})], answer(returned), http)
    check("code_mismatch → final re-run executes exactly the returned code",
          http.posts == [CANDIDATE, returned], str(http.posts))
    check("…and it passes → verified True, attempts 2",
          out.get("verified") is True and out.get("attempts") == 2, str(out))
    scopes = {b.get("agent_scope") for _u, b, _h in http.requests}
    scope = next(iter(scopes)) if len(scopes) == 1 else None
    check("…test runs + re-run share ONE fix:<12 hex> agent_scope",
          isinstance(scope, str) and scope.startswith("fix:") and len(scope) == 16, str(scopes))
    check("…and the fix clears that scope once, after the verdict",
          [b for _u, b in http.clears] == [{"scope": scope}], str(http.clears))

    http = FakeHttpx({CANDIDATE: ok_resp(), returned: err_resp("TypeError: len() of NoneType")})
    out = run_agentic([("execute_code", {"code": CANDIDATE})], answer(returned), http)
    check("code_mismatch re-run fails → last_run_failed with that error",
          out.get("verified") is False and out.get("verify_reason") == "last_run_failed"
          and out.get("verify_error") == "TypeError: len() of NoneType", str(out))

    # 5e. code_mismatch with the budget spent → reported, no re-run.
    http = FakeHttpx(default=ok_resp())
    plan = [("execute_code", {"code": f"probe{i}"}) for i in range(3)] + [("execute_code", {"code": CANDIDATE})]
    out = run_agentic(plan, answer(returned), http, max_attempts=4)
    check("code_mismatch at the attempt cap → no re-run, code_mismatch",
          out.get("verify_reason") == "code_mismatch" and len(http.posts) == 4 and out.get("attempts") == 4, str(out))

    # 5f. Last run failed and agent returned different code → no re-run.
    http = FakeHttpx({CANDIDATE: err_resp(RC_ERROR)})
    out = run_agentic([("execute_code", {"code": CANDIDATE})], answer(returned), http)
    check("last_run_failed with different returned code → no re-run",
          out.get("verify_reason") == "last_run_failed" and len(http.posts) == 1, str(out))

    # 5g. Agent loop raises → one-shot fallback judged against the runs that happened.
    http = FakeHttpx({CANDIDATE: err_resp(RC_ERROR)})
    out = run_agentic([("execute_code", {"code": CANDIDATE})], "", http, raise_exc=RuntimeError("model down"))
    check("agent failure → one-shot code, verified False, last run error surfaced",
          out.get("fixed_code") == ONE_SHOT_CODE and out.get("verified") is False
          and out.get("verify_reason") == "last_run_failed" and out.get("verify_error") == RC_ERROR, str(out))

    # 5h. No code block → fallback; no runs → no_run.
    out = run_agentic([], "I could not do it.", FakeHttpx())
    # bare text is parsed as code by _parse_fix_response; force the empty case:
    out_empty = run_agentic([], "", FakeHttpx())
    check("no parseable code → one-shot fallback, no_run",
          out_empty.get("fixed_code") == ONE_SHOT_CODE and out_empty.get("verify_reason") == "no_run", str(out_empty))
    check("bare-text answer was never run → not verified", out.get("verified") is False, str(out))

    print("== 6. one-shot fix_error is never verified ==")
    out = na.fix_error("# md", "err", {"cell_type": "markdown"})
    check("markdown → verified False / no_run",
          out.get("verified") is False and out.get("verify_reason") == "no_run" and out.get("attempts") == 0, str(out))

    class FakeBedrock:
        def converse(self, **kw):
            return {"stopReason": "end_turn",
                    "output": {"message": {"content": [{"text": "EXPLANATION: x\n```sql\nSELECT 1\n```"}]}}}

    p = Patch()
    p.set(na, "_get_direct_client", lambda: FakeBedrock())
    try:
        out = na.fix_error("SELEC 1", "syntax", {"cell_type": "sql"})
        check("SQL one-shot → verified False / no_run",
              out.get("fixed_code") == "SELECT 1" and out.get("verified") is False
              and out.get("verify_reason") == "no_run", str(out))
        out = na.fix_error_agentic("bad()", "NameError", {"cell_type": "code", "session_id": ""})
        check("no session (no VM) → one-shot → verified False / no_run",
              out.get("verified") is False and out.get("verify_reason") == "no_run", str(out))
    finally:
        p.undo()

    print("== 7. POST /ai/fix passes the verdict through ==")
    import proxy.notebook.routes.ai as ai_routes

    class Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    p = Patch()
    try:
        p.set(ai_routes, "agent_fix_error_agentic", lambda c, e, ctx: {
            "fixed_code": CANDIDATE, "explanation": "x", "verified": False, "attempts": 3,
            "verify_reason": "last_run_failed", "verify_error": RC_ERROR})
        resp = asyncio.run(ai_routes.ai_fix_error(Req({"code": "a", "error": "b", "session_id": "s"})))
        check("not verified → verify_reason + full verify_error in the response",
              resp == {"fixed_code": CANDIDATE, "explanation": "x", "verified": False, "attempts": 3,
                       "verify_reason": "last_run_failed", "verify_error": RC_ERROR}, str(resp))
        p.set(ai_routes, "agent_fix_error_agentic", lambda c, e, ctx: {
            "fixed_code": "ok", "explanation": "", "verified": True, "attempts": 2,
            "verify_reason": None, "verify_error": ""})
        resp = asyncio.run(ai_routes.ai_fix_error(Req({"code": "a", "error": "b", "session_id": "s"})))
        check("verified → backward-compatible shape (no verify_* keys)",
              resp == {"fixed_code": "ok", "explanation": "", "verified": True, "attempts": 2}, str(resp))
        p.set(ai_routes, "agent_fix_error", lambda c, e, ctx: {
            "fixed_code": "SELECT 1", "explanation": "", **na._ONE_SHOT_VERDICT})
        resp = asyncio.run(ai_routes.ai_fix_error(Req({"code": "a", "error": "b", "cell_type": "sql"})))
        check("one-shot route → verified False / no_run",
              resp.get("verified") is False and resp.get("verify_reason") == "no_run"
              and resp.get("attempts") == 0, str(resp))
    finally:
        p.undo()

    print("== 8. SQL fix: execute_sql test runs (fake agent + fake httpx) ==")
    # 8a. Candidate runs clean → verified; the request is an isolated test run.
    http = FakeHttpx({SQL_FIXED: sql_ok()})
    out = run_sql([("execute_sql", {"sql": SQL_FIXED})], sql_answer(SQL_FIXED), http)
    check("SQL candidate runs clean → verified True, attempts 1",
          out.get("verified") is True and out.get("verify_reason") is None
          and out.get("attempts") == 1 and out.get("fixed_code") == SQL_FIXED, str(out))
    check("SQL agent gets ONLY execute_sql + get_variables + get_available_data_sources",
          sorted(FakeAgent.last["tools"]) == sorted(["execute_sql", "get_variables",
                                                      "get_available_data_sources"]),
          str(FakeAgent.last["tools"]))
    sp = FakeAgent.last["system_prompt"]
    check("SQL agent uses the SQL fix prompt + SQL verify directive",
          "<broken_sql>" in sp and "execute_sql" in sp and "```sql" in sp
          and "do NOT add a LIMIT" in sp and "COST" in sp, sp[-400:])
    url, body, headers = http.requests[0]
    check("execute_sql POSTs to /proxy/execute-sql through the proxy",
          url == "http://fake/proxy/execute-sql", url)
    check("execute_sql sends the session header", headers.get("X-Session-Id") == "sess-1", str(headers))
    check("execute_sql sends NO cell_id and origin 'agent'",
          "cell_id" not in body and body.get("origin") == "agent", str(body))
    check("execute_sql binds a scratch output variable, not the user's",
          body.get("output_variable") == et.FIX_SQL_TEST_OUTPUT_VAR == "_fix_test_result", str(body))
    check("execute_sql sends the SQL verbatim with {param} placeholders (like the web's SQL cell run)",
          body.get("sql") == SQL_FIXED and "'{market}'" in body["sql"] and "{min_units}" in body["sql"])
    # The VM substitutes placeholders for EVERY /execute-sql request (cell or test run)
    # in _run_sql_cell_core via _interpolate_scalars — the same function, same namespace.
    try:
        from app.notebook.sql_engine import _interpolate_scalars
        resolved, reads = _interpolate_scalars(body["sql"], {"market": "US", "min_units": 10})
        check("VM-side substitution resolves the test run's placeholders like a real run",
              "s.market = 'US'" in resolved and ">= 10" in resolved
              and sorted(reads) == ["market", "min_units"], resolved)
    except ImportError as e:
        check("app.notebook.sql_engine importable for the substitution check", False, str(e))

    # 8b. Last SQL run fails → last_run_failed with the FULL Athena error text.
    http = FakeHttpx({SQL_FIXED: sql_err(ATHENA_ERR)})
    out = run_sql([("execute_sql", {"sql": SQL_FIXED})], sql_answer(SQL_FIXED), http)
    check("last SQL run fails → verified False / last_run_failed",
          out.get("verified") is False and out.get("verify_reason") == "last_run_failed", str(out))
    check("…verify_error is the FULL Athena error", out.get("verify_error") == ATHENA_ERR,
          repr(out.get("verify_error")))
    check("…no extra run after a failed last run", len(http.posts) == 1, str(http.posts))

    # 8c. Probe ok but a DIFFERENT query returned → one re-run of exactly the returned SQL.
    http = FakeHttpx({SQL_PROBE: sql_ok(5), SQL_FIXED: sql_ok()})
    out = run_sql([("execute_sql", {"sql": SQL_PROBE})], sql_answer(SQL_FIXED), http)
    check("SQL code_mismatch within cap → re-runs exactly the returned SQL",
          http.posts == [SQL_PROBE, SQL_FIXED], str(http.posts))
    check("…re-run is also an isolated test run (no cell_id, origin agent, scratch var)",
          all("cell_id" not in b and b.get("origin") == "agent"
              and b.get("output_variable") == "_fix_test_result" for _, b, _h in http.requests))
    check("…and it passes → verified True, attempts 2",
          out.get("verified") is True and out.get("attempts") == 2, str(out))

    http = FakeHttpx({SQL_PROBE: sql_ok(5), SQL_FIXED: sql_err(ATHENA_ERR)})
    out = run_sql([("execute_sql", {"sql": SQL_PROBE})], sql_answer(SQL_FIXED), http)
    check("SQL code_mismatch re-run fails → last_run_failed with the Athena error",
          out.get("verify_reason") == "last_run_failed" and out.get("verify_error") == ATHENA_ERR, str(out))

    # 8d. Attempt cap = the SAME FIX_VERIFY_MAX_ATTEMPTS as Python (prompt, turns, re-run).
    http = FakeHttpx(default=sql_ok())
    out = run_sql([("execute_sql", {"sql": SQL_PROBE}), ("execute_sql", {"sql": SQL_PROBE + " "})],
                  sql_answer(SQL_FIXED), http, max_attempts=2)
    check("SQL code_mismatch at FIX_VERIFY_MAX_ATTEMPTS → no re-run, code_mismatch",
          out.get("verify_reason") == "code_mismatch" and len(http.posts) == 2
          and out.get("attempts") == 2, str(out))
    check("SQL prompt states the FIX_VERIFY_MAX_ATTEMPTS cap",
          "make at most 2 execute_sql verification run(s)" in FakeAgent.last["system_prompt"])
    check("SQL Strands turn limit derives from FIX_VERIFY_MAX_ATTEMPTS (same as Python)",
          FakeAgent.last["limits"] == {"turns": max(4, 2 * 2 + 2)}, str(FakeAgent.last["limits"]))
    run_sql([], sql_answer(SQL_FIXED), FakeHttpx(), max_attempts=5)
    sql_limits = FakeAgent.last["limits"]
    run_agentic([], answer(CANDIDATE), FakeHttpx(), max_attempts=5)
    check("SQL and Python get the same turn limit for the same cap",
          sql_limits == FakeAgent.last["limits"] == {"turns": 12}, f"{sql_limits} {FakeAgent.last['limits']}")
    check("no separate SQL attempt constant exists",
          not any("SQL" in n and "ATTEMPT" in n for n in dir(ai_constants)))

    # 8e. Only read tools → no_run; markdown / no session → one-shot no_run.
    http = FakeHttpx()
    out = run_sql([("get_variables", {})], sql_answer(SQL_FIXED), http)
    check("SQL with no execute_sql run → no_run", out.get("verify_reason") == "no_run"
          and out.get("attempts") == 0 and http.posts == [], str(out))
    http = FakeHttpx()
    out = run_sql([], "", http, session_id="")
    check("SQL without a session → one-shot fallback / no_run, nothing executed",
          out.get("fixed_code") == ONE_SHOT_CODE and out.get("verify_reason") == "no_run"
          and http.posts == [], str(out))
    out = run_agentic([], "", FakeHttpx(), cell_type="markdown")
    check("markdown via fix_error_agentic → one-shot / no_run",
          out.get("verify_reason") == "no_run" and out.get("attempts") == 0, str(out))

    # 8f. _parse_fix_response extracts the ```sql block for SQL cells.
    txt = ("EXPLANATION: fixed.\n```text\nnot sql\n```\n```sql\n" + SQL_FIXED + "\n```")
    code_out, _ = na._parse_fix_response(txt, "sql")
    check("_parse_fix_response prefers the ```sql block for SQL cells", code_out == SQL_FIXED, code_out)
    code_out, expl = na._parse_fix_response(sql_answer(SQL_FIXED), "sql")
    check("_parse_fix_response: plain SQL answer → query + explanation",
          code_out == SQL_FIXED and expl.startswith("GROUP BY"), code_out)

    # 8g. Recorder isolation unchanged: SQL runs only land in the active fix's recorder.
    http = FakeHttpx(default=sql_ok())
    p = Patch()
    p.set(et, "httpx", http)
    try:
        et.run_sql_recorded("SELECT 1")
        check("execute_sql with no recorder active records nothing", et._execute_runs.get() is None)
        with et.record_execute_runs() as rec:
            et.execute_sql(sql="SELECT 1")
            et.run_code_recorded("fine()")
            check("SQL and Python runs share the per-fix recorder, SQL text as code",
                  [r["code"] for r in rec] == ["SELECT 1", "fine()"] and rec[0]["ok"] is True, str(rec))
        results = {}
        barrier = threading.Barrier(2)

        def sql_fix_like(name, sql):
            with et.record_execute_runs() as mine:
                barrier.wait()
                et.run_sql_recorded(sql)
                barrier.wait()
                results[name] = [r["code"] for r in mine]

        t1 = threading.Thread(target=contextvars.copy_context().run, args=(sql_fix_like, "a", "SELECT 'a'"))
        t2 = threading.Thread(target=contextvars.copy_context().run, args=(sql_fix_like, "b", "SELECT 'b'"))
        t1.start(); t2.start(); t1.join(); t2.join()
        check("concurrent SQL recorders are isolated",
              results == {"a": ["SELECT 'a'"], "b": ["SELECT 'b'"]}, str(results))
    finally:
        p.undo()

    # 8h. Route: SQL + session → agentic path.
    p = Patch()
    try:
        called = {}
        p.set(ai_routes, "agent_fix_error_agentic", lambda c, e, ctx: called.setdefault("agentic", {
            "fixed_code": SQL_FIXED, "explanation": "x", "verified": True, "attempts": 1,
            "verify_reason": None, "verify_error": ""}))
        p.set(ai_routes, "agent_fix_error", lambda c, e, ctx: called.setdefault("one_shot", {
            "fixed_code": "", "explanation": "", **na._ONE_SHOT_VERDICT}))
        resp = asyncio.run(ai_routes.ai_fix_error(Req({"code": SQL_BROKEN, "error": ATHENA_ERR,
                                                       "cell_type": "sql", "session_id": "s"})))
        check("POST /ai/fix: SQL cell with a session → agentic path, unchanged response shape",
              list(called) == ["agentic"] and resp == {"fixed_code": SQL_FIXED, "explanation": "x",
                                                       "verified": True, "attempts": 1}, f"{called} {resp}")
        called.clear()
        asyncio.run(ai_routes.ai_fix_error(Req({"code": "# x", "error": "e",
                                                "cell_type": "markdown", "session_id": "s"})))
        check("POST /ai/fix: markdown with a session → one-shot", list(called) == ["one_shot"], str(called))
    finally:
        p.undo()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
