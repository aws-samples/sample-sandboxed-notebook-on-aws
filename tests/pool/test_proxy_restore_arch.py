#!/usr/bin/env python3
# test-kind: offline
"""
A restored notebook comes back on the architecture (and tier) it last ran on
(restore-arch-bug).

  (a) arch_size_from_image parses {base}-{arch}-{mem} image ARNs/names.
  (b) Pool-enabled restore: the /grant asks for the SAVED arch:size (x86_64:1024)
      even when the request says arm64:2048, and the 409 cold-launch fallback
      launches the saved image too (never a spare/image of another arch).
  (c) Restore of a session whose last vm_sessions row has no arch/tier: they are
      inferred from that VM's image (control-plane detail), not defaulted.
  (d) GET /instances reports a VM's arch/size from its image ARN when the proxy
      has no record of it (a cold launch still PENDING was shown as arm64/2048).
  (e) Reactive recovery with no in-memory/DB arch reads it from the dead VM's
      image and asks the pool for that arch.

Cross-arch grant refusal inside the pool manager is covered by
tests/pool/test_pool_grant.py::test_two_arch_grant_matches_arch.

Offline: fakes from test_proxy_launch_grant / test_proxy_recover_grant, a temp
SQLite for (d). No AWS, no network.
Run:
    .venv/bin/python tests/pool/test_proxy_restore_arch.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from proxy.platform.defaults import arch_size_from_image  # noqa: E402
from tests.pool.test_proxy_launch_grant import (  # noqa: E402
    EnvSandbox, _FakeAStorage, _FakeAsyncClient, _FakeResponse, _FakeVmManager,
    _build_request, _patch_module, run_async,
)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


_SIZES = {"arm64": [1024, 2048, 4096, 8192], "x86_64": [1024, 2048, 4096]}
_X86_ARN = "arn:aws:lambda:us-west-2:123456789012:microvm-image:notebook-microvm-x86_64-1024"


def test_arch_size_from_image():
    check("(a) x86_64-1024 ARN", arch_size_from_image(_X86_ARN) == ("x86_64", 1024))
    check("(a) arm64-2048 bare name",
          arch_size_from_image("notebook-microvm-arm64-2048") == ("arm64", 2048))
    check("(a) unparseable / empty -> (None, None)",
          arch_size_from_image("other-image") == (None, None)
          and arch_size_from_image(None) == (None, None)
          and arch_size_from_image("") == (None, None))


_LAST = {}  # astorage / vm_manager of the most recent _restore_launch (for row assertions)


def _restore_launch(saved, pool, grant_status=409, detail=None, extra=None, fetch_report=None, s3=None):
    """Run one restore /launch (request arm64/2048). Returns (result, grants, run_vm_calls).
    extra: more body keys (restoreOverride, arch, memoryMiB). fetch_report: async stand-in
    for the proxy's restore-report fetch (default: reports nothing). s3: fake S3 client
    for the checkpoint size lookup (default: none, so the size is unknown)."""
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true" if pool else "false"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        vm_manager = _FakeVmManager()
        if s3 is not None:
            vm_manager._get_s3_client = lambda _s3=s3: _s3
        from proxy.notebook import session_relink
        session_relink._ckpt_kb_cache.clear()
        getattr(session_relink, "_ckpt_list_cache", {}).clear()
        if detail is not None:
            async def _detail(mid, _d=detail):
                return dict(_d)
            vm_manager.get_microvm_detail = _detail
        astorage = _FakeAStorage()

        async def _list(session_id, _saved=saved):
            return list(_saved)
        astorage.vm_session_list_for_session = _list
        restore = _patch_module(vm_manager, astorage)
        orig = (microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH, microvm_mod._fetch_restore_report)
        microvm_mod.IMAGE_ARCHES = ["arm64", "x86_64"]
        microvm_mod._SIZES_BY_ARCH = {a: list(s) for a, s in _SIZES.items()}
        async def _no_report(*a, **k):
            return None
        microvm_mod._fetch_restore_report = fetch_report or _no_report
        _LAST.update(astorage=astorage, vm_manager=vm_manager)
        grants = []

        def _behavior(url, json):
            grants.append(json)
            if grant_status == 200:
                return _FakeResponse(200, {"vm_id": "pool-vm-x", "endpoint": "pool-ep-x"})
            return _FakeResponse(grant_status, {"error": "POOL_EMPTY"})
        _FakeAsyncClient.behavior = _behavior
        try:
            body = {"arch": "arm64", "memoryMiB": 2048}
            body.update(extra or {})
            req = _build_request(vm_manager, session_id="sess-ra", restoreFromSession="sess-ra", **body)
            result = run_async(microvm_mod.launch_microvm(req))
        finally:
            microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH, microvm_mod._fetch_restore_report = orig
            restore()
        return result, grants, vm_manager.run_vm_calls


def test_pool_restore_claims_saved_arch():
    saved = [{"microvm_id": "old-vm", "architecture": "x86_64", "memory_mib": 1024}]
    result, grants, _ = _restore_launch(saved, pool=True, grant_status=200)
    check("(b) restore grant asks for the saved x86_64:1024, not the requested arm64:2048",
          len(grants) == 1 and grants[0].get("arch") == "x86_64" and grants[0].get("size") == 1024)
    check("(b) restore grant carries restore_from", bool(grants) and grants[0].get("restore_from") == "sess-ra")
    check("(b) granted restore reports x86_64/1024",
          result.get("architecture") == "x86_64" and result.get("memory_mib") == 1024)
    result, grants, calls = _restore_launch(saved, pool=True, grant_status=409)
    check("(b) pool miss: cold launch uses the saved x86_64-1024 image",
          len(calls) == 1 and calls[0]["imageIdentifier"].endswith("-x86_64-1024"))


_SAVED_X86 = [{"microvm_id": "old-vm", "architecture": "x86_64", "memory_mib": 1024}]


def _is_400(result):
    import json as _json
    try:
        return result.status_code == 400 and "error" in _json.loads(result.body)
    except Exception:
        return False


def test_restore_override():
    """(h) Only restoreOverride: true changes the pin; invalid choices are 400."""
    r, _, calls = _restore_launch(_SAVED_X86, pool=False)
    check("(h) no flag: stays on saved x86_64/1024 even when the body says arm64/2048",
          r.get("architecture") == "x86_64" and r.get("memory_mib") == 1024
          and calls[0]["imageIdentifier"].endswith("-x86_64-1024"))
    r, _, calls = _restore_launch(_SAVED_X86, pool=False, extra={"restoreOverride": "true"})
    check("(h) restoreOverride that is not boolean true is ignored (pinned)",
          r.get("architecture") == "x86_64" and r.get("memory_mib") == 1024)
    r, _, calls = _restore_launch(_SAVED_X86, pool=False, extra={"restoreOverride": True})
    check("(h) override honors arch and memory (arm64/2048)",
          r.get("architecture") == "arm64" and r.get("memory_mib") == 2048
          and calls[0]["imageIdentifier"].endswith("-arm64-2048"))
    r, _, calls = _restore_launch(_SAVED_X86, pool=False,
                                  extra={"restoreOverride": True, "arch": "x86_64", "memoryMiB": 4096})
    check("(h) override larger tier on the same arch (x86_64/4096)",
          r.get("architecture") == "x86_64" and r.get("memory_mib") == 4096)
    r, _, calls = _restore_launch(_SAVED_X86, pool=False, extra={"restoreOverride": True, "arch": None})
    check("(h) override without arch keeps the saved arch, honors memory",
          r.get("architecture") == "x86_64" and r.get("memory_mib") == 2048)
    n = len(_LAST["astorage"].vm_session_create_calls)
    r, _, calls = _restore_launch(_SAVED_X86, pool=False, extra={"restoreOverride": True, "arch": "riscv"})
    check("(h) invalid arch: 400 JSON error, nothing launched",
          _is_400(r) and not calls and not _LAST["astorage"].vm_session_create_calls)
    r, _, calls = _restore_launch(_SAVED_X86, pool=False,
                                  extra={"restoreOverride": True, "arch": "x86_64", "memoryMiB": 8192})
    check("(h) tier not offered for the arch: 400 JSON error, nothing launched (no snapping)",
          _is_400(r) and not calls)
    r, grants, _ = _restore_launch(_SAVED_X86, pool=True, grant_status=200, extra={"restoreOverride": True})
    check("(h) pool grant asks for the override arm64:2048",
          len(grants) == 1 and grants[0].get("arch") == "arm64" and grants[0].get("size") == 2048)
    row = (_LAST["astorage"].vm_session_create_calls or [{}])[-1]
    rec = _LAST["vm_manager"].active_microvms.get(r.get("microvmId"), {})
    check("(h) granted override: vm_sessions row and in-memory record carry arm64/2048",
          row.get("architecture") == "arm64" and row.get("memory_mib") == 2048
          and rec.get("architecture") == "arm64" and rec.get("memory_mib") == 2048)
    r, grants, calls = _restore_launch(_SAVED_X86, pool=True, grant_status=409, extra={"restoreOverride": True})
    check("(h) pool miss cold-launches exactly the overridden image (arm64-2048)",
          len(calls) == 1 and calls[0]["imageIdentifier"].endswith("-arm64-2048"))
    row = (_LAST["astorage"].vm_session_create_calls or [{}])[-1]
    rec = _LAST["vm_manager"].active_microvms.get(r.get("microvmId"), {})
    check("(h) cold override: row and record carry arm64/2048",
          row.get("architecture") == "arm64" and row.get("memory_mib") == 2048
          and rec.get("architecture") == "arm64" and rec.get("memory_mib") == 2048)
    after = [{"microvm_id": "old-vm", "architecture": "x86_64", "memory_mib": 1024},
             {"microvm_id": "new-vm", "architecture": "arm64", "memory_mib": 2048}]
    r, _, calls = _restore_launch(after, pool=False)
    check("(h) a later default restore pins to the override row (arm64/2048)",
          r.get("architecture") == "arm64" and r.get("memory_mib") == 2048)


def test_restore_report_plumbing():
    """(i) /launch returns restore_report on a restore (pool and cold), never otherwise."""
    async def _failed(*a, **k):
        return {"verified": True, "arch": "x86_64", "python": "3.12", "packages_total": 3,
                "packages_failed": [{"name": "foo", "reason": "No matching distribution found for foo"}]}
    for pool, status in ((True, 200), (False, 409)):
        r, _, _ = _restore_launch(_SAVED_X86, pool=pool, grant_status=status, fetch_report=_failed)
        rep = r.get("restore_report") or {}
        check(f"(i) pool={pool}: failed packages passed through",
              rep.get("verified") is True and rep.get("packages_total") == 3
              and rep.get("packages_failed") == [{"name": "foo", "reason": "No matching distribution found for foo"}])
    r, _, _ = _restore_launch(_SAVED_X86, pool=False)
    check("(i) fetch returns nothing: restore_report is verified false",
          (r.get("restore_report") or {}).get("verified") is False)
    async def _boom(*a, **k):
        raise RuntimeError("down")
    r, _, _ = _restore_launch(_SAVED_X86, pool=True, grant_status=200, fetch_report=_boom)
    check("(i) fetch raises: restore_report is verified false",
          (r.get("restore_report") or {}).get("verified") is False)
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        vm_manager = _FakeVmManager()
        restore = _patch_module(vm_manager, _FakeAStorage())
        try:
            r = run_async(microvm_mod.launch_microvm(_build_request(vm_manager, session_id="sess-fresh")))
        finally:
            restore()
    check("(i) a launch that is not a restore carries no restore_report", "restore_report" not in r)


class _FakeS3:
    """sessions/<sid>/metadata.json and a paginated object list, in memory."""
    def __init__(self, metadata=None, objects=None, page_size=1000, fail=False):
        self.metadata, self.objects, self.page_size, self.fail = metadata, dict(objects or {}), page_size, fail
        self.get_calls, self.list_calls = [], []

    def get_object(self, Bucket, Key):
        import io
        import json as _json
        self.get_calls.append(Key)
        if self.fail:
            raise RuntimeError("S3 down")
        if Key.endswith("/metadata.json") and self.metadata is not None:
            return {"Body": io.BytesIO(_json.dumps(self.metadata).encode())}
        err = RuntimeError("NoSuchKey")
        err.response = {"Error": {"Code": "NoSuchKey"}}
        raise err

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):
        self.list_calls.append(ContinuationToken)
        if self.fail:
            raise RuntimeError("S3 down")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + min(MaxKeys, self.page_size)]
        out = {"Contents": [{"Key": k, "Size": self.objects[k]} for k in page], "KeyCount": len(page)}
        if start + len(page) < len(keys):
            out.update(IsTruncated=True, NextContinuationToken=str(start + len(page)))
        else:
            out["IsTruncated"] = False
        return out


def _capture_logs():
    import logging

    class _H(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())
    h = _H()
    microvm_mod.logger.addHandler(h)
    return h


def test_restore_memory_check():
    """(j) /launch with restore_from compares the real S3 checkpoint size with the
    EFFECTIVE tier, logs it and returns restore_memory_check. It never blocks."""
    big = _FakeS3(metadata={"checkpoint_total_kb": 1572864})  # 1.5 GB
    for pool, status in ((True, 200), (False, 409)):
        h = _capture_logs()
        try:
            r, grants, calls = _restore_launch(_SAVED_X86, pool=pool, grant_status=status, s3=big)
        finally:
            microvm_mod.logger.removeHandler(h)
        mc = r.get("restore_memory_check") or {}
        check(f"(j) pool={pool}: default pinned restore of 1.5 GB at x86_64/1024 launches (not blocked)",
              r.get("status") == "running" and r.get("memory_mib") == 1024
              and (grants if pool else calls))
        check(f"(j) pool={pool}: restore_memory_check strong, 1572864 KB on 1024 MiB, need 3072 MiB, ratio 3.0",
              mc.get("level") == "strong" and mc.get("checkpoint_kb") == 1572864 and mc.get("tier_mib") == 1024
              and mc.get("need_mib") == 3072 and abs((mc.get("ratio") or 0) - 3.0) < 0.01
              and mc.get("source") == "metadata")
        check(f"(j) pool={pool}: a log line names the session, size, tier and level",
              any("Restore memory check" in ln and "sess-ra" in ln and "1572864" in ln
                  and "1024" in ln and "strong" in ln for ln in h.lines))
    # Override from a saved 2048 down to 1024 (the effective tier is the override).
    saved_2048 = [{"microvm_id": "old-vm", "architecture": "x86_64", "memory_mib": 2048}]
    r, _, calls = _restore_launch(saved_2048, pool=False, s3=big,
                                  extra={"restoreOverride": True, "arch": "x86_64", "memoryMiB": 1024})
    mc = r.get("restore_memory_check") or {}
    check("(j) override 2048 -> 1024: launched on 1024, restore_memory_check strong for 1024",
          r.get("memory_mib") == 1024 and calls and calls[0]["imageIdentifier"].endswith("-x86_64-1024")
          and mc.get("level") == "strong" and mc.get("tier_mib") == 1024)
    # Same saved 2048 restored on its default tier: the check uses 2048 (still strong, 1.5).
    r, _, _ = _restore_launch(saved_2048, pool=False, s3=big)
    mc = r.get("restore_memory_check") or {}
    check("(j) default restore at the saved 2048: strong for 2048 (ratio 1.5)",
          mc.get("level") == "strong" and mc.get("tier_mib") == 2048 and abs((mc.get("ratio") or 0) - 1.5) < 0.01)
    r, _, _ = _restore_launch(saved_2048, pool=False, s3=big,
                              extra={"restoreOverride": True, "arch": "x86_64", "memoryMiB": 4096})
    check("(j) override to a larger 4096: warn (ratio 0.75)",
          (r.get("restore_memory_check") or {}).get("level") == "warn")
    # Metadata missing and no objects listed: unknown, launch not failed.
    r, _, _ = _restore_launch(_SAVED_X86, pool=False, s3=_FakeS3())
    mc = r.get("restore_memory_check") or {}
    check("(j) metadata missing, nothing listed: level unknown, launch still runs",
          r.get("status") == "running" and mc.get("level") == "unknown" and mc.get("checkpoint_kb") is None)
    # S3 failing outright: unknown, launch not failed.
    r, _, _ = _restore_launch(_SAVED_X86, pool=True, grant_status=200, s3=_FakeS3(fail=True))
    check("(j) S3 error: level unknown, launch still runs",
          r.get("status") == "running" and (r.get("restore_memory_check") or {}).get("level") == "unknown")
    # No S3 client at all on the vm manager: unknown, launch not failed.
    r, _, _ = _restore_launch(_SAVED_X86, pool=False)
    check("(j) no S3 client: level unknown, launch still runs",
          r.get("status") == "running" and (r.get("restore_memory_check") or {}).get("level") == "unknown")
    # List fallback: no metadata.json, several objects over several pages are summed.
    objs = {"sessions/sess-ra/manifest.json": 2048,
            "sessions/sess-ra/vars/a.pkl": 700 * 1024 * 1024,
            "sessions/sess-ra/vars/b.pkl": 500 * 1024 * 1024,
            "sessions/sess-ra/files/big.bin": 336 * 1024 * 1024 - 2048,
            "sessions/other/vars/z.pkl": 9 * 1024 * 1024 * 1024}
    lst = _FakeS3(metadata=None, objects=objs, page_size=2)
    r, _, _ = _restore_launch(_SAVED_X86, pool=False, s3=lst)
    mc = r.get("restore_memory_check") or {}
    check("(j) list fallback: 4 objects over 2 pages summed to 1572864 KB, strong at 1024",
          mc.get("source") == "s3_list" and mc.get("checkpoint_kb") == 1572864
          and mc.get("level") == "strong" and len(lst.list_calls) == 2)
    # metadata with checkpoint_total_kb = 0 (old image) also falls back to the list.
    lst0 = _FakeS3(metadata={"checkpoint_total_kb": 0}, objects=objs, page_size=1000)
    r, _, _ = _restore_launch(_SAVED_X86, pool=False, s3=lst0)
    check("(j) metadata total 0 falls back to the list sum",
          (r.get("restore_memory_check") or {}).get("checkpoint_kb") == 1572864)
    # Bounded: a list with more pages than the bound stops and reports a partial sum.
    import proxy.notebook.session_relink as sr
    many = {f"sessions/sess-ra/f{i:04d}": 1024 for i in range(sr._CKPT_LIST_MAX_PAGES * 3)}
    bounded = _FakeS3(objects=many, page_size=1)
    r, _, _ = _restore_launch(_SAVED_X86, pool=False, s3=bounded)
    mc = r.get("restore_memory_check") or {}
    check("(j) list is bounded to _CKPT_LIST_MAX_PAGES pages and marked partial",
          len(bounded.list_calls) == sr._CKPT_LIST_MAX_PAGES and mc.get("source") == "s3_list_partial"
          and mc.get("checkpoint_kb") == sr._CKPT_LIST_MAX_PAGES)
    # Cached: a second lookup inside the TTL does not list again.
    lst2 = _FakeS3(objects={k.replace("sess-ra", "sess-cache"): v for k, v in objs.items()}, page_size=1000)
    kb1 = run_async(sr.restore_checkpoint_size(types.SimpleNamespace(
        _get_s3_client=lambda: lst2, get_artifacts_bucket=lambda: "b"), "sess-cache"))
    kb2 = run_async(sr.restore_checkpoint_size(types.SimpleNamespace(
        _get_s3_client=lambda: lst2, get_artifacts_bucket=lambda: "b"), "sess-cache"))
    check("(j) size lookup is cached (one metadata read, no second list)",
          kb1 == kb2 and kb1[0] == 1572864 and len(lst2.list_calls) == 1
          and lst2.get_calls.count("sessions/sess-cache/metadata.json") == 1)
    # A launch that is not a restore has no restore_memory_check.
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        vm_manager = _FakeVmManager()
        restore = _patch_module(vm_manager, _FakeAStorage())
        try:
            r = run_async(microvm_mod.launch_microvm(_build_request(vm_manager, session_id="sess-fresh")))
        finally:
            restore()
    check("(j) a launch that is not a restore carries no restore_memory_check", "restore_memory_check" not in r)


def test_restore_infers_missing_arch_from_image():
    saved = [{"microvm_id": "old-vm", "architecture": None, "memory_mib": None}]
    result, _, calls = _restore_launch(saved, pool=False,
                                       detail={"state": "TERMINATED", "imageArn": _X86_ARN})
    check("(c) row without arch/tier: restore launches the old VM's x86_64-1024 image",
          len(calls) == 1 and calls[0]["imageIdentifier"].endswith("-x86_64-1024"))
    check("(c) ... and reports x86_64/1024",
          result.get("architecture") == "x86_64" and result.get("memory_mib") == 1024)


def test_instances_uses_image_when_unrecorded():
    from tests.pool._poolkit import _run_sqlite
    from proxy.platform.cost_tracker import CostTracker
    import proxy.storage as ps
    from proxy.storage.async_storage import AsyncStorage
    from proxy.platform.datasources import registry
    storage = _run_sqlite()
    saved = (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
             registry.maybe_refresh_catalog_in_background)
    a = AsyncStorage(storage)
    ps.storage = storage
    ps.astorage = a
    microvm_mod.storage = storage
    microvm_mod.astorage = a
    registry.maybe_refresh_catalog_in_background = lambda *x, **k: False
    microvm_mod._vm_missing_polls.clear()

    class _Vm:
        cost_tracker = CostTracker()
        active_microvms = {}

        async def list_vms(self):
            return {"items": [{"microvmId": "microvm-unrec", "state": "PENDING", "imageArn": _X86_ARN}]}

        async def get_microvm_detail(self, mid):
            return {"imageArn": _X86_ARN}

        async def get_vm_state(self, mid):
            return "PENDING"
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=_Vm())))
    try:
        out = asyncio.run(microvm_mod.list_instances(req))
    finally:
        (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
         registry.maybe_refresh_catalog_in_background) = saved
    inst = (out.get("instances") or {}).get("microvm-unrec") or {}
    check("(d) /instances: unrecorded PENDING x86 VM reports x86_64 (was arm64)",
          inst.get("architecture") == "x86_64")
    check("(d) /instances: ... and 1024 MiB from its image (was 2048)", inst.get("memory_mib") == 1024)


def test_recover_infers_arch_from_image():
    from tests.pool import test_proxy_recover_grant as rg
    with rg.EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        storage_fake = rg._FakeStorage()
        manager, _calls, restore = rg._build_manager(storage_fake)
        seen = []

        def _behavior(url, json):
            seen.append(json)
            return rg._FakeResponse(200, {"vm_id": "pool-vm-r", "endpoint": "pool-ep-r"})
        rg._FakeAsyncClient.behavior = _behavior

        async def _detail(mid):
            return {"state": "TERMINATED", "imageArn": _X86_ARN}
        manager.get_microvm_detail = _detail
        try:
            # No in-memory record and no DB row (vm_session_get -> None).
            run_async(manager._do_recover("sess-rr", {"vm_id": "old-vm-r"}))
        finally:
            restore()
    check("(e) recovery with no arch record asks the pool for x86_64:1024 (the dead VM's image)",
          len(seen) == 1 and seen[0].get("arch") == "x86_64" and seen[0].get("size") == 1024)


def test_cold_launch_records_arch_and_instances_reports_it():
    """(f) A cold launch (pool miss) at x86_64/1024 records arch+tier on BOTH the
    in-memory record and the vm_sessions row, and GET /instances then reports
    x86_64 / 1024 MiB from the record alone (list item without an imageArn)."""
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        restore = _patch_module(vm_manager, astorage)
        orig = (microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH)
        microvm_mod.IMAGE_ARCHES = ["arm64", "x86_64"]
        microvm_mod._SIZES_BY_ARCH = {a: list(s) for a, s in _SIZES.items()}
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(409, {"error": "POOL_EMPTY"})
        try:
            req = _build_request(vm_manager, session_id="sess-cold", arch="x86_64", memoryMiB=1024)
            result = run_async(microvm_mod.launch_microvm(req))
        finally:
            microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH = orig
            restore()
    mid = result.get("microvmId")
    rows = astorage.vm_session_create_calls
    check("(f) cold launch (pool 409) used run_vm with the x86_64-1024 image",
          len(vm_manager.run_vm_calls) == 1
          and vm_manager.run_vm_calls[0]["imageIdentifier"].endswith("-x86_64-1024"))
    check("(f) cold launch records x86_64/1024 in memory",
          vm_manager.active_microvms.get(mid, {}).get("architecture") == "x86_64"
          and vm_manager.active_microvms.get(mid, {}).get("memory_mib") == 1024)
    check("(f) cold launch records x86_64/1024 on the vm_sessions row",
          len(rows) == 1 and rows[0].get("architecture") == "x86_64" and rows[0].get("memory_mib") == 1024)

    # /instances over a real SQLite holding that row; the list item has NO imageArn.
    from tests.pool._poolkit import _run_sqlite
    from proxy.platform.cost_tracker import CostTracker
    import proxy.storage as ps
    from proxy.storage.async_storage import AsyncStorage
    from proxy.platform.datasources import registry
    storage = _run_sqlite()
    storage.vm_session_create(**rows[0])
    saved = (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
             registry.maybe_refresh_catalog_in_background)
    a = AsyncStorage(storage)
    ps.storage, ps.astorage, microvm_mod.storage, microvm_mod.astorage = storage, a, storage, a
    registry.maybe_refresh_catalog_in_background = lambda *x, **k: False
    microvm_mod._vm_missing_polls.clear()

    class _Vm:
        cost_tracker = CostTracker()
        active_microvms = {}

        async def list_vms(self):
            return {"items": [{"microvmId": mid, "state": "RUNNING"}]}

        async def get_microvm_detail(self, m):
            return {"endpoint": "cold-endpoint-1"}

        async def get_vm_state(self, m):
            return "RUNNING"
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=_Vm())))
    try:
        out = asyncio.run(microvm_mod.list_instances(req))
    finally:
        (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
         registry.maybe_refresh_catalog_in_background) = saved
    inst = (out.get("instances") or {}).get(mid) or {}
    check("(f) /instances reports the cold-launched VM as x86_64 / 1024 MiB (1 GB)",
          inst.get("architecture") == "x86_64" and inst.get("memory_mib") == 1024)


def test_instances_just_launched_entry_has_arch():
    """(g) A VM launched <60 s ago that the control-plane list does not show yet
    is added from the in-memory map. That entry had no 'architecture' key (seen
    live: arch None for a just-adopted x86_64-1024 VM, and the UI drew ARM64)."""
    from tests.pool._poolkit import _run_sqlite
    from proxy.platform.cost_tracker import CostTracker
    import proxy.storage as ps
    from proxy.storage.async_storage import AsyncStorage
    from proxy.platform.datasources import registry
    import time as _time
    storage = _run_sqlite()
    storage.vm_session_create(microvm_id="microvm-fresh-db", session_id="s-db", memory_mib=1024,
                              endpoint="e2", architecture="x86_64")
    saved = (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
             registry.maybe_refresh_catalog_in_background)
    a = AsyncStorage(storage)
    ps.storage, ps.astorage, microvm_mod.storage, microvm_mod.astorage = storage, a, storage, a
    registry.maybe_refresh_catalog_in_background = lambda *x, **k: False
    microvm_mod._vm_missing_polls.clear()

    class _Vm:
        cost_tracker = CostTracker()
        active_microvms = {
            "microvm-fresh": {"endpoint": "e", "session_id": "s-f", "memory_mib": 1024,
                              "architecture": "x86_64", "launched_at": _time.time()},
            # In-memory record without arch/tier (e.g. older code path): the DB row supplies them.
            "microvm-fresh-db": {"endpoint": "e2", "session_id": "s-db", "launched_at": _time.time()},
        }

        async def list_vms(self):
            return {"items": []}

        async def get_microvm_detail(self, m):
            return {}

        async def get_vm_state(self, m):
            return "RUNNING"
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=_Vm())))
    try:
        out = asyncio.run(microvm_mod.list_instances(req))
    finally:
        (microvm_mod.storage, microvm_mod.astorage, ps.storage, ps.astorage,
         registry.maybe_refresh_catalog_in_background) = saved
    inst = out.get("instances") or {}
    f, d = inst.get("microvm-fresh") or {}, inst.get("microvm-fresh-db") or {}
    check("(g) just-launched entry (not yet listed) reports x86_64/1024 from the record",
          f.get("architecture") == "x86_64" and f.get("memory_mib") == 1024)
    check("(g) just-launched entry without an in-memory arch takes x86_64/1024 from the DB row",
          d.get("architecture") == "x86_64" and d.get("memory_mib") == 1024)


_SECTIONS = (test_arch_size_from_image, test_pool_restore_claims_saved_arch,
             test_cold_launch_records_arch_and_instances_reports_it,
             test_instances_just_launched_entry_has_arch,
             test_restore_override, test_restore_report_plumbing, test_restore_memory_check,
             test_restore_infers_missing_arch_from_image, test_instances_uses_image_when_unrecorded,
             test_recover_infers_arch_from_image)


def main():
    print("\n=== Restore keeps the notebook's arch/tier ===")
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
    print("ALL RESTORE-ARCH CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_restore_arch():
    _checks.clear()
    for fn in _SECTIONS:
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    main()
