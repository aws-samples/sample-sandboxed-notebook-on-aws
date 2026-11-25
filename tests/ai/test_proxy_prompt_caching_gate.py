#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the PROXY's prompt-caching gate (issue: the proxy attached
cache_config / cache_tools unconditionally).

WHAT THIS PROVES (no stack, no network, no live Bedrock)
--------------------------------------------------------
Uses the REAL canonical catalog (modelcaps/model_caps.json) and captures the kwargs the
proxy hands to BedrockModel. Nothing is constructed for real: BedrockModel is replaced
by a recorder.

Cases:
  helper     _prompt_caching_kwargs: a Claude id with prompt_caching=true gets
             cache_config (strategy "auto") and cache_tools "default"; a Claude id
             with prompt_caching=false and a Llama id get {}; a base id and its
             region-prefixed form are gated the same way.
  get_model  proxy notebook_agent.get_model(): cache kwargs present only for the
             caching model; temperature and max_tokens are unchanged by the gate.
  intel      intel/generate.py uses the same helper (same object), so its BedrockModel
             gets the same gate.

Run:
    .venv/bin/python tests/ai/test_proxy_prompt_caching_gate.py
Exit 0 = all passed; 1 = at least one failed.
"""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from strands.models.bedrock import CacheConfig  # noqa: E402

import proxy.notebook.ai.notebook_agent as pna  # noqa: E402
import proxy.notebook.ai.intel.generate as intel_gen  # noqa: E402
from modelcaps import supports_prompt_caching, supports_temperature  # noqa: E402

# Catalog facts this test depends on (checked below, so a catalog regen fails loudly).
CACHING_CLAUDE = "anthropic.claude-haiku-5-5"            # prompt_caching: true
NON_CACHING_CLAUDE = "anthropic.claude-opus-4-7"          # prompt_caching: false
NON_CACHING_LLAMA = "meta.llama3-1-8b-instruct-v1:0"      # prompt_caching: false

_results = []


def record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail and not ok else ""))


def _has_cache(kwargs: dict) -> bool:
    return "cache_config" in kwargs or "cache_tools" in kwargs


def case_catalog_facts():
    print("\n[catalog facts]")
    record("caching Claude id is prompt_caching=true",
           supports_prompt_caching(CACHING_CLAUDE))
    record("non-caching Claude id is prompt_caching=false",
           not supports_prompt_caching(NON_CACHING_CLAUDE))
    record("non-caching Llama id is prompt_caching=false",
           not supports_prompt_caching(NON_CACHING_LLAMA))


def case_helper():
    print("\n[_prompt_caching_kwargs]")
    kw = pna._prompt_caching_kwargs(CACHING_CLAUDE)
    record("caching Claude: cache_config attached",
           isinstance(kw.get("cache_config"), CacheConfig)
           and kw["cache_config"].strategy == "auto", repr(kw))
    record("caching Claude: cache_tools is 'default'", kw.get("cache_tools") == "default",
           repr(kw))
    record("caching Claude, region-prefixed id: same gate",
           pna._prompt_caching_kwargs(f"us.{CACHING_CLAUDE}") == kw)
    record("non-caching Claude: no cache kwargs",
           pna._prompt_caching_kwargs(NON_CACHING_CLAUDE) == {})
    record("non-caching Llama: no cache kwargs",
           pna._prompt_caching_kwargs(NON_CACHING_LLAMA) == {})
    record("unknown model id: no cache kwargs (safe default)",
           pna._prompt_caching_kwargs("vendor.not-in-catalog-v1") == {})


class _Recorder:
    """Stands in for BedrockModel; records the kwargs the proxy passed."""
    last = None

    def __init__(self, **kwargs):
        _Recorder.last = kwargs


def _get_model_for(model_id: str) -> dict:
    """Run the proxy's get_model() for ``model_id`` and return the kwargs it passed."""
    saved = (pna.AI_MODEL_ID, pna._model, pna.BedrockModel)
    try:
        pna.AI_MODEL_ID = model_id
        pna._model = None
        pna.BedrockModel = _Recorder
        pna.get_model()
        return dict(_Recorder.last)
    finally:
        pna.AI_MODEL_ID, pna._model, pna.BedrockModel = saved


def case_get_model():
    print("\n[proxy get_model()]")
    kw = _get_model_for(CACHING_CLAUDE)
    record("caching Claude: cache_config + cache_tools present",
           isinstance(kw.get("cache_config"), CacheConfig)
           and kw.get("cache_tools") == "default", sorted(kw))

    kw = _get_model_for(NON_CACHING_CLAUDE)
    record("non-caching Claude: no cache_config / cache_tools", not _has_cache(kw), sorted(kw))

    kw = _get_model_for(NON_CACHING_LLAMA)
    record("non-caching Llama: no cache_config / cache_tools", not _has_cache(kw), sorted(kw))

    # Gate touches only caching: temperature and max_tokens are as before.
    for mid in (CACHING_CLAUDE, NON_CACHING_CLAUDE):
        kw = _get_model_for(mid)
        record(f"{mid}: temperature gating unchanged",
               ("temperature" in kw) == supports_temperature(mid), sorted(kw))
        record(f"{mid}: max_tokens still passed",
               kw.get("max_tokens") == pna.AGENT_MAX_TOKENS, repr(kw.get("max_tokens")))
    pna._model = None


def case_intel_wiring():
    print("\n[intel generate.py]")
    record("intel generate uses the proxy helper (same object)",
           intel_gen._prompt_caching_kwargs is pna._prompt_caching_kwargs)


def main():
    print("=" * 72)
    print(" proxy prompt-caching gate — OFFLINE")
    print("=" * 72)
    case_catalog_facts()
    case_helper()
    case_get_model()
    case_intel_wiring()
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 72)
    print(f" SUMMARY: {passed} passed, {failed} failed")
    print("=" * 72)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
