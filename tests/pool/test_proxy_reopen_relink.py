#!/usr/bin/env python3
# test-kind: offline
"""
Terminate -> link cleared -> reopen -> restore keeps the saved session and arch
(restore-arch-bug, decision B).

POST /terminate clears notebooks.session_id (notebook_clear_session). A reopen
used to get no session, so the UI offered only a fresh launch with a NEW session
on the default arch (ARM64) and the checkpoint was orphaned. Now
GET /notebooks/{id}, GET /notebooks and a /launch without sessionId fall back to
the notebook's LATEST vm_sessions session when its checkpoint manifest exists.

Checks (real SQLite through the storage interface, fake vm_manager / pool):
  (a) GET /notebooks/{id} and GET /notebooks fill session_id with the last
      session after the clear (response only: the notebooks row stays NULL);
  (b) the reopen's restore /launch (no arch in the body, as App.jsx sends it)
      asks the pool for x86_64:1024 although an arm64:1024 spare exists, the
      pool misses (409), and the cold launch uses the -x86_64-1024 image;
  (c) a /launch with notebookId but NO sessionId re-links + restores the same
      session on x86_64/1024;
  (d) no relink when the manifest is confirmed missing, when the notebook still
      has a session (live VM), for another notebook's session, or when pooling
      is irrelevant (checkpoint disabled);
  (e) after a fresh launch (Option B: new session), the fallback picks the NEW
      session, never the older one.
Run:
    .venv/bin/python tests/pool/test_proxy_reopen_relink.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from tests.pool._poolkit import _run_sqlite  # noqa: E402
from tests.pool.test_proxy_launch_grant import (  # noqa: E402
    EnvSandbox, _FakeAsyncClient, _FakeResponse, _FakeVmManager, _FakeRequest, run_async,
)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


_SIZES = {"arm64": [1024, 2048, 4096, 8192], "x86_64": [1024, 2048, 4096]}


def _call(route, req, *args):
    # The routes take the request (for vm_manager) since this fix; tolerate the
    # older request-less signature so the fail-before run reports assertions.
    import inspect
    if "request" in inspect.signature(route).parameters:
        return route(*args, req)
    return route(*args)


class _Vm(_FakeVmManager):
    def __init__(self, manifests):
        super().__init__()
        self.manifests = manifests  # {session_id: True/False/None}

    def checkpoint_manifest_exists(self, session_id):
        return self.manifests.get(session_id, False)


class _World:
    """Real SQLite behind every astorage the routes use; fake pool + AWS."""

    def __enter__(self):
        import proxy.storage as ps
        from proxy.storage.async_storage import AsyncStorage
        from proxy.notebook import session_relink, dag_service
        from proxy.notebook.routes import notebooks as nb_routes
        self.storage = _run_sqlite()
        a = AsyncStorage(self.storage)
        self._mods = [(ps, "storage", self.storage), (ps, "astorage", a), (microvm_mod, "astorage", a),
                      (microvm_mod, "storage", self.storage), (session_relink, "astorage", a),
                      (nb_routes, "astorage", a), (dag_service, "astorage", a),
                      (microvm_mod, "EXEC_ROLE_ARN", "arn:aws:iam::123456789012:role/exec"),
                      (microvm_mod, "IMAGE_ARN", "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"),
                      (microvm_mod, "IMAGE_ARCHES", ["arm64", "x86_64"]),
                      (microvm_mod, "_SIZES_BY_ARCH", {k: list(v) for k, v in _SIZES.items()}),
                      (httpx, "AsyncClient", _FakeAsyncClient)]
        self._saved = [(m, n, getattr(m, n)) for m, n, _ in self._mods]
        for m, n, v in self._mods:
            setattr(m, n, v)
        session_relink._manifest_cache.clear()
        self.nb_routes = nb_routes
        self.env = EnvSandbox().__enter__()
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        # Pool: only an arm64:1024 spare. A claim must match arch AND size.
        self.grants = []

        def _grant(url, json):
            self.grants.append(json)
            if (json.get("arch"), json.get("size")) == ("arm64", 1024):
                return _FakeResponse(200, {"vm_id": "pool-arm-1024", "endpoint": "pool-ep"})
            return _FakeResponse(409, {"error": "POOL_EMPTY"})
        _FakeAsyncClient.behavior = _grant
        return self

    def __exit__(self, *exc):
        for m, n, v in self._saved:
            setattr(m, n, v)
        self.env.__exit__(*exc)

    def notebook(self, name, sid, arch="x86_64", mem=1024, launched="2026-01-01T00:00:00+00:00", vm="vm-old"):
        nb = self.storage.notebook_create(name, cells=[{"id": "c1"}], session_id=sid)
        self.storage.notebook_update(nb["id"], checkpoint_enabled=1)
        self.storage.vm_session_create(vm, notebook_id=nb["id"], session_id=sid, memory_mib=mem,
                                       architecture=arch, checkpoint_enabled=True, launched_at=launched)
        self.storage.vm_session_update_state(vm, "TERMINATED")
        return nb["id"]

    def get(self, nb_id, vm):
        req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=vm)))
        return run_async(_call(self.nb_routes.notebook_get, req, nb_id))

    def listing(self, vm):
        req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=vm)))
        return {n["id"]: n for n in run_async(_call(self.nb_routes.notebook_list, req))["notebooks"]}

    def launch(self, vm, body):
        self.grants.clear()
        return run_async(microvm_mod.launch_microvm(_FakeRequest(vm, dict(body))))


def test_reopen_after_terminate_restores_x86():
    with _World() as w:
        nb_id = w.notebook("TSF", "sess-x86")
        w.storage.notebook_clear_session("sess-x86")   # what POST /terminate does
        vm = _Vm({"sess-x86": True})
        check("(a) the notebooks row really is cleared",
              (w.storage.notebook_get(nb_id) or {}).get("session_id") is None)
        got = w.get(nb_id, vm)
        check("(a) GET /notebooks/{id} returns the last session", got.get("session_id") == "sess-x86")
        check("(a) GET /notebooks lists it with that session too",
              w.listing(vm).get(nb_id, {}).get("session_id") == "sess-x86")
        check("(a) the GET did not write the row (v_nb_notebook meaning unchanged)",
              (w.storage.notebook_get(nb_id) or {}).get("session_id") is None)
        # App.jsx reopen auto-restore body: no arch, no memoryMiB.
        r = w.launch(vm, {"name": "TSF", "notebookId": str(nb_id), "sessionId": "sess-x86",
                          "restoreFromSession": "sess-x86", "checkpointEnabled": True})
        check("(b) restore grant asked for x86_64:1024 (an arm64:1024 spare was available)",
              len(w.grants) == 1 and (w.grants[0].get("arch"), w.grants[0].get("size")) == ("x86_64", 1024)
              and w.grants[0].get("restore_from") == "sess-x86")
        check("(b) pool miss -> cold launch of the -x86_64-1024 image",
              len(vm.run_vm_calls) == 1 and vm.run_vm_calls[0]["imageIdentifier"].endswith("-x86_64-1024"))
        check("(b) response: x86_64/1024 on the SAME session",
              r.get("architecture") == "x86_64" and r.get("memory_mib") == 1024 and r.get("sessionId") == "sess-x86")
        check("(b) the launch re-linked the notebooks row",
              (w.storage.notebook_get(nb_id) or {}).get("session_id") == "sess-x86")


def test_launch_without_session_id_relinks():
    with _World() as w:
        nb_id = w.notebook("TSF2", "sess-x86b")
        w.storage.notebook_clear_session("sess-x86b")
        vm = _Vm({"sess-x86b": True})
        r = w.launch(vm, {"name": "TSF2", "notebookId": str(nb_id), "arch": "arm64", "memoryMiB": 2048})
        check("(c) /launch without sessionId reuses the last session",
              r.get("sessionId") == "sess-x86b")
        check("(c) ... restores it (grant carries restore_from) on the saved x86_64:1024",
              len(w.grants) == 1 and w.grants[0].get("restore_from") == "sess-x86b"
              and (w.grants[0].get("arch"), w.grants[0].get("size")) == ("x86_64", 1024))
        check("(c) ... cold launch image -x86_64-1024 (not the requested arm64-2048)",
              len(vm.run_vm_calls) == 1 and vm.run_vm_calls[0]["imageIdentifier"].endswith("-x86_64-1024"))


def test_no_relink_cases():
    with _World() as w:
        a = w.notebook("NoManifest", "sess-nomf", vm="vm-a")
        w.storage.notebook_clear_session("sess-nomf")
        check("(d) manifest confirmed missing -> no session (as before)",
              w.get(a, _Vm({"sess-nomf": False})).get("session_id") is None)
        b = w.notebook("Live", "sess-live", vm="vm-b")
        w.storage.vm_session_create("vm-b2", notebook_id=b, session_id="sess-other-old", memory_mib=1024,
                                    architecture="x86_64", launched_at="2026-02-01T00:00:00+00:00")
        check("(d) a linked session is returned unchanged (live VM: no relink)",
              w.get(b, _Vm({"sess-other-old": True, "sess-live": True})).get("session_id") == "sess-live")
        c = w.notebook("Shared", "sess-shared", vm="vm-c")
        w.storage.notebook_clear_session("sess-shared")
        other = w.storage.notebook_create("Other", cells=[{"id": "c1"}], session_id="sess-shared")
        check("(d) never re-links a session another notebook uses",
              w.get(c, _Vm({"sess-shared": True})).get("session_id") is None)
        d = w.notebook("NoCkpt", "sess-nock", vm="vm-d")
        w.storage.notebook_update(d, checkpoint_enabled=0)
        w.storage.notebook_clear_session("sess-nock")
        check("(d) checkpoint disabled -> no relink",
              w.get(d, _Vm({"sess-nock": True})).get("session_id") is None)
        _ = other


def test_fresh_launch_then_fallback_picks_new():
    with _World() as w:
        nb_id = w.notebook("Fresh", "sess-first", vm="vm-1")
        w.storage.notebook_clear_session("sess-first")
        vm = _Vm({"sess-first": True, "sess-second": True})
        # Option B: an explicit NEW session, no restore.
        r = w.launch(vm, {"name": "Fresh", "notebookId": str(nb_id), "sessionId": "sess-second",
                          "arch": "arm64", "memoryMiB": 1024})
        check("(e) Option B keeps its NEW session (no relink to the old one)",
              r.get("sessionId") == "sess-second" and not (w.grants and w.grants[0].get("restore_from")))
        w.storage.notebook_clear_session("sess-second")
        check("(e) after that VM's terminate the fallback picks the NEW session",
              w.get(nb_id, vm).get("session_id") == "sess-second")


def test_payload_carries_saved_arch_tier():
    """(f) GET /notebooks/{id} carries session_arch / session_memory_mib: the
    arch and tier the restore is pinned to (latest vm_sessions row; from the
    image when the row lacks them), so a tab with no arch shows x86_64."""
    with _World() as w:
        nb_id = w.notebook("Tier", "sess-tier", arch="x86_64", mem=1024)
        w.storage.notebook_clear_session("sess-tier")
        vm = _Vm({"sess-tier": True})
        got = w.get(nb_id, vm)
        check("(f) relinked GET /notebooks/{id}: session_arch x86_64, session_memory_mib 1024",
              got.get("session_arch") == "x86_64" and got.get("session_memory_mib") == 1024)
        row = w.listing(vm).get(nb_id, {})
        check("(f) GET /notebooks (relinked row) carries them too",
              row.get("session_arch") == "x86_64" and row.get("session_memory_mib") == 1024)
        linked = w.notebook("Linked", "sess-linked", arch="x86_64", mem=2048, vm="vm-l")
        got = w.get(linked, vm)
        check("(f) a still-linked session gets them on GET /notebooks/{id}",
              got.get("session_arch") == "x86_64" and got.get("session_memory_mib") == 2048)
        # An older row with no arch/tier: inferred from that VM's image.
        from proxy.notebook import session_relink

        class _OldRows:
            async def vm_session_list_for_session(self, sid):
                return [{"microvm_id": "vm-o", "architecture": None, "memory_mib": None,
                         "image_arn": "arn:aws:lambda:us-west-2:123456789012:microvm-image:notebook-microvm-x86_64-4096"}]
        real = session_relink.astorage
        session_relink.astorage = _OldRows()
        try:
            inferred = run_async(session_relink.session_saved_tier("sess-oldrow"))
        finally:
            session_relink.astorage = real
        check("(f) a row without arch/tier: inferred from the image (x86_64/4096)",
              inferred == ("x86_64", 4096))
        none_nb = w.storage.notebook_create("NoSession", cells=[{"id": "c1"}])
        got = w.get(none_nb["id"], vm)
        check("(f) no session -> no session_arch key",
              "session_arch" not in got)


def test_payload_carries_checkpoint_kb():
    """(g) GET /notebooks/{id} carries session_checkpoint_kb from metadata.json
    checkpoint_total_kb; None when the metadata is missing or unreadable."""
    import json as _json
    from proxy.notebook import session_relink
    class _S3:
        def __init__(self, objs):
            self.objs = objs
        def get_object(self, Bucket, Key):
            if Key not in self.objs:
                err = Exception("nope")
                err.response = {"Error": {"Code": "NoSuchKey"}}
                raise err
            data = self.objs[Key]
            return {"Body": types.SimpleNamespace(read=lambda: data)}
    class _VmS3(_Vm):
        def __init__(self, manifests, objs):
            super().__init__(manifests)
            self._s3 = _S3(objs)
        def _get_s3_client(self):
            return self._s3
        def get_artifacts_bucket(self):
            return "bucket"
    with _World() as w:
        session_relink._ckpt_kb_cache.clear()
        nb_id = w.notebook("Kb", "sess-kb", arch="x86_64", mem=1024)
        vm = _VmS3({"sess-kb": True},
                   {"sessions/sess-kb/metadata.json": _json.dumps({"checkpoint_total_kb": 307200}).encode()})
        got = w.get(nb_id, vm)
        check("(g) session_checkpoint_kb comes from metadata checkpoint_total_kb",
              got.get("session_checkpoint_kb") == 307200)
        session_relink._ckpt_kb_cache.clear()
        got = w.get(nb_id, _VmS3({"sess-kb": True}, {}))
        check("(g) missing metadata -> session_checkpoint_kb None (key present)",
              "session_checkpoint_kb" in got and got["session_checkpoint_kb"] is None)
        session_relink._ckpt_kb_cache.clear()
        got = w.get(nb_id, _VmS3({"sess-kb": True}, {"sessions/sess-kb/metadata.json": b"not json"}))
        check("(g) unreadable metadata -> None, no error", got.get("session_checkpoint_kb") is None)
        session_relink._ckpt_kb_cache.clear()
        row = w.listing(vm).get(nb_id, {})
        check("(g) the list route does not read S3 for the size (with_tier only)",
              "session_checkpoint_kb" not in row)


def test_payload_carries_session_packages():
    """(h) GET /notebooks/{id} carries session_packages: the lines of the
    checkpoint's requirements.txt (what restore reinstalls), for the Connect
    dialog's "Reinstalling N packages (...)..." line. [] without the file, None
    when S3 fails; the list route never reads it."""
    from proxy.notebook import session_relink

    class _S3:
        def __init__(self, objs, fail=False):
            self.objs, self.fail, self.keys = objs, fail, []

        def get_object(self, Bucket, Key):
            self.keys.append(Key)
            if self.fail:
                raise RuntimeError("S3 down")
            if Key not in self.objs:
                err = Exception("nope")
                err.response = {"Error": {"Code": "NoSuchKey"}}
                raise err
            data = self.objs[Key]
            return {"Body": types.SimpleNamespace(read=lambda: data)}

    class _VmS3(_Vm):
        def __init__(self, manifests, s3):
            super().__init__(manifests)
            self._s3 = s3

        def _get_s3_client(self):
            return self._s3

        def get_artifacts_bucket(self):
            return "bucket"
    with _World() as w:
        nb_id = w.notebook("Pk", "sess-pk", arch="x86_64", mem=1024)
        reqs = b"statsmodels==0.15.0\n\n# a comment\nbottle==0.13.4\n"
        session_relink._pkg_cache.clear()
        s3 = _S3({"sessions/sess-pk/requirements.txt": reqs})
        got = w.get(nb_id, _VmS3({"sess-pk": True}, s3))
        check("(h) session_packages = requirements.txt lines in file order (blank/# dropped)",
              got.get("session_packages") == ["statsmodels==0.15.0", "bottle==0.13.4"])
        check("(h) read sessions/<sid>/requirements.txt", "sessions/sess-pk/requirements.txt" in s3.keys)
        session_relink._pkg_cache.clear()
        got = w.get(nb_id, _VmS3({"sess-pk": True}, _S3({})))
        check("(h) no requirements.txt -> [] (key present)", got.get("session_packages") == [])
        session_relink._pkg_cache.clear()
        got = w.get(nb_id, _VmS3({"sess-pk": True}, _S3({}, fail=True)))
        check("(h) S3 error -> None, no error", "session_packages" in got and got["session_packages"] is None)
        session_relink._pkg_cache.clear()
        s3 = _S3({"sessions/sess-pk/requirements.txt": reqs})
        row = w.listing(_VmS3({"sess-pk": True}, s3)).get(nb_id, {})
        check("(h) the list route does not read requirements.txt",
              "session_packages" not in row and "sessions/sess-pk/requirements.txt" not in s3.keys)


_SECTIONS = (test_reopen_after_terminate_restores_x86, test_launch_without_session_id_relinks,
             test_no_relink_cases, test_fresh_launch_then_fallback_picks_new,
             test_payload_carries_saved_arch_tier, test_payload_carries_checkpoint_kb,
             test_payload_carries_session_packages)


def main():
    print("\n=== Reopen after terminate re-links the last session ===")
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL REOPEN-RELINK CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_reopen_relink():
    _checks.clear()
    for fn in _SECTIONS:
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    main()
