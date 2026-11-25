"""
MicroVM Lifecycle Hooks.

Part of: app.platform (infrastructure layer)

These endpoints are called by the AWS Lambda MicroVMs runtime (not by user code)
at key points in the MicroVM lifecycle:

  /ready     — Image build: app is ready for snapshot
  /validate  — Image build: validation hook
  /run       — MicroVM started: initialize per-session state (restore if asked)
  /suspend   — Going idle: checkpoint state to S3 (dirty-flag), then freeze
  /resume    — Waking up: restore connections, validate state
  /terminate — Shutting down (while RUNNING): checkpoint to S3 if dirty

============================================================================
STATE PERSISTENCE MODEL — why we checkpoint on /suspend (read before editing)
============================================================================
Empirically verified against real VMs:

  The /terminate hook fires ONLY when the VM is RUNNING at kill time.
  A VM that is SUSPENDED when it is terminated does NOT fire /terminate —
  regardless of whether the killer is suspendedDurationSeconds OR
  maximumDurationInSeconds. (/suspend and control-plane terminate of a RUNNING
  VM both fire reliably; a suspended-then-killed VM does not.)

Consequence: if we only saved in /terminate, any VM that went idle → suspended
and was later reclaimed while suspended would lose all state silently. That is
the common case for an abandoned notebook.

Design (Phase 1): treat /suspend as the last reliable save point.
  • /suspend  → checkpoint to S3 (the state is about to be frozen; if the VM is
                later killed while suspended, /terminate never runs, so this is
                our only chance).
  • /terminate → still checkpoints, for the RUNNING → terminate path (user
                closes the notebook, or the rotator terminates a running VM).

Both saves are gated by a DIRTY FLAG (CheckpointManager.is_dirty()) so we only
write to S3 when the namespace/packages actually changed since the last save —
an idle VM that suspends without new work writes nothing.

CRITICAL: the /suspend save is AWAITED TO COMPLETION, never fire-and-forget.
The platform freezes memory+disk the instant the hook returns 200, so a
background/detached save would be snapshotted mid-upload and never finish. We
offload the blocking boto3/dill work to a thread so the event loop isn't
blocked, but we still await it before returning 200.

The AWS reference sample (aws-samples/sample-lambda-microvm-claude-managed-
agents) sidesteps this entirely by keeping VMs disposable (self-terminate while
RUNNING, empty suspend/terminate hooks, state posted out during work). We keep
the richer "resume the same kernel later" UX, so we must persist across the
suspend boundary — hence this model.
"""

import json
import os
import asyncio
import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/aws/lambda-microvms/runtime/v1", tags=["lifecycle"])


def _reset_boto3_credentials():
    """Clear boto3's process-global default session so the NEXT AWS call resolves
    credentials fresh under THIS VM's assigned execution role.

    Why this is required: cold-start warmup (app.server.run_all_warmups) touches
    boto3 at IMAGE-BUILD time, which happens under the BUILD role. That resolves
    and caches build-role credentials into boto3.DEFAULT_SESSION, and that session
    is frozen into the Firecracker snapshot. Every VM launched from the image
    restores the snapshot and inherits the stale build-role credentials — so every
    data call (S3/Glue/Athena/DynamoDB) and checkpoint authenticates as the build
    role instead of the execution role the platform actually assigned. Resetting
    the default session at /run and /resume forces re-resolution against the live
    container-credentials endpoint (the correct execution role). This is also what
    makes distinct per-VM execution roles work at all.

    The shared checkpoint S3 client (app.platform.checkpoint.manager) caches
    credentials too, so it is dropped here as well and rebuilt on next use. The
    notebook client cache (app.notebook.aws_clients) is cleared the same way.
    """
    try:
        import boto3
        boto3.DEFAULT_SESSION = None
        logger.info("   Reset boto3 default session — credentials will re-resolve under the execution role")
    except Exception as e:
        logger.warning(f"   boto3 default-session reset failed (non-fatal): {e}")
    try:
        from app.platform.checkpoint.manager import reset_checkpoint_s3_client
        reset_checkpoint_s3_client()
    except Exception as e:
        logger.warning(f"   checkpoint S3 client reset failed (non-fatal): {e}")
    try:
        from app.notebook.aws_clients import reset_aws_clients
        reset_aws_clients()
    except Exception as e:
        logger.warning(f"   notebook AWS client cache reset failed (non-fatal): {e}")


def _prewarm_s3_connection():
    """Warm this VM's S3 connection in the background so the NEXT real S3 call
    (a /restore-state GET) doesn't pay the cold handshake.

    On a freshly-resumed pool spare, the FIRST S3 request pays DNS + TLS +
    execution-role credential resolution against the container-credentials
    endpoint — a one-time cost that occasionally STALLS for many seconds and was
    the dominant term in a slow restore (a 20 KB object took ~19s: ~all setup, ~no
    transfer). We can't make that handshake instant, but we CAN move it OFF the
    restore critical path by doing a cheap throwaway S3 call now (right after
    /rebind resets creds, before the proxy issues /restore-state). The connection
    + creds are then cached in boto3's session, so restore's GETs are hot.

    Fire-and-forget on a daemon thread: the rebind response must not wait on it,
    and a failure here is harmless (restore would just pay the handshake itself,
    i.e. today's behavior). Uses the SHARED checkpoint S3 client (bounded
    timeout/retry config, tcp_keepalive) — the same client restore/save use, so
    the warmed connection pool is the one restore's GETs actually reuse."""
    def _warm():
        try:
            from app.platform.checkpoint.manager import get_checkpoint_s3_client
            bucket = os.environ.get("ARTIFACT_BUCKET")
            s3 = get_checkpoint_s3_client()
            # A HEAD on the bucket is the cheapest call that still forces the full
            # DNS+TLS+creds handshake. If we don't know the bucket, list_buckets
            # still warms the connection + creds (just not the bucket's region hint).
            if bucket:
                s3.head_bucket(Bucket=bucket)
            else:
                s3.list_buckets()
            logger.info("   🔥 S3 connection pre-warmed — restore GETs will skip the cold handshake")
        except Exception as e:
            logger.info(f"   S3 pre-warm skipped (non-fatal): {e}")
    threading.Thread(target=_warm, daemon=True, name="s3-prewarm").start()


def inject_secrets_into_env(secrets: list) -> dict:
    """Resolve a list of secret REFERENCES from AWS Secrets Manager and write the
    values into os.environ. Shared by the /run launch hook AND the runtime
    /refresh-secrets endpoint so a launch and a live refresh resolve identically.

    `secrets` is a list of {name?, arn, envVar, secretKey?}:
      • arn        — Secrets Manager ARN/id to fetch (required),
      • envVar     — the os.environ key to set (required),
      • secretKey  — optional JSON key to extract from a JSON secret; when omitted
                     the whole SecretString is injected.

    Fetches are cached per-ARN within one call (a secret feeding several env vars is
    fetched once). Each secret is best-effort: one that fails to resolve is recorded
    and skipped rather than aborting the batch. Values are NEVER logged. Returns a
    summary {"injected": [envVar, ...], "failed": [{envVar, error}, ...]} so the
    caller can report what changed without exposing values."""
    import boto3
    injected, failed = [], []
    if not secrets:
        return {"injected": injected, "failed": failed}
    sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    secret_cache: dict = {}
    for secret in secrets:
        env_var_name = (secret or {}).get("envVar", "")
        arn = (secret or {}).get("arn", "")
        secret_key = (secret or {}).get("secretKey", "")
        if not env_var_name or not arn:
            continue
        try:
            if arn not in secret_cache:
                resp = sm.get_secret_value(SecretId=arn)
                secret_cache[arn] = resp.get("SecretString", "")
            secret_value = secret_cache[arn]

            if secret_key:
                # Extract a specific key from a JSON secret; fall back to the whole
                # value if it isn't JSON (matches launch-time behavior).
                try:
                    data = json.loads(secret_value)
                    os.environ[env_var_name] = str(data.get(secret_key, ""))
                    logger.info(f"   SECRET: {env_var_name} ← {secret.get('name', arn)}[{secret_key}]")
                except (json.JSONDecodeError, TypeError):
                    os.environ[env_var_name] = secret_value
                    logger.info(f"   SECRET: {env_var_name} ← {secret.get('name', arn)} (plain)")
            else:
                os.environ[env_var_name] = secret_value
                logger.info(f"   SECRET: {env_var_name} ← {secret.get('name', arn)}")
            injected.append(env_var_name)
        except Exception as e:
            logger.warning(f"   Failed to fetch secret {arn}: {e}")
            failed.append({"envVar": env_var_name, "error": str(e)})
    return {"injected": injected, "failed": failed}


def _apply_session_identity(app, payload: dict, microvm_id=None) -> None:
    """Overwrite THIS VM's session identity + per-session config from a
    runHookPayload-shaped dict. Shared by hook_run (/run, fresh launch) and
    rebind (/rebind, pool grant) so the two code paths can never drift.

    Sets, in order: session_state identity fields (session_id, notebook_id,
    notebook_name, persistence_mode, checkpoint_enabled, artifacts_bucket,
    and microvm_id when `microvm_id` is not None); the MICROVM_* os.environ
    identity mirror; env var + Secrets Manager injection; the
    MICROVM_USER_ENV_KEYS allowlist map; the data catalog (inline entries or
    S3-staged) + local /tmp discovery.

    Deliberately does NOT set `started_at`: a pool spare's real AWS 8h
    lifetime clock started when it was launched into the pool, NOT when it is
    later rebound to a real session, so rebinding must never reset or extend
    it. Only hook_run's /run wrapper sets started_at, exactly once, at the
    true boot moment — this helper only republishes whatever is already in
    session_state["started_at"] into the env mirror. Also does NOT handle
    restore_from or _reset_boto3_credentials() — both are launch-only /
    caller concerns, not identity-setting.

    `microvm_id`: pass the control-plane id on a fresh /run
    (body["microvmId"]). Pass None on /rebind — the VM's own microvm_id is
    immutable post-boot, so the existing session_state["microvm_id"] is left
    untouched.
    """
    session_state = app.state.session_state

    if microvm_id is not None:
        session_state["microvm_id"] = microvm_id

    session_state["session_id"] = payload.get("session_id")
    session_state["notebook_id"] = payload.get("notebook_id")
    session_state["notebook_name"] = payload.get("notebook_name")
    session_state["checkpoint_enabled"] = payload.get("checkpoint_enabled", False)
    session_state["persistence_mode"] = payload.get("persistence_mode", "checkpoint")
    session_state["artifacts_bucket"] = payload.get("artifacts_bucket")

    # Mirror the session identity into os.environ so the notebook helper library
    # (app.notebook.helpers.this_notebook) can read it at CALL time without importing
    # app.server's session_state (which would be a circular import). Same pattern as
    # the AWS/Aurora config the helpers already read from the environment. Values are
    # re-published on every /run (launch, reconnect, rotation) AND /rebind (pool
    # grant) so they stay current.
    _identity_env = {
        "MICROVM_ID": session_state.get("microvm_id"),
        "MICROVM_SESSION_ID": session_state.get("session_id"),
        "MICROVM_NOTEBOOK_ID": session_state.get("notebook_id"),
        "MICROVM_NOTEBOOK_NAME": session_state.get("notebook_name"),
        "MICROVM_PERSISTENCE_MODE": session_state.get("persistence_mode"),
        # Republish whatever started_at ALREADY is — this helper never sets it
        # (see docstring above).
        "MICROVM_STARTED_AT": session_state.get("started_at"),
        # This VM's OWN memory tier + arch, from the launch/rebind payload (the
        # proxy knows them exactly; the VM has no other source). The log readers
        # (app.notebook.helpers.logs) build this VM's own CloudWatch log group —
        # /aws/lambda-microvms/{IMAGE_BASE_NAME}-{arch}-{mem} — from these, so a
        # no-arg read_logs()/tail_logs() can default to "this notebook's logs"
        # even when Aurora analytics isn't configured (the graceful fallback).
        "MICROVM_MEMORY_MIB": payload.get("memory_mib"),
        "MICROVM_ARCH": payload.get("architecture"),
    }
    for _k, _v in _identity_env.items():
        if _v is None or _v == "":
            os.environ.pop(_k, None)   # unset stale value from a prior session
        else:
            os.environ[_k] = str(_v)

    logger.info(f"   MicroVM ID: {session_state['microvm_id']}")
    logger.info(f"   Session: {session_state['session_id']}")
    logger.info(f"   Checkpoint enabled: {session_state['checkpoint_enabled']}")

    # Inject environment variables (direct values + secrets from Secrets Manager).
    # The secret-resolution logic is shared with the runtime /refresh-secrets
    # endpoint (see inject_secrets_into_env) so a launch and a live refresh resolve
    # secrets identically.
    try:
        env_vars = payload.get("env_vars", {})
        secrets = payload.get("secrets", [])

        # Direct env vars — inject immediately
        for key, value in env_vars.items():
            os.environ[key] = str(value)
            logger.info(f"   ENV: {key} = ****")

        # Secrets Manager — (re-)fetch values and inject
        if secrets:
            inject_secrets_into_env(secrets)
    except Exception as e:
        logger.warning(f"   Failed to inject env vars/secrets: {e}")

    # Record the NAMES (never values) of the USER-configured env/secret keys, TAGGED
    # BY SOURCE, so the in-VM list_env()/list_secret() helpers show exactly what THIS
    # notebook set and distinguish a Secrets-Manager secret from a direct env var.
    # The proxy computed this map BEFORE merging in the platform config
    # (launch_env_vars), so it excludes ATHENA_*/AURORA_*/BEDROCK_*/ARTIFACT_BUCKET/…
    # — which is why we read `user_env_keys` from the payload rather than re-deriving
    # from `env_vars` (which here is the MERGED platform+user dict and would wrongly
    # tag every platform var as a user 'env'). Published verbatim as a JSON
    # {name: "secret"|"env"} map.
    #
    # Deliberately OUTSIDE the inject try/except above: the key list depends only on
    # the payload, not on injection succeeding, so even a Secrets-Manager fetch
    # failure must not leave list_env()/list_secret() blank. Its own guard so a
    # malformed map can't break the hook either.
    # ALWAYS publish the map, even when empty: an empty JSON object {} means "this
    # notebook configured NOTHING" (authoritative), which is DIFFERENT from the var
    # being absent (an old VM image with no map at all). list_env()/list_secret()
    # treat a present map as the allowlist and show ONLY its keys — so an empty map
    # correctly yields ZERO user vars. If we instead popped the var here, those
    # helpers would fall back to the system-var DENYLIST, which can't catch every
    # runtime var the native stack sets (KMP_*, OMP_*, …) and so leaks them as
    # bogus "user env vars". Publishing {} closes that leak.
    try:
        _key_sources = payload.get("user_env_keys") or {}
        if not isinstance(_key_sources, dict):
            _key_sources = {}
        os.environ["MICROVM_USER_ENV_KEYS"] = json.dumps(
            {str(k): str(v) for k, v in _key_sources.items()}
        )
    except Exception as e:
        logger.warning(f"   Failed to record user env-key map: {e}")

    # Data catalog: the proxy is authoritative for CLOUD sources — it discovers
    # them + their schemas and pushes them fully-formed as `catalog_entries`. The
    # VM loads those verbatim and separately self-discovers local /tmp files (the
    # only sources it can read itself).
    catalog = app.state.data_catalog
    catalog_entries = payload.get("catalog_entries")
    catalog_s3_key = payload.get("catalog_s3_key")
    if catalog_entries is None and catalog_s3_key:
        # Preferred path: the proxy stages the fully-formed entries in S3 (the
        # runHookPayload is capped at 4096 bytes, so they can't be inlined) and
        # passes only this key. Download + parse under THIS VM's execution role.
        bucket = payload.get("artifact_bucket") or payload.get("artifacts_bucket")
        if bucket:
            try:
                import boto3
                s3 = boto3.client("s3")
                obj = s3.get_object(Bucket=bucket, Key=catalog_s3_key)
                catalog_entries = json.loads(obj["Body"].read().decode("utf-8"))
                logger.info(f"   📊 Fetched {len(catalog_entries)} cloud catalog entries from s3://{bucket}/{catalog_s3_key}")
            except Exception as e:
                logger.warning(f"   Failed to fetch staged catalog from s3://{bucket}/{catalog_s3_key}: {e}")
        else:
            logger.warning("   catalog_s3_key present but no artifact_bucket in payload — skipping cloud catalog load")
    if catalog_entries is not None:
        logger.info(f"   📊 Loading {len(catalog_entries)} proxy-pushed cloud catalog entries")
        try:
            catalog.load_cloud_entries(catalog_entries)
        except Exception as e:
            logger.warning(f"   Failed to load pushed catalog entries: {e}")
    else:
        # Backward-compat: an older proxy may still send the enumerate-only
        # `data_sources` shape. We no longer discover cloud schemas on the VM, so
        # just log and rely on the next panel-refresh resync to populate them.
        legacy = payload.get("data_sources")
        if legacy:
            logger.info("   📊 Legacy data_sources payload received; cloud schemas now come from the proxy resync push")

    # Always self-discover local /tmp files in the background.
    logger.info("   📊 Starting local /tmp file discovery")
    catalog.start_local_discovery()


# Separate router for proxy-facing endpoints (no prefix — called directly by our proxy)
proxy_router = APIRouter(tags=["rotation"])


@router.post("/ready")
async def hook_ready(request: Request):
    """Called during image build — signals app is ready for snapshot."""
    logger.info("🔧 HOOK /ready — App initialized, ready for snapshot")
    return {"status": "ready"}


@router.post("/validate")
async def hook_validate(request: Request):
    """Called during image build — validates the image."""
    executor = request.app.state.executor
    logger.info("🔧 HOOK /validate — Running validation")
    result = executor.execute("print('Sandbox validation: OK')")
    executor.reset()
    return {
        "status": "valid",
        "validation_output": result.output,
    }


@router.post("/run")
async def hook_run(request: Request):
    """
    Called when this MicroVM starts from snapshot.

    The runHookPayload contains a JSON string with session config:
    - notebook_name: display name
    - session_id: unique session ID for checkpoint/restore
    - restore_from: session_id to restore state from (optional)
    - artifacts_bucket: S3 bucket name for checkpoint storage

    IMPORTANT: No external traffic reaches the app until this returns 200.
    """
    session_state = request.app.state.session_state

    # FIRST: drop any snapshot-frozen boto3 credentials (from build-time warmup)
    # so all AWS access below (restore, secret fetch) and in user cells resolves
    # under THIS VM's execution role, not the stale build role. See helper docstring.
    _reset_boto3_credentials()

    body = await request.json()
    session_state["microvm_id"] = body.get("microvmId")
    # started_at is set ONLY here, exactly once, at the true boot moment — NOT
    # inside _apply_session_identity. A pool spare's real AWS 8h lifetime clock
    # starts when it is first launched (here), not when it is later rebound to
    # a real session (/rebind never touches this field). See
    # _apply_session_identity's docstring for the full reasoning.
    session_state["started_at"] = datetime.now(timezone.utc).isoformat()

    run_payload = body.get("runHookPayload", "")
    restore_from = None
    payload = {}
    try:
        payload = json.loads(run_payload)
        # Preserve the original `payload.get("session_id", run_payload)` fallback
        # (fall back to the raw payload string when the key is absent) before
        # handing `payload` to the shared helper, which has no access to the raw
        # string and so has no equivalent fallback of its own.
        if "session_id" not in payload:
            payload["session_id"] = run_payload
        restore_from = payload.get("restore_from")
        _apply_session_identity(request.app, payload, microvm_id=session_state["microvm_id"])
    except (json.JSONDecodeError, TypeError):
        session_state["session_id"] = run_payload
        session_state["checkpoint_enabled"] = False
        session_state["persistence_mode"] = "checkpoint"
        # Legacy payload: the raw string is the session id. The helper sets
        # session_state["session_id"] from payload["session_id"], so seed it with
        # the raw string. `payload` is {} after a JSONDecodeError; when the string
        # parsed to a non-object (a bare number or quoted string), json.loads
        # already rebound `payload` to that value and the assignment below raises.
        payload["session_id"] = run_payload
        _apply_session_identity(request.app, payload, microvm_id=session_state["microvm_id"])

    # Single greppable identity banner so a CloudWatch stream is self-describing:
    # which notebook/session/VM this log belongs to, its memory tier + image, and
    # the persistence mode. Filter on "VM SESSION" to find the header of any stream.
    # tier/arch/image come from the runHookPayload (the proxy knows them exactly at
    # launch; the VM has no other source), with env as a fallback.
    _mem_mb = payload.get("memory_mib") or os.environ.get("DEFAULT_IMAGE_MEMORY_MB") or "?"
    _arch = payload.get("architecture") or os.environ.get("DEFAULT_IMAGE_ARCH") or "?"
    _image = payload.get("image") or os.environ.get("MICROVM_IMAGE_ARN") or "?"
    _region = os.environ.get("AWS_REGION", "?")
    logger.info(
        f"   🏷  VM SESSION session={session_state.get('session_id')} "
        f"notebook={session_state.get('notebook_name')!r} nb_id={session_state.get('notebook_id')} "
        f"microvm={session_state.get('microvm_id')} tier={_mem_mb}MB arch={_arch} "
        f"region={_region} persistence={session_state.get('persistence_mode')} "
        f"checkpoint={session_state.get('checkpoint_enabled')} restore_from={restore_from or '-'} "
        f"image={_image}"
    )

    # Restore from a previous session checkpoint if requested. Launch-only:
    # uses the local `restore_from` var (not session_state), per
    # session-rebind-feasibility.md §1 point 5 — a /rebind caller follows up
    # with the EXISTING POST /restore-state instead (see that handler below).
    if restore_from:
        logger.info(f"   Restoring from session: {restore_from}")
        request.app.state.checkpoint_manager.restore(restore_from)

    return {"status": "running", "session_id": session_state["session_id"]}


@router.post("/suspend")
async def hook_suspend(request: Request):
    """
    Called BEFORE suspend (idle timeout or explicit suspend).
    Memory + disk will be frozen after this returns.

    CHECKPOINT ON SUSPEND (checkpoint mode only):
    A MicroVM that is terminated while SUSPENDED does NOT fire the /terminate
    hook (verified empirically — the hook only fires when the VM is RUNNING at
    kill time). So /suspend is the last reliable moment to persist state before
    a suspended VM can be silently reclaimed by suspendedDurationSeconds /
    maximumDurationInSeconds. We therefore checkpoint here.

    This save is AWAITED TO COMPLETION (not fire-and-forget): the platform
    freezes memory + disk the instant this hook returns 200, so a background
    task would be frozen mid-upload and never finish. We offload the blocking
    boto3/dill work to a thread (asyncio.to_thread) so the event loop isn't
    blocked, but we still wait for it before returning.

    Dirty-flag: if nothing changed since the last successful checkpoint
    (namespace + packages), we skip the save entirely — an idle VM that suspends
    without new work writes nothing to S3.

    TIME-BOUNDED, NEVER FAILS THE HOOK: the platform terminates the VM (state
    lost) when this hook times out (suspendTimeoutInSeconds, 60 s in
    scripts/build_all_images.sh) or fails. The save therefore runs with a
    SUSPEND_SAVE_BUDGET_SECONDS deadline (_bounded_suspend_save) and the hook
    always returns 200: on a timeout, an out-of-memory skip or any exception it
    logs a WARNING, keeps the previous checkpoint (the manifest of a stopped
    save is never written), leaves the state dirty so the next save retries,
    and lets the VM suspend with its memory intact. The outcome is kept in
    checkpoint_manager.last_suspend_save (GET /vm-status, the VM detail card).
    """
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    session_state["suspend_count"] = session_state.get("suspend_count", 0) + 1

    try:
        stats = executor.get_stats()
        logger.info(f"💤 HOOK /suspend — Going to sleep")
        logger.info(f"   Executions so far: {stats['execution_count']}")
        logger.info(f"   Variables in namespace: {stats['variables_count']}")
    except Exception as e:
        logger.warning(f"💤 HOOK /suspend — Going to sleep (stats unavailable: {e})")

    # Persist state before the freeze, in BOTH persistence modes:
    #  - checkpoint mode: this is the primary save point (suspended-then-killed
    #    VMs never fire /terminate).
    #  - eternal mode: an idle eternal VM can be suspended and then reclaimed
    #    while suspended BEFORE its rotation happens — /terminate won't fire and
    #    the rotator won't have run, so without this save its state would be
    #    lost. (Reactive rotation then relaunches+restores from THIS checkpoint
    #    on the next request.) The dirty-flag keeps this cheap when idle.
    # Gated only by: checkpoint enabled + a session_id exists + state is dirty.
    if (
        session_state.get("checkpoint_enabled")
        and session_state.get("session_id")
    ):
        checkpoint_manager = request.app.state.checkpoint_manager
        # Nothing here may escape: a failed or timed-out hook terminates the VM.
        try:
            if not checkpoint_manager.is_dirty():
                logger.info("   ⏭️  No changes since last checkpoint — skipping save on suspend")
                checkpoint_manager.last_suspend_save = _suspend_outcome(
                    "ok", "no changes since the last checkpoint", 0.0, 0, None, committed=False)
            else:
                logger.info("   📦 State changed — checkpointing to S3 before suspend...")
                outcome = await _bounded_suspend_save(checkpoint_manager, session_state["session_id"])
                checkpoint_manager.last_suspend_save = outcome
                if outcome["status"] == "ok":
                    s = checkpoint_manager.last_save_summary
                    # Single greppable line — filter CloudWatch on "CHECKPOINT trigger=suspend".
                    # step_ms/step_kb give the per-step time+size breakdown.
                    logger.info(
                        f"   ✅ CHECKPOINT trigger=suspend session={session_state['session_id']} "
                        f"vars={s.get('vars')} files={s.get('files')} packages={s.get('packages')} "
                        f"size_kb={s.get('size_kb')} pkl_kb={s.get('pkl_kb')} total_ms={s.get('total_ms')} "
                        f"step_ms={s.get('step_ms')} step_kb={s.get('step_kb')} "
                        f"not_saved={[i.get('name') for i in (s.get('not_saved') or [])]} "
                        f"→ s3://{checkpoint_manager.bucket}/sessions/{session_state['session_id']}/"
                    )
        except Exception as e:
            import traceback
            logger.warning(f"   ⚠️ SUSPEND SAVE failed outside the save: {type(e).__name__}: {e}; "
                           f"previous checkpoint kept; suspending with memory intact\n{traceback.format_exc()}")
            try:
                checkpoint_manager.last_suspend_save = _suspend_outcome(
                    "failed", f"{type(e).__name__}: {e}"[:300], None, None, None, committed=False)
            except Exception:
                pass

    return {"status": "suspended"}


# ─── Time-bounded suspend save ───────────────────────────────────────────────
# The image sets suspendTimeoutInSeconds to 60 (scripts/build_all_images.sh,
# the service maximum); the platform terminates the VM when the hook runs past
# it. The save gets SUSPEND_SAVE_BUDGET_SECONDS, then the hook waits at most
# SUSPEND_SAVE_ABORT_GRACE_SECONDS more for the save thread to reach a
# cancellation point. Budget + grace must stay below the hook timeout and the
# budget at least 10 s below it (tests/build/test_hook_timeouts.py checks both).
# The env vars of the same names (read at each suspend, so /proxy/set-env works
# on a running VM) can only LOWER them.
SUSPEND_SAVE_BUDGET_SECONDS = 45.0
SUSPEND_SAVE_ABORT_GRACE_SECONDS = 5.0


def _suspend_budget() -> tuple[float, float]:
    """(budget, grace) in seconds: the env overrides, capped at the defaults."""
    budget = min(_env_float("SUSPEND_SAVE_BUDGET_SECONDS", SUSPEND_SAVE_BUDGET_SECONDS),
                 SUSPEND_SAVE_BUDGET_SECONDS)
    grace = min(_env_float("SUSPEND_SAVE_ABORT_GRACE_SECONDS", SUSPEND_SAVE_ABORT_GRACE_SECONDS),
                SUSPEND_SAVE_ABORT_GRACE_SECONDS)
    return budget, grace


def _suspend_outcome(status: str, reason: str, duration_s, nbytes, budget_s, *, committed: bool) -> dict:
    """The last_suspend_save record (see CheckpointManager.last_suspend_save)."""
    from datetime import datetime, timezone
    return {
        "status": status,
        "reason": reason,
        "duration_s": round(duration_s, 2) if duration_s is not None else None,
        "bytes": nbytes,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "budget_s": budget_s,
        "committed": bool(committed),
    }


async def _bounded_suspend_save(cm, session_id: str) -> dict:
    """Run cm.save(session_id) in a worker thread with a SaveDeadline; return
    the outcome dict. Never raises.

    ok: saved completely. skipped: saved, but variables were left out for
    memory or disk, or a previous save still held the lock. timed_out: the
    budget ran out (the save aborts at its next cancellation point without
    committing). failed: any exception (MemoryError included)."""
    import time as _time
    from app.platform.checkpoint.deadline import SaveCancelled, SaveDeadline

    budget, grace = _suspend_budget()
    deadline = SaveDeadline(budget)
    t0 = _time.monotonic()
    status, reason = "ok", ""
    fut = None
    try:
        fut = asyncio.ensure_future(asyncio.to_thread(cm.save, session_id, deadline=deadline))
        await asyncio.wait_for(asyncio.shield(fut), budget + grace)
        left_out = [i for i in (cm.last_not_saved or []) if i.get("kind") in ("memory", "disk")]
        if left_out:
            status = "skipped"
            reason = "; ".join(f"{i.get('name')}: {i.get('reason')}" for i in left_out)[:300]
    except SaveCancelled as e:
        if e.phase == "lock":
            # last_save_progress belongs to the save still holding the lock.
            status, reason = "skipped", e.reason
            elapsed = _time.monotonic() - t0
            logger.warning(f"   ⚠️ SUSPEND SAVE skipped after {elapsed:.1f}s (budget {budget:g}s): "
                           f"{reason}; nothing saved; previous checkpoint kept; state stays dirty; "
                           f"suspending with memory intact")
            return _suspend_outcome(status, reason, elapsed, 0, budget, committed=False)
        status = "timed_out"
        reason = f"{e.reason} (during {e.phase})"
    except (asyncio.TimeoutError, TimeoutError):
        deadline.cancel("suspend hook stopped waiting for the save")
        status = "timed_out"
        reason = (f"save still running after the {budget:g} s budget; "
                  f"it will abort without committing")
        if fut is not None:
            # The thread ends on its own at its next cancellation point; retrieve
            # its result so asyncio does not log "exception never retrieved".
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())
    except MemoryError:
        status, reason = "failed", "the notebook ran out of memory while saving"
    except Exception as e:
        status, reason = "failed", f"{type(e).__name__}: {e}"[:300]
    elapsed = _time.monotonic() - t0
    progress = getattr(cm, "last_save_progress", None) or {}
    committed = bool(progress.get("committed"))
    if status == "ok":
        s = getattr(cm, "last_save_summary", None) or {}
        try:
            nbytes = int(round(float(s.get("size_kb") or 0) * 1024))
        except (TypeError, ValueError):
            nbytes = None
    else:
        nbytes = progress.get("uploaded_bytes")
    outcome = _suspend_outcome(status, reason, elapsed, nbytes, budget, committed=committed)
    if status == "skipped" and progress.get("phase") == "done":
        logger.warning(
            f"   ⚠️ SUSPEND SAVE skipped after {elapsed:.1f}s (budget {budget:g}s): {reason}; "
            f"not saved: those variables (a previously saved copy is kept); the rest was saved; "
            f"state stays dirty; suspending with memory intact")
    elif status != "ok":
        what = ("variables committed; files, packages, DAG, imports and metadata were not"
                if committed else "nothing from this save was committed")
        logger.warning(
            f"   ⚠️ SUSPEND SAVE {status} after {elapsed:.1f}s (budget {budget:g}s): {reason}; "
            f"not saved: {what}; previous checkpoint kept; state stays dirty; "
            f"suspending with memory intact")
    return outcome


@router.post("/resume")
async def hook_resume(request: Request):
    """
    Called AFTER resume (traffic arrived or explicit resume).
    Memory + disk are restored from the suspend snapshot.
    """
    executor = request.app.state.executor
    session_state = request.app.state.session_state
    session_state["resume_count"] += 1

    # A resumed VM restores the same snapshot memory, so re-drop any cached boto3
    # credentials here too — the resumed VM may run under a fresh role/token and
    # must not reuse stale (build-role) creds baked into the snapshot.
    _reset_boto3_credentials()

    stats = executor.get_stats()
    logger.info(f"⏰ HOOK /resume — Waking up (warm resume — in-memory state, no S3 restore)")
    logger.info(f"   State intact: {stats['variables_count']} variables, {stats['execution_count']} prior executions")

    return {"status": "running"}


@router.post("/terminate")
async def hook_terminate(request: Request):
    """
    Called BEFORE termination, ONLY when the VM is RUNNING at kill time
    (explicit terminate-microvm, or auto-terminate of a RUNNING VM at max
    lifetime). Timeout: 60 seconds to complete.

    IMPORTANT — this hook does NOT fire if the VM is SUSPENDED when it is
    terminated (verified: see the STATE PERSISTENCE MODEL note at the top of
    this module). That suspended-kill case is covered by the checkpoint we take
    in /suspend. So this hook now handles only the RUNNING → terminate path:
      • user explicitly closes / terminates the notebook, or
      • the rotator terminates a still-RUNNING VM, or
      • AWS auto-terminates a VM that is RUNNING at max lifetime.

    If checkpoint is enabled and state is dirty, serializes the executor
    namespace + local files to S3 so the session can be restored on a new
    MicroVM. Uses the same dirty-flag as /suspend, so a terminate immediately
    after a clean suspend (nothing changed since) does not re-serialize.
    """
    executor = request.app.state.executor
    session_state = request.app.state.session_state

    stats = executor.get_stats()
    logger.info(f"🔴 HOOK /terminate — Shutting down")
    logger.info(f"   Total executions: {stats['execution_count']}")
    logger.info(f"   Total suspends: {session_state['suspend_count']}")
    logger.info(f"   Total resumes: {session_state['resume_count']}")

    # Save on terminate in BOTH modes (the RUNNING → terminate path). Eternal
    # mode no longer relies on a pre-emptive rotator to transfer state; it
    # recovers reactively from the last S3 checkpoint on the next request. So if
    # a RUNNING eternal VM is terminated (max lifetime hit while running, or an
    # explicit terminate), we must persist here or lose changes made since the
    # last /suspend. The dirty-flag makes this a no-op when nothing changed
    # (e.g. a terminate right after a clean suspend).
    if session_state.get("checkpoint_enabled") and session_state.get("session_id"):
        checkpoint_manager = request.app.state.checkpoint_manager
        if not checkpoint_manager.is_dirty():
            logger.info(f"   ⏭️  No changes since last checkpoint — skipping save on terminate")
        else:
            logger.info(f"   📦 Checkpointing session to S3...")
            try:
                # Worker thread (like /suspend): the save blocks on S3 and takes the
                # executor's _exec_lock (bounded 2 s) for the runtime snapshot —
                # never on the event loop.
                await asyncio.to_thread(checkpoint_manager.save, session_state["session_id"])
                s = checkpoint_manager.last_save_summary
                # Single greppable line — filter CloudWatch on "CHECKPOINT trigger=terminate".
                # step_ms/step_kb give the per-step time+size breakdown.
                logger.info(
                    f"   ✅ CHECKPOINT trigger=terminate session={session_state['session_id']} "
                    f"vars={s.get('vars')} files={s.get('files')} packages={s.get('packages')} "
                    f"size_kb={s.get('size_kb')} pkl_kb={s.get('pkl_kb')} total_ms={s.get('total_ms')} "
                    f"step_ms={s.get('step_ms')} step_kb={s.get('step_kb')} "
                    f"not_saved={[i.get('name') for i in (s.get('not_saved') or [])]} "
                    f"→ s3://{checkpoint_manager.bucket}/sessions/{session_state['session_id']}/"
                )
            except Exception as e:
                import traceback
                logger.error(f"   ❌ Checkpoint failed: {e}\n{traceback.format_exc()}")

    return {"status": "terminated"}


def _env_float(name: str, default: float) -> float:
    """Parse a positive float env var; fall back to `default` on missing/invalid."""
    try:
        v = float(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# Bounded wait for the execute lock before /checkpoint-save and /restore-state.
# 25s is deliberately BELOW the UI's 30s checkpointSession timeout
# (web/src/services/notebooks.js) and the proxy's 60s _checkpoint_session_vm
# timeout, so a long Run All yields a clean "busy" failure instead of a client
# timeout. Both /restore-state callers (proxy reactive recovery, pool grant)
# fail closed on a failure (retry / terminate the new VM), and restore runs on
# a freshly-launched VM before any traffic is routed, so the same cap is safe.
CHECKPOINT_LOCK_WAIT_SECONDS = _env_float("CHECKPOINT_LOCK_WAIT_SECONDS", 25.0)

_BUSY = object()  # sentinel: execute lock not acquired within the cap


async def _run_under_execute_lock(request: Request, fn, *args):
    """Run blocking `fn(*args)` in a worker thread while holding the SAME
    asyncio execute lock every execution route uses (/execute, /execute-batch,
    /execute-sql, /upload, /set-variable), so a save/restore never overlaps a
    running cell (save iterates and pickles the live namespace).

    Returns _BUSY (without calling fn) if the lock isn't free within
    CHECKPOINT_LOCK_WAIT_SECONDS. Otherwise returns fn's result or raises its
    exception; the lock is ALWAYS released. If the request coroutine is
    cancelled mid-run the worker thread can't be interrupted, so the lock is
    released only when that thread finishes — never while it is still touching
    the namespace.
    """
    from app.notebook.code_engine import _get_execute_lock

    lock = _get_execute_lock(request)
    try:
        # asyncio.Lock.acquire only claims the lock if its wait completes without
        # cancellation, so a wait_for timeout can't leak it.
        await asyncio.wait_for(lock.acquire(), timeout=CHECKPOINT_LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return _BUSY

    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    release_deferred = False
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        if not fut.done():
            release_deferred = True

            def _release_when_done(f):
                lock.release()
                if not f.cancelled():
                    f.exception()  # mark retrieved; avoid "never retrieved" noise

            fut.add_done_callback(_release_when_done)
        raise
    finally:
        if not release_deferred:
            lock.release()


@proxy_router.post("/checkpoint-save")
async def checkpoint_save(request: Request):
    """
    On-demand checkpoint save — saves state to S3 WITHOUT terminating.
    Called by the proxy's rotation logic before swapping to a new VM.

    Request body (optional):
        {"session_id": "override-session-id"}
    """
    session_state = request.app.state.session_state
    checkpoint_manager = request.app.state.checkpoint_manager

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    session_id = body.get("session_id") or session_state.get("session_id")

    if not session_id:
        return {"success": False, "error": "No session_id available"}

    logger.info(f"📦 /checkpoint-save — Saving state on demand (session={session_id})")
    try:
        # Blocking dill/boto3 work in a worker thread, but AWAITED to completion —
        # the caller (proxy rotation) relies on the save being durable when we reply.
        # Held under the execute lock so no cell runs during the save.
        if await _run_under_execute_lock(request, checkpoint_manager.save, session_id) is _BUSY:
            msg = (f"VM busy — execution in progress; checkpoint not taken "
                   f"(execute lock not free within {CHECKPOINT_LOCK_WAIT_SECONDS:g}s)")
            logger.warning(f"   ⏳ /checkpoint-save busy — execution in progress, skipping save (session={session_id})")
            return {"success": False, "error": msg, "busy": True}
        not_saved = list(getattr(checkpoint_manager, "last_not_saved", None) or [])
        logger.info(f"   ✅ Checkpoint saved: sessions/{session_id}/"
                    + (f" ({len(not_saved)} variable(s) not saved: "
                       f"{[i.get('name') for i in not_saved][:10]})" if not_saved else ""))
        return {
            "success": True,
            "session_id": session_id,
            "save_timings_ms": checkpoint_manager.last_save_timings,
            # Variables this save left out: [{name, reason, kind}]; kind "memory"
            # or "disk" means it would not fit (the proxy keeps the VM running on
            # a pre-terminate save), "unpicklable" means it cannot be pickled.
            "not_saved": not_saved,
        }
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"   ❌ Checkpoint save failed: {e}\n{tb}")
        err = str(e) or type(e).__name__
        if isinstance(e, MemoryError):
            err = "the notebook ran out of memory while saving"
        return {"success": False, "error": err, "traceback": tb}


@proxy_router.post("/restore-state")
async def restore_state(request: Request):
    """
    On-demand state restore — loads state from S3 onto this running VM.
    Called by the proxy's rotation logic after launching a new VM.

    Request body:
        {"session_id": "session-to-restore-from"}
    """
    checkpoint_manager = request.app.state.checkpoint_manager
    session_state = request.app.state.session_state

    body = await request.json()
    session_id = body.get("session_id")

    if not session_id:
        return {"success": False, "error": "session_id required"}

    logger.info(f"♻️ /restore-state — Restoring from S3 (session={session_id})")
    try:
        # Blocking S3 GETs + unpickling in a worker thread, AWAITED to completion
        # (the response must only report success once the namespace is restored).
        # Held under the execute lock so no cell runs against a half-restored namespace.
        if await _run_under_execute_lock(request, checkpoint_manager.restore, session_id) is _BUSY:
            msg = (f"VM busy — execution in progress; restore not performed "
                   f"(execute lock not free within {CHECKPOINT_LOCK_WAIT_SECONDS:g}s)")
            logger.warning(f"   ⏳ /restore-state busy — execution in progress, skipping restore (session={session_id})")
            return {"success": False, "error": msg, "busy": True}
        # Update session state to reflect the restored session (back on the loop)
        session_state["session_id"] = session_id
        session_state["checkpoint_enabled"] = True
        logger.info(f"   ✅ State restored from: {session_id}")
        return {
            "success": True,
            "session_id": session_id,
            "restore_timings": checkpoint_manager.last_restore_timings,
        }
    except Exception as e:
        import traceback
        logger.error(f"   ❌ Restore failed: {e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@proxy_router.post("/rebind")
async def rebind(request: Request):
    """
    Overwrite THIS VM's session identity — fake pool-spare id -> real session —
    after a pooled spare has been resumed and handed to a real session. Reuses
    the SAME identity-setting logic hook_run (/run) uses for a fresh launch
    (_apply_session_identity), so /run and /rebind can never drift.

    Request body: a runHookPayload-shaped JSON object — session_id,
    notebook_id, notebook_name, persistence_mode, checkpoint_enabled,
    artifacts_bucket, env_vars?, secrets?, user_env_keys?, and either
    catalog_entries? or catalog_s3_key? (plus memory_mib/architecture for the
    MICROVM_MEMORY_MIB/MICROVM_ARCH env mirror). microvm_id is intentionally
    NOT read from the body — this VM's own control-plane id is immutable
    post-boot, so the existing session_state["microvm_id"] is preserved.

    Does NOT handle restore_from — the caller (the VM Pool Manager) follows up
    with the EXISTING POST /restore-state for the restore case, unchanged.

    IMPORTANT ORDERING REQUIREMENT (caller's responsibility, not enforced
    here): artifacts_bucket is set BY THIS CALL. CheckpointManager.bucket
    reads session_state["artifacts_bucket"] (app/platform/checkpoint/manager.py).
    So the caller MUST complete this /rebind call BEFORE issuing any
    /restore-state call, or restore would read a stale/empty bucket.
    """
    session_state = request.app.state.session_state

    payload = await request.json()
    if not isinstance(payload, dict):
        return {"status": "error", "error": "body must be a JSON object"}

    _apply_session_identity(request.app, payload, microvm_id=None)

    # Defensive, not strictly required: this VM's execution role is fixed at
    # run-microvm (launch) time and does not change on rebind, so there is no
    # stale-role credential to drop here the way there is on /run and
    # /resume. Called anyway because it is harmless (it only clears a
    # process-global boto3 session cache) and guards against any future case
    # where a pool spare's cached creds could otherwise outlive a grant.
    _reset_boto3_credentials()

    # Pre-warm S3 (DNS+TLS+creds) in the background NOW, so the /restore-state
    # that the pool issues immediately after this rebind doesn't pay the cold
    # handshake on its first GET (the dominant term in a slow restore). Harmless
    # if it fails — restore would just warm the connection itself. See helper.
    _prewarm_s3_connection()

    logger.info(f"🔁 HOOK /rebind — Session rebound to {session_state['session_id']}")
    return {"status": "rebound", "session_id": session_state["session_id"]}
