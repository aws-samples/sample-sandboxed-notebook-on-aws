"""Pure helpers for the notebook_dag row, shared by SqliteStorage and
PostgresStorage so both backends parse, stamp, serialize and size-check the
per-notebook DAG document identically (design §7.1). Stdlib + the shared
app.notebook.dag_contract only — no DB imports.

The document itself is built by proxy/notebook/dag.py; storage owns the stamps
(version, runtime_version, graph_hash, runtime_hash, built_at, notebook_id, and
`trigger` on a runtime-only write), the no-op change check and the write guard
(delta-plan §0.2):
  * graph unchanged and runtime unchanged → nothing is written (no SQL);
  * graph changed → version + 1, built_at = now;
  * runtime-only change → version/built_at/trigger kept, runtime_version + 1.
The row always stores the FULL document.
"""

import json
import logging
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from app.notebook.dag_contract import (
    DAG_MAX_BYTES,
    DAG_SCHEMA,
    clock_projection,
    graph_hash,
    runtime_hash,
    stored_hashes,
)

logger = logging.getLogger(__name__)


class DagWrite(NamedTuple):
    """Result of one DAG read-modify-write.

    prev:            the stored doc before (None when there was none)
    doc:             the stored doc after (== prev when nothing was written)
    wrote:           a row was written
    graph_changed:   graph_hash changed (version bumped)
    runtime_changed: runtime_hash changed (runtime_version bumped)
    vm_changed:      what the VM copy holds changed (graph or run clocks)
    nbytes:          UTF-8 size of the stored dag_json when written, else None"""
    prev: Optional[dict]
    doc: Optional[dict]
    wrote: bool
    graph_changed: bool
    runtime_changed: bool
    vm_changed: bool
    nbytes: Optional[int]


def unchanged_write(prev) -> DagWrite:
    return DagWrite(prev, prev, False, False, False, False, None)


def load_stored_doc(dag_json, version, built_at, notebook_id) -> Optional[dict]:
    """Parse a stored dag_json. None when empty / unparseable / not a dict /
    schema != DAG_SCHEMA. The row's version/built_at/notebook_id always win over
    whatever the JSON carries; an absent runtime_version reads as 0."""
    if not dag_json:
        return None
    try:
        doc = json.loads(dag_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != DAG_SCHEMA:
        return None
    doc["version"] = int(version or 0)
    doc["built_at"] = built_at
    doc["notebook_id"] = notebook_id
    rv = doc.get("runtime_version")
    if isinstance(rv, bool) or not isinstance(rv, int):
        doc["runtime_version"] = 0
    return doc


def utc_now_ms() -> str:
    """UTC timestamp with millisecond precision and a 'Z' suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def serialize(doc: dict) -> str:
    """Compact JSON, non-ASCII kept as-is (the size guard measures UTF-8 bytes)."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def stamp_doc(prev: Optional[dict], new: dict, row_version, notebook_id, now: str) -> tuple:
    """Pure stamping + change check (delta-plan §0.2).

    Returns (doc, wrote, graph_changed, runtime_changed, vm_changed). When
    nothing changed, doc IS prev and wrote is False. The caller's dicts are not
    mutated."""
    g = graph_hash(new)
    r = runtime_hash(new)
    doc = dict(new)
    doc["notebook_id"] = notebook_id
    doc["graph_hash"] = g
    doc["runtime_hash"] = r
    if prev is None:
        doc["version"] = int(row_version or 0) + 1
        doc["runtime_version"] = 1
        doc["built_at"] = now
        return doc, True, True, True, True
    g_prev, r_prev = stored_hashes(prev)
    graph_changed = g != g_prev
    runtime_changed = r != r_prev
    if not graph_changed and not runtime_changed:
        return prev, False, False, False, False
    if graph_changed:
        doc["version"] = int(row_version or 0) + 1
        doc["built_at"] = now
    else:
        # Runtime-only: the graph is the stored one; keep its stamps.
        doc["version"] = prev.get("version")
        doc["built_at"] = prev.get("built_at")
        if "trigger" in prev:
            doc["trigger"] = prev["trigger"]
        else:
            doc.pop("trigger", None)
    rv = prev.get("runtime_version")
    rv = rv if isinstance(rv, int) and not isinstance(rv, bool) else 0
    doc["runtime_version"] = rv + (1 if runtime_changed else 0)
    vm_changed = graph_changed or clock_projection(prev) != clock_projection(doc)
    return doc, True, graph_changed, runtime_changed, vm_changed


def prepare_write(new_doc: dict, prev: Optional[dict], row_version, notebook_id) -> tuple:
    """Stamp a freshly built doc for storage (stamp_doc with now = utc_now_ms()).

    Returns (DagWrite, dag_json): dag_json is the text to UPSERT, or None when
    nothing must be written — no change, or the serialized doc exceeds
    DAG_MAX_BYTES (logged at error level; the previous doc is kept — nothing is
    ever truncated)."""
    doc, wrote, g_changed, r_changed, vm_changed = stamp_doc(
        prev, new_doc, row_version, notebook_id, utc_now_ms())
    if not wrote:
        return unchanged_write(prev), None
    text = serialize(doc)
    size = len(text.encode("utf-8"))
    if size > DAG_MAX_BYTES:
        logger.error("notebook_dag: doc for notebook %s is %d bytes (> %d DAG_MAX_BYTES); "
                     "not stored, previous version kept", notebook_id, size, DAG_MAX_BYTES)
        return unchanged_write(prev), None
    return DagWrite(prev, doc, True, g_changed, r_changed, vm_changed, size), text
