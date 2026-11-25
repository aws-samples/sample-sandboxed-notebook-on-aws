"""DATA EXPORT — Write to S3 and local."""

import os
import io

import boto3
import pandas as pd

from ._common import _log_write
from ._common import _REGION
from ..aws_clients import aws_region as _aws_region
from .readers import _resolve_s3_target
from . import _s3


# =============================================================================
# DATA EXPORT — Write to S3 and local
# =============================================================================

@_log_write("s3")
def to_s3_csv(df: pd.DataFrame, bucket: str, key: str = None, index: bool = False) -> str:
    """
    Upload a DataFrame as CSV to S3.
    
    Accepts either (df, bucket, key) or (df, full_s3_uri):
        to_s3_csv(df, "my-bucket", "user-data/output.csv")
        to_s3_csv(df, "s3://my-bucket/user-data/output.csv")

    Returns the S3 URI of the uploaded file.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=index)
    _s3.put_bytes(bucket, key, csv_buffer.getvalue(), content_type='text/csv', region=_aws_region())
    return f"s3://{bucket}/{key}"


@_log_write("s3")
def to_s3_parquet(df: pd.DataFrame, bucket: str, key: str = None) -> str:
    """
    Upload a DataFrame as Parquet to S3.
    
    Accepts either (df, bucket, key) or (df, full_s3_uri):
        to_s3_parquet(df, "my-bucket", "user-data/output.parquet")
        to_s3_parquet(df, "s3://my-bucket/user-data/output.parquet")

    Returns the S3 URI of the uploaded file.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    parquet_buffer = io.BytesIO()
    df.to_parquet(parquet_buffer, index=False)
    _s3.put_bytes(bucket, key, parquet_buffer.getvalue(), content_type='application/octet-stream',
                  region=_aws_region())
    return f"s3://{bucket}/{key}"


@_log_write("s3")
def to_s3_json(df: pd.DataFrame, bucket: str, key: str = None, orient: str = 'records') -> str:
    """
    Upload a DataFrame as JSON to S3.
    
    Accepts either (df, bucket, key) or (df, full_s3_uri):
        to_s3_json(df, "my-bucket", "user-data/output.json")
        to_s3_json(df, "s3://my-bucket/user-data/output.json")

    Returns the S3 URI of the uploaded file.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    json_str = df.to_json(orient=orient, indent=2)
    _s3.put_bytes(bucket, key, json_str, content_type='application/json', region=_aws_region())
    return f"s3://{bucket}/{key}"


@_log_write("s3")
def to_s3_excel(df: pd.DataFrame, bucket: str, key: str = None,
                sheet: str = "Sheet1", index: bool = False) -> str:
    """
    Upload a DataFrame as an Excel (.xlsx) file to S3.

    Accepts either (df, bucket, key) or (df, full_s3_uri):
        to_s3_excel(df, "my-bucket", "reports/q4.xlsx")
        to_s3_excel(df, "s3://my-bucket/reports/q4.xlsx", sheet="Summary")

    Returns the S3 URI of the uploaded file.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    xlsx_buffer = io.BytesIO()
    # openpyxl is baked into the image; it's the default .xlsx writer engine.
    with pd.ExcelWriter(xlsx_buffer, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet, index=index)
    _s3.put_bytes(
        bucket, key, xlsx_buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        region=_aws_region(),
    )
    return f"s3://{bucket}/{key}"


@_log_write("s3")
def upload_s3(data, bucket: str, key: str = None) -> str:
    """
    Upload an arbitrary artifact to S3 — a local file OR raw bytes. This is the
    inverse of download_s3, for NON-tabular objects the to_s3_* DataFrame writers
    don't cover: a serialized ONNX model, a saved figure, a zip, a PDF, etc.

    `data` is either:
        • a local file path (str starting with '/', e.g. '/tmp/model.onnx') — the
          file's contents are uploaded; or
        • raw bytes already in memory (e.g. onnx_model.SerializeToString()).

    Accepts (data, bucket, key) or (data, full_s3_uri):
        upload_s3('/tmp/model.onnx', 'my-bucket', 'models/model.onnx')
        upload_s3(onx.SerializeToString(), 'my-bucket', 'models/model.onnx')
        upload_s3(png_bytes, 's3://my-bucket/plots/chart.png')

    For a fitted scikit-learn estimator use save_model (it joblib-serializes for
    you); for a DataFrame use to_s3_csv/parquet/json/excel. Returns the S3 URI.
    """
    bucket, key = _resolve_s3_target(bucket, key)
    if isinstance(data, str):
        # A local path → read its bytes (must be a real file, not a bytes-ish str).
        if not os.path.exists(data):
            raise FileNotFoundError(
                f"upload_s3: '{data}' is not a file on the sandbox. Pass a local "
                f"path (e.g. '/tmp/model.onnx') or raw bytes."
            )
        with open(data, "rb") as f:
            body = f.read()
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    else:
        raise TypeError(
            "upload_s3 expects a local file PATH (str) or raw BYTES. For a "
            "DataFrame use to_s3_csv/parquet/json/excel; for a fitted model use "
            "save_model."
        )
    _s3.put_bytes(bucket, key, body, content_type='application/octet-stream',
                  region=_aws_region())
    return f"s3://{bucket}/{key}"


# Local writes are confined to /tmp — the only writable location in the sandbox
# VM (the rest of the filesystem is read-only). This app-level check gives a
# clear, early error in the notebook UI; the OS enforces the same boundary as a
# hard backstop for code that writes files directly.
_WRITABLE_ROOT = "/tmp"


def _require_tmp_path(path: str) -> None:
    """Validate a local write target resolves to inside /tmp, else ValueError.

    Resolves symlinks and '..' before checking so tricks like '/tmp/../etc/x' or
    a symlink out of /tmp are caught, not just literal prefixes. Validation only —
    it does NOT alter the caller's path (we write to the path the user gave).
    """
    resolved = os.path.realpath(path)
    root = os.path.realpath(_WRITABLE_ROOT)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(
            f"Cannot write to '{path}': the sandbox only allows writing under "
            f"{_WRITABLE_ROOT}/. Save your file to a {_WRITABLE_ROOT}/ path "
            f"(e.g. '{_WRITABLE_ROOT}/output.csv')."
        )


@_log_write("local")
def to_local(df: pd.DataFrame, path: str, index: bool = False) -> str:
    """
    Save a DataFrame to a local file. Auto-detects format from extension.

    Writes are restricted to /tmp (the sandbox's only writable directory).

    Example:
        to_local(df, '/tmp/output.csv')
        to_local(df, '/tmp/output.parquet')
    """
    _require_tmp_path(path)  # validate only; write to the path the user gave
    ext = path.rsplit('.', 1)[-1].lower() if '.' in path else 'csv'
    if ext == 'csv':
        df.to_csv(path, index=index)
    elif ext == 'parquet':
        df.to_parquet(path, index=False)
    elif ext == 'json':
        df.to_json(path, orient='records', indent=2)
    elif ext in ('xlsx', 'xls'):
        df.to_excel(path, index=index)
    else:
        df.to_csv(path, index=index)
    return path


@_log_write("local")
def to_local_prefix(df: pd.DataFrame, path: str, parts: int = 4,
                    format: str = "parquet", index: bool = False) -> str:
    """
    Save a DataFrame as MULTIPLE part-files under a /tmp FOLDER — the write parallel
    to read_local_prefix (and the local mirror of a Spark/Athena partitioned export).
    The rows are split into `parts` roughly-equal chunks, each written as
    part-0000.<ext>, part-0001.<ext>, … so read_local_prefix(path) reads them all
    back into one DataFrame.

    Args:
      path:   destination FOLDER under /tmp (created if missing), e.g.
              '/tmp/orders_parts/'. Writes are restricted to /tmp.
      parts:  how many part-files to split into (default 4). Clamped to
              1..len(df) — you can't have more parts than rows (an empty frame
              writes a single empty part).
      format: 'parquet' (default), 'csv', or 'json' — the part-file format/extension.
      index:  include the DataFrame index in each part (csv/json only; default False).

    Returns the folder path.

    NOTE: existing part-*.{parquet,csv,json} files in the folder are DELETED first,
    so a re-run replaces the dataset cleanly (no stale parts mixed with new ones).
    Other files in the folder are left untouched.

    Example:
        to_local_prefix(df, '/tmp/orders_parts/', parts=4)   # part-0000.parquet … part-0003.parquet
        back = read_local_prefix('/tmp/orders_parts/')        # clean round-trip
    """
    import glob as _glob

    fmt = (format or "parquet").lower()
    if fmt not in ("parquet", "csv", "json"):
        raise ValueError(
            f"to_local_prefix: unsupported format {format!r}. Use 'parquet', 'csv', "
            f"or 'json'."
        )
    ext = fmt

    # Normalize to a directory path and validate it's inside /tmp (same guard as
    # to_local). realpath-based, so '..'/symlink escapes are caught.
    folder = path.rstrip("/") or path
    _require_tmp_path(folder)
    os.makedirs(folder, exist_ok=True)

    # Clear existing parts for a clean replace (only our part-*.{ext} naming for the
    # chosen format — leave any other files in the folder alone).
    for old in _glob.glob(os.path.join(folder, f"part-*.{ext}")):
        try:
            os.remove(old)
        except OSError:
            pass

    # Split row-wise into EXACTLY `k` roughly-equal, CONTIGUOUS chunks via positional
    # slicing (keeps each chunk a DataFrame — np.array_split would coerce to ndarray).
    # Clamp k to 1..len(df): can't have more parts than rows. The boundary math is
    # the same even split numpy.array_split uses — the first (n % k) chunks get one
    # extra row — so uneven divisions still yield k parts (e.g. n=5,k=4 → 2,1,1,1),
    # NOT fewer (a plain ceil-size slice would emit only 3 here). An empty frame
    # still writes exactly one (empty) part so the folder stays readable.
    n = len(df)
    if n == 0:
        chunks = [df]
    else:
        k = max(1, min(int(parts), n))
        base, extra = divmod(n, k)  # extra chunks get one more row than base
        chunks = []
        start = 0
        for j in range(k):
            length = base + (1 if j < extra else 0)
            chunks.append(df.iloc[start:start + length])
            start += length

    written = []
    for i, chunk in enumerate(chunks):
        part_path = os.path.join(folder, f"part-{i:04d}.{ext}")
        if ext == "parquet":
            chunk.to_parquet(part_path, index=False)
        elif ext == "csv":
            chunk.to_csv(part_path, index=index)
        else:  # json
            chunk.to_json(part_path, orient="records", indent=2)
        written.append(part_path)

    return folder
