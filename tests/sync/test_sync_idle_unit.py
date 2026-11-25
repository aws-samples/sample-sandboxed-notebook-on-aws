#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the SQLite -> Aurora sync worker's idle early-return and
reconcile cadence (proxy-log-chatter B1, sync/sync_to_aurora.py run_cycle):
  * the first cycle in a process is a RECONCILE cycle (identity discovery,
    begin/commit, one row-count drift statement) even with nothing to sync;
  * an idle cycle (no rows past the cursors, no tombstones) makes NO Aurora call
    at all and returns status "idle" without writing a _sync_run row;
  * a changed row is synced (upsert inside begin/commit) and the cursor advances;
    the identity query is NOT repeated (cached once per process);
  * a deleted notebook (tombstone) is synced as a DELETE, then the next cycle is
    idle again;
  * the periodic reconcile runs again once SYNC_RECONCILE_INTERVAL_SEC has passed
    (identity re-read), and never with an interval of 0 (startup only);
  * the drift check only logs: a count mismatch gives one WARNING and changes no
    data or cursor;
  * a reconcile cycle that hits the Serverless v2 auto-pause resume race retries
    (DatabaseResumingException) and succeeds; a failed cycle does not mark the
    reconcile done, so the next cycle retries it;
  * run_once(reconcile=True) (the --once CLI) always reconciles.
No AWS: boto3.client is replaced by a recording fake rds-data client; the source
is a temp SQLite file initialized through the storage layer.
Run:
    .venv/bin/python tests/sync/test_sync_idle_unit.py
"""
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
_checks = []
def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  ({detail})" if detail and not ok else ""))
class DatabaseResumingException(Exception):
    pass
class FakeRdsData:
    """Records every Data API call; answers the identity, count and
    parent-scoped SELECT queries the worker issues."""
    class exceptions:
        DatabaseResumingException = DatabaseResumingException
    def __init__(self):
        self.calls = []
        self.resume_failures = 0
        self.counts = None        # table -> count override for the drift check
        self.fail_begin = False
    def _rec(self, name, kw):
        self.calls.append((name, kw.get("sql", "")))
    def execute_statement(self, **kw):
        self._rec("execute_statement", kw)
        if self.resume_failures:
            self.resume_failures -= 1
            raise DatabaseResumingException("resuming")
        sql = kw.get("sql", "")
        if "information_schema.columns" in sql:
            return {"records": [[{"stringValue": "vm_metrics"}]]}
        if " UNION ALL " in sql or sql.startswith("SELECT '"):
            out = []
            for part in sql.split(" UNION ALL "):
                t = part.split("'")[1]
                n = (self.counts or {}).get(t, LOCAL_COUNTS.get(t, 0))
                out.append([{"stringValue": t}, {"longValue": n}])
            return {"records": out}
        return {"records": []}
    def batch_execute_statement(self, **kw):
        self._rec("batch_execute_statement", kw)
        return {}
    def begin_transaction(self, **kw):
        self._rec("begin_transaction", kw)
        if self.fail_begin:
            raise RuntimeError("begin failed")
        return {"transactionId": "tx-1"}
    def commit_transaction(self, **kw):
        self._rec("commit_transaction", kw)
        return {}
    def rollback_transaction(self, **kw):
        self._rec("rollback_transaction", kw)
        return {}
    def names(self):
        return [c[0] for c in self.calls]
    def sqls(self):
        return [c[1] for c in self.calls]
LOCAL_COUNTS: dict = {}
FAKE = FakeRdsData()
def _setup():
    import proxy.storage.sqlite_db as sq
    import sync.sync_to_aurora as s
    tmp = tempfile.mkdtemp(prefix="syncidle_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "syncidle.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    st = sq.SqliteStorage()
    st.initialize()
    s.DB_PATH = sq.DB_PATH
    s.boto3.client = lambda *a, **k: FAKE
    s.time.sleep = lambda *_: None            # resume backoff: no real wait
    s._IDENTITY_CACHE.clear()
    s._PROCESS_STATE["last_reconcile"] = None
    for k, v in {"AURORA_DATA_API_CLUSTER_ARN": "arn:aws:rds:us-west-2:000000000000:cluster:test",
                 "AURORA_DATA_API_RW_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:000000000000:secret:test",
                 "AURORA_DATA_API_DATABASE": "db"}.items():
        os.environ[k] = v
    return s, st, sq.DB_PATH
def _local_counts(db_path, s):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = s.introspect(conn)
        return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                for t, ti in tables.items() if ti.watermark}
    finally:
        conn.close()
def _runs(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT count(*) FROM _sync_run").fetchone()[0]
    finally:
        conn.close()
def _cycle(s, **kw):
    FAKE.calls.clear()
    return s.run_once(**kw)
def main():
    s, st, db = _setup()
    LOCAL_COUNTS.update(_local_counts(db, s))

    print("\n[sync] startup reconcile, then idle")
    r = _cycle(s)
    names = FAKE.names()
    check("first cycle in the process is a reconcile (status ok)", r["status"] == "ok", r)
    check("reconcile: identity discovery, begin, commit, drift count",
          any("information_schema" in q for q in FAKE.sqls()) and "begin_transaction" in names
          and "commit_transaction" in names and any(" UNION ALL " in q for q in FAKE.sqls()))
    runs_before = _runs(db)
    r = _cycle(s)
    check("idle cycle returns status 'idle'", r["status"] == "idle", r)
    check("idle cycle makes ZERO Aurora Data API calls", FAKE.calls == [], FAKE.names())
    check("idle cycle writes no _sync_run row", _runs(db) == runs_before)
    for _ in range(5):
        _cycle(s)
    check("five more idle cycles: still zero calls", FAKE.calls == [])

    print("\n[sync] a changed row is synced")
    time.sleep(0.01)
    nid = st.notebook_create("sync-idle-test", cells=[])["id"]
    r = _cycle(s)
    names = FAKE.names()
    check("cycle after a change syncs (status ok, upserted > 0)",
          r["status"] == "ok" and r["rows_upserted"] > 0, r)
    check("the upsert runs inside begin/commit",
          names[0] == "begin_transaction" and names[-1] == "commit_transaction"
          and any('INSERT INTO "notebooks"' in q for q in FAKE.sqls()), names)
    check("identity tables NOT re-queried (cached per process)",
          not any("information_schema" in q for q in FAKE.sqls()))
    check("no drift check outside a reconcile", not any(" UNION ALL " in q for q in FAKE.sqls()))
    r = _cycle(s)
    check("next cycle is idle again (cursor advanced)", r["status"] == "idle" and FAKE.calls == [])

    print("\n[sync] a delete is synced")
    st.notebook_delete(nid)
    r = _cycle(s)
    check("tombstone replayed as a DELETE (rows_deleted >= 1)",
          r["status"] == "ok" and r["rows_deleted"] >= 1
          and any('DELETE FROM "notebooks"' in q for q in FAKE.sqls()), r)
    r = _cycle(s)
    check("after the delete the next cycle is idle", r["status"] == "idle" and FAKE.calls == [])

    print("\n[sync] periodic reconcile")
    s._PROCESS_STATE["last_reconcile"] -= s._RECONCILE_INTERVAL_SEC + 1
    r = _cycle(s)
    check("reconcile runs again once the interval has passed (no data change)",
          r["status"] == "ok" and "begin_transaction" in FAKE.names())
    check("periodic reconcile re-reads the identity tables",
          any("information_schema" in q for q in FAKE.sqls()))
    r = _cycle(s)
    check("and then idle again", r["status"] == "idle" and FAKE.calls == [])
    saved = s._RECONCILE_INTERVAL_SEC
    s._RECONCILE_INTERVAL_SEC = 0
    s._PROCESS_STATE["last_reconcile"] -= 10 ** 6
    r = _cycle(s)
    check("interval 0 = startup only: no periodic reconcile", r["status"] == "idle" and FAKE.calls == [])
    s._RECONCILE_INTERVAL_SEC = saved
    r = _cycle(s, reconcile=True)
    check("run_once(reconcile=True) (the --once CLI) always reconciles",
          r["status"] == "ok" and "begin_transaction" in FAKE.names())

    print("\n[sync] drift check only logs")
    LOCAL_COUNTS.clear(); LOCAL_COUNTS.update(_local_counts(db, s))
    FAKE.counts = {"notebooks": LOCAL_COUNTS.get("notebooks", 0) + 7}
    cap = []
    h = logging.Handler(); h.emit = lambda rec: cap.append(rec)
    logging.getLogger("sync_to_aurora").addHandler(h)
    try:
        conn = sqlite3.connect(db)
        cursors_before = conn.execute("SELECT * FROM _sync_cursor ORDER BY table_name").fetchall()
        conn.close()
        r = _cycle(s, reconcile=True)
        conn = sqlite3.connect(db)
        cursors_after = conn.execute("SELECT * FROM _sync_cursor ORDER BY table_name").fetchall()
        conn.close()
    finally:
        logging.getLogger("sync_to_aurora").removeHandler(h)
        FAKE.counts = None
    drift = [rec for rec in cap if "row-count drift" in rec.getMessage()]
    check("count mismatch -> one WARNING naming the table",
          len(drift) == 1 and drift[0].levelno == logging.WARNING and "notebooks" in drift[0].getMessage())
    check("drift check writes nothing to Aurora",
          not any(q.startswith(("INSERT", "DELETE", "UPDATE")) for q in FAKE.sqls()))
    check("drift check leaves the cursors unchanged", cursors_before == cursors_after)
    check("the cycle still reports ok", r["status"] == "ok")

    print("\n[sync] first sync after an auto-pause")
    s._PROCESS_STATE["last_reconcile"] = None
    s._IDENTITY_CACHE.clear()
    FAKE.resume_failures = 2
    r = _cycle(s)
    check("DatabaseResumingException is retried and the reconcile succeeds",
          r["status"] == "ok" and FAKE.resume_failures == 0, r)
    FAKE.fail_begin = True
    s._PROCESS_STATE["last_reconcile"] = None
    r = _cycle(s)
    FAKE.fail_begin = False
    check("a failed reconcile reports error", r["status"] == "error")
    check("... and is not marked done", s._PROCESS_STATE["last_reconcile"] is None)
    r = _cycle(s)
    check("the next cycle retries the reconcile and succeeds",
          r["status"] == "ok" and "begin_transaction" in FAKE.names())

    print("\n[sync] dry-run")
    r = s.SyncEngine(s.AuroraDataApiWriter("a", "b", "db", "us-west-2", dry_run=True)).run_cycle()
    check("dry-run idle cycle returns idle", r["status"] == "idle", r)
def test_sync_idle_unit():
    _checks.clear()
    main()
    failed = [n for n, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"
if __name__ == "__main__":
    main()
    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
    sys.exit(1 if failed else 0)
