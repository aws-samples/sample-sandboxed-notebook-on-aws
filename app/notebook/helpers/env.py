"""UTILITIES — read env vars & secrets from os.environ with smart typing."""

import os
import json



# =============================================================================
# UTILITIES — environment variables & secrets
# =============================================================================
# (Categorized under UTILITIES so get_env/get_secret group with the other utility
# helpers in the Snippets panel / autocomplete — the category comes from this
# header via completion_catalog.)
#
# Both plain env vars (Direct env vars in the Secrets & Env panel) and
# Secrets-Manager-backed secrets land in os.environ at VM launch (the /run hook
# injects them), so a single reader covers BOTH. get_env is the primary; get_secret
# is a thin alias with secret-flavored wording — identical behavior.
#
# The value in os.environ is always a STRING. get_env smartly turns it into a useful
# Python value:
#   • auto (as_type=None, the default): a JSON OBJECT or ARRAY value is parsed to a
#     dict/list (so a JSON credentials secret just works); any other value stays a
#     STRING. We deliberately do NOT auto-coerce bare numbers/booleans — a plaintext
#     secret like an all-digits API key or a zero-padded id must not silently become
#     an int (losing leading zeros / changing type). Opt into that with as_type.
#   • explicit (as_type=int|float|bool|str|json|dict|list): force the conversion.


# Sentinel so get_env(..., default=None) can be distinguished from "no default".
# A named class with a clean, STABLE repr — a bare object() reprs as
# "<object object at 0x...>" (a changing memory address), which leaked into the
# helper signature shown in autocomplete / the generated helpers_block (and made
# that generated file differ on every run → perpetually "stale").
class _Unset:
    __slots__ = ()
    def __repr__(self):
        return "UNSET"


_UNSET = _Unset()

# Values accepted as True/False for as_type=bool (case-insensitive).
_BOOL_TRUE = {"1", "true", "t", "yes", "y", "on"}
_BOOL_FALSE = {"0", "false", "f", "no", "n", "off", ""}


def _coerce(name: str, raw: str, as_type):
    """Coerce the raw string `raw` to `as_type`. Raises ValueError with a clear,
    actionable message on failure (so a bad value / wrong type is obvious in the
    cell, not a cryptic traceback)."""
    # Normalize string aliases ("int", "json", …) and real types (int, dict, …).
    key = as_type
    if isinstance(as_type, type):
        key = {int: "int", float: "float", bool: "bool", str: "str",
               dict: "dict", list: "list"}.get(as_type, getattr(as_type, "__name__", "str"))
    key = str(key).lower()

    if key == "str":
        return raw
    if key in ("json", "dict", "list"):
        try:
            val = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(
                f"get_env({name!r}, as_type={as_type!r}): value is not valid JSON "
                f"({e}). Got: {raw[:80]!r}"
            ) from e
        if key == "dict" and not isinstance(val, dict):
            raise ValueError(f"get_env({name!r}, as_type=dict): JSON is a "
                             f"{type(val).__name__}, not an object.")
        if key == "list" and not isinstance(val, list):
            raise ValueError(f"get_env({name!r}, as_type=list): JSON is a "
                             f"{type(val).__name__}, not an array.")
        return val
    if key == "int":
        try:
            return int(raw)
        except (ValueError, TypeError) as e:
            raise ValueError(f"get_env({name!r}, as_type=int): {raw!r} is not an "
                             f"integer.") from e
    if key == "float":
        try:
            return float(raw)
        except (ValueError, TypeError) as e:
            raise ValueError(f"get_env({name!r}, as_type=float): {raw!r} is not a "
                             f"number.") from e
    if key == "bool":
        s = raw.strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
        raise ValueError(
            f"get_env({name!r}, as_type=bool): {raw!r} isn't a recognized boolean "
            f"(use one of true/false/1/0/yes/no/on/off)."
        )
    raise ValueError(
        f"get_env({name!r}): unsupported as_type={as_type!r}. Use int, float, bool, "
        f"str, dict, list, or 'json'."
    )


def _auto(raw: str):
    """Default (as_type=None) conversion: parse a JSON OBJECT/ARRAY to dict/list;
    leave everything else as the raw string. Never coerces bare scalars (so a
    numeric-looking plaintext secret keeps its exact string form)."""
    s = raw.lstrip()
    if s[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw  # looked like JSON but wasn't — hand back the raw string
    return raw


def get_env(name, default=_UNSET, *, as_type=None, required=False):
    """
    Read an environment variable (or a Secrets-Manager secret) by name, with smart
    typing — the one-liner that replaces hand-written os.environ + json.loads.

    Both plain env vars and secrets land in os.environ at VM launch, so this reads
    EITHER. Configure them in the Secrets & Env panel.

    Args:
      name:     the env var / secret's target name (e.g. "DEMO_API_KEY").
      default:  value to return when the var is unset. If omitted entirely, an unset
                var returns None (unless required=True).
      as_type:  force a conversion of the string value. One of int, float, bool, str,
                dict, list, or "json". When None (default): a JSON object/array value
                is auto-parsed to a dict/list, and anything else is returned as the
                raw string (bare numbers/booleans are NOT auto-coerced — pass
                as_type to opt in, so a numeric-looking string isn't silently
                changed).
      required: if True, raise KeyError when the var is unset (ignores `default`).

    Returns: the typed value, or `default` (or None) when unset.

    Examples:
      key    = get_secret("DEMO_API_KEY")               # dict if JSON, else the string
      creds  = get_secret("DB_CREDS")                   # {"username": ..., "password": ...}
      port   = get_env("DB_PORT", 5432, as_type=int)    # int, default 5432
      debug  = get_env("DEBUG", False, as_type=bool)    # bool
      token  = get_env("API_TOKEN", required=True)      # raises if not set
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if required:
            raise KeyError(
                f"get_env({name!r}, required=True): not set. Add it in the "
                f"Secrets & Env panel (or pass a default)."
            )
        return None if default is _UNSET else default

    try:
        return _coerce(name, raw, as_type) if as_type is not None else _auto(raw)
    except ValueError:
        # A coercion error is a real user-facing problem — re-raise so the cell shows
        # it (don't silently fall back to a wrong-typed value or the default).
        raise


def get_secret(name, default=_UNSET, *, as_type=None, required=False):
    """
    Read a secret by its target env var name — an alias of get_env (secrets and env
    vars both resolve from os.environ at VM launch; identical behavior).

    Use the name that reads best: get_secret("DB_CREDS") for a Secrets-Manager
    secret, get_env("DB_PORT", as_type=int) for a plain config var.

    A whole-value secret works whether it's JSON or plaintext: JSON parses to a
    dict/list, plaintext comes back as the string — no json.loads boilerplate, no
    JSONDecodeError on a plain string.

    Examples:
      creds = get_secret("DB_CREDS")              # {"username": ..., "password": ...}
      key   = get_secret("DEMO_API_KEY")          # "sk-..." (plaintext stays a string)
      tok   = get_secret("API_TOKEN", required=True)
    """
    return get_env(name, default, as_type=as_type, required=required)


# Platform/system env keys to HIDE from list_env() by default — the ones the user
# didn't set. Exact names + prefixes. The user's secrets/direct-env (from the
# Secrets & Env panel) are whatever's left after removing these, so the list stays
# focused on what the notebook actually configured. A prefix-based denylist is
# robust as launch_env_vars() grows (vs. a hand-kept allowlist that would drift).
_SYSTEM_ENV_NAMES = {
    "PATH", "HOME", "PWD", "OLDPWD", "SHELL", "TERM", "USER", "LOGNAME", "HOSTNAME",
    "LANG", "TZ", "SHLVL", "_", "TMPDIR", "LD_LIBRARY_PATH", "DISPLAY",
    # Platform knobs injected by microvm_manager.launch_env_vars() (not user-set):
    "IMAGE_BASE_NAME", "DEFAULT_IMAGE_ARCH", "ARTIFACT_BUCKET",
    "NOTEBOOK_MAX_DISPLAY_ROWS", "EXECUTION_TIMEOUT_SECONDS", "MICROVM_PRICING_JSON",
}
_SYSTEM_ENV_PREFIXES = (
    "AWS_", "PYTHON", "LC_", "MICROVM_", "ATHENA_", "DATASOURCE_", "AURORA_",
    "BEDROCK_", "SAGEMAKER_", "GLUE_", "S3TABLES_",
    # Native numeric/ML runtime knobs the baked libs (libomp/MKL/OpenBLAS/numexpr/
    # tokenizers) set in-process — never user config. Caught here so the DENYLIST
    # fallback (old images with no source map) doesn't leak them as "user env vars"
    # (e.g. KMP_DUPLICATE_LIB_OK, KMP_INIT_AT_FORK, OMP_NUM_THREADS).
    "KMP_", "OMP_", "MKL_", "OPENBLAS_", "NUMEXPR_", "VECLIB_", "TOKENIZERS_",
    "GOTO_", "BLIS_",
)


def _is_system_env(name: str) -> bool:
    if name in _SYSTEM_ENV_NAMES:
        return True
    return any(name.startswith(p) for p in _SYSTEM_ENV_PREFIXES)


def _record_user_env_key(name: str, source: str) -> None:
    """Add/update ONE user key in the MICROVM_USER_ENV_KEYS source map (the JSON
    {name: "secret"|"env"} that list_env()/list_secret() read). Called by the live
    set-env / refresh-secrets endpoints so a var/secret added to a RUNNING VM is
    tracked immediately — the /run hook only sets the map at launch, so without this
    a live-added secret would show up untagged (and list_secret() couldn't find it).
    Best-effort; never raises into the caller."""
    if not name:
        return
    src = "secret" if source == "secret" else "env"
    try:
        cur = _user_key_sources() or {}
        cur[name] = src
        os.environ["MICROVM_USER_ENV_KEYS"] = json.dumps(cur)
    except Exception:
        pass


def _user_key_sources() -> dict | None:
    """Parse the {name: "secret"|"env"} map the /run hook published in
    MICROVM_USER_ENV_KEYS. Returns None when absent (older VM image / nothing
    configured) so callers can fall back to the system-var denylist. Tolerates a
    legacy comma-joined list (treats every key as source 'env')."""
    raw = os.environ.get("MICROVM_USER_ENV_KEYS")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
    except (json.JSONDecodeError, TypeError):
        pass
    # Legacy comma-joined list → unknown source, call it 'env'.
    return {k: "env" for k in raw.split(",") if k}


def _list_env(all: bool, kind: str, label: str):
    """Shared implementation for list_env/list_secret. `kind` is 'all'|'secret'|
    'env' — the source filter applied to the user-configured keys:
      • 'env'    → list_env(): only DIRECT env vars  (source == 'env')
      • 'secret' → list_secret(): only SECRETS        (source == 'secret')
      • 'all'    → list_env(all=True): the full dump (system + user, unfiltered)
    KEYS ONLY; a value is NEVER included in the output."""
    import pandas as pd

    # all=True is the raw full dump → no source map, no kind filter (everything).
    sources = None if all else _user_key_sources()

    # SECRETS can ONLY be identified from the source map — a secret is
    # indistinguishable from a plain env var by name/value alone. So list_secret()
    # with no map must NOT guess: return empty with a hint rather than mislabel env
    # vars as secrets. (A freshly launched VM always has a map — even {} — so this
    # is only the old-image case.)
    if kind == "secret" and not all and sources is None:
        print("  Can't determine secrets on this VM (no source map). Relaunch the "
              "notebook to populate it.")
        return pd.DataFrame(columns=["name", "source", "looks_like", "length"])

    rows = []
    for name in sorted(os.environ):
        source = "env"  # default label for a var with no recorded source
        if not all:
            if sources is not None:
                # The map is the authoritative ALLOWLIST: a var not in it is a
                # system/platform var → skip. Its recorded source drives the filter.
                if name not in sources:
                    continue
                source = sources[name]
            elif _is_system_env(name):
                # No map (old image): fall back to the system-var DENYLIST. Only
                # reachable for kind='env' (the secret+no-map case returned above),
                # so unlisted vars are shown as best-effort 'env'.
                continue
            # Source filter: list_env() shows only 'env', list_secret() only
            # 'secret'. (kind=='all' is all=True and never reaches here.)
            if source != kind:
                continue
        raw = os.environ.get(name) or ""
        s = raw.lstrip()
        looks = "text"
        if s[:1] in ("{", "["):
            try:
                json.loads(raw)
                looks = "json"
            except (json.JSONDecodeError, TypeError):
                looks = "text"
        rows.append({
            "name": name,
            "source": source,          # 'secret' (Secrets Manager) | 'env' (direct)
            "looks_like": looks,
            "length": len(raw),        # length only — NEVER the value itself
        })

    df = pd.DataFrame(rows, columns=["name", "source", "looks_like", "length"])
    if len(df):
        print(f"  {len(df)} {label} (keys only — values hidden)")
    elif not all:
        if kind == "secret":
            hint = ("No Secrets-Manager secrets configured for this notebook. Add "
                    "one in the Secrets & Env panel's Secrets picker.")
        else:
            hint = ("No direct env vars configured for this notebook. Add one in "
                    "the Secrets & Env panel, or call list_secret() for secrets / "
                    "list_env(all=True) for system vars.")
        print("  " + hint)
    return df


def list_env(all: bool = False):
    """
    List the DIRECT environment variables this notebook configured, as a DataFrame
    — the discovery companion to get_env. Shows KEYS ONLY, never the values.

    Scope is the mirror of list_secret(): list_env() shows ONLY direct env vars
    (source == 'env'); SECRETS are NOT included here — use list_secret() for those.
    (Both still land in os.environ, so get_env/get_secret can read either by name;
    this is just the listing split so each function shows one kind.)

    Columns: name, source (always 'env' here), looks_like ('json' when the value
    parses as a JSON object/array, else 'text'), length (character count — a
    populated-ness check that never reveals the value).

    By default lists only the env vars THIS notebook set in the Secrets & Env panel
    — the platform's own system/config vars (AWS_*, MICROVM_*, ATHENA_*, PATH, …)
    are hidden. Pass all=True for the full raw dump (system + user, env + secrets).

    Example:
        list_env()                 # your direct env vars (keys only)
        list_secret()              # your secrets (keys only)
        list_env(all=True)         # everything, including system/platform vars
        get_env("DB_PORT", as_type=int)
    """
    return _list_env(all=all, kind="env",
                     label=("all env vars" if all else "your direct env vars"))


def list_secret():
    """
    List the Secrets-Manager-backed secrets this notebook configured, as a
    DataFrame — the mirror of list_env(). Shows KEYS ONLY, never values.

    Secret-scoped: lists exactly the keys configured from the Secrets & Env panel's
    Secrets Manager picker (source == 'secret'). Direct env vars are NOT shown here
    — use list_env() for those. There is NO `all` option (a system/platform env var
    is never a secret); use list_env(all=True) for the full raw env dump.

    Columns: name, source (always 'secret'), looks_like ('json'/'text'), length.

    Example:
        list_secret()              # just your Secrets-Manager secrets (keys only)
        list_env()                 # just your direct env vars (keys only)
        get_secret("DB_CREDS")     # then read one by name
    """
    return _list_env(all=False, kind="secret", label="your Secrets-Manager secrets")
