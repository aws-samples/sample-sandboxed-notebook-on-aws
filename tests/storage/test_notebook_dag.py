#!/usr/bin/env python3
# test-kind: offline
"""
notebook_dag storage conformance (design §7.1-§7.6, §12).

Functional checks run against a temp SQLite DB (never the live proxy DB) and,
when TEST_PG_DSN is set and psycopg2 is installed, against Postgres too:
  get/update RMW and version stamping (1..N), 8 concurrent writers with no lost
  write, build -> None keeps the version, missing notebook -> None, cascade +
  explicit delete, notebook_update(cells, dag_build) in the SAME transaction
  (rolled back with the cells when _write_cells fails), notebook_create(dag_build)
  returns "dag", notebook_id_for_session (most recently updated wins, tie -> higher
  id), with_cells=True sees cells saved between the caller's read and the RMW,
  oversize docs are not written (nothing truncated), unparseable / wrong-schema
  rows read as None, and the four v_nb_dag_* views over a fixture doc (incl. a
  cyclic one where v_nb_dag_lineage terminates).

Static parity (always): notebook_dag DDL columns identical in both backends, the
four view names + column order identical, no ALTER for notebook_dag, removed
notebook_cells dataflow columns absent from both, CURATED_VIEWS in
scripts/setup_aurora_postgres.sh and both AI prompts list the four views.

Run:
    .venv/bin/python tests/storage/test_notebook_dag.py
"""
import inspect
import json
import os
import re
import sys
import tempfile
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


DAG_VIEWS = ["v_nb_dag_nodes", "v_nb_dag_edges", "v_nb_dag_issues", "v_nb_dag_lineage"]
DAG_TABLE_COLUMNS = ["notebook_id", "version", "built_at", "dag_json", "updated_at"]
REMOVED_CELL_COLUMNS = ["vars_read_json", "vars_written_json", "stale_reason_json",
                        "sources_read_json"]
EXPECTED_VIEW_COLUMNS = {
    "v_nb_dag_nodes": ["notebook_id", "cell_id", "position", "level", "status",
                       "facts_origin", "reads_json", "writes_json", "mutates_json",
                       "sources_json", "cell_type", "executed_at", "last_run_seq",
                       "stale_kind", "dag_version", "built_at"],
    "v_nb_dag_edges": ["notebook_id", "from_cell_id", "to_cell_id", "var_name", "kind",
                       "origin", "use_kind", "is_backward", "dag_version"],
    "v_nb_dag_issues": ["notebook_id", "cell_id", "code", "severity", "var_name",
                        "detail", "related_json", "source_kind", "source_id",
                        "dag_version"],
    "v_nb_dag_lineage": ["notebook_id", "cell_id", "upstream_cell_id"],
}


def _doc(**extra):
    d = {"schema": 1, "version": 0, "built_at": None, "run_seq": 0,
         "nodes": [], "edges": [], "issues": [],
         "runtime": {"clock": 0, "vars": {}, "runs": {}}, "pending": {}}
    d.update(extra)
    return d


def _append(tag):
    """build(prev) that appends `tag` to doc['log'] (detects lost writes)."""
    def build(prev):
        base = prev if prev is not None else _doc()
        return dict(base, log=list(base.get("log") or []) + [tag])
    return build


def run_change_check(s, kind, raw):
    """delta-plan §0.2: no-op / runtime-only / graph change stamping."""
    import time
    from app.notebook.dag_contract import graph_hash, runtime_hash
    L = f"[{kind}]"
    nb = s.notebook_create("dag-change", cells=[])
    nid = nb["id"]

    def upd_at():
        return raw.q("SELECT updated_at FROM notebook_dag WHERE notebook_id = ?", (nid,))[0][0]

    node = {"cell_id": "c1", "position": 0, "reads": [], "writes": ["x"], "status": "current",
            "executed_at": "2026-01-01T00:00:00.000Z", "last_run_seq": 1, "last_ok_seq": 1}
    w1 = s.notebook_dag_write(nid, lambda prev: _doc(nodes=[dict(node)], run_seq=1))
    check(f"{L} first write: v1 / rv1, wrote, graph+runtime+vm changed",
          w1 and w1.wrote and w1.doc["version"] == 1 and w1.doc["runtime_version"] == 1
          and w1.graph_changed and w1.runtime_changed and w1.vm_changed and w1.prev is None)
    check(f"{L} first write stamps graph_hash/runtime_hash",
          w1.doc["graph_hash"] == graph_hash(w1.doc) and w1.doc["runtime_hash"] == runtime_hash(w1.doc))
    check(f"{L} nbytes = stored dag_json UTF-8 size",
          w1.nbytes == len(raw.q("SELECT dag_json FROM notebook_dag WHERE notebook_id = ?",
                                 (nid,))[0][0].encode("utf-8")))
    u1 = upd_at()
    time.sleep(0.02)

    w2 = s.notebook_dag_write(nid, lambda prev: dict(prev, trigger="execute", built_at="x"))
    check(f"{L} no-op build: wrote=False, doc is prev", w2 and not w2.wrote and w2.doc == w2.prev
          and not (w2.graph_changed or w2.runtime_changed or w2.vm_changed) and w2.nbytes is None)
    g = s.notebook_dag_get(nid)
    check(f"{L} no-op build: version, built_at, runtime_version and updated_at unchanged",
          g["version"] == 1 and g["built_at"] == w1.doc["built_at"]
          and g["runtime_version"] == 1 and upd_at() == u1)

    def rt_only(prev):
        return dict(prev, trigger="runtime",
                    runtime={"clock": 5, "vars": {"x": {"defined_by": "c1"}}, "runs": {}})
    w3 = s.notebook_dag_write(nid, rt_only)
    g = s.notebook_dag_get(nid)
    check(f"{L} runtime-only: version/built_at/trigger kept, runtime_version 2",
          w3.wrote and not w3.graph_changed and w3.runtime_changed
          and g["version"] == 1 and g["built_at"] == w1.doc["built_at"]
          and g.get("trigger") == w1.doc.get("trigger") and g["runtime_version"] == 2)
    check(f"{L} runtime-only: the new runtime is persisted, updated_at advanced",
          g["runtime"]["clock"] == 5 and upd_at() != u1)
    check(f"{L} runtime vars only: vm_changed is False", w3.vm_changed is False)

    def clock_only(prev):
        n = dict(prev["nodes"][0], last_run_seq=2, last_ok_seq=2, executed_at="2026-01-01T00:00:01.000Z")
        return dict(prev, nodes=[n], run_seq=2)
    w4 = s.notebook_dag_write(nid, clock_only)
    check(f"{L} clocks-only: graph unchanged, vm_changed True, rv 3, version 1",
          w4.wrote and not w4.graph_changed and w4.vm_changed
          and w4.doc["version"] == 1 and w4.doc["runtime_version"] == 3)

    w5 = s.notebook_dag_write(nid, lambda prev: dict(prev, nodes=[dict(prev["nodes"][0], reads=["y"])]))
    check(f"{L} graph change: version 2, new built_at, runtime_version kept (3)",
          w5.wrote and w5.graph_changed and not w5.runtime_changed
          and w5.doc["version"] == 2 and w5.doc["runtime_version"] == 3
          and w5.doc["built_at"] > w1.doc["built_at"])
    check(f"{L} rev pairs strictly increase across writes",
          [(w.doc["version"], w.doc["runtime_version"]) for w in (w1, w3, w4, w5)]
          == [(1, 1), (1, 2), (1, 3), (2, 3)])
    w6 = s.notebook_dag_write(nid, lambda prev: None)
    check(f"{L} build -> None: DagWrite(prev, prev, False...)",
          w6 and w6.doc == w6.prev and not w6.wrote and w6.doc["version"] == 2)
    check(f"{L} notebook_dag_write on a missing notebook -> None",
          s.notebook_dag_write(999999999, lambda p: _doc()) is None)


# ── backend factories ────────────────────────────────────────────────────────
def _temp_sqlite():
    import proxy.storage.sqlite_db as sq
    tmp = tempfile.mkdtemp(prefix="nbdag_sqlite_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "nbdag.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    s = sq.SqliteStorage()
    s.initialize()
    return s


class _Raw:
    """Backend-specific raw SQL for test setup/introspection only."""

    def __init__(self, storage, kind):
        self.s, self.kind = storage, kind

    def q(self, sql, params=()):
        if self.kind == "SQLite":
            with self.s._db() as conn:
                return [tuple(r) for r in conn.execute(sql, params).fetchall()]
        sql = sql.replace("?", "%s")
        with self.s._db() as (conn, cur):
            cur.execute(sql, params)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []

    def x(self, sql, params=()):
        if self.kind == "SQLite":
            with self.s._db() as conn:
                conn.execute(sql, params)
            return
        with self.s._db() as (conn, cur):
            cur.execute(sql.replace("?", "%s"), params)


# ── functional suite (any backend) ───────────────────────────────────────────
def run_functional(s, kind):
    print(f"\n=== notebook_dag functional: {kind} ===")
    raw = _Raw(s, kind)
    L = f"[{kind}]"

    # RMW + version stamping
    nb = s.notebook_create("dag-rmw", cells=[{"id": "c1", "code": "x = 1"}])
    nid = nb["id"]
    check(f"{L} notebook_create without dag_build has no 'dag' key", "dag" not in nb)
    check(f"{L} notebook_dag_get with no row -> None", s.notebook_dag_get(nid) is None)
    seen_prev = []

    def first(prev):
        seen_prev.append(prev)
        return _doc(log=["a"], version=42, built_at="bogus", notebook_id=-1)
    d1 = s.notebook_dag_update(nid, first)
    check(f"{L} first build receives prev=None", seen_prev == [None])
    check(f"{L} first stored version is 1 (doc's own version ignored)",
          d1 and d1["version"] == 1)
    check(f"{L} built_at stamped as UTC ms 'Z'",
          d1 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", d1["built_at"] or ""))
    check(f"{L} notebook_id stamped from the row", d1 and d1["notebook_id"] == nid)
    got = s.notebook_dag_get(nid)
    check(f"{L} notebook_dag_get == returned doc", got == d1)
    versions = [d1["version"]]
    for i in range(4):
        versions.append(s.notebook_dag_update(nid, _append(f"r{i}"))["version"])
    check(f"{L} versions advance 1..N", versions == [1, 2, 3, 4, 5])
    check(f"{L} RMW sees the previous doc each time",
          s.notebook_dag_get(nid)["log"] == ["a", "r0", "r1", "r2", "r3"])

    # build -> None
    before = s.notebook_dag_get(nid)
    same = s.notebook_dag_update(nid, lambda prev: None)
    check(f"{L} build returning None returns prev unchanged", same == before)
    check(f"{L} build returning None leaves the version",
          s.notebook_dag_get(nid)["version"] == 5)

    # build exception propagates and writes nothing
    def boom(prev):
        raise RuntimeError("builder bug")
    try:
        s.notebook_dag_update(nid, boom)
        raised = False
    except RuntimeError:
        raised = True
    check(f"{L} exception from build propagates", raised)
    check(f"{L} failed build writes nothing", s.notebook_dag_get(nid)["version"] == 5)

    # missing notebook
    called = []
    check(f"{L} notebook_dag_update on a missing notebook -> None",
          s.notebook_dag_update(999999999, lambda p: called.append(1) or _doc()) is None)
    check(f"{L} build not called for a missing notebook", called == [])
    check(f"{L} no row written for a missing notebook",
          raw.q("SELECT COUNT(*) FROM notebook_dag WHERE notebook_id = ?", (999999999,))[0][0] == 0)
    check(f"{L} notebook_dag_get on a missing notebook -> None",
          s.notebook_dag_get(999999999) is None)

    # concurrency: 8 threads, no lost write, versions 1..N
    nbc = s.notebook_create("dag-conc", cells=[])
    cid = nbc["id"]
    per_thread, n_threads = 6, 8
    results, errors = [], []

    def worker(t):
        try:
            for i in range(per_thread):
                d = s.notebook_dag_update(cid, _append(f"t{t}-{i}"))
                results.append(d["version"])
        except Exception as e:  # pragma: no cover - surfaced via check
            errors.append(repr(e))
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    total = per_thread * n_threads
    final = s.notebook_dag_get(cid)
    check(f"{L} concurrent writers raised no error", not errors)
    check(f"{L} concurrent versions are exactly 1..{total}",
          sorted(results) == list(range(1, total + 1)))
    check(f"{L} no lost write (all {total} appends present)",
          final and len(final["log"]) == total and len(set(final["log"])) == total)
    check(f"{L} final version == {total}", final and final["version"] == total)

    # notebook_create(dag_build=)
    created_prev = []

    def on_create(prev):
        created_prev.append(prev)
        return _doc(trigger="create")
    nbd = s.notebook_create("dag-create", cells=[{"id": "k1"}], dag_build=on_create)
    check(f"{L} notebook_create(dag_build=) returns 'dag'",
          isinstance(nbd.get("dag"), dict) and nbd["dag"]["version"] == 1
          and nbd["dag"]["notebook_id"] == nbd["id"])
    check(f"{L} create build receives prev=None", created_prev == [None])
    check(f"{L} create doc is stored", s.notebook_dag_get(nbd["id"]) == nbd["dag"])
    nbn = s.notebook_create("dag-create-none", cells=[], dag_build=lambda prev: None)
    check(f"{L} notebook_create with build->None has no 'dag' and no row",
          "dag" not in nbn and s.notebook_dag_get(nbn["id"]) is None)

    # notebook_update(cells, dag_build=) — same transaction
    upd_calls = []

    def on_save(prev):
        upd_calls.append(prev["version"] if prev else None)
        # `saves` is a graph key (trigger is a stamp): the save changes the graph.
        return dict(prev or _doc(), trigger="save", saves=len(upd_calls))
    ok = s.notebook_update(nbd["id"], cells=[{"id": "k1"}, {"id": "k2"}], dag_build=on_save)
    check(f"{L} notebook_update(cells, dag_build=) returns True", ok is True)
    check(f"{L} save build received the stored prev", upd_calls == [1])
    check(f"{L} save stored the doc (version 2)",
          s.notebook_dag_get(nbd["id"])["version"] == 2)
    s.notebook_update(nbd["id"], name="renamed", dag_build=on_save)
    check(f"{L} dag_build NOT run when 'cells' is absent", upd_calls == [1])
    check(f"{L} notebook_update on a missing notebook -> False, build not run",
          s.notebook_update(999999999, cells=[], dag_build=on_save) is False
          and upd_calls == [1])

    # rollback: _write_cells fails -> neither cells nor DAG change
    orig = s._write_cells

    def failing_write(*a, **k):
        raise RuntimeError("disk full")
    s._write_cells = failing_write
    try:
        s.notebook_update(nbd["id"], cells=[{"id": "zz"}], dag_build=on_save)
        raised = False
    except RuntimeError:
        raised = True
    finally:
        s._write_cells = orig
    check(f"{L} _write_cells failure propagates", raised)
    check(f"{L} _write_cells failure: DAG not written",
          s.notebook_dag_get(nbd["id"])["version"] == 2 and upd_calls == [1])
    check(f"{L} _write_cells failure: cells unchanged",
          [c["id"] for c in s.notebook_get(nbd["id"])["cells"]] == ["k1", "k2"])
    check(f"{L} _write_cells failure: notebook row rolled back too",
          s.notebook_get(nbd["id"])["name"] == "renamed")

    # rollback: dag_build raises -> the cell write rolls back too (same txn)
    try:
        s.notebook_update(nbd["id"], cells=[{"id": "yy"}], dag_build=boom)
        raised = False
    except RuntimeError:
        raised = True
    check(f"{L} unwrapped dag_build exception propagates from notebook_update", raised)
    check(f"{L} unwrapped dag_build exception rolls back the cells",
          [c["id"] for c in s.notebook_get(nbd["id"])["cells"]] == ["k1", "k2"])

    # with_cells=True sees cells saved between the caller's read and the RMW
    stale_cells = s.notebook_get(nbd["id"])["cells"]
    s.notebook_update(nbd["id"], cells=[{"id": "k1", "code": "a = 1"},
                                        {"id": "k3", "code": "b = a"}])
    seen = {}

    def with_cells_build(prev, cells):
        seen["prev_version"] = prev["version"] if prev else None
        seen["cells"] = [(c["id"], c["code"]) for c in cells]
        seen["keys"] = set(cells[0]) if cells else set()
        return None
    s.notebook_dag_update(nbd["id"], with_cells_build, with_cells=True)
    check(f"{L} with_cells=True passes the cells committed by the later save",
          seen.get("cells") == [("k1", "a = 1"), ("k3", "b = a")]
          and [c["id"] for c in stale_cells] == ["k1", "k2"])
    check(f"{L} with_cells cells use the notebook_get mapper keys",
          {"id", "type", "code", "outputVariable", "lastExecutedCode", "error",
           "executedAt"} <= seen.get("keys", set()))
    check(f"{L} with_cells build returning None leaves the version unchanged",
          seen.get("prev_version") == 2 and s.notebook_dag_get(nbd["id"])["version"] == 2)
    d3 = s.notebook_dag_update(nbd["id"], lambda prev, cells: dict(prev, n=len(cells)),
                               with_cells=True)
    check(f"{L} with_cells build result is stored", d3["version"] == 3 and d3["n"] == 2)

    # oversize: not written, prev returned, nothing truncated
    from app.notebook.dag_contract import DAG_MAX_BYTES
    big = "é" * (DAG_MAX_BYTES // 2 + 16)   # 2 UTF-8 bytes each -> > DAG_MAX_BYTES
    back = s.notebook_dag_update(nbd["id"], lambda prev: dict(prev, blob=big))
    stored = s.notebook_dag_get(nbd["id"])
    check(f"{L} oversize doc returns prev", back == stored and "blob" not in back)
    check(f"{L} oversize doc not written (version unchanged)", stored["version"] == 3)
    check(f"{L} oversize guard measures UTF-8 bytes (len chars < limit)",
          len(big) < DAG_MAX_BYTES)

    # unparseable / wrong schema rows read as None; version keeps counting
    raw.x("UPDATE notebook_dag SET dag_json = ? WHERE notebook_id = ?", ("{not json", nbd["id"]))
    check(f"{L} unparseable dag_json -> None", s.notebook_dag_get(nbd["id"]) is None)
    prevs = []
    d4 = s.notebook_dag_update(nbd["id"], lambda prev: prevs.append(prev) or _doc())
    check(f"{L} unparseable row: build gets prev=None, version continues (4)",
          prevs == [None] and d4["version"] == 4)
    raw.x("UPDATE notebook_dag SET dag_json = ? WHERE notebook_id = ?",
          (json.dumps({"schema": 2, "nodes": []}), nbd["id"]))
    check(f"{L} schema != 1 -> None", s.notebook_dag_get(nbd["id"]) is None)
    raw.x("UPDATE notebook_dag SET dag_json = ?, version = 7, built_at = ? WHERE notebook_id = ?",
          (json.dumps(_doc(version=99, built_at="json", notebook_id=-5)),
           "2026-01-01T00:00:00.000Z", nbd["id"]))
    g = s.notebook_dag_get(nbd["id"])
    check(f"{L} version/built_at/notebook_id forced to the row's",
          g and g["version"] == 7 and g["built_at"] == "2026-01-01T00:00:00.000Z"
          and g["notebook_id"] == nbd["id"])

    run_change_check(s, kind, raw)

    # notebook_id_for_session
    sa = s.notebook_create("sess-a", cells=[], session_id="shared-sess")
    sb = s.notebook_create("sess-b", cells=[], session_id="shared-sess")
    raw.x("UPDATE notebooks SET updated_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", sa["id"]))
    raw.x("UPDATE notebooks SET updated_at = ? WHERE id = ?", ("2025-01-01T00:00:00+00:00", sb["id"]))
    check(f"{L} notebook_id_for_session: most recently updated wins",
          s.notebook_id_for_session("shared-sess") == sa["id"])
    raw.x("UPDATE notebooks SET updated_at = ? WHERE id IN (?, ?)",
          ("2026-02-02T00:00:00+00:00", sa["id"], sb["id"]))
    check(f"{L} notebook_id_for_session: tie -> higher id",
          s.notebook_id_for_session("shared-sess") == max(sa["id"], sb["id"]))
    check(f"{L} notebook_id_for_session returns an int",
          isinstance(s.notebook_id_for_session("shared-sess"), int))
    check(f"{L} notebook_id_for_session unknown -> None",
          s.notebook_id_for_session("no-such-session") is None)
    check(f"{L} notebook_id_for_session falsy -> None",
          s.notebook_id_for_session(None) is None and s.notebook_id_for_session("") is None)

    # delete: explicit delete via notebook_delete, and FK cascade
    check(f"{L} notebook_delete True", s.notebook_delete(nbd["id"]) is True)
    check(f"{L} notebook_delete removes the notebook_dag row",
          raw.q("SELECT COUNT(*) FROM notebook_dag WHERE notebook_id = ?", (nbd["id"],))[0][0] == 0)
    casc = s.notebook_create("dag-cascade", cells=[], dag_build=lambda p: _doc())
    raw.x("DELETE FROM notebooks WHERE id = ?", (casc["id"],))
    check(f"{L} FK cascade removes the notebook_dag row",
          raw.q("SELECT COUNT(*) FROM notebook_dag WHERE notebook_id = ?", (casc["id"],))[0][0] == 0)

    _views(s, raw, kind)


# ── views over fixture docs ──────────────────────────────────────────────────
def _fixture_doc():
    return _doc(
        nodes=[
            {"cell_id": "c1", "position": 0, "cell_type": "code", "level": 0,
             "status": "current", "facts_origin": "runtime", "reads": [],
             "writes": ["df"], "mutates": [],
             "sources": [{"kind": "s3", "id": "s3://b/k.csv"}],
             "executed_at": "2026-05-01T10:00:00.000Z", "last_run_seq": 3,
             "stale_reason": None},
            {"cell_id": "c2", "position": 2, "cell_type": "sql", "level": 1,
             "status": "stale", "facts_origin": "static", "reads": ["df"],
             "writes": ["result"], "mutates": [], "sources": None,
             "executed_at": None, "last_run_seq": None,
             "stale_reason": {"kind": "upstream_changed", "vars": ["df"], "via": ["c1"]}},
            {"cell_id": "c3", "position": 3, "cell_type": "code", "level": 2,
             "status": "never_run", "facts_origin": "static", "reads": ["result"],
             "writes": [], "mutates": [], "sources": [], "executed_at": None,
             "last_run_seq": None, "stale_reason": None},
        ],
        edges=[
            {"from": "c1", "to": "c2", "var": "df", "kind": "defines", "use": "read",
             "origin": "runtime", "backward": False},
            {"from": "c2", "to": "c3", "var": "result", "kind": "defines", "use": "read",
             "origin": "static", "backward": True},
        ],
        issues=[
            {"cell_id": "c1", "code": "external_state", "severity": "info", "var": None,
             "related": [], "source": {"kind": "s3", "id": "s3://b/k.csv"},
             "detail": "reads external data from s3://b/k.csv"},
            {"cell_id": "c3", "code": "out_of_order", "severity": "warning",
             "var": "result", "related": ["c2"], "source": None,
             "detail": "reads result from cell c2, which comes after it in the notebook"},
        ],
    )


def _cyclic_doc():
    return _doc(
        nodes=[{"cell_id": c, "position": i, "cell_type": "code", "level": None,
                "status": "current", "facts_origin": "static", "reads": [],
                "writes": [], "mutates": [], "sources": None} for i, c in
               enumerate(["a", "b", "c"])],
        edges=[
            {"from": "a", "to": "b", "var": "x", "kind": "defines", "use": "read",
             "origin": "static", "backward": False},
            {"from": "b", "to": "a", "var": "y", "kind": "defines", "use": "read",
             "origin": "static", "backward": True},
            {"from": "b", "to": "c", "var": "y", "kind": "defines", "use": "read",
             "origin": "static", "backward": False},
            # duplicate pair on another var: lineage de-duplicates
            {"from": "a", "to": "b", "var": "z", "kind": "defines", "use": "read",
             "origin": "static", "backward": False},
        ],
    )


def _view_cols(s, raw, kind, view):
    if kind == "SQLite":
        return [r[1] for r in raw.q(f"PRAGMA table_info({view})")]
    rows = raw.q("SELECT column_name FROM information_schema.columns "
                 "WHERE table_name = ? ORDER BY ordinal_position", (view,))
    return [r[0] for r in rows]


def _views(s, raw, kind):
    L = f"[{kind}]"
    nb = s.notebook_create("dag-views", cells=[], dag_build=lambda p: _fixture_doc())
    nid = nb["id"]
    ver = nb["dag"]["version"]
    for v in DAG_VIEWS:
        check(f"{L} {v} column order", _view_cols(s, raw, kind, v) == EXPECTED_VIEW_COLUMNS[v])

    nodes = raw.q("SELECT cell_id, position, level, status, facts_origin, reads_json, "
                  "writes_json, mutates_json, sources_json, cell_type, executed_at, "
                  "last_run_seq, stale_kind, dag_version, built_at FROM v_nb_dag_nodes "
                  "WHERE notebook_id = ? ORDER BY position", (nid,))
    check(f"{L} v_nb_dag_nodes: one row per node", [r[0] for r in nodes] == ["c1", "c2", "c3"])
    c1, c2, c3 = nodes
    check(f"{L} v_nb_dag_nodes: scalar columns",
          c1[1:5] == (0, 0, "current", "runtime") and c2[1:5] == (2, 1, "stale", "static")
          and c1[9] == "code" and c2[9] == "sql" and c1[10] == "2026-05-01T10:00:00.000Z"
          and c1[11] == 3 and c2[11] is None)
    check(f"{L} v_nb_dag_nodes: JSON array columns parse",
          json.loads(c1[6]) == ["df"] and json.loads(c2[5]) == ["df"]
          and json.loads(c1[7]) == [])
    check(f"{L} v_nb_dag_nodes: sources_json array / NULL when not captured / []",
          json.loads(c1[8]) == [{"kind": "s3", "id": "s3://b/k.csv"}]
          and c2[8] is None and json.loads(c3[8]) == [])
    check(f"{L} v_nb_dag_nodes: stale_kind", c2[12] == "upstream_changed" and c1[12] is None)
    check(f"{L} v_nb_dag_nodes: dag_version/built_at from the row",
          all(r[13] == ver and r[14] == nb["dag"]["built_at"] for r in nodes))

    edges = raw.q("SELECT from_cell_id, to_cell_id, var_name, kind, origin, use_kind, "
                  "is_backward, dag_version FROM v_nb_dag_edges WHERE notebook_id = ? "
                  "ORDER BY from_cell_id", (nid,))
    check(f"{L} v_nb_dag_edges rows",
          edges == [("c1", "c2", "df", "defines", "runtime", "read", 0, ver),
                    ("c2", "c3", "result", "defines", "static", "read", 1, ver)])

    issues = raw.q("SELECT cell_id, code, severity, var_name, detail, related_json, "
                   "source_kind, source_id, dag_version FROM v_nb_dag_issues "
                   "WHERE notebook_id = ? ORDER BY cell_id", (nid,))
    check(f"{L} v_nb_dag_issues rows",
          len(issues) == 2
          and issues[0][:5] == ("c1", "external_state", "info", None,
                                "reads external data from s3://b/k.csv")
          and json.loads(issues[0][5]) == [] and issues[0][6:] == ("s3", "s3://b/k.csv", ver)
          and issues[1][:4] == ("c3", "out_of_order", "warning", "result")
          and json.loads(issues[1][5]) == ["c2"] and issues[1][6:8] == (None, None))
    check(f"{L} v_nb_dag_issues: detail text in full",
          issues[1][4] == "reads result from cell c2, which comes after it in the notebook")

    lin = set(raw.q("SELECT cell_id, upstream_cell_id FROM v_nb_dag_lineage "
                    "WHERE notebook_id = ?", (nid,)))
    check(f"{L} v_nb_dag_lineage: transitive upstream",
          lin == {("c2", "c1"), ("c3", "c2"), ("c3", "c1")})

    cyc = s.notebook_create("dag-cycle", cells=[], dag_build=lambda p: _cyclic_doc())
    clin = raw.q("SELECT cell_id, upstream_cell_id FROM v_nb_dag_lineage "
                 "WHERE notebook_id = ?", (cyc["id"],))
    check(f"{L} v_nb_dag_lineage terminates on a cycle (no duplicates)",
          len(clin) == len(set(clin)))
    check(f"{L} v_nb_dag_lineage on a cycle: cells on the cycle list themselves",
          set(clin) == {("b", "a"), ("a", "b"), ("a", "a"), ("b", "b"),
                        ("c", "b"), ("c", "a")})

    # a real builder document goes through the views too
    from proxy.notebook import dag as dagmod
    cells = [{"id": "p1", "type": "code", "code": "x = 1"},
             {"id": "m1", "type": "markdown", "code": "# notes"},
             {"id": "p2", "type": "code", "code": "y = x + 1"},
             {"id": "p3", "type": "code", "code": "print(y)"},
             {"id": "p4", "type": "code", "code": "x = 2"}]
    real = s.notebook_create(
        "dag-real", cells=cells,
        dag_build=lambda p: dagmod.apply_cells(p, cells, trigger="create",
                                               now="2026-05-01T10:00:00.000Z"))
    rid = real["id"]
    rnodes = raw.q("SELECT cell_id, position FROM v_nb_dag_nodes WHERE notebook_id = ? "
                   "ORDER BY position", (rid,))
    check(f"{L} builder doc: nodes exclude markdown, position = full-list index",
          rnodes == [("p1", 0), ("p2", 2), ("p3", 3), ("p4", 4)])
    redges = raw.q("SELECT COUNT(*) FROM v_nb_dag_edges WHERE notebook_id = ?", (rid,))[0][0]
    rissues = raw.q("SELECT COUNT(*) FROM v_nb_dag_issues WHERE notebook_id = ?", (rid,))[0][0]
    check(f"{L} builder doc: edge/issue rows match the document",
          redges == len(real["dag"]["edges"]) and rissues == len(real["dag"]["issues"])
          and redges > 0)
    rlin = set(raw.q("SELECT upstream_cell_id FROM v_nb_dag_lineage "
                     "WHERE notebook_id = ? AND cell_id = 'p3'", (rid,)))
    check(f"{L} builder doc: lineage(p3) = {{p1, p2}}", rlin == {("p1",), ("p2",)})

    # id-less cells are skipped by storage; the stored positions are gap-free
    # and equal the document's node.position (cells_build drops them too).
    from proxy.notebook import dag_service
    gap_cells = [{"id": "g1", "type": "code", "code": "a = 1"},
                 {"type": "code", "code": "orphan = 0"},
                 {"id": "g2", "type": "code", "code": "b = a + 1"}]
    gap = s.notebook_create("dag-gap", cells=gap_cells,
                            dag_build=dag_service.cells_build(gap_cells, "create"))
    gid = gap["id"]
    gjoin = raw.q("SELECT n.cell_id, n.position, c.position FROM v_nb_dag_nodes n "
                  "JOIN notebook_cells c ON c.notebook_id = n.notebook_id "
                  "AND c.cell_id = n.cell_id WHERE n.notebook_id = ? ORDER BY n.position",
                  (gid,))
    check(f"{L} id-less cell: node.position == notebook_cells.position (gap-free)",
          [(r[0], int(r[1]), int(r[2])) for r in gjoin] == [("g1", 0, 0), ("g2", 1, 1)])


# ── static parity (no DB) ────────────────────────────────────────────────────
def _table_columns(ddl_text, table):
    m = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \((.*?)\n\s*\)", ddl_text, re.S)
    if not m:
        return None
    cols = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if re.match(r"(PRIMARY KEY|FOREIGN KEY|UNIQUE|CONSTRAINT|CHECK)\b", line):
            continue
        mm = re.match(r"([a-z_][a-z0-9_]*)\s", line)
        if mm:
            cols.append(mm.group(1))
    return cols


def _split_top_level(s):
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _select_columns(view_sql):
    """Output column names of a view body (final SELECT list)."""
    body = re.sub(r"--[^\n]*", "", view_sql)
    sel = body.rsplit("SELECT", 1)[1] if "WITH RECURSIVE" in body else body.split("SELECT", 1)[1]
    sel = re.split(r"\bFROM\b", sel, maxsplit=1)[0]
    out = []
    for item in _split_top_level(sel):
        m = re.search(r"\bAS\s+([a-z_][a-z0-9_]*)\s*$", item)
        out.append(m.group(1) if m else item.split(".")[-1].strip())
    return out


def _sqlite_view_sql(schema, view):
    m = re.search(r"CREATE VIEW " + view + r" AS(.*?);\n", schema, re.S)
    return m.group(1) if m else None


def _pg_view_sql(stmts, view):
    for st in stmts:
        if f"CREATE OR REPLACE VIEW {view} AS" in st:
            return st.split(f"CREATE OR REPLACE VIEW {view} AS", 1)[1]
    return None


def run_static():
    print("\n=== notebook_dag static parity ===")
    import proxy.storage.postgres_db as pg
    import proxy.storage.sqlite_db as sq
    check("importing postgres_db did not need psycopg2", "psycopg2" not in sys.modules)

    pg_text = "\n".join(pg._SCHEMA_STATEMENTS)
    sq_cols = _table_columns(sq.SCHEMA, "notebook_dag")
    pg_cols = _table_columns(pg_text, "notebook_dag")
    check("SQLite notebook_dag DDL columns", sq_cols == DAG_TABLE_COLUMNS)
    check("Postgres notebook_dag DDL columns identical", pg_cols == DAG_TABLE_COLUMNS)
    check("notebook_dag cascades from notebooks in both backends",
          re.search(r"FOREIGN KEY \(notebook_id\) REFERENCES notebooks\(id\) ON DELETE CASCADE",
                    sq.SCHEMA.split("CREATE TABLE IF NOT EXISTS notebook_dag", 1)[1].split(");", 1)[0])
          is not None
          and "notebook_id BIGINT PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE" in pg_text)

    for view in DAG_VIEWS:
        s_sql, p_sql = _sqlite_view_sql(sq.SCHEMA, view), _pg_view_sql(pg._SCHEMA_STATEMENTS, view)
        check(f"{view}: SQLite DROP VIEW IF EXISTS + CREATE VIEW",
              f"DROP VIEW IF EXISTS {view};" in sq.SCHEMA and s_sql is not None)
        check(f"{view}: Postgres CREATE OR REPLACE VIEW", p_sql is not None)
        if s_sql and p_sql:
            check(f"{view}: column names/order identical in both backends",
                  _select_columns(s_sql) == _select_columns(p_sql) == EXPECTED_VIEW_COLUMNS[view])

    for path in ("proxy/storage/sqlite_db.py", "proxy/storage/postgres_db.py"):
        text = open(os.path.join(ROOT, path)).read()
        name = os.path.basename(path)
        check(f"{name}: no ALTER TABLE for notebook_dag",
              not re.search(r"ALTER TABLE\s+notebook_dag", text, re.I))
        check(f"{name}: no ADD COLUMN for the removed dataflow columns",
              not any(re.search(r"ADD COLUMN[^\n]*\b" + c + r"\b", text)
                      for c in REMOVED_CELL_COLUMNS))
        check(f"{name}: removed dataflow columns absent",
              not any(c in text for c in REMOVED_CELL_COLUMNS))
    check("SQLite _migrate has no notebook_dag / notebook_cells entry",
          "notebook_dag" not in inspect.getsource(sq.SqliteStorage._migrate)
          and "notebook_cells" not in inspect.getsource(sq.SqliteStorage._migrate))
    check("v_nb_cells exposes no dataflow column (both backends)",
          not any(c in (_sqlite_view_sql(sq.SCHEMA, "v_nb_cells") or "")
                  or c in (_pg_view_sql(pg._SCHEMA_STATEMENTS, "v_nb_cells") or "")
                  for c in REMOVED_CELL_COLUMNS))

    # locking + interface parity
    check("SQLite notebook_dag_write takes BEGIN IMMEDIATE (notebook_dag_update delegates)",
          "BEGIN IMMEDIATE" in inspect.getsource(sq.SqliteStorage.notebook_dag_write)
          and "notebook_dag_write" in inspect.getsource(sq.SqliteStorage.notebook_dag_update))
    check("both backends: the UPSERT runs only when the stamped doc must be written",
          all("if text is None:" in inspect.getsource(cls._dag_rmw)
              for cls in (sq.SqliteStorage, pg.PostgresStorage)))
    check("Postgres DAG RMW locks the notebooks row FOR UPDATE",
          "FROM notebooks WHERE id = %s FOR UPDATE" in inspect.getsource(pg.PostgresStorage._dag_rmw))
    for m in ("notebook_dag_get", "notebook_dag_update", "notebook_dag_write",
              "notebook_id_for_session",
              "notebook_create", "notebook_update"):
        check(f"{m}: identical signature in both backends",
              inspect.signature(getattr(sq.SqliteStorage, m))
              == inspect.signature(getattr(pg.PostgresStorage, m)))
    check("notebook_id_for_session orders by updated_at DESC, id DESC in both",
          all("ORDER BY updated_at DESC, id DESC LIMIT 2" in inspect.getsource(
              getattr(cls, "notebook_id_for_session")) for cls in (sq.SqliteStorage, pg.PostgresStorage)))
    check("both backends delete notebook_dag explicitly in notebook_delete",
          all("DELETE FROM notebook_dag WHERE notebook_id" in inspect.getsource(cls.notebook_delete)
              for cls in (sq.SqliteStorage, pg.PostgresStorage)))

    # grants + prompts
    sh = open(os.path.join(ROOT, "scripts", "setup_aurora_postgres.sh")).read()
    check("setup script imports the public schema accessors",
          "from proxy.storage.postgres_db import schema_statements, view_names" in sh
          and "_SCHEMA_STATEMENTS" not in sh)
    check("setup script derives CURATED_VIEWS from view_names()",
          'CURATED_VIEWS = [v for v in view_names() if v.startswith("v_nb_")]' in sh)
    curated = [v for v in pg.view_names() if v.startswith("v_nb_")]
    check("curated views include the four DAG views",
          all(v in curated for v in DAG_VIEWS))
    check("curated Postgres views equal the SQLite v_nb_* views",
          set(curated) == set(re.findall(r"CREATE VIEW (v_nb_[a-z_]+)", sq.SCHEMA)))
    for p in ("agent/ai_assistant/prompts.py", "proxy/notebook/ai/prompts.py"):
        text = open(os.path.join(ROOT, p)).read()
        check(f"{p}: views-only list names the four DAG views",
              "v_nb_session_cost, v_nb_dag_nodes, v_nb_dag_edges, v_nb_dag_issues, "
              "v_nb_dag_lineage)" in text)


# ── entry points ─────────────────────────────────────────────────────────────
def _run_postgres_if_configured():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        print("\n(skipping live Postgres: set TEST_PG_DSN to run it; static parity covers Postgres)")
        return
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        print("\n(skipping live Postgres: psycopg2 not installed)")
        return
    from proxy.storage.postgres_db import PostgresStorage
    try:
        storage = PostgresStorage(connection_string=dsn)
        run_functional(storage, "Postgres")
    except Exception as e:
        check(f"[Postgres] backend ran without error: {e}", False)


def test_notebook_dag_sqlite():
    _checks.clear()
    run_functional(_temp_sqlite(), "SQLite")
    failed = [n for n, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


def test_notebook_dag_static_parity():
    _checks.clear()
    run_static()
    failed = [n for n, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


def test_notebook_dag_postgres():
    if not os.environ.get("TEST_PG_DSN"):
        import pytest
        pytest.skip("TEST_PG_DSN not set")
    _checks.clear()
    _run_postgres_if_configured()
    failed = [n for n, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    run_static()
    run_functional(_temp_sqlite(), "SQLite")
    _run_postgres_if_configured()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        for n, ok in _checks:
            if not ok:
                print(f"  ❌ {n}")
    sys.exit(0 if passed == len(_checks) else 1)
