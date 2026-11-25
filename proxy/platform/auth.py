"""
Shared security primitives for the control-plane services (C1/C2/C3).

This module is deliberately dependency-light and side-effect-free so it can be
imported by proxy/server.py, pool_manager/server.py, and the terminal WS relay
without triggering any app construction.

What it provides:
  * require_proxy_auth  — a FastAPI dependency enforcing a shared-secret header
                          (X-Proxy-Auth) on the proxy app. Fails CLOSED (503)
                          when the server-side token env is unset.
  * is_token_valid      — constant-time token check reusable by the WS relay and
                          the pool-manager auth dependency.
  * allowed_origins     — the CORS allowlist (loopback UI origins only).
  * assert_loopback_bind — a startup guard that refuses a non-loopback bind
                          unless explicitly opted in or running in a container.

Design notes
------------
This is a LOCAL educational POC (published to the public aws-samples GitHub org, NOT an
Amazon-internal service). Auth here is a shared secret minted per-run by the
installer and handed to both the server (PROXY_AUTH_TOKEN / POOL_AUTH_TOKEN) and
the UI (VITE_PROXY_TOKEN). It intentionally references NO Amazon-internal auth
system — the code must stay portable for external readers.
"""

import hmac
import os

from fastapi import HTTPException, Request, WebSocket

# The HTTP header carrying the shared secret from the UI (and other callers) to
# the proxy. Mirrored in the UI (VITE_PROXY_TOKEN) and documented for the agent.
AUTH_HEADER = "X-Proxy-Auth"

# WebSocket subprotocol carrying the shared secret for the terminal relay,
# mirroring the outbound `lambda-microvms.authentication.<token>` pattern used
# to talk to the VM shell (terminal.py).
WS_SUBPROTOCOL_PREFIX = "x-proxy-auth."

# Loopback hostnames that never count as a network-exposing bind.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Proxy routes exempt from require_proxy_auth. GET /health is the ONLY one: the
# installer and orchestrator liveness checks hit it unauthenticated. A FastAPI
# app-level dependency runs on every route and CANNOT be cleared by a route-level
# `dependencies=[]`, so the exemption lives in the dependency itself (by path).
_PROXY_AUTH_EXEMPT_PATHS = {"/health"}


def _token(env_var: str = "PROXY_AUTH_TOKEN") -> str:
    """Return the configured server-side shared secret (may be empty/unset)."""
    return os.environ.get(env_var, "") or ""


def is_token_valid(value: str, env_var: str = "PROXY_AUTH_TOKEN") -> bool:
    """Constant-time compare `value` against the configured server-side token.

    Returns False (fail CLOSED) when the server-side token is unset/empty or the
    supplied value is empty — an unconfigured server authenticates NO ONE.
    """
    expected = _token(env_var)
    if not expected or not value:
        return False
    return hmac.compare_digest(str(value), expected)


def require_proxy_auth(request: Request = None, websocket: WebSocket = None):
    """FastAPI dependency: enforce the shared secret on the proxy app.

    Behaviour:
      * 503 (fail CLOSED) when PROXY_AUTH_TOKEN is unset/empty — the server is
        misconfigured and must not serve the restricted control plane.
      * 401 when the X-Proxy-Auth header is missing or does not match under a
        constant-time compare.
      * passes (returns None) otherwise.

    Both `request` and `websocket` are OPTIONAL so this one app-level dependency
    works on BOTH HTTP and WebSocket routes. FastAPI injects whichever matches
    the route's scope (a `Request` for HTTP, a `WebSocket` for a WS handshake)
    and leaves the other None. This matters because the dependency is attached
    app-wide (`dependencies=[Depends(require_proxy_auth)]`); if it declared only
    `request: Request`, FastAPI could not satisfy that parameter on a WebSocket
    route and would call it with no args → TypeError (regression that broke the
    terminal relay). WebSocket connections are NOT gated here — the WS terminal
    relay performs its own Origin + shared-secret check inside the handler before
    accept() (C3), which is the correct place since a dependency can't cleanly
    reject a handshake with an HTTP status.
    """
    # No HTTP Request in scope → this is a WebSocket handshake (FastAPI injected
    # `websocket` instead) or a non-HTTP scope. Skip the HTTP header gate: the WS
    # terminal relay enforces its own Origin + shared-secret check inside the
    # handler before accept() (C3). An HTTP dependency can't 401 a WS handshake
    # meaningfully anyway. Guarding on `request is None` (rather than
    # `websocket is not None`) also makes the both-None edge safe instead of
    # raising AttributeError on request.method below.
    if request is None:
        return None

    # GET /health is exempt (liveness). Checked here because an app-level
    # dependency cannot be overridden by a route-level `dependencies=[]`.
    if request.method == "GET" and request.url.path in _PROXY_AUTH_EXEMPT_PATHS:
        return None

    expected = _token("PROXY_AUTH_TOKEN")
    if not expected:
        # Fail closed: an unconfigured token means the gate cannot verify anyone.
        raise HTTPException(status_code=503, detail="auth not configured")

    supplied = request.headers.get(AUTH_HEADER, "")
    if not is_token_valid(supplied, "PROXY_AUTH_TOKEN"):
        raise HTTPException(status_code=401, detail="invalid or missing proxy auth")
    return None


def require_pool_auth(request: Request):
    """FastAPI dependency mirroring require_proxy_auth but for POOL_AUTH_TOKEN.

    Applied to the pool manager's privileged routes (/grant, /admin/*). The
    liveness/readiness/status probes stay unauthenticated so the installer and
    orchestrator health checks keep working.
    """
    expected = _token("POOL_AUTH_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="auth not configured")

    supplied = request.headers.get(AUTH_HEADER, "")
    if not is_token_valid(supplied, "POOL_AUTH_TOKEN"):
        raise HTTPException(status_code=401, detail="invalid or missing pool auth")
    return None


def allowed_origins():
    """CORS allowlist: the loopback UI origins only (no wildcard).

    Built from UI_PORT (default 5173, the Vite dev-server port). Both the
    localhost and 127.0.0.1 spellings are included because browsers treat them
    as distinct origins.
    """
    ui_port = os.environ.get("UI_PORT", "5173")
    return [
        f"http://localhost:{ui_port}",
        f"http://127.0.0.1:{ui_port}",
    ]


def assert_loopback_bind(env_var: str, service_name: str) -> None:
    """Refuse to start a service on a non-loopback bind unless explicitly allowed.

    Reads the bind host from `env_var` (default 127.0.0.1). If it is a loopback
    address the service is private and we return. A non-loopback bind is only
    permitted when:
      * ALLOW_NON_LOOPBACK_BIND == '1'  (explicit operator opt-in), OR
      * we are running inside a container (/.dockerenv present, or
        DOCKER_CONTAINER set) — where the port publish, not the in-container
        bind, is the real network boundary.
    Otherwise raise RuntimeError naming the opt-in env var.
    """
    host = os.environ.get(env_var, "127.0.0.1")
    if host in _LOOPBACK_HOSTS:
        return

    if os.environ.get("ALLOW_NON_LOOPBACK_BIND") == "1":
        return

    in_container = os.path.exists("/.dockerenv") or bool(
        os.environ.get("DOCKER_CONTAINER")
    )
    if in_container:
        return

    raise RuntimeError(
        f"{service_name}: refusing to bind {env_var}={host!r} on a non-loopback "
        f"interface. This exposes an authenticated-but-local control plane to the "
        f"network. Set ALLOW_NON_LOOPBACK_BIND=1 to override intentionally, or "
        f"run inside a container (the port publish is then the boundary)."
    )
