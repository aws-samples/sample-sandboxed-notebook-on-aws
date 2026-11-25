#!/usr/bin/env python3
# test-kind: offline
"""
H3 secret-keys name-prefix enforcement tests (FEAT-003).

GET /secrets/keys fetches the JSON keys of a Secrets Manager secret. Without a
prefix check, any account secret could be probed by name/ARN. The handler must
reject any secret_id whose name is outside SECRETS_NAME_PREFIX ('microvm/')
BEFORE touching AWS, and still fetch legitimate microvm/ secrets.

We prove "no AWS call on rejection" with a fake Secrets Manager client that
records whether get_secret_value was called. sessions.aws_clients.get_client is
monkeypatched, so no real AWS request is ever made.

Run:
    python3 tests/proxy/test_secret_keys_prefix.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# AWS_REGION must be set before importing proxy route modules (microvm_manager
# reads it at import time); we never make a real AWS call thanks to the stub.
os.environ.setdefault("AWS_REGION", "us-west-2")

from proxy.platform.routes import sessions  # noqa: E402


_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")


class _FakeSMClient:
    """Fake Secrets Manager client that records whether get_secret_value was
    called. Returns a JSON SecretString so a legitimate call yields real keys."""

    def __init__(self):
        self.get_secret_value_called = False

    def get_secret_value(self, SecretId=None):
        self.get_secret_value_called = True
        return {"SecretString": json.dumps({"username": "u", "password": "p"})}


def _install_fake_boto3():
    """Monkeypatch sessions.aws_clients.get_client (the route's only client
    source) to return a fresh fake SM client and return that client so the test
    can inspect get_secret_value_called."""
    fake = _FakeSMClient()

    def _fake_client(service_name, *args, **kwargs):
        return fake

    sessions.aws_clients.get_client = _fake_client
    return fake


def _body(resp):
    """The route returns a dict on success and a json_error JSONResponse
    (403 for a rejected prefix) on failure."""
    if isinstance(resp, dict):
        return 200, resp
    return resp.status_code, json.loads(resp.body)


def main():
    print("\n=== secret-keys name-prefix enforcement (H3) ===")

    # (1) Bare non-microvm/ name -> prefix error, NO AWS call.
    fake = _install_fake_boto3()
    status, resp = _body(asyncio.run(sessions.list_secret_keys(secret_id="prod-db-credentials")))
    check("bare non-microvm/ name -> 403 prefix error",
          status == 403 and resp.get("keys") == [] and "prefix" in (resp.get("error") or ""))
    check("bare non-microvm/ name -> get_secret_value NOT called",
          fake.get_secret_value_called is False)

    # (2) ARN for microvm-internal/env-master-key (OUTSIDE microvm/) -> rejected.
    fake = _install_fake_boto3()
    arn_bad = ("arn:aws:secretsmanager:us-west-2:111122223333:"
               "secret:microvm-internal/env-master-key-xyz")
    status, resp = _body(asyncio.run(sessions.list_secret_keys(secret_id=arn_bad)))
    check("microvm-internal/ ARN -> 403 prefix error",
          status == 403 and resp.get("keys") == [] and "prefix" in (resp.get("error") or ""))
    check("microvm-internal/ ARN -> get_secret_value NOT called",
          fake.get_secret_value_called is False)

    # (3) Legitimate bare microvm/ name -> AWS call made, keys returned.
    fake = _install_fake_boto3()
    _, resp = _body(asyncio.run(sessions.list_secret_keys(secret_id="microvm/proxy-db/readonly")))
    check("microvm/ bare name -> get_secret_value called",
          fake.get_secret_value_called is True)
    check("microvm/ bare name -> keys returned",
          sorted(resp.get("keys", [])) == ["password", "username"])

    # (4) Legitimate microvm/ ARN -> AWS call made, keys returned.
    fake = _install_fake_boto3()
    arn_ok = ("arn:aws:secretsmanager:us-west-2:111122223333:"
              "secret:microvm/foo-abc123")
    _, resp = _body(asyncio.run(sessions.list_secret_keys(secret_id=arn_ok)))
    check("microvm/ ARN -> get_secret_value called",
          fake.get_secret_value_called is True)
    check("microvm/ ARN -> keys returned",
          sorted(resp.get("keys", [])) == ["password", "username"])

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  FAIL {name}")
        sys.exit(1)
    print("ALL SECRET-KEYS PREFIX CHECKS PASSED")


if __name__ == "__main__":
    main()
