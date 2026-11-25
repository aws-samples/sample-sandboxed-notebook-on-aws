"""
Session checkpoint, data source, and secrets/env routes.
Part of: proxy.platform (Smart MicroVM Service layer)
Endpoints:
  GET    /sessions                    - List S3 session checkpoints
  DELETE /sessions/{id}               - Delete a session checkpoint from S3 (+ its intel rows)
  GET    /datasources                 - List data sources from every registered provider
                                        (short TTL cache; ?refresh=true bypasses it)
  GET    /datasources/schema          - Schema of one source
  GET    /datasources/entity-doc      - Full entity profile document of one source
  GET    /datasources/catalog         - The session VM's data catalog (resync first by default)
  GET    /datasources/snippet         - Ready-to-run Python/SQL snippet for a source
  DELETE /datasources/s3-file         - Delete a deletable S3 source
  POST   /datasources/sagemaker-endpoint/lifecycle - Stop/start a project SageMaker endpoint
  POST   /datasources/promote-attachment - Copy a chat attachment into a session data source
  GET    /secrets                     - Injectable Secrets Manager secrets (names only)
  GET    /secrets/keys                - Key names of one JSON secret
  GET    /notebooks/{id}/env          - A notebook's persisted env presence markers + secret refs
  PUT    /notebooks/{id}/env          - Merge a notebook's persisted env vars / replace secret refs
"""

import os
import json
import time
import asyncio
import functools
import logging
import concurrent.futures

from fastapi import APIRouter, Request, Response

from proxy.platform.microvm_manager import AWS_REGION
from proxy.platform import aws_clients, http_clients
from proxy.platform.errors import emit_error_metric, json_error

logger = logging.getLogger(__name__)

# Athena workgroup is surfaced in the /datasources response so the frontend can
# show it. The Glue database + all source enumeration now live in the data source
# provider registry (proxy/platform/datasources), not here.
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "microvm-demo")

router = APIRouter(tags=["sessions"])


@router.get("/sessions")
async def list_sessions(request: Request):
    """List available session checkpoints from S3."""
    vm_manager = request.app.state.vm_manager
    try:
        # S3 paging + per-session metadata GETs are blocking → worker thread.
        return await asyncio.to_thread(_list_sessions_sync, vm_manager)
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return json_error(502, f"Failed to list sessions: {e}", sessions=[])


def _list_sessions_sync(vm_manager) -> dict:
    """SYNC body of GET /sessions (run via asyncio.to_thread). Raises on S3 errors;
    the route maps any exception to the empty list."""
    s3 = aws_clients.get_client("s3", region_name=AWS_REGION)
    bucket_name = vm_manager.get_artifacts_bucket()

    if not bucket_name:
        return {"sessions": []}

    sessions = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name, Prefix="sessions/", Delimiter="/"):
        for prefix_obj in page.get("CommonPrefixes", []):
            session_prefix = prefix_obj["Prefix"]
            session_id = session_prefix.replace("sessions/", "").rstrip("/")

            metadata = {}
            try:
                meta_resp = s3.get_object(Bucket=bucket_name, Key=f"{session_prefix}metadata.json")
                metadata = json.loads(meta_resp["Body"].read())
            except Exception:
                pass

            sessions.append({
                "session_id": session_id,
                "checkpointed_at": metadata.get("checkpointed_at"),
                "execution_count": metadata.get("execution_count", 0),
                "variables_count": metadata.get("variables_count", 0),
                "files_count": metadata.get("files_count", 0),
                "checkpoint_size_kb": metadata.get("checkpoint_size_kb", 0),
                "save_timings_ms": metadata.get("save_timings_ms", {}),
            })

    sessions.sort(key=lambda s: s.get("checkpointed_at") or "", reverse=True)
    return {"sessions": sessions}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    """Delete a session checkpoint from S3. Also removes the session's
    workbook_intel / local_file_entities rows (their docs live in that prefix)."""
    vm_manager = request.app.state.vm_manager
    try:
        # Blocking S3 list + deletes → worker thread.
        return await asyncio.to_thread(_delete_session_sync, vm_manager, session_id)
    except Exception as e:
        return json_error(500, f"Delete failed: {e}")


def _delete_session_sync(vm_manager, session_id: str) -> dict:
    """SYNC body of DELETE /sessions/{id} (run via asyncio.to_thread). Raises on
    S3 errors; the route maps them to the 500 error Response."""
    s3 = aws_clients.get_client("s3", region_name=AWS_REGION)
    bucket_name = vm_manager.get_artifacts_bucket()
    if not bucket_name:
        return json_error(503, "Bucket not found")

    prefix = f"sessions/{session_id}/"
    resp = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
    for obj in resp.get("Contents", []):
        s3.delete_object(Bucket=bucket_name, Key=obj["Key"])

    # The intel doc (sessions/<sid>/workbook-intel.json) and local-entity docs
    # (sessions/<sid>/local-entities/…) were just deleted, so drop the rows that
    # point at them. Best effort: the response shape is unchanged.
    try:
        from proxy.storage import storage  # sync: this runs in a worker thread
        n = storage.session_intel_delete(session_id)
        if n:
            logger.info(f"delete_session: removed {n} intel/local-entity row(s) for {session_id}")
    except Exception as e:
        logger.warning(f"delete_session: session_intel_delete({session_id}) failed: {e}")

    return {"status": "deleted", "session_id": session_id}


@router.get("/datasources")
async def list_datasources(request: Request):
    """
    List external data sources accessible from the MicroVM.

    Registry-driven: enumeration is delegated to each provider's discover().
    Returns a generic `sources` array (+ `source_types` metadata) that the panel
    renders generically, plus legacy grouped arrays (s3/dynamodb/athena) kept for
    backward compatibility.
    """
    from proxy.platform.datasources import registry

    vm_manager = request.app.state.vm_manager
    bucket_name = None
    try:
        bucket_name = vm_manager.get_artifacts_bucket()
    except Exception:
        pass

    # Thread the session id so the S3 provider can also surface this session's
    # PROMOTED attachments under user-data/<session_id>/ (session overlay). Other
    # providers accept and ignore session_id. Sent by the frontend as X-Session-Id
    # (falls back to a ?session_id= query param).
    session_id = request.headers.get("X-Session-Id") or request.query_params.get("session_id") or None
    # Short TTL cache keyed on session_id (registry.discover_all_cached): the
    # mutation routes below invalidate it, SageMaker endpoints stay live, and
    # ?refresh=true (the panel's manual Refresh button) bypasses it.
    refresh = (request.query_params.get("refresh") or "").strip().lower() in ("1", "true", "yes")
    refs = await registry.discover_all_cached(session_id=session_id, refresh=refresh)

    # Generic, type-agnostic list (the data-driven panel consumes this).
    sources = [r.to_dict() for r in refs]

    # Legacy grouped arrays rebuilt from the same discovery results.
    s3_files, dynamodb_tables, athena_tables = [], [], []
    for r in refs:
        if r.source_type == "s3":
            s3_files.append({
                "key": r.extra.get("key"),
                "bucket": r.extra.get("bucket"),
                "size": r.extra.get("size"),
                "size_bytes": r.extra.get("size_bytes"),
                "uri": r.extra.get("uri"),
            })
        elif r.source_type == "dynamodb":
            dynamodb_tables.append({
                "name": r.extra.get("name"),
                "item_count": r.extra.get("item_count", 0),
                "region": r.extra.get("region"),
            })
        elif r.source_type == "athena":
            athena_tables.append({
                "name": r.extra.get("name"),
                "database": r.extra.get("database"),
                "columns": r.extra.get("columns", []),
                "column_count": r.extra.get("column_count", 0),
                "region": r.extra.get("region"),
            })

    return {
        "sources": sources,
        "source_types": registry.provider_metadata(),
        "s3": s3_files,
        "dynamodb": dynamodb_tables,
        "athena": athena_tables,
        "artifact_bucket": bucket_name,
        "athena_workgroup": ATHENA_WORKGROUP,
    }


@router.get("/datasources/schema")
async def get_datasource_schema(source_type: str, source_id: str, request: Request, session_id: str = None):
    """
    Get column schema for a specific data source.

    First tries the VM's data catalog (pre-discovered schemas) for instant response.
    Falls back to direct AWS API calls if the VM catalog doesn't have it.

    Args:
        source_type: 'athena', 'dynamodb', 's3', 'local'
        source_id: Table name, S3 URI, or file path
        session_id: Required for 'local' type (to forward request to the VM)

    Returns:
        {
            "source_type": "athena",
            "source_id": "microvm_demo_db.customers",
            "display_name": "customers",
            "columns": [{"name": "customer_id", "dtype": "string", "sample": "CUST-0100"}, ...],
            "row_count": 200,
            "size": "13 KB"
        }
    """
    from proxy.platform.datasources import registry

    vm_manager = request.app.state.vm_manager

    meta = {m["source_type"]: m for m in registry.provider_metadata()}
    if source_type not in meta:
        return json_error(400, f"Unknown source type: {source_type}")
    _requires_vm = meta[source_type]["requires_vm_execution"]

    # --- VM data catalog: ONLY for VM-resident sources (local /tmp files) ---
    # The proxy is the authority for CLOUD source schemas (S3/DynamoDB/Athena/
    # Aurora): it discovers them and PUSHES them to the VM at launch, and each
    # provider caches the result (5-min TTL, shared across tabs/sessions). So for
    # cloud sources we answer directly from the proxy provider below — no VM hop.
    # That avoids the round-trip, removes the VM-liveness dependency (a suspended
    # VM had to RESUME just to answer a schema click), and drops a failure surface
    # (e.g. the Aurora auto-pause race). Only LOCAL files live solely on the VM, so
    # only they consult the VM catalog first.
    if _requires_vm and session_id:
        session_vm = await vm_manager.get_session_vm_async(session_id)
        if session_vm:
            try:
                token = await vm_manager.get_auth_token(session_vm["vm_id"])
                resp = await http_clients.request_idempotent(
                    http_clients.vm_client(), "GET",
                    f"https://{session_vm['endpoint']}/data-catalog",
                    headers={"X-aws-proxy-auth": token},
                    params={"source_id": source_id},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Only return if schema has been discovered (not pending)
                    if data.get("status") == "discovered" and data.get("columns"):
                        # Normalize dtypes at proxy boundary (VM may still
                        # report raw pandas names until image is rebuilt)
                        for col in data.get("columns", []):
                            if "dtype" in col:
                                col["dtype"] = _normalize_col_dtype(col["dtype"])
                        return data
            except Exception:
                pass  # Fall through to direct provider

    # --- Direct provider via the registry ---
    # (source_type was already validated against `meta` at the top of the function.)

    # Providers that need VM execution (local files) require a session + endpoint,
    # so build an execute_fn that forwards code to the session's VM.
    execute_on_vm = None
    if _requires_vm:
        if not session_id:
            return json_error(400, "session_id required for this source type")
        session_vm = await vm_manager.get_session_vm_async(session_id)
        if not session_vm:
            return json_error(404, "Session not found")

        async def execute_on_vm(sid, code):
            token = await vm_manager.get_auth_token(session_vm["vm_id"])
            resp = await http_clients.vm_client().post(
                f"https://{session_vm['endpoint']}/execute",
                headers={"X-aws-proxy-auth": token, "Content-Type": "application/json"},
                # origin="internal": per-run throwaway scratch on the VM, so the schema
                # probe's df/path/pd/... never bind into (or clobber) user variables.
                json={"code": code, "origin": "internal"},
                timeout=15.0,
            )
            if resp.status_code == 200:
                return resp.json()
            return None

    provider = registry.get_provider(source_type, execute_fn=execute_on_vm)
    schema = await provider.get_schema(source_id, session_id=session_id)
    if not schema:
        return json_error(404, f"Schema not found for {source_id}")

    return {
        "source_type": schema.source_type,
        "source_id": schema.source_id,
        "display_name": schema.display_name,
        "columns": [{"name": c.name, "dtype": _normalize_col_dtype(c.dtype), "sample": c.sample, "nullable": c.nullable} for c in schema.columns],
        "row_count": schema.row_count,
        "size": schema.size,
    }


def _normalize_col_dtype(raw: str) -> str:
    """Normalize dtype at the API boundary — ensures consistent labels even if
    the upstream provider (VM catalog, fallback provider) hasn't been updated yet."""
    from app.notebook.dtypes import normalize_dtype
    return normalize_dtype(raw)


async def _enrich_catalog_entries(entries: list[dict], session_id: str = None) -> None:
    """
    In-place enrichment: for each catalog entry, check if a pre-computed entity
    doc exists (from batch/entity_discovery.py for global sources, or from the
    per-session local_file_entities table for local /tmp files) and if so, attach
    lightweight metadata (business_description, quality_flags summary) so the
    frontend can show an indicator and popover without fetching the full doc.
    """
    from proxy.storage import astorage

    # Pass 1: DB lookups (off-loop via astorage) + has_entity_doc + dtype
    # normalization; collect the ready entries whose doc JSON must be loaded.
    pending: list[tuple[dict, str, str]] = []  # (entry, doc_s3_key, version)
    for entry in entries:
        source_id = entry.get("source_id", "")
        source_type = entry.get("source_type", "")
        if not source_id:
            entry["has_entity_doc"] = False
            continue

        meta = None
        if source_type == "local" and session_id:
            # Local files are session-scoped — check the local_file_entities table
            meta = await astorage.local_entity_get(session_id, source_id)
        else:
            # Global entities (S3, DynamoDB, Athena) — check data_source_entities table.
            meta = await astorage.entity_get(source_id)
            # KEY MISMATCH FIX (DynamoDB): entity_discovery stores DynamoDB docs under
            # a "dynamodb.<table>" source_id (batch/entity_discovery.py), but the
            # DynamoDB provider emits the BARE table name as the catalog entry's
            # source_id (datasources/dynamodb.py). So a bare lookup misses the doc and
            # the sparkle never shows. When a dynamodb entry doesn't match bare, retry
            # with the "dynamodb." prefix. We do NOT change entry["source_id"] itself —
            # the bare name is what SQL routing / snippets use — only the doc lookup key.
            if (not meta or meta.get("status") != "ready") and source_type == "dynamodb" \
                    and not source_id.startswith("dynamodb."):
                meta = await astorage.entity_get(f"dynamodb.{source_id}")

        if meta and meta.get("status") == "ready" and meta.get("doc_s3_key"):
            entry["has_entity_doc"] = True
            # Version the cache on last_discovered_at so a re-profiled doc (same S3
            # key) is re-read instead of served stale from the lru_cache.
            pending.append((entry, meta["doc_s3_key"],
                            str(meta.get("last_discovered_at") or meta.get("updated_at") or "")))
        else:
            entry["has_entity_doc"] = False

        # Also normalize column dtypes at this enrichment boundary
        for col in entry.get("columns", []):
            if "dtype" in col:
                col["dtype"] = _normalize_col_dtype(col["dtype"])

    if not pending:
        return
    # Pass 2: load every doc concurrently, each S3 GET in a worker thread (cache
    # hits return immediately). Same fields/values as the old sequential load.
    docs = await asyncio.gather(*(
        asyncio.to_thread(_load_entity_doc_json_cached_safe, key, version)
        for _, key, version in pending
    ))
    for (entry, _, _), doc in zip(pending, docs):
        if doc:
            entry["business_description"] = doc.get("business_description", "")
            entry["quality_flags"] = doc.get("quality_flags", [])
        else:
            entry["business_description"] = ""
            entry["quality_flags"] = []


def _load_entity_doc_json(s3_key: str) -> dict | None:
    """Load the full entity doc JSON from S3. Returns None on any failure."""
    region = os.environ.get("AWS_REGION", "us-west-2")
    bucket = os.environ.get(
        "ARTIFACT_BUCKET",
        f"microvm-sandbox-artifacts-{os.environ.get('ACCOUNT_ID', 'unknown')}-{region}"
    )
    try:
        s3 = aws_clients.get_client("s3", region_name=region)
        resp = s3.get_object(Bucket=bucket, Key=s3_key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except Exception:
        return None


# Cached wrapper — entity docs change rarely (only when batch/entity_discovery reruns).
# Avoids hitting S3 on every panel open (~12 calls per render otherwise).
#
# The cache key includes a `version` token (the entity's last_discovered_at) IN
# ADDITION to the S3 key: save_entity_doc writes to a FIXED key per source, so a
# regenerated doc reuses the same key and a key-only cache would serve the STALE
# JSON forever (until a proxy restart). Threading last_discovered_at busts the
# cache the moment the doc is re-profiled. `version` is ignored inside the body.
#
# A failed/missing load (None) is NOT cached: the body raises _DocMissing instead
# (lru_cache never caches exceptions), so a transient S3 error or a doc that lands
# later is retried on the next render. Callers use _load_entity_doc_json_cached_safe.
class _DocMissing(Exception):
    """Internal: entity doc failed to load — raised so lru_cache skips caching."""


@functools.lru_cache(maxsize=64)
def _load_entity_doc_json_cached(s3_key: str, version: str = "") -> dict:
    doc = _load_entity_doc_json(s3_key)
    if doc is None:
        raise _DocMissing(s3_key)
    return doc


def _load_entity_doc_json_cached_safe(s3_key: str, version: str = "") -> dict | None:
    """Cached load that returns None (uncached) when the doc can't be loaded.
    SYNC (S3 GET on a miss) — call via asyncio.to_thread."""
    try:
        return _load_entity_doc_json_cached(s3_key, version)
    except _DocMissing:
        return None


@router.get("/datasources/entity-doc")
async def get_entity_doc(source_id: str, request: Request):
    """
    Get the full entity profile document (markdown + metadata) for a data source.
    
    Returns the complete discovery result including business_description,
    quality_flags, and the full markdown profile. Called on-demand when the
    user clicks "View Full Profile" — NOT on every panel render.
    
    Works for both global entities (S3/Athena/DynamoDB) and local files
    (session-scoped /tmp files) — checks both tables.
    """
    from proxy.storage import astorage

    session_id = request.headers.get("X-Session-Id", "")

    # Try global entity first
    meta = await astorage.entity_get(source_id)
    # DynamoDB docs are stored under "dynamodb.<table>" but the provider/catalog use
    # the bare table name — retry with the prefix (see _enrich_catalog_entries note).
    if (not meta or meta.get("status") != "ready") and not source_id.startswith("dynamodb."):
        prefixed = await astorage.entity_get(f"dynamodb.{source_id}")
        if prefixed and prefixed.get("status") == "ready":
            meta = prefixed
    # If not found globally and we have a session_id, try local file entity
    if (not meta or meta.get("status") != "ready") and session_id:
        meta = await astorage.local_entity_get(session_id, source_id)

    if not meta or meta.get("status") != "ready" or not meta.get("doc_s3_key"):
        return json_error(404, f"No entity doc found for source_id: {source_id}")

    doc = await asyncio.to_thread(_load_entity_doc_json, meta["doc_s3_key"])
    if not doc:
        return json_error(502, "Entity doc exists in DB but failed to load from S3")

    return {
        "source_id": source_id,
        "business_description": doc.get("business_description", ""),
        "quality_flags": doc.get("quality_flags", []),
        "markdown": doc.get("markdown", ""),
    }


@router.get("/datasources/catalog")
async def get_full_catalog(request: Request):
    """
    Get the full data catalog from the VM (all sources with discovered schemas).
    Proxies GET /data-catalog from the active VM for the given session.
    Returns the progressive catalog — entries may still be "pending" if discovery is in progress.

    Query params:
      refresh (default "true"): when "true", RESYNC first — re-discover cloud
        schemas on the proxy and PUSH them to the VM before reading back (the
        panel-refresh behavior). When "false", READ-ONLY — skip the push and just
        read + enrich the VM's current catalog. Read-only callers (e.g. the intel
        report enrichment) use refresh=false so they don't trigger an expensive
        cloud re-discovery + push that contends with a busy VM (which was timing
        out during post-upload deltas).
    """
    session_id = request.headers.get("X-Session-Id", "")
    if not session_id:
        return json_error(400, "X-Session-Id header required")

    refresh = request.query_params.get("refresh", "true").lower() != "false"

    vm_manager = request.app.state.vm_manager
    session_vm = await vm_manager.get_session_vm_async(session_id)
    if not session_vm:
        return json_error(404, "No active VM for this session")

    # Skip the resync PUSH on the very first fetch of a freshly-launched VM. At
    # launch we already stage the fully-formed cloud catalog to S3 and the VM loads
    # it on /run, so a just-booted VM's catalog already equals what a push would
    # send — pushing again is redundant AND races the VM's cold start (the source
    # of the launch-time "resync ReadTimeout"). After this short window, normal
    # push-then-read resumes so post-launch source changes still propagate.
    LAUNCH_RESYNC_SKIP_SEC = 45
    vm_id = session_vm.get("vm_id")
    launched_at = (vm_manager.active_microvms.get(vm_id, {}) or {}).get("launched_at", 0)
    just_launched = launched_at and (time.time() - launched_at) < LAUNCH_RESYNC_SKIP_SEC
    if just_launched:
        logger.info(
            f"Skipping resync push for session {session_id[:8]} — VM launched "
            f"{time.time() - launched_at:.0f}s ago (catalog already loaded from S3 at launch)"
        )

    # RESYNC (push-then-read): the proxy is authoritative for CLOUD schemas, so on
    # every panel refresh we re-discover them (parallel, in build_cloud_catalog_
    # entries) and PUSH the fresh, fully-formed set to the VM before reading the
    # catalog back. This is how new/removed remote sources (Athena/DynamoDB/S3/
    # Aurora) created after launch reach a live VM — the VM keeps only its local
    # /tmp entries and merges in whatever we push. Best-effort: if the push fails
    # (VM warming, transient error) we still fall through and return whatever the
    # VM currently has, so the panel degrades gracefully instead of erroring.
    #
    # Skipped for read-only callers (refresh=false) and for a just-launched VM
    # (just_launched) — neither needs to write to the VM.
    if refresh and not just_launched:
        try:
            # AWAIT the async discovery — a blocking wait here would freeze the
            # single-loop proxy (and any in-flight AI chat stream) for the whole
            # multi-second cloud fan-out. The async path lets the loop keep serving.
            # Explicit panel refresh → bypass the TTL catalog cache so the user
            # sees live schemas (force_refresh), then re-cache for the next launch.
            entries = await vm_manager.build_cloud_catalog_entries_async(
                session_id=session_id, force_refresh=True)
            token = await vm_manager.get_auth_token(session_vm["vm_id"])
            await http_clients.vm_client().post(
                f"https://{session_vm['endpoint']}/data-catalog/resync",
                headers={"X-aws-proxy-auth": token, "Content-Type": "application/json"},
                json={"entries": entries},
                timeout=10.0,
            )
        except Exception as e:
            # httpx timeout/connection errors (ReadTimeout, ConnectTimeout, PoolTimeout,
            # ConnectError, ...) stringify to an EMPTY message, so logging {e} alone
            # produced a blank cause. Log the exception TYPE (and repr) so a resync
            # timeout against a busy/cold VM is identifiable. Still best-effort: we fall
            # through and return the VM's current catalog.
            logger.warning(
                f"Catalog resync push failed for session {session_id[:8]} "
                f"(returning current VM catalog): {type(e).__name__}: {e!r}"
            )
            # Handled/degraded (we still return the VM's current catalog), but emit
            # a metric so this intermittent resync failure is visible on a dashboard.
            # Use a DEDICATED metric name — NOT UnhandledErrors — so a benign,
            # self-healing resync-push timeout doesn't inflate (or alarm) the
            # unhandled-error count that the catch-all handler owns.
            emit_error_metric("datasources", "get_full_catalog", type(e).__name__, 200,
                              metric="CatalogResyncPushFailed")

    # The VM's Lambda endpoint can return a transient 502/503 while the MicroVM is
    # still cold-starting or being rotated. If we returned that straight through,
    # enrichment (has_entity_doc → entity intel icons) would be silently skipped and
    # the frontend would show no icons until a manual refresh. Retry a few times with
    # a short backoff so a warming VM resolves without user intervention.
    RETRY_STATUSES = {502, 503, 504}
    MAX_ATTEMPTS = 4
    BACKOFF_SECONDS = 1.0

    last_resp = None
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            token = await vm_manager.get_auth_token(session_vm["vm_id"])
            resp = await http_clients.vm_client().get(
                f"https://{session_vm['endpoint']}/data-catalog",
                headers={"X-aws-proxy-auth": token},
                timeout=10.0,
            )
            if resp.status_code == 200:
                catalog = resp.json()
                # Enrich entries with entity doc metadata where available
                await _enrich_catalog_entries(catalog.get("entries", []), session_id=session_id)
                return catalog

            last_resp = resp
            if resp.status_code not in RETRY_STATUSES:
                # Non-transient (e.g. 404/400) — no point retrying.
                return Response(content=resp.text, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            last_error = e

        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(BACKOFF_SECONDS)

    if last_resp is not None:
        return Response(content=last_resp.text, status_code=last_resp.status_code, media_type="application/json")
    return json_error(502, f"Failed to reach VM: {last_error}")


@router.get("/datasources/snippet")
async def get_datasource_snippet(source_type: str, source_id: str, language: str = "python"):
    """
    Get a ready-to-run code snippet for a data source.

    Args:
        source_type: 'athena', 'dynamodb', 's3', 'local'
        source_id: Table name, S3 URI, or file path
        language: 'python' or 'sql'

    Returns:
        {"code": "...", "cell_type": "code" or "sql"}
    """
    from proxy.platform.datasources import registry

    provider = registry.get_provider(source_type)
    if not provider:
        return json_error(400, f"Unknown source type: {source_type}")

    if language == "sql":
        code = provider.get_sql_snippet(source_id)
        cell_type = "sql"
    else:
        code = provider.get_python_snippet(source_id)
        cell_type = "code"

    return {"code": code, "cell_type": cell_type}


@router.delete("/datasources/s3-file")
async def delete_datasource_s3_file(source_id: str, request: Request, source_type: str = "s3"):
    """
    Delete a source from the panel, generically, via its provider. Kept at the
    /datasources/s3-file path for back-compat (S3 files were the first deletable
    source), but now dispatches to ANY provider that opts into deletion by
    overriding is_deletable()/delete().

    S3 files (source_type="s3", the default) are restricted to keys under a
    configured deletable prefix (DATASOURCE_S3_DELETABLE_PREFIXES, e.g. user-data/).
    SageMaker endpoints (source_type="sagemaker_endpoint") tear down the endpoint +
    its config + model. The provider's is_deletable() is the authority in every
    case — this route just enforces it before calling delete().

    Args:
        source_id:   provider-specific id of the source to delete (S3 URI, endpoint
                     name, ...).
        source_type: which provider owns it (default "s3" for back-compat).

    Returns:
        {"deleted": "<source_id>"} on success. 403 if the source isn't deletable;
        400 for unknown provider; 500 if the underlying delete fails.
    """
    from proxy.platform.datasources import registry

    provider = registry.get_provider(source_type)
    if not provider:
        return json_error(400, f"Unknown data source type: {source_type}")

    # is_deletable() is a pure name/prefix check on every provider (no I/O), so it
    # runs inline; delete() does blocking boto3 work, so it is offloaded to a worker
    # thread per the provider concurrency contract (datasources/interface.py).
    if not provider.is_deletable(source_id):
        logger.warning(f"Rejected {source_type} delete (not deletable): {source_id}")
        return json_error(403, "This source is not in a deletable location.")

    try:
        await asyncio.to_thread(provider.delete, source_id)
        logger.info(f"Deleted {source_type} source: {source_id}")
    except Exception as e:
        logger.error(f"{source_type} delete failed for {source_id}: {e}")
        return json_error(500, f"Delete failed: {e}")
    finally:
        # Even a failed delete may have removed part of a prefix or an endpoint.
        registry.invalidate_datasources_cache(f"delete {source_type}")

    return {"deleted": source_id}


@router.post("/datasources/sagemaker-endpoint/lifecycle")
async def sagemaker_endpoint_lifecycle(source_id: str, action: str, request: Request):
    """
    Stop or start an INSTANCE-backed SageMaker endpoint from the panel (no live VM
    needed — this is a proxy-side control-plane call, like the delete route).

      • action="stop"  → deletes the running endpoint to pause per-instance-hour
                         billing, KEEPING its config + model so it can resume.
      • action="start" → recreates the endpoint from its retained config.

    The provider's supports_lifecycle() is the authority: it returns True ONLY for
    this project's instance endpoints (serverless has nothing to stop, and foreign
    endpoints are out of scope), so this route enforces it before acting.

    Args:
        source_id: the endpoint name (microvm-ep-*).
        action:    "stop" or "start".

    Returns {"status": "<msg>", "source_id": ...} or an error Response.
    """
    from proxy.platform.datasources import registry

    if action not in ("stop", "start"):
        return json_error(400, f"Invalid action: {action!r} (expected 'stop' or 'start')")

    provider = registry.get_provider("sagemaker_endpoint")
    if not provider:
        return json_error(400, "SageMaker endpoint provider not registered")

    # supports_lifecycle() may call describe_endpoint_config (on a config-cache
    # miss) and lifecycle_action() does blocking boto3 work, so both are offloaded
    # to a worker thread per the provider concurrency contract (interface.py).
    if not await asyncio.to_thread(provider.supports_lifecycle, source_id):
        logger.warning(f"Rejected endpoint lifecycle {action} (unsupported): {source_id}")
        return json_error(403, "This endpoint does not support stop/start "
                               "(only this project's instance endpoints do).")

    try:
        status = await asyncio.to_thread(provider.lifecycle_action, source_id, action)
        logger.info(f"Endpoint lifecycle {action} on {source_id}: {status}")
    except Exception as e:
        logger.error(f"Endpoint lifecycle {action} failed for {source_id}: {e}")
        return json_error(500, f"{action.capitalize()} failed: {e}")
    finally:
        registry.invalidate_datasources_cache(f"sagemaker {action}")

    return {"status": status, "source_id": source_id, "action": action}


# Only secrets under this name prefix are injectable into MicroVMs. The execution
# role's secretsmanager:GetSecretValue is scoped to `microvm/*`, so we filter the
# listing to the same prefix — otherwise the dropdown would show secrets the VM
# can't actually read (confusing) and leak the account's full secret inventory.
SECRETS_NAME_PREFIX = "microvm/"


@router.get("/secrets")
async def list_secrets(request: Request):
    """List injectable secrets from AWS Secrets Manager (names only, not values).

    Scoped to the `microvm/` name prefix to match the execution role's read scope."""
    try:
        # Blocking Secrets Manager paging → worker thread.
        return await asyncio.to_thread(_list_secrets_sync)
    except Exception as e:
        logger.warning(f"Failed to list secrets: {e}")
        return json_error(502, str(e), secrets=[])


def _list_secrets_sync() -> dict:
    """SYNC body of GET /secrets (run via asyncio.to_thread). Names/metadata only,
    never values. Raises on AWS errors; the route maps them to the error body."""
    sm = aws_clients.get_client("secretsmanager", region_name=AWS_REGION)
    secrets = []
    paginator = sm.get_paginator("list_secrets")
    # Server-side name-prefix filter so we never even enumerate non-microvm/ secrets.
    filters = [{"Key": "name", "Values": [SECRETS_NAME_PREFIX]}]
    for page in paginator.paginate(Filters=filters, MaxResults=50):
        for secret in page.get("SecretList", []):
            # Defensive: name filter is a prefix match, so confirm it really
            # starts with our prefix before surfacing it.
            if not secret.get("Name", "").startswith(SECRETS_NAME_PREFIX):
                continue
            secrets.append({
                "name": secret["Name"],
                "arn": secret["ARN"],
                "description": secret.get("Description", ""),
                "last_changed": secret.get("LastChangedDate", "").isoformat() if secret.get("LastChangedDate") else None,
            })
    return {"secrets": secrets}


def _secret_keys_sync(secret_id: str) -> dict:
    """SYNC body of GET /secrets/keys AFTER the prefix check (run via
    asyncio.to_thread). Returns key NAMES only — the secret value never leaves
    this function. Raises on AWS errors; the route maps them to the error body."""
    sm = aws_clients.get_client("secretsmanager", region_name=AWS_REGION)
    resp = sm.get_secret_value(SecretId=secret_id)
    secret_str = resp.get("SecretString", "")
    try:
        data = json.loads(secret_str)
        if isinstance(data, dict):
            return {"keys": list(data.keys()), "type": "json"}
        else:
            return {"keys": [], "type": "plaintext"}
    except (json.JSONDecodeError, TypeError):
        return {"keys": [], "type": "plaintext"}


@router.get("/secrets/keys")
async def list_secret_keys(secret_id: str = "", request: Request = None):
    """Fetch keys from a JSON secret (returns key names only, not values)."""
    if not secret_id:
        return json_error(400, "secret_id query param required", keys=[])
    # Enforce the microvm/ name prefix BEFORE any AWS call so an arbitrary account
    # secret (e.g. prod-db-credentials, or microvm-internal/env-master-key which
    # deliberately lives OUTSIDE microvm/) can't be probed. This matches both a bare
    # name (microvm/foo) and a full ARN (arn:...:secret:microvm/foo-abc123) because
    # the name portion carries the prefix.
    if SECRETS_NAME_PREFIX not in secret_id:
        return json_error(403, "secret_id outside the allowed prefix", keys=[])
    try:
        # Only reached after the prefix check above; the AWS call runs off-loop.
        return await asyncio.to_thread(_secret_keys_sync, secret_id)
    except Exception as e:
        logger.warning(f"Failed to get secret keys: {e}")
        return json_error(502, str(e), keys=[])


def _coerce_notebook_id(raw):
    """Notebook PKs are DB-generated integers. Coerce the wire value (which may
    arrive as a string) to int; return None for missing/garbage so callers 400."""
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/notebooks/{notebook_id}/env")
async def get_notebook_env(notebook_id: str, request: Request = None):
    """Return a notebook's PERSISTED env-var PRESENCE markers + secret references.

    Response: {"env_vars": {KEY: {"set": true|false}}, "secrets": [{name, arn, envVar, secretKey?}]}
    Env-var VALUES are WRITE-ONLY: the stored plaintext is NEVER returned over the
    wire — each env var collapses to a presence marker {"set": bool}, mirroring how
    secrets are already value-free. (The launch path reads real decrypted values via
    storage.notebook_env_get directly; this HTTP view does not.) Secret VALUES are
    never stored or returned either. Empty defaults when nothing is saved."""
    from proxy.storage import astorage
    nb_id = _coerce_notebook_id(notebook_id)
    if nb_id is None:
        return json_error(400, "Invalid notebook_id")
    try:
        data = await astorage.notebook_env_get(nb_id)
        # Collapse decrypted env values to presence markers so a plaintext value
        # never crosses the HTTP boundary. storage.notebook_env_get is UNCHANGED —
        # the launch path still gets real values from it directly.
        env_markers = {k: {"set": bool(v)} for k, v in (data.get("env_vars", {}) or {}).items()}
        return {"env_vars": env_markers, "secrets": data.get("secrets", []) or []}
    except Exception as e:
        logger.warning(f"Failed to get notebook env for {nb_id}: {e}")
        return json_error(500, str(e), env_vars={}, secrets=[])


@router.put("/notebooks/{notebook_id}/env")
async def set_notebook_env(notebook_id: str, request: Request):
    """MERGE a notebook's persisted env vars; whole-object replace of secrets.

    Body: {"env_vars": {KEY: value}, "secrets": [{name, arn, envVar, secretKey?}],
           "remove": [KEY, ...]}
    Because the GET now returns presence markers (no plaintext values), the client
    never holds the other vars' values to resend. So env_vars is MERGED: only the
    keys present in the body are updated; every other stored env var is left intact.
    An optional top-level `remove` array deletes those keys first (for a UI delete).
    Secrets remain a WHOLE-OBJECT replace (they carry no value). The proxy re-injects
    the persisted env on every launch/restore so it survives suspend/resume and
    relaunch. To also apply to the CURRENTLY running VM without relaunch, the client
    separately calls /proxy/set-env + /proxy/set-variable."""
    from proxy.storage import astorage
    nb_id = _coerce_notebook_id(notebook_id)
    if nb_id is None:
        return json_error(400, "Invalid notebook_id")
    body = await request.json()
    env_vars = body.get("env_vars", {}) or {}
    secrets = body.get("secrets", []) or []
    remove = body.get("remove", []) or []
    if not isinstance(env_vars, dict) or not isinstance(secrets, list) or not isinstance(remove, list):
        return json_error(400, "env_vars must be an object, secrets and remove arrays")
    try:
        await astorage.notebook_env_set(nb_id, env_vars, secrets,
                                        merge_env=True, remove_env_keys=remove)
        return {"success": True, "env_vars": env_vars, "secrets": secrets}
    except Exception as e:
        logger.warning(f"Failed to set notebook env for {nb_id}: {e}")
        return json_error(500, str(e))


@router.post("/datasources/promote-attachment")
async def promote_attachment(request: Request):
    """Promote a chat attachment to a session-scoped data source.

    COPIES a tabular file the user attached in chat (stored at
    attachments/<session_id>/<uuid>/<name>) to user-data/<session_id>/<name>, where
    the S3 provider's session overlay discovers it — so it appears in the Data
    Sources panel and becomes a queryable/joinable source for THIS session only. The
    original attachment is left intact (copy, not move) so the chat transcript chip
    still resolves.

    Called by the chat agent's promote_attachment tool when the user asks to keep /
    reuse an attached file or to run analysis joining it with other data sources.

    Body: {"session_id": "<uuid>", "s3_uri": "s3://.../attachments/<session>/.../f.csv"}
    Returns: {"promoted": true, "s3_uri": "<new user-data uri>", "name": "<leaf>"}.
    400 for a validation failure (wrong prefix, non-tabular, missing session);
    502 if the S3 copy fails.
    """
    from proxy.platform.datasources import registry

    body = await request.json()
    session_id = (body.get("session_id") or request.headers.get("X-Session-Id") or "").strip()
    s3_uri = (body.get("s3_uri") or "").strip()
    if not session_id or not s3_uri:
        return json_error(400, "session_id and s3_uri are required")

    provider = registry.get_provider("s3")
    if provider is None:
        return json_error(400, "s3 provider unavailable")
    try:
        # Blocking S3 head/copy → worker thread (same ValueError/Exception mapping).
        result = await asyncio.to_thread(provider.promote_attachment, s3_uri, session_id)
    except ValueError as e:
        # Validation failure (bad prefix / non-tabular / wrong session).
        logger.warning(f"promote-attachment rejected (session {session_id[:8]}...): {e}")
        return json_error(400, str(e))
    except Exception as e:
        logger.error(f"promote-attachment S3 copy failed (session {session_id[:8]}...): {e}")
        return json_error(502, f"promotion failed: {e}")
    # The promoted file is a new session-overlay source: drop cached discovery.
    registry.invalidate_datasources_cache("promote-attachment")

    # Fire-and-forget: generate the per-file entity intel doc for the promoted
    # object so it shows the ✨ intel icon in the Data Sources panel, just like every
    # other discovered source. This is SINGLE-ENTITY discovery only (profile + doc +
    # upsert) — NOT the delta Workbook Intel regen. Runs in a background thread so
    # the promote call (which the agent awaits) returns immediately; a discovery
    # failure is logged and never affects the promotion result.
    _trigger_entity_intel_async(result["s3_uri"], result["key"])

    return {"promoted": True, "s3_uri": result["s3_uri"], "name": result["name"]}


# Bounded pool for post-promote entity-intel generation. Each promoted file's
# discovery makes a Bedrock call (generate_entity_markdown), so when the user
# promotes several files in one turn (up to ATTACHMENT_MAX_PER_MESSAGE) we cap the
# concurrent discoveries here instead of spawning an unbounded thread per promote.
# Extra promotions QUEUE on this executor rather than firing simultaneously.
_ENTITY_INTEL_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="promote-entity-intel"
)


def _trigger_entity_intel_async(s3_uri: str, key: str) -> None:
    """Kick off single-entity discovery for a promoted S3 file (bounded pool).

    Builds the S3 entity dict (the shape batch.entity_discovery expects) and calls
    discover_entity, which profiles the file, generates the markdown/business
    description, saves the doc to S3, and upserts status=ready — after which
    _enrich_catalog_entries sets has_entity_doc=True and the panel shows the sparkle.
    Submitted to a bounded executor so N simultaneous promotions don't fan out into
    N concurrent Bedrock calls. Best-effort: any failure is logged, never raised."""
    def _run():
        try:
            from batch.entity_discovery import discover_entity
            from proxy.storage import storage as _sync_storage
            bucket = os.environ.get(
                "ARTIFACT_BUCKET",
                f"microvm-sandbox-artifacts-{os.environ.get('ACCOUNT_ID', 'unknown')}-{AWS_REGION}",
            )
            entity = {
                "source_id": s3_uri,      # s3://bucket/user-data/<session>/<name>
                "source_type": "s3",
                "bucket": bucket,
                "key": key,
                # fetch_and_profile() keys off the file extension to pick the reader
                # (csv/parquet/json) — the real S3 discovery includes it, so we must too.
                "extension": key.rsplit(".", 1)[-1].lower() if "." in key else "",
            }
            res = discover_entity(entity, bucket, AWS_REGION, _sync_storage)
            logger.info(f"[promote] entity intel for {s3_uri}: {res.get('status')}")
        except Exception as e:
            logger.warning(f"[promote] entity intel generation failed for {s3_uri}: {e}")

    try:
        _ENTITY_INTEL_POOL.submit(_run)
    except Exception as e:
        logger.warning(f"[promote] could not schedule entity intel for {s3_uri}: {e}")
