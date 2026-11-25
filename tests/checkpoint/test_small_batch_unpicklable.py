#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review A-B-10 / G-B-01, plus A-B-05).

A-B-10: one SMALL variable that pickles but will not unpickle used to abort the
whole checkpoint save (the small batch was rebuilt by unpickling every blob).
Now the batch stores each var's pickle bytes (format 2) and restore loads each
one on its own, skipping a bad one with a WARNING (name + error type only).
  - save() succeeds with a bad-unpickle var (__reduce__ and __setstate__ kinds)
    plus normal vars, and writes manifest.json;
  - restore returns the normal vars and skips the bad ones with a warning;
  - an OLD-format batch (dill dict of live objects) still restores;
  - the batch re-uploads only when a small var changes (hash logic unchanged);
  - a large var that won't unpickle is skipped the same way;
  - no variable value appears in the logs.
A-B-05: a FAILED user cell that bound a variable marks the checkpoint dirty
(a scratch run still does not).
In-memory fake S3 (same pattern as test_delta_s3_roundtrip.py); no AWS.
Run:
    .venv/bin/python tests/checkpoint/test_small_batch_unpicklable.py
"""
import io
import json
import logging
import os
import sys
import types
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))  # import-time only
import dill  # noqa: E402
from app.platform import checkpoint as ckpt  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f" — {str(detail)[:300]}" if not ok and detail else ""))


class _NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self):
        self.store, self.put_calls = {}, []

        class _Exc:
            NoSuchKey = _NoSuchKey
        self.exceptions = _Exc()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        b = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()
        self.store[Key] = bytes(b)
        self.put_calls.append(Key)

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.store[Key])}

    def delete_object(self, Bucket, Key):
        self.store.pop(Key, None)


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
        return {"execution_count": 0}


SECRET_MARK = "VALUE-MUST-NOT-BE-LOGGED-42"


def _explode(*_a):
    raise RuntimeError("cannot rebuild this object")


class BadReduce:
    """Pickles fine; unpickling calls _explode, which raises."""
    def __init__(self):
        self.payload = SECRET_MARK

    def __reduce__(self):
        return (_explode, (self.payload,))


class BadSetstate:
    """Pickles fine; unpickling raises in __setstate__."""
    def __init__(self):
        self.payload = SECRET_MARK

    def __getstate__(self):
        return {"payload": self.payload}

    def __setstate__(self, state):
        raise ValueError("needs missing state")


class BadLarge:
    """A LARGE (> DELTA_SMALL_VAR_BYTES) value whose unpickle raises."""
    def __init__(self):
        self.blob = "x" * (ckpt.manager.DELTA_SMALL_VAR_BYTES * 2)

    def __reduce__(self):
        return (_explode, (self.blob,))


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, r):
        self.records.append(r)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)

    def warnings(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


cap = Capture()
logging.getLogger().addHandler(cap)
logging.getLogger().setLevel(logging.DEBUG)

PREFIX = "sessions/sess-small"


def new_mgr():
    m = ckpt.CheckpointManager(FakeExecutor(), {"microvm_id": "vm-test"})
    m._bucket = "fake-bucket"
    return m


def restore_ns(s3, mgr=None):
    mgr = mgr or new_mgr()
    man = json.loads(s3.store[f"{PREFIX}/manifest.json"].decode())
    meta = man["vars"]
    small = [n for n, m in meta.items() if m.get("object") == "vars/__small__.pkl"]
    return mgr._restore_namespace_delta(s3, PREFIX, meta, small, {}, {}), mgr


# ── 1. save with bad-unpickle small vars ──────────────────────────────────────
print("\n1. save() with small vars that pickle but won't unpickle")


def _try(f, b):
    try:
        f(b)
        return False
    except Exception:
        return True


check("precondition: BadReduce pickles but won't unpickle", _try(dill.loads, dill.dumps(BadReduce())))
check("precondition: BadSetstate pickles but won't unpickle", _try(dill.loads, dill.dumps(BadSetstate())))
s3 = FakeS3()
mgr = new_mgr()
ns = {"a": 1, "b": "hello", "rows": [1, 2, 3], "bad1": BadReduce(), "bad2": BadSetstate()}
err = None
try:
    mgr._save_namespace_delta(s3, PREFIX, dict(ns), {}, {})
except Exception as e:  # the A-B-10 failure mode
    err = e
check("save succeeds", err is None, repr(err))
check("manifest.json written (commit point)", f"{PREFIX}/manifest.json" in s3.store)
man = json.loads(s3.store.get(f"{PREFIX}/manifest.json", b"{}").decode() or "{}")
check("manifest lists the normal and the bad vars", set(man.get("vars", {})) == set(ns), man.get("vars"))
check("manifest records small_format 2", man.get("small_format") == 2)

# ── 2. restore skips the bad ones ─────────────────────────────────────────────
print("\n2. restore")
cap.records.clear()
restored, rmgr = restore_ns(s3) if err is None else ({}, new_mgr())
check("normal vars restored", restored.get("a") == 1 and restored.get("b") == "hello"
      and restored.get("rows") == [1, 2, 3], restored)
check("bad vars skipped", "bad1" not in restored and "bad2" not in restored)
check("skipped names reported", rmgr._last_restore_skipped_vars == ["bad1", "bad2"],
      rmgr._last_restore_skipped_vars)
w = cap.warnings()
check("a WARNING names each skipped var and the error type",
      any("'bad1'" in x and "RuntimeError" in x for x in w) and any("'bad2'" in x and "ValueError" in x for x in w), w)

# ── 3. old-format batch still restores ────────────────────────────────────────
print("\n3. old-format (format 1) batch")
s3_old = FakeS3()
mgr_old = new_mgr()
mgr_old._save_namespace_delta(s3_old, PREFIX, {"a": 7, "b": "old"}, {}, {})
s3_old.store[f"{PREFIX}/vars/__small__.pkl"] = dill.dumps({"a": 7, "b": "old"})  # as saved before
restored_old, _ = restore_ns(s3_old)
check("format-1 batch restores both vars", restored_old == {"a": 7, "b": "old"}, restored_old)
check("_decode_small_batch reports format 1",
      ckpt.manager._decode_small_batch(dill.dumps({"a": 7}))[0] == 1)

# ── 4. batch re-upload only on a small-var change ─────────────────────────────
print("\n4. small-batch hash logic")
s3h = FakeS3()
mh = new_mgr()
mh._save_namespace_delta(s3h, PREFIX, {"a": 1, "b": "x", "bad1": BadReduce()}, {}, {})
s3h.put_calls.clear()
mh._save_namespace_delta(s3h, PREFIX, {"a": 1, "b": "x", "bad1": BadReduce()}, {}, {})
check("unchanged small vars: batch NOT re-uploaded",
      not any("__small__.pkl" in k for k in s3h.put_calls), s3h.put_calls)
s3h.put_calls.clear()
mh._save_namespace_delta(s3h, PREFIX, {"a": 2, "b": "x", "bad1": BadReduce()}, {}, {})
check("one small var changed: batch re-uploaded",
      any("__small__.pkl" in k for k in s3h.put_calls), s3h.put_calls)
check("  manifest still written last", s3h.put_calls[-1].endswith("manifest.json"), s3h.put_calls)

# ── 5. large var that won't unpickle ──────────────────────────────────────────
print("\n5. large var that won't unpickle")
s3l = FakeS3()
ml = new_mgr()
ml._save_namespace_delta(s3l, PREFIX, {"a": 1, "huge_bad": BadLarge(), "big": list(range(50_000))}, {}, {})
man_l = json.loads(s3l.store[f"{PREFIX}/manifest.json"].decode())
check("huge_bad stored as its own object", man_l["vars"]["huge_bad"]["object"] == "vars/huge_bad.pkl")
restored_l, rl = restore_ns(s3l)
check("restore skips it and keeps the rest", "huge_bad" not in restored_l and restored_l.get("a") == 1
      and len(restored_l.get("big", [])) == 50_000, sorted(restored_l))
check("  reported as skipped", rl._last_restore_skipped_vars == ["huge_bad"], rl._last_restore_skipped_vars)

# ── 6. full restore() path + logs ─────────────────────────────────────────────
print("\n6. full restore() and logs")
import boto3  # noqa: E402
_orig_client = boto3.client
boto3.client = lambda *a, **k: s3
_orig_get = ckpt.manager.get_checkpoint_s3_client
ckpt.manager.get_checkpoint_s3_client = lambda: s3
s3.store.setdefault("sessions/sess-small/manifest.json", s3.store.get(f"{PREFIX}/manifest.json"))
full = new_mgr()
try:
    full.restore("sess-small")
except Exception as e:
    print(f"    (restore raised: {e!r})")
finally:
    boto3.client = _orig_client
    ckpt.manager.get_checkpoint_s3_client = _orig_get
rns = full._executor._namespace
check("restore() adopts the normal vars", rns.get("a") == 1 and rns.get("rows") == [1, 2, 3])
check("restore() skips the bad ones", "bad1" not in rns and "bad2" not in rns)
check("restore() logs the skipped names", any("could not be unpickled" in x and "bad1" in x
                                              for x in cap.warnings()), cap.warnings())
check("no variable value in any log line", SECRET_MARK not in cap.text())

# ── 7. A-B-05: a failed cell marks the checkpoint dirty ───────────────────────
print("\n7. A-B-05: failed cell -> dirty")
from app.notebook.executor import SandboxExecutor  # noqa: E402
ex = SandboxExecutor()
cm = ckpt.CheckpointManager(ex, {"microvm_id": "vm-dirty"})
ex.execute("x = 1", cell_id="c1")
cm._saved_fingerprint = cm._current_fingerprint()
check("clean right after the 'save'", cm.is_dirty() is False)
r = ex.execute("big = [1] * 10\nraise ValueError('boom')", cell_id="c2")
check("cell failed but kept its partial binding", not r.success and ex._namespace.get("big") == [1] * 10)
check("is_dirty() is True after the failed cell", cm.is_dirty() is True)
cm._saved_fingerprint = cm._current_fingerprint()
ex.execute("tmp = 5\nraise ValueError('boom')", scratch={})
check("a failed SCRATCH run does not mark dirty", cm.is_dirty() is False)
ex.execute("y = 2", cell_id="c3")
check("a successful cell still marks dirty", cm.is_dirty() is True)

passed = sum(1 for _, ok in _checks if ok)
print(f"\n{passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
