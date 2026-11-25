#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for the deleted-cell flag (F1), proxy side:
proxy/notebook/var_orphans.py + dag_service.sync_vm_copy + the hook in
proxy/platform/routes/microvm.py proxy_request.

Driven through the REAL proxy app over raw ASGI calls (harness copied from
tests/dag/test_dag_capture.py): temp SQLite set BEFORE proxy.server is imported, the
VM is an httpx.MockTransport, app.state.vm_manager is a small fake. No network.

Cases (DAG over the notebook's CURRENT cells):
  (a) case 2: deleted producer, nobody reads it   -> "deleted cell", in clean-up,
      flagged with no cell run (the VM DAG copy is synced before the read);
  (b) case 3: deleted producer, live readers      -> used_by, excluded from
      clean-up on every payload shape, server-side delete guard;
  (c) case 1: another live cell defines it        -> no flag;
  (d) re-definition clears the flag with no run; a repeated read sends no /dag;
  (e) unchanged behaviour: agent/unknown/normal entries, scratch session,
      non-200 / non-JSON replies;
  (f) pure-function checks.

Run:  .venv/bin/python tests/proxy/test_deleted_cell_flag_proxy.py   (exit 0 = pass)
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import proxy.storage.sqlite_db as _sq  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="deleted_cell_flag_")
_DB = "deleted_cell_flag.db"
_sq.DB_DIR = _TMP
_sq.DB_PATH = os.path.join(_TMP, _DB)
_sq._local = threading.local()
_sq._HELD_SESSION_LOCKS.clear()

_TOKEN = "deleted-cell-flag-test-token"
os.environ["PROXY_AUTH_TOKEN"] = _TOKEN
os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
os.environ.pop("POOL_ENABLED", None)

import httpx  # noqa: E402

import proxy.server as proxy_server  # noqa: E402
from proxy.notebook import dag_service, var_orphans  # noqa: E402
from proxy.platform import http_clients  # noqa: E402
from proxy.storage import astorage  # noqa: E402

APP = proxy_server.app
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ─── Fakes ─────────────────────────────────────────────────────────────────────

class FakeVmManager:
    def __init__(self):
        self.sessions = {}
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
        return "RUNNING"


class FakeVM:
    def __init__(self):
        self.requests = []
        self.routes = {}

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        fn = self.routes.get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(200, json={"ok": True})
        return fn(request)

    def paths(self):
        return [(r.method, r.url.path) for r in self.requests]

    def dag_calls(self):
        return [r for r in self.requests if r.url.path == "/dag" and r.method in ("PUT", "PATCH")]


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


# ─── Fixture ───────────────────────────────────────────────────────────────────

C1 = {"id": "c1", "type": "code", "code": "x = 1"}
C2 = {"id": "c2", "type": "code", "code": "y = x + 1"}
C3 = {"id": "c3", "type": "code", "code": "z = 5"}
C4 = {"id": "c4", "type": "code", "code": "w = 2"}
HDR = {"X-Session-Id": "sf1"}

# The VM's runtime provenance (what each cell run recorded).
RT = {"clock": 0, "vars": {}, "runs": {}}


def _entry(name, orphan=False, reason=None, by=None):
    return {"name": name, "type": "int", "kind": "scalar", "defined_by": None if orphan else by,
            "orphan": orphan, "orphan_reason": reason}


# What the VM currently says about each variable (set per test).
VM_VARS = {}


def _vm_routes():
    VM.routes[("GET", "/dag")] = lambda r: httpx.Response(200, json={
        "origin": "live", "runtime": copy.deepcopy(RT)})
    VM.routes[("GET", "/variables")] = lambda r: httpx.Response(200, json={
        "variables": copy.deepcopy(VM_VARS), "count": len(VM_VARS)})
    VM.routes[("GET", "/variables/redacted")] = lambda r: httpx.Response(200, json={
        "variables": copy.deepcopy(VM_VARS), "count": len(VM_VARS)})
    VM.routes[("GET", "/variables/schema")] = lambda r: httpx.Response(200, json={
        "variables": [copy.deepcopy(e) for e in VM_VARS.values()], "count": len(VM_VARS),
        "truncated": False})

    def _inspect(r):
        name = (_json(r.content) or {}).get("name")
        e = VM_VARS.get(name)
        return httpx.Response(200, json=copy.deepcopy(e)) if e else httpx.Response(404, json={})
    VM.routes[("POST", "/variables/inspect")] = _inspect
    VM.routes[("GET", "/variables/untracked")] = lambda r: httpx.Response(200, json={
        "visible": [{"name": n, "reason": e["orphan_reason"]} for n, e in VM_VARS.items()
                    if e.get("orphan") and e.get("orphan_reason") != "internal"],
        "internal": [n for n, e in VM_VARS.items() if e.get("orphan_reason") == "internal"]})

    def _delete(r):
        names = (_json(r.content) or {}).get("names") or []
        return httpx.Response(200, json={"deleted": list(names), "skipped": [],
                                         "runtime": copy.deepcopy(RT)})
    VM.routes[("POST", "/variables/delete")] = _delete


async def _run(cid, code, created):
    """One cell run captured into the DB doc (runtime.vars defined_by = cid)."""
    RT["clock"] += 1
    for v in created:
        RT["vars"][v] = {"defined_by": cid, "last_cell": cid, "last_exec": RT["clock"]}
    VM.routes[("POST", "/execute")] = lambda r: httpx.Response(200, json={
        "success": True, "output": "", "variables_read": ["x"] if cid == "c2" else [],
        "variables_created": list(created), "variables_rebound": [], "variables_mutated": [],
        "sources_read": [], "runtime": copy.deepcopy(RT)})
    st, b = await asgi("POST", "/proxy/execute", body={"code": code, "cell_id": cid}, headers=HDR)
    return st


async def _put(nid, cells):
    st, b = await asgi("PUT", f"/notebooks/{nid}", body={"cells": cells})
    return st


async def _get(path, body=None):
    method = "POST" if body is not None else "GET"
    st, b = await asgi(method, f"/proxy/{path}", body=body, headers=HDR)
    return st, _json(b)


# ─── Tests ─────────────────────────────────────────────────────────────────────

async def _setup(ctx):
    print("setup")
    st, b = await asgi("POST", "/notebooks", body={"name": "flag", "cells": [C1, C2, C3, C4],
                                                    "session_id": "sf1"})
    nid = (_json(b) or {}).get("id")
    check("POST /notebooks → 200", st == 200 and nid is not None, f"{st} {b[:200]!r}")
    ctx["nid"] = nid
    VMM.sessions["sf1"] = {"vm_id": "vm-f1", "endpoint": "epf1"}
    _vm_routes()
    for c, created in ((C1, ["x"]), (C2, ["y"]), (C3, ["z"]), (C4, ["w"])):
        check(f"execute {c['id']} → 200", await _run(c["id"], c["code"], created) == 200)
    doc = await astorage.notebook_dag_get(nid)
    check("DB runtime records defined_by per cell",
          all((doc.get("runtime", {}).get("vars", {}).get(v) or {}).get("defined_by") == c
              for v, c in (("x", "c1"), ("y", "c2"), ("z", "c3"), ("w", "c4"))))
    VM_VARS.clear()
    VM_VARS.update({"x": _entry("x", by="c1"), "y": _entry("y", by="c2"),
                    "z": _entry("z", by="c3"), "w": _entry("w", by="c4")})


async def _t_case2(ctx):
    print("(a) case 2: deleted producer, unread")
    nid = ctx["nid"]
    check("PUT without c3 → 200", await _put(nid, [C1, C2, C4]) == 200)
    VM_VARS["z"] = _entry("z", orphan=True, reason="deleted cell")
    VM.requests.clear()
    st, d = await _get("variables/untracked")
    paths = VM.paths()
    i_dag = next((i for i, (m, p) in enumerate(paths) if p == "/dag" and m in ("PUT", "PATCH")), None)
    i_unt = next((i for i, (m, p) in enumerate(paths) if p == "/variables/untracked"), None)
    check("a /dag PUT/PATCH reached the VM BEFORE /variables/untracked (no cell run)",
          i_dag is not None and i_unt is not None and i_dag < i_unt, str(paths))
    calls = VM.dag_calls()
    pushed = _json(calls[0].content) if calls else None
    if calls and calls[0].method == "PUT":
        ok = isinstance(pushed, dict) and not any(n.get("cell_id") == "c3" for n in pushed.get("nodes", []))
    else:   # PATCH delta: it must remove c3
        ok = bool(calls) and "c3" in json.dumps(pushed)
    check("the pushed doc drops c3", ok, json.dumps(pushed)[:300])
    check("no /execute was needed", not any(p == "/execute" for _, p in paths))
    check("untracked: z in visible, in_use == []",
          st == 200 and {"name": "z", "reason": "deleted cell"} in d.get("visible", [])
          and d.get("in_use") == [], json.dumps(d))
    st, d = await _get("variables")
    z = (d or {}).get("variables", {}).get("z", {})
    check("/variables: z orphan, 'deleted cell', no used_by",
          z.get("orphan") is True and z.get("orphan_reason") == "deleted cell" and "used_by" not in z,
          json.dumps(z))


async def _t_case3(ctx):
    print("(b) case 3: deleted producer, live readers")
    nid = ctx["nid"]
    check("PUT without c1 → 200", await _put(nid, [C2, C4]) == 200)
    VM_VARS["x"] = _entry("x", orphan=True, reason="deleted cell")
    st, d = await _get("variables")
    x = (d or {}).get("variables", {}).get("x", {})
    check("/variables: x orphan with used_by == ['c2']",
          x.get("orphan") is True and x.get("orphan_reason") == "deleted cell" and x.get("used_by") == ["c2"],
          json.dumps(x))
    y = (d or {}).get("variables", {}).get("y", {})
    check("/variables: y (live producer) untouched", y == _entry("y", by="c2"), json.dumps(y))
    st, d = await _get("variables/untracked")
    check("untracked: x only in in_use, not in visible",
          d.get("in_use") == [{"name": "x", "used_by": ["c2"]}]
          and not any(v.get("name") == "x" for v in d.get("visible", [])), json.dumps(d))
    check("untracked: z still visible", any(v.get("name") == "z" for v in d.get("visible", [])))
    st, d = await _get("variables/schema")
    xs = next((e for e in (d or {}).get("variables", []) if e.get("name") == "x"), {})
    check("/variables/schema (list shape): x used_by ['c2']", xs.get("used_by") == ["c2"], json.dumps(xs))
    st, d = await _get("variables/redacted")
    check("/variables/redacted: x used_by ['c2']",
          (d or {}).get("variables", {}).get("x", {}).get("used_by") == ["c2"])
    st, d = await _get("variables/inspect", {"name": "x"})
    check("/variables/inspect: x used_by ['c2']", (d or {}).get("used_by") == ["c2"], json.dumps(d))
    VM.requests.clear()
    st, d = await _get("variables/delete", {"names": ["x", "z"]})
    sent = [_json(r.content) for r in VM.requests if r.url.path == "/variables/delete"]
    check("delete: the VM received names == ['z']", sent and sent[0].get("names") == ["z"], str(sent))
    check("delete: response skipped has x, in_use == ['x']",
          st == 200 and "x" in d.get("skipped", []) and d.get("in_use") == ["x"]
          and d.get("deleted") == ["z"], json.dumps(d))
    check("delete: response carries a dag (capture still ran)", "dag" in d or "dag_delta" in d,
          json.dumps(d)[:200])
    VM_VARS.pop("z", None)
    RT["vars"].pop("z", None)


async def _t_case1(ctx):
    print("(c) case 1: another live cell defines it")
    nid = ctx["nid"]
    c4b = {"id": "c4", "type": "code", "code": "x = 3"}
    check("PUT c4 as 'x = 3' (no run) → 200", await _put(nid, [C2, c4b]) == 200)
    st, d = await _get("variables")
    x = (d or {}).get("variables", {}).get("x", {})
    check("/variables: x orphan false, reason null",
          x.get("orphan") is False and x.get("orphan_reason") is None, json.dumps(x))
    st, d = await _get("variables/untracked")
    names = [v.get("name") for v in d.get("visible", [])] + [v.get("name") for v in d.get("in_use", [])]
    check("untracked: x neither visible nor in_use", "x" not in names, json.dumps(d))
    VM.requests.clear()
    st, d = await _get("variables/delete", {"names": ["x"]})
    sent = [_json(r.content) for r in VM.requests if r.url.path == "/variables/delete"]
    check("delete strips x (another live cell defines it)",
          sent and sent[0].get("names") == [] and d.get("in_use") == ["x"], str(sent))
    check("PUT back to [c2, c4 'w = 2'] → 200", await _put(nid, [C2, C4]) == 200)


async def _t_redefine(ctx):
    print("(d) re-definition clears without a run")
    nid = ctx["nid"]
    st, d = await _get("variables")
    check("precondition: x used_by ['c2']",
          (d or {}).get("variables", {}).get("x", {}).get("used_by") == ["c2"])
    c5 = {"id": "c5", "type": "code", "code": "x = 1"}
    check("PUT adding c5 'x = 1' → 200", await _put(nid, [c5, C2, C4]) == 200)
    VM.requests.clear()
    st, d = await _get("variables")
    x = (d or {}).get("variables", {}).get("x", {})
    check("/variables: x orphan false after re-definition",
          x.get("orphan") is False and x.get("orphan_reason") is None and "used_by" not in x, json.dumps(x))
    check("no /execute was needed", not any(p == "/execute" for _, p in VM.paths()))
    st, d = await _get("variables/untracked")
    names = [v.get("name") for v in d.get("visible", [])] + [v.get("name") for v in d.get("in_use", [])]
    check("untracked: x absent", "x" not in names, json.dumps(d))
    VM.requests.clear()
    await _get("variables")
    check("a second identical read sends NO /dag PUT/PATCH (sync is a no-op)",
          VM.dag_calls() == [], str(VM.paths()))


async def _t_unchanged(ctx):
    print("(e) unchanged behaviour")
    VM_VARS["a1"] = _entry("a1", orphan=True, reason="agent")
    var_orphans.record_writes("sf1", ["a1"], "agent")   # the proxy saw an agent write a1
    VM_VARS["u1"] = _entry("u1", orphan=True, reason="unknown")
    VM_VARS["_p"] = _entry("_p", orphan=True, reason="internal")
    st, d = await _get("variables")
    vs = (d or {}).get("variables", {})
    check("agent / unknown / internal / normal entries pass through equal",
          all(vs.get(n) == VM_VARS[n] for n in ("a1", "u1", "_p", "y", "w")), json.dumps(vs)[:400])
    st, d = await _get("variables/untracked")
    check("untracked: agent and unknown stay visible, internal stays internal",
          {"name": "a1", "reason": "agent"} in d.get("visible", [])
          and {"name": "u1", "reason": "unknown"} in d.get("visible", [])
          and d.get("internal") == ["_p"], json.dumps(d))
    # A live cell reading an agent orphan does NOT protect it from deletion.
    VM.requests.clear()
    st, d = await _get("variables/delete", {"names": ["a1"]})
    sent = [_json(r.content) for r in VM.requests if r.url.path == "/variables/delete"]
    check("delete: agent orphan is sent unchanged", sent and sent[0].get("names") == ["a1"], str(sent))
    for n in ("a1", "u1", "_p"):
        VM_VARS.pop(n, None)

    # scratch session: no notebook → no /dag call, payload unchanged
    VMM.sessions["s-scratch"] = {"vm_id": "vm-s", "endpoint": "eps"}
    VM.requests.clear()
    st, b = await asgi("GET", "/proxy/variables", headers={"X-Session-Id": "s-scratch"})
    check("scratch: no /dag call", not any(p == "/dag" for _, p in VM.paths()), str(VM.paths()))
    check("scratch: payload unchanged",
          _json(b) == {"variables": VM_VARS, "count": len(VM_VARS)})

    # non-200 and non-JSON replies pass through
    VM.routes[("GET", "/variables")] = lambda r: httpx.Response(500, json={"error": "boom"})
    st, d = await _get("variables")
    check("non-200 passes through", st == 500 and d == {"error": "boom"}, f"{st} {d}")
    VM.routes[("GET", "/variables")] = lambda r: httpx.Response(200, content=b"not json",
                                                                headers={"content-type": "text/plain"})
    st, b = await asgi("GET", "/proxy/variables", headers=HDR)
    check("non-JSON passes through", st == 200 and b == b"not json", f"{st} {b[:80]!r}")
    _vm_routes()


async def _t_writer(ctx):
    print("(g) no-cell writer: agent vs UI/API (older image labels both 'agent')")
    VM.routes[("POST", "/set-variable")] = lambda r: httpx.Response(200, json={"success": True})

    def _exec(r):
        code = (_json(r.content) or {}).get("code", "")
        name = code.split("=")[0].strip()
        return httpx.Response(200, json={"success": True, "variables_created": [name],
                                         "variables_rebound": [], "variables_read": []})
    VM.routes[("POST", "/execute")] = _exec
    st1, _ = await _get("set-variable", {"name": "ui_v", "value": 1})          # UI control
    st2, _ = await _get("execute", {"code": "nc_v = 2"})                        # API, no cell
    st3, _ = await _get("execute", {"code": "ag_v = 3", "origin": "agent"})     # agent tool
    check("writes forwarded → 200", (st1, st2, st3) == (200, 200, 200))
    for n in ("ui_v", "nc_v", "ag_v", "legacy_v"):
        VM_VARS[n] = _entry(n, orphan=True, reason="agent")    # what the older image says
    st, d = await _get("variables")
    vs = (d or {}).get("variables", {})
    check("UI set-variable → 'no cell'", vs.get("ui_v", {}).get("orphan_reason") == "no cell", json.dumps(vs.get("ui_v")))
    check("API no-cell execute → 'no cell'", vs.get("nc_v", {}).get("orphan_reason") == "no cell")
    check("agent execute (origin agent) → stays 'agent'", vs.get("ag_v", {}).get("orphan_reason") == "agent")
    check("never seen by this proxy (legacy) → 'no cell'", vs.get("legacy_v", {}).get("orphan_reason") == "no cell")
    check("still orphans", all(vs.get(n, {}).get("orphan") is True for n in ("ui_v", "nc_v", "ag_v", "legacy_v")))
    st, d = await _get("variables/untracked")
    vis = {v["name"]: v["reason"] for v in d.get("visible", [])}
    check("untracked: no-cell writes stay in clean-up with reason 'no cell'; agent stays 'agent'",
          vis.get("ui_v") == "no cell" and vis.get("nc_v") == "no cell" and vis.get("ag_v") == "agent"
          and vis.get("legacy_v") == "no cell", json.dumps(vis))
    st, d = await _get("variables/schema")
    sch = {e["name"]: e for e in (d or {}).get("variables", [])}
    check("schema (agent tools): same relabel", sch.get("ui_v", {}).get("orphan_reason") == "no cell"
          and sch.get("ag_v", {}).get("orphan_reason") == "agent")
    await _get("set-variable", {"name": "ag_v", "value": 9})                    # the user overwrites it
    st, d = await _get("variables/inspect", {"name": "ag_v"})
    check("last no-cell writer wins (user after agent → 'no cell')", (d or {}).get("orphan_reason") == "no cell",
          json.dumps(d))
    await _get("execute", {"code": "cell_v = 1", "cell_id": "c2"})
    check("a write WITH a cell id is not recorded", var_orphans.writer_of("sf1", "cell_v") is None)
    VM_VARS["new_img"] = _entry("new_img", orphan=True, reason="no cell")      # newer image
    st, d = await _get("variables")
    check("newer image 'no cell' passes through", (d or {}).get("variables", {}).get("new_img", {})
          .get("orphan_reason") == "no cell")
    # scratch session (no notebook): relabel still applies
    VMM.sessions["s-scratch"] = {"vm_id": "vm-s", "endpoint": "eps"}
    st, b = await asgi("GET", "/proxy/variables", headers={"X-Session-Id": "s-scratch"})
    check("scratch session: 'agent' from an unseen writer → 'no cell'",
          (_json(b) or {}).get("variables", {}).get("ui_v", {}).get("orphan_reason") == "no cell")
    for n in ("ui_v", "nc_v", "ag_v", "legacy_v", "new_img"):
        VM_VARS.pop(n, None)
    _vm_routes()


def _t_pure():
    print("(f) pure functions")
    doc = {"nodes": [
        {"cell_id": "b", "position": 1, "reads": ["x"], "writes": ["df"], "mutates": ["df"]},
        {"cell_id": "a", "position": 0, "reads": [], "writes": ["x"], "mutates": []},
        {"cell_id": "c", "position": 2, "reads": ["x", "df"], "writes": [], "mutates": []},
    ], "runtime": {"vars": {"df": {"defined_by": "gone"}, "q": {"defined_by": ""},
                            "x": {"defined_by": "a"}}}}
    use = var_orphans.live_use(doc)
    live, definers, readers = use
    check("live_use: live ids", live == {"a", "b", "c"})
    check("live_use: a mutate-only writer is not a definer", "df" not in definers and definers == {"x": ["a"]})
    check("live_use: readers in notebook order", readers == {"x": ["b", "c"], "df": ["c"]})
    check("live_use: malformed doc → empty", var_orphans.live_use(None) == (set(), {}, {})
          and var_orphans.live_use({"nodes": "bad"}) == (set(), {}, {}))
    e = var_orphans.adjust_entry("x", {"orphan": True, "orphan_reason": "deleted cell"}, use)
    check("adjust_entry: live definer → not orphan", e == {"orphan": False, "orphan_reason": None})
    e = var_orphans.adjust_entry("df", {"orphan": True, "orphan_reason": "deleted cell"}, use)
    check("adjust_entry: live readers → used_by", e.get("used_by") == ["c"] and e["orphan"] is True)
    e = var_orphans.adjust_entry("zz", {"orphan": True, "orphan_reason": "deleted cell"}, use)
    check("adjust_entry: nobody → unchanged", e == {"orphan": True, "orphan_reason": "deleted cell"})
    e = var_orphans.adjust_entry("x", {"orphan": True, "orphan_reason": "unknown"}, use)
    check("adjust_entry: other reasons untouched", e == {"orphan": True, "orphan_reason": "unknown"})
    var_orphans.record_writes("sp", ["x"], "agent")
    e = var_orphans.adjust_entry("x", {"orphan": True, "orphan_reason": "agent"}, use, "sp")
    check("adjust_entry: agent kept when the proxy saw an agent write", e["orphan_reason"] == "agent")
    e = var_orphans.adjust_entry("x", {"orphan": True, "orphan_reason": "agent"}, None, "other")
    check("adjust_entry: agent from an unseen writer → 'no cell' (use None ok)", e["orphan_reason"] == "no cell")
    doc["runtime"]["vars"]["q"] = {"defined_by": ""}
    doc["nodes"][2]["reads"].append("q")
    use = var_orphans.live_use(doc)
    keep, in_use = var_orphans.guard_delete_names(["df", "q", "x", "nope"], doc, use)
    check("guard: deleted-producer var read by a live cell is stripped; agent ('') kept; "
          "live producer kept (VM decides)", in_use == ["df"] and keep == ["q", "x", "nope"],
          f"{keep} {in_use}")
    long = "v" * 300
    d = var_orphans.adjust_payload("variables/untracked",
                                   {"visible": [{"name": long, "reason": "deleted cell"}]}, use)
    check("untracked: long names kept whole", d["visible"][0]["name"] == long and d["in_use"] == [])


async def main_async():
    ctx = {}
    real_push = dag_service.schedule_push
    dag_service.schedule_push = lambda sid, nid=None: None   # deterministic VM traffic
    try:
        await _setup(ctx)
        await _t_case2(ctx)
        await _t_case3(ctx)
        await _t_case1(ctx)
        await _t_redefine(ctx)
        await _t_unchanged(ctx)
        await _t_writer(ctx)
    finally:
        dag_service.schedule_push = real_push
    _t_pure()
    c = _client_holder.pop("c", None)
    if c is not None:
        await c.aclose()


def _use_own_db():
    _sq.DB_DIR = _TMP
    _sq.DB_PATH = os.path.join(_TMP, _DB)
    _sq._local = threading.local()
    astorage._sync.initialize()


def test_deleted_cell_flag_proxy():
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
        test_deleted_cell_flag_proxy()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")
