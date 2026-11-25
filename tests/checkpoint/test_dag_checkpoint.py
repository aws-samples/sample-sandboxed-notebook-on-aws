#!/usr/bin/env python3
# test-kind: offline
"""
Offline test of the checkpoint "dag" step (design §6, §12 test_dag_checkpoint).

Drives the REAL CheckpointManager.save()/restore() against an in-memory fake S3
(FakeS3 pattern from test_delta_s3_roundtrip.py; no AWS, no MicroVM). /tmp file
scanning is neutralised so only the namespace + tail steps run.

Proves:
  • save writes dag.json and NOT provenance.json; dag.json.checkpoint.id ==
    manifest.json checkpoint_id; dag.json carries the DagStore doc + runtime.
  • a failing dag.json PUT raises, metadata.json is not written and the dirty
    fingerprint is not advanced.
  • restore from dag.json → set_runtime_section (vars, clock, runs) and DagStore
    origin 'restored', runtime_source 'dag.json', consistent True.
  • id mismatch / manifest without checkpoint_id → consistent False, vars +
    clock loaded, runs == {}.
  • only provenance.json present → set_var_provenance fallback,
    runtime_source 'provenance.json'; nothing present → (None, None).
  • a thread holding the real executor _exec_lock: save returns in ~3 s with
    manifest.json + dag.json and runtime.snapshot == 'unlocked'.
  • runtime_section and runtime_section_unlocked both raising → snapshot 'failed'.
  • hook_terminate dispatches save through asyncio.to_thread.

Run (system python3 — needs dill):
    python3 tests/checkpoint/test_dag_checkpoint.py
"""
import asyncio
import io
import json
import logging
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.platform import checkpoint as ckpt  # noqa: E402
from app.platform.checkpoint import manager as manager_mod  # noqa: E402
from app.notebook.dag_store import DagStore  # noqa: E402
from app.notebook.executor import RuntimeSectionBusy, SandboxExecutor  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


# ── in-memory fake S3 client (only the calls checkpoint.py uses) ─────────────
class _NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self, fail_suffix=None):
        self.store = {}
        self.put_keys = []
        self.fail_suffix = fail_suffix

        class _Exc:
            NoSuchKey = _NoSuchKey
        self.exceptions = _Exc()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if self.fail_suffix and Key.endswith(self.fail_suffix):
            raise RuntimeError(f"simulated S3 failure for {Key}")
        b = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()
        self.store[Key] = bytes(b)
        self.put_keys.append(Key)

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.store[Key])}

    def delete_object(self, Bucket, Key):
        self.store.pop(Key, None)


class _Patched:
    """Route the manager's shared S3 client to `s3` and skip /tmp scanning."""

    def __init__(self, s3):
        self.s3 = s3

    def __enter__(self):
        self._client = manager_mod.get_checkpoint_s3_client
        self._scan = manager_mod._scan_tmp_checkpoint_files
        manager_mod.get_checkpoint_s3_client = lambda: self.s3
        manager_mod._scan_tmp_checkpoint_files = lambda: []
        return self

    def __exit__(self, *exc):
        manager_mod.get_checkpoint_s3_client = self._client
        manager_mod._scan_tmp_checkpoint_files = self._scan
        return False


def _manager(executor, dag_store=None):
    mgr = ckpt.CheckpointManager(executor, {"microvm_id": "vm-test"}, dag_store=dag_store)
    mgr._bucket = "fake-bucket"
    mgr._tmp_files_signature = lambda: ()
    return mgr


def _json(s3, key):
    return json.loads(s3.store[key].decode("utf-8"))


_EXECUTOR = None


def _real_executor():
    """One real SandboxExecutor (construction preloads packages), user namespace
    trimmed to a few small vars so saves stay fast."""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = SandboxExecutor()
    ex = _EXECUTOR
    ex.reset()
    return ex


def _seed(ex):
    ex.execute("a = 1", "c1")
    ex.execute("b = [a]", "c2")
    ex.execute("b.append(2)", "c3")


PREFIX = "sessions/sess-1"
PUSHED = {"schema": 1, "version": 4, "notebook_id": 14, "trigger": "execute",
          "nodes": [{"cell_id": "c1"}, {"cell_id": "c2"}], "edges": [], "issues": []}


def _run():
    # ── SAVE ────────────────────────────────────────────────────────────────
    print("\nSAVE writes dag.json (no provenance.json)")
    ex = _real_executor()
    _seed(ex)
    store = DagStore()
    store.put(PUSHED)
    s3 = FakeS3()
    mgr = _manager(ex, store)
    with _Patched(s3):
        mgr.save("sess-1")
    keys = set(s3.store)
    check("dag.json written", f"{PREFIX}/dag.json" in keys)
    check("provenance.json NOT written", f"{PREFIX}/provenance.json" not in keys)
    check("metadata.json written", f"{PREFIX}/metadata.json" in keys)
    man = _json(s3, f"{PREFIX}/manifest.json")
    dag = _json(s3, f"{PREFIX}/dag.json")
    check("manifest.json carries checkpoint_id", isinstance(man.get("checkpoint_id"), str)
          and len(man["checkpoint_id"]) == 32)
    check("dag.json.checkpoint.id == manifest checkpoint_id", dag["checkpoint"]["id"] == man["checkpoint_id"])
    check("dag.json.checkpoint has saved_at/session_id/microvm_id",
          dag["checkpoint"]["session_id"] == "sess-1" and dag["checkpoint"]["microvm_id"] == "vm-test"
          and bool(dag["checkpoint"]["saved_at"]))
    check("dag.json carries the DagStore doc", dag["version"] == 4 and dag["nodes"] == PUSHED["nodes"]
          and dag["notebook_id"] == 14)
    rt = dag["runtime"]
    check("dag.json runtime: locked snapshot with clock/vars/runs",
          rt.get("snapshot") == "locked" and rt["clock"] == ex._prov_clock
          and set(rt["vars"]) >= {"a", "b"} and set(rt["runs"]) == {"c1", "c2", "c3"})
    check("step 'dag' in timings, 'provenance' gone",
          "dag" in mgr.last_save_timings and "provenance" not in mgr.last_save_timings)
    check("metadata save_timings_ms lists dag",
          "dag" in _json(s3, f"{PREFIX}/metadata.json")["save_timings_ms"])
    first_id = man["checkpoint_id"]
    with _Patched(s3):
        mgr.save("sess-1")
    man2 = _json(s3, f"{PREFIX}/manifest.json")
    check("second save: new checkpoint id, manifest/dag ids still match", man2["checkpoint_id"] != first_id
          and _json(s3, f"{PREFIX}/dag.json")["checkpoint"]["id"] == man2["checkpoint_id"])

    print("\nSAVE after PUT + PATCH (delta): dag.json is the full applied document")
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dag")))
    from test_vm_dag_endpoint import _stamped_pair
    from app.notebook.dag_contract import diff_docs
    vm_prev, vm_next, full_prev, full_next = _stamped_pair()
    pstore = DagStore()
    pstore.put(vm_prev)
    vdelta = {k: v for k, v in diff_docs(full_prev, full_next).items() if k not in ("runtime", "pending")}
    status, _body = pstore.apply_delta(vdelta)
    s3p = FakeS3()
    with _Patched(s3p):
        _manager(ex, pstore).save("sess-1")
    dp = _json(s3p, f"{PREFIX}/dag.json")
    check("DagStore.apply_delta → 200", status == 200)
    check("dag.json nodes/edges/issues == the applied full doc",
          dp["nodes"] == vm_next["nodes"] and dp["edges"] == vm_next["edges"]
          and dp["issues"] == vm_next["issues"])
    check("dag.json carries the applied rev and graph_hash",
          (dp["version"], dp["runtime_version"], dp["graph_hash"])
          == (vm_next["version"], vm_next["runtime_version"], vm_next["graph_hash"]))

    print("\nSAVE without a DagStore doc")
    s3e = FakeS3()
    with _Patched(s3e):
        _manager(ex, DagStore()).save("sess-1")
    d = _json(s3e, f"{PREFIX}/dag.json")
    check("empty doc fields when nothing was pushed",
          d["schema"] == 1 and d["version"] == 0 and d["nodes"] == [] and d["edges"] == []
          and d["issues"] == [] and "runtime" in d and "checkpoint" in d)
    s3n = FakeS3()
    with _Patched(s3n):
        _manager(ex, None).save("sess-1")
    check("dag_store=None still writes dag.json", f"{PREFIX}/dag.json" in s3n.store)

    print("\nSAVE: dag.json PUT failure")
    s3f = FakeS3(fail_suffix="/dag.json")
    mgr_f = _manager(ex, store)
    raised = False
    with _Patched(s3f):
        try:
            mgr_f.save("sess-1")
        except Exception:
            raised = True
    check("save() raises when dag.json PUT fails", raised)
    check("metadata.json not written", f"{PREFIX}/metadata.json" not in s3f.store)
    check("fingerprint not advanced (still dirty)", mgr_f._saved_fingerprint is None and mgr_f.is_dirty())
    # A previously-clean manager stays dirty after a failed save.
    s3ok = FakeS3()
    mgr_c = _manager(ex, store)
    with _Patched(s3ok):
        mgr_c.save("sess-1")
    fp_clean = mgr_c._saved_fingerprint
    ex.execute("a = 5", "c1")
    s3ok.fail_suffix = "/dag.json"
    s3ok.store.pop(f"{PREFIX}/metadata.json", None)
    with _Patched(s3ok):
        try:
            mgr_c.save("sess-1")
        except Exception:
            pass
    check("failed re-save keeps the previous fingerprint and is_dirty()",
          mgr_c._saved_fingerprint == fp_clean and mgr_c.is_dirty())

    # ── RESTORE ─────────────────────────────────────────────────────────────
    print("\nRESTORE from dag.json (consistent)")
    ex = _real_executor()
    _seed(ex)
    saved_section = ex.runtime_section()
    s3 = FakeS3()
    with _Patched(s3):
        _manager(ex, store).save("sess-1")
    ex.reset()
    store2 = DagStore()
    with _Patched(s3):
        _manager(ex, store2).restore("sess-1")
    got = ex.runtime_section()
    check("runtime restored via set_runtime_section (vars, clock, runs)",
          got["vars"] == saved_section["vars"] and got["clock"] == saved_section["clock"]
          and got["runs"] == saved_section["runs"])
    rep = store2.report()
    check("DagStore origin 'restored', restored_from 'sess-1'",
          rep["origin"] == "restored" and rep["restored_from"] == "sess-1")
    check("runtime_source 'dag.json', consistent True",
          rep["runtime_source"] == "dag.json" and rep["consistent"] is True)
    check("restored doc is dag.json minus runtime", rep["doc"]["version"] == 4
          and "runtime" not in rep["doc"] and rep["doc"]["nodes"] == PUSHED["nodes"])

    print("\nRESTORE: id mismatch → inconsistent")
    man = _json(s3, f"{PREFIX}/manifest.json")
    man["checkpoint_id"] = "f" * 32
    s3.store[f"{PREFIX}/manifest.json"] = json.dumps(man).encode()
    ex.reset()
    store3 = DagStore()
    with _Patched(s3):
        _manager(ex, store3).restore("sess-1")
    got = ex.runtime_section()
    check("vars + clock loaded", got["vars"] == saved_section["vars"] and got["clock"] == saved_section["clock"])
    check("run ledger dropped (runs == {})", got["runs"] == {})
    rep = store3.report()
    check("consistent False, runtime_source 'dag.json'",
          rep["consistent"] is False and rep["runtime_source"] == "dag.json" and rep["origin"] == "restored")
    man.pop("checkpoint_id")
    s3.store[f"{PREFIX}/manifest.json"] = json.dumps(man).encode()
    ex.reset()
    store3b = DagStore()
    with _Patched(s3):
        _manager(ex, store3b).restore("sess-1")
    check("manifest without checkpoint_id → consistent False, runs == {}",
          store3b.report()["consistent"] is False and ex.runtime_section()["runs"] == {})

    print("\nRESTORE: provenance.json fallback")
    s3p = FakeS3()
    prov = {"p": {"defined_by": "c7", "defined_at": 3, "modified_by": [], "last_cell": "c7", "last_exec": 9}}
    s3p.store[f"{PREFIX}/provenance.json"] = json.dumps(prov).encode()
    ex.reset()
    store4 = DagStore()
    with _Patched(s3p):
        _manager(ex, store4).restore("sess-1")
    got = ex.runtime_section()
    check("provenance.json loaded via set_var_provenance (clock = max last_exec)",
          got["vars"] == prov and got["clock"] == 9 and got["runs"] == {})
    rep = store4.report()
    check("DagStore: restored, doc None, runtime_source 'provenance.json', consistent None",
          rep["origin"] == "restored" and rep["doc"] is None
          and rep["runtime_source"] == "provenance.json" and rep["consistent"] is None)
    s3p.store[f"{PREFIX}/dag.json"] = b"{not json"
    ex.reset()
    store4b = DagStore()
    with _Patched(s3p):
        _manager(ex, store4b).restore("sess-1")
    check("unparseable dag.json → provenance.json fallback",
          store4b.report()["runtime_source"] == "provenance.json" and ex.runtime_section()["vars"] == prov)

    print("\nRESTORE: nothing present")
    ex.reset()
    store5 = DagStore()
    with _Patched(FakeS3()):
        _manager(ex, store5).restore("sess-1")
    rep = store5.report()
    check("set_restored(None, sid, None, None)", rep["origin"] == "restored" and rep["doc"] is None
          and rep["runtime_source"] is None and rep["consistent"] is None)
    try:
        with _Patched(FakeS3()):
            _manager(ex, None).restore("sess-1")
        no_raise = True
    except Exception:
        no_raise = False
    check("dag_store=None restore does not raise", no_raise)

    # ── HELD LOCK / FAILED SNAPSHOT ─────────────────────────────────────────
    print("\nSAVE while another thread holds _exec_lock")
    ex = _real_executor()
    _seed(ex)
    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with ex._exec_lock:
            holding.set()
            release.wait(20)

    th = threading.Thread(target=_hold)
    th.start()
    holding.wait(5)
    s3h = FakeS3()
    t0 = time.monotonic()
    try:
        with _Patched(s3h):
            _manager(ex, store).save("sess-1")
        save_err = None
    except Exception as e:
        save_err = e
    elapsed = time.monotonic() - t0
    release.set()
    th.join()
    check(f"save returns within ~3 s ({elapsed:.1f}s)", save_err is None and elapsed < 3.5)
    check("manifest.json + dag.json written", f"{PREFIX}/manifest.json" in s3h.store
          and f"{PREFIX}/dag.json" in s3h.store)
    dag = _json(s3h, f"{PREFIX}/dag.json")
    check("runtime.snapshot == 'unlocked' with the live vars",
          dag["runtime"]["snapshot"] == "unlocked" and set(dag["runtime"]["vars"]) >= {"a", "b"})

    print("\nSAVE with a failing runtime snapshot")

    class _BrokenExecutor:
        def __init__(self):
            self._namespace = {"z": 1}
            self._prov_clock = 3
            self.unlocked_calls = 0

        def runtime_section(self, lock_timeout=None):
            raise RuntimeSectionBusy("held")

        def runtime_section_unlocked(self):
            self.unlocked_calls += 1
            raise RuntimeError("dictionary changed size during iteration")

        def get_var_provenance(self):
            return {"z": {"defined_by": "c1", "last_exec": 3}}

        def get_imports(self):
            return {}

        def get_stats(self):
            return {"execution_count": 0}

    bx = _BrokenExecutor()
    s3b = FakeS3()
    try:
        with _Patched(s3b):
            _manager(bx, None).save("sess-1")
        ok = True
    except Exception as e:
        print(f"    save raised: {e}")
        ok = False
    dag = _json(s3b, f"{PREFIX}/dag.json") if ok else {}
    rt = dag.get("runtime", {})
    check("save succeeds with snapshot 'failed'", ok and rt.get("snapshot") == "failed")
    check("3 unlocked attempts made", bx.unlocked_calls == 3)
    check("failed runtime: clock 0, vars from get_var_provenance, runs {}",
          rt.get("clock") == 0 and rt.get("vars") == {"z": {"defined_by": "c1", "last_exec": 3}}
          and rt.get("runs") == {})
    check("values still written (manifest.json + metadata.json)",
          f"{PREFIX}/manifest.json" in s3b.store and f"{PREFIX}/metadata.json" in s3b.store)

    class _ValueErrorExecutor(_BrokenExecutor):
        def runtime_section(self, lock_timeout=None):
            raise ValueError("copy failed")

        def get_var_provenance(self):
            raise RuntimeError("also broken")

    s3v = FakeS3()
    with _Patched(s3v):
        _manager(_ValueErrorExecutor(), None).save("sess-1")
    rt = _json(s3v, f"{PREFIX}/dag.json")["runtime"]
    check("other exception → 'failed' with vars {} when provenance also raises",
          rt["snapshot"] == "failed" and rt["vars"] == {})

    class _LegacyExecutor:
        _namespace = {}
        _prov_clock = 0

        def get_var_provenance(self):
            return {"q": {"last_exec": 1}}

        def get_imports(self):
            return {}

        def get_stats(self):
            return {"execution_count": 0}

    s3l = FakeS3()
    with _Patched(s3l):
        _manager(_LegacyExecutor(), None).save("sess-1")
    rt = _json(s3l, f"{PREFIX}/dag.json")["runtime"]
    check("executor without runtime_section → provenance vars, 'locked'",
          rt == {"clock": 0, "vars": {"q": {"last_exec": 1}}, "runs": {}, "snapshot": "locked"})

    # ── TERMINATE HOOK ──────────────────────────────────────────────────────
    print("\nhook_terminate dispatches save through asyncio.to_thread")
    from app.platform import hooks

    calls = []

    class _Mgr:
        bucket = "fake-bucket"
        last_save_summary = {}
        saved = []

        def is_dirty(self):
            return True

        def save(self, sid):
            self.saved.append((sid, threading.current_thread() is threading.main_thread()))

    mgr_t = _Mgr()
    state = types.SimpleNamespace(
        executor=types.SimpleNamespace(get_stats=lambda: {"execution_count": 1}),
        session_state={"suspend_count": 0, "resume_count": 0, "checkpoint_enabled": True,
                       "session_id": "sess-t"},
        checkpoint_manager=mgr_t,
    )
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
    real_asyncio = hooks.asyncio

    async def _recording_to_thread(fn, *args, **kwargs):
        calls.append((fn, args))
        return await real_asyncio.to_thread(fn, *args, **kwargs)

    hooks.asyncio = types.SimpleNamespace(**{k: getattr(real_asyncio, k) for k in dir(real_asyncio)
                                             if not k.startswith("__")})
    hooks.asyncio.to_thread = _recording_to_thread
    try:
        out = asyncio.run(hooks.hook_terminate(request))
    finally:
        hooks.asyncio = real_asyncio
    check("hook_terminate returns terminated", out == {"status": "terminated"})
    check("save dispatched via asyncio.to_thread with the session id",
          len(calls) == 1 and calls[0][0] == mgr_t.save and calls[0][1] == ("sess-t",))
    check("save ran off the main thread", mgr_t.saved == [("sess-t", False)])


def test_dag_checkpoint():
    """pytest entry point."""
    _checks.clear()
    _run()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
