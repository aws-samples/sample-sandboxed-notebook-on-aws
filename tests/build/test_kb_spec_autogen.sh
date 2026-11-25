#!/usr/bin/env bash
# test-kind: offline
# ============================================================
# Offline test for the Bedrock KB product-spec auto-generation path
# (scripts/kb_specs_lib.sh, used by scripts/setup_bedrock_kb.sh).
#
# No AWS, no proxy, no Bedrock: it never sources config.sh or runs
# setup_bedrock_kb.sh. It checks
#   1. both scripts parse (bash -n),
#   2. kb_spec_set_complete's detection logic on empty/partial/complete dirs,
#   3. the specgen venv pins are exact (==),
#   4. setup_bedrock_kb.sh's fresh-clone branch generates with --no-llm,
#   5. (if scripts/.specgen-venv exists) a real --no-llm generation from a small
#      product fixture (--from-json) into an EMPTY temp dir yields a complete set:
#      one .pdf.metadata.json per PDF + manifest.json listing every PDF.
#
# Run:  bash tests/build/test_kb_spec_autogen.sh
# ============================================================
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB="$ROOT_DIR/scripts/kb_specs_lib.sh"
SETUP="$ROOT_DIR/scripts/setup_bedrock_kb.sh"

PASS=0
FAIL=0
check() {  # check "name" <command...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  ✓ $name"; PASS=$((PASS + 1))
  else
    echo "  ✗ $name"; FAIL=$((FAIL + 1))
  fi
}
not() { ! "$@"; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/kb_spec_autogen.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "== syntax"
check "setup_bedrock_kb.sh parses" bash -n "$SETUP"
check "kb_specs_lib.sh parses" bash -n "$LIB"

# shellcheck source=../scripts/kb_specs_lib.sh
source "$LIB"

echo "== detection (kb_spec_set_complete)"
check "missing dir → incomplete" not kb_spec_set_complete "$TMP/does-not-exist"
mkdir -p "$TMP/empty"
check "empty dir → incomplete" not kb_spec_set_complete "$TMP/empty"
mkdir -p "$TMP/partial"
: > "$TMP/partial/A - Product Spec.pdf"
check "pdf without sidecar/manifest → incomplete" not kb_spec_set_complete "$TMP/partial"
: > "$TMP/partial/A - Product Spec.pdf.metadata.json"
check "pdf + sidecar, no manifest → incomplete" not kb_spec_set_complete "$TMP/partial"
echo "[]" > "$TMP/partial/manifest.json"
check "pdf + sidecar + manifest → complete" kb_spec_set_complete "$TMP/partial"
: > "$TMP/partial/B - Product Spec.pdf"
check "second pdf missing its sidecar → incomplete" not kb_spec_set_complete "$TMP/partial"

echo "== pins"
all_pinned() {
  local p
  for p in "${KB_SPECGEN_PACKAGES[@]}"; do [[ "$p" == *==* ]] || return 1; done
  [ "${#KB_SPECGEN_PACKAGES[@]}" -ge 4 ]
}
check "specgen packages are exact-pinned" all_pinned

echo "== setup_bedrock_kb.sh wiring"
check "sources kb_specs_lib.sh" grep -q 'source .*kb_specs_lib.sh' "$SETUP"
check "fresh-clone branch passes --no-llm" grep -q 'SPEC_GEN_FLAGS+=("--no-llm")' "$SETUP"
check "generation targets the spec dir" grep -q 'kb_generate_specs "\$VENV_PY" "\$ROOT_DIR" "\$SPEC_DIR"' "$SETUP"
check "upload uses s3 sync (no re-upload of unchanged files)" grep -q 'aws s3 sync "\$SPEC_DIR/"' "$SETUP"

echo "== offline --no-llm generation"
VENV_PY="$ROOT_DIR/scripts/.specgen-venv/bin/python"
if [ -x "$VENV_PY" ]; then
  cat > "$TMP/products.json" <<'JSON'
[
  {"product_id": "PROD-0001", "name": "TechPro Electronics Item 1", "category": "Electronics",
   "brand": "TechPro", "price": "99.50", "cost": "40.00", "stock_quantity": "120",
   "rating_avg": "4.1", "order_count": "30", "units_sold": "75", "revenue": "7462.50"},
  {"product_id": "PROD-0002", "name": "ReadMore Books Item 2", "category": "Books",
   "brand": "ReadMore", "price": "19.99", "cost": "6.00", "stock_quantity": "300",
   "rating_avg": "3.8", "order_count": "22", "units_sold": "50", "revenue": "999.50"},
  {"product_id": "PROD-0003", "name": "PlayTime Toys Item 3", "category": "Toys",
   "brand": "PlayTime", "price": "35.00", "cost": "12.00", "stock_quantity": "80",
   "rating_avg": "4.6", "order_count": "18", "units_sold": "40", "revenue": "1400.00"}
]
JSON
  OUT="$TMP/gen"
  mkdir -p "$OUT"
  check "empty output dir starts incomplete" not kb_spec_set_complete "$OUT"
  # Dummy creds + no profile: proves --no-llm --from-json makes no AWS call.
  if env AWS_ACCESS_KEY_ID=offline AWS_SECRET_ACCESS_KEY=offline AWS_CLI_PROFILE=default \
       bash -c 'source "$1"; kb_generate_specs "$2" "$3" "$4" --no-llm --from-json "$5"' \
       _ "$LIB" "$VENV_PY" "$ROOT_DIR" "$OUT" "$TMP/products.json" > "$TMP/gen.log" 2>&1; then
    echo "  ✓ generator exited 0"; PASS=$((PASS + 1))
  else
    echo "  ✗ generator failed:"; sed 's/^/      /' "$TMP/gen.log"; FAIL=$((FAIL + 1))
  fi
  check "generated set is complete" kb_spec_set_complete "$OUT"
  n_pdf=$(find "$OUT" -maxdepth 1 -name '*.pdf' | wc -l | tr -d ' ')
  n_side=$(find "$OUT" -maxdepth 1 -name '*.pdf.metadata.json' | wc -l | tr -d ' ')
  check "3 PDFs generated" test "$n_pdf" -eq 3
  check "3 sidecars generated" test "$n_side" -eq 3
  manifest_matches() {
    "$VENV_PY" - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
m = json.load(open(os.path.join(out, "manifest.json")))
pdfs = sorted(f for f in os.listdir(out) if f.endswith(".pdf"))
assert sorted(e["file"] for e in m) == pdfs, (m, pdfs)
assert not any(e["llm_generated"] for e in m), "expected template content only"
for f in pdfs:
    side = json.load(open(os.path.join(out, f + ".metadata.json")))
    assert side["metadataAttributes"]["doc_type"] == "product_spec", side
PY
  }
  check "manifest lists every PDF, all template (no LLM), sidecars well-formed" manifest_matches
else
  echo "  - SKIP: scripts/.specgen-venv not found (created by scripts/setup_bedrock_kb.sh)"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
