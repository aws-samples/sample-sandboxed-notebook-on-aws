"""
Constants for the AI CHAT assistant (the agent/ai_assistant process).

Holds the constants the chat code reads: the model configuration (from env), the
agent generation and retry parameters, the chat agent id, the per-turn source
labels, the chat attachment maps/guards and the execution-tool truncation
limits. Several of these duplicate proxy/notebook/ai/constants.py so the chat
agent runs standalone with NO proxy.* import; the chat-only ones (CHAT_* retry
budget, throttle cooldown) live only here.

SIBLING COPY: proxy/notebook/ai/constants.py holds the proxy's set (tag, intel,
bedrock, fix, lineage, explain constants and the attachment-marker parse +
mime/prefix helpers used by the proxy routes and transcript reader). If you change
a constant that lives in BOTH files, update it in both.
"""

import os

# ============================================================
# MODEL CONFIGURATION
# ============================================================

# Default chat model. Read from BEDROCK_MODEL_ID only (scripts/config.sh owns the
# value, local_install.sh passes it to the agent); require_model_id() raises a
# clear error before any Bedrock call when it is unset.
AI_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "").strip()
AI_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-west-2")


def require_model_id(model_id: str | None) -> str:
    """Return model_id, or raise a clear error when no model is configured."""
    if not model_id:
        raise RuntimeError(
            "No Bedrock model configured: BEDROCK_MODEL_ID is unset. Set it in "
            "scripts/config.sh and rerun ./local_install.sh.")
    return model_id

# ============================================================
# AGENT GENERATION PARAMETERS
# ============================================================

# Generation parameters
AGENT_TEMPERATURE = 0.2          # Low temperature for code generation (deterministic)
AGENT_MAX_TOKENS = 32768          # Max output tokens (model supports up to 64K)

# Bedrock client retry/timeout — handed to the BedrockModel's boto3 client so
# TRANSIENT failures (throttling, 5xx, timeouts) are retried with exponential
# backoff by boto3 BEFORE we ever see an error. Matches the values the proxy's
# BEDROCK_* constants use, so every Bedrock call site retries consistently. NOTE:
# this covers TRANSIENT errors only — a HARD model-unavailability (access denied /
# not-in-region / invalid model) is NOT retried here; that triggers the model
# FALLBACK chain in notebook_agent.chat_stream instead.
BEDROCK_MAX_RETRIES = 5          # boto3 retry attempts for transient errors
BEDROCK_READ_TIMEOUT = 120       # seconds — model responses can be slow
BEDROCK_CONNECT_TIMEOUT = 10     # seconds — connection establishment


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment, falling back to ``default`` on
    an unset / non-numeric / negative value (a bad override never breaks startup)."""
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# SMALL retry budget for the CHAT agent only (notebook_agent). The chat agent has a
# model FALLBACK chain plus a throttle COOLDOWN (below), which is what handles a
# PERSISTENT throttle (e.g. a model whose account quota is zero). Long retries in front
# of that only delay the switch: with the library defaults (Strands 6 attempts with
# 4s..240s backoff, times boto3's own 5 retries per attempt) a dead model took minutes
# to fall back. So the chat path retries just enough to absorb a momentary blip.
# Two stacked layers, both small:
#   * Strands ModelRetryStrategy around each model call: CHAT_MODEL_RETRY_MAX_ATTEMPTS
#     TOTAL attempts, backoff CHAT_MODEL_RETRY_INITIAL_DELAY doubling up to
#     CHAT_MODEL_RETRY_MAX_DELAY seconds (2 attempts => one 2s wait).
#   * boto3 inside each attempt: CHAT_BEDROCK_MAX_RETRIES TOTAL attempts (botocore
#     ``total_max_attempts``, standard mode, so 2 = one retry after the first call).
# One-shot / proxy call sites (Fix agent, helpers) have NO fallback and keep the larger
# BEDROCK_MAX_RETRIES above. Env overrides: CHAT_MODEL_RETRY_MAX_ATTEMPTS,
# CHAT_MODEL_RETRY_INITIAL_DELAY, CHAT_MODEL_RETRY_MAX_DELAY, CHAT_BEDROCK_MAX_RETRIES
# (a bad/negative value falls back to the default; attempts are floored at 1).
CHAT_MODEL_RETRY_MAX_ATTEMPTS = max(1, _env_int("CHAT_MODEL_RETRY_MAX_ATTEMPTS", 2))
CHAT_MODEL_RETRY_INITIAL_DELAY = _env_int("CHAT_MODEL_RETRY_INITIAL_DELAY", 2)   # seconds
CHAT_MODEL_RETRY_MAX_DELAY = _env_int("CHAT_MODEL_RETRY_MAX_DELAY", 10)          # seconds
CHAT_BEDROCK_MAX_RETRIES = max(1, _env_int("CHAT_BEDROCK_MAX_RETRIES", 2))       # total boto3 attempts


# Throttle COOLDOWN for the chat agent's model fallback (notebook_agent). When a model
# is still throttled AFTER every (small, see CHAT_* above) retry — e.g. an account whose quota
# for that model is effectively zero — the turn falls back to the next model in the
# chain and the throttled model's RESOLVED id is skipped for this long, so later turns
# go straight to a usable model instead of burning the full retry budget again.
# In-memory, per process (cleared on restart), expires by monotonic time.
# Env override: AI_THROTTLE_COOLDOWN_SECONDS (0 disables the cooldown).
AI_THROTTLE_COOLDOWN_SECONDS = _env_int("AI_THROTTLE_COOLDOWN_SECONDS", 600)  # 10 minutes

# ============================================================
# CONTEXT TRUNCATION LIMITS (read by tools/execution_tools.py)
#
# These control how much text we send to the LLM to stay within
# reasonable token budgets while preserving relevant context.
# ============================================================

# Per-cell truncation when building notebook state for the agent
CELL_CODE_MAX_CHARS = 300        # First 300 chars of code (the web already cuts chat cells to 300; keeps renames like .rename(columns=…))
CELL_OUTPUT_MAX_CHARS = 150      # First 150 chars of output (captures key result)
CELL_ERROR_MAX_CHARS = 150       # First 150 chars of error (captures the traceback line)
MARKDOWN_PREVIEW_MAX_CHARS = 100 # First 100 chars of markdown (captures heading/summary)

# Variable preview truncation
VARIABLE_PREVIEW_MAX_CHARS = 200 # Variable value preview sent to agent

# HTTP error response truncation
HTTP_ERROR_BODY_MAX_CHARS = 500  # Truncate HTTP error bodies for readability

# ============================================================
# SESSION CONFIGURATION
# ============================================================

# Agent conversation window: the chat agent's SlidingWindowConversationManager keeps
# the last AGENT_CONVERSATION_WINDOW_SIZE messages. Read as a window of
# AGENT_CONVERSATION_WINDOW_SIZE // 2 question/answer turns (10 => 5 user + 5
# assistant messages); the web chat shows the last that-many turns as "in the AI's
# context" (tool-use/tool-result messages also count toward the window, so this is
# the nominal, not exact, turn count). Env override: AGENT_CONVERSATION_WINDOW_SIZE
# (scripts/config.sh). The proxy reads the SAME env var with the SAME default
# (proxy/notebook/ai/constants.py) and exposes it on GET /ai/config. Floored at 2.
AGENT_CONVERSATION_WINDOW_SIZE = max(2, _env_int("AGENT_CONVERSATION_WINDOW_SIZE", 10))

# Strands agent_id for the interactive chat agent. Strands' SessionRepository keys
# messages by (session_id, agent_id, message_id) to allow multiple agents per
# session; our model is 1:1 (one notebook = one session = one chat agent = one
# conversation), so we pin a single stable, human-readable id that matches the
# panel's "AI Assistant" label. The column is retained in storage so multi-agent
# per session would work later without a schema change.
CHAT_AGENT_ID = "AI Assistant"

# ============================================================
# DATA SOURCE TYPE LABELS / DISPLAY ORDER
#
# Ordered (source_type -> display label) map used by the agent's per-turn data
# inventory (_build_available_data_block) and the get_available_data_sources tool
# to GROUP + LABEL sources by type. This mirrors the proxy's data source provider
# registry (proxy.platform.datasources.registry.provider_metadata) BUT is kept as
# a local static constant so the chat agent has NO import dependency on proxy.* —
# it must run as a standalone process (BedrockAgentCoreApp, and later AgentCore
# Runtime). The actual sources come from the /datasources catalog HTTP response
# (each entry carries source_type); we only use this for label + display order.
# Any source_type not listed here falls back to using the raw type as its label
# and is appended after the known ones (future-proof for a new provider).
SOURCE_TYPE_LABELS: list[tuple[str, str]] = [
    ("local", "Sandbox Files"),
    ("s3", "S3 Bucket"),
    ("dynamodb", "DynamoDB"),
    ("athena", "Athena"),
    ("aurora", "Aurora PostgreSQL"),
    ("bedrock_kb", "Bedrock Knowledge Base"),
]


# ============================================================
# CHAT ATTACHMENTS (images + documents attached in the AI chat panel)
# ============================================================
# Users can attach files to a chat turn ("explain this image", "summarize this
# PDF"). The file is uploaded to S3 by the proxy (POST /ai/attachments); only the
# S3 URI + metadata are persisted in history. For the CURRENT turn, the proxy
# re-fetches the bytes from S3 and sends them to the model as Bedrock Converse
# content blocks (image/document with inline bytes — Sonnet 4.6 rejects the
# s3Location source, verified live, so bytes are the only working path).
#
# Formats map to the Converse block type. These MUST stay within Bedrock's
# supported sets: ImageFormat = png/jpeg/gif/webp; DocumentFormat =
# pdf/doc/docx/xls/xlsx (BINARY documents only — see below).
#
# CLASSIFICATION TAXONOMY (three kinds):
#   * image    → Converse IMAGE block (requires the model's VISION capability).
#   * document → Converse DOCUMENT block for TRUE BINARY documents the model must
#                parse natively: pdf/doc/docx/xls/xlsx (requires the model's
#                DOCUMENT_SUPPORT capability — a vision-only model rejects these).
#   * text     → inline the decoded UTF-8 as a {"text": ...} block. csv/txt/json
#                are PLAIN TEXT: any model can read them once inlined, so they need
#                NO model capability at all. They live in ATTACHMENT_TEXT_MIMES (not
#                in ATTACHMENT_DOCUMENT_FORMATS) precisely so they are never gated.
#
# Why csv/txt are text (not documents): csv and txt are human-readable UTF-8. There
# is no reason to spend a Converse DOCUMENT block (and force document_support) on
# them when inlining the text works on every model — and inlining also lets us
# attach the S3-usage hint so the agent can JOIN them by S3 URI.
ATTACHMENT_IMAGE_FORMATS = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
    "image/gif": "gif", "image/webp": "webp",
}
# BINARY documents ONLY (pdf/doc/docx/xls/xlsx). csv/txt are NOT here — they are
# plain text (see ATTACHMENT_TEXT_MIMES). This set is what gates on document_support.
ATTACHMENT_DOCUMENT_FORMATS = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
# Text-inlined formats — csv/txt/json. These are plain readable text, require NO
# model capability, and are sent as {"text": ...} blocks (not Converse documents).
# (The agent classifies upstream in the proxy; this copy exists so valid_doc_fmts
# in _build_message_content stays binary-only and csv/txt never match the doc branch.)
ATTACHMENT_TEXT_MIMES = {"application/json", "text/json", "text/csv", "text/plain"}

# Size + count guardrails. Bedrock caps documents ~4.5MB and images ~a few MB;
# keep a single conservative per-file cap and a small per-message count.
ATTACHMENT_MAX_BYTES = 4_500_000        # ~4.5 MB per file
ATTACHMENT_MAX_PER_MESSAGE = 5          # max attachments in one chat turn
ATTACHMENT_TEXT_INLINE_MAX_CHARS = 200_000  # cap for JSON text inlined into the turn


# Persisted attachment marker. When a chat turn carries an image/document, the heavy
# Converse block (with raw bytes) is NOT persisted; instead a lightweight TEXT block
# with this marker is stored in history (see memory.py scrub) so (a) the transcript
# stays small and old turns don't re-ship megabytes, and (b) the transcript reader
# (which keeps only {"text"} blocks) can parse it back into a structured chip on
# reload (see _transcript.py). Format is a single line, pipe-delimited, round-trip
# parseable: ⟦attachment⟧ name=<name> | mime=<mime> | s3=<s3_uri>
ATTACHMENT_MARKER_PREFIX = "\u27e6attachment\u27e7"  # ⟦attachment⟧


def format_attachment_marker(name: str, mime: str, s3_uri: str) -> str:
    """Render the persisted-attachment marker text block (see ATTACHMENT_MARKER_PREFIX)."""
    # Strip pipes/newlines from fields so the single-line delimited form stays parseable.
    def _clean(v: str) -> str:
        return (str(v or "")).replace("|", "/").replace("\n", " ").strip()
    return f"{ATTACHMENT_MARKER_PREFIX} name={_clean(name)} | mime={_clean(mime)} | s3={_clean(s3_uri)}"
