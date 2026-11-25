#!/usr/bin/env python3
# test-kind: offline
"""Offline regression test for the canonical modelcaps resolver (modelcaps.params).

Verifies, WITHOUT any network / live Bedrock call, that modelcaps.params builds
exactly the params each model accepts and surfaces the catalog correctly:

  * supports_temperature: True for 4.6/older + unknown/None, False for 5.x gen.
  * minimal_thinking_field: disabled for Haiku 5.x, between_tools for Sonnet 5.x,
    None for 4.6.
  * build_inference_config: always maxTokens (clamped to catalog max_output when
    known); temperature only when supported AND provided.
  * build_additional_request_fields: the thinking dict for 5.x when disabling,
    None for 4.6 and when not disabling.
  * resolve_model_id: CRIS/region resolution (global>region-geo>any CRIS>direct;
    passthrough) — global-first per the product requirement.
  * list_models: surfaces per-model pricing (four-key per-1k block) + prompt_caching.

Additive-feature coverage:
  * PRICING — list_models surfaces the four-key pricing block; a known model's
    per-1k == per-token*1000; a missing field -> 0.
  * prompt_caching — capabilities carries a boolean prompt_caching; list_models
    surfaces it; a caching model is True, a non-caching one is False/default-False.

It also guards the VENDORED copies that cannot import the canonical package because
they ship decoupled: the standalone chat-agent service
(agent/ai_assistant/modelcaps/params.py) and the in-VM user LLM helpers
(app/notebook/helpers/modelcaps/params.py). Those sections load each copy by file
path and assert identical behavior to the canonical copy, so the synced files can't
drift silently. (A dedicated byte-level drift guard lives in test_modelcaps_sync.py.)

Run:  .venv/bin/python tests/ai/test_model_params.py   (exit 0 = pass, 1 = fail)
"""
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


# Representative ids across generations.
SONNET_46 = "us.anthropic.claude-sonnet-4-6"
HAIKU_55 = "us.anthropic.claude-haiku-5-5"
SONNET_55 = "us.anthropic.claude-sonnet-5-5"
HAIKU_45 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA_LITE = "us.amazon.nova-2-lite-v1:0"

# Inline FIXTURE catalog (do NOT read a live-generated model_caps.json). This mirrors
# the catalog's `models` map (keyed on BASE ids, geo prefix stripped) and is injected
# into a module's catalog cache to exercise the catalog-PRIMARY path. It is shaped so
# the catalog DISAGREES with the substring fallback on some models to prove the
# catalog wins when present, and carries the ADDITIVE pricing + prompt_caching fields.
FIXTURE_CATALOG = {
    "anthropic.claude-sonnet-4-6": {
        "family": "anthropic",
        "max_output_tokens": 64000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": True,
            "thinking_disable_shape": None,
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
            "prompt_caching": True,
        },
        "pricing": {
            "input_per_1k": 0.003, "output_per_1k": 0.015,
            "cache_read_per_1k": 0.0003, "cache_write_per_1k": 0.00375,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
            {"name": "us-west-2", "cris_profiles": ["global", "us"]},
            {"name": "eu-west-2", "cris_profiles": ["eu", "global"], "direct": True},
        ],
    },
    "anthropic.claude-haiku-5-5": {
        "family": "anthropic",
        "max_output_tokens": 64000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": False,
            "thinking_disable_shape": {"thinking": {"type": "disabled"}},
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
            "prompt_caching": True,
        },
        "pricing": {
            "input_per_1k": 0.001, "output_per_1k": 0.005,
            "cache_read_per_1k": 0.0001, "cache_write_per_1k": 0.00125,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
        ],
    },
    "anthropic.claude-sonnet-5-5": {
        "family": "anthropic",
        "max_output_tokens": 64000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": False,
            "thinking_disable_shape": {"thinking": {"type": "between_tools"}},
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
            "prompt_caching": True,
        },
        "pricing": {
            "input_per_1k": 0.003, "output_per_1k": 0.015,
            "cache_read_per_1k": 0.0003, "cache_write_per_1k": 0.00375,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
        ],
    },
    "anthropic.claude-opus-5-5": {
        "family": "anthropic",
        "max_output_tokens": 64000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": False,
            "thinking_disable_shape": None,
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
            "prompt_caching": True,
        },
        "pricing": {
            "input_per_1k": 0.015, "output_per_1k": 0.075,
            "cache_read_per_1k": 0.0015, "cache_write_per_1k": 0.01875,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
        ],
    },
    # Non-Claude model: small output cap, NO caching, pricing with a MISSING
    # cache_write field (→ 0). Exercises BOTH cris_profiles and direct in regions.
    "amazon.nova-2-lite-v1:0": {
        "family": "amazon",
        "max_output_tokens": 10000,
        "capabilities": {
            "converse_supported": True,
            "supports_temperature": True,
            "thinking_disable_shape": None,
            "supports_system_prompt": True,
            "tool_use": True,
            "vision": True,
            "document_support": True,
            "prompt_caching": False,
        },
        "pricing": {
            "input_per_1k": 0.00033, "output_per_1k": 0.00275,
            "cache_read_per_1k": 0.0, "cache_write_per_1k": 0.0,
        },
        "regions": [
            {"name": "us-east-1", "cris_profiles": ["global", "us"]},
            {"name": "eu-west-1", "cris_profiles": ["eu", "global"]},
            # Both a global CRIS profile AND direct — global must win over direct.
            {"name": "eu-west-2", "cris_profiles": ["global", "eu"], "direct": True},
            # Direct ONLY (no CRIS) — must fall through to the bare base id.
            {"name": "sa-east-1", "direct": True},
            # No "global" here — exercises the "fall to the region's geo profile"
            # branch (us-west-1 → us) and the ap-northeast-1 → jp geo mapping.
            {"name": "us-west-1", "cris_profiles": ["us"]},
            {"name": "ap-northeast-1", "cris_profiles": ["jp", "apac"]},
        ],
    },
}
OPUS_55 = "us.anthropic.claude-opus-5-5"


def _install_catalog(mod, catalog):
    """Point a resolver module at a fixture catalog (or None to force the no-catalog
    fallback path) by seeding its lazy cache. Returns a restore() callable."""
    prev_cache = mod._catalog_cache
    prev_loaded = mod._catalog_loaded
    mod._catalog_cache = catalog
    mod._catalog_loaded = True

    def restore():
        mod._catalog_cache = prev_cache
        mod._catalog_loaded = prev_loaded

    return restore


def main() -> int:
    from modelcaps import params as m

    # Sections 1-5 assert the pre-catalog SUBSTRING-FALLBACK behavior. Force the
    # no-catalog state so this holds even though a real model_caps.json ships next to
    # the canonical params.py.
    _install_catalog(m, None)

    print("== 1. supports_temperature ==")
    check("sonnet-4-6 supports temperature", m.supports_temperature(SONNET_46) is True)
    check("haiku-5-5 does NOT support temperature", m.supports_temperature(HAIKU_55) is False)
    check("sonnet-5-5 does NOT support temperature", m.supports_temperature(SONNET_55) is False)
    check("haiku-4-5 (4.x) supports temperature", m.supports_temperature(HAIKU_45) is True)
    check("None id falls back to supports temperature", m.supports_temperature(None) is True)
    check("non-str id falls back to supports temperature", m.supports_temperature(12345) is True)

    print("== 2. minimal_thinking_field ==")
    check("haiku-5-5 → disabled",
          m.minimal_thinking_field(HAIKU_55) == {"thinking": {"type": "disabled"}})
    check("sonnet-5-5 → between_tools",
          m.minimal_thinking_field(SONNET_55) == {"thinking": {"type": "between_tools"}})
    check("sonnet-4-6 → None", m.minimal_thinking_field(SONNET_46) is None)
    check("haiku-4-5 (4.x) → None", m.minimal_thinking_field(HAIKU_45) is None)

    print("== 3. build_inference_config ==")
    check("4.6 includes temperature",
          m.build_inference_config(SONNET_46, max_tokens=100, temperature=0.2)
          == {"maxTokens": 100, "temperature": 0.2})
    check("haiku-5-5 omits temperature",
          m.build_inference_config(HAIKU_55, max_tokens=100, temperature=0.2)
          == {"maxTokens": 100})
    check("sonnet-5-5 omits temperature",
          m.build_inference_config(SONNET_55, max_tokens=100, temperature=0.2)
          == {"maxTokens": 100})
    check("always includes maxTokens (no temperature arg)",
          m.build_inference_config(SONNET_46, max_tokens=150) == {"maxTokens": 150})
    check("omits temperature when temperature=None even if supported",
          m.build_inference_config(SONNET_46, max_tokens=150, temperature=None)
          == {"maxTokens": 150})

    print("== 4. build_additional_request_fields ==")
    check("haiku-5-5 disable_thinking=True → disabled",
          m.build_additional_request_fields(HAIKU_55, disable_thinking=True)
          == {"thinking": {"type": "disabled"}})
    check("sonnet-5-5 disable_thinking=True → between_tools",
          m.build_additional_request_fields(SONNET_55, disable_thinking=True)
          == {"thinking": {"type": "between_tools"}})
    check("sonnet-4-6 disable_thinking=True → None",
          m.build_additional_request_fields(SONNET_46, disable_thinking=True) is None)
    check("haiku-5-5 disable_thinking=False → None",
          m.build_additional_request_fields(HAIKU_55, disable_thinking=False) is None)

    print("== 5. Sonnet 4.6 params unchanged from today's behavior (fallback path) ==")
    check("4.6 inferenceConfig == {maxTokens, temperature} (no change)",
          m.build_inference_config(SONNET_46, max_tokens=32768, temperature=0.2)
          == {"maxTokens": 32768, "temperature": 0.2})
    check("4.6 additionalModelRequestFields is None (no change)",
          m.build_additional_request_fields(SONNET_46, disable_thinking=True) is None)

    print("== 6. the VENDORED copies stay in sync with the canonical copy (fallback) ==")
    # params.py is vendored into agent/ai_assistant/modelcaps/ and
    # app/notebook/helpers/modelcaps/ because those services run decoupled from the
    # repo root. The vendored files carry a leading comment banner but an identical
    # body; we load each by file path (pure stdlib) and assert identical results.
    import importlib.util

    def _load_by_path(label, relpath):
        path = os.path.join(_ROOT, *relpath)
        if not os.path.exists(path):
            check(f"{label} exists", False, f"missing at {path}")
            return None
        spec = importlib.util.spec_from_file_location(f"copy_{label}", path)
        mod = importlib.util.module_from_spec(spec)
        # Register in sys.modules BEFORE exec_module: Python 3.12+ dataclass
        # processing (AgentPickerConfig is a @dataclass) resolves the owning module
        # via sys.modules[cls.__module__] during decoration, which is None for a
        # by-path load that was never registered. Registering (and cleaning up) keeps
        # this pure-stdlib loader working across Python versions.
        import sys as _sys
        _sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            _sys.modules.pop(spec.name, None)
            raise
        return mod

    def _behaves_like_canonical(mod) -> str:
        """Return '' if mod matches the canonical copy m across all ids, else the
        first diverging call description."""
        for mid in [SONNET_46, HAIKU_55, SONNET_55, HAIKU_45, None, 12345]:
            label = repr(mid)
            if mod.supports_temperature(mid) != m.supports_temperature(mid):
                return f"supports_temperature({label})"
            if mod.minimal_thinking_field(mid) != m.minimal_thinking_field(mid):
                return f"minimal_thinking_field({label})"
            if (mod.build_inference_config(mid, max_tokens=100, temperature=0.2)
                    != m.build_inference_config(mid, max_tokens=100, temperature=0.2)):
                return f"build_inference_config({label})"
            if (mod.build_additional_request_fields(mid, disable_thinking=True)
                    != m.build_additional_request_fields(mid, disable_thinking=True)):
                return f"build_additional_request_fields({label})"
        return ""

    _VENDORED_COPIES = [
        ("agent/ai_assistant copy", ["agent", "ai_assistant", "modelcaps", "params.py"]),
        ("app in-VM helpers copy", ["app", "notebook", "helpers", "modelcaps", "params.py"]),
    ]
    loaded_copies = []
    for label, relpath in _VENDORED_COPIES:
        mod = _load_by_path(label, relpath)
        if mod is not None:
            loaded_copies.append((label, mod))

    for label, mod in loaded_copies:
        restore = _install_catalog(mod, None)
        try:
            diverged = _behaves_like_canonical(mod)
        finally:
            restore()
        check(f"{label} behaves identically to canonical copy (fallback path)",
              diverged == "", f"diverged at {diverged}" if diverged else "")

    print("== 7. catalog-PRIMARY reads (fixture catalog) ==")
    restore_m = _install_catalog(m, FIXTURE_CATALOG)
    try:
        check("catalog supports_temperature True (sonnet-4-6)",
              m.supports_temperature(SONNET_46) is True)
        check("catalog supports_temperature False (haiku-5-5)",
              m.supports_temperature(HAIKU_55) is False)
        check("catalog supports_temperature True (nova, non-Claude)",
              m.supports_temperature(NOVA_LITE) is True)
        check("catalog thinking_disable_shape None (sonnet-4-6)",
              m.minimal_thinking_field(SONNET_46) is None)
        check("catalog thinking_disable_shape disabled (haiku-5-5)",
              m.minimal_thinking_field(HAIKU_55) == {"thinking": {"type": "disabled"}})
        check("catalog thinking_disable_shape between_tools (sonnet-5-5)",
              m.minimal_thinking_field(SONNET_55) == {"thinking": {"type": "between_tools"}})
        check("catalog thinking_disable_shape None (opus-5-5, rejects both)",
              m.minimal_thinking_field(OPUS_55) is None)
        check("catalog thinking_disable_shape None (nova)",
              m.minimal_thinking_field(NOVA_LITE) is None)

        print("== 8. build_inference_config maxTokens clamp (fixture catalog) ==")
        check("clamps 32768 request down to catalog max_output 10000 (nova)",
              m.build_inference_config(NOVA_LITE, max_tokens=32768)
              == {"maxTokens": 10000})
        check("request under catalog cap passes through (nova 500)",
              m.build_inference_config(NOVA_LITE, max_tokens=500)
              == {"maxTokens": 500})
        check("request under catalog cap passes through (sonnet-4-6 32768 < 64000)",
              m.build_inference_config(SONNET_46, max_tokens=32768, temperature=0.2)
              == {"maxTokens": 32768, "temperature": 0.2})
        check("clamp + temperature kept together (nova supports temp)",
              m.build_inference_config(NOVA_LITE, max_tokens=99999, temperature=0.3)
              == {"maxTokens": 10000, "temperature": 0.3})

        print("== 9. resolve_model_id (fixture catalog) ==")
        check("already-prefixed id passes through unchanged",
              m.resolve_model_id("us.anthropic.claude-sonnet-4-6", "us-east-1")
              == "us.anthropic.claude-sonnet-4-6")
        # NEW preference order: global FIRST, then the region's own geo profile,
        # then any remaining CRIS, then direct.
        check("base id → global CRIS profile preferred over region geo (us-east-1)",
              m.resolve_model_id("anthropic.claude-sonnet-4-6", "us-east-1")
              == "global.anthropic.claude-sonnet-4-6")
        check("base id → global beats a direct region entry (nova eu-west-2)",
              m.resolve_model_id("amazon.nova-2-lite-v1:0", "eu-west-2")
              == "global.amazon.nova-2-lite-v1:0")
        check("base id → global when present (nova eu-west-1)",
              m.resolve_model_id("amazon.nova-2-lite-v1:0", "eu-west-1")
              == "global.amazon.nova-2-lite-v1:0")
        check("base id → region geo when NO global (nova us-west-1 → us)",
              m.resolve_model_id("amazon.nova-2-lite-v1:0", "us-west-1")
              == "us.amazon.nova-2-lite-v1:0")
        check("base id → region geo jp over apac (nova ap-northeast-1 → jp)",
              m.resolve_model_id("amazon.nova-2-lite-v1:0", "ap-northeast-1")
              == "jp.amazon.nova-2-lite-v1:0")
        check("base id → bare base when only direct (nova sa-east-1)",
              m.resolve_model_id("amazon.nova-2-lite-v1:0", "sa-east-1")
              == "amazon.nova-2-lite-v1:0")
        check("unknown region → input unchanged",
              m.resolve_model_id("amazon.nova-2-lite-v1:0", "af-south-1")
              == "amazon.nova-2-lite-v1:0")
        check("unknown model → input unchanged",
              m.resolve_model_id("some.unknown-model", "us-east-1")
              == "some.unknown-model")

        print("== 10. Sonnet 4.6 params unchanged via the CATALOG path ==")
        check("4.6 inferenceConfig == {maxTokens, temperature} on catalog path",
              m.build_inference_config(SONNET_46, max_tokens=32768, temperature=0.2)
              == {"maxTokens": 32768, "temperature": 0.2})
        check("4.6 additionalModelRequestFields is None on catalog path",
              m.build_additional_request_fields(SONNET_46, disable_thinking=True) is None)

        print("== 11. list_models surfaces pricing + prompt_caching ==")
        result = m.list_models()
        by_id = {x["model_id"]: x for x in result["models"]}
        check("list_models available with the fixture catalog", result["available"] is True)
        check("list_models count matches fixture", result["count"] == len(FIXTURE_CATALOG))
        # Every listed model carries the two additive fields.
        check("every listed model carries a pricing block",
              all("pricing" in x for x in result["models"]))
        check("every listed model carries prompt_caching (bool)",
              all(isinstance(x["prompt_caching"], bool) for x in result["models"]))
        # Pricing four-key block surfaced verbatim from the catalog.
        nova = by_id["amazon.nova-2-lite-v1:0"]
        check("pricing block has exactly the four per-1k keys",
              set(nova["pricing"].keys())
              == {"input_per_1k", "output_per_1k", "cache_read_per_1k", "cache_write_per_1k"})
        check("list_models pricing matches catalog (nova)",
              nova["pricing"] == FIXTURE_CATALOG["amazon.nova-2-lite-v1:0"]["pricing"])
        # Missing cache fields modeled as 0 in the catalog -> surfaced as 0.
        check("missing cache_write surfaced as 0 (nova)",
              nova["pricing"]["cache_write_per_1k"] == 0.0)

        print("== 12. PRICING per-1k == per-token*1000 (known model) ==")
        # Simulate the generator's per-token→per-1k conversion to prove the catalog's
        # emitted per-1k equals per-token*1000 for a known rate.
        per_token = 0.00000033  # nova-2-lite input_cost_per_token
        check("per-token*1000 == catalog input_per_1k (nova)",
              round(per_token * 1000, 10) == round(nova["pricing"]["input_per_1k"], 10))

        print("== 13. prompt_caching derived boolean surfaces correctly ==")
        check("caching model surfaces prompt_caching True (haiku-5-5)",
              by_id["anthropic.claude-haiku-5-5"]["prompt_caching"] is True)
        check("non-caching model surfaces prompt_caching False (nova)",
              by_id["amazon.nova-2-lite-v1:0"]["prompt_caching"] is False)
        # capabilities-level read: the resolver reads the catalog's boolean as-is.
        check("catalog capabilities.prompt_caching True (sonnet-4-6)",
              FIXTURE_CATALOG["anthropic.claude-sonnet-4-6"]["capabilities"]["prompt_caching"] is True)
    finally:
        restore_m()

    print("== 14. prompt_caching DEFAULT False when the catalog entry lacks it ==")
    # A catalog entry with NO prompt_caching field -> list_models surfaces False.
    NO_PC = {
        "legacy.model-v1:0": {
            "family": "legacy",
            "max_output_tokens": 4096,
            "capabilities": {
                "converse_supported": True,
                "supports_temperature": None,
                "thinking_disable_shape": None,
                "supports_system_prompt": None,
                "tool_use": None,
                "vision": False,
                "document_support": False,
                # NOTE: no prompt_caching key — pre-additive catalog shape.
            },
            "regions": [{"name": "us-east-1", "direct": True}],
        }
    }
    restore_m = _install_catalog(m, NO_PC)
    try:
        legacy = m.list_models()["models"][0]
        check("prompt_caching defaults to False when absent from capabilities",
              legacy["prompt_caching"] is False)
        check("pricing is None when absent from the entry (pre-additive)",
              legacy["pricing"] is None)
    finally:
        restore_m()

    print("== 14b. top_models_for_agent (dropdown picker, real shipped catalog) ==")
    # Run against the REAL model_caps.json shipped next to params.py (the picker needs
    # a realistic multi-model catalog). Force a fresh load of the on-disk catalog.
    m._catalog_cache = None
    m._catalog_loaded = False
    DEFAULT = "us.anthropic.claude-haiku-5-5"
    REGION = "us-west-2"
    res = m.top_models_for_agent(n=10, region=REGION, default_id=DEFAULT)
    if res.get("available"):
        picks = res["models"]
        ids = [p["id"] for p in picks]
        check("returns available + non-empty", res["available"] and res["count"] > 0,
              f"count={res['count']}")
        check("echoes the region it was scoped to", res.get("region") == REGION)
        check("count respects n (<=10)", res["count"] <= 10, f"count={res['count']}")
        # Region gate: every pick is actually offered in REGION, and carries a
        # region-resolved invocable id matching resolve_model_id.
        region_ok = all(
            any(reg.get("name") == REGION
                for reg in (m._catalog_entry(p["id"]) or {}).get("regions", []))
            for p in picks
        )
        check("every pick is available in the region", region_ok)
        resolved_ok = all(
            p.get("resolved_id") == m.resolve_model_id(p["id"], REGION) for p in picks
        )
        check("resolved_id == resolve_model_id(id, region) for every pick", resolved_ok)
        check("default resolved_id prefers global (us-west-2 has global+us)",
              next(p for p in picks if p["is_default"])["resolved_id"]
              == "global.anthropic.claude-haiku-5-5")
        check("default is present and base-normalized",
              res["default_id"] == "anthropic.claude-haiku-5-5"
              and "anthropic.claude-haiku-5-5" in ids)
        default_pick = next((p for p in picks if p["is_default"]), None)
        check("default pick flagged is_default + cost 1x",
              default_pick is not None and default_pick["cost_multiplier"] == 1.0
              and default_pick["cost_label"] == "1x")
        check("default is listed first", picks and picks[0]["is_default"])
        check("no duplicate ids", len(ids) == len(set(ids)))
        # Every pick passed the hard gate (capability + context; region checked above).
        gate_ok = all(
            (m._catalog_entry(p["id"]) or {}).get("capabilities", {}).get("tool_use")
            and (m._catalog_entry(p["id"]) or {}).get("capabilities", {}).get("supports_system_prompt")
            and (p["context_tokens"] or 0) >= 200_000
            for p in picks
        )
        check("all picks pass the hard gate (tool_use + system_prompt + ctx>=200K)", gate_ok)
        # Family cap respected (<=2 per provider by default).
        from collections import Counter
        fam = Counter(p["provider"] for p in picks)
        # family_cap is a SOFT cap: it limits the score-ordered fill to <=2 per
        # family, but the backfill (when <=2/family can't reach n) and the
        # premium-guarantee swap may legitimately push one family to 3 so the list
        # still returns n models. So assert the cap holds UNLESS the list is at full
        # n (backfill relaxed it), and never grossly exceeds it (<=3).
        cap_ok = all(v <= 2 for v in fam.values()) or res["count"] >= 10
        check("family cap respected (soft; relaxed only to fill n)",
              cap_ok and all(v <= 3 for v in fam.values()), f"counts={dict(fam)}")
        # Premium guarantee: at least one premium-tier pick.
        check("at least one premium-tier model present",
              any(p["tier"] == "premium" for p in picks),
              f"tiers={[p['tier'] for p in picks]}")
        # Generated display names are clean: non-empty, no stray ':N' version-set
        # tag, and no raw family prefix (e.g. "anthropic.claude-..."). A version dot
        # like "5.5" is fine — only a lowercase-dotted provider prefix is a leak.
        import re as _re
        def _name_clean(name):
            return bool(name.strip()) and ":" not in name and not _re.match(r"^[a-z0-9]+\.", name)
        check("display names are generated/clean",
              all(_name_clean(p["display_name"]) for p in picks))
        # Cost multipliers are raw ratios anchored on the default (monotone w/ price
        # isn't required, but the default must be the 1.0 anchor and all > 0).
        check("cost multipliers positive, default == 1.0",
              all(p["cost_multiplier"] > 0 for p in picks)
              and default_pick["cost_multiplier"] == 1.0)
        # Descriptions are non-empty and lead with a context window.
        check("descriptions non-empty with context window",
              all(p["description"] and "context" in p["description"] for p in picks))
        # n is honored downward.
        small = m.top_models_for_agent(n=3, region=REGION, default_id=DEFAULT)
        check("n=3 returns exactly 3 (catalog has >=3 eligible)", small["count"] == 3,
              f"count={small['count']}")
        # A region with NO models yields an empty, graceful result.
        none_region = m.top_models_for_agent(n=10, region="me-central-1", default_id=DEFAULT)
        check("region with no models -> available False, empty",
              none_region["available"] is False and none_region["count"] == 0
              and none_region["region"] == "me-central-1")

        # STREAMING tool-use gate: no picked model may be a tool_use=True /
        # tool_use_streaming=False model (e.g. Meta Llama 4) — our agent streams tools.
        stream_gate_ok = True
        for p in picks:
            caps = (m._catalog_entry(p["id"]) or {}).get("capabilities", {})
            if caps.get("tool_use_streaming") is False:
                stream_gate_ok = False
        check("no pick lacks streaming tool-use (Llama-4 class excluded)", stream_gate_ok,
              f"ids={ids}")
        check("Llama 4 is NOT in the picks (streaming tool-use gate)",
              not any("llama4" in i for i in ids))

        # EXCLUDE denylist: an excluded id never appears, even though it's otherwise
        # eligible. Use the shortlist's #2 as a reproducible exclusion target.
        if len(ids) >= 2:
            victim = ids[1]
            ex = m.top_models_for_agent(n=10, region=REGION, default_id=DEFAULT,
                                        exclude=[victim])
            ex_ids = [x["id"] for x in ex["models"]]
            check("excluded model is dropped from the list", victim not in ex_ids,
                  f"excluded {victim}, got {ex_ids}")
            # A CRIS-prefixed exclude id is normalized to base and still excludes.
            ex2 = m.top_models_for_agent(n=10, region=REGION, default_id=DEFAULT,
                                         exclude=["us." + victim])
            check("CRIS-prefixed exclude id is normalized + dropped",
                  victim not in [x["id"] for x in ex2["models"]])
    else:
        check("top_models_for_agent available on shipped catalog", False,
              "no catalog / no eligible models")

    # no-catalog degradation: force the empty-catalog path and assert a graceful dict.
    restore_empty = _install_catalog(m, {})
    try:
        empty = m.top_models_for_agent(n=10, region=REGION, default_id=DEFAULT)
        check("no-catalog -> available False, empty models",
              empty["available"] is False and empty["count"] == 0
              and empty["models"] == [])
    finally:
        restore_empty()
    print("== 14c. fallback_chain (model-unavailability degrade path) ==")
    m._catalog_cache = None
    m._catalog_loaded = False
    top = m.top_models_for_agent(n=10, region=REGION, default_id=DEFAULT)
    ranked = [x["id"] for x in top["models"]]
    default_base = "anthropic.claude-haiku-5-5"
    if ranked:
        # Picking the TOP model -> chain walks the whole shortlist; default present.
        # Pin n=10 on every chain call so the chain's shortlist pool matches `ranked`
        # (which was built with n=10) regardless of fallback_chain's default n.
        ch_top = m.fallback_chain(ranked[0], REGION, DEFAULT, n=10)
        check("chain for top model starts with it", ch_top[0] == ranked[0])
        check("chain contains the default (floor)", default_base in ch_top)
        check("chain has no duplicates", len(ch_top) == len(set(ch_top)))
        check("chain bounded by shortlist+default", len(ch_top) <= len(ranked) + 1)
        # Picking the BOTTOM model -> only it + default (your "at bottom -> default").
        ch_bot = m.fallback_chain(ranked[-1], REGION, DEFAULT, n=10)
        check("chain for bottom model is [itself, default]",
              ch_bot[0] == ranked[-1] and ch_bot[-1] == default_base,
              f"got {ch_bot}")
        # Picking a MIDDLE model -> everything strictly BELOW it by score, then default.
        if len(ranked) >= 4:
            mid = ranked[3]
            ch_mid = m.fallback_chain(mid, REGION, DEFAULT, n=10)
            below = ranked[4:]
            check("chain for a middle model = [itself] + lower-ranked... + default",
                  ch_mid[0] == mid
                  and all(x in ch_mid for x in below)
                  and ch_mid[-1] == default_base,
                  f"got {ch_mid}")
            # Higher-ranked models are excluded — EXCEPT the default, which is always
            # allowed as the guaranteed floor even when it outranks the pick.
            higher_non_default = [h for h in ranked[:3] if h != default_base]
            check("chain for a middle model excludes higher-ranked (non-default) models",
                  not any(h in ch_mid for h in higher_non_default),
                  f"got {ch_mid}")
        # An unknown pick still leads, then falls through to the shortlist + default.
        ch_unk = m.fallback_chain("some.unknown-model", REGION, DEFAULT, n=10)
        check("unknown pick leads the chain, default present",
              ch_unk[0] == "some.unknown-model" and default_base in ch_unk)
        # A region with no models still yields at least the default as a floor.
        ch_none = m.fallback_chain("anthropic.claude-sonnet-5-5", "me-central-1", DEFAULT)
        check("empty-region chain still contains the default floor",
              default_base in ch_none, f"got {ch_none}")
    else:
        check("fallback_chain testable on shipped catalog", False, "no ranked models")

    # Restore the real on-disk catalog for the sections below.
    m._catalog_cache = None
    m._catalog_loaded = False

    print("== 15. all VENDORED copies identical on the fixture-catalog path ==")
    CATALOG_IDS = [SONNET_46, HAIKU_55, SONNET_55, OPUS_55, NOVA_LITE]
    REGIONS = ["us-east-1", "eu-west-1", "eu-west-2", "ap-south-1"]

    def _catalog_behaves_like_canonical(mod) -> str:
        for mid in CATALOG_IDS:
            label = repr(mid)
            if mod.supports_temperature(mid) != m.supports_temperature(mid):
                return f"supports_temperature({label})"
            if mod.minimal_thinking_field(mid) != m.minimal_thinking_field(mid):
                return f"minimal_thinking_field({label})"
            if (mod.build_inference_config(mid, max_tokens=99999, temperature=0.2)
                    != m.build_inference_config(mid, max_tokens=99999, temperature=0.2)):
                return f"build_inference_config({label})"
            for base in [mid, mod._base_id(mid)]:
                for reg in REGIONS:
                    if mod.resolve_model_id(base, reg) != m.resolve_model_id(base, reg):
                        return f"resolve_model_id({base!r}, {reg!r})"
        # list_models parity (pricing + prompt_caching included).
        if mod.list_models() != m.list_models():
            return "list_models()"
        # top_models_for_agent parity (dropdown picker on the same fixture catalog),
        # both region-scoped and global.
        for _reg in ("us-east-1", None):
            if (mod.top_models_for_agent(n=10, region=_reg, default_id=HAIKU_55)
                    != m.top_models_for_agent(n=10, region=_reg, default_id=HAIKU_55)):
                return f"top_models_for_agent(region={_reg!r})"
        # fallback_chain parity on the fixture catalog.
        for _pick in (SONNET_55, HAIKU_55, "x.unknown"):
            if (mod.fallback_chain(_pick, "us-east-1", HAIKU_55)
                    != m.fallback_chain(_pick, "us-east-1", HAIKU_55)):
                return f"fallback_chain({_pick!r})"
        return ""

    restore_m = _install_catalog(m, FIXTURE_CATALOG)
    try:
        for label, mod in loaded_copies:
            restore = _install_catalog(mod, FIXTURE_CATALOG)
            try:
                diverged = _catalog_behaves_like_canonical(mod)
            finally:
                restore()
            check(f"{label} behaves identically to canonical copy (catalog path)",
                  diverged == "", f"diverged at {diverged}" if diverged else "")
    finally:
        restore_m()

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("RESULT: all checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
