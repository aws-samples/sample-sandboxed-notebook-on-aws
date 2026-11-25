#!/usr/bin/env bash
# ============================================================
# Setup SageMaker for notebooks: the EXECUTION ROLE (training/transform jobs) AND
# the custom SERVING CONTAINER for real-time endpoints (Phase 2).
# ============================================================
# A notebook never runs training itself: it SUBMITS a SageMaker Training Job (or a
# Batch Transform job) that runs on SageMaker-managed compute. Those jobs run as
# their OWN IAM role — NOT the VM exec role — which SageMaker assumes. This script
# provisions that role (idempotently) and writes its ARN to scripts/.sagemaker_env,
# which local_install.sh sources AFTER config.sh so the resolved value wins.
#
# The VM exec role (iam/exec-role-permissions.json, applied by setup_iam.sh) already
# carries the sagemaker:Create*/Describe*/Stop* actions plus iam:PassRole for THIS
# role — so re-run setup_iam.sh if you added SageMaker after the last IAM apply.
#
# What the SageMaker role can do (iam/sagemaker-role-permissions.json):
#   • read training input + write model.tar.gz on the artifacts bucket
#   • pull the AWS-owned built-in algorithm container from ECR
#   • write CloudWatch logs / training metrics under /aws/sagemaker/*
# Nothing else — least-privilege, scoped to the artifacts bucket.
#
# Phase 2 (real-time endpoints): this script also builds + pushes the ONE custom
# serving container (serving/) that sm_deploy_* points SageMaker at — it serves BOTH
# sklearn (.joblib) and ONNX (.onnx) on SERVERLESS, where the stock SageMaker
# containers can't (they need a writable root fs / exceed the 10GB image limit). The
# build is IDEMPOTENT and content-addressed: the image tag is a hash of serving/, so
# a rerun only rebuilds when serving/ actually changed — otherwise it just re-resolves
# the already-pushed URI. The resolved URI is written to scripts/.sagemaker_env as
# SAGEMAKER_SERVING_IMAGE (blank ⇒ endpoints unavailable → sm_deploy_* raises clearly;
# training/in-VM scoring are unaffected). Set SAGEMAKER_SERVING_IMAGE in the env
# before running to pin a specific image and skip the build entirely.
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

SAGEMAKER_ROLE_NAME="${SAGEMAKER_ROLE_NAME:-MicroVMSageMakerRole}"

echo "============================================"
echo "  Sandboxed Notebook on AWS — SageMaker Role Setup"
echo "============================================"
echo "  Account:  $ACCOUNT_ID"
echo "  Region:   $AWS_REGION"
echo "  Role:     $SAGEMAKER_ROLE_NAME"
echo "  Bucket:   $ARTIFACT_BUCKET (training IO)"
echo ""

# --- SageMaker execution role (trust = sagemaker.amazonaws.com) ---
echo ">> Creating SageMaker execution role: $SAGEMAKER_ROLE_NAME"
aws iam create-role \
  --role-name "$SAGEMAKER_ROLE_NAME" \
  --assume-role-policy-document file://"$ROOT_DIR/iam/sagemaker-role-trust.json" \
  --profile "$AWS_CLI_PROFILE" 2>/dev/null || echo "   (role already exists)"

# Inline permissions policy (idempotent — put-role-policy REPLACES it, so a rerun
# re-asserts the current document). No bucket substitution needed: the policy scopes
# by the microvm-sandbox-artifacts-* wildcard, matching any account/region bucket.
aws iam put-role-policy \
  --role-name "$SAGEMAKER_ROLE_NAME" \
  --policy-name "SageMakerExecPolicy" \
  --policy-document file://"$ROOT_DIR/iam/sagemaker-role-permissions.json" \
  --profile "$AWS_CLI_PROFILE"

SAGEMAKER_EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SAGEMAKER_ROLE_NAME}"
echo "   ✓ Role ready: $SAGEMAKER_EXEC_ROLE_ARN"

# ============================================================
#  Phase 2 — build + push the custom serving container (idempotent)
# ============================================================
# Resolves SAGEMAKER_SERVING_IMAGE. Order of preference:
#   1. If SAGEMAKER_SERVING_IMAGE is already set in the env, honor it (pin/override).
#   2. Else compute a content-hash tag over serving/, and if that image already
#      exists in ECR, reuse it (no build).
#   3. Else build serving/ for linux/amd64 with finch or docker, push, use that URI.
# If no container tool is available AND the image isn't already in ECR, we leave
# SAGEMAKER_SERVING_IMAGE blank and warn — training still works; only sm_deploy_* is
# gated until an image exists.
SERVING_DIR="$ROOT_DIR/serving"

# The value we'll ultimately write to .sagemaker_env (empty = endpoints not ready).
RESOLVED_SERVING_IMAGE="${SAGEMAKER_SERVING_IMAGE:-}"

if [ -n "$RESOLVED_SERVING_IMAGE" ]; then
  echo ""
  echo ">> Serving image pinned via env — skipping build:"
  echo "   $RESOLVED_SERVING_IMAGE"
elif [ ! -d "$SERVING_DIR" ]; then
  echo ""
  echo "   ⚠ serving/ directory not found — skipping serving-image build."
  echo "     Real-time endpoints (sm_deploy_*) will be unavailable until it's built."
else
  echo ""
  echo ">> Resolving custom serving container (repo: $SAGEMAKER_SERVING_REPO)"

  # --- Content-hash tag: rebuild only when serving/ changes ---
  # Hash the exact files that define the image. shasum is present on macOS + Linux;
  # take the first 12 hex chars for a short, stable tag. Guarded (|| true) so a
  # transient read issue under `set -e`/pipefail can't abort the whole setup.
  SERVING_HASH=$(cat \
      "$SERVING_DIR/Dockerfile" \
      "$SERVING_DIR/serve" \
      "$SERVING_DIR/serve_app.py" \
      "$SERVING_DIR/requirements.txt" 2>/dev/null \
    | shasum -a 256 | awk '{print substr($1,1,12)}' || true)
  SERVING_TAG="sha-${SERVING_HASH}"
  ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  CANDIDATE_IMAGE="${ECR_REGISTRY}/${SAGEMAKER_SERVING_REPO}:${SERVING_TAG}"
  echo "   Content tag: $SERVING_TAG"

  # --- Ensure the ECR repo exists (idempotent) ---
  if ! aws_ecr describe-repositories --repository-names "$SAGEMAKER_SERVING_REPO" \
        >/dev/null 2>&1; then
    echo "   Creating ECR repository: $SAGEMAKER_SERVING_REPO"
    aws_ecr create-repository --repository-name "$SAGEMAKER_SERVING_REPO" \
      --image-scanning-configuration scanOnPush=true >/dev/null
  fi

  # --- Does this exact content tag already exist? Then reuse, no build. ---
  if aws_ecr describe-images --repository-name "$SAGEMAKER_SERVING_REPO" \
        --image-ids imageTag="$SERVING_TAG" >/dev/null 2>&1; then
    echo "   ✓ Image already in ECR for this content — reusing (no rebuild)."
    RESOLVED_SERVING_IMAGE="$CANDIDATE_IMAGE"
  else
    # --- Need to build. Pick a container tool. ---
    CONTAINER_TOOL=""
    if command -v finch >/dev/null 2>&1; then
      CONTAINER_TOOL="finch"
    elif command -v docker >/dev/null 2>&1; then
      CONTAINER_TOOL="docker"
    fi

    if [ -z "$CONTAINER_TOOL" ]; then
      echo "   ⚠ Neither finch nor docker found on PATH — cannot build serving image."
      echo "     Real-time endpoints (sm_deploy_*) will be unavailable. Install finch"
      echo "     or docker and re-run: bash scripts/setup_sagemaker.sh"
    else
      echo "   Building with $CONTAINER_TOOL (linux/amd64) → $CANDIDATE_IMAGE"

      # Build failures must NOT abort the whole setup (they'd leave the Phase 1 role
      # config unwritten). Run login/build/push in a guarded block that degrades to
      # "endpoints disabled" on any failure — the .sagemaker_env write below still
      # persists the exec-role ARN regardless.
      build_ok=true
      {
        # ECR login (pipe the token to the tool's registry login).
        aws_ecr get-login-password \
          | "$CONTAINER_TOOL" login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null &&
        # Build for x86_64 (SageMaker serverless is x86 CPU) and push.
        "$CONTAINER_TOOL" build --platform linux/amd64 \
          -t "$CANDIDATE_IMAGE" "$SERVING_DIR" &&
        "$CONTAINER_TOOL" push "$CANDIDATE_IMAGE"
      } || build_ok=false

      if [ "$build_ok" = "true" ]; then
        echo "   ✓ Pushed $CANDIDATE_IMAGE"
        RESOLVED_SERVING_IMAGE="$CANDIDATE_IMAGE"
      else
        echo "   ⚠ Serving image build/push failed — real-time endpoints will be"
        echo "     unavailable (sm_deploy_* raises a clear error). Fix the build tool"
        echo "     and re-run: bash scripts/setup_sagemaker.sh"
        RESOLVED_SERVING_IMAGE=""
      fi
    fi
  fi
fi

# --- Emit resolved config for local_install.sh to source AFTER config.sh ---
# Mirrors scripts/.aurora_env / scripts/.kb_env: the discovered/created ARN is
# written here so a run wins over the blank default in config.sh. Also carries the
# default instance types + the resolved serving image (overridable via the
# environment before this script runs).
SAGEMAKER_ENV_FILE="$(dirname "${BASH_SOURCE[0]}")/.sagemaker_env"
cat > "$SAGEMAKER_ENV_FILE" <<EOF
# AUTO-GENERATED by scripts/setup_sagemaker.sh — do not edit by hand.
# Sourced by local_install.sh AFTER config.sh so these resolved values win.
export SAGEMAKER_EXEC_ROLE_ARN="${SAGEMAKER_EXEC_ROLE_ARN}"
export SAGEMAKER_TRAIN_INSTANCE="${SAGEMAKER_TRAIN_INSTANCE:-ml.m5.xlarge}"
export SAGEMAKER_TRANSFORM_INSTANCE="${SAGEMAKER_TRANSFORM_INSTANCE:-ml.m5.xlarge}"
export SAGEMAKER_ARTIFACT_PREFIX="${SAGEMAKER_ARTIFACT_PREFIX:-sagemaker/}"
# Phase 2 serving container (blank ⇒ real-time endpoints unavailable; sm_deploy_*
# raises a clear error). Resolved above: reused from ECR when serving/ is unchanged,
# rebuilt + pushed when it changed, or honored as-is when pinned via the env.
export SAGEMAKER_SERVING_IMAGE="${RESOLVED_SERVING_IMAGE}"
EOF
echo "   ✓ Wrote $SAGEMAKER_ENV_FILE"

echo ""
echo "✅ SageMaker setup complete."
echo "   • Ensure the VM exec role has the latest policy (SageMaker actions + PassRole):"
echo "       bash scripts/setup_iam.sh"
echo "   • The role ARN is now in scripts/.sagemaker_env — launch the proxy so it"
echo "     propagates SAGEMAKER_EXEC_ROLE_ARN into new VMs (via launch_env_vars)."
echo "   • Wait ~10s for IAM propagation before the first sm_train() call."
if [ -n "$RESOLVED_SERVING_IMAGE" ]; then
  echo "   • Real-time endpoints ENABLED — serving image:"
  echo "       $RESOLVED_SERVING_IMAGE"
  echo "     Relaunch the proxy so it propagates SAGEMAKER_SERVING_IMAGE into new VMs."
else
  echo "   • Real-time endpoints DISABLED (no serving image) — sm_train/predict still"
  echo "     work; sm_deploy_* will raise a clear error until an image is built."
fi
