#!/usr/bin/env python3
# test-kind: offline
"""
Proxy per-VM detail assembly (vm-card-state): proxy/platform/vm_detail.py and
routes/metrics.latest_image_versions.

Pure fakes, asyncio.run, no network:
  - SUSPENDED notebook VM: the VM is never called; checkpoint/variables/packages
    come from S3 metadata; dirty is None.
  - RUNNING VM: /vm-status is fetched once; a slow or failed fetch falls back to S3
    with vm_status "unavailable"; a newer live save wins over S3.
  - Stale-state guard: the cache says RUNNING but the control plane says SUSPENDED
    → no VM call.
  - Pool spare: no VM call, no S3 read, no notebook parts; Python from the
    per-image cache.
  - outdated image, origin (pool / restored), persistence 'none', S3 cache.
  - latest_image_versions: one control-plane call per 300 s; {} on failure.

Run:
    .venv/bin/python tests/proxy/test_vm_detail.py
"""
import asyncio
import os
import sys
import time
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from proxy.platform import vm_detail  # noqa: E402
from proxy.platform.cost_tracker import CostTracker  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:notebook-microvm-arm64-2048"
SID = "sess-1"


class FakeStorage:
    def __init__(self, row=None, log=None, siblings=None, spares=None):
        self.row, self.log, self.siblings, self.spares = row, log or [], siblings or [], spares or []

    async def vm_session_get(self, mid):
        return self.row

    async def pool_vm_list(self, state=None):
        return self.spares

    async def vm_state_log_get(self, mid):
        return self.log

    async def vm_session_list_for_session(self, sid):
        return self.siblings


class FakeVm:
    def __init__(self, cp_state="SUSPENDED"):
        self.cp_state = cp_state
        self.active_microvms = {}
        self.cost_tracker = CostTracker()
        self.state_calls = 0

    async def get_vm_state(self, mid):
        self.state_calls += 1
        return self.cp_state

    async def get_microvm_detail(self, mid):
        return {"state": self.cp_state, "imageArn": ARN, "imageVersion": "3"}


def _row(**over):
    r = {"microvm_id": "microvm-a", "session_id": SID, "notebook_id": 26, "state": "SUSPENDED",
         "checkpoint_enabled": 1, "launched_at": "2026-11-12T00:30:00+00:00", "architecture": "arm64",
         "endpoint": "vm.example"}
    r.update(over)
    return r


# checkpoint_total_kb = the whole saved state (shown); checkpoint_size_kb = the
# var pickles uploaded by that one save (a delta, never shown).
META = {"checkpointed_at": "2026-11-12T01:00:00+00:00", "variables_count": 186,
        "user_variables_count": 46, "user_variables_bytes": 2048, "checkpoint_size_kb": 6041.6,
        "checkpoint_total_kb": 16793.6}
# A checkpoint written before user_variables_count / checkpoint_total_kb existed.
OLD_META = {"checkpointed_at": "2026-11-12T01:00:00+00:00", "variables_count": 186, "checkpoint_size_kb": 12595.2}
REQS = ["polars==1.9.0", "", "# comment", "boto3", "requests==2.32.0"]


def _run(vm, mid, *, storage, mode="checkpoint", s3=None, live=None, live_delay=0.0,
         latest=None, last=None):
    counts = {"s3": 0, "fetch": 0}

    async def s3_read(sid):
        counts["s3"] += 1
        return s3 if s3 is not None else {"metadata": META, "requirements": REQS}

    async def fetch(m):
        counts["fetch"] += 1
        if live_delay:
            await asyncio.sleep(live_delay)
        return live

    async def latest_versions():
        return latest if latest is not None else {ARN: "4"}

    if last is None:
        last = {mid: {"state": vm.cp_state, "image_arn": ARN, "image_version": "3",
                      "architecture": "arm64", "memory_mib": 2048}}
    out = asyncio.run(vm_detail.build_vm_detail(
        vm, mid, astorage=storage, persistence_mode=mode, s3_read=s3_read,
        vm_status_fetch=fetch, latest_versions=latest_versions, last_instance=last))
    return out, counts


def _reset():
    vm_detail._S3_CACHE.clear()
    vm_detail._PY_BY_IMAGE.clear()
    vm_detail._RESTORED_AT.clear()
    vm_detail._BASE_BY_IMAGE.clear()


LIVE = {"python": "3.11.9", "checkpoint_enabled": True, "dirty": True,
        "last_checkpoint_at": None, "last_save": None, "restored": False, "restored_from": None,
        "var_count": 38, "var_bytes": 429916160, "untracked": 3,
        "packages": ["a==1", "b", "c", "d", "e", "f", "polars==1.9.0"], "packages_total": 7}


def test_suspended():
    _reset()
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=_row(), log=[{"new_state": "RUNNING"}],
                     siblings=[{"microvm_id": "microvm-old", "launched_at": "2026-11-11T20:00:00+00:00"},
                               {"microvm_id": "microvm-a", "launched_at": "2026-11-12T00:30:00+00:00"}])
    d, c = _run(vm, "microvm-a", storage=st)
    check("suspended: VM never called", c["fetch"] == 0)
    check("suspended: vm_status skipped_not_running", d["vm_status"] == "skipped_not_running")
    check("suspended: checkpoint from S3", d["checkpoint"] and d["checkpoint"]["source"] == "s3"
          and d["checkpoint"]["vars"] == 46 and d["checkpoint"]["vars_approx"] is False
          and d["checkpoint"]["size_kb"] == 16793.6)
    check("suspended: size = checkpoint_total_kb, not the checkpoint_size_kb delta",
          d["checkpoint"]["size_kb"] != META["checkpoint_size_kb"])
    check("suspended: dirty is None", d["dirty"] is None)
    check("suspended: variables None (no live VM; count/size are on the checkpoint)",
          d["variables"] is None)
    check("suspended: packages from requirements (cleaned, sorted)",
          d["packages"] and d["packages"]["names"] == ["boto3", "polars==1.9.0", "requests==2.32.0"]
          and d["packages"]["total"] == 3)
    check("suspended: image outdated v3 < v4", d["image"] == {
        "name": "notebook-microvm-arm64-2048", "version": "3", "latest_version": "4", "outdated": True,
        "base": None})
    check("suspended: origin fresh + restored (earlier VM for the session)",
          d["origin"] and d["origin"]["source"] == "fresh" and d["origin"]["restored"] is True)
    check("suspended: restored_checkpoint_at = S3 time (save predates this VM? no → None)",
          d["origin"]["restored_checkpoint_at"] is None)
    check("suspended: python unknown without a cache", d["python"] is None)
    check("suspended: kind notebook", d["kind"] == "notebook")


def test_restored_at():
    _reset()
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=_row(launched_at="2026-11-12T01:05:00+00:00"), log=[{"new_state": "SUSPENDED"}],
                     siblings=[{"microvm_id": "microvm-old", "launched_at": "2026-11-11T20:00:00+00:00"}])
    d, _ = _run(vm, "microvm-a", storage=st)
    check("origin pool when the log starts SUSPENDED", d["origin"]["source"] == "pool")
    check("restored_checkpoint_at = S3 checkpointed_at when it predates the VM",
          d["origin"]["restored_checkpoint_at"] == META["checkpointed_at"])


def test_running():
    _reset()
    vm = FakeVm("RUNNING")
    st = FakeStorage(row=_row(state="RUNNING"), log=[{"new_state": "SUSPENDED"}])
    d, c = _run(vm, "microvm-a", storage=st, live=LIVE)
    check("running: fetched once", c["fetch"] == 1)
    check("running: vm_status live", d["vm_status"] == "live")
    check("running: dirty True from the VM", d["dirty"] is True)
    check("running: variables from the VM = untracked only", d["variables"] == {
        "untracked": 3, "source": "vm"})
    check("running: packages from the VM", d["packages"]["total"] == 7 and d["packages"]["source"] == "vm")
    check("running: python from the VM", d["python"] == "3.11.9")
    check("running: S3 checkpoint used when the VM hasn't saved", d["checkpoint"]["source"] == "s3")
    check("running: origin pool", d["origin"]["source"] == "pool" and d["origin"]["restored"] is False)
    check("running: no last_suspend_save key when the VM reports none",
          "last_suspend_save" not in d["checkpoint"])
    sus_live = dict(LIVE, last_suspend_save={"status": "timed_out", "reason": "budget exceeded",
                                             "duration_s": 45.2, "bytes": 0, "saved_at": "2026-01-01T00:00:00+00:00",
                                             "budget_s": 45.0, "committed": False})
    d, _ = _run(vm, "microvm-a", storage=st, live=sus_live)
    check("running: last_suspend_save from the VM is in the checkpoint block",
          d["checkpoint"].get("last_suspend_save") == {"status": "timed_out", "reason": "budget exceeded",
                                                       "duration_s": 45.2, "bytes": 0,
                                                       "saved_at": "2026-01-01T00:00:00+00:00", "committed": False})
    d, _ = _run(vm, "microvm-a", storage=st, live=dict(LIVE, last_suspend_save={"status": "weird"}))
    check("running: unknown suspend status dropped", "last_suspend_save" not in d["checkpoint"])

    newer = dict(LIVE, last_checkpoint_at=time.time(),
                 last_save={"vars": 140, "user_vars": 40, "pkl_kb": 0.0, "size_kb": 100.0,
                            "total_kb": 18000.0})
    d, _ = _run(vm, "microvm-a", storage=st, live=newer)
    check("running: newer live save wins over S3 (user_vars preferred, total_kb as the size)",
          d["checkpoint"]["source"] == "vm"
          and d["checkpoint"]["vars"] == 40 and d["checkpoint"]["vars_approx"] is False
          and d["checkpoint"]["size_kb"] == 18000.0)
    # Old image: only the size_kb/pkl_kb deltas -> size unknown, never a delta, never "0 B".
    delta_only = dict(LIVE, last_checkpoint_at=time.time(),
                      last_save={"vars": 140, "user_vars": 40, "pkl_kb": 0.0, "size_kb": 100.0})
    d, _ = _run(vm, "microvm-a", storage=st, live=delta_only)
    check("running: newer save without total_kb → no size (size_kb delta not shown)",
          d["checkpoint"]["size_kb"] is None)
    delta0 = dict(LIVE, last_checkpoint_at=time.time(), last_save={"vars": 140, "user_vars": 40, "pkl_kb": 0.0})
    d, _ = _run(vm, "microvm-a", storage=st, live=delta0)
    check("running: 0 KB delta pickle is not reported as the checkpoint size", d["checkpoint"]["size_kb"] is None)
    # The VM stamps its save time just after writing metadata.json: same save -> S3 metadata wins.
    from datetime import datetime as _dt
    s3_epoch = _dt.fromisoformat(META["checkpointed_at"]).timestamp()
    same = dict(LIVE, last_checkpoint_at=s3_epoch + 2.0, last_save={"vars": 199, "pkl_kb": 0.0})
    d, _ = _run(vm, "microvm-a", storage=st, live=same)
    check("running: same save, old image (no total_kb) → metadata checkpoint_total_kb",
          d["checkpoint"]["source"] == "s3" and d["checkpoint"]["vars"] == 46
          and d["checkpoint"]["vars_approx"] is False and d["checkpoint"]["size_kb"] == 16793.6)
    same_t = dict(LIVE, last_checkpoint_at=s3_epoch + 2.0,
                  last_save={"vars": 199, "pkl_kb": 0.0, "size_kb": 4.0, "total_kb": 17000.0})
    d, _ = _run(vm, "microvm-a", storage=st, live=same_t)
    check("running: same save → the live last_save.total_kb is preferred",
          d["checkpoint"]["source"] == "s3" and d["checkpoint"]["size_kb"] == 17000.0)
    d, c = _run(vm, "microvm-a", storage=st, live=same_t)
    check("running: still one /vm-status fetch per detail (no extra VM call)", c["fetch"] == 1)
    d, _ = _run(vm, "microvm-a", storage=st, live=same_t,
                s3={"metadata": dict(META, checkpoint_total_kb=0), "requirements": REQS})
    check("running: same save, live total wins even when metadata total is 0",
          d["checkpoint"]["size_kb"] == 17000.0)
    older_vm = dict(LIVE, last_checkpoint_at=time.time(), last_save={"vars": 140, "pkl_kb": 100.0})
    d, _ = _run(vm, "microvm-a", storage=st, live=older_vm)
    check("running: older VM image without user_vars → total, vars_approx",
          d["checkpoint"]["vars"] == 140 and d["checkpoint"]["vars_approx"] is True)

    old = vm_detail.VM_STATUS_TIMEOUT_SEC
    vm_detail.VM_STATUS_TIMEOUT_SEC = 0.2
    try:
        t0 = time.monotonic()
        d, c = _run(vm, "microvm-a", storage=st, live=LIVE, live_delay=1.0)
        dt = time.monotonic() - t0
    finally:
        vm_detail.VM_STATUS_TIMEOUT_SEC = old
    check(f"running: slow fetch times out ({dt:.2f}s) → unavailable", d["vm_status"] == "unavailable" and dt < 0.9)
    check("running: no live → variables None", d["variables"] is None)
    check("running: dirty unknown after fallback", d["dirty"] is None)

    d, _ = _run(vm, "microvm-a", storage=st, live=None)
    check("running: None from fetch → unavailable", d["vm_status"] == "unavailable")

    vm.active_microvms["microvm-a"] = {"_502_strikes": 3}
    d, c = _run(vm, "microvm-a", storage=st, live=LIVE)
    check("unhealthy (3 strikes): no fetch", c["fetch"] == 0)
    vm.active_microvms.clear()


def test_stale_state_guard():
    _reset()
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=_row(state="RUNNING"))
    last = {"microvm-a": {"state": "RUNNING", "image_arn": ARN, "image_version": "3"}}
    d, c = _run(vm, "microvm-a", storage=st, live=LIVE, last=last)
    check("stale cache RUNNING but control plane SUSPENDED → no fetch", c["fetch"] == 0)
    check("state reported from the control plane", d["state"] == "SUSPENDED")


def test_pool_spare():
    _reset()
    vm_detail._PY_BY_IMAGE[(ARN, "3")] = "3.11.9"
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=None, spares=[{"vm_id": "microvm-spare"}])
    d, c = _run(vm, "microvm-spare", storage=st, live=LIVE)
    check("spare: kind pool_spare", d["kind"] == "pool_spare")
    check("spare: no fetch, no S3 read, no state probe", c["fetch"] == 0 and c["s3"] == 0 and vm.state_calls == 0)
    check("spare: notebook parts are None", all(d[k] is None for k in
                                                 ("checkpoint", "variables", "packages", "origin", "dirty")))
    check("spare: python from the per-image cache", d["python"] == "3.11.9")
    check("spare: image present", d["image"]["name"] == "notebook-microvm-arm64-2048")
    check("spare: vm_status not_applicable", d["vm_status"] == "not_applicable")


def test_outdated_and_modes():
    check("outdated ('3','4') → True", vm_detail.is_outdated("3", "4") is True)
    check("outdated ('4','4') → False", vm_detail.is_outdated("4", "4") is False)
    check("outdated ('abc','4') → False", vm_detail.is_outdated("abc", "4") is False)
    check("outdated ('1.2','1.10') → True", vm_detail.is_outdated("1.2", "1.10") is True)
    _reset()
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=_row())
    d, c = _run(vm, "microvm-a", storage=st, mode="none")
    check("mode none: checkpoint None, no S3 read", d["checkpoint"] is None and c["s3"] == 0)
    st2 = FakeStorage(row=_row(checkpoint_enabled=0))
    d, c = _run(vm, "microvm-a", storage=st2, mode="eternal")
    check("checkpoint_enabled false: checkpoint None, no S3 read", d["checkpoint"] is None and c["s3"] == 0)
    d, c = _run(vm, "microvm-a", storage=st, mode="eternal")
    check("eternal mode: checkpoint applies", d["checkpoint"] is not None and d["checkpoint"]["applies"])
    _reset()
    d, c = _run(vm, "microvm-a", storage=st, s3={"metadata": None, "requirements": None})
    check("no metadata → 'No checkpoint yet' shape", d["checkpoint"] == {
        "applies": True, "saved_at": None, "vars": None, "vars_approx": False, "size_kb": None,
        "source": "none"})


def test_s3_cache():
    _reset()
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=_row())
    n = {"s3": 0}

    async def s3_read(sid):
        n["s3"] += 1
        return {"metadata": META, "requirements": REQS}

    async def nofetch(m):
        raise AssertionError("must not fetch")

    async def lv():
        return {}

    async def two():
        for _ in range(2):
            await vm_detail.build_vm_detail(vm, "microvm-a", astorage=st, persistence_mode="checkpoint",
                                            s3_read=s3_read, vm_status_fetch=nofetch,
                                            latest_versions=lv, last_instance={})
    asyncio.run(two())
    check("S3 cache: two builds within 60 s → one S3 read", n["s3"] == 1)


def test_latest_image_versions():
    from proxy.platform.routes import metrics

    class Client:
        def __init__(self, fail=False):
            self.calls, self.fail = 0, fail

        def list_microvm_images(self):
            self.calls += 1
            if self.fail:
                raise RuntimeError("boom")
            return {"items": [{"imageArn": ARN, "name": "notebook-microvm-arm64-2048",
                               "latestActiveImageVersion": 4},
                              {"imageArn": "x", "name": "no-version"}]}

    cl = Client()
    vm = types.SimpleNamespace(get_lambda_poll_client=lambda: cl)
    metrics._IMAGE_VERSIONS_CACHE.update(value=None, ts=0.0)

    async def go():
        return await metrics.latest_image_versions(vm), await metrics.latest_image_versions(vm)
    a, b = asyncio.run(go())
    check("latest_image_versions: one control-plane call for two lookups", cl.calls == 1)
    check("latest_image_versions: by arn and by name", a.get(ARN) == "4"
          and a.get("notebook-microvm-arm64-2048") == "4" and a == b)
    check("latest_image_versions: image without a version skipped", "no-version" not in a)
    metrics._IMAGE_VERSIONS_CACHE.update(value=None, ts=0.0)
    bad = Client(fail=True)
    vm2 = types.SimpleNamespace(get_lambda_poll_client=lambda: bad)
    check("latest_image_versions: failure → {}", asyncio.run(metrics.latest_image_versions(vm2)) == {})
    check("latest_image_versions: failure not cached", metrics._IMAGE_VERSIONS_CACHE["value"] is None)


def test_old_checkpoint_fallback():
    _reset()
    vm = FakeVm("SUSPENDED")
    st = FakeStorage(row=_row(), log=[{"new_state": "RUNNING"}])
    d, _ = _run(vm, "microvm-a", storage=st, s3={"metadata": OLD_META, "requirements": REQS})
    check("old checkpoint: checkpoint vars = variables_count, vars_approx",
          d["checkpoint"]["vars"] == 186 and d["checkpoint"]["vars_approx"] is True)
    check("old checkpoint: no checkpoint_total_kb → size None (not the delta)",
          d["checkpoint"]["size_kb"] is None)
    check("old checkpoint: variables None", d["variables"] is None)
    d, _ = _run(vm, "microvm-a", storage=st,
                s3={"metadata": dict(META, checkpoint_total_kb=0), "requirements": REQS})
    check("checkpoint_total_kb 0 → size None (never 0 B)", d["checkpoint"]["size_kb"] is None)
    d, _ = _run(vm, "microvm-a", storage=st,
                s3={"metadata": dict(META, checkpoint_total_kb=True), "requirements": REQS})
    check("checkpoint_total_kb bool → size None", d["checkpoint"]["size_kb"] is None)


BASE_ARN = "arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1"


def test_base_image():
    _reset()
    calls = []
    mono = {"t": 1000.0}
    mode = {"fail": False}

    async def base(arn, ver):
        calls.append((arn, ver))
        if mode["fail"]:
            raise RuntimeError("boom")
        return {"arn": BASE_ARN, "version": "3.1"}

    async def s3_read(sid):
        return {"metadata": META, "requirements": REQS}

    async def fetch(m):
        raise AssertionError("VM called")

    async def latest():
        return {ARN: "1.0"}

    def build(last):
        vm = FakeVm("SUSPENDED")
        return asyncio.run(vm_detail.build_vm_detail(
            vm, "microvm-a", astorage=FakeStorage(row=_row()), persistence_mode="checkpoint",
            s3_read=s3_read, vm_status_fetch=fetch, latest_versions=latest, last_instance=last,
            base_image=base, monotonic=lambda: mono["t"]))
    last = {"microvm-a": {"state": "SUSPENDED", "image_arn": ARN, "image_version": "1.0"}}
    d = build(last)
    check("base: from the image-version lookup",
          d["image"]["base"] == {"name": "al2023-1", "arn": BASE_ARN, "version": "3.1"})
    check("base: fetcher gets only (image arn, image version), never the VM endpoint",
          calls == [(ARN, "1.0")])
    build(last)
    check("base: cached per image version (no second fetch)", len(calls) == 1)
    _reset()
    calls.clear()
    mode["fail"] = True
    d = build(last)
    check("base failure: base None, image otherwise intact",
          d["image"]["base"] is None and d["image"]["name"] == "notebook-microvm-arm64-2048"
          and d["image"]["version"] == "1.0" and d["image"]["latest_version"] == "1.0")
    mode["fail"] = False
    mono["t"] += 299
    build(last)
    check("base failure: not retried before 300 s", len(calls) == 1)
    mono["t"] += 2
    d = build(last)
    check("base failure: retried after 300 s", len(calls) == 2 and d["image"]["base"]["version"] == "3.1")
    _reset()
    calls.clear()
    d = build({"microvm-a": {"state": "SUSPENDED", "image_arn": ARN, "image_version": None}})
    check("no image version: no lookup, base None", calls == [] and d["image"]["base"] is None)


def test_route_validation():
    from proxy.platform.routes import microvm as routes
    check("id validation accepts microvm-abc123", routes._valid_microvm_id("microvm-abc123"))
    check("id validation rejects ../ and junk", not routes._valid_microvm_id("../etc")
          and not routes._valid_microvm_id("microvm-a/b") and not routes._valid_microvm_id(""))


def main():
    for fn in (test_suspended, test_restored_at, test_running, test_stale_state_guard, test_pool_spare,
               test_outdated_and_modes, test_s3_cache, test_latest_image_versions, test_old_checkpoint_fallback,
               test_base_image, test_route_validation):
        print(fn.__name__)
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    sys.exit(main())
