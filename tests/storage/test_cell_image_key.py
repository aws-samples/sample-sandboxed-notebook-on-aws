#!/usr/bin/env python3
# test-kind: offline
"""
notebook_cells.image_s3_key (chart PNG location) through the storage interface.

SQLite (temp DB): round-trip, key-ownership validation on every write (foreign
notebook / cell, traversal, uppercase hex, wrong extension → stored as NULL),
numeric cell ids, notebook_cell_image_keys, the notebook_create echo, version
snapshots/restore carrying no key, v_nb_cells.has_image, and the fail-fast on a
pre-chart-persistence DB (not a migration). Static: both backends run the
schema check BEFORE any DDL, and the Postgres check SQL is present.

Run:
    python3 tests/storage/test_cell_image_key.py
"""
import hashlib
import inspect
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


SHA = hashlib.sha256(b"chart").hexdigest()
SHA2 = hashlib.sha256(b"chart2").hexdigest()


def _key(nid, cid, sha=SHA, ext="png"):
    return f"notebook-outputs/{nid}/{cid}/{sha}.{ext}"


def _temp_sqlite(name="img.db"):
    import proxy.storage.sqlite_db as sq
    tmp = tempfile.mkdtemp(prefix="cellimg_sqlite_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, name)
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    s = sq.SqliteStorage()
    s.initialize()
    return s


def _sqlite_checks():
    print("\n[SQLite] image_s3_key")
    s = _temp_sqlite()
    a = s.notebook_create(name="A", cells=[])
    b = s.notebook_create(name="B", cells=[])
    na, nb = a["id"], b["id"]
    cells = [
        {"id": "ok", "code": "plot", "imageS3Key": _key(na, "ok"), "image": "data:image/png;base64,AAAA"},
        {"id": "foreign_nb", "imageS3Key": _key(nb, "foreign_nb")},
        {"id": "foreign_cell", "imageS3Key": _key(na, "ok")},
        {"id": "trav", "imageS3Key": f"notebook-outputs/{na}/trav/../../{nb}/x/{SHA}.png"},
        {"id": "upper", "imageS3Key": _key(na, "upper", SHA.upper())},
        {"id": "ext", "imageS3Key": _key(na, "ext", ext="jpg")},
        {"id": 42, "imageS3Key": _key(na, "42")},
        {"id": "none"},
    ]
    check("notebook_update(cells=...) succeeds", s.notebook_update(str(na), cells=cells))
    got = {c["id"]: c for c in s.notebook_get(str(na))["cells"]}
    check("own key round-trips", got["ok"]["imageS3Key"] == _key(na, "ok"))
    check("base64 image is never stored (returned as None)", got["ok"]["image"] is None)
    for cid in ("foreign_nb", "foreign_cell", "trav", "upper", "ext"):
        check(f"{cid}: stored as None", got[cid]["imageS3Key"] is None)
    check("numeric cell id matches its str form", got["42"]["imageS3Key"] == _key(na, "42"))
    check("cell without a key → None", got["none"]["imageS3Key"] is None)
    check("notebook_cell_image_keys returns only stored keys",
          s.notebook_cell_image_keys(na) == {"ok": _key(na, "ok"), "42": _key(na, "42")})
    check("notebook_cell_image_keys for an unknown notebook → {}", s.notebook_cell_image_keys(999999) == {})
    listed = next(n for n in s.notebook_list() if n["id"] == na)
    check("notebook_list carries imageS3Key",
          next(c for c in listed["cells"] if c["id"] == "ok")["imageS3Key"] == _key(na, "ok"))

    with s._db() as conn:
        rows = dict(conn.execute("SELECT cell_id, has_image FROM v_nb_cells WHERE notebook_id = ?",
                                 (na,)).fetchall())
    check("v_nb_cells.has_image 1 for stored keys, 0 otherwise",
          rows.get("ok") == 1 and rows.get("42") == 1 and rows.get("trav") == 0 and rows.get("none") == 0)

    # notebook_create echo: only validated keys (a client can't learn a new id's
    # key shape from the echo, and a foreign key is never echoed back).
    created = s.notebook_create(name="C", cells=[{"id": "x", "imageS3Key": _key(na, "x")}])
    check("notebook_create drops a key for another notebook (echo + stored)",
          created["cells"][0]["imageS3Key"] is None
          and s.notebook_get(str(created["id"]))["cells"][0]["imageS3Key"] is None)

    # Versions are document-only: a snapshot/restore carries no key.
    v = s.notebook_version_create(notebook_id=na, cells=s.notebook_get(str(na))["cells"],
                                  label="v", trigger="manual")
    full = s.notebook_version_get(v["version_group_id"])
    check("version snapshot cells carry no imageS3Key",
          all(not c.get("imageS3Key") for c in full["cells"]))
    s.notebook_update(str(na), cells=full["cells"])
    check("restore (write version cells) leaves no key", s.notebook_cell_image_keys(na) == {})

    # Clear → None persisted.
    s.notebook_update(str(na), cells=[{"id": "ok", "imageS3Key": _key(na, "ok")}])
    s.notebook_update(str(na), cells=[{"id": "ok", "imageS3Key": None}])
    check("clearing the key persists NULL", s.notebook_cell_image_keys(na) == {})

    # Delete notebook → no dangling DB reference.
    s.notebook_update(str(na), cells=[{"id": "ok", "imageS3Key": _key(na, "ok")}])
    s.notebook_delete(str(na))
    check("notebook_delete leaves no image key rows", s.notebook_cell_image_keys(na) == {})


def _fail_fast_checks():
    print("\n[SQLite] fail fast on a pre-image_s3_key DB (no migration)")
    import proxy.storage.sqlite_db as sq
    from proxy.storage._cell_fields import SCHEMA_RESET_MESSAGE
    tmp = tempfile.mkdtemp(prefix="cellimg_old_")
    path = os.path.join(tmp, "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notebook_cells (notebook_id INTEGER, cell_id TEXT, position INTEGER)")
    conn.commit()
    conn.close()
    sq.DB_DIR, sq.DB_PATH = tmp, path
    sq._local = threading.local()
    s = sq.SqliteStorage()
    try:
        s.initialize()
        check("old-schema DB → RuntimeError", False)
    except RuntimeError as e:
        check("old-schema DB → RuntimeError naming --AuroraReset",
              str(e) == SCHEMA_RESET_MESSAGE and "--AuroraReset" in str(e))
    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notebook_cells)").fetchall()]
    n_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    conn.close()
    check("…and nothing was altered or created", cols == ["notebook_id", "cell_id", "position"]
          and n_tables == 1)
    _temp_sqlite("fresh.db")
    check("a fresh DB initializes", True)


def _static_checks():
    print("\n[Static] schema check precedes DDL in both backends")
    import proxy.storage.postgres_db as pg
    import proxy.storage.sqlite_db as sq
    pg_src = inspect.getsource(pg.PostgresStorage.initialize)
    i_check = pg_src.find("_IMAGE_KEY_SCHEMA_CHECK_SQL")
    i_loop = pg_src.find("for stmt in _SCHEMA_STATEMENTS")
    check("postgres: image_s3_key check before the _SCHEMA_STATEMENTS loop", 0 <= i_check < i_loop)
    check("postgres: check SQL inspects information_schema for image_s3_key",
          "information_schema.columns" in pg._IMAGE_KEY_SCHEMA_CHECK_SQL
          and "'image_s3_key'" in pg._IMAGE_KEY_SCHEMA_CHECK_SQL
          and "information_schema.tables" in pg._IMAGE_KEY_SCHEMA_CHECK_SQL)
    sq_src = inspect.getsource(sq.SqliteStorage.initialize)
    i_pragma = sq_src.find("PRAGMA table_info(notebook_cells)")
    i_exec = sq_src.find("executescript(SCHEMA)")
    check("sqlite: PRAGMA check before executescript(SCHEMA)", 0 <= i_pragma < i_exec)
    for path in ("proxy/storage/sqlite_db.py", "proxy/storage/postgres_db.py"):
        text = open(os.path.join(ROOT, path)).read()
        check(f"{os.path.basename(path)}: no ALTER/ADD COLUMN for image_s3_key",
              "ADD COLUMN image_s3_key" not in text and '"image_s3_key", "TEXT"' not in text)
    from proxy.storage.interface import StorageBackend
    check("interface declares notebook_cell_image_keys",
          "notebook_cell_image_keys" in StorageBackend.__abstractmethods__)


def test_cell_image_key():
    _checks.clear()
    _sqlite_checks()
    _fail_fast_checks()
    _static_checks()
    failed = [n for n, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    _sqlite_checks()
    _fail_fast_checks()
    _static_checks()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
