"""DATA LOADING — Bedrock Knowledge Base (semantic retrieval over document chunks)."""

import os
import re

import boto3
import pandas as pd

from ._common import _log_read
from ._common import _REGION, _BEDROCK_KB_ID
from .. import aws_clients as _aws


# =============================================================================
# GENERATIVE AI — Bedrock Knowledge Base (RAG: semantic retrieval + grounded answers)
# =============================================================================
# (Categorized under GENERATIVE AI so read_bedrock_kb/ask_bedrock_kb group with the
# RAG toolkit — embed_texts / semantic_search / invoke_llm / llm_* — rather than the
# tabular readers. Although read_bedrock_kb returns a DataFrame, it's a SEMANTIC
# retrieval over document chunks (model-ranked, like semantic_search), and
# ask_bedrock_kb invokes a foundation model to synthesize a cited answer — both are
# RAG, not data loading. Category is derived from this header by
# completion_catalog._category_map_from_source.)
#
# A Bedrock Knowledge Base is a RETRIEVE-ONLY vector store (not a SQL/tabular
# source). read_bedrock_kb runs the bedrock-agent-runtime Retrieve API and returns
# one DataFrame row per retrieved chunk; ask_bedrock_kb runs RetrieveAndGenerate
# for a synthesized, cited answer. The KB id is injected at VM launch
# (BEDROCK_KB_ID), read at CALL time so a value injected by the /run hook after
# import still applies. Blank BEDROCK_KB_ID ⇒ not configured. Both read AWS_REGION
# from the environment (the KB lives in the deployment's region).
#
# Both support metadata FILTERING via `filter=` (server-side, on the document
# metadata from the KB's .metadata.json sidecars — category/brand/product_id/
# price/rating_avg). read_bedrock_kb also supports client-side `min_score=`.

# Bedrock vectorSearchConfiguration.filter operator keys — if a filter dict's top
# key is one of these, we treat it as a RAW Bedrock filter and pass it through
# unchanged; otherwise we sugar it (see _kb_build_filter).
_KB_FILTER_OPS = {
    "equals", "notEquals", "greaterThan", "greaterThanOrEquals",
    "lessThan", "lessThanOrEquals", "startsWith", "in", "notIn",
    "listContains", "stringContains", "andAll", "orAll",
}


def _kb_build_filter(filter):
    """Translate a user `filter=` into a Bedrock vectorSearchConfiguration.filter.

    HYBRID: accepts two shapes —
      1. RAW Bedrock filter (pass-through): the dict's key is a Bedrock operator,
         e.g. {"equals": {"key": "category", "value": "Beauty"}} or a compound
         {"andAll": [ {...}, {...} ]}. Returned unchanged.
      2. SIMPLE sugar (the common case): a flat {field: value} map, e.g.
         {"category": "Beauty", "brand": "GlowUp"} → each pair becomes an `equals`
         and multiple pairs are combined with `andAll`. A single pair returns the
         bare `equals` (Bedrock rejects a 1-element andAll).

    Returns the Bedrock filter dict, or None when `filter` is falsy."""
    if not filter:
        return None
    if not isinstance(filter, dict):
        raise ValueError("filter must be a dict (simple {field: value} or a raw Bedrock filter)")
    # Pass-through: already a Bedrock filter (top-level key is an operator).
    if len(filter) >= 1 and any(k in _KB_FILTER_OPS for k in filter):
        return filter
    # Sugar: {field: value, ...} → equals clauses, andAll when >1.
    clauses = [{"equals": {"key": str(k), "value": v}} for k, v in filter.items()]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"andAll": clauses}


# A Bedrock KB id is a 10-char uppercase-alphanumeric token (e.g. "AVSDQZX2CR").
# A display NAME (as shown in the Data Sources panel, e.g. "microvm-product-kb")
# has lowercase/hyphens and won't match this — so we can tell them apart cheaply.
_KB_ID_RE = re.compile(r"^[A-Z0-9]{10}$")


def _kb_resolve_id(kb, region):
    """Resolve a KB id OR display NAME to a knowledge-base id.

    The Data Sources panel shows a KB by its NAME, so a user may pass either the
    id (e.g. "AVSDQZX2CR") or the name (e.g. "microvm-product-kb") to the readers.
    - Looks like an id (10-char [A-Z0-9]) → returned as-is (no API call).
    - Otherwise treated as a NAME and resolved via bedrock-agent list_knowledge_bases
      (case-insensitive exact match). Supports multiple KBs in the account.
    Raises a clear error if a name can't be resolved."""
    if not kb:
        return kb
    kb = str(kb).strip()
    if _KB_ID_RE.match(kb):
        return kb
    # Treat as a display name → find its id.
    try:
        agent = _aws.aws_client("bedrock-agent", region)
        matches = []
        paginator = agent.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for s in page.get("knowledgeBaseSummaries", []):
                if (s.get("name") or "").strip().lower() == kb.lower():
                    matches.append(s["knowledgeBaseId"])
        if not matches:
            raise RuntimeError(
                f"No Bedrock Knowledge Base named '{kb}' found in {region}. "
                f"Pass the KB name shown in the Data Sources panel, or its id."
            )
        return matches[0]
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not resolve Bedrock KB '{kb}': {e}")


def _kb_source_uri(location: dict) -> str:
    """Collapse a Bedrock retrieval `location` object to a single source URI/URL.
    `location.type` is one of S3/WEB/CONFLUENCE/SALESFORCE/SHAREPOINT/CUSTOM/
    KENDRA/SQL/ONEDRIVE/GOOGLEDRIVE; each carries its own sub-object with the ref.
    Returns "" when nothing usable is present."""
    if not isinstance(location, dict):
        return ""
    ltype = (location.get("type") or "").upper()
    # Map each location type to the field holding its URI/URL.
    by_type = {
        "S3": ("s3Location", "uri"),
        "WEB": ("webLocation", "url"),
        "CONFLUENCE": ("confluenceLocation", "url"),
        "SALESFORCE": ("salesforceLocation", "url"),
        "SHAREPOINT": ("sharePointLocation", "url"),
        "CUSTOM": ("customDocumentLocation", "id"),
        "KENDRA": ("kendraDocumentLocation", "uri"),
        "SQL": ("sqlLocation", "query"),
        "ONEDRIVE": ("oneDriveLocation", "url"),
        "GOOGLEDRIVE": ("googleDriveLocation", "url"),
    }
    key, field = by_type.get(ltype, (None, None))
    if key and isinstance(location.get(key), dict):
        return location[key].get(field, "") or ""
    # Fallback: scan any *Location sub-dict for a uri/url/id.
    for v in location.values():
        if isinstance(v, dict):
            for f in ("uri", "url", "id"):
                if v.get(f):
                    return v[f]
    return ""


@_log_read("bedrock_kb")
def read_bedrock_kb(query: str, kb_id: str = None, top_k: int = 5,
                    filter: dict = None, min_score: float = None) -> pd.DataFrame:
    """
    Semantic search a Bedrock Knowledge Base and return the retrieved chunks as a
    DataFrame — one row per chunk, with columns:
        text        the chunk's cited text
        score       relevance score (higher = more relevant)
        source_uri  where the chunk came from (e.g. the source S3 URI)
        source_type S3 / WEB / CONFLUENCE / ...
        metadata    the chunk's document metadata (dict)

    The KB is configured at the platform level (BEDROCK_KB_ID); you normally only
    pass `query`. Pass `kb_id` to target a DIFFERENT KB — it accepts either the KB
    id (e.g. "AVSDQZX2CR") OR the KB NAME shown in the Data Sources panel (e.g.
    "microvm-product-kb"); a name is resolved to its id automatically. Useful when
    the account has multiple knowledge bases.

    filter: metadata filter applied SERVER-SIDE against each document's metadata
      (from its .metadata.json sidecar: category / brand / product_id / price /
      rating_avg). Two accepted shapes:
        • Simple  {field: value}  → equality match, e.g. {"category": "Beauty"};
          multiple keys are AND-ed, e.g. {"category": "Beauty", "brand": "GlowUp"}.
        • Raw Bedrock filter (full power), e.g.
          {"greaterThan": {"key": "rating_avg", "value": 4.0}} or
          {"andAll": [ {"equals": {...}}, {"greaterThan": {...}} ]}.

    min_score: keep only rows with score >= min_score (applied CLIENT-SIDE after
      retrieval). Default None = no score filtering. NOTE on this KB's scale: the
      Titan-v2 + euclidean scores here cluster in a narrow band (~0.35–0.40 even
      for strong matches), so pick a threshold from an actual result set rather
      than assuming a 0–1 range.

    Example:
        df = read_bedrock_kb("Which laptops have an OLED display?")
        df = read_bedrock_kb("skincare", filter={"category": "Beauty"}, top_k=10)
        df = read_bedrock_kb("headphones", filter={"greaterThan": {"key": "rating_avg", "value": 4.0}})
        df = read_bedrock_kb("running shoes", min_score=0.38)
    """
    kb_id = kb_id or os.environ.get("BEDROCK_KB_ID", _BEDROCK_KB_ID)
    region = os.environ.get("AWS_REGION", _REGION)
    if not kb_id:
        raise RuntimeError(
            "No Bedrock Knowledge Base configured (BEDROCK_KB_ID is unset). "
            "Re-run local_install.sh to provision it, or pass kb_id=..."
        )
    # Accept a KB id OR the display name shown in the panel; resolve name → id.
    kb_id = _kb_resolve_id(kb_id, region)

    kb_filter = _kb_build_filter(filter)
    client = _aws.aws_client("bedrock-agent-runtime", region)
    rows = []
    next_token = None
    # Page until we have top_k results (each page returns up to numberOfResults).
    while len(rows) < top_k:
        vsc = {"numberOfResults": top_k}
        if kb_filter:
            vsc["filter"] = kb_filter
        kwargs = {
            "knowledgeBaseId": kb_id,
            "retrievalQuery": {"text": query},
            "retrievalConfiguration": {"vectorSearchConfiguration": vsc},
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = client.retrieve(**kwargs)
        for r in resp.get("retrievalResults", []):
            content = r.get("content", {}) or {}
            rows.append({
                "text": content.get("text", ""),
                "score": r.get("score"),
                "source_uri": _kb_source_uri(r.get("location", {})),
                "source_type": (r.get("location", {}) or {}).get("type", ""),
                "metadata": r.get("metadata", {}) or {},
            })
            if len(rows) >= top_k:
                break
        next_token = resp.get("nextToken")
        if not next_token:
            break

    df = pd.DataFrame(rows, columns=["text", "score", "source_uri", "source_type", "metadata"])
    # Present most-relevant first (score may be None if the store didn't return it).
    if not df.empty and df["score"].notna().any():
        df = df.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)
    # Client-side confidence-score filter (Bedrock Retrieve has no server-side
    # min-score). Only rows AT OR ABOVE the threshold survive; None = keep all.
    if min_score is not None and not df.empty:
        df = df[df["score"].notna() & (df["score"] >= float(min_score))].reset_index(drop=True)
    return df


@_log_read("bedrock_kb")
def ask_bedrock_kb(query: str, kb_id: str = None, filter: dict = None,
                   model_id: str = None) -> dict:
    """
    Ask a Bedrock Knowledge Base a question and get a SYNTHESIZED, cited answer
    (the RetrieveAndGenerate API): Bedrock retrieves the relevant chunks AND uses a
    foundation model to compose a natural-language answer grounded in them.

    Returns a dict:
        {
          "answer":    the generated answer text,
          "citations": [ {text, source_uri, source_type}, ... ]  # supporting chunks
        }

    kb_id: like read_bedrock_kb — accepts either the KB id OR the KB NAME shown in
      the Data Sources panel (e.g. "microvm-product-kb"); a name is resolved to its
      id. Defaults to the platform-configured KB (BEDROCK_KB_ID).
    filter: metadata filter applied SERVER-SIDE to the retrieval that feeds the
      answer (same shapes as read_bedrock_kb — simple {field: value} or a raw
      Bedrock filter). Narrows what the model grounds its answer on, e.g.
      filter={"category": "Beauty"}.
    model_id: generation model override. Defaults to BEDROCK_KB_GENERATION_MODEL,
      then BEDROCK_MODEL_ID (error when neither is set). Must be a RAG-supported model with access enabled in this
      account/region (Claude / Nova families); accepts a bare model id, a
      region-prefixed inference-profile id (us./eu./global.), or a full ARN.

    Use this for a ready-to-read answer; use read_bedrock_kb when you want the raw
    retrieved chunks to post-process yourself.

    Example:
        result = ask_bedrock_kb("Which products are best for cold weather?")
        print(result["answer"])
        result = ask_bedrock_kb("Best for sensitive skin?", filter={"category": "Beauty"})
    """
    kb_id = kb_id or os.environ.get("BEDROCK_KB_ID", _BEDROCK_KB_ID)
    region = os.environ.get("AWS_REGION", _REGION)
    # Generation model for RetrieveAndGenerate. This uses the KB-specific
    # BEDROCK_KB_GENERATION_MODEL (NOT the chat BEDROCK_MODEL_ID): RetrieveAndGenerate
    # only runs with its built-in default prompt template for models the service
    # blesses, and refuses models it has no default template for (e.g. Claude Haiku
    # 5.5 → "Custom prompt templates must be provided..."). The KB generation model is
    # pinned to a KB-friendly model (Sonnet 4.6) so the RAG path works with the default
    # template, while the assistant/chat stays on BEDROCK_MODEL_ID (Haiku). Precedence:
    # explicit arg → BEDROCK_KB_GENERATION_MODEL → BEDROCK_MODEL_ID (both set in
    # scripts/config.sh and passed in at launch) → error.
    # RetrieveAndGenerate wants a model ARN; accept a bare id/inference-profile id.
    model_id = (
        model_id
        or os.environ.get("BEDROCK_KB_GENERATION_MODEL")
        or os.environ.get("BEDROCK_MODEL_ID")
    )
    if not model_id:
        raise RuntimeError(
            "No generation model configured: BEDROCK_KB_GENERATION_MODEL and "
            "BEDROCK_MODEL_ID are unset in this MicroVM. Pass model_id=... or set them "
            "in scripts/config.sh and relaunch."
        )
    if not kb_id:
        raise RuntimeError(
            "No Bedrock Knowledge Base configured (BEDROCK_KB_ID is unset). "
            "Re-run local_install.sh to provision it, or pass kb_id=..."
        )
    # Accept a KB id OR the display name shown in the panel; resolve name → id.
    kb_id = _kb_resolve_id(kb_id, region)

    if model_id.startswith("arn:"):
        model_arn = model_id
    else:
        account = _aws.aws_account_id()
        # A region-prefixed id (us./eu./global.) is a cross-region inference profile;
        # anything else is a plain foundation model.
        if model_id.count(".") >= 2 and model_id.split(".", 1)[0] in ("us", "eu", "apac", "global"):
            model_arn = f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}"
        else:
            model_arn = f"arn:aws:bedrock:{region}::foundation-model/{model_id}"

    # Metadata filter feeds the retrieval that grounds the generated answer.
    kb_config = {"knowledgeBaseId": kb_id, "modelArn": model_arn}
    kb_filter = _kb_build_filter(filter)
    if kb_filter:
        kb_config["retrievalConfiguration"] = {
            "vectorSearchConfiguration": {"filter": kb_filter}
        }

    client = _aws.aws_client("bedrock-agent-runtime", region)
    resp = client.retrieve_and_generate(
        input={"text": query},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": kb_config,
        },
    )

    answer = (resp.get("output", {}) or {}).get("text", "")
    citations = []
    for c in resp.get("citations", []):
        for ref in c.get("retrievedReferences", []) or []:
            content = ref.get("content", {}) or {}
            citations.append({
                "text": content.get("text", ""),
                "source_uri": _kb_source_uri(ref.get("location", {})),
                "source_type": (ref.get("location", {}) or {}).get("type", ""),
            })
    return {"answer": answer, "citations": citations}
