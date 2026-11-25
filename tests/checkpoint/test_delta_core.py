#!/usr/bin/env python3
# test-kind: offline
"""
Local (offline) unit tests for the delta-checkpoint CORE — Option A.

No AWS, no MicroVM, no proxy. Exercises the pure functions in checkpoint.py:
  build_var_blobs()  — per-variable pickle + content hash
  compute_delta()    — changed / unchanged / deleted vs a previous manifest

Plus an in-memory "S3" (dict) round-trip to prove the RESTORE side reconstructs
the exact namespace from (only) the objects a delta save would have uploaded,
carried forward across multiple suspends.

Run:
    python3 tests/checkpoint/test_delta_core.py
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import dill  # noqa: E402
from app.platform.checkpoint import (  # noqa: E402
    build_var_blobs,
    compute_delta,
)


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


# ── an in-memory object store standing in for S3 ─────────────────────────────
class FakeStore:
    """Mimics the per-var object layout: vars/{name} -> pickle bytes, + manifest."""

    def __init__(self):
        self.objects = {}          # key -> bytes
        self.manifest = {}         # name -> {"hash","size"}
        self.put_count = 0
        self.put_bytes = 0
        self.delete_count = 0

    def apply_delta_save(self, blobs, plan):
        """Upload ONLY changed var objects; delete removed; swap manifest last."""
        for name in plan["changed"]:
            key = f"vars/{name}"
            self.objects[key] = blobs[name]["bytes"]
            self.put_count += 1
            self.put_bytes += blobs[name]["size"]
        for name in plan["deleted"]:
            self.objects.pop(f"vars/{name}", None)
            self.delete_count += 1
        # Manifest is written LAST and references only fully-uploaded objects.
        self.manifest = plan["manifest"]

    def restore_namespace(self):
        """RESTORE side: read manifest → download listed objects → unpickle each."""
        ns = {}
        for name in self.manifest:
            ns[name] = dill.loads(self.objects[f"vars/{name}"])
        return ns


def _reset_counters(store):
    store.put_count = 0
    store.put_bytes = 0
    store.delete_count = 0


# ── tests ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 68)
    print("  Delta Checkpoint Core — offline unit tests (Option A)")
    print("=" * 68)

    store = FakeStore()

    # ---- SAVE #1: first save, everything is "changed" (no prev manifest) -----
    print("\nSAVE #1 (initial):")
    ns1 = {
        "a": 1,
        "b": "hello",
        "big": list(range(100_000)),         # the large var
        "cfg": {"x": 1, "y": 2},
    }
    blobs1, skipped1 = build_var_blobs(ns1)
    plan1 = compute_delta(blobs1, store.manifest)
    _reset_counters(store)
    store.apply_delta_save(blobs1, plan1)
    check("no unpicklable vars skipped", not skipped1)
    check("all 4 vars changed on first save", sorted(plan1["changed"]) == ["a", "b", "big", "cfg"])
    check("nothing unchanged on first save", plan1["unchanged"] == [])
    check("nothing deleted on first save", plan1["deleted"] == [])
    check("store holds 4 objects", len(store.objects) == 4)

    # restore must equal original
    r1 = store.restore_namespace()
    check("restore #1 reconstructs namespace exactly", r1 == ns1)

    # ---- SAVE #2: change ONE small var; 'big' untouched -> must NOT re-upload -
    print("\nSAVE #2 (change only 'a'):")
    ns2 = dict(ns1)
    ns2["a"] = 999                      # changed
    # 'b', 'big', 'cfg' identical objects/content
    blobs2, _ = build_var_blobs(ns2)
    plan2 = compute_delta(blobs2, store.manifest)
    _reset_counters(store)
    store.apply_delta_save(blobs2, plan2)
    check("only 'a' marked changed", plan2["changed"] == ["a"])
    check("'big' is unchanged (delta skips it)", "big" in plan2["unchanged"])
    check("exactly 1 object uploaded", store.put_count == 1)
    big_size = blobs2["big"]["size"]
    check("did NOT re-upload the large var (bytes saved)", store.put_bytes < big_size)
    r2 = store.restore_namespace()
    check("restore #2 reconstructs namespace exactly", r2 == ns2)

    # ---- SAVE #3: mutate the large var -> now it SHOULD re-upload ------------
    print("\nSAVE #3 (append to 'big'):")
    ns3 = dict(ns2)
    ns3["big"] = ns2["big"] + [1]       # content changed
    blobs3, _ = build_var_blobs(ns3)
    plan3 = compute_delta(blobs3, store.manifest)
    _reset_counters(store)
    store.apply_delta_save(blobs3, plan3)
    check("'big' re-uploaded after real change", "big" in plan3["changed"])
    check("'a','b','cfg' unchanged", set(["a", "b", "cfg"]).issubset(set(plan3["unchanged"])))
    r3 = store.restore_namespace()
    check("restore #3 reconstructs namespace exactly", r3 == ns3)

    # ---- SAVE #4: delete a var + add a new one ------------------------------
    print("\nSAVE #4 (delete 'b', add 'new'):")
    ns4 = dict(ns3)
    del ns4["b"]
    ns4["new"] = [1, 2, 3]
    blobs4, _ = build_var_blobs(ns4)
    plan4 = compute_delta(blobs4, store.manifest)
    _reset_counters(store)
    store.apply_delta_save(blobs4, plan4)
    check("'b' detected as deleted", plan4["deleted"] == ["b"])
    check("'new' detected as changed(new)", "new" in plan4["changed"])
    check("deleted object removed from store", "vars/b" not in store.objects)
    check("manifest no longer lists 'b'", "b" not in store.manifest)
    r4 = store.restore_namespace()
    check("restore #4 reconstructs namespace exactly", r4 == ns4)
    check("restore #4 does NOT contain deleted 'b'", "b" not in r4)

    # ---- SAVE #5: no changes -> zero uploads --------------------------------
    print("\nSAVE #5 (no changes):")
    blobs5, _ = build_var_blobs(ns4)
    plan5 = compute_delta(blobs5, store.manifest)
    _reset_counters(store)
    store.apply_delta_save(blobs5, plan5)
    check("no vars changed when nothing mutated", plan5["changed"] == [])
    check("zero objects uploaded on no-op save", store.put_count == 0)

    # ---- hash stability: same content -> same hash across builds ------------
    print("\nHASH STABILITY:")
    hA = build_var_blobs({"v": {"k": [1, 2, 3]}})[0]["v"]["hash"]
    hB = build_var_blobs({"v": {"k": [1, 2, 3]}})[0]["v"]["hash"]
    check("identical content hashes equal (stable)", hA == hB)
    hC = build_var_blobs({"v": {"k": [1, 2, 4]}})[0]["v"]["hash"]
    check("different content hashes differ", hA != hC)

    # ---- report -------------------------------------------------------------
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 68)
    print(f"  DELTA CORE: {passed}/{total} checks passed")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
