"""
FakeVmBackend — pure in-memory VmBackend implementation (design §3.1).

Built and used FIRST, with zero real AWS and zero real MicroVMs, so the whole
pool manager (fill, recycle, grant, orphan recovery) can be exhaustively
robustness-tested with deterministic fault + latency injection.
"""

import asyncio
import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pool_manager.vm_backend import (
    DEAD,
    RUNNING,
    SUSPENDED,
    TERMINATED,
    BackendError,
    QuotaExceededError,
    VmBackend,
    VmHandle,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _FakeVm:
    vm_id: str
    arch: str
    size_mib: int
    endpoint: str
    launched_at: str
    state: str = RUNNING
    fake_session_id: str = ""
    rebound_identity: dict = None
    restored_from: str = None


class FakeVmBackend(VmBackend):
    """In-memory VmBackend with a fault/latency injection surface. Deterministic
    (no unseeded randomness) — every fault is driven by an explicit toggle/
    counter set by the test, never by chance."""

    def __init__(self, launch_delay_seconds: float = 0.0):
        self._vms: dict[str, _FakeVm] = {}
        self._id_seq = itertools.count(1)
        # Every method call recorded as (name, args_dict) — for assertions
        # (ordering, bounded-concurrency, payload shape).
        self.calls: list[tuple[str, dict]] = []

        # --- latency injection: method name -> seconds to sleep before acting ---
        # Default launch latency simulates the real ~10s AWS provisioning floor
        # (launch→RUNNING→healthy), so a standalone/demo run of the service feels
        # like production (grants wait on in-flight mints, fill is paced, the
        # transient-dip-then-converge behavior is real). Defaults to 0 here so the
        # fast offline test suite — which constructs FakeVmBackend() directly — is
        # not slowed; server.py seeds the realistic default from
        # POOL_FAKE_LAUNCH_DELAY_SECONDS, and any test can override per-method via
        # set_latency("launch", N).
        self._latency: dict[str, float] = {}
        if launch_delay_seconds:
            self._latency["launch"] = launch_delay_seconds

        # --- failure injection ---
        self._fail_launch_remaining = 0
        self._fail_launch_quota = False
        self._fail_wait_healthy_remaining = 0
        self._fail_suspend_remaining = 0
        self._fail_resume_remaining = 0
        self._fail_resume_returns_dead_remaining = 0
        self._fail_rebind_remaining = 0
        self._fail_restore_state_remaining = 0
        self._fail_terminate_remaining = 0

    # ------------------------------------------------------------------
    # fault/latency injection surface
    # ------------------------------------------------------------------

    def set_latency(self, method: str, seconds: float) -> None:
        """Make `method` sleep `seconds` before doing its work."""
        self._latency[method] = seconds

    def fail_launch_times(self, n: int) -> None:
        """The next `n` calls to launch() raise BackendError."""
        self._fail_launch_remaining = n

    def raise_quota_exceeded_on_launch(self) -> None:
        """The next call to launch() raises QuotaExceededError."""
        self._fail_launch_quota = True

    def fail_wait_healthy_times(self, n: int) -> None:
        """The next `n` calls to wait_healthy() raise BackendError (simulated
        health-check timeout)."""
        self._fail_wait_healthy_remaining = n

    def fail_suspend_times(self, n: int) -> None:
        self._fail_suspend_remaining = n

    def fail_resume_times(self, n: int) -> None:
        self._fail_resume_remaining = n

    def fail_resume_returns_dead_times(self, n: int) -> None:
        """The next `n` calls to resume() succeed (no raise) but leave the VM
        in a DEAD state — simulating a resume that comes back unhealthy."""
        self._fail_resume_returns_dead_remaining = n

    def fail_next_rebind(self, n: int = 1) -> None:
        self._fail_rebind_remaining = n

    def fail_next_restore_state(self, n: int = 1) -> None:
        self._fail_restore_state_remaining = n

    def fail_terminate_times(self, n: int) -> None:
        self._fail_terminate_remaining = n

    def force_state(self, vm_id: str, state: str) -> None:
        """Flip a VM's state out-of-band (e.g. a mid-grant death)."""
        vm = self._vms.get(vm_id)
        if vm is not None:
            vm.state = state

    def inject_backend_only_vm(self, arch: str = "arm64", size_mib: int = 2048,
                               vm_id: str = None, launched_at: str = None,
                               state: str = RUNNING) -> VmHandle:
        """Create a VM that exists in the backend but was never recorded in the
        DB — for orphan-recovery tests (design §5.4: 'backend VM, no DB row')."""
        vm_id = vm_id or f"vm-orphan-{next(self._id_seq)}"
        launched_at = launched_at or _now_iso()
        vm = _FakeVm(
            vm_id=vm_id, arch=arch, size_mib=size_mib,
            endpoint=f"fake://{vm_id}", launched_at=launched_at, state=state,
        )
        self._vms[vm_id] = vm
        return self._handle(vm)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _record(self, name: str, **kwargs) -> None:
        self.calls.append((name, kwargs))

    @staticmethod
    def _handle(vm: "_FakeVm") -> VmHandle:
        return VmHandle(
            vm_id=vm.vm_id, endpoint=vm.endpoint, arch=vm.arch,
            size_mib=vm.size_mib, launched_at=vm.launched_at,
            meta={"state": vm.state},
        )

    async def _delay(self, method: str) -> None:
        seconds = self._latency.get(method)
        if seconds:
            await asyncio.sleep(seconds)

    def _require_vm(self, vm_id: str) -> "_FakeVm":
        vm = self._vms.get(vm_id)
        if vm is None:
            raise BackendError(f"unknown vm_id: {vm_id}")
        return vm

    # ------------------------------------------------------------------
    # VmBackend
    # ------------------------------------------------------------------

    async def launch(self, arch: str, size_mib: int, fake_session_id: str) -> VmHandle:
        self._record("launch", arch=arch, size_mib=size_mib, fake_session_id=fake_session_id)
        await self._delay("launch")
        if self._fail_launch_quota:
            self._fail_launch_quota = False
            raise QuotaExceededError("simulated ServiceQuotaExceeded on launch")
        if self._fail_launch_remaining > 0:
            self._fail_launch_remaining -= 1
            raise BackendError("simulated launch failure")

        vm_id = f"vm-{next(self._id_seq)}-{uuid.uuid4().hex[:8]}"
        vm = _FakeVm(
            vm_id=vm_id, arch=arch, size_mib=size_mib,
            endpoint=f"fake://{vm_id}", launched_at=_now_iso(),
            state=RUNNING, fake_session_id=fake_session_id,
        )
        self._vms[vm_id] = vm
        return self._handle(vm)

    async def wait_healthy(self, vm: VmHandle, timeout: float) -> None:
        self._record("wait_healthy", vm_id=vm.vm_id, timeout=timeout)
        await self._delay("wait_healthy")
        if self._fail_wait_healthy_remaining > 0:
            self._fail_wait_healthy_remaining -= 1
            raise BackendError(f"wait_healthy timed out for {vm.vm_id}")
        self._require_vm(vm.vm_id)  # raises if unknown

    async def suspend(self, vm: VmHandle) -> None:
        self._record("suspend", vm_id=vm.vm_id)
        await self._delay("suspend")
        if self._fail_suspend_remaining > 0:
            self._fail_suspend_remaining -= 1
            raise BackendError(f"suspend failed for {vm.vm_id}")
        self._require_vm(vm.vm_id).state = SUSPENDED

    async def resume(self, vm: VmHandle, timeout: float) -> str:
        self._record("resume", vm_id=vm.vm_id, timeout=timeout)
        await self._delay("resume")
        if self._fail_resume_remaining > 0:
            self._fail_resume_remaining -= 1
            raise BackendError(f"resume failed for {vm.vm_id}")
        fake_vm = self._require_vm(vm.vm_id)
        if self._fail_resume_returns_dead_remaining > 0:
            self._fail_resume_returns_dead_remaining -= 1
            fake_vm.state = DEAD
            return DEAD
        fake_vm.state = RUNNING
        return RUNNING

    async def rebind(self, vm: VmHandle, identity_payload: dict) -> None:
        self._record("rebind", vm_id=vm.vm_id, identity_payload=dict(identity_payload))
        await self._delay("rebind")
        if self._fail_rebind_remaining > 0:
            self._fail_rebind_remaining -= 1
            raise BackendError(f"rebind failed (500) for {vm.vm_id}")
        self._require_vm(vm.vm_id).rebound_identity = dict(identity_payload)

    async def restore_state(self, vm: VmHandle, restore_from_session: str) -> None:
        self._record("restore_state", vm_id=vm.vm_id, restore_from_session=restore_from_session)
        await self._delay("restore_state")
        if self._fail_restore_state_remaining > 0:
            self._fail_restore_state_remaining -= 1
            raise BackendError(f"restore_state failed for {vm.vm_id}")
        self._require_vm(vm.vm_id).restored_from = restore_from_session

    async def terminate(self, vm: VmHandle) -> None:
        self._record("terminate", vm_id=vm.vm_id)
        await self._delay("terminate")
        if self._fail_terminate_remaining > 0:
            self._fail_terminate_remaining -= 1
            raise BackendError(f"terminate failed for {vm.vm_id}")
        fake_vm = self._vms.get(vm.vm_id)
        if fake_vm is not None:
            fake_vm.state = TERMINATED
        # Terminated VMs are dropped from the store — list_all() should no
        # longer report them (mirrors a real control plane eventually losing
        # a terminated instance).
        self._vms.pop(vm.vm_id, None)

    async def get_state(self, vm_id: str) -> str:
        self._record("get_state", vm_id=vm_id)
        await self._delay("get_state")
        vm = self._vms.get(vm_id)
        return vm.state if vm is not None else TERMINATED

    async def list_all(self) -> list[VmHandle]:
        self._record("list_all")
        await self._delay("list_all")
        return [self._handle(vm) for vm in self._vms.values()]
