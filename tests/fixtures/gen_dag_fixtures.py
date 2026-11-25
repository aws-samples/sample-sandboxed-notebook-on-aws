#!/usr/bin/env python3
"""
Generate the DAG document fixtures consumed by the web node tests
(web/src/services/__tests__/depGraph.test.mjs, lineage.test.mjs).

Every document is REAL builder output (proxy/notebook/dag.py), so the JS
traversal/presentation code is tested against what the proxy stores. Storage
stamps the documents (version, runtime_version, graph_hash, runtime_hash,
built_at, notebook_id); this script stamps them with storage's own stamp_doc
(deterministic: built_at from a fixed base time).

Output: tests/fixtures/dag_<group>.json, each
    {"_generated_by": ..., "scenarios": {name: {"cells": [...], "doc": {...}}}}
and tests/fixtures/dag_deltas.json
    {"_generated_by": ..., "cases": {name: {"prev", "delta", "next", "graph_hash"}}}
(stamped prev/next pairs and dag_contract.diff_docs between them).
`cells` are the builder inputs (storage cell-mapper keys: id, type, code,
outputVariable); the JS tests derive live cell objects from them.

Deterministic and checked in. Regenerate after a builder change:
    python3 tests/fixtures/gen_dag_fixtures.py
"""
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app.notebook.static_analysis import analyze_cell  # noqa: E402
from proxy.notebook import dag  # noqa: E402
from proxy.notebook.dag import RunCapture  # noqa: E402
from proxy.storage._dag_doc import stamp_doc  # noqa: E402
from app.notebook.dag_contract import diff_docs  # noqa: E402

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
NOW = "2026-05-01T10:00:00.000Z"
BASE = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
VM = "vm-fixture-1"


class Stamper:
    """Stamps through storage's own rule (proxy/storage/_dag_doc.py::stamp_doc):
    successive docs of one Stamper are successive writes of one row; built_at is
    a fixed base time + one second per call (deterministic). When nothing
    changed the previous doc is kept (no write)."""

    def __init__(self, notebook_id=1):
        self.prev = None
        self.calls = 0
        self.notebook_id = notebook_id

    def __call__(self, doc):
        self.calls += 1
        now = (BASE + timedelta(seconds=self.calls)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        row_version = self.prev["version"] if self.prev else 0
        d, _wrote, *_ = stamp_doc(self.prev, copy.deepcopy(doc), row_version, self.notebook_id, now)
        self.prev = d
        return copy.deepcopy(d)


def cell(cid, code, ctype="code", **kw):
    c = {"id": cid, "type": ctype, "code": code}
    c.update(kw)
    return c


def run(cid, code, *, ctype="code", ov=None, ok=True, reads=(), created=(), rebound=(), mutated=None,
        sources=()):
    """A RunCapture; code-cell `mutated` defaults to static mutated ∩ reads (the
    executor's in-place detection)."""
    if mutated is None:
        mutated = () if ctype == "sql" else tuple(sorted(set(analyze_cell("code", code)["mutated"]) & set(reads)))
    return RunCapture(cid, ctype, code, ov, ok, tuple(reads), tuple(created), tuple(rebound), tuple(mutated),
                      None if sources is None else tuple(sources), NOW)


def runtime(clock, vars_, runs=None):
    return {"clock": clock, "vars": copy.deepcopy(vars_), "runs": copy.deepcopy(runs or {})}


def opened(cells):
    return dag.apply_cells(None, cells, trigger="open", now=NOW)


def ran(doc, runs, rt=None, vm=VM):
    return dag.apply_runs(doc, runs, rt, vm, trigger="execute", now=NOW)


# ── depGraph: the "AWS Data Sources" mutation chain ──────────────────────────

MUTATOR_CODE = "\n".join([
    '# @param {"type": "slider", "min": 3, "max": 45, "default": 14, "label": "Stockout risk window (days)"}',
    "stockout_risk_days = 14",
    "eff = stockout_risk_days * season_multiplier",
    "products360['action'] = products360.apply(lambda r: 'RESTOCK', axis=1)",
    "products360['priority_score'] = products360['revenue'] * season_multiplier",
])

NB = [
    ("demand", "code", 'product_demand = read_athena("q")', [], ["product_demand"],
     [{"kind": "athena", "id": "microvm_demo_db.orders"}]),
    ("reco", "sql", "SELECT 1", [], ["product_reco"], [{"kind": "dynamodb", "id": "ecommerce-recommendations"}]),
    ("holiday", "code", "season_multiplier = 1.2", [], ["season_multiplier"],
     [{"kind": "url", "id": "https://date.nager.at"}]),
    ("builder", "code", "products360 = product_demand.merge(product_reco)\nproducts360['x'] = 0",
     ["product_demand", "product_reco"], ["products360"], []),
    ("mutator", "code", MUTATOR_CODE, ["products360", "season_multiplier"], ["stockout_risk_days", "eff"], []),
    ("r31", "code", "action_counts = products360['action'].value_counts()", ["products360"], ["action_counts"], []),
    ("r32", "code", "products360.head()", ["products360"], [], []),
    ("kbparams", "code", 'kb_query = "x"\nkb_top_k = 5', [], ["kb_query", "kb_top_k"], []),
    ("kb", "code", "chunks = read_bedrock_kb(kb_query, top_k=kb_top_k)", ["kb_query", "kb_top_k"], ["chunks"],
     [{"kind": "bedrock_kb", "id": "AVSDQZX2CR"}]),
    ("r42", "code", "in_kb = products360[products360['product_id'].isin([])]\nresult = ask_bedrock_kb('q')",
     ["products360"], ["in_kb", "result"], [{"kind": "bedrock_kb", "id": "AVSDQZX2CR"}]),
    ("unrelated", "code", "z = 5", [], ["z"], []),
]

PROVENANCE = {
    "product_demand": {"defined_by": "demand", "defined_at": 1, "modified_by": [], "last_cell": "demand",
                       "last_exec": 1},
    "product_reco": {"defined_by": "reco", "defined_at": 2, "modified_by": [], "last_cell": "reco", "last_exec": 2},
    "season_multiplier": {"defined_by": "holiday", "defined_at": 3, "modified_by": [], "last_cell": "holiday",
                          "last_exec": 3},
    "products360": {"defined_by": "builder", "defined_at": 4, "modified_by": ["mutator"], "last_cell": "mutator",
                    "last_exec": 5},
    "stockout_risk_days": {"defined_by": "mutator", "defined_at": 5, "modified_by": [], "last_cell": "mutator",
                           "last_exec": 5},
    "kb_query": {"defined_by": "kbparams", "defined_at": 6, "modified_by": [], "last_cell": "kbparams",
                 "last_exec": 6},
    "kb_top_k": {"defined_by": "kbparams", "defined_at": 6, "modified_by": [], "last_cell": "kbparams",
                 "last_exec": 6},
}


def nb_cells():
    out = []
    for cid, ctype, code, _r, w, _s in NB:
        c = cell(cid, code, ctype)
        if ctype == "sql":
            c["outputVariable"] = w[0]
        out.append(c)
    return out


def nb_run(cid):
    _cid, ctype, code, reads, writes, sources = next(x for x in NB if x[0] == cid)
    if ctype == "sql":
        return run(cid, code, ctype="sql", ov=writes[0], reads=reads, sources=sources)
    return run(cid, code, reads=reads, created=writes, sources=sources)


def gen_mutation_chain():
    st = Stamper()
    cells = nb_cells()
    scen = {}
    for name, prov in (("with_provenance", PROVENANCE), ("no_provenance", {})):
        d = ran(opened(cells), [nb_run(x[0]) for x in NB], runtime(11, prov))
        scen[name] = {"cells": cells, "doc": st(d)}
    full = ran(opened(cells), [nb_run(x[0]) for x in NB], runtime(11, PROVENANCE))
    # Changing a PY-10 slider (re-run of the mutator): products360 + stockout_risk_days move.
    prov = copy.deepcopy(PROVENANCE)
    prov["products360"]["last_exec"] = 12
    prov["stockout_risk_days"]["last_exec"] = 12
    scen["after_mutator"] = {"cells": cells, "baseline_clock": 11,
                             "doc": st(ran(full, [nb_run("mutator")], runtime(12, prov)))}
    # Re-running the builder: it owns products360 again.
    prov = copy.deepcopy(PROVENANCE)
    prov["products360"].update({"last_cell": "builder", "last_exec": 12})
    scen["after_builder"] = {"cells": cells, "baseline_clock": 11,
                             "doc": st(ran(full, [nb_run("builder")], runtime(12, prov)))}
    prov = copy.deepcopy(PROVENANCE)
    prov["season_multiplier"]["last_exec"] = 12
    scen["after_holiday"] = {"cells": cells, "baseline_clock": 11,
                             "doc": st(ran(full, [nb_run("holiday")], runtime(12, prov)))}
    return scen


def gen_two_mutators():
    cells = [cell("b", "df = make()"), cell("m1", "df['a'] = 1"), cell("mid", "n = len(df)"),
             cell("m2", 'df.sort_values("a", inplace=True)'), cell("end", "top = df.head()")]
    runs = [run("b", cells[0]["code"], created=("df",), sources=[]),
            run("m1", cells[1]["code"], reads=("df",), mutated=("df",), sources=[]),
            run("mid", cells[2]["code"], reads=("df",), created=("n",), sources=[]),
            run("m2", cells[3]["code"], reads=("df",), mutated=("df",), sources=[]),
            run("end", cells[4]["code"], reads=("df",), created=("top",), sources=[])]
    rt = runtime(5, {"df": {"defined_by": "b", "defined_at": 1, "modified_by": ["m1", "m2"], "last_cell": "m2",
                            "last_exec": 4},
                     "n": {"defined_by": "mid", "defined_at": 3, "modified_by": [], "last_cell": "mid", "last_exec": 3},
                     "top": {"defined_by": "end", "defined_at": 5, "modified_by": [], "last_cell": "end",
                             "last_exec": 5}})
    return {"ran": {"cells": cells, "doc": Stamper()(ran(opened(cells), runs, rt))}}


def gen_out_of_order():
    st = Stamper()
    cells = [cell("r", "y = x"), cell("w", "x = 1")]
    runs = [run("r", "y = x", reads=("x",), created=("y",), sources=[]),
            run("w", "x = 1", created=("x",), sources=[])]
    rt = runtime(2, {"x": {"defined_by": "w", "defined_at": 2, "modified_by": [], "last_cell": "w", "last_exec": 2},
                     "y": {"defined_by": "r", "defined_at": 1, "modified_by": [], "last_cell": "r", "last_exec": 1}})
    self_cells = [cell("a", "x = x")]
    self_runs = [run("a", "x = x", reads=("x",), created=("x",), sources=[])]
    return {
        "ran": {"cells": cells, "doc": st(ran(opened(cells), runs, rt))},
        "self_read": {"cells": self_cells, "doc": st(ran(opened(self_cells), self_runs, runtime(1, {})))},
    }


def gen_cycle():
    cells = [cell("a", "y = x + 1"), cell("b", "x = y + 1")]
    runs = [run("a", "y = x + 1", reads=("x",), created=("y",), sources=[]),
            run("b", "x = y + 1", reads=("y",), created=("x",), sources=[])]
    rt = runtime(2, {"x": {"defined_by": "b", "defined_at": 2, "modified_by": [], "last_cell": "b", "last_exec": 2},
                     "y": {"defined_by": "a", "defined_at": 1, "modified_by": [], "last_cell": "a", "last_exec": 1}})
    return {"ran": {"cells": cells, "doc": Stamper()(ran(opened(cells), runs, rt))}}


# ── lineage: param → SQL → Python, plus an unrelated cell ────────────────────

PARAM_CODE = "\n".join([
    '# @param {"type":"dropdown","label":"Region","options":[{"label":"North America","value":"na"},'
    '{"label":"Europe","value":"eu"}]}',
    'region = "na"',
])
SQL_CODE = "-- Orders for the selected region\nSELECT * FROM microvm_demo_db.orders WHERE region = '{region}'"
PY_CODE = '# Revenue by month\nmonthly = orders_df.groupby("month").sum()\nmonthly'
ATHENA = [{"kind": "athena", "id": "microvm_demo_db.orders"}]


def lineage_cells():
    return [cell("1", PARAM_CODE), cell("2", SQL_CODE, "sql", outputVariable="orders_df"),
            cell("3", PY_CODE), cell("4", "# Notes", "markdown"), cell("5", "other = 1\nprint(other)")]


LINEAGE_VARS = {
    "region": {"defined_by": "1", "defined_at": 1, "modified_by": [], "last_cell": "1", "last_exec": 1},
    "orders_df": {"defined_by": "2", "defined_at": 2, "modified_by": [], "last_cell": "2", "last_exec": 2},
    "monthly": {"defined_by": "3", "defined_at": 3, "modified_by": [], "last_cell": "3", "last_exec": 3},
    "other": {"defined_by": "5", "defined_at": 4, "modified_by": [], "last_cell": "5", "last_exec": 4},
}


def lineage_runs(*, cell3_reads=("orders_df",), cell3_sources=(), skip=()):
    out = [run("1", PARAM_CODE, created=("region",), sources=[]),
           run("2", SQL_CODE, ctype="sql", ov="orders_df", reads=("region",), sources=ATHENA),
           run("3", PY_CODE, reads=cell3_reads, created=("monthly",), sources=cell3_sources),
           run("5", "other = 1\nprint(other)", created=("other",), sources=[])]
    return [r for r in out if r.cell_id not in skip]


def gen_lineage():
    st = Stamper()
    cells = lineage_cells()

    def build(runs, vars_=LINEAGE_VARS):
        return st(ran(opened(cells), runs, runtime(4, vars_)))

    vars_lookup = {**copy.deepcopy(LINEAGE_VARS),
                   "lookup": {"defined_by": "99", "defined_at": 0, "modified_by": [], "last_cell": "99",
                              "last_exec": 0}}
    vars_no2 = {k: v for k, v in LINEAGE_VARS.items() if k != "orders_df"}
    return {
        "base": {"cells": cells, "doc": build(lineage_runs())},
        "url_s3": {"cells": cells, "doc": build(lineage_runs(cell3_sources=[
            {"kind": "url", "id": "https://example.com/data.csv"}, {"kind": "s3", "id": "s3://other/x.csv"}]))},
        "url_host": {"cells": cells, "doc": build(lineage_runs(cell3_sources=[
            {"kind": "url", "id": "https://date.nager.at"}]))},
        "never_run_upstream": {"cells": cells, "doc": build(lineage_runs(skip=("2",)), vars_no2)},
        "missing_producer": {"cells": cells, "doc": build(lineage_runs(cell3_reads=("orders_df", "lookup")),
                                                          vars_lookup)},
        "sources_not_captured": {"cells": cells, "doc": build(lineage_runs(cell3_sources=None))},
    }


# ── statuses: finding 5 (edited until the autosave lands), transitive stale,
#    not_in_checkpoint ────────────────────────────────────────────────────────

def gen_status():
    st = Stamper()
    saved = [cell("c1", "x = 1"), cell("c2", "y = x + 1"), cell("c3", "z = y")]
    live_c2 = "y = x + 2"   # executed but not saved yet
    vars1 = {"x": {"defined_by": "c1", "defined_at": 1, "modified_by": [], "last_cell": "c1", "last_exec": 1},
             "y": {"defined_by": "c2", "defined_at": 2, "modified_by": [], "last_cell": "c2", "last_exec": 2},
             "z": {"defined_by": "c3", "defined_at": 3, "modified_by": [], "last_cell": "c3", "last_exec": 3}}
    first = ran(opened(saved), [run("c1", "x = 1", created=("x",), sources=[]),
                                run("c2", live_c2, reads=("x",), created=("y",), sources=[]),
                                run("c3", "z = y", reads=("y",), created=("z",), sources=[])], runtime(3, vars1))
    vars2 = copy.deepcopy(vars1)
    vars2["x"]["last_exec"] = 4
    again = ran(first, [run("c1", "x = 1", created=("x",), sources=[])], runtime(4, vars2))
    # Cold launch without restore: a new VM with an empty namespace.
    cold = dag.apply_runtime(first, runtime(0, {}), "vm-fixture-2", trigger="attach", empty_namespace=True)
    return {
        "edited_unsaved": {"cells": saved, "live_code": {"c2": live_c2}, "doc": st(first)},
        "edited_with_reason": {"cells": saved, "live_code": {"c2": live_c2}, "baseline_clock": 3,
                               "doc": st(again)},
        "not_in_checkpoint": {"cells": saved, "live_code": {"c2": live_c2}, "doc": st(cold)},
    }


# ── issues of every code ──────────────────────────────────────────────────────

def gen_issues():
    cells = [
        cell("i1", "df = load()"),                                  # url + s3 + local + athena sources
        cell("i2", "df['a'] = 1"),                                   # cross_cell_mutation
        cell("i3", "df = other()"),                                  # multiple_definitions
        cell("i4", "print(df, ghost)"),                              # missing_producer (ghost)
        cell("i5", 'exec("w = 1")'),                                 # dynamic_names
        cell("i6", "late_value = 2 * early"),                        # out_of_order (reads early)
        cell("i7", "early = 1"),
        cell("i8", "p = q + 1"),                                     # cycle with i9 (+ out_of_order)
        cell("i9", "q = p + 1"),
    ]
    sources = [{"kind": "url", "id": "https://example.com/feed.json"},
               {"kind": "s3", "id": "s3://bucket/raw/x.csv"},
               {"kind": "local", "id": "/tmp/x.csv"},
               {"kind": "athena", "id": "microvm_demo_db.orders"}]
    runs = [run("i1", cells[0]["code"], created=("df",), sources=sources),
            run("i2", cells[1]["code"], reads=("df",), mutated=("df",), sources=[]),
            run("i3", cells[2]["code"], created=("df",), sources=[]),
            run("i4", cells[3]["code"], reads=("df", "ghost"), sources=[]),
            run("i5", cells[4]["code"], created=("w",), sources=[]),
            run("i6", cells[5]["code"], reads=("early",), created=("late_value",), sources=[]),
            run("i7", cells[6]["code"], created=("early",), sources=[]),
            run("i8", cells[7]["code"], reads=("q",), created=("p",), sources=[]),
            run("i9", cells[8]["code"], reads=("p",), created=("q",), sources=[])]
    rt = runtime(9, {"early": {"defined_by": "i7", "defined_at": 7, "modified_by": [], "last_cell": "i7",
                               "last_exec": 7},
                     "q": {"defined_by": "i9", "defined_at": 9, "modified_by": [], "last_cell": "i9",
                           "last_exec": 9}})
    return {"every_code": {"cells": cells, "doc": Stamper()(ran(opened(cells), runs, rt))}}




# ── deltas: {prev, delta, next, graph_hash} pairs of real builder output, for
#    the browser's applyDagDelta / graphHash parity (dagDelta.test.mjs) ───────

def _delta_case(st, prev_doc, next_doc):
    prev = st(prev_doc)
    nxt = st(next_doc)
    return {"prev": prev, "delta": diff_docs(prev, nxt), "next": nxt, "graph_hash": nxt["graph_hash"]}


def _bump(vars_, clock, *names):
    v = copy.deepcopy(vars_)
    for n in names:
        v[n]["last_exec"] = clock
    return v


def gen_deltas():
    cells = lineage_cells()
    full = ran(opened(cells), lineage_runs(), runtime(4, LINEAGE_VARS))
    cases = {}
    leaf = ran(full, [run("5", "other = 1\nprint(other)", created=("other",), sources=[])],
               runtime(5, _bump(LINEAGE_VARS, 5, "other")))
    cases["leaf_rerun"] = _delta_case(Stamper(), full, leaf)
    up = ran(full, [run("1", PARAM_CODE, created=("region",), sources=[])],
             runtime(5, _bump(LINEAGE_VARS, 5, "region")))
    cases["upstream_rerun"] = _delta_case(Stamper(), full, up)
    edited = [dict(c) for c in cells]
    edited[2]["code"] = PY_CODE.replace("orders_df.groupby", "orders_df[orders_df.r == region].groupby")
    cases["edit_changes_reads"] = _delta_case(Stamper(), full,
                                              dag.apply_cells(full, edited, trigger="save", now=NOW))
    inserted = cells[:2] + [cell("2b", "extra = orders_df.shape")] + cells[2:]
    cases["node_insert"] = _delta_case(Stamper(), full, dag.apply_cells(full, inserted, trigger="save", now=NOW))
    deleted = [c for c in cells if c["id"] != "2"]
    cases["node_delete"] = _delta_case(Stamper(), full, dag.apply_cells(full, deleted, trigger="save", now=NOW))
    reordered = [cells[0], cells[4], cells[1], cells[2], cells[3]]
    cases["node_reorder"] = _delta_case(Stamper(), full,
                                        dag.apply_cells(full, reordered, trigger="save", now=NOW))
    cyc = [dict(c) for c in cells]
    cyc[0]["code"] = PARAM_CODE + "\nprint(monthly)"
    cases["cycle_introduced"] = _delta_case(Stamper(), full, dag.apply_cells(full, cyc, trigger="save", now=NOW))
    cases["runtime_only"] = _delta_case(Stamper(), full, dag.apply_runs(
        full, [], runtime(6, {**{k: v for k, v in LINEAGE_VARS.items() if k != "other"},
                              "tmp": {"defined_by": None, "defined_at": 6, "modified_by": [],
                                      "last_cell": None, "last_exec": 6}}), VM, trigger="runtime", now=NOW))
    pend = ran(full, [run("6", "late = monthly", reads=("monthly",), created=("late",), sources=[])],
               runtime(5, LINEAGE_VARS))
    st = Stamper()
    st(full)
    p_case = _delta_case(st, pend, dag.apply_cells(pend, cells + [cell("6", "late = monthly")],
                                                   trigger="save", now=NOW))
    cases["pending_adoption"] = p_case
    ua = [cell("u1", "données = 1"), cell("u2", "x = données + 1")]
    ua_vars = {"données": {"defined_by": "u1", "defined_at": 1, "modified_by": [], "last_cell": "u1",
                           "last_exec": 1}}
    ua_doc = ran(opened(ua), [run("u1", "données = 1", created=("données",),
                                  sources=[{"kind": "s3", "id": "s3://bucket/🚀/données.csv"}]),
                              run("u2", "x = données + 1", reads=("données",), created=("x",), sources=[])],
                 runtime(2, ua_vars))
    ua_next = ran(ua_doc, [run("u1", "données = 1", created=("données",),
                               sources=[{"kind": "s3", "id": "s3://bucket/🚀/données.csv"}])],
                  runtime(3, _bump(ua_vars, 3, "données")))
    cases["non_ascii"] = _delta_case(Stamper(), ua_doc, ua_next)
    cases["nothing_changed"] = _delta_case(Stamper(), full, copy.deepcopy(full))
    return cases


# ── hash parity: dag.json's `checkpoint` save stamp is never hashed (Python
#    dag_contract.graph_hash == JS dagDelta.graphHash, with and without it) ───

def gen_hash_parity():
    cells = lineage_cells()
    doc = Stamper()(ran(opened(cells), lineage_runs(), runtime(4, LINEAGE_VARS)))
    with_ck = {**copy.deepcopy(doc), "checkpoint": {
        "id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "saved_at": "2026-05-01T10:00:05.000000+00:00",
        "session_id": "f55c1bb9-0000-4000-8000-000000000000", "microvm_id": "vm-fixture"}}
    return {"doc_with_checkpoint": with_ck, "doc_without_checkpoint": doc, "graph_hash": doc["graph_hash"]}


# ── explain-this-code: diamond + many ancestors (static "open" documents) ────
def gen_explain():
    diamond = [cell("a", "x = 1"), cell("b", "y = x + 1"), cell("c", "z = x + 2"),
               cell("d", "w = y + z\nw")]
    # 30 chained code cells of ~1.2k chars each: the total is well over the 24k budget.
    pad = "# filler comment text filler comment text filler comment text\n"
    many = []
    for i in range(30):
        prev = f"v{i - 1}" if i else "0"
        many.append(cell(f"m{i}", f"v{i} = {prev} + 1\n" + pad * 20))
    many.append(cell("m30", "final = v29 + 1\nfinal"))
    return {"diamond": {"cells": diamond, "doc": Stamper()(opened(diamond))},
            "many_ancestors": {"cells": many, "doc": Stamper()(opened(many))}}
GROUPS = {
    "dag_mutation_chain.json": gen_mutation_chain,
    "dag_two_mutators.json": gen_two_mutators,
    "dag_out_of_order.json": gen_out_of_order,
    "dag_cycle.json": gen_cycle,
    "dag_lineage.json": gen_lineage,
    "dag_status.json": gen_status,
    "dag_issues.json": gen_issues,
    "dag_explain.json": gen_explain,
}


def main():
    path = os.path.join(OUT_DIR, "dag_deltas.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_generated_by": "tests/fixtures/gen_dag_fixtures.py (stamp_doc + dag_contract.diff_docs "
                                    "over proxy/notebook/dag.py builder output)",
                   "cases": gen_deltas()}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"wrote {os.path.relpath(path, ROOT)}")
    path = os.path.join(OUT_DIR, "dag_hash_parity.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_generated_by": "tests/fixtures/gen_dag_fixtures.py (stamp_doc over proxy/notebook/dag.py "
                                    "builder output + a dag.json checkpoint stamp)",
                   **gen_hash_parity()}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"wrote {os.path.relpath(path, ROOT)}")
    for fname, fn in GROUPS.items():
        payload = {"_generated_by": "tests/fixtures/gen_dag_fixtures.py (proxy/notebook/dag.py builder output)",
                   "scenarios": fn()}
        path = os.path.join(OUT_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=False)
            f.write("\n")
        print(f"wrote {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
