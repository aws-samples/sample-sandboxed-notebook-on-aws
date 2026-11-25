#!/usr/bin/env python3
# test-kind: gated
"""
AwsVmBackend real-AWS smoke + launch-latency measurement (Phase 6).

OPT-IN ONLY. This file makes REAL lambda-microvms calls and launches REAL
MicroVMs. It is NEVER run as part of the offline suite: with POOL_E2E unset it
prints a skip line and exits 0 (zero AWS calls). The human operator runs it
explicitly, with real AWS credentials and a deployed /rebind-capable image, to
(1) smoke-test the real backend end to end and (2) measure cold-launch vs
pooled-grant latency.

Run (operator only):
    POOL_E2E=1 .venv/bin/python tests/pool/test_pool_e2e.py

Environment it reads (same vars _build_backend uses — see pool_manager/server.py
and the runbook): AWS_REGION, MICROVM_IMAGE_ARN, MICROVM_EXEC_ROLE_ARN,
MICROVM_INGRESS_CONNECTOR, MICROVM_SHELL_INGRESS_CONNECTOR,
MICROVM_EGRESS_CONNECTOR, IDLE_TIMEOUT_SECONDS, MAX_LIFETIME_SECONDS.
POOL_E2E_ARCH (default $TEST_ARCH, else arm64) and POOL_E2E_SIZE (default 2048)
pick the tier.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


def _skip(msg):
    print(f"SKIP test_pool_e2e: {msg}")
    print("  (set POOL_E2E=1 with real AWS creds + a /rebind-capable image to run)")
    sys.exit(0)


def _build_real_backend():
    """Construct an AwsVmBackend from env exactly the way
    pool_manager.server._build_backend does for config.backend == 'aws'."""
    from pool_manager.aws_backend import AwsVmBackend

    region = os.environ.get("AWS_REGION", "us-west-2")
    image = os.environ.get("MICROVM_IMAGE_ARN", "")
    if not image:
        _skip("MICROVM_IMAGE_ARN is empty — cannot launch a real VM")
    return AwsVmBackend(
        region=region,
        image_arn_base=image,
        exec_role_arn=os.environ.get("MICROVM_EXEC_ROLE_ARN", ""),
        ingress_connector=os.environ.get(
            "MICROVM_INGRESS_CONNECTOR",
            f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:HTTP_INGRESS"),
        shell_ingress_connector=os.environ.get(
            "MICROVM_SHELL_INGRESS_CONNECTOR",
            f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:SHELL_INGRESS"),
        egress_connector=os.environ.get(
            "MICROVM_EGRESS_CONNECTOR",
            f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"),
        idle_timeout_seconds=int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")),
        max_duration_seconds=int(os.environ.get("MAX_LIFETIME_SECONDS", "28800")),
    )


async def _smoke(backend, arch, size_mib):
    """Launch -> wait_healthy -> suspend -> resume -> rebind -> terminate, the
    full pooled-VM lifecycle, against a real VM. Returns (cold_launch_secs)."""
    print(f">> smoke: launch {arch}/{size_mib} ...")
    t0 = time.perf_counter()
    vm = await backend.launch(arch, size_mib, fake_session_id="e2e-spare")
    await backend.wait_healthy(vm, timeout=120)
    cold = time.perf_counter() - t0
    print(f"   launched + healthy in {cold:.2f}s  (vm_id={vm.vm_id})")
    try:
        print(">> smoke: suspend -> resume -> rebind ...")
        await backend.suspend(vm)
        await backend.resume(vm, timeout=120)
        await backend.rebind(vm, {
            "session_id": "e2e-real-session",
            "notebook_id": "e2e-nb",
            "notebook_name": "E2E Smoke",
            "persistence_mode": "none",
            "checkpoint_enabled": False,
        })
        print("   suspend/resume/rebind OK")
    finally:
        print(">> smoke: terminate (cleanup) ...")
        await backend.terminate(vm)
        print("   terminated")
    return cold


def main():
    if os.environ.get("POOL_E2E") != "1":
        _skip("POOL_E2E != 1")

    # Per-test var wins; else TEST_ARCH (suite-wide opt-in); else arm64.
    arch = os.environ.get("POOL_E2E_ARCH") or os.environ.get("TEST_ARCH") or "arm64"
    size = int(os.environ.get("POOL_E2E_SIZE", "2048"))

    backend = _build_real_backend()
    print(f"\n=== AwsVmBackend REAL e2e smoke ({arch}/{size}) ===")

    # (1) conformance smoke + cold-launch timing
    cold = asyncio.run(_smoke(backend, arch, size))

    # (2) before/after latency note. A full pooled-grant round trip (fill_once
    #     -> grant) is best measured through the live PoolManager against this
    #     backend; here we report the cold-launch floor this backend produces,
    #     which is the "before" number. The "after" (pooled grant) is observed
    #     from GET /status + the proxy launch logs once --WarmPool is live
    #     (see runbook §d). Printing, not asserting.
    print(f"\n--- latency ---")
    print(f"cold launch + healthy: {cold:.2f}s  (the 'before' / pool-miss floor)")
    print("pooled grant latency: measure live via GET /status + proxy logs "
          "after `POOL_VM_BACKEND=aws ./local_install.sh --WarmPool` (runbook §d)")
    print("\n=== e2e smoke complete ===")


if __name__ == "__main__":
    main()
