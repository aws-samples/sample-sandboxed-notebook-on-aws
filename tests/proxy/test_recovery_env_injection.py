#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests (code-review B-B-01): eternal-mode reactive recovery and rotation
relaunch a session's VM WITH the notebook's persisted env vars and secrets, the
same as /launch (shared helper proxy.platform.microvm_manager.build_user_env).

  - /launch (pool grant body AND cold runHookPayload): reference payload;
  - _do_recover pool-grant branch: the /grant body;
  - _do_recover cold branch: the kwargs handed to launch_for_rotation AND the
    runHookPayload the real launch_for_rotation builds from them;
  - all equal in env_vars / secrets / user_env_keys;
  - fallbacks: no notebook linked, and a storage error -> platform env only + WARNING;
  - no env or secret VALUE in the captured logs.
Fakes only (no AWS, no DB, no network); reuses the pool test harnesses.
Run:
    .venv/bin/python tests/proxy/test_recovery_env_injection.py
"""
import asyncio
import json
import logging
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
import proxy.platform.microvm_manager as mm_mod  # noqa: E402
import proxy.platform.routes.microvm as microvm_mod  # noqa: E402
from tests.pool import test_proxy_launch_grant as tl  # noqa: E402
from tests.pool import test_proxy_recover_grant as tr  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {str(detail)[:400]}" if not cond else ""))
    if not cond:
        _fails.append(label)


NB_ID = 42
PLATFORM = {"ATHENA_WORKGROUP": "wg-platform", "SHARED_KEY": "platform-value"}
STORED_ENV = {"MY_API_URL": "https://stored.example/api-v1", "SHARED_KEY": "user-wins-value",
              "DB_PASS": "env-shadowed-by-secret"}
STORED_SECRETS = [{"name": "db-pass", "arn": "arn:aws:secretsmanager:us-west-2:1:secret:db-pass-XyZ1",
                   "envVar": "DB_PASS"}]
VALUES = ["https://stored.example/api-v1", "user-wins-value", "env-shadowed-by-secret",
          "arn:aws:secretsmanager:us-west-2:1:secret:db-pass-XyZ1"]
EXPECTED = {
    "env_vars": {**PLATFORM, **STORED_ENV},
    "secrets": STORED_SECRETS,
    "user_env_keys": {"MY_API_URL": "env", "SHARED_KEY": "env", "DB_PASS": "secret"},
}
PLATFORM_ONLY = {"env_vars": dict(PLATFORM), "secrets": [], "user_env_keys": {}}


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)

    def warnings(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


cap = Capture()
logging.getLogger().addHandler(cap)
logging.getLogger().setLevel(logging.DEBUG)


def triple(d):
    return {"env_vars": d.get("env_vars"), "secrets": d.get("secrets"),
            "user_env_keys": d.get("user_env_keys")}


# ── 1. /launch reference (pool grant + cold run hook) ─────────────────────────
class _AStorage(tl._FakeAStorage):
    async def notebook_env_get(self, notebook_id):
        assert notebook_id == NB_ID, notebook_id
        return {"env_vars": dict(STORED_ENV), "secrets": [dict(s) for s in STORED_SECRETS]}


class _VmManager(tl._FakeVmManager):
    def launch_env_vars(self):
        return dict(PLATFORM)


def run_launch(pool):
    with tl.EnvSandbox():
        os.environ["POOL_ENABLED"] = "true" if pool else "false"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        vm_manager, astorage = _VmManager(), _AStorage()
        restore = tl._patch_module(vm_manager, astorage)
        seen = {}

        def _behavior(url, body):
            seen["grant"] = body
            return tl._FakeResponse(200, {"vm_id": "pool-vm-1", "endpoint": "pool-ep-1"})
        tl._FakeAsyncClient.behavior = _behavior
        try:
            req = tl._build_request(vm_manager, session_id=f"sess-launch-{pool}", notebookId=str(NB_ID))
            asyncio.run(microvm_mod.launch_microvm(req))
        finally:
            restore()
    if pool:
        return seen.get("grant") or {}
    params = vm_manager.run_vm_calls[0] if vm_manager.run_vm_calls else {}
    return json.loads(params.get("runHookPayload", "{}"))


print("\n/launch reference")
launch_grant = triple(run_launch(pool=True))
launch_cold = triple(run_launch(pool=False))
check("/launch grant body carries the merged env/secrets/user_env_keys", launch_grant == EXPECTED, launch_grant)
check("/launch cold runHookPayload carries the same", launch_cold == EXPECTED, launch_cold)


# ── 2. _do_recover ────────────────────────────────────────────────────────────
class _Storage(tr._FakeStorage):
    def __init__(self, notebook_id=NB_ID, env_error=False):
        super().__init__()
        self._nb = notebook_id
        self._env_error = env_error

    def vm_session_get(self, microvm_id):
        return {"notebook_id": self._nb, "memory_mib": 2048, "architecture": "arm64"}

    def notebook_env_get(self, notebook_id):
        if self._env_error:
            raise RuntimeError("storage unavailable")
        assert notebook_id == NB_ID, notebook_id
        return {"env_vars": dict(STORED_ENV), "secrets": [dict(s) for s in STORED_SECRETS]}


class _LaunchClient:
    def __init__(self):
        self.calls = []

    def run_microvm(self, **params):
        self.calls.append(params)
        return {"microvmId": "rot-vm-real", "endpoint": "rot-ep-real"}


def run_recover(pool, storage_fake):
    """Returns (grant body or None, launch_for_rotation kwargs or None, real
    rotation runHookPayload built from those kwargs or None)."""
    with tr.EnvSandbox():
        os.environ["POOL_ENABLED"] = "true" if pool else "false"
        os.environ["POOL_MANAGER_URL"] = "http://localhost:8090"
        manager, calls, restore = tr._build_manager(storage_fake)
        manager.launch_env_vars = lambda: dict(PLATFORM)
        seen = {}

        def _behavior(url, body):
            seen["grant"] = body
            return tr._FakeResponse(200, {"vm_id": "pool-vm-rec", "endpoint": "pool-ep-rec"})
        tr._FakeAsyncClient.behavior = _behavior
        try:
            asyncio.run(manager._do_recover("sess-recover-env", {"vm_id": "old-vm-env"}))
        finally:
            restore()
    rot_kwargs = calls["launch_for_rotation"][0] if calls["launch_for_rotation"] else None
    rot_payload = None
    if rot_kwargs is not None:
        # Feed the same kwargs to the REAL launch_for_rotation (launch client stubbed).
        real = mm_mod.MicrovmManager()
        real.launch_env_vars = lambda: dict(PLATFORM)
        real._artifacts_bucket = "fake-bucket"
        client = _LaunchClient()
        real.get_lambda_launch_client = lambda: client
        orig_role = mm_mod.EXEC_ROLE_ARN
        mm_mod.EXEC_ROLE_ARN = "arn:aws:iam::123456789012:role/exec"
        try:
            real.launch_for_rotation(**rot_kwargs)
        finally:
            mm_mod.EXEC_ROLE_ARN = orig_role
        rot_payload = json.loads(client.calls[0]["runHookPayload"])
    return seen.get("grant"), rot_kwargs, rot_payload


print("\n_do_recover pool-grant branch")
grant, rot_kwargs, _ = run_recover(pool=True, storage_fake=_Storage())
check("grant body carries the merged env/secrets/user_env_keys", grant and triple(grant) == EXPECTED,
      grant and triple(grant))
check("  identical to /launch's grant body", grant and triple(grant) == launch_grant)
check("  still restores from the session", grant and grant.get("restore_from") == "sess-recover-env")
check("  no cold relaunch", rot_kwargs is None)

print("\n_do_recover cold branch -> launch_for_rotation")
grant, rot_kwargs, rot_payload = run_recover(pool=False, storage_fake=_Storage())
check("no grant attempted (pool disabled)", grant is None)
check("launch_for_rotation gets the merged env/secrets/user_env_keys",
      rot_kwargs is not None and triple(rot_kwargs) == EXPECTED, rot_kwargs and triple(rot_kwargs))
check("rotation runHookPayload carries them", rot_payload and triple(rot_payload) == EXPECTED,
      rot_payload and triple(rot_payload))
check("  identical to /launch's cold runHookPayload", rot_payload and triple(rot_payload) == launch_cold)

print("\nlaunch_for_rotation without user env (back-compat)")
real = mm_mod.MicrovmManager()
real.launch_env_vars = lambda: dict(PLATFORM)
real._artifacts_bucket = "fake-bucket"
client = _LaunchClient()
real.get_lambda_launch_client = lambda: client
_orig_role, mm_mod.EXEC_ROLE_ARN = mm_mod.EXEC_ROLE_ARN, "arn:aws:iam::123456789012:role/exec"
try:
    real.launch_for_rotation(image_arn="arn:img", memory_mib=2048, idle_timeout_sec=60,
                             notebook_name="nb", session_id="s1")
finally:
    mm_mod.EXEC_ROLE_ARN = _orig_role
check("platform env only, empty secrets/user_env_keys",
      triple(json.loads(client.calls[0]["runHookPayload"])) == PLATFORM_ONLY)

print("\nfallbacks")
for label, storage_fake, want_warn in (
        ("no notebook linked", _Storage(notebook_id=None), "no notebook linked"),
        ("storage error", _Storage(env_error=True), "could not load persisted env")):
    for pool in (True, False):
        cap.records.clear()
        grant, rot_kwargs, rot_payload = run_recover(pool=pool, storage_fake=storage_fake)
        got = triple(grant) if pool else triple(rot_payload or {})
        branch = "pool" if pool else "cold"
        check(f"{label} ({branch}): platform env only", got == PLATFORM_ONLY, got)
        check(f"{label} ({branch}): WARNING logged",
              any(want_warn in w for w in cap.warnings()), cap.warnings())

print("\nlogs")
cap.records.clear()
run_launch(pool=True)
run_launch(pool=False)
run_recover(pool=True, storage_fake=_Storage())
run_recover(pool=False, storage_fake=_Storage())
run_recover(pool=False, storage_fake=_Storage(env_error=True))
logs = cap.text()
check("captured some log lines", len(cap.records) > 0)
leaked = [v for v in VALUES if v in logs]
check("no env or secret value appears in the logs", not leaked, leaked)

print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
