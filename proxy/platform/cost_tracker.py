"""
MicroVM Cost Tracker

Part of: proxy.platform (Smart MicroVM Service layer)

Tracks state transitions per MicroVM and computes estimated cost
based on time spent in each state (RUNNING vs SUSPENDED).

The in-memory records survive browser refreshes (they live in the proxy
process). Durable cost survives proxy restarts too: state transitions are
written to vm_state_log and burst totals to vm_sessions, the v_vm_sessions view
reconstructs running/suspended time from them, and load_from_db() rebuilds the
in-memory records at startup.

Pricing is based on published Lambda MicroVM rates, per CPU arch (arm64 /
x86_64), read from MICROVM_PRICING_JSON (scripts/config.sh). Each VM is priced
by its own arch. AWS bills on two axes: vCPU (per vCPU-second) + Memory (per
GB-second). CPU is allocated at 2 GB : 1 vCPU ratio.
"""

import os
import json
import logging
import time
from dataclasses import dataclass, field

from proxy.platform.defaults import DEFAULT_MEMORY_MIB

logger = logging.getLogger(__name__)

# ── Per-arch pricing table (SINGLE source of truth) ──────────────────────────
# A single proxy prices VMs of every arch it serves, so pricing is a per-arch
# table read from MICROVM_PRICING_JSON (set in config.sh). Each VM is priced by
# ITS architecture. Kept in Python (NOT in the SQL view) so changing a rate never
# requires a schema migration and never re-prices historical rows.
#
# Shape: {"arm64": {"vcpu_per_sec":.., "memory_per_gb_sec":.., "snapshot_per_gb_month":..,
#                   "snapshot_read_per_gb":.., "snapshot_write_per_gb":..}, "x86_64": {...}}
_DEFAULT_ARCH = os.environ.get("DEFAULT_IMAGE_ARCH", "arm64")
# Last-resort fallback (arm64/Graviton) if the env JSON is missing or malformed —
# so the proxy never crashes on bad config; a warning is logged instead.
_FALLBACK_RATES = {"vcpu_per_sec": 0.0000276944, "memory_per_gb_sec": 0.0000036667,
                   "snapshot_per_gb_month": 0.08,
                   "snapshot_read_per_gb": 0.00155, "snapshot_write_per_gb": 0.00380}


def _load_pricing() -> dict:
    raw = os.environ.get("MICROVM_PRICING_JSON", "")
    if raw:
        try:
            table = json.loads(raw)
            if isinstance(table, dict) and table:
                return table
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"MICROVM_PRICING_JSON invalid ({e}); using fallback arm64 rates")
    return {_DEFAULT_ARCH: dict(_FALLBACK_RATES)}


_PRICING = _load_pricing()


def _rates_for(arch: str) -> dict:
    """Rates for an arch, falling back to the default arch, then hard fallback."""
    return _PRICING.get(arch) or _PRICING.get(_DEFAULT_ARCH) or _FALLBACK_RATES


def price_quantities(running_secs: float, suspended_secs: float,
                     burst_mb_seconds: float, memory_mib: int,
                     architecture: str = None,
                     resume_count: int = 0, suspend_count: int = 0) -> dict:
    """Turn objective QUANTITIES (seconds in each state, burst MB-seconds, memory
    tier) into the full cost dict the UI consumes. This is the SINGLE pricing
    function — both the legacy in-memory MicroVMCostRecord.compute() and the
    view-backed CostTracker.get_cost() call it, so dollar math never diverges.

    Rates are selected by `architecture` (each VM priced by its own arch); when
    omitted, the default arch's rates are used.
    """
    rates = _rates_for(architecture or _DEFAULT_ARCH)
    price_vcpu_per_sec = rates["vcpu_per_sec"]
    price_memory_per_gb_sec = rates["memory_per_gb_sec"]
    price_snapshot_per_gb_sec = rates["snapshot_per_gb_month"] / (30 * 24 * 3600)
    price_snapshot_read_per_gb = rates.get("snapshot_read_per_gb", _FALLBACK_RATES["snapshot_read_per_gb"])
    price_snapshot_write_per_gb = rates.get("snapshot_write_per_gb", _FALLBACK_RATES["snapshot_write_per_gb"])

    memory_gb = (memory_mib or DEFAULT_MEMORY_MIB) / 1024.0
    vcpus = memory_gb / 2.0  # 2 GB : 1 vCPU

    running_cost = (
        vcpus * running_secs * price_vcpu_per_sec +
        memory_gb * running_secs * price_memory_per_gb_sec
    )
    suspended_cost = memory_gb * suspended_secs * price_snapshot_per_gb_sec

    burst_gb_seconds = (burst_mb_seconds or 0.0) / 1024.0
    burst_vcpu_seconds = burst_gb_seconds / 2.0  # same 2GB:1vCPU ratio for burst
    burst_cost = (
        burst_vcpu_seconds * price_vcpu_per_sec +
        burst_gb_seconds * price_memory_per_gb_sec
    )
    # Snapshot DATA charges: one READ per transition into RUNNING (cold launch +
    # every resume), one WRITE per transition into SUSPENDED; GB = the memory tier.
    snapshot_read_cost = resume_count * memory_gb * price_snapshot_read_per_gb
    snapshot_write_cost = suspend_count * memory_gb * price_snapshot_write_per_gb
    total_cost = running_cost + suspended_cost + burst_cost + snapshot_read_cost + snapshot_write_cost

    return {
        "running_secs": round(running_secs),
        "suspended_secs": round(suspended_secs),
        "running_cost_usd": round(running_cost, 6),
        "suspended_cost_usd": round(suspended_cost, 6),
        "burst_cost_usd": round(burst_cost, 6),
        "snapshot_read_cost_usd": round(snapshot_read_cost, 6),
        "snapshot_write_cost_usd": round(snapshot_write_cost, 6),
        "total_cost_usd": round(total_cost, 6),
        "memory_gb": memory_gb,
        "burst_mb_seconds": round(burst_mb_seconds or 0.0, 1),
        "burst_gb_seconds": round(burst_gb_seconds, 2),
        "burst_vcpu_seconds": round(burst_vcpu_seconds, 2),
        "resume_count": int(resume_count or 0),
        "suspend_count": int(suspend_count or 0),
        # The arch this was priced for and the effective rates used, so the UI
        # shows the VM's own (per-arch) rates instead of hard-coded arm64 ones.
        "architecture": architecture or _DEFAULT_ARCH,
        "rates": {
            "vcpu_per_sec": price_vcpu_per_sec,
            "memory_per_gb_sec": price_memory_per_gb_sec,
            "snapshot_per_gb_month": rates["snapshot_per_gb_month"],
            "snapshot_read_per_gb": price_snapshot_read_per_gb,
            "snapshot_write_per_gb": price_snapshot_write_per_gb,
        },
    }


@dataclass
class StateTransition:
    """A recorded state change for a MicroVM."""
    state: str
    timestamp: float


@dataclass
class MicroVMCostRecord:
    """Cost tracking record for a single MicroVM."""
    microvm_id: str
    memory_mib: int = DEFAULT_MEMORY_MIB
    architecture: str = None  # CPU arch (arm64/x86_64) → selects pricing rates
    transitions: list[StateTransition] = field(default_factory=list)
    # Burst tracking: accumulates MB-seconds above baseline
    burst_mb_seconds: float = 0.0
    _last_burst_poll: float = 0.0
    # Usage stats for the VM card (in-memory only; lost on a proxy restart).
    # peak_mem_mb: highest used_mb seen in a metrics sample. burst_episodes:
    # number of crossings from at/below the tier to above it.
    peak_mem_mb: float = 0.0
    burst_episodes: int = 0
    _in_burst: bool = False

    @property
    def memory_gb(self) -> float:
        return self.memory_mib / 1024.0

    @property
    def vcpus(self) -> float:
        """vCPUs allocated (2 GB : 1 vCPU ratio)."""
        return self.memory_gb / 2.0

    def record_state(self, state: str):
        """Record a state transition (only if state actually changed)."""
        if self.transitions and self.transitions[-1].state == state:
            return  # No change
        self.transitions.append(StateTransition(state=state, timestamp=time.time()))

    def record_burst_sample(self, used_mb: float):
        """
        Record a memory usage sample for burst cost tracking.
        Called each time metrics are polled from the VM.
        Accumulates (used_mb - baseline_mb) × elapsed_seconds when above baseline.
        """
        now = time.time()
        if self._last_burst_poll > 0 and used_mb > self.memory_mib:
            elapsed = now - self._last_burst_poll
            overage_mb = used_mb - self.memory_mib
            self.burst_mb_seconds += overage_mb * elapsed
        self._last_burst_poll = now
        # Peak and burst episodes (shown on the VM card's Resources tab).
        if used_mb > self.peak_mem_mb:
            self.peak_mem_mb = used_mb
        if used_mb > self.memory_mib:
            if not self._in_burst:
                self.burst_episodes += 1
                self._in_burst = True
        else:
            self._in_burst = False

    def compute(self) -> dict:
        """
        Compute estimated cost breakdown.

        Returns:
            {
                "running_secs": int,
                "suspended_secs": int,
                "running_cost_usd": float,
                "suspended_cost_usd": float,
                "burst_cost_usd": float,
                "total_cost_usd": float,
                "memory_gb": float,
                "burst_mb_seconds": float,
            }
        """
        if not self.transitions:
            return {
                "running_secs": 0,
                "suspended_secs": 0,
                "running_cost_usd": 0.0,
                "suspended_cost_usd": 0.0,
                "burst_cost_usd": 0.0,
                "snapshot_read_cost_usd": 0.0,
                "snapshot_write_cost_usd": 0.0,
                "total_cost_usd": 0.0,
                "memory_gb": self.memory_gb,
                "burst_mb_seconds": 0.0,
                "resume_count": 0,
                "suspend_count": 0,
            }

        now = time.time()
        running_secs = 0.0
        suspended_secs = 0.0
        resume_count = 0
        suspend_count = 0

        for i, t in enumerate(self.transitions):
            # Duration until next transition, or until now if last entry
            if i + 1 < len(self.transitions):
                duration = self.transitions[i + 1].timestamp - t.timestamp
            elif t.state == "TERMINATED":
                duration = 0
            else:
                duration = now - t.timestamp

            if t.state == "RUNNING":
                running_secs += duration
                resume_count += 1
            elif t.state == "SUSPENDED":
                suspended_secs += duration
                suspend_count += 1

        # Delegate dollar math to the single pricing function (shared with the
        # view-backed path) so pricing never diverges between the two. Priced by
        # this VM's architecture. Count every into-RUNNING / into-SUSPENDED
        # transition as one snapshot read / write, mirroring the view.
        return price_quantities(running_secs, suspended_secs,
                                self.burst_mb_seconds, self.memory_mib,
                                architecture=self.architecture,
                                resume_count=resume_count,
                                suspend_count=suspend_count)


class CostTracker:
    """
    Tracks cost across all MicroVMs managed by this proxy session.

    Usage:
        tracker = CostTracker()
        tracker.record("microvm-abc", "RUNNING", memory_mib=2048)
        tracker.record("microvm-abc", "SUSPENDED")
        cost = tracker.get_cost("microvm-abc")
        total = tracker.get_total_cost()
    """

    def __init__(self):
        self._records: dict[str, MicroVMCostRecord] = {}
        # Warm-pool spares whose record was anchored to pool_vms history.
        self._seeded_spares: set[str] = set()

    def record(self, microvm_id: str, state: str, memory_mib: int = None,
               architecture: str = None):
        """
        Record a state observation for a MicroVM.
        Only creates a new transition if the state actually changed.

        Args:
            microvm_id: The MicroVM identifier
            state: Current state (RUNNING, SUSPENDED, TERMINATED, etc.)
            memory_mib: Memory in MiB (only needed on first call per MicroVM)
            architecture: CPU arch (arm64/x86_64) — selects pricing rates
        """
        if microvm_id not in self._records:
            self._records[microvm_id] = MicroVMCostRecord(
                microvm_id=microvm_id,
                memory_mib=memory_mib or DEFAULT_MEMORY_MIB,
                architecture=architecture,
            )
        record = self._records[microvm_id]

        # Update memory if provided and different
        if memory_mib and memory_mib != record.memory_mib:
            record.memory_mib = memory_mib
        # Backfill arch if it wasn't known at first record
        if architecture and record.architecture != architecture:
            record.architecture = architecture

        record.record_state(state)

    def seed_pool_spare(self, microvm_id: str, launched_epoch: float,
                        suspended_epoch: float | None, memory_mib: int = None,
                        architecture: str = None) -> bool:
        """Anchor an unattached warm-pool SPARE's in-memory record to its real
        history from pool_vms: RUNNING from mint (launched_at) until it became
        'available' (suspended), then SUSPENDED until now.

        Spares have no vm_sessions row, so this in-memory record is their ONLY cost
        source. Without seeding, record() starts the clock at the proxy's first
        /instances observation — so after a proxy restart (or a fresh UI load) every
        spare showed ~0s running / a few seconds suspended despite sitting suspended
        for many minutes. Seeds once per VM (idempotent); REPLACES any transitions a
        prior poll recorded for it (those were anchored to observation time, not
        real history). Returns True if it seeded. Bad/missing times → no-op (False)."""
        if microvm_id in self._seeded_spares:
            return False
        now = time.time()
        if not launched_epoch or launched_epoch >= now:
            return False
        if suspended_epoch is not None and not (launched_epoch <= suspended_epoch <= now):
            suspended_epoch = None
        record = self._records.get(microvm_id)
        if record is None:
            record = MicroVMCostRecord(microvm_id=microvm_id, memory_mib=memory_mib or DEFAULT_MEMORY_MIB,
                                       architecture=architecture)
            self._records[microvm_id] = record
        if memory_mib and memory_mib != record.memory_mib:
            record.memory_mib = memory_mib
        if architecture and record.architecture != architecture:
            record.architecture = architecture
        record.transitions = [StateTransition(state="RUNNING", timestamp=launched_epoch)]
        if suspended_epoch is not None:
            record.transitions.append(StateTransition(state="SUSPENDED", timestamp=suspended_epoch))
        self._seeded_spares.add(microvm_id)
        return True

    def record_prior_suspended(self, microvm_id: str, suspended_since_epoch: float,
                               memory_mib: int = None, architecture: str = None):
        """Seed a freshly-adopted POOLED VM's record with the pool-residency
        SUSPENDED interval it incurred before the grant.

        A spare sat SUSPENDED (snapshot storage) from its MINT time
        (suspended_since_epoch) until now (the grant). Called INSTEAD OF
        record(..., 'RUNNING') on the pool-grant path so the in-memory fallback in
        get_cost() reports a non-zero suspended_secs ≈ (now - mint) immediately,
        matching what v_vm_sessions reconstructs from the two seeded state-log rows.

        Seeds TWO transitions — SUSPENDED at the mint epoch, then RUNNING at now —
        which compute() sums as a suspended interval followed by a running interval.
        (record_state appends on state CHANGE; SUSPENDED then RUNNING are distinct,
        so both land.) Guards a bad mint time: if suspended_since_epoch is missing
        or NOT strictly before now, falls back to a plain RUNNING record (cold-launch
        behavior — no spurious suspended interval)."""
        if microvm_id not in self._records:
            self._records[microvm_id] = MicroVMCostRecord(
                microvm_id=microvm_id,
                memory_mib=memory_mib or DEFAULT_MEMORY_MIB,
                architecture=architecture,
            )
        record = self._records[microvm_id]
        if memory_mib and memory_mib != record.memory_mib:
            record.memory_mib = memory_mib
        if architecture and record.architecture != architecture:
            record.architecture = architecture

        # The spare already has real history (seeded from pool_vms by /instances,
        # or recorded by an earlier poll): just transition it to RUNNING now.
        # Appending SUSPENDED@mint here would land OUT OF ORDER after those
        # transitions and corrupt compute()'s interval sums.
        if record.transitions:
            record.record_state("RUNNING")
            self._seeded_spares.discard(microvm_id)
            return

        now = time.time()
        if not suspended_since_epoch or suspended_since_epoch >= now:
            # Bad/missing mint time → degrade to the single-RUNNING cold-launch shape.
            record.record_state("RUNNING")
            return
        # Seed the pool-residency SUSPENDED interval, then the RUNNING interval.
        record.transitions.append(StateTransition(state="SUSPENDED", timestamp=suspended_since_epoch))
        record.transitions.append(StateTransition(state="RUNNING", timestamp=now))

    def record_burst(self, microvm_id: str, used_mb: float):
        """
        Record a memory usage sample for burst cost tracking.
        Call this each time metrics are polled from the VM.

        Args:
            microvm_id: The MicroVM identifier
            used_mb: Current memory usage in MB (from psutil)
        """
        record = self._records.get(microvm_id)
        if record:
            record.record_burst_sample(used_mb)

    def usage_stats(self, microvm_id: str) -> dict | None:
        """Peak memory and burst episodes seen in this proxy's metrics samples,
        or None when there is no record or no sample yet. In-memory only."""
        record = self._records.get(microvm_id)
        if record is None or record._last_burst_poll == 0:
            return None
        return {
            "peak_mem_mb": round(record.peak_mem_mb, 1),
            "memory_mb": record.memory_mib,
            "burst_episodes": record.burst_episodes,
        }

    def last_state(self, microvm_id: str) -> str | None:
        """Last in-memory transition state for a VM, or None if untracked."""
        record = self._records.get(microvm_id)
        if record is None or not record.transitions:
            return None
        return record.transitions[-1].state

    def note_served(self, microvm_id: str) -> bool:
        """A request to this VM just succeeded, so it is RUNNING now.

        A suspended MicroVM auto-resumes on traffic (and AWS re-suspends it when
        idle), so a resume can start and end between two /instances polls and
        never be logged. When the last recorded state is SUSPENDED / SUSPENDING /
        RESUMING, record RUNNING and return True so the caller also writes the
        transition to vm_state_log. Otherwise (already RUNNING, or no record, e.g.
        right after a proxy restart) record nothing and return False."""
        if self.last_state(microvm_id) in ("SUSPENDED", "SUSPENDING", "RESUMING"):
            self._records[microvm_id].record_state("RUNNING")
            return True
        return False

    def served_since(self, microvm_id: str, ts: float) -> bool:
        """True when the VM's last transition is RUNNING recorded at or after
        `ts` (epoch seconds).

        /instances passes its poll start time: a RUNNING written after the poll's
        list_microvms snapshot (by note_served, or by an overlapping poll) is newer
        than that snapshot, so the poll must not write its stale SUSPENDED /
        SUSPENDING / RESUMING over it. Writing it would log a RUNNING → stale →
        RUNNING blip and double count resumes, suspends and snapshot cost. The
        next poll records the real state."""
        record = self._records.get(microvm_id)
        if record is None or not record.transitions:
            return False
        last = record.transitions[-1]
        return last.state == "RUNNING" and last.timestamp >= ts

    @staticmethod
    def _zero_cost() -> dict:
        return {
            "running_secs": 0,
            "suspended_secs": 0,
            "running_cost_usd": 0.0,
            "suspended_cost_usd": 0.0,
            "burst_cost_usd": 0.0,
            "snapshot_read_cost_usd": 0.0,
            "snapshot_write_cost_usd": 0.0,
            "total_cost_usd": 0.0,
            "memory_gb": 0,
            "resume_count": 0,
            "suspend_count": 0,
        }

    def get_cost(self, microvm_id: str) -> dict:
        """Get cost breakdown for a specific MicroVM.

        Source of truth is the v_vm_sessions VIEW (running/suspended reconstructed
        in SQL from vm_state_log — the complete transition history). Dollars are
        computed here from those quantities via price_quantities(). Falls back to
        the in-memory record if the view has no row yet (e.g. a VM whose first
        state hasn't been written to the log) or if the DB read fails, so live
        display never regresses.
        """
        from proxy.storage import storage
        try:
            q = storage.vm_cost_quantities(microvm_id)
        except Exception:
            q = None
        return self._price(microvm_id, q)

    def get_costs_many(self, microvm_ids: list[str]) -> dict:
        """Batch get_cost: ONE vm_cost_quantities_many read for all ids, then the
        same per-VM pricing as get_cost (view row with live burst, else in-memory
        fallback, else zeros). A DB failure falls back per-VM exactly like
        get_cost. Returns {microvm_id: cost_dict}. SYNC (blocking DB read) — call
        it via asyncio.to_thread from async code."""
        from proxy.storage import storage
        try:
            qmap = storage.vm_cost_quantities_many(microvm_ids)
        except Exception:
            qmap = {}
        return {i: self._price(i, qmap.get(i)) for i in microvm_ids}

    def _price(self, microvm_id: str, q: dict | None) -> dict:
        """Price one VM from its view quantities `q` (or None → in-memory
        fallback / zeros). Pure: no DB access."""
        record = self._records.get(microvm_id)

        if q is not None:
            # running/suspended come from the VIEW (authoritative — reconstructed
            # from the complete state-log, more accurate than the periodically
            # flushed cache). burst_mb_seconds, however, accumulates continuously
            # in memory and only lands in the view's column on persist_cost — so
            # the LIVE in-memory value is fresher. Prefer it when we have a record
            # (a currently-bursting VM), else use the view's persisted value
            # (e.g. terminated VMs not in memory).
            live_burst = record.burst_mb_seconds if record is not None else None
            burst = live_burst if live_burst is not None else (q.get("burst_mb_seconds") or 0.0)
            # Arch selects pricing: prefer the view's stored arch, then the live
            # record's, then default. (View column added with the architecture
            # migration; falls back gracefully until then.)
            arch = q.get("architecture") or (record.architecture if record else None)
            return price_quantities(
                running_secs=q.get("running_secs") or 0.0,
                suspended_secs=q.get("suspended_secs") or 0.0,
                burst_mb_seconds=burst,
                memory_mib=q.get("memory_mib") or DEFAULT_MEMORY_MIB,
                architecture=arch,
                resume_count=q.get("resume_count") or 0,
                suspend_count=q.get("suspend_count") or 0,
            )
        # View has no row yet (VM's first state not logged) or DB unavailable →
        # fall back to full in-memory reconstruction.
        return record.compute() if record else self._zero_cost()

    def get_total_cost(self) -> dict:
        """Get aggregated cost across all tracked MicroVMs."""
        total_running_secs = 0
        total_suspended_secs = 0
        total_running_cost = 0.0
        total_suspended_cost = 0.0
        total_burst_cost = 0.0
        total_snapshot_read_cost = 0.0
        total_snapshot_write_cost = 0.0

        for record in self._records.values():
            cost = record.compute()
            total_running_secs += cost["running_secs"]
            total_suspended_secs += cost["suspended_secs"]
            total_running_cost += cost["running_cost_usd"]
            total_suspended_cost += cost["suspended_cost_usd"]
            total_burst_cost += cost["burst_cost_usd"]
            total_snapshot_read_cost += cost.get("snapshot_read_cost_usd", 0.0)
            total_snapshot_write_cost += cost.get("snapshot_write_cost_usd", 0.0)

        total = (total_running_cost + total_suspended_cost + total_burst_cost +
                 total_snapshot_read_cost + total_snapshot_write_cost)
        return {
            "running_secs": total_running_secs,
            "suspended_secs": total_suspended_secs,
            "running_cost_usd": round(total_running_cost, 6),
            "suspended_cost_usd": round(total_suspended_cost, 6),
            "burst_cost_usd": round(total_burst_cost, 6),
            "snapshot_read_cost_usd": round(total_snapshot_read_cost, 6),
            "snapshot_write_cost_usd": round(total_snapshot_write_cost, 6),
            "total_cost_usd": round(total, 6),
            "microvm_count": len(self._records),
        }

    def get_session_cost(self, session_vm_ids: list[str]) -> dict:
        """
        Get aggregated cost for a session (sum of all VMs that served it).

        Each VM's cost comes from get_cost() — i.e. the v_vm_sessions view (with
        in-memory fallback) — so this rollup is view-backed too and inherits the
        complete-history reconstruction, including terminated predecessors.

        Args:
            session_vm_ids: List of all MicroVM IDs that served this session
                           (including terminated ones from rotation)
        """
        return self.aggregate_session_cost(
            [self.get_cost(vm_id) for vm_id in session_vm_ids], len(session_vm_ids))

    @staticmethod
    def aggregate_session_cost(costs: list[dict], vm_count: int) -> dict:
        """Sum already-computed per-VM cost dicts into the session rollup shape
        returned by get_session_cost (pure, no DB access)."""
        total_running_secs = 0
        total_suspended_secs = 0
        total_running_cost = 0.0
        total_suspended_cost = 0.0
        total_burst_cost = 0.0
        total_snapshot_read_cost = 0.0
        total_snapshot_write_cost = 0.0

        for cost in costs:
            total_running_secs += cost["running_secs"]
            total_suspended_secs += cost["suspended_secs"]
            total_running_cost += cost["running_cost_usd"]
            total_suspended_cost += cost.get("suspended_cost_usd", 0.0)
            total_burst_cost += cost.get("burst_cost_usd", 0.0)
            total_snapshot_read_cost += cost.get("snapshot_read_cost_usd", 0.0)
            total_snapshot_write_cost += cost.get("snapshot_write_cost_usd", 0.0)

        total = (total_running_cost + total_suspended_cost + total_burst_cost +
                 total_snapshot_read_cost + total_snapshot_write_cost)
        return {
            "running_secs": total_running_secs,
            "suspended_secs": total_suspended_secs,
            "running_cost_usd": round(total_running_cost, 6),
            "suspended_cost_usd": round(total_suspended_cost, 6),
            "burst_cost_usd": round(total_burst_cost, 6),
            "snapshot_read_cost_usd": round(total_snapshot_read_cost, 6),
            "snapshot_write_cost_usd": round(total_snapshot_write_cost, 6),
            "total_cost_usd": round(total, 6),
            "vm_count": vm_count,
        }

    @property
    def tracked_ids(self) -> list[str]:
        """List all tracked MicroVM IDs."""
        return list(self._records.keys())

    def persist_cost(self, microvm_id: str, storage) -> None:
        """Persist the VM's live-accumulated burst_mb_seconds to the DB.

        This is the ONLY cost quantity we persist — running/suspended seconds are
        reconstructed on read from vm_state_log (via v_vm_sessions) and dollars are
        computed in Python, so nothing else needs a cache column. burst does,
        because it's accumulated from metric polls and can't be derived from the
        state log."""
        record = self._records.get(microvm_id)
        if not record:
            return
        try:
            storage.vm_session_update_burst(microvm_id, record.burst_mb_seconds)
        except Exception:
            pass  # Non-critical — burst is also live in memory until next flush

    def persist_costs(self, microvm_ids: list[str], storage) -> None:
        """Batch persist_cost (per-VM failures swallowed, as in persist_cost).
        SYNC — call via asyncio.to_thread from async code."""
        for microvm_id in microvm_ids:
            self.persist_cost(microvm_id, storage)

    def persist_all(self, storage) -> int:
        """Flush EVERY tracked MicroVM's cost to the DB.

        Used on graceful shutdown so an ECS task that is being drained/replaced
        (rolling deploy, scale-in) doesn't lose the only in-memory-only value,
        burst_mb_seconds, since its last periodic persist_cost. Running/suspended
        seconds are always re-derivable from vm_state_log on load_from_db, so this
        exists specifically to make the burst delta handoff-safe.

        Returns the number of records flushed (handy for tests/logging).
        """
        count = 0
        for microvm_id in list(self._records.keys()):
            self.persist_cost(microvm_id, storage)
            count += 1
        return count

    def load_from_db(self, storage) -> None:
        """
        Load cost state from the database on startup.
        Reconstructs CostTracker records from vm_sessions + vm_state_log.
        """
        try:
            active_sessions = storage.vm_session_list_active()

            for session in active_sessions:
                microvm_id = session["microvm_id"]
                memory_mib = session.get("memory_mib") or DEFAULT_MEMORY_MIB
                db_burst_mb_sec = session.get("burst_mb_seconds") or 0

                # Create record with stored burst data
                record = MicroVMCostRecord(
                    microvm_id=microvm_id,
                    memory_mib=memory_mib,
                    burst_mb_seconds=db_burst_mb_sec,
                )

                # Reconstruct transitions from state_log
                log_entries = storage.vm_state_log_get(microvm_id)
                if log_entries:
                    from datetime import datetime
                    for entry in log_entries:
                        try:
                            ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                            record.transitions.append(StateTransition(
                                state=entry["new_state"],
                                timestamp=ts.timestamp()
                            ))
                        except (ValueError, TypeError):
                            pass

                if record.transitions:
                    self._records[microvm_id] = record

        except Exception:
            pass  # Silently fail — fresh start is acceptable
