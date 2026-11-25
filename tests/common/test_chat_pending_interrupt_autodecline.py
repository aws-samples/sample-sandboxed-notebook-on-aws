#!/usr/bin/env python3
# test-kind: both
"""
Offline regression test: a NEW chat message sent while a confirm-before-execute
approval is still pending must auto-decline that approval and then answer the new
message (instead of Strands raising
"TypeError: prompt_type=<class 'str'> | must resume from interrupt with list of
interruptResponse's", which broke the session until the thread was cleared).

WHAT THIS PROVES (no stack, no network, no live model, temp SQLite DB)
---------------------------------------------------------------------
Drives notebook_agent.chat_stream() through the REAL get_or_create_agent build path
(make_session_manager -> DbSessionRepository on a TEMP SQLite DB, flush_session,
ExecutionApprovalHook, _mvmnb_model_id) with get_model patched to return ONE shared
scripted model (mirrors the shared per-model cache). A guard makes any real
BedrockModel construction raise.

Cases:
  (0) Bug reproduction: with the auto-decline disabled, a new message on a paused
      session raises the original TypeError.
  (1) New message while an execute_code approval is pending: no error, notice text
      frame first, new message answered, tool NOT run, 0 extra model calls, one
      INFO log line (no user content / code). Also via agent_runtime.invoke.
  (2) History valid afterwards (every toolUse has a toolResult, roles alternate,
      declined toolResult = "User denied ...", followed by the notice) + further turn.
  (3) Same after a simulated process restart (agent rehydrated from the temp DB with
      an activated interrupt); persisted row deactivated afterwards.
  (4) Two pending interrupts (execute_code + install_package) both declined.
  (5) ISOLATION: sessions A and B — A paused while B runs tool + approval + resume;
      A then auto-declines concurrently with B's turn; neither touches the other's
      in-memory state, history, or persisted row. Variant: A declines while B is
      paused; B stays paused and resumes normally.
  (6) Explicit Approve / Reject resume flows unchanged.
  (7) /ai/chat/sync (approval_channel False) with a pending interrupt: pending tool
      is DECLINED (not auto-run), no notice frame, response == model answer only.
      A sync turn with nothing pending still auto-runs tools (existing contract).

LIVE section (runs AFTER the offline cases, real proxy + agent + Bedrock model)
-----------------------------------------------------------------------------
A preflight first checks the proxy is up (GET /health), the X-Proxy-Auth token
resolves and is accepted (GET /ai/config), the chat agent answers GET /ping, and
the proxy runs with AI_AGENT_AUTO_EXECUTE=false (gate ON). If any check fails the
live cases are SKIPPED (reason printed) and only the offline result counts.
Cases (HTTP only; the gated tool never runs, so no MicroVM is launched):
  (L1) /ai/chat asking the model to call execute_code -> confirm_execute frame +
       interrupt marker (turn paused).
  (L2) a NEW /ai/chat message on the paused session -> no error, notice frame
       first, real model answer, done; no new confirm frames.
  (L3) persisted history in the live SQLite DB (read-only): valid pairing, the
       declined toolUse carries the "User denied" result, interrupt deactivated.
  (L4) GET /ai/chat/{sid}/messages transcript shows the notice; a further turn works.
  (L5) explicit Reject via /ai/chat/resume still works (no notice).
  (L6) /ai/chat/sync with a pending approval -> declined, answer has no notice.
  Every test session is deleted (DELETE /ai/chat/{sid}) in a finally.
Costs a handful of Bedrock calls. Opt out with AUTODECLINE_SKIP_LIVE=1.

Run (sanitized env, see the task report):
    PYTHONPATH=<repo>:<repo>/agent .venv/bin/python tests/common/test_chat_pending_interrupt_autodecline.py
Exit 0 = all passed (live skipped counts as pass); 1 = at least one failed.
"""
import asyncio
import copy
import logging
import os
import shutil
import sys
import tempfile
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
for _p in (_AGENT, _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Gate must be ON (auto-execute OFF) for the hook to interrupt. Set BEFORE import.
os.environ["AI_AGENT_AUTO_EXECUTE"] = "false"
os.environ.pop("MEMORY_BACKEND", None)  # sqlite (DbSessionRepository)

# ── Temp SQLite DB: patch BEFORE any storage call (connections are lazy). ──
_TMP = tempfile.mkdtemp(prefix="autodecline-test-")
import proxy.storage.sqlite_db as sdb  # noqa: E402
# The LIVE section (below) reads the running agent's real DB read-only; capture its
# path before the offline section redirects this process to the temp DB.
_LIVE_DB_PATH = sdb.DB_PATH
sdb.DB_DIR = _TMP
sdb.DB_PATH = os.path.join(_TMP, "test.db")
if not sdb.DB_PATH.startswith(_TMP):
    raise SystemExit("refusing to run: DB_PATH not in temp dir")
from proxy.storage import storage  # noqa: E402
storage.initialize()

from strands import tool  # noqa: E402
from strands.models.model import Model  # noqa: E402

from ai_assistant import notebook_agent  # noqa: E402
from ai_assistant import memory  # noqa: E402
from ai_assistant.constants import CHAT_AGENT_ID  # noqa: E402
from ai_assistant.sessions import clear_session, get_session  # noqa: E402
from ai_assistant.tools.approval_hook import DENY_MESSAGE  # noqa: E402

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail and not ok else ""))


class _Capture(logging.Handler):
    def __init__(self, level):
        super().__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]

    def detach_errors(self):
        return [m for m in self.messages() if "Failed to detach context" in m]


# ── Fake tools (no network): record that they ran. ──
_RAN = []


@tool
def execute_code(code: str) -> str:
    """Fake execute_code."""
    _RAN.append(("execute_code", code))
    return f"ran: {code}"


@tool
def install_package(package_name: str) -> str:
    """Fake install_package."""
    _RAN.append(("install_package", package_name))
    return f"installed: {package_name}"


_SIDS: list = []          # every session id used (to attribute model calls)
_MODEL_CALLS: list = []   # (session_id, kind)


def _tool_block(idx, name, input_json):
    tid = "tool-" + uuid.uuid4().hex[:8]
    return [
        {"contentBlockStart": {"contentBlockIndex": idx,
                               "start": {"toolUse": {"toolUseId": tid, "name": name}}}},
        {"contentBlockDelta": {"contentBlockIndex": idx,
                               "delta": {"toolUse": {"input": input_json}}}},
        {"contentBlockStop": {"contentBlockIndex": idx}},
    ]


class ScriptedModel(Model):
    """Behaviour from the LAST message: toolResult -> 'after-tool'; user text
    'RUN_EXEC x' -> execute_code toolUse; 'RUN_BOTH x' -> execute_code +
    install_package in one message; anything else -> 'answer:<first word>'."""

    def __init__(self):
        self._cfg = {"model_id": "scripted-test"}

    def update_config(self, **cfg):
        self._cfg.update(cfg)

    def get_config(self):
        return self._cfg

    async def structured_output(self, output_model, prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        sid = next((s for s in _SIDS if s in (system_prompt or "")), "?")
        last = messages[-1] if messages else {}
        blocks = last.get("content") or []
        text = next((b["text"] for b in reversed(blocks)
                     if isinstance(b, dict) and "text" in b), "")
        has_result = any(isinstance(b, dict) and "toolResult" in b for b in blocks)
        events = [{"messageStart": {"role": "assistant"}}]
        if has_result:
            kind = "after-tool"
        else:
            words = text.split()
            kind = words[0] if words else "empty"
        rest = " ".join(text.split()[1:])
        if kind == "RUN_EXEC":
            events += _tool_block(0, "execute_code",
                                  '{"code": "print(\\"SENTINEL_CODE %s\\")"}' % rest)
            stop = "tool_use"
        elif kind == "RUN_BOTH":
            events += _tool_block(0, "execute_code",
                                  '{"code": "print(\\"SENTINEL_CODE %s\\")"}' % rest)
            events += _tool_block(1, "install_package", '{"package_name": "sentinelpkg"}')
            stop = "tool_use"
        else:
            reply = "after-tool" if kind == "after-tool" else f"answer:{kind}"
            events += [
                {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": reply}}},
                {"contentBlockStop": {"contentBlockIndex": 0}},
            ]
            stop = "end_turn"
        events.append({"messageStop": {"stopReason": stop}})
        _MODEL_CALLS.append((sid, kind))
        for ev in events:
            await asyncio.sleep(0)  # let concurrent sessions interleave
            yield ev


_SHARED_MODEL = ScriptedModel()
_BEDROCK_BUILT = []


def _install_patches():
    from strands.models.bedrock import BedrockModel as _BM
    orig_init = _BM.__init__
    orig_get_model = notebook_agent.get_model
    orig_tools = notebook_agent.AGENT_TOOLS

    def _no_init(self, *a, **kw):
        _BEDROCK_BUILT.append(kw.get("model_id"))
        raise AssertionError("real Bedrock model constructed in an offline test")

    _BM.__init__ = _no_init
    notebook_agent.get_model = lambda model_id=None: _SHARED_MODEL
    notebook_agent.AGENT_TOOLS = [execute_code, install_package]

    def _undo():
        _BM.__init__ = orig_init
        notebook_agent.get_model = orig_get_model
        notebook_agent.AGENT_TOOLS = orig_tools
    return _undo


# ── helpers ──
def new_sid(tag):
    sid = f"{tag}-{uuid.uuid4()}"
    _SIDS.append(sid)
    return sid


async def collect(sid, msg=None, responses=None, channel=True):
    ctx = {"approval_channel": channel, "proxy_url": "http://localhost:8081"}
    frames = []
    async for fr in notebook_agent.chat_stream(sid, msg or "", ctx,
                                               interrupt_responses=responses):
        frames.append(fr)
    return frames


def texts(frames):
    return "".join(f.get("content", "") for f in frames if f.get("type") == "text")


def confirm_ids(frames):
    return [f["data"]["interrupt_id"] for f in frames
            if f.get("type") == "action"
            and (f.get("data") or {}).get("action") in ("confirm_execute", "confirm_install")]


def calls_for(sid, since):
    return [k for s, k in _MODEL_CALLS[since:] if s == sid]


def history_valid(messages):
    """Every assistant toolUse id has a toolResult in the NEXT user message and
    roles strictly alternate."""
    for i, m in enumerate(messages):
        if i and messages[i - 1]["role"] == m["role"]:
            return False, f"consecutive {m['role']} at {i}"
        ids = [b["toolUse"]["toolUseId"] for b in m.get("content") or []
               if isinstance(b, dict) and "toolUse" in b]
        if ids:
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            if not nxt or nxt["role"] != "user":
                return False, f"toolUse at {i} without following user message"
            got = {b["toolResult"]["toolUseId"] for b in nxt.get("content") or []
                   if isinstance(b, dict) and "toolResult" in b}
            if not set(ids) <= got:
                return False, f"toolUse ids {ids} missing results (got {got})"
    return True, ""


def denied_results(messages):
    out = []
    for m in messages:
        for b in m.get("content") or []:
            if isinstance(b, dict) and "toolResult" in b:
                tr = b["toolResult"]
                t = "".join(c.get("text", "") for c in tr.get("content") or [])
                if t == DENY_MESSAGE and tr.get("status") == "error":
                    out.append(tr["toolUseId"])
    return out


def row(sid):
    memory.flush(sid)
    return storage.agent_read(sid, CHAT_AGENT_ID)


def row_activated(sid):
    r = row(sid) or {}
    return ((r.get("_internal_state") or {}).get("interrupt_state") or {}).get("activated")


def row_messages(sid):
    memory.flush(sid)
    return [r.get("message") for r in storage.agent_message_list(sid, CHAT_AGENT_ID)]


def activated(sid):
    a = get_session(sid)
    return bool(a and a._interrupt_state.activated)


def decline_logs(cap, since=0):
    return [m for m in cap.messages()[since:] if m.startswith("auto-decline:")]


async def pause(sid, msg="RUN_EXEC x"):
    frames = await collect(sid, msg)
    return frames, confirm_ids(frames)


# ── cases ──
async def case0_bug_repro():
    print("\n(0) bug reproduction with the fix disabled")
    sid = new_sid("c0")
    _, ids = await pause(sid)
    check("c0: paused with one interrupt", len(ids) == 1 and activated(sid))
    orig = notebook_agent._auto_decline_pending

    async def _noop(agent, session_id):
        return None
    notebook_agent._auto_decline_pending = _noop
    err = None
    try:
        await collect(sid, "hello")
    except Exception as e:  # noqa: BLE001
        err = e
    finally:
        notebook_agent._auto_decline_pending = orig
    check("c0: without auto-decline the original TypeError is raised",
          isinstance(err, TypeError) and "must resume from interrupt" in str(err),
          repr(err))
    frames = await collect(sid, "hello")
    check("c0: with the fix restored the same session recovers",
          "answer:hello" in texts(frames) and not activated(sid))
    clear_session(sid)


async def case1_2(cap):
    print("\n(1)+(2) new message while execute_code approval is pending")
    _RAN.clear()
    sid = new_sid("c1")
    _, ids = await pause(sid, "RUN_EXEC c1code")
    check("c1: paused (confirm frame + activated)", len(ids) == 1 and activated(sid))
    n_calls = len(_MODEL_CALLS)
    log_mark = len(cap.messages())
    frames = await collect(sid, "hello")
    types = [f.get("type") for f in frames]
    check("c1: no error frame", "error" not in types, str(types))
    # model_status (UI badge) is sent before the notice; the notice is the first TEXT frame.
    first = next((f for f in frames if f.get("type") != "model_status"), {})
    check("c1: first text frame is the notice naming execute_code",
          first.get("type") == "text" and "execute_code" in first.get("content", "")
          and "new message" in first.get("content", "")
          and first.get("content", "").startswith("_(") ,
          repr(first))
    check("c1: new message answered after the notice",
          texts(frames).endswith("answer:hello"), texts(frames))
    check("c1: declined execute_code adds the 'may guess column names' note",
          any(f.get("type") == "text" and "may guess column names" in f.get("content", "")
              and f.get("content", "").startswith("_(") for f in frames), texts(frames))
    check("c1: no confirm/interrupt frames on the new turn",
          not confirm_ids(frames) and "interrupt" not in types, str(types))
    check("c1: pending tool did NOT run", not _RAN, repr(_RAN))
    check("c1: agent no longer activated", not activated(sid))
    check("c1: decline cost 0 model calls (only the new turn's 1)",
          calls_for(sid, n_calls) == ["hello"], repr(calls_for(sid, n_calls)))
    logs = decline_logs(cap, log_mark)
    check("c1: exactly one auto-decline INFO line", len(logs) == 1, repr(logs))
    if logs:
        ln = logs[0]
        check("c1: log has session prefix, tool, count",
              f"session={sid[:8]}..." in ln and "tools=execute_code" in ln
              and "interrupts=1" in ln, ln)
        check("c1: log has no user content / code",
              "SENTINEL_CODE" not in ln and "hello" not in ln and "c1code" not in ln, ln)
    check("c1: no OTel detach error", not cap.detach_errors(), repr(cap.detach_errors()))

    # (2) history validity + declined result + further turn
    agent = get_session(sid)
    msgs = agent.messages
    ok, why = history_valid(msgs)
    check("c2: history valid (toolUse/toolResult paired, roles alternate)", ok, why)
    check("c2: declined toolUse carries the 'User denied' error result",
          len(denied_results(msgs)) == 1, repr(denied_results(msgs)))
    idx = next((i for i, m in enumerate(msgs)
                if any("toolResult" in b for b in m.get("content") or [])), None)
    after = msgs[idx + 1] if idx is not None and idx + 1 < len(msgs) else {}
    check("c2: declined result followed by the notice as assistant text",
          after.get("role") == "assistant"
          and "Declined the pending execute_code request" in
          "".join(b.get("text", "") for b in after.get("content") or []), repr(after))
    frames = await collect(sid, "again")
    check("c2: a further turn works", texts(frames) == "answer:again"
          and "error" not in [f.get("type") for f in frames], texts(frames))
    ok, why = history_valid(row_messages(sid))
    check("c2: persisted history valid too", ok, why)
    check("c2: persisted row deactivated", row_activated(sid) is False, repr(row_activated(sid)))
    clear_session(sid)

    # via agent_runtime.invoke (the network entrypoint's dispatcher)
    from ai_assistant import agent_runtime
    sid = new_sid("c1rt")
    await pause(sid)
    out = []
    async for ev in agent_runtime.invoke({"action": "chat", "session_id": sid,
                                          "message": "hello",
                                          "context": {"approval_channel": True}}, None):
        out.append(ev)
    t = [e.get("type") for e in out]
    check("c1: via agent_runtime.invoke — no error, ends with done, notice + answer",
          "error" not in t and t[-1:] == ["done"] and "Declined" in texts(out)
          and texts(out).endswith("answer:hello"), repr(out))
    clear_session(sid)


async def case3_restart(cap):
    print("\n(3) after a simulated process restart")
    _RAN.clear()
    sid = new_sid("c3")
    await pause(sid)
    check("c3: persisted row has activated interrupt", row_activated(sid) is True,
          repr(row_activated(sid)))
    old = get_session(sid)
    memory.flush(sid)
    clear_session(sid)
    fresh = notebook_agent.get_or_create_agent(sid, {"approval_channel": True})
    check("c3: fresh agent rehydrated with activated interrupt",
          fresh is not old and fresh._interrupt_state.activated
          and any(i.response is None for i in fresh._interrupt_state.interrupts.values()))
    n_calls = len(_MODEL_CALLS)
    log_mark = len(cap.messages())
    frames = await collect(sid, "hello")
    check("c3: no error, notice + answer",
          "error" not in [f.get("type") for f in frames]
          and [f for f in frames if f.get("type") != "model_status"][0].get("type") == "text"
          and "execute_code" in [f for f in frames if f.get("type") != "model_status"][0]["content"]
          and texts(frames).endswith("answer:hello"), repr(frames))
    check("c3: tool not run, 0 decline model calls",
          not _RAN and calls_for(sid, n_calls) == ["hello"], repr(calls_for(sid, n_calls)))
    check("c3: one log line", len(decline_logs(cap, log_mark)) == 1)
    ok, why = history_valid(get_session(sid).messages)
    check("c3: history valid", ok, why)
    check("c3: persisted row deactivated", row_activated(sid) is False)
    frames = await collect(sid, "again")
    check("c3: further turn works", texts(frames) == "answer:again")
    # and once more across another restart (persisted history replays cleanly)
    memory.flush(sid)
    clear_session(sid)
    frames = await collect(sid, "third")
    ok, why = history_valid(get_session(sid).messages)
    check("c3: post-restart turn on the healed session works + history valid",
          texts(frames) == "answer:third" and ok, why)
    clear_session(sid)


async def case4_two(cap):
    print("\n(4) two pending interrupts")
    _RAN.clear()
    sid = new_sid("c4")
    frames, ids = await pause(sid, "RUN_BOTH y")
    acts = [f["data"]["action"] for f in frames if f.get("type") == "action"]
    check("c4: pause yields confirm_execute + confirm_install with 2 ids",
          sorted(acts) == ["confirm_execute", "confirm_install"] and len(set(ids)) == 2,
          repr(acts))
    n_calls = len(_MODEL_CALLS)
    log_mark = len(cap.messages())
    frames = await collect(sid, "hello")
    note = next((f.get("content", "") for f in frames if f.get("type") == "text"), "")
    check("c4: notice names both tools",
          "execute_code" in note and "install_package" in note and "2 pending" in note, note)
    check("c4: declined execute_code adds the 'may guess column names' note",
          "may guess column names" in texts(frames) and "may guess" not in note, texts(frames))
    logs = decline_logs(cap, log_mark)
    check("c4: log says interrupts=2", len(logs) == 1 and "interrupts=2" in logs[0], repr(logs))
    msgs = get_session(sid).messages
    check("c4: both declined (two 'User denied' results)", len(denied_results(msgs)) == 2)
    ok, why = history_valid(msgs)
    check("c4: history valid", ok, why)
    check("c4: nothing ran, 0 decline model calls, answered",
          not _RAN and calls_for(sid, n_calls) == ["hello"]
          and texts(frames).endswith("answer:hello"), repr((_RAN, calls_for(sid, n_calls))))
    clear_session(sid)


async def case5_isolation(cap):
    print("\n(5) isolation between sessions A and B")
    _RAN.clear()
    A, B = new_sid("isoA"), new_sid("isoB")
    _, a_ids = await pause(A, "RUN_EXEC alpha")
    agent_a = get_session(A)
    snap_a_msgs = copy.deepcopy(agent_a.messages)
    snap_a_int = copy.deepcopy(agent_a._interrupt_state.to_dict())
    snap_a_row = copy.deepcopy(row(A))
    snap_a_rowmsgs = copy.deepcopy(row_messages(A))

    _, b_ids = await pause(B, "RUN_EXEC bravo")
    agent_b = get_session(B)
    check("iso: A and B have distinct agent objects", agent_a is not agent_b)
    check("iso: B paused on its own interrupt while A is paused",
          len(b_ids) == 1 and b_ids != a_ids and activated(A) and activated(B))
    fr = await collect(B, responses=[{"interruptId": b_ids[0], "response": "y"}])
    check("iso: B approve runs B's tool and finishes",
          any("SENTINEL_CODE bravo" in c for _, c in _RAN)
          and texts(fr) == "after-tool" and not activated(B), repr(_RAN))
    check("iso: A untouched by B's turn (messages, interrupt state, row)",
          agent_a.messages == snap_a_msgs
          and agent_a._interrupt_state.to_dict() == snap_a_int
          and row(A) == snap_a_row and row_messages(A) == snap_a_rowmsgs)
    check("iso: A still paused", activated(A))

    snap_b_msgs = copy.deepcopy(agent_b.messages)
    snap_b_int = copy.deepcopy(agent_b._interrupt_state.to_dict())
    snap_b_state = copy.deepcopy(agent_b.state.get())
    log_mark = len(cap.messages())
    fa, fb = await asyncio.gather(collect(A, "hello alpha"), collect(B, "plain bravo"))
    check("iso: A auto-declined (notice) and answered",
          "Declined the pending execute_code" in texts(fa)
          and texts(fa).endswith("answer:hello"), texts(fa))
    check("iso: B's frames have no notice, just its answer", texts(fb) == "answer:plain",
          texts(fb))
    logs = decline_logs(cap, log_mark)
    check("iso: exactly one decline log, naming A's prefix only",
          len(logs) == 1 and f"session={A[:8]}" in logs[0] and B[:8] not in logs[0],
          repr(logs))
    check("iso: B's earlier history prefix untouched, only its own turn appended",
          agent_b.messages[:len(snap_b_msgs)] == snap_b_msgs
          and len(agent_b.messages) == len(snap_b_msgs) + 2
          and not denied_results(agent_b.messages))
    check("iso: B's interrupt + agent.state (exec_trust) untouched",
          agent_b._interrupt_state.to_dict() == snap_b_int
          and agent_b.state.get() == snap_b_state)
    check("iso: only A carries a decline result", len(denied_results(agent_a.messages)) == 1)

    ra, rb = row_messages(A), row_messages(B)

    def _txt(ms):
        return " ".join(str(b.get("text", "")) + str(b.get("toolUse", {}).get("input", ""))
                        for m in ms for b in (m.get("content") or []))
    check("iso: persisted rows distinct and each carries only its own markers",
          "alpha" in _txt(ra) and "bravo" not in _txt(ra)
          and "bravo" in _txt(rb) and "alpha" not in _txt(rb)
          and row(A) != row(B))
    ok_a, _ = history_valid(ra)
    ok_b, _ = history_valid(rb)
    check("iso: both persisted histories valid", ok_a and ok_b)
    check("iso: A's tool never ran", not any("alpha" in c for _, c in _RAN), repr(_RAN))

    # Variant: A declines while B is PAUSED.
    _RAN.clear()
    A2, B2 = new_sid("isoA2"), new_sid("isoB2")
    await pause(A2, "RUN_EXEC alpha2")
    _, b2_ids = await pause(B2, "RUN_EXEC bravo2")
    agent_b2 = get_session(B2)
    snap_b2_int = copy.deepcopy(agent_b2._interrupt_state.to_dict())
    snap_b2_msgs = copy.deepcopy(agent_b2.messages)
    snap_b2_row = copy.deepcopy(row(B2))
    fa2 = await collect(A2, "hello alpha2")
    check("iso2: A2 auto-declined", "Declined" in texts(fa2) and not activated(A2))
    check("iso2: B2 still paused, interrupt id + state + history + row unchanged",
          activated(B2) and list(agent_b2._interrupt_state.interrupts) == b2_ids
          and agent_b2._interrupt_state.to_dict() == snap_b2_int
          and agent_b2.messages == snap_b2_msgs and row(B2) == snap_b2_row)
    fb2 = await collect(B2, responses=[{"interruptId": b2_ids[0], "response": "n"}])
    check("iso2: B2 resumes 'n' normally (no notice, tool not run, model continues)",
          texts(fb2) == "after-tool" and not _RAN and not activated(B2), texts(fb2))
    for s in (A, B, A2, B2):
        clear_session(s)


async def case6_explicit(cap):
    print("\n(6) explicit Approve / Reject unchanged")
    _RAN.clear()
    sid = new_sid("c6y")
    _, ids = await pause(sid, "RUN_EXEC yes")
    n_calls, log_mark = len(_MODEL_CALLS), len(cap.messages())
    fr = await collect(sid, responses=[{"interruptId": ids[0], "response": "y"}])
    check("c6: Approve runs the tool, no notice, model called once more",
          len(_RAN) == 1 and texts(fr) == "after-tool"
          and calls_for(sid, n_calls) == ["after-tool"] and not decline_logs(cap, log_mark),
          repr((_RAN, texts(fr), calls_for(sid, n_calls))))
    clear_session(sid)

    _RAN.clear()
    sid = new_sid("c6n")
    _, ids = await pause(sid, "RUN_EXEC no")
    n_calls, log_mark = len(_MODEL_CALLS), len(cap.messages())
    fr = await collect(sid, responses=[{"interruptId": ids[0], "response": "n"}])
    msgs = get_session(sid).messages
    ok, why = history_valid(msgs)
    check("c6: Reject skips the tool, no notice, model called once more (continues turn)",
          not _RAN and texts(fr) == "after-tool"
          and calls_for(sid, n_calls) == ["after-tool"] and not decline_logs(cap, log_mark),
          repr((_RAN, texts(fr), calls_for(sid, n_calls))))
    check("c6: Reject history valid with one 'User denied' result",
          ok and len(denied_results(msgs)) == 1, why)
    clear_session(sid)


async def case7_sync(cap):
    print("\n(7) /ai/chat/sync (approval_channel False)")
    _RAN.clear()
    sid = new_sid("c7")
    await pause(sid, "RUN_EXEC synccode")  # paused from the interactive channel
    log_mark = len(cap.messages())
    fr = await collect(sid, "hello", channel=False)
    check("c7: sync with pending interrupt — no error", "error" not in
          [f.get("type") for f in fr])
    check("c7: pending tool DECLINED, not auto-run on the sync channel", not _RAN, repr(_RAN))
    check("c7: no notice frame; aggregated text == model answer only",
          texts(fr) == "answer:hello", repr(texts(fr)))
    check("c7: decline logged, agent deactivated",
          len(decline_logs(cap, log_mark)) == 1 and not activated(sid))
    ok, why = history_valid(get_session(sid).messages)
    check("c7: history valid with the decline recorded",
          ok and len(denied_results(get_session(sid).messages)) == 1, why)
    clear_session(sid)

    _RAN.clear()
    sid = new_sid("c7b")
    fr = await collect(sid, "RUN_EXEC autorun", channel=False)
    check("c7: sync turn with nothing pending still auto-runs tools (existing contract)",
          len(_RAN) == 1 and texts(fr) == "after-tool" and not confirm_ids(fr),
          repr((_RAN, texts(fr))))
    clear_session(sid)


async def run(cap):
    await case0_bug_repro()
    await case1_2(cap)
    await case3_restart(cap)
    await case4_two(cap)
    await case5_isolation(cap)
    await case6_explicit(cap)
    await case7_sync(cap)
    check("offline: NO real Bedrock model constructed", not _BEDROCK_BUILT,
          repr(_BEDROCK_BUILT))
    check("offline: temp DB used", storage._get_conn() is not None
          and sdb.DB_PATH.startswith(_TMP))


# ════════════════════════════════════════════════════════════════════════════
# LIVE section — real proxy (:8081) → chat agent → Bedrock. HTTP only.
# ════════════════════════════════════════════════════════════════════════════
import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8081")
_LIVE_TIMEOUT = 180.0          # one model turn, generous
_LIVE_SIDS: list = []          # every live session id (deleted in finally)
_PAUSE_PROMPT = ("Use the execute_code tool right now to run exactly this Python code, "
                 "without asking me first and without any explanation: "
                 "print('AUTODECLINE_LIVE_SENTINEL')")
_NOTICE_MARK = "Declined the pending execute_code request"


def _proxy_pid():
    try:
        return subprocess.check_output(
            ["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN", "-t"], text=True).split()[0]
    except Exception:  # noqa: BLE001
        return None


def _proxy_env_value(name):
    """Read NAME=value from the listening proxy process env (macOS `ps eww`).
    Never printed by callers (tokens are secrets)."""
    pid = _proxy_pid()
    if not pid:
        return None
    try:
        env = subprocess.check_output(["ps", "eww", "-p", pid, "-o", "command="], text=True)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(rf"(?:^|\s){re.escape(name)}=(\S*)", env)
    return m.group(1) if m else None


def _proxy_token():
    return os.environ.get("PROXY_AUTH_TOKEN") or _proxy_env_value("PROXY_AUTH_TOKEN")


def _live_client(token):
    import httpx
    return httpx.Client(base_url=PROXY_URL, timeout=_LIVE_TIMEOUT,
                        headers={"X-Proxy-Auth": token, "Connection": "close"})


def _live_preflight():
    """Return (client, None) when the stack is usable, else (None, reason)."""
    if os.environ.get("AUTODECLINE_SKIP_LIVE") == "1":
        return None, "AUTODECLINE_SKIP_LIVE=1"
    try:
        import httpx
    except ImportError:
        return None, "httpx not installed in this interpreter"
    try:
        h = httpx.get(f"{PROXY_URL}/health", timeout=5)
        body = h.json() if h.status_code == 200 else {}
    except Exception as e:  # noqa: BLE001
        return None, f"proxy not reachable at {PROXY_URL} ({type(e).__name__})"
    if h.status_code != 200 or body.get("status") != "proxy running":
        return None, f"proxy /health not healthy (HTTP {h.status_code})"
    token = _proxy_token()
    if not token:
        return None, "no X-Proxy-Auth token (set PROXY_AUTH_TOKEN or run the proxy on :8081)"
    client = _live_client(token)
    try:
        cfg = client.get("/ai/config", timeout=10)
    except Exception as e:  # noqa: BLE001
        client.close()
        return None, f"/ai/config failed ({type(e).__name__})"
    if cfg.status_code != 200:
        client.close()
        return None, f"/ai/config HTTP {cfg.status_code} (auth token rejected?)"
    if not cfg.json().get("ai_available"):
        client.close()
        return None, "proxy reports ai_available=false (no AWS credentials)"
    agent_url = os.environ.get("AGENT_URL") or _proxy_env_value("AGENT_URL") or "http://localhost:8082"
    try:
        p = httpx.get(f"{agent_url}/ping", timeout=5)
        if p.status_code != 200:
            raise RuntimeError(f"HTTP {p.status_code}")
    except Exception as e:  # noqa: BLE001
        client.close()
        return None, f"chat agent not reachable at {agent_url}/ping ({e})"
    gate = (os.environ.get("LIVE_AI_AGENT_AUTO_EXECUTE")
            or _proxy_env_value("AI_AGENT_AUTO_EXECUTE") or "").lower()
    if gate in ("1", "true", "yes", "on"):
        client.close()
        return None, "proxy runs with AI_AGENT_AUTO_EXECUTE=true (approval gate OFF)"
    backend = (_proxy_env_value("MEMORY_BACKEND") or "sqlite").lower()
    if backend != "sqlite":
        client.close()
        return None, f"MEMORY_BACKEND={backend}; live DB checks need sqlite"
    if not os.path.exists(_LIVE_DB_PATH):
        client.close()
        return None, f"live SQLite DB not found at {_LIVE_DB_PATH}"
    _warn_if_agent_stale(agent_url)
    return client, None


def _warn_if_agent_stale(agent_url):
    """Warn (don't skip) when the running agent process started BEFORE the agent
    code was last modified — it is then serving old code, and the live cases test
    that old code, not the working tree."""
    try:
        port = agent_url.rsplit(":", 1)[-1].split("/")[0]
        pid = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], text=True).split()[0]
        etime = subprocess.check_output(["ps", "-p", pid, "-o", "etime="], text=True).strip()
        # etime: [[dd-]hh:]mm:ss
        days, _, rest = etime.rpartition("-")
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        age = (int(days or 0) * 86400) + parts[0] * 3600 + parts[1] * 60 + parts[2]
        started = time.time() - age
        src = os.path.join(_AGENT, "ai_assistant", "notebook_agent.py")
        if os.path.getmtime(src) > started + 5:
            print("  ! WARNING: the chat agent process started BEFORE "
                  "agent/ai_assistant/notebook_agent.py was last modified — it is serving "
                  "OLD code. Restart the agent to test the working tree.")
    except Exception:  # noqa: BLE001
        pass


def _sse(client, path, payload):
    """POST and collect the SSE frames ({"type": ...} dicts)."""
    frames = []
    with client.stream("POST", path, json=payload) as r:
        if r.status_code != 200:
            r.read()
            return [{"type": "error", "content": f"HTTP {r.status_code}: {r.text[:200]}"}]
        for line in r.iter_lines():
            if line.startswith("data: "):
                try:
                    frames.append(json.loads(line[6:]))
                except ValueError:
                    frames.append({"type": "unparsed", "content": line[:200]})
    return frames


def _live_chat(client, sid, message):
    return _sse(client, "/ai/chat", {"session_id": sid, "notebook_id": "", "message": message})


def _live_resume(client, sid, iid, response):
    return _sse(client, "/ai/chat/resume", {
        "session_id": sid, "notebook_id": "",
        "interrupt_responses": [{"interruptId": iid, "response": response}]})


def _types(frames):
    return [f.get("type") for f in frames]


def _errors(frames):
    return [f.get("content") for f in frames if f.get("type") == "error"]


def _live_pause(client, tag):
    """Open a session and get it paused on an execute_code approval. The model
    usually obeys first time; retry once on a fresh session if it didn't call the
    tool. Returns (sid, frames, interrupt_ids)."""
    for attempt in range(2):
        sid = f"autodecline-live-{tag}-{uuid.uuid4()}"
        _LIVE_SIDS.append(sid)
        frames = _live_chat(client, sid, _PAUSE_PROMPT)
        ids = [f["data"]["interrupt_id"] for f in frames
               if f.get("type") == "action"
               and (f.get("data") or {}).get("action") == "confirm_execute"]
        if ids and _types(frames)[-1:] == ["interrupt"]:
            return sid, frames, ids
        if attempt == 0:
            print(f"    (model did not pause on attempt 1; types={_types(frames)} — retrying)")
    return sid, frames, ids


def _db_rows(sid, wait_for=None, timeout=15.0):
    """Read the session's persisted messages + agent_json from the live SQLite DB
    (read-only). Polls until wait_for(msgs, agent) is true (persistence is flushed
    at turn end, possibly a beat after the stream closes)."""
    deadline = time.monotonic() + timeout
    while True:
        con = sqlite3.connect(f"file:{_LIVE_DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            msgs = [json.loads(r[0]).get("message") or {} for r in con.execute(
                "SELECT message_json FROM agent_messages WHERE session_id=? "
                "ORDER BY message_id", (sid,))]
            row = con.execute("SELECT agent_json FROM agent_sessions WHERE session_id=?",
                              (sid,)).fetchone()
        finally:
            con.close()
        agent = json.loads(row[0]) if row and row[0] else {}
        if wait_for is None or wait_for(msgs, agent) or time.monotonic() > deadline:
            return msgs, agent
        time.sleep(0.5)


def _db_activated(agent):
    return ((agent.get("_internal_state") or {}).get("interrupt_state") or {}).get("activated")


def _tool_use_ids(msgs, name):
    return [b["toolUse"]["toolUseId"] for m in msgs for b in m.get("content") or []
            if isinstance(b, dict) and "toolUse" in b and b["toolUse"].get("name") == name]


def live_case_autodecline(client):
    print("\n(L1)-(L4) live: new message while an execute_code approval is pending")
    sid, frames, ids = _live_pause(client, "main")
    check("live L1: turn paused on confirm_execute (+ interrupt marker, no done/error)",
          len(ids) == 1 and _types(frames)[-1:] == ["interrupt"]
          and "done" not in _types(frames) and not _errors(frames), repr(_types(frames)))
    if not ids:
        return
    frames = _live_chat(client, sid, "Reply with exactly the word PONG and nothing else. "
                                     "Do not call any tools.")
    t = _types(frames)
    # model_status (UI badge) is sent before the notice by design, as in the offline
    # cases above; the notice is the first non-model_status frame.
    content_frames = [f for f in frames if f.get("type") != "model_status"]
    first = content_frames[0] if content_frames else {}
    answer = "".join(f.get("content", "") for f in content_frames[1:] if f.get("type") == "text")
    check("live L2: no error frame", not _errors(frames), repr(_errors(frames)))
    check("live L2: first frame is the decline notice",
          first.get("type") == "text" and first.get("content", "").startswith("_(")
          and _NOTICE_MARK in first.get("content", ""), repr(first))
    check("live L2: real model answer after the notice", "PONG" in answer.upper(),
          repr(answer[:200]))
    check("live L2: stream ends with done, no new confirm/interrupt",
          t[-1:] == ["done"] and "interrupt" not in t
          and not any(f.get("type") == "action" and (f.get("data") or {}).get("action")
                      in ("confirm_execute", "confirm_install") for f in frames), repr(t))

    msgs, agent = _db_rows(sid, wait_for=lambda m, a: _db_activated(a) is False
                           and any("PONG" in json.dumps(x) for x in m))
    ok, why = history_valid(msgs)
    check("live L3: persisted history valid (toolUse/toolResult paired, roles alternate)",
          ok, why)
    exec_ids = _tool_use_ids(msgs, "execute_code")
    denied = denied_results(msgs)
    check("live L3: the declined execute_code carries the 'User denied' result",
          len(exec_ids) == 1 and denied == exec_ids, repr((exec_ids, denied)))
    check("live L3: no successful execute_code result (tool never ran)",
          not any(isinstance(b, dict) and "toolResult" in b
                  and b["toolResult"].get("status") != "error"
                  for m in msgs for b in m.get("content") or []))
    check("live L3: persisted interrupt state deactivated", _db_activated(agent) is False,
          repr(_db_activated(agent)))

    r = client.get(f"/ai/chat/{sid}/messages")
    bubbles = r.json().get("messages", []) if r.status_code == 200 else []
    check("live L4: transcript endpoint shows the notice in an assistant bubble",
          any(b.get("role") == "assistant" and _NOTICE_MARK in b.get("content", "")
              for b in bubbles), f"HTTP {r.status_code}, {len(bubbles)} bubbles")
    frames = _live_chat(client, sid, "Reply with exactly the word OK and nothing else. "
                                     "Do not call any tools.")
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "text")
    check("live L4: further turn works (no error, no notice, done)",
          not _errors(frames) and _NOTICE_MARK not in text and text.strip()
          and _types(frames)[-1:] == ["done"], repr((_types(frames), text[:120])))


def live_case_explicit_reject(client):
    print("\n(L5) live: explicit Reject via /ai/chat/resume unchanged")
    sid, frames, ids = _live_pause(client, "reject")
    if not ids:
        check("live L5: paused for the Reject case", False, repr(_types(frames)))
        return
    frames = _live_resume(client, sid, ids[0], "n")
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "text")
    check("live L5: Reject resumes cleanly (no error, no notice, done)",
          not _errors(frames) and _NOTICE_MARK not in text
          and _types(frames)[-1:] == ["done"], repr((_types(frames), _errors(frames))))
    msgs, agent = _db_rows(sid, wait_for=lambda m, a: _db_activated(a) is False)
    ok, why = history_valid(msgs)
    check("live L5: history valid with exactly one 'User denied' result",
          ok and len(denied_results(msgs)) == 1, why or repr(denied_results(msgs)))


def live_case_sync(client):
    print("\n(L6) live: /ai/chat/sync with a pending approval")
    sid, frames, ids = _live_pause(client, "sync")
    if not ids:
        check("live L6: paused for the sync case", False, repr(_types(frames)))
        return
    r = client.post("/ai/chat/sync", json={
        "session_id": sid, "notebook_id": "",
        "message": "Reply with exactly the word PONG and nothing else. Do not call any tools."})
    resp = r.json().get("response", "") if r.status_code == 200 else ""
    check("live L6: sync returns 200 with the model answer",
          r.status_code == 200 and "PONG" in resp.upper(), f"HTTP {r.status_code}: {r.text[:200]}")
    check("live L6: sync answer carries no decline notice", _NOTICE_MARK not in resp, resp[:200])
    msgs, agent = _db_rows(sid, wait_for=lambda m, a: _db_activated(a) is False
                           and any("PONG" in json.dumps(x) for x in m))
    ok, why = history_valid(msgs)
    check("live L6: pending tool declined (one 'User denied'), history valid, deactivated",
          ok and len(denied_results(msgs)) == 1 and _db_activated(agent) is False,
          why or repr((denied_results(msgs), _db_activated(agent))))


def run_live():
    print("\n" + "=" * 72)
    print(" LIVE — real proxy + chat agent + Bedrock")
    print("=" * 72)
    client, reason = _live_preflight()
    if client is None:
        print(f"  - [SKIP] live cases skipped: {reason}")
        return
    print(f"  preflight OK: proxy {PROXY_URL} healthy, auth accepted, agent up, gate ON")
    try:
        live_case_autodecline(client)
        live_case_explicit_reject(client)
        live_case_sync(client)
    except Exception as e:  # noqa: BLE001
        check("live: no unexpected exception", False, f"{type(e).__name__}: {e}")
    finally:
        leftover = []
        for sid in _LIVE_SIDS:
            try:
                client.delete(f"/ai/chat/{sid}")
            except Exception:  # noqa: BLE001
                pass
            msgs, agent = _db_rows(sid, wait_for=lambda m, a: not m and not a, timeout=10)
            if msgs or agent:
                leftover.append(sid)
        check("live: every test session deleted", not leftover, repr(leftover))
        client.close()


def main():
    print("=" * 72)
    print(" chat_stream — auto-decline pending approval on a new message (offline)")
    print("=" * 72)
    cap = _Capture(logging.INFO)
    na_logger = logging.getLogger("ai_assistant.notebook_agent")
    na_logger.setLevel(logging.INFO)
    na_logger.addHandler(cap)
    otel = logging.getLogger("opentelemetry.context")
    otel.addHandler(cap)
    undo = _install_patches()
    try:
        asyncio.run(run(cap))
    finally:
        undo()
        na_logger.removeHandler(cap)
        otel.removeHandler(cap)
        for s in _SIDS:
            clear_session(s)
        memory.flush()
        shutil.rmtree(_TMP, ignore_errors=True)
    run_live()
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
