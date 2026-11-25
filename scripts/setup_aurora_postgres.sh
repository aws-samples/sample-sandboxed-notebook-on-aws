#!/usr/bin/env bash
# ============================================================
# Provision an Aurora PostgreSQL cluster for the proxy DB, create two
# least-privilege roles (read-write for the proxy, read-only for MicroVMs via
# the Data API), and create all proxy tables/indexes/views.
#
# ALL in-database work (roles, grants, schema DDL, view grants) runs over the RDS
# Data API (rds-data ExecuteStatement, HTTPS/443) — never psycopg2/5432 — so this
# script works from anywhere the AWS control plane is reachable, including a laptop
# against a private cluster. The schema DDL is the SAME statement list the
# proxy runs at startup (proxy.storage.postgres_db.schema_statements()).
#
# The READ-ONLY role is scoped to the curated v_nb_* VIEWS ONLY — it has NO
# SELECT on the base tables, so the notebook/agent can never read raw cell code,
# outputs, or errors, only the curated (sensitive-body-excluded) views.
#
# Idempotent: safe to re-run. Existing cluster/instance/secrets/roles/database
# are reused (roles get their password re-synced from the secret).
#
# What this does NOT do: it does not edit config.sh for you. After it finishes it
# prints the exact STORAGE_* (proxy) and AURORA_DATA_API_* (MicroVM) lines to set.
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

# --- Resolve a Python 3 interpreter (same logic as local_install.sh) ---
# Prefer the interpreter local_install.sh exported ($PYTHON → the project .venv) so
# this subscript uses the SAME env the deps were installed into. Only fall back to a
# host python3 when run standalone (no inherited $PYTHON).
if [ -n "${PYTHON:-}" ] && "$PYTHON" --version &>/dev/null; then
  :
elif command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null && [[ "$(python --version 2>&1)" == *"3."* ]]; then
  PYTHON=python
else
  echo "❌ Python 3 not found. Install python3 or ensure 'python' points to Python 3."
  exit 1
fi

# --- jq is required for building/reading the JSON secret ---
if ! command -v jq &>/dev/null; then
  echo "❌ jq not found. Install it (macOS: brew install jq; Debian/Ubuntu: apt-get install jq)."
  exit 1
fi

# --- Helper: call rds with profile + region (mirrors aws_mvm in config.sh) ---
aws_rds() {
  aws rds "$@" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION"
}

echo "============================================"
echo "  Aurora PostgreSQL — Proxy DB Provisioning"
echo "============================================"
echo "  Account:        $ACCOUNT_ID"
echo "  Region:         $AWS_REGION"
echo "  Cluster:        $AURORA_CLUSTER_ID"
echo "  Database:       $AURORA_DB_NAME"
echo "  Master user:    $AURORA_MASTER_USERNAME"
echo "  Engine version: $AURORA_ENGINE_VERSION"
echo "  Instance class: $AURORA_INSTANCE_CLASS"
echo "  Public access:  $AURORA_PUBLICLY_ACCESSIBLE"
echo "  Secrets:        $AURORA_SECRET_NAME (master), $AURORA_RW_SECRET_NAME (rw), $AURORA_RO_SECRET_NAME (ro)"
echo ""

WRITER_INSTANCE_ID="${AURORA_CLUSTER_ID}-writer-1"

# ------------------------------------------------------------
# 1. Credential secrets (master / read-write / read-only) in Secrets Manager, so
#    reruns are idempotent and passwords are never echoed. Created with generated
#    values if absent; read back otherwise.
# ------------------------------------------------------------
# Each credential is stored as RDS-shaped JSON: {"username":...,"password":...}.
# That JSON shape is what the Data API requires; the proxy (psycopg2) reads the
# password field out of it. There are THREE: master (bootstrap-only), read-write
# (proxy), read-only (VM Data API).
#
# ensure_secret <secret_name> <username> — create with a generated password if
# absent (idempotent), then echo the password to stdout. Never prints the value.
ensure_secret() {
  local name="$1" user="$2"
  if ! aws secretsmanager describe-secret \
        --secret-id "$name" \
        --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" &>/dev/null; then
    local gen json
    gen=$(aws secretsmanager get-random-password \
      --exclude-punctuation --password-length 32 \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
      --query 'RandomPassword' --output text)
    json=$(jq -nc --arg u "$user" --arg p "$gen" '{username:$u, password:$p}')
    aws secretsmanager create-secret \
      --name "$name" \
      --description "Aurora credential ($user) for the MicroVM proxy DB ($AURORA_CLUSTER_ID)" \
      --secret-string "$json" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  fi
  # Read back + extract the password (tolerate a legacy bare-string secret).
  local s pw
  s=$(aws secretsmanager get-secret-value \
    --secret-id "$name" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
    --query 'SecretString' --output text)
  pw=$(printf '%s' "$s" | jq -r '.password // empty' 2>/dev/null || true)
  [ -z "$pw" ] && pw="$s"
  printf '%s' "$pw"
}

echo ">> Ensuring credential secrets (master / read-write / read-only)"
MASTER_PASSWORD=$(ensure_secret "$AURORA_SECRET_NAME" "$AURORA_MASTER_USERNAME")
RW_PASSWORD=$(ensure_secret "$AURORA_RW_SECRET_NAME" "$AURORA_RW_USERNAME")
RO_PASSWORD=$(ensure_secret "$AURORA_RO_SECRET_NAME" "$AURORA_RO_USERNAME")
echo "   master / $AURORA_RW_USERNAME / $AURORA_RO_USERNAME secrets ready ✓"

# ------------------------------------------------------------
# 2. Aurora DB cluster (engine=aurora-postgresql). Reused if it already exists.
# ------------------------------------------------------------
echo ">> Ensuring Aurora cluster: $AURORA_CLUSTER_ID"
if aws_rds describe-db-clusters --db-cluster-identifier "$AURORA_CLUSTER_ID" &>/dev/null; then
  echo "   (cluster already exists — reusing)"
else
  echo "   Creating cluster (engine aurora-postgresql $AURORA_ENGINE_VERSION)"
  CREATE_ARGS=(
    --db-cluster-identifier "$AURORA_CLUSTER_ID"
    --engine aurora-postgresql
    --engine-version "$AURORA_ENGINE_VERSION"
    --master-username "$AURORA_MASTER_USERNAME"
    --master-user-password "$MASTER_PASSWORD"
    --database-name "$AURORA_DB_NAME"
    # Enable the RDS Data API so MicroVMs can query over HTTPS/443 (their only
    # reachable path). Harmless for the proxy, which uses psycopg2/5432.
    --enable-http-endpoint
  )
  # Serverless v2 needs a capacity range on the cluster.
  if [ "$AURORA_INSTANCE_CLASS" = "db.serverless" ]; then
    CREATE_ARGS+=(--serverless-v2-scaling-configuration "MinCapacity=${AURORA_MIN_ACU},MaxCapacity=${AURORA_MAX_ACU}")
  fi
  aws_rds create-db-cluster "${CREATE_ARGS[@]}" >/dev/null
fi

# Ensure the Data API (HTTP endpoint) is ON — covers a pre-existing cluster that
# was created without it. Idempotent: only modifies when currently disabled.
HTTP_ENDPOINT_ENABLED=$(aws_rds describe-db-clusters \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --query 'DBClusters[0].HttpEndpointEnabled' --output text 2>/dev/null || echo "False")
if [ "$HTTP_ENDPOINT_ENABLED" != "True" ]; then
  echo ">> Enabling RDS Data API (HTTP endpoint) on the cluster"
  aws_rds enable-http-endpoint \
    --resource-arn "$(aws_rds describe-db-clusters --db-cluster-identifier "$AURORA_CLUSTER_ID" --query 'DBClusters[0].DBClusterArn' --output text)" \
    >/dev/null 2>&1 || \
  aws_rds modify-db-cluster \
    --db-cluster-identifier "$AURORA_CLUSTER_ID" \
    --enable-http-endpoint --apply-immediately >/dev/null
else
  echo "   Data API already enabled ✓"
fi

# ------------------------------------------------------------
# 3. Writer instance in the cluster. Reused if it already exists.
# ------------------------------------------------------------
echo ">> Ensuring writer instance: $WRITER_INSTANCE_ID"
if aws_rds describe-db-instances --db-instance-identifier "$WRITER_INSTANCE_ID" &>/dev/null; then
  echo "   (instance already exists — reusing)"
else
  echo "   Creating writer instance ($AURORA_INSTANCE_CLASS)"
  PUBLIC_FLAG="--no-publicly-accessible"
  if [ "$AURORA_PUBLICLY_ACCESSIBLE" = "true" ]; then
    PUBLIC_FLAG="--publicly-accessible"
  fi
  aws_rds create-db-instance \
    --db-instance-identifier "$WRITER_INSTANCE_ID" \
    --db-cluster-identifier "$AURORA_CLUSTER_ID" \
    --engine aurora-postgresql \
    --db-instance-class "$AURORA_INSTANCE_CLASS" \
    $PUBLIC_FLAG >/dev/null
fi

# ------------------------------------------------------------
# 4. Wait until the writer instance is available (endpoint reachable).
# ------------------------------------------------------------
echo ">> Waiting for the writer instance to become available (this can take several minutes)..."
aws_rds wait db-instance-available --db-instance-identifier "$WRITER_INSTANCE_ID"
echo "   Instance available ✓"

# ------------------------------------------------------------
# 5. Resolve the cluster writer endpoint + port.
# ------------------------------------------------------------
CLUSTER_ENDPOINT=$(aws_rds describe-db-clusters \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --query 'DBClusters[0].Endpoint' --output text)
CLUSTER_PORT=$(aws_rds describe-db-clusters \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --query 'DBClusters[0].Port' --output text)
echo "   Writer endpoint: ${CLUSTER_ENDPOINT}:${CLUSTER_PORT}"

# Cluster ARN (resourceArn for the Data API). The Data API secretArn is the
# READ-ONLY secret (so notebooks authenticate as the SELECT-only user).
CLUSTER_ARN=$(aws_rds describe-db-clusters \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --query 'DBClusters[0].DBClusterArn' --output text)
RO_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "$AURORA_RO_SECRET_NAME" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
  --query 'ARN' --output text)
# The READ-WRITE secret ARN feeds the LOCAL-DEV-ONLY SQLite→Aurora sync worker
# (sync/sync_to_aurora.py), which writes base tables over the Data API and so
# needs the RW user (the RO secret is SELECT-only on v_nb_* views).
RW_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "$AURORA_RW_SECRET_NAME" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
  --query 'ARN' --output text)
echo "   Cluster ARN:      ${CLUSTER_ARN}"
echo "   RO secret ARN:    ${RO_SECRET_ARN}"
echo "   RW secret ARN:    ${RW_SECRET_ARN}"

# Proxy DSN uses the READ-WRITE user. Password is URL-encoded in Python below to
# avoid shell/URL escaping pitfalls; here we build a display DSN with the password
# masked for logs.
DSN_DISPLAY="postgresql://${AURORA_RW_USERNAME}:****@${CLUSTER_ENDPOINT}:${CLUSTER_PORT}/${AURORA_DB_NAME}"

# ------------------------------------------------------------
# 6. Bootstrap the two least-privilege roles (as master), then create the proxy
#    schema (as the read-write user, so it OWNS the tables and the read-only
#    default-privileges apply to everything the proxy creates now and later).
#
#    ALL of this runs over the RDS DATA API (rds-data ExecuteStatement, HTTPS/443)
#    — NOT psycopg2/5432 — so it works from anywhere the AWS control plane is
#    reachable, INCLUDING a laptop against a private cluster. This is what makes a
#    fresh provision and --AuroraReset work locally.
# ------------------------------------------------------------
echo ">> Bootstrapping roles + creating schema (over the RDS Data API / 443)"
cd "$ROOT_DIR"

# Resolve the MASTER secret ARN (role bootstrap runs AS master). The RW/RO secret
# ARNs were resolved above ($RW_SECRET_ARN / $RO_SECRET_ARN); the cluster ARN is
# $CLUSTER_ARN. All three secrets already store {"username","password"} in the
# RDS-shaped JSON the Data API needs.
MASTER_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "$AURORA_SECRET_NAME" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
  --query 'ARN' --output text)

# Pass ARNs/names via env (no passwords — the Data API reads them from Secrets
# Manager itself, so no credential ever lands in argv or this process list).
AURORA_REGION="$AWS_REGION" \
AURORA_CLUSTER_ARN="$CLUSTER_ARN" \
AURORA_DB_NAME="$AURORA_DB_NAME" \
AURORA_MASTER_SECRET_ARN="$MASTER_SECRET_ARN" \
AURORA_RW_SECRET_ARN="$RW_SECRET_ARN" \
AURORA_RO_SECRET_ARN="$RO_SECRET_ARN" \
AURORA_RW_USERNAME="$AURORA_RW_USERNAME" \
AURORA_RO_USERNAME="$AURORA_RO_USERNAME" \
AURORA_MASTER_USERNAME="$AURORA_MASTER_USERNAME" \
AURORA_RESET="${AURORA_RESET:-false}" \
$PYTHON - <<'PYEOF'
import os
import sys
import boto3

region      = os.environ["AURORA_REGION"]
cluster_arn = os.environ["AURORA_CLUSTER_ARN"]
db          = os.environ["AURORA_DB_NAME"]

master_secret = os.environ["AURORA_MASTER_SECRET_ARN"]
rw_secret     = os.environ["AURORA_RW_SECRET_ARN"]
ro_secret     = os.environ["AURORA_RO_SECRET_ARN"]

rw_user     = os.environ["AURORA_RW_USERNAME"]
ro_user     = os.environ["AURORA_RO_USERNAME"]
master_user = os.environ["AURORA_MASTER_USERNAME"]

import time

rds = boto3.client("rds-data", region_name=region)

# Serverless v2 with min ACU=0 auto-pauses when idle; the FIRST Data API call
# after a pause returns DatabaseResumingException while the instance wakes. Retry
# just that race with linear backoff (mirrors the sync worker's _call). Any other
# error propagates immediately.
_RESUME_MAX_ATTEMPTS = 10
_RESUME_BACKOFF_BASE = 3.0  # seconds; delay = base * attempt (3,6,9,... ~ up to ~2.5min)


def qi(ident: str) -> str:
    """Quote a Postgres identifier (double-quote, escape embedded double-quotes).
    The Data API has no identifier-binding, so we quote defensively ourselves.
    Role/DB/view names here are all internal config values, not user input."""
    return '"' + ident.replace('"', '""') + '"'


def ql(literal: str) -> str:
    """Quote a Postgres string literal (single-quote, double embedded quotes).
    Used for role PASSWORD, which is not a bindable Data API parameter. Generated
    passwords use --exclude-punctuation (alphanumeric), so there's nothing to
    escape in practice; we escape anyway for safety."""
    return "'" + literal.replace("'", "''") + "'"


def secret_password(secret_arn: str) -> str:
    """Fetch the password out of an RDS-shaped {"username","password"} secret."""
    import json
    sm = boto3.client("secretsmanager", region_name=region)
    s = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    try:
        return json.loads(s).get("password", s)
    except Exception:
        return s


def run(secret_arn: str, statement: str, *, quiet_errors: bool = False):
    """Execute ONE SQL statement over the Data API as the user in `secret_arn`.
    The Data API runs exactly one statement per call (no multi-statement scripts),
    which matches how schema_statements() is authored (one DDL per entry).

    Retries ONLY the auto-pause resume race (DatabaseResumingException) with linear
    backoff, so a cold serverless instance can't fail the first DDL call. Any other
    error propagates (or is swallowed when quiet_errors=True for best-effort revokes)."""
    last_err = None
    for attempt in range(_RESUME_MAX_ATTEMPTS):
        try:
            return rds.execute_statement(
                resourceArn=cluster_arn,
                secretArn=secret_arn,
                database=db,
                sql=statement,
            )
        except rds.exceptions.DatabaseResumingException as e:
            last_err = e
            if attempt < _RESUME_MAX_ATTEMPTS - 1:
                delay = _RESUME_BACKOFF_BASE * (attempt + 1)
                print(f"   Aurora resuming from auto-pause; retry "
                      f"{attempt + 1}/{_RESUME_MAX_ATTEMPTS} in {delay:.0f}s")
                time.sleep(delay)
        except Exception as e:  # noqa: BLE001
            if quiet_errors:
                print(f"   (skipped: {str(e).strip().splitlines()[0]})")
                return None
            raise
    # Exhausted resume retries.
    raise last_err


def query_rows(secret_arn: str, statement: str):
    """Run a SELECT and return the first column of each row as a list of str."""
    resp = run(secret_arn, statement)
    rows = resp.get("records", []) if resp else []
    out = []
    for r in rows:
        cell = r[0] if r else {}
        out.append(cell.get("stringValue", ""))
    return out


# --- Phase 1: bootstrap roles + grants AS MASTER (idempotent) ---
# Every statement is a single ExecuteStatement over the Data API, authenticated
# with the MASTER secret. Role create/alter and grants are all safe to re-run.
rw_pw = secret_password(rw_secret)
ro_pw = secret_password(ro_secret)

for role, pw in ((rw_user, rw_pw), (ro_user, ro_pw)):
    # CREATE ROLE ... LOGIN if absent; always (re)set the password to match the
    # secret (so a rotated secret re-syncs the DB on rerun). We can't read/branch
    # cheaply in one call, so: try CREATE; if it already exists, ALTER instead.
    exists = query_rows(master_secret,
                        f"SELECT 1 FROM pg_roles WHERE rolname = {ql(role)}")
    if exists:
        run(master_secret, f"ALTER ROLE {qi(role)} WITH LOGIN PASSWORD {ql(pw)}")
    else:
        run(master_secret, f"CREATE ROLE {qi(role)} WITH LOGIN PASSWORD {ql(pw)}")

dbid = qi(db)
rw = qi(rw_user)
ro = qi(ro_user)
master = qi(master_user)

# On Aurora the master is NOT a Postgres superuser, so to manage objects/default-
# privileges owned BY the rw/ro roles the master must be a MEMBER of those roles.
run(master_secret, f"GRANT {rw} TO {master}")
run(master_secret, f"GRANT {ro} TO {master}")

# Read-write: connect + full rights on public. It OWNS the tables it creates in
# Phase 2 (they are created with the RW secret, so RW is the owner).
run(master_secret, f"GRANT CONNECT ON DATABASE {dbid} TO {rw}")
run(master_secret, f"GRANT USAGE, CREATE ON SCHEMA public TO {rw}")
run(master_secret, f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {rw}")

# Read-only: connect + USAGE on the schema so it can RESOLVE the curated views.
# Deliberately NO table-level SELECT and NO default privileges — the RO user (the
# notebook/agent over the Data API) must see ONLY the curated v_nb_* views, never
# the base tables. Per-view SELECT grants happen in Phase 2.5 (after the views exist).
run(master_secret, f"GRANT CONNECT ON DATABASE {dbid} TO {ro}")
run(master_secret, f"GRANT USAGE ON SCHEMA public TO {ro}")

# Defensively REVOKE any broad table access a PRIOR run (an older version granted
# SELECT ON ALL TABLES + default privileges) may have left on the RO role, so
# re-running tightens an existing cluster to views-only. Best-effort: a benign
# failure (nothing to revoke) must not abort the run.
run(master_secret,
    f"REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM {ro}", quiet_errors=True)
run(master_secret,
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {rw} IN SCHEMA public REVOKE SELECT ON TABLES FROM {ro}",
    quiet_errors=True)
print("   Roles bootstrapped + grants applied ✓ (RO tightened to views-only)")

# --- Phase 1.5: (RESET ONLY) DROP the existing schema AS THE READ-WRITE USER ---
# DESTRUCTIVE; runs ONLY when AURORA_RESET=true (local_install.sh --AuroraReset).
# Drops every curated v_nb_* view and every public BASE TABLE (CASCADE) so Phase 2
# recreates them exactly as defined in postgres_db.py with no stale-column drift.
# Cluster/roles/secrets untouched. Data is disposable (repopulated from SQLite by
# the sync worker).
#
# DEFAULT (AURORA_RESET=false): SKIP the drop. Phase 2 is create-if-missing
# (CREATE ... IF NOT EXISTS), so an existing schema + its data is preserved.
reset = os.environ.get("AURORA_RESET", "false").lower() == "true"
if reset:
    views = query_rows(rw_secret, "SELECT viewname FROM pg_views WHERE schemaname='public'")
    for v in views:
        run(rw_secret, f"DROP VIEW IF EXISTS {qi(v)} CASCADE")
    tables = query_rows(rw_secret, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    for t in tables:
        run(rw_secret, f"DROP TABLE IF EXISTS {qi(t)} CASCADE")
    print(f"   RESET: dropped existing schema: {len(views)} views + {len(tables)} tables ✓")
else:
    print("   Preserving existing schema (idempotent provision; --AuroraReset to drop+recreate)")

# --- Phase 2: create the proxy schema AS THE READ-WRITE USER ---
# Reuse the SAME DDL the proxy runs at startup: schema_statements() returns the
# ordered list of single CREATE TABLE/INDEX/VIEW (+ additive ALTER) statements
# from proxy.storage.postgres_db. Importing the module does NOT require psycopg2 (that import
# lives inside PostgresStorage.initialize(), not at module load). Each statement
# is one ExecuteStatement over the Data API, run with the RW secret so RW owns
# every object. IF NOT EXISTS makes this safe on an already-populated schema;
# the v_nb_* views are CREATE OR REPLACE, so a view change that ONLY appends
# columns at the end is applied here without a reset and base-table data is
# untouched. A view that DROPS or reorders columns cannot be CREATE OR REPLACE'd
# over the old definition (Postgres rejects it), and a base-table shape change is
# not applied by IF NOT EXISTS: those rollouts need --AuroraReset. The notebook
# DAG change is one of them (v_nb_cells loses the dataflow columns, notebook_cells
# drops them, notebook_dag is new) — roll it out with --AuroraReset. Chart
# persistence is another: notebook_cells gains image_s3_key (no ALTER; the
# proxy's initialize() fails fast on an old table) and v_nb_cells gains a
# trailing has_image flag (the key itself is not exposed) — --AuroraReset.
# Phase 2.5 below re-grants RO SELECT on the views (CREATE OR REPLACE also
# preserves existing grants; whole-view SELECT covers new columns).
from proxy.storage.postgres_db import schema_statements, view_names  # noqa: E402
SCHEMA_STATEMENTS = schema_statements()
for stmt in SCHEMA_STATEMENTS:
    run(rw_secret, stmt)
print(f"   Schema ensured over Data API ({len(SCHEMA_STATEMENTS)} statements) as read-write user ✓")

# --- Phase 2.5: grant the READ-ONLY role SELECT on ONLY the curated views ---
# Views now exist. A Postgres view runs with its OWNER's privileges, so RO reading
# a v_nb_* view needs NO base-table grant — that is what makes "views-only"
# enforceable. Grant per-view SELECT as the RW owner. Idempotent.
#
# The curated set is DERIVED from the DDL (every v_nb_* view postgres_db.py
# creates), so a new curated view is granted without editing this script.
CURATED_VIEWS = [v for v in view_names() if v.startswith("v_nb_")]
present = set(query_rows(
    rw_secret,
    "SELECT viewname FROM pg_views WHERE schemaname = 'public' AND viewname LIKE 'v_nb_%'"))
missing = [v for v in CURATED_VIEWS if v not in present]
if missing:
    print(f"   ERROR: expected curated views missing: {missing}")
    sys.exit(1)
for v in CURATED_VIEWS:
    run(rw_secret, f"GRANT SELECT ON {qi(v)} TO {ro}")
print(f"   Granted RO SELECT on {len(CURATED_VIEWS)} curated views ✓")
PYEOF

# ------------------------------------------------------------
# 6.5 Emit a machine-readable env file for local_install.sh to source.
# ------------------------------------------------------------
# The ARNs resolved above (cluster + RO/RW secrets + DB name) are the source of
# truth for a freshly-provisioned OR pre-existing cluster. Write them to a
# gitignored, sourceable file so local_install.sh can pick them up without the
# hardcoded config.sh defaults — this is the "create it, capture the ARN, pass it
# down to the services that need it" handoff. Contains only ARNs/names (no
# secrets/passwords), but it's gitignored regardless.
AURORA_ENV_FILE="${AURORA_ENV_FILE:-$ROOT_DIR/scripts/.aurora_env}"
cat > "$AURORA_ENV_FILE" <<ENVEOF
# Auto-generated by scripts/setup_aurora_postgres.sh — do not edit by hand.
# Sourced by local_install.sh when --AuroraSetup is used. Safe to delete; it is
# regenerated on the next provisioning run. Contains ARNs/names only (no secrets).
export AURORA_DATA_API_CLUSTER_ARN="${CLUSTER_ARN}"
export AURORA_DATA_API_SECRET_ARN="${RO_SECRET_ARN}"
export AURORA_DATA_API_DATABASE="${AURORA_DB_NAME}"
export AURORA_DATA_API_RW_SECRET_ARN="${RW_SECRET_ARN}"
ENVEOF
echo "   Wrote resolved ARNs to ${AURORA_ENV_FILE} ✓"

# ------------------------------------------------------------
# 7. Done — tell the user exactly what to set in config.sh.
# ------------------------------------------------------------
# NOTE: Aurora is populated from REAL local activity by the standalone
# SQLite→Aurora sync worker (sync/sync_to_aurora.py, started by
# local_install.sh) — there is no synthetic sample-data seeder anymore.
echo ""
echo "============================================"
echo "  ✅ Aurora PostgreSQL provisioned & schema created"
echo ""
echo "  Cluster endpoint: ${DSN_DISPLAY}"
echo "  Secrets (Secrets Manager):"
echo "    master     → $AURORA_SECRET_NAME   (bootstrap only)"
echo "    read-write → $AURORA_RW_SECRET_NAME (proxy / psycopg2)"
echo "    read-only  → $AURORA_RO_SECRET_NAME (MicroVM Data API; curated v_nb_* views ONLY)"
echo ""
echo "  ── PROXY (psycopg2 / 5432, READ-WRITE user) ─────────────"
echo "  Set these in scripts/config.sh:"
echo ""
echo "    export STORAGE_BACKEND=\"postgres\""
echo "    export STORAGE_CONNECTION=\"postgresql://${AURORA_RW_USERNAME}:<RW_PASSWORD>@${CLUSTER_ENDPOINT}:${CLUSTER_PORT}/${AURORA_DB_NAME}\""
echo ""
echo "  Retrieve <RW_PASSWORD> without printing it to the terminal, e.g.:"
echo "    aws secretsmanager get-secret-value --secret-id $AURORA_RW_SECRET_NAME \\"
echo "      --profile $AWS_CLI_PROFILE --region $AWS_REGION \\"
echo "      --query SecretString --output text | jq -r .password"
echo ""
echo "  ── Data API ARNs (auto-wired — nothing to paste) ───────"
echo "  These were resolved and written to scripts/.aurora_env, which"
echo "  local_install.sh sources automatically on the next launch. You do NOT"
echo "  need to edit scripts/config.sh — the ARNs are discovered from your account:"
echo ""
echo "    AURORA_DATA_API_CLUSTER_ARN   = ${CLUSTER_ARN}"
echo "    AURORA_DATA_API_SECRET_ARN    = ${RO_SECRET_ARN}   (RO; VM reads of v_nb_* views)"
echo "    AURORA_DATA_API_RW_SECRET_ARN = ${RW_SECRET_ARN}   (RW; sync worker writes)"
echo "    AURORA_DATA_API_DATABASE      = ${AURORA_DB_NAME}"
echo ""
echo "  The sync worker (sync/sync_to_aurora.py) uses the RW secret; it runs"
echo "  locally under your own AWS creds (needs rds-data:* on the cluster +"
echo "  secretsmanager:GetSecretValue on the RW secret). Not needed once"
echo "  STORAGE_BACKEND=postgres (proxy writes Aurora directly; sync/ can be removed)."
echo ""
echo "  The VM exec role must allow rds-data:* on the cluster ARN and"
echo "  secretsmanager:GetSecretValue on the RO secret ARN. This repo's"
echo "  iam/exec-role-permissions.json already grants them (the RO secret is"
echo "  under microvm/*, and rds-data is scoped to the cluster) — re-run"
echo "  scripts/setup_iam.sh if you haven't applied the latest policy."
echo ""
echo "  ── Data ─────────────────────────────────────────────────"
echo "  Aurora is populated from REAL local activity by the standalone"
echo "  SQLite→Aurora sync worker (sync/sync_to_aurora.py), started"
echo "  automatically by local_install.sh. No synthetic seeding step."
echo ""
echo "  Then start the proxy: bash local_install.sh"
echo "============================================"
