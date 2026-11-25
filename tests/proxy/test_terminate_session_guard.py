#!/usr/bin/env python3
# test-kind: offline
"""
/terminate refuses a cross-session pair; /notebook-runs trusts the registry
(fix-tab-wrong-vm, fixes 1 and 2).

Bug: a notebook tab whose stale microvmId pointed at ANOTHER notebook's VM sent
POST /terminate with X-Session-Id = its own session and microvmId = the other
VM. The proxy checkpointed the other VM's kernel into this session's S3 prefix
(overwriting its checkpoint), flushed this session's DAG into that VM, and
terminated the other notebook's VM. /notebook-runs recorded the client's VM id
even when the session registry knew the real one.

Drives proxy.platform.routes.microvm.terminate_session and
proxy.notebook.routes.notebooks.notebook_run_create against a REAL temp SQLite
storage (wrapped in AsyncStorage like production `astorage`) with a fake
vm_manager, a spy DAG flush and a spy VM HTTP client. No AWS, no running proxy.

Run (sanitized env):
    env -i PATH="$PATH" HOME="$HOME" AWS_REGION=us-west-2 AWS_ACCESS_KEY_ID=testing \
      AWS_SECRET_ACCESS_KEY=testing AWS_SHARED_CREDENTIALS_FILE=/dev/null \
      AWS_CONFIG_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. \
      .venv/bin/python tests/proxy/test_terminate_session_guard.py
Exits non-zero on any failure.
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
import proxy.notebook.routes.notebooks as nb_routes  # noqa: E402
from proxy.notebook import dag_service  # noqa: E402
from proxy.platform import http_clients  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeTokenCache:
    def __init__(self):
        self.deleted = []

    async def delete(self, vm_id):
        self.deleted.append(vm_id)


class _FakeVmManager:
    """Only what terminate_session / _checkpoint_session_vm touch, with spies."""

    def __init__(self):
        self.active_microvms = {}
        self._session_registry = {}
        self.terminate_calls = []
        self.unregister_calls = []
        self.token_cache = _FakeTokenCache()

    def add(self, session_id, vm_id, in_memory=True, registry=True):
        ep = f"{vm_id}.ep"
        if in_memory:
            self.active_microvms[vm_id] = {"endpoint": ep, "session_id": session_id}
        if registry and session_id:
            self._session_registry[session_id] = {"vm_id": vm_id, "endpoint": ep}

    async def get_session_vm_async(self, session_id):
        return self._session_registry.get(session_id)

    def unregister_session(self, session_id):
        self.unregister_calls.append(session_id)
        self._session_registry.pop(session_id, None)

    async def get_vm_state(self, vm_id):
        return "RUNNING"

    async def resume_vm(self, vm_id):
        raise AssertionError("resume_vm must not be called")

    async def get_auth_token(self, vm_id):
        return "tok"

    async def terminate_vm(self, vm_id):
        self.terminate_calls.append(vm_id)


class _FakeResp:
    status_code = 200

    def json(self):
        return {"saved": True}


class _SpyVmClient:
    def __init__(self):
        self.posts = []

    async def post(self, url, headers=None, content=None, timeout=None):
        self.posts.append({"url": url, "body": json.loads(content) if content else None})
        return _FakeResp()


class _Req:
    """fastapi.Request stand-in: app.state.vm_manager, headers, body(), json()."""

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
    """Route return value → (status, dict)."""
    if isinstance(resp, dict):
        return 200, resp
    return resp.status_code, json.loads(resp.body)


# ── environment ──────────────────────────────────────────────────────────────
class _Env:
    def __enter__(self):
        self._env = dict(os.environ)
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        self._tmp = tempfile.mkdtemp(prefix="term_guard_")
        self._orig_sq = (sq.DB_DIR, sq.DB_PATH, sq._local)
        sq.DB_DIR = self._tmp
        sq.DB_PATH = os.path.join(self._tmp, "t.db")
        sq._local = threading.local()
        sq._HELD_SESSION_LOCKS.clear()
        self.storage = sq.SqliteStorage()
        self.storage.initialize()
        self.astorage = AsyncStorage(self.storage)
        self.vm_client = _SpyVmClient()
        self.flush_calls = []

        async def _flush(sid, nid=None, *, vm_id=None, endpoint=None, timeout=5.0):
            self.flush_calls.append((sid, vm_id))
            return True

        self._orig = (microvm_mod.astorage, nb_routes.astorage, dag_service.flush,
                      http_clients.vm_client)
        microvm_mod.astorage = self.astorage
        nb_routes.astorage = self.astorage
        dag_service.flush = _flush
        http_clients.vm_client = lambda: self.vm_client
        return self

    def __exit__(self, *exc):
        (microvm_mod.astorage, nb_routes.astorage, dag_service.flush,
         http_clients.vm_client) = self._orig
        sq.DB_DIR, sq.DB_PATH, sq._local = self._orig_sq
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._env)

    def seed(self, vm, session_id, vm_id, **kw):
        """Notebook + vm_sessions row + (by default) in-memory + registry entry."""
        nb = self.storage.notebook_create(f"nb-{session_id}", session_id=session_id)
        self.storage.notebook_update(str(nb["id"]), session_id=session_id, microvm_id=vm_id)
        self.storage.vm_session_create(microvm_id=vm_id, notebook_id=str(nb["id"]),
                                       session_id=session_id, endpoint=f"{vm_id}.ep")
        vm.add(session_id, vm_id, **kw)
        return nb["id"]

    def snapshot(self, nb_ids, vm_ids):
        return (
            [self.storage.notebook_get(str(n)) for n in nb_ids],
            [self.storage.vm_session_get(v) for v in vm_ids],
        )

    def terminate(self, vm, microvm_id, session_header=None):
        headers = {"X-Session-Id": session_header} if session_header else {}
        return _decode(asyncio.run(microvm_mod.terminate_session(
            _Req(vm, {"microvmId": microvm_id}, headers))))


# ── tests ────────────────────────────────────────────────────────────────────
def test_mismatch_refused():
    with _Env() as env:
        vm = _FakeVmManager()
        nb_a = env.seed(vm, "sess-AAAAAAAA-1111", "vm-A")
        nb_b = env.seed(vm, "sess-BBBBBBBB-2222", "vm-B")
        before = env.snapshot([nb_a, nb_b], ["vm-A", "vm-B"])
        reg_before = dict(vm._session_registry)
        status, body = env.terminate(vm, "vm-B", "sess-AAAAAAAA-1111")
        check("mismatch: 409", status == 409, f"{status} {body}")
        check("mismatch: error body", body == {"error": "microvmId belongs to a different session"}, str(body))
        check("mismatch: no checkpoint forward", env.vm_client.posts == [], str(env.vm_client.posts))
        check("mismatch: no DAG flush", env.flush_calls == [], str(env.flush_calls))
        check("mismatch: no terminate_vm", vm.terminate_calls == [], str(vm.terminate_calls))
        check("mismatch: no unregister", vm.unregister_calls == [], str(vm.unregister_calls))
        check("mismatch: registry untouched", vm._session_registry == reg_before)
        check("mismatch: in-memory VM kept", "vm-B" in vm.active_microvms and "vm-A" in vm.active_microvms)
        check("mismatch: token cache untouched", vm.token_cache.deleted == [])
        check("mismatch: storage untouched", env.snapshot([nb_a, nb_b], ["vm-A", "vm-B"]) == before)


def test_matching_pair_terminates():
    with _Env() as env:
        vm = _FakeVmManager()
        nb_a = env.seed(vm, "sess-A", "vm-A")
        env.seed(vm, "sess-B", "vm-B")
        status, body = env.terminate(vm, "vm-A", "sess-A")
        check("match: 200", status == 200, f"{status} {body}")
        check("match: response shape unchanged",
              body == {"status": "terminated", "microvmId": "vm-A", "sessionId": "sess-A"}, str(body))
        check("match: checkpoint forwarded once to vm-A for sess-A",
              len(env.vm_client.posts) == 1
              and env.vm_client.posts[0]["url"] == "https://vm-A.ep/checkpoint-save"
              and env.vm_client.posts[0]["body"] == {"session_id": "sess-A"}, str(env.vm_client.posts))
        check("match: DAG flushed for sess-A into vm-A", env.flush_calls == [("sess-A", "vm-A")], str(env.flush_calls))
        check("match: terminate_vm(vm-A)", vm.terminate_calls == ["vm-A"], str(vm.terminate_calls))
        check("match: session unregistered", vm.unregister_calls == ["sess-A"])
        check("match: notebook link cleared",
              not env.storage.notebook_get(str(nb_a)).get("session_id"))
        check("match: other session untouched", vm._session_registry.get("sess-B", {}).get("vm_id") == "vm-B")


def test_sessionless_vm_any_header():
    with _Env() as env:
        vm = _FakeVmManager()
        env.seed(vm, "sess-A", "vm-A")
        vm.active_microvms["vm-spare"] = {"endpoint": "vm-spare.ep"}  # no session, no DB row
        status, body = env.terminate(vm, "vm-spare", "sess-A")
        check("sessionless: 200", status == 200, f"{status} {body}")
        check("sessionless: terminate_vm(vm-spare)", vm.terminate_calls == ["vm-spare"], str(vm.terminate_calls))
        check("sessionless: sess-A mapping kept (CAS)", vm._session_registry.get("sess-A", {}).get("vm_id") == "vm-A")


def test_header_absent():
    with _Env() as env:
        vm = _FakeVmManager()
        env.seed(vm, "sess-A", "vm-A")
        env.seed(vm, "sess-B", "vm-B")
        status, body = env.terminate(vm, "vm-B", None)
        check("no header: 200", status == 200, f"{status} {body}")
        check("no header: response shape",
              body == {"status": "terminated", "microvmId": "vm-B", "sessionId": None}, str(body))
        check("no header: no checkpoint (as today)", env.vm_client.posts == [] and env.flush_calls == [])
        check("no header: terminate_vm(vm-B)", vm.terminate_calls == ["vm-B"])


def test_storage_only_vm_guarded():
    with _Env() as env:
        vm = _FakeVmManager()
        env.seed(vm, "sess-A", "vm-A")
        # vm-B known ONLY through storage (proxy restart: not in active_microvms).
        nb_b = env.seed(vm, "sess-B", "vm-B", in_memory=False, registry=False)
        before = env.snapshot([nb_b], ["vm-B"])
        status, body = env.terminate(vm, "vm-B", "sess-A")
        check("storage-only: 409", status == 409, f"{status} {body}")
        check("storage-only: no terminate / checkpoint / flush",
              vm.terminate_calls == [] and env.vm_client.posts == [] and env.flush_calls == [])
        check("storage-only: storage untouched", env.snapshot([nb_b], ["vm-B"]) == before)
        # In memory without a session_id key, but storage knows it → still guarded.
        vm.active_microvms["vm-B"] = {"endpoint": "vm-B.ep"}
        status, _ = env.terminate(vm, "vm-B", "sess-A")
        check("in-memory without session + storage session: 409", status == 409, str(status))
        # And the owner can still terminate it.
        status, body = env.terminate(vm, "vm-B", "sess-B")
        check("storage-only: owner terminates", status == 200 and vm.terminate_calls == ["vm-B"], f"{status} {body}")


def _post_run(env, body):
    return asyncio.run(nb_routes.notebook_run_create(_Req(None, body)))


def test_notebook_runs_prefers_registry():
    with _Env() as env:
        nb = env.storage.notebook_create("nb-runs", session_id="sess-A")
        env.storage.session_registry_upsert("sess-A", "vm-A", "vm-A.ep")
        base = {"notebookId": nb["id"], "trigger": "single", "cellsExecuted": 1,
                "startedAt": "2026-01-01T00:00:00+00:00", "finishedAt": "2026-01-01T00:00:01+00:00"}
        run = _post_run(env, {**base, "sessionId": "sess-A", "microvmId": "vm-FOREIGN"})
        check("runs: registry wins over client id", run.get("microvm_id") == "vm-A", str(run))
        run = _post_run(env, {**base, "sessionId": "sess-A"})
        check("runs: registry used when client sends none", run.get("microvm_id") == "vm-A", str(run))
        run = _post_run(env, {**base, "sessionId": "sess-NOREG", "microvmId": "vm-client"})
        check("runs: no registry → client id", run.get("microvm_id") == "vm-client", str(run))
        run = _post_run(env, {**base, "microvmId": "vm-client"})
        check("runs: no session → client id", run.get("microvm_id") == "vm-client", str(run))
        rows = env.storage.notebook_run_list(str(nb["id"]), limit=10)
        check("runs: all 4 recorded", len(rows) == 4, str(len(rows)))


def _run():
    for fn in (test_mismatch_refused, test_matching_pair_terminates, test_sessionless_vm_any_header,
               test_header_absent, test_storage_only_vm_guarded, test_notebook_runs_prefers_registry):
        print(f"\n# {fn.__name__}")
        try:
            fn()
        except Exception as e:  # a crash is a failure, keep going
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} raised {type(e).__name__}: {e}", False)


def test_main():
    _fails.clear()
    _run()
    assert not _fails, _fails


if __name__ == "__main__":
    _run()
    print()
    if _fails:
        print(f"FAILED: {len(_fails)}")
        for f in _fails:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
