#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the shared pool grant budget (code-review D-B-04 / D-I-01).

A claimed spare stays in 'granting' for resume + rebind + restore. The step
timeouts live once in pool_manager/config.py; the reconcile reaper, AwsVmBackend
and the proxy derive their bounds from them.

Covers:
  - budget math (resume + rebind + restore = 160 s), reconcile timeout and proxy
    restore bound are both above the budget;
  - reconcile_once does NOT reap a 'granting' row aged 160 s (a slow restore
    grant still inside its budget) but does reap one older than the reconcile
    timeout;
  - PoolManager, AwsVmBackend and both proxy grant call sites use the shared
    values;
  - `import pool_manager.config` does not import pool_manager.server / manager /
    aws_backend (the proxy imports it).

No AWS, no network (FakeVmBackend + temp SQLite).
Run:
    .venv/bin/python tests/pool/test_grant_budget.py
"""
import asyncio
import inspect
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

from tests.pool._poolkit import _run_sqlite, build_config, build_manager, mk_aged_row  # noqa: E402
from pool_manager import config as pc  # noqa: E402
from pool_manager import manager as pm  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


def _backdate_updated_at(storage, vm_id, seconds_ago):
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    storage.pool_vm_backdate(vm_id, updated_at=old)


def test_budget_math():
    print("\n-- budget math --")
    check("budget = resume + rebind + restore = 160 s",
          pc.GRANT_BUDGET_SECONDS == pc.RESUME_TIMEOUT_SECONDS + pc.REBIND_TIMEOUT_SECONDS
          + pc.RESTORE_TIMEOUT_SECONDS == 160.0, pc.GRANT_BUDGET_SECONDS)
    check("reconcile timeout exceeds the budget",
          pc.RECONCILE_GRANT_TIMEOUT_SECONDS > pc.GRANT_BUDGET_SECONDS,
          pc.RECONCILE_GRANT_TIMEOUT_SECONDS)
    check("proxy restore bound exceeds the budget",
          pc.PROXY_GRANT_RESTORE_BOUND_SECONDS > pc.GRANT_BUDGET_SECONDS,
          pc.PROXY_GRANT_RESTORE_BOUND_SECONDS)


def test_shared_values_used():
    print("\n-- call sites use the shared values --")
    check("PoolManager default grant timeout = reconcile timeout",
          pm.DEFAULT_GRANT_TIMEOUT_SECONDS == pc.RECONCILE_GRANT_TIMEOUT_SECONDS)
    check("grant() resumes with RESUME_TIMEOUT_SECONDS",
          "timeout=RESUME_TIMEOUT_SECONDS" in inspect.getsource(pm.PoolManager.grant))
    from pool_manager.aws_backend import AwsVmBackend
    sig = inspect.signature(AwsVmBackend.__init__).parameters
    check("AwsVmBackend http timeout default = REBIND_TIMEOUT_SECONDS",
          sig["http_timeout_seconds"].default == pc.REBIND_TIMEOUT_SECONDS)
    check("AwsVmBackend restore timeout default = RESTORE_TIMEOUT_SECONDS",
          sig["restore_timeout_seconds"].default == pc.RESTORE_TIMEOUT_SECONDS)
    from proxy.platform.routes import microvm as routes_microvm
    from proxy.platform import microvm_manager
    check("proxy /launch restore-grant bound = PROXY_GRANT_RESTORE_BOUND_SECONDS",
          routes_microvm._POOL_GRANT_RESTORE_TIMEOUT_SECONDS == pc.PROXY_GRANT_RESTORE_BOUND_SECONDS)
    check("proxy recovery restore-grant bound = PROXY_GRANT_RESTORE_BOUND_SECONDS",
          microvm_manager._POOL_GRANT_RESTORE_TIMEOUT_SECONDS == pc.PROXY_GRANT_RESTORE_BOUND_SECONDS)
    check("proxy non-restore grant timeout stays 10 s",
          routes_microvm._POOL_GRANT_TIMEOUT_SECONDS == 10.0
          and microvm_manager._POOL_GRANT_TIMEOUT_SECONDS == 10.0)


def test_reconcile_respects_budget():
    print("\n-- reconcile vs a slow restore grant --")
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=0, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    check("manager uses the derived reconcile timeout by default",
          manager.grant_timeout_seconds == pc.RECONCILE_GRANT_TIMEOUT_SECONDS)

    slow = asyncio.run(backend.launch("arm64", 2048, "pool-slow"))
    mk_aged_row(storage, slow.vm_id, arch="arm64", size=2048, state="granting",
                endpoint=slow.endpoint)
    _backdate_updated_at(storage, slow.vm_id, seconds_ago=pc.GRANT_BUDGET_SECONDS)

    stuck = asyncio.run(backend.launch("arm64", 2048, "pool-stuck"))
    mk_aged_row(storage, stuck.vm_id, arch="arm64", size=2048, state="granting",
                endpoint=stuck.endpoint)
    _backdate_updated_at(storage, stuck.vm_id,
                         seconds_ago=pc.RECONCILE_GRANT_TIMEOUT_SECONDS + 20)

    asyncio.run(manager.reconcile_once())
    rows = {r["vm_id"]: r["state"] for r in storage.pool_vm_list()}
    alive = {h.vm_id for h in asyncio.run(backend.list_all())}
    check("granting row aged 160 s (inside budget) is kept",
          rows.get(slow.vm_id) == "granting" and slow.vm_id in alive, rows)
    check("granting row older than the reconcile timeout is reaped",
          stuck.vm_id not in rows and stuck.vm_id not in alive, rows)


def test_config_import_is_light():
    print("\n-- import pool_manager.config stays stdlib-only --")
    code = ("import sys, pool_manager.config; "
            "bad = [m for m in ('pool_manager.server', 'pool_manager.manager', "
            "'pool_manager.aws_backend', 'pool_manager.fake_backend', 'boto3', 'fastapi') "
            "if m in sys.modules]; print(','.join(bad)); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": ROOT})
    check("import pool_manager.config pulls in no server/manager/backend/boto3/fastapi",
          r.returncode == 0, (r.stdout + r.stderr).strip())
    code = "import pool_manager; print(pool_manager.PoolConfig.__name__)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": ROOT})
    check("lazy pool_manager.<name> still resolves", r.returncode == 0 and "PoolConfig" in r.stdout,
          (r.stdout + r.stderr).strip())


def main():
    test_budget_math()
    test_shared_values_used()
    test_reconcile_respects_budget()
    test_config_import_is_light()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
