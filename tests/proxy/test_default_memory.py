#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the proxy's single default memory tier
(proxy/platform/defaults.py::DEFAULT_MEMORY_MIB, code-review I-B-02 / E-I-02).

Covers:
  - no 4096 memory fallback is left in proxy/ source ("or 4096", ".get(..., 4096)",
    "memory_mib: int = 4096", "memory_mib = 4096");
  - the default is 2048 when DEFAULT_MEMORY_MIB is unset and the env override is
    honored (fresh interpreter each);
  - cost_tracker.price_quantities of a record with memory_mib=None prices at 2048;
  - launch_env_vars() forwards DEFAULT_MEMORY_MIB to the VM;
  - GET /image-tiers returns default_memory_mib.

Run:
    .venv/bin/python tests/proxy/test_default_memory.py
"""
import asyncio
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.pop("DEFAULT_MEMORY_MIB", None)

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


_FALLBACK_4096 = re.compile(
    r"or 4096\b|,\s*4096\)|memory_mib\s*(?::\s*int\s*)?=\s*4096\b|memoryMiB\"?,\s*4096")


def test_no_4096_fallback():
    print("\n-- source scan --")
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "proxy")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if _FALLBACK_4096.search(line):
                            hits.append(f"{os.path.relpath(path, ROOT)}:{i}")
    check("no 4096 memory fallback in proxy/", not hits, ", ".join(hits))


def _default_in_fresh_process(env_value):
    env = {k: v for k, v in os.environ.items() if k != "DEFAULT_MEMORY_MIB"}
    if env_value is not None:
        env["DEFAULT_MEMORY_MIB"] = env_value
    r = subprocess.run(
        [sys.executable, "-c",
         "from proxy.platform.defaults import DEFAULT_MEMORY_MIB as d; print(d)"],
        cwd=ROOT, env={**env, "PYTHONPATH": ROOT}, capture_output=True, text=True)
    return r.stdout.strip() or r.stderr.strip()


def test_env():
    print("\n-- env --")
    check("unset -> 2048", _default_in_fresh_process(None) == "2048")
    check("DEFAULT_MEMORY_MIB=4096 honored", _default_in_fresh_process("4096") == "4096")
    check("invalid value -> 2048", _default_in_fresh_process("lots").endswith("2048"))


def test_pricing_and_launch_env():
    print("\n-- pricing, VM env, /image-tiers --")
    from proxy.platform.cost_tracker import price_quantities, MicroVMCostRecord
    from proxy.platform.defaults import DEFAULT_MEMORY_MIB
    check("module default is 2048", DEFAULT_MEMORY_MIB == 2048, DEFAULT_MEMORY_MIB)
    q = dict(running_secs=3600.0, suspended_secs=600.0, burst_mb_seconds=0.0,
             architecture="arm64", resume_count=1, suspend_count=1)
    none_cost = price_quantities(memory_mib=None, **q)
    tier_cost = price_quantities(memory_mib=2048, **q)
    check("price_quantities(memory_mib=None) prices at 2048",
          none_cost["total_cost_usd"] == tier_cost["total_cost_usd"]
          and none_cost["total_cost_usd"] != price_quantities(memory_mib=4096, **q)["total_cost_usd"])
    check("MicroVMCostRecord default memory is 2048",
          MicroVMCostRecord(microvm_id="x").memory_mib == 2048)

    from proxy.platform.microvm_manager import MicrovmManager
    mgr = MicrovmManager()
    mgr.get_artifacts_bucket = lambda: None
    env = mgr.launch_env_vars()
    check("launch_env_vars forwards DEFAULT_MEMORY_MIB", env.get("DEFAULT_MEMORY_MIB") == "2048",
          env.get("DEFAULT_MEMORY_MIB"))

    import proxy.platform.routes.metrics as metrics
    orig = metrics.IMAGE_ARN
    metrics.IMAGE_ARN = ""
    try:
        body = asyncio.run(metrics.list_image_tiers(request=None))
    finally:
        metrics.IMAGE_ARN = orig
    check("/image-tiers returns default_memory_mib", body.get("default_memory_mib") == 2048, body)


def main():
    test_no_4096_fallback()
    test_env()
    test_pricing_and_launch_env()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
