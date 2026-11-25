#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review C-B-01): server-to-server calls from the chat agent and
the proxy's AI code back into the proxy carry X-Proxy-Auth when PROXY_AUTH_TOKEN
is set, and omit it cleanly when it is unset.

Covered at runtime (httpx stubbed, no network):
  - agent/ai_assistant/tools/execution_tools.py: promote_attachment,
    get_available_data_sources (the two fixed sites) and the proxy copies;
  - proxy/notebook/ai/intel/context.py: _fetch_session_catalog;
  - proxy/notebook/ai/notebook_agent.py: _prefetch_live_variables (its
    /variables/redacted call AND the schema call it hands the headers to).
Static guard: every function in the AI tool/intel/agent modules that calls httpx
must build its headers with the token (PROXY_AUTH_TOKEN / _get_headers()) or take
`headers` from its caller.
Run:  .venv/bin/python tests/ai/test_server_calls_auth_header.py   (exit 0 = pass)
"""
import ast
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
import logging  # noqa: E402
import httpx  # noqa: E402
from ai_assistant.tools import execution_tools as agent_et  # noqa: E402
from proxy.notebook.ai.tools import execution_tools as proxy_et  # noqa: E402
from proxy.notebook.ai.intel import context as intel_ctx  # noqa: E402
from proxy.notebook.ai import notebook_agent as proxy_na  # noqa: E402
logging.disable(logging.CRITICAL)

TOKEN = "test-token-not-real"
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


calls = []  # (method, url, headers)


class FakeResp:
    def __init__(self, url):
        self.status_code = 200
        self.content = b"{}"
        self.text = "{}"
        self._url = url

    def json(self):
        if "promote-attachment" in self._url:
            return {"s3_uri": "s3://b/k.csv", "name": "k.csv"}
        if "/variables/schema" in self._url:
            return {"variables": {}}
        if "/variables" in self._url:
            return {"variables": {"df": {"type": "DataFrame", "shape": "(1, 1)"}}}
        return {"entries": []}


def _record(method, url, kw):
    calls.append((method, url, dict(kw.get("headers") or {})))
    return FakeResp(url)


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        return _record("GET", url, kw)

    def post(self, url, **kw):
        return _record("POST", url, kw)


_real_client, _real_get = httpx.Client, httpx.get
httpx.Client = FakeClient
httpx.get = lambda url, **kw: _record("GET", url, kw)

CTX = {"session_id": "sess-1234", "proxy_url": "http://proxy.test"}


def call_tool(fn, *args):
    # strands @tool objects stay callable with the original signature.
    return fn(*args)


SITES = [
    ("agent promote_attachment", lambda: (agent_et.set_execution_context(CTX),
                                          call_tool(agent_et.promote_attachment, "s3://b/attachments/x.csv"))),
    ("agent get_available_data_sources", lambda: (agent_et.set_execution_context(CTX),
                                                  call_tool(agent_et.get_available_data_sources))),
    ("proxy promote_attachment", lambda: (proxy_et.set_execution_context(CTX),
                                          call_tool(proxy_et.promote_attachment, "s3://b/attachments/x.csv"))),
    ("proxy get_available_data_sources", lambda: (proxy_et.set_execution_context(CTX),
                                                  call_tool(proxy_et.get_available_data_sources))),
    ("intel _fetch_session_catalog", lambda: intel_ctx._fetch_session_catalog("http://proxy.test", "sess-1234")),
    ("fix _prefetch_live_variables", lambda: proxy_na._prefetch_live_variables("sess-1234", "http://proxy.test")),
]

try:
    for token in (TOKEN, None):
        if token:
            os.environ["PROXY_AUTH_TOKEN"] = token
        else:
            os.environ.pop("PROXY_AUTH_TOKEN", None)
        state = "set" if token else "unset"
        print(f"\nPROXY_AUTH_TOKEN {state}")
        for label, fn in SITES:
            calls.clear()
            try:
                fn()
            except Exception as e:  # a crash here is a failure, not a skip
                check(f"{label}: runs", False, f"{type(e).__name__}: {e}")
                continue
            check(f"{label}: made a call to the proxy", bool(calls) and all(
                c[1].startswith("http://proxy.test") for c in calls), calls)
            for method, url, hdrs in calls:
                path = url.replace("http://proxy.test", "")
                if token:
                    check(f"{label}: {method} {path} sends X-Proxy-Auth",
                          hdrs.get("X-Proxy-Auth") == TOKEN, sorted(hdrs))
                else:
                    check(f"{label}: {method} {path} omits X-Proxy-Auth",
                          "X-Proxy-Auth" not in hdrs, sorted(hdrs))
                check(f"{label}: {method} {path} keeps X-Session-Id",
                      hdrs.get("X-Session-Id") == "sess-1234", sorted(hdrs))
    # the prefetch must also have reached the schema call with the same headers
    os.environ["PROXY_AUTH_TOKEN"] = TOKEN
    calls.clear()
    proxy_na._prefetch_live_variables("sess-1234", "http://proxy.test")
    check("fix prefetch: schema call is authenticated too",
          any("/variables/schema" in u and h.get("X-Proxy-Auth") == TOKEN for _, u, h in calls),
          [(u, sorted(h)) for _, u, h in calls])
finally:
    httpx.Client, httpx.get = _real_client, _real_get
    os.environ.pop("PROXY_AUTH_TOKEN", None)

# Static guard
print("\nstatic guard")
GUARD_DIRS = ["agent/ai_assistant/tools", "proxy/notebook/ai/tools", "proxy/notebook/ai/intel"]
GUARD_FILES = ["agent/ai_assistant/notebook_agent.py", "proxy/notebook/ai/notebook_agent.py"]
files = list(GUARD_FILES)
for d in GUARD_DIRS:
    for name in sorted(os.listdir(os.path.join(ROOT, d))):
        if name.endswith(".py"):
            files.append(f"{d}/{name}")


def uses_httpx(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "httpx" \
                and n.attr in ("get", "post", "put", "delete", "request", "Client", "AsyncClient", "stream"):
            return True
    return False


offenders, checked = [], 0
for rel in files:
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not uses_httpx(fn):
            continue
        checked += 1
        body = ast.get_source_segment(src, fn) or ""
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        ok = ("PROXY_AUTH_TOKEN" in body or "_get_headers()" in body or "headers" in params)
        if not ok:
            offenders.append(f"{rel}:{fn.lineno} {fn.name}")
check(f"every httpx caller ({checked} functions) attaches the token or takes caller headers",
      checked > 0 and not offenders, offenders)

print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
