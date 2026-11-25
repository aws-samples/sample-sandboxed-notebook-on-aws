#!/usr/bin/env python3
# test-kind: offline
"""
Offline test of the time-bounded /suspend save (app/platform/hooks.py
hook_suspend + _bounded_suspend_save, CheckpointManager.save(deadline=...)).

The platform terminates the VM when the suspend hook times out or fails, so the
hook must return {"status": "suspended"} within its budget in every case, keep
the previous checkpoint, and leave the state dirty when the save did not finish.
Drives the real hook and the real CheckpointManager with a fake S3 (dict store,
multipart calls, optional per-call sleeps) and a fake request. Cases:
  1. a slow uploader (cooperative): returns within budget + grace, timed_out,
     manifest.json and its objects unchanged, multipart upload aborted, spool
     dir empty, still dirty;
  2. one blocking call longer than budget + grace: the hook returns timed_out
     anyway; the save thread later aborts without committing;
  3. an exception in serialize -> failed, reason recorded, still dirty;
  4. MemoryError -> failed, "ran out of memory";
  5. a pre-flight memory skip -> skipped with the variable named, the rest
     saved, still dirty (the next save retries);
  6. a fast save -> ok, bytes > 0, committed, clean;
  7. not dirty -> ok "no changes", no PUTs;
  8. an exception in is_dirty() -> still suspended, failed recorded;
  9. budget invariants and env overrides (lower only).
Run:
    PYTHONPATH=. .venv/bin/python tests/checkpoint/test_suspend_save_budget.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")
SPOOL = tempfile.mkdtemp(prefix="suspend_spool_")
os.environ["CHECKPOINT_SPOOL_DIR"] = SPOOL

import numpy as np  # noqa: E402

from app.platform import checkpoint as ckpt  # noqa: E402
from app.platform import hooks  # noqa: E402
from app.platform.checkpoint import manager as manager_mod  # noqa: E402

MiB = 1024 * 1024
SESSION = "sess-b"
PREFIX = f"sessions/{SESSION}"
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {str(detail)[:300]}" if not ok and detail else ""))


class _NoSuchKey(Exception):
    pass


class FakeS3:
    """Dict store with the multipart calls. part_sleep: seconds per upload_part;
    block_first_part: seconds for the first upload_part only (one blocking call)."""

    def __init__(self):
        self.store = {}
        self.put_keys = []
        self.part_sleep = 0.0
        self.block_first_part = 0.0
        self.open_uploads = {}
        self.aborted = []
        self.completed = []
        self._n = 0
        self._lock = threading.Lock()

        class _Exc:
            NoSuchKey = _NoSuchKey
        self.exceptions = _Exc()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        b = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()
        self.store[Key] = bytes(b)
        self.put_keys.append(Key)

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.store[Key])}

    def delete_object(self, Bucket, Key):
        self.store.pop(Key, None)

    def create_multipart_upload(self, Bucket, Key):
        with self._lock:
            self._n += 1
            uid = f"up-{self._n}"
            self.open_uploads[uid] = {"key": Key, "parts": {}}
        return {"UploadId": uid}

    def upload_part(self, Bucket, Key, PartNumber, UploadId, Body):
        with self._lock:
            block, self.block_first_part = self.block_first_part, 0.0
        time.sleep(block or self.part_sleep)
        with self._lock:
            up = self.open_uploads.get(UploadId)
            if up is None:
                raise RuntimeError("NoSuchUpload")
            up["parts"][PartNumber] = bytes(Body)
        return {"ETag": f'"{UploadId}-{PartNumber}"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        with self._lock:
            up = self.open_uploads.pop(UploadId)
            nums = [p["PartNumber"] for p in MultipartUpload["Parts"]]
            self.store[Key] = b"".join(up["parts"][n] for n in nums)
            self.completed.append(UploadId)
            self.put_keys.append(Key)

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        with self._lock:
            self.open_uploads.pop(UploadId, None)
            self.aborted.append(UploadId)


class FakeExecutor:
    def __init__(self):
        self._namespace, self._prov_clock = {}, 0

    def get_var_provenance(self):
        return {}

    def set_var_provenance(self, d):
        pass

    def get_imports(self):
        return {}

    def set_imports(self, d):
        pass

    def get_stats(self):
        return {"execution_count": 0, "variables_count": len(self._namespace)}

    def set(self, **kw):
        self._namespace.update(kw)
        self._prov_clock += 1


def _env(budget=None, grace=None):
    for k, v in (("SUSPEND_SAVE_BUDGET_SECONDS", budget), ("SUSPEND_SAVE_ABORT_GRACE_SECONDS", grace)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)


class Rig:
    def __init__(self):
        self.s3 = FakeS3()
        self.ex = FakeExecutor()
        self.mgr = ckpt.CheckpointManager(self.ex, {"microvm_id": "vm-test"})
        self.mgr._bucket = "fake-bucket"
        self.mgr._mem_available = lambda: None
        self.req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(
            executor=self.ex, checkpoint_manager=self.mgr,
            session_state={"suspend_count": 0, "checkpoint_enabled": True, "session_id": SESSION})))

    def suspend(self):
        # Time when the hook coroutine returns (what the platform sees). asyncio.run
        # itself then joins the worker thread at loop shutdown; the server's loop
        # never shuts down, so that wait is not part of the hook.
        async def _call():
            t0 = time.monotonic()
            r = await hooks.hook_suspend(self.req)
            return r, time.monotonic() - t0
        return asyncio.run(_call())

    def snapshot(self):
        man = self.s3.store[f"{PREFIX}/manifest.json"]
        objs = {m["object"] for m in json.loads(man)["vars"].values()}
        return man, {o: self.s3.store.get(f"{PREFIX}/{o}") for o in objs}

    def unchanged(self, snap):
        man, objs = snap
        return self.s3.store.get(f"{PREFIX}/manifest.json") == man and \
            all(self.s3.store.get(f"{PREFIX}/{o}") == b for o, b in objs.items())

    def wait_idle(self, limit=10.0):
        end = time.monotonic() + limit
        while self.mgr._save_lock.locked() and time.monotonic() < end:
            time.sleep(0.05)
        return not self.mgr._save_lock.locked()


def spool_files():
    return os.listdir(SPOOL) if os.path.isdir(SPOOL) else []


def _first_commit(rig):
    _env()
    rig.ex.set(a=1, v0=os.urandom(2 * MiB))
    res, _ = rig.suspend()
    return res == {"status": "suspended"} and not rig.mgr.is_dirty()


def case_slow_uploader():
    print("\n# 1. slow cooperative uploader")
    rig = Rig()
    check("first checkpoint committed", _first_commit(rig))
    snap = rig.snapshot()
    rig.ex.set(v1=os.urandom(4 * MiB), v2=os.urandom(4 * MiB), v3=os.urandom(4 * MiB))
    rig.s3.part_sleep = 0.4
    budget, grace = 1.0, 0.5
    _env(budget, grace)
    res, took = rig.suspend()
    rig.wait_idle()
    out = rig.mgr.last_suspend_save or {}
    print(f"    hook returned in {took:.2f}s: {out}")
    check("hook returns suspended", res == {"status": "suspended"}, res)
    check("within budget + grace + 0.5 s", took < budget + grace + 0.5, f"{took:.2f}s")
    check("status timed_out, not committed", out.get("status") == "timed_out" and out.get("committed") is False, out)
    check("reason mentions the budget", "budget" in (out.get("reason") or ""), out)
    check("manifest.json and its objects unchanged", rig.unchanged(snap))
    check("state still dirty", rig.mgr.is_dirty())
    check("spool dir empty", spool_files() == [], spool_files())
    check("no multipart upload left open, at least one aborted",
          not rig.s3.open_uploads and len(rig.s3.aborted) >= 1, (rig.s3.open_uploads, rig.s3.aborted))
    # The next suspend (no budget pressure) saves everything.
    rig.s3.part_sleep = 0.0
    _env()
    rig.suspend()
    check("next suspend retries and saves ok", (rig.mgr.last_suspend_save or {}).get("status") == "ok"
          and not rig.mgr.is_dirty())


def case_blocking_call():
    print("\n# 2. one blocking call longer than budget + grace")
    rig = Rig()
    check("first checkpoint committed", _first_commit(rig))
    snap = rig.snapshot()
    rig.ex.set(v1=os.urandom(2 * MiB))
    rig.s3.block_first_part = 2.5
    budget, grace = 1.0, 0.5
    _env(budget, grace)
    res, took = rig.suspend()
    out = rig.mgr.last_suspend_save or {}
    print(f"    hook returned in {took:.2f}s: {out}")
    check("hook returns suspended", res == {"status": "suspended"}, res)
    check("within budget + grace + 0.5 s", took < budget + grace + 0.5, f"{took:.2f}s")
    check("status timed_out", out.get("status") == "timed_out", out)
    check("save thread ends on its own", rig.wait_idle())
    check("manifest.json and its objects unchanged after the thread ends", rig.unchanged(snap))
    check("spool dir empty", spool_files() == [], spool_files())
    check("no multipart upload left open", not rig.s3.open_uploads, rig.s3.open_uploads)
    check("state still dirty", rig.mgr.is_dirty())
    _env()


def _with_build(fn, exc):
    real = manager_mod.build_var_blobs

    def boom(*a, **kw):
        raise exc
    manager_mod.build_var_blobs = boom
    try:
        return fn()
    finally:
        manager_mod.build_var_blobs = real


def case_exceptions():
    print("\n# 3/4. exception and MemoryError in serialize")
    for exc, word in ((RuntimeError("boom"), "boom"), (MemoryError(), "out of memory")):
        rig = Rig()
        check("first checkpoint committed", _first_commit(rig))
        snap = rig.snapshot()
        rig.ex.set(v1=5)
        res, _ = _with_build(rig.suspend, exc)
        out = rig.mgr.last_suspend_save or {}
        check(f"{type(exc).__name__}: hook returns suspended", res == {"status": "suspended"}, res)
        check(f"{type(exc).__name__}: status failed, reason has '{word}'",
              out.get("status") == "failed" and word in (out.get("reason") or ""), out)
        check(f"{type(exc).__name__}: previous checkpoint unchanged", rig.unchanged(snap))
        check(f"{type(exc).__name__}: still dirty", rig.mgr.is_dirty())


def case_preflight_skip():
    print("\n# 5. pre-flight memory skip")
    rig = Rig()
    rig.mgr._mem_available = lambda: 300 * MiB
    rig.ex.set(small=1, arr=np.zeros(64 * MiB // 8))
    res, _ = rig.suspend()
    out = rig.mgr.last_suspend_save or {}
    man = json.loads(rig.s3.store[f"{PREFIX}/manifest.json"])
    check("hook returns suspended", res == {"status": "suspended"}, res)
    check("status skipped, reason names arr", out.get("status") == "skipped" and "arr" in (out.get("reason") or ""), out)
    check("the rest is in the manifest", "small" in man["vars"] and "arr" not in man["vars"], man["vars"].keys())
    check("arr listed as not saved (memory)", man.get("not_saved", {}).get("arr", {}).get("kind") == "memory")
    check("state still dirty (next save retries)", rig.mgr.is_dirty())


def case_fast_and_clean():
    print("\n# 6/7. fast save, then nothing changed")
    rig = Rig()
    _env()
    rig.ex.set(a=1, big=os.urandom(3 * MiB))
    res, _ = rig.suspend()
    out = rig.mgr.last_suspend_save or {}
    check("hook returns suspended", res == {"status": "suspended"}, res)
    check("status ok, bytes > 0, committed", out.get("status") == "ok" and (out.get("bytes") or 0) > 0
          and out.get("committed") is True, out)
    check("outcome fields", {"status", "reason", "duration_s", "bytes", "saved_at", "budget_s", "committed"} <= set(out))
    check("multipart upload completed", len(rig.s3.completed) == 1 and not rig.s3.aborted)
    check("clean after an ok save", not rig.mgr.is_dirty())
    n = len(rig.s3.put_keys)
    rig.suspend()
    out = rig.mgr.last_suspend_save or {}
    check("not dirty: ok 'no changes'", out.get("status") == "ok" and "no changes" in out.get("reason", ""), out)
    check("not dirty: no PUTs", len(rig.s3.put_keys) == n)


def case_is_dirty_raises():
    print("\n# 8. exception outside the save")
    rig = Rig()

    def bad():
        raise ValueError("fingerprint broke")
    rig.mgr.is_dirty = bad
    res, _ = rig.suspend()
    out = rig.mgr.last_suspend_save or {}
    check("hook returns suspended", res == {"status": "suspended"}, res)
    check("failed recorded", out.get("status") == "failed" and "fingerprint broke" in out.get("reason", ""), out)


def case_invariants():
    print("\n# 9. budget invariants")
    b, g = hooks.SUSPEND_SAVE_BUDGET_SECONDS, hooks.SUSPEND_SAVE_ABORT_GRACE_SECONDS
    check("budget + grace < 60 s hook timeout", b + g < 60)
    check("budget at least 10 s below the 60 s timeout", 60 - b >= 10)
    _env(100, 100)
    check("env can not raise the budget", hooks._suspend_budget() == (b, g))
    _env(2, 0.5)
    check("env lowers the budget", hooks._suspend_budget() == (2.0, 0.5))
    _env("bogus", -1)
    check("invalid env -> defaults", hooks._suspend_budget() == (b, g))
    _env()


def main():
    chunk, thr, conc = (manager_mod._SPOOL_UPLOAD_CHUNK, manager_mod._SPOOL_UPLOAD_MULTIPART_THRESHOLD,
                        manager_mod._SPOOL_UPLOAD_CONCURRENCY)
    scan = manager_mod._scan_tmp_checkpoint_files
    client = manager_mod.get_checkpoint_s3_client
    manager_mod._SPOOL_UPLOAD_CHUNK = manager_mod._SPOOL_UPLOAD_MULTIPART_THRESHOLD = MiB
    manager_mod._SPOOL_UPLOAD_CONCURRENCY = 2
    manager_mod._scan_tmp_checkpoint_files = lambda: []
    current = {}
    manager_mod.get_checkpoint_s3_client = lambda: current["s3"]
    orig_init = Rig.__init__

    def init(self):
        orig_init(self)
        current["s3"] = self.s3
    Rig.__init__ = init
    try:
        case_slow_uploader()
        case_blocking_call()
        case_exceptions()
        case_preflight_skip()
        case_fast_and_clean()
        case_is_dirty_raises()
        case_invariants()
    finally:
        manager_mod._SPOOL_UPLOAD_CHUNK, manager_mod._SPOOL_UPLOAD_MULTIPART_THRESHOLD = chunk, thr
        manager_mod._SPOOL_UPLOAD_CONCURRENCY = conc
        manager_mod._scan_tmp_checkpoint_files = scan
        manager_mod.get_checkpoint_s3_client = client
        _env()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR)
    sys.exit(main())
