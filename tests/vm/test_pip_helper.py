#!/usr/bin/env python3
# test-kind: offline
"""
app/platform/pip_helper.py: the pre-imported pip helper process, its fallback,
the restore pin check, the pip log timeline and the VM-wide pip lock.

Offline: the real helper is started with this interpreter's pip on a temp Unix
socket; every pip run is offline (--no-index / PIP_NO_INDEX=1, a local wheel
installed with --target into a temp dir). Nothing is installed into the venv.

Sections:
  [pins]      unsatisfied_requirements: exact pins satisfied / missing / other
              version, PEP 503 names (charset_normalizer vs charset-normalizer vs
              Charset.Normalizer, scikit_learn vs scikit-learn), non-exact specs
              (ranges, extras, wildcards, ===, bare names) always need pip, the
              distribution-scan fallback.
  [timeline]  pip_log_key_lines: offsets from the start, key lines only, first
              HTTPS connection only, long logs elided in the middle.
  [helper]    start_at_build -> ping; --version / an offline error / a local wheel
              via the helper; rc, stdout and stderr exactly as pip writes them;
              cwd and env come from the request; AWS_* never in the helper's env;
              a timeout kills the run and the helper keeps serving; pip's logging
              handlers / warnings filters / sys.exit never touch the helper.
  [executor]  SandboxExecutor.install_package through the helper: the same error
              text and kind (no_distribution) as the plain subprocess path, logs
              "via helper"; with the helper killed it falls back to the subprocess.
  [fallback]  no socket, stale socket, garbage reply, NOTEBOOK_PIP_HELPER=0 -> None.
  [lock]      pip_lock serialises two concurrent runs; PipBusy after wait_s; a
              busy install returns a clear error without running pip.
Run:
    .venv/bin/python tests/vm/test_pip_helper.py
"""
import importlib.metadata as md
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import zipfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.platform import pip_helper as ph  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  [{detail}]" if not ok and detail else ""))


_PRIVATE_TMP = None


def _leaked(d=None):
    """pip-helper temp output files in OUR private temp dir (never the shared /tmp,
    where parallel test runs create their own)."""
    d = d or _PRIVATE_TMP or tempfile.gettempdir()
    return [f for f in os.listdir(d) if f.startswith("pip-helper-")]


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# ----------------------------------------------------------------------------
def section_pins():
    print("\n[pins]")
    inst = {"statsmodels": "0.15.0", "bottle": "0.13.4", "scikit-learn": "1.5.2"}

    def lookup(name):
        return inst.get(ph.canonical_name(name))
    u = ph.unsatisfied_requirements
    check("all exact pins installed -> nothing to do",
          u(["statsmodels==0.15.0", "bottle==0.13.4"], lookup) == [])
    check("scikit_learn==1.5.2 matches scikit-learn (PEP 503)", u(["scikit_learn==1.5.2"], lookup) == [])
    check("Scikit.Learn == 1.5.2 (spaces, dots, case) matches", u(["Scikit.Learn == 1.5.2"], lookup) == [])
    check("missing package -> needs pip", u(["tabulate==0.9.0"], lookup) == ["tabulate==0.9.0"])
    check("wrong version -> needs pip", u(["bottle==0.13.3"], lookup) == ["bottle==0.13.3"])
    try:
        import packaging.version  # noqa: F401
        check("PEP 440 equal versions (1.5.2.0 vs 1.5.2) count as installed",
              u(["scikit-learn==1.5.2.0"], lookup) == [])
    except ImportError:
        check("no packaging: 1.5.2.0 vs 1.5.2 safely goes through pip",
              u(["scikit-learn==1.5.2.0"], lookup) == ["scikit-learn==1.5.2.0"])
    non_exact = ["bottle", "bottle>=0.13", "bottle~=0.13.4", "bottle[extra]==0.13.4",
                 "bottle==0.13.*", "bottle===0.13.4", "bottle==0.13.4; python_version>'3'",
                 "git+https://example.invalid/x.git"]
    check("non-exact specs always go through pip", u(non_exact, lookup) == non_exact,
          u(non_exact, lookup))
    check("mixed list: only the specs pip must handle are returned",
          u(["statsmodels==0.15.0", "six"], lookup) == ["six"])
    check("lookup raising -> needs pip", u(["bottle==0.13.4"], lambda n: 1 / 0) == ["bottle==0.13.4"])

    # Real importlib.metadata lookup against this interpreter.
    v = md.version("charset-normalizer")
    real = ph.unsatisfied_requirements
    check("real: charset_normalizer / charset-normalizer / Charset.Normalizer pins installed",
          real([f"charset_normalizer=={v}", f"charset-normalizer=={v}", f"Charset.Normalizer=={v}"]) == [])
    check("real: pip==<installed> satisfied", real([f"pip=={md.version('pip')}"]) == [])
    check("real: other version -> needs pip", real(["pip==0.0.1"]) == ["pip==0.0.1"])
    check("real: missing distribution -> needs pip",
          real(["definitely-not-installed-zz==1.0"]) == ["definitely-not-installed-zz==1.0"])
    try:
        sk = md.version("scikit-learn")
        check("real: scikit_learn vs scikit-learn", real([f"scikit_learn=={sk}", f"scikit-learn=={sk}"]) == [])
    except md.PackageNotFoundError:
        print("  (scikit-learn not in this venv: covered by the injected lookup above)")
    # Distribution-scan fallback when the direct lookup misses.
    saved = md.version

    def _miss(name):
        raise md.PackageNotFoundError(name)
    md.version = _miss
    try:
        check("real: scan fallback finds charset_normalizer by normalised name",
              real([f"Charset_Normalizer=={v}"]) == [])
    finally:
        md.version = saved


# ----------------------------------------------------------------------------
def section_timeline():
    print("\n[timeline]")
    from datetime import datetime
    t0 = datetime(2026, 11, 21, 9, 0, 0).timestamp()
    log = "\n".join([
        "2026-11-21T09:00:00,300 Using pip 22.3.1 from /app/venv/lib/python3.11/site-packages/pip (python 3.11)",
        "2026-11-21T09:00:00,310 Non-user install because user site-packages disabled",
        "2026-11-21T09:00:00,400 Starting new HTTPS connection (1): pypi.org:443",
        "2026-11-21T09:00:00,450 Starting new HTTPS connection (1): files.pythonhosted.org:443",
        "2026-11-21T09:00:00,500 Collecting statsmodels==0.15.0",
        "2026-11-21T09:00:00,700 Downloading statsmodels-0.15.0-cp311-manylinux.whl (10.2 MB)",
        "2026-11-21T09:00:00,701   continuation line without a timestamp",
        "2026-11-21T09:00:01,000 Requirement already satisfied: numpy>=1.22 in /app/venv",
        "2026-11-21T09:00:01,100 Installing collected packages: patsy, statsmodels",
        "2026-11-21T09:00:02,900 Successfully installed patsy-1.0.3 statsmodels-0.15.0",
    ])
    lines = ph.pip_log_key_lines(log, t0)
    check("offsets from the caller's start", lines[0].startswith("+0.30s Using pip 22.3.1"), lines[:1])
    check("non-key lines dropped", not any("Non-user install" in ln or "continuation" in ln for ln in lines))
    check("only the first HTTPS connection kept",
          sum("Starting new HTTPS connection" in ln for ln in lines) == 1)
    check("download / install / success lines kept with offsets",
          "+0.70s Downloading statsmodels-0.15.0-cp311-manylinux.whl (10.2 MB)" in lines
          and "+1.10s Installing collected packages: patsy, statsmodels" in lines
          and lines[-1] == "+2.90s Successfully installed patsy-1.0.3 statsmodels-0.15.0", lines)
    check("default base = first key line", ph.pip_log_key_lines(log)[0].startswith("+0.00s Using pip"))
    many = "\n".join(f"2026-11-21T09:00:00,{i:03d} Collecting p{i}" for i in range(200))
    out = ph.pip_log_key_lines(many, t0)
    check("long logs: first and last 30 kept, middle elided",
          len(out) == 61 and "p0" in out[0] and "p199" in out[-1] and "more lines" in out[30], len(out))
    check("empty / no timestamps -> []", ph.pip_log_key_lines("") == [] and ph.pip_log_key_lines("x\ny") == [])


# ----------------------------------------------------------------------------
def _make_wheel(dirpath, name="zyg_demo", version="1.0"):
    whl = os.path.join(dirpath, f"{name}-{version}-py3-none-any.whl")
    di = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "VALUE = 42\n",
        f"{di}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{di}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{p},,\n" for p in files) + f"{di}/RECORD,,\n"
    with zipfile.ZipFile(whl, "w") as z:
        for p, body in files.items():
            z.writestr(p, body)
        z.writestr(f"{di}/RECORD", record)
    return whl


class Helper:
    """A real helper on a temp socket for the duration of a block."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="ph", dir="/tmp")
        self.sock = os.path.join(self.dir, "h.sock")
        self._env = {k: os.environ.get(k) for k in ("NOTEBOOK_PIP_HELPER_SOCK", "AWS_ACCESS_KEY_ID",
                                                    "AWS_SESSION_TOKEN", "NOTEBOOK_PIP_HELPER")}
        os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = self.sock
        os.environ.pop("NOTEBOOK_PIP_HELPER", None)
        # Fake credentials in the server env: the helper must not inherit them.
        os.environ["AWS_ACCESS_KEY_ID"] = "AKIAFAKEFAKEFAKE"
        os.environ["AWS_SESSION_TOKEN"] = "fake-token"
        self.info = ph.start_at_build(wait_s=60)
        os.environ.pop("AWS_ACCESS_KEY_ID", None)
        os.environ.pop("AWS_SESSION_TOKEN", None)
        return self

    def kill(self):
        pid = self.info.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            if ph._HELPER_PROC is not None:
                try:
                    ph._HELPER_PROC.wait(timeout=5)
                except Exception:
                    pass

    def __exit__(self, *exc):
        self.kill()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


def section_helper():
    print("\n[helper]")
    with Helper() as h:
        check("start_at_build: ready with pip preloaded",
              not h.info.get("error") and h.info.get("preloaded", 0) > 20 and os.path.exists(h.sock), h.info)
        check("socket is 0600", (os.stat(h.sock).st_mode & 0o777) == 0o600, oct(os.stat(h.sock).st_mode))
        st0 = ph.ping()
        check("ping: pip loaded, no AWS_* in the helper's environment",
              st0 and st0.get("pip_loaded") and st0.get("aws_env_vars") == 0, st0)

        with ph.pip_lock():
            r = ph.run_via_helper(["--version"], timeout=60)
        check("--version via helper: rc 0, stdout 'pip <ver>'",
              r is not None and r.returncode == 0 and r.stdout.startswith(f"pip {md.version('pip')}")
              and not r.timed_out, r)

        env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        with ph.pip_lock():
            r = ph.run_via_helper(["install", "--no-input", "nonexistent-zz-pkg"], timeout=60, env=env)
        check("offline error via helper: rc 1 and pip's ERROR lines on stderr",
              r is not None and r.returncode == 1 and "No matching distribution found for nonexistent-zz-pkg" in r.stderr,
              r)
        # Same command as a plain subprocess: identical rc and stderr.
        import subprocess
        p = subprocess.run([sys.executable, "-m", "pip", "install", "--no-input", "nonexistent-zz-pkg"],
                           capture_output=True, text=True, env=env, timeout=120)
        check("helper output == `python -m pip` output (rc, stdout, stderr)",
              (r.returncode, r.stdout, r.stderr) == (p.returncode, p.stdout, p.stderr),
              f"{(r.returncode, r.stdout, r.stderr)!r} vs {(p.returncode, p.stdout, p.stderr)!r}")

        # A real install from a local wheel into a temp target; cwd from the request.
        work = tempfile.mkdtemp(prefix="phw", dir="/tmp")
        try:
            _make_wheel(work)
            target = os.path.join(work, "target")
            with ph.pip_lock():
                r = ph.run_via_helper(["install", "--no-index", "--find-links", ".", "--target", target,
                                       "--disable-pip-version-check", "zyg_demo==1.0"],
                                      timeout=60, env=env, cwd=work)
            check("local wheel via helper: rc 0, 'Successfully installed zyg_demo-1.0', files in target",
                  r is not None and r.returncode == 0 and "Successfully installed zyg_demo-1.0" in r.stdout
                  and os.path.exists(os.path.join(target, "zyg_demo", "__init__.py")), r)

            # Timeout: pip blocks reading a FIFO requirements file with no writer.
            fifo = os.path.join(work, "reqs.fifo")
            os.mkfifo(fifo)
            t = time.perf_counter()
            with ph.pip_lock():
                r = ph.run_via_helper(["install", "-r", fifo], timeout=2, env=env)
            el = time.perf_counter() - t
            check("timeout: timed_out, rc None, returns at the deadline",
                  r is not None and r.timed_out and r.returncode is None and 1.8 <= el < 15, (r, el))
        finally:
            shutil.rmtree(work, ignore_errors=True)

        st1 = ph.ping()
        check("helper alive after a timeout and after pip's sys.exit (same pid)",
              st1 and st1.get("pid") == st0.get("pid"), (st0, st1))
        check("pip's logging handlers / warnings filters never reach the helper",
              st1.get("root_handlers") == st0.get("root_handlers") == 0
              and st1.get("warning_filters") == st0.get("warning_filters"), (st0, st1))
        check("no temp output files left behind", not _leaked())


# ----------------------------------------------------------------------------
def section_executor():
    print("\n[executor]")
    import app.notebook.executor as ex
    cap = _LogCapture()
    logging.getLogger("app.notebook.executor").addHandler(cap)
    logging.getLogger("app.notebook.executor").setLevel(logging.DEBUG)
    os.environ["PIP_NO_INDEX"] = "1"
    try:
        # Reference: the plain subprocess path (no helper socket).
        os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = "/tmp/definitely-no-helper.sock"
        ref = ex.SandboxExecutor().install_package("nonexistent-zz-pkg")
        with Helper() as h:
            cap.lines.clear()
            via = ex.SandboxExecutor().install_package("nonexistent-zz-pkg")
            check("install via helper: same success/error/kind as the subprocess path",
                  (via.success, via.error, via.install_error_kind) == (ref.success, ref.error, ref.install_error_kind)
                  and via.install_error_kind == "no_distribution", (via.error, ref.error))
            check("install via helper: pip output kept in details",
                  "No matching distribution found" in via.install_details)
            check("log says 'via helper'", any("via helper" in ln for ln in cap.lines), cap.lines[-3:])
            h.kill()
            cap.lines.clear()
            fb = ex.SandboxExecutor().install_package("nonexistent-zz-pkg")
            check("helper killed -> falls back to the subprocess, same error",
                  (fb.success, fb.error, fb.install_error_kind) == (ref.success, ref.error, ref.install_error_kind))
            check("log says 'via subprocess'", any("via subprocess" in ln for ln in cap.lines), cap.lines[-3:])
    finally:
        os.environ.pop("PIP_NO_INDEX", None)
        os.environ.pop("NOTEBOOK_PIP_HELPER_SOCK", None)
        logging.getLogger("app.notebook.executor").removeHandler(cap)


# ----------------------------------------------------------------------------
def section_fallback():
    print("\n[fallback]")
    d = tempfile.mkdtemp(prefix="phf", dir="/tmp")
    try:
        os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = os.path.join(d, "missing.sock")
        check("no socket -> None", ph.run_via_helper(["--version"], timeout=5) is None)
        stale = os.path.join(d, "stale.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(stale)
        s.close()
        os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = stale
        check("stale socket (nobody listening) -> None", ph.run_via_helper(["--version"], timeout=5) is None)
        bad = os.path.join(d, "bad.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(bad)
        srv.listen(1)

        def _garbage():
            c, _ = srv.accept()
            c.recv(65536)
            c.sendall(b"not json\n")
            c.close()
        th = threading.Thread(target=_garbage, daemon=True)
        th.start()
        os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = bad
        check("garbage reply -> None (caller runs the subprocess)",
              ph.run_via_helper(["--version"], timeout=5) is None)
        th.join(5)

        def _refuse():
            c, _ = srv.accept()
            c.recv(65536)
            c.sendall(b'{"ok": false, "error": "fork failed"}\n')
            c.close()
        th = threading.Thread(target=_refuse, daemon=True)
        th.start()
        check("helper reports it could not run the request -> None",
              ph.run_via_helper(["--version"], timeout=5) is None)
        th.join(5)
        srv.close()
        os.environ["NOTEBOOK_PIP_HELPER"] = "0"
        check("NOTEBOOK_PIP_HELPER=0 -> None and start_at_build does nothing",
              ph.run_via_helper(["--version"], timeout=5) is None and ph.start_at_build() == {"disabled": True})
        check("no temp output files left behind", not _leaked())
    finally:
        os.environ.pop("NOTEBOOK_PIP_HELPER", None)
        os.environ.pop("NOTEBOOK_PIP_HELPER_SOCK", None)
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------
def section_lock():
    print("\n[lock]")
    spans = []

    def worker(tag):
        with ph.pip_lock():
            a = time.perf_counter()
            time.sleep(0.3)
            spans.append((tag, a, time.perf_counter()))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    spans.sort(key=lambda s: s[1])
    check("two concurrent pip runs serialise (no overlap)",
          len(spans) == 2 and spans[1][1] >= spans[0][2] - 1e-3, spans)

    import app.notebook.executor as ex
    held = threading.Event()
    release = threading.Event()

    def holder():
        with ph.pip_lock():
            held.set()
            release.wait(10)
    th = threading.Thread(target=holder)
    th.start()
    held.wait(5)
    try:
        t = time.perf_counter()
        try:
            with ph.pip_lock(wait_s=0.2):
                busy = False
        except ph.PipBusy:
            busy = True
        check("PipBusy after wait_s while another run holds the lock",
              busy and 0.15 <= time.perf_counter() - t < 2)

        calls = []
        orig_popen, orig_wait = ex.subprocess.Popen, ex.PIP_LOCK_WAIT_S

        def _no_popen(*a, **k):
            calls.append(a)
            raise AssertionError("pip must not run while busy")
        ex.subprocess.Popen, ex.PIP_LOCK_WAIT_S = _no_popen, 0.2
        os.environ["NOTEBOOK_PIP_HELPER_SOCK"] = "/tmp/definitely-no-helper.sock"
        try:
            r = ex.SandboxExecutor().install_package("bottle")
        finally:
            ex.subprocess.Popen, ex.PIP_LOCK_WAIT_S = orig_popen, orig_wait
            os.environ.pop("NOTEBOOK_PIP_HELPER_SOCK", None)
        check("busy install: success False, clear message, pip not started",
              r.success is False and "Another package install is still running" in r.error and not calls, r.error)
    finally:
        release.set()
        th.join(5)
    check("lock free again afterwards", ph._PIP_LOCK.acquire(timeout=1) and (ph._PIP_LOCK.release() or True))


_SECTIONS = [section_pins, section_timeline, section_helper, section_executor, section_fallback, section_lock]


def section_hermetic_tmp():
    print("\n[hermetic-tmp]")
    check("code under test writes into the private temp dir, not the shared one",
          tempfile.gettempdir() == _PRIVATE_TMP and os.environ.get("TMPDIR") == _PRIVATE_TMP)
    # Negative: a file the helper "leaves behind" in the private dir IS detected;
    # a pip-helper-* file in some other directory is not.
    leak = os.path.join(_PRIVATE_TMP, "pip-helper-negative.out")
    other = tempfile.mkdtemp(prefix="phother", dir="/tmp")
    try:
        open(os.path.join(other, "pip-helper-elsewhere.out"), "w").close()
        check("leak check ignores files outside the private dir", _leaked() == [])
        open(leak, "w").close()
        check("leak check detects a file left in the private dir", _leaked() == ["pip-helper-negative.out"])
    finally:
        if os.path.exists(leak):
            os.remove(leak)
        shutil.rmtree(other, ignore_errors=True)
    check("leak check clean again", _leaked() == [])


def main():
    global _PRIVATE_TMP
    logging.basicConfig(level=logging.WARNING)
    # Hermetic temp dir: the helper client (tempfile.mkstemp) and the helper process
    # (reads TMPDIR from the request env) both use it.
    _PRIVATE_TMP = tempfile.mkdtemp(prefix="pip-helper-test-")
    saved = (os.environ.get("TMPDIR"), tempfile.tempdir)
    os.environ["TMPDIR"] = _PRIVATE_TMP
    tempfile.tempdir = _PRIVATE_TMP
    try:
        for fn in _SECTIONS:
            fn()
        section_hermetic_tmp()
    finally:
        if saved[0] is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = saved[0]
        tempfile.tempdir = saved[1]
        shutil.rmtree(_PRIVATE_TMP, ignore_errors=True)
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PIP HELPER CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
