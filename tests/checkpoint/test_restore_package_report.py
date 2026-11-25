#!/usr/bin/env python3
# test-kind: offline
"""
CheckpointManager._restore_packages / restore(): the package re-install report
served at GET /restore-report. Fake S3 and a fake pip (subprocess.run is replaced
in the manager module); no network, no real installs.
Run: PYTHONPATH=. .venv/bin/python tests/checkpoint/test_restore_package_report.py
"""
import logging
import os
import subprocess
import sys
import types
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.platform.checkpoint import manager as manager_mod  # noqa: E402
from test_dag_checkpoint import FakeS3, _manager  # noqa: E402

_checks = []
def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")

PREFIX = "sessions/sess-pkg"
NO_DIST = ("ERROR: Could not find a version that satisfies the requirement badpkg (from versions: none)\n"
           "ERROR: No matching distribution found for badpkg\n")


def _fake_run(results):
    """results: {package-or-'batch': (returncode, stdout, stderr) | 'timeout'}."""
    calls = []
    def run(cmd, **kw):
        pkgs = [c for c in cmd[cmd.index("--quiet") + 1:]]
        key = "batch" if len(pkgs) != 1 else pkgs[0]
        calls.append(key)
        res = results.get(key, (0, "", ""))
        if res == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
        return types.SimpleNamespace(returncode=res[0], stdout=res[1], stderr=res[2])
    return run, calls


def _restore(requirements, results, s3=None):
    s3 = s3 or FakeS3()
    if requirements is not None:
        s3.store[f"{PREFIX}/requirements.txt"] = requirements.encode()
    mgr = _manager(types.SimpleNamespace())
    run, calls = _fake_run(results)
    saved = manager_mod.subprocess.run
    manager_mod.subprocess.run = run
    try:
        mgr._restore_packages(s3, PREFIX)
    finally:
        manager_mod.subprocess.run = saved
    return mgr, mgr._pkg_result, calls


def main():
    logging.disable(logging.CRITICAL)
    mgr, rep, calls = _restore("tabulate==0.9.0\nsix\n", {})
    check("all ok: verified, total 2, none failed",
          rep["verified"] is True and rep["packages_total"] == 2 and rep["packages_failed"] == [])
    check("all ok: one batch call only", calls == ["batch"])
    check("report carries arch and python", bool(rep.get("arch")) and bool(rep.get("python")))

    mgr, rep, calls = _restore("tabulate\nbadpkg>=1.0\n", {"batch": (1, "", NO_DIST), "badpkg>=1.0": (1, "", NO_DIST)})
    names = [f["name"] for f in rep["packages_failed"]]
    check("batch fails, one package fails: only that name listed", names == ["badpkg"] and rep["packages_total"] == 2)
    check("failure carries the pip diagnostic text",
          "badpkg" in rep["packages_failed"][0]["reason"] and "no matching distribution" in rep["packages_failed"][0]["reason"])
    check("failed names stay in the next checkpoint's requirements",
          mgr._user_installed_packages == ["tabulate", "badpkg>=1.0"])

    mgr, rep, calls = _restore("slowpkg\n", {"batch": "timeout", "slowpkg": "timeout"})
    check("timeout: package reported failed with a timeout message",
          [f["name"] for f in rep["packages_failed"]] == ["slowpkg"]
          and "timed out" in rep["packages_failed"][0]["reason"].lower())

    mgr, rep, calls = _restore(None, {})
    check("no requirements.txt: verified true, total 0, no pip call",
          rep["verified"] is True and rep["packages_total"] == 0 and rep["packages_failed"] == [] and calls == [])

    class _BrokenS3(FakeS3):
        def get_object(self, Bucket, Key):
            raise RuntimeError("S3 unavailable")
    mgr, rep, calls = _restore(None, {}, s3=_BrokenS3())
    check("S3 error: verified false with a reason", rep["verified"] is False and bool(rep.get("reason")))

    passed = sum(1 for _, ok in _checks if ok)
    print(f"RESULT: {passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)


if __name__ == "__main__":
    main()
