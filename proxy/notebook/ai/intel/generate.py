"""
Full-generation pipeline + background orchestrator for Workbook Intelligence.

Part of: proxy.notebook.ai.intel

  - generate_intel: the dedicated agent-with-tools loop (Phase 1 — structured
    arrays), plus generate_full_report (Phase 2 — the prose full_report).
  - generate_intel_async: the non-blocking entry point that picks the right path
    (full / incremental / deletion) and runs it in a background thread.
"""

import os
import json
import time
import uuid
import logging

from ..prompts import INTEL_PROMPT, INTEL_REPORT_PROMPT
from ..notebook_agent import (
    AgentTraceCallbackHandler, _get_direct_client, _prompt_caching_kwargs,
)
from ..tools.execution_tools import set_execution_context, clear_agent_scope
from ..constants import INTEL_MODEL_ID, AGENT_TEMPERATURE, AGENT_MAX_TOKENS
from proxy.platform.defaults import DEFAULT_MEMORY_MIB
from batch.entity_discovery import discover_all_local_files

from .parsing import (
    _extract_intel_json,
    _extract_json_object,
    _strip_code_fences,
    _salvage_phase2_fields,
    _parse_phase2_delimited,
)
from .context import _fetch_relevant_entity_docs
from .store import (
    save_intel_to_s3,
    load_intel_from_s3,
    submit_generation,
)
from .delta import generate_intel_incremental, generate_intel_deletion

logger = logging.getLogger(__name__)

PROXY_PORT = int(os.environ.get("PROXY_PORT", "8081"))
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


def intel_agent_tools() -> list:
    """The Workbook Intelligence agent's tools. Includes inspect_variable because
    NOTEBOOK_AGENT_BASE_PROMPT (its system prompt) tells the model to check a
    variable's live columns with it instead of execute_code."""
    from ..tools.execution_tools import (
        execute_code, get_variables, get_notebook_state, install_package,
        get_available_data_sources, inspect_variable,
    )
    return [execute_code, get_variables, inspect_variable, get_notebook_state,
            install_package, get_available_data_sources]


def generate_intel(session_id: str, vm_manager, bucket: str, storage) -> dict | None:
    """
    Generate workbook intelligence using a dedicated AI agent with tools.
    
    Creates a new agent with a unique session ID each time (avoids concurrency
    conflicts with the user's chat agent).
    
    Returns structured dict or None on failure.
    """
    session_vm = vm_manager.get_session_vm(session_id)
    if not session_vm:
        logger.warning(f"[intel] No VM found for session {session_id[:8]}...")
        return None

    vm_id = session_vm["vm_id"]
    proxy_url = f"http://localhost:{PROXY_PORT}"

    try:
        # Ensure every local /tmp file in this session has an up-to-date profile
        # doc before we read them — this is the "notebook-level per-local-file
        # discovery" step. Cheap no-op if nothing changed (see entity_discovery.py).
        # NOTE: this is the main cost between "Initiating COMPLETE" and "Invoking agent" —
        # each newly-uploaded local file is profiled on the VM + gets an LLM-generated doc.
        _t0 = time.time()
        try:
            local_summary = discover_all_local_files(session_id, proxy_url, bucket, AWS_REGION, storage)
            if local_summary["total"] > 0:
                logger.info(f"[intel]    → Local file discovery for session {session_id[:8]}...: "
                           f"{local_summary['discovered']} discovered, "
                           f"{local_summary['skipped_unchanged']} unchanged, "
                           f"{local_summary['errors']} errors "
                           f"({time.time() - _t0:.1f}s)")
        except Exception as e:
            logger.warning(f"[intel] Local file discovery failed for session {session_id[:8]}...: {e}")

        # Pull every pre-computed entity profile relevant to this session (global
        # sources from Phase 1 discovery + the local files just ensured above)
        _t1 = time.time()
        entity_docs = _fetch_relevant_entity_docs(session_id, proxy_url, bucket, storage)
        logger.info(f"[intel]    → Fetched entity profiles ({len(entity_docs)} chars) in {time.time() - _t1:.1f}s; "
                    f"pre-agent prep total {time.time() - _t0:.1f}s")

        # Resolve this session's notebook_id (the stable integer PK) so the agent's
        # <notebook_context> can scope v_nb_* metadata queries. A notebook has ONE
        # stable session_id, so we match on it. Best-effort: 'unknown' if not found
        # (the prompt handles 'unknown'), and never let this fail intel generation.
        # Also capture the notebook's CELLS here (same lookup) so the intel agent's
        # get_notebook_state tool can actually see the user's code/outputs. Without
        # this the context carried notebook_cells=[] and the tool reported "Notebook
        # is empty (no cells)" even for a populated notebook — so the agent narrated
        # that contradiction instead of emitting the report JSON. notebook_list()
        # already includes cells (type/code/output/error), the exact shape the tool
        # reads, so no remapping is needed.
        notebook_id = None
        notebook_cells = []
        try:
            for nb in storage.notebook_list():
                if nb.get("session_id") == session_id:
                    notebook_id = nb.get("id")
                    notebook_cells = nb.get("cells") or []
                    break
        except Exception as e:
            logger.warning(f"[intel] Could not resolve notebook_id for session {session_id[:8]}...: {e}")

        # Build context for the agent tools
        context = {
            "proxy_url": proxy_url,
            "session_id": session_id,
            "notebook_id": notebook_id,
            "notebook_cells": notebook_cells,
            "memory_mib": vm_manager.active_microvms.get(vm_id, {}).get("memory_mib"),
            "data_sources": None,  # Agent will fetch via tool
            "packages": [],
            "uploaded_files": [],
        }

        # Use a unique session ID so get_or_create_agent creates a fresh agent
        intel_session_id = f"intel-{uuid.uuid4().hex[:8]}-{session_id}"
        # This run's exploration scratch: execute_code probes read the user's
        # variables but their names never reach the user namespace (they once
        # leaked as orphans, e.g. dynamodb_sentiment). Cleared in the finally below.
        intel_scope = f"intel:{intel_session_id}"
        context["agent_scope"] = intel_scope

        # Set context for tools
        set_execution_context(context)

        # Create a brand new agent (unique ID = never conflicts with user's chat).
        # Attach a tracing callback so we can see the agent's tool calls / inputs /
        # results step by step (COMPLETE flow only — the interactive chat agent stays silent).
        tracer = AgentTraceCallbackHandler(log_prefix=f"[intel] {session_id[:8]}")

        # Create a dedicated intel agent using INTEL_MODEL_ID — separate from the
        # user's chat agent (BEDROCK_MODEL_ID). Uses prompt caching for the multi-turn tool loop.
        from strands import Agent
        from strands.models import BedrockModel
        from strands.agent.conversation_manager import SlidingWindowConversationManager

        _intel_model_id = os.environ.get("INTEL_MODEL_ID", INTEL_MODEL_ID)
        intel_model = BedrockModel(
            model_id=_intel_model_id,
            region_name=AWS_REGION,
            temperature=AGENT_TEMPERATURE,
            max_tokens=AGENT_MAX_TOKENS,
            **_prompt_caching_kwargs(_intel_model_id),
        )

        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc)
        # Intel uses the BUSINESS base prompt only — NOT NOTEBOOK_AGENT_PROMPT. The
        # base omits the <notebook_context> operational block (the v_nb_* self-
        # telemetry steering), so the intel agent stays focused on the user's data
        # sources and can't fabricate run/cost/cell-time "findings" about the
        # notebook itself. Because the base has no {notebook_id}/{session_id} keys,
        # those are intentionally NOT passed here (the chat agent supplies them via
        # the composed NOTEBOOK_AGENT_PROMPT).
        from ..prompts import NOTEBOOK_AGENT_BASE_PROMPT
        _system_prompt = NOTEBOOK_AGENT_BASE_PROMPT.format(
            current_time=_now.strftime("%Y-%m-%d %H:%M UTC (%A)"),
            aws_region=AWS_REGION,
            memory_tier=f"{context.get('memory_mib') or DEFAULT_MEMORY_MIB} MB",
            athena_workgroup=os.environ.get("ATHENA_WORKGROUP", "microvm-demo"),
            athena_db=os.environ.get("ATHENA_DB", "microvm_demo_db"),
            s3_bucket=os.environ.get("ARTIFACT_BUCKET", "unknown"),
            dynamo_table_prefix=os.environ.get("DYNAMO_TABLE", "microvm-demo").rsplit("-", 1)[0] + "-",
        )

        agent = Agent(
            model=intel_model,
            system_prompt=_system_prompt,
            tools=intel_agent_tools(),
            conversation_manager=SlidingWindowConversationManager(window_size=10),
            callback_handler=tracer,
        )

        # Send the intel prompt
        logger.info(f"[intel]    → Invoking analysis agent for session {session_id[:8]}... (dedicated agent: {intel_session_id[:20]}...)")

        agent_start = time.time()
        try:
            result = agent(INTEL_PROMPT.format(
                entity_docs=entity_docs,
                catalog_json="[Call get_available_data_sources tool if you need anything not already covered above]",
                notebook_state="[Call get_variables tool to see current namespace state]",
                variables="[Use execute_code SPARINGLY — only for cross-source verification or sources not covered above]",
            ))
        finally:
            # Best-effort (3 s, never raises); the VM's TTL/LRU is the backstop.
            clear_agent_scope(proxy_url, session_id, intel_scope)

        raw_text = str(result).strip()
        logger.info(f"[intel]    → Agent finished in {time.time() - agent_start:.1f}s "
                    f"({tracer.tool_count} tool call(s)); response {len(raw_text)} chars")

        intel_data = _extract_intel_json(raw_text)
        if intel_data:
            logger.info(f"[intel]    → Parsed structured intel: "
                       f"{len(intel_data.get('suggested_analyses', []))} analyses, "
                       f"{len(intel_data.get('alerts', []))} alerts")
            # Phase 1 complete — structured arrays are ready (no full_report yet)
            intel_data["report_status"] = "generating"
            return intel_data, entity_docs
        else:
            # Final fallback: wrap entire response as a narrative full_report
            logger.warning("[intel] All JSON extraction strategies failed — using raw text as report")
            return {
                "suggested_analyses": [],
                "visualizations": [],
                "investigations": [],
                "alerts": [],
                "data_landscape": {"source_summary": "See full report for details"},
                "relationships": [],
                "full_report": raw_text[:8000],
                "report_status": "ready",
            }, entity_docs

    except Exception as e:
        logger.error(f"[intel] Generation failed for session {session_id[:8]}...: {e}")
        import traceback
        logger.error(traceback.format_exc())

    return None, None


def generate_full_report(session_id: str, intel_data: dict, entity_docs: str, bucket: str, storage) -> None:
    """
    Phase 2: Generate the full_report markdown + enrich structured arrays with
    prompt/action/join_suggestion fields.
    
    Single-shot Bedrock call (no tools, no agent loop). Runs in background after
    Phase 1 results are already saved and visible to the user.
    Updates the existing S3 intel JSON with enriched data + sets report_status='ready'.
    """
    from ..constants import AGENT_MAX_TOKENS
    
    _t0 = time.time()
    try:
        # Overwrite the source count with an AUTHORITATIVE value computed from the VM
        # catalog (in code, not the LLM). The model otherwise miscounts or carries a
        # stale total, so the intro's "N distinct data sources" disagreed with the
        # sources it listed (e.g. said 8 while listing 10). The prompt is instructed to
        # render total_sources + source_breakdown verbatim.
        try:
            from .context import compute_source_counts
            # VM-free count (proxy cloud catalog + DB local entities) — pass storage
            # so local files are included; no live VM read, so it can't time out.
            counts = compute_source_counts(session_id, storage=storage)
            if counts:
                landscape = dict(intel_data.get("data_landscape", {}) or {})
                landscape["total_sources"] = counts["total"]
                landscape["source_breakdown"] = counts["breakdown"]
                intel_data["data_landscape"] = landscape
                logger.info(f"[intel]    → authoritative source count: {counts['total']} ({counts['breakdown']})")
        except Exception as e:
            logger.warning(f"[intel]    → source-count computation failed ({e}); using existing count")

        # Build a compact representation of Phase 1 findings for the report prompt
        structured_findings = json.dumps({
            "suggested_analyses": intel_data.get("suggested_analyses", []),
            "visualizations": intel_data.get("visualizations", []),
            "investigations": intel_data.get("investigations", []),
            "alerts": intel_data.get("alerts", []),
            "data_landscape": intel_data.get("data_landscape", {}),
            "relationships": intel_data.get("relationships", []),
        }, indent=2)

        prompt_text = INTEL_REPORT_PROMPT.format(
            entity_docs=entity_docs,
            structured_findings=structured_findings,
        )

        # Single-shot Bedrock converse call (no agent, no tools). Build params
        # per-model via modelcaps so this survives an INTEL_MODEL_ID change to a
        # model that deprecates temperature (today's Haiku 4.5 still accepts it).
        from modelcaps import build_inference_config, build_additional_request_fields
        client = _get_direct_client()
        _intel_model = os.environ.get("INTEL_MODEL_ID", INTEL_MODEL_ID)
        _intel_kwargs = dict(
            modelId=_intel_model,
            messages=[{"role": "user", "content": [{"text": prompt_text}]}],
            inferenceConfig=build_inference_config(
                _intel_model, max_tokens=AGENT_MAX_TOKENS, temperature=0.2
            ),
        )
        _intel_extra = build_additional_request_fields(_intel_model, disable_thinking=True)
        if _intel_extra:
            _intel_kwargs["additionalModelRequestFields"] = _intel_extra
        response = client.converse(**_intel_kwargs)

        raw_text = response["output"]["message"]["content"][0]["text"].strip()
        logger.info(f"[intel]    → Phase 2 response in {time.time() - _t0:.1f}s ({len(raw_text)} chars)")

        # Parse Phase 2. The prompt now asks for a DELIMITED format — a small
        # relationships JSON, then "===FULL_REPORT===", then RAW markdown — so the
        # report (with code blocks, quotes, newlines) is NOT packed inside a JSON
        # string. That was the root cause of the recurring "Could not extract valid
        # JSON" failures: LLMs can't reliably escape a 1500-word markdown blob inside
        # a JSON value. Parse the delimited form first; fall back to the legacy
        # JSON-object form (+ field salvage) for older responses / partial compliance.
        rels, report = _parse_phase2_delimited(raw_text)
        if report is not None or rels is not None:
            if rels:
                intel_data["relationships"] = rels
            intel_data["full_report"] = report or ""
        else:
            # Legacy path: whole response is (or contains) a JSON object.
            phase2_data = None
            try:
                phase2_data = json.loads(raw_text)
            except (json.JSONDecodeError, ValueError):
                phase2_data = _extract_json_object(raw_text)

            if phase2_data and isinstance(phase2_data, dict):
                if phase2_data.get("relationships"):
                    intel_data["relationships"] = phase2_data["relationships"]
                intel_data["full_report"] = phase2_data.get("full_report") or ""
            else:
                # Field salvage: recover full_report + relationships individually so
                # a single bad char doesn't discard both.
                salvaged_report, salvaged_rels = _salvage_phase2_fields(raw_text)
                if salvaged_rels:
                    intel_data["relationships"] = salvaged_rels
                if salvaged_report:
                    logger.warning("[intel] Phase 2 not delimited + JSON invalid — salvaged full_report"
                                   f"{' + relationships' if salvaged_rels else ''} via field extraction")
                    intel_data["full_report"] = salvaged_report
                else:
                    logger.warning("[intel] Phase 2 parse failed — using stripped raw text as full_report")
                    intel_data["full_report"] = _strip_code_fences(raw_text)[:15000]

        intel_data["report_status"] = "ready"

        # Re-save to S3
        save_intel_to_s3(session_id, intel_data, bucket)
        logger.info(f"[intel]    → Phase 2 complete for session {session_id[:8]}... "
                    f"(full_report: {len(intel_data.get('full_report', ''))} chars)")

    except Exception as e:
        logger.error(f"[intel] Phase 2 (full_report) failed for session {session_id[:8]}...: {e}")
        # Mark as ready anyway (user just won't get the prose report)
        intel_data["report_status"] = "ready"
        intel_data["full_report"] = ""
        try:
            save_intel_to_s3(session_id, intel_data, bucket)
        except Exception:
            pass


def generate_intel_async(session_id: str, vm_manager, bucket: str, storage, trigger: str = "manual",
                         deleted_source: str = None) -> bool:
    """
    Generate intel in a background thread using the AI agent.
    Non-blocking — returns immediately.

    If trigger is "file_upload" and intel already exists for this session,
    uses the faster incremental path (single LLM call, no tool loop).
    Otherwise falls back to the full generation pipeline.

    Submits the job to the per-session FIFO scheduler (submit_generation in
    intel/store.py). Generations for a session run strictly one-at-a-time in
    submission order, so overlapping triggers (a file upload while a full run is
    mid-flight, a manual refresh during a delta, rapid successive uploads) are
    QUEUED and applied in order instead of being dropped or racing to overwrite
    the S3 doc. The whole job (Phase 1 + Phase 2) runs inside one queued unit, so
    "busy" is held across both phases (no mid-way release / clobber window).

    Returns True if the job was accepted (running or queued), False if it was
    dropped as an exact-duplicate of the queued tail (e.g. a double-clicked
    refresh, repeated identical file events) or the queue is at its cap.
    """

    def _run():
        start = time.time()
        entity_docs_for_report = None  # Only set by the COMPLETE path (Phase 1)
        try:
            intel_data = None

            # Deletion path: a local file was removed. If intel exists, prune everything
            # tied to that file (removal delta). If no intel exists, there's nothing to
            # prune — skip entirely (don't do a full regen just because a file was deleted).
            if trigger == "file_delete":
                existing_meta = storage.workbook_intel_get(session_id)
                if existing_meta and existing_meta.get("s3_key"):
                    existing_intel = load_intel_from_s3(session_id, bucket)
                    if existing_intel:
                        logger.info(f"[intel] ▶ Initiating DELETION Notebook Intel for session {session_id[:8]}... "
                                    f"(trigger=file_delete, source='{os.path.basename(deleted_source) if deleted_source else '?'}')")
                        intel_data = generate_intel_deletion(session_id, bucket, storage, existing_intel, deleted_source)
                if not intel_data:
                    # Either there was no existing report to prune, or the deletion pass
                    # produced no usable result. Either way, do NOT fall through to a full
                    # regen for a deletion — leave the current report as-is.
                    logger.info(f"[intel] Deletion trigger for session {session_id[:8]}...: nothing pruned — leaving report unchanged")
                    return

            # Incremental path: if this is a file_upload trigger and intel already exists,
            # just update the existing report with the new file info (much faster)
            if not intel_data and trigger == "file_upload":
                existing_meta = storage.workbook_intel_get(session_id)
                if existing_meta and existing_meta.get("s3_key"):
                    existing_intel = load_intel_from_s3(session_id, bucket)
                    if existing_intel:
                        logger.info(f"[intel] ▶ Initiating DELTA Notebook Intel for session {session_id[:8]}... (trigger=file_upload, existing intel found — incremental update)")
                        intel_data = generate_intel_incremental(session_id, vm_manager, bucket, storage, existing_intel)

            # Full path: either no existing intel, or non-file-upload trigger, or incremental failed.
            # (A file_delete that reached here already returned above, so it never does a full regen.)
            if not intel_data:
                logger.info(f"[intel] ▶ Initiating COMPLETE Notebook Intel for session {session_id[:8]}... (trigger={trigger})")
                intel_data, entity_docs_for_report = generate_intel(session_id, vm_manager, bucket, storage)

            elapsed = time.time() - start
            if intel_data:
                s3_key = save_intel_to_s3(session_id, intel_data, bucket)
                try:
                    storage.workbook_intel_save(session_id, s3_key)
                except Exception as e:
                    logger.warning(f"[intel] Failed to save metadata to DB: {e}")

                # If Phase 1 returned without a full_report, kick off Phase 2 in-thread
                # (we're already in a background thread, so just continue sequentially)
                if intel_data.get("report_status") == "generating" and entity_docs_for_report:
                    # Phase 1 is already saved to S3, so the frontend can render its
                    # cards now (it keys off report_status='generating', not the
                    # busy flag). We keep the job "running" across Phase 2 so a
                    # concurrent trigger can't start a second writer mid-report —
                    # the scheduler serializes it behind this job instead.
                    logger.info(f"[intel]    → Phase 1 complete ({elapsed:.1f}s). Starting Phase 2 (full_report)...")
                    generate_full_report(session_id, intel_data, entity_docs_for_report, bucket, storage)
                    elapsed = time.time() - start

                logger.info(f"[intel] Complete for session {session_id[:8]}... ({elapsed:.1f}s)")
            else:
                logger.warning(f"[intel] No intel generated for session {session_id[:8]}... ({elapsed:.1f}s)")
        except Exception as e:
            # Contain per-job failures so the scheduler's worker keeps draining.
            import traceback
            logger.error(f"[intel] Generation failed for session {session_id[:8]}...: {e}\n{traceback.format_exc()}")

    # Enqueue onto the per-session FIFO scheduler. dedup_key drops an exact-dup
    # CONSECUTIVE enqueue (same trigger + same deleted_source at the queue tail),
    # e.g. a double-clicked refresh or repeated identical file events.
    return submit_generation(session_id, _run, trigger=trigger,
                             dedup_key=(trigger, deleted_source))
