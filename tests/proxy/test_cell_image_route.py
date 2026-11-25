#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for the chart image route and the S3 cleanup hooks on the
notebook routes, driven through the REAL proxy app (proxy.server.app) over raw
ASGI calls. Temp SQLite (redirected before proxy.server is imported), fake S3
(proxy.notebook.cell_images._s3), no AWS.

GET /notebooks/{id}/cells/{cell}/image:
  * 401 without / with a wrong X-Proxy-Auth; 503 with no server token (fail closed);
  * 200 with the PNG + nosniff / private no-store / CSP when authenticated;
  * 404 for an unknown notebook, a cell without a key, another notebook's cell
    id, a sha mismatch, a missing object; 400 for a bad sha / over-long cell id;
    the cell id is a single path segment (no traversal to another route);
  * a PUT storing notebook A's key on notebook B stores NULL → 404.
PUT orphan diff deletes only removed keys; version restore deletes the replaced
keys; DELETE /notebooks/{id} runs the prefix delete after the DB delete.

Run:  .venv/bin/python tests/proxy/test_cell_image_route.py   (exit 0 = pass)
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import proxy.storage.sqlite_db as _sq  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="cell_image_route_")
_DB = os.path.join(_TMP, "cell_image_route.db")
_sq.DB_DIR = _TMP
_sq.DB_PATH = _DB
_sq._local = threading.local()
_sq._HELD_SESSION_LOCKS.clear()
_TOKEN = "cell-image-route-test-token"
os.environ.setdefault("PROXY_AUTH_TOKEN", _TOKEN)
os.environ.setdefault("SESSION_PERSISTENCE_MODE", "checkpoint")

import proxy.server as proxy_server  # noqa: E402
from proxy.notebook import cell_images  # noqa: E402
from proxy.storage import astorage  # noqa: E402

APP = proxy_server.app
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


PNG = b"\x89PNG\r\n\x1a\n" + b"route-chart" * 30
PNG2 = b"\x89PNG\r\n\x1a\n" + b"second-chart" * 30
SHA = hashlib.sha256(PNG).hexdigest()
SHA2 = hashlib.sha256(PNG2).hexdigest()


class NoSuchKey(Exception):
    pass


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, **kw):
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": _Body(self.objects[Key]), "ContentLength": len(self.objects[Key])}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.objects.pop(o["Key"], None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
                "IsTruncated": False}


S3 = FakeS3()


async def asgi(method, raw_path, *, body=None, auth=True, token=None):
    path, _, qs = raw_path.partition("?")
    hdrs = {"content-type": "application/json"}
    if auth:
        hdrs["x-proxy-auth"] = token or _TOKEN
    payload = b"" if body is None else json.dumps(body).encode()
    hdrs["content-length"] = str(len(payload))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http",
        "path": unquote(path), "raw_path": path.encode(), "query_string": qs.encode(),
        "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8080),
    }
    msgs = [{"type": "http.request", "body": payload, "more_body": False}]
    never = asyncio.Event()

    async def receive():
        if msgs:
            return msgs.pop(0)
        await never.wait()
        return {"type": "http.disconnect"}
    out = {"status": None, "body": b"", "headers": {}}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
    await APP(scope, receive, send)
    return out["status"], out["body"], out["headers"]


def _key(nid, cid, sha=SHA):
    return f"notebook-outputs/{nid}/{cid}/{sha}.png"


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not cell_images._background:
            return


async def main_async():
    st, b, _ = await asgi("POST", "/notebooks", body={"name": "A", "cells": [{"id": "c1"}]})
    na = json.loads(b)["id"]
    st, b, _ = await asgi("POST", "/notebooks", body={"name": "B", "cells": [{"id": "c1"}]})
    nb = json.loads(b)["id"]
    ka = _key(na, "c1")
    S3.objects[ka] = PNG
    st, b, _ = await asgi("PUT", f"/notebooks/{na}",
                          body={"cells": [{"id": "c1", "imageS3Key": ka}, {"id": "c2"}]})
    check("PUT with the notebook's own key → 200", st == 200, f"{st} {b[:200]!r}")
    st, b, _ = await asgi("GET", f"/notebooks/{na}")
    cells = {c["id"]: c for c in json.loads(b)["cells"]}
    check("GET /notebooks/{id} returns imageS3Key (no base64)",
          cells["c1"]["imageS3Key"] == ka and cells["c1"]["image"] is None)

    url = f"/notebooks/{na}/cells/c1/image"
    st, b, h = await asgi("GET", url, auth=False)
    check("no X-Proxy-Auth → 401", st == 401 and b != PNG, str(st))
    st, b, h = await asgi("GET", url, token="wrong-token")
    check("wrong X-Proxy-Auth → 401", st == 401 and b != PNG, str(st))
    saved = os.environ.pop("PROXY_AUTH_TOKEN")
    try:
        st, b, h = await asgi("GET", url)
        check("no server token → 503 (fail closed)", st == 503 and b != PNG, str(st))
    finally:
        os.environ["PROXY_AUTH_TOKEN"] = saved
    st, b, h = await asgi("GET", url)
    check("authenticated → 200 with the PNG bytes", st == 200 and b == PNG, str(st))
    check("headers: image/png, nosniff, private no-store, CSP default-src 'none'",
          h.get("content-type") == "image/png" and h.get("x-content-type-options") == "nosniff"
          and h.get("cache-control") == "private, no-store"
          and h.get("content-security-policy") == "default-src 'none'", str(h))
    st, b, h = await asgi("GET", f"{url}?sha={SHA}")
    check("matching sha → 200", st == 200 and b == PNG)

    for label, path, want in (
            ("unknown notebook", "/notebooks/999999/cells/c1/image", 404),
            ("cell without a key", f"/notebooks/{na}/cells/c2/image", 404),
            ("another notebook's cell id (B has no key)", f"/notebooks/{nb}/cells/c1/image", 404),
            ("sha mismatch", f"{url}?sha={SHA2}", 404),
            ("bad sha", f"{url}?sha=XYZ", 400),
            ("uppercase sha", f"{url}?sha={SHA.upper()}", 400),
            ("over-long cell id", f"/notebooks/{na}/cells/{'x' * 257}/image", 400),
            ("traversal in the cell id (%2e%2e)", f"/notebooks/{na}/cells/%2e%2e/image", 404),
            ("encoded slash in the cell id", f"/notebooks/{na}/cells/c1%2F..%2F..%2Fx/image", 404),
            ("non-int notebook id", "/notebooks/abc/cells/c1/image", 422)):
        st, b, _ = await asgi("GET", path)
        check(f"{label} → {want}", st == want and b != PNG, f"{st} {b[:120]!r}")

    # Foreign key on B is stored as NULL → 404 on B.
    st, b, _ = await asgi("PUT", f"/notebooks/{nb}", body={"cells": [{"id": "c1", "imageS3Key": ka}]})
    check("PUT of notebook A's key on notebook B → stored NULL",
          (await astorage.notebook_cell_image_keys(nb)) == {})
    st, b, _ = await asgi("GET", f"/notebooks/{nb}/cells/c1/image")
    check("…and B's image route → 404 (never A's bytes)", st == 404 and b != PNG)
    await _settle()
    check("the foreign PUT deleted nothing", ka in S3.objects)

    # Missing object → 404.
    S3.objects.pop(ka)
    st, b, _ = await asgi("GET", url)
    check("stored key but no object → 404", st == 404)
    S3.objects[ka] = PNG

    # PUT orphan diff: replace c1's chart; only the old key is deleted.
    cell_images.RECENT_PUT_GRACE_S = 0
    k2 = _key(na, "c1", SHA2)
    k3 = _key(na, "c3")
    S3.objects[k2] = PNG2
    S3.objects[k3] = PNG
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "imageS3Key": ka}, {"id": "c3", "imageS3Key": k3}]})
    S3.deleted.clear()
    st, b, _ = await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "imageS3Key": k2}, {"id": "c3", "imageS3Key": k3}]})
    await _settle()
    check("PUT orphan diff deletes only the replaced key",
          S3.deleted == [ka] and k2 in S3.objects and k3 in S3.objects, str(S3.deleted))
    # Clear output (key null) → deleted.
    S3.deleted.clear()
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "imageS3Key": k2}, {"id": "c3", "imageS3Key": None}]})
    await _settle()
    check("PUT with a cleared key deletes its object", S3.deleted == [k3], str(S3.deleted))
    # Anti-clobber (empty cells) deletes nothing.
    S3.deleted.clear()
    await asgi("PUT", f"/notebooks/{na}", body={"cells": []})
    await _settle()
    check("anti-clobber empty-cells PUT deletes nothing", S3.deleted == [] and k2 in S3.objects)

    # Version restore deletes the replaced keys.
    st, b, _ = await asgi("POST", f"/notebooks/{na}/versions", body={"label": "v", "trigger": "manual"})
    vg = json.loads(b)["version_group_id"]
    S3.deleted.clear()
    st, b, _ = await asgi("POST", f"/notebooks/{na}/versions/{vg}/restore")
    await _settle()
    check("version restore → 200 and the restored cells carry no key",
          st == 200 and all(not c.get("imageS3Key") for c in json.loads(b)["cells"]))
    check("version restore deletes the replaced chart", S3.deleted == [k2], str(S3.deleted))

    # DELETE notebook → DB first, then the prefix.
    S3.objects[ka] = PNG
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [{"id": "c1", "imageS3Key": ka}]})
    S3.objects[_key(na, "never-saved")] = PNG
    S3.objects[_key(nb, "c1")] = PNG
    order = []
    real_delete = astorage._sync.notebook_delete

    def tracking_delete(nid):
        order.append(("db", list(k for k in S3.objects if k.startswith(f"notebook-outputs/{na}/"))))
        return real_delete(nid)
    astorage._sync.notebook_delete = tracking_delete
    astorage._wrapped.pop("notebook_delete", None)
    try:
        st, b, _ = await asgi("DELETE", f"/notebooks/{na}")
    finally:
        astorage._sync.notebook_delete = real_delete
        astorage._wrapped.pop("notebook_delete", None)
    check("DELETE → 200 success", st == 200 and json.loads(b).get("success") is True)
    check("DB delete ran while the objects still existed (DB first)", order and len(order[0][1]) == 2)
    check("DELETE removed every object under the notebook's prefix only",
          not any(k.startswith(f"notebook-outputs/{na}/") for k in S3.objects)
          and _key(nb, "c1") in S3.objects)
    check("DELETE leaves no DB key reference", (await astorage.notebook_cell_image_keys(na)) == {})


def test_cell_image_route():
    _checks.clear()
    saved_env = os.environ.get("PROXY_AUTH_TOKEN")
    os.environ["PROXY_AUTH_TOKEN"] = _TOKEN
    _sq.DB_DIR = _TMP
    _sq.DB_PATH = _DB
    _sq._local = threading.local()
    astorage._sync.initialize()
    saved = (cell_images._s3, cell_images._bucket_cache, cell_images.RECENT_PUT_GRACE_S)
    cell_images._s3 = lambda: S3
    cell_images._bucket_cache = "test-bucket"
    try:
        asyncio.run(main_async())
    finally:
        cell_images._s3, cell_images._bucket_cache, cell_images.RECENT_PUT_GRACE_S = saved
        if saved_env is None:
            os.environ.pop("PROXY_AUTH_TOKEN", None)
        else:
            os.environ["PROXY_AUTH_TOKEN"] = saved_env
    failed = [n for n, ok in _checks if not ok]
    assert not failed, failed


if __name__ == "__main__":
    try:
        test_cell_image_route()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")
