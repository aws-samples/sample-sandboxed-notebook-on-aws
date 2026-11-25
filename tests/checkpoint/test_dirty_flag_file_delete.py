#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit test: the dirty-flag must react to /tmp FILE changes — especially
DELETIONS — not just namespace/package changes.

Scenario the user called out:
  1. user uploads 2 files → suspend → both saved to S3 (dirty).
  2. user DELETES one file, nothing else changes → suspend again.
     The provenance clock did NOT move (no variable touched), so without folding
     the file set into the fingerprint, is_dirty() would return False and the
     save would be skipped — leaving the deleted file's S3 object orphaned.

This test monkeypatches the file glob root to a temp dir (via the exts glob in
_tmp_files_signature which reads /tmp) — so we drive it through a fake executor
and a temp dir symlinked pattern. We test _tmp_files_signature + is_dirty directly.

Run:
    python3 tests/checkpoint/test_dirty_flag_file_delete.py
"""

import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.platform import checkpoint as ckpt  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


class FakeExecutor:
    def __init__(self):
        self._namespace = {}
        self._prov_clock = 5  # some fixed value; we will NOT change it


def main():
    print("=" * 68)
    print("  Dirty-flag reacts to /tmp file add/change/DELETE (offline)")
    print("=" * 68)

    # Use a temp dir and point the signature scanner at it by monkeypatching the
    # module-level glob root. _tmp_files_signature globs '/tmp/*.ext'; we redirect
    # by temporarily patching glob.glob within the checkpoint module.
    tmp = tempfile.mkdtemp(prefix="dirtyflag-")
    real_glob = ckpt.glob.glob

    def fake_glob(pattern, *args, **kwargs):
        # Product calls glob.glob(pattern, recursive=True) with patterns like
        # '/tmp/**/*.csv'. Accept (and ignore) extra args: the basename redirect
        # flattens the pattern to '<tmp>/*.csv', which has no '**', so recursive
        # is irrelevant and dropping it keeps the redirect confined to our temp dir.
        base = os.path.basename(pattern)  # '*.csv'
        return real_glob(os.path.join(tmp, base))

    ckpt.glob.glob = fake_glob
    try:
        mgr = ckpt.CheckpointManager(FakeExecutor(), {"microvm_id": "vm"})

        def write(name, content):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(content)

        # --- Start: 2 files, capture "saved" fingerprint ---
        write("a.csv", "1,2,3\n")
        write("b.csv", "4,5,6\n")
        # Guard: the scanner swallows exceptions (best-effort), so a broken
        # monkeypatch would yield an empty signature and every check below would
        # look trivially "clean". Make sure the scan actually sees both files.
        check("scanner sees the temp files", len(mgr._tmp_files_signature()) == 2)
        # simulate a successful save: capture the fingerprint like save() does at the end
        mgr._saved_fingerprint = mgr._current_fingerprint()
        check("clean immediately after save (no changes)", not mgr.is_dirty())

        # --- DELETE one file, touch nothing else ---
        os.remove(os.path.join(tmp, "b.csv"))
        check("DIRTY after deleting a file (the key fix)", mgr.is_dirty())

        # re-save (recapture fingerprint) → clean again
        mgr._saved_fingerprint = mgr._current_fingerprint()
        check("clean after re-save following delete", not mgr.is_dirty())

        # --- ADD a new file ---
        write("c.csv", "7,8,9\n")
        check("DIRTY after adding a file", mgr.is_dirty())
        mgr._saved_fingerprint = mgr._current_fingerprint()

        # --- MODIFY a file (append) — size changes ---
        with open(os.path.join(tmp, "a.csv"), "a") as f:
            f.write("10,11,12\n")
        check("DIRTY after modifying a file (size/mtime changed)", mgr.is_dirty())
        mgr._saved_fingerprint = mgr._current_fingerprint()

        # --- No change → clean ---
        check("clean when nothing changed", not mgr.is_dirty())

        # --- delete ALL files → still detects dirty ---
        os.remove(os.path.join(tmp, "a.csv"))
        os.remove(os.path.join(tmp, "c.csv"))
        check("DIRTY after deleting all files", mgr.is_dirty())

    finally:
        ckpt.glob.glob = real_glob
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 68)
    print(f"  DIRTY-FLAG FILE DELETE: {passed}/{total} checks passed")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
