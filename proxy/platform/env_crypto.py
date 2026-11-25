"""Envelope encryption for notebook ENV VAR values at rest.

Why this exists
---------------
The Secrets & Env panel has two sections. SECRETS are stored as REFERENCES only
(name/arn/envVar/secretKey) — a value never touches our DB, so nothing to protect
there. ENV VARS, however, are plain key→value pairs the user types, and they used
to be written to notebook_env.env_json in cleartext. Anyone who could read the DB
(the SQLite file, or the synced Aurora replica) could read every env var value.

This module seals each env var VALUE before it is written and unseals it on read,
so a DB reader sees ciphertext only. The design (agreed with the owner):

  • MASTER KEY — 32 random bytes, base64, in AWS Secrets Manager at
    `microvm-internal/env-master-key`. That name is deliberately OUTSIDE the
    `microvm/` prefix, which matters twice over:
      - the VM execution role's secretsmanager:GetSecretValue is scoped to
        `microvm/*`, so a MicroVM (even a fully compromised one) physically cannot
        read the master key; and
      - the Secrets & Env / launch dropdowns list only `microvm/*`, so it never
        shows up as an attachable secret.
    Only the PROXY (which does the encrypt/decrypt) can read it.

  • PER-NOTEBOOK SUBKEY — subkey = HKDF-SHA256(master, salt=notebook_id,
    info="notebook-env-v1", length=32). A leaked subkey for one notebook can't
    decrypt another's. The salt (notebook_id) need not be secret — it is the stable
    integer PK of notebook_env, always present at read/write time (session_id, by
    contrast, can rotate). The SECRET is the master key, which is not in the DB.

  • PER-VALUE SEAL — AES-256-GCM with a fresh random 12-byte nonce. GCM is
    authenticated (tamper-evident). A sealed value is stored as
    {"v": 1, "nonce": b64, "ct": b64} in place of the plaintext string.

Threat model / limits
----------------------
Protects against a reader of the DB alone (SQLite file or Aurora replica) — they
get ciphertext with no key. Does NOT protect against a compromise of the proxy
HOST (the proxy must be able to decrypt to inject values into the VM at launch, so
it necessarily holds the master key). Upgrading the master-key SOURCE from Secrets
Manager to a KMS Encrypt/Decrypt call (host never holds raw key material + a
CloudTrail audit trail) is the natural next step; this module isolates that choice
behind _load_master_key().

Failure policy
--------------
Encryption FAILS CLOSED: if the master key can't be loaded, seal_env_vars raises,
so we refuse to persist an env var rather than silently write plaintext. Decryption
is best-effort per value: an unreadable/legacy value is returned as-is (so one bad
row can't break a whole notebook's launch), with a warning.
"""

import base64
import logging
import os
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

# Master-key secret NAME (not ARN) — resolved by Secrets Manager within the
# caller's account/region, so we don't need the account id here. Deliberately NOT
# under the `microvm/` prefix (see module docstring). Overridable for tests/rotation.
MASTER_KEY_SECRET_NAME = os.environ.get(
    "ENV_MASTER_KEY_SECRET_NAME", "microvm-internal/env-master-key"
)

# HKDF context string — bump alongside the stored `v` if the scheme ever changes.
_HKDF_INFO = b"notebook-env-v1"
_SCHEME_VERSION = 1

# The master key is fetched once and cached process-wide (the proxy is the only
# encryptor/decryptor). Guarded by a lock so concurrent requests fetch it once.
_master_key: bytes | None = None
_master_key_lock = threading.Lock()


class EnvCryptoUnavailable(RuntimeError):
    """Raised when the master key can't be loaded — callers fail closed on write."""


def _region() -> str:
    # Import lazily to avoid a hard module dependency / import cycle at load time.
    try:
        from proxy.platform.microvm_manager import AWS_REGION
        return AWS_REGION
    except Exception:
        return os.environ.get("AWS_REGION", "us-west-2")


def _load_master_key() -> bytes:
    """Fetch + cache the 32-byte master key from Secrets Manager.

    The stored SecretString is base64 of 32 raw bytes. Cached process-wide after the
    first successful load. Raises EnvCryptoUnavailable on any failure so writes fail
    closed. This is the single seam to swap for KMS later."""
    global _master_key
    if _master_key is not None:
        return _master_key
    with _master_key_lock:
        if _master_key is not None:  # re-check inside the lock
            return _master_key
        try:
            import boto3
            sm = boto3.client("secretsmanager", region_name=_region())
            resp = sm.get_secret_value(SecretId=MASTER_KEY_SECRET_NAME)
            raw_b64 = resp.get("SecretString", "")
            key = base64.b64decode(raw_b64)
            if len(key) != 32:
                raise EnvCryptoUnavailable(
                    f"master key must be 32 bytes (got {len(key)}) — regenerate "
                    f"{MASTER_KEY_SECRET_NAME}"
                )
            _master_key = key
            logger.info("🔐 env-crypto: master key loaded and cached")
            return _master_key
        except EnvCryptoUnavailable:
            raise
        except Exception as e:
            # Don't leak details; just say it's unavailable.
            raise EnvCryptoUnavailable(
                f"could not load env master key '{MASTER_KEY_SECRET_NAME}': "
                f"{type(e).__name__}"
            ) from e


def _subkey(notebook_id) -> bytes:
    """Derive the per-notebook 32-byte AES key: HKDF(master, salt=notebook_id)."""
    master = _load_master_key()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=str(notebook_id).encode("utf-8"),
        info=_HKDF_INFO,
    )
    return hkdf.derive(master)


def _is_sealed(value) -> bool:
    """True if `value` is one of our seal envelopes ({"v","nonce","ct"})."""
    return (
        isinstance(value, dict)
        and value.get("v") == _SCHEME_VERSION
        and "nonce" in value
        and "ct" in value
    )


def seal_env_vars(notebook_id, env_vars: dict) -> dict:
    """Return a copy of `env_vars` with every VALUE sealed (keys unchanged).

    Each value → {"v":1, "nonce": b64, "ct": b64}. Values are coerced to str first
    (the API only ever stores strings). Raises EnvCryptoUnavailable if the master
    key is missing — caller MUST fail the write rather than persist plaintext."""
    if not env_vars:
        return {}
    key = _subkey(notebook_id)  # raises → fail closed
    aes = AESGCM(key)
    sealed: dict = {}
    for k, v in env_vars.items():
        # Already sealed? Pass through (idempotent — e.g. re-persisting a loaded blob).
        if _is_sealed(v):
            sealed[k] = v
            continue
        plaintext = str(v).encode("utf-8")
        nonce = os.urandom(12)
        ct = aes.encrypt(nonce, plaintext, None)  # GCM tag appended to ct
        sealed[k] = {
            "v": _SCHEME_VERSION,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }
    return sealed


def unseal_env_vars(notebook_id, env_vars: dict) -> dict:
    """Return a copy of `env_vars` with every sealed VALUE decrypted to its string.

    Best-effort per value: a value that isn't a v1 envelope is returned as-is (so a
    stray/legacy plaintext can't break the notebook), and a value that fails to
    decrypt (wrong key, corruption, tamper) is dropped with a warning rather than
    raising — a launch shouldn't crash because one env var is unreadable."""
    if not env_vars:
        return {}
    try:
        key = _subkey(notebook_id)
    except EnvCryptoUnavailable as e:
        # No key → we can't read anything. Return empty rather than exposing envelopes.
        logger.warning(f"env-crypto: cannot decrypt notebook {notebook_id} env: {e}")
        return {}
    aes = AESGCM(key)
    out: dict = {}
    for k, v in env_vars.items():
        if not _is_sealed(v):
            # Not our envelope — pass through as-is (defensive; shouldn't happen for
            # values written by seal_env_vars).
            out[k] = v
            continue
        try:
            nonce = base64.b64decode(v["nonce"])
            ct = base64.b64decode(v["ct"])
            out[k] = aes.decrypt(nonce, ct, None).decode("utf-8")
        except Exception:
            logger.warning(
                f"env-crypto: failed to decrypt env var '{k}' for notebook "
                f"{notebook_id} (skipped)"
            )
    return out


def reset_cache_for_tests() -> None:
    """Test hook: drop the cached master key so a test can re-point the secret."""
    global _master_key
    with _master_key_lock:
        _master_key = None
