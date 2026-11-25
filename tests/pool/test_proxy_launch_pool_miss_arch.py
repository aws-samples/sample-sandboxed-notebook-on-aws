#!/usr/bin/env python3
# test-kind: offline
"""
The user-selected arch/size survives every pool-miss fallback (restore-arch-bug).

For a launch the pool cannot serve, /launch falls back to a cold launch. For
each way the pool can miss -- 409 POOL_EMPTY, the pool holding that size only in
the OTHER arch (the pool manager answers 409, see test_pool_grant.py
::test_two_arch_grant_matches_arch), a /grant 5xx, the pool manager down
(connection error), and pooling disabled -- check, for x86_64/1024 and
arm64/4096:
  (1) the grant asked for exactly that arch:size, and the cold launch used the
      {IMAGE_ARN}-<arch>-<size> image (never a default);
  (2) while the cold-launched VM is still PENDING (before the RUNNING commit),
      GET /instances already reports that arch/size even when the list item
      carries no imageArn; the record is gone once the launch commits;
  (3) the /launch response and the vm_sessions row carry the launched
      arch/size;
  (4) a restore that misses the pool cold-launches the SAVED arch/size;
  (5) reactive recovery after a pool miss: launch_for_rotation's in-memory
      record carries the arch of the image it launched, from the moment it
      returns (before the restore completes).

Fakes: tests/pool/test_proxy_launch_grant (vm_manager, astorage, httpx client),
a temp SQLite for GET /instances. No AWS, no network.
Run:
    .venv/bin/python tests/pool/test_proxy_launch_pool_miss_arch.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from tests.pool.test_proxy_launch_grant import (  # noqa: E402
    EnvSandbox, _FakeAStorage, _FakeAsyncClient, _FakeResponse, _FakeVmManager,
    _build_request, _patch_module, run_async,
)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


_SIZES = {"arm64": [1024, 2048, 4096, 8192], "x86_64": [1024, 2048, 4096]}


def _miss(kind):
    """Grant behaviour for one pool-miss kind."""
    def _behavior(url, json):
        if kind == "409":
            return _FakeResponse(409, {"error": "POOL_EMPTY"})
        if kind == "other-arch-only":
            # The pool has spares of this SIZE only in the other arch: the
            # manager's claim filters arch AND size, so it answers 409.
            return _FakeResponse(409, {"error": "POOL_EMPTY"})
        if kind == "5xx":
            return _FakeResponse(503, {"error": "unavailable"})
        if kind == "down":
            raise httpx.ConnectError("connection refused")
        raise AssertionError(kind)
    return _behavior


def _instances_now(vm_manager, mid):
    """GET /instances over a temp SQLite (no rows yet) with the real vm_manager's
    in-memory map; the list item reports PENDING and NO imageArn, so only a record
    of the launch can supply arch/size."""
    from tests.pool._poolkit import _run_sqlite
    import proxy.storage as ps
    from proxy.storage.async_storage import AsyncStorage
    from proxy.platform.datasources import registry
    from proxy.platform.cost_tracker import CostTracker
    storage = _run_sqlite()
    a = AsyncStorage(storage)
    saved = (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
             registry.maybe_refresh_catalog_in_background)
    ps.storage, ps.astorage, microvm_mod.storage, microvm_mod.astorage = storage, a, storage, a
    registry.maybe_refresh_catalog_in_background = lambda *x, **k: False

    class _Poll:
        cost_tracker = CostTracker()
        active_microvms = vm_manager.active_microvms

        async def list_vms(self):
            return {"items": [{"microvmId": mid, "state": "PENDING"}]}

        async def get_microvm_detail(self, m):
            return {}

        async def get_vm_state(self, m):
            return "PENDING"
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=_Poll())))
    try:
        # list_instances is a coroutine; we are already inside the launch's loop.
        return req, saved
    finally:
        pass


def _launch(arch, size, kind, saved_rows=None):
    """One /launch with the given pool-miss kind. Returns a dict of observations."""
    obs = {"grants": [], "pending": None}
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false" if kind == "disabled" else "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        if saved_rows is not None:
            async def _list(session_id, _s=saved_rows):
                return list(_s)
            astorage.vm_session_list_for_session = _list
        restore = _patch_module(vm_manager, astorage)
        orig = (microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH)
        microvm_mod.IMAGE_ARCHES = ["arm64", "x86_64"]
        microvm_mod._SIZES_BY_ARCH = {a: list(s) for a, s in _SIZES.items()}
        inner = _miss(kind) if kind != "disabled" else (lambda url, json: _FakeResponse(200, {}))

        def _behavior(url, json):
            obs["grants"].append(json)
            return inner(url, json)
        _FakeAsyncClient.behavior = _behavior

        # Poll /instances once while the VM is PENDING (first wait-for-RUNNING poll).
        polls = {"n": 0}

        async def _detail_launch(mid):
            polls["n"] += 1
            if polls["n"] == 1:
                req, saved = _instances_now(vm_manager, mid)
                try:
                    out = await microvm_mod.list_instances(req)
                finally:
                    (microvm_mod.storage, microvm_mod.astorage, _ps.storage, _ps.astorage,
                     _reg.maybe_refresh_catalog_in_background) = saved
                obs["pending"] = (out.get("instances") or {}).get(mid)
                return {"state": "PENDING"}
            return {"state": "RUNNING"}
        import proxy.storage as _ps
        from proxy.platform.datasources import registry as _reg
        vm_manager.get_microvm_detail_launch = _detail_launch
        orig_sleep = asyncio.sleep

        async def _no_sleep(_s):
            await orig_sleep(0)
        microvm_mod.asyncio.sleep = _no_sleep
        try:
            body = {"arch": arch, "memoryMiB": size}
            if saved_rows is not None:
                body = {"arch": "arm64", "memoryMiB": 2048, "restoreFromSession": "sess-miss"}
            req = _build_request(vm_manager, session_id="sess-miss", **body)
            obs["result"] = run_async(microvm_mod.launch_microvm(req))
        finally:
            microvm_mod.asyncio.sleep = orig_sleep
            microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH = orig
            restore()
        obs["run_vm"] = vm_manager.run_vm_calls
        obs["rows"] = astorage.vm_session_create_calls
        obs["pending_left"] = dict(microvm_mod._PENDING_LAUNCH) if hasattr(microvm_mod, "_PENDING_LAUNCH") else {}
    return obs


def test_pool_miss_kinds():
    for arch, size in (("x86_64", 1024), ("arm64", 4096)):
        for kind in ("409", "other-arch-only", "5xx", "down", "disabled"):
            tag = f"{arch}:{size} pool={kind}"
            o = _launch(arch, size, kind)
            if kind == "disabled":
                check(f"[{tag}] no /grant attempted", len(o["grants"]) == 0)
            else:
                check(f"[{tag}] (1) /grant asked for exactly {arch}:{size}",
                      len(o["grants"]) == 1 and o["grants"][0].get("arch") == arch
                      and o["grants"][0].get("size") == size)
            check(f"[{tag}] (1) cold launch image is -{arch}-{size}",
                  len(o["run_vm"]) == 1 and o["run_vm"][0]["imageIdentifier"].endswith(f"-{arch}-{size}"))
            p = o["pending"] or {}
            check(f"[{tag}] (2) /instances while PENDING reports {arch}/{size}",
                  p.get("architecture") == arch and p.get("memory_mib") == size)
            check(f"[{tag}] (2) no pending record left after the commit", not o["pending_left"])
            r = o["result"]
            check(f"[{tag}] (3) /launch response carries {arch}/{size}",
                  isinstance(r, dict) and r.get("architecture") == arch and r.get("memory_mib") == size)
            check(f"[{tag}] (3) vm_sessions row carries {arch}/{size}",
                  len(o["rows"]) == 1 and o["rows"][0].get("architecture") == arch
                  and o["rows"][0].get("memory_mib") == size)


def test_restore_pool_miss_uses_saved():
    saved = [{"microvm_id": "old", "architecture": "x86_64", "memory_mib": 1024}]
    for kind in ("409", "5xx", "down"):
        o = _launch(None, None, kind, saved_rows=saved)
        check(f"[restore pool={kind}] (4) grant asked for the saved x86_64:1024",
              len(o["grants"]) == 1 and o["grants"][0].get("arch") == "x86_64"
              and o["grants"][0].get("size") == 1024)
        check(f"[restore pool={kind}] (4) cold launch uses the saved -x86_64-1024 image",
              len(o["run_vm"]) == 1 and o["run_vm"][0]["imageIdentifier"].endswith("-x86_64-1024"))
        check(f"[restore pool={kind}] (4) response is x86_64/1024",
              o["result"].get("architecture") == "x86_64" and o["result"].get("memory_mib") == 1024)


def test_rotation_record_has_arch():
    import proxy.platform.microvm_manager as mm
    mgr = mm.MicrovmManager()

    class _Client:
        def run_microvm(self, **params):
            return {"microvmId": "rot-vm", "endpoint": "rot-ep"}
    mgr.get_lambda_launch_client = lambda: _Client()
    mgr.get_artifacts_bucket = lambda: "b"
    orig = mm.EXEC_ROLE_ARN
    mm.EXEC_ROLE_ARN = "arn:aws:iam::123456789012:role/exec"
    try:
        vm_id, _ = mgr.launch_for_rotation(
            image_arn="arn:aws:lambda:us-west-2:1:microvm-image:notebook-microvm-x86_64-1024",
            memory_mib=1024, idle_timeout_sec=60, notebook_name="nb", session_id="s")
    finally:
        mm.EXEC_ROLE_ARN = orig
    rec = mgr.active_microvms.get(vm_id, {})
    check("(5) launch_for_rotation record carries x86_64/1024 before the restore commit",
          rec.get("architecture") == "x86_64" and rec.get("memory_mib") == 1024)


_SECTIONS = (test_pool_miss_kinds, test_restore_pool_miss_uses_saved, test_rotation_record_has_arch)


def main():
    print("\n=== Pool-miss fallback keeps the selected arch/size ===")
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL-MISS ARCH CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_launch_pool_miss_arch():
    _checks.clear()
    for fn in _SECTIONS:
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    main()
