#!/usr/bin/env python3
# test-kind: offline
"""
CheckpointManager._restore_packages, faster reinstall: skip pip when every exact
pin is installed, cheap pip flags, the INFO timing / pip-log timeline, the pip
helper path and its fallback, the VM-wide pip lock. Fake S3; pip is either a fake
subprocess.run or the real pip helper run fully offline (PIP_NO_INDEX=1).

Checks:
  [skip]      requirements of exact pins that are installed (pip==<ver>,
              charset_normalizer==<ver> written with '_' / '.' / case) -> no pip
              call, report verified / total N / none failed, list kept for the next
              checkpoint, INFO "already installed ... skipping pip";
              a missing pin, another version or a non-exact spec -> pip with the
              WHOLE list (unchanged behaviour).
  [flags]     batch argv = install --disable-pip-version-check --no-input
              --progress-bar off --no-compile --log <file> --quiet <pkgs> (no
              --prefer-binary); INFO "pip batch via subprocess: rc=0 <ms>ms" and
              the timeline from pip's --log; the log file is removed afterwards;
              per-package retries carry the same flags.
  [helper]    with a real helper running: the batch and per-package retries go
              through it ("via helper"); a bad package fails with pip's
              no-matching-distribution text in packages_failed exactly like the
              subprocess path; the other package is not reported.
  [lock]      the restore holds the pip lock while pip runs (an /install waiting
              on it gets PipBusy).
Run: .venv/bin/python tests/checkpoint/test_restore_pip_fast.py
"""
import importlib.metadata as md
import logging
import os
import re
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "vm")))
from app.platform.checkpoint import manager as manager_mod  # noqa: E402
from app.platform import pip_helper as ph  # noqa: E402
from test_dag_checkpoint import FakeS3, _manager  # noqa: E402

PREFIX = "sessions/sess-fast"
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  [{detail}]" if not ok and detail else ""))


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


_cap = _Cap()
manager_mod.logger.addHandler(_cap)
manager_mod.logger.setLevel(logging.DEBUG)
manager_mod.logger.propagate = False

FAKE_LOG = ("2099-01-01T00:00:00,100 Using pip 22.3.1 from /x (python 3.11)\n"
            "2099-01-01T00:00:00,200 Collecting {p}\n"
            "2099-01-01T00:00:00,900 Successfully installed {p}\n")


def _fake_run(results, seen_logs):
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        pkgs = cmd[cmd.index("--quiet") + 1:]
        key = "batch" if len(pkgs) != 1 else pkgs[0]
        log = cmd[cmd.index("--log") + 1]
        seen_logs.append(log)
        with open(log, "a") as f:  # what pip --log writes
            f.write(FAKE_LOG.format(p=" ".join(pkgs)))
        res = results.get(key, (0, "", ""))
        return types.SimpleNamespace(returncode=res[0], stdout=res[1], stderr=res[2])
    return run, calls


def _restore(requirements, results=None, use_fake=True):
    s3 = FakeS3()
    s3.store[f"{PREFIX}/requirements.txt"] = requirements.encode()
    mgr = _manager(types.SimpleNamespace())
    logs = []
    run, calls = _fake_run(results or {}, logs)
    saved = manager_mod.subprocess.run
    if use_fake:
        manager_mod.subprocess.run = run
    _cap.lines.clear()
    try:
        mgr._restore_packages(s3, PREFIX)
    finally:
        manager_mod.subprocess.run = saved
    return mgr, mgr._pkg_result, calls, logs


def section_skip():
    print("\n[skip]")
    pv, cv = md.version("pip"), md.version("charset-normalizer")
    req = f"pip=={pv}\ncharset_normalizer=={cv}\nCharset.Normalizer=={cv}\n"
    os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = "/tmp/definitely-no-helper.sock"
    mgr, rep, calls, _ = _restore(req)
    check("all exact pins installed: no pip call", calls == [], calls)
    check("report: verified, total 3, none failed",
          rep["verified"] is True and rep["packages_total"] == 3 and rep["packages_failed"] == [], rep)
    check("list kept for the next checkpoint",
          mgr._user_installed_packages == [f"pip=={pv}", f"charset_normalizer=={cv}", f"Charset.Normalizer=={cv}"])
    check("INFO says the pins are installed and pip was skipped",
          any("already installed at the pinned versions" in ln and "skipping pip" in ln for ln in _cap.lines),
          _cap.lines)
    check("no 'Installing N user packages' line when skipped",
          not any("Installing 3 user packages" in ln for ln in _cap.lines))

    for label, extra in (("missing pin", "definitely-not-installed-zz==1.0"),
                         ("other version", "pip==0.0.1"),
                         ("non-exact spec", "pip>=1"),
                         ("bare name", "pip")):
        mgr, rep, calls, _ = _restore(f"pip=={pv}\n{extra}\n")
        pkgs = calls[0][calls[0].index("--quiet") + 1:] if calls else None
        check(f"{label}: pip runs once with the WHOLE list", len(calls) == 1 and pkgs == [f"pip=={pv}", extra],
              calls)
        check(f"{label}: report total 2, none failed",
              rep["packages_total"] == 2 and rep["packages_failed"] == [], rep)


def section_flags():
    print("\n[flags]")
    mgr, rep, calls, logs = _restore("tabulate==0.0.0-not-installed\nsix\n")
    cmd = calls[0]
    check("batch argv: -m pip install + cheap flags + --log + --quiet + pkgs",
          cmd[:3] == [sys.executable, "-m", "pip"]
          and cmd[3:10] == ["install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
                            "--no-compile", "--log"]
          and cmd[11:] == ["--quiet", "tabulate==0.0.0-not-installed", "six"], cmd)
    check("no --prefer-binary (measured: no gain)", "--prefer-binary" not in cmd)
    check("INFO: pip batch via subprocess: rc=0 <ms>ms",
          any(re.search(r"pip batch via subprocess: rc=0 \d+ms", ln) for ln in _cap.lines), _cap.lines)
    tl = [ln for ln in _cap.lines if "pip batch timeline" in ln]
    check("INFO: timeline from pip's --log (key lines with offsets)",
          tl and "Using pip 22.3.1" in tl[0] and "Collecting tabulate" in tl[0] and "Successfully installed" in tl[0]
          and re.search(r"\+\d+\.\d\ds ", tl[0]), tl)
    check("the --log file is removed afterwards", logs and not any(os.path.exists(p) for p in logs))

    mgr, rep, calls, logs = _restore("good\nbad\n", {"batch": (1, "", "ERROR: x\n"),
                                                     "bad": (1, "", "ERROR: No matching distribution found for bad\n")})
    check("batch fails: per-package retries carry the same flags",
          len(calls) == 3 and all(c[3:10] == ["install", "--disable-pip-version-check", "--no-input",
                                               "--progress-bar", "off", "--no-compile", "--log"]
                                  for c in calls), calls)
    check("batch fails: WARNING 'returned 1 — retrying per-package' kept",
          any("Batch install returned 1" in ln for ln in _cap.lines))
    check("failed package in the report with pip's reason",
          [f["name"] for f in rep["packages_failed"]] == ["bad"]
          and "no matching distribution" in rep["packages_failed"][0]["reason"], rep)
    check("failure output tail logged at INFO", any("pip bad output tail" in ln for ln in _cap.lines))
    check("all --log files removed", not any(os.path.exists(p) for p in logs))


def section_helper():
    print("\n[helper]")
    from test_pip_helper import Helper
    os.environ["PIP_NO_INDEX"] = "1"
    try:
        with Helper() as h:
            check("helper started", not h.info.get("error"), h.info)
            pv = md.version("pip")
            mgr, rep, calls, _ = _restore(f"pip=={pv}\nnonexistent-zz-pkg==1.0\n", use_fake=False)
            check("no subprocess.run fake needed: pip ran in the helper",
                  any("pip batch via helper: rc=1" in ln for ln in _cap.lines), _cap.lines)
            check("per-package retries via helper too",
                  any("pip nonexistent-zz-pkg via helper: rc=1" in ln for ln in _cap.lines)
                  and any(f"pip pip via helper: rc=0" in ln for ln in _cap.lines), _cap.lines)
            check("only the bad package reported, with pip's diagnostic",
                  [f["name"] for f in rep["packages_failed"]] == ["nonexistent-zz-pkg"]
                  and "no matching distribution" in rep["packages_failed"][0]["reason"], rep)
            helper_reason = rep["packages_failed"][0]["reason"]
            check("timeline lines from the real pip --log",
                  any("timeline" in ln and "Using pip" in ln for ln in _cap.lines))
            h.kill()
            mgr, rep, calls, _ = _restore(f"pip=={pv}\nnonexistent-zz-pkg==1.0\n", use_fake=False)
            check("helper gone: same restore via subprocess, same failure text",
                  any("pip batch via subprocess: rc=1" in ln for ln in _cap.lines)
                  and [f["reason"] for f in rep["packages_failed"]] == [helper_reason], rep)
    finally:
        os.environ.pop("PIP_NO_INDEX", None)


def section_lock():
    print("\n[lock]")
    seen = {}

    def run(cmd, **kw):
        try:
            with ph.pip_lock(wait_s=0.05):
                seen["free"] = True
        except ph.PipBusy:
            seen["free"] = False
        log = cmd[cmd.index("--log") + 1]
        open(log, "a").close()
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    s3 = FakeS3()
    s3.store[f"{PREFIX}/requirements.txt"] = b"six\n"
    mgr = _manager(types.SimpleNamespace())
    saved = manager_mod.subprocess.run
    manager_mod.subprocess.run = run
    try:
        mgr._restore_packages(s3, PREFIX)
    finally:
        manager_mod.subprocess.run = saved
    check("pip lock held while restore's pip runs (a concurrent install would wait)",
          seen.get("free") is False, seen)
    check("lock released after the restore", ph._PIP_LOCK.acquire(timeout=1) and (ph._PIP_LOCK.release() or True))


def section_scan():
    """The fallback lookup reads directory names, not every METADATA."""
    import tempfile
    d = tempfile.mkdtemp()
    for name in ("scikit_learn-1.5.0.dist-info", "Foo.Bar-2.0.egg-info"):
        os.makedirs(os.path.join(d, name))
    with open(os.path.join(d, "scikit_learn-1.5.0.dist-info", "METADATA"), "w") as f:
        f.write("Metadata-Version: 2.1\nName: scikit-learn\nVersion: 1.5.0\n")
    with open(os.path.join(d, "Foo.Bar-2.0.egg-info", "PKG-INFO"), "w") as f:
        f.write("Metadata-Version: 1.1\nName: Foo.Bar\nVersion: 2.0\n")
    sys.path.insert(0, d)
    try:
        lookup = ph._installed_versions_lookup()
        check("[scan] normalised name found by directory name", lookup("Scikit-Learn") == "1.5.0", lookup("Scikit-Learn"))
        check("[scan] dotted egg-info name found", lookup("foo_bar") == "2.0", lookup("foo_bar"))
        check("[scan] a missing name is None (pip will run)", lookup("no-such-dist-zz") is None)
        check("[scan] unsatisfied_requirements: pins met -> nothing for pip",
              ph.unsatisfied_requirements(["scikit_learn==1.5.0", "foo-bar==2.0"]) == [])
        check("[scan] wrong version -> still needs pip",
              ph.unsatisfied_requirements(["scikit-learn==1.4.0"]) == ["scikit-learn==1.4.0"])
    finally:
        sys.path.remove(d)
def main():
    try:
        for fn in (section_skip, section_flags, section_helper, section_lock, section_scan):
            fn()
    finally:
        os.environ.pop("NOTEBOOK_PIP_HELPER_SOCK", None)
    passed = sum(1 for _, ok in _checks if ok)
    print(f"RESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
    sys.exit(0 if passed == len(_checks) else 1)


if __name__ == "__main__":
    main()
