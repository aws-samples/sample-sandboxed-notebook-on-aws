#!/usr/bin/env python3
# test-kind: offline
"""
scripts/config.sh CPU-architecture block test (x86_64 support).

Sources scripts/config.sh in clean `bash -c` subprocesses and asserts:
  - us-west-2 defaults: IMAGE_ARCHES="arm64 x86_64", DEFAULT_IMAGE_ARCH=arm64,
    nothing dropped by the region guard;
  - both arches share the al2023-1 base image ARN and map to the
    create-microvm-image cpuConfigurations values ARM_64 / X86_64;
  - MICROVM_SIZES_JSON lists both arches; MICROVM_PRICING_JSON has the real
    (non-zero) x86_64 rates and the per-arch snapshot keys;
  - POOL_SIZE_OVERRIDES_X86_64 defaults to 1 spare at 2048, 0 elsewhere;
  - the region guard drops x86_64 in ap-east-1 / ap-southeast-5 /
    ap-southeast-7 / eu-south-2 (and only there), and an explicit
    IMAGE_ARCHES=arm64 is honored as-is.

No AWS calls: a stub `aws` that exits 1 is put first on PATH, so config.sh's
`aws sts get-caller-identity` (ACCOUNT_ID) never reaches the network.

Run:
    .venv/bin/python tests/build/test_arch_config.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

AL2023_ARN = "arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1"
EXCLUDED = ("ap-east-1", "ap-southeast-5", "ap-southeast-7", "eu-south-2")
X86_RATES = {
    "vcpu_per_sec": 0.0000326557,
    "memory_per_gb_sec": 0.0000043235,
    "snapshot_per_gb_month": 0.08,
    "snapshot_read_per_gb": 0.00155,
    "snapshot_write_per_gb": 0.00380,
}
ARM_RATES = {
    "vcpu_per_sec": 0.0000276944,
    "memory_per_gb_sec": 0.0000036667,
    "snapshot_per_gb_month": 0.08,
    "snapshot_read_per_gb": 0.00155,
    "snapshot_write_per_gb": 0.00380,
}

# ── tiny test harness (matches tests/pool/test_config_wiring.py) ───────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


_STUB_DIR = None


def _stub_path() -> str:
    """A temp dir holding a fake `aws` (exit 1) — keeps sourcing offline."""
    global _STUB_DIR
    if _STUB_DIR is None:
        _STUB_DIR = tempfile.mkdtemp(prefix="arch_cfg_stub_")
        stub = os.path.join(_STUB_DIR, "aws")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(stub, 0o755)
    return _STUB_DIR


def _cleanup_stub():
    global _STUB_DIR
    if _STUB_DIR:
        shutil.rmtree(_STUB_DIR, ignore_errors=True)
        _STUB_DIR = None


def _bash(snippet: str, extra_env=None) -> str:
    """Run `source scripts/config.sh; <snippet>` in a clean env, return stdout."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("IMAGE_ARCHES", "DEFAULT_IMAGE_ARCH", "IMAGE_ARCHES_DROPPED")
           and not k.startswith("POOL_")}
    env["PATH"] = _stub_path() + os.pathsep + env.get("PATH", "")
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "-c", f"source scripts/config.sh >/dev/null 2>&1; {snippet}"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.stdout.strip()


# ── Section 1: defaults in us-west-2 ───────────────────────────────────────
def test_defaults():
    out = _bash('echo "$AWS_REGION|$IMAGE_ARCHES|$DEFAULT_IMAGE_ARCH|${IMAGE_ARCHES_DROPPED-unset}"')
    region, arches, default, dropped = out.split("|")
    check("AWS_REGION is us-west-2 (the guard is exercised against it)", region == "us-west-2")
    check("IMAGE_ARCHES defaults to 'arm64 x86_64'", arches == "arm64 x86_64")
    check("DEFAULT_IMAGE_ARCH stays arm64", default == "arm64")
    check("IMAGE_ARCHES_DROPPED is set and empty in us-west-2", dropped == "")

    out = _bash('for a in arm64 x86_64 riscv; do echo "$a=$(base_image_arn_for $a)=$(cpu_arch_api_value_for $a)"; done')
    lines = dict((l.split("=", 1)[0], l.split("=", 1)[1]) for l in out.splitlines())
    check("base_image_arn_for arm64 is the al2023-1 managed image",
          lines.get("arm64") == f"{AL2023_ARN}=ARM_64")
    check("base_image_arn_for x86_64 is the SAME al2023-1 image, cpu value X86_64",
          lines.get("x86_64") == f"{AL2023_ARN}=X86_64")
    check("an unknown arch gets an empty base ARN and an empty cpu value",
          lines.get("riscv") == "=")


# ── Section 2: sizes, pricing and pool JSON ────────────────────────────────
def test_json_tables():
    sizes = json.loads(_bash('echo "$MICROVM_SIZES_JSON"'))
    check("MICROVM_SIZES_JSON lists both arches with 1024/2048/4096/8192",
          sizes == {"arm64": [1024, 2048, 4096, 8192], "x86_64": [1024, 2048, 4096, 8192]})

    pricing = json.loads(_bash('echo "$MICROVM_PRICING_JSON"'))
    check("MICROVM_PRICING_JSON has exactly arm64 and x86_64", set(pricing) == {"arm64", "x86_64"})
    check("x86_64 rates are the Price List API values", pricing.get("x86_64") == X86_RATES)
    check("x86_64 vCPU and memory rates are non-zero",
          pricing["x86_64"]["vcpu_per_sec"] > 0 and pricing["x86_64"]["memory_per_gb_sec"] > 0)
    check("arm64 rates are unchanged (and carry the snapshot read/write keys)",
          pricing.get("arm64") == ARM_RATES)

    pool = _bash('echo "$POOL_SIZE_OVERRIDES_X86_64"')
    check("POOL_SIZE_OVERRIDES_X86_64 defaults to 1 spare at 2048, 0 elsewhere",
          pool == '{"1024":0,"2048":1,"4096":0,"8192":0}')
    check("POOL_SIZE_OVERRIDES_X86_64 is valid JSON",
          json.loads(pool) == {"1024": 0, "2048": 1, "4096": 0, "8192": 0})


# ── Section 3: region guard ────────────────────────────────────────────────
def _guard(region, arches, default="arm64"):
    return _bash(
        f'AWS_REGION={region}; IMAGE_ARCHES="{arches}"; DEFAULT_IMAGE_ARCH={default}; '
        'apply_arch_region_guard; echo "$IMAGE_ARCHES|$DEFAULT_IMAGE_ARCH|$IMAGE_ARCHES_DROPPED"'
    )


def test_region_guard():
    for region in EXCLUDED:
        check(f"{region}: x86_64 dropped, default x86_64 -> arm64",
              _guard(region, "arm64 x86_64", "x86_64") == "arm64|arm64|x86_64")
        check(f"{region}: IMAGE_ARCHES=x86_64 alone becomes arm64",
              _guard(region, "x86_64") == "arm64|arm64|x86_64")
        check(f"{region}: IMAGE_ARCHES=arm64 is untouched, nothing reported dropped",
              _guard(region, "arm64") == "arm64|arm64|")
    for region in ("us-west-2", "us-east-1", "eu-west-1", "ap-southeast-1"):
        check(f"{region}: nothing dropped",
              _guard(region, "arm64 x86_64") == "arm64 x86_64|arm64|")

    out = _bash('echo "$IMAGE_ARCHES|$DEFAULT_IMAGE_ARCH"', {"IMAGE_ARCHES": "arm64"})
    check("an explicit IMAGE_ARCHES=arm64 is honored as-is", out == "arm64|arm64")

    out = _bash('set -u; apply_arch_region_guard; echo ok')
    check("apply_arch_region_guard is safe under set -u", out == "ok")


# ── runner ───────────────────────────────────────────────────────────────────
_SECTIONS = (test_defaults, test_json_tables, test_region_guard)


def main():
    print("\n=== scripts/config.sh architecture block ===")
    try:
        for fn in _SECTIONS:
            print(f"\n-- {fn.__name__} --")
            fn()
    finally:
        _cleanup_stub()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL ARCH CONFIG CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_arch_config():
    _checks.clear()
    try:
        for fn in _SECTIONS:
            fn()
    finally:
        _cleanup_stub()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "arch config failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
