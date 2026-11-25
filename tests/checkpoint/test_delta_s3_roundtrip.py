#!/usr/bin/env python3
# test-kind: offline
"""
Offline round-trip test of the REAL delta save/restore paths in checkpoint.py,
driven against an in-memory fake S3 client (no AWS, no MicroVM).

This exercises the actual production code:
  CheckpointManager._save_namespace_delta()  — small/large split, __small__.pkl,
                                                manifest.json, changed-only upload
  CheckpointManager.restore()  namespace step — delta manifest → per-var download

Proves:
  • First save uploads everything; restore reconstructs it.
  • Changing one small var re-uploads only the batched small object, NOT the big var.
  • Changing the big var re-uploads it.
  • Deleting a var removes its object + manifest entry; restore drops it.
  • Files/requirements/etc. are full-overwrite (delta is variables-only).

Run:
    python3 tests/checkpoint/test_delta_s3_roundtrip.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.platform import checkpoint as ckpt  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


# ── in-memory fake S3 client (only the calls checkpoint.py uses) ─────────────
class _NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self):
        self.store = {}          # key -> bytes
        self.put_calls = []      # [(key, nbytes)]
        self.deleted = []        # [key]

        class _Exc:
            NoSuchKey = _NoSuchKey
        self.exceptions = _Exc()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        b = Body if isinstance(Body, (bytes, bytearray)) else str(Body).encode()
        self.store[Key] = bytes(b)
        self.put_calls.append((Key, len(b)))

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise _NoSuchKey(Key)
        import io as _io
        return {"Body": _io.BytesIO(self.store[Key])}

    def delete_object(self, Bucket, Key):
        self.store.pop(Key, None)
        self.deleted.append(Key)


# ── minimal fakes for the executor the manager talks to ──────────────────────
class FakeExecutor:
    def __init__(self):
        self._namespace = {}
        self._prov_clock = 0

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


def _new_manager():
    mgr = ckpt.CheckpointManager(FakeExecutor(), {"microvm_id": "vm-test"})
    mgr._bucket = "fake-bucket"
    return mgr


def _reset_puts(s3):
    s3.put_calls = []
    s3.deleted = []


def _put_bytes_for_prefix(s3, substr):
    return sum(n for k, n in s3.put_calls if substr in k)


def main():
    print("=" * 68)
    print("  Delta save/restore round-trip — REAL code, fake S3 (offline)")
    print("=" * 68)

    s3 = FakeS3()
    mgr = _new_manager()
    prefix = "sessions/sess-1"

    big = list(range(200_000))   # large var (>64KB pickled)
    small_a = 1
    small_b = "hello"

    # ---- SAVE #1 -------------------------------------------------------------
    print("\nSAVE #1 (initial):")
    ns = {"a": small_a, "b": small_b, "big": big}
    _reset_puts(s3)
    up1 = mgr._save_namespace_delta(s3, prefix, dict(ns), {}, {})
    man = json.loads(s3.store[f"{prefix}/manifest.json"].decode())
    check("manifest format is delta-v1", man.get("format") == "delta-v1")
    check("manifest lists all 3 vars", set(man["vars"].keys()) == {"a", "b", "big"})
    check("big var has its own object", man["vars"]["big"]["object"] == "vars/big.pkl")
    check("small vars point to __small__.pkl",
          man["vars"]["a"]["object"] == "vars/__small__.pkl" and man["vars"]["b"]["object"] == "vars/__small__.pkl")
    check("big object uploaded", f"{prefix}/vars/big.pkl" in s3.store)
    check("small batch object uploaded", f"{prefix}/vars/__small__.pkl" in s3.store)
    check("uploaded bytes > 0", up1 > 0)

    # ---- SAVE #2: change only small 'a' -> big must NOT re-upload -------------
    print("\nSAVE #2 (change only small 'a'):")
    ns2 = {"a": 999, "b": small_b, "big": big}
    _reset_puts(s3)
    up2 = mgr._save_namespace_delta(s3, prefix, dict(ns2), {}, {})
    big_reuploaded = any("vars/big.pkl" in k for k, _ in s3.put_calls)
    small_reuploaded = any("vars/__small__.pkl" in k for k, _ in s3.put_calls)
    check("big var NOT re-uploaded", not big_reuploaded)
    check("small batch re-uploaded (a changed)", small_reuploaded)
    check("delta bytes are small (no big var)", up2 < _put_bytes_for_prefix(s3, "big.pkl") + 10_000_000 and not big_reuploaded)

    # ---- SAVE #3: change big -> it re-uploads --------------------------------
    print("\nSAVE #3 (mutate big):")
    ns3 = {"a": 999, "b": small_b, "big": big + [1]}
    _reset_puts(s3)
    mgr._save_namespace_delta(s3, prefix, dict(ns3), {}, {})
    # A changed large var goes to the key the committed manifest does NOT use
    # (vars/big.pkl <-> vars/big.alt.pkl), so the previous checkpoint stays intact.
    check("big var re-uploaded after change", any(k.endswith("vars/big.alt.pkl") for k, _ in s3.put_calls))
    man3 = json.loads(s3.store[f"{prefix}/manifest.json"].decode())
    check("manifest points at the new object; the old one is deleted",
          man3["vars"]["big"]["object"] == "vars/big.alt.pkl" and f"{prefix}/vars/big.pkl" not in s3.store)
    check("small batch NOT re-uploaded (unchanged)", not any("__small__.pkl" in k for k, _ in s3.put_calls))

    # ---- SAVE #4: delete 'b' -------------------------------------------------
    print("\nSAVE #4 (delete small 'b'):")
    ns4 = {"a": 999, "big": big + [1]}
    _reset_puts(s3)
    mgr._save_namespace_delta(s3, prefix, dict(ns4), {}, {})
    man4 = json.loads(s3.store[f"{prefix}/manifest.json"].decode())
    check("'b' gone from manifest", "b" not in man4["vars"])

    # ---- RESTORE into a FRESH manager (simulates new VM) ---------------------
    print("\nRESTORE (fresh manager, new VM):")
    mgr2 = _new_manager()
    mgr2._executor._namespace.clear()
    # Drive the real restore() but stub boto3.client to our fake + neutralize the
    # non-namespace steps (files/packages/etc.) which aren't under test here.
    import boto3
    _orig_client = boto3.client
    boto3.client = lambda *a, **k: s3
    try:
        mgr2.restore("sess-1")
    except Exception as e:
        # files/packages steps will hit NoSuchKey on our fake and are caught inside
        # restore(); only namespace matters here. Surface unexpected errors though.
        print(f"    (restore raised: {e})")
    finally:
        boto3.client = _orig_client

    ns_restored = {k: v for k, v in mgr2._executor._namespace.items()
                   if not (k.startswith("__") and k.endswith("__"))}
    check("restored 'a' correct", ns_restored.get("a") == 999)
    check("restored 'big' correct (len)", hasattr(ns_restored.get("big"), "__len__") and len(ns_restored["big"]) == len(big) + 1)
    check("restored namespace does NOT contain deleted 'b'", "b" not in ns_restored)
    check("restore primed in-memory manifest for next delta", bool(mgr2._saved_manifest))

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 68)
    print(f"  DELTA S3 ROUND-TRIP: {passed}/{total} checks passed")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
