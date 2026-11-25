#!/usr/bin/env bash
# ============================================================
# Full Teardown — Terminate all MicroVMs and delete all images
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

echo "============================================"
echo "  Lambda MicroVM Sandbox — Full Teardown"
echo "============================================"
echo "   Region:  $AWS_REGION"
echo "   Account: $ACCOUNT_ID"
echo ""

# --- Close the local app FIRST ---------------------------------------------
# Stop everything local before touching AWS, via local_stop_app.sh: it deletes the
# OPEN notebooks (those with a live VM — checkpointed + terminated through the
# proxy, then deleted as the UI's Delete does), stops every local service (proxy,
# AI agent, pool manager, UI, sync worker) and terminates the warm-pool spares.
# Otherwise a still-running pool manager would mint NEW spares from the images
# being deleted below, and the UI/proxy/sync would keep working against VMs that
# no longer exist. Closed notebooks (no live VM) are kept. Best-effort: a failure
# here must never block the AWS cleanup, which is this script's main job.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$ROOT_DIR/local_stop_app.sh" ] && [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  echo ">> Closing the local app (local_stop_app.sh --yes)..."
  ( cd "$ROOT_DIR" && bash ./local_stop_app.sh --yes </dev/null ) \
    || echo "   ⚠ local_stop_app.sh reported a problem — continuing with the AWS teardown"
  echo ""
else
  echo ">> Skipping local_stop_app.sh (not found or no .venv) — continuing with the AWS teardown"
  echo ""
fi

# --- Terminate all running/suspended MicroVMs ---
echo ">> Listing MicroVMs..."
MICROVM_IDS=$(aws_mvm list-microvms --query 'items[?state!=`TERMINATED`].microvmId' --output text 2>/dev/null || echo "")

if [ -z "$MICROVM_IDS" ] || [ "$MICROVM_IDS" = "None" ]; then
  echo "   No active MicroVMs found."
else
  for MICROVM_ID in $MICROVM_IDS; do
    echo "   Terminating $MICROVM_ID..."
    aws_mvm terminate-microvm --microvm-identifier "$MICROVM_ID" 2>/dev/null || true
  done

  echo ">> Waiting for all to reach TERMINATED state..."
  sleep 5
  for MICROVM_ID in $MICROVM_IDS; do
    for i in {1..6}; do
      state=$(aws_mvm get-microvm --microvm-identifier "$MICROVM_ID" \
        --query 'state' --output text 2>/dev/null || echo "TERMINATED")
      [ "$state" = "TERMINATED" ] && break
      sleep 5
    done
    echo "   $MICROVM_ID → TERMINATED"
  done
fi

echo ""

# --- Delete all image tiers (every arch × its per-arch sizes) ---
# Image names are {IMAGE_BASE_NAME}-{arch}-{mem}, so we must iterate arches too;
# using image_arn_for keeps this in lock-step with the build script's naming.
echo ">> Deleting MicroVM images..."
for ARCH in $IMAGE_ARCHES; do
  for MEM in $(image_sizes_for "$ARCH"); do
    TIER_NAME="${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
    TIER_ARN=$(image_arn_for "$ARCH" "$MEM")

    STATE=$(aws_mvm get-microvm-image --image-identifier "$TIER_ARN" \
      --query 'state' --output text 2>/dev/null || echo "NOT_FOUND")

    if [ "$STATE" = "NOT_FOUND" ] || [ "$STATE" = "DELETED" ]; then
      echo "   $TIER_NAME — not found (already deleted)"
      continue
    fi

    echo "   Deleting $TIER_NAME (state: $STATE)..."
    aws_mvm delete-microvm-image --image-identifier "$TIER_ARN" 2>/dev/null || true
  done
done

# Wait for images to finish deleting
echo ">> Waiting for image deletion..."
sleep 5
ALL_DELETED=true
for ARCH in $IMAGE_ARCHES; do
  for MEM in $(image_sizes_for "$ARCH"); do
    TIER_NAME="${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
    TIER_ARN=$(image_arn_for "$ARCH" "$MEM")
    STATE=$(aws_mvm get-microvm-image --image-identifier "$TIER_ARN" \
      --query 'state' --output text 2>/dev/null || echo "DELETED")
    if [ "$STATE" != "DELETED" ] && [ "$STATE" != "NOT_FOUND" ]; then
      echo "   $TIER_NAME still deleting ($STATE)..."
      ALL_DELETED=false
    else
      echo "   $TIER_NAME ✓ deleted"
    fi
  done
done

# --- Delete the seeded sample secrets (microvm/ prefix) ---
# These are created by setup_sample_data.sh with fake values. Force-delete without
# the recovery window so they don't linger and accrue Secrets Manager charges.
echo ""
echo ">> Deleting sample microvm/ secrets..."
for SECRET in "microvm/demo-api-key" "microvm/demo-db-credentials"; do
  if aws secretsmanager describe-secret --secret-id "$SECRET" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws secretsmanager delete-secret --secret-id "$SECRET" --force-delete-without-recovery \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && echo "   ✓ deleted $SECRET" || echo "   ⚠ could not delete $SECRET"
  else
    echo "   $SECRET — not found (already deleted)"
  fi
done

# --- Delete SageMaker real-time endpoints (sm_deploy_serverless/_instance; microvm-ep-*) ---
# Deploy names the Endpoint, EndpointConfig, and Model identically with a
# microvm-ep-<base>-<ts> prefix. Serverless endpoints bill per-invoke (idle = $0);
# INSTANCE endpoints bill per instance-hour while running. Either way the
# Endpoint/EndpointConfig/Model objects linger until deleted, so sweep them. A
# STOPPED instance endpoint (sm_stop_endpoint) has no live Endpoint but still leaves
# its EndpointConfig + Model + S3 artifact — the orphan sweeps below catch exactly
# those, so teardown reclaims stopped endpoints too.
# We do NOT delete the ECR serving repo ($SAGEMAKER_SERVING_REPO): its images are
# content-addressed and tiny, and keeping them makes the next rebuild instant.
echo ""
echo ">> Deleting SageMaker endpoints (microvm-ep-*)..."
SM_ENDPOINTS=$(aws sagemaker list-endpoints \
  --name-contains "microvm-ep-" --max-results 100 \
  --query 'Endpoints[].EndpointName' --output text \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "")

if [ -z "$SM_ENDPOINTS" ] || [ "$SM_ENDPOINTS" = "None" ]; then
  echo "   No microvm-ep-* endpoints found."
else
  for EP in $SM_ENDPOINTS; do
    # Endpoint, its config, and the model all share the same name (see deploy).
    aws sagemaker delete-endpoint --endpoint-name "$EP" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && echo "   ✓ deleted endpoint $EP" || echo "   ⚠ could not delete endpoint $EP"
    aws sagemaker delete-endpoint-config --endpoint-config-name "$EP" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && echo "     ✓ deleted endpoint-config $EP" || true
    aws sagemaker delete-model --model-name "$EP" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && echo "     ✓ deleted model $EP" || true
  done
  # Sweep any orphaned configs/models whose endpoint was already gone (name-scoped).
  for CFG in $(aws sagemaker list-endpoint-configs --name-contains "microvm-ep-" \
        --max-results 100 --query 'EndpointConfigs[].EndpointConfigName' --output text \
        --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo ""); do
    [ "$CFG" = "None" ] && continue
    aws sagemaker delete-endpoint-config --endpoint-config-name "$CFG" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && echo "   ✓ deleted orphan endpoint-config $CFG" || true
  done
  for MDL in $(aws sagemaker list-models --name-contains "microvm-ep-" \
        --max-results 100 --query 'Models[].ModelName' --output text \
        --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo ""); do
    [ "$MDL" = "None" ] && continue
    aws sagemaker delete-model --model-name "$MDL" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1 \
      && echo "   ✓ deleted orphan model $MDL" || true
  done
fi

# --- Sweep S3 ML model artifacts (models/ + the whole sagemaker/ prefix) ---
# This is a SANDBOX/demo stack, so teardown returns a clean slate: it removes the ML
# artifacts that accumulate across runs and show up in the Data Sources panel —
#   • models/     — save_model / save_model_local + upload (local-trained .joblib/.onnx)
#   • sagemaker/  — managed training-job outputs (sagemaker/<job>/) AND endpoint
#                   deploy packages (sagemaker/endpoints/<name>/)
#   • unload/     — unload_athena_to_s3 / SQL-cell UNLOAD output. no-TO runs each
#                   write a FRESH timestamped prefix (unload/<ts>-<id>/), so these
#                   pile up across runs — sweep the whole subtree.
# These are user-owned, DELETABLE prefixes in config.sh
# (DATASOURCE_S3_DELETABLE_PREFIXES), i.e. the panel lets you delete them by hand —
# teardown just does the whole set in one shot. We do NOT touch samples/ (shared demo
# data), user-data/ (your uploads), or knowledge-base/ (the Bedrock KB source PDFs).
echo ""
echo ">> Sweeping S3 ML artifacts (models/, sagemaker/, unload/)..."
if [ -n "${ARTIFACT_BUCKET:-}" ]; then
  # The sagemaker/ prefix comes from config (SAGEMAKER_ARTIFACT_PREFIX); models/ is
  # the ML helpers' save_model destination; unload/ is the Athena UNLOAD output root.
  # Trailing slash scopes each to its subtree.
  for PFX in "models/" "${SAGEMAKER_ARTIFACT_PREFIX:-sagemaker/}" "unload/"; do
    if aws s3 rm "s3://${ARTIFACT_BUCKET}/${PFX}" --recursive \
        --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
      echo "   ✓ removed s3://${ARTIFACT_BUCKET}/${PFX}*"
    else
      echo "   (nothing to remove under ${PFX})"
    fi
  done
else
  echo "   ⚠ ARTIFACT_BUCKET unset — skipped S3 artifact sweep"
fi

# Clean up local state file if it exists
rm -f "$ROOT_DIR/.microvm"

echo ""
echo "============================================"
if [ "$ALL_DELETED" = "true" ]; then
  echo "  ✅ Full teardown complete."
else
  echo "  ⚠️  MicroVMs terminated. Some images still deleting (async)."
fi
echo ""
echo "  Resources cleaned:"
echo "    • All MicroVMs terminated"
echo "    • All image tiers deleted (arches: ${IMAGE_ARCHES})"
echo "    • SageMaker endpoints/configs/models (microvm-ep-*)"
echo "    • S3 ML artifacts (models/, sagemaker/, unload/)"
echo ""
echo "  Resources NOT deleted (manual cleanup if needed):"
echo "    • S3 bucket: $ARTIFACT_BUCKET (samples/, user-data/, knowledge-base/ kept)"
echo "    • IAM roles: $BUILD_ROLE_NAME, $EXEC_ROLE_NAME"
echo "    • ECR serving repo: $SAGEMAKER_SERVING_REPO (kept — makes rebuilds instant)"
echo "    • DynamoDB tables: ecommerce-reviews, ecommerce-recommendations"
echo "    • Athena DB: $ATHENA_DB"
echo "============================================"
