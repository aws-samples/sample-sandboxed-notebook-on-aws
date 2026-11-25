#!/usr/bin/env python3
# test-kind: offline
"""
Offline re-verification for review finding F1: a confirm-before-execute PAUSE in
notebook_agent.chat_stream must NOT emit an OpenTelemetry "Failed to detach context"
ERROR, and the observable frame sequence on pause/resume must be unchanged.

WHAT THIS PROVES (no stack, no network, no live model)
------------------------------------------------------
Drives notebook_agent.chat_stream() directly against a real Strands Agent that is
pre-seeded into the session cache (save_session) and backed by a SCRIPTED model that
emits the Bedrock streaming protocol (toolUse -> resume -> end_turn). This exercises
the EXACT drain path F1 is about: chat_stream iterates agent.stream_async manually and,
on the interrupt, sets paused=True + continues so stream_async runs its own post-yield
teardown (_end_agent_trace_span + context_api.detach(baggage_token)) in its own frame.

Assertions:
  1. PAUSE frame sequence: on an execute_code interrupt chat_stream yields a
     confirm_execute action frame (carrying code + interrupt_id) followed by an
     {"type":"interrupt","interrupt_ids":[...]} marker, and NO {"type":"done"}.
  2. NO OTel ERROR: an opentelemetry.context logging handler installed for the whole
     run captures ZERO "Failed to detach context" records on the pause (F1 happy path).
  3. RESUME 'y': feeding the interruptResponse back runs the tool and the turn finishes
     (frames resume into the same consumer; no interrupt marker on the resumed turn).
  4. RESUME 'n': denial cancels the tool (never runs) and the turn still finishes.
  5. No OTel ERROR across the resume turns either.
  6. Offline guard: the seeded agent is the one get_or_create_agent returns, and
     no real BedrockModel is ever constructed (construction raises AssertionError).

Run:
    .venv/bin/python tests/ai/test_chat_stream_interrupt_drain.py
Exit 0 = all passed; 1 = at least one failed.
"""

import asyncio
import logging
import os
import sys
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

# Gate must be ON (auto-execute OFF) for the hook to interrupt. Set BEFORE import.
os.environ["AI_AGENT_AUTO_EXECUTE"] = "false"

from strands import Agent, tool  # noqa: E402
from strands.models.model import Model  # noqa: E402

from ai_assistant.tools.approval_hook import ExecutionApprovalHook  # noqa: E402
from ai_assistant.tools.execution_tools import set_execution_context  # noqa: E402
from ai_assistant import notebook_agent  # noqa: E402
from ai_assistant.sessions import save_session, clear_session  # noqa: E402

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail else ""))


# Capture every opentelemetry log record so we can assert the F1 ERROR never fires.
class _OTelCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def detach_errors(self):
        return [r for r in self.records
                if "Failed to detach context" in r.getMessage()
                or ("detach" in r.getMessage() and r.levelno >= logging.ERROR)]


_RAN = []


@tool
def execute_code(code: str) -> str:
    """Fake execute_code: records that it ran and echoes the code."""
    _RAN.append(code)
    return f"ran: {code}"


class ScriptedModel(Model):
    """toolUse on the first pass, final text once a toolResult is present."""

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
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                                         "delta": {"text": "done."}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}


_BEDROCK_BUILT = []


def _install_no_bedrock_guard():
    """Fail loudly if anything tries to build a REAL Bedrock model during this
    offline test (a cache miss in get_or_create_agent would otherwise silently
    build a BedrockModel and call ConverseStream). Returns an undo callable."""
    from strands.models.bedrock import BedrockModel as _BM

    orig_init = _BM.__init__
    orig_get_model = notebook_agent.get_model

    def _no_init(self, *a, **kw):
        _BEDROCK_BUILT.append(kw.get("model_id"))
        raise AssertionError("real Bedrock model constructed in an offline test")

    def _no_get_model(model_id=None):
        _BEDROCK_BUILT.append(model_id)
        raise AssertionError("real Bedrock model constructed in an offline test")

    _BM.__init__ = _no_init
    notebook_agent.get_model = _no_get_model

    def _undo():
        _BM.__init__ = orig_init
        notebook_agent.get_model = orig_get_model
    return _undo


def _seed_agent(session_id):
    """Build a scripted Agent wired with the hook and pre-seed it into the session
    cache so chat_stream's get_or_create_agent returns it (no Bedrock model built)."""
    agent = Agent(
        model=ScriptedModel(),
        tools=[execute_code],
        hooks=[ExecutionApprovalHook()],
        callback_handler=None,
    )
    # get_or_create_agent reuses a cached agent ONLY when its _mvmnb_model_id equals
    # resolve_model_id(context.get("model_id") or AI_MODEL_ID, AI_REGION) — otherwise
    # it treats it as a model switch and rebuilds a real BedrockModel agent. Our
    # context carries no model_id, so stamp the resolved DEFAULT id using the same
    # function/constants the product uses (no hard-coded model string).
    agent._mvmnb_model_id = notebook_agent.resolve_model_id(
        notebook_agent.AI_MODEL_ID, notebook_agent.AI_REGION)
    save_session(session_id, agent)
    reused = notebook_agent.get_or_create_agent(session_id, {})
    record(f"seed: get_or_create_agent reuses the seeded scripted agent ({session_id})",
           reused is agent,
           f"_mvmnb_model_id={agent._mvmnb_model_id!r}")
    return agent


async def _collect(session_id, message=None, interrupt_responses=None):
    """Run one chat_stream turn and collect the yielded frames."""
    frames = []
    ctx = {"_approval_channel": True, "approval_channel": True,
           "proxy_url": "http://localhost:8081"}
    async for fr in notebook_agent.chat_stream(session_id, message or "", ctx,
                                               interrupt_responses=interrupt_responses):
        frames.append(fr)
    return frames


async def run(cap: _OTelCapture):
    set_execution_context({"_approval_channel": True, "proxy_url": "http://localhost:8081"})

    # ── Case 1+2: pause, then APPROVE ──
    _RAN.clear()
    sid = "drain-" + uuid.uuid4().hex[:8]
    _seed_agent(sid)
    frames = await _collect(sid, message="run print(2+2)")

    confirm = [f for f in frames
               if f.get("type") == "action"
               and (f.get("data") or {}).get("action") == "confirm_execute"]
    interrupt_markers = [f for f in frames if f.get("type") == "interrupt"]
    done_markers = [f for f in frames if f.get("type") == "done"]

    record("pause: confirm_execute frame emitted", len(confirm) == 1,
           f"n={len(confirm)}")
    iid = None
    if confirm:
        d = confirm[0]["data"]
        iid = d.get("interrupt_id")
        record("pause: confirm_execute carries code + interrupt_id",
               d.get("code") == "print(2+2)" and bool(iid),
               f"code={d.get('code')!r} iid={iid!r}")
    record("pause: interrupt marker emitted after confirm", len(interrupt_markers) == 1
           and bool((interrupt_markers[0].get("interrupt_ids") or [])),
           f"markers={interrupt_markers!r}")
    record("pause: NO done frame (turn paused)", len(done_markers) == 0,
           f"done={len(done_markers)}")
    record("pause: tool did NOT run", not _RAN, f"ran={_RAN!r}")

    # F1 core assertion — no OTel detach ERROR on the pause.
    errs = cap.detach_errors()
    record("pause: NO opentelemetry 'Failed to detach context' ERROR",
           len(errs) == 0, f"errs={[r.getMessage() for r in errs]!r}")

    # Resume APPROVE
    if iid:
        rframes = await _collect(sid, interrupt_responses=[{"interruptId": iid, "response": "y"}])
        r_interrupts = [f for f in rframes if f.get("type") == "interrupt"]
        record("resume 'y': tool RAN", _RAN == ["print(2+2)"], f"ran={_RAN!r}")
        record("resume 'y': turn finished (no new interrupt marker)",
               len(r_interrupts) == 0, f"interrupts={r_interrupts!r}")

    # ── Case 3: pause, then DENY ──
    _RAN.clear()
    cap.records.clear()
    sid2 = "drain-" + uuid.uuid4().hex[:8]
    _seed_agent(sid2)
    frames2 = await _collect(sid2, message="run print(2+2)")
    confirm2 = [f for f in frames2
                if (f.get("data") or {}).get("action") == "confirm_execute"]
    iid2 = confirm2[0]["data"].get("interrupt_id") if confirm2 else None
    if iid2:
        rframes2 = await _collect(sid2, interrupt_responses=[{"interruptId": iid2, "response": "n"}])
        r_interrupts2 = [f for f in rframes2 if f.get("type") == "interrupt"]
        record("resume 'n': tool did NOT run (cancelled)", not _RAN, f"ran={_RAN!r}")
        record("resume 'n': turn finished (no new interrupt marker)",
               len(r_interrupts2) == 0, f"interrupts={r_interrupts2!r}")
    else:
        record("deny case produced an interrupt to resume", False, "no confirm frame")

    # F1 across the resume turns too.
    errs2 = cap.detach_errors()
    record("resume turns: NO opentelemetry detach ERROR", len(errs2) == 0,
           f"errs={[r.getMessage() for r in errs2]!r}")

    record("offline: NO real Bedrock model constructed", not _BEDROCK_BUILT,
           f"attempted={_BEDROCK_BUILT!r}")

    clear_session(sid)
    clear_session(sid2)


def main():
    print("=" * 72)
    print(" chat_stream — OFFLINE interrupt DRAIN re-verification (review F1)")
    print("=" * 72)
    cap = _OTelCapture()
    otel_logger = logging.getLogger("opentelemetry.context")
    otel_logger.addHandler(cap)
    otel_logger.setLevel(logging.WARNING)
    # Also attach to root so any ERROR logged elsewhere is caught.
    logging.getLogger().addHandler(cap)
    undo_guard = _install_no_bedrock_guard()
    try:
        asyncio.run(run(cap))
    finally:
        undo_guard()
        otel_logger.removeHandler(cap)
        logging.getLogger().removeHandler(cap)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
