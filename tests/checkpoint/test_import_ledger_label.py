#!/usr/bin/env python3
# test-kind: offline
"""
app/platform/checkpoint/manager.py _import_already_loaded(line, loaded): the restore
import-ledger [cold]/[preloaded] label judged against a sys.modules snapshot taken
at restore() start, so a module the var unpickle imported moments earlier is
labelled cold. Without a snapshot the behaviour is unchanged (live sys.modules).
Uses a private temp package (tempfile.mkdtemp) on sys.path; no AWS, no network.
Run:
    .venv/bin/python tests/checkpoint/test_import_ledger_label.py
"""
import importlib
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))  # import-time only
from app.platform.checkpoint.manager import _import_already_loaded  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  [{detail}]" if not ok and detail else ""))


def _make_pkg(root, pkg):
    d = os.path.join(root, pkg)
    os.makedirs(d)
    with open(os.path.join(d, "__init__.py"), "w") as f:
        f.write("plain_name = 1\n")
    for sub in ("early", "late", "lazy"):
        with open(os.path.join(d, f"{sub}.py"), "w") as f:
            f.write("X = 1\n")


def main():
    tmp = tempfile.mkdtemp(prefix="ledger-label-")
    pkg = f"_ledger_pkg_{os.getpid()}"
    try:
        _make_pkg(tmp, pkg)
        sys.path.insert(0, tmp)
        importlib.import_module(pkg)
        importlib.import_module(f"{pkg}.early")

        snapshot = frozenset(sys.modules)  # restore() start

        # "Unpickle" imports a module after the snapshot.
        importlib.import_module(f"{pkg}.late")

        print("[import X]")
        check("imported after snapshot -> cold with snapshot",
              _import_already_loaded(f"import {pkg}.late", snapshot) is False)
        check("imported after snapshot -> preloaded without snapshot (old behaviour)",
              _import_already_loaded(f"import {pkg}.late") is True)
        check("present at snapshot -> preloaded",
              _import_already_loaded(f"import {pkg}.early", snapshot) is True)
        check("`import P.m as alias` present at snapshot -> preloaded",
              _import_already_loaded(f"import {pkg}.early as e", snapshot) is True)
        check("never imported -> cold",
              _import_already_loaded(f"import {pkg}.lazy", snapshot) is False)

        print("[from P import N]")
        check("submodule imported after snapshot -> cold",
              _import_already_loaded(f"from {pkg} import late", snapshot) is False)
        check("submodule imported after snapshot -> preloaded without snapshot",
              _import_already_loaded(f"from {pkg} import late") is True)
        check("submodule present at snapshot -> preloaded",
              _import_already_loaded(f"from {pkg} import early", snapshot) is True)
        check("plain attribute of a package present at snapshot -> preloaded",
              _import_already_loaded(f"from {pkg} import plain_name", snapshot) is True)
        check("lazy submodule never imported -> cold",
              _import_already_loaded(f"from {pkg} import lazy", snapshot) is False)
        check("package missing from snapshot -> cold for a plain attribute",
              _import_already_loaded(f"from {pkg} import plain_name", frozenset()) is False)
        check("parenthesised names use the first name",
              _import_already_loaded(f"from {pkg} import (early, late)", snapshot) is True)

        print("[other]")
        check("non-import line -> None", _import_already_loaded("x = 1", snapshot) is None)
        check("empty line -> None", _import_already_loaded("", snapshot) is None)
    finally:
        try:
            sys.path.remove(tmp)
        except ValueError:
            pass
        for m in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
            del sys.modules[m]
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
