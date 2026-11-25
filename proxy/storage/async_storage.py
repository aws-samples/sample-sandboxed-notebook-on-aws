"""
Async wrapper around the synchronous StorageBackend.

Part of: proxy.storage

The storage backends (SQLite via a thread-local connection, Postgres via
psycopg2) are fully SYNCHRONOUS — every method does blocking disk/socket I/O.
Calling them directly from an `async def` FastAPI handler runs that blocking I/O
ON the single event loop, so an unrelated request (e.g. the Run History card's
GET /notebooks/{id}/runs) can't be scheduled until the loop is free. Under load
that shows up as a spinner that hangs while the loop is busy.

AsyncStorage wraps the `storage` singleton and exposes EVERY method as an async
call that runs the underlying sync method in a worker thread via
`asyncio.to_thread`. Handlers switch from:

    runs = storage.notebook_run_list(nb_id, limit=15)

to:

    runs = await astorage.notebook_run_list(nb_id, limit=15)

so the blocking DB call happens off-loop and the event loop stays responsive.

Notes:
  • Use `astorage` ONLY from async code running on the event loop (route
    handlers). Code already running in its own thread (e.g. the intel
    background workers in proxy/notebook/ai/intel/store.py) should keep calling
    the plain sync `storage` — hopping to yet another thread would add overhead
    for no benefit.
  • This is a thin dispatch layer: it adds no caching, batching, or state. The
    sync `storage` remains the single source of truth and is still importable
    for background/non-async callers.
  • `__getattr__` dispatch means new StorageBackend methods are covered
    automatically, with no per-method boilerplate to keep in sync.
"""

import asyncio
import functools
from typing import Any, Callable

from proxy.storage.interface import StorageBackend


class AsyncStorage:
    """Async facade over a synchronous StorageBackend.

    Every attribute access that resolves to a callable on the wrapped backend is
    returned as an async function that offloads the call to a thread. Non-callable
    attributes are returned as-is (passthrough).
    """

    def __init__(self, sync_backend: StorageBackend):
        self._sync = sync_backend
        # Cache the per-name async wrappers so repeated access doesn't rebuild them.
        self._wrapped: dict[str, Callable] = {}

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only called when normal attribute lookup fails, so this
        # never shadows _sync/_wrapped (real instance attributes).
        attr = getattr(self._sync, name)
        if not callable(attr):
            # Passthrough for plain attributes/properties on the backend.
            return attr

        cached = self._wrapped.get(name)
        if cached is not None:
            return cached

        @functools.wraps(attr)
        async def _async_call(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        self._wrapped[name] = _async_call
        return _async_call
