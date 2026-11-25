#!/usr/bin/env python3
# test-kind: offline
"""
Offline unit checks: notebook_cells.image_s3_key flows through the SQLite ->
Aurora sync worker with no code change.

  * introspect() on a fresh SQLite schema lists image_s3_key among the
    notebook_cells columns;
  * batch_upsert's ON CONFLICT SET clause updates it from EXCLUDED, so a
    null-out (cleared chart) propagates;
  * the parameter for a stored key is a plain stringValue and a None is isNull.

No AWS calls (captured writer), no live DB (temp SQLite file).
Run:
    .venv/bin/python tests/sync/test_sync_cell_image_unit.py
"""
import os
import sqlite3
import sys
import tempfile
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _run_all():
    print("\n[sync] notebook_cells.image_s3_key")
    import proxy.storage.sqlite_db as sq
    import sync.sync_to_aurora as s
    tmp = tempfile.mkdtemp(prefix="syncimg_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "syncimg.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    st = sq.SqliteStorage()
    st.initialize()
    nid = st.notebook_create("sync-img", cells=[])["id"]
    key = f"notebook-outputs/{nid}/c1/{'b' * 64}.png"
    st.notebook_update(str(nid), cells=[{"id": "c1", "imageS3Key": key}, {"id": "c2"}])
    conn = sqlite3.connect(sq.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ti = s.introspect(conn)["notebook_cells"]
        check("introspected notebook_cells columns include image_s3_key", "image_s3_key" in ti.columns)
        rows = [dict(r) for r in s._rows_since(conn, ti, None)]
    finally:
        conn.close()
    check("the stored key is read by _rows_since",
          {r["cell_id"]: r["image_s3_key"] for r in rows} == {"c1": key, "c2": None})

    class Captured(s.AuroraDataApiWriter):
        def __init__(self):
            super().__init__("arn:cluster", "arn:secret", "db", "us-west-2", dry_run=True)
            self.calls = []

        def _execute(self, sql, param_sets):
            self.calls.append((sql, param_sets))

    w = Captured()
    w.batch_upsert("notebook_cells", ti.columns, ti.pk_cols, rows)
    sql = w.calls[0][0] if w.calls else ""
    check('upsert SET clause has "image_s3_key" = EXCLUDED."image_s3_key"',
          '"image_s3_key" = EXCLUDED."image_s3_key"' in sql)
    params = {p["name"]: p for p in (w.calls[0][1][0] if w.calls else [])}
    params2 = {p["name"]: p for p in (w.calls[0][1][1] if w.calls else [])}
    by_cell = {params.get("cell_id", {}).get("value", {}).get("stringValue"): params,
               params2.get("cell_id", {}).get("value", {}).get("stringValue"): params2}
    check("a stored key is sent as stringValue",
          by_cell.get("c1", {}).get("image_s3_key", {}).get("value") == {"stringValue": key})
    check("a NULL key is sent as isNull (null-out propagates)",
          by_cell.get("c2", {}).get("image_s3_key", {}).get("value") == {"isNull": True})


def test_sync_cell_image_unit():
    _checks.clear()
    _run_all()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    _run_all()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
