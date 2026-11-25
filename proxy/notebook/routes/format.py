"""
Notebook Format — proxy-native endpoint.

Part of: proxy.notebook (Notebook application layer)

Endpoint:
  POST /format  - Format notebook cells (Python via ruff, SQL via sqlglot)

This runs ON THE PROXY (not forwarded to a MicroVM): formatting is a pure text
transform that needs no kernel, so it works even when the notebook's VM is
suspended/terminated and requires no image rebuild to iterate. LINT stays VM-side
(it's namespace-aware). No session/VM is required — the request carries the cells.
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from proxy.notebook.format_engine import format_cells

logger = logging.getLogger(__name__)

router = APIRouter(tags=["format"])


@router.post("/format")
async def format_notebook(request: Request):
    """Format notebook cells with ruff (Python) / sqlglot (SQL), each cell
    independently.

    Body: {"cells": [{"id": <cell id>, "code": <str>, "type": "code"|"sql"}, ...]}.
    Response:
        {"success": bool, "formatted": {cell_id: <code>},
         "changed": {cell_id: bool}, "error": <str, on failure>}
    """
    body = await request.json()
    cells = body.get("cells", [])
    if not isinstance(cells, list):
        return JSONResponse(status_code=400, content={"error": "Expected {\"cells\": [{\"id\", \"code\", \"type\"}, ...]}"})

    # ruff runs as a subprocess per Python cell (and sqlglot is CPU-bound) — do the
    # whole batch in a worker thread so the event loop stays free.
    result = await asyncio.to_thread(format_cells, cells)
    if not result.get("success"):
        logger.warning(f"🎨 format failed: {result.get('error', '')[:200]}")
    else:
        _changed = sum(1 for v in result.get("changed", {}).values() if v)
        logger.info(f"🎨 format: {len(cells)} cell(s), {_changed} changed")
    return result
