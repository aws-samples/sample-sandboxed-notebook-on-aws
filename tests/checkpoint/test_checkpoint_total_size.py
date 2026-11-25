#!/usr/bin/env python3
# test-kind: offline
"""
Offline test of metadata.json checkpoint_total_kb (vm-card-regroup Fix A,
rebuild-live-check F1). Drives the REAL CheckpointManager.save() against an
in-memory fake S3 (pattern from test_dag_checkpoint.py; no AWS, no MicroVM).
Proves:
  • checkpoint_total_kb = (every var pickle + every tracked /tmp file + JSON
    tail) / 1024, and is ≥ checkpoint_size_kb (the uploaded-pickle delta).
  • a no-change second save: checkpoint_size_kb == 0 but checkpoint_total_kb is
    unchanged and > 0 (the F1 bug: the card showed the delta / nothing).
  • a small var change: the total follows, the delta stays small.
  • last_save_summary["total_kb"] == metadata["checkpoint_total_kb"].
  • removing a tracked file lowers the total.
Run (system python3 — needs dill):
    python3 tests/checkpoint/test_checkpoint_total_size.py
"""
import json
import logging
import os
import shutil
import sys
import tempfile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.platform.checkpoint import manager as manager_mod  # noqa: E402
from test_dag_checkpoint import FakeS3, _manager, _real_executor  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


PREFIX = "sessions/sess-total"


class _Patched:
    """Route the manager's S3 client to `s3`; /tmp scan returns `files()`."""

    def __init__(self, s3, files):
        self.s3, self.files = s3, files

    def __enter__(self):
        self._client = manager_mod.get_checkpoint_s3_client
        self._scan = manager_mod._scan_tmp_checkpoint_files
        manager_mod.get_checkpoint_s3_client = lambda: self.s3
        manager_mod._scan_tmp_checkpoint_files = lambda: list(self.files())
        return self

    def __exit__(self, *exc):
        manager_mod.get_checkpoint_s3_client = self._client
        manager_mod._scan_tmp_checkpoint_files = self._scan
        return False


def _meta(s3):
    return json.loads(s3.store[f"{PREFIX}/metadata.json"].decode("utf-8"))


def _expected_kb(mgr, files):
    sz = mgr.last_save_sizes
    tail = sum(sz.get(k, 0) for k in ("packages", "data_catalog", "dag", "imports"))
    fbytes = sum(os.path.getsize(f) for f in files)
    return round((sz.get("serialize", 0) + fbytes + tail) / 1024, 1)


def _run():
    tmpdir = tempfile.mkdtemp(dir="/tmp", prefix="ckpt-total-")
    try:
        f1 = os.path.join(tmpdir, "big.csv")
        f2 = os.path.join(tmpdir, "small.csv")
        with open(f1, "wb") as fh:
            fh.write(b"x,y\n" * 50_000)   # 200 KB (large → own object)
        with open(f2, "wb") as fh:
            fh.write(b"a,b\n" * 100)      # 400 B (small → batched)
        files = [f1, f2]

        ex = _real_executor()
        ex.execute("big = list(range(40000))", "c1")   # > 64 KB pickle
        ex.execute("small = {'k': 1}", "c2")
        s3 = FakeS3()
        mgr = _manager(ex)
        with _Patched(s3, lambda: files):
            # (a) first save
            mgr.save("sess-total")
            m1 = _meta(s3)
            tot1 = m1.get("checkpoint_total_kb")
            check("(a) metadata has checkpoint_total_kb (a float > 0)",
                  isinstance(tot1, float) and tot1 > 0)
            check("(a) total ≈ (serialize + files + tail) / 1024",
                  tot1 is not None and abs(tot1 - _expected_kb(mgr, files)) <= 0.1)
            check("(a) total ≥ checkpoint_size_kb (uploaded pickles are a subset)",
                  tot1 >= m1.get("checkpoint_size_kb", 0))
            check("(a) total covers the 200 KB file (> 195 KB)", tot1 > 195)
            # (d) summary matches metadata
            check("(d) last_save_summary total_kb == metadata checkpoint_total_kb",
                  mgr.last_save_summary.get("total_kb") == tot1)
            check("(d) checkpoint_total_kb not added to step_sizes (written-bytes sum)",
                  "total" not in mgr.last_save_sizes and "checkpoint_total" not in mgr.last_save_sizes)

            # (b) no-change save: delta 0, total unchanged
            mgr.save("sess-total")
            m2 = _meta(s3)
            check("(b) no-change save: checkpoint_size_kb == 0.0", m2.get("checkpoint_size_kb") == 0.0)
            tot2 = m2.get("checkpoint_total_kb")
            check("(b) no-change save: checkpoint_total_kb > 0", isinstance(tot2, float) and tot2 > 0)
            check("(b) no-change save: total unchanged (±0.2 KB for the dag.json clock)",
                  tot2 is not None and abs(tot2 - tot1) <= 0.2)

            # (c) change one small var
            ex.execute("small = {'k': 1, 'j': 'x' * 2000}", "c3")
            mgr.save("sess-total")
            m3 = _meta(s3)
            tot3 = m3.get("checkpoint_total_kb")
            check("(c) small change: total grows by ~2 KB", tot3 is not None and 1.5 <= tot3 - tot2 <= 4.0)
            check("(c) small change: the delta stays small (< 10 KB, big not re-uploaded)",
                  m3.get("checkpoint_size_kb", 99) < 10)
            check("(c) summary total_kb follows", mgr.last_save_summary.get("total_kb") == tot3)

            # (e) remove the big file → total drops by ~195 KB
            os.remove(f1)
            files = [f2]
            mgr.save("sess-total")
            tot4 = _meta(s3).get("checkpoint_total_kb")
            check("(e) removed file lowers the total by ~195 KB",
                  tot4 is not None and 190 <= tot3 - tot4 <= 200)
            check("(e) total still matches the formula", abs(tot4 - _expected_kb(mgr, files)) <= 0.1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkpoint_total_size():
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
