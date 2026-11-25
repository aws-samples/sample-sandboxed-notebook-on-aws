#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: DELETE /sessions/{id} also removes the session's workbook_intel and
local_file_entities rows (proxy/platform/routes/sessions.py::_delete_session_sync,
live-check-2 F4).
Covers: the S3 prefix and the session's rows are removed while another session's
rows stay; a missing bucket returns HTTP 503 {"error": "Bucket not found"} and
touches no rows; a failing session_intel_delete is best effort (S3 still deleted, the
response is still status: deleted).
Uses a temp SQLite DB (repointed BEFORE proxy.storage is used) and a fake S3, so
it never touches proxy/data/microvm.db or AWS.
Run with python3:
    python3 tests/proxy/test_delete_cascade_routes.py
Exits non-zero on any failure.
"""
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

# Repoint the SQLite backend at a temp DB BEFORE anything opens a connection
# (same as tests/storage/test_storage_conformance.py::_run_sqlite).
import proxy.storage.sqlite_db as sq  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="delcascade_")
sq.DB_DIR = _TMP
sq.DB_PATH = os.path.join(_TMP, "t.db")
sq._local = threading.local()

from proxy.storage import storage  # noqa: E402

storage.initialize()

import proxy.platform.routes.sessions as sess  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


class _FakeS3:
    def __init__(self, keys):
        self.objects = {k: b"x" for k in keys}

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


class _VM:
    def __init__(self, bucket="bkt"):
        self.bucket = bucket

    def get_artifacts_bucket(self):
        return self.bucket


def _seed():
    for sid in ("sa", "sb"):
        storage.workbook_intel_save(sid, f"sessions/{sid}/workbook-intel.json")
        storage.local_entity_upsert(sid, f"/tmp/{sid}.csv",
                                    doc_s3_key=f"sessions/{sid}/local-entities/x.json", status="ready")


def _rows(sid):
    return storage.workbook_intel_get(sid) is not None, len(storage.local_entity_list(sid))


def _repoint():
    """(Re)point the singleton at this file's temp DB. Under pytest another test
    module may have repointed sqlite_db's globals since import."""
    os.makedirs(_TMP, exist_ok=True)
    sq.DB_DIR = _TMP
    sq.DB_PATH = os.path.join(_TMP, "t.db")
    sq._local = threading.local()
    storage.initialize()


def _run():
    _repoint()
    orig_get_client = sess.aws_clients.get_client
    orig_sid = storage.session_intel_delete
    try:
        # 1. happy path
        _seed()
        s3 = _FakeS3(["sessions/sa/workbook-intel.json", "sessions/sa/local-entities/x.json",
                      "sessions/sb/workbook-intel.json"])
        sess.aws_clients.get_client = lambda *a, **k: s3
        out = sess._delete_session_sync(_VM(), "sa")
        check("delete: response unchanged", out == {"status": "deleted", "session_id": "sa"}, str(out))
        check("delete: sessions/sa/ objects removed, sb kept",
              sorted(s3.objects) == ["sessions/sb/workbook-intel.json"], str(sorted(s3.objects)))
        check("delete: sa intel + local entities gone", _rows("sa") == (False, 0), str(_rows("sa")))
        check("delete: sb rows kept", _rows("sb") == (True, 1), str(_rows("sb")))

        # 2. no bucket: early return, rows untouched
        _seed()
        out = sess._delete_session_sync(_VM(bucket=None), "sa")
        check("no bucket: 503 error returned",
              getattr(out, "status_code", None) == 503
              and json.loads(out.body) == {"error": "Bucket not found"}, str(out))
        check("no bucket: sa rows untouched", _rows("sa") == (True, 1), str(_rows("sa")))

        # 3. storage failure is best effort
        s3 = _FakeS3(["sessions/sa/workbook-intel.json"])
        sess.aws_clients.get_client = lambda *a, **k: s3

        def _boom(_sid):
            raise RuntimeError("db down")

        storage.session_intel_delete = _boom
        out = sess._delete_session_sync(_VM(), "sa")
        check("storage error: still status deleted", out == {"status": "deleted", "session_id": "sa"}, str(out))
        check("storage error: S3 still deleted", s3.objects == {}, str(s3.objects))
    finally:
        sess.aws_clients.get_client = orig_get_client
        if storage.session_intel_delete is not orig_sid:
            del storage.session_intel_delete  # drop the instance override


def test_main():
    _fails.clear()
    try:
        _run()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    assert not _fails, _fails


if __name__ == "__main__":
    try:
        _run()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if _fails:
        print(f"FAILED: {len(_fails)}")
        for f in _fails:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
