#!/usr/bin/env python3
# test-kind: offline
"""
Per-arch pricing lookup (x86_64 support).

Loads MICROVM_PRICING_JSON exactly as scripts/config.sh exports it (sourced in
a subprocess with a stub `aws` first on PATH, so no AWS call is made), reloads
proxy.platform.cost_tracker over it, and asserts:
  - 2048 MiB for 1 h RUNNING costs $0.12610 on arm64 and $0.148690 on x86_64;
  - x86_64 burst is priced at x86 rates; snapshot read/write are the same for
    both arches;
  - price_quantities echoes `architecture` and the effective `rates`;
  - an unknown arch falls back to the default arch's (arm64) rates;
  - the notebook-side estimate_session_cost agrees with the proxy for x86_64.

Run:
    .venv/bin/python tests/storage/test_pricing_by_arch.py
"""

import importlib
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _config_sh_pricing_json() -> str:
    stub_dir = tempfile.mkdtemp(prefix="pricing_stub_")
    try:
        stub = os.path.join(stub_dir, "aws")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(stub, 0o755)
        env = dict(os.environ)
        env["PATH"] = stub_dir + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            ["bash", "-c", 'source scripts/config.sh >/dev/null 2>&1; printf "%s" "$MICROVM_PRICING_JSON"'],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        return proc.stdout
    finally:
        shutil.rmtree(stub_dir, ignore_errors=True)


def _run():
    print("\n=== per-arch pricing (config.sh MICROVM_PRICING_JSON) ===")
    raw = _config_sh_pricing_json()
    check("config.sh exported a non-empty MICROVM_PRICING_JSON", bool(raw.strip()))
    saved = {k: os.environ.get(k) for k in ("MICROVM_PRICING_JSON", "DEFAULT_IMAGE_ARCH")}
    import proxy.platform.cost_tracker as ct
    try:
        os.environ["MICROVM_PRICING_JSON"] = raw
        os.environ["DEFAULT_IMAGE_ARCH"] = "arm64"
        ct = importlib.reload(ct)
        from app.notebook.helpers.cost import estimate_session_cost

        arm = ct.price_quantities(3600, 0, 0, 2048, architecture="arm64")
        x86 = ct.price_quantities(3600, 0, 0, 2048, architecture="x86_64")
        check(f"arm64 2 GB x 1 h == 0.12610 (got {arm['total_cost_usd']})",
              arm["total_cost_usd"] == 0.1261)
        check(f"x86_64 2 GB x 1 h == 0.148690 (got {x86['total_cost_usd']})",
              x86["total_cost_usd"] == 0.14869)
        check("x86_64 costs more than arm64 for the same usage",
              x86["total_cost_usd"] > arm["total_cost_usd"])

        # Burst: 2048 MB-seconds above the tier = 2 GB-s + 1 vCPU-s.
        xb = ct.price_quantities(0, 0, 2048, 2048, architecture="x86_64")
        exp_xb = round(1 * 0.0000326557 + 2 * 0.0000043235, 6)
        check(f"x86_64 burst uses x86 rates (== {exp_xb}, got {xb['burst_cost_usd']})",
              xb["burst_cost_usd"] == exp_xb)

        ar = ct.price_quantities(0, 0, 0, 4096, architecture="arm64", resume_count=2, suspend_count=1)
        xr = ct.price_quantities(0, 0, 0, 4096, architecture="x86_64", resume_count=2, suspend_count=1)
        check("snapshot read cost is identical across arches",
              ar["snapshot_read_cost_usd"] == xr["snapshot_read_cost_usd"] == round(2 * 4 * 0.00155, 6))
        check("snapshot write cost is identical across arches",
              ar["snapshot_write_cost_usd"] == xr["snapshot_write_cost_usd"] == round(1 * 4 * 0.00380, 6))
        xs = ct.price_quantities(0, 30 * 24 * 3600, 0, 1024, architecture="x86_64")
        check("x86_64 snapshot storage is $0.08/GB-month",
              abs(xs["suspended_cost_usd"] - 0.08) < 1e-6)

        check("x86_64 cost echoes architecture == 'x86_64'", x86.get("architecture") == "x86_64")
        check("x86_64 cost echoes the x86 rates",
              x86.get("rates") == {"vcpu_per_sec": 0.0000326557, "memory_per_gb_sec": 0.0000043235,
                                   "snapshot_per_gb_month": 0.08, "snapshot_read_per_gb": 0.00155,
                                   "snapshot_write_per_gb": 0.0038})
        check("arm64 cost echoes the arm64 vCPU/memory rates",
              arm["rates"]["vcpu_per_sec"] == 0.0000276944
              and arm["rates"]["memory_per_gb_sec"] == 0.0000036667)
        dflt = ct.price_quantities(3600, 0, 0, 2048)
        check("architecture omitted -> default arch arm64", dflt["architecture"] == "arm64"
              and dflt["total_cost_usd"] == arm["total_cost_usd"])

        unk = ct.price_quantities(3600, 0, 0, 2048, architecture="riscv")
        check("an unknown arch falls back to the default arch's rates",
              unk["total_cost_usd"] == arm["total_cost_usd"]
              and unk["rates"] == arm["rates"])

        nb = estimate_session_cost(3600, 600, 2048, 2048, architecture="x86_64",
                                   resume_count=1, suspend_count=1)
        px = ct.price_quantities(3600, 600, 2048, 2048, architecture="x86_64",
                                 resume_count=1, suspend_count=1)
        check("estimate_session_cost (notebook side) == price_quantities for x86_64",
              nb["total_cost_usd"] == px["total_cost_usd"])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(ct)


def main():
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL PRICING-BY-ARCH CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_pricing_by_arch():
    _checks.clear()
    _run()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pricing-by-arch failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
