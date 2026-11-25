"""
Constants for the proxy's AI module (proxy/notebook/ai and routes/ai.py).

Tunable parameters, truncation limits and the model configuration the proxy
reads. Model ids are not written here: the default chat/one-shot model comes
from BEDROCK_MODEL_ID (scripts/config.sh owns the value, local_install.sh
passes it to the proxy). INTEL_MODEL_ID is the one model pinned in code (the
INTEL_MODEL_ID env var overrides it).
The chat agent process keeps its own copy of the shared subset in
agent/ai_assistant/constants.py (it cannot import proxy.*).
"""

import logging
import os

logger = logging.getLogger(__name__)

# ============================================================
# MODEL CONFIGURATION
# ============================================================

# Default model for the one-shot calls (fix, annotate, tag, terminal suggest,
# lineage, explain) and the AI config/model routes. Read from BEDROCK_MODEL_ID
# only; empty when the proxy was not started by local_install.sh, in which case
# require_model_id() raises before any Bedrock call.
AI_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "").strip()
AI_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-west-2")
if not AI_MODEL_ID:
    logger.warning("BEDROCK_MODEL_ID is unset: AI calls fail until it is set "
                   "(scripts/config.sh, passed by ./local_install.sh)")


def require_model_id(model_id: str | None) -> str:
    """Return model_id, or raise a clear error when no model is configured."""
    if not model_id:
        raise RuntimeError(
            "No Bedrock model configured: BEDROCK_MODEL_ID is unset. Set it in "
            "scripts/config.sh and rerun ./local_install.sh.")
    return model_id


# Model for Workbook Intel (Phase 1, Phase 2, Incremental, Deletion)
INTEL_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Generation parameters
AGENT_TEMPERATURE = 0.2          # Low temperature for code generation (deterministic)
AGENT_MAX_TOKENS = 32768          # Max output tokens (model supports up to 64K)
TAG_TEMPERATURE = 0.0            # Zero temperature for tag suggestion (single-word answers)
TAG_MAX_TOKENS = 10              # Very short response for tag suggestions
# "How was this computed?" lineage summary (POST /ai/explain-lineage): a 1–2
# sentence description from a small fast model. Low temperature for stable,
# factual wording; short output; inputs are capped so the prompt stays small.
LINEAGE_TEMPERATURE = 0.1        # Near-deterministic plain-language summary
LINEAGE_MAX_TOKENS = 512         # Generous headroom over the ~500-char ask so the model never stops mid-sentence
LINEAGE_SUMMARY_CHARS = 500      # Length budget stated in the prompt; the reply is NOT truncated in code
LINEAGE_PROMPT_VERSION = 2       # Bump to invalidate cached summaries when the prompt changes
LINEAGE_CACHE_MAX = 256          # In-process LRU entries (keyed by input hash)
LINEAGE_MAX_STEPS = 25           # Max upstream steps sent to the model
LINEAGE_MAX_CODE_CHARS = 3000    # Per-step code truncation
LINEAGE_MAX_COLUMNS = 60         # Output column names sent to the model
LINEAGE_MAX_SOURCES = 50         # Source identifiers sent to the model
# "Explain this code" (POST /ai/explain-cell): structured explanation of one cell plus
# the upstream cells it depends on (via the reactive DAG). Upstream code is budgeted in
# WHOLE cells, nearest ancestors first; the cell's own code is never truncated.
EXPLAIN_UPSTREAM_CHAR_BUDGET = 24000   # Total characters of upstream code sent (whole cells only)
EXPLAIN_MAX_CELL_CHARS = 60000         # The cell's own code above this is rejected, not truncated
EXPLAIN_SHORT_LINES = 25               # Code with at most this many lines gets ONE section
EXPLAIN_MAX_SECTIONS = 12              # Upper bound on sections for long code
EXPLAIN_MAX_TOKENS = 4096              # Headroom for overview + sections + depends_on
EXPLAIN_TEMPERATURE = 0.1              # Near-deterministic, factual wording
EXPLAIN_TIMEOUT_S = 75                 # Whole-request deadline (model call + one JSON retry)
EXPLAIN_MODEL_ID = os.environ.get("EXPLAIN_MODEL_ID", "").strip() or AI_MODEL_ID
# "How was this computed?" summaries; defaults to the shared model.
LINEAGE_MODEL_ID = os.environ.get("LINEAGE_MODEL_ID", "").strip() or AI_MODEL_ID
FIX_MAX_TOKENS = 8192            # Full cell replacement — large multi-panel viz cells (make_subplots
                                 # + many traces/hovertemplates) can exceed 4096 output tokens, which
                                 # truncated the corrected code MID-STATEMENT and returned broken code.
                                 # 8192 gives ~2x headroom for the biggest realistic cell. (Sonnet 4.6
                                 # supports up to 64K, but a FIX needing more than this is over-rewriting.)

# Max verify iterations for the AGENTIC fix loop (fix_error_agentic). The agent
# proposes a fix, RUNS it via execute_code, and iterates (installing missing deps)
# until the cell runs clean — this caps the attempts so a stubborn error can't spin
# many expensive LLM+VM round-trips. Sourced from FIX_VERIFY_MAX_ATTEMPTS in
# config.sh (default 4). Clamped to a sane minimum of 1.
try:
    FIX_VERIFY_MAX_ATTEMPTS = max(1, int(os.environ.get("FIX_VERIFY_MAX_ATTEMPTS", "4")))
except (TypeError, ValueError):
    FIX_VERIFY_MAX_ATTEMPTS = 4

# ============================================================
# CONTEXT TRUNCATION LIMITS
#
# These control how much text we send to the LLM to stay within
# reasonable token budgets while preserving relevant context.
# Claude Sonnet supports 200K tokens, but shorter context = faster
# inference and more focused responses.
# ============================================================

# Per-cell truncation when building notebook state for the agent
CELL_CODE_MAX_CHARS = 200        # First 200 chars of code (captures imports + key logic)
CELL_OUTPUT_MAX_CHARS = 150      # First 150 chars of output (captures key result)
CELL_ERROR_MAX_CHARS = 150       # First 150 chars of error (captures the traceback line)
MARKDOWN_PREVIEW_MAX_CHARS = 100 # First 100 chars of markdown (captures heading/summary)

# Variable preview truncation
VARIABLE_PREVIEW_MAX_CHARS = 200 # Variable value preview sent to agent

# HTTP error response truncation
HTTP_ERROR_BODY_MAX_CHARS = 500  # Truncate HTTP error bodies for readability

# Max cells sent for tag suggestion (lightweight call)
MAX_CELLS_FOR_TAG = 4           # 4 cells is enough to classify notebook topic

# Tag suggestion max length
TAG_MAX_LENGTH = 25             # Max characters for a suggested tag

# ============================================================
# BEDROCK CLIENT CONFIGURATION
# ============================================================

BEDROCK_MAX_RETRIES = 5          # Retry attempts for transient errors
BEDROCK_READ_TIMEOUT = 120       # 2 min read timeout for model responses
BEDROCK_CONNECT_TIMEOUT = 10     # 10s connection timeout

# ============================================================
# WORKBOOK INTEL — INCREMENTAL (DELTA) PROMPT INPUT BUDGET
# ============================================================
# The model supports a very large context (~1M input tokens), so we do NOT tightly
# truncate the delta prompt inputs — truncating the entity schemas is what caused the
# model to guess/hallucinate join column names. These are generous SAFETY caps (in
# characters) to bound a pathological runaway prompt, not functional limits. Realistic
# content is ~60K chars total, far below these. ~4 chars/token, so 800K chars ≈ 200K tokens.
INTEL_DELTA_ENTITY_SUMMARIES_MAX_CHARS = 800_000  # all other sources' schemas (join grounding)
INTEL_DELTA_NEW_FILE_DOC_MAX_CHARS = 200_000      # the newly-uploaded file's profile
INTEL_DELTA_EXISTING_SUMMARY_MAX_CHARS = 200_000  # compact summary of existing report

# ============================================================
# SESSION CONFIGURATION
# ============================================================

# Agent conversation window (messages kept by SlidingWindowConversationManager).
# SIBLING of agent/ai_assistant/constants.py AGENT_CONVERSATION_WINDOW_SIZE: same env
# var (AGENT_CONVERSATION_WINDOW_SIZE, scripts/config.sh), same default (10), same
# floor (2). GET /ai/config exposes it so the web chat can show the last
# AGENT_CONVERSATION_WINDOW_SIZE // 2 turns (10 => 5 user + 5 assistant messages) as
# "in the AI's context". Also used by the proxy's Fix agent.
# Parsed exactly like the agent's _env_int: unset / non-numeric / negative -> 10.
try:
    _window = int(os.environ.get("AGENT_CONVERSATION_WINDOW_SIZE", "").strip() or 10)
except (TypeError, ValueError):
    _window = 10
AGENT_CONVERSATION_WINDOW_SIZE = max(2, _window if _window >= 0 else 10)
del _window

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
ATTACHMENT_TEXT_MIMES = {"application/json", "text/json", "text/csv", "text/plain"}

# Extension fallback when the browser-supplied mime is missing/generic
# (application/octet-stream). Keyed by lowercased file extension (no dot).
ATTACHMENT_EXT_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp",
    "pdf": "application/pdf", "txt": "text/plain", "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
}

# Size + count guardrails. Bedrock caps documents ~4.5MB and images ~a few MB;
# keep a single conservative per-file cap and a small per-message count.
ATTACHMENT_MAX_BYTES = 4_500_000        # ~4.5 MB per file
ATTACHMENT_MAX_PER_MESSAGE = 5          # max attachments in one chat turn
ATTACHMENT_TEXT_INLINE_MAX_CHARS = 200_000  # cap for JSON text inlined into the turn

# S3 key prefix for chat attachments (scoped per session).
ATTACHMENT_S3_PREFIX = "attachments"


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


def parse_attachment_marker(text: str) -> dict | None:
    """Parse a marker line back into {name, mime, s3_uri}, or None if not a marker."""
    if not isinstance(text, str) or not text.startswith(ATTACHMENT_MARKER_PREFIX):
        return None
    body = text[len(ATTACHMENT_MARKER_PREFIX):].strip()
    fields = {"name": "", "mime": "", "s3_uri": ""}
    for part in body.split("|"):
        part = part.strip()
        if part.startswith("name="):
            fields["name"] = part[len("name="):].strip()
        elif part.startswith("mime="):
            fields["mime"] = part[len("mime="):].strip()
        elif part.startswith("s3="):
            fields["s3_uri"] = part[len("s3="):].strip()
    return fields
