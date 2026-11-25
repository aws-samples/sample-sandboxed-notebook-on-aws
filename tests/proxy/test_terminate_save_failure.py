#!/usr/bin/env python3
# test-kind: offline
"""
/terminate keeps the VM running when the pre-terminate save fails
(.agents/tasks/big-save-failure).

Bug: the notebook server was killed by the out-of-memory killer while saving a
large variable. The Lambda MicroVM endpoint answered the save with an empty 502
(x-aws-proxy-error: APP_CONNECT_FAILED); /terminate logged "proceeding to
terminate" at INFO, terminated the VM and answered 200, so the state was lost
without a word to the user.

Now:
  - a failed save (the server's own error, busy, no answer while the VM is up,
    or a variable left out because it would not fit in memory or on disk)
    returns 409 {"error": "Save failed: <reason>. The VM was kept running so you
    can free memory or save again.", "saveFailed": true, ...} and does NOT
    terminate the VM or touch the session registry;
  - {"terminateWithoutSaving": true} skips the save and terminates;
  - a VM that died during the save (502, then TERMINATED) is cleaned up and the
    200 body carries checkpoint.vmLost with an out-of-memory explanation;
  - a successful save, or one that only skipped unpicklable variables, keeps
    today's 200 response shape;
  - POST /checkpoint-save names the cause instead of "vm returned 502".

Drives proxy.platform.routes.microvm with a fake vm_manager, a fake VM HTTP
client and a temp SQLite storage. No AWS, no running proxy.
Run:
    .venv/bin/python tests/proxy/test_terminate_save_failure.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import proxy.storage.sqlite_db as sq  # noqa: E402
from proxy.storage.async_storage import AsyncStorage  # noqa: E402
import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from proxy.notebook import dag_service  # noqa: E402
from proxy.platform import http_clients  # noqa: E402

_fails = []
KEPT = "The VM was kept running so you can free memory or save again."


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


class _FakeTokenCache:
    def __init__(self):
        self.deleted = []

    async def delete(self, vm_id):
        self.deleted.append(vm_id)


class _FakeVmManager:
    def __init__(self, states=None):
        self.active_microvms = {}
        self._session_registry = {}
        self.terminate_calls = []
        self.unregister_calls = []
        self.token_cache = _FakeTokenCache()
        self._states = list(states or [])   # get_vm_state answers, in order; then RUNNING

    def add(self, session_id, vm_id):
        ep = f"{vm_id}.ep"
        self.active_microvms[vm_id] = {"endpoint": ep, "session_id": session_id}
        self._session_registry[session_id] = {"vm_id": vm_id, "endpoint": ep}

    async def get_session_vm_async(self, session_id):
        return self._session_registry.get(session_id)

    def get_session_vm(self, session_id):
        return self._session_registry.get(session_id)

    def unregister_session(self, session_id):
        self.unregister_calls.append(session_id)
        self._session_registry.pop(session_id, None)

    async def get_vm_state(self, vm_id):
        return self._states.pop(0) if self._states else "RUNNING"

    async def resume_vm(self, vm_id):
        raise AssertionError("resume_vm must not be called")

    async def get_auth_token(self, vm_id):
        return "tok"

    async def terminate_vm(self, vm_id):
        self.terminate_calls.append(vm_id)


class _FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})

    def json(self):
        if self._body is None:
            raise ValueError("empty body")
        return self._body


class _VmClient:
    def __init__(self, resp):
        self.resp = resp
        self.posts = []

    async def post(self, url, headers=None, content=None, timeout=None):
        self.posts.append(url)
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class _Req:
    def __init__(self, vm_manager, body, headers=None):
        self._body = body
        self.headers = dict(headers or {})

        class _S:
            pass
        st = _S()
        st.vm_manager = vm_manager
        app = _S()
        app.state = st
        self.app = app

    async def body(self):
        return json.dumps(self._body).encode() if self._body is not None else b""

    async def json(self):
        return self._body


def _decode(resp):
    if isinstance(resp, dict):
        return 200, resp
    return resp.status_code, json.loads(resp.body)


class _Env:
    def __init__(self, resp, states=None):
        self.resp = resp
        self.states = states

    def __enter__(self):
        self._env = dict(os.environ)
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        self._tmp = tempfile.mkdtemp(prefix="term_save_")
        self._orig_sq = (sq.DB_DIR, sq.DB_PATH, sq._local)
        sq.DB_DIR = self._tmp
        sq.DB_PATH = os.path.join(self._tmp, "t.db")
        sq._local = threading.local()
        sq._HELD_SESSION_LOCKS.clear()
        self.storage = sq.SqliteStorage()
        self.storage.initialize()
        self.vm_client = _VmClient(self.resp)

        async def _flush(sid, nid=None, *, vm_id=None, endpoint=None, timeout=5.0):
            return True

        self._orig = (microvm_mod.astorage, dag_service.flush, http_clients.vm_client,
                      getattr(microvm_mod, "SAVE_FAILURE_STATE_INTERVAL_SEC", None))
        microvm_mod.astorage = AsyncStorage(self.storage)
        dag_service.flush = _flush
        http_clients.vm_client = lambda: self.vm_client
        microvm_mod.SAVE_FAILURE_STATE_INTERVAL_SEC = 0
        self.vm = _FakeVmManager(self.states)
        nb = self.storage.notebook_create("nb-A", session_id="sess-A")
        self.storage.notebook_update(str(nb["id"]), session_id="sess-A", microvm_id="vm-A")
        self.storage.vm_session_create(microvm_id="vm-A", notebook_id=str(nb["id"]),
                                       session_id="sess-A", endpoint="vm-A.ep")
        self.vm.add("sess-A", "vm-A")
        return self

    def __exit__(self, *exc):
        (microvm_mod.astorage, dag_service.flush, http_clients.vm_client,
         microvm_mod.SAVE_FAILURE_STATE_INTERVAL_SEC) = self._orig
        sq.DB_DIR, sq.DB_PATH, sq._local = self._orig_sq
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._env)

    def terminate(self, **extra):
        return _decode(asyncio.run(microvm_mod.terminate_session(
            _Req(self.vm, {"microvmId": "vm-A", **extra}, {"X-Session-Id": "sess-A"}))))

    def kept(self, label):
        check(f"{label}: terminate_vm not called", self.vm.terminate_calls == [], self.vm.terminate_calls)
        check(f"{label}: session registry intact",
              self.vm._session_registry.get("sess-A", {}).get("vm_id") == "vm-A" and not self.vm.unregister_calls)
        check(f"{label}: VM still tracked", "vm-A" in self.vm.active_microvms)


def test_server_error_keeps_vm():
    with _Env(_FakeResp(200, {"success": False, "error": "S3 access denied"})) as env:
        status, body = env.terminate()
        check("server error: 409", status == 409, f"{status} {body}")
        check("server error: message", body.get("error") == f"Save failed: S3 access denied. {KEPT}", body)
        check("server error: saveFailed + reason", body.get("saveFailed") is True
              and body.get("reason") == "S3 access denied", body)
        env.kept("server error")


def test_not_saved_for_memory_keeps_vm():
    item = {"name": "big", "kind": "memory",
            "reason": "not enough memory to save 'big' (needs about 1300 MiB, 900 MiB available)"}
    with _Env(_FakeResp(200, {"success": True, "not_saved": [item]})) as env:
        status, body = env.terminate()
        check("not saved (memory): 409", status == 409, f"{status} {body}")
        check("not saved (memory): message names the variable and sizes",
              body.get("error") == f"Save failed: {item['reason']}. The other variables were saved. {KEPT}", body)
        check("not saved (memory): notSaved listed", body.get("notSaved") == [item], body)
        env.kept("not saved (memory)")
    kept_item = dict(item, kept_previous=True)
    with _Env(_FakeResp(200, {"success": True, "not_saved": [kept_item]})) as env:
        status, body = env.terminate()
        check("not saved, previous copy kept: 409 and says so", status == 409 and body.get("error") ==
              f"Save failed: {item['reason']} (its previously saved copy was kept). "
              f"The other variables were saved. {KEPT}", f"{status} {body}")
        env.kept("not saved, previous copy kept")


def test_unpicklable_only_terminates():
    item = {"name": "conn", "kind": "unpicklable", "reason": "Connection: cannot pickle"}
    with _Env(_FakeResp(200, {"success": True, "not_saved": [item]})) as env:
        status, body = env.terminate()
        check("unpicklable only: 200, shape unchanged", status == 200 and body == {
            "status": "terminated", "microvmId": "vm-A", "sessionId": "sess-A"}, f"{status} {body}")
        check("unpicklable only: terminated", env.vm.terminate_calls == ["vm-A"])


def test_without_saving():
    with _Env(_FakeResp(200, {"success": False, "error": "x"})) as env:
        status, body = env.terminate(terminateWithoutSaving=True)
        check("without saving: 200", status == 200 and body.get("status") == "terminated", f"{status} {body}")
        check("without saving: no save request", env.vm_client.posts == [], env.vm_client.posts)
        check("without saving: terminated", env.vm.terminate_calls == ["vm-A"])
    with _Env(_FakeResp(200, {"success": False, "error": "x"})) as env:
        status, _ = env.terminate(terminateWithoutSaving="yes")
        check("without saving: only the boolean true counts", status == 409, status)


def test_502_vm_alive_keeps_vm():
    with _Env(_FakeResp(502, None, {"x-aws-proxy-error": "APP_CONNECT_FAILED"})) as env:
        status, body = env.terminate()
        check("502, VM up: 409", status == 409, f"{status} {body}")
        check("502, VM up: names the status and the endpoint error",
              body.get("reason") == "the notebook server did not answer the save request "
                                    "(HTTP 502, APP_CONNECT_FAILED)", body)
        env.kept("502, VM up")


def test_502_vm_lost_cleans_up():
    # state before the save RUNNING, then RUNNING, then TERMINATED (exit code 137).
    with _Env(_FakeResp(502, None, {"x-aws-proxy-error": "APP_CONNECT_FAILED"}),
              states=["RUNNING", "RUNNING", "TERMINATED"]) as env:
        status, body = env.terminate()
        check("VM lost: 200 terminated", status == 200 and body.get("status") == "terminated", f"{status} {body}")
        cp = body.get("checkpoint") or {}
        check("VM lost: checkpoint.vmLost", cp.get("success") is False and cp.get("vmLost") is True, body)
        check("VM lost: says it ran out of memory", "ran out of memory" in str(cp.get("error")), cp)
        check("VM lost: cleaned up", env.vm.terminate_calls == ["vm-A"] and env.vm.unregister_calls == ["sess-A"])


def test_busy_keeps_vm():
    msg = "VM busy — execution in progress; checkpoint not taken (execute lock not free within 25s)"
    with _Env(_FakeResp(200, {"success": False, "error": msg, "busy": True})) as env:
        status, body = env.terminate()
        check("busy: 409", status == 409 and body.get("reason") == msg, f"{status} {body}")
        env.kept("busy")


def test_transport_error_keeps_vm():
    with _Env(RuntimeError("read timeout")) as env:
        status, body = env.terminate()
        check("transport error: 409", status == 409, f"{status} {body}")
        check("transport error: reason", body.get("reason") ==
              "the notebook server did not answer the save request (read timeout)", body)
        env.kept("transport error")


def test_success_shape_unchanged():
    with _Env(_FakeResp(200, {"success": True, "not_saved": []})) as env:
        status, body = env.terminate()
        check("success: 200, shape unchanged", status == 200 and body == {
            "status": "terminated", "microvmId": "vm-A", "sessionId": "sess-A"}, f"{status} {body}")


def test_checkpoint_save_route_error():
    with _Env(_FakeResp(502, None, {"x-aws-proxy-error": "APP_CONNECT_FAILED"}),
              states=["RUNNING", "TERMINATED"]) as env:
        res = asyncio.run(microvm_mod.checkpoint_session(_Req(env.vm, None, {"X-Session-Id": "sess-A"})))
        check("/checkpoint-save: success false", res.get("success") is False, res)
        check("/checkpoint-save: explains the out-of-memory stop",
              "ran out of memory" in str(res.get("error")) and res.get("vmLost") is True, res)


def test_vm_card_not_saved():
    from proxy.platform.vm_detail import _not_saved
    item = {"name": "big", "kind": "memory", "reason": "not enough memory to save 'big'"}
    check("card: not_saved passed through", _not_saved([item, {"reason": "no name"}, "x"]) == [item])
    check("card: missing → []", _not_saved(None) == [] and _not_saved({}) == [])
    check("card: capped at 20", len(_not_saved([{"name": f"v{i}"} for i in range(30)])) == 20)


def _run():
    for fn in (test_vm_card_not_saved, test_server_error_keeps_vm, test_not_saved_for_memory_keeps_vm, test_unpicklable_only_terminates,
               test_without_saving, test_502_vm_alive_keeps_vm, test_502_vm_lost_cleans_up, test_busy_keeps_vm,
               test_transport_error_keeps_vm, test_success_shape_unchanged, test_checkpoint_save_route_error):
        print(f"\n# {fn.__name__}")
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} raised {type(e).__name__}: {e}", False)


if __name__ == "__main__":
    _run()
    print()
    if _fails:
        print(f"FAILED: {len(_fails)}")
        for f in _fails:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
