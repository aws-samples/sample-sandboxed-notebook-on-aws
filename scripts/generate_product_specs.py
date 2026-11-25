#!/usr/bin/env python3
"""
generate_product_specs.py — Build demo product-specification PDFs for the
Bedrock Knowledge Base data source.

WHAT THIS DOES
--------------
Queries the top-N most popular products (by units sold) from the Athena demo
warehouse (microvm_demo_db: products JOIN orders) and emits ONE multi-page,
category-aware product-spec PDF per product into scripts/demoproductspec/.

These PDFs follow a typical e-commerce spec-doc layout: an Executive Summary, a
Product Overview with key features, Technical Specification tables, several
category-specific detail sections, an embedded chart figure (making the docs
genuinely multimodal so Bedrock's multimodal FM parser has something to extract),
and warranty/support content.

CONTENT: by default the per-product prose (summary, overview, features, spec
tables, detail sections, warranty) is authored by the project's Bedrock model
(Claude Sonnet 4.6 — same as the notebook AI), which invents plausible,
category-appropriate, internally-consistent details per product. This produces
detailed, varied spec sheets rather than generic templated filler. Each field
falls back to a built-in category template if the Bedrock call fails or returns
unparseable output, so generation never hard-fails. Pass --no-llm to skip Bedrock
entirely and use the fast, offline templated content.

Each PDF is accompanied by a `<name>.pdf.metadata.json` sidecar (the same shape
Bedrock Knowledge Bases consume for metadata filtering), carrying the product's
structured attributes (category, brand, price, rating, popularity).

WHY BUILD-TIME
--------------
This is a developer/deploy-time tool, NOT VM or proxy runtime code. It depends on
reportlab + matplotlib (+ boto3, pypdf), which live in the dedicated venv
scripts/.specgen-venv/ (created with exact pins by scripts/setup_bedrock_kb.sh via
scripts/kb_specs_lib.sh), never in app/requirements.txt / proxy/requirements.txt.

The generated PDFs are NOT committed (scripts/demoproductspec/ is gitignored). On
first install, local_install.sh → scripts/setup_bedrock_kb.sh finds the folder
empty and runs this script with --no-llm (built-in template, no Bedrock calls),
then uploads the set to s3://<ARTIFACT_BUCKET>/knowledge-base/product-guides/.
Later installs reuse the local set. To regenerate with Bedrock-authored content:
    bash scripts/setup_bedrock_kb.sh --regen          # LLM prose (default)
    bash scripts/setup_bedrock_kb.sh --regen --no-llm # template again

USAGE
-----
    scripts/.specgen-venv/bin/python scripts/generate_product_specs.py [--no-llm]
    # options:
    #   --no-llm           built-in template only (no Bedrock); what install uses
    #   --top N            number of products (default 25)
    #   --out DIR          output dir (default scripts/demoproductspec)
    #   --from-json FILE   skip Athena, read products from a saved JSON array
    #   --region / --workgroup / --database  Athena overrides

The Athena query is self-contained so this is re-runnable in a fresh deploy; if
Athena is unreachable, pass --from-json with a previously-saved product array.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import date

# reportlab (PDF authoring) + matplotlib (figure) come from scripts/.specgen-venv.
try:
    import matplotlib
    matplotlib.use("Agg")  # headless — no display needed
    import matplotlib.pyplot as plt
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image as RLImage, PageBreak, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"Missing PDF authoring deps ({e}). Create the venv first:\n"
        "  python3 -m venv scripts/.specgen-venv\n"
        "  scripts/.specgen-venv/bin/python -m pip install reportlab==5.1.0 \\\n"
        "      matplotlib==3.10.9 boto3==1.43.51 pypdf==6.21.4\n"
        "  (or let scripts/setup_bedrock_kb.sh create it)\n"
    )
    sys.exit(2)


# ----------------------------------------------------------------------------
# Product sourcing
# ----------------------------------------------------------------------------

TOP_PRODUCTS_SQL = """
SELECT p.product_id, p.name, p.category, p.brand, p.price, p.cost,
       p.stock_quantity, p.rating_avg,
       COUNT(o.order_id)  AS order_count,
       SUM(o.quantity)    AS units_sold,
       SUM(o.total)       AS revenue
FROM products p
JOIN orders o ON o.product_id = p.product_id
GROUP BY p.product_id, p.name, p.category, p.brand, p.price, p.cost,
         p.stock_quantity, p.rating_avg
ORDER BY units_sold DESC
LIMIT {top}
"""


def fetch_products_from_athena(top, region, workgroup, database):
    """Run the popularity query on Athena and return a list of product dicts."""
    import boto3
    client = boto3.client("athena", region_name=region)
    qid = client.start_query_execution(
        QueryString=TOP_PRODUCTS_SQL.format(top=int(top)),
        WorkGroup=workgroup,
        QueryExecutionContext={"Database": database},
    )["QueryExecutionId"]

    # Poll to completion.
    for _ in range(60):
        st = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = st["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = st.get("StateChangeReason", "unknown")
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(2)
    else:
        raise RuntimeError("Athena query timed out")

    # Page through results, flattening the ResultSet into dicts.
    rows = []
    header = None
    paginator = client.get_paginator("get_query_results")
    for page in paginator.paginate(QueryExecutionId=qid):
        for r in page["ResultSet"]["Rows"]:
            vals = [c.get("VarCharValue") for c in r["Data"]]
            if header is None:
                header = vals
                continue
            rows.append(dict(zip(header, vals)))
    return rows


def load_products(args):
    if args.from_json:
        with open(args.from_json) as f:
            data = json.load(f)
        return data[: args.top]
    return fetch_products_from_athena(
        args.top, args.region, args.workgroup, args.database
    )


# ----------------------------------------------------------------------------
# Category-aware content model
#
# Each category maps to a "profile": the product noun, a set of key features, a
# spec-table builder, and a category-specific section. This keeps the generated
# text plausible and varied per category rather than one generic template.
# ----------------------------------------------------------------------------

def _f(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _i(val, default=0):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


CATEGORY_PROFILES = {
    "Electronics": {
        "noun": "device",
        "summary": (
            "engineered for performance and reliability, combining modern components "
            "with energy-efficient operation and a durable build suited to daily use"
        ),
        "features": [
            "High-performance internal components tuned for responsiveness",
            "Energy-efficient operation with intelligent power management",
            "Durable enclosure rated for everyday handling",
            "Broad connectivity (USB-C, Bluetooth 5.3, Wi-Fi 6)",
            "Firmware-upgradeable for long-term feature support",
        ],
        "spec_rows": [
            ("Processor", "Quad-core, up to 2.4 GHz"),
            ("Memory", "8 GB LPDDR5"),
            ("Storage", "128 GB eMMC / expandable"),
            ("Connectivity", "Wi-Fi 6, Bluetooth 5.3, USB-C"),
            ("Battery", "Rated for up to 12 hours active use"),
            ("Operating Temp.", "0°C to 40°C"),
        ],
        "extra_title": "Connectivity & Ports",
        "extra_rows": [
            ("USB-C", "2x (data + charging, 10 Gbps)"),
            ("Wireless", "Wi-Fi 6E (802.11ax), Bluetooth 5.3"),
            ("Audio", "3.5mm combo jack"),
            ("Indicators", "RGB status LED"),
        ],
    },
    "Clothing": {
        "noun": "garment",
        "summary": (
            "a versatile, comfortable everyday piece made from premium materials, "
            "combining softness and durability with a timeless, easy-care design"
        ),
        "features": [
            "Premium preshrunk fabric for lasting fit",
            "Breathable, soft-touch construction",
            "Reinforced double-needle stitching",
            "Colorfast dye that resists fading",
            "Comprehensive size range for all body types",
        ],
        "spec_rows": [
            ("Primary Material", "100% preshrunk cotton"),
            ("Weight", "5.4 oz/yd² (medium-weight)"),
            ("Fabric Type", "Jersey knit"),
            ("Care", "Machine washable, tumble dry low"),
            ("Origin", "Ethically sourced"),
        ],
        "extra_title": "Sizing Chart (inches)",
        "extra_rows": [
            ("Small (S)", "Chest 18 / Length 28"),
            ("Medium (M)", "Chest 20 / Length 29"),
            ("Large (L)", "Chest 22 / Length 30"),
            ("X-Large (XL)", "Chest 24 / Length 31"),
        ],
    },
    "Books": {
        "noun": "title",
        "summary": (
            "a professionally produced edition offering engaging content with "
            "high-quality printing and binding built to last through repeated reading"
        ),
        "features": [
            "Acid-free paper for archival longevity",
            "Durable sewn binding that lies flat",
            "Clear, legible typesetting",
            "Available in paperback and eBook formats",
            "Includes index and reference notes",
        ],
        "spec_rows": [
            ("Format", "Paperback / eBook"),
            ("Page Count", "320 pages"),
            ("Dimensions", "6 x 9 inches"),
            ("Paper", "Acid-free, 80 gsm"),
            ("Language", "English"),
        ],
        "extra_title": "Edition Details",
        "extra_rows": [
            ("Binding", "Perfect-bound softcover"),
            ("ISBN", "Assigned per edition"),
            ("Publisher", "In-house imprint"),
            ("First Printing", str(date.today().year)),
        ],
    },
    "Beauty": {
        "noun": "product",
        "summary": (
            "a carefully formulated personal-care product using skin-friendly "
            "ingredients, dermatologically tested for gentle, effective daily use"
        ),
        "features": [
            "Dermatologically tested, hypoallergenic formula",
            "Free from parabens and sulfates",
            "Cruelty-free and not tested on animals",
            "Suitable for sensitive skin",
            "Recyclable packaging",
        ],
        "spec_rows": [
            ("Volume", "150 ml"),
            ("Skin Type", "All / sensitive"),
            ("Shelf Life", "24 months unopened"),
            ("Fragrance", "Light, hypoallergenic"),
            ("Certification", "Cruelty-free"),
        ],
        "extra_title": "Ingredients & Usage",
        "extra_rows": [
            ("Base", "Water, glycerin, botanical extracts"),
            ("Application", "Apply to clean skin, morning and night"),
            ("Storage", "Store below 25°C, away from sunlight"),
            ("Patch Test", "Recommended before first use"),
        ],
    },
    "Home & Kitchen": {
        "noun": "product",
        "summary": (
            "a durable, practical home essential designed for everyday use, "
            "combining functional design with easy cleaning and long service life"
        ),
        "features": [
            "Food-safe, BPA-free materials",
            "Ergonomic, easy-grip design",
            "Dishwasher-safe components",
            "Scratch- and stain-resistant finish",
            "Compact storage footprint",
        ],
        "spec_rows": [
            ("Material", "Food-grade stainless steel / BPA-free plastic"),
            ("Capacity", "1.5 L"),
            ("Care", "Dishwasher safe (top rack)"),
            ("Warranty", "2-year limited"),
            ("Dimensions", "20 x 12 x 10 cm"),
        ],
        "extra_title": "Care & Maintenance",
        "extra_rows": [
            ("Cleaning", "Rinse after each use; dishwasher safe"),
            ("Do Not", "Use abrasive scourers"),
            ("Storage", "Dry fully before storing"),
            ("Replacement Parts", "Available via support"),
        ],
    },
    "Toys": {
        "noun": "toy",
        "summary": (
            "a safe, engaging toy designed to encourage play and learning, built "
            "to child-safety standards from durable, non-toxic materials"
        ),
        "features": [
            "Meets ASTM F963 / EN71 safety standards",
            "Non-toxic, child-safe materials",
            "Encourages creativity and motor skills",
            "Durable construction for active play",
            "No sharp edges; age-appropriate design",
        ],
        "spec_rows": [
            ("Recommended Age", "3+ years"),
            ("Material", "Non-toxic ABS plastic"),
            ("Safety", "ASTM F963, EN71 compliant"),
            ("Batteries", "Not required"),
            ("Dimensions", "25 x 18 x 8 cm"),
        ],
        "extra_title": "Safety & Compliance",
        "extra_rows": [
            ("Choking Hazard", "Not suitable for under 3"),
            ("Certifications", "CE, ASTM F963"),
            ("Materials", "BPA-free, lead-free paint"),
            ("Supervision", "Adult supervision recommended"),
        ],
    },
    "Sports": {
        "noun": "gear",
        "summary": (
            "performance sports equipment engineered for durability and comfort, "
            "supporting active use with materials chosen for grip, support, and wear"
        ),
        "features": [
            "Moisture-wicking, breathable materials",
            "Reinforced high-wear zones",
            "Ergonomic support and grip",
            "Lightweight for extended activity",
            "Weather-resistant finish",
        ],
        "spec_rows": [
            ("Material", "Breathable synthetic composite"),
            ("Weight", "320 g"),
            ("Use", "Training and competition"),
            ("Sizing", "Unisex, S–XL"),
            ("Care", "Hand wash, air dry"),
        ],
        "extra_title": "Performance & Fit",
        "extra_rows": [
            ("Support", "Reinforced midsole / structured panels"),
            ("Grip", "High-traction contact surface"),
            ("Ventilation", "Mesh airflow zones"),
            ("Conditions", "Indoor and outdoor use"),
        ],
    },
    "Food & Grocery": {
        "noun": "product",
        "summary": (
            "a quality grocery product sourced to consistent standards, offering "
            "reliable freshness with clear nutritional labeling and safe packaging"
        ),
        "features": [
            "Sourced to consistent quality standards",
            "Clear nutritional and allergen labeling",
            "Resealable, food-safe packaging",
            "No artificial preservatives",
            "Traceable supply chain",
        ],
        "spec_rows": [
            ("Net Weight", "500 g"),
            ("Storage", "Cool, dry place"),
            ("Shelf Life", "12 months"),
            ("Allergens", "See packaging"),
            ("Certification", "Food-safety audited"),
        ],
        "extra_title": "Nutrition & Storage",
        "extra_rows": [
            ("Serving Size", "50 g"),
            ("Storage (opened)", "Refrigerate, use within 7 days"),
            ("Preservatives", "None added"),
            ("Origin", "Regionally sourced"),
        ],
    },
}

# Fallback profile for any category not explicitly modeled.
_DEFAULT_PROFILE = {
    "noun": "product",
    "summary": (
        "a quality product designed for everyday use, combining reliable "
        "construction with practical, user-focused design"
    ),
    "features": [
        "Quality construction and materials",
        "Designed for everyday use",
        "Reliable performance",
        "Backed by manufacturer support",
        "Value-focused design",
    ],
    "spec_rows": [
        ("Material", "Quality-grade materials"),
        ("Warranty", "1-year limited"),
        ("Support", "Standard customer support"),
    ],
    "extra_title": "Additional Details",
    "extra_rows": [
        ("Packaging", "Retail-ready"),
        ("Support", "Available via customer service"),
    ],
}


def profile_for(category: str) -> dict:
    return CATEGORY_PROFILES.get(category or "", _DEFAULT_PROFILE)


# ----------------------------------------------------------------------------
# LLM content generation (Bedrock)
#
# For richer, product-specific prose than the category templates, we ask the
# project's Bedrock model (Claude Sonnet 4.6 by default) to author the spec
# content as STRUCTURED JSON. Structured output keeps the PDF layout stable while
# the text becomes detailed and varied per product. Every LLM field falls back to
# the category template if the call fails or returns unparseable/short output, so
# the generator NEVER hard-fails on a bad model response — it degrades to templated.
# ----------------------------------------------------------------------------

# Model + region mirror the project convention (see proxy/notebook/routes/ai.py).
DEFAULT_LLM_MODEL = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-5-5")
DEFAULT_LLM_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-west-2"))

_LLM_CLIENT = None  # lazily created, reused across products


def _llm_client(region):
    global _LLM_CLIENT
    if _LLM_CLIENT is None:
        import boto3
        from botocore.config import Config
        _LLM_CLIENT = boto3.client(
            "bedrock-runtime", region_name=region,
            config=Config(retries={"max_attempts": 4, "mode": "standard"},
                          read_timeout=90),
        )
    return _LLM_CLIENT


LLM_SPEC_PROMPT = """You are a technical product writer. Write a detailed, realistic \
product specification document for the following e-commerce product. The product is \
synthetic demo data, so INVENT plausible, specific, and internally-consistent details \
(materials, dimensions, performance figures, components, standards) appropriate to the \
category. Do NOT mention that the product is synthetic or demo data.

Product:
- Name: {name}
- Category: {category}
- Brand: {brand}
- List price: ${price:.2f}
- Average rating: {rating:.1f} / 5
- Units sold (popularity): {units}

Return ONLY a JSON object (no markdown fences, no commentary) with EXACTLY this shape:
{{
  "summary": "2-3 sentence executive summary describing what the product is and who it's for",
  "overview": "1 paragraph (4-6 sentences) product overview describing design, build, and intended use",
  "features": ["6-8 specific key-feature bullet strings"],
  "spec_rows": [["Attribute","Value"], ... 6-9 rows of concrete technical specifications appropriate to the category],
  "sections": [
     {{"title":"Section title","body":"2-4 sentence paragraph","rows":[["Attribute","Value"], ... 3-6 rows]}},
     ... 2-3 category-appropriate detailed sections (e.g. performance, materials, usage, care, safety, compatibility)
  ],
  "warranty": "1 paragraph describing warranty and support terms"
}}

Keep every string concise (no field longer than ~400 characters). Values in spec_rows \
and section rows must be short (a few words). Make the content SPECIFIC to this product \
and category, not generic. If you mention any email address, website, or support/warranty \
contact, it MUST use an RFC 2606 reserved domain of the form \
support@<brand-slug>.example.com (brand name lowercased with non-alphanumerics replaced \
by hyphens). Do NOT use any real registrable domain for emails or websites."""


def _coerce_rows(rows):
    """Normalize an LLM 'rows' value into a list of (label, value) 2-tuples of str.
    Tolerates dicts, ragged lists, or missing cells so a slightly-off model response
    still renders instead of crashing the table builder."""
    out = []
    if isinstance(rows, dict):
        rows = [[k, v] for k, v in rows.items()]
    if not isinstance(rows, list):
        return out
    for r in rows:
        if isinstance(r, dict):
            # e.g. {"attribute": "...", "value": "..."} or a single-key pair
            vals = list(r.values())
            if len(vals) >= 2:
                out.append((str(vals[0]), str(vals[1])))
            elif len(r) == 1:
                (k, v), = r.items()
                out.append((str(k), str(v)))
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            out.append((str(r[0]), str(r[1])))
        elif isinstance(r, (list, tuple)) and len(r) == 1:
            out.append((str(r[0]), ""))
    return out


def generate_llm_content(product, model, region):
    """Ask Bedrock for structured spec content. Returns a dict with keys
    summary/overview/features/spec_rows/sections/warranty, or None on any failure
    (caller falls back to the category template)."""
    prompt = LLM_SPEC_PROMPT.format(
        name=product.get("name") or "Product",
        category=product.get("category") or "General",
        brand=product.get("brand") or "Generic",
        price=_f(product.get("price")),
        rating=_f(product.get("rating_avg")),
        units=_i(product.get("units_sold")),
    )
    try:
        client = _llm_client(region)
        resp = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 4096, "temperature": 0.7},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
    except Exception as e:
        sys.stderr.write(f"    LLM call failed ({type(e).__name__}: {str(e)[:120]}) — using template\n")
        return None

    # Strip accidental ```json fences and parse.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        # Grab the outermost JSON object if the model wrapped it in prose.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        data = json.loads(text)
    except Exception as e:
        sys.stderr.write(f"    LLM JSON parse failed ({str(e)[:80]}) — using template\n")
        return None

    features = data.get("features")
    features = [str(x) for x in features] if isinstance(features, list) else None

    sections = []
    for s in (data.get("sections") or []):
        if not isinstance(s, dict):
            continue
        sections.append({
            "title": str(s.get("title", "Details")),
            "body": str(s.get("body", "")),
            "rows": _coerce_rows(s.get("rows")),
        })

    content = {
        "summary": (str(data["summary"]) if data.get("summary") else None),
        "overview": (str(data["overview"]) if data.get("overview") else None),
        "features": features if features else None,
        "spec_rows": _coerce_rows(data.get("spec_rows")) or None,
        "sections": sections or None,
        "warranty": (str(data["warranty"]) if data.get("warranty") else None),
    }
    # Require at least the core prose fields; otherwise treat as failure.
    if not content["summary"] or not content["features"] or not content["spec_rows"]:
        sys.stderr.write("    LLM output missing core fields — using template\n")
        return None
    return content


# RFC 2606 reserved second-level labels. An email/website domain ending in one of
# these (e.g. "foo.example.com", "example.net") is already safe demo data.
_RESERVED_DOMAIN_RE = re.compile(
    r"(?:\.|^)(?:example\.(?:com|net|org)|example|test|invalid|localhost)$",
    re.IGNORECASE,
)
# Matches an email address: local-part@domain (domain = dot-separated labels).
_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b"
)


def brand_slug(brand: str) -> str:
    """RFC-2606-safe brand slug: lowercase, non-alphanumerics → single hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", (brand or "generic").lower()).strip("-")
    return slug or "generic"


def _sanitize_emails(text: str, brand: str) -> str:
    """Rewrite any email at a real registrable domain to the brand's RFC 2606
    reserved form (support@<brand-slug>.example.com). Addresses already on an
    example./.test/.invalid/.localhost domain are left unchanged. Pure string
    transform — no network, no reportlab/Athena/Bedrock dependency."""
    if not text:
        return text
    reserved = f"support@{brand_slug(brand)}.example.com"

    def _repl(m: "re.Match") -> str:
        domain = m.group(2)
        if _RESERVED_DOMAIN_RE.search(domain):
            return m.group(0)  # already a reserved demo domain — leave as-is
        return reserved

    return _EMAIL_RE.sub(_repl, text)


def _sanitize_content_emails(content: dict, brand: str) -> dict:
    """Walk a resolved-content dict and rewrite any real-domain email in its
    string/list/section fields to the brand's RFC 2606 reserved form, so no PDF
    can ever carry a non-reserved support/warranty email regardless of what the
    model returned (or a future template edit introduces)."""
    def _san(v):
        if isinstance(v, str):
            return _sanitize_emails(v, brand)
        if isinstance(v, list):
            return [_san(x) for x in v]
        if isinstance(v, tuple):
            return tuple(_san(x) for x in v)
        if isinstance(v, dict):
            return {k: _san(x) for k, x in v.items()}
        return v

    for key in ("summary", "overview", "features", "spec_rows", "sections", "warranty"):
        if key in content and content[key] is not None:
            content[key] = _san(content[key])
    return content


def resolve_content(product, use_llm, model, region):
    """Merge LLM-generated content over the category template. Any field the LLM
    didn't supply (or when use_llm is False) falls back to the template so the PDF
    is always fully populated."""
    prof = profile_for(product.get("category") or "")
    tmpl = {
        "summary": prof["summary"],           # note: template summary is a clause, see build_pdf
        "overview": None,
        "features": prof["features"],
        "spec_rows": prof["spec_rows"],
        "sections": [{"title": prof["extra_title"], "body": "", "rows": prof["extra_rows"]}],
        "warranty": None,
        "_from_llm": False,
        "_noun": prof["noun"],
    }
    brand = product.get("brand") or "Generic"
    if not use_llm:
        return _sanitize_content_emails(tmpl, brand)
    llm = generate_llm_content(product, model, region)
    if not llm:
        return _sanitize_content_emails(tmpl, brand)
    merged = dict(tmpl)
    for k in ("summary", "overview", "features", "spec_rows", "sections", "warranty"):
        if llm.get(k):
            merged[k] = llm[k]
    merged["_from_llm"] = True
    # Deterministic post-processing guardrail: no PDF may contain a support/
    # warranty email at a real registrable domain, whatever the model returned.
    return _sanitize_content_emails(merged, brand)


# ----------------------------------------------------------------------------
# Figure (multimodal element)
# ----------------------------------------------------------------------------

def build_popularity_figure(product: dict) -> io.BytesIO:
    """Render a small bar chart of this product's popularity metrics as a PNG,
    returned as an in-memory buffer for embedding in the PDF. Having a real
    figure makes the doc multimodal so the Bedrock FM parser extracts an image."""
    units = _i(product.get("units_sold"))
    orders = _i(product.get("order_count"))
    stock = _i(product.get("stock_quantity"))
    rating = _f(product.get("rating_avg"))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.4, 2.6))

    # Left: volume metrics.
    labels = ["Units\nSold", "Orders", "In\nStock"]
    vals = [units, orders, stock]
    bars = ax1.bar(labels, vals, color=["#2563eb", "#7c3aed", "#059669"])
    ax1.set_title("Demand & Inventory", fontsize=9)
    ax1.tick_params(labelsize=8)
    for b, v in zip(bars, vals):
        ax1.text(b.get_x() + b.get_width() / 2, b.get_height(),
                 str(v), ha="center", va="bottom", fontsize=7)

    # Right: rating gauge (0-5).
    ax2.barh(["Rating"], [rating], color="#f59e0b")
    ax2.barh(["Rating"], [5 - rating], left=[rating], color="#e5e7eb")
    ax2.set_xlim(0, 5)
    ax2.set_title(f"Avg. Rating: {rating:.1f} / 5", fontsize=9)
    ax2.tick_params(labelsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


# ----------------------------------------------------------------------------
# PDF assembly
# ----------------------------------------------------------------------------

def _styles():
    # Custom style names are prefixed 'Spec' to avoid colliding with reportlab's
    # built-in stylesheet keys (Title, Normal, Bullet, Heading1, ... are already
    # defined by getSampleStyleSheet(); re-adding any of those raises KeyError).
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(name="SpecTitle", parent=ss["Title"], fontSize=22,
                          spaceAfter=4, textColor=colors.HexColor("#111827")))
    ss.add(ParagraphStyle(name="SpecSub", parent=ss["Normal"], fontSize=11,
                          textColor=colors.HexColor("#6b7280"), spaceAfter=2,
                          alignment=TA_CENTER))
    ss.add(ParagraphStyle(name="SpecH1", parent=ss["Heading1"], fontSize=14,
                          textColor=colors.HexColor("#1e3a8a"), spaceBefore=14,
                          spaceAfter=6))
    ss.add(ParagraphStyle(name="SpecH2", parent=ss["Heading2"], fontSize=12,
                          textColor=colors.HexColor("#374151"), spaceBefore=10,
                          spaceAfter=4))
    ss.add(ParagraphStyle(name="SpecBody", parent=ss["Normal"], fontSize=10,
                          leading=14, spaceAfter=6))
    ss.add(ParagraphStyle(name="SpecBullet", parent=ss["Normal"], fontSize=10,
                          leading=14, leftIndent=14, spaceAfter=2))
    ss.add(ParagraphStyle(name="SpecCaption", parent=ss["Normal"], fontSize=8,
                          textColor=colors.HexColor("#6b7280"),
                          alignment=TA_CENTER, spaceBefore=2, spaceAfter=8))
    return ss


def _kv_table(rows, ss, col1="Specification", col2="Detail"):
    data = [[Paragraph(f"<b>{col1}</b>", ss["SpecBody"]),
             Paragraph(f"<b>{col2}</b>", ss["SpecBody"])]]
    for k, v in rows:
        data.append([Paragraph(str(k), ss["SpecBody"]), Paragraph(str(v), ss["SpecBody"])])
    t = Table(data, colWidths=[2.3 * inch, 3.9 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f3f4f6")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def safe_filename(name: str) -> str:
    """File naming convention: '<Product Name> - Product Spec.pdf'."""
    clean = re.sub(r"[^\w\s\-&]", "", name or "product").strip()
    clean = re.sub(r"\s+", " ", clean)
    return f"{clean} - Product Spec.pdf"


def build_pdf(product: dict, out_dir: str, content: dict) -> tuple[str, int]:
    """Write one product-spec PDF + metadata sidecar. Returns (path, page_count).

    `content` is the resolved content dict (LLM-generated merged over the category
    template) from resolve_content(): summary/overview/features/spec_rows/sections/
    warranty (+ _from_llm, _noun)."""
    ss = _styles()
    name = product.get("name") or "Product"
    category = product.get("category") or "General"
    brand = product.get("brand") or "Generic"
    noun = content.get("_noun") or "product"
    price = _f(product.get("price"))
    rating = _f(product.get("rating_avg"))

    fname = safe_filename(name)
    path = os.path.join(out_dir, fname)

    doc = SimpleDocTemplate(
        path, pagesize=LETTER,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        title=f"{name} - Product Specification",
        author=brand,
    )
    story = []

    # --- Title block ---
    story.append(Paragraph(name, ss["SpecTitle"]))
    story.append(Paragraph("Product Specification Document", ss["SpecSub"]))
    story.append(Paragraph(
        f"Brand: {brand} &nbsp;|&nbsp; Category: {category} &nbsp;|&nbsp; "
        f"SKU: {product.get('product_id', 'N/A')}", ss["SpecSub"]))
    story.append(Paragraph(f"Version 1.0 &nbsp;|&nbsp; {date.today():%B %Y}", ss["SpecSub"]))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1,
                            color=colors.HexColor("#1e3a8a")))

    def para(text):
        """Render text as one or more paragraphs (split on blank lines), escaping
        reportlab markup so LLM text with stray < > & doesn't break parsing."""
        # Use stdlib html.escape (not xml.sax.saxutils) to escape & < > — there is no
        # XML parsing here, so this is a pure string-escape; html.escape(quote=False)
        # matches saxutils.escape's behavior (leaves quotes untouched) and is not
        # flagged by the Semgrep use-defused-xml rule.
        import html
        for block in re.split(r"\n\s*\n", str(text).strip()):
            block = block.strip()
            if block:
                story.append(Paragraph(html.escape(block, quote=False).replace("\n", "<br/>"),
                                       ss["SpecBody"]))

    # --- Executive Summary ---
    story.append(Paragraph("Executive Summary", ss["SpecH1"]))
    summary = content.get("summary") or ""
    if content.get("_from_llm"):
        # LLM summary is a complete 2-3 sentence paragraph — use as-is, then append
        # the popularity fact so the doc always ties back to the demo warehouse.
        para(summary)
        story.append(Paragraph(
            f"Priced at ${price:,.2f} with an average customer rating of {rating:.1f} "
            f"out of 5, it is among the most popular items in its category "
            f"({_i(product.get('units_sold'))} units sold across "
            f"{_i(product.get('order_count'))} orders).", ss["SpecBody"]))
    else:
        # Template summary is a sentence clause — splice it into the framing sentence.
        story.append(Paragraph(
            f"The {name} is a {category.lower()} {noun} from {brand}, {summary}. "
            f"Priced at ${price:,.2f}, it carries an average customer rating of "
            f"{rating:.1f} out of 5 and is one of the most popular items in its "
            f"category, with {_i(product.get('units_sold'))} units sold across "
            f"{_i(product.get('order_count'))} orders.", ss["SpecBody"]))

    # --- 1. Product Overview ---
    story.append(Paragraph("1. Product Overview", ss["SpecH1"]))
    if content.get("overview"):
        para(content["overview"])
    else:
        story.append(Paragraph(
            f"The {name} is engineered to deliver dependable everyday value. This "
            f"document details its specifications, features, and support terms so "
            f"buyers and reviewers can assess suitability for their needs.", ss["SpecBody"]))
    story.append(Paragraph("1.1 Key Features", ss["SpecH2"]))
    for feat in content["features"]:
        story.append(Paragraph(f"• {feat}", ss["SpecBullet"]))

    # --- Figure ---
    story.append(Paragraph("1.2 Popularity & Ratings", ss["SpecH2"]))
    fig_buf = build_popularity_figure(product)
    story.append(RLImage(fig_buf, width=5.6 * inch, height=2.28 * inch))
    story.append(Paragraph(
        "Figure 1: Demand, inventory, and average customer rating.", ss["SpecCaption"]))

    # --- 2. Technical Specifications ---
    story.append(Paragraph("2. Technical Specifications", ss["SpecH1"]))
    story.append(Paragraph("2.1 General Specifications", ss["SpecH2"]))
    story.append(_kv_table(content["spec_rows"], ss))
    story.append(Spacer(1, 8))

    # Commercial spec table (always present, derived from the row data).
    story.append(Paragraph("2.2 Commercial Details", ss["SpecH2"]))
    margin = price - _f(product.get("cost"))
    story.append(_kv_table([
        ("List Price", f"${price:,.2f}"),
        ("Average Rating", f"{rating:.1f} / 5"),
        ("Units Sold (demo)", str(_i(product.get("units_sold")))),
        ("Orders (demo)", str(_i(product.get("order_count")))),
        ("In Stock", str(_i(product.get("stock_quantity")))),
        ("Est. Unit Margin", f"${margin:,.2f}"),
    ], ss, col1="Attribute", col2="Value"))

    story.append(PageBreak())

    # --- 3+. Category-specific / detailed sections ---
    for idx, sec in enumerate(content.get("sections") or [], start=3):
        story.append(Paragraph(f"{idx}. {sec.get('title', 'Details')}", ss["SpecH1"]))
        if sec.get("body"):
            para(sec["body"])
        rows = sec.get("rows") or []
        if rows:
            story.append(_kv_table(rows, ss, col1="Attribute", col2="Detail"))
        story.append(Spacer(1, 10))
    next_num = 3 + len(content.get("sections") or [])

    # --- N. Warranty & Support ---
    story.append(Paragraph(f"{next_num}. Warranty & Support", ss["SpecH1"]))
    if content.get("warranty"):
        para(content["warranty"])
    else:
        story.append(Paragraph(
            f"The {name} is covered by {brand}'s standard limited warranty against "
            f"manufacturing defects. Support is available through the {brand} customer "
            f"service portal. Retain proof of purchase to validate warranty claims.",
            ss["SpecBody"]))
    story.append(Paragraph(f"{next_num}.1 What's Included", ss["SpecH2"]))
    for line in ("Manufacturer limited warranty",
                 "Access to online documentation and support",
                 "Replacement parts where applicable",
                 "Return window per retailer policy"):
        story.append(Paragraph(f"• {line}", ss["SpecBullet"]))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor("#d1d5db")))
    story.append(Paragraph(
        f"This specification document is provided for demonstration purposes as "
        f"part of the Sandboxed Notebook on AWS product knowledge base. Product data is "
        f"synthetic (source: microvm_demo_db.products).", ss["SpecCaption"]))

    doc.build(story)

    # Count pages for verification (reportlab doesn't return it directly).
    page_count = _count_pages(path)

    # --- Metadata sidecar (Bedrock KB metadata-filtering format) ---
    _write_metadata_sidecar(path, product, category, brand)
    return path, page_count


def _count_pages(path: str) -> int:
    try:
        import pypdf
        return len(pypdf.PdfReader(path).pages)
    except Exception:
        return -1


def _write_metadata_sidecar(pdf_path, product, category, brand):
    """Write <pdf>.metadata.json — the shape Bedrock Knowledge Bases read for
    metadata filtering."""
    meta = {
        "metadataAttributes": {
            "doc_type": "product_spec",
            "category": category,
            "brand": brand,
            "product_id": product.get("product_id") or "",
            "product_name": product.get("name") or "",
            "price": _f(product.get("price")),
            "rating_avg": _f(product.get("rating_avg")),
        }
    }
    with open(pdf_path + ".metadata.json", "w") as f:
        json.dump(meta, f, indent=2)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Generate demo product-spec PDFs for the KB.")
    ap.add_argument("--top", type=int, default=25, help="Number of products (default 25)")
    ap.add_argument("--out", default=os.path.join(here, "demoproductspec"),
                    help="Output directory (default scripts/demoproductspec)")
    ap.add_argument("--from-json", help="Read products from a JSON array instead of Athena")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--workgroup", default=os.environ.get("ATHENA_WORKGROUP", "microvm-demo"))
    ap.add_argument("--database", default=os.environ.get("ATHENA_DB", "microvm_demo_db"))
    # LLM content generation (default ON). Use --no-llm for fast, offline, templated
    # content (no Bedrock calls). --model / --llm-region override the Bedrock target.
    ap.add_argument("--no-llm", dest="use_llm", action="store_false",
                    help="Skip Bedrock; use templated content only")
    ap.set_defaults(use_llm=True)
    ap.add_argument("--model", default=DEFAULT_LLM_MODEL,
                    help=f"Bedrock model id for content (default {DEFAULT_LLM_MODEL})")
    ap.add_argument("--llm-region", default=DEFAULT_LLM_REGION,
                    help=f"Bedrock region (default {DEFAULT_LLM_REGION})")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    print(f"Sourcing top {args.top} products "
          f"({'JSON: ' + args.from_json if args.from_json else 'Athena: ' + args.database})...")
    products = load_products(args)
    print(f"  got {len(products)} products")
    if args.use_llm:
        print(f"  content: LLM ({args.model} @ {args.llm_region}), template fallback")
    else:
        print("  content: templated (--no-llm)")

    manifest = []
    llm_used = 0
    for i, p in enumerate(products, 1):
        content = resolve_content(p, args.use_llm, args.model, args.llm_region)
        if content.get("_from_llm"):
            llm_used += 1
        path, pages = build_pdf(p, args.out, content)
        size = os.path.getsize(path)
        manifest.append({
            "product_id": p.get("product_id"),
            "name": p.get("name"),
            "category": p.get("category"),
            "file": os.path.basename(path),
            "pages": pages,
            "bytes": size,
            "llm_generated": content.get("_from_llm", False),
        })
        tag = "LLM" if content.get("_from_llm") else "tmpl"
        print(f"  [{i:2}/{len(products)}] {os.path.basename(path):48} "
              f"{pages}p  {size/1024:.0f} KB  [{tag}]")

    # A manifest lets the deploy step know exactly what to upload.
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nWrote {len(manifest)} PDFs + sidecars + manifest.json to {args.out}")
    if args.use_llm:
        print(f"  {llm_used}/{len(manifest)} used LLM content "
              f"({len(manifest) - llm_used} fell back to template)")


if __name__ == "__main__":
    main()
