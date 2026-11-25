"""
Token cache abstraction for the Sandboxed Notebook on AWS proxy.

Provides an async `TokenCache` interface plus a `make_token_cache()` factory that
selects the backend via environment variable — mirroring the StorageBackend
pattern in proxy/storage/__init__.py.

  TOKEN_CACHE_BACKEND:  "memory" (default, the only implemented backend).
                        "redis" is reserved for a future ElastiCache backend and
                        raises NotImplementedError at startup today.
  TOKEN_CACHE_URL:      connection URL for that future redis/valkey backend (unused)

Why async?
----------
The in-memory backend does not need to be async (dict ops are instant), but the
interface is async so a future ElastiCache (Valkey/Redis) backend — whose I/O is
genuinely async via redis.asyncio — can be dropped in WITHOUT rippling `await`
changes back through every call site. Callers already `await` today.

Value semantics
---------------
Values are opaque dicts, e.g. {"token": "...", "expires_at": <epoch_seconds>}.
TTL / expiry is currently checked by the CALLER (get_auth_token compares
expires_at). `set()` accepts an optional `ttl_seconds` that the in-memory backend
ignores (it only enforces the LRU size bound); a Redis backend will map it to the
native key TTL (SET ... EX). Keeping expiry caller-side preserves the exact
behavior we have today.
"""

import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict


# L1 (in-process) size bound ONLY. This caps the per-instance cache — it is NOT
# a cap on total notebooks/MicroVMs in the fleet. Each proxy instance only holds
# hot tokens for the VMs *it* is actively routing to, so size this to the peak
# number of *concurrent* sessions a single proxy instance serves (not the fleet
# total). Tokens are ~25-min-lived and cheap to re-mint, so eviction just causes
# a re-mint on the next request for that VM.
#
# The shared L2 (Redis/ElastiCache, future) is a different story: it is the
# fleet-wide source of truth, holds a token per active VM, and is NOT bounded by
# this count — it evicts via native per-key TTL (ttl_seconds -> SET ... EX).
# RedisTokenCache must therefore NOT receive TOKEN_CACHE_MAX_SIZE.
TOKEN_CACHE_MAX_SIZE = int(os.environ.get("TOKEN_CACHE_MAX_SIZE", "1000"))


class TokenCache(ABC):
    """Async key/value cache interface for auth tokens.

    Keys are strings (e.g. a microvm id, or "shell:<microvm id>").
    Values are opaque dicts owned by the caller.
    """

    @abstractmethod
    async def get(self, key: str) -> dict | None:
        """Return the cached value for `key`, or None if absent."""
        ...

    @abstractmethod
    async def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        """Store `value` under `key`.

        `ttl_seconds` is a hint for backends with native expiry (e.g. Redis EX).
        The in-memory backend ignores it (expiry is checked caller-side).
        """
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove `key` if present (no-op if absent)."""
        ...

    @abstractmethod
    async def size(self) -> int:
        """Return the number of entries currently cached."""
        ...


class InMemoryTokenCache(TokenCache):
    """
    In-process L1 cache: LRU-bounded, thread-safe.

    Eviction policy: Least-Recently-Used (LRU).
    - get() promotes the accessed item to most-recent (move_to_end).
    - set() promotes updated items to most-recent.
    - When capacity is exceeded, popitem(last=False) evicts from the front
      (the item that hasn't been accessed/updated the longest).

    Thread-safety matters because the cache is touched both from the asyncio
    event loop (route handlers) and from background threads.
    """

    def __init__(self, max_size: int = TOKEN_CACHE_MAX_SIZE):
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    async def get(self, key: str) -> dict | None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    async def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        # ttl_seconds is intentionally ignored here — expiry is enforced by the
        # caller (see get_auth_token's expires_at check). A Redis backend will
        # honor it via native key TTL.
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def delete(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    async def size(self) -> int:
        with self._lock:
            return len(self._cache)


def make_token_cache() -> TokenCache:
    """Select and construct the token cache backend from the environment.

    Mirrors proxy.storage's STORAGE_BACKEND switch.
    """
    backend = os.environ.get("TOKEN_CACHE_BACKEND", "memory").lower()

    if backend == "memory":
        return InMemoryTokenCache()
    elif backend == "redis":
        # Future: ElastiCache (Valkey/Redis) backend for a stateless, horizontally
        # scaled proxy fleet. Sketch:
        #     from proxy.platform.redis_token_cache import RedisTokenCache
        #     return RedisTokenCache(url=os.environ.get("TOKEN_CACHE_URL", ""))
        # Notes for the implementation:
        #   - Use redis.asyncio; map ttl_seconds -> SET ... EX for native expiry.
        #   - Do NOT pass TOKEN_CACHE_MAX_SIZE: L2 is fleet-wide and unbounded by
        #     count; it evicts via per-key TTL, so it scales past 100/1000 VMs.
        #   - Optionally keep an InMemoryTokenCache (bounded by TOKEN_CACHE_MAX_SIZE)
        #     as an in-process L1 in front of Redis to cut hot-path round-trips.
        raise NotImplementedError(
            "Redis token cache backend not yet implemented. Set TOKEN_CACHE_BACKEND=memory"
        )
    else:
        raise ValueError(
            f"Unknown TOKEN_CACHE_BACKEND: {backend}. Supported: memory, redis"
        )
