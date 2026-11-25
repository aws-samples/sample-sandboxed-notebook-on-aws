#!/usr/bin/env bash
# ============================================================
# AWS MicroVM Mode — Full Self-Contained Launch
#
# This script handles everything:
# 1. Creates IAM roles + S3 bucket (if not exists)
# 2. Builds the MicroVM image (if not exists)
# 3. Starts the token proxy (with image ARN configured)
# 4. Starts the notebook web UI
#
# New notebook tabs in the UI auto-launch MicroVM instances.
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "$ROOT_DIR/app.zip"
source "$ROOT_DIR/scripts/config.sh"

# --- Args ---
# Aurora is OPT-IN. With NO Aurora flag, the whole solution runs WITHOUT Aurora:
# no cluster is created/touched, no Aurora values are passed to the proxy (VMs get
# "no Aurora data source"), and the SQLite→Aurora sync worker is NOT started.
#
# --AuroraSetup: intent to USE Aurora. Ensure the cluster/DB/schema EXIST
#   (idempotent — NON-destructive). If the cluster is missing it is provisioned and
#   its ARNs captured; if it already exists it is reused as-is with its data intact.
#   This is the flag for everyday runs now that Aurora is set up.
#
# --AuroraReset: DESTRUCTIVE. Everything --AuroraSetup does, PLUS reset schemas to a
#   clean, current shape before launch:
#     • SQLite — delete the DB file so the proxy recreates it fresh on start.
#     • Aurora — setup_aurora_postgres.sh DROPS + recreates all tables and v_nb_*
#                views (drop-and-recreate, no migrations). Cluster/roles/secrets kept.
#   Use only when you intend to wipe and rebuild the synced schema.
#
# ALWAYS-ON platform data sources (no flag needed): the Bedrock Knowledge Base
#   (S3 Vectors, from the product-guide PDFs — generated on first install) and the S3 Tables / Apache
#   Iceberg demo tables (order_events, product_price_history) are now part of the
#   standard install — every run provisions/reuses them (idempotent). They add a
#   few minutes on first provision; on later runs they're reused with no data loss.
#   Aurora gates the SQLite→Aurora sync worker and the "Notebook Analytics &
#   Cost" sample notebook.
#
# --WarmPool: OPT-IN. Start the VM Pool Manager — a warm pool of pre-booted,
#   suspended spare MicroVMs (all image sizes) so new-notebook launches and
#   session restores skip the cold-provisioning floor. Sets POOL_ENABLED=true,
#   which (1) starts the pool_manager service and (2) tells the proxy to grant
#   VMs from the pool and to FREEZE idle-timeout + max-lifetime at their config
#   values (pooled VMs are pre-booted with fixed lifecycle, so those controls
#   are locked in the UI). With NO --WarmPool flag the pool is off and launch/
#   restore behavior is unchanged. A pre-set POOL_ENABLED env var is honored too;
#   the flag just forces it on. Pool tuning lives in scripts/config.sh
#   (POOL_SIZE_PER_IMAGE, POOL_MAX_AGE_HOURS, POOL_VM_BACKEND, etc.).
AURORA_SETUP=false
AURORA_RESET=false
for arg in "$@"; do
  case "$arg" in
    # Intent to use Aurora: ensure the cluster/DB/schema EXIST (idempotent, no
    # data loss). If the cluster is missing it is provisioned; if it already
    # exists it is reused as-is.
    --AuroraSetup) AURORA_SETUP=true ;;
    # DESTRUCTIVE: drop + recreate the Aurora schema (wipes the synced tables/
    # views, then recreates them clean). Implies --AuroraSetup.
    --AuroraReset) AURORA_SETUP=true; AURORA_RESET=true ;;
    # Opt into the VM Pool Manager (warm pool of pre-booted, suspended spare
    # MicroVMs for fast launch/restore). Sets POOL_ENABLED=true, which gates the
    # pool_manager service launch below AND tells the proxy to grant-from-pool
    # (and to FREEZE idle-timeout + max-lifetime, since pooled VMs have fixed
    # values). Without this flag the pool is off and behavior is unchanged.
    # Also respects a pre-set POOL_ENABLED env var; the flag just forces it on.
    --WarmPool) POOL_ENABLED=true ;;
    *) echo "⚠ Unknown argument: $arg (ignored)" ;;
  esac
done
# Export so the gated pool_manager launch below and the proxy both see it.
# Honors either the --WarmPool flag or a pre-set POOL_ENABLED env; defaults off.
export POOL_ENABLED="${POOL_ENABLED:-false}"

cleanup() {
  echo ""
  echo ">> Shutting down proxy & UI..."
  # Guard every PID with ${VAR:-}: the trap can fire during the setup phase (before
  # the servers are launched), and under `set -u` a bare $PROXY_PID would abort the
  # trap with "unbound variable" instead of cleaning up. All four may be unset.
  kill ${PROXY_PID:-} 2>/dev/null || true
  kill ${AGENT_PID:-} 2>/dev/null || true
  kill ${FRONTEND_PID:-} 2>/dev/null || true
  kill ${SYNC_PID:-} 2>/dev/null || true
  kill ${POOL_PID:-} 2>/dev/null || true
  echo "   MicroVMs launched by the UI are still running."
  echo "   To terminate all: bash scripts/teardown.sh"
  exit 0
}
trap cleanup SIGINT SIGTERM

echo "============================================"
echo "  Sandboxed Notebook on AWS — AWS Mode"
echo "============================================"
echo "  Region:   $AWS_REGION"
echo "  Account:  $ACCOUNT_ID"
echo "  Image:    $IMAGE_BASE_NAME (arches: $IMAGE_ARCHES)"
if [ -n "${IMAGE_ARCHES_DROPPED:-}" ]; then
  echo "  ⚠ x86_64 is not available for Lambda MicroVMs in $AWS_REGION (excluded: $X86_UNSUPPORTED_REGIONS) — building and offering arm64 only"
fi
if [ "$AURORA_RESET" = "true" ]; then
  echo "  Setup:    --AuroraReset (use Aurora + reset SQLite & drop/recreate Aurora schema)"
elif [ "$AURORA_SETUP" = "true" ]; then
  echo "  Setup:    --AuroraSetup (use Aurora; ensure cluster/schema exist, non-destructive)"
else
  echo "  Setup:    (no Aurora — running fully local, sync worker disabled)"
fi
echo "  KB:       Bedrock Knowledge Base (S3 Vectors) — always installed"
echo "  S3 Tables:Apache Iceberg demo tables (order_events, product_price_history) — always installed"
echo ""

# --- Kill stale processes from a previous run ---
for port in $BACKEND_PORT $PROXY_PORT ${AI_PORT:-8082} 5173 ${POOL_MANAGER_PORT:-8090}; do
  lsof -ti :$port 2>/dev/null | xargs kill -9 2>/dev/null || true
done
# The SQLite->Aurora sync worker listens on NO port, so the port kill above misses
# it — and it survives a Ctrl-C that doesn't reach the cleanup() trap (e.g. the
# terminal being closed). Without this, every rerun leaks another worker: N copies
# each tick once/min, so the log fills with clustered "sync ok" lines. Reap any
# stale sync worker by name so exactly one runs after launch.
pkill -f "sync.sync_to_aurora" 2>/dev/null || true

# --- Preflight: required command-line tools ------------------------------------
# Fail FAST and CLEARLY if a required tool is missing, instead of dying with a
# cryptic "jq: command not found" deep inside a setup subscript. We collect ALL
# missing tools first, then print one actionable block with install paths — so a
# fresh Mac sees everything to fix in a single pass. Homebrew is the macOS norm; we
# tailor the hint to whether `brew` is present. (python3 / aws / node version get
# their own dedicated checks below because they need version/interpreter logic.)
#
# NOTE on jq: current macOS (Sequoia 15+) ships /usr/bin/jq, but Sonoma (14) and
# earlier do NOT — and the Aurora + Bedrock-KB setup scripts use jq, so without
# this check an older Mac would fail mid-provision. That's the gap this guards.
echo ">> Checking required tools..."
_HAS_BREW=false
command -v brew &>/dev/null && _HAS_BREW=true
_MISSING_TOOLS=()
# tool|what it's for
for _spec in \
  "jq|parse AWS JSON in the Aurora/Bedrock-KB setup scripts" \
  "curl|download installers + health-check the local services" \
  "unzip|unpack build artifacts" \
  "git|clone/version metadata"; do
  _tool="${_spec%%|*}"
  if ! command -v "$_tool" &>/dev/null; then
    _MISSING_TOOLS+=("$_spec")
  fi
done
if [ "${#_MISSING_TOOLS[@]}" -gt 0 ]; then
  echo "❌ Missing required tool(s):"
  _brew_list=""
  for _spec in "${_MISSING_TOOLS[@]}"; do
    _tool="${_spec%%|*}"; _why="${_spec#*|}"
    echo "     • $_tool — $_why"
    _brew_list="$_brew_list $_tool"
  done
  echo ""
  if [ "$_HAS_BREW" = "true" ]; then
    echo "   Install them with Homebrew:"
    echo "     brew install$_brew_list"
  else
    echo "   Homebrew isn't installed. Install it first (https://brew.sh):"
    echo "     /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "   then:"
    echo "     brew install$_brew_list"
  fi
  echo "   Then re-run this script."
  exit 1
fi
echo "   Required tools present (jq, curl, unzip, git) ✓"

# --- Check dependencies ---
# Pick the HOST interpreter only to BOOTSTRAP a project virtualenv — we never
# install into (or run from) the host Python directly. Everything below runs from
# .venv, so the install is fully isolated and we sidestep the modern-macOS
# "externally managed environment" (PEP 668) problem that forces
# --break-system-packages against Homebrew/Apple Python.
# Minimum version: agent/ai_assistant/pyproject.toml declares requires-python >=3.13,
# and the proxy, pool manager and agent all run from the same .venv.
MIN_PYTHON_VERSION="3.13"
# python_is_new_enough <interpreter> → exit 0 when it runs and is >= MIN_PYTHON_VERSION.
python_is_new_enough() {
  "$1" -c "import sys; sys.exit(0 if sys.version_info >= tuple(int(p) for p in '$MIN_PYTHON_VERSION'.split('.')) else 1)" >/dev/null 2>&1
}
HOST_PYTHON=""
for candidate in python3.14 python3.13 python3 python; do
  if command -v "$candidate" &>/dev/null && python_is_new_enough "$candidate"; then
    HOST_PYTHON="$candidate"
    break
  fi
done
if [ -z "$HOST_PYTHON" ]; then
  echo "❌ Python $MIN_PYTHON_VERSION or newer not found (tried python3.14, python3.13, python3, python)."
  if command -v python3 &>/dev/null; then
    echo "   python3 is $(python3 --version 2>&1) ($(command -v python3))."
  fi
  echo "   Install Python $MIN_PYTHON_VERSION+ from https://www.python.org/downloads/"
  echo "   (or 'brew install python@3.13'), then re-run this script."
  exit 1
fi
echo "   Host Python: $($HOST_PYTHON --version)  ($(command -v "$HOST_PYTHON"))"

# --- Project virtualenv (.venv) ------------------------------------------------
# All proxy/agent/sync deps live in .venv at the repo root — created once, reused on
# every run. $PYTHON is repointed at the venv interpreter and EXPORTED so every child
# script (setup_sample_data.sh, entity_discovery, …) and every service launch
# (uvicorn / agent_runtime / sync) uses the SAME isolated interpreter. This is what
# makes a bare `python3` in a subscript resolve to the venv, not Apple's
# /usr/bin/python3 (which lacks boto3) — the old "No module named 'boto3'" trap.
VENV_DIR="$ROOT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo ">> Creating project virtualenv at .venv ..."
  if ! "$HOST_PYTHON" -m venv "$VENV_DIR" 2>/dev/null; then
    echo "❌ Could not create a virtualenv at $VENV_DIR."
    echo "   The 'venv' module may be missing from your Python."
    echo "   Install a full Python 3 (https://www.python.org/downloads/ or 'brew install python3'),"
    echo "   or on Debian/Ubuntu: 'sudo apt-get install python3-venv'. Then re-run."
    exit 1
  fi
else
  echo ">> Reusing existing virtualenv at .venv"
  # A .venv made by an older interpreter keeps that version; dependency installs
  # (the agent needs >=3.13) would fail later with a less clear error.
  if ! python_is_new_enough "$VENV_PY"; then
    echo "❌ The existing .venv uses $("$VENV_PY" --version 2>&1); Python $MIN_PYTHON_VERSION or newer is required."
    echo "   Recreate it with $HOST_PYTHON ($($HOST_PYTHON --version 2>&1)):"
    echo "     rm -rf \"$VENV_DIR\" && ./local_install.sh"
    exit 1
  fi
fi
# From here on, $PYTHON IS the venv interpreter. Export so subscripts inherit it.
PYTHON="$VENV_PY"
export PYTHON
echo "   venv Python: $($PYTHON --version)  ($PYTHON)"

# --- Vendor the modelcaps runtime trio into the isolated services -------------
# The canonical modelcaps package lives at the repo root; the agent (AgentCore,
# self-contained build context) and the in-VM app ship decoupled and need an
# on-disk copy inside their own trees. Sync EARLY — before ai_assistant deps are
# installed and before any service starts — so the vendored copies are present for
# the agent's `python -m ai_assistant.agent_runtime` launch below. Uses $PYTHON for
# the JSON banner injection (stdlib only).
echo ">> Syncing modelcaps into agent + app (vendored runtime copies)"
bash "$ROOT_DIR/scripts/sync_modelcaps.sh"

# Check AWS CLI version (need 2.35.10+ for lambda-microvms)
if ! command -v aws &>/dev/null; then
  echo "❌ AWS CLI not found. Install from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  exit 1
fi

AWS_CLI_VERSION=$(aws --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
AWS_CLI_MAJOR=$(echo "$AWS_CLI_VERSION" | cut -d. -f1)
AWS_CLI_MINOR=$(echo "$AWS_CLI_VERSION" | cut -d. -f2)
AWS_CLI_PATCH=$(echo "$AWS_CLI_VERSION" | cut -d. -f3)

if [ "$AWS_CLI_MAJOR" -lt 2 ] || ([ "$AWS_CLI_MAJOR" -eq 2 ] && [ "$AWS_CLI_MINOR" -lt 35 ]) || \
   ([ "$AWS_CLI_MAJOR" -eq 2 ] && [ "$AWS_CLI_MINOR" -eq 35 ] && [ "$AWS_CLI_PATCH" -lt 10 ]); then
  echo "❌ AWS CLI version $AWS_CLI_VERSION is too old."
  echo "   Lambda MicroVMs requires AWS CLI 2.35.10+"
  echo ""
  echo "   Update with:"
  echo "   curl \"https://awscli.amazonaws.com/AWSCLIV2.pkg\" -o /tmp/AWSCLIV2.pkg"
  echo "   sudo installer -pkg /tmp/AWSCLIV2.pkg -target /"
  exit 1
fi
echo "   AWS CLI version: $AWS_CLI_VERSION ✓"

echo ">> Installing Python dependencies into .venv ..."
# Inside the venv there is no "externally managed" guard, so a plain pip install
# works — no --break-system-packages, no mutation of the host site-packages. Upgrade
# pip first (a venv's bundled pip can be old enough to misresolve some wheels).
"$PYTHON" -m pip install --quiet --upgrade pip 2>/dev/null || true
# proxy/requirements.txt is a GENERATED, fully hash-pinned lockfile (compiled from
# proxy/requirements.in + pool_manager/requirements.in via `uv pip compile
# --generate-hashes`). Install it with --require-hashes so every direct AND
# transitive dependency is verified against its sha256 — a tampered or substituted
# wheel on PyPI will fail the install instead of running arbitrary code under the
# operator's AWS admin credentials. --require-hashes is all-or-nothing: it only
# succeeds because the lock pins every requirement with hashes.
if ! uv pip install --quiet --require-hashes --python "$PYTHON" -r "$ROOT_DIR/proxy/requirements.txt"; then
  echo "❌ Failed to install Python dependencies into the venv."
  echo "   Try verbosely:  uv pip install --require-hashes --python \"$PYTHON\" -r proxy/requirements.txt"
  echo "   (A hash-mismatch or resolver error here names the offending package;"
  echo "    if a .in source changed, regenerate proxy/requirements.txt — see its header.)"
  exit 1
fi

# Validate the deps actually landed in the venv. boto3 is used by the proxy AND by
# the sample-data seeding subscript; a silent miss previously surfaced as a confusing
# "No module named 'boto3'" crash deep in seeding. Fail fast + clear.
if ! "$PYTHON" -c "import boto3" 2>/dev/null; then
  echo "❌ The venv interpreter cannot import boto3 after dependency install."
  echo "   venv: $PYTHON"
  echo "   The install step above reported success but boto3 isn't importable —"
  echo "   the .venv may be corrupt. Recreate it and re-run:"
  echo "     rm -rf \"$VENV_DIR\" && bash local_install.sh${AURORA_SETUP:+ --AuroraSetup}"
  exit 1
fi
# Install the AI chat-agent SERVICE *dependencies* into the SAME shared venv (the
# agent runs locally from this venv as `python -m ai_assistant.agent_runtime`, from
# inside agent/, NOT as an installed distribution). The agent is uv/pyproject-managed
# (agent/ai_assistant/pyproject.toml + uv.lock). We install ONLY the locked
# dependencies — NOT the project itself: `uv export --no-emit-project` emits the
# resolved deps from uv.lock, which we pip-install into $PYTHON so NO second venv is
# created. Installing the project (`uv pip install .`) would try to BUILD a wheel and
# fail — the package files sit flat in agent/ai_assistant/ (the dir IS the
# ai_assistant package), so setuptools' flat-layout auto-discovery errors with
# "Multiple top-level modules". Deps-only is also what the Dockerfile does
# (`uv sync --no-install-project`), so local + container stay consistent.
# KEEP hashes in the export (no --no-hashes) and install with --require-hashes so the
# agent leg is verified the same way as the proxy leg — uv.lock pins every dep with a
# hash, so the export is fully hashed and the install is all-or-nothing.
if ! (cd "$ROOT_DIR/agent/ai_assistant" && \
      uv export --no-emit-project --no-dev --quiet 2>/dev/null \
        | uv pip install --quiet --require-hashes --python "$PYTHON" -r -); then
  echo "❌ Failed to install ai_assistant dependencies into the venv."
  echo "   Try verbosely:  (cd agent/ai_assistant && uv export --no-emit-project --no-dev | uv pip install --require-hashes --python \"$PYTHON\" -r -)"
  exit 1
fi
echo "   Dependencies installed in .venv ✓"

# --- Env-var encryption master key (always; foundational) --------------------
# Notebook env var VALUES are sealed at rest by the proxy (proxy/platform/env_crypto.py)
# using a master key in Secrets Manager. Needed on EVERY run (the proxy seals env
# vars regardless of Aurora), so provision it unconditionally + idempotently before
# anything else. Non-fatal: if it can't be created the proxy fails CLOSED on env-var
# writes (refuses to persist), which is the safe outcome.
echo ""
echo ">> Ensuring env-var encryption master key exists"
bash "$ROOT_DIR/scripts/setup_env_master_key.sh" || \
  echo "   ⚠ Master-key setup failed (non-fatal) — env-var persistence will fail closed until fixed."

# --- Aurora provisioning / gate (opt-in, Option A) ---------------------------
# AURORA_ENABLED is the single computed truth for "does this run use Aurora?".
# It is true ONLY when an Aurora flag was passed AND we confirm the cluster is
# really usable. It controls (a) whether Aurora ARNs are passed to the proxy (so
# VMs get an Aurora data source) and (b) whether the SQLite→Aurora sync worker
# starts. With NO flag, we never touch Aurora and never look at the hardcoded
# config.sh ARNs — the solution runs fully local.
#
# AURORA_ENV_FILE is the machine-readable handoff written by
# setup_aurora_postgres.sh (resolved cluster/secret ARNs). When present we source
# it so a freshly-provisioned OR pre-existing cluster feeds the proxy/sync the
# correct ARNs regardless of the hardcoded config.sh defaults.
AURORA_ENABLED=false
AURORA_ENV_FILE="$ROOT_DIR/scripts/.aurora_env"

# Helper: does the configured Aurora cluster actually exist?
aurora_cluster_exists() {
  aws rds describe-db-clusters \
    --db-cluster-identifier "$AURORA_CLUSTER_ID" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" &>/dev/null
}

# Helper: resolve the Aurora ARNs from the CONTROL PLANE only (describe-*), which
# works from anywhere (local included) — NO database connection. This is the
# local-safe path for an already-provisioned cluster: everything the proxy/VMs and
# the sync worker need is ARNs (used over the RDS Data API / 443), never psycopg2
# /5432. Writes the same .aurora_env the provisioning script would. Returns
# non-zero if any ARN can't be resolved.
resolve_aurora_arns() {
  local cluster_arn ro_arn rw_arn
  cluster_arn=$(aws rds describe-db-clusters \
    --db-cluster-identifier "$AURORA_CLUSTER_ID" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
    --query 'DBClusters[0].DBClusterArn' --output text 2>/dev/null) || return 1
  ro_arn=$(aws secretsmanager describe-secret \
    --secret-id "$AURORA_RO_SECRET_NAME" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
    --query 'ARN' --output text 2>/dev/null) || return 1
  rw_arn=$(aws secretsmanager describe-secret \
    --secret-id "$AURORA_RW_SECRET_NAME" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
    --query 'ARN' --output text 2>/dev/null) || return 1
  [ -n "$cluster_arn" ] && [ "$cluster_arn" != "None" ] || return 1
  [ -n "$ro_arn" ] && [ "$ro_arn" != "None" ] || return 1
  [ -n "$rw_arn" ] && [ "$rw_arn" != "None" ] || return 1
  cat > "$AURORA_ENV_FILE" <<ENVEOF
# Auto-generated by local_install.sh (control-plane resolve) — do not edit by hand.
export AURORA_DATA_API_CLUSTER_ARN="${cluster_arn}"
export AURORA_DATA_API_SECRET_ARN="${ro_arn}"
export AURORA_DATA_API_DATABASE="${AURORA_DB_NAME}"
export AURORA_DATA_API_RW_SECRET_ARN="${rw_arn}"
ENVEOF
  return 0
}

if [ "$AURORA_SETUP" = "true" ]; then
  echo ""
  if [ "$AURORA_RESET" = "true" ]; then
    echo ">> --AuroraReset: use Aurora + reset schemas to a clean, current shape"
    # (a) SQLite: delete the file so the proxy recreates it fresh on start.
    SQLITE_DB="$ROOT_DIR/proxy/data/microvm.db"
    rm -f "$SQLITE_DB" "$SQLITE_DB-wal" "$SQLITE_DB-shm"
    echo "   SQLite DB deleted — proxy will recreate it fresh ✓"
    # (a2) Chart PNGs persisted per notebook cell (notebook-outputs/<notebook_id>/…).
    # Their DB rows are wiped with the schemas, and notebook ids restart, so the
    # objects go too. No lifecycle rule expires this prefix (charts live as long
    # as their cells). Safe on a first install, before the bucket exists.
    aws s3 rm "s3://$ARTIFACT_BUCKET/notebook-outputs/" --recursive --quiet \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
      && echo "   S3 notebook-outputs/ (persisted charts) cleared ✓" \
      || echo "   (no notebook-outputs/ to clear)"
  else
    echo ">> --AuroraSetup: ensuring Aurora exists (idempotent, non-destructive)"
  fi

  # Decide the path. setup_aurora_postgres.sh does its in-database work (roles,
  # grants, schema DDL, view grants) over the RDS Data API (443), so it works from
  # local too — but it's only NEEDED when the cluster/schema must change:
  #   • cluster MISSING  → fresh provision (create cluster/roles/schema), OR
  #   • --AuroraReset     → intentional drop+recreate of the schema.
  # For the common LOCAL case (cluster already exists, no reset) we skip it entirely
  # and just resolve the ARNs from the control plane, then wire the Data-API-based
  # services (proxy VMs + sync worker). No DDL is re-run on an everyday launch.
  NEED_PROVISION=false
  if ! aurora_cluster_exists; then
    NEED_PROVISION=true
    echo "   Aurora cluster '$AURORA_CLUSTER_ID' not found — provisioning (cluster + schema over the Data API)..."
  elif [ "$AURORA_RESET" = "true" ]; then
    NEED_PROVISION=true
    echo "   Aurora cluster exists; --AuroraReset requested — drop/recreate schema over the Data API..."
  else
    echo "   Aurora cluster '$AURORA_CLUSTER_ID' exists — reusing; resolving ARNs from control plane (no DDL)."
  fi

  if [ "$NEED_PROVISION" = "true" ]; then
    # Run provisioning; if it fails, DON'T take down the whole stack — warn and fall
    # back to a local (no-Aurora) run, matching the sync-worker guard philosophy.
    if AURORA_RESET="$AURORA_RESET" bash "$ROOT_DIR/scripts/setup_aurora_postgres.sh"; then
      if [ -f "$AURORA_ENV_FILE" ]; then
        # shellcheck disable=SC1090
        source "$AURORA_ENV_FILE"
        echo "   Sourced resolved Aurora ARNs from ${AURORA_ENV_FILE} ✓"
      fi
      if aurora_cluster_exists; then
        AURORA_ENABLED=true
        echo "   Aurora ready ✓ (services will be wired to it)"
      else
        echo "   ⚠ Aurora cluster still not present after setup — continuing WITHOUT Aurora."
      fi
    else
      echo "   ⚠ Aurora setup failed — continuing WITHOUT Aurora (proxy runs on SQLite, sync disabled)."
    fi
  else
    # Local-safe path: control-plane ARN resolution only, no psycopg2.
    if resolve_aurora_arns; then
      # shellcheck disable=SC1090
      source "$AURORA_ENV_FILE"
      AURORA_ENABLED=true
      echo "   Resolved Aurora ARNs → ${AURORA_ENV_FILE} ✓"
      echo "   Aurora ready ✓ (services will be wired over the RDS Data API)"
    else
      echo "   ⚠ Could not resolve Aurora ARNs (cluster/secrets) — continuing WITHOUT Aurora."
    fi
  fi
else
  echo ""
  echo ">> Aurora: disabled (no --AuroraSetup/--AuroraReset). Running fully local."
fi

# ------------------------------------------------------------
# SageMaker execution role (always installed, idempotent). Unlike Aurora, there is
# NO standing resource to gate: setup_sagemaker.sh only creates an IAM ROLE (free,
# idle at $0). Actual spend happens ONLY when a user explicitly calls sm_train() /
# predict_batch(job, df), which submit transient managed jobs. So we provision the role
# unconditionally (mirroring the Bedrock KB handoff) and let the sm_* helpers just
# be available. On any failure we WARN and continue WITHOUT SageMaker (the in-VM
# sm_* helpers then raise a clear "SageMaker not configured" error) — never take the
# stack down. With the role present, scripts/.sagemaker_env carries the role ARN +
# default instance types, which we source so the proxy propagates them into VMs.
# ------------------------------------------------------------
SAGEMAKER_ENABLED=false
SAGEMAKER_ENV_FILE="$ROOT_DIR/scripts/.sagemaker_env"
echo ""
echo ">> Ensuring SageMaker execution role exists"
if bash "$ROOT_DIR/scripts/setup_sagemaker.sh"; then
  if [ -f "$SAGEMAKER_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$SAGEMAKER_ENV_FILE"
    if [ -n "${SAGEMAKER_EXEC_ROLE_ARN:-}" ]; then
      SAGEMAKER_ENABLED=true
      echo "   SageMaker ready ✓ (notebooks can sm_train/predict_batch via role: $SAGEMAKER_EXEC_ROLE_ARN)"
    else
      echo "   ⚠ .sagemaker_env has no SAGEMAKER_EXEC_ROLE_ARN — continuing WITHOUT SageMaker."
    fi
  else
    echo "   ⚠ setup_sagemaker.sh wrote no ${SAGEMAKER_ENV_FILE} — continuing WITHOUT SageMaker."
  fi
else
  echo "   ⚠ SageMaker role setup failed/skipped — continuing WITHOUT SageMaker."
fi

if ! command -v node &>/dev/null || ! command -v npm &>/dev/null; then
  echo "❌ Node.js / npm not found (needed to build + serve the web UI)."
  if [ "${_HAS_BREW:-false}" = "true" ]; then
    echo "   Install with Homebrew:  brew install node"
  else
    echo "   Install Node.js LTS (20.19+ or 22.12+) from https://nodejs.org/ (or 'brew install node')."
  fi
  echo "   Then re-run this script."
  exit 1
fi
# Vite 8 (web/package.json) requires Node >= 20.19 or >= 22.12. An older Node fails
# with a confusing Vite/ESM error at `npm run dev`, so check the MAJOR up front and
# warn with the real requirement rather than letting the dev server crash later.
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
if [ "${NODE_MAJOR:-0}" -lt 20 ]; then
  echo "⚠ Node.js $(node --version 2>/dev/null) is older than the UI needs (Vite 8 wants"
  echo "   Node 20.19+ or 22.12+). The notebook UI (npm run dev) may fail to start."
  if [ "${_HAS_BREW:-false}" = "true" ]; then
    echo "   Upgrade with:  brew upgrade node   (or install an LTS via nvm)."
  else
    echo "   Upgrade from https://nodejs.org/ (LTS), or use nvm."
  fi
  echo "   Continuing — the proxy + MicroVMs still work without the UI."
fi

if [ ! -d "$ROOT_DIR/web/node_modules" ]; then
  echo ">> Installing web dependencies..."
  (cd "$ROOT_DIR/web" && npm install)
fi

# --- Ensure S3 bucket exists ---
echo ">> Checking S3 bucket: $ARTIFACT_BUCKET"
if ! aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null; then
  echo "   Creating bucket..."
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket \
      --bucket "$ARTIFACT_BUCKET" \
      --profile "$AWS_CLI_PROFILE" \
      --region "$AWS_REGION"
  else
    aws s3api create-bucket \
      --bucket "$ARTIFACT_BUCKET" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" \
      --profile "$AWS_CLI_PROFILE" \
      --region "$AWS_REGION"
  fi
else
  echo "   Bucket exists ✓"
fi

# --- Set S3 lifecycle rules: expire transient/session-scoped data after N days ---
# put-bucket-lifecycle-configuration REPLACES the whole config, so this asserts the
# FULL rule set (must match scripts/setup_iam.sh, which also applies it — whichever
# runs last wins, so they have to agree or one silently clobbers the other). Per
# prefix so long-lived platform assets are untouched:
#   • sessions/ user-data/ attachments/ athena-results/ → expire
#   • samples/ data-catalog/ images/ notebook-outputs/  → kept (not listed;
#     notebook-outputs/ holds cell chart PNGs referenced from notebook_cells —
#     deleted with their cell/notebook and on --AuroraReset, never expired)
echo "   Setting lifecycle rules: sessions/, user-data/, attachments/, athena-results/ expire after ${S3_CHECKPOINT_RETENTION_DAYS} days"
aws s3api put-bucket-lifecycle-configuration \
  --bucket "$ARTIFACT_BUCKET" \
  --lifecycle-configuration '{
    "Rules": [
      { "ID": "ExpireSessionCheckpoints", "Status": "Enabled", "Filter": {"Prefix": "sessions/"}, "Expiration": {"Days": '"$S3_CHECKPOINT_RETENTION_DAYS"'} },
      { "ID": "ExpireUserData", "Status": "Enabled", "Filter": {"Prefix": "user-data/"}, "Expiration": {"Days": '"$S3_CHECKPOINT_RETENTION_DAYS"'} },
      { "ID": "ExpireChatAttachments", "Status": "Enabled", "Filter": {"Prefix": "attachments/"}, "Expiration": {"Days": '"$S3_CHECKPOINT_RETENTION_DAYS"'} },
      { "ID": "ExpireAthenaResults", "Status": "Enabled", "Filter": {"Prefix": "athena-results/"}, "Expiration": {"Days": '"$S3_CHECKPOINT_RETENTION_DAYS"'} }
    ]
  }' \
  --profile "$AWS_CLI_PROFILE" \
  --region "$AWS_REGION" 2>/dev/null || true

# --- Ensure IAM roles exist ---
echo ">> Checking IAM roles..."
if ! aws iam get-role --role-name "$BUILD_ROLE_NAME" --profile "$AWS_CLI_PROFILE" &>/dev/null; then
  echo "   Creating build role: $BUILD_ROLE_NAME"
  aws iam create-role \
    --role-name "$BUILD_ROLE_NAME" \
    --assume-role-policy-document file://"$ROOT_DIR/iam/build-role-trust.json" \
    --profile "$AWS_CLI_PROFILE" >/dev/null
  echo "   Waiting 10s for IAM propagation..."
  sleep 10
else
  echo "   Build role exists ✓"
fi

# Always (re)apply the build role policy so permission changes are picked up on
# every run — NOT just at role creation. The build role is used ONLY during image
# creation (Dockerfile build + /ready hook); at runtime, the VM assumes the
# EXECUTION role (executionRoleArn). However, the build role currently carries
# data permissions (Glue/DynamoDB/Athena/S3/Secrets) as well — this is legacy
# over-provisioning, not a platform requirement. Build-only permissions (s3:Get
# for artifacts + CloudWatch logs) would suffice. Keeping the broad policy for
# now avoids a disruptive change; a future cleanup can strip it to build-only.
BUILD_POLICY=$(sed "s/\${ARTIFACT_BUCKET}/$ARTIFACT_BUCKET/g" "$ROOT_DIR/iam/build-role-permissions.json")
aws iam put-role-policy \
  --role-name "$BUILD_ROLE_NAME" \
  --policy-name "BuildPolicy" \
  --policy-document "$BUILD_POLICY" \
  --profile "$AWS_CLI_PROFILE"

if ! aws iam get-role --role-name "$EXEC_ROLE_NAME" --profile "$AWS_CLI_PROFILE" &>/dev/null; then
  echo "   Creating exec role: $EXEC_ROLE_NAME"
  aws iam create-role \
    --role-name "$EXEC_ROLE_NAME" \
    --assume-role-policy-document file://"$ROOT_DIR/iam/exec-role-trust.json" \
    --profile "$AWS_CLI_PROFILE" >/dev/null
else
  echo "   Exec role exists ✓"
fi

# Always update the exec role policy (picks up permission changes)
aws iam put-role-policy \
  --role-name "$EXEC_ROLE_NAME" \
  --policy-name "ExecPolicy" \
  --policy-document file://"$ROOT_DIR/iam/exec-role-permissions.json" \
  --profile "$AWS_CLI_PROFILE"

# --- Setup sample data (DynamoDB + S3) ---
bash "$ROOT_DIR/scripts/setup_sample_data.sh"

# --- S3 Tables (Apache Iceberg) demo datasets (always installed) ---
# Runs AFTER sample data so orders/products exist (the Iceberg tables correlate to
# them). Provisions a real Amazon S3 Table bucket + namespace and creates/seeds
# order_events + product_price_history via Athena's federated s3tablescatalog; they
# surface through the existing Athena data source (auto-resolved federated catalog)
# and get profiled by the entity-intel refresh below. Idempotent + non-fatal.
echo ""
echo ">> Ensuring S3 Tables (Apache Iceberg) demo tables (order_events, product_price_history)"
bash "$ROOT_DIR/scripts/setup_s3_tables.sh" || echo "   ⚠ S3 Tables setup failed (non-fatal — continuing launch)"

# ------------------------------------------------------------
# Bedrock Knowledge Base (always installed). Mirrors the Aurora
# handoff pattern: setup_bedrock_kb.sh provisions/reuses the KB and writes
# scripts/.kb_env (KB id + index ARN); we source it so the proxy is given the KB
# (BEDROCK_KB_ID) as a Data Source. On failure we WARN and continue WITHOUT the KB
# (never take the whole stack down), matching the Aurora guard philosophy. With no
# flag, BEDROCK_KB_ID stays blank → the proxy exposes no Bedrock KB data source.
# ORDERING: runs AFTER the artifact bucket, setup_sample_data.sh and the S3 Tables
# step — on a fresh clone setup_bedrock_kb.sh generates the product-spec PDFs from
# the Athena products/orders tables (built-in template, --no-llm), so those tables
# must exist first, and it uploads the PDFs to the artifact bucket.
# ------------------------------------------------------------
KB_ENABLED=false
KB_ENV_FILE="$ROOT_DIR/scripts/.kb_env"
echo ""
echo ">> Ensuring Bedrock Knowledge Base (S3 Vectors) exists"
if bash "$ROOT_DIR/scripts/setup_bedrock_kb.sh"; then
  if [ -f "$KB_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$KB_ENV_FILE"
    if [ -n "${BEDROCK_KB_ID:-}" ]; then
      KB_ENABLED=true
      echo "   Bedrock KB ready ✓ (proxy will expose it as a Data Source: $BEDROCK_KB_ID)"
    else
      echo "   ⚠ .kb_env has no BEDROCK_KB_ID — continuing WITHOUT the Bedrock KB."
    fi
  else
    echo "   ⚠ setup_bedrock_kb.sh wrote no ${KB_ENV_FILE} — continuing WITHOUT the Bedrock KB."
  fi
else
  echo "   ⚠ Bedrock KB setup failed/skipped — continuing WITHOUT the Bedrock KB."
fi

# --- Refresh entity intel docs (profiles for the Workbook Intel) ---
# Runs right after seeding so the pre-computed per-entity profiles stay in lockstep with
# the data. Change-signal aware: unchanged entities are skipped, so this is cheap on
# restarts where data didn't change. NON-BLOCKING: discovery hitting Bedrock (throttle,
# creds) must never prevent the proxy/UI from starting — log a warning and continue.
echo ">> Refreshing entity intel docs (batch/entity_discovery)..."
if (cd "$ROOT_DIR" && \
    AWS_REGION="$AWS_REGION" ACCOUNT_ID="$ACCOUNT_ID" ARTIFACT_BUCKET="$ARTIFACT_BUCKET" \
    ATHENA_DB="$ATHENA_DB" ATHENA_WORKGROUP="$ATHENA_WORKGROUP" \
    DATASOURCE_ATHENA_DATABASES="$DATASOURCE_ATHENA_DATABASES" \
    S3TABLES_BUCKET="$S3TABLES_BUCKET" S3TABLES_NAMESPACE="$S3TABLES_NAMESPACE" \
    S3TABLES_ATHENA_CATALOG="$S3TABLES_ATHENA_CATALOG" \
    $PYTHON -m batch.entity_discovery); then
  echo "   Entity intel docs refreshed ✓"
else
  echo "   ⚠ Entity discovery failed (non-fatal) — Workbook Intel may use stale/absent profiles until it is re-run."
fi

# --- Ensure MicroVM images exist (all size tiers) ---
echo ">> Checking MicroVM images..."
NEEDS_BUILD=false
# Images are {IMAGE_BASE_NAME}-{arch}-{mem}; check every built arch × its sizes.
# image_arn_for keeps this in lock-step with the build/teardown naming.
for ARCH in $IMAGE_ARCHES; do
  for MEM in $(image_sizes_for "$ARCH"); do
    TIER_ARN=$(image_arn_for "$ARCH" "$MEM")
    STATE=$(aws_mvm get-microvm-image --image-identifier "$TIER_ARN" \
      --query 'state' --output text 2>/dev/null || echo "NOT_FOUND")
    if [ "$STATE" != "CREATED" ]; then
      NEEDS_BUILD=true
      break 2
    fi
  done
done

if [ "$NEEDS_BUILD" = "true" ]; then
  echo "   Some images missing — building all tiers..."
  # Retry the build once on failure. The build is idempotent (existing CREATED
  # tiers are skipped, missing/failed ones are rebuilt), so a retry cheaply
  # recovers from transient image-service errors instead of aborting the launch.
  build_ok=false
  for build_attempt in 1 2; do
    if bash "$ROOT_DIR/scripts/build_all_images.sh"; then
      build_ok=true
      break
    fi
    echo "   ⚠ Image build attempt ${build_attempt} failed — retrying in 10s (idempotent: existing tiers skipped)..."
    sleep 10
  done
  if [ "$build_ok" = "false" ]; then
    echo "   ❌ Image build failed after 2 attempts. Aborting."
    exit 1
  fi
else
  echo "   All image tiers ready ✓"
fi

# --- Ensure CloudWatch metric filters for app error metrics ---
# The VM app emits a structured "APP_METRIC {json}" log line (see
# app/platform/errors.py) whenever a handled/unhandled error occurs, and VM logs
# ship to CloudWatch Logs under /aws/lambda-microvms/{IMAGE_BASE_NAME}-{arch}-{mem}.
# Here we (idempotently) attach a metric filter to EACH VM log group so those log
# lines become a real CloudWatch METRIC (namespace MicroVMNotebook, name
# UnhandledErrors) — graphable + alarmable, not just log text. put-metric-filter
# is an upsert, so re-running is safe. Best-effort: any failure is non-fatal (the
# structured log line + the in-app Logs panel still work without the metric).
#
# NOTE — scope is VM-ONLY: the PROXY (proxy/platform/errors.py) emits the SAME
# APP_METRIC lines (incl. the handled "CatalogResyncPushFailed" one), but in this
# local-dev topology the proxy runs as a plain host process logging to a local file
# — its logs are NOT shipped to CloudWatch — so no proxy-side metric filter is
# created here (it would never match). Proxy APP_METRIC lines are observable in the
# proxy's local log; wire a CloudWatch agent / log shipper for the proxy if those
# ever need to be real CloudWatch metrics.
echo ">> Ensuring CloudWatch metric filters (APP_METRIC → UnhandledErrors)..."
METRIC_NS="MicroVMNotebook"
for ARCH in $IMAGE_ARCHES; do
  for MEM in $(image_sizes_for "$ARCH"); do
    LOG_GROUP="/aws/lambda-microvms/${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
    # The platform creates the log group on first VM launch; create it up front
    # (ignore "already exists") so the filter can attach before any VM runs.
    aws logs create-log-group --log-group-name "$LOG_GROUP" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null || true
    # Filter the JSON APP_METRIC lines where metric == UnhandledErrors → count.
    # defaultValue 0 so the metric reports 0 (not a gap) in periods with no errors.
    if aws logs put-metric-filter \
        --log-group-name "$LOG_GROUP" \
        --filter-name "UnhandledErrors" \
        --filter-pattern '{ $.metric = "UnhandledErrors" }' \
        --metric-transformations \
          metricName=UnhandledErrors,metricNamespace="$METRIC_NS",metricValue=1,defaultValue=0 \
        --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null; then
      echo "   ✓ ${IMAGE_BASE_NAME}-${ARCH}-${MEM}"
    else
      echo "   ⚠ metric filter not set for ${IMAGE_BASE_NAME}-${ARCH}-${MEM} (non-fatal)"
    fi
  done
done

# --- Compute effective Aurora Data API values for the proxy (VM data source) ---
# When Aurora is enabled these come from the sourced .aurora_env (or config.sh
# fallback); when DISABLED we force them blank so VMs get "no Aurora data source"
# (a blank cluster ARN is how config.sh documents "no Aurora"). This is what lets
# the solution run without ever pointing at a cluster.
if [ "$AURORA_ENABLED" = "true" ]; then
  EFF_AURORA_CLUSTER_ARN="${AURORA_DATA_API_CLUSTER_ARN:-}"
  EFF_AURORA_SECRET_ARN="${AURORA_DATA_API_SECRET_ARN:-}"
  EFF_AURORA_DATABASE="${AURORA_DATA_API_DATABASE:-}"
else
  EFF_AURORA_CLUSTER_ARN=""
  EFF_AURORA_SECRET_ARN=""
  EFF_AURORA_DATABASE=""
fi

# --- Compute effective Bedrock KB values for the proxy (VM data source) ---
# When the KB is enabled these come from the sourced .kb_env; when DISABLED we
# force them blank so the proxy exposes no Bedrock KB data source (a blank
# BEDROCK_KB_ID is how config.sh documents "no Bedrock KB").
if [ "$KB_ENABLED" = "true" ]; then
  EFF_BEDROCK_KB_ID="${BEDROCK_KB_ID:-}"
  EFF_BEDROCK_KB_INDEX_ARN="${BEDROCK_KB_INDEX_ARN:-}"
else
  EFF_BEDROCK_KB_ID=""
  EFF_BEDROCK_KB_INDEX_ARN=""
fi

# --- Compute effective SageMaker values for the proxy (propagated into VMs) ---
# When the role exists these come from the sourced .sagemaker_env; when it doesn't
# we force the role ARN blank so the proxy propagates "no SageMaker" (a blank role
# ARN is how config.sh documents "SageMaker not configured" → the in-VM sm_* helpers
# raise a clear error). Instance-type defaults are harmless to pass regardless.
if [ "$SAGEMAKER_ENABLED" = "true" ]; then
  EFF_SAGEMAKER_ROLE_ARN="${SAGEMAKER_EXEC_ROLE_ARN:-}"
  # Phase 2 serving image (from .sagemaker_env). Blank when the serving container
  # wasn't built (no container tool / build failed) → the proxy propagates "no image"
  # and sm_deploy_* raises a clear error, while sm_train/predict keep working.
  EFF_SAGEMAKER_SERVING_IMAGE="${SAGEMAKER_SERVING_IMAGE:-}"
else
  EFF_SAGEMAKER_ROLE_ARN=""
  EFF_SAGEMAKER_SERVING_IMAGE=""
fi

# --- Security: loopback bind + per-run shared secret (C1/C2/C3) ---
# Host-side services bind loopback only and authenticate every proxy call with a
# per-run shared secret (sent in the X-Proxy-Auth header). The tokens are minted
# fresh on each install and exported into each service's env block below; they
# never touch disk. openssl is the primary source; fall back to python3 secrets.
_mint_token() {
  openssl rand -hex 32 2>/dev/null \
    || $PYTHON -c 'import secrets;print(secrets.token_hex(32))'
}
PROXY_AUTH_TOKEN="$(_mint_token)"
POOL_AUTH_TOKEN="$(_mint_token)"
PROXY_BIND_HOST="127.0.0.1"
POOL_BIND_HOST="127.0.0.1"
UI_PORT="5173"

# --- Start token proxy ---
echo ""
echo ">> Starting token proxy on http://localhost:$PROXY_PORT"
(cd "$ROOT_DIR" && \
  PROXY_BIND_HOST="$PROXY_BIND_HOST" \
  PROXY_AUTH_TOKEN="$PROXY_AUTH_TOKEN" \
  POOL_AUTH_TOKEN="$POOL_AUTH_TOKEN" \
  UI_PORT="$UI_PORT" \
  MICROVM_IMAGE_ARN="$IMAGE_ARN" \
  MICROVM_EXEC_ROLE_ARN="$EXEC_ROLE_ARN" \
  CATALOG_CACHE_TTL_SECONDS="$CATALOG_CACHE_TTL_SECONDS" \
  IMAGE_BASE_NAME="$IMAGE_BASE_NAME" \
  IMAGE_ARCHES="$IMAGE_ARCHES" \
  DEFAULT_IMAGE_ARCH="$DEFAULT_IMAGE_ARCH" \
  MICROVM_PRICING_JSON="$MICROVM_PRICING_JSON" \
  MICROVM_SIZES_JSON="$MICROVM_SIZES_JSON" \
  AWS_REGION="$AWS_REGION" \
  POLL_INTERVAL_MS="$POLL_INTERVAL_MS" \
  ATHENA_DB="$ATHENA_DB" \
  ATHENA_WORKGROUP="$ATHENA_WORKGROUP" \
  DATASOURCE_ATHENA_DATABASES="$DATASOURCE_ATHENA_DATABASES" \
  DATASOURCE_S3_PREFIXES="$DATASOURCE_S3_PREFIXES" \
  DATASOURCE_S3_DELETABLE_PREFIXES="$DATASOURCE_S3_DELETABLE_PREFIXES" \
  DATASOURCE_DDB_NAME_FILTERS="$DATASOURCE_DDB_NAME_FILTERS" \
  DATASOURCE_CLOUDWATCH_LOG_GROUP_PREFIXES="$DATASOURCE_CLOUDWATCH_LOG_GROUP_PREFIXES" \
  ACCOUNT_ID="$ACCOUNT_ID" \
  S3TABLES_BUCKET="$S3TABLES_BUCKET" \
  S3TABLES_NAMESPACE="$S3TABLES_NAMESPACE" \
  S3TABLES_ATHENA_CATALOG="$S3TABLES_ATHENA_CATALOG" \
  ARTIFACT_BUCKET="$ARTIFACT_BUCKET" \
  STORAGE_BACKEND="$STORAGE_BACKEND" \
  STORAGE_CONNECTION="$STORAGE_CONNECTION" \
  AURORA_DATA_API_CLUSTER_ARN="$EFF_AURORA_CLUSTER_ARN" \
  AURORA_DATA_API_SECRET_ARN="$EFF_AURORA_SECRET_ARN" \
  AURORA_DATA_API_DATABASE="$EFF_AURORA_DATABASE" \
  BEDROCK_KB_ID="$EFF_BEDROCK_KB_ID" \
  BEDROCK_KB_INDEX_ARN="$EFF_BEDROCK_KB_INDEX_ARN" \
  SAGEMAKER_EXEC_ROLE_ARN="$EFF_SAGEMAKER_ROLE_ARN" \
  SAGEMAKER_TRAIN_INSTANCE="${SAGEMAKER_TRAIN_INSTANCE:-ml.m5.xlarge}" \
  SAGEMAKER_TRANSFORM_INSTANCE="${SAGEMAKER_TRANSFORM_INSTANCE:-ml.m5.xlarge}" \
  SAGEMAKER_ARTIFACT_PREFIX="${SAGEMAKER_ARTIFACT_PREFIX:-sagemaker/}" \
  SAGEMAKER_SERVING_IMAGE="$EFF_SAGEMAKER_SERVING_IMAGE" \
  SAGEMAKER_SERVING_REPO="${SAGEMAKER_SERVING_REPO:-microvm-sagemaker-serving}" \
  SAGEMAKER_SERVERLESS_MEMORY_MB="${SAGEMAKER_SERVERLESS_MEMORY_MB:-2048}" \
  SAGEMAKER_SERVERLESS_MAX_CONCURRENCY="${SAGEMAKER_SERVERLESS_MAX_CONCURRENCY:-5}" \
  AI_PORT="$AI_PORT" \
  AGENT_URL="$AGENT_URL" \
  AGENT_CONVERSATION_WINDOW_SIZE="$AGENT_CONVERSATION_WINDOW_SIZE" \
  BEDROCK_MODEL_ID="$BEDROCK_MODEL_ID" \
  BEDROCK_KB_GENERATION_MODEL="$BEDROCK_KB_GENERATION_MODEL" \
  BEDROCK_REGION="$BEDROCK_REGION" \
  DEFAULT_MEMORY_MIB="$DEFAULT_MEMORY_MIB" \
  TOKEN_CACHE_BACKEND="$TOKEN_CACHE_BACKEND" \
  TOKEN_CACHE_MAX_SIZE="$TOKEN_CACHE_MAX_SIZE" \
  TOKEN_CACHE_URL="$TOKEN_CACHE_URL" \
  METRICS_RETENTION_HOURS="$METRICS_RETENTION_HOURS" \
  SESSION_PERSISTENCE_MODE="$SESSION_PERSISTENCE_MODE" \
  MAX_LIFETIME_SECONDS="$MAX_LIFETIME_SECONDS" \
  $PYTHON -m uvicorn proxy.server:app --host "$PROXY_BIND_HOST" --port "$PROXY_PORT" --log-level warning) &
PROXY_PID=$!
sleep 1

# --- Start the VM Pool Manager (opt-in, gated by POOL_ENABLED) ---
# Mirrors the SQLite→Aurora sync worker's opt-in gating below: only start when
# explicitly enabled, same backgrounding/kill/trap pattern as the proxy/agent/
# sync services. With POOL_ENABLED=false (default) nothing here runs and
# nothing changes vs today. With POOL_VM_BACKEND=fake (the current default),
# starting it launches NO real MicroVMs (fake backend is in-memory).
POOL_PID=""
if [ "${POOL_ENABLED:-false}" = "true" ]; then
  echo ""
  # --- Clean slate: terminate pooled MicroVMs FIRST, then wipe pool_vms ---
  # Local dev always starts the pool from a known-empty ledger. The one-shot
  # (pool_manager.reset_local_pool) terminates every pool_vms MicroVM via the
  # real control plane, THEN deletes its row (per-row, so an interrupted run
  # never orphans a live billing VM). It runs in its OWN subshell BEFORE the
  # uvicorn start subshell, so it MUST re-export the backend-selecting env vars
  # itself — a sibling command does NOT inherit the uvicorn subshell's exports,
  # and an unset POOL_VM_BACKEND would silently default to the fake backend and
  # terminate NO real VM. Fail CLOSED: if the reset can't confirm it ran, abort
  # before starting the pool manager rather than start over un-terminated VMs.
  # It then SWEEPS the control plane for ORPHANED spares — live VMs from this
  # app's image family with no pool_vms row, no vm_sessions row (not a notebook
  # VM), older than POOL_ORPHAN_MIN_AGE_SEC (default 600s) — and terminates them.
  # Those are spares whose ledger row was lost (stack killed mid-mint, etc.);
  # without the sweep they bill forever and show up as extra "POOL SPARE" rows.
  echo ">> pool reset: terminating pooled MicroVMs, wiping pool_vms, then sweeping orphans"
  ( cd "$ROOT_DIR" && \
      STORAGE_BACKEND="$STORAGE_BACKEND" \
      STORAGE_CONNECTION="$STORAGE_CONNECTION" \
      POOL_VM_BACKEND="$POOL_VM_BACKEND" \
      AWS_REGION="$AWS_REGION" \
      MICROVM_IMAGE_ARN="$IMAGE_ARN" \
      MICROVM_EXEC_ROLE_ARN="$EXEC_ROLE_ARN" \
      IMAGE_ARCHES="$IMAGE_ARCHES" \
      MICROVM_SIZES_JSON="$MICROVM_SIZES_JSON" \
      $PYTHON -m pool_manager.reset_local_pool ) \
    || { echo "   ✗ pool reset failed — aborting install before starting the pool manager"; exit 1; }

  echo ">> Starting VM Pool Manager on http://localhost:$POOL_MANAGER_PORT (backend=${POOL_VM_BACKEND:-fake})"
  (cd "$ROOT_DIR" && \
    POOL_BIND_HOST="$POOL_BIND_HOST" \
    POOL_AUTH_TOKEN="$POOL_AUTH_TOKEN" \
    POOL_ENABLED="$POOL_ENABLED" \
    POOL_SIZE_PER_IMAGE="$POOL_SIZE_PER_IMAGE" \
    POOL_SIZE_OVERRIDES_ARM64="$POOL_SIZE_OVERRIDES_ARM64" \
    POOL_SIZE_OVERRIDES_X86_64="$POOL_SIZE_OVERRIDES_X86_64" \
    POOL_MAX_AGE_HOURS="$POOL_MAX_AGE_HOURS" \
    POOL_MANAGER_PORT="$POOL_MANAGER_PORT" \
    POOL_FILL_CONCURRENCY="$POOL_FILL_CONCURRENCY" \
    POOL_MINT_HEALTH_TIMEOUT_SECONDS="$POOL_MINT_HEALTH_TIMEOUT_SECONDS" \
    POOL_VM_BACKEND="$POOL_VM_BACKEND" \
    IMAGE_ARCHES="$IMAGE_ARCHES" \
    MICROVM_SIZES_JSON="$MICROVM_SIZES_JSON" \
    DEFAULT_MEMORY_MIB="$DEFAULT_MEMORY_MIB" \
    STORAGE_BACKEND="$STORAGE_BACKEND" \
    STORAGE_CONNECTION="$STORAGE_CONNECTION" \
    AWS_REGION="$AWS_REGION" \
    MICROVM_IMAGE_ARN="$IMAGE_ARN" \
    MICROVM_EXEC_ROLE_ARN="$EXEC_ROLE_ARN" \
    $PYTHON -m uvicorn pool_manager.server:app --host "$POOL_BIND_HOST" --port "$POOL_MANAGER_PORT" --log-level warning) &
  POOL_PID=$!
  sleep 1

  # --- Wait for the pool to fill, then report VMs-per-image-size ---
  # Poll /status until every tier reaches its target (actual >= target) or we
  # hit the budget. Each spare is a real MicroVM boot (~10-15s) minted up to
  # POOL_FILL_CONCURRENCY at a time, so give it a generous bound; never hang the
  # install — on timeout we print whatever filled so far and move on.
  echo ">> Waiting for the VM pool to fill (backend=${POOL_VM_BACKEND:-fake})..."
  POOL_READY_DEADLINE=$(( $(date +%s) + 180 ))
  while :; do
    POOL_STATUS=$(curl -s --max-time 5 "http://localhost:${POOL_MANAGER_PORT}/status" 2>/dev/null || echo "")
    if [ -n "$POOL_STATUS" ]; then
      # all_ready=1 only if every tier has actual >= target
      ALL_READY=$(echo "$POOL_STATUS" | $PYTHON -c "
import sys, json
try:
    d = json.load(sys.stdin)
    tiers = d.get('tiers', {})
    print('1' if tiers and all(t.get('actual',0) >= t.get('target',0) for t in tiers.values()) else '0')
except Exception:
    print('0')
" 2>/dev/null || echo "0")
      [ "$ALL_READY" = "1" ] && break
    fi
    [ "$(date +%s)" -ge "$POOL_READY_DEADLINE" ] && { echo "   ⚠ pool not fully filled within 180s — continuing (it will keep filling in the background)"; break; }
    sleep 5
  done

  # Print the final per-tier VM counts (available/granted/etc. per image size).
  POOL_STATUS=$(curl -s --max-time 5 "http://localhost:${POOL_MANAGER_PORT}/status" 2>/dev/null || echo "")
  if [ -n "$POOL_STATUS" ]; then
    echo "$POOL_STATUS" | $PYTHON -c "
import sys, json
d = json.load(sys.stdin)
tiers = d.get('tiers', {})
total = sum(t.get('actual', 0) for t in tiers.values())
print(f'>> VM Pool Manager READY (backend={d.get(\"backend\")}): {total} warm VM(s) across {len(tiers)} image tier(s)')
for key in sorted(tiers, key=lambda k: (tiers[k].get('arch', ''), tiers[k].get('size_mib', 0))):
    t = tiers[key]
    counts = ', '.join(f'{k}={v}' for k, v in sorted(t.get('counts', {}).items()))
    print(f'     {t[\"arch\"]}-{t[\"size_mib\"]}MiB: {t.get(\"actual\",0)}/{t.get(\"target\",0)} spare(s)' + (f'  ({counts})' if counts else ''))
" 2>/dev/null || echo "   (could not parse /status — check http://localhost:${POOL_MANAGER_PORT}/status)"
  else
    echo "   ⚠ pool /status not reachable on :${POOL_MANAGER_PORT}"
  fi
else
  echo ""
  echo ">> VM Pool Manager: skipped (POOL_ENABLED=false — set POOL_ENABLED=true to start it)"
  # --- Clean up any orphaned pool VMs from a PRIOR --WarmPool run ---------------
  # Stopping the pool manager does NOT terminate its spares, and the clean-slate
  # reset above only runs when POOL_ENABLED=true. So a plain (no-pool) start would
  # otherwise leave earlier warm-pool spares running — suspended VMs that keep
  # costing money and show up as stale "POOL SPARE" rows. Run the SAME one-shot
  # here to terminate them + wipe pool_vms. It no-ops when pool_vms is empty.
  # IMPORTANT: force the REAL aws backend (reset defaults to fake, which would
  # terminate nothing); only meaningful on the aws backend, so gate on it. Do NOT
  # fail-closed here — the pool isn't in use this run, so a cleanup hiccup should
  # warn, not abort the whole install.
  if [ "${POOL_VM_BACKEND:-fake}" = "aws" ]; then
    echo ">> pool cleanup: terminating any orphaned warm-pool VMs from a prior run"
    ( cd "$ROOT_DIR" && \
        STORAGE_BACKEND="$STORAGE_BACKEND" \
        STORAGE_CONNECTION="$STORAGE_CONNECTION" \
        POOL_VM_BACKEND="aws" \
        AWS_REGION="$AWS_REGION" \
        MICROVM_IMAGE_ARN="$IMAGE_ARN" \
        MICROVM_EXEC_ROLE_ARN="$EXEC_ROLE_ARN" \
        IMAGE_ARCHES="$IMAGE_ARCHES" \
        MICROVM_SIZES_JSON="$MICROVM_SIZES_JSON" \
        $PYTHON -m pool_manager.reset_local_pool ) \
      || echo "   ⚠ pool cleanup had a problem (continuing; pool is off this run)"
  fi
fi

# --- Start the AI chat agent (extracted BedrockAgentCoreApp service) ---
# The interactive Strands chat agent runs as its OWN local process (ai_assistant/agent_runtime.py)
# on $AI_PORT, exposing the AgentCore contract (POST /invocations + GET /ping). The proxy's
# /ai/chat forwards to it. Same artifact we later deploy to AWS Bedrock AgentCore Runtime.
# It reaches VMs via the proxy's existing HTTP endpoints (PROXY_PORT) and calls Bedrock directly.
echo ">> Starting AI chat agent on http://localhost:${AI_PORT:-8082}"
# NOTE: we `cd agent/` so `ai_assistant` resolves as a top-level package, but the
# LOCAL SQLite memory path imports `proxy.storage` (agent/ai_assistant/memory.py),
# and proxy/ lives at the repo ROOT — one level ABOVE this cwd. Without the repo root
# on PYTHONPATH that import fails with "No module named 'proxy'" and durable chat
# history is silently disabled (durable=no) for EVERY session. So put $ROOT_DIR on
# PYTHONPATH here. (The AgentCore CONTAINER deliberately runs WITHOUT proxy/ and uses
# MEMORY_BACKEND=agentcore instead — that path never imports proxy.storage.)
(cd "$ROOT_DIR/agent" && \
  AI_PORT="${AI_PORT:-8082}" \
  PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  PROXY_PORT="$PROXY_PORT" \
  PROXY_AUTH_TOKEN="$PROXY_AUTH_TOKEN" \
  BEDROCK_MODEL_ID="$BEDROCK_MODEL_ID" \
  BEDROCK_KB_GENERATION_MODEL="$BEDROCK_KB_GENERATION_MODEL" \
  BEDROCK_REGION="$BEDROCK_REGION" \
  AI_AGENT_AUTO_EXECUTE="$AI_AGENT_AUTO_EXECUTE" \
  AGENT_CONVERSATION_WINDOW_SIZE="$AGENT_CONVERSATION_WINDOW_SIZE" \
  AWS_REGION="$AWS_REGION" \
  ATHENA_DB="$ATHENA_DB" \
  ATHENA_WORKGROUP="$ATHENA_WORKGROUP" \
  ARTIFACT_BUCKET="$ARTIFACT_BUCKET" \
  STORAGE_BACKEND="$STORAGE_BACKEND" \
  STORAGE_CONNECTION="$STORAGE_CONNECTION" \
  MEMORY_BACKEND="$MEMORY_BACKEND" \
  AGENTCORE_MEMORY_ID="$AGENTCORE_MEMORY_ID" \
  AGENTCORE_ACTOR_ID="$AGENTCORE_ACTOR_ID" \
  BEDROCK_KB_ID="$EFF_BEDROCK_KB_ID" \
  BEDROCK_KB_INDEX_ARN="$EFF_BEDROCK_KB_INDEX_ARN" \
  $PYTHON -m ai_assistant.agent_runtime) &
AGENT_PID=$!

# Health check: poll the agent's GET /ping until it responds (uvicorn takes a
# moment to bind). NON-FATAL — if it doesn't come up we warn and continue so the
# agent can never take down the proxy/UI (same guard philosophy as the sync
# worker). If the agent is down, the proxy's /ai/chat forwarder degrades with a
# clear "AI agent is not reachable" message rather than crashing.
AGENT_URL_LOCAL="http://localhost:${AI_PORT:-8082}"
agent_ready=false
for _try in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s -m 2 "$AGENT_URL_LOCAL/ping" >/dev/null 2>&1; then
    agent_ready=true
    break
  fi
  sleep 1
done
if [ "$agent_ready" = "true" ]; then
  echo "   AI chat agent healthy (GET /ping) ✓"
else
  echo "   ⚠ AI chat agent did not answer /ping in time — continuing anyway."
  echo "     Chat will show 'AI agent is not reachable' until it's up; check its logs."
fi

# --- Start frontend ---
echo ">> Starting notebook UI on http://localhost:5173"
(cd "$ROOT_DIR/web" && VITE_PROXY_PORT="$PROXY_PORT" VITE_PROXY_TOKEN="$PROXY_AUTH_TOKEN" npm run dev -- --open) &
FRONTEND_PID=$!

# --- Start the LOCAL-DEV-ONLY SQLite -> Aurora sync worker ---
# Standalone, throwaway bridge (sync/sync_to_aurora.py — NOT part of the proxy):
# replays local SQLite writes/deletes into Aurora over the RDS Data API so the
# notebook-facing v_nb_* views reflect real local activity. Started LAST, after
# the proxy + UI are up. Guarded three ways so it can NEVER take down the app:
#   • only launches when SQLite is the source AND a RW Data API secret is set
#     (STORAGE_BACKEND=postgres means the proxy writes Aurora directly — no sync);
#   • runs in its own subshell in the background;
#   • a crash/exit of the worker does not signal the proxy/UI (we don't `wait` on it).
SYNC_PID=""
if [ "$AURORA_ENABLED" = "true" ] && [ "${STORAGE_BACKEND:-sqlite}" = "sqlite" ] && [ -n "${AURORA_DATA_API_RW_SECRET_ARN:-}" ]; then
  echo ">> Starting SQLite→Aurora sync worker (interval ${AURORA_SYNC_INTERVAL:-60}s)"
  (cd "$ROOT_DIR" && \
    AWS_REGION="$AWS_REGION" \
    AURORA_DATA_API_CLUSTER_ARN="$AURORA_DATA_API_CLUSTER_ARN" \
    AURORA_DATA_API_RW_SECRET_ARN="$AURORA_DATA_API_RW_SECRET_ARN" \
    AURORA_DATA_API_DATABASE="$AURORA_DATA_API_DATABASE" \
    $PYTHON -m sync.sync_to_aurora --interval "${AURORA_SYNC_INTERVAL:-60}") &
  SYNC_PID=$!
elif [ "$AURORA_ENABLED" != "true" ]; then
  echo ">> SQLite→Aurora sync worker: skipped (Aurora disabled — no --AuroraSetup/--AuroraReset)"
else
  echo ">> SQLite→Aurora sync worker: skipped (STORAGE_BACKEND=${STORAGE_BACKEND:-sqlite}, RW secret $( [ -n "${AURORA_DATA_API_RW_SECRET_ARN:-}" ] && echo set || echo unset ))"
fi

echo ""
echo "============================================"
echo "  ✅ All running!"
echo ""
echo "  Notebook UI:    http://localhost:5173"
echo "  Token Proxy:    http://localhost:$PROXY_PORT"
echo "  AI Chat Agent:  http://localhost:${AI_PORT:-8082} (BedrockAgentCoreApp — /invocations, /ping)"
echo "  MicroVM Image:  $IMAGE_ARN"
if [ -n "${SYNC_PID:-}" ]; then
  echo "  Aurora Sync:    SQLite→Aurora every ${AURORA_SYNC_INTERVAL:-60}s (local-dev bridge)"
fi
if [ -n "${POOL_PID:-}" ]; then
  echo "  VM Pool Manager: http://localhost:$POOL_MANAGER_PORT"
fi
echo ""
echo "  New notebook tabs auto-launch their own MicroVM."
echo "  Closing a tab suspends its MicroVM (reopen resumes it)."
echo ""
echo "  Press Ctrl+C to stop proxy & UI"
echo "============================================"
echo ""

wait
