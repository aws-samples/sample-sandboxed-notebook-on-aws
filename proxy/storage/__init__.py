"""
Storage abstraction layer for the Sandboxed Notebook on AWS proxy.

Provides a single `storage` instance that implements the StorageBackend interface.
The backend and connection details are configured via environment variables
(set in scripts/config.sh):

  STORAGE_BACKEND:    "sqlite" (default), "postgres"
  STORAGE_CONNECTION: Connection string (empty for sqlite, URL for postgres)

Usage in proxy code:
    from proxy.storage import storage

    notebooks = storage.notebook_list()
    storage.vm_session_create(microvm_id=..., ...)

Async route handlers should prefer the `astorage` facade, which runs each
(blocking) storage call in a worker thread so it doesn't stall the event loop:

    from proxy.storage import astorage

    notebooks = await astorage.notebook_list()

Background threads (e.g. intel workers) keep using the sync `storage`.
"""

import os
from proxy.storage.interface import StorageBackend

_backend_type = os.environ.get("STORAGE_BACKEND", "sqlite").lower()
_connection_string = os.environ.get("STORAGE_CONNECTION", "")

if _backend_type == "sqlite":
    from proxy.storage.sqlite_db import SqliteStorage
    storage: StorageBackend = SqliteStorage()
elif _backend_type == "postgres":
    from proxy.storage.postgres_db import PostgresStorage
    storage: StorageBackend = PostgresStorage(connection_string=_connection_string)
else:
    raise ValueError(f"Unknown STORAGE_BACKEND: {_backend_type}. Supported: sqlite, postgres")

# Async facade over the sync backend (offloads each call to a thread). Import
# this from async route handlers so blocking DB I/O doesn't run on the event loop.
from proxy.storage.async_storage import AsyncStorage
astorage: AsyncStorage = AsyncStorage(storage)
