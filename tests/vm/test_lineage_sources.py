# test-kind: offline
"""
Unit tests for lineage source capture (`sources_read` on /execute results).

Covers app.notebook.read_capture, the _log_read recording hook, sql_engine's
table_refs_in_sql / sources_to_refs, SQL cell results (_run_sql_cell_blocking)
and Python cell results (SandboxExecutor.execute + code_engine._code_result_dict).
Asserts that only IDENTIFIERS are stored — never SQL text, literals or URL queries.

Standalone (no pytest, no AWS): run with
    .venv/bin/python tests/vm/test_lineage_sources.py
Exits non-zero on any failure. Each check prints PASS/FAIL.
"""
import os
import sys
import json
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["ATHENA_DB"] = "microvm_demo_db"
os.environ.pop("AURORA_DATA_API_DATABASE", None)

# .venv lacks plotly and duckdb — stub them BEFORE importing app code.
_plotly = types.ModuleType("plotly")
_px = types.ModuleType("plotly.express")
_plotly.express = _px
sys.modules.setdefault("plotly", _plotly)
sys.modules.setdefault("plotly.express", _px)
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))

import pandas as pd  # noqa: E402

from app.notebook import read_capture  # noqa: E402
from app.notebook import sql_engine as se  # noqa: E402
from app.notebook.sql_engine import (  # noqa: E402
    SourceRef, SourceType, sources_to_refs, table_refs_in_sql, _run_sql_cell_blocking,
)
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.notebook.helpers._common import _log_read  # noqa: E402
from app.notebook import code_engine  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


def _df():
    df = pd.DataFrame({"region": ["us", "eu"], "n": [1, 2]})
    df.attrs = {}
    return df


# ─── sources_to_refs ──────────────────────────────────────────────────────────
def test_sources_to_refs():
    srcs = [
        SourceRef(SourceType.ATHENA, "microvm_demo_db.orders", "orders", database="microvm_demo_db"),
        SourceRef(SourceType.AURORA, "microvm_db.public.customers", "customers",
                  database="microvm_db", schema="public"),
        SourceRef(SourceType.DYNAMODB, 'dynamodb."events-t"', "events-t"),
        SourceRef(SourceType.S3_FILE, "read_parquet('s3://b/k.parquet')", "read_parquet('s3://b/k.parquet')"),
        SourceRef(SourceType.LOCAL_FILE, "'/tmp/x.csv'", "x.csv"),
        SourceRef(SourceType.LOCAL_FILE, "read_csv('/tmp/y.csv')", "y.csv"),
        SourceRef(SourceType.DATAFRAME, "df", "df"),
        SourceRef(SourceType.ATHENA, "microvm_demo_db.orders", "orders", database="microvm_demo_db"),
    ]
    got = sources_to_refs(srcs)
    check("sources_to_refs maps every type, excludes DATAFRAME, dedupes", got == [
        {"kind": "athena", "id": "microvm_demo_db.orders"},
        {"kind": "aurora", "id": "microvm_db.public.customers"},
        {"kind": "dynamodb", "id": "events-t"},
        {"kind": "s3", "id": "s3://b/k.parquet"},
        {"kind": "local", "id": "/tmp/x.csv"},
        {"kind": "local", "id": "/tmp/y.csv"},
    ], repr(got))
    check("sources_to_refs([]) → []", sources_to_refs([]) == [])


# ─── table_refs_in_sql ────────────────────────────────────────────────────────
def test_table_refs_in_sql():
    sql = """
    -- FROM commented_out
    /* JOIN also_commented */
    WITH recent AS (SELECT * FROM db1.orders WHERE note = 'from secret_tbl'),
         agg AS (SELECT * FROM recent)
    SELECT EXTRACT(YEAR FROM order_date), a.*
    FROM agg a
    JOIN cat.db2.items i ON a.id = i.id
    JOIN "My Db"."Quoted Tbl" q ON q.id = a.id
    LEFT JOIN plain_tbl p ON p.id = a.id
    JOIN read_parquet('s3://b/k.parquet') s ON s.id = a.id
    JOIN dynamodb."events" e ON e.id = a.id
    JOIN db1.orders o2 ON o2.id = a.id
    """
    got = table_refs_in_sql(sql)
    check("table_refs_in_sql: 1/2/3-part, quoted, deduped; CTE/comments/literals/calls skipped",
          got == ["db1.orders", "cat.db2.items", "My Db.Quoted Tbl", "plain_tbl"], repr(got))
    check("table_refs_in_sql: quoted /path literal skipped",
          table_refs_in_sql("SELECT * FROM '/tmp/x.csv'") == [])
    check("table_refs_in_sql: non-str → []", table_refs_in_sql(None) == [])


# ─── SQL cells ────────────────────────────────────────────────────────────────
class FakeCatalog:
    def get_all(self):
        return {"entries": [
            {"source_type": "athena", "source_id": "microvm_demo_db.orders",
             "metadata": {"database": "microvm_demo_db"}},
            {"source_type": "aurora", "source_id": "microvm_db.public.customers",
             "metadata": {"database": "microvm_db", "schema": "public"}},
        ]}


_RUNNER_NAMES = ("_run_athena_query_sync", "_run_aurora_query", "_try_partiql",
                 "_scan_dynamodb_table", "_run_duckdb_query")
_ORIG_RUNNERS = {n: getattr(se, n) for n in _RUNNER_NAMES}


def _restore_runners():
    """Put the real sql_engine runners back so later suites in a shared pytest
    session (e.g. test_vm_dag_endpoint's real DuckDB error path) are unaffected."""
    for n, fn in _ORIG_RUNNERS.items():
        setattr(se, n, fn)


def _patch_runners(raise_on=None):
    def maybe(name, val):
        if raise_on == name:
            raise RuntimeError("runner failed: SELECT secret")
        return val
    se._run_athena_query_sync = lambda *a, **k: maybe("athena", _df())
    se._run_aurora_query = lambda *a, **k: maybe("aurora", _df())
    se._try_partiql = lambda *a, **k: maybe("partiql", _df())
    se._scan_dynamodb_table = lambda *a, **k: maybe("scan", _df())
    se._run_duckdb_query = lambda *a, **k: maybe("duckdb", _df())


def _sql(sql, ex=None):
    return _run_sql_cell_blocking(ex or SandboxExecutor(), FakeCatalog(), sql, "result", "c1")


def test_sql_cells():
    try:
        _test_sql_cells()
    finally:
        _restore_runners()


def _test_sql_cells():
    _patch_runners()
    cases = [
        ("athena", "SELECT * FROM microvm_demo_db.orders WHERE k = 'SECRET123'", "athena",
         [{"kind": "athena", "id": "microvm_demo_db.orders"}]),
        ("aurora", "SELECT * FROM microvm_db.public.customers", "aurora",
         [{"kind": "aurora", "id": "microvm_db.public.customers"}]),
        ("dynamodb-partiql", 'SELECT * FROM dynamodb."events"', "dynamodb-partiql",
         [{"kind": "dynamodb", "id": "events"}]),
        ("duckdb s3", "SELECT * FROM read_parquet('s3://b/k.parquet')", "duckdb",
         [{"kind": "s3", "id": "s3://b/k.parquet"}]),
        ("duckdb local", "SELECT * FROM '/tmp/x.csv'", "duckdb",
         [{"kind": "local", "id": "/tmp/x.csv"}]),
        ("mixed athena+dynamodb",
         'SELECT * FROM microvm_demo_db.orders o JOIN dynamodb."events" e ON o.id = e.id',
         "duckdb+athena+dynamodb",
         # Order follows _classify_sources' detection passes (DynamoDB before Athena).
         [{"kind": "dynamodb", "id": "events"}, {"kind": "athena", "id": "microvm_demo_db.orders"}]),
    ]
    for label, sql, engine, expected in cases:
        r = _sql(sql)
        check(f"SQL {label}: success + engine={engine}",
              r.get("success") is True and r.get("engine") == engine, repr((r.get("engine"), r.get("error"))))
        check(f"SQL {label}: sources_read", r.get("sources_read") == expected, repr(r.get("sources_read")))
        dumped = json.dumps(r.get("sources_read"))
        check(f"SQL {label}: no SQL text / literals stored",
              "SELECT" not in dumped and "SECRET123" not in dumped)

    ex = SandboxExecutor()
    ex._namespace["mydf"] = _df()
    r = _sql("SELECT * FROM mydf", ex)
    check("SQL DataFrame-only → sources_read []", r.get("success") and r.get("sources_read") == [],
          repr(r.get("sources_read")))

    _patch_runners(raise_on="athena")
    r = _sql("SELECT * FROM microvm_demo_db.orders")
    check("SQL error path: success False", r.get("success") is False)
    check("SQL error path still returns sources_read",
          r.get("sources_read") == [{"kind": "athena", "id": "microvm_demo_db.orders"}],
          repr(r.get("sources_read")))
    _patch_runners()


# ─── Python cells ─────────────────────────────────────────────────────────────
@_log_read("athena")
def fake_athena(sql, database=None):
    return _df()


@_log_read("aurora")
def fake_aurora(sql, database=None):
    return _df()


@_log_read("s3")
def fake_s3(bucket, key=None):
    return _df()


@_log_read("s3")
def fake_s3_fail(bucket, key=None):
    raise IOError("nope")


@_log_read("url")
def fake_url(url, format="csv"):
    return _df()


@_log_read("dynamodb")
def fake_dynamodb(table_name, limit=None):
    return _df()


CODE = '''
a = fake_athena("SELECT * FROM orders o JOIN other_db.items i ON o.id = i.id WHERE s = 'SECRET123'")
b = fake_aurora("SELECT * FROM public.customers")
c = fake_s3("bucket-a", "data/k.csv")
d = fake_url("https://user:pw@example.com/data.csv?token=abc#frag")
e = fake_dynamodb("events")
a2 = fake_athena("SELECT * FROM orders")
try:
    fake_s3_fail("s3://fail-bucket/x.csv")
except Exception:
    pass
'''


def test_python_cells():
    ex = SandboxExecutor()
    for fn in (fake_athena, fake_aurora, fake_s3, fake_s3_fail, fake_url, fake_dynamodb):
        ex._namespace[fn.__name__] = fn
    res = ex.execute(CODE)
    check("Python cell succeeds", res.success, res.error)
    expected = [
        {"kind": "athena", "id": "microvm_demo_db.orders"},
        {"kind": "athena", "id": "other_db.items"},
        {"kind": "aurora", "id": "microvm_db.public.customers"},
        {"kind": "s3", "id": "s3://bucket-a/data/k.csv"},
        {"kind": "url", "id": "https://example.com/data.csv"},
        {"kind": "dynamodb", "id": "events"},
        {"kind": "s3", "id": "s3://fail-bucket/x.csv"},
    ]
    check("Python cell sources_read ordered + deduped (failing read recorded)",
          res.sources_read == expected, repr(res.sources_read))
    dumped = json.dumps(res.sources_read)
    check("Python sources_read has no SQL / literals / query strings / userinfo",
          "SELECT" not in dumped and "SECRET123" not in dumped and "token=" not in dumped
          and "pw@" not in dumped)
    d = code_engine._code_result_dict(res, ex)
    check("_code_result_dict carries sources_read", d.get("sources_read") == expected)

    # A second cell starts a fresh capture.
    res2 = ex.execute("z = 1")
    check("next cell starts empty", res2.sources_read == [], repr(res2.sources_read))

    # Outside a cell: no capture, no raise.
    try:
        fake_s3("outside", "k")
        ok = True
    except Exception:
        ok = False
    check("reader outside execute does not raise", ok)
    check("reader outside execute records nothing", read_capture.end() == [])


def test_read_capture_caps():
    read_capture.begin()
    read_capture.record("s3", "")
    read_capture.record("s3", None)
    read_capture.record("s3", "x" * 1000)
    for i in range(300):
        read_capture.record("s3", f"s3://b/{i}")
    out = read_capture.end()
    check("read_capture caps at 200 and truncates ids to 512", len(out) == 200 and len(out[0]["id"]) == 512)


# ─── Raw outbound HTTP from cells (http_capture) ──────────────────────────────
# No real network: the requests transport (HTTPAdapter.send), the urllib openers
# (fake HTTP/HTTPS handlers) and httpx (MockTransport) are all faked. The hooks
# sit ABOVE these transports, so they see exactly what user code would send.
import io  # noqa: E402
import email.message  # noqa: E402
import urllib.request  # noqa: E402
import urllib.response  # noqa: E402
import requests  # noqa: E402
import httpx  # noqa: E402
from app.notebook import http_capture  # noqa: E402

_wire = []  # every URL that reached a (fake) transport


def _fake_adapter_send(self, request, **kwargs):
    _wire.append(request.url)
    r = requests.Response()
    r.status_code = 200
    r._content = b'{"ok": true}'
    r.url = request.url
    r.request = request
    return r


def _fake_urllib_open(self, req):
    _wire.append(req.get_full_url())
    resp = urllib.response.addinfourl(io.BytesIO(b'{"ok": true}'), email.message.Message(),
                                      req.get_full_url(), 200)
    resp.msg = "OK"
    return resp


class _FakeHTTP(urllib.request.HTTPHandler):
    http_open = _fake_urllib_open


class _FakeHTTPS(urllib.request.HTTPSHandler):
    https_open = _fake_urllib_open


def _httpx_handler(request):
    _wire.append(str(request.url))
    return httpx.Response(200, json={"ok": True})


HTTP_CODE = '''
import requests, urllib.request, httpx, asyncio
r1 = requests.get("https://date.nager.at/api/v3/publicholidays/2026/US?key=SECRET")
r2 = requests.get("https://user:SECRETPW@date.nager.at/api/v3/AvailableCountries?key=SECRET#frag")
status = r1.status_code
body = urllib.request.urlopen("https://api.open-meteo.com:443/v1/forecast?apikey=SECRET").read()
urllib.request.urlopen(urllib.request.Request("http://example.org:8080/x?token=SECRET"))
with httpx.Client(transport=_mock_transport) as c:
    c.get("https://httpx.example.net/a?key=SECRET")
async def _go():
    async with httpx.AsyncClient(transport=_mock_transport) as ac:
        await ac.get("https://async.example.net/b?key=SECRET")
asyncio.run(_go())
# Skipped: AWS endpoints, loopback, localhost, link-local IMDS.
requests.get("https://s3.us-west-2.amazonaws.com/bucket/key")
requests.get("https://bedrock-runtime.us-west-2.api.aws/x")
requests.get("https://foo.cn-north-1.amazonaws.com.cn/x")
requests.get("http://127.0.0.1:8000/health")
requests.get("http://localhost:8080/x")
requests.get("http://[::1]:9000/x")
_imds = urllib.request.urlopen("http://169.254.169.254/latest/meta-data/")
'''


def test_http_capture():
    orig_send = requests.adapters.HTTPAdapter.send
    requests.adapters.HTTPAdapter.send = _fake_adapter_send
    urllib.request.install_opener(urllib.request.build_opener(_FakeHTTP, _FakeHTTPS))
    try:
        ex = SandboxExecutor()  # installs the hooks
        check("hooks installed for requests/httpx/urllib",
              getattr(requests.Session.request, http_capture._MARK, False)
              and getattr(httpx.Client.send, http_capture._MARK, False)
              and getattr(httpx.AsyncClient.send, http_capture._MARK, False)
              and getattr(urllib.request.OpenerDirector.open, http_capture._MARK, False))

        # Idempotent: installing again (and another executor) does not double-wrap.
        before = (requests.Session.request, httpx.Client.send, httpx.AsyncClient.send,
                  urllib.request.OpenerDirector.open)
        http_capture.install()
        SandboxExecutor()
        after = (requests.Session.request, httpx.Client.send, httpx.AsyncClient.send,
                 urllib.request.OpenerDirector.open)
        check("install() twice does not re-wrap", before == after)
        check("wrapped original is not itself a hook",
              all(not getattr(f.__wrapped__, http_capture._MARK, False) for f in after))

        ex._namespace["_mock_transport"] = httpx.MockTransport(_httpx_handler)
        _wire.clear()
        res = ex.execute(HTTP_CODE)
        check("HTTP cell succeeds", res.success, res.error)
        check("hooks are transparent (response returned unchanged)",
              ex._namespace.get("status") == 200 and ex._namespace.get("body") == b'{"ok": true}',
              repr((ex._namespace.get("status"), ex._namespace.get("body"))))
        check("every call still reached its transport", len(_wire) == 13, repr(_wire))
        expected = [
            {"kind": "url", "id": "https://date.nager.at"},
            {"kind": "url", "id": "https://api.open-meteo.com"},
            {"kind": "url", "id": "http://example.org:8080"},
            {"kind": "url", "id": "https://httpx.example.net"},
            {"kind": "url", "id": "https://async.example.net"},
        ]
        check("HTTP sources grouped by host; AWS/loopback/localhost/IMDS skipped",
              res.sources_read == expected, repr(res.sources_read))
        dumped = json.dumps(code_engine._code_result_dict(res, ex).get("sources_read"))
        check("no path / query / userinfo / fragment recorded",
              "SECRET" not in dumped and "/api/" not in dumped and "frag" not in dumped
              and "user" not in dumped, dumped)

        # Outside an active capture: nothing recorded.
        calls = []
        real_record = read_capture.record
        read_capture.record = lambda *a, **k: calls.append(a)
        try:
            requests.get("https://date.nager.at/outside")
            urllib.request.urlopen("https://date.nager.at/outside")
        finally:
            read_capture.record = real_record
        check("HTTP outside a cell capture records nothing", calls == [], repr(calls))

        # A reader helper that uses requests internally: only the helper's id.
        @_log_read("url")
        def fake_read_url(url, format="json"):
            requests.get(url)
            urllib.request.urlopen(url)
            return _df()
        ex._namespace["fake_read_url"] = fake_read_url
        res = ex.execute('d = fake_read_url("https://api.helper.io/data.json?key=SECRET")\n'
                         '_a = requests.get("https://after.example.com/x")')
        check("helper's own record wins; internal HTTP suppressed; suppression ends with the helper",
              res.sources_read == [{"kind": "url", "id": "https://api.helper.io/data.json"},
                                   {"kind": "url", "id": "https://after.example.com"}],
              repr(res.sources_read))
        check("helper scope depth back to 0", read_capture._helper_depth == 0)

        # record() failing never raises into user code.
        def boom(*a, **k):
            raise RuntimeError("record failed")
        read_capture.record = boom
        try:
            res = ex.execute('rr = requests.get("https://date.nager.at/x")\n'
                             'ok = rr.status_code\n'
                             '_y = urllib.request.urlopen("https://date.nager.at/y")')
        finally:
            read_capture.record = real_record
        check("hooks never raise when record() fails",
              res.success and ex._namespace.get("ok") == 200, res.error)

        # User-visible exceptions from the transport still propagate unchanged.
        def refuse(self, request, **kwargs):
            raise requests.exceptions.ConnectionError("refused")
        requests.adapters.HTTPAdapter.send = refuse
        res = ex.execute('try:\n'
                         '    requests.get("https://down.example.com/")\n'
                         '    err = None\n'
                         'except requests.exceptions.ConnectionError as e:\n'
                         '    err = str(e)\n')
        check("transport exceptions pass through; failed host still recorded",
              ex._namespace.get("err") == "refused"
              and res.sources_read == [{"kind": "url", "id": "https://down.example.com"}],
              repr((ex._namespace.get("err"), res.sources_read)))
    finally:
        requests.adapters.HTTPAdapter.send = orig_send
        urllib.request.install_opener(None)


def test_host_id():
    cases = [
        ("https://Date.Nager.AT/api?key=SECRET", "https://date.nager.at"),
        ("https://h.example.com:443/x", "https://h.example.com"),
        ("http://h.example.com:80/x", "http://h.example.com"),
        ("https://h.example.com:8443/x", "https://h.example.com:8443"),
        ("http://[2001:db8::1]:8080/x", "http://[2001:db8::1]:8080"),
        ("file:///etc/passwd", None),
        ("data:text/plain,hi", None),
        ("https://169.254.170.2/creds", None),
        ("http://0.0.0.0:80/", None),
        ("https://sts.amazonaws.com/", None),
        ("https://notamazonaws.com/", "https://notamazonaws.com"),
    ]
    for url, want in cases:
        got = http_capture.host_id(url)
        check(f"host_id({url!r}) → {want!r}", got == want, repr(got))


if __name__ == "__main__":
    test_sources_to_refs()
    test_table_refs_in_sql()
    test_sql_cells()
    test_python_cells()
    test_read_capture_caps()
    test_http_capture()
    test_host_id()
    print()
    if _fails:
        print(f"{len(_fails)} FAILURE(S): {_fails}")
        sys.exit(1)
    print("All checks passed.")
