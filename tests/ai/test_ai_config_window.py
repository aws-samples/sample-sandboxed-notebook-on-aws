#!/usr/bin/env python3
# test-kind: offline
"""Offline test: GET /ai/config exposes the chat agent's conversation window size.

The web chat shows the last ``agent_conversation_window_size // 2`` turns as "in the
AI's context". The value has ONE source, the AGENT_CONVERSATION_WINDOW_SIZE env var
(scripts/config.sh); the agent (agent/ai_assistant/constants.py) and the proxy
(proxy/notebook/ai/constants.py) both read it with the same default (10) and the
same parsing. This checks, each in a fresh interpreter so the env is isolated:
  * unset -> /ai/config returns 10, and the agent constant is 10;
  * AGENT_CONVERSATION_WINDOW_SIZE=14 -> both 14;
  * a bad value ("abc", "-3") -> both fall back to 10; "1" -> both floor at 2.
No network / AWS call: the credential probe is stubbed.

Run:  python tests/ai/test_ai_config_window.py   (exit 0 = pass, 1 = fail)
"""
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROBE = r"""
import asyncio, importlib.util, json, os, sys
sys.path.insert(0, ROOT)
from proxy.notebook.routes import ai as ai_routes
ai_routes._ai_credentials_available = lambda: True
ai_routes._AI_AVAILABLE_CACHE["value"] = None
cfg = asyncio.run(ai_routes.ai_config())
spec = importlib.util.spec_from_file_location(
    "agent_constants", os.path.join(ROOT, "agent", "ai_assistant", "constants.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps({"route": cfg.get("agent_conversation_window_size"),
                  "ai_available": cfg.get("ai_available"),
                  "agent": mod.AGENT_CONVERSATION_WINDOW_SIZE}))
"""

_failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def probe(value):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_CONVERSATION_WINDOW_SIZE"}
    if value is not None:
        env["AGENT_CONVERSATION_WINDOW_SIZE"] = value
    out = subprocess.run([sys.executable, "-c", f"ROOT = {_ROOT!r}\n" + _PROBE],
                         env=env, cwd=_ROOT, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[-400:]}
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    for value, expected in ((None, 10), ("14", 14), ("abc", 10), ("-3", 10), ("1", 2), ("", 10)):
        res = probe(value)
        label = "unset" if value is None else repr(value)
        check(f"{label}: probe ran", "error" not in res, res.get("error", ""))
        if "error" in res:
            continue
        check(f"{label}: /ai/config agent_conversation_window_size == {expected}", res["route"] == expected, res)
        check(f"{label}: agent constant == {expected} (same source, same default)", res["agent"] == expected, res)
        check(f"{label}: existing ai_available field intact", res["ai_available"] is True, res)
    print("PASS" if not _failures else f"FAIL ({len(_failures)}): {_failures}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
