"""SSRF guard for user-supplied fetch URLs (import-from-url endpoint).

``validate_public_url`` is a dependency-free, stdlib-only (urllib.parse, socket,
ipaddress) check that blocks server-side request forgery against INTERNAL
destinations while still allowing any PUBLIC host — the endpoint is intentionally
NOT allowlisted to github. It returns a human-readable error string when the URL
is unsafe, else ``None`` (the string-or-None contract keeps call sites a simple
``if err: return 400``).

TOCTOU / DNS-rebinding residual: this validator resolves the hostname and checks
every returned IP, but httpx re-resolves the name at connect time, so a hostile
resolver could return a public IP here and a private IP at connect. A
resolve-and-check guard is the accepted POC mitigation; connection-level IP
pinning is intentionally NOT implemented (over-engineering for this POC).
"""

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}


def _ip_is_internal(ip_str: str) -> bool:
    """True if the IP falls in any non-public range we must block."""
    ip = ipaddress.ip_address(ip_str)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — the EC2 metadata endpoint
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> str | None:
    """Return an error string if ``url`` is unsafe to fetch, else ``None``.

    Rejects any non-http(s) scheme (file://, ftp://, gopher://, ...), empty host,
    unresolvable host, and any host whose resolution yields an internal IP
    (private, loopback, link-local/metadata, reserved, multicast, unspecified).
    A bare IP literal host is covered by the same per-IP check because
    ``socket.getaddrinfo`` returns that literal back.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid URL"

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return "URL scheme not allowed (only http/https)"

    host = parts.hostname
    if not host:
        return "URL has no host"

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "could not resolve host"

    resolved = {info[4][0] for info in infos}
    if not resolved:
        return "could not resolve host"

    for ip_str in resolved:
        try:
            if _ip_is_internal(ip_str):
                return "URL resolves to a non-public (internal) address"
        except ValueError:
            # Unparseable address from the resolver — treat as unsafe.
            return "URL resolves to an invalid address"

    return None
