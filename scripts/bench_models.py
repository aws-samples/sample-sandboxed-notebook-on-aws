#!/usr/bin/env python3
"""
Model benchmark: Claude Sonnet 4.6 vs Sonnet 5.5 vs Haiku 5.5 for the MicroVM
Notebook use case, with an optional Claude Opus 5 LLM-as-judge quality layer.

WHAT THIS DOES
--------------
Imports the ACTUAL system/one-shot prompts this product ships (from
``proxy/notebook/ai/prompts.py`` — the chat agent's NOTEBOOK_AGENT_PROMPT, the
FIX_ERROR_PROMPT, ANNOTATE_PROMPT, and FIX_SQL_ERROR_PROMPT) and pairs each with a
realistic user turn built the way the app builds it (Python code-gen, NL-to-chart,
SQL generation, the fix-error loop, and cell annotation). It runs the SAME prompts
across a set of model VARIANTS via the Bedrock ``converse`` API and prints a
side-by-side comparison of:

  * latency      — wall-clock seconds for the full (non-streaming) response
  * input/output tokens  — from Bedrock's own ``usage`` block (the real cost driver)
  * a light quality signal — per-task structural checks (e.g. "produced a python
    block", "valid JSON", "used a bind parameter not an f-string") so you get an
    objective first-pass read, not just vibes. These are heuristics, NOT a judge.
  * (optional, --judge) an LLM-as-judge pass with Claude Opus 5 that scores each
    response BLIND (no model label leaked) on correctness / completeness /
    follows_instructions / conciseness, via structured output. Caveat: one
    Anthropic model grading Anthropic models — directional, not ground truth.

VARIANTS COMPARED
-----------------
  * 4.6                    — Sonnet 4.6 (the id the app ships today), plain
  * 5.5 / 5.5-concise / 5.5-lowthink — Sonnet 5.5, plus two conciseness levers
  * haiku-5.5              — Haiku 5.5 (thinking disabled — the fast/cheap tier)
  * *-structured           — the same models with JSON-Schema structured output

It uses the SAME SDK call shape as the app (proxy/notebook/ai/notebook_agent.py:
boto3 ``bedrock-runtime`` client, ``.converse(modelId, messages, inferenceConfig)``,
reading ``response["output"]["message"]["content"][0]["text"]`` + ``response["usage"]``).

MODEL IDS
---------
4.6 is the id the app uses today (BEDROCK_MODEL_ID default). Sonnet 5.5 is
DISCOVERED from Bedrock at runtime (list_inference_profiles / list_foundation_models)
so we never hardcode an id that may not exist in your region. Haiku 5.5 and the
Opus 5 judge default to known us-west-2 ids and are overridable via CLI flags.

USAGE
-----
    # from the repo root, with AWS creds for Bedrock in your region:
    .venv/bin/python scripts/bench_models.py
    .venv/bin/python scripts/bench_models.py --region us-west-2
    .venv/bin/python scripts/bench_models.py --runs 3            # average N runs/task
    .venv/bin/python scripts/bench_models.py --judge            # add the Opus 5 judge
    .venv/bin/python scripts/bench_models.py --json results.json

NOTES / HONESTY
---------------
  * This spends real Bedrock tokens on your account for each task × variant × runs
    (and again per response when --judge is set). Keep --runs small.
  * The quality checks are structural heuristics tuned to THIS product's prompts;
    they flag obvious wins/regressions (missing code block, invalid JSON, raw
    f-string SQL) but do NOT judge correctness/elegance. Use --judge and read the
    saved outputs (--dump) for the real comparison, and ultimately A/B in the live app.
  * Pricing is NOT fetched (Bedrock has no price API); token counts are the
    portable cost proxy. Multiply by your per-token rate card to estimate $.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    print("boto3 is required. Run with the project venv: .venv/bin/python scripts/bench_models.py")
    sys.exit(1)

# Make the repo root importable so `modelcaps` (and `proxy.notebook.ai.prompts`,
# imported later in load_real_prompts) resolve when this script is run as
# `.venv/bin/python scripts/bench_models.py` from the repo root. Mirrors the
# sys.path.insert load_real_prompts already does.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# DOGFOOD the modelcaps API layer: the benchmark now derives "what inference params
# does this model accept" (temperature-dropping, thinking disable-shape, maxTokens
# clamping, CRIS id resolution) FROM THE CATALOG instead of hand-coding it per
# model. The Variant below only carries "what experiment am I running" (a brevity
# instruction, forced structured output, whether to disable thinking) — model-
# capability facts live in modelcaps, experiment levers live in the Variant.
from modelcaps import (  # noqa: E402 - intentional: after the sys.path.insert above
    build_additional_request_fields,
    build_inference_config,
    supports_temperature,
)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

DEFAULT_46 = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
# 5.5 is discovered; this is only a hint/fallback pattern we look for.
HINT_55_SUBSTR = "sonnet-5-5"
# Haiku 5.5 is an explicit, confirmed id (probed live in us-west-2 via Converse —
# works, and has `temperature` DEPRECATED like Sonnet 5.5). We do NOT discover it;
# the --model-haiku CLI arg makes it overridable.
HAIKU_55 = "us.anthropic.claude-haiku-5-5"


def resolve_model_ids(region: str, model_46: str, model_55: str | None) -> tuple[str, str | None, list[str]]:
    """Return (id_46, id_55_or_None, all_available_sonnet_profile_ids).

    id_46 is taken as given (the app's current default; assumed valid). id_55 is
    the explicit override if provided, else discovered by scanning Bedrock
    inference profiles + foundation models for a 'sonnet-5-5' id. We never invent
    an id: if nothing matches, id_55 is None and the caller reports availability.
    """
    available: list[str] = []
    bedrock = boto3.client("bedrock", region_name=region)

    # Inference profiles are the '{geo}.anthropic....' ids the app uses.
    try:
        paginator = bedrock.get_paginator("list_inference_profiles")
        for page in paginator.paginate():
            for p in page.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                if "sonnet" in pid.lower():
                    available.append(pid)
    except Exception as e:
        print(f"  (list_inference_profiles unavailable: {type(e).__name__}: {e})")

    # Also scan base foundation-model ids as a fallback source of truth.
    try:
        fms = bedrock.list_foundation_models(byProvider="anthropic")
        for m in fms.get("modelSummaries", []):
            mid = m.get("modelId", "")
            if "sonnet" in mid.lower():
                available.append(mid)
    except Exception as e:
        print(f"  (list_foundation_models unavailable: {type(e).__name__}: {e})")

    available = sorted(set(available))

    if model_55:
        return model_46, model_55, available

    # Discover: prefer an inference-profile id containing the 5.5 hint.
    candidates = [a for a in available if HINT_55_SUBSTR in a.lower()]
    # Prefer a region-prefixed inference profile (us./eu./apac.) over a bare base id.
    candidates.sort(key=lambda a: (0 if a[:3] in ("us.", "eu.") or a.startswith("apac.") else 1, len(a)))
    id_55 = candidates[0] if candidates else None
    return model_46, id_55, available


# ---------------------------------------------------------------------------
# Real product prompts
# ---------------------------------------------------------------------------
# We import the ACTUAL system/one-shot prompts the app ships (proxy/notebook/ai/
# prompts.py) rather than hand-written copies, so the benchmark exercises the real
# thing and never drifts from production. If the import fails (e.g. a missing
# transitive dep in a bare env), we fall back to loading just the prompt strings
# by exec-ing the module's top-level assignments is NOT safe, so instead we fail
# with a clear message telling you to run from the repo root with the project venv.

def load_real_prompts():
    """Return the real prompts dict from proxy/notebook/ai/prompts.py.

    Must be run from the repo root with the project venv so `proxy...` imports
    resolve. Returns None on failure (caller prints guidance)."""
    # Ensure repo root is importable.
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from proxy.notebook.ai import prompts as P
    except Exception as e:
        print(f"  Could not import the real prompts (proxy.notebook.ai.prompts): {type(e).__name__}: {e}")
        print("  Run from the repo root with the project venv: .venv/bin/python scripts/bench_models.py")
        return None
    # Import the app's OWN context-section builder so the fix tasks append the EXACT
    # same <available_variables>/<full_traceback>/<failing_frame_locals>/
    # <available_data_sources>/<previous_cells> block the real fix_error() path builds
    # (proxy/notebook/ai/notebook_agent.py). Importing the real function (instead of
    # re-implementing it here) means the benchmark can never drift from production.
    # Best-effort: if the agent module can't import (missing transitive dep in a bare
    # env), fall back to None and the fix tasks run without the context block rather
    # than crashing the whole benchmark.
    build_fix_context = None
    try:
        from proxy.notebook.ai.notebook_agent import _build_fix_context_section as build_fix_context
    except Exception as e:
        print(f"  (note: could not import _build_fix_context_section "
              f"({type(e).__name__}: {e}); fix tasks will omit the context block)")
    # The chat system prompt carries several {placeholder} keys (notebook_id,
    # session_id, aws_region, …). We don't want to hardcode/guess the full set, so
    # fill via a defaulting map: known keys get realistic demo values, any other
    # placeholder resolves to a benign token rather than raising KeyError. This keeps
    # the REAL prompt verbatim without us tracking its exact placeholder list.
    class _Defaulting(dict):
        def __missing__(self, key):
            return {
                "notebook_id": "42",
                "session_id": "demo-session-abc123",
                "aws_region": os.environ.get("AWS_REGION", "us-west-2"),
            }.get(key, f"<{key}>")

    # The chat system prompt is the one the chat agent process sends
    # (agent/ai_assistant/prompts.py, generated from proxy/notebook/ai/prompts.py
    # by scripts/gen_prompts.py).
    agent_dir = os.path.join(repo_root, "agent")
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    from ai_assistant.prompts import NOTEBOOK_AGENT_PROMPT as _CHAT_PROMPT
    chat_system = _CHAT_PROMPT.format_map(_Defaulting())
    return {
        "chat_system": chat_system,
        "annotate": P.ANNOTATE_PROMPT,          # .format(cells=...)
        "fix_error": P.FIX_ERROR_PROMPT,        # .format(code=, error=)
        "fix_sql": getattr(P, "FIX_SQL_ERROR_PROMPT", None),  # .format(code=, error=) + context
        "terminal": getattr(P, "TERMINAL_SUGGEST_PROMPT", None),
        # The app's real context-section builder (or None if unavailable). The fix
        # tasks append its output so they match what fix_error() actually sends.
        "build_fix_context": build_fix_context,
    }


# ---------------------------------------------------------------------------
# Tasks — representative of what THIS product asks the model to do.
# Each task carries a REAL system prompt (from the repo) + a realistic user prompt
# built the same way the app builds it.
# ---------------------------------------------------------------------------

@dataclass
class Task:
    name: str
    system: Optional[str]
    prompt: str
    max_tokens: int
    temperature: float
    # quality(text) -> list of (check_name, passed_bool)
    quality: Callable
    # Short, MODEL-AGNOSTIC description of what a good answer must do. Fed to the
    # LLM judge (blind) so it grades against the task's intent, not a label.
    judge_desc: str = ""


def _has_python_block(text: str) -> bool:
    return "```python" in text or ("```" in text and "import " in text)


def _q_codegen(text: str):
    return [
        ("produced a ```python block", _has_python_block(text)),
        ("mentions pandas/requests", ("pandas" in text or "requests" in text)),
        ("non-trivial length (>80 chars)", len(text.strip()) > 80),
    ]


def _q_sql(text: str):
    low = text.lower()
    return [
        ("contains a SELECT", "select" in low),
        ("contains FROM", "from" in low),
        ("no obvious f-string/format injection", "{" not in text or "```" in text),
    ]


def _q_fix(text: str):
    low = text.lower()
    return [
        ("produced code", _has_python_block(text) or "=" in text),
        ("addresses the error (mentions the symbol)", "df" in low or "column" in low or "key" in low),
    ]


def _q_annotate_json(text: str):
    t = text.strip()
    if t.startswith("```json"):
        t = t.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    elif t.startswith("```"):
        t = t.split("```", 1)[1].rsplit("```", 1)[0].strip()
    ok = False
    try:
        json.loads(t)
        ok = True
    except Exception:
        ok = False
    return [("valid JSON output", ok)]


def _q_nl2code(text: str):
    return [
        ("produced a ```python block", _has_python_block(text)),
        ("uses a plotting or df op", any(k in text for k in ("plot", "groupby", "head(", "merge", "plt."))),
    ]


# Realistic user-turn inputs, built the way the app builds them. These feed the
# .format() placeholders in the real prompts (fix_error → code/error, annotate →
# cells) or are the raw user message (chat path).

_FIX_CODE = (
    "import pandas as pd, requests\n"
    "resp = requests.get('https://api.worldbank.org/v2/country/all/indicator/"
    "SP.POP.TOTL?date=2022&format=json&per_page=300').json()\n"
    "df = pd.DataFrame(resp[1])\n"
    "top = df.sort_values('Population').tail(10)[['country','Population']]\n"
    "top"
)
_FIX_ERROR = "KeyError: 'Population'"

_FIX_SQL_QUERY = (
    "SELECT category, SUM(revenue) AS rev\n"
    "FROM orders o JOIN products p ON o.product_id = p.id\n"
    "GROUP BY category ORDER BY rev DESC"
)
_FIX_SQL_ERROR = "Binder Error: Referenced column \"category\" not found; candidates: orders.product_id, products.cat"

# ---------------------------------------------------------------------------
# Context dicts fed to the APP's real _build_fix_context_section(context) so the
# fix tasks append the SAME <available_variables>/<available_data_sources>/
# <previous_cells>/<full_traceback>/<failing_frame_locals> block the live
# fix_error() path builds. Only keys the app actually reads are populated:
#   variables, data_sources (via the generic `sources` list + `_catalog` columns),
#   cells, traceback, error_locals, cell_type. Values are realistic demo data that
#   match the broken query/code so the model gets exactly the information the app
#   would give it (correct columns, source/engine, prior cells).
# ---------------------------------------------------------------------------

# SQL cell context: the broken query joins `orders` to `products`; the error's
# candidate list (orders.product_id, products.cat) only makes sense if the model
# can see that `products` has a `cat` column (not `category`). The data-source
# schemas supply exactly that — two parquet sources matching the query.
_FIX_SQL_CONTEXT = {
    "cell_type": "sql",
    "variables": ["orders", "products"],
    "data_sources": {
        # Generic `sources` list — the shape the current frontend sends (rendered
        # by _build_fix_context_section's primary path). Columns come from the deep
        # `_catalog` entries below (keyed by display_name).
        "sources": [
            {"source_id": "orders", "display_name": "orders", "source_type": "parquet",
             "detail": "s3://microvm-sandbox-artifacts-demo/samples/orders.parquet"},
            {"source_id": "products", "display_name": "products", "source_type": "parquet",
             "detail": "s3://microvm-sandbox-artifacts-demo/samples/products.parquet"},
        ],
        "_catalog": {"entries": [
            {"display_name": "orders", "status": "discovered",
             "columns": ["order_id", "product_id", "revenue", "order_date"]},
            {"display_name": "products", "status": "discovered",
             "columns": ["id", "cat"]},
        ]},
    },
    "cells": [
        {"code": "orders = read_parquet('s3://microvm-sandbox-artifacts-demo/samples/orders.parquet')"},
        {"code": "products = read_parquet('s3://microvm-sandbox-artifacts-demo/samples/products.parquet')"},
    ],
}

# Python cell context for the fix_error task: the cell builds a DataFrame from the
# World Bank API and KeyErrors on 'Population'. The context gives the model the
# variables in scope, the full traceback, the failing-frame locals (showing the
# actual columns the API returned — the key signal), and the prior cell.
_FIX_CODE_CONTEXT = {
    "cell_type": "code",
    "variables": ["pd", "requests", "resp", "df"],
    "traceback": (
        "Traceback (most recent call last):\n"
        "  File \"<cell>\", line 4, in <module>\n"
        "    top = df.sort_values('Population').tail(10)[['country','Population']]\n"
        "  File \"pandas/core/frame.py\", line 6942, in sort_values\n"
        "    k = self._get_label_or_level_values(by[0], axis=axis)\n"
        "KeyError: 'Population'"
    ),
    "error_locals": {
        "df.columns": "['indicator', 'country', 'countryiso3code', 'date', "
                      "'value', 'unit', 'obs_status', 'decimal']",
        "resp_len": "2",
        "resp[1][0]": "{'indicator': {'id': 'SP.POP.TOTL', 'value': 'Population, total'}, "
                      "'country': {'id': 'ZH', 'value': 'Africa Eastern and Southern'}, "
                      "'date': '2022', 'value': 720859132}",
    },
    "cells": [
        {"code": "import pandas as pd, requests"},
    ],
}

_ANNOTATE_CELLS = (
    "[1] CODE: df = read_s3('s3://microvm-sandbox-artifacts-demo/samples/sales.parquet')\n"
    "[2] CODE: monthly = df.groupby(df['date'].dt.to_period('M')).revenue.sum()\n"
    "[3] CODE: plot_line(monthly.reset_index(), x='date', y='revenue')"
)


class _FmtDefault(dict):
    """format_map helper: known keys passed in; any other {placeholder} → '<key>'
    so a real prompt with extra placeholders can't KeyError the benchmark."""
    def __missing__(self, key):
        return "{" + key + "}"  # leave unknown placeholders intact (harmless literal)


def _fill(template: str, **known) -> str:
    return template.format_map(_FmtDefault(known))


def build_tasks(prompts: dict) -> list[Task]:
    """Build the benchmark tasks from the REAL product prompts + realistic user turns."""
    chat_system = prompts["chat_system"]

    # The app's real context-section builder (proxy/notebook/ai/notebook_agent.py).
    # When available, the fix tasks append its output so they replicate EXACTLY what
    # fix_error() sends. When it couldn't be imported, fall back to no context block
    # (the prompt+code+error still make a valid, if less faithful, task).
    _build_fix_context = prompts.get("build_fix_context")

    def _fix_context(ctx: dict) -> str:
        if _build_fix_context is None:
            return ""
        try:
            return _build_fix_context(ctx)
        except Exception:
            return ""

    tasks: list[Task] = [
        # 1) CHAT: generate code from a natural-language request (the agent's PRIMARY job).
        Task(
            name="chat_codegen_public_api",
            system=chat_system,
            prompt="Fetch 2022 total population for all countries from the World Bank API, load it into a "
                   "pandas DataFrame, drop nulls, and show the top 10 most populous countries.",
            # Cap raised 900→3072 UNIFORMLY (all variants) so Sonnet 5.5 finishes at
            # end_turn instead of truncating here; keeps the comparison apples-to-apples.
            max_tokens=3072, temperature=0.0, quality=_q_codegen,
            judge_desc="Runnable Python that calls the World Bank API, builds a pandas DataFrame, "
                       "drops nulls, and shows the top 10 most populous countries for 2022. Should "
                       "handle the API's response shape correctly.",
        ),
        # 2) CHAT: NL -> chart code.
        Task(
            name="chat_nl_to_chart",
            system=chat_system,
            prompt="I have a DataFrame `sales` with columns date, region, revenue. Plot monthly total revenue "
                   "as a line chart.",
            # Cap raised 700→3072 UNIFORMLY (all variants) so Sonnet 5.5 finishes at
            # end_turn instead of truncating here; keeps the comparison apples-to-apples.
            max_tokens=3072, temperature=0.0, quality=_q_nl2code,
            judge_desc="Runnable Python that aggregates the `sales` DataFrame to monthly total revenue "
                       "(summing across regions) and draws a line chart of revenue over month.",
        ),
        # 3) CHAT: SQL generation (notebook SQL cell).
        Task(
            name="chat_sql_generation",
            system=chat_system,
            prompt="Write a SQL cell: given parquet 'orders' (order_id, product_id, revenue, order_date) and "
                   "'products' (id, category), compute total revenue per product category, descending.",
            # Cap raised 500→3072 UNIFORMLY (all variants) so Sonnet 5.5 finishes at
            # end_turn instead of truncating here; keeps the comparison apples-to-apples.
            max_tokens=3072, temperature=0.0, quality=_q_sql,
            judge_desc="A correct SQL query joining orders to products on product_id = id, summing "
                       "revenue grouped by category, ordered by total revenue descending.",
        ),
        # 4) FIX-ERROR one-shot — replicate the app's fix_error() for a CODE cell:
        #    FIX_ERROR_PROMPT.format(code=, error=) + the app's context_section.
        Task(
            name="fix_error",
            system=None,  # FIX_ERROR_PROMPT is the full instruction; user turn is the filled prompt.
            prompt=_fill(prompts["fix_error"], code=_FIX_CODE, error=_FIX_ERROR)
                   + _fix_context(_FIX_CODE_CONTEXT),
            max_tokens=800, temperature=0.0, quality=_q_fix,
            judge_desc="Diagnoses the KeyError (the World Bank API has no 'Population' column; the "
                       "value lives under a 'value' field, and country is a nested object) and returns "
                       "corrected, runnable code that selects the right columns.",
        ),
        # 5) ANNOTATE one-shot (real ANNOTATE_PROMPT with .format(cells=)); expects JSON.
        Task(
            name="annotate_cells_json",
            system=None,
            prompt=_fill(prompts["annotate"], cells=_ANNOTATE_CELLS),
            max_tokens=700, temperature=0.0, quality=_q_annotate_json,
            judge_desc="Valid JSON annotations that accurately and concisely describe what each of the "
                       "three notebook cells does (reading a parquet file, monthly revenue aggregation, "
                       "and plotting), matching the shape the prompt asks for.",
        ),
    ]
    # 6) SQL FIX one-shot — replicate the app's fix_error() for a SQL cell EXACTLY:
    #    FIX_SQL_ERROR_PROMPT.format(code=, error=) + the app's context_section.
    #    NOTE the app's SQL template uses {code}/{error} (NOT {query}/{sql}); filling
    #    query=/sql= like before left {code} unfilled, so the model got NO query to
    #    fix. We now fill {code}=<broken query>/{error}=<binder error> and append the
    #    SAME <available_data_sources> block the app builds, so the model can see that
    #    `products` has a `cat` column (not `category`) and reconstruct the query.
    if prompts.get("fix_sql"):
        try:
            sql_fix_prompt = (_fill(prompts["fix_sql"], code=_FIX_SQL_QUERY, error=_FIX_SQL_ERROR)
                              + _fix_context(_FIX_SQL_CONTEXT))
        except Exception:
            sql_fix_prompt = None
        if sql_fix_prompt:
            tasks.append(Task(
                name="fix_sql_error",
                system=None,
                prompt=sql_fix_prompt,
                max_tokens=600, temperature=0.0, quality=_q_sql,
                judge_desc="Fixes the SQL binder error: the category column lives on products and the "
                           "real column is 'cat' (not 'category'), so the corrected query references "
                           "products.cat (aliased as category) and still groups/orders by it correctly.",
            ))
    return tasks


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class CallResult:
    ok: bool
    latency_s: float = 0.0
    in_tokens: int = 0
    out_tokens: int = 0
    text: str = ""
    error: str = ""
    checks: list = field(default_factory=list)
    stop_reason: str = ""


# A "variant" is a model + how we call it. This lets us A/B not just 4.6 vs 5.5 but
# ALSO the two conciseness levers on 5.5:
#   (a) a conciseness INSTRUCTION appended to the system prompt
#   (b) 5.5's `thinking` mode set to "between_tools" (its low-thinking setting) via
#       additionalModelRequestFields — the REAL knob (discovered empirically;
#       `temperature`/`top_p` are deprecated on 5.5, and thinking defaults to
#       "adaptive", which is what makes it verbose).
# NOTE on maxTokens: it is a HARD CAP (truncates), NOT a conciseness control, so we
# keep it generous and never use it to force brevity.

_CONCISE_SUFFIX = (
    "\n\n<output_brevity>\nBe concise. Return ONLY what was asked — for code tasks, a "
    "single fenced code block with no prose preamble or postscript; for JSON tasks, only "
    "the JSON. Do not explain your reasoning unless explicitly asked.\n</output_brevity>"
)


# ---------------------------------------------------------------------------
# Structured-output (JSON Schema) definitions.
# These are wired per task TYPE. For code/SQL tasks the model returns a JSON
# object {explanation, code}; for the annotate task (which already emits JSON) we
# use a permissive object so we don't over-constrain its existing shape.
# A Variant opts in via `json_schema`, a dict mapping task.name -> schema dict.
# `_converse` json.dumps()es the selected schema into the Converse outputConfig.
# ---------------------------------------------------------------------------

CODE_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "code": {"type": "string"},
    },
    "required": ["explanation", "code"],
    "additionalProperties": False,
}

# The annotate task already emits JSON shaped {root, sections[], comments[]} (see
# ANNOTATE_PROMPT). Bedrock structured outputs REJECT `additionalProperties: true`
# ("For 'object' type, 'additionalProperties: true' is not supported"), so we can't
# use a bare open object. We mirror the prompt's own expected shape with all fields
# OPTIONAL (no `required`) and arrays left loosely typed — permissive without being
# an invalid open object.
ANNOTATE_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": ["string", "null"]},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "before_index": {"type": "integer"},
                    "markdown": {"type": "string"},
                },
                "required": ["before_index", "markdown"],
                "additionalProperties": False,
            },
        },
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "comment": {"type": "string"},
                },
                "required": ["index", "comment"],
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

# Per-task schema mapping used by the structured variants. Tasks not present here
# get NO structured output (skip). Code/SQL tasks share CODE_SCHEMA; the annotate
# task gets the permissive ANNOTATE_SCHEMA.
STRUCTURED_SCHEMAS = {
    "chat_codegen_public_api": CODE_SCHEMA,
    "chat_nl_to_chart": CODE_SCHEMA,
    "chat_sql_generation": CODE_SCHEMA,
    "fix_error": CODE_SCHEMA,
    "fix_sql_error": CODE_SCHEMA,
    "annotate_cells_json": ANNOTATE_SCHEMA,
}


@dataclass
class Variant:
    """A benchmark variant = a model + what EXPERIMENT we run on it.

    IMPORTANT split (post-modelcaps refactor): this struct carries ONLY the
    benchmark's experimental levers, NOT per-model correctness facts. "Does this
    model accept temperature / what thinking-disable shape does it take / what is
    its max-output cap / how is its CRIS id resolved" all come from the modelcaps
    catalog at call time (see _converse). What stays here is the deliberate
    experiment: append a brevity instruction (concise_suffix), force a structured
    JSON-Schema output (json_schema), or turn the model's default thinking OFF
    (disable_thinking) as a lever to measure the token/latency effect.
    """
    label: str
    model_id: str
    # EXPERIMENT lever, not a model fact: when True, ask modelcaps for this model's
    # thinking-disable shape and send it (stops a verbose default reasoning block
    # from eating the maxTokens budget). modelcaps owns the SHAPE per model; the
    # Variant owns the CHOICE of whether to disable for this experiment.
    disable_thinking: bool = False
    concise_suffix: bool = False           # append the brevity instruction to system
    # Structured outputs (JSON Schema): dict mapping task.name -> raw schema dict.
    # _converse json.dumps()es the selected schema; tasks absent from the map skip
    # structured output. None = plain (unstructured) variant.
    json_schema: Optional[dict] = None
    schema_name: str = "result"


# Headroom for structured variants only. Forcing a JSON object (plus, on 5.5, a
# reasoningContent block that is billed against maxTokens) needs MORE output budget
# than the plain text answer, or the response truncates (stopReason 'max_tokens')
# before the JSON closes. We therefore RAISE the cap for structured calls — never
# lower it, and never touch task.max_tokens itself, so the four non-structured
# variants' caps/numbers are byte-for-byte unchanged.
_STRUCTURED_MAXTOK_FLOOR = 4096
_STRUCTURED_MAXTOK_MULT = 4


def _structured_active(task: "Task", v: "Variant") -> bool:
    return bool(v.json_schema) and (v.json_schema.get(task.name) is not None)


def _converse_no_temperature(client, task: Task, v: "Variant"):
    """Retry helper: identical to _converse but with temperature forced off.

    Used by run_task's safety-net retry for a model the catalog didn't yet know
    deprecates temperature. Only the temperature key differs from _converse."""
    return _converse(client, task, v, force_no_temperature=True)


def _converse(client, task: Task, v: "Variant", force_no_temperature: bool = False):
    # ── BASE params come from the modelcaps catalog (dogfooding the API layer) ──
    # build_inference_config() sets maxTokens AND only includes temperature when
    # THIS model actually accepts it (so 5.x models never get a deprecated field)
    # AND clamps maxTokens to the model's catalog-known output cap.
    # build_additional_request_fields() returns the model's exact thinking-disable
    # shape (or None) when this variant's experiment asks to disable thinking.
    # The structured-output headroom is a BENCHMARK concern, so the structured
    # bump is layered on top of the catalog-derived base maxTokens.
    max_tokens = task.max_tokens
    if _structured_active(task, v):
        max_tokens = max(_STRUCTURED_MAXTOK_FLOOR, task.max_tokens * _STRUCTURED_MAXTOK_MULT)
    temperature = None if force_no_temperature else task.temperature
    infcfg = build_inference_config(v.model_id, max_tokens=max_tokens, temperature=temperature)
    kwargs = dict(modelId=v.model_id, messages=[{"role": "user", "content": [{"text": task.prompt}]}],
                  inferenceConfig=infcfg)
    system = task.system or ""
    if v.concise_suffix:
        system = (system + _CONCISE_SUFFIX) if system else _CONCISE_SUFFIX.strip()
    if system:
        kwargs["system"] = [{"text": system}]
    # EXPERIMENT lever: disable the model's default thinking if this variant asks.
    # modelcaps returns the correct per-model shape (or None when the model has no
    # disable shape — e.g. Sonnet 4.6), so we only set the key when it's non-None.
    extra_fields = build_additional_request_fields(v.model_id, disable_thinking=v.disable_thinking)
    if extra_fields:
        kwargs["additionalModelRequestFields"] = extra_fields
    # Structured outputs: when this variant has a schema for THIS task, request a
    # JSON-Schema-constrained response. The schema MUST be a json.dumps STRING
    # (not a dict) in the Converse outputConfig. Combines fine with extra_fields /
    # concise_suffix. Tasks without a mapping fall through unchanged.
    if v.json_schema:
        schema = v.json_schema.get(task.name)
        if schema is not None:
            kwargs["outputConfig"] = {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "name": v.schema_name,
                            "schema": json.dumps(schema),
                        }
                    },
                }
            }
    return client.converse(**kwargs)


def run_task(client, v: "Variant", task: Task) -> CallResult:
    t0 = time.perf_counter()
    try:
        resp = _converse(client, task, v)
    except Exception as e:
        msg = str(e)
        # Safety net: modelcaps already drops `temperature` for models it KNOWS
        # deprecate it (Sonnet/Haiku 5.x), so _converse won't send it there. This
        # retry only fires if the catalog DIDN'T know a given model deprecates
        # temperature yet Bedrock rejected it — we retry once forcing a no-temp
        # call. We detect "did we send temperature?" from the catalog itself
        # (supports_temperature) rather than a removed per-Variant flag.
        temp_was_sent = supports_temperature(v.model_id) and task.temperature is not None
        if "temperature" in msg and ("deprecated" in msg or "ValidationException" in msg) and temp_was_sent:
            # Re-issue the same call but with temperature forced off. We rebuild the
            # kwargs directly here (rather than via a Variant flag) so the ONE thing
            # that changes is the temperature key — everything else, including the
            # catalog-derived thinking/maxTokens, is identical.
            try:
                resp = _converse_no_temperature(client, task, v)
            except Exception as e2:
                return CallResult(ok=False, latency_s=time.perf_counter() - t0, error=f"{type(e2).__name__}: {e2}")
        else:
            return CallResult(ok=False, latency_s=time.perf_counter() - t0, error=f"{type(e).__name__}: {e}")
    dt = time.perf_counter() - t0
    # Pull the text content block. Sonnet 5.5 can prepend a `reasoningContent`
    # block (extended thinking), so content[0] is not always the text — scan for
    # the first block that actually carries a 'text' key instead of hardcoding [0].
    text = ""
    try:
        content = resp["output"]["message"]["content"] or []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                text = block["text"]
                break
    except Exception:
        text = ""
    stop_reason = resp.get("stopReason", "")
    usage = resp.get("usage", {}) or {}
    checks = []
    # If this variant used a schema for this task, the output is a JSON object
    # (e.g. {explanation, code}) rather than fenced prose/code, so the existing
    # structural heuristics would mis-score it. Parse the JSON and run quality on
    # the extracted 'code' field (code/SQL tasks); for the annotate task there is
    # no 'code' key, so we run quality on the whole JSON text. Add a 'valid JSON'
    # pass/fail. Defensive: any failure falls back to scoring the raw text.
    structured_active = _structured_active(task, v)
    try:
        if structured_active:
            try:
                obj = json.loads(text)
                valid_json = True
            except Exception:
                obj = None
                valid_json = False
            if valid_json and isinstance(obj, dict) and "code" in obj:
                target = obj.get("code") or ""
            else:
                target = text
            checks = [("valid JSON", valid_json)] + task.quality(target)
        else:
            checks = task.quality(text)
    except Exception:
        checks = [("quality-check errored", False)]
    return CallResult(
        ok=True, latency_s=dt,
        in_tokens=int(usage.get("inputTokens", 0)),
        out_tokens=int(usage.get("outputTokens", 0)),
        text=text, checks=checks, stop_reason=stop_reason,
    )


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


# ---------------------------------------------------------------------------
# LLM-as-judge layer (optional, gated behind --judge).
# ---------------------------------------------------------------------------
# After the normal benchmark, we can have a STRONGER model (Opus 5 by default)
# grade each variant's response ABSOLUTELY (1-5 per axis), not pairwise. To avoid
# the well-known LLM-judge biases we keep it BLIND: the judge never sees which
# model produced a response or its label — only the original user prompt, a short
# model-agnostic description of what a good answer must do, and the response text.
# Scoring is via structured output (same outputConfig shape as the benchmark) so
# we get a clean, parseable rubric instead of free-form prose.
#
# Opus 5 caveat (probed live): `temperature` is DEPRECATED on Opus 5 (rejects it),
# so the judge Converse call sends inferenceConfig.maxTokens ONLY — no temperature.

DEFAULT_JUDGE_MODEL = "us.anthropic.claude-opus-5"

# NOTE: Bedrock structured outputs REJECT `minimum`/`maximum` on integer types
# ("For 'integer' type, properties maximum, minimum are not supported"), so we do
# NOT constrain the 1-5 range in the schema. Instead we state the scale explicitly
# in the judge prompt and CLAMP each score to [1,5] on parse (see _judge_response).
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "correctness": {"type": "integer"},
        "completeness": {"type": "integer"},
        "follows_instructions": {"type": "integer"},
        "conciseness": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["correctness", "completeness", "follows_instructions", "conciseness", "rationale"],
    "additionalProperties": False,
}

JUDGE_AXES = ("correctness", "completeness", "follows_instructions", "conciseness")

_JUDGE_SYSTEM = (
    "You are a strict, impartial code/response reviewer. You grade a single candidate "
    "response ON ITS OWN MERITS against a task, on a 1-5 integer scale per axis (1 = very "
    "poor, 5 = excellent). You do NOT know or care which system produced the response; judge "
    "only the text in front of you. Be calibrated: reserve 5 for responses with no meaningful "
    "flaws. Return ONLY the structured JSON asked for."
)


def _extract_judged_payload(cell_text: str, task: "Task", structured: bool) -> str:
    """Return the text the judge should grade.

    For structured variants the stored text is a JSON envelope; grade the actual
    deliverable (the 'code' field) for code/SQL tasks so the judge scores the code,
    not JSON formatting. For the annotate task (no 'code' key) grade the whole JSON.
    Non-structured responses are graded as-is. Defensive: any parse failure falls
    back to the raw text."""
    if not structured:
        return cell_text
    try:
        obj = json.loads(cell_text)
    except Exception:
        return cell_text
    if isinstance(obj, dict) and "code" in obj and task.name != "annotate_cells_json":
        return obj.get("code") or cell_text
    return cell_text


def _judge_response(client, judge_model: str, task: "Task", response_text: str) -> dict:
    """Score one response with the judge model via structured output.

    BLIND by construction: the prompt carries only the task's user prompt, the
    model-agnostic judge description, and the response text — never a model id or
    variant label. Raises on API/parse failure so the caller can record a judge
    error for just that cell and keep going."""
    good = task.judge_desc or "A correct, complete answer that does exactly what the task asks."
    judge_user = (
        "Grade the CANDIDATE RESPONSE below against the task. Score each axis 1-5 and give a "
        "rationale of AT MOST two short sentences (keep it brief — do not quote the response "
        "back at length).\n\n"
        "AXES:\n"
        "- correctness: is it technically right / would it actually work?\n"
        "- completeness: does it cover everything the task asked for?\n"
        "- follows_instructions: does it do what was asked, in the requested form?\n"
        "- conciseness: is it free of needless verbosity/boilerplate (without omitting substance)?\n\n"
        f"=== TASK (what the user asked) ===\n{task.prompt}\n\n"
        f"=== WHAT A GOOD ANSWER MUST DO ===\n{good}\n\n"
        f"=== CANDIDATE RESPONSE (grade this) ===\n{response_text}\n"
    )
    resp = client.converse(
        modelId=judge_model,
        messages=[{"role": "user", "content": [{"text": judge_user}]}],
        system=[{"text": _JUDGE_SYSTEM}],
        # maxTokens must comfortably fit 4 integers + the rationale string. At 1024 the
        # judge's structured JSON was TRUNCATED mid-`rationale` on long code responses
        # (stopReason=max_tokens), yielding unparseable JSON ("Unterminated string").
        # 4096 gives ample headroom; we also bound the rationale in-prompt and fail
        # loudly (below) if a judgement still truncates, rather than emitting a cryptic
        # JSONDecodeError. NO temperature: deprecated on Opus 5.
        inferenceConfig={"maxTokens": 4096},
        outputConfig={
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "name": "judge_score",
                        "schema": json.dumps(JUDGE_SCHEMA),
                    }
                },
            }
        },
    )
    # If the judge itself hit the output cap, its JSON is incomplete — surface that
    # as a precise error instead of letting json.loads raise an opaque decode error.
    stop = resp.get("stopReason", "")
    if stop == "max_tokens":
        raise RuntimeError("judge response truncated (stopReason=max_tokens); raise judge maxTokens")
    text = ""
    content = resp["output"]["message"]["content"] or []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            text = block["text"]
            break
    obj = json.loads(text)
    out = {}
    for axis in JUDGE_AXES:
        # Schema can't bound the range (Bedrock rejects min/max on integers), so
        # clamp to [1,5] defensively in case the judge returns out-of-range.
        out[axis] = max(1, min(5, int(obj[axis])))
    out["rationale"] = str(obj.get("rationale", ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B benchmark Sonnet 4.6 vs 5.5 for the Sandboxed Notebook on AWS use case.")
    ap.add_argument("--region", default=os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-west-2")))
    ap.add_argument("--model-46", default=DEFAULT_46, help=f"4.6 model/profile id (default {DEFAULT_46})")
    ap.add_argument("--model-55", default=None, help="5.5 model/profile id (default: discover from Bedrock)")
    ap.add_argument("--model-haiku", default=HAIKU_55, help=f"Haiku 5.5 model/profile id (default {HAIKU_55})")
    ap.add_argument("--runs", type=int, default=1, help="runs per task, averaged (default 1)")
    ap.add_argument("--dump", action="store_true", help="print each model's full output text per task")
    ap.add_argument("--json", metavar="PATH", help="write full machine-readable results to PATH")
    ap.add_argument("--judge", action="store_true",
                    help="after the run, have an LLM judge (Opus 5) score each response 1-5 per axis "
                         "(BLIND/absolute). Costs extra Bedrock calls; off by default.")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                    help=f"judge model/profile id (default {DEFAULT_JUDGE_MODEL})")
    args = ap.parse_args()

    print(f"Region: {args.region}")
    print("Resolving model ids...")
    id_46, id_55, available = resolve_model_ids(args.region, args.model_46, args.model_55)
    haiku_id = args.model_haiku  # explicit, confirmed id (overridable via --model-haiku)
    print(f"  4.6 → {id_46}")
    print(f"  Haiku 5.5 → {haiku_id}")
    if id_55:
        print(f"  5.5 → {id_55}")
    else:
        print("  5.5 → NOT FOUND in this region.")
        print("  Available Sonnet ids discovered:")
        for a in available:
            print(f"      {a}")
        print("\n  Sonnet 5.5 is not available (or not enabled) in this Bedrock region.")
        print("  Enable model access in the Bedrock console, or pass --model-55 <id> explicitly, then re-run.")
        return 2

    print("Loading the real product prompts (proxy/notebook/ai/prompts.py)...")
    prompts = load_real_prompts()
    if prompts is None:
        return 3
    tasks = build_tasks(prompts)
    print(f"  loaded real prompts; built {len(tasks)} tasks "
          f"(chat system prompt is {len(prompts['chat_system'])} chars).")

    cfg = BotoConfig(retries={"max_attempts": 2, "mode": "standard"}, read_timeout=120)
    rt = boto3.client("bedrock-runtime", region_name=args.region, config=cfg)

    # The variants we compare. 4.6 is the baseline. Then three ways to run 5.5:
    #   5.5            — stock (thinking defaults to "adaptive"; this is the verbose one)
    #   5.5-concise    — same model, + a brevity INSTRUCTION appended to the system prompt
    #   5.5-lowthink   — same model, + thinking:{"type":"between_tools"} (the real low-
    #                     thinking knob; temperature/top_p are deprecated on 5.5)
    # This isolates "does telling 5.5 to be brief, or turning its thinking down, close
    # the token/latency gap to 4.6?" — the actual question.
    # Minimal-thinking knobs, PROBED live per model (the field is NOT uniform):
    #   Sonnet 4.6   — emits NO budget-eating reasoning block; accepts thinking.disabled
    #                  but between_tools is REJECTED. 4.6 never truncated at the stock
    #                  caps, so we leave its plain call UNCHANGED (no extra_fields).
    #   Sonnet 5.5   — only `thinking:{"type":"between_tools"}` turns thinking off
    #                  (thinking.disabled is REJECTED). Applied to the plain 5.5 variant
    #                  so its reasoning block stops consuming the maxTokens budget.
    #                  NOTE: this makes plain `5.5` converge with `5.5-lowthink` (same
    #                  knob) — an accepted, clearly-labeled consequence of killing the
    #                  truncation without raising caps.
    #   Haiku 5.5    — accepts `thinking:{"type":"disabled"}` (between_tools REJECTED);
    #                  applied to the plain haiku-5.5 variant.
    # This removes truncation at the ORIGINAL per-task caps, so token counts reflect
    # only the real answer. The -structured variants keep their own headroom/intent.
    # NOTE (post-modelcaps refactor): temperature-dropping and the exact thinking-
    # disable SHAPE are no longer hand-coded per variant — modelcaps supplies them
    # at call time from the catalog (temperature auto-dropped for 5.x; the right
    # {"thinking": …} shape per model). What each Variant still declares is its
    # EXPERIMENT: disable_thinking (turn the default reasoning block OFF to measure
    # the token/latency effect), concise_suffix (a brevity instruction), and
    # json_schema (forced structured output).
    variants: list[Variant] = [
        Variant(label="4.6", model_id=id_46),
        # 5.5 stock-for-this-bench: disable thinking so its verbose default block
        # stops eating the maxTokens budget (modelcaps picks between_tools for 5.5).
        Variant(label="5.5", model_id=id_55, disable_thinking=True),
        # 5.5-concise keeps the model's DEFAULT (adaptive) thinking ON and instead
        # layers a brevity instruction — a distinct experiment from killing thinking.
        Variant(label="5.5-concise", model_id=id_55, concise_suffix=True),
        # 5.5-lowthink explicitly disables thinking (same knob modelcaps owns).
        Variant(label="5.5-lowthink", model_id=id_55, disable_thinking=True),
        # Structured outputs (JSON Schema) for BOTH models, so we can compare
        # structured vs unstructured on each. For code/SQL tasks the model returns
        # {explanation, code}; the annotate task gets a permissive object schema.
        Variant(label="4.6-structured", model_id=id_46, json_schema=STRUCTURED_SCHEMAS),
        Variant(label="5.5-structured", model_id=id_55, json_schema=STRUCTURED_SCHEMAS),
        # Haiku 5.5 — cheaper/faster candidate substitute for Sonnet 4.6. temperature
        # is DEPRECATED on Haiku 5.5 (modelcaps drops it automatically). Disable
        # thinking (modelcaps picks "disabled" for Haiku) so the comparison to 4.6 and
        # 4.6-structured is apples-to-apples (same schema map on the structured pair).
        Variant(label="haiku-5.5", model_id=haiku_id, disable_thinking=True),
        # Behavior-equivalent to the pre-refactor variant: the structured haiku
        # variant did NOT disable thinking (it relies on the structured maxTokens
        # headroom instead), so disable_thinking stays False here.
        Variant(label="haiku-5.5-structured", model_id=haiku_id,
                json_schema=STRUCTURED_SCHEMAS),
    ]
    labels = [v.label for v in variants]
    results: dict = {
        "region": args.region,
        "models": {"4.6": id_46, "5.5": id_55, "haiku-5.5": haiku_id},
        # Record the EXPERIMENT levers per variant. temperature-dropping + the
        # thinking SHAPE are now catalog-derived (modelcaps), so we record the
        # variant's CHOICE (disable_thinking) and whether the model supports
        # temperature (from modelcaps) rather than a removed per-Variant flag.
        "variants": {v.label: {
            "model_id": v.model_id,
            "supports_temperature": supports_temperature(v.model_id),
            "disable_thinking": v.disable_thinking,
            "concise_suffix": v.concise_suffix,
            "thinking_field": build_additional_request_fields(
                v.model_id, disable_thinking=v.disable_thinking),
            "structured": bool(v.json_schema),
        } for v in variants},
        "runs": args.runs, "tasks": {},
    }

    for task in tasks:
        print(f"\n=== Task: {task.name} ===")
        results["tasks"][task.name] = {}
        for v in variants:
            lat, itok, otok = [], [], []
            check_pass, check_total = 0, 0
            last_text, err, last_stop = "", "", ""
            for _ in range(args.runs):
                r = run_task(rt, v, task)
                if not r.ok:
                    err = r.error
                    break
                lat.append(r.latency_s); itok.append(r.in_tokens); otok.append(r.out_tokens)
                last_text = r.text
                last_stop = r.stop_reason
                for _n, ok in r.checks:
                    check_total += 1
                    check_pass += 1 if ok else 0
            if err:
                print(f"  [{v.label:>12}] ERROR: {err}")
                results["tasks"][task.name][v.label] = {"error": err}
                continue
            a_lat, a_in, a_out = avg(lat), avg(itok), avg(otok)
            qual = f"{check_pass}/{check_total}" if check_total else "n/a"
            trunc = " TRUNCATED" if last_stop == "max_tokens" else ""
            print(f"  [{v.label:>12}] {a_lat:6.2f}s  in={a_in:5.0f} out={a_out:5.0f} tok  "
                  f"quality={qual} stop={last_stop}{trunc}")
            results["tasks"][task.name][v.label] = {
                "latency_s": round(a_lat, 3), "in_tokens": round(a_in),
                "out_tokens": round(a_out), "quality": qual,
                "stop_reason": last_stop, "text": last_text,
            }
            if args.dump:
                print(f"      --- {v.label} output ---")
                for line in last_text.splitlines():
                    print(f"      {line}")

    # Summary
    print("\n" + "=" * 68)
    print("SUMMARY (averaged over runs)")
    print("=" * 68)
    tot = {label: {"lat": [], "in": [], "out": []} for label in labels}
    for tname, byvariant in results["tasks"].items():
        for label in labels:
            d = byvariant.get(label, {})
            if "latency_s" in d:
                tot[label]["lat"].append(d["latency_s"])
                tot[label]["in"].append(d["in_tokens"])
                tot[label]["out"].append(d["out_tokens"])
    for label in labels:
        t = tot[label]
        print(f"  {label:>12}: avg latency {avg(t['lat']):5.2f}s | "
              f"total in={sum(t['in']):6.0f} out={sum(t['out']):6.0f} tok "
              f"(sum over {len(t['lat'])} tasks)")

    # Relative to 4.6 (the baseline the app ships today): how do the 5.5 variants compare?
    base = tot.get("4.6", {})
    o_base = sum(base.get("out", []) or [0])
    l_base = avg(base.get("lat", []) or [0])
    if o_base:
        print(f"\n  Relative to 4.6 (baseline):")
        for label in labels:
            if label == "4.6":
                continue
            t = tot[label]
            o = sum(t["out"]) if t["out"] else 0
            l = avg(t["lat"]) if t["lat"] else 0
            out_x = f"{o / o_base:.2f}x out" if o_base else "n/a"
            lat_x = f"{l / l_base:.2f}x latency" if l_base else "n/a"
            print(f"    {label:>12}: {out_x:>12} | {lat_x}")
        # Did either conciseness lever help 5.5 vs stock 5.5?
        o_55 = sum(tot["5.5"]["out"]) if tot.get("5.5", {}).get("out") else 0
        if o_55:
            print(f"\n  Conciseness levers vs STOCK 5.5 (out tokens):")
            for label in ("5.5-concise", "5.5-lowthink"):
                t = tot.get(label, {})
                o = sum(t.get("out", [])) if t.get("out") else 0
                if o:
                    delta = (o - o_55) / o_55 * 100.0
                    print(f"    {label:>12}: {o:6.0f} tok ({delta:+.0f}% vs stock 5.5)")
    print("\n  NOTE: quality here is a STRUCTURAL heuristic, not a correctness judge.")
    print("  Read the --dump outputs and A/B in the live app before deciding.")

    # -----------------------------------------------------------------------
    # LLM-as-judge pass (optional). Grades every cell ABSOLUTELY + BLIND.
    # -----------------------------------------------------------------------
    if args.judge:
        print("\n" + "=" * 68)
        print(f"JUDGE PASS — scoring each response with {args.judge_model} (blind, absolute 1-5)")
        print("=" * 68)
        judge_errors: list[str] = []
        task_by_name = {t.name: t for t in tasks}
        for tname, byvariant in results["tasks"].items():
            task = task_by_name.get(tname)
            if task is None:
                continue
            for label in labels:
                cell = byvariant.get(label, {})
                if "text" not in cell:
                    # A benchmark error on this cell → nothing to judge.
                    cell["judge"] = {"error": cell.get("error", "no response text to judge")}
                    continue
                structured = results["variants"].get(label, {}).get("structured", False)
                payload = _extract_judged_payload(cell["text"], task, structured)
                try:
                    scores = _judge_response(rt, args.judge_model, task, payload)
                    cell["judge"] = scores
                    overall = avg([scores[a] for a in JUDGE_AXES])
                    print(f"  [{tname} / {label:>20}] "
                          f"corr={scores['correctness']} compl={scores['completeness']} "
                          f"instr={scores['follows_instructions']} concise={scores['conciseness']} "
                          f"overall={overall:.2f}")
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    cell["judge"] = {"error": msg}
                    judge_errors.append(f"{tname}/{label}: {msg}")
                    print(f"  [{tname} / {label:>20}] JUDGE ERROR: {msg}")

        # JUDGE SUMMARY: per-variant average of each axis across tasks + overall.
        print("\n" + "=" * 68)
        print("JUDGE SUMMARY (avg over tasks; 1-5 per axis, overall = mean of 4 axes)")
        print("=" * 68)
        jtot = {label: {a: [] for a in JUDGE_AXES} for label in labels}
        for tname, byvariant in results["tasks"].items():
            for label in labels:
                j = byvariant.get(label, {}).get("judge", {})
                if "error" in j:
                    continue
                for a in JUDGE_AXES:
                    if a in j:
                        jtot[label][a].append(j[a])
        header = f"  {'variant':>20}  {'corr':>5} {'compl':>5} {'instr':>5} {'concise':>7}  {'OVERALL':>7}  {'n':>3}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        # Order: put 4.6 and haiku-5.5 adjacent for easy comparison, then the rest.
        preferred = ["4.6", "haiku-5.5", "haiku-5.5-structured", "4.6-structured"]
        ordered = [l for l in preferred if l in labels] + [l for l in labels if l not in preferred]
        overalls = {}
        for label in ordered:
            axis_avgs = {a: avg(jtot[label][a]) for a in JUDGE_AXES}
            n = max((len(jtot[label][a]) for a in JUDGE_AXES), default=0)
            overall = avg([axis_avgs[a] for a in JUDGE_AXES]) if n else 0.0
            overalls[label] = overall
            print(f"  {label:>20}  {axis_avgs['correctness']:5.2f} {axis_avgs['completeness']:5.2f} "
                  f"{axis_avgs['follows_instructions']:5.2f} {axis_avgs['conciseness']:7.2f}  "
                  f"{overall:7.2f}  {n:3d}")

        # Direct head-to-head the user cares about: Haiku 5.5 vs Sonnet 4.6.
        if "4.6" in overalls and "haiku-5.5" in overalls:
            d = overalls["haiku-5.5"] - overalls["4.6"]
            print(f"\n  haiku-5.5 vs 4.6 (overall): {overalls['haiku-5.5']:.2f} vs "
                  f"{overalls['4.6']:.2f}  (Δ {d:+.2f})")

        if judge_errors:
            print(f"\n  JUDGE ERRORS ({len(judge_errors)}):")
            for e in judge_errors:
                print(f"    {e}")
        else:
            print("\n  Judge errors: none (every cell scored).")

        print("\n  DISCLAIMER: the judge is a single Anthropic model grading Anthropic models, so it may "
              "carry house-style / self-preference bias. These scores are DIRECTIONAL, not ground truth; "
              "for code the stronger eval is actually executing it.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n  Full results written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
