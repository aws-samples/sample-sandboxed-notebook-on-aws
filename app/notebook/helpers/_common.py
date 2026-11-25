"""Shared internals for the helpers package: logging decorators + env-sourced constants."""

import os
import time
import logging
import functools

import pandas as pd

logger = logging.getLogger(__name__)


def _log_read(source_kind):
    """Decorator: log one line after a data-reader helper returns.

    Wraps a reader (read_s3_csv, read_athena, read_dynamodb, …) so that after it
    computes its result we emit a single greppable line with the source, row×col
    shape, approx in-memory size, and elapsed ms — the reads are where most real
    latency/cost lives, and they were previously invisible in the VM log. Purely
    observational: it never changes the return value, and a logging failure can
    never break the read (best-effort in a try). On error it logs the failure +
    elapsed and re-raises so the user still sees the original exception.

    `source_kind` is a short tag ("s3", "athena", "dynamodb", "aurora", "local",
    "url", "bedrock_kb"). A best-effort target string is derived for context —
    but it is ONLY a LOCATION/IDENTIFIER (bucket/key, table, KB id, path/URL),
    NEVER user CONTENT. We deliberately do NOT log SQL text or a KB's natural-
    language query: those are user data, and logs should carry key info, not the
    payload. So athena/aurora/bedrock_kb readers log no free-form query here.

    Lineage: before calling the reader it also records the source IDENTIFIER(s)
    into the per-cell read_capture (surfaced as `sources_read` on /execute): s3 →
    s3://bucket/key, local → path, url → scheme://host/path (query, fragment and
    userinfo dropped), dynamodb → table, bedrock_kb → KB id, cloudwatch → log
    group(s), athena/aurora → the qualified FROM/JOIN table names parsed from the
    SQL (never the SQL itself). Recording is best-effort and never breaks a read.
    """
    def _target_str(args, kwargs):
        try:
            if source_kind in ("athena", "aurora"):
                # The only arg is SQL (user data) — do NOT log it. The source_kind
                # tag + shape + elapsed already identify the read.
                return ""
            if source_kind == "bedrock_kb":
                # arg0 is the natural-language query (user data) — never log it.
                # Log the KB IDENTIFIER instead: explicit kb_id if passed, else the
                # configured BEDROCK_KB_ID. (kb_id is the 2nd positional / a kwarg.)
                kb = kwargs.get("kb_id")
                if not kb and len(args) >= 2:
                    kb = args[1]
                kb = kb or os.environ.get("BEDROCK_KB_ID", "")
                return str(kb) if kb else ""
            if source_kind == "cloudwatch":
                # arg0 is the Insights query / grep pattern (user data) — do NOT log
                # it. Log the LOG GROUP instead (a location/identifier): it's the 2nd
                # positional for read_logs/grep_logs/tail_logs, or a `log_group` kwarg.
                lg = kwargs.get("log_group")
                if not lg and len(args) >= 2:
                    lg = args[1]
                if isinstance(lg, (list, tuple)):
                    lg = ", ".join(str(x) for x in lg)
                return str(lg)[:120] if lg else ""
            if not args:
                return ""
            a0 = args[0]
            if source_kind == "s3":
                # (bucket, key) or a single s3:// URI / path — a location, not data.
                if len(args) >= 2 and args[1]:
                    return f"{a0}/{args[1]}"
                return str(a0)
            # dynamodb (table name), local (path), url (URL) — identifiers/locations.
            return str(a0)[:120]
        except Exception:
            return ""

    def _sql_table_ids(sql, kwargs, args):
        # athena/aurora: arg0 is SQL (user data) — we NEVER record it. Only the
        # FROM/JOIN table identifiers parsed out of it, qualified to a stable id.
        if not isinstance(sql, str):
            return []
        from ..sql_engine import table_refs_in_sql  # lazy: avoid import cycle
        db = kwargs.get("database")
        if not db and len(args) >= 2 and isinstance(args[1], str):
            db = args[1]
        ids = []
        for ref in table_refs_in_sql(sql):
            parts = ref.split(".")
            if source_kind == "athena":
                if len(parts) == 1:
                    adb = db or os.environ.get("ATHENA_DB", _ATHENA_DB)
                    ids.append(f"{adb}.{parts[0]}")
                elif len(parts) == 2:
                    ids.append(ref)
                else:  # catalog.db.table → db.table
                    ids.append(".".join(parts[-2:]))
            else:  # aurora
                adb = db or os.environ.get("AURORA_DATA_API_DATABASE") or "microvm_db"
                if len(parts) == 3:
                    ids.append(ref)
                elif len(parts) == 2:
                    ids.append(f"{adb}.{parts[0]}.{parts[1]}")
                else:
                    ids.append(f"{adb}.public.{parts[0]}")
        return ids

    def _source_ids(args, kwargs):
        """Best-effort source IDENTIFIERS for the lineage capture (read_capture).
        Same privacy rule as _target_str: locations/names only, never SQL text,
        KB queries, URL query strings or data. Never raises (→ [])."""
        try:
            if source_kind in ("athena", "aurora"):
                return _sql_table_ids(args[0] if args else None, kwargs, args)
            if source_kind == "bedrock_kb":
                kb = kwargs.get("kb_id")
                if not kb and len(args) >= 2:
                    kb = args[1]
                kb = kb or os.environ.get("BEDROCK_KB_ID", "")
                return [str(kb)] if kb else []
            if source_kind == "cloudwatch":
                lg = kwargs.get("log_group")
                if not lg and len(args) >= 2:
                    lg = args[1]
                if not lg:
                    return []
                if isinstance(lg, (list, tuple)):
                    return [str(x) for x in lg if x]
                return [str(lg)]
            if source_kind == "s3":
                a0 = args[0] if args else kwargs.get("bucket")
                if not a0:
                    return []
                a0 = str(a0)
                key = args[1] if len(args) >= 2 else (kwargs.get("key") or kwargs.get("prefix"))
                if a0.startswith("s3://"):
                    if key:
                        return [f"{a0.rstrip('/')}/{str(key).lstrip('/')}"]
                    return [a0]
                if key:
                    return [f"s3://{a0}/{str(key).lstrip('/')}"]
                return [f"s3://{a0}"]
            if source_kind == "dynamodb":
                t = args[0] if args else kwargs.get("table_name")
                return [str(t)] if t else []
            if source_kind == "url":
                from urllib.parse import urlsplit
                u = args[0] if args else kwargs.get("url")
                if not u:
                    return []
                p = urlsplit(str(u))
                host = p.hostname or ""
                if p.port:
                    host = f"{host}:{p.port}"
                if not p.scheme or not host:
                    return []
                return [f"{p.scheme}://{host}{p.path}"]
            if source_kind == "local":
                path = args[0] if args else kwargs.get("path")
                return [str(path)] if path else []
            return []
        except Exception:
            return []

    def _decorator(func):
        @functools.wraps(func)
        def _wrapper(*args, **kwargs):
            start = time.perf_counter()
            target = _target_str(args, kwargs)
            # Lineage: record the source identifier(s) BEFORE the read, so a
            # failing read is still attributed. No-op outside a cell capture.
            _scope = None
            try:
                from .. import read_capture
                for _ident in _source_ids(args, kwargs):
                    read_capture.record(source_kind, _ident)
                # Suppress the raw-HTTP hooks (http_capture) while the helper runs,
                # so e.g. read_url's internal requests.get isn't recorded twice.
                _scope = read_capture.helper_scope()
            except Exception:
                _scope = None
            try:
                if _scope is None:
                    result = func(*args, **kwargs)
                else:
                    with _scope:
                        result = func(*args, **kwargs)
            except Exception as e:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.warning(
                    f"  ✗ READ {source_kind} FAILED ({elapsed_ms:.0f}ms) "
                    f"{target} — {type(e).__name__}: {e}"
                )
                raise
            elapsed_ms = (time.perf_counter() - start) * 1000
            try:
                shape = ""
                size = ""
                if isinstance(result, pd.DataFrame):
                    shape = f"{result.shape[0]}×{result.shape[1]}"
                    try:
                        kb = result.memory_usage(deep=True).sum() / 1024
                        size = f" {kb:.1f}KB" if kb < 1024 else f" {kb/1024:.1f}MB"
                    except Exception:
                        size = ""
                elif result is None:
                    shape = "None"
                logger.info(
                    f"  📥 READ {source_kind} ({elapsed_ms:.0f}ms) {shape}{size}"
                    + (f" | {target}" if target else "")
                )
            except Exception:
                pass  # logging must never break the read
            return result
        return _wrapper
    return _decorator


def _log_write(sink_kind):
    """Decorator symmetric to _log_read for the WRITE helpers (to_s3_csv/json/
    parquet, to_local). Data EXPORTS were previously invisible in the VM log while
    reads were fully logged — and a write that fails INSIDE a cell is caught by the
    executor and only shown as cell output, never reaching the app-level error
    handler, so failures were dark in CloudWatch too. This logs the destination,
    the source DataFrame's row×col shape, and elapsed ms on success, and the
    failure + elapsed on error (then re-raises). Best-effort; never alters the
    return value or masks the original exception.

    The DataFrame is arg 0 for these helpers (to_s3_csv(df, bucket, key, ...)); the
    destination string is derived from the remaining args and, when the helper
    returns an s3:// URI, that returned URI is preferred as the target.
    """
    def _dest_str(args, kwargs, result):
        try:
            if isinstance(result, str) and result:
                return result  # helpers return the s3:// URI / path they wrote
            # args after the df: (bucket, key) or (path,)
            rest = [str(a) for a in args[1:] if a is not None]
            return "/".join(rest) if rest else ""
        except Exception:
            return ""

    def _decorator(func):
        @functools.wraps(func)
        def _wrapper(*args, **kwargs):
            start = time.perf_counter()
            shape = ""
            try:
                df0 = args[0] if args else None
                if isinstance(df0, pd.DataFrame):
                    shape = f"{df0.shape[0]}×{df0.shape[1]}"
            except Exception:
                shape = ""
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                elapsed_ms = (time.perf_counter() - start) * 1000
                dest = _dest_str(args, kwargs, None)
                logger.warning(
                    f"  ✗ WRITE {sink_kind} FAILED ({elapsed_ms:.0f}ms) {shape} "
                    f"{dest} — {type(e).__name__}: {e}"
                )
                raise
            elapsed_ms = (time.perf_counter() - start) * 1000
            try:
                dest = _dest_str(args, kwargs, result)
                logger.info(
                    f"  📤 WRITE {sink_kind} ({elapsed_ms:.0f}ms) {shape}"
                    + (f" → {dest}" if dest else "")
                )
            except Exception:
                pass  # logging must never break the write
            return result
        return _wrapper
    return _decorator

# Defaults sourced from the execution environment (set at VM launch / config.sh).
# Fallbacks match the project defaults so nothing breaks if a var is unset.
_REGION = os.environ.get("AWS_REGION", "us-west-2")
_ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "microvm-demo")
_ATHENA_DB = os.environ.get("ATHENA_DB", "microvm_demo_db")

# Aurora RDS Data API config, injected at VM launch (see microvm_manager
# .launch_env_vars). Read HERE at import is fine for defaults, but read_aurora
# re-reads os.environ at CALL time so a value injected by the /run hook AFTER the
# app is imported still takes effect. Blank cluster ARN ⇒ Aurora not configured.
_AURORA_CLUSTER_ARN = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
_AURORA_SECRET_ARN = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
_AURORA_DB = os.environ.get("AURORA_DATA_API_DATABASE", "microvm_db")

# Bedrock Knowledge Base config, injected at VM launch (blank ⇒ no KB configured).
# read_bedrock_kb/ask_bedrock_kb re-read os.environ at CALL time so a value injected
# by the /run hook after import still takes effect (same pattern as read_aurora).
_BEDROCK_KB_ID = os.environ.get("BEDROCK_KB_ID", "")


# =============================================================================
# UTILITIES — in-notebook helper catalog (discoverability)
# =============================================================================

def helpers(query=None):
    """
    Browse the pre-loaded helper functions from inside a cell — the in-notebook
    reference for everything that's injected into your namespace (read_*, plot_*,
    invoke_llm, train_model, …). Same catalog the Snippets panel + autocomplete use.

        helpers()                     # every helper, grouped by category
        helpers("Machine Learning")   # just one category (case-insensitive, partial)
        helpers("train")              # fuzzy: any helper/category matching "train"
        helpers(train_model)          # full signature + docstring for one function

    With no argument (or a category/text filter) it returns a DataFrame — category,
    name, signature, summary — that renders as a table in the cell. Passing a
    function (or its exact name) prints that function's full signature and docstring.
    """
    # Reuse the SAME introspection the /helpers endpoint + autocomplete use, so this
    # never drifts from what's actually injected. Import lazily to avoid a heavy /
    # circular import at module load (completion_catalog imports helpers).
    try:
        from ..completion_catalog import build_helpers_meta
        meta = build_helpers_meta()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("helpers(): could not build catalog (%s)", e)
        return pd.DataFrame(columns=["category", "name", "signature", "summary"])

    # helpers(train_model) or helpers("exact_name") → detailed single-function view.
    fn_name = None
    if callable(query) and getattr(query, "__name__", None):
        fn_name = query.__name__
    elif isinstance(query, str):
        exact = [m for m in meta if m["name"] == query]
        if exact:
            fn_name = query

    if fn_name:
        m = next((m for m in meta if m["name"] == fn_name), None)
        if not m:
            print(f"No helper named {fn_name!r}. Try helpers() to list them all.")
            return None
        print(f"{m['signature']}    [{m.get('category', 'Other')}]")
        doc = (m.get("doc") or m.get("summary") or "").strip()
        if doc:
            print()
            print(doc)
        return None

    rows = [
        {
            "category": m.get("category", "Other"),
            "name": m["name"],
            "signature": m.get("signature", m["name"] + "(…)"),
            "summary": m.get("summary", ""),
        }
        for m in meta
    ]

    # A text/category filter matches EITHER the category or the name (case-insensitive
    # substring), so helpers("ml"), helpers("Machine"), helpers("train") all work.
    if isinstance(query, str) and query.strip():
        q = query.strip().lower()
        rows = [r for r in rows
                if q in r["category"].lower() or q in r["name"].lower()]
        if not rows:
            print(f"No helpers match {query!r}. Try helpers() to see every category.")
            return pd.DataFrame(columns=["category", "name", "signature", "summary"])

    df = pd.DataFrame(rows, columns=["category", "name", "signature", "summary"])
    # Stable, readable order: by category then name.
    return df.sort_values(["category", "name"]).reset_index(drop=True)
