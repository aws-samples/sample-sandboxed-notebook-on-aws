"""pip helper process: pip imported once at image build, one fork per install.

Why: every `python -m pip ...` subprocess imports ~650 modules (pip + stdlib) from
disk. On a freshly launched or resumed VM those files are not in the guest page
cache (the platform drops unmapped page cache before the snapshot) and the first
read of each disk block is slow (1.3-4 MB/s measured), so the import phase alone
took 14.8 s of a 16.8 s tiny install on an arm64 0.5 vCPU VM. Process memory IS
captured in the Firecracker snapshot, so a process that imported pip at build time
still has those modules in RAM on every VM launched from the image.
(.agents/tasks/pip-prewarm/report.md has the measurements.)

Design:
  * start_at_build() (called from app/server.py run_all_warmups) spawns
    `python pip_helper.py serve <sock>` as a separate interpreter (exec, never a
    fork of the server, so no boto3 / credential state is inherited). It IMPORTS
    pip only: no pip command, no network, and every AWS_* variable is removed from
    its environment. It then listens on a root-only (0600) Unix socket; nothing is
    reachable over the network.
  * Each request runs in a fork()ed child of the helper, so pip runs in a clean
    process exactly like `python -m pip`: pip's logging handlers, warnings filters,
    sys.exit and other global state stay in the child and die with it. The child
    gets the CALLER's environment and cwd at request time (the same as today's
    subprocess), its own session (so a timeout kills pip's build children too) and
    writes stdout / stderr to separate files, line-buffered, so a timeout still
    leaves the partial output.
  * run_via_helper() is the client. It returns None whenever the helper cannot
    take the request (no socket, connect refused, helper died before replying);
    the caller then runs its plain `python -m pip` subprocess, so an install is
    never lost. A timeout is reported as a timeout and never re-run.
  * pip_lock() serialises every pip call of the server process (restore, /install,
    the install_package helper, uninstall) so two installs never race on
    site-packages, whichever path (helper or subprocess) runs them.

Memory: the helper is a separate ~47 MB RSS process (measured on the VM). Set
NOTEBOOK_PIP_HELPER=0 in the image build environment to not start it (installs
then use the plain subprocess, as before).

This module must stay importable as a plain script (the helper runs it as
__main__): stdlib imports only at module level, nothing from app.*.
"""
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_SOCK = "/run/notebook-pip-helper.sock"
_HERE = os.path.dirname(os.path.abspath(__file__))
_PRELOAD_FILE = os.path.join(_HERE, "pip_preload_modules.txt")

# The roots of what a real `pip install statsmodels==0.15.0 bottle==0.13.4` imports
# (pip 22.3.1, -X importtime on the VM). pip_preload_modules.txt is the full list
# recorded there; importing these pulls in the stdlib modules they need.
PRELOAD_ROOTS = (
    "pip._internal.cli.main",
    "pip._internal.commands.install",
    "pip._internal.commands.uninstall",
    "pip._internal.commands.download",
    "pip._internal.cli.req_command",
    "pip._internal.network.session",
    "pip._internal.network.download",
    "pip._internal.index.package_finder",
    "pip._internal.index.collector",
    "pip._internal.resolution.resolvelib.resolver",
    "pip._internal.resolution.resolvelib.factory",
    "pip._internal.operations.prepare",
    "pip._internal.operations.install.wheel",
    "pip._internal.operations.check",
    "pip._internal.req.req_install",
    "pip._internal.req.req_uninstall",
    "pip._internal.metadata",
    "pip._internal.self_outdated_check",
    "pip._vendor.pkg_resources",
    "pip._vendor.requests",
    "pip._vendor.urllib3",
    "pip._vendor.cachecontrol",
    "pip._vendor.certifi",
    "pip._vendor.rich.console",
    "pip._vendor.rich.progress",
    "pip._vendor.packaging.specifiers",
    "pip._vendor.resolvelib",
    "py_compile",
    "compileall",
)

# How long the build waits for the helper to finish importing pip, so the imports
# are in memory before /ready triggers the snapshot.
BUILD_WAIT_S = 90.0
# Connect timeout and the slack the client allows over the helper's own deadline.
_CONNECT_TIMEOUT_S = 5.0
_REPLY_SLACK_S = 30.0

# One pip at a time in this process (see pip_lock).
_PIP_LOCK = threading.Lock()
# Bumped on pip_lock acquire and release (see pip_generation).
_PIP_GEN = 0
# The helper's Popen object (kept so it is not garbage collected; never waited on).
_HELPER_PROC = None


def sock_path() -> str:
    """The helper's socket: NOTEBOOK_PIP_HELPER_SOCK, else /run (tmpfs, root only)
    when writable, else /tmp. Server and client compute the same path."""
    override = os.environ.get("NOTEBOOK_PIP_HELPER_SOCK")
    if override:
        return override
    if os.path.isdir("/run") and os.access("/run", os.W_OK):
        return DEFAULT_SOCK
    return "/tmp/notebook-pip-helper.sock"


def _disabled() -> bool:
    return os.environ.get("NOTEBOOK_PIP_HELPER", "1").strip() == "0"


# =============================================================================
# Concurrency: one pip run at a time
# =============================================================================

class PipBusy(RuntimeError):
    """Another pip run held the lock for longer than the caller wanted to wait."""


@contextmanager
def pip_lock(wait_s: float | None = None):
    """Hold the process-wide pip lock. wait_s=None waits as long as needed; a
    number raises PipBusy when the lock is not free within that many seconds."""
    if wait_s is None:
        acquired = _PIP_LOCK.acquire()
    else:
        acquired = _PIP_LOCK.acquire(timeout=max(0.0, float(wait_s)))
    if not acquired:
        raise PipBusy("Another package install is still running on this VM. "
                      "Try again when it finishes.")
    global _PIP_GEN
    _PIP_GEN += 1  # odd while pip runs (see pip_generation)
    try:
        yield
    finally:
        _PIP_GEN += 1
        _PIP_LOCK.release()


def pip_generation() -> int:
    """A counter bumped when pip_lock is taken and again when it is released: odd
    while a locked pip run is in progress. The /packages cache keys on it and never
    stores a list built while it is odd (app/notebook/package_list.py)."""
    return _PIP_GEN


# =============================================================================
# Client
# =============================================================================

@dataclass
class HelperResult:
    returncode: int | None   # None when timed out
    stdout: str
    stderr: str
    timed_out: bool
    ms: float


def _recv_line(conn) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def _read_and_remove(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        data = b""
    try:
        os.unlink(path)
    except OSError:
        pass
    return data.decode("utf-8", "replace")


def run_via_helper(args, timeout: float, env: dict | None = None, cwd: str | None = None):
    """Run `pip <args>` in a fork of the pre-imported helper.

    Returns a HelperResult, or None when the helper cannot run it (not started,
    disabled, gone, or it failed before replying); the caller then falls back to
    its `python -m pip` subprocess. Never raises. The caller holds pip_lock()."""
    path = sock_path()
    if _disabled() or not os.path.exists(path):
        return None
    import tempfile
    out_fd, out_path = tempfile.mkstemp(prefix="pip-helper-", suffix=".out")
    err_fd, err_path = tempfile.mkstemp(prefix="pip-helper-", suffix=".err")
    os.close(out_fd)
    os.close(err_fd)
    t0 = time.perf_counter()
    conn = None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(_CONNECT_TIMEOUT_S)
        conn.connect(path)
        req = {"op": "pip", "args": [str(a) for a in args],
               "env": dict(os.environ if env is None else env),
               "cwd": cwd or os.getcwd(), "stdout": out_path, "stderr": err_path,
               "timeout": float(timeout)}
        conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
        conn.settimeout(float(timeout) + _REPLY_SLACK_S)
        try:
            raw = _recv_line(conn)
        except socket.timeout:
            # The helper should have killed pip at `timeout`; report a timeout
            # with whatever pip wrote (never re-run: it may still be running).
            logger.warning(f"pip helper: no reply {timeout + _REPLY_SLACK_S:.0f}s after the request")
            return HelperResult(None, _read_and_remove(out_path), _read_and_remove(err_path),
                                True, (time.perf_counter() - t0) * 1000)
        resp = json.loads(raw.decode("utf-8")) if raw.strip() else None
        if not isinstance(resp, dict) or not resp.get("ok"):
            reason = (resp or {}).get("error") if isinstance(resp, dict) else "no reply"
            logger.warning(f"pip helper: request not run ({reason}); using a pip subprocess")
            _read_and_remove(out_path)
            _read_and_remove(err_path)
            return None
        timed_out = bool(resp.get("timed_out"))
        rc = None if timed_out else int(resp.get("rc", 1))
        return HelperResult(rc, _read_and_remove(out_path), _read_and_remove(err_path),
                            timed_out, (time.perf_counter() - t0) * 1000)
    except Exception as e:  # connect refused, socket gone, bad reply: use the subprocess
        logger.info(f"pip helper unavailable ({type(e).__name__}: {e}); using a pip subprocess")
        _read_and_remove(out_path)
        _read_and_remove(err_path)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def ping(timeout: float = 5.0) -> dict | None:
    """The helper's status ({pid, modules, root_handlers, warning_filters, ...}),
    or None when it is not reachable. For logs and tests."""
    path = sock_path()
    if not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(path)
            conn.sendall(b'{"op": "ping"}\n')
            raw = _recv_line(conn)
        resp = json.loads(raw.decode("utf-8"))
        return resp if isinstance(resp, dict) else None
    except Exception:
        return None


# =============================================================================
# pip --log summary (restore timing)
# =============================================================================

_LOG_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d,\d{3}) (.*)$")
_KEY_LINE_RE = re.compile(
    r"^(Using pip |Collecting |Downloading |Using cached |Requirement already satisfied|"
    r"Installing collected packages|Building wheel|Created wheel|Attempting uninstall|"
    r"Successfully |ERROR:|WARNING:|Could not |Starting new HTTPS connection)")
_MAX_KEY_LINES = 60


def pip_log_key_lines(log_text: str, t0_epoch: float | None = None) -> list[str]:
    """The lines of a pip --log file that split a run into phases (interpreter
    start, resolve, download, install), each prefixed with its offset in seconds
    from t0_epoch (the moment the caller started pip; default: the first line).
    'Starting new HTTPS connection' is kept only for its first occurrence."""
    from datetime import datetime
    rows = []
    for line in (log_text or "").splitlines():
        m = _LOG_TS_RE.match(line)
        if not m:
            continue
        msg = m.group(2).strip()
        if not _KEY_LINE_RE.match(msg):
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S,%f").timestamp()
        except ValueError:
            continue
        rows.append((ts, msg))
    if not rows:
        return []
    seen_conn = False
    kept = []
    for ts, msg in rows:
        if msg.startswith("Starting new HTTPS connection"):
            if seen_conn:
                continue
            seen_conn = True
        kept.append((ts, msg))
    base = t0_epoch if t0_epoch is not None else kept[0][0]
    out = [f"+{max(0.0, ts - base):.2f}s {msg}" for ts, msg in kept]
    if len(out) > _MAX_KEY_LINES:
        half = _MAX_KEY_LINES // 2
        out = out[:half] + [f"... {len(out) - 2 * half} more lines ..."] + out[-half:]
    return out


# =============================================================================
# Restore: are the pins already installed?
# =============================================================================

_EXACT_PIN_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*==\s*"
                           r"([A-Za-z0-9][A-Za-z0-9.+!_-]*)\s*$")


def canonical_name(name: str) -> str:
    """PEP 503 normalisation: 'Scikit_Learn' -> 'scikit-learn'."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def _installed_versions_lookup():
    """A function name -> installed version | None for this interpreter. Tries the
    direct importlib.metadata lookup first, then a one-off scan of all
    distributions compared by PEP 503 normalised name."""
    import importlib
    import importlib.metadata as md
    importlib.invalidate_caches()
    scanned: dict | None = None

    def lookup(name: str):
        nonlocal scanned
        try:
            return md.version(name)
        except md.PackageNotFoundError:
            pass
        except Exception:
            pass
        if scanned is None:
            # Directory names only (`name-version.dist-info`): reading every
            # distribution's METADATA cost ~5 s on a cold-disk arm64 VM (live
            # restore, 120 dists), for a pin that is simply not installed yet.
            # A name that cannot be matched here is "missing", which only sends
            # the list through pip (the safe direction).
            scanned = {}
            for base in list(sys.path):
                try:
                    entries = list(os.scandir(base or "."))
                except OSError:
                    continue
                for e in entries:
                    for suffix in (".dist-info", ".egg-info"):
                        if e.name.endswith(suffix):
                            scanned.setdefault(canonical_name(e.name[:-len(suffix)].split("-")[0]), e.path)
        path = scanned.get(canonical_name(name))
        if path is None:
            return None
        try:
            return md.Distribution.at(path).version
        except Exception:
            return None
    return lookup


def _same_version(pinned: str, installed: str) -> bool:
    if pinned == installed:
        return True
    try:
        from packaging.version import Version  # present in the image (pandas/statsmodels dep)
        return Version(pinned) == Version(installed)
    except Exception:
        return False


def unsatisfied_requirements(packages, version_of=None) -> list[str]:
    """The specs of `packages` that still need pip: every spec that is not an
    exact `name==version` pin (ranges, extras, URLs, markers... are left to pip),
    and every exact pin whose distribution is missing or at another version.
    An empty list means pip has nothing to do. version_of(name) -> version | None
    is injectable for tests."""
    lookup = version_of or _installed_versions_lookup()
    need = []
    for spec in packages:
        m = _EXACT_PIN_RE.match(spec or "")
        if not m:
            need.append(spec)
            continue
        try:
            installed = lookup(m.group(1))
        except Exception:
            installed = None
        if not installed or not _same_version(m.group(2), str(installed)):
            need.append(spec)
    return need


# =============================================================================
# Build time: start the helper
# =============================================================================

def start_at_build(sock: str | None = None, wait_s: float = BUILD_WAIT_S) -> dict:
    """Spawn the helper and wait until it has imported pip and is listening, so
    the imports are in process memory before the snapshot. Returns its status
    dict ({preloaded, failed, preload_s, modules, pid}) or {"error": ...}.
    Offline and credential-free: the helper is a fresh interpreter (not a fork of
    the server), every AWS_* variable is removed from its environment, and it runs
    no pip command. Never raises."""
    global _HELPER_PROC
    if _disabled():
        return {"disabled": True}
    sock = sock or sock_path()
    try:
        import select
        r, w = os.pipe()
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        _HELPER_PROC = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "serve", sock, str(w)],
            pass_fds=(w,), env=env, cwd="/", start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        )
        os.close(w)
        try:
            if select.select([r], [], [], wait_s)[0]:
                line = os.read(r, 65536).decode("utf-8", "replace")
                if line.strip():
                    return json.loads(line)
                return {"error": "helper exited before it was ready", "pid": _HELPER_PROC.pid}
            return {"error": f"helper not ready after {wait_s:.0f}s", "pid": _HELPER_PROC.pid}
        finally:
            os.close(r)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# =============================================================================
# Helper process (runs as __main__)
# =============================================================================

def _module_list() -> list[str]:
    names = list(PRELOAD_ROOTS)
    try:
        with open(_PRELOAD_FILE, encoding="utf-8") as f:
            names += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        pass
    return names


def preload() -> tuple[int, list[str]]:
    """Import pip's modules (import only). Best-effort: a module that fails to
    import (optional C accelerators, other pip versions) is skipped."""
    import importlib
    import warnings
    ok, failed = 0, []
    seen = set()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name in _module_list():
            if name in seen:
                continue
            seen.add(name)
            try:
                importlib.import_module(name)
                ok += 1
            except BaseException as e:  # noqa: BLE001 - best-effort, like every warm-up
                failed.append(f"{name}: {type(e).__name__}")
    return ok, failed


def _status() -> dict:
    import warnings
    return {"ok": True, "pid": os.getpid(), "modules": len(sys.modules),
            "root_handlers": len(logging.getLogger().handlers),
            "warning_filters": len(warnings.filters),
            "pip_loaded": "pip._internal.cli.main" in sys.modules,
            # Count only (never names or values): must be 0 (see start_at_build).
            "aws_env_vars": sum(1 for k in os.environ if k.startswith("AWS_"))}


def _child(req: dict, inherited=()):
    """Forked child: run pip like `python -m pip` would, then _exit. Never returns."""
    rc = 1
    try:
        for s in inherited:  # the helper's listening / request sockets
            try:
                s.close()
            except OSError:
                pass
        os.setsid()  # own process group: a timeout kills pip's build children too
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        import io
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        for fd, key in ((1, "stdout"), (2, "stderr")):
            f = os.open(req[key], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.dup2(f, fd)
            os.close(f)

        def _stream(fd):
            return io.TextIOWrapper(io.FileIO(fd, "w", closefd=False), encoding="utf-8",
                                    errors="backslashreplace", line_buffering=True, write_through=True)
        sys.stdout = sys.__stdout__ = _stream(1)
        sys.stderr = sys.__stderr__ = _stream(2)
        sys.stdin = sys.__stdin__ = open(0, "r", closefd=False)
        os.environ.clear()
        os.environ.update({str(k): str(v) for k, v in (req.get("env") or {}).items()})
        try:
            os.chdir(req.get("cwd") or "/")
        except OSError:
            os.chdir("/")
        # Per-run freshness: the helper's state was frozen at image build and is
        # cloned into every VM, so re-seed RNGs and drop cached lookups.
        import random
        random.seed()
        if "ssl" in sys.modules:
            try:
                sys.modules["ssl"].RAND_add(os.urandom(64), 64.0)
            except Exception:
                pass
        import importlib
        import tempfile
        importlib.invalidate_caches()
        tempfile.tempdir = None
        pkr = sys.modules.get("pip._vendor.pkg_resources")
        if pkr is not None and hasattr(pkr, "_initialize_master_working_set"):
            try:
                pkr._initialize_master_working_set()
            except Exception:
                pass
        import pip
        sys.argv = [os.path.join(os.path.dirname(pip.__file__), "__main__.py")] + list(req["args"])
        from pip._internal.cli.main import main
        rc = main(list(req["args"]))
    except SystemExit as e:
        code = e.code
        rc = 0 if code is None else (code if isinstance(code, int) else 1)
    except BaseException:  # noqa: BLE001 - report like an uncaught error in `python -m pip`
        try:
            import traceback
            traceback.print_exc()
        except Exception:
            pass
        rc = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(rc if isinstance(rc, int) and 0 <= rc < 256 else 1)


def _run_request(req: dict, inherited=()) -> dict:
    t0 = time.perf_counter()
    timeout = float(req.get("timeout") or 900)
    pid = os.fork()
    if pid == 0:
        _child(req, inherited)  # never returns
    deadline = time.monotonic() + timeout
    timed_out = False
    status = None
    delay = 0.002
    while True:
        done, st = os.waitpid(pid, os.WNOHANG)
        if done:
            status = os.waitstatus_to_exitcode(st)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            _, st = os.waitpid(pid, 0)
            status = os.waitstatus_to_exitcode(st)
            break
        time.sleep(delay)
        delay = min(delay * 2, 0.02)
    return {"ok": True, "rc": status, "timed_out": timed_out,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def serve(sock: str, ready_fd: int | None = None):
    t0 = time.perf_counter()
    ok, failed = preload()
    parent = os.getppid()
    old_umask = os.umask(0o177)  # the socket is created 0600 (root only)
    try:
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock)
    finally:
        os.umask(old_umask)
    os.chmod(sock, 0o600)
    srv.listen(8)
    srv.settimeout(5.0)
    info = {"preloaded": ok, "failed": failed, "preload_s": round(time.perf_counter() - t0, 3),
            "modules": len(sys.modules), "pid": os.getpid(), "sock": sock}
    if ready_fd is not None:
        os.write(ready_fd, (json.dumps(info) + "\n").encode("utf-8"))
        os.close(ready_fd)
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            if os.getppid() != parent:  # the server is gone: do not linger
                break
            continue
        except OSError:
            continue
        try:
            conn.settimeout(10.0)
            raw = _recv_line(conn)
            req = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if req.get("op") == "ping":
                resp = _status()
            elif req.get("op") == "pip" and isinstance(req.get("args"), list) \
                    and req.get("stdout") and req.get("stderr"):
                resp = _run_request(req, (srv, conn))
            else:
                resp = {"ok": False, "error": "bad request"}
        except Exception as e:  # noqa: BLE001 - one bad request must not end the helper
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            conn.settimeout(10.0)
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            conn.close()
    try:
        os.unlink(sock)
    except OSError:
        pass


if __name__ == "__main__":
    # Run as a script, sys.path[0] is app/platform: drop it so nothing there can
    # shadow a module pip imports.
    if sys.path and os.path.abspath(sys.path[0] or ".") == _HERE:
        sys.path.pop(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "serve":
        serve(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
