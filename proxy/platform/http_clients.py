"""Shared, lifespan-managed httpx clients for the proxy.

The proxy used to build a fresh ``httpx.AsyncClient`` (new TLS handshake, new
connection pool) for every forwarded request. These module singletons are
created once in the FastAPI lifespan (``startup()``) and closed on shutdown
(``aclose()``); call sites pass their timeout PER REQUEST and must never close
the shared client.

Roles:
  - ``vm_client()``  — proxy -> MicroVM HTTPS endpoints.
  - ``aux_client()`` — localhost agent runtime + PyPI.

Retry policy (deliberately conservative — never re-send a request the server
may already have acted on):
  - Transport ``retries=2``: httpcore retries ONLY ``ConnectError`` /
    ``ConnectTimeout`` while establishing a connection, i.e. the request was
    provably never sent. Safe for every method.
  - ``request_idempotent()``: GET/HEAD only, ONE extra re-send on
    ``RemoteProtocolError`` / ``ReadError`` raised before any response exists
    (typically a stale pooled keep-alive connection dropped by a suspended /
    resumed / idle VM). Non-idempotent methods are re-raised untouched.

Security: no auth header is ever set on a shared client (callers pass
``X-aws-proxy-auth`` per request); TLS verification stays at httpx's default
(on); nothing request-specific (headers, bodies, tokens) is logged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})

_TRANSPORT_RETRIES = 2

_vm: Optional[httpx.AsyncClient] = None
_aux: Optional[httpx.AsyncClient] = None
# Event loop each lazily/startup-created client belongs to. Pooled connections
# are bound to the loop that opened them, so a client built under a loop that
# has since gone away (scripts/tests calling asyncio.run repeatedly) is rebuilt
# instead of reused. In the server there is one loop, so this never triggers.
# A client injected directly (tests) has no recorded loop and is left alone.
_vm_loop = None
_aux_loop = None


def _current_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _stale(client: Optional[httpx.AsyncClient], loop) -> bool:
    if client is None or client.is_closed:
        return True
    return loop is not None and loop is not _current_loop()


def _make(retries: int = _TRANSPORT_RETRIES) -> httpx.AsyncClient:
    # When transport= is given httpx ignores client-level limits/verify, so they
    # are set on the transport (verify defaults to True). max_connections=None:
    # before this change every request had its own client (no cap); a cap would
    # introduce a new PoolTimeout failure mode for long-running cells.
    # NOTE: with a custom transport httpx intentionally does NOT honor
    # HTTP(S)_PROXY / ALL_PROXY / NO_PROXY env vars (boto3 is unaffected).
    # See SETUP.md → Configuration ("Outbound HTTP proxy env vars").
    transport = httpx.AsyncHTTPTransport(
        retries=retries,
        limits=httpx.Limits(
            max_connections=None,
            max_keepalive_connections=100,
            keepalive_expiry=15.0,
        ),
    )
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10.0))


def vm_client() -> httpx.AsyncClient:
    """Shared client for proxy -> VM requests (created lazily outside the lifespan)."""
    global _vm, _vm_loop
    if _stale(_vm, _vm_loop):
        _vm, _vm_loop = _make(), _current_loop()
    return _vm


def aux_client() -> httpx.AsyncClient:
    """Shared client for localhost agent / PyPI requests (created lazily)."""
    global _aux, _aux_loop
    if _stale(_aux, _aux_loop):
        _aux, _aux_loop = _make(), _current_loop()
    return _aux


def startup() -> None:
    """Create both shared clients (idempotent). Call from the app lifespan."""
    vm_client()
    aux_client()


async def aclose() -> None:
    """Close and reset both shared clients (idempotent)."""
    global _vm, _aux, _vm_loop, _aux_loop
    clients = (_vm, _aux)
    _vm = _aux = _vm_loop = _aux_loop = None
    for client in clients:
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as e:
                logger.warning(f"Shared httpx client close failed: {type(e).__name__}")


async def request_idempotent(client: httpx.AsyncClient, method: str, url, **kwargs) -> httpx.Response:
    """``client.request`` with ONE re-send for GET/HEAD on a pre-response
    ``RemoteProtocolError`` / ``ReadError``. Any other method or error is
    re-raised unchanged (no re-send)."""
    try:
        return await client.request(method, url, **kwargs)
    except (httpx.RemoteProtocolError, httpx.ReadError) as e:
        if method.upper() not in IDEMPOTENT_METHODS:
            raise
        logger.info(f"Idempotent {method.upper()} retry after {type(e).__name__}")
        return await client.request(method, url, **kwargs)
