"""
PoolManager — fill loop, recycle loop, /grant orchestration, orphan recovery
(design §4, §5, §13). Producer/consumer with the pool_vms table as the
bounded buffer: the fill+recycle loops are the producer, /grant is the
consumer, and the DB table itself IS the queue (§13.1) — there is no
in-memory queue here.
"""

import asyncio
import logging
import time
import uuid

from pool_manager.config import (
    RECONCILE_GRANT_TIMEOUT_SECONDS,
    RESUME_TIMEOUT_SECONDS,
    PoolConfig,
)
from pool_manager.vm_backend import RUNNING, BackendError, VmBackend, VmHandle

logger = logging.getLogger(__name__)

# Clock seam for the grant timing line (tests patch it with a fake clock).
_clock = time.perf_counter

# Grant-timeout for stale 'granting' reconciliation: a granting row older than
# this is assumed to be a crashed grant and is torn down rather than silently
# returned to 'available'. Derived from the grant budget (resume + rebind +
# restore, pool_manager/config.py) plus a margin, so a slow restore grant that
# is still inside its budget is never reaped mid-grant.
DEFAULT_GRANT_TIMEOUT_SECONDS = RECONCILE_GRANT_TIMEOUT_SECONDS

# Fixed storage session-lock key under which the mutating maintenance sweeps
# (fill/recycle/reconcile) are serialized so that multiple pool-manager
# instances sharing one pool_vms ledger never run those sweeps concurrently
# (multi-instance safety). Postgres backs this with pg_try_advisory_lock
# (cross-process); SQLite with an in-process set (correct for single-process
# local dev). The lock is NON-REENTRANT.
POOL_MAINTENANCE_LOCK_KEY = "__pool_maintenance__"

# fill_loop back-off when demand is pending but the maintenance lock is held
# by another sweep/instance: retry at this cadence instead of waiting out the
# full fill interval (keeps demand-aware backfill prompt, design §13.5).
FILL_LOCK_BUSY_RETRY_SECONDS = 0.5


class PoolEmpty(Exception):
    """Raised by grant() when no spare of the requested tier is available.
    The server maps this to 409 POOL_EMPTY."""


class GrantError(Exception):
    """Raised by grant() when resume/rebind/restore fails after a spare was
    claimed. The server maps this to a 5xx."""


# Values shorter than this are not masked: replacing "1" or "on" everywhere would
# garble the message and such values carry no secret material.
_REDACT_MIN_LEN = 4
REDACTED = "***"


def _sensitive_values(req) -> list[str]:
    """String forms of every env_vars value and every secrets entry value in a
    grant body (the user's plaintext env values + Secrets Manager refs)."""
    out: list[str] = []
    if not isinstance(req, dict):
        return out
    env = req.get("env_vars")
    if isinstance(env, dict):
        out.extend(str(v) for v in env.values() if v is not None)
    secrets = req.get("secrets")
    if isinstance(secrets, list):
        for s in secrets:
            if isinstance(s, dict):
                # envVar is the target KEY name (fine to log); the rest is the ref.
                out.extend(str(v) for k, v in s.items() if v is not None and k != "envVar")
            elif s is not None:
                out.append(str(s))
    return [v for v in out if len(v) >= _REDACT_MIN_LEN]


def redact_grant_text(text, req) -> str:
    """`text` (an exception message, a response body) with every env/secret value
    of the grant body `req` replaced by "***", so a backend error that echoes the
    payload never reaches a log line or the /grant response. Longest values first
    so a value containing another is masked whole."""
    text = str(text)
    for v in sorted(set(_sensitive_values(req)), key=len, reverse=True):
        text = text.replace(v, REDACTED)
    return text


def grant_log_context(req) -> str:
    """The non-sensitive identifiers of a grant body, for log lines: session_id,
    arch, size and notebook_id. Never env_vars / secrets / user_env_keys /
    catalog_entries."""
    if not isinstance(req, dict):
        return "req=<not a dict>"
    return " ".join(f"{k}={req.get(k)!r}" for k in ("session_id", "arch", "size", "notebook_id")
                    if req.get(k) is not None)


def _row_to_handle(row: dict) -> VmHandle:
    return VmHandle(
        vm_id=row["vm_id"], endpoint=row["endpoint"], arch=row["architecture"],
        size_mib=row["size_mib"], launched_at=row["launched_at"],
    )


class PoolManager:
    """Owns ONLY a VmBackend + a StorageBackend + a PoolConfig (design §3.1).
    No boto3, no proxy import beyond proxy.storage."""

    def __init__(self, backend: VmBackend, storage, config: PoolConfig,
                 grant_timeout_seconds: float = DEFAULT_GRANT_TIMEOUT_SECONDS):
        self.backend = backend
        self.storage = storage
        self.config = config
        self.grant_timeout_seconds = grant_timeout_seconds

        # Event grant() sets to signal the fill loop for demand-aware backfill
        # (design §13.5). Not a queue of work — just a wakeup signal.
        self._refill_signal = asyncio.Event()

        self._fill_task: asyncio.Task | None = None
        self._recycle_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None

        self.fill_interval_seconds = 30.0
        self.recycle_interval_seconds = 60.0
        self.reconcile_interval_seconds = 60.0

    # ------------------------------------------------------------------
    # demand-aware backfill trigger (design §13.5)
    # ------------------------------------------------------------------

    def _ensure_tier_at_target(self) -> None:
        """Single trigger point for demand-aware backfill: wakes fill_loop()
        for an immediate top-up instead of waiting the full periodic
        interval. Called whenever a spare LEAVES the pool outside of the
        periodic sweep — on a successful grant, on a failed grant's cleanup,
        and after recycling an aged-out spare — so the pool converges back
        to target promptly on every exit path, not just grant."""
        self._refill_signal.set()

    # ------------------------------------------------------------------
    # multi-instance-safe maintenance (advisory-locked sweeps)
    # ------------------------------------------------------------------

    async def _run_locked_maintenance(self, name: str, coro_factory) -> bool:
        """Run one mutating maintenance sweep under the shared storage session
        lock so multiple pool-manager instances sharing one pool_vms ledger
        never run fill/recycle/reconcile concurrently. If the lock is already
        held (by another instance or an in-flight sweep), skip this round and
        return False; otherwise acquire -> run coro_factory() -> finally
        release, returning True. coro_factory is a zero-arg callable returning
        a coroutine (callers pass self.fill_once / self.recycle_once /
        self.reconcile_once). The lock is NON-REENTRANT, so a wrapped sweep
        must never be nested inside another wrapped sweep."""
        # The acquire runs in a worker thread. If this task is cancelled while
        # that thread is still running, the thread may go on to ACQUIRE the
        # lock after we've stopped waiting — so shield the acquire and, on
        # cancellation, release the lock as soon as the abandoned acquire
        # completes successfully. Otherwise a cancelled sweep could leak the
        # non-reentrant lock and wedge every future sweep.
        acquire = asyncio.ensure_future(
            self._db("try_session_lock", POOL_MAINTENANCE_LOCK_KEY))
        try:
            acquired = await asyncio.shield(acquire)
        except asyncio.CancelledError:
            acquire.add_done_callback(self._release_abandoned_lock)
            raise
        if not acquired:
            logger.debug(f"{name}: maintenance lock held elsewhere — skipping this round")
            return False
        try:
            await coro_factory()
            return True
        finally:
            try:
                await self._db("release_session_lock", POOL_MAINTENANCE_LOCK_KEY)
            except asyncio.CancelledError:
                # The release thread still runs to completion (to_thread work is
                # not interruptible), so the lock IS released; only our wait for
                # it was cancelled.
                logger.warning(f"{name}: cancelled while releasing the maintenance lock "
                               f"(release continues in its worker thread)")
                raise

    def _release_abandoned_lock(self, fut: asyncio.Future) -> None:
        """Done-callback for an acquire whose waiter was cancelled: if the
        acquire succeeded, release the lock off the loop (fire-and-forget)."""
        if fut.cancelled() or fut.exception() is not None or not fut.result():
            return
        try:
            asyncio.get_running_loop().run_in_executor(
                None, self.storage.release_session_lock, POOL_MAINTENANCE_LOCK_KEY)
        except RuntimeError:
            # Loop/executor already shutting down — release inline rather than leak.
            self.storage.release_session_lock(POOL_MAINTENANCE_LOCK_KEY)

    async def _db(self, method: str, *args, **kwargs):
        """Run one blocking storage call off the event loop. `self.storage` is
        resolved at call time so tests that swap the storage object still work."""
        return await asyncio.to_thread(getattr(self.storage, method), *args, **kwargs)

    # ------------------------------------------------------------------
    # tiers
    # ------------------------------------------------------------------

    def _tiers(self):
        """Yield every (arch, size) tier this pool manages, per config."""
        for arch in self.config.arches:
            for size in self.config.sizes_by_arch.get(arch, []):
                yield arch, size

    # ------------------------------------------------------------------
    # producer: fill
    # ------------------------------------------------------------------

    async def _launch_one(self, arch: str, size: int) -> None:
        """Launch, health-check, and suspend exactly one spare. On ANY failure,
        clean up (terminate the backend VM if created + delete the DB row) and
        return without raising — a bad spare must never wedge the fill loop."""
        fake_session_id = f"pool-{uuid.uuid4()}"
        vm = None
        row_created = False
        try:
            vm = await self.backend.launch(arch, size, fake_session_id)
            await self._db("pool_vm_create",
                vm.vm_id, arch, size, vm.endpoint, fake_session_id,
                launched_at=vm.launched_at, state="launching",
            )
            row_created = True
            # Give a cold boot a realistic budget (config, default 90s). Too tight
            # a timeout discarded a spare that was about to become healthy, leaving
            # the tier short until the next fill tick — the "pool sits at N-1 for a
            # while" symptom. See PoolConfig.mint_health_timeout_seconds.
            await self.backend.wait_healthy(
                vm, timeout=self.config.mint_health_timeout_seconds)
            await self.backend.suspend(vm)
            await self._db("pool_vm_set_state", vm.vm_id, "available")
        except Exception as e:
            logger.warning(f"fill: failed to mint a spare ({arch}, {size}mib): {e}")
            if vm is not None:
                try:
                    await self.backend.terminate(vm)
                except Exception as cleanup_err:
                    logger.warning(f"fill: cleanup terminate failed for {vm.vm_id}: {cleanup_err}")
                if row_created:
                    try:
                        await self._db("pool_vm_delete", vm.vm_id)
                    except Exception as cleanup_err:
                        logger.warning(f"fill: cleanup pool_vm_delete failed for {vm.vm_id}: {cleanup_err}")

    async def fill_once(self) -> None:
        """For each configured (arch,size) tier, launch enough spares to reach
        its target, bounded by config.fill_concurrency. One bad tier must not
        block others (each tier's launches run independently)."""
        semaphore = asyncio.Semaphore(max(1, self.config.fill_concurrency))

        async def _bounded_launch(arch, size):
            async with semaphore:
                await self._launch_one(arch, size)

        tasks = []
        for arch, size in self._tiers():
            target = self.config.target_for(arch, size)
            existing = await self._db("pool_vm_count_by_size",
                arch, size, states=("available", "launching"))
            deficit = max(0, target - existing)
            for _ in range(deficit):
                tasks.append(asyncio.create_task(_bounded_launch(arch, size)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def fill_loop(self) -> None:
        """Periodic fill_once() + demand-aware backfill: a grant() signals
        _refill_signal for an immediate top-up instead of waiting the full
        interval (design §13.5).

        The refill signal is only consumed by a round that actually holds the
        maintenance lock. If the lock is busy (a recycle/reconcile sweep, or
        another instance) while demand is pending, back off briefly and retry
        rather than dropping the wakeup until the next periodic interval —
        with storage calls off the loop, a round can count the deficit while
        grant claims are still in flight, so the follow-up signal matters.

        The short retry is ONLY for a genuinely busy lock. If the round RAISED
        (e.g. try_session_lock failing during a DB outage) we back off for the
        normal fill interval, as before, instead of spinning — but the pending
        demand is carried over and re-armed for the next round."""
        demand_carried = False
        while True:
            try:
                await asyncio.wait_for(self._refill_signal.wait(),
                                       timeout=self.fill_interval_seconds)
            except asyncio.TimeoutError:
                pass
            if demand_carried:
                # Re-arm demand held over from a failed round, so a busy lock on
                # this round still gets the short retry.
                self._refill_signal.set()
                demand_carried = False
            demand_pending = self._refill_signal.is_set()
            try:
                ran = await self._run_locked_maintenance(
                    "fill_loop", self._fill_once_consuming_signal)
            except Exception as e:
                logger.warning(f"fill_loop: fill_once failed: {e}")
                # Clear the signal so the next wait blocks for the normal
                # interval (a NEW grant can still wake it early), and remember
                # the demand so it isn't lost.
                demand_carried = demand_pending or self._refill_signal.is_set()
                self._refill_signal.clear()
                continue
            if not ran and self._refill_signal.is_set():
                # Lock genuinely busy (another sweep/instance holds it).
                await asyncio.sleep(FILL_LOCK_BUSY_RETRY_SECONDS)

    async def _fill_once_consuming_signal(self) -> None:
        """fill_once() for fill_loop: clears the refill signal only once the
        maintenance lock is held, so a skipped round never swallows demand."""
        self._refill_signal.clear()
        await self.fill_once()

    # ------------------------------------------------------------------
    # producer: recycle
    # ------------------------------------------------------------------

    async def recycle_once(self) -> None:
        """Terminate + delete every 'available' spare older than
        max_age_seconds, then backfill the deficit. AFTER that, trim any tier
        that sits ABOVE its target by terminating+deleting the OLDEST excess
        'available' spares (over-target trim). ONLY 'available' spares are
        touched by either step — 'granted' and 'granting' are left alone.

        Ordering is age-recycle FIRST (with its backfill), THEN the over-target
        trim re-listed against the post-recycle/post-backfill set, so a tier is
        never both backfilled and trimmed on a stale snapshot and the trim
        itself never triggers a fill."""
        aged = await self._db("pool_vm_list_older_than",
            self.config.max_age_seconds, state="available")
        for row in aged:
            vm = _row_to_handle(row)
            try:
                await self.backend.terminate(vm)
            except Exception as e:
                logger.warning(f"recycle: terminate failed for {vm.vm_id}: {e}")
            try:
                await self._db("pool_vm_delete", vm.vm_id)
            except Exception as e:
                logger.warning(f"recycle: pool_vm_delete failed for {vm.vm_id}: {e}")
        if aged:
            # Recycled spares left the pool too — backfill immediately rather
            # than waiting for the next periodic fill sweep (design §13.5
            # applies to every exit path, not just grant).
            self._ensure_tier_at_target()
            await self.fill_once()

        # Over-target trim: for each tier, drop any 'available' spares beyond
        # its target, oldest first. Re-list per tier (post-recycle/post-backfill
        # set); never widen the state filter past 'available', so granted/
        # granting are excluded at the query level. Do NOT fill after trimming.
        for arch, size in self._tiers():
            target = self.config.target_for(arch, size)
            available = await self._db("pool_vm_list",
                state="available", architecture=arch, size_mib=size)
            # pool_vm_list returns rows UNORDERED; sort oldest-first by the
            # ISO-8601 launched_at string (lexicographic == chronological).
            available.sort(key=lambda r: r["launched_at"])
            if len(available) > target:
                excess = len(available) - target
                for row in available[:excess]:
                    vm = _row_to_handle(row)
                    try:
                        await self.backend.terminate(vm)
                    except Exception as e:
                        logger.warning(f"recycle: trim terminate failed for {vm.vm_id}: {e}")
                    try:
                        await self._db("pool_vm_delete", vm.vm_id)
                    except Exception as e:
                        logger.warning(f"recycle: trim pool_vm_delete failed for {vm.vm_id}: {e}")
                    logger.info(f"recycle: trimmed over-target spare {vm.vm_id} "
                                f"tier=({arch},{size}) available={len(available)} target={target}")

    async def recycle_loop(self) -> None:
        while True:
            await asyncio.sleep(self.recycle_interval_seconds)
            try:
                await self._run_locked_maintenance("recycle_loop", self.recycle_once)
            except Exception as e:
                logger.warning(f"recycle_loop: recycle_once failed: {e}")

    # ------------------------------------------------------------------
    # consumer: grant
    # ------------------------------------------------------------------

    async def grant(self, req: dict) -> dict:
        """Claim the youngest available spare of (arch,size), rebind it to the
        real session, optionally restore from a prior checkpoint, and hand it
        to the proxy. Never leaves a half-bound VM or a dangling 'granting' row."""
        arch = req["arch"]
        size = req["size"]
        session_id = req["session_id"]

        claimed = await self._db("pool_vm_claim_youngest", arch, size, session_id)
        if claimed is None:
            raise PoolEmpty(f"no available spare for ({arch}, {size}mib)")

        vm = _row_to_handle(claimed)
        try:
            t_start = _clock()
            # resume() returns the final control-plane state it observed, so no
            # separate get_state round-trip is needed.
            state = await self.backend.resume(vm, timeout=RESUME_TIMEOUT_SECONDS)
            t_resumed = _clock()
            if state != RUNNING:
                raise BackendError(
                    f"resume for {vm.vm_id} returned non-RUNNING state: {state}")
            resume_s = t_resumed - t_start
            if resume_s > 3.0:
                logger.warning(f"grant {vm.vm_id}: resume took {resume_s:.1f}s (>3 s)")

            identity_payload = {
                "session_id": session_id,
                "notebook_id": req.get("notebook_id"),
                "notebook_name": req.get("notebook_name"),
                "persistence_mode": req.get("persistence_mode"),
                "checkpoint_enabled": req.get("checkpoint_enabled", False),
                "artifacts_bucket": req.get("artifacts_bucket"),
                "env_vars": req.get("env_vars", {}),
                "secrets": req.get("secrets", []),
                "user_env_keys": req.get("user_env_keys", {}),
                "memory_mib": size,
                "architecture": arch,
            }
            if req.get("catalog_entries") is not None:
                identity_payload["catalog_entries"] = req["catalog_entries"]
            if req.get("catalog_s3_key") is not None:
                identity_payload["catalog_s3_key"] = req["catalog_s3_key"]

            t_rebind = _clock()
            await self.backend.rebind(vm, identity_payload)
            t_rebound = _clock()

            restore_from = req.get("restore_from")
            restore_ms = "-"
            if restore_from:
                await self.backend.restore_state(vm, restore_from)
                restore_ms = f"{(_clock() - t_rebound) * 1000:.0f}"

            await self._db("pool_vm_set_state", vm.vm_id, "granted", real_session_id=session_id)
            self._ensure_tier_at_target()
            # IDs and timings only: never log the request (it carries env/secrets).
            logger.info(
                f"grant {vm.vm_id} ({arch},{size}mib): resume={resume_s * 1000:.0f}ms "
                f"rebind={(t_rebound - t_rebind) * 1000:.0f}ms restore_state={restore_ms}ms "
                f"total={(_clock() - t_start) * 1000:.0f}ms state={state}")
            # launched_at is the spare's true MINT time (when the pool ran
            # run_microvm), NOT this adopt moment. The AWS 8h max-lifetime clock
            # starts at mint, so the proxy must record this value — not now() — or
            # the side panel's 'Terminates in' countdown over-reports remaining
            # lifetime by however long the spare sat warm in the pool.
            return {"vm_id": vm.vm_id, "endpoint": vm.endpoint, "launched_at": vm.launched_at}
        except Exception as e:
            # The backend error may echo the identity payload (e.g. a /rebind
            # response body): mask the env/secret values before logging/raising.
            err = redact_grant_text(e, req)
            logger.warning(f"grant: failed for claimed spare {vm.vm_id} ({arch},{size}mib): {err}")
            try:
                await self.backend.terminate(vm)
            except Exception as cleanup_err:
                logger.warning(f"grant: cleanup terminate failed for {vm.vm_id}: {cleanup_err}")
            try:
                await self._db("pool_vm_delete", vm.vm_id)
            except Exception as cleanup_err:
                logger.warning(f"grant: cleanup pool_vm_delete failed for {vm.vm_id}: {cleanup_err}")
            # The claimed spare also left the pool (terminated) — backfill its
            # tier immediately rather than leaving it below target until the
            # next periodic sweep.
            self._ensure_tier_at_target()
            raise GrantError(f"grant failed for ({arch}, {size}mib): {err}") from e

    # ------------------------------------------------------------------
    # orphan recovery
    # ------------------------------------------------------------------

    async def reconcile_once(self) -> None:
        """Reconcile the pool_vms ledger against the backend's actual VMs.

        reconcile is the first-line janitor and OWNS stale-row cleanup. It runs
        at startup — so a starting instance ADOPTS, rather than wipes, a shared
        pool — and periodically.

        The pool_vms table is the AUTHORITATIVE, COMPLETE record of what the
        pool owns. The pool manager does NOT own the whole account: the proxy
        also runs its own notebook VMs (cold-launch fallback on a pool miss,
        non-pooled launches), which are REAL in-use VMs with NO pool_vms row.
        The lambda-microvms API exposes no tags and list_microvms returns no
        marker, so a backend VM cannot be identified as pool-vs-notebook from
        the control plane — the ONLY authority on pool ownership is this table.

        Therefore reconcile only acts on VMs the pool has a record of:
          - a pool_vms row whose backend VM is gone → delete the stale row.
          - a stale 'granting' row past the grant timeout → tear it down.
        We DELIBERATELY do NOT terminate "backend VMs with no pool_vms row":
        such a VM is NOT a pool orphan — it is almost always a live proxy
        notebook VM, and terminating it kills a user's running session (the bug
        that produced the reconnect/restore popup). A launch that fails
        mid-flight is cleaned up by _launch_one itself."""
        db_rows = {row["vm_id"]: row for row in await self._db("pool_vm_list")}
        backend_vms = {vm.vm_id: vm for vm in await self.backend.list_all()}

        for vm_id, row in db_rows.items():
            if vm_id not in backend_vms:
                logger.info(f"reconcile: pool_vms row {vm_id} has no backend VM — deleting stale row")
                await self._db("pool_vm_delete", vm_id)

        # Stale-'granting' detection measures age from updated_at (when the row
        # ENTERED 'granting' via claim_youngest), NOT launched_at (mint time).
        # A warm spare may sit in the pool for minutes before being granted, so
        # keying off launched_at would flag it as "stale granting" the instant
        # it is claimed and tear it down mid-grant — the bug that killed a VM
        # 6s into a slow grant and produced the reconnect/restore popup.
        stale_granting = await self._db("pool_vm_list_older_than",
            self.grant_timeout_seconds, state="granting", since_column="updated_at")
        for row in stale_granting:
            vm_id = row["vm_id"]
            logger.info(f"reconcile: stale granting row {vm_id} past grant timeout — tearing down")
            if vm_id in backend_vms:
                try:
                    await self.backend.terminate(backend_vms[vm_id])
                except Exception as e:
                    logger.warning(f"reconcile: terminate of stale granting VM {vm_id} failed: {e}")
            await self._db("pool_vm_delete", vm_id)

        # Orphaned-'granted' reaper. A pool_vms row stuck in 'granted' after its
        # session rotated onto a different VM (or ended) is otherwise cleaned up
        # only by T10 "backend VM gone" or a manual /admin/drain, so the AWS VM
        # bills SUSPENDED until the 8h max-lifetime and /status counts it above
        # target. Reap such rows here — but ONLY when the owning session is no
        # longer on this VM. Measured from updated_at (the moment the row entered
        # 'granted' via grant()), NOT launched_at, and gated by the same grant
        # grace window as the stale-'granting' check so a row still being adopted
        # by the proxy (registry not yet written) is never reaped mid-adoption.
        # CRITICAL SAFETY INVARIANT: a row whose session's CURRENT VM id still
        # equals this row's vm_id is LEFT ALONE regardless of age — never reclaim
        # a VM a session is actively using.
        orphaned_granted = await self._db("pool_vm_list_older_than",
            self.grant_timeout_seconds, state="granted", since_column="updated_at")
        for row in orphaned_granted:
            vm_id = row["vm_id"]
            session_id = row.get("real_session_id")
            if session_id is None:
                # Defensive: a 'granted' row with no owning session — never throw.
                continue
            current_vm_id = await asyncio.to_thread(self._resolve_current_vm_id, session_id)
            if current_vm_id == vm_id:
                # Session is still actively on this VM — the safety invariant.
                continue
            reason = ("rotated-away" if current_vm_id is not None
                      else "session-gone")
            logger.info(f"reconcile: orphaned granted row {vm_id} (session {session_id} "
                        f"{reason}, current_vm={current_vm_id}) — reclaiming")
            if vm_id in backend_vms:
                try:
                    await self.backend.terminate(backend_vms[vm_id])
                except Exception as e:
                    logger.warning(f"reconcile: terminate of orphaned granted VM {vm_id} failed: {e}")
            await self._db("pool_vm_delete", vm_id)

    def _resolve_current_vm_id(self, session_id: str):
        """Return the session's CURRENT backing vm_id, or None if the session is
        gone. Prefer the authoritative session_registry mapping; fall back to the
        newest NON-TERMINATED vm_sessions row (the list is oldest-first, so the
        last non-terminated row is newest). All reads go through the storage
        interface — no raw SQL."""
        reg = self.storage.session_registry_get(session_id)
        if reg is not None and reg.get("vm_id") is not None:
            return reg["vm_id"]
        current = None
        for vm_row in self.storage.vm_session_list_for_session(session_id):
            if vm_row.get("state") != "TERMINATED":
                current = vm_row.get("microvm_id")
        return current

    async def reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval_seconds)
            try:
                await self._run_locked_maintenance("reconcile_loop", self.reconcile_once)
            except Exception as e:
                logger.warning(f"reconcile_loop: reconcile_once failed: {e}")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run startup reconciliation + an initial fill (both advisory-locked),
        then start the three background loops (design §5.1, §12 Build Phase 3).

        A starting instance ADOPTS the shared pool rather than wiping it
        (multi-instance safe): because pool_vms is shared storage that may be
        owned by other live instances, startup does NOT clear inherited rows.
        Instead the locked startup reconcile_once() is the first-line janitor —
        it drops only rows whose backing VM is truly gone and tears down stale
        'granting' rows — and the locked fill_once() tops the shared pool up to
        target. Both run under POOL_MAINTENANCE_LOCK_KEY so a concurrent
        instance's sweep is skipped rather than racing."""
        await self._run_locked_maintenance("start", self.reconcile_once)
        await self._run_locked_maintenance("start", self.fill_once)
        self._fill_task = asyncio.create_task(self.fill_loop())
        self._recycle_task = asyncio.create_task(self.recycle_loop())
        self._reconcile_task = asyncio.create_task(self.reconcile_loop())

    async def stop(self) -> None:
        """Cancel and await the three background loop tasks."""
        for task in (self._fill_task, self._recycle_task, self._reconcile_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._fill_task = self._recycle_task = self._reconcile_task = None

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Per-(arch,size) counts by state + target vs actual, for GET /status."""
        tiers = {}
        for arch, size in self._tiers():
            key = f"{arch}:{size}"
            rows = self.storage.pool_vm_list(architecture=arch, size_mib=size)
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            tiers[key] = {
                "arch": arch,
                "size_mib": size,
                "target": self.config.target_for(arch, size),
                "actual": len(rows),
                "counts": counts,
            }
        return {"enabled": self.config.enabled, "backend": self.config.backend, "tiers": tiers}

    async def status_async(self) -> dict:
        """status() off the event loop, for the async routes. status() itself
        stays sync (callers/tests use it directly)."""
        return await asyncio.to_thread(self.status)
