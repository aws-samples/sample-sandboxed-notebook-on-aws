#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for MULTI-OUTPUT cells: several chart images per cell (keys listed in
the cell's html envelope), image GET ?key=, orphan diff, notebook delete cleanup,
the agent execute_code counts. Same harness as tests/proxy/test_cell_image_route.py: the chart route and the S3 cleanup hooks on the
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

_TMP = tempfile.mkdtemp(prefix="multi_output_proxy_")
_DB = os.path.join(_TMP, "multi_output_proxy.db")
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
PNG3 = b"\x89PNG\r\n\x1a\n" + b"third-chart" * 30
SHA = hashlib.sha256(PNG).hexdigest()
SHA2 = hashlib.sha256(PNG2).hexdigest()
SHA3 = hashlib.sha256(PNG3).hexdigest()


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


def _envelope(nid, cell, shas, extra=()):
    items = [{"t": "text", "text": "hello\n"}]
    for sh in shas:
        items.append({"t": "image", "key": _key(nid, cell, sh)})
    items.extend(extra)
    return json.dumps({"__outputs__": 1, "items": items}, separators=(",", ":"))
def _data_url(png):
    import base64
    return "data:image/png;base64," + base64.b64encode(png).decode()
async def main_async():
    cell_images.RECENT_PUT_GRACE_S = 0
    st, b, _ = await asgi("POST", "/notebooks", body={"name": "M", "cells": [{"id": "c1"}]})
    na = json.loads(b)["id"]
    st, b, _ = await asgi("POST", "/notebooks", body={"name": "N", "cells": [{"id": "c1"}]})
    nb = json.loads(b)["id"]

    print("prepare_outputs (N images)")
    outs = [{"t": "text", "text": "a"}, {"t": "image", "src": _data_url(PNG), "key": "forged"},
            {"t": "table", "payload": "{}"}, {"t": "image", "src": _data_url(PNG2)},
            {"t": "image", "src": "data:image/jpeg;base64,AAAA", "key": "forged2"},
            {"t": "image", "src": _data_url(PNG3)}]
    S3.objects.clear()
    last = cell_images.prepare_outputs(na, "c1", outs)
    await _settle()
    keys = [o.get("key") for o in outs if o["t"] == "image"]
    check("keys: sha-addressed for valid PNGs", keys[0] == _key(na, "c1", SHA) and keys[1] == _key(na, "c1", SHA2)
          and keys[3] == _key(na, "c1", SHA3), str(keys))
    check("a forged key on an unpreparable image is removed", keys[2] is None and "forged" not in json.dumps(outs))
    check("last key returned = the last image's key", last == _key(na, "c1", SHA3))
    check("one PUT per valid image", sorted(S3.objects) == sorted([_key(na, "c1", SHA), _key(na, "c1", SHA2), _key(na, "c1", SHA3)]))
    check("base64 src stays in the in-flight response items", all(o["src"] for o in outs if o["t"] == "image"))
    check("non-list outputs never raise", cell_images.prepare_outputs(na, "c1", None) is None
          and cell_images.prepare_outputs(na, "c1", "x") is None)

    print("stored envelope + GET ?key=")
    k1, k2, k3 = (_key(na, "c1", s) for s in (SHA, SHA2, SHA3))
    env = _envelope(na, "c1", [SHA, SHA2, SHA3])
    st, b, _ = await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": env, "imageS3Key": k3}]})
    check("PUT of an envelope cell → 200", st == 200, f"{st} {b[:200]!r}")
    st, b, _ = await asgi("GET", f"/notebooks/{na}")
    cell = {c["id"]: c for c in json.loads(b)["cells"]}["c1"]
    check("stored html is the envelope string, byte-identical, no base64", cell["html"] == env
          and "data:image" not in cell["html"])
    url = f"/notebooks/{na}/cells/c1/image"
    for sh, png in ((SHA, PNG), (SHA2, PNG2), (SHA3, PNG3)):
        st, b, h = await asgi("GET", f"{url}?key={sh}")
        check(f"?key={sh[:6]}… serves its PNG", st == 200 and b == png and h.get("content-type") == "image/png", str(st))
    st, b, _ = await asgi("GET", url)
    check("no query → the last image (legacy column)", st == 200 and b == PNG3)
    st, b, _ = await asgi("GET", f"{url}?sha={SHA3}")
    check("?sha= legacy: equals the stored last key → 200", st == 200 and b == PNG3)
    st, b, _ = await asgi("GET", f"{url}?sha={SHA}")
    check("?sha= legacy: another image of the cell → 404", st == 404)
    st, b, _ = await asgi("GET", f"{url}?key={'e' * 64}")
    check("?key= unknown sha → 404", st == 404 and b != PNG)
    for bad in ("XYZ", SHA.upper(), "../../x", SHA[:-1]):
        st, b, _ = await asgi("GET", f"{url}?key={bad}")
        check(f"?key={bad[:12]} malformed → 400", st == 400, str(st))
    st, b, _ = await asgi("GET", f"/notebooks/{nb}/cells/c1/image?key={SHA}")
    check("?key= on another notebook → 404", st == 404)
    st, b, _ = await asgi("GET", f"/notebooks/{na}/cells/c9/image?key={SHA}")
    check("?key= on a cell without images → 404", st == 404)
    st, b, _ = await asgi("GET", url, auth=False)
    check("?key= without X-Proxy-Auth → 401", st == 401)
    # Foreign keys placed in an envelope are never served or stored as references.
    foreign = _envelope(na, "c1", [SHA])          # notebook A's key, placed on notebook B
    other_cell = _envelope(nb, "c2", [SHA2])      # B's key for ANOTHER cell, placed on B's c1
    S3.objects[_key(nb, "c1", SHA)] = PNG
    S3.objects[_key(nb, "c2", SHA2)] = PNG2
    await asgi("PUT", f"/notebooks/{nb}", body={"cells": [{"id": "c1", "html": foreign}]})
    st, b, _ = await asgi("GET", f"/notebooks/{nb}/cells/c1/image?key={SHA}")
    check("envelope key of another notebook is not served (404)", st == 404 and b != PNG)
    await asgi("PUT", f"/notebooks/{nb}", body={"cells": [{"id": "c1", "html": other_cell}]})
    st, b, _ = await asgi("GET", f"/notebooks/{nb}/cells/c1/image?key={SHA2}")
    check("envelope key of another cell is not served (404)", st == 404 and b != PNG2)
    await asgi("PUT", f"/notebooks/{nb}", body={"cells": [{"id": "c1"}]})

    print("orphan diff")
    S3.objects.clear()
    for k, p in ((k1, PNG), (k2, PNG2), (k3, PNG3)):
        S3.objects[k] = p
    # c2 references k1's twin (same sha under ITS OWN cell segment) – a different key.
    c2k = _key(na, "c2", SHA)
    S3.objects[c2k] = PNG
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": env, "imageS3Key": k3},
        {"id": "c2", "html": _envelope(na, "c2", [SHA]), "imageS3Key": c2k}]})
    await _settle()
    S3.deleted.clear()
    env2 = _envelope(na, "c1", [SHA, SHA3])      # drops k2 only
    st, b, _ = await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": env2, "imageS3Key": k3},
        {"id": "c2", "html": _envelope(na, "c2", [SHA]), "imageS3Key": c2k}]})
    await _settle()
    check("envelope drops one key: only that key is deleted", S3.deleted == [k2], str(S3.deleted))
    check("the cell's other keys and the other cell's key are kept",
          k1 in S3.objects and k3 in S3.objects and c2k in S3.objects)
    S3.deleted.clear()
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": env2, "imageS3Key": k3}, {"id": "c2"}]})
    await _settle()
    check("a cell cleared of its envelope releases its keys", S3.deleted == [c2k], str(S3.deleted))
    # Same key referenced by the envelope of ANOTHER save of the same cell: never deleted.
    S3.deleted.clear()
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": _envelope(na, "c1", [SHA3, SHA]), "imageS3Key": k3}, {"id": "c2"}]})
    await _settle()
    check("re-ordered envelope with the same keys deletes nothing", S3.deleted == [], str(S3.deleted))
    S3.deleted.clear()
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": "<div>plain</div>", "imageS3Key": None}, {"id": "c2"}]})
    await _settle()
    check("replacing the envelope by plain html deletes every key it listed",
          sorted(S3.deleted) == sorted([k1, k3]), str(S3.deleted))

    print("notebook delete cleanup")
    S3.objects.clear()
    for k, p in ((k1, PNG), (k2, PNG2), (k3, PNG3), (_key(na, "c5", SHA), PNG), (_key(nb, "c1", SHA), PNG)):
        S3.objects[k] = p
    await asgi("PUT", f"/notebooks/{na}", body={"cells": [
        {"id": "c1", "html": env, "imageS3Key": k3}]})
    st, b, _ = await asgi("DELETE", f"/notebooks/{na}")
    check("DELETE → 200", st == 200 and json.loads(b).get("success") is True)
    check("DELETE removes all 3 keys of the envelope cell and the unreferenced one under the prefix",
          not any(k.startswith(f"notebook-outputs/{na}/") for k in S3.objects))
    check("DELETE keeps another notebook's objects", _key(nb, "c1", SHA) in S3.objects)

    print("execute_code counts (agent)")
    from proxy.notebook.ai.tools import execution_tools as et
    fn = getattr(et, "_rendered_summary", None)
    check("execution_tools exposes the count helper", fn is not None)
    if fn:
        t = {"t": "table"}; p = {"t": "plotly"}; im = {"t": "image"}; tx = {"t": "text", "text": "SECRET-TABLE-CONTENT"}
        check("2 tables, 1 chart", fn({"outputs": [t, tx, t, p]}) == "[2 tables, 1 chart rendered in notebook]", fn({"outputs": [t, tx, t, p]}))
        check("images count as charts", fn({"outputs": [im, im, t]}) == "[1 table, 2 charts rendered in notebook]")
        check("single table", fn({"outputs": [t]}) == "[1 table rendered in notebook]")
        check("old VM (html only) keeps the old line", fn({"html": "{}"}) == "[DataFrame table rendered in notebook]")
        check("no rich output → empty", fn({"outputs": [tx], "html": ""}) == "" and fn({}) == "")
        check("never includes text contents", "SECRET" not in fn({"outputs": [t, tx]}))
def test_multi_output_proxy():
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
        test_multi_output_proxy()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")
