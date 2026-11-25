#!/usr/bin/env python3
# test-kind: offline
"""
SandboxExecutor.install_package test (pip output classified per CPU arch).

Monkeypatches app.notebook.executor's subprocess.Popen, os.killpg and
platform.machine so no real pip runs, then asserts:
  - success: success=True, "Successfully installed name==x.y.z", installed_spec
    pinned exactly as before (uses an installed dist, "pip", for the pin);
  - pip is called with --progress-bar off --disable-pip-version-check
    --no-input (no --quiet), PYTHONUNBUFFERED=1 and a new session;
  - timeout: the first communicate raises TimeoutExpired, the process group is
    killed, the second communicate returns the recorded partial build output
    → kind source_build_timeout, the message names the arch and 120s, and the
    partial output is kept in install_details;
  - rc=1 with the recorded no-distribution output → kind no_distribution;
  - rc=1 with a missing pin (pip lists other versions) → kind version_not_found;
  - timeout while still downloading an sdist → kind sdist_download_timeout;
  - plain rc=1 with unrecognised stderr → kind other, error == stderr text;
for both platform.machine() = 'aarch64' and 'x86_64'.

Run:
    .venv/bin/python tests/vm/test_install_package_pip.py
"""

import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app.notebook.executor as ex  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "pip_install")

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakePopen:
    """Scripted stand-in for subprocess.Popen used by install_package."""
    script = None   # list of results for successive communicate() calls
    calls = []      # (args, kwargs) of every construction

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))
        self.args = args
        self.pid = 4242
        self.returncode = None
        self._script = list(FakePopen.script)
        self.killed = False

    def communicate(self, timeout=None):
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        rc, out, err = step
        self.returncode = rc
        return out, err

    def kill(self):
        self.killed = True


_killpg_calls = []


def _fake_killpg(pid, sig):
    _killpg_calls.append((pid, sig))


def _run(machine, script, package):
    FakePopen.script = script
    FakePopen.calls = []
    _killpg_calls.clear()
    orig = (ex.subprocess.Popen, ex.os.killpg, ex.platform.machine)
    ex.subprocess.Popen = FakePopen
    ex.os.killpg = _fake_killpg
    ex.platform.machine = lambda: machine
    try:
        return ex.SandboxExecutor().install_package(package)
    finally:
        ex.subprocess.Popen, ex.os.killpg, ex.platform.machine = orig


def section_success():
    print("\n[success path unchanged]")
    from importlib.metadata import version
    r = _run("aarch64", [(0, "Successfully installed pip\n", "")], "pip")
    want = f"pip=={version('pip')}"
    check("success True", r.success is True)
    check("installed_spec pinned name==x.y.z", r.installed_spec == want)
    check("output 'Successfully installed <pin>'", r.output == f"Successfully installed {want}")
    check("no error / kind on success", r.error == "" and r.install_error_kind == "" and r.install_arch == "")
    args, kwargs = FakePopen.calls[0]
    check("pip args: --progress-bar off --disable-pip-version-check --no-input, no --quiet",
          args[1:] == ["-m", "pip", "install", "--progress-bar", "off",
                       "--disable-pip-version-check", "--no-input", "pip"])
    check("PYTHONUNBUFFERED=1", kwargs.get("env", {}).get("PYTHONUNBUFFERED") == "1")
    check("new session (own process group)", kwargs.get("start_new_session") is True)
    check("stdout/stderr piped separately",
          kwargs.get("stdout") is subprocess.PIPE and kwargs.get("stderr") is subprocess.PIPE)


def section_failures():
    for machine, arch in (("aarch64", "arm64"), ("x86_64", "x86_64")):
        print(f"\n[failures on {machine}]")
        partial = _load(f"source_build_timeout_{arch}.txt")
        pkg = "pyarrow==12.0.1" if arch == "arm64" else "scikit-learn==0.24.2"
        base = pkg.split("==")[0]
        r = _run(machine, [subprocess.TimeoutExpired(["pip"], ex.PIP_INSTALL_TIMEOUT_S),
                           (-9, partial, "")], pkg)
        check(f"{arch} timeout: success False", r.success is False)
        check(f"{arch} timeout: kind source_build_timeout", r.install_error_kind == "source_build_timeout")
        check(f"{arch} timeout: message names arch + 120s",
              r.error == (f"No prebuilt wheel for {base} on {arch}; building from source "
                          f"timed out after 120s. Try a version that has an {arch} wheel."))
        check(f"{arch} timeout: install_arch", r.install_arch == arch)
        check(f"{arch} timeout: partial output kept in details", r.install_details == partial)
        check(f"{arch} timeout: process group killed", _killpg_calls and _killpg_calls[0][0] == 4242)

        nodist = _load(f"no_distribution_{arch}.txt")
        r = _run(machine, [(1, "", nodist)], "numpy==1.21.1")
        check(f"{arch} rc=1: kind no_distribution", r.install_error_kind == "no_distribution")
        check(f"{arch} rc=1: message",
              r.error == (f"No version of numpy is available for Python "
                          f"{sys.version_info.major}.{sys.version_info.minor} on {arch} "
                          f"(pip found no matching distribution)."))
        check(f"{arch} rc=1: details = stdout+stderr", r.install_details == nodist)
        check(f"{arch} rc=1: no killpg", not _killpg_calls)

        pin = _load("version_not_found_pandas_pin.txt")
        r = _run(machine, [(1, "", pin)], "pandas==99")
        check(f"{arch} missing pin: kind version_not_found", r.install_error_kind == "version_not_found")
        check(f"{arch} missing pin: message (no arch)",
              r.error == (f"No version of pandas matches '==99' for Python "
                          f"{sys.version_info.major}.{sys.version_info.minor} "
                          f"(available: …, 2.1.4, 2.2.0, 2.2.1, 2.2.2, 2.2.3)."))
        check(f"{arch} missing pin: full list kept in details", r.install_details == pin)

        sdist = _load("sdist_download_timeout_arm64.txt")
        r = _run(machine, [subprocess.TimeoutExpired(["pip"], ex.PIP_INSTALL_TIMEOUT_S),
                           (-9, sdist, "")], "opencv-python==4.5.1.48")
        check(f"{arch} sdist timeout: kind sdist_download_timeout",
              r.install_error_kind == "sdist_download_timeout")
        check(f"{arch} sdist timeout: message",
              r.error == (f"Installing opencv-python timed out after 120s while downloading its "
                          f"source package (no prebuilt wheel for {arch}; the source build "
                          f"hadn't started yet)."))

        r = _run(machine, [(1, "", "ERROR: something odd\n")], "foo")
        check(f"{arch} other: error == stderr text", r.error == "ERROR: something odd")
        check(f"{arch} other: kind other", r.install_error_kind == "other")


def section_validation():
    print("\n[validation unchanged]")
    r = ex.SandboxExecutor().install_package("--index-url=http://x foo")
    check("flag injection rejected before pip", r.success is False and "Invalid package name" in r.error)


_SECTIONS = [section_success, section_failures, section_validation]


def main():
    for fn in _SECTIONS:
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL INSTALL PACKAGE CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
