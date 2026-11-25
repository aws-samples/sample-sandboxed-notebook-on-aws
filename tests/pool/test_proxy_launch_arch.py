#!/usr/bin/env python3
# test-kind: offline
"""
Proxy /launch architecture selection + /image-tiers arch filtering (x86_64
support).

Exercises proxy.platform.routes.microvm.launch_microvm with POOL_ENABLED=false
(cold launch through a fake vm_manager.run_vm) and asserts:
  (a) arch x86_64 / 2048 launches {IMAGE_ARN}-x86_64-2048 and reports x86_64;
  (b) an x86_64 size that arch doesn't offer snaps to the nearest x86 tier;
  (c) an unknown arch falls back to the default (arm64);
  (d) arch omitted -> arm64;
  (e) a restore pins the checkpoint's saved arch/memory over the request;
  (f) a restore whose saved arch is no longer offered runs on the requested
      arch and logs a warning;
and that GET /image-tiers (proxy.platform.routes.metrics.list_image_tiers)
offers only arches in IMAGE_ARCHES and sizes in each arch's size table.

Reuses the fakes from tests/pool/test_proxy_launch_grant.py. No AWS, no
network, no real DB.

Run:
    .venv/bin/python tests/pool/test_proxy_launch_arch.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.platform.routes.metrics as metrics_mod  # noqa: E402
import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from tests.pool.test_proxy_launch_grant import (  # noqa: E402
    EnvSandbox, _FakeAStorage, _FakeAsyncClient, _FakeRequest, _FakeResponse,
    _FakeVmManager, _build_request, _patch_module, run_async,
)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


_SIZES = {"arm64": [1024, 2048, 4096, 8192], "x86_64": [2048, 4096]}


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _launch(body_overrides, arches=("arm64", "x86_64"), saved=None):
    """Cold-launch once with the given body; returns (result, run_vm_calls, warnings)."""
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "false"
        os.environ["SESSION_PERSISTENCE_MODE"] = "checkpoint"
        vm_manager = _FakeVmManager()
        astorage = _FakeAStorage()
        if saved is not None:
            async def _list(session_id, _saved=saved):
                return list(_saved)
            astorage.vm_session_list_for_session = _list
        restore = _patch_module(vm_manager, astorage)
        orig_arches, orig_sizes = microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH
        microvm_mod.IMAGE_ARCHES = list(arches)
        microvm_mod._SIZES_BY_ARCH = {a: list(s) for a, s in _SIZES.items()}
        _FakeAsyncClient.behavior = lambda url, json: _FakeResponse(500, {})
        handler = _CaptureHandler()
        microvm_mod.logger.addHandler(handler)
        try:
            request = _build_request(vm_manager, session_id="sess-arch", **body_overrides)
            if "arch" in body_overrides and body_overrides["arch"] is None:
                request._body.pop("arch", None)
            result = run_async(microvm_mod.launch_microvm(request))
        finally:
            microvm_mod.logger.removeHandler(handler)
            microvm_mod.IMAGE_ARCHES, microvm_mod._SIZES_BY_ARCH = orig_arches, orig_sizes
            restore()
        return result, vm_manager.run_vm_calls, handler.messages


def _image(calls):
    return calls[0].get("imageIdentifier", "") if calls else ""


def test_launch_arch_selection():
    result, calls, _ = _launch({"arch": "x86_64", "memoryMiB": 2048})
    check("(a) x86_64/2048 launches the -x86_64-2048 image", _image(calls).endswith("-x86_64-2048"))
    check("(a) x86_64/2048 result reports architecture x86_64",
          isinstance(result, dict) and result.get("architecture") == "x86_64")

    _, calls, _ = _launch({"arch": "x86_64", "memoryMiB": 1024})
    check("(b) x86_64/1024 (not an x86 tier) snaps to the nearest x86 tier 2048",
          _image(calls).endswith("-x86_64-2048"))

    result, calls, _ = _launch({"arch": "riscv", "memoryMiB": 4096})
    check("(c) unknown arch 'riscv' falls back to arm64", _image(calls).endswith("-arm64-4096"))

    result, calls, _ = _launch({"arch": None, "memoryMiB": 1024})
    check("(d) arch omitted -> arm64", _image(calls).endswith("-arm64-1024")
          and result.get("architecture") == "arm64")

    _, calls, _ = _launch({"arch": "x86_64", "memoryMiB": 4096}, arches=("arm64",))
    check("x86_64 requested while IMAGE_ARCHES=arm64 -> arm64", _image(calls).endswith("-arm64-4096"))


def test_restore_keeps_saved_arch():
    saved = [{"architecture": "x86_64", "memory_mib": 4096}]
    _, calls, _ = _launch({"arch": "arm64", "memoryMiB": 2048, "restoreFromSession": "sess-old"},
                          saved=saved)
    check("(e) restore pins the checkpoint's arch+memory (-x86_64-4096) over the request",
          _image(calls).endswith("-x86_64-4096"))

    saved_arm = [{"architecture": "arm64", "memory_mib": 8192}]
    _, calls, _ = _launch({"arch": "x86_64", "memoryMiB": 2048, "restoreFromSession": "sess-old"},
                          saved=saved_arm)
    check("(e) an arm64 checkpoint restores on arm64 even if x86_64 is picked",
          _image(calls).endswith("-arm64-8192"))

    _, calls, warnings = _launch({"arch": "arm64", "memoryMiB": 2048, "restoreFromSession": "sess-old"},
                                 arches=("arm64",), saved=saved)
    check("(f) saved arch x86_64 no longer offered -> restores on arm64",
          _image(calls).endswith("-arm64-4096"))
    check("(f) ... and logs a warning naming the saved arch",
          any("saved on x86_64" in m for m in warnings))


# ── /image-tiers ─────────────────────────────────────────────────────────────
class _FakePollClient:
    def __init__(self, names):
        self._names = names

    def list_microvm_images(self):
        return {"items": [{"name": n, "state": "CREATED"} for n in self._names]}


class _TiersVmManager:
    def __init__(self, names):
        self._client = _FakePollClient(names)

    def get_lambda_poll_client(self):
        return self._client


_IMAGES = ([f"notebook-microvm-arm64-{m}" for m in (1024, 2048, 4096, 8192)]
           + ["notebook-microvm-x86_64-2048", "notebook-microvm-x86_64-4096",
              "notebook-microvm-x86_64-512", "other-image"])


def _tiers(arches):
    saved = (metrics_mod.IMAGE_ARN, metrics_mod.IMAGE_ARCHES, metrics_mod.IMAGE_BASE_NAME,
             microvm_mod._SIZES_BY_ARCH, dict(metrics_mod._IMAGE_TIERS_CACHE))
    metrics_mod.IMAGE_ARN = "arn:aws:lambda:us-west-2:123456789012:microvm-image:notebook-microvm"
    metrics_mod.IMAGE_ARCHES = list(arches)
    metrics_mod.IMAGE_BASE_NAME = "notebook-microvm"
    microvm_mod._SIZES_BY_ARCH = {a: list(s) for a, s in _SIZES.items()}
    metrics_mod._IMAGE_TIERS_CACHE.update({"value": None, "ts": 0.0})
    try:
        return run_async(metrics_mod.list_image_tiers(_FakeRequest(_TiersVmManager(_IMAGES), {})))
    finally:
        (metrics_mod.IMAGE_ARN, metrics_mod.IMAGE_ARCHES, metrics_mod.IMAGE_BASE_NAME,
         microvm_mod._SIZES_BY_ARCH, cache) = saved
        metrics_mod._IMAGE_TIERS_CACHE.clear()
        metrics_mod._IMAGE_TIERS_CACHE.update(cache)


def test_image_tiers_filter():
    both = _tiers(["arm64", "x86_64"])
    check("(g) /image-tiers arches == ['arm64', 'x86_64']", both["arches"] == ["arm64", "x86_64"])
    check("(g) /image-tiers defaultArch == arm64", both["defaultArch"] == "arm64")
    check("(g) arm64 tiers are 1024/2048/4096/8192",
          [t["memory_mib"] for t in both["tiersByArch"]["arm64"]] == [1024, 2048, 4096, 8192])
    check("(g) x86_64 tiers are 2048/4096 (512 filtered by the size table)",
          [t["memory_mib"] for t in both["tiersByArch"]["x86_64"]] == [2048, 4096])
    check("(g) x86_64 tiers carry arch x86_64",
          all(t["arch"] == "x86_64" for t in both["tiersByArch"]["x86_64"]))

    arm_only = _tiers(["arm64"])
    check("(g) IMAGE_ARCHES=arm64 -> /image-tiers offers arm64 only even with x86 images built",
          arm_only["arches"] == ["arm64"] and "x86_64" not in arm_only["tiersByArch"])


_SECTIONS = (test_launch_arch_selection, test_restore_keeps_saved_arch, test_image_tiers_filter)


def main():
    print("\n=== Proxy /launch arch selection + /image-tiers ===")
    for fn in _SECTIONS:
        print(f"\n-- {fn.__name__} --")
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PROXY LAUNCH ARCH CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_proxy_launch_arch():
    _checks.clear()
    for fn in _SECTIONS:
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "proxy launch arch failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
