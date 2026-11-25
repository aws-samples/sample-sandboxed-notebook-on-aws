"""
Workbook Intelligence routes — generate and retrieve AI data insights reports.

Part of: proxy.notebook (Notebook application layer)

Endpoints:
  GET  /workbook-intel           - Get the current intel report for a session
  POST /workbook-intel/generate  - Trigger (re)generation of the intel report
"""

import os
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from proxy.storage import storage, astorage
from proxy.notebook.ai.workbook_intel import (
    generate_intel_async,
    load_intel_from_s3,
    is_generating,
    get_generating_trigger,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workbook-intel"])

ARTIFACT_BUCKET = os.environ.get(
    "ARTIFACT_BUCKET",
    f"microvm-sandbox-artifacts-{os.environ.get('ACCOUNT_ID', 'unknown')}-{os.environ.get('AWS_REGION', 'us-west-2')}"
)


@router.get("/workbook-intel")
async def get_workbook_intel(request: Request):
    """
    Get the workbook intelligence report for the current session.
    
    Returns the markdown report if available, or status if generation is in progress.
    
    Headers:
        X-Session-Id: session UUID (required)
    """
    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "X-Session-Id header required"})

    # Report real in-progress state first — this is the key fix for reliable
    # auto-popup: without it, callers can't tell "hasn't started" apart from
    # "actively running", and are forced to poll on a blind guessed schedule.
    if is_generating(session_id):
        # If a report already exists, this run is an incremental UPDATE (delta) that
        # extends/prunes it — signal that so the UI keeps the current report visible with
        # an "updating" indicator instead of blanking it and showing a bare spinner.
        existing_meta = await astorage.workbook_intel_get(session_id)
        has_existing = existing_meta is not None
        gen_trigger = get_generating_trigger(session_id)
        is_deletion = gen_trigger == "file_delete"
        if not has_existing:
            message = "Workbook intelligence report is being generated..."
        elif is_deletion:
            message = "Updating the report — removing insights tied to the deleted file..."
        else:
            message = "Updating the report with the newly uploaded data..."
        # Include the CURRENT (pre-update) report so a fresh panel mount (e.g. user
        # navigated away to Logs and back mid-update) can still render it under the
        # "updating" strip instead of showing a bare spinner. The report is replaced
        # once the update finishes and status flips to "ready".
        # Offload the synchronous S3 GET: the UI polls this endpoint while
        # Intel generates, so a blocking GET here would stall the event loop on
        # every poll.
        current_intel = (
            await asyncio.to_thread(load_intel_from_s3, session_id, ARTIFACT_BUCKET)
            if has_existing else None
        )

        # PHASE-1-READY SHORT-CIRCUIT: the two-phase generator saves the structured
        # arrays (analyses/alerts/visualizations) to S3 at the END of Phase 1 with
        # report_status='generating', then keeps the busy flag held while Phase 2
        # writes the prose full_report. Without this, the GET reports "generating"
        # for the WHOLE job, so the UI can't tell "Phase 1 cards are ready" from
        # "nothing yet" and only auto-expands after Phase 2 — a visible lag.
        #
        # Fires for ALL THREE staged flows that save Phase-1 arrays with
        # report_status='generating' before the prose Phase-2 lands:
        #   • a FIRST generation (generate_intel → save → generate_full_report),
        #   • an UPLOAD delta (generate_intel_incremental saves the MERGED arrays
        #     with report_status='generating' before regenerating full_report), and
        #   • a DELETION delta (generate_intel_deletion saves the PRUNED arrays with
        #     report_status='generating' before regenerating full_report).
        # If structured content is present and report_status is still 'generating',
        # surface it as status='ready' so the UI renders + auto-expands the current
        # (merged or pruned) cards immediately — right after the fast delta call —
        # instead of waiting the full ~90s Phase-2 regeneration. We pass
        # report_status='generating' through so the prose-report button keeps its
        # "generating…" state until Phase 2 saves the ready report.
        #
        # This is SEQUENCED correctly for deletion WITHOUT a special case: a completed
        # report on S3 carries report_status='ready', so during the brief window before
        # the prune save this guard is False → the caller keeps showing the pre-prune
        # report under the "removing…" strip. Only once the prune save writes
        # 'generating' does this fire — now with the PRUNED cards. So the "removing"
        # strip shows until the prune lands, then the pruned cards reveal, exactly
        # mirroring the upload flow's "updating → merged cards" transition.
        # Uniform update-context fields carried on EVERY in-progress response (both
        # phase1 and phase2). Previously the phase-2 short-circuit OMITTED these, so
        # the client's isUpdating collapsed to false the instant phase-1 cards
        # appeared and the "updating" strip vanished mid-update. Emitting them on
        # both branches is the fix — the client keeps one consistent update context
        # across the whole job.
        mode = "update" if has_existing else "full"
        reason = "deletion" if is_deletion else ("addition" if has_existing else "full")

        phase1_cards_ready = bool(
            current_intel
            and current_intel.get("report_status") == "generating"
            and (
                current_intel.get("suggested_analyses")
                or current_intel.get("alerts")
                or current_intel.get("visualizations")
                or current_intel.get("investigations")
            )
        )

        if phase1_cards_ready:
            # PHASE 2: structured cards (merged/pruned/first-gen) are saved to S3 with
            # report_status='generating'; the prose full_report is still being written.
            # Surface status='ready' so the UI renders the cards NOW, phase='phase2' so
            # it keeps the updating strip + shows the report icon as "generating…", and
            # the full update context (mode/reason/has_existing) so isUpdating stays true.
            return {
                "status": "ready",
                "phase": "phase2",
                "intel": current_intel,
                "generated_at": existing_meta.get("generated_at") if has_existing else None,
                "version": existing_meta.get("version") if has_existing else None,
                "report_status": "generating",
                "mode": mode,
                "reason": reason,
                "has_existing": has_existing,
                "message": message,
            }

        # PHASE 1: the job is running but the new/updated cards aren't saved yet. For a
        # delta we still return the PRE-update report (so the panel shows the old cards
        # under the spinner instead of blanking); for a first generation there's nothing
        # yet. phase='phase1' drives the spinner strip.
        return {
            "status": "generating",
            "phase": "phase1",
            "intel": current_intel,
            "generated_at": existing_meta.get("generated_at") if has_existing else None,
            "report_status": "generating",
            "mode": mode,
            "reason": reason,
            "has_existing": has_existing,
            "message": message,
        }

    # Check DB for existing intel
    intel_meta = await astorage.workbook_intel_get(session_id)
    if not intel_meta:
        return {"status": "not_generated", "phase": "not_generated", "intel": None, "message": "No intelligence report yet. Generate one or wait for auto-generation."}

    # Load from S3 (offloaded off the event loop, same rationale as above)
    intel_data = await asyncio.to_thread(load_intel_from_s3, session_id, ARTIFACT_BUCKET)
    if intel_data:
        # A COMPLETED, at-rest report: not generating and the prose full_report has
        # landed (report_status='ready'). phase='ready' is the terminal state the
        # client uses to drop the updating strip, enable the full-report button, and
        # re-enable regenerate. (If report_status is somehow still 'generating' here
        # with is_generating False, the job died mid-Phase-2 — we still surface
        # phase='ready' so the UI isn't stuck showing an updating strip forever.)
        return {
            "status": "ready",
            "phase": "ready",
            "intel": intel_data,
            "generated_at": intel_meta["generated_at"],
            "version": intel_meta["version"],
            "report_status": intel_data.get("report_status", "ready"),
        }

    # The DB row survived but the S3 payload is gone — there is NO usable base Intel.
    # This happens when a session's artifacts were rotated/cleared out from under a
    # surviving session_id (e.g. VM terminated + relaunched, artifacts pruned). Report
    # this as 'not_generated' rather than 'error' so callers treat it as "no base
    # Intel" and (re)generate a FULL report on the next Run All, instead of trusting a
    # stale metadata row that points at missing content.
    logger.info(f"[intel] Metadata row exists for session {session_id[:8]}... but S3 payload is "
                f"missing — reporting not_generated so a full report regenerates.")
    return {"status": "not_generated", "phase": "not_generated", "intel": None,
            "message": "No intelligence content found (metadata was stale). Generate a new report."}


@router.post("/workbook-intel/generate")
async def generate_workbook_intel(request: Request):
    """
    Trigger generation (or regeneration) of the workbook intelligence report.
    
    Can be called:
      1. By the VM after catalog discovery completes (trigger: "catalog_ready")
      2. By the user manually (trigger: "manual")
      3. On the FIRST Run All of a notebook (trigger: "first_run_all")
      4. On file upload/delete (trigger: "file_upload" | "file_delete")
    
    Non-blocking — returns immediately, generation happens in background.
    Any trigger other than file_upload/file_delete routes to a full generation.
    
    Headers:
        X-Session-Id: session UUID (required)
    
    Body (optional):
        {"trigger": "catalog_ready" | "manual" | "first_run_all" | "file_upload" | "file_delete"}
    """
    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "X-Session-Id header required"})

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    trigger = body.get("trigger", "manual")
    deleted_source = body.get("deleted_source")  # required for trigger="file_delete"
    vm_manager = request.app.state.vm_manager

    # Verify session has an active VM
    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        return JSONResponse(status_code=404, content={"error": "No active VM for this session"})

    logger.info(f"[intel] Generation triggered: session={session_id[:8]}... trigger={trigger}"
                + (f" deleted_source={deleted_source}" if deleted_source else ""))

    # Start async generation — de-duplicated: if one is already running for this
    # session (e.g. file-upload auto-trigger and a manual click landed close together),
    # this returns False and no second agent thread is spawned.
    started = generate_intel_async(
        session_id, vm_manager, ARTIFACT_BUCKET, storage,
        trigger=trigger, deleted_source=deleted_source,
    )

    if not started:
        return {"status": "generating", "message": "A workbook intelligence report is already being generated for this session."}

    return {"status": "generating", "message": "Workbook intelligence report is being generated..."}
