# test-kind: offline
"""
Unit tests for the env/secret helpers (app.notebook.helpers.env) and the
user-env-key source-map contract between the /launch route and the /run hook.

Standalone (no pytest, no running proxy): run with
    .venv/bin/python tests/vm/test_env_helpers.py
Exits non-zero on any failure. Each check prints PASS/FAIL.
"""
import os
import sys
import json

# Make the repo importable when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("AWS_REGION", "us-west-2")

from app.notebook.helpers.env import (  # noqa: E402
    get_env, get_secret, list_env, list_secret,
)

_fails = []


def check(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def _clear(*names):
    for n in names:
        os.environ.pop(n, None)


# ─── get_env / get_secret: coercion matrix ───────────────────────────────────
def test_get_env():
    _clear("V")
    # unset
    check("unset → None", get_env("V") is None)
    check("unset → default", get_env("V", "d") == "d")
    check("unset required → KeyError", _raises(KeyError, lambda: get_env("V", required=True)))

    # auto (as_type=None)
    os.environ["V"] = "plain-string"
    check("auto plaintext → str", get_env("V") == "plain-string")
    os.environ["V"] = "00123"
    check("auto zero-padded stays str", get_env("V") == "00123")
    os.environ["V"] = "42"
    check("auto bare int stays str (no silent coerce)", get_env("V") == "42")
    os.environ["V"] = '{"a": 1}'
    check("auto JSON object → dict", get_env("V") == {"a": 1})
    os.environ["V"] = "[1, 2, 3]"
    check("auto JSON array → list", get_env("V") == [1, 2, 3])
    os.environ["V"] = "{not json"
    check("auto JSON-looking but invalid → raw str", get_env("V") == "{not json")

    # explicit as_type
    os.environ["V"] = "5432"
    check("as_type=int", get_env("V", as_type=int) == 5432)
    os.environ["V"] = "0.5"
    check("as_type=float", get_env("V", as_type=float) == 0.5)
    for t in ("true", "YES", "on", "1"):
        os.environ["V"] = t
        check(f"as_type=bool {t!r} → True", get_env("V", as_type=bool) is True)
    for f in ("false", "no", "off", "0"):
        os.environ["V"] = f
        check(f"as_type=bool {f!r} → False", get_env("V", as_type=bool) is False)
    os.environ["V"] = "maybe"
    check("as_type=bool bad → ValueError", _raises(ValueError, lambda: get_env("V", as_type=bool)))
    os.environ["V"] = "notanum"
    check("as_type=int bad → ValueError", _raises(ValueError, lambda: get_env("V", as_type=int)))
    os.environ["V"] = '{"a":1}'
    check("as_type=dict on object", get_env("V", as_type=dict) == {"a": 1})
    check("as_type=str forces string even on JSON", get_env("V", as_type=str) == '{"a":1}')
    os.environ["V"] = "[1,2]"
    check("as_type=dict on array → ValueError", _raises(ValueError, lambda: get_env("V", as_type=dict)))
    check("as_type=list on array", get_env("V", as_type=list) == [1, 2])
    check("as_type='json' on array", get_env("V", as_type="json") == [1, 2])

    # get_secret is a true alias
    os.environ["V"] = '{"u":"x"}'
    check("get_secret == get_env (alias)", get_secret("V") == get_env("V") == {"u": "x"})
    _clear("V")


# ─── list_env / list_secret: scoping, source tagging, NO value leak ──────────
def test_list_env():
    # Clean slate: remove any user-key map, set a precise one + mixed vars.
    _clear("MICROVM_USER_ENV_KEYS", "U_SEC", "U_JSON", "U_ENV")
    os.environ.update({
        "U_SEC": "sk-plaintext-SECRETVAL",
        "U_JSON": '{"password": "P@SS-SECRET"}',
        "U_ENV": "debug",
        # platform/system vars that MUST be hidden by default
        "ATHENA_DB": "d", "AWS_REGION": "us-west-2", "SAGEMAKER_TRAIN_INSTANCE": "ml.m5",
        "MICROVM_USER_ENV_KEYS": json.dumps({"U_SEC": "secret", "U_JSON": "secret", "U_ENV": "env"}),
    })

    e = list_env()
    names = set(e["name"])
    # NEW contract: list_env() shows ONLY direct env vars — secrets are excluded.
    check("list_env default = ONLY the direct env var (secrets excluded)", names == {"U_ENV"})
    check("list_env source tags all 'env'", all(src == "env" for src in e["source"]))

    s = list_secret()
    check("list_secret = only the 2 secrets", set(s["name"]) == {"U_SEC", "U_JSON"})
    check("list_secret all source=secret", all(src == "secret" for src in s["source"]))
    check("list_secret JSON value classified json", s[s.name == "U_JSON"]["looks_like"].iloc[0] == "json")
    check("list_secret plaintext classified text", s[s.name == "U_SEC"]["looks_like"].iloc[0] == "text")

    # env and secret listings are DISJOINT (no overlap).
    check("list_env and list_secret are disjoint", not (set(e["name"]) & set(s["name"])))

    # NO VALUE must appear anywhere in either frame.
    blob = e.to_string() + s.to_string()
    check("list_env/list_secret NEVER leak a value",
          "sk-plaintext-SECRETVAL" not in blob and "P@SS-SECRET" not in blob and "debug" not in blob)

    # all=True includes system vars (source col present, no crash)
    ea = list_env(all=True)
    check("list_env(all=True) includes system vars",
          {"ATHENA_DB", "AWS_REGION"}.issubset(set(ea["name"])))
    check("list_env(all=True) still leaks no user value",
          "sk-plaintext-SECRETVAL" not in ea.to_string())

    # Fallback path (old image, NO source map):
    #  • list_env() → denylist fallback: hides system vars, shows the rest as 'env'
    #    (best effort — can't prove which are secrets, so all shown as env).
    #  • list_secret() → empty (a secret can't be identified without the map).
    #  • list_env(all=True) → full dump regardless of map.
    _clear("MICROVM_USER_ENV_KEYS")
    e2 = list_env()
    n2 = set(e2["name"])
    check("no-map list_env hides ATHENA_/AWS_/SAGEMAKER_",
          not ({"ATHENA_DB", "AWS_REGION", "SAGEMAKER_TRAIN_INSTANCE"} & n2))
    check("no-map list_env shows user-ish vars as 'env'", {"U_SEC", "U_JSON", "U_ENV"} <= n2)
    check("no-map list_env all source=env", all(src == "env" for src in e2["source"]))
    check("no-map list_secret → empty", len(list_secret()) == 0)
    check("no-map list_env(all=True) still dumps system vars",
          {"ATHENA_DB", "AWS_REGION"}.issubset(set(list_env(all=True)["name"])))
    _clear("U_SEC", "U_JSON", "U_ENV", "ATHENA_DB", "SAGEMAKER_TRAIN_INSTANCE")


# ─── EMPTY source map (nothing configured) → ZERO user vars, no KMP_* leak ───
def test_empty_map_and_native_runtime_vars():
    """The reported bug: a notebook with NOTHING configured still showed 2 rows
    (KMP_DUPLICATE_LIB_OK, KMP_INIT_AT_FORK) in list_env(). Two guarantees:
      1. An EMPTY source map ({}) is authoritative → list_env() shows ZERO vars
         (even though native-runtime vars are present in os.environ).
      2. Even on the DENYLIST fallback (no map at all / old image), the native ML
         runtime vars (KMP_*, OMP_*, MKL_*, …) are hidden."""
    # Native/runtime vars the baked libs set in-process (not user config).
    os.environ.update({
        "KMP_DUPLICATE_LIB_OK": "TRUE",
        "KMP_INIT_AT_FORK": "FALSE",
        "OMP_NUM_THREADS": "4",
        "MKL_NUM_THREADS": "4",
    })

    # (1) Authoritative EMPTY map → zero user vars.
    os.environ["MICROVM_USER_ENV_KEYS"] = json.dumps({})
    e_empty = list_env()
    check("empty source map → list_env shows ZERO vars", len(e_empty) == 0)
    s_empty = list_secret()
    check("empty source map → list_secret shows ZERO secrets", len(s_empty) == 0)

    # (2) Denylist fallback (no map) must also hide the native runtime vars.
    _clear("MICROVM_USER_ENV_KEYS")
    e_fb = list_env()
    nfb = set(e_fb["name"])
    check("denylist fallback hides KMP_*/OMP_*/MKL_*",
          not ({"KMP_DUPLICATE_LIB_OK", "KMP_INIT_AT_FORK",
                "OMP_NUM_THREADS", "MKL_NUM_THREADS"} & nfb))
    _clear("KMP_DUPLICATE_LIB_OK", "KMP_INIT_AT_FORK", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


# ─── /run hook contract: ALWAYS publishes a map, {} when nothing configured ──
def test_hook_publishes_empty_map():
    """Lock in the hook fix: when the launch payload carries no user_env_keys, the
    hook must publish an EMPTY map ({}) — NOT pop the var — so list_env() has an
    authoritative 'nothing configured' signal (and doesn't fall back to the leaky
    denylist). Replicates app/platform/hooks.py's publish logic."""
    _clear("MICROVM_USER_ENV_KEYS")

    def _publish(user_env_keys):  # mirror of hooks.py
        ks = user_env_keys or {}
        if not isinstance(ks, dict):
            ks = {}
        os.environ["MICROVM_USER_ENV_KEYS"] = json.dumps({str(k): str(v) for k, v in ks.items()})

    _publish({})  # nothing configured this launch
    check("hook publishes {} (not absent) when nothing configured",
          os.environ.get("MICROVM_USER_ENV_KEYS") == "{}")
    from app.notebook.helpers.env import _user_key_sources
    check("_user_key_sources on {} → empty dict (NOT None)", _user_key_sources() == {})

    _publish({"MY_VAR": "env"})
    check("hook publishes populated map", json.loads(os.environ["MICROVM_USER_ENV_KEYS"]) == {"MY_VAR": "env"})
    _clear("MICROVM_USER_ENV_KEYS")


# ─── The /launch → /run contract: user_env_keys captured BEFORE platform merge ──
def test_user_env_key_capture():
    """Replicate the exact logic in proxy/platform/routes/microvm.py (capture
    BEFORE the launch_env_vars merge) + the /run hook recording, to lock in the
    regression where platform vars were wrongly tagged as user vars."""
    # user set: 1 direct env + 2 secrets
    user_env_vars = {"MY_FLAG": "1"}
    user_secrets = [
        {"name": "microvm/api", "arn": "arn:...", "envVar": "DEMO_API_KEY", "secretKey": ""},
        {"name": "microvm/db", "arn": "arn:...", "envVar": "DB_CREDS", "secretKey": ""},
    ]
    # ---- microvm.py: capture BEFORE merge ----
    user_env_keys = {}
    for k in user_env_vars.keys():
        if k:
            user_env_keys[k] = "env"
    for s in user_secrets:
        if isinstance(s, dict) and s.get("envVar"):
            user_env_keys[s["envVar"]] = "secret"
    # platform merge happens AFTER capture (must NOT affect user_env_keys)
    platform = {"ATHENA_DB": "d", "AWS_REGION": "us-west-2", "ARTIFACT_BUCKET": "b"}
    merged_env = {**platform, **user_env_vars}  # what actually goes to the VM

    check("capture excludes platform vars",
          set(user_env_keys) == {"MY_FLAG", "DEMO_API_KEY", "DB_CREDS"})
    check("secrets tagged secret, env tagged env",
          user_env_keys == {"MY_FLAG": "env", "DEMO_API_KEY": "secret", "DB_CREDS": "secret"})
    check("merged env (to VM) still carries platform vars",
          "ATHENA_DB" in merged_env and "MY_FLAG" in merged_env)

    # ---- /run hook: publish MICROVM_USER_ENV_KEYS from user_env_keys (NOT merged) ----
    _clear("MICROVM_USER_ENV_KEYS")
    if isinstance(user_env_keys, dict) and user_env_keys:
        os.environ["MICROVM_USER_ENV_KEYS"] = json.dumps(
            {str(k): str(v) for k, v in user_env_keys.items()})
    # Simulate the VM env: merged env + resolved secrets all in os.environ
    os.environ.update(merged_env)
    os.environ["DEMO_API_KEY"] = "sk-x"
    os.environ["DB_CREDS"] = '{"u":"x"}'

    e = list_env()
    check("end-to-end list_env shows ONLY the direct env var (not secrets/platform)",
          set(e["name"]) == {"MY_FLAG"})
    s = list_secret()
    check("end-to-end list_secret shows the 2 secrets", set(s["name"]) == {"DEMO_API_KEY", "DB_CREDS"})
    _clear("MICROVM_USER_ENV_KEYS", "MY_FLAG", "DEMO_API_KEY", "DB_CREDS",
           "ATHENA_DB", "AWS_REGION", "ARTIFACT_BUCKET")


def test_live_record_and_no_map():
    """record_user_env_key keeps the source map current for vars added to a RUNNING
    VM (set-env / refresh-secrets); and list_secret with NO map returns empty rather
    than mislabeling env vars as secrets."""
    from app.notebook.helpers.env import _record_user_env_key

    # No source map at all → list_secret must be EMPTY (not env vars shown as secret)
    _clear("MICROVM_USER_ENV_KEYS")
    os.environ.update({"LIVE_ENV": "x", "LIVE_SEC": "y"})
    check("list_secret with no map → empty", len(list_secret()) == 0)

    # Live-add an env var then a secret via the recorder (simulates set-env /
    # refresh-secrets updating the map on a running VM).
    _record_user_env_key("LIVE_ENV", "env")
    _record_user_env_key("LIVE_SEC", "secret")
    e = list_env()
    check("live-recorded env var appears in list_env (secret excluded)", set(e["name"]) == {"LIVE_ENV"})
    check("live list_env source=env", all(src == "env" for src in e["source"]))
    s = list_secret()
    check("live list_secret shows only the secret", set(s["name"]) == {"LIVE_SEC"})
    _clear("MICROVM_USER_ENV_KEYS", "LIVE_ENV", "LIVE_SEC")


def _raises(exc, fn):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    test_get_env()
    test_list_env()
    test_empty_map_and_native_runtime_vars()
    test_hook_publishes_empty_map()
    test_user_env_key_capture()
    test_live_record_and_no_map()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ ALL ENV-HELPER TESTS PASSED")
