"""One S3 object module for the VM helpers: paginated listing, get/put bytes,
prefix delete and the bucket/key parsers.

Listing follows ContinuationToken to the end (or to max_keys), so no caller is
limited to the first 1000 keys. Clients come from the factory
(app.notebook.aws_clients), which caches them per region.
Underscore-prefixed so helpers/__init__ does not export it to notebook cells.
"""
import re

from .. import aws_clients as _aws


def s3_client(region=None):
    return _aws.aws_client("s3", region)


def parse_s3_uri(uri: str) -> tuple:
    """'s3://bucket/key/prefix/' -> ('bucket', 'key/prefix/'). Raises on a non-s3 URI."""
    m = re.match(r"s3://([^/]+)/?(.*)$", uri.strip())
    if not m:
        raise ValueError(f"Not an S3 URI: {uri!r} (expected 's3://bucket/prefix/').")
    return m.group(1), m.group(2)


def split_bucket_prefix(ref: str) -> tuple:
    """'bucket', 'bucket/prefix', 's3://bucket/prefix' -> (bucket, prefix)."""
    ref = (ref or "").strip()
    if ref.startswith("s3://"):
        ref = ref[len("s3://"):]
    if "/" in ref:
        bucket, prefix = ref.split("/", 1)
        return bucket, prefix
    return ref, ""


def list_objects(bucket, prefix="", *, delimiter=None, max_keys=None, region=None):
    """List objects under a prefix across all pages.

    Returns (objects, common_prefixes): the raw `Contents` dicts and the
    `CommonPrefixes` strings (only with a delimiter). max_keys stops the listing
    once that many objects were collected."""
    s3 = s3_client(region)
    objects, prefixes = [], []
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix or ""}
        if delimiter:
            params["Delimiter"] = delimiter
        if max_keys is not None:
            params["MaxKeys"] = max(1, min(1000, max_keys - len(objects)))
        if token:
            params["ContinuationToken"] = token
        resp = s3.list_objects_v2(**params)
        objects.extend(resp.get("Contents", []))
        prefixes.extend(p["Prefix"] for p in resp.get("CommonPrefixes", []))
        if max_keys is not None and len(objects) >= max_keys:
            return objects[:max_keys], prefixes
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated") or not token:
            return objects, prefixes


def list_keys(bucket, prefix="", *, max_keys=None, region=None):
    """Keys under a prefix across all pages (see list_objects)."""
    objects, _ = list_objects(bucket, prefix, max_keys=max_keys, region=region)
    return [o["Key"] for o in objects]


def get_bytes(bucket, key, *, byte_range=None, region=None) -> bytes:
    """Object body as bytes. byte_range is an HTTP range such as 'bytes=0-65536'."""
    params = {"Bucket": bucket, "Key": key}
    if byte_range:
        params["Range"] = byte_range
    return s3_client(region).get_object(**params)["Body"].read()


def put_bytes(bucket, key, body, *, content_type=None, region=None) -> None:
    params = {"Bucket": bucket, "Key": key, "Body": body}
    if content_type:
        params["ContentType"] = content_type
    s3_client(region).put_object(**params)


def head_size(bucket, key, region=None) -> int:
    return s3_client(region).head_object(Bucket=bucket, Key=key)["ContentLength"]


def delete_prefix(bucket, prefix, region=None, *, client=None) -> int:
    """Delete every object under a prefix, one delete_objects call per listing page
    (so a prefix of any size is cleared). Caller guarantees the bucket is one it may
    clear. Returns the number of keys deleted."""
    s3 = client or s3_client(region)
    deleted = 0
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix}
        if token:
            params["ContinuationToken"] = token
        resp = s3.list_objects_v2(**params)
        objs = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
            deleted += len(objs)
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated") or not token:
            return deleted
