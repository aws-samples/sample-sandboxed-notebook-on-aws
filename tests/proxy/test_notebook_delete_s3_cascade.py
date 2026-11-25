#!/usr/bin/env python3
# test-kind: offline
"""Offline tests: deleting a notebook removes EVERY S3 prefix it owns, and
nothing else.

A notebook's S3 data (see proxy/notebook/session_checkpoints.py):
  notebook-outputs/<nid>/   chart images            (cell_images)
  attachments/<nid>/        chat attachments sent with the notebook id (tab
                            had no session id yet)
  and per OWNED session <sid>:
  sessions/<sid>/           checkpoints, launch catalog, workbook intel,
                            local-entity docs
  attachments/<sid>/        chat attachments
  user-data/<sid>/          attachments promoted to a session data source

Covered:
  (1) prefix builders: the exact prefixes, the guards (invalid ids → none),
      DATASOURCE_S3_SESSION_PREFIX honoured / blank disables it;
  (2) delete_session_data_sync: every session prefix across pages, batches of
      ≤ 1000, one failing prefix does not stop the others (and is reported);
  (3) DELETE /notebooks/{id} (REAL proxy app over raw ASGI, temp SQLite, fake
      S3): all of the deleted notebook's prefixes go; another notebook's
      prefixes, a shared session's prefixes, the shared flat user-data/ files,
      user-data/<nid>/ (may be a user's own folder), samples/, models/,
      sagemaker/, unload/, knowledge-base/ and look-alike prefixes survive;
  (4) the local_stop_app.sh delete-notebooks fallback does the same.
Run:  .venv/bin/python tests/proxy/test_notebook_delete_s3_cascade.py   (exit 0 = pass)
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

_TMP = tempfile.mkdtemp(prefix="nb_delete_s3_cascade_")
_DB = os.path.join(_TMP, "nb_delete_s3_cascade.db")
_sq.DB_DIR = _TMP
_sq.DB_PATH = _DB
_sq._local = threading.local()
_sq._HELD_SESSION_LOCKS.clear()

_TOKEN = "nb-delete-s3-cascade-test-token"
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
        self.calls.append(("delete", keys))
        for k in keys:
            self.objects.pop(k, None)
        return {}


def _sid():
    return str(uuid.uuid4())


def _seed_session(s3, sid, n=2):
    for i in range(n):
        s3.objects[f"sessions/{sid}/vars/v{i}.pkl"] = b"x"
    s3.objects[f"sessions/{sid}/workbook-intel.json"] = b"{}"
    s3.objects[f"sessions/{sid}/local-entities/f.json"] = b"{}"
    s3.objects[f"attachments/{sid}/{uuid.uuid4().hex}/a.csv"] = b"a"
    s3.objects[f"user-data/{sid}/a.csv"] = b"a"


def _seed_notebook(s3, nid):
    s3.objects[f"notebook-outputs/{nid}/c1/abc.png"] = b"p"
    s3.objects[f"attachments/{nid}/{uuid.uuid4().hex}/b.csv"] = b"b"


def _session_left(s3, sid):
    return sorted(k for k in s3.objects
                  if any(k.startswith(p) for p in (f"sessions/{sid}/", f"attachments/{sid}/",
                                                   f"user-data/{sid}/")))


def _notebook_left(s3, nid):
    return sorted(k for k in s3.objects
                  if k.startswith(f"notebook-outputs/{nid}/") or k.startswith(f"attachments/{nid}/"))


def _shared(nid):
    """Objects that a notebook delete must never touch."""
    return ["samples/a.csv", "user-data/flat.csv", f"user-data/{nid}/mine.csv",
            "knowledge-base/k.md", "models/m.joblib", "sagemaker/job/output/model.tar.gz",
            "unload/run1/part-0", "athena-results/q.csv",
            f"attachments/{nid}0/x.csv", f"notebook-outputs/{nid}0/c/x.png"]


# ── (1) prefix builders ─────────────────────────────────────────────────────
def test_prefixes():
    sid = _sid()
    saved = os.environ.pop("DATASOURCE_S3_SESSION_PREFIX", None)
    try:
        check("(1) session prefixes: sessions/, attachments/, user-data/<sid>/",
              sc.session_data_prefixes(sid) == [f"sessions/{sid}/", f"attachments/{sid}/",
                                                f"user-data/{sid}/"],
              str(sc.session_data_prefixes(sid)))
        os.environ["DATASOURCE_S3_SESSION_PREFIX"] = "custom-src/"
        check("(1) DATASOURCE_S3_SESSION_PREFIX is honoured",
              sc.session_data_prefixes(sid)[-1] == f"custom-src/{sid}/")
        os.environ["DATASOURCE_S3_SESSION_PREFIX"] = "  "
        check("(1) a blank DATASOURCE_S3_SESSION_PREFIX drops that prefix (no top-level <sid>/)",
              sc.session_data_prefixes(sid) == [f"sessions/{sid}/", f"attachments/{sid}/"])
    finally:
        if saved is None:
            os.environ.pop("DATASOURCE_S3_SESSION_PREFIX", None)
        else:
            os.environ["DATASOURCE_S3_SESSION_PREFIX"] = saved
    for bad in ["", None, "abc", "a/b-cdefgh", "../../xx", "*", "abcdefgh/", 12345678]:
        check(f"(1) invalid session id {bad!r} → no prefixes", sc.session_data_prefixes(bad) == [])
    check("(1) notebook prefixes for 32 / '32'",
          sc.notebook_data_prefixes(32) == ["attachments/32/"]
          and sc.notebook_data_prefixes("32") == ["attachments/32/"])
    for bad in [0, -1, "0", "01", "3/2", "..", "", None, True, "32 ", "*", 3.0]:
        check(f"(1) invalid notebook id {bad!r} → no prefixes", sc.notebook_data_prefixes(bad) == [])
    g = FakeS3()
    try:
        sc.delete_notebook_data_sync("../x", "b", s3=g)
        ok = False
    except ValueError:
        ok = True
    check("(1) delete_notebook_data_sync invalid id → ValueError, zero S3 calls", ok and g.calls == [])


# ── (2) delete_session_data_sync ────────────────────────────────────────────
def test_session_data_delete():
    sid, other = _sid(), _sid()
    s3 = FakeS3(page_size=2)
    _seed_session(s3, sid, n=5)          # 5 vars + 2 + 1 + 1 = 9 objects
    _seed_session(s3, other)
    keep = _shared(7) + [f"sessions/{sid}x/k", f"attachments/{sid}x/k", f"user-data/{sid}x/k",
                         f"user-data/{sid}.csv"]
    for k in keep:
        s3.objects[k] = b"k"
    n = sc.delete_session_data_sync(sid, "b", s3=s3)
    check("(2) every object under the 3 session prefixes deleted, across pages",
          n == 9 and _session_left(s3, sid) == [], f"{n} {_session_left(s3, sid)}")
    check("(2) another session's prefixes survive", len(_session_left(s3, other)) == 6)
    check("(2) shared + look-alike keys survive", all(k in s3.objects for k in keep),
          str(sorted(set(keep) - set(s3.objects))))
    check("(2) list calls used exactly the 3 session prefixes",
          {c[1] for c in s3.calls if c[0] == "list"} == set(sc.session_data_prefixes(sid)))

    big = FakeS3()
    for i in range(2400):
        big.objects[f"attachments/{sid}/{i:05d}/f.csv"] = b"x"
    check("(2) attachments/<sid>/ beyond 1000 keys: all deleted in ≤1000 batches",
          sc.delete_session_data_sync(sid, "b", s3=big) == 2400 and not big.objects
          and [len(c[1]) for c in big.calls if c[0] == "delete"] == [1000, 1000, 400])

    f = FakeS3(fail_prefix=f"sessions/{sid}/")
    _seed_session(f, sid)
    try:
        sc.delete_session_data_sync(sid, "b", s3=f)
        raised = False
    except RuntimeError:
        raised = True
    check("(2) a failing prefix raises, after the other prefixes were still deleted",
          raised and _session_left(f, sid) == sorted(k for k in f.objects if k.startswith("sessions/")))
    res = sc.delete_owned_session_prefixes_sync([sid], "b", s3=f)
    check("(2) owned-session wrapper reports the failure", res == {sid: "error: RuntimeError"}, str(res))


# ── (3) DELETE /notebooks/{id} ──────────────────────────────────────────────
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


async def route_async(s3):
    sync = astorage._sync
    na, nb = await _new_nb("Own"), await _new_nb("Other")
    sa, sa_old, sb = _sid(), _sid(), _sid()
    sync.notebook_update(na, session_id=sa)
    # An older (terminated) session of the same notebook: owned via vm_sessions.
    sync.vm_session_create("vm-old-own", notebook_id=na, session_id=sa_old, memory_mib=2048)
    sync.vm_session_update_state("vm-old-own", "TERMINATED")
    sync.notebook_update(nb, session_id=sb)
    for sid in (sa, sa_old, sb):
        _seed_session(s3, sid)
    _seed_notebook(s3, na)
    _seed_notebook(s3, nb)
    shared = _shared(na)
    for k in shared:
        s3.objects[k] = b"k"

    st, b = await asgi("DELETE", f"/notebooks/{na}")
    check("route: DELETE → 200 {success: true}", st == 200 and json.loads(b) == {"success": True},
          f"{st} {b[:200]!r}")
    check("route: current session's sessions/, attachments/, user-data/ gone",
          _session_left(s3, sa) == [], str(_session_left(s3, sa)))
    check("route: older owned session's prefixes gone too", _session_left(s3, sa_old) == [])
    check("route: notebook-outputs/<id>/ and attachments/<id>/ gone",
          _notebook_left(s3, na) == [], str(_notebook_left(s3, na)))
    check("route: the other notebook's prefixes all kept",
          len(_session_left(s3, sb)) == 6 and len(_notebook_left(s3, nb)) == 2)
    check("route: shared data, user-data/<nid>/ and look-alike prefixes kept",
          all(k in s3.objects for k in shared), str(sorted(set(shared) - set(s3.objects))))

    # A session shared by two notebooks is kept until its last notebook goes.
    n1, n2 = await _new_nb("Shared1"), await _new_nb("Shared2")
    ss = _sid()
    sync.notebook_update(n1, session_id=ss)
    sync.vm_session_create("vm-shared-y", notebook_id=n2, session_id=ss, memory_mib=2048)
    sync.vm_session_update_state("vm-shared-y", "TERMINATED")
    _seed_session(s3, ss)
    st, _ = await asgi("DELETE", f"/notebooks/{n1}")
    check("route(shared): first delete keeps all 3 shared-session prefixes",
          st == 200 and len(_session_left(s3, ss)) == 6)
    st, _ = await asgi("DELETE", f"/notebooks/{n2}")
    check("route(shared): deleting the last notebook removes them",
          st == 200 and _session_left(s3, ss) == [])
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


# ── (4) local_stop_app.sh delete-notebooks helper ───────────────────────────
def _script_helper_src():
    with open(os.path.join(_ROOT, "local_stop_app.sh")) as fh:
        sh = fh.read()
    m = re.search(r"read -r -d '' DB_HELPER_PY <<'PY' \|\| true\n(.*?)\nPY\n", sh, re.S)
    assert m, "DB_HELPER_PY heredoc not found"
    return m.group(1)


def test_script_path():
    _sq.DB_DIR = _TMP
    _sq.DB_PATH = _DB
    _sq._local = threading.local()
    sync = astorage._sync
    sync.initialize()
    s3 = FakeS3(page_size=2)
    own_nb = sync.notebook_create("ScriptOwn", cells=[{"id": "c1"}])["id"]
    keep_nb = sync.notebook_create("ScriptKeep", cells=[{"id": "c1"}])["id"]
    so, sk = _sid(), _sid()
    sync.notebook_update(own_nb, session_id=so)
    sync.notebook_update(keep_nb, session_id=sk)
    _seed_session(s3, so)
    _seed_session(s3, sk)
    _seed_notebook(s3, own_nb)
    _seed_notebook(s3, keep_nb)
    shared = _shared(own_nb)
    for k in shared:
        s3.objects[k] = b"k"
    src = _script_helper_src()
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
    check("script: notebook deleted from the DB", sync.notebook_get(own_nb) is None, out.getvalue())
    check("script: owned session's 3 prefixes gone", _session_left(s3, so) == [],
          str(_session_left(s3, so)))
    check("script: notebook-outputs/<id>/ and attachments/<id>/ gone",
          _notebook_left(s3, own_nb) == [], str(_notebook_left(s3, own_nb)))
    check("script: other notebook + shared data kept",
          len(_session_left(s3, sk)) == 6 and len(_notebook_left(s3, keep_nb)) == 2
          and all(k in s3.objects for k in shared))
    sync.notebook_delete(keep_nb)


def test_all_checks_passed():
    """pytest entry: the check() calls above don't raise on their own."""
    failed = [n for n, ok in _checks if not ok]
    assert not failed, "failed checks: " + "; ".join(failed)


def main():
    test_prefixes()
    test_session_data_delete()
    test_route()
    test_script_path()
    failed = [n for n, ok in _checks if not ok]
    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")


if __name__ == "__main__":
    main()
