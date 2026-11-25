"""Shared timestamp normalization for both storage backends.

Every timestamp the StorageBackend interface returns is an ISO-8601 UTC string
('YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00') or None, whichever backend served it:

  * SQLite stores TEXT in three shapes: Python isoformat() ('...T...+00:00'),
    strftime('%Y-%m-%dT%H:%M:%fZ') ('...Z') and datetime('now')
    ('YYYY-MM-DD HH:MM:SS', UTC, no 'T', no offset).
  * Postgres returns timestamptz columns as datetime objects.
"""

from datetime import datetime, timezone
from typing import Optional


def iso_utc(value) -> Optional[str]:
    """datetime | str | None -> ISO-8601 UTC string, or None.

    A naive datetime or naive string is taken as UTC (SQLite's datetime('now')
    is UTC). A value with an offset (including 'Z') is converted to UTC.
    An unparseable value returns None, so write-path callers store NULL instead
    of a string that later breaks a timestamp cast."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            # Python 3.11+ accepts ' ' or 'T', fractional seconds, '+HH:MM' and 'Z'.
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# Timestamp columns each row shape returns (read path). Both backends normalize
# these keys with iso_utc_cols so callers see one format.
VM_SESSION_TS = ("launched_at", "terminated_at", "updated_at")
POOL_VM_TS = ("launched_at", "updated_at")
ENTITY_TS = ("last_discovered_at", "updated_at")
METRICS_TS = ("timestamp", "updated_at")


def iso_utc_cols(d: dict, cols) -> dict:
    """Normalize, in place, each key of `cols` present in row dict `d` with
    iso_utc. Returns `d`."""
    for c in cols:
        if c in d:
            d[c] = iso_utc(d[c])
    return d
