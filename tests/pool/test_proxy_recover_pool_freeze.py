#!/usr/bin/env python3
# test-kind: offline
"""
Proxy `_do_recover` pool-freeze test suite (pool-freeze-lifecycle-attrs task).

Exercises MicrovmManager._do_recover's idle-timeout freeze: when
POOL_ENABLED is true, the recovery cold-launch fallback (launch_for_rotation)
is called with the FIXED IDLE_TIMEOUT_SECONDS value, not the old session's
saved idle_timeout_sec. POOL_ENABLED=false keeps today's behavior (the saved
idle value is reused).

max-duration is NOT exercised here — it is already always the fixed
MAX_LIFETIME_SECONDS value in both the pool-grant and launch_for_rotation
fallback branches (never a per-session override), confirmed by reading
microvm_manager.py's existing storage.vm_session_create calls.

Uses the same fakes/harness as tests/pool/test_proxy_recover_grant.py.

Run:
    .venv/bin/python tests/pool/test_proxy_recover_pool_freeze.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.microvm_manager as mm_mod  # noqa: E402
from proxy.platform.microvm_manager import MicrovmManager  # noqa: E402

# ── tiny test harness (matches tests/pool/test_proxy_recover_grant.py convention) ──
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


class EnvSandbox:
    def __enter__(self):
        self._snapshot = dict(os.environ)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._snapshot)


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    behavior = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        return _FakeAsyncClient.behavior(url, json)


class _FakeStorage:
    def __init__(self):
        self.vm_session_create_calls = []
        self.vm_session_update_state_calls = []
        self._registry = {}

    def session_registry_upsert(self, session_id, vm_id, endpoint):
        self._registry[session_id] = {"vm_id": vm_id, "endpoint": endpoint}

    def session_registry_delete(self, session_id):
        self._registry.pop(session_id, None)

    def session_registry_get(self, session_id):
        return self._registry.get(session_id)

    def vm_session_create(self, **kwargs):
        self.vm_session_create_calls.append(kwargs)

    def vm_session_update_state(self, microvm_id, state):
        self.vm_session_update_state_calls.append((microvm_id, state))

    def vm_session_get(self, microvm_id):
        return None


def _patch_module(storage_fake):
    orig_storage = mm_mod.storage
    orig_async_client = httpx.AsyncClient
    orig_image_arn = mm_mod.IMAGE_ARN
    mm_mod.storage = storage_fake
    httpx.AsyncClient = _FakeAsyncClient
    mm_mod.IMAGE_ARN = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

    def _restore():
        mm_mod.storage = orig_storage
        httpx.AsyncClient = orig_async_client
        mm_mod.IMAGE_ARN = orig_image_arn

    return _restore


def _spy(calls_list, return_value=None, raises=None):
    async def _fn(*args, **kwargs):
        calls_list.append((args, kwargs))
        if raises is not None:
            raise raises
        return return_value
    return _fn


def _build_manager(storage_fake):
    restore = _patch_module(storage_fake)
    manager = MicrovmManager()

    calls = {"launch_for_rotation": [], "wait_healthy": [], "restore_state": [],
             "push_catalog": [], "terminate": []}

    def _fake_launch_for_rotation(**kwargs):
        calls["launch_for_rotation"].append(kwargs)
        vm_id = "rotation-vm-1"
        manager.active_microvms[vm_id] = {"memory_mib": kwargs.get("memory_mib"),
                                           "name": kwargs.get("notebook_name")}
        return vm_id, "rotation-endpoint-1"

    manager.launch_for_rotation = _fake_launch_for_rotation
    manager._wait_for_healthy_async = _spy(calls["wait_healthy"])
    manager._restore_state_async = _spy(calls["restore_state"])
    manager._push_catalog_async = _spy(calls["push_catalog"])

    def _fake_terminate(vm_id):
        calls["terminate"].append(vm_id)

    manager.terminate = _fake_terminate

    return manager, calls, restore


# ── Section 1: POOL_ENABLED=true, saved idle=300, grant unreachable -> fixed 60 wins ──
def test_pool_enabled_recovery_forces_fixed_idle():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(409, {"error": "POOL_EMPTY"})

        try:
            session_id = "sess-recover-freeze-pooled"
            current = {"vm_id": "old-vm-pooled"}
            manager.active_microvms["old-vm-pooled"] = {
                "memory_mib": 2048, "idle_timeout_sec": 300,
                "name": "nb-recover-pooled", "architecture": "arm64",
            }
            run_async(manager._do_recover(session_id, current))
        finally:
            restore()

        check("pooled recovery: falls through to launch_for_rotation",
              len(calls["launch_for_rotation"]) == 1)
        idle_passed = calls["launch_for_rotation"][0].get("idle_timeout_sec")
        check("pooled recovery: launch_for_rotation called with FIXED idle (60), not saved (300)",
              idle_passed == 60)


# ── Section 2 (regression): POOL_ENABLED=false, saved idle=300 -> saved value wins ──
def test_pool_disabled_recovery_keeps_saved_idle():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        post_called = {"count": 0}

        def _behavior(url, json):
            post_called["count"] += 1
            return _FakeResponse(200, {"vm_id": "x", "endpoint": "y"})
        _FakeAsyncClient.behavior = _behavior

        try:
            session_id = "sess-recover-freeze-nonpooled"
            current = {"vm_id": "old-vm-nonpooled"}
            manager.active_microvms["old-vm-nonpooled"] = {
                "memory_mib": 2048, "idle_timeout_sec": 300,
                "name": "nb-recover-nonpooled", "architecture": "arm64",
            }
            run_async(manager._do_recover(session_id, current))
        finally:
            restore()

        check("non-pooled recovery: no HTTP grant call attempted", post_called["count"] == 0)
        check("non-pooled recovery: launch_for_rotation IS called",
              len(calls["launch_for_rotation"]) == 1)
        idle_passed = calls["launch_for_rotation"][0].get("idle_timeout_sec")
        check("non-pooled recovery: launch_for_rotation called with saved idle (300), unchanged",
              idle_passed == 300)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Proxy _do_recover pool-freeze (idle-timeout) ===")
    print("\n-- POOL_ENABLED=true: fixed idle wins over saved --")
    test_pool_enabled_recovery_forces_fixed_idle()
    print("\n-- POOL_ENABLED=false (regression): saved idle wins, unchanged --")
    test_pool_disabled_recovery_keeps_saved_idle()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY RECOVER POOL-FREEZE CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_recover_pool_freeze():
    _checks.clear()
    test_pool_enabled_recovery_forces_fixed_idle()
    test_pool_disabled_recovery_keeps_saved_idle()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "proxy recover pool-freeze failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
