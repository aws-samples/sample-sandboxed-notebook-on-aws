"""Cached AWS client factory for VM-side code (helpers and the SQL engine).

- aws_client(service, region, config): thread-safe, cached per (service, region,
  config). The cache is dropped by reset_aws_clients(), which
  hooks._reset_boto3_credentials calls on /run, /resume and /rebind, so no client
  keeps build-role credentials across a snapshot restore.
- aws_region(): the region is read from AWS_REGION at call time, never at import.
- artifact_bucket() / aws_account_id(): one resolver; the account id costs at most
  one STS call until the next credential reset.

Kept free of heavy imports so app.platform.hooks can import it cheaply.
"""
import os
import threading

_LOCK = threading.Lock()
_CLIENTS: dict = {}
_ACCOUNT_ID: list = []  # [] = unknown, [id] = cached


def aws_region() -> str:
    return os.environ.get("AWS_REGION") or "us-west-2"


def _real_boto3_client(boto3) -> bool:
    # Tests replace boto3.client with a lambda (sometimes swapping the fake behind
    # it). Only the real constructor is cached, so such fakes are honoured on
    # every call.
    return getattr(boto3.client, "__module__", None) == "boto3"


def aws_client(service: str, region: str = None, config=None):
    import boto3
    region = region or aws_region()
    kwargs = {"region_name": region}
    if config is not None:
        kwargs["config"] = config
    if not _real_boto3_client(boto3):
        return boto3.client(service, **kwargs)
    key = (service, region, id(config) if config is not None else None)
    with _LOCK:
        entry = _CLIENTS.get(key)
        if entry is None:
            # Keep `config` referenced so its id() cannot be reused while cached.
            entry = (boto3.client(service, **kwargs), config)
            _CLIENTS[key] = entry
        return entry[0]


def aws_resource(service: str, region: str = None):
    """boto3 resource, NOT cached (resources are not thread-safe)."""
    import boto3
    return boto3.resource(service, region_name=region or aws_region())


def reset_aws_clients() -> None:
    with _LOCK:
        _CLIENTS.clear()
        _ACCOUNT_ID.clear()


def aws_account_id() -> str:
    with _LOCK:
        if _ACCOUNT_ID:
            return _ACCOUNT_ID[0]
    acct = aws_client("sts").get_caller_identity()["Account"]
    with _LOCK:
        _ACCOUNT_ID[:] = [acct]
    return acct


def artifact_bucket(region: str = None) -> str:
    """The sandbox artifacts bucket: ARTIFACT_BUCKET, else
    microvm-sandbox-artifacts-<account>-<region>."""
    b = os.environ.get("ARTIFACT_BUCKET")
    if b:
        return b
    return f"microvm-sandbox-artifacts-{aws_account_id()}-{region or aws_region()}"
