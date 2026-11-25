#!/usr/bin/env python3
# test-kind: offline
"""
app/notebook/pip_diagnostics.py test (clear pip messages per CPU architecture).

Feeds the recorded pip outputs in tests/fixtures/pip_install/*.txt (both arm64
and x86_64) through classify_pip_output / describe_pip_failure and asserts:
  - every sample lands in the expected kind, on both arches;
  - a missing pin/specifier while pip lists other versions is version_not_found
    (last few versions shown, no arch in the message); "(from versions: none)"
    stays no_distribution;
  - a timeout while still downloading an sdist is sdist_download_timeout, and
    only a real build step (or compiler output) makes it source_build_timeout;
  - the message names the package (base name), the arch and, for timeouts,
    the limit; the no-wheel kinds say "No prebuilt wheel for X on <arch>";
  - the plain "timeout" and "other" kinds keep today's text (stderr as-is);
  - normalize_arch maps platform.machine() values;
  - no message ever claims another arch "has a prebuilt wheel" or suggests
    switching arch (the VM can't know what the deployment offers).

Pure: no pip, no network, no VM.

Run:
    .venv/bin/python tests/vm/test_pip_diagnostics.py
"""

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "app", "notebook"))

import pip_diagnostics as pd  # noqa: E402  (loaded standalone: no app package imports)

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "pip_install")

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# (fixture, package spec, timed_out, returncode, expected kind)
CASES = [
    ("no_distribution_arm64.txt", "tensorflow-intel==2.15.0", False, 1, "no_distribution"),
    ("no_distribution_x86_64.txt", "tensorflow-macos>=2.15", False, 1, "no_distribution"),
    # Formerly no_distribution_x86_64.txt: pip lists other versions, so the pin
    # is the problem (version_not_found), not the arch.
    ("version_not_found_x86_64_numpy_python.txt", "numpy==1.21.1", False, 1, "version_not_found"),
    ("version_not_found_pandas_pin.txt", "pandas==99", False, 1, "version_not_found"),
    ("version_not_found_requests_specifier.txt", "requests>=99", False, 1, "version_not_found"),
    ("version_not_found_extras_marker.txt", "sqlalchemy-utils[babel]~=9.0", False, 1, "version_not_found"),
    ("version_not_found_dependency.txt", "oldtool==1.0", False, 1, "version_not_found"),
    ("sdist_download_timeout_arm64.txt", "opencv-python==4.5.1.48", True, None, "sdist_download_timeout"),
    ("source_build_failed_arm64_gcc.txt", "pycrypto==2.6.1", False, 1, "source_build_failed"),
    ("source_build_failed_x86_64_python_h.txt", "netifaces==0.11.0", False, 1, "source_build_failed"),
    ("source_build_failed_x86_64_pg_config.txt", "psycopg2==2.9.9", False, 1, "source_build_failed"),
    ("source_build_failed_arm64_rust.txt", "tokenizers==0.13.3", False, 1, "source_build_failed"),
    ("source_build_timeout_arm64.txt", "pyarrow==12.0.1", True, None, "source_build_timeout"),
    ("source_build_timeout_x86_64.txt", "scikit-learn==0.24.2", True, None, "source_build_timeout"),
    ("download_timeout_x86_64.txt", "torch==2.3.1", True, None, "download_timeout"),
    ("download_timeout_arm64.txt", "torch==2.3.1", True, None, "download_timeout"),
    ("other_resolver_conflict.txt", "pandas==1.5.3", False, 1, "other"),
]

FORBIDDEN = ("has a prebuilt wheel", "switch to", "Running the notebook on")

_SKIPPED_PY = " Some versions were skipped because they need a different Python version."
# version_not_found: exact message (arch-independent), keyed by fixture.
VERSION_NOT_FOUND_MESSAGES = {
    "version_not_found_x86_64_numpy_python.txt":
        "No version of numpy matches '==1.21.1' for Python 3.11 "
        "(available: 1.3.0, 1.4.1, 1.5.0, 1.26.4, 2.0.0)." + _SKIPPED_PY,
    "version_not_found_pandas_pin.txt":
        "No version of pandas matches '==99' for Python 3.11 "
        "(available: …, 2.1.4, 2.2.0, 2.2.1, 2.2.2, 2.2.3).",
    "version_not_found_requests_specifier.txt":
        "No version of requests matches '>=99' for Python 3.11 "
        "(available: …, 2.31.0, 2.32.0, 2.32.1, 2.32.2, 2.32.3).",
    "version_not_found_extras_marker.txt":
        "No version of sqlalchemy-utils matches '~=9.0' for Python 3.11 "
        "(available: 0.36.8, 0.37.0, 0.41.1, 0.41.2)." + _SKIPPED_PY,
    "version_not_found_dependency.txt":
        "Installing oldtool failed: no version of its dependency numpy matches '<1.20' "
        "for Python 3.11 (available: …, 2.0.0, 2.0.1, 2.0.2, 2.1.0, 2.1.1).",
}


def section_fixtures():
    print("\n[fixtures × arch]")
    seen = set(os.listdir(FIXTURES))
    listed = {c[0] for c in CASES}
    check("every fixture file has a case", seen == listed)
    for fname, spec, timed_out, rc, kind in CASES:
        out = _load(fname)
        base = pd.base_package_name(spec)
        check(f"{fname}: classify → {kind}",
              pd.classify_pip_output(out, timed_out=timed_out, returncode=rc) == kind)
        for arch in ("arm64", "x86_64"):
            # stderr == output: pip writes errors to stderr, the "other" kind shows it.
            d = pd.describe_pip_failure(spec, out, out, timed_out=timed_out, returncode=rc,
                                        arch=arch, python_version="3.11", timeout_s=120)
            msg = d["message"]
            tag = f"{fname} [{arch}]"
            check(f"{tag}: kind {kind}", d["kind"] == kind)
            check(f"{tag}: arch field", d["arch"] == arch)
            check(f"{tag}: no other-arch claim", not any(f in msg for f in FORBIDDEN))
            # download_timeout is a network problem, so it names the package only.
            if kind in ("no_distribution", "source_build_failed", "source_build_timeout",
                        "sdist_download_timeout"):
                check(f"{tag}: names {base} and {arch}", base in msg and arch in msg)
            if kind != "other":
                check(f"{tag}: base name only (no version spec)", spec not in msg)
            if kind == "no_distribution":
                check(f"{tag}: message", msg == (
                    f"No version of {base} is available for Python 3.11 on {arch} "
                    f"(pip found no matching distribution)."))
            elif kind == "version_not_found":
                check(f"{tag}: message", msg == VERSION_NOT_FOUND_MESSAGES[fname])
                check(f"{tag}: doesn't blame the arch",
                      "arm64" not in msg and "x86_64" not in msg and "wheel" not in msg)
            elif kind == "sdist_download_timeout":
                check(f"{tag}: message", msg == (
                    f"Installing {base} timed out after 120s while downloading its source "
                    f"package (no prebuilt wheel for {arch}; the source build hadn't started yet)."))
            elif kind == "source_build_timeout":
                check(f"{tag}: message", msg == (
                    f"No prebuilt wheel for {base} on {arch}; building from source timed out "
                    f"after 120s. Try a version that has an {arch} wheel."))
            elif kind == "source_build_failed":
                check(f"{tag}: prefix",
                      msg.startswith(f"No prebuilt wheel for {base} on {arch}; building from source failed"))
                check(f"{tag}: ends with '.' and ≤ ~260 chars", msg.endswith(".") and len(msg) <= 260)
            elif kind == "download_timeout":
                check(f"{tag}: message", msg == (
                    f"Installing {base} timed out after 120s while downloading (no source build "
                    f"had started). Check network access and try again."))
            elif kind == "other":
                check(f"{tag}: stderr kept (stripped)", msg == out.strip())


def section_reasons():
    print("\n[short reasons]")
    def msg(fname, spec):
        return pd.describe_pip_failure(spec, _load(fname), "", timed_out=False, returncode=1,
                                       arch="arm64", python_version="3.11", timeout_s=120)["message"]
    m = msg("source_build_failed_arm64_gcc.txt", "pycrypto==2.6.1")
    check("gcc: missing compiler/headers + gcc line",
          "(missing compiler/headers): unable to execute 'gcc'" in m)
    m = msg("source_build_failed_x86_64_python_h.txt", "netifaces")
    check("Python.h: header line", "(missing compiler/headers):" in m and "fatal error: Python.h" in m)
    m = msg("source_build_failed_x86_64_pg_config.txt", "psycopg2")
    check("pg_config: reason", "pg_config executable not found" in m and not m.endswith(".."))
    m = msg("source_build_failed_arm64_rust.txt", "tokenizers")
    check("rust: reason", "can't find Rust compiler" in m)
    long = "x" * 400
    r = pd.short_reason(f"gcc: not found {long}")
    check("short_reason ≤160 chars", 0 < len(r) <= 160)
    check("short_reason strips control chars", "\x1b" not in pd.short_reason("\x1b[31merror: boom\x1b[0m"))
    check("generic build failure without toolchain → no '(missing …)'",
          "(missing" not in pd.describe_pip_failure(
              "foo", "Building wheel for foo (setup.py): started\nERROR: Failed building wheel for foo\n",
              "", timed_out=False, returncode=1, arch="x86_64", python_version="3.11",
              timeout_s=120)["message"])


def section_edges():
    print("\n[edge cases]")
    check("rc=0 → ok", pd.classify_pip_output("Successfully installed x", timed_out=False, returncode=0) == "ok")
    check("timeout with no output → timeout",
          pd.classify_pip_output("", timed_out=True, returncode=None) == "timeout")
    d = pd.describe_pip_failure("pandas==2.0", "", "", timed_out=True, returncode=None,
                                arch="arm64", python_version="3.11", timeout_s=120)
    check("plain timeout keeps today's text",
          d["message"] == "Installation of pandas==2.0 timed out (120s limit)")
    # Timeouts: an sdist download alone is not a build (review finding 2).
    check("timeout while downloading an sdist → sdist_download_timeout",
          pd.classify_pip_output("Collecting a\n  Downloading a-1.0.tar.gz (3 kB)\n",
                                 timed_out=True, returncode=None) == "sdist_download_timeout")
    check("timeout while downloading a .zip sdist → sdist_download_timeout",
          pd.classify_pip_output("Collecting a\n  Downloading a-1.0.zip (3 kB)\n",
                                 timed_out=True, returncode=None) == "sdist_download_timeout")
    check("sdist then wheel still downloading → download_timeout (last activity is a wheel)",
          pd.classify_pip_output("  Downloading a-1.0.tar.gz (3 kB)\n  Downloading b-2.0-py3-none-any.whl (9 MB)\n",
                                 timed_out=True, returncode=None) == "download_timeout")
    for marker in ("  Installing build dependencies: started",
                   "  Getting requirements to build wheel: started",
                   "  Preparing metadata (pyproject.toml): started",
                   "  Running setup.py install for a: started",
                   "  Building wheel for a (pyproject.toml): started",
                   "  gcc -pthread -fPIC -I/usr/include/python3.11 -c a.c -o a.o"):
        check(f"sdist + {marker.strip()[:30]!r} → source_build_timeout",
              pd.classify_pip_output(f"Collecting a\n  Downloading a-1.0.tar.gz (3 kB)\n{marker}\n",
                                     timed_out=True, returncode=None) == "source_build_timeout")
    check("failure path unchanged: sdist + subprocess error → source_build_failed",
          pd.classify_pip_output("  Downloading a-1.0.tar.gz (3 kB)\nerror: subprocess-exited-with-error\n",
                                 timed_out=False, returncode=1) == "source_build_failed")

    # parse_missing_version variants (review finding 1).
    pm = pd.parse_missing_version
    check("no 'Could not find' line → None", pm("ERROR: No matching distribution found for x") is None)
    check("'from versions: none' → empty list",
          pm("ERROR: Could not find a version that satisfies the requirement x==1 (from versions: none)")
          == {"name": "x", "spec": "==1", "parent": "", "versions": []})
    check("'(from versions: )' (empty) → empty list",
          pm("Could not find a version that satisfies the requirement x (from versions: )")["versions"] == [])
    check("legacy pip without 'ERROR:' prefix parses",
          pm("Could not find a version that satisfies the requirement foo>=2,<3 (from versions: 1.0, 1.1)")
          == {"name": "foo", "spec": ">=2,<3", "parent": "", "versions": ["1.0", "1.1"]})
    check("extras + marker stripped from spec",
          pm('Could not find a version that satisfies the requirement a[x,y]~=9.0; python_version >= "3.8" '
             '(from versions: 1.0)')["spec"] == "~=9.0")
    check("dependency '(from parent)' split off",
          pm("Could not find a version that satisfies the requirement b<1 (from a) (from versions: 2.0)")
          == {"name": "b", "spec": "<1", "parent": "a", "versions": ["2.0"]})
    check("no versions list but 'No matching distribution' → no_distribution",
          pd.classify_pip_output("ERROR: No matching distribution found for x\n",
                                 timed_out=False, returncode=1) == "no_distribution")
    check("versions none → no_distribution",
          pd.classify_pip_output("ERROR: Could not find a version that satisfies the requirement x (from versions: none)\n",
                                 timed_out=False, returncode=1) == "no_distribution")
    d = pd.describe_pip_failure(
        "x", "ERROR: Could not find a version that satisfies the requirement x (from versions: 1.0a1, 1.0b2)\n",
        "", timed_out=False, returncode=1, arch="arm64", python_version="3.11", timeout_s=120)
    check("no specifier, only pre-releases listed → shows them, generic wording",
          d["kind"] == "version_not_found" and d["message"] ==
          "No version of x matches the requirement for Python 3.11 (available: 1.0a1, 1.0b2).")
    d = pd.describe_pip_failure(
        "Foo_Bar==9", "ERROR: Could not find a version that satisfies the requirement foo-bar==9 (from versions: 1.0)\n",
        "", timed_out=False, returncode=1, arch="arm64", python_version="3.11", timeout_s=120)
    check("name compared PEP 503-normalised (not treated as a dependency)",
          d["message"] == "No version of foo-bar matches '==9' for Python 3.11 (available: 1.0).")
    check("subprocess error without build marker → other",
          pd.classify_pip_output("error: subprocess-exited-with-error\n", timed_out=False, returncode=1) == "other")
    check("other with empty stderr falls back to output tail",
          pd.describe_pip_failure("x", "ERROR: boom", "", timed_out=False, returncode=2, arch="arm64",
                                  python_version="3.11", timeout_s=120)["message"] == "ERROR: boom")
    check("base_package_name strips extras/spec", pd.base_package_name("pandas[sql]>=2.0") == "pandas")


def section_arch():
    print("\n[normalize_arch]")
    for given, want in (("x86_64", "x86_64"), ("AMD64", "x86_64"), ("amd64", "x86_64"),
                        ("aarch64", "arm64"), ("arm64", "arm64"), ("riscv64", "riscv64")):
        check(f"normalize_arch({given!r}) == {want!r}", pd.normalize_arch(given) == want)


_SECTIONS = [section_fixtures, section_reasons, section_edges, section_arch]


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
    print("ALL PIP DIAGNOSTICS CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
