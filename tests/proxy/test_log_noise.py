#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the proxy log-noise changes (proxy-log-chatter, section A):
  - access-log policy (proxy/server.py _proxy_quiet + _proxy_access_log):
      * fast 2xx of polled endpoints go to DEBUG: POST /sessions/costs (was the
        stale '/session-cost' prefix), GET /workbook-intel, GET /datasources and
        GET /datasources/*, plus the existing /instances, /metrics, /logs, ...;
      * writes on the same prefixes stay INFO: POST /workbook-intel/generate,
        DELETE /datasources/s3-file, POST /datasources/sagemaker-endpoint/lifecycle;
      * a polled endpoint that is slow (>= 1 s) stays INFO, a 4xx stays INFO and
        a 5xx is a WARNING;
      * other paths (PUT /notebooks/1) stay INFO;
      * LOG_ACCESS_POLLS=1 restores INFO for the polled paths;
      * the lines come from the 'proxy.server' logger (the one basicConfig feeds);
  - the 'httpx' logger is WARNING in the proxy and in the pool manager, and the
    pool manager's basicConfig carries the proxy's timestamped format;
  - 'S3 Tables discovery: N table(s)' is INFO on the first discovery and when N
    changes, DEBUG when unchanged (the ERROR path is untouched).
No AWS: storage points at a temp SQLite file, the access middleware runs in a
small FastAPI app, Glue and S3 Tables are stubbed.
Run:
    .venv/bin/python tests/proxy/test_log_noise.py
"""
import asyncio
import logging
import os
import sys
import tempfile
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="lognoise_")
os.environ["STORAGE_DB_PATH"] = os.path.join(_TMP, "microvm.db")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("PROXY_AUTH_TOKEN", "test-token")
os.environ.pop("PROXY_BIND_HOST", None)
os.environ.pop("LOG_ACCESS_POLLS", None)
import httpx  # noqa: E402
from fastapi import FastAPI, Response  # noqa: E402
import proxy.server as server  # noqa: E402
_checks = []
def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
    def emit(self, record):
        self.records.append(record)
def _mini_app(delay=0.0, status=200):
    app = FastAPI()
    app.middleware("http")(server._proxy_access_log)
    async def handler():
        if delay:
            await asyncio.sleep(delay)
        return Response(status_code=status, content=b"{}", media_type="application/json")
    for path in ("/sessions/costs", "/workbook-intel", "/workbook-intel/generate", "/datasources",
                 "/datasources/catalog", "/datasources/schema", "/datasources/s3-file",
                 "/datasources/sagemaker-endpoint/lifecycle", "/instances", "/notebooks/1",
                 "/logs/metrics-summary"):
        app.add_api_route(path, handler, methods=["GET", "POST", "PUT", "DELETE"])
    return app
async def _levels(calls, delay=0.0, status=200):
    cap = Capture()
    lg = logging.getLogger(server.logger.name)
    old = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    try:
        transport = httpx.ASGITransport(app=_mini_app(delay, status))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            out = {}
            for method, path in calls:
                cap.records.clear()
                await c.request(method, path)
                recs = [r for r in cap.records if "\u2190" in r.getMessage()]
                out[(method, path)] = recs[0].levelname if recs else None
            return out
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old)
def test_access_policy():
    print("\n-- access-log policy --")
    check("access lines come from the 'proxy.server' logger", server.logger.name == "proxy.server")
    quiet = [("POST", "/sessions/costs"), ("GET", "/workbook-intel"), ("GET", "/datasources"),
             ("GET", "/datasources/catalog"), ("GET", "/datasources/schema"), ("GET", "/instances"),
             ("GET", "/logs/metrics-summary")]
    loud = [("POST", "/workbook-intel/generate"), ("DELETE", "/datasources/s3-file"),
            ("POST", "/datasources/sagemaker-endpoint/lifecycle"), ("PUT", "/notebooks/1")]
    lv = asyncio.run(_levels(quiet + loud))
    for k in quiet:
        check(f"fast 200 {k[0]} {k[1]} -> DEBUG", lv[k] == "DEBUG", lv[k])
    for k in loud:
        check(f"fast 200 {k[0]} {k[1]} -> INFO", lv[k] == "INFO", lv[k])
    slow = asyncio.run(_levels([("GET", "/datasources"), ("POST", "/sessions/costs")], delay=1.05))
    check("slow (>= 1 s) polled request stays INFO", set(slow.values()) == {"INFO"}, slow)
    four = asyncio.run(_levels([("GET", "/workbook-intel")], status=404))
    check("4xx polled request stays INFO", four[("GET", "/workbook-intel")] == "INFO", four)
    five = asyncio.run(_levels([("GET", "/datasources"), ("POST", "/sessions/costs")], status=502))
    check("5xx polled request is a WARNING", set(five.values()) == {"WARNING"}, five)
    check("_proxy_quiet: GET-only prefixes are not quiet for POST/DELETE",
          not server._proxy_quiet("/workbook-intel/generate", "POST")
          and not server._proxy_quiet("/datasources/s3-file", "DELETE")
          and server._proxy_quiet("/datasources/s3-file", "GET"))
    check("the stale '/session-cost' prefix is gone", "/session-cost" not in server._PROXY_QUIET_PREFIXES)
    server._LOG_ACCESS_POLLS = True
    try:
        on = asyncio.run(_levels([("POST", "/sessions/costs"), ("GET", "/workbook-intel")]))
    finally:
        server._LOG_ACCESS_POLLS = False
    check("LOG_ACCESS_POLLS=1 restores INFO for polled paths", set(on.values()) == {"INFO"}, on)
def test_httpx_and_pool_logging():
    print("\n-- httpx / pool manager logging --")
    check("proxy: httpx logger is WARNING", logging.getLogger("httpx").level == logging.WARNING)
    src = open(os.path.join(ROOT, "pool_manager", "server.py")).read()
    check("pool manager: httpx logger set to WARNING",
          'logging.getLogger("httpx").setLevel(logging.WARNING)' in src)
    check("pool manager: timestamped basicConfig format like the proxy",
          '%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s' in src)
def test_s3tables_log_on_change():
    print("\n-- S3 Tables discovery: log on change --")
    from proxy.platform.datasources import athena
    os.environ.update(S3TABLES_BUCKET="b", S3TABLES_NAMESPACE="ns", S3TABLES_ATHENA_CATALOG="cat",
                      ACCOUNT_ID="123456789012")
    p = athena.AthenaSchemaProvider()
    p._databases_to_scan = lambda glue: []
    athena.aws_clients.get_client = lambda *a, **k: object()
    counts = iter([3, 3, 3, 4, 4])
    p._discover_s3tables = lambda: [athena.SourceRef(source_type="athena", source_id=f"t{i}", display_name=f"t{i}")
                                    for i in range(next(counts))]
    athena._S3T_LAST_COUNT.clear()
    cap = Capture()
    lg = logging.getLogger(athena.logger.name)
    lg.addHandler(cap)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        levels = []
        for _ in range(5):
            cap.records.clear()
            p._discover_blocking()
            recs = [r for r in cap.records if r.getMessage().startswith("S3 Tables discovery:")]
            levels.append(recs[0].levelname if recs else None)
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old)
    check("first discovery INFO, unchanged DEBUG, changed INFO, unchanged DEBUG",
          levels == ["INFO", "DEBUG", "DEBUG", "INFO", "DEBUG"], levels)
    check("logger name is the provider module's", athena.logger.name == "proxy.platform.datasources.athena")
test_access_policy()
test_httpx_and_pool_logging()
test_s3tables_log_on_change()
failed = [n for n, ok in _checks if not ok]
print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
sys.exit(1 if failed else 0)
