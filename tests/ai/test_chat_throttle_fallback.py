#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the AI Assistant chat's THROTTLE fallback + cooldown
(agent/ai_assistant/notebook_agent.py chat_stream).

WHAT THIS PROVES (no stack, no network, no live Bedrock)
--------------------------------------------------------
Drives notebook_agent.chat_stream() end-to-end through a REAL Strands Agent built
by get_or_create_agent, with:
  * a SCRIPTED model per (fake) model id that streams text or raises a Bedrock-shaped
    error (Strands ModelThrottledException / botocore ClientError),
  * a fixed 3-model fallback chain (model-a -> model-b -> model-c) and a fake
    resolve_model_id ("global." prefix), so the test never depends on the catalog,
  * a FileSessionManager in a temp dir as the durable SessionManager (so "history
    preserved, no duplicated user message" is checked on what was PERSISTED too),
  * Strands' ModelRetryStrategy delays patched to 0 (its retries still run; they
    just don't sleep), and an injected monotonic clock for the cooldown.
A guard makes any attempt to build a real BedrockModel fail the run.

Cases:
  classifier   _is_exhausted_throttle matches throttles (incl. the exact UI error
               text), not access-denied / temperature / 5xx-with-max-retries;
               _is_model_unavailable stays narrow (no throttles).
  cooldown     _ThrottleCooldown: expiry by clock, never cools the last usable
               model, seconds=0 disables, concurrent adds can't cool every model.
  open         throttle when opening the stream -> falls back to chain[1] with a
               visible note naming the reason; throttled model on cooldown; same
               agent/SessionManager; no duplicated user message (memory + disk).
  next turn    skips the cooling model immediately (no retries spent) with the
               cooldown note.
  expiry       after the cooldown (clock advanced) the selected model is tried again.
  all          every model throttled -> the ORIGINAL error surfaces; the last model
               is never put on cooldown; a following turn spends no calls on the
               cooled models.
  mid-stream   throttle after text streamed -> error (full Bedrock text), no model
               switch, model on cooldown; the agent runtime emits it as the error
               frame; the next turn falls back automatically.
  raw          a raw botocore ClientError ThrottlingException also falls back.
  denied       access-denied fallback works as before (note, no cooldown).
  temperature  temperature self-heal: same model, no temperature, no switch.
  absorbed     a throttle the retries absorb (and a clean first call) never falls back.
  retry budget the chat Agent is built with the SMALL ModelRetryStrategy (CHAT_MODEL_RETRY_*),
               get_model's boto config uses the small CHAT_BEDROCK_MAX_RETRIES budget (and
               the shared BEDROCK_MAX_RETRIES is unchanged), and a model that ALWAYS
               throttles falls back after the small budget with the REAL (unpatched)
               backoff delays — only asyncio.sleep is recorded, so the case stays fast.
  attachments  a turn with an image/document block in the live history skips fallback
               candidates that cannot accept it (vision / document capability); with no
               compatible candidate the original error is raised and logged; text-only
               turns are unaffected.

Run:
    .venv/bin/python tests/ai/test_chat_throttle_fallback.py
Exit 0 = all passed; 1 = at least one failed.
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import threading
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_REPO, "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

# Default cooldown (10 min) under test — make sure a shell override can't skew it.
os.environ.pop("AI_THROTTLE_COOLDOWN_SECONDS", None)
for _k in ("CHAT_MODEL_RETRY_MAX_ATTEMPTS", "CHAT_MODEL_RETRY_INITIAL_DELAY",
           "CHAT_MODEL_RETRY_MAX_DELAY", "CHAT_BEDROCK_MAX_RETRIES"):
    os.environ.pop(_k, None)

from botocore.exceptions import ClientError  # noqa: E402
from strands.models.model import Model  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402
from strands.event_loop._retry import ModelRetryStrategy  # noqa: E402
from strands.session.file_session_manager import FileSessionManager  # noqa: E402
from strands.types.exceptions import ModelThrottledException  # noqa: E402

from ai_assistant import notebook_agent as na  # noqa: E402
from ai_assistant import memory as mem  # noqa: E402
from ai_assistant import agent_runtime  # noqa: E402
from ai_assistant.constants import (  # noqa: E402
    AI_THROTTLE_COOLDOWN_SECONDS, CHAT_AGENT_ID, BEDROCK_MAX_RETRIES,
    CHAT_MODEL_RETRY_MAX_ATTEMPTS, CHAT_MODEL_RETRY_INITIAL_DELAY,
    CHAT_MODEL_RETRY_MAX_DELAY, CHAT_BEDROCK_MAX_RETRIES,
)
from strands.event_loop import _retry as strands_retry  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
from ai_assistant.sessions import get_session, clear_session  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)

# The exact error the user saw in the UI.
UI_THROTTLE_TEXT = ("An error occurred (ThrottlingException) when calling the ConverseStream "
                    "operation (reached max retries: 5): Too many tokens, please wait before "
                    "trying again.")

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail and not ok else ""))


def _client_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, "ConverseStream")


# ── Scripted models ──────────────────────────────────────────────────────────────

class ScriptedModel(Model):
    """Bedrock-protocol fake. ``plan`` is a list of actions consumed one per model
    call; the LAST action repeats forever. Actions:
      ok           stream "answer from <name>"
      throttle     raise ModelThrottledException (what BedrockModel raises for a
                   ThrottlingException), message = the UI text + [<name>]
      throttle_raw raise a raw botocore ClientError ThrottlingException
      denied       raise ClientError AccessDeniedException
      temperature  raise ClientError ValidationException rejecting temperature
      midstream    stream some text, THEN raise ModelThrottledException
      throttle_after_start  emit messageStart (no text), THEN raise
                   ModelThrottledException
    """

    def __init__(self, name, plan):
        self.name = name
        self.plan = list(plan)
        self.calls = 0
        self._cfg = {"model_id": "global." + name}

    def update_config(self, **cfg):
        self._cfg.update(cfg)

    def get_config(self):
        return self._cfg

    async def structured_output(self, output_model, prompt=None, **kwargs):
        raise NotImplementedError

    def _action(self):
        idx = min(self.calls, len(self.plan) - 1)
        self.calls += 1
        return self.plan[idx]

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        action = self._action()
        if action == "tool_then_text":
            # Approval flow: pause on a gated execute_code until a tool result exists
            # (the test denies it on resume, so nothing ever runs), then answer.
            has_result = any(isinstance(b, dict) and "toolResult" in b
                             for m in messages for b in (m.get("content") or []))
            if not has_result:
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {
                    "toolUse": {"toolUseId": "tool-" + uuid.uuid4().hex[:8],
                                "name": "execute_code"}}}}
                yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
                    "toolUse": {"input": '{"code": "print(2+2)"}'}}}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            action = "ok"
        if action == "throttle":
            raise ModelThrottledException(f"{UI_THROTTLE_TEXT} [{self.name}]")
        if action == "throttle_raw":
            raise _client_error("ThrottlingException",
                                f"Too many tokens, please wait before trying again. [{self.name}]")
        if action == "denied":
            raise _client_error("AccessDeniedException",
                                "You don't have access to the model with the specified model ID.")
        if action == "temperature":
            raise _client_error("ValidationException",
                                "This model doesn't support the temperature field.")
        yield {"messageStart": {"role": "assistant"}}
        if action == "throttle_after_start":
            raise ModelThrottledException(f"{UI_THROTTLE_TEXT} [{self.name}]")
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        if action == "midstream":
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                                         "delta": {"text": f"partial from {self.name} "}}}
            raise ModelThrottledException(f"{UI_THROTTLE_TEXT} [{self.name}]")
        yield {"contentBlockDelta": {"contentBlockIndex": 0,
                                     "delta": {"text": f"answer from {self.name}"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


_REAL_GET_MODEL = na.get_model                       # before _install_patches replaces it
_REAL_CALCULATE_DELAY = ModelRetryStrategy._calculate_delay
_REGISTRY: dict = {}
_BEDROCK_BUILT: list = []
_TMP = tempfile.mkdtemp(prefix="chat-throttle-test-")


def _fake_resolve(model_id, region=None):
    return model_id if model_id.startswith("global.") else "global." + model_id


def _fake_chain(requested, region, default_id=None, exclude=None, **kw):
    base = ["model-a", "model-b", "model-c"]
    req = requested[len("global."):] if requested.startswith("global.") else requested
    return [req] + [m for m in base if m != req]


def _fake_get_model(model_id=None):
    resolved = _fake_resolve(model_id or "model-a")
    if resolved not in _REGISTRY:
        _BEDROCK_BUILT.append(resolved)
        raise AssertionError(f"unexpected model build {resolved}")
    return _REGISTRY[resolved]


def _install_patches():
    """Hermetic patches; returns an undo callable."""
    saved = {
        "resolve": na.resolve_model_id, "chain": na.fallback_chain,
        "get_model": na.get_model, "cooldown": na._throttle_cooldown,
        "make_sm": mem.make_session_manager, "bm_init": BedrockModel.__init__,
        "delay": ModelRetryStrategy._calculate_delay,
    }

    def _no_bedrock(self, *a, **kw):
        _BEDROCK_BUILT.append(kw.get("model_id"))
        raise AssertionError("real Bedrock model constructed in an offline test")

    na.resolve_model_id = _fake_resolve
    na.fallback_chain = _fake_chain
    na.get_model = _fake_get_model
    mem.make_session_manager = lambda sid, notebook_id=None: FileSessionManager(
        session_id=sid, storage_dir=_TMP)
    BedrockModel.__init__ = _no_bedrock
    ModelRetryStrategy._calculate_delay = lambda self, attempt: 0  # retries run, no sleep

    def _undo():
        na.resolve_model_id = saved["resolve"]
        na.fallback_chain = saved["chain"]
        na.get_model = saved["get_model"]
        na._throttle_cooldown = saved["cooldown"]
        mem.make_session_manager = saved["make_sm"]
        BedrockModel.__init__ = saved["bm_init"]
        ModelRetryStrategy._calculate_delay = saved["delay"]
    return _undo


def _fresh(plans):
    """New scripted models + a fresh cooldown on a fake clock + a new session id."""
    _REGISTRY.clear()
    models = {}
    for name, plan in plans.items():
        models[name] = ScriptedModel(name, plan)
        _REGISTRY["global." + name] = models[name]
    clock = FakeClock()
    na._throttle_cooldown = na._ThrottleCooldown(clock=clock)
    sid = "thr-" + uuid.uuid4().hex[:12]
    return models, clock, sid


def _ctx(model_id="model-a", approval_channel=True):
    return {"model_id": model_id, "approval_channel": approval_channel,
            "proxy_url": "http://localhost:8081"}


async def _turn(sid, message="hello", model_id="model-a", approval_channel=True):
    frames, err = [], None
    try:
        async for fr in na.chat_stream(sid, message, _ctx(model_id, approval_channel)):
            frames.append(fr)
    except Exception as e:  # noqa: BLE001 — the test inspects it
        err = e
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "text")
    return frames, text, err


def _roles(messages):
    return [m.get("role") for m in messages]


def _persisted_roles(sid):
    mem.flush_session(sid)
    repo = FileSessionManager(session_id=sid, storage_dir=_TMP)
    return [sm.message["role"] for sm in repo.list_messages(sid, CHAT_AGENT_ID)]


# ── Cases ────────────────────────────────────────────────────────────────────────

def case_classifier():
    print("\n[classifier]")
    t = na._is_exhausted_throttle
    record("ModelThrottledException (exact UI text) is a throttle",
           t(ModelThrottledException(UI_THROTTLE_TEXT)))
    record("raw ClientError ThrottlingException is a throttle",
           t(_client_error("ThrottlingException", "Too many tokens")))
    record("ClientError TooManyRequestsException is a throttle",
           t(_client_error("TooManyRequestsException", "slow down")))
    record("message-only 'Too many tokens' error is a throttle",
           t(RuntimeError(UI_THROTTLE_TEXT)))
    try:
        try:
            raise _client_error("ThrottlingException", "x")
        except ClientError as ce:
            raise RuntimeError("wrapped") from ce
    except RuntimeError as wrapped:
        record("wrapper raised `from` a ThrottlingException is a throttle", t(wrapped))
    record("access denied is NOT a throttle",
           not t(_client_error("AccessDeniedException", "You don't have access to the model")))
    record("temperature rejection is NOT a throttle",
           not t(_client_error("ValidationException", "temperature is not supported")))
    record("5xx 'reached max retries' (no throttle) is NOT a throttle",
           not t(RuntimeError("An error occurred (InternalServerException) when calling the "
                              "ConverseStream operation (reached max retries: 5): boom")))
    record("_is_model_unavailable stays narrow (throttle is not 'unavailable')",
           not na._is_model_unavailable(ModelThrottledException(UI_THROTTLE_TEXT))
           and not na._is_model_unavailable(_client_error("ThrottlingException", "Too many tokens")))


def case_cooldown_unit():
    print("\n[cooldown unit]")
    record("default cooldown is 10 minutes", AI_THROTTLE_COOLDOWN_SECONDS == 600,
           f"got {AI_THROTTLE_COOLDOWN_SECONDS}")
    clock = FakeClock()
    cd = na._ThrottleCooldown(clock=clock)
    pool = ["a", "b"]
    record("add with another usable model applies", cd.add_unless_last("a", pool, 600))
    record("cooling after add", cd.is_cooling("a") and abs(cd.remaining("a") - 600) < 1e-6)
    record("last usable model is never cooled", not cd.add_unless_last("b", pool, 600)
           and not cd.is_cooling("b"))
    record("single-model chain is never cooled", not cd.add_unless_last("z", ["z"], 600))
    record("seconds=0 disables the cooldown", not cd.add_unless_last("b", ["b", "c"], 0))
    clock.t += 599
    record("still cooling just before expiry", cd.is_cooling("a"))
    clock.t += 2
    record("expired by monotonic clock", not cd.is_cooling("a") and cd.remaining("a") == 0.0)

    # Concurrent adds over a 2-model pool: exactly ONE may be cooled.
    for _ in range(50):
        cd2 = na._ThrottleCooldown(clock=FakeClock())
        barrier = threading.Barrier(2)
        out = []

        def _w(mid):
            barrier.wait()
            out.append(cd2.add_unless_last(mid, ["x", "y"], 600))
        ths = [threading.Thread(target=_w, args=(m,)) for m in ("x", "y")]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        if sorted(out) != [False, True]:
            record("concurrent adds never cool every model", False, f"out={out}")
            break
    else:
        record("concurrent adds never cool every model", True)


async def case_open_throttle_and_cooldown():
    print("\n[open throttle -> fallback, cooldown, expiry]")
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    a, b = models["model-a"], models["model-b"]

    frames, text, err = await _turn(sid)
    record("turn 1: no error surfaced", err is None, repr(err))
    record("turn 1: answered by chain[1] (model-b)", "answer from model-b" in text, text)
    record("turn 1: visible note names the reason + target",
           "model-a is throttled for this account" in text and "switched to model-b" in text,
           text)
    record("turn 1: note precedes the answer",
           text.find("throttled") < text.find("answer from model-b"), text)
    record("turn 1: model-a was retried (Strands retries) before falling back", a.calls >= 2,
           f"calls={a.calls}")
    record("turn 1: model-a (resolved id) is on the cooldown",
           na._throttle_cooldown.is_cooling("global.model-a"))
    record("turn 1: model-b NOT on cooldown", not na._throttle_cooldown.is_cooling("global.model-b"))
    agent = get_session(sid)
    record("turn 1: same session agent now on model-b",
           agent is not None and agent._mvmnb_model_id == "global.model-b"
           and agent.model is b)
    sm = getattr(agent, "_session_manager", None)
    record("turn 1: in-memory history has no duplicated user message",
           _roles(agent.messages) == ["user", "assistant"], _roles(agent.messages))
    record("turn 1: persisted history has no duplicated user message",
           _persisted_roles(sid) == ["user", "assistant"], _persisted_roles(sid))

    a_calls = a.calls
    frames, text, err = await _turn(sid, "second question")
    record("turn 2: no error", err is None, repr(err))
    record("turn 2: cooling model skipped immediately (no calls/retries spent)",
           a.calls == a_calls, f"calls {a_calls} -> {a.calls}")
    record("turn 2: no cooldown note (same episode, switch note already shown)",
           "cooling down" not in text and "model-a is throttled" not in text, text)
    record("turn 2: answered by model-b", "answer from model-b" in text, text)
    agent2 = get_session(sid)
    record("turn 2: same agent + SessionManager reused (no rebuild)",
           agent2 is agent and getattr(agent2, "_session_manager", None) is sm)
    record("turn 2: history alternates (memory + disk)",
           _roles(agent2.messages) == ["user", "assistant"] * 2
           and _persisted_roles(sid) == ["user", "assistant"] * 2,
           f"{_roles(agent2.messages)} / {_persisted_roles(sid)}")

    # Cooldown expiry: the selected model is tried again.
    clock.t += AI_THROTTLE_COOLDOWN_SECONDS + 1
    a.plan = ["ok"]
    a.calls = 0
    frames, text, err = await _turn(sid, "third question")
    record("expiry: model-a tried again after the cooldown", a.calls == 1, f"calls={a.calls}")
    record("expiry: answered by model-a with no switch note",
           "answer from model-a" in text and "throttled" not in text and "cooling" not in text,
           text)
    clear_session(sid)


async def case_all_throttled():
    print("\n[all models throttled]")
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["throttle"],
                                 "model-c": ["throttle"]})
    frames, text, err = await _turn(sid)
    record("error surfaced", err is not None)
    record("ORIGINAL (selected model's) error surfaced, full text",
           err is not None and UI_THROTTLE_TEXT in str(err) and "[model-a]" in str(err),
           str(err))
    record("every model was tried", all(m.calls >= 1 for m in models.values()),
           {k: m.calls for k, m in models.items()})
    cd = na._throttle_cooldown
    record("model-a and model-b on cooldown",
           cd.is_cooling("global.model-a") and cd.is_cooling("global.model-b"))
    record("LAST usable model (model-c) never put on cooldown",
           not cd.is_cooling("global.model-c"))
    record("no visible text frames before the error", text == "", text)

    calls = {k: m.calls for k, m in models.items()}
    frames, text, err = await _turn(sid, "again")
    record("next turn: error surfaced again", err is not None and UI_THROTTLE_TEXT in str(err),
           str(err))
    record("next turn: cooled models spent no calls",
           models["model-a"].calls == calls["model-a"]
           and models["model-b"].calls == calls["model-b"]
           and models["model-c"].calls > calls["model-c"],
           {k: (calls[k], m.calls) for k, m in models.items()})
    record("next turn: model-c still never cooled", not cd.is_cooling("global.model-c"))
    clear_session(sid)


async def case_midstream():
    print("\n[mid-stream throttle]")
    models, clock, sid = _fresh({"model-a": ["midstream"], "model-b": ["ok"], "model-c": ["ok"]})
    b = models["model-b"]
    frames, text, err = await _turn(sid)
    record("content had started streaming", "partial from model-a" in text, text)
    record("error raised is ModelThrottledMidStreamError",
           isinstance(err, na.ModelThrottledMidStreamError), repr(err))
    msg = str(err)
    record("error message is clear + keeps the FULL Bedrock error text",
           "model-a was throttled for this account partway through this answer" in msg
           and UI_THROTTLE_TEXT in msg and "[model-a]" in msg, msg)
    record("error tells the user the next message will use model-b", "model-b" in msg, msg)
    record("no model switch mid-answer (model-b never called, no switch note)",
           b.calls == 0 and "switched to" not in text, f"b.calls={b.calls}")
    record("model-a on cooldown", na._throttle_cooldown.is_cooling("global.model-a"))

    # Agent runtime turns it into the SSE error frame (full text, untruncated).
    models, clock, sid_rt = _fresh({"model-a": ["midstream"], "model-b": ["ok"],
                                    "model-c": ["ok"]})
    rt_frames = []
    async for fr in agent_runtime.invoke({"action": "chat", "session_id": sid_rt,
                                          "message": "hi", "context": _ctx()}):
        rt_frames.append(fr)
    errs = [f for f in rt_frames if f.get("type") == "error"]
    record("runtime: one error frame, no done frame",
           len(errs) == 1 and not any(f.get("type") == "done" for f in rt_frames),
           [f.get("type") for f in rt_frames])
    record("runtime: error frame carries the full Bedrock text",
           bool(errs) and UI_THROTTLE_TEXT in errs[0]["content"]
           and "partway through this answer" in errs[0]["content"],
           errs[0]["content"] if errs else "")

    a_calls = models["model-a"].calls
    frames, text, err = await _turn(sid_rt, "next")
    record("next turn: falls back automatically (no error)", err is None, repr(err))
    record("next turn: answered by model-b with the cooldown note",
           "answer from model-b" in text and "model-a is cooling down" in text, text)
    record("next turn: model-a not called", models["model-a"].calls == a_calls)
    agent = get_session(sid_rt)
    # NOTE: a turn that ERRORS (any error, not only throttles — pre-existing Strands
    # behavior) leaves its user message in history without an assistant reply, so
    # history here is [user(failed turn), user, assistant]. Out of scope for this
    # change; we only assert the fallback turn itself completed and was recorded once.
    roles = _roles(agent.messages)
    record("next turn: completed with one new user + assistant message",
           roles[-2:] == ["user", "assistant"] and _persisted_roles(sid_rt) == roles, roles)
    clear_session(sid)
    clear_session(sid_rt)


async def case_raw_client_error_throttle():
    print("\n[raw ClientError throttle at open]")
    models, clock, sid = _fresh({"model-a": ["throttle_raw"], "model-b": ["ok"],
                                 "model-c": ["ok"]})
    frames, text, err = await _turn(sid)
    record("falls back to model-b", err is None and "answer from model-b" in text,
           f"{err!r} {text}")
    record("model-a on cooldown", na._throttle_cooldown.is_cooling("global.model-a"))
    clear_session(sid)


async def case_access_denied():
    print("\n[access denied fallback — unchanged behavior]")
    models, clock, sid = _fresh({"model-a": ["denied"], "model-b": ["ok"], "model-c": ["ok"]})
    a = models["model-a"]
    frames, text, err = await _turn(sid)
    record("no error", err is None, repr(err))
    record("unavailable note + answered by model-b",
           "Selected model was unavailable; using model-b instead." in text
           and "answer from model-b" in text, text)
    record("access-denied model NOT put on the throttle cooldown",
           not na._throttle_cooldown.is_cooling("global.model-a"))
    record("history alternates", _roles(get_session(sid).messages) == ["user", "assistant"],
           _roles(get_session(sid).messages))
    calls = a.calls
    frames, text, err = await _turn(sid, "again")
    record("next turn: selected model is tried again (as before)",
           a.calls == calls + 1 and "answer from model-b" in text, f"calls={a.calls}")
    clear_session(sid)


async def case_temperature():
    print("\n[temperature self-heal — unchanged behavior]")
    models, clock, sid = _fresh({"model-a": ["temperature", "ok"], "model-b": ["ok"],
                                 "model-c": ["ok"]})
    na._no_temperature.discard("global.model-a")
    frames, text, err = await _turn(sid)
    record("no error", err is None, repr(err))
    record("same model answers after the retry", "answer from model-a" in text, text)
    record("no switch / throttle note", "unavailable" not in text and "throttled" not in text,
           text)
    record("model recorded as temperature-less", "global.model-a" in na._no_temperature)
    record("fallback model never called", models["model-b"].calls == 0)
    record("not on the throttle cooldown", not na._throttle_cooldown.is_cooling("global.model-a"))
    record("history alternates", _roles(get_session(sid).messages) == ["user", "assistant"],
           _roles(get_session(sid).messages))
    na._no_temperature.discard("global.model-a")
    clear_session(sid)


async def case_absorbed():
    print("\n[short throttle absorbed by retries / first call succeeds]")
    models, clock, sid = _fresh({"model-a": ["throttle", "ok"], "model-b": ["ok"],
                                 "model-c": ["ok"]})
    frames, text, err = await _turn(sid)
    record("absorbed throttle: no error", err is None, repr(err))
    record("absorbed throttle: answered by model-a, no note",
           "answer from model-a" in text and "throttled" not in text, text)
    record("absorbed throttle: no fallback, no cooldown",
           models["model-b"].calls == 0
           and not na._throttle_cooldown.is_cooling("global.model-a"))
    clear_session(sid)

    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    frames, text, err = await _turn(sid)
    record("clean first call: answered by model-a, single call, no note",
           err is None and text == "answer from model-a" and models["model-a"].calls == 1, text)
    clear_session(sid)


async def case_sync_channel():
    print("\n[/ai/chat/sync (approval_channel=False) — answer only, no notes]")
    # Open throttle -> fallback: the aggregated sync response is ONLY the answer.
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    frames, text, err = await _turn(sid, approval_channel=False)
    record("sync fallback: no error", err is None, repr(err))
    record("sync fallback: response is exactly the fallback model's answer",
           text == "answer from model-b", repr(text))
    record("sync fallback: model-a still put on cooldown",
           na._throttle_cooldown.is_cooling("global.model-a"))

    # Cooldown skip on a later sync turn: still answer only.
    frames, text, err = await _turn(sid, "second", approval_channel=False)
    record("sync cooldown skip: response is exactly the answer (no cooling note)",
           err is None and text == "answer from model-b", f"{err!r} {text!r}")

    # Same session on the interactive channel still shows the note.
    frames, text, err = await _turn(sid, "third", approval_channel=True)
    record("interactive channel still shows the cooldown note",
           "model-a is cooling down" in text and "answer from model-b" in text, text)
    clear_session(sid)

    # Access-denied fallback on sync: answer only.
    models, clock, sid = _fresh({"model-a": ["denied"], "model-b": ["ok"], "model-c": ["ok"]})
    frames, text, err = await _turn(sid, approval_channel=False)
    record("sync unavailable fallback: response is exactly the answer",
           err is None and text == "answer from model-b", f"{err!r} {text!r}")
    clear_session(sid)

    # Fallback model throttles after messageStart but before any text: the sync turn
    # must yield NO text frames (so the proxy reports an error, not a note-only 200).
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["throttle_after_start"],
                                 "model-c": ["ok"]})
    frames, text, err = await _turn(sid, approval_channel=False)
    record("sync: fallback throttled before text -> error, no text frames",
           err is not None and text == "", f"{err!r} {text!r}")
    clear_session(sid)


async def case_retry_budget():
    print("\n[small retry budget: chat Agent strategy, boto config, fast always-throttle fallback]")
    record("constants: chat retry defaults are 2 attempts / 2s / 10s / 2 boto attempts",
           (CHAT_MODEL_RETRY_MAX_ATTEMPTS, CHAT_MODEL_RETRY_INITIAL_DELAY,
            CHAT_MODEL_RETRY_MAX_DELAY, CHAT_BEDROCK_MAX_RETRIES) == (2, 2, 10, 2),
           (CHAT_MODEL_RETRY_MAX_ATTEMPTS, CHAT_MODEL_RETRY_INITIAL_DELAY,
            CHAT_MODEL_RETRY_MAX_DELAY, CHAT_BEDROCK_MAX_RETRIES))
    record("constants: shared BEDROCK_MAX_RETRIES (other call sites) is unchanged at 5",
           BEDROCK_MAX_RETRIES == 5, BEDROCK_MAX_RETRIES)

    # (1) The chat Agent is constructed with the configured small strategy.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    agent = na.get_or_create_agent(sid, _ctx())
    rs = agent._retry_strategy
    record("agent: built with a ModelRetryStrategy", isinstance(rs, ModelRetryStrategy), type(rs))
    record("agent: strategy max_attempts/initial_delay/max_delay = configured chat values",
           (rs._max_attempts, rs._initial_delay, rs._max_delay)
           == (CHAT_MODEL_RETRY_MAX_ATTEMPTS, CHAT_MODEL_RETRY_INITIAL_DELAY,
               CHAT_MODEL_RETRY_MAX_DELAY),
           (rs._max_attempts, rs._initial_delay, rs._max_delay))
    record("agent: strategy is far smaller than Strands' defaults (6 / 4s / 240s)",
           rs._max_attempts < 6 and rs._max_delay < 240)
    clear_session(sid)

    # (2) get_model's boto config: run the REAL get_model, capturing the BedrockModel kwargs.
    captured = {}

    def _capture_init(self, *a, **kw):
        captured.update(kw)

    saved_init = BedrockModel.__init__
    saved_models = dict(na._models)
    try:
        BedrockModel.__init__ = _capture_init
        na._models.clear()
        _REAL_GET_MODEL("model-retry-probe")
    finally:
        BedrockModel.__init__ = saved_init
        na._models.clear()
        na._models.update(saved_models)
    cfg = captured.get("boto_client_config")
    retries = getattr(cfg, "retries", None) or {}
    record("get_model: boto retries = chat budget, standard mode",
           retries.get("total_max_attempts") == CHAT_BEDROCK_MAX_RETRIES
           and retries.get("mode") == "standard", retries)
    record("get_model: boto retries are NOT the shared 5-retry budget",
           retries.get("total_max_attempts") != BEDROCK_MAX_RETRIES
           and "max_attempts" not in retries, retries)
    record("get_model: read/connect timeouts unchanged",
           getattr(cfg, "read_timeout", None) == 120 and getattr(cfg, "connect_timeout", None) == 10,
           (getattr(cfg, "read_timeout", None), getattr(cfg, "connect_timeout", None)))

    # (3) Always-throttle end to end, with the REAL backoff delays: only asyncio.sleep (as
    # seen by the Strands retry module) is replaced by a recorder, so wall time stays small.
    sleeps = []

    async def _rec_sleep(d):
        sleeps.append(d)

    saved_delay = ModelRetryStrategy._calculate_delay
    saved_asyncio = strands_retry.asyncio
    ModelRetryStrategy._calculate_delay = _REAL_CALCULATE_DELAY
    strands_retry.asyncio = types.SimpleNamespace(sleep=_rec_sleep)
    try:
        models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
        t0 = time.monotonic()
        frames, text, err = await _turn(sid)
        elapsed = time.monotonic() - t0
    finally:
        ModelRetryStrategy._calculate_delay = saved_delay
        strands_retry.asyncio = saved_asyncio
    a = models["model-a"]
    old_budget = 4 + 8 + 16 + 32 + 64  # Strands defaults: 6 attempts, 5 backoff sleeps
    print(f"      always-throttle: model-a calls={a.calls}, backoff sleeps={sleeps}, "
          f"wall={elapsed:.3f}s (old default backoff alone: {old_budget}s)")
    record("always-throttle: falls back to model-b with the note",
           err is None and "answer from model-b" in text and "switched to model-b" in text,
           f"{err!r} {text}")
    record("always-throttle: model-a attempted exactly CHAT_MODEL_RETRY_MAX_ATTEMPTS times",
           a.calls == CHAT_MODEL_RETRY_MAX_ATTEMPTS, f"calls={a.calls}")
    record("always-throttle: real backoff delays are the configured ones (one 2s wait)",
           sleeps == [CHAT_MODEL_RETRY_INITIAL_DELAY], sleeps)
    record("always-throttle: total backoff far below the old ~2 minutes",
           sum(sleeps) <= CHAT_MODEL_RETRY_MAX_DELAY and sum(sleeps) < old_budget / 5, sum(sleeps))
    record("always-throttle: scripted turn completes quickly (< 5s wall)", elapsed < 5.0,
           f"{elapsed:.3f}s")
    record("always-throttle: model-a on cooldown after the small budget",
           na._throttle_cooldown.is_cooling("global.model-a"))
    clear_session(sid)


IMAGE_BLOCK = {"image": {"format": "png", "source": {"bytes": b"\x89PNG-test"}}}
DOC_BLOCK = {"document": {"format": "pdf", "name": "report",
                          "source": {"bytes": b"%PDF-test"}}}


def _with_attachment_caps(vision: dict, documents: dict, blocks: list):
    """Scripted capability table (resolved id -> bool) plus a user prompt that carries
    the given attachment blocks. Returns the saved originals for _restore_caps."""
    saved = (na.supports_vision, na.supports_document, na._build_message_content)
    na.supports_vision = lambda r: vision.get(r, False)
    na.supports_document = lambda r: documents.get(r, False)
    na._build_message_content = lambda message, context: [{"text": message}, *blocks]
    return saved


def _restore_caps(saved):
    na.supports_vision, na.supports_document, na._build_message_content = saved


def _live_kinds(sid):
    return na._turn_attachment_kinds(get_session(sid))


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def case_attachments():
    print("\n[attachments: in-turn fallback skips models that cannot accept them]")
    # (a) throttled primary; the middle model has no vision, the last one does.
    vision = {"global.model-a": True, "global.model-b": False, "global.model-c": True}
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    saved = _with_attachment_caps(vision, {}, [IMAGE_BLOCK])
    try:
        frames, text, err = await _turn(sid, "what is in this picture?")
    finally:
        _restore_caps(saved)
    record("(a) image turn, throttled: no error", err is None, repr(err))
    record("(a) image turn, throttled: non-vision model-b skipped (never called)",
           models["model-b"].calls == 0, f"b.calls={models['model-b'].calls}")
    record("(a) image turn, throttled: lands on vision model-c",
           "answer from model-c" in text and "switched to model-c" in text, text)
    record("(a) image bytes stay in the live history (not stripped)",
           _live_kinds(sid) == {"image"}, repr(_live_kinds(sid)))
    clear_session(sid)

    # (a2) unavailable (access denied) primary: same skip, different note.
    models, clock, sid = _fresh({"model-a": ["denied"], "model-b": ["ok"], "model-c": ["ok"]})
    saved = _with_attachment_caps(vision, {}, [IMAGE_BLOCK])
    try:
        frames, text, err = await _turn(sid, "describe this")
    finally:
        _restore_caps(saved)
    record("(a2) image turn, unavailable: no error", err is None, repr(err))
    record("(a2) image turn, unavailable: model-b skipped, model-c answers",
           models["model-b"].calls == 0 and "answer from model-c" in text
           and "Selected model was unavailable; using model-c instead." in text, text)
    clear_session(sid)

    # (a3) document turn: a vision-capable model without document support is skipped.
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    saved = _with_attachment_caps({"global.model-b": True},
                                  {"global.model-c": True}, [DOC_BLOCK])
    try:
        frames, text, err = await _turn(sid, "summarise the report")
    finally:
        _restore_caps(saved)
    record("(a3) document turn: no error", err is None, repr(err))
    record("(a3) document turn: model-b (vision only) skipped, model-c answers",
           models["model-b"].calls == 0 and "answer from model-c" in text, text)
    clear_session(sid)

    # (b) no compatible candidate left: the ORIGINAL error is raised, nothing is stripped,
    # and the log plus the error note say why.
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    saved = _with_attachment_caps({"global.model-a": True}, {}, [IMAGE_BLOCK])
    cap = _LogCapture()
    na.logger.addHandler(cap)
    try:
        frames, text, err = await _turn(sid, "what is in this picture?")
    finally:
        na.logger.removeHandler(cap)
        _restore_caps(saved)
    record("(b) image turn, no compatible model: original error raised",
           err is not None and UI_THROTTLE_TEXT in str(err) and "[model-a]" in str(err),
           repr(err))
    record("(b) no incompatible model was called",
           models["model-b"].calls == 0 and models["model-c"].calls == 0,
           {k: m.calls for k, m in models.items()})
    record("(b) log names the attachment and the models that cannot take it",
           any("No compatible fallback model exists" in m and "image" in m
               and "model-b" in m and "model-c" in m for m in cap.messages),
           cap.messages)
    record("(b) the error message itself carries the explanation (what the runtime shows)",
           "No compatible fallback model exists" in str(err), str(err))
    record("(b) image left in the live history (attachment not stripped)",
           _live_kinds(sid) == {"image"}, repr(_live_kinds(sid)))
    clear_session(sid)

    # (b2) the only compatible candidate is on the throttle cooldown: the message must say
    # the compatible models are cooling, NOT that no compatible model exists.
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    na._throttle_cooldown.add_unless_last(
        "global.model-c", ["global.model-a", "global.model-b", "global.model-c"], 600)
    saved = _with_attachment_caps({"global.model-c": True}, {}, [IMAGE_BLOCK])
    cap = _LogCapture()
    na.logger.addHandler(cap)
    try:
        frames, text, err = await _turn(sid, "what is in this picture?")
    finally:
        na.logger.removeHandler(cap)
        _restore_caps(saved)
    record("(b2) image turn, compatible model cooling: original error raised",
           err is not None and UI_THROTTLE_TEXT in str(err), repr(err))
    record("(b2) message says the compatible model is on cooldown, not 'none exists'",
           "model-c" in str(err) and "on throttle cooldown" in str(err)
           and "No compatible fallback model exists" not in str(err), str(err))
    record("(b2) cooling model-c and non-vision model-b were never called",
           models["model-b"].calls == 0 and models["model-c"].calls == 0,
           {k: m.calls for k, m in models.items()})
    record("(b2) log carries the same cooldown explanation",
           any("on throttle cooldown" in m for m in cap.messages), cap.messages)
    clear_session(sid)

    # (c) no attachments: the capability table is irrelevant; fallback is unchanged.
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    saved = _with_attachment_caps({"global.model-b": False}, {}, [])
    try:
        frames, text, err = await _turn(sid, "plain question")
    finally:
        _restore_caps(saved)
    record("(c) text-only turn: no attachment kinds detected", _live_kinds(sid) == set(),
           repr(_live_kinds(sid)))
    record("(c) text-only turn: falls back to model-b as before (no capability gate)",
           err is None and "answer from model-b" in text
           and "switched to model-b" in text, text)
    clear_session(sid)


def _statuses(frames):
    return [f for f in frames if f.get("type") == "model_status"]


def _notes_count(text):
    return text.count("cooling down after being throttled")


def _cool(name, seconds=None):
    """Put a model on the (fake-clock) throttle cooldown, as a real throttle would."""
    chain = [f"global.{n}" for n in ("model-a", "model-b", "model-c")]
    return na._throttle_cooldown.add_unless_last(
        f"global.{name}", chain, AI_THROTTLE_COOLDOWN_SECONDS if seconds is None else seconds)


async def _resume(session_id, interrupt_id, response="n", model_id="model-a"):
    frames, err = [], None
    try:
        async for fr in na.chat_stream(session_id, "", _ctx(model_id),
                                       interrupt_responses=[{"interruptId": interrupt_id,
                                                             "response": response}]):
            frames.append(fr)
    except Exception as e:  # noqa: BLE001
        err = e
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "text")
    return frames, text, err


async def case_cooldown_note_once():
    print("\n[cooldown note once per episode per session + model_status events]")

    # (f) no fallback in effect: one status with active == requested, reason None.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    frames, text, err = await _turn(sid, "plain")
    st = _statuses(frames)
    record("(f) no fallback: exactly one model_status, first frame of the turn",
           len(st) == 1 and frames[0] is st[0], [f.get("type") for f in frames])
    record("(f) no fallback: requested == active == base id, reason/cooldown null",
           st and st[0] == {"type": "model_status", "requested": "model-a", "active": "model-a",
                            "reason": None, "cooldown_remaining_s": None}, st)
    clear_session(sid)

    # (a) cooldown skip: note once, then quiet on resumes and later turns in the episode.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["tool_then_text"],
                                 "model-c": ["ok"]})
    _cool("model-a")
    frames, text, err = await _turn(sid, "first question")
    record("(a) turn 1: no error", err is None, repr(err))
    record("(a) turn 1: cooldown note shown once", _notes_count(text) == 1, text)
    record("(a) turn 1: first frame is the model_status for the skip",
           frames and frames[0].get("type") == "model_status", [f.get("type") for f in frames])
    st = _statuses(frames)
    record("(a) turn 1: status = cooldown, active model-b, ~600 s left",
           len(st) == 1 and st[0]["requested"] == "model-a" and st[0]["active"] == "model-b"
           and st[0]["reason"] == "cooldown" and st[0]["cooldown_remaining_s"] == 600, st)
    interrupts = [f for f in frames if f.get("type") == "interrupt"]
    record("(a) turn 1: paused on the approval gate (model-b asked to run code)",
           len(interrupts) == 1, [f.get("type") for f in frames])
    if interrupts:
        iid = interrupts[0]["interrupt_ids"][0]
        frames, text, err = await _resume(sid, iid, "n")
        record("(a) resume 1: no error", err is None, repr(err))
        record("(a) resume 1: NO cooldown note repeated", _notes_count(text) == 0, text)
        st = _statuses(frames)
        record("(a) resume 1: model_status still emitted (badge stays correct)",
               len(st) == 1 and st[0]["reason"] == "cooldown" and st[0]["active"] == "model-b",
               st)
        record("(a) resume 1: answered by model-b", "answer from model-b" in text, text)
    frames, text, err = await _turn(sid, "second question")
    record("(a) turn 2: no cooldown note", _notes_count(text) == 0, text)
    record("(a) turn 2: model_status still emitted", len(_statuses(frames)) == 1, frames)
    frames, text, err = await _turn(sid, "third question")
    record("(a) turn 3: no cooldown note", _notes_count(text) == 0, text)
    clear_session(sid)

    # (b) cooldown expires and the model is throttled again -> a NEW episode -> note again.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    _cool("model-a")
    frames, text, err = await _turn(sid, "q1")
    record("(b) episode 1: note shown", _notes_count(text) == 1, text)
    frames, text, err = await _turn(sid, "q2")
    record("(b) episode 1: quiet on the next turn", _notes_count(text) == 0, text)
    clock.t += AI_THROTTLE_COOLDOWN_SECONDS + 1
    _cool("model-a")  # throttled again after the expiry: a different expiry
    frames, text, err = await _turn(sid, "q3")
    record("(b) episode 2 (re-throttled): note shown once again",
           _notes_count(text) == 1, text)
    frames, text, err = await _turn(sid, "q4")
    record("(b) episode 2: quiet afterwards", _notes_count(text) == 0, text)
    clear_session(sid)

    # (c) a DIFFERENT skipped model gets its own note in the same session.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    _cool("model-a")
    _cool("model-b")
    frames, text, err = await _turn(sid, "dropdown on A", model_id="model-a")
    record("(c) model-a skipped: its note shown", "model-a is cooling down" in text, text)
    frames, text, err = await _turn(sid, "dropdown on B", model_id="model-b")
    record("(c) model-b skipped: its own note shown", "model-b is cooling down" in text, text)
    frames, text, err = await _turn(sid, "dropdown on A again", model_id="model-a")
    record("(c) model-a again in the same episode: quiet", _notes_count(text) == 0, text)
    clear_session(sid)

    # (d) clearing the session (new thread / DELETE) resets the note.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    _cool("model-a")
    frames, text, err = await _turn(sid, "before clear")
    record("(d) before clear: note shown", _notes_count(text) == 1, text)
    frames, text, err = await _turn(sid, "still same chat")
    record("(d) before clear: quiet", _notes_count(text) == 0, text)
    na.new_thread(sid)
    frames, text, err = await _turn(sid, "new chat")
    record("(d) after new_thread: note shown again", _notes_count(text) == 1, text)
    clear_session(sid)

    # (e) a throttle-switch note announces the episode: later turns stay quiet.
    models, clock, sid = _fresh({"model-a": ["throttle"], "model-b": ["ok"], "model-c": ["ok"]})
    frames, text, err = await _turn(sid, "switch")
    st = _statuses(frames)
    record("(e) switch turn: switch note shown", "switched to model-b" in text, text)
    record("(e) switch turn: status goes to throttled + active model-b, ~600 s",
           len(st) == 2 and st[0]["reason"] is None and st[1]["reason"] == "throttled"
           and st[1]["active"] == "model-b" and st[1]["requested"] == "model-a"
           and st[1]["cooldown_remaining_s"] == 600, st)
    frames, text, err = await _turn(sid, "later")
    record("(e) later turn in the episode: no cooldown note", _notes_count(text) == 0
           and "cooling down" not in text, text)
    st = _statuses(frames)
    record("(e) later turn: status still says cooldown/active model-b",
           len(st) == 1 and st[0]["reason"] == "cooldown" and st[0]["active"] == "model-b", st)
    clear_session(sid)

    # (g) /ai/chat/sync: no notes and no model_status frames, even in an episode.
    models, clock, sid = _fresh({"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]})
    _cool("model-a")
    frames, text, err = await _turn(sid, "sync", approval_channel=False)
    record("(g) sync: answer only, no cooldown note", text == "answer from model-b", repr(text))
    record("(g) sync: no model_status frame", not _statuses(frames),
           [f.get("type") for f in frames])
    clear_session(sid)


async def run_async():
    await case_cooldown_note_once()
    await case_sync_channel()
    await case_open_throttle_and_cooldown()
    await case_all_throttled()
    await case_midstream()
    await case_raw_client_error_throttle()
    await case_access_denied()
    await case_temperature()
    await case_absorbed()
    await case_retry_budget()
    await case_attachments()


def main():
    print("=" * 72)
    print(" chat_stream — OFFLINE throttle fallback + cooldown")
    print("=" * 72)
    undo = _install_patches()
    try:
        case_classifier()
        case_cooldown_unit()
        asyncio.run(run_async())
        record("offline: NO real Bedrock model constructed", not _BEDROCK_BUILT,
               f"attempted={_BEDROCK_BUILT!r}")
    finally:
        undo()
        mem.flush(timeout=5.0)
        shutil.rmtree(_TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
