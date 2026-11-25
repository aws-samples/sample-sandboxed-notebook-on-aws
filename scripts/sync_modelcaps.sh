#!/usr/bin/env bash
# =============================================================================
# sync_modelcaps.sh — vendor the modelcaps RUNTIME trio into isolated services
# =============================================================================
# The canonical modelcaps package lives at the repo root (/modelcaps). Two
# service contexts CANNOT import it directly because they ship decoupled from
# the repo root:
#   • agent/ai_assistant/  — an Amazon Bedrock AgentCore service. Its Docker build
#     context is agent/ai_assistant/ (codeLocation), so Docker cannot COPY from
#     outside it; `agentcore deploy` + local `python -m ai_assistant.*` only see
#     files physically under agent/ai_assistant/.
#   • app/  — the in-VM MicroVM image, built by scripts/build_all_images.sh which
#     zips ONLY app/ + the repo Dockerfile. Anything outside app/ is not shipped.
#
# So this script MECHANICALLY copies the RUNTIME trio — params.py, __init__.py,
# model_caps.json — (NOT refresh.py: it pulls in boto3/third-party and must stay
# out of the VM/agent) into a sibling modelcaps/ package inside each context:
#       agent/ai_assistant/modelcaps/
#       app/notebook/helpers/modelcaps/
# The copies are GENERATED artifacts — never hand-edit them; edit /modelcaps and
# re-run this script. A drift guard (tests/ai/test_modelcaps_sync.py) asserts the
# vendored copies match the canonical files (accounting for the banners below).
#
# Banners:
#   • .py copies get a leading comment banner.
#   • model_caps.json gets a top-level "_generated_banner" string key, injected
#     HERE at copy time ONLY — the canonical /modelcaps/model_caps.json is never
#     modified by this script.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT_DIR/modelcaps"

# The vendored destinations (sibling modelcaps/ package in each isolated context).
DESTS=(
  "$ROOT_DIR/agent/ai_assistant/modelcaps"
  "$ROOT_DIR/app/notebook/helpers/modelcaps"
)

PY_BANNER="# GENERATED — vendored from /modelcaps by scripts/sync_modelcaps.sh.
# DO NOT EDIT; edit /modelcaps and re-run sync."
JSON_BANNER="GENERATED — vendored from /modelcaps by scripts/sync_modelcaps.sh. DO NOT EDIT; edit /modelcaps and re-run sync."

for dest in "${DESTS[@]}"; do
  mkdir -p "$dest"

  # --- .py files: prepend the comment banner, then the canonical body. ---
  for f in params.py __init__.py; do
    {
      printf '%s\n\n' "$PY_BANNER"
      cat "$SRC/$f"
    } > "$dest/$f"
  done

  # --- model_caps.json: inject a top-level _generated_banner key at copy time.
  # Done with the repo venv's Python (stdlib json) so key ordering is stable and
  # the banner lands FIRST; the canonical file on disk is never touched. ---
  "${PYTHON:-python3}" - "$SRC/model_caps.json" "$dest/model_caps.json" "$JSON_BANNER" <<'PYEOF'
import json, sys
src, dst, banner = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src) as fh:
    data = json.load(fh)
# Banner FIRST, then the canonical content verbatim (same keys/values/order).
out = {"_generated_banner": banner}
out.update(data)
with open(dst, "w") as fh:
    json.dump(out, fh, indent=2)
    fh.write("\n")
PYEOF

  echo "   vendored modelcaps -> $dest (params.py, __init__.py, model_caps.json)"
done

echo "✅ modelcaps synced to ${#DESTS[@]} context(s)."
