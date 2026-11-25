#!/usr/bin/env bash
# ============================================================
# Setup IAM roles and S3 bucket for Lambda MicroVM sandbox
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

echo "============================================"
echo "  Lambda MicroVM Sandbox — IAM & S3 Setup"
echo "============================================"
echo "  Account:  $ACCOUNT_ID"
echo "  Region:   $AWS_REGION"
echo "  Bucket:   $ARTIFACT_BUCKET"
echo ""

# --- S3 Bucket ---
echo ">> Creating S3 bucket: $ARTIFACT_BUCKET"
if [ "$AWS_REGION" = "us-east-1" ]; then
  aws s3api create-bucket \
    --bucket "$ARTIFACT_BUCKET" \
    --profile "$AWS_CLI_PROFILE" \
    --region "$AWS_REGION" 2>/dev/null || echo "   (bucket already exists)"
else
  aws s3api create-bucket \
    --bucket "$ARTIFACT_BUCKET" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION" \
    --profile "$AWS_CLI_PROFILE" \
    --region "$AWS_REGION" 2>/dev/null || echo "   (bucket already exists)"
fi

# --- S3 lifecycle: expire transient/session-scoped data after 30 days ---
# Keeps the bucket from growing unbounded. Applied on every setup (idempotent —
# put-bucket-lifecycle-configuration REPLACES the whole config, so re-running just
# re-asserts these rules). Rules are per-prefix so long-lived data is untouched:
#   • sessions/       — per-session checkpoint state (dill blobs, files, metadata)
#   • user-data/      — files promoted from chat attachments to session data sources
#   • attachments/    — raw chat attachments (images/docs) uploaded to the AI panel
#   • athena-results/ — Athena query output spill (regenerated on every SQL run)
# NOT expired: samples/ (demo datasets), data-catalog/ (entity intel), images/
# (build artifacts), notebook-outputs/ (cell chart PNGs referenced from
# notebook_cells.image_s3_key; deleted with their cell/notebook and on
# local_install.sh --AuroraReset). The retention window is S3_CHECKPOINT_RETENTION_DAYS (the same
# project var local_install.sh uses — keep both setters on ONE source of truth so
# they can't disagree), defaulting to 30.
LIFECYCLE_EXPIRE_DAYS="${S3_CHECKPOINT_RETENTION_DAYS:-${LIFECYCLE_EXPIRE_DAYS:-30}}"
echo ">> Applying S3 lifecycle (expire transient data after ${LIFECYCLE_EXPIRE_DAYS} days)"
LIFECYCLE_JSON=$(cat <<EOF
{
  "Rules": [
    {
      "ID": "ExpireSessionCheckpoints",
      "Filter": { "Prefix": "sessions/" },
      "Status": "Enabled",
      "Expiration": { "Days": ${LIFECYCLE_EXPIRE_DAYS} }
    },
    {
      "ID": "ExpireUserData",
      "Filter": { "Prefix": "user-data/" },
      "Status": "Enabled",
      "Expiration": { "Days": ${LIFECYCLE_EXPIRE_DAYS} }
    },
    {
      "ID": "ExpireChatAttachments",
      "Filter": { "Prefix": "attachments/" },
      "Status": "Enabled",
      "Expiration": { "Days": ${LIFECYCLE_EXPIRE_DAYS} }
    },
    {
      "ID": "ExpireAthenaResults",
      "Filter": { "Prefix": "athena-results/" },
      "Status": "Enabled",
      "Expiration": { "Days": ${LIFECYCLE_EXPIRE_DAYS} }
    }
  ]
}
EOF
)
if aws s3api put-bucket-lifecycle-configuration \
    --bucket "$ARTIFACT_BUCKET" \
    --lifecycle-configuration "$LIFECYCLE_JSON" \
    --profile "$AWS_CLI_PROFILE" \
    --region "$AWS_REGION" 2>/dev/null; then
  echo "   ✓ Lifecycle applied: sessions/, user-data/, attachments/, athena-results/ → expire in ${LIFECYCLE_EXPIRE_DAYS}d"
else
  echo "   ⚠ Could not apply S3 lifecycle (non-fatal) — check s3:PutLifecycleConfiguration permission"
fi

# --- Build Role ---
echo ">> Creating build role: $BUILD_ROLE_NAME"
aws iam create-role \
  --role-name "$BUILD_ROLE_NAME" \
  --assume-role-policy-document file://"$ROOT_DIR/iam/build-role-trust.json" \
  --profile "$AWS_CLI_PROFILE" 2>/dev/null || echo "   (role already exists)"

# Substitute bucket name into permissions policy
POLICY=$(sed "s/\${ARTIFACT_BUCKET}/$ARTIFACT_BUCKET/g" "$ROOT_DIR/iam/build-role-permissions.json")
aws iam put-role-policy \
  --role-name "$BUILD_ROLE_NAME" \
  --policy-name "BuildPolicy" \
  --policy-document "$POLICY" \
  --profile "$AWS_CLI_PROFILE"

# --- Execution Role ---
echo ">> Creating execution role: $EXEC_ROLE_NAME"
aws iam create-role \
  --role-name "$EXEC_ROLE_NAME" \
  --assume-role-policy-document file://"$ROOT_DIR/iam/exec-role-trust.json" \
  --profile "$AWS_CLI_PROFILE" 2>/dev/null || echo "   (role already exists)"

aws iam put-role-policy \
  --role-name "$EXEC_ROLE_NAME" \
  --policy-name "ExecPolicy" \
  --policy-document file://"$ROOT_DIR/iam/exec-role-permissions.json" \
  --profile "$AWS_CLI_PROFILE"

# --- Env-var encryption master key (Secrets Manager) ---
# Notebook env var VALUES are sealed at rest by the proxy (proxy/platform/env_crypto.py)
# with a master key stored in Secrets Manager at microvm-internal/env-master-key.
# That name is DELIBERATELY outside the microvm/* prefix so the VM exec role above
# (secretsmanager:GetSecretValue on microvm/*) canNOT read it — only the proxy can.
# Provision it here too (idempotent) so a bare `setup_iam.sh` run leaves the account
# ready; local_install.sh also ensures it on every launch.
echo ""
bash "$(dirname "${BASH_SOURCE[0]}")/setup_env_master_key.sh" || \
  echo "   ⚠ Master-key setup failed (non-fatal here) — re-run scripts/setup_env_master_key.sh"

# NOTE on the PROXY role: today the proxy runs LOCALLY under your own AWS creds, so
# there is no proxy IAM role to attach a policy to, and none is created here. When
# you host the proxy under a dedicated role (EC2/ECS/Fargate), grant it read access
# to the master key using iam/proxy-role-permissions.json (GetSecretValue on
# microvm-internal/env-master-key*), ALONGSIDE the other permissions a hosted proxy
# needs (lambda-microvms, S3 artifacts, rds-data on the cluster, secretsmanager on
# microvm/*, bedrock). The VM exec role must NEVER get the master-key grant.

echo ""
echo "✅ Setup complete. Wait ~10 seconds for IAM propagation, then run:"
echo "   bash scripts/build_all_images.sh"
