"""
Python code execution engine.

Part of: app.notebook (application layer)

Endpoints:
  POST /execute         - Execute Python code in the persistent sandbox namespace
  POST /execute-batch   - Run a list of cells (code and SQL) in order, streamed
  POST /set-variable    - Bind a value in the namespace (parameter widgets)
  POST /set-env         - Set one environment variable on the running VM
  POST /refresh-secrets - Re-fetch the session's secrets into the environment
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .scratch import valid_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["code-engine"])


def _get_execute_lock(request: Request) -> asyncio.Lock:
    """Get or create the shared execution lock on app.state.
    Ensures only one code/SQL execution runs at a time on the single-threaded
    SandboxExecutor (which redirects sys.stdout — not safe for concurrent use)."""
    if not hasattr(request.app.state, '_execute_lock'):
        request.app.state._execute_lock = asyncio.Lock()
    return request.app.state._execute_lock


def _code_result_dict(result, executor) -> dict:
    """Shape a SandboxExecutor result into the /execute response dict. Single seam so
    /execute and the batch endpoint emit byte-identical per-cell payloads."""
    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "error_lineno": result.error_lineno,
        # Rich debug context (empty on success): cleaned multi-frame traceback text,
        # structured cell-local frames, and the failing frame's locals (safe reprs).
        "traceback": result.traceback,
        "error_frames": result.error_frames,
        "error_locals": result.error_locals,
        "html": result.html,
        "image": result.image,
        # Ordered rich outputs (text/table/plotly/image items), [] when none are
        # rich. Added next to the legacy output/html/image fields above.
        "outputs": getattr(result, "outputs", []),
        "variables_created": result.variables_created,
        "variables_read": result.variables_read,
        # Pre-existing vars changed in place (df['c'] = ..., inplace=True, .append).
        "variables_mutated": getattr(result, "variables_mutated", []),
        # Pre-existing vars this cell rebound (`x = ...`, `x += 1`) — a definition.
        "variables_rebound": getattr(result, "variables_rebound", []),
        "execution_time_ms": result.execution_time_ms,
        "peak_mem_mb": result.peak_mem_mb,
        "peak_cpu_pct": result.peak_cpu_pct,
        # [{kind, id}] data-source identifiers the cell's reader helpers touched.
        "sources_read": getattr(result, "sources_read", []),
        "execution_number": executor.get_stats()["execution_count"],
    }


def _execute_isolated(executor, code: str, cell_id, origin, agent_scope):
    """Worker-thread body of /execute: pick the scratch for `origin`, then run.
    (Resolving a scope takes the executor's _exec_lock, so it never runs on the
    event loop.)"""
    if origin not in ("agent", "internal"):
        return executor.execute(code, cell_id, capture_runtime=True)
    # One hold of the (re-entrant) lock across scope lookup + run, so a concurrent
    # clear cannot drop the scope in between.
    with executor._exec_lock:
        if origin == "agent" and valid_scope(agent_scope):
            scratch = executor.scratch_scope(agent_scope)
        else:
            scratch = {}
        return executor.execute(code, cell_id, capture_runtime=True, scratch=scratch)


@router.post("/execute")
async def execute_code(request: Request):
    """Execute Python code in the persistent sandbox namespace."""
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    session_state["request_count"] += 1

    body = await request.json()
    code = body.get("code", "")
    cell_id = body.get("cell_id")  # frontend cell id, recorded as variable provenance
    # origin marks WHO ran this: "agent" = AI-assistant exploration (execute_code
    # tool), "internal" = a system probe (schema discovery, profiler), else a user
    # cell. We stamp it into the log lines with an [agent] / [internal] tag so the
    # Logs panel can render a pill: agent and internal probe runs (and their errors)
    # are then visibly distinct from user-run cells. Both run in a scratch, so their
    # created names are logged as scratch_vars=, not vars=. Plain == comparisons: a
    # non-str JSON origin (e.g. a list) must not raise.
    origin = body.get("origin", "")
    origin_tag = "[agent] " if origin == "agent" else "[internal] " if origin == "internal" else ""
    if not code.strip():
        return JSONResponse(status_code=400, content={"error": "No code provided. Send {\"code\": \"...\"}"})

    logger.info(f"▶ {origin_tag}Executing code (len={len(code)})")
    lock = _get_execute_lock(request)
    async with lock:
        # Isolation: an agent probe runs in its scope's scratch overlay (chained
        # across the turn's probes; a per-run throwaway when an older client sends
        # no valid agent_scope); an internal system probe ("internal": profiler,
        # schema discovery) runs in a throwaway scratch. Neither writes the user
        # namespace, provenance or run ledger. Anything else is a cell: unchanged.
        # capture_runtime: the runtime section {clock, vars, runs} is taken inside
        # the executor's lock at the end of the run (after the run ledger entry).
        result = await asyncio.to_thread(_execute_isolated, executor, code, cell_id,
                                         origin, body.get("agent_scope"))

    _log_execute_outcome(result, code, origin_tag, scratch=origin in ("agent", "internal"))
    d = _code_result_dict(result, executor)
    # Added here, never inside the shared seam: batch frames must not carry it.
    d["runtime"] = getattr(result, "runtime", None)
    return d


@router.post("/set-variable")
async def set_variable(request: Request):
    """Inject a variable into the live namespace of the RUNNING executor — no relaunch.

    Body: {"name": "<identifier>", "value": <json>, "cell_id"?: "<id>"}

    The value is any JSON-serializable literal; it lands in the namespace exactly as
    Python's json.loads would decode it (str/int/float/bool/None/list/dict). We grab
    the SAME execute lock /execute uses so the write can't race a running cell on the
    single-threaded executor. Provenance is stamped so the Variables panel shows an
    origin and reactive dataflow treats it like any other defined var."""
    executor = request.app.state.executor
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Missing 'name'"})
    if "value" not in body:
        return JSONResponse(status_code=400, content={"error": "Missing 'value'"})
    value = body.get("value")
    cell_id = body.get("cell_id")

    lock = _get_execute_lock(request)
    try:
        async with lock:
            await asyncio.to_thread(executor.set_variable, name, value, cell_id)
            # Worker thread: runtime_section() takes _exec_lock, which an off-lock
            # caller may hold — never block the event loop on it.
            runtime = await asyncio.to_thread(executor.runtime_section)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.warning(f"set-variable {name!r} failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    logger.info(f"  ⇢ set variable {name!r} ({type(value).__name__})")
    return {"success": True, "name": name, "runtime": runtime}


@router.post("/set-env")
async def set_env(request: Request):
    """Set an environment variable on the RUNNING VM — no relaunch.

    Body: {"key": "<ENV_NAME>", "value": "<string>"}

    Injected into os.environ so user cells reading os.environ[...] see it immediately.
    This is the live half of the persisted env/secret feature: the proxy separately
    stores the reference so it's re-injected on the next launch/restore. Values are
    NOT logged (env vars may hold sensitive data)."""
    import os
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse(status_code=400, content={"error": "Missing 'key'"})
    value = body.get("value", "")
    os.environ[key] = str(value)
    # Track it in the user-env source map so list_env()/list_secret() reflect a var
    # added to the RUNNING VM (the /run hook only sets the map at launch).
    try:
        from app.notebook.helpers.env import _record_user_env_key
        _record_user_env_key(key, "env")
    except Exception:
        pass
    logger.info(f"  ⇢ set env {key} = ****")
    return {"success": True, "key": key}


@router.post("/refresh-secrets")
async def refresh_secrets(request: Request):
    """Re-resolve Secrets Manager secrets on the RUNNING VM — no relaunch.

    Secrets are otherwise fetched only once, in the /run hook at launch/restore, so
    a value rotated in Secrets Manager afterward is NOT picked up until the VM is
    relaunched. This endpoint re-fetches the current values and overwrites os.environ
    in place, so the NEXT cell run (or Run All) sees the new values.

    Body: {"secrets": [{name?, arn, envVar, secretKey?}, ...]} — the SAME secret
    references the proxy persists per-notebook (the proxy forwards them here). Values
    are never logged or returned. Response:
        {"success": bool, "injected": [envVar, ...], "failed": [{envVar, error}, ...]}
    """
    from app.platform.hooks import inject_secrets_into_env

    body = await request.json()
    secrets = body.get("secrets", [])
    if not isinstance(secrets, list):
        return JSONResponse(status_code=400, content={"error": "Expected {\"secrets\": [ {arn, envVar, ...} ]}"})
    if not secrets:
        return {"success": True, "injected": [], "failed": []}

    # boto3 (Secrets Manager) is blocking — run off the event loop.
    result = await asyncio.to_thread(inject_secrets_into_env, secrets)
    # Track each injected secret in the user-env source map (tagged 'secret') so a
    # secret attached to the RUNNING VM shows up in list_secret() immediately.
    try:
        from app.notebook.helpers.env import _record_user_env_key
        for ev in result.get("injected", []):
            _record_user_env_key(ev, "secret")
    except Exception:
        pass
    logger.info(f"  🔑 refresh-secrets: {len(result['injected'])} injected, {len(result['failed'])} failed")
    return {"success": True, **result}


def _log_execute_outcome(result, code, origin_tag="", scratch=False):
    """Log the rich per-cell outcome line for a Python execution. Shared by
    /execute AND /execute-batch so a code cell run via Run All gets the SAME
    ✓ OK / ✗ ERROR line (with resources + error type) it gets when run alone —
    previously batch code cells were logged only on an uncaught raise.
    scratch=True (agent/internal runs) labels created names scratch_vars=: they
    landed in a scratch overlay, never in the user namespace."""
    # Code SIZE for context — NOT the code text. The user's cell code is their data
    # and must not be echoed to logs; a length gives enough operational signal
    # (alongside timing/vars/resources) without dumping the payload.
    code_len = len((code or "").strip())

    # Resource + data-flow context (already computed by the executor): peak RSS
    # high-water mark, best-effort peak CPU%, and how many variables the cell read.
    def _res_suffix(r):
        parts = []
        if getattr(r, "peak_mem_mb", 0):
            parts.append(f"peak_mem={r.peak_mem_mb:.0f}MB")
        if getattr(r, "peak_cpu_pct", 0):
            parts.append(f"peak_cpu={r.peak_cpu_pct:.0f}%")
        if getattr(r, "variables_read", None):
            parts.append(f"vars_read={len(r.variables_read)}")
        return (" " + " ".join(parts)) if parts else ""

    if result.success:
        vars_label = "scratch_vars" if scratch else "vars"
        vars_info = f" {vars_label}={result.variables_created}" if result.variables_created else ""
        logger.info(f"  ✓ {origin_tag}OK ({result.execution_time_ms:.0f}ms){vars_info}{_res_suffix(result)} (len={code_len})")
    else:
        # Surface the exception TYPE distinctly (first token of the error) so error
        # patterns are greppable across sessions, not buried in the message.
        err_type = ""
        try:
            em = (result.error or "").strip()
            if em and ":" in em.split("\n", 1)[0]:
                err_type = em.split("\n", 1)[0].split(":", 1)[0].strip()
        except Exception:
            err_type = ""
        type_tag = f" [{err_type}]" if err_type else ""
        logger.warning(f"  ✗ {origin_tag}ERROR ({result.execution_time_ms:.0f}ms){type_tag}{_res_suffix(result)} (len={code_len})")
        logger.warning(f"    {origin_tag}{result.error}")


@router.post("/execute-batch")
async def execute_batch(request: Request):
    """Run an ORDERED list of cells in ONE request, streaming one SSE frame per cell.

    This exists to eliminate the per-cell HTTP round-trip (~380ms each to the managed
    microVM endpoint). Run All / Run-from / Run-dependents send the whole ordered list
    here instead of one /execute call per cell — collapsing N round-trips into one.

    Semantics match running the cells one-by-one:
      - ONE executor.execute() (code) or run_sql_cell() (sql) PER cell, in order, under
        the SAME shared execution lock (held for the whole batch). So per-cell error,
        execution_time_ms, peak_mem_mb, peak_cpu_pct are all isolated per cell.
      - CONTINUE after a cell error (a failed cell does not stop the batch — matches
        Run All today; the client scrolls to the first error afterward).

    Request body: {"cells": [{"cell_id","type":"code"|"sql","code","output_variable"?}]}
    Response: text/event-stream, one frame per cell then a terminal done frame:
      data: {"type":"cell_result","cell_id":"...","result":{<same dict as /execute[-sql]>}}
      data: {"type":"runtime","runtime":{"clock","vars","runs"}}   (exactly one)
      data: {"type":"done","cells_run":N}
    Per-cell results never carry `runtime`; the single runtime frame is taken after
    the last cell (the proxy consumes it for the notebook DAG).
    """
    from .sql_engine import run_sql_cell

    body = await request.json()
    cells = body.get("cells", []) or []
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    lock = _get_execute_lock(request)

    logger.info(f"▶ Executing batch ({len(cells)} cells)")

    async def stream():
        # Hold the lock for the WHOLE batch: the run is atomic w.r.t. other executions,
        # exactly like a sequence of individually-locked /execute calls but without
        # releasing between cells (nothing else should interleave into a Run All).
        async with lock:
            run = 0
            for cell in cells:
                cid = cell.get("cell_id")
                ctype = (cell.get("type") or "code").lower()
                code = cell.get("code", "") or ""
                if ctype == "markdown" or not code.strip():
                    continue
                session_state["request_count"] += 1
                try:
                    if ctype == "sql":
                        result = await run_sql_cell(
                            request, code.strip(),
                            cell.get("output_variable") or "result", cid,
                        )
                    else:
                        exec_result = await asyncio.to_thread(executor.execute, code, cid)
                        # Same per-cell outcome line as a standalone /execute (SQL
                        # cells already log their own line via run_sql_cell).
                        _log_execute_outcome(exec_result, code)
                        result = _code_result_dict(exec_result, executor)
                except Exception as e:  # never let one cell kill the stream
                    logger.warning(f"  ✗ batch cell {cid} raised: {e}")
                    result = {"success": False, "output": None, "error": f"{type(e).__name__}: {e}",
                              "execution_time_ms": 0, "peak_mem_mb": 0, "peak_cpu_pct": 0}
                run += 1
                frame = {"type": "cell_result", "cell_id": cid, "result": result}
                yield f"data: {json.dumps(frame)}\n\n"
            # One runtime frame after the last cell_result, before done (taken in
            # a worker thread — runtime_section() may wait on _exec_lock).
            try:
                runtime = await asyncio.to_thread(executor.runtime_section)
                yield f"data: {json.dumps({'type': 'runtime', 'runtime': runtime})}\n\n"
            except Exception as e:
                logger.warning(f"  batch runtime section failed: {e}")
            yield f"data: {json.dumps({'type': 'done', 'cells_run': run})}\n\n"
        logger.info(f"  ✓ batch done ({run} cells run)")

    return StreamingResponse(stream(), media_type="text/event-stream")
