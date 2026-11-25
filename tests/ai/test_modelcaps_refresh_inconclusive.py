#!/usr/bin/env python3
# test-kind: offline
"""Unit tests: thinking-shape / reasoning probes in modelcaps/refresh.py must not
turn unclear results (throttles, transient Bedrock errors) into definite answers.

Covers the _INCONCLUSIVE sentinel, retry-with-backoff, prior-catalog fallback,
the reasoning True->False flip re-check, the end-of-run summary, sentinel-free
emission, and the --prior / --no-prior flags.

Uses a FAKE Bedrock client and zero retry delays — no AWS calls are made.

Run:  .venv/bin/python tests/ai/test_modelcaps_refresh_inconclusive.py   (exit 0 = pass)
Also collected by pytest (plain test_* functions).
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from botocore.exceptions import (  # noqa: E402
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from modelcaps import refresh  # noqa: E402

INC = refresh._INCONCLUSIVE
NO_DELAY = (0.0, 0.0)
REASONING_PROMPT = "17 + 26"


# ── Fakes ────────────────────────────────────────────────────────────

def client_error(code, msg="boom"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "Converse")


def throttle():
    return client_error("ThrottlingException", "Too many requests, please wait")


def validation(msg="The model does not support this field"):
    return client_error("ValidationException", msg)


def text_response(reasoning=False):
    content = [{"text": "43"}]
    if reasoning:
        content.insert(0, {"reasoningContent": {"reasoningText": {"text": "17+26=43"}}})
    return {"output": {"message": {"content": content}}, "stopReason": "end_turn",
            "usage": {}}


class FakeClient:
    """converse(**kw) delegates to handler(kw, call_index); exceptions are raised."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.handler(kwargs, len(self.calls) - 1)
        if isinstance(result, BaseException):
            raise result
        return result

    def converse_stream(self, **kwargs):
        return {"stream": []}


def scripted(*outcomes):
    """Handler returning outcomes[i] for the i-th call (last one repeats)."""
    def handler(kwargs, i):
        return outcomes[min(i, len(outcomes) - 1)]
    return handler


# ── Probe-level tests ────────────────────────────────────────────────

def test_shape_throttled_on_all_attempts_is_inconclusive():
    c = FakeClient(scripted(throttle()))
    assert refresh._probe_thinking_disable_shape(c, "m", retry_delays=NO_DELAY) is INC
    # 1 attempt + 2 retries on the first candidate, then stop (order-dependent answer).
    assert len(c.calls) == 3


def test_shape_throttled_on_both_shapes_is_not_accepted():
    # Throttle on every call, whichever shape: never reported as accepted.
    c = FakeClient(lambda kw, i: throttle())
    result = refresh._probe_thinking_disable_shape(c, "m", retry_delays=NO_DELAY)
    assert result is INC and not isinstance(result, dict)


def test_shape_throttle_then_success_on_retry_is_accepted():
    c = FakeClient(scripted(throttle(), text_response()))
    assert refresh._probe_thinking_disable_shape(c, "m", retry_delays=NO_DELAY) == \
        {"thinking": {"type": "disabled"}}
    assert len(c.calls) == 2


def test_shape_validation_on_both_is_rejected_none():
    c = FakeClient(scripted(validation()))
    assert refresh._probe_thinking_disable_shape(c, "m", retry_delays=NO_DELAY) is None
    assert len(c.calls) == 2  # ValidationException is never retried


def test_shape_second_candidate_accepted():
    c = FakeClient(scripted(validation(), text_response()))
    assert refresh._probe_thinking_disable_shape(c, "m", retry_delays=NO_DELAY) == \
        {"thinking": {"type": "between_tools"}}


def test_reasoning_transient_errors_are_inconclusive():
    for err in (
        client_error("ServiceUnavailableException"),
        client_error("ModelNotReadyException"),
        client_error("InternalServerException"),
        client_error("ModelTimeoutException"),
        client_error("TooManyRequestsException"),
        throttle(),
        ReadTimeoutError(endpoint_url="https://bedrock"),
        ConnectTimeoutError(endpoint_url="https://bedrock"),
        EndpointConnectionError(endpoint_url="https://bedrock"),
    ):
        c = FakeClient(scripted(err))
        assert refresh._probe_reasoning(c, "m", retry_delays=NO_DELAY) is INC, err
        assert len(c.calls) == 3, err


def test_reasoning_transient_then_success_uses_response():
    c = FakeClient(scripted(client_error("ServiceUnavailableException"), text_response(True)))
    assert refresh._probe_reasoning(c, "m", retry_delays=NO_DELAY) is True


def test_reasoning_clean_response_decides():
    assert refresh._probe_reasoning(FakeClient(scripted(text_response(False))), "m",
                                    retry_delays=NO_DELAY) is False
    assert refresh._probe_reasoning(FakeClient(scripted(text_response(True))), "m",
                                    retry_delays=NO_DELAY) is True


def test_reasoning_validation_is_false():
    assert refresh._probe_reasoning(FakeClient(scripted(validation())), "m",
                                    retry_delays=NO_DELAY) is False


def test_sentinel_is_not_usable_as_bool():
    try:
        bool(INC)
    except TypeError:
        return
    raise AssertionError("sentinel must not be truthy/falsy")


# ── Resolution tests ─────────────────────────────────────────────────

PRIOR_SHAPE = {"thinking": {"type": "disabled"}}


def prior_entry(shape=PRIOR_SHAPE, reasoning=True):
    return {"capabilities": {"thinking_disable_shape": shape, "reasoning": reasoning}}


def test_prior_fallback_for_both_fields():
    r = refresh.resolve_thinking_and_reasoning(INC, INC, prior_entry(), base_id="b", invocable="us.b")
    assert r["thinking_disable_shape"] == PRIOR_SHAPE
    assert r["thinking_disable_shape_provenance"] == "prior"
    assert r["reasoning"] is True and r["reasoning_provenance"] == "prior"
    assert {(i["field"], i["kind"]) for i in r["issues"]} == {
        ("thinking_disable_shape", "inconclusive->prior"),
        ("reasoning", "inconclusive->prior"),
    }


def test_prior_null_shape_is_kept_as_null():
    r = refresh.resolve_thinking_and_reasoning(INC, False, prior_entry(shape=None, reasoning=False))
    assert r["thinking_disable_shape"] is None
    assert r["thinking_disable_shape_provenance"] == "prior"
    assert r["reasoning"] is False and r["reasoning_provenance"] == "probed"


def test_brand_new_model_inconclusive_is_null():
    r = refresh.resolve_thinking_and_reasoning(INC, INC, None, base_id="new", invocable="us.new")
    assert r["thinking_disable_shape"] is None
    assert r["thinking_disable_shape_provenance"] == "inconclusive"
    assert r["reasoning"] is None and r["reasoning_provenance"] == "inconclusive"
    assert {(i["field"], i["kind"]) for i in r["issues"]} == {
        ("thinking_disable_shape", "inconclusive->null"),
        ("reasoning", "inconclusive->null"),
    }


def test_inconclusive_reasoning_moot_when_shape_probed():
    r = refresh.resolve_thinking_and_reasoning(PRIOR_SHAPE, INC, None)
    assert r["reasoning"] is True and r["reasoning_provenance"] == "probed"
    assert r["issues"] == []


def test_clean_probes_unchanged_behaviour():
    # Same as the old `reasoning_probe or (shape is not None)`, all "probed", no issues.
    for shape, rp, expected in ((None, False, False), (None, True, True),
                                (PRIOR_SHAPE, False, True), (PRIOR_SHAPE, True, True)):
        r = refresh.resolve_thinking_and_reasoning(shape, rp, None)
        assert r["thinking_disable_shape"] == shape and r["reasoning"] is expected
        assert r["thinking_disable_shape_provenance"] == "probed"
        assert r["reasoning_provenance"] == "probed" and r["issues"] == []


def test_reasoning_flip_recheck_agrees_records_false():
    calls = []
    r = refresh.resolve_thinking_and_reasoning(
        None, False, prior_entry(shape=None, reasoning=True),
        recheck_reasoning=lambda: calls.append(1) or False, base_id="opus", invocable="us.opus")
    assert calls == [1]
    assert r["reasoning"] is False and r["reasoning_provenance"] == "probed"
    assert [i["kind"] for i in r["issues"]] == [
        "changed-vs-prior (True->False, confirmed by re-check)"]


def test_reasoning_flip_recheck_disagrees_keeps_prior_true():
    for second in (True, INC):
        r = refresh.resolve_thinking_and_reasoning(
            None, False, prior_entry(shape=None, reasoning=True),
            recheck_reasoning=lambda s=second: s, base_id="opus", invocable="us.opus")
        assert r["reasoning"] is True and r["reasoning_provenance"] == "prior", second
        assert len(r["issues"]) == 1 and r["issues"][0]["kind"].startswith("disagreement->prior")


def test_no_recheck_when_prior_agrees():
    calls = []
    r = refresh.resolve_thinking_and_reasoning(
        None, False, prior_entry(shape=None, reasoning=False),
        recheck_reasoning=lambda: calls.append(1) or False)
    assert calls == [] and r["reasoning"] is False and r["issues"] == []


def test_summary_lists_pairs_and_only_command():
    issues = []
    issues += refresh.resolve_thinking_and_reasoning(
        INC, INC, prior_entry(), base_id="zai.glm-5.3", invocable="us.zai.glm-5.3")["issues"]
    issues += refresh.resolve_thinking_and_reasoning(
        None, False, prior_entry(shape=None, reasoning=True), recheck_reasoning=lambda: True,
        base_id="anthropic.claude-opus-5-5", invocable="global.anthropic.claude-opus-5-5")["issues"]
    issues += refresh.resolve_thinking_and_reasoning(
        None, False, prior_entry(shape=None, reasoning=True), recheck_reasoning=lambda: False,
        base_id="x.confirmed", invocable="us.x.confirmed")["issues"]
    text = refresh.summarize_issues(issues)
    assert "zai.glm-5.3 [thinking_disable_shape]: inconclusive->prior" in text
    assert "zai.glm-5.3 [reasoning]: inconclusive->prior" in text
    assert "anthropic.claude-opus-5-5 [reasoning]: disagreement->prior" in text
    assert "x.confirmed [reasoning]: changed-vs-prior" in text
    cmd = [ln.strip() for ln in text.splitlines() if "--only" in ln]
    assert cmd == [".venv/bin/python -m modelcaps.refresh --only "
                   "global.anthropic.claude-opus-5-5,us.zai.glm-5.3 "
                   "--output /tmp/modelcaps_reprobe.json"], cmd
    assert "none" in refresh.summarize_issues([])


# ── End-to-end build_catalog + write_catalog (all I/O faked) ─────────

GLM = "zai.glm-x"        # throttled on shape + reasoning; prior exists
NEW = "newco.model-1"    # throttled on shape + reasoning; no prior
OPUS = "anthropic.opus-x"  # reasoning flip: False then True on re-check; prior True


def _make_e2e_handler():
    state = {}

    def handler(kwargs, _i):
        return _e2e_route(kwargs, state)
    return handler


def _e2e_route(kwargs, _state):
    mid = kwargs["modelId"]
    text = kwargs["messages"][0]["content"][0].get("text", "")
    is_shape = "additionalModelRequestFields" in kwargs
    is_reasoning = REASONING_PROMPT in text
    if mid.endswith(GLM) or mid.endswith(NEW):
        if is_shape or is_reasoning:
            return throttle()
        return text_response()
    if mid.endswith(OPUS):
        if is_shape:
            return validation()
        if is_reasoning:
            n = _state.get(mid, 0)
            _state[mid] = n + 1
            return text_response(reasoning=(n >= 1))  # 1st: no block, re-check: block
        return text_response()
    return text_response()


def _run_build(prior_models):
    handler = _make_e2e_handler()  # one shared state per run (clients are per-model)
    regions = [{"name": "us-east-1", "cris_profiles": ["us"]}]
    saved = (refresh.discover_profiles, refresh.fetch_litellm_data, refresh.boto3,
             refresh.time, refresh._RETRY_DELAYS)
    clients = []

    def make_client(*_a, **_k):
        c = FakeClient(handler)
        clients.append(c)
        return c

    try:
        refresh.discover_profiles = lambda: ({b: regions for b in (GLM, NEW, OPUS)}, {}, {})
        refresh.fetch_litellm_data = lambda: {}
        refresh.boto3 = types.SimpleNamespace(client=make_client)
        refresh.time = types.SimpleNamespace(sleep=lambda s: None, time=__import__("time").time)
        refresh._RETRY_DELAYS = NO_DELAY
        return refresh.build_catalog(region="us-west-2", skip_probes=False, only=None,
                                     prior_models=prior_models)
    finally:
        (refresh.discover_profiles, refresh.fetch_litellm_data, refresh.boto3,
         refresh.time, refresh._RETRY_DELAYS) = saved


PRIOR_MODELS = {
    GLM: prior_entry(shape=PRIOR_SHAPE, reasoning=True),
    OPUS: prior_entry(shape=None, reasoning=True),
}


def _write_and_read(catalog):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "caps.json"
        refresh.write_catalog(catalog, out)
        text = out.read_text()
    return text, json.loads(text)


def _assert_emitted_types(data):
    for base_id, entry in data["models"].items():
        caps = entry["capabilities"]
        assert caps["thinking_disable_shape"] is None or isinstance(caps["thinking_disable_shape"], dict), base_id
        assert caps["reasoning"] is None or isinstance(caps["reasoning"], bool), base_id


def test_e2e_with_prior():
    text, data = _write_and_read(_run_build(PRIOR_MODELS))
    assert "INCONCLUSIVE" not in text and "object at 0x" not in text
    _assert_emitted_types(data)
    m = data["models"]
    assert m[GLM]["capabilities"]["thinking_disable_shape"] == PRIOR_SHAPE
    assert m[GLM]["capabilities"]["reasoning"] is True
    assert m[GLM]["provenance"]["thinking_disable_shape"] == "prior"
    assert m[GLM]["provenance"]["reasoning"] == "prior"
    assert m[NEW]["capabilities"]["thinking_disable_shape"] is None
    assert m[NEW]["capabilities"]["reasoning"] is None
    assert m[NEW]["provenance"]["thinking_disable_shape"] == "inconclusive"
    assert m[NEW]["provenance"]["reasoning"] == "inconclusive"
    # Opus: probe said False, re-check said True -> keep prior True.
    assert m[OPUS]["capabilities"]["reasoning"] is True
    assert m[OPUS]["provenance"]["reasoning"] == "prior"
    assert m[OPUS]["capabilities"]["thinking_disable_shape"] is None
    assert m[OPUS]["provenance"]["thinking_disable_shape"] == "probed"


def test_e2e_no_prior_disables_fallback():
    text, data = _write_and_read(_run_build({}))
    assert "INCONCLUSIVE" not in text
    _assert_emitted_types(data)
    m = data["models"]
    assert m[GLM]["capabilities"]["thinking_disable_shape"] is None
    assert m[GLM]["provenance"]["thinking_disable_shape"] == "inconclusive"
    assert m[GLM]["capabilities"]["reasoning"] is None
    # No prior True -> no re-check; the single clean False is recorded.
    assert m[OPUS]["capabilities"]["reasoning"] is False
    assert m[OPUS]["provenance"]["reasoning"] == "probed"


def test_write_catalog_never_emits_sentinel():
    catalog = {"models": {"a": {"capabilities": {"thinking_disable_shape": INC,
                                                 "reasoning": INC},
                                "extra": [INC]}}}
    text, data = _write_and_read(catalog)
    assert "INCONCLUSIVE" not in text
    assert data["models"]["a"]["capabilities"] == {"thinking_disable_shape": None,
                                                   "reasoning": None}
    assert data["models"]["a"]["extra"] == [None]


# ── CLI flag wiring ──────────────────────────────────────────────────

def _run_main(argv):
    captured = {}
    saved = (refresh.build_catalog, refresh.write_catalog)

    def fake_build(**kw):
        captured.update(kw)
        return {"models": {}}

    try:
        refresh.build_catalog = fake_build
        refresh.write_catalog = lambda catalog, output: captured.setdefault("output", output)
        assert refresh.main(argv) == 0
    finally:
        refresh.build_catalog, refresh.write_catalog = saved
    return captured


def test_cli_prior_flags():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.json"
        out.write_text(json.dumps({"models": {"from-output": {}}}))
        other = Path(d) / "other.json"
        other.write_text(json.dumps({"models": {"from-prior-flag": {}}}))

        assert _run_main(["--skip-probes", "--output", str(out)])["prior_models"] == {"from-output": {}}
        assert _run_main(["--skip-probes", "--output", str(out), "--prior", str(other)])[
            "prior_models"] == {"from-prior-flag": {}}
        assert _run_main(["--skip-probes", "--output", str(out), "--no-prior"])["prior_models"] == {}

    missing = Path("/nonexistent/caps.json")
    assert refresh.resolve_prior_path(missing, None, False) == (
        refresh.DEFAULT_OUTPUT_PATH if refresh.DEFAULT_OUTPUT_PATH.exists() else None)
    assert refresh.resolve_prior_path(missing, None, True) is None


# ── Standalone runner ────────────────────────────────────────────────

def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as e:  # noqa: BLE001
            failures.append(name)
            print(f"  [FAIL] {name} — {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        return 1
    print(f"RESULT: all {len(tests)} checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
