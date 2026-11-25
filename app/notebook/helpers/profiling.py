"""UTILITIES — data profiling and session/identity/S3 introspection helpers."""

import os

import boto3
import pandas as pd

from ._common import _REGION
from ..aws_clients import aws_region as _aws_region
from .. import aws_clients as _aws
from . import _s3
from .readers import read_s3_csv
from .aurora import read_aurora
from .cost import estimate_session_cost, _default_memory_mib


# =============================================================================
# UTILITIES
# =============================================================================

def profile(df: pd.DataFrame) -> pd.DataFrame:
    """
    Quick data profiling: types, nulls, unique values, and basic stats.
    
    Example:
        profile(df)
    """
    stats = pd.DataFrame({
        'dtype': df.dtypes,
        'non_null': df.count(),
        'null_count': df.isnull().sum(),
        'null_pct': (df.isnull().sum() / len(df) * 100).round(1),
        'unique': df.nunique(),
        'sample': df.iloc[0] if len(df) > 0 else None,
    })
    
    print(f"Shape: {df.shape[0]} rows x {df.shape[1]} columns")
    print(f"Memory: {df.memory_usage(deep=True).sum() / 1024:.1f} KB")
    print()
    return stats


def whoami() -> dict:
    """
    Show current AWS identity: account, region, role, and artifacts bucket.
    
    Example:
        whoami()
    """
    sts = _aws.aws_client('sts', _aws_region())
    identity = sts.get_caller_identity()
    
    account = identity['Account']
    arn = identity['Arn']
    role = arn.split('/')[-2] if '/role/' in arn else arn.split('/')[-1]
    bucket = f"microvm-sandbox-artifacts-{account}-{_aws_region()}"
    
    info = {
        'account': account,
        'region': _aws_region(),
        'role': role,
        'arn': arn,
        'bucket': bucket,
    }
    
    print(f"  Account:  {account}")
    print(f"  Region:   {_aws_region()}")
    print(f"  Role:     {role}")
    print(f"  Bucket:   {bucket}")
    print(f"  ARN:      {arn}")
    
    return info


def this_notebook(session_id: str = None) -> dict:
    """
    Describe a notebook session: its notebook name + id, MicroVM id, persistence
    mode, and (when Aurora is configured) the session's usage and a cost estimate.

    Two modes:

    • this_notebook()               → THIS session (the notebook this cell runs in).
      Identity comes from the values injected at VM launch (the /run hook publishes
      them into the environment), so there's nothing to copy/paste. Live fields
      (microvm_id, persistence_mode, started_at) reflect the current VM.

    • this_notebook(session_id="…")  → ANY session, by id. Identity + usage are
      looked up from the curated `v_nb_session_cost` view (which carries the
      denormalized notebook name, so it still resolves even after the notebook was
      deleted). Live-only fields (microvm_id, persistence_mode, started_at) are not
      available for an arbitrary past session and come back as None.

    In BOTH modes, when Aurora is reachable the session's running/suspended seconds
    are read from `v_nb_session_cost` and priced with `estimate_session_cost()`.

    Example:
        me = this_notebook()                      # current session
        other = this_notebook("378ad81b-…")       # some other/older session
    """
    # ---- Resolve the base identity depending on the mode ----------------------
    explicit = session_id is not None and str(session_id) != ""

    if explicit:
        # Foreign session: no environment context applies (that describes the
        # CURRENT VM only). Identity is filled in from the view below.
        info = {
            "notebook_name": None,
            "notebook_id": None,
            "session_id": str(session_id),
            "microvm_id": None,          # not resolvable from curated views
            "persistence_mode": None,    # launch-time only (env of the live VM)
            "started_at": None,          # launch-time only
            "vm_count": None,
            "running_secs": None,
            "suspended_secs": None,
            "session_cost_usd": None,
            "scope": "session",
        }
    else:
        notebook_id = os.environ.get("MICROVM_NOTEBOOK_ID") or None
        # Coerce the notebook id back to int (it's the integer PK in the v_nb_*
        # views); leave as-is if a non-numeric value ever slips through.
        if notebook_id is not None:
            try:
                notebook_id = int(notebook_id)
            except (TypeError, ValueError):
                pass
        info = {
            "notebook_name": os.environ.get("MICROVM_NOTEBOOK_NAME") or None,
            "notebook_id": notebook_id,
            "session_id": os.environ.get("MICROVM_SESSION_ID") or None,
            "microvm_id": os.environ.get("MICROVM_ID") or None,
            "persistence_mode": os.environ.get("MICROVM_PERSISTENCE_MODE") or None,
            "started_at": os.environ.get("MICROVM_STARTED_AT") or None,
            "vm_count": None,
            "running_secs": None,
            "suspended_secs": None,
            "session_cost_usd": None,
            "scope": "current",
        }

    # ---- Enrich with usage + cost (and, in session mode, identity) ------------
    # Best-effort: any failure (Aurora off, cluster resuming, no row yet) leaves
    # the enrichment fields as None — the base identity above is always returned.
    #
    # NOTE: we read ONLY curated v_nb_* views (the read-only notebook user is
    # granted those, never the base tables or the internal v_vm_sessions view).
    # v_nb_session_cost now carries notebook_name/notebook_id, so a foreign session
    # still resolves to a human name even after its notebook was deleted.
    #
    # Read the Aurora config from os.environ at CALL time — NOT the module-level
    # _AURORA_* constants. Those are captured at import, which happens during the
    # image BUILD (catalog generation / app warmup) BEFORE the /run hook injects
    # AURORA_DATA_API_* at launch — so the constants are frozen empty and the guard
    # would wrongly skip enrichment. read_aurora() itself already reads env at call
    # time (that's why direct queries work); this guard must match it.
    _aurora_cluster = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
    _aurora_secret = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
    aurora_on = bool(_aurora_cluster and _aurora_secret)
    info["aurora_configured"] = aurora_on
    if aurora_on and info["session_id"]:
        try:
            cost_df = read_aurora(
                "SELECT notebook_id, notebook_name, vm_count, running_secs, "
                "suspended_secs, burst_mb_seconds, memory_mib, architecture "
                "FROM v_nb_session_cost WHERE session_id = :sid",
                parameters=[{"name": "sid", "value": {"stringValue": str(info["session_id"])}}],
            )
            if len(cost_df):
                row = cost_df.iloc[0].to_dict()
                info["vm_count"] = int(row.get("vm_count") or 0)
                info["running_secs"] = row.get("running_secs")
                info["suspended_secs"] = row.get("suspended_secs")
                info["memory_gb"] = (int(row.get("memory_mib") or _default_memory_mib())) / 1024.0
                info["architecture"] = row.get("architecture")
                # In session mode the view IS the identity source. In current mode
                # the env values win, but backfill anything the env didn't have.
                if info.get("notebook_id") is None and row.get("notebook_id") is not None:
                    try:
                        info["notebook_id"] = int(row["notebook_id"])
                    except (TypeError, ValueError):
                        info["notebook_id"] = row["notebook_id"]
                if not info.get("notebook_name") and row.get("notebook_name"):
                    info["notebook_name"] = row["notebook_name"]
                # Price the QUANTITIES (view carries no dollars) with the same model
                # the platform bills on. Only the pricing args go to the helper.
                priceable = {k: row.get(k) for k in
                             ("running_secs", "suspended_secs", "burst_mb_seconds",
                              "memory_mib", "architecture")}
                info["session_cost_usd"] = estimate_session_cost(**priceable)["total_cost_usd"]
        except Exception:
            pass  # analytics enrichment is optional; keep the identity fields

        # Identity fallback. v_nb_session_cost is populated from VM state history and
        # lags v_nb_notebook (a notebook has a row there as soon as it's saved, but
        # its session-cost row only appears once a VM records state — and the local
        # SQLite→Aurora sync adds ~a minute). So a freshly-opened notebook can be
        # pickable yet have no cost row yet, which would leave name/id blank here.
        # Resolve identity from v_nb_notebook by session_id when the cost view came
        # up empty. (Cost stays None — there genuinely isn't any yet.)
        if info.get("notebook_id") is None:
            try:
                nb_df = read_aurora(
                    "SELECT notebook_id, name FROM v_nb_notebook "
                    "WHERE session_id = :sid LIMIT 1",
                    parameters=[{"name": "sid", "value": {"stringValue": str(info["session_id"])}}],
                )
                if len(nb_df):
                    r2 = nb_df.iloc[0].to_dict()
                    if r2.get("notebook_id") is not None:
                        try:
                            info["notebook_id"] = int(r2["notebook_id"])
                        except (TypeError, ValueError):
                            info["notebook_id"] = r2["notebook_id"]
                    if not info.get("notebook_name") and r2.get("name"):
                        info["notebook_name"] = r2["name"]
            except Exception:
                pass  # identity fallback is best-effort too

    # ---- Print a compact summary ----------------------------------------------
    header = "This notebook" if not explicit else "Session"
    print(f"  [{header}]")
    print(f"  Notebook: {info['notebook_name']}" + (f" (id {info['notebook_id']})" if info['notebook_id'] is not None else ""))
    print(f"  Session:  {info['session_id']}")
    if not explicit:
        print(f"  MicroVM:  {info['microvm_id']}")
        print(f"  Persist:  {info['persistence_mode']}")
    if info["vm_count"] is not None:
        print(f"  VMs:      {info['vm_count']} (this session, incl. rotations/restores)")
    if info["session_cost_usd"] is not None:
        print(f"  Cost:     ${info['session_cost_usd']:.4f} (session to date)")
    if info["started_at"]:
        print(f"  Started:  {info['started_at']}")
    # Footer: distinguish "Aurora is off" (feature unavailable) from "Aurora is on
    # but this session has no analytics row yet" (unknown id, or sync not caught up).
    if not info.get("aurora_configured"):
        print("  Analytics (usage/cost, and lookups by session id) require the proxy "
              "to be launched with --AuroraSetup. Identity above still reflects this "
              "notebook; usage/cost is unavailable without Aurora.")
    elif info["vm_count"] is None:
        print("  (no analytics row for this session_id yet — unknown session, or the "
              "SQLite\u2192Aurora sync hasn't caught up; retry in ~a minute.)")

    return info


def sample_data(name: str = None) -> pd.DataFrame:
    """
    Load a built-in sample dataset from S3.
    
    Dynamically discovers available datasets from the S3 samples/ prefix.
    
    Example:
        df = sample_data()       # lists available datasets
        df = sample_data('orders')
    """
    # Discover bucket from account
    try:
        bucket = _aws.artifact_bucket()
    except Exception:
        print("  Could not determine S3 bucket. Check AWS credentials.")
        return None

    # Dynamically discover available sample datasets from S3
    try:
        # All pages (a samples/ prefix can hold more than 1000 keys).
        top_objs, top_prefixes = _s3.list_objects(bucket, 'samples/', delimiter='/',
                                                  region=_aws_region())
        # Collect CSV files at top level and in sub-prefixes
        datasets = {}
        # Top-level CSV files in samples/
        for obj in top_objs:
            key = obj['Key']
            if key.endswith('.csv'):
                fname = key.split('/')[-1]
                dataset_name = fname.replace('.csv', '')
                datasets[dataset_name] = key
        # Sub-prefix folders (e.g., samples/orders/orders.csv)
        for prefix in top_prefixes:  # e.g., samples/orders/
            sub_objs, _ = _s3.list_objects(bucket, prefix, region=_aws_region())
            for obj in sub_objs:
                key = obj['Key']
                if key.endswith('.csv'):
                    fname = key.split('/')[-1]
                    dataset_name = fname.replace('.csv', '')
                    datasets[dataset_name] = key
    except Exception as e:
        print(f"  Could not list sample datasets from S3: {e}")
        return None

    if name is None:
        print(f"Available datasets: {', '.join(sorted(datasets.keys()))}")
        print("Usage: df = sample_data('dataset_name')")
        return None

    if name not in datasets:
        print(f"Dataset '{name}' not found. Available: {', '.join(sorted(datasets.keys()))}")
        return None

    s3_key = datasets[name]
    local_path = f"/tmp/{name}.csv"

    # Try local cache first (faster if already downloaded)
    if os.path.exists(local_path):
        return pd.read_csv(local_path)

    # Download from S3
    try:
        df = read_s3_csv(bucket, s3_key)
        # Cache locally for next time
        df.to_csv(local_path, index=False)
        return df
    except Exception as e:
        print(f"  Warning: Could not load from S3: {e}")

    print(f"  Dataset '{name}' not available.")
    return None


def timer(func):
    """
    Decorator that prints execution time of a function.
    
    Example:
        @timer
        def heavy_computation():
            return sum(range(10**7))
        
        heavy_computation()
        # → heavy_computation took 0.42s
    """
    import functools
    import time as _time
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = _time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = _time.perf_counter() - start
        print(f"  {func.__name__} took {elapsed:.2f}s")
        return result
    return wrapper


def compare_df(df1: pd.DataFrame, df2: pd.DataFrame, name1: str = "df1", name2: str = "df2") -> pd.DataFrame:
    """
    Compare two DataFrames and show differences in shape, columns, and values.
    
    Example:
        compare_df(before, after, "before_cleaning", "after_cleaning")
    """
    print(f"  {'':20s} {name1:>15s}  {name2:>15s}")
    print(f"  {'Rows':20s} {df1.shape[0]:>15,}  {df2.shape[0]:>15,}")
    print(f"  {'Columns':20s} {df1.shape[1]:>15,}  {df2.shape[1]:>15,}")
    print(f"  {'Memory (KB)':20s} {df1.memory_usage(deep=True).sum()/1024:>15.1f}  {df2.memory_usage(deep=True).sum()/1024:>15.1f}")
    print(f"  {'Null cells':20s} {df1.isnull().sum().sum():>15,}  {df2.isnull().sum().sum():>15,}")
    print(f"  {'Duplicated rows':20s} {df1.duplicated().sum():>15,}  {df2.duplicated().sum():>15,}")
    
    # Column differences
    cols1 = set(df1.columns)
    cols2 = set(df2.columns)
    added = cols2 - cols1
    removed = cols1 - cols2
    if added:
        print(f"\n  Added columns: {', '.join(sorted(added))}")
    if removed:
        print(f"\n  Removed columns: {', '.join(sorted(removed))}")
    
    return pd.DataFrame({
        'metric': ['rows', 'columns', 'memory_kb', 'nulls', 'duplicates'],
        name1: [df1.shape[0], df1.shape[1], df1.memory_usage(deep=True).sum()/1024, df1.isnull().sum().sum(), df1.duplicated().sum()],
        name2: [df2.shape[0], df2.shape[1], df2.memory_usage(deep=True).sum()/1024, df2.isnull().sum().sum(), df2.duplicated().sum()],
    })


def list_datasources(source_type: str = None) -> pd.DataFrame:
    """
    List the data sources available to THIS notebook as a DataFrame — the in-cell
    equivalent of the Data Sources side panel ("what can I query right now?").
    Unifies every source the catalog knows about (local /tmp files, S3 objects/
    prefixes, DynamoDB tables, Athena/Glue tables, Aurora tables) into one view, so
    you don't have to call athena_tables / dynamodb_tables / aurora_tables separately.

    Columns: source_type, name (display name), source_id (the id/URI you pass to a
    reader), columns (column count), row_count, size, status.

    source_type: optional filter (e.g. "s3", "athena", "dynamodb", "aurora",
      "local") — case-insensitive, matches the catalog's source_type.

    Example:
        list_datasources()                 # everything
        list_datasources("athena")         # just Athena/Glue tables

    For the exact column schema of ONE source, use the per-source schema helpers
    (athena_schema / aurora_schema / dynamodb_schema) or head_s3 for a file.
    """
    cols = ["source_type", "name", "source_id", "columns", "row_count", "size", "status"]
    try:
        from app.notebook.data_catalog import get_active_catalog
        cat = get_active_catalog()
    except Exception:
        cat = None
    if cat is None:
        print("  No data catalog is active in this runtime.")
        return pd.DataFrame(columns=cols)

    data = cat.get_all() or {}
    entries = data.get("entries", []) or []
    rows = []
    for e in entries:
        rows.append({
            "source_type": e.get("source_type", ""),
            "name": e.get("display_name") or e.get("source_id", ""),
            "source_id": e.get("source_id", ""),
            "columns": len(e.get("columns", []) or []),
            "row_count": e.get("row_count"),
            "size": e.get("size"),
            "status": e.get("status", ""),
        })
    df = pd.DataFrame(rows, columns=cols)
    if source_type:
        st = source_type.strip().lower()
        df = df[df["source_type"].str.lower() == st].reset_index(drop=True)
    if len(df):
        print(f"  {len(df)} data source(s)"
              + (f" of type '{source_type}'" if source_type else "")
              + f" — {data.get('discovered', 0)} discovered, "
              + f"{data.get('pending', 0)} pending, {data.get('errors', 0)} error(s).")
    else:
        print("  No data sources"
              + (f" of type '{source_type}'" if source_type else "")
              + " found for this session.")
    return df.sort_values(["source_type", "name"]).reset_index(drop=True)
