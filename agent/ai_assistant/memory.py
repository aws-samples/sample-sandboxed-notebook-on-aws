"""
Conversation memory for the chat agent — the SessionManager WRITE seam.

This is the chat agent's copy of the memory module. It owns the WRITE seam (how the
agent persists/rehydrates its conversation) plus the agentcore DELETE chain that
`notebook_agent.new_thread` needs to purge a reset conversation from AgentCore
Memory. The backend is selected by the MEMORY_BACKEND env:

  - "sqlite"   (default, local/dev): a DB-backed Strands SessionManager. The agent
                auto-persists every message and rehydrates the conversation on cold
                start, all through the storage interface (SQLite locally, Postgres
                in the cloud — the SessionRepository never touches raw SQL).
  - "agentcore" (cloud): Amazon Bedrock AgentCore Memory via the SDK's
                AgentCoreMemorySessionManager.

SIBLING COPY: proxy/notebook/ai/memory.py keeps the READ seam
(get_transcript_bubbles + its agentcore read helpers) and its own DELETE seam for
the proxy's notebook-delete path. This copy carries ONLY the write seam and the
agentcore delete chain the chat agent calls directly. The two services do NOT
import each other, so DbSessionRepository / _scrub_attachment_bytes / _resolve_actor_id
and the agentcore config constants are intentionally duplicated. If you change the
write-seam or delete-chain logic, update it in BOTH files.

DB access rule: DbSessionRepository delegates 100% to the storage interface
(proxy.storage), imported LAZILY so the agentcore path keeps zero proxy.storage
dependency. The local sqlite path runs the agent from the repo .venv
(local_install.sh), where proxy/ is importable; the container image
(ai_assistant/Dockerfile) has no proxy/ and is meant for MEMORY_BACKEND=agentcore.
"""

import os
import copy
import queue
import atexit
import logging
import threading
from typing import Any, Callable, Optional

from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage

logger = logging.getLogger(__name__)

MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "sqlite").lower()


# ── Fire-and-forget persistence: ONE ordered background writer ───────────────────
# Strands' session hooks are SYNC and fire on the thread driving the agent — for
# chat_stream that is the event loop — so every per-message DB/AgentCore write used
# to block the loop. Writes are now queued to a single daemon thread that drains them
# in FIFO order:
#   - one thread ⇒ writes from this process never run concurrently (no SQLite writer
#     contention) and per-session order is exactly the submit order;
#   - payloads are snapshotted on the CALLER thread before submit, so later in-memory
#     mutation of the live message/agent can't race the write;
#   - a failing write is logged (op + session prefix + exception type, never the
#     payload) and never raised into the turn; the drain loop never dies;
#   - reads in this process call flush(session_id) first, so they always see every
#     prior write; atexit drains the queue on shutdown.
# Known limit: another PROCESS (the proxy's transcript reader) reads the DB directly,
# so a read issued within milliseconds of a turn may not yet see the last write
# (cross-process, eventually consistent).

class _PersistWriter:
    """Single-thread FIFO writer with per-session pending counters + flush."""

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._cond = threading.Condition()
        self._pending: dict[str, int] = {}
        self._total = 0
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                t = threading.Thread(target=self._run, name="chat-persist-writer", daemon=True)
                t.start()
                self._thread = t

    def _on_writer_thread(self) -> bool:
        t = self._thread
        return t is not None and threading.get_ident() == t.ident

    def submit(self, session_id: str, op_name: str, fn: Callable, *args: Any, **kwargs: Any) -> None:
        """Queue fn(*args, **kwargs) for the writer thread; returns immediately.
        Called FROM the writer thread (e.g. an SDK write that internally triggers
        another write) it runs inline — already in order, and avoids self-deadlock."""
        if self._on_writer_thread():
            self._execute(session_id, op_name, fn, args, kwargs)
            return
        sid = session_id or ""
        with self._cond:
            self._pending[sid] = self._pending.get(sid, 0) + 1
            self._total += 1
        self._ensure_started()
        self._q.put((sid, op_name, fn, args, kwargs))

    @staticmethod
    def _execute(session_id: str, op_name: str, fn: Callable, args: tuple, kwargs: dict) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — log-never-raise; no payload in the log
            logger.warning("chat persist %s failed (session=%s...): %s",
                           op_name, (session_id or "")[:8], type(e).__name__)

    def _run(self) -> None:
        while True:
            sid, op_name, fn, args, kwargs = self._q.get()
            try:
                self._execute(sid, op_name, fn, args, kwargs)
            except BaseException:  # noqa: BLE001 — the drain loop must never die
                pass
            finally:
                with self._cond:
                    n = self._pending.get(sid, 1) - 1
                    if n <= 0:
                        self._pending.pop(sid, None)
                    else:
                        self._pending[sid] = n
                    self._total = max(0, self._total - 1)
                    self._cond.notify_all()

    def flush(self, session_id: Optional[str] = None, timeout: float = 10.0) -> bool:
        """Block until that session's (or, with None, ALL) pending writes ran.
        Returns True when drained, False (with a warning) on timeout. A no-op on the
        writer thread itself (everything queued before it has already run)."""
        if self._on_writer_thread():
            return True
        if session_id is None:
            done = lambda: self._total == 0  # noqa: E731
        else:
            sid = session_id or ""
            done = lambda: self._pending.get(sid, 0) == 0  # noqa: E731
        with self._cond:
            ok = self._cond.wait_for(done, timeout=timeout)
        if not ok:
            logger.warning("chat persist flush timed out after %.1fs (session=%s)",
                           timeout, (session_id or "*")[:8])
        return ok


_WRITER = _PersistWriter()


def submit(session_id: str, op_name: str, fn: Callable, *args: Any, **kwargs: Any) -> None:
    """Fire-and-forget: queue a persistence call on the ordered writer thread."""
    _WRITER.submit(session_id, op_name, fn, *args, **kwargs)


def flush(session_id: Optional[str] = None, timeout: float = 10.0) -> bool:
    """Wait for pending writes of one session (or all, when session_id is None)."""
    return _WRITER.flush(session_id, timeout=timeout)


def flush_session(session_id: str, timeout: float = 10.0) -> bool:
    """Wait for every pending write of `session_id` (call before rehydrating it)."""
    return _WRITER.flush(session_id, timeout=timeout)


atexit.register(lambda: _WRITER.flush(timeout=10.0))


def _scrub_attachment_bytes(message_dict: dict) -> dict:
    """Remove heavy attachment bytes from a serialized SessionMessage before it's
    persisted / replayed.

    A chat turn with an image/document attachment is sent to the model as Converse
    `image`/`document` content blocks carrying raw bytes (see notebook_agent.
    _build_message_content). We must NOT store those bytes: they'd bloat the DB and,
    worse, be replayed into the sliding-window context on every following turn
    (re-billing tokens, risking size limits). Each attachment is ALSO preceded by a
    lightweight marker TEXT block (name/mime/s3_uri), so dropping the bytes block
    loses nothing durable — the marker remains and the transcript reader turns it
    back into a chip on reload.

    Operates on the to_dict() output (never the live SessionMessage the agent already
    consumed). Scrubs both `message` and `redact_message`. Best-effort + copy-safe:
    on any unexpected shape it returns the input unchanged (never raises, never drops
    a non-attachment block).
    """
    try:
        import copy
        out = copy.deepcopy(message_dict)
        for key in ("message", "redact_message"):
            inner = out.get(key)
            if not isinstance(inner, dict):
                continue
            content = inner.get("content")
            if not isinstance(content, list):
                continue
            # Keep every block EXCEPT image/document (the byte carriers). Text blocks
            # (user text, attachment markers, inlined JSON) all survive.
            inner["content"] = [
                b for b in content
                if not (isinstance(b, dict) and ("image" in b or "document" in b))
            ]
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"attachment scrub skipped (unexpected message shape): {e}")
        return message_dict

# --- AgentCore Memory config (only read when MEMORY_BACKEND=agentcore) ----------
# memory_id is the AgentCore Memory resource id (create it once via the AgentCore
# CLI / boto3 / console). actor_id should be stable per user/identity so long-term
# memory namespaces stay consistent (AWS best practice); locally we don't have an
# authenticated user, so it falls back to a stable per-notebook id, then the
# session_id. region falls back to AWS_REGION.
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
AGENTCORE_ACTOR_ID = os.environ.get("AGENTCORE_ACTOR_ID", "")  # optional explicit override
AGENTCORE_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-west-2"))


class DbSessionRepository(SessionRepository):
    """Strands SessionRepository backed by the proxy storage interface.

    One session holds one agent (our 1:1 model), but we honor the full
    (session_id, agent_id, message_id) contract so the SDK is satisfied and a
    future multi-agent-per-session would work without a schema change.

    Every method delegates to storage.agent_* (which is implemented by both the
    SQLite and Postgres backends) — no raw SQL here. The optional notebook_id is
    passed through so the rows are notebook-scoped for the sync worker / cascade.

    `proxy.storage` is imported LAZILY (in __init__) rather than at module load, so
    the agentcore backend — which never constructs this class — keeps zero
    proxy.storage dependency.
    """

    def __init__(self, notebook_id: Optional[str] = None):
        self._notebook_id = notebook_id
        # Lazy import: only the sqlite backend uses the DB repo, so the agentcore
        # path never pulls in proxy.storage.
        from proxy.storage import storage
        self._storage = storage

    # WRITE methods snapshot their payload on the caller thread (to_dict() / scrub)
    # and hand the storage call to the ordered background writer — they return
    # immediately. READ methods flush that session's pending writes first, so a read
    # in this process never misses a write it queued.

    # ---- Session ------------------------------------------------------------
    def create_session(self, session: Session, **kwargs: Any) -> Session:
        submit(session.session_id, "agent_session_create", self._storage.agent_session_create,
               session.session_id, session.to_dict(), notebook_id=self._notebook_id)
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Optional[Session]:
        flush(session_id)
        data = self._storage.agent_session_read(session_id)
        return Session.from_dict(data) if data else None

    # ---- Agent --------------------------------------------------------------
    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        submit(session_id, "agent_create", self._storage.agent_create,
               session_id, session_agent.agent_id, session_agent.to_dict())

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> Optional[SessionAgent]:
        flush(session_id)
        data = self._storage.agent_read(session_id, agent_id)
        return SessionAgent.from_dict(data) if data else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        submit(session_id, "agent_update", self._storage.agent_update,
               session_id, session_agent.agent_id, session_agent.to_dict())

    # ---- Messages -----------------------------------------------------------
    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage,
                        **kwargs: Any) -> None:
        submit(session_id, "agent_message_create", self._storage.agent_message_create,
               session_id, agent_id, session_message.message_id,
               _scrub_attachment_bytes(session_message.to_dict()),
               notebook_id=self._notebook_id)

    def read_message(self, session_id: str, agent_id: str, message_id: int,
                     **kwargs: Any) -> Optional[SessionMessage]:
        flush(session_id)
        data = self._storage.agent_message_read(session_id, agent_id, message_id)
        return SessionMessage.from_dict(data) if data else None

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage,
                       **kwargs: Any) -> None:
        submit(session_id, "agent_message_update", self._storage.agent_message_update,
               session_id, agent_id, session_message.message_id,
               _scrub_attachment_bytes(session_message.to_dict()))

    def list_messages(self, session_id: str, agent_id: str, limit: Optional[int] = None,
                      offset: int = 0, **kwargs: Any) -> list[SessionMessage]:
        flush(session_id)
        rows = self._storage.agent_message_list(session_id, agent_id, limit=limit, offset=offset)
        return [SessionMessage.from_dict(r) for r in rows]


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


class _AgentIdRef:
    """Minimal stand-in for the agent passed to the SDK's append_message on the writer
    thread — AgentCoreMemorySessionManager.append_message only reads agent.agent_id,
    and holding the live Agent there would race its in-flight turn."""
    __slots__ = ("agent_id",)

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


_NONBLOCKING_AGENTCORE_CLS = None
_NONBLOCKING_AGENTCORE_LOCK = threading.Lock()


def _nonblocking_agentcore_cls():
    """Lazily define (once) the AgentCore session manager subclass that defers its
    per-turn writes (append_message / update_agent) to the ordered writer thread and
    flushes before every read / initialize / redact. Lazy so the sqlite path never
    imports the AgentCore integration."""
    global _NONBLOCKING_AGENTCORE_CLS
    if _NONBLOCKING_AGENTCORE_CLS is not None:
        return _NONBLOCKING_AGENTCORE_CLS
    with _NONBLOCKING_AGENTCORE_LOCK:
        if _NONBLOCKING_AGENTCORE_CLS is not None:
            return _NONBLOCKING_AGENTCORE_CLS
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
        base = AgentCoreMemorySessionManager

        class _NonBlockingAgentCoreSessionManager(base):
            # ---- deferred writes (snapshot on the caller thread) -------------
            def append_message(self, message, agent, **kwargs: Any) -> None:
                # The SDK sets _latest_agent_message from create_message's eventId;
                # that now happens on the writer thread (redact flushes first).
                submit(self.session_id, "append_message", base.append_message,
                       self, copy.deepcopy(message), _AgentIdRef(agent.agent_id))

            def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
                submit(self.session_id, "update_agent", base.update_agent,
                       self, session_id, SessionAgent.from_dict(session_agent.to_dict()))

            # ---- reads / bookkeeping that depend on prior writes -------------
            def redact_latest_message(self, redact_message, agent, **kwargs: Any) -> None:
                flush(self.session_id)
                return base.redact_latest_message(self, redact_message, agent, **kwargs)

            def initialize(self, agent, **kwargs: Any) -> None:
                flush(self.session_id)
                return base.initialize(self, agent, **kwargs)

            def read_session(self, session_id: str, **kwargs: Any):
                flush(session_id)
                return base.read_session(self, session_id, **kwargs)

            def read_agent(self, session_id: str, agent_id: str, **kwargs: Any):
                flush(session_id)
                return base.read_agent(self, session_id, agent_id, **kwargs)

            def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any):
                flush(session_id)
                return base.read_message(self, session_id, agent_id, message_id, **kwargs)

            def list_messages(self, session_id: str, agent_id: str, *args: Any, **kwargs: Any):
                flush(session_id)
                return base.list_messages(self, session_id, agent_id, *args, **kwargs)

        _NONBLOCKING_AGENTCORE_CLS = _NonBlockingAgentCoreSessionManager
        return _NONBLOCKING_AGENTCORE_CLS


def make_session_manager(session_id: str, notebook_id: Optional[str] = None):
    """Return the SessionManager for the chat agent, per MEMORY_BACKEND.

    - sqlite:    RepositorySessionManager over DbSessionRepository (auto-persist +
                 cold-start rehydration through the storage interface).
    - agentcore: AgentCoreMemorySessionManager over Amazon Bedrock AgentCore Memory.

    Both are RepositorySessionManager subclasses driven through
    Agent(session_manager=...), so the agent construction is identical — only which
    manager this factory returns changes.
    """
    if MEMORY_BACKEND == "agentcore":
        if not AGENTCORE_MEMORY_ID:
            raise RuntimeError(
                "MEMORY_BACKEND=agentcore requires AGENTCORE_MEMORY_ID (the AgentCore "
                "Memory resource id). Set it in scripts/config.sh."
            )
        # Lazy import so the sqlite path (and environments without the memory
        # integration deps) never load it.
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

        actor_id = _resolve_actor_id(session_id)
        config = AgentCoreMemoryConfig(
            memory_id=AGENTCORE_MEMORY_ID,
            session_id=session_id,   # our notebook's stable session_id (1:1 model)
            actor_id=actor_id,       # stable per user/notebook identity
            # async_mode=False: with async_mode=True the SDK registers async-only
            # hooks, and any SYNC agent(...) call then raises RuntimeError from
            # Strands' hook registry. Every turn now goes through chat_stream()
            # (stream_async; /ai/chat/sync aggregates it too), so async_mode=True
            # would only offload the per-turn boto3 writes off the event loop. Those
            # writes are already deferred to the ordered background writer by
            # _NonBlockingAgentCoreSessionManager (its hooks stay sync), so the
            # setting is kept as is.
            async_mode=False,
        )
        logger.info(
            "AgentCore Memory session manager: memory_id=%s session=%s actor=%s region=%s",
            AGENTCORE_MEMORY_ID, session_id, actor_id, AGENTCORE_REGION,
        )
        return _nonblocking_agentcore_cls()(config, region_name=AGENTCORE_REGION)

    return RepositorySessionManager(
        session_id=session_id,
        session_repository=DbSessionRepository(notebook_id=notebook_id),
    )


# ── DELETE seam: purge a session's conversation (new-chat reset) ─────────────────
# "New conversation" is a RESET: notebook_agent.new_thread clears the in-memory
# thread AND the durable store. For sqlite/postgres the durable clear is the DB
# cascade (storage.agent_session_delete, called in new_thread), so delete_transcript
# below is a NO-OP there. For the agentcore backend the conversation lives in the
# managed service, so we must explicitly delete its events — AgentCore has no
# session-scoped bulk delete, so we list the session's events and delete each by
# eventId. Same MEMORY_BACKEND dispatch as the write seam above.

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


def _agentcore_delete_transcript(session_id: str) -> int:
    """Delete ALL events for (memory, actor, session) from AgentCore Memory. Returns
    the number of events deleted. Best-effort: logs and returns the count so far on
    any error (a failed purge must not block the reset). No-op (0) if the memory id
    is unset / boto3 missing."""
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
      delete).
    - sqlite/postgres: NO-OP. The conversation lives in the DB and new_thread's
      storage.agent_session_delete already removes agent_sessions (→ agent_messages
      cascade). We deliberately do NOT double-delete here to keep a single,
      well-understood cascade path.

    SYNCHRONOUS (blocking). Async callers should offload via asyncio.to_thread."""
    if MEMORY_BACKEND == "agentcore":
        _agentcore_delete_transcript(session_id)
    # sqlite/postgres: handled by the DB cascade — nothing to do.
