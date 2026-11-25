#!/usr/bin/env python3
# test-kind: offline
"""
Offline END-TO-END interrupt handshake test for the confirm-before-execute gate,
WITHOUT a live Bedrock model.

WHAT THIS PROVES (no stack, no network)
---------------------------------------
Drives a real Strands Agent wired with ExecutionApprovalHook and a fake/echo tool
named "execute_code", using a SCRIPTED model that emits the Bedrock streaming
protocol (messageStart → contentBlockStart(toolUse) → delta(input) →
contentBlockStop → messageStop(tool_use)). This is the "minimal scripted agent"
path the task calls for when a pure stub of chat_stream is impractical:

  1. First turn → the hook interrupts BEFORE the tool runs; the AgentResult has
     stop_reason == "interrupt" and a non-empty result.interrupts list (the tool
     did NOT execute — the echo recorder is empty).
  2. Resume with "y" → the interrupt returns the response, the tool RUNS (the echo
     recorder captured the code), and the turn finishes end_turn.
  3. Resume with "n" (fresh agent) → the hook sets cancel_tool, so the tool is
     NEVER run (recorder empty) and the turn still finishes.

Run:
    agent/ai_assistant/.venv/bin/python tests/ai/test_approval_hook_handshake.py
Exit 0 = all passed; 1 = at least one failed.
"""

import asyncio
import os
import sys
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

# Gate must be ON (auto-execute OFF) for the hook to interrupt. Set BEFORE import so
# _auto_execute_enabled() reads it.
os.environ["AI_AGENT_AUTO_EXECUTE"] = "false"

from strands import Agent, tool  # noqa: E402
from strands.models.model import Model  # noqa: E402

from ai_assistant.tools.approval_hook import ExecutionApprovalHook  # noqa: E402
from ai_assistant.tools.execution_tools import set_execution_context  # noqa: E402

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail else ""))


# A recorder the fake tool writes to when it actually runs — our proof of execution.
_RAN = []


@tool
def execute_code(code: str) -> str:
    """Fake execute_code: records that it ran and echoes the code."""
    _RAN.append(code)
    return f"ran: {code}"


class ScriptedModel(Model):
    """Emits a tool-use on the first pass, then a final text on the resumed pass.

    Strands calls stream() once per event-loop cycle. The FIRST call (no toolResult
    in the messages yet) yields a toolUse for execute_code. Any LATER call (the
    messages now contain a toolResult, i.e. the tool ran or was cancelled) yields a
    short final text and stops with end_turn.
    """

    def __init__(self):
        self._cfg = {"model_id": "scripted-test"}

    def update_config(self, **cfg):
        self._cfg.update(cfg)

    def get_config(self):
        return self._cfg

    async def structured_output(self, output_model, prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        has_tool_result = any(
            isinstance(b, dict) and "toolResult" in b
            for m in messages for b in (m.get("content") or [])
        )
        if not has_tool_result:
            # ── emit a toolUse for execute_code ──
            tool_use_id = "tool-" + uuid.uuid4().hex[:8]
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0,
                                         "start": {"toolUse": {"toolUseId": tool_use_id,
                                                               "name": "execute_code"}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                                         "delta": {"toolUse": {"input": '{"code": "print(2+2)"}'}}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            # ── resumed pass: short final answer ──
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                                         "delta": {"text": "done."}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def _make_agent():
    return Agent(
        model=ScriptedModel(),
        tools=[execute_code],
        hooks=[ExecutionApprovalHook()],
        callback_handler=None,
    )


async def _drive_first(agent):
    """Run the first turn; return the terminal AgentResult."""
    result = None
    async for event in agent.stream_async("run print(2+2)"):
        if isinstance(event, dict) and "result" in event:
            result = event["result"]
    return result


async def _drive_resume(agent, interrupt_id, response):
    prompt = [{"interruptResponse": {"interruptId": interrupt_id, "response": response}}]
    result = None
    async for event in agent.stream_async(prompt):
        if isinstance(event, dict) and "result" in event:
            result = event["result"]
    return result


async def run():
    set_execution_context({"_approval_channel": True, "proxy_url": "http://localhost:8081"})

    # ── Case 1+2: interrupt, then APPROVE ──
    _RAN.clear()
    agent = _make_agent()
    res = await _drive_first(agent)
    paused = res is not None and getattr(res, "stop_reason", None) == "interrupt"
    interrupts = list(getattr(res, "interrupts", None) or [])
    record("first turn pauses (stop_reason == 'interrupt')", paused,
           f"stop_reason={getattr(res, 'stop_reason', None)!r}")
    record("result carries unanswered interrupt(s)", len(interrupts) == 1,
           f"n={len(interrupts)}")
    record("tool did NOT run before approval", not _RAN, f"ran={_RAN!r}")

    if interrupts:
        itr = interrupts[0]
        reason = itr.reason or {}
        record("interrupt reason carries execute_code + code", (
            reason.get("tool") == "execute_code"
            and (reason.get("input") or {}).get("code") == "print(2+2)"
        ), f"reason={reason!r}")

        res2 = await _drive_resume(agent, itr.id, "y")
        record("resume 'y': tool RAN (recorder captured code)",
               _RAN == ["print(2+2)"], f"ran={_RAN!r}")
        record("resume 'y': turn finished (not interrupt)",
               res2 is not None and getattr(res2, "stop_reason", None) != "interrupt",
               f"stop_reason={getattr(res2, 'stop_reason', None)!r}")

    # ── Case 3: interrupt, then DENY ──
    _RAN.clear()
    agent2 = _make_agent()
    res3 = await _drive_first(agent2)
    ints3 = list(getattr(res3, "interrupts", None) or [])
    if ints3:
        res4 = await _drive_resume(agent2, ints3[0].id, "n")
        record("resume 'n': tool did NOT run (cancelled)", not _RAN, f"ran={_RAN!r}")
        record("resume 'n': turn still finished (not interrupt)",
               res4 is not None and getattr(res4, "stop_reason", None) != "interrupt",
               f"stop_reason={getattr(res4, 'stop_reason', None)!r}")
    else:
        record("deny case produced an interrupt to resume", False, "no interrupt on first turn")


def main():
    print("=" * 72)
    print(" ExecutionApprovalHook — OFFLINE scripted interrupt handshake")
    print("=" * 72)
    asyncio.run(run())
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
