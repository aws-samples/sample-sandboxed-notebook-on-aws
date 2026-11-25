"""Record OUTBOUND HTTP made directly from user Python cells as lineage sources.

A plain `requests.get("https://date.nager.at/api/...")` in a cell never passes
through a reader helper, so `_log_read` never sees it. `install()` wraps the
single choke point of each common HTTP client:

  - requests:        requests.Session.request   (requests.get/post/... all land here)
  - httpx:           httpx.Client.send / httpx.AsyncClient.send   (if installed)
  - urllib.request:  urllib.request.OpenerDirector.open   (urlopen + pandas URLs)

Each wrapper records `{"kind": "url", "id": "scheme://host[:port]"}` into
read_capture and then calls the original unchanged. Grouped BY HOST: path, query
string, fragment and userinfo are never recorded (API keys often live in query
strings), and repeated calls to one host dedupe to a single source. Only the
identifier is recorded — never bodies, headers, params or responses.

Recording happens only while a cell capture is active (read_capture._active is
not None) and NOT while a reader helper runs (the helper records its own id).
Skipped hosts: AWS endpoints (*.amazonaws.com, *.amazonaws.com.cn, *.api.aws),
loopback / localhost, link-local 169.254.x (IMDS) and unspecified addresses. The
VM makes no outbound calls to the notebook proxy (the proxy calls INTO the VM),
so there is no proxy host to skip. Direct boto3/botocore calls go through
urllib3, not these clients, so they are not captured (by design).

The hooks are transparent: recording is wrapped in try/except so it can never
raise into user code, and the original's arguments/return value/exceptions pass
through untouched. install() is idempotent (a wrapped function is marked and
never wrapped twice).
"""

import functools
import ipaddress
import logging
from urllib.parse import urlsplit

from . import read_capture

logger = logging.getLogger(__name__)

_MARK = "__lineage_http_hook__"
_AWS_SUFFIXES = ("amazonaws.com", "amazonaws.com.cn", "api.aws")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_skipped_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    for sfx in _AWS_SUFFIXES:
        if h == sfx or h.endswith("." + sfx):
            return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified


def host_id(url) -> str | None:
    """`scheme://host[:port]` for an http(s) URL, or None when it should not be
    recorded (non-http scheme, no host, skipped host). Default ports are dropped."""
    p = urlsplit(str(url))
    scheme = (p.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = p.hostname  # lowercased, userinfo stripped
    if not host or _is_skipped_host(host):
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = p.port
    if port and port != _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _record(url) -> None:
    """Best-effort: record the host of `url` if a capture is active. Never raises."""
    try:
        if not read_capture.is_active() or read_capture.in_helper():
            return
        ident = host_id(url)
        if ident:
            read_capture.record("url", ident)
    except Exception:
        pass


def _url_of_urllib(fullurl):
    # OpenerDirector.open takes a str or a urllib.request.Request.
    get = getattr(fullurl, "get_full_url", None)
    return get() if callable(get) else fullurl


def _wrap(owner, attr, url_of, is_async=False) -> bool:
    """Replace owner.attr with a recording wrapper. Returns True if (now) hooked."""
    orig = getattr(owner, attr, None)
    if orig is None:
        return False
    if getattr(orig, _MARK, False):
        return True  # already hooked — idempotent

    if is_async:
        @functools.wraps(orig)
        async def wrapper(*args, **kwargs):
            try:
                _record(url_of(args, kwargs))
            except Exception:
                pass
            return await orig(*args, **kwargs)
    else:
        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            try:
                _record(url_of(args, kwargs))
            except Exception:
                pass
            return orig(*args, **kwargs)

    setattr(wrapper, _MARK, True)
    setattr(owner, attr, wrapper)
    return True


def _requests_url(args, kwargs):
    # Session.request(self, method, url, ...)
    return kwargs["url"] if "url" in kwargs else args[2]


def _httpx_url(args, kwargs):
    # Client.send(self, request, ...)
    req = kwargs["request"] if "request" in kwargs else args[1]
    return req.url


def _urllib_url(args, kwargs):
    # OpenerDirector.open(self, fullurl, data=None, timeout=...)
    return _url_of_urllib(kwargs["fullurl"] if "fullurl" in kwargs else args[1])


def install() -> list:
    """Install the HTTP hooks (idempotent). Returns the names of hooked clients."""
    hooked = []
    try:
        import requests
        if _wrap(requests.Session, "request", _requests_url):
            hooked.append("requests")
    except Exception:
        pass
    try:
        import httpx
        ok = _wrap(httpx.Client, "send", _httpx_url)
        ok = _wrap(httpx.AsyncClient, "send", _httpx_url, is_async=True) and ok
        if ok:
            hooked.append("httpx")
    except Exception:
        pass
    try:
        import urllib.request
        if _wrap(urllib.request.OpenerDirector, "open", _urllib_url):
            hooked.append("urllib")
    except Exception:
        pass
    logger.debug("lineage HTTP capture hooks: %s", hooked)
    return hooked
