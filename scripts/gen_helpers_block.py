#!/usr/bin/env python3
"""Generate the AI prompt's helper reference block from the SAME introspection the
/helpers endpoint + autocomplete use (app.notebook.completion_catalog.build_helpers_meta).

WHY THIS EXISTS
    The chat agent (agent/ai_assistant/) runs as a STANDALONE process with no
    runtime dependency on the VM app (app.notebook.*) or proxy.*, and the proxy's
    prompts (Workbook Intel, fix) must not import the VM app either — see the
    comments in prompts.py. So we can't introspect the helpers at runtime. Instead we
    introspect them HERE (a build-time step, where the app package IS importable) and
    emit the same plain-string module twice: proxy/notebook/ai/helpers_block.py and
    agent/ai_assistant/helpers_block.py, plus the web Snippets fallback list
    (web/src/components/panels/helpersFallback.generated.json). Each prompts.py
    imports HELPERS_BLOCK from its sibling module (with a static fallback) and
    substitutes it into the base prompt.

    This is the "auto-generated signature list" half of the hybrid: the mechanical,
    drift-prone API surface (one line per helper: name(signature) -> ret  # summary)
    is generated so adding a helper surfaces it in the prompt with zero hand edits.
    The BEHAVIORAL STEERING (PREFER this, one-call-per-row, composition recipes, the
    IMPORTANT examples) stays hand-written in prompts.py — that's judgment a docstring
    summary can't carry.

USAGE
    python -m scripts.gen_helpers_block          # writes both helpers_block.py copies + the web fallback
    python -m scripts.gen_helpers_block --check   # exit 1 if any output is stale (CI guard)

    Run it from the repo root. Re-run whenever a helper is added/renamed or its
    signature/summary changes. It's cheap and deterministic.
"""
import argparse
import os
import re
import sys

# Ensure the repo root is importable when this file is run directly
# (python scripts/gen_helpers_block.py) — so `import app.notebook...` resolves.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Category display order in the prompt (mirrors the Snippets panel's CATEGORY_ORDER).
# Unknown categories sort after these, alphabetically — nothing is ever dropped.
_CATEGORY_ORDER = [
    "Data Loading", "Data Export", "Visualization",
    "Generative AI", "Machine Learning", "Utilities",
]


def _clean_signature(name: str, signature: str) -> str:
    """Compact a raw inspect signature for prompt use: drop module prefixes and
    string-annotation quotes, collapse whitespace. Keeps param names + defaults
    (useful to the model) but trims the noise (`pandas.DataFrame` -> `DataFrame`,
    `'pd.DataFrame'` -> `pd.DataFrame`). Falls back to `name(...)` if empty."""
    if not signature:
        return f"{name}(...)"
    s = signature
    # Strip the qualifier on common annotated types so the line stays readable.
    s = s.replace("pandas.core.frame.DataFrame", "DataFrame")
    s = s.replace("pandas.core.series.Series", "Series")
    s = s.replace("pandas.DataFrame", "DataFrame")
    s = s.replace("pandas.Series", "Series")
    s = s.replace("numpy.ndarray", "np.ndarray")
    # Drop the quotes around string (forward-ref) annotations: 'pd.DataFrame' -> pd.DataFrame.
    s = re.sub(r"'([\w\.\[\], ]+)'", r"\1", s)
    # Collapse any doubled whitespace introduced by the replacements.
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Max chars for a helper's summary line in the prompt. Long enough for a full first
# sentence (the common case), short enough that one rambling docstring can't bloat
# the prompt. A sentence longer than this is cut at the last word boundary + "…".
_SUMMARY_MAX = 200

# Abbreviations whose trailing period must NOT end the "first sentence" (otherwise
# "e.g." / "i.e." split the summary mid-thought, as in read_bedrock_kb).
_ABBREV = ("e.g.", "i.e.", "etc.", "vs.", "approx.", "incl.", "cf.", "no.", "Dr.", "a.k.a.")


def _one_liner(doc: str) -> str:
    """Build a clean single-line summary from a helper's FULL docstring: the first
    SENTENCE (not just the first physical line — docstrings wrap the opening
    sentence across lines, which truncated it mid-thought). Collapses whitespace,
    skips abbreviation periods (e.g./i.e.), and caps length at _SUMMARY_MAX with a
    word-boundary ellipsis so the prompt stays one tidy line per helper."""
    if not doc:
        return ""
    text = re.sub(r"\s+", " ", doc).strip()
    if not text:
        return ""

    # Walk to the first REAL sentence end: '.', '!' or '?' followed by a space (or
    # end), skipping a period that closes a known abbreviation.
    sentence = text
    for m in re.finditer(r"[.!?](?:\s|$)", text):
        end = m.start() + 1  # include the terminator
        head = text[:end]
        if any(head.endswith(a) for a in _ABBREV):
            continue  # abbreviation period — keep scanning
        sentence = head
        break

    sentence = sentence.strip()
    if len(sentence) > _SUMMARY_MAX:
        # Cut at the last word boundary within the budget, add an ellipsis.
        cut = sentence[:_SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        sentence = cut + "…"
    return sentence


def build_block() -> str:
    """Return the formatted, grouped helper reference block (the __HELPERS_BLOCK__
    substitution). One line per helper: `name(signature)  # summary`, grouped by
    category with a blank line + header between groups."""
    # Import lazily so a plain `--help` doesn't require the app package.
    from app.notebook.completion_catalog import build_helpers_meta

    meta = build_helpers_meta()
    by_cat: dict[str, list[dict]] = {}
    for m in meta:
        by_cat.setdefault(m.get("category", "Other"), []).append(m)

    def rank(cat: str) -> int:
        return _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else len(_CATEGORY_ORDER)

    lines: list[str] = []
    for cat in sorted(by_cat, key=lambda c: (rank(c), c)):
        lines.append(f"{cat}:")
        for m in sorted(by_cat[cat], key=lambda x: x["name"]):
            sig = _clean_signature(m["name"], m.get("signature", ""))
            # Use the FULL docstring (not the pre-truncated first-LINE `summary`) so
            # _one_liner can take the whole first SENTENCE — the first physical line
            # often cut the opening sentence mid-thought.
            summ = _one_liner(m.get("doc") or m.get("summary", ""))
            lines.append(f"  {sig}" + (f"  # {summ}" if summ else ""))
        lines.append("")  # blank line between categories
    return "\n".join(lines).rstrip()


_HEADER = '''"""AUTO-GENERATED — do not edit by hand.

Generated by scripts/gen_helpers_block.py from app.notebook.completion_catalog
.build_helpers_meta() (the same introspection the /helpers endpoint uses). Re-run
that script whenever a helper is added/renamed. prompts.py imports HELPERS_BLOCK
from here and substitutes it into the agent's base prompt, so the helper API surface
in the prompt never drifts from the real helpers — while the hand-written behavioral
steering stays in prompts.py.
"""

# ruff: noqa: E501
'''


def render_module(block: str) -> str:
    """Render the full helpers_block.py module text."""
    # Use a triple-quoted string; escape any stray triple-quote in the content (none
    # expected in signatures/summaries, but be safe).
    safe = block.replace('"""', '\\"\\"\\"')
    return f'{_HEADER}\nHELPERS_BLOCK = """\\\n{safe}\n"""\n'


def _load_curated() -> dict:
    """Load the hand-written example snippets (name -> {desc, code}) from
    web/src/components/panels/curated_snippets.json — the SINGLE source of truth for
    curated code, read by BOTH this generator and the Snippets panel (which imports
    it directly as a sibling of SnippetsPanel.jsx). Missing file → {} (every helper
    then gets a synthesized one-liner)."""
    import json as _json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "web", "src", "components", "panels", "curated_snippets.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _synth_code(name: str, signature: str, returns: str, summary: str) -> str:
    """A minimal, always-valid insert snippet for a helper with no curated example:
    a commented call using the REQUIRED positional params as placeholders, derived
    from the cleaned signature. Mirrors the frontend's old synthSnippet so a helper
    with no curated code still inserts something runnable-shaped."""
    import re as _re
    inner = ""
    mo = _re.search(r"\((.*)\)", signature)
    if mo:
        inner = mo.group(1)
    req = []
    depth = 0
    cur = ""
    parts = []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    for raw in parts:
        p = raw.strip()
        if not p or p == "*" or p.startswith("*") or p in ("self", "cls"):
            continue
        if "=" in p:  # has a default → optional
            continue
        nm = _re.split(r"[:=]", p)[0].strip()
        if nm:
            req.append(nm)
    args = ", ".join(req)
    assign = "df = " if "DataFrame" in (returns or "") else ""
    head = "\ndf.head()" if "DataFrame" in (returns or "") else ""
    return f"# {summary or name}\n{assign}{name}({args}){head}"


def build_fallback_json() -> str:
    """Return the pretty-printed JSON for the frontend Snippets panel's COLD-START
    fallback helper list — now FULLY SELF-CONTAINED: each entry carries the final
    `code` to insert (the curated example from web/src/components/panels/
    curated_snippets.json when
    present, else a synthesized one-liner) plus name/category/signature/returns/
    summary/desc. Built from the SAME introspection the live GET /helpers endpoint
    uses, so the list never drifts from the real helpers. The panel renders this
    directly — no frontend code-resolution needed."""
    import json as _json
    from app.notebook.completion_catalog import build_helpers_meta

    curated = _load_curated()
    meta = build_helpers_meta()
    items = []
    for m in sorted(
        meta,
        key=lambda x: (
            _CATEGORY_ORDER.index(x.get("category", ""))
            if x.get("category", "") in _CATEGORY_ORDER else len(_CATEGORY_ORDER),
            x.get("category", ""),
            x["name"],
        ),
    ):
        name = m["name"]
        sig = _clean_signature(name, m.get("signature", ""))
        returns = m.get("returns", "")
        summary = _one_liner(m.get("doc") or m.get("summary", ""))
        c = curated.get(name) or {}
        code = c.get("code") or _synth_code(name, sig, returns, summary)
        desc = c.get("desc") or summary
        items.append({
            "name": name,
            "category": m.get("category", "Other"),
            "signature": sig,
            "returns": returns,
            "summary": summary,
            "desc": desc,
            "code": code,
        })
    # sort_keys for a byte-stable file (CI --check parity).
    return _json.dumps({"helpers": items}, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="Exit 1 if either generated file is stale (does not write).")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(root, "proxy", "notebook", "ai", "helpers_block.py")
    # The agent ships its OWN static copy of the same module text (imported by
    # agent/ai_assistant/prompts.py).
    agent_out_path = os.path.join(root, "agent", "ai_assistant", "helpers_block.py")
    json_path = os.path.join(root, "web", "src", "components", "panels",
                             "helpersFallback.generated.json")

    block = build_block()
    module_text = render_module(block)
    fallback_json = build_fallback_json()

    if args.check:
        stale = []
        # Check EVERY generated output, including the agent's static copy
        # (agent_out_path). The write path below emits all three; the --check guard
        # must validate all three or the agent copy could silently drift and still
        # pass CI (it ships WITH the standalone agent, so a stale copy means the
        # agent's prompt helper reference diverges from the real helpers).
        for path, want in ((out_path, module_text),
                           (agent_out_path, module_text),
                           (json_path, fallback_json)):
            existing = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    existing = f.read()
            if existing.strip() != want.strip():
                stale.append(os.path.relpath(path, root))
        if stale:
            print(f"STALE: {', '.join(stale)} out of date. "
                  "Run: python -m scripts.gen_helpers_block", file=sys.stderr)
            return 1
        print("OK: helpers_block.py (proxy + agent) + "
              "helpersFallback.generated.json are up to date.")
        return 0

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(module_text)
    with open(agent_out_path, "w", encoding="utf-8") as f:
        f.write(module_text)
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(fallback_json)
    n = block.count("\n  ")  # rough helper-line count
    print(f"Wrote {out_path} (~{n} helper lines).")
    print(f"Wrote {agent_out_path} (agent static copy).")
    print(f"Wrote {json_path} (Snippets panel fallback list).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
