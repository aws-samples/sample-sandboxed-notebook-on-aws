#!/usr/bin/env python3
# test-kind: offline
"""
Proxy /health `pool_enabled` test suite (pool-freeze-lifecycle-attrs task).

Exercises proxy.server.health()'s new `pool_enabled` field: it must mirror
POOL_ENABLED via the SAME parse routes/microvm.py already uses
(_pool_enabled), not a second/duplicated parse. Also regression-guards that
max_lifetime_seconds / idle_timeout_seconds are still present and unchanged
(7b9ee14 behavior).

Calls proxy.server.health() directly as an async function with a fake
app.state.vm_manager — no real FastAPI/uvicorn server, no AWS.

Run:
    .venv/bin/python tests/pool/test_proxy_health_pool_enabled.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.server as server_mod  # noqa: E402

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


class _FakeTokenCache:
    async def size(self):
        return 0


class _FakeVmManager:
    def __init__(self):
        self.token_cache = _FakeTokenCache()
        self.active_microvms = {}


def _patch_app_state(vm_manager):
    orig_state = server_mod.app.state
    server_mod.app.state.vm_manager = vm_manager
    return lambda: setattr(server_mod.app, "state", orig_state)


# ── Section 1: POOL_ENABLED unset/false -> pool_enabled is False ──────────
def test_pool_enabled_unset_is_false():
    with EnvSandbox():
        os.environ.pop("POOL_ENABLED", None)
        vm_manager = _FakeVmManager()
        restore = _patch_app_state(vm_manager)
        try:
            result = run_async(server_mod.health())
        finally:
            restore()

        check("POOL_ENABLED unset: pool_enabled is False", result.get("pool_enabled") is False)
        check("max_lifetime_seconds still present", "max_lifetime_seconds" in result)
        check("idle_timeout_seconds still present", "idle_timeout_seconds" in result)


def test_pool_enabled_false_is_false():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        vm_manager = _FakeVmManager()
        restore = _patch_app_state(vm_manager)
        try:
            result = run_async(server_mod.health())
        finally:
            restore()

        check("POOL_ENABLED=false: pool_enabled is False", result.get("pool_enabled") is False)


# ── Section 2: POOL_ENABLED=true -> pool_enabled is True ──────────────────
def test_pool_enabled_true_is_true():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["MAX_LIFETIME_SECONDS"] = "28800"
        os.environ["IDLE_TIMEOUT_SECONDS"] = "60"
        vm_manager = _FakeVmManager()
        restore = _patch_app_state(vm_manager)
        try:
            result = run_async(server_mod.health())
        finally:
            restore()

        check("POOL_ENABLED=true: pool_enabled is True", result.get("pool_enabled") is True)
        check("max_lifetime_seconds unchanged (28800)", result.get("max_lifetime_seconds") == 28800)
        check("idle_timeout_seconds unchanged (60)", result.get("idle_timeout_seconds") == 60)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Proxy /health pool_enabled ===")
    print("\n-- POOL_ENABLED unset: pool_enabled is False --")
    test_pool_enabled_unset_is_false()
    print("\n-- POOL_ENABLED=false: pool_enabled is False --")
    test_pool_enabled_false_is_false()
    print("\n-- POOL_ENABLED=true: pool_enabled is True --")
    test_pool_enabled_true_is_true()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY HEALTH POOL_ENABLED CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_health_pool_enabled():
    _checks.clear()
    test_pool_enabled_unset_is_false()
    test_pool_enabled_false_is_false()
    test_pool_enabled_true_is_true()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "proxy health pool_enabled failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
