"""
VmBackend — the pool manager's central seam (design §3.1).

The whole pool_manager service is written against this interface, never
against boto3 or a concrete VM directly. A "VM" the service holds is an
opaque VmHandle the backend understands — the service never knows whether
it is a real MicroVM (AwsVmBackend) or an in-memory fake (FakeVmBackend,
tests only).

All methods are async (the manager is asyncio; design §13.3).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# --- VM lifecycle state-string constants (shared across the service) ---
RUNNING = "RUNNING"
SUSPENDED = "SUSPENDED"
TERMINATED = "TERMINATED"
DEAD = "DEAD"

# Control-plane states meaning the VM is gone or going away. A VM in one of these
# is NOT a reconcilable orphan and must never be (re-)terminated — it is already
# dead/dying. Shared so aws_backend.list_all() filtering and manager.reconcile_once()
# both use ONE definition (no drift). "NOT_FOUND" matches the proxy's dead-state
# set; no pool backend produces it today (AwsVmBackend.get_state and
# FakeVmBackend.get_state return TERMINATED for a VM the control plane no
# longer knows).
VM_DEAD_STATES = frozenset({"TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"})


@dataclass
class VmHandle:
    """Opaque handle to a VM, real or fake. The service never looks past these
    fields — any backend-specific bookkeeping lives in `meta`."""
    vm_id: str
    endpoint: str
    arch: str
    size_mib: int
    launched_at: str  # UTC ISO
    meta: dict = field(default_factory=dict)


class BackendError(Exception):
    """Raised by any VmBackend method on failure. The manager catches this
    (and its subclasses) to drive cleanup/retry logic."""


class QuotaExceededError(BackendError):
    """Raised by `launch` when the backend simulates/encounters an AWS
    service-quota-exceeded condition (design §11 risk #2)."""


class VmBackend(ABC):
    """Abstract backend for all VM operations the pool manager needs.

    Two implementations, interchangeable via config (POOL_VM_BACKEND=fake|aws):
      - AwsVmBackend (aws_backend.py): real lambda-microvms calls; the only
        backend scripts/config.sh allows.
      - FakeVmBackend (fake_backend.py): pure in-memory, fault/latency
        injectable; used by the offline tests.
    """

    @abstractmethod
    async def launch(self, arch: str, size_mib: int, fake_session_id: str) -> VmHandle:
        """Launch a new VM (run-microvm). Returns its VmHandle."""
        ...

    @abstractmethod
    async def wait_healthy(self, vm: VmHandle, timeout: float) -> None:
        """Block until the VM is RUNNING and healthy (poll /run == 200)."""
        ...

    @abstractmethod
    async def suspend(self, vm: VmHandle) -> None:
        """Suspend the VM (suspend-microvm)."""
        ...

    @abstractmethod
    async def resume(self, vm: VmHandle, timeout: float) -> str:
        """Resume the VM and wait until RUNNING. Returns the final
        control-plane state: RUNNING, or the dead state observed."""
        ...

    @abstractmethod
    async def rebind(self, vm: VmHandle, identity_payload: dict) -> None:
        """POST /rebind to the VM with the real session's identity payload."""
        ...

    @abstractmethod
    async def restore_state(self, vm: VmHandle, restore_from_session: str) -> None:
        """POST /restore-state to the VM, restoring from a prior session's S3
        checkpoint."""
        ...

    @abstractmethod
    async def terminate(self, vm: VmHandle) -> None:
        """Terminate the VM (terminate-microvm)."""
        ...

    @abstractmethod
    async def get_state(self, vm_id: str) -> str:
        """Return the VM's control-plane state string."""
        ...

    @abstractmethod
    async def list_all(self) -> list[VmHandle]:
        """List every VM the backend currently knows about (for orphan
        reconciliation, design §5.4)."""
        ...
