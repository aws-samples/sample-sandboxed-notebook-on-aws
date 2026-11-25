#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit tests for the Phase 1 VM Pool Manager session-rebind mechanism:
  _apply_session_identity()  — the shared identity-setting helper
  hook_run()                 — POST /run  (fresh launch, launch-only wrapper)
  rebind()                   — POST /rebind (pool grant: fake id -> real id)

No AWS, no MicroVM, no proxy, no pytest. Drives the real functions in
app.platform.hooks against a fake app.state (fake executor, fake data
catalog, a plain session_state dict seeded exactly like app/server.py's,
and a monkeypatched inject_secrets_into_env + isolated os.environ).

This suite checks the identity-field spec of /run and /rebind.

Run:
    python3 tests/pool/test_rebind_identity.py
"""

import sys
import os
import json
import copy
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import app.platform.hooks as hooks  # noqa: E402


# ── tiny test harness (matches tests/checkpoint/*.py convention) ────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


# ── fakes ─────────────────────────────────────────────────────────────────

def fresh_session_state() -> dict:
    """Mirrors app/server.py's session_state dict literal exactly — a fresh
    VM's starting point before any hook has run."""
    return {
        "microvm_id": None,
        "session_id": None,
        "notebook_id": None,
        "notebook_name": None,
        "persistence_mode": None,
        "started_at": None,
        "request_count": 0,
        "suspend_count": 0,
        "resume_count": 0,
        "checkpoint_enabled": False,
        "artifacts_bucket": None,
    }


class FakeExecutor:
    """Identity logic never calls executor methods; present only because
    hook_run() reads request.app.state.executor."""

    def get_stats(self):
        return {"execution_count": 0, "variables_count": 0}


class FakeDataCatalog:
    """Records every load_cloud_entries() call (so append-vs-replace growth is
    observable) and tracks the catalog's current VISIBLE content (replace
    semantics, matching the real DataCatalog.load_cloud_entries contract)."""

    def __init__(self):
        self.loaded = []       # list of every `entries` argument passed in, in order
        self.current = None    # most recent entries == what the catalog "shows" now
        self.discovery_calls = 0

    def load_cloud_entries(self, entries):
        self.loaded.append(entries)
        self.current = entries

    def start_local_discovery(self):
        self.discovery_calls += 1


class FakeCheckpointManager:
    def __init__(self):
        self.restore_calls = []

    def restore(self, session_id):
        self.restore_calls.append(session_id)


class FakeAppState:
    def __init__(self):
        self.executor = FakeExecutor()
        self.session_state = fresh_session_state()
        self.data_catalog = FakeDataCatalog()
        self.checkpoint_manager = FakeCheckpointManager()


class FakeApp:
    def __init__(self):
        self.state = FakeAppState()


class FakeRequest:
    """Stands in for FastAPI's Request: .app plus an async .json()."""

    def __init__(self, app, body):
        self.app = app
        self._body = body

    async def json(self):
        return self._body


def run_async(coro):
    return asyncio.run(coro)


# ── os.environ isolation ─────────────────────────────────────────────────

class EnvSandbox:
    """Snapshot os.environ on entry, restore on exit — tests must not leak
    MICROVM_* (or anything else) into each other or the real test process."""

    def __enter__(self):
        self._snapshot = dict(os.environ)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._snapshot)


# ── secrets injection monkeypatch ────────────────────────────────────────

class SecretsPatch:
    """Patches app.platform.hooks.inject_secrets_into_env so tests never hit
    real Secrets Manager / boto3. Records each call's secrets list."""

    def __init__(self):
        self.calls = []
        self._orig = None

    def __enter__(self):
        self._orig = hooks.inject_secrets_into_env

        def fake(secrets):
            self.calls.append(secrets)
            return {"injected": [s.get("envVar") for s in (secrets or [])], "failed": []}

        hooks.inject_secrets_into_env = fake
        return self

    def __exit__(self, *exc):
        hooks.inject_secrets_into_env = self._orig


# ── payload builder ──────────────────────────────────────────────────────

def make_payload(session_id, **overrides):
    payload = {
        "session_id": session_id,
        "notebook_id": f"nb-{session_id}",
        "notebook_name": f"Notebook {session_id}",
        "persistence_mode": "checkpoint",
        "checkpoint_enabled": True,
        "artifacts_bucket": f"bucket-{session_id}",
        "memory_mib": 2048,
        "architecture": "arm64",
        "env_vars": {"FOO": f"foo-{session_id}", "BAR": f"bar-{session_id}"},
        "secrets": [{"name": "s1", "arn": f"arn:{session_id}", "envVar": "SECRET_ONE"}],
        "user_env_keys": {"FOO": "env", "SECRET_ONE": "secret"},
        "catalog_entries": [{"source_type": "s3", "source_id": f"s3://{session_id}/data"}],
    }
    payload.update(overrides)
    return payload


EXPECTED_MICROVM_ENV_KEYS = [
    "MICROVM_ID",
    "MICROVM_SESSION_ID",
    "MICROVM_NOTEBOOK_ID",
    "MICROVM_NOTEBOOK_NAME",
    "MICROVM_PERSISTENCE_MODE",
    "MICROVM_STARTED_AT",
    "MICROVM_MEMORY_MIB",
    "MICROVM_ARCH",
]


# ── CORE ──────────────────────────────────────────────────────────────────

def test_apply_once_sets_everything():
    print("\n[CORE] apply-once sets session_state + env mirror + catalog + secrets")
    with EnvSandbox(), SecretsPatch() as secrets_patch:
        app = FakeApp()
        app.state.session_state["started_at"] = "2024-01-01T00:00:00+00:00"
        payload = make_payload("sess-A")

        hooks._apply_session_identity(app, payload, microvm_id="vm-1")

        ss = app.state.session_state
        check("session_id set", ss["session_id"] == "sess-A")
        check("notebook_id set", ss["notebook_id"] == "nb-sess-A")
        check("notebook_name set", ss["notebook_name"] == "Notebook sess-A")
        check("persistence_mode set", ss["persistence_mode"] == "checkpoint")
        check("checkpoint_enabled set", ss["checkpoint_enabled"] is True)
        check("artifacts_bucket set", ss["artifacts_bucket"] == "bucket-sess-A")
        check("microvm_id set when provided", ss["microvm_id"] == "vm-1")

        check("MICROVM_ID env", os.environ.get("MICROVM_ID") == "vm-1")
        check("MICROVM_SESSION_ID env", os.environ.get("MICROVM_SESSION_ID") == "sess-A")
        check("MICROVM_NOTEBOOK_ID env", os.environ.get("MICROVM_NOTEBOOK_ID") == "nb-sess-A")
        check("MICROVM_NOTEBOOK_NAME env", os.environ.get("MICROVM_NOTEBOOK_NAME") == "Notebook sess-A")
        check("MICROVM_PERSISTENCE_MODE env", os.environ.get("MICROVM_PERSISTENCE_MODE") == "checkpoint")
        check("MICROVM_STARTED_AT env republished", os.environ.get("MICROVM_STARTED_AT") == "2024-01-01T00:00:00+00:00")
        check("MICROVM_MEMORY_MIB env", os.environ.get("MICROVM_MEMORY_MIB") == "2048")
        check("MICROVM_ARCH env", os.environ.get("MICROVM_ARCH") == "arm64")

        check("env_vars injected", os.environ.get("FOO") == "foo-sess-A" and os.environ.get("BAR") == "bar-sess-A")
        check("secrets injection called once", len(secrets_patch.calls) == 1)
        check("secrets injection called with payload's secrets", secrets_patch.calls[0] == payload["secrets"])

        user_keys = json.loads(os.environ.get("MICROVM_USER_ENV_KEYS", "{}"))
        check("MICROVM_USER_ENV_KEYS matches payload", user_keys == {"FOO": "env", "SECRET_ONE": "secret"})

        check("catalog loaded exactly once", len(app.state.data_catalog.loaded) == 1)
        check("catalog loaded payload's entries", app.state.data_catalog.loaded[0] == payload["catalog_entries"])
        check("local discovery started", app.state.data_catalog.discovery_calls == 1)


def test_double_apply_rebind_proof():
    print("\n[CORE] double-apply rebind proof: fake A then real B -> fully on B")
    with EnvSandbox():
        app = FakeApp()
        app.state.session_state["started_at"] = "2024-01-01T00:00:00+00:00"

        payload_a = make_payload(
            "fake-pool-A",
            notebook_id="nb-fake", notebook_name="FAKE SPARE",
            memory_mib=1024, architecture="x86_64",
            env_vars={"ONLY_A": "a-value"},
            secrets=[{"name": "sA", "arn": "arn:a", "envVar": "SECRET_A"}],
            user_env_keys={"ONLY_A": "env", "SECRET_A": "secret"},
            catalog_entries=[{"source_type": "s3", "source_id": "s3://a/data"}],
        )
        payload_b = make_payload(
            "real-session-B",
            notebook_id="nb-real", notebook_name="REAL NOTEBOOK",
            memory_mib=4096, architecture="arm64",
            env_vars={"ONLY_B": "b-value"},
            secrets=[{"name": "sB", "arn": "arn:b", "envVar": "SECRET_B"}],
            user_env_keys={"ONLY_B": "env", "SECRET_B": "secret"},
            catalog_entries=[{"source_type": "s3", "source_id": "s3://b/data"}],
        )

        with SecretsPatch():
            hooks._apply_session_identity(app, payload_a, microvm_id="vm-pool-1")
        with SecretsPatch():
            hooks._apply_session_identity(app, payload_b, microvm_id=None)  # rebind: microvm_id untouched

        ss = app.state.session_state
        check("session_id is B", ss["session_id"] == "real-session-B")
        check("notebook_id is B", ss["notebook_id"] == "nb-real")
        check("notebook_name is B", ss["notebook_name"] == "REAL NOTEBOOK")
        check("persistence_mode is B", ss["persistence_mode"] == "checkpoint")
        check("checkpoint_enabled is B", ss["checkpoint_enabled"] is True)
        check("artifacts_bucket is B", ss["artifacts_bucket"] == "bucket-real-session-B")
        check("microvm_id STILL vm-pool-1 (rebind doesn't change it)", ss["microvm_id"] == "vm-pool-1")

        check("MICROVM_SESSION_ID is B", os.environ.get("MICROVM_SESSION_ID") == "real-session-B")
        check("MICROVM_NOTEBOOK_ID is B", os.environ.get("MICROVM_NOTEBOOK_ID") == "nb-real")
        check("MICROVM_NOTEBOOK_NAME is B", os.environ.get("MICROVM_NOTEBOOK_NAME") == "REAL NOTEBOOK")
        check("MICROVM_MEMORY_MIB is B", os.environ.get("MICROVM_MEMORY_MIB") == "4096")
        check("MICROVM_ARCH is B", os.environ.get("MICROVM_ARCH") == "arm64")
        check("MICROVM_ID still vm-pool-1", os.environ.get("MICROVM_ID") == "vm-pool-1")

        check("env ONLY_B present", os.environ.get("ONLY_B") == "b-value")
        check("env ONLY_A still present (never unset by design)", os.environ.get("ONLY_A") == "a-value")

        user_keys = json.loads(os.environ.get("MICROVM_USER_ENV_KEYS", "{}"))
        check("MICROVM_USER_ENV_KEYS is B's map only, no leftover A keys", user_keys == {"ONLY_B": "env", "SECRET_B": "secret"})

        check("catalog current reflects B only (replace, not union)", app.state.data_catalog.current == payload_b["catalog_entries"])
        check("catalog.loaded has both calls recorded", len(app.state.data_catalog.loaded) == 2)
        check("catalog.loaded[-1] equals B's entries", app.state.data_catalog.loaded[-1] == payload_b["catalog_entries"])


def test_run_handler_end_to_end():
    print("\n[CORE] /run handler end-to-end (with and without restore_from)")
    with EnvSandbox(), SecretsPatch():
        # -- with restore_from --
        app = FakeApp()
        payload = make_payload("run-sess-1", restore_from="prior-session-9")
        body = {"microvmId": "vm-run-1", "runHookPayload": json.dumps(payload)}
        req = FakeRequest(app, body)

        result = run_async(hooks.hook_run(req))

        check("returns status running", result.get("status") == "running")
        check("returns session_id", result.get("session_id") == "run-sess-1")
        check("restore called exactly once with restore_from", app.state.checkpoint_manager.restore_calls == ["prior-session-9"])
        check("started_at was set", app.state.session_state["started_at"] is not None)
        check("microvm_id equals body's microvmId", app.state.session_state["microvm_id"] == "vm-run-1")

        # -- without restore_from --
        app2 = FakeApp()
        payload2 = make_payload("run-sess-2")
        assert "restore_from" not in payload2
        body2 = {"microvmId": "vm-run-2", "runHookPayload": json.dumps(payload2)}
        req2 = FakeRequest(app2, body2)

        result2 = run_async(hooks.hook_run(req2))
        check("restore NOT called when restore_from absent", app2.state.checkpoint_manager.restore_calls == [])
        check("second /run returns correct session_id", result2.get("session_id") == "run-sess-2")


def test_rebind_handler_end_to_end():
    print("\n[CORE] /rebind handler end-to-end")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        app.state.session_state["microvm_id"] = "vm-pool-sentinel"
        app.state.session_state["started_at"] = "2024-01-01T00:00:00+00:00"

        # Payload includes a restore_from key — /rebind must ignore it entirely.
        payload = make_payload("rebind-sess-1", restore_from="should-be-ignored")
        req = FakeRequest(app, payload)

        result = run_async(hooks.rebind(req))

        check("returns status rebound", result.get("status") == "rebound")
        check("returns real session_id", result.get("session_id") == "rebind-sess-1")
        check("checkpoint.restore NEVER called by /rebind", app.state.checkpoint_manager.restore_calls == [])
        check("microvm_id unchanged (sentinel preserved)", app.state.session_state["microvm_id"] == "vm-pool-sentinel")


# ── EDGE CASES ────────────────────────────────────────────────────────────

def test_started_at_preserved_across_rebind():
    print("\n[EDGE] started_at preserved across rebind")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        sentinel = "2023-06-15T08:00:00+00:00"
        app.state.session_state["started_at"] = sentinel

        payload = make_payload("rebind-age-test")
        assert "started_at" not in payload
        req = FakeRequest(app, payload)
        run_async(hooks.rebind(req))

        check("session_state started_at unchanged", app.state.session_state["started_at"] == sentinel)
        check("MICROVM_STARTED_AT env equals sentinel", os.environ.get("MICROVM_STARTED_AT") == sentinel)


def test_catalog_inline_vs_s3_vs_neither():
    print("\n[EDGE] catalog: inline vs catalog_s3_key (mocked S3) vs neither")

    # (a) inline catalog_entries, no catalog_s3_key -> no S3 call attempted
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = make_payload("cat-inline")
        assert "catalog_s3_key" not in payload

        orig_import_boto3 = sys.modules.get("boto3")

        class ExplodingBoto3:
            def client(self, *a, **kw):
                raise AssertionError("boto3.client should NOT be called when catalog_entries is inline")

        sys.modules["boto3"] = ExplodingBoto3()
        try:
            hooks._apply_session_identity(app, payload, microvm_id="vm-cat-a")
        finally:
            if orig_import_boto3 is not None:
                sys.modules["boto3"] = orig_import_boto3
            else:
                sys.modules.pop("boto3", None)

        check("(a) inline catalog_entries loaded, no S3 call", app.state.data_catalog.loaded == [payload["catalog_entries"]])

    # (b) catalog_s3_key + artifacts_bucket, no catalog_entries -> mocked S3 fetch
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        s3_entries = [{"source_type": "s3", "source_id": "s3://staged/entry"}]
        payload = make_payload("cat-s3", catalog_s3_key="catalogs/staged.json")
        del payload["catalog_entries"]

        calls = {}

        class FakeS3Client:
            def get_object(self, Bucket, Key):
                calls["Bucket"] = Bucket
                calls["Key"] = Key

                class Body:
                    def read(self):
                        return json.dumps(s3_entries).encode("utf-8")
                return {"Body": Body()}

        class FakeBoto3Module:
            def client(self, name, *a, **kw):
                assert name == "s3"
                return FakeS3Client()

        orig_import_boto3 = sys.modules.get("boto3")
        sys.modules["boto3"] = FakeBoto3Module()
        try:
            hooks._apply_session_identity(app, payload, microvm_id="vm-cat-b")
        finally:
            if orig_import_boto3 is not None:
                sys.modules["boto3"] = orig_import_boto3
            else:
                sys.modules.pop("boto3", None)

        check("(b) S3 client called with right bucket/key", calls == {"Bucket": payload["artifacts_bucket"], "Key": "catalogs/staged.json"})
        check("(b) catalog loaded the fetched S3 entries", app.state.data_catalog.loaded == [s3_entries])

    # (c) neither catalog_entries nor catalog_s3_key -> no crash, no catalog load
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = make_payload("cat-neither")
        del payload["catalog_entries"]
        assert "catalog_s3_key" not in payload

        hooks._apply_session_identity(app, payload, microvm_id="vm-cat-c")

        check("(c) no catalog load attempted", app.state.data_catalog.loaded == [])
        check("(c) local discovery still runs unconditionally", app.state.data_catalog.discovery_calls == 1)


def test_secrets_present_vs_absent():
    print("\n[EDGE] secrets present vs absent")
    with EnvSandbox(), SecretsPatch() as secrets_patch:
        app = FakeApp()
        payload = make_payload("secrets-present")
        hooks._apply_session_identity(app, payload, microvm_id="vm-s-1")
        check("(a) secrets injection called once when present", len(secrets_patch.calls) == 1)

    with EnvSandbox(), SecretsPatch() as secrets_patch:
        app = FakeApp()
        payload = make_payload("secrets-absent")
        payload["secrets"] = []
        hooks._apply_session_identity(app, payload, microvm_id="vm-s-2")
        check("(b) secrets injection NOT called when empty/absent", len(secrets_patch.calls) == 0)


def test_env_vars_present_vs_absent():
    print("\n[EDGE] env_vars present vs absent")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = make_payload("env-present", env_vars={"KEY_ONE": "v1", "KEY_TWO": "v2"})
        hooks._apply_session_identity(app, payload, microvm_id="vm-e-1")
        check("(a) env vars injected with str() coercion", os.environ.get("KEY_ONE") == "v1" and os.environ.get("KEY_TWO") == "v2")

    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        before = dict(os.environ)
        payload = make_payload("env-absent")
        del payload["env_vars"]
        hooks._apply_session_identity(app, payload, microvm_id="vm-e-2")
        after = dict(os.environ)
        # Only MICROVM_* / MICROVM_USER_ENV_KEYS keys should differ; nothing else introduced.
        new_keys = set(after) - set(before)
        unexpected = {k for k in new_keys if not k.startswith("MICROVM_")}
        check("(b) no crash and no spurious non-MICROVM_* env vars when env_vars absent", unexpected == set())


def test_user_env_keys_present_vs_absent():
    print("\n[EDGE] user_env_keys present vs absent")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = make_payload("keys-present", user_env_keys={"X": "env", "Y": "secret"})
        hooks._apply_session_identity(app, payload, microvm_id="vm-k-1")
        decoded = json.loads(os.environ.get("MICROVM_USER_ENV_KEYS", "null"))
        check("(a) MICROVM_USER_ENV_KEYS decodes to exactly the payload's map", decoded == {"X": "env", "Y": "secret"})

    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = make_payload("keys-absent")
        del payload["user_env_keys"]
        hooks._apply_session_identity(app, payload, microvm_id="vm-k-2")
        raw = os.environ.get("MICROVM_USER_ENV_KEYS", None)
        check("(b) MICROVM_USER_ENV_KEYS published as '{}' (always-publish contract), not unset", raw == "{}")


def test_partial_missing_optional_fields_no_crash():
    print("\n[EDGE] partial/missing optional fields -> no crash, mirror /run defaults")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = {"session_id": "minimal-session"}

        hooks._apply_session_identity(app, payload, microvm_id="vm-min-1")

        ss = app.state.session_state
        check("session_id set", ss["session_id"] == "minimal-session")
        check("notebook_id is None (no invented default)", ss["notebook_id"] is None)
        check("notebook_name is None (no invented default)", ss["notebook_name"] is None)
        check("checkpoint_enabled defaults to False", ss["checkpoint_enabled"] is False)
        check("persistence_mode defaults to 'checkpoint'", ss["persistence_mode"] == "checkpoint")
        check("artifacts_bucket is None", ss["artifacts_bucket"] is None)

        check("MICROVM_NOTEBOOK_ID absent from os.environ (popped on None)", "MICROVM_NOTEBOOK_ID" not in os.environ)
        check("MICROVM_NOTEBOOK_NAME absent from os.environ", "MICROVM_NOTEBOOK_NAME" not in os.environ)
        check("MICROVM_MEMORY_MIB absent from os.environ", "MICROVM_MEMORY_MIB" not in os.environ)
        check("MICROVM_ARCH absent from os.environ", "MICROVM_ARCH" not in os.environ)
        check("MICROVM_USER_ENV_KEYS still published as '{}'", os.environ.get("MICROVM_USER_ENV_KEYS") == "{}")
        check("no exception raised", True)


def test_idempotency_same_payload_twice():
    print("\n[EDGE] idempotency: same payload applied twice -> same state, no growth")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        payload = make_payload("idempotent-session")

        hooks._apply_session_identity(app, copy.deepcopy(payload), microvm_id="vm-idem")
        ss_after_1 = copy.deepcopy(app.state.session_state)
        env_after_1 = {k: v for k, v in os.environ.items() if k in EXPECTED_MICROVM_ENV_KEYS}

        hooks._apply_session_identity(app, copy.deepcopy(payload), microvm_id="vm-idem")
        ss_after_2 = copy.deepcopy(app.state.session_state)
        env_after_2 = {k: v for k, v in os.environ.items() if k in EXPECTED_MICROVM_ENV_KEYS}

        check("session_state identical after 2nd apply", ss_after_1 == ss_after_2)
        check("MICROVM_* env identical after 2nd apply", env_after_1 == env_after_2)
        check("catalog called twice (not deduped)", len(app.state.data_catalog.loaded) == 2)
        check("catalog visible content NOT grown/duplicated", app.state.data_catalog.current == payload["catalog_entries"])

        user_keys_1 = json.loads(os.environ.get("MICROVM_USER_ENV_KEYS"))
        check("MICROVM_USER_ENV_KEYS not doubled/merged across repeat calls", user_keys_1 == payload["user_env_keys"])


# ── BEHAVIOR-PRESERVATION (regression guard) ─────────────────────────────

def test_run_behavior_preservation_regression():
    print("\n[REGRESSION] /run produces the SAME session_state + env mirror as pre-refactor")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()

        full_payload = {
            "session_id": "regress-session",
            "notebook_id": "nb-regress",
            "notebook_name": "Regression Notebook",
            "persistence_mode": "checkpoint",
            "checkpoint_enabled": True,
            "artifacts_bucket": "regress-bucket",
            "memory_mib": 4096,
            "architecture": "arm64",
            "env_vars": {"REGRESS_A": "1", "REGRESS_B": "2"},
            "secrets": [{"name": "rs", "arn": "arn:regress", "envVar": "REGRESS_SECRET"}],
            "user_env_keys": {"REGRESS_A": "env", "REGRESS_SECRET": "secret"},
            "catalog_entries": [{"source_type": "s3", "source_id": "s3://regress/data"}],
            # no restore_from
        }
        body = {"microvmId": "vm-regress", "runHookPayload": json.dumps(full_payload)}
        req = FakeRequest(app, body)

        run_async(hooks.hook_run(req))

        # Hand-computed expected state, reasoned independently from the
        # PRE-REFACTOR hook_run body (plan.md part (a)/(c)) applied to
        # full_payload above — NOT derived by calling any helper.
        expected_session_state = {
            "microvm_id": "vm-regress",
            "session_id": "regress-session",
            "notebook_id": "nb-regress",
            "notebook_name": "Regression Notebook",
            "persistence_mode": "checkpoint",
            "started_at": None,  # checked separately (wall-clock)
            "request_count": 0,
            "suspend_count": 0,
            "resume_count": 0,
            "checkpoint_enabled": True,
            "artifacts_bucket": "regress-bucket",
        }
        expected_env = {
            "MICROVM_ID": "vm-regress",
            "MICROVM_SESSION_ID": "regress-session",
            "MICROVM_NOTEBOOK_ID": "nb-regress",
            "MICROVM_NOTEBOOK_NAME": "Regression Notebook",
            "MICROVM_PERSISTENCE_MODE": "checkpoint",
            "MICROVM_MEMORY_MIB": "4096",
            "MICROVM_ARCH": "arm64",
        }

        ss = app.state.session_state
        ss_for_compare = dict(ss)
        started_at_actual = ss_for_compare.pop("started_at")
        expected_session_state.pop("started_at")
        check("session_state matches expected (excl. started_at)", ss_for_compare == expected_session_state)
        check("started_at is a non-None ISO8601-ish string", isinstance(started_at_actual, str) and len(started_at_actual) > 0 and "T" in started_at_actual)

        for k, v in expected_env.items():
            check(f"env {k} matches expected", os.environ.get(k) == v)
        check("MICROVM_STARTED_AT env present and non-empty", bool(os.environ.get("MICROVM_STARTED_AT")))

        check("env_vars injected (REGRESS_A/REGRESS_B)", os.environ.get("REGRESS_A") == "1" and os.environ.get("REGRESS_B") == "2")
        decoded_keys = json.loads(os.environ.get("MICROVM_USER_ENV_KEYS", "null"))
        check("user_env_keys map matches expected", decoded_keys == {"REGRESS_A": "env", "REGRESS_SECRET": "secret"})
        check("catalog loaded the payload's entries", app.state.data_catalog.loaded == [full_payload["catalog_entries"]])


# ── parse-failure fall-through (extra regression: plan.md part (c) note) ──

def test_run_parse_failure_fallback():
    print("\n[REGRESSION] /run with an unparseable runHookPayload still falls through")
    with EnvSandbox(), SecretsPatch():
        app = FakeApp()
        body = {"microvmId": "vm-badjson", "runHookPayload": "not-valid-json{{{"}
        req = FakeRequest(app, body)

        result = run_async(hooks.hook_run(req))

        ss = app.state.session_state
        check("session_id falls back to the raw payload string", ss["session_id"] == "not-valid-json{{{")
        check("checkpoint_enabled is False on parse failure", ss["checkpoint_enabled"] is False)
        check("persistence_mode is 'checkpoint' on parse failure", ss["persistence_mode"] == "checkpoint")
        check("response status is running", result.get("status") == "running")
        check("local discovery still ran (fell through into shared helper)", app.state.data_catalog.discovery_calls == 1)
        check("MICROVM_USER_ENV_KEYS still published as '{}' on parse failure", os.environ.get("MICROVM_USER_ENV_KEYS") == "{}")


# ── main ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  VM Pool Manager Phase 1 — rebind identity offline unit tests")
    print("=" * 72)

    test_apply_once_sets_everything()
    test_double_apply_rebind_proof()
    test_run_handler_end_to_end()
    test_rebind_handler_end_to_end()

    test_started_at_preserved_across_rebind()
    test_catalog_inline_vs_s3_vs_neither()
    test_secrets_present_vs_absent()
    test_env_vars_present_vs_absent()
    test_user_env_keys_present_vs_absent()
    test_partial_missing_optional_fields_no_crash()
    test_idempotency_same_payload_twice()

    test_run_behavior_preservation_regression()
    test_run_parse_failure_fallback()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 72)
    print(f"  REBIND IDENTITY: {passed}/{total} checks passed")
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
