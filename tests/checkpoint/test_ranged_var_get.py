#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: ranged parallel S3 GET for large checkpoint var objects
(restore-speedups FEAT-004).

A var object at or above _RANGED_GET_THRESHOLD is downloaded with parallel
ranged GETs into one preallocated bytearray (IfMatch on the ETag of part 0,
size check against ContentRange and the manifest, in-order sha256 against the
manifest hash); any failure falls back to the single GET. Covers:
  - _split_ranges (exact multiple, remainder, size < part, contiguous);
  - a ranged restore equals the original, ranges cover the object once,
    IfMatch on every part after the first;
  - a transient part error is retried;
  - exhausted retries / hash mismatch / ContentRange size mismatch fall back to
    a single GET (warning names the var and error type only);
  - a below-threshold var makes no Range call;
  - tracemalloc peak of a ranged download stays near the single-GET peak;
  - the 'Restore vars:' timing line;
  - nspickle.loads(bytearray) == loads(bytes).
In-memory fake S3, thresholds monkeypatched down (4 MiB threshold, 1 MiB parts).
No AWS, no network.
Run:
    .venv/bin/python tests/checkpoint/test_ranged_var_get.py
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import tracemalloc
import types
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")
_SPOOL = tempfile.mkdtemp(prefix="ranged-get-test-")
os.environ["CHECKPOINT_SPOOL_DIR"] = _SPOOL
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))  # import-time only
from app.platform import checkpoint as ckpt  # noqa: E402
from app.platform.checkpoint import nspickle  # noqa: E402

M = ckpt.manager
MiB = 1024 * 1024
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f" — {str(detail)[:300]}" if not ok and detail else ""))


class _NoSuchKey(Exception):
    pass


class _Body:
    """Streaming body over a buffer: read(amt) copies only `amt` bytes."""

    def __init__(self, data):
        self._mv = memoryview(data)
        self._pos = 0

    def read(self, amt=None):
        end = len(self._mv) if amt is None else min(len(self._mv), self._pos + amt)
        out = bytes(self._mv[self._pos:end])
        self._pos = end
        return out


class FakeS3:
    """In-memory S3. `fault(key, start, attempt)` may return None, an exception
    to raise, "corrupt" (flip a byte of the part) or "total" (wrong
    ContentRange total)."""

    ETAG = '"etag-1"'

    def __init__(self):
        self.store, self.put_calls, self.get_calls = {}, [], []
        self.fault = None
        self._attempts = {}

        class _Exc:
            NoSuchKey = _NoSuchKey
        self.exceptions = _Exc()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        b = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()
        self.store[Key] = bytes(b)
        self.put_calls.append(Key)

    def get_object(self, Bucket, Key, Range=None, IfMatch=None):
        self.get_calls.append((Key, Range, IfMatch))
        if Key not in self.store:
            raise _NoSuchKey(Key)
        data = self.store[Key]
        if Range is None:
            return {"Body": _Body(data), "ETag": self.ETAG, "ContentLength": len(data)}
        if IfMatch is not None and IfMatch != self.ETAG:
            raise RuntimeError("PreconditionFailed")
        a, b = (int(x) for x in Range[len("bytes="):].split("-"))
        b = min(b, len(data) - 1)
        att = self._attempts[(Key, a)] = self._attempts.get((Key, a), 0) + 1
        action = self.fault(Key, a, att) if self.fault else None
        if isinstance(action, BaseException):
            raise action
        total = len(data) + (1 if action == "total" else 0)
        part = memoryview(data)[a:b + 1]
        if action == "corrupt":
            part = bytearray(part)
            part[0] ^= 0xFF
        return {"Body": _Body(part), "ETag": self.ETAG, "ContentLength": b - a + 1,
                "ContentRange": f"bytes {a}-{b}/{total}"}

    def delete_object(self, Bucket, Key):
        self.store.pop(Key, None)

    def reset(self):
        self.get_calls.clear()
        self._attempts.clear()
        self.fault = None


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


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, r):
        self.records.append(r)

    def warnings(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]

    def infos(self):
        return [r.getMessage() for r in self.records]


cap = Capture()
logging.getLogger().addHandler(cap)
logging.getLogger().setLevel(logging.DEBUG)

PREFIX = "sessions/sess-ranged"
_orig = {k: getattr(M, k) for k in ("_RANGED_GET_THRESHOLD", "_RANGED_GET_PART", "_RANGED_PART_BACKOFF")}
M._RANGED_GET_THRESHOLD = 4 * MiB
M._RANGED_GET_PART = 1 * MiB
M._RANGED_PART_BACKOFF = (0.0, 0.0)


def new_mgr():
    m = ckpt.CheckpointManager(FakeExecutor(), {"microvm_id": "vm-test"})
    m._bucket = "fake-bucket"
    return m


def restore_ns(s3):
    man = json.loads(s3.store[f"{PREFIX}/manifest.json"].decode())
    meta = man["vars"]
    small = [n for n, m in meta.items() if m.get("object") == "vars/__small__.pkl"]
    return new_mgr()._restore_namespace_delta(s3, PREFIX, meta, small, {}, {}), meta


def range_calls(s3, key):
    return [(r, im) for k, r, im in s3.get_calls if k == key and r is not None]


def single_calls(s3, key):
    return [1 for k, r, _ in s3.get_calls if k == key and r is None]


try:
    # ── 1. _split_ranges ───────────────────────────────────────────────────────
    print("\n1. _split_ranges")
    check("exact multiple", M._split_ranges(8, 4) == [(0, 3), (4, 7)], M._split_ranges(8, 4))
    check("remainder", M._split_ranges(10, 4) == [(0, 3), (4, 7), (8, 9)], M._split_ranges(10, 4))
    check("size < part", M._split_ranges(3, 4) == [(0, 2)], M._split_ranges(3, 4))
    rs = M._split_ranges(65 * MiB + 7, 16 * MiB)
    check("contiguous, no overlap, covers once",
          rs[0][0] == 0 and rs[-1][1] == 65 * MiB + 6
          and all(rs[i + 1][0] == rs[i][1] + 1 for i in range(len(rs) - 1)), rs)

    # ── 2. ranged restore ──────────────────────────────────────────────────────
    print("\n2. ranged restore of a 6.5 MiB var")
    big = os.urandom(6 * MiB + MiB // 2)
    medium = os.urandom(MiB)   # large object but below the (patched) threshold
    s3 = FakeS3()
    new_mgr()._save_namespace_delta(s3, PREFIX, {"big": big, "medium": medium, "a": 1}, {}, {})
    _, meta = restore_ns(s3)   # read the manifest
    key_big = f"{PREFIX}/{meta['big']['object']}"
    key_med = f"{PREFIX}/{meta['medium']['object']}"
    size_big = meta["big"]["size"]
    check("precondition: big is over the threshold and has a hash",
          size_big >= M._RANGED_GET_THRESHOLD and meta["big"].get("hash"), meta["big"])
    s3.reset()
    cap.records.clear()
    ns, _ = restore_ns(s3)
    check("restored value equals the original", ns.get("big") == big and ns.get("a") == 1)
    rc = range_calls(s3, key_big)
    spans = sorted(tuple(int(x) for x in r[len("bytes="):].split("-")) for r, _ in rc)
    check("ranges cover the object exactly once",
          spans == M._split_ranges(size_big, M._RANGED_GET_PART), spans)
    check("no single GET for the ranged var", not single_calls(s3, key_big))
    check("part 0 has no IfMatch, later parts IfMatch the ETag",
          all((im is None) if r.startswith("bytes=0-") else (im == FakeS3.ETAG) for r, im in rc), rc)
    # ── 7 (same run). below-threshold var ──────────────────────────────────────
    check("below-threshold var: no Range call, one single GET",
          not range_calls(s3, key_med) and len(single_calls(s3, key_med)) == 1 and ns.get("medium") == medium)
    # ── 9 (same run). timing line ──────────────────────────────────────────────
    lines = [x for x in cap.infos() if "Restore vars:" in x]
    check("one 'Restore vars:' line with per-var get/unpickle",
          len(lines) == 1 and "unpickle=" in lines[0] and "per var (slowest first): big get=" in lines[0]
          and " ranged" in lines[0] and "medium" not in lines[0], lines)
    check("no warning on the happy path", not cap.warnings(), cap.warnings())

    # ── 3. transient part error retried ────────────────────────────────────────
    print("\n3. transient part error")
    s3.reset()
    cap.records.clear()
    s3.fault = lambda k, a, att: ConnectionError("blip") if (k == key_big and a == 2 * MiB and att == 1) else None
    ns, _ = restore_ns(s3)
    check("value correct", ns.get("big") == big)
    check("the failed part was retried", sum(1 for r, _ in range_calls(s3, key_big)
                                             if r.startswith(f"bytes={2 * MiB}-")) == 2)
    check("no fallback single GET", not single_calls(s3, key_big))

    # ── 4. exhausted retries => fallback ───────────────────────────────────────
    print("\n4. exhausted retries")
    s3.reset()
    cap.records.clear()
    s3.fault = lambda k, a, att: ConnectionError("DETAIL-NOT-LOGGED") if (k == key_big and a == 3 * MiB) else None
    ns, _ = restore_ns(s3)
    check("value correct", ns.get("big") == big)
    check(f"{M._RANGED_PART_ATTEMPTS} attempts on the failing part",
          sum(1 for r, _ in range_calls(s3, key_big) if r.startswith(f"bytes={3 * MiB}-"))
          == M._RANGED_PART_ATTEMPTS)
    check("fell back to one single GET", len(single_calls(s3, key_big)) == 1)
    w = [x for x in cap.warnings() if "ranged download" in x]
    check("warning names the var and error type only",
          len(w) == 1 and "'big'" in w[0] and "ConnectionError" in w[0] and "DETAIL-NOT-LOGGED" not in w[0], w)
    lines = [x for x in cap.infos() if "Restore vars:" in x]
    check("timing line has no ' ranged' after a fallback", lines and " ranged" not in lines[0], lines)

    # ── 5. hash mismatch => fallback ───────────────────────────────────────────
    print("\n5. hash mismatch")
    s3.reset()
    cap.records.clear()
    s3.fault = lambda k, a, att: "corrupt" if (k == key_big and a == 4 * MiB) else None
    ns, _ = restore_ns(s3)
    check("value correct", ns.get("big") == big)
    check("fell back to one single GET", len(single_calls(s3, key_big)) == 1)
    check("warning names _RangedGetFailed", any("'big'" in x and "_RangedGetFailed" in x for x in cap.warnings()),
          cap.warnings())

    # ── 6. ContentRange size mismatch => fallback ──────────────────────────────
    print("\n6. ContentRange total differs")
    s3.reset()
    cap.records.clear()
    s3.fault = lambda k, a, att: "total" if (k == key_big and a == 0) else None
    ns, _ = restore_ns(s3)
    check("value correct", ns.get("big") == big)
    check("only part 0 was requested before falling back", len(range_calls(s3, key_big)) == 1,
          range_calls(s3, key_big))
    check("fell back to one single GET", len(single_calls(s3, key_big)) == 1)

    # ── 6b. a client without Range (TypeError) => fallback ─────────────────────
    print("\n6b. client without Range")

    class NoRangeS3(FakeS3):
        def get_object(self, Bucket, Key):
            return super().get_object(Bucket, Key)

    s3n = NoRangeS3()
    s3n.store = dict(s3.store)
    cap.records.clear()
    ns, _ = restore_ns(s3n)
    check("value correct via fallback", ns.get("big") == big)
    check("warning names TypeError", any("'big'" in x and "TypeError" in x for x in cap.warnings()), cap.warnings())

    # ── 8. tracemalloc peak ────────────────────────────────────────────────────
    print("\n8. memory peak of a 24 MiB ranged download")
    s3m = FakeS3()
    data24 = os.urandom(24 * MiB)
    s3m.store["k24"] = data24
    h24 = M._hash_bytes(data24)
    tracemalloc.start()
    b1 = s3m.get_object(Bucket="b", Key="k24")["Body"].read()
    _, single_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del b1
    tracemalloc.start()
    b2 = M._ranged_get(s3m, "b", "k24", len(data24), h24)
    _, ranged_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("ranged bytes equal the object", b2 == data24 and isinstance(b2, bytearray))
    limit = single_peak + M._RANGED_GET_CONCURRENCY * M._RANGED_READ_CHUNK + MiB
    check("ranged peak <= single-GET peak + concurrency*chunk + 1 MiB", ranged_peak <= limit,
          f"ranged={ranged_peak} single={single_peak} limit={limit}")
    del b2

    # ── 10. nspickle.loads(bytearray) ──────────────────────────────────────────
    print("\n10. nspickle.loads on a bytearray")
    live = {}
    val = {"a": [1, 2, 3], "s": "x" * 200_000, "b": os.urandom(300_000)}
    pb = nspickle.dumps(val, live)
    check("loads(bytearray) == loads(bytes)", nspickle.loads(bytearray(pb), live) == nspickle.loads(pb, live) == val)
    check("loads(memoryview) == loads(bytes)", nspickle.loads(memoryview(pb), live) == val)
finally:
    for k, v in _orig.items():
        setattr(M, k, v)
    shutil.rmtree(_SPOOL, ignore_errors=True)

passed = sum(1 for _, ok in _checks if ok)
print(f"\n{passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
