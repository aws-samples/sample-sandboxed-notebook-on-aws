#!/usr/bin/env python3
# test-kind: manual
"""Rebuild benchmark for the persisted per-notebook DAG (design.md §3.8, NFR-2;
delta-plan step 7).

Synthetic notebooks of 60 and 200 cells: every 5th cell is SQL with 10 sources;
the code cells form builder → mutator → mutator → reader chains over the SQL
outputs. For each size it reports p50/p95 of
  * apply_runs (one captured execute folded into the full document),
  * json.dumps of the document,
  * diff_docs (the delta, computed after the RMW, outside the DB lock),
  * the full SQLite notebook_dag_write RMW (lock + read + build + change check +
    write + commit) on a temp DB — the per-execute capture cost (NFR-2) — for:
      - mid-chain re-run   (graph changes: downstream cells go stale),
      - leaf re-run        (graph unchanged; clocks/runtime → runtime-only write),
      - edit changing reads (apply_cells; graph changes),
      - no-op              (an identical runtime twice: nothing written),
and the response payload bytes per case: the full document (what every execute
response carried before the delta transport), the delta, and what is sent after
the size guard (dag_contract.send_full) — the unchanged marker for the no-op.
When TEST_PG_DSN is set the mid re-run RMW is also measured against Postgres;
otherwise it prints "Postgres: not measured".

NFR-2 target: p95 <= 25 ms for capture + rebuild + write, 60 cells, SQLite
(asserted on the worst of the three writing cases). Exit 1 when it is not met,
when the no-op case writes a row / sends more than the marker, or when a leaf
re-run changes the graph.

Run:  python3 tests/dag/bench_dag_rebuild.py   (no network; temp SQLite only)
"""
import json
import os
import statistics
import sys
import tempfile
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import proxy.storage.sqlite_db as sq  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="dag_bench_")
sq.DB_DIR = _TMP
sq.DB_PATH = os.path.join(_TMP, "bench.db")
sq._local = threading.local()
sq._HELD_SESSION_LOCKS.clear()

from app.notebook.dag_contract import diff_docs, json_bytes, send_full  # noqa: E402
from proxy.notebook.dag import RunCapture, apply_cells, apply_runs  # noqa: E402

ITER_FAST = 300
ITER_RMW = 150
NFR2_MS = 25.0
NOW = "2026-10-31T12:00:00.000Z"


def synth(n: int):
    """(cells, captures, runtime) for an n-cell notebook."""
    cells, caps = [], []
    vars_, runs = {}, {}
    last_sql = None
    group = 0
    clock = 0
    for i in range(n):
        cid = f"cell-{i:04d}"
        clock += 1
        if i % 5 == 0:
            ov = f"s{i}"
            code = (f"SELECT a.*, b.v FROM db.t{i} a JOIN db.u{i} b ON a.k = b.k "
                    f"WHERE a.d > DATE '2026-01-01'")
            cells.append({"id": cid, "type": "sql", "code": code, "outputVariable": ov})
            srcs = tuple({"kind": "athena", "id": f"db.t{i}_{k}"} for k in range(10))
            caps.append(RunCapture(cid, "sql", code, ov, True, (), (ov,), (), (), srcs, NOW))
            last_sql = ov
            vars_[ov] = {"defined_by": cid, "last_cell": cid, "last_exec": clock, "modified_by": []}
        else:
            step = i % 5
            df = f"df{group}"
            if step == 1:
                code = f"{df} = {last_sql}.copy()"
                cap = RunCapture(cid, "code", code, None, True, (last_sql,), (df,), (), (), (), NOW)
                vars_[df] = {"defined_by": cid, "last_cell": cid, "last_exec": clock, "modified_by": []}
            elif step in (2, 3):
                code = f"{df}['c{i}'] = {df}['v'] * {i}"
                cap = RunCapture(cid, "code", code, None, True, (df,), (), (), (df,), (), NOW)
                vars_[df]["last_cell"] = cid
                vars_[df]["last_exec"] = clock
                vars_[df]["modified_by"].append(cid)
            else:
                code = f"r{i} = {df}.groupby('k').sum()\nprint(r{i}.shape)"
                cap = RunCapture(cid, "code", code, None, True, (df,), (f"r{i}",), (), (), (), NOW)
                vars_[f"r{i}"] = {"defined_by": cid, "last_cell": cid, "last_exec": clock, "modified_by": []}
                group += 1
            cells.append({"id": cid, "type": "code", "code": code})
            caps.append(cap)
        runs[cid] = {"code_hash": caps[-1].code_hash, "clock": clock, "ok": True}
    runtime = {"clock": clock, "vars": vars_, "runs": runs}
    return cells, caps, runtime


def pct(samples, q):
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def fmt(samples):
    return f"p50 {statistics.median(samples):7.3f} ms   p95 {pct(samples, 0.95):7.3f} ms"


def timed(fn, iters):
    out = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def _sent(w):
    """(kind, bytes) of the response payload for a DagWrite (dag_service._payload rule)."""
    if w.prev is None:
        return "dag", json_bytes(w.doc)
    delta = diff_docs(w.prev, w.doc)
    db = json_bytes(delta)
    if w.wrote and send_full(db, w.nbytes):
        return "dag", json_bytes(w.doc)
    return ("unchanged" if not w.wrote else "dag_delta"), db


def _case(storage, nid, build, iters, reset=None):
    """Time `iters` RMWs of `build`; `reset` (untimed) runs before each."""
    times, payload = [], []
    for _ in range(iters):
        if reset is not None:
            storage.notebook_dag_write(nid, reset)
        t0 = time.perf_counter()
        w = storage.notebook_dag_write(nid, build)
        times.append((time.perf_counter() - t0) * 1000.0)
        payload.append((w, _sent(w)))
    return times, payload


def _summary(p):
    sent = [b for _w, (_k, b) in p]
    sample = p[:10]
    return {"kinds": sorted({k for _w, (k, _b) in p}),
            "sent": statistics.median(sent),
            "delta": statistics.median(json_bytes(diff_docs(w.prev, w.doc)) for w, _ in sample),
            "full": statistics.median(json_bytes(w.doc) for w, _ in sample),
            "wrote": sum(1 for w, _ in p if w.wrote),
            "graph": sum(1 for w, _ in p if w.graph_changed)}


def run_size(n, storage, results, pg=None):
    cells, caps, runtime = synth(n)
    doc = apply_cells(None, cells, trigger="create", now=NOW)
    doc = apply_runs(doc, caps, runtime, "vm-bench", trigger="batch", now=NOW)
    mid = caps[len(caps) // 2 + 2]
    leaf = caps[-1]   # the last reader of the last chain: no outgoing edge

    t_apply = timed(lambda: apply_runs(doc, [mid], runtime, "vm-bench", trigger="execute", now=NOW), ITER_FAST)
    t_dumps = timed(lambda: json.dumps(doc, ensure_ascii=False, separators=(",", ":")), ITER_FAST)
    size = len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    nb = storage.notebook_create(name=f"bench-{n}", cells=cells, dag_build=lambda prev: doc)
    nid = nb["id"]
    clock = [runtime["clock"]]

    def rt_next():
        clock[0] += 1
        return {**runtime, "clock": clock[0]}

    def reset(prev):   # untimed: back to "every cell ran, all current"
        return apply_runs(prev, caps, rt_next(), "vm-bench", trigger="batch", now=NOW)

    def mid_build(prev):
        return apply_runs(prev, [mid], rt_next(), "vm-bench", trigger="execute", now=NOW)

    def leaf_build(prev):
        return apply_runs(prev, [leaf], rt_next(), "vm-bench", trigger="execute", now=NOW)

    t_mid, p_mid = _case(storage, nid, mid_build, ITER_RMW, reset=reset)
    storage.notebook_dag_write(nid, reset)
    storage.notebook_dag_write(nid, leaf_build)
    t_leaf, p_leaf = _case(storage, nid, leaf_build, ITER_RMW)

    fixed_rt = rt_next()

    def same_runtime(prev):
        return apply_runs(prev, [], fixed_rt, "vm-bench", trigger="runtime", now=NOW)
    storage.notebook_dag_write(nid, same_runtime)
    t_noop, p_noop = _case(storage, nid, same_runtime, ITER_RMW)

    # Edit that changes reads: alternate one mid reader cell between two versions.
    ei = next(i for i in range(len(cells) // 2, len(cells))
              if cells[i]["type"] == "code" and "groupby" in cells[i]["code"])
    flip = [0]

    def edit_build(prev):
        flip[0] ^= 1
        edited = [dict(c) for c in cells]
        if flip[0]:
            edited[ei]["code"] = edited[ei]["code"] + "\nextra = s0.shape"
        return apply_cells(prev, edited, trigger="save", now=NOW)
    storage.notebook_dag_write(nid, reset)
    t_edit, p_edit = _case(storage, nid, edit_build, ITER_RMW)

    w_mid = p_mid[-1][0]
    t_diff = timed(lambda: diff_docs(w_mid.prev, w_mid.doc), ITER_FAST)

    s_mid, s_leaf, s_noop, s_edit = _summary(p_mid), _summary(p_leaf), _summary(p_noop), _summary(p_edit)
    print(f"\n{n} cells ({sum(1 for c in cells if c['type'] == 'sql')} SQL × 10 sources, "
          f"{len(doc['edges'])} edges, {len(doc['issues'])} issues)")
    print(f"  apply_runs (1 capture)          {fmt(t_apply)}")
    print(f"  json.dumps(doc)                 {fmt(t_dumps)}")
    print(f"  diff_docs (mid re-run pair)     {fmt(t_diff)}")
    print(f"  serialized doc size             {size:,} bytes (sent on EVERY execute before the delta transport)")
    print("  SQLite notebook_dag_write RMW (capture + rebuild + change check + write):")
    for label, t, sm in (("mid-chain re-run", t_mid, s_mid), ("leaf re-run", t_leaf, s_leaf),
                         ("edit changing reads", t_edit, s_edit), ("no-op (same runtime)", t_noop, s_noop)):
        print(f"    {label:22s} {fmt(t)}   rows written {sm['wrote']}/{len(t)} (graph {sm['graph']})")
    print("  Response payload bytes (median):  full doc  →  delta  →  sent")
    for label, sm in (("mid-chain re-run", s_mid), ("leaf re-run", s_leaf),
                      ("edit changing reads", s_edit), ("no-op (unchanged)", s_noop)):
        print(f"    {label:22s} {int(sm['full']):>9,}  →  {int(sm['delta']):>7,}  →  "
              f"{int(sm['sent']):>7,}  ({'/'.join(sm['kinds'])})")
    results[n] = {"rmw_p95": max(pct(t_mid, 0.95), pct(t_leaf, 0.95), pct(t_edit, 0.95)),
                  "noop": s_noop, "leaf": s_leaf}
    if pg is not None:
        pnb = pg.notebook_create(name=f"bench-{n}", cells=cells, dag_build=lambda prev: doc)
        t_pg, _ = _case(pg, pnb["id"], mid_build, ITER_RMW, reset=reset)
        print(f"  Postgres notebook_dag_write (mid re-run) {fmt(t_pg)}")
        pg.notebook_delete(pnb["id"])


def main() -> int:
    storage = sq.SqliteStorage()
    storage.initialize()
    pg = None
    dsn = os.environ.get("TEST_PG_DSN")
    if dsn:
        from proxy.storage.postgres_db import PostgresStorage
        pg = PostgresStorage(connection_string=dsn)
        pg.initialize(connection_string=dsn)
    print(f"DAG rebuild benchmark (python {sys.version.split()[0]}, "
          f"{ITER_FAST} fast iterations, {ITER_RMW} RMW iterations)")
    results = {}
    for n in (60, 200):
        run_size(n, storage, results, pg)
    if pg is None:
        print("\nPostgres: not measured (TEST_PG_DSN not set)")
    p95 = results[60]["rmw_p95"]
    met = p95 <= NFR2_MS
    print(f"\nNFR-2 (60 cells, SQLite, capture + rebuild + write p95 <= {NFR2_MS:.0f} ms, worst case): "
          f"{'MET' if met else 'NOT MET'} (p95 {p95:.3f} ms)")
    ok = met
    for n, r in results.items():
        if r["noop"]["wrote"] != 0 or r["noop"]["kinds"] != ["unchanged"]:
            print(f"  {n} cells: the no-op case wrote a row or sent more than the unchanged marker")
            ok = False
        if r["leaf"]["graph"] != 0:
            print(f"  {n} cells: a leaf re-run changed the graph")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
