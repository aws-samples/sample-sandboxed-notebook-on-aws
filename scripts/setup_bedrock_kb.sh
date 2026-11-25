#!/usr/bin/env bash
# ============================================================
# Provision an Amazon Bedrock Knowledge Base backed by Amazon S3 Vectors, with the
# product-guide PDFs (scripts/demoproductspec/*.pdf) as its data source. The PDFs
# are NOT committed: when the folder is empty (fresh clone) this script generates
# them with scripts/generate_product_specs.py --no-llm (built-in template, no
# Bedrock calls) from the Athena demo tables, then reuses them on later runs.
#
# This makes the KB a first-class notebook Data Source (like Athena/S3/DynamoDB/
# Aurora): notebooks/agent can retrieve from it via read_bedrock_kb(...). It is
# an e-commerce product-spec KB, using:
#   • Vector store  : Amazon S3 Vectors (serverless, pay-per-use — no OpenSearch /
#                     Aurora vector cost). Bucket + index (float32, dim 1024,
#                     euclidean; non-filterable keys AMAZON_BEDROCK_TEXT/METADATA).
#   • Embeddings    : Titan Text Embeddings V2 (1024-dim, FLOAT32).
#   • Parsing       : BEDROCK_FOUNDATION_MODEL, MULTIMODAL, via a Sonnet inference
#                     profile — so figures/tables in the PDFs are extracted. Needs
#                     a supplemental S3 storage location for the extracted images.
#   • Data source   : the PDFs uploaded to s3://$ARTIFACT_BUCKET/$BEDROCK_KB_S3_PREFIX.
#
# ALL creates are IDEMPOTENT: an existing vector bucket / index / exec role /
# knowledge base / data source (matched by the configured NAMES) is reused. Safe to
# re-run. It does NOT edit config.sh; it writes the resolved KB id + index ARN to
# scripts/.kb_env, which local_install.sh sources so the proxy learns the KB.
#
# Invoked by local_install.sh on every run (the KB is always installed, idempotent).
#
# Flags:
#   --regen   Regenerate the PDFs from Athena via generate_product_specs.py before
#             uploading, with Bedrock-authored (LLM) product prose — the
#             generator's default (model: $BEDROCK_MODEL_ID). Without --regen the
#             existing local PDFs are uploaded as-is, or generated with --no-llm
#             if the folder has no complete set.
#   --no-llm  With --regen: use the built-in template instead of Bedrock.
# ============================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
# shellcheck source=kb_specs_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/kb_specs_lib.sh"
export AWS_PAGER=""

REGEN=false
GEN_LLM_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --regen) REGEN=true ;;
    --no-llm) GEN_LLM_FLAG="--no-llm" ;;
    *) echo "⚠ Unknown argument: $arg (ignored)" ;;
  esac
done

# --- Prereqs ---------------------------------------------------------------
# Prefer the interpreter local_install.sh exported ($PYTHON → the project .venv) so
# this subscript uses the SAME env the deps were installed into. Only fall back to a
# host python3 when run standalone (no inherited $PYTHON).
if [ -n "${PYTHON:-}" ] && "$PYTHON" --version &>/dev/null; then :
elif command -v python3 &>/dev/null; then PYTHON=python3
elif command -v python &>/dev/null && [[ "$(python --version 2>&1)" == *"3."* ]]; then PYTHON=python
else echo "❌ Python 3 not found."; exit 1; fi

if ! command -v jq &>/dev/null; then
  echo "❌ jq not found. Install it (macOS: brew install jq; Debian/Ubuntu: apt-get install jq)."
  exit 1
fi

# Helpers: scope every call to the configured profile + region.
aws_bedrock() { aws bedrock-agent "$@" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION"; }
aws_s3v()     { aws s3vectors "$@" --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION"; }
aws_iam()     { aws iam "$@" --profile "$AWS_CLI_PROFILE"; }

VECTOR_BUCKET_ARN="arn:aws:s3vectors:${AWS_REGION}:${ACCOUNT_ID}:bucket/${BEDROCK_KB_VECTOR_BUCKET}"
INDEX_ARN="${VECTOR_BUCKET_ARN}/index/${BEDROCK_KB_VECTOR_INDEX}"
SOURCE_BUCKET_ARN="arn:aws:s3:::${ARTIFACT_BUCKET}"
EMBED_MODEL_ARN="arn:aws:bedrock:${AWS_REGION}::foundation-model/${BEDROCK_KB_EMBED_MODEL}"
# Supplemental multimodal storage must be a BUCKET ROOT (Bedrock rejects a
# sub-folder), so it's a dedicated bucket, not a prefix on the artifact bucket.
SUPPLEMENTAL_BUCKET_ARN="arn:aws:s3:::${BEDROCK_KB_SUPPLEMENTAL_BUCKET}"
SUPPLEMENTAL_URI="s3://${BEDROCK_KB_SUPPLEMENTAL_BUCKET}"
KB_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${BEDROCK_KB_ROLE_NAME}"
SPEC_DIR="$ROOT_DIR/scripts/demoproductspec"

echo "============================================"
echo "  Bedrock Knowledge Base — S3 Vectors provisioning"
echo "============================================"
echo "  Account:         $ACCOUNT_ID"
echo "  Region:          $AWS_REGION"
echo "  KB name:         $BEDROCK_KB_NAME"
echo "  Vector bucket:   $BEDROCK_KB_VECTOR_BUCKET"
echo "  Vector index:    $BEDROCK_KB_VECTOR_INDEX  (dim $BEDROCK_KB_EMBED_DIMENSIONS, float32, euclidean)"
echo "  Embedding model: $BEDROCK_KB_EMBED_MODEL"
echo "  Parser model:    $BEDROCK_KB_PARSER_MODEL  (MULTIMODAL)"
echo "  Source PDFs:     s3://${ARTIFACT_BUCKET}/${BEDROCK_KB_S3_PREFIX}"
echo "  Exec role:       $BEDROCK_KB_ROLE_NAME"
echo ""

# --- Region preflight: S3 Vectors must be reachable here ---------------------
if ! aws_s3v list-vector-buckets --max-results 1 &>/dev/null; then
  echo "❌ Amazon S3 Vectors is not available/reachable in ${AWS_REGION} with these"
  echo "   credentials (or the AWS CLI is too old — need a build with 's3vectors')."
  echo "   Bedrock KB provisioning cannot proceed; skipping."
  exit 3
fi

# ------------------------------------------------------------
# 1. Product-guide PDFs → S3.
#    Upload the local scripts/demoproductspec/*.pdf (+ sidecars). If the folder has
#    no complete set (fresh clone — the PDFs are not committed), generate it first
#    with the built-in template (--no-llm: deterministic, no Bedrock calls) from the
#    Athena products/orders tables (local_install.sh runs setup_sample_data.sh
#    before this script). With --regen, always regenerate; Bedrock-authored prose
#    unless --no-llm is also passed.
# ------------------------------------------------------------
SPEC_GEN_FLAGS=()
if [ "$REGEN" = "true" ]; then
  echo ">> --regen: regenerating product-spec PDFs from Athena ($([ -n "$GEN_LLM_FLAG" ] && echo "built-in template" || echo "Bedrock-authored content, model $BEDROCK_MODEL_ID"))"
  [ -n "$GEN_LLM_FLAG" ] && SPEC_GEN_FLAGS+=("$GEN_LLM_FLAG")
elif ! kb_spec_set_complete "$SPEC_DIR"; then
  echo ">> No complete product-spec PDF set in $SPEC_DIR — generating it with the"
  echo "   built-in template (--no-llm, no Bedrock calls) from Athena ($ATHENA_DB)."
  echo "   For Bedrock-authored content later: bash scripts/setup_bedrock_kb.sh --regen"
  REGEN=true
  SPEC_GEN_FLAGS+=("--no-llm")
fi
if [ "$REGEN" = "true" ]; then
  if ! VENV_PY=$(kb_ensure_specgen_venv "$ROOT_DIR" "$PYTHON"); then
    echo "❌ Could not create the specgen venv (scripts/.specgen-venv)."
    exit 4
  fi
  if ! AWS_REGION="$AWS_REGION" ATHENA_WORKGROUP="$ATHENA_WORKGROUP" ATHENA_DB="$ATHENA_DB" \
      kb_generate_specs "$VENV_PY" "$ROOT_DIR" "$SPEC_DIR" ${SPEC_GEN_FLAGS[@]+"${SPEC_GEN_FLAGS[@]}"}; then
    echo "❌ Product-spec generation failed (needs the Athena demo tables from"
    echo "   scripts/setup_sample_data.sh; see the generator output above)."
    exit 4
  fi
fi

if ! kb_spec_set_complete "$SPEC_DIR"; then
  echo "❌ Product-spec PDF set in $SPEC_DIR is still incomplete after generation."
  echo "   Generate it manually and re-run:"
  echo "     scripts/.specgen-venv/bin/python scripts/generate_product_specs.py --no-llm"
  exit 4
fi

PDF_COUNT=$(ls "$SPEC_DIR"/*.pdf | wc -l | tr -d ' ')
echo ">> Syncing ${PDF_COUNT} PDFs (+ .metadata.json sidecars) to s3://${ARTIFACT_BUCKET}/${BEDROCK_KB_S3_PREFIX}"
# `s3 sync` (not `cp`) so unchanged files are NOT re-uploaded on every install: it
# only copies a file whose size differs or whose local mtime is newer than the S3
# copy (i.e. after a regen). The ingestion job below is still started every run;
# Bedrock's ingestion is incremental, so unchanged documents are not re-parsed.
# --exclude manifest.json (internal only, not KB content). Sidecars ARE included
# (Bedrock reads <file>.pdf.metadata.json for metadata filtering).
aws s3 sync "$SPEC_DIR/" "s3://${ARTIFACT_BUCKET}/${BEDROCK_KB_S3_PREFIX}" \
  --exclude "*" --include "*.pdf" --include "*.pdf.metadata.json" \
  --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
echo "   Synced ✓"

# ------------------------------------------------------------
# 1b. Supplemental multimodal storage bucket (idempotent). Bedrock writes the
#     page images it extracts during MULTIMODAL parsing here, and REQUIRES a
#     bucket root (no sub-folder), so it's a dedicated bucket.
# ------------------------------------------------------------
echo ">> Ensuring supplemental storage bucket: $BEDROCK_KB_SUPPLEMENTAL_BUCKET"
if aws s3api head-bucket --bucket "$BEDROCK_KB_SUPPLEMENTAL_BUCKET" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" &>/dev/null; then
  echo "   (bucket exists — reusing)"
else
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BEDROCK_KB_SUPPLEMENTAL_BUCKET" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BEDROCK_KB_SUPPLEMENTAL_BUCKET" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" \
      --profile "$AWS_CLI_PROFILE" --region "$AWS_REGION" >/dev/null
  fi
  echo "   Created ✓"
fi

# ------------------------------------------------------------
# 2. S3 Vectors bucket + index (idempotent).
# ------------------------------------------------------------
echo ">> Ensuring S3 Vectors bucket: $BEDROCK_KB_VECTOR_BUCKET"
if aws_s3v get-vector-bucket --vector-bucket-name "$BEDROCK_KB_VECTOR_BUCKET" &>/dev/null; then
  echo "   (vector bucket exists — reusing)"
else
  aws_s3v create-vector-bucket --vector-bucket-name "$BEDROCK_KB_VECTOR_BUCKET" >/dev/null
  echo "   Created ✓"
fi

# Bedrock Knowledge Bases on S3 Vectors RESERVE two metadata keys and REQUIRE that
# the index marks them NON-FILTERABLE: Bedrock stores each chunk's source text
# under AMAZON_BEDROCK_TEXT and its document metadata under AMAZON_BEDROCK_METADATA.
# If the index is created without these as non-filterable keys, ingestion FAILS
# (S3 Vectors caps filterable metadata size, and these fields exceed it). These
# names are fixed by Bedrock — do not rename. Verified against the console.
KB_NONFILTERABLE_KEYS='["AMAZON_BEDROCK_TEXT","AMAZON_BEDROCK_METADATA"]'
METADATA_CONFIG=$(jq -nc --argjson keys "$KB_NONFILTERABLE_KEYS" '{nonFilterableMetadataKeys:$keys}')

echo ">> Ensuring S3 Vectors index: $BEDROCK_KB_VECTOR_INDEX"
if aws_s3v get-index --vector-bucket-name "$BEDROCK_KB_VECTOR_BUCKET" \
      --index-name "$BEDROCK_KB_VECTOR_INDEX" &>/dev/null; then
  echo "   (index exists — reusing)"
  # Guard: a pre-existing index whose non-filterable keys DON'T include the two
  # Bedrock reserves will silently fail ingestion. Detect + warn (the metadata
  # config is immutable after create, so we can't fix it in place — the operator
  # must drop/recreate the index or point at a fresh one).
  EXISTING_KEYS=$(aws_s3v get-index --vector-bucket-name "$BEDROCK_KB_VECTOR_BUCKET" \
    --index-name "$BEDROCK_KB_VECTOR_INDEX" \
    --query 'index.metadataConfiguration.nonFilterableMetadataKeys' --output json 2>/dev/null || echo "[]")
  for k in AMAZON_BEDROCK_TEXT AMAZON_BEDROCK_METADATA; do
    if ! echo "$EXISTING_KEYS" | jq -e --arg k "$k" 'index($k) != null' >/dev/null 2>&1; then
      echo "   ⚠ WARNING: existing index is MISSING non-filterable key '$k'."
      echo "     Bedrock ingestion will FAIL. Recreate the index (or set"
      echo "     BEDROCK_KB_VECTOR_INDEX to a new name) with non-filterable keys:"
      echo "     AMAZON_BEDROCK_TEXT, AMAZON_BEDROCK_METADATA."
    fi
  done
else
  # Geometry MUST match the embedding model (Titan v2 = 1024 dims, float32) and the
  # non-filterable keys Bedrock reserves for its stored chunk text + metadata.
  aws_s3v create-index \
    --vector-bucket-name "$BEDROCK_KB_VECTOR_BUCKET" \
    --index-name "$BEDROCK_KB_VECTOR_INDEX" \
    --data-type float32 \
    --dimension "$BEDROCK_KB_EMBED_DIMENSIONS" \
    --distance-metric euclidean \
    --metadata-configuration "$METADATA_CONFIG" \
    >/dev/null
  echo "   Created ✓ (dim $BEDROCK_KB_EMBED_DIMENSIONS, float32, euclidean; non-filterable: AMAZON_BEDROCK_TEXT, AMAZON_BEDROCK_METADATA)"
fi

# ------------------------------------------------------------
# 3. KB execution role + scoped policies (idempotent). The role's shape:
#    trust bedrock.amazonaws.com (SourceAccount + KB-ARN condition), and four
#    inline policies (S3 source read/supplemental write, S3 Vectors, embed model,
#    parser inference profile).
# ------------------------------------------------------------
echo ">> Ensuring KB execution role: $BEDROCK_KB_ROLE_NAME"
TRUST_DOC=$(jq -nc --arg acct "$ACCOUNT_ID" --arg region "$AWS_REGION" '{
  Version:"2012-10-17",
  Statement:[{
    Sid:"AmazonBedrockKnowledgeBaseTrustPolicy",
    Effect:"Allow",
    Principal:{Service:"bedrock.amazonaws.com"},
    Action:"sts:AssumeRole",
    Condition:{
      StringEquals:{"aws:SourceAccount":$acct},
      ArnLike:{"aws:SourceArn":("arn:aws:bedrock:"+$region+":"+$acct+":knowledge-base/*")}
    }
  }]
}')
if aws_iam get-role --role-name "$BEDROCK_KB_ROLE_NAME" &>/dev/null; then
  echo "   (role exists — updating trust policy)"
  aws_iam update-assume-role-policy --role-name "$BEDROCK_KB_ROLE_NAME" \
    --policy-document "$TRUST_DOC" >/dev/null
else
  aws_iam create-role --role-name "$BEDROCK_KB_ROLE_NAME" \
    --description "Execution role for the Sandboxed Notebook on AWS Bedrock Knowledge Base (S3 Vectors)" \
    --assume-role-policy-document "$TRUST_DOC" >/dev/null
  echo "   Created ✓"
fi

# Resolve the parser model to an inference-profile ARN when it looks like a
# cross-region inference profile id (e.g. us.anthropic... / global.anthropic...);
# otherwise treat it as a plain foundation-model id. The KB parsing config + the
# role policy both need this ARN.
if [[ "$BEDROCK_KB_PARSER_MODEL" == *.*.* ]]; then
  # inference-profile id (has a region prefix like us./global./eu.)
  PARSER_ARN="arn:aws:bedrock:${AWS_REGION}:${ACCOUNT_ID}:inference-profile/${BEDROCK_KB_PARSER_MODEL}"
  # The underlying foundation-model id drops the leading region-scope token.
  PARSER_FM_ID="${BEDROCK_KB_PARSER_MODEL#*.}"
else
  PARSER_ARN="arn:aws:bedrock:${AWS_REGION}::foundation-model/${BEDROCK_KB_PARSER_MODEL}"
  PARSER_FM_ID="$BEDROCK_KB_PARSER_MODEL"
fi

echo "   Attaching scoped inline policies"
# (a) S3: list/get the source PDFs (artifact bucket, scoped to the KB prefix) +
#     list/get/put/delete on the DEDICATED supplemental bucket (Bedrock writes the
#     extracted multimodal page images there — it's a bucket root, not a prefix).
S3_POLICY=$(jq -nc \
  --arg bucket "$SOURCE_BUCKET_ARN" \
  --arg supp "$SUPPLEMENTAL_BUCKET_ARN" \
  --arg acct "$ACCOUNT_ID" \
  --arg srcpfx "${BEDROCK_KB_S3_PREFIX%/}/*" '{
  Version:"2012-10-17",
  Statement:[
    {Sid:"S3ListBuckets",Effect:"Allow",Action:["s3:ListBucket"],
     Resource:[$bucket,$supp],
     Condition:{StringEquals:{"aws:ResourceAccount":[$acct]}}},
    {Sid:"S3GetSource",Effect:"Allow",Action:["s3:GetObject"],
     Resource:[($bucket+"/"+$srcpfx)],
     Condition:{StringEquals:{"aws:ResourceAccount":[$acct]}}},
    {Sid:"S3RWSupplemental",Effect:"Allow",
     Action:["s3:GetObject","s3:PutObject","s3:DeleteObject"],
     Resource:[($supp+"/*")],
     Condition:{StringEquals:{"aws:ResourceAccount":$acct}}}
  ]
}')
aws_iam put-role-policy --role-name "$BEDROCK_KB_ROLE_NAME" \
  --policy-name "MicroVMKBS3Policy" --policy-document "$S3_POLICY" >/dev/null

# (b) S3 Vectors: query/put/get/delete + get-index on the specific index.
S3V_POLICY=$(jq -nc --arg idx "$INDEX_ARN" --arg acct "$ACCOUNT_ID" '{
  Version:"2012-10-17",
  Statement:[{
    Sid:"S3VectorsPermissions",Effect:"Allow",
    Action:["s3vectors:GetIndex","s3vectors:QueryVectors","s3vectors:PutVectors",
            "s3vectors:GetVectors","s3vectors:DeleteVectors"],
    Resource:$idx,
    Condition:{StringEquals:{"aws:ResourceAccount":$acct}}
  }]
}')
aws_iam put-role-policy --role-name "$BEDROCK_KB_ROLE_NAME" \
  --policy-name "MicroVMKBS3VectorsPolicy" --policy-document "$S3V_POLICY" >/dev/null

# (c) Embedding model: InvokeModel on the Titan embed FM.
EMBED_POLICY=$(jq -nc --arg arn "$EMBED_MODEL_ARN" '{
  Version:"2012-10-17",
  Statement:[{Sid:"BedrockEmbed",Effect:"Allow",Action:["bedrock:InvokeModel"],Resource:[$arn]}]
}')
aws_iam put-role-policy --role-name "$BEDROCK_KB_ROLE_NAME" \
  --policy-name "MicroVMKBEmbedPolicy" --policy-document "$EMBED_POLICY" >/dev/null

# (d) Parser inference profile: GetInferenceProfile + InvokeModel on the profile
#     ARN AND the underlying foundation-model (cross-region profiles fan out).
PARSER_POLICY=$(jq -nc \
  --arg prof "$PARSER_ARN" \
  --arg fm "arn:aws:bedrock:*::foundation-model/${PARSER_FM_ID}" '{
  Version:"2012-10-17",
  Statement:[{
    Sid:"BedrockParser",Effect:"Allow",
    Action:["bedrock:GetInferenceProfile","bedrock:InvokeModel"],
    Resource:[$prof,$fm]
  }]
}')
aws_iam put-role-policy --role-name "$BEDROCK_KB_ROLE_NAME" \
  --policy-name "MicroVMKBParserPolicy" --policy-document "$PARSER_POLICY" >/dev/null
echo "   Policies attached ✓"

# IAM is eventually consistent — a freshly created role may not be assumable by
# Bedrock for a few seconds. Give it a short grace period before create-kb.
echo "   Waiting for IAM role propagation (10s)..."
sleep 10

# ------------------------------------------------------------
# 4. Knowledge base (S3_VECTORS + Titan v2). Reused if a KB with this name exists.
# ------------------------------------------------------------
echo ">> Ensuring Knowledge Base: $BEDROCK_KB_NAME"
KB_ID=$(aws_bedrock list-knowledge-bases \
  --query "knowledgeBaseSummaries[?name=='${BEDROCK_KB_NAME}'].knowledgeBaseId | [0]" \
  --output text 2>/dev/null || echo "None")

if [ -n "$KB_ID" ] && [ "$KB_ID" != "None" ]; then
  echo "   (KB '$BEDROCK_KB_NAME' exists: $KB_ID — reusing)"
else
  KB_CONFIG=$(jq -nc \
    --arg embed "$EMBED_MODEL_ARN" \
    --argjson dims "$BEDROCK_KB_EMBED_DIMENSIONS" \
    --arg supp "$SUPPLEMENTAL_URI" '{
    type:"VECTOR",
    vectorKnowledgeBaseConfiguration:{
      embeddingModelArn:$embed,
      embeddingModelConfiguration:{bedrockEmbeddingModelConfiguration:{dimensions:$dims,embeddingDataType:"FLOAT32"}},
      supplementalDataStorageConfiguration:{storageLocations:[{type:"S3",s3Location:{uri:$supp}}]}
    }
  }')
  # S3_VECTORS storage takes the index ARN ALONE. Passing vectorBucketArn and/or
  # indexName alongside the full indexArn is REJECTED by CreateKnowledgeBase
  # ("Vector index name should not be present with namespace arn") — the ARN
  # already fully identifies the bucket+index. Verified against a working
  # S3 Vectors KB, whose s3VectorsConfiguration is {indexArn} only.
  STORAGE_CONFIG=$(jq -nc --arg iarn "$INDEX_ARN" '{
    type:"S3_VECTORS",
    s3VectorsConfiguration:{indexArn:$iarn}
  }')
  KB_ID=$(aws_bedrock create-knowledge-base \
    --name "$BEDROCK_KB_NAME" \
    --description "Sandboxed Notebook on AWS product knowledge base (S3 Vectors; product-spec PDFs)" \
    --role-arn "$KB_ROLE_ARN" \
    --knowledge-base-configuration "$KB_CONFIG" \
    --storage-configuration "$STORAGE_CONFIG" \
    --query 'knowledgeBase.knowledgeBaseId' --output text)
  echo "   Created KB: $KB_ID ✓"
fi

# ------------------------------------------------------------
# 5. Data source (S3 prefix) with MULTIMODAL FM parsing. Reused if present.
# ------------------------------------------------------------
DS_NAME="${BEDROCK_KB_NAME}-product-guides"
echo ">> Ensuring data source: $DS_NAME"
DS_ID=$(aws_bedrock list-data-sources --knowledge-base-id "$KB_ID" \
  --query "dataSourceSummaries[?name=='${DS_NAME}'].dataSourceId | [0]" \
  --output text 2>/dev/null || echo "None")

DS_CONFIG=$(jq -nc \
  --arg barn "$SOURCE_BUCKET_ARN" \
  --arg pfx "$BEDROCK_KB_S3_PREFIX" '{
  type:"S3",
  s3Configuration:{bucketArn:$barn,inclusionPrefixes:[$pfx]}
}')
# MULTIMODAL FM parsing so figures/tables in the PDFs are
# extracted. The parser modelArn is pinned into the data source's server-side
# config, so it MUST be reconciled to the CURRENT $PARSER_ARN on every run — a
# stale parser (e.g. a prior BEDROCK_MODEL_ID) left here causes ingestion to call
# GetInferenceProfile on a model the (re-scoped) KB role no longer allows → 403.
INGEST_CONFIG=$(jq -nc --arg parser "$PARSER_ARN" '{
  parsingConfiguration:{
    parsingStrategy:"BEDROCK_FOUNDATION_MODEL",
    bedrockFoundationModelConfiguration:{modelArn:$parser,parsingModality:"MULTIMODAL"}
  }
}')

if [ -n "$DS_ID" ] && [ "$DS_ID" != "None" ]; then
  # Reuse the existing data source but UPDATE it so its parsingConfiguration tracks
  # the current parser model (idempotent; fixes a stale parser after a model switch).
  # update-data-source requires the full config set, so we resend the S3 config too.
  aws_bedrock update-data-source \
    --knowledge-base-id "$KB_ID" \
    --data-source-id "$DS_ID" \
    --name "$DS_NAME" \
    --data-source-configuration "$DS_CONFIG" \
    --vector-ingestion-configuration "$INGEST_CONFIG" \
    --data-deletion-policy "DELETE" >/dev/null \
    && echo "   (data source exists: $DS_ID — updated parser → $BEDROCK_KB_PARSER_MODEL)" \
    || echo "   (data source exists: $DS_ID — reusing; parser update skipped)"
else
  DS_ID=$(aws_bedrock create-data-source \
    --knowledge-base-id "$KB_ID" \
    --name "$DS_NAME" \
    --data-source-configuration "$DS_CONFIG" \
    --vector-ingestion-configuration "$INGEST_CONFIG" \
    --data-deletion-policy "DELETE" \
    --query 'dataSource.dataSourceId' --output text)
  echo "   Created data source: $DS_ID ✓"
fi

# ------------------------------------------------------------
# 6. Start an ingestion job and poll to completion (multimodal parsing takes
#    minutes). A KB with a fresh data source has nothing retrievable until this
#    finishes, so we wait — but a timeout is non-fatal (ingestion keeps running
#    server-side; the KB just isn't queryable yet).
# ------------------------------------------------------------
echo ">> Starting ingestion job (multimodal parse + embed → S3 Vectors)"
JOB_ID=$(aws_bedrock start-ingestion-job \
  --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
  --query 'ingestionJob.ingestionJobId' --output text)
echo "   Ingestion job: $JOB_ID"

# Poll up to ~10 minutes (60 * 10s).
STATUS="STARTING"
for i in $(seq 1 60); do
  STATUS=$(aws_bedrock get-ingestion-job \
    --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" --ingestion-job-id "$JOB_ID" \
    --query 'ingestionJob.status' --output text 2>/dev/null || echo "UNKNOWN")
  case "$STATUS" in
    COMPLETE) echo "   Ingestion COMPLETE ✓"; break ;;
    FAILED)
      echo "   ⚠ Ingestion FAILED. Details:"
      aws_bedrock get-ingestion-job --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
        --ingestion-job-id "$JOB_ID" --query 'ingestionJob.failureReasons' --output json 2>/dev/null || true
      break ;;
    *) printf "   ...%s (%d/60)\r" "$STATUS" "$i"; sleep 10 ;;
  esac
done
echo ""
if [ "$STATUS" != "COMPLETE" ] && [ "$STATUS" != "FAILED" ]; then
  echo "   ⚠ Ingestion still '$STATUS' after ~10min — continuing. It keeps running"
  echo "     server-side; the KB becomes queryable once it reaches COMPLETE."
fi

# ------------------------------------------------------------
# 7. Emit the machine-readable handoff for local_install.sh to source.
#    ARNs/ids only (no secrets); gitignored regardless.
# ------------------------------------------------------------
KB_ENV_FILE="${KB_ENV_FILE:-$ROOT_DIR/scripts/.kb_env}"
cat > "$KB_ENV_FILE" <<ENVEOF
# Auto-generated by scripts/setup_bedrock_kb.sh — do not edit by hand.
# Sourced by local_install.sh on every run (the KB is always installed). Safe to delete; it
# is regenerated on the next provisioning run. Contains ids/ARNs only (no secrets).
# BEDROCK_KB_ID accepts a COMMA-SEPARATED list of KB ids for multi-KB setups; each
# id is surfaced as its own Data Source and gets its own entity-intel profile.
export BEDROCK_KB_ID="${KB_ID}"
export BEDROCK_KB_INDEX_ARN="${INDEX_ARN}"
export BEDROCK_KB_DATA_SOURCE_ID="${DS_ID}"
export BEDROCK_KB_NAME="${BEDROCK_KB_NAME}"
ENVEOF
echo "   Wrote resolved KB ids/ARNs to ${KB_ENV_FILE} ✓"

echo ""
echo "============================================"
echo "  ✅ Bedrock Knowledge Base provisioned (S3 Vectors)"
echo "    KB id:        $KB_ID"
echo "    KB name:      $BEDROCK_KB_NAME"
echo "    Index ARN:    $INDEX_ARN"
echo "    Data source:  $DS_ID  (s3://${ARTIFACT_BUCKET}/${BEDROCK_KB_S3_PREFIX})"
echo "    Ingestion:    $STATUS"
echo ""
echo "  local_install.sh sources scripts/.kb_env automatically on the next launch,"
echo "  so the proxy learns the KB (BEDROCK_KB_ID). The VM exec role must allow"
echo "  bedrock:Retrieve on the KB ARN + bedrock-agent list/get for discovery —"
echo "  re-run scripts/setup_iam.sh if you haven't applied the latest policy."
echo "============================================"
