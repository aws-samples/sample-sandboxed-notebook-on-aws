#!/usr/bin/env python3
# test-kind: offline
"""Offline regression test for the model-catalog listing API (FEAT-003).

Verifies, WITHOUT any network / live AWS call, that the catalog-listing helper
``modelcaps.list_models`` and the ``GET /ai/models`` route in
``proxy.notebook.routes.ai`` expose the generated model catalog in the shape a UI
model-picker dropdown needs:

  * list_models groups models by provider (family) and annotates each with the
    capability fields the dropdown renders (label, supports_temperature,
    thinking-disable support, max_output_tokens, regions available, etc.).
  * Every field is DERIVED from the catalog — no model list is hardcoded.
  * When the catalog is ABSENT, the result degrades gracefully to a clearly-flagged
    empty list (available: False) and never raises.
  * The GET /ai/models route now returns the CURATED region-scoped top-N via
    top_models_for_agent() (id/resolved_id/display_name/description/cost_label/tier/
    is_default/pricing/...), falling back through the SAME helper (catalog=canonical)
    to modelcaps/model_caps.json when the primary path is empty.

It uses an inline FIXTURE catalog (same FEAT-001 schema the sync test uses); it does
NOT read a live-generated file for the helper assertions.

Run:  python tests/ai/test_ai_models_route.py   (exit 0 = pass, 1 = fail)
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# Inline FIXTURE catalog (FEAT-001 `models` map shape: keyed on BASE ids). Two
# providers so grouping can be asserted; nova exercises null capability fields +
# multiple regions + a small output cap; one entry carries a thinking_disable_shape.
FIXTURE_CATALOG = {
    "anthropic.claude-haiku-5-5": {
        "family": "anthropic",
        "max_input_tokens": 1000000,
        "max_output_tokens": 64000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": False,
            "thinking_disable_shape": {"thinking": {"type": "disabled"}},
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
            {"name": "us-west-2", "cris_profiles": ["us"], "direct": True},
        ],
    },
    "anthropic.claude-sonnet-4-6": {
        "family": "anthropic",
        "max_input_tokens": 1000000,
        "max_output_tokens": 64000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": True,
            "thinking_disable_shape": None,
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
        ],
    },
    "amazon.nova-2-lite-v1:0": {
        "family": "amazon",
        "max_input_tokens": 300000,
        "max_output_tokens": 10000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": True,
            "thinking_disable_shape": None,
            "supports_system_prompt": None,
            "tool_use": None,
            "vision": False,
            "document_support": False,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
            {"name": "eu-west-1", "cris_profiles": ["eu"]},
        ],
    },
}


def main() -> int:
    from modelcaps import params as m

    print("== 1. list_models groups by provider with dropdown fields (fixture) ==")
    res = m.list_models(FIXTURE_CATALOG)
    check("available is True when catalog has models", res["available"] is True)
    check("count equals number of fixture models", res["count"] == 3)
    check("providers sorted (amazon before anthropic)",
          res["providers"] == ["amazon", "anthropic"])
    check("by_provider groups the two anthropic models",
          [mm["model_id"] for mm in res["by_provider"]["anthropic"]]
          == ["anthropic.claude-haiku-5-5", "anthropic.claude-sonnet-4-6"])
    check("by_provider groups the one amazon model",
          [mm["model_id"] for mm in res["by_provider"]["amazon"]]
          == ["amazon.nova-2-lite-v1:0"])
    check("flat models list sorted by (provider, model_id)",
          [mm["model_id"] for mm in res["models"]]
          == ["amazon.nova-2-lite-v1:0",
              "anthropic.claude-haiku-5-5",
              "anthropic.claude-sonnet-4-6"])

    print("== 2. per-model fields are derived from the catalog ==")
    haiku = next(mm for mm in res["models"] if mm["model_id"] == "anthropic.claude-haiku-5-5")
    nova = next(mm for mm in res["models"] if mm["model_id"] == "amazon.nova-2-lite-v1:0")
    check("haiku provider == anthropic", haiku["provider"] == "anthropic")
    check("haiku has a display label", bool(haiku["label"]) and "Anthropic" in haiku["label"])
    check("haiku supports_temperature False (from catalog)",
          haiku["supports_temperature"] is False)
    check("haiku supports_thinking_disable True (shape present)",
          haiku["supports_thinking_disable"] is True)
    check("haiku thinking_disable_shape passed through verbatim",
          haiku["thinking_disable_shape"] == {"thinking": {"type": "disabled"}})
    check("haiku max_output_tokens from catalog", haiku["max_output_tokens"] == 64000)
    check("haiku regions available (sorted names)",
          haiku["regions"] == ["us-east-1", "us-west-2"])
    check("nova supports_thinking_disable False (shape null)",
          nova["supports_thinking_disable"] is False)
    check("nova thinking_disable_shape None", nova["thinking_disable_shape"] is None)
    check("nova small max_output cap from catalog", nova["max_output_tokens"] == 10000)
    check("nova null capability fields preserved as null (tool_use)",
          nova["tool_use"] is None and nova["supports_system_prompt"] is None)
    check("nova regions available", nova["regions"] == ["eu-west-1", "us-east-1"])

    print("== 3. absent catalog degrades gracefully (flagged-empty, no raise) ==")
    empty = m.list_models({})
    check("available False when catalog empty", empty["available"] is False)
    check("count 0 when catalog empty", empty["count"] == 0)
    check("models empty list", empty["models"] == [])
    check("providers empty list", empty["providers"] == [])
    check("by_provider empty dict", empty["by_provider"] == {})
    # None catalog arg → falls back to the module's lazy next-to-file cache. Force the
    # no-catalog state so this asserts the graceful-empty path, not a shipped file.
    prev_cache, prev_loaded = m._catalog_cache, m._catalog_loaded
    m._catalog_cache, m._catalog_loaded = None, True
    try:
        none_res = m.list_models(None)
        check("no sibling catalog → available False", none_res["available"] is False)
        check("no sibling catalog → count 0", none_res["count"] == 0)
    finally:
        m._catalog_cache, m._catalog_loaded = prev_cache, prev_loaded

    print("== 4. GET /ai/models route returns the curated top-N shape ==")
    from proxy.notebook.routes import ai as ai_routes

    # Point the module-level lazy cache at the fixture so the route's primary path
    # (top_models_for_agent() with no catalog arg) returns the fixture without hitting
    # any file. Pin AI_REGION/AI_MODEL_ID (the route reads these for region/default_id)
    # to a region + default the fixture actually offers (us-east-1 / haiku 5.5).
    from modelcaps import params as mpm
    prev_cache, prev_loaded = mpm._catalog_cache, mpm._catalog_loaded
    prev_region, prev_model = ai_routes.AI_REGION, ai_routes.AI_MODEL_ID
    mpm._catalog_cache, mpm._catalog_loaded = FIXTURE_CATALOG, True
    ai_routes.AI_REGION = "us-east-1"
    ai_routes.AI_MODEL_ID = "anthropic.claude-haiku-5-5"
    try:
        route_res = asyncio.run(ai_routes.ai_models())
        check("route available True", route_res["available"] is True)
        check("route carries top-N envelope fields",
              all(k in route_res for k in ("available", "count", "region", "default_id", "models")))
        check("route region echoes the scoped region", route_res["region"] == "us-east-1")
        check("route default_id is the base id", route_res["default_id"] == "anthropic.claude-haiku-5-5")
        check("route models is a non-empty list", isinstance(route_res["models"], list) and route_res["models"])
        check("every model row carries the dropdown fields",
              all(all(k in mm for k in ("id", "resolved_id", "display_name", "description",
                                        "cost_label", "tier", "is_default", "pricing"))
                  for mm in route_res["models"]))
        check("exactly one row flagged is_default",
              sum(1 for mm in route_res["models"] if mm["is_default"]) == 1)
        default_row = next(mm for mm in route_res["models"] if mm["is_default"])
        check("the default row is the pinned default id",
              default_row["id"] == "anthropic.claude-haiku-5-5")
        check("default row resolved_id is region-invocable (global CRIS)",
              default_row["resolved_id"] == "global.anthropic.claude-haiku-5-5")
        check("default row is listed FIRST", route_res["models"][0]["is_default"] is True)
    finally:
        mpm._catalog_cache, mpm._catalog_loaded = prev_cache, prev_loaded
        ai_routes.AI_REGION, ai_routes.AI_MODEL_ID = prev_region, prev_model

    print("== 5. route falls back through top_models_for_agent(catalog=canonical) when primary empty ==")
    # Force the primary (next-to-params) catalog absent; the route must fall back to
    # the canonical modelcaps catalog loader, reshaped THROUGH the same helper. If the
    # canonical file is present we expect a non-empty curated list; if absent, a
    # flagged-empty result (never 500).
    prev_region, prev_model = ai_routes.AI_REGION, ai_routes.AI_MODEL_ID
    mpm._catalog_cache, mpm._catalog_loaded = {}, True  # sibling resolves to empty
    ai_routes.AI_REGION = "us-east-1"
    ai_routes.AI_MODEL_ID = "us.anthropic.claude-haiku-5-5"
    try:
        route_res = asyncio.run(ai_routes.ai_models())
        canonical_present = ai_routes._CANONICAL_CATALOG_PATH.exists()
        if canonical_present:
            check("fallback surfaces the real canonical catalog (curated, non-empty)",
                  route_res["available"] is True and route_res["count"] > 0)
            check("fallback rows carry the curated shape",
                  all(all(k in mm for k in ("id", "resolved_id", "display_name",
                                            "cost_label", "tier", "is_default"))
                      for mm in route_res["models"]))
            check("fallback flags exactly one default (or zero if default not offered)",
                  sum(1 for mm in route_res["models"] if mm["is_default"]) <= 1)
        else:
            check("no canonical file → flagged-empty (no raise)",
                  route_res["available"] is False and route_res["count"] == 0)
    finally:
        mpm._catalog_cache, mpm._catalog_loaded = prev_cache, prev_loaded
        ai_routes.AI_REGION, ai_routes.AI_MODEL_ID = prev_region, prev_model

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("RESULT: all checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
