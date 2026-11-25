"""
Read-only variable inspector for the AI agents (live DataFrame schemas).
Part of: app.notebook (application layer)

Backs GET /variables/schema, POST /variables/inspect and the secret redaction of
GET /variables/redacted. The chat agent uses these so it writes code against a
variable's REAL columns (e.g. `product_id` after a cell renamed `productId`)
instead of guessing from the raw data-source catalog.

Safety rules (the inspector runs on arbitrary user objects):
  • Tabular types are recognized by EXACT class (module + class name): a pandas
    DataFrame/Series or a numpy ndarray. A user subclass is kind "other".
  • repr()/str()/len()/getattr() are NEVER called on an arbitrary user object —
    only on pandas/numpy containers, their dtypes, and allowlisted scalar types.
  • Sample values come only from head(n<=3); values in columns whose NAME looks
    sensitive are omitted; any value equal to / containing a configured SECRET's
    value is redacted. Secret values are never logged or returned.
  • Every response is size-bounded (schema ~48 KB / 200 entries, inspect 4096 B).
pandas/numpy are imported lazily so this module imports without them.
"""

import datetime as _dt
import decimal
import json
import os
import re

SENSITIVE_COL_RE = re.compile(r"pass|secret|token|key|cred|ssn|email|phone", re.IGNORECASE)

MAX_SCHEMA_COLS = 60          # columns per entry ("more" carries the rest)
MAX_SCHEMA_ENTRIES = 200      # entries in GET /variables/schema
MAX_SCHEMA_BYTES = 48 * 1024  # serialized size bound of GET /variables/schema
MAX_INSPECT_BYTES = 4096      # serialized size bound of POST /variables/inspect
MAX_SAMPLE_ROWS = 3
VALUE_MAX_CHARS = 60          # per sample value
LABEL_MAX_CHARS = 60          # per column label / dtype string
NAME_MAX_CHARS = 128
# Values longer than this are cut BEFORE redaction (bounds the redaction cost);
# only the first VALUE_MAX_CHARS survive anyway.
_REDACT_WINDOW = 4096

REDACTED = "<redacted>"
OMITTED = "<omitted>"

# Same skip rules as SandboxExecutor.get_variables().
_SKIP_TYPE_NAMES = frozenset({"module", "function", "builtin_function_or_method", "type"})
# len() is computed only for these EXACT builtin types (never a user class).
_LEN_TYPES = (list, tuple, dict, set, frozenset, str, bytes)
# Sample values are rendered only for these EXACT types (plus pandas/numpy scalars).
_SCALAR_TYPES = (str, int, float, bool, type(None), decimal.Decimal,
                 _dt.date, _dt.datetime, _dt.time, _dt.timedelta)
_PANDAS_SCALARS = frozenset({"Timestamp", "Timedelta", "NaTType"})
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── type helpers (no user code runs here) ─────────────────────────────────────

def _type_info(obj) -> tuple[str, str]:
    """(module, class name) of obj's EXACT type. Class-level lookups only, so an
    instance's __getattr__/__getattribute__ is never invoked."""
    t = type(obj)
    try:
        mod = t.__module__
        name = t.__name__
    except Exception:
        return "", "object"
    if type(mod) is not str:
        mod = ""
    if type(name) is not str:
        name = "object"
    return mod, name


def classify(obj) -> str:
    """'dataframe' | 'series' | 'ndarray' | 'other' — by exact class."""
    mod, name = _type_info(obj)
    if mod.startswith("pandas") and name == "DataFrame":
        return "dataframe"
    if mod.startswith("pandas") and name == "Series":
        return "series"
    if mod == "numpy" and name == "ndarray":
        return "ndarray"
    return "other"


def _exact(t, types) -> bool:
    """`t` is one of `types` by IDENTITY (`in` would call a metaclass __eq__)."""
    return any(t is x for x in types)


def _is_numpy_scalar(v) -> bool:
    mod, _ = _type_info(v)
    if mod != "numpy":
        return False
    try:
        import numpy as np
        return isinstance(v, np.generic)
    except Exception:
        return False


def _clean(s: str, limit: int) -> str:
    """Control chars → spaces, then cut to `limit` chars."""
    s = _CTRL_RE.sub(" ", s)
    return s if len(s) <= limit else s[:limit]


def _scalar_label(label) -> str:
    """A non-tuple label as a raw string (user objects → '<TypeName>')."""
    try:
        if type(label) is str:
            return label
        if _exact(type(label), (int, float, bool, type(None))):
            return repr(label)
        if _is_numpy_scalar(label):
            return repr(label.item())
    except Exception:
        pass
    return f"<{_type_info(label)[1]}>"


def _label(label) -> str:
    """A column label as a short, safe string (user objects → '<TypeName>').
    An EXACT tuple (MultiIndex column) renders as "('contact', 'email')" from its
    members, each rendered the same safe way (str members quoted)."""
    if type(label) is tuple:
        parts = []
        for m in label[:8]:
            s = _scalar_label(m)
            parts.append(repr(s) if type(m) is str else s)
        if len(label) > 8:
            parts.append("…")
        s = "(" + ", ".join(parts) + ("," if len(label) == 1 else "") + ")"
    else:
        s = _scalar_label(label)
    return _clean(s, LABEL_MAX_CHARS)


def _label_sensitive(label) -> bool:
    """Does a RAW column label look sensitive? Decided from the raw label, not the
    rendered one, so a MultiIndex tuple like ('contact', 'email') is caught: each
    member of an exact tuple is tested (no user code runs)."""
    if type(label) is tuple:
        return any(SENSITIVE_COL_RE.search(_scalar_label(m)) for m in label)
    return bool(SENSITIVE_COL_RE.search(_scalar_label(label)))


def _render_value(v, secrets: set) -> str:
    """A sample cell value as a short string. Only allowlisted scalar types are
    stringified; anything else is '<TypeName>'. Redacted BEFORE the cut so a
    secret is never partially exposed."""
    t = type(v)
    try:
        if t is str:
            s = v
        elif _exact(t, _SCALAR_TYPES):
            s = str(v)
        else:
            mod, name = _type_info(v)
            if (mod.startswith("pandas") and name in _PANDAS_SCALARS) or _is_numpy_scalar(v):
                s = str(v)
            else:
                return f"<{name}>"
    except Exception:
        return f"<{_type_info(v)[1]}>"
    s = s[:_REDACT_WINDOW]
    s = redact_text(s, secrets)
    return _clean(s, VALUE_MAX_CHARS)


# ── secrets ───────────────────────────────────────────────────────────────────

def _key_sources() -> dict | None:
    try:
        from .helpers.env import _user_key_sources
        return _user_key_sources()
    except Exception:
        # helpers not importable (stripped test env) — same parse, inline.
        raw = (os.environ.get("MICROVM_USER_ENV_KEYS") or "").strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
            return {str(k): str(v) for k, v in obj.items()} if isinstance(obj, dict) else None
        except Exception:
            return None


def _json_str_leaves(obj, out: set, depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, str):
        if len(obj) >= 4:
            out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _json_str_leaves(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _json_str_leaves(v, out, depth + 1)


def secret_values() -> set:
    """The VALUES of the configured secrets (MICROVM_USER_ENV_KEYS keys whose source
    is 'secret', read from os.environ), plus every str leaf of a JSON secret and the
    repr-escaped form of each. Empty when no source map exists (older image).
    Never log or return these."""
    out: set = set()
    try:
        sources = _key_sources() or {}
        for k, src in sources.items():
            if src != "secret":
                continue
            v = os.environ.get(k)
            if not v or len(v) < 4:
                continue
            out.add(v)
            if v.lstrip()[:1] in ("{", "["):
                try:
                    _json_str_leaves(json.loads(v), out)
                except Exception:
                    pass
        # repr() escapes quotes/backslashes/newlines — redact that form too.
        for v in list(out):
            esc = repr(v)[1:-1]
            if esc != v:
                out.add(esc)
    except Exception:
        return set()
    return out


def redact_text(s, secrets) -> str:
    """Return s with secret values masked: an exact match → '<redacted>'; every
    occurrence of a secret of len >= 6 → '<redacted>'; and a trailing PREFIX of a
    secret (a value cut mid-secret, e.g. repr()[:100] + '...') → '<redacted>'."""
    if not secrets or not isinstance(s, str) or not s:
        return s
    if s in secrets:
        return REDACTED
    for sec in sorted(secrets, key=len, reverse=True):
        if len(sec) >= 6 and sec in s:
            s = s.replace(sec, REDACTED)
    # A value truncated in the middle of a secret leaves only its prefix behind.
    base, tail = (s[:-3], "...") if s.endswith("...") else (s, "")
    for sec in secrets:
        for k in range(min(len(sec) - 1, len(base)), 3, -1):
            if base.endswith(sec[:k]):
                base = base[:-k] + REDACTED
                break
    return base + tail


# ── entries ───────────────────────────────────────────────────────────────────

def validate_name(name) -> bool:
    return (type(name) is str and 0 < len(name) <= NAME_MAX_CHARS
            and name.isidentifier() and not name.startswith("_"))


def is_user_variable(name, obj) -> bool:
    """Same skip rules as get_variables(): no dunders/_private, modules, functions,
    builtins or classes."""
    if type(name) is not str or name.startswith("_"):
        return False
    return _type_info(obj)[1] not in _SKIP_TYPE_NAMES


def classify_origin(name, prov, cell_ids) -> tuple:
    """(orphan, reason) for a user variable: is there a CELL that produces it?

    prov: its runtime provenance entry; cell_ids: the set of cell ids in the
    notebook's DAG document, or None when unknown (then "deleted cell" is never
    reported). Reasons: "no cell" (written outside any cell: a no-cell /execute,
    /set-variable without a cell id, a UI control or an API call; also legacy
    entries with no writer marker), "agent" (same, but the entry's explicit
    `writer` marker says an AI agent wrote it), "internal" (same, `_`-prefixed
    name: a system probe leftover), "deleted cell" (its cell is not in the
    notebook), "unknown" (no provenance at all)."""
    if not isinstance(prov, dict):
        return True, "unknown"
    defined_by = prov.get("defined_by")
    if defined_by == "":
        if isinstance(name, str) and name.startswith("_"):
            return True, "internal"
        return True, ("agent" if prov.get("writer") == "agent" else "no cell")
    if defined_by is None:
        return True, "unknown"
    if cell_ids is not None and str(defined_by) not in cell_ids:
        return True, "deleted cell"
    return False, None


def schema_entry(name: str, obj, prov: dict | None, origin: tuple | None = None) -> dict:
    """origin: (orphan, reason) from classify_origin; computed with unknown cell
    ids when omitted. An orphan reports defined_by null."""
    kind = classify(obj)
    _, type_name = _type_info(obj)
    orphan, reason = origin if origin is not None else classify_origin(name, prov, None)
    prov = prov if isinstance(prov, dict) else {}
    entry = {
        "name": name, "type": type_name, "kind": kind,
        "shape": None, "columns": [], "more": 0, "dtype": None, "len": None,
        "defined_by": None if orphan else prov.get("defined_by"),
        "defined_at": prov.get("defined_at"),
        "last_exec": prov.get("last_exec"),
        "orphan": bool(orphan),
        "orphan_reason": reason,
    }
    try:
        if kind == "dataframe":
            entry["shape"] = [int(obj.shape[0]), int(obj.shape[1])]
            cols = obj.columns
            n = len(cols)
            dtypes = obj.dtypes
            entry["columns"] = [[_label(cols[i]), _clean(str(dtypes.iloc[i]), LABEL_MAX_CHARS)]
                                for i in range(min(n, MAX_SCHEMA_COLS))]
            entry["more"] = max(0, n - MAX_SCHEMA_COLS)
        elif kind == "series":
            entry["shape"] = [int(obj.shape[0])]
            dtype = _clean(str(obj.dtype), LABEL_MAX_CHARS)
            sname = obj.name
            entry["dtype"] = dtype
            entry["columns"] = [["value" if sname is None else _label(sname), dtype]]
        elif kind == "ndarray":
            entry["shape"] = [int(x) for x in obj.shape]
            entry["dtype"] = _clean(str(obj.dtype), LABEL_MAX_CHARS)
        elif _exact(type(obj), _LEN_TYPES):
            entry["len"] = len(obj)
    except Exception:
        pass  # leave the safe defaults (type/kind/provenance still useful)
    return entry


def _sample(obj, kind: str, entry: dict, n: int, secrets: set) -> tuple[list, list]:
    """(sample, omitted_columns) — at most n rows, values rendered safely."""
    sample: list = []
    omitted: list = []
    if n <= 0:
        return sample, omitted
    if kind == "dataframe":
        cols = obj.columns
        # Row keys by column POSITION: a duplicate rendered label gets '#<pos>'
        # so no column's value is silently collapsed into another's.
        keys: list = []
        seen: set = set()
        for j, c in enumerate(entry["columns"]):
            k = c[0] if c[0] not in seen else f"{c[0]}#{j}"
            seen.add(k)
            keys.append(k)
        # Sensitivity from the RAW label at each position (MultiIndex-safe).
        sensitive = [_label_sensitive(cols[j]) for j in range(len(keys))]
        omitted = [k for k, s in zip(keys, sensitive) if s]
        head = obj.head(n)
        rows = int(head.shape[0])
        for r in range(rows):
            row = {}
            for j, k in enumerate(keys):
                row[k] = OMITTED if sensitive[j] else _render_value(head.iloc[r, j], secrets)
            sample.append(row)
    elif kind == "series":
        lb = entry["columns"][0][0] if entry["columns"] else "value"
        if obj.name is not None and _label_sensitive(obj.name):
            omitted = [lb]
        head = obj.head(n)
        for r in range(int(head.shape[0])):
            sample.append(OMITTED if omitted else _render_value(head.iloc[r], secrets))
    elif kind == "ndarray" and len(obj.shape) == 1:
        for r in range(min(n, int(obj.shape[0]))):
            sample.append(_render_value(obj[r], secrets))
    return sample, omitted


def _size(body: dict) -> int:
    return len(json.dumps(body, default=str))


def inspect_entry(name: str, obj, prov: dict | None, sample_rows, secrets: set,
                  origin: tuple | None = None) -> dict:
    """schema_entry + up to 3 safe sample rows, bounded to MAX_INSPECT_BYTES."""
    try:
        n = int(sample_rows)
    except Exception:
        n = MAX_SAMPLE_ROWS
    n = max(0, min(MAX_SAMPLE_ROWS, n))
    entry = schema_entry(name, obj, prov, origin)
    try:
        sample, omitted = _sample(obj, entry["kind"], entry, n, secrets)
    except Exception:
        sample, omitted = [], []
    body = dict(entry, sample=sample, omitted_columns=omitted, truncated=False)
    # Trim samples first, then columns, until the body fits.
    while _size(body) > MAX_INSPECT_BYTES:
        body["truncated"] = True
        if body["sample"]:
            body["sample"].pop()
        elif body["omitted_columns"] and len(body["omitted_columns"]) > len(body["columns"]):
            body["omitted_columns"].pop()
        elif body["columns"]:
            body["columns"].pop()
            body["more"] = int(body["more"] or 0) + 1
            kept = {c[0] for c in body["columns"]}
            ncols = len(body["columns"])
            body["omitted_columns"] = [
                c for c in body["omitted_columns"]
                if c in kept or (c.rsplit("#", 1)[0] in kept
                                 and c.rsplit("#", 1)[-1].isdigit()
                                 and int(c.rsplit("#", 1)[-1]) < ncols)]
        else:
            break
    return body


def _recency(item) -> tuple:
    prov = item[2] or {}
    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else -1
    return (num(prov.get("last_exec")), num(prov.get("defined_at")))


def build_schema(namespace_items, provenance, origin_of=None) -> dict:
    """{"variables": [entry…], "count": n, "truncated": bool} for every user variable,
    most recently executed first; at most MAX_SCHEMA_ENTRIES entries and about
    MAX_SCHEMA_BYTES serialized. origin_of(name, prov) → (orphan, reason); default
    classify_origin with unknown cell ids. The orphan fields are part of each
    entry before its size is counted."""
    provenance = provenance if isinstance(provenance, dict) else {}
    items = []
    for name, obj in namespace_items:
        if not is_user_variable(name, obj):
            continue
        items.append((name, obj, provenance.get(name)))
    # Stable sort: equal recency keeps namespace (insertion) order.
    items.sort(key=_recency, reverse=True)
    out, size, truncated = [], 0, False
    for name, obj, prov in items:
        if len(out) >= MAX_SCHEMA_ENTRIES:
            truncated = True
            break
        origin = origin_of(name, prov) if origin_of is not None else None
        entry = schema_entry(name, obj, prov, origin)
        esize = _size(entry) + 2
        if size + esize > MAX_SCHEMA_BYTES:
            truncated = True
            break
        out.append(entry)
        size += esize
    return {"variables": out, "count": len(out), "truncated": truncated}
