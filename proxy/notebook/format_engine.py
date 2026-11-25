"""
Inline notebook Format engine — PROXY-SIDE.

Part of: proxy.notebook

Formatting is a pure text→text transform: it needs no notebook kernel and no live
MicroVM, so it lives on the proxy (unlike LINT, which is namespace-aware and stays
on the VM). Consequences of this placement:
  • Format works even when a notebook's VM is suspended or terminated.
  • It can be iterated with a proxy restart — no MicroVM image rebuild.

Two cell kinds, each with the right tool (matching what the VM runs):
  • Python cells → ruff format (each cell independently — layout never crosses cell
    boundaries, so per-cell avoids ruff injecting blank lines at cell seams).
  • SQL cells   → sqlglot pretty-print (duckdb dialect, matching the notebook's SQL
    engine). {name} placeholders are masked first so sqlglot can't rewrite them.

All functions here are PURE (no I/O beyond the ruff subprocess, no shared state), so
they're easy to test and safe to call per-request. ruff/sqlglot are proxy-host deps
(proxy/requirements.txt). ruff is run as the wheel's native binary located via
ruff's find_ruff_bin() (no interpreter startup per cell), falling back to
`sys.executable -m ruff` (the proxy's Python) if the binary can't be found.
"""

import re
import sys
import logging
import functools
import subprocess

logger = logging.getLogger(__name__)

# ruff/sqlglot are fast; a small ceiling just bounds a hang.
_RUFF_TIMEOUT_S = 30

# SQL dialect — matches the notebook's DuckDB-based SQL engine so valid DuckDB
# syntax (read_csv(...), dynamodb."tbl", …) is never mis-parsed or mangled.
_SQL_DIALECT = "duckdb"

# Matches a leading `UNLOAD (` (after optional whitespace / leading comments). Used
# to format the INNER query of an UNLOAD while leaving the Athena wrapper verbatim,
# since no sqlglot dialect pretty-prints UNLOAD itself.
# Both comment styles are handled: `-- line` AND `/* block */`. This matters because
# the formatter itself REWRITES `--` comments into `/* */` on pretty-print, so a cell
# formatted once comes back with block comments — the leading-comment skip must accept
# either (and the trailing newline after a line comment is optional, hence `\n?`).
_UNLOAD_HEAD_RE = re.compile(
    r"\A((?:\s|--[^\n]*\n?|/\*.*?\*/)*UNLOAD\s*\()", re.IGNORECASE | re.DOTALL)


def _split_unload(sql):
    """Split `UNLOAD ( <query> ) TO '…' WITH (…)` into (head, body, tail) by matching
    the OUTERMOST parens with a balanced-paren scan (string-literal aware, so a ')'
    inside a '…' literal doesn't close early). Returns None if `sql` isn't a leading
    UNLOAD. head = everything up to and incl. the opening '('; body = the inner query;
    tail = the closing ')' and the trailing TO/WITH clause (left verbatim)."""
    m = _UNLOAD_HEAD_RE.match(sql)
    if not m:
        return None
    start = m.end()          # index just AFTER the opening '('
    depth = 1
    in_str = False
    i = start
    while i < len(sql):
        ch = sql[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                head = sql[:start].rstrip()
                body = sql[start:i].strip()
                tail = sql[i:].strip()   # ') TO ... WITH (...)'
                return head, body, tail
        i += 1
    return None  # unbalanced — not a well-formed UNLOAD

# Notebook SQL cells use {name} placeholders (interpolated from namespace scalars by
# the VM's SQL engine at run time). These are NOT valid SQL: sqlglot parses a bare
# `{min_units}` as a struct/map literal and REGENERATES it as `{'min_units': min_units}`
# on format — silently corrupting the query. So we MASK each {name} to a plain-identifier
# sentinel `__param_<name>__` (which round-trips untouched) and UNMASK afterward.
_SQL_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")
_SQL_SENTINEL_RE = re.compile(r"__param_([A-Za-z_]\w*)__")


def _mask_sql_placeholders(sql: str) -> str:
    """{name} → __param_name__ so sqlglot treats it as a plain identifier."""
    return _SQL_PLACEHOLDER_RE.sub(lambda m: f"__param_{m.group(1)}__", sql)


def _unmask_sql_placeholders(sql: str) -> str:
    """__param_name__ → {name}, reversing _mask_sql_placeholders."""
    return _SQL_SENTINEL_RE.sub(lambda m: "{" + m.group(1) + "}", sql)


def _normalize_cells(cells) -> list[dict]:
    """Coerce the request's cell list into [{'id': str, 'code': str, 'type': str}]
    and drop malformed entries. Preserves order. `type` is 'sql' for SQL cells,
    else 'code' (Python)."""
    out = []
    if not isinstance(cells, list):
        return out
    for i, c in enumerate(cells):
        if not isinstance(c, dict):
            continue
        code = c.get("code", "")
        if not isinstance(code, str):
            continue
        cid = c.get("id")
        ctype = "sql" if c.get("type") == "sql" else "code"
        out.append({
            "id": str(cid) if cid is not None else str(i),
            "code": code,
            "type": ctype,
        })
    return out


@functools.lru_cache(maxsize=1)
def _ruff_cmd() -> tuple[str, ...]:
    """Command prefix for ruff. Prefer the ruff wheel's native binary directly
    (skips a Python interpreter start-up per cell); fall back to
    `sys.executable -m ruff` if the binary can't be located."""
    try:
        from ruff.__main__ import find_ruff_bin
        return (str(find_ruff_bin()),)
    except Exception:
        return (sys.executable, "-m", "ruff")


def _format_python(cid: str, code: str) -> str | None:
    """Format one Python cell with ruff. Returns the formatted code, or the ORIGINAL
    code on a syntax error / timeout / transient failure (so one bad cell never fails
    the batch). Returns None ONLY when ruff is missing entirely, which the caller
    treats as a whole-request failure."""
    try:
        proc = subprocess.run(
            [*_ruff_cmd(), "format", "--no-cache", "-"],
            input=code, capture_output=True, text=True,
            timeout=_RUFF_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning("  ✗ ruff format timed out on cell %s", cid)
        return code
    except FileNotFoundError:
        return None  # ruff missing → signal whole-request failure to caller
    except Exception as e:
        logger.warning("  ✗ ruff format error on cell %s: %s", cid, e)
        return code

    if proc.returncode == 0:
        new_code = proc.stdout
        # ruff format guarantees a trailing newline; notebook cells don't carry one
        # (the editor adds no final blank line), so strip exactly one trailing newline
        # to avoid growing a blank line on every format.
        if new_code.endswith("\n"):
            new_code = new_code[:-1]
        return new_code
    # rc != 0 → almost always a syntax error ruff won't format. Leave the cell as-is;
    # the lint pass (VM-side) surfaces the actual syntax diagnostic.
    return code


def _format_sql(code: str) -> str:
    """Pretty-print one SQL cell with sqlglot (DuckDB dialect). Returns the formatted
    SQL, or the ORIGINAL code unchanged if it can't be parsed or sqlglot is
    unavailable — so an in-progress/invalid query is never mangled and a missing dep
    degrades gracefully. {name} placeholders are masked so sqlglot can't rewrite them."""
    try:
        import sqlglot
    except ImportError:
        return code  # sqlglot missing → no-op

    # `UNLOAD (<query>) TO 's3://…' WITH (…)` is Athena syntax that NO sqlglot dialect
    # pretty-prints (duckdb AND athena both fall back to an opaque Command node and
    # echo the statement unchanged) — so a plain transpile silently no-ops on exactly
    # the big-data UNLOAD cells. Instead, format just the INNER query (a normal
    # SELECT/CTE sqlglot handles fine) and keep the UNLOAD wrapper + TO/WITH verbatim.
    unload = _split_unload(code)
    if unload is not None:
        head, body, tail = unload
        new_body = _format_sql_query(body, sqlglot)
        if new_body is None:
            return code  # inner query didn't parse → don't touch the cell
        # Re-indent the formatted body one level inside the UNLOAD ( … ).
        indented = "\n".join(("  " + ln if ln else ln) for ln in new_body.splitlines())
        return f"{head}\n{indented}\n{tail}"

    formatted = _format_sql_query(code, sqlglot)
    return code if formatted is None else formatted


def _format_sql_query(code: str, sqlglot) -> "str | None":
    """Pretty-print a plain (non-UNLOAD) SQL query with sqlglot/DuckDB. Returns the
    formatted SQL, or None if it can't be parsed (caller leaves the cell untouched).
    {name} placeholders are masked so sqlglot can't rewrite them."""
    try:
        masked = _mask_sql_placeholders(code)
        statements = sqlglot.transpile(
            masked, read=_SQL_DIALECT, write=_SQL_DIALECT, pretty=True,
        )
        if not statements:
            return None
        # transpile returns one formatted string per statement; join multi-statement
        # cells with a blank line so they stay visually separated.
        formatted = "\n\n".join(s for s in statements if s is not None)
        return _unmask_sql_placeholders(formatted)
    except Exception:
        return None  # ParseError (invalid SQL) or any other issue


def format_cells(cells) -> dict:
    """Format each cell INDEPENDENTLY (layout is purely local). Python → ruff, SQL →
    sqlglot.

    Input: ordered [{'id', 'code', 'type'}] cells. Output:
        {"success": bool, "formatted": {cell_id: formatted_code},
         "changed": {cell_id: bool}, "error": str}
    A cell that the formatter can't parse (syntax error) is returned UNCHANGED rather
    than failing the whole request — you can still format the cells that are valid."""
    norm = _normalize_cells(cells)
    if not norm:
        return {"success": True, "formatted": {}, "changed": {}}

    formatted: dict[str, str] = {}
    changed: dict[str, bool] = {}
    for cell in norm:
        cid, code = cell["id"], cell["code"]
        if not code.strip():
            formatted[cid] = code
            changed[cid] = False
            continue
        if cell["type"] == "sql":
            new_code = _format_sql(code)
        else:
            new_code = _format_python(cid, code)
            if new_code is None:
                # ruff isn't installed on the proxy — whole-request failure.
                return {"success": False, "error": "ruff is not installed on the proxy", "formatted": {}, "changed": {}}
        formatted[cid] = new_code
        changed[cid] = new_code != code
    return {"success": True, "formatted": formatted, "changed": changed}
