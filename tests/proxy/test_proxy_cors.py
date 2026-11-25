#!/usr/bin/env python3
# test-kind: offline
"""
C2 CORS allowlist tests (FEAT-001).

Verifies the proxy app's CORS middleware is a loopback allowlist, NOT a
wildcard:
  * an allowlisted origin is reflected back on a preflight;
  * a non-allowlisted origin is NOT granted access;
  * credentials are disabled (no allow-credentials: true);
  * methods and headers are explicitly enumerated (no '*'), and X-Proxy-Auth is
    an allowed header.

Run:
    python3 tests/proxy/test_proxy_cors.py
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

from proxy.platform.auth import allowed_origins  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _load_app():
    os.environ["PROXY_AUTH_TOKEN"] = "secret-token"
    os.environ.pop("PROXY_BIND_HOST", None)
    import proxy.server as server
    importlib.reload(server)
    return server.app


def _preflight(client, origin):
    """Send a CORS preflight for a protected route and return the response."""
    return client.options(
        "/package-categories",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Proxy-Auth",
        },
    )


def main():
    print("\n=== proxy CORS allowlist (C2) ===")
    if not _HAVE_TESTCLIENT:
        print("  ❌ fastapi.testclient.TestClient unavailable")
        sys.exit(1)

    app = _load_app()
    good_origin = allowed_origins()[0]  # http://localhost:5173
    bad_origin = "http://evil.example.com"

    with TestClient(app) as client:
        good = _preflight(client, good_origin)
        allow_origin = good.headers.get("access-control-allow-origin")
        check("allowlisted origin is reflected on preflight",
              allow_origin == good_origin)
        check("preflight allow-origin is NOT the wildcard '*'",
              allow_origin != "*")

        allow_methods = good.headers.get("access-control-allow-methods", "")
        check("allow-methods is explicit (no '*')",
              allow_methods != "" and "*" not in allow_methods)
        check("allow-methods enumerates GET and POST",
              "GET" in allow_methods and "POST" in allow_methods)

        allow_headers = good.headers.get("access-control-allow-headers", "")
        check("allow-headers includes X-Proxy-Auth",
              "x-proxy-auth" in allow_headers.lower())
        check("allow-headers is NOT the wildcard '*'", "*" not in allow_headers)

        # allow_credentials=False -> the credentials header must be absent/false.
        allow_creds = good.headers.get("access-control-allow-credentials")
        check("allow-credentials is not 'true'", allow_creds != "true")

        bad = _preflight(client, bad_origin)
        bad_allow_origin = bad.headers.get("access-control-allow-origin")
        check("non-allowlisted origin is NOT granted (no matching allow-origin)",
              bad_allow_origin != bad_origin)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY CORS CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
