#!/usr/bin/env python3
"""Generate the chat agent's prompt module from the proxy's prompt source.

WHY THIS EXISTS
    The chat agent (agent/ai_assistant/) runs as a standalone process with no
    proxy.* import, so it cannot import proxy/notebook/ai/prompts.py. The chat
    prompt group used to be hand-copied and drifted (code-review C-I-03). This
    script copies the group verbatim from the proxy file, which is the single
    source, into agent/ai_assistant/prompts.py with a generated-file header.

    The group runs from the NOTEBOOK_AGENT_BASE_PROMPT section up to and including
    the NOTEBOOK_AGENT_PROMPT assignment: base prompt, operational context, the
    static reader/SQL/source-type blocks, the helpers_block import and the
    placeholder fill. The one-shot (fix/annotate/terminal) and intel prompts after
    it stay proxy-only.

USAGE
    python scripts/gen_prompts.py           # writes agent/ai_assistant/prompts.py
    python scripts/gen_prompts.py --check   # exit 1 if the agent copy is stale
    Run it from anywhere; paths are resolved from this file. Re-run after any edit
    to the chat prompt group in proxy/notebook/ai/prompts.py.
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_PATH = os.path.join(_ROOT, "proxy", "notebook", "ai", "prompts.py")
TARGET_PATH = os.path.join(_ROOT, "agent", "ai_assistant", "prompts.py")

# First line of the chat group (the comment block above NOTEBOOK_AGENT_BASE_PROMPT)
# and its last line.
_START_MARKER = "# NOTEBOOK_AGENT_BASE_PROMPT"
_END_LINE = 'NOTEBOOK_AGENT_PROMPT = NOTEBOOK_AGENT_BASE_PROMPT + "\\n\\n" + NOTEBOOK_OPERATIONAL_CONTEXT'

HEADER = '''"""
System prompt for the AI CHAT assistant (chat prompt group only).

GENERATED FILE, do not edit. Source: proxy/notebook/ai/prompts.py (the chat
group: NOTEBOOK_AGENT_BASE_PROMPT, NOTEBOOK_OPERATIONAL_CONTEXT and the assembled
NOTEBOOK_AGENT_PROMPT). Regenerate with `python scripts/gen_prompts.py`;
tests/ai/test_prompt_sync.py fails when this copy is stale. The copy exists so the
chat agent runs standalone with NO proxy.* import; it imports HELPERS_BLOCK from
its own helpers_block.py (scripts/gen_helpers_block.py writes both copies).
"""

'''


def extract_chat_group(source: str) -> str:
    """The chat prompt group of the proxy prompts module, verbatim."""
    lines = source.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith(_START_MARKER))
        end = next(i for i, ln in enumerate(lines) if i > start and ln == _END_LINE)
    except StopIteration:
        raise SystemExit(f"chat prompt group markers not found in {SOURCE_PATH}")
    return "\n".join(lines[start:end + 1]) + "\n"


def render() -> str:
    with open(SOURCE_PATH, encoding="utf-8") as f:
        source = f.read()
    return HEADER + extract_chat_group(source)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="Exit 1 if agent/ai_assistant/prompts.py is stale (does not write).")
    args = ap.parse_args()
    want = render()
    if args.check:
        existing = ""
        if os.path.exists(TARGET_PATH):
            with open(TARGET_PATH, encoding="utf-8") as f:
                existing = f.read()
        if existing != want:
            print(f"STALE: {os.path.relpath(TARGET_PATH, _ROOT)} is out of date. "
                  "Run: python scripts/gen_prompts.py", file=sys.stderr)
            return 1
        print(f"OK: {os.path.relpath(TARGET_PATH, _ROOT)} is up to date.")
        return 0
    with open(TARGET_PATH, "w", encoding="utf-8") as f:
        f.write(want)
    print(f"Wrote {TARGET_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
