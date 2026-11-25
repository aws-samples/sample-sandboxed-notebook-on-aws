#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: the chat agent's prompt is generated from the proxy's prompt source
(scripts/gen_prompts.py, code-review C-I-03).

Covers:
  - `scripts/gen_prompts.py --check` exits 0 (agent/ai_assistant/prompts.py is
    the current generated output);
  - the assembled prompts are equal at runtime: agent NOTEBOOK_AGENT_PROMPT and
    NOTEBOOK_AGENT_BASE_PROMPT == the proxy's;
  - the obsolete PENDING_USER_APPROVAL sentinel rule is gone from both (the
    Strands approval hook replaced the sentinel).

Run:
    .venv/bin/python tests/ai/test_prompt_sync.py
"""
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


def main():
    print("\n-- generator --")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen_prompts.py"), "--check"],
                       capture_output=True, text=True, timeout=60)
    check("gen_prompts.py --check passes", r.returncode == 0,
          (r.stderr or r.stdout).strip() + " (run: python scripts/gen_prompts.py)")

    print("\n-- runtime equality --")
    from proxy.notebook.ai import prompts as proxy_prompts
    from ai_assistant import prompts as agent_prompts
    check("NOTEBOOK_AGENT_PROMPT equal",
          agent_prompts.NOTEBOOK_AGENT_PROMPT == proxy_prompts.NOTEBOOK_AGENT_PROMPT)
    check("NOTEBOOK_AGENT_BASE_PROMPT equal",
          agent_prompts.NOTEBOOK_AGENT_BASE_PROMPT == proxy_prompts.NOTEBOOK_AGENT_BASE_PROMPT)
    check("NOTEBOOK_OPERATIONAL_CONTEXT equal",
          agent_prompts.NOTEBOOK_OPERATIONAL_CONTEXT == proxy_prompts.NOTEBOOK_OPERATIONAL_CONTEXT)

    print("\n-- stale sentinel rule --")
    for label, mod in (("proxy", proxy_prompts), ("agent", agent_prompts)):
        check(f"{label}: no PENDING_USER_APPROVAL rule",
              "PENDING_USER_APPROVAL" not in mod.NOTEBOOK_AGENT_PROMPT)

    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
