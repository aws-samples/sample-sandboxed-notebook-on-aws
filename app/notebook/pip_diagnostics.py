"""
pip install failure diagnostics (pure, stdlib only)

Part of: app.notebook (application layer)

The sandbox runs on two CPU architectures (arm64 / Graviton and x86_64). PyPI
wheels are per-architecture, so a package that installs instantly on one arch
can fall back to a source build on the other, and then fail (no compiler,
missing headers) or run past the install timeout. pip's raw stderr says none of
that plainly, so executor.install_package classifies the captured output here
and returns a message that names the arch and what actually happened.

Kinds returned by classify_pip_output:
    ok                     pip exited 0
    no_distribution        no installable version at all for this Python/arch
                           (pip printed "(from versions: none)" or no list)
    version_not_found      the requested version/specifier doesn't exist, but
                           other versions do ("(from versions: 1.0, …)"); not
                           an arch problem, so the UI adds no other-arch hint
    source_build_failed    no usable wheel, the source build ran and failed
    source_build_timeout   timed out while building from source (a build step
                           had started)
    sdist_download_timeout timed out while still downloading a source package
                           (pip found no wheel for this arch; the build hadn't
                           started)
    download_timeout       timed out while still downloading (no build started)
    timeout                timed out, nothing recognisable in the output
    other                  any other failure (message = pip's stderr, as before)

The VM can't know which arches the deployment offers, so messages never suggest
switching arch, and never claim another arch "has" a wheel. The UI adds a
hedged hint when the other arch is actually offered.
"""

import re

# ── Markers ────────────────────────────────────────────────────────────────

_NO_DISTRIBUTION_MARKERS = (
    "No matching distribution found",
    "Could not find a version that satisfies",
)

# pip's "Could not find a version that satisfies the requirement <req> (from
# versions: a, b, c)" line. <req> is str(requirement) and may carry extras, a
# specifier, a marker ("; python_version >= ...") and, for a dependency,
# " (from <parent>)". The list is "none" when nothing is installable at all.
_VERSIONS_RE = re.compile(
    r"Could not find a version that satisfies the requirement (?P<req>.+?) "
    r"\(from versions: (?P<versions>[^()]*)\)"
)
_REQ_NAME_RE = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PRERELEASE_RE = re.compile(r"(?:a|b|rc|c|alpha|beta|pre|preview|dev)\d*", re.I)
_DIFFERENT_PYTHON = "require a different python version"
_SHOWN_VERSIONS = 5

# Lines pip prints once a source build has actually started.
_BUILD_MARKERS = (
    "Building wheel for",
    "Building wheels for collected packages",
    "Running setup.py",
    "Preparing metadata (pyproject.toml)",
    "Preparing metadata (setup.py)",
    "Installing build dependencies",
    "Getting requirements to build wheel",
    "running build_ext",
    "running bdist_wheel",
)
# Compiler invocations echoed by a build backend ("gcc -pthread …").
_COMPILER_LINE_RE = re.compile(r"^\s*(?:\S+-)?(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+)\s+-", re.M)
# A downloaded sdist (".tar.gz" / ".zip") means a source build is next, but
# hasn't started yet. It counts as a build marker only for failures (a
# subprocess error after it is the build failing), not for timeouts.
_SDIST_DOWNLOAD_RE = re.compile(r"Downloading \S+\.(?:tar\.gz|tar\.bz2|tgz|zip)\b")

# Toolchain / header problems: the build could not even compile.
_TOOLCHAIN_MARKERS = (
    "command 'gcc' failed",
    "gcc: not found",
    "unable to execute 'gcc'",
    "fatal error: Python.h",
    "Microsoft Visual C++",
    "can't find Rust compiler",
    "pg_config executable not found",
)
_MISSING_HEADER_RE = re.compile(r"[\w./-]+\.h: No such file")

# Source-build failures that aren't toolchain-specific.
_BUILD_FAILURE_MARKERS = (
    "Failed building wheel",
    "Running setup.py install",
)
_SUBPROCESS_ERROR = "error: subprocess-exited-with-error"

_DOWNLOAD_MARKERS = ("Collecting", "Downloading")

# Lines that are pip boilerplate rather than a reason.
_BOILERPLATE_ERRORS = (
    "error: subprocess-exited-with-error",
    "error: metadata-generation-failed",
    "error: failed-wheel-build-for-install",
    "error: legacy-install-failure",
)

_SHORT_REASON_MAX = 160
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def normalize_arch(machine: str) -> str:
    """Map platform.machine() values to the names the rest of the app uses."""
    m = (machine or "").strip().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return machine


def base_package_name(package: str) -> str:
    """'pandas[sql]>=2.0' -> 'pandas' (same split executor uses for the pin)."""
    return re.split(r"[<>=!~\[]", package or "", maxsplit=1)[0].strip()


def _canonical(name: str) -> str:
    """PEP 503 name normalisation (Foo_Bar.baz -> foo-bar-baz)."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def parse_missing_version(output: str):
    """Parse pip's "Could not find a version …" line.

    Returns None when the line is absent, else a dict with:
      name      requirement name as pip printed it
      spec      version specifier ("==99", ">=99", "~=9.0"), extras and
                marker stripped; "" when there is none
      parent    the package that required it (dependency case) or ""
      versions  available versions in pip's order (ascending); [] for "none"
    """
    m = _VERSIONS_RE.search(output or "")
    if not m:
        return None
    req = m.group("req").strip()
    parent = ""
    i = req.find(" (from ")
    if i != -1 and req.endswith(")"):
        req, parent = req[:i].strip(), req[i + len(" (from "):-1].strip()
    req = req.split(";", 1)[0].strip()          # drop the environment marker
    nm = _REQ_NAME_RE.match(req)
    name = nm.group(1) if nm else req
    spec = req[nm.end():] if nm else ""
    spec = re.sub(r"^\s*\[[^\]]*\]", "", spec)  # drop extras
    spec = spec.strip().strip("()").strip()
    raw = m.group("versions").strip()
    versions = [] if raw.lower() in ("", "none") else [v.strip() for v in raw.split(",") if v.strip()]
    return {"name": name, "spec": spec, "parent": parent, "versions": versions}


def _shown_versions(versions):
    """The last few versions (final releases preferred), '…, ' when trimmed."""
    finals = [v for v in versions if not _PRERELEASE_RE.search(v.split("+", 1)[0].lstrip("0123456789."))]
    pool = finals or versions
    tail = [_sanitize(v)[:40] for v in pool[-_SHOWN_VERSIONS:]]
    return ("…, " if len(pool) > _SHOWN_VERSIONS else "") + ", ".join(tail)


def _has_build_started(output: str) -> bool:
    """A source build step ran (not merely an sdist download)."""
    return (any(m in output for m in _BUILD_MARKERS)
            or bool(_COMPILER_LINE_RE.search(output))
            or _has_toolchain_marker(output))


def _has_build_marker(output: str) -> bool:
    return _has_build_started(output) or bool(_SDIST_DOWNLOAD_RE.search(output))


def _last_download_is_sdist(output: str) -> bool:
    downloads = [ln for ln in output.splitlines() if "Downloading " in ln]
    return bool(downloads) and bool(_SDIST_DOWNLOAD_RE.search(downloads[-1]))


def _has_toolchain_marker(output: str) -> bool:
    return any(m in output for m in _TOOLCHAIN_MARKERS) or bool(_MISSING_HEADER_RE.search(output))


def classify_pip_output(output: str, *, timed_out: bool, returncode) -> str:
    """Classify pip's combined stdout+stderr into one of the kinds above."""
    output = output or ""
    if timed_out:
        if _has_build_started(output):
            return "source_build_timeout"
        if _last_download_is_sdist(output):
            return "sdist_download_timeout"
        if any(m in output for m in _DOWNLOAD_MARKERS):
            return "download_timeout"
        return "timeout"
    if returncode == 0:
        return "ok"
    if any(m in output for m in _NO_DISTRIBUTION_MARKERS):
        missing = parse_missing_version(output)
        if missing and missing["versions"]:
            return "version_not_found"
        return "no_distribution"
    if _has_toolchain_marker(output) or any(m in output for m in _BUILD_FAILURE_MARKERS):
        return "source_build_failed"
    if _SUBPROCESS_ERROR in output and _has_build_marker(output):
        return "source_build_failed"
    return "other"


def _sanitize(line: str) -> str:
    line = _ANSI_RE.sub("", line)
    line = _CONTROL_CHARS_RE.sub("", line)
    line = " ".join(line.split())
    if len(line) > _SHORT_REASON_MAX:
        line = line[: _SHORT_REASON_MAX - 1].rstrip() + "…"
    return line


def short_reason(output: str) -> str:
    """First compiler/header line, else the first meaningful error line."""
    lines = (output or "").splitlines()
    for line in lines:
        if any(m in line for m in _TOOLCHAIN_MARKERS) or _MISSING_HEADER_RE.search(line):
            return _sanitize(line)
    for line in lines:
        s = line.strip()
        low = s.lower()
        if not s or any(s.startswith(b) for b in _BOILERPLATE_ERRORS):
            continue
        if low.startswith("error:") or "error:" in low or s.startswith("ERROR:"):
            return _sanitize(s)
    for line in lines:
        if "Failed building wheel" in line:
            return _sanitize(line)
    return ""


def describe_pip_failure(package, output, stderr, *, timed_out, returncode,
                         arch, python_version, timeout_s):
    """Return {kind, message, arch} for a failed pip install.

    `output` is stdout+stderr (what gets classified); `stderr` alone keeps
    today's message for failures we don't recognise.
    """
    output = output or ""
    pkg = base_package_name(package) or package
    kind = classify_pip_output(output, timed_out=timed_out, returncode=returncode)
    t = int(timeout_s) if float(timeout_s).is_integer() else timeout_s

    if kind == "no_distribution":
        message = (f"No version of {pkg} is available for Python {python_version} "
                   f"on {arch} (pip found no matching distribution).")
    elif kind == "version_not_found":
        # Other versions exist, so this is the pin/specifier, not the arch.
        missing = parse_missing_version(output)
        name = _sanitize(missing["name"]) or pkg
        spec = _sanitize(missing["spec"])
        wanted = f"matches '{spec}'" if spec else "matches the requirement"
        available = f"(available: {_shown_versions(missing['versions'])})"
        if _canonical(name) == _canonical(pkg):
            message = f"No version of {name} {wanted} for Python {python_version} {available}."
        else:
            message = (f"Installing {pkg} failed: no version of its dependency {name} "
                       f"{wanted} for Python {python_version} {available}.")
        if _DIFFERENT_PYTHON in output.lower():
            message += " Some versions were skipped because they need a different Python version."
    elif kind == "sdist_download_timeout":
        message = (f"Installing {pkg} timed out after {t}s while downloading its source "
                   f"package (no prebuilt wheel for {arch}; the source build hadn't started yet).")
    elif kind == "source_build_timeout":
        message = (f"No prebuilt wheel for {pkg} on {arch}; building from source "
                   f"timed out after {t}s. Try a version that has an {arch} wheel.")
    elif kind == "source_build_failed":
        detail = " (missing compiler/headers)" if _has_toolchain_marker(output) else ""
        reason = short_reason(output).rstrip(".")
        message = f"No prebuilt wheel for {pkg} on {arch}; building from source failed{detail}"
        message += f": {reason}." if reason else "."
    elif kind == "download_timeout":
        message = (f"Installing {pkg} timed out after {t}s while downloading "
                   f"(no source build had started). Check network access and try again.")
    elif kind == "timeout":
        # Same text as before this module existed.
        message = f"Installation of {package} timed out ({t}s limit)"
    else:
        message = (stderr or "").strip() or output.strip()[-2000:]
    return {"kind": kind, "message": message, "arch": arch}
