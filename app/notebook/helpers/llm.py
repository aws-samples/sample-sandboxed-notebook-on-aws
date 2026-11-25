"""GENERATIVE AI — Bedrock foundation-model inference (text + embeddings).

Pre-loaded LLM helpers available in every cell. All calls go through Amazon
Bedrock using the MicroVM's execution role (bedrock:InvokeModel) — there are no
API keys in the notebook.

Design:
  • invoke_llm(prompt)      — the base primitive: one prompt → one text (or JSON).
  • llm_map(df, template)   — the per-row workhorse: run a prompt over every row of
                              a DataFrame column. The named tasks below are thin
                              specializations of it, so batching + retry/backoff +
                              the system/instructions/temperature/model knobs live
                              in ONE place.
      - llm_classify        — pick a label from a fixed set, per row.
      - llm_extract         — pull structured fields (a schema) out of text, per row.
      - llm_summarize       — summarize one string or a column of strings.
      - llm_sentiment       — sentiment label/score, per row.
  • embed_texts(texts)      — batch embeddings → numpy array.
  • semantic_search(q, corpus) — cosine-rank a corpus against a query (uses embed_texts).
  • count_tokens(text)      — rough token estimate for sizing prompts / cost.
  • list_bedrock_models()   — discover the models available in this account/region.

Text generation uses the Bedrock **Converse** API, which is a single unified
interface across model families (Claude, Nova, …) — so `model=` accepts any of
them without per-provider request wrangling. The default model is BEDROCK_MODEL_ID
(read at CALL time, so a launch-time value applies; set by scripts/config.sh and
passed into the VM at launch). There is no built-in fallback model id.
Embeddings use InvokeModel (Titan v2 by default; Cohere embed also supported).
"""

import os
import re
import json
import time

import boto3
import pandas as pd

from ._common import logger
from ._common import _REGION
from .. import aws_clients as _aws
# Private aliases: llm.py has no __all__ and helpers/__init__.py does `from .llm import *`,
# so public names here would leak into the user-facing helpers surface (cell namespace,
# /helpers autocomplete, Snippets panel, AI prompt helpers block).
from .modelcaps import build_inference_config as _build_inference_config
from .modelcaps import build_additional_request_fields as _build_additional_request_fields


# Defaults, read at CALL time (not import) so a value injected at VM launch applies.
_DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
# Foundation-model / inference-profile id prefixes that mark a cross-region
# inference profile (vs. a plain foundation-model id).
_INFERENCE_PROFILE_PREFIXES = ("us", "eu", "apac", "global")


# =============================================================================
# Shared Bedrock plumbing (private)
# =============================================================================

def _bedrock_runtime(region: str = None):
    """A bedrock-runtime client (converse / invoke_model)."""
    return _aws.aws_client("bedrock-runtime", region or _aws.aws_region())


def _resolve_model_id(model: str = None) -> str:
    """The model id to use: explicit arg → BEDROCK_MODEL_ID env. Raises when
    neither is set (the platform sets BEDROCK_MODEL_ID at VM launch)."""
    resolved = model or os.environ.get("BEDROCK_MODEL_ID")
    if not resolved:
        raise RuntimeError(
            "No Bedrock model configured: BEDROCK_MODEL_ID is unset in this MicroVM. "
            "Pass model=... or set BEDROCK_MODEL_ID in scripts/config.sh and relaunch."
        )
    return resolved


def _converse(prompt: str, *, system: str = None, model: str = None,
              max_tokens: int = 1024, temperature: float = 0.0) -> str:
    """One-shot text generation via the Bedrock Converse API. Returns the reply text.

    Converse is provider-agnostic: the same request shape works for Claude, Nova,
    etc., so callers never build per-model bodies. `model` accepts a bare foundation
    -model id or a cross-region inference-profile id (us./eu./…); both are valid
    modelId values for Converse (no ARN building needed)."""
    client = _bedrock_runtime()
    resolved_id = _resolve_model_id(model)
    # Build inferenceConfig per-model: `temperature` stays a caller-facing knob, but
    # it's a NO-OP that is silently dropped for models that reject it (5.x Claude
    # deprecate it). For 4.6 / Nova / Cohere / unknown ids it is still sent, exactly
    # as before — so this is pass-through-unchanged for everything but 5.x Claude.
    kwargs = {
        "modelId": resolved_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": _build_inference_config(
            resolved_id, max_tokens=int(max_tokens), temperature=float(temperature)),
    }
    # 5.x Claude emit a verbose default reasoning block that eats maxTokens; suppress
    # it with the model's minimal-thinking field. None (4.6 / Nova / unknown) → omit.
    extra_fields = _build_additional_request_fields(resolved_id, disable_thinking=True)
    if extra_fields is not None:
        kwargs["additionalModelRequestFields"] = extra_fields
    if system:
        kwargs["system"] = [{"text": system}]
    resp = client.converse(**kwargs)
    # output.message.content is a list of blocks; concatenate the text blocks.
    blocks = (((resp.get("output") or {}).get("message") or {}).get("content") or [])
    return "".join(b.get("text", "") for b in blocks).strip()


def _extract_json(text: str):
    """Best-effort parse of a JSON object/array out of an LLM reply. Models often
    wrap JSON in prose or ```json fences; strip those and parse the first {...} or
    [...] span. Returns the parsed object, or None if nothing parseable is found."""
    if text is None:
        return None
    s = text.strip()
    # Strip a ```json ... ``` (or bare ```) fence if present.
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", s, re.DOTALL | re.IGNORECASE)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall back to the first balanced-looking {...} or [...] span.
    m = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _as_text_list(texts) -> list:
    """Coerce the `texts` arg (a list, a pandas Series, or a single string) into a
    list of strings so the batch helpers have one shape to work with."""
    if isinstance(texts, str):
        return [texts]
    if isinstance(texts, pd.Series):
        return texts.astype(str).tolist()
    return [("" if t is None else str(t)) for t in texts]


def _retry_call(fn, *, what: str = "bedrock call", attempts: int = 5, base: float = 1.0):
    """Run fn() with exponential backoff on Bedrock throttling/transient errors.
    Bedrock throttles under load (ThrottlingException / TooManyRequestsException);
    a per-row batch would otherwise fail partway. Backoff: base·2^n (1,2,4,8s…),
    bounded by `attempts`. Non-throttle errors are raised immediately."""
    from botocore.exceptions import ClientError
    for i in range(attempts):
        try:
            return fn()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            transient = code in ("ThrottlingException", "TooManyRequestsException",
                                 "ModelTimeoutException", "ServiceUnavailableException")
            if transient and i < attempts - 1:
                time.sleep(base * (2 ** i))
                continue
            raise
    # Unreachable, but keeps type-checkers happy.
    raise RuntimeError(f"{what} failed after {attempts} attempts")


# Per-row LLM helpers make ONE Bedrock call per item. Bedrock calls are I/O-bound
# (the VM waits on the network), so running them across a small thread pool is a
# near-linear speedup — 200 rows drop from ~80s (sequential) to well under the
# cell's execution timeout. Concurrency is kept modest by default so we don't trip
# Bedrock throttling (and _retry_call backs off if we do).
_DEFAULT_LLM_CONCURRENCY = 8


def _map_concurrent(items, fn, *, max_workers: int = _DEFAULT_LLM_CONCURRENCY,
                    on_error=None):
    """Apply fn to each item across a thread pool, returning results IN ORDER.

    Used by the per-row llm_* helpers so a whole column is processed concurrently
    instead of one-at-a-time. Order is preserved (results line up with `items`).

    PER-ROW ERROR ISOLATION: a single row that raises a NON-transient error (e.g. an
    AccessDenied, a model refusal, a malformed prompt) must NOT discard every other
    row's work — a 200-row job shouldn't die on row 173. So each item is run under its
    own try/except: on failure the row's result becomes `on_error(item, exc)` (default:
    None) and a one-line warning is logged, while the rest of the batch completes.
    (Transient Bedrock throttling is already retried with backoff in _retry_call BEFORE
    it ever reaches here, so an exception that lands here is a genuine per-row failure,
    not load.) max_workers is bounded by the item count; a single item runs inline."""
    n = len(items)
    if n == 0:
        return []

    _failures = {"count": 0, "first": None}

    def _safe(it):
        try:
            return fn(it)
        except Exception as e:  # noqa: BLE001 — isolate THIS row; keep the batch alive
            _failures["count"] += 1
            if _failures["first"] is None:
                _failures["first"] = e
            return on_error(it, e) if on_error is not None else None

    if n == 1:
        out = [_safe(items[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(max_workers, n))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            # _safe never raises, so executor.map returns one result per item IN ORDER
            # (failed rows carry the on_error/None sentinel instead of aborting the map).
            out = list(ex.map(_safe, items))

    if _failures["count"]:
        logger.warning(
            "  ⚠️ %d of %d row(s) failed and were set to None (first error: %s: %s). "
            "The rest of the batch completed.",
            _failures["count"], n, type(_failures["first"]).__name__, _failures["first"],
        )
    return out


# =============================================================================
# GENERATIVE AI — inference
# =============================================================================

def invoke_llm(prompt: str, *, system: str = None, model: str = None,
               max_tokens: int = 1024, temperature: float = 0.0,
               json: bool = False):
    """
    Call a Bedrock LLM with a single prompt and return its reply.

    The base inference primitive — everything else in this section builds on it.
    Uses the Bedrock Converse API, so `model` works across families (Claude, Nova,
    …). No API keys: auth is the MicroVM's execution role.

    Args:
        prompt:      the user prompt (the question / instruction).
        system:      optional system prompt (role/behavior, e.g. "You are a terse
                     financial analyst").
        model:       model id or inference-profile id. Default: BEDROCK_MODEL_ID
                     (error when unset). See list_bedrock_models().
        max_tokens:  max tokens to generate (default 1024).
        temperature: 0.0 = deterministic (default), higher = more creative.
        json:        if True, parse the reply as JSON and return the object (dict/
                     list); returns the raw string if the model didn't emit valid
                     JSON. If False (default), returns the reply text.

    Returns: str (json=False) or the parsed object (json=True).

    Example:
        invoke_llm("Summarize AWS Lambda in one sentence.")
        invoke_llm("Extract the company and amount as JSON.", json=True,
                   system="Return only compact JSON.")
    """
    text = _retry_call(
        lambda: _converse(prompt, system=system, model=model,
                          max_tokens=max_tokens, temperature=temperature),
        what="invoke_llm",
    )
    if json:
        parsed = _extract_json(text)
        return parsed if parsed is not None else text
    return text


def llm_map(df: pd.DataFrame, prompt_template: str, *, output_column: str = "llm_output",
            system: str = None, model: str = None, max_tokens: int = 512,
            temperature: float = 0.0, json: bool = False) -> pd.DataFrame:
    """
    Run an LLM prompt over EVERY ROW of a DataFrame — "apply, but with an LLM".

    `prompt_template` is a Python format string that references columns by name in
    braces, filled per row: e.g. "Summarize this review:\\n{review_text}". Returns a
    COPY of df with the model's reply added as `output_column`.

    This is the generic per-row engine; llm_classify / llm_extract / llm_summarize /
    llm_sentiment are specializations of it. Rows are processed CONCURRENTLY (a
    small thread pool) with automatic retry/backoff on Bedrock throttling, so a
    whole column finishes well within the cell timeout.

    Args:
        df:            the DataFrame to map over.
        prompt_template: format string using {column} placeholders.
        output_column: name of the column to write replies into (default "llm_output").
        system/model/max_tokens/temperature: passed through to invoke_llm.
        json:          if True, each reply is parsed as JSON (object stored per cell).

    ⚠️ COST/LATENCY: this makes ONE Bedrock call PER ROW (run concurrently). It still
    costs per-token, so for a very large DataFrame filter/sample first and use
    count_tokens to size it. For a fixed label set prefer llm_classify (a tighter prompt).

    Example:
        df = llm_map(reviews, "Rewrite this politely: {text}", output_column="polite")
        df = llm_map(tickets, "Return JSON {{'team': ...}} for: {body}", json=True)
    """
    out = df.copy()
    # Build every prompt up-front (fast, sequential) so a bad template column fails
    # clearly before we spend any Bedrock calls.
    prompts = []
    for _, row in out.iterrows():
        try:
            prompts.append(prompt_template.format(**row.to_dict()))
        except KeyError as e:
            raise KeyError(
                f"prompt_template references column {e} which isn't in the DataFrame. "
                f"Available columns: {list(out.columns)}"
            )
    results = _map_concurrent(
        prompts,
        lambda p: invoke_llm(p, system=system, model=model,
                             max_tokens=max_tokens, temperature=temperature, json=json),
    )
    out[output_column] = results
    return out


def llm_classify(texts, labels, *, system: str = None, instructions: str = None,
                 model: str = None, temperature: float = 0.0,
                 multi_label: bool = False) -> pd.DataFrame:
    """
    Classify each text into one of `labels` using an LLM. Returns a DataFrame with
    columns: text, label (a list when multi_label=True).

    The user prompt is BUILT FOR YOU from each text + the label set; you tune the
    behavior via system/instructions/temperature/model. temperature defaults to 0.0
    (classification wants determinism).

    Args:
        texts:        a list / pandas Series of strings (or a single string).
        labels:       the allowed labels, e.g. ["billing", "technical", "other"].
        system:       optional system prompt (persona/role).
        instructions: extra task guidance folded into the prompt (e.g. "treat
                      refund questions as 'billing'").
        multi_label:  if True the model may return several applicable labels.
        model/temperature: passed through.

    Example:
        llm_classify(df["ticket"], ["billing","technical","other"])
        llm_classify(reviews, ["positive","negative"], instructions="ignore shipping")
    """
    label_str = ", ".join(str(l) for l in labels)
    items = _as_text_list(texts)
    guide = f"\n{instructions}" if instructions else ""

    def _classify_one(t):
        if multi_label:
            prompt = (
                f"Classify the text into ALL applicable labels from this set: "
                f"[{label_str}].{guide}\nReturn ONLY a JSON array of the chosen labels.\n\n"
                f"Text:\n{t}"
            )
            reply = invoke_llm(prompt, system=system, model=model, temperature=temperature,
                               max_tokens=128, json=True)
            return reply if isinstance(reply, list) else [str(reply)]
        prompt = (
            f"Classify the text into EXACTLY ONE label from this set: "
            f"[{label_str}].{guide}\nReturn ONLY the single chosen label, nothing else.\n\n"
            f"Text:\n{t}"
        )
        return invoke_llm(prompt, system=system, model=model, temperature=temperature,
                          max_tokens=32).strip().strip('"').strip("'")

    labels_out = _map_concurrent(items, _classify_one)
    return pd.DataFrame({"text": items, "label": labels_out}, columns=["text", "label"])


def llm_extract(texts, schema: dict, *, system: str = None, instructions: str = None,
                model: str = None, temperature: float = 0.0) -> pd.DataFrame:
    """
    Extract structured fields from each text into a DataFrame — one row per input,
    one column per field in `schema`.

    `schema` maps field name → a short description of what to pull, e.g.
        {"company": "the company name", "amount_usd": "total amount in USD as a number"}
    The prompt is built from the schema; the model returns JSON with those keys,
    which becomes the row. Missing fields come back as None.

    Args:
        texts:        a list / pandas Series of strings (or a single string).
        schema:       {field: description} of what to extract.
        system/instructions/model/temperature: tuning knobs (temperature 0.0 default).

    Example:
        llm_extract(df["email"], {"sender": "who sent it", "intent": "one of ask/complaint/other"})
    """
    if not isinstance(schema, dict) or not schema:
        raise ValueError("schema must be a non-empty {field: description} dict")
    fields = list(schema.keys())
    schema_desc = "\n".join(f"  - {k}: {v}" for k, v in schema.items())
    guide = f"\n{instructions}" if instructions else ""
    items = _as_text_list(texts)

    def _extract_one(t):
        prompt = (
            f"Extract the following fields from the text and return ONLY a JSON object "
            f"with EXACTLY these keys: {fields}.{guide}\n"
            f"Field descriptions:\n{schema_desc}\n"
            f"If a field is not present, use null.\n\n"
            f"Text:\n{t}"
        )
        obj = invoke_llm(prompt, system=system, model=model, temperature=temperature,
                         max_tokens=512, json=True)
        if not isinstance(obj, dict):
            obj = {}
        return {k: obj.get(k) for k in fields}

    rows = _map_concurrent(items, _extract_one)
    return pd.DataFrame(rows, columns=fields)


def llm_summarize(texts, *, style: str = "concise", max_words: int = None,
                  system: str = None, model: str = None, temperature: float = 0.0):
    """
    Summarize text with an LLM. Pass a single string → get a string back; pass a
    list / pandas Series → get a DataFrame (columns: text, summary).

    Args:
        texts:      one string, or a list / Series of strings.
        style:      "concise" (default), "bullets", or "executive".
        max_words:  optional soft length cap included in the instruction.
        system/model/temperature: tuning knobs.

    Example:
        llm_summarize(long_article)                         # → str
        llm_summarize(df["notes"], style="bullets")         # → DataFrame
    """
    style_hint = {
        "concise": "Write a concise 1-2 sentence summary.",
        "bullets": "Summarize as 3-5 short bullet points.",
        "executive": "Write a brief executive summary (2-3 sentences, decision-oriented).",
    }.get(style, "Write a concise summary.")
    length = f" Keep it under {int(max_words)} words." if max_words else ""
    single = isinstance(texts, str)
    items = _as_text_list(texts)
    summaries = _map_concurrent(
        items,
        lambda t: invoke_llm(f"{style_hint}{length}\n\nText:\n{t}", system=system,
                             model=model, temperature=temperature, max_tokens=512),
    )
    if single:
        return summaries[0]
    return pd.DataFrame({"text": items, "summary": summaries})


def llm_translate(texts, to, *, source: str = None, system: str = None,
                  model: str = None, temperature: float = 0.0):
    """
    Translate text with an LLM. Pass a single string → get a string back; pass a
    list / pandas Series → get a DataFrame (columns: text, translation).

    Args:
        texts:   one string, or a list / Series of strings.
        to:      target language (e.g. "French", "Spanish", "Japanese", "de").
        source:  source language (optional). Default: let the model auto-detect it.
        system/model/temperature: tuning knobs.

    Example:
        llm_translate("Good morning", to="French")             # → "Bonjour"
        llm_translate(df["review"], to="English")              # → DataFrame
        llm_translate(df["note"], to="German", source="English")
    """
    if not to or not str(to).strip():
        raise ValueError("llm_translate: `to` (target language) is required, "
                         "e.g. llm_translate(text, to='Spanish').")
    src = f" from {source}" if source else ""
    instruction = (
        f"Translate the following text{src} into {to}. Preserve meaning, tone, and "
        f"any formatting. Return ONLY the translation — no preamble, no quotes, no "
        f"notes."
    )
    single = isinstance(texts, str)
    items = _as_text_list(texts)
    translations = _map_concurrent(
        items,
        lambda t: invoke_llm(f"{instruction}\n\nText:\n{t}", system=system,
                             model=model, temperature=temperature, max_tokens=1024),
    )
    # Trim stray wrapping quotes/whitespace the model sometimes adds.
    translations = [(x or "").strip().strip('"').strip("'") if isinstance(x, str) else x
                    for x in translations]
    if single:
        return translations[0]
    return pd.DataFrame({"text": items, "translation": translations})


def llm_sentiment(texts, *, scale: str = "label", system: str = None,
                  model: str = None, temperature: float = 0.0) -> pd.DataFrame:
    """
    Sentiment analysis over text. Returns a DataFrame (columns: text, sentiment).

    Args:
        texts:  a list / pandas Series of strings (or a single string).
        scale:  "label" (default) → negative | neutral | positive;
                "score"           → a number from -1.0 (very negative) to 1.0.
        system/model/temperature: tuning knobs.

    Example:
        llm_sentiment(df["review"])                 # labels
        llm_sentiment(df["review"], scale="score")  # -1..1 numeric
    """
    items = _as_text_list(texts)

    def _sentiment_one(t):
        if scale == "score":
            prompt = ("Rate the sentiment of the text as a single number from -1.0 "
                      "(very negative) to 1.0 (very positive). Return ONLY the number.\n\n"
                      f"Text:\n{t}")
            raw = invoke_llm(prompt, system=system, model=model, temperature=temperature, max_tokens=16)
            try:
                return float(re.search(r"-?\d+(?:\.\d+)?", raw).group(0))
            except (AttributeError, ValueError):
                return None
        prompt = ("Classify the sentiment of the text as EXACTLY ONE of: "
                  "negative, neutral, positive. Return ONLY that word.\n\n"
                  f"Text:\n{t}")
        return invoke_llm(prompt, system=system, model=model, temperature=temperature,
                          max_tokens=8).strip().lower().strip('"').strip("'")

    sentiments = _map_concurrent(items, _sentiment_one)
    return pd.DataFrame({"text": items, "sentiment": sentiments}, columns=["text", "sentiment"])


def embed_texts(texts, *, model: str = None, batch_size: int = 96,
                input_type: str = "search_document"):
    """
    Compute embeddings for a list of texts via Bedrock. Returns a numpy array of
    shape (n_texts, embedding_dim).

    Defaults to Amazon Titan Text Embeddings v2. Cohere embed models are also
    supported (their request/response shape is handled). Calls run concurrently
    (a small thread pool) with retry/backoff, so a large corpus embeds quickly.

    Args:
        texts:      a list / pandas Series of strings (or a single string).
        model:      embedding model id (default amazon.titan-embed-text-v2:0).
        batch_size: how many texts to embed per call for models that accept a batch
                    (Cohere). Titan embeds one text per call.
        input_type: Cohere-only retrieval role — 'search_document' (default) for a
                    corpus, 'search_query' for a search query. Cohere embeds the two
                    asymmetrically, so semantic_search embeds its query with
                    'search_query'. Titan ignores this.

    Example:
        vecs = embed_texts(df["description"].tolist())     # (n, 1024) for Titan v2
        vecs.shape

    Note: pairs with semantic_search — embed a corpus once, reuse the vectors.
    """
    import numpy as np
    model_id = model or _DEFAULT_EMBED_MODEL
    items = _as_text_list(texts)
    is_cohere = "cohere" in model_id.lower()
    vectors = []

    if is_cohere:
        # Cohere embeds a BATCH per call: body {"texts": [...], "input_type": ...}.
        # Run the batches CONCURRENTLY (each a separate Bedrock call), preserving order.
        step = max(1, batch_size)
        chunks = [items[i:i + step] for i in range(0, len(items), step)]

        def _embed_chunk(chunk):
            client = _bedrock_runtime()  # per-thread client
            # input_type matters for Cohere's asymmetric retrieval: documents are
            # embedded as 'search_document', a search QUERY as 'search_query'
            # (semantic_search passes the latter for the query). Titan ignores it.
            body = {"texts": chunk, "input_type": input_type}
            resp = _retry_call(
                lambda: client.invoke_model(modelId=model_id, body=json.dumps(body)),
                what="embed_texts(cohere)",
            )
            return json.loads(resp["body"].read()).get("embeddings", [])

        for emb in _map_concurrent(chunks, _embed_chunk):
            vectors.extend(emb)
    else:
        # Titan embeds ONE text per call — run those calls concurrently (in order).
        def _embed_one(t):
            client = _bedrock_runtime()  # per-thread client
            resp = _retry_call(
                lambda: client.invoke_model(modelId=model_id, body=json.dumps({"inputText": t})),
                what="embed_texts(titan)",
            )
            return json.loads(resp["body"].read()).get("embedding", [])

        vectors = _map_concurrent(items, _embed_one)

    return np.array(vectors, dtype="float32")


def semantic_search(query: str, corpus, *, corpus_vectors=None, top_k: int = 5,
                    model: str = None) -> pd.DataFrame:
    """
    Rank a corpus by semantic similarity to a query and return the top-k matches as
    a DataFrame (columns: rank, score, text) sorted by score descending.

    Uses embed_texts under the hood (cosine similarity in numpy). Two ways to call:

      • One-shot (small/ad-hoc): pass just `corpus` — it embeds the whole corpus
        each call.
      • Efficient (reused corpus): embed ONCE with embed_texts, cache the vectors in
        a notebook variable, and pass them as `corpus_vectors=` so only the QUERY is
        embedded on each search:

            vecs = embed_texts(corpus)                       # embed once, reuse
            semantic_search("battery complaints", corpus,
                            corpus_vectors=vecs, top_k=10)   # embeds only the query

    Args:
        query:          the search string.
        corpus:         list / pandas Series of candidate strings.
        corpus_vectors: optional precomputed embeddings for `corpus` (from
                        embed_texts); skips re-embedding the corpus.
        top_k:          how many matches to return.
        model:          embedding model (must match whatever produced corpus_vectors).

    ⚠️ SCALE: this is a brute-force cosine scan in the VM — fine up to ~tens of
    thousands of rows. For a large/persistent corpus, use a Bedrock Knowledge Base
    (read_bedrock_kb) instead.
    """
    import numpy as np
    docs = _as_text_list(corpus)
    if not docs:
        return pd.DataFrame(columns=["rank", "score", "text"])

    doc_vecs = np.asarray(corpus_vectors, dtype="float32") if corpus_vectors is not None \
        else embed_texts(docs, model=model)
    if doc_vecs.shape[0] != len(docs):
        raise ValueError(
            f"corpus_vectors has {doc_vecs.shape[0]} rows but corpus has {len(docs)} "
            f"texts — they must correspond 1:1 (re-embed with embed_texts(corpus))."
        )
    # Embed the QUERY with Cohere's 'search_query' role (vs 'search_document' for the
    # corpus) so asymmetric retrieval ranks correctly; Titan ignores input_type.
    q_vec = embed_texts([query], model=model, input_type="search_query")[0]

    # Cosine similarity = normalized dot product.
    def _norm(m):
        n = np.linalg.norm(m, axis=-1, keepdims=True)
        return m / np.clip(n, 1e-12, None)
    sims = _norm(doc_vecs) @ _norm(q_vec.reshape(1, -1)).ravel()

    k = int(min(top_k, len(docs)))
    top_idx = np.argsort(-sims)[:k]
    return pd.DataFrame({
        "rank": range(1, k + 1),
        "score": [round(float(sims[i]), 4) for i in top_idx],
        "text": [docs[i] for i in top_idx],
    })


def count_tokens(text, *, model: str = None) -> int:
    """
    Estimate the token count of a string (or the total for a list of strings).

    A fast, dependency-free heuristic (~4 chars/token, with a word-count floor) for
    SIZING a prompt or a batch before you run it — handy to predict cost/latency of
    a big llm_map. It is an ESTIMATE, not an exact tokenizer count.

    NOTE: `model` is accepted for forward-compatibility (a future model-specific
    tokenizer) but is NOT used today — the estimate is model-agnostic. Treat the
    result as a rough size, not a billing-exact token count.

    Example:
        count_tokens(long_prompt)
        count_tokens(df["review"].tolist())   # total across all rows
    """
    items = _as_text_list(text)
    total_chars = sum(len(t) for t in items)
    total_words = sum(len(t.split()) for t in items)
    # ~4 chars/token is the common rule of thumb; never estimate below the word count.
    return max(total_words, round(total_chars / 4))


def list_bedrock_models(*, by_provider: str = None, text_only: bool = True) -> pd.DataFrame:
    """
    List the Bedrock models available in this account/region as a DataFrame
    (columns: model_id, provider, name, input_modalities, output_modalities,
    streaming). Discovery helper — use it to find a value for the `model=` arg of
    invoke_llm / embed_texts / the llm_* helpers.

    Args:
        by_provider: filter to one provider (case-insensitive substring), e.g.
                     "anthropic", "amazon", "cohere".
        text_only:   when True (default) keep only models that accept TEXT input
                     (hides pure image/video models). Set False to see everything.

    Example:
        list_bedrock_models()
        list_bedrock_models(by_provider="anthropic")
    """
    client = _aws.aws_client("bedrock", _aws.aws_region())
    resp = client.list_foundation_models()
    rows = []
    for m in resp.get("modelSummaries", []):
        inputs = m.get("inputModalities", []) or []
        if text_only and "TEXT" not in inputs:
            continue
        provider = m.get("providerName", "") or ""
        if by_provider and by_provider.lower() not in provider.lower():
            continue
        rows.append({
            "model_id": m.get("modelId", ""),
            "provider": provider,
            "name": m.get("modelName", ""),
            "input_modalities": ", ".join(inputs),
            "output_modalities": ", ".join(m.get("outputModalities", []) or []),
            "streaming": bool(m.get("responseStreamingSupported", False)),
        })
    df = pd.DataFrame(rows, columns=["model_id", "provider", "name",
                                     "input_modalities", "output_modalities", "streaming"])
    if len(df):
        df = df.sort_values(["provider", "model_id"]).reset_index(drop=True)
    return df
