#!/usr/bin/env python3
# test-kind: offline
"""
Proxy /launch grant-then-fallback test suite (Phase 5 — pool-phase45-config-
proxy, plan.md item 5).

Exercises proxy.platform.routes.microvm.launch_microvm's gated pool-grant
attempt (design §10, §13.4 Option A): POOL_ENABLED gates whether /grant is
even attempted; a 200 adopts the granted VM (no cold launch, no re-run/
re-restore); a 409/5xx/timeout/connection-error ALWAYS falls through to the
existing cold-launch path, never raising.

Mocks the pool-manager HTTP endpoint by monkeypatching
proxy.platform.routes.microvm.httpx.AsyncClient with a tiny fake client/
response — no running pool_manager, no AWS, no real DB (astorage/storage
calls are monkeypatched to no-ops that record what they were given).

Run:
    .venv/bin/python tests/pool/test_proxy_launch_grant.py
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402

# ── tiny test harness (matches tests/pool/test_pool_grant.py convention) ───
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


class EnvSandbox:
    """Snapshot os.environ on entry, restore on exit."""

    _KEYS = ("POOL_ENABLED", "POOL_MANAGER_URL", "MICROVM_EXEC_ROLE_ARN", "MICROVM_IMAGE_ARN")

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

    behavior = None  # callable(url, json) -> _FakeResponse, or raises

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
    """Minimal double for the handful of vm_manager attributes/methods
    launch_microvm touches, with spies to assert which paths were taken."""

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


def _build_request(vm_manager, session_id="sess-launch-grant-1", **overrides):
    body = {
        "name": "nb-1", "sessionId": session_id, "memoryMiB": 2048,
        "arch": "arm64",
    }
    body.update(overrides)
    return _FakeRequest(vm_manager, body)


# ── test setup/teardown helpers ─────────────────────────────────────────────

def _patch_module(vm_manager, astorage):
    """Monkeypatch the module-level astorage + httpx.AsyncClient the function
    under test actually calls, and ensure EXEC_ROLE_ARN/IMAGE_ARN are set so a
    cold-launch attempt (fallback paths) doesn't bail out before reaching
    run_vm. EXEC_ROLE_ARN/IMAGE_ARN are module-level constants captured at
    import time (not read from os.environ per-call), so they must be
    monkeypatched directly on the module rather than via EnvSandbox."""
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


# ── Section 1: POOL_ENABLED=false -> no /grant call, cold-launch taken ─────
def test_pool_disabled_no_grant_call_cold_launch_taken():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
        os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        post_called = {"count": 0}

        def _behavior(url, json):
            post_called["count"] += 1
            return _FakeResponse(200, {"vm_id": "pool-vm-1", "endpoint": "pool-endpoint-1"})
        _FakeAsyncClient.behavior = _behavior

        try:
            request = _build_request(vm_manager, session_id="sess-disabled")
            result = run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("POOL_ENABLED=false: /grant is never attempted", post_called["count"] == 0)
        check("POOL_ENABLED=false: cold-launch run_vm IS called", len(vm_manager.run_vm_calls) == 1)
        check("POOL_ENABLED=false: result uses the cold-launch vm id",
              isinstance(result, dict) and result.get("microvmId") == "cold-vm-1")


# ── Section 2: POOL_ENABLED=true + 200 -> adopts, no run_vm, no re-restore ──
def test_pool_enabled_200_adopts_vm_skips_cold_launch():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
        os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)

        # A spare minted ~1h before this grant — the pool returns its true MINT
        # time as launched_at. The adopted VM must carry THIS, not the adopt moment.
        mint_iso = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()

        def _behavior(url, json):
            check("grant POST goes to {POOL_MANAGER_URL}/grant", url == "http://localhost:8090/grant")
            check("grant payload carries the real session_id",
                  json.get("session_id") == "sess-granted-1")
            return _FakeResponse(200, {
                "vm_id": "pool-vm-granted", "endpoint": "pool-endpoint-granted",
                "launched_at": mint_iso,
            })
        _FakeAsyncClient.behavior = _behavior

        adopt_epoch = time.time()
        try:
            request = _build_request(vm_manager, session_id="sess-granted-1")
            result = run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("POOL_ENABLED=true + 200: run_vm is NOT called", len(vm_manager.run_vm_calls) == 0)
        check("POOL_ENABLED=true + 200: register_session IS called with (session_id, vm_id, endpoint)",
              vm_manager.register_session_calls == [("sess-granted-1", "pool-vm-granted", "pool-endpoint-granted")])
        check("POOL_ENABLED=true + 200: returned dict has the granted microvmId",
              isinstance(result, dict) and result.get("microvmId") == "pool-vm-granted")
        check("POOL_ENABLED=true + 200: returned dict has the granted endpoint",
              isinstance(result, dict) and result.get("endpoint") == "pool-endpoint-granted")
        check("POOL_ENABLED=true + 200: returned dict status is 'running'",
              isinstance(result, dict) and result.get("status") == "running")
        check("POOL_ENABLED=true + 200: vm_session_create was called exactly once (no re-restore call)",
              len(astorage.vm_session_create_calls) == 1)
        check("POOL_ENABLED=true + 200: active_microvms registered under the granted vm_id",
              "pool-vm-granted" in vm_manager.active_microvms)

        # ── mint-time adoption: the adopted VM must carry the pool's MINT time,
        #    NOT the adopt moment (bug fix: 'Terminates in' must not over-report) ──
        created = astorage.vm_session_create_calls[0]
        check("POOL_ENABLED=true + 200: vm_session_create got the pool's ISO mint time (not now)",
              created.get("launched_at") == mint_iso)
        # The DB row must record the ISO mint string straight through.
        check("POOL_ENABLED=true + 200: persisted launched_at is clearly older than the adopt moment",
              created.get("launched_at") is not None
              and datetime.fromisoformat(created["launched_at"]).timestamp() < adopt_epoch - 1800)
        # The in-memory map stores an EPOCH FLOAT (parity with the cold path) and
        # it must equal the mint epoch, NOT ~adopt_epoch.
        in_mem = vm_manager.active_microvms["pool-vm-granted"]["launched_at"]
        mint_epoch = datetime.fromisoformat(mint_iso).timestamp()
        check("POOL_ENABLED=true + 200: in-memory launched_at is an epoch float equal to the mint time",
              isinstance(in_mem, float) and abs(in_mem - mint_epoch) < 1.0)
        check("POOL_ENABLED=true + 200: in-memory launched_at is ~1h before the adopt moment",
              adopt_epoch - in_mem > 1800)

        # ── pool-residency SUSPENDED cost inheritance (interpretation A): the
        #    grant path must pass the mint ISO as prior_suspended_since so the DB
        #    seeds a mint->grant SUSPENDED interval, and must seed the in-memory
        #    cost record via record_prior_suspended (NOT a plain RUNNING record). ──
        check("POOL_ENABLED=true + 200: vm_session_create got prior_suspended_since = pool mint ISO",
              created.get("prior_suspended_since") == mint_iso)
        check("POOL_ENABLED=true + 200: cost_tracker.record_prior_suspended called once for the granted VM",
              len(vm_manager.cost_tracker.prior_suspended_calls) == 1
              and vm_manager.cost_tracker.prior_suspended_calls[0][0] == "pool-vm-granted")
        check("POOL_ENABLED=true + 200: record_prior_suspended got the mint epoch (~1h before adopt)",
              abs(vm_manager.cost_tracker.prior_suspended_calls[0][1] - mint_epoch) < 1.0)
        check("POOL_ENABLED=true + 200: plain cost_tracker.record('RUNNING') NOT used on the pool-grant path",
              all(r[1] != "RUNNING" or r[0] != "pool-vm-granted" for r in vm_manager.cost_tracker.records))


# ── Section 3: POOL_ENABLED=true + 409 POOL_EMPTY -> falls through ─────────
def test_pool_enabled_409_falls_through_to_cold_launch():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
        os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)

        def _behavior(url, json):
            return _FakeResponse(409, {"error": "POOL_EMPTY"})
        _FakeAsyncClient.behavior = _behavior

        try:
            request = _build_request(vm_manager, session_id="sess-pool-empty")
            result = run_async(microvm_mod.launch_microvm(request))
        finally:
            restore()

        check("409 POOL_EMPTY: run_vm IS called (cold-launch fallback)",
              len(vm_manager.run_vm_calls) == 1)
        check("409 POOL_EMPTY: launch completes successfully via cold-launch",
              isinstance(result, dict) and result.get("microvmId") == "cold-vm-1")


# ── Section 4: 5xx / timeout / connection-refused -> falls through, no raise ─
def test_pool_enabled_errors_fall_through_never_raise():
    scenarios = {
        "5xx": lambda url, json: _FakeResponse(502, {"error": "GRANT_FAILED"}),
        "timeout": lambda url, json: (_ for _ in ()).throw(httpx.TimeoutException("timed out")),
        "connection-refused": lambda url, json: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    }
    for label, behavior in scenarios.items():
        with EnvSandbox():
            os.environ["POOL_ENABLED"] = "true"
            os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
            os.environ["MICROVM_EXEC_ROLE_ARN"] = "arn:aws:iam::123456789012:role/exec"
            os.environ["MICROVM_IMAGE_ARN"] = "arn:aws:lambda:us-west-2:123456789012:microvm-image:nb"

            vm_manager = _FakeVmManager()
            astorage = _FakeAStorage()
            restore = _patch_module(vm_manager, astorage)
            _FakeAsyncClient.behavior = behavior

            raised = None
            result = None
            try:
                request = _build_request(vm_manager, session_id=f"sess-err-{label}")
                result = run_async(microvm_mod.launch_microvm(request))
            except Exception as e:
                raised = e
            finally:
                restore()

            check(f"{label}: _try_pool_grant never raises out of launch_microvm", raised is None)
            check(f"{label}: run_vm IS called (fallback)", len(vm_manager.run_vm_calls) == 1)
            check(f"{label}: launch completes successfully via cold-launch fallback",
                  isinstance(result, dict) and result.get("microvmId") == "cold-vm-1")


# ── Section 5: end-to-end mint-time flow — pool grant returns MINT time, and a
#    real SQLite vm_session_create persists it (regression guard: omitting it
#    still defaults to now, so every existing caller is unchanged) ───────────
def test_grant_and_storage_carry_mint_time_not_adopt_time():
    from tests.pool._poolkit import _run_sqlite, build_config, build_manager
    from pool_manager.fake_backend import FakeVmBackend

    # (a) PoolManager.grant() returns the spare's MINT launched_at, not adopt now.
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    manager = build_manager(backend, storage, config)
    run_async(manager.fill_once())

    # The one spare the fill minted — grab its mint time from the ledger.
    spare = storage.pool_vm_list(state="available")[0]
    spare_mint = spare["launched_at"]

    before_grant = datetime.now(timezone.utc).timestamp()
    result = run_async(manager.grant(
        {"arch": "arm64", "size": 2048, "session_id": "real-sess-mint"}))
    check("grant() response includes launched_at",
          "launched_at" in result)
    check("grant() launched_at equals the spare's MINT time (not the adopt moment)",
          result["launched_at"] == spare_mint)
    # In this fast fake the mint happens milliseconds before the grant, so prove
    # it's the SAME mint value, not a fresh now() minted at grant time.
    check("grant() launched_at is the pre-grant mint value, not a grant-time now()",
          datetime.fromisoformat(result["launched_at"]).timestamp() <= before_grant + 0.001)

    # (b) vm_session_create persists a PASSED launched_at (the pool mint time),
    #     and OMITTING it still defaults to now — the existing-caller regression.
    old_mint = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    created = storage.vm_session_create("vm-mint", session_id="s-mint",
                                        memory_mib=2048, launched_at=old_mint)
    check("vm_session_create(launched_at=<iso>) returns that mint time",
          created["launched_at"] == old_mint)
    row = storage.vm_session_get("vm-mint")
    check("vm_session_create persists the passed mint time to the row (not now)",
          row["launched_at"] == old_mint)

    before_default = datetime.now(timezone.utc).timestamp()
    created2 = storage.vm_session_create("vm-default", session_id="s-def", memory_mib=2048)
    row2 = storage.vm_session_get("vm-default")
    check("vm_session_create WITHOUT launched_at still defaults to now (existing-caller guard)",
          row2["launched_at"] is not None
          and datetime.fromisoformat(row2["launched_at"]).timestamp() >= before_default - 1.0)
    check("default launched_at round-trips in the returned dict",
          created2["launched_at"] == row2["launched_at"])


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Proxy /launch grant-then-fallback ===")
    print("\n-- POOL_ENABLED=false: no /grant call, cold-launch taken --")
    test_pool_disabled_no_grant_call_cold_launch_taken()
    print("\n-- POOL_ENABLED=true + 200: adopts granted VM, skips cold-launch --")
    test_pool_enabled_200_adopts_vm_skips_cold_launch()
    print("\n-- POOL_ENABLED=true + 409 POOL_EMPTY: falls through --")
    test_pool_enabled_409_falls_through_to_cold_launch()
    print("\n-- POOL_ENABLED=true + 5xx/timeout/connection-refused: falls through, never raises --")
    test_pool_enabled_errors_fall_through_never_raise()
    print("\n-- grant + storage carry MINT time, not adopt time --")
    test_grant_and_storage_carry_mint_time_not_adopt_time()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY LAUNCH GRANT CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_launch_grant():
    _checks.clear()
    test_pool_disabled_no_grant_call_cold_launch_taken()
    test_pool_enabled_200_adopts_vm_skips_cold_launch()
    test_pool_enabled_409_falls_through_to_cold_launch()
    test_pool_enabled_errors_fall_through_never_raise()
    test_grant_and_storage_carry_mint_time_not_adopt_time()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "proxy launch grant failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
