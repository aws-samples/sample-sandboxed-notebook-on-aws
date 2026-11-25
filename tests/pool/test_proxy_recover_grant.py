#!/usr/bin/env python3
# test-kind: offline
"""
Proxy `_do_recover` grant-then-fallback test suite (Phase 5 —
pool-phase45-config-proxy, plan.md item 6).

Exercises MicrovmManager._do_recover's gated pool-grant-with-restore attempt
(design §10, §13.4 Option A): POOL_ENABLED gates whether /grant is even
attempted; a 200 adopts the already-restored VM and SKIPS
launch_for_rotation + _wait_for_healthy_async + _restore_state_async entirely
(no double-restore); a 409/error ALWAYS falls through to the existing
launch_for_rotation path, unchanged.

Mocks the pool-manager HTTP endpoint by monkeypatching
proxy.platform.microvm_manager.httpx.AsyncClient with a tiny fake client/
response. Uses a REAL MicrovmManager instance (plain Python object, no
network at construction) with launch_for_rotation/_wait_for_healthy_async/
_restore_state_async/_push_catalog_async/terminate replaced by spies, and
proxy.platform.microvm_manager.storage monkeypatched to a fake in-memory
storage (no real DB).

Run:
    .venv/bin/python tests/pool/test_proxy_recover_grant.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.microvm_manager as mm_mod  # noqa: E402
from proxy.platform.microvm_manager import MicrovmManager  # noqa: E402

# ── tiny test harness (matches tests/pool/test_pool_grant.py convention) ───
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


# ── fakes ────────────────────────────────────────────────────────────────────

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
    """Minimal in-memory stand-in for proxy.storage.storage — only the methods
    _do_recover's grant-success path touches."""

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
    """IMAGE_ARN is a module-level constant captured at import time (not read
    from os.environ per-call) and _do_recover bails out early if it's unset —
    before the grant attempt even runs — so it must be monkeypatched directly
    on the module rather than via EnvSandbox."""
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
    """A real MicrovmManager with the heavy launch/restore sub-steps replaced
    by spies, so a grant miss/error can still exercise the EXISTING
    launch_for_rotation path without actually touching AWS."""
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


# ── Section 1: POOL_ENABLED=true + grant 200 -> adopts, skips launch+restore ─
def test_pool_enabled_200_adopts_skips_launch_and_restore():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)

        def _behavior(url, json):
            check("grant POST goes to {POOL_MANAGER_URL}/grant", url == "http://localhost:8090/grant")
            check("grant payload has restore_from == session_id",
                  json.get("restore_from") == "sess-recover-1")
            return _FakeResponse(200, {"vm_id": "pool-vm-recovered", "endpoint": "pool-endpoint-recovered"})
        _FakeAsyncClient.behavior = _behavior

        try:
            session_id = "sess-recover-1"
            current = {"vm_id": "old-vm-1"}
            manager.active_microvms["old-vm-1"] = {"memory_mib": 2048, "idle_timeout_sec": 1800,
                                                    "name": "nb-recover", "architecture": "arm64"}
            result = run_async(manager._do_recover(session_id, current))
        finally:
            restore()

        check("_do_recover returns the granted vm_id/endpoint",
              result == {"vm_id": "pool-vm-recovered", "endpoint": "pool-endpoint-recovered"})
        check("launch_for_rotation is NOT called (pool already provisioned)",
              len(calls["launch_for_rotation"]) == 0)
        check("_wait_for_healthy_async is NOT called (pool already resumed+healthy)",
              len(calls["wait_healthy"]) == 0)
        check("_restore_state_async is NOT called (pool already restored — no double-restore)",
              len(calls["restore_state"]) == 0)
        check("register_session landed in the DB-backed registry",
              storage_fake._registry.get(session_id, {}).get("vm_id") == "pool-vm-recovered")
        check("vm_session_create was called exactly once for the granted VM",
              len(storage_fake.vm_session_create_calls) == 1
              and storage_fake.vm_session_create_calls[0]["microvm_id"] == "pool-vm-recovered")
        check("the old VM was marked TERMINATED in storage",
              ("old-vm-1", "TERMINATED") in storage_fake.vm_session_update_state_calls)
        check("the old VM was terminated via the backend",
              "old-vm-1" in calls["terminate"])


# ── Section 2: POOL_ENABLED=true + grant 409 -> falls through, unchanged ───
def test_pool_enabled_409_falls_through_to_launch_for_rotation():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(409, {"error": "POOL_EMPTY"})

        try:
            session_id = "sess-recover-409"
            current = {"vm_id": "old-vm-2"}
            manager.active_microvms["old-vm-2"] = {"memory_mib": 2048, "idle_timeout_sec": 1800,
                                                    "name": "nb-409", "architecture": "arm64"}
            result = run_async(manager._do_recover(session_id, current))
        finally:
            restore()

        check("409 POOL_EMPTY: _do_recover falls through to launch_for_rotation",
              len(calls["launch_for_rotation"]) == 1)
        check("409 POOL_EMPTY: _wait_for_healthy_async IS called (existing path exercised)",
              len(calls["wait_healthy"]) == 1)
        check("409 POOL_EMPTY: _restore_state_async IS called (existing path exercised)",
              len(calls["restore_state"]) == 1)
        check("409 POOL_EMPTY: _do_recover returns the rotation VM",
              result == {"vm_id": "rotation-vm-1", "endpoint": "rotation-endpoint-1"})


def test_pool_enabled_grant_raises_falls_through_to_launch_for_rotation():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)

        def _raise(url, json):
            raise httpx.ConnectError("refused")
        _FakeAsyncClient.behavior = _raise

        try:
            session_id = "sess-recover-err"
            current = {"vm_id": "old-vm-3"}
            manager.active_microvms["old-vm-3"] = {"memory_mib": 2048, "idle_timeout_sec": 1800,
                                                    "name": "nb-err", "architecture": "arm64"}
            raised = None
            try:
                result = run_async(manager._do_recover(session_id, current))
            except Exception as e:
                raised = e
        finally:
            restore()

        check("grant connection error: _do_recover never raises out", raised is None)
        check("grant connection error: falls through to launch_for_rotation",
              len(calls["launch_for_rotation"]) == 1)
        check("grant connection error: _do_recover returns the rotation VM",
              result == {"vm_id": "rotation-vm-1", "endpoint": "rotation-endpoint-1"})


# ── Section 3: POOL_ENABLED=false -> no HTTP call, launch_for_rotation used ─
def test_pool_disabled_no_grant_call():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        post_called = {"count": 0}

        def _behavior(url, json):
            post_called["count"] += 1
            return _FakeResponse(200, {"vm_id": "x", "endpoint": "y"})
        _FakeAsyncClient.behavior = _behavior

        try:
            session_id = "sess-recover-disabled"
            current = {"vm_id": "old-vm-4"}
            manager.active_microvms["old-vm-4"] = {"memory_mib": 2048, "idle_timeout_sec": 1800,
                                                    "name": "nb-disabled", "architecture": "arm64"}
            result = run_async(manager._do_recover(session_id, current))
        finally:
            restore()

        check("POOL_ENABLED=false: no HTTP call attempted at all", post_called["count"] == 0)
        check("POOL_ENABLED=false: launch_for_rotation IS called",
              len(calls["launch_for_rotation"]) == 1)
        check("POOL_ENABLED=false: _do_recover returns the rotation VM",
              result == {"vm_id": "rotation-vm-1", "endpoint": "rotation-endpoint-1"})


# ── Section 4: recovering an x86_64 VM asks the pool for an x86_64 grant ───
def test_x86_recover_requests_x86_grant():
    """A notebook keeps the arch it ran on across a recover: the grant body
    must carry the dead VM's x86_64 arch, never the arm64 default."""
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"

        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        seen = []

        def _behavior(url, json):
            seen.append(json)
            return _FakeResponse(200, {"vm_id": "pool-vm-x86", "endpoint": "pool-endpoint-x86"})
        _FakeAsyncClient.behavior = _behavior

        try:
            current = {"vm_id": "old-vm-x86"}
            manager.active_microvms["old-vm-x86"] = {"memory_mib": 2048, "idle_timeout_sec": 1800,
                                                      "name": "nb-x86", "architecture": "x86_64"}
            result = run_async(manager._do_recover("sess-recover-x86", current))
        finally:
            restore()

        check("x86 recover: exactly one grant request was made", len(seen) == 1)
        check("x86 recover: the grant body asks for arch x86_64 (size 2048)",
              bool(seen) and seen[0].get("arch") == "x86_64" and seen[0].get("size") == 2048)
        check("x86 recover: _do_recover adopts the granted VM",
              result == {"vm_id": "pool-vm-x86", "endpoint": "pool-endpoint-x86"})
        created = storage_fake.vm_session_create_calls
        check("x86 recover: the adopted VM's session row is recorded as x86_64",
              len(created) == 1 and created[0].get("architecture") == "x86_64")


# ── Section 5: recovery keeps the spare's mint time and seeds its cost ─────
def test_recover_grant_keeps_mint_time_and_seed_cost():
    """code-review B-B-07: like the /launch pool-grant path, a recovered pool VM's
    launched_at is the pool's MINT time (not adoption), passed to the DB as
    launched_at + prior_suspended_since, stored as epoch in active_microvms, and
    the mint->grant SUSPENDED interval is seeded into the cost tracker."""
    import time as _time
    from datetime import datetime, timezone
    mint_epoch = int(_time.time()) - 3600
    mint_iso = datetime.fromtimestamp(mint_epoch, tz=timezone.utc).isoformat()
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(
            200, {"vm_id": "pool-vm-mint", "endpoint": "pool-endpoint-mint", "launched_at": mint_iso})
        try:
            manager.active_microvms["old-vm-mint"] = {"memory_mib": 2048, "idle_timeout_sec": 1800,
                                                       "name": "nb-mint", "architecture": "arm64"}
            result = run_async(manager._do_recover("sess-recover-mint", {"vm_id": "old-vm-mint"}))
        finally:
            restore()
        check("mint: _do_recover adopts the granted VM",
              result == {"vm_id": "pool-vm-mint", "endpoint": "pool-endpoint-mint"})
        created = storage_fake.vm_session_create_calls
        check("mint: vm_session_create gets launched_at = pool mint time (ISO)",
              len(created) == 1 and created[0].get("launched_at") == mint_iso)
        check("mint: vm_session_create gets prior_suspended_since = pool mint time",
              len(created) == 1 and created[0].get("prior_suspended_since") == mint_iso)
        entry = manager.active_microvms.get("pool-vm-mint", {})
        check("mint: active_microvms launched_at is the mint epoch",
              abs((entry.get("launched_at") or 0) - mint_epoch) < 1)
        check("mint: active_microvms carries the tier memory", entry.get("memory_mib") == 2048)
        cost = manager.cost_tracker.get_cost("pool-vm-mint")
        check("mint: pool-residency SUSPENDED time seeded as cost (~1 h)",
              cost is not None and cost.get("suspended_secs", 0) >= 3500)

    # An older pool that returns no launched_at: DB defaults to now, epoch is now.
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        storage_fake = _FakeStorage()
        manager, calls, restore = _build_manager(storage_fake)
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(
            200, {"vm_id": "pool-vm-old", "endpoint": "pool-endpoint-old"})
        try:
            manager.active_microvms["old-vm-old"] = {"memory_mib": 2048, "architecture": "arm64"}
            run_async(manager._do_recover("sess-recover-old", {"vm_id": "old-vm-old"}))
        finally:
            restore()
        created = storage_fake.vm_session_create_calls
        check("no mint time: launched_at/prior_suspended_since passed as None (DB uses now)",
              len(created) == 1 and created[0].get("launched_at") is None
              and created[0].get("prior_suspended_since") is None)
        check("no mint time: active_microvms launched_at is ~now",
              abs(manager.active_microvms["pool-vm-old"]["launched_at"] - _time.time()) < 5)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Proxy _do_recover grant-then-fallback ===")
    print("\n-- POOL_ENABLED=true + 200: adopts, skips launch+restore (no double-restore) --")
    test_pool_enabled_200_adopts_skips_launch_and_restore()
    print("\n-- POOL_ENABLED=true + 409 POOL_EMPTY: falls through --")
    test_pool_enabled_409_falls_through_to_launch_for_rotation()
    print("\n-- POOL_ENABLED=true + connection error: falls through, never raises --")
    test_pool_enabled_grant_raises_falls_through_to_launch_for_rotation()
    print("\n-- POOL_ENABLED=false: no HTTP call, launch_for_rotation used --")
    test_pool_disabled_no_grant_call()
    print("\n-- x86_64 VM: recover requests an x86_64 grant --")
    test_x86_recover_requests_x86_grant()
    print("\n-- pool grant: recovery keeps mint time and seeds pool-residency cost --")
    test_recover_grant_keeps_mint_time_and_seed_cost()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY RECOVER GRANT CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_recover_grant():
    _checks.clear()
    test_pool_enabled_200_adopts_skips_launch_and_restore()
    test_pool_enabled_409_falls_through_to_launch_for_rotation()
    test_pool_enabled_grant_raises_falls_through_to_launch_for_rotation()
    test_pool_disabled_no_grant_call()
    test_x86_recover_requests_x86_grant()
    test_recover_grant_keeps_mint_time_and_seed_cost()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "proxy recover grant failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
