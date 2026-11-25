#!/usr/bin/env bash
# ============================================================
# CONFIGURATION — Edit these values for your AWS account
# ============================================================

export AWS_REGION="us-west-2"
export AWS_CLI_PROFILE="default"  # change if using a named profile

# Auto-detect account ID
export ACCOUNT_ID=$(aws sts get-caller-identity --profile "$AWS_CLI_PROFILE" --query Account --output text 2>/dev/null)

# S3 bucket for MicroVM image artifacts
export ARTIFACT_BUCKET="microvm-sandbox-artifacts-${ACCOUNT_ID}-${AWS_REGION}"

# ------------------------------------------------------------
# CPU architecture(s). Lambda MicroVMs run on Graviton (arm64, the AWS default)
# and x86_64 (GA: https://aws.amazon.com/about-aws/whats-new/2026/11/aws-lambda-microvms-x86-architecture/).
# Arch is a PER-VM choice — the proxy supports every arch listed here at once,
# the user picks one per launch, and each VM is priced/restored by ITS arch.
#
# IMAGE_ARCHES is a space-separated list of arches to BUILD and OFFER. Default
# "arm64 x86_64" → both are built, and the UI shows an arch picker with
# DEFAULT_IMAGE_ARCH pre-selected. Opt out of x86 with:
#   IMAGE_ARCHES=arm64 ./local_install.sh
# ------------------------------------------------------------
export IMAGE_ARCHES="${IMAGE_ARCHES:-arm64 x86_64}"
# Default arch pre-selected in the UI (must be one of IMAGE_ARCHES). arm64 = cheaper.
export DEFAULT_IMAGE_ARCH="${DEFAULT_IMAGE_ARCH:-arm64}"

# Regions where Lambda MicroVMs exist but x86_64 is NOT offered (What's New post
# above; codes verified via SSM /aws/service/global-infrastructure/regions):
#   ap-east-1      Asia Pacific (Hong Kong)
#   ap-southeast-5 Asia Pacific (Malaysia)
#   ap-southeast-7 Asia Pacific (Thailand)
#   eu-south-2     Europe (Spain)
export X86_UNSUPPORTED_REGIONS="ap-east-1 ap-southeast-5 ap-southeast-7 eu-south-2"

# x86_supported_in_region <region> → exit 0 when x86_64 MicroVMs exist there.
x86_supported_in_region() {
  local r
  for r in $X86_UNSUPPORTED_REGIONS; do
    [ "$r" = "${1:-}" ] && return 1
  done
  return 0
}

# Region guard (the ONE place x86 is dropped). In an excluded region x86_64 can't
# be built at all, so it's removed from IMAGE_ARCHES whether it came from the
# default or the caller. The proxy reads IMAGE_ARCHES, so it then offers arm64
# only, and so does the UI. Sets IMAGE_ARCHES_DROPPED="x86_64" when it dropped
# something (local_install.sh / build_all_images.sh print the warning). Silent.
apply_arch_region_guard() {
  IMAGE_ARCHES_DROPPED=""
  if ! x86_supported_in_region "${AWS_REGION:-}"; then
    local a kept=""
    for a in ${IMAGE_ARCHES:-}; do
      if [ "$a" = "x86_64" ]; then
        IMAGE_ARCHES_DROPPED="x86_64"
      else
        kept="${kept:+$kept }$a"
      fi
    done
    IMAGE_ARCHES="${kept:-arm64}"
    [ "${DEFAULT_IMAGE_ARCH:-}" = "x86_64" ] && DEFAULT_IMAGE_ARCH="arm64"
  fi
  export IMAGE_ARCHES DEFAULT_IMAGE_ARCH IMAGE_ARCHES_DROPPED
}
apply_arch_region_guard

# AWS-managed base image ARN per arch. There is ONE managed base image (al2023-1)
# for both arches: `aws lambda-microvms list-managed-microvm-images` lists only
# al2023-1, and its version 3+ supports ARM_64 and X86_64. The arch is chosen per
# image with `create-microvm-image --cpu-configurations architecture=...` (see
# cpu_arch_api_value_for), per https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html
base_image_arn_for() {
  case "$1" in
    arm64|x86_64) echo "arn:aws:lambda:${AWS_REGION}:aws:microvm-image:al2023-1" ;;
    *) echo "" ;;
  esac
}

# cpu_arch_api_value_for <arch> → the create-microvm-image cpuConfigurations
# architecture value (ARM_64 | X86_64), empty for an unknown arch.
cpu_arch_api_value_for() {
  case "$1" in
    arm64)  echo "ARM_64" ;;
    x86_64) echo "X86_64" ;;
    *) echo "" ;;
  esac
}

# Image naming. Images are arch-suffixed so arm64 and x86_64 coexist:
#   notebook-microvm-arm64-2048, notebook-microvm-x86_64-2048, ...
# IMAGE_BASE_NAME is the shared prefix; the proxy appends -{arch}-{mem} at launch.
export IMAGE_BASE_NAME="notebook-microvm"
# IMAGE_ARN = arch-agnostic base ARN template the proxy uses (it appends
# -{arch}-{mem}). Kept as MICROVM_IMAGE_ARN for backward compat.
export IMAGE_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:microvm-image:${IMAGE_BASE_NAME}"
# image_arn_for <arch> <mem> → full per-tier ARN
image_arn_for() {
  echo "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:microvm-image:${IMAGE_BASE_NAME}-${1}-${2}"
}

# Size tiers (image name suffix → memory in MiB), per architecture.
# Note: 512 excluded — not enough memory for pandas+numpy+matplotlib snapshot.
# ARM and x86_64 do NOT have to offer the same tiers — keep them separate.
# Today both use the service's one sizing table (0.5-8 GB baseline at 2 GB : 1
# vCPU, no per-arch limits): "MicroVM sizing" in
# https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html
export IMAGE_SIZES_ARM64="1024 2048 4096 8192"
export IMAGE_SIZES_X86_64="1024 2048 4096 8192"

# image_sizes_for <arch> → echoes the space-separated size list for that arch.
# Falls back to the arm64 list for any unknown arch so callers always get a sane default.
image_sizes_for() {
  local arch="${1:-$DEFAULT_IMAGE_ARCH}"
  case "$arch" in
    arm64)  echo "$IMAGE_SIZES_ARM64" ;;
    x86_64) echo "$IMAGE_SIZES_X86_64" ;;
    *)      echo "$IMAGE_SIZES_ARM64" ;;
  esac
}

# Per-arch size table for the proxy (mirrors MICROVM_PRICING_JSON pattern).
# The launch route validates requested memoryMiB against the SELECTED arch's list.
export MICROVM_SIZES_JSON="{\"arm64\":[$(echo "$IMAGE_SIZES_ARM64" | tr ' ' ',')],\"x86_64\":[$(echo "$IMAGE_SIZES_X86_64" | tr ' ' ',')]}"

# Default memory tier (MiB) for a launch that names no size, and the fallback the
# proxy, pool manager and in-VM cost helpers use when a record has no memory size.
# Must be one of the size tiers above for every arch in IMAGE_ARCHES.
# Override: DEFAULT_MEMORY_MIB=4096 ./local_install.sh
export DEFAULT_MEMORY_MIB="${DEFAULT_MEMORY_MIB:-2048}"
for _arch in $IMAGE_ARCHES; do
  case " $(image_sizes_for "$_arch") " in
    *" $DEFAULT_MEMORY_MIB "*) ;;
    *)
      echo "❌ DEFAULT_MEMORY_MIB=$DEFAULT_MEMORY_MIB is not one of the $_arch size tiers ($(image_sizes_for "$_arch"))." >&2
      unset _arch
      return 1 2>/dev/null || exit 1
      ;;
  esac
done
unset _arch

# IAM role names
export BUILD_ROLE_NAME="MicroVMSandboxBuildRole"
export EXEC_ROLE_NAME="MicroVMSandboxExecRole"
export BUILD_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${BUILD_ROLE_NAME}"
export EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${EXEC_ROLE_NAME}"

# Network connectors: the proxy and pool_manager read these from the
# MICROVM_INGRESS_CONNECTOR / MICROVM_SHELL_INGRESS_CONNECTOR /
# MICROVM_EGRESS_CONNECTOR env vars and fall back to AWS-managed defaults
# (HTTP_INGRESS / SHELL_INGRESS / INTERNET_EGRESS) in code when those are unset.
# Neither launch path consumes a bare INGRESS_CONNECTOR/EGRESS_CONNECTOR shell
# var, so none is exported here; override via the MICROVM_* names if ever needed.

# Artifact key in S3 (shared app.zip for every arch/size — code is identical)
export ARTIFACT_KEY="images/${IMAGE_BASE_NAME}.zip"

# Polling interval for MicroVM state refresh (milliseconds)
export POLL_INTERVAL_MS="10000"

# ------------------------------------------------------------
# MicroVM runtime knobs — injected into EVERY VM's os.environ at launch
# (proxy reads these → runHookPayload.env_vars → /run hook → os.environ).
# Single source of truth: vm_manager.launch_env_vars(). The in-VM code reads
# each at CALL time (not import), so changing a value here takes effect on the
# next VM launch — no image rebuild needed (rebuild only needed the first time
# the in-VM read site is added).
# ------------------------------------------------------------
# Max DataFrame/SQL result rows the VM serializes to the client (as a JSON table
# payload). The client renders these in a paginated, sortable, searchable grid;
# beyond this cap the UI shows "first N of M rows — refine your query".
export NOTEBOOK_MAX_DISPLAY_ROWS="1000"
# Max seconds a single cell may run before it's forcefully interrupted.
export EXECUTION_TIMEOUT_SECONDS="60"

# ------------------------------------------------------------
# VM Pool Manager (warm pool of pre-booted, suspended spares)
# Keeps a small pool of pre-booted MicroVMs ready to be granted instantly
# instead of a ~11-12s cold launch. OFF by default — when POOL_ENABLED=false,
# nothing about today's launch/recovery behavior changes.
# ------------------------------------------------------------
export POOL_ENABLED="${POOL_ENABLED:-false}"          # master on/off (off = pure cold-launch, today's behavior)
export POOL_SIZE_PER_IMAGE="${POOL_SIZE_PER_IMAGE:-2}" # default spares per (arch,size)
# Optional per-size overrides (JSON, size→count); unset sizes use POOL_SIZE_PER_IMAGE.
# NOTE: the default JSON is assigned on its own line, NOT inlined into a
# ${VAR:-default} expansion. Inlining it as "${VAR:-{...}}" breaks because bash
# matches the JSON's own closing brace as the expansion terminator and appends a
# stray literal "}" to the value, producing invalid JSON (which the pool manager
# then silently ignores, falling back to POOL_SIZE_PER_IMAGE for every tier).
_POOL_SIZE_OVERRIDES_ARM64_DEFAULT='{"1024":1,"2048":2,"4096":2,"8192":1}'
export POOL_SIZE_OVERRIDES_ARM64="${POOL_SIZE_OVERRIDES_ARM64:-$_POOL_SIZE_OVERRIDES_ARM64_DEFAULT}"
# x86_64 per-size overrides (same format, same own-line rule). Default: 1 spare
# for the most common tier (2048 MiB, the UI's default size and the AWS default
# 2 GB / 1 vCPU), 0 for every other x86 tier (target 0 = no spares).
_POOL_SIZE_OVERRIDES_X86_64_DEFAULT='{"1024":0,"2048":1,"4096":0,"8192":0}'
export POOL_SIZE_OVERRIDES_X86_64="${POOL_SIZE_OVERRIDES_X86_64:-$_POOL_SIZE_OVERRIDES_X86_64_DEFAULT}"
# Recycle spares older than this (hours). The pool manager clamps any value > 2 to 2.
export POOL_MAX_AGE_HOURS="${POOL_MAX_AGE_HOURS:-2}"
export POOL_MANAGER_PORT="${POOL_MANAGER_PORT:-8090}"
export POOL_MANAGER_URL="${POOL_MANAGER_URL:-http://localhost:${POOL_MANAGER_PORT}}"  # how the proxy reaches the pool manager
export POOL_FILL_CONCURRENCY="${POOL_FILL_CONCURRENCY:-4}"  # bounded parallel launches at fill time
# Pool backend. Set UNCONDITIONALLY to "aws" (not "${VAR:-aws}") so a stale
# POOL_VM_BACKEND left in the environment can never silently downgrade a real
# launch to the in-memory fake backend. Tests do NOT source config.sh — they
# build PoolConfig directly (tests/pool/_poolkit.build_config) — so forcing
# "aws" here affects only real local_install.sh launches. To run the fake
# backend manually, edit this line or export POOL_VM_BACKEND after sourcing.
export POOL_VM_BACKEND="aws"                           # "aws" (real MicroVMs) — forced; "fake" is for tests only
export POOL_FAKE_LAUNCH_DELAY_SECONDS="${POOL_FAKE_LAUNCH_DELAY_SECONDS:-10}"  # fake backend only; simulates ~10s AWS provisioning. Ignored by the real AWS backend (phase 6).
# How long a mint waits for a fresh spare's /health to go 200 before discarding it.
# A cold MicroVM can take >30s to become healthy; too tight a budget abandons a VM
# that was about to come up, leaving a tier short (available=target-1) until the next
# fill tick retries — the "pool sits at N-1 for a while" symptom. 90s covers slow cold
# boots; a VM that truly never comes up is still cleaned up, just later.
export POOL_MINT_HEALTH_TIMEOUT_SECONDS="${POOL_MINT_HEALTH_TIMEOUT_SECONDS:-90}"

# Cloud data-source catalog cache (proxy). discover_full_catalog() fans out to
# S3/DynamoDB/Athena/Aurora (~7s) on every VM launch/restore/recovery, but the
# result is the same shared catalog, so the proxy caches it for this many seconds.
# An explicit Data Sources panel refresh always bypasses the cache. 0 disables
# caching (rediscover every time). Read by proxy/platform/datasources/registry.py.
export CATALOG_CACHE_TTL_SECONDS="${CATALOG_CACHE_TTL_SECONDS:-300}"   # 5 minutes

# Frontend VM-state poll cadence (ms). Served to the UI via GET /health's
# poll_interval_ms, so the browser polls /instances this often to refresh VM
# state (dots, auto-connect, termination→Restore). Each poll can fan out to a
# paginated ListMicrovms on the control plane, so a longer cadence trades a
# little UI latency for less throttling pressure. 15s is the default; drop to
# 10s for snappier dots, raise to 20s+ under heavy ListMicrovms throttling.
# Read by proxy/platform/microvm_manager.py (POLL_INTERVAL_MS).
# Set UNCONDITIONALLY (not "${VAR:-15000}") so a stale POLL_INTERVAL_MS left in
# the environment from an earlier run can't silently downgrade the cadence — the
# same guard config.sh uses for POOL_VM_BACKEND. To change it, edit this line.
export POLL_INTERVAL_MS="15000"   # 15 seconds

# Default thread-pool size for the proxy's asyncio.to_thread offloads (DB reads,
# control-plane SDK calls, STS/S3 staging). Empty = let the proxy compute an
# ECS-aware default at startup: max(16, cpu_count*4). Python's own default is
# min(32, cpu+4), which on a small ECS task (2–4 vCPU) is only ~6–8 workers and
# would serialize the off-loop I/O the latency fixes depend on. Set a positive
# integer here to override (e.g. for a very small or very large task).
# Read by proxy/server.py (lifespan startup).
export PROXY_THREAD_POOL_SIZE="${PROXY_THREAD_POOL_SIZE:-}"

# Launch-path latency instrumentation. Empty/unset = OFF (timing lines stay at
# DEBUG and are suppressed by the proxy's WARNING default log level). Set to a
# truthy value (1/true/yes/on) to raise the per-call timings (auth-token mint,
# wait-for-RUNNING poll, STS/S3 staging, session-registry read/write) to INFO so
# the warm-pool grant latency improvement can be confirmed live without a
# redeploy. Read by proxy/platform/microvm_manager.py and proxy/platform/routes/microvm.py.
export PROXY_LAUNCH_TIMING="${PROXY_LAUNCH_TIMING:-}"

# Per-cell execute-forward (/proxy/{path}) httpx timeouts. CONNECT is kept
# short so an unreachable-but-RUNNING VM fails fast (retryable) instead of a
# ~31s TCP stall; READ stays long so a legitimately long-running cell isn't
# cut off. Read fresh per request by proxy/platform/routes/microvm.py.
export PROXY_FORWARD_CONNECT_TIMEOUT="${PROXY_FORWARD_CONNECT_TIMEOUT:-5.0}"
export PROXY_FORWARD_READ_TIMEOUT="${PROXY_FORWARD_READ_TIMEOUT:-120.0}"

# Storage backend configuration
# Supported: "sqlite" (default) and "postgres" (shared Aurora/PostgreSQL)
export STORAGE_BACKEND="sqlite"
# SQLite: path is relative to proxy/data/ by default (no connection string needed)
# Postgres: export STORAGE_CONNECTION="postgresql://user:pass@host:5432/microvm_db"
export STORAGE_CONNECTION=""

# ------------------------------------------------------------
# Aurora PostgreSQL provisioning (scripts/setup_aurora_postgres.sh)
# Used ONLY by the provisioning script to create a managed Postgres cluster. These
# knobs do NOT affect a running proxy.
#
# The SAME cluster is reached two different ways by the two tiers, each with its
# OWN least-privilege database user and its OWN Secrets Manager secret:
#   • PROXY  → Aurora over a normal 5432 socket (psycopg2), via STORAGE_CONNECTION
#     above, as a READ-WRITE user (it creates its schema + reads/writes state). The
#     host/ECS the proxy runs on must have network reachability to the endpoint.
#   • MicroVMs → Aurora over the RDS Data API (HTTPS/443), via the AURORA_DATA_API_*
#     knobs below, as a READ-ONLY user (notebooks only SELECT the curated v_nb_*
#     views — never the base tables). This is the ONLY
#     path that works from a VM (managed egress allows only 80/443/53 — confirmed
#     empirically), so the DB can be a notebook data source without VPC/socket access.
#
# Three users / three JSON secrets ({"username","password"}), all under microvm/*:
#   master     — created by RDS; used by the script ONLY to bootstrap the two roles.
#   read-write — proxy (psycopg2). Owns + creates the schema.
#   read-only  — MicroVM Data API. SELECT on ONLY the curated v_nb_* views.
# The script (a) enables the cluster's Data API, (b) creates the RW + RO roles and
# their secrets, (c) runs schema creation AS the RW user so it owns the tables and
# the RO default-privileges apply to everything the proxy creates now and later.
#
# For a quick start, provision as publicly accessible; for production, run the proxy
# inside the VPC and set AURORA_PUBLICLY_ACCESSIBLE=false.
# ------------------------------------------------------------
export AURORA_CLUSTER_ID="${AURORA_CLUSTER_ID:-microvm-proxy-db}"
# Database created inside the cluster (matches the default in the Postgres DSN examples).
export AURORA_DB_NAME="${AURORA_DB_NAME:-microvm_db}"
export AURORA_MASTER_USERNAME="${AURORA_MASTER_USERNAME:-microvm_admin}"
# Aurora PostgreSQL engine version. Leave as the default unless you need a specific
# minor; the script validates availability against the region.
export AURORA_ENGINE_VERSION="${AURORA_ENGINE_VERSION:-16.8}"
# Writer instance class. db.serverless => Aurora Serverless v2 (autoscaling; the
# script sets a capacity range below). Any provisioned class (e.g. db.r6g.large)
# is used as-is instead.
export AURORA_INSTANCE_CLASS="${AURORA_INSTANCE_CLASS:-db.serverless}"
# Serverless v2 capacity range (Aurora Capacity Units) — only used when
# AURORA_INSTANCE_CLASS=db.serverless. One ACU ≈ 2 GiB RAM + proportional CPU/net.
#
# MIN=0 enables SCALE-TO-ZERO / auto-pause: after a period with NO user
# connections the cluster pauses and you pay $0 for compute (storage still bills);
# the first new connection auto-resumes it. Ideal for a lightly-used test/dev DB.
# Trade-off: the first query after an idle pause pays a short resume delay (a few
# seconds) — fine here since both tiers tolerate it (the proxy retries, and the
# notebook Data API call simply waits for resume, like a cold VM). Requires a
# recent Aurora PostgreSQL engine version that supports min 0 ACU; if the account/
# region/engine doesn't, set MIN back to 0.5 (idles cheap but never fully pauses).
export AURORA_MIN_ACU="${AURORA_MIN_ACU:-0}"
export AURORA_MAX_ACU="${AURORA_MAX_ACU:-2}"
# --- Three credentials, each an RDS-shaped JSON secret {"username","password"} ---
# All live under microvm/* so the VM exec role (scoped to that prefix) can read the
# RO one. The script creates each with a generated password if absent and reads it
# back on reruns (idempotent; never prints passwords).
#
# MASTER — created by RDS at cluster creation; used by the script ONLY to bootstrap
# the RW/RO roles. Not used at runtime by either tier.
export AURORA_MASTER_USERNAME="${AURORA_MASTER_USERNAME:-microvm_admin}"
export AURORA_SECRET_NAME="${AURORA_SECRET_NAME:-microvm/proxy-db/master}"
# READ-WRITE — used by the PROXY (psycopg2). Owns + creates the schema; full DML/DDL
# on microvm_db public. This secret's password feeds STORAGE_CONNECTION.
export AURORA_RW_USERNAME="${AURORA_RW_USERNAME:-microvm_proxy_rw}"
export AURORA_RW_SECRET_NAME="${AURORA_RW_SECRET_NAME:-microvm/proxy-db/readwrite}"
# READ-ONLY — used by MicroVMs via the Data API. SELECT-only on the curated
# v_nb_* VIEWS (NOT the base tables). This secret's ARN becomes
# AURORA_DATA_API_SECRET_ARN below.
# NOTE: this RO role is granted SELECT on ONLY the curated v_nb_* views (not the
# base tables) by setup_aurora_postgres.sh — least-privilege for the notebook.
export AURORA_RO_USERNAME="${AURORA_RO_USERNAME:-microvm_notebook_ro}"
export AURORA_RO_SECRET_NAME="${AURORA_RO_SECRET_NAME:-microvm/proxy-db/readonly}"
# NOTE: Aurora's operational tables are populated from REAL local activity by the
# standalone SQLite→Aurora sync worker (sync/sync_to_aurora.py, started by
# local_install.sh). There is no synthetic sample-data seeder.
# Quick-start convenience: make the cluster publicly accessible so the script (and
# a locally-run proxy) can reach 5432 without VPC plumbing. Set to "false" for a
# VPC-internal cluster (then run the provisioning + proxy from inside the VPC).
export AURORA_PUBLICLY_ACCESSIBLE="${AURORA_PUBLICLY_ACCESSIBLE:-true}"

# ------------------------------------------------------------
# Aurora Data API access for MicroVMs (notebook data source).
# These are what the VM-side Aurora datasource/SQL engine uses (rds-data
# ExecuteStatement with resourceArn + secretArn over 443), propagated into each VM
# at launch via vm_manager.launch_env_vars().
#   AURORA_DATA_API_CLUSTER_ARN — the cluster's ARN (resourceArn for rds-data).
#   AURORA_DATA_API_SECRET_ARN  — ARN of the READ-ONLY secret (AURORA_RO_SECRET_NAME),
#                                 so notebooks authenticate as the SELECT-only user.
#   AURORA_DATA_API_DATABASE    — default database name the Data API targets.
#
# DO NOT hardcode ARNs here. They are DISCOVERED at launch: when you pass
# --AuroraSetup, local_install.sh resolves the cluster + RO/RW secret ARNs from
# your OWN account (control-plane describe-*, or a fresh provision) and writes them
# to scripts/.aurora_env, which it sources AFTER this file — so the resolved values
# win. Left blank here, a blank cluster ARN means "no Aurora data source", which is
# exactly the correct default for a no-flag (fully local) run. You can still
# override via the environment if you really want to pin specific ARNs.
export AURORA_DATA_API_CLUSTER_ARN="${AURORA_DATA_API_CLUSTER_ARN:-}"
export AURORA_DATA_API_SECRET_ARN="${AURORA_DATA_API_SECRET_ARN:-}"
export AURORA_DATA_API_DATABASE="${AURORA_DATA_API_DATABASE:-${AURORA_DB_NAME}}"

# ------------------------------------------------------------
# LOCAL-DEV-ONLY: SQLite -> Aurora sync worker (sync/sync_to_aurora.py)
# ------------------------------------------------------------
# In the local topology the proxy writes to SQLite while Aurora is a read replica
# the notebooks query over the Data API. A standalone worker (started by
# local_install.sh, NOT part of the proxy) bridges the two: it replays local
# writes/deletes into Aurora over the RDS Data API. This is THROWAWAY — once the
# proxy runs against Aurora directly (STORAGE_BACKEND=postgres) the worker is
# unnecessary and the sync/ package can be deleted.
#
# The worker WRITES base tables, so it authenticates with the READ-WRITE Data API
# secret (microvm/proxy-db/readwrite = AURORA_RW_SECRET_NAME) — NOT the read-only
# secret above. It runs LOCALLY under your own AWS creds (an admin/dev principal),
# so no extra IAM grant is needed beyond rds-data:* on the cluster +
# secretsmanager:GetSecretValue on the RW secret (both covered by admin). NOTE:
# the VM exec role (iam/exec-role-permissions.json) already carries the rds-data
# write actions (Batch/Begin/Commit/RollbackTransaction) for a future in-cloud path.
#
# DO NOT hardcode this ARN. Like the Data API ARNs above, it is DISCOVERED at
# launch and written to scripts/.aurora_env by local_install.sh --AuroraSetup
# (resolved from your own account's RW secret). Left blank here so a no-flag run
# has no RW secret → local_install.sh skips launching the sync worker, which is the
# correct fully-local default. Overridable via the environment if needed.
export AURORA_DATA_API_RW_SECRET_ARN="${AURORA_DATA_API_RW_SECRET_ARN:-}"
# Seconds between sync cycles (worker default is 60 if unset).
export AURORA_SYNC_INTERVAL="${AURORA_SYNC_INTERVAL:-60}"
# Max rows a notebook Aurora query returns over the Data API. The RDS Data API
# caps EACH response at ~1MB, so an unbounded SELECT truncates silently. The VM
# auto-injects "LIMIT ${AURORA_MAX_ROWS}" when a query has no LIMIT of its own,
# giving a complete, non-truncated result up to this bound. It ALSO detects when a
# response still hit the 1MB ceiling (wide rows can truncate BELOW this count) and
# warns in the cell output. Raise it for narrow tables (e.g. 100000); lower it for
# very wide rows. For genuinely large extracts, use the proxy's psycopg2 path
# (no 1MB cap) rather than the VM Data API path.
export AURORA_MAX_ROWS="${AURORA_MAX_ROWS:-25000}"

# Auth token cache backend (proxy/platform/token_cache.py)
# Supported: "memory" (default, in-process L1), "redis" (future ElastiCache L2)
export TOKEN_CACHE_BACKEND="memory"
# L1 (in-process) size bound ONLY — caps the per-instance cache, NOT the fleet.
# Each proxy instance holds hot tokens only for the VMs it is actively routing to,
# so size this to PEAK CONCURRENT SESSIONS PER PROXY INSTANCE (not total notebooks).
# Eviction just re-mints a token on the next request, so overshoot is cheap.
# The future "redis" L2 is fleet-wide and unbounded by count (evicts via per-key
# TTL), so this value does NOT limit it.
export TOKEN_CACHE_MAX_SIZE="${TOKEN_CACHE_MAX_SIZE:-1000}"
# Redis/Valkey (ElastiCache) connection URL — used only when TOKEN_CACHE_BACKEND=redis.
# Defined (empty) by default so the proxy launch block references a set variable
# under `set -u`. Set it to a redis:// URL when TOKEN_CACHE_BACKEND=redis, e.g.:
#   export TOKEN_CACHE_URL="redis://microvm-tokens.abc123.use1.cache.amazonaws.com:6379"
export TOKEN_CACHE_URL="${TOKEN_CACHE_URL:-}"

# Pricing (Lambda MicroVM on-demand rates, us-west-2 = us-east-1). AWS bills on
# TWO axes: vCPU + Memory (separately); CPU allocated at 2 GB : 1 vCPU ratio.
# vCPU and memory rates differ by CPU arch; snapshot read/write/storage rates
# do not. A single proxy prices BOTH arches, so pricing is a per-arch table:
# each VM (and its burst) is priced by ITS arch.
# Source: https://aws.amazon.com/lambda/pricing/ (MicroVMs tab) and the AWS Price
# List API (`aws pricing get-products --service-code AWSLambda`, effective
# 2026-10-01), usage types USW2-Lambda-MicroVM-vCPU-Second[-ARM],
# USW2-Lambda-MicroVM-Memory-GB-Second[-ARM] (no -ARM suffix = x86),
# USW2-Lambda-MicroVM-Snapshot-{Read,Write}-GB and
# USW2-Lambda-MicroVM-Snapshot-Storage-GB-Hour ($0.0001111111/GB-h = $0.08/GB-month).
#
# MICROVM_PRICING_JSON is the authoritative per-arch table the proxy reads.
export MICROVM_PRICING_JSON='{
  "arm64":  {"vcpu_per_sec": 0.0000276944, "memory_per_gb_sec": 0.0000036667, "snapshot_per_gb_month": 0.08, "snapshot_read_per_gb": 0.00155, "snapshot_write_per_gb": 0.00380},
  "x86_64": {"vcpu_per_sec": 0.0000326557, "memory_per_gb_sec": 0.0000043235, "snapshot_per_gb_month": 0.08, "snapshot_read_per_gb": 0.00155, "snapshot_write_per_gb": 0.00380}
}'

# Metrics retention (hours) — how long VM metrics are kept in the DB
export METRICS_RETENTION_HOURS="168"

# S3 session checkpoint retention (days)
export S3_CHECKPOINT_RETENTION_DAYS="30"

# MicroVM lifetime & persistence
# Max lifetime of one VM (seconds), passed as the VM's max duration at launch; AWS
# terminates the VM when it is reached. AWS max is 28800 (8h).
# Set a smaller value (e.g., 180) to test recovery after an AWS max-lifetime kill.
# Override: MAX_LIFETIME_SECONDS=180 ./local_install.sh
export MAX_LIFETIME_SECONDS="${MAX_LIFETIME_SECONDS:-28800}"
# Min idle time before AWS suspends the VM. AWS minimum is 60s. Single source of
# truth; the proxy + UI read this.
export IDLE_TIMEOUT_SECONDS="${IDLE_TIMEOUT_SECONDS:-60}"

# Session persistence mode — one of: checkpoint (default) | eternal | none.
# In checkpoint and eternal mode the VM saves its state to S3 on /suspend and on
# /terminate (skipped when nothing changed). There is no rotation timer: the
# modes differ only in when a new VM is started and linked to the session.
#   "checkpoint"  — When the VM is gone, state stays in S3 and the user restores
#                   it on the next launch/open of the notebook.
#   "eternal"     — Reactive recovery: the next proxied request that finds the VM
#                   dead launches a fresh VM, restores from S3, relinks the same
#                   session ID and replays the request (one cold start, ~9-12s).
#   "none"        — Ephemeral. State is NEVER checkpointed to S3 and there is no
#                   restore; when a VM suspends or terminates its session state is
#                   lost. Cheapest / simplest — good for throwaway or stateless runs.
# Any unrecognized value is treated as "none" (fails safe to ephemeral).
export SESSION_PERSISTENCE_MODE="${SESSION_PERSISTENCE_MODE:-checkpoint}"

# Port configuration
export PROXY_PORT="8081"
export BACKEND_PORT="8080"
# AI chat agent (Strands, extracted into its own local BedrockAgentCoreApp service).
# Same artifact we later deploy to AWS Bedrock AgentCore Runtime. Runs locally as a
# uvicorn server on 127.0.0.1:$AI_PORT exposing POST /invocations + GET /ping.
# 8080=backend, 8081=proxy, 5173=web UI — 8082 is free.
export AI_PORT="${AI_PORT:-8082}"
# WHERE the proxy forwards /ai/chat to (the agent's /invocations endpoint). Single
# source of truth for the agent's location — the proxy reads this, no host is
# hardcoded in code. Locally: the BedrockAgentCoreApp on localhost:$AI_PORT. At
# cloud cutover, set this to the AWS Bedrock AgentCore Runtime endpoint (this line
# is the only change needed on the proxy side).
export AGENT_URL="${AGENT_URL:-http://localhost:${AI_PORT}}"

# Bedrock model + region for the AI agent (chat) and the one-shot helpers.
# The agent process and the proxy both read BEDROCK_MODEL_ID; BEDROCK_REGION falls
# back to AWS_REGION when unset.
#
# Default is Claude Haiku 5.5 (switched from us.anthropic.claude-sonnet-4-6). An A/B
# benchmark of the product's real prompts (scripts/bench_models.py, Opus-5 judged,
# 3 runs) found Haiku 5.5 ~40% faster than Sonnet 4.6 at equal-or-better judged
# quality, for a modest output-token increase. Haiku 5.x deprecates `temperature`
# and defaults to verbose thinking, so all call sites build per-model inference
# params via */model_params.py (temperature dropped + thinking disabled for 5.x);
# set BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6 to revert.
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-haiku-5-5}"
export BEDROCK_REGION="${BEDROCK_REGION:-$AWS_REGION}"

# Models to EXCLUDE from the AI-assistant dropdown + fallback chain, comma-separated
# (base or CRIS-prefixed ids; normalized to base ids downstream). Operator knob for
# models that are capability-eligible but should NOT be offered — e.g. a model AWS
# has moved to LEGACY/deprecated. Seeded with Claude Sonnet 4.5 (Bedrock deprecation
# notice: Legacy now, end-of-life 2027-04-08). Add ids here as deprecation emails
# arrive; the proxy /ai/models route and the agent both read this and pass it to
# modelcaps.top_models_for_agent/fallback_chain. Empty = exclude nothing.
export BEDROCK_EXCLUDE_MODELS="${BEDROCK_EXCLUDE_MODELS:-anthropic.claude-sonnet-4-5-20250929-v1:0}"

# Max verify iterations for the AGENTIC "Fix code" loop. The Fix button runs an
# agent that proposes a fix, RUNS it on the connected MicroVM (execute_code) and
# install_package's any missing dep, iterating until the cell runs clean — so the
# user gets a pre-validated fix instead of the old apply-then-run-then-refix loop.
# This caps how many verify attempts it makes before returning its best effort
# (keeps a stubborn error from running many expensive LLM+VM round-trips).
export FIX_VERIFY_MAX_ATTEMPTS="${FIX_VERIFY_MAX_ATTEMPTS:-4}"

# Confirm-before-execute gate for the standalone interactive AI assistant.
# Default false = the assistant asks for an inline Approve/Reject before it runs
# execute_code or install_package, so a tool call driven by untrusted data-source
# content (prompt-injection) can't silently touch the MicroVM without the user
# saying yes. Set true to auto-run those tools immediately (today's behavior).
# This gates ONLY the chat assistant; the in-proxy Fix-loop is unaffected.
export AI_AGENT_AUTO_EXECUTE="${AI_AGENT_AUTO_EXECUTE:-false}"

# Chat agent conversation window, in MESSAGES: the agent's
# SlidingWindowConversationManager keeps the last N messages (10 = 5 user + 5
# assistant turns). Single source for both processes: the agent uses it for the
# window, the proxy exposes it on GET /ai/config, and the web chat shows the last
# N // 2 conversations as "in the AI's context" (older ones muted). Both default to
# 10 when unset. Restart the agent AND proxy after changing it.
export AGENT_CONVERSATION_WINDOW_SIZE="${AGENT_CONVERSATION_WINDOW_SIZE:-10}"

# ------------------------------------------------------------
# Chat agent conversation MEMORY backend (proxy/notebook/ai/memory.py)
# ------------------------------------------------------------
# Where the chat agent persists + rehydrates conversation history:
#   sqlite    (default, local/dev): DB-backed Strands RepositorySessionManager —
#             writes to the agent_sessions/agent_messages tables via the storage
#             interface (SQLite locally; synced to Aurora).
#   agentcore (cloud): Amazon Bedrock AgentCore Memory (managed). Flip this and set
#             AGENTCORE_MEMORY_ID and the agent persists/rehydrates via the managed
#             memory service instead of the DB — no code change, no proxy.storage
#             dependency. Both use the same Strands SessionManager seam, so the
#             agent construction is identical.
export MEMORY_BACKEND="${MEMORY_BACKEND:-sqlite}"
# Required when MEMORY_BACKEND=agentcore: the AgentCore Memory resource id (create
# it once via the AgentCore CLI `agentcore add memory` / boto3 / console). Ignored
# for sqlite.
export AGENTCORE_MEMORY_ID="${AGENTCORE_MEMORY_ID:-}"
# Optional: stable actor id for AgentCore Memory (pin to the authenticated user —
# Cognito sub / internal id — so long-term memory namespaces stay consistent). If
# unset, the code derives a stable per-notebook id (notebook-<id>), then session_id.
export AGENTCORE_ACTOR_ID="${AGENTCORE_ACTOR_ID:-}"

# ------------------------------------------------------------
# Data Source auto-discovery scope (read by proxy/platform/datasources providers)
# These declaratively bound what the Data Sources panel + entity discovery surface.
# The AWS role still limits what is *accessible*; these limit what is *shown*.
# Each provider reads its own scope below; leave blank/unset to use the defaults.
# ------------------------------------------------------------
# Athena: ATHENA_DB is the DEFAULT database for query EXECUTION (the db.table
# prefix, read_athena default, SQL engine catalog). ATHENA_WORKGROUP scopes runs.
export ATHENA_DB="microvm_demo_db"
export ATHENA_WORKGROUP="microvm-demo"
# Athena DISCOVERY scope (which databases' tables appear in the panel) is separate
# from the query default above. Set to a comma-separated allowlist to restrict which
# Glue databases are shown, or leave EMPTY to auto-discover EVERY database the role
# can access. Defaults to the demo DB so the panel stays clean; empty it (or add more)
# to surface additional databases the role has access to.
export DATASOURCE_ATHENA_DATABASES="${ATHENA_DB}"
# S3: comma-separated key prefixes to scan (one level deep).
# models/ is where the ML helpers (save_model / save_model_local + upload) write
# trained model artifacts, so it's scanned too — a saved model shows up in the
# Data Sources panel like any other S3 object. unload/ is where Athena UNLOAD
# (unload_athena_to_s3 / SQL-cell UNLOAD) lands its output, so it's scanned too —
# otherwise the exported data is invisible in the panel and can't be cleared by hand.
export DATASOURCE_S3_PREFIXES="samples/,user-data/,models/,sagemaker/,unload/"
# S3: comma-separated prefixes whose files a user may DELETE from the Data Sources
# panel. Should be a subset of DATASOURCE_S3_PREFIXES. Deleting a file removes the
# S3 object and triggers a Notebook Intel deletion update (like local file delete).
# Leave blank to disable S3 deletion entirely. models/, sagemaker/, and unload/ are
# user-owned artifacts, so they're deletable (unlike samples/, which is shared demo data).
export DATASOURCE_S3_DELETABLE_PREFIXES="user-data/,models/,sagemaker/,unload/"
# DynamoDB: comma-separated substrings; only tables whose name contains one are shown.
export DATASOURCE_DDB_NAME_FILTERS="microvm,demo,ecommerce"

# CloudWatch Logs: comma-separated log-group-name PREFIXES to surface in the Data
# Sources panel (and query from read_logs/ask_logs). Defaults to this platform's OWN
# VM log groups so the panel is useful + scoped out of the box; widen it to expose
# your own app's groups (e.g. "/aws/lambda-microvms/,/myapp/"). Leave blank to hide
# CloudWatch entirely. The exec role is granted logs query actions scoped to
# /aws/lambda-microvms/* (see iam/exec-role-permissions.json) — widening the prefix
# to other groups also needs that query grant broadened to match.
export DATASOURCE_CLOUDWATCH_LOG_GROUP_PREFIXES="${DATASOURCE_CLOUDWATCH_LOG_GROUP_PREFIXES:-/aws/lambda-microvms/}"

# ------------------------------------------------------------
# S3 Tables (Apache Iceberg) — scripts/setup_s3_tables.sh, run on every
# local_install.sh (always installed, idempotent).
#
# Provisions a REAL S3 Table bucket (visible on the S3 -> Table buckets console
# page), a namespace, and two Iceberg tables (order_events, product_price_history)
# seeded with data correlated to the existing orders/products CSV tables. Tables
# are created + seeded via Athena against the "s3tablescatalog" federated Glue
# catalog (the S3 Tables <-> analytics integration must be enabled once per
# account/region from the S3 console or the Glue API).
#
# HOW ATHENA REACHES THE TABLES: the child federated catalog
# (s3tablescatalog/<bucket>) is registered ONCE as an Athena data catalog named
# S3TABLES_ATHENA_CATALOG; queries run with that catalog + the namespace as the
# execution context, using plain unqualified table names and Iceberg-implicit
# CREATE TABLE (no LOCATION, no table_type).
# ------------------------------------------------------------
# Real S3 Table bucket name (lowercase; account-suffixed so it is globally unique
# within the account/region). The bucket ARN is derived, not stored.
export S3TABLES_BUCKET="${S3TABLES_BUCKET:-microvm-tables-${ACCOUNT_ID}}"
# Namespace (a.k.a. database) that groups the demo tables inside the bucket.
# Named to read as the Iceberg counterpart of the CSV Glue DB (microvm_demo_db).
export S3TABLES_NAMESPACE="${S3TABLES_NAMESPACE:-microvm_demo_iceberg}"
# Name we register the child federated catalog under as an Athena data source, and
# the name the proxy/providers reference it by. Alphanumeric/underscore only.
export S3TABLES_ATHENA_CATALOG="${S3TABLES_ATHENA_CATALOG:-s3tables_microvm}"

# ------------------------------------------------------------
# Bedrock Knowledge Base (scripts/setup_bedrock_kb.sh, run on every
# local_install.sh — always installed, idempotent).
#
# If provisioning fails (e.g. a missing permission) the launch warns and continues
# with a blank BEDROCK_KB_ID ("no Bedrock KB data source"). The setup script provisions a
# Bedrock Knowledge Base backed by Amazon S3 Vectors (serverless, pay-per-use —
# no OpenSearch/Aurora vector cost), configured as follows:
#   • Embedding model : Titan Text Embeddings V2 (1024-dim, FLOAT32)
#   • Vector store    : S3 Vectors bucket + index (float32, dim 1024, euclidean)
#   • Data source     : the product-guide PDFs under the S3 prefix below
#   • Parsing         : BEDROCK_FOUNDATION_MODEL, MULTIMODAL, via the Sonnet
#                       inference profile (so figures/tables in the PDFs are
#                       extracted) — this needs the supplemental-storage bucket.
#
# Source PDFs are scripts/demoproductspec/*.pdf (+ .metadata.json sidecars +
# manifest.json), generated by scripts/generate_product_specs.py and NOT committed.
# On first install (empty folder) the setup script generates them with --no-llm
# (built-in template, no Bedrock calls) from the Athena demo tables; later runs
# reuse them. It syncs them to s3://${ARTIFACT_BUCKET}/${BEDROCK_KB_S3_PREFIX}.
# Regenerate with Bedrock-authored content: bash scripts/setup_bedrock_kb.sh --regen
# (add --no-llm for the template).
#
# DO NOT hardcode the KB id / index ARN here. Like the Aurora ARNs, they are
# DISCOVERED/created by setup_bedrock_kb.sh and written to scripts/.kb_env, which
# local_install.sh sources AFTER this file — so resolved values win. Left blank
# here, a blank BEDROCK_KB_ID is the correct default for a no-flag run.
# ------------------------------------------------------------
# Names for the resources the setup script creates (prefixed so they don't collide
# with other KBs/buckets in the account). Override via env if desired.
export BEDROCK_KB_NAME="${BEDROCK_KB_NAME:-microvm-product-kb}"
export BEDROCK_KB_VECTOR_BUCKET="${BEDROCK_KB_VECTOR_BUCKET:-microvm-kb-vectors-${ACCOUNT_ID}}"
export BEDROCK_KB_VECTOR_INDEX="${BEDROCK_KB_VECTOR_INDEX:-microvm-kb-default-index}"
# Execution role Bedrock assumes to read S3 + write vectors + call the models.
export BEDROCK_KB_ROLE_NAME="${BEDROCK_KB_ROLE_NAME:-MicroVMKnowledgeBaseRole}"
# S3 prefix (on the existing ARTIFACT_BUCKET) that holds the product-guide PDFs
# and is the KB's data source. Trailing slash matters.
export BEDROCK_KB_S3_PREFIX="${BEDROCK_KB_S3_PREFIX:-knowledge-base/product-guides/}"
# Supplemental multimodal storage — where Bedrock writes the page images it
# extracts during MULTIMODAL parsing. Bedrock REQUIRES this to be a BUCKET ROOT
# (s3://bucket), NOT a prefix/sub-folder ("...contains a sub-folder which is not
# supported"), so it must be its OWN dedicated bucket, separate from the
# source-PDF bucket. The setup script creates it if absent.
export BEDROCK_KB_SUPPLEMENTAL_BUCKET="${BEDROCK_KB_SUPPLEMENTAL_BUCKET:-microvm-kb-multimodal-${ACCOUNT_ID}}"
# Embedding model + vector geometry (must match the index). Titan v2 = 1024 dims.
export BEDROCK_KB_EMBED_MODEL="${BEDROCK_KB_EMBED_MODEL:-amazon.titan-embed-text-v2:0}"
export BEDROCK_KB_EMBED_DIMENSIONS="${BEDROCK_KB_EMBED_DIMENSIONS:-1024}"
# KB multimodal parser model (its OWN setting — do NOT couple to BEDROCK_MODEL_ID).
# Bedrock Knowledge Base validates the FM parser against its OWN approved-parser
# allowlist, which is SEPARATE from (and narrower than) "can this model accept an
# image via Converse". Per AWS docs, KB parsing accepts the "Claude / Nova / Llama 4
# vision model" families, but a given new model is not necessarily on KB's list yet:
# Claude Haiku 5.5 is vision-capable at the Converse API (our model_caps.json probes
# vision=true for it) YET KB rejects it as a parser with "Invalid Bedrock Foundation
# Model Parser Provided" — i.e. it is not on KB's approved-parser allowlist here.
# So the KB parser must be pinned independently to a KB-APPROVED model; Sonnet 4.6
# is verified-accepted by KB for this account/region. The chat model
# (BEDROCK_MODEL_ID) can move to models KB hasn't blessed yet, which is exactly why
# these are separate knobs. The setup script resolves this to an inference-profile ARN.
#   docs: https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-supported.html
export BEDROCK_KB_PARSER_MODEL="${BEDROCK_KB_PARSER_MODEL:-us.anthropic.claude-sonnet-4-6}"
# KB RAG answer-GENERATION model for ask_bedrock_kb (RetrieveAndGenerate). ALSO its
# own setting, decoupled from BEDROCK_MODEL_ID for the SAME class of reason as the
# parser: RetrieveAndGenerate only uses its built-in default prompt template for the
# models the service blesses; for a model it has no default template for (empirically,
# Claude Haiku 5.5) it returns "Custom prompt templates must be provided for both
# Orchestration and Generation when using the selected model" and refuses to run
# without hand-authored templates. (AWS docs do NOT publish which models need this —
# the RetrieveAndGenerate API error is the only source; the kb-test-config.html
# placeholder table still lists only Claude v2.x / Claude 3 Sonnet, i.e. it is stale.)
# Sonnet 4.6 is verified to work with the default template here, so pin it. The chat
# model (BEDROCK_MODEL_ID) stays on Haiku; only the KB RAG generation uses this.
export BEDROCK_KB_GENERATION_MODEL="${BEDROCK_KB_GENERATION_MODEL:-us.anthropic.claude-sonnet-4-6}"
# Resolved at provisioning time and written to scripts/.kb_env (blank = no KB).
# COMMA-SEPARATED LIST: BEDROCK_KB_ID may hold more than one KB id
# (e.g. "AVSDQZX2CR,BKB9ZZ12QW") — the provider surfaces each as its own Data
# Source and entity-discovery profiles each one. A single id is the common case.
export BEDROCK_KB_ID="${BEDROCK_KB_ID:-}"
export BEDROCK_KB_INDEX_ARN="${BEDROCK_KB_INDEX_ARN:-}"

# ------------------------------------------------------------
# SageMaker managed training / batch transform (scripts/setup_sagemaker.sh).
# Phase 1: a notebook SUBMITS a SageMaker Training Job or Batch Transform job that
# runs on SageMaker-managed compute (bigger/GPU, unbounded by the VM size) and
# writes a model artifact to S3 — NO always-on real-time endpoints (that's Phase 2,
# the standing-cost surface). The in-VM helpers are sm_train / sm_training_job_status /
# sm_load_model / predict_batch (+ sm_predict_batch) / sm_jobs (app/notebook/helpers/sagemaker.py).
#
# SageMaker jobs run as their OWN IAM role (MicroVMSageMakerRole), which SageMaker
# assumes — NOT the VM exec role. The VM exec role only needs to SUBMIT/DESCRIBE
# jobs and iam:PassRole that role (both already in iam/exec-role-permissions.json).
#
# DO NOT hardcode the role ARN here. setup_sagemaker.sh (run on EVERY local_install.sh,
# idempotent — it only creates a free IAM role, so there's nothing to gate) creates
# the role and writes the resolved SAGEMAKER_EXEC_ROLE_ARN (+ default instance types)
# to scripts/.sagemaker_env, which local_install.sh sources AFTER this file so the
# resolved value wins. Left blank here, a blank role ARN means "SageMaker not
# configured" → the sm_* helpers raise a clear, actionable error and nothing else
# changes (the graceful-degradation the Aurora/KB helpers also use). No standing
# cost exists until a user explicitly calls sm_train()/predict_batch(job, df).
# ------------------------------------------------------------
export SAGEMAKER_ROLE_NAME="${SAGEMAKER_ROLE_NAME:-MicroVMSageMakerRole}"
export SAGEMAKER_EXEC_ROLE_ARN="${SAGEMAKER_EXEC_ROLE_ARN:-}"
# Default instance types for training + batch transform (overridable per-call in
# sm_train(..., instance=...)). ml.m5.xlarge
# is a solid CPU default for tabular XGBoost; use a g-family type for GPU training.
export SAGEMAKER_TRAIN_INSTANCE="${SAGEMAKER_TRAIN_INSTANCE:-ml.m5.xlarge}"
export SAGEMAKER_TRANSFORM_INSTANCE="${SAGEMAKER_TRANSFORM_INSTANCE:-ml.m5.xlarge}"
# S3 prefix (on ARTIFACT_BUCKET) under which training inputs + model artifacts live.
# Trailing slash matters. Added to DATASOURCE_S3_PREFIXES below so trained models
# surface in the Data Sources panel like any other artifact.
export SAGEMAKER_ARTIFACT_PREFIX="${SAGEMAKER_ARTIFACT_PREFIX:-sagemaker/}"

# Phase 2 (real-time endpoints): the CUSTOM serving container that sm_deploy_* points
# SageMaker at. ONE image serves BOTH sklearn (.joblib) and ONNX (.onnx); it binds
# gunicorn on :8080 and writes only to /tmp, so it works on SERVERLESS (the stock
# SageMaker SKLearn/Triton containers do not — see docs). Built + pushed to ECR by
# the setup step; the resolved URI is written to scripts/.sagemaker_env (like the
# exec role ARN) so it isn't hardcoded. Blank ⇒ endpoints not available yet →
# sm_deploy_* raises a clear error; training/in-VM scoring are unaffected.
export SAGEMAKER_SERVING_IMAGE="${SAGEMAKER_SERVING_IMAGE:-}"
export SAGEMAKER_SERVING_REPO="${SAGEMAKER_SERVING_REPO:-microvm-sagemaker-serving}"
# Serverless endpoint defaults (overridable per-call in sm_deploy_serverless(..., memory_mb=,
# max_concurrency=)). Memory must be a multiple of 1024 (1024–6144).
export SAGEMAKER_SERVERLESS_MEMORY_MB="${SAGEMAKER_SERVERLESS_MEMORY_MB:-2048}"
export SAGEMAKER_SERVERLESS_MAX_CONCURRENCY="${SAGEMAKER_SERVERLESS_MAX_CONCURRENCY:-5}"

# Project root
export ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Helper: call lambda-microvms with profile and region
aws_mvm() {
  aws lambda-microvms "$@" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION"
}

# Helper: call ECR with profile and region (used by setup_sagemaker.sh to build +
# push the custom SageMaker serving container).
aws_ecr() {
  aws ecr "$@" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION"
}
