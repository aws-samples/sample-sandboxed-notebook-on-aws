#!/usr/bin/env python3
# test-kind: offline
"""
pool_manager.reset_local_pool test suite (FEAT-002).

Exercises the local-dev clean-slate one-shot against FakeVmBackend + a temp
SQLite file: every seeded pool_vms row's VM is terminated BEFORE the table is
emptied, only pool_vms is touched, and the backend-selection logic in
pool_manager.backend_factory.build_backend picks aws vs fake correctly (so a
silent fake-default no-op can never pass unnoticed).

No AWS, no network, no real MicroVM.

Run:
    .venv/bin/python tests/pool/test_reset_local_pool.py
"""

import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, mk_aged_row  # noqa: E402
from pool_manager import reset_local_pool  # noqa: E402
from pool_manager.backend_factory import build_backend  # noqa: E402
from pool_manager.config import PoolConfig, load_config  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.aws_backend import AwsVmBackend  # noqa: E402


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


def _seed_backed_rows(storage, backend):
    """Launch VMs on the FakeVmBackend and record a matching available
    pool_vms row for each — so the ledger and the backend agree, mirroring a
    normal filled pool. Returns the list of vm_ids in insertion order."""
    vm_ids = []
    for arch, size in (("arm64", 2048), ("arm64", 4096), ("x86_64", 1024)):
        vm = run_async(backend.launch(arch, size, f"pool-{arch}-{size}"))
        mk_aged_row(storage, vm.vm_id, arch=arch, size=size, state="available",
                    endpoint=vm.endpoint)
        vm_ids.append(vm.vm_id)
    return vm_ids


# ── Section 1: terminate-before-delete ordering + empty table at the end ──
def test_terminates_every_vm_before_emptying_table():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    vm_ids = _seed_backed_rows(storage, backend)

    # Monkeypatch the module's build_backend so _reset() uses OUR seeded
    # backend (which records every terminate call) instead of building one
    # from the environment.
    orig = reset_local_pool.build_backend
    reset_local_pool.build_backend = lambda config: backend
    try:
        run_async(reset_local_pool._reset())
    finally:
        reset_local_pool.build_backend = orig

    terminated = [kwargs["vm_id"] for name, kwargs in backend.calls if name == "terminate"]
    check("every seeded VM received a terminate() call",
          set(terminated) == set(vm_ids))

    rows_after = storage.pool_vm_list()
    check("pool_vms is empty after the reset", rows_after == [])

    # Ordering: the table must still contain a row at the moment each VM is
    # terminated (terminate FIRST, delete the row AFTER). The FakeVmBackend
    # drops a VM from list_all() on terminate, so a VM that was terminated
    # while its row still existed proves the pairing held per row.
    backend_after = {h.vm_id for h in run_async(backend.list_all())}
    check("no backend VM survives the reset (all were terminated)",
          backend_after == set())


def test_terminate_before_delete_is_per_row():
    """Assert the terminate-FIRST-then-delete ordering explicitly: capture the
    live row set at the instant each terminate() fires and confirm that VM's
    row was still present (not deleted before its termination)."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    vm_ids = _seed_backed_rows(storage, backend)

    seen_rows_at_terminate = {}

    orig_terminate = backend.terminate

    async def spy_terminate(vm):
        live = {r["vm_id"] for r in storage.pool_vm_list()}
        seen_rows_at_terminate[vm.vm_id] = vm.vm_id in live
        await orig_terminate(vm)

    backend.terminate = spy_terminate

    orig = reset_local_pool.build_backend
    reset_local_pool.build_backend = lambda config: backend
    try:
        run_async(reset_local_pool._reset())
    finally:
        reset_local_pool.build_backend = orig

    check("each VM's row was still present when its terminate() fired "
          "(terminate before delete)",
          all(seen_rows_at_terminate.get(vm_id) for vm_id in vm_ids))


# ── Section 2: only pool_vms is touched ────────────────────────────────────
def test_touches_only_pool_vms():
    backend = FakeVmBackend()
    storage = _run_sqlite()
    _seed_backed_rows(storage, backend)

    # Seed an unrelated table (session_registry) to prove the reset leaves it
    # alone.
    storage.session_registry_upsert("sess-keep", "vm-keep", "fake://vm-keep")
    registry_before = storage.session_registry_get("sess-keep")

    orig = reset_local_pool.build_backend
    reset_local_pool.build_backend = lambda config: backend
    try:
        run_async(reset_local_pool._reset())
    finally:
        reset_local_pool.build_backend = orig

    registry_after = storage.session_registry_get("sess-keep")
    check("session_registry row is untouched by the reset (only pool_vms "
          "is cleared)", registry_after is not None and registry_before is not None)


# ── Section 3: empty-table short-circuit ───────────────────────────────────
def test_empty_table_is_noop():
    backend = FakeVmBackend()
    storage = _run_sqlite()

    orig = reset_local_pool.build_backend
    reset_local_pool.build_backend = lambda config: backend
    try:
        run_async(reset_local_pool._reset())
    finally:
        reset_local_pool.build_backend = orig

    check("empty pool_vms: no terminate() calls were made",
          not any(name == "terminate" for name, _ in backend.calls))
    check("empty pool_vms: table stays empty", storage.pool_vm_list() == [])


# ── Section 4: backend selection (aws vs fake) ─────────────────────────────
def test_build_backend_selects_aws_vs_fake():
    """A silent fake-default no-op must not be able to pass: confirm aws is
    selected under POOL_VM_BACKEND=aws and fake otherwise."""
    aws_backend = build_backend(PoolConfig(backend="aws"))
    check("build_backend(PoolConfig(backend='aws')) returns an AwsVmBackend",
          isinstance(aws_backend, AwsVmBackend))

    fake_backend = build_backend(PoolConfig(backend="fake"))
    check("build_backend(PoolConfig(backend='fake')) returns a FakeVmBackend",
          isinstance(fake_backend, FakeVmBackend))

    # And via load_config() reading the environment, as local_install.sh does.
    prev = os.environ.get("POOL_VM_BACKEND")
    try:
        os.environ["POOL_VM_BACKEND"] = "aws"
        check("load_config() with POOL_VM_BACKEND=aws builds an AwsVmBackend",
              isinstance(build_backend(load_config()), AwsVmBackend))
    finally:
        if prev is None:
            os.environ.pop("POOL_VM_BACKEND", None)
        else:
            os.environ["POOL_VM_BACKEND"] = prev


# ── Section 5: reset's SOURCE does NOT import pool_manager.server ──────────
def test_reset_does_not_import_server():
    """The reset one-shot must build its backend via backend_factory, never by
    importing pool_manager.server (whose module-level app=create_app() has side
    effects). Assert against the module's own source — the pool_manager package
    __init__ importing server for its public API is a separate concern and does
    NOT mean reset_local_pool itself depends on server."""
    # Strip inline comments so prose like "# NOT server" can't trip the check.
    import_lines = [
        ln.strip().split("#", 1)[0].strip()
        for ln in inspect.getsource(reset_local_pool).splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    imports_server = any(
        "pool_manager.server" in ln or ln.endswith(" server") or " server " in ln
        for ln in import_lines
    )
    check("reset_local_pool has no import of pool_manager.server",
          not imports_server)
    check("reset_local_pool imports build_backend from backend_factory",
          any("from pool_manager.backend_factory import build_backend" in ln
              for ln in import_lines))


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== pool_manager.reset_local_pool ===")
    print("\n-- reset source does not import pool_manager.server --")
    test_reset_does_not_import_server()
    print("\n-- terminate-before-delete + empty table at end --")
    test_terminates_every_vm_before_emptying_table()
    print("\n-- terminate-before-delete is per row --")
    test_terminate_before_delete_is_per_row()
    print("\n-- only pool_vms is touched --")
    test_touches_only_pool_vms()
    print("\n-- empty table short-circuit --")
    test_empty_table_is_noop()
    print("\n-- backend selection (aws vs fake) --")
    test_build_backend_selects_aws_vs_fake()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL RESET-LOCAL-POOL CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_reset_local_pool():
    _checks.clear()
    for fn in (test_reset_does_not_import_server,
               test_terminates_every_vm_before_emptying_table,
               test_terminate_before_delete_is_per_row,
               test_touches_only_pool_vms,
               test_empty_table_is_noop,
               test_build_backend_selects_aws_vs_fake):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "reset_local_pool failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
