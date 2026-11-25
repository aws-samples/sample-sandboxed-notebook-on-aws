#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit tests for the per-file DELTA core in checkpoint.py.

No AWS, no MicroVM. Uses a real temp dir for the "/tmp" files and an in-memory
store standing in for S3, to prove:
  • First save uploads all files; restore reconstructs them byte-for-byte.
  • Changing ONE file re-uploads only that file (others skipped by signature).
  • A same-content re-write with a new mtime is treated as changed (signature).
  • Deleting a file removes its object + manifest entry; restore drops it.
  • No changes → zero uploads.

Run:
    python3 tests/checkpoint/test_file_delta_core.py
"""

import sys
import os
import time
import shutil
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.platform.checkpoint import file_signature, compute_file_delta  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def scan(dirpath):
    """Build the disk_files dict the way save() would (name → path/size/mtime)."""
    out = {}
    for fn in os.listdir(dirpath):
        p = os.path.join(dirpath, fn)
        if os.path.isfile(p):
            size, mtime_ns = file_signature(p)
            out[fn] = {"path": p, "size": size, "mtime_ns": mtime_ns}
    return out


class FakeFileStore:
    """Per-file object store + manifest (mirrors sessions/{id}/files/*)."""

    def __init__(self):
        self.objects = {}    # name -> bytes
        self.manifest = {}   # name -> {"size","mtime_ns"}
        self.puts = []       # [(name, nbytes)]
        self.deletes = []

    def apply(self, disk_files, plan):
        for name in plan["changed"]:
            with open(disk_files[name]["path"], "rb") as f:
                b = f.read()
            self.objects[name] = b
            self.puts.append((name, len(b)))
        for name in plan["deleted"]:
            self.objects.pop(name, None)
            self.deletes.append(name)
        self.manifest = plan["manifest"]  # written last

    def restore_into(self, dirpath):
        for name in self.manifest:
            with open(os.path.join(dirpath, name), "wb") as f:
                f.write(self.objects[name])


def _reset(store):
    store.puts = []
    store.deletes = []


def main():
    print("=" * 68)
    print("  File Delta Core — offline unit tests (per-file objects)")
    print("=" * 68)

    src = tempfile.mkdtemp(prefix="filedelta-src-")
    dst = tempfile.mkdtemp(prefix="filedelta-dst-")
    store = FakeFileStore()
    try:
        def write(name, content):
            with open(os.path.join(src, name), "w") as f:
                f.write(content)

        # ---- SAVE #1 ----------------------------------------------------------
        print("\nSAVE #1 (initial 3 files):")
        write("a.csv", "id,val\n1,10\n")
        write("big.parquet", "x" * 500_000)
        write("notes.txt", "hello")
        df1 = scan(src)
        plan1 = compute_file_delta(df1, store.manifest)
        _reset(store)
        store.apply(df1, plan1)
        check("all 3 files changed on first save", sorted(plan1["changed"]) == ["a.csv", "big.parquet", "notes.txt"])
        check("nothing unchanged/deleted first save", plan1["unchanged"] == [] and plan1["deleted"] == [])
        check("store has 3 objects", len(store.objects) == 3)
        store.restore_into(dst)
        check("restore #1 reconstructs a.csv", open(os.path.join(dst, "a.csv")).read() == "id,val\n1,10\n")
        check("restore #1 reconstructs big.parquet size", os.path.getsize(os.path.join(dst, "big.parquet")) == 500_000)

        # ---- SAVE #2: change ONLY a.csv; big.parquet untouched ---------------
        print("\nSAVE #2 (change only a.csv):")
        time.sleep(0.01)  # ensure mtime advances
        write("a.csv", "id,val\n1,10\n2,20\n")   # changed content + mtime
        df2 = scan(src)
        plan2 = compute_file_delta(df2, store.manifest)
        _reset(store)
        store.apply(df2, plan2)
        check("only a.csv changed", plan2["changed"] == ["a.csv"])
        check("big.parquet skipped (unchanged)", "big.parquet" in plan2["unchanged"])
        check("exactly 1 upload", len(store.puts) == 1)
        big_bytes = os.path.getsize(os.path.join(src, "big.parquet"))
        uploaded = sum(n for _, n in store.puts)
        check("did NOT re-upload the big file", uploaded < big_bytes)

        # ---- SAVE #3: touch big.parquet (new mtime, same size) → changed -----
        print("\nSAVE #3 (rewrite big.parquet, new mtime):")
        time.sleep(0.01)
        write("big.parquet", "y" * 500_000)   # same size, different content + mtime
        df3 = scan(src)
        plan3 = compute_file_delta(df3, store.manifest)
        _reset(store)
        store.apply(df3, plan3)
        check("big.parquet re-uploaded (mtime/content changed)", "big.parquet" in plan3["changed"])
        check("a.csv, notes.txt unchanged", set(["a.csv", "notes.txt"]).issubset(set(plan3["unchanged"])))
        # restore should now have the NEW big content
        store.restore_into(dst)
        check("restore #3 big.parquet has new content", open(os.path.join(dst, "big.parquet")).read()[0] == "y")

        # ---- SAVE #4: delete notes.txt + add new file ------------------------
        print("\nSAVE #4 (delete notes.txt, add new.json):")
        os.remove(os.path.join(src, "notes.txt"))
        write("new.json", '{"k": 1}')
        df4 = scan(src)
        plan4 = compute_file_delta(df4, store.manifest)
        _reset(store)
        store.apply(df4, plan4)
        check("notes.txt detected as deleted", plan4["deleted"] == ["notes.txt"])
        check("new.json detected as changed(new)", "new.json" in plan4["changed"])
        check("notes.txt object removed", "notes.txt" not in store.objects)
        check("notes.txt gone from manifest", "notes.txt" not in store.manifest)
        # rebuild dst from scratch to prove manifest is authoritative
        shutil.rmtree(dst); os.makedirs(dst)
        store.restore_into(dst)
        check("restore #4 has new.json", os.path.exists(os.path.join(dst, "new.json")))
        check("restore #4 does NOT have deleted notes.txt", not os.path.exists(os.path.join(dst, "notes.txt")))

        # ---- SAVE #5: no changes → zero uploads ------------------------------
        print("\nSAVE #5 (no changes):")
        df5 = scan(src)
        plan5 = compute_file_delta(df5, store.manifest)
        _reset(store)
        store.apply(df5, plan5)
        check("no files changed when nothing mutated", plan5["changed"] == [])
        check("zero uploads on no-op save", len(store.puts) == 0)

    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 68)
    print(f"  FILE DELTA CORE: {passed}/{total} checks passed")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
