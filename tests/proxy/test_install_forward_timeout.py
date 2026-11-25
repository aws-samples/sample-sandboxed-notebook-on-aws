#!/usr/bin/env python3
# test-kind: offline
"""
Proxy forwarder read timeout for /install (pip messages per CPU arch).

The VM's pip install stops at 120s and then returns a classified message
("No prebuilt wheel for X on arm64; building from source timed out…"). The
forwarder's default read timeout is also 120s, so the proxy would give up first
and the message would be lost. Asserts
proxy.platform.routes.microvm._forward_read_timeout_for:
  - install (with or without slashes) gets max(base, 180s);
  - every other path keeps _forward_read_timeout() unchanged;
  - PROXY_FORWARD_READ_TIMEOUT is still honoured (300 → install 300, others
    300; 60 → install 180, others 60).

Offline: imports the module only (no proxy server, no VM).

Run:
    .venv/bin/python tests/proxy/test_install_forward_timeout.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _with_env(value, fn):
    old = os.environ.get("PROXY_FORWARD_READ_TIMEOUT")
    if value is None:
        os.environ.pop("PROXY_FORWARD_READ_TIMEOUT", None)
    else:
        os.environ["PROXY_FORWARD_READ_TIMEOUT"] = value
    try:
        fn()
    finally:
        if old is None:
            os.environ.pop("PROXY_FORWARD_READ_TIMEOUT", None)
        else:
            os.environ["PROXY_FORWARD_READ_TIMEOUT"] = old


def main():
    f = microvm_mod._forward_read_timeout_for

    def default():
        print("\n[default env]")
        check("base still 120", microvm_mod._forward_read_timeout() == 120.0)
        for p in ("install", "/install", "install/"):
            check(f"{p!r} → 180", f(p) == 180.0)
        for p in ("execute", "uninstall", "install-status", "packages", ""):
            check(f"{p!r} → 120 (unchanged)", f(p) == 120.0)

    def high():
        print("\n[PROXY_FORWARD_READ_TIMEOUT=300]")
        check("install → 300", f("install") == 300.0)
        check("execute → 300", f("execute") == 300.0)

    def low():
        print("\n[PROXY_FORWARD_READ_TIMEOUT=60]")
        check("install → 180 (floor)", f("install") == 180.0)
        check("execute → 60", f("execute") == 60.0)

    _with_env(None, default)
    _with_env("300", high)
    _with_env("60", low)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL INSTALL FORWARD TIMEOUT CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
