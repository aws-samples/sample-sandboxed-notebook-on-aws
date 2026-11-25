#!/usr/bin/env python3
# test-kind: offline
"""
assert_loopback_bind guard tests (FEAT-001).

Verifies the startup bind guard that keeps the control-plane services loopback
by default:
  * unset bind host -> OK (defaults to 127.0.0.1);
  * explicit loopback (127.0.0.1/::1/localhost) -> OK;
  * a non-loopback bind (0.0.0.0) -> RuntimeError;
  * non-loopback + ALLOW_NON_LOOPBACK_BIND=1 -> OK (explicit opt-in);
  * non-loopback + container detection (DOCKER_CONTAINER) -> OK.

Run:
    python3 tests/proxy/test_bind_guard.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from proxy.platform.auth import assert_loopback_bind  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


_ENV_VAR = "TEST_BIND_HOST"


def _clear_env():
    for k in (_ENV_VAR, "ALLOW_NON_LOOPBACK_BIND", "DOCKER_CONTAINER"):
        os.environ.pop(k, None)


def _raises(**env):
    _clear_env()
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        assert_loopback_bind(_ENV_VAR, "test-service")
        return False
    except RuntimeError:
        return True
    finally:
        _clear_env()


def main():
    print("\n=== assert_loopback_bind guard ===")

    # Unset -> ok (no raise).
    check("unset bind host -> ok (defaults to loopback)",
          not _raises())

    # Explicit loopback spellings -> ok.
    for host in ("127.0.0.1", "::1", "localhost"):
        check(f"explicit loopback {host!r} -> ok",
              not _raises(**{_ENV_VAR: host}))

    # Non-loopback -> raises.
    check("0.0.0.0 -> raises RuntimeError",
          _raises(**{_ENV_VAR: "0.0.0.0"}))

    # Non-loopback + explicit opt-in -> ok.
    check("0.0.0.0 + ALLOW_NON_LOOPBACK_BIND=1 -> ok",
          not _raises(**{_ENV_VAR: "0.0.0.0", "ALLOW_NON_LOOPBACK_BIND": "1"}))

    # Non-loopback + container detection -> ok.
    check("0.0.0.0 + DOCKER_CONTAINER=1 (container) -> ok",
          not _raises(**{_ENV_VAR: "0.0.0.0", "DOCKER_CONTAINER": "1"}))

    # The error message names the opt-in var so the operator knows the override.
    _clear_env()
    os.environ[_ENV_VAR] = "0.0.0.0"
    try:
        assert_loopback_bind(_ENV_VAR, "test-service")
        msg = ""
    except RuntimeError as e:
        msg = str(e)
    finally:
        _clear_env()
    check("error message names ALLOW_NON_LOOPBACK_BIND",
          "ALLOW_NON_LOOPBACK_BIND" in msg)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL BIND GUARD CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
