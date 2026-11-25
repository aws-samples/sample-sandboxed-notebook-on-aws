#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the big-save failure (.agents/tasks/big-save-failure).

Root cause: the namespace save held three copies of a large numpy array (the
live array, the bytes copy numpy makes for pickling, and the BytesIO buffer of
dill.dumps), so a 1.2 GiB array on the 1024 tier pushed the notebook server
into the out-of-memory killer (exit code 137) and nothing was saved.

  1. peak memory: a large array is streamed to a spool file, so the save adds
     about 1x the array (tracemalloc), not 2x; the restore returns it intact;
  2. a variable whose pickling raises MemoryError is skipped and recorded as
     not saved (kind "memory") in the manifest and metadata; the rest is saved;
  3. the pre-flight check skips a variable that cannot fit in MemAvailable or
     on the spool disk, with the needed and available MiB in the reason;
  4. a save that fails part-way leaves the previous checkpoint loadable: the
     committed manifest and the objects it references are not overwritten;
  5. spool files are deleted after a save and after a failed one.

In-memory/on-disk fake S3; no AWS.
Run:
    .venv/bin/python tests/checkpoint/test_streaming_save.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import tracemalloc
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))  # import-time only
import numpy as np  # noqa: E402
from app.platform import checkpoint as ckpt  # noqa: E402

MiB = 1024 * 1024
PREFIX = "sessions/sess-stream"
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {str(detail)[:300]}" if not ok and detail else ""))


class _NoSuchKey(Exception):
    pass


class FakeS3:
    """Objects live as files under a temp dir, so storing an uploaded spool file
    (upload_file) costs no Python memory and does not distort tracemalloc."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="fake_s3_")
        self.put_calls = []
        self.fail_on = None   # callable(key) -> bool: raise on that PUT

        class _Exc:
            NoSuchKey = _NoSuchKey
        self.exceptions = _Exc()

    def _path(self, key):
        return os.path.join(self.dir, key.replace("/", "__"))

    def _maybe_fail(self, key):
        if self.fail_on and self.fail_on(key):
            raise RuntimeError(f"simulated S3 failure on {key}")

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._maybe_fail(Key)
        b = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()
        with open(self._path(Key), "wb") as f:
            f.write(b)
        self.put_calls.append(Key)

    def upload_file(self, Filename, Bucket, Key, Config=None):
        self._maybe_fail(Key)
        shutil.copyfile(Filename, self._path(Key))
        self.put_calls.append(Key)

    def get_object(self, Bucket, Key):
        p = self._path(Key)
        if not os.path.exists(p):
            raise _NoSuchKey(Key)
        with open(p, "rb") as f:
            return {"Body": io.BytesIO(f.read())}

    def delete_object(self, Bucket, Key):
        try:
            os.unlink(self._path(Key))
        except OSError:
            pass

    def has(self, key):
        return os.path.exists(self._path(key))

    def read(self, key):
        with open(self._path(key), "rb") as f:
            return f.read()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class FakeExecutor:
    def __init__(self, ns=None):
        self._namespace, self._prov_clock = dict(ns or {}), 0

    def get_var_provenance(self):
        return {}

    def set_var_provenance(self, d):
        pass

    def get_imports(self):
        return {}

    def set_imports(self, d):
        pass

    def get_stats(self):
        return {"execution_count": 0}


SPOOL = tempfile.mkdtemp(prefix="ckpt_spool_")
os.environ["CHECKPOINT_SPOOL_DIR"] = SPOOL


def new_mgr(ns=None):
    m = ckpt.CheckpointManager(FakeExecutor(ns), {"microvm_id": "vm-test"})
    m._bucket = "fake-bucket"
    return m


def restore(s3):
    mgr = new_mgr()
    man = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
    meta = man["vars"]
    small = [n for n, m in meta.items() if m.get("object") == "vars/__small__.pkl"]
    return mgr._restore_namespace_delta(s3, PREFIX, meta, small, {}, {}), man


def spool_files():
    return [f for f in os.listdir(SPOOL)] if os.path.isdir(SPOOL) else []


def test_peak_memory():
    print("\n# 1. peak memory of a large-array save")
    arr = np.random.default_rng(7).random(48 * MiB // 8)   # 48 MiB, incompressible
    s3 = FakeS3()
    try:
        mgr = new_mgr()
        mgr._mem_available = lambda: None
        tracemalloc.start()
        tracemalloc.reset_peak()
        base, _ = tracemalloc.get_traced_memory()
        mgr._save_namespace_delta(s3, PREFIX, {"big": arr, "marker": 1}, {}, {})
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        factor = (peak - base) / arr.nbytes
        print(f"    save peak over the live array: {factor:.2f}x of {arr.nbytes / MiB:.0f} MiB")
        # Measured 1.00x (one pickling copy of the buffer). dill.dumps measured
        # 2.0x on the VM and about 2.1x here (copy + BytesIO), plus the fake's copy.
        check("save adds at most 1.3x the array (was 2x or more)", factor <= 1.3, f"{factor:.2f}x")
        ns, man = restore(s3)
        check("restore returns the array intact", np.array_equal(ns.get("big"), arr))
        check("restore returns the small var", ns.get("marker") == 1, ns)
        check("large var went through the managed upload of the spool file",
              f"{PREFIX}/{man['vars']['big']['object']}" in s3.put_calls, s3.put_calls)
        check("spool files deleted after the save", spool_files() == [], spool_files())
    finally:
        s3.cleanup()


class RaisesMemoryError:
    def __reduce__(self):
        raise MemoryError()


class RaisesTypeError:
    def __reduce__(self):
        raise TypeError("cannot pickle this object")


def test_memory_error_skips_one_var_delta():
    print("\n# 2. MemoryError while pickling skips that variable only")
    s3 = FakeS3()
    try:
        ns = {"ok_big": np.arange(200_000, dtype=np.float64), "a": 1, "oom": RaisesMemoryError()}
        mgr = new_mgr(ns)
        mgr._mem_available = lambda: None
        mgr._save_namespace_delta(s3, PREFIX, ns, {}, {})
        man = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        check("the other vars are saved", set(man["vars"]) == {"ok_big", "a"}, man["vars"].keys())
        ns_saved = man.get("not_saved") or {}
        check("manifest records 'oom' as not saved, kind memory",
              ns_saved.get("oom", {}).get("kind") == "memory", ns_saved)
        check("reason says not enough memory",
              str(ns_saved.get("oom", {}).get("reason", "")).startswith("not enough memory to save 'oom'"), ns_saved)
        check("last_not_saved lists it", [i["name"] for i in mgr.last_not_saved] == ["oom"], mgr.last_not_saved)
        restored, _ = restore(s3)
        check("restore returns the saved vars", restored.get("a") == 1
              and np.array_equal(restored.get("ok_big"), ns["ok_big"]), list(restored))
        check("spool files deleted", spool_files() == [], spool_files())
    finally:
        s3.cleanup()


def test_preflight():
    print("\n# 3. pre-flight: not enough memory / disk")
    arr = np.ones(16 * MiB // 8)   # 16 MiB
    s3 = FakeS3()
    try:
        mgr = new_mgr()
        # 200 MiB available: 16 MiB + 256 MiB headroom does not fit.
        mgr._mem_available = lambda: 200 * MiB
        mgr._save_namespace_delta(s3, PREFIX, {"big": arr, "a": 1}, {}, {})
        man = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        reason = (man.get("not_saved") or {}).get("big", {}).get("reason", "")
        check("big skipped for memory", (man.get("not_saved") or {}).get("big", {}).get("kind") == "memory", man)
        check("reason names the needed and available MiB",
              reason == "not enough memory to save 'big' (needs about 16 MiB, 0 MiB available)", reason)
        check("small var still saved", "a" in man["vars"] and "big" not in man["vars"], man["vars"])
        # Enough memory, too little disk.
        mgr2 = new_mgr()
        mgr2._mem_available = lambda: 8 * 1024 * MiB
        mgr2._disk_free = lambda path: 70 * MiB
        mgr2._save_namespace_delta(s3, PREFIX, {"big": arr, "a": 2}, {}, {})
        man = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        item = (man.get("not_saved") or {}).get("big", {})
        check("big skipped for disk", item.get("kind") == "disk", man)
        check("disk reason names the spool dir", "not enough disk space to save 'big'" in item.get("reason", "")
              and SPOOL in item.get("reason", ""), item)
        # Enough of both: saved.
        mgr3 = new_mgr()
        mgr3._mem_available = lambda: 8 * 1024 * MiB
        mgr3._disk_free = lambda path: 8 * 1024 * MiB
        mgr3._save_namespace_delta(s3, PREFIX, {"big": arr, "a": 3}, {}, {})
        man = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        check("big saved when it fits", "big" in man["vars"] and not man.get("not_saved"), man)
        # A notebook function needs about the namespace (dill pickles its globals).
        est = ckpt.delta.estimate_value_bytes
        check("estimate: numpy nbytes", est(arr) == arr.nbytes)
        check("estimate: dict of arrays sums them", est({"x": arr, "y": arr[:10]}) >= arr.nbytes)
    finally:
        s3.cleanup()


def test_previous_checkpoint_untouched():
    print("\n# 4. a failed save leaves the previous checkpoint loadable")
    a1 = np.full(100_000, 1.0)
    a2 = np.full(100_000, 2.0)
    for label, fail in (("large upload fails", lambda k: "/vars/big" in k),
                        ("manifest write fails", lambda k: k.endswith("/manifest.json"))):
        s3 = FakeS3()
        try:
            mgr = new_mgr()
            mgr._save_namespace_delta(s3, PREFIX, {"big": a1, "gone": np.zeros(20_000), "a": 1}, {}, {})
            man1 = s3.read(f"{PREFIX}/manifest.json")
            objs1 = {k: s3.read(f"{PREFIX}/{m['object']}") for k, m in json.loads(man1)["vars"].items()
                     if m["object"] != "vars/__small__.pkl"}
            s3.fail_on = fail
            raised = False
            try:
                mgr._save_namespace_delta(s3, PREFIX, {"big": a2, "a": 2}, {}, {})
            except RuntimeError:
                raised = True
            s3.fail_on = None
            check(f"{label}: the save raises", raised)
            check(f"{label}: manifest.json unchanged", s3.read(f"{PREFIX}/manifest.json") == man1)
            check(f"{label}: every large object of the previous checkpoint unchanged",
                  all(s3.has(f"{PREFIX}/{m['object']}") and s3.read(f"{PREFIX}/{m['object']}") == objs1[k]
                      for k, m in json.loads(man1)["vars"].items() if m["object"] != "vars/__small__.pkl"))
            ns, _ = restore(s3)
            check(f"{label}: restore returns the previous big", np.array_equal(ns.get("big"), a1))
            check(f"{label}: removed var still restorable", "gone" in ns, list(ns))
            if label == "large upload fails":
                check(f"{label}: small batch not rewritten", ns.get("a") == 1, ns.get("a"))
            check(f"{label}: spool files deleted", spool_files() == [], spool_files())
            # The next successful save commits and cleans up the old objects.
            mgr2 = new_mgr()
            mgr2._save_namespace_delta(s3, PREFIX, {"big": a2, "a": 2}, {}, {})
            man2 = json.loads(s3.read(f"{PREFIX}/manifest.json"))
            ns, _ = restore(s3)
            check(f"{label}: next save commits the new values",
                  np.array_equal(ns.get("big"), a2) and ns.get("a") == 2 and "gone" not in ns, list(ns))
            old_keys = {m["object"] for m in json.loads(man1)["vars"].values()} - {"vars/__small__.pkl"}
            new_keys = {m["object"] for m in man2["vars"].values()}
            check(f"{label}: objects the new manifest does not reference are deleted",
                  not any(s3.has(f"{PREFIX}/{k}") for k in old_keys - new_keys), old_keys - new_keys)
        finally:
            s3.cleanup()


def test_skip_keeps_previous_copy():
    print("\n# 6. a var skipped for memory/disk keeps its previous saved copy")
    big1 = np.full(16 * MiB // 8, 1.0)   # 16 MiB: large object
    big2 = np.full(16 * MiB // 8, 2.0)
    s3 = FakeS3()
    try:
        mgr = new_mgr()
        mgr._mem_available = lambda: 8 * 1024 * MiB
        mgr._save_namespace_delta(s3, PREFIX, {"big": big1, "tiny": 5, "a": 1}, {}, {})
        man1 = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        big_obj = man1["vars"]["big"]["object"]
        old_big_bytes = s3.read(f"{PREFIX}/{big_obj}")
        # Next save: memory is tight, so the pre-flight skips the (changed) big;
        # "tiny" raises MemoryError while pickling; "a" changes, so the small
        # batch is rewritten and must still hold the kept copy of "tiny".
        mgr._mem_available = lambda: 200 * MiB
        mgr._save_namespace_delta(s3, PREFIX, {"big": big2, "tiny": RaisesMemoryError(), "a": 2}, {}, {})
        man2 = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        check("manifest still references the previous big object",
              man2["vars"].get("big") == man1["vars"]["big"], man2["vars"].get("big"))
        check("previous big object still in S3, unchanged",
              s3.has(f"{PREFIX}/{big_obj}") and s3.read(f"{PREFIX}/{big_obj}") == old_big_bytes)
        ns_saved = man2.get("not_saved") or {}
        check("big recorded as not saved, kind memory, previous kept",
              ns_saved.get("big", {}).get("kind") == "memory" and ns_saved["big"].get("kept_previous") is True,
              ns_saved)
        check("tiny recorded as not saved, previous kept", ns_saved.get("tiny", {}).get("kept_previous") is True,
              ns_saved)
        check("last_not_saved carries kept_previous",
              all(i.get("kept_previous") for i in mgr.last_not_saved), mgr.last_not_saved)
        ns, _ = restore(s3)
        check("restore returns the previous big", np.array_equal(ns.get("big"), big1))
        check("restore returns the previous tiny and the new a", ns.get("tiny") == 5 and ns.get("a") == 2,
              {k: ns.get(k) for k in ("tiny", "a")})
        # A fresh manager (manifest read from S3) carries it again on the next skip.
        mgr2 = new_mgr()
        mgr2._mem_available = lambda: 200 * MiB
        mgr2._save_namespace_delta(s3, PREFIX, {"big": big2, "a": 3}, {}, {})
        man3 = json.loads(s3.read(f"{PREFIX}/manifest.json").decode())
        check("second skip still keeps the previous big",
              man3["vars"].get("big") == man1["vars"]["big"] and s3.has(f"{PREFIX}/{big_obj}"))
        check("a var that is gone from the namespace is removed", "tiny" not in man3["vars"], man3["vars"])
        # When it fits again, the new value replaces the kept copy.
        mgr2._mem_available = lambda: 8 * 1024 * MiB
        mgr2._save_namespace_delta(s3, PREFIX, {"big": big2, "a": 3}, {}, {})
        ns, man4 = restore(s3)
        check("once it fits, the new big is saved and the old object deleted",
              np.array_equal(ns.get("big"), big2) and not man4.get("not_saved")
              and not s3.has(f"{PREFIX}/{big_obj}"), man4)
        # An unpicklable var has no copy to keep: it is dropped as before.
        s3b = FakeS3()
        try:
            m = new_mgr()
            m._mem_available = lambda: None
            m._save_namespace_delta(s3b, PREFIX, {"f": 1, "a": 1}, {}, {})
            m._save_namespace_delta(s3b, PREFIX, {"f": RaisesTypeError(), "a": 1}, {}, {})
            man = json.loads(s3b.read(f"{PREFIX}/manifest.json").decode())
            item = (man.get("not_saved") or {}).get("f", {})
            check("unpicklable var not carried", "f" not in man["vars"] and "kept_previous" not in item, man)
        finally:
            s3b.cleanup()
    finally:
        s3.cleanup()


def _run():
    for fn in (test_peak_memory, test_memory_error_skips_one_var_delta, test_preflight,
               test_previous_checkpoint_untouched, test_skip_keeps_previous_copy):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} raised {type(e).__name__}: {e}", False)


if __name__ == "__main__":
    _run()
    shutil.rmtree(SPOOL, ignore_errors=True)
    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
    sys.exit(1 if failed else 0)
