#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate the lean Bedrock Converse capability catalog (modelcaps/model_caps.json).

BUILD / REFRESH tool — slow by design. This script makes MANY AWS Bedrock calls
(multi-region discovery + per-model empirical probes) and is NOT meant to run at
request time. Run it when the model fleet or its capabilities change; the
emitted modelcaps/model_caps.json is what the runtime resolver (modelcaps/params.py)
reads. This module uses boto3 + a LiteLLM fetch and is BUILD/DEV-ONLY — it must
NOT be imported by modelcaps/params.py (the pure-stdlib runtime resolver).

What it does:
  1. Multi-region-discovers Bedrock foundation models + CRIS inference profiles.
  2. Filters to Converse-capable, text-output, on-demand/profile models only.
  3. Empirically probes each model for the capabilities we actually need to build
     a correct Converse request: temperature support, the accepted ``thinking``
     disable shape, system-prompt support, tool_use, the server-enforced max
     output-token limit, and — as GROUND TRUTH over control-plane metadata —
     vision (inline image), document (inline document block), and prompt caching
     (system ``cachePoint`` + usage inspection). Each multimodal/caching probe is
     throttle-aware and returns an inconclusive sentinel on ambiguous errors so a
     noisy probe falls back to the derived value rather than clobbering it.
  4. Enriches token limits from LiteLLM's public model map, and emits per-model
     per-1k ``pricing``. ``capabilities.prompt_caching`` is LiteLLM-derived by
     default and REFINED by the empirical cache probe where that probe runs
     conclusively; control-plane vision/document are seeded from the
     lifecycle-agnostic, region-unioned ListFoundationModels modalities and then
     overwritten by the empirical vision/document probes.
  5. Emits a lean JSON catalog keyed on the geo-stripped BASE model id.

This is a focused cousin of the repo-root refresh_catalog.py: it lifts that
file's discovery + throttle-aware probe idiom but deliberately DROPS tiers,
Artificial Analysis, Mantle, Chat-Completions/Responses probing, and
pricing-as-tier. It only emits what the Converse serving path needs.

Usage (prefer running as a module so the modelcaps package resolves):
    # Fast iteration — discovery + LiteLLM only, no probe calls
    .venv/bin/python -m modelcaps.refresh --skip-probes

    # Full refresh (slow, many Bedrock calls)
    .venv/bin/python -m modelcaps.refresh

    # Bounded live probe of a shortlist
    .venv/bin/python -m modelcaps.refresh --only us.anthropic.claude-sonnet-4-6,us.amazon.nova-2-lite-v1:0
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

try:
    import requests
except ImportError:  # pragma: no cover - requests is optional; urllib fallback below
    requests = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
logger = logging.getLogger("refresh_model_caps")

# Quiet the noisy connection-pool + credential chatter during parallel scans.
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
logging.getLogger("botocore.credentials").setLevel(logging.WARNING)

# ── Lifted constants ────────────────────────────────────────────────
GEO_PREFIXES = ("us.", "eu.", "ap.", "apac.", "global.", "au.", "jp.", "in.")

FAMILY_PATTERNS = {
    "anthropic": "anthropic",
    "amazon": "amazon",
    "meta": "meta",
    "mistral": "mistral",
    "deepseek": "deepseek",
    "cohere": "cohere",
    "ai21": "ai21",
    "stability": "stability",
    "google": "google",
    "nvidia": "nvidia",
    "writer": "writer",
    "openai": "openai",
    "minimax": "minimax",
    "zai": "zai",
    "moonshotai": "moonshot",
    "moonshot": "moonshot",
    "qwen": "qwen",
    "twelvelabs": "twelvelabs",
    "xai": "xai",
}

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_CACHE_PATH = Path(__file__).parent / "_litellm_models.json"

DEFAULT_OUTPUT_PATH = Path(__file__).parent / "model_caps.json"

# Regions scanned for model + inference-profile availability (lifted verbatim).
SCAN_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1",
    "ap-south-1", "ap-southeast-1", "ap-southeast-2",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ca-central-1", "sa-east-1",
]

# Preferred regions/prefixes when choosing where/how to probe a model.
_PROBE_REGION_PREFERENCE = ("us-east-1", "us-west-2")
_PROBE_PREFIX_PREFERENCE = ("us", "global")

# A minimal VALID 8x8 red PNG (raw bytes), used by the empirical vision probe.
# It must be a genuinely decodable image, not a degenerate 1x1 — Bedrock's image-
# capable models reject an un-decodable tiny PNG with a "Could not process image"
# content error that is indistinguishable from "accepted", so we ship a real 8x8
# raster whose only possible rejection is a true "model doesn't support images".
_VISION_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP4z8CAFWEXHbQSACj/P8Fu7N9hAAAAAElFTkSuQmCC"
)


# ── Pure helpers (lifted) ───────────────────────────────────────────

def strip_geo_prefix(model_id: str) -> str:
    """Strip a leading CRIS geo prefix (us./eu./global./...) from a model id."""
    for prefix in GEO_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def detect_family(model_id: str) -> str:
    """Map a model id to its provider family (anthropic, amazon, meta, ...)."""
    base = strip_geo_prefix(model_id).lower()
    for pattern, family in FAMILY_PATTERNS.items():
        if base.startswith(pattern):
            return family
    return "unknown"


def _control_plane_vision(raw: dict | None) -> bool:
    """Control-plane vision from RAW (lifecycle-agnostic) modality metadata.

    vision = True if ANY scanned region reports an IMAGE input modality for this
    base model. This reads raw_meta — the UNION of ListFoundationModels
    inputModalities across every region with NO ACTIVE-lifecycle filter — instead
    of meta_map, which only sees ACTIVE models and therefore silently dropped the
    IMAGE flag for LEGACY-but-valid models (e.g. claude-sonnet-4-5, which reports
    [TEXT, IMAGE] in every region but is LEGACY, so discover_models skipped it and
    meta_map defaulted it to vision=False). Aligns with refresh_catalog's rule
    (has_vision = "IMAGE" in input_modalities) but hardened to union across regions
    so a per-region metadata gap never flips vision to False.
    """
    if not raw:
        return False
    return "IMAGE" in (raw.get("input_modalities") or set())


# Name substrings that mark a non-conversational utility model. Mirrors
# discover_models' skip list, PLUS speech/voice markers ("sonic") so
# speech-to-speech models (e.g. amazon.nova-2-5-sonic) are dropped even when their
# RAW metadata reports TEXT in/out alongside SPEECH.
_NON_TEXT_NAME_MARKERS = ("rerank", "safeguard", "embed", "sonic")


def _is_text_llm(base_id: str, raw: dict | None) -> bool:
    """Decide whether a base id is a Converse-eligible TEXT LLM.

    Applies discover_models' modality + name filters to the RAW (non-lifecycle-
    filtered) metadata so regions_map entries that arrived via CRIS profiles or
    direct on-demand ids are held to the SAME bar as discover_models. Unlike
    discover_models this does NOT require ACTIVE lifecycle, so LEGACY-but-valid
    CRIS models (e.g. claude-sonnet-4-5) survive and the live probe decides them.

    Drops:
    - name-substring rerank/safeguard/embed/sonic (utility + speech-to-speech)
    - no TEXT in output modalities (e.g. EMBEDDING/IMAGE/VIDEO-only)
    - no TEXT in input modalities
    - SPEECH or AUDIO input (speech/audio models)
    - VIDEO input without IMAGE (video-understanding models)

    When no RAW metadata exists for the base id (CRIS-only model with no
    ListFoundationModels summary in the scanned regions), it is KEPT and the probe
    decides — the name filter above still applies.
    """
    name_lower = base_id.lower()
    if any(marker in name_lower for marker in _NON_TEXT_NAME_MARKERS):
        return False
    if not raw:
        return True
    inputs = raw.get("input_modalities") or set()
    outputs = raw.get("output_modalities") or set()
    if "TEXT" not in outputs:
        return False
    if "TEXT" not in inputs:
        return False
    if "SPEECH" in inputs or "AUDIO" in inputs:
        return False
    if "VIDEO" in inputs and "IMAGE" not in inputs:
        return False
    return True


# ── Step 1: Discover foundation models (lifted, all filters intact) ──

def discover_models(bedrock_client: Any, region: str) -> list[dict]:
    """List foundation models in a region, filtered to Converse-eligible text LLMs.

    Filters (all lifted from refresh_catalog.py):
    - ACTIVE lifecycle only
    - must output TEXT
    - must support ON_DEMAND or INFERENCE_PROFILE
    - skip context-window variants (e.g. ...-v1:0:128k)
    - skip rerank/safeguard/embedding utility models
    - skip SPEECH/AUDIO-input models and VIDEO-without-IMAGE models
    """
    try:
        response = bedrock_client.list_foundation_models()
    except ClientError as e:
        logger.debug(f"  {region}: list_foundation_models failed - {e}")
        return []

    models = []
    for summary in response.get("modelSummaries", []):
        model_id = summary.get("modelId", "")
        if not model_id:
            continue

        status = summary.get("modelLifecycle", {}).get("status", "")
        if status != "ACTIVE":
            continue

        if "TEXT" not in summary.get("outputModalities", []):
            continue

        inference_types = summary.get("inferenceTypesSupported", [])
        if not any(t in inference_types for t in ("ON_DEMAND", "INFERENCE_PROFILE")):
            continue

        parts = model_id.split(":")
        if len(parts) > 2 and parts[-1].endswith("k"):
            continue

        name_lower = model_id.lower()
        if any(skip in name_lower for skip in ("rerank", "safeguard", "embed")):
            continue

        input_modalities = summary.get("inputModalities", [])
        if "TEXT" not in input_modalities:
            continue
        if "SPEECH" in input_modalities or "AUDIO" in input_modalities:
            continue
        if "VIDEO" in input_modalities and "IMAGE" not in input_modalities:
            continue

        has_vision = "IMAGE" in input_modalities
        models.append({
            "model_id": model_id,
            "model_name": summary.get("modelName", model_id),
            "provider_name": summary.get("providerName", ""),
            "has_vision": has_vision,
            "has_document": has_vision,  # Converse handles documents for vision models
            "streaming": summary.get("responseStreamingSupported", True),
            "inference_types": inference_types,
        })

    return models


# ── Step 2: Discover inference profiles + regional availability ──────

def discover_profiles() -> tuple[dict[str, list[dict]], dict[str, dict], dict[str, dict]]:
    """Scan all regions for CRIS profiles + direct availability + model metadata.

    Runs in parallel across SCAN_REGIONS. Returns a tuple of:
      - regions_map: base_model_id -> [{name, cris_profiles:[...]} and/or
        {name, direct:true}] (exactly the models.json shape).
      - meta_map: base_model_id -> {vision, document, streaming, direct_regions:set}
        unioned from discover_models across the scanned regions.
      - raw_meta: base_model_id -> {input_modalities:set, output_modalities:set}
        unioned from the RAW ListFoundationModels summaries across regions (NOT
        lifecycle-filtered). Used by build_catalog to drop speech/embedding/
        non-text models that enter regions_map via CRIS profiles or direct
        on-demand ids but never pass discover_models' modality filters.
    """
    logger.info("Discovering inference profiles + regional availability...")

    # base_id -> region -> {cris_profiles:set, direct:bool}
    model_regions: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"cris_profiles": set(), "direct": False})
    )
    # base_id -> metadata unioned across regions
    meta_map: dict[str, dict] = {}
    # base_id -> raw modality metadata unioned across regions (no lifecycle filter)
    raw_meta: dict[str, dict] = {}
    all_prefixes: set[str] = set()

    def _scan_region(region: str) -> tuple[str, list, list, list, list, str | None]:
        """Scan one region for profiles, direct-access ids, and model metadata."""
        try:
            client = boto3.client("bedrock", region_name=region)
            profiles = []
            kwargs: dict[str, Any] = {"maxResults": 100, "typeEquals": "SYSTEM_DEFINED"}
            while True:
                resp = client.list_inference_profiles(**kwargs)
                for p in resp.get("inferenceProfileSummaries", []):
                    pid = p.get("inferenceProfileId", "")
                    prefix = pid.split(".")[0] if "." in pid else ""
                    base = ".".join(pid.split(".")[1:]) if "." in pid else pid
                    profiles.append({"prefix": prefix, "base_model": base})
                nt = resp.get("nextToken")
                if not nt:
                    break
                kwargs["nextToken"] = nt

            fm_resp = client.list_foundation_models()
            direct_models = [
                m.get("modelId", "")
                for m in fm_resp.get("modelSummaries", [])
                if "ON_DEMAND" in m.get("inferenceTypesSupported", [])
            ]

            # RAW modality summaries for EVERY model id (any lifecycle). These feed
            # the regions_map text-LLM filter so LEGACY-but-valid CRIS models (e.g.
            # claude-sonnet-4-5) survive while speech/embedding models are dropped.
            raw = [
                {
                    "model_id": m.get("modelId", ""),
                    "input_modalities": m.get("inputModalities", []),
                    "output_modalities": m.get("outputModalities", []),
                }
                for m in fm_resp.get("modelSummaries", [])
                if m.get("modelId", "")
            ]

            discovered = discover_models(client, region)
            return region, profiles, direct_models, discovered, raw, None
        except Exception as e:  # noqa: BLE001 - per-region failures are non-fatal
            return region, [], [], [], [], str(e)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_scan_region, r): r for r in SCAN_REGIONS}
        for future in as_completed(futures):
            region, profiles, direct_models, discovered, raw, err = future.result()
            if err:
                logger.debug(f"  {region}: error - {err[:80]}")
                continue

            for p in profiles:
                base = p["base_model"]
                prefix = p["prefix"]
                all_prefixes.add(prefix)
                model_regions[base][region]["cris_profiles"].add(prefix)

            for model_id in direct_models:
                model_regions[model_id][region]["direct"] = True

            for m in discovered:
                base = strip_geo_prefix(m["model_id"])
                entry = meta_map.setdefault(
                    base, {"vision": False, "document": False, "streaming": False}
                )
                entry["vision"] = entry["vision"] or m["has_vision"]
                entry["document"] = entry["document"] or m["has_document"]
                entry["streaming"] = entry["streaming"] or m["streaming"]

            for m in raw:
                base = strip_geo_prefix(m["model_id"])
                rentry = raw_meta.setdefault(
                    base, {"input_modalities": set(), "output_modalities": set()}
                )
                rentry["input_modalities"].update(m["input_modalities"])
                rentry["output_modalities"].update(m["output_modalities"])

            logger.debug(f"  {region}: {len(profiles)} profiles, {len(direct_models)} direct")

    # Convert to the models.json region shape, keyed on BASE id.
    regions_map: dict[str, list[dict]] = {}
    for model_id, regions_data in model_regions.items():
        base = strip_geo_prefix(model_id)
        entries = []
        for region in sorted(regions_data.keys()):
            rdata = regions_data[region]
            entry: dict[str, Any] = {"name": region}
            if rdata["cris_profiles"]:
                entry["cris_profiles"] = sorted(rdata["cris_profiles"])
            if rdata["direct"]:
                entry["direct"] = True
            entries.append(entry)
        # Merge in case both a base-id and a profile keyed the same base.
        regions_map.setdefault(base, [])
        regions_map[base] = _merge_region_entries(regions_map[base], entries)

    logger.info(
        f"  Scanned {len(SCAN_REGIONS)} regions, {len(regions_map)} base models, "
        f"CRIS prefixes: {sorted(all_prefixes)}"
    )
    return regions_map, meta_map, raw_meta


def _merge_region_entries(a: list[dict], b: list[dict]) -> list[dict]:
    """Union two per-region availability arrays by region name."""
    by_name: dict[str, dict] = {}
    for entry in [*a, *b]:
        name = entry["name"]
        merged = by_name.setdefault(name, {"name": name})
        if "cris_profiles" in entry:
            existing = set(merged.get("cris_profiles", []))
            existing.update(entry["cris_profiles"])
            merged["cris_profiles"] = sorted(existing)
        if entry.get("direct"):
            merged["direct"] = True
    return [by_name[name] for name in sorted(by_name)]


# ── Step 3: LiteLLM token limits + pricing ──────────────────────────

def _download_litellm() -> dict | None:
    """Download the LiteLLM map, preferring requests but falling back to urllib.

    The urllib fallback builds its SSL context from certifi's CA bundle (bundled
    with botocore) so cert verification works on hosts whose system trust store
    isn't wired into Python.
    """
    if requests is not None:
        resp = requests.get(LITELLM_URL, timeout=30)
        resp.raise_for_status()
        return resp.json()
    import ssl
    import urllib.request  # stdlib fallback when requests is unavailable
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with urllib.request.urlopen(LITELLM_URL, timeout=30, context=context) as r:  # noqa: S310 - trusted URL
        return json.loads(r.read().decode("utf-8"))


def fetch_litellm_data() -> dict[str, dict]:
    """Fetch LiteLLM model data (token limits + costs), caching to a temp file.

    Always tries a fresh download and overwrites the local cache; falls back to
    the cache on failure. Returns a dict keyed by the LiteLLM model key.
    """
    logger.info("Fetching token limits from LiteLLM...")
    data: dict | None = None
    try:
        data = _download_litellm()
        LITELLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LITELLM_CACHE_PATH, "w") as f:
            json.dump(data, f)
        logger.info(f"  Downloaded {len(data)} entries, cached to {LITELLM_CACHE_PATH.name}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"  LiteLLM download failed: {e}")
        if LITELLM_CACHE_PATH.exists():
            logger.info(f"  Using local cache: {LITELLM_CACHE_PATH.name}")
            with open(LITELLM_CACHE_PATH) as f:
                data = json.load(f)
        else:
            return {}

    result: dict[str, dict] = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        max_input = entry.get("max_input_tokens")
        max_output = entry.get("max_output_tokens")
        if max_input is None and max_output is None:
            continue
        result[key] = {
            "max_input_tokens": max_input,
            "max_output_tokens": max_output,
            "input_cost_per_token": entry.get("input_cost_per_token"),
            "output_cost_per_token": entry.get("output_cost_per_token"),
            "cache_read_input_token_cost": entry.get("cache_read_input_token_cost"),
            "cache_creation_input_token_cost": entry.get("cache_creation_input_token_cost"),
            # Whether LiteLLM flags this model as supporting Anthropic-style prompt
            # caching. Feeds capabilities.prompt_caching (OR'd with a non-zero
            # cache-read cost) — no new live probe, purely data-derived.
            "supports_prompt_caching": entry.get("supports_prompt_caching"),
        }
    logger.info(f"  Extracted token limits for {len(result)} models")
    return result


def _litellm_normalize(key: str) -> str:
    """Normalize a LiteLLM key / model id to a comparable bare model name.

    Drops any leading provider/route prefix segments (bedrock/, bedrock_mantle/,
    vertex_ai/, us-gov-east-1/, anthropic/, the CRIS geo prefix, the
    ``anthropic.``/``amazon.`` family prefix), the trailing Bedrock version suffix
    (``-v1:0``), and the ``@``/``-`` date stamp, then lowercases. This lets a
    Bedrock id like ``anthropic.claude-3-haiku-20240307-v1:0`` match a non-Bedrock
    LiteLLM key like ``vertex_ai/claude-3-haiku@20240307`` whose only difference is
    the provider route + version-suffix formatting.
    """
    k = key.lower()
    # Strip leading route prefixes (possibly several, e.g. bedrock/us-gov-east-1/).
    while "/" in k:
        k = k.split("/", 1)[1]
    k = strip_geo_prefix(k)
    # Strip the family/provider dotted prefix (anthropic., amazon., meta., ...).
    if "." in k:
        k = k.split(".", 1)[1]
    # Drop the Bedrock version suffix (-v1:0 / -v2:0 / :0) and any date stamp.
    k = re.sub(r"[-:@]v?\d+:\d+$", "", k)
    k = re.sub(r"[-@]\d{6,8}$", "", k)
    k = re.sub(r"-v\d+$", "", k)
    return k


def lookup_litellm(base_id: str, model_id: str, litellm: dict[str, dict]) -> dict | None:
    """Find the best LiteLLM entry for a base id.

    Tries exact keys first (in order): the full invocable model_id, us.<base>,
    bedrock/<model_id>, bedrock/us.<base>, and the bare <base> key (mirroring
    refresh_catalog's build-step lookup). If none hit, falls back to a NORMALIZED
    match: strip provider route, geo/family prefix, version suffix, and date stamp
    from both sides and compare bare model names. This resolves Bedrock Claude 3
    ids (e.g. ``anthropic.claude-3-haiku-20240307-v1:0``) whose pricing LiteLLM
    only carries under a non-Bedrock key (``vertex_ai/claude-3-haiku@20240307``),
    PREFERRING a priced entry so a zero-cost near-match never shadows a real one.
    """
    candidates = [
        model_id,
        f"us.{base_id}",
        f"bedrock/{model_id}",
        f"bedrock/us.{base_id}",
        base_id,
    ]
    for cand in candidates:
        if cand in litellm:
            return litellm[cand]

    # Normalized fallback: match the bare model name against every LiteLLM key,
    # preferring an entry that actually carries a non-zero input cost.
    target = _litellm_normalize(base_id)
    if not target:
        return None
    priced: dict | None = None
    unpriced: dict | None = None
    for key, entry in litellm.items():
        if _litellm_normalize(key) != target:
            continue
        if entry.get("input_cost_per_token"):
            priced = priced or entry
        else:
            unpriced = unpriced or entry
    return priced or unpriced


def _per_1k(per_token: Any) -> float:
    """Convert a LiteLLM per-TOKEN cost to a per-1k rate (``*1000``); 0.0 when absent.

    A missing/None/non-numeric field -> 0.0, so every model carries all four
    pricing keys as real floats.
    """
    if per_token is None:
        return 0.0
    try:
        return float(per_token) * 1000
    except (TypeError, ValueError):
        return 0.0


# Manual pricing overrides live NEXT TO this generator and are merged over the
# LiteLLM-derived prices (see _build_pricing). Hand-curated from AWS model cards /
# the Bedrock pricing page for models LiteLLM gets wrong or doesn't price (preview
# models). Keyed by BASE id; values already in the catalog's per-1k unit.
_PRICE_OVERRIDES_PATH = Path(__file__).parent / "price_overrides.json"
_price_overrides_cache: dict | None = None


def _load_price_overrides() -> dict:
    """Lazily load + cache the {<base_id>: {input_per_1k, output_per_1k, ...}} map
    from price_overrides.json. Returns {} when the file is absent/unreadable (so the
    generator simply falls back to LiteLLM prices). Never raises."""
    global _price_overrides_cache
    if _price_overrides_cache is not None:
        return _price_overrides_cache
    try:
        data = json.loads(_PRICE_OVERRIDES_PATH.read_text())
        ov = data.get("overrides")
        _price_overrides_cache = ov if isinstance(ov, dict) else {}
    except (OSError, ValueError):
        _price_overrides_cache = {}
    return _price_overrides_cache


def _build_pricing(llm: dict | None, base_id: str | None = None) -> tuple[dict[str, float], str]:
    """Build the per-1k ``pricing`` block + its provenance for one model.

    MANUAL OVERRIDE FIRST: if ``base_id`` has an entry in price_overrides.json, use
    those hand-curated AWS-published rates and tag provenance ``"manual"`` — this is
    how preview models (whose LiteLLM price is wrong/missing) and any drifted rate get
    corrected, and the correction survives a refresh. The override supplies the full
    four-key block (input/output required; cache_read/cache_write default 0.0).

    Otherwise fall back to the LiteLLM entry: per-TOKEN costs converted to per-1k by
    ``*1000``, any missing field -> 0, provenance ``"litellm"`` when ANY rate is a
    real non-zero value else ``"unknown"``.
    """
    overrides = _load_price_overrides()
    ov = overrides.get(base_id) if base_id else None
    if isinstance(ov, dict) and (ov.get("input_per_1k") is not None
                                 or ov.get("output_per_1k") is not None):
        pricing = {
            "input_per_1k": float(ov.get("input_per_1k") or 0.0),
            "output_per_1k": float(ov.get("output_per_1k") or 0.0),
            "cache_read_per_1k": float(ov.get("cache_read_per_1k") or 0.0),
            "cache_write_per_1k": float(ov.get("cache_write_per_1k") or 0.0),
        }
        return pricing, "manual"

    llm = llm or {}
    pricing = {
        "input_per_1k": _per_1k(llm.get("input_cost_per_token")),
        "output_per_1k": _per_1k(llm.get("output_cost_per_token")),
        "cache_read_per_1k": _per_1k(llm.get("cache_read_input_token_cost")),
        "cache_write_per_1k": _per_1k(llm.get("cache_creation_input_token_cost")),
    }
    provenance = "litellm" if any(v for v in pricing.values()) else "unknown"
    return pricing, provenance


def _derive_prompt_caching(llm: dict | None) -> bool:
    """Derive the ``capabilities.prompt_caching`` boolean from LiteLLM data.

    Rule (no live probe, purely data-derived):
        bool(litellm.supports_prompt_caching)
        OR (cache_read_input_token_cost not in (None, 0))
    Defaults to False when LiteLLM has no entry for the model.
    """
    if not llm:
        return False
    if bool(llm.get("supports_prompt_caching")):
        return True
    return llm.get("cache_read_input_token_cost") not in (None, 0)


# ── Step 4: Probes (lifted + the two new ones we need) ──────────────

# Bedrock throttling error codes. Throttled probe calls must be treated as
# "accepted/retry" (NOT "unsupported") so a transient throttle never flips a
# capability to False. Mirrors refresh_catalog's throttle-aware ClientError idiom.
_THROTTLE_CODES = ("ThrottlingException", "TooManyRequestsException")


def _is_throttle(e: ClientError) -> bool:
    """True if a ClientError is a Bedrock throttle (by error code OR message)."""
    code = e.response.get("Error", {}).get("Code", "")
    if code in _THROTTLE_CODES:
        return True
    return "throttl" in str(e).lower()


# Bedrock service-side error codes that say "try again later", NOT "this request
# shape is wrong". Like a throttle, they carry no evidence about the capability.
_TRANSIENT_CODES = (
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
    "ModelTimeoutException",
)

# Network-level failures (no response from Bedrock at all) — equally uninformative.
_TRANSIENT_EXCEPTIONS = (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError)


def _is_transient(e: BaseException) -> bool:
    """True if an error is a throttle or a transient Bedrock/network failure.

    Single classification point for the thinking-shape and reasoning probes: these
    errors mean "no answer" (retry, then inconclusive), never "rejected"/False. A
    clear ValidationException is NOT transient.
    """
    if isinstance(e, _TRANSIENT_EXCEPTIONS):
        return True
    if isinstance(e, ClientError):
        if _is_throttle(e):
            return True
        return e.response.get("Error", {}).get("Code", "") in _TRANSIENT_CODES
    return False


class _Inconclusive:
    """Sentinel type: a probe could not decide (throttled / transient errors).

    Deliberately NOT usable as a bool so it can never silently leak into an
    ``a or b`` expression (a plain ``object()`` is truthy). It must be resolved
    (prior value or null) before the catalog is written — see resolve_field and
    _sanitize_catalog.
    """

    def __bool__(self) -> bool:
        raise TypeError("_INCONCLUSIVE must be resolved before use as a bool")

    def __repr__(self) -> str:
        return "<INCONCLUSIVE>"


_INCONCLUSIVE = _Inconclusive()

# Backoff (seconds) between retries of a throttled/transient probe call: the call
# is attempted once plus once per entry. Module-level so tests can zero it.
_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)


def _converse_with_retry(client: Any, retry_delays: tuple[float, ...] | None = None,
                         **kwargs: Any) -> Any:
    """client.converse(**kwargs), retrying throttle/transient errors with backoff.

    Re-raises the last error when retries are exhausted or the error is not
    transient, so the caller classifies it.
    """
    delays = _RETRY_DELAYS if retry_delays is None else retry_delays
    for attempt in range(len(delays) + 1):
        try:
            return client.converse(**kwargs)
        except Exception as e:  # noqa: BLE001 - classified below / by the caller
            if attempt >= len(delays) or not _is_transient(e):
                raise
            delay = delays[attempt]
            logger.debug(f"    {kwargs.get('modelId')}: transient {type(e).__name__}, "
                         f"retry {attempt + 1}/{len(delays)} in {delay}s")
            if delay > 0:
                time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _probe_converse_support(client: Any, model_id: str) -> bool:
    """Test whether a model supports the Converse API (lifted verbatim)."""
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 5},
        )
        return True
    except ClientError as e:
        if _is_throttle(e):
            return True
        msg = str(e).lower()
        if any(phrase in msg for phrase in (
            "does not support converse", "is not supported with converse",
            "not supported for this model", "model identifier is invalid",
            "provided model identifier is invalid",
        )):
            return False
        return True  # Other errors (validation, content) = Converse works
    except Exception:
        return False


def _probe_tool_use(client: Any, model_id: str) -> bool:
    """Test whether a model supports Converse tool_use (lifted; 500->2000 retry)."""
    def _try_probe(max_tokens: int) -> bool | None:
        try:
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [
                    {"text": "What is 2+2? Use the calculator tool."}
                ]}],
                toolConfig={
                    "tools": [{
                        "toolSpec": {
                            "name": "calculator",
                            "description": "Performs math calculations. You MUST use this tool for any math.",
                            "inputSchema": {"json": {
                                "type": "object",
                                "properties": {"expression": {"type": "string"}},
                                "required": ["expression"],
                            }},
                        }
                    }]
                },
                inferenceConfig={"maxTokens": max_tokens},
            )
            content = response.get("output", {}).get("message", {}).get("content", [])
            has_tool_use = any(
                isinstance(block, dict) and "toolUse" in block for block in content
            )
            stop_reason = response.get("stopReason", "")
            if has_tool_use or stop_reason == "tool_use":
                return True
            if stop_reason == "max_tokens":
                return None  # Inconclusive — reasoning model ran out of budget
            return False
        except ClientError as e:
            if _is_throttle(e):
                return True
            code = e.response.get("Error", {}).get("Code", "")
            msg = str(e).lower()
            if "tool" in msg or "not supported" in msg or "does not support" in msg:
                return False
            if "internal" in code.lower():
                return False
            return False
        except Exception:
            return False

    result = _try_probe(500)
    if result is not None:
        return result
    result = _try_probe(2000)
    if result is not None:
        return result
    return True  # accepted toolConfig without error, just ran out of tokens


def _probe_tool_use_streaming(client: Any, model_id: str) -> bool:
    """Test whether a model supports tool_use IN STREAMING MODE (ConverseStream +
    toolConfig). This is a SEPARATE capability from non-streaming tool_use: some
    models (e.g. Meta Llama 4 on Bedrock) accept toolConfig on converse() but reject
    it on converse_stream() with "This model doesn't support tool use in streaming
    mode." Our interactive chat agent is a STREAMING tool-use agent, so this is the
    capability that actually gates dropdown eligibility.

    Sends a minimal streaming tool call and pulls ONE event to trigger server-side
    validation. Throttle-aware (a throttle counts as supported). Returns False only
    on a ValidationException that names streaming tool use (or tool support) as
    unsupported; any other outcome defaults to True to avoid dropping a capable model
    on an ambiguous signal."""
    tool_cfg = {"tools": [{"toolSpec": {
        "name": "calculator",
        "description": "Performs math calculations.",
        "inputSchema": {"json": {"type": "object",
                                 "properties": {"expression": {"type": "string"}}}},
    }}]}
    try:
        resp = client.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            toolConfig=tool_cfg,
            inferenceConfig={"maxTokens": 64},
        )
        # Pull a single event — ValidationException for unsupported streaming tool use
        # is raised on the first stream read, not at the call above.
        for _ in resp.get("stream", []):
            break
        return True
    except ClientError as e:
        if _is_throttle(e):
            return True
        msg = str(e).lower()
        if "tool" in msg and any(
            p in msg for p in ("streaming", "not support", "does not support",
                               "doesn't support", "unsupported")
        ):
            return False
        return True
    except Exception:
        return True


def _probe_max_output_tokens(client: Any, model_id: str) -> int | None:
    """Probe the server-enforced max output-token limit (lifted verbatim).

    Sends maxTokens=9999999; a ValidationException reveals the real cap via the
    "exceeds the model limit of {N}" pattern. Returns the int limit, or None if
    the model accepted the value / was throttled / gave no limit.
    """
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 9999999},
        )
        return None  # Accepted — no server-side cap revealed
    except ClientError as e:
        if _is_throttle(e):
            return None  # throttled — no cap revealed, treat as accepted
        msg = str(e)
        match = re.search(r"model limit of (\d+)", msg)
        if match:
            return int(match.group(1))
        return None
    except Exception:
        return None


def _probe_supports_temperature(client: Any, model_id: str) -> bool:
    """Test whether a model accepts the ``temperature`` inference param.

    Throttle-aware: a throttled call counts as accepted. We ONLY return False
    when a ValidationException clearly names temperature as deprecated/
    unsupported; any other error defaults to True (send it) to avoid dropping a
    valid param on an ambiguous signal.
    """
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 16, "temperature": 0.0},
        )
        return True
    except ClientError as e:
        if _is_throttle(e):
            return True
        msg = str(e).lower()
        # Match the ways Bedrock signals temperature is not accepted. Claude 5.x says
        # "temperature ... deprecated"; OpenAI-on-Bedrock says "This model doesn't
        # support the temperature field" — so BOTH the "not supported" and the
        # apostrophe-s "doesn't support"/"does not support" forms must be covered. A
        # missing "doesn't support" here is exactly what let OpenAI models through as
        # a false-positive supports_temperature=True.
        if "temperature" in msg and any(
            phrase in msg for phrase in (
                "deprecat", "not supported", "unsupported",
                "doesn't support", "does not support", "not support",
            )
        ):
            return False
        return True
    except Exception:
        return True


def _probe_thinking_disable_shape(client: Any, model_id: str,
                                  retry_delays: tuple[float, ...] | None = None):
    """Find the ``thinking`` disable shape Converse accepts for this model.

    Tries candidates IN ORDER and returns the FIRST {'thinking': <shape>} that
    Converse accepts (clean response). A ValidationException (or other
    non-transient error) means that shape is rejected; returns None if both are
    rejected. A throttle/transient error is retried (see _converse_with_retry);
    if a candidate is STILL undecided the probe returns _INCONCLUSIVE at once —
    the answer depends on candidate order, so a later candidate's acceptance
    can't be trusted while an earlier one is unknown.
    """
    candidates = [{"type": "disabled"}, {"type": "between_tools"}]
    for shape in candidates:
        try:
            _converse_with_retry(
                client, retry_delays,
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                additionalModelRequestFields={"thinking": shape},
                inferenceConfig={"maxTokens": 64},
            )
            return {"thinking": shape}
        except Exception as e:  # noqa: BLE001
            if _is_transient(e):
                return _INCONCLUSIVE
            # ValidationException (or anything else) => this shape rejected; try next.
            continue
    return None


def _probe_reasoning(client: Any, model_id: str,
                     retry_delays: tuple[float, ...] | None = None):
    """Test whether a model REASONS at all — emits a reasoning/thinking content block
    — INDEPENDENT of whether that reasoning can be disabled.

    This is a DIFFERENT capability from ``thinking_disable_shape``: a model that
    reasons always-on (and rejects every disable shape, so its disable_shape is null)
    STILL reasons. Using disable_shape as the reasoning signal wrongly scored such
    models (e.g. Claude Opus 5.5) as non-reasoning; this probe fixes that by looking
    for the signal directly.

    Ground truth: Converse returns a model's reasoning as a distinct
    ``reasoningContent`` content block in the output message when the model reasons.
    We send a prompt that invites a little reasoning (a small maxTokens budget is
    enough to elicit the block's presence) and return True if ANY output content
    block is a reasoningContent. Throttle/transient errors are retried and, if
    still failing, return _INCONCLUSIVE (never a definite True/False). Any other
    error / no reasoning block -> False.
    """
    try:
        resp = _converse_with_retry(
            client, retry_delays,
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "What is 17 + 26? Think step by step."}]}],
            inferenceConfig={"maxTokens": 512},
        )
        content = resp.get("output", {}).get("message", {}).get("content", []) or []
        return any(isinstance(b, dict) and "reasoningContent" in b for b in content)
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            return _INCONCLUSIVE
        return False


def _probe_supports_system_prompt(client: Any, model_id: str) -> bool:
    """Test whether a model accepts a Converse ``system`` prompt.

    Throttle-aware. Returns False ONLY when a ValidationException clearly says
    the system prompt is unsupported; other errors default to True.
    """
    try:
        client.converse(
            modelId=model_id,
            system=[{"text": "You are a helpful assistant."}],
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 16},
        )
        return True
    except ClientError as e:
        if _is_throttle(e):
            return True
        msg = str(e).lower()
        if "system" in msg and any(
            phrase in msg for phrase in ("not supported", "unsupported", "does not support")
        ):
            return False
        return True
    except Exception:
        return True


# Image/vision NOT-supported ValidationException markers. A probe only flips
# vision to False on one of these — any OTHER error (invalid model id, content
# decode, max_tokens, throttle, …) is NOT evidence of missing vision support.
_VISION_UNSUPPORTED_MARKERS = (
    "support the image content block",
    "image content block that you provided",
    "does not support image",
    "doesn't support image",
    "images are not supported",
    "vision is not supported",
    "no support for image",
)

# Document NOT-supported ValidationException markers (same idiom as vision).
_DOCUMENT_UNSUPPORTED_MARKERS = (
    "doesn't support documents",
    "does not support documents",
    "document content block",
    "documents are not supported",
)


def _probe_vision(client: Any, model_id: str) -> bool | None:
    """EMPIRICALLY probe whether a model accepts an inline image (Converse vision).

    Sends a Converse request whose user message carries a text block plus a tiny
    but VALID inline PNG. Ground truth over control-plane metadata:
      * ACCEPTED (200 — any stopReason) -> True (the model processed the image).
      * ValidationException clearly naming image/vision as unsupported -> False.
      * Throttle -> treated as accepted (True), mirroring the throttle-aware idiom
        in the other probes (a transient throttle must never flip a cap to False).
      * ANY other error (invalid model id, image-decode content error, max-token
        rejection, …) -> None (inconclusive) so the caller can fall back to the
        control-plane value instead of trusting a noisy negative.
    """
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [
                {"text": "What color is this image?"},
                {"image": {"format": "png", "source": {"bytes": _VISION_PROBE_PNG}}},
            ]}],
            inferenceConfig={"maxTokens": 64},
        )
        return True
    except ClientError as e:
        if _is_throttle(e):
            return True
        msg = str(e).lower()
        if any(marker in msg for marker in _VISION_UNSUPPORTED_MARKERS):
            return False
        return None  # ambiguous — let the control-plane value decide
    except Exception:
        return None


def _probe_document(client: Any, model_id: str) -> bool | None:
    """EMPIRICALLY probe whether a model accepts a Converse document content block.

    Sends a small inline text document. Same accept/deny/throttle/inconclusive
    contract as _probe_vision. Worth a separate probe (not just mirroring vision):
    some models that REJECT images still ACCEPT documents (e.g. Llama 3.1), so the
    two capabilities genuinely diverge and a document probe yields distinct truth.
    """
    try:
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [
                {"text": "Summarize this document."},
                {"document": {
                    "format": "txt",
                    "name": "probe",
                    "source": {"bytes": b"hello world probe document"},
                }},
            ]}],
            inferenceConfig={"maxTokens": 32},
        )
        return True
    except ClientError as e:
        if _is_throttle(e):
            return True
        msg = str(e).lower()
        if any(marker in msg for marker in _DOCUMENT_UNSUPPORTED_MARKERS):
            return False
        return None
    except Exception:
        return None


def _probe_prompt_caching(client: Any, model_id: str) -> bool | None:
    """EMPIRICALLY probe whether a model supports Converse prompt caching.

    Sends a request with a system ``cachePoint`` block preceded by enough filler
    context to exceed the model's minimum cacheable-token threshold, then inspects
    the response ``usage``:
      * ``cacheWriteInputTokens`` (or ``cacheReadInputTokens``) > 0 -> True
        (the model actually wrote a cache entry — definitive).
      * ``usage`` present with the cache fields all zero/absent -> False
        (the model accepted the request but cached nothing).
      * AccessDenied/Validation clearly saying caching is unsupported -> False.
      * Throttle -> None (inconclusive — no usage revealed; fall back to LiteLLM).
      * ANY other error -> None (inconclusive; fall back to the LiteLLM-derived
        value) so a noisy failure never flips the flag.
    """
    try:
        resp = client.converse(
            modelId=model_id,
            system=[
                {"text": "You are a helpful assistant. " + ("filler context " * 600)},
                {"cachePoint": {"type": "default"}},
            ],
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 16},
        )
        usage = resp.get("usage", {}) or {}
        write = usage.get("cacheWriteInputTokens") or 0
        read = usage.get("cacheReadInputTokens") or 0
        return bool(write or read)
    except ClientError as e:
        if _is_throttle(e):
            return None
        msg = str(e).lower()
        if "cach" in msg and any(
            phrase in msg for phrase in (
                "unsupported", "not support", "did not allow", "does not support",
            )
        ):
            return False
        return None
    except Exception:
        return None


# ── Step 5: Per-model orchestration ─────────────────────────────────

def _pick_probe_target(regions: list[dict]) -> tuple[str | None, str | None]:
    """Choose a (region, prefix-or-None) to probe a base model from.

    Prefers us-east-1 then us-west-2 then any available region. Within the chosen
    region, prefers the 'us' then 'global' CRIS profile, else any profile, else a
    bare/direct invocation. Returns (region, prefix) where prefix is None for
    direct invocation; (None, None) if nothing is available.
    """
    by_name = {r["name"]: r for r in regions}
    ordered = [r for r in _PROBE_REGION_PREFERENCE if r in by_name]
    ordered += [r for r in sorted(by_name) if r not in ordered]

    for region in ordered:
        entry = by_name[region]
        profiles = entry.get("cris_profiles", [])
        if profiles:
            for pref in _PROBE_PREFIX_PREFERENCE:
                if pref in profiles:
                    return region, pref
            return region, sorted(profiles)[0]
        if entry.get("direct"):
            return region, None
    return None, None


def _invocable_id(base_id: str, prefix: str | None) -> str:
    """Build the invocable id: '{prefix}.{base}' for CRIS, else the bare base."""
    return f"{prefix}.{base_id}" if prefix else base_id


def _safe_probe(fn: Any, client: Any, model_id: str):
    """Run a single probe, swallowing any unexpected error into None (inconclusive).

    Keeps one probe's unexpected failure from aborting a model's whole probe run:
    an exception becomes None, which the caller reads as "fall back to the derived
    value" rather than a hard False.
    """
    try:
        return fn(client, model_id)
    except Exception as e:  # noqa: BLE001 - defensive; individual probes also guard
        logger.debug(f"    probe {getattr(fn, '__name__', fn)} on {model_id} errored: {e}")
        return None


# ── Inconclusive-result resolution (pure) ───────────────────────────
# Provenance labels written for thinking_disable_shape / reasoning:
#   "probed"       — decided by this run's probe
#   "prior"        — probe undecided (or disagreed with itself); previous catalog kept
#   "inconclusive" — probe undecided and no previous value for this model -> null

def _valid_field_value(field: str, value: Any) -> bool:
    """Only dict|None (thinking_disable_shape) and bool|None (reasoning) are emitted."""
    if field == "thinking_disable_shape":
        return value is None or isinstance(value, dict)
    if field == "reasoning":
        return value is None or isinstance(value, bool)
    return True


def resolve_field(probed: Any, prior_entry: dict | None, field: str) -> tuple[Any, str]:
    """Resolve one probe result to (value, provenance).

    A conclusive result is used as-is ("probed"). An _INCONCLUSIVE result falls
    back to the previous catalog's capabilities[field] when the prior has this
    model ("prior"), else null ("inconclusive").
    """
    if probed is not _INCONCLUSIVE:
        return probed, "probed"
    prior_caps = (prior_entry or {}).get("capabilities")
    if isinstance(prior_caps, dict) and field in prior_caps \
            and _valid_field_value(field, prior_caps[field]):
        return prior_caps[field], "prior"
    return None, "inconclusive"


def resolve_thinking_and_reasoning(
    shape_raw: Any,
    reasoning_raw: Any,
    prior_entry: dict | None,
    recheck_reasoning: Any = None,
    *,
    base_id: str = "",
    invocable: str = "",
) -> dict[str, Any]:
    """Resolve the thinking-shape + reasoning probe results for one model.

    Returns {thinking_disable_shape, thinking_disable_shape_provenance, reasoning,
    reasoning_provenance, issues}. ``issues`` lists every (model, field) that stayed
    inconclusive or disagreed with the prior, for the end-of-run summary.

    reasoning = (reasoning probe) OR (a disable shape exists), computed from the
    RESOLVED values only, so the sentinel never reaches the ``or``. When reasoning
    resolves to a probed False but the prior says True, ``recheck_reasoning()`` is
    called once: False is recorded only if it agrees; otherwise the prior True is
    kept (provenance "prior") and logged as a disagreement.
    """
    issues: list[dict[str, str]] = []

    def _issue(field: str, kind: str) -> None:
        issues.append({"base_id": base_id, "invocable": invocable, "field": field, "kind": kind})

    shape, shape_prov = resolve_field(shape_raw, prior_entry, "thinking_disable_shape")
    if shape_prov != "probed":
        _issue("thinking_disable_shape", f"inconclusive->{'prior' if shape_prov == 'prior' else 'null'}")

    r_val, r_prov = resolve_field(reasoning_raw, prior_entry, "reasoning")

    if r_val is True:
        reasoning, reasoning_prov = True, r_prov
    elif shape is not None:
        reasoning, reasoning_prov = True, shape_prov
    else:
        # r_val is False (probed or prior) or None (no answer at all).
        reasoning, reasoning_prov = r_val, r_prov
    # Only flag reasoning when the FINAL value wasn't decided by this run (e.g. an
    # inconclusive reasoning probe is moot when a probed disable shape proves it).
    if reasoning_prov != "probed":
        _issue("reasoning", f"inconclusive->{'prior' if reasoning_prov == 'prior' else 'null'}")

    prior_caps = (prior_entry or {}).get("capabilities") or {}
    if reasoning is False and prior_caps.get("reasoning") is True:
        second = recheck_reasoning() if recheck_reasoning is not None else _INCONCLUSIVE
        if second is False:
            _issue("reasoning", "changed-vs-prior (True->False, confirmed by re-check)")
        else:
            reasoning, reasoning_prov = True, "prior"
            _issue("reasoning", f"disagreement->prior (probe False, re-check {second!r}, prior True)")

    return {
        "thinking_disable_shape": shape,
        "thinking_disable_shape_provenance": shape_prov,
        "reasoning": reasoning,
        "reasoning_provenance": reasoning_prov,
        "issues": issues,
    }


# Issue kinds worth re-probing (a confirmed change was already decided twice).
def _needs_reprobe(issue: dict[str, str]) -> bool:
    return not issue["kind"].startswith("changed-vs-prior")


def summarize_issues(issues: list[dict[str, str]]) -> str:
    """Human-readable end-of-run summary + a ready-to-copy --only re-probe command.

    The command writes to a scratch --output so a scoped re-probe never overwrites
    the full catalog (--only emits only the probed models).
    """
    if not issues:
        return "Inconclusive/disagreement summary: none — all thinking/reasoning probes conclusive."
    lines = [f"Inconclusive/disagreement summary ({len(issues)} model/field pairs):"]
    for i in sorted(issues, key=lambda x: (x["base_id"], x["field"])):
        lines.append(f"  {i['base_id']} [{i['field']}]: {i['kind']}")
    ids = sorted({i["invocable"] or i["base_id"] for i in issues if _needs_reprobe(i)})
    if ids:
        lines.append("Re-probe just these with:")
        lines.append(f"  .venv/bin/python -m modelcaps.refresh --only {','.join(ids)} "
                     f"--output /tmp/modelcaps_reprobe.json")
    return "\n".join(lines)


def probe_model(base_id: str, regions: list[dict],
                prior_entry: dict | None = None) -> dict[str, Any] | None:
    """Run the needed probes ONCE for a base model in a chosen region.

    Returns the capability dict, or None if the model is NOT Converse-capable
    (so the caller drops it from the catalog). Small sleeps throttle-cushion the
    live calls — kept INSIDE the probe (as refresh_catalog does) so they still
    pace Bedrock when probes run concurrently. The whole body is wrapped in a
    try/except so one model's unexpected failure never aborts a parallel run.
    """
    region, prefix = _pick_probe_target(regions)
    if region is None:
        logger.debug(f"  {base_id}: no probe region available, skipping")
        return None

    invocable = _invocable_id(base_id, prefix)
    logger.info(f"  probing {invocable} in {region}...")

    try:
        client = boto3.client("bedrock-runtime", region_name=region)

        if not _probe_converse_support(client, invocable):
            logger.info(f"    {base_id}: not Converse-capable, dropping")
            return None
        time.sleep(0.3)

        supports_temperature = _probe_supports_temperature(client, invocable)
        time.sleep(0.3)
        shape_raw = _probe_thinking_disable_shape(client, invocable)
        time.sleep(0.3)
        # Whether the model REASONS at all. Two independent positive signals, OR'd —
        # because models differ in whether reasoning is ON by default:
        #   (a) emits a reasoningContent block on a plain call (reasons BY DEFAULT —
        #       e.g. OpenAI GPT-5.6/6, Grok, Opus 5.x), caught by _probe_reasoning; OR
        #   (b) exposes a thinking DISABLE shape (has a thinking mechanism but it's
        #       OFF by default, so a plain call emits nothing — e.g. Claude 5.x,
        #       Kimi, GLM), caught by thinking_disable_shape.
        # Using (a) alone false-negatives every off-by-default reasoner (most of
        # Claude); using (b) alone false-negatives an always-on reasoner with no
        # disable shape (Opus 5.5). The union is correct for both.
        # Either probe may be _INCONCLUSIVE (throttle/transient after retries); the
        # union is computed from RESOLVED values (prior or null), never the sentinel.
        reasoning_raw = _probe_reasoning(client, invocable)
        time.sleep(0.3)
        resolved = resolve_thinking_and_reasoning(
            shape_raw, reasoning_raw, prior_entry,
            recheck_reasoning=lambda: _probe_reasoning(client, invocable),
            base_id=base_id, invocable=invocable,
        )
        supports_system_prompt = _probe_supports_system_prompt(client, invocable)
        time.sleep(0.3)
        tool_use = _probe_tool_use(client, invocable)
        time.sleep(0.3)
        # Streaming tool use is a DISTINCT capability (some models accept toolConfig
        # on converse() but reject it on converse_stream()). Only probe it when the
        # model has tool_use at all; otherwise it's trivially False.
        tool_use_streaming = (_probe_tool_use_streaming(client, invocable)
                              if tool_use else False)
        time.sleep(0.3)
        max_output_probed = _probe_max_output_tokens(client, invocable)
        time.sleep(0.3)
        # Empirical multimodal + caching probes (ground truth over metadata). Each
        # returns True/False, or None when the result is inconclusive (ambiguous
        # error / throttle) — None means "fall back to the derived value" in the
        # caller, so a noisy probe never clobbers a known-good control-plane/LiteLLM
        # value. Wrapped individually so one probe's failure can't drop the model.
        vision_probed = _safe_probe(_probe_vision, client, invocable)
        time.sleep(0.3)
        document_probed = _safe_probe(_probe_document, client, invocable)
        time.sleep(0.3)
        prompt_caching_probed = _safe_probe(_probe_prompt_caching, client, invocable)
        time.sleep(0.3)
    except Exception as e:  # noqa: BLE001 - one model's failure must not abort the run
        logger.warning(f"    {base_id}: probe failed ({type(e).__name__}: {str(e)[:120]}), dropping")
        return None

    return {
        "converse_supported": True,
        "supports_temperature": supports_temperature,
        "thinking_disable_shape": resolved["thinking_disable_shape"],
        "thinking_disable_shape_provenance": resolved["thinking_disable_shape_provenance"],
        "reasoning": resolved["reasoning"],
        "reasoning_provenance": resolved["reasoning_provenance"],
        "issues": resolved["issues"],
        "supports_system_prompt": supports_system_prompt,
        "tool_use": tool_use,
        "tool_use_streaming": tool_use_streaming,
        "max_output_tokens_probed": max_output_probed,
        "vision_probed": vision_probed,
        "document_probed": document_probed,
        "prompt_caching_probed": prompt_caching_probed,
    }


def _only_set(only: str | None) -> set[str] | None:
    """Parse --only into a set of BASE ids (geo prefixes stripped), or None."""
    if not only:
        return None
    return {strip_geo_prefix(x.strip()) for x in only.split(",") if x.strip()}


# ── Step 6: Catalog assembly + emission ─────────────────────────────

# Parallel probe fan-out width. Matches refresh_catalog's ThreadPoolExecutor
# max_workers=5 — enough to overlap Bedrock round-trips without tripping throttles
# (each probe keeps its own inter-call sleeps).
_PROBE_MAX_WORKERS = 5


def _enrich_base(base_id: str, regions: list[dict], raw: dict | None,
                 llm: dict | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the pre-probe entry + provenance for one base model (pure, no I/O).

    Control-plane vision/document are seeded from ``raw`` (the lifecycle-agnostic,
    region-unioned ListFoundationModels modalities) — NOT the ACTIVE-only meta_map,
    which silently dropped IMAGE for LEGACY models. These seed values are overwritten
    by the empirical probes when those run (see build_catalog); they remain the
    value (provenance 'control-plane') only when probing is skipped.
    """
    provenance: dict[str, str] = {}
    max_input = None
    max_output_llm = None
    if llm:
        max_input = llm.get("max_input_tokens")
        max_output_llm = llm.get("max_output_tokens")
        if max_input is not None:
            provenance["max_input_tokens"] = "litellm"

    # ── Per-model PRICING (per-1k, from LiteLLM per-token costs) ──────────
    # LiteLLM carries per-TOKEN costs; the catalog emits per-1k rates for a
    # UI-friendly cost column. Convert by *1000. Every model carries the full
    # four-key block (missing field -> 0.0). provenance.pricing = "litellm"
    # when ANY real rate was found, "unknown" when all four are 0/absent.
    pricing, pricing_prov = _build_pricing(llm, base_id)
    provenance["pricing"] = pricing_prov

    # ── capabilities.prompt_caching (data-derived, no live probe) ────────
    # Rule: bool(litellm.supports_prompt_caching) OR a non-zero/non-None
    # cache-read cost. Default False when LiteLLM has no entry for the model.
    prompt_caching = _derive_prompt_caching(llm)

    cp_vision = _control_plane_vision(raw)
    caps: dict[str, Any] = {
        "converse_supported": True,
        "supports_temperature": None,
        "thinking_disable_shape": None,
        "reasoning": None,
        "supports_system_prompt": None,
        "tool_use": None,
        "tool_use_streaming": None,
        "vision": cp_vision,
        # Converse serves documents for every vision model, so control-plane
        # document_support mirrors vision (refresh_catalog's rule); the empirical
        # document probe refines this where it runs (some non-vision models still
        # accept documents, e.g. Llama 3.1).
        "document_support": cp_vision,
        "prompt_caching": prompt_caching,
    }
    provenance["vision"] = "control-plane"
    provenance["document_support"] = "control-plane"
    provenance["prompt_caching"] = "litellm"

    max_output = max_output_llm
    if max_output_llm is not None:
        provenance["max_output_tokens"] = "litellm"

    entry: dict[str, Any] = {
        "family": detect_family(base_id),
        "regions": regions,
        "max_input_tokens": max_input,
        "max_output_tokens": max_output,
        "capabilities": caps,
        "pricing": pricing,
        "provenance": provenance,
    }
    return entry, provenance


def build_catalog(*, region: str, skip_probes: bool, only: str | None,
                  prior_models: dict[str, Any] | None = None) -> dict[str, Any]:
    """Discover, probe (unless skipped), enrich, and assemble the catalog dict.

    ``prior_models`` (the previous catalog's ``models`` map, or None) supplies the
    fallback for thinking_disable_shape / reasoning probes that stay inconclusive.

    Probing runs CONCURRENTLY (ThreadPoolExecutor, max_workers=5 — matching
    refresh_catalog) across candidate base models; each probe keeps its own
    inter-call sleeps + try/except so a slow or failing model can't stall or
    abort the run. Probe results are merged back deterministically (the final
    models dict is assembled over SORTED base ids) so output ordering is stable
    regardless of completion order.
    """
    regions_map, meta_map, raw_meta = discover_profiles()
    litellm = fetch_litellm_data()


    only_bases = _only_set(only)
    prior = prior_models or {}

    # ── Discovery-filter tightening (drop speech/embedding/non-text models) ──
    # regions_map carries EVERY CRIS profile + direct on-demand id, which is NOT
    # held to discover_models' modality/name filters (those only feed meta_map).
    # Apply the same bar here via raw ListFoundationModels modalities so sonic /
    # embedding / rerank / image / video / safeguard models never reach the probe.
    all_bases = sorted(regions_map)
    kept_bases = [b for b in all_bases if _is_text_llm(b, raw_meta.get(b))]
    dropped = [b for b in all_bases if b not in set(kept_bases)]
    if dropped:
        logger.info(f"  Discovery filter dropped {len(dropped)} non-text/speech/embedding models:")
        for b in dropped:
            rm = raw_meta.get(b) or {}
            logger.info(
                f"    DROP {b} | in={sorted(rm.get('input_modalities', []))} "
                f"out={sorted(rm.get('output_modalities', []))}"
            )

    # Build pre-probe enrichment for every kept base. Control-plane vision/document
    # are seeded from raw_meta (lifecycle-agnostic, region-unioned) so LEGACY-but-
    # valid models (e.g. claude-sonnet-4-5) keep their IMAGE flag; the empirical
    # probes refine these when probing runs. (meta_map is no longer read here — it
    # was ACTIVE-only and mis-seeded vision for LEGACY models.)
    pre: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for base_id in kept_bases:
        llm = lookup_litellm(base_id, _invocable_id(base_id, "us"), litellm)
        pre[base_id] = _enrich_base(base_id, regions_map[base_id], raw_meta.get(base_id), llm)

    # ── Probe (concurrently) unless skipped ──────────────────────────────
    probe_results: dict[str, dict[str, Any] | None] = {}
    if not skip_probes:
        # The --only filter scopes WHICH kept models get probed.
        to_probe = [
            b for b in kept_bases
            if only_bases is None or b in only_bases
        ]
        logger.info(f"Probing {len(to_probe)} models (parallel, max_workers={_PROBE_MAX_WORKERS})...")
        t_probe = time.time()
        with ThreadPoolExecutor(max_workers=_PROBE_MAX_WORKERS) as executor:
            futures = {
                executor.submit(probe_model, b, regions_map[b], prior.get(b)): b
                for b in to_probe
            }
            done = 0
            for future in as_completed(futures):
                b = futures[future]
                try:
                    probe_results[b] = future.result()
                except Exception as e:  # noqa: BLE001 - defensive; probe_model already guards
                    logger.warning(f"    {b}: probe raised ({type(e).__name__}), dropping")
                    probe_results[b] = None
                done += 1
                if done % 10 == 0:
                    logger.info(f"  Probed {done}/{len(to_probe)} models...")
        logger.info(f"  Parallel probe wall-time: {time.time() - t_probe:.1f}s")

    # ── Assemble final catalog over SORTED keys (deterministic order) ─────
    models: dict[str, Any] = {}
    issues: list[dict[str, str]] = []
    for base_id in kept_bases:
        entry, provenance = pre[base_id]

        if not skip_probes:
            # --only models we skipped probing are not emitted (preserves prior
            # behavior: a non-probed model carries null probe fields -> dropped).
            if only_bases is not None and base_id not in only_bases:
                continue
            probed = probe_results.get(base_id)
            if probed is None:
                continue  # not Converse-capable OR unprobeable -> drop
            caps = entry["capabilities"]
            caps["supports_temperature"] = probed["supports_temperature"]
            caps["thinking_disable_shape"] = probed["thinking_disable_shape"]
            caps["reasoning"] = probed["reasoning"]
            caps["supports_system_prompt"] = probed["supports_system_prompt"]
            caps["tool_use"] = probed["tool_use"]
            caps["tool_use_streaming"] = probed["tool_use_streaming"]
            provenance["supports_temperature"] = "probed"
            # "probed" | "prior" | "inconclusive" (see resolve_thinking_and_reasoning)
            provenance["thinking_disable_shape"] = probed.get(
                "thinking_disable_shape_provenance", "probed")
            provenance["reasoning"] = probed.get("reasoning_provenance", "probed")
            issues.extend(probed.get("issues") or [])
            provenance["supports_system_prompt"] = "probed"
            provenance["tool_use"] = "probed"
            provenance["tool_use_streaming"] = "probed"
            if probed["max_output_tokens_probed"] is not None:
                entry["max_output_tokens"] = probed["max_output_tokens_probed"]
                provenance["max_output_tokens"] = "probed"

            # ── EMPIRICAL vision (ground truth over control-plane metadata) ──
            # The probe wins when it is conclusive (True/False); when inconclusive
            # (None — ambiguous error / throttle) we KEEP the control-plane seed and
            # leave provenance 'control-plane'.
            cp_vision = caps["vision"]
            vp = probed["vision_probed"]
            if vp is not None:
                caps["vision"] = vp
                provenance["vision"] = "probed"
                if vp != cp_vision:
                    logger.info(
                        f"    {base_id}: VISION probe={vp} disagrees with "
                        f"control-plane={cp_vision}"
                    )

            # ── document_support: prefer the empirical document probe; else mirror
            # the (now possibly probed) vision value with provenance 'derived'. ──
            dp = probed["document_probed"]
            if dp is not None:
                caps["document_support"] = dp
                provenance["document_support"] = "probed"
            else:
                caps["document_support"] = caps["vision"]
                provenance["document_support"] = "derived"

            # ── prompt_caching: prefer the empirical cache probe when conclusive;
            # otherwise KEEP the LiteLLM-derived value (provenance stays 'litellm'). ──
            pcp = probed["prompt_caching_probed"]
            if pcp is not None:
                caps["prompt_caching"] = pcp
                provenance["prompt_caching"] = "probed"

        models[base_id] = entry

    logger.info(f"Assembled catalog with {len(models)} Converse-capable models")
    if not skip_probes:
        logger.info(summarize_issues(issues))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "region_scanned": "all",
        "models": models,
    }


def _sanitize_catalog(catalog: dict[str, Any]) -> None:
    """Guarantee the sentinel is never written (in place).

    Any _INCONCLUSIVE anywhere becomes null (logged as an error — it means a
    resolution path was missed), and thinking_disable_shape / reasoning are forced
    to their emitted types (dict|null, bool|null).
    """
    def _walk(node: Any, path: str) -> Any:
        if node is _INCONCLUSIVE:
            logger.error(f"  unresolved inconclusive value at {path}; writing null")
            return None
        if isinstance(node, dict):
            for k in list(node):
                node[k] = _walk(node[k], f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                node[i] = _walk(v, f"{path}[{i}]")
        return node

    _walk(catalog, "catalog")
    for base_id, entry in (catalog.get("models") or {}).items():
        caps = entry.get("capabilities") if isinstance(entry, dict) else None
        if not isinstance(caps, dict):
            continue
        for field in ("thinking_disable_shape", "reasoning"):
            if field in caps and not _valid_field_value(field, caps[field]):
                logger.error(f"  {base_id}: invalid {field}={caps[field]!r}; writing null")
                caps[field] = None


def resolve_prior_path(output: Path, prior: Path | None, no_prior: bool) -> Path | None:
    """Pick the previous catalog to fall back on: --no-prior -> None; --prior PATH;
    else the existing --output file; else the canonical modelcaps/model_caps.json."""
    if no_prior:
        return None
    if prior is not None:
        return prior
    if output.exists():
        return output
    return DEFAULT_OUTPUT_PATH if DEFAULT_OUTPUT_PATH.exists() else None


def load_prior_models(path: Path | None) -> dict[str, Any]:
    """Load the ``models`` map of a previous catalog; {} if absent/unreadable."""
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            models = json.load(f).get("models")
    except (OSError, ValueError, AttributeError) as e:
        logger.warning(f"  prior catalog {path} unreadable ({e}); no prior fallback")
        return {}
    if not isinstance(models, dict):
        return {}
    logger.info(f"Prior catalog: {path} ({len(models)} models) for inconclusive fallback")
    return models


def write_catalog(catalog: dict[str, Any], output: Path) -> None:
    """Write the catalog as pretty JSON with a trailing newline."""
    _sanitize_catalog(catalog)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(catalog, f, indent=2)
        f.write("\n")
    logger.info(f"Wrote {output}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-west-2",
                        help="Region for the sts/client bootstrap (default us-west-2).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH,
                        help="Output path (default modelcaps/model_caps.json).")
    parser.add_argument("--skip-probes", action="store_true",
                        help="Skip live probes; emit discovery + LiteLLM only (probe fields null).")
    parser.add_argument("--only", default=None,
                        help="Comma-separated invocable/base ids to scope live probing to.")
    parser.add_argument("--prior", type=Path, default=None,
                        help="Previous catalog whose thinking_disable_shape/reasoning are kept "
                             "when a probe stays inconclusive (default: existing --output, "
                             "else modelcaps/model_caps.json).")
    parser.add_argument("--no-prior", action="store_true",
                        help="Disable the prior-catalog fallback (inconclusive -> null).")
    args = parser.parse_args(argv)

    # Read the prior BEFORE writing: --output may be the same file.
    prior_models = load_prior_models(resolve_prior_path(args.output, args.prior, args.no_prior))
    catalog = build_catalog(region=args.region, skip_probes=args.skip_probes, only=args.only,
                            prior_models=prior_models)
    write_catalog(catalog, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
