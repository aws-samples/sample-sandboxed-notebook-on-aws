"""
Per-model Bedrock inference-parameter resolver (catalog-primary, substring-fallback).

CANONICAL runtime resolver for the ``modelcaps`` package. Single source of truth for
building the inference parameters a given Bedrock model actually accepts, so the
product can run on models that deprecate `temperature` and need `thinking`
disabled/changed as cleanly as it runs on the current default Sonnet 4.6. Pure,
stateless, dependency-free (STDLIB ONLY — no boto3/third-party) so it is trivially
unit-testable offline and safe to import from the proxy, the standalone agent, and
the in-VM app. The generator (``modelcaps/refresh.py``) is the BUILD-time cousin that
produces the catalog; it is NEVER imported here.

This file is the canonical copy. The runtime trio (``params.py``, ``__init__.py``,
``model_caps.json``) is MECHANICALLY vendored next to the isolated services
(agent/ai_assistant/modelcaps/, app/notebook/helpers/modelcaps/) by
``scripts/sync_modelcaps.sh`` — never hand-edit a vendored copy; edit here and re-sync.

TWO-LAYER RESOLUTION:
  1. PRIMARY — the generated capability catalog, ``model_caps.json``, shipped NEXT TO
     this file (``Path(__file__).parent / "model_caps.json"``), produced by
     ``modelcaps/refresh.py``. It is keyed on BASE model ids (geo prefix stripped)
     and carries, per model, ``capabilities.supports_temperature``,
     ``capabilities.thinking_disable_shape`` (already the exact
     ``{"thinking": {...}}`` payload or null), ``capabilities.prompt_caching``,
     ``max_output_tokens``, a per-1k ``pricing`` block, and a ``regions`` array in
     the ``{name, cris_profiles:[...]}`` / ``{name, direct}`` shape used for CRIS
     resolution. The catalog is loaded LAZILY and cached.
  2. FALLBACK — when the catalog file is absent/incomplete (e.g. before it is
     generated, or for a model id not in the catalog), the functions fall back to
     the verified Claude-specific substring rules below, so nothing breaks
     pre-catalog and the current default Sonnet 4.6 behaves byte-for-byte as today.

Verified per-model facts (ground truth fallback — do NOT re-probe):

- Sonnet 4.6 (``us.anthropic.claude-sonnet-4-6``, current default):
    * accepts ``temperature``
    * accepts ``thinking:{"type":"disabled"}``; REJECTS ``thinking:{"type":"between_tools"}``
    * no verbose default reasoning block
- Sonnet 5.5 (``us.anthropic.claude-sonnet-5-5``):
    * ``temperature`` / ``top_p`` DEPRECATED (ValidationException if sent)
    * ``thinking:{"type":"disabled"}`` REJECTED; only ``thinking:{"type":"between_tools"}`` accepted
    * default thinking verbose
- Haiku 5.5 (``us.anthropic.claude-haiku-5-5``):
    * ``temperature`` DEPRECATED (rejected)
    * ``thinking:{"type":"disabled"}`` accepted (``between_tools`` rejected)
    * default thinking verbose and eats maxTokens — disabling it is what makes Haiku fast/cheap

``thinking`` is passed to Converse via ``additionalModelRequestFields={"thinking": {...}}``.
Strands ``BedrockModel`` accepts ``additional_request_fields`` (dict) in its config,
which it forwards as ``additionalModelRequestFields``; it also takes ``temperature``
and ``max_tokens`` directly, and auto-strips ``thinking`` when tool_choice forces tool use.

Model ids look like ``us.anthropic.claude-haiku-5-5``, ``us.anthropic.claude-sonnet-4-6``,
``us.anthropic.claude-haiku-4-5-20251001-v1:0``. In the fallback path, generation is
detected by a robust lowercased substring match (``sonnet-5`` / ``haiku-5`` identify
the 5.x generation and do NOT false-match the 4.x ids). Unknown / None / non-str ids
fall back to the safe current-default behavior (supports temperature, no thinking
override).

CROSS-VENDOR NOTE: the fallback rules are Claude-specific. Any id that doesn't match
the 5.x-Claude substrings (Amazon Nova, Cohere, Llama, unknown/None) takes the safe
default path — supports_temperature True, no thinking field. The catalog path is
generic across vendors and is preferred whenever the catalog knows the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Geo/CRIS prefixes that may lead an invocable model id. The catalog is keyed on the
# BASE id with any of these stripped, so lookups and resolve_model_id both normalize
# against this set. Kept ALIGNED with the generator's GEO_PREFIXES (modelcaps/refresh.py)
# — incl. "in." (India-region CRIS) — so an invocable id with any geo prefix the
# generator can emit strips cleanly to its base for catalog lookup/resolution.
_GEO_PREFIXES = ("us.", "eu.", "global.", "ap.", "apac.", "jp.", "au.", "in.")

# Map an AWS region to the CRIS geo profile(s) that serve it, most-specific first.
# cris_profiles tokens in the catalog are geo-scoped (global, us, eu, apac, jp, au,
# ca, in); resolve_model_id uses this to pick "the region's own profile" after it
# has preferred the account-wide ``global`` profile. A region's prefix determines
# its geo: us-*→us, eu-*→eu, ca-*→ca, ap-*→one of jp/au/in/apac (Tokyo/Osaka→jp,
# Sydney→au, Mumbai/Hyderabad→in, else apac). Order within a list = preference.
_REGION_GEO_PROFILES = {
    "us": ("us",),
    "eu": ("eu",),
    "ca": ("ca", "us"),          # Canada CRIS, else fall back to us
    # ap-* regions: the Japan/Australia/India-specific profiles take precedence over
    # the broad apac profile when the region is in that geo.
    "ap-northeast-1": ("jp", "apac"),   # Tokyo
    "ap-northeast-3": ("jp", "apac"),   # Osaka
    "ap-south-1": ("in", "apac"),       # Mumbai
    "ap-south-2": ("in", "apac"),       # Hyderabad
    "ap-southeast-2": ("au", "apac"),   # Sydney
    "ap-southeast-4": ("au", "apac"),   # Melbourne
    "ap": ("apac",),             # any other ap-* region → broad apac
}


def _region_geo_profiles(region: str) -> tuple:
    """CRIS geo profiles that serve ``region``, most-specific first (excluding the
    account-wide ``global`` profile, which the resolver prefers separately). Derived
    from the region prefix; '' / unknown yields ()."""
    if not isinstance(region, str) or not region:
        return ()
    # Exact ap-* entries (Tokyo/Sydney/Mumbai/...) win over the broad "ap" fallback.
    if region in _REGION_GEO_PROFILES:
        return _REGION_GEO_PROFILES[region]
    prefix = region.split("-", 1)[0]
    if prefix == "ap":
        return _REGION_GEO_PROFILES["ap"]
    return _REGION_GEO_PROFILES.get(prefix, ())

# Catalog file lives NEXT TO this module (hand-copied at ship time).
_CATALOG_PATH = Path(__file__).parent / "model_caps.json"

# Lazy + cached catalog state. _catalog_cache is the parsed {<base_id>: entry} map
# (empty dict when no catalog), and _catalog_loaded guards the one-shot load so a
# missing file is not re-stat'd on every call.
_catalog_cache: dict | None = None
_catalog_loaded: bool = False


def _load_catalog() -> dict:
    """Lazily load and cache the model_caps.json catalog's ``models`` map.

    Returns the ``{<base_model_id>: entry}`` dict, or an empty dict when the catalog
    file is absent or unreadable (so every caller transparently falls back to the
    substring rules). Never raises.
    """
    global _catalog_cache, _catalog_loaded
    if _catalog_loaded:
        return _catalog_cache or {}
    _catalog_loaded = True
    try:
        with _CATALOG_PATH.open() as fh:
            data = json.load(fh)
        models = data.get("models")
        _catalog_cache = models if isinstance(models, dict) else {}
    except (OSError, ValueError):
        _catalog_cache = {}
    return _catalog_cache


def _normalize(model_id) -> str:
    """Lowercase a model id for substring matching; '' for non-str/None (safe default)."""
    return model_id.lower() if isinstance(model_id, str) else ""


def _base_id(model_id) -> str:
    """Strip any leading geo/CRIS prefix (us./eu./global./ap./apac./jp./au.) so an
    invocable id maps to the BASE id the catalog is keyed on. '' for non-str/None."""
    if not isinstance(model_id, str):
        return ""
    for prefix in _GEO_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def _catalog_entry(model_id):
    """Return the catalog entry for a model id (matched on its base id), or None when
    the catalog is absent or does not know this model."""
    catalog = _load_catalog()
    if not catalog:
        return None
    entry = catalog.get(_base_id(model_id))
    return entry if isinstance(entry, dict) else None


def supports_temperature(model_id: str) -> bool:
    """Return whether the model accepts a ``temperature`` inference param.

    Catalog-primary: reads ``capabilities.supports_temperature`` for the base id when
    the catalog knows it (and the value is a real bool). Otherwise falls back to the
    verified substring rule — False for the 5.x generation (Sonnet 5.x and Haiku 5.x
    deprecate temperature); True for 4.6 and older, and for unknown/None ids.
    """
    entry = _catalog_entry(model_id)
    if entry is not None:
        val = entry.get("capabilities", {}).get("supports_temperature")
        if isinstance(val, bool):
            return val
    mid = _normalize(model_id)
    # 5.x generation deprecated temperature; these substrings never match 4.x ids.
    if "sonnet-5" in mid or "haiku-5" in mid:
        return False
    return True


def supports_prompt_caching(model_id: str) -> bool:
    """Return whether the model supports Converse PROMPT CACHING (so a caller can
    cache a big, static system prompt + tool specs and pay the cheap cache-read rate
    on repeat turns instead of re-billing the full prompt every turn).

    Catalog-primary: reads ``capabilities.prompt_caching`` for the base id. Falls back
    to False (the safe default — don't attach cache directives a model might reject)
    when the catalog is absent or doesn't know the model.
    """
    entry = _catalog_entry(model_id)
    if entry is not None:
        return bool(entry.get("capabilities", {}).get("prompt_caching"))
    return False


def supports_vision(model_id: str) -> bool:
    """Return whether the model accepts IMAGE content blocks (Converse vision) — so a
    caller can gate image attachments to models that can actually read them.

    Catalog-primary: reads ``capabilities.vision``. Falls back to False (safe default:
    don't send an image to a model that would reject it) when the catalog is absent or
    doesn't know the model.
    """
    entry = _catalog_entry(model_id)
    if entry is not None:
        return bool(entry.get("capabilities", {}).get("vision"))
    return False


def supports_document(model_id: str) -> bool:
    """Return whether the model accepts DOCUMENT content blocks (Converse document —
    PDF/Word/Excel bytes) — so a caller can gate binary-document attachments to models
    that can actually parse them. Plain text/CSV/JSON do NOT need this (they are
    inlined as text), only true binary documents do.

    Catalog-primary: reads ``capabilities.document_support``. Falls back to False
    (safe default) when the catalog is absent or doesn't know the model.
    """
    entry = _catalog_entry(model_id)
    if entry is not None:
        return bool(entry.get("capabilities", {}).get("document_support"))
    return False


def minimal_thinking_field(model_id: str):
    """Return the ``{"thinking": {...}}`` field needed to suppress verbose default
    reasoning for this model, or None when no override is needed/accepted.

    Catalog-primary: reads ``capabilities.thinking_disable_shape`` as-is (it is
    already the exact additionalModelRequestFields payload, or null) when the catalog
    knows this model. Otherwise falls back to the verified substring rule:
        * Haiku 5.x → ``{"thinking": {"type": "disabled"}}``  (between_tools rejected)
        * Sonnet 5.x → ``{"thinking": {"type": "between_tools"}}``  (disabled rejected)
        * Sonnet 4.6 / others → None  (no verbose default reasoning to suppress)
    """
    entry = _catalog_entry(model_id)
    if entry is not None:
        caps = entry.get("capabilities", {})
        if "thinking_disable_shape" in caps:
            return caps["thinking_disable_shape"]  # may legitimately be None
    mid = _normalize(model_id)
    if "haiku-5" in mid:
        return {"thinking": {"type": "disabled"}}
    if "sonnet-5" in mid:
        return {"thinking": {"type": "between_tools"}}
    return None


def build_inference_config(model_id: str, *, max_tokens: int, temperature: float | None = None) -> dict:
    """Build the Converse ``inferenceConfig`` dict for this model.

    Always includes ``maxTokens``. Includes ``temperature`` ONLY when the model
    supports it AND a temperature was provided — so a deprecated field is never sent
    to a 5.x model (which would raise a ValidationException).

    Catalog-primary CLAMP: when the catalog knows this model's ``max_output_tokens``,
    ``maxTokens`` is clamped to ``min(requested, max_output_tokens)`` so a request
    never exceeds the model's enforced output cap. When the cap is unknown/absent the
    requested value is passed through unchanged. Never raises.
    """
    clamped = max_tokens
    entry = _catalog_entry(model_id)
    if entry is not None:
        cap = entry.get("max_output_tokens")
        if isinstance(cap, int) and cap > 0:
            clamped = min(max_tokens, cap)
    config: dict = {"maxTokens": clamped}
    if temperature is not None and supports_temperature(model_id):
        config["temperature"] = temperature
    return config


def build_additional_request_fields(model_id: str, *, disable_thinking: bool):
    """Build the Converse ``additionalModelRequestFields`` dict for this model, or None.

    When ``disable_thinking`` is True and the model has a minimal-thinking field,
    return ``{"thinking": {...}}``; otherwise None (so the caller omits the key
    entirely rather than sending an empty/rejected value).
    """
    if disable_thinking:
        return minimal_thinking_field(model_id)
    return None


def _catalog_models(catalog: dict | None = None) -> dict:
    """Return the ``{<base_id>: entry}`` map to list from.

    Catalog-primary: when ``catalog`` is None the lazily-loaded next-to-this-file
    cache is used (the same single source every other function reads); pass an
    explicit ``models`` map to list a catalog loaded from elsewhere (e.g. the
    canonical ``modelcaps/model_caps.json``). Never raises; returns {} when absent.
    """
    if catalog is not None:
        return catalog if isinstance(catalog, dict) else {}
    return _load_catalog()


def _display_label(base_id: str, entry: dict) -> str:
    """Build a human-friendly dropdown label from a base id (no new data invented).

    Turns e.g. ``anthropic.claude-sonnet-4-6`` into ``Anthropic · Claude Sonnet 4 6``
    by title-casing the hyphen/dot segments after the family prefix. Purely derived
    from the catalog key + family; falls back to the raw base id if anything is odd.
    """
    family = entry.get("family") or ""
    name = base_id
    if family and base_id.startswith(family + "."):
        name = base_id[len(family) + 1:]
    # Drop a trailing version tag like ``-v1:0`` / ``:0`` for readability.
    name = name.split(":", 1)[0]
    words = [w for w in name.replace(".", "-").split("-") if w]
    pretty = " ".join(w.capitalize() for w in words) if words else base_id
    fam_label = family.capitalize() if family else "Other"
    return f"{fam_label} · {pretty}" if pretty else fam_label


def list_models(catalog: dict | None = None) -> dict:
    """List the catalog's Converse-capable models for a UI model-picker dropdown.

    Read-only and catalog-primary: every field is derived from the generated
    ``model_caps.json`` (via the same ``_load_catalog`` single source) — nothing is
    hardcoded. Returns a dict shaped for direct JSON serialization::

        {
          "available": bool,                 # False when no catalog is present
          "count": int,                      # number of models listed
          "providers": [str, ...],           # sorted family names present
          "models": [                        # flat list, one per base model id
            {
              "model_id": str,               # BASE id (geo prefix stripped)
              "provider": str,               # family / provider
              "label": str,                  # display label for the dropdown
              "max_input_tokens": int|null,
              "max_output_tokens": int|null,
              "regions": [str, ...],         # region names the model is available in
              "supports_temperature": bool|null,
              "supports_thinking_disable": bool,   # thinking_disable_shape present & non-null
              "thinking_disable_shape": dict|null, # the exact additionalModelRequestFields payload
              "supports_system_prompt": bool|null,
              "tool_use": bool|null,
              "vision": bool|null,
              "document_support": bool|null,
              "prompt_caching": bool,              # caching support (catalog-derived)
              "pricing": {                         # per-1k rates (or null pre-additive)
                "input_per_1k": float, "output_per_1k": float,
                "cache_read_per_1k": float, "cache_write_per_1k": float,
              } | null,
            }, ...
          ],
          "by_provider": { <family>: [ <model as above>, ... ] },
        }

    When the catalog is absent the result degrades gracefully to
    ``{"available": False, "count": 0, "providers": [], "models": [], "by_provider": {}}``
    — it never raises or signals an error. Models are sorted by (provider, model_id)
    for stable dropdown ordering.
    """
    models = _catalog_models(catalog)
    listed: list[dict] = []
    for base_id in sorted(models):
        entry = models[base_id]
        if not isinstance(entry, dict):
            continue
        caps = entry.get("capabilities") or {}
        shape = caps.get("thinking_disable_shape")
        regions = [
            reg.get("name")
            for reg in entry.get("regions", [])
            if isinstance(reg, dict) and reg.get("name")
        ]
        provider = entry.get("family") or "unknown"
        listed.append({
            "model_id": base_id,
            "provider": provider,
            "label": _display_label(base_id, entry),
            "max_input_tokens": entry.get("max_input_tokens"),
            "max_output_tokens": entry.get("max_output_tokens"),
            "regions": sorted(set(regions)),
            "supports_temperature": caps.get("supports_temperature"),
            "supports_thinking_disable": shape is not None,
            "thinking_disable_shape": shape,
            "supports_system_prompt": caps.get("supports_system_prompt"),
            "tool_use": caps.get("tool_use"),
            "vision": caps.get("vision"),
            "document_support": caps.get("document_support"),
            # Caching support (data-derived in the catalog) + the per-1k pricing
            # block, surfaced so the UI model-picker can show caching + cost per
            # model. prompt_caching defaults to False when the catalog entry lacks
            # the field; pricing passes the catalog's four-key block through as-is
            # (None when the entry has none — pre-additive catalogs).
            "prompt_caching": bool(caps.get("prompt_caching")),
            # Whether the model reasons (the proper 'reasoning' signal, independent of
            # thinking-disable-ability; falls back to the disable-shape heuristic on a
            # pre-regen catalog).
            "reasoning": _caps_reasoning(caps),
            "pricing": entry.get("pricing"),
        })

    listed.sort(key=lambda m: (m["provider"], m["model_id"]))
    by_provider: dict = {}
    for model in listed:
        by_provider.setdefault(model["provider"], []).append(model)
    return {
        "available": bool(listed),
        "count": len(listed),
        "providers": sorted(by_provider),
        "models": listed,
        "by_provider": by_provider,
    }


# ── Agent model picker ──────────────────────────────────────────────────────
# top_models_for_agent() ranks the catalog down to a short, dropdown-ready menu
# for the AI-assistant model selector. The logic is a capability-gated, cost-aware
# score computed ENTIRELY from model_caps.json — no hardcoded model list — so a
# refresh.py run re-derives the menu (names, descriptions, cost ratios) with no
# hand-editing. The public entry point stays a plain function (matching the rest of
# this stateless module); internally it is a short pipeline over the _agent_* step
# helpers below, with every tuning knob gathered in the frozen AgentPickerConfig.
#
# Weights/thresholds were chosen in exploration. In brief:
#   * HARD GATE — eligible only if the model can do the agent's job: orchestrate
#     tools, run under the ~80K-char system prompt, speak Converse, and hold a large
#     context (every turn packs data-source inventory + variables + cells +
#     attachments). The 200K floor cleanly isolates the frontier/long-context tier.
#   * SCORE — reward capability richness + big context, penalize price, all with
#     diminishing returns via log10: Wc*cap_pts + Wx*log10(ctx) - Wp*log10(price).
#   * FAMILY CAP — at most N models per family so the menu keeps a family's tier
#     ladder (Haiku/Sonnet/Opus) without becoming all one vendor.


@dataclass(frozen=True)
class AgentPickerConfig:
    """Immutable tuning knobs for top_models_for_agent(). A frozen dataclass (not a
    stateful class): it groups the constants in one place and documents them, while
    the ranking itself stays a pure function. The module-level DEFAULT instance is
    the single source of defaults; callers override per-call via the function kwargs,
    never by mutating this."""
    context_floor: int = 200_000   # min max_input_tokens to be eligible
    w_cap: float = 2.0             # weight on capability points (0..4)
    w_ctx: float = 1.0             # weight on log10(context window)
    w_cost: float = 1.5            # weight on log10(blended price) — a penalty
    family_cap: int = 3            # max models per family (0 = unlimited)


_AGENT_PICKER = AgentPickerConfig()

# Back-compat module-level aliases (kept so existing references / defaults resolve).
_AGENT_CONTEXT_FLOOR = _AGENT_PICKER.context_floor
_AGENT_W_CAP = _AGENT_PICKER.w_cap
_AGENT_W_CTX = _AGENT_PICKER.w_ctx
_AGENT_W_COST = _AGENT_PICKER.w_cost
_AGENT_FAMILY_CAP = _AGENT_PICKER.family_cap

# Pretty family → brand label for generated display names.
_FAMILY_BRAND = {
    "anthropic": "Claude", "amazon": "Nova", "meta": "Llama", "openai": "GPT",
    "moonshot": "Kimi", "moonshotai": "Kimi", "mistral": "Mistral", "qwen": "Qwen",
    "deepseek": "DeepSeek", "ai21": "Jamba", "nvidia": "Nemotron", "zai": "GLM",
    "xai": "Grok", "minimax": "MiniMax",
}
_NAME_DROP_TOKENS = {"instruct", "chat", "v1", "v2"}
_NAME_UPPER_TOKENS = {"vl", "oss", "glm", "gpt"}


def _agent_blended_price(pricing) -> float:
    """Single price proxy per 1K tokens for ranking: a 1:3 input:output mix (a chat
    agent reads a big prompt, writes a smaller but pricier answer), floored so a
    free/unpriced model never breaks log10."""
    pricing = pricing or {}
    pin = pricing.get("input_per_1k") or 0.0
    pout = pricing.get("output_per_1k") or 0.0
    return max(0.25 * pin + 0.75 * pout, 1e-5)


def _caps_reasoning(caps: dict) -> bool:
    """Whether the model REASONS — the proper ``capabilities.reasoning`` signal
    (does it emit a reasoning block), NOT ``thinking_disable_shape`` (which is only
    about DISABLE-ability and wrongly reads an always-on reasoner like Opus 5.5 as
    non-reasoning). Falls back to the old disable-shape heuristic only when the
    catalog predates the ``reasoning`` field, so behavior degrades gracefully."""
    v = caps.get("reasoning")
    if isinstance(v, bool):
        return v
    return caps.get("thinking_disable_shape") is not None  # pre-regen fallback


def _agent_capability_points(caps: dict) -> int:
    """0..4 — one point per capability that matters to a notebook agent."""
    pts = 0
    if caps.get("vision"):
        pts += 1
    if caps.get("document_support"):
        pts += 1
    if caps.get("prompt_caching"):
        pts += 1
    if _caps_reasoning(caps):
        pts += 1
    return pts


def _agent_capability_words(caps: dict) -> list:
    """User-facing capability tags for the description, in a stable order."""
    words = []
    if caps.get("vision"):
        words.append("vision")
    if caps.get("document_support"):
        words.append("documents")
    if caps.get("prompt_caching"):
        words.append("caching")
    if _caps_reasoning(caps):
        words.append("reasoning")
    return words


def _context_label(tokens) -> str:
    """Compact context-window label: 1000000 -> '1M', 300000 -> '300K'."""
    t = tokens or 0
    if t >= 1_000_000:
        v = t / 1_000_000
        return f"{v:.0f}M" if v == int(v) else f"{v:.1f}M"
    if t >= 1_000:
        return f"{t // 1000}K"
    return str(t)


def _agent_display_name(base_id: str, family: str) -> str:
    """Human label from a raw base id. Catalog-driven and heuristic — never a
    hand-maintained per-model string. Handles the id shapes in model_caps.json:
      anthropic.claude-haiku-5-5          -> Claude Haiku 5.5
      meta.llama4-scout-17b-instruct-v1:0 -> Llama 4 Scout
      qwen.qwen3-vl-235b-a22b             -> Qwen3 VL
      amazon.nova-2-lite-v1:0             -> Nova 2 Lite
    """
    import re
    base = base_id.split(".", 1)[1] if "." in base_id else base_id
    base = re.sub(r"-v\d+(?::\d+)?$", "", base)   # trailing -vN:0 version set
    base = re.sub(r"-\d{8}$", "", base)            # trailing date stamp
    base = re.sub(r":\d+$", "", base)              # stray :0

    brand = _FAMILY_BRAND.get(family, family.title() if family else "")
    parts = [p for p in base.split("-") if p]

    def _is_size(tok):
        return bool(re.fullmatch(r"a?\d+b", tok))  # 17b, 480b, a22b, a3b

    parts = [p for p in parts if p.lower() not in _NAME_DROP_TOKENS and not _is_size(p)]

    if parts:
        first = parts[0].lower()
        brand_token = brand.lower()
        m = (re.fullmatch(rf"{re.escape(family)}(\d+)", first)
             or re.fullmatch(rf"{re.escape(brand_token)}(\d+)", first))
        if first in (brand_token, family):
            parts = parts[1:]
        elif m:
            if family == "meta":
                parts[0] = m.group(1)              # Llama 4 ...
            else:
                parts[0] = f"{brand}{m.group(1)}"  # Qwen3 (brand embedded)
                brand = ""

    def _tok(t):
        return t.upper() if t.lower() in _NAME_UPPER_TOKENS else (t if t[:1].isupper() else t.capitalize())

    merged, i = [], 0
    while i < len(parts):
        if re.fullmatch(r"\d+", parts[i]):
            run, j = [parts[i]], i + 1
            while j < len(parts) and re.fullmatch(r"\d+", parts[j]):
                run.append(parts[j]); j += 1
            merged.append(".".join(run)); i = j
        else:
            merged.append(_tok(parts[i])); i += 1

    tail = " ".join(merged)
    return (f"{brand} {tail}".strip() if brand else tail).strip()


def _available_in_region(entry: dict, region: str | None) -> bool:
    """True when the model is offered in ``region`` (a regions[] entry matches), or
    when ``region`` is None/empty (region gating disabled — rank globally)."""
    if not region:
        return True
    for reg in entry.get("regions", []):
        if isinstance(reg, dict) and reg.get("name") == region:
            return True
    return False


def _agent_tool_use_streaming(caps: dict) -> bool:
    """Whether the model can do tool_use IN STREAMING mode — the capability our chat
    agent actually needs (it runs converse_stream WITH tools). Prefers the explicit
    ``tool_use_streaming`` field; when absent (a pre-regen catalog that predates the
    field), falls back to ``tool_use`` so behavior degrades gracefully rather than
    excluding every model."""
    v = caps.get("tool_use_streaming")
    if isinstance(v, bool):
        return v
    return bool(caps.get("tool_use"))


def _agent_score_eligible(models: dict, region: str | None, cfg: AgentPickerConfig,
                          exclude: set | None = None) -> list:
    """GATE + SCORE. Keep only models that pass the hard gate (available in ``region``
    + converse + STREAMING tool_use + system_prompt + ctx>=cfg.context_floor + not in
    ``exclude``), scoring each by the capability-gated cost-aware formula. Returns a
    list of candidate dicts (unsorted); empty when nothing is eligible."""
    import math
    exclude = exclude or set()
    scored = []
    for base_id in models:
        entry = models[base_id]
        if not isinstance(entry, dict):
            continue
        if base_id in exclude:
            continue  # operator-denied (e.g. a deprecated model) — never offer it
        caps = entry.get("capabilities") or {}
        ctx = entry.get("max_input_tokens") or 0
        if not (_available_in_region(entry, region)
                and caps.get("converse_supported") and _agent_tool_use_streaming(caps)
                and caps.get("supports_system_prompt") and ctx >= cfg.context_floor):
            continue
        blended = _agent_blended_price(entry.get("pricing"))
        score = (cfg.w_cap * _agent_capability_points(caps)
                 + cfg.w_ctx * math.log10(ctx)
                 - cfg.w_cost * math.log10(blended))
        scored.append({"id": base_id, "entry": entry, "caps": caps, "ctx": ctx,
                       "blended": blended, "score": score,
                       "family": entry.get("family") or "unknown"})
    return scored


def _lineage_key(base_id: str, family: str) -> str:
    """Model LINE identity, used to keep only ONE version of a model in the menu
    (e.g. Claude Sonnet 5 and Claude Sonnet 5.5 are the same line → keep one). Derived
    from the generated display name with any TRAILING version number stripped:
      'Claude Sonnet 5.5' / 'Claude Sonnet 5'  -> 'Claude Sonnet'   (collapse)
      'Claude Opus 5' / '5.5' / '4.8'          -> 'Claude Opus'     (collapse)
      'Grok 4.7' / 'Grok 4.6'                  -> 'Grok'            (collapse)
    But a tier/product name that isn't a trailing version stays distinct:
      'Nova Lite' / 'Nova 2 Lite' / 'Nova Pro' -> three keys        (kept)
      'Kimi K3' / 'Kimi K2.5'                  -> two keys          (kept)
    So different VERSIONS of a line collapse; different LINES/tiers do not."""
    import re
    name = _agent_display_name(base_id, family)
    # Drop a trailing pure version number ("5", "5.5", "4.8"); keep names whose last
    # token is a word (Lite, Pro, Luna, VL) or has an embedded brand+gen (Qwen3, K3).
    return re.sub(r"\s+\d+(?:\.\d+)*$", "", name).strip() or name


def _agent_dedupe_siblings(rows: list) -> list:
    """Collapse to ONE row per model line + per near-identical sibling. ``rows`` MUST
    already be score-sorted (newest-first on a tie) so the survivor of each group is
    the highest-scored (newest) one. Two collapses run together:
      1. LINEAGE — one version per model line (Sonnet 5 vs 5.5, Opus 5 vs 5.5 vs 4.8,
         Grok 4.7 vs 4.6 → keep the top-scored of each line). See _lineage_key.
      2. SIGNATURE — same family + capability shape + price (a belt-and-suspenders
         catch for identically-priced siblings the lineage key might miss).
    A row survives only if BOTH its lineage and its signature are unseen. Order preserved."""
    out, seen_lineage, seen_sig = [], set(), set()
    for r in rows:
        lineage = _lineage_key(r["id"], r["family"])
        c = r["caps"]
        sig = (r["family"], bool(c.get("vision")), bool(c.get("document_support")),
               bool(c.get("prompt_caching")), _caps_reasoning(c),
               round(r["blended"], 6))
        if lineage in seen_lineage or sig in seen_sig:
            continue
        seen_lineage.add(lineage)
        seen_sig.add(sig)
        out.append(r)
    return out


def _agent_pick(rows: list, *, n: int, default_base: str | None, cfg: AgentPickerConfig,
                guarantee_premium: bool, premium_cut: float) -> list:
    """Select the final ≤n rows from the deduped, score-sorted ``rows``: pin the
    default first, fill by score under the per-family cap (backfilling if the cap
    leaves us short), then — when ``guarantee_premium`` — ensure a premium-tier
    (>= premium_cut) model is present by swapping out the lowest-scoring non-default
    pick. The default is never evicted."""
    chosen, fam_count = [], {}

    def _take(r):
        chosen.append(r)
        fam_count[r["family"]] = fam_count.get(r["family"], 0) + 1

    default_row = next((r for r in rows if r["id"] == default_base), None)
    if default_row:
        _take(default_row)
    for r in rows:
        if len(chosen) >= n:
            break
        if r in chosen:
            continue
        if cfg.family_cap and fam_count.get(r["family"], 0) >= cfg.family_cap:
            continue
        _take(r)
    if len(chosen) < n:  # family cap left us short — backfill by pure score
        for r in rows:
            if len(chosen) >= n:
                break
            if r not in chosen:
                _take(r)
    chosen = chosen[:n]

    if guarantee_premium and chosen and not any(r["blended"] >= premium_cut for r in chosen):
        chosen_ids = {r["id"] for r in chosen}
        best_premium = next((r for r in rows
                             if r["blended"] >= premium_cut and r["id"] not in chosen_ids), None)
        if best_premium is not None:
            evictable = [r for r in chosen if r["id"] != default_base]
            if evictable:
                drop = min(evictable, key=lambda r: r["score"])
                chosen = [r for r in chosen if r["id"] != drop["id"]] + [best_premium]
    return chosen


def _agent_describe(chosen: list, *, region: str | None, default_base: str | None,
                    default_row: dict | None, premium_cut: float, economy_cut: float) -> list:
    """Turn picked rows into dropdown-ready entries: generated name + description,
    region-resolved invocable id, raw cost multiplier (anchored on the default = 1.0),
    and price tier. All fields derive from the catalog."""
    base_price = (default_row["blended"] if default_row
                  else min(r["blended"] for r in chosen))

    def _cost_label(mult):
        if mult < 1:
            return f"{mult:.1f}x"
        return f"{int(mult)}x" if mult == int(mult) else f"{mult:.1f}x"

    def _tier(blended):
        if blended >= premium_cut:
            return "premium"
        if blended <= economy_cut:
            return "economy"
        return "standard"

    out = []
    for r in chosen:
        words = _agent_capability_words(r["caps"])
        ctx_label = _context_label(r["ctx"])
        mult = r["blended"] / base_price if base_price else 1.0
        pricing = r["entry"].get("pricing") or {}
        # Region-resolved invocable id (global > region geo > direct). When region is
        # None we can't resolve, so fall back to the base id.
        resolved = resolve_model_id(r["id"], region) if region else r["id"]
        out.append({
            "id": r["id"],
            "resolved_id": resolved,
            "display_name": _agent_display_name(r["id"], r["family"]),
            "description": ctx_label + " context" + (" · " + ", ".join(words) if words else ""),
            "provider": r["family"],
            "context_tokens": r["ctx"],
            "capabilities": words,
            "pricing": {"input_per_1k": pricing.get("input_per_1k"),
                        "output_per_1k": pricing.get("output_per_1k")},
            "cost_multiplier": round(mult, 2),
            "cost_label": _cost_label(mult),
            "tier": _tier(r["blended"]),
            "is_default": r["id"] == default_base,
            "score": round(r["score"], 2),
        })
    return out


def top_models_for_agent(n: int = 10, *, region: str | None = None,
                         default_id: str | None = None,
                         family_cap: int = _AGENT_FAMILY_CAP,
                         guarantee_premium: bool = True,
                         context_floor: int = _AGENT_CONTEXT_FLOOR,
                         exclude: set | list | None = None,
                         catalog: dict | None = None) -> dict:
    """Pick the top-``n`` models for the AI-assistant dropdown, straight from the
    catalog — NO hardcoded list — and SCOPED TO ``region``: only models actually
    offered in the configured region are eligible, and each entry carries the
    region-resolved invocable id so the caller can invoke it directly. Returns a
    JSON-serializable dict::

        {
          "available": bool,          # False when no catalog / no region-eligible models
          "count": int,
          "region": str | None,       # the region the list was scoped to
          "default_id": str | None,   # base id of the pinned default (always included)
          "models": [                 # ordered: default first, then by score
            {
              "id": str,              # BASE model id
              "resolved_id": str,     # region-resolved INVOCABLE id (global.>geo>direct)
              "display_name": str,    # generated, e.g. "Claude Haiku 5.5"
              "description": str,     # generated, e.g. "1M context · vision, caching"
              "provider": str,        # family
              "context_tokens": int,
              "capabilities": [str, ...],   # vision / documents / caching / reasoning
              "pricing": {"input_per_1k": float, "output_per_1k": float},
              "cost_multiplier": float,     # raw ratio vs the DEFAULT model (=1.0)
              "cost_label": str,            # e.g. "1x", "0.4x", "20x"
              "tier": "economy"|"standard"|"premium",
              "is_default": bool,
              "score": float,               # ranking score (UI may ignore)
            }, ...
          ],
        }

    ``exclude`` is an operator denylist (base or CRIS-prefixed ids) dropped from the
    gate — e.g. deprecated models that are capability-eligible but must not be offered.
    The hard gate also requires STREAMING tool_use (the chat agent runs converse_stream
    with tools; a model that only does non-streaming tool_use, e.g. Meta Llama 4, is
    excluded).

    A short pipeline over the _agent_* step helpers: gate+score (``_agent_score_eligible``)
    → sort (score desc, newer-id tie-break) → collapse siblings (``_agent_dedupe_siblings``)
    → pick under the family cap + premium guarantee (``_agent_pick``) → describe
    (``_agent_describe``). Knobs come from AgentPickerConfig; the ``family_cap`` and
    ``context_floor`` kwargs override per call (defaults mirror the frozen config).
    Cost multipliers anchor on the default (=1.0). When ``region`` is None the region
    gate is skipped (ranks globally). Degrades to
    ``{"available": False, "count": 0, ...}`` with no catalog or no region-eligible
    models. Never raises.
    """
    cfg = AgentPickerConfig(context_floor=context_floor, w_cap=_AGENT_PICKER.w_cap,
                            w_ctx=_AGENT_PICKER.w_ctx, w_cost=_AGENT_PICKER.w_cost,
                            family_cap=family_cap)
    models = _catalog_models(catalog)
    # Normalize the default to a base id (it may arrive geo-prefixed from config).
    default_base = _base_id(default_id) if default_id else None
    # Normalize the exclude list to BASE ids (operators may pass CRIS-prefixed or
    # full ids, e.g. from a deprecation notice) so the gate matches catalog keys.
    exclude_bases = {_base_id(x) for x in (exclude or []) if x}

    scored = _agent_score_eligible(models, region, cfg, exclude=exclude_bases)
    if not scored:
        return {"available": False, "count": 0, "region": region,
                "default_id": default_base, "models": []}

    # Tie-break: higher score, then lexically-greater id (newer minor wins) so the
    # sibling dedupe keeps the newest of an identical group.
    scored.sort(key=lambda r: (r["score"], r["id"]), reverse=True)

    # Price tiers (catalog-derived percentiles): premium guarantee + tier labels.
    prices = sorted(r["blended"] for r in scored)
    premium_cut = prices[int(0.75 * (len(prices) - 1))]
    economy_cut = prices[int(0.25 * (len(prices) - 1))]

    deduped = _agent_dedupe_siblings(scored)
    default_row = next((r for r in deduped if r["id"] == default_base), None)
    chosen = _agent_pick(deduped, n=n, default_base=default_base, cfg=cfg,
                         guarantee_premium=guarantee_premium, premium_cut=premium_cut)
    out = _agent_describe(chosen, region=region, default_base=default_base,
                          default_row=default_row, premium_cut=premium_cut,
                          economy_cut=economy_cut)

    return {"available": True, "count": len(out), "region": region,
            "default_id": default_base, "models": out}


def fallback_chain(model_id: str | None, region: str | None,
                   default_id: str | None = None, *, n: int = 12,
                   context_floor: int = _AGENT_CONTEXT_FLOOR,
                   exclude: set | list | None = None,
                   catalog: dict | None = None) -> list[str]:
    """Ordered list of BASE model ids to try when ``model_id`` is unusable, computed
    from the SAME capability-gated ranking as the dropdown — NO hardcoded list.

    Policy (agreed): on a HARD model-unavailability (access denied / not available in
    region / invalid model / repeated hard failure — NOT throttling, which boto3
    retries), degrade DOWN by score to the next-best model, and keep going down. The
    configured DEFAULT is always appended last as the guaranteed-good floor, so the
    chain can never run dry.

    SCOPE: the fallback pool is the dropdown's own top-``n`` curated shortlist (the
    models the user could pick), NOT the entire catalog — a bounded, sensible set to
    try rather than dozens of models. The chain is:
        [requested, each lower-ranked dropdown model, ..., default]
    deduped and order-preserving. ``requested`` leads even if it's outside the
    shortlist (we still try the user's pick first). Returns base ids; the caller
    resolves each to an invocable id via resolve_model_id at use time. Region-scoped
    exactly like the dropdown. With no catalog / nothing eligible, returns just the
    requested (or default) id so the caller always has something to try. Never raises.
    """
    req_base = _base_id(model_id) if model_id else None
    default_base = _base_id(default_id) if default_id else None

    # The curated shortlist, in score order (default pinned first inside it).
    top = top_models_for_agent(n=n, region=region, default_id=default_id,
                               context_floor=context_floor, exclude=exclude,
                               catalog=catalog)
    ranked_ids = [m["id"] for m in top.get("models", [])]

    chain: list[str] = []

    def _add(mid):
        if mid and mid not in chain:
            chain.append(mid)

    # 1) The user's pick always leads (even if it's not in the shortlist).
    _add(req_base)

    # 2) Walk DOWN the shortlist from just after the requested model (or from the top
    #    when the pick isn't in the shortlist), lower score = cheaper-ward.
    start = ranked_ids.index(req_base) + 1 if req_base in ranked_ids else 0
    for mid in ranked_ids[start:]:
        _add(mid)

    # 3) The default is the guaranteed floor — always present, appended last if the
    #    downward walk didn't already include it.
    _add(default_base)
    return chain


def resolve_model_id(base_or_full_id: str, region: str) -> str:
    """Resolve a base (or already-invocable) model id to an invocable id for ``region``.

    Catalog-primary:
      * If the input already carries a geo/CRIS prefix (us./eu./global./ap./apac./
        jp./au.) it is already invocable — returned unchanged.
      * Otherwise the input is treated as a base id and the catalog's ``regions``
        array for the model is consulted for ``region``: prefer a CRIS profile (order
        us, then global, then any other) returning ``{profile}.{base}``; else return
        the bare base id when that region entry is ``direct``; else return the input
        unchanged.
      * When the catalog is absent or does not know the model/region, the input is
        returned unchanged.
    """
    if not isinstance(base_or_full_id, str):
        return base_or_full_id
    for prefix in _GEO_PREFIXES:
        if base_or_full_id.startswith(prefix):
            return base_or_full_id

    entry = _catalog_entry(base_or_full_id)
    if entry is None:
        return base_or_full_id
    base = _base_id(base_or_full_id)
    region_entry = None
    for reg in entry.get("regions", []):
        if isinstance(reg, dict) and reg.get("name") == region:
            region_entry = reg
            break
    if region_entry is None:
        return base_or_full_id

    # CRIS profile preference (per product requirement):
    #   1. ``global`` — the account-wide cross-region profile, tried FIRST so a
    #      model picked from the dropdown routes through the broad pool when offered.
    #   2. the REGION's own geo profile(s), most-specific first (us-west-2→us,
    #      ap-northeast-1→jp then apac, ...), derived from the region prefix.
    #   3. any remaining CRIS profile (deterministic, sorted) as a last CRIS resort.
    # Only then, if the region entry exposes no usable CRIS profile, fall back to a
    # ``direct`` invoke (bare base id). Otherwise pass the input through unchanged.
    profiles = region_entry.get("cris_profiles") or []
    if profiles:
        if "global" in profiles:
            return f"global.{base}"
        for geo in _region_geo_profiles(region):
            if geo in profiles:
                return f"{geo}.{base}"
        # No global and no matching geo profile for this region — take the first
        # remaining profile deterministically rather than guessing wrong.
        remaining = sorted(p for p in profiles if p != "global")
        if remaining:
            return f"{remaining[0]}.{base}"
    if region_entry.get("direct"):
        return base
    return base_or_full_id
