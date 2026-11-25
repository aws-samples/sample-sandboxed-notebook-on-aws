#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for the proxy DAG orchestration (design.md §4, §12 test_dag_capture).

proxy/notebook/dag_service.py + the DAG hooks in proxy/platform/routes/microvm.py
and proxy/notebook/routes/notebooks.py, driven through the REAL proxy app
(proxy.server.app) over raw ASGI calls (exact raw paths, no client-side URL
normalisation). No network, no AWS, no live DB:
  * storage = a temp SQLite DB (DB_DIR/DB_PATH redirected BEFORE proxy.server
    is imported; the live proxy/data/microvm.db is never opened);
  * the VM = an httpx.MockTransport behind a patched http_clients.vm_client();
  * app.state.vm_manager = a small fake (session → VM map, tokens, states).

Covers: capture_forwarded for execute / execute-sql / set-variable / unknown
cell → pending; cell_id coercion (7 → "7"; True / "" / 257 chars → runtime-only);
origin "agent" → runtime-only; scratch session → no dag and no runtime; the
session cache does not cache misses; the batch relay emits the dag frame before
done and swallows the runtime frame (+ background apply on a broken stream);
the /proxy/{path} denylist (403 + zero VM requests) while /proxy/variables still
forwards; attach retry (503 twice then 200 → reconcile on the 3rd GET, no PUT
before it; 3 failures → synced_vm_id set, warning, pushes resume); flush before
/checkpoint-save; PUT /notebooks with dag_build/dag keys; GET /notebooks/{id},
GET /notebooks/{id}/dag, POST, PUT and version restore return "dag";
unauthenticated GET /notebooks/1/dag → 401; reconcile marks not_in_checkpoint;
/launch reuse path calls attach_soon; no new auth exemption.
Change check + delta transport (delta-plan §0.5/§0.7): execute / set-variable /
batch payloads are `dag_delta` (full `dag` on the first document or above the
size guard) and every one applied to a GET mirror equals the stored doc; a leaf
re-run is `unchanged`; an identical set-variable twice is the unchanged marker
with the row untouched; runtime-only captures make no VM call; the VM side is
the REAL DagStore (PUT + PATCH): coalesced pushes send ONE cumulative PATCH from
the VM-acked rev to the current DB rev; PATCH 409 → one full PUT; 405 →
no_patch then PUTs; flush after a code-only save PATCHes the graph ops; attach
→ full PUT; PATCH /proxy/dag is not forwarded.

Run:  .venv/bin/python tests/dag/test_dag_capture.py   (or python3; exit 0 = pass)
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Temp SQLite BEFORE anything imports proxy.server (it calls initialize()) ──
import proxy.storage.sqlite_db as _sq  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="dag_capture_")
_sq.DB_DIR = _TMP
_sq.DB_PATH = os.path.join(_TMP, "dag_capture.db")
_sq._local = threading.local()
_sq._HELD_SESSION_LOCKS.clear()

_TOKEN = "dag-capture-test-token"
os.environ["PROXY_AUTH_TOKEN"] = _TOKEN
os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
os.environ.pop("POOL_ENABLED", None)

import httpx  # noqa: E402

import proxy.server as proxy_server  # noqa: E402
from proxy.notebook import dag_service  # noqa: E402
from proxy.platform import http_clients  # noqa: E402
from proxy.platform import auth as proxy_auth  # noqa: E402
from proxy.platform.routes import microvm as microvm_routes  # noqa: E402
from proxy.storage import astorage  # noqa: E402
from app.notebook.dag_contract import apply_delta, cell_code_hash, rev  # noqa: E402

APP = proxy_server.app

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ─── Fakes ─────────────────────────────────────────────────────────────────────

class FakeVmManager:
    def __init__(self):
        self.sessions = {}
        self.states = {}
        self.active_microvms = {}

    async def get_session_vm_async(self, sid):
        return self.sessions.get(sid)

    def is_session_launching(self, sid):
        return False

    def assign_arrival_seq(self, sid):
        return 0

    def is_recovering(self, sid):
        return False

    def invalidate_session_vm(self, sid):
        pass

    async def get_auth_token(self, vm_id):
        return f"vm-token-{vm_id}"

    async def get_vm_state(self, vm_id):
        return self.states.get(vm_id, "RUNNING")


class FakeVM:
    """httpx MockTransport handler: records every request, answers per route."""

    def __init__(self):
        self.requests = []
        self.routes = {}

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        fn = self.routes.get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(200, json={"ok": True, "path": request.url.path})
        return fn(request)

    def count(self, method=None, path=None):
        return sum(1 for r in self.requests
                   if (method is None or r.method == method) and (path is None or r.url.path == path))


VM = FakeVM()
VMM = FakeVmManager()
_client_holder = {}


def _fake_vm_client():
    c = _client_holder.get("c")
    if c is None:
        c = httpx.AsyncClient(transport=httpx.MockTransport(VM.handler))
        _client_holder["c"] = c
    return c


http_clients.vm_client = _fake_vm_client
APP.state.vm_manager = VMM


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


LOGS = _Records()
logging.getLogger("proxy.notebook.dag_service").addHandler(LOGS)
logging.getLogger("proxy.notebook.dag_service").setLevel(logging.DEBUG)


# ─── Raw ASGI client (exact raw path, like uvicorn: path = unquote(raw)) ──────

async def asgi(method, raw_path, *, body=None, headers=None, auth=True):
    hdrs = {"content-type": "application/json"}
    if auth:
        hdrs["x-proxy-auth"] = _TOKEN
    for k, v in (headers or {}).items():
        hdrs[k.lower()] = v
    payload = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    hdrs["content-length"] = str(len(payload))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http",
        "path": unquote(raw_path), "raw_path": raw_path.encode(), "query_string": b"",
        "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8080),
    }
    msgs = [{"type": "http.request", "body": payload, "more_body": False}]
    never = asyncio.Event()

    async def receive():
        if msgs:
            return msgs.pop(0)
        await never.wait()
        return {"type": "http.disconnect"}

    out = {"status": None, "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await APP(scope, receive, send)
    return out["status"], out["body"]


def _json(b):
    try:
        return json.loads(b)
    except Exception:
        return None


def _node(doc, cid):
    return next((n for n in (doc or {}).get("nodes") or [] if n.get("cell_id") == cid), None)


def _rt(clock, vars_=None, runs=None):
    return {"clock": clock, "vars": vars_ or {}, "runs": runs or {}}


class Mirror:
    """The browser's view: a doc from GET, advanced by each response's `dag`
    (full) or `dag_delta` (apply_delta). take() also checks the result equals
    the stored doc (GET /notebooks/{id}/dag)."""

    def __init__(self, nid):
        self.nid = nid
        self.doc = None
        self.ok = True
        self.n = 0

    async def sync(self):
        self.doc = await astorage.notebook_dag_get(self.nid)

    async def take(self, d):
        if "dag" in d:
            self.doc = d["dag"]
        elif "dag_delta" in d:
            self.doc = apply_delta(self.doc, d["dag_delta"])
        else:
            return None
        self.n += 1
        stored = await astorage.notebook_dag_get(self.nid)
        if self.doc != stored:
            self.ok = False
        return self.doc


CELLS = [
    {"id": "c1", "type": "code", "code": "x = 1"},
    {"id": "md", "type": "markdown", "code": "# notes"},
    {"id": "c2", "type": "code", "code": "y = x + 1"},
    {"id": "7", "type": "code", "code": "z = 3"},
    {"id": "q1", "type": "sql", "code": "SELECT 1", "outputVariable": "df"},
]


async def _wait(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# ─── Tests ─────────────────────────────────────────────────────────────────────

async def _t_notebook_routes(ctx):
    print("notebook routes")
    st, b = await asgi("POST", "/notebooks", body={"name": "nb1", "cells": CELLS, "session_id": "s1",
                                                    "dag": {"schema": 1, "nodes": ["evil"]},
                                                    "dag_build": "evil"})
    nb = _json(b) or {}
    check("POST /notebooks → 200", st == 200, f"{st} {b[:200]!r}")
    check("POST /notebooks returns 'dag' with the 4 non-markdown nodes",
          [n["cell_id"] for n in (nb.get("dag") or {}).get("nodes", [])] == ["c1", "c2", "7", "q1"])
    check("POST dag trigger == 'create'", (nb.get("dag") or {}).get("trigger") == "create")
    nid = nb.get("id")
    ctx["nid"] = nid

    st, b = await asgi("GET", f"/notebooks/{nid}")
    got = _json(b) or {}
    check("GET /notebooks/{id} returns 'dag'", st == 200 and isinstance(got.get("dag"), dict))
    check("GET /notebooks/{id} in-sync doc not rebuilt (version 1)", (got.get("dag") or {}).get("version") == 1)

    st, b = await asgi("GET", f"/notebooks/{nid}/dag")
    doc = _json(b) or {}
    check("GET /notebooks/{id}/dag → 200 doc", st == 200 and doc.get("schema") == 1 and doc.get("notebook_id") == nid)
    st, b = await asgi("GET", "/notebooks/999999/dag")
    check("GET /notebooks/{missing}/dag → 404 {'error':'Notebook not found'}",
          st == 404 and _json(b) == {"error": "Notebook not found"})

    st, b = await asgi("GET", "/notebooks/1/dag", auth=False)
    check("unauthenticated GET /notebooks/1/dag → 401", st == 401, str(st))

    edited = [dict(c) for c in CELLS]
    edited[2]["code"] = "y = x + 2"
    st, b = await asgi("PUT", f"/notebooks/{nid}", body={"cells": edited, "dag_build": "x",
                                                         "dag": {"schema": 1, "nodes": []}})
    put = _json(b) or {}
    check("PUT /notebooks/{id} with dag_build/dag keys → 200", st == 200 and put.get("success") is True,
          f"{st} {b[:300]!r}")
    pdag = put.get("dag") or {}
    check("PUT response carries the stored doc (client 'dag' ignored)",
          len(pdag.get("nodes") or []) == 4 and pdag.get("trigger") == "save" and pdag.get("version") == 2)
    check("PUT rebuilt the edited node's code hash",
          (_node(pdag, "c2") or {}).get("code_hash") == cell_code_hash("code", "y = x + 2"))

    # Version restore → dag (trigger restore_version)
    st, b = await asgi("POST", f"/notebooks/{nid}/versions", body={"label": "v-edited", "trigger": "manual"})
    vg = (_json(b) or {}).get("version_group_id")
    st, b = await asgi("PUT", f"/notebooks/{nid}", body={"cells": CELLS})
    st, b = await asgi("POST", f"/notebooks/{nid}/versions/{vg}/restore")
    rs = _json(b) or {}
    check("version restore returns 'dag'", st == 200 and isinstance(rs.get("dag"), dict), f"{st} {b[:200]!r}")
    check("version restore dag trigger == 'restore_version' with the restored code",
          (rs.get("dag") or {}).get("trigger") == "restore_version"
          and (_node(rs.get("dag"), "c2") or {}).get("code_hash") == cell_code_hash("code", "y = x + 2"))
    # Back to CELLS for the capture tests.
    await asgi("PUT", f"/notebooks/{nid}", body={"cells": CELLS})


async def _t_capture(ctx):
    print("capture_forwarded")
    nid = ctx["nid"]
    VMM.sessions["s1"] = {"vm_id": "vm-1", "endpoint": "ep1"}
    pushes = []
    real_push = dag_service.schedule_push
    dag_service.schedule_push = lambda sid, nid=None: pushes.append((sid, nid))
    M = Mirror(nid)
    await M.sync()
    try:
        hdr = {"X-Session-Id": "s1"}
        rt1 = _rt(1, {"x": {"defined_by": "c1", "last_cell": "c1", "last_exec": 1}},
                  {"c1": {"code_hash": cell_code_hash("code", "x = 1"), "clock": 1, "ok": True}})
        VM.routes[("POST", "/execute")] = lambda r: httpx.Response(200, json={
            "success": True, "output": "", "variables_read": [], "variables_created": ["x"],
            "variables_rebound": [], "variables_mutated": [], "sources_read": [],
            "runtime": rt1})
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", "cell_id": "c1"}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        n1 = _node(dd, "c1") or {}
        check("execute on a notebook with a doc → 200 with 'dag_delta', without 'runtime'/'dag'",
              st == 200 and "dag_delta" in d and "dag" not in d and "runtime" not in d,
              f"{st} {b[:200]!r}")
        check("execute: node c1 runtime facts, writes x, last_run_seq 1",
              n1.get("facts_origin") == "runtime" and n1.get("writes") == ["x"] and n1.get("last_run_seq") == 1)
        check("execute: doc.runtime copied from the VM with vm_id",
              dd.get("runtime", {}).get("clock") == 1
              and dd.get("runtime", {}).get("vm_id") == "vm-1")
        check("execute schedules a push", pushes[-1:] == [("s1", nid)])
        check("execute forwarded once to the VM", VM.count("POST", "/execute") == 1)

        # execute-sql
        VM.routes[("POST", "/execute-sql")] = lambda r: httpx.Response(200, json={
            "success": True, "output_variable": "df", "variables_read": [], "variables_created": ["df"],
            "sources_read": [{"kind": "athena", "id": "db.t"}], "runtime": _rt(2)})
        st, b = await asgi("POST", "/proxy/execute-sql",
                           body={"sql": "  SELECT 1\n", "cell_id": "q1", "output_variable": "df"}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        nq = _node(dd, "q1") or {}
        check("execute-sql: node q1 writes [df], sources captured, run_hash = sql hash",
              nq.get("writes") == ["df"] and nq.get("sources") == [{"kind": "athena", "id": "db.t"}]
              and nq.get("run_hash") == cell_code_hash("sql", "SELECT 1"), json.dumps(nq)[:300])
        seq_before = dd.get("run_seq")

        # set-variable → runtime only
        VM.routes[("POST", "/set-variable")] = lambda r: httpx.Response(200, json={
            "success": True, "name": "k", "runtime": _rt(3, {"k": {"defined_by": None}})})
        st, b = await asgi("POST", "/proxy/set-variable", body={"name": "k", "value": 1}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        check("set-variable: dag runtime refreshed, run_seq unchanged, no 'runtime' key",
              dd.get("runtime", {}).get("clock") == 3 and dd.get("run_seq") == seq_before
              and "runtime" not in d)

        # variables/delete (Clean up untracked variables) → runtime only
        VM.routes[("POST", "/variables/delete")] = lambda r: httpx.Response(200, json={
            "deleted": ["k"], "skipped": [], "runtime": _rt(4, {})})
        st, b = await asgi("POST", "/proxy/variables/delete", body={"names": ["k"]}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        check("variables/delete: dag runtime refreshed, run_seq unchanged, no 'runtime' key",
              st == 200 and dd.get("runtime", {}).get("clock") == 4
              and dd.get("run_seq") == seq_before and "runtime" not in d
              and d.get("deleted") == ["k"], f"{st} {b[:200]!r}")

        # unknown cell → pending
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", "cell_id": "new1"}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        check("unknown cell id → pending[new1]", "new1" in (dd.get("pending") or {}))

        # numeric cell id 7 → node "7"
        st, b = await asgi("POST", "/proxy/execute", body={"code": "z = 3", "cell_id": 7}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        n7 = _node(dd, "7") or {}
        check("numeric cell_id 7 captured as node '7'", n7.get("last_run_seq") == dd.get("run_seq"))

        # invalid ids / agent → runtime-only
        for label, extra in (("True", {"cell_id": True}), ("''", {"cell_id": ""}),
                             ("257 chars", {"cell_id": "a" * 257}),
                             ("origin agent", {"cell_id": "c1", "origin": "agent"})):
            before = (await astorage.notebook_dag_get(nid))["run_seq"]
            st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", **extra}, headers=hdr)
            d = _json(b) or {}
            dd = await M.take(d) or {}
            check(f"cell_id {label} → runtime-only (run_seq unchanged, dag present)",
                  st == 200 and dd.get("run_seq") == before and "runtime" not in d)

        # AI Fix agent SQL test run (execute_sql tool body: no cell_id, origin agent,
        # scratch output var) → runtime-only: no DAG run for any cell.
        from proxy.notebook.ai.tools.execution_tools import FIX_SQL_TEST_OUTPUT_VAR
        before_doc = await astorage.notebook_dag_get(nid)
        q1_before = (_node(before_doc, "q1") or {}).get("last_run_seq")
        st, b = await asgi("POST", "/proxy/execute-sql",
                           body={"sql": "SELECT 1", "output_variable": FIX_SQL_TEST_OUTPUT_VAR,
                                 "origin": "agent"}, headers=hdr)
        d = _json(b) or {}
        dd = await M.take(d) or {}
        check("agent SQL test run → runtime-only (run_seq unchanged, q1 untouched, no 'runtime')",
              st == 200 and dd.get("run_seq") == before_doc["run_seq"]
              and (_node(dd, "q1") or {}).get("last_run_seq") == q1_before
              and "runtime" not in d, f"{st} {b[:200]!r}")

        # scratch session → no dag, no runtime
        VMM.sessions["s-scratch"] = {"vm_id": "vm-9", "endpoint": "ep9"}
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", "cell_id": "c1"},
                           headers={"X-Session-Id": "s-scratch"})
        d = _json(b) or {}
        dd = await M.take(d) or {}
        check("session without notebook → no 'dag'/'dag_delta', no 'runtime'",
              st == 200 and "dag" not in d and "dag_delta" not in d and "runtime" not in d)

        # Session cache: a miss is not cached (link, then execute captures immediately)
        st, b = await asgi("POST", "/notebooks", body={"name": "nb2", "cells": [CELLS[0]]})
        nid2 = (_json(b) or {}).get("id")
        VMM.sessions["s2"] = {"vm_id": "vm-2", "endpoint": "ep2"}
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", "cell_id": "c1"},
                           headers={"X-Session-Id": "s2"})
        check("s2 before link → no dag", not ({"dag", "dag_delta"} & set(_json(b) or {})))
        await astorage.notebook_update(nid2, session_id="s2")
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", "cell_id": "c1"},
                           headers={"X-Session-Id": "s2"})
        d = _json(b) or {}
        check("s2 right after link → captured (miss was not cached)",
              (d.get("dag") or d.get("dag_delta") or {}).get("notebook_id") == nid2)
        ctx["nid2"] = nid2

        # Hits are cached (no storage call on the second lookup)
        calls = []
        real = astorage._sync.notebook_id_for_session

        def counting(sid):
            calls.append(sid)
            return real(sid)
        astorage._sync.notebook_id_for_session = counting
        astorage._wrapped.pop("notebook_id_for_session", None)
        try:
            dag_service.invalidate_session("s1")
            a = await dag_service.notebook_for_session("s1")
            b2 = await dag_service.notebook_for_session("s1")
            check("session cache: hit served from cache", a == b2 == nid and calls == ["s1"])
            dag_service.invalidate_notebook(nid)
            await dag_service.notebook_for_session("s1")
            check("invalidate_notebook drops the hit", calls == ["s1", "s1"])
        finally:
            del astorage._sync.notebook_id_for_session
            astorage._wrapped.pop("notebook_id_for_session", None)

        # Builder failure → dag_error, response still 200
        real_apply = dag_service.apply_runs
        dag_service.apply_runs = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            st, b = await asgi("POST", "/proxy/execute", body={"code": "x = 1", "cell_id": "c1"}, headers=hdr)
            d = _json(b) or {}
            await M.take(d)
            check("builder exception → 200 + dag_error 'capture_failed'",
                  st == 200 and d.get("dag_error") == "capture_failed" and "dag" not in d
                  and "dag_delta" not in d)
        finally:
            dag_service.apply_runs = real_apply
        check("every execute/set-variable payload applied to the GET mirror equals the stored doc",
              M.ok and M.n >= 8, f"ok={M.ok} n={M.n}")
    finally:
        dag_service.schedule_push = real_push


def _sse(*frames):
    return b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames)


async def _t_batch(ctx):
    print("execute-batch relay")
    nid = ctx["nid"]
    pushes = []
    real_push = dag_service.schedule_push
    dag_service.schedule_push = lambda sid, nid=None: pushes.append((sid, nid))
    M = Mirror(nid)
    await M.sync()
    try:
        before = (await astorage.notebook_dag_get(nid))["run_seq"]
        res = {"success": True, "variables_read": ["x"], "variables_created": ["y"],
               "variables_rebound": [], "variables_mutated": [], "sources_read": []}
        body = _sse({"type": "cell_result", "cell_id": "c1",
                     "result": {**res, "variables_read": [], "variables_created": ["x"]}},
                    {"type": "cell_result", "cell_id": "c2", "result": res},
                    {"type": "runtime", "runtime": _rt(9)},
                    {"type": "done", "cells_run": 2})
        VM.routes[("POST", "/execute-batch")] = lambda r: httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"})
        req = {"cells": [{"cell_id": "c1", "type": "code", "code": "x = 1"},
                         {"cell_id": "c2", "type": "code", "code": "y = x + 1"}]}
        st, b = await asgi("POST", "/proxy/execute-batch", body=req, headers={"X-Session-Id": "s1"})
        frames = [json.loads(f[6:]) for f in b.decode().split("\n\n") if f.startswith("data: ")]
        types = [f.get("type") for f in frames]
        check("batch frames: cell_result, cell_result, dag, done (runtime swallowed)",
              types == ["cell_result", "cell_result", "dag", "done"], str(types))
        frame = next((f for f in frames if f.get("type") == "dag"), {})
        check("batch dag frame carries dag_delta (not the full doc)",
              "dag_delta" in frame and "dag" not in frame, str(list(frame)))
        dag = await M.take(frame) or {}
        check("batch: the frame's delta applied to the GET mirror == the stored doc", M.ok)
        check("batch: one RMW recorded both runs (run_seq +2) and the runtime",
              dag.get("run_seq") == before + 2 and dag.get("runtime", {}).get("clock") == 9
              and dag.get("trigger") == "batch")
        check("batch: c2 reads x / writes y from the frame",
              (_node(dag, "c2") or {}).get("reads") == ["x"] and (_node(dag, "c2") or {}).get("writes") == ["y"])
        check("batch: cell_result frames forwarded unchanged",
              frames[0] == {"type": "cell_result", "cell_id": "c1",
                            "result": {**res, "variables_read": [], "variables_created": ["x"]}})
        check("batch schedules a push", pushes[-1:] == [("s1", nid)])

        # Broken stream before done → error frame; runs applied in the background.
        async def broken():
            yield _sse({"type": "cell_result", "cell_id": "c1",
                        "result": {**res, "variables_read": [], "variables_created": ["x"]}})
            raise httpx.ReadError("vm went away")
        VM.routes[("POST", "/execute-batch")] = lambda r: httpx.Response(
            200, content=broken(), headers={"content-type": "text/event-stream"})
        before = (await astorage.notebook_dag_get(nid))["run_seq"]
        st, b = await asgi("POST", "/proxy/execute-batch", body=req, headers={"X-Session-Id": "s1"})
        types = [json.loads(f[6:]).get("type") for f in b.decode().split("\n\n") if f.startswith("data: ")]
        check("broken batch: no dag frame, error frame surfaced", "dag" not in types and types[-1:] == ["error"],
              str(types))
        ok = False
        for _ in range(100):
            await asyncio.sleep(0.02)
            if (await astorage.notebook_dag_get(nid))["run_seq"] == before + 1:
                ok = True
                break
        check("broken batch: captured run applied in the background", ok)
    finally:
        dag_service.schedule_push = real_push


# ─── Chart persistence on the capture paths (image_s3_key) ───────────────────
import base64 as _b64  # noqa: E402
import hashlib as _hl  # noqa: E402
from proxy.notebook import cell_images  # noqa: E402
_PNG = b"\x89PNG\r\n\x1a\n" + b"chart-bytes-" * 20
_PNG2 = b"\x89PNG\r\n\x1a\n" + b"other-chart-" * 20
def _data_url(png):
    return "data:image/png;base64," + _b64.b64encode(png).decode()
def _key(nid, cid, png):
    return f"notebook-outputs/{nid}/{cid}/{_hl.sha256(png).hexdigest()}.png"
class _FakeS3:
    def __init__(self):
        self.puts = []
    def put_object(self, **kw):
        self.puts.append(kw)
        return {}
async def _t_images(ctx):
    print("chart persistence capture")
    # Own notebook + session so the later attach/reconcile tests see their state.
    st, b = await asgi("POST", "/notebooks", body={"name": "nb-img", "cells": CELLS,
                                                    "session_id": "s-img"})
    nid = (_json(b) or {}).get("id")
    fake = _FakeS3()
    real_s3, real_cache = cell_images._s3, cell_images._bucket_cache
    cell_images._s3 = lambda: fake
    cell_images._bucket_cache = "test-bucket"
    real_push = dag_service.schedule_push
    dag_service.schedule_push = lambda sid, nid=None: None
    hdr = {"X-Session-Id": "s-img"}
    VMM.sessions["s-img"] = {"vm_id": "vm-img", "endpoint": "ep-img"}
    try:
        def exec_resp(image):
            return lambda r: httpx.Response(200, json={
                "success": True, "output": "", "image": image, "variables_read": [],
                "variables_created": [], "variables_rebound": [], "variables_mutated": [],
                "sources_read": [], "runtime": _rt(50)})
        VM.routes[("POST", "/execute")] = exec_resp(_data_url(_PNG))
        before = (await astorage.notebook_dag_get(nid))["run_seq"]
        st, b = await asgi("POST", "/proxy/execute", body={"code": "plt.plot()", "cell_id": "c1"},
                           headers=hdr)
        d = _json(b) or {}
        check("execute with a chart → image_s3_key = notebook-outputs/<nid>/c1/<sha>.png",
              st == 200 and d.get("image_s3_key") == _key(nid, "c1", _PNG), str(d.get("image_s3_key")))
        check("execute with a chart keeps the base64 image for immediate display",
              d.get("image") == _data_url(_PNG))
        after = (await astorage.notebook_dag_get(nid))["run_seq"]
        check("execute with a chart: DAG capture unchanged (run recorded, dag payload present)",
              ("dag_delta" in d or "dag" in d) and after == before + 1, f"{list(d)} {before}->{after}")
        ok = await _wait(lambda: len(fake.puts) == 1)
        put = fake.puts[0] if fake.puts else {}
        check("execute with a chart: one background upload with the key + PNG bytes",
              ok and put.get("Key") == _key(nid, "c1", _PNG) and put.get("Body") == _PNG
              and put.get("Bucket") == "test-bucket")
        check("upload is private: no ACL, image/png, SSE-S3",
              "ACL" not in put and put.get("ContentType") == "image/png"
              and put.get("ServerSideEncryption") == "AES256")
        for label, image, body in (
                ("image ''", "", {"code": "1", "cell_id": "c1"}),
                ("origin agent", _data_url(_PNG), {"code": "1", "cell_id": "c1", "origin": "agent"}),
                ("no cell_id", _data_url(_PNG), {"code": "1"}),
                ("non-PNG data URL", "data:image/jpeg;base64,AAAA", {"code": "1", "cell_id": "c1"})):
            VM.routes[("POST", "/execute")] = exec_resp(image)
            n = len(fake.puts)
            st, b = await asgi("POST", "/proxy/execute", body=body, headers=hdr)
            d = _json(b) or {}
            await asyncio.sleep(0.05)
            check(f"execute {label} → no image_s3_key, no upload",
                  st == 200 and "image_s3_key" not in d and len(fake.puts) == n)
        # Multi-output: every image item of `outputs` gets its own key + upload.
        _PNG3 = b"\x89PNG\r\n\x1a\n" + b"third-chart-" * 20
        def _outs():
            return [{"t": "text", "text": "a\n"}, {"t": "image", "src": _data_url(_PNG)},
                    {"t": "table", "payload": "{}"}, {"t": "image", "src": _data_url(_PNG2)},
                    {"t": "image", "src": _data_url(_PNG3)}]
        def multi_resp(image, outs):
            return lambda r: httpx.Response(200, json={
                "success": True, "output": "", "image": image, "outputs": outs, "variables_read": [],
                "variables_created": [], "variables_rebound": [], "variables_mutated": [],
                "sources_read": [], "runtime": _rt(55)})
        VM.routes[("POST", "/execute")] = multi_resp(_data_url(_PNG3), _outs())
        n = len(fake.puts)
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x", "cell_id": "c1"}, headers=hdr)
        d = _json(b) or {}
        ks = [o.get("key") for o in d.get("outputs", []) if o.get("t") == "image"]
        check("execute with 3 images: each image item carries its own key",
              ks == [_key(nid, "c1", _PNG), _key(nid, "c1", _PNG2), _key(nid, "c1", _PNG3)], str(ks))
        check("execute with 3 images: image_s3_key is the LAST image's key; base64 src kept",
              d.get("image_s3_key") == _key(nid, "c1", _PNG3)
              and all(o.get("src") for o in d["outputs"] if o.get("t") == "image"))
        ok = await _wait(lambda: len(fake.puts) >= n + 3)
        check("execute with 3 images: 3 uploads of the 3 keys",
              ok and sorted(p_["Key"] for p_ in fake.puts[n:]) == sorted(ks), str([p_["Key"] for p_ in fake.puts[n:]]))
        VM.routes[("POST", "/execute")] = multi_resp("", _outs())
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x", "cell_id": "c1"}, headers=hdr)
        d = _json(b) or {}
        check("error-run shape (no legacy image, images in outputs): keys still added, last key as image_s3_key",
              d.get("image_s3_key") == _key(nid, "c1", _PNG3) and all(
                  o.get("key") for o in d["outputs"] if o.get("t") == "image"))
        VM.routes[("POST", "/execute")] = multi_resp(_data_url(_PNG3), _outs())
        n = len(fake.puts)
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x", "cell_id": "c1", "origin": "agent"},
                           headers=hdr)
        d = _json(b) or {}
        await asyncio.sleep(0.05)
        check("agent origin with images in outputs: no keys, no uploads",
              "image_s3_key" not in d and not any("key" in o for o in d.get("outputs", []))
              and len(fake.puts) == n)
        VM.routes[("POST", "/execute")] = multi_resp("", [{"t": "text", "text": "z"}])
        st, b = await asgi("POST", "/proxy/execute", body={"code": "x", "cell_id": "c1"}, headers=hdr)
        check("outputs without images: response unchanged, no key", "image_s3_key" not in (_json(b) or {}))
        # Scratch session (no notebook) → no key.
        VM.routes[("POST", "/execute")] = exec_resp(_data_url(_PNG))
        st, b = await asgi("POST", "/proxy/execute", body={"code": "1", "cell_id": "c1"},
                           headers={"X-Session-Id": "scratch-img"})
        check("execute on a scratch session → no image_s3_key", "image_s3_key" not in (_json(b) or {}))

        # Batch: image + non-image cell_result frames.
        res = {"success": True, "variables_read": [], "variables_created": [],
               "variables_rebound": [], "variables_mutated": [], "sources_read": []}
        f_img = {"type": "cell_result", "cell_id": "c1", "result": {**res, "image": _data_url(_PNG2)}}
        f_txt = {"type": "cell_result", "cell_id": "c2", "result": {**res, "image": ""}}
        raw_txt = f"data: {json.dumps(f_txt)}\n\n".encode()
        body = (_sse(f_img) + raw_txt
                + _sse({"type": "runtime", "runtime": _rt(60)}, {"type": "done", "cells_run": 2}))
        VM.routes[("POST", "/execute-batch")] = lambda r: httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"})
        req = {"cells": [{"cell_id": "c1", "type": "code", "code": "x = 1"},
                         {"cell_id": "c2", "type": "code", "code": "y = x + 1"}]}
        calls = {"n": 0}
        real_nfs = dag_service.notebook_for_session
        async def counting(sid):
            calls["n"] += 1
            return await real_nfs(sid)
        dag_service.notebook_for_session = counting
        try:
            before = (await astorage.notebook_dag_get(nid))["run_seq"]
            n = len(fake.puts)
            st, b = await asgi("POST", "/proxy/execute-batch", body=req, headers=hdr)
        finally:
            dag_service.notebook_for_session = real_nfs
        frames = [json.loads(f[6:]) for f in b.decode().split("\n\n") if f.startswith("data: ")]
        types = [f.get("type") for f in frames]
        check("batch with a chart: frames cell_result, cell_result, dag, done",
              types == ["cell_result", "cell_result", "dag", "done"], str(types))
        check("batch: the image frame gains image_s3_key (base64 kept)",
              frames[0]["result"].get("image_s3_key") == _key(nid, "c1", _PNG2)
              and frames[0]["result"].get("image") == _data_url(_PNG2))
        check("batch: the non-image frame is forwarded byte-identical", raw_txt in b)
        check("batch: DAG capture unchanged (both runs recorded in one RMW)",
              (await astorage.notebook_dag_get(nid))["run_seq"] == before + 2)
        check("batch: notebook id resolved once for images + once for the RMW", calls["n"] == 2,
              str(calls["n"]))
        ok = await _wait(lambda: len(fake.puts) == n + 1)
        check("batch: one upload for the image frame",
              ok and fake.puts[-1].get("Key") == _key(nid, "c1", _PNG2))
        f_multi = {"type": "cell_result", "cell_id": "c1", "result": {
            **res, "image": "", "outputs": [{"t": "image", "src": _data_url(_PNG)},
                                            {"t": "image", "src": _data_url(_PNG2)}]}}
        mbody = _sse(f_multi) + _sse({"type": "runtime", "runtime": _rt(61)}, {"type": "done", "cells_run": 1})
        VM.routes[("POST", "/execute-batch")] = lambda r: httpx.Response(
            200, content=mbody, headers={"content-type": "text/event-stream"})
        n = len(fake.puts)
        st, b = await asgi("POST", "/proxy/execute-batch", body={"cells": req["cells"][:1]}, headers=hdr)
        frames = [json.loads(f[6:]) for f in b.decode().split("\n\n") if f.startswith("data: ")]
        r0 = frames[0]["result"]
        check("batch: images inside outputs get keys (frame re-serialized), image_s3_key = last key",
              [o.get("key") for o in r0["outputs"]] == [_key(nid, "c1", _PNG), _key(nid, "c1", _PNG2)]
              and r0.get("image_s3_key") == _key(nid, "c1", _PNG2), str(r0))
        ok = await _wait(lambda: len(fake.puts) >= n + 2)
        check("batch: one upload per image item", ok)
        # Cached None: a scratch batch resolves the notebook once even with 2 images.
        bc = dag_service.BatchCapture("scratch-img", json.dumps(req).encode(), "vm-img")
        calls["n"] = 0
        dag_service.notebook_for_session = counting
        try:
            outs = []
            for f in (f_img, {**f_img, "cell_id": "c2"}):
                async for o in bc._handle(f"data: {json.dumps(f)}".encode()):
                    outs.append(o)
        finally:
            dag_service.notebook_for_session = real_nfs
        check("batch on a scratch session: nid None cached (1 lookup), frames unchanged, no key",
              calls["n"] == 1 and all(b"image_s3_key" not in o for o in outs), str(calls["n"]))
    finally:
        cell_images._s3, cell_images._bucket_cache = real_s3, real_cache
        dag_service.schedule_push = real_push
        VM.routes.pop(("POST", "/execute"), None)
        VM.routes.pop(("POST", "/execute-batch"), None)
        VMM.sessions.pop("s-img", None)
DENY = ["/proxy/dag", "/proxy/DAG", "/proxy/./dag", "/proxy/x/../dag", "/proxy/%2e%2e/dag",
        "/proxy/dag%3Fx", "/proxy/dag%23x", "/proxy/DAG%3F", "/proxy/dag%5Cx", "/proxy/dag%00",
        "/proxy/x/.%252e/dag", "/proxy/%2564ag"]


async def _t_denylist(ctx):
    print("forward denylist")
    VM.requests.clear()
    for raw in DENY:
        for method in ("GET", "PUT"):
            st, b = await asgi(method, raw, body={"schema": 1} if method == "PUT" else None,
                               headers={"X-Session-Id": "s1"})
            check(f"{method} {raw} → 403 proxy-internal",
                  st == 403 and _json(b) == {"error": "proxy-internal endpoint"}, f"{st} {b[:120]!r}")
    check("denylist: zero outbound VM requests", len(VM.requests) == 0, str([r.url for r in VM.requests]))
    st, b = await asgi("GET", "/proxy/variables", headers={"X-Session-Id": "s1"})
    check("/proxy/variables still forwards", st == 200 and VM.count("GET", "/variables") == 1)
    # Unit: httpx.URL parse failure fails closed; a normal path passes.
    check("_internal_target('variables') is False", microvm_routes._internal_target("variables") is False)
    check("_internal_target('execute-sql') is False", microvm_routes._internal_target("execute-sql") is False)
    check("_internal_target with a tab fails closed", microvm_routes._internal_target("da\tg") is True)


def _report(origin, runtime, *, source="dag.json", consistent=True):
    return {"origin": origin, "doc": None, "runtime": runtime, "restored_from": "s1",
            "runtime_source": source, "consistent": consistent}


async def _t_attach(ctx):
    print("push / attach / reconcile")
    nid = ctx["nid"]
    dag_service._reset_for_tests()
    dag_service.use_vm_manager(VMM)
    dag_service.ATTACH_BACKOFF = (0.01, 0.01, 0.01)
    dag_service.EMPTY_RETRY_DELAY = 0.01
    VM.requests.clear()
    doc = await astorage.notebook_dag_get(nid)
    c1 = _node(doc, "c1")
    seen = {"n": 0}
    rt = _rt(20, {"x": {"defined_by": "c1", "last_cell": "c1", "last_exec": 20}},
             {"c1": {"code_hash": c1["run_hash"], "clock": 20, "ok": True}})

    def get_dag(r):
        seen["n"] += 1
        if seen["n"] <= 2:
            return httpx.Response(503, json={"busy": True})
        return httpx.Response(200, json=_report("restored", rt))
    VM.routes[("GET", "/dag")] = get_dag
    VM.routes[("PUT", "/dag")] = lambda r: httpx.Response(200, json={"ok": True, "version": 1})
    VMM.sessions["s1"] = {"vm_id": "vm-r", "endpoint": "epr"}
    dag_service.attach_soon("s1", nid, expect_restore=True)
    ok = await _wait(lambda: dag_service._sync["s1"].synced_vm_id == "vm-r")
    check("attach: synced after 503, 503, 200", ok)
    await _wait(lambda: VM.count("PUT", "/dag") >= 1)
    seq = [(r.method, r.url.path) for r in VM.requests]
    check("attach: GET ×3 then PUT (no PUT before the successful attach)",
          seq == [("GET", "/dag")] * 3 + [("PUT", "/dag")], str(seq))
    g = VM.requests[0]
    check("VM calls carry X-aws-proxy-auth + X-Dag-Writer: proxy",
          g.headers.get("x-aws-proxy-auth") == "vm-token-vm-r" and g.headers.get("x-dag-writer") == "proxy")
    put_body = json.loads(VM.requests[-1].content)
    check("PUT body is strip_for_vm (no runtime / pending)",
          "runtime" not in put_body and "pending" not in put_body and put_body.get("schema") == 1)
    after = await astorage.notebook_dag_get(nid)
    check("reconcile: trigger 'reconcile', runtime from the report",
          after.get("trigger") == "reconcile" and after["runtime"]["clock"] == 20
          and after["runtime"]["vm_id"] == "vm-r")
    check("reconcile: c1 (matching ledger entry) is in the checkpoint",
          (_node(after, "c1") or {}).get("not_in_checkpoint") is False)
    check("reconcile: c2 (ran, no ledger entry, provenance lacks y) → not_in_checkpoint",
          (_node(after, "c2") or {}).get("not_in_checkpoint") is True)
    check("await_attach resolved", dag_service._sync["s1"].attach_future.done())

    # attach_soon again for the same VM: idempotent (no GET, no PUT)
    n_before = len(VM.requests)
    dag_service.attach_soon("s1", nid)
    await dag_service.await_attach("s1", timeout=2)
    await asyncio.sleep(0.05)
    check("attach_soon on an already-synced VM sends nothing", len(VM.requests) == n_before)
    dag_service.schedule_push("s1", nid)
    await asyncio.sleep(0.05)
    check("schedule_push on a synced VM with an unchanged doc → no VM call (VM copy current)",
          VM.count("PUT", "/dag") == 1 and VM.count("PATCH", "/dag") == 0 and VM.count("GET", "/dag") == 3)

    # Three failures → give up: synced_vm_id set, warning, pushes resume.
    nid2 = ctx["nid2"]
    VMM.sessions["s2"] = {"vm_id": "vm-f", "endpoint": "epf"}
    VM.requests.clear()
    LOGS.records.clear()
    VM.routes[("GET", "/dag")] = lambda r: httpx.Response(503, json={"busy": True})
    dag_service.schedule_push("s2", nid2)
    ok = await _wait(lambda: "s2" in dag_service._sync and dag_service._sync["s2"].synced_vm_id == "vm-f")
    await _wait(lambda: VM.count("PUT", "/dag") >= 1)
    check("3 failed attaches → synced_vm_id set", ok)
    check("3 failed attaches → exactly 3 GETs, then the push PUTs",
          [(r.method, r.url.path) for r in VM.requests] == [("GET", "/dag")] * 3 + [("PUT", "/dag")],
          str([(r.method, r.url.path) for r in VM.requests]))
    check("give-up logged at warning", any(r.levelno == logging.WARNING and "gave up" in r.getMessage()
                                           for r in LOGS.records))
    dag_service._sync["s2"].vm_base = None   # no acknowledged base → full PUT
    dag_service.schedule_push("s2", nid2)
    await _wait(lambda: VM.count("PUT", "/dag") == 2)
    check("next push PUTs without re-attaching", VM.count("PUT", "/dag") == 2 and VM.count("GET", "/dag") == 3)

    # Cold attach: origin empty, clock 0, new VM → run writers marked not_in_checkpoint.
    VMM.sessions["s1"] = {"vm_id": "vm-cold", "endpoint": "epc"}
    VM.routes[("GET", "/dag")] = lambda r: httpx.Response(200, json=_report("empty", _rt(0), source=None,
                                                                             consistent=None))
    dag_service.schedule_push("s1", nid)
    await _wait(lambda: dag_service._sync["s1"].synced_vm_id == "vm-cold")
    after = await astorage.notebook_dag_get(nid)
    check("cold attach → c1 not_in_checkpoint, trigger attach",
          after.get("trigger") == "attach" and (_node(after, "c1") or {}).get("not_in_checkpoint") is True)

    # 404 → no_dag_support, pushes skipped for that VM.
    VMM.sessions["s1"] = {"vm_id": "vm-old", "endpoint": "epo"}
    VM.requests.clear()
    VM.routes[("GET", "/dag")] = lambda r: httpx.Response(404, json={"detail": "Not Found"})
    dag_service.schedule_push("s1", nid)
    await _wait(lambda: "vm-old" in dag_service._sync["s1"].no_dag_support)
    dag_service.schedule_push("s1", nid)
    await asyncio.sleep(0.05)
    check("404 → no_dag_support, no PUT", VM.count("PUT", "/dag") == 0 and VM.count("GET", "/dag") == 1)

    # flush before /checkpoint-save: PUT /dag precedes POST /checkpoint-save.
    VMM.sessions["s1"] = {"vm_id": "vm-1", "endpoint": "ep1"}
    VM.routes[("GET", "/dag")] = lambda r: httpx.Response(200, json=_report("pushed", _rt(30)))
    VM.routes[("POST", "/checkpoint-save")] = lambda r: httpx.Response(200, json={"saved": True})
    VM.requests.clear()
    res = await microvm_routes._checkpoint_session_vm(VMM, "vm-1", "ep1", "s1")
    seq = [(r.method, r.url.path) for r in VM.requests]
    check("checkpoint: flush attaches + PUTs /dag before POST /checkpoint-save",
          seq == [("GET", "/dag"), ("PUT", "/dag"), ("POST", "/checkpoint-save")] and res.get("success") is True,
          str(seq))
    VM.requests.clear()
    res = await microvm_routes._checkpoint_session_vm(VMM, "vm-1", "ep1", "s1")
    seq = [(r.method, r.url.path) for r in VM.requests]
    check("second checkpoint: flush sends the marker PATCH (VM confirms its base), no PUT",
          seq == [("PATCH", "/dag"), ("POST", "/checkpoint-save")], str(seq))
    VM.requests.clear()
    VM.routes[("PATCH", "/dag")] = lambda r: httpx.Response(500, json={"error": "x"})
    VM.routes[("PUT", "/dag")] = lambda r: httpx.Response(500, json={"error": "x"})
    res = await microvm_routes._checkpoint_session_vm(VMM, "vm-1", "ep1", "s1")
    check("checkpoint proceeds when the flush PATCH fails",
          res.get("success") is True and VM.count("POST", "/checkpoint-save") == 1)
    VM.requests.clear()
    res = await microvm_routes._checkpoint_session_vm(VMM, "vm-1", "ep1", "s1")
    check("after a failed push the next flush is a full PUT; checkpoint still proceeds when it fails",
          [(r.method, r.url.path) for r in VM.requests] == [("PUT", "/dag"), ("POST", "/checkpoint-save")]
          and res.get("success") is True)
    VM.routes.pop(("PATCH", "/dag"), None)
    VM.routes[("PUT", "/dag")] = lambda r: httpx.Response(200, json={"ok": True, "version": 1})

    # forget_vm (terminate / mark_dead): per-VM and idle per-session state pruned.
    check("pre-forget: per-VM warn/no-support entries exist",
          "vm-1" in dag_service._last_put_warn and "vm-old" in dag_service._no_support_logged)
    dag_service.forget_vm("vm-1", "s2")   # s2 is synced to vm-f → kept
    check("forget_vm keeps a session synced to another VM", "s2" in dag_service._sync)
    check("forget_vm drops the VM's put-warn entry", "vm-1" not in dag_service._last_put_warn)
    dag_service.forget_vm("vm-1", "s1")   # s1 idle on vm-1 → dropped
    check("forget_vm drops an idle session synced to that VM", "s1" not in dag_service._sync)
    dag_service.forget_vm("vm-old")
    check("forget_vm drops the VM's no-support log entry", "vm-old" not in dag_service._no_support_logged)
    st2 = dag_service._sync["s2"]
    st2.attach_wanted = True
    dag_service.forget_vm("vm-f", "s2")
    check("forget_vm keeps a session with a pending attach", dag_service._sync.get("s2") is st2)
    st2.attach_wanted = False
    dag_service.forget_vm("vm-f", "s2")
    check("forget_vm drops it once idle", "s2" not in dag_service._sync)


async def _t_launch_reuse(ctx):
    print("/launch reuse path")
    nid = ctx["nid"]
    calls = []
    real = dag_service.attach_soon
    dag_service.attach_soon = lambda sid, nid=None, expect_restore=False, **kw: calls.append(
        (sid, nid, expect_restore))
    try:
        VMM.sessions["s1"] = {"vm_id": "vm-live", "endpoint": "epl"}
        VMM.states["vm-live"] = "RUNNING"
        await dag_service.notebook_for_session("s1")
        st, b = await asgi("POST", "/launch", body={"notebookId": nid, "sessionId": "s1"})
        d = _json(b) or {}
        check("/launch reuse returns the live VM", st == 200 and d.get("microvmId") == "vm-live", f"{st} {b[:200]!r}")
        check("/launch reuse calls attach_soon(sid, nid, expect_restore=False)", calls == [("s1", nid, False)],
              str(calls))
        check("_persist_notebook_link invalidated the session cache", "s1" not in dag_service._session_cache)
    finally:
        dag_service.attach_soon = real


async def _t_safe_wrapper(ctx):
    print("safe() wrapper")
    f = dag_service.safe(lambda prev: 1 / 0, nb=1, trigger="t")
    check("safe(build)(prev) swallows → None, failed flag", f({}) is None and f.failed is True)
    g = dag_service.safe(lambda prev, cells: (prev, cells), nb=1, trigger="t")
    check("safe(build)(prev, cells) passes both args", g(1, 2) == (1, 2) and g.failed is False)
    check("_cell_id coercion", (dag_service._cell_id(7), dag_service._cell_id(True), dag_service._cell_id(""),
                                dag_service._cell_id("a" * 256) is not None, dag_service._cell_id("a" * 257),
                                dag_service._cell_id(1.5)) == ("7", None, None, True, None, None))


class StatefulVM:
    """The VM's /dag behind the mock transport, backed by the REAL DagStore:
    PUT stores, PATCH applies (409 on base/hash mismatch), GET reports."""

    def __init__(self, store_cls):
        self.store = store_cls()
        self.patch_status = None   # force a status for PATCH (e.g. 405 / 409)

    def put(self, r):
        doc = json.loads(r.content)
        for k in ("runtime", "pending", "checkpoint"):
            doc.pop(k, None)
        self.store.put(doc)
        return httpx.Response(200, json={"ok": True, "version": doc.get("version")})

    def patch(self, r):
        if self.patch_status is not None:
            return httpx.Response(self.patch_status, json={"error": "forced"})
        delta = json.loads(r.content)
        delta.pop("runtime", None)
        delta.pop("pending", None)
        status, body = self.store.apply_delta(delta)
        return httpx.Response(status, json=body)

    def get(self, r):
        return httpx.Response(200, json=_report("pushed", _rt(0)))

    def install(self):
        VM.routes[("PUT", "/dag")] = self.put
        VM.routes[("PATCH", "/dag")] = self.patch
        VM.routes[("GET", "/dag")] = self.get


CHAIN = [{"id": f"k{i}", "type": "code", "code": ("a0 = 1" if i == 0 else f"a{i} = a{i - 1} + 1")}
         for i in range(12)]


def _chain_exec(clock_box):
    def handler(r):
        body = json.loads(r.content)
        cid = body.get("cell_id")
        i = int(str(cid)[1:]) if isinstance(cid, str) and cid[1:].isdigit() else 0
        clock_box[0] += 1
        return httpx.Response(200, json={
            "success": True, "output": "", "variables_read": [] if i == 0 else [f"a{i - 1}"],
            "variables_created": [f"a{i}"], "variables_rebound": [], "variables_mutated": [],
            "sources_read": [], "runtime": _rt(clock_box[0])})
    return handler


async def _t_delta(ctx):
    print("change check + delta transport")
    from app.notebook.dag_store import DagStore
    dag_service._reset_for_tests()
    dag_service.use_vm_manager(VMM)
    vm = StatefulVM(DagStore)
    vm.install()
    clock = [0]
    VM.routes[("POST", "/execute")] = _chain_exec(clock)
    hdr = {"X-Session-Id": "s3"}
    VMM.sessions["s3"] = {"vm_id": "vm-3", "endpoint": "ep3"}
    VM.requests.clear()

    # First capture on a notebook WITHOUT a document → the full doc.
    nb = await astorage.notebook_create("nb-delta", cells=CHAIN, session_id="s3")
    nid = nb["id"]
    st, b = await asgi("POST", "/proxy/execute", body={"code": CHAIN[0]["code"], "cell_id": "k0"}, headers=hdr)
    d = _json(b) or {}
    check("first capture on a notebook without a document → full 'dag' (no 'dag_delta')",
          st == 200 and isinstance(d.get("dag"), dict) and "dag_delta" not in d, str(list(d)))
    await _wait(lambda: dag_service._sync.get("s3") is not None and dag_service._sync["s3"].vm_base is not None)
    check("first push after attach is a full PUT (no PATCH)",
          VM.count("PUT", "/dag") == 1 and VM.count("PATCH", "/dag") == 0)

    M = Mirror(nid)
    await M.sync()
    # Open path: build the nodes (the doc so far only had pending runs).
    await asgi("GET", f"/notebooks/{nid}")
    await M.sync()
    for c in CHAIN:
        st, b = await asgi("POST", "/proxy/execute", body={"code": c["code"], "cell_id": c["id"]}, headers=hdr)
        await M.take(_json(b) or {})
    check("run the whole chain: each response applied to the GET mirror == stored doc", M.ok and M.n == len(CHAIN))
    await _wait(lambda: rev(dag_service._sync["s3"].vm_base or {}) == rev(dag_service.strip_for_vm(M.doc)))
    vm_doc = vm.store.snapshot()
    check("VM copy (PUT + PATCHes via the real DagStore) == strip_for_vm(stored doc)",
          vm_doc == dag_service.strip_for_vm(await astorage.notebook_dag_get(nid)))

    # Leaf re-run twice: the second is unchanged (same version) with clocks/runtime.
    leaf = CHAIN[-1]
    for _ in range(2):
        st, b = await asgi("POST", "/proxy/execute", body={"code": leaf["code"], "cell_id": leaf["id"]},
                           headers=hdr)
        d = _json(b) or {}
        await M.take(d)
    dl = d.get("dag_delta") or {}
    check("second leaf re-run → dag_delta.unchanged, same version, clocks + runtime only",
          dl.get("unchanged") is True and dl.get("version") == dl.get("base_version")
          and "clocks" in dl and "runtime" in dl
          and not ({"nodes_upsert", "edges_add", "edges_remove", "issues"} & set(dl)), str(sorted(dl)))
    check("leaf delta is small (< 1 KB)", len(json.dumps(dl)) < 1024, str(len(json.dumps(dl))))

    # No-op: an identical set-variable twice → the unchanged marker, row untouched.
    VM.routes[("POST", "/set-variable")] = lambda r: httpx.Response(200, json={
        "success": True, "name": "k", "runtime": _rt(500, {"k": {"defined_by": None}})})
    await _wait(lambda: not (dag_service._sync["s3"].task and not dag_service._sync["s3"].task.done()))
    n_vm = VM.count("PUT", "/dag") + VM.count("PATCH", "/dag")
    st, b = await asgi("POST", "/proxy/set-variable", body={"name": "k", "value": 1}, headers=hdr)
    d1 = _json(b) or {}
    await M.take(d1)
    dd1 = d1.get("dag_delta") or {}
    check("set-variable → runtime-only dag_delta (runtime op, no graph ops, version kept)",
          dd1.get("unchanged") is True and "runtime" in dd1 and dd1["version"] == dd1["base_version"]
          and dd1["runtime_version"] == dd1["base_runtime_version"] + 1, str(sorted(dd1)))

    def upd_at():
        with _sq.SqliteStorage()._db() as conn:
            return conn.execute("SELECT updated_at FROM notebook_dag WHERE notebook_id = ?",
                                (nid,)).fetchone()[0]
    u_before = upd_at()
    await asyncio.sleep(0.01)
    st, b = await asgi("POST", "/proxy/set-variable", body={"name": "k", "value": 1}, headers=hdr)
    d2 = _json(b) or {}
    await M.take(d2)
    dd2 = d2.get("dag_delta") or {}
    marker_keys = {"delta", "schema", "base_version", "base_runtime_version", "version", "runtime_version",
                   "graph_hash", "runtime_hash", "unchanged", "notebook_id", "built_at", "trigger"}
    check("identical set-variable again → the unchanged marker (base == target, stamps only)",
          set(dd2) == marker_keys and dd2["unchanged"] is True
          and (dd2["version"], dd2["runtime_version"]) == (dd2["base_version"], dd2["base_runtime_version"]),
          str(sorted(dd2)))
    check("no-op → row not rewritten (updated_at unchanged)", upd_at() == u_before)
    await asyncio.sleep(0.05)
    check("runtime-only / no-op captures make zero VM PUT/PATCH calls",
          VM.count("PUT", "/dag") + VM.count("PATCH", "/dag") == n_vm)

    # A big change (upstream re-run makes every downstream node stale) → full doc.
    st, b = await asgi("POST", "/proxy/execute", body={"code": CHAIN[0]["code"], "cell_id": "k0"}, headers=hdr)
    d = _json(b) or {}
    check("upstream re-run staling the whole chain → delta above the ratio → full 'dag'",
          isinstance(d.get("dag"), dict) and "dag_delta" not in d, str(list(d)))
    await M.take(d)
    check("mirror still equals the stored doc after the full-doc response", M.ok)

    # Coalescing: hold the session lock, capture 3 runs, release → ONE cumulative PATCH.
    st3 = dag_service._sync["s3"]
    await _wait(lambda: not (st3.task and not st3.task.done()))
    acked = rev(st3.vm_base)
    VM.requests.clear()
    async with st3.lock:
        for c in CHAIN[3:6]:
            await asgi("POST", "/proxy/execute", body={"code": c["code"], "cell_id": c["id"]}, headers=hdr)
        await asyncio.sleep(0.05)
        check("pushes wait while the session lock is held", VM.count("PATCH", "/dag") == 0)
    await _wait(lambda: VM.count("PATCH", "/dag") >= 1)
    await _wait(lambda: not (st3.task and not st3.task.done()))
    patches = [json.loads(r.content) for r in VM.requests if r.method == "PATCH"]
    cur = rev(await astorage.notebook_dag_get(nid))
    check("coalesced: exactly one PATCH, base == the VM-acked rev, target == the current DB rev",
          len(patches) == 1 and (patches[0]["base_version"], patches[0]["base_runtime_version"]) == acked
          and (patches[0]["version"], patches[0]["runtime_version"]) == cur
          and VM.count("PUT", "/dag") == 0, f"{len(patches)} {acked} {cur}")
    check("coalesced PATCH carries no runtime/pending (VM-owned)",
          patches and not ({"runtime", "pending"} & set(patches[0])))

    # PATCH 409 → exactly one full PUT follows.
    VM.requests.clear()
    vm.patch_status = 409
    await asgi("POST", "/proxy/execute", body={"code": CHAIN[2]["code"], "cell_id": "k2"}, headers=hdr)
    await _wait(lambda: VM.count("PUT", "/dag") >= 1)
    await _wait(lambda: not (st3.task and not st3.task.done()))
    check("PATCH 409 → exactly one full PUT follows",
          [(r.method) for r in VM.requests if r.url.path == "/dag"] == ["PATCH", "PUT"],
          str([(r.method, r.url.path) for r in VM.requests]))

    # PATCH 405 (older image) → no_patch; later pushes are PUTs.
    VM.requests.clear()
    vm.patch_status = 405
    await asgi("POST", "/proxy/execute", body={"code": CHAIN[2]["code"], "cell_id": "k2"}, headers=hdr)
    await _wait(lambda: VM.count("PUT", "/dag") >= 1)
    await _wait(lambda: not (st3.task and not st3.task.done()))
    await asgi("POST", "/proxy/execute", body={"code": CHAIN[4]["code"], "cell_id": "k4"}, headers=hdr)
    await _wait(lambda: VM.count("PUT", "/dag") >= 2)
    await _wait(lambda: not (st3.task and not st3.task.done()))
    check("PATCH 405 → vm in no_patch, then PUT; next push is a PUT without PATCH",
          "vm-3" in st3.no_patch
          and [r.method for r in VM.requests if r.url.path == "/dag"] == ["PATCH", "PUT", "PUT"],
          str([(r.method, r.url.path) for r in VM.requests]))
    st3.no_patch.clear()
    vm.patch_status = None

    # flush after a code-only save → a PATCH carrying the graph ops.
    VM.requests.clear()
    edited = [dict(c) for c in CHAIN]
    edited[7]["code"] = "a7 = a6 + a0"
    st, b = await asgi("PUT", f"/notebooks/{nid}", body={"cells": edited})
    await asyncio.sleep(0.05)
    check("a save does not push to the VM", VM.count("PUT", "/dag") + VM.count("PATCH", "/dag") == 0)
    ok = await dag_service.flush("s3", nid)
    patches = [json.loads(r.content) for r in VM.requests if r.method == "PATCH"]
    check("flush after a code-only save → one PATCH with the graph ops, VM copy == stored doc",
          ok is True and len(patches) == 1 and "nodes_upsert" in patches[0]
          and vm.store.snapshot() == dag_service.strip_for_vm(await astorage.notebook_dag_get(nid)),
          str([(r.method, r.url.path) for r in VM.requests]))

    # After an attach the push is a full PUT.
    VM.requests.clear()
    st3.synced_vm_id = None
    dag_service.schedule_push("s3", nid)
    await _wait(lambda: VM.count("PUT", "/dag") >= 1)
    check("after an attach the push is a full PUT",
          [r.method for r in VM.requests if r.url.path == "/dag"] == ["GET", "PUT"],
          str([(r.method, r.url.path) for r in VM.requests]))

    # PATCH is not routed by the browser-facing forward.
    VM.requests.clear()
    st, b = await asgi("PATCH", "/proxy/dag", body={"delta": 1}, headers=hdr)
    check("PATCH /proxy/dag → 403/405 with zero VM calls", st in (403, 405) and len(VM.requests) == 0, str(st))
    st, b = await asgi("PATCH", "/proxy/dag", body={"delta": 1}, headers=hdr, auth=False)
    check("unauthenticated PATCH /proxy/dag → 401/405 (method not routed), zero VM calls",
          st in (401, 405) and len(VM.requests) == 0, str(st))
    for k in ("PUT", "PATCH", "GET"):
        VM.routes.pop((k, "/dag"), None)


async def main_async():
    ctx = {}
    await _t_safe_wrapper(ctx)
    await _t_notebook_routes(ctx)
    await _t_capture(ctx)
    await _t_batch(ctx)
    await _t_images(ctx)
    await _t_denylist(ctx)
    await _t_attach(ctx)
    await _t_launch_reuse(ctx)
    await _t_delta(ctx)
    check("_PROXY_AUTH_EXEMPT_PATHS == {'/health'}", proxy_auth._PROXY_AUTH_EXEMPT_PATHS == {"/health"})
    c = _client_holder.pop("c", None)
    if c is not None:
        await c.aclose()


def _use_own_db():
    """(Re)point the storage singleton at this suite's temp DB — a shared pytest
    session may have redirected sqlite_db to another suite's temp dir since import."""
    _sq.DB_DIR = _TMP
    _sq.DB_PATH = os.path.join(_TMP, "dag_capture.db")
    _sq._local = threading.local()
    astorage._sync.initialize()


def test_dag_capture():
    _checks.clear()
    _use_own_db()
    http_clients.vm_client = _fake_vm_client
    APP.state.vm_manager = VMM
    dag_service._reset_for_tests()
    asyncio.run(main_async())
    failed = [n for n, ok in _checks if not ok]
    assert not failed, failed


if __name__ == "__main__":
    try:
        test_dag_capture()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")
