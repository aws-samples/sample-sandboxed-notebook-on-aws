"""Per-cell capture of the data sources a Python cell READ (lineage "Sources").

The `_log_read` decorator (helpers/_common.py) calls `record(kind, ident)` every
time a reader helper (read_athena, read_s3_csv, read_dynamodb, …) is invoked; the
executor brackets each cell with `begin()` / `end()` and returns the collected
list as `sources_read` on the /execute result.

Only IDENTIFIERS are recorded — table names, s3:// URIs, local paths, URLs with
the query string/fragment/userinfo dropped, KB ids, log-group names. Never SQL
text, a KB's natural-language query, or row data.

A single module-global capture is safe because SandboxExecutor.execute holds
_exec_lock for the whole cell, and SQL cells run under the shared app execute
lock, so at most one cell is ever capturing at a time. Outside a capture
(`_active is None`) `record()` is a no-op. Nothing here ever raises into a reader.

Raw outbound HTTP from a cell (requests / httpx / urllib) is recorded too, by the
hooks in http_capture.py, as kind "url" with a scheme://host[:port] id. While a
reader helper runs, `_log_read` holds `helper_scope()` so the helper's own
identifier wins and its internal HTTP calls are not double-recorded.
"""

import threading
from contextlib import contextmanager

_MAX_ENTRIES = 200   # per-cell cap so a loop over thousands of keys stays bounded
_MAX_ID_LEN = 512    # per-identifier cap

_lock = threading.Lock()
_active: list | None = None
_seen: set = set()
# >0 while a reader helper (wrapped by _log_read) is running. Process-global, not
# thread-local, so HTTP issued from a helper's worker threads is suppressed too
# (only one cell captures at a time — see the module docstring).
_helper_depth = 0


@contextmanager
def helper_scope():
    """Mark a reader helper as running: HTTP hooks skip recording inside it."""
    global _helper_depth
    with _lock:
        _helper_depth += 1
    try:
        yield
    finally:
        with _lock:
            _helper_depth -= 1


def in_helper() -> bool:
    return _helper_depth > 0


def is_active() -> bool:
    return _active is not None


def begin() -> None:
    """Start a fresh capture (discarding any previous, unfinished one)."""
    global _active, _seen
    with _lock:
        _active = []
        _seen = set()


def record(kind, ident) -> None:
    """Record one source identifier. No-op when no capture is active or the
    identifier is empty; dedupes on (kind, id); caps the list; never raises."""
    global _active
    try:
        if _active is None or ident is None:
            return
        ident_s = str(ident)[:_MAX_ID_LEN]
        if not ident_s:
            return
        kind_s = str(kind)
        key = (kind_s, ident_s)
        with _lock:
            if _active is None or key in _seen or len(_active) >= _MAX_ENTRIES:
                return
            _seen.add(key)
            _active.append({"kind": kind_s, "id": ident_s})
    except Exception:
        pass


def end() -> list:
    """Stop the capture and return the recorded [{kind, id}] list (in order)."""
    global _active, _seen
    with _lock:
        out = _active or []
        _active = None
        _seen = set()
    return out
