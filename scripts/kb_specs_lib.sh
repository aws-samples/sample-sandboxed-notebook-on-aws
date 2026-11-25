#!/usr/bin/env bash
# ============================================================
# Product-spec PDF helpers for the Bedrock Knowledge Base corpus.
# Sourced by scripts/setup_bedrock_kb.sh (and by tests/build/test_kb_spec_autogen.sh).
#
# The PDFs (+ .metadata.json sidecars + manifest.json) under
# scripts/demoproductspec/ are NOT committed. On first install the folder is
# empty, so setup_bedrock_kb.sh generates them with
# scripts/generate_product_specs.py --no-llm (deterministic built-in template, no
# Bedrock calls) from the Athena demo tables. Bedrock-authored content is opt-in:
#   bash scripts/setup_bedrock_kb.sh --regen
#
# Pure functions only: sourcing this file has no side effects (no AWS calls).
# ============================================================

# Exact pins for the dedicated generator venv (scripts/.specgen-venv). boto3 matches
# app/requirements.txt. matplotlib stays on app/requirements.txt's 3.10 line but at
# 3.10.9, the first 3.10.x pin with wheels for every Python the installer may hand
# us (3.10 → 3.14; 3.10.3 ships no cp314 wheel). reportlab + pypdf are not used
# elsewhere in the repo — pinned to the latest stable release.
KB_SPECGEN_PACKAGES=(
  "reportlab==5.1.0"
  "matplotlib==3.10.9"
  "boto3==1.43.51"
  "pypdf==6.21.4"
)

# kb_spec_set_complete DIR
#   Succeeds (0) when DIR holds a complete generated set: ≥1 *.pdf, a
#   <name>.pdf.metadata.json sidecar for EVERY pdf, and manifest.json. Any gap
#   (empty/missing dir, a pdf without its sidecar, no manifest) returns 1 and
#   prints what is missing, so the caller regenerates the set.
kb_spec_set_complete() {
  local dir="$1" pdf found=0 missing=0
  if [ ! -d "$dir" ]; then
    echo "   (spec dir $dir does not exist)"
    return 1
  fi
  for pdf in "$dir"/*.pdf; do
    [ -e "$pdf" ] || continue
    found=$((found + 1))
    if [ ! -f "${pdf}.metadata.json" ]; then
      echo "   (missing sidecar: $(basename "$pdf").metadata.json)"
      missing=$((missing + 1))
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "   (no product-spec PDFs in $dir)"
    return 1
  fi
  if [ ! -f "$dir/manifest.json" ]; then
    echo "   (missing $dir/manifest.json)"
    return 1
  fi
  [ "$missing" -eq 0 ]
}

# kb_ensure_specgen_venv ROOT_DIR PYTHON
#   Creates scripts/.specgen-venv with the pinned generator deps if it does not
#   exist yet. Prints the venv python path on stdout (last line).
kb_ensure_specgen_venv() {
  local root="$1" py="$2"
  local venv="$root/scripts/.specgen-venv"
  local venv_py="$venv/bin/python"
  if [ ! -x "$venv_py" ]; then
    echo "   Creating specgen venv (${KB_SPECGEN_PACKAGES[*]})..." >&2
    "$py" -m venv "$venv" >&2 || return 1
    "$venv_py" -m pip install --quiet --upgrade pip >&2 || return 1
    "$venv_py" -m pip install --quiet "${KB_SPECGEN_PACKAGES[@]}" >&2 || return 1
  fi
  echo "$venv_py"
}

# kb_generate_specs VENV_PY ROOT_DIR OUT_DIR [generator args...]
#   Runs scripts/generate_product_specs.py into OUT_DIR. Extra args are passed
#   through (e.g. --no-llm, --from-json FILE). Athena/Bedrock targets come from the
#   caller's environment (AWS_REGION, ATHENA_WORKGROUP, ATHENA_DB, BEDROCK_MODEL_ID);
#   AWS_PROFILE is set from AWS_CLI_PROFILE when it names a non-default profile, so
#   boto3 uses the same credentials as the aws CLI calls in setup_bedrock_kb.sh.
kb_generate_specs() {
  local venv_py="$1" root="$2" out="$3"
  shift 3
  if [ -n "${AWS_CLI_PROFILE:-}" ] && [ "${AWS_CLI_PROFILE}" != "default" ]; then
    AWS_PROFILE="$AWS_CLI_PROFILE" \
      "$venv_py" "$root/scripts/generate_product_specs.py" --out "$out" "$@"
  else
    "$venv_py" "$root/scripts/generate_product_specs.py" --out "$out" "$@"
  fi
}
