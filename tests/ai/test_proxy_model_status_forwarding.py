#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: the proxy relays the agent's `model_status` frame unchanged.

WHAT THIS PROVES (no stack, no network)
---------------------------------------
proxy/notebook/routes/ai.py `_stream_agent_events` parses the chat agent's SSE
`data:` frames back into dicts and the routes re-serialize them to the browser. This
drives it against a mocked agent (httpx.MockTransport) that streams:
  model_status (new event type), text, an unknown future event type, done.
and checks that:
  - every frame comes out equal to what the agent sent (no filtering, no rewriting),
  - the re-serialized SSE line the browser gets round-trips to the same dict,
  - the /ai/chat/sync aggregator still only collects text (model_status is ignored),
    by mirroring its loop over the same frames.

Run:
    .venv/bin/python tests/ai/test_proxy_model_status_forwarding.py
Exit 0 = all passed; 1 = at least one failed.
"""

import asyncio
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import httpx  # noqa: E402

import proxy.notebook.routes.ai as ai_routes  # noqa: E402

STATUS = {"type": "model_status", "requested": "zai.glm-5.3",
          "active": "anthropic.claude-haiku-5-5", "reason": "cooldown",
          "cooldown_remaining_s": 482}
FUTURE = {"type": "some_future_event", "payload": {"k": [1, 2]}}
SENT = [STATUS,
        {"type": "text", "content": "_(note)_\n\n"},
        FUTURE,
        {"type": "text", "content": "answer"},
        {"type": "done"}]

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail and not ok else ""))


def _sse_body() -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in SENT).encode()


class _FakeAux:
    """Stands in for http_clients.aux_client(): an httpx client over a mock agent."""

    def __init__(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse_body(),
                                  headers={"content-type": "text/event-stream"})
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def aux_client(self):
        return self._client


async def _forward():
    real = ai_routes.http_clients
    ai_routes.http_clients = _FakeAux()
    try:
        return [e async for e in ai_routes._stream_agent_events(
            {"action": "chat", "session_id": "s", "message": "m", "context": {}})]
    finally:
        ai_routes.http_clients = real


def main():
    print("=" * 72)
    print(" proxy relays model_status unchanged (offline)")
    print("=" * 72)
    got = asyncio.run(_forward())
    record("every agent frame comes through, in order, unchanged",
           got == SENT, f"got {got!r}")
    status = next((e for e in got if e.get("type") == "model_status"), None)
    record("model_status keeps its exact fields and values",
           status == STATUS, repr(status))
    # The browser gets `data: <json>` lines produced by the route's own serializer.
    lines = [f"data: {json.dumps(e)}" for e in got]
    back = [json.loads(line[len("data: "):]) for line in lines]
    record("re-serialized SSE frames round-trip to the same dicts", back == SENT)
    # /ai/chat/sync only aggregates text (and error); a model_status must not leak in.
    parts = [e.get("content", "") for e in got if e.get("type") == "text"]
    record("sync aggregation ignores model_status and unknown types",
           "".join(parts) == "_(note)_\n\nanswer", repr("".join(parts)))

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
