"""
One-shot AI helpers that run inside the proxy (Strands Agents SDK + Bedrock Converse).

Part of: proxy.notebook.ai (Notebook application layer)

- annotate_notebook: markdown annotations for a notebook
- fix_error: one-shot fix of a failing cell
- fix_error_agentic: fix that runs the proposed code on the VM and iterates
- suggest_shell_command: terminal command from a description
- AgentTraceCallbackHandler: step-by-step trace used by Workbook Intel

The interactive chat agent is a separate process (agent/ai_assistant).
"""

import os
import uuid
import logging
from strands import Agent
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig
from strands.agent.conversation_manager import SlidingWindowConversationManager

from .constants import (
    AI_MODEL_ID, AI_REGION, require_model_id,
    AGENT_TEMPERATURE, AGENT_MAX_TOKENS, FIX_MAX_TOKENS, AGENT_CONVERSATION_WINDOW_SIZE,
    BEDROCK_MAX_RETRIES, BEDROCK_READ_TIMEOUT, BEDROCK_CONNECT_TIMEOUT,
)
# Import from the repo-root canonical `modelcaps` package. The proxy image's build
# context is the repo ROOT (proxy/Dockerfile + pool_manager/Dockerfile), which also
# COPYs /modelcaps to /app/modelcaps, and local dev runs uvicorn from the repo root —
# so top-level `modelcaps` is importable in every proxy runtime.
from modelcaps import (
    supports_temperature, supports_prompt_caching, build_inference_config,
    build_additional_request_fields,
)

logger = logging.getLogger(__name__)

def _prompt_caching_kwargs(model_id: str) -> dict:
    """BedrockModel kwargs that enable prompt caching, only when the catalog says the
    model supports it (same gate as the chat agent's get_model). Passing cache
    directives to a non-caching model is at best ignored and at worst rejected."""
    if not supports_prompt_caching(model_id):
        return {}
    return {
        "cache_config": CacheConfig(strategy="auto"),  # caches the system prompt
        "cache_tools": "default",                       # caches the tool specs
    }


class AgentTraceCallbackHandler:
    """Strands callback handler that logs what the agent is doing, step by step.

    Used for tracing long-running agent runs (e.g. the COMPLETE Workbook Intel flow)
    so we can see which tools the agent called, with what inputs, and what came back —
    rather than only the start/end boundaries. Does NOT stream raw text chunks (too
    noisy); it logs tool invocations, tool inputs, tool results (truncated), and
    reasoning summaries.

    Pass an instance as `callback_handler=` when creating the Agent.
    """

    # Keep logged tool inputs/outputs readable in the log
    _MAX_INPUT_CHARS = 600
    _MAX_RESULT_CHARS = 500

    def __init__(self, log_prefix: str):
        self._prefix = log_prefix          # e.g. "[intel] 16666093"
        self.tool_count = 0
        self._seen_tool_ids: set[str] = set()
    def __call__(self, **kwargs) -> None:
        try:
            self._handle(**kwargs)
        except Exception as e:
            # Tracing must never break the agent run
            logger.debug(f"{self._prefix}    → [trace] callback error (ignored): {e}")

    def _handle(self, **kwargs) -> None:
        # 1) New tool invocation — detected from the raw model stream chunk.
        event = kwargs.get("event") or {}
        tool_use_start = (
            event.get("contentBlockStart", {}).get("start", {}).get("toolUse")
            if isinstance(event, dict) else None
        )
        if tool_use_start:
            tid = tool_use_start.get("toolUseId", "")
            if tid and tid not in self._seen_tool_ids:
                self._seen_tool_ids.add(tid)
                self.tool_count += 1
                logger.info(f"{self._prefix}    → [trace] tool #{self.tool_count}: "
                            f"{tool_use_start.get('name', '?')}")

        # 2) Assistant message committed — log the fully-assembled tool input args,
        #    and any assistant reasoning/text turn boundaries.
        message = kwargs.get("message")
        if isinstance(message, dict):
            role = message.get("role")
            for block in message.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                if role == "assistant" and "toolUse" in block:
                    tu = block["toolUse"]
                    self._log_kv(f"tool input [{tu.get('name','?')}]", tu.get("input"))
                elif role == "user" and "toolResult" in block:
                    tr = block["toolResult"]
                    status = tr.get("status", "")
                    text = self._extract_result_text(tr)
                    logger.info(f"{self._prefix}    → [trace] tool result "
                                f"({status}): {self._truncate(text, self._MAX_RESULT_CHARS)}")

    def _log_kv(self, label: str, value) -> None:
        import json as _json
        try:
            s = value if isinstance(value, str) else _json.dumps(value, default=str)
        except Exception:
            s = str(value)
        logger.info(f"{self._prefix}    → [trace] {label}: {self._truncate(s, self._MAX_INPUT_CHARS)}")

    @staticmethod
    def _extract_result_text(tool_result: dict) -> str:
        parts = []
        for c in tool_result.get("content", []) or []:
            if isinstance(c, dict):
                if "text" in c:
                    parts.append(str(c["text"]))
                elif "json" in c:
                    parts.append(str(c["json"]))
        return " ".join(parts).replace("\n", " ")

    @staticmethod
    def _truncate(s: str, limit: int) -> str:
        s = s or ""
        return s if len(s) <= limit else s[:limit] + f"… (+{len(s) - limit} chars)"

_model = None
_bedrock_direct_client = None


def get_model() -> BedrockModel:
    """Get or create the Bedrock model instance (singleton).

    Only fix_error_agentic uses it (the chat agent and its model live in
    agent/ai_assistant); it stays a cached singleton so repeated agentic fixes
    reuse one BedrockModel and its boto3 client."""
    global _model
    if _model is None:
        # Build model-aware config: temperature only for models that accept it
        # (5.x models reject it), prompt caching only where the catalog allows it.
        from botocore.config import Config as _BotoConfig
        model_kwargs = dict(
            model_id=require_model_id(AI_MODEL_ID),
            region_name=AI_REGION,
            max_tokens=AGENT_MAX_TOKENS,
            # boto3 retry/timeout for transient failures — consistent with every
            # other Bedrock call site (BEDROCK_* constants).
            boto_client_config=_BotoConfig(
                retries={"max_attempts": BEDROCK_MAX_RETRIES, "mode": "standard"},
                read_timeout=BEDROCK_READ_TIMEOUT,
                connect_timeout=BEDROCK_CONNECT_TIMEOUT,
            ),
            # Prompt caching only for models the catalog marks as supporting it.
            **_prompt_caching_kwargs(AI_MODEL_ID),
        )
        # Only pass temperature on models that accept it — Strands forwards it to
        # Converse verbatim, and 5.x models raise a ValidationException if it's sent.
        if supports_temperature(AI_MODEL_ID):
            model_kwargs["temperature"] = AGENT_TEMPERATURE
        # Thinking is LEFT ON for this multi-step tool loop (same choice as the chat
        # agent): with thinking off, 5.x models skipped multi-step instructions. The
        # one-shot converse calls below (annotate, fix, terminal suggest) pass
        # disable_thinking=True.
        _model = BedrockModel(**model_kwargs)
    return _model


def _get_direct_client():
    """Get a cached Bedrock client for one-shot calls (explain, fix)."""
    global _bedrock_direct_client
    if _bedrock_direct_client is None:
        import boto3
        from botocore.config import Config
        _bedrock_direct_client = boto3.client(
            "bedrock-runtime",
            region_name=AI_REGION,
            # Standardized retry/timeout (BEDROCK_* constants) — same policy as the
            # chat agent + entity_discovery, not an ad-hoc 3/60.
            config=Config(
                retries={"max_attempts": BEDROCK_MAX_RETRIES, "mode": "standard"},
                read_timeout=BEDROCK_READ_TIMEOUT,
                connect_timeout=BEDROCK_CONNECT_TIMEOUT,
            ),
        )
    return _bedrock_direct_client


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


def annotate_notebook(cells: list) -> dict:
    """
    One-shot: document a whole notebook in a single pass. Returns
      {"root": str|None,
       "sections": [{"before_index": int, "markdown": str}],
       "comments": [{"index": int, "comment": str}]}
    Cells are referenced by their position in the input list.
    """
    from .prompts import ANNOTATE_PROMPT
    import json as json_mod

    # Build an index-addressable, compact view of the notebook.
    lines = []
    for i, c in enumerate(cells or []):
        ctype = (c.get("type") or "code").lower()
        code = (c.get("code") or "").strip()
        if ctype == "markdown":
            lines.append(f"[{i}] MARKDOWN\n{code[:400]}")
        else:
            out = (c.get("output") or "").strip()
            snippet = f"\n(output: {out[:200]})" if out else ""
            lines.append(f"[{i}] {ctype.upper()}\n{code[:800]}{snippet}")
    cells_repr = "\n\n".join(lines) if lines else "(empty notebook)"

    prompt = ANNOTATE_PROMPT.format(cells=cells_repr)
    client = _get_direct_client()

    # Model-aware params: for Sonnet 4.6 this is {maxTokens, temperature} with no
    # additionalModelRequestFields (unchanged). For 5.x models the helper drops the
    # deprecated temperature and supplies the thinking override to suppress verbose
    # default reasoning (which otherwise eats maxTokens on Haiku).
    converse_kwargs = dict(
        modelId=require_model_id(AI_MODEL_ID),
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=build_inference_config(
            AI_MODEL_ID, max_tokens=AGENT_MAX_TOKENS, temperature=AGENT_TEMPERATURE,
        ),
    )
    _thinking = build_additional_request_fields(AI_MODEL_ID, disable_thinking=True)
    if _thinking is not None:
        converse_kwargs["additionalModelRequestFields"] = _thinking
    response = client.converse(**converse_kwargs)
    text = response["output"]["message"]["content"][0]["text"].strip()

    # Strip markdown fences if present
    if text.startswith("```json"):
        text = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    elif text.startswith("```"):
        _l = text.split("\n")
        text = "\n".join(_l[1:-1] if _l[-1].strip() == "```" else _l[1:]).strip()

    try:
        data = json_mod.loads(text)
    except (json_mod.JSONDecodeError, TypeError):
        import re as _re
        m = _re.search(r'\{[\s\S]*\}', text)
        try:
            data = json_mod.loads(m.group(0)) if m else {}
        except (json_mod.JSONDecodeError, TypeError):
            data = {}

    n = len(cells or [])
    sections = [
        {"before_index": s["before_index"], "markdown": s["markdown"]}
        for s in (data.get("sections") or [])
        if isinstance(s, dict) and isinstance(s.get("before_index"), int)
        and 0 <= s["before_index"] <= n and s.get("markdown")
    ]
    comments = [
        {"index": c["index"], "comment": c["comment"]}
        for c in (data.get("comments") or [])
        if isinstance(c, dict) and isinstance(c.get("index"), int)
        and 0 <= c["index"] < n and c.get("comment")
    ]
    return {"root": data.get("root") or None, "sections": sections, "comments": comments}


def _build_fix_context_section(context: dict) -> str:
    """Build the <available_variables>/<full_traceback>/<failing_frame_locals>/
    <available_data_sources>/<previous_cells> context block appended to a fix prompt.
    Shared by BOTH the one-shot fix_error and the agentic fix_error_agentic so the
    two paths reason over identical context and can never drift."""
    context_section = ""
    variables = context.get("variables", [])
    data_sources = context.get("data_sources")
    cells = context.get("cells", [])

    if variables:
        context_section += f"\n<available_variables>\n{', '.join(variables[:30])}\n</available_variables>\n"

    # Full multi-frame traceback (library-internal frames pruned): lets the model see
    # the actual call chain + exception type/line, not just the one-line message.
    error_traceback = (context.get("traceback") or "").strip()
    if error_traceback:
        context_section += f"\n<full_traceback>\n{error_traceback}\n</full_traceback>\n"

    # Local variable VALUES at the crash point (safe reprs). This is the biggest
    # signal for the fix — e.g. a KeyError is obvious once you see which keys the
    # failing dict/row actually had. Bounded server-side; render one per line.
    error_locals = context.get("error_locals")
    if isinstance(error_locals, dict) and error_locals:
        loc_lines = "\n".join(f"  {k} = {v}" for k, v in list(error_locals.items())[:40])
        context_section += (
            "\n<failing_frame_locals>\n"
            "# Local variable values captured AT the line that raised (use these to\n"
            "# pinpoint the fix — e.g. which key/column actually exists):\n"
            f"{loc_lines}\n</failing_frame_locals>\n"
        )

    if data_sources:
        # Render EVERY registered source type generically (the same pattern the chat
        # inventory uses), so a fix can reason over ALL sources — including ML MODEL
        # ARTIFACTS (.onnx/.joblib/model.tar.gz) and SAGEMAKER ENDPOINTS, whose schema
        # is surfaced exactly like a table/file: columns = the model's INPUT feature
        # columns, and the detail/size string = its output (task + classes). The old
        # renderer hardcoded s3/dynamodb/athena and dropped model/endpoint sources, so
        # the fixer never saw what to feed predict()/sm_predict() or what comes back.
        ds_lines = []
        columns_by_name = _extract_source_columns(data_sources)
        generic = data_sources.get("sources")
        if isinstance(generic, list) and generic:
            for s in generic[:20]:
                if not isinstance(s, dict):
                    continue
                name = s.get("display_name") or s.get("source_id") or ""
                if not name:
                    continue
                label = s.get("source_type") or "source"
                alt = ""
                if label == "athena" and s.get("source_id"):
                    # Athena: database.table (source_id), so a fix doesn't repeat a
                    # wrong-database mistake.
                    alt = name
                    name = s["source_id"]
                    if (s.get("table_format") or "").lower() == "iceberg":
                        name += " · Iceberg"
                detail = s.get("detail") or s.get("size") or ""   # size carries model task/classes
                line = f"  {label}: {name}" + (f" ({detail})" if detail else "")
                cols = columns_by_name.get(s.get("source_id") or name) or columns_by_name.get(alt or name)
                if cols:
                    shown = ", ".join(cols[:12])
                    if len(cols) > 12:
                        shown += f", +{len(cols) - 12} more"
                    # For a model/endpoint these columns ARE the input features to pass.
                    line += f" — columns: {shown}"
                ds_lines.append(line)
        else:
            # Legacy fallback: older frontend sends only the grouped s3/dynamodb/athena
            # arrays (no generic `sources`). Keep rendering those so nothing regresses.
            for f in (data_sources.get("s3") or [])[:5]:
                ds_lines.append(f"  s3: {f.get('uri') or f.get('key', '')}")
            for t in (data_sources.get("dynamodb") or [])[:5]:
                ds_lines.append(f"  dynamodb: {t.get('name', '')}")
            for t in (data_sources.get("athena") or [])[:5]:
                db = t.get("database", "")
                cols = t.get("columns", [])
                col_names = ", ".join(c.get("name", "") for c in cols[:8]) if cols and isinstance(cols[0], dict) else ""
                col_info = f" [{col_names}]" if col_names else ""
                ds_lines.append(f"  athena: {db}.{t.get('name', '')}{col_info}")
        if ds_lines:
            context_section += "\n<available_data_sources>\n" + "\n".join(ds_lines) + "\n</available_data_sources>\n"

    if cells:
        prev_code = "\n---\n".join(c.get("code", "")[:200] for c in cells[-5:])
        context_section += f"\n<previous_cells>\n{prev_code}\n</previous_cells>\n"

    return context_section


def _prefetch_live_variables(session_id: str, proxy_url: str) -> str:
    """Pull the LIVE variable inventory (name → type, shape, and preview) from the
    connected MicroVM once, up front, so the fix agent's FIRST candidate can
    reference real DataFrame columns without spending a verification round-trip
    probing `ts.dtypes` / `ts.columns`. Best-effort: on any failure return "" and
    the agent simply falls back to probing with its tools.

    This is the single biggest lever for cutting attempts: the /ai/fix context block
    otherwise carries only variable NAMES, so the model can't know which column of a
    DataFrame holds the numeric series it needs until it inspects — which is exactly
    the probe(s) we watched burn turns."""
    if not session_id:
        return ""
    try:
        import httpx
        from .tools.execution_tools import fetch_schema_columns, orphan_marker
        # Server-to-server calls back into the proxy (also used by the schema fetch
        # below): gated by X-Proxy-Auth, so attach the shared secret when set.
        headers = {"X-Session-Id": session_id}
        token = os.environ.get("PROXY_AUTH_TOKEN")
        if token:
            headers["X-Proxy-Auth"] = token
        with httpx.Client(timeout=10.0) as client:
            # /variables/redacted masks configured secret values; older images 404 it.
            resp = client.get(f"{proxy_url}/proxy/variables/redacted", headers=headers)
            if resp.status_code == 404:
                resp = client.get(f"{proxy_url}/proxy/variables", headers=headers)
        if resp.status_code != 200:
            return ""
        variables = (resp.json() or {}).get("variables", {})
        if not variables:
            return ""
        # Real column names + dtypes (GET /variables/schema, best-effort, 3 s).
        columns = fetch_schema_columns(proxy_url, headers)
        lines = []
        for name, info in list(variables.items())[:30]:
            type_name = info.get("type", "unknown")
            shape = info.get("shape", "")
            preview = (info.get("preview") or info.get("value") or "")[:300]
            line = f"  {name}: {type_name}"
            if shape:
                line += f" {shape}"
            if preview:
                line += f" = {preview}"
            if columns.get(name):
                line += f" — columns: {columns[name]}"
            lines.append(line + orphan_marker(info))
        if not lines:
            return ""
        if columns:
            note = (
                "# LIVE values from the connected kernel (name: type shape = preview).\n"
                "# Tabular variables list their LIVE columns after \"— columns:\"; use exactly\n"
                "# those names (they may differ from the data source's raw columns) in your\n"
                "# FIRST candidate (e.g. df['visitors']); do NOT waste a verification run\n"
                "# probing dtypes/columns you can already see here.\n"
            )
        else:
            note = (
                "# LIVE values from the connected kernel (name: type shape = preview).\n"
                "# A preview is a short repr and may NOT show every column.\n"
            )
        return "\n<live_variable_state>\n" + note + "\n".join(lines) + "\n</live_variable_state>\n"
    except Exception:
        return ""


# ── Fix verification verdict ────────────────────────────────────────────────────
# Reason codes returned as `verify_reason` when `verified` is False.
VERIFY_REASON_NO_RUN = "no_run"                  # execute_code never ran
VERIFY_REASON_LAST_RUN_FAILED = "last_run_failed"  # the last execute_code run errored
VERIFY_REASON_CODE_MISMATCH = "code_mismatch"    # last run ok, but it ran DIFFERENT code


def normalize_code_for_verify(code: str) -> str:
    """Conservative normalization used ONLY to compare the code that last ran with
    the code being returned. It is NOT semantic equivalence — it only forgives
    differences that cannot change what Python executes:
      • line endings (CRLF / CR → LF)
      • trailing whitespace on each line
      • leading/trailing blank lines
    Everything else (indentation, comments, blank lines between statements, any
    token) must match exactly."""
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def compute_fix_verdict(runs: list, fixed_code: str) -> dict:
    """Decide whether a returned fix was PROVEN to run, from the recorded
    execute_code runs (see execution_tools.execution_run_record). Pure function.

    verified = there was ≥1 execute_code run AND the LAST run succeeded AND its code
    equals the returned code (after normalize_code_for_verify). Other tool calls
    (get_variables, get_available_data_sources, install_package) are not runs.

    Returns {"verified", "verify_reason", "verify_error", "attempts"}:
      verify_reason — None when verified, else no_run | last_run_failed | code_mismatch
      verify_error  — the last run's FULL error text for last_run_failed, else ""
      attempts      — number of execute_code runs
    """
    runs = list(runs or [])
    attempts = len(runs)
    if not runs:
        return {"verified": False, "verify_reason": VERIFY_REASON_NO_RUN,
                "verify_error": "", "attempts": 0}
    last = runs[-1]
    if not last.get("ok"):
        return {"verified": False, "verify_reason": VERIFY_REASON_LAST_RUN_FAILED,
                "verify_error": last.get("error") or "Unknown error", "attempts": attempts}
    if normalize_code_for_verify(last.get("code", "")) != normalize_code_for_verify(fixed_code):
        return {"verified": False, "verify_reason": VERIFY_REASON_CODE_MISMATCH,
                "verify_error": "", "attempts": attempts}
    return {"verified": True, "verify_reason": None, "verify_error": "", "attempts": attempts}


# Verdict for paths that never execute anything (one-shot fix: SQL / markdown / no VM).
_ONE_SHOT_VERDICT = {"verified": False, "verify_reason": VERIFY_REASON_NO_RUN,
                     "verify_error": "", "attempts": 0}


def fix_error_agentic(code: str, error: str, context: dict) -> dict:
    """
    AGENTIC fix: propose a correction, RUN it on the connected MicroVM, learn from
    the real failure, and iterate — returning a fix that has been PROVEN to run
    clean (instead of the one-shot model's unverified guess).

    The agent has execute_code + install_package + read tools. Its loop (encoded in
    FIX_VERIFY_DIRECTIVE): write candidate → execute_code → if it errors, read the
    real error and revise (install missing packages, correct the next broken line,
    check real variable/schema names) → re-run → stop when the cell runs clean or
    the FIX_VERIFY_MAX_ATTEMPTS cap is hit.

    Test runs execute in this fix's exploration scratch on the user's kernel
    (agent_scope "fix:<id>"): they read every user variable, but the names they
    create stay in the scratch, which is cleared once the verdict is in, so a
    verified fix leaves nothing in the user namespace — the fix becomes real only
    when the user applies it and runs the cell. In-place mutation of a user object
    still reaches it (the directive forbids it).

    Returns {"fixed_code", "explanation", "verified", "attempts",
    "verify_reason", "verify_error"} — see compute_fix_verdict. `verified` is True
    ONLY when the agent's LAST execute_code run succeeded AND ran the exact code
    being returned; `attempts` counts execute_code runs only.

    Final verification run (code_mismatch only): if the last run succeeded but the
    agent then returned DIFFERENT code (e.g. a cosmetic tweak after testing), and
    fewer than FIX_VERIFY_MAX_ATTEMPTS execute_code runs were made, we run the
    returned code ONCE more and re-judge. This executes exactly the cell the user is
    about to apply, in the same fix scratch the agent's own candidate runs already
    used, so it adds no new kind of side effect. No re-run for last_run_failed (the
    kernel just raised mid-cell) or when the budget is spent — those are reported.

    SQL cells (cell_type == "sql") use the same loop with SQL tools: execute_sql +
    get_variables + get_available_data_sources (no execute_code / install_package),
    FIX_SQL_ERROR_PROMPT + FIX_SQL_VERIFY_DIRECTIVE, and the final code_mismatch
    re-run goes through run_sql_recorded. execute_sql runs via the SAME VM route a
    real SQL cell uses (so `{param}` placeholders and engine routing behave the same)
    but as an isolated test run: no cell_id, origin="agent", scratch output variable
    — no DAG run / run-history / chart is recorded for the user's cell. The attempt
    cap, turn limit and re-run budget are the SAME FIX_VERIFY_MAX_ATTEMPTS as Python.
    COST NOTE: SQL test runs are REAL queries — Athena bills per data scanned, and
    Aurora / DynamoDB reads are billed too — so each verification attempt may cost
    money. The FIX_VERIFY_MAX_ATTEMPTS cap bounds that. The verifying run executes
    EXACTLY the returned SQL (never rewritten to add a LIMIT); the agent may use small
    LIMITed probes earlier, but those count as runs and cannot verify the fix.

    Callers that have NO connected VM should use the one-shot fix_error instead
    (there's nothing to execute against); the route handles that fallback.
    """
    from .prompts import (
        FIX_ERROR_PROMPT, FIX_VERIFY_DIRECTIVE,
        FIX_SQL_ERROR_PROMPT, FIX_SQL_VERIFY_DIRECTIVE,
    )
    from .constants import FIX_VERIFY_MAX_ATTEMPTS
    from .tools.execution_tools import (
        set_execution_context, execute_code, install_package, execute_sql,
        get_variables, get_available_data_sources,
        record_execute_runs, run_code_recorded, run_sql_recorded, clear_agent_scope,
    )

    cell_type = context.get("cell_type", "code")
    # Markdown has no execution error (and anything else unknown) — one-shot path.
    if cell_type not in ("code", "sql"):
        return fix_error(code, error, context)
    is_sql = cell_type == "sql"

    # Wire the per-invocation tool context (session_id / proxy_url / endpoint) onto
    # THIS async task so the tools' asyncio.to_thread workers inherit it (same seam
    # chat/intel use). Without a session there's nothing to execute against.
    session_id = context.get("session_id", "")
    if not session_id:
        logger.info("fix_error_agentic: no session_id — falling back to one-shot fix.")
        return fix_error(code, error, context)
    # The fix's exploration scratch: every test run (and the final code_mismatch
    # re-run) shares it, so test runs read the user's variables but never write
    # them. Cleared (best-effort) once the verdict is in, on every exit path.
    fix_scope = f"fix:{uuid.uuid4().hex[:12]}"
    fix_proxy_url = context.get("proxy_url", "http://localhost:8081")
    set_execution_context({
        "session_id": session_id,
        "proxy_url": fix_proxy_url,
        "microvm_id": context.get("microvm_id", ""),
        "microvm_endpoint": context.get("microvm_endpoint", ""),
        "agent_scope": fix_scope,
    })
    try:

        context_section = _build_fix_context_section(context)
        # Pre-fetch the LIVE variable state (columns/dtypes/shape) once, up front, so the
        # agent's first candidate can reference real DataFrame columns without burning a
        # verification round-trip on a dtypes/columns probe. Best-effort — appended only
        # when the kernel returns something.
        live_vars = _prefetch_live_variables(
            session_id, context.get("proxy_url", "http://localhost:8081")
        )
        # System prompt = the SAME fix prompt (already .format()-ed with {code}/{error})
        # + the verification protocol that turns it into a tool-using verify loop, +
        # the gathered context + the live variable state + the explicit attempt cap.
        if is_sql:
            base_prompt, directive, run_tool = FIX_SQL_ERROR_PROMPT, FIX_SQL_VERIFY_DIRECTIVE, "execute_sql"
            tools = [execute_sql, get_variables, get_available_data_sources]
            run_recorded = run_sql_recorded
            unit, lang = "query", "sql"
        else:
            base_prompt, directive, run_tool = FIX_ERROR_PROMPT, FIX_VERIFY_DIRECTIVE, "execute_code"
            tools = [execute_code, install_package, get_variables, get_available_data_sources]
            run_recorded = run_code_recorded
            unit, lang = "cell", "python"
        system_prompt = (
            base_prompt.format(code=code, error=error)
            + context_section
            + live_vars
            + directive
            + f"\n\nATTEMPT LIMIT: make at most {FIX_VERIFY_MAX_ATTEMPTS} {run_tool} "
              f"verification run(s). If the {unit} still errors after that, return your "
              f"best-reasoned fix and say in the EXPLANATION that it could not be fully "
              f"verified.\n"
        )

        tracer = AgentTraceCallbackHandler(log_prefix="[fix]")
        agent = Agent(
            model=get_model(),
            system_prompt=system_prompt,
            tools=tools,
            conversation_manager=SlidingWindowConversationManager(window_size=AGENT_CONVERSATION_WINDOW_SIZE),
            callback_handler=tracer,
        )

        # HARD backstop for the attempt cap, enforced by Strands (not just the prompt):
        # `limits={"turns": N}` stops the event loop after N model turns with
        # stop_reason="limit_turns" (no exception). Each verify iteration costs roughly
        # two turns (model proposes+calls tool → model reasons on the result), plus a
        # little headroom for install/inspect calls — so 2*attempts + 2 turns lets the
        # model actually reach FIX_VERIFY_MAX_ATTEMPTS execute_code runs before we cut it.
        max_turns = max(4, FIX_VERIFY_MAX_ATTEMPTS * 2 + 2)

        kickoff = (
            f"Fix the {unit} shown in your instructions. VERIFY it with {run_tool} before "
            f"answering, per the verification protocol. Return ONLY the final EXPLANATION "
            f"+ one ```{lang} block with the verified {unit}."
        )
        def _fallback(runs: list) -> dict:
            # One-shot fallback: its code was never executed. Judge it against whatever
            # the agent DID run (no re-run) — it can only verify if the agent's last
            # successful run happens to be byte-identical, which is the honest answer.
            out = fix_error(code, error, context)
            out.update(compute_fix_verdict(runs, out.get("fixed_code", "")))
            return out

        # Record every execute_code run THIS agent makes (per-invocation ContextVar list;
        # concurrent chat/intel agents don't share it).
        with record_execute_runs() as runs:
            try:
                result = agent(kickoff, limits={"turns": max_turns})
                raw = str(result).strip()
            except Exception as e:
                # Agent loop failed (model/tool error) — degrade to the one-shot fix so the
                # user still gets a suggestion rather than nothing.
                logger.warning(f"fix_error_agentic: agent run failed ({type(e).__name__}: {e}) — "
                               f"falling back to one-shot fix.")
                return _fallback(runs)

            fixed_code, explanation = _parse_fix_response(raw, cell_type)
            if not fixed_code:
                # Model returned no parseable code block — fall back rather than hand back
                # the transcript.
                logger.warning("fix_error_agentic: no code block in agent output — falling back to one-shot fix.")
                return _fallback(runs)

            verdict = compute_fix_verdict(runs, fixed_code)
            if (verdict["verify_reason"] == VERIFY_REASON_CODE_MISMATCH
                    and len(runs) < FIX_VERIFY_MAX_ATTEMPTS):
                # See docstring: one final run of EXACTLY the returned code, then re-judge.
                logger.info("fix_error_agentic: last successful run differs from the returned "
                            "code — running the returned code once to verify it.")
                run_recorded(fixed_code)
                verdict = compute_fix_verdict(runs, fixed_code)
            runs_snapshot = list(runs)

        last = runs_snapshot[-1] if runs_snapshot else {}
        logger.info(
            f"fix_error_agentic: done — {tracer.tool_count} tool call(s), "
            f"{verdict['attempts']} {run_tool} run(s), verified={verdict['verified']}"
            + (f", reason={verdict['verify_reason']}" if verdict["verify_reason"] else "")
            + (f", last_error_type={last.get('error_type')}, line={last.get('error_lineno')}"
               if verdict["verify_reason"] == VERIFY_REASON_LAST_RUN_FAILED else "")
        )
        return {
            "fixed_code": fixed_code,
            "explanation": explanation,
            **verdict,
        }
    finally:
        clear_agent_scope(fix_proxy_url, session_id, fix_scope)


def fix_error(code: str, error: str, context: dict) -> dict:
    """
    One-shot: fix a cell's error AND explain why it failed, in a SINGLE model call.
    Returns {"fixed_code": <corrected code>, "explanation": <plain-language why>,
    "verified": False, "verify_reason": "no_run", "verify_error": "", "attempts": 0}
    — nothing is executed here, so the fix is never reported as verified.
    Uses a direct model call (no agent loop) for speed. The explanation is read-only
    context shown alongside the fix diff — it is NOT persisted as a cell note.
    """
    from .prompts import FIX_ERROR_PROMPT, FIX_SQL_ERROR_PROMPT

    cell_type = context.get("cell_type", "code")
    if cell_type == "markdown":
        # markdown cells have no exec errors; nothing was executed → not verified.
        return {"fixed_code": code, "explanation": "", **_ONE_SHOT_VERDICT}

    context_section = _build_fix_context_section(context)

    if cell_type == "sql":
        prompt = FIX_SQL_ERROR_PROMPT.format(code=code, error=error) + context_section
    else:
        prompt = FIX_ERROR_PROMPT.format(code=code, error=error) + context_section

    client = _get_direct_client()

    # Temperature-bump-on-retry: the first fix is near-deterministic (0.1) for a
    # precise, reproducible correction. If the user re-clicks Fix on the SAME error
    # (attempt > 1), the prior fix didn't work — raise temperature so the model
    # EXPLORES a genuinely different fix instead of re-deriving the same one at 0.1.
    # Clamped so it stays sane even after many retries.
    attempt = context.get("attempt", 1) or 1
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 1
    temperature = min(0.1 + 0.3 * (attempt - 1), 0.9)  # 0.1, 0.4, 0.7, 0.9(cap)

    # build_inference_config keeps this escalation EXACTLY as-is on models that
    # support temperature (Sonnet 4.6: {maxTokens, temperature}). On models that
    # don't (Haiku / Sonnet 5.x) it drops the temperature kwarg entirely, so the
    # retry-escalation is a no-op there — acceptable: the user re-click still
    # re-runs the fix; we just can't raise temperature to force exploration.
    converse_kwargs = dict(
        modelId=require_model_id(AI_MODEL_ID),
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=build_inference_config(
            AI_MODEL_ID, max_tokens=FIX_MAX_TOKENS, temperature=temperature,
        ),
    )
    _thinking = build_additional_request_fields(AI_MODEL_ID, disable_thinking=True)
    if _thinking is not None:
        converse_kwargs["additionalModelRequestFields"] = _thinking
    response = client.converse(**converse_kwargs)

    # Guard against SILENT truncation: if the model hit the output-token cap, the
    # corrected code is cut off mid-statement — returning it would hand back
    # syntactically-broken code that looks like a valid fix in the diff. Detect the
    # cap and log it loudly (the caller still gets the partial text, but this makes
    # the root cause greppable instead of a mysterious "the fix broke my cell").
    stop_reason = response.get("stopReason", "")
    if stop_reason == "max_tokens":
        logger.warning(
            f"fix_error: response hit maxTokens ({FIX_MAX_TOKENS}) — corrected code was "
            f"TRUNCATED. Cell may be too large for a single-shot fix; consider raising "
            f"FIX_MAX_TOKENS or splitting the cell."
        )

    response_text = response["output"]["message"]["content"][0]["text"].strip()

    # The model returns "EXPLANATION: <prose>\n```lang\n<code>\n```". Split the two:
    # the explanation is the read-only "why", the fenced block is the corrected code.
    fixed_code, explanation = _parse_fix_response(response_text, cell_type)
    # The one-shot path never executes code, so it is never verified.
    return {"fixed_code": fixed_code, "explanation": explanation, **_ONE_SHOT_VERDICT}


def _parse_fix_response(text: str, cell_type: str) -> tuple[str, str]:
    """Split a fix response into (fixed_code, explanation).

    Expected shape: an "EXPLANATION: ..." line/paragraph followed by a fenced code
    block. Robust to the model omitting the label or the fence: falls back to
    treating the fenced block as code + everything before it as the explanation, and
    if there's no fence at all, treats the whole thing as code with no explanation
    (preserves the old behavior so a fix is never lost)."""
    import re
    explanation = ""
    # Pull the EXPLANATION: prose (up to the first code fence), if present.
    m = re.search(r"EXPLANATION:\s*(.*?)(?=\n```|\Z)", text, re.IGNORECASE | re.DOTALL)
    if m:
        explanation = m.group(1).strip()

    # Extract the first fenced code block (```lang ... ```), any language tag.
    # For SQL cells prefer an explicit ```sql block (so a stray non-SQL fence earlier
    # in the answer can't be taken as the query); otherwise the first fence.
    fence = None
    if cell_type == "sql":
        fence = re.search(r"```sql[ \t]*\n?(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence is None:
        fence = re.search(r"```[a-zA-Z0-9]*\n?(.*?)```", text, re.DOTALL)
    if fence:
        code = fence.group(1).strip()
        # If there was no EXPLANATION: label, treat the text BEFORE the fence as the
        # explanation (minus a stray leading label), so we still surface a "why".
        if not explanation:
            pre = text[:fence.start()].strip()
            explanation = re.sub(r"^EXPLANATION:\s*", "", pre, flags=re.IGNORECASE).strip()
        return code, explanation

    # No fence at all — the model returned bare code (older behavior). Strip a stray
    # EXPLANATION: label if it leaked in, and return the rest as code, no explanation.
    stripped = re.sub(r"^EXPLANATION:.*?\n", "", text, flags=re.IGNORECASE | re.DOTALL).strip() if text.upper().startswith("EXPLANATION:") else text
    return stripped, explanation


def suggest_shell_command(description: str, context: dict) -> str:
    """
    One-shot: convert natural language to a shell command.
    Uses a direct model call for speed (no agent loop).
    """
    from .prompts import TERMINAL_SUGGEST_PROMPT, TERMINAL_ENV_INFO

    cwd = context.get("cwd", "/tmp")
    packages = context.get("packages", [])
    files = context.get("files", [])
    terminal_history = context.get("terminal_history", "")

    # Build environment info with dynamic context
    env_info = list(TERMINAL_ENV_INFO)
    if files:
        env_info.append(f"Files in /tmp: {', '.join(files[:15])}")
    if packages:
        extra_pkgs = [p for p in packages[:10] if p not in ("pandas", "numpy", "matplotlib")]
        if extra_pkgs:
            env_info.append(f"User-installed packages: {', '.join(extra_pkgs)}")

    prompt = TERMINAL_SUGGEST_PROMPT.format(
        env_info=chr(10).join(env_info),
        cwd=cwd,
        description=description,
    )

    # Add terminal history if available (helps with contextual commands)
    if terminal_history.strip():
        # Truncate to last 1500 chars to stay within token budget
        history = terminal_history.strip()[-1500:]
        prompt += f"\n<recent_terminal_output>\n{history}\n</recent_terminal_output>\n"

    client = _get_direct_client()

    # Model-aware params (see build_inference_config / build_additional_request_fields):
    # Sonnet 4.6 → {maxTokens:150, temperature:0.1}, no thinking field (unchanged).
    # 5.x models → temperature dropped + verbose default reasoning suppressed.
    converse_kwargs = dict(
        modelId=require_model_id(AI_MODEL_ID),
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=build_inference_config(AI_MODEL_ID, max_tokens=150, temperature=0.1),
    )
    _thinking = build_additional_request_fields(AI_MODEL_ID, disable_thinking=True)
    if _thinking is not None:
        converse_kwargs["additionalModelRequestFields"] = _thinking
    response = client.converse(**converse_kwargs)

    command = response["output"]["message"]["content"][0]["text"].strip()

    # Clean markdown fences if model wraps it
    if command.startswith("```") and command.endswith("```"):
        lines = command.split("\n")
        command = "\n".join(lines[1:-1]).strip()
    if command.startswith("```bash"):
        command = command[7:]
    if command.startswith("```"):
        command = command[3:]
    if command.endswith("```"):
        command = command[:-3]

    # Force single line — replace newlines with semicolons or &&
    command = command.strip()
    if "\n" in command:
        lines = [l.strip() for l in command.split("\n") if l.strip()]
        command = " && ".join(lines)

    return command
