#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit test for the confirm-before-execute approval hook
(agent/ai_assistant/tools/approval_hook.py::ExecutionApprovalHook).

WHAT THIS PROVES (no live model, no stack)
------------------------------------------
The hook is the SINGLE enforcement point for the gate. Its _gate(event) method is
pure given a small stub `event`, so we can drive every decision branch directly:

  1. Non-gated tool            → pass through (no interrupt, no cancel).
  2. Auto-execute ON (env)     → pass through (gate disabled).
  3. approval_channel == False → pass through (the /ai/chat/sync endpoint).
  4. Session already trusts    → pass through (prior "trust" response).
  5. Approve ("y")             → interrupt raised, then run (no cancel).
  6. Trust ("t")               → interrupt raised, trust persisted, run (no cancel).
  7. Deny (anything else)      → interrupt raised, cancel_tool set.

It also pins that:
  * the interrupt reason carries {"tool", "input"} so the UI card can render the
    proposed code/package;
  * the decision is normalized case-insensitively (uppercase "Y"/"Trust" work);
  * the absent-channel default is GATED (fail-safe) — a missing _approval_channel
    still prompts.

The hook reads _auto_execute_enabled / _get_context from execution_tools; we
monkeypatch those module attributes so no env/context plumbing is needed.

Run:
    agent/ai_assistant/.venv/bin/python tests/ai/test_approval_hook.py
Exit 0 = all passed; 1 = at least one failed.
"""

import os
import sys

# Make `ai_assistant...` importable (the agent service puts `agent/` on sys.path).
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

from ai_assistant.tools import approval_hook as ah  # noqa: E402

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail else ""))


class _State:
    """Stub for agent.state (JSONSerializableDict): get/set a small dict."""
    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Agent:
    def __init__(self, state=None):
        self.state = _State(state)


class _Event:
    """Stub BeforeToolCallEvent: tool_use, agent, interrupt(), cancel_tool."""
    def __init__(self, name, tool_input=None, decision="y", state=None):
        self.tool_use = {"name": name, "input": tool_input or {}}
        self.agent = _Agent(state)
        self._decision = decision
        self.interrupt_calls = []   # list of (name, reason)
        self.cancel_tool = False    # mirrors the real default

    def interrupt(self, name, reason=None, response=None):
        # On a live run this raises the first time and RETURNS the stored response
        # on resume. In the hook's single-pass logic the resumed value is what it
        # acts on, so we simulate the resumed return directly.
        self.interrupt_calls.append((name, reason))
        return self._decision


def _run(event, *, auto=False, channel=True):
    """Drive the hook with monkeypatched env/context helpers."""
    orig_auto, orig_ctx = ah._auto_execute_enabled, ah._get_context
    ah._auto_execute_enabled = lambda: auto
    ah._get_context = lambda: ({} if channel is None else {"_approval_channel": channel})
    try:
        ah.ExecutionApprovalHook()._gate(event)
    finally:
        ah._auto_execute_enabled = orig_auto
        ah._get_context = orig_ctx


def main():
    print("=" * 72)
    print(" ExecutionApprovalHook._gate — OFFLINE unit test")
    print("=" * 72)

    # 1. Non-gated tool → pass through.
    e = _Event("insert_cell", decision="n")
    _run(e)
    record("non-gated tool: no interrupt, no cancel",
           not e.interrupt_calls and e.cancel_tool is False)

    # 2. Auto-execute ON → pass through even for a gated tool.
    e = _Event("execute_code", {"code": "print(1)"}, decision="n")
    _run(e, auto=True)
    record("auto-execute ON: gate bypassed (no interrupt, no cancel)",
           not e.interrupt_calls and e.cancel_tool is False)

    # 3. approval_channel == False → pass through (sync endpoint).
    e = _Event("execute_code", {"code": "print(1)"}, decision="n")
    _run(e, channel=False)
    record("approval_channel False: pass through (no interrupt, no cancel)",
           not e.interrupt_calls and e.cancel_tool is False)

    # 3b. absent channel defaults to GATED (fail-safe) → it DOES interrupt.
    e = _Event("execute_code", {"code": "print(1)"}, decision="y")
    _run(e, channel=None)
    record("absent approval_channel defaults to gated (interrupt raised)",
           len(e.interrupt_calls) == 1 and e.cancel_tool is False)

    # 4. Session trust short-circuits → no interrupt.
    e = _Event("execute_code", {"code": "print(1)"}, decision="n",
               state={"exec_trust": {"execute_code": "trusted"}})
    _run(e)
    record("session-trusted tool: no interrupt, no cancel",
           not e.interrupt_calls and e.cancel_tool is False)

    # 5. Approve → interrupt raised with the right reason, then run (no cancel).
    e = _Event("execute_code", {"code": "print(2+2)"}, decision="y")
    _run(e)
    ok = (len(e.interrupt_calls) == 1 and e.cancel_tool is False)
    record("approve 'y': interrupt raised, tool runs (no cancel)", ok)
    name, reason = e.interrupt_calls[0] if e.interrupt_calls else (None, {})
    record("interrupt reason carries {tool, input}",
           name == "approve:execute_code"
           and reason.get("tool") == "execute_code"
           and (reason.get("input") or {}).get("code") == "print(2+2)",
           f"name={name!r} reason={reason!r}")

    # 5b. Uppercase approve still approves (case-insensitive normalization).
    e = _Event("execute_code", {"code": "x"}, decision="  Y ")
    _run(e)
    record("approve 'Y' (whitespace/case): runs (no cancel)",
           e.cancel_tool is False and len(e.interrupt_calls) == 1)

    # 6. Trust → interrupt raised, trust persisted, run (no cancel).
    e = _Event("install_package", {"package_name": "seaborn"}, decision="t")
    _run(e)
    persisted = e.agent.state.get("exec_trust") or {}
    record("trust 't': persisted in agent.state + no cancel",
           persisted.get("install_package") == "trusted" and e.cancel_tool is False,
           f"exec_trust={persisted!r}")

    # 6b. Uppercase "Trust" also persists.
    e = _Event("execute_code", {"code": "x"}, decision="Trust")
    _run(e)
    record("trust 'Trust': persisted + no cancel",
           (e.agent.state.get("exec_trust") or {}).get("execute_code") == "trusted"
           and e.cancel_tool is False)

    # 7. Deny → interrupt raised, cancel_tool set to a message string.
    e = _Event("execute_code", {"code": "print(1)"}, decision="n")
    _run(e)
    record("deny 'n': cancel_tool set (tool will NOT run)",
           isinstance(e.cancel_tool, str) and bool(e.cancel_tool)
           and len(e.interrupt_calls) == 1,
           f"cancel_tool={e.cancel_tool!r}")

    # 7b. Garbled/empty decision also denies (fail-safe default).
    e = _Event("execute_code", {"code": "print(1)"}, decision="")
    _run(e)
    record("empty decision denies (fail-safe): cancel_tool set",
           isinstance(e.cancel_tool, str) and bool(e.cancel_tool))

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
