#!/usr/bin/env python3
# test-kind: offline
"""
VM GET /vm-status (vm-card-state): read-only status for the Live MicroVMs card.

Offline: FastAPI TestClient over the REAL VM app (`app.server`, lifespan not run),
with the real SandboxExecutor and CheckpointManager.

Covers: the contract keys, python version, variable count/bytes without values in
the response, dirty flag (None when checkpoints are off; True never-saved; False
right after a fingerprint capture; True again after a change), packages, and the
untracked scan bounded by a timeout.

Run (needs duckdb/dill/pandas/fastapi: tests/requirements-dev.txt adds duckdb):
    PYTHONPATH=. .venv/bin/python tests/vm/test_vm_status_endpoint.py
Prints SKIP and exits 0 when duckdb is not installed (app.server imports it).
"""
import logging
import os
import platform
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []

KEYS = {"python", "checkpoint_enabled", "dirty", "last_checkpoint_at", "last_save",
        "restored", "restored_from", "var_count", "var_bytes", "untracked",
        "packages", "packages_total", "last_suspend_save"}


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _drop_stub_modules():
    for name in ("plotly.express", "plotly", "duckdb"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__spec__", None) is None and not hasattr(mod, "__file__"):
            del sys.modules[name]


def _vm_app():
    _drop_stub_modules()
    import app.server as server
    logging.disable(logging.WARNING)
    return server.app


def _run():
    # Isolate the checkpoint /tmp scan from the host. The dirty fingerprint folds
    # in a glob over /tmp; on a dev Mac that is ~1000 files and takes 3-4 s, so
    # cm.is_dirty() exceeds the endpoint's 1.5 s bound and dirty comes back None
    # (and the slow-untracked timing check adds that second timeout). On the VM
    # /tmp holds only the notebook's files. The file part of the dirty flag is
    # covered by tests/checkpoint/test_dirty_flag_file_delete.py.
    from app.platform.checkpoint import manager as manager_mod
    saved_scan = manager_mod._scan_tmp_checkpoint_files
    manager_mod._scan_tmp_checkpoint_files = lambda: []
    try:
        _run_isolated()
    finally:
        manager_mod._scan_tmp_checkpoint_files = saved_scan


def _run_isolated():
    from fastapi.testclient import TestClient
    app = _vm_app()
    client = TestClient(app)
    executor = app.state.executor
    cm = app.state.checkpoint_manager
    ss = app.state.session_state
    client.post("/reset")
    saved_ck = ss.get("checkpoint_enabled")
    saved_pkgs = list(cm._user_installed_packages)
    try:
        ss["checkpoint_enabled"] = False
        r = client.get("/vm-status")
        j = r.json()
        check("200 on a fresh VM", r.status_code == 200)
        check("every contract key present", KEYS <= set(j.keys()))
        check("python == platform.python_version()", j["python"] == platform.python_version())
        check("fresh: var_count == 0", j["var_count"] == 0)
        check("checkpoints off → dirty is None", j["dirty"] is None)

        executor.execute("secret_val = 'TOPSECRET-123'; big = list(range(100000))", "c1")
        r = client.get("/vm-status")
        j = r.json()
        check("after execute: var_count == 2", j["var_count"] == 2)
        check("var_bytes > 0", isinstance(j["var_bytes"], int) and j["var_bytes"] > 0)
        check("no variable values in the response", "TOPSECRET" not in r.text)
        check("no variable names in the response", "secret_val" not in r.text)

        ss["checkpoint_enabled"] = True
        cm._saved_fingerprint = None
        check("never saved → dirty True", client.get("/vm-status").json()["dirty"] is True)
        cm._saved_fingerprint = cm._current_fingerprint()
        check("just saved → dirty False", client.get("/vm-status").json()["dirty"] is False)
        executor.execute("x2 = 5", "c2")
        check("changed after save → dirty True", client.get("/vm-status").json()["dirty"] is True)

        cm.record_package_install("polars==1.9.0")
        j = client.get("/vm-status").json()
        check("packages lists the user install", "polars==1.9.0" in j["packages"])
        check("packages_total counts it", j["packages_total"] >= 1)
        check("no suspend yet → last_suspend_save is None", j["last_suspend_save"] is None)
        _sus = {"status": "timed_out", "reason": "suspend save budget of 45 s exceeded (during upload)",
                "duration_s": 45.1, "bytes": 0, "saved_at": "2026-01-01T00:00:00+00:00",
                "budget_s": 45.0, "committed": False}
        cm.last_suspend_save = _sus
        try:
            check("last_suspend_save passed through", client.get("/vm-status").json()["last_suspend_save"] == _sus)
        finally:
            cm.last_suspend_save = None

        orig = getattr(executor, "untracked", None)

        def _slow(cell_ids=None):
            time.sleep(3)
            return {"visible": [{"name": "a", "reason": "x"}], "internal": []}
        executor.untracked = _slow
        try:
            # Time the handler inside a running loop: a bare TestClient closes its
            # per-request loop and waits for the abandoned worker thread, which a
            # long-lived uvicorn loop never does.
            import asyncio
            import types
            from app.notebook.vm_status import vm_status

            async def _timed():
                t0 = time.monotonic()
                out = await vm_status(types.SimpleNamespace(app=app))
                return out, time.monotonic() - t0
            j, dt = asyncio.run(_timed())
        finally:
            if orig is None:
                del executor.untracked
            else:
                executor.untracked = orig
        check(f"slow untracked → None ({dt:.1f}s)", j["untracked"] is None)
        check("response still within ~2.5 s", dt < 2.5)

        executor.untracked = lambda cell_ids=None: {"visible": [{"name": "a", "reason": "x"}] * 3, "internal": []}
        try:
            check("untracked count from executor.untracked", client.get("/vm-status").json()["untracked"] == 3)
        finally:
            if orig is None:
                del executor.untracked
            else:
                executor.untracked = orig
        _check_checkpoint_user_count(client, executor, cm)
        saved_report = getattr(cm, "last_restore_report", None)
        try:
            cm.last_restore_report = None
            r = client.get("/restore-report")
            check("/restore-report default: 200, verified false, a reason",
                  r.status_code == 200 and r.json().get("verified") is False and bool(r.json().get("reason")))
            cm.last_restore_report = {"verified": True, "arch": "arm64", "python": "3.12",
                                      "packages_total": 2,
                                      "packages_failed": [{"name": "x", "reason": "no wheel"}]}
            check("/restore-report returns the manager's report", client.get("/restore-report").json() == cm.last_restore_report)
            check("/vm-status keys are unchanged", set(client.get("/vm-status").json().keys()) == KEYS)
        finally:
            cm.last_restore_report = saved_report
    finally:
        ss["checkpoint_enabled"] = saved_ck
        cm._user_installed_packages = saved_pkgs
        cm._saved_fingerprint = None
        client.post("/reset")


class _FakeS3:
    """In-memory S3 (only the calls CheckpointManager.save uses)."""
    class _NoSuchKey(Exception):
        pass

    def __init__(self):
        self.store = {}
        self.exceptions = type("_Exc", (), {"NoSuchKey": _FakeS3._NoSuchKey})

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.store[Key] = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.store:
            raise self._NoSuchKey(Key)
        return {"Body": io.BytesIO(self.store[Key])}

    def delete_object(self, Bucket, Key):
        self.store.pop(Key, None)


def _check_checkpoint_user_count(client, executor, cm):
    """metadata.json user_variables_count/bytes == /vm-status var_count/var_bytes
    for a namespace with modules, helpers, _-names and user variables."""
    import json
    from app.platform.checkpoint import manager as manager_mod
    client.post("/reset")
    executor.execute(
        "import json, os\n"
        "from collections import OrderedDict\n"
        "def helper(x):\n    return x\n"
        "class Thing:\n    pass\n"
        "_private = 42\n"
        "__dunder__ = 1\n"
        "rows = [1, 2, 3]\n"
        "name = 'alice'\n"
        "obj = Thing()\n", "c-mix")
    j = client.get("/vm-status").json()
    s3 = _FakeS3()
    saved = (manager_mod.get_checkpoint_s3_client, manager_mod._scan_tmp_checkpoint_files,
             cm._bucket, cm._tmp_files_signature)
    manager_mod.get_checkpoint_s3_client = lambda: s3
    manager_mod._scan_tmp_checkpoint_files = lambda: []
    cm._bucket = "fake-bucket"
    cm._tmp_files_signature = lambda: ()
    try:
        cm.save("sess-uv")
    finally:
        (manager_mod.get_checkpoint_s3_client, manager_mod._scan_tmp_checkpoint_files,
         cm._bucket, cm._tmp_files_signature) = saved
    meta = json.loads(s3.store["sessions/sess-uv/metadata.json"].decode("utf-8"))
    check("mixed namespace: /vm-status var_count == 3 (rows, name, obj)", j["var_count"] == 3)
    check("metadata user_variables_count == /vm-status var_count",
          meta.get("user_variables_count") == j["var_count"])
    check("metadata user_variables_bytes == /vm-status var_bytes",
          meta.get("user_variables_bytes") == j["var_bytes"])
    check("metadata variables_count kept (whole saved namespace, > user count)",
          isinstance(meta.get("variables_count"), int) and meta["variables_count"] > j["var_count"])
    ls = client.get("/vm-status").json()["last_save"] or {}
    check("/vm-status last_save.user_vars == var_count", ls.get("user_vars") == j["var_count"])
    check("/vm-status last_save has total_kb == metadata checkpoint_total_kb",
          "total_kb" in ls and ls.get("total_kb") == meta.get("checkpoint_total_kb"))


def _has_duckdb():
    try:
        import duckdb  # noqa: F401
        return True
    except ImportError:
        return False


_SKIP_MSG = ("SKIP  duckdb not installed (app.server imports it): "
             "uv pip install --python .venv/bin/python -r tests/requirements-dev.txt")


def test_vm_status_endpoint():
    """pytest entry point."""
    if not _has_duckdb():
        import pytest
        pytest.skip(_SKIP_MSG)
    _checks.clear()
    try:
        _run()
    finally:
        logging.disable(logging.NOTSET)
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    if not _has_duckdb():
        print(_SKIP_MSG)
        sys.exit(0)
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
