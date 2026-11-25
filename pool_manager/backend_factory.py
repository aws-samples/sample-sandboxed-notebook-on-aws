"""
Side-effect-free VM backend factory.

Builds a VmBackend (FakeVmBackend or AwsVmBackend) from a PoolConfig + the
environment, with NO module-level side effects — importing this module only
defines build_backend(); no app, no PoolManager, no eager boto3 client is
constructed until build_backend() is called.

This exists so callers that need only to construct a backend (e.g. the
local-dev reset one-shot pool_manager.reset_local_pool) can do so WITHOUT
importing pool_manager.server, whose module-level `app = create_app()` would
eagerly build a throwaway backend + PoolManager + FastAPI app. server.py now
imports build_backend from here, so the server's behaviour is unchanged.
"""

import os

from pool_manager.aws_backend import AwsVmBackend
from pool_manager.config import PoolConfig
from pool_manager.fake_backend import FakeVmBackend


def build_backend(config: PoolConfig):
    if config.backend == "fake":
        # Seed the realistic ~10s launch delay so a standalone fake-backed run
        # behaves like production. Tests inject their own FakeVmBackend() (delay
        # 0) via create_app(backend=...), so this only affects real runs.
        return FakeVmBackend(launch_delay_seconds=config.fake_launch_delay_seconds)
    if config.backend == "aws":
        region = os.environ.get("AWS_REGION", "us-west-2")
        return AwsVmBackend(
            region=region,
            image_arn_base=os.environ.get("MICROVM_IMAGE_ARN", ""),
            exec_role_arn=os.environ.get("MICROVM_EXEC_ROLE_ARN", ""),
            ingress_connector=os.environ.get(
                "MICROVM_INGRESS_CONNECTOR",
                f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:HTTP_INGRESS",
            ),
            shell_ingress_connector=os.environ.get(
                "MICROVM_SHELL_INGRESS_CONNECTOR",
                f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:SHELL_INGRESS",
            ),
            egress_connector=os.environ.get(
                "MICROVM_EGRESS_CONNECTOR",
                f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
            ),
            idle_timeout_seconds=int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")),
            max_duration_seconds=int(os.environ.get("MAX_LIFETIME_SECONDS", "28800")),
        )
    raise ValueError(f"Unknown POOL_VM_BACKEND: {config.backend}")
