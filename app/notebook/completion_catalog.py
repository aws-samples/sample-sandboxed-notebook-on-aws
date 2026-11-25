"""
Completion Catalog — build-time introspection of Python builtins + common
data-science libraries into a static autocomplete catalog.

Part of: app.notebook (application layer)

WHY THIS EXISTS
    The cell editor's autocomplete has no language server. To offer signature-rich
    completions for builtins (round, sum, open, ...) and pre-baked libraries
    (pandas, numpy, plotly, boto3) WITHOUT a per-keystroke engine like Jedi, we
    introspect those libraries ONCE — at image build time, when they're installed —
    and emit a compact JSON catalog. The editor fetches it and offers:
      • builtins + module-level functions (pd.read_csv, np.arange, ...) — PRECISE,
        module-qualified, with signatures + param snippets.
      • common class methods (DataFrame.clip, Series.value_counts, str.upper, ...) —
        a pooled "common methods" list surfaced on dot-completion BEFORE a cell has
        run, ranked below the live /introspect results (which are exact post-run).

    This is a static heuristic, not type inference: it can't know a given variable
    IS a DataFrame before it runs. It complements (does not replace) live
    introspection and the /helpers metadata.

USAGE
    Build time (in the Docker image, after pip install, libs present):
        python -m app.notebook.completion_catalog        # writes catalog.json
    Runtime (served by GET /completions-catalog):
        from .completion_catalog import load_catalog
        load_catalog()   # cached read of the baked catalog.json

CATALOG SHAPE
    {
      "generated_at": "<iso>",
      "python": "3.11.x",
      "builtins":  [ <entry>, ... ],
      "modules":   { "pd": [ <entry>, ... ], "np": [...], ... },
      "classes":   { "DataFrame": [ <entry>, ... ], "str": [...], ... }
    }
    <entry> = {
      name, kind ("function"|"method"|"builtin"),
      signature ("(args) -> ret" tail; "" when unavailable for C callables),
      params: [ {name, type, default, optional}, ... ],
      returns, summary (first docstring line)
    }
"""

from __future__ import annotations

import os
import re
import json
import inspect
import pkgutil
import importlib
from datetime import datetime, timezone


# Where the baked catalog lives inside the image (and is read at runtime).
_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "completion_catalog.json")

# Module-level function sources → the alias the user types in a cell. Only public,
# genuinely-callable, signature-bearing functions are kept. Import failures are
# tolerated (a module may be absent in a slimmer image) — that source is skipped.
_MODULE_SOURCES = {
    "pd": "pandas",
    "np": "numpy",
    "px": "plotly.express",
    "boto3": "boto3",
    # A few stdlib faves users reach for in notebooks:
    "os": "os",
    "json": "json",
    "re": "re",
    "math": "math",
    "datetime": "datetime",
    "random": "random",
}

# Classes whose methods form the "common methods on dot" pool. Keyed by the bare
# class name the editor matches against; value is "module:qualname" to import.
_CLASS_SOURCES = {
    "DataFrame": "pandas:DataFrame",
    "Series": "pandas:Series",
    "ndarray": "numpy:ndarray",
    # Builtin container/str types are always importable from builtins.
    "str": "builtins:str",
    "list": "builtins:list",
    "dict": "builtins:dict",
    "set": "builtins:set",
    "tuple": "builtins:tuple",
}

# Caps so the catalog (and the editor menus it feeds) stay snappy.
_MAX_FUNCS_PER_MODULE = 400
_MAX_METHODS_PER_CLASS = 250


def _annotation_str(annotation) -> str:
    """Render a parameter/return annotation compactly (int, str, DataFrame, ...).
    Mirrors the helper-metadata renderer so the two catalogs read consistently."""
    empty = (inspect.Parameter.empty, inspect.Signature.empty)
    if annotation in empty:
        return ""
    if isinstance(annotation, type):
        return annotation.__name__
    return re.sub(r"\b[\w.]+\.", "", str(annotation))  # strip module paths (pandas.core.frame.DataFrame -> DataFrame)


def _describe_callable(name: str, fn, kind: str) -> dict | None:
    """Introspect one callable into a catalog entry, or None if it should be
    skipped. Many C-extension callables (a lot of numpy/pandas) have no signature —
    we still keep them (name + docstring summary) but with an empty signature so the
    editor can at least suggest the name and show the doc."""
    doc = ""
    try:
        doc = inspect.getdoc(fn) or ""
    except Exception:
        doc = ""
    summary = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")

    params: list[dict] = []
    signature = ""
    returns = ""
    try:
        sig = inspect.signature(fn)
        for p in sig.parameters.values():
            if p.name in ("self", "cls"):
                continue
            kind_prefix = "**" if p.kind == p.VAR_KEYWORD else ("*" if p.kind == p.VAR_POSITIONAL else "")
            has_default = p.default is not inspect.Parameter.empty
            params.append({
                "name": kind_prefix + p.name,
                "type": _annotation_str(p.annotation),
                "default": repr(p.default) if has_default else None,
                "optional": has_default or kind_prefix != "",
                # Keyword-only (after a bare * in the signature): the caller MUST pass
                # it by name. Surfaced so autocomplete inserts `name=` and signature
                # help can flag it, preventing "takes 0 positional arguments" errors.
                "kw_only": p.kind == p.KEYWORD_ONLY,
            })
        returns = _annotation_str(sig.return_annotation)
        # Signature tail shown in the completion detail column, e.g. "(a, b=0) -> int".
        # Rebuild from the kept params (not str(sig)) so self/cls is dropped and the
        # display matches what actually gets inserted. Types are omitted here to keep
        # the detail compact; the full doc carries the rich detail.
        arg_bits = []
        for pr in params:
            bit = pr["name"]
            if pr["default"] is not None:
                bit += f"={pr['default']}"
            arg_bits.append(bit)
        signature = f"({', '.join(arg_bits)})"
        if returns:
            signature = f"{signature} -> {returns}"
    except (ValueError, TypeError):
        # No introspectable signature (typical for C builtins) — keep name + doc only.
        pass

    return {
        "name": name,
        "kind": kind,
        "signature": signature,
        "params": params,
        "returns": returns,
        "summary": summary,
    }


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _build_builtins() -> list[dict]:
    """Introspect the builtin functions/types users call bare: round, sum, open ..."""
    import builtins as _b
    out = []
    for name in dir(_b):
        if not _is_public(name):
            continue
        obj = getattr(_b, name, None)
        # Callables only (functions, builtin_function_or_method, and the type
        # constructors like list/dict/str which are useful to complete + call).
        if not callable(obj):
            continue
        # Skip exception classes — they're callable but users don't complete-and-call
        # ArithmeticError(); they'd just clutter the menu.
        if isinstance(obj, type) and issubclass(obj, BaseException):
            continue
        entry = _describe_callable(name, obj, "builtin")
        if entry:
            out.append(entry)
    out.sort(key=lambda e: e["name"])
    return out


def _build_module(mod_name: str) -> list[dict]:
    """Introspect the PUBLIC, module-level functions of one module."""
    try:
        mod = importlib.import_module(mod_name)
    except Exception:
        return []
    out = []
    for name, obj in inspect.getmembers(mod):
        if not _is_public(name):
            continue
        # Module-level FUNCTIONS (and builtin functions). Skip classes/modules/data —
        # class methods are catalogued separately, and we don't want submodules here.
        if not (inspect.isfunction(obj) or inspect.isbuiltin(obj) or
                type(obj).__name__ in ("builtin_function_or_method", "function")):
            continue
        entry = _describe_callable(name, obj, "function")
        if entry:
            out.append(entry)
        if len(out) >= _MAX_FUNCS_PER_MODULE:
            break
    out.sort(key=lambda e: e["name"])
    return out


def _build_class(spec: str) -> list[dict]:
    """Introspect the PUBLIC methods of one class (module:qualname)."""
    try:
        mod_name, qual = spec.split(":", 1)
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, qual)
    except Exception:
        return []
    out = []
    for name in dir(cls):
        if not _is_public(name):
            continue
        try:
            attr = inspect.getattr_static(cls, name)
        except Exception:
            attr = getattr(cls, name, None)
        # Methods/functions/descriptors that are callable. Properties are surfaced
        # as name-only (no signature) so the editor can still suggest them.
        is_callable_method = callable(attr) or inspect.isfunction(attr) or inspect.ismethoddescriptor(attr)
        if not is_callable_method:
            continue
        entry = _describe_callable(name, getattr(cls, name, attr), "method")
        if entry:
            out.append(entry)
        if len(out) >= _MAX_METHODS_PER_CLASS:
            break
    out.sort(key=lambda e: e["name"])
    return out


def build_catalog() -> dict:
    """Assemble the full catalog by introspecting builtins, modules, and classes.
    Safe to call at build time (libs installed) — missing libs are skipped."""
    import platform
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "builtins": _build_builtins(),
        "modules": {alias: _build_module(mod) for alias, mod in _MODULE_SOURCES.items()},
        "classes": {name: _build_class(spec) for name, spec in _CLASS_SOURCES.items()},
    }


def write_catalog(path: str | None = None) -> str:
    """Build and write catalog.json to disk (build-time entry point). Returns the
    path written. Creates the parent dir if needed."""
    path = path or _CATALOG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    catalog = build_catalog()
    with open(path, "w") as f:
        json.dump(catalog, f, separators=(",", ":"))  # compact — it's machine-read
    # A tiny summary for build logs.
    n_builtins = len(catalog["builtins"])
    n_mod = sum(len(v) for v in catalog["modules"].values())
    n_cls = sum(len(v) for v in catalog["classes"].values())
    print(f"[completion-catalog] wrote {path}: {n_builtins} builtins, "
          f"{n_mod} module fns, {n_cls} class methods")
    return path


# Runtime cache — the baked catalog is read once and reused.
_CATALOG_CACHE: dict | None = None


def load_catalog() -> dict:
    """Read the baked catalog.json (cached). Returns an empty-but-valid catalog if
    the file is missing (e.g. an older image built before this feature) so callers
    never crash — the editor simply gets no static suggestions."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    try:
        with open(_CATALOG_PATH) as f:
            _CATALOG_CACHE = json.load(f)
    except Exception:
        _CATALOG_CACHE = {"builtins": [], "modules": {}, "classes": {}}
    return _CATALOG_CACHE


# =============================================================================
# HELPER-FUNCTION METADATA
# Introspection of app.notebook.helpers into structured autocomplete metadata.
# Lives here (with the catalog builder) so ALL inspect-based introspection is in
# one module. Consumed by GET /helpers (editor autocomplete + Snippets panel).
# The helper set is fixed at build time, so the result is cached after first build.
# =============================================================================

_HELPERS_META_CACHE: list | None = None

# Acronyms that must stay upper-cased in a title-cased category header. str.title()
# would otherwise render "GENERATIVE AI" as "Generative Ai" and "LLM" as "Llm".
# Keep this list in sync with any acronym used in a helper module's section header.
_CATEGORY_ACRONYMS = {"AI", "ML", "LLM", "SQL", "KB", "S3", "ONNX", "API", "ETL", "GPU"}

# Mixed-case brand/product names that must keep their EXACT casing in a title-cased
# header (str.title() would mangle "SAGEMAKER" -> "Sagemaker"). Keyed by the word's
# UPPER form so we can match a SCREAMING header word. Extend as new brands appear in
# a section header (e.g. "DYNAMODB" -> "DynamoDB").
_CATEGORY_MIXED_CASE = {"SAGEMAKER": "SageMaker"}


def _title_with_acronyms(header: str) -> str:
    """Title-case a SCREAMING section header while preserving known acronyms:
    "GENERATIVE AI" -> "Generative AI", "MACHINE LEARNING" -> "Machine Learning",
    "LLM & EMBEDDINGS" -> "LLM & Embeddings". Word-by-word so mixed headers work."""
    out = []
    for word in header.split(" "):
        up = word.upper()
        if up in _CATEGORY_MIXED_CASE:
            out.append(_CATEGORY_MIXED_CASE[up])   # brand casing, e.g. SageMaker
        elif up in _CATEGORY_ACRONYMS:
            out.append(up)                          # acronym, e.g. AI / ML / SQL
        else:
            out.append(word.title())
    return " ".join(out)


def _category_map_from_source(module) -> dict:
    """Map each public function name → its category, by reading the section-header
    comments in helpers.py (lines like '# DATA LOADING — ...' between '# ======'
    banners) and assigning every `def` that follows to the most recent header.
    Falls back to 'Other' for anything before the first header."""
    cats = {}
    try:
        if hasattr(module, "__path__"):
            # A package: its __init__ source has no submodule bodies (and thus none
            # of the section-header comments), so concatenate every submodule's
            # source instead, so the headers that categorize functions are seen.
            sources = []
            for modinfo in pkgutil.iter_modules(module.__path__):
                submod = importlib.import_module(module.__name__ + "." + modinfo.name)
                sources.append(inspect.getsource(submod))
            src = "\n".join(sources)
        else:
            src = inspect.getsource(module)
    except Exception:
        return cats
    current = "Other"
    header_re = re.compile(r"^#\s+([A-Z][A-Za-z0-9 &/]+?)(?:\s+—.*)?$")
    def_re = re.compile(r"^def\s+([a-zA-Z]\w*)\s*\(")

    def _is_banner(s: str) -> bool:
        # A '# =====' rule line: only '#', '=', and spaces, and it has at least a few '='.
        return s.startswith("#") and set(s) <= {"#", "=", " "} and s.count("=") >= 4

    lines = [ln.rstrip() for ln in src.splitlines()]
    for i, stripped in enumerate(lines):
        # A section header is a header-shaped comment that is DIRECTLY ADJACENT to a
        # '# =====' banner (the line above or below it). Requiring the banner is what
        # distinguishes a real section header ("# DATA LOADING — ...", wrapped in
        # banners) from ordinary prose comments that happen to start capitalized and
        # avoid punctuation (e.g. "# IMPORTANT — why NOT match…" inside a function),
        # which previously got mis-read as categories.
        if stripped.startswith("#") and not _is_banner(stripped):
            m = header_re.match(stripped)
            if m:
                prev_banner = i > 0 and _is_banner(lines[i - 1])
                next_banner = i + 1 < len(lines) and _is_banner(lines[i + 1])
                if prev_banner or next_banner:
                    # Title-case the SCREAMING headers (DATA LOADING -> Data Loading),
                    # but keep known ACRONYMS upper-cased — str.title() would mangle
                    # them ("GENERATIVE AI" -> "Generative Ai", "LLM" -> "Llm"). So we
                    # title-case then restore any word that is a known acronym.
                    current = _title_with_acronyms(m.group(1).strip())
                    continue
        m = def_re.match(stripped)
        if m:
            cats[m.group(1)] = current
    return cats


def build_helpers_meta() -> list[dict]:
    """Introspect the helpers module into structured autocomplete metadata. Each
    entry: { name, category, signature ("name(args)"), params[], returns, summary,
    doc }. Only public functions actually defined in helpers.py are included."""
    from . import helpers as helpers_mod
    cat_map = _category_map_from_source(helpers_mod)
    out = []
    for name, fn in inspect.getmembers(helpers_mod, inspect.isfunction):
        # Public helpers only, and only those actually DEFINED in this module
        # (skip imported names like pd/px pulled in via `inspect.getmembers`).
        if name.startswith("_"):
            continue
        _mod = getattr(fn, "__module__", "") or ""
        if _mod != helpers_mod.__name__ and not _mod.startswith(helpers_mod.__name__ + "."):
            continue
        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            continue
        params = []
        for p in sig.parameters.values():
            if p.name == "self":
                continue
            kind_prefix = "**" if p.kind == p.VAR_KEYWORD else ("*" if p.kind == p.VAR_POSITIONAL else "")
            has_default = p.default is not inspect.Parameter.empty
            params.append({
                "name": kind_prefix + p.name,
                "type": _annotation_str(p.annotation),
                "default": repr(p.default) if has_default else None,
                "optional": has_default or kind_prefix != "",
                # Keyword-only (after a bare * in the signature): the caller MUST pass
                # it by name. Surfaced so autocomplete inserts `name=` and signature
                # help can flag it, preventing "takes 0 positional arguments" errors.
                "kw_only": p.kind == p.KEYWORD_ONLY,
            })
        doc = inspect.getdoc(fn) or ""
        summary = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
        out.append({
            "name": name,
            "category": cat_map.get(name, "Other"),
            "signature": f"{name}{sig}",
            "params": params,
            "returns": _annotation_str(sig.return_annotation),
            "summary": summary,
            "doc": doc,
        })
    out.sort(key=lambda h: h["name"])
    return out


def load_helpers() -> list[dict]:
    """Cached helper metadata (built once — the helper set is fixed at runtime).
    Returns [] on any introspection failure so the endpoint never crashes."""
    global _HELPERS_META_CACHE
    if _HELPERS_META_CACHE is None:
        try:
            _HELPERS_META_CACHE = build_helpers_meta()
        except Exception:
            return []
    return _HELPERS_META_CACHE


if __name__ == "__main__":
    # Build-time invocation: `python -m app.notebook.completion_catalog`
    write_catalog()
