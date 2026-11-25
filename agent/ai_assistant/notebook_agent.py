"""
Notebook AI CHAT Agent powered by Strands Agents SDK.

Part of: ai_assistant (the extracted chat-agent service)

This is the CHAT half split out of proxy/notebook/ai/notebook_agent.py. It creates
and manages per-session Strands agent instances that answer the user's questions,
run code on the VM through the execution tools and propose code cells in their
answers. The one-shot proxy helpers (fix_error, annotate_notebook,
suggest_shell_command) and Workbook Intel stay in the proxy copy — this module
carries ONLY the interactive chat agent.
Runs standalone with NO top-level proxy.* import. proxy.storage is only imported
LAZILY on the sqlite memory path: in new_thread (the durable clear) and in
memory.DbSessionRepository.
"""

import os
import re
import math
import time
import asyncio
import logging
import threading
import warnings
from botocore.config import Config as BotoConfig
from strands import Agent
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.event_loop._retry import ModelRetryStrategy
from strands.types.exceptions import ModelThrottledException

from .prompts import NOTEBOOK_AGENT_PROMPT
from .sessions import get_session, save_session, clear_session
from .constants import (
    AGENT_TEMPERATURE, AGENT_MAX_TOKENS, AGENT_CONVERSATION_WINDOW_SIZE,
    CHAT_AGENT_ID, SOURCE_TYPE_LABELS,
    CHAT_BEDROCK_MAX_RETRIES, BEDROCK_READ_TIMEOUT, BEDROCK_CONNECT_TIMEOUT,
    CHAT_MODEL_RETRY_MAX_ATTEMPTS, CHAT_MODEL_RETRY_INITIAL_DELAY, CHAT_MODEL_RETRY_MAX_DELAY,
    AI_THROTTLE_COOLDOWN_SECONDS,
    AI_MODEL_ID, AI_REGION, require_model_id,
)
from .modelcaps import (
    supports_prompt_caching,
    supports_vision,
    supports_document,
    resolve_model_id,
    build_inference_config,
    fallback_chain,
)
from .tools.execution_tools import (
    execute_code, get_variables, inspect_variable, get_notebook_state, set_execution_context,
    install_package, get_available_data_sources, promote_attachment,
    format_variable_entry, SCHEMA_BLOCK_MAX_COLS, SCHEMA_FETCH_TIMEOUT_S,
    clear_agent_scope,
)
from .tools.approval_hook import ExecutionApprovalHook, AUTO_DECLINE_STATE_KEY

logger = logging.getLogger(__name__)

# Model configuration (BEDROCK_MODEL_ID / BEDROCK_REGION, see constants.py)
# Operator denylist (deprecated / not-to-be-offered models), comma-separated ids —
# the SAME BEDROCK_EXCLUDE_MODELS the proxy /ai/models route reads, so a denied model
# can't sneak back in via the fallback chain either. Normalized downstream.
AI_EXCLUDE_MODELS = [x.strip() for x in os.environ.get("BEDROCK_EXCLUDE_MODELS", "").split(",") if x.strip()]

# Per-model-id cache: each DISTINCT model the user picks from the dropdown is built
# once and reused. Keyed by the RESOLVED (region-invocable) id, not the base id, so a
# base id and its already-resolved form map to the same BedrockModel. Replaces the old
# single-model singleton now that the model is user-selectable per turn.
_models: dict[str, BedrockModel] = {}

# Runtime self-heal for CATALOG DRIFT on temperature. The catalog
# (model_caps.json) is the source of truth for supports_temperature, but a provider
# can change behavior after the catalog was generated (e.g. OpenAI-on-Bedrock rejects
# temperature outright, yet a stale catalog marked it supported). When a turn fails
# at invoke time with a "model doesn't support the temperature field"
# ValidationException, we record the RESOLVED id here and rebuild that model without
# temperature — so the retry (and every later turn) succeeds regardless of what the
# catalog claims. Belt-and-suspenders alongside the fixed probe; never a hard fail.
_no_temperature: set[str] = set()


def _is_temperature_rejection(exc: Exception) -> bool:
    """True when an exception is Bedrock refusing the temperature inference field.
    Matches both the 'deprecated' (Claude 5.x) and 'doesn't support' (OpenAI) forms."""
    msg = str(exc).lower()
    return "temperature" in msg and any(
        p in msg for p in ("deprecat", "not supported", "unsupported",
                            "doesn't support", "does not support", "not support")
    )


def _is_model_unavailable(exc: Exception) -> bool:
    """True when an exception means the SELECTED MODEL is HARD-unavailable — a
    deterministic failure that won't fix itself on retry, so we should fall back to
    the next model in the chain rather than retry the same one.

    DELIBERATELY NARROW. This must NOT match transient errors (throttling, 5xx,
    timeouts) — boto3 already retries those, and switching models on a throttle would
    be wrong. It matches: access-denied (model not granted to the account),
    model-not-available / not-supported-in-region, and invalid/unknown model id. The
    temperature rejection is handled separately (it's a param fix, not unavailability)
    so it is explicitly EXCLUDED here.
    """
    if _is_temperature_rejection(exc):
        return False
    # botocore ClientError carries a structured error code when available.
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error", {}) or {}).get("Code", "") or ""
    code = code.lower()
    if code in ("accessdeniedexception", "resourcenotfoundexception"):
        return True
    msg = str(exc).lower()
    # Message-shape fallbacks (and to catch ValidationExceptions that name the model
    # as unavailable/invalid rather than carrying a distinct code).
    markers = (
        "access denied", "accessdenied", "don't have access", "not authorized",
        "is not available", "not available in", "not supported in",
        "model identifier is invalid", "provided model identifier is invalid",
        "invalid model identifier", "could not be found", "resourcenotfound",
        "no access to the model", "you don't have access",
    )
    return any(m in msg for m in markers)


# Error codes / message markers of a Bedrock THROTTLE. Matched case-insensitively.
_THROTTLE_CODES = ("throttlingexception", "throttling", "toomanyrequestsexception",
                   "toomanyrequests")
# "reached max retries" is deliberately NOT a marker on its own: botocore appends it
# to ANY retried error (5xx, timeouts). It only counts when the message also names a
# throttle — which the markers below already cover.
_THROTTLE_MARKERS = ("throttlingexception", "too many tokens", "too many requests",
                     "toomanyrequests")


def _is_exhausted_throttle(exc: Exception) -> bool:
    """True when ``exc`` is a Bedrock THROTTLE that survived every retry.

    Any throttle that reaches chat_stream has ALREADY been retried, with a deliberately
    SMALL budget — boto3 (CHAT_BEDROCK_MAX_RETRIES total attempts) inside Strands' own
    ModelRetryStrategy (CHAT_MODEL_RETRY_MAX_ATTEMPTS attempts, short backoff). The fallback
    chain + cooldown handle a persistent throttle (e.g. a model whose quota is effectively
    zero), so long retries would only delay the switch. A blip those retries absorb never
    raises at all, so it never reaches this classifier. What surfaces after the retries is
    Strands' ModelThrottledException (raised `from` the botocore ClientError).

    Kept SEPARATE from _is_model_unavailable on purpose: a throttle is not a hard
    unavailability (the model will work again later), so it gets its own fallback +
    cooldown policy in chat_stream instead of widening the hard-unavailable test.
    Matches Strands' ModelThrottledException (what BedrockModel raises for a
    ThrottlingException), a raw botocore ClientError with a throttle code, and the
    throttle message shapes ("Too many tokens", "TooManyRequests", ...).
    """
    if _is_temperature_rejection(exc):
        return False
    # Walk the cause chain: Strands raises ModelThrottledException `from` the ClientError.
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ModelThrottledException):
            return True
        resp = getattr(cur, "response", None)
        if isinstance(resp, dict):
            code = ((resp.get("Error", {}) or {}).get("Code", "") or "").lower()
            if code in _THROTTLE_CODES:
                return True
        cur = cur.__cause__  # explicit `raise ... from` only (not incidental context)
    msg = str(exc).lower()
    return any(m in msg for m in _THROTTLE_MARKERS)


class ModelThrottledMidStreamError(RuntimeError):
    """A throttle that hit AFTER the answer had started streaming. The turn is not
    switched to another model mid-answer; this error (whose message carries the full
    Bedrock error text) is surfaced as the turn's error frame instead, and the model
    is put on the throttle cooldown so the NEXT turn falls back automatically."""


class _ThrottleCooldown:
    """In-memory, per-process cooldown of RESOLVED model ids that fell back for
    throttling. Thread-safe (one lock; chat turns may run on different threads/event
    loops) and expires by MONOTONIC time — never persisted, cleared on restart.

    ``clock`` is injectable so tests can move time without sleeping.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def remaining(self, resolved: str) -> float:
        """Seconds left on ``resolved``'s cooldown (0.0 when it isn't cooling).
        Expired entries are pruned on read."""
        with self._lock:
            until = self._until.get(resolved)
            if until is None:
                return 0.0
            left = until - self._clock()
            if left <= 0:
                self._until.pop(resolved, None)
                return 0.0
            return left

    def is_cooling(self, resolved: str) -> bool:
        return self.remaining(resolved) > 0

    def expiry(self, resolved: str):
        """Clock time at which ``resolved``'s cooldown ends, or None when it isn't
        cooling. Two different values mean two different cooldown episodes."""
        with self._lock:
            until = self._until.get(resolved)
            if until is None or until <= self._clock():
                return None
            return until

    def add_unless_last(self, resolved: str, pool: list, seconds: float) -> bool:
        """Put ``resolved`` on cooldown for ``seconds`` — UNLESS that would leave no
        usable model in ``pool`` (the turn's resolved fallback chain), i.e. every
        OTHER member is already cooling (or there is no other member). The check and
        the insert happen under one lock so two concurrent turns can't each cool a
        different "last" model. Returns True when the cooldown was applied."""
        if seconds <= 0:
            return False
        with self._lock:
            now = self._clock()
            others = [p for p in pool if p != resolved]
            if not any(self._until.get(p, 0.0) <= now for p in others):
                return False
            self._until[resolved] = now + seconds
            return True

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


_throttle_cooldown = _ThrottleCooldown()


def _fmt_minutes(seconds: float) -> str:
    """'N min' wording for the user-facing cooldown notes (rounded up, at least 1)."""
    return f"{max(1, math.ceil(seconds / 60))} min"


# Cooldown notes already shown in each chat session: session_id -> {resolved model id:
# cooldown expiry}. One (model, expiry) pair is one cooldown episode, so the
# "X is cooling down" note appears once per episode per session, not on every turn
# (and not again on the approval resumes of the same answer). Cleared by new_thread().
_cooldown_notified: dict[str, dict[str, float]] = {}


def _remaining_s(resolved: str):
    """Whole seconds left on ``resolved``'s cooldown, or None when it isn't cooling."""
    left = _throttle_cooldown.remaining(resolved)
    return math.ceil(left) if left > 0 else None


def _model_status_event(requested: str, active: str, reason=None, remaining_s=None) -> dict:
    """Structured per-turn model status for the UI badge. Ids are BASE ids (the
    dropdown's values). reason: None (no fallback) | "cooldown" | "throttled" |
    "unavailable"; cooldown_remaining_s: whole seconds or None."""
    return {"type": "model_status", "requested": requested, "active": active,
            "reason": reason, "cooldown_remaining_s": remaining_s}


def _first_usable_index(chain_resolved: list, start: int, skip: str | None = None,
                        accepts=None):
    """Index of the first chain entry at/after ``start`` that is NOT on the throttle
    cooldown (and isn't ``skip``) and, when ``accepts`` is given, that ``accepts``
    returns True for. None when there is none."""
    for i in range(start, len(chain_resolved)):
        r = chain_resolved[i]
        if r == skip:
            continue
        if accepts is not None and not accepts(r):
            continue
        if not _throttle_cooldown.is_cooling(r):
            return i
    return None


def _turn_attachment_kinds(agent) -> set:
    """Binary attachment kinds ({"image", "document"}) present in the agent's LIVE
    history. Only blocks actually in agent.messages count: earlier turns' image and
    document blocks were already removed by memory._scrub_attachment_bytes, so what
    is found here belongs to the current turn. Text attachments are inlined as text
    and need no capability gate."""
    kinds = set()
    for msg in getattr(agent, "messages", None) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                if "image" in block:
                    kinds.add("image")
                if "document" in block:
                    kinds.add("document")
    return kinds


def _accepts_attachments(kinds: set):
    """Predicate over RESOLVED model ids: True when the model can take every attachment
    kind in ``kinds`` (image needs vision, binary document needs document support)."""
    def _accepts(resolved: str) -> bool:
        if "image" in kinds and not supports_vision(resolved):
            return False
        if "document" in kinds and not supports_document(resolved):
            return False
        return True
    return _accepts


# Keys of stream_async events that mean the MODEL has started producing content: a
# raw model-stream chunk ("event"), streamed text ("data"), a tool use, the assembled
# message, or the terminal result. Everything else ({"init_event_loop"}, {"start"},
# {"start_event_loop"}, {"force_stop"}, throttle-delay notices, ...) is bookkeeping
# that stream_async yields before / around the model call.
_CONTENT_EVENT_KEYS = ("event", "data", "current_tool_use", "message", "result")


def _is_content_event(ev) -> bool:
    return isinstance(ev, dict) and any(k in ev for k in _CONTENT_EVENT_KEYS)


def _turn_input_appended(agent, last_before) -> bool:
    """True when a FAILED stream attempt already appended this turn's input to the
    agent's history (Strands appends the user message / tool results before calling
    the model and keeps them on failure). Compared by identity against the message
    that was last before the attempt, so a sliding-window trim can't fool a length
    check. When True the retry continues from history (stream_async(None)) instead of
    re-sending the prompt, which would duplicate the user message."""
    msgs = getattr(agent, "messages", None) or []
    if not msgs:
        return False
    last = msgs[-1]
    return last is not last_before and isinstance(last, dict) and last.get("role") == "user"


def _swap_agent_model(agent, model_id: str, session_id: str) -> None:
    """Switch a live agent to ``model_id`` WITHIN a turn (fallback / temperature
    self-heal). Keeps the same Agent — so the same SessionManager, history, approval
    state and session-cache entry — and only replaces its model, then strips
    reasoningContent the new model may reject (same as a turn-boundary rebuild) and
    re-stamps _mvmnb_model_id so a later turn's model comparison sees the new id."""
    resolved = resolve_model_id(model_id, AI_REGION)
    agent.model = get_model(resolved)
    agent._mvmnb_model_id = resolved
    if getattr(agent, "messages", None) is not None:
        n = _strip_reasoning_content(agent.messages)
        if n:
            logger.info(f"Stripped {n} reasoningContent block(s) for {session_id[:8]}... "
                        f"(in-turn model switch to {resolved})")


def get_model(model_id: str | None = None) -> BedrockModel:
    """Get or create the Bedrock model instance for ``model_id`` (per-model-id cache).

    ``model_id`` may be a base id (e.g. the dropdown's ``id``) OR an already
    region-resolved/CRIS-prefixed id — we run it through resolve_model_id(., AI_REGION)
    so the invocable id is used both as the cache key and as the BedrockModel's
    model_id. Defaults to the configured AI_MODEL_ID when None.
    """
    model_id = require_model_id(model_id or AI_MODEL_ID)
    # Resolve the INVOCABLE id (global-first CRIS, then region-geo, then direct) BEFORE
    # constructing/looking up the model. resolve_model_id is idempotent on an already
    # resolved id, so this is safe whether the UI sent a base id or a resolved one.
    resolved = resolve_model_id(model_id, AI_REGION)
    cached = _models.get(resolved)
    if cached is not None:
        return cached
    # Build model-aware config via the SAME modelcaps resolver every other call site
    # uses, so a user-picked model gets params it actually accepts:
    #   * maxTokens is CLAMPED to the model's catalog max_output_tokens — critical now
    #     that the dropdown can select small-output models (e.g. Nova Lite caps at
    #     10000, Llama 4 Scout at 8192) while AGENT_MAX_TOKENS is 32768; sending the
    #     raw value raised a ConverseStream ValidationException.
    #   * temperature is included ONLY when the model accepts it (5.x Claude and some
    #     others reject it).
    # build_inference_config returns a Converse inferenceConfig ({maxTokens[, temperature]});
    # Strands BedrockModel takes those as plain kwargs, so we unpack them.
    inf = build_inference_config(resolved, max_tokens=AGENT_MAX_TOKENS,
                                 temperature=AGENT_TEMPERATURE)
    model_kwargs = dict(
        model_id=resolved,
        region_name=AI_REGION,
        max_tokens=inf["maxTokens"],
        # boto3 retry/timeout. The retry budget is deliberately SMALL (chat-only,
        # CHAT_BEDROCK_MAX_RETRIES total attempts, standard mode): enough to absorb a
        # momentary blip, but a PERSISTENT throttle is handled by the model fallback
        # chain + cooldown in chat_stream, so long boto3 retries would only delay the
        # switch. (Other Bedrock call sites have no fallback and keep BEDROCK_MAX_RETRIES.)
        boto_client_config=BotoConfig(
            retries={"total_max_attempts": CHAT_BEDROCK_MAX_RETRIES, "mode": "standard"},
            read_timeout=BEDROCK_READ_TIMEOUT,
            connect_timeout=BEDROCK_CONNECT_TIMEOUT,
        ),
    )
    # PROMPT CACHING — only attach the cache directives when the SELECTED model
    # actually supports Converse prompt caching (catalog-driven). Our ~80K-char system
    # prompt + tool specs are static and re-sent every turn, so caching them lets the
    # model reuse them at the cheap cache-read rate instead of re-billing the full
    # prompt each turn (e.g. Haiku 5.5 cache-read $0.01/M vs $0.10/M input). For a model
    # WITHOUT caching support we omit these entirely — passing cache directives to a
    # non-caching model is at best ignored, at worst a ValidationException.
    if supports_prompt_caching(resolved):
        model_kwargs["cache_config"] = CacheConfig(strategy="auto")  # caches the system prompt
        model_kwargs["cache_tools"] = "default"                       # caches the tool specs
    # Include temperature only if the catalog says so AND we haven't LEARNED at
    # runtime that this model rejects it (catalog-drift self-heal — see _no_temperature).
    if "temperature" in inf and resolved not in _no_temperature:
        model_kwargs["temperature"] = inf["temperature"]
    # Thinking is LEFT ON for this interactive chat agent. Disabling it on 5.x
    # (the fast/cheap config our one-shot benchmark measured) made Haiku 5.5 terse
    # and caused it to skip multi-step system-prompt instructions (e.g. "emit a
    # markdown cell first, then the code cell"). This module is ONLY the
    # conversational chat agent, so reasoning stays on so it follows the 80K-char
    # system prompt faithfully. (build_additional_request_fields is intentionally
    # not called here; the thinking-disabled win is kept only on one-shot paths,
    # which live in the proxy copy, not this standalone chat service.)
    #
    # Suppress ONLY the cache_tools deprecation warning. We intentionally keep
    # cache_tools="default": in this Strands version it's the single combination that
    # caches BOTH the system prompt (via strategy="auto") AND the tool specs while
    # still letting Strands auto-detect per-model cache support — the "modern"
    # CacheConfig(tools_ttl=...) only caches tools under strategy="anthropic", which
    # bypasses that detection. The deprecation is a harmless warning, not an error;
    # we filter exactly its message so no other warnings are hidden.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning,
                                message=r"cache_tools is deprecated.*")
        model = BedrockModel(**model_kwargs)
    _models[resolved] = model
    return model


# inspect_variable is read-only (the same schema the Variables panel shows) and is
# deliberately NOT in approval_hook.GATED_TOOLS: it is the approval-free way to
# check a variable's real columns.
AGENT_TOOLS = [execute_code, get_variables, inspect_variable, get_notebook_state, install_package,
               get_available_data_sources, promote_attachment]


def _strip_reasoning_content(messages: list) -> int:
    """Strip every `reasoningContent` block from a rehydrated message list, in place.

    WHY: this session is model-switchable mid-conversation. On a switch we rebuild the
    Agent but REUSE the same SessionManager, so durable history rehydrates into the new
    model. A reasoning-capable model (e.g. Claude Haiku 5.5) persists assistant messages
    carrying a `reasoningContent` block (a `reasoningText` with a signature, or opaque
    `redactedContent`). When the session then switches to an Amazon Nova model, Bedrock
    ConverseStream REJECTS that rehydrated history with a ValidationException (Nova 2 Lite
    rejects `reasoningContent.redactedContent`; Nova Pro rejects any reasoning content on
    a message). The vendored SDK filter `BedrockModel._rejects_reasoning_block` is too
    conservative — it keeps `redactedContent` and doesn't cover the Nova family — and we
    must not edit anything under `.venv`, so we strip reasoning blocks ourselves.

    Stripping is UNCONDITIONAL (every model, both the `reasoningText` and `redactedContent`
    variants, both assistant AND user messages) rather than a per-vendor allowlist: it's
    the lowest-risk approach and avoids a stale allowlist. It is also safe for context —
    reasoning is a per-turn artifact of the producing model; the conversational facts live
    in the sibling `text` blocks, which are preserved. A message whose `content` becomes
    empty after stripping is dropped entirely.

    Best-effort and never raises (mirrors memory._scrub_attachment_bytes): on any
    unexpected shape it leaves that message alone. Returns the count of blocks stripped.
    """
    stripped = 0
    try:
        kept_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                kept_messages.append(msg)
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                kept_messages.append(msg)
                continue
            # A reasoning block is any content-block dict with a "reasoningContent" key;
            # this single membership test covers BOTH reasoningText and redactedContent.
            new_content = [b for b in content
                           if not (isinstance(b, dict) and "reasoningContent" in b)]
            stripped += len(content) - len(new_content)
            if not new_content:
                # Dropping the whole message — its only content was reasoning.
                continue
            msg["content"] = new_content
            kept_messages.append(msg)
        # Rewrite the list in place so the caller's `agent.messages` binding is updated.
        messages[:] = kept_messages
    except Exception as e:  # noqa: BLE001
        logger.warning(f"reasoning-content strip skipped (unexpected message shape): {e}")
    return stripped


def get_or_create_agent(session_id: str, context: dict = None, callback_handler=None) -> Agent:
    """
    Get an existing agent for this session or create a new one.
    Uses SlidingWindowConversationManager to limit context to the last
    AGENT_CONVERSATION_WINDOW_SIZE messages (default 10, i.e. 5 question/answer turns).

    callback_handler: optional Strands callback handler for step-by-step tracing.
        chat_stream passes none (silent). Workbook Intel runs in the proxy with its
        own AgentTraceCallbackHandler and never reaches this function.
    """
    from datetime import datetime, timezone
    import os

    agent = get_session(session_id)

    # Model-switch at a TURN BOUNDARY (this function is called once per
    # /invocations turn with that turn's context). The UI sends the base id as
    # context["model_id"]; normalize BOTH it and the cached agent's stored id through
    # resolve_model_id(., AI_REGION) so the comparison is a single consistent key
    # (resolve is idempotent on an already-resolved id). If a cached agent exists but
    # was built with a DIFFERENT model, discard it (agent = None) so the build-new
    # path below rebuilds a fresh Agent with the new model AND the SAME
    # make_session_manager(session_id, ...) — history rehydrates via the cold-start
    # path and the fresh agent.state clears exec_trust (accepted trust reset). A
    # user's turn-boundary switch never mutates a live agent's .model in place (only
    # chat_stream's WITHIN-turn fallback does, via _swap_agent_model, so a failed
    # attempt's already-appended user message isn't duplicated).
    requested = resolve_model_id((context or {}).get("model_id") or AI_MODEL_ID, AI_REGION)
    if agent is not None and getattr(agent, "_mvmnb_model_id", None) != requested:
        old = getattr(agent, "_mvmnb_model_id", None)
        logger.info(f"Model switch for {session_id[:8]}...: {old} -> {requested}")
        agent = None

    if agent is None:
        # Inject dynamic context into the system prompt
        now = datetime.now(timezone.utc)
        aws_region = os.environ.get("AWS_REGION", "us-west-2")

        # Memory tier from context (if available)
        memory_tier = "Unknown"
        if context and context.get("memory_mib"):
            mem_mib = int(context["memory_mib"])
            memory_tier = f"{mem_mib} MB ({mem_mib / 1024:.1f} GB / {mem_mib / 2048:.1f} vCPU)"

        system_prompt = NOTEBOOK_AGENT_PROMPT.format(
            current_time=now.strftime("%Y-%m-%d %H:%M UTC (%A)"),
            aws_region=aws_region,
            memory_tier=memory_tier,
            athena_workgroup=os.environ.get("ATHENA_WORKGROUP", "microvm-demo"),
            athena_db=os.environ.get("ATHENA_DB", "microvm_demo_db"),
            s3_bucket=os.environ.get("ARTIFACT_BUCKET", f"microvm-sandbox-artifacts-{os.environ.get('ACCOUNT_ID', 'unknown')}-{aws_region}"),
            dynamo_table_prefix=os.environ.get("DYNAMO_TABLE", "microvm-demo").rsplit("-", 1)[0] + "-",
            # STABLE identifiers baked into the (frozen) system prompt so the agent
            # can scope v_nb_* metadata queries to THIS notebook. vm_id is NOT here
            # — it can rotate mid-session, so it rides the per-turn message instead
            # (see _build_available_data_block).
            notebook_id=(context or {}).get("notebook_id") or "unknown",
            session_id=session_id or "unknown",
        )
        # Durable conversation memory via the SessionManager seam (memory.py):
        # locally a DB-backed RepositorySessionManager, later AgentCore Memory —
        # same base class, factory-selected by MEMORY_BACKEND. Constructing the
        # Agent with session_manager makes it auto-PERSIST every message and, on a
        # COLD process/instance, REHYDRATE the conversation from storage (Strands'
        # AgentInitializedEvent -> initialize). The in-memory _sessions cache below
        # is then just a per-process fast path, not the source of truth.
        # agent_id is pinned to CHAT_AGENT_ID ("AI Assistant") — our 1:1 model.
        session_manager = None
        try:
            from .memory import make_session_manager, flush_session
            # Persistence is fire-and-forget (memory._PersistWriter): drain this
            # session's queued writes (e.g. from the agent being replaced on a model
            # switch) BEFORE the new SessionManager rehydrates history.
            flush_session(session_id)
            # Normalize notebook_id: the chat route defaults it to "" and some client
            # paths (in-cell NL generate) omit it entirely. Persist NULL, never "",
            # so agent_messages.notebook_id stays a valid (nullable) BIGINT — an empty
            # string can't sync to the Aurora bigint column and wedges the sync cycle.
            _nb_id = (context or {}).get("notebook_id")
            if isinstance(_nb_id, str):
                _nb_id = _nb_id.strip() or None
            session_manager = make_session_manager(session_id, notebook_id=_nb_id)
        except Exception as e:
            # Never let a memory-backend problem break chat — fall back to an
            # in-memory-only agent (no durable history) and log it.
            logger.warning(f"SessionManager unavailable for {session_id[:8]}... — "
                           f"continuing without durable history: {e}")

        agent = Agent(
            model=get_model(requested),
            agent_id=CHAT_AGENT_ID,
            system_prompt=system_prompt,
            tools=AGENT_TOOLS,
            # Confirm-before-execute gate: raises a native Strands interrupt before
            # execute_code / install_package run (unless auto-execute is ON or the
            # tool is session-trusted). This is the ONLY place the gate is enforced.
            hooks=[ExecutionApprovalHook()],
            conversation_manager=SlidingWindowConversationManager(window_size=AGENT_CONVERSATION_WINDOW_SIZE),
            callback_handler=callback_handler,
            session_manager=session_manager,
            # SMALL Strands retry budget (Strands' default is 6 attempts, 4s..240s
            # backoff = minutes). A persistent throttle is handled by the fallback
            # chain + cooldown in chat_stream, so long retries only delay the switch.
            retry_strategy=ModelRetryStrategy(
                max_attempts=CHAT_MODEL_RETRY_MAX_ATTEMPTS,
                initial_delay=CHAT_MODEL_RETRY_INITIAL_DELAY,
                max_delay=CHAT_MODEL_RETRY_MAX_DELAY,
            ),
            trace_attributes={"session.id": session_id},
        )
        # By the time Agent(...) returns, the SessionManager has synchronously
        # rehydrated durable history into agent.messages (Strands fires
        # AgentInitializedEvent at the end of __init__). On a cross-model switch that
        # history may carry reasoningContent blocks produced by a DIFFERENT model that
        # the now-selected model rejects (see _strip_reasoning_content). Strip them here
        # — once per cold build / switch — before the first turn replays the history.
        if getattr(agent, "messages", None) is not None:
            n = _strip_reasoning_content(agent.messages)
            if n:
                logger.info(f"Stripped {n} reasoningContent block(s) from rehydrated "
                            f"history for {session_id[:8]}... (model switch to {requested})")
        save_session(session_id, agent)
        # Track the model this cached agent was built with (the RESOLVED id — the
        # comparison key) so a later turn with a different model_id triggers the
        # discard-and-rebuild above.
        agent._mvmnb_model_id = requested
        logger.info(f"Created new agent session: {session_id} (region={aws_region}, "
                    f"memory={memory_tier}, model={requested}, "
                    f"durable={'yes' if session_manager else 'no'})")
    return agent




# Cap columns shown per source in the passive inventory — enough to disambiguate
# the common columns (and stop the model inventing names) without bloating every
# turn. The agent can still call get_available_data_sources() for the full schema.
_MAX_INVENTORY_COLS = 20


def _extract_source_columns(data_sources: dict) -> dict:
    """Build a {source_name: [column, ...]} map from the data_sources payload.

    Columns arrive in two shapes the frontend sends:
      • data_sources["athena"][i]["columns"]              (basic /datasources response)
      • data_sources["_catalog"]["entries"][i]["columns"] (deep discovery, all types)
    Column entries may be dicts ({"name": ...}) or bare strings. Deep-catalog
    entries win over the basic list when a name appears in both (they carry the
    fuller, discovered schema). Never raises — a malformed payload yields {}.
    """
    if not isinstance(data_sources, dict):
        return {}

    def _col_names(cols) -> list:
        out = []
        for c in cols or []:
            if isinstance(c, dict):
                n = c.get("name") or c.get("Name") or c.get("column")
            else:
                n = c
            if n:
                out.append(str(n))
        return out

    result: dict = {}
    try:
        # Basic Athena columns first (lower priority).
        for t in data_sources.get("athena") or []:
            if isinstance(t, dict):
                name = t.get("name") or t.get("table")
                cols = _col_names(t.get("columns"))
                if name and cols:
                    result[name] = cols
                    # Also key by db.table so same-named tables in two databases
                    # (default DB vs the S3 Tables/Iceberg namespace) don't collide.
                    if t.get("database"):
                        result[f"{t['database']}.{name}"] = cols
        # Deep-discovery catalog entries (higher priority — overwrite basic).
        catalog = data_sources.get("_catalog") or {}
        for entry in (catalog.get("entries") if isinstance(catalog, dict) else None) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("status") and entry.get("status") != "discovered":
                continue
            name = (entry.get("display_name") or entry.get("name")
                    or entry.get("source_id") or entry.get("table"))
            cols = _col_names(entry.get("columns"))
            if name and cols:
                result[name] = cols
                # Qualified key (source_id / database.name); lookups try it first.
                qualified = entry.get("source_id")
                if not qualified and entry.get("database") and name:
                    qualified = f"{entry['database']}.{name}"
                if qualified and qualified != name:
                    result[qualified] = cols
    except Exception:
        # Inventory enrichment is best-effort; never let a bad payload break the turn.
        return result
    return result


def _build_available_data_block(context: dict) -> str:
    """Assemble a compact, CURRENT inventory of the data the user can reach, from
    the context the frontend sends every turn: local /tmp files (uploaded_files)
    and cloud sources (data_sources: s3/dynamodb/athena).

    Why per-turn (prepended to the message) and NOT the system prompt: the agent
    is created ONCE per session and cached (get_or_create_agent), so its system
    prompt is frozen at creation time and would miss any file added afterwards.
    Riding the message guarantees the model sees the file the user JUST added.

    Returns '' when there's nothing to report (so short/no-data turns are unchanged).
    """
    if not context:
        return ""

    uploaded_files = context.get("uploaded_files") or []
    data_sources = context.get("data_sources") or {}

    lines = []

    if uploaded_files:
        lines.append("Local files (in /tmp/ on this VM):")
        for f in uploaded_files:
            name = f.get("name", "")
            if not name:
                continue
            size = f.get("size", "")
            schema = f.get("schema", "") or f.get("columns", "")
            entry = f"  - /tmp/{name}" + (f" ({size})" if size else "")
            if schema:
                entry += f" — columns: {schema}"
            lines.append(entry)

    # Source-type-AGNOSTIC rendering, driven by the generic `sources` array the
    # /datasources endpoint returns (each item: source_type, source_id, display_name,
    # detail). This replaces the old hardcoded s3/dynamodb/athena keys so EVERY
    # registered provider — Aurora's v_nb_* views included — appears in the passive
    # inventory the agent sees each turn, not only when it calls
    # get_available_data_sources(). Grouped by type, labeled via the provider registry.
    #
    # COLUMN SCHEMAS: the client sends column info in two places — athena[].columns
    # (basic /datasources response) and _catalog.entries[].columns (deep discovery,
    # all source types). We fold both into a name→[cols] map and append the columns
    # to each rendered source line. This is what lets the model write SQL/Python
    # against REAL columns instead of guessing (the customers.country bug); the
    # inventory used to be names-only, forcing a get_available_data_sources() call
    # the model often skipped.
    columns_by_name = _extract_source_columns(data_sources)

    def _with_cols(name: str, base: str, alt: str = "") -> str:
        # Prefer the qualified key (source_id / db.table); fall back to the bare name.
        cols = columns_by_name.get(name) or (columns_by_name.get(alt) if alt else None)
        if cols:
            shown = ", ".join(cols[:_MAX_INVENTORY_COLS])
            more = f", +{len(cols) - _MAX_INVENTORY_COLS} more" if len(cols) > _MAX_INVENTORY_COLS else ""
            return f"{base} — columns: {shown}{more}"
        return base

    if isinstance(data_sources, dict):
        generic = data_sources.get("sources")
        if isinstance(generic, list) and generic:
            # Label + display order from the LOCAL static map (constants.SOURCE_TYPE_
            # LABELS) rather than the proxy data-source registry — the chat agent must
            # run standalone with no proxy.* import. The actual sources (and their
            # schemas, surfaced by get_available_data_sources) come from the catalog;
            # here we only group + label. Unknown types fall back to the raw type.
            ordered = list(SOURCE_TYPE_LABELS)
            by_type: dict = {}
            for s in generic:
                if isinstance(s, dict) and s.get("source_type"):
                    by_type.setdefault(s["source_type"], []).append(s)
            known = {t for t, _ in ordered}
            for t in by_type:
                if t not in known:
                    ordered.append((t, t))
            for st, label in ordered:
                items = by_type.get(st) or []
                if not items:
                    continue
                lines.append(f"{label}:")
                for s in items:
                    name = s.get("display_name") or s.get("source_id") or ""
                    alt = ""
                    if st == "athena" and s.get("source_id"):
                        # Athena: show database.table (source_id) so the model never
                        # has to guess the database; tag Iceberg (S3 Tables) tables.
                        alt = name
                        name = s["source_id"]
                    detail = s.get("detail") or s.get("size") or ""
                    base = f"  - {name}"
                    if st == "athena" and (s.get("table_format") or "").lower() == "iceberg":
                        base += " · Iceberg"
                    base += (f" ({detail})" if detail else "")
                    lines.append(_with_cols(name, base, alt))
        else:
            # Backward-compatible fallback: an older frontend may still send only the
            # legacy grouped arrays (no generic `sources`). Render those so nothing
            # regresses if the generic list is absent.
            for label, key, fmt in (
                ("S3 files", "s3", lambda f: f"s3://{f.get('bucket','')}/{f.get('key','')}"
                    + (f" ({f.get('size','')})" if f.get("size") else "")),
                ("DynamoDB tables", "dynamodb", lambda t: t.get("name", "")),
                ("Athena tables", "athena", lambda t: (
                    f"{t['database']}.{t.get('name') or t.get('table')}"
                    if t.get("database") and (t.get("name") or t.get("table"))
                    else t.get("name") or t.get("table") or "")),
            ):
                group = data_sources.get(key) or []
                if group:
                    lines.append(f"{label}:")
                    for it in group:
                        rendered = fmt(it)
                        if rendered:
                            col_key = it.get("name") or it.get("table") or rendered
                            lines.append(_with_cols(rendered, f"  - {rendered}", col_key))

    if not lines:
        return ""

    return (
        "[Currently available data — this inventory is live and authoritative; "
        "if a file/table is listed here it DOES exist and you must not claim "
        "otherwise. Use get_available_data_sources() for exact column schemas "
        "before writing code that references a source.]\n"
        + "\n".join(lines)
    )


# ── Live variable schema in every turn ────────────────────────────────────────
# The VM's GET /variables/schema (read-only: columns/dtypes of pandas/numpy
# containers, never a user repr) is fetched best-effort before each NEW message and
# rendered into the "[Variables already defined…]" block, so the model sees a
# variable's REAL columns (e.g. product_id after a cell renamed the source's
# productId) without an approval-gated execute_code probe.
# The block must stay ONE header line + lines with NO blank line inside:
# proxy/storage/_transcript.py strips it from restored bubbles by splitting on "\n\n".
_SCHEMA_BLOCK_MAX_VARS = 25
_SCHEMA_BLOCK_MAX_CHARS = 6000
_SCHEMA_DETAILED_KINDS = ("dataframe", "series", "ndarray")
_ORPHAN_BLOCK_MAX_LINES = 25
_ORPHAN_BLOCK_MAX_CHARS = 3000
_MAX_RENAME_HINTS = 3
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")


def _fetch_variable_schema(context: dict) -> dict | None:
    """The turn's GET /proxy/variables/schema payload, or None (no session, no
    variables, VM down, older image, timeout, bad payload). Headers come from the
    turn's context (X-Session-Id) and env (X-Proxy-Auth) — never from the model.
    Logs only a status code / exception type."""
    session_id = (context or {}).get("session_id")
    if not session_id or not context.get("variables"):
        return None
    headers = {"X-Session-Id": str(session_id)}
    token = os.environ.get("PROXY_AUTH_TOKEN")
    if token:
        headers["X-Proxy-Auth"] = token
    proxy_url = context.get("proxy_url", "http://localhost:8081")
    try:
        import httpx
        with httpx.Client(timeout=SCHEMA_FETCH_TIMEOUT_S) as client:
            resp = client.get(f"{proxy_url}/proxy/variables/schema", headers=headers)
        if resp.status_code != 200:
            logger.info(f"variable schema unavailable: HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("variables"), list):
            return None
        return data
    except Exception as e:
        logger.info(f"variable schema fetch failed: {type(e).__name__}")
        return None


def _norm_col(s: str) -> str:
    """productId ≈ product_id ≈ Product ID (case/underscore/punctuation-insensitive)."""
    return re.sub(r"[_\W]", "", str(s)).lower()


def _rename_hints(var_cols: list, source_cols_by_name: dict) -> dict:
    """{variable column: source column} where a variable column is a RENAMED form
    of a column of a source it plausibly came from (shares >= 2 normalized column
    names). Cheap name comparison only; at most _MAX_RENAME_HINTS per variable."""
    hints: dict = {}
    if not var_cols or not source_cols_by_name:
        return hints
    var_norm = {_norm_col(c) for c in var_cols}
    for _src, scols in source_cols_by_name.items():
        snorm = {}
        for sc in scols or []:
            snorm.setdefault(_norm_col(sc), sc)
        if len(var_norm & set(snorm)) < 2:
            continue
        exact = set(scols)
        for c in var_cols:
            if c in exact or c in hints:
                continue
            s = snorm.get(_norm_col(c))
            if s and s != c:
                hints[c] = s
                if len(hints) >= _MAX_RENAME_HINTS:
                    return hints
    return hints


def _window_identifiers(context: dict) -> set:
    """Identifiers in the code of the cells around the active cell (±5 notebook
    positions), or of the last 10 sent cells when none is flagged active."""
    cells = [c for c in (context.get("notebook_cells") or []) if isinstance(c, dict)]
    if not cells:
        return set()
    active_pos = next((i for i, c in enumerate(cells) if c.get("active")), None)
    if active_pos is None:
        window = cells[-10:]
    else:
        a = cells[active_pos]
        a_idx = a.get("index")
        if isinstance(a_idx, int):
            window = [c for c in cells if isinstance(c.get("index"), int) and abs(c["index"] - a_idx) <= 5]
        else:
            window = cells[max(0, active_pos - 5): active_pos + 6]
    out: set = set()
    for c in window:
        out.update(_IDENT_RE.findall(str(c.get("code") or "")))
    return out


def _rank_schema_vars(entries: list, context: dict, message: str = "") -> list:
    """Priority: names in the user's message, then names used by cells near the
    active cell, then the VM's order (most recently executed/defined first)."""
    in_msg = set(_IDENT_RE.findall(message or ""))
    in_window = _window_identifiers(context or {})

    def rank(e):
        n = e.get("name")
        return 0 if n in in_msg else (1 if n in in_window else 2)
    return sorted(entries, key=rank)  # stable: equal rank keeps the VM's recency order


def _one_line(s: str) -> str:
    return s.replace("\r", " ").replace("\n", " ")


def _build_existing_vars_block(context: dict, message: str = "") -> str:
    """The variables already defined in the notebook, so the assistant uses their
    EXACT names (and, when the live schema arrived, their EXACT columns) and does
    NOT reuse a name (that collides with the defining cells and the reactive graph).
    Without a schema (fetch failed / VM down / older image) it is the names-only
    block."""
    names = context.get("variables") or []
    # Accept either a list of names or a {name: ...} dict.
    if isinstance(names, dict):
        names = list(names.keys())
    names = [str(n) for n in names if n]

    schema = context.get("_variable_schema")
    entries = schema.get("variables") if isinstance(schema, dict) else None
    entries = [e for e in (entries or []) if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]]
    if not entries:
        if not names:
            return ""
        return (
            "[Variables already defined in the notebook. These are the EXACT names — to "
            "USE an existing result, reference it by its exact name here (call "
            "get_variables for its type/shape/preview, or inspect_variable(name) for its "
            "exact columns — no approval needed — before writing code against it; "
            "never guess or invent a name). To CREATE a new result, pick a fresh name — "
            "do NOT overwrite one of these unless the user asks to modify that specific "
            "variable.]\n  "
            + ", ".join(sorted(names))
        )

    # Variables with NO producing cell (left by an AI exploration, a deleted cell, ...)
    # come FIRST, one marked line each, with their own budget; they are kept out of
    # the ranked detailed lines and out of "Other variables".
    orphans = [e for e in entries if e.get("orphan")]
    orphan_names = {e["name"] for e in orphans}
    orphan_lines: list = []
    orphan_overflow: list = []
    o_total = 0
    for e in orphans:
        line = "  " + _one_line(format_variable_entry(e, SCHEMA_BLOCK_MAX_COLS))
        if (len(orphan_lines) >= _ORPHAN_BLOCK_MAX_LINES
                or o_total + len(line) + 1 > _ORPHAN_BLOCK_MAX_CHARS):
            orphan_overflow.append(e["name"])
            continue
        orphan_lines.append(line)
        o_total += len(line) + 1
    if orphan_overflow:
        orphan_lines.append("  More variables with NO PRODUCING CELL: "
                            + _one_line(", ".join(orphan_overflow)))

    all_names = (set(names) | {e["name"] for e in entries}) - orphan_names
    detailed = [e for e in entries
                if e.get("kind") in _SCHEMA_DETAILED_KINDS and not e.get("orphan")]
    ranked = _rank_schema_vars(detailed, context, message)[:_SCHEMA_BLOCK_MAX_VARS]
    source_cols = _extract_source_columns(context.get("data_sources") or {})

    lines: list = []
    shown: set = set()
    total = 0
    for e in ranked:
        cols = [c[0] for c in (e.get("columns") or [])
                if isinstance(c, (list, tuple)) and len(c) == 2]
        hints = _rename_hints(cols, source_cols) if e.get("kind") == "dataframe" else None
        line = "  " + _one_line(format_variable_entry(e, SCHEMA_BLOCK_MAX_COLS, hints))
        if total + len(line) + 1 > _SCHEMA_BLOCK_MAX_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
        shown.add(e["name"])
    others = sorted(all_names - shown)
    if others:
        lines.append("  Other variables: " + _one_line(", ".join(others)))
    orphan_note = (
        " Variables marked [NO PRODUCING CELL …] were not produced by any cell (e.g. left "
        "by an AI exploration or a deleted cell): exploration may read them, but cell code "
        "must NOT reference them — recompute the data in the cell instead."
    ) if orphan_lines else ""
    return (
        "[Variables already defined in the notebook — LIVE schema from the kernel. For an "
        "existing variable THIS is the only authority for its columns (the data-source "
        "inventory lists RAW source columns, which cells often rename). Use these EXACT "
        "variable and column names; call inspect_variable(name) (no approval needed) for "
        "sample rows or any variable not detailed here. To CREATE a new result pick a fresh "
        "name — do NOT overwrite one of these unless the user asks."
        + orphan_note + "]\n"
        + "\n".join(orphan_lines + lines)
    )


def _build_vm_context_block(context: dict) -> str:
    """The CURRENT VM id for this turn. Rides the per-turn message (NOT the frozen
    system prompt) because a notebook's VM can change across restarts/rotations —
    so the value the agent sees is always the live one. Reference only: the agent
    is told (in <notebook_context>) to scope metadata queries by notebook_id/
    session_id, never vm_id. Empty when there's no active VM."""
    vm_id = (context or {}).get("vm_id")
    if not vm_id:
        return ""
    return f"[Current VM for this turn: vm_id = {vm_id} (reference only — do NOT filter v_nb_* queries by vm_id).]"


def _build_session_attachments_block(context: dict) -> str:
    """List EVERY file attached so far in this conversation (this turn + prior turns)
    with its S3 URI, so the model can act on a PRIOR-turn attachment by URI.

    Why this is needed: an attachment's bytes are sent to the model only on the turn
    it's attached, then scrubbed from persisted history (so the transcript stays
    light). Without this list, a follow-up like "add the attached file to my data
    sources" has nothing to act on — the model literally can't see the earlier file
    and (correctly, from its view) says "no file attached". This block gives it the
    durable reference: the name + S3 URI it can pass to promote_attachment or read
    directly, no re-attachment required."""
    atts = (context or {}).get("session_attachments") or []
    if not atts:
        return ""
    lines = ["[Files attached in this conversation — you can act on ANY of these by "
             "their S3 URI even if they were attached in an EARLIER turn (no "
             "re-attachment needed). To register one as a reusable data source, call "
             "promote_attachment with its S3 URI; to use it once, read it by the URI.]"]
    for a in atts:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or "attachment"
        uri = a.get("s3_uri") or ""
        mime = a.get("mime") or ""
        if uri:
            lines.append(f"  - {name} ({mime}) → {uri}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _prepend_available_data(message: str, context: dict) -> str:
    """Prefix the user's message with the current data inventory + existing-variable
    names + session attachments + the live vm_id (each included only if present)."""
    parts = [b for b in (_build_available_data_block(context),
                         _build_existing_vars_block(context, message),
                         _build_session_attachments_block(context),
                         _build_vm_context_block(context)) if b]
    return ("\n\n".join(parts) + "\n\n" + message) if parts else message


def _fetch_attachment_bytes(s3_uri: str) -> bytes:
    """Fetch an attachment's bytes from S3 (s3://bucket/key). Raises on failure —
    the caller decides whether to skip the block or surface the error."""
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {s3_uri!r}")
    bucket, _, key = s3_uri[len("s3://"):].partition("/")
    s3 = _attachment_s3_client()
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


# ONE shared S3 client for attachment fetches (boto3 clients are thread-safe; building
# one per attachment re-resolves credentials/endpoints on every call). Rebuilt if
# boto3.client itself was swapped (keeps monkeypatch-based test seams working).
_attachment_s3 = None
_attachment_s3_factory = None
_attachment_s3_lock = threading.Lock()


def _attachment_s3_client():
    global _attachment_s3, _attachment_s3_factory
    import boto3
    with _attachment_s3_lock:
        if _attachment_s3 is None or _attachment_s3_factory is not boto3.client:
            _attachment_s3_factory = boto3.client
            _attachment_s3 = boto3.client("s3", region_name=AI_REGION)
        return _attachment_s3


def _prefetch_attachment_bytes(attachments: list) -> dict:
    """Fetch every dict attachment's bytes IN PARALLEL. Returns {index: (raw, exc)}
    — exactly one of raw/exc is set — so the caller can re-raise the stored
    exception inside its existing per-attachment try (identical error handling)."""
    idxs = [i for i, a in enumerate(attachments) if isinstance(a, dict)]
    if not idxs:
        return {}

    def _one(i):
        try:
            return i, (_fetch_attachment_bytes(attachments[i].get("s3_uri") or ""), None)
        except Exception as e:  # noqa: BLE001 — surfaced per attachment by the caller
            return i, (None, e)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, len(idxs)),
                            thread_name_prefix="attachment-fetch") as pool:
        return dict(pool.map(_one, idxs))


def _build_message_content(message: str, context: dict):
    """Build the agent input for a turn — a plain string (no attachments) or a list
    of Bedrock Converse ContentBlocks (text + one block per attachment).

    Attachments arrive as lightweight refs in context["attachments"] (uploaded to S3
    by POST /ai/attachments): {s3_uri, name, mime, kind, format, size}. For the
    CURRENT turn we re-fetch the bytes from S3 and inline them as Converse blocks —
    Sonnet 4.6 rejects the s3Location source, so bytes are the only working path
    (verified live). kind:
      • "image"    → {"image": {"format", "source": {"bytes"}}}
      • "document" → {"document": {"format", "name", "source": {"bytes"}}}
      • "text"     → {"text": "<name>:\\n<decoded utf-8>"}   (JSON — not a Converse
                     document format, so it's inlined as text)

    The user's text (with the usual data-inventory/vars/vm preamble) is ALWAYS the
    first block. A per-attachment failure (missing S3 object, oversize, decode
    error) is logged and replaced with a short text note rather than failing the
    whole turn. Returns a str when there are no usable attachments so the common
    path is byte-for-byte unchanged.
    """
    text = _prepend_available_data(message, context)
    attachments = (context or {}).get("attachments") or []
    if not attachments:
        return text

    from .constants import (
        ATTACHMENT_IMAGE_FORMATS, ATTACHMENT_DOCUMENT_FORMATS,
        ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_PER_MESSAGE, ATTACHMENT_TEXT_INLINE_MAX_CHARS,
        format_attachment_marker,
    )
    valid_image_fmts = set(ATTACHMENT_IMAGE_FORMATS.values())
    valid_doc_fmts = set(ATTACHMENT_DOCUMENT_FORMATS.values())
    # Tabular attachment formats a user would want to JOIN with other data sources.
    # For these we ALSO surface the durable S3 URI + the exact read helper, so the
    # agent can emit runnable join code (read_s3_csv/read_s3_json against the real
    # object, or an S3 read_csv() in a SQL cell) instead of guessing a path. The VM
    # exec role can read the attachments/ prefix (S3ReadAccess is prefix-agnostic).
    _tabular_fmts = {"csv", "xls", "xlsx"}  # documents; json handled in the text branch

    def _s3_usage_hint(nm: str, uri: str, ext: str) -> str:
        if ext == "csv":
            how = f'read_s3_csv from its S3 URI: `df = read_s3_csv("{uri}")`'
        elif ext in ("xls", "xlsx"):
            how = (f'read it from S3 with pandas: '
                   f'`import boto3, pandas as pd; obj = boto3.client("s3").get_object('
                   f'Bucket="{uri.split("/",3)[2]}", Key="{uri.split("/",3)[3]}"); '
                   f'df = pd.read_excel(obj["Body"].read())`')
        elif ext == "json":
            how = f'read_s3_json from its S3 URI: `df = read_s3_json("{uri}", lines=False)`'
        else:
            how = f'read it from its S3 URI: `{uri}`'
        return (f"The attached file {nm} is also stored at {uri} and is readable from "
                f"the notebook. To use it in code (e.g. to JOIN it with another data "
                f"source), {how}. It is a one-off attachment for this turn, NOT a "
                f"registered data source — reference it by this S3 URI.")

    blocks = [{"text": text}]
    capped = attachments[:ATTACHMENT_MAX_PER_MESSAGE]
    prefetched = _prefetch_attachment_bytes(capped)
    for idx, att in enumerate(capped):
        if not isinstance(att, dict):
            continue
        s3_uri = att.get("s3_uri") or ""
        name = att.get("name") or "attachment"
        mime = att.get("mime") or ""
        kind = att.get("kind") or ""
        fmt = att.get("format") or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else fmt
        try:
            raw, fetch_err = prefetched[idx]
            if fetch_err is not None:
                raise fetch_err
            if len(raw) > ATTACHMENT_MAX_BYTES:
                raise ValueError(f"attachment {name} exceeds size cap ({len(raw)} bytes)")
            # Marker text block FIRST — this is the lightweight, persisted record of
            # the attachment (carries name/mime/s3_uri). The heavy image/document
            # block that follows is scrubbed out before persistence (memory.py); the
            # marker survives and the transcript reader renders it as a chip.
            if kind in ("image", "document"):
                blocks.append({"text": format_attachment_marker(name, mime, s3_uri)})
            # Tabular documents: add the S3-usage hint so the agent can write runnable
            # join code against the real object (only when we have a usable S3 URI).
            if kind == "document" and fmt in _tabular_fmts and s3_uri:
                blocks.append({"text": _s3_usage_hint(name, s3_uri, ext)})
            if kind == "image" and fmt in valid_image_fmts:
                blocks.append({"image": {"format": fmt, "source": {"bytes": raw}}})
            elif kind == "document" and fmt in valid_doc_fmts:
                # Converse requires a non-empty document name; use the filename stem.
                doc_name = name.rsplit(".", 1)[0] or "document"
                blocks.append({"document": {"format": fmt, "name": doc_name,
                                            "source": {"bytes": raw}}})
            elif kind == "text":
                # JSON (and any text-kind) inlined as text — not a Converse doc format.
                # It carries a marker too (so it renders as a chip) plus the content,
                # plus the S3-usage hint so it's join-able by S3 URI like the tabular
                # documents above.
                decoded = raw.decode("utf-8", "replace")[:ATTACHMENT_TEXT_INLINE_MAX_CHARS]
                blocks.append({"text": format_attachment_marker(name, mime, s3_uri)})
                if s3_uri:
                    blocks.append({"text": _s3_usage_hint(name, s3_uri, ext)})
                blocks.append({"text": f"Attached file {name}:\n```\n{decoded}\n```"})
            else:
                logger.warning(f"[attachments] skipping {name}: unknown kind/format "
                               f"(kind={kind!r} fmt={fmt!r})")
                blocks.append({"text": f"[attachment {name} could not be attached: unsupported type]"})
        except Exception as e:
            logger.warning(f"[attachments] failed to attach {name} ({s3_uri}): "
                           f"{type(e).__name__}: {e}")
            blocks.append({"text": f"[attachment {name} could not be loaded]"})

    # Only one block (the text) means every attachment failed AND there were none
    # usable — return the plain string so behavior matches the no-attachment path.
    return blocks if len(blocks) > 1 else text


async def _clear_turn_scope(context: dict, scope: str) -> None:
    """Best-effort clear of a chat turn's VM exploration scratch, off the event loop
    (3 s timeout inside clear_agent_scope; never raises)."""
    try:
        await asyncio.to_thread(clear_agent_scope,
                                (context or {}).get("proxy_url", "http://localhost:8081"),
                                (context or {}).get("session_id"), scope)
    except Exception:
        pass


# Upper bound on auto-decline resume passes for one new message. One pass clears
# every pending interrupt in the normal case; the bound only guards against a
# pathological re-pause loop.
_MAX_AUTO_DECLINE_PASSES = 3


def _pending_interrupts(agent) -> list:
    """Interrupts a NEW message must decline before it can run.

    Empty unless the agent's interrupt state is activated (a turn paused on a
    confirm-before-execute approval that was never answered — e.g. the user typed
    instead of clicking, or the card was lost after a reload/restart). Returns the
    unanswered interrupts; if activated but all carry a response (crashed mid-resume)
    returns all of them, since Strands would still reject a plain prompt. Read-only
    access to Strands' private _interrupt_state — the only way to find the ids after
    a restart; every WRITE goes through the public resume path."""
    state = getattr(agent, "_interrupt_state", None)
    if state is None or not getattr(state, "activated", False):
        return []
    interrupts = list((getattr(state, "interrupts", None) or {}).values())
    unanswered = [i for i in interrupts if i.response is None]
    return unanswered or interrupts


def _interrupt_tool_name(itr) -> str:
    r = itr.reason if isinstance(itr.reason, dict) else {}
    return str(r.get("tool") or (itr.name or "").split(":", 1)[-1] or "tool")


DECLINED_INSPECTION_NOTE = "The earlier inspection wasn't run; the answer may guess column names."


def _decline_notice(tool_names: list) -> str:
    """One-sentence user-facing notice for an auto-decline (also persisted as the
    declined turn's final assistant text)."""
    if len(tool_names) == 1:
        return (f"Declined the pending {tool_names[0]} request because a new message "
                f"was sent; it was not run.")
    return (f"Declined {len(tool_names)} pending requests ({', '.join(tool_names)}) "
            f"because a new message was sent; they were not run.")


async def _auto_decline_pending(agent, session_id: str):
    """Decline every pending approval on ``agent`` so a new message can run.

    Uses the PUBLIC resume path: an interruptResponse "n" for each pending interrupt,
    with AUTO_DECLINE_STATE_KEY in invocation_state. Strands replays the stored
    toolUse with NO model call, the approval hook denies it (ignoring any bypass) so
    a "User denied" toolResult is appended, and the hook's AfterToolsEvent end_turn
    closes the turn with the notice as assistant text — again without a model call.
    History therefore stays valid (every toolUse has a toolResult, roles alternate).
    The stream is drained to natural exhaustion (clean OTel teardown + lock release)
    and its events are suppressed. Returns the notice, or None if nothing was pending.
    """
    declined: list = []
    for _ in range(_MAX_AUTO_DECLINE_PASSES):
        pending = _pending_interrupts(agent)
        if not pending:
            break
        names = [_interrupt_tool_name(i) for i in pending]
        declined.extend(names)
        prompt = [{"interruptResponse": {"interruptId": i.id, "response": "n"}}
                  for i in pending]
        stream = agent.stream_async(
            prompt, invocation_state={AUTO_DECLINE_STATE_KEY: _decline_notice(names)})
        async for _ev in stream:
            pass  # drain; nothing from the declined turn is shown
    if _pending_interrupts(agent):
        raise RuntimeError("Could not clear a pending approval; start a new conversation.")
    if not declined:
        return None
    logger.info(f"auto-decline: session={session_id[:8]}... "
                f"tools={','.join(declined)} interrupts={len(declined)}")
    return _decline_notice(declined)


async def chat_stream(session_id: str, message: str, context: dict,
                      interrupt_responses: list = None):
    """
    Send a message to the notebook agent and stream the response, OR resume a turn
    that paused on a confirm-before-execute interrupt.

    Args:
        session_id: UUID session identifier
        message: User's message (ignored when resuming)
        context: Dict with proxy_url, session_id, notebook_cells, etc. May carry
            "approval_channel" (True for the interactive /ai/chat + resume paths,
            False for /ai/chat/sync). Absent defaults to gated (True).
        interrupt_responses: when truthy, RESUME instead of starting a new turn —
            a list of {"interruptId": ..., "response": ...} dicts threaded back into
            the agent as interruptResponse content blocks.

    Yields:
        Dict events:
          {"type": "text", "content": "..."}                        — streamed text
          {"type": "action", "data": {"action": "insert_cell", ...}} — non-gated tool
          {"type": "action", "data": {"action": "confirm_execute",
                                       "code": ..., "interrupt_id": ...}} — gate
          {"type": "action", "data": {"action": "confirm_install",
                                       "package": ..., "interrupt_id": ...}} — gate
          {"type": "interrupt", "interrupt_ids": [...]}              — turn paused
          {"type": "model_status", "requested": <base id>, "active": <base id>,
           "reason": "cooldown"|"throttled"|"unavailable"|None,
           "cooldown_remaining_s": <int>|None}                       — interactive only;
                                       first on every turn (resumes too), again on a switch

    Pending approval + NEW message: if the session's agent is still paused on an
    unanswered approval (user typed instead of clicking Approve/Reject, or the card
    was lost after a reload/agent restart), every pending approval is auto-declined
    first (_auto_decline_pending — tool never runs, no extra model call), then the
    new message runs as a normal turn. On the interactive channel the first frame is
    a text notice "_(Declined the pending ... request ...)_"; on /ai/chat/sync
    (approval_channel False) the notice frame is suppressed so the aggregated
    response stays only the model's answer, but the decline itself is identical.
    """
    import json

    # Promote the proxy-supplied channel flag onto the execution context BEFORE
    # set_execution_context snapshots a COPY (execution_tools.set_execution_context
    # does _execution_context.set(context.copy())). The approval hook reads
    # _approval_channel via _get_context(), so it MUST be present on the dict at the
    # moment of the copy. Streaming/resume default True (gated); only /ai/chat/sync
    # sends approval_channel=False.
    context["_approval_channel"] = bool(context.get("approval_channel", True))

    # The turn's model FALLBACK CHAIN: [user's pick, lower-ranked dropdown models...,
    # configured default] as BASE ids (fallback_chain), plus each one's RESOLVED id
    # (the cooldown / cache key). Computed BEFORE the agent is built so a model on the
    # throttle cooldown is skipped up front — the turn goes straight to the first
    # usable model instead of spending the full retry budget on a model that was just
    # throttled. If EVERY model is cooling, the user's pick is tried anyway (and its
    # error surfaced if it still fails).
    requested_model = (context or {}).get("model_id") or AI_MODEL_ID
    chain = fallback_chain(requested_model, AI_REGION, AI_MODEL_ID,
                           exclude=AI_EXCLUDE_MODELS) or [requested_model]
    chain_resolved = [resolve_model_id(m, AI_REGION) for m in chain]
    chain_idx = 0
    # User-facing one-line notes (model switches and why), emitted before the answer.
    notes: list[str] = []
    start = _first_usable_index(chain_resolved, 0)
    cooldown_skip = start is not None and start > 0
    if cooldown_skip:
        left = _throttle_cooldown.remaining(chain_resolved[0])
        logger.warning(f"chat turn: {chain_resolved[0]} is on the throttle cooldown "
                       f"({left:.0f}s left) — using {chain[start]} for this turn.")
        # The text note is shown once per cooldown episode per session (interactive
        # channel only). Later turns and approval resumes in the same episode rely on
        # the model_status event (UI badge) instead of repeating the note.
        notified = _cooldown_notified.setdefault(session_id, {})
        episode = _throttle_cooldown.expiry(chain_resolved[0])
        if context.get("_approval_channel") and notified.get(chain_resolved[0]) != episode:
            notified[chain_resolved[0]] = episode
            notes.append(f"{chain[0]} is cooling down after being throttled for this account; "
                         f"using {chain[start]} instead. {chain[0]} will be tried again in "
                         f"about {_fmt_minutes(left)}.")
        context = {**context, "model_id": chain[start]}
        chain_idx = start

    # This turn's exploration scratch on the VM (agent_scope): execute_code probes
    # read the user's variables, and their names live in the scratch, never in the
    # user namespace. Keyed by the agent session id (stable across resume).
    turn_scope = f"chat:{session_id}"
    context["agent_scope"] = turn_scope
    # Set execution context so tools can reach the MicroVM
    set_execution_context(context)

    agent = get_or_create_agent(session_id, context)

    # AUTHORITATIVE per-turn model line — runs on EVERY turn (new or reused agent,
    # chat / resume / sync), so you can confirm from the agent log exactly which model
    # served this turn. `requested` is what the UI asked for (base id, or the default
    # when unset); `resolved` is the invocable id the BedrockModel was actually built
    # with (CRIS-resolved, after any fallback/exclude) — stashed on the agent as
    # _mvmnb_model_id. No user message text is logged (that's user data).
    _requested = requested_model
    _resolved = getattr(agent, "_mvmnb_model_id", None) or resolve_model_id(
        (context or {}).get("model_id") or AI_MODEL_ID, AI_REGION)
    logger.info(f"chat turn: session={session_id[:8]}... "
                f"{'resume' if interrupt_responses else 'new'} "
                f"model={_resolved} (requested={_requested})")

    paused = False
    try:
        decline_notice = None  # set only when a new message auto-declined a pending approval
        if interrupt_responses:
            # RESUME: the prompt is the interruptResponse content-block list, not a
            # user message. Strands matches each interruptId to the stored interrupt and
            # the hook's event.interrupt(...) returns that response instead of raising.
            prompt = [{"interruptResponse": {"interruptId": r["interruptId"],
                                             "response": r["response"]}}
                      for r in interrupt_responses]
        else:
            # A new message can't run while an approval is pending (Strands only accepts
            # interruptResponse blocks then) — decline the stale approval(s) first.
            # A NEW turn starts with an empty exploration scratch (a resume keeps it:
            # approved runs belong to the paused turn they resume).
            await _clear_turn_scope(context, turn_scope)
            decline_notice = await _auto_decline_pending(agent, session_id)
            # Live variable schema for the [Variables…] block (best-effort, 3 s timeout;
            # None → the names-only block, i.e. today's behavior).
            context["_variable_schema"] = await asyncio.to_thread(_fetch_variable_schema, context)
            # Off the event loop: attachment turns fetch bytes from S3 (in parallel).
            prompt = await asyncio.to_thread(_build_message_content, message, context)

        # Iterate the agent stream manually (not `async for`) so that when we pause on
        # an interrupt we can DRAIN the generator to its natural StopAsyncIteration
        # instead of abandoning it mid-yield. The {"result": ...} event is yielded by
        # stream_async BEFORE it runs its own post-yield teardown
        # (_end_agent_trace_span + context_api.detach(baggage_token), see
        # strands/agent/agent.py). Returning/breaking at that suspended final yield would
        # aclose() the generator under GeneratorExit, forcing the OTel context detach to
        # run in a different context and logging "Failed to detach context ... was created
        # in a different Context" at ERROR on EVERY pause. Draining lets stream_async tear
        # its span/baggage context down cleanly in its own frame, so a normal approval
        # pause emits no ERROR traces.
        # OPEN the stream: pull events until the model has started producing CONTENT,
        # self-healing three classes of failure that surface BEFORE any content (so a
        # retry shows no partial output):
        #   (a) TEMPERATURE rejection (catalog drift) — rebuild the SAME model without
        #       temperature and retry.
        #   (b) HARD MODEL-UNAVAILABILITY (access denied / not-in-region / invalid id) —
        #       walk the fallback chain DOWN by score to the next usable model (ending at
        #       the configured default) and emit a visible switch note.
        #   (c) EXHAUSTED THROTTLE (still throttled after the SMALL boto3 + Strands retry
        #       budgets, see CHAT_* constants) — same
        #       walk as (b), plus the throttled model goes on the throttle cooldown so
        #       later turns skip it (never the last usable model of the chain).
        # Other errors are re-raised unchanged.
        #
        # WHY "until content" and not just the first event: stream_async yields
        # bookkeeping events ({"init_event_loop"}, {"start"}, {"start_event_loop"}, ...)
        # BEFORE it calls the model, so a model error never arrives on the very first
        # event. The model has "started producing content" once a raw model-stream event
        # ({"event": ...}) or anything the loop below renders (data / tool use / result /
        # message) arrives — from that point on a failure is MID-STREAM (see
        # _event_source) and the turn is never switched to another model.
        #
        # HOW a retry keeps history intact: the failed attempt already appended (and the
        # SessionManager already persisted) this turn's user message. Rebuilding the agent
        # and re-sending the prompt would DUPLICATE it (two consecutive user messages in
        # durable history). Instead the SAME Agent (same SessionManager, same in-memory
        # history incl. any attachment bytes) gets its .model swapped and continues from
        # its current history with stream_async(None) — or re-sends the prompt only when
        # the failed attempt never appended it.
        # `buffered` holds the already-pulled events, replayed below.
        # Structured model status for the UI badge. Interactive channel only (the sync
        # aggregator would not use it). Emitted on EVERY interactive turn — resumes too —
        # so the badge stays right after the text note is suppressed. Re-emitted below
        # whenever the opening loop switches model.
        emit_status = bool(context.get("_approval_channel"))
        if emit_status:
            if cooldown_skip:
                yield _model_status_event(chain[0], chain[chain_idx], "cooldown",
                                          _remaining_s(chain_resolved[0]))
            else:
                yield _model_status_event(chain[0], chain[chain_idx])

        first_error = None
        turn_prompt = prompt
        buffered: list = []

        while True:
            last_before = agent.messages[-1] if getattr(agent, "messages", None) else None
            stream = agent.stream_async(turn_prompt)
            buffered = []
            try:
                while True:
                    ev = await stream.__anext__()
                    buffered.append(ev)
                    if _is_content_event(ev):
                        break
                break  # stream opened cleanly
            except StopAsyncIteration:
                stream = None
                break  # finished without content, cleanly
            except Exception as e:
                if first_error is None:
                    first_error = e
                turn_prompt = None if _turn_input_appended(agent, last_before) else prompt
                current = getattr(agent, "_mvmnb_model_id", None) or chain_resolved[chain_idx]
                if _is_temperature_rejection(e):
                    logger.warning(f"Model {current} rejected temperature at runtime — "
                                   f"rebuilding without it and retrying (catalog drift self-heal).")
                    _no_temperature.add(current)
                    _models.pop(current, None)
                    _swap_agent_model(agent, current, session_id)
                    continue  # retry SAME model (minus temperature)
                unavailable = _is_model_unavailable(e)
                throttled = (not unavailable) and _is_exhausted_throttle(e)
                if unavailable or throttled:
                    failed_name = chain[chain_idx]
                    cooled = False
                    if throttled:
                        cooled = _throttle_cooldown.add_unless_last(
                            current, chain_resolved, AI_THROTTLE_COOLDOWN_SECONDS)
                    # Advance to the next usable model in the chain (skipping any on the
                    # throttle cooldown, and the model that just failed). When this turn
                    # carries image/document blocks, candidates that cannot accept them are
                    # skipped too: sending bytes to a non-vision model is a ValidationException.
                    kinds = _turn_attachment_kinds(agent)
                    accepts = _accepts_attachments(kinds) if kinds else None
                    nxt = _first_usable_index(chain_resolved, chain_idx + 1, skip=current,
                                              accepts=accepts)
                    if nxt is not None:
                        chain_idx = nxt
                        next_model = chain[nxt]
                        reason = "throttled" if throttled else "unavailable"
                        logger.warning(f"Model {reason} ({current}: {e}) — falling back to "
                                       f"{next_model} (chain {nxt}/{len(chain) - 1})"
                                       f"{'; on throttle cooldown' if cooled else ''}.")
                        _swap_agent_model(agent, next_model, session_id)
                        if throttled:
                            note = (f"{failed_name} is throttled for this account (Bedrock kept "
                                    f"returning ThrottlingException after retries); switched to "
                                    f"{next_model}.")
                            if cooled:
                                note += (f" {failed_name} will be skipped for about "
                                         f"{_fmt_minutes(AI_THROTTLE_COOLDOWN_SECONDS)}.")
                                # This switch note announces the episode: later turns in it
                                # must not repeat the cooldown note.
                                if emit_status:
                                    _cooldown_notified.setdefault(session_id, {})[current] = (
                                        _throttle_cooldown.expiry(current))
                            notes.append(note)
                        else:
                            notes.append(f"Selected model was unavailable; using {next_model} instead.")
                        if emit_status:
                            reason = "throttled" if throttled else "unavailable"
                            yield _model_status_event(
                                chain[0], next_model, reason,
                                _remaining_s(chain_resolved[0]) if throttled else None)
                        continue
                    # Chain exhausted (every model failed or is cooling) — surface the
                    # ORIGINAL error (the first failure of this turn).
                    logger.error("Model fallback chain exhausted; no usable model.")
                    if accepts is not None:
                        later = range(chain_idx + 1, len(chain))
                        incompatible = [chain[i] for i in later
                                        if not accepts(chain_resolved[i])]
                        compatible = [chain[i] for i in later if accepts(chain_resolved[i])]
                        if incompatible:
                            attached = " and ".join(sorted(kinds))
                            if compatible:
                                detail = (f"The compatible fallback models for this turn's "
                                          f"{attached} attachment ({', '.join(compatible)}) are "
                                          f"on throttle cooldown; {', '.join(incompatible)} "
                                          f"cannot accept it. Attachment not stripped.")
                            else:
                                detail = (f"No compatible fallback model exists for this turn's "
                                          f"{attached} attachment: {', '.join(incompatible)} cannot "
                                          f"accept it. Attachment not stripped.")
                            logger.error(detail)
                            # The runtime turns str(e) into the error frame, and notes are not
                            # part of str(e), so the explanation goes into the message itself.
                            raise RuntimeError(f"{detail} Original error: {first_error}") \
                                from first_error
                    raise first_error
                raise  # not a self-healable error

        stream_model_name = chain[chain_idx]
        stream_model_resolved = getattr(agent, "_mvmnb_model_id", None) or chain_resolved[chain_idx]

        # Replay the already-pulled events, then continue the stream, so the rest of the
        # loop below is unchanged. A THROTTLE after content started is NOT switched to
        # another model mid-answer: the model goes on the throttle cooldown (so the NEXT
        # turn falls back automatically) and a clear error carrying the full Bedrock text
        # is raised — the runtime turns it into the turn's {"type": "error"} frame.
        async def _event_source():
            for ev in buffered:
                yield ev
            if stream is None:
                return
            try:
                async for ev in stream:
                    yield ev
            except Exception as e:
                if not _is_exhausted_throttle(e):
                    raise
                cooled = _throttle_cooldown.add_unless_last(
                    stream_model_resolved, chain_resolved, AI_THROTTLE_COOLDOWN_SECONDS)
                nxt = _first_usable_index(chain_resolved, 0, skip=stream_model_resolved)
                logger.warning(f"Model throttled MID-STREAM ({stream_model_resolved}: {e}); "
                               f"not switching mid-answer"
                               f"{'; on throttle cooldown' if cooled else ''}.")
                msg = (f"{stream_model_name} was throttled for this account partway through "
                       f"this answer, so the response is incomplete. ")
                if cooled and nxt is not None:
                    msg += (f"Your next message will use {chain[nxt]} automatically while "
                            f"{stream_model_name} cools down (about "
                            f"{_fmt_minutes(AI_THROTTLE_COOLDOWN_SECONDS)}). ")
                else:
                    msg += "Please try again shortly. "
                msg += f"Bedrock error: {e}"
                raise ModelThrottledMidStreamError(msg) from e

        # Auto-decline notice (interactive channel only; on /ai/chat/sync the aggregated
        # response must stay just the model's answer). Yielded directly as an output
        # frame — NOT via _event_source, whose events are raw Strands events filtered by
        # the loop below (a pre-shaped {"type": "text"} dict there would be dropped).
        if decline_notice and context.get("_approval_channel"):
            yield {"type": "text", "content": f"_({decline_notice})_\n\n"}
            if "execute_code" in decline_notice:
                # The declined call was usually the model's probe of the data (e.g.
                # df.columns): say so, separately (the notice text above is pinned by
                # web/src/services/chatTurns.js DECLINE_RE and the autodecline tests).
                yield {"type": "text", "content": f"_({DECLINED_INSPECTION_NOTE})_\n\n"}

        # Model-switch notes (why this turn is answered by a different model than the one
        # selected), yielded directly as output frames FIRST for the same reason.
        # Interactive channel only, like the decline notice: /ai/chat/sync aggregates
        # every text frame into the response that in-cell Generate writes as code, so a
        # note there would corrupt the cell. The switch is still logged per turn.
        if context.get("_approval_channel"):
            for note in notes:
                yield {"type": "text", "content": f"_({note})_\n\n"}

        async for event in _event_source():
            if paused:
                # Interrupt already surfaced; keep pulling only to let stream_async run
                # its post-yield teardown and finish normally. Emit nothing more.
                continue
            if "data" in event:
                yield {"type": "text", "content": event["data"]}
            elif "current_tool_use" in event:
                tool_use = event["current_tool_use"]
                tool_name = tool_use.get("name", "")
                tool_output = tool_use.get("output", "")

                # Non-gated notebook action tools: parse and emit the action frame
                # (unchanged). execute_code / install_package are NOT handled here — the
                # approval hook interrupts them, surfaced below on the result event.
                if tool_name in ("insert_cell", "edit_cell", "delete_cell") and tool_output:
                    try:
                        action_data = json.loads(tool_output)
                        yield {"type": "action", "data": action_data}
                    except (json.JSONDecodeError, TypeError):
                        pass
            elif "result" in event:
                # The terminal event of stream_async is {"result": AgentResult}. On a
                # confirm-before-execute interrupt, stop_reason == "interrupt" and
                # result.interrupts carries the unanswered Interrupt objects. We key the
                # decision SOLELY on this result event and ignore any intermediate
                # tool_interrupt_event/tool_cancel_event frames (they carry no matched
                # key and fall through harmlessly).
                result = event["result"]
                if getattr(result, "stop_reason", None) == "interrupt":
                    interrupts = result.interrupts or []
                    for itr in interrupts:
                        r = itr.reason or {}
                        tool = r.get("tool")
                        tool_input = r.get("input") or {}
                        if tool == "execute_code":
                            yield {"type": "action",
                                   "data": {"action": "confirm_execute",
                                            "code": tool_input.get("code", ""),
                                            "interrupt_id": itr.id}}
                        elif tool == "install_package":
                            yield {"type": "action",
                                   "data": {"action": "confirm_install",
                                            "package": tool_input.get("package_name", ""),
                                            "interrupt_id": itr.id}}
                    # Terminal marker for a PAUSED turn — the runtime suppresses `done`
                    # when it sees this, and the UI knows the stream paused (did not
                    # finish). Set `paused` and let the loop drain (see note above); the
                    # {"result": ...} is the last event, so the next pull exhausts the
                    # generator cleanly.
                    yield {"type": "interrupt",
                           "interrupt_ids": [i.id for i in interrupts]}
                    paused = True
                # Non-interrupt stop (end_turn, etc.): fall through to normal completion;
                # the runtime emits the terminal {"type":"done"} frame.
    finally:
        # A finished turn (also on error / early close) drops its scratch; a
        # PAUSED turn keeps it for the approved runs of its resume.
        if not paused:
            await _clear_turn_scope(context, turn_scope)



def new_thread(session_id: str) -> None:
    """Clear conversation history for a session (start fresh thread).

    "New conversation" is a RESET, not a new parallel thread: it removes the
    previous conversation under this same session_id. So we clear BOTH the
    per-process in-memory cache AND the durable store (agent_sessions +
    cascade agent_messages), then the next turn constructs a fresh agent that
    rehydrates to an empty history. session_id itself is stable across resets.
    """
    clear_session(session_id)
    # A new conversation starts with no cooldown notes shown, so the note is shown
    # once again if the model is still cooling.
    _cooldown_notified.pop(session_id, None)

    def _durable_clear():
        try:
            from proxy.storage import storage
            storage.agent_session_delete(session_id)
        except Exception as e:
            logger.warning(f"new_thread: durable clear failed for {session_id[:8]}...: {e}")

    # For the agentcore memory backend the durable store is AgentCore Memory, NOT the
    # DB — so also purge the session's events there (no-op for sqlite/postgres). Keeps
    # "new conversation" a true reset regardless of backend.
    def _agentcore_clear():
        try:
            from .memory import delete_transcript
            delete_transcript(session_id)
        except Exception as e:
            logger.warning(f"new_thread: agentcore clear failed for {session_id[:8]}...: {e}")

    # Route the deletes through the ordered persistence writer so they run AFTER any
    # still-queued writes of the previous conversation (FIFO), then wait for them:
    # new_thread returns only once the durable reset has actually happened.
    try:
        from .memory import submit, flush
    except Exception as e:  # memory module unavailable — clear inline as before
        logger.warning(f"new_thread: persistence writer unavailable ({type(e).__name__}); clearing inline")
        _durable_clear()
        _agentcore_clear()
    else:
        submit(session_id, "agent_session_delete", _durable_clear)
        submit(session_id, "delete_transcript", _agentcore_clear)
        flush(session_id)
    logger.info(f"Cleared agent session: {session_id}")
