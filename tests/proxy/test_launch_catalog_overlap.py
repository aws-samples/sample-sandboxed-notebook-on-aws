#!/usr/bin/env python3
# test-kind: offline
"""
/launch concurrency (restore-speedups item 3 / FEAT-002 F).

proxy.platform.routes.microvm.launch_microvm:
  - stages a NON-EMPTY launch catalog to S3 concurrently with the pool grant.
    A pool hit inlines catalog_entries (catalog_s3_key None) and returns without
    waiting for the PUT; a pool miss / pool disabled awaits the PUT before run_vm
    and passes the staged key in runHookPayload. An EMPTY catalog is awaited
    before the grant, as before, and leaves no background task;
  - runs the restore pre-work (manifest HEAD + checkpoint size lookup)
    concurrently with the vm_session_list_for_session storage read, with the
    same semantics: HEAD False drops the restore (no restore_memory_check), a
    HEAD error keeps it, the override 400 path returns 400 with no
    "never retrieved" noise;
  - reads the notebook row once on a no-sessionId relink launch.
Fakes from tests/pool/test_proxy_launch_grant. No AWS, no network.
Run:
    .venv/bin/python tests/proxy/test_launch_catalog_overlap.py
"""
import asyncio
import gc
import json
import logging
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
import proxy.notebook.session_relink as session_relink  # noqa: E402
from tests.pool import test_proxy_launch_grant as tl  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {str(detail)[:400]}" if not cond else ""))
    if not cond:
        _fails.append(label)


_ENTRIES = [{"name": "s3://bucket/a.csv", "kind": "s3", "columns": ["x"]}]
_STAGED_KEY = "sessions/sess/launch_catalog.json"


class _VmManager(tl._FakeVmManager):
    def __init__(self, entries, events, gate=None, head=False, head_event=None):
        super().__init__()
        self._entries = entries
        self.events = events
        self.gate = gate
        self._head = head
        self._head_event = head_event
        self.stage_done = threading.Event()

    async def build_cloud_catalog_entries_async(self, session_id=None):
        return list(self._entries)

    def stage_catalog_to_s3(self, session_id, catalog_entries):
        self.events.append("stage_start")
        if not catalog_entries:
            self.events.append("stage_done")
            self.stage_done.set()
            return None
        if self.gate is not None:
            self.gate.wait(5)
        self.events.append("stage_done")
        self.stage_done.set()
        return _STAGED_KEY

    def checkpoint_manifest_exists(self, session_id):
        self.events.append("head")
        if self._head_event is not None:
            self._head_event.set()
        if isinstance(self._head, BaseException):
            raise self._head
        return self._head

    async def run_vm(self, **params):
        self.events.append("run_vm")
        self.run_vm_stage_done = self.stage_done.is_set()
        return await super().run_vm(**params)


class _AStorage(tl._FakeAStorage):
    def __init__(self, head_event=None, prior=None):
        super().__init__()
        self.notebook_get_calls = 0
        self._head_event = head_event
        self._prior = prior or []
        self.head_seen_before_list_returned = None

    async def notebook_get(self, notebook_id):
        self.notebook_get_calls += 1
        return None

    async def vm_session_list_for_session(self, session_id):
        if self._head_event is not None:
            self.head_seen_before_list_returned = await asyncio.to_thread(self._head_event.wait, 2)
        return list(self._prior)


async def _run_launch(vm, astorage, body, pool_enabled, grant_result, events,
                      release_gate_after=None, after_return=None):
    """Run launch_microvm with patched seams; returns (result, ctx)."""
    ctx = {"grant_payloads": [], "size_calls": [], "loop_errors": [],
           "bg_after_return": None, "stage_done_at_return": None}
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda lp, c: ctx["loop_errors"].append(c.get("message")))

    async def _fake_grant(payload):
        events.append("grant")
        ctx["grant_payloads"].append(payload)
        return grant_result

    async def _fake_size(vm_manager, sid):
        ctx["size_calls"].append(sid)
        return 2048, "metadata"

    async def _fake_report(vm_manager, microvm_id, endpoint):
        return {"verified": True}

    saved = (microvm_mod._try_pool_grant, session_relink.restore_checkpoint_size,
             microvm_mod._launch_restore_report, microvm_mod.dag_service.attach_soon,
             session_relink.relinked_session_id)
    microvm_mod._try_pool_grant = _fake_grant
    session_relink.restore_checkpoint_size = _fake_size
    microvm_mod._launch_restore_report = _fake_report
    microvm_mod.dag_service.attach_soon = lambda *a, **k: None

    async def _no_relink(row, vm_manager):
        return None
    session_relink.relinked_session_id = _no_relink
    restore = tl._patch_module(vm, astorage)
    try:
        with tl.EnvSandbox():
            os.environ["POOL_ENABLED"] = "true" if pool_enabled else "false"
            os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
            rel = None
            if release_gate_after is not None and vm.gate is not None:
                async def _release():
                    await asyncio.sleep(release_gate_after)
                    vm.gate.set()
                rel = asyncio.create_task(_release())
            result = await microvm_mod.launch_microvm(tl._FakeRequest(vm, body))
            ctx["stage_done_at_return"] = vm.stage_done.is_set()
            ctx["bg_after_return"] = len(microvm_mod._BACKGROUND_TASKS)
            if after_return is not None:
                after_return()
            if vm.gate is not None:
                vm.gate.set()
            if rel is not None:
                await rel
            pending = list(microvm_mod._BACKGROUND_TASKS)
            if pending:
                await asyncio.wait(pending, timeout=5)
            await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
    finally:
        restore()
        (microvm_mod._try_pool_grant, session_relink.restore_checkpoint_size,
         microvm_mod._launch_restore_report, microvm_mod.dag_service.attach_soon,
         session_relink.relinked_session_id) = saved
    return result, ctx


def _body(session_id="sess-ov", **kw):
    b = {"name": "nb-ov", "sessionId": session_id, "memoryMiB": 2048, "arch": "arm64"}
    b.update(kw)
    return b


_HIT = {"vm_id": "pool-vm-1", "endpoint": "pool-ep-1"}


def _status(result):
    return getattr(result, "status_code", 200 if isinstance(result, dict) else None)


def test_pool_hit_does_not_wait_for_staging():
    events = []
    vm = _VmManager(_ENTRIES, events, gate=threading.Event())
    r, ctx = asyncio.run(_run_launch(vm, _AStorage(), _body(), True, _HIT, events))
    check("pool hit: launch returns the granted VM", isinstance(r, dict) and r.get("microvmId") == "pool-vm-1", r)
    check("pool hit: returned while staging still blocked", ctx["stage_done_at_return"] is False)
    gp = ctx["grant_payloads"][0] if ctx["grant_payloads"] else {}
    check("pool hit: grant payload inlines catalog_entries", gp.get("catalog_entries") == _ENTRIES, gp)
    check("pool hit: grant payload catalog_s3_key is None", "catalog_s3_key" in gp and gp["catalog_s3_key"] is None, gp)
    check("pool hit: staging started (concurrently)", "stage_start" in events, events)
    check("pool hit: staging completes afterwards", vm.stage_done.is_set() and events[-1] == "stage_done", events)
    check("pool hit: run_vm not called", vm.run_vm_calls == [])
    check("pool hit: no background task left", not microvm_mod._BACKGROUND_TASKS)
    check("pool hit: no loop errors", not ctx["loop_errors"], ctx["loop_errors"])


def test_pool_miss_awaits_staging_before_run_vm():
    events = []
    vm = _VmManager(_ENTRIES, events, gate=threading.Event())
    r, ctx = asyncio.run(_run_launch(vm, _AStorage(), _body(), True, None, events,
                                     release_gate_after=0.1))
    check("pool miss: cold launch succeeds", isinstance(r, dict) and r.get("microvmId") == "cold-vm-1", r)
    check("pool miss: grant attempted before staging finished",
          "grant" in events and events.index("grant") < events.index("stage_done"), events)
    check("pool miss: run_vm only after staging finished",
          getattr(vm, "run_vm_stage_done", False) and events.index("stage_done") < events.index("run_vm"), events)
    payload = json.loads(vm.run_vm_calls[0]["runHookPayload"]) if vm.run_vm_calls else {}
    check("pool miss: runHookPayload catalog_s3_key is the staged key",
          payload.get("catalog_s3_key") == _STAGED_KEY, payload.get("catalog_s3_key"))
    check("pool miss: executionRoleArn set", bool(vm.run_vm_calls and vm.run_vm_calls[0].get("executionRoleArn")))
    check("pool miss: no loop errors", not ctx["loop_errors"], ctx["loop_errors"])


def test_empty_catalog_awaited_before_grant():
    events = []
    vm = _VmManager([], events)
    r, ctx = asyncio.run(_run_launch(vm, _AStorage(), _body(), True, _HIT, events))
    check("empty catalog: launch returns the granted VM", isinstance(r, dict) and r.get("microvmId") == "pool-vm-1", r)
    check("empty catalog: staged/awaited before the grant",
          events[:3] == ["stage_start", "stage_done", "grant"], events)
    gp = ctx["grant_payloads"][0] if ctx["grant_payloads"] else {}
    check("empty catalog: no inlined entries, key None",
          "catalog_entries" not in gp and gp.get("catalog_s3_key") is None, gp)
    check("empty catalog: no background task left at return", ctx["bg_after_return"] == 0, ctx["bg_after_return"])


def test_pool_disabled_awaits_staging():
    events = []
    vm = _VmManager(_ENTRIES, events, gate=threading.Event())
    r, ctx = asyncio.run(_run_launch(vm, _AStorage(), _body(), False, _HIT, events,
                                     release_gate_after=0.1))
    check("pool disabled: cold launch succeeds", isinstance(r, dict) and r.get("microvmId") == "cold-vm-1", r)
    check("pool disabled: grant never attempted", "grant" not in events, events)
    check("pool disabled: run_vm only after staging finished",
          getattr(vm, "run_vm_stage_done", False), events)
    payload = json.loads(vm.run_vm_calls[0]["runHookPayload"]) if vm.run_vm_calls else {}
    check("pool disabled: runHookPayload has the staged key", payload.get("catalog_s3_key") == _STAGED_KEY)


def test_restore_prework_concurrent():
    events = []
    head_ev = threading.Event()
    vm = _VmManager([], events, head=True, head_event=head_ev)
    st = _AStorage(head_event=head_ev)
    r, ctx = asyncio.run(_run_launch(vm, st, _body(restoreFromSession="sess-old"), True, _HIT, events))
    check("restore: HEAD started before vm_session_list_for_session returned",
          st.head_seen_before_list_returned is True, st.head_seen_before_list_returned)
    gp = ctx["grant_payloads"][0] if ctx["grant_payloads"] else {}
    check("restore: grant carries restore_from", gp.get("restore_from") == "sess-old", gp)
    check("restore: size lookup ran once", ctx["size_calls"] == ["sess-old"], ctx["size_calls"])
    check("restore: restore_memory_check in response",
          isinstance(r, dict) and isinstance(r.get("restore_memory_check"), dict)
          and r["restore_memory_check"].get("source") == "metadata", r)


def test_restore_head_false_skips():
    events = []
    vm = _VmManager([], events, head=False)
    r, ctx = asyncio.run(_run_launch(vm, _AStorage(), _body(restoreFromSession="sess-old"), True, _HIT, events))
    gp = ctx["grant_payloads"][0] if ctx["grant_payloads"] else {}
    check("HEAD False: no restore_from in grant", "restore_from" not in gp, gp)
    check("HEAD False: no restore_memory_check / restore_report",
          isinstance(r, dict) and "restore_memory_check" not in r and "restore_report" not in r, r)
    check("HEAD False: no loop errors", not ctx["loop_errors"], ctx["loop_errors"])


def test_restore_head_error_proceeds():
    events = []
    vm = _VmManager([], events, head=RuntimeError("s3 blip"))
    r, ctx = asyncio.run(_run_launch(vm, _AStorage(), _body(restoreFromSession="sess-old"), True, _HIT, events))
    gp = ctx["grant_payloads"][0] if ctx["grant_payloads"] else {}
    check("HEAD error: restore proceeds", gp.get("restore_from") == "sess-old", gp)
    check("HEAD error: restore_memory_check present", isinstance(r, dict) and "restore_memory_check" in r, r)


def test_override_400_no_noise():
    events = []
    vm = _VmManager([], events, head=True)
    cap = []

    class _H(logging.Handler):
        def emit(self, rec):
            cap.append(rec.getMessage())
    h = _H(level=logging.DEBUG)
    logging.getLogger().addHandler(h)
    try:
        r, ctx = asyncio.run(_run_launch(
            vm, _AStorage(), _body(restoreFromSession="sess-old", restoreOverride=True, arch="bogus-arch"),
            True, _HIT, events))
        gc.collect()
    finally:
        logging.getLogger().removeHandler(h)
    check("override 400: returns 400", _status(r) == 400, r)
    check("override 400: no grant attempted", not ctx["grant_payloads"])
    noise = [m for m in ctx["loop_errors"] + cap if m and "never retrieved" in m]
    check("override 400: no 'never retrieved' warnings", not noise, noise)


def test_relink_single_notebook_get():
    events = []
    vm = _VmManager([], events)
    st = _AStorage()
    body = {"name": "nb-rl", "notebookId": 42, "memoryMiB": 2048, "arch": "arm64"}
    r, ctx = asyncio.run(_run_launch(vm, st, body, True, _HIT, events))
    check("relink: launch succeeds", isinstance(r, dict) and r.get("microvmId") == "pool-vm-1", r)
    check("relink: one notebook_get per launch", st.notebook_get_calls == 1, st.notebook_get_calls)


_SECTIONS = (test_pool_hit_does_not_wait_for_staging, test_pool_miss_awaits_staging_before_run_vm,
             test_empty_catalog_awaited_before_grant, test_pool_disabled_awaits_staging,
             test_restore_prework_concurrent, test_restore_head_false_skips,
             test_restore_head_error_proceeds, test_override_400_no_noise,
             test_relink_single_notebook_get)


if __name__ == "__main__":
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    print(f"\n{'ALL PASSED' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
