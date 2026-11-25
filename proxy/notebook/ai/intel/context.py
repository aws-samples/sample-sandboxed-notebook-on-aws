"""
Entity-doc context gathering for Workbook Intelligence.

Part of: proxy.notebook.ai.intel

Pulls the pre-computed markdown profile for every data source known to a
session (global entities from batch discovery + local files profiled this
session) into the text fed to the intel prompt.
"""

import os
import logging

import httpx

from batch.entity_discovery import get_entity_doc_markdown
from proxy.platform.errors import emit_error_metric

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


# The intel report only READS the current catalog to enrich the report (source
# count + entity profiles). It has no reason to trigger a cloud re-discovery + push
# to the VM. So we call /datasources/catalog with refresh=false (READ-ONLY): the
# proxy skips the expensive resync push and just reads + enriches the VM's current
# catalog. This removes the contention that was timing out post-upload deltas (the
# resync push competed with the VM's in-progress upload + local rediscovery). A
# modest timeout still covers a cold/slow VM read; on failure we fall back to the
# agent's own get_available_data_sources/execute_code tools.
_CATALOG_FETCH_TIMEOUT = 15.0


def _fetch_session_catalog(proxy_url: str, session_id: str) -> list[dict]:
    """Fetch this session's VM data catalog entries (all source types), READ-ONLY
    (refresh=false → no resync push, no cloud re-discovery)."""
    # Server-to-server call back into the proxy: it is gated by X-Proxy-Auth like
    # every other route, so attach the shared secret when it's in the env.
    headers = {"X-Session-Id": session_id}
    token = os.environ.get("PROXY_AUTH_TOKEN")
    if token:
        headers["X-Proxy-Auth"] = token
    try:
        resp = httpx.get(
            f"{proxy_url}/datasources/catalog",
            headers=headers,
            params={"refresh": "false"},
            timeout=_CATALOG_FETCH_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("entries", [])
        logger.warning(
            f"[intel] Catalog fetch for session {session_id[:8]}... returned "
            f"HTTP {resp.status_code} (report enrichment will fall back to agent tools)"
        )
        emit_error_metric("intel", "_fetch_session_catalog", f"HTTP{resp.status_code}", resp.status_code)
    except Exception as e:
        logger.warning(
            f"[intel] Failed to fetch catalog for session {session_id[:8]}... "
            f"(report enrichment falls back to agent tools): {type(e).__name__}: {e!r}"
        )
        emit_error_metric("intel", "_fetch_session_catalog", type(e).__name__, 504)
    return []


# Human labels for the per-type source breakdown line in the report.
_SOURCE_TYPE_LABELS = {
    "s3": "S3",
    "dynamodb": "DynamoDB",
    "athena": "Athena",
    "aurora": "Aurora PostgreSQL",
    "bedrock_kb": "Bedrock Knowledge Base",
    "local": "local",
}


def compute_source_counts(session_id: str, proxy_url: str = None, storage=None) -> dict | None:
    """Count the session's data sources by type — the AUTHORITATIVE number for the
    report's "N distinct data sources" line. We compute this in code (not via the
    LLM) because the model consistently miscounts / uses a stale value, producing an
    intro count that disagrees with the sources it actually lists.

    Enumeration is VM-FREE (proxy cloud catalog + DB local entities via
    _enumerate_session_sources) so it can't time out against a busy VM. `storage`
    is required for the local-file count; if omitted, only cloud sources are counted.

    Returns {"total": int, "by_type": {type: n}, "breakdown": "X S3, Y DynamoDB, ..."}
    or None if nothing is known yet (caller should then leave the count alone).
    """
    sources = _enumerate_session_sources(session_id, storage) if storage is not None else []
    if not sources:
        return None
    by_type: dict[str, int] = {}
    seen = set()
    for stype, sid in sources:
        if not sid or sid in seen:
            continue
        seen.add(sid)
        by_type[stype] = by_type.get(stype, 0) + 1
    total = sum(by_type.values())
    if total == 0:
        return None
    # Stable, readable breakdown ordered by our known types, then any extras.
    order = ["s3", "dynamodb", "athena", "aurora", "bedrock_kb", "local"]
    parts = []
    for t in order:
        if by_type.get(t):
            parts.append(f"{by_type[t]} {_SOURCE_TYPE_LABELS[t]}")
    # Any type not in `order` still gets counted — use its pretty label if we have
    # one, else fall back to the raw type name.
    for t, n in by_type.items():
        if t not in order and n:
            parts.append(f"{n} {_SOURCE_TYPE_LABELS.get(t, t)}")
    return {"total": total, "by_type": by_type, "breakdown": ", ".join(parts)}


def _enumerate_session_sources(session_id: str, storage) -> list[tuple[str, str]]:
    """Enumerate (source_type, source_id) for every source known to a session —
    WITHOUT touching the VM.

    Why not read the VM catalog here: this is called during the intel DELTA, right
    after the VM profiled the just-uploaded files (CPU-heavy). A live VM catalog
    read at that moment contends with that work and times out (the ReadTimeout we
    saw). But we don't need the VM for this: entity-doc enumeration only needs the
    list of source_ids, and the proxy already has them authoritatively —
      • cloud sources: registry.discover_full_catalog_sync (proxy-derived), and
      • local files:   storage.local_entity_list (the DB, populated by discovery).
    So we assemble the id list from those two, no VM round-trip.
    """
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Cloud sources — proxy-authoritative, no VM.
    try:
        from proxy.platform.datasources import registry
        for entry in registry.discover_full_catalog_sync(session_id=session_id) or []:
            sid = entry.get("source_id")
            stype = entry.get("source_type")
            if sid and sid not in seen:
                seen.add(sid)
                sources.append((stype, sid))
    except Exception as e:
        logger.warning(f"[intel] cloud source enumeration failed for {session_id[:8]}...: {e}")

    # Local /tmp files — from the DB (discovery writes rows here), no VM.
    try:
        for row in storage.local_entity_list(session_id) or []:
            sid = row.get("filepath")
            if sid and sid not in seen:
                seen.add(sid)
                sources.append(("local", sid))
    except Exception as e:
        logger.warning(f"[intel] local source enumeration failed for {session_id[:8]}...: {e}")

    return sources


def _fetch_relevant_entity_docs(session_id: str, proxy_url: str, bucket: str, storage) -> str:
    """
    Pull the pre-computed markdown profile for every data source known to this
    session (global entities from batch/entity_discovery.py's Phase 1,
    plus local files already profiled this session), and concatenate them for
    the INTEL_PROMPT's precomputed_entity_profiles section.

    Source enumeration is VM-FREE (see _enumerate_session_sources) so it can't time
    out against a busy VM mid-delta; each entity's doc is then read from S3/DB.
    Entities without a ready doc yet (never discovered, or discovery failed) are
    simply omitted — the agent's own execute_code/get_available_data_sources tools
    remain the fallback for anything not covered here.
    """
    sources = _enumerate_session_sources(session_id, storage)
    if not sources:
        return "(No precomputed profiles available — this session's data catalog hasn't finished discovery yet. Use get_available_data_sources and execute_code as usual.)"

    sections = []
    for source_type, source_id in sources:
        if not source_id:
            continue
        is_local = source_type == "local"
        doc = get_entity_doc_markdown(
            source_id, bucket, AWS_REGION, storage,
            session_id=session_id if is_local else None,
        )
        if doc:
            sections.append(doc.strip())

    if not sections:
        return "(No precomputed profiles are ready yet for this session's sources. Use get_available_data_sources and execute_code as usual.)"

    return "\n\n---\n\n".join(sections)
