#!/usr/bin/env python3
"""Generate app/platform/pip_preload_modules.txt: the modules the pip helper imports at
image build (app/platform/pip_helper.py preload()).
WHY THIS EXISTS
    The list used to be recorded by hand from ONE real install (statsmodels + bottle). This
    script records it from a fixed matrix of pip actions instead (SCENARIOS: installs of
    wheels and sdists, requirements files, extras, ranges, upgrades, resolver errors,
    uninstall / list / show / freeze / check / download / --dry-run --report, git+https,
    PEP 517 build isolation) so the list does not depend on which packages someone tried.
    Measured on a VM (.agents/tasks/pip-preload-coverage/report.md): what a pip run imports
    depends on the pip code path, not on the package, so the matrix is small.
HOW
    Each scenario runs in a FRESH process of a throwaway venv whose pip is exactly
    IMAGE_PIP (the image's pip), through `pip._internal.cli.main.main`. At exit the process
    writes sys.modules (aliases dropped), which Python keeps in first-import order. The union over the
    scenarios (scenario order, then import order) is filtered to pip's own modules plus the
    standard library (nothing from site-packages, no credentials, nothing that needs a
    network to import) and written under a comment header (header_block(), the one source
    of the header text) whose first line is:
        # pip==22.3.1 python=3.11 scenarios=matrix-v1:<hash of SCENARIOS>
    Imports done by pip's subprocesses (build isolation: setuptools, venv, pyproject hooks)
    run in other processes and cannot be preloaded in the helper.
USAGE
    python scripts/gen_pip_preload_modules.py                 # record (network) and write the txt
    python scripts/gen_pip_preload_modules.py --check          # offline: header matches this script
    python scripts/gen_pip_preload_modules.py --check --strict # ... and fail on the legacy list
    python scripts/gen_pip_preload_modules.py --check --live   # re-record (network) and diff
    python scripts/gen_pip_preload_modules.py --out FILE       # write somewhere else
    python scripts/gen_pip_preload_modules.py --python PY      # base interpreter for the venv
    Run it on Linux (a throwaway VM or a container) with Python 3.11: the module set is
    the one the image's pip imports there (posix, not nt). --check is stale when the header's
    pip / python / scenario hash differ from this script, when the file is not byte-identical
    to what render() writes for its module lines (hand-edited header, a line added or
    removed), when the body is not canonical, or when there is no header.
    A header with scenarios=hand-v0:statsmodels+bottle (LEGACY_SOURCE) marks the earlier
    list recorded from one install: --check passes it with a note (use --strict to fail).
    Regenerate to adopt the matrix; the image picks it up at its next rebuild.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "app", "platform", "pip_preload_modules.txt")

# The image's pip and Python (the VM prints "pip 22.3.1 ... (python 3.11)"; python3.11-pip on
# AL2023). Nothing in the repo pins pip, so this is the one place that does.
IMAGE_PIP = "22.3.1"
IMAGE_PYTHON = "3.11"
SCENARIO_SET = "matrix-v1"

_I = ["install", "--progress-bar", "off", "--disable-pip-version-check", "--no-input"]
_R = ["install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off", "--no-compile",
      "--quiet", "--log", "{W}/restore.log"]
_D = ["--disable-pip-version-check"]
# (name, pip args, {relative file: content}). {W} = a per-run work dir. Order matters (union order).
SCENARIOS = [
    ("install-wheel", _I + ["bottle==0.13.4"], {}),
    ("install-again", _I + ["bottle==0.13.4"], {}),
    ("restore-flags-3-pins", _R + ["tabulate==0.9.0", "toolz==1.0.0", "cowsay==6.1"], {}),
    ("requirements-file", _I + ["-r", "{W}/req.txt"], {"req.txt": "tabulate==0.9.0\ntoolz==1.0.0\n"}),
    ("version-range", _I + ["tabulate>=0.8,<0.9"], {}),
    ("upgrade", _I + ["-U", "tabulate"], {}),
    ("extras", _I + ["tabulate[widechars]"], {}),
    ("install-sdist-legacy", _I + ["word2number"], {}),
    ("install-sdist-pep517", _I + ["--use-pep517", "word2number"], {}),
    ("install-git", _I + ["git+https://github.com/pallets/click.git@8.1.7"], {}),
    ("conflict", _I + ["requests==2.32.3", "urllib3==1.20"], {}),
    ("no-such-dist", _I + ["this-pkg-does-not-exist-zzqq"], {}),
    ("dry-run-report", _I + ["--dry-run", "--report", "{W}/r.json", "--quiet", "seaborn"], {}),
    ("download", ["download", "--no-deps", "-d", "{W}/dl", "tabulate"] + _D, {}),
    ("uninstall", ["uninstall", "-y", "tabulate", "toolz", "cowsay"] + _D, {}),
    ("uninstall-missing", ["uninstall", "-y", "nopkgxyz"] + _D, {}),
    ("list-json", ["list", "--format=json"] + _D, {}),
    ("list", ["list"] + _D, {}),
    ("show", ["show", "pip"] + _D, {}),
    ("freeze", ["freeze"] + _D, {}),
    ("check", ["check"] + _D, {}),
]

# Runs inside each scenario's fresh process (`python -c`). Imports nothing but sys/os, which
# the interpreter already has, so it adds nothing to sys.modules.
_RECORDER = r"""
import os, sys
rc = 1
try:
    from pip._internal.cli.main import main
    rc = main(sys.argv[1:])
except SystemExit as e:
    rc = e.code
finally:
    # Skip aliases (pip's vendored requests.packages.* entries point at other modules).
    names = [n for n, m in list(sys.modules.items()) if getattr(m, "__name__", n) == n]
    with open(os.environ["PIP_PRELOAD_TRACE"], "w") as f:
        f.write("\n".join(names))
"""

_HEADER_RE = re.compile(r"^#\s*pip==(?P<pip>\S+)\s+python=(?P<py>\S+)\s+scenarios=(?P<scn>\S+)\s*$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def scenarios_id(scenarios=None) -> str:
    """matrix-v1:<8 hex of the scenario definitions>; changes when SCENARIOS changes."""
    blob = json.dumps(SCENARIOS if scenarios is None else scenarios, sort_keys=True)
    return f"{SCENARIO_SET}:{hashlib.sha256(blob.encode()).hexdigest()[:8]}"


def header_line(pip=IMAGE_PIP, py=IMAGE_PYTHON, scenarios=None, source=None) -> str:
    return f"# pip=={pip} python={py} scenarios={source or scenarios_id(scenarios)}"


# `scenarios=` value of the list that predates this script: recorded from ONE real
# `pip install statsmodels==0.15.0 bottle==0.13.4` on the VM (pip 22.3.1, Python 3.11).
# --check passes it with a note (fails with --strict); regenerating replaces it.
LEGACY_SOURCE = "hand-v0:statsmodels+bottle"


def body_hash(names) -> str:
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:12]


def header_block(names, scenarios=None, pip=IMAGE_PIP, py=IMAGE_PYTHON, source=None) -> list:
    """The comment block at the top of the txt. The single source of the header text.
    No timestamps: --check compares it byte for byte."""
    src = source or scenarios_id(scenarios)
    if src == LEGACY_SOURCE:
        origin = ["# Source: the earlier list, recorded from ONE real pip install of statsmodels==0.15.0",
                  "#   and bottle==0.13.4 on the VM. Regenerating replaces it with the union over the",
                  "#   script's SCENARIOS matrix (installs, sdists, git, requirements files, uninstall,",
                  "#   list/show/freeze/check, ...)."]
    else:
        origin = ["# Source: the union over the script's SCENARIOS matrix (installs, sdists, git,",
                  "#   requirements files, uninstall, list/show/freeze/check, ...), one fresh pip",
                  "#   process per scenario."]
    return [
        header_line(pip, py, scenarios, src),
        "# AUTO-GENERATED by scripts/gen_pip_preload_modules.py. DO NOT EDIT BY HAND.",
        "#",
        "# What: the pip and standard-library modules a real pip run imports, in first-import",
        "#   order. app/platform/pip_helper.py preload() imports them at image build, so they",
        "#   are already in memory in the Firecracker snapshot and a user's first pip run does",
        "#   not import them from disk.",
        f"# Recorded with: pip {pip}, Python {py} (the image's; IMAGE_PIP / IMAGE_PYTHON in the script).",
        *origin,
        f"# Body: {len(names)} modules, sha256 {body_hash(names)} (checked by --check).",
        "#",
        "# Regenerate (needs network; run on Linux with Python 3.11, e.g. a throwaway VM or",
        f"#   container; the script creates a throwaway venv and installs pip=={pip} into it):",
        "#     python3.11 scripts/gen_pip_preload_modules.py",
        "# Verify (offline):",
        "#     python3 scripts/gen_pip_preload_modules.py --check",
        "#   exits 1 (STALE) when this header was edited or no longer matches the script, when a",
        "#   module line was added or removed, or the list has duplicates or non-module names.",
        "# Regenerate when: the image's pip or Python version changes, or a pip code path worth",
        "#   covering is added to SCENARIOS. The image picks the new list up at its next rebuild.",
        "# A missing or stale line is harmless: preload is best-effort (a failing import is",
        "#   skipped) and a module that is not preloaded only costs time on the first pip run.",
        "# The loader skips lines starting with '#' and blank lines.",
    ]


def parse_header(text: str):
    """The first `# pip==X python=Y scenarios=Z` line of a file, as a dict, else None."""
    for ln in text.splitlines():
        m = _HEADER_RE.match(ln.strip())
        if m:
            return {"pip": m.group("pip"), "python": m.group("py"), "scenarios": m.group("scn")}
        if ln.strip() and not ln.startswith("#"):
            break  # the header must come before the first module
    return None


def body_names(text: str) -> list:
    """Module names of a txt file, as pip_helper._module_list reads them."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def keep(name: str, stdlib: frozenset, builtin: frozenset) -> bool:
    """pip's own modules and the standard library only (no site-packages, no __main__)."""
    if not _NAME_RE.match(name):
        return False
    top = name.split(".")[0]
    if top == "pip":
        return True
    return top in stdlib or top in builtin


def merge(traces, stdlib, builtin) -> list:
    """Union of per-scenario module lists: scenario order, then first-import order, filtered."""
    seen, out = set(), []
    for names in traces:
        for n in names:
            if n not in seen and keep(n, stdlib, builtin):
                seen.add(n)
                out.append(n)
    return out


def render(names, scenarios=None, pip=IMAGE_PIP, py=IMAGE_PYTHON, source=None) -> str:
    return "\n".join([*header_block(names, scenarios, pip, py, source), *names, ""])


def check_text(text: str, strict=False, scenarios=None):
    """(ok, [messages]) for the txt's content versus this script. Offline. The whole file
    must equal what render() writes for its module lines (header text, module count and
    hash included), so a hand-edited header or an added / removed line is stale."""
    msgs = []
    h = parse_header(text)
    names = body_names(text)
    if h is None:
        return False, ["stale: no `# pip==... python=... scenarios=...` header; "
                       "regenerate with scripts/gen_pip_preload_modules.py"]
    legacy = h["scenarios"] == LEGACY_SOURCE
    if h["pip"] != IMAGE_PIP:
        msgs.append(f"stale: header pip=={h['pip']}, the image's pip is {IMAGE_PIP}")
    if h["python"] != IMAGE_PYTHON:
        msgs.append(f"stale: header python={h['python']}, the image's python is {IMAGE_PYTHON}")
    if not legacy and h["scenarios"] != scenarios_id(scenarios):
        msgs.append(f"stale: header scenarios={h['scenarios']}, this script's matrix is {scenarios_id(scenarios)}")
    if not names:
        msgs.append("no modules listed")
    if len(set(names)) != len(names):
        msgs.append("duplicate module names")
    bad = [n for n in names if not _NAME_RE.match(n)]
    if bad:
        msgs.append(f"not module names: {bad[:3]}")
    if not msgs and text != render(names, scenarios, source=h["scenarios"] if legacy else None):
        msgs.append("stale: the file differs from what this script writes for its module lines "
                    "(header edited by hand, or a module line added or removed); regenerate it")
    if msgs:
        return False, msgs
    if legacy:
        note = (f"scenarios={LEGACY_SOURCE}: the module list is the earlier one recorded from one install "
                "of statsmodels + bottle; regenerate on Linux to adopt the SCENARIOS matrix")
        return (not strict), [note]
    return True, []


# ---------------------------------------------------------------------------------------
# Recording (network: the scenarios install from PyPI and GitHub)
# ---------------------------------------------------------------------------------------

def make_venv(base_python: str, path: str) -> str:
    """A throwaway venv whose pip is exactly IMAGE_PIP. Returns its python."""
    subprocess.run([base_python, "-m", "venv", path], check=True)
    py = os.path.join(path, "bin", "python")
    have = pip_version(py)
    if have != IMAGE_PIP:
        subprocess.run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        f"pip=={IMAGE_PIP}"], check=True)
        have = pip_version(py)
    if have != IMAGE_PIP:
        raise SystemExit(f"venv pip is {have}, need {IMAGE_PIP}")
    return py


def pip_version(py: str) -> str:
    r = subprocess.run([py, "-c", "import pip; print(pip.__version__)"], capture_output=True, text=True)
    return r.stdout.strip()


def python_mm(py: str) -> str:
    r = subprocess.run([py, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def record(py: str, scenarios=None, timeout=300):
    """Run every scenario in a fresh process of `py`; returns ([module lists], [failures])."""
    scenarios = SCENARIOS if scenarios is None else scenarios
    traces, failures = [], []
    with tempfile.TemporaryDirectory(prefix="pip-preload-") as work:
        for name, args, files in scenarios:
            for rel, content in files.items():
                with open(os.path.join(work, rel), "w") as f:
                    f.write(content)
            trace = os.path.join(work, f"trace-{name}.txt")
            env = dict(os.environ, PIP_PRELOAD_TRACE=trace, PYTHONUNBUFFERED="1")
            for k in [k for k in env if k.startswith("PIP_") and k != "PIP_PRELOAD_TRACE"]:
                del env[k]  # the caller's pip config must not change what is imported
            argv = [a.replace("{W}", work) for a in args]
            try:
                r = subprocess.run([py, "-c", _RECORDER] + argv, env=env, cwd=work,
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                failures.append((name, "timeout"))
                continue
            if not os.path.exists(trace):
                failures.append((name, f"no trace (rc={r.returncode}) {r.stderr[-200:]}"))
                continue
            with open(trace) as f:
                traces.append(f.read().split("\n"))
    return traces, failures


def generate(base_python: str, scenarios=None):
    """(text, failures): record on a throwaway venv created from base_python."""
    with tempfile.TemporaryDirectory(prefix="pip-preload-venv-") as d:
        py = make_venv(base_python, os.path.join(d, "v"))
        mm = python_mm(py)
        if mm != IMAGE_PYTHON:
            raise SystemExit(f"python is {mm}, the image's is {IMAGE_PYTHON}")
        traces, failures = record(py, scenarios)
        probe = subprocess.run([py, "-c", "import sys, json; print(json.dumps([sorted(sys.stdlib_module_names), "
                                "list(sys.builtin_module_names)]))"], capture_output=True, text=True)
        std, builtin = json.loads(probe.stdout)
    names = merge(traces, frozenset(std), frozenset(builtin))
    return render(names, scenarios), failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("WHY")[0].strip())
    ap.add_argument("--check", action="store_true", help="offline: fail when the txt is stale")
    ap.add_argument("--strict", action="store_true", help="with --check: fail on the legacy (hand-v0) list")
    ap.add_argument("--live", action="store_true", help="with --check: re-record (network) and diff")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--python", default=sys.executable, help="base interpreter for the throwaway venv")
    a = ap.parse_args(argv)
    if a.check:
        try:
            with open(a.out, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"cannot read {a.out}: {e}", file=sys.stderr)
            return 1
        ok, msgs = check_text(text, strict=a.strict)
        for m in msgs:
            print(("OK (note): " if ok else "STALE: ") + m, file=sys.stderr if not ok else sys.stdout)
        if ok and a.live and parse_header(text):
            fresh, failures = generate(a.python)
            if body_names(fresh) != body_names(text):
                print("STALE: the re-recorded list differs from the file", file=sys.stderr)
                return 1
        if ok and not msgs:
            print(f"{os.path.relpath(a.out, ROOT)} is up to date ({parse_header(text)['scenarios']})")
        return 0 if ok else 1
    text, failures = generate(a.python)
    for name, why in failures:
        print(f"scenario {name} did not record: {why}", file=sys.stderr)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {a.out}: {len(body_names(text))} modules, {len(failures)} scenario(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
