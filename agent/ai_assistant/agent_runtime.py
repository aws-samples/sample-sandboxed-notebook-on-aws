"""
AI chat agent — the AgentCore Runtime artifact.

This runs the interactive Strands **chat** agent on the Bedrock AgentCore built-in
server (`BedrockAgentCoreApp`), instead of inside the proxy FastAPI app. The *same*
module IS the Runtime artifact: deployment is driven by the `@aws/agentcore` CLI /
CDK (agentcore.json + generated cdk/), not the deprecated starter-toolkit
configure/launch flow. Locally it's just a uvicorn server on 127.0.0.1:$AI_PORT
(scripts/config.sh exports 8082; with AI_PORT unset the server binds 8080, the
AgentCore Runtime contract port, see the bottom of this file).

What it exposes (the AgentCore contract, provided by the SDK):
  - POST /invocations   → the agent call. Our entrypoint dispatches on payload
                          "action": "chat" (streams events) | "clear" (reset thread).
  - GET  /ping          → health.

Design notes:
  - Only the CHAT agent (Class A) lives here. The one-shot helpers (fix/annotate/
    suggest-tag) and Workbook Intel (Class C) stay in the proxy.
  - We REUSE the existing agent functions unchanged: `chat_stream` (async generator
    yielding {"type": "text"|"action", ...}) and `new_thread` (reset). The proxy is
    the caller: it assembles `context` in-process (it still has
    `vm_manager`) and POSTs here, then relays our SSE frames to the browser.
  - Identity: AgentCore passes session/user on the invocation `context`. We resolve
    them there (payload is the local fallback) and thread user_id into memory via
    os.environ['AGENTCORE_ACTOR_ID'] — memory._resolve_actor_id stays the single
    source of truth.
  - Streaming: the entrypoint yields our custom {type: text|action|done|error|
    cleared} frames; the SDK serializes each to an SSE `data: <json>\n\n` frame —
    the same shape the proxy already relays to the frontend. We deliberately do NOT
    emit raw strands events.

Run locally:
    python -m ai_assistant.agent_runtime   # binds 127.0.0.1:$AI_PORT
"""

import os
import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from ai_assistant.notebook_agent import (
    chat_stream as agent_chat_stream,
    new_thread as agent_new_thread,
)

# Timestamped lines (HH:MM:SS.mmm); the `LEVEL:name:message` tail is the default
# basicConfig shape, so existing greps like 'INFO:ai_assistant.notebook_agent:' still match.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent_runtime")

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict, context=None):
    """AgentCore entrypoint. Dispatches on `action`.

    Payload shape (assembled by the caller — today the proxy's /ai/chat forwarder):
        {
          "action": "chat" | "clear" | "resume",
          "session_id": "<notebook session uuid>",
          "message": "<user message>",          # chat only
          "context": { ... per-turn context ... } # chat + resume
          "interrupt_responses": [               # resume only
            {"interruptId": "<opaque id>", "response": "y" | "n" | "t" | ...}
          ]
        }

    "resume" resumes a turn that paused on a confirm-before-execute interrupt. It
    carries NO user message (the empty-message guard is scoped to "chat" only) and
    threads the interrupt responses back into chat_stream.

    Identity comes from the AgentCore runtime `context` (session_id / user_id), with
    the payload as the local fallback. user_id is threaded into memory via the
    existing env override `AGENTCORE_ACTOR_ID` only — memory._resolve_actor_id stays
    the single source of truth.

    For "chat": yields the agent's stream events as dicts. The SDK serializes each
    yielded dict to an SSE `data:` frame. Event shapes (mirrors the pre-extraction
    proxy contract):
        {"type": "text",   "content": "..."}
        {"type": "action", "data": {...}}
        {"type": "model_status", "requested": ..., "active": ..., "reason": ...,
         "cooldown_remaining_s": ...}   # interactive channel; passed through as-is
        {"type": "done"}
        {"type": "error",  "content": "..."}

    For "clear": resets the session's conversation thread and yields a single
    status event.
    """
    payload = payload or {}

    # --- session_id: runtime context first, then the proxy payload, then a
    # synthesized fallback. AgentCore places session identity on the invocation
    # `context`; the proxy forwarder puts it in the body; a bare AgentCore client
    # (the `agentcore dev` TUI, a generic /invocations caller) sends neither, so we
    # mint one so a plain {"prompt": "..."} still works instead of erroring. ---
    import uuid as _uuid
    session_id = (getattr(context, "session_id", None)
                  or payload.get("session_id")
                  or _uuid.uuid4().hex)
    user_id = getattr(context, "user_id", None)
    if user_id:
        # Thread the actor through memory via the existing env override only.
        os.environ["AGENTCORE_ACTOR_ID"] = user_id

    action = payload.get("action", "chat")

    if action == "clear":
        # Reset the in-memory conversation thread for this session.
        agent_new_thread(session_id)
        yield {"type": "cleared", "session_id": session_id}
        return

    if action not in ("chat", "resume"):
        yield {"type": "error", "content": f"Unknown action: {action}"}
        return

    # --- message: accept BOTH payload shapes. The proxy forwarder sends
    # {"message": "..."}; the AgentCore CLI/TUI and standard /invocations clients
    # send {"prompt": "..."} (and sometimes a {"messages": [...]} list — take the
    # last user turn's text). This mirrors the scaffold main.py's _extract_prompt
    # while keeping our proxy contract unchanged. ---
    def _extract_message(p: dict) -> str:
        if p.get("message"):
            return str(p["message"]).strip()
        if p.get("prompt"):
            return str(p["prompt"]).strip()
        msgs = p.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if isinstance(last, dict):
                c = last.get("content")
                if isinstance(c, str):
                    return c.strip()
                if isinstance(c, list):  # content-block form: join any text blocks
                    return " ".join(
                        b.get("text", "") for b in c
                        if isinstance(b, dict) and "text" in b
                    ).strip()
                return str(last.get("content", "")).strip()
        return ""

    message = _extract_message(payload)
    # Per-turn operational context: the proxy nests it under "context"; a bare
    # AgentCore caller has none.
    context = payload.get("context") or {}

    # The empty-message guard applies to "chat" only. A "resume" call carries
    # interrupt responses and NO user message, so it must pass through.
    if action == "chat" and not message:
        yield {"type": "error", "content": "No message provided"}
        return

    if action == "resume":
        responses = payload.get("interrupt_responses") or []
        logger.info(f"resume: session={session_id[:8]}... responses={len(responses)}")
        stream = agent_chat_stream(session_id, "", context,
                                   interrupt_responses=responses)
    else:
        # Key info only — the message text is user data; log its length, not content.
        logger.info(f"chat: session={session_id[:8]}... msg_len={len(message or '')}")
        stream = agent_chat_stream(session_id, message, context)

    try:
        # Suppress the terminal {"type":"done"} when the turn PAUSED on an interrupt
        # (chat_stream emits a {"type":"interrupt"} marker and returns). `done` means
        # a normal finish; `interrupt` means the turn is waiting on a resume. This
        # applies to both the chat and resume arms (a resumed turn can pause again).
        paused = False
        async for event in stream:
            if event.get("type") == "interrupt":
                paused = True
            yield event
        if not paused:
            yield {"type": "done"}
    except Exception as e:
        import traceback
        logger.error(f"{action} stream error: {e}\n{traceback.format_exc()}")
        yield {"type": "error", "content": str(e)}


if __name__ == "__main__":
    # Port resolution — the default MUST be 8080, the AgentCore Runtime HTTP
    # service-contract port that BedrockAgentCoreApp.run() and the AgentCore CLI
    # dev harness both expect. In an AgentCore container (via `agentcore dev` /
    # `deploy`) AI_PORT is NOT set, so we bind 8080 and the runtime/CLI can reach
    # /invocations + /ping; binding anything else here makes the CLI's connection
    # attempts fail ("fetch failed"). For LOCAL non-container dev, local_install.sh
    # (and scripts/config.sh) explicitly export AI_PORT=8082 (8080 is the local
    # notebook backend), and the proxy's AGENT_URL points there — so the local
    # process still runs on 8082 while the container default stays on the contract
    # port. app.run binds 0.0.0.0 automatically inside a container.
    port = int(os.environ.get("AI_PORT", "8080"))
    logger.info(f"Starting AI chat agent (BedrockAgentCoreApp) on port {port}")
    app.run(port=port)
