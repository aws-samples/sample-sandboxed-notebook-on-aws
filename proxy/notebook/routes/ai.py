"""
AI routes — chat forwarding, attachments, model catalog, one-shot helpers.

Part of: proxy.notebook (Notebook application layer)

Endpoints:
  GET    /ai/config                    - AI availability, model info, chat context window size
  GET    /ai/models                    - Model catalog for the chat model picker
  POST   /ai/attachments               - Upload a chat attachment to S3
  POST   /ai/chat                      - Conversational chat (SSE streaming, forwarded to the agent)
  POST   /ai/chat/resume               - Resume a chat turn paused on an approval
  POST   /ai/chat/sync                 - Conversational chat (JSON response)
  DELETE /ai/chat/{session_id}         - Clear conversation history
  GET    /ai/chat/{session_id}/messages - Saved conversation as UI chat bubbles
  POST   /ai/annotate                  - Markdown annotations for a notebook
  POST   /ai/fix                       - Fix cell error
  POST   /ai/suggest-tag               - Suggest notebook category tag
  POST   /ai/explain-lineage           - 1–2 sentence "How was this computed?" summary
  POST   /ai/explain-cell              - Structured explanation of one cell
  POST   /terminal/suggest             - Shell command from a description

Error bodies are {"error": "<message>"} (json_error); /ai/explain-cell returns
{"error": "<code>", "message": "<text>"}.
"""

import os
import json
import time
import asyncio
import hashlib
import logging
import threading
from collections import OrderedDict
from pathlib import Path

import boto3
from fastapi import APIRouter, Request
from proxy.platform.errors import json_error
from starlette.responses import StreamingResponse

from proxy.notebook.ai.constants import (
    AI_MODEL_ID, AI_REGION, LINEAGE_MODEL_ID, require_model_id,
    TAG_TEMPERATURE, TAG_MAX_TOKENS, TAG_MAX_LENGTH, MAX_CELLS_FOR_TAG,
    BEDROCK_MAX_RETRIES, BEDROCK_READ_TIMEOUT, BEDROCK_CONNECT_TIMEOUT,
    ATTACHMENT_IMAGE_FORMATS, ATTACHMENT_DOCUMENT_FORMATS, ATTACHMENT_TEXT_MIMES,
    ATTACHMENT_EXT_MIME, ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_PER_MESSAGE,
    ATTACHMENT_S3_PREFIX,
    LINEAGE_TEMPERATURE, LINEAGE_MAX_TOKENS, LINEAGE_CACHE_MAX, LINEAGE_MAX_STEPS,
    LINEAGE_MAX_CODE_CHARS, LINEAGE_MAX_COLUMNS, LINEAGE_MAX_SOURCES,
    LINEAGE_SUMMARY_CHARS, LINEAGE_PROMPT_VERSION,
    AGENT_CONVERSATION_WINDOW_SIZE,
)
from modelcaps import (
    build_inference_config, build_additional_request_fields,
    top_models_for_agent, supports_vision, supports_document,
)
# The CHAT agent runs as its own local service (agent/ai_assistant/agent_runtime.py,
# BedrockAgentCoreApp on $AI_PORT). The chat routes below build the context and
# FORWARD to it over HTTP. The one-shot helpers (annotate/fix/suggest-shell) are
# not agents and stay in the proxy, so those imports remain.
import httpx

from proxy.notebook.ai.notebook_agent import (
    annotate_notebook as agent_annotate,
    fix_error as agent_fix_error,
    fix_error_agentic as agent_fix_error_agentic,
    suggest_shell_command as agent_suggest_shell,
)
from proxy.platform.microvm_manager import AWS_REGION
from proxy.platform import aws_clients, http_clients
from proxy.storage import astorage

logger = logging.getLogger(__name__)

# Artifact bucket for chat attachments (same bucket the intel/datasource paths use).
ARTIFACT_BUCKET = os.environ.get(
    "ARTIFACT_BUCKET",
    f"microvm-sandbox-artifacts-{os.environ.get('ACCOUNT_ID', 'unknown')}-{AWS_REGION}",
)


def _classify_attachment(name: str, mime: str) -> tuple[str, str, str]:
    """Resolve (kind, converse_format, normalized_mime) for an attachment.

    kind ∈ {"image","document","text"}:
      * image    ⇒ Converse IMAGE block (needs the model's vision capability).
      * document ⇒ Converse DOCUMENT block, BINARY documents only: pdf/doc/docx/
                   xls/xlsx (needs the model's document_support capability).
      * text     ⇒ inline the decoded UTF-8 as a {"text": ...} block. csv/txt/json
                   are plain text — ANY model can read them inlined, so text needs
                   NO model capability. (This is why csv/txt moved out of
                   ATTACHMENT_DOCUMENT_FORMATS and into ATTACHMENT_TEXT_MIMES.)

    Falls back to the file extension when the browser mime is missing or generic
    (application/octet-stream). Raises ValueError for unsupported types so the
    caller returns a clean 400.
    """
    mime = (mime or "").split(";", 1)[0].strip().lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mime in ("", "application/octet-stream") and ext in ATTACHMENT_EXT_MIME:
        mime = ATTACHMENT_EXT_MIME[ext]
    if mime in ATTACHMENT_IMAGE_FORMATS:
        return "image", ATTACHMENT_IMAGE_FORMATS[mime], mime
    if mime in ATTACHMENT_DOCUMENT_FORMATS:
        return "document", ATTACHMENT_DOCUMENT_FORMATS[mime], mime
    if mime in ATTACHMENT_TEXT_MIMES:
        return "text", "txt", mime
    raise ValueError(
        f"Unsupported attachment type: name={name!r} mime={mime!r}. "
        "Allowed: images (png/jpeg/gif/webp), binary documents "
        "(pdf/doc/docx/xls/xlsx), and text (json/csv/txt)."
    )

# The extracted chat agent's endpoint (AgentCore /invocations contract).
# AGENT_URL is the single source of truth for WHERE the agent lives and is env-driven
# (set in scripts/config.sh) so topology stays out of code: locally it's the
# BedrockAgentCoreApp on 127.0.0.1:$AI_PORT; at cloud cutover point it at the AWS
# Bedrock AgentCore Runtime endpoint by changing only config.sh. Falls back to the
# local port so a bare `AI_PORT` still works without AGENT_URL set. 8082 is the
# local port scripts/config.sh exports; agent_runtime.py itself binds 8080 when
# AI_PORT is unset (the AgentCore Runtime contract), which only applies in the cloud.
AI_PORT = os.environ.get("AI_PORT", "8082")
AGENT_URL = os.environ.get("AGENT_URL", f"http://localhost:{AI_PORT}").rstrip("/")

_bedrock_client = None

router = APIRouter(tags=["ai"])


async def _stream_agent_events(payload: dict):
    """POST an AgentCore payload to the chat agent's /invocations and yield the
    parsed event dicts from its SSE stream.

    The agent emits `data: <json>\\n\\n` frames (same shape the proxy sends the
    browser). We parse each frame back into a dict and re-yield it, so callers can
    re-serialize for the browser (streaming) or aggregate (sync). If the agent is
    unreachable, yield a single error event instead of raising — the proxy stays up.
    """
    try:
        # Shared aux client (connection reuse to the local agent); the timeout is
        # per request. POST stream → only connect-phase transport retries.
        async with http_clients.aux_client().stream(
                "POST", f"{AGENT_URL}/invocations", json=payload,
                timeout=httpx.Timeout(300.0, connect=5.0)) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                yield {"type": "error", "content": f"agent HTTP {resp.status_code}: {body}"}
                return
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    # Non-JSON frame — pass through as text so nothing is lost.
                    yield {"type": "text", "content": data}
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.error(f"AI chat agent unreachable at {AGENT_URL} — is it running (AI_PORT={AI_PORT})?")
        yield {"type": "error", "content": "AI agent is not reachable. Ensure the agent service is running."}
    except Exception as e:
        import traceback
        logger.error(f"AI chat forward error: {e}\n{traceback.format_exc()}")
        yield {"type": "error", "content": str(e)}


def _get_bedrock_client():
    """Get a Bedrock Runtime client for lightweight calls (tag suggestion)."""
    global _bedrock_client
    if _bedrock_client is None:
        from botocore.config import Config
        bedrock_config = Config(
            retries={'max_attempts': BEDROCK_MAX_RETRIES, 'mode': 'standard'},
            read_timeout=BEDROCK_READ_TIMEOUT,
            connect_timeout=BEDROCK_CONNECT_TIMEOUT,
        )
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=AI_REGION,
            config=bedrock_config,
        )
    return _bedrock_client


# /ai/config credential-availability cache: resolving the boto3 credential chain
# can block (IMDS / container-credentials / SSO file I/O), so it runs off-loop and
# the boolean is cached for _AI_AVAILABLE_TTL_SECONDS.
_AI_AVAILABLE_TTL_SECONDS = 60.0
_AI_AVAILABLE_CACHE: dict = {"value": None, "ts": 0.0}


def _ai_credentials_available() -> bool:
    """SYNC: True when the boto3 credential chain yields an access key. Never
    raises. Run via asyncio.to_thread."""
    ai_available = False
    try:
        session = boto3.Session(region_name=AI_REGION)
        credentials = session.get_credentials()
        if credentials and credentials.get_frozen_credentials().access_key:
            ai_available = True
    except Exception:
        pass
    return ai_available


@router.get("/ai/config")
async def ai_config():
    """Return AI configuration and availability."""
    now = time.monotonic()
    cached = _AI_AVAILABLE_CACHE["value"]
    if cached is not None and now - _AI_AVAILABLE_CACHE["ts"] < _AI_AVAILABLE_TTL_SECONDS:
        ai_available = cached
    else:
        ai_available = await asyncio.to_thread(_ai_credentials_available)
        _AI_AVAILABLE_CACHE["value"] = ai_available
        _AI_AVAILABLE_CACHE["ts"] = time.monotonic()
    # agent_conversation_window_size: the chat agent's sliding window in MESSAGES (same
    # env var + default as the agent's constant). The web shows the last
    # window // 2 turns as "in the AI's context". Config only; no per-turn data.
    return {"model_id": AI_MODEL_ID, "region": AI_REGION, "ai_available": ai_available,
            "agent_conversation_window_size": AGENT_CONVERSATION_WINDOW_SIZE}


# Canonical catalog location. list_models() reads the catalog shipped NEXT TO the
# modelcaps package (modelcaps/model_caps.json) as its single source, so in the proxy
# — which imports the repo-root canonical `modelcaps` — the primary path is always
# available. This explicit repo-root path is a belt-and-suspenders fallback: if
# list_models() ever reports unavailable, read the same canonical catalog and shape it
# through the SAME list_models() (one code path, one schema — never re-parsed ad hoc).
_CANONICAL_CATALOG_PATH = Path(__file__).resolve().parents[3] / "modelcaps" / "model_caps.json"


def _load_canonical_models() -> dict | None:
    """Load the ``models`` map from the canonical modelcaps/model_caps.json, or None.

    Never raises: returns None when the file is absent/unreadable/invalid so the
    endpoint degrades gracefully to the no-catalog (flagged-empty) result.
    """
    try:
        with _CANONICAL_CATALOG_PATH.open() as fh:
            data = json.load(fh)
        models = data.get("models")
        return models if isinstance(models, dict) else None
    except (OSError, ValueError):
        return None


@router.get("/ai/models")
async def ai_models():
    """Return the curated top-N Bedrock models for the UI model-picker dropdown.

    Read-only and catalog-driven: every field comes from the generated model
    capability catalog (modelcaps/model_caps.json, produced by modelcaps/refresh.py)
    via modelcaps.top_models_for_agent() — no model list is hardcoded. Rather than the
    full by_provider catalog, this returns a region-scoped, ranked, dropdown-ready
    shortlist. Shape:
      {available, count, region, default_id, models: [ {id, resolved_id,
       display_name, description, provider, context_tokens, capabilities,
       pricing, cost_multiplier, cost_label, tier, is_default, score} ... ]}
    where ``id`` is the base id the UI sends back as model_id and ``resolved_id`` is
    the region-resolved invocable id the agent uses.

    If the catalog is absent the endpoint degrades gracefully to a clearly-flagged
    empty result (``available: false``, empty models) and never raises/500.
    """
    # Region + default come from proxy/notebook/ai/constants.py (AI_REGION =
    # BEDROCK_REGION or AWS_REGION, AI_MODEL_ID = BEDROCK_MODEL_ID).
    region = AI_REGION
    default_id = AI_MODEL_ID
    # Operator denylist (e.g. deprecated models) from BEDROCK_EXCLUDE_MODELS —
    # comma-separated ids, normalized downstream. Excluded models never appear in the
    # dropdown. Empty/unset -> exclude nothing.
    exclude = [x.strip() for x in os.environ.get("BEDROCK_EXCLUDE_MODELS", "").split(",") if x.strip()]
    # Prefer the catalog shipped next to the modelcaps package (the helper's own
    # single source). As a belt-and-suspenders fallback, read the canonical repo-root
    # catalog and shape it THROUGH the SAME top_models_for_agent() so there is one
    # code path and one schema.
    result = top_models_for_agent(n=12, region=region, default_id=default_id, exclude=exclude)
    if not result.get("available"):
        canonical = _load_canonical_models()
        if canonical is not None:
            result = top_models_for_agent(n=12, region=region, default_id=default_id,
                                          exclude=exclude, catalog=canonical)
    return result


@router.post("/ai/attachments")
async def ai_upload_attachment(request: Request):
    """Upload a chat attachment to S3 and return its durable reference.

    Option A (proxy-brokered upload): the browser sends base64 bytes; the proxy
    (which holds the IAM role — the browser never gets AWS credentials) validates
    type/size and PutObjects to s3://<artifact-bucket>/attachments/<session>/<uuid>/<name>.
    Returns {s3_uri, name, mime, kind, format, size} which the client then attaches
    to a /ai/chat turn. Bytes are NOT persisted in chat history — only this S3 URI
    + metadata are (see memory.py scrub); the proxy re-fetches the bytes from S3 to
    build the model's Converse block for the turn the attachment is sent.
    """
    import base64
    import uuid as _uuid

    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    name = (body.get("name") or "").strip() or "attachment"
    mime = body.get("mime") or ""
    data_b64 = body.get("data") or ""
    # The client sends the currently-selected model so the proxy can enforce the
    # SAME capability gate the web UI applies (defense-in-depth). An older client
    # that doesn't send it degrades to the configured default (AI_MODEL_ID) so the
    # check always runs against SOME model rather than being skipped — see the gate
    # block below where this fallback is applied.
    model_id = (body.get("model_id") or "").strip()

    if not session_id:
        return json_error(400, "session_id required")
    if not data_b64:
        return json_error(400, "no file data provided")

    # Decode + size-check (server-side re-validation; the client also checks).
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return json_error(400, "invalid base64 file data")
    if len(raw) == 0:
        return json_error(400, "empty file")
    if len(raw) > ATTACHMENT_MAX_BYTES:
        return json_error(413, f"file too large ({len(raw)} bytes); max {ATTACHMENT_MAX_BYTES}")

    # Type allowlist.
    try:
        kind, fmt, norm_mime = _classify_attachment(name, mime)
    except ValueError as e:
        return json_error(400, str(e))

    # Capability backstop — the SERVER-SIDE pair of the web UI's attach gate
    # (defense-in-depth): the browser already filters by the selected model's caps
    # (accept= + the JS extension check), but drag-drop/paste bypass accept= and a
    # stale/forged client could still POST a mismatched file — so we re-check here
    # authoritatively. Runs BEFORE the S3 PutObject below so a rejected file is
    # NEVER written to S3 (no orphan objects). An image needs vision; a binary
    # document (pdf/doc/docx/xls/xlsx) needs document_support; kind=="text"
    # (csv/txt/json) is plain text that any model inlines, so it is ALWAYS allowed
    # with no capability check. When model_id is absent (older client), fall back to
    # the configured default (BEDROCK_MODEL_ID) so the check still runs against
    # some model.
    if not model_id:
        model_id = AI_MODEL_ID
    if kind == "image" and not supports_vision(model_id):
        return json_error(400, f"The selected model {model_id} can't read images. Switch to a vision-capable model or remove the image.")
    if kind == "document" and not supports_document(model_id):
        return json_error(400, f"The selected model {model_id} can't read PDF/Office documents. Switch to a document-capable model, or attach a CSV/text file / image instead.")

    # Upload under a per-session, per-attachment key. Sanitize the filename to a
    # safe leaf (defense against path traversal / odd characters in the key).
    safe_name = os.path.basename(name).replace("/", "_").replace("\\", "_") or "attachment"
    key = f"{ATTACHMENT_S3_PREFIX}/{session_id}/{_uuid.uuid4().hex}/{safe_name}"

    def _put():
        s3 = aws_clients.get_client("s3", region_name=AWS_REGION)
        s3.put_object(Bucket=ARTIFACT_BUCKET, Key=key, Body=raw, ContentType=norm_mime)

    try:
        await asyncio.to_thread(_put)
    except Exception as e:
        logger.error(f"attachment upload failed for session {session_id[:8]}...: {e}")
        return json_error(502, f"S3 upload failed: {e}")

    s3_uri = f"s3://{ARTIFACT_BUCKET}/{key}"
    logger.info(f"attachment uploaded: session={session_id[:8]}... kind={kind} fmt={fmt} "
                f"size={len(raw)} → {s3_uri}")
    return {"s3_uri": s3_uri, "name": safe_name, "mime": norm_mime,
            "kind": kind, "format": fmt, "size": len(raw)}


async def _build_chat_context(body: dict, vm_manager, session_id: str) -> dict:
    """Assemble the per-turn context dict for the chat agent from the request body.

    Shared by /ai/chat, /ai/chat/sync, and /ai/chat/resume so all three stay in
    lock-step (one field list, no drift). vm_id + memory_mib are resolved ONCE from
    vm_manager here. Callers set context["approval_channel"] themselves (True for the
    interactive streaming + resume paths, False for the sync endpoint).
    """
    # Async lookup: the DB-authoritative session→VM read runs off the event loop.
    session_vm = await vm_manager.get_session_vm_async(session_id)
    vm_id = (session_vm or {}).get("vm_id", "")
    memory_mib = vm_manager.active_microvms.get(vm_id, {}).get("memory_mib") if vm_id else None
    return {
        "proxy_url": f"http://localhost:{os.environ.get('PROXY_PORT', '8081')}",
        "session_id": session_id,
        # notebook_id (stable) → baked into the agent's system prompt so it can
        # scope v_nb_* metadata queries to THIS notebook. vm_id (mutable) → rides
        # the per-turn message so it's always current.
        "notebook_id": body.get("notebook_id", ""),
        "vm_id": vm_id,
        # User-selected model (base id from the dropdown) or None to fall back to the
        # agent's configured default. Shared by /ai/chat, /ai/chat/sync AND
        # /ai/chat/resume — so a resumed turn keeps the chosen model too. The agent
        # normalizes this through resolve_model_id and switches at a turn boundary.
        "model_id": body.get("model_id") or None,
        "notebook_cells": body.get("cells", []),
        "memory_mib": memory_mib,
        "data_sources": body.get("data_sources"),
        "packages": body.get("packages", []),
        "variables": body.get("variables", []),
        "uploaded_files": body.get("uploaded_files", []),
        # Chat attachments (image/document refs uploaded via /ai/attachments). Each:
        # {s3_uri, name, mime, kind, format, size}. The agent turns these into
        # Converse content blocks (bytes re-fetched from S3) for THIS turn only.
        "attachments": body.get("attachments") or [],
        # Every attachment seen so far in this conversation (this turn + prior turns),
        # deduped by s3_uri. Surfaced to the model so a follow-up ("add the attached
        # file", "promote it") can act on a PRIOR-turn attachment by URI — the bytes
        # are scrubbed from history, so this list is how the model "remembers" them.
        "session_attachments": body.get("session_attachments") or [],
    }


@router.post("/ai/chat")
async def ai_chat(request: Request):
    """Conversational chat with the notebook AI agent (SSE streaming)."""
    vm_manager = request.app.state.vm_manager
    body = await request.json()
    session_id = body.get("session_id", "")
    message = body.get("message", "").strip()
    attachments = body.get("attachments") or []

    if not message:
        return json_error(400, "No message provided")
    if not session_id:
        return json_error(400, "No session_id provided")
    if len(attachments) > ATTACHMENT_MAX_PER_MESSAGE:
        return json_error(400, f"too many attachments (max {ATTACHMENT_MAX_PER_MESSAGE})")

    context = await _build_chat_context(body, vm_manager, session_id)
    # Interactive streaming path → the confirm-before-execute gate is ON.
    context["approval_channel"] = True

    # Log KEY INFO only — session, message LENGTH, attachment count. The message
    # text is user data (their question) and must not be echoed to logs. model_id is
    # the REQUESTED model from the dropdown ('default' when the UI didn't send one);
    # the agent log reports the RESOLVED invocable id it actually ran.
    logger.info(f"AI chat: session={session_id[:8]}... msg_len={len(message or '')} "
                f"attachments={len(attachments)} "
                f"model_id={body.get('model_id') or 'default'}")

    # Forward to the extracted chat agent (Option 2). Context is assembled here in
    # the proxy (which still holds vm_manager), then streamed to the agent's
    # /invocations; we relay its SSE frames straight back to the browser. The agent
    # terminates the stream with a {"type":"done"} event, or a {"type":"interrupt"}
    # marker when the turn paused awaiting a confirm-before-execute approval.
    agent_payload = {"action": "chat", "session_id": session_id, "message": message, "context": context}

    async def event_stream():
        async for event in _stream_agent_events(agent_payload):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/ai/chat/resume")
async def ai_chat_resume(request: Request):
    """Resume a chat turn that paused on a confirm-before-execute approval.

    The browser POSTs the opaque interrupt id(s) plus the user's decision ("y"/"n"/
    "t") and the SAME context-input fields /ai/chat accepts. We rebuild the context
    via the shared helper, mark approval_channel=True (resume belongs to the
    interactive gate), and forward an {"action":"resume", ...} payload to the agent,
    relaying its SSE frames back as a streaming response — exactly like /ai/chat. On
    resume the gated tool actually runs inside the agent turn (approve/trust) or is
    cancelled (deny), and the model continues streaming.
    """
    vm_manager = request.app.state.vm_manager
    body = await request.json()
    session_id = body.get("session_id", "")
    interrupt_responses = body.get("interrupt_responses") or []

    if not session_id:
        return json_error(400, "No session_id provided")
    if not isinstance(interrupt_responses, list) or not interrupt_responses or not all(
        isinstance(r, dict) and isinstance(r.get("interruptId"), str) and r.get("interruptId")
        for r in interrupt_responses
    ):
        return json_error(400, "interrupt_responses required")

    context = await _build_chat_context(body, vm_manager, session_id)
    context["approval_channel"] = True

    logger.info(f"AI chat resume: session={session_id[:8]}... "
                f"responses={len(interrupt_responses)} "
                f"model_id={body.get('model_id') or 'default'}")

    # Forward `response` VERBATIM as a string — normalization happens once, hook-side.
    agent_payload = {
        "action": "resume",
        "session_id": session_id,
        "context": context,
        "interrupt_responses": interrupt_responses,
    }

    async def event_stream():
        async for event in _stream_agent_events(agent_payload):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/ai/chat/sync")
async def ai_chat_sync(request: Request):
    """Non-streaming chat (JSON response)."""
    vm_manager = request.app.state.vm_manager
    body = await request.json()
    session_id = body.get("session_id", "")
    message = body.get("message", "").strip()
    notebook_cells = body.get("cells", [])
    active_cell_index = body.get("active_cell_index")

    if not message or not session_id:
        return json_error(400, "session_id and message required")

    # The web sends a WINDOW of cells tagged with their notebook position `index`
    # (services/chatCells.js), so look the active cell up by that tag; untagged
    # (older) payloads are positional.
    active_cell = None
    if isinstance(active_cell_index, int) and isinstance(notebook_cells, list) and notebook_cells:
        if any(isinstance(c, dict) and "index" in c for c in notebook_cells):
            active_cell = next((c for c in notebook_cells
                                if isinstance(c, dict) and c.get("index") == active_cell_index), None)
        elif 0 <= active_cell_index < len(notebook_cells):
            active_cell = notebook_cells[active_cell_index]
    if isinstance(active_cell, dict):
        message = f"[User is currently focused on Cell {active_cell_index} which contains: {(active_cell.get('code', ''))[:150]}]\n\n" + message

    pkg_list = body.get("packages", [])
    if pkg_list:
        pkg_names = [p.get("name", "") for p in pkg_list[:30]]
        message = f"[Installed packages: {', '.join(pkg_names)}]\n\n" + message

    context = await _build_chat_context(body, vm_manager, session_id)
    # Sync endpoint keeps auto-executing gated tools (no interactive gate): a
    # confirm_* frame would be silently dropped by the aggregator below. False turns
    # the hook into pass-through so execute_code/install_package run as today.
    context["approval_channel"] = False

    # Forward to the extracted chat agent and aggregate the streamed text into a
    # single JSON response (same external contract as before).
    agent_payload = {"action": "chat", "session_id": session_id, "message": message, "context": context}
    parts = []
    err = None
    async for event in _stream_agent_events(agent_payload):
        etype = event.get("type")
        if etype == "text":
            parts.append(event.get("content", ""))
        elif etype == "error":
            err = event.get("content", "AI chat failed")

    if err and not parts:
        logger.error(f"AI chat (sync) error: {err}")
        return json_error(500, f"AI chat failed: {err}")

    return {"response": "".join(parts), "session_id": session_id}


@router.delete("/ai/chat/{session_id}")
async def ai_clear_chat(session_id: str):
    """Clear conversation history for a session (reset, not a new parallel thread).

    Forwards a `clear` action to the extracted chat agent, which resets its
    in-memory thread AND deletes the durable session (agent_sessions + cascade
    agent_messages) via new_thread(). We ALSO delete here as a safety net for the
    agent-down case (same shared DB locally), so the reset is never blocked.
    """
    async for _ in _stream_agent_events({"action": "clear", "session_id": session_id}):
        pass  # drain the (single) status/error event
    await astorage.agent_session_delete(session_id)
    return {"status": "cleared", "session_id": session_id}


@router.post("/ai/annotate")
async def ai_annotate_notebook(request: Request):
    """Document a whole notebook in one pass: root title/intro, section headers,
    and per-cell comments. Returns {root, sections[], comments[]} keyed by cell index."""
    body = await request.json()
    cells = body.get("cells", [])
    if not cells:
        return json_error(400, "cells required")

    logger.info(f"AI annotate: {len(cells)} cells")
    try:
        result = await asyncio.to_thread(agent_annotate, cells)
        logger.info(
            f"AI annotate done: root={'yes' if result.get('root') else 'no'}, "
            f"sections={len(result.get('sections', []))}, comments={len(result.get('comments', []))}"
        )
        return result
    except Exception as e:
        import traceback
        logger.error(f"AI annotate error: {e}\n{traceback.format_exc()}")
        return json_error(500, f"Annotate failed: {e}")


@router.post("/ai/fix")
async def ai_fix_error(request: Request):
    """Fix a cell's error and return corrected code."""
    body = await request.json()
    code = body.get("code", "")
    error = body.get("error", "")
    cell_type = body.get("cell_type", "code")
    microvm_id = body.get("microvm_id", "")
    microvm_endpoint = body.get("microvm_endpoint", "")
    session_id = body.get("session_id", "")
    variables = body.get("variables", [])
    data_sources = body.get("data_sources", None)
    cells = body.get("cells", [])
    # Rich debug context (optional): full multi-frame traceback + failing-frame locals,
    # so the fix reasons over the real failure state rather than just the one-liner.
    error_traceback = body.get("traceback", "") or ""
    error_locals = body.get("error_locals", None)
    # 1 = first fix; >1 = user re-clicked Fix on the SAME error (prior fix didn't
    # resolve it). fix_error raises the sampling temperature on retries so it
    # explores a different fix instead of re-deriving the same near-deterministic one.
    attempt = body.get("attempt", 1)

    if not code or not error:
        return json_error(400, "code and error required")

    context = {
        "proxy_url": f"http://localhost:{os.environ.get('PROXY_PORT', '8081')}",
        "microvm_id": microvm_id,
        "microvm_endpoint": microvm_endpoint,
        "session_id": session_id,
        "cell_type": cell_type,
        "variables": variables,
        "data_sources": data_sources,
        "cells": cells,
        "attempt": attempt,
        "traceback": error_traceback,
        "error_locals": error_locals,
    }

    # AGENTIC fix when we can actually RUN the fix to verify it: a Python or SQL cell
    # with a live session. The agent proposes → execute_code / execute_sql → learns
    # from the real failure → reworks → returns a PROVEN fix. Gate ONLY on session_id
    # (+ code/sql cell): the tools reach the VM through the PROXY using the
    # X-Session-Id header (see execution_tools.py — they need session_id + proxy_url,
    # NOT the VM's direct endpoint, which the frontend doesn't even send on /ai/fix).
    # Otherwise (markdown, or no session) fall back to the one-shot fix. Both
    # return {"fixed_code","explanation","verified","attempts"} (+ verify_reason /
    # verify_error when not verified); the one-shot path is never verified.
    use_agentic = bool(session_id and cell_type in ("code", "sql"))
    fix_fn = agent_fix_error_agentic if use_agentic else agent_fix_error

    try:
        result = await asyncio.to_thread(fix_fn, code, error, context)
        if isinstance(result, dict):
            resp = {
                "fixed_code": result.get("fixed_code", ""),
                "explanation": result.get("explanation", ""),
            }
            # Verification status (both paths now report it; the one-shot path is
            # always verified=False/no_run since it never executes). verified=True
            # ONLY when the agent's last execute_code run succeeded on the exact
            # returned code. When not verified, also say WHY (verify_reason) and the
            # last run's full error (verify_error) so the UI can warn before Apply.
            if "verified" in result:
                resp["verified"] = bool(result.get("verified"))
                resp["attempts"] = int(result.get("attempts", 0) or 0)
                if not resp["verified"]:
                    resp["verify_reason"] = result.get("verify_reason") or "no_run"
                    resp["verify_error"] = result.get("verify_error") or ""
            return resp
        # Defensive: an older code path returning a bare string still works.
        return {"fixed_code": result, "explanation": ""}
    except Exception as e:
        logger.error(f"AI fix error: {e}")
        return json_error(500, f"Fix failed: {e}")


@router.post("/ai/suggest-tag")
async def ai_suggest_tag(request: Request):
    """Suggest a category tag for a notebook based on its cells."""
    body = await request.json()
    notebook_name = body.get("name", "")
    description = body.get("description", "")
    cells = body.get("cells", [])

    if not cells:
        return {"tag": "Drafts"}

    cell_summaries = []
    for cell in cells[:MAX_CELLS_FOR_TAG]:
        code = (cell.get("code", "") or "")[:200]
        cell_type = cell.get("type", "code")
        cell_summaries.append(f"[{cell_type}] {code}")

    context = "\n".join(cell_summaries)
    desc_line = f'\nDescription: "{description}"' if description else ""

    prompt = f"""Given this notebook titled "{notebook_name}"{desc_line} with these cells:
{context}

Respond with ONLY a single short category tag (1-2 words) for this notebook.
Examples: Analytics, ML Training, Data Cleaning, API Integration, Visualization, ETL, Exploration, Finance, Statistics, Web Scraping, NLP, Time Series, Geospatial.
Tag:"""

    # The Bedrock converse() call is synchronous/blocking. Offload it to a thread
    # (like /ai/chat/sync, /ai/annotate, /ai/fix, /terminal/suggest do) so this
    # handler never blocks the proxy's event loop during the round-trip.
    def _converse_tag() -> str:
        client = _get_bedrock_client()
        # Model-aware params: Sonnet 4.6 → {maxTokens, temperature} with no thinking
        # field (unchanged). 5.x models → deprecated temperature dropped + verbose
        # default reasoning suppressed (which otherwise eats maxTokens on Haiku).
        converse_kwargs = dict(
            modelId=require_model_id(AI_MODEL_ID),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig=build_inference_config(
                AI_MODEL_ID, max_tokens=TAG_MAX_TOKENS, temperature=TAG_TEMPERATURE,
            ),
        )
        _thinking = build_additional_request_fields(AI_MODEL_ID, disable_thinking=True)
        if _thinking is not None:
            converse_kwargs["additionalModelRequestFields"] = _thinking
        response = client.converse(**converse_kwargs)
        return response["output"]["message"]["content"][0]["text"].strip()

    try:
        output = await asyncio.to_thread(_converse_tag)
        tag = " ".join(output.split()[:2]).strip(".,;:!\"'")
        if not tag or len(tag) > TAG_MAX_LENGTH:
            tag = "Exploration"
        logger.info(f"AI suggested tag: '{tag}' for notebook '{notebook_name}'")
        return {"tag": tag}
    except Exception as e:
        logger.warning(f"AI tag suggestion failed: {e}")
        return {"tag": "Exploration"}


# ============================================================
# LINEAGE SUMMARY ("How was this computed?")
#
# One-shot, small fast model: given the upstream steps' code, the source
# identifiers they read and the output's var/columns/shape, return a 1–2 sentence
# plain-language summary. Results are cached in-process (LRU by input hash) so
# reopening the drawer for an unchanged chain costs nothing. Failures degrade to
# {"summary": None, "error": ...} — the drawer still shows the deterministic chain.
# ============================================================

_lineage_cache: "OrderedDict[str, str]" = OrderedDict()
_lineage_cache_lock = threading.Lock()


def _opt_int(v):
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _normalize_lineage_payload(body: dict) -> dict:
    """Coerce + cap the request body into the canonical shape used for both the
    cache key and the prompt (so equivalent inputs hash identically)."""
    body = body if isinstance(body, dict) else {}
    out = body.get("output") if isinstance(body.get("output"), dict) else {}
    var = out.get("var")
    cols = out.get("columns") if isinstance(out.get("columns"), list) else []
    output = {
        "var": str(var)[:120] if var else None,
        "kind": str(out.get("kind") or "")[:40],
        "columns": [str(c)[:80] for c in cols[:LINEAGE_MAX_COLUMNS]],
        "rows": _opt_int(out.get("rows")),
        "cols": _opt_int(out.get("cols")),
    }
    raw_steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    steps = []
    for s in raw_steps[:LINEAGE_MAX_STEPS]:
        if not isinstance(s, dict):
            continue
        stype = s.get("type") if s.get("type") in ("code", "sql") else "code"
        steps.append({
            "badge": str(s.get("badge") or "")[:16],
            "type": stype,
            "code": str(s.get("code") or "")[:LINEAGE_MAX_CODE_CHARS],
        })
    raw_sources = body.get("sources") if isinstance(body.get("sources"), list) else []
    sources = []
    for s in raw_sources[:LINEAGE_MAX_SOURCES]:
        if isinstance(s, dict) and s.get("id"):
            sources.append({"kind": str(s.get("kind") or "")[:40], "id": str(s.get("id"))[:512]})
    return {"output": output, "steps": steps, "sources": sources}


def _lineage_cache_key(norm: dict) -> str:
    return hashlib.sha256(
        json.dumps({"model": LINEAGE_MODEL_ID, "v": LINEAGE_PROMPT_VERSION, "p": norm},
                   sort_keys=True).encode("utf-8")
    ).hexdigest()


def _build_lineage_prompt(norm: dict) -> str:
    lines = ["Steps (in execution order):"]
    for i, s in enumerate(norm["steps"], 1):
        label = s["badge"] or f"step {i}"
        lines.append(f"--- {label} [{s['type']}] ---")
        lines.append(s["code"])
    lines.append("")
    if norm["sources"]:
        lines.append("Data sources read:")
        for s in norm["sources"]:
            lines.append(f"- {s['kind']}: {s['id']}")
    else:
        lines.append("Data sources read: none recorded")
    o = norm["output"]
    shape = ""
    if o["rows"] is not None and o["cols"] is not None:
        shape = f"{o['rows']} rows x {o['cols']} columns"
    lines.append("")
    lines.append(f"Output variable: {o['var'] or '(unnamed)'}"
                 + (f" ({o['kind']})" if o["kind"] else "")
                 + (f", shape {shape}" if shape else ""))
    if o["columns"]:
        lines.append("Output columns: " + ", ".join(o["columns"]))
    lines.append("")
    lines.append(
        "Explain how this output was computed in 1-2 plain sentences, at most "
        f"{LINEAGE_SUMMARY_CHARS} characters in total: name the data sources, then the "
        "key transformations, then the resulting output. Do not invent numbers or "
        "values that are not in the code. No markdown, no lists."
    )
    return "\n".join(lines)


def _clean_summary(text: str) -> str:
    """Collapse whitespace only. The length budget lives in the prompt; the model's
    reply is never cut in code (a hard slice produced mid-word summaries)."""
    return " ".join((text or "").split())


@router.post("/ai/explain-lineage")
async def ai_explain_lineage(request: Request):
    """Return a 1–2 sentence lineage summary.

    Request:  {"output": {"var", "kind", "columns": [..], "rows", "cols"},
               "steps": [{"badge", "type": "code"|"sql", "code"}],
               "sources": [{"kind", "id"}]}
    Response: {"summary": str, "cached": bool}
              | {"summary": None, "error": "Summary unavailable"}
    400 when the body is not JSON or has no steps.
    """
    try:
        body = await request.json()
    except Exception:
        return json_error(400, "Invalid JSON")
    norm = _normalize_lineage_payload(body)
    if not norm["steps"]:
        return json_error(400, "No steps provided")

    key = _lineage_cache_key(norm)
    with _lineage_cache_lock:
        hit = _lineage_cache.get(key)
        if hit is not None:
            _lineage_cache.move_to_end(key)
    if hit is not None:
        return {"summary": hit, "cached": True}

    if not await asyncio.to_thread(_ai_credentials_available):
        return {"summary": None, "error": "Summary unavailable"}

    prompt = _build_lineage_prompt(norm)

    def _converse_lineage() -> str:
        client = _get_bedrock_client()
        converse_kwargs = dict(
            modelId=require_model_id(LINEAGE_MODEL_ID),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig=build_inference_config(
                LINEAGE_MODEL_ID, max_tokens=LINEAGE_MAX_TOKENS, temperature=LINEAGE_TEMPERATURE,
            ),
        )
        _thinking = build_additional_request_fields(LINEAGE_MODEL_ID, disable_thinking=True)
        if _thinking is not None:
            converse_kwargs["additionalModelRequestFields"] = _thinking
        response = client.converse(**converse_kwargs)
        return response["output"]["message"]["content"][0]["text"]

    try:
        text = await asyncio.to_thread(_converse_lineage)
        summary = _clean_summary(text)
        if not summary:
            raise ValueError("empty summary")
    except Exception as e:
        # Hash prefix + exception type only — never the code or sources.
        logger.warning(f"AI lineage summary failed (key={key[:8]}): {type(e).__name__}")
        return {"summary": None, "error": "Summary unavailable"}

    with _lineage_cache_lock:
        _lineage_cache[key] = summary
        _lineage_cache.move_to_end(key)
        while len(_lineage_cache) > LINEAGE_CACHE_MAX:
            _lineage_cache.popitem(last=False)
    return {"summary": summary, "cached": False}


# ============================================================
# EXPLAIN THIS CODE (POST /ai/explain-cell)
#
# Request {"notebook_id", "cell_id", "code"?}: the proxy builds the context from the
# SAVED notebook + its DAG (upstream cells, budgeted); "code" is the unsaved editor
# text of the cell itself. The logic lives in proxy.notebook.ai.explain_*; this is
# the thin handler (auth is global via the app dependency).
# ============================================================
from fastapi.responses import JSONResponse
from proxy.notebook.ai.constants import (
    EXPLAIN_MAX_TOKENS, EXPLAIN_TEMPERATURE, EXPLAIN_MODEL_ID,
)
from proxy.notebook.ai import explain_cell as _explain_cell_mod
from proxy.notebook import dag_service as _explain_dag_service


@router.post("/ai/explain-cell")
async def ai_explain_cell(request: Request):
    """Structured explanation of one cell + its upstream cells.
    Response 200: {overview, sections[{title,start_line,end_line,explanation}],
    depends_on[{cell,provides,why}], notes, model, omitted_labels, omitted_count,
    missing_inputs, upstream_available, repaired}. Errors: {"error", "message"} with
    400 invalid_request|cell_too_large, 429 throttled, 502 model_failed,
    503 ai_unavailable, 504 timeout."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request", "message": "Invalid JSON."}, status_code=400)
    if not await asyncio.to_thread(_ai_credentials_available):
        return JSONResponse({"error": "ai_unavailable", "message": "AI is not available."},
                            status_code=503)

    def _converse_explain(system: str, user: str) -> str:
        client = _get_bedrock_client()
        converse_kwargs = dict(
            modelId=require_model_id(EXPLAIN_MODEL_ID),
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig=build_inference_config(
                EXPLAIN_MODEL_ID, max_tokens=EXPLAIN_MAX_TOKENS, temperature=EXPLAIN_TEMPERATURE,
            ),
        )
        _thinking = build_additional_request_fields(EXPLAIN_MODEL_ID, disable_thinking=True)
        if _thinking is not None:
            converse_kwargs["additionalModelRequestFields"] = _thinking
        response = client.converse(**converse_kwargs)
        return response["output"]["message"]["content"][0]["text"]

    status, payload = await _explain_cell_mod.explain_cell(
        body,
        get_notebook=astorage.notebook_get,
        get_dag=_explain_dag_service.get_or_build,
        converse=_converse_explain,
        model_id=EXPLAIN_MODEL_ID,
    )
    return JSONResponse(payload, status_code=status)


# ============================================================
# AI CHAT HISTORY (read-only)
#
# The chat agent (via its Strands SessionManager) is now the WRITER of record —
# it persists every message as the conversation happens (agent_sessions +
# agent_messages). So there is NO PUT here anymore; the frontend is a pure reader.
# GET returns the durable transcript translated into UI chat bubbles (user text ->
# user bubble; assistant text merged into one bubble per turn; tool blocks dropped),
# so a reloaded notebook shows the same conversation the user saw live.
# ============================================================

@router.get("/ai/chat/{session_id}/messages")
async def ai_get_messages(session_id: str):
    """Get the saved conversation for a session, as UI chat bubbles.

    Reads from whichever memory backend is active (MEMORY_BACKEND): the DB
    (agent_messages → messages_to_bubbles) for sqlite/postgres, or AgentCore Memory
    (list_events → events_to_bubbles) for agentcore. get_transcript_bubbles is a
    blocking call (DB or boto3), so we offload it to a worker thread to keep the
    event loop responsive."""
    import asyncio
    from proxy.notebook.ai.memory import get_transcript_bubbles
    messages = await asyncio.to_thread(get_transcript_bubbles, session_id)
    return {"messages": messages}


@router.post("/terminal/suggest")
async def terminal_suggest_command(request: Request):
    """
    NL-to-Shell: convert natural language to a bash command.
    
    The user describes what they want in plain English and the AI
    returns a ready-to-execute shell command for the MicroVM terminal.
    """
    body = await request.json()
    description = body.get("description", "")
    cwd = body.get("cwd", "/tmp")
    files = body.get("files", [])
    packages = body.get("packages", [])
    terminal_history = body.get("terminal_history", "")

    if not description.strip():
        return json_error(400, "description required")

    context = {
        "cwd": cwd,
        "files": files,
        "packages": packages,
        "terminal_history": terminal_history,
    }

    try:
        command = await asyncio.to_thread(agent_suggest_shell, description, context)
        return {"command": command}
    except Exception as e:
        logger.error(f"Terminal suggest error: {e}")
        return json_error(500, f"Suggest failed: {e}")
