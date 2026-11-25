"""Thread-safe cached boto3 client factory for the proxy.

``boto3.client(...)`` is expensive (service-model JSON load, endpoint
resolution, credential-chain walk: typically 10-100 ms of CPU) and the
default-session factory is not thread-safe while it builds a client. The proxy
used to construct a fresh client on every request/discovery call, often from
``asyncio.to_thread`` workers. This module builds each distinct client ONCE
and reuses it; botocore clients are thread-safe once constructed.

Invariants:
  - Same service / region / ``Config`` as the original call site (the cache key
    includes the Config's user-provided options, so different retry/timeout
    settings get different clients).
  - Credentials are still resolved by botocore; refreshable providers (SSO,
    assume-role, container/instance metadata) keep refreshing inside the
    cached client.
  - ``boto3.client`` is looked up at CALL time and a cached entry is rebuilt
    when ``boto3.client`` is no longer the callable that created it, so tests
    that monkeypatch ``boto3.client`` per scenario keep working.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

import boto3

_lock = threading.Lock()
# key -> (creator, client)
_cache: dict[tuple, tuple[Any, Any]] = {}


def _config_key(config) -> Optional[tuple]:
    if config is None:
        return None
    opts = getattr(config, "_user_provided_options", None) or {}
    return tuple(sorted((k, repr(v)) for k, v in opts.items()))


def get_client(service: str, region_name: Optional[str] = None, config=None):
    """Return a cached boto3 client for (service, region_name, config)."""
    key = (service, region_name, _config_key(config))
    creator = boto3.client
    entry = _cache.get(key)
    if entry is not None and entry[0] is creator:
        return entry[1]
    with _lock:
        creator = boto3.client
        entry = _cache.get(key)
        if entry is not None and entry[0] is creator:
            return entry[1]
        kwargs: dict[str, Any] = {}
        if region_name is not None:
            kwargs["region_name"] = region_name
        if config is not None:
            kwargs["config"] = config
        client = creator(service, **kwargs)
        _cache[key] = (creator, client)
        return client
