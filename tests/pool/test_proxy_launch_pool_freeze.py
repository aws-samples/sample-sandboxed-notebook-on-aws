#!/usr/bin/env python3
# test-kind: offline
"""
Proxy /launch pool-freeze test suite (pool-freeze-lifecycle-attrs task).

Exercises proxy.platform.routes.microvm.launch_microvm's lifecycle-attribute
freeze: when POOL_ENABLED is true, the two immutable-at-launch VM attributes
(idleTimeoutSeconds / maxDurationSeconds) are FORCED to the config values
(IDLE_TIMEOUT_SECONDS / MAX_LIFETIME_SECONDS) and the client-sent values are
IGNORED — on BOTH the pool-grant path and the cold-launch fallback path, and
the freeze wins over a SAVED value on restore too. POOL_ENABLED=false leaves
all of this byte-for-byte unchanged (client/saved values win, as today).

Uses the same fake-request/fake-vm_manager harness as
tests/pool/test_proxy_launch_grant.py.

Run:
    .venv/bin/python tests/pool/test_proxy_launch_pool_freeze.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402

# ── tiny test harness (matches tests/pool/test_proxy_launch_grant.py convention) ──
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


class EnvSandbox:
    """Snapshot os.environ on entry, restore on exit."""

    def __enter__(self):
        self._snapshot = dict(os.environ)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._snapshot)


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    """Drop-in fake for httpx.AsyncClient, used as an async context manager.
    `post` behavior is injected via a class-level callable set per-test."""

    behavior = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        return _FakeAsyncClient.behavior(url, json)


class _FakeCostTracker:
    def __init__(self):
        self.records = []
        self.prior_suspended_calls = []

    def record(self, microvm_id, state, memory_mib=None, architecture=None):
        self.records.append((microvm_id, state, memory_mib, architecture))

    def record_prior_suspended(self, microvm_id, suspended_since_epoch,
                               memory_mib=None, architecture=None):
        self.prior_suspended_calls.append(
            (microvm_id, suspended_since_epoch, memory_mib, architecture))


class _FakeVmManager:
    def __init__(self):
        self.active_microvms = {}
        self.cost_tracker = _FakeCostTracker()
        self._session_registry = {}
        self.run_vm_calls = []
        self.register_session_calls = []
        self.unregister_session_calls = []

        class _FakeTokenCache:
            async def delete(self, vm_id):
                pass
        self.token_cache = _FakeTokenCache()

    def get_session_vm(self, session_id):
        return self._session_registry.get(session_id)

    async def get_session_vm_async(self, session_id):
        return self._session_registry.get(session_id)

    def register_session(self, session_id, vm_id, endpoint):
        self.register_session_calls.append((session_id, vm_id, endpoint))
        self._session_registry[session_id] = {"vm_id": vm_id, "endpoint": endpoint}

    async def register_session_async(self, session_id, vm_id, endpoint):
        self.register_session(session_id, vm_id, endpoint)

    def unregister_session(self, session_id):
        self.unregister_session_calls.append(session_id)
        self._session_registry.pop(session_id, None)

    def mark_session_launching(self, session_id):
        pass

    def clear_session_launching(self, session_id):
        pass

    async def get_vm_state(self, vm_id):
        return "RUNNING"

    def is_session_launching(self, session_id):
        return False

    async def build_cloud_catalog_entries_async(self, session_id=None):
        return []

    def get_artifacts_bucket(self):
        return "fake-bucket"

    def stage_catalog_to_s3(self, session_id, catalog_entries):
        return None

    def launch_env_vars(self):
        return {}

    async def run_vm(self, **params):
        self.run_vm_calls.append(params)
        return {"microvmId": "cold-vm-1", "endpoint": "cold-endpoint-1"}

    async def get_microvm_detail(self, microvm_id):
        return {"state": "RUNNING"}

    async def get_microvm_detail_launch(self, microvm_id):
        return {"state": "RUNNING"}

    async def terminate_vm(self, microvm_id):
        pass


class _FakeAStorage:
    def __init__(self, prior_sessions=None):
        self.vm_session_create_calls = []
        self._prior_sessions = prior_sessions or {}

    async def vm_session_list_for_session(self, session_id):
        return self._prior_sessions.get(session_id, [])

    async def notebook_get(self, notebook_id):
        return None

    async def notebook_env_get(self, notebook_id):
        return {}

    async def vm_session_create(self, **kwargs):
        self.vm_session_create_calls.append(kwargs)

    async def vm_session_mark_terminated(self, microvm_id):
        pass


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — launch_microvm only calls
    request.app.state.vm_manager, request.json(), and request.body()."""

    def __init__(self, vm_manager, body: dict):
        self._body = body

        class _State:
            pass
        state = _State()
        state.vm_manager = vm_manager

        class _App:
            pass
        app = _App()
        app.state = state
        self.app = app

    async def body(self):
        import json as _json
        return _json.dumps(self._body).encode()

    async def json(self):
        return self._body


def _build_request(vm_manager, session_id="sess-pool-freeze-1", **overrides):
    body = {
        "name": "nb-1", "sessionId": session_id, "memoryMiB": 2048,
        "arch": "arm64",
    }
    body.update(overrides)
    return _FakeRequest(vm_manager, body)


def _patch_module(vm_manager, astorage):
    orig_astorage = microvm_mod.astorage
    orig_async_client = httpx.AsyncClient
    orig_exec_role_arn = microvm_mod.EXEC_ROLE_ARN
    orig_image_arn = microvm_mod.IMAGE_ARN
    microvm_mod.astorage = astorage
    httpx.AsyncClient = _FakeAsyncClient
    microvm_mod.EXEC_ROLE_ARN = "arn:aws:iam::123456789012:role/exec"
    microvm_mod.IMAGE_ARN = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

    def _restore():
        microvm_mod.astorage = orig_astorage
        httpx.AsyncClient = orig_async_client
        microvm_mod.EXEC_ROLE_ARN = orig_exec_role_arn
        microvm_mod.IMAGE_ARN = orig_image_arn

    return _restore


_BASE_ENV = {
    "MICROVM_EXEC_ROLE_ARN": "arn:aws:iam::123456789012:role/exec",
    "MICROVM_IMAGE_ARN": "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb",
    "IDLE_TIMEOUT_SECONDS": "60",
    "MAX_LIFETIME_SECONDS": "28800",
}


# ── Section 1: POOL_ENABLED=true, cold-launch fallback (grant unreachable) ──
def test_pool_enabled_cold_launch_fallback_forces_fixed_values():
    with EnvSandbox():
        os.environ.update(_BASE_ENV)
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(409, {"error": "POOL_EMPTY"})

        try:
            request = _build_request(
                vm_manager, session_id="sess-freeze-fallback",
                idleTimeoutSeconds=300, maxDurationSeconds=3600,
            )
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("cold-launch fallback: run_vm IS called", len(vm_manager.run_vm_calls) == 1)
        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds")
        max_sent = vm_manager.run_vm_calls[0].get("maximumDurationInSeconds")
        check("cold-launch fallback: idle FORCED to IDLE_TIMEOUT_SECONDS (60), client's 300 ignored",
              idle_sent == 60)
        check("cold-launch fallback: max FORCED to MAX_LIFETIME_SECONDS (28800), client's 3600 ignored",
              max_sent == 28800)


# ── Section 2: POOL_ENABLED=true, grant succeeds (200) ─────────────────────
def test_pool_enabled_grant_path_forces_fixed_values():
    with EnvSandbox():
        os.environ.update(_BASE_ENV)
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(
            200, {"vm_id": "pool-vm-freeze", "endpoint": "pool-endpoint-freeze"}
        )

        try:
            request = _build_request(
                vm_manager, session_id="sess-freeze-grant",
                idleTimeoutSeconds=300, maxDurationSeconds=3600,
            )
            result = run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("grant path: result is the granted VM",
              isinstance(result, dict) and result.get("microvmId") == "pool-vm-freeze")
        committed = vm_manager.active_microvms.get("pool-vm-freeze", {})
        check("grant path: active_microvms idle_timeout_sec FORCED to 60 (client's 300 ignored)",
              committed.get("idle_timeout_sec") == 60)
        check("grant path: active_microvms max_duration_sec FORCED to 28800 (client's 3600 ignored)",
              committed.get("max_duration_sec") == 28800)
        check("grant path: vm_session_create was called exactly once",
              len(astorage.vm_session_create_calls) == 1)
        created = astorage.vm_session_create_calls[0]
        check("grant path: vm_session_create idle_timeout_sec FORCED to 60",
              created.get("idle_timeout_sec") == 60)
        check("grant path: vm_session_create max_duration_sec FORCED to 28800",
              created.get("max_duration_sec") == 28800)


# ── Section 3: POOL_ENABLED=true, restore with a SAVED idle/max different from fixed ──
def test_pool_enabled_restore_fixed_wins_over_saved():
    with EnvSandbox():
        os.environ.update(_BASE_ENV)
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"

        vm_manager = _FakeVmManager()
        prior_sessions = {
            "sess-freeze-restore": [{
                "architecture": "arm64", "memory_mib": 2048,
                "idle_timeout_sec": 900, "max_duration_sec": 7200,
            }]
        }
        astorage = _FakeAStorage(prior_sessions=prior_sessions)
        restore = _patch_module(vm_manager, astorage)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(409, {"error": "POOL_EMPTY"})

        try:
            request = _build_request(
                vm_manager, session_id="sess-freeze-restore",
                restoreFromSession="sess-freeze-restore",
            )
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("restore: run_vm IS called (cold-launch fallback)", len(vm_manager.run_vm_calls) == 1)
        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds")
        max_sent = vm_manager.run_vm_calls[0].get("maximumDurationInSeconds")
        check("restore: FIXED idle (60) wins over saved idle (900)", idle_sent == 60)
        check("restore: FIXED max (28800) wins over saved max (7200)", max_sent == 28800)


# ── Section 4 (regression): POOL_ENABLED=false -> client/saved values win ──
def test_pool_disabled_client_values_honored_cold_launch():
    with EnvSandbox():
        os.environ.update(_BASE_ENV)
        os.environ["POOL_ENABLED"] = "false"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)

        try:
            request = _build_request(
                vm_manager, session_id="sess-nofreeze-fallback",
                idleTimeoutSeconds=300, maxDurationSeconds=3600,
            )
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds")
        max_sent = vm_manager.run_vm_calls[0].get("maximumDurationInSeconds")
        check("POOL_ENABLED=false: client idle (300) honored, unchanged", idle_sent == 300)
        check("POOL_ENABLED=false: client max (3600) honored, unchanged", max_sent == 3600)


def test_pool_disabled_restore_saved_values_win():
    with EnvSandbox():
        os.environ.update(_BASE_ENV)
        os.environ["POOL_ENABLED"] = "false"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"

        vm_manager = _FakeVmManager()
        prior_sessions = {
            "sess-nofreeze-restore": [{
                "architecture": "arm64", "memory_mib": 2048,
                "idle_timeout_sec": 900, "max_duration_sec": 7200,
            }]
        }
        astorage = _FakeAStorage(prior_sessions=prior_sessions)
        restore = _patch_module(vm_manager, astorage)

        try:
            request = _build_request(
                vm_manager, session_id="sess-nofreeze-restore",
                restoreFromSession="sess-nofreeze-restore",
            )
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds")
        max_sent = vm_manager.run_vm_calls[0].get("maximumDurationInSeconds")
        check("POOL_ENABLED=false: saved idle (900) wins, unchanged", idle_sent == 900)
        check("POOL_ENABLED=false: saved max (7200) wins, unchanged", max_sent == 7200)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Proxy /launch pool-freeze (idle/max-duration) ===")
    print("\n-- POOL_ENABLED=true, cold-launch fallback: fixed values forced --")
    test_pool_enabled_cold_launch_fallback_forces_fixed_values()
    print("\n-- POOL_ENABLED=true, grant path: fixed values forced --")
    test_pool_enabled_grant_path_forces_fixed_values()
    print("\n-- POOL_ENABLED=true, restore: fixed wins over saved --")
    test_pool_enabled_restore_fixed_wins_over_saved()
    print("\n-- POOL_ENABLED=false (regression): client values win, cold-launch --")
    test_pool_disabled_client_values_honored_cold_launch()
    print("\n-- POOL_ENABLED=false (regression): saved values win, restore --")
    test_pool_disabled_restore_saved_values_win()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY LAUNCH POOL-FREEZE CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_launch_pool_freeze():
    _checks.clear()
    test_pool_enabled_cold_launch_fallback_forces_fixed_values()
    test_pool_enabled_grant_path_forces_fixed_values()
    test_pool_enabled_restore_fixed_wins_over_saved()
    test_pool_disabled_client_values_honored_cold_launch()
    test_pool_disabled_restore_saved_values_win()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "proxy launch pool-freeze failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
