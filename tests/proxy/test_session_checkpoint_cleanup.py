#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for the S3 session-checkpoint cleanup on notebook delete
(vm-card-regroup Fix B): proxy/notebook/session_checkpoints.py, its use in
DELETE /notebooks/{id} (REAL proxy app over raw ASGI, temp SQLite redirected
before proxy.server is imported) and in the local_stop_app.sh
``delete-notebooks`` helper (the script's own DB_HELPER_PY body, exec'd
in-process). Fake S3 (cell_images._s3), no AWS.
  (a) deletes exactly sessions/<sid>/* across pages; neighbours survive
      (sessions/<sid>x/, another session, samples/, user-data/<sid>/,
      knowledge-base/, notebook-outputs/);
  (b) the guard: empty / None / short / slash / .. / blank / wildcard ids →
      ValueError with zero S3 calls;
  (c) no bucket → BucketUnavailable;
  (d) the best-effort wrapper: one failure doesn't stop the next, never raises;
  (e) the async wrapper with no bucket is a no-op;
  route: owned prefix gone, a shared session's prefix kept until its last
  notebook is deleted, DB delete before S3, S3 failure still → success;
  script: the delete-notebooks helper removes the owned prefix only.
Run:  .venv/bin/python tests/proxy/test_session_checkpoint_cleanup.py   (exit 0 = pass)
"""
import asyncio
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import uuid
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import proxy.storage.sqlite_db as _sq  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="session_ckpt_cleanup_")
_DB = os.path.join(_TMP, "session_ckpt_cleanup.db")
_sq.DB_DIR = _TMP
_sq.DB_PATH = _DB
_sq._local = threading.local()
_sq._HELD_SESSION_LOCKS.clear()

_TOKEN = "session-ckpt-cleanup-test-token"
os.environ.setdefault("PROXY_AUTH_TOKEN", _TOKEN)
os.environ.setdefault("SESSION_PERSISTENCE_MODE", "checkpoint")

import proxy.server as proxy_server  # noqa: E402
from proxy.notebook import cell_images, session_checkpoints as sc  # noqa: E402
from proxy.storage import astorage  # noqa: E402

APP = proxy_server.app
_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeS3:
    """list_objects_v2 paginates by page_size; every call is logged."""

    def __init__(self, page_size=1000, fail_prefix=None):
        self.objects = {}
        self.calls = []
        self.page_size = page_size
        self.fail_prefix = fail_prefix
        self.on_delete = None

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self.calls.append(("list", Prefix))
        if self.fail_prefix and Prefix.startswith(self.fail_prefix):
            raise RuntimeError("AccessDenied")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        more = start + self.page_size < len(keys)
        out = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + self.page_size)
        return out

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        assert len(keys) <= cell_images.DELETE_BATCH
        assert Delete.get("Quiet") is True
        self.calls.append(("delete", keys))
        if self.on_delete:
            self.on_delete(keys)
        for k in keys:
            self.objects.pop(k, None)
        return {}


def _sid():
    return str(uuid.uuid4())


def _seed(s3, sid, n=7):
    for i in range(n):
        s3.objects[f"sessions/{sid}/vars/v{i}.pkl"] = b"x"
    s3.objects[f"sessions/{sid}/metadata.json"] = b"{}"


# ── unit: the module ─────────────────────────────────────────────────────────
def test_module():
    sid, other = _sid(), _sid()
    s3 = FakeS3(page_size=3)
    _seed(s3, sid)                                           # 8 objects → 3 pages
    survivors = [f"sessions/{sid}x/keep.json", f"sessions/{other}/metadata.json",
                 "samples/a.csv", f"user-data/{sid}/f.csv", "knowledge-base/k.md",
                 "notebook-outputs/1/c1/abc.png"]
    for k in survivors:
        s3.objects[k] = b"k"
    n = sc.delete_session_prefix_sync(sid, "b", s3=s3)
    check("(a) deleted all 8 objects of sessions/<sid>/ across 3 pages", n == 8,
          str(n))
    check("(a) 3 list pages, then the deletes", [c[0] for c in s3.calls] == ["list"] * 3 + ["delete"],
          str([c[0] for c in s3.calls]))
    check("(a) no sessions/<sid>/ object left",
          not any(k.startswith(f"sessions/{sid}/") for k in s3.objects))
    check("(a) neighbours survive (sid-prefix-alike, other session, samples, user-data, kb, outputs)",
          all(k in s3.objects for k in survivors), str(sorted(s3.objects)))
    check("(a) every list call used exactly sessions/<sid>/",
          all(c[1] == f"sessions/{sid}/" for c in s3.calls if c[0] == "list"))

    big = FakeS3(page_size=1000)
    _seed(big, sid, n=2400)
    check("(a) batches ≤ 1000 per delete_objects", sc.delete_session_prefix_sync(sid, "b", s3=big) == 2401
          and [len(c[1]) for c in big.calls if c[0] == "delete"] == [1000, 1000, 401])

    bad = ["", None, "abc", "a/b-cdefgh", "../../xx", "        ", "*", "abc*defgh", 12345678,
           "abcdefg", "abcdefgh/", "-abcdefgh", "abc defgh"]
    for b in bad:
        g = FakeS3()
        try:
            sc.delete_session_prefix_sync(b, "bucket", s3=g)
            ok = False
        except ValueError:
            ok = True
        except Exception:
            ok = False
        check(f"(b) guard: {b!r} → ValueError, zero S3 calls", ok and g.calls == [], str(g.calls))
        check(f"(b) guard: prefix({b!r}) is None", sc.session_checkpoint_prefix(b) is None)
    check("(b) a UUID forms sessions/<uuid>/", sc.session_checkpoint_prefix(sid) == f"sessions/{sid}/")

    g = FakeS3()
    for nob in (None, ""):
        try:
            sc.delete_session_prefix_sync(sid, nob, s3=g)
            ok = False
        except cell_images.BucketUnavailable:
            ok = True
        check(f"(c) bucket {nob!r} → BucketUnavailable, zero S3 calls", ok and g.calls == [])

    s1, s2 = _sid(), _sid()
    f = FakeS3(fail_prefix=f"sessions/{s1}/")
    _seed(f, s1, 2)
    _seed(f, s2, 2)
    res = sc.delete_owned_session_prefixes_sync([s1, "../x", s2], "b", s3=f)
    check("(d) failing id → 'error: RuntimeError', invalid → 'skipped', next still deleted",
          res == {s1: "error: RuntimeError", "../x": "skipped", s2: 3}, str(res))
    check("(d) the failing session's objects are left alone, s2's are gone",
          sum(k.startswith(f"sessions/{s1}/") for k in f.objects) == 3
          and not any(k.startswith(f"sessions/{s2}/") for k in f.objects))
    check("(d) None ids → {} (never raises)", sc.delete_owned_session_prefixes_sync(None, "b", s3=f) == {})

    saved = (cell_images._bucket_cache, cell_images._s3)
    calls = []
    cell_images._bucket_cache = None
    cell_images._s3 = lambda: calls.append("s3") or FakeS3()
    try:
        asyncio.run(sc.delete_owned_session_prefixes([s1]))
        check("(e) async wrapper, no bucket → no-op (no S3 client)", calls == [])
        cell_images._bucket_cache = "b"
        asyncio.run(sc.delete_owned_session_prefixes([]))
        check("(e) async wrapper, no ids → no-op", calls == [])
    finally:
        cell_images._bucket_cache, cell_images._s3 = saved


# ── route: DELETE /notebooks/{id} ────────────────────────────────────────────
async def asgi(method, raw_path, *, body=None):
    path, _, qs = raw_path.partition("?")
    hdrs = {"content-type": "application/json", "x-proxy-auth": _TOKEN}
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
    out = {"status": None, "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
    await APP(scope, receive, send)
    return out["status"], out["body"]


async def _new_nb(name):
    st, b = await asgi("POST", "/notebooks", body={"name": name, "cells": [{"id": "c1"}]})
    return json.loads(b)["id"]


def _has(s3, sid):
    return any(k.startswith(f"sessions/{sid}/") for k in s3.objects)


async def route_async(s3):
    sync = astorage._sync
    # 1. An owning notebook + a notebook with its own session.
    na, nb = await _new_nb("Own"), await _new_nb("Other")
    sa, sb = _sid(), _sid()
    sync.notebook_update(na, session_id=sa)
    sync.notebook_update(nb, session_id=sb)
    _seed(s3, sa)
    _seed(s3, sb)
    s3.objects["samples/s.csv"] = b"s"
    order = []
    real_delete = sync.notebook_delete

    def tracking_delete(nid):
        order.append(("db", _has(s3, sa)))
        return real_delete(nid)
    s3.on_delete = lambda keys: order.append(("s3", len(keys)))
    sync.notebook_delete = tracking_delete
    astorage._wrapped.pop("notebook_delete", None)
    try:
        st, b = await asgi("DELETE", f"/notebooks/{na}")
    finally:
        sync.notebook_delete = real_delete
        astorage._wrapped.pop("notebook_delete", None)
        s3.on_delete = None
    check("route: DELETE → 200 {success: true}", st == 200 and json.loads(b) == {"success": True},
          f"{st} {b[:200]!r}")
    check("route: the owned session's prefix is gone", not _has(s3, sa))
    check("route: another notebook's session prefix + samples/ kept",
          _has(s3, sb) and "samples/s.csv" in s3.objects)
    check("route: DB delete ran first (objects still there), then the S3 delete",
          order[:1] == [("db", True)] and ("s3", 8) in order[1:], str(order))

    # 2. A session shared by two notebooks (one after /terminate via vm_sessions).
    n1, n2 = await _new_nb("Shared1"), await _new_nb("Shared2")
    ss = _sid()
    sync.notebook_update(n1, session_id=ss)
    sync.vm_session_create("vm-shared-x", notebook_id=n2, session_id=ss, memory_mib=2048)
    sync.vm_session_update_state("vm-shared-x", "TERMINATED")
    _seed(s3, ss)
    st, _ = await asgi("DELETE", f"/notebooks/{n1}")
    check("route(shared): first delete keeps the shared prefix", st == 200 and _has(s3, ss))
    st, _ = await asgi("DELETE", f"/notebooks/{n2}")
    check("route(shared): deleting the last notebook removes it", st == 200 and not _has(s3, ss))

    # 3. S3 failure → the delete still succeeds.
    nf = await _new_nb("Fails")
    sf = _sid()
    sync.notebook_update(nf, session_id=sf)
    _seed(s3, sf)
    s3.fail_prefix = f"sessions/{sf}/"
    try:
        st, b = await asgi("DELETE", f"/notebooks/{nf}")
    finally:
        s3.fail_prefix = None
    check("route: S3 failure still → 200 {success: true}",
          st == 200 and json.loads(b) == {"success": True} and sync.notebook_get(nf) is None)
    # cleanup for the next part
    s3.objects.clear()
    await asgi("DELETE", f"/notebooks/{nb}")


def test_route():
    os.environ["PROXY_AUTH_TOKEN"] = _TOKEN
    _sq.DB_DIR = _TMP
    _sq.DB_PATH = _DB
    _sq._local = threading.local()
    astorage._sync.initialize()
    s3 = FakeS3(page_size=3)
    saved = (cell_images._s3, cell_images._bucket_cache)
    cell_images._s3 = lambda: s3
    cell_images._bucket_cache = "test-bucket"
    try:
        asyncio.run(route_async(s3))
    finally:
        cell_images._s3, cell_images._bucket_cache = saved


# ── script: local_stop_app.sh delete-notebooks helper ────────────────────────
def _script_helper_src():
    with open(os.path.join(_ROOT, "local_stop_app.sh")) as fh:
        sh = fh.read()
    m = re.search(r"read -r -d '' DB_HELPER_PY <<'PY' \|\| true\n(.*?)\nPY\n", sh, re.S)
    assert m, "DB_HELPER_PY heredoc not found"
    return m.group(1)


def test_script_path():
    sync = astorage._sync
    s3 = FakeS3(page_size=2)
    own_nb = sync.notebook_create("ScriptOwn", cells=[{"id": "c1"}])["id"]
    keep_nb = sync.notebook_create("ScriptKeep", cells=[{"id": "c1"}])["id"]
    so, sk = _sid(), _sid()
    sync.notebook_update(own_nb, session_id=so)
    sync.notebook_update(keep_nb, session_id=sk)
    _seed(s3, so)
    _seed(s3, sk)
    s3.objects["user-data/u.csv"] = b"u"
    src = _script_helper_src()
    check("script: the helper reads notebook_owned_session_ids before notebook_delete",
          0 < src.index("notebook_owned_session_ids(nbid)") < src.index("storage.notebook_delete(nbid)"))
    saved = (cell_images._s3, sys.argv, sys.stdin, sys.stdout, os.environ.get("ARTIFACT_BUCKET"))
    cell_images._s3 = lambda: s3
    os.environ["ARTIFACT_BUCKET"] = "test-bucket"
    sys.argv = ["db_helper", "delete-notebooks"]
    sys.stdin = io.StringIO(f"{own_nb}\n")
    out = io.StringIO()
    sys.stdout = out
    try:
        exec(compile(src, "local_stop_app.sh:DB_HELPER_PY", "exec"), {"__name__": "__main__"})
    finally:
        logging.disable(logging.NOTSET)
        cell_images._s3, sys.argv, sys.stdin, sys.stdout, ab = saved
        if ab is None:
            os.environ.pop("ARTIFACT_BUCKET", None)
        else:
            os.environ["ARTIFACT_BUCKET"] = ab
    check("script: notebook deleted from the DB", sync.notebook_get(own_nb) is None
          and "deleted from the DB" in out.getvalue(), out.getvalue())
    check("script: the owned session's prefix is gone", not _has(s3, so))
    check("script: another notebook's session + user-data/ kept",
          _has(s3, sk) and "user-data/u.csv" in s3.objects)
    sync.notebook_delete(keep_nb)


def main():
    test_module()
    test_route()
    test_script_path()
    failed = [n for n, ok in _checks if not ok]
    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")


if __name__ == "__main__":
    main()
