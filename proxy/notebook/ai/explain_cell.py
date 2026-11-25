"""Orchestrator for POST /ai/explain-cell (no FastAPI, no boto: injectable).

``explain_cell(body, get_notebook=..., get_dag=..., converse=..., model_id=...)``
returns ``(status, payload)``. ``converse(system, user) -> str`` is a SYNC model
call (run off-loop here). Errors use ``{"error": <code>, "message": ...}``.
Logs carry ids and character counts only, never code or explanations.
"""
import asyncio
import logging
import re

from proxy.notebook.ai import constants as C
from proxy.notebook.ai.explain_context import build_context
from proxy.notebook.ai.explain_prompt import SYSTEM_PROMPT, build_user_prompt, build_retry_prompt
from proxy.notebook.ai.explain_sections import parse_model_json, normalize

logger = logging.getLogger(__name__)

# Module attribute so tests can shorten it.
EXPLAIN_TIMEOUT_S = C.EXPLAIN_TIMEOUT_S
MAX_CELL_ID_LEN = 256
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}


def _err(status, code, message):
    return status, {"error": code, "message": message}


def _is_throttle(exc):
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        if (resp.get("Error") or {}).get("Code") in _THROTTLE_CODES:
            return True
    return "Throttl" in type(exc).__name__


def _map_exception(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return _err(504, "timeout", "The explanation took too long. Please retry.")
    if _is_throttle(exc):
        return _err(429, "throttled", "The AI is busy right now. Try again in a moment.")
    return _err(502, "model_failed", "Couldn't generate an explanation.")


def _valid_ids(body):
    nid = body.get("notebook_id")
    if isinstance(nid, bool):
        return None
    if isinstance(nid, int):
        pass
    elif isinstance(nid, str) and nid.isdigit() and len(nid) <= 18:
        nid = int(nid)
    else:
        return None
    cid = body.get("cell_id")
    if not isinstance(cid, str) or not (1 <= len(cid) <= MAX_CELL_ID_LEN) or _CTRL.search(cid):
        return None
    return nid, cid


async def explain_cell(body, *, get_notebook, get_dag, converse, model_id):
    if not isinstance(body, dict):
        return _err(400, "invalid_request", "Request body must be a JSON object.")
    ids = _valid_ids(body)
    if ids is None:
        return _err(400, "invalid_request", "notebook_id and cell_id are required.")
    nid, cid = ids
    own = body.get("code")
    if own is not None and not isinstance(own, str):
        return _err(400, "invalid_request", "code must be a string.")

    nb = None
    try:
        nb = await get_notebook(nid)
    except Exception as e:
        logger.warning("explain-cell notebook read failed nb=%s: %s", nid, type(e).__name__)
    cells = (nb or {}).get("cells") or []
    saved = next((c for c in cells if str(c.get("id")) == cid), None)
    if saved is None and own is None:
        return _err(400, "invalid_request", "Cell not found in the saved notebook.")
    if saved is not None and (saved.get("type") or "code") == "markdown":
        return _err(400, "invalid_request", "Markdown cells cannot be explained.")
    code = own if own is not None else (saved.get("code") or "")
    if len(code) > C.EXPLAIN_MAX_CELL_CHARS:
        return _err(400, "cell_too_large",
                    f"This cell is too large to explain ({C.EXPLAIN_MAX_CELL_CHARS} characters at most).")
    if not code.strip():
        return _err(400, "invalid_request", "The cell is empty.")

    doc = None
    if nb is not None:
        try:
            doc = await get_dag(nid, cells)
        except Exception as e:
            logger.warning("explain-cell dag read failed nb=%s: %s", nid, type(e).__name__)
    ctx = build_context(cells, doc, cid, own_code=own, budget=C.EXPLAIN_UPSTREAM_CHAR_BUDGET)
    user = build_user_prompt(ctx)
    known = {u["label"] for u in ctx["upstream"]}
    logger.info("explain-cell nb=%s cell=%s code_chars=%d upstream=%d omitted=%d prompt_chars=%d",
                nid, cid, len(code), len(ctx["upstream"]), len(ctx["omitted_labels"]), len(user))

    async def attempt(prompt):
        text = await asyncio.to_thread(converse, SYSTEM_PROMPT, prompt)
        try:
            obj = parse_model_json(text)
        except ValueError as e:
            return None, [f"not valid JSON ({e})"]
        return normalize(obj, ctx["target"]["code"], known)

    async def run():
        res, probs = await attempt(user)
        if not probs:
            return res
        best = res if res and res["overview"] else None
        try:
            res2, probs2 = await attempt(build_retry_prompt(user, "; ".join(probs)))
        except Exception as e:
            if best is None:
                raise
            logger.warning("explain-cell retry failed nb=%s cell=%s: %s", nid, cid, type(e).__name__)
            return best
        if not probs2:
            return res2
        return res2 if res2 and res2["overview"] else best

    try:
        res = await asyncio.wait_for(run(), timeout=EXPLAIN_TIMEOUT_S)
    except Exception as e:
        logger.warning("explain-cell failed nb=%s cell=%s: %s", nid, cid, type(e).__name__)
        return _map_exception(e)
    if res is None or not res["overview"]:
        logger.warning("explain-cell unusable reply nb=%s cell=%s", nid, cid)
        return _err(502, "model_failed", "Couldn't generate an explanation.")
    return 200, {
        "overview": res["overview"],
        "sections": res["sections"],
        "depends_on": res["depends_on"],
        "notes": res["notes"],
        "model": model_id,
        "omitted_labels": ctx["omitted_labels"],
        "omitted_count": len(ctx["omitted_labels"]),
        "missing_inputs": bool(ctx["missing_inputs"]),
        "upstream_available": ctx["upstream_available"],
        "repaired": bool(res["repaired"] or res["fallback"]),
    }
