"""
Conversation -> UI-bubble transcript translation (shared by ALL memory backends).

The chat agent's conversation is persisted by whichever memory backend is active:
  - SQLite / Postgres: Strands' native SessionMessage format (Bedrock shape — role
    + content blocks, including toolUse / toolResult entries).
  - AgentCore Memory: the managed service's event format (list_events → per-event
    `payload[].conversational = {content:{text}, role: USER|ASSISTANT|TOOL|OTHER}`).

The frontend chat panel renders a much simpler transcript in BOTH cases: a flat
list of user/assistant bubbles whose `content` is plain (markdown) text — exactly
what the live stream accumulates (only text is shown; tool steps are transient and
never become bubbles).

Design (so both backends produce the IDENTICAL UI shape):
  - Each backend has a thin ADAPTER that normalizes its stored form into an ordered
    stream of (role, text) turns, where role ∈ {"user","assistant","tool"} and text
    is plain text (tool/other content already reduced to "" or role "tool").
      • messages_to_bubbles(session_messages)  — Strands SessionMessage dicts
      • events_to_bubbles(events)              — AgentCore list_events events
  - Both hand that stream to the SAME assembler, _assemble_bubbles(), which owns the
    shared rules:
      • a user turn's text starts a user bubble — but first the per-turn context
        preamble the agent prepends (data inventory / existing vars / vm id) is
        STRIPPED so the bubble shows only the user's actual question;
      • assistant turns are merged into ONE bubble per user→assistant span (an
        assistant "turn" may span several messages/events across tool round-trips),
        joined with a blank line so interstitial narration and the final answer
        (with its code cells) stay intact and don't run together;
      • tool turns (toolUse/toolResult, or AgentCore role TOOL/OTHER) are DROPPED;
      • empty turns contribute nothing.

Keeping the rules in one shared assembler guarantees SQLite, Postgres, and
AgentCore all return the same bubbles, and keeps each adapter a pure format shim.
"""

from typing import Any, Iterable

# Attachment marker parsing (single source of truth in the ai package). Importing
# this constants module is safe here: it has no heavy/proxy deps (pure constants +
# two small string helpers), so no import cycle with proxy.storage.
try:
    from proxy.notebook.ai.constants import parse_attachment_marker, ATTACHMENT_MARKER_PREFIX
except Exception:  # pragma: no cover - defensive: transcript must still work if import fails
    ATTACHMENT_MARKER_PREFIX = "\u27e6attachment\u27e7"

    def parse_attachment_marker(text):  # type: ignore
        return None


# ── Shared: strip the per-turn context preamble off a user message ──────────────
# notebook_agent._prepend_available_data() prepends these blocks to the user's
# message before handing it to the agent. The agent (correctly) needs the enriched
# prompt, so it is persisted verbatim as the user turn — by BOTH backends. On read
# we strip any LEADING run of these blocks so the bubble shows only the question.
#
# KEEP IN SYNC with the block headers built in agent/ai_assistant/notebook_agent.py
# (_build_available_data_block / _build_existing_vars_block /
# _build_session_attachments_block / _build_vm_context_block). The agent and proxy
# are separate services, so this can't import them; tests/storage/test_transcript.py
# builds a preamble with the REAL agent builders and fails if a header here drifts.
# A renamed header leaves the old prefix in place so transcripts persisted before the
# rename still strip.
_INJECTED_BLOCK_PREFIXES = (
    "[Currently available data",
    "[Variables already defined in the notebook",   # current header (since eb0c503)
    "[Existing variables in the notebook",          # pre-eb0c503 transcripts
    "[Files attached in this conversation",
    "[Current VM for this turn",
)


def _strip_injected_context(text: str) -> str:
    """Return just the user's question by removing the leading per-turn context
    blocks (data inventory / existing vars / vm id) the agent prepended. Drops only
    LEADING blocks whose first line starts with a known injected marker, splitting
    on the blank-line ("\\n\\n") separator, then returns the remainder. If nothing
    matches, the text is returned unchanged (a message that merely contains a
    bracket later is untouched)."""
    if not text:
        return text
    remainder = text
    while True:
        stripped = remainder.lstrip()
        if not any(stripped.startswith(p) for p in _INJECTED_BLOCK_PREFIXES):
            break
        idx = stripped.find("\n\n")
        if idx == -1:
            # Whole remaining text is one injected block, no user text after it.
            return ""
        remainder = stripped[idx + 2:]
    return remainder.strip()


# ── Shared: assemble normalized (role, text) turns into UI bubbles ──────────────
def _assemble_bubbles(turns: Iterable[tuple]) -> list[dict]:
    """Turn an ordered stream of (role, text[, attachments]) into UI chat bubbles:
        [{"role": "user"|"assistant", "content": "<text>", "attachments"?: [...]}]

    role is one of "user" | "assistant" | "tool". Rules (mirror the live UI):
      - "tool" turns are dropped (never shown).
      - a "user" turn with real text OR attachments starts a user bubble (after
        stripping the injected preamble) and flushes any in-progress assistant
        bubble. attachments (parsed from ⟦attachment⟧ markers) ride the user bubble
        as an optional structured list [{name, mime, s3_uri}] for chip rendering.
      - "assistant" turns accumulate into the current assistant bubble; segments
        from distinct messages/events are joined with a blank line.
      - empty turns contribute nothing.

    Turns may be 2-tuples (role, text) or 3-tuples (role, text, attachments); the
    3rd element defaults to [] so older adapters stay compatible.
    """
    bubbles: list[dict] = []
    assistant_buf: list[str] = []

    def _flush_assistant():
        if assistant_buf:
            text = "\n\n".join(t.strip() for t in assistant_buf if t.strip()).strip()
            if text:
                bubbles.append({"role": "assistant", "content": text})
            assistant_buf.clear()

    for turn in turns:
        role, text = turn[0], turn[1]
        attachments = turn[2] if len(turn) > 2 else []
        if role == "tool":
            continue
        if role == "user":
            text = _strip_injected_context((text or "").strip())
            # A user turn is shown if it has real text OR any attachments (an
            # attachment-only turn — "explain this image" with no typed text —
            # still becomes a bubble so the chip renders).
            if not text and not attachments:
                continue
            _flush_assistant()
            bubble = {"role": "user", "content": text}
            if attachments:
                bubble["attachments"] = attachments
            bubbles.append(bubble)
        elif role == "assistant":
            if text:
                assistant_buf.append(text)
        # any other role: ignored

    _flush_assistant()
    return bubbles


# ── Adapter A: Strands SessionMessage dicts (SQLite / Postgres) ─────────────────
def _strands_text(message: dict) -> str:
    """Concatenate the plain-text content blocks of a Bedrock-shaped message.

    Serialized Strands SessionMessage shape:
        {"message": {"role": "user"|"assistant",
                     "content": [{"text": "..."} | {"toolUse": {...}} | {"toolResult": {...}}]},
         "message_id": 0, "redact_message": <msg>|None, ...}
    Honors redaction; keeps only {"text": ...} blocks (toolUse/toolResult dropped).
    `content` may also be a bare string.
    """
    inner = message.get("redact_message") or message.get("message") or {}
    content = inner.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            t = block["text"]
            # Attachment marker text blocks are surfaced structurally (see
            # _strands_attachments) — keep them OUT of the displayed content.
            if t.startswith(ATTACHMENT_MARKER_PREFIX):
                continue
            parts.append(t)
        elif isinstance(block, str):
            parts.append(block)
        # toolUse / toolResult / other blocks: intentionally dropped
    return "".join(parts)


def _strands_attachments(message: dict) -> list[dict]:
    """Extract structured attachments from a message's marker text blocks.

    Returns [{name, mime, s3_uri}, ...] parsed from the ⟦attachment⟧ markers the
    persist scrub left in place of the (dropped) image/document byte blocks. Empty
    list for messages with no attachments (the common case)."""
    inner = message.get("redact_message") or message.get("message") or {}
    content = inner.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parsed = parse_attachment_marker(block["text"])
            if parsed:
                out.append(parsed)
    return out


def _strands_role(message: dict) -> str:
    inner = message.get("redact_message") or message.get("message") or {}
    return inner.get("role", "")


def _strands_is_tool_result_only(message: dict) -> bool:
    """True for a user-role message whose content is ONLY toolResult blocks (the
    tool round-trip carrier) — it must be treated as a "tool" turn, not a user
    bubble (it belongs to the in-progress assistant turn)."""
    if _strands_role(message) != "user":
        return False
    inner = message.get("redact_message") or message.get("message") or {}
    content = inner.get("content")
    if not isinstance(content, list):
        return False
    has_text = any(isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip()
                   for b in content)
    has_tool_result = any(isinstance(b, dict) and "toolResult" in b for b in content)
    return has_tool_result and not has_text


def _strands_turns(session_messages: list[dict[str, Any]]):
    """Normalize Strands SessionMessage dicts → (role, text, attachments) turns."""
    for sm in session_messages:
        role = _strands_role(sm)
        if role == "user":
            # A toolResult-only user message is a tool round-trip carrier, not a
            # user turn — mark it "tool" so the assembler drops it.
            if _strands_is_tool_result_only(sm):
                yield ("tool", "", [])
            else:
                yield ("user", _strands_text(sm), _strands_attachments(sm))
        elif role == "assistant":
            yield ("assistant", _strands_text(sm), [])
        else:
            # system / other: ignore (assembler drops unknown roles anyway)
            yield ("tool", "", [])


def messages_to_bubbles(session_messages: list[dict[str, Any]]) -> list[dict]:
    """Translate ordered Strands SessionMessage dicts (SQLite/Postgres) into UI
    chat bubbles. Thin adapter over the shared assembler."""
    return _assemble_bubbles(_strands_turns(session_messages))


# ── Adapter B: AgentCore Memory list_events events ──────────────────────────────
# IMPORTANT (verified against a live AgentCore Memory resource): the Strands
# AgentCoreMemorySessionManager stores each turn as an event whose
# `payload[].conversational.content.text` is the FULL serialized Strands
# SessionMessage JSON (the SAME dict SQLite stores in agent_messages.message_json) —
# NOT plain text. So the correct adapter is: pull each event's text, json.loads it
# back into a SessionMessage dict, and reuse the Strands translator. This guarantees
# byte-for-byte identical bubbles to the SQLite path (same messages, same rules:
# preamble strip, tool-carrier drop, \n\n assistant merge).
#
# Also verified: list_events returns events NEWEST-FIRST, so we reverse to
# chronological order before translating.
import json as _json


def _agentcore_event_text(event: dict) -> str | None:
    """Return the conversational text of an AgentCore event, or None if the event
    carries no conversational payload (blob/json/SDK_UNKNOWN are ignored)."""
    for payload in event.get("payload") or []:
        if not isinstance(payload, dict):
            continue
        conv = payload.get("conversational")
        if isinstance(conv, dict):
            content = conv.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return content["text"]
    return None


def events_to_bubbles(events: list[dict[str, Any]]) -> list[dict]:
    """Translate AgentCore Memory list_events events into UI chat bubbles.

    Reconstructs the Strands SessionMessage list from the events (each event's
    conversational text IS a serialized SessionMessage), in chronological order,
    then reuses messages_to_bubbles — so the output is identical to the SQLite path.
    Events whose text isn't a valid SessionMessage JSON are skipped defensively."""
    # list_events returns newest-first; the transcript needs oldest-first.
    ordered = list(reversed(events or []))
    session_messages: list[dict] = []
    for ev in ordered:
        text = _agentcore_event_text(ev)
        if not text:
            continue
        try:
            msg = _json.loads(text)
        except (ValueError, TypeError):
            # Not a serialized SessionMessage (e.g. a raw text-only write) — wrap it
            # as a minimal user/assistant message so it still renders. We can't know
            # the role from a bare string, so skip rather than guess.
            continue
        if isinstance(msg, dict) and "message" in msg:
            session_messages.append(msg)
    return messages_to_bubbles(session_messages)
