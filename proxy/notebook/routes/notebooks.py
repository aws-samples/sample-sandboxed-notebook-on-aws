"""
Notebook CRUD API routes.

Part of: proxy.notebook (Notebook application layer)

Endpoints:
  GET    /notebooks           - List all notebooks
  GET    /notebooks/{id}      - Get a notebook (+ its dependency DAG document as "dag")
  GET    /notebooks/{id}/dag  - Get the notebook's dependency DAG document
  POST   /notebooks           - Create a notebook (response carries "dag")
  PUT    /notebooks/{id}      - Update a notebook (response carries "dag")
  DELETE /notebooks/{id}      - Delete a notebook (+ its chart objects in S3)
  POST   /notebooks/{id}/versions/{vg}/restore - Restore a version ("dag" rebuilt)
  GET    /notebooks/{id}/cells/{cell}/image   - A cell's persisted chart PNG

Chart images: cells carry only `imageS3Key` (validated by storage to the
notebook's/cell's own notebook-outputs/ prefix). The image route never takes a
key from the client — it reads the stored key — and sits behind the app-level
X-Proxy-Auth gate like every other route. Keys a save/restore stops referencing
are deleted from S3 by the guarded proxy.notebook.cell_images.delete_keys.

The DAG document is built server-side only (proxy/notebook/dag.py via
proxy/notebook/dag_service.py) and stored in notebook_dag in the same transaction
as the cell write. Client-sent "dag" / "dag_build" keys are always discarded.
"""

import asyncio
import json
import re
import uuid
import logging

from fastapi import APIRouter, Query, Request, Response
from proxy.platform.errors import json_error

from proxy.storage import astorage
from proxy.storage._cell_fields import image_sha_from_key, valid_image_key
from proxy.notebook import cell_images, dag_service, session_checkpoints
from proxy.notebook.session_relink import apply_relink

logger = logging.getLogger(__name__)
# Shared versioning helpers — the SAME fingerprint the storage layer stores on each
# version header, so the route's dedup decision and the stored rows can't drift.
from proxy.storage.versioning import fingerprint_hash

# Valid run triggers: single-cell, Run All, run-from-cell, or a reactive re-run of
# dataflow-stale downstream cells ("Run stale downstream").
_VALID_TRIGGERS = {"single", "run_all", "run_from", "reactive"}

# Valid DOCUMENT version triggers: explicit user save, background interval autosave,
# a checkpoint taken right before an AI edit, one taken right before a restore, and
# the one-time BASELINE snapshot auto-created when a notebook is first populated
# (so there's always an original restore point + something to diff against).
_VALID_VERSION_TRIGGERS = {"manual", "auto", "pre_ai", "pre_restore", "baseline"}




router = APIRouter(tags=["notebooks"])

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CELL_ID_LEN = 256


async def _image_keys_or_empty(notebook_id: int) -> dict:
    try:
        return await astorage.notebook_cell_image_keys_all(notebook_id)
    except Exception as e:
        logger.warning(f"chart key read failed for notebook {notebook_id}: {e}")
        return {}


async def _delete_orphan_images(notebook_id: int, old_keys: dict) -> None:
    """After a successful cell write: delete (guarded, in the background) the
    chart objects the notebook no longer references."""
    if not old_keys:
        return
    new = await _image_keys_or_empty(notebook_id)
    # {cell: [keys]}: a key is an orphan only when NO cell (old or new) lists it.
    orphans = {k for ks in old_keys.values() for k in ks} - {k for ks in new.values() for k in ks}
    if orphans:
        cell_images.schedule_delete_keys(notebook_id, orphans)


def _strip_client_dag_keys(body: dict) -> dict:
    """Client-supplied DAG keys are never trusted: `dag` arrives from
    export/import payloads and `dag_build` is a storage keyword (a callable)."""
    body.pop("dag_build", None)
    body.pop("dag", None)
    return body


def _vm_manager(request):
    return getattr(getattr(getattr(request, "app", None), "state", None), "vm_manager", None)


@router.get("/notebooks")
async def notebook_list(request: Request):
    """List all notebooks. A notebook whose session link /terminate cleared is
    listed with its last checkpointed session (session_relink), so a fresh
    browser can still restore it."""
    notebooks = await astorage.notebook_list()
    vm_manager = _vm_manager(request)
    await asyncio.gather(*(apply_relink(nb, vm_manager) for nb in notebooks
                           if not nb.get("session_id") and nb.get("checkpoint_enabled")))
    return {"notebooks": notebooks}


@router.get("/notebooks/{notebook_id}")
async def notebook_get(notebook_id: int, request: Request):
    """Get a single notebook by ID, with its DAG document under "dag". An empty
    session_id is filled from the notebook's last checkpointed session (reopen
    after /terminate cleared the link; see proxy/notebook/session_relink.py)."""
    nb = await astorage.notebook_get(notebook_id)
    if not nb:
        return json_error(404, "Notebook not found")
    await apply_relink(nb, _vm_manager(request), with_tier=True)
    nb["dag"] = await dag_service.get_or_build(notebook_id, nb.get("cells") or [])
    return nb


@router.get("/notebooks/{notebook_id}/dag")
async def notebook_dag_get(notebook_id: int):
    """The notebook's dependency DAG document (design §2).

    Waits (≤ 5 s) for an in-flight VM attach of the notebook's session so a
    reopen sees the reconciled runtime, then returns the stored document,
    rebuilt from the stored cells when missing or out of sync. Never contacts
    the VM (must not wake a suspended VM)."""
    nb = await astorage.notebook_get(notebook_id)
    if not nb:
        return json_error(404, "Notebook not found")
    await dag_service.await_attach(nb.get("session_id"), timeout=5.0)
    return await dag_service.get_or_build(notebook_id, nb.get("cells") or [])


@router.post("/notebooks")
async def notebook_create(request: Request):
    """Create a new notebook and RETURN it with its DB-generated integer `id`.

    The client no longer supplies the id (create-returns-id / Option A): it POSTs
    name/tag/cells (+ its stable session_id) and adopts the `id` in the response as
    the notebook's permanent key. Any client-sent `id` is ignored. `name` defaults
    to a placeholder here; the frontend names it (e.g. "Notebook N") once it has
    the id, so numbering is decoupled from the PK."""
    body = _strip_client_dag_keys(await request.json())
    name = body.get("name") or "Untitled notebook"
    description = body.get("description", "")
    tag = body.get("tag", "Drafts")
    cells = body.get("cells", [])
    session_id = body.get("session_id") or body.get("sessionId")
    dag_build = (dag_service.cells_build(cells, "create")
                 if isinstance(cells, list) and cells else None)
    result = await astorage.notebook_create(name, description, tag, cells, session_id=session_id,
                                            dag_build=dag_build)
    if isinstance(result, dict) and "dag" not in result:
        # No cells (or the build was skipped): the open-path builds an empty doc.
        result["dag"] = await dag_service.get_or_build(result.get("id"), cells if isinstance(cells, list) else [])
    return result


@router.put("/notebooks/{notebook_id}")
async def notebook_update(notebook_id: int, request: Request):
    """Update a notebook (partial update — only send changed fields).

    Update-only: notebooks are created solely via POST /notebooks (which mints the
    DB id). There is NO upsert-create here anymore — a PUT for a nonexistent id is
    a 404 (previously it created a row under a client-supplied id, which is
    impossible now that the DB owns id generation).

    When the update carries cells, the DAG document is rebuilt in the SAME
    transaction (changed code hashes only); the response carries it as "dag"."""
    body = _strip_client_dag_keys(await request.json())

    # ANTI-CLOBBER BACKSTOP (server-side data-loss prevention): never let an
    # update ERASE an existing notebook's cells by writing an empty list. A
    # client crash/remount can transiently send {cells: []} for a notebook that
    # actually has content (observed: a frontend render-loop crash blew the
    # `notebooks`/`notebook_cells` tables to 0 rows). If the payload carries an
    # empty `cells` for a notebook that currently HAS cells, drop just the `cells`
    # field and apply the rest (name/tag/etc). A new notebook is created through
    # POST /notebooks; an update of an unknown id returns 404. This is the durable
    # guard: even a buggy client can't overwrite good content with nothing.
    if "cells" in body:
        incoming = body.get("cells")
        is_empty = not isinstance(incoming, list) or len(incoming) == 0
        if is_empty:
            try:
                existing = await astorage.notebook_get(notebook_id)
            except Exception:
                existing = None
            existing_cells = (existing or {}).get("cells") or []
            if existing_cells:
                logger.warning(
                    f"Refusing to overwrite notebook {notebook_id} "
                    f"({len(existing_cells)} cells) with an empty cell list — "
                    f"dropping 'cells' from this update (anti-clobber guard)."
                )
                body = {k: v for k, v in body.items() if k != "cells"}
                if not body:
                    # The update was ONLY the empty-cells clobber — nothing left to do.
                    return {"success": True, "skipped": "empty_cells_guard"}

    old_image_keys = None
    if "cells" in body:
        cells = body.get("cells") if isinstance(body.get("cells"), list) else []
        old_image_keys = await _image_keys_or_empty(notebook_id)
        success = await astorage.notebook_update(
            notebook_id, **body,
            dag_build=dag_service.cells_build(cells, "save", nb=notebook_id))
        if success:
            await _delete_orphan_images(notebook_id, old_image_keys)
    else:
        success = await astorage.notebook_update(notebook_id, **body)
    if not success:
        # Update-only: the notebook must already exist (created via POST). No
        # upsert-create — the DB owns id generation, so we can't materialize a row
        # under a client-supplied id. A missing id is a genuine 404.
        return json_error(404, "Notebook not found")

    # BASELINE version (one-time, per notebook). Sample/new/imported notebooks are
    # populated client-side and reach the server via this autosave PUT — the FIRST
    # such PUT carries the full initial cells. When a notebook first has cells AND
    # has no versions yet, snapshot a 'baseline' version so there's always an
    # original restore point and something to diff against before any edits.
    # Idempotent: the "no versions yet" gate makes it fire exactly once. Only runs
    # when this PUT actually carried cells (skip metadata-only updates). Wrapped so
    # a versioning hiccup can never fail the notebook save.
    if "cells" in body:
        try:
            await _maybe_create_baseline_version(notebook_id)
        except Exception as e:
            logger.warning(f"Baseline version creation skipped for {notebook_id}: {e}")

    try:
        dag = await astorage.notebook_dag_get(notebook_id)
    except Exception as e:
        logger.warning(f"DAG read-back after save failed for {notebook_id}: {e}")
        dag = None
    return {"success": True, "dag": dag}


async def _maybe_create_baseline_version(notebook_id: int) -> None:
    """Create a one-time 'baseline' version the first time a notebook has cells.
    No-op if the notebook has no cells yet, or if it already has ANY version
    (so it fires exactly once and never on reopen/re-persist)."""
    nb = await astorage.notebook_get(notebook_id)
    cells = (nb or {}).get("cells") or []
    if not cells:
        return  # nothing meaningful to baseline yet
    if await astorage.notebook_version_list(notebook_id, limit=1):
        return  # already has a version — baseline (or later) already exists
    await astorage.notebook_version_create(
        notebook_id=notebook_id,
        cells=cells,
        label="As loaded",
        trigger="baseline",
    )
    logger.info(f"Created baseline version for notebook {notebook_id} ({len(cells)} cells)")


@router.delete("/notebooks/{notebook_id}")
async def notebook_delete(notebook_id: int):
    """Delete a notebook AND its chat memory.

    For sqlite/postgres the chat lives in the DB and notebook_delete removes the
    agent_sessions row (→ agent_messages cascade). For the agentcore memory backend
    the conversation lives in the managed AgentCore Memory service, which the DB
    delete can't reach — so we also purge that session's events there. We capture
    the notebook's session_id BEFORE deleting the row, then purge (best-effort:
    a purge failure must not fail the delete).
    After the DB delete commits it also removes, best-effort, the chart images
    (notebook-outputs/<id>/), the chat attachments keyed by the notebook id
    (attachments/<id>/), and, for every session the notebook OWNED (read before
    the delete with notebook_owned_session_ids: no other surviving notebook
    references it), that session's checkpoints (sessions/<sid>/), chat
    attachments (attachments/<sid>/) and promoted data sources
    (user-data/<sid>/)."""
    import asyncio
    from proxy.notebook.ai.memory import delete_transcript, MEMORY_BACKEND

    # Resolve the session_id up front (needed for the external-store purge; the row
    # is gone after notebook_delete). Only needed for the agentcore backend.
    session_id = None
    if MEMORY_BACKEND == "agentcore":
        nb = await astorage.notebook_get(notebook_id)
        session_id = (nb or {}).get("session_id")

    # Owned sessions BEFORE the delete (agent_sessions is one of the sources).
    try:
        owned_sessions = await astorage.notebook_owned_session_ids(notebook_id)
    except Exception as e:
        logger.warning(f"notebook_delete: owned sessions for {notebook_id} unavailable: {type(e).__name__}")
        owned_sessions = []
    success = await astorage.notebook_delete(notebook_id)
    # The notebook_dag row is deleted by storage; drop the session → notebook hit.
    dag_service.invalidate_notebook(notebook_id)

    if success:
        # Chart objects: DB first (above), then best-effort S3 prefix delete
        # (drains pending uploads ≤ 10 s; never fails the delete).
        await cell_images.delete_notebook_prefix(notebook_id)
        # Owned sessions' S3 data (guarded sessions/, attachments/ and
        # user-data/<sid>/ prefixes), then attachments/<id>/.
        await session_checkpoints.delete_owned_session_prefixes(owned_sessions)
        await session_checkpoints.delete_notebook_data(notebook_id)
        if owned_sessions:
            # user-data/<sid>/ overlays are gone: drop cached GET /datasources.
            from proxy.platform.datasources import registry as _ds_registry
            _ds_registry.invalidate_datasources_cache("notebook delete")

    if success and MEMORY_BACKEND == "agentcore" and session_id:
        # Purge the conversation from AgentCore Memory (list events + delete each).
        # Best-effort + off-loop; the notebook is already deleted regardless.
        await asyncio.to_thread(delete_transcript, session_id)

    return {"success": success}


# ============================================================
# NOTEBOOK RUNS — execution history (one row per run)
# ============================================================

@router.post("/notebook-runs")
async def notebook_run_create(request: Request):
    """Record one execution run (single cell / Run All / run-from-cell).

    Frontend-driven: the client owns run boundaries, so it POSTs a summary when
    a run completes. Write-only history — the live sidebar still derives its
    subline from cell state; this table makes runs durable + queryable."""
    body = await request.json() if await request.body() else {}
    notebook_id = body.get("notebookId")
    if not notebook_id:
        return json_error(400, "notebookId is required")
    # notebook_id is now the DB-generated integer PK; coerce the JSON value (which
    # may arrive as a JSON number or a numeric string) to int for a clean FK.
    try:
        notebook_id = int(notebook_id)
    except (TypeError, ValueError):
        return json_error(400, "notebookId must be an integer")
    trigger = body.get("trigger", "single")
    if trigger not in _VALID_TRIGGERS:
        trigger = "single"
    session_id = body.get("sessionId")
    # The session→vm registry is authoritative: it is the VM the proxy actually
    # routed this session's cells to. Use it whenever it has an entry, so a stale
    # or foreign client id (a tab pointing at another notebook's VM, or lagging a
    # recovery/rotation) can't misattribute the run. The client-supplied id is only
    # a fallback when the registry has none (the run is still recorded either way).
    microvm_id = body.get("microvmId")
    if session_id:
        reg = await astorage.session_registry_get(session_id)
        if reg and reg.get("vm_id"):
            microvm_id = reg.get("vm_id")
    # Per-cell breakdown for the drill-down, accepted as-is (client-shaped:
    # [{cell_id, label, type, status, exec_ms, ...}]) and stored as an opaque JSON
    # blob. Each entry may also carry its own `params` map ({label: value}) for the
    # @param values that cell declared; the UI accumulates these across scope.
    run_cells = body.get("runCells") or body.get("cells") or []
    if not isinstance(run_cells, list):
        run_cells = []
    run = await astorage.notebook_run_create(
        run_id=str(uuid.uuid4()),
        notebook_id=notebook_id,
        session_id=session_id,
        microvm_id=microvm_id,
        trigger=trigger,
        started_at=body.get("startedAt"),
        finished_at=body.get("finishedAt"),
        cells_executed=int(body.get("cellsExecuted", 0) or 0),
        cells_errored=int(body.get("cellsErrored", 0) or 0),
        cells_interrupted=int(body.get("cellsInterrupted", 0) or 0),
        total_exec_ms=float(body.get("totalExecMs", 0) or 0),
        run_cells=run_cells,
    )
    return run


@router.get("/notebooks/{notebook_id}/runs")
async def notebook_run_list(notebook_id: int, limit: int = Query(20, ge=0), trigger: str = None):
    """List a notebook's execution runs, most recent first. Optional `trigger`
    ('single'|'run_all'|'run_from') returns only that kind, so the UI can show
    the last N Run All runs for apples-to-apples comparison."""
    if trigger not in _VALID_TRIGGERS:
        trigger = None
    return {"runs": await astorage.notebook_run_list(notebook_id, limit=limit, trigger=trigger)}


# ============================================================
# NOTEBOOK VERSIONS — document history (header + lean cell snapshot)
# ============================================================
# These version the DOCUMENT only (code/type/position/output_variable/
# ai_explanation/ai_explanation_source/generated) — NOT runtime outputs or
# dataflow, which are rebuilt on re-execution. The 3s autosave (notebook_update)
# remains the untouched working copy; versions are point-in-time snapshots of it.

@router.post("/notebooks/{notebook_id}/versions")
async def notebook_version_create(notebook_id: int, request: Request):
    """Snapshot the notebook's CURRENT cells as a new version group.

    The server is the source of truth for "current" — it reads the live cells via
    storage (so a version can never disagree with what autosave persisted) and
    hands them to the store. Body is optional: {"label": str?, "trigger": str?}.
    Returns the version header."""
    body = await request.json() if await request.body() else {}
    nb = await astorage.notebook_get(notebook_id)
    if not nb:
        return json_error(404, "Notebook not found")
    label = body.get("label")
    trigger = body.get("trigger", "manual")
    if trigger not in _VALID_VERSION_TRIGGERS:
        trigger = "manual"
    current_cells = nb.get("cells") or []

    # Dedup AUTOMATIC snapshots against ANY existing version, not just the latest.
    # A manual save is explicit user intent, so we always honor it (a user may want
    # a named marker even with no changes). Auto / pre_ai / pre_restore are machine-
    # triggered, so if the current document already matches an existing snapshot —
    # even an OLDER one the user edited back to — creating another is pure noise.
    # We compare fingerprints so this holds regardless of how the client's local
    # change-tracking behaved. Returns the matching header instead of a new row.
    if trigger != "manual":
        current_hash = fingerprint_hash(current_cells)
        match = await astorage.notebook_version_find_by_fingerprint(notebook_id, current_hash)
        if match:
            return match

    header = await astorage.notebook_version_create(
        notebook_id=notebook_id,
        cells=current_cells,
        label=label,
        trigger=trigger,
    )
    # Keep auto-snapshots bounded; explicit/pre_ai/pre_restore versions are kept.
    if trigger == "auto":
        await astorage.notebook_version_prune(notebook_id, keep=50)
    return header


@router.get("/notebooks/{notebook_id}/versions")
async def notebook_version_list(notebook_id: int, limit: int = Query(50, ge=0)):
    """List a notebook's version headers, most recent first (no cell rows)."""
    return {"versions": await astorage.notebook_version_list(notebook_id, limit=limit)}


@router.get("/notebooks/{notebook_id}/versions/{version_group_id}")
async def notebook_version_get(notebook_id: int, version_group_id: str):
    """Get one version: header + its cells (ordered, restore-shaped)."""
    version = await astorage.notebook_version_get(version_group_id)
    if not version or version.get("notebook_id") != notebook_id:
        return json_error(404, "Version not found")
    return version


@router.delete("/notebooks/{notebook_id}/versions/{version_group_id}")
async def notebook_version_delete(notebook_id: int, version_group_id: str):
    """Delete one version (any trigger) and its cell rows. Scoped by notebook_id."""
    ok = await astorage.notebook_version_delete(notebook_id, version_group_id)
    if not ok:
        return json_error(404, "Version not found")
    return {"success": True, "deleted": version_group_id}


@router.post("/notebooks/{notebook_id}/versions/{version_group_id}/restore")
async def notebook_version_restore(notebook_id: int, version_group_id: str):
    """Restore a version onto the live notebook.

    Safety net: before overwriting, snapshot the CURRENT cells as a 'pre_restore'
    version so a restore is itself undoable. Then write the target version's cells
    to the live store via notebook_update — the same path autosave uses, so the
    working copy stays consistent. Runtime outputs/dataflow are intentionally NOT
    restored; they rebuild on re-execution."""
    version = await astorage.notebook_version_get(version_group_id)
    if not version or version.get("notebook_id") != notebook_id:
        return json_error(404, "Version not found")
    nb = await astorage.notebook_get(notebook_id)
    if not nb:
        return json_error(404, "Notebook not found")
    # 1) Undo point for the restore itself.
    pre = await astorage.notebook_version_create(
        notebook_id=notebook_id,
        cells=nb.get("cells") or [],
        label=f"Before restore of {(version.get('label') or version_group_id)}",
        trigger="pre_restore",
    )
    # 2) Overwrite the live document with the version's cells. notebook_update
    #    returns False if no row was written (e.g. the notebook was deleted between
    #    the check above and here) — don't claim success, or the client would swap
    #    its live cells to a set that was never persisted and lose them on reload.
    restored_cells = version.get("cells") or []
    old_image_keys = await _image_keys_or_empty(notebook_id)
    ok = await astorage.notebook_update(
        notebook_id, cells=restored_cells,
        dag_build=dag_service.cells_build(restored_cells, "restore_version", nb=notebook_id))
    if not ok:
        return json_error(500, "Restore failed — notebook could not be updated")
    # Versions are document-only (no chart keys), so the restored cells reference
    # none: the replaced charts are orphans (guarded delete).
    await _delete_orphan_images(notebook_id, old_image_keys)
    try:
        dag = await astorage.notebook_dag_get(notebook_id)
    except Exception as e:
        logger.warning(f"DAG read-back after version restore failed for {notebook_id}: {e}")
        dag = None
    return {
        "success": True,
        "restored_from": version_group_id,
        "pre_restore_version": pre.get("version_group_id"),
        "cells": restored_cells,
        "dag": dag,
    }


@router.get("/notebooks/{notebook_id}/cells/{cell_id}/versions")
async def notebook_cell_versions(notebook_id: int, cell_id: str, limit: int = Query(20, ge=0)):
    """Per-cell history: every snapshot of ONE cell across version groups, newest
    first. Powers per-cell revert / inline diff."""
    return {
        "versions": await astorage.notebook_cell_versions(notebook_id, cell_id, limit=limit)
    }




@router.get("/notebooks/{notebook_id}/cells/{cell_id}/image")
async def notebook_cell_image(notebook_id: int, cell_id: str, sha: str = None, key: str = None):
    """A cell's persisted chart PNG (behind the app-level X-Proxy-Auth gate).

    The client never supplies an S3 key: the route reads THIS notebook/cell's
    stored key and re-validates it, so no arbitrary key / traversal read is
    possible. `sha` (optional) must match the stored key, so a stale client
    after a re-run gets 404 rather than a different chart. `key` (optional, a
    sha256) selects ANY image the cell references (its last image or one listed
    in its multi-output envelope); an unknown sha is a 404. The bytes are served
    from this loopback route only (no presigned URLs)."""
    if not cell_id or len(cell_id) > _MAX_CELL_ID_LEN:
        return json_error(400, "invalid cell id")
    if sha is not None and not _SHA_RE.match(sha):
        return json_error(400, "invalid sha")
    if key is not None and not _SHA_RE.match(key):
        return json_error(400, "invalid key")
    try:
        all_keys = await astorage.notebook_cell_image_keys_all(notebook_id)
    except Exception as e:
        logger.warning(f"chart key read failed for notebook {notebook_id}: {e}")
        return json_error(502, "image lookup failed")
    cell_keys = all_keys.get(str(cell_id)) or []
    if not cell_keys:
        return json_error(404, "image not found")
    if key is not None:
        # Any image of this cell, chosen by its sha. Never a client-built S3 key.
        key = next((k for k in cell_keys if image_sha_from_key(k) == key), None)
    else:
        key = cell_keys[0]   # the last-image column (legacy behaviour)
    if not key:
        return json_error(404, "image not found")
    if valid_image_key(notebook_id, cell_id, key) is None:
        logger.warning(f"stored chart key for notebook {notebook_id} cell failed validation")
        return json_error(404, "image not found")
    if sha is not None and image_sha_from_key(key) != sha:
        return json_error(404, "image not found")
    try:
        png = await cell_images.get(key)
    except cell_images.ImageMissing:
        return json_error(404, "image not found")
    except cell_images.BucketUnavailable:
        return json_error(503, "image storage unavailable")
    except Exception as e:
        logger.warning(f"chart fetch failed for notebook {notebook_id}: {type(e).__name__}")
        return json_error(502, "image fetch failed")
    return Response(content=png, media_type="image/png", headers={
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'",
    })


@router.post("/import-from-url")
async def import_notebook_from_url(request: Request):
    """
    Import a notebook from a GitHub (or raw) URL.
    
    Accepts:
      - GitHub blob URLs: https://github.com/user/repo/blob/main/notebook.ipynb
      - Raw URLs: https://raw.githubusercontent.com/user/repo/main/notebook.ipynb
      - Any direct URL to a .ipynb or .notebook.json file
    
    Returns parsed cells in our format, ready to open as a new tab.
    """
    import httpx
    import re

    from ._url_guard import validate_public_url

    body = await request.json()
    url = body.get("url", "").strip()

    if not url:
        return json_error(400, "url required")

    # SSRF guard: block internal destinations (metadata endpoint, RFC-1918,
    # loopback, the MicroVM control plane, file://, etc.) while still allowing any
    # PUBLIC host. Validate the user-supplied URL BEFORE the github-blob rewrite.
    err = validate_public_url(url)
    if err:
        return json_error(400, "URL not allowed")

    # Convert GitHub blob URLs to raw URLs
    # https://github.com/user/repo/blob/main/file.ipynb → https://raw.githubusercontent.com/user/repo/main/file.ipynb
    github_blob_pattern = r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)"
    match = re.match(github_blob_pattern, url)
    if match:
        user, repo, path = match.groups()
        url = f"https://raw.githubusercontent.com/{user}/{repo}/{path}"

    # Re-validate AFTER the rewrite — the rewritten target must also be public.
    err = validate_public_url(url)
    if err:
        return json_error(400, "URL not allowed")

    # Fetch the file. follow_redirects=False so a public URL cannot bounce (3xx)
    # into an internal address past the validator — redirects are rejected
    # outright. The github-blob→raw rewrite yields direct 200s, so the happy path
    # is unaffected.
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(url)
            if 300 <= resp.status_code < 400:
                return json_error(400, "redirects not followed")
            if resp.status_code != 200:
                return json_error(400, f"Failed to fetch: HTTP {resp.status_code}")
            content = resp.text
    except Exception as e:
        return json_error(500, f"Fetch error: {e}")

    # Parse the notebook
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return json_error(400, "Not valid JSON")

    # Detect format: Jupyter .ipynb vs our .notebook.json
    cells = []
    name = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # filename without extension

    if "nbformat" in data and "cells" in data:
        # Jupyter .ipynb format
        for cell in data["cells"]:
            cell_type = cell.get("cell_type", "code")
            source = cell.get("source", [])
            code = "".join(source) if isinstance(source, list) else (source or "")

            if cell_type == "markdown":
                cells.append({"type": "markdown", "code": code})
            elif cell_type == "code":
                # Detect %%sql magic
                if code.strip().startswith("%%sql"):
                    cells.append({"type": "sql", "code": code.strip().replace("%%sql\n", "").replace("%%sql", "")})
                else:
                    cells.append({"type": "code", "code": code})
            # Skip raw cells

        name = data.get("metadata", {}).get("title", name)

    elif "cells" in data and isinstance(data["cells"], list):
        # Our .notebook.json format
        cells = data["cells"]
        name = data.get("name", name)

    else:
        return json_error(400, "Unrecognized notebook format")

    return {
        "name": name,
        "description": data.get("description", ""),
        "cells": cells,
        "source_url": url,
    }
