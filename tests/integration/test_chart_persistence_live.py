#!/usr/bin/env python3
# test-kind: manual
"""
LIVE integration test — chart (matplotlib PNG) persistence to S3.

Run ONLY after the proxy restart / image rebuild that ships this change and the
go-live reset (`local_install.sh --AuroraSetup --AuroraReset --WarmPool`):

    env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \\
        .venv/bin/python tests/integration/test_chart_persistence_live.py

Creates its own throwaway notebooks (A with a VM, B without) and cleans up the
notebooks, the VM and every S3 object under their notebook-outputs/ prefixes.

Checks:
  1. single execute: POST /proxy/execute (cell_id c1, a matplotlib chart) returns
     the base64 image AND image_s3_key = notebook-outputs/<A>/c1/<sha256>.png;
     the S3 object appears (head_object, real credentials) with ContentType
     image/png and SSE; the object's sha256 matches the key.
  2. persistence: PUT /notebooks/A with the key → GET /notebooks/A returns it
     (no base64 in the GET).
  3. route: GET /notebooks/A/cells/c1/image → 200 PNG with X-Proxy-Auth, 401
     without and with a wrong token; notebook B's cell id → 404; a PUT of A's key
     on B stores NULL (GET B has no key) and B's route → 404.
  4. Run All: POST /proxy/execute-batch (c1 chart + c2 text + c3 chart): the
     chart cell_result frames carry image_s3_key, the text frame does not; the
     objects exist after the save.
  5. clear + re-run: PUT c1 with imageS3Key null → GET has no key (no dangling DB
     reference; the object itself is kept for the 600 s recent-put grace by
     design and removed with the notebook); re-running the identical chart and
     saving the key → object exists, route 200.
  6. privacy (read-only): the artifacts bucket's Block Public Access has all four
     settings on, and the chart object's ACL has no AllUsers/AuthenticatedUsers
     grant.
  7. delete: DELETE /notebooks/A → GET 404, and no object is left under
     notebook-outputs/<A>/ (polled ≤ 20 s).

Exit 0 = all checks passed; 1 = a failure or the stack wasn't reachable. The
proxy token is read from the running proxy's environment and only ever sent as
a request header (never printed).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8081").rstrip("/")
AUTH_HEADER = "X-Proxy-Auth"
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
SESSION_ID = str(uuid.uuid4())
KEY_RE = re.compile(r"^notebook-outputs/(\d+)/([A-Za-z0-9_-]{1,130})/([0-9a-f]{64})\.png$")

_failures: list[str] = []


def ok(msg):   print(f"  [PASS] {msg}")
def bad(msg):  print(f"  [FAIL] {msg}"); _failures.append(msg)
def info(msg): print(f"  ... {msg}")
def head(msg): print(f"\n== {msg} ==")


def check(cond, msg, detail=""):
    (ok if cond else bad)(msg + (f" — {detail}" if detail and not cond else ""))
    return cond


CHART = ("import matplotlib.pyplot as plt\n"
         "plt.figure(figsize=(4, 3))\n"
         "plt.barh(['alpha', 'beta', 'gamma'], [3, 1, 2])\n"
         "plt.title('chart persistence live test')\n"
         "plt.show()\n")
CHART2 = ("import matplotlib.pyplot as plt\n"
          "fig, ax = plt.subplots(figsize=(4, 3))\n"
          "ax.plot([1, 2, 3], [3, 1, 2])\n"
          "plt.show()\n")
CELLS = [
    {"id": "c1", "type": "code", "code": CHART},
    {"id": "c2", "type": "code", "code": "print('no chart here')\n"},
    {"id": "c3", "type": "code", "code": CHART2},
]


def _token() -> str:
    if os.environ.get("PROXY_AUTH_TOKEN"):
        return os.environ["PROXY_AUTH_TOKEN"]
    pid = subprocess.run(["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    if not pid:
        return ""
    env = subprocess.run(["ps", "eww", "-p", pid[0]], capture_output=True, text=True).stdout
    for tok in env.split():
        if tok.startswith("PROXY_AUTH_TOKEN="):
            return tok.split("=", 1)[1]
    return ""


def _proxy_env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    pid = subprocess.run(["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    if not pid:
        return ""
    env = subprocess.run(["ps", "eww", "-p", pid[0]], capture_output=True, text=True).stdout
    for tok in env.split():
        if tok.startswith(name + "="):
            return tok.split("=", 1)[1]
    return ""


TOKEN = _token()
BUCKET = _proxy_env("ARTIFACT_BUCKET")


def raw_call(method, path, body=None, *, timeout=180, session=True, token=None, auth=True):
    """(status, headers, bytes) — never raises on an HTTP status."""
    headers = {"Content-Type": "application/json"}
    if auth:
        headers[AUTH_HEADER] = TOKEN if token is None else token
    if session:
        headers["X-Session-Id"] = SESSION_ID
    req = urllib.request.Request(PROXY_URL + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def call(method, path, body=None, **kw):
    st, _, raw = raw_call(method, path, body, **kw)
    if st >= 400:
        raise RuntimeError(f"{method} {path} → {st}: {raw[:200]!r}")
    return json.loads(raw) if raw else {}


def call_retry(method, path, body=None, attempts=10, **kw):
    for i in range(attempts):
        st, _, raw = raw_call(method, path, body, **kw)
        if st < 400:
            return json.loads(raw) if raw else {}
        if st not in (404, 502, 503) or i == attempts - 1:
            raise RuntimeError(f"{method} {path} → {st}: {raw[:200]!r}")
        time.sleep(4)


def s3():
    import boto3
    return boto3.client("s3", region_name=AWS_REGION)


def wait_object(key, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            return s3().head_object(Bucket=BUCKET, Key=key)
        except Exception:
            time.sleep(1)
    return None


def list_prefix(nid):
    out = []
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": f"notebook-outputs/{int(nid)}/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3().list_objects_v2(**kw)
        out += [o["Key"] for o in page.get("Contents") or []]
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def purge_prefix(nid):
    keys = list_prefix(nid)
    for i in range(0, len(keys), 1000):
        s3().delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})


def save(nid, keys):
    cells = [dict(c, imageS3Key=keys.get(c["id"])) for c in CELLS]
    call("PUT", f"/notebooks/{nid}", {"cells": cells}, session=False)


def stored_keys(nid):
    nb = call("GET", f"/notebooks/{nid}", session=False)
    return {c["id"]: c.get("imageS3Key") for c in nb.get("cells") or []}, nb


def sse_frames(raw: bytes):
    return [json.loads(f[6:]) for f in raw.decode().split("\n\n") if f.startswith("data: ")]


STATE: dict = {"vm": None, "nids": [], "created": []}


def run(nid_a, nid_b):
    head("1. single execute")
    vm_res = call("POST", "/launch", {"notebookName": "[live test] chart persistence",
                                      "notebookId": str(nid_a), "memoryMiB": 2048,
                                      "idleTimeoutSeconds": 300, "checkpointEnabled": False,
                                      "sessionId": SESSION_ID}, timeout=300)
    STATE["vm"] = vm_res.get("microvmId") or vm_res.get("microvm_id")
    info(f"launched {STATE['vm']}")
    r = call_retry("POST", "/proxy/execute", {"code": CHART, "cell_id": "c1"}, timeout=300)
    key1 = r.get("image_s3_key")
    m = KEY_RE.match(key1 or "")
    check(bool(r.get("image", "").startswith("data:image/png;base64,")), "execute returns the base64 chart")
    check(bool(m) and m.group(1) == str(nid_a) and m.group(2) == "c1",
          "execute returns image_s3_key notebook-outputs/<A>/c1/<sha>.png", str(key1))
    if not m:
        return
    obj = wait_object(key1)
    check(obj is not None, "the S3 object exists at the returned key")
    if obj:
        check(obj.get("ContentType") == "image/png", "object ContentType image/png")
        check(obj.get("ServerSideEncryption") in ("AES256", "aws:kms"), "object is encrypted at rest")
        body = s3().get_object(Bucket=BUCKET, Key=key1)["Body"].read()
        check(hashlib.sha256(body).hexdigest() == m.group(3) and body.startswith(b"\x89PNG"),
              "object bytes are the PNG whose sha256 is in the key")

    head("2. persistence")
    save(nid_a, {"c1": key1})
    keys, nb = stored_keys(nid_a)
    check(keys.get("c1") == key1, "GET /notebooks/A returns imageS3Key")
    check(all(not c.get("image") for c in nb.get("cells") or []), "GET carries no base64 image")

    head("3. image route")
    url = f"/notebooks/{nid_a}/cells/c1/image"
    st, hdrs, png = raw_call("GET", url, session=False)
    check(st == 200 and png.startswith(b"\x89PNG") and hashlib.sha256(png).hexdigest() == m.group(3),
          "route returns the PNG with X-Proxy-Auth", str(st))
    check((hdrs.get("Content-Type") or hdrs.get("content-type")) == "image/png", "route Content-Type image/png")
    st, _, b = raw_call("GET", url, session=False, auth=False)
    check(st == 401 and not b.startswith(b"\x89PNG"), "route without X-Proxy-Auth → 401", str(st))
    st, _, b = raw_call("GET", url, session=False, token="not-the-token")
    check(st == 401 and not b.startswith(b"\x89PNG"), "route with a wrong token → 401", str(st))
    st, _, b = raw_call("GET", f"/notebooks/{nid_b}/cells/c1/image", session=False)
    check(st == 404, "notebook B's cell id → 404", str(st))
    call("PUT", f"/notebooks/{nid_b}", {"cells": [{"id": "c1", "imageS3Key": key1}]}, session=False)
    kb, _ = stored_keys(nid_b)
    check(not kb.get("c1"), "PUT of A's key on B stores NULL")
    st, _, b = raw_call("GET", f"/notebooks/{nid_b}/cells/c1/image", session=False)
    check(st == 404 and not b.startswith(b"\x89PNG"), "…and B's route → 404", str(st))
    st, _, _ = raw_call("GET", f"{url}?sha={'0' * 64}", session=False)
    check(st == 404, "a stale sha → 404", str(st))
    check(wait_object(key1, 2) is not None, "the foreign PUT on B did not delete A's object")

    head("4. Run All (execute-batch)")
    st, _, raw = raw_call("POST", "/proxy/execute-batch", {"cells": [
        {"cell_id": c["id"], "type": "code", "code": c["code"]} for c in CELLS]}, timeout=300)
    frames = sse_frames(raw) if st == 200 else []
    results = {f.get("cell_id"): f.get("result") or {} for f in frames if f.get("type") == "cell_result"}
    bk1 = (results.get("c1") or {}).get("image_s3_key")
    bk3 = (results.get("c3") or {}).get("image_s3_key")
    check(st == 200 and set(results) == {"c1", "c2", "c3"}, "batch streamed 3 cell_result frames",
          f"{st} {sorted(results)}")
    check(bool(KEY_RE.match(bk1 or "")) and f"/{nid_a}/c1/" in (bk1 or ""), "batch c1 frame carries its key")
    check(bool(KEY_RE.match(bk3 or "")) and f"/{nid_a}/c3/" in (bk3 or ""), "batch c3 frame carries its key")
    check("image_s3_key" not in (results.get("c2") or {}), "batch text-only frame has no key")
    check(any(f.get("type") == "dag" for f in frames), "batch still emits the dag frame")
    save(nid_a, {"c1": bk1, "c3": bk3})
    check(wait_object(bk1) is not None and wait_object(bk3) is not None, "batch chart objects exist")
    keys, _ = stored_keys(nid_a)
    check(keys.get("c1") == bk1 and keys.get("c3") == bk3, "batch keys persisted")

    head("5. clear + re-run")
    save(nid_a, {"c3": bk3})
    keys, _ = stored_keys(nid_a)
    check(keys.get("c1") is None, "cleared c1 → no DB reference")
    st, _, _ = raw_call("GET", url, session=False)
    check(st == 404, "cleared cell's route → 404 (UI shows 'Chart unavailable')", str(st))
    r = call("POST", "/proxy/execute", {"code": CHART, "cell_id": "c1"}, timeout=300)
    rk = r.get("image_s3_key")
    save(nid_a, {"c1": rk, "c3": bk3})
    check(wait_object(rk) is not None, "re-run of the identical chart: object exists at the saved key")
    st, _, png = raw_call("GET", url, session=False)
    check(st == 200 and png.startswith(b"\x89PNG"), "route 200 after the re-run", str(st))

    head("6. privacy (read-only)")
    try:
        pab = s3().get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"]
        check(all(pab.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls",
                                       "BlockPublicPolicy", "RestrictPublicBuckets")),
              "bucket Block Public Access: all four settings on", str(pab))
    except Exception as e:
        bad(f"get_public_access_block failed: {type(e).__name__}")
    try:
        grants = s3().get_object_acl(Bucket=BUCKET, Key=rk).get("Grants") or []
        public = [g for g in grants if "AllUsers" in str(g.get("Grantee", {}).get("URI", ""))
                  or "AuthenticatedUsers" in str(g.get("Grantee", {}).get("URI", ""))]
        check(not public, "chart object ACL has no public grant")
    except Exception as e:
        # Bucket-owner-enforced buckets have ACLs disabled; that is private too.
        info(f"get_object_acl: {type(e).__name__} (ACLs disabled counts as private)")

    head("7. delete notebook")
    call("DELETE", f"/notebooks/{nid_a}", session=False)
    STATE["nids"].remove(nid_a)
    st, _, _ = raw_call("GET", f"/notebooks/{nid_a}", session=False)
    check(st == 404, "GET deleted notebook → 404", str(st))
    end = time.time() + 20
    left = list_prefix(nid_a)
    while left and time.time() < end:
        time.sleep(2)
        left = list_prefix(nid_a)
    check(not left, "no objects left under notebook-outputs/<A>/", str(left[:3]))


def main() -> int:
    if not TOKEN:
        print("proxy not running on :8081 (no PROXY_AUTH_TOKEN found) — start the stack first")
        return 1
    if not BUCKET:
        print("ARTIFACT_BUCKET not found in the proxy environment")
        return 1
    st, _, _ = raw_call("GET", "/health", session=False, auth=False, timeout=10)
    if st != 200:
        print(f"proxy not healthy at {PROXY_URL}: {st}")
        return 1
    try:
        a = call("POST", "/notebooks", {"name": "[live test] chart persistence A", "cells": CELLS,
                                        "session_id": SESSION_ID}, session=False)
        b = call("POST", "/notebooks", {"name": "[live test] chart persistence B",
                                        "cells": [{"id": "c1", "type": "code", "code": "1"}]},
                 session=False)
        STATE["nids"] += [a["id"], b["id"]]
        STATE["created"] += [a["id"], b["id"]]
        try:
            run(a["id"], b["id"])
        except Exception as e:
            bad(f"harness error: {type(e).__name__}: {e}")
    finally:
        try:
            if STATE["vm"]:
                call("POST", "/terminate", {"microvmId": STATE["vm"]}, timeout=300)
            for nid in list(STATE["nids"]):
                raw_call("DELETE", f"/notebooks/{nid}", session=False)
            for nid in STATE["created"]:
                purge_prefix(nid)
            info("cleanup: notebooks deleted, VM terminated, S3 prefixes purged")
        except Exception as e:
            info(f"cleanup failed: {type(e).__name__}: {e}")
    print(f"\nRESULT: {'all checks passed' if not _failures else f'{len(_failures)} check(s) FAILED'}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
