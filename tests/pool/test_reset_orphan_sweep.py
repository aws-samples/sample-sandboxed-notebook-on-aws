#!/usr/bin/env python3
# test-kind: offline
"""
pool_manager.reset_local_pool — ORPHAN SWEEP checks.

A stub backend reports a mix of live VMs; only true orphans (this app's image
family, no vm_sessions row, not in pool_vms, older than the grace window) may be
terminated. Everything else — notebook VMs (even with a TERMINATED row), other
image families, young VMs, VMs with no start time — must be left alone. Also
checks the fake backend (no image family) skips the sweep entirely, and a
list_all() failure is tolerated.

No AWS, no network, no real MicroVM. Temp SQLite only.

Run:
    .venv/bin/python tests/pool/test_reset_orphan_sweep.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, mk_aged_row  # noqa: E402
from pool_manager import reset_local_pool  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.vm_backend import VmHandle  # noqa: E402

BASE = "arn:aws:lambda:us-west-2:111111111111:microvm-image:notebook-microvm"

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class StubBackend:
    """Minimal AwsVmBackend stand-in: image_arn_base + list_all + terminate."""

    def __init__(self, vms, list_raises=False):
        self.image_arn_base = BASE
        self._vms = vms
        self._list_raises = list_raises
        self.terminated = []

    async def list_all(self):
        if self._list_raises:
            raise RuntimeError("ListMicrovms throttled")
        return [v for v in self._vms if v.vm_id not in self.terminated]

    async def terminate(self, vm):
        self.terminated.append(vm.vm_id)


def _vm(vm_id, image, started_at, state="SUSPENDED"):
    return VmHandle(vm_id=vm_id, endpoint="", arch="", size_mib=0, launched_at="",
                    meta={"state": state, "image_arn": image, "started_at": started_at})


def _run(backend):
    orig = reset_local_pool.build_backend
    reset_local_pool.build_backend = lambda config: backend
    try:
        asyncio.run(reset_local_pool._reset())
    finally:
        reset_local_pool.build_backend = orig


def test_sweep_terminates_only_true_orphans():
    storage = _run_sqlite()
    # A tracked spare (pool_vms row) — phase 1 terminates it, sweep must not double-handle.
    mk_aged_row(storage, "vm-tracked", state="available")
    # Notebook VMs: one active, one whose row is TERMINATED but VM still listed.
    storage.vm_session_create("vm-notebook", notebook_id="7", memory_mib=2048)
    storage.vm_session_create("vm-notebook-old", notebook_id="8", memory_mib=2048)
    storage.vm_session_mark_terminated("vm-notebook-old")

    vms = [
        _vm("vm-orphan-1", f"{BASE}-arm64-2048", _ago(300)),
        _vm("vm-orphan-2", f"{BASE}-arm64-8192", _ago(45), state="RUNNING"),
        _vm("vm-tracked", f"{BASE}-arm64-2048", _ago(300)),
        _vm("vm-notebook", f"{BASE}-arm64-2048", _ago(300)),
        _vm("vm-notebook-old", f"{BASE}-arm64-2048", _ago(300)),
        _vm("vm-other-app", "arn:aws:lambda:us-west-2:111111111111:microvm-image:someone-else-arm64-2048", _ago(300)),
        _vm("vm-young", f"{BASE}-arm64-4096", _ago(2)),
        _vm("vm-no-start", f"{BASE}-arm64-4096", None),
    ]
    backend = StubBackend(vms)
    _run(backend)

    t = set(backend.terminated)
    check("both aged orphans of this image family are terminated",
          {"vm-orphan-1", "vm-orphan-2"} <= t)
    check("tracked spare is terminated exactly once (phase 1, not again by the sweep)",
          backend.terminated.count("vm-tracked") == 1)
    check("notebook VM with an active vm_sessions row is NOT touched", "vm-notebook" not in t)
    check("notebook VM with a TERMINATED vm_sessions row is NOT touched", "vm-notebook-old" not in t)
    check("VM from a different image family is NOT touched", "vm-other-app" not in t)
    check("VM younger than the grace window is NOT touched", "vm-young" not in t)
    check("VM with no start time is NOT touched", "vm-no-start" not in t)
    check("pool_vms is empty afterwards", storage.pool_vm_list() == [])


def test_list_failure_is_tolerated():
    _run_sqlite()
    backend = StubBackend([], list_raises=True)
    try:
        _run(backend)
        ok = True
    except Exception:
        ok = False
    check("a list_all() failure does not fail the reset", ok)
    check("nothing is terminated when listing fails", backend.terminated == [])


def test_fake_backend_skips_sweep():
    storage = _run_sqlite()
    backend = FakeVmBackend()
    vm = asyncio.run(backend.launch("arm64", 2048, "pool-x"))  # a VM with NO pool_vms row
    _run(backend)
    terminated = [kw["vm_id"] for name, kw in backend.calls if name == "terminate"]
    check("fake backend (no image family): sweep terminates nothing", vm.vm_id not in terminated)
    check("fake backend: pool_vms still empty", storage.pool_vm_list() == [])


def main():
    print("\n=== reset_local_pool orphan sweep ===")
    test_sweep_terminates_only_true_orphans()
    test_list_failure_is_tolerated()
    test_fake_backend_skips_sweep()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


def test_reset_orphan_sweep():
    _checks.clear()
    test_sweep_terminates_only_true_orphans()
    test_list_failure_is_tolerated()
    test_fake_backend_skips_sweep()
    failed = [n for n, ok in _checks if not ok]
    assert not failed, "orphan sweep failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
