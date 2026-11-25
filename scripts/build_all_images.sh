#!/usr/bin/env bash
# ============================================================
# Build MicroVM images for all (arch × size) tiers
# Creates one image per (architecture, memory size): same code and the same
# AWS-managed base image (al2023-1); the arch is chosen per image with
# `create-microvm-image --cpu-configurations architecture=ARM_64|X86_64`, the
# size with --resources. Image names: {IMAGE_BASE_NAME}-{arch}-{mem}
#   e.g. notebook-microvm-arm64-2048, notebook-microvm-x86_64-4096
#
# Arches built = $IMAGE_ARCHES (space-separated, default "arm64 x86_64"; the
# config.sh region guard drops x86_64 in regions without x86 MicroVMs).
# A single proxy serves every built arch at once; the user picks arch per launch.
#
# Idempotent: skips images that already exist.
# Retries: on CREATE_FAILED, deletes and recreates (up to 2 retries).
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

MAX_BUILD_RETRIES=2
BUILD_POLL_INTERVAL=12
BUILD_TIMEOUT_SECS=600  # 10 minutes max wait per image build

echo "============================================"
echo "  Building MicroVM images"
echo "  Arches: ${IMAGE_ARCHES}"
for _arch in $IMAGE_ARCHES; do
  echo "  Sizes (${_arch}): $(image_sizes_for "$_arch")"
done
if [ -n "${IMAGE_ARCHES_DROPPED:-}" ]; then
  echo "  ⚠ ${IMAGE_ARCHES_DROPPED} is not available for Lambda MicroVMs in ${AWS_REGION} — skipped"
fi
echo "============================================"

cd "$ROOT_DIR"

# Package app once (same code for every arch — Lambda builds each image for the
# arch in its --cpu-configurations; app/requirements.txt has dual-arch wheels).
#
# requirements.txt now lives INSIDE app/ (moved from the repo root), so zipping
# the app/ directory already includes it — the Dockerfile COPYs it from
# app/requirements.txt. Only app/ and the Dockerfile are listed explicitly.
#
# EXCLUDE build/editor junk so the VM image ships ONLY source. Without this the
# zip sweeps in local __pycache__/*.pyc (stale, interpreter-version-specific —
# the VM runs its own Python and regenerates its own bytecode) and macOS
# .DS_Store files. They bloat the image and the Firecracker snapshot for zero
# benefit. -x patterns are matched against the archive paths; the globs cover
# __pycache__ at any depth plus loose compiled/OS cruft.
# Vendor the modelcaps runtime trio into app/ BEFORE zipping — the in-VM image
# ships only app/ + the repo Dockerfile, so app/notebook/helpers/modelcaps/ must be
# on disk (and current) before `zip -r app.zip app/` sweeps it in. The canonical
# source is the repo-root /modelcaps; this keeps the shipped copy in lockstep.
echo ">> Syncing modelcaps into app (vendored runtime copy) before packaging"
PYTHON="${PYTHON:-python3}" bash "$ROOT_DIR/scripts/sync_modelcaps.sh"

echo ">> Packaging app.zip"
rm -f app.zip
zip -r app.zip app/ Dockerfile \
  -x '*/__pycache__/*' '*.pyc' '*.pyo' '*.DS_Store' >/dev/null
echo "   Created app.zip"

# Upload once
echo ">> Uploading to s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"
aws s3 cp app.zip "s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null

# --- Per-arch base image version (queried once per arch, cached) ---
# NOTE: macOS ships bash 3.2, which has no associative arrays (`declare -A`).
# We cache per-arch versions in name-mangled scalars: BASE_VERSION_<arch>
# (arch is a valid identifier fragment, incl. x86_64). Read via indirection.
base_version_var() { echo "BASE_VERSION_$1"; }
for ARCH in $IMAGE_ARCHES; do
  base_arn=$(base_image_arn_for "$ARCH")
  if [ -z "$base_arn" ]; then
    echo "❌ No base image ARN configured for arch '$ARCH' (see base_image_arn_for in config.sh)"
    exit 1
  fi
  api_arch=$(cpu_arch_api_value_for "$ARCH")
  if [ -z "$api_arch" ]; then
    echo "❌ No cpuConfigurations value for arch '$ARCH' (see cpu_arch_api_value_for in config.sh)"
    exit 1
  fi
  # Newest base version that lists this arch in supportedCpuConfigurations
  # (x86_64 needs al2023-1 version 3+). Older CLIs (e.g. 2.35.x) drop that
  # field from parsed output, so the filter yields None: then fall back to the
  # newest version (items[0]), which supports both arches today.
  arch_query=$(printf "items[?contains(supportedCpuConfigurations[].architecture || \`[]\`, '%s')] | [0].imageVersion" "$api_arch")
  ver=$(aws_mvm list-managed-microvm-image-versions \
    --image-identifier "$base_arn" \
    --query "$arch_query" --output text 2>/dev/null || true)
  ver_source="newest version supporting $api_arch"
  if [ -z "$ver" ] || [ "$ver" = "None" ]; then
    ver=$(aws_mvm list-managed-microvm-image-versions \
      --image-identifier "$base_arn" \
      --query 'items[0].imageVersion' --output text 2>/dev/null || echo "0")
    ver_source="latest version (CLI did not report supportedCpuConfigurations)"
  fi
  eval "$(base_version_var "$ARCH")=\"\$ver\""
  echo "   Base image ($ARCH, $api_arch): $base_arn @ version $ver — $ver_source"
done

# --- Helper: wait for an image to reach a terminal state (returns state) ---
wait_for_image() {
  local tier_arn="$1"
  local elapsed=0
  local state="CREATING"
  while [ "$state" = "CREATING" ]; do
    sleep "$BUILD_POLL_INTERVAL"
    elapsed=$((elapsed + BUILD_POLL_INTERVAL))
    state=$(aws_mvm get-microvm-image --image-identifier "$tier_arn" \
      --query 'state' --output text 2>/dev/null || echo "NOT_FOUND")
    if [ $elapsed -ge $BUILD_TIMEOUT_SECS ]; then
      echo "TIMED_OUT"; return
    fi
  done
  echo "$state"
}

# --- Helper: delete an image and wait for deletion ---
delete_and_wait() {
  local tier_arn="$1"
  aws_mvm delete-microvm-image --image-identifier "$tier_arn" >/dev/null 2>&1 || true
  local elapsed=0
  while [ $elapsed -lt 120 ]; do
    sleep 5
    elapsed=$((elapsed + 5))
    local state
    state=$(aws_mvm get-microvm-image --image-identifier "$tier_arn" \
      --query 'state' --output text 2>/dev/null || echo "DELETED")
    if [ "$state" = "DELETED" ] || [ "$state" = "NOT_FOUND" ]; then
      return 0
    fi
  done
  echo "   ⚠ Timed out waiting for deletion of $tier_arn"
  return 1
}

# --- Helper: create a single (arch, mem) image ---
create_image() {
  local arch="$1"
  local mem="$2"
  local tier_name="${IMAGE_BASE_NAME}-${arch}-${mem}"
  local base_version_ref
  base_version_ref=$(base_version_var "$arch")
  local base_version="${!base_version_ref}"
  local api_arch
  api_arch=$(cpu_arch_api_value_for "$arch")
  if [ -z "$api_arch" ]; then
    echo "   ❌ No cpuConfigurations value for arch '$arch'" >&2
    return 1
  fi
  aws_mvm create-microvm-image \
    --name "$tier_name" \
    --base-image-arn "$(base_image_arn_for "$arch")" \
    --base-image-version "$base_version" \
    --cpu-configurations "architecture=${api_arch}" \
    --build-role-arn "$BUILD_ROLE_ARN" \
    --code-artifact "uri=s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY" \
    --hooks '{"port":8080,"microvmHooks":{"run":"ENABLED","runTimeoutInSeconds":60,"resume":"ENABLED","resumeTimeoutInSeconds":10,"suspend":"ENABLED","suspendTimeoutInSeconds":60,"terminate":"ENABLED","terminateTimeoutInSeconds":60},"microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":60}}' \
    --resources "[{\"minimumMemoryInMiB\":${mem}}]" >/dev/null
}

# NEEDS_WAIT holds "arch:mem" tokens for images we kicked off and must poll.
NEEDS_WAIT=()

# Build each (arch, size) tier — kick off all creates in parallel
for ARCH in $IMAGE_ARCHES; do
  for MEM in $(image_sizes_for "$ARCH"); do
    VCPU=$(echo "$MEM" | awk '{printf "%.2g", $1 / 2048}')
    TIER_NAME="${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
    TIER_ARN=$(image_arn_for "$ARCH" "$MEM")

    TIER_STATE=$(aws_mvm get-microvm-image --image-identifier "$TIER_ARN" \
      --query 'state' --output text 2>/dev/null || echo "NOT_FOUND")

    if [ "$TIER_STATE" = "CREATED" ]; then
      echo "   ✓ ${TIER_NAME} (${MEM}MB / ${VCPU}vCPU) — already exists"
      continue
    fi

    if [ "$TIER_STATE" = "CREATE_FAILED" ]; then
      echo "   🔄 ${TIER_NAME} — cleaning up failed image..."
      delete_and_wait "$TIER_ARN"
      TIER_STATE="NOT_FOUND"
    fi

    if [ "$TIER_STATE" = "CREATING" ]; then
      echo "   ⏳ ${TIER_NAME} — already building"
      NEEDS_WAIT+=("${ARCH}:${MEM}")
      continue
    fi

    echo ">> Creating image: ${TIER_NAME} (${MEM}MB / ${VCPU}vCPU)"
    create_attempts=0
    create_success=false
    while [ $create_attempts -lt 3 ]; do
      if create_image "$ARCH" "$MEM" 2>/dev/null; then
        create_success=true
        NEEDS_WAIT+=("${ARCH}:${MEM}")
        break
      fi
      create_attempts=$((create_attempts + 1))
      echo "   ⚠ create call failed (attempt $create_attempts/3), retrying in 15s..."
      sleep 15
    done
    if [ "$create_success" = "false" ]; then
      echo "   ❌ Failed to create ${TIER_NAME} after 3 attempts"
      exit 1
    fi
  done
done

# --- Wait for all images to reach CREATED state (parallel poll) ---
if [ ${#NEEDS_WAIT[@]} -gt 0 ]; then
  echo ""
  echo ">> Waiting for ${#NEEDS_WAIT[@]} image(s) to build..."
  elapsed=0
  while [ $elapsed -lt $BUILD_TIMEOUT_SECS ]; do
    sleep "$BUILD_POLL_INTERVAL"
    elapsed=$((elapsed + BUILD_POLL_INTERVAL))

    all_done=true
    status_line=""
    for TOKEN in "${NEEDS_WAIT[@]}"; do
      ARCH="${TOKEN%%:*}"; MEM="${TOKEN##*:}"
      TIER_NAME="${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
      TIER_ARN=$(image_arn_for "$ARCH" "$MEM")
      STATE=$(aws_mvm get-microvm-image --image-identifier "$TIER_ARN" \
        --query 'state' --output text 2>/dev/null || echo "NOT_FOUND")

      if [ "$STATE" = "CREATE_FAILED" ]; then
        echo "   ⚠ ${TIER_NAME} failed — retrying..."
        delete_and_wait "$TIER_ARN"
        create_image "$ARCH" "$MEM" 2>/dev/null || true
        all_done=false
      elif [ "$STATE" != "CREATED" ]; then
        all_done=false
      fi
      status_line="${status_line} ${ARCH}-${MEM}=${STATE}"
    done

    if [ "$all_done" = "true" ]; then
      echo "   All images ready ✓"
      break
    fi
    echo "   $(date +%H:%M:%S)${status_line}"
  done

  # Final check — re-verify each tier reached CREATED. A single get-microvm-image
  # right after creation can transiently return NOT_FOUND (eventual consistency),
  # so retry a few times before declaring failure. Recover genuine CREATE_FAILED.
  for TOKEN in "${NEEDS_WAIT[@]}"; do
    ARCH="${TOKEN%%:*}"; MEM="${TOKEN##*:}"
    TIER_NAME="${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
    TIER_ARN=$(image_arn_for "$ARCH" "$MEM")
    STATE=""
    verify_attempt=0
    while [ $verify_attempt -lt 6 ]; do
      STATE=$(aws_mvm get-microvm-image --image-identifier "$TIER_ARN" \
        --query 'state' --output text 2>/dev/null || echo "QUERY_ERROR")
      if [ "$STATE" = "CREATED" ]; then
        break
      fi
      if [ "$STATE" = "CREATE_FAILED" ]; then
        echo "   🔄 ${TIER_NAME} CREATE_FAILED at final check — rebuilding..."
        delete_and_wait "$TIER_ARN"
        create_image "$ARCH" "$MEM" 2>/dev/null || true
        STATE=$(wait_for_image "$TIER_ARN")
        [ "$STATE" = "CREATED" ] && break
      fi
      verify_attempt=$((verify_attempt + 1))
      echo "   ⏳ ${TIER_NAME} final-check state=${STATE} (attempt ${verify_attempt}/6) — retrying in 5s..."
      sleep 5
    done
    if [ "$STATE" != "CREATED" ]; then
      echo "   ❌ ${TIER_NAME} not ready after retries (state: $STATE)"
      exit 1
    fi
    echo "   ✓ ${TIER_NAME} verified CREATED"
  done
fi

rm -f app.zip

echo ""
echo "✅ All images ready:"
for ARCH in $IMAGE_ARCHES; do
  for MEM in $(image_sizes_for "$ARCH"); do
    VCPU=$(echo "$MEM" | awk '{printf "%.2g", $1 / 2048}')
    GB=$(echo "$MEM" | awk '{printf "%.1f", $1 / 1024}')
    echo "   ${IMAGE_BASE_NAME}-${ARCH}-${MEM}: ${GB} GB / ${VCPU} vCPU"
  done
done
