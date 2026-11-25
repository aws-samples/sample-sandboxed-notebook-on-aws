#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: the ONE S3 object module (helpers/_s3.py) and the helpers built on it.
Fake S3 client with a paged list_objects_v2 (ContinuationToken, page size 3).
Covers list_keys / list_objects, list_s3, read_s3_prefix (one listing, all pages),
sample_data (top level and sub-prefix, 2+ pages), get/put bytes and the to_s3_csv
body, delete_prefix over 2 pages, and the URI parser.

Run: .venv/bin/python tests/vm/test_s3_module.py
"""
import datetime
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["ARTIFACT_BUCKET"] = "microvm-sandbox-artifacts-test"

import boto3  # noqa: E402
import pandas as pd  # noqa: E402

from app.notebook import helpers as H  # noqa: E402
from app.notebook.helpers import _s3  # noqa: E402

logging.disable(logging.WARNING)
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


NOW = datetime.datetime(2026, 1, 1)
PAGE = 3


class FakeS3:
    def __init__(self, store):
        self.store = store          # key -> bytes
        self.list_calls = []
        self.deleted = []
        self.delete_calls = 0
        self.put = []

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, MaxKeys=1000,
                        ContinuationToken=None):
        self.list_calls.append({"Prefix": Prefix, "Delimiter": Delimiter, "MaxKeys": MaxKeys,
                                "token": ContinuationToken})
        keys = sorted(k for k in self.store if k.startswith(Prefix))
        prefixes = []
        if Delimiter:
            direct = []
            for k in keys:
                rest = k[len(Prefix):]
                if Delimiter in rest:
                    p = Prefix + rest.split(Delimiter, 1)[0] + Delimiter
                    if p not in prefixes:
                        prefixes.append(p)
                else:
                    direct.append(k)
            keys = direct
        # Like S3, the token is key-based (last key returned), so deleting keys
        # between pages does not shift the next page.
        start = (sum(1 for k in keys if k <= ContinuationToken) if ContinuationToken else 0)
        n = min(PAGE, MaxKeys)
        page = keys[start:start + n]
        resp = {"Contents": [{"Key": k, "Size": len(self.store[k]), "LastModified": NOW}
                             for k in page],
                "CommonPrefixes": [{"Prefix": p} for p in prefixes] if start == 0 else [],
                "IsTruncated": start + n < len(keys)}
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = page[-1]
        return resp

    def get_object(self, Bucket, Key, Range=None):
        import io
        body = self.store[Key]
        return {"Body": io.BytesIO(body)}

    def put_object(self, **kw):
        self.put.append(kw)
        self.store[kw["Key"]] = kw["Body"] if isinstance(kw["Body"], bytes) else kw["Body"].encode()

    def delete_objects(self, Bucket, Delete):
        self.delete_calls += 1
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.store.pop(o["Key"], None)


store = {f"data/part-{i}.csv": b"a,b\n1,2\n" for i in range(8)}
store["data/_SUCCESS"] = b""
fake = FakeS3(store)
boto3.client = lambda service, **k: fake

# ── list_keys / list_objects ──────────────────────────────────────────────────
keys = _s3.list_keys("b", "data/")
check("list_keys returns all 9 keys over 3 pages", len(keys) == 9 and len(fake.list_calls) == 3,
      (len(keys), len(fake.list_calls)))
check("later pages send ContinuationToken", fake.list_calls[1]["token"] == "data/part-1.csv")
fake.list_calls.clear()
keys = _s3.list_keys("b", "data/", max_keys=4)
check("max_keys stops early", len(keys) == 4 and len(fake.list_calls) == 2)

# ── list_s3 ───────────────────────────────────────────────────────────────────
fake.list_calls.clear()
df = H.list_s3("b", "data/", max_keys=7)
check("list_s3 returns more than one page (7 rows)", len(df) == 7 and len(fake.list_calls) == 3)
df = H.list_s3("s3://b/data/", max_keys=100)
check("list_s3 from a URI lists everything", len(df) == 9)

# ── read_s3_prefix: one listing, all pages, one concat ───────────────────────
fake.list_calls.clear()
df = H.read_s3_prefix("s3://b/data/")
check("read_s3_prefix reads all 8 data files across pages (skips _SUCCESS)", len(df) == 8)

# ── get/put bytes + export body ──────────────────────────────────────────────
src = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
uri = H.to_s3_csv(src, "b", "out/x.csv")
check("to_s3_csv uri", uri == "s3://b/out/x.csv")
check("to_s3_csv body equals df.to_csv(index=False)",
      fake.store["out/x.csv"] == src.to_csv(index=False).encode())
check("to_s3_csv content type", fake.put[-1]["ContentType"] == "text/csv")
check("get_bytes round trip", _s3.get_bytes("b", "out/x.csv") == fake.store["out/x.csv"])
check("read_s3_csv round trip", H.read_s3_csv("b", "out/x.csv").equals(src))
_s3.put_bytes("b", "k.bin", b"zz", content_type="application/octet-stream")
check("put_bytes stores the body", fake.store["k.bin"] == b"zz")

# ── delete_prefix over 2+ pages ───────────────────────────────────────────────
dstore = {f"del/o{i}": b"x" for i in range(7)}
dstore["keep/o"] = b"x"
dfake = FakeS3(dstore)
boto3.client = lambda service, **k: dfake
n = _s3.delete_prefix("b", "del/")
check("delete_prefix deletes all 7 keys", n == 7 and "keep/o" in dfake.store and
      not any(k.startswith("del/") for k in dfake.store))
check("one delete_objects per page (3)", dfake.delete_calls == 3, dfake.delete_calls)

# ── sample_data over 2+ pages ─────────────────────────────────────────────────
sstore = {f"samples/top{i}.csv": b"c\n1\n" for i in range(4)}
sstore.update({f"samples/orders/part{i}.csv": b"c\n1\n" for i in range(5)})
sstore["samples/orders/readme.txt"] = b"x"
sfake = FakeS3(sstore)
boto3.client = lambda service, **k: sfake
import io  # noqa: E402
import contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    H.sample_data()
out = buf.getvalue()
names = out.split("Available datasets:")[1].splitlines()[0]
check("sample_data lists top-level datasets from page 2 (top3)", "top3" in names, out)
check("sample_data lists sub-prefix datasets past page 1 (part4)", "part4" in names, out)

# ── parser ────────────────────────────────────────────────────────────────────
check("parse_s3_uri splits bucket and key", _s3.parse_s3_uri("s3://bkt/a/b/") == ("bkt", "a/b/"))
try:
    _s3.parse_s3_uri("http://x/y")
    check("parse_s3_uri rejects non-s3", False)
except ValueError as e:
    check("parse_s3_uri rejects non-s3 with the existing message", "Not an S3 URI" in str(e))
check("split_bucket_prefix handles bucket only / path / uri",
      [_s3.split_bucket_prefix(x) for x in ("b", "b/p/q", "s3://b/p/")] ==
      [("b", ""), ("b", "p/q"), ("b", "p/")])

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
