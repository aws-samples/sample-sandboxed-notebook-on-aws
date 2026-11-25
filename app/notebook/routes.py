"""
Sandbox utility routes — package management, variables, health, metrics, files.

Part of: app.notebook (application layer)

Endpoints:
  POST /install            - Install a pip package
  POST /lint               - Lint the whole notebook (per-cell diagnostics)
  GET  /variables          - List namespace variables
  GET  /variables/redacted - /variables with secret values masked (AI agents)
  GET  /variables/schema   - Live columns/dtypes of every variable (AI agents)
  POST /variables/inspect  - One variable's schema + safe sample rows (AI agents)
  POST /variables/scratch  - Names an agent exploration scope made (labelled scratch)
  POST /variables/scratch/clear - Drop an agent exploration scope
  GET  /variables/untracked- Variables no cell produces + internal probe leftovers
  POST /variables/delete   - Delete still-untracked variables (user-confirmed clean-up)
  POST /variable-detail    - Rich detail for one variable (schema + full table)
  POST /introspect         - Get attributes/methods of a variable (for autocomplete)
  POST /param-options      - Option values for a parameter widget from a variable
  GET  /helpers            - Helper-function metadata (autocomplete + Snippets)
  GET  /completions-catalog- Static builtins/library completion catalog (baked)
  POST /reset              - Clear namespace
  POST /interrupt          - Interrupt running execution
  GET  /health             - Health check + session stats
  GET  /metrics            - Real-time system metrics (CPU, memory, disk, network)
  POST /upload             - Upload and load a data file
  POST /delete-file        - Delete a /tmp file and refresh the data catalog
  GET  /files              - List uploaded files in /tmp/
  GET  /checkpoint-timings - Timing breakdown from last checkpoint save/restore
  GET  /packages           - User-installed packages
  GET  /data-catalog       - Merged data-source catalog (cloud + local files)
  POST /data-catalog/refresh-local - Re-scan local /tmp files
  POST /data-catalog/resync - Replace the cloud entries with a proxy-pushed set
Python execution (/execute, /execute-batch, /set-*) is in code_engine.py, SQL in
sql_engine.py.
"""

import os
import base64
import asyncio
import inspect
import logging
import glob

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.platform.errors import emit_error_metric

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sandbox"])


def _frame_to_table(frame) -> str:
    """Serialize a pandas DataFrame to the client's __df_table__ JSON payload.

    Single seam for the /variable-detail branches: every tabular variable
    (DataFrame, Series, ndarray, Index, flat-scalar sequence) is first coerced
    into a small DataFrame, then rendered by the same structured serializer the
    cell-output path uses — so the frontend's DataGrid renders variable data and
    cell data identically. Row/column caps + truncation metadata are handled by
    build_df_table_payload.
    """
    from .executor import build_df_table_payload
    return build_df_table_payload(frame)


def _column_labels(columns) -> list[str]:
    """JSON-safe display label per column, in order, duplicates kept.

    MultiIndex columns are flattened the same way as the cell-output table
    (SandboxExecutor._table_payload): ('a', 'x') -> 'a_x'. Any other label (int,
    tuple, None, Timestamp) is str()-ed. Labels are not made unique: callers
    address columns by position.
    """
    if getattr(columns, "nlevels", 1) > 1:
        return ['_'.join(str(c) for c in col).strip('_') for col in columns]
    return [str(c) for c in columns]


@router.post("/install")
async def install_package(request: Request):
    """Install or uninstall a Python package in this sandbox."""
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    session_state["request_count"] += 1

    body = await request.json()
    package = body.get("package", "")
    uninstall = body.get("uninstall", False)
    if not package.strip():
        return JSONResponse(status_code=400, content={"error": "No package specified. Send {\"package\": \"...\"}"})

    checkpoint_manager = request.app.state.checkpoint_manager
    # pip (subprocess) and the requirements.txt S3 flush are blocking — run them
    # in a worker thread so the event loop keeps serving other requests.
    if uninstall:
        logger.info(f"📦 Uninstalling package: {package}")
        return await asyncio.to_thread(_uninstall_package_blocking, checkpoint_manager, package)
    else:
        logger.info(f"📦 Installing package: {package}")
        return await asyncio.to_thread(_install_package_blocking, executor, checkpoint_manager, package)


def _uninstall_package_blocking(checkpoint_manager, package: str) -> dict:
    """pip uninstall + checkpoint package-list update (runs in a worker thread)."""
    import subprocess, sys
    from app.platform.pip_helper import pip_lock
    try:
        # One pip at a time on this VM (PipBusy is reported like any other error).
        with pip_lock(wait_s=40):
            result = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", package],
                capture_output=True, text=True, timeout=30
            )
        if result.returncode == 0:
            logger.info(f"  ✓ Uninstalled {package}")
            # Drop it from the checkpoint list so it doesn't reappear on restore.
            checkpoint_manager.record_package_uninstall(package)
            # Persist the updated list immediately (removes/rewrites requirements.txt).
            checkpoint_manager.flush_packages()
            return {"success": True, "output": result.stdout.strip()}
        else:
            logger.warning(f"  ✗ Uninstall failed: {package} — {result.stderr.strip()[:100]}")
            return {"success": False, "error": result.stderr.strip() or "Uninstall failed"}
    except Exception as e:
        logger.warning(f"  ✗ Uninstall error: {package} — {e}")
        return {"success": False, "error": str(e)}


def _install_package_blocking(executor, checkpoint_manager, package: str) -> dict:
    """pip install + checkpoint package-list update (runs in a worker thread)."""
    result = executor.install_package(package)

    # Track the installed package (pinned to the resolved version) for checkpoint.
    if result.success:
        spec = result.installed_spec or package
        logger.info(f"  ✓ Installed {spec}")
        checkpoint_manager.record_package_install(spec)
        # Persist the package list to S3 IMMEDIATELY (a few bytes), so it can't
        # be lost if a later namespace checkpoint is frozen mid-upload before it
        # reaches its (late) requirements.txt write step.
        checkpoint_manager.flush_packages()
    else:
        logger.warning(f"  ✗ Install failed: {package} — {result.error[:100]}")

    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "installed_spec": result.installed_spec,
        # Failure diagnostics (empty on success): pip_diagnostics kind, the raw
        # pip output for the Packages panel's Details expander, and the VM arch.
        "error_kind": result.install_error_kind,
        "details": result.install_details,
        "arch": result.install_arch,
    }


@router.post("/lint")
async def lint_notebook(request: Request):
    """Lint the WHOLE notebook and return per-cell diagnostics.

    Body: {"cells": [{"id": <cell id>, "code": <str>}, ...]} in notebook order.

    Cells are concatenated into one virtual module so ruff resolves names ACROSS
    cells (a `df` defined in cell 1 and used in cell 5 isn't flagged undefined);
    the executor remaps ruff's blob line numbers back to per-cell, cell-relative
    positions. Response:
        {"success": bool, "diagnostics": {cell_id: [{line, col, end_line, end_col,
         code, message, severity, fixable}, ...]}, "error": <str, on failure>}
    Every submitted cell gets an entry (empty list = clean), so the client can
    clear stale markers on a now-clean cell.
    """
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1

    body = await request.json()
    cells = body.get("cells", [])
    if not isinstance(cells, list):
        return JSONResponse(status_code=400, content={"error": "Expected {\"cells\": [{\"id\", \"code\"}, ...]}"})

    # ruff subprocess + namespace analysis is blocking — run off the event loop.
    result = await asyncio.to_thread(executor.lint_notebook, cells)
    if not result.get("success"):
        logger.warning(f"🔎 lint failed: {result.get('error', '')[:200]}")
    else:
        _n = sum(len(v) for v in result.get("diagnostics", {}).values())
        logger.info(f"🔎 lint: {len(cells)} cell(s), {_n} diagnostic(s)")
    return result


def _cell_ids(request: Request):
    """Cell ids of the proxy-pushed DAG document (None when unknown) — lets the
    variable payloads flag a variable whose producing cell was deleted."""
    store = getattr(request.app.state, "dag_store", None)
    if store is None:
        return None
    try:
        return store.cell_ids()
    except Exception:
        return None


@router.get("/variables")
async def list_variables(request: Request):
    """List all variables in the sandbox namespace."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    try:
        # repr()/getsizeof per variable can be slow for large objects — off-loop.
        variables = await asyncio.to_thread(executor.get_variables, False, _cell_ids(request))
        return {"variables": variables, "count": len(variables)}
    except Exception as e:
        import traceback
        logger.error(f"Failed to list variables: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": f"Failed to inspect variables: {str(e)}"})


# ── AI-agent variable views (read-only; see app/notebook/var_inspect.py) ─────
# Error bodies are fixed strings: never str(e) or a traceback (they could carry
# values). Only the exception TYPE is logged.
_INSPECT_TIMEOUT_S = 5.0


@router.get("/variables/redacted")
async def list_variables_redacted(request: Request):
    """The /variables payload with configured secret values masked — the AI agents'
    view. The Variables panel keeps using /variables (the user's own values)."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    try:
        variables = await asyncio.to_thread(executor.get_variables, True, _cell_ids(request))
        return {"variables": variables, "count": len(variables)}
    except Exception as e:
        logger.error(f"Failed to list redacted variables: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "Failed to inspect variables"})


@router.get("/variables/schema")
async def variables_schema(request: Request):
    """Live columns/dtypes/shape of every user variable (bounded, no user repr)."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(executor.get_variable_schemas, _cell_ids(request)),
            _INSPECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Variable schema timed out")
        return JSONResponse(status_code=504, content={"error": "inspection timed out"})
    except Exception as e:
        logger.error(f"Variable schema failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "inspection failed"})


@router.post("/variables/inspect")
async def variables_inspect(request: Request):
    """{name, sample_rows<=3} → one variable's schema + safe sample rows (<=4 KB)."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "invalid request"})
    try:
        status, result = await asyncio.wait_for(
            asyncio.to_thread(executor.inspect_variable, body.get("name"),
                              body.get("sample_rows", 3), body.get("scope"),
                              _cell_ids(request)),
            _INSPECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Variable inspect timed out")
        return JSONResponse(status_code=504, content={"error": "inspection timed out"})
    except Exception as e:
        logger.error(f"Variable inspect failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "inspection failed"})
    return JSONResponse(status_code=status, content=result)


# ── Agent exploration scratch (app/notebook/scratch.py) ──────────────────────

async def _json_dict(request: Request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@router.post("/variables/scratch")
async def variables_scratch(request: Request):
    """{scope} → the names that scope's agent probes made, redacted like
    /variables/redacted, each with "scratch": true. Never notebook variables."""
    from .scratch import valid_scope
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    body = await _json_dict(request)
    scope = body.get("scope") if body else None
    if not valid_scope(scope):
        return JSONResponse(status_code=400, content={"error": "invalid scope"})
    try:
        variables = await asyncio.wait_for(
            asyncio.to_thread(executor.scratch_variables, scope, True), _INSPECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Scratch listing timed out")
        return JSONResponse(status_code=504, content={"error": "inspection timed out"})
    except Exception as e:
        logger.error(f"Scratch listing failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "inspection failed"})
    return {"variables": variables, "count": len(variables)}


@router.post("/variables/scratch/clear")
async def variables_scratch_clear(request: Request):
    """{scope} → {cleared: bool}. Called by the proxy/agent at turn end."""
    from .scratch import valid_scope
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    body = await _json_dict(request)
    scope = body.get("scope") if body else None
    if not valid_scope(scope):
        return JSONResponse(status_code=400, content={"error": "invalid scope"})
    try:
        cleared = await asyncio.to_thread(executor.clear_scratch, scope)
    except Exception as e:
        logger.error(f"Scratch clear failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "clear failed"})
    return {"cleared": bool(cleared)}


# ── Untracked (orphan) variables: list + user-confirmed clean-up ─────────────

@router.get("/variables/untracked")
async def variables_untracked(request: Request):
    """{visible: [{name, reason}], internal: [names]} — variables no cell produces
    (visible) and `_`-prefixed system-probe leftovers (internal)."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(executor.untracked, _cell_ids(request)), _INSPECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Untracked listing timed out")
        return JSONResponse(status_code=504, content={"error": "inspection timed out"})
    except Exception as e:
        logger.error(f"Untracked listing failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "inspection failed"})


_DELETE_MAX_NAMES = 1000


@router.post("/variables/delete")
async def variables_delete(request: Request):
    """{names: [...]} → {deleted, skipped, runtime}. Deletes only names that are
    STILL untracked (a cell-produced name is skipped), under the execute lock."""
    from .code_engine import _get_execute_lock
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    body = await _json_dict(request)
    names = body.get("names") if body else None
    if (not isinstance(names, list) or len(names) > _DELETE_MAX_NAMES
            or not all(type(n) is str and 0 < len(n) <= 256 for n in names)):
        return JSONResponse(status_code=400, content={"error": "invalid names"})
    cell_ids = _cell_ids(request)
    try:
        async with _get_execute_lock(request):
            result = await asyncio.to_thread(executor.delete_untracked, names, cell_ids)
            # Worker thread: runtime_section() takes _exec_lock.
            runtime = await asyncio.to_thread(executor.runtime_section)
    except Exception as e:
        logger.error(f"Untracked delete failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"error": "delete failed"})
    if result["deleted"]:
        logger.info(f"🧹 Deleted {len(result['deleted'])} untracked variable(s)")
    return {"deleted": result["deleted"], "skipped": result["skipped"], "runtime": runtime}


_NO_STATIC_ATTR = object()


def _classify_static_attr(static_val) -> tuple[str, str]:
    """(type, detail) for an attribute found by inspect.getattr_static — never
    invokes a descriptor. Properties / cached_property / getset & member
    descriptors / other non-callable descriptors → ("property", "") (not
    evaluated); functions, static/class methods, builtin & method descriptors and
    other callables → ("method", "()"); a plain stored value is classified from
    that static value, exactly like the old getattr-based logic."""
    import functools
    if isinstance(static_val, (property, functools.cached_property)):
        return "property", ""
    if isinstance(static_val, (staticmethod, classmethod)):
        return "method", "()"
    if inspect.isgetsetdescriptor(static_val) or inspect.ismemberdescriptor(static_val):
        return "property", ""
    if callable(static_val):
        return "method", "()"
    if hasattr(type(static_val), "__get__"):
        # Some other (non-callable) descriptor — its value is computed on access.
        return "property", ""
    return "property", (type(static_val).__name__ if static_val is not None else "")


@router.post("/introspect")
async def introspect_variable(request: Request):
    """
    Get attributes/methods of a variable for autocomplete dot-completion.
    Request body: { "variable": "df", "partial": "hea" }
    Returns: { "completions": [{"name": "head", "type": "method", "detail": "(...)"}, ...] }
    """
    executor = request.app.state.executor
    body = await request.json()
    var_name = body.get("variable", "")
    partial = body.get("partial", "").lower()

    ns = executor._namespace
    if var_name not in ns:
        return {"completions": []}

    obj = ns[var_name]
    try:
        attrs = dir(obj)
    except Exception:
        return {"completions": []}

    completions = []
    for attr in attrs:
        # Skip dunder attributes unless user explicitly types __
        if attr.startswith("__") and not partial.startswith("__"):
            continue
        # Filter by partial match
        if partial and not attr.lower().startswith(partial):
            continue

        # Determine type (method vs property) WITHOUT evaluating properties: a
        # plain getattr() runs every @property / cached_property getter (e.g. a
        # DataFrame's .T / .values copies the whole frame) just to label it.
        # inspect.getattr_static looks the attribute up without invoking
        # descriptors; only a dynamic attribute (__getattr__, e.g. a DataFrame
        # column) falls back to the evaluating getattr path.
        try:
            try:
                static_val = inspect.getattr_static(obj, attr)
            except AttributeError:
                static_val = _NO_STATIC_ATTR
            if static_val is _NO_STATIC_ATTR:
                val = getattr(obj, attr, None)
                if callable(val):
                    comp_type = "method"
                    detail = "()"
                else:
                    comp_type = "property"
                    detail = type(val).__name__ if val is not None else ""
            else:
                comp_type, detail = _classify_static_attr(static_val)
        except Exception:
            comp_type = "property"
            detail = ""

        completions.append({"name": attr, "type": comp_type, "detail": detail})

        # Cap at 50 to avoid overwhelming the UI
        if len(completions) >= 50:
            break

    return {"completions": completions}


@router.get("/helpers")
async def list_helpers():
    """Structured metadata for the pre-loaded notebook helper functions
    (read_s3_csv, plot_bar, ...). Powers editor autocomplete + signature hints and
    the Snippets side panel, so both stay in lockstep with helpers.py. Introspection
    lives in completion_catalog (cached; the helper set is fixed at build time).

    Returns: { "helpers": [ {name, category, signature, params[], returns,
                             summary, doc}, ... ] }
    """
    from .completion_catalog import load_helpers
    return {"helpers": load_helpers()}


@router.get("/completions-catalog")
async def completions_catalog():
    """Static autocomplete catalog for builtins + pre-baked libraries (pandas,
    numpy, plotly, boto3, stdlib) — module-level functions and common class methods
    with signatures. Generated at IMAGE BUILD TIME (python -m
    app.notebook.completion_catalog) and baked into the image; here we just serve
    the cached JSON. Returns an empty-but-valid catalog if the file is absent (older
    image), so the editor degrades gracefully.

    Returns: { builtins:[...], modules:{alias:[...]}, classes:{name:[...]} }
    """
    from .completion_catalog import load_catalog
    return load_catalog()


@router.post("/variable-detail")
async def variable_detail(request: Request):
    """
    Lazy, on-demand rich detail for ONE variable (Variables panel viewer).
    Request body: { "name": "df" }

    For a DataFrame/Series: returns a column schema (name/dtype/null%/unique) plus a
    `table` payload (the structured __df_table__ JSON, capped at NOTEBOOK_MAX_DISPLAY_ROWS)
    that the UI renders in its DataGrid. For ndarray/Index/flat-scalar sequences: a
    `table` payload too. Nested containers return a `json_tree`; everything else a
    longer `text` repr.
    """
    from .executor import max_display_rows
    from .dtypes import normalize_dtype

    executor = request.app.state.executor
    body = await request.json()
    name = body.get("name", "")
    ns = executor._namespace
    if not name or name not in ns:
        return JSONResponse(status_code=404, content={"error": f"Variable not found: {name}"})

    obj = ns[name]

    # Schema stats / nunique / table serialization / repr are blocking for big
    # objects — build the response in a worker thread (404 lookup stays inline).
    def _build():
        try:
            max_rows = max_display_rows()
            type_name = type(obj).__name__
            module = getattr(type(obj), "__module__", "") or ""
            result = {"name": name, "type": type_name}

            def _is_flat_scalar_seq(seq):
                # True only when EVERY element is a genuine scalar (number, string,
                # bool, None, or a numpy/datetime scalar) — i.e. a 1-D sequence that
                # reads well as a single-column "value" table. We use a scalar
                # ALLOWLIST rather than a container denylist so a list of arbitrary
                # objects (e.g. concurrent.futures.Future, custom class instances)
                # is NOT force-rendered as a table showing each element's repr() —
                # that's misleading (looks tabular, and reprs like
                # "<Future ... returned float64>" read like real values). Such lists
                # fall through to the JSON-tree view instead. Empty seq → not a table.
                import itertools as _it
                import datetime as _dt
                import numbers as _numbers

                def _is_scalar(v):
                    if v is None or isinstance(v, (str, bytes, bool, _numbers.Number)):
                        return True
                    if isinstance(v, (_dt.date, _dt.datetime, _dt.time, _dt.timedelta)):
                        return True
                    # numpy scalars (np.int64, np.float64, np.bool_, np.str_, ...) —
                    # detected structurally so we don't hard-import numpy here.
                    if getattr(type(v), "__module__", "") == "numpy" and getattr(v, "ndim", None) == 0:
                        return True
                    return False

                saw_any = False
                for v in _it.islice(seq, 2000):
                    saw_any = True
                    if not _is_scalar(v):
                        return False
                return saw_any

            if "pandas" in module and type_name == "DataFrame":
                total_rows, total_cols = int(obj.shape[0]), int(obj.shape[1])
                result["total_rows"] = total_rows
                result["total_cols"] = total_cols
                # nunique can be expensive on very large frames — skip beyond a threshold.
                compute_unique = total_rows <= 100_000
                # Columns are read BY POSITION (iloc), never by label: a frame with
                # duplicate column names (concat/rename of frames sharing 'name') makes
                # obj[label] a DataFrame, so .dtype / nunique() fail. Each column is
                # summarised on its own, so one odd column degrades to defaults
                # instead of failing the whole request.
                labels = _column_labels(obj.columns)
                schema = []
                for i, label in enumerate(labels):
                    entry = {"column": label, "dtype": "", "display_dtype": "",
                             "null_count": 0, "null_pct": 0.0, "unique": None}
                    try:
                        s = obj.iloc[:, i]
                        entry["dtype"] = str(s.dtype)
                        entry["display_dtype"] = normalize_dtype(entry["dtype"])
                        nnull = int(s.isna().sum())
                        entry["null_count"] = nnull
                        entry["null_pct"] = round(nnull / total_rows * 100, 1) if total_rows else 0.0
                        if compute_unique:
                            try:
                                entry["unique"] = int(s.nunique(dropna=True))
                            except Exception:
                                pass  # e.g. unhashable cells (dicts/lists)
                    except Exception:
                        pass
                    schema.append(entry)
                result["schema"] = schema
                # Same labels in the grid as in the schema (MultiIndex flattened like
                # the cell-output table). Show the index when it's meaningful (named /
                # MultiIndex); allow_duplicates keeps it when its name is also a column.
                df_disp = obj
                try:
                    if getattr(obj.columns, "nlevels", 1) > 1:
                        df_disp = obj.copy(deep=False)
                        df_disp.columns = labels
                except Exception:
                    df_disp = obj
                try:
                    if obj.index.name or (hasattr(obj.index, "names") and any(n for n in obj.index.names if n)):
                        df_disp = df_disp.reset_index(allow_duplicates=True)
                except Exception:
                    pass
                result["table"] = _frame_to_table(df_disp)
                result["truncated"] = total_rows > max_rows

            elif "pandas" in module and type_name == "Series":
                total_rows = int(len(obj))
                result["total_rows"] = total_rows
                result["total_cols"] = 1
                try:
                    nnull = int(obj.isnull().sum())
                except Exception:
                    nnull = 0
                nunique = None
                if total_rows <= 100_000:
                    try:
                        nunique = int(obj.nunique(dropna=True))
                    except Exception:
                        pass  # e.g. unhashable cells (dicts/lists)
                result["schema"] = [{
                    "column": str(obj.name) if obj.name is not None else "value",
                    "dtype": str(obj.dtype),
                    "display_dtype": normalize_dtype(str(obj.dtype)),
                    "null_count": nnull,
                    "null_pct": round(nnull / total_rows * 100, 1) if total_rows else 0.0,
                    "unique": nunique,
                }]
                # Render as a clean 2-column table: the (named) index becomes a real
                # column and the values get a sensible column name — instead of pandas'
                # default "0" for an unnamed Series plus an awkward index-name header
                # (common for groupby().size() results indexed by e.g. product_id).
                _value_name = str(obj.name) if obj.name is not None else "value"
                # allow_duplicates: an index named like the Series (groupby('a')['a']
                # .count()) would otherwise raise "cannot insert a, already exists".
                _series_frame = obj.head(max_rows).rename(_value_name).reset_index(allow_duplicates=True)
                result["table"] = _frame_to_table(_series_frame)
                result["truncated"] = total_rows > max_rows

            elif "numpy" in module and hasattr(obj, "shape"):
                result["shape"] = str(obj.shape)
                result["dtype"] = str(getattr(obj, "dtype", ""))
                try:
                    import pandas as pd
                    if getattr(obj, "ndim", None) == 1:
                        result["table"] = _frame_to_table(pd.DataFrame(obj[:max_rows], columns=["value"]))
                        result["total_rows"] = int(obj.shape[0])
                        result["truncated"] = int(obj.shape[0]) > max_rows
                    elif getattr(obj, "ndim", None) == 2:
                        result["table"] = _frame_to_table(pd.DataFrame(obj[:max_rows]))
                        result["total_rows"] = int(obj.shape[0])
                        result["truncated"] = int(obj.shape[0]) > max_rows
                    else:
                        result["text"] = repr(obj)[:5000]
                except Exception:
                    result["text"] = repr(obj)[:5000]

            elif module.startswith("plotly") and type_name == "Figure":
                # Don't dump the huge figure spec — the chart is already in the cell output.
                try:
                    traces = list(getattr(obj, "data", []) or [])
                    kinds = ", ".join(sorted({(getattr(t, "type", None) or "?") for t in traces})) or "—"
                    summary = f"Plotly Figure · {len(traces)} trace(s) · {kinds}"
                    try:
                        title = (obj.layout.title.text or "") if (obj.layout and obj.layout.title) else ""
                    except Exception:
                        title = ""
                    if title:
                        summary += f"\nTitle: {title}"
                    result["text"] = summary + "\n\nThe chart is rendered in the cell's output."
                except Exception:
                    result["text"] = "Plotly Figure\n\nThe chart is rendered in the cell's output."

            elif module.startswith("matplotlib") and type_name == "Figure":
                result["text"] = "Matplotlib Figure\n\nThe chart is rendered in the cell's output."

            elif "pandas" in module and type_name.endswith("Index"):
                # Index types (Index, DatetimeIndex, RangeIndex, MultiIndex, ...) are
                # 1-D sequences — render as a compact table, not a wrapped repr.
                import pandas as pd
                total = int(len(obj))
                result["total_rows"] = total
                result["total_cols"] = 1
                try:
                    result["dtype"] = str(obj.dtype)
                except Exception:
                    pass
                _freq = getattr(obj, "freqstr", None)
                if _freq:
                    result["index_freq"] = _freq
                try:
                    if type_name == "MultiIndex":
                        _frame = obj[:max_rows].to_frame(index=False)
                        result["total_cols"] = int(_frame.shape[1])
                    else:
                        _col = str(obj.name) if obj.name is not None else "value"
                        _frame = pd.DataFrame({_col: obj[:max_rows]})
                    result["table"] = _frame_to_table(_frame)
                    result["truncated"] = total > max_rows
                except Exception:
                    result["text"] = repr(obj)[:5000]

            elif isinstance(obj, (set, frozenset, list, tuple)) and _is_flat_scalar_seq(obj):
                # Flat sequence of scalars → single-column sortable table (much nicer
                # than a wrapped brace/bracket repr). Sets are unordered, so sort them
                # for readable, stable display.
                import pandas as pd
                is_set = isinstance(obj, (set, frozenset))
                items = list(obj)
                total = len(items)
                if is_set:
                    try:
                        items = sorted(items)
                    except TypeError:
                        items = sorted(items, key=lambda v: str(v))  # mixed types
                    result["note"] = "Set is unordered — shown sorted."
                result["total_rows"] = total
                result["total_cols"] = 1
                result["table"] = _frame_to_table(pd.DataFrame({"value": items[:max_rows]}))
                result["truncated"] = total > max_rows

            elif isinstance(obj, (dict, list, tuple, set, frozenset)):
                # Nested containers → a bounded, JSON-safe structure for a collapsible
                # tree view on the frontend. Depth/element/string caps keep it cheap.
                def _to_json_safe(value, depth=0):
                    _MAX_DEPTH, _MAX_ITEMS, _MAX_STR = 6, 200, 500
                    if depth >= _MAX_DEPTH:
                        return "… (max depth)"
                    # concurrent.futures.Future: show the RESULT, not the pointer repr
                    # (e.g. "<Future at 0x... returned float64>"). Strictly guarded so
                    # we NEVER block: only unwrap a future that is already done. A
                    # pending future shows a short label; an errored one shows the
                    # error type. Duck-typed (done/result/exception) so we don't import
                    # concurrent.futures just for an isinstance check.
                    if (callable(getattr(value, "done", None))
                            and callable(getattr(value, "result", None))
                            and callable(getattr(value, "exception", None))):
                        try:
                            if not value.done():
                                return "<Future: pending>"
                            exc = value.exception(timeout=0)
                            if exc is not None:
                                return f"<Future: error: {type(exc).__name__}>"
                            return _to_json_safe(value.result(timeout=0), depth + 1)
                        except Exception:
                            # Any surprise (not actually a Future, timeout, etc.) —
                            # fall through to the generic handling below.
                            pass
                    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
                        return value
                    if isinstance(value, str):
                        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
                    if isinstance(value, dict):
                        out = {}
                        for i, (k, v) in enumerate(value.items()):
                            if i >= _MAX_ITEMS:
                                out["…"] = f"({len(value) - _MAX_ITEMS} more keys)"
                                break
                            out[str(k)] = _to_json_safe(v, depth + 1)
                        return out
                    if isinstance(value, (list, tuple, set, frozenset)):
                        seq = list(value)
                        out = []
                        for i, v in enumerate(seq):
                            if i >= _MAX_ITEMS:
                                out.append(f"… ({len(seq) - _MAX_ITEMS} more items)")
                                break
                            out.append(_to_json_safe(v, depth + 1))
                        return out
                    try:
                        return str(value)[:_MAX_STR]
                    except Exception:
                        return f"<{type(value).__name__}>"

                result["json_tree"] = _to_json_safe(obj)
                result["json_root_type"] = "object" if isinstance(obj, dict) else "array"

            else:
                try:
                    # A plain string is shown as its RAW value — NOT repr(). repr() wraps
                    # it in quotes and escapes newlines/tabs as literal "\n"/"\t", so a
                    # multi-line LLM answer rendered as one escaped blob. The modal shows
                    # `text` verbatim in a <pre>, so the raw value reads correctly
                    # (real line breaks). Cap length so a huge string can't bloat the
                    # payload. (bytes still uses repr — raw bytes aren't display-safe.)
                    if isinstance(obj, str):
                        result["text"] = obj[:100_000]
                    # NumPy scalars (np.int64(291), np.float64(1017.67), …) repr as the
                    # ugly "np.int64(291)". Show the plain Python value ("291") instead,
                    # matching the /variables preview. Detected structurally (0-d numpy)
                    # so we don't hard-import numpy here.
                    elif "numpy" in module and getattr(obj, "ndim", None) == 0:
                        result["text"] = repr(obj.item())[:5000]
                    else:
                        result["text"] = repr(obj)[:5000]
                except Exception:
                    result["text"] = f"<{type_name}>"

            return result
        except Exception as e:
            import traceback
            logger.error(f"variable-detail failed for {name}: {e}\n{traceback.format_exc()}")
            # The variable exists: degrade to its repr instead of a 500 that leaves
            # the panel with no schema and the viewer with "HTTP 500".
            try:
                text = repr(obj)[:5000]
            except Exception:
                text = f"<{type(obj).__name__}>"
            return {"name": name, "type": type(obj).__name__, "text": text,
                    "note": f"Could not build the full view ({type(e).__name__})."}

    return await asyncio.to_thread(_build)


# Max options a dropdown will accept from a variable — keeps the payload small and
# a giant accidental list (e.g. a 100k-row column) from bloating the widget.
_PARAM_OPTIONS_LIMIT = 1000


@router.post("/param-options")
async def param_options(request: Request):
    """
    Resolve a notebook variable into a dropdown's option list — the runtime half of
    an `@param {"type":"dropdown","optionsFrom":"<var>"}` widget.

    Request body: { "name": "<variable name>" }
    Response: { "name", "options": [{"label": str, "value": <scalar>}, ...],
                "truncated": bool }  OR  { "error": "..." } on a bad/missing var.

    The variable's SHAPE decides the mapping (all coerced server-side, where the
    real Python objects live — the /variables map only carries a truncated repr):
      • list of {"label","value"} dicts  → passed through (value kept as-is)
      • list of 2-tuples [(label,value)]  → label, value
      • flat list/tuple/set/Index/Series of scalars → {label:str(x), value:x}
      • dict                              → {label:str(k), value:v} per item
      • DataFrame                         → 'label'/'value' cols if present, else
                                            first two cols, else the single column
    value is always JSON-safe (numpy/pandas scalars are unwrapped to Python types).
    """
    executor = request.app.state.executor
    body = await request.json()
    name = body.get("name", "")
    ns = executor._namespace
    if not name or name not in ns:
        return JSONResponse(status_code=404, content={"error": f"Variable not found: {name}"})

    obj = ns[name]

    def _scalarize(v):
        """Coerce a numpy/pandas scalar (or anything) to a JSON-safe Python value."""
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        # numpy scalar → .item(); pandas NA/NaT → None; else str().
        try:
            if getattr(type(v), "__module__", "") == "numpy" and getattr(v, "ndim", None) == 0:
                return v.item()
        except Exception:
            pass
        try:
            import pandas as pd
            if v is pd.NA or (hasattr(pd, "isna") and pd.isna(v) is True):
                return None
        except Exception:
            pass
        return str(v)

    def _as_option(entry):
        """Map ONE source entry to {'label','value'} (or None to skip)."""
        # {"label","value"} dict.
        if isinstance(entry, dict):
            if "value" not in entry:
                return None
            val = _scalarize(entry["value"])
            lbl = entry.get("label", val)
            return {"label": str(lbl), "value": val}
        # (label, value) 2-tuple/list.
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            val = _scalarize(entry[1])
            return {"label": str(entry[0]), "value": val}
        # Bare scalar → label == value.
        val = _scalarize(entry)
        return {"label": str(val), "value": val}

    try:
        options = []
        truncated = False
        type_name = type(obj).__name__
        module = getattr(type(obj), "__module__", "") or ""

        if "pandas" in module and type_name == "DataFrame":
            # Columns by POSITION: with duplicate names row[label] is a Series.
            cols = [str(c) for c in obj.columns]
            if "label" in cols and "value" in cols:
                label_pos, value_pos = cols.index("label"), cols.index("value")
            elif len(cols) >= 2:
                label_pos, value_pos = 0, 1
            elif len(cols) == 1:
                label_pos = value_pos = 0
            else:
                return JSONResponse(status_code=400, content={"error": f"'{name}' has no columns"})
            n = len(obj)
            truncated = n > _PARAM_OPTIONS_LIMIT
            head = obj.head(_PARAM_OPTIONS_LIMIT)
            for lbl, v in zip(head.iloc[:, label_pos].tolist(), head.iloc[:, value_pos].tolist()):
                options.append({"label": str(lbl), "value": _scalarize(v)})

        elif "pandas" in module and type_name == "Series":
            n = len(obj)
            truncated = n > _PARAM_OPTIONS_LIMIT
            for v in obj.head(_PARAM_OPTIONS_LIMIT).tolist():
                options.append(_as_option(v))

        elif "pandas" in module and type_name.endswith("Index"):
            n = len(obj)
            truncated = n > _PARAM_OPTIONS_LIMIT
            for v in list(obj[:_PARAM_OPTIONS_LIMIT]):
                options.append(_as_option(v))

        elif isinstance(obj, dict):
            items = list(obj.items())
            truncated = len(items) > _PARAM_OPTIONS_LIMIT
            for k, v in items[:_PARAM_OPTIONS_LIMIT]:
                options.append({"label": str(k), "value": _scalarize(v)})

        elif isinstance(obj, (list, tuple, set, frozenset)):
            items = list(obj)
            truncated = len(items) > _PARAM_OPTIONS_LIMIT
            for entry in items[:_PARAM_OPTIONS_LIMIT]:
                opt = _as_option(entry)
                if opt is not None:
                    options.append(opt)

        else:
            return JSONResponse(status_code=400, content={
                "error": f"'{name}' (type {type_name}) can't be used as dropdown options — "
                         "provide a list, Series, Index, dict, or DataFrame."})

        # Drop any None-valued skips and enforce the cap defensively.
        options = [o for o in options if o is not None][:_PARAM_OPTIONS_LIMIT]
        return {"name": name, "options": options, "truncated": truncated}
    except Exception as e:
        import traceback
        logger.error(f"param-options failed for {name}: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/reset")
async def reset_sandbox(request: Request):
    """Clear the sandbox namespace."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1
    # Capture how much we're clearing BEFORE the reset, for context.
    try:
        _n = executor.get_stats().get("variables_count", "?")
    except Exception:
        _n = "?"
    # Worker thread: reset() clears the runtime state (provenance, clock, run
    # ledger) under _exec_lock, and runtime_section() takes it too — an off-lock
    # caller (/validate, import replay) may hold it, so never wait on the loop.
    await asyncio.to_thread(executor.reset)
    runtime = await asyncio.to_thread(executor.runtime_section)
    logger.info(f"🔄 Sandbox namespace reset — cleared {_n} variable(s)")
    return {"status": "reset", "message": "Namespace cleared", "runtime": runtime}


@router.post("/interrupt")
async def interrupt_execution(request: Request):
    """Interrupt a running code execution."""
    executor = request.app.state.executor
    interrupted = executor.interrupt()
    if interrupted:
        try:
            _n = executor.get_stats().get("execution_count", "?")
        except Exception:
            _n = "?"
        logger.info(f"⛔ Execution interrupted by user (after {_n} prior executions)")
        return {"status": "interrupted", "message": "Execution interrupted"}
    logger.info("⛔ Interrupt requested but nothing was running")
    return {"status": "idle", "message": "Nothing running to interrupt"}


@router.get("/health")
async def health(request: Request):
    """Health check with session metadata."""
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    stats = executor.get_stats()
    return {
        "status": "healthy",
        "microvm_id": session_state["microvm_id"],
        "session_id": session_state["session_id"],
        "started_at": session_state["started_at"],
        "request_count": session_state["request_count"],
        "execution_count": stats["execution_count"],
        "variables_count": stats["variables_count"],
        "suspend_count": session_state["suspend_count"],
        "resume_count": session_state["resume_count"],
    }


@router.get("/metrics")
async def metrics(request: Request):
    """Real-time system metrics for this MicroVM."""
    import psutil
    import time

    executor = request.app.state.executor
    cpu_percent = psutil.cpu_percent(interval=None)

    mem = psutil.virtual_memory()
    proc = psutil.Process()
    proc_mem = proc.memory_info()
    proc_mem_pct = (proc_mem.rss / mem.total * 100) if mem.total > 0 else 0
    effective_mem_pct = max(mem.percent, proc_mem_pct)

    try:
        disk = psutil.disk_usage('/tmp')
    except Exception:
        disk = None

    net = psutil.net_io_counters()
    proc_count = len(psutil.pids())
    uptime_sec = time.time() - psutil.boot_time()
    stats = executor.get_stats()

    return {
        "cpu": {"percent": round(cpu_percent, 1), "count": psutil.cpu_count() or 1},
        "memory": {
            "total_mb": round(mem.total / (1024 * 1024), 1),
            "used_mb": round(proc_mem.rss / (1024 * 1024), 1),
            "percent": round(effective_mem_pct, 1),
        },
        "disk": {
            "total_mb": round(disk.total / (1024 * 1024), 1) if disk else 0,
            "used_mb": round(disk.used / (1024 * 1024), 1) if disk else 0,
            "free_mb": round(disk.free / (1024 * 1024), 1) if disk else 0,
            "percent": round(disk.percent, 1) if disk else 0,
        },
        "network": {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv},
        "processes": proc_count,
        "uptime_sec": round(uptime_sec, 1),
        "executor": stats,
    }


_INVALID_BASE64 = object()


def _upload_blocking(executor, filename: str, data_b64: str, reader: str):
    """Blocking half of /upload (runs in a worker thread, under the execute lock):
    base64 decode, write to /tmp, then a throwaway pandas probe for the shape.
    Returns _INVALID_BASE64 for undecodable data, else the probe ExecutionResult.
    The probe runs in a throwaway scratch overlay: nothing it binds (the frame,
    `pd`) reaches the user namespace or the provenance."""
    # Decode and save
    try:
        file_bytes = base64.b64decode(data_b64)
    except Exception:
        return _INVALID_BASE64

    tmp_path = os.path.join("/tmp", filename)
    with open(tmp_path, "wb") as f:
        f.write(file_bytes)

    # Pass the path as a scratch variable (prevents code injection via the filename).
    path_var = f"__upload_path_{id(tmp_path)}"
    tmp_df_var = f"__upload_df_{id(tmp_path)}"

    logger.info(f"📄 Uploading {filename}")
    probe_code = (
        f"import pandas as pd\n"
        f"{tmp_df_var} = pd.{reader}({path_var})\n"
        f"print(f'{{{tmp_df_var}.shape}}')"
    )
    return executor.execute(probe_code, scratch={path_var: tmp_path})


@router.post("/upload")
async def upload_file(request: Request):
    """Upload a data file and load it into a pandas DataFrame."""
    executor = request.app.state.executor
    request.app.state.session_state["request_count"] += 1

    body = await request.json()
    filename = body.get("filename", "")
    data_b64 = body.get("data", "")
    var_name = body.get("variable_name", "")

    if not filename or not data_b64:
        return JSONResponse(status_code=400, content={"error": "Provide 'filename' and 'data' (base64-encoded)"})

    # SECURITY: Strip path components
    filename = os.path.basename(filename)
    if not filename:
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})

    # SECURITY: Validate extension
    ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".parquet", ".json"}
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return JSONResponse(status_code=400, content={"error": f"Unsupported file type: {ext}"})

    # Determine variable name
    if not var_name:
        stem = os.path.splitext(filename)[0]
        var_name = stem.replace(" ", "_").replace("-", "_").replace(".", "_")
        var_name = "".join(c for c in var_name if c.isalnum() or c == "_")
        if var_name and var_name[0].isdigit():
            var_name = "df_" + var_name

    if not var_name or not var_name.isidentifier():
        var_name = "df_upload"

    # Validate the file is loadable and capture its shape — but do NOT leave a
    # DataFrame behind in the namespace. Uploading a file only drops it in /tmp;
    # the user creates the actual variable later by clicking the file to insert a
    # read cell (which runs with a real cell id, so provenance resolves). Eagerly
    # binding `var_name` here would create an orphaned, provenance-less variable
    # (no owning cell) AND a duplicate in-memory copy of the data. So we read into
    # a throwaway temp var, read its shape, then delete it.
    reader = {
        ".csv": "read_csv", ".xlsx": "read_excel", ".xls": "read_excel",
        ".parquet": "read_parquet", ".json": "read_json",
    }.get(ext)
    if reader is None:
        return JSONResponse(status_code=400, content={"error": f"Unsupported: {ext}"})

    # Decode + write + probe are blocking (big payloads, pandas read) — run them in
    # a worker thread. Hold the SAME execute lock /execute uses: the probe runs
    # executor.execute(), which swaps sys.stdout and must never overlap another cell.
    from .code_engine import _get_execute_lock
    async with _get_execute_lock(request):
        result = await asyncio.to_thread(_upload_blocking, executor, filename, data_b64, reader)
    if result is _INVALID_BASE64:
        return JSONResponse(status_code=400, content={"error": "Invalid base64 data"})

    if result.success:
        shape = result.output.strip()
        logger.info(f"  ✓ Saved {filename} to /tmp {shape}")
        # Refresh data catalog with newly uploaded file
        request.app.state.data_catalog.refresh_local_files()
        return {
            "success": True,
            "variable_name": var_name,
            "filename": filename,
            "message": f"Saved '{filename}' to /tmp",
            "shape": shape,
        }
    logger.warning(f"  ✗ Upload failed: {filename} — {result.error[:100]}")
    return {"success": False, "error": result.error}


@router.post("/delete-file")
async def delete_file(request: Request):
    """Delete a local /tmp data file and RECONCILE the data catalog.

    Mirrors /upload's catalog handling: after removing the file we call
    refresh_local_files() so the deleted file is pruned from the catalog (and the
    catalog-ready callback re-persists it). Without this, deleting a file leaves a
    phantom entry that keeps surfacing in the Data Sources panel and the Workbook
    Intel report. Body: {"filename": "<name>"} — basename only (no path traversal).
    """
    body = await request.json()
    filename = os.path.basename(body.get("filename", ""))  # strip any path components
    if not filename:
        return JSONResponse(status_code=400, content={"error": "Provide 'filename'"})

    tmp_path = os.path.join("/tmp", filename)
    # Confirm the resolved path stays under /tmp (defense in depth vs. traversal).
    if os.path.realpath(tmp_path) != os.path.realpath(os.path.join("/tmp", filename)) \
            or not os.path.realpath(tmp_path).startswith(os.path.realpath("/tmp") + os.sep):
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})

    removed = False
    try:
        os.remove(tmp_path)
        removed = True
        logger.info(f"🗑️  Deleted /tmp/{filename}")
    except FileNotFoundError:
        pass  # already gone — still reconcile the catalog below (idempotent)
    except Exception as e:
        logger.warning(f"  ✗ Delete failed: {filename} — {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Reconcile the catalog so the deleted file is dropped (prune happens in
    # _discover_local_files). Safe/idempotent even if the file was already gone.
    request.app.state.data_catalog.refresh_local_files()
    return {"success": True, "filename": filename, "removed": removed}


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _enumerate_tmp_files(root: str = "/tmp") -> list[dict]:
    """Enumerate data files under `root` for the Sandbox Files panel, rolling up
    partition FOLDERS (a subdir of part-*/chunk-* files) into ONE row — the local
    analogue of the S3 parquet_prefix rollup — so the panel shows one dataset row
    with a directory/glob read snippet instead of one row per part.

    A partition-folder row keeps a trailing slash in `name` so the frontend builds
    source_id = "/tmp/<name>" ending in "/", which the proxy's local snippet
    functions detect as a prefix. Standalone files keep today's exact shape.
    `root` is injectable for offline testing only; prod passes nothing.
    """
    import datetime as _dt
    # Lazy import mirrors the checkpoint's lazy import of data_catalog (avoid any
    # import cycle through routes).
    from app.notebook.data_catalog import rollup_local_files

    extensions = ['*.csv', '*.xlsx', '*.xls', '*.parquet', '*.json', '*.txt']
    discovered: list[str] = []
    seen: set[str] = set()
    for ext in extensions:
        for filepath in glob.glob(f'{root}/**/{ext}', recursive=True):
            if filepath not in seen:
                seen.add(filepath)
                discovered.append(filepath)

    standalone, folders = rollup_local_files(discovered, root=root)
    files: list[dict] = []

    for filepath in standalone:
        rel_path = os.path.relpath(filepath, root)
        st = os.stat(filepath)
        size = st.st_size
        last_modified = _dt.datetime.fromtimestamp(
            st.st_mtime, tz=_dt.timezone.utc).isoformat()
        files.append({"name": rel_path, "path": filepath, "size": _human_size(size),
                      "size_bytes": size, "last_modified": last_modified})

    for folder in folders:
        # name/path keep a trailing slash so the frontend builds a prefix source_id
        # (/tmp/<dir>/) that the proxy snippet treats as a directory read.
        rel_dir = os.path.relpath(folder["dir"], root)
        last_modified = _dt.datetime.fromtimestamp(
            folder["mtime"], tz=_dt.timezone.utc).isoformat() if folder["mtime"] else None
        files.append({
            "name": f"{rel_dir}/", "path": folder["source_id"],
            "size": _human_size(folder["size_bytes"]),
            "size_bytes": folder["size_bytes"], "last_modified": last_modified,
            "is_prefix": True, "kind": "parquet_prefix",
        })

    files.sort(key=lambda f: f["name"])
    return files


@router.get("/files")
async def list_files():
    """List data files in /tmp/ (recursive, includes subdirectories).

    Partition folders (a /tmp subdir of part-*/chunk-* files, e.g. a to_local_prefix
    export) are collapsed into ONE row, mirroring the S3 parquet_prefix rollup."""
    return {"files": _enumerate_tmp_files()}


@router.get("/checkpoint-timings")
async def checkpoint_timings(request: Request):
    """Return timing + size breakdown from the last checkpoint save/restore.

    - last_save:         per-step timings (ms) of the last save
    - last_save_sizes:   per-step bytes written to S3 in the last save
    - last_save_summary: counts/size/timing summary of the last save
    - last_restore:      per-step timings (ms) of the last restore
    - last_checkpoint_at: unix ts of the last successful save (0 if never)
    """
    cm = request.app.state.checkpoint_manager
    return {
        "last_save": cm.last_save_timings,
        "last_save_sizes": getattr(cm, "last_save_sizes", {}),
        "last_save_summary": getattr(cm, "last_save_summary", {}),
        "last_restore": cm.last_restore_timings,
        "last_checkpoint_at": getattr(cm, "last_checkpoint_at", 0),
    }

@router.get("/packages")
async def list_packages():
    """
    List installed Python packages.

    Reads distribution metadata directly via importlib.metadata (stdlib, IN-PROCESS)
    — NOT `pip list`. Shelling out to pip spawned a subprocess that imported pip and
    walked every distribution; on the smallest VM tier (0.5 vCPU, cold FS cache,
    competing with boot-time discovery) that `pip list --format=json` routinely
    exceeded even a 30s timeout and raised TimeoutExpired, so the panel showed no
    packages. importlib.metadata reads the SAME on-disk metadata the resolver uses,
    in-process, in milliseconds — no subprocess, no pip import. Run off the event
    loop since it's a (fast but blocking) filesystem walk. The list is cached on a
    site-packages fingerprint and rebuilt on any install/uninstall
    (app/notebook/package_list.py).
    """
    import asyncio
    from app.notebook import package_list

    try:
        pkgs = await asyncio.to_thread(package_list.list_packages)
    except Exception as e:
        # Metadata read failed — don't hide it behind an empty list; log + metric.
        logger.exception("list_packages: importlib.metadata enumeration failed")
        emit_error_metric("sandbox", "list_packages", type(e).__name__, 500)
        pkgs = []

    pkg_list = [{"name": p["name"], "version": p["version"]} for p in pkgs]
    pkg_list.sort(key=lambda p: p["name"].lower())
    return {"packages": pkg_list, "count": len(pkg_list)}


@router.get("/data-catalog")
async def get_data_catalog(request: Request):
    """
    Return the full data catalog with discovered schemas.
    
    Progressive: returns whatever has been discovered so far.
    Entries have status: "pending" (not yet discovered), "discovered" (schema available), "error".
    
    Query params:
        source_id: (optional) return schema for a single source only
    """
    catalog = request.app.state.data_catalog
    source_id = request.query_params.get("source_id")
    
    if source_id:
        schema = catalog.get_schema(source_id)
        if schema:
            return schema
        return JSONResponse(status_code=404, content={"error": f"Source not found: {source_id}"})
    
    return catalog.get_all()


@router.post("/data-catalog/refresh-local")
async def refresh_local_catalog(request: Request):
    """Re-scan local /tmp files after a file upload."""
    catalog = request.app.state.data_catalog
    catalog.refresh_local_files()
    return {"status": "refreshing"}


@router.post("/data-catalog/resync")
async def resync_data_catalog(request: Request):
    """Replace the cloud data-source entries with a proxy-pushed, fully-formed set.

    The proxy is authoritative for cloud schemas (S3/DynamoDB/Athena/Aurora): it
    discovers them in parallel and POSTs them here on every Data Sources panel
    refresh. Local /tmp entries are preserved (VM-owned). Body: {"entries": [...]}
    where each entry matches the GET /data-catalog entry shape.
    """
    body = await request.json()
    entries = body.get("entries", [])
    catalog = request.app.state.data_catalog
    catalog.load_cloud_entries(entries)
    return {"status": "ok", "loaded": len(entries)}
