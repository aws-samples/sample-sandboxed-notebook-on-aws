#!/usr/bin/env python3
# test-kind: offline
"""
Transcript-translation unit tests (backend-agnostic, no DB / no AWS).

Guards proxy/storage/_transcript.py — the shared read path that turns each memory
backend's stored form into identical UI chat bubbles:

  - messages_to_bubbles : Strands SessionMessage dicts (SQLite / Postgres store)
  - events_to_bubbles   : AgentCore Memory list_events events

Both must produce the SAME bubble shape and apply the SAME rules: strip the
per-turn injected context preamble off the user message, merge assistant text
across a turn (blank-line separated), and drop tool round-trips.

The AgentCore fixtures mirror the shape verified against a LIVE AgentCore Memory
resource: each event's `payload[].conversational.content.text` is the FULL
serialized Strands SessionMessage JSON (not plain text), and list_events returns
events NEWEST-FIRST.

Run: python3 tests/storage/test_transcript.py   (exit 0 = all passed)
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from proxy.storage._transcript import messages_to_bubbles, events_to_bubbles  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  \u2713 {name}")
    else:
        _failed += 1
        print(f"  \u2717 {name}")


# A realistic single turn in Strands SessionMessage form: an enriched user message,
# an assistant toolUse, a toolResult carrier (user role), then the final answer.
ENRICHED_USER = (
    "[Currently available data \u2014 this inventory is live and authoritative; ...]\n"
    "Local files (in /tmp/ on this VM):\n  - a.csv (1 KB)\n\n"
    "[Existing variables in the notebook ...]\n  df, x\n\n"
    "[Current VM for this turn: vm_id = microvm-abc (reference only ...).]\n\n"
    "What is the total revenue by category?"
)

STRANDS_TURN = [
    {"message": {"role": "user", "content": [{"text": ENRICHED_USER}]}, "message_id": 0},
    {"message": {"role": "assistant", "content": [{"text": "Let me look at the data."}]}, "message_id": 1},
    {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "execute_code", "input": {}}}]}, "message_id": 2},
    {"message": {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "ran"}]}}]}, "message_id": 3},
    {"message": {"role": "assistant", "content": [{"text": "## Revenue by Category\n\n```python\nprint(df)\n```"}]}, "message_id": 4},
]


def test_strands():
    print("Strands (SQLite/Postgres) adapter:")
    b = messages_to_bubbles(STRANDS_TURN)
    check("2 bubbles (user + merged assistant)", len(b) == 2)
    check("user preamble stripped -> just the question",
          b[0] == {"role": "user", "content": "What is the total revenue by category?"})
    check("assistant merged across tool round-trip",
          b[1]["role"] == "assistant"
          and "Let me look at the data." in b[1]["content"]
          and "## Revenue by Category" in b[1]["content"]
          and "```python" in b[1]["content"])
    check("no tool blocks leak into bubbles",
          "toolUse" not in str(b) and "toolResult" not in str(b))
    # assistant segments blank-line separated (don't run together)
    check("assistant segments separated by blank line",
          "data.\n\n## Revenue" in b[1]["content"])


def _event(session_message: dict) -> dict:
    """Wrap a Strands SessionMessage as an AgentCore event (text = serialized JSON),
    mirroring how AgentCoreMemorySessionManager actually stores turns."""
    role = session_message["message"]["role"].upper()
    return {"payload": [{"conversational": {"role": role, "content": {"text": json.dumps(session_message)}}}]}


def test_agentcore():
    print("AgentCore Memory adapter:")
    # Same turn, but each message is an event whose text is the serialized message,
    # and events are NEWEST-FIRST (as the real list_events returns them).
    events = [_event(m) for m in STRANDS_TURN]
    events_newest_first = list(reversed(events))
    b = events_to_bubbles(events_newest_first)
    check("2 bubbles (user + merged assistant)", len(b) == 2)
    check("chronological order restored (user first)",
          b[0]["role"] == "user" and b[1]["role"] == "assistant")
    check("user preamble stripped -> just the question",
          b[0] == {"role": "user", "content": "What is the total revenue by category?"})
    check("assistant final answer intact (code fence preserved)",
          "## Revenue by Category" in b[1]["content"] and "```python" in b[1]["content"])
    check("tool round-trips dropped",
          "toolUse" not in str(b) and "toolResult" not in str(b))
    # Non-conversational payloads (blob/json) and unparseable text are ignored.
    noise = [
        {"payload": [{"blob": b"x"}]},
        {"payload": [{"json": {"content": {"k": 1}}}]},
        {"payload": [{"conversational": {"role": "USER", "content": {"text": "not-json-plain"}}}]},
    ]
    b2 = events_to_bubbles(noise + events_newest_first)
    check("blob/json/unparseable events ignored, real turn still 2 bubbles", len(b2) == 2)


def test_real_agent_preamble():
    """Regression guard: build the preamble with the AGENT's real block builders (not
    a hand-written copy) so a renamed header in notebook_agent.py fails here instead
    of leaking the whole context into restored chat bubbles. (eb0c503 renamed
    '[Existing variables in the notebook' -> '[Variables already defined in the
    notebook' and the stale fixture above kept this test green while the bug shipped.)"""
    print("Real agent preamble (all four blocks) stripped:")
    agent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent"))
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    try:
        from ai_assistant.notebook_agent import _prepend_available_data
    except Exception as e:  # noqa: BLE001 — a missing agent dep must FAIL, not skip
        check(f"import ai_assistant.notebook_agent ({type(e).__name__}: {e})", False)
        return
    question = ("Context (from Workbook Intel): KB coverage is 25 products. "
                "Task: check the 7 restock products not in kb_pids.")
    context = {
        "uploaded_files": [{"name": "a.csv", "size": "1 KB"}],
        "data_sources": {"sources": [{"source_type": "athena", "source_id": "db.orders",
                                      "display_name": "db.orders", "detail": "8 cols"}]},
        "variables": ["restock", "kb_pids", "df"],
        "session_attachments": [{"name": "x.png", "mime": "image/png",
                                 "s3_uri": "s3://bucket/x.png"}],
        "vm_id": "microvm-abc",
    }
    enriched = _prepend_available_data(question, context)
    check("all four blocks were actually prepended (fixture is meaningful)",
          enriched.count("\n\n") >= 4 and enriched.endswith(question))
    msgs = [{"message": {"role": "user", "content": [{"text": enriched}]}, "message_id": 0},
            {"message": {"role": "assistant", "content": [{"text": "On it."}]}, "message_id": 1}]
    b = messages_to_bubbles(msgs)
    check("user bubble is ONLY the Context/Task text (no injected blocks)",
          b and b[0] == {"role": "user", "content": question})
    ac = events_to_bubbles(list(reversed([_event(m) for m in msgs])))
    check("AgentCore path strips the real preamble identically", ac == b)

    # The LIVE-schema form of the [Variables…] block (multi-line, one line per
    # frame) must strip too — it must never contain a blank line.
    def _df(name, cols):
        return {"name": name, "type": "DataFrame", "kind": "dataframe", "shape": [80, len(cols)],
                "columns": [[c, "object"] for c in cols], "more": 0}
    schema_ctx = dict(context, _variable_schema={"variables": [
        _df("restock", ["product_id", "qty"]), _df("kb_pids", ["product_id"])], "count": 2})
    enriched = _prepend_available_data(question, schema_ctx)
    check("schema preamble renders frame lines (fixture is meaningful)",
          "  restock: DataFrame 80×2 — product_id:object" in enriched)
    msgs = [{"message": {"role": "user", "content": [{"text": enriched}]}, "message_id": 0},
            {"message": {"role": "assistant", "content": [{"text": "On it."}]}, "message_id": 1}]
    b = messages_to_bubbles(msgs)
    check("schema preamble: user bubble is ONLY the question",
          b and b[0] == {"role": "user", "content": question})


def test_parity():
    print("SQLite vs AgentCore parity:")
    strands_bubbles = messages_to_bubbles(STRANDS_TURN)
    ac_events = list(reversed([_event(m) for m in STRANDS_TURN]))
    ac_bubbles = events_to_bubbles(ac_events)
    check("both backends produce IDENTICAL bubbles", strands_bubbles == ac_bubbles)


if __name__ == "__main__":
    test_strands()
    test_agentcore()
    test_real_agent_preamble()
    test_parity()
    print(f"\nRESULT: {_passed}/{_passed + _failed} checks passed")
    if _failed:
        print("SOME CHECKS FAILED \u2717")
        sys.exit(1)
    print("ALL TRANSCRIPT CHECKS PASSED \u2713")
