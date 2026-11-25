"""
Conversation memory for the chat agent — the proxy's READ/DELETE seam.

The chat agent's WRITE seam (make_session_manager / DbSessionRepository) moved to
ai_assistant/memory.py when the chat agent was extracted into its own service. This
module keeps ONLY the two seams the PROXY still owns, both selected by MEMORY_BACKEND:

  - READ seam (get_transcript_bubbles): rehydrate a saved conversation into UI
    bubbles on page refresh. sqlite/postgres read the agent_messages table via
    storage.agent_transcript_get(); agentcore reads AgentCore Memory via list_events
    (boto3) and _transcript.events_to_bubbles().
  - DELETE seam (delete_transcript): purge a session's conversation on notebook
    delete. sqlite/postgres rely on the DB cascade (no-op here); agentcore lists the
    session's events and deletes each by eventId (boto3).

Both agentcore paths use boto3.client("bedrock-agentcore", ...) — a hyphenated
service STRING, NOT the AgentCore SDK PACKAGE — so this module (and the proxy)
carries no import of that package. The write-seam's lazy AgentCore-SDK import moved
to ai_assistant/memory.py with the write seam.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "sqlite").lower()


# --- AgentCore Memory config (only read when MEMORY_BACKEND=agentcore) ----------
# memory_id is the AgentCore Memory resource id (create it once via the AgentCore
# CLI / boto3 / console). actor_id should be stable per user/identity so long-term
# memory namespaces stay consistent (AWS best practice); locally we don't have an
# authenticated user, so it falls back to a stable per-notebook id, then the
# session_id. region falls back to AWS_REGION.
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
AGENTCORE_ACTOR_ID = os.environ.get("AGENTCORE_ACTOR_ID", "")  # optional explicit override
AGENTCORE_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-west-2"))


def _resolve_actor_id(session_id: str) -> str:
    """Stable actor_id for AgentCore Memory.

    AWS best practice: pin actor_id to a stable identity so memory namespaces stay
    consistent. It MUST be derivable identically at WRITE time (agent turn) and READ
    time (transcript rehydration on refresh). The only identifier guaranteed at both
    points is the session_id — which in this app is 1:1 with the notebook (the
    notebook's stable UUID), so it already IS a stable per-notebook identity. Using
    anything only available at write time (e.g. a notebook_id from the agent's
    context) would make the read path resolve a DIFFERENT actor and return an empty
    transcript. An explicit AGENTCORE_ACTOR_ID env always wins (set it to the Cognito
    sub / internal user id in the cloud, where it's available on both paths)."""
    return AGENTCORE_ACTOR_ID or session_id


# ── Transcript READ path (rehydrate the chat on page refresh) ───────────────────
# The frontend loads a saved conversation via GET /ai/chat/{id}/messages, which must
# return UI bubbles from WHEREVER the active backend persisted them:
#   - sqlite/postgres: the agent_messages table (Strands SessionMessage dicts) →
#     storage.agent_transcript_get() → _transcript.messages_to_bubbles().
#   - agentcore: the managed AgentCore Memory service (list_events) →
#     _transcript.events_to_bubbles().
# Both translators return the IDENTICAL bubble shape, so the frontend is unchanged.
# This dispatch mirrors the WRITE seam (now in ai_assistant/memory.make_session_manager):
# the READ seam is selected by the same MEMORY_BACKEND flag.

def _agentcore_client():
    """Return a bedrock-agentcore data-plane client, or None if unusable (missing
    memory id or boto3). Callers degrade gracefully on None."""
    if not AGENTCORE_MEMORY_ID:
        return None
    try:
        import boto3
    except Exception as e:  # noqa: BLE001
        logger.warning("agentcore: boto3 unavailable (%s)", e)
        return None
    return boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)


def _agentcore_list_all_events(client, session_id: str, actor_id: str) -> list[dict]:
    """Return ALL events for (memory, actor, session), paginated. includePayloads so
    the reader gets conversational text. Raises on API error (callers decide)."""
    events: list[dict] = []
    next_token: Optional[str] = None
    while True:
        kwargs = dict(
            memoryId=AGENTCORE_MEMORY_ID,
            sessionId=session_id,
            actorId=actor_id,
            includePayloads=True,
            maxResults=100,
        )
        if next_token:
            kwargs["nextToken"] = next_token
        resp = client.list_events(**kwargs)
        events.extend(resp.get("events") or [])
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return events


def _agentcore_transcript_bubbles(session_id: str) -> list[dict]:
    """Read the conversation for a session from AgentCore Memory and translate it to
    UI chat bubbles. Lists conversational events (paginated) for our
    (memory_id, actor_id, session_id) and hands them to events_to_bubbles().

    Degrades to an empty transcript (never raises) if the memory id is unset or the
    service call fails — a missing/unreachable transcript must not break the chat
    panel load (the live conversation still works; only rehydration is empty)."""
    client = _agentcore_client()
    if client is None:
        logger.warning("agentcore transcript read skipped: memory id unset / boto3 missing")
        return []

    from proxy.storage._transcript import events_to_bubbles  # translator only (no DB)

    actor_id = _resolve_actor_id(session_id)
    try:
        events = _agentcore_list_all_events(client, session_id, actor_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "agentcore transcript read failed (memory_id=%s session=%s actor=%s): %s",
            AGENTCORE_MEMORY_ID, session_id, actor_id, e,
        )
        return []

    return events_to_bubbles(events)


def get_transcript_bubbles(session_id: str, notebook_id: Optional[str] = None) -> list[dict]:
    """Return the saved conversation as UI chat bubbles for the ACTIVE memory backend.

    - agentcore: read from AgentCore Memory (list_events → events_to_bubbles).
    - sqlite/postgres: read from the DB (storage.agent_transcript_get, which uses
      messages_to_bubbles). Imported lazily so the agentcore path keeps zero
      proxy.storage dependency.

    SYNCHRONOUS (blocking). Async route handlers should offload it via
    asyncio.to_thread (see routes/ai.py)."""
    if MEMORY_BACKEND == "agentcore":
        return _agentcore_transcript_bubbles(session_id)
    from proxy.storage import storage
    return storage.agent_transcript_get(session_id)


# ── DELETE seam: purge a session's conversation (notebook delete / new-chat reset) ─
# Deleting a notebook must also delete its chat memory. For sqlite/postgres this is
# the DB cascade (notebook_delete removes the agent_sessions row → agent_messages
# cascade), so the DELETE seam is a no-op there. For agentcore the conversation
# lives in the managed service, so we must explicitly delete its events — AgentCore
# has no session-scoped bulk delete, so we list the session's events and delete each
# by eventId (verified against a live memory resource). Same MEMORY_BACKEND dispatch
# as the read/write seams.

def _agentcore_delete_transcript(session_id: str) -> int:
    """Delete ALL events for (memory, actor, session) from AgentCore Memory. Returns
    the number of events deleted. Best-effort: logs and returns the count so far on
    any error (a failed purge must not block the notebook delete — the DB row is
    already gone). No-op (0) if the memory id is unset / boto3 missing."""
    client = _agentcore_client()
    if client is None:
        logger.warning("agentcore transcript delete skipped: memory id unset / boto3 missing")
        return 0

    actor_id = _resolve_actor_id(session_id)
    deleted = 0
    try:
        events = _agentcore_list_all_events(client, session_id, actor_id)
        for ev in events:
            event_id = ev.get("eventId")
            if not event_id:
                continue
            try:
                client.delete_event(
                    memoryId=AGENTCORE_MEMORY_ID,
                    sessionId=session_id,
                    actorId=actor_id,
                    eventId=event_id,
                )
                deleted += 1
            except Exception as e:  # noqa: BLE001 — per-event best-effort
                logger.warning("agentcore delete_event failed (event=%s): %s", event_id, e)
        logger.info(
            "agentcore transcript deleted: %d events (memory_id=%s session=%s actor=%s)",
            deleted, AGENTCORE_MEMORY_ID, session_id, actor_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "agentcore transcript delete failed (memory_id=%s session=%s actor=%s): %s",
            AGENTCORE_MEMORY_ID, session_id, actor_id, e,
        )
    return deleted


def delete_transcript(session_id: str) -> None:
    """Purge a session's conversation from the ACTIVE memory backend.

    - agentcore: delete the session's events from AgentCore Memory (list + per-event
      delete). The DB-side notebook_delete still runs its own cascade for any legacy
      rows, so calling this is additive and safe.
    - sqlite/postgres: NO-OP. The conversation lives in the DB, and notebook_delete
      already removes agent_sessions (→ agent_messages cascade). We deliberately do
      NOT double-delete here to keep a single, well-understood cascade path.

    SYNCHRONOUS (blocking). Async callers should offload via asyncio.to_thread."""
    if MEMORY_BACKEND == "agentcore":
        _agentcore_delete_transcript(session_id)
    # sqlite/postgres: handled by the DB cascade in notebook_delete — nothing to do.
