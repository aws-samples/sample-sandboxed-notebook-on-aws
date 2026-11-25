#!/usr/bin/env python3
# test-kind: offline
"""
scripts/gen_pip_preload_modules.py (the generator and its --check) and the loader that
reads app/platform/pip_preload_modules.txt (pip_helper._module_list / preload).
Offline: --check reads files only; the recording section runs this interpreter's own
pip with offline commands (--version, list, check) in fresh subprocesses and is skipped
when this interpreter has no pip.
Sections:
  [header]   render / parse_header / scenarios_id / header_block: the header changes when
             SCENARIOS does, every line starts with '#', it names the regenerate command and
             --check, records pip / python, has no timestamp.
  [file]     the checked-in txt starts with the generator's header block; the loader's list
             is identical with and without the header.
  [check]    check_text: up to date, stale pip / python / scenario hash, duplicates, bad
             names, empty body, no header, a module line added / removed, a hand-edited
             header; the legacy (hand-v0) list passes with a note unless strict.
  [merge]    keep / merge: pip + stdlib + builtins only, no site-packages, no aliases of
             __main__, first-import order across scenarios, de-duplicated.
  [cli]      main(["--check", ...]) exit codes on temp files, on the checked-in txt, and on
             copies of it with a module line removed or the header edited.
  [record]   record() runs each scenario in a fresh process and returns its modules.
  [loader]   _module_list: comments, blank lines, duplicates, CRLF, a missing file;
             preload(): a duplicate imports once, a missing module and a module that
             raises SystemExit are skipped, not fatal.
Run:
    .venv/bin/python tests/vm/test_pip_preload_generator.py
"""
import io
import os
import contextlib
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from app.platform import pip_helper as ph  # noqa: E402
from scripts import gen_pip_preload_modules as gen  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  [{detail}]" if not ok and detail else ""))


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def _cli(args):
    """gen.main(args) -> (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = gen.main(args)
    return rc, out.getvalue(), err.getvalue()


def section_header():
    print("\n[header]")
    h = gen.header_line()
    check("header line has pip, python and scenarios",
          h == f"# pip=={gen.IMAGE_PIP} python={gen.IMAGE_PYTHON} scenarios={gen.scenarios_id()}", h)
    text = gen.render(["os", "pip._internal.cli.main"])
    p = gen.parse_header(text)
    check("render -> parse_header round trip",
          p == {"pip": gen.IMAGE_PIP, "python": gen.IMAGE_PYTHON, "scenarios": gen.scenarios_id()}, p)
    check("the body is the module names only (comments skipped)",
          gen.body_names(text) == ["os", "pip._internal.cli.main"], gen.body_names(text))
    check("scenario id is matrix-v1:<8 hex>", gen.scenarios_id().startswith("matrix-v1:")
          and len(gen.scenarios_id().split(":")[1]) == 8)
    changed = gen.SCENARIOS + [("extra", ["list"], {})]
    check("a different scenario matrix changes the id", gen.scenarios_id(changed) != gen.scenarios_id())
    check("the header is found only before the first module",
          gen.parse_header("os\n# pip==1 python=3.11 scenarios=x:y\n") is None)
    check("no header -> None", gen.parse_header("os\nre\n") is None)
    block = gen.header_block(["os"])
    check("every header line starts with '#'", all(ln.startswith("#") for ln in block), block)
    joined = "\n".join(block)
    check("the header says auto-generated / do not edit, and names the regenerate and --check commands",
          "AUTO-GENERATED" in joined and "DO NOT EDIT BY HAND" in joined
          and "python3.11 scripts/gen_pip_preload_modules.py\n" in joined
          and "scripts/gen_pip_preload_modules.py --check" in joined, joined)
    check("the header records pip and python", f"pip {gen.IMAGE_PIP}, Python {gen.IMAGE_PYTHON}" in joined)
    check("the header has no timestamp (same input -> same text)",
          gen.render(["os"]) == gen.render(["os"]) and not any(c in joined for c in ("2025-", "2026-", "UTC")))
    check("the legacy source gets its own provenance line",
          "statsmodels==0.15.0" in "\n".join(gen.header_block(["os"], source=gen.LEGACY_SOURCE)))


def section_file():
    print("\n[file]  (the checked-in app/platform/pip_preload_modules.txt)")
    with open(gen.DEFAULT_OUT, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    names = gen.body_names(text)
    n_head = next(i for i, ln in enumerate(lines) if not ln.startswith("#"))
    check("the file starts with the generator's header block",
          lines[:n_head] == gen.header_block(names, source=gen.parse_header(text)["scenarios"]), lines[:3])
    check("the header is at the top, starts with '#', mentions the regenerate command and --check",
          n_head > 5 and lines[0].startswith("# pip==") and "--check" in text[:3000]
          and "python3.11 scripts/gen_pip_preload_modules.py" in text[:3000])
    check("no comment lines after the header (body = module lines only)",
          lines[n_head:] == names, n_head)
    # the loader reads the same list with and without the header
    orig = ph._PRELOAD_FILE
    try:
        with tempfile.TemporaryDirectory() as d:
            ph._PRELOAD_FILE = _write(d, "bare.txt", "\n".join(names) + "\n")
            bare = ph._module_list()
            ph._PRELOAD_FILE = gen.DEFAULT_OUT
            with_header = ph._module_list()
    finally:
        ph._PRELOAD_FILE = orig
    check("the loader skips the header: the module list is identical with and without it",
          with_header == bare and "\n".join(with_header).encode() == "\n".join(bare).encode(),
          (len(with_header), len(bare)))
    check("... and contains no comment text", not any(n.startswith("#") or " " in n for n in with_header))


def section_check():
    print("\n[check]")
    good = gen.render(["os", "re", "pip._internal.cli.main"])
    ok, msgs = gen.check_text(good)
    check("a freshly rendered file is up to date", ok and not msgs, msgs)
    ok, msgs = gen.check_text(good.replace(f"pip=={gen.IMAGE_PIP}", "pip==99.0"))
    check("stale when the header pip differs", not ok and "pip==99.0" in msgs[0], msgs)
    ok, msgs = gen.check_text(good.replace(f"python={gen.IMAGE_PYTHON}", "python=3.9"))
    check("stale when the header python differs", not ok and "python=3.9" in msgs[0], msgs)
    ok, msgs = gen.check_text(good.replace("matrix-v1:", "matrix-v0:"))
    check("stale when the scenario id differs", not ok and "scenarios=" in msgs[0], msgs)
    ok, msgs = gen.check_text(good + "os\n")
    check("duplicate module names are stale", not ok and any("duplicate" in m for m in msgs), msgs)
    ok, msgs = gen.check_text(gen.render(["os", "not a module", "x-y"]))
    check("non-module names are stale", not ok and any("not module names" in m for m in msgs), msgs)
    ok, msgs = gen.check_text(gen.render([]))
    check("an empty body is stale", not ok and any("no modules" in m for m in msgs), msgs)
    ok, msgs = gen.check_text("_io\nmarshal\nos\n")
    check("a header-less file is stale", not ok and "no `# pip==" in msgs[0], msgs)
    legacy = gen.render(["_io", "marshal", "os"], source=gen.LEGACY_SOURCE)
    ok, msgs = gen.check_text(legacy)
    check("the legacy (hand-v0) list passes with a note", ok and len(msgs) == 1 and "hand-v0" in msgs[0], msgs)
    ok, msgs = gen.check_text(legacy, strict=True)
    check("... and fails with strict", not ok, msgs)
    lines = good.splitlines(keepends=True)
    removed = "".join(ln for ln in lines if ln != "re\n")
    ok, msgs = gen.check_text(removed)
    check("stale when a module line is removed", not ok and "differs" in msgs[-1], msgs)
    ok, msgs = gen.check_text(good + "json\n")
    check("stale when a module line is added", not ok and "differs" in msgs[-1], msgs)
    edited = good.replace("DO NOT EDIT BY HAND.", "DO NOT EDIT BY HAND!")
    ok, msgs = gen.check_text(edited)
    check("stale when the header text is edited by hand", not ok and "differs" in msgs[-1], msgs)
    ok, msgs = gen.check_text(good.replace("\nre\n", "\n# a note\nre\n"))
    check("stale when a comment is added after the header", not ok, msgs)


def section_merge():
    print("\n[merge]")
    std = frozenset({"os", "re", "json", "importlib"})
    builtin = frozenset({"sys", "_io"})
    t1 = ["sys", "os", "pip", "pip._internal", "numpy", "_distutils_hack", "__main__", "os"]
    t2 = ["sys", "re", "pip._vendor.rich", "os", "importlib.metadata", "setuptools", "bad name"]
    out = gen.merge([t1, t2], std, builtin)
    check("pip + stdlib + builtins kept, in first-import order across scenarios",
          out == ["sys", "os", "pip", "pip._internal", "re", "pip._vendor.rich", "importlib.metadata"], out)
    check("site-packages modules and __main__ dropped",
          not ({"numpy", "_distutils_hack", "__main__", "setuptools"} & set(out)))
    check("no duplicates", len(out) == len(set(out)))
    check("a package named like pip but not pip is dropped (pipx)", gen.merge([["pipx", "pip"]], std, builtin) == ["pip"])
    check("same input -> same output (deterministic)", gen.merge([t1, t2], std, builtin) == out)


def section_cli():
    print("\n[cli]")
    with tempfile.TemporaryDirectory() as d:
        good = _write(d, "good.txt", gen.render(["os", "re"]))
        rc, out, err = _cli(["--check", "--out", good])
        check("--check exits 0 on an up-to-date file", rc == 0 and "up to date" in out, (rc, out, err))
        stale = _write(d, "stale.txt", gen.render(["os"]).replace(f"pip=={gen.IMAGE_PIP}", "pip==1.0"))
        rc, out, err = _cli(["--check", "--out", stale])
        check("--check exits 1 and says STALE when the pip version differs", rc == 1 and "STALE" in err, (rc, err))
        legacy = _write(d, "legacy.txt", gen.render(["_io", "os"], source=gen.LEGACY_SOURCE))
        rc, out, err = _cli(["--check", "--out", legacy])
        check("--check exits 0 on the legacy list, with a note", rc == 0 and "hand-v0" in out, (rc, out, err))
        rc, out, err = _cli(["--check", "--strict", "--out", legacy])
        check("--check --strict exits 1 on the legacy list", rc == 1, (rc, out, err))
        bare = _write(d, "bare.txt", "_io\nos\n")
        rc, out, err = _cli(["--check", "--out", bare])
        check("--check exits 1 on a file without a header", rc == 1 and "STALE" in err, (rc, err))
        with open(gen.DEFAULT_OUT, encoding="utf-8") as f:
            real = f.read()
        real_lines = real.splitlines(keepends=True)
        first_mod = next(i for i, ln in enumerate(real_lines) if not ln.startswith("#"))
        cut = _write(d, "cut.txt", "".join(real_lines[:first_mod + 3] + real_lines[first_mod + 4:]))
        rc, out, err = _cli(["--check", "--out", cut])
        check("--check exits 1 on the checked-in file with one module line removed", rc == 1 and "STALE" in err,
              (rc, err))
        hand = _write(d, "hand.txt", real.replace("# What:", "# What (edited):", 1))
        rc, out, err = _cli(["--check", "--out", hand])
        check("--check exits 1 on the checked-in file with its header edited", rc == 1 and "STALE" in err, (rc, err))
        rc, out, err = _cli(["--check", "--out", os.path.join(d, "missing.txt")])
        check("--check exits 1 when the file is missing", rc == 1 and "cannot read" in err, (rc, err))
    rc, out, err = _cli(["--check"])
    check("--check passes on the checked-in app/platform/pip_preload_modules.txt", rc == 0, (rc, out, err))
    r = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "gen_pip_preload_modules.py"), "--check"],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    check("the script runs as `python scripts/gen_pip_preload_modules.py --check` (exit 0)", r.returncode == 0,
          (r.stdout + r.stderr)[-200:])


def section_record():
    print("\n[record]")
    have_pip = subprocess.run([sys.executable, "-c", "import pip"], capture_output=True).returncode == 0
    if not have_pip:
        print("  - skipped: this interpreter has no pip")
        return
    scn = [("version", ["--version"], {}), ("list", ["list", "--format=json", "--disable-pip-version-check"], {}),
           ("nothing", ["check", "--disable-pip-version-check"], {})]
    traces, failures = gen.record(sys.executable, scn)
    check("every offline scenario recorded", len(traces) == 3 and not failures, failures)
    check("each trace lists pip's CLI module", all("pip._internal.cli.main" in t for t in traces))
    check("a command module appears only in its own scenario",
          "pip._internal.commands.list" in traces[1] and "pip._internal.commands.list" not in traces[0])
    probe = subprocess.run([sys.executable, "-c", "import sys, json; print(json.dumps([sorted(sys.stdlib_module_names),"
                            " list(sys.builtin_module_names)]))"], capture_output=True, text=True)
    import json as _json
    std, builtin = _json.loads(probe.stdout)
    merged = gen.merge(traces, frozenset(std), frozenset(builtin))
    check("merged list: pip + stdlib only, first import first",
          merged and merged[0] in ("sys", "builtins") and "pip._internal.cli.main" in merged
          and all(n.split(".")[0] in ("pip", *std, *builtin) for n in merged))
    check("the recorder writes no aliases (names equal the module's __name__)",
          all(not n.startswith("pip._vendor.requests.packages.") for n in merged))
    bad = [("boom", ["definitely-not-a-pip-command"], {})]
    t2, f2 = gen.record(sys.executable, bad)
    check("a failing scenario still records the modules it imported", len(t2) == 1 and not f2, f2)


def section_loader():
    print("\n[loader]")
    orig_file, orig_roots = ph._PRELOAD_FILE, ph.PRELOAD_ROOTS
    try:
        with tempfile.TemporaryDirectory() as d:
            ph.PRELOAD_ROOTS = ("json", "re")
            ph._PRELOAD_FILE = _write(d, "m.txt",
                                      "# pip==1 python=3.11 scenarios=x:y\n# a comment\n\nos\r\n   \nstring  \nos\n#trailing\n")
            names = ph._module_list()
            check("roots first, then the file's names", names[:2] == ["json", "re"], names)
            check("comments and blank lines skipped, whitespace and CRLF stripped",
                  names[2:] == ["os", "string", "os"], names)
            ph._PRELOAD_FILE = os.path.join(d, "missing.txt")
            check("a missing file -> the roots only", ph._module_list() == ["json", "re"], ph._module_list())
            ph._PRELOAD_FILE = _write(d, "empty.txt", "# only a comment\n\n")
            check("a comment-only file -> the roots only", ph._module_list() == ["json", "re"])
            # preload: duplicates once, failures skipped (also a module that raises SystemExit)
            mod_dir = os.path.join(d, "mods")
            os.makedirs(mod_dir)
            _write(mod_dir, "pip_preload_boom_mod.py", "raise SystemExit(3)\n")
            _write(mod_dir, "pip_preload_ok_mod.py", "X = 1\n")
            sys.path.insert(0, mod_dir)
            try:
                ph._PRELOAD_FILE = _write(d, "p.txt", "pip_preload_ok_mod\npip_preload_ok_mod\n"
                                          "no_such_module_zzz_pip_preload\npip_preload_boom_mod\nos\n")
                ok, failed = ph.preload()
                check("a duplicate counts once; json, re, os, the ok module import", ok == 4, (ok, failed))
                check("a missing module is skipped and reported",
                      any(f.startswith("no_such_module_zzz_pip_preload: ModuleNotFoundError") for f in failed), failed)
                check("a module that raises SystemExit is skipped and reported",
                      any(f.startswith("pip_preload_boom_mod: SystemExit") for f in failed), failed)
                check("two failures, nothing raised", len(failed) == 2, failed)
            finally:
                sys.path.remove(mod_dir)
                sys.modules.pop("pip_preload_ok_mod", None)
    finally:
        ph._PRELOAD_FILE, ph.PRELOAD_ROOTS = orig_file, orig_roots
    names = ph._module_list()
    check("the real file loads: roots + hundreds of modules", len(names) > 600 and names[0] == ph.PRELOAD_ROOTS[0], len(names))


if __name__ == "__main__":
    section_header()
    section_file()
    section_check()
    section_merge()
    section_cli()
    section_record()
    section_loader()
    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    print("All passed")
