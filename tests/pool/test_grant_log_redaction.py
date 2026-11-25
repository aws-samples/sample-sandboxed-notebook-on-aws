#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review D-B-01): the pool manager's /grant never writes the
grant body's env_vars values or secret refs to a log line or the response.

FastAPI TestClient over create_app() with a FakeVmBackend + temp SQLite (no AWS,
no network). Paths covered:
  1. unexpected exception (storage.pool_vm_claim_youngest raises) -> 500 INTERNAL;
  2. the same exception with the secret value IN its message;
  3. GrantError path: backend.rebind raises a BackendError whose message echoes
     the identity payload -> 502 GRANT_FAILED;
  4. the redaction helpers on their own.
Logs are captured with a handler on the root logger (pool_manager.* propagate).
Run:
    .venv/bin/python tests/pool/test_grant_log_redaction.py
"""
import logging
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from tests.pool._poolkit import _run_sqlite, build_config  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.manager import grant_log_context, redact_grant_text  # noqa: E402
from pool_manager.server import create_app  # noqa: E402
from pool_manager.vm_backend import BackendError  # noqa: E402
try:
    from fastapi.testclient import TestClient
except ImportError:
    print("SKIP  fastapi.testclient unavailable")
    sys.exit(0)

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f" — {str(detail)[:300]}" if detail and not ok else ""))


SECRET = "TOP-SECRET-VALUE-123"
SECRET_ARN = "arn:aws:secretsmanager:us-west-2:111122223333:secret:my-db-pass-AbCdEf"
TOKEN = "grant-redaction-test-token"
os.environ["POOL_AUTH_TOKEN"] = TOKEN


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        self.lines.append(text)

    def text(self):
        return "\n".join(self.lines)


def body(**extra):
    b = {"arch": "arm64", "size": 2048, "session_id": "sess-redact-1", "notebook_id": 77,
         "env_vars": {"SECRET_ENV": SECRET, "PLAIN": "x"},
         "secrets": [{"name": "db", "arn": SECRET_ARN, "envVar": "DB_PASS"}],
         "user_env_keys": {"SECRET_ENV": "env", "DB_PASS": "secret"},
         "catalog_entries": [{"source_id": "s", "sample": SECRET}]}
    b.update(extra)
    return b


def run_case(label, setup, want_status):
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=1, arches=["arm64"], sizes_by_arch={"arm64": [2048]})
    app = create_app(config=config, backend=backend, storage=storage)
    cap = Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        with TestClient(app, headers={"X-Proxy-Auth": TOKEN}, raise_server_exceptions=False) as client:
            setup(app, backend, storage)
            resp = client.post("/grant", json=body())
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    logs = cap.text()
    print(f"\n{label}")
    check(f"status {want_status}", resp.status_code == want_status, (resp.status_code, resp.text))
    check("secret env value not in the logs", SECRET not in logs, logs[-600:])
    check("secret ref not in the logs", SECRET_ARN not in logs)
    check("secret env value not in the response", SECRET not in resp.text, resp.text)
    check("secret ref not in the response", SECRET_ARN not in resp.text, resp.text)
    return logs, resp


def boom_plain(app, backend, storage):
    def _raise(*a, **kw):
        raise RuntimeError("database is locked")
    storage.pool_vm_claim_youngest = _raise


def boom_echo(app, backend, storage):
    def _raise(*a, **kw):
        raise RuntimeError(f"bad row for env {{'SECRET_ENV': '{SECRET}'}} secret {SECRET_ARN}")
    storage.pool_vm_claim_youngest = _raise


logs, resp = run_case("1. unexpected exception -> 500", boom_plain, 500)
check("log names session_id / arch / size", "sess-redact-1" in logs and "arm64" in logs and "2048" in logs, logs)
check("log keeps the exception type + message", "RuntimeError" in logs and "database is locked" in logs, logs)
check("log never dumps the request dict", "env_vars" not in logs and "user_env_keys" not in logs, logs)
check("response detail is the exception message", resp.json().get("detail") == "database is locked", resp.text)

logs, resp = run_case("2. unexpected exception echoing the values -> 500, masked", boom_echo, 500)
check("masked marker present in the log", "***" in logs, logs)
check("masked marker present in the response", "***" in resp.text, resp.text)


def rebind_echo(app, backend, storage):
    async def _rebind(vm, identity_payload):
        raise BackendError(f"rebind failed (400) for {vm.vm_id}: {identity_payload!r}")
    backend.rebind = _rebind


logs, resp = run_case("3. GrantError: backend error echoing the payload -> 502, masked", rebind_echo, 502)
check("GRANT_FAILED body", resp.json().get("error") == "GRANT_FAILED", resp.text)
check("grant warning still logged (masked)", "grant: failed for claimed spare" in logs and "***" in logs, logs)

print("\n4. helpers")
req = body()
check("redact_grant_text masks env values and secret refs",
      redact_grant_text(f"a {SECRET} b {SECRET_ARN} c", req) == "a *** b *** c")
check("short values are left alone (no garbling)", redact_grant_text("x marks", req) == "x marks")
check("non-dict request: text unchanged", redact_grant_text("plain", None) == "plain")
ctx = grant_log_context(req)
check("grant_log_context: identifiers only", ctx == "session_id='sess-redact-1' arch='arm64' size=2048 notebook_id=77"
      and SECRET not in ctx, ctx)

passed = sum(1 for _, ok in _checks if ok)
print(f"\n{passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
