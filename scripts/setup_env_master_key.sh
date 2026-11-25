#!/usr/bin/env bash
# ============================================================
# Provision the ENV-VAR encryption MASTER KEY (Secrets Manager)
# ============================================================
# Notebook ENV VAR values are sealed at rest (AES-256-GCM, per-notebook subkey)
# by the proxy — see proxy/platform/env_crypto.py. The master key from which every
# per-notebook subkey is derived lives in AWS Secrets Manager under the name below.
#
# The name is DELIBERATELY OUTSIDE the `microvm/` prefix. That prefix is the read
# scope of the MicroVM execution role (iam/exec-role-permissions.json:
# secretsmanager:GetSecretValue on microvm/*) AND the filter for the Secrets & Env /
# launch dropdowns. Keeping the master key at `microvm-internal/*` therefore means:
#   • a MicroVM (even fully compromised) CANNOT read it, and
#   • it never appears as an attachable secret in the UI.
# Only the PROXY — which does the encrypt/decrypt — reads it (locally, under your
# own AWS creds; or, in a hosted deployment, via iam/proxy-role-permissions.json).
#
# Idempotent: creates the secret with 32 random bytes (base64) if absent; otherwise
# leaves the existing key untouched (regenerating it would orphan every already-
# sealed value). Never prints the key.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

# Must match proxy/platform/env_crypto.py MASTER_KEY_SECRET_NAME.
ENV_MASTER_KEY_SECRET_NAME="${ENV_MASTER_KEY_SECRET_NAME:-microvm-internal/env-master-key}"

echo ">> Ensuring env-var master key: $ENV_MASTER_KEY_SECRET_NAME"

if aws secretsmanager describe-secret \
      --secret-id "$ENV_MASTER_KEY_SECRET_NAME" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" &>/dev/null; then
  echo "   (master key already exists — left untouched)"
else
  # 32 random bytes → base64. The proxy base64-decodes SecretString back to 32 raw
  # bytes for HKDF. Generated locally with openssl so the raw key is never echoed.
  KEY_B64="$(openssl rand -base64 32)"
  aws secretsmanager create-secret \
    --name "$ENV_MASTER_KEY_SECRET_NAME" \
    --description "Master key for notebook env-var encryption at rest (proxy-only; NOT under microvm/* so VMs can't read it)" \
    --secret-string "$KEY_B64" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  unset KEY_B64
  echo "   ✓ created (32-byte key, base64)"
fi

echo ""
echo "✅ Env-var master key ready."
echo "   Proxy reads it at startup (cached in-process) to seal/unseal env var values."
echo "   The VM exec role (microvm/*) intentionally has NO access to it."
