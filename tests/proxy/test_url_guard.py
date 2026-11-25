#!/usr/bin/env python3
# test-kind: offline
"""Offline unit test for the SSRF guard (proxy.notebook.routes._url_guard).

Verifies, WITHOUT any live network for the REJECT cases:
  - internal/metadata/loopback/private targets and non-http(s) schemes are REJECTED.
  - public hosts are ACCEPTED (socket.getaddrinfo is stubbed so the accept cases
    stay offline and deterministic).

Run:  python tests/proxy/test_url_guard.py    (exit 0 = pass, 1 = fail)
"""
import os
import socket
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from proxy.notebook.routes import _url_guard
from proxy.notebook.routes._url_guard import validate_public_url

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def _getaddrinfo_stub(public_hosts):
    """Return a getaddrinfo replacement: public_hosts resolve to a public IP;
    'localhost' resolves to loopback; everything else raises gaierror. Keeps the
    whole test offline and deterministic."""
    def _stub(host, *args, **kwargs):
        if host in public_hosts:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        if host == "localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        raise socket.gaierror("stub: unresolvable host")
    return _stub


def main() -> int:
    print("== REJECT cases (no live network) ==")
    # Bare IP literals: getaddrinfo returns the literal back, so these need no DNS.
    for bad in [
        "http://169.254.169.254/",   # EC2 metadata (link-local)
        "http://127.0.0.1/",         # loopback
        "http://10.0.0.5/",          # RFC-1918 private
        "http://192.168.1.1/",       # RFC-1918 private
    ]:
        err = validate_public_url(bad)
        check(f"REJECT {bad}", isinstance(err, str) and err != "", f"got {err!r}")

    # Non-http(s) scheme — rejected before any resolution.
    err = validate_public_url("file:///etc/passwd")
    check("REJECT file:///etc/passwd", isinstance(err, str) and err != "", f"got {err!r}")

    # Hostname resolving to loopback — stub the resolver to loopback (offline).
    _orig = _url_guard.socket.getaddrinfo
    _url_guard.socket.getaddrinfo = _getaddrinfo_stub(set())
    try:
        err = validate_public_url("http://localhost/")
        check("REJECT http://localhost/ (resolves to loopback)",
              isinstance(err, str) and err != "", f"got {err!r}")
    finally:
        _url_guard.socket.getaddrinfo = _orig

    print("== ACCEPT cases (resolver stubbed to a public IP) ==")
    public = {"raw.githubusercontent.com", "api.worldbank.org"}
    _url_guard.socket.getaddrinfo = _getaddrinfo_stub(public)
    try:
        for good in [
            "https://raw.githubusercontent.com/user/repo/main/x.ipynb",
            "https://api.worldbank.org/v2/country",
        ]:
            err = validate_public_url(good)
            check(f"ACCEPT {good}", err is None, f"got {err!r}")
    finally:
        _url_guard.socket.getaddrinfo = _orig

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("RESULT: all checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
