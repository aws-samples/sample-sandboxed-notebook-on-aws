#!/usr/bin/env python3
# test-kind: offline
"""
C3 terminal WebSocket relay auth tests (FEAT-001).

The /ws/terminal relay can resume a suspended VM and attach a shell, so it must
reject before accept() and before any VM resume when:
  * Origin is missing;
  * Origin is not on the loopback allowlist;
  * the shared-secret token is missing/invalid.

A good Origin + valid token must get PAST the gate (reach session resolution).
We prove "past the gate" with a fake vm_manager that records whether
get_session_vm was called — it is called only once the Origin+token gate has
passed, and never on a 4403 rejection.

Run:
    python3 tests/proxy/test_terminal_ws_auth.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    _HAVE_TESTCLIENT = True
except ImportError:
    _HAVE_TESTCLIENT = False

from proxy.platform.auth import allowed_origins  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


class _FakeVmManager:
    """Records whether session resolution was reached (i.e. the gate passed).
    Returns None so the handler closes 4004 right after the gate — we never
    touch a real VM or _ensure_vm_awake."""

    def __init__(self):
        self.get_session_vm_called = False

    def get_session_vm(self, session_id):
        self.get_session_vm_called = True
        return None  # unknown session -> handler closes 4004 (but gate passed)

    async def get_session_vm_async(self, session_id):
        self.get_session_vm_called = True
        return None  # unknown session -> handler closes 4004 (but gate passed)


def _build_app(vm_manager):
    from proxy.platform.routes.terminal import router as terminal_router

    app = FastAPI()
    app.state.vm_manager = vm_manager
    app.state.active_terminals = set()
    app.include_router(terminal_router)
    return app


def _connect(client, origin, subprotocols=None, token=None):
    """Attempt a WS connect; return the close code (int) the server used.

    Starlette's TestClient raises WebSocketDisconnect(code=...) when the server
    closes before/at accept. If the connection is accepted (gate passed) the
    fake vm_manager returns None and the handler closes 4004 — also surfaced as
    a WebSocketDisconnect we can read the code from.
    """
    url = "/ws/terminal?session_id=sess-1"
    if token is not None:
        url += f"&token={token}"
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    try:
        with client.websocket_connect(
            url, subprotocols=subprotocols, headers=headers
        ) as ws:
            # Accepted — drain until the server closes (4004 for unknown session).
            try:
                while True:
                    ws.receive()
            except WebSocketDisconnect as e:
                return e.code
            return None
    except WebSocketDisconnect as e:
        return e.code


def main():
    print("\n=== terminal WS relay auth (C3) ===")
    if not _HAVE_TESTCLIENT:
        print("  ❌ fastapi.testclient.TestClient unavailable")
        sys.exit(1)

    os.environ["PROXY_AUTH_TOKEN"] = "secret-token"
    good_origin = allowed_origins()[0]
    good_sub = ["x-proxy-auth.secret-token"]

    # (1) Missing Origin -> 4403, gate NOT passed.
    vm = _FakeVmManager()
    client = TestClient(_build_app(vm))
    code = _connect(client, origin=None, subprotocols=good_sub)
    check("missing Origin -> close 4403", code == 4403)
    check("missing Origin -> gate rejected before session resolution",
          vm.get_session_vm_called is False)

    # (2) Bad Origin -> 4403, gate NOT passed.
    vm = _FakeVmManager()
    client = TestClient(_build_app(vm))
    code = _connect(client, origin="http://evil.example.com", subprotocols=good_sub)
    check("non-allowlisted Origin -> close 4403", code == 4403)
    check("non-allowlisted Origin -> gate rejected before session resolution",
          vm.get_session_vm_called is False)

    # (3) Good Origin, NO token -> 4403, gate NOT passed.
    vm = _FakeVmManager()
    client = TestClient(_build_app(vm))
    code = _connect(client, origin=good_origin, subprotocols=None)
    check("good Origin + no token -> close 4403", code == 4403)
    check("good Origin + no token -> gate rejected before session resolution",
          vm.get_session_vm_called is False)

    # (4) Good Origin + bad token -> 4403.
    vm = _FakeVmManager()
    client = TestClient(_build_app(vm))
    code = _connect(client, origin=good_origin, subprotocols=["x-proxy-auth.wrong"])
    check("good Origin + wrong token -> close 4403", code == 4403)
    check("good Origin + wrong token -> gate rejected before session resolution",
          vm.get_session_vm_called is False)

    # (5) Good Origin + valid token (subprotocol) -> PAST the gate.
    vm = _FakeVmManager()
    client = TestClient(_build_app(vm))
    code = _connect(client, origin=good_origin, subprotocols=good_sub)
    check("good Origin + valid token -> gate PASSED (session resolution reached)",
          vm.get_session_vm_called is True)
    check("good Origin + valid token -> NOT a 4403 (closes 4004 for unknown session)",
          code != 4403)

    # (6) Good Origin + valid token via ?token= query-param fallback -> PAST gate.
    vm = _FakeVmManager()
    client = TestClient(_build_app(vm))
    code = _connect(client, origin=good_origin, subprotocols=None, token="secret-token")
    check("good Origin + valid ?token= fallback -> gate PASSED",
          vm.get_session_vm_called is True)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL TERMINAL WS AUTH CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
