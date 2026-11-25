#!/usr/bin/env python3
# test-kind: offline
"""
resume_count undercount after an idle auto-resume (vm-card-state).

Seen in the x86 live check: after an idle auto-resume, cost.resume_count stayed
at 1 while suspend_count reached 2. Two causes, both fixed:
  A. One-poll lag: GET /instances read costs BEFORE writing this poll's
     transitions, so the poll that first saw SUSPENDED→RUNNING returned the old
     count. Fix: the batched cost read now runs after the writes.
  B. Missed RUNNING windows: transitions were logged only by /instances polls, so
     an auto-resume that ran and re-suspended between polls left no RUNNING row.
     Fix: a successful /proxy/* forward records RUNNING (CostTracker.note_served
     + vm_session_update_state) when the tracker last saw the VM suspended.
  D. (review finding 1) That served RUNNING must not be overwritten by a poll
     whose list_microvms snapshot was taken before it (stale SUSPENDED /
     RESUMING), which would double count. /instances skips the write when
     CostTracker.served_since(mid, poll_started) is true.

Also covers CostTracker.usage_stats (peak memory, burst episodes) and that
/instances passes image_arn / image_version through.

Offline: temp SQLite (tests/pool/_poolkit._run_sqlite), a fake vm_manager.
Run:
    .venv/bin/python tests/proxy/test_resume_count_autoresume.py
"""
import asyncio
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from tests.pool._poolkit import _run_sqlite  # noqa: E402
from proxy.platform.cost_tracker import CostTracker  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


MID = "microvm-test-autoresume"
ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:notebook-microvm-arm64-2048"


class FakeVmManager:
    def __init__(self):
        self.cost_tracker = CostTracker()
        self.active_microvms = {}
        self.script = []  # states returned by successive list_vms calls
        self.calls = 0
        self.on_list = None  # optional async hook run after the snapshot is taken

    async def list_vms(self):
        state = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if self.on_list is not None:
            await self.on_list(self.calls)
        return {"items": [{"microvmId": MID, "state": state, "imageArn": ARN, "imageVersion": "3"}]}

    async def get_microvm_detail(self, mid):
        return {"endpoint": "vm.example", "imageArn": ARN, "state": "RUNNING"}

    async def get_vm_state(self, mid):
        return "RUNNING"


def _wire(storage):
    """Point the route module, the storage package and the tracker's lazy import
    at the temp SQLite backend."""
    import proxy.storage as ps
    from proxy.storage.async_storage import AsyncStorage
    from proxy.platform.routes import microvm as routes
    from proxy.platform.datasources import registry
    a = AsyncStorage(storage)
    ps.storage = storage
    ps.astorage = a
    routes.storage = storage
    routes.astorage = a
    registry.maybe_refresh_catalog_in_background = lambda *a, **k: False
    routes._vm_missing_polls.clear()
    return routes


def _request(vm):
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=vm)))


def _new_vm(storage):
    storage.vm_session_create(MID, notebook_id=None, session_id="s-auto", memory_mib=2048,
                              endpoint="vm.example", checkpoint_enabled=True, architecture="arm64")
    vm = FakeVmManager()
    vm.active_microvms[MID] = {"endpoint": "vm.example", "session_id": "s-auto",
                               "memory_mib": 2048, "architecture": "arm64",
                               "launched_at": 0, "_502_strikes": 0}
    vm.cost_tracker.record(MID, "RUNNING", memory_mib=2048, architecture="arm64")
    return vm


def test_a_one_poll_lag():
    storage = _run_sqlite()
    routes = _wire(storage)
    vm = _new_vm(storage)
    vm.script = ["RUNNING", "SUSPENDED", "RUNNING"]
    req = _request(vm)

    async def go():
        out = []
        for _ in range(3):
            out.append(await routes.list_instances(req))
        return out
    polls = asyncio.run(go())
    third = polls[2]["instances"][MID]
    check("A: third poll's own response has resume_count == 2", third["cost"]["resume_count"] == 2)
    check("A: third poll's own response has suspend_count == 1", third["cost"]["suspend_count"] == 1)
    second = polls[1]["instances"][MID]
    check("A: second poll (first SUSPENDED) already counts the suspend", second["cost"]["suspend_count"] == 1)
    check("A: session_cost is still present", "session_cost" in third and third["session_cost"]["vm_count"] == 1)
    check("A: image_arn passed through", third.get("image_arn") == ARN)
    check("A: image_version passed through", third.get("image_version") == "3")
    check("A: _LAST_INSTANCE filled (state, image)",
          routes._LAST_INSTANCE.get(MID, {}).get("state") == "RUNNING"
          and routes._LAST_INSTANCE[MID].get("image_version") == "3")


def test_b_missed_running_window():
    storage = _run_sqlite()
    routes = _wire(storage)
    vm = _new_vm(storage)
    vm.script = ["RUNNING", "SUSPENDED", "SUSPENDED"]
    req = _request(vm)

    async def go():
        await routes.list_instances(req)
        await routes.list_instances(req)
        # Traffic auto-resumes the VM; the forward succeeds before AWS re-suspends it.
        await routes._note_vm_served(vm, MID)
        return await routes.list_instances(req)
    last = asyncio.run(go())
    cost = last["instances"][MID]["cost"]
    check("B: resume_count == 2 after an auto-resume between polls", cost["resume_count"] == 2)
    check("B: suspend_count == 2 after the idle re-suspend", cost["suspend_count"] == 2)
    states = [e["new_state"] for e in storage.vm_state_log_get(MID)]
    check(f"B: state log is RUNNING, SUSPENDED, RUNNING, SUSPENDED ({states})",
          states == ["RUNNING", "SUSPENDED", "RUNNING", "SUSPENDED"])

    # note_served on a RUNNING VM / an unknown id writes nothing.
    before = len(storage.vm_state_log_get(MID))
    vm.cost_tracker.record(MID, "RUNNING")
    storage.vm_session_update_state(MID, "RUNNING")
    before = len(storage.vm_state_log_get(MID))
    asyncio.run(routes._note_vm_served(vm, MID))
    check("note_served on RUNNING: no new log row", len(storage.vm_state_log_get(MID)) == before)
    check("note_served on RUNNING returns False", vm.cost_tracker.note_served(MID) is False)
    check("note_served on an unknown id returns False", vm.cost_tracker.note_served("microvm-nope") is False)
    check("note_served on an unknown id creates no record", vm.cost_tracker.last_state("microvm-nope") is None)


def _counts(routes, req):
    async def go():
        return await routes.list_instances(req)
    return asyncio.run(go())["instances"][MID]["cost"]


def test_d_stale_snapshot_race():
    """A forward served the VM AFTER the poll's list_microvms snapshot (SUSPENDED /
    RESUMING) was taken. The poll must not write that stale state over the served
    RUNNING (no RUNNING → stale → RUNNING blip, no extra resume/suspend)."""
    for stale in ("SUSPENDED", "RESUMING"):
        # D1: the forward completes while list_vms is in flight (snapshot already taken).
        storage = _run_sqlite()
        routes = _wire(storage)
        vm = _new_vm(storage)
        vm.script = ["RUNNING", "SUSPENDED", stale, "RUNNING"]
        req = _request(vm)
        _counts(routes, req)                  # poll 1: RUNNING
        c2 = _counts(routes, req)             # poll 2: SUSPENDED (real)

        async def serve_during_list(n):
            if n == 3:
                await routes._note_vm_served(vm, MID)
        vm.on_list = serve_during_list
        c3 = _counts(routes, req)             # poll 3: stale snapshot, served mid-poll
        vm.on_list = None
        c4 = _counts(routes, req)             # poll 4: RUNNING
        states = [e["new_state"] for e in storage.vm_state_log_get(MID)]
        check(f"D1[{stale}]: log has no blip ({states})",
              states == ["RUNNING", "SUSPENDED", "RUNNING"])
        check(f"D1[{stale}]: resume_count 2 after poll 3 and poll 4 ({c3['resume_count']}, {c4['resume_count']})",
              c3["resume_count"] == 2 and c4["resume_count"] == 2)
        check(f"D1[{stale}]: suspend_count stays 1 ({c2['suspend_count']}, {c3['suspend_count']}, {c4['suspend_count']})",
              c2["suspend_count"] == 1 and c3["suspend_count"] == 1 and c4["suspend_count"] == 1)
        check(f"D1[{stale}]: tracker last state RUNNING", vm.cost_tracker.last_state(MID) == "RUNNING")

    # D2: the forward completes later in the loop (during the per-VM DB read).
    storage = _run_sqlite()
    routes = _wire(storage)
    vm = _new_vm(storage)
    vm.script = ["RUNNING", "SUSPENDED", "SUSPENDED", "RUNNING"]
    req = _request(vm)
    _counts(routes, req)
    _counts(routes, req)
    orig_get = routes.astorage.vm_session_get
    fired = {"n": 0}

    async def get_and_serve(mid):
        row = await orig_get(mid)
        if fired["n"] == 0:
            fired["n"] = 1
            await routes._note_vm_served(vm, mid)
        return row
    routes.astorage.vm_session_get = get_and_serve
    c3 = _counts(routes, req)
    routes.astorage.vm_session_get = orig_get
    c4 = _counts(routes, req)
    states = [e["new_state"] for e in storage.vm_state_log_get(MID)]
    check(f"D2: log has no blip ({states})", states == ["RUNNING", "SUSPENDED", "RUNNING"])
    check("D2: resume_count 2, suspend_count 1 after both polls",
          (c3["resume_count"], c3["suspend_count"], c4["resume_count"], c4["suspend_count"]) == (2, 1, 2, 1))

    # D3: a real suspend AFTER the served RUNNING (next poll started later) is recorded.
    vm.script = ["SUSPENDED"]
    vm.calls = 0
    c5 = _counts(routes, req)
    states = [e["new_state"] for e in storage.vm_state_log_get(MID)]
    check(f"D3: later real suspend is logged ({states})",
          states == ["RUNNING", "SUSPENDED", "RUNNING", "SUSPENDED"] and c5["suspend_count"] == 2)

    # served_since unit checks.
    t = CostTracker()
    check("served_since: unknown → False", t.served_since("vm-s", 0) is False)
    t.record("vm-s", "SUSPENDED")
    import time as _time
    ts = _time.time()
    check("served_since: last SUSPENDED → False", t.served_since("vm-s", 0) is False)
    t.note_served("vm-s")
    check("served_since: RUNNING at/after ts → True", t.served_since("vm-s", ts) is True)
    check("served_since: RUNNING before ts → False", t.served_since("vm-s", _time.time() + 1) is False)


def test_usage_stats():
    t = CostTracker()
    t.record("vm-u", "RUNNING", memory_mib=2048)
    check("usage_stats: no samples → None", t.usage_stats("vm-u") is None)
    check("usage_stats: unknown → None", t.usage_stats("vm-x") is None)
    for mb in (1500, 2100, 2200, 1800, 2300):
        t.record_burst("vm-u", mb)
    u = t.usage_stats("vm-u")
    check(f"usage_stats: peak 2300 ({u})", u and u["peak_mem_mb"] == 2300)
    check("usage_stats: 2 burst episodes", u and u["burst_episodes"] == 2)
    check("usage_stats: memory_mb is the tier", u and u["memory_mb"] == 2048)
    c = t.get_costs_many  # cost dict is unchanged by the new fields
    keys = set(t._records["vm-u"].compute().keys())
    check("cost dict has no usage keys", "peak_mem_mb" not in keys and "burst_episodes" not in keys and c)


def main():
    print("Section A: one-poll lag")
    test_a_one_poll_lag()
    print("Section B: missed RUNNING window (auto-resume between polls)")
    test_b_missed_running_window()
    print("Section D: stale /instances snapshot vs a served forward (no double count)")
    test_d_stale_snapshot_race()
    print("Section C: usage stats")
    test_usage_stats()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    sys.exit(main())
