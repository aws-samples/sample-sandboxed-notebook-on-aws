"""DATA LOADING — Read from various sources (local, S3, DynamoDB, Athena, URL)."""

import os
import io
import re
import time

import boto3
import pandas as pd

from ._common import _log_read, logger
from ._common import _REGION, _ATHENA_WORKGROUP, _ATHENA_DB
from ..aws_clients import aws_region as _aws_region
from .. import aws_clients as _aws
from . import _s3
from . import _dynamo
from ._athena import (run_athena as _run_athena, execute_athena as _execute_athena,
                      coerce_athena_dtypes as _coerce_athena_dtypes)


# =============================================================================
# DATA LOADING — Read from various sources
# =============================================================================

@_log_read("local")
def read_local(path: str) -> pd.DataFrame:
    """
    Read a SINGLE local file into a DataFrame. Auto-detects the format from the
    extension and transparently handles compressed files.

    Supports: .csv, .tsv, .parquet/.pq, .json/.jsonl/.ndjson, .xlsx/.xls — each also
    in a compressed form pandas understands (.gz, .bz2, .zip, .xz), e.g. .csv.gz,
    .jsonl.gz.

    Example:
        df = read_local('/tmp/sales_data.csv')
        df = read_local('/tmp/report.parquet')
        df = read_local('/tmp/export.csv.gz')          # compressed — just works

    For a WHOLE FOLDER of part files (a glob/prefix), use read_local_prefix — the
    local parallel to read_s3 / read_s3_prefix.
    """
    return _read_local_one(path)


def _read_local_one(path: str) -> pd.DataFrame:
    """Read ONE local file into a DataFrame, dispatching on its (possibly compressed)
    extension. A trailing compression suffix (.gz/.bz2/.zip/.xz) is stripped to find
    the real data extension — pandas' csv/json readers decompress by extension
    automatically, so we just route to the right reader. CSV is the fallback for
    unknown/extensionless files (matches the S3 readers)."""
    name = path.lower()
    # Peel a compression suffix so '.csv.gz' → data ext 'csv' (pandas handles the
    # decompression itself via its default compression='infer').
    for _c in ('.gz', '.bz2', '.zip', '.xz'):
        if name.endswith(_c):
            name = name[: -len(_c)]
            break
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    if ext == 'csv':
        return pd.read_csv(path)
    if ext == 'tsv':
        return pd.read_csv(path, sep='\t')
    if ext in ('parquet', 'pq'):
        return pd.read_parquet(path)
    if ext in ('json', 'jsonl', 'ndjson'):
        # .jsonl/.ndjson = one object per line; .json = a single document.
        return pd.read_json(path, lines=ext in ('jsonl', 'ndjson'))
    if ext in ('xlsx', 'xls'):
        return pd.read_excel(path)
    # Unknown/extensionless → CSV default (pandas still infers .gz etc. from the name).
    return pd.read_csv(path)


@_log_read("local")
def read_local_prefix(path: str, format: str = None, max_files: int = 1000) -> pd.DataFrame:
    """
    Read EVERY local file matching a folder or glob and concatenate them into ONE
    DataFrame — the local parallel to read_s3_prefix, for a partitioned export
    written to /tmp (e.g. part-0000.parquet, part-0001.parquet, …).

    path accepts either:
        • a DIRECTORY — reads every file directly inside it (non-recursive):
            df = read_local_prefix('/tmp/parts/')
        • a GLOB with wildcards (* ? [ ]) — reads every match:
            df = read_local_prefix('/tmp/parts/*.parquet')
            df = read_local_prefix('/tmp/logs/*.jsonl.gz')     # glob + compressed

    format: force one of 'csv'|'tsv'|'parquet'|'json'|'excel' for every file (handy
      when the parts are EXTENSIONLESS, as Spark/Athena UNLOAD writes them). Default
      None → auto-detect per file from its extension; files with no recognized data
      extension (e.g. a _SUCCESS marker) are skipped.
    max_files: safety cap on how many files to read (default 1000).

    All files must share a compatible schema (they're pandas.concat'd, ignore_index).
    A pattern/dir that matches no readable file raises FileNotFoundError.
    """
    import glob as _glob
    # A bare directory → match everything inside it (non-recursive). A path with glob
    # metacharacters is used as-is. Anything else is treated as a single-file glob.
    if os.path.isdir(path):
        pattern = os.path.join(path, '*')
    else:
        pattern = path
    matches = sorted(p for p in _glob.glob(pattern) if os.path.isfile(p))

    # Known data extensions (after peeling a compression suffix) when auto-detecting.
    _LOCAL_EXTS = {'csv', 'tsv', 'txt', 'parquet', 'pq', 'json', 'jsonl', 'ndjson', 'xlsx', 'xls'}

    def _data_ext(p: str) -> str:
        n = p.lower()
        for _c in ('.gz', '.bz2', '.zip', '.xz'):
            if n.endswith(_c):
                n = n[: -len(_c)]
                break
        return n.rsplit('.', 1)[-1] if '.' in n else ''

    if format is None:
        matches = [p for p in matches if _data_ext(p) in _LOCAL_EXTS]
    matches = matches[:max_files]

    if not matches:
        raise FileNotFoundError(
            f"read_local_prefix: no readable files under {path!r}. Pass a directory "
            f"(e.g. '/tmp/parts/'), a glob (e.g. '/tmp/parts/*.parquet'), and — if the "
            f"part files are extensionless — set format= ('parquet'/'csv'/…)."
        )

    frames = []
    for p in matches:
        if format == 'parquet':
            frames.append(pd.read_parquet(p))
        elif format == 'csv':
            frames.append(pd.read_csv(p))
        elif format == 'tsv':
            frames.append(pd.read_csv(p, sep='\t'))
        elif format == 'json':
            frames.append(pd.read_json(p))
        elif format == 'excel':
            frames.append(pd.read_excel(p))
        else:
            frames.append(_read_local_one(p))
    return pd.concat(frames, ignore_index=True)


def _resolve_s3_target(bucket: str, key: str = None) -> tuple:
    """Normalize the S3 reader args to (bucket, key), accepting BOTH call styles:

      • two args:  read_s3_csv("my-bucket", "data/sales.csv")
      • one full URI: read_s3_csv("s3://my-bucket/data/sales.csv")
      • one bucket/key path: read_s3_csv("my-bucket/data/sales.csv")

    Passing a full s3:// URI as the single argument is the natural thing to do with
    a URI you were handed (e.g. a promoted data source), so the readers accept it and
    split it here rather than raising 'missing key'. Raises a clear ValueError if a
    single argument isn't a resolvable bucket/key."""
    if key is not None:
        return bucket, key
    ref = (bucket or "").strip()
    if ref.startswith("s3://"):
        ref = ref[len("s3://"):]
    if "/" not in ref:
        raise ValueError(
            f"read_s3_* needs a key: call read_s3_csv(bucket, key) or pass a full "
            f"path/URI like read_s3_csv('s3://bucket/key.csv'). Got: {bucket!r}"
        )
    b, k = ref.split("/", 1)
    return b, k


@_log_read("s3")
def read_s3_csv(bucket: str, key: str = None, **kwargs) -> pd.DataFrame:
    """
    Read a CSV file from S3 into a DataFrame.

    Accepts either (bucket, key) or a single full S3 URI:
        df = read_s3_csv("my-bucket", "data/sales.csv")
        df = read_s3_csv("s3://my-bucket/data/sales.csv")
        df = read_s3_csv("my-bucket", "exports/report.csv", delimiter=';')
    """
    bucket, key = _resolve_s3_target(bucket, key)
    return pd.read_csv(io.BytesIO(_s3.get_bytes(bucket, key, region=_aws_region())), **kwargs)


@_log_read("s3")
def read_s3_parquet(bucket: str, key: str = None) -> pd.DataFrame:
    """
    Read a Parquet file from S3 into a DataFrame.

    Accepts either (bucket, key) or a single full S3 URI:
        df = read_s3_parquet("my-bucket", "data/events.parquet")
        df = read_s3_parquet("s3://my-bucket/data/events.parquet")
    """
    bucket, key = _resolve_s3_target(bucket, key)
    return pd.read_parquet(io.BytesIO(_s3.get_bytes(bucket, key, region=_aws_region())))


@_log_read("s3")
def read_s3_json(bucket: str, key: str = None, lines: bool = True) -> pd.DataFrame:
    """
    Read a JSON file from S3 into a DataFrame.

    Accepts either (bucket, key) or a single full S3 URI:
        df = read_s3_json("my-bucket", "data/events.jsonl")
        df = read_s3_json("s3://my-bucket/data/config.json", lines=False)
    """
    bucket, key = _resolve_s3_target(bucket, key)
    return pd.read_json(io.BytesIO(_s3.get_bytes(bucket, key, region=_aws_region())), lines=lines)


@_log_read("s3")
def read_s3_excel(bucket: str, key: str = None, sheet=0, **kwargs) -> pd.DataFrame:
    """
    Read an Excel file (.xlsx/.xls) from S3 into a DataFrame.

    Accepts either (bucket, key) or a single full S3 URI:
        df = read_s3_excel("my-bucket", "reports/q4.xlsx")
        df = read_s3_excel("s3://my-bucket/reports/q4.xlsx", sheet="Summary")

    sheet: sheet name or 0-based index (passed to pandas as sheet_name).
    """
    bucket, key = _resolve_s3_target(bucket, key)
    return pd.read_excel(io.BytesIO(_s3.get_bytes(bucket, key, region=_aws_region())),
                         sheet_name=sheet, **kwargs)


# Map a file extension → the S3 reader that handles it (used by read_s3 dispatch).
_S3_READERS_BY_EXT = {
    'csv': 'csv', 'tsv': 'csv', 'txt': 'csv',
    'parquet': 'parquet', 'pq': 'parquet',
    'json': 'json', 'jsonl': 'json', 'ndjson': 'json',
    'xlsx': 'excel', 'xls': 'excel',
}


def _s3_read_one(bucket: str, key: str) -> pd.DataFrame:
    """Read a single S3 object into a DataFrame, dispatching on its extension.
    CSV is the fallback for unknown/extensionless keys (matches read_local)."""
    ext = key.rsplit('.', 1)[-1].lower() if '.' in key else ''
    kind = _S3_READERS_BY_EXT.get(ext, 'csv')
    if kind == 'parquet':
        return read_s3_parquet(bucket, key)
    if kind == 'json':
        # .json (single doc) vs .jsonl/.ndjson (one object per line).
        return read_s3_json(bucket, key, lines=ext in ('jsonl', 'ndjson'))
    if kind == 'excel':
        return read_s3_excel(bucket, key)
    return read_s3_csv(bucket, key)


@_log_read("s3")
def read_s3(bucket: str, key: str = None) -> pd.DataFrame:
    """
    Read a single file from S3 into a DataFrame, auto-detecting the format from the
    file extension (.csv/.tsv, .parquet, .json/.jsonl, .xlsx/.xls). One reader when
    you don't want to pick read_s3_csv vs read_s3_parquet vs … by hand.

    Accepts either (bucket, key) or a single full S3 URI:
        df = read_s3("my-bucket", "data/sales.parquet")
        df = read_s3("s3://my-bucket/data/events.jsonl")

    For a whole prefix of part files, use read_s3_prefix.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    return _s3_read_one(bucket, key)


@_log_read("s3")
def read_s3_prefix(bucket: str, prefix: str = None, format: str = None,
                   max_files: int = 1000) -> pd.DataFrame:
    """
    Read EVERY file under an S3 prefix and concatenate them into ONE DataFrame —
    the natural way to load a partitioned export (e.g. data/part-0000.parquet,
    part-0001.parquet, …) written by Spark/Athena/Glue.

    Accepts either (bucket, prefix) or a single full S3 URI/path:
        df = read_s3_prefix("my-bucket", "exports/orders/")
        df = read_s3_prefix("s3://my-bucket/exports/orders/")

    format: force one of 'csv'|'parquet'|'json'|'excel' for all files. Default None
      → auto-detect per file from its extension (files with no data extension, e.g.
      Spark's _SUCCESS marker, are skipped).
    max_files: safety cap on how many objects to read (default 1000).

    All files must share a compatible schema (they're pandas.concat'd, ignore_index).
    """
    # Accept a single "bucket/prefix" or "s3://bucket/prefix" argument.
    if prefix is None:
        bucket, prefix = _s3.split_bucket_prefix(bucket)

    keys = []
    for k in _s3.list_keys(bucket, prefix or "", region=_aws_region()):
        if k.endswith('/'):
            continue  # a "folder" placeholder object
        ext = k.rsplit('.', 1)[-1].lower() if '.' in k else ''
        # When auto-detecting, skip files with no recognized data extension
        # (e.g. Spark's _SUCCESS / _committed markers). When format is forced,
        # read every non-folder object under the prefix.
        if format is None and ext not in _S3_READERS_BY_EXT:
            continue
        keys.append(k)
        if len(keys) >= max_files:
            break

    if not keys:
        raise FileNotFoundError(
            f"No readable files under s3://{bucket}/{prefix or ''} "
            f"(looked for {', '.join(sorted(_S3_READERS_BY_EXT))} extensions)."
        )

    frames = []
    for k in sorted(keys):
        if format == 'parquet':
            frames.append(read_s3_parquet(bucket, k))
        elif format == 'csv':
            frames.append(read_s3_csv(bucket, k))
        elif format == 'json':
            frames.append(read_s3_json(bucket, k))
        elif format == 'excel':
            frames.append(read_s3_excel(bucket, k))
        else:
            frames.append(_s3_read_one(bucket, k))
    return pd.concat(frames, ignore_index=True)


@_log_read("s3")
def download_s3(bucket: str, key: str = None, local_path: str = None) -> str:
    """
    Download an S3 object to a local file under /tmp (the sandbox's only writable
    dir) and return the local path. Use this for NON-tabular artifacts — a model
    file, an archive, an image — that you want on the local filesystem rather than
    in a DataFrame. (For tabular data, prefer read_s3 / read_s3_prefix.)

    Accepts either (bucket, key[, local_path]) or a single full S3 URI:
        path = download_s3("my-bucket", "models/model.joblib")   # → /tmp/model.joblib
        path = download_s3("s3://my-bucket/data/archive.zip")
        path = download_s3("my-bucket", "big.bin", "/tmp/custom.bin")

    local_path: destination under /tmp. Defaults to /tmp/<basename-of-key>.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    if local_path is None:
        local_path = f"/tmp/{key.rsplit('/', 1)[-1]}"
    # Confine downloads to /tmp (the only writable location in the sandbox VM).
    resolved = os.path.realpath(local_path)
    tmp_root = os.path.realpath("/tmp")
    if resolved != tmp_root and not resolved.startswith(tmp_root + os.sep):
        raise ValueError(
            f"Cannot download to '{local_path}': the sandbox only allows writing "
            f"under /tmp/. Pass a /tmp/ path (e.g. '/tmp/{key.rsplit('/', 1)[-1]}')."
        )
    _s3.s3_client(_aws_region()).download_file(bucket, key, local_path)
    return local_path


def list_s3(bucket: str, prefix: str = "", max_keys: int = 100) -> pd.DataFrame:
    """
    List objects in an S3 bucket/prefix and return as a DataFrame.

    Accepts either (bucket, prefix) or a single full S3 URI/path:
        files = list_s3("my-bucket", "data/")
        files = list_s3("my-bucket")                 # list root
        files = list_s3("s3://my-bucket/data/")      # bucket + prefix from a URI
    """
    # If only one arg is given and it looks like a URI/path, split bucket + prefix.
    # (Unlike the readers, a bare "bucket" with NO slash is valid here → list root.)
    if not prefix:
        bucket, prefix = _s3.split_bucket_prefix(bucket)
    # All pages up to max_keys (a single list call returns at most 1000 keys).
    contents, _ = _s3.list_objects(bucket, prefix, max_keys=max_keys, region=_aws_region())

    items = []
    for obj in contents:
        items.append({
            'key': obj['Key'],
            'size_mb': round(obj['Size'] / (1024 * 1024), 2),
            'last_modified': obj['LastModified'].strftime('%Y-%m-%d %H:%M'),
        })

    df = pd.DataFrame(items)
    if len(df) > 0:
        print(f"  {len(df)} objects in s3://{bucket}/{prefix} ({df['size_mb'].sum():.1f} MB total)")
    return df


@_log_read("s3")
def head_s3(bucket: str, key: str = None, n: int = 5) -> pd.DataFrame:
    """
    Preview the first N rows of an S3 file, auto-detecting the format from the file
    extension (.csv/.tsv, .parquet/.pq, .json/.jsonl/.ndjson, .xlsx/.xls). Reads as
    little as possible — a byte range for text, the first row group for Parquet — so
    it's cheap even on large files. Also the quick way to see a file's SCHEMA before
    a full read: `head_s3("s3://bucket/data/events.parquet").dtypes`.

    Accepts either (bucket, key) or a single full S3 URI:
        head_s3("my-bucket", "data/large_file.csv")
        head_s3("s3://my-bucket/data/events.parquet")
        head_s3("my-bucket", "reports/q4.xlsx", n=10)

    Note: this reads ONE object. For a prefix of part files, read the first part with
    read_s3_prefix(uri, max_files=1) (its .dtypes is the dataset's schema).
    """
    bucket, key = _resolve_s3_target(bucket, key)
    ext = key.rsplit('.', 1)[-1].lower() if '.' in key else ''
    kind = _S3_READERS_BY_EXT.get(ext, 'csv')  # same ext→format map read_s3 uses; csv fallback
    region = _aws_region()

    if kind == 'parquet':
        # Parquet schema + rows live in row groups; read only the FIRST one instead of
        # the whole file. pyarrow exposes per-row-group reads; fall back to a full read
        # if anything about the layout prevents it (e.g. a single huge row group).
        buf = io.BytesIO(_s3.get_bytes(bucket, key, region=region))
        try:
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(buf)
            if pf.num_row_groups > 0:
                tbl = pf.read_row_group(0)
                return tbl.to_pandas().head(n)
            return pf.read().to_pandas().head(n)
        except Exception:
            buf.seek(0)
            return pd.read_parquet(buf).head(n)

    if kind == 'excel':
        # Excel is a zip container — it can't be byte-ranged, so read the full object
        # then take the head. (Preview files are small; a huge .xlsx is rare.)
        return pd.read_excel(io.BytesIO(_s3.get_bytes(bucket, key, region=region)), nrows=n)

    if kind == 'json':
        if ext in ('jsonl', 'ndjson'):
            # Line-delimited JSON: a byte range gives us the first rows cheaply.
            raw = _s3.get_bytes(bucket, key, byte_range='bytes=0-65536', region=region)
            return pd.read_json(io.BytesIO(raw), lines=True, nrows=n)
        # A single JSON document can't be safely truncated mid-array by bytes — read
        # the whole object, then head.
        return pd.read_json(io.BytesIO(_s3.get_bytes(bucket, key, region=region)),
                            lines=False).head(n)

    # csv / tsv / txt / unknown → range-read the first 64KB and parse N rows. A range
    # can cut the final line mid-row; nrows=n keeps us well inside the window for a
    # small preview, and pandas drops a trailing partial line.
    sep = '\t' if ext == 'tsv' else ','
    raw = _s3.get_bytes(bucket, key, byte_range='bytes=0-65536', region=region)
    return pd.read_csv(io.BytesIO(raw), nrows=n, sep=sep)


# DynamoDB rejects Python floats (it stores numbers as Decimal). Convert floats —
# anywhere in the value, incl. lists for `between`/`in` — to Decimal via str() so
# the user never sees "Float types are not supported" and never has to think about
# Decimal. int/str/bool/etc. pass through untouched.
def _dynamo_number(v):
    from decimal import Decimal
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, (list, tuple)):
        return [_dynamo_number(x) for x in v]
    return v


# Comparison operators accepted in a dict filter, mapped to the boto3 Attr method.
# This is what lets a user write filter={"score": {"gte": 0.9}} instead of importing
# Attr and wrapping values in Decimal themselves.
_DYNAMO_FILTER_OPS = {
    "eq", "ne", "lt", "lte", "gt", "gte",
    "begins_with", "contains", "between", "in",
}


def _build_dynamo_filter(filter):
    """Turn a user filter into a boto3 FilterExpression condition (or return a raw
    Attr()-built condition untouched). Supports:
      • {field: value}                      → equality (field == value)
      • {field: {op: value}}                → Attr(field).<op>(value), op in
        eq/ne/lt/lte/gt/gte/begins_with/contains/between/in
        (between takes a [lo, hi] list; in takes a list of values)
      • {field: {op1: v1, op2: v2}}         → both conditions AND-ed (e.g. a range)
    Multiple fields are AND-ed together. Floats are auto-converted to Decimal, so
    filter={"score": {"gte": 0.9}} just works — no Attr import, no Decimal dance.
    A non-dict is assumed to be a raw boto3 condition and passed through."""
    from boto3.dynamodb.conditions import Attr
    if not isinstance(filter, dict):
        return filter  # raw Attr()-built condition — full expressiveness, untouched

    conds = []
    for field, spec in filter.items():
        attr = Attr(field)
        if isinstance(spec, dict):
            # {op: value} form — one or more comparison operators on this field.
            for op, val in spec.items():
                key = op.lower().rstrip("_")  # accept "in_" as an alias for "in"
                if key not in _DYNAMO_FILTER_OPS:
                    raise ValueError(
                        f"Unknown filter operator {op!r} for field {field!r}. "
                        f"Use one of: {', '.join(sorted(_DYNAMO_FILTER_OPS))}. "
                        f"Example: filter={{'{field}': {{'gte': 10}}}}."
                    )
                val = _dynamo_number(val)
                if key == "between":
                    if not isinstance(val, (list, tuple)) or len(val) != 2:
                        raise ValueError(
                            f"'between' on {field!r} needs a [low, high] pair, e.g. "
                            f"filter={{'{field}': {{'between': [10, 20]}}}}."
                        )
                    conds.append(attr.between(val[0], val[1]))
                elif key == "in":
                    vals = val if isinstance(val, (list, tuple)) else [val]
                    conds.append(attr.is_in(list(vals)))
                else:
                    conds.append(getattr(attr, key)(val))
        else:
            # {field: value} shorthand → equality.
            conds.append(attr.eq(_dynamo_number(spec)))

    cond = None
    for c in conds:
        cond = c if cond is None else (cond & c)
    return cond


@_log_read("dynamodb")
def read_dynamodb(table_name: str, limit: int = None, filter: dict = None) -> pd.DataFrame:
    """
    Scan a DynamoDB table and return as a DataFrame.

    filter (optional): narrow the rows returned. NO boto3 import needed — pass a
    plain dict:
        • Equality:   filter={"segment": "Premium"}            (multiple keys AND-ed)
        • Operators:  filter={"score": {"gte": 0.9}}           (float→Decimal handled)
                      filter={"rating": {"between": [4, 5]}}
                      filter={"status": {"in": ["new", "open"]}}
                      filter={"title": {"begins_with": "Great"}}
        • Range:      filter={"price": {"gte": 10, "lte": 50}}  (ops AND-ed)
      Operators: eq, ne, lt, lte, gt, gte, begins_with, contains, between, in.
      (A raw Attr()-built condition is still accepted for anything beyond this.)

    ⚠️ COST — a filter is applied SERVER-SIDE but only AFTER a full table scan:
    DynamoDB still READS (and bills read capacity for) every item, then returns
    just the matches. So a filter cuts network transfer + VM memory, NOT scan cost.
    For a cost-efficient lookup by key, use read_dynamodb_query (it uses the index).

    ⚠️ limit + filter CAN UNDER-RETURN: DynamoDB's Limit caps items SCANNED, not items
    MATCHED, and the filter is applied after. So read_dynamodb(t, limit=100,
    filter=...) returns the matches found WITHIN the first 100 scanned items — which
    may be far fewer than 100 (or zero) even when many more exist. The result is NOT
    paginated when a limit is set. To get ALL matches, OMIT limit (the scan paginates
    the whole table); use limit only for a quick unfiltered peek.

    Example:
        df = read_dynamodb("microvm-demo-data")
        df = read_dynamodb("users-table", limit=100)
        df = read_dynamodb("customers", filter={"segment": "Premium"})
        df = read_dynamodb("ecommerce-recommendations", filter={"score": {"gte": 0.9}})
    """
    cond = _build_dynamo_filter(filter) if filter is not None else None

    # No limit: paginate the whole table. A filtered scan STILL paginates over the
    # whole table (LastEvaluatedKey advances by items scanned, not items matched), so
    # we keep scanning until the table is exhausted to collect all matches. With a
    # limit: one page (documented above).
    items = _dynamo.scan_items(table_name, _aws_region(), limit=limit or None,
                               filter_condition=cond, fill_limit=False)
    return pd.DataFrame(items)


@_log_read("dynamodb")
def dynamodb_tables() -> pd.DataFrame:
    """
    List the DynamoDB tables visible to this notebook as a DataFrame (one row per
    table). Discovery helper — use it to find table names to pass to read_dynamodb
    / read_dynamodb_query.

    Example:
        dynamodb_tables()
    """
    client = _aws.aws_client('dynamodb', _aws_region())
    names = []
    resp = client.list_tables()
    names.extend(resp.get('TableNames', []))
    while 'LastEvaluatedTableName' in resp:
        resp = client.list_tables(ExclusiveStartTableName=resp['LastEvaluatedTableName'])
        names.extend(resp.get('TableNames', []))
    return pd.DataFrame({'table': names})


# DynamoDB scalar type codes (from the key AttributeDefinitions) → readable names.
_DYNAMO_TYPE_NAMES = {"S": "string", "N": "number", "B": "binary"}


def _dynamo_key_schema(table_name: str) -> dict:
    """Resolve a table's KEY schema via describe_table: {partition_key, sort_key,
    key_types:{name:type}}. Shared by read_dynamodb_query (for its error hint) and
    dynamodb_schema. sort_key is None for a partition-only table."""
    client = _aws.aws_client('dynamodb', _aws_region())
    desc = client.describe_table(TableName=table_name)["Table"]
    type_by_attr = {a["AttributeName"]: _DYNAMO_TYPE_NAMES.get(a["AttributeType"], a["AttributeType"])
                    for a in desc.get("AttributeDefinitions", [])}
    partition_key = sort_key = None
    for k in desc.get("KeySchema", []):
        if k["KeyType"] == "HASH":
            partition_key = k["AttributeName"]
        elif k["KeyType"] == "RANGE":
            sort_key = k["AttributeName"]
    return {"partition_key": partition_key, "sort_key": sort_key, "key_types": type_by_attr}


@_log_read("dynamodb")
def dynamodb_schema(table_name: str, sample: int = 25) -> pd.DataFrame:
    """
    Show a DynamoDB table's schema as a DataFrame (columns: attribute, type, role) —
    the discovery helper to run BEFORE read_dynamodb_query so you use the right key.
    Parallel to athena_schema / aurora_schema.

    DynamoDB is schemaless beyond its keys, so this reports:
      • The KEY schema exactly (from describe_table): the partition key (role
        'partition_key') and sort key (role 'sort_key'), with their declared types.
      • Other attributes DISCOVERED by sampling a few items (role 'attribute') — since
        items can differ, treat these as "seen on sampled rows", not a fixed schema.

    Example:
        dynamodb_schema("ecommerce-recommendations")
        # → partition_key: userId, sort_key: productId, plus sampled attributes
    """
    ks = _dynamo_key_schema(table_name)
    rows = []
    seen = set()
    if ks["partition_key"]:
        rows.append({"attribute": ks["partition_key"],
                     "type": ks["key_types"].get(ks["partition_key"], ""),
                     "role": "partition_key"})
        seen.add(ks["partition_key"])
    if ks["sort_key"]:
        rows.append({"attribute": ks["sort_key"],
                     "type": ks["key_types"].get(ks["sort_key"], ""),
                     "role": "sort_key"})
        seen.add(ks["sort_key"])
    # Sample a handful of items to surface the non-key attributes (best-effort).
    try:
        items = _dynamo.sample_items(table_name, sample, _aws_region())
        for it in items:
            for attr, val in it.items():
                if attr in seen:
                    continue
                seen.add(attr)
                # Infer a readable type from the sampled Python value.
                if isinstance(val, bool):
                    t = "boolean"
                elif isinstance(val, (int, float)) or type(val).__name__ == "Decimal":
                    t = "number"
                elif isinstance(val, (list, tuple)):
                    t = "list"
                elif isinstance(val, dict):
                    t = "map"
                else:
                    t = "string"
                rows.append({"attribute": attr, "type": t, "role": "attribute"})
    except Exception:
        pass  # keys alone are still useful if the sample scan fails
    return pd.DataFrame(rows, columns=["attribute", "type", "role"])


@_log_read("dynamodb")
def read_dynamodb_query(table_name: str, key, value, sort_key: str = None,
                        sort_value=None) -> pd.DataFrame:
    """
    Query a DynamoDB table by partition key (and optional sort key), equality match.

    Pass the key NAME and VALUE as data — you do NOT write a condition expression.
    Reserved-word attribute names (status, name, date, year, ...) are handled
    automatically, so any key name just works.

    Example:
        # partition key only
        df = read_dynamodb_query("ecommerce-reviews", "productId", "P-100")
        # partition key + sort key
        df = read_dynamodb_query("orders", "customer_id", "CUST-001",
                                 sort_key="order_date", sort_value="2024-01-01")
    """
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
    cond = Key(_dynamo_number(key)).eq(_dynamo_number(value))
    if sort_key is not None:
        cond = cond & Key(sort_key).eq(_dynamo_number(sort_value))
    try:
        items = _dynamo.query_items(table_name, cond, _aws_region())
    except ClientError as e:
        # The classic mistake: querying by a NON-KEY attribute. DynamoDB's Query can
        # only match the table's key schema; a non-key attribute raises "Query
        # condition missed key schema element". Turn that into an actionable message
        # that names the table's ACTUAL partition/sort key and points to the right
        # tool for non-key lookups.
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ValidationException":
            try:
                ks = _dynamo_key_schema(table_name)
                pk = ks.get("partition_key")
                sk = ks.get("sort_key")
                key_desc = f"partition key '{pk}'" + (f" (+ optional sort key '{sk}')" if sk else "")
                raise ValueError(
                    f"read_dynamodb_query on '{table_name}' must use the table's "
                    f"{key_desc} — you passed key='{key}'. DynamoDB Query only "
                    f"matches the key schema. To filter by the non-key attribute "
                    f"'{key}', scan instead: "
                    f"read_dynamodb('{table_name}', filter={{'{key}': {value!r}}}) "
                    f"(or use an operator, e.g. filter={{'{key}': {{'gte': ...}}}}). "
                    f"Tip: dynamodb_schema('{table_name}') shows the key schema."
                ) from e
            except ValueError:
                raise
            except Exception:
                raise e  # couldn't introspect keys — surface the original error
        raise
    return pd.DataFrame(items)


@_log_read("athena")
def read_athena(sql: str, database: str = None, catalog: str = None) -> pd.DataFrame:
    """
    Run an Athena SQL query and return results as a DataFrame.

    Example:
        df = read_athena("SELECT * FROM sales_data LIMIT 100")
        df = read_athena("SELECT country, COUNT(*) FROM customers GROUP BY country")

    S3 Tables (Apache Iceberg) just work like any Athena table — reference them by
    the database.table shown in the Data Sources panel; the federated catalog is
    resolved for you automatically. Both of these are equivalent:
        df = read_athena("SELECT * FROM microvm_demo_iceberg.order_events LIMIT 100")
        df = read_athena("SELECT * FROM order_events LIMIT 100", database="microvm_demo_iceberg")

    catalog (optional): only needed to force a specific Athena data catalog. Leave
    unset — it's auto-resolved from the database (default AwsDataCatalog for regular
    Glue tables, the federated catalog for S3 Tables).

    CROSS-CATALOG JOINS (S3 Tables / Iceberg ⋈ regular Athena): Athena CAN join
    tables from different catalogs in ONE query — you just have to reference each
    table with its FULLY-QUALIFIED "catalog"."database"."table" name so Athena
    resolves each in the right catalog. The problem with a plain 2-part
    "database.table" reference is that it's resolved against the query's single
    execution-context catalog, so mixing an S3 Tables db with a default-catalog db
    fails with SCHEMA_NOT_FOUND. This helper handles that for you: when a query
    references more than one catalog, it rewrites every "<db>.<table>" to the
    3-part "<catalog>"."<db>"."<table>" form (the catalog is looked up from the
    data-source registry) and runs it as ONE server-side Athena query — no data is
    pulled into the VM. So both forms work:
        df = read_athena("SELECT ... FROM microvm_demo_iceberg.order_events oe "
                         "JOIN microvm_demo_db.orders o ON oe.order_id=o.order_id")
    """
    region = _aws_region()

    # --- Resolve the { database -> federated catalog } map for S3 Tables ---
    # S3 Tables namespaces carry a non-default Athena catalog (e.g. "s3tables_microvm");
    # regular Glue databases are absent from this map (they use "AwsDataCatalog").
    db_catalog_map = {}
    if not catalog:
        try:
            from app.notebook.data_catalog import get_active_catalog
            _cat = get_active_catalog()
            if _cat is not None:
                db_catalog_map = _cat.athena_db_catalog_map()
        except Exception:
            db_catalog_map = {}

    # --- Classify the db refs in the SQL by catalog ---
    # Every "<db>.<table>" (FROM/JOIN) belongs to a federated catalog (from the map)
    # or "" (the default AwsDataCatalog). If the SQL spans MORE THAN ONE catalog, a
    # plain 2-part reference can't resolve in a single execution context — so rewrite
    # every ref to the fully-qualified "<catalog>"."<db>"."<table>" form and let
    # Athena federate the join server-side (no VM-side materialization).
    if not catalog and db_catalog_map:
        referenced_dbs = _athena_referenced_dbs(sql)
        catalogs_in_play = set()
        for db in referenced_dbs:
            catalogs_in_play.add(db_catalog_map.get(db, ""))  # "" = default catalog
        # A federated db passed via database= (with no db-qualified refs in SQL) also
        # counts as its own catalog.
        if database and database in db_catalog_map:
            catalogs_in_play.add(db_catalog_map[database])

        if len(catalogs_in_play) > 1:
            # CROSS-CATALOG: fully-qualify every ref and run ONE federated query.
            qualified_sql = _qualify_athena_refs(sql, referenced_dbs, db_catalog_map)
            logger.info(
                f"[read_athena] Cross-catalog JOIN across {len(catalogs_in_play)} "
                f"catalog(s) — fully-qualifying table refs and running one federated "
                f"Athena query."
            )
            # Every ref is now self-qualified, so the execution-context catalog is
            # irrelevant; run in the default AwsDataCatalog context.
            return _run_athena_raw(qualified_sql, catalog=None, database=_ATHENA_DB,
                                   region=region)

        # SINGLE federated catalog (all refs in one S3 Tables namespace): run in that
        # context with the "<db>." prefix stripped (the federated context needs plain
        # table names — the qualified form won't parse).
        fed_db = None
        if database and database in db_catalog_map:
            fed_db = database
        else:
            for db in referenced_dbs:
                if db in db_catalog_map:
                    fed_db = db
                    break
        if fed_db:
            catalog = db_catalog_map[fed_db]
            database = fed_db
            sql = _strip_db_prefix(sql, fed_db)

    database = database or _ATHENA_DB
    return _run_athena_raw(sql, catalog=catalog, database=database, region=region)


# ─── Athena helpers: db-ref parsing, ref qualification, single-context runner ──

def _athena_referenced_dbs(sql: str) -> list[str]:
    """Return the DISTINCT database names referenced as a `<db>.<table>` prefix in
    FROM/JOIN/comma clauses. Order-preserving. Uses the SAME masking + ref regex as
    the rewrite so the catalog classification and the rewrite agree exactly — string
    literals/comments are ignored, and a 3-part-qualified ref (whose 'db' is really a
    catalog) is skipped so it can't inflate the catalog set."""
    masked = _mask_sql_literals(sql)
    out = []
    seen = set()
    for m in _ATHENA_REF_RE.finditer(masked):
        db = sql[m.start(2):m.end(2)].strip('"')
        if db not in seen:
            seen.add(db)
            out.append(db)
    return out


def _strip_db_prefix(sql: str, db: str) -> str:
    """Strip the "<db>." qualifier so table refs become plain names for a federated
    execution context. Handles all quoting combinations and a table token that may
    be quoted or contain non-word chars (e.g. Iceberg metadata like tbl$snapshots):
        "db"."tbl" -> "tbl"   db."tbl" -> "tbl"   "db".tbl -> tbl   db.tbl -> tbl
    """
    return re.sub(r'"?' + re.escape(db) + r'"?\s*\.\s*("(?:[^"]+)"|[A-Za-z_]\w*(?:\$\w+)?)',
                  r'\1', sql)


def _run_athena_raw(sql: str, catalog: str = None, database: str = None,
                    region: str = None) -> pd.DataFrame:
    """Execute ONE Athena query in a SINGLE catalog context and return a typed
    DataFrame. This is the low-level runner shared by the fast single-catalog path
    and the per-source materialization in the cross-catalog path. `catalog` empty/
    None targets the default AwsDataCatalog; a value targets that federated catalog
    (and `sql` must already reference plain, unqualified table names). Runs on the
    shared runner (_athena.py): bounded by the cell timeout, cancelled on timeout
    or interrupt."""
    region = region or _aws.aws_region()
    return _run_athena(
        sql, workgroup=_ATHENA_WORKGROUP, region=region,
        database=database or _ATHENA_DB, catalog=catalog or None,
        output_location=f"s3://{_aws.artifact_bucket(region)}/athena-results/",
        fail_prefix="Athena query failed")
def _resolve_athena_catalog(database: str) -> str | None:
    """Return the NON-default Athena data-catalog a database lives in (e.g. the S3
    Tables federated catalog "s3tables_microvm" for the "microvm_demo_iceberg"
    namespace), or None if it's a regular database in the default AwsDataCatalog.

    Same source of truth read_athena uses (the active DataCatalog's db→catalog map),
    so the discovery helpers route to the same catalog the query would. Best-effort:
    any failure resolving the map returns None → the caller uses the default catalog
    / Glue path, exactly as before."""
    try:
        from app.notebook.data_catalog import get_active_catalog
        cat = get_active_catalog()
        if cat is None:
            return None
        return cat.athena_db_catalog_map().get(database) or None
    except Exception:
        return None


@_log_read("athena")
def athena_tables(database: str = None) -> pd.DataFrame:
    """
    List the tables in an Athena/Glue database as a DataFrame (columns: database,
    table, type). Discovery helper — use it to find tables to query with read_athena.

    database: the Glue database to list. Defaults to the project database
      (ATHENA_DB). Pass a name to inspect another database (e.g. the S3 Tables /
      Iceberg namespace "microvm_demo_iceberg").

    Example:
        athena_tables()
        athena_tables("microvm_demo_iceberg")
    """
    database = database or _ATHENA_DB
    # S3 Tables (Iceberg) namespaces live in a NON-default Athena catalog (e.g.
    # "s3tables_microvm"), so their Glue database isn't in the account's default
    # Glue catalog — a direct glue.get_tables(DatabaseName=...) raises
    # EntityNotFoundException. When the db is a federated one, ask ATHENA instead
    # (SHOW TABLES IN <db> against the resolved catalog), matching how read_athena
    # routes queries. Default-catalog databases keep the fast, cheap Glue path.
    catalog = _resolve_athena_catalog(database)
    if catalog:
        res = _run_athena_raw(f'SHOW TABLES IN "{database}"', catalog=catalog, database=database)
        # SHOW TABLES returns a single unnamed column of table names.
        names = res.iloc[:, 0].tolist() if not res.empty else []
        return pd.DataFrame(
            [{'database': database, 'table': n, 'type': 'EXTERNAL_TABLE'} for n in names],
            columns=['database', 'table', 'type'],
        )
    glue = _aws.aws_client('glue', _aws_region())
    rows = []
    paginator = glue.get_paginator('get_tables')
    for page in paginator.paginate(DatabaseName=database):
        for t in page.get('TableList', []):
            rows.append({
                'database': database,
                'table': t.get('Name'),
                'type': t.get('TableType', ''),
            })
    return pd.DataFrame(rows, columns=['database', 'table', 'type'])


@_log_read("athena")
def athena_schema(table: str, database: str = None) -> pd.DataFrame:
    """
    Show the columns of an Athena/Glue table as a DataFrame (columns: column, type,
    partition). Includes partition keys (partition=True) after the regular columns.
    Discovery helper — use it to see what you can SELECT before writing read_athena.

    Accepts "table" or "database.table"; an explicit database= wins over a prefix.

    Example:
        athena_schema("orders")
        athena_schema("microvm_demo_iceberg.order_events")
    """
    # Allow a "database.table" convenience form.
    if database is None and '.' in table:
        database, table = table.split('.', 1)
    database = database or _ATHENA_DB
    # Federated (S3 Tables / Iceberg) database → resolve via Athena, not Glue
    # (glue.get_table can't see a db that lives in a non-default catalog). We read
    # column metadata from information_schema in that catalog; Iceberg partitioning
    # is hidden (transform-based), so `partition` is reported False there.
    catalog = _resolve_athena_catalog(database)
    if catalog:
        safe_db = database.replace("'", "''")
        safe_tbl = table.replace("'", "''")
        res = _run_athena_raw(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema = '{safe_db}' AND table_name = '{safe_tbl}' "
            "ORDER BY ordinal_position",
            catalog=catalog, database=database,
        )
        rows = [{'column': r['column_name'], 'type': r['data_type'], 'partition': False}
                for _, r in res.iterrows()]
        return pd.DataFrame(rows, columns=['column', 'type', 'partition'])
    glue = _aws.aws_client('glue', _aws_region())
    meta = glue.get_table(DatabaseName=database, Name=table)['Table']
    sd = meta.get('StorageDescriptor', {}) or {}
    rows = []
    for c in sd.get('Columns', []):
        rows.append({'column': c.get('Name'), 'type': c.get('Type', ''), 'partition': False})
    for c in meta.get('PartitionKeys', []) or []:
        rows.append({'column': c.get('Name'), 'type': c.get('Type', ''), 'partition': True})
    return pd.DataFrame(rows, columns=['column', 'type', 'partition'])


_ATHENA_DEFAULT_CATALOG = "AwsDataCatalog"


# A `<db>.<table>` reference immediately after a FROM or JOIN keyword. Captures:
#   1: the FROM/JOIN keyword + following whitespace,
#   2: the db    — a bare identifier `\w+` (word-boundary anchored so the whole name
#      is consumed, never a prefix) or a double-quoted name,
#   3: the table — bare (may carry an Iceberg metadata suffix like `$snapshots`) or
#      double-quoted.
# A NEGATIVE LOOKAHEAD `(?!\s*\.)` after the table skips a ref that is ALREADY
# 3-part qualified (e.g. "catalog"."db"."table" — what we'd read as db.table is
# really catalog.db, and rewriting would corrupt it).
#
# IMPORTANT — why NOT match a leading comma: an implicit comma-join (`FROM a, b`)
# is vanishingly rare, but a comma before an `alias.column` in the SELECT list
# (`SELECT o.id, o.date, ...`) is extremely common. Matching a comma would rewrite
# those column refs into bogus `"catalog"."alias"."column"` tables — the exact
# COLUMN_NOT_FOUND bug. So we anchor STRICTLY to FROM/JOIN. `\b` after the bare-db
# alternative forces the identifier to be consumed whole (fixes the "microvm_demo_d"
# truncation seen when a lookahead caused backtracking).
_ATHENA_REF_RE = re.compile(
    r'((?:FROM|JOIN)\s+)'                            # 1: FROM / JOIN + ws
    r'("(?:[^"]+)"|[A-Za-z_]\w*\b)'                  # 2: db  (quoted, or whole bare identifier)
    r'\s*\.\s*'
    r'("(?:[^"]+)"|[A-Za-z_]\w*(?:\$\w+)?\b)'        # 3: table (quoted, or whole bare +$meta)
    r'(?!\s*\.)',                                    # not followed by another dot (→ not 3-part)
    re.IGNORECASE,
)


def _mask_sql_literals(sql: str):
    """Replace single-quoted string literals and SQL comments (-- ... and /* ... */)
    with same-length placeholders so a regex that scans for FROM/JOIN table refs can
    NEVER match text inside a string/comment (e.g. a literal 'FROM db.orders' or a
    commented-out join). Returns (masked_sql, restore_fn). restore_fn(s) maps the
    masked text back, so we operate on the masked copy for FINDING match spans, then
    rebuild the real SQL from the original using those spans.

    We keep it length-preserving so character offsets in the masked string line up
    1:1 with the original — the caller rewrites by SPAN, reading db/table from the
    original text at the matched offsets."""
    # Order matters: block comments, then line comments, then strings. Each match is
    # blanked to spaces of the SAME length (newlines preserved so line math is intact).
    def _blank(m):
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))

    masked = re.sub(r'/\*.*?\*/', _blank, sql, flags=re.DOTALL)   # /* block comments */
    masked = re.sub(r'--[^\n]*', _blank, masked)                  # -- line comments
    # Single-quoted strings, honoring '' escaped quotes inside.
    masked = re.sub(r"'(?:[^']|'')*'", _blank, masked)
    return masked


def _qualify_athena_refs(sql: str, referenced_dbs: list, db_catalog_map: dict) -> str:
    """Rewrite every `<db>.<table>` reference in a FROM/JOIN clause to the
    fully-qualified `"<catalog>"."<db>"."<table>"` form so a SINGLE Athena query can
    federate a join across catalogs (verified: Athena joins the S3 Tables catalog
    and the default AwsDataCatalog in one query when each ref is 3-part qualified).

    The catalog per db comes from `db_catalog_map` (a federated db → its registered
    Athena data-catalog name, e.g. "s3tables_microvm"); a db absent from the map is
    a regular Glue table in the default "AwsDataCatalog".

    ROBUSTNESS: only the db.table PREFIX of a real table reference is rewritten. We
      • MASK string literals + comments first, so a literal like 'FROM db.orders' or
        a commented-out `-- JOIN db.t` is never touched;
      • match refs ONLY after FROM/JOIN (NOT after a comma) — a comma precedes an
        `alias.column` in the SELECT list far more often than an implicit comma-join,
        and rewriting those columns caused a COLUMN_NOT_FOUND error;
      • SKIP refs that are already 3-part qualified (negative lookahead on a trailing
        dot), so a mix of 2-part and pre-qualified refs doesn't get mangled;
      • leave table aliases and every column reference untouched (matching is anchored
        to the FROM/JOIN keyword, never a bare `alias.column`)."""
    masked = _mask_sql_literals(sql)

    # Find spans in the MASKED text (so we skip strings/comments), then rebuild the
    # output from the ORIGINAL text using those spans — the two are offset-aligned.
    out = []
    last = 0
    for m in _ATHENA_REF_RE.finditer(masked):
        # Read the real tokens from the ORIGINAL sql at the matched offsets.
        keyword = sql[m.start(1):m.end(1)]
        db = sql[m.start(2):m.end(2)].strip('"')
        tbl = sql[m.start(3):m.end(3)].strip('"')
        cat = db_catalog_map.get(db) or _ATHENA_DEFAULT_CATALOG
        out.append(sql[last:m.start()])
        out.append(f'{keyword}"{cat}"."{db}"."{tbl}"')
        last = m.end()
    out.append(sql[last:])
    return "".join(out)


@_log_read("url")
def read_url(url: str, format: str = 'csv', **kwargs) -> pd.DataFrame:
    """
    Fetch data from a URL and return as a DataFrame.
    
    Example:
        df = read_url("https://raw.githubusercontent.com/datasets/iris/master/data/iris.csv")
        df = read_url("https://api.example.com/data.json", format='json')
    """
    if format == 'csv':
        return pd.read_csv(url, **kwargs)
    elif format == 'json':
        import requests
        # Bound the fetch so a slow/hung endpoint can't block the cell indefinitely,
        # and surface a clear error (bad status / non-JSON body) instead of a raw
        # traceback. timeout is overridable via kwargs; raise_for_status turns a 404/
        # 500 into an actionable message rather than feeding HTML to pd.DataFrame.
        timeout = kwargs.pop('timeout', 30)
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout as e:
            raise TimeoutError(
                f"read_url: request to {url} timed out after {timeout}s. Pass a larger "
                f"timeout= if the endpoint is just slow.") from e
        except ValueError as e:  # includes JSONDecodeError
            raise ValueError(
                f"read_url: {url} did not return valid JSON (format='json'). Check the "
                f"URL/format, or use format='csv'/'parquet'.") from e
        return pd.DataFrame(data)
    elif format == 'parquet':
        return pd.read_parquet(url)
    else:
        return pd.read_csv(url, **kwargs)


# =============================================================================
# DATA LOADING — Athena UNLOAD → S3 (VM-memory-free big-data transform)
# =============================================================================
# read_athena() pulls query RESULTS into the VM as a DataFrame — bounded by the VM's
# memory. For BIG data you don't want in the VM at all (prep a training set, reshape
# a huge table), use unload_athena_to_s3(): Athena runs the SELECT on its OWN
# distributed fleet and writes the result straight to S3. The VM only sends SQL and
# gets back an S3 location + a small file list — ZERO rows transit VM memory. This is
# the prep primitive that feeds managed training (sm_train) and any S3-input consumer.
#
# A SQL cell can do the same by writing a literal `UNLOAD (SELECT ...) TO 's3://...'
# WITH (format='PARQUET')` — the SQL engine routes it through the SAME runner (and
# injects a default TO if you omit it). This helper is the Python front door.

# UNLOAD output formats Athena supports. PARQUET is the default (columnar, typed,
# compressed — what downstream Athena/Spark + sm_train want). The others are valid
# but only PARQUET and TEXTFILE/CSV currently feed the training path.
_UNLOAD_FORMATS = ("parquet", "orc", "avro", "json", "textfile")


def _artifact_bucket() -> str:
    """The sandbox artifacts bucket (see aws_clients.artifact_bucket) — the one the
    exec role is scoped to WRITE (s3:PutObject on microvm-sandbox-artifacts-*/*).
    UNLOAD destinations default here so the write always has permission."""
    return _aws.artifact_bucket()
def _default_unload_dest() -> str:
    """A fresh, unique, always-empty UNLOAD prefix under the artifact bucket:
    s3://<artifact-bucket>/unload/<ts>-<shortid>/. Unique by construction, so it
    never collides with a prior run (no overwrite needed) and the write is always
    permitted (artifact bucket)."""
    import uuid
    stamp = time.strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"s3://{_artifact_bucket()}/unload/{stamp}-{short}/"


_parse_s3_uri = _s3.parse_s3_uri


def _resolve_unload_sql(sql: str) -> str:
    """Apply the SAME cross-catalog qualification read_athena uses, so an UNLOAD whose
    SELECT joins a Glue table with an S3 Tables/Iceberg table resolves each ref to its
    right catalog. Returns the (possibly rewritten) SELECT. No-op when all refs are in
    one catalog or the catalog map is unavailable."""
    db_catalog_map = {}
    try:
        from app.notebook.data_catalog import get_active_catalog
        cat = get_active_catalog()
        if cat is not None:
            db_catalog_map = cat.athena_db_catalog_map()
    except Exception:
        db_catalog_map = {}
    if not db_catalog_map:
        return sql
    referenced = _athena_referenced_dbs(sql)
    catalogs = {db_catalog_map.get(db, "") for db in referenced}
    if len(catalogs) > 1:
        # Spans >1 catalog → fully-qualify every ref so one federated query resolves.
        return _qualify_athena_refs(sql, referenced, db_catalog_map)
    return sql


class UnloadResult:
    """Handle for a completed UNLOAD. NOT a DataFrame of rows (that's the point) — it
    carries the S3 location the data landed at plus the list of files written. Pass it
    (or str(it)) anywhere an S3 input prefix is expected.

        res = unload_athena_to_s3("SELECT ... FROM db.t")
        res.s3_uri        # 's3://.../unload/<ts>-<id>/'
        res.file_count    # how many part files Athena wrote (parallel)
        res.files         # ['s3://.../file_0.parquet', ...] (from the manifest)
        str(res)          # the s3_uri — so it drops into downstream S3-input calls
    """

    def __init__(self, s3_uri, files, total_bytes, format, query_execution_id):
        self.s3_uri = s3_uri
        self.files = files
        self.file_count = len(files)
        self.total_bytes = total_bytes
        self.format = format
        self.query_execution_id = query_execution_id

    def __str__(self):
        return self.s3_uri

    def __fspath__(self):
        return self.s3_uri

    def __repr__(self):
        mb = self.total_bytes / (1024 * 1024)
        return (f"<UnloadResult {self.file_count} {self.format} file(s), "
                f"{mb:.1f} MB → {self.s3_uri}>")


def _read_unload_manifest(athena, query_execution_id: str, region: str) -> list[str]:
    """Read the list of data files an UNLOAD wrote, from the manifest Athena records
    for the query (DataManifestLocation in the query statistics). Returns the S3 URIs
    of the data files — NEVER the rows. Falls back to listing the output prefix if the
    manifest can't be read."""
    exec_info = athena.get_query_execution(QueryExecutionId=query_execution_id)
    stats = exec_info["QueryExecution"].get("Statistics", {})
    manifest_loc = stats.get("DataManifestLocation")
    if not manifest_loc:
        return []
    bucket, key = _parse_s3_uri(manifest_loc)
    body = _s3.get_bytes(bucket, key, region=region).decode("utf-8")
    # The manifest is one S3 URI per line.
    return [line.strip() for line in body.splitlines() if line.strip()]


def _sum_s3_sizes(file_uris: list[str], region: str) -> int:
    """Total bytes across the written files (small HEAD calls — no data read)."""
    total = 0
    for uri in file_uris:
        try:
            b, k = _parse_s3_uri(uri)
            total += _s3.head_size(b, k, region)
        except Exception:
            pass
    return total


def _run_athena_unload(select_sql: str, dest: str, *, format: str, compression: str,
                       partition_by, overwrite: bool, region: str) -> UnloadResult:
    """Shared, ROW-FREE UNLOAD runner used by BOTH unload_athena_to_s3() and the
    SQL-cell UNLOAD passthrough. Resolves cross-catalog refs, validates the dest,
    submits the UNLOAD, polls to SUCCEEDED, and reads the MANIFEST for the file list
    (never get_query_results, so no rows touch the VM). Returns an UnloadResult."""
    fmt = (format or "parquet").lower()
    if fmt not in _UNLOAD_FORMATS:
        raise ValueError(
            f"format={format!r} is not supported. Choose one of "
            f"{', '.join(_UNLOAD_FORMATS)} (parquet is the default).")

    # --- dest resolution + pre-flight validation -------------------------------
    dest = dest or _default_unload_dest()
    if not dest.endswith("/"):
        dest += "/"
    bucket, prefix = _parse_s3_uri(dest)
    # The exec role can only WRITE under microvm-sandbox-artifacts-*; warn/stop early
    # for any other bucket so the user gets an actionable message instead of a
    # mid-query AccessDenied minutes later.
    if not bucket.startswith("microvm-sandbox-artifacts-"):
        logger.warning(
            f"[unload] destination bucket {bucket!r} is outside the sandbox artifacts "
            f"bucket this notebook's role can write to. If the UNLOAD fails with "
            f"AccessDenied, use a prefix under microvm-sandbox-artifacts-* (the "
            f"default), or have an admin grant s3:PutObject on {bucket!r}.")

    # UNLOAD requires an EMPTY destination. The default dest is unique (always empty);
    # an explicit reused dest is checked here. If the empty-check itself can't reach
    # the bucket (doesn't exist, or the role can't list it), translate that into the
    # same actionable guidance rather than leaking a raw botocore error.
    try:
        existing = _s3.list_objects(bucket, prefix, max_keys=1, region=region)[0]
    except Exception as e:  # noqa: BLE001 — NoSuchBucket / AccessDenied / etc.
        raise RuntimeError(
            f"Cannot write UNLOAD output to s3://{bucket}/{prefix}: the destination "
            f"bucket is missing or not accessible to this notebook's role ("
            f"{type(e).__name__}). Use a prefix under microvm-sandbox-artifacts-* "
            f"(omit dest for a fresh one), or have an admin grant access to "
            f"{bucket!r}.") from e
    if existing:
        if not overwrite:
            raise RuntimeError(
                f"UNLOAD destination s3://{bucket}/{prefix} is not empty. UNLOAD "
                f"requires an empty prefix. Pass overwrite=True to replace it, or use "
                f"a different prefix (omit dest to get a fresh unique one).")
        # overwrite=True: clear the prefix first (scoped to the artifact bucket only).
        if not bucket.startswith("microvm-sandbox-artifacts-"):
            raise RuntimeError(
                f"Refusing to overwrite s3://{bucket}/{prefix}: overwrite only clears "
                f"prefixes under the sandbox artifacts bucket.")
        _clear_s3_prefix(None, bucket, prefix, region)

    # --- build the UNLOAD statement --------------------------------------------
    resolved_select = _resolve_unload_sql(select_sql)
    with_opts = [f"format = '{fmt.upper()}'"]
    if compression:
        with_opts.append(f"compression = '{compression.lower()}'")
    if partition_by:
        cols = ", ".join(f"'{c}'" for c in partition_by)
        with_opts.append(f"partitioned_by = ARRAY[{cols}]")
    unload_sql = (f"UNLOAD ({resolved_select})\nTO '{dest}'\n"
                  f"WITH ({', '.join(with_opts)})")

    return _submit_unload(unload_sql, dest, fmt, region)


def _submit_unload(unload_sql: str, dest: str, fmt: str, region: str) -> UnloadResult:
    """Submit a fully-formed UNLOAD statement, poll to SUCCEEDED, and read the MANIFEST
    for the file list (never get_query_results — no rows touch the VM). Shared by the
    helper (which builds the statement) and the SQL-cell passthrough (which gets a
    user-written statement). Runs on the shared runner (_athena.py): bounded by the
    cell timeout and cancelled with stop_query_execution on timeout or interrupt.
    Returns an UnloadResult."""
    def _enrich(reason: str) -> str:
        # Enrich the common write-permission failure with an actionable hint.
        low = reason.lower()
        if "access denied" in low or "403" in low or "not authorized" in low:
            reason += (f" — the query ran but Athena couldn't WRITE to {dest}. Use a "
                       f"prefix under microvm-sandbox-artifacts-* (the default dest), "
                       f"or grant s3:PutObject on that bucket to the exec role. "
                       f"(A failed UNLOAD may leave partial files — clear the prefix "
                       f"before retrying.)")
        return reason

    qid, athena = _execute_athena(
        unload_sql, workgroup=_ATHENA_WORKGROUP, region=region,
        output_location=f"s3://{_aws.artifact_bucket(region)}/athena-results/",
        fail_prefix="Athena UNLOAD failed", enrich_reason=_enrich)
    # --- read the manifest (file list), NOT the rows ---------------------------
    files = _read_unload_manifest(athena, qid, region)
    total_bytes = _sum_s3_sizes(files, region)
    logger.info(f"[unload] wrote {len(files)} {fmt} file(s) ({total_bytes} bytes) → {dest}")
    return UnloadResult(dest, files, total_bytes, fmt, qid)


# Match a leading UNLOAD and (optionally) capture an existing TO '...' destination.
_UNLOAD_LEAD_RE = re.compile(r"^\s*UNLOAD\b", re.IGNORECASE | re.DOTALL)
_UNLOAD_TO_RE = re.compile(r"\bTO\s+'([^']+)'", re.IGNORECASE)
_UNLOAD_OPEN_RE = re.compile(r"^\s*UNLOAD\s*\(", re.IGNORECASE | re.DOTALL)

# Leading SQL noise = whitespace, line comments (-- …), and block comments (/* … */)
# that appear BEFORE the first real token. We strip ONLY this leading run (not inner
# comments, which could legitimately be substrings of a string literal in the query)
# so the UNLOAD router and the body parser agree on where the statement starts.
_LEADING_SQL_NOISE_RE = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/)+", re.DOTALL)


def _strip_leading_sql_noise(sql: str) -> str:
    """Drop leading whitespace + leading SQL comments so a statement that opens with
    an explanatory `-- …` or `/* … */` comment still parses. Only the LEADING run is
    removed; comments later in the SQL are left for the engine to handle."""
    prev = None
    out = sql
    # Loop because a block comment can be followed by a line comment, etc.
    while out != prev:
        prev = out
        out = _LEADING_SQL_NOISE_RE.sub("", out, count=1)
    return out


def _extract_unload_body(raw_sql: str):
    """Return the inner query of `UNLOAD ( <query> ) TO … WITH …`, matching the
    OUTERMOST parenthesis group by balanced-paren scanning. A regex can't do this
    reliably because the inner query often contains its own parentheses — a WITH CTE
    (`WITH x AS ( … )`), function calls, subqueries — and a non-greedy `(.*?)` would
    stop at the first inner `)`. Returns the body string, or None if UNLOAD isn't a
    leading parenthesized statement. (Parens inside string literals are ignored.)

    Leading SQL comments are stripped first so a cell that opens with an explanatory
    `-- …` line still parses — the router (_is_unload_statement) also strips comments,
    so without this the two would DISAGREE: routed to UNLOAD, then rejected here."""
    raw_sql = _strip_leading_sql_noise(raw_sql)
    m = _UNLOAD_OPEN_RE.match(raw_sql)
    if not m:
        return None
    start = m.end()            # index just AFTER the opening '('
    depth = 1
    in_str = False
    i = start
    while i < len(raw_sql):
        ch = raw_sql[i]
        if in_str:
            if ch == "'":
                # Doubled '' is an escaped quote inside the literal — skip both.
                if i + 1 < len(raw_sql) and raw_sql[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return raw_sql[start:i].strip()
        i += 1
    return None  # unbalanced — no matching close paren


def _is_unload_statement(sql: str) -> bool:
    """True if `sql` is a leading UNLOAD statement (comments stripped). Used by the
    SQL-cell router to divert UNLOAD off the row-returning path."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    return bool(_UNLOAD_LEAD_RE.match(stripped))


def _run_unload_sql_cell(raw_sql: str, region: str = None) -> UnloadResult:
    """SQL-CELL front door for UNLOAD. The user writes a full
    `UNLOAD (SELECT …) TO 's3://…' WITH (…)` statement; we:
      • inject a DEFAULT TO (fresh unique prefix under the artifact bucket) when the
        user omits it, so UNLOAD-without-destination still works and the path is
        surfaced back to the cell;
      • apply the SAME cross-catalog qualification to the inner SELECT (so a join
        across Glue + S3 Tables resolves);
      • pre-flight validate the destination (writable bucket, empty prefix);
      • run through the shared row-free _submit_unload (manifest read, no rows).
    Returns an UnloadResult (the caller turns .files into the cell's result frame)."""
    region = region or _aws_region()
    # Strip leading comments/whitespace ONCE so the body parser, the TO lookup, and
    # the WITH-options lookup all operate on the same statement text — and so a cell
    # that opens with an explanatory `-- …` comment (the router already ignores it)
    # isn't rejected here. Inner comments are left for Athena.
    raw_sql = _strip_leading_sql_noise(raw_sql)
    select_sql = _extract_unload_body(raw_sql)
    if not select_sql:
        raise RuntimeError(
            "Could not parse the UNLOAD statement. Expected "
            "UNLOAD (<query>) TO 's3://...' WITH (format='PARQUET'). The <query> may "
            "be a SELECT or a WITH … SELECT CTE.")
    resolved_select = _resolve_unload_sql(select_sql)

    to_match = _UNLOAD_TO_RE.search(raw_sql)
    dest = to_match.group(1).strip() if to_match else _default_unload_dest()
    if not dest.endswith("/"):
        dest += "/"
    bucket, prefix = _parse_s3_uri(dest)
    if not bucket.startswith("microvm-sandbox-artifacts-"):
        logger.warning(
            f"[unload] destination bucket {bucket!r} is outside the sandbox artifacts "
            f"bucket this notebook's role can write to; the UNLOAD may fail with "
            f"AccessDenied. Use a prefix under microvm-sandbox-artifacts-*.")
    try:
        existing = _s3.list_objects(bucket, prefix, max_keys=1, region=region)[0]
    except Exception as e:  # noqa: BLE001 — NoSuchBucket / AccessDenied / etc.
        raise RuntimeError(
            f"Cannot write UNLOAD output to s3://{bucket}/{prefix}: the destination "
            f"bucket is missing or not accessible to this notebook's role ("
            f"{type(e).__name__}). Use a prefix under microvm-sandbox-artifacts-* "
            f"(or omit TO for a fresh one).") from e
    if existing and to_match:
        # Athena UNLOAD refuses a non-empty prefix. The SQL cell has no overwrite=
        # knob (SQL cells take no kwargs), and RE-RUNNING a cell is the most common
        # action — so for an explicit TO inside the SANDBOX artifacts bucket we
        # DEFAULT TO OVERWRITE: clear the prefix (prior output of THIS cell) and go.
        # Outside the sandbox bucket we don't own the data and the role can't write
        # there anyway, so we still refuse rather than delete someone else's objects.
        if bucket.startswith("microvm-sandbox-artifacts-"):
            logger.info(
                f"[unload] destination s3://{bucket}/{prefix} is not empty — clearing "
                f"it (overwrite) before re-running UNLOAD.")
            _clear_s3_prefix(None, bucket, prefix, region)
        else:
            raise RuntimeError(
                f"UNLOAD destination s3://{bucket}/{prefix} is not empty, and it's "
                f"outside the sandbox artifacts bucket this notebook can manage. Use "
                f"a prefix under microvm-sandbox-artifacts-* (or omit TO for a fresh "
                f"unique one).")

    # Preserve the user's WITH options; default to PARQUET when absent. Rebuild the
    # statement with the resolved SELECT + the resolved dest so both are consistent.
    with_match = re.search(r"\bWITH\s*\((.*)\)\s*;?\s*$", raw_sql,
                           re.IGNORECASE | re.DOTALL)
    with_clause = with_match.group(1).strip() if with_match else "format = 'PARQUET'"
    fmt_match = re.search(r"format\s*=\s*'([^']+)'", with_clause, re.IGNORECASE)
    fmt = (fmt_match.group(1).lower() if fmt_match else "parquet")
    unload_sql = f"UNLOAD ({resolved_select})\nTO '{dest}'\nWITH ({with_clause})"
    return _submit_unload(unload_sql, dest, fmt, region)


def _clear_s3_prefix(s3, bucket: str, prefix: str, region: str = None) -> None:
    """Delete every object under a prefix (paginated). Caller guarantees the bucket is
    the sandbox artifacts bucket. `s3` is an optional client (None = factory client)."""
    _s3.delete_prefix(bucket, prefix, region, client=s3)


@_log_read("athena")
def unload_athena_to_s3(sql: str, dest: str = None, *, format: str = "parquet",
                        compression: str = None, partition_by: list = None,
                        overwrite: bool = True) -> UnloadResult:
    """
    Run an Athena SELECT and have ATHENA write the result straight to S3 — WITHOUT
    pulling any rows into the VM. This is the big-data counterpart of read_athena():
    read_athena materializes results as a DataFrame (bounded by VM memory), while
    unload_athena_to_s3 runs the query on Athena's own fleet and lands the output in
    S3, returning only a pointer + file list. Use it to prep a large training set or
    reshape a huge table that would never fit in the VM.

        res = unload_athena_to_s3("SELECT * FROM microvm_demo_db.orders")
        res.s3_uri            # where the data landed
        res.files             # the part files Athena wrote
        # ...then feed res (or str(res)) to an S3-input consumer.

    CROSS-DATABASE JOINS work exactly like read_athena — qualify each table as
    db.table and the right catalog (Glue or the S3 Tables/Iceberg federated catalog)
    is resolved for you, including a join ACROSS catalogs in one query:

        res = unload_athena_to_s3('''
            SELECT o.*, e.status
            FROM microvm_demo_db.orders o
            JOIN microvm_demo_iceberg.order_events e ON o.order_id = e.order_id
            WHERE o.total > 100
        ''')

    Args:
      sql          the SELECT to run (NOT wrapped in UNLOAD — this wraps it). Qualify
                   tables as db.table (same contract as read_athena).
      dest         S3 prefix to write to. DEFAULT: a fresh, unique prefix under the
                   sandbox artifacts bucket (s3://<artifact-bucket>/unload/<ts>-<id>/)
                   — always empty, always writable, and returned in the result so you
                   never lose track of it. Pass your own 's3://...' to control it (it
                   must be under the artifacts bucket unless you've granted the exec
                   role s3:PutObject elsewhere).
      format       output format: parquet (DEFAULT), orc, avro, json, or textfile.
                   Parquet is columnar/typed/compressed — the right choice for ML and
                   downstream Athena/Spark. (Only parquet + textfile/csv feed sm_train.)
      compression  codec override, format-dependent (e.g. parquet: snappy [default],
                   gzip, zstd; textfile/json: gzip, bzip2). Leave None for the
                   format's sensible default (snappy for parquet).
      partition_by optional list of columns to partition the output by (max 100
                   partitions). Usually omit for ML training.
      overwrite    only relevant for an EXPLICIT dest you reuse. True (DEFAULT) clears
                   the prefix first so RE-RUNNING a cell just works (UNLOAD requires an
                   empty prefix); clearing is scoped to the sandbox artifacts bucket
                   only — a prefix outside it is never deleted and still errors. Pass
                   overwrite=False to instead error on a non-empty prefix. Irrelevant
                   for the default dest, which is always fresh.

    Returns an UnloadResult (s3_uri, files, file_count, total_bytes). str(result) is
    the s3_uri, so it drops straight into a downstream S3-input call.
    """
    return _run_athena_unload(
        sql, dest, format=format, compression=compression,
        partition_by=partition_by, overwrite=overwrite, region=_aws_region())


# =============================================================================
# DATA LOADING — SQL over in-memory DataFrames (DuckDB, in-process)
# =============================================================================
# read_* helpers pull data INTO DataFrames; query_df then lets you JOIN/aggregate
# those frames with plain SQL instead of multi-step pandas merges. DuckDB runs
# in-process (no server, no network) directly against the DataFrames — zero copy
# for the common case — so it's the natural "I have a few DataFrames, let me just
# SQL them together" primitive. duckdb is baked into the image.

def _caller_namespace() -> dict:
    """Return the USER cell's namespace — the dict query_df looks its DataFrames up
    in. The executor runs each cell with `exec(code, self._namespace)`, so the cell's
    module globals ARE that namespace. From inside query_df, the caller's frame is the
    cell, and its f_globals is exactly executor._namespace — so one hop up gives us
    the authoritative user namespace directly (no stack walk, no registration)."""
    import inspect
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back  # skip _caller_namespace + query_df → the cell
        # Merge globals + locals (locals win) so it also works when query_df is called
        # from inside a user-defined function, not just at cell top level.
        ns = dict(caller.f_globals)
        ns.update(caller.f_locals)
        return ns
    except Exception:
        return {}
    finally:
        del frame


def query_df(sql: str) -> pd.DataFrame:
    """
    Run SQL over the DataFrames already in your notebook with DuckDB (in-process) and
    return the result as a DataFrame — the clean way to JOIN / GROUP BY / window
    across frames you've loaded, instead of chaining pandas merges by hand.

    Reference a DataFrame in the SQL by its VARIABLE NAME — nothing to register. Every
    DataFrame currently in your namespace is queryable as a table of the same name:
        orders   = read_athena("SELECT * FROM orders_raw")
        products = read_s3("s3://bucket/products.parquet")
        top = query_df('''
            SELECT p.category, SUM(o.revenue) AS rev
            FROM orders o JOIN products p ON o.product_id = p.id
            GROUP BY 1 ORDER BY rev DESC LIMIT 10
        ''')

    Notes:
      • DuckDB SQL dialect (very close to standard SQL / Postgres) — the SAME engine
        the notebook's SQL cells use, so behavior matches.
      • Runs entirely in the VM against the DataFrames already in memory: no AWS call,
        no network. For warehouse-scale SQL use read_athena / read_aurora.
      • Read-only over your frames: returns a NEW DataFrame, never mutates the inputs.
    """
    try:
        import duckdb
    except ImportError:  # duckdb is baked into the image; guard anyway
        raise RuntimeError(
            "query_df needs duckdb, which isn't available in this runtime. "
            "Install it with install_package('duckdb'), then retry."
        )

    # Every public DataFrame in the user's namespace is queryable by its variable name.
    ns = _caller_namespace()
    frames = {name: val for name, val in ns.items()
              if isinstance(val, pd.DataFrame) and name.isidentifier()
              and not name.startswith("_")}

    if not frames:
        raise ValueError(
            "query_df: no DataFrames in your namespace to query. Load one first "
            "(e.g. df = read_s3(...)) and reference it by name in the SQL."
        )

    con = duckdb.connect()  # fresh in-memory connection per call (isolated, cheap)
    try:
        for name, df in frames.items():
            # register() views the DataFrame zero-copy; no data is duplicated. Only the
            # frames the SQL actually references are scanned by DuckDB.
            con.register(name, df)
        return con.execute(sql).df()
    except Exception as e:
        # DuckDB's "Table with name X does not exist" is the usual error when the SQL
        # names a frame that isn't defined. Make it actionable by listing what IS.
        available = ", ".join(sorted(frames)) or "(none)"
        raise RuntimeError(
            f"query_df failed: {e}\nDataFrames you can query right now: {available}. "
            f"Reference one of those by name in your SQL."
        ) from e
    finally:
        con.close()
