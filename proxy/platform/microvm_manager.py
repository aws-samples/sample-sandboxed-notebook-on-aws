"""
MicroVM Manager — encapsulates all MicroVM lifecycle state.

Part of: proxy.platform (Smart MicroVM Service layer)

This class manages:
- AWS Lambda MicroVMs client
- Auth token cache (bounded LRU) — both HTTP and shell tokens
- Shell auth tokens for WebSocket terminal access (SHELL_INGRESS connector)
- Active MicroVM tracking (endpoint, session, memory tier)
- Reactive recovery of a dead session VM (pool grant or relaunch + restore)
- Cost tracking (baseline + burst metering)
- Artifacts bucket discovery
- Session-to-VM mapping for request routing

Network connectors:
- HTTP_INGRESS: Standard HTTPS endpoint for API requests
- SHELL_INGRESS: WebSocket-based interactive shell (platform-managed PTY)
- INTERNET_EGRESS: Outbound internet access for pip install, git clone, etc.

All route modules access this via app.state.vm_manager.
"""

import os
import time
import asyncio
import logging
import httpx
from datetime import datetime

import boto3
import botocore.config

from proxy.storage import storage
from proxy.platform.cost_tracker import CostTracker
from proxy.platform import http_clients
from proxy.platform.token_cache import make_token_cache
from proxy.platform.errors import emit_error_metric
from proxy.platform.defaults import DEFAULT_MEMORY_MIB, arch_size_from_image
from pool_manager.config import PROXY_GRANT_RESTORE_BOUND_SECONDS

logger = logging.getLogger(__name__)

# --- Launch-path timing instrumentation (OFF by default) -------------------
# perf_counter timing lines around the launch path's control-plane / I/O calls
# (auth-token mint, wait-for-RUNNING poll, STS/S3 staging, session-registry
# read/write). Logged at DEBUG (suppressed under the proxy's WARNING level);
# PROXY_LAUNCH_TIMING=1 raises them to INFO.
def _launch_timing_enabled() -> bool:
    return os.environ.get("PROXY_LAUNCH_TIMING", "").strip().lower() in ("1", "true", "yes", "on")


def _timed_log(label: str, ms: float) -> None:
    """Emit a single launch-path timing line. INFO when PROXY_LAUNCH_TIMING is
    truthy, otherwise DEBUG (suppressed under the proxy's WARNING default)."""
    msg = f"[launch-timing] {label}: {ms:.1f}ms"
    if _launch_timing_enabled():
        logger.info(msg)
    else:
        logger.debug(msg)


# --- Configuration (from environment) ---
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
IMAGE_ARN = os.environ.get("MICROVM_IMAGE_ARN", "")
# Arch-agnostic image base name (e.g. "notebook-microvm"). Images are named
# {IMAGE_BASE_NAME}-{arch}-{mem}. Sourced from config.sh via env; no hardcoding.
IMAGE_BASE_NAME = os.environ.get("IMAGE_BASE_NAME", "notebook-microvm")
# Arches this proxy supports (space-separated, e.g. "arm64 x86_64"), and the
# default the UI pre-selects. A single proxy serves every listed arch at once.
IMAGE_ARCHES = os.environ.get("IMAGE_ARCHES", "arm64").split()
DEFAULT_IMAGE_ARCH = os.environ.get("DEFAULT_IMAGE_ARCH", "arm64")
EXEC_ROLE_ARN = os.environ.get("MICROVM_EXEC_ROLE_ARN", "")
# Frontend VM-state poll cadence, served to the UI via GET /health's
# poll_interval_ms (App.jsx applies it, overriding the JS default). 15s (not 10s)
# eases ListMicrovms pressure — each /instances poll can fan out to a paginated
# list_microvms — at a negligible UX cost on state dots / auto-connect. Operator-
# tunable via POLL_INTERVAL_MS in scripts/config.sh.
POLL_INTERVAL_MS = int(os.environ.get("POLL_INTERVAL_MS", "15000"))
INGRESS_CONNECTOR = os.environ.get("MICROVM_INGRESS_CONNECTOR",
    f"arn:aws:lambda:{AWS_REGION}:aws:network-connector:aws-network-connector:HTTP_INGRESS")
SHELL_INGRESS_CONNECTOR = os.environ.get("MICROVM_SHELL_INGRESS_CONNECTOR",
    f"arn:aws:lambda:{AWS_REGION}:aws:network-connector:aws-network-connector:SHELL_INGRESS")
EGRESS_CONNECTOR = os.environ.get("MICROVM_EGRESS_CONNECTOR",
    f"arn:aws:lambda:{AWS_REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS")

# VM lifecycle states (from get_microvm). "Alive" states mean the VM is fine or
# will be (suspended VMs auto-resume on traffic), so we retry rather than recover.
VM_ALIVE_STATES = frozenset({"RUNNING", "SUSPENDED", "SUSPENDING", "RESUMING", "PENDING"})

# Reactive-recovery restore retry policy (exponential backoff).
RECOVER_RESTORE_MAX_ATTEMPTS = 3
RECOVER_RESTORE_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8 ...

# B2 (multi-instance): when THIS instance loses the distributed recovery lock,
# another instance is recovering the session. We poll the DB registry for the
# new VM it publishes, then forward to it instead of launching a duplicate.
# Bounded so a stuck/crashed peer can't hang the loser forever (it then fails
# loudly and the next request re-attempts recovery — the lock will be free).
RECOVER_LOCK_WAIT_TIMEOUT = 120.0   # seconds to wait for the winner to publish
RECOVER_LOCK_WAIT_POLL = 0.5        # seconds between DB registry polls


def _get_persistence_mode() -> str:
    """Local copy of routes.microvm.get_persistence_mode's exact logic (reading
    the same SESSION_PERSISTENCE_MODE env var). Duplicated here rather than
    imported to avoid a circular import (routes/microvm.py imports FROM this
    module). Keep in sync with routes/microvm.py's PERSISTING_MODES/get_persistence_mode."""
    mode = os.environ.get("SESSION_PERSISTENCE_MODE", "checkpoint")
    return mode if mode in ("eternal", "checkpoint") else "none"


async def build_user_env(notebook_id, request_env=None, request_secrets=None, *,
                         platform_env: dict, load_env=None, context: str = "Launch",
                         warn_if_unlinked: bool = False) -> tuple[dict, list, dict]:
    """The env/secrets a VM launch injects: (env_vars, secrets, user_env_keys).

    Shared by /launch, eternal-mode reactive recovery (_do_recover, pool-grant and
    cold) and rotation (launch_for_rotation), so a relaunched VM gets the same
    user env as a fresh launch.
      - The notebook's PERSISTED env/secrets (storage notebook_env_get) are merged
        UNDER the request's: request env wins by key; secrets are unioned by their
        target envVar, request entries winning over stored ones.
      - user_env_keys = {name: "env"|"secret"} for the USER keys only (request +
        persisted, taken before the platform merge); a secret wins over a
        same-named env. The /run hook publishes it as MICROVM_USER_ENV_KEYS.
      - platform_env (vm_manager.launch_env_vars()) is merged UNDER the user env.
    load_env: async callable(notebook_id) -> {"env_vars", "secrets"}; defaults to
    the module storage's notebook_env_get off the event loop. A load failure falls
    back to the request values with a WARNING. warn_if_unlinked: also WARN when
    notebook_id is None (recovery/rotation, where the user env is then missing).
    Logs never carry env or secret VALUES (at most the notebook id and error type)."""
    env_vars = dict(request_env or {})
    secrets = list(request_secrets or [])
    if notebook_id is not None:
        if load_env is None:
            async def load_env(nid):
                return await asyncio.to_thread(storage.notebook_env_get, nid)
        try:
            persisted = await load_env(notebook_id) or {}
            stored_env = persisted.get("env_vars", {}) or {}
            stored_secrets = persisted.get("secrets", []) or []
            env_vars = {**stored_env, **env_vars}
            req_env_targets = {s.get("envVar") for s in secrets
                               if isinstance(s, dict) and s.get("envVar")}
            merged_secrets = list(secrets)
            for s in stored_secrets:
                if isinstance(s, dict) and s.get("envVar") and s.get("envVar") not in req_env_targets:
                    merged_secrets.append(s)
            secrets = merged_secrets
        except Exception as e:
            logger.warning(f"{context}: could not load persisted env for notebook {notebook_id} "
                           f"({type(e).__name__}); continuing without it")
    elif warn_if_unlinked:
        logger.warning(f"{context}: no notebook linked to this session; relaunching without "
                       f"the notebook's env vars and secrets")
    user_env_keys = {}
    for _k in env_vars.keys():
        if _k:
            user_env_keys[_k] = "env"
    for _s in secrets:
        if isinstance(_s, dict) and _s.get("envVar"):
            user_env_keys[_s["envVar"]] = "secret"
    env_vars = {**(platform_env or {}), **env_vars}
    return env_vars, secrets, user_env_keys


# --- VM Pool Manager integration (design §10, §13.4 Option A) --------------
# Gated by POOL_ENABLED (default false — zero change to today's recovery path
# when off). A sibling of routes/microvm.py's _try_pool_grant: duplicated
# rather than imported to avoid a circular import (routes/microvm.py already
# imports FROM this module). Read fresh on every call (not cached at import)
# so tests can monkeypatch os.environ per-test without reimporting the module.
_POOL_GRANT_TIMEOUT_SECONDS = 10.0  # bounded — a slow/dead pool manager must never hang a recovery. 10s (not 5s) so a slow-but-successful grant (e.g. a ~6s rebind on a freshly-resumed spare) isn't abandoned mid-flight.
# A restore grant (payload carries restore_from, as every recovery grant does)
# waits for the pool's whole grant budget plus a margin, same bound as the
# /launch restore grant in routes/microvm.py (pool_manager/config.py).
_POOL_GRANT_RESTORE_TIMEOUT_SECONDS = PROXY_GRANT_RESTORE_BOUND_SECONDS


def _pool_enabled() -> bool:
    return os.environ.get("POOL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _pool_manager_url() -> str:
    return os.environ.get("POOL_MANAGER_URL", "http://localhost:8090")


def adopt_pool_grant_times(granted: dict) -> tuple[str | None, float]:
    """Mint time of a pool-granted VM as (ISO string, epoch float).
    A pooled spare's AWS 8h max-lifetime clock started at MINT (when the pool ran
    run_microvm), NOT at adoption: the spare may have sat warm in the pool for up
    to the recycle ceiling (~2h). The ISO value goes to vm_session_create
    (launched_at + prior_suspended_since; the DB column is ISO, and None means
    "now"); the epoch goes to active_microvms["launched_at"] and
    cost_tracker.record_prior_suspended. An older pool may not return launched_at,
    and a malformed value degrades the same way: (None, now)."""
    iso = granted.get("launched_at")
    try:
        epoch = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else time.time()
    except (ValueError, TypeError, AttributeError):
        return None, time.time()
    return iso, epoch


async def _try_pool_grant(payload: dict) -> dict | None:
    """POST {POOL_MANAGER_URL}/grant. Returns {"vm_id":..., "endpoint":...} on
    200, else None (409 POOL_EMPTY, any non-200, timeout, connection error, or
    any other exception) so the caller ALWAYS falls through to the existing
    launch_for_rotation path. Never raises."""
    if not _pool_enabled():
        return None
    # Server-to-server auth: /grant is gated by POOL_AUTH_TOKEN (X-Proxy-Auth).
    headers = {}
    _pool_token = os.environ.get("POOL_AUTH_TOKEN")
    if _pool_token:
        headers["X-Proxy-Auth"] = _pool_token
    try:
        grant_timeout = (_POOL_GRANT_RESTORE_TIMEOUT_SECONDS
                         if payload.get("restore_from")
                         else _POOL_GRANT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=grant_timeout) as client:
            resp = await client.post(f"{_pool_manager_url()}/grant", json=payload, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if "vm_id" in data and "endpoint" in data:
                return data
            logger.warning(f"🔁 Pool grant 200 but malformed body: {data!r} — falling back to launch_for_rotation")
            return None
        logger.info(f"🔁 Pool grant miss/error (status={resp.status_code}) — falling back to launch_for_rotation")
        return None
    except Exception as e:
        logger.info(f"🔁 Pool grant unreachable/failed ({e}) — falling back to launch_for_rotation")
        return None


# Eternal-mode death tombstone TTL (Fix 2 / defense-in-depth backstop — see
# _eternal_death_tombstones). Comparable to the launch/recovery window: long
# enough that a request landing just after the death still recovers, short enough
# that a tombstone can't linger and cause recovery for a session nobody is using
# any more.
ETERNAL_DEATH_TOMBSTONE_TTL_SEC = 90.0


class _RecoveryState:
    """Per-session reactive-recovery coordination.

    - lock:      ensures only ONE coroutine performs the launch+restore.
    - queue:     FIFO list of (request_data, asyncio.Future) for requests that
                 arrived DURING recovery — replayed in arrival order on the new
                 VM so notebook cell execution stays ordered.
    - in_progress: True while a recovery is running (so late arrivals enqueue
                 instead of starting a second recovery).
    """
    __slots__ = ("lock", "queue", "in_progress", "seq")

    def __init__(self):
        self.lock = asyncio.Lock()
        # Each item: (seq: int, request_data: dict, future: asyncio.Future).
        # seq is a monotonic ARRIVAL sequence assigned at proxy_request entry, so
        # replay order == true arrival order regardless of how long each request's
        # failed-forward + state-check took before it reached the queue (that
        # latency is what broke FIFO when ordering by append order).
        self.queue: list = []
        self.in_progress = False
        self.seq = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class MicrovmManager:
    """
    Central manager for MicroVM lifecycle operations.

    Holds all shared state that route handlers need:
    - Lambda MicroVMs client
    - Token cache
    - Active VM tracking
    - Cost tracker
    """

    def __init__(self):
        self.token_cache = make_token_cache()
        # Single-flight auth-token mints: microvm_id -> in-flight mint task, so
        # concurrent cache misses for one VM share ONE CreateMicrovmAuthToken.
        self._token_inflight: dict[str, asyncio.Task] = {}
        self.active_microvms: dict[str, dict] = {}
        self.cost_tracker = CostTracker()
        # Two SEPARATE control-plane clients (see get_lambda_poll_client /
        # get_lambda_launch_client). The POLL client (adaptive) serves the
        # high-frequency /instances dashboard poll + pool/recovery reconcile; the
        # LAUNCH client (standard) serves the latency-sensitive interactive path.
        self._lambda_poll_client = None
        self._lambda_launch_client = None
        # Cached STS/S3 clients so the launch path doesn't construct a new
        # boto3 client (and, for STS, re-resolve the account) on every call.
        self._sts_client = None
        self._s3_client = None
        self._artifacts_bucket: str | None = os.environ.get("ARTIFACT_BUCKET")

        # Session registry — maps session_id → current VM info
        # This is the single source of truth for "which VM serves this session?"
        # Updated on launch, reactive recovery (VM swap), and terminate.
        self._session_registry: dict[str, dict] = {}  # session_id → {vm_id, endpoint}
        # Per-session reactive-recovery state. Concurrent requests that discover a
        # dead VM must (a) trigger only ONE recovery and (b) be replayed in FIFO
        # arrival order on the recovered VM. Each entry is a _RecoveryState (see
        # below) holding a lock + an ordered pending-request queue.
        # Cleaned up in unregister_session() to avoid unbounded growth.
        self._recovery: dict[str, "_RecoveryState"] = {}
        # Per-phase timing of the last reactive recovery (exposed via
        # GET /recovery-timings for tests/dashboards).
        self.last_recovery_timings: dict = {}
        self._last_restore_pkl_ms: float = 0.0

        # Sessions with a launch in flight → monotonic start time. Set at the very
        # start of /launch and cleared once register_session lands (or launch fails).
        # The /proxy/{path} forwarder uses this to tell the brief "VM not registered
        # YET" window (wait for the mapping) apart from a genuinely terminated /
        # never-existed session (404 immediately). Without it a background panel/
        # editor fetch (/variables, /helpers, /completions-catalog, /files, ...) that
        # races registration gets a hard 404 — functionally recovered by client
        # retries, but it litters the browser console with 404s. Entries self-expire
        # via _LAUNCHING_TTL_SEC so a crashed launch can't pin a session forever.
        self._launching_sessions: dict[str, float] = {}

        # User-installed packages per session (for categorization display)
        self._user_installed: dict[str, list[dict]] = {}  # session_id → [{package, category}]

        # Eternal-mode death tombstones (Fix 2 / defense-in-depth): session_id →
        # (last_vm_id, monotonic_death_time). Written by
        # record_eternal_death_tombstone() ONLY on a confirmed VM-death event
        # observed while in eternal mode (never for an explicit user /terminate).
        # Consulted by proxy_request's top-of-function 404 guard so recovery
        # survives even if the session→VM mapping is gone by the time the next
        # request arrives (the common case is already handled by _mark_vm_dead
        # leaving the mapping in place — this is the backstop for residual paths: a
        # future unregister call, DB/registry eviction, a proxy restart that drops
        # the L1 cache, etc.). Short TTL so a stale tombstone can't resurrect a
        # session indefinitely.
        self._eternal_death_tombstones: dict[str, tuple[str, float]] = {}

    # ============================================================
    # SESSION REGISTRY
    # ============================================================

    def _register_session_local(self, session_id: str, vm_id: str, endpoint: str):
        """In-memory half of registering a session → VM mapping: write the L1 cache
        and clear the launching marker. Shared by the sync register_session and the
        async register_session_async so both do the exact same in-process work; only
        the DB write differs (inline-sync vs threaded)."""
        self._session_registry[session_id] = {"vm_id": vm_id, "endpoint": endpoint}
        # Mapping is live now — the launch window is over, so a waiting forwarder
        # stops waiting and routes. (No-op if the session was never marked.)
        self._launching_sessions.pop(session_id, None)

    def register_session(self, session_id: str, vm_id: str, endpoint: str):
        """Register or update a session → VM mapping (SYNC — for startup / sync
        contexts; async handlers use register_session_async so the DB write runs
        off the event loop).

        Write-through: the in-process dict is an L1 cache; the DB (via storage)
        is the source of truth so a horizontally-scaled proxy fleet stays
        consistent (B1). On SQLite this is a same-process local write, so
        single-instance behavior is unchanged. DB errors are non-fatal — the L1
        cache still lets THIS instance route — but are logged loudly.
        """
        self._register_session_local(session_id, vm_id, endpoint)
        try:
            storage.session_registry_upsert(session_id, vm_id, endpoint)
        except Exception as e:
            logger.warning(f"session_registry_upsert failed for {session_id} (using L1 cache): {e}")
        logger.info(f"Session registered: {session_id} → {vm_id}")

    async def register_session_async(self, session_id: str, vm_id: str, endpoint: str):
        """Async register_session: identical semantics, but the SQLite
        write-through runs in a worker thread (asyncio.to_thread) so it never blocks the
        single event loop. Used by the launch/recover hot paths. The L1 write +
        launching-marker clear happen in-process (synchronous, must stay) BEFORE the
        threaded DB write, so a concurrent forwarder sees the live mapping with no
        thread-hop delay — same ordering as the sync method."""
        self._register_session_local(session_id, vm_id, endpoint)
        _t0 = time.perf_counter()
        try:
            # Offload via to_thread against the module-level `storage` (resolved at
            # call time) so the write runs off the event loop AND the sync and async
            # paths share the exact same backend/test-patch seam (_do_recover's
            # tests monkeypatch module `storage`).
            await asyncio.to_thread(storage.session_registry_upsert, session_id, vm_id, endpoint)
        except Exception as e:
            logger.warning(f"session_registry_upsert failed for {session_id} (using L1 cache): {e}")
        _timed_log("session_register", (time.perf_counter() - _t0) * 1000.0)
        logger.info(f"Session registered: {session_id} → {vm_id}")

    def unregister_session(self, session_id: str):
        """Remove a session from the registry (VM terminated, no rotation)."""
        self._session_registry.pop(session_id, None)
        try:
            storage.session_registry_delete(session_id)
        except Exception as e:
            logger.warning(f"session_registry_delete failed for {session_id}: {e}")
        # Drop any reactive-recovery state so _recovery doesn't grow unbounded.
        # Fail any stragglers still queued (session is being torn down).
        rs = self._recovery.pop(session_id, None)
        if rs is not None and rs.queue:
            err = RuntimeError(f"Session {session_id} terminated during recovery")
            for _seq, _req, fut in rs.queue:
                if not fut.done():
                    fut.set_exception(err)
        logger.info(f"Session unregistered: {session_id}")

    def record_eternal_death_tombstone(self, session_id: str, vm_id: str):
        """Record a short-lived tombstone {session_id: (vm_id, now)} for a session
        whose VM was just confirmed dead WHILE IN ETERNAL MODE. Written by
        _mark_vm_dead's eternal branch alongside leaving the session→VM mapping in
        place (Fix 1): the mapping handles the common case, this tombstone is the
        backstop (Fix 2) for residual paths that nonetheless drop the mapping
        before the next request arrives (a future unregister call, DB/registry
        eviction, a proxy restart that lost the L1 cache). No-op outside eternal
        mode and when vm_id is falsy, so non-eternal behavior is unchanged and an
        explicit user /terminate (which never calls this) is never resurrected."""
        if vm_id and _get_persistence_mode() == "eternal":
            self._eternal_death_tombstones[session_id] = (vm_id, time.monotonic())

    def pop_eternal_death_tombstone(self, session_id: str) -> str | None:
        """Return (and clear) the last known vm_id for a session whose VM was
        confirmed dead in eternal mode (see record_eternal_death_tombstone), if the
        tombstone is still within ETERNAL_DEATH_TOMBSTONE_TTL_SEC. Returns None if
        there is no tombstone or it has expired (expired entries are pruned on
        read). Used by proxy_request's 404 guard (Fix 2 backstop) to recover a
        session whose mapping is gone."""
        entry = self._eternal_death_tombstones.pop(session_id, None)
        if entry is None:
            return None
        vm_id, died_at = entry
        if time.monotonic() - died_at > ETERNAL_DEATH_TOMBSTONE_TTL_SEC:
            return None
        return vm_id

    def _apply_session_vm_read(self, session_id: str, db: dict | None) -> dict | None:
        """Common post-processing for a session_registry read: refresh L1 from the
        authoritative DB row, or fall back to L1 when the DB says unknown. Shared by
        the sync get_session_vm and the async get_session_vm_async so they resolve
        identically."""
        if db is not None:
            # Refresh L1 so downstream in-process reads (recovery double-check)
            # see the authoritative mapping.
            self._session_registry[session_id] = db
            return db
        # DB says unknown. Fall back to L1 only if present (covers the tiny window
        # between an in-process register and its write-through, and keeps SQLite
        # identical). If neither knows it, it's genuinely unregistered.
        return self._session_registry.get(session_id)

    def invalidate_session_vm(self, session_id: str) -> None:
        """Drop the in-process L1 session->VM entry so the NEXT get_session_vm_async
        re-reads the authoritative DB (picks up a rotation/rebind that just landed).
        Called from the forward-failure path before a single re-resolve+retry.

        Caveat (single retry only): get_session_vm_async falls back to L1 on a DB
        read ERROR. After invalidation that fallback is empty, so a concurrent DB
        outage makes the re-resolve return no mapping — which simply surfaces the
        502, acceptable for a one-shot retry and strictly better than retrying a
        known-stale row."""
        self._session_registry.pop(session_id, None)

    def get_session_vm(self, session_id: str) -> dict | None:
        """Look up the current VM for a session. Returns {vm_id, endpoint} or None.
        (SYNC — for thread/sync callers such as the intel generation workers. Async
        handlers on the event loop — including _build_chat_context and
        _resolve_session_vm_targets — use get_session_vm_async so the SQLite read
        runs off-loop.)

        Read-through-DB per request (B1): the DB registry is authoritative, so if
        another proxy instance relinked this session to a new VM (e.g. after a
        recovery it performed), THIS instance sees the new VM immediately. The
        in-process dict is refreshed from what the DB returns and used as an L1
        fallback when the DB is briefly unavailable.

        On SQLite (single instance) the DB always matches the L1 cache, so
        behavior is identical to a pure in-memory lookup.
        """
        try:
            db = storage.session_registry_get(session_id)
        except Exception as e:
            logger.warning(f"session_registry_get failed for {session_id} (using L1 cache): {e}")
            return self._session_registry.get(session_id)
        return self._apply_session_vm_read(session_id, db)

    async def get_session_vm_async(self, session_id: str) -> dict | None:
        """Async get_session_vm: identical DB-authoritative semantics, but
        the SQLite SELECT runs in a worker thread (asyncio.to_thread) so it never blocks
        the single event loop. Used by the /proxy, /launch, /intel and session
        routes. On a DB error it falls back to the in-memory L1 cache, exactly like
        the sync method."""
        _t0 = time.perf_counter()
        try:
            # Offload via to_thread against the module-level `storage` (resolved at
            # call time) so the read runs off the event loop AND shares the exact
            # same backend/test-patch seam as the sync get_session_vm.
            db = await asyncio.to_thread(storage.session_registry_get, session_id)
        except Exception as e:
            logger.warning(f"session_registry_get failed for {session_id} (using L1 cache): {e}")
            _timed_log("session_vm_read [error→L1]", (time.perf_counter() - _t0) * 1000.0)
            return self._session_registry.get(session_id)
        _timed_log("session_vm_read", (time.perf_counter() - _t0) * 1000.0)
        return self._apply_session_vm_read(session_id, db)

    # How long a "launching" marker stays valid. A launch (VM create + restore +
    # register) normally completes well inside this; past it we assume the launch
    # died and stop treating the session as registering (so forwards 404 fast
    # instead of waiting the full budget on every request for a dead launch).
    _LAUNCHING_TTL_SEC = 90.0

    def mark_session_launching(self, session_id: str):
        """Mark that a launch is in flight for this session. Called at the very
        start of /launch so the /proxy/{path} forwarder can distinguish the brief
        'VM not registered YET' window from a terminated/never-existed session.
        Cleared by register_session (success) or clear_session_launching (failure);
        also self-expires after _LAUNCHING_TTL_SEC."""
        if session_id:
            self._launching_sessions[session_id] = time.monotonic()

    def clear_session_launching(self, session_id: str):
        """Clear the launching marker (launch failed/aborted before registration).
        No-op if the session was never marked or already registered."""
        if session_id:
            self._launching_sessions.pop(session_id, None)

    def is_session_launching(self, session_id: str) -> bool:
        """True if a launch for this session started within the TTL and hasn't yet
        registered. Expired markers are pruned on read so they can't accumulate."""
        if not session_id:
            return False
        started = self._launching_sessions.get(session_id)
        if started is None:
            return False
        if time.monotonic() - started > self._LAUNCHING_TTL_SEC:
            self._launching_sessions.pop(session_id, None)
            return False
        return True

    # ============================================================
    # REACTIVE ROTATION (scheduler-free eternal mode)
    # ============================================================
    # Eternal mode used to rotate PRE-EMPTIVELY via an in-process timer (rotate
    # ~60s before max lifetime). That needs a scheduler and doesn't survive a
    # stateless/horizontally-scaled proxy. Instead, because state is now always
    # safe in S3 (save-on-suspend + save-on-terminate), we recover REACTIVELY:
    # when a request finds the session's VM dead, we launch a fresh VM, restore
    # from the last S3 checkpoint, relink the session, and serve the request on
    # the new VM. The cost is a one-time cold-start on that single request.
    #
    # No scheduler, no polling loop, no push notification of termination needed —
    # the trigger is simply the user's next request.

    async def get_vm_state(self, microvm_id: str) -> str:
        """Return the VM's authoritative lifecycle state from the control-plane
        get_microvm API (RUNNING / SUSPENDED / SUSPENDING / RESUMING / PENDING /
        TERMINATING / TERMINATED / FAILED), or "NOT_FOUND" if it no longer exists.

        On a transient/unknown API error, returns "UNKNOWN" — callers should treat
        UNKNOWN as "not confirmed dead" (retry) so a blip doesn't trigger a
        needless relaunch. This is the single source of truth for liveness; the
        VM endpoint itself has no status API (a refused connection is ambiguous).
        """
        try:
            detail = await asyncio.to_thread(
                lambda: self.get_lambda_poll_client().get_microvm(microvmIdentifier=microvm_id)
            )
            return detail.get("state", "UNKNOWN")
        except Exception as e:
            msg = str(e)
            if "ResourceNotFound" in msg or "NotFound" in msg:
                return "NOT_FOUND"
            logger.warning(f"get_vm_state({microvm_id}) inconclusive: {e}")
            return "UNKNOWN"

    def _get_recovery_state(self, session_id: str) -> "_RecoveryState":
        rs = self._recovery.get(session_id)
        if rs is None:
            rs = _RecoveryState()
            self._recovery[session_id] = rs
        return rs

    def assign_arrival_seq(self, session_id: str) -> int:
        """Assign a monotonic arrival sequence number for a request, captured at
        proxy entry (before any forward). Passed to recover_and_replay so queued
        requests replay in TRUE arrival order, not failed-forward-completion order."""
        return self._get_recovery_state(session_id).next_seq()

    def is_recovering(self, session_id: str) -> bool:
        """True if a reactive recovery is currently in progress for this session.

        Used by proxy_request to route requests that arrive DURING a recovery
        straight into the FIFO queue (via recover_and_replay) BEFORE attempting a
        forward — otherwise each request would race its own forward+state-check
        and enter the queue out of true arrival order (breaking FIFO)."""
        rs = self._recovery.get(session_id)
        return bool(rs and rs.in_progress)

    async def recover_and_replay(self, session_id: str, request_data: dict, seq: int | None = None,
                                 old_vm_id: str | None = None):
        """Reactive recovery entry point that PRESERVES FIFO request ordering.

        `seq` is the request's arrival sequence (from assign_arrival_seq at proxy
        entry). The queue is drained in seq order, so replay == true arrival order
        even though requests reach here after variable-latency failed forwards.

        `old_vm_id` is an OPTIONAL hint for the dead VM's id, used ONLY when the
        session→VM mapping is already gone (the Fix 2 tombstone path): with no
        mapping, get_session_vm returns None and _do_recover would otherwise lose
        the dead VM's memory/arch/notebook_id metadata and relaunch with defaults.
        Passing the tombstoned vm_id lets _do_recover resolve that metadata from
        the DB row (storage.vm_session_get) exactly as the mapped path does. When
        the mapping is still intact (the normal Fix 1 path) this is None and the
        live mapping supplies old_vm_id as before.

        Behavior:
          - If a recovery is already in progress for this session, the caller's
            request is ENQUEUED (FIFO) and it awaits its turn; the active
            recoverer will replay it in arrival order after restore. Returns the
            replayed httpx.Response.
          - Otherwise the caller becomes the recoverer: it launches a fresh VM,
            restores from S3 (with exponential-backoff retries), relinks routing,
            then replays the queued requests IN ORDER followed by its own.

        Raises RuntimeError if recovery ultimately fails (so the proxy can return
        a loud 5xx). On failure the empty new VM is terminated and NOT registered,
        so the good S3 checkpoint is preserved for a later retry.

        `request_data` = {"method", "path", "headers", "body"} for the caller's
        own request; its replayed response is returned to the caller.
        """
        rs = self._get_recovery_state(session_id)

        # EVERY caller (including the one that will do the recovery) enqueues its
        # own request with a future FIRST. This is the key to correct FIFO + no
        # orphaned requests: the recoverer drains the SAME queue that late
        # arrivals append to, so there is no separate "own request" served after
        # the drain loop (which was the orphan race). Order in the list = arrival
        # order = replay order.
        if seq is None:
            seq = rs.next_seq()  # fallback if caller didn't assign one at entry
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        rs.queue.append((seq, request_data, fut))

        if rs.in_progress:
            # A recovery is already running; the active recoverer will replay our
            # queued request in order. Just wait for our future.
            logger.info(f"🔁 Session {session_id}: recovery in progress — queued request seq={seq} (depth {len(rs.queue)})")
            return await fut  # httpx.Response, or raises on failure

        # We are the recoverer. Serialize with the lock (guards against a second
        # coroutine that passed the in_progress check before we set it).
        async with rs.lock:
            if rs.in_progress:
                # Another coroutine became the recoverer while we waited for the
                # lock — our request is already queued; just await it.
                return await fut

            rs.in_progress = True
            recover_error = None
            # B2: acquire the DISTRIBUTED recovery lock. rs.lock/in_progress above
            # already guarantee one recoverer PER PROCESS; this extends that to one
            # recoverer across the whole proxy FLEET. On SQLite the lock is
            # in-process and (because only this coroutine reaches here) always
            # free → always acquired → the "lost" branch below never runs, so
            # single-instance behavior is byte-for-byte unchanged.
            have_lock = False
            try:
                have_lock = await asyncio.to_thread(storage.try_session_lock, session_id)
            except Exception as e:
                logger.warning(f"🔁 try_session_lock failed for {session_id} "
                               f"(proceeding as sole recoverer): {e}")
                have_lock = True  # fail-open: better to recover than to stall
            try:
                new_vm_id = new_endpoint = None

                if not have_lock:
                    # Another INSTANCE is recovering this session. Don't launch a
                    # duplicate VM — wait for the winner to publish the new VM in
                    # the DB registry, then drain our queue against it.
                    logger.info(f"🔁 Session {session_id}: another instance holds the recovery "
                                f"lock — waiting for it to publish the new VM")
                    winner = await self._await_recovered_vm(session_id)
                    if winner is None:
                        recover_error = RuntimeError(
                            f"Reactive recovery: peer instance did not publish a VM for "
                            f"{session_id} within {RECOVER_LOCK_WAIT_TIMEOUT:.0f}s")
                    else:
                        new_vm_id, new_endpoint = winner["vm_id"], winner["endpoint"]

                # Double-check: the registry may already point to a LIVE vm
                # (recovered by a prior cycle). If so, skip launch and just drain.
                if have_lock and recover_error is None:
                    current = await self.get_session_vm_async(session_id)
                    if current:
                        state = await self.get_vm_state(current["vm_id"])
                        if state in VM_ALIVE_STATES:
                            new_vm_id, new_endpoint = current["vm_id"], current["endpoint"]
                            logger.info(f"🔁 Session {session_id}: VM already alive ({state}) — draining queue")

                    if new_vm_id is None:
                        # If the mapping is gone (Fix 2 tombstone path), seed the
                        # dead VM's id from the hint so _do_recover can still read
                        # memory/arch/notebook_id from its DB row instead of
                        # relaunching with defaults.
                        recover_from = current or ({"vm_id": old_vm_id} if old_vm_id else {})
                        recovered = await self._do_recover(session_id, recover_from)
                        if not recovered:
                            recover_error = RuntimeError(f"Reactive recovery failed for session {session_id}")
                        else:
                            new_vm_id, new_endpoint = recovered["vm_id"], recovered["endpoint"]
                            # The new VM is registered: attach the notebook's DAG
                            # (reconcile against the restored namespace) in the
                            # background. Lazy import: dag_service → storage/http
                            # clients only, but keep this module import-light.
                            try:
                                from proxy.notebook import dag_service
                                dag_service.attach_soon(session_id, None, expect_restore=True,
                                                        vm_manager=self)
                            except Exception as e:
                                logger.warning(f"🔁 DAG attach scheduling failed for {session_id}: {e}")

                if recover_error is None:
                    # Drain the queue in FIFO order, sequentially (cells must run in
                    # order). Loop until empty so requests that arrive DURING replay
                    # (they append while in_progress=True) are also served — no
                    # orphans, and no post-loop await to race against.
                    while rs.queue:
                        await self._replay_queue_in_order(rs, new_vm_id, new_endpoint)
            except Exception as e:
                recover_error = e
            finally:
                # B2: always release the distributed lock we acquired, so the next
                # recovery (this instance or another) can proceed. Releasing a lock
                # we don't hold is a safe no-op.
                if have_lock:
                    try:
                        await asyncio.to_thread(storage.release_session_lock, session_id)
                    except Exception as e:
                        logger.warning(f"🔁 release_session_lock failed for {session_id}: {e}")
                rs.in_progress = False
                if recover_error is not None:
                    # Fail every still-queued waiter loudly (recovery unusable).
                    self._drain_queue_with_error(rs, recover_error)
                if not rs.queue:
                    self._recovery.pop(session_id, None)

        # Our own request was in the queue and has been resolved (or failed) by
        # the drain above. Return its result (raises if recovery failed).
        return await fut

    async def _replay_one(self, vm_id: str, endpoint: str, request_data: dict):
        """Forward a single buffered request to a VM. Returns the httpx.Response."""
        token = await self.get_auth_token(vm_id)
        headers = dict(request_data.get("headers") or {})
        headers["X-aws-proxy-auth"] = token
        # Shared client; a replayed (possibly non-idempotent) request gets only
        # the transport's connect-phase retries — never an extra re-send here.
        return await http_clients.vm_client().request(
            method=request_data["method"],
            url=f"https://{endpoint}{request_data['path']}",
            headers=headers,
            content=request_data.get("body") or b"",
            timeout=120.0,
        )

    async def _replay_queue_in_order(self, rs: "_RecoveryState", vm_id: str, endpoint: str):
        """Replay all queued requests sequentially in ARRIVAL order (sorted by
        seq), resolving each waiter's future. Drains the queue."""
        # Snapshot + clear so requests that arrive AFTER this point start a fresh
        # cycle rather than being replayed twice. Sort by seq = true arrival order
        # (append order can differ due to variable failed-forward latency).
        pending = sorted(rs.queue, key=lambda item: item[0])
        rs.queue = []
        if pending:
            seqs = [s for s, _, _ in pending]
            logger.info(f"🔁 Replaying {len(pending)} queued request(s) in arrival order {seqs} on {vm_id}")
        for _seq, req, fut in pending:
            if fut.done():
                continue
            try:
                resp = await self._replay_one(vm_id, endpoint, req)
                fut.set_result(resp)
            except Exception as e:
                fut.set_exception(e)

    def _drain_queue_with_error(self, rs: "_RecoveryState", err: Exception):
        """Fail every queued waiter loudly (recovery could not complete)."""
        pending = rs.queue
        rs.queue = []
        for _seq, _req, fut in pending:
            if not fut.done():
                fut.set_exception(err)

    async def _await_recovered_vm(self, session_id: str) -> dict | None:
        """B2 loser path: poll the DB registry until the INSTANCE that holds the
        recovery lock publishes a fresh, live VM for this session, then return
        {vm_id, endpoint}. Returns None if nothing live appears within
        RECOVER_LOCK_WAIT_TIMEOUT (peer likely crashed — caller fails loudly and
        the next request retries, by which point the lock is free).

        We wait for a VM that is BOTH present in the registry AND in an alive
        control-plane state, so we never forward to the same dead VM that
        triggered recovery, nor to a half-published entry.
        """
        old = self._session_registry.get(session_id) or {}
        old_vm_id = old.get("vm_id")
        deadline = time.time() + RECOVER_LOCK_WAIT_TIMEOUT
        while time.time() < deadline:
            try:
                current = await asyncio.to_thread(storage.session_registry_get, session_id)
            except Exception as e:
                logger.warning(f"🔁 registry poll failed for {session_id}: {e}")
                current = None
            if current and current.get("vm_id") and current["vm_id"] != old_vm_id:
                state = await self.get_vm_state(current["vm_id"])
                if state in VM_ALIVE_STATES:
                    # Adopt the peer's VM as our L1 mapping and route to it.
                    self._session_registry[session_id] = current
                    logger.info(f"🔁 Session {session_id}: peer published live VM "
                                f"{current['vm_id']} ({state}) — forwarding queued requests")
                    return current
            await asyncio.sleep(RECOVER_LOCK_WAIT_POLL)
        return None

    async def _do_recover(self, session_id: str, current: dict) -> dict | None:
        """Launch a fresh VM + restore from S3 (with exponential-backoff retries),
        relink routing, and persist. Returns {vm_id, endpoint} on success.

        On restore failure after all retries: terminates the empty new VM and does
        NOT register it (so the good S3 checkpoint is never overwritten by an empty
        namespace), and returns None → caller fails loudly.
        """
        old_vm_id = current.get("vm_id")
        info = self.active_microvms.get(old_vm_id, {}) if old_vm_id else {}
        db = None
        if not info and old_vm_id:
            try:
                db = await asyncio.to_thread(storage.vm_session_get, old_vm_id)
            except Exception:
                db = None
        memory_mib = info.get("memory_mib") or (db or {}).get("memory_mib") or DEFAULT_MEMORY_MIB
        # pooled recovery ⇒ fixed idle (the pool freeze, see
        # routes/microvm.py::_fixed_lifecycle_values); non-pooled keeps the
        # session's saved idle unchanged.
        # max-duration in recovery is ALREADY always the fixed MAX_LIFETIME_SECONDS
        # value in both the pool-grant and launch_for_rotation fallback branches
        # below (never a per-session override), so no change is needed for it here.
        idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")) if _pool_enabled() \
            else (info.get("idle_timeout_sec") or (db or {}).get("idle_timeout_sec")
                  or int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")))
        # Display name for the relaunched VM. Prefer the live in-memory name; fall
        # back to the stable session_id. NOTE: we deliberately do NOT fall back to
        # vm_sessions.notebook_id — that is now the integer PK, not a name (it used
        # to hold the name string, which is the bug this refactor fixes).
        notebook_name = info.get("name") or session_id
        # The recovery VM belongs to the SAME notebook, so carry its integer
        # notebook_id (the PK) forward onto the new vm_sessions row. Read it from the
        # old session's DB row; None if unlinked. This must be the INT id, never the
        # name — the column is an integer FK now.
        try:
            _old_row = db if db is not None else (
                await asyncio.to_thread(storage.vm_session_get, old_vm_id) if old_vm_id else None)
            notebook_id = (_old_row or {}).get("notebook_id")
        except Exception:
            notebook_id = None
        # SAME-ARCH restore: the replacement VM is the arch the session was saved
        # on, so a notebook keeps running on the arch (and its pricing) it ran on.
        # Arch is read from the dead session (in-memory, else the DB row).
        architecture = info.get("architecture") or (db or {}).get("architecture")
        if old_vm_id and (not architecture
                          or not (info.get("memory_mib") or (db or {}).get("memory_mib"))):
            # No record of the arch/tier: read them from the dead VM's image name
            # rather than defaulting (an x86_64 session must not come back on arm64).
            try:
                _d = await self.get_microvm_detail(old_vm_id)
                img_arch, img_mem = arch_size_from_image((_d or {}).get("imageArn"))
            except Exception:
                img_arch, img_mem = None, None
            architecture = architecture or img_arch
            if img_mem and not (info.get("memory_mib") or (db or {}).get("memory_mib")):
                memory_mib = img_mem
        architecture = architecture or DEFAULT_IMAGE_ARCH

        if not IMAGE_ARN:
            logger.error(f"🔁 Reactive: MICROVM_IMAGE_ARN not set — cannot relaunch {session_id}")
            return None
        image_arn = f"{IMAGE_ARN}-{architecture}-{memory_mib}"

        # Per-phase timing for observability (stored on self.last_recovery_timings,
        # exposed via GET /recovery-timings so tests/dashboards can measure the
        # end-to-end cold-start breakdown: launch → healthy → restore → relink).
        phase = {}

        # The notebook's persisted env vars + secrets, exactly as /launch builds
        # them (same helper), for BOTH the pool-grant and the cold relaunch below.
        # Without them the recovered VM's os.environ / list_env() / list_secret()
        # would lack the user's keys. Unlinked session: platform env only + WARNING.
        env_vars, secrets, user_env_keys = await build_user_env(
            notebook_id, platform_env=self.launch_env_vars(),
            context=f"🔁 Reactive recovery {session_id}", warn_if_unlinked=True)

        # --- Try the VM pool FIRST (gated by POOL_ENABLED; grant-then-fallback,
        # design §10/§13.4 Option A). On 200, the pool manager already resumed +
        # rebound + restored the VM (restore_from=session_id), so we SKIP
        # launch_for_rotation + _wait_for_healthy_async + _restore_state_async
        # entirely and just relink routing. On a miss/error/timeout, _try_pool_grant
        # returns None and we fall straight through to the existing path below,
        # completely unchanged — never a double-restore. ---
        if _pool_enabled():
            # get_artifacts_bucket may do a blocking STS/S3 discovery on first use.
            artifacts_bucket = await asyncio.to_thread(self.get_artifacts_bucket)
            grant_payload = {
                "arch": architecture, "size": memory_mib, "session_id": session_id,
                "notebook_id": notebook_id, "notebook_name": notebook_name,
                "persistence_mode": "checkpoint", "checkpoint_enabled": True,
                "artifacts_bucket": artifacts_bucket,
                "env_vars": env_vars, "secrets": secrets, "user_env_keys": user_env_keys,
                "restore_from": session_id,
            }
            granted = await _try_pool_grant(grant_payload)
            if granted is not None:
                new_vm_id, new_endpoint = granted["vm_id"], granted["endpoint"]
                # Same adoption bookkeeping as the /launch pool-grant path: the
                # spare's mint time (not now) drives "Terminates in", and its
                # pool-residency SUSPENDED interval is seeded as cost.
                g_launched_iso, g_launched_epoch = adopt_pool_grant_times(granted)
                phase["launch_ms"] = 0.0
                phase["wait_healthy_ms"] = 0.0
                phase["restore_ms"] = 0.0
                _t = time.perf_counter()
                await self.register_session_async(session_id, new_vm_id, new_endpoint)
                self.active_microvms[new_vm_id] = self.active_microvms.get(new_vm_id, {})
                self.active_microvms[new_vm_id]["session_id"] = session_id
                self.active_microvms[new_vm_id]["restored_from"] = session_id  # VM card Origin hint
                self.active_microvms[new_vm_id]["architecture"] = architecture
                self.active_microvms[new_vm_id]["endpoint"] = new_endpoint
                self.active_microvms[new_vm_id]["launched_at"] = g_launched_epoch
                self.active_microvms[new_vm_id]["memory_mib"] = memory_mib
                self.active_microvms[new_vm_id].pop("_rotation_pending", None)
                self.cost_tracker.record_prior_suspended(
                    new_vm_id, g_launched_epoch, memory_mib=memory_mib,
                    architecture=architecture)

                def _persist_granted():
                    # Module-global `storage` resolved at call time (test seam).
                    storage.vm_session_create(
                        microvm_id=new_vm_id, notebook_id=notebook_id, notebook_name=notebook_name,
                        session_id=session_id, memory_mib=memory_mib, endpoint=new_endpoint,
                        idle_timeout_sec=idle_timeout,
                        max_duration_sec=int(os.environ.get("MAX_LIFETIME_SECONDS", "28800")),
                        checkpoint_enabled=True, architecture=architecture,
                        launched_at=g_launched_iso, prior_suspended_since=g_launched_iso,
                    )
                    if old_vm_id:
                        storage.vm_session_update_state(old_vm_id, "TERMINATED")

                try:
                    await asyncio.to_thread(_persist_granted)
                except Exception as e:
                    logger.warning(f"🔁 Pool-granted recovery: DB update warning for {session_id}: {e}")
                if old_vm_id:
                    try:
                        await asyncio.to_thread(self.terminate, old_vm_id)
                    except Exception:
                        pass
                phase["relink_ms"] = (time.perf_counter() - _t) * 1000
                await self._push_catalog_async(new_vm_id, new_endpoint, session_id)
                phase["restore_pkl_ms"] = self._last_restore_pkl_ms
                phase["total_ms"] = sum(phase.get(k, 0) for k in
                                        ("launch_ms", "wait_healthy_ms", "restore_ms", "relink_ms"))
                phase["new_vm_id"] = new_vm_id
                self.last_recovery_timings = phase
                logger.info(f"🔁 Pool-granted recovery complete: session {session_id} → {new_vm_id} "
                            f"(skipped launch+restore)")
                return {"vm_id": new_vm_id, "endpoint": new_endpoint}
            # miss/error -> fall through to the existing launch_for_rotation path
            # below, unchanged.

        logger.info(f"🔁 Reactive recovery for session {session_id}: launching {image_arn} ...")
        _t = time.perf_counter()
        try:
            new_vm_id, new_endpoint = await asyncio.to_thread(
                self.launch_for_rotation,
                image_arn=image_arn, memory_mib=memory_mib, idle_timeout_sec=idle_timeout,
                notebook_name=notebook_name, session_id=session_id, notebook_id=notebook_id,
                env_vars=env_vars, secrets=secrets, user_env_keys=user_env_keys,
            )
        except Exception as e:
            logger.error(f"🔁 Reactive: launch failed for {session_id}: {e}")
            return None
        phase["launch_ms"] = (time.perf_counter() - _t) * 1000

        # Health + restore with exponential backoff. Restore is idempotent (it
        # re-downloads from S3), so retrying is safe. We ONLY commit (register +
        # persist) after a SUCCESSFUL restore, so a partial/empty VM can never be
        # routed to or checkpointed over the good S3 state.
        restored = False
        last_err = None
        _t = time.perf_counter()
        try:
            await self._wait_for_healthy_async(new_vm_id, new_endpoint, timeout=90)
        except Exception as e:
            last_err = e
            logger.error(f"🔁 Reactive: new VM {new_vm_id} never healthy: {e}")
        phase["wait_healthy_ms"] = (time.perf_counter() - _t) * 1000

        if last_err is None:
            _t = time.perf_counter()
            for attempt in range(RECOVER_RESTORE_MAX_ATTEMPTS):
                try:
                    await self._restore_state_async(new_vm_id, new_endpoint, session_id)
                    restored = True
                    break
                except Exception as e:
                    last_err = e
                    if attempt < RECOVER_RESTORE_MAX_ATTEMPTS - 1:
                        delay = RECOVER_RESTORE_BACKOFF_BASE * (2 ** attempt)
                        logger.warning(f"🔁 Restore attempt {attempt+1}/{RECOVER_RESTORE_MAX_ATTEMPTS} "
                                       f"failed for {session_id}: {e} — retrying in {delay:.0f}s")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"🔁 Restore FAILED after {RECOVER_RESTORE_MAX_ATTEMPTS} attempts "
                                     f"for {session_id}: {e}")
            phase["restore_ms"] = (time.perf_counter() - _t) * 1000

        if not restored:
            # Fail closed: tear down the empty VM, keep the good S3 checkpoint intact.
            logger.error(f"🔁 Reactive recovery ABORTED for {session_id} — terminating empty VM {new_vm_id}, "
                         f"S3 checkpoint preserved for retry (last error: {last_err})")
            try:
                await asyncio.to_thread(self.terminate, new_vm_id)
            except Exception:
                pass
            return None

        # Commit: relink routing + persist ONLY after successful restore.
        _t = time.perf_counter()
        await self.register_session_async(session_id, new_vm_id, new_endpoint)
        self.active_microvms[new_vm_id]["session_id"] = session_id
        self.active_microvms[new_vm_id]["restored_from"] = session_id  # VM card Origin hint
        self.active_microvms[new_vm_id]["architecture"] = architecture  # same arch as the dead VM
        self.active_microvms[new_vm_id].pop("_rotation_pending", None)

        def _persist_recovered():
            # Module-global `storage` resolved at call time (test seam).
            storage.vm_session_create(
                microvm_id=new_vm_id, notebook_id=notebook_id, notebook_name=notebook_name,
                session_id=session_id,
                memory_mib=memory_mib, endpoint=new_endpoint, idle_timeout_sec=idle_timeout,
                max_duration_sec=int(os.environ.get("MAX_LIFETIME_SECONDS", "28800")),
                checkpoint_enabled=True, architecture=architecture,
            )
            if old_vm_id:
                storage.vm_session_update_state(old_vm_id, "TERMINATED")

        try:
            await asyncio.to_thread(_persist_recovered)
        except Exception as e:
            logger.warning(f"🔁 Reactive: DB update warning for {session_id}: {e}")

        # Defensively terminate the old VM in AWS (usually already dead; guards the
        # false-positive/suspended-race case so we don't leak a VM).
        if old_vm_id:
            try:
                await asyncio.to_thread(self.terminate, old_vm_id)
            except Exception:
                pass
        phase["relink_ms"] = (time.perf_counter() - _t) * 1000

        # The recovered VM restored its catalog from the (possibly stale) S3
        # checkpoint. Push a FRESH cloud catalog so it routes SQL against the
        # current source set. Best-effort — never fails the recovery.
        await self._push_catalog_async(new_vm_id, new_endpoint, session_id)
        phase["restore_pkl_ms"] = self._last_restore_pkl_ms  # from the VM's own restore timing
        phase["total_ms"] = phase.get("launch_ms", 0) + phase.get("wait_healthy_ms", 0) \
            + phase.get("restore_ms", 0) + phase.get("relink_ms", 0)
        phase["new_vm_id"] = new_vm_id
        self.last_recovery_timings = phase

        logger.info(
            f"🔁 Reactive recovery complete: session {session_id} → {new_vm_id} "
            f"[launch={phase['launch_ms']:.0f}ms healthy={phase['wait_healthy_ms']:.0f}ms "
            f"restore={phase.get('restore_ms',0):.0f}ms relink={phase['relink_ms']:.0f}ms "
            f"total={phase['total_ms']:.0f}ms]"
        )
        return {"vm_id": new_vm_id, "endpoint": new_endpoint}

    async def _wait_for_healthy_async(self, vm_id: str, endpoint: str, timeout: int = 60):
        """Poll the new VM's /health until 200 (async — used by reactive recovery)."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                token = await self.get_auth_token(vm_id)
                resp = await http_clients.vm_client().get(
                    f"https://{endpoint}/health",
                    headers={"X-aws-proxy-auth": token},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(f"VM {vm_id} did not become healthy in {timeout}s")

    async def _restore_state_async(self, vm_id: str, endpoint: str, session_id: str):
        """POST /restore-state on the new VM (async — used by reactive recovery)."""
        token = await self.get_auth_token(vm_id)
        resp = await http_clients.vm_client().post(
            f"https://{endpoint}/restore-state",
            headers={"X-aws-proxy-auth": token, "Content-Type": "application/json"},
            json={"session_id": session_id},
            timeout=120.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Restore failed: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Restore failed: {data.get('error')}")
        timings = data.get("restore_timings", {})
        if timings:
            self._last_restore_pkl_ms = timings.get("total_ms", 0)
            logger.info(f"    🔁 restore breakdown (in-VM): total={timings.get('total_ms', 0):.0f}ms")

    async def _push_catalog_async(self, vm_id: str, endpoint: str, session_id: str):
        """POST a FRESH cloud data-source catalog to a recovered VM's
        /data-catalog/resync (async — used after reactive recovery restore).

        A restored VM loads its catalog from the S3 checkpoint, which is a stale
        snapshot from when it was last saved. This re-derives the cloud catalog on
        the proxy (authoritative) and pushes it so the recovered VM routes SQL
        against the CURRENT set of sources, not the stale one. Best-effort: on any
        failure we log and continue (the next Data Sources panel refresh will
        resync anyway), so a push hiccup never fails an otherwise-good recovery.
        """
        try:
            # AWAIT the async builder — we're on the event loop here (this method
            # awaits get_auth_token below), so the sync version's blocking .result()
            # would freeze the loop for the whole cloud fan-out.
            entries = await self.build_cloud_catalog_entries_async(session_id=session_id)
            token = await self.get_auth_token(vm_id)
            await http_clients.vm_client().post(
                f"https://{endpoint}/data-catalog/resync",
                headers={"X-aws-proxy-auth": token, "Content-Type": "application/json"},
                json={"entries": entries},
                timeout=10.0,
            )
            logger.info(f"🔁 Reactive: pushed fresh catalog ({len(entries)} cloud entries) to recovered VM {vm_id}")
        except Exception as e:
            logger.warning(f"🔁 Reactive: catalog push to recovered VM {vm_id} failed "
                           f"(will resync on next panel refresh): {e}")
            emit_error_metric("microvm", "_push_catalog_async", type(e).__name__, 200)

    # ============================================================
    # USER-INSTALLED PACKAGE TRACKING
    # ============================================================

    def record_user_install(self, session_id: str, package: str, category: str):
        """Record a user-installed package for a session."""
        if session_id not in self._user_installed:
            self._user_installed[session_id] = []
        # Avoid duplicates
        existing = [p["package"] for p in self._user_installed[session_id]]
        if package.lower() not in [e.lower() for e in existing]:
            self._user_installed[session_id].append({"package": package, "category": category})

    def record_user_uninstall(self, session_id: str, package: str):
        """Drop a package (any PEP 503 spelling, any version pin) from a session's list."""
        from proxy.platform.user_packages import base_name, canonical_name
        target = canonical_name(base_name(package) or package)
        if session_id in self._user_installed:
            self._user_installed[session_id] = [
                p for p in self._user_installed[session_id]
                if canonical_name(base_name(p.get("package")) or p.get("package")) != target]

    def get_user_installed_packages(self, session_id: str) -> list[dict]:
        """Get list of user-installed packages for a session."""
        return self._user_installed.get(session_id, [])

    # ============================================================
    # AWS CLIENT
    # ============================================================

    def get_lambda_poll_client(self):
        """POLLING control-plane client — ADAPTIVE retries + bounded timeouts.

        Used by the high-frequency, latency-tolerant callers: the /instances
        dashboard poll, list_vms, get_vm_state, get_microvm_detail, and the
        startup recovery/reconcile fan-out.

        list_microvms/get_microvm are polled frequently (the /instances dashboard
        poll + the pool), and the account's ListMicrovms API is rate-limited. Under
        throttling, boto3's DEFAULT retry mode retries with little backoff and no
        client-side rate limiting, so calls pile up: each queued-then-retried call
        can sit long enough that its SigV4 signature EXPIRES (5-min window) →
        InvalidSignatureException, which then retries again — a death spiral
        (observed: /instances taking 25-65s, signature-expired errors). "adaptive"
        mode adds client-side throttling-aware backoff (it slows the client when it
        sees ThrottlingException) which is exactly the right behavior here; the
        read/connect timeouts stop a single call from hanging into the signature
        window; tcp_keepalive keeps the pooled socket alive.
        """
        if self._lambda_poll_client is None:
            self._lambda_poll_client = boto3.client(
                "lambda-microvms", region_name=AWS_REGION,
                config=botocore.config.Config(
                    retries={"max_attempts": 5, "mode": "adaptive"},
                    connect_timeout=5,
                    read_timeout=20,
                    tcp_keepalive=True,
                ),
            )
        return self._lambda_poll_client

    def get_lambda_launch_client(self):
        """LAUNCH control-plane client — a SEPARATE boto3 client with its OWN
        connection pool and its OWN retry token bucket, used by the
        latency-sensitive interactive path: create_microvm_auth_token, the launch's
        wait-for-RUNNING get_microvm poll, run_microvm, and resume/suspend/terminate
        on the interactive path.

        It uses "standard" retry mode (NOT "adaptive") on purpose: the adaptive
        client-side rate limiter on the POLL client slows *every* call on that
        client when the /instances poll pushes ListMicrovms into throttling. A
        separate client means the dashboard/poll-induced backoff cannot bleed into
        a concurrent launch — the exact "new notebook takes longer while RunAll /
        the dashboard poll is active" symptom. NOTE: the AWS account's SERVER-SIDE
        control-plane rate limits are still shared across both clients; this split
        only removes the SELF-INFLICTED client-side serialization, which is the
        dominant local effect.
        """
        if self._lambda_launch_client is None:
            self._lambda_launch_client = boto3.client(
                "lambda-microvms", region_name=AWS_REGION,
                config=botocore.config.Config(
                    retries={"max_attempts": 5, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=20,
                    tcp_keepalive=True,
                ),
            )
        return self._lambda_launch_client

    def get_lambda_client(self):
        """Alias of get_lambda_poll_client(). Its one caller is the terminal shell
        token fetch (get_shell_auth_token); launch-path callers use
        get_lambda_launch_client() explicitly."""
        return self.get_lambda_poll_client()

    def _get_sts_client(self):
        """Cached STS client — reused for get_artifacts_bucket's one-time
        account resolution instead of constructing a new client each call."""
        if self._sts_client is None:
            self._sts_client = boto3.client("sts", region_name=AWS_REGION)
        return self._sts_client

    def _get_s3_client(self):
        """Cached S3 client — reused by stage_catalog_to_s3 /
        checkpoint_manifest_exists instead of constructing a new client per call."""
        if self._s3_client is None:
            self._s3_client = boto3.client("s3", region_name=AWS_REGION)
        return self._s3_client

    # ── Async control-plane wrappers ──────────────────────────────────────────
    # The lambda-microvms boto3 client is synchronous, so calling it directly from
    # an async route handler blocks the proxy's single event loop for the whole
    # round-trip (100ms–seconds), starving unrelated requests (e.g. the Run History
    # card). These helpers run the blocking SDK call in a worker thread via
    # asyncio.to_thread so handlers stay non-blocking. get_vm_state (above) already
    # follows this pattern; these cover the other control-plane ops that route
    # handlers were invoking inline.

    async def get_microvm_detail(self, microvm_id: str) -> dict:
        """Full control-plane detail for a VM (state, endpoint, ...) via get_microvm,
        off the event loop. Raises on error (caller decides how to handle); use
        get_vm_state() when you only need the liveness string and want the
        error-swallowing NOT_FOUND/UNKNOWN semantics.

        POLL client: this serves the /instances dashboard detail and the
        /resume poll. The cold-launch wait-for-RUNNING loop uses the separate
        get_microvm_detail_launch() on the launch client instead."""
        return await asyncio.to_thread(
            lambda: self.get_lambda_poll_client().get_microvm(microvmIdentifier=microvm_id)
        )

    async def get_microvm_detail_launch(self, microvm_id: str) -> dict:
        """Same as get_microvm_detail, but on the LAUNCH client. Used ONLY
        by the cold-launch wait-for-RUNNING poll — the latency-critical loop that
        must not queue behind the poll client's adaptive backoff when the /instances
        dashboard is hot."""
        return await asyncio.to_thread(
            lambda: self.get_lambda_launch_client().get_microvm(microvmIdentifier=microvm_id)
        )

    async def resume_vm(self, microvm_id: str) -> None:
        """Resume a SUSPENDED VM (control-plane resume_microvm), off the event loop.
        LAUNCH client: resume is on the interactive latency path."""
        await asyncio.to_thread(
            lambda: self.get_lambda_launch_client().resume_microvm(microvmIdentifier=microvm_id)
        )

    async def suspend_vm(self, microvm_id: str) -> None:
        """Suspend a RUNNING VM (control-plane suspend_microvm), off the event loop.
        LAUNCH client: suspend is on the interactive latency path."""
        await asyncio.to_thread(
            lambda: self.get_lambda_launch_client().suspend_microvm(microvmIdentifier=microvm_id)
        )

    async def list_vms(self) -> dict:
        """List ALL VMs in the account (control-plane list_microvms), off the event
        loop. Returns the raw response dict ({items: [...]}).

        list_microvms is PAGINATED — and the account list includes every
        not-yet-reaped TERMINATED VM, so after churn the list can be hundreds of
        rows (observed 219: 212 TERMINATED + 7 live). A single call sees only the
        first page, so VMs on later pages silently vanish from /instances — the tab
        for such a VM then looks disconnected and wrongly pops the reconnect panel
        even though the VM is alive/SUSPENDED. We must walk every page and aggregate.

        PAGE SIZE — why maxResults=50:
        - The ListMicrovms API caps maxResults at 50 (anything >50 is a
          ValidationException); the default page is ~10. We request the max.
        - Why it matters: EVERY control-plane round-trip is paced by the lambda
          client's adaptive-retry backoff under ListMicrovms throttling, so the
          cost of this call is dominated by the NUMBER OF PAGES, not the row count.
          Fewer pages = fewer throttle-exposure points = far less tail latency.
        - Measured on a real 219-VM list (212 TERMINATED + 7 live): default page
          = 22 round-trips, ~2s unthrottled and up to 30–48s while throttled (one
          page alone spiked to 4.2s on backoff); maxResults=50 = 5 round-trips,
          ~0.4s and FLAT (~85ms/page) even under the same conditions. So ~4.4x
          fewer calls and the difference between "sub-second" and "tens of
          seconds" when ListMicrovms is hot.
        - The TERMINATED backlog that bloats this list is AWS control-plane
          retention (no documented reap window, empirically hours-to-days) and
          cannot be filtered server-side (ListMicrovms has no state filter), so the
          only lever we control is walking the list cheaply. The caller filters
          TERMINATED/TERMINATING out after aggregation.
        Bounded (max_pages) so a runaway token can't loop forever."""
        _PAGE = 50  # ListMicrovms hard max (ValidationException above 50)
        def _list_all() -> dict:
            client = self.get_lambda_poll_client()  # POLL client
            items = []
            token = None
            max_pages = 40  # safety bound: 40 * 50 = 2000 VMs, far beyond real use
            for _ in range(max_pages):
                resp = (client.list_microvms(maxResults=_PAGE, nextToken=token)
                        if token else client.list_microvms(maxResults=_PAGE))
                items.extend(resp.get("items", []) or [])
                token = resp.get("nextToken")
                if not token:
                    break
            return {"items": items}
        return await asyncio.to_thread(_list_all)

    async def terminate_vm(self, microvm_id: str) -> None:
        """Terminate a VM (control-plane terminate_microvm), off the event loop.
        LAUNCH client: terminate is on the interactive latency path."""
        await asyncio.to_thread(
            lambda: self.get_lambda_launch_client().terminate_microvm(microvmIdentifier=microvm_id)
        )

    async def run_vm(self, **params) -> dict:
        """Launch a VM (control-plane run_microvm), off the event loop. Returns the
        raw response dict (microvmId, endpoint, ...). LAUNCH client: this
        IS the cold-launch, the most latency-critical control-plane call."""
        return await asyncio.to_thread(
            lambda: self.get_lambda_launch_client().run_microvm(**params)
        )

    # ============================================================
    # AUTH TOKENS
    # ============================================================

    async def get_auth_token(self, microvm_id: str) -> str:
        """Get a cached or fresh auth token for a MicroVM.

        Async because the token cache interface is async (see token_cache.py) so
        a future Redis/ElastiCache backend swaps in without changing callers.
        """
        cached = await self.token_cache.get(microvm_id)
        if cached and time.time() < cached["expires_at"]:
            return cached["token"]

        # Single-flight: concurrent cache misses for the same VM (launch burst:
        # /health, /packages, /image-tiers, ...) share ONE mint. The task is
        # shielded so a cancelled first waiter does not abort the mint for the
        # others; a task bound to another (e.g. closed test) loop is not reused.
        loop = asyncio.get_running_loop()
        task = self._token_inflight.get(microvm_id)
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self._mint_auth_token(microvm_id))
            self._token_inflight[microvm_id] = task

            def _done(t: asyncio.Task, _id: str = microvm_id) -> None:
                if self._token_inflight.get(_id) is t:
                    self._token_inflight.pop(_id, None)
                if not t.cancelled():
                    # Mark the exception retrieved so a failure whose waiters
                    # were all cancelled does not log "never retrieved".
                    t.exception()

            task.add_done_callback(_done)
        # Shielded wait: cancelling this waiter never cancels the mint. Uses
        # asyncio.wait rather than asyncio.shield because 3.12+ shield logs
        # "exception in shielded future" when the mint fails after every waiter
        # was cancelled (the failure is already surfaced to live waiters).
        await asyncio.wait({task})
        return task.result()

    async def _mint_auth_token(self, microvm_id: str) -> str:
        """Mint (and cache) a fresh auth token; one retry, failures not cached."""
        cached = await self.token_cache.get(microvm_id)
        if cached and time.time() < cached["expires_at"]:
            return cached["token"]

        logger.info(f"Fetching new auth token for {microvm_id}")
        # Offload the synchronous SDK call to a worker thread so a token
        # cache MISS on the launch/forward path doesn't freeze the single event
        # loop for the full SigV4-signed control-plane round trip. Use the LAUNCH
        # client so this isn't slowed by adaptive backoff from the
        # /instances dashboard poll.
        _t0 = time.perf_counter()
        for _attempt in (1, 2):
            try:
                response = await asyncio.to_thread(
                    lambda: self.get_lambda_launch_client().create_microvm_auth_token(
                        microvmIdentifier=microvm_id,
                        expirationInMinutes=30,
                        allowedPorts=[{"allPorts": {}}],
                    )
                )
                break
            except Exception as e:
                if _attempt == 2:
                    raise
                logger.warning(
                    f"Auth token mint for {microvm_id} failed "
                    f"({type(e).__name__}); retrying once"
                )
                await asyncio.sleep(0.2)
        _timed_log("auth_token boto3 [launch client]", (time.perf_counter() - _t0) * 1000.0)

        token = response["authToken"]["X-aws-proxy-auth"]
        await self.token_cache.set(microvm_id, {
            "token": token,
            "expires_at": time.time() + (25 * 60),
        }, ttl_seconds=25 * 60)
        return token

    async def get_shell_auth_token(self, microvm_id: str) -> str:
        """Get a shell auth token for WebSocket terminal access.

        Shell tokens use the SHELL_INGRESS connector and are passed
        via WebSocket subprotocols (not HTTP headers).
        """
        cache_key = f"shell:{microvm_id}"
        cached = await self.token_cache.get(cache_key)
        if cached and time.time() < cached["expires_at"]:
            return cached["token"]

        logger.info(f"Fetching shell auth token for {microvm_id}")
        # Deliberate get_lambda_client() (poll-client alias): this is the terminal
        # WebSocket connect path, NOT the warm-pool/launch latency path, so it is
        # intentionally left on the shared poll client (the launch client is
        # scoped to the interactive launch path).
        client = self.get_lambda_client()
        # Blocking control-plane SDK call → worker thread (off the event loop).
        response = await asyncio.to_thread(
            lambda: client.create_microvm_shell_auth_token(
                microvmIdentifier=microvm_id,
                expirationInMinutes=30,
            )
        )

        token = response["authToken"]["X-aws-proxy-auth"]
        await self.token_cache.set(cache_key, {
            "token": token,
            "expires_at": time.time() + (25 * 60),
        }, ttl_seconds=25 * 60)
        return token

    # ============================================================
    # ARTIFACTS BUCKET
    # ============================================================

    def get_artifacts_bucket(self) -> str | None:
        """Get the artifacts bucket name (from env or discovered)."""
        if self._artifacts_bucket:
            return self._artifacts_bucket
        try:
            # Cached STS client — one-time account resolution; the result
            # is memoized in self._artifacts_bucket so this STS call happens at most
            # once per process. The launch route calls this via asyncio.to_thread
            # so the first (blocking) resolution stays off the event loop.
            sts = self._get_sts_client()
            account_id = sts.get_caller_identity()["Account"]
            self._artifacts_bucket = f"microvm-sandbox-artifacts-{account_id}-{AWS_REGION}"
            return self._artifacts_bucket
        except Exception:
            pass
        return None

    # ============================================================
    # VM ROTATION HELPERS
    # ============================================================

    def launch_env_vars(self) -> dict:
        """Config values propagated into EVERY MicroVM's os.environ at launch, via
        runHookPayload.env_vars → the /run hook's injection loop. Single source of
        truth so the main launch and rotation launch stay consistent — add new VM
        runtime knobs here (and to config.sh) and both paths pick them up.

        All values are sourced from the proxy's environment (i.e. config.sh), with
        defaults matching the project defaults so nothing breaks if a var is unset.
        The in-VM code MUST read these at call time (not module import), because the
        /run hook fires AFTER the app is imported into the Firecracker snapshot.
        """
        env = {
            "AWS_REGION": os.environ.get("AWS_REGION", "us-west-2"),
            "ATHENA_DB": os.environ.get("ATHENA_DB", "microvm_demo_db"),
            "ATHENA_WORKGROUP": os.environ.get("ATHENA_WORKGROUP", "microvm-demo"),
            "DATASOURCE_ATHENA_DATABASES": os.environ.get("DATASOURCE_ATHENA_DATABASES", ""),
            "NOTEBOOK_MAX_DISPLAY_ROWS": os.environ.get("NOTEBOOK_MAX_DISPLAY_ROWS", "50"),
            "EXECUTION_TIMEOUT_SECONDS": os.environ.get("EXECUTION_TIMEOUT_SECONDS", "60"),
            # Per-arch pricing table + default arch, so the notebook's
            # estimate_session_cost() helper applies the SAME rates the proxy uses
            # (cost dollars are Python/config, not schema — see cost_tracker.py).
            # Empty string is fine: the helper falls back to the built-in arm64
            # rates just like the proxy does.
            "MICROVM_PRICING_JSON": os.environ.get("MICROVM_PRICING_JSON", ""),
            "DEFAULT_IMAGE_ARCH": os.environ.get("DEFAULT_IMAGE_ARCH", "arm64"),
            # Arch-agnostic image base name. The in-VM log readers
            # (app.notebook.helpers.logs) build this notebook's own CloudWatch log
            # group — /aws/lambda-microvms/{IMAGE_BASE_NAME}-{arch}-{mem} — so a
            # no-arg read_logs() can default to "this notebook's logs". Same base
            # the proxy uses to name images + resolve log groups (logs route).
            "IMAGE_BASE_NAME": os.environ.get("IMAGE_BASE_NAME", "notebook-microvm"),
            # Default memory tier (config.sh), so the in-VM cost/profiling helpers
            # fall back to the same tier the proxy prices with.
            "DEFAULT_MEMORY_MIB": str(DEFAULT_MEMORY_MIB),
        }
        bucket = self.get_artifacts_bucket()
        if bucket:
            env["ARTIFACT_BUCKET"] = bucket

        # Aurora RDS Data API config (notebook data source over HTTPS/443). Only
        # propagated when a cluster ARN is set, so if Aurora isn't provisioned the
        # in-VM catalog/router simply skips Aurora and behaves exactly as before.
        # The VM authenticates as the READ-ONLY user via the RO secret ARN.
        aurora_cluster_arn = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
        if aurora_cluster_arn:
            env["AURORA_DATA_API_CLUSTER_ARN"] = aurora_cluster_arn
            env["AURORA_DATA_API_SECRET_ARN"] = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
            env["AURORA_DATA_API_DATABASE"] = os.environ.get(
                "AURORA_DATA_API_DATABASE", os.environ.get("AURORA_DB_NAME", "microvm_db")
            )
            # Max rows a notebook Aurora query returns over the Data API. The Data
            # API caps each response at ~1MB, so an unbounded SELECT would truncate
            # silently; the VM auto-injects `LIMIT AURORA_MAX_ROWS` when the query
            # has none, and warns if the response still hit the 1MB ceiling (wide
            # rows can truncate below this row count). Tunable per-deployment.
            env["AURORA_MAX_ROWS"] = os.environ.get("AURORA_MAX_ROWS", "25000")

        # Bedrock Knowledge Base id (notebook data source via read_bedrock_kb /
        # ask_bedrock_kb, which call bedrock-agent-runtime from inside the VM under
        # its exec role). Only propagated when a KB is configured (i.e. launched
        # with --WithKnowledgeBase → BEDROCK_KB_ID sourced from scripts/.kb_env);
        # when unset the in-VM readers raise a clear "no KB configured" error and
        # the KB simply doesn't appear as a data source. BEDROCK_MODEL_ID is passed
        # too so ask_bedrock_kb (RetrieveAndGenerate) uses the project's model for
        # answer generation.
        # BEDROCK_MODEL_ID is ALWAYS propagated into the VM (not gated on the KB):
        # the in-VM LLM helpers (app/notebook/helpers/llm.py — invoke_llm, llm_map,
        # llm_classify, …) read it at call time, so the VM must use the SAME model as
        # the proxy/agent. Without this the in-cell helpers would fall back to their
        # own default and silently diverge from the project model. scripts/config.sh
        # is the only source of the value: when the proxy has none, the VM gets none
        # and the helpers raise a clear "BEDROCK_MODEL_ID is unset" error.
        bedrock_model_id = os.environ.get("BEDROCK_MODEL_ID", "").strip()
        if bedrock_model_id:
            env["BEDROCK_MODEL_ID"] = bedrock_model_id
        bedrock_kb_id = os.environ.get("BEDROCK_KB_ID", "")
        if bedrock_kb_id:
            env["BEDROCK_KB_ID"] = bedrock_kb_id
            # KB RAG answer-generation model (ask_bedrock_kb → RetrieveAndGenerate).
            # Decoupled from BEDROCK_MODEL_ID: the chat model (Haiku 5.5) can lack a
            # built-in KB prompt template, which RetrieveAndGenerate rejects; this is
            # set separately in scripts/config.sh (BEDROCK_KB_GENERATION_MODEL).
            # Only meaningful with a KB.
            kb_generation_model = os.environ.get("BEDROCK_KB_GENERATION_MODEL", "").strip()
            if kb_generation_model:
                env["BEDROCK_KB_GENERATION_MODEL"] = kb_generation_model

        # SageMaker managed training / batch transform (notebook sm_* helpers submit
        # jobs that run on SageMaker-managed compute). Only propagated when the
        # execution role ARN is set (i.e. setup_sagemaker.sh was run and its
        # scripts/.sagemaker_env sourced); when unset the in-VM sm_* helpers raise a
        # clear "SageMaker not configured" error and nothing else changes. The VM
        # exec role submits the job + PassRoles THIS role; SageMaker then assumes it.
        sagemaker_role_arn = os.environ.get("SAGEMAKER_EXEC_ROLE_ARN", "")
        if sagemaker_role_arn:
            env["SAGEMAKER_EXEC_ROLE_ARN"] = sagemaker_role_arn
            env["SAGEMAKER_TRAIN_INSTANCE"] = os.environ.get("SAGEMAKER_TRAIN_INSTANCE", "ml.m5.xlarge")
            env["SAGEMAKER_TRANSFORM_INSTANCE"] = os.environ.get("SAGEMAKER_TRANSFORM_INSTANCE", "ml.m5.xlarge")
            env["SAGEMAKER_ARTIFACT_PREFIX"] = os.environ.get("SAGEMAKER_ARTIFACT_PREFIX", "sagemaker/")
            # SageMaker endpoints: the custom serving image + serverless defaults. Only
            # propagate the image when it's actually set (built + pushed by setup);
            # blank ⇒ sm_deploy_* raises a clear "serving image not configured" error.
            serving_image = os.environ.get("SAGEMAKER_SERVING_IMAGE", "")
            if serving_image:
                env["SAGEMAKER_SERVING_IMAGE"] = serving_image
            env["SAGEMAKER_SERVERLESS_MEMORY_MB"] = os.environ.get("SAGEMAKER_SERVERLESS_MEMORY_MB", "2048")
            env["SAGEMAKER_SERVERLESS_MAX_CONCURRENCY"] = os.environ.get("SAGEMAKER_SERVERLESS_MAX_CONCURRENCY", "5")
        return env

    async def build_cloud_catalog_entries_async(self, session_id: str = None,
                                                 force_refresh: bool = False) -> list:
        """Discover every CLOUD data source + its schema (S3/DynamoDB/Athena/Aurora/...)
        in parallel and return fully-formed catalog entries to push to the VM.
        The proxy is authoritative for cloud schemas (the VM discovers only local
        /tmp files). This is the SINGLE builder shared by the launch payload and
        the /datasources/catalog panel-refresh resync, so the two can't drift.

        force_refresh=True bypasses the registry's TTL catalog cache (used by the
        explicit Data Sources panel refresh so the user always sees live schemas);
        the launch path leaves it False to hit the cache.

        Awaiting the native coroutine keeps the single-loop proxy free during the
        multi-second cloud fan-out (a blocking wrapper on the loop would stall
        every other request, including streamed AI chat). Best-effort: returns []
        on any failure so a discovery hiccup never blocks a VM launch."""
        try:
            from proxy.platform.datasources import registry
            return await registry.discover_full_catalog(session_id=session_id, force_refresh=force_refresh)
        except Exception as e:
            logger.warning(f"Cloud catalog discovery failed (VM will have no cloud sources until refresh): {e}")
            emit_error_metric("microvm", "build_cloud_catalog_entries", type(e).__name__, 500)
            return []

    def stage_catalog_to_s3(self, session_id: str, catalog_entries: list) -> str | None:
        """Stage fully-formed cloud catalog entries in S3 and return the object KEY.

        WHY: the RunMicrovm `runHookPayload` is hard-capped at 4096 bytes by the
        AWS API. Fully-formed catalog entries (columns + samples + metadata for
        every cloud source) blow past that, so we CANNOT inline them in the launch
        payload. Instead we write them to S3 — which the VM already reads under its
        execution role — and pass only the small object key in the payload. The VM
        downloads + loads them in the /run hook.

        Written to sessions/{session_id}/launch_catalog.json so it shares the
        session prefix (covered by the same lifecycle expiry as checkpoints) and is
        naturally scoped/overwritten per launch. Best-effort: returns None on any
        failure (no bucket, no session_id, empty entries, or S3 error) so a staging
        hiccup degrades to "VM has no cloud sources until the panel refresh resync"
        rather than blocking the launch.
        """
        if not session_id or not catalog_entries:
            return None
        bucket = self.get_artifacts_bucket()
        if not bucket:
            logger.warning("No artifacts bucket — cannot stage launch catalog to S3")
            emit_error_metric("microvm", "stage_catalog_to_s3", "NoArtifactsBucket", 500)
            return None
        key = f"sessions/{session_id}/launch_catalog.json"
        try:
            import json as _json
            s3 = self._get_s3_client()  # cached S3 client
            body = _json.dumps(catalog_entries)
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
            )
            logger.info(
                f"Staged {len(catalog_entries)} cloud catalog entries to "
                f"s3://{bucket}/{key} ({len(body)} B)"
            )
            return key
        except Exception as e:
            logger.warning(f"Failed to stage launch catalog to S3 (VM will have no cloud sources until refresh): {e}")
            emit_error_metric("microvm", "stage_catalog_to_s3", type(e).__name__, 500)
            return None

    def checkpoint_manifest_exists(self, session_id: str) -> bool | None:
        """Does a namespace checkpoint exist in S3 for this session?

        Probes sessions/{session_id}/manifest.json — the SAME object the in-VM
        restore keys on (app/platform/checkpoint/manager.py restore(): "NO
        manifest.json → nothing was ever checkpointed → fresh session"). Lets the
        launch route skip the whole /restore-state round-trip (~2s on a resumed
        spare) when there is provably nothing to restore — a notebook that was
        opened before but never produced a saved namespace still carries a
        session_id, so without this probe every such reopen pays a full restore
        just to discover an empty checkpoint.

        Tri-state, fail-safe toward restoring:
          True  — manifest present → a real checkpoint exists, DO restore.
          False — confirmed 404    → no checkpoint, SKIP restore.
          None  — couldn't tell (no bucket / S3 error) → caller MUST preserve
                  today's behavior and restore, so an availability blip can never
                  silently drop a user's real saved state.
        """
        if not session_id:
            return False
        bucket = self.get_artifacts_bucket()
        if not bucket:
            return None
        key = f"sessions/{session_id}/manifest.json"
        try:
            s3 = self._get_s3_client()  # cached S3 client
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception as e:
            # Any "not found" shape (ClientError 404/NoSuchKey) → provably absent.
            if isinstance(getattr(e, "response", None), dict):
                status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                err_code = e.response.get("Error", {}).get("Code")
                if status == 404 or err_code in ("404", "NoSuchKey", "NotFound"):
                    return False
            # Unknown error (perms, network, throttle) — fail safe: let the caller restore.
            logger.warning(f"checkpoint_manifest_exists probe inconclusive for {session_id} ({e}); will not skip restore")
            return None

    def launch_for_rotation(self, image_arn: str, memory_mib: int, idle_timeout_sec: int, notebook_name: str, session_id: str, notebook_id=None,
                            env_vars: dict | None = None, secrets: list | None = None,
                            user_env_keys: dict | None = None) -> tuple:
        """
        Launch a bare VM for rotation (no restore payload — state applied separately).
        Uses the same API params as the main launch to ensure identical config.
        env_vars / secrets / user_env_keys: what build_user_env returned for the
        session's notebook (the caller is async and builds them, since this runs in
        a worker thread). env_vars=None means platform env only (launch_env_vars()).
        Returns (microvm_id, endpoint).
        """
        import json

        # LAUNCH client: rotation is a reactive-recovery relaunch on the
        # interactive latency path, not background polling.
        client = self.get_lambda_launch_client()
        bucket = self.get_artifacts_bucket()
        max_duration_sec = int(os.environ.get("MAX_LIFETIME_SECONDS", "28800"))

        run_payload = json.dumps({
            "notebook_name": notebook_name,
            # Carry the notebook's integer PK forward onto the rotated VM so
            # this_notebook() reports the same identity across a rotation.
            "notebook_id": notebook_id,
            "session_id": session_id,
            "checkpoint_enabled": True,
            "persistence_mode": "checkpoint",
            "artifacts_bucket": bucket,
            # Same runtime knobs + the notebook's user env/secrets as the main
            # launch (see build_user_env) so rotated VMs stay consistent.
            "env_vars": env_vars if env_vars is not None else self.launch_env_vars(),
            "secrets": secrets or [],
            "user_env_keys": user_env_keys or {},
        })

        params = {
            "imageIdentifier": image_arn,
            "ingressNetworkConnectors": [INGRESS_CONNECTOR, SHELL_INGRESS_CONNECTOR],
            "egressNetworkConnectors": [EGRESS_CONNECTOR],
            "idlePolicy": {
                "autoResumeEnabled": True,
                "maxIdleDurationSeconds": idle_timeout_sec,
                "suspendedDurationSeconds": max_duration_sec,
            },
            "maximumDurationInSeconds": max_duration_sec,
            "runHookPayload": run_payload,
        }

        # Runtime execution role is mandatory — without it the VM launches with NO
        # AWS credentials (NOT a build-role fallback), so every data call/checkpoint
        # fails. Fail loudly rather than rotate in a broken, credential-less VM.
        if not EXEC_ROLE_ARN:
            raise RuntimeError(
                "MICROVM_EXEC_ROLE_ARN is not set — refusing to launch a rotation VM "
                "with no execution role (it would have no AWS credentials)."
            )
        params["executionRoleArn"] = EXEC_ROLE_ARN

        resp = client.run_microvm(**params)
        vm_id = resp["microvmId"]
        endpoint = resp["endpoint"]

        # Track locally — no session_id yet (assigned after swap completes)
        self.active_microvms[vm_id] = {
            "endpoint": endpoint,
            "name": notebook_name,
            "launched_at": time.time(),
            "memory_mib": memory_mib,
            # From the image launched ({base}-{arch}-{mem}), so the VM never shows a
            # default arch while it boots and restores (the caller sets it again on commit).
            "architecture": arch_size_from_image(image_arn)[0],
            "idle_timeout_sec": idle_timeout_sec,
            "max_duration_sec": max_duration_sec,
            "_rotation_pending": True,  # Internal flag: not yet serving traffic
            "_502_strikes": 0,
        }

        logger.info(f"🔄 Rotation: launched bare VM {vm_id} at {endpoint}")
        return vm_id, endpoint

    def terminate(self, microvm_id: str):
        """Terminate a MicroVM via the AWS API.
        LAUNCH client: terminate is on the interactive latency path."""
        try:
            client = self.get_lambda_launch_client()
            client.terminate_microvm(microvmIdentifier=microvm_id)
            self.active_microvms.pop(microvm_id, None)
            logger.info(f"Terminated VM: {microvm_id}")
        except Exception as e:
            logger.warning(f"Terminate failed for {microvm_id}: {e}")

    def restore_session_registry_from_db(self):
        """Rebuild the session→VM registry for VMs still alive after a proxy restart.

        The proxy is stateless across restarts; this re-links each live session to
        its VM so /proxy/{path} routes immediately. Dead VMs are cleaned from the DB.
        (There are no in-process timers to restore — eternal mode recovers reactively
        on the next request, and suspended-VM state is saved in the /suspend hook.)
        """
        try:
            active_sessions = storage.vm_session_list_active()
            client = self.get_lambda_poll_client()  # POLL client: startup reconcile fan-out

            for session in active_sessions:
                microvm_id = session.get("microvm_id")
                session_id = session.get("session_id")
                endpoint = session.get("endpoint", "")

                if not microvm_id or not session_id:
                    continue

                # Verify VM is still alive in AWS before re-registering
                try:
                    detail = client.get_microvm(microvmIdentifier=microvm_id)
                    vm_state = detail.get("state", "UNKNOWN")
                    if vm_state in ("TERMINATED", "TERMINATING", "FAILED"):
                        logger.info(f"   Skipping dead VM {microvm_id} (state={vm_state}) — cleaning DB")
                        storage.vm_session_mark_terminated(microvm_id)
                        continue
                    # Update endpoint from AWS if available (may have changed)
                    if detail.get("endpoint"):
                        endpoint = detail["endpoint"]
                except Exception as e:
                    logger.warning(f"   Cannot verify VM {microvm_id}: {e} — skipping")
                    continue

                # Restore session→VM mapping so /proxy/{path} works immediately
                if endpoint:
                    self.register_session(session_id, microvm_id, endpoint)
                    # Also repopulate active_microvms from the DB row. Without this,
                    # a restored VM is absent from active_microvms, so consumers that
                    # read active_microvms[vm_id]["memory_mib"]/["architecture"] fall
                    # back to hardcoded defaults (e.g. the logs route defaulted to the
                    # 2048 tier), which resolves the WRONG CloudWatch log group for a
                    # VM that actually runs on another tier — logs then never appear.
                    # The DB row is authoritative for size/arch, so seed from it.
                    # Resolve the notebook's DISPLAY NAME from its id (vm_sessions has
                    # no name column). Do NOT fall back to session_id — that leaked a
                    # raw UUID into the "Notebook" label in the MicroVMs panel. Empty
                    # string when unresolved: the UI then shows the attached tab's name
                    # (and notebook_id), never a UUID. One cheap lookup per reconnected
                    # VM at startup.
                    _nb_name = ""
                    _nb_id = session.get("notebook_id")
                    if _nb_id is not None:
                        try:
                            _nb = storage.notebook_get(_nb_id)
                            _nb_name = (_nb or {}).get("name") or ""
                        except Exception:
                            _nb_name = ""
                    self.active_microvms[microvm_id] = {
                        "endpoint": endpoint,
                        "session_id": session_id,
                        "notebook_id": _nb_id,
                        "name": _nb_name,
                        "memory_mib": session.get("memory_mib") or DEFAULT_MEMORY_MIB,
                        "architecture": session.get("architecture") or DEFAULT_IMAGE_ARCH,
                        "idle_timeout_sec": session.get("idle_timeout_sec") or int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")),
                        "max_duration_sec": session.get("max_duration_sec"),
                        "launched_at": time.time(),
                        "_502_strikes": 0,
                    }
                    logger.info(f"   Restored session {session_id} → {microvm_id} "
                                f"(state={vm_state}, {session.get('memory_mib')}MiB "
                                f"{session.get('architecture')})")
        except Exception as e:
            logger.warning(f"Failed to restore sessions from DB: {e}")

    async def reconcile_session_registry(self):
        """Drop session_registry rows whose VM is no longer live.

        restore_session_registry_from_db() re-links sessions from the vm_sessions
        table, but the SEPARATE session_registry table (the per-request routing map
        read by /proxy/{path}) can carry orphan rows that outlived their VM — e.g.
        a VM terminated/suspended-out while the proxy was down, or a teardown that
        wiped VMs without clearing this table. On the next proxy boot those rows
        resolve to dead Lambda URLs, so every client poll (/helpers,
        /completions-catalog, /variables) 502s forever.

        Here we enumerate every registry row and ask the control-plane for each
        VM's state; rows for TERMINATED/TERMINATING/FAILED/NOT_FOUND VMs are
        deleted. Rows for live or indeterminate (UNKNOWN) VMs are kept — we only
        remove a mapping we can POSITIVELY confirm is dead, so a transient API blip
        never drops a healthy session. Best-effort and fully guarded: a failure
        here must never block proxy startup.
        """
        try:
            rows = storage.session_registry_all()
        except Exception as e:
            logger.warning(f"Session-registry reconcile: cannot list rows: {e}")
            return

        if not rows:
            return

        dead_states = ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND")
        removed = 0
        for row in rows:
            session_id = row.get("session_id")
            vm_id = row.get("vm_id")
            if not session_id or not vm_id:
                continue
            state = await self.get_vm_state(vm_id)
            if state in dead_states:
                try:
                    storage.session_registry_delete(session_id)
                    # Keep the L1 cache consistent with the DB we just pruned.
                    self._session_registry.pop(session_id, None)
                    removed += 1
                    logger.info(f"   Reconcile: dropped stale mapping {session_id} → "
                                f"{vm_id} (state={state})")
                except Exception as e:
                    logger.warning(f"   Reconcile: failed to drop {session_id}: {e}")

        if removed:
            logger.info(f"Session-registry reconcile: removed {removed} stale "
                        f"mapping(s) of {len(rows)} checked.")
        else:
            logger.info(f"Session-registry reconcile: all {len(rows)} mapping(s) live.")
