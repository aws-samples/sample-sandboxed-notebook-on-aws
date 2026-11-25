#!/usr/bin/env python3
# test-kind: offline
"""
/launch persists the notebook → VM link (pool-reopen-modal-bug regression).

Bug: with the warm pool ON, open notebook → pooled spare granted + linked → close
tab (VM SUSPENDED) → reopen showed the 'Connect to Sandbox' modal listing the
notebook's own VM. Root cause: the notebooks row's microvm_id / checkpoint_enabled
(exactly what reopen reads — App.jsx `canAutoRestore`, useInstances.js auto-connect)
were written ONLY by the browser autosave, which doesn't re-arm on a VM link. A
pool grant links in ~1-5s, so open → close with no edit left the row unlinked.

Fix: launch_microvm writes the link server-side on every success branch (pool
grant, cold launch, idempotent reuse), best-effort and update-only.

Drives proxy.platform.routes.microvm.launch_microvm against a REAL temp SQLite
storage (wrapped in AsyncStorage, exactly like production `astorage`) with fake
vm_manager / pool-manager HTTP — no AWS, no running proxy or pool manager.

Run:
    .venv/bin/python tests/pool/test_proxy_launch_notebook_link.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
import proxy.storage.sqlite_db as sq  # noqa: E402
from proxy.storage.async_storage import AsyncStorage  # noqa: E402
from tests.pool.test_proxy_launch_grant import (  # noqa: E402
    EnvSandbox, _FakeAsyncClient, _FakeResponse, _FakeVmManager, _FakeRequest,
)

# Response keys every /launch success branch returned BEFORE this fix — must be
# unchanged (backward compatibility). Reuse additionally carries "reused".
_BASE_KEYS = {"microvmId", "endpoint", "name", "sessionId", "status", "memory_mib", "architecture"}


# ── fixtures ────────────────────────────────────────────────────────────────

class _Env:
    """Temp SQLite storage + patched module globals; restores everything on exit."""

    def __init__(self, pool_enabled=True, mode="checkpoint"):
        self.pool_enabled = pool_enabled
        self.mode = mode

    def __enter__(self):
        self._env = EnvSandbox().__enter__()
        os.environ["POOL_ENABLED"] = "true" if self.pool_enabled else "false"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["SESSION_PERSISTENCE_MODE"] = self.mode

        self._tmp = tempfile.mkdtemp(prefix="nb_link_test_")
        self._orig_sq = (sq.DB_DIR, sq.DB_PATH, sq._local)
        sq.DB_DIR = self._tmp
        sq.DB_PATH = os.path.join(self._tmp, "test.db")
        sq._local = threading.local()
        sq._HELD_SESSION_LOCKS.clear()
        self.storage = sq.SqliteStorage()
        self.storage.initialize()

        self._orig = (microvm_mod.astorage, httpx.AsyncClient,
                      microvm_mod.EXEC_ROLE_ARN, microvm_mod.IMAGE_ARN)
        microvm_mod.astorage = AsyncStorage(self.storage)
        httpx.AsyncClient = _FakeAsyncClient
        microvm_mod.EXEC_ROLE_ARN = "arn:aws:iam::123456789012:role/exec"
        microvm_mod.IMAGE_ARN = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"
        self.grant_posts = []

        def _grant(url, json):
            self.grant_posts.append(json)
            return _FakeResponse(200, {"vm_id": "pool-vm-granted", "endpoint": "pool-ep",
                                       "launched_at": "2026-01-01T00:00:00+00:00"})
        _FakeAsyncClient.behavior = _grant
        return self

    def __exit__(self, *exc):
        (microvm_mod.astorage, httpx.AsyncClient,
         microvm_mod.EXEC_ROLE_ARN, microvm_mod.IMAGE_ARN) = self._orig
        sq.DB_DIR, sq.DB_PATH, sq._local = self._orig_sq
        shutil.rmtree(self._tmp, ignore_errors=True)
        self._env.__exit__(*exc)

    def new_notebook(self, session_id):
        # Mirrors createTab() POST + the first autosave BEFORE the VM is linked:
        # session_id set, microvm_id NULL, checkpoint_enabled 0.
        nb = self.storage.notebook_create("nb", session_id=session_id)
        self.storage.notebook_update(str(nb["id"]), session_id=session_id,
                                     microvm_id=None, checkpoint_enabled=0)
        return nb["id"]

    def launch(self, vm_manager, session_id, notebook_id):
        body = {"name": "nb", "sessionId": session_id, "memoryMiB": 2048, "arch": "arm64",
                "checkpointEnabled": True, "idleTimeoutSeconds": 60, "maxDurationSeconds": 28800}
        if notebook_id is not None:
            body["notebookId"] = notebook_id
        return asyncio.run(microvm_mod.launch_microvm(_FakeRequest(vm_manager, body)))


def _reopen_decision(row, instances):
    """Python mirror of the frontend reopen decision (App.jsx reopenNotebook +
    Notebook.jsx initial showConnection + useInstances.js auto-connect):
    returns 'auto' when the tab reconnects without the modal, else 'modal'."""
    microvm_id = row.get("microvm_id") or None
    checkpoint_enabled = bool(row.get("checkpoint_enabled"))
    session_saved = bool(row.get("session_id") and checkpoint_enabled)
    if session_saved and row.get("session_id") and checkpoint_enabled:
        return "auto"  # status='launching' → /instances attach or /launch (idempotent)
    if microvm_id and instances.get(microvm_id, {}).get("state") in ("RUNNING", "SUSPENDED"):
        return "auto"  # useInstances poll auto-connect → modal auto-dismissed
    return "modal"


# ── tests ───────────────────────────────────────────────────────────────────

def test_pool_grant_persists_notebook_link():
    with _Env(pool_enabled=True, mode="checkpoint") as env:
        nb_id = env.new_notebook("sess-pool")
        before = env.storage.notebook_get(str(nb_id))
        assert _reopen_decision(before, {"pool-vm-granted": {"state": "SUSPENDED"}}) == "modal"  # the bug

        vm = _FakeVmManager()
        result = env.launch(vm, "sess-pool", nb_id)

        assert len(env.grant_posts) == 1 and vm.run_vm_calls == []  # pooled, not cold
        assert set(result) == _BASE_KEYS and result["microvmId"] == "pool-vm-granted"
        row = env.storage.notebook_get(str(nb_id))
        assert row["microvm_id"] == "pool-vm-granted"
        assert row["session_id"] == "sess-pool"
        assert row["checkpoint_enabled"] is True
        # Pooled VM SUSPENDED → reopen ⇒ auto-resume, no modal.
        assert _reopen_decision(row, {"pool-vm-granted": {"state": "SUSPENDED"}}) == "auto"


def test_pool_grant_none_mode_links_vm_without_checkpoint():
    with _Env(pool_enabled=True, mode="none") as env:
        nb_id = env.new_notebook("sess-none")
        env.launch(_FakeVmManager(), "sess-none", nb_id)
        row = env.storage.notebook_get(str(nb_id))
        assert row["microvm_id"] == "pool-vm-granted"
        assert row["checkpoint_enabled"] is False  # ephemeral: no restore promised
        assert _reopen_decision(row, {"pool-vm-granted": {"state": "SUSPENDED"}}) == "auto"


def test_cold_launch_persists_notebook_link():
    with _Env(pool_enabled=False, mode="checkpoint") as env:
        nb_id = env.new_notebook("sess-cold")
        vm = _FakeVmManager()
        result = env.launch(vm, "sess-cold", nb_id)
        assert env.grant_posts == [] and len(vm.run_vm_calls) == 1
        assert set(result) == _BASE_KEYS and result["microvmId"] == "cold-vm-1"
        row = env.storage.notebook_get(str(nb_id))
        assert row["microvm_id"] == "cold-vm-1" and row["checkpoint_enabled"] is True
        assert _reopen_decision(row, {"cold-vm-1": {"state": "SUSPENDED"}}) == "auto"


def test_idempotent_reuse_self_heals_unlinked_row():
    for state in ("RUNNING", "SUSPENDED", "SUSPENDING", "RESUMING"):
        with _Env(pool_enabled=True, mode="checkpoint") as env:
            nb_id = env.new_notebook("sess-reuse")
            vm = _FakeVmManager()
            vm._session_registry["sess-reuse"] = {"vm_id": "live-vm", "endpoint": "live-ep"}

            async def _state(_vm_id, _s=state):
                return _s
            vm.get_vm_state = _state
            result = env.launch(vm, "sess-reuse", nb_id)
            # Live VM returned — no grant, no cold launch (no duplicate VM).
            assert result["microvmId"] == "live-vm" and result.get("reused") is True, state
            assert set(result) == _BASE_KEYS | {"reused"}, state
            assert env.grant_posts == [] and vm.run_vm_calls == [], state
            row = env.storage.notebook_get(str(nb_id))
            assert row["microvm_id"] == "live-vm" and row["checkpoint_enabled"] is True, state


def test_dead_registered_vm_is_not_reused():
    with _Env(pool_enabled=True, mode="checkpoint") as env:
        nb_id = env.new_notebook("sess-dead")
        vm = _FakeVmManager()
        vm._session_registry["sess-dead"] = {"vm_id": "dead-vm", "endpoint": "x"}

        async def _state(_vm_id):
            return "TERMINATED"
        vm.get_vm_state = _state
        result = env.launch(vm, "sess-dead", nb_id)
        assert result["microvmId"] == "pool-vm-granted" and "reused" not in result
        assert env.storage.notebook_get(str(nb_id))["microvm_id"] == "pool-vm-granted"


def test_link_write_failure_never_fails_or_rolls_back_launch():
    with _Env(pool_enabled=True, mode="checkpoint") as env:
        nb_id = env.new_notebook("sess-fail")

        def _boom(*a, **k):
            raise RuntimeError("db down")
        env.storage.notebook_update = _boom
        vm = _FakeVmManager()
        result = env.launch(vm, "sess-fail", nb_id)
        assert isinstance(result, dict) and result["microvmId"] == "pool-vm-granted"
        # Grant NOT rolled back, no fallthrough cold launch.
        assert "pool-vm-granted" in vm.active_microvms
        assert vm._session_registry["sess-fail"]["vm_id"] == "pool-vm-granted"
        assert vm.unregister_session_calls == [] and vm.run_vm_calls == []


def test_no_notebook_id_and_deleted_notebook_write_nothing():
    with _Env(pool_enabled=True, mode="checkpoint") as env:
        other = env.new_notebook("sess-other")
        env.launch(_FakeVmManager(), "sess-anon", None)
        assert env.storage.notebook_get(str(other))["microvm_id"] is None

        gone = env.new_notebook("sess-gone")
        env.storage.notebook_delete(str(gone))
        env.launch(_FakeVmManager(), "sess-gone", gone)
        assert env.storage.notebook_get(str(gone)) is None  # update-only: no resurrection
        assert [n["id"] for n in env.storage.notebook_list()] == [other]


if __name__ == "__main__":
    # Standalone runner (repo convention: tests are __main__ scripts; pytest is
    # not installed in .venv). Runs every top-level test_* function in order.
    import traceback
    _tests = [(n, f) for n, f in list(globals().items())
              if n.startswith("test_") and callable(f)]
    _failed = []
    for _name, _fn in _tests:
        try:
            _fn()
            print(f"  ✓ {_name}")
        except Exception:
            _failed.append(_name)
            print(f"  ❌ {_name}")
            traceback.print_exc()
    print(f"\nNOTEBOOK-LINK: {len(_tests) - len(_failed)}/{len(_tests)} checks passed")
    sys.exit(1 if _failed else 0)
