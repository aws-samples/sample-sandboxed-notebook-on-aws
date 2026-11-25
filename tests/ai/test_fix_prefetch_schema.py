# test-kind: offline
"""Offline tests: the AI Fix agent's live-variable prefetch carries REAL columns.

proxy.notebook.ai.notebook_agent._prefetch_live_variables used to claim "for a
DataFrame the preview shows its COLUMNS", but the preview is repr() cut to 300
chars (pandas elides middle columns). It now:
  • reads /proxy/variables/redacted (secret values masked VM-side), falling back
    to /proxy/variables on 404 (older VM images);
  • appends "— columns: c:dtype, …" from GET /proxy/variables/schema (best-effort);
  • claims columns ONLY when the schema arrived;
  • returns "" on any exception.
Also: the Fix agent's get_variables tool uses /redacted with the same fallback,
and the proxy's inspect_variable tool (rendering, name-only spec, context headers,
short errors) is registered in the Workbook Intelligence agent, whose system
prompt (NOTEBOOK_AGENT_BASE_PROMPT) points the model at it.
No network: httpx.Client is replaced by a fake.
Run:  .venv/bin/python tests/ai/test_fix_prefetch_schema.py   (exit 0 = pass)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

import httpx  # noqa: E402
from proxy.notebook.ai import notebook_agent as na  # noqa: E402
from proxy.notebook.ai.tools import execution_tools as et  # noqa: E402

_failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {str(detail)[:300]}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, **kw):
        self.urls.append(url)
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return FakeResp(404)

    def post(self, url, headers=None, json=None, **kw):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return self.get(url, headers=headers)

    @property
    def posts(self):
        if not hasattr(self, "_posts"):
            self._posts = []
        return self._posts


_REAL = httpx.Client
VARS = {"variables": {"reviews": {"type": "DataFrame", "shape": "80 rows × 3 cols", "preview": "<df>"},
                      "n": {"type": "int", "shape": "", "preview": "3"}}}
SCHEMA = {"variables": [{"name": "reviews", "kind": "dataframe", "type": "DataFrame", "shape": [80, 3],
                         "columns": [["product_id", "object"], ["rating", "float64"], ["text", "object"]],
                         "more": 0},
                        {"name": "n", "kind": "other", "type": "int", "columns": []}]}


def run(routes):
    fake = FakeHTTP(routes)
    httpx.Client = fake
    try:
        return na._prefetch_live_variables("S1", "http://px"), fake.urls
    finally:
        httpx.Client = _REAL


def main():
    print("== prefetch with schema ==")
    out, urls = run({"/proxy/variables/redacted": FakeResp(200, VARS),
                     "/proxy/variables/schema": FakeResp(200, SCHEMA)})
    check("uses /proxy/variables/redacted first", urls[0] == "http://px/proxy/variables/redacted", urls)
    check("renders real columns", "reviews: DataFrame 80 rows × 3 cols = <df> — columns: product_id:object, rating:float64, text:object" in out, out)
    check("non-tabular has no columns", "  n: int = 3\n" in out, out)
    check("columns claim present", '— columns:"' in out and "may differ from the data source" in out, out)
    check("old false claim gone", "the preview shows its COLUMNS" not in out)

    print("== 404 fallback, schema unavailable ==")
    out, urls = run({"/proxy/variables/redacted": FakeResp(404),
                     "/proxy/variables": FakeResp(200, VARS)})
    check("falls back to /proxy/variables", urls[:2] == ["http://px/proxy/variables/redacted", "http://px/proxy/variables"], urls)
    check("no columns claim without a schema", "— columns" not in out and "may NOT show every column" in out, out)
    check("still lists variables", "reviews: DataFrame" in out)

    print("== failures ==")
    out, _ = run({"/proxy/variables/redacted": ConnectionError("x")})
    check("exception → ''", out == "")
    out, _ = run({"/proxy/variables/redacted": FakeResp(500, {})})
    check("non-200 → ''", out == "")
    check("no session → ''", na._prefetch_live_variables("", "http://px") == "")

    print("== Fix agent get_variables tool ==")
    et.set_execution_context({"session_id": "S1", "proxy_url": "http://px"})
    fake = FakeHTTP({"/proxy/variables/redacted": FakeResp(404), "/proxy/variables": FakeResp(200, VARS)})
    httpx.Client = fake
    try:
        text = et.get_variables()
    finally:
        httpx.Client = _REAL
    check("tool tries /redacted then /variables", [u.rsplit("/proxy", 1)[-1] for u in fake.urls[:2]]
          == ["/variables/redacted", "/variables"], fake.urls)
    check("tool output lists variables", "reviews: DataFrame" in text, text)

    print("== proxy inspect_variable tool (intel agent) ==")
    spec = et.inspect_variable.tool_spec
    props = spec["inputSchema"]["json"]["properties"]
    check("inspect_variable takes only `name`", set(props) == {"name"}, props)
    INSPECT = {"name": "reviews", "kind": "dataframe", "type": "DataFrame", "shape": [80, 3],
               "columns": [["product_id", "object"], ["rating", "float64"], ["email", "object"]], "more": 0,
               "sample": [{"product_id": "p1", "rating": "4.0", "email": "<omitted>"}, {"product_id": "x" * 200}],
               "omitted_columns": ["email"], "truncated": False}
    os.environ["PROXY_AUTH_TOKEN"] = "test-token"
    et.set_execution_context({"session_id": "S1", "proxy_url": "http://px"})
    fake = FakeHTTP({"/proxy/variables/inspect": FakeResp(200, INSPECT)})
    httpx.Client = fake
    try:
        text = et.inspect_variable(name="reviews")
    finally:
        httpx.Client = _REAL
    post = fake.posts[0] if fake.posts else {}
    check("POSTs /proxy/variables/inspect", post.get("url") == "http://px/proxy/variables/inspect", post)
    check("body is name + sample_rows 3", post.get("json") == {"name": "reviews", "sample_rows": 3}, post)
    check("session/auth headers from context", (post.get("headers") or {}).get("X-Session-Id") == "S1"
          and (post.get("headers") or {}).get("X-Proxy-Auth") == "test-token")
    check("renders schema line", text.startswith(
        "reviews: DataFrame 80×3 — product_id:object, rating:float64, email:object"), text)
    check("renders sample rows", "[0] product_id=p1, rating=4.0, email=<omitted>" in text, text)
    check("cuts values to 60 chars", "x" * 60 not in text and "x" * 59 + "…" in text, text)
    check("lists omitted columns", "Values omitted (sensitive column names): email" in text, text)
    for bad in ("_x", "1a", "a b", ""):
        fake = FakeHTTP({})
        httpx.Client = fake
        try:
            out = et.inspect_variable(name=bad)
        finally:
            httpx.Client = _REAL
        check(f"invalid name {bad!r} rejected locally", out.startswith("Invalid variable name") and not fake.posts)
    fake = FakeHTTP({"/proxy/variables/inspect": FakeResp(404, {"error": "variable not found"})})
    httpx.Client = fake
    try:
        out = et.inspect_variable(name="nope")
    finally:
        httpx.Client = _REAL
    check("missing variable message", out == "Variable 'nope' is not defined.", out)
    fake = FakeHTTP({"/proxy/variables/inspect": FakeResp(404)})
    httpx.Client = fake
    try:
        out = et.inspect_variable(name="reviews")
    finally:
        httpx.Client = _REAL
    check("older image 404 message", "not available on this VM image" in out, out)
    fake = FakeHTTP({"/proxy/variables/inspect": RuntimeError("SECRETVALUE")})
    httpx.Client = fake
    try:
        out = et.inspect_variable(name="reviews")
    finally:
        httpx.Client = _REAL
    check("exception → short type-only message", out == "Failed to inspect variable: RuntimeError", out)
    os.environ.pop("PROXY_AUTH_TOKEN", None)

    print("== intel agent registers inspect_variable ==")
    try:
        from proxy.notebook.ai.intel import generate as gen
        names = [t.tool_name for t in gen.intel_agent_tools()]
        check("intel tools include inspect_variable", "inspect_variable" in names, names)
        check("intel tools keep the existing five", {"execute_code", "get_variables", "get_notebook_state",
                                                     "install_package", "get_available_data_sources"} <= set(names), names)
        from proxy.notebook.ai.prompts import NOTEBOOK_AGENT_BASE_PROMPT
        check("intel prompt mentions inspect_variable (so the tool must be registered)",
              "inspect_variable" in NOTEBOOK_AGENT_BASE_PROMPT)
    except Exception as e:  # noqa: BLE001
        check("import intel generate", False, f"{type(e).__name__}: {e}")

    print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED: {_failures}'}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
