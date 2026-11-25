#!/usr/bin/env python3
# test-kind: manual
"""
LIVE integration test — AI-assistant model dropdown: every model works, switching
preserves context, and attachment gating matches each model's capabilities.

What it proves end-to-end (UI → proxy → agent → Bedrock → durable memory):
  1. The proxy (:8081) and the AI-assistant agent (:8082) are up; GET /ai/models
     returns the curated, region-scoped dropdown list (default flagged, per-model
     capabilities).
  2. EVERY model in the dropdown actually serves a chat turn. Each turn exercises the
     agent's per-model config in get_model — temperature dropped for models that
     reject it, thinking shaped per-model, maxTokens clamped to the model's cap, and
     prompt caching attached only when the model supports it. A model whose config
     were wrong would ValidationException here; a clean reply = that model's whole
     inference-config path is correct.
  3. MODEL SWITCHING preserves conversation context: facts stated to one model are
     recalled by the NEXT model on the SAME session (agent discards+rebuilds on
     switch, reusing the durable SessionManager so history rehydrates).
  4. ATTACHMENT GATING (proxy /ai/attachments backstop) matches each model's
     capabilities:
       • image  → accepted only by VISION-capable models, 400 otherwise.
       • binary document (pdf) → accepted only by DOCUMENT-capable models, 400 else.
       • text/csv/json → text-inline, require NO capability, ALWAYS accepted — even
         on document-incapable models.

LIVE test: talks to the running stack, makes real (small, cheap) Bedrock calls.
NOT part of the offline unit suite. Run only when the stack is up:

    .venv/bin/python tests/integration/test_model_switch_live.py
    # options:
    QUICK=1 .venv/bin/python tests/integration/test_model_switch_live.py   # skip the all-12 sweep
    PROXY_URL=http://localhost:8081 .venv/bin/python tests/integration/test_model_switch_live.py

    MODEL_SWITCH_EXCLUDE="" …   # also test the models in EXCLUDE (default: GLM 5.3)

Models in EXCLUDE (below) are left out of steps 2-4. A throttled turn is a [SKIP];
a hung/erroring turn is one [FAIL] and the run continues.

Exit 0 = all checks passed; 1 = a failure or the stack wasn't reachable.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8081").rstrip("/")
AGENT_PING = os.environ.get("AGENT_URL", "http://localhost:8082").rstrip("/") + "/ping"
HEALTH_URL = PROXY_URL + "/health"
MODELS_URL = PROXY_URL + "/ai/models"
CHAT_URL = PROXY_URL + "/ai/chat"
ATTACH_URL = PROXY_URL + "/ai/attachments"
AUTH_HEADER = "X-Proxy-Auth"
TURN_TIMEOUT = 120  # seconds per chat turn
QUICK = os.environ.get("QUICK") == "1"  # skip the full all-12 sweep when set

# Models this test never uses (sweep, context switch, attachment gating), even though
# they stay in the product's /ai/models dropdown.
#   zai.glm-5.3 (invoked as global.zai.glm-5.3) — GLM 5.3: not to be used (user
#                request); this account's Bedrock token quota for it is exhausted, so
#                every turn ThrottlingException-retries past TURN_TIMEOUT.
# Override with MODEL_SWITCH_EXCLUDE="id1,id2" (empty string = exclude nothing). Ids
# match with or without a cross-region prefix (global./us./eu./apac.).
DEFAULT_EXCLUDE = ("zai.glm-5.3",)
_GEO_PREFIXES = ("global.", "us.", "eu.", "apac.")


def _base_id(model_id: str) -> str:
    for p in _GEO_PREFIXES:
        if model_id.startswith(p):
            return model_id[len(p):]
    return model_id


EXCLUDE = {_base_id(s.strip()) for s in
           os.environ.get("MODEL_SWITCH_EXCLUDE", ",".join(DEFAULT_EXCLUDE)).split(",") if s.strip()}


def is_excluded(model: dict) -> bool:
    return _base_id(model.get("id", "")) in EXCLUDE

# Facts planted on the default model; later models must recall them after a switch.
FACTS = {
    "name": ("My name is Zephyrina. Remember it.", "zephyrina"),
    "number": ("My lucky number is 8472. Remember it.", "8472"),
    "city": ("I live in Reykjavik. Remember it.", "reykjavik"),
}

# Tiny attachment payloads (all well under the size cap). A real 1x1 PNG, and
# trivial text/csv/json/pdf bytes — just enough that the proxy's classify + the
# capability backstop run; we assert the HTTP status, not model comprehension.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
# Minimal valid-enough PDF (header + trivial body); the proxy only checks mime/ext +
# capability, not PDF validity, so this is sufficient to exercise the gate.
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
_CSV = b"a,b,c\n1,2,3\n4,5,6\n"
_TXT = b"hello from a plain text attachment\n"
_JSON = b'{"hello": "world", "n": 42}'


# ── tiny output helpers ───────────────────────────────────────────────────────
_failures: list[str] = []


_skips: list[str] = []


def ok(msg):    print(f"  [PASS] {msg}")
def bad(msg):   print(f"  [FAIL] {msg}"); _failures.append(msg)
def skip(msg):  print(f"  [SKIP] {msg}"); _skips.append(msg)
def info(msg):  print(f"  ... {msg}")
def head(msg):  print(f"\n== {msg} ==")


def _short(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


# ── proxy auth token: read from the running proxy process env ─────────────────
def _proxy_token() -> str | None:
    env_tok = os.environ.get("PROXY_AUTH_TOKEN")
    if env_tok:
        return env_tok
    try:
        pid = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN", "-t"], text=True
        ).split()[0]
        env = subprocess.check_output(["ps", "eww", "-p", pid, "-o", "command="], text=True)
        m = re.search(r"PROXY_AUTH_TOKEN=(\S+)", env)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _req(url, *, token, method="GET", body=None, timeout=30):
    headers = {AUTH_HEADER: token, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _is_throttle(text: str) -> bool:
    t = text.lower()
    return "throttl" in t or "too many tokens" in t or "too many requests" in t or "rate exceeded" in t


def report_turn(reply: str, fail_msg: str) -> bool:
    """True when the turn produced a reply. A throttled model is a [SKIP] (account
    quota, not a product bug); any other error is ONE [FAIL]."""
    if reply.startswith("__THROTTLED__"):
        skip(f"{fail_msg}: {_short(reply, 160)}")
        return False
    if reply.startswith("__"):
        bad(f"{fail_msg}: {_short(reply, 200)}")
        return False
    return True


# ── one chat turn: POST /ai/chat, read the SSE stream, return the full text ────
def chat_turn(token, session_id, message, model_id=None, timeout=TURN_TIMEOUT) -> str:
    body = {"session_id": session_id, "message": message, "notebook_id": "", "cells": []}
    if model_id:
        body["model_id"] = model_id
    text_parts, saw_done = [], False
    try:
        resp = _req(CHAT_URL, token=token, method="POST", body=body, timeout=timeout)
    except urllib.error.HTTPError as e:
        return f"__HTTP_{e.code}__: {e.read().decode(errors='replace')[:200]}"
    except Exception as e:
        return f"__ERR__: {e}"
    # A hung / throttled / erroring model must end THIS turn only (one [FAIL] or
    # [SKIP] at the call site), never the whole run: the read loop is guarded too.
    try:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "text":
                text_parts.append(evt.get("content", ""))
            elif evt.get("type") == "done":
                saw_done = True
                break
            elif evt.get("type") == "error":
                err = str(evt.get("content") or evt.get("message"))
                if _is_throttle(err):
                    return f"__THROTTLED__: {err}"
                return f"__STREAM_ERR__: {err}"
    except Exception as e:  # socket timeout, reset, truncated chunked body, ...
        return f"__ERR__: stream read failed after {len(text_parts)} text frames: {type(e).__name__}: {e}"
    finally:
        resp.close()
    out = "".join(text_parts).strip()
    if not out and not saw_done:
        return "__EMPTY__"
    return out


# ── upload an attachment; return (http_status, detail) ────────────────────────
def upload_attachment(token, session_id, name, mime, raw_bytes, model_id):
    """POST /ai/attachments. Returns (status, info-dict-or-error-text). 200 = accepted
    (the proxy classified + passed the capability backstop and PutObject'd to S3);
    400 = rejected by the capability backstop (or a bad type)."""
    body = {
        "session_id": session_id,
        "name": name,
        "mime": mime,
        "data": base64.b64encode(raw_bytes).decode(),
        "model_id": model_id,
    }
    try:
        resp = _req(ATTACH_URL, token=token, method="POST", body=body, timeout=60)
        return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]
    except Exception as e:
        return -1, str(e)


def main() -> int:
    head("0. preflight: stack reachable + auth")
    token = _proxy_token()
    if not token:
        bad("could not obtain PROXY_AUTH_TOKEN (is the proxy running on :8081?)")
        return 1
    ok("resolved proxy auth token")
    for url, label in ((HEALTH_URL, "proxy /health"), (AGENT_PING, "agent /ping")):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                ok(f"{label} reachable (HTTP {r.status})")
        except Exception as e:
            bad(f"{label} unreachable: {e}")
            return 1

    head("1. GET /ai/models — the dropdown")
    try:
        with _req(MODELS_URL, token=token, timeout=30) as r:
            catalog = json.loads(r.read())
    except Exception as e:
        bad(f"/ai/models failed: {e}")
        return 1
    if not catalog.get("available") or not catalog.get("models"):
        bad(f"/ai/models returned no models: {catalog}")
        return 1
    default_id = catalog.get("default_id")
    models = catalog["models"]  # already score-ordered, default first
    ok(f"/ai/models: {catalog['count']} models, region={catalog.get('region')}, default={default_id}")
    for m in models:
        caps = ",".join(m.get("capabilities") or [])
        info(f"    [{m.get('provider'):<9}] {m.get('display_name'):<20} {m.get('cost_label'):>6}  caps=[{caps}]")
    excluded = [m for m in models if is_excluded(m)]
    models = [m for m in models if not is_excluded(m)]
    if excluded:
        info("excluded from this run (MODEL_SWITCH_EXCLUDE): "
             + ", ".join(f"{m['display_name']} ({m['id']})" for m in excluded))
    if not models:
        bad("every dropdown model is excluded — nothing to test")
        return 1
    if models[0]["id"] != default_id:
        info(f"default {default_id} is excluded — seeding the switch on {models[0]['display_name']}")

    def guarded(step, fn):
        """One step's unexpected exception = ONE [FAIL]; the remaining steps still run."""
        try:
            fn()
        except Exception as e:
            bad(f"{step}: harness error: {type(e).__name__}: {e}")

    # ── 2. EVERY model serves a turn (exercises per-model inference config) ──
    head("2. each of the %d models serves a chat turn (per-model config)" % len(models))

    def sweep():
        if QUICK:
            info("QUICK=1 — skipping the all-model sweep")
            return
        for m in models:
            sid = f"perturn-{uuid.uuid4()}"
            # A trivial prompt; a wrong temperature/thinking/maxTokens/cache config for
            # THIS model would raise a ValidationException surfaced as __STREAM_ERR__.
            reply = chat_turn(token, sid, "Reply with exactly the word: ready", model_id=m["id"])
            if report_turn(reply, f"[{m['provider']}] {m['display_name']} turn FAILED"):
                ok(f"[{m['provider']}] {m['display_name']} served a turn -> \"{_short(reply, 40)}\"")

    guarded("2. sweep", sweep)

    # ── 3. model switching preserves context (one shared session) ──
    # This measures a TRUE cross-model handoff, not window depth: for each step we
    # (1) state a fact to the CURRENT model, (2) switch to the NEXT model, (3) ask the
    # next model to recall it. Re-planting the fact immediately before each switch keeps
    # BOTH the fact-bearing message and the recall question inside the sliding window
    # regardless of how far down the 12-model sequence we are, so the check is
    # deterministic and independent of AGENT_CONVERSATION_WINDOW_SIZE. Each recall still
    # crosses a model switch (model B recalls what model A was just told), and a
    # reasoningContent block left in the rehydrated history would still surface as a
    # ValidationException on the Nova models.
    head("3. model switch preserves conversation context (all models, one session)")

    def switch():
        session_id = f"switch-{uuid.uuid4()}"
        questions = {"name": "What is my name? Answer with ONLY the name.",
                     "number": "What is my lucky number? Answer with ONLY the number.",
                     "city": "Which city do I live in? Answer with ONLY the city."}
        fact_keys = list(FACTS.keys())

        # Switch through ALL non-default models (or just a few families when QUICK). The
        # "current" model (the one that hears each fact) starts as the default and advances
        # to each model after it's been asked.
        switch_set = models[1:] if not QUICK else [
            m for m in models[1:] if m["provider"] in ("amazon", "openai", "mistral")][:3]
        info(f"session={session_id} | handoff seeded on [{models[0]['provider']}] {models[0]['display_name']}")
        current = models[0]  # the default — the first "previous" model that states a fact
        for i, m in enumerate(switch_set):
            key = fact_keys[i % len(fact_keys)]
            statement, expect = FACTS[key]
            # (1) plant the fact on the CURRENT (previous) model...
            s = chat_turn(token, session_id, statement, model_id=current["id"])
            if not report_turn(s, f"plant {key} on [{current['provider']}] {current['display_name']} failed"):
                current = m
                continue
            # (2+3) switch to the NEXT model and ask it to recall the just-planted fact.
            reply = chat_turn(token, session_id, questions[key], model_id=m["id"])
            if not report_turn(reply, f"switch -> [{m['provider']}] {m['display_name']}: turn failed"):
                pass
            elif expect.lower() in reply.lower():
                ok(f"[{m['provider']}] {m['display_name']} RECALLED {key} ('{expect}') "
                   f"across switch from [{current['display_name']}]")
            else:
                bad(f"[{m['provider']}] {m['display_name']} did NOT recall {key} "
                    f"(want '{expect}') -> \"{_short(reply)}\"")
            current = m  # this model becomes the "previous" model for the next step

    guarded("3. switch", switch)

    # ── 4. attachment capability gating (proxy backstop) ──
    head("4. attachment gating matches each model's capabilities")

    def gating():
        att_sid = f"attach-{uuid.uuid4()}"
        vision_model = next((m for m in models if "vision" in (m.get("capabilities") or [])), None)
        novision_model = next((m for m in models if "vision" not in (m.get("capabilities") or [])), None)
        doc_model = next((m for m in models if "documents" in (m.get("capabilities") or [])), None)
        nodoc_model = next((m for m in models if "documents" not in (m.get("capabilities") or [])), None)

        # 4a. IMAGE on a vision-capable model -> accepted (200).
        if vision_model:
            st, _d = upload_attachment(token, att_sid, "pic.png", "image/png", _PNG_1x1, vision_model["id"])
            (ok if st == 200 else bad)(
                f"image on vision model [{vision_model['display_name']}] -> {st} (want 200)")
        else:
            info("no vision-capable model in the list (unexpected) — skipping image-accept")

        # 4b. IMAGE on a NON-vision model -> rejected (400). GLM 5.3 is the dropdown's
        #     only non-vision model; with it excluded there is no substitute.
        if novision_model:
            st, d = upload_attachment(token, att_sid, "pic.png", "image/png", _PNG_1x1, novision_model["id"])
            (ok if st == 400 else bad)(
                f"image on non-vision model [{novision_model['display_name']}] -> {st} (want 400)")
        elif any("vision" not in (m.get("capabilities") or []) for m in excluded):
            skip("image-reject: the only non-vision dropdown model(s) "
                 f"{[m['display_name'] for m in excluded if 'vision' not in (m.get('capabilities') or [])]} "
                 "are excluded (MODEL_SWITCH_EXCLUDE)")
        else:
            info("all models are vision-capable — no non-vision model to test image-reject (ok)")

        # 4c. PDF (binary document) on a DOCUMENT-capable model -> accepted (200).
        if doc_model:
            st, _d = upload_attachment(token, att_sid, "doc.pdf", "application/pdf", _PDF, doc_model["id"])
            (ok if st == 200 else bad)(
                f"pdf on document model [{doc_model['display_name']}] -> {st} (want 200)")
        else:
            info("no document-capable model — skipping pdf-accept")

        # 4d. PDF on a NON-document model -> rejected (400). This is the key guardrail:
        #     vision-only models (Kimi K3, Grok, ...) must refuse a PDF.
        if nodoc_model:
            st, d = upload_attachment(token, att_sid, "doc.pdf", "application/pdf", _PDF, nodoc_model["id"])
            (ok if st == 400 else bad)(
                f"pdf on NON-document model [{nodoc_model['display_name']}] -> {st} (want 400)  {_short(d, 60)}")
        else:
            bad("no non-document model found — expected some (e.g. Kimi K3) to test pdf-reject")

        # 4e. TEXT / CSV / JSON are text-inline → NO capability needed → ALWAYS accepted,
        #     even on a document-INCAPABLE model. (The whole point of reclassifying
        #     csv/txt as text.)
        text_target = nodoc_model or models[0]
        info(f"text/csv/json on a non-document model [{text_target['display_name']}] — all should pass:")
        for fname, mime, raw in (("note.txt", "text/plain", _TXT),
                                 ("data.csv", "text/csv", _CSV),
                                 ("blob.json", "application/json", _JSON)):
            st, d = upload_attachment(token, att_sid, fname, mime, raw, text_target["id"])
            (ok if st == 200 else bad)(
                f"{fname} on [{text_target['display_name']}] -> {st} (want 200){'' if st == 200 else '  ' + _short(d, 80)}")

    guarded("4. attachments", gating)

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"   - {f}")
        return 1
    if _skips:
        print(f"SKIPPED: {len(_skips)} check(s):")
        for s_ in _skips:
            print(f"   - {s_}")
    print("RESULT: all dropdown / switch / attachment-gating checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
