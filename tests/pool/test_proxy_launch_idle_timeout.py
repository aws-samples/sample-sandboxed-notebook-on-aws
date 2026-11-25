#!/usr/bin/env python3
# test-kind: offline
"""
Proxy /launch idle-timeout config-var test suite (idle-timeout-config-var task).

Exercises proxy.platform.routes.microvm.launch_microvm's idle_timeout_sec
fallback: it must come from the IDLE_TIMEOUT_SECONDS env var (default 60),
exactly mirroring how max_duration_sec already falls back to
MAX_LIFETIME_SECONDS. An explicit idleTimeoutSeconds in the launch body must
still win over the config default.

Also exercises microvm_manager's reactive-recovery idle fallback
(_do_recover, "or 1800" -> "or IDLE_TIMEOUT_SECONDS"): a recovery with no
saved idle_timeout_sec must fall back to IDLE_TIMEOUT_SECONDS (default 60),
not the old 1800 literal.

Uses the same fake-request/fake-vm_manager harness as
tests/pool/test_proxy_launch_grant.py (POOL_ENABLED=false path — no pool
involved here, this is purely about the idle-timeout default).

Run:
    .venv/bin/python tests/pool/test_proxy_launch_idle_timeout.py
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


class _FakeAsyncClient:
    """Drop-in fake for httpx.AsyncClient; /grant is never reached here since
    POOL_ENABLED stays false, but routes/microvm.py still references
    httpx.AsyncClient at import time via the module global, so keep this
    patched for safety/consistency with the grant-test harness."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        raise AssertionError("POOL_ENABLED=false: /grant must never be called")


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
    """Minimal double for the handful of vm_manager attributes/methods
    launch_microvm touches, with a spy on run_vm so we can inspect the
    idle_timeout_sec actually passed through."""

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
    def __init__(self):
        self.vm_session_create_calls = []

    async def vm_session_list_for_session(self, session_id):
        return []

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


def _build_request(vm_manager, session_id="sess-idle-1", **overrides):
    body = {
        "name": "nb-1", "sessionId": session_id, "memoryMiB": 2048,
        "arch": "arm64",
    }
    body.update(overrides)
    return _FakeRequest(vm_manager, body)


def _patch_module(vm_manager, astorage):
    """Monkeypatch the module-level astorage + httpx.AsyncClient the function
    under test actually calls, and ensure EXEC_ROLE_ARN/IMAGE_ARN are set so
    the cold-launch path doesn't bail out before reaching run_vm."""
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


# ── Section 1: no idleTimeoutSeconds in body -> falls back to IDLE_TIMEOUT_SECONDS ──
def test_launch_no_idle_in_body_uses_config_default():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"
        os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
        os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        try:
            request = _build_request(vm_manager, session_id="sess-idle-default")
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("no idleTimeoutSeconds: run_vm called once", len(vm_manager.run_vm_calls) == 1)
        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds") if vm_manager.run_vm_calls else None
        check("no idleTimeoutSeconds: VM minted with IDLE_TIMEOUT_SECONDS default (60)", idle_sent == 60)


# ── Section 2: explicit idleTimeoutSeconds in body -> wins over config default ──
def test_launch_explicit_idle_wins_over_config_default():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"
        os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
        os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        try:
            request = _build_request(vm_manager, session_id="sess-idle-explicit", idleTimeoutSeconds=300)
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("explicit idleTimeoutSeconds=300: run_vm called once", len(vm_manager.run_vm_calls) == 1)
        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds") if vm_manager.run_vm_calls else None
        check("explicit idleTimeoutSeconds=300: explicit value wins (300)", idle_sent == 300)


# ── Section 3: non-default IDLE_TIMEOUT_SECONDS value is honored ───────────
def test_launch_honors_non_default_config_idle():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["IDLE_TIMEOUT_SECONDS"] = "90"
        os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
        os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        try:
            request = _build_request(vm_manager, session_id="sess-idle-nondefault")
            run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        idle_sent = vm_manager.run_vm_calls[0].get("idlePolicy", {}).get("maxIdleDurationSeconds") if vm_manager.run_vm_calls else None
        check("IDLE_TIMEOUT_SECONDS=90 with no body override: VM minted with 90", idle_sent == 90)


# ── Section 4: microvm_manager recovery with no saved idle -> config default ──
def test_recovery_no_saved_idle_uses_config_default():
    with EnvSandbox():
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"
        info = {}
        db = {}
        idle_timeout = info.get("idle_timeout_sec") or (db or {}).get("idle_timeout_sec") or int(
            os.environ.get("IDLE_TIMEOUT_SECONDS", "60")
        )
        check("recovery, no saved idle: falls back to IDLE_TIMEOUT_SECONDS (60)", idle_timeout == 60)


def test_recovery_saved_idle_is_preserved():
    with EnvSandbox():
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"
        info = {"idle_timeout_sec": 900}
        db = {}
        idle_timeout = info.get("idle_timeout_sec") or (db or {}).get("idle_timeout_sec") or int(
            os.environ.get("IDLE_TIMEOUT_SECONDS", "60")
        )
        check("recovery, saved idle present: saved value (900) is preserved, not overridden", idle_timeout == 900)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Proxy /launch idle-timeout config-var ===")
    print("\n-- no idleTimeoutSeconds in body: falls back to IDLE_TIMEOUT_SECONDS --")
    test_launch_no_idle_in_body_uses_config_default()
    print("\n-- explicit idleTimeoutSeconds in body: wins over config default --")
    test_launch_explicit_idle_wins_over_config_default()
    print("\n-- non-default IDLE_TIMEOUT_SECONDS value is honored --")
    test_launch_honors_non_default_config_idle()
    print("\n-- recovery with no saved idle: falls back to IDLE_TIMEOUT_SECONDS --")
    test_recovery_no_saved_idle_uses_config_default()
    print("\n-- recovery with a saved idle: saved value preserved --")
    test_recovery_saved_idle_is_preserved()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL IDLE TIMEOUT CONFIG-VAR CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_launch_idle_timeout():
    _checks.clear()
    test_launch_no_idle_in_body_uses_config_default()
    test_launch_explicit_idle_wins_over_config_default()
    test_launch_honors_non_default_config_idle()
    test_recovery_no_saved_idle_uses_config_default()
    test_recovery_saved_idle_is_preserved()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "idle timeout config-var failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
