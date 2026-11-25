#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for app.notebook.aws_clients: cached thread-safe client factory,
credential reset (hooks._reset_boto3_credentials), call-time region and the single
artifact-bucket resolver (no STS call when ARTIFACT_BUCKET is set, one when not).
Client creation makes no network call. Run: .venv/bin/python tests/vm/test_aws_clients.py
"""
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["AWS_ACCESS_KEY_ID"] = "x"
os.environ["AWS_SECRET_ACCESS_KEY"] = "x"
os.environ["AWS_REGION"] = "us-west-2"
os.environ.pop("ARTIFACT_BUCKET", None)

import logging  # noqa: E402
import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from app.notebook import aws_clients as ac  # noqa: E402
from app.platform import hooks  # noqa: E402

logging.disable(logging.CRITICAL)
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


_REAL_CLIENT = boto3.client

a = ac.aws_client("s3")
check("same key returns the same client", ac.aws_client("s3") is a)
check("explicit region equal to the default shares the client", ac.aws_client("s3", "us-west-2") is a)
check("different service returns another client", ac.aws_client("sts") is not a)
check("different region returns another client", ac.aws_client("s3", "eu-west-1") is not a)
cfg = Config(connect_timeout=3)
c1 = ac.aws_client("s3", config=cfg)
check("config client differs from the plain one", c1 is not a)
check("same config object is cached", ac.aws_client("s3", config=cfg) is c1)

out = []
barrier = threading.Barrier(16)


def worker():
    barrier.wait()
    out.append(ac.aws_client("logs"))


ts = [threading.Thread(target=worker) for _ in range(16)]
[t.start() for t in ts]
[t.join() for t in ts]
check("16 threads get one client", len({id(x) for x in out}) == 1 and len(out) == 16)

hooks._reset_boto3_credentials()
b = ac.aws_client("s3")
check("credential reset drops cached clients", b is not a)
check("  and the new one is cached again", ac.aws_client("s3") is b)

# Region is read at call time.
os.environ["AWS_REGION"] = "eu-central-1"
check("aws_region follows AWS_REGION without re-import", ac.aws_region() == "eu-central-1")
check("  client for the default region uses the new region",
      ac.aws_client("s3").meta.region_name == "eu-central-1")
os.environ["AWS_REGION"] = "us-west-2"

# A replaced boto3.client (test fake) is honoured on every call and never cached.
created = []


def fake_client(service, **kw):
    obj = object()
    created.append((service, kw.get("region_name"), obj))
    return obj


boto3.client = fake_client
try:
    x1 = ac.aws_client("athena", "us-east-1")
    x2 = ac.aws_client("athena", "us-east-1")
    check("fake boto3.client: returns what the fake returns, nothing cached",
          x1 is not x2 and len(created) == 2 and created[0][:2] == ("athena", "us-east-1"))
finally:
    boto3.client = _REAL_CLIENT

# artifact_bucket / aws_account_id.
sts_calls = []


class FakeSts:
    def get_caller_identity(self):
        sts_calls.append(1)
        return {"Account": "111122223333"}


boto3.client = lambda service, **kw: FakeSts()
try:
    ac.reset_aws_clients()
    os.environ["ARTIFACT_BUCKET"] = "my-bucket"
    check("ARTIFACT_BUCKET wins", ac.artifact_bucket() == "my-bucket")
    check("  with zero STS calls", sts_calls == [])
    del os.environ["ARTIFACT_BUCKET"]
    b1 = ac.artifact_bucket()
    b2 = ac.artifact_bucket("eu-west-1")
    check("unset: bucket built from account and region",
          b1 == "microvm-sandbox-artifacts-111122223333-us-west-2"
          and b2 == "microvm-sandbox-artifacts-111122223333-eu-west-1", (b1, b2))
    check("  exactly one STS call across two lookups", len(sts_calls) == 1, len(sts_calls))
    hooks._reset_boto3_credentials()
    ac.artifact_bucket()
    check("credential reset drops the cached account id", len(sts_calls) == 2, len(sts_calls))
finally:
    boto3.client = _REAL_CLIENT
    ac.reset_aws_clients()

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
