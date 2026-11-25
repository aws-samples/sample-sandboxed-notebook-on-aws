#!/usr/bin/env python3
# test-kind: offline
"""
C1 proxy shared-secret auth tests (FEAT-001).

Exercises the module-level proxy app (`proxy.server:app` — the exact object
`-m uvicorn proxy.server:app` imports) through FastAPI's TestClient:

  * GET /health is reachable WITHOUT a token (the one exempt route).
  * Any other route is 401 without the X-Proxy-Auth header, 200/other with the
    correct token.
  * When PROXY_AUTH_TOKEN is unset, the gate FAILS CLOSED (503) on protected
    routes — an unconfigured server authenticates no one.

Run:
    python3 tests/proxy/test_proxy_auth.py
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    from fastapi.testclient import TestClient
    _HAVE_TESTCLIENT = True
except ImportError:
    _HAVE_TESTCLIENT = False


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _load_app(token):
    """(Re)import proxy.server with PROXY_AUTH_TOKEN set to `token` (or unset
    when token is None). The bind guard + global dependency are wired at import
    time, so each scenario re-imports fresh."""
    if token is None:
        os.environ.pop("PROXY_AUTH_TOKEN", None)
    else:
        os.environ["PROXY_AUTH_TOKEN"] = token
    # Ensure a clean loopback bind for the import-time assert_loopback_bind.
    os.environ.pop("PROXY_BIND_HOST", None)
    import proxy.server as server
    importlib.reload(server)
    return server.app


def test_health_exempt():
    app = _load_app("secret-token")
    with TestClient(app) as client:
        resp = client.get("/health")
        check("GET /health reachable WITHOUT a token (200)", resp.status_code == 200)


def test_protected_requires_token():
    app = _load_app("secret-token")
    with TestClient(app) as client:
        # /package-categories is a plain GET route on the app; no token -> 401.
        no_tok = client.get("/package-categories")
        check("protected route without token -> 401", no_tok.status_code == 401)

        with_tok = client.get(
            "/package-categories", headers={"X-Proxy-Auth": "secret-token"}
        )
        check("protected route WITH correct token -> not 401/503 (passes gate)",
              with_tok.status_code not in (401, 503))

        bad_tok = client.get(
            "/package-categories", headers={"X-Proxy-Auth": "wrong-token"}
        )
        check("protected route with WRONG token -> 401", bad_tok.status_code == 401)


def test_fail_closed_when_unset():
    app = _load_app(None)
    with TestClient(app) as client:
        resp = client.get("/package-categories")
        check("PROXY_AUTH_TOKEN unset -> protected route fails CLOSED (503)",
              resp.status_code == 503)
        # /health must still work even with no token configured.
        health = client.get("/health")
        check("PROXY_AUTH_TOKEN unset -> /health still reachable (200)",
              health.status_code == 200)


def main():
    print("\n=== proxy shared-secret auth (C1) ===")
    if not _HAVE_TESTCLIENT:
        print("  ❌ fastapi.testclient.TestClient unavailable")
        sys.exit(1)

    print("\n-- /health exemption --")
    test_health_exempt()
    print("\n-- protected routes require a token --")
    test_protected_requires_token()
    print("\n-- fail closed when token unset --")
    test_fail_closed_when_unset()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY AUTH CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
