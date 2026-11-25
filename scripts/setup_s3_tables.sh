#!/usr/bin/env bash
# ============================================================
# Setup S3 Tables (Apache Iceberg) demo datasets — always installed by local_install.sh.
#
# Provisions a REAL Amazon S3 Table bucket (the purpose-built Iceberg bucket type
# that shows up on the S3 -> "Table buckets" console page), a namespace, and two
# Iceberg tables correlated to the existing orders/products so they JOIN cleanly
# and light up Iceberg-only capabilities the flat CSV tables can't:
#
#   order_events           — the order LIFECYCLE log (one row per status change:
#                            placed -> paid -> packed -> shipped -> delivered,
#                            some returned/cancelled). Keyed on order_id/user_id
#                            from `orders`. Showcases MERGE/upsert + TIME-TRAVEL.
#   product_price_history  — SCD-2 price/cost history per product (valid_from/
#                            valid_to/is_current, change_reason). Keyed on
#                            product_id from `products`. Showcases SCHEMA-EVOLUTION
#                            + TIME-TRAVEL (reprice an order at its as-of cost).
#
# STORAGE = REAL S3 TABLE BUCKET (not Athena-managed Iceberg in the Glue DB). The
# bucket + namespace are created with the `aws s3tables` API; the tables + data are
# created/seeded with Athena against the "s3tablescatalog" FEDERATED Glue catalog
# that the S3 Tables <-> analytics integration mounts. S3 Tables ARE Iceberg
# natively, so:
#   • CREATE TABLE omits LOCATION and table_type (Iceberg is implicit; S3 manages
#     the storage location and the Iceberg metadata).
#   • Athena can't address the federated catalog inline in DDL, so we register the
#     child catalog (s3tablescatalog/<bucket>) ONCE as an Athena data catalog and
#     run every query with that catalog + the namespace as the EXECUTION CONTEXT,
#     using plain unqualified table names.
#
# PREREQUISITE (one-time, per account/region): the S3 Tables integration with the
# AWS Glue Data Catalog must be ENABLED (S3 console -> Table buckets -> "Enable
# integration", or the Glue API). Without it, `s3tablescatalog` doesn't exist and
# the Athena queries here fail. This script checks for the catalog and exits with a
# clear message if the integration is missing.
#
# The seed data is generated DETERMINISTICALLY from the SAME seeds
# setup_sample_data.sh uses (orders: seed 42; products: seed 42, with the SAME
# per-product RNG draws so prices/costs match the products table exactly), so
# order_events reference real ORD-#### / USR-#### and product_price_history's
# is_current rows reconcile with `products`. Rows are inserted in MULTIPLE Athena
# INSERT batches so each table has real Iceberg SNAPSHOT history to time-travel.
#
# Idempotent: creates the bucket/namespace/catalog if absent (reuses them if
# present) and drops + recreates the two Iceberg tables on every run (clean slate),
# mirroring how setup_sample_data.sh recreates the CSV tables.
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

# Use the interpreter local_install.sh exported ($PYTHON → the project .venv); fall
# back to host python3 when run standalone. The seed heredoc below is stdlib-only,
# but keeping this consistent means every subscript runs under the SAME interpreter.
PYTHON="${PYTHON:-python3}"

ATHENA_OUTPUT="s3://${ARTIFACT_BUCKET}/athena-results/"
# Staging prefix for the temp external CSV tables we load the generated data from.
# These live in the regular Glue DB (AwsDataCatalog), NOT the S3 Tables namespace
# (which only holds Iceberg tables) — we INSERT...SELECT across catalogs from them.
STAGE_PREFIX="samples/_s3tables_stage"
STAGE_DB="${ATHENA_DB}"

# S3 Tables coordinates (from config.sh).
TABLE_BUCKET="${S3TABLES_BUCKET}"
TABLE_BUCKET_ARN="arn:aws:s3tables:${AWS_REGION}:${ACCOUNT_ID}:bucket/${TABLE_BUCKET}"
NS="${S3TABLES_NAMESPACE}"
CAT="${S3TABLES_ATHENA_CATALOG}"

echo ">> Setting up S3 Tables (real S3 Table bucket): order_events, product_price_history"
echo "   bucket=${TABLE_BUCKET}  namespace=${NS}  athena-catalog=${CAT}  region=${AWS_REGION}"

# --- Retry helper (mirrors setup_sample_data.sh) ---
retry() {
  local max_attempts=$1; shift
  local delay=$1; shift
  local attempt=1
  while true; do
    if "$@"; then return 0; fi
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "   ⚠ Command failed after $max_attempts attempts: $*"; return 1
    fi
    echo "   ⚠ Attempt $attempt failed, retrying in ${delay}s..."; sleep "$delay"
    attempt=$((attempt + 1))
  done
}

# --- Athena query runner (blocking). $2 (optional) = "federated" to run against
#     the S3 Tables catalog+namespace execution context; default runs against the
#     regular AwsDataCatalog/${STAGE_DB}. -----------------------------------------
run_athena_query() {
  local query="$1"
  local ctx="${2:-stage}"
  local -a ctx_args
  if [ "$ctx" = "federated" ]; then
    ctx_args=(--query-execution-context "Catalog=${CAT},Database=${NS}")
  else
    ctx_args=(--query-execution-context "Catalog=AwsDataCatalog,Database=${STAGE_DB}")
  fi
  local max_retries=3
  local attempt=1
  while [ "$attempt" -le "$max_retries" ]; do
    local execution_id
    execution_id=$(aws athena start-query-execution \
      --query-string "$query" \
      --work-group "$ATHENA_WORKGROUP" \
      "${ctx_args[@]}" \
      --result-configuration "OutputLocation=$ATHENA_OUTPUT" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
      --query 'QueryExecutionId' --output text 2>/dev/null || echo "")
    if [ -z "$execution_id" ] || [ "$execution_id" = "None" ]; then
      sleep 2; attempt=$((attempt + 1)); continue
    fi
    local status="RUNNING" wait_count=0
    while [ "$status" = "RUNNING" ] || [ "$status" = "QUEUED" ]; do
      sleep 1
      status=$(aws athena get-query-execution \
        --query-execution-id "$execution_id" \
        --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
        --query 'QueryExecution.Status.State' --output text 2>/dev/null || echo "RUNNING")
      wait_count=$((wait_count + 1))
      [ "$wait_count" -gt 180 ] && break   # 3-min ceiling per query
    done
    if [ "$status" = "SUCCEEDED" ]; then LAST_QID="$execution_id"; return 0; fi
    local reason
    reason=$(aws athena get-query-execution --query-execution-id "$execution_id" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" \
      --query 'QueryExecution.Status.StateChangeReason' --output text 2>/dev/null || echo "unknown")
    if echo "$reason" | grep -qi "already exists"; then return 0; fi
    echo "   ⚠ Athena query failed (attempt $attempt): $reason"
    attempt=$((attempt + 1)); sleep 3
  done
  echo "   ✗ Athena query failed after $max_retries attempts:"; echo "     ${query:0:200}"
  return 1
}

# ============================================================
# 0. Preconditions: S3 Tables <-> Glue integration must be enabled (the
#    s3tablescatalog federated catalog must exist), and the CSV demo tables must
#    exist (we correlate to them).
# ============================================================
echo "   Checking S3 Tables <-> Glue Data Catalog integration..."
if ! aws glue get-catalog --catalog-id s3tablescatalog \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "   ✗ The 's3tablescatalog' federated catalog is not available."
  echo "     Enable the S3 Tables integration with AWS analytics services first:"
  echo "       S3 console -> Table buckets -> Enable integration   (once per account/region)"
  echo "     Then re-run local_install.sh."
  exit 1
fi
echo "   ✓ Integration active (s3tablescatalog reachable)."

echo "   Ensuring staging database ${STAGE_DB} exists..."
run_athena_query "CREATE DATABASE IF NOT EXISTS ${STAGE_DB}"

# ============================================================
# 1. Create the S3 Table bucket + namespace (idempotent).
# ============================================================
echo "   Ensuring S3 Table bucket '${TABLE_BUCKET}' exists..."
if aws s3tables get-table-bucket --table-bucket-arn "$TABLE_BUCKET_ARN" \
     --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "   ✓ Table bucket already exists."
else
  retry 3 3 aws s3tables create-table-bucket --name "$TABLE_BUCKET" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  echo "   ✓ Table bucket created."
fi

echo "   Ensuring namespace '${NS}' exists..."
if aws s3tables get-namespace --table-bucket-arn "$TABLE_BUCKET_ARN" --namespace "$NS" \
     --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "   ✓ Namespace already exists."
else
  retry 3 3 aws s3tables create-namespace --table-bucket-arn "$TABLE_BUCKET_ARN" \
    --namespace "$NS" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  echo "   ✓ Namespace created."
fi

# ============================================================
# 2. Register the child federated catalog (s3tablescatalog/<bucket>) as an Athena
#    data catalog so we can target it via the query execution context. Idempotent.
# ============================================================
echo "   Ensuring Athena data catalog '${CAT}' is registered..."
if aws athena get-data-catalog --name "$CAT" \
     --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "   ✓ Athena data catalog already registered."
else
  retry 3 3 aws athena create-data-catalog \
    --name "$CAT" --type GLUE \
    --parameters "catalog-id=${ACCOUNT_ID}:s3tablescatalog/${TABLE_BUCKET}" \
    --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  echo "   ✓ Athena data catalog registered."
fi

# ============================================================
# 3. Generate correlated seed data (deterministic — SAME seeds as
#    setup_sample_data.sh so keys line up with orders/products).
#    Writes CSVs to /tmp, then we stage them to S3 for temp external tables.
# ============================================================
echo "   Generating correlated seed data (order_events, product_price_history)..."
"$PYTHON" - <<'PYEOF'
import csv, random
from datetime import date, timedelta

# ---- Reproduce the EXACT orders + products the CSV tables were built from ----
# (setup_sample_data.sh uses random.seed(42) and these exact loops, in this order:
#  orders(2000) -> customers(500) -> products(100). To land products in lockstep
#  we must replay the orders + customers draws too.)
COUNTRIES = ['US','UK','DE','FR','JP','AU','CA','MX','BR','IN']
CATEGORIES = ['Electronics','Clothing','Home & Kitchen','Sports','Books','Food & Grocery','Beauty','Toys']
BRANDS = ['TechPro','StyleCraft','HomeEssentials','SportFit','ReadMore','FreshChoice','GlowUp','PlayTime']
PAYMENT_METHODS = ['credit_card','debit_card','paypal','bank_transfer','crypto']

random.seed(42)
orders = []
for i in range(1, 2001):
    qty = random.randint(1, 5)
    price = round(random.uniform(5.0, 200.0), 2)
    orders.append({
        'order_id': f'ORD-{i:05d}',
        'user_id': f'USR-{random.randint(1,500):04d}',
        'product_id': f'PROD-{random.randint(1,100):04d}',
        'quantity': qty,
        'unit_price': price,
        'total': round(qty * price, 2),
        'order_date': date(2024, random.randint(1,12), random.randint(1,28)),
        'shipping_country': random.choice(COUNTRIES),
        'payment_method': random.choice(PAYMENT_METHODS),
    })

# customers loop — consume the RNG stream (values unused here) so products align.
SIGNUP_CHANNELS = ['organic','paid_search','social','referral','email','direct']
SEGMENTS = ['Premium','Standard','New','At-Risk','Churned']
first_names = ['James','Mary','John','Patricia','Robert','Jennifer','Michael','Linda','David','Elizabeth',
               'William','Barbara','Richard','Susan','Joseph','Jessica','Thomas','Sarah','Charles','Karen',
               'Christopher','Nancy','Daniel','Lisa','Matthew','Betty','Anthony','Margaret','Mark','Sandra',
               'Donald','Ashley','Steven','Kimberly','Paul','Emily','Andrew','Donna','Joshua','Michelle',
               'Kenneth','Carol','Kevin','Amanda','Brian','Dorothy','George','Melissa','Timothy','Deborah',
               'Ronald','Stephanie','Edward','Rebecca','Jason','Sharon','Jeffrey','Laura','Ryan','Cynthia']
last_names = ['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez',
              'Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin',
              'Lee','Perez','Thompson','White','Harris','Sanchez','Clark','Ramirez','Lewis','Robinson',
              'Walker','Young','Allen','King','Wright','Scott','Torres','Nguyen','Hill','Flores']
for i in range(1, 501):
    random.choice(first_names); random.choice(last_names)
    random.randint(200,999); random.randint(100,999); random.randint(1000,9999)
    random.randint(20,25); random.randint(1,12); random.randint(1,28)
    random.choice(SIGNUP_CHANNELS); random.choice(SEGMENTS); random.uniform(10,5000)

products = []
for i in range(1, 101):
    cat_idx = (i - 1) % len(CATEGORIES)
    cat = CATEGORIES[cat_idx]; brand = BRANDS[cat_idx]
    price = round(random.uniform(5.0, 300.0), 2)
    cost = round(price * random.uniform(0.3, 0.7), 2)
    # Consume the SAME per-product draws setup_sample_data.sh makes AFTER cost —
    # stock_quantity=randint(0,500) and rating_avg=uniform(1.0,5.0). Omitting these
    # drifts the stream so PROD-0002+ prices would stop matching the products table.
    random.randint(0, 500)              # stock_quantity (unused here)
    round(random.uniform(1.0, 5.0), 1)  # rating_avg (unused here)
    products.append({
        'product_id': f'PROD-{i:04d}', 'name': f'{brand} {cat} Item {i}',
        'category': cat, 'brand': brand, 'price': price, 'cost': cost,
    })

# ============================================================
# order_events — one row PER STATUS TRANSITION of an order.
# Anchored to each order's order_date so timelines are realistic and JOINable.
# ============================================================
random.seed(101)  # own stream, deterministic
CARRIERS = ['UPS','FedEx','DHL','USPS','Amazon Logistics']
oe_rows = []
eid = 0
for o in orders:
    oid, uid, od = o['order_id'], o['user_id'], o['order_date']
    timeline = [('placed', 0), ('paid', 0), ('packed', 1), ('shipped', 2), ('delivered', 5)]
    r = random.random()
    if r < 0.08:
        timeline = [('placed', 0), ('paid', 0), ('cancelled', 1)]           # ~8% cancelled pre-ship
    elif r < 0.20:
        timeline = [('placed', 0), ('paid', 0), ('packed', 1), ('shipped', 2),
                    ('delivered', 5), ('returned', 12)]                       # ~12% delivered->returned
    carrier = random.choice(CARRIERS)
    tracking = f'TRK{random.randint(10**9, 10**10 - 1)}'
    for status, off in timeline:
        eid += 1
        ev_date = od + timedelta(days=off)
        hh, mm = random.randint(8, 20), random.randint(0, 59)
        oe_rows.append({
            'event_id': f'OE-{eid:07d}',
            'order_id': oid,
            'user_id': uid,
            'status': status,
            'event_ts': f'{ev_date.isoformat()} {hh:02d}:{mm:02d}:00',
            'carrier': carrier if status in ('packed','shipped','delivered','returned') else '',
            'tracking_number': tracking if status in ('shipped','delivered','returned') else '',
            'event_date': ev_date.isoformat(),
        })

with open('/tmp/_s3t_order_events.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['event_id','order_id','user_id','status','event_ts','carrier','tracking_number','event_date'])
    for r in oe_rows:
        w.writerow([r['event_id'], r['order_id'], r['user_id'], r['status'],
                    r['event_ts'], r['carrier'], r['tracking_number'], r['event_date']])

# ============================================================
# product_price_history — SCD-2 price/cost versions per product.
# The CURRENT (is_current) row's price/cost EQUALS the products table so the two
# sources reconcile; earlier versions walk backwards in time with reasons.
# ============================================================
random.seed(202)
pph_rows = []
REASONS_UP = ['launch','cost_increase']
REASONS_DOWN = ['promo','markdown','clearance']
for p in products:
    pid = p['product_id']; cur_price = p['price']; cur_cost = p['cost']
    n_versions = random.randint(2, 4)                 # 2-4 versions incl. current
    launch = date(2023, random.randint(1, 12), random.randint(1, 28))
    span_days = (date(2024, 12, 31) - launch).days
    cuts = sorted(random.sample(range(30, max(31, span_days - 1)), n_versions - 1)) if n_versions > 1 else []
    boundaries = [launch] + [launch + timedelta(days=c) for c in cuts] + [None]  # None => open current
    for v in range(n_versions):
        is_current = (v == n_versions - 1)
        valid_from = boundaries[v]
        valid_to = boundaries[v + 1]
        if is_current:
            price, cost = cur_price, cur_cost
            reason = 'current'
        else:
            factor = random.uniform(1.0, 1.4) if random.random() < 0.7 else random.uniform(0.8, 1.0)
            price = round(cur_price * factor, 2)
            cost = round(price * random.uniform(0.3, 0.7), 2)
            reason = random.choice(REASONS_UP if factor >= 1.0 else REASONS_DOWN) if v > 0 else 'launch'
        pph_rows.append({
            'product_id': pid,
            'price': price,
            'cost': cost,
            'valid_from': valid_from.isoformat(),
            'valid_to': valid_to.isoformat() if valid_to else '',
            'is_current': 'true' if is_current else 'false',
            'change_reason': reason,
        })

with open('/tmp/_s3t_price_history.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['product_id','price','cost','valid_from','valid_to','is_current','change_reason'])
    for r in pph_rows:
        w.writerow([r['product_id'], r['price'], r['cost'], r['valid_from'],
                    r['valid_to'], r['is_current'], r['change_reason']])

print(f"   Generated {len(oe_rows)} order_events rows, {len(pph_rows)} price_history rows")
PYEOF

# ============================================================
# 4. Stage the CSVs to S3 and register TEMP external (CSV) tables in the regular
#    Glue DB to read from (we INSERT...SELECT across catalogs into the S3 Tables).
# ============================================================
echo "   Staging seed CSVs to S3..."
aws s3 rm "s3://${ARTIFACT_BUCKET}/${STAGE_PREFIX}/" --recursive \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null || true
retry 3 3 aws s3 cp /tmp/_s3t_order_events.csv \
  "s3://${ARTIFACT_BUCKET}/${STAGE_PREFIX}/order_events/data.csv" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
retry 3 3 aws s3 cp /tmp/_s3t_price_history.csv \
  "s3://${ARTIFACT_BUCKET}/${STAGE_PREFIX}/price_history/data.csv" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null

echo "   Registering temp staging tables (in ${STAGE_DB})..."
run_athena_query "DROP TABLE IF EXISTS ${STAGE_DB}.stg_order_events"
run_athena_query "
CREATE EXTERNAL TABLE ${STAGE_DB}.stg_order_events (
  event_id STRING, order_id STRING, user_id STRING, status STRING,
  event_ts STRING, carrier STRING, tracking_number STRING, event_date STRING
)
ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
STORED AS TEXTFILE
LOCATION 's3://${ARTIFACT_BUCKET}/${STAGE_PREFIX}/order_events/'
TBLPROPERTIES ('skip.header.line.count'='1')
"
run_athena_query "DROP TABLE IF EXISTS ${STAGE_DB}.stg_price_history"
run_athena_query "
CREATE EXTERNAL TABLE ${STAGE_DB}.stg_price_history (
  product_id STRING, price DOUBLE, cost DOUBLE,
  valid_from STRING, valid_to STRING, is_current STRING, change_reason STRING
)
ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
STORED AS TEXTFILE
LOCATION 's3://${ARTIFACT_BUCKET}/${STAGE_PREFIX}/price_history/'
TBLPROPERTIES ('skip.header.line.count'='1')
"

# Fully-qualified staging refs for cross-catalog SELECT from the federated context.
STG_OE="AwsDataCatalog.${STAGE_DB}.stg_order_events"
STG_PH="AwsDataCatalog.${STAGE_DB}.stg_price_history"

# ============================================================
# 5. Create the S3 Table (Iceberg) tables in the namespace (drop first for a
#    clean, idempotent rerun). NOTE: no LOCATION, no table_type — S3 Tables are
#    Iceberg natively and manage their own storage.
# ============================================================
echo "   Creating S3 Table: order_events"
run_athena_query "DROP TABLE IF EXISTS order_events" federated
run_athena_query "
CREATE TABLE order_events (
  event_id STRING,
  order_id STRING,
  user_id STRING,
  status STRING,
  event_ts TIMESTAMP,
  carrier STRING,
  tracking_number STRING,
  event_date DATE
)
PARTITIONED BY (month(event_date))
" federated

echo "   Creating S3 Table: product_price_history"
run_athena_query "DROP TABLE IF EXISTS product_price_history" federated
run_athena_query "
CREATE TABLE product_price_history (
  product_id STRING,
  price DOUBLE,
  cost DOUBLE,
  valid_from DATE,
  valid_to DATE,
  is_current BOOLEAN,
  change_reason STRING
)
" federated

# ============================================================
# 6. Seed via MULTIPLE INSERT batches so each table has real Iceberg SNAPSHOT
#    history (time-travel). order_events: 3 batches by status phase; price
#    history: 2 batches (historical versions, then current). Each INSERT SELECTs
#    across catalogs from the staging table into the federated S3 Table.
# ============================================================
echo "   Seeding order_events (3 snapshot batches)..."
run_athena_query "
INSERT INTO order_events
SELECT event_id, order_id, user_id, status,
       CAST(event_ts AS TIMESTAMP), carrier, tracking_number, CAST(event_date AS DATE)
FROM ${STG_OE}
WHERE status IN ('placed','paid','cancelled')
" federated
run_athena_query "
INSERT INTO order_events
SELECT event_id, order_id, user_id, status,
       CAST(event_ts AS TIMESTAMP), carrier, tracking_number, CAST(event_date AS DATE)
FROM ${STG_OE}
WHERE status IN ('packed','shipped')
" federated
run_athena_query "
INSERT INTO order_events
SELECT event_id, order_id, user_id, status,
       CAST(event_ts AS TIMESTAMP), carrier, tracking_number, CAST(event_date AS DATE)
FROM ${STG_OE}
WHERE status IN ('delivered','returned')
" federated

echo "   Seeding product_price_history (2 snapshot batches)..."
run_athena_query "
INSERT INTO product_price_history
SELECT product_id, price, cost, CAST(valid_from AS DATE),
       CASE WHEN valid_to = '' THEN NULL ELSE CAST(valid_to AS DATE) END,
       CAST(is_current AS BOOLEAN), change_reason
FROM ${STG_PH}
WHERE is_current = 'false'
" federated
run_athena_query "
INSERT INTO product_price_history
SELECT product_id, price, cost, CAST(valid_from AS DATE),
       CASE WHEN valid_to = '' THEN NULL ELSE CAST(valid_to AS DATE) END,
       CAST(is_current AS BOOLEAN), change_reason
FROM ${STG_PH}
WHERE is_current = 'true'
" federated

# ============================================================
# 7. Clean up the temp staging tables + S3 (leave the Iceberg data in place).
# ============================================================
echo "   Cleaning up staging tables..."
run_athena_query "DROP TABLE IF EXISTS ${STAGE_DB}.stg_order_events" || true
run_athena_query "DROP TABLE IF EXISTS ${STAGE_DB}.stg_price_history" || true
aws s3 rm "s3://${ARTIFACT_BUCKET}/${STAGE_PREFIX}/" --recursive \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" 2>/dev/null || true
rm -f /tmp/_s3t_order_events.csv /tmp/_s3t_price_history.csv

# ============================================================
# 8. Ensure the exec role can read the new S3 Tables (s3tables read + Glue on the
#    federated catalog + lakeformation:GetDataAccess). Re-apply to pick up the
#    added actions in iam/exec-role-permissions.json.
# ============================================================
echo "   Refreshing execution role permissions..."
retry 3 5 aws iam put-role-policy \
  --role-name "$EXEC_ROLE_NAME" \
  --policy-name "ExecPolicy" \
  --policy-document file://"$ROOT_DIR/iam/exec-role-permissions.json" \
  --profile "$AWS_CLI_PROFILE" >/dev/null || echo "   ⚠ Could not refresh exec role (non-fatal)"

echo ""
echo "   S3 Tables setup complete ✓"
echo "     bucket   : ${TABLE_BUCKET}  (S3 console -> Table buckets)"
echo "     namespace: ${NS}"
echo "     tables   : order_events (order lifecycle log — MERGE + time-travel)"
echo "                product_price_history (SCD-2 price history — schema-evolution + time-travel)"
echo "   Query via Athena catalog '${CAT}' (database=${NS}), e.g.:"
echo "     SELECT * FROM order_events LIMIT 10;   -- with Catalog=${CAT}, Database=${NS}"
