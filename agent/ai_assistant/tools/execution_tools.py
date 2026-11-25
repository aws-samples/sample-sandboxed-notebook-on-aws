"""
Tools for interacting with the MicroVM execution environment.

These tools make HTTP calls to the connected MicroVM via the proxy's
existing auth mechanism to execute code, inspect variables, etc.
"""

import os
import httpx
import contextvars
from strands import tool
from ai_assistant.constants import (
    CELL_CODE_MAX_CHARS, CELL_OUTPUT_MAX_CHARS, CELL_ERROR_MAX_CHARS,
    MARKDOWN_PREVIEW_MAX_CHARS, VARIABLE_PREVIEW_MAX_CHARS,
    HTTP_ERROR_BODY_MAX_CHARS, SOURCE_TYPE_LABELS,
)

# Thread-local context so concurrent agent sessions don't leak between each other
# Per-invocation execution context, stored in a contextvars.ContextVar.
#
# Why a ContextVar (not threading.local + a module-global fallback): Strands runs
# each sync tool via `asyncio.to_thread(tool_func, ...)`, which COPIES the calling
# task's contextvars.Context into the worker thread. So the context set here on the
# event-loop task (by chat/chat_stream, right before invoking the agent) propagates
# correctly into every tool call — while remaining ISOLATED per concurrent request,
# because each request's async task carries its own copied context.
#
# The previous design used threading.local (which does NOT survive the to_thread
# hop, since the worker is a different thread) plus a lock-guarded module-level
# _fallback_context that every tool read when the thread-local was empty. Once the
# agent serves requests concurrently as its own process, that shared global is a
# cross-session BLEED risk: two interleaved sessions would clobber/read each other's
# context. The ContextVar removes the shared global entirely — no fallback needed.
_execution_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "notebook_agent_execution_context", default={}
)


def set_execution_context(context: dict):
    """Set the per-invocation execution context. Call this on the async task that
    then invokes the agent (chat/chat_stream do). The value propagates into each
    tool's asyncio.to_thread worker via contextvars copy, and stays isolated per
    concurrent request."""
    _execution_context.set(context.copy())


def _get_context() -> dict:
    """Get the current execution context (empty dict if unset)."""
    return _execution_context.get()


def _get_headers() -> dict:
    """Build headers for MicroVM proxy requests using session_id."""
    ctx = _get_context()
    headers = {"Content-Type": "application/json"}
    if ctx.get("session_id"):
        headers["X-Session-Id"] = ctx["session_id"]
    # Server-to-server auth: the agent runs as its own process and calls back
    # into the proxy, so attach the per-run shared secret (X-Proxy-Auth) when
    # it's present in the env. Loopback-only + this header gate every proxy call.
    token = os.environ.get("PROXY_AUTH_TOKEN")
    if token:
        headers["X-Proxy-Auth"] = token
    return headers


def _auto_execute_enabled() -> bool:
    """Whether the AI assistant may run execute_code/install_package WITHOUT asking
    the user first. Gated by env AI_AGENT_AUTO_EXECUTE; DEFAULT OFF (confirm-before-
    execute).

    Consumed by the approval hook (tools/approval_hook.py), NOT by the tools
    themselves: when this returns False the ExecutionApprovalHook raises a native
    Strands interrupt BEFORE the gated tool runs, so the UI can ask the user; when
    it returns True the hook bypasses the gate and the tool runs normally. The tools
    below always perform their HTTP call when actually invoked (the gate lives in the
    hook, so an approved/trusted call executes for real and its result feeds back to
    the model)."""
    return os.environ.get("AI_AGENT_AUTO_EXECUTE", "false").lower() in ("1", "true", "yes")


# ── Exploration scratch + orphan markers (mirrors proxy/notebook/ai/tools/execution_tools.py;
# duplicated because the agent package must not import proxy code) ──

SCRATCH_CLEAR_TIMEOUT_S = 3.0
SCRATCH_LABEL = " [scratch: this exploration only]"
SCRATCH_SECTION_HEADER = "Exploration scratch (this turn only; not notebook variables):"


def clear_agent_scope(proxy_url: str, session_id: str, scope: str) -> bool:
    """Drop the VM's exploration scratch for `scope` (POST
    /proxy/variables/scratch/clear {scope}). Best-effort: 3 s timeout, never raises,
    logs nothing. Returns True only when the VM answered 200. The VM's TTL and LRU
    cap are the backstop when this call is lost."""
    if not session_id or not scope:
        return False
    try:
        headers = {"Content-Type": "application/json", "X-Session-Id": str(session_id)}
        token = os.environ.get("PROXY_AUTH_TOKEN")
        if token:
            headers["X-Proxy-Auth"] = token
        with httpx.Client(timeout=SCRATCH_CLEAR_TIMEOUT_S) as client:
            resp = client.post(f"{proxy_url}/proxy/variables/scratch/clear",
                               headers=headers, json={"scope": scope})
        return resp.status_code == 200
    except Exception:
        return False


def orphan_marker(info) -> str:
    """'' for a variable a cell produced, else the agent-facing marker for a variable
    with NO producing cell (VM payload fields `orphan` / `orphan_reason`)."""
    if not isinstance(info, dict) or not info.get("orphan"):
        return ""
    reason = info.get("orphan_reason")
    if reason == "agent":
        return " [NO PRODUCING CELL (left by an AI exploration) — do not reference in cell code]"
    if reason == "deleted cell":
        return " [NO PRODUCING CELL (its cell was deleted) — do not reference in cell code]"
    if reason == "no cell":
        return " [NO PRODUCING CELL (set outside any cell, by the UI or an API call) — do not reference in cell code]"
    return " [NO PRODUCING CELL — do not reference in cell code]"


def _fetch_scratch_variables(proxy_url: str, headers: dict, scope: str) -> dict:
    """{name: entry} of the scope's exploration scratch (POST /proxy/variables/scratch),
    or {} on any failure (older image 404, VM down). Never raises."""
    try:
        with httpx.Client(timeout=SCHEMA_FETCH_TIMEOUT_S) as client:
            resp = client.post(f"{proxy_url}/proxy/variables/scratch",
                               headers=headers, json={"scope": scope})
        if resp.status_code != 200:
            return {}
        data = resp.json()
        variables = data.get("variables") if isinstance(data, dict) else None
        return variables if isinstance(variables, dict) else {}
    except Exception:
        return {}


def _format_variable_line(name: str, info: dict) -> str:
    """'  name: type shape = preview' (get_variables line, no marker/columns)."""
    type_name = info.get("type", "unknown")
    shape = info.get("shape", "")
    preview = (info.get("preview", info.get("value", "")) or "")[:VARIABLE_PREVIEW_MAX_CHARS]
    line = f"  {name}: {type_name}"
    if shape:
        line += f" {shape}"
    if preview:
        line += f" = {preview}"
    return line


def _rendered_summary(data: dict) -> str:
    """What the notebook showed besides text, as counts only ("[2 tables, 1 chart
    rendered in notebook]"): never table contents. Images count as charts. An older
    VM (no `outputs`) with `html` keeps the one-table line; "" when nothing rich."""
    outs = data.get("outputs")
    if not isinstance(outs, list):
        return "[DataFrame table rendered in notebook]" if data.get("html") else ""
    tables = sum(1 for o in outs if isinstance(o, dict) and o.get("t") == "table")
    charts = sum(1 for o in outs if isinstance(o, dict) and o.get("t") in ("plotly", "image"))
    parts = []
    if tables:
        parts.append(f"{tables} table{'s' if tables != 1 else ''}")
    if charts:
        parts.append(f"{charts} chart{'s' if charts != 1 else ''}")
    return f"[{', '.join(parts)} rendered in notebook]" if parts else ""


@tool
def execute_code(code: str) -> str:
    """
    Execute Python code on the connected MicroVM and return the result.
    Use this to test code, verify fixes, or check data before suggesting changes to the user.

    The code runs in this turn's exploration scratch: it can read every notebook
    variable, but names it creates are kept only for later execute_code calls of
    THIS turn and are never notebook variables. Never mutate a user variable in
    place (no inplace=True, df['col'] = ..., .append/.update/.pop/del): that DOES
    change the user's object.

    IMPORTANT: When the user asks about files, ONLY look in /tmp/ for data files
    (.csv, .xlsx, .xls, .parquet, .json). Do NOT run 'ls /' or list system directories.
    Use: import glob; glob.glob('/tmp/*.csv') + glob.glob('/tmp/*.json') + glob.glob('/tmp/*.parquet')

    Args:
        code: Python code to execute.

    Returns:
        The execution output (stdout), or the error message if it failed.
    """
    # Confirm-before-execute is enforced by ExecutionApprovalHook (a
    # BeforeToolCallEvent hook) BEFORE this tool body runs — so by the time we're
    # here the call was approved (or auto-execute is ON). The tool is unconditional:
    # it always performs its proxy call, and the real result feeds back to the model.
    proxy_url = _get_context().get("proxy_url", "http://localhost:8081")
    headers = _get_headers()
    body = {"code": code, "origin": "agent"}
    scope = _get_context().get("agent_scope")
    if scope:
        body["agent_scope"] = scope

    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{proxy_url}/proxy/execute",
                headers=headers,
                # origin="agent" tags this as AI-agent exploration (not a user cell)
                # so the VM's /execute log line is marked and the Logs panel can show
                # an "agent" pill — the agent's explore/self-correct probes (e.g. a
                # KeyError it recovers from) are then visibly distinct from user runs.
                # It also makes the VM run it in the turn's scratch overlay
                # (agent_scope, sent only when the turn set one).
                json=body,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    output = data.get("output", "").strip()
                    rendered = _rendered_summary(data)
                    if rendered:
                        return f"Output:\n{output}\n\n{rendered}"
                    return output or "(no output)"
                else:
                    return f"Error: {data.get('error', 'Unknown error')}"
            else:
                return f"HTTP error {resp.status_code}: {resp.text[:HTTP_ERROR_BODY_MAX_CHARS]}"
    except Exception as e:
        return f"Execution failed: {str(e)}"


@tool
def get_variables() -> str:
    """
    Get all Python variables currently defined in the MicroVM namespace.
    Returns variable names, types, shapes, and preview values.
    Use this to understand what data the user has available.

    Returns:
        JSON-formatted list of variables with their types and previews.
    """
    proxy_url = _get_context().get("proxy_url", "http://localhost:8081")
    headers = _get_headers()

    try:
        with httpx.Client(timeout=30.0) as client:
            # /variables/redacted masks configured secret values (VM-side: only the
            # VM knows them). Older images 404 it → the plain /variables payload.
            resp = client.get(
                f"{proxy_url}/proxy/variables/redacted",
                headers=headers,
            )
            if resp.status_code == 404:
                resp = client.get(
                    f"{proxy_url}/proxy/variables",
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                variables = data.get("variables", {})
                scope = _get_context().get("agent_scope")
                scratch = _fetch_scratch_variables(proxy_url, headers, scope) if scope else {}
                if not variables and not scratch:
                    return "No variables defined yet."

                schema = fetch_schema_entries(proxy_url, headers) if variables else {}

                # Format for the agent
                lines = []
                for name, info in variables.items():
                    line = _format_variable_line(name, info)
                    cols = format_columns(schema.get(name), SCHEMA_BLOCK_MAX_COLS)
                    if cols:
                        line += f" — columns: {cols}"
                    lines.append(line + orphan_marker(info))
                out = ("Current variables:\n" + "\n".join(lines)) if lines else "No variables defined yet."
                if scratch:
                    out += "\n" + SCRATCH_SECTION_HEADER + "\n" + "\n".join(
                        _format_variable_line(n, i if isinstance(i, dict) else {}) + SCRATCH_LABEL
                        for n, i in scratch.items())
                return out
            else:
                return "Could not fetch variables."
    except Exception as e:
        return f"Failed to get variables: {str(e)}"


# ── Live variable schema (VM: GET /variables/schema, POST /variables/inspect) ──
# Read-only, approval-free: the VM inspects only pandas/numpy containers (never a
# user repr), omits sensitive-looking columns' values and redacts secret values.

SCHEMA_FETCH_TIMEOUT_S = 3.0
SCHEMA_BLOCK_MAX_COLS = 25         # columns per variable in get_variables / per-turn block
INSPECT_OUTPUT_MAX_CHARS = 4000    # whole inspect_variable tool result
INSPECT_VALUE_MAX_CHARS = 60       # per sample value (model input, not UI text)
_TABULAR_KINDS = ("dataframe", "series")


def fetch_schema_entries(proxy_url: str, headers: dict, timeout: float = SCHEMA_FETCH_TIMEOUT_S) -> dict:
    """{name: schema entry} from GET /proxy/variables/schema, or {} on ANY failure
    (VM down, older image without the route, timeout). Best-effort, never raises."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{proxy_url}/proxy/variables/schema", headers=headers)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        out = {}
        for e in (data.get("variables") or []) if isinstance(data, dict) else []:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                out[e["name"]] = e
        return out
    except Exception:
        return {}


def format_columns(entry, max_cols: int, hints: dict | None = None) -> str:
    """'c1:dtype, c2:dtype, +M more' for a tabular schema entry ('' otherwise).
    hints maps a column to its source column name (rename hint)."""
    if not isinstance(entry, dict) or entry.get("kind") not in _TABULAR_KINDS:
        return ""
    cols = [c for c in (entry.get("columns") or []) if isinstance(c, (list, tuple)) and len(c) == 2]
    if not cols:
        return ""
    parts = []
    for col, dtype in cols[:max_cols]:
        p = f"{col}:{dtype}"
        if hints and col in hints:
            p += f" (source column {hints[col]} is {col} here)"
        parts.append(p)
    try:
        extra = int(entry.get("more") or 0)
    except (TypeError, ValueError):
        extra = 0
    rest = max(0, len(cols) - max_cols) + extra
    if rest:
        parts.append(f"+{rest} more")
    return ", ".join(parts)


def format_variable_entry(entry: dict, max_cols: int = SCHEMA_BLOCK_MAX_COLS, hints: dict | None = None) -> str:
    """One line: 'reviews: DataFrame 80×12 — rating:float64, …, +M more'."""
    name = entry.get("name", "?")
    kind = entry.get("kind")
    shape = entry.get("shape") or []
    head = f"{name}: {entry.get('type', 'object')}"
    try:
        if kind == "dataframe" and len(shape) == 2:
            head += f" {int(shape[0])}×{int(shape[1])}"
        elif kind == "series" and shape:
            head += f" {int(shape[0])}"
        elif kind == "ndarray":
            head += " " + "×".join(str(int(x)) for x in shape)
            if entry.get("dtype"):
                head += f" {entry['dtype']}"
        elif entry.get("len") is not None:
            head += f" len {int(entry['len'])}"
    except (TypeError, ValueError):
        pass
    cols = format_columns(entry, max_cols, hints)
    line = f"{head} — {cols}" if cols else head
    return line + (SCRATCH_LABEL if entry.get("scratch") else orphan_marker(entry))


def _cut(v, n: int = INSPECT_VALUE_MAX_CHARS) -> str:
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


@tool
def inspect_variable(name: str) -> str:
    """
    Inspect ONE existing notebook variable's LIVE schema: type, shape, every column
    with its dtype, and up to 3 sample rows. Read-only and needs NO user approval —
    use it (not execute_code) to check a DataFrame's real column names before
    writing code that uses them.

    Args:
        name: The variable name, exactly as defined in the notebook (e.g. "reviews").

    Returns:
        A one-line schema plus sample rows, or a short message if unavailable.
    """
    if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
        return "Invalid variable name: pass a notebook variable's exact name (e.g. reviews)."
    # Session + auth come from the turn's execution context, never from the model.
    proxy_url = _get_context().get("proxy_url", "http://localhost:8081")
    headers = _get_headers()
    body = {"name": name, "sample_rows": 3}
    scope = _get_context().get("agent_scope")
    if scope:
        body["scope"] = scope  # the VM resolves this turn's scratch first
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                f"{proxy_url}/proxy/variables/inspect",
                headers=headers,
                json=body,
            )
        status = resp.status_code
        try:
            data = resp.json()
        except Exception:
            data = None
        if status == 404:
            if isinstance(data, dict) and data.get("error") == "variable not found":
                return f"Variable '{name}' is not defined."
            return ("inspect_variable is not available on this VM image; "
                    "use get_variables for names, types and previews.")
        if status != 200 or not isinstance(data, dict):
            err = data.get("error") if isinstance(data, dict) else None
            detail = str(err)[:HTTP_ERROR_BODY_MAX_CHARS] if err else f"HTTP {status}"
            return f"Could not inspect '{name}': {detail}"
        lines = [format_variable_entry(data, max_cols=60)]
        sample = data.get("sample") or []
        if sample:
            lines.append(f"Sample rows (first {len(sample)}):")
            for i, row in enumerate(sample):
                if isinstance(row, dict):
                    lines.append(f"  [{i}] " + ", ".join(f"{k}={_cut(v)}" for k, v in row.items()))
                else:
                    lines.append(f"  [{i}] {_cut(row)}")
        omitted = data.get("omitted_columns") or []
        if omitted:
            lines.append("Values omitted (sensitive column names): " + ", ".join(str(c) for c in omitted))
        if data.get("truncated"):
            lines.append("(output truncated for size)")
        out = "\n".join(lines)
        return out if len(out) <= INSPECT_OUTPUT_MAX_CHARS else out[: INSPECT_OUTPUT_MAX_CHARS - 1] + "…"
    except Exception as e:
        return f"Failed to inspect variable: {type(e).__name__}"


@tool
def get_notebook_state() -> str:
    """
    Get the full state of the current notebook: all cells with their code, outputs, and errors.
    Use this to understand what the user has done so far before making suggestions.

    Returns:
        Formatted summary of all notebook cells.
    """
    notebook_context = _get_context().get("notebook_cells", [])
    if not notebook_context:
        return "Notebook is empty (no cells)."

    lines = []
    # The web sends a WINDOW of cells (around the active cell + the cells that
    # define variables it reads), each tagged with its notebook position `index`.
    indexed = any(isinstance(c, dict) and "index" in c for c in notebook_context)
    if indexed:
        lines.append(f"(Showing {len(notebook_context)} cells; cell numbers are notebook "
                     "positions — gaps are cells omitted for size.)")
        lines.append("")
    code_num = 0
    for i, cell in enumerate(notebook_context):
        cell_type = cell.get("type", "code")
        code = (cell.get("code") or "").strip()
        output = cell.get("output", "")
        error = cell.get("error", "")
        pos = cell.get("index", i)
        active = " (active cell)" if cell.get("active") else ""

        if cell_type == "markdown":
            lines.append(f"[Cell {pos}]{active} Markdown: {code[:MARKDOWN_PREVIEW_MAX_CHARS]}")
        else:
            code_num += 1
            lines.append(f"[Cell {pos}]{active} Code:" if indexed
                         else f"[Cell {pos}]{active} Code [{code_num}]:")
            lines.append(f"  {code[:CELL_CODE_MAX_CHARS]}")
            if output:
                lines.append(f"  → Output: {output[:CELL_OUTPUT_MAX_CHARS]}")
            if error:
                lines.append(f"  → ERROR: {error[:CELL_ERROR_MAX_CHARS]}")
        lines.append("")

    return "\n".join(lines)


@tool
def install_package(package_name: str) -> str:
    """
    Install a Python package on the connected MicroVM using pip.
    Use this when the user needs a library that isn't already installed.

    Args:
        package_name: The pip package name to install (e.g. "scikit-learn", "prophet", "seaborn").

    Returns:
        Success or failure message from pip install.
    """
    # Confirm-before-execute is enforced by ExecutionApprovalHook BEFORE this body
    # runs (see execute_code). The install is unconditional here: when it's reached,
    # the user approved it (or auto-execute is ON), so it actually installs.
    proxy_url = _get_context().get("proxy_url", "http://localhost:8081")
    headers = _get_headers()

    try:
        # 180s > the VM's 120s pip limit, so its classified failure message
        # ("No prebuilt wheel for X on <arch>; …") reaches the model.
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(
                f"{proxy_url}/proxy/install",
                headers=headers,
                json={"package": package_name},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return f"Successfully installed {package_name}"
                else:
                    return f"Install failed: {data.get('error', 'Unknown error')}"
            else:
                return f"Install request failed: HTTP {resp.status_code}"
    except Exception as e:
        return f"Install failed: {str(e)}"


@tool
def promote_attachment(s3_uri: str) -> str:
    """
    Register a chat-attached TABULAR file (CSV, TSV, XLS, XLSX, Parquet, JSON) as a
    reusable data source for this notebook session, so it appears in the Data
    Sources panel and can be queried/joined like any other source in future turns.

    WHEN TO CALL THIS: only when the user wants to KEEP or REUSE an attached file —
    e.g. they say "add this to my data sources", "keep this file", or ask to run
    analysis joining the attachment with other data sources going forward. For a
    ONE-OFF question about an attachment ("what's in this file", "explain this
    chart"), do NOT promote — just read it directly by its S3 URI for that turn.

    Only tabular files can be promoted (images/PDF/txt cannot). The file is COPIED
    into the session's data-source area (the original attachment is unchanged).

    Args:
        s3_uri: The attachment's S3 URI, exactly as given in the attachment info
                for this turn (looks like s3://.../attachments/<session>/.../file.csv).

    Returns:
        A confirmation with the NEW data-source S3 URI to use in code, or an error
        message (e.g. if the file isn't tabular or isn't a chat attachment).
    """
    context = _get_context()
    session_id = context.get("session_id", "")
    proxy_url = context.get("proxy_url", "http://localhost:8081")
    if not session_id:
        return "Cannot promote: no active session."

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{proxy_url}/datasources/promote-attachment",
                headers=_get_headers(),
                json={"session_id": session_id, "s3_uri": s3_uri},
            )
        if resp.status_code == 200:
            data = resp.json()
            new_uri = data.get("s3_uri", "")
            name = data.get("name", "")
            return (
                f"Registered '{name}' as a data source for THIS notebook session "
                f"only. It appears in the Data Sources panel for this session and is "
                f"readable at {new_uri} — use that S3 URI in code (e.g. "
                f"read_s3_csv(\"{new_uri}\")). "
                f"IMPORTANT when telling the user: it is scoped to THIS session — do "
                f"NOT say it is permanent or available across other/any notebook "
                f"sessions. Phrase it as \"added to this session's Data Sources\"."
            )
        # Surface the proxy's validation/error message to the agent.
        err = resp.json().get("error", resp.text[:HTTP_ERROR_BODY_MAX_CHARS]) if resp.content else f"HTTP {resp.status_code}"
        return f"Could not promote the file: {err}"
    except Exception as e:
        return f"Promotion failed: {str(e)}"


@tool
def get_available_data_sources() -> str:
    """
    Get the list of available data sources with full schema information (columns, types, samples).
    Use this to understand what data the user can access and the exact column names/types plus the fully-qualified table identifier.
    Call this BEFORE generating code that references data sources — it gives you exact column names and the fully-qualified table identifier (database.table).

    Returns:
        Formatted list of data sources with column schemas where discovered.
    """
    import httpx

    context = _get_context()
    session_id = context.get("session_id", "")
    proxy_url = context.get("proxy_url", "http://localhost:8081")

    # Try fetching the full data catalog from the VM (has schema info). READ-ONLY
    # (refresh=false): the agent only needs to SEE the current sources, not trigger
    # a cloud re-discovery + push to the VM. A read-only read is fast and won't
    # contend with in-flight VM work (the resync push under a 5s timeout would
    # often just fail here).
    catalog = None
    if session_id:
        try:
            resp = httpx.get(
                f"{proxy_url}/datasources/catalog",
                headers=_get_headers(),
                params={"refresh": "false"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                catalog = resp.json()
        except Exception:
            pass

    # If we got the catalog with discovered schemas, use it
    if catalog and catalog.get("entries"):
        lines = []
        # Group by source type
        by_type = {}
        for entry in catalog["entries"]:
            st = entry["source_type"]
            if st not in by_type:
                by_type[st] = []
            by_type[st].append(entry)

        # Ordered (source_type, label) pairs from the LOCAL static map, plus any
        # types present in the catalog that aren't listed yet (future-proof). Uses
        # constants.SOURCE_TYPE_LABELS instead of importing the proxy data-source
        # registry, so this tool (and the whole chat agent) has NO proxy.* import and
        # runs standalone. The per-source COLUMN SCHEMA below still comes from the
        # catalog HTTP entries — only the label/order source changed.
        ordered_types = list(SOURCE_TYPE_LABELS)
        _known = {t for t, _ in ordered_types}
        for st in by_type:
            if st not in _known:
                ordered_types.append((st, st))

        for src_type, label in ordered_types:
            entries = by_type.get(src_type, [])
            if not entries:
                continue
            lines.append(f"{label}:")
            for entry in entries:
                name = entry.get("display_name", entry["source_id"])
                size = entry.get("size", "")
                row_count = entry.get("row_count")
                status = entry.get("status", "pending")
                size_info = f" ({size})" if size else ""
                row_info = f", {row_count} rows" if row_count else ""

                if status == "discovered" and entry.get("columns"):
                    cols = entry["columns"]
                    col_summary = ", ".join(f"{c['name']}:{c['dtype']}" for c in cols[:12])
                    if len(cols) > 12:
                        col_summary += f", ... ({len(cols)} total)"
                    lines.append(f"  - {entry['source_id']}{size_info}{row_info}")
                    lines.append(f"    Columns: [{col_summary}]")
                else:
                    lines.append(f"  - {entry['source_id']}{size_info}{row_info} [{status}]")
            lines.append("")

        return "\n".join(lines) if lines else "No data sources found."

    # Fallback: basic info from frontend-passed context (no schema)
    data_sources = context.get("data_sources")
    uploaded_files = context.get("uploaded_files", [])

    if not data_sources and not uploaded_files:
        return "No data source information available."

    lines = []

    if uploaded_files:
        lines.append("Local Data Files (in /tmp/ on this VM):")
        for f in uploaded_files:
            name = f.get('name', '')
            size = f.get('size', '')
            schema = f.get('schema', '')
            line = f"  - /tmp/{name} ({size})"
            if schema:
                line += f"\n    Columns: {schema}"
            lines.append(line)
        lines.append("")

    if data_sources:
        s3 = data_sources.get("s3", [])
        dynamo = data_sources.get("dynamodb", [])
        athena = data_sources.get("athena", [])

        if s3:
            lines.append("S3 Files:")
            for f in s3:
                lines.append(f"  - s3://{f.get('bucket', '')}/{f.get('key', '')} ({f.get('size', '')})")
            lines.append("")

        if dynamo:
            lines.append("DynamoDB Tables:")
            for t in dynamo:
                lines.append(f"  - {t.get('name', '')} ({t.get('item_count', 0)} items)")
            lines.append("")

        if athena:
            lines.append("Athena Tables:")
            for t in athena:
                cols = t.get('columns', [])
                if cols and isinstance(cols[0], dict):
                    col_names = [c.get('name', '') for c in cols[:10]]
                else:
                    col_names = cols[:10]
                col_info = f" — columns: {', '.join(col_names)}" if col_names else ""
                lines.append(f"  - {t.get('database', '')}.{t.get('name', '')} ({t.get('column_count', 0)} cols){col_info}")
            lines.append("")

    return "\n".join(lines) if lines else "No data sources found."
