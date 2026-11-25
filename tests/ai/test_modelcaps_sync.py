#!/usr/bin/env python3
# test-kind: offline
"""Drift guard for the VENDORED modelcaps copies.

The canonical modelcaps package lives at the repo root (/modelcaps). Two isolated
service contexts carry MECHANICALLY vendored copies of the runtime trio (params.py,
__init__.py, model_caps.json), produced by scripts/sync_modelcaps.sh:

    agent/ai_assistant/modelcaps/
    app/notebook/helpers/modelcaps/

This test asserts each vendored copy matches the canonical files, accounting HONESTLY
for the banner transforms the sync script applies:

  * .py copies: canonical body, prefixed with a leading comment banner. We strip the
    banner (the first two comment lines + the blank separator) and require the
    REMAINDER to be byte-for-byte identical to the canonical file.
  * model_caps.json: canonical JSON with a single extra top-level "_generated_banner"
    key injected FIRST. We load both as JSON, drop that key from the copy, and require
    the remaining object to equal the canonical object exactly.

If a vendored copy drifts (hand-edited, or sync not re-run after a canonical change),
this fails — directing the fix to /modelcaps + a re-run of scripts/sync_modelcaps.sh.

Run:  .venv/bin/python tests/ai/test_modelcaps_sync.py   (exit 0 = pass, 1 = fail)
"""
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


CANONICAL = os.path.join(_ROOT, "modelcaps")
VENDORED_DIRS = [
    ("agent/ai_assistant", os.path.join(_ROOT, "agent", "ai_assistant", "modelcaps")),
    ("app/notebook/helpers", os.path.join(_ROOT, "app", "notebook", "helpers", "modelcaps")),
]

# The exact banner strings the sync script applies (keep in lockstep with
# scripts/sync_modelcaps.sh).
_PY_BANNER_PREFIX = "# GENERATED"
_JSON_BANNER_KEY = "_generated_banner"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_py_banner(text: str) -> str:
    """Remove the leading comment banner the sync script prepends to a .py copy.

    The banner is two `# ...` comment lines followed by a single blank line, then the
    canonical body. If the text does not start with the banner it is returned as-is
    (so a genuinely drifted/un-bannered copy still fails the equality check below).
    """
    if not text.startswith(_PY_BANNER_PREFIX):
        return text
    lines = text.split("\n")
    # Drop leading comment lines, then exactly one blank separator line.
    i = 0
    while i < len(lines) and lines[i].startswith("#"):
        i += 1
    if i < len(lines) and lines[i] == "":
        i += 1
    return "\n".join(lines[i:])


def main() -> int:
    canonical_params = _read(os.path.join(CANONICAL, "params.py"))
    canonical_init = _read(os.path.join(CANONICAL, "__init__.py"))
    with open(os.path.join(CANONICAL, "model_caps.json"), encoding="utf-8") as fh:
        canonical_json = json.load(fh)

    for label, vdir in VENDORED_DIRS:
        print(f"== {label} ==")
        if not os.path.isdir(vdir):
            check(f"{label}: vendored modelcaps/ dir exists", False, f"missing {vdir}")
            continue

        # --- params.py ---
        vp = os.path.join(vdir, "params.py")
        if os.path.exists(vp):
            stripped = _strip_py_banner(_read(vp))
            check(f"{label}: params.py has the GENERATED banner",
                  _read(vp).startswith(_PY_BANNER_PREFIX))
            check(f"{label}: params.py body matches canonical (banner stripped)",
                  stripped == canonical_params,
                  "body differs — re-run scripts/sync_modelcaps.sh")
        else:
            check(f"{label}: params.py present", False, f"missing {vp}")

        # --- __init__.py ---
        vi = os.path.join(vdir, "__init__.py")
        if os.path.exists(vi):
            stripped = _strip_py_banner(_read(vi))
            check(f"{label}: __init__.py has the GENERATED banner",
                  _read(vi).startswith(_PY_BANNER_PREFIX))
            check(f"{label}: __init__.py body matches canonical (banner stripped)",
                  stripped == canonical_init,
                  "body differs — re-run scripts/sync_modelcaps.sh")
        else:
            check(f"{label}: __init__.py present", False, f"missing {vi}")

        # --- model_caps.json ---
        vj = os.path.join(vdir, "model_caps.json")
        if os.path.exists(vj):
            with open(vj, encoding="utf-8") as fh:
                vendored_json = json.load(fh)
            check(f"{label}: model_caps.json carries the _generated_banner key",
                  _JSON_BANNER_KEY in vendored_json)
            copy_wo_banner = {k: v for k, v in vendored_json.items() if k != _JSON_BANNER_KEY}
            check(f"{label}: model_caps.json content matches canonical (banner key dropped)",
                  copy_wo_banner == canonical_json,
                  "catalog differs — re-run scripts/sync_modelcaps.sh")
        else:
            check(f"{label}: model_caps.json present", False, f"missing {vj}")

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("RESULT: all checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
