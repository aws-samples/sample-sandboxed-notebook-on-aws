#!/usr/bin/env python3
# test-kind: offline
"""
Unit tests for the pure notebook-DAG builder (proxy/notebook/dag.py, design §3).

Covers every §3 rule (edge rule, chains with edge kind/use, multiple
definitions, out_of_order, cycle, missing_producer, edited fallback, SQL output
variable rule, pending adoption + expiry, lastExecutedCode seeding, levels,
statuses incl. transitive stale and not_in_checkpoint, every issue code,
`x += 1` static/runtime parity, reconcile matrix, cold attach, attach_seq
rules, determinism, in_sync) plus a Python port of every scenario in
web/src/services/__tests__/depGraph.test.mjs, built as runtime captures.

No I/O, no DB, no AWS. Run:
    python3 tests/dag/test_dag_builder.py
"""
import copy
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app.notebook.dag_contract import cell_code_hash  # noqa: E402
from app.notebook.static_analysis import analyze_cell  # noqa: E402
from proxy.notebook import dag  # noqa: E402
from proxy.notebook.dag import RunCapture  # noqa: E402

# ── tiny test harness (same style as tests/storage/test_cell_runtime_fields.py)
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


NOW = "2026-05-01T10:00:00.000Z"
T0 = "2026-05-01T09:59:00.000Z"


# ── helpers ───────────────────────────────────────────────────────────────────

def cell(cid, code, ctype="code", **kw):
    c = {"id": cid, "type": ctype, "code": code}
    c.update(kw)
    return c


def sim_run(c, *, ok=True, reads=None, created=None, rebound=(), mutated=None, sources=None):
    """A RunCapture for cell dict `c`. Defaults simulate the executor from the
    static facts: reads = static reads, mutated = static mutated, created =
    static writes - mutated."""
    ctype = c.get("type") or "code"
    a = analyze_cell(ctype, c["code"], c.get("outputVariable") or "result") if ctype == "sql" \
        else analyze_cell(ctype, c["code"])
    if reads is None:
        reads = a["reads"]
    if mutated is None:
        mutated = sorted(set(a["mutated"]) & set(reads))
    if created is None:
        created = [] if ctype == "sql" else sorted(set(a["writes"]) - set(mutated))
    return RunCapture(cell_id=c["id"], cell_type=ctype, code=c["code"],
                      output_variable=c.get("outputVariable"), ok=ok, reads=tuple(reads),
                      created=tuple(created), rebound=tuple(rebound), mutated=tuple(mutated),
                      sources=tuple(sources) if sources is not None else None, executed_at=NOW)


def build(cells, runs=None, runtime=None, vm_id="vm-1"):
    d = dag.apply_cells(None, cells, trigger="open", now=NOW)
    if runs:
        d = dag.apply_runs(d, runs, runtime, vm_id, trigger="execute", now=NOW)
    return d


def node(d, cid):
    return next(n for n in d["nodes"] if n["cell_id"] == cid)


def edges(d, to=None, var=None):
    return [e for e in d["edges"] if (to is None or e["to"] == to) and (var is None or e["var"] == var)]


def source_of(d, to, var):
    es = edges(d, to, var)
    return es[0]["from"] if len(es) == 1 else (None if not es else [e["from"] for e in es])


def issues(d, code=None, cid=None):
    return [i for i in d["issues"] if (code is None or i["code"] == code)
            and (cid is None or i["cell_id"] == cid)]


def downstream(d, starts):
    out = {}
    for e in d["edges"]:
        out.setdefault(e["from"], set()).add(e["to"])
    seen, queue = set(), list(starts)
    while queue:
        cur = queue.pop()
        for n in out.get(cur, ()):
            if n not in seen and n not in starts:
                seen.add(n)
                queue.append(n)
    return seen


def upstream(d, start):
    inn = {}
    for e in d["edges"]:
        inn.setdefault(e["to"], set()).add(e["from"])
    seen, queue = set(), [start]
    while queue:
        cur = queue.pop()
        for p in inn.get(cur, ()):
            if p not in seen and p != start:
                seen.add(p)
                queue.append(p)
    return seen


def stale_ids(d):
    return sorted(n["cell_id"] for n in d["nodes"] if n["stale_reason"] is not None)


# ── §3.4 edge rule, chains, kinds/use ─────────────────────────────────────────

def t_edge_rule_and_chain():
    print("\n§3.4 edge rule: builder → mutator → mutator → reader")
    cells = [cell("b", "df = make()"), cell("m1", "df['a'] = 1"), cell("mid", "n = len(df)"),
             cell("m2", 'df.sort_values("a", inplace=True)'), cell("end", "top = df.head()")]
    for label, d in (("static", build(cells)), ("runtime", build(cells, [sim_run(c) for c in cells]))):
        check(f"[{label}] m1 reads df from b (defines/mutate)",
              [(e["from"], e["kind"], e["use"]) for e in edges(d, "m1", "df")] == [("b", "defines", "mutate")])
        check(f"[{label}] reader between two mutators reads the earlier version (m1)",
              [(e["from"], e["kind"], e["use"]) for e in edges(d, "mid", "df")] == [("m1", "mutates", "read")])
        check(f"[{label}] m2 ← m1 mutates/mutate",
              [(e["from"], e["kind"], e["use"]) for e in edges(d, "m2", "df")] == [("m1", "mutates", "mutate")])
        check(f"[{label}] end ← m2 mutates/read",
              [(e["from"], e["kind"], e["use"]) for e in edges(d, "end", "df")] == [("m2", "mutates", "read")])
        check(f"[{label}] no backward edges", not any(e["backward"] for e in d["edges"]))
        check(f"[{label}] edge origin", all(e["origin"] == label for e in d["edges"]))
        check(f"[{label}] cross_cell_mutation on m1 (related b) and m2 (related m1)",
              [(i["cell_id"], i["related"]) for i in issues(d, "cross_cell_mutation")]
              == [("m1", ["b"]), ("m2", ["m1"])])
        check(f"[{label}] levels 0,1,2,2,3",
              [node(d, c)["level"] for c in ("b", "m1", "mid", "m2", "end")] == [0, 1, 2, 2, 3])
        check(f"[{label}] no multiple_definitions", not issues(d, "multiple_definitions"))


def t_no_self_edge():
    print("\n§3.4 no self-edge")
    d = build([cell("a", "x = x + 1")])
    check("static: a reads+writes x, no edge", d["edges"] == [])
    d = build([cell("a", "x = 1")], [RunCapture("a", "code", "x = x + 1", None, True, ("x",), (), ("x",), (),
                                                None, NOW)])
    check("runtime: no self edge", d["edges"] == [])
    check("runtime: missing_producer for x", [i["var"] for i in issues(d, "missing_producer", "a")] == ["x"])


def t_multiple_definitions():
    print("\n§3.6 multiple_definitions")
    d = build([cell("c1", "x = 1"), cell("c2", "x = 2"), cell("c3", "x = 3"), cell("c4", "print(x)")])
    md = issues(d, "multiple_definitions")
    check("raised on every definer after the first",
          [(i["cell_id"], i["related"], i["severity"]) for i in md]
          == [("c2", ["c1"], "info"), ("c3", ["c1", "c2"], "info")])
    check("c4 reads the nearest earlier definer", source_of(d, "c4", "x") == "c3")
    check("detail is full text", md[1]["detail"] == "redefines x, also defined in cells c1, c2")
    d = build([cell("c1", "x = 1"), cell("c2", "x = 2")])
    check("no reader → no multiple_definitions", not issues(d, "multiple_definitions"))


def t_out_of_order():
    print("\n§3.4 out_of_order fallback")
    d = build([cell("r", "y = x"), cell("w", "x = 1")])
    check("backward edge w → r", [(e["from"], e["to"], e["var"], e["backward"]) for e in d["edges"]]
          == [("w", "r", "x", True)])
    ooo = issues(d, "out_of_order", "r")
    check("out_of_order issue (amber, related [w])",
          [(i["var"], i["related"], i["severity"]) for i in ooo] == [("x", ["w"], "amber")])
    check("backward edges ignored by levels", node(d, "r")["level"] == 0)
    # The runtime owner (provenance last_cell) wins over the notebook-order fallback.
    cells = [cell("r", "y = x"), cell("w1", "x = 1"), cell("w2", "x = 2")]
    rt = {"clock": 3, "vars": {"x": {"defined_by": "w1", "modified_by": [], "last_cell": "w1", "last_exec": 3}},
          "runs": {}}
    d = build(cells, [sim_run(cells[1])], rt)
    check("fallback owner = runtime.vars[x].last_cell", source_of(d, "r", "x") == "w1")
    d = build(cells)
    check("no runtime → last producer in notebook order", source_of(d, "r", "x") == "w2")


def t_cycle():
    print("\n§3.5 cycle")
    d = build([cell("c1", "a = b + 1"), cell("c2", "b = a + 1"), cell("c3", "z = 1")])
    cyc = issues(d, "cycle")
    check("cycle issue on both members", [(i["cell_id"], i["related"]) for i in cyc]
          == [("c1", ["c2"]), ("c2", ["c1"])])
    check("cycle severity amber", all(i["severity"] == "amber" for i in cyc))
    check("unrelated cell not in cycle", not issues(d, "cycle", "c3"))


def t_missing_producer():
    print("\n§3.4 missing_producer (runtime reads only)")
    c = cell("a", "print(undefined_x)")
    d = build([c])
    check("static build: no missing_producer", not issues(d, "missing_producer"))
    d = build([c], [sim_run(c, reads=["undefined_x"])])
    mp = issues(d, "missing_producer", "a")
    check("runtime build: missing_producer amber for undefined_x",
          [(i["var"], i["severity"]) for i in mp] == [("undefined_x", "amber")])


def t_edited_fallback():
    print("\n§3.3 edited fallback to static")
    c1 = cell("a", "x = 1")
    d = build([c1], [sim_run(c1)])
    check("after run: runtime facts, current", node(d, "a")["facts_origin"] == "runtime"
          and node(d, "a")["status"] == "current")
    d2 = dag.apply_cells(d, [cell("a", "x = 2\ny = 3")], trigger="save", now=NOW)
    n = node(d2, "a")
    check("edited code → static facts of new code", n["facts_origin"] == "static" and n["writes"] == ["x", "y"])
    check("run bookkeeping kept, status edited",
          n["status"] == "edited" and n["last_run_seq"] == 1 and n["run_hash"] == cell_code_hash("code", "x = 1"))
    d3 = dag.apply_cells(d2, [cell("a", "x = 1")], trigger="save", now=NOW)
    check("revert to run code → current again (static facts)", node(d3, "a")["status"] == "current")
    d4 = dag.apply_cells(d, [cell("a", "x = 1")], trigger="save", now=NOW)
    check("unchanged code keeps runtime facts", node(d4, "a")["facts_origin"] == "runtime")
    check("apply_cells did not mutate prev", node(d, "a")["facts_origin"] == "runtime"
          and node(d, "a")["status"] == "current")


def t_sql_output_variable_rule():
    print("\n§3.3 step 2 SQL output variable rule")
    s = cell("s", "SELECT 1", "sql", outputVariable="a")
    d = build([s], [sim_run(s)])
    n = node(d, "s")
    check("SQL run writes [output_variable], runtime, current",
          n["writes"] == ["a"] and n["facts_origin"] == "runtime" and n["status"] == "current"
          and n["run_output_variable"] == "a" and n["mutates"] == [])
    d2 = dag.apply_cells(d, [cell("s", "SELECT 1", "sql", outputVariable="b")], trigger="save", now=NOW)
    n2 = node(d2, "s")
    check("explicit outputVariable changed → static facts for b",
          n2["facts_origin"] == "static" and n2["writes"] == ["b"])
    check("bookkeeping kept + status edited",
          n2["status"] == "edited" and n2["run_output_variable"] == "a" and n2["last_run_seq"] == 1)
    d3 = dag.apply_cells(d, [cell("s", "  SELECT 1\n", "sql", outputVariable="a")], trigger="save", now=NOW)
    check("same explicit variable + whitespace-only SQL change keeps runtime facts",
          node(d3, "s")["facts_origin"] == "runtime" and node(d3, "s")["status"] == "current")
    d4 = build([cell("s", "SELECT * FROM dynamodb.\"orders\"", "sql")])
    check("derived output variable", node(d4, "s")["output_variable"] == "orders"
          and node(d4, "s")["output_variable_explicit"] is False and node(d4, "s")["writes"] == ["orders"])


def t_pending():
    print("\n§3.3 step 4 pending adoption + expiry")
    base = build([cell("a", "x = 1")])
    s = cell("c9", "SELECT * FROM x", "sql", outputVariable="q")
    d = dag.apply_runs(base, [sim_run(s, reads=["x"])], None, "vm-1", trigger="execute", now=T0)
    p = d["pending"].get("c9")
    check("unknown cell → pending entry with run_seq + output_variable",
          p is not None and p["run_seq"] == 1 and p["output_variable"] == "q" and p["captured_at"] == T0)
    check("pending stores no code", "code" not in json.dumps(p).replace('"code_hash"', ""))
    d2 = dag.apply_cells(d, [cell("a", "x = 1"), s], trigger="save", now=NOW)
    n = node(d2, "c9")
    check("adopted: last_run_seq == pending.run_seq, run_output_variable",
          n["last_run_seq"] == 1 and n["last_ok_seq"] == 1 and n["run_output_variable"] == "q"
          and n["facts_origin"] == "runtime" and n["status"] == "current")
    check("adopted entry deleted", "c9" not in d2["pending"])
    check("adopted node has edge a → c9", source_of(d2, "c9", "x") == "a")
    d3 = dag.apply_cells(d, [cell("a", "x = 1"), cell("c9", "SELECT 2", "sql", outputVariable="q")],
                         trigger="save", now=NOW)
    check("hash mismatch → not adopted (never_run), entry kept",
          node(d3, "c9")["status"] == "never_run" and "c9" in d3["pending"])
    keep = dag.apply_cells(d, [cell("a", "x = 1")], trigger="save", now="2026-05-01T09:59:59.000Z")
    check("absent cell, 59 s old → kept", "c9" in keep["pending"])
    gone = dag.apply_cells(d, [cell("a", "x = 1")], trigger="save", now="2026-05-01T10:01:01.000Z")
    check("absent cell, > 120 s old → dropped", "c9" not in gone["pending"])
    check("run_seq advanced at capture", d["run_seq"] == 1 and d3["run_seq"] == 1)


def t_seeding():
    print("\n§3.3 step 6 lastExecutedCode seeding")
    d = build([cell("a", "x = 1", lastExecutedCode="x = 1", executedAt=T0),
               cell("b", "y = x", lastExecutedCode="y = x + 1"),
               cell("c", "z = 1", lastExecutedCode="z = 1", error="boom"),
               cell("d", "w = 1")])
    a = node(d, "a")
    check("seeded: run_hash, last_run_seq = last_ok_seq = 0, current",
          a["run_hash"] == a["code_hash"] and a["last_run_seq"] == 0 and a["last_ok_seq"] == 0
          and a["status"] == "current" and a["executed_at"] == T0)
    check("seeded with different code → edited", node(d, "b")["status"] == "edited")
    check("seeded with error → error", node(d, "c")["status"] == "error" and node(d, "c")["last_run_ok"] is False)
    check("no history → never_run", node(d, "d")["status"] == "never_run")
    d2 = dag.apply_runs(d, [sim_run(cell("a", "x = 1"))], None, "vm-1", trigger="execute", now=NOW)
    check("a real run outranks seq 0 → downstream gets stale_reason",
          node(d2, "b")["stale_reason"] == {"kind": "upstream_changed", "vars": ["x"], "via": ["a"]}
          and node(d2, "b")["status"] == "edited")


def t_statuses():
    print("\n§3.5 statuses + staleness")
    cells = [cell("c1", "x = 1"), cell("c2", "y = x"), cell("c3", "z = y"), cell("c4", "q = 1")]
    d = build(cells, [sim_run(c) for c in cells])
    check("all current after a full run", all(n["status"] == "current" for n in d["nodes"]))
    d2 = dag.apply_runs(d, [sim_run(cells[0])], None, "vm-1", trigger="execute", now=NOW)
    check("direct: c2 upstream_changed via c1",
          node(d2, "c2")["status"] == "stale"
          and node(d2, "c2")["stale_reason"] == {"kind": "upstream_changed", "vars": ["x"], "via": ["c1"]})
    check("transitive: c3 upstream_stale via c2",
          node(d2, "c3")["status"] == "stale"
          and node(d2, "c3")["stale_reason"] == {"kind": "upstream_stale", "vars": [], "via": ["c2"]})
    check("unrelated c4 and re-run c1 current",
          node(d2, "c4")["status"] == "current" and node(d2, "c1")["status"] == "current")
    # Edited downstream node keeps 'edited' but receives the stale reason.
    d3 = dag.apply_cells(d2, [cells[0], cell("c2", "y = x * 2"), cells[2], cells[3]], trigger="save", now=NOW)
    check("edited node keeps status edited + gets stale_reason",
          node(d3, "c2")["status"] == "edited" and node(d3, "c2")["stale_reason"]["kind"] == "upstream_changed")
    check("its downstream still stale", node(d3, "c3")["status"] == "stale")
    # A failed upstream run does not mark downstream stale.
    d4 = dag.apply_runs(d, [sim_run(cells[0], ok=False)], None, "vm-1", trigger="execute", now=NOW)
    check("failed run → error, downstream stays current",
          node(d4, "c1")["status"] == "error" and node(d4, "c2")["status"] == "current")
    check("failed run → static facts, last_ok_seq unchanged",
          node(d4, "c1")["facts_origin"] == "static" and node(d4, "c1")["last_ok_seq"] == 1
          and node(d4, "c1")["last_run_seq"] == 5)
    # A producer that never succeeded never marks downstream stale.
    d5 = build(cells, [sim_run(cells[1]), sim_run(cells[0], ok=False)])
    check("never-succeeded producer does not stale its reader", node(d5, "c2")["status"] == "current")
    # Edited / never-run upstream does not stale downstream.
    d6 = dag.apply_cells(d, [cell("c1", "x = 5")] + cells[1:], trigger="save", now=NOW)
    check("edited upstream does not stale downstream",
          node(d6, "c1")["status"] == "edited" and node(d6, "c2")["status"] == "current")
    # not_in_checkpoint via reconcile (no runtime restored) → stale + transitive.
    d7 = dag.reconcile_restored(d, {"origin": "empty", "runtime": {"clock": 0, "vars": {}, "runs": {}},
                                    "runtime_source": None, "consistent": False}, "vm-2", now=NOW)
    check("not_in_checkpoint → status stale, reason not_in_checkpoint",
          node(d7, "c1")["status"] == "stale"
          and node(d7, "c1")["stale_reason"] == {"kind": "not_in_checkpoint", "vars": [], "via": []})
    check("precedence: not_in_checkpoint beats upstream reasons",
          node(d7, "c2")["stale_reason"]["kind"] == "not_in_checkpoint")


def t_issue_codes():
    print("\n§3.6 issue codes")
    cells = [cell("u", "html = fetch()"), cell("s3", "obj = load()"), cell("ath", "t = q()"),
             cell("dyn", "exec('k = 1')\ng = globals()"), cell("m", "obj['a'] = 1")]
    runs = [sim_run(cells[0], sources=[{"kind": "url", "id": "https://example.com/" + "p" * 300}]),
            sim_run(cells[1], sources=[{"kind": "s3", "id": "s3://b/k"}, {"kind": "s3", "id": "s3://b/k"},
                                       {"kind": "local", "id": "/tmp/x.csv"}]),
            sim_run(cells[2], sources=[{"kind": "athena", "id": "db.t"}, {"kind": "dynamodb", "id": "tbl"}]),
            sim_run(cells[3]), sim_run(cells[4])]
    d = build(cells, runs)
    ext = issues(d, "external_state")
    check("external_state only for url/s3/local, one per (node, source)",
          [(i["cell_id"], i["source"]["kind"]) for i in ext] == [("u", "url"), ("s3", "local"), ("s3", "s3")])
    check("no external_state for athena/dynamodb", not issues(d, "external_state", "ath"))
    check("athena sources kept on node", [s["kind"] for s in node(d, "ath")["sources"]] == ["athena", "dynamodb"])
    long_id = "https://example.com/" + "p" * 300
    check("external_state detail full text (no truncation)",
          ext[0]["detail"] == f"reads url {long_id}; the cell won't re-run when the source changes"
          and ext[0]["source"] == {"kind": "url", "id": long_id} and ext[0]["severity"] == "info")
    dn = issues(d, "dynamic_names", "dyn")
    check("dynamic_names amber, lists constructs",
          len(dn) == 1 and dn[0]["severity"] == "amber" and "exec()" in dn[0]["detail"]
          and "globals()" in dn[0]["detail"])
    check("cross_cell_mutation info on m", [(i["severity"], i["related"]) for i in issues(d, "cross_cell_mutation")]
          == [("info", ["s3"])])
    # Issue ordering: (position, code, var, source id).
    keys = [(node(d, i["cell_id"])["position"], i["code"], i["var"] or "", (i["source"] or {}).get("id", ""))
            for i in d["issues"]]
    check("issues sorted", keys == sorted(keys))


def t_augassign_parity():
    print("\n§3.2 x += 1 static vs runtime parity")
    cells = [cell("c1", "x = 0"), cell("c2", "x += 1"), cell("c3", "print(x)")]
    runs = [RunCapture("c1", "code", "x = 0", None, True, (), ("x",), (), (), None, NOW),
            RunCapture("c2", "code", "x += 1", None, True, ("x",), (), ("x",), (), None, NOW),
            RunCapture("c3", "code", "print(x)", None, True, ("x",), (), (), (), None, NOW)]
    for label, d in (("static", build(cells)), ("runtime", build(cells, runs))):
        check(f"[{label}] c1→c2 defines/mutate",
              [(e["from"], e["kind"], e["use"]) for e in edges(d, "c2", "x")] == [("c1", "defines", "mutate")])
        check(f"[{label}] c2→c3 mutates/read",
              [(e["from"], e["kind"], e["use"]) for e in edges(d, "c3", "x")] == [("c2", "mutates", "read")])
        check(f"[{label}] no multiple_definitions", not issues(d, "multiple_definitions"))


def t_reconcile_matrix():
    print("\n§3.7 reconcile matrix")
    cells = [cell("a", "x = 1"), cell("p", "print(1)")]
    d = build(cells, [sim_run(c) for c in cells])
    h = node(d, "a")["run_hash"]
    vars_a = {"x": {"defined_by": "a", "modified_by": [], "last_cell": "a", "last_exec": 1}}

    def rec(source, consistent, runs, vars_):
        r = dag.reconcile_restored(d, {"origin": "restored", "runtime": {"clock": 5, "vars": vars_, "runs": runs},
                                       "runtime_source": source, "consistent": consistent}, "vm-2", now=NOW)
        return node(r, "a")["not_in_checkpoint"], node(r, "p")["not_in_checkpoint"], r

    nic, _, r = rec("dag.json", True, {"a": {"code_hash": h, "clock": 1, "ok": True}}, {})
    check("consistent + matching runs entry → in", nic is False)
    check("runtime taken from report (vm_id stamped)", r["runtime"]["clock"] == 5 and r["runtime"]["vm_id"] == "vm-2"
          and r["trigger"] == "reconcile")
    nic, _, _ = rec("dag.json", True, {"a": {"code_hash": "0" * 64, "clock": 1, "ok": True}}, vars_a)
    check("consistent + mismatched entry → not in (even if provenance shows it)", nic is True)
    nic, _, _ = rec("dag.json", True, {}, vars_a)
    check("consistent + no entry + provenance shows the write → in", nic is False)
    nic, _, _ = rec("dag.json", True, {}, {})
    check("consistent + no entry + no provenance → not in", nic is True)
    nic, _, _ = rec("dag.json", False, {"a": {"code_hash": h}}, {})
    check("inconsistent → provenance rule (ledger ignored)", nic is True)
    nic, _, _ = rec("dag.json", False, {}, {"x": {"defined_by": "z", "modified_by": ["a"], "last_cell": "z"}})
    check("inconsistent + modified_by → in", nic is False)
    nic, _, _ = rec("provenance.json", True, {}, vars_a)
    check("provenance.json → provenance rule (in)", nic is False)
    nic, _, _ = rec("provenance.json", True, {"a": {"code_hash": h}}, {})
    check("provenance.json → provenance rule (not in)", nic is True)
    nic, pnic, _ = rec(None, False, {}, {})
    check("no runtime source → writers not in, write-nothing nodes in", nic is True and pnic is False)


def t_cold_attach():
    print("\n§3.7 cold attach")
    cells = [cell("a", "x = 1"), cell("p", "print(1)"), cell("n", "y = 2")]
    rt1 = {"clock": 2, "vars": {}, "runs": {}}
    d = build(cells, [sim_run(cells[0]), sim_run(cells[1])], rt1, "vm-1")
    empty = {"origin": "empty", "runtime": {"clock": 0, "vars": {}, "runs": {}}}
    check("is_cold_attach: origin empty, clock 0, new vm", dag.is_cold_attach(d, empty, "vm-2") is True)
    check("is_cold_attach: origin pushed → False",
          dag.is_cold_attach(d, {**empty, "origin": "pushed"}, "vm-2") is False)
    check("is_cold_attach: clock > 0 → False",
          dag.is_cold_attach(d, {"origin": "empty", "runtime": {"clock": 3}}, "vm-2") is False)
    check("is_cold_attach: same vm → False", dag.is_cold_attach(d, empty, "vm-1") is False)
    c = dag.apply_runtime(d, empty["runtime"], "vm-2", trigger="attach", empty_namespace=True)
    check("cold attach marks run writers not_in_checkpoint",
          node(c, "a")["not_in_checkpoint"] is True and node(c, "a")["status"] == "stale")
    check("write-nothing and never-run nodes not marked",
          node(c, "p")["not_in_checkpoint"] is False and node(c, "n")["not_in_checkpoint"] is False)
    c2 = dag.apply_runtime(d, {"clock": 0, "vars": {}, "runs": {}}, "vm-2", trigger="attach")
    check("empty_namespace=False → nothing marked", node(c2, "a")["not_in_checkpoint"] is False)
    check("runtime replaced with vm_id", c2["runtime"]["vm_id"] == "vm-2" and c2["trigger"] == "attach")


def t_nit2():
    print("\nattach vs capture")
    cells = [cell("a", "x = 1"), cell("b", "y = 2")]
    rt = {"clock": 7, "vars": {"x": {"defined_by": "a", "modified_by": [], "last_cell": "a"}}, "runs": {}}
    d = build(cells, [sim_run(cells[0])], rt, "vm-1")
    attach_seq = d["run_seq"]
    # A capture lands between the GET /dag and the RMW: b runs, clock 8.
    rt2 = {"clock": 8, "vars": {**rt["vars"], "y": {"defined_by": "b", "modified_by": [], "last_cell": "b"}},
           "runs": {}}
    d_mid = dag.apply_runs(d, [sim_run(cells[1])], rt2, "vm-1", trigger="execute", now=NOW)
    old_report = {"origin": "pushed", "runtime": {"clock": 7, "vars": rt["vars"], "runs": {}},
                  "runtime_source": "dag.json", "consistent": True}
    r = dag.apply_runtime(d_mid, old_report["runtime"], "vm-1", trigger="attach")
    check("apply_runtime keeps prev.runtime (same vm, higher clock)",
          r["runtime"]["clock"] == 8 and "y" in r["runtime"]["vars"])
    r = dag.reconcile_restored(d_mid, old_report, "vm-1", now=NOW, attach_seq=attach_seq)
    check("reconcile keeps prev.runtime (same vm, higher clock)", r["runtime"]["clock"] == 8)
    r_other = dag.apply_runtime(d_mid, old_report["runtime"], "vm-9", trigger="attach")
    check("different vm → report runtime taken", r_other["runtime"]["clock"] == 7)
    # Node that ran after attach_seq is never marked not_in_checkpoint.
    bad = {"origin": "restored", "runtime": {"clock": 1, "vars": {}, "runs": {}},
           "runtime_source": "dag.json", "consistent": True}
    r = dag.reconcile_restored(d_mid, bad, "vm-2", now=NOW, attach_seq=attach_seq)
    check("last_run_seq > attach_seq → not marked", node(r, "b")["not_in_checkpoint"] is False)
    check("last_run_seq <= attach_seq → marked", node(r, "a")["not_in_checkpoint"] is True)
    r = dag.apply_runtime(d_mid, {"clock": 0, "vars": {}, "runs": {}}, "vm-2", trigger="attach",
                          empty_namespace=True, attach_seq=attach_seq)
    check("cold attach respects attach_seq",
          node(r, "a")["not_in_checkpoint"] is True and node(r, "b")["not_in_checkpoint"] is False)
    # Cold-attach condition evaluated on prev: a capture already stamped vm-2.
    d_vm2 = dag.apply_runs(d, [sim_run(cells[1])], {"clock": 1, "vars": {}, "runs": {}}, "vm-2",
                           trigger="execute", now=NOW)
    r = dag.apply_runtime(d_vm2, {"clock": 0, "vars": {}, "runs": {}}, "vm-2", trigger="attach",
                          empty_namespace=True)
    check("cold attach is a no-op when prev.runtime.vm_id already == vm_id",
          not any(n["not_in_checkpoint"] for n in r["nodes"]))


def t_determinism_and_helpers():
    print("\n§3.8 determinism, in_sync, normalize_sources, strip_for_vm")
    cells = [cell("m0", "# md", "markdown"), cell("b", "df = make()"), cell("m", "df['a'] = 1"),
             cell("s", "SELECT * FROM df", "sql", outputVariable="q"), cell("r", "print(q, df)")]
    runs = [sim_run(c, sources=[{"kind": "url", "id": "u"}]) for c in cells[1:]]
    a = build(cells, runs, {"clock": 3, "vars": {}, "runs": {}})
    b = build(copy.deepcopy(cells), list(runs), {"clock": 3, "vars": {}, "runs": {}})

    def dump(x):
        x = dict(x)
        x.pop("built_at", None)
        return json.dumps(x, ensure_ascii=False, separators=(",", ":"))

    check("byte-identical JSON for identical inputs", dump(a) == dump(b))
    check("positions are indexes in the FULL list (markdown skipped)",
          [(n["cell_id"], n["position"]) for n in a["nodes"]] == [("b", 1), ("m", 2), ("s", 3), ("r", 4)])
    ek = [(node(a, e["to"])["position"], e["var"], node(a, e["from"])["position"]) for e in a["edges"]]
    check("edges sorted (to pos, var, from pos)", ek == sorted(ek))
    check("node lists sorted + unique", all(n[k] == sorted(set(n[k])) for n in a["nodes"]
                                            for k in ("reads", "writes", "mutates", "dynamic")))
    check("in_sync true for the same cells", dag.in_sync(a, cells))
    check("in_sync false after code edit",
          not dag.in_sync(a, cells[:2] + [cell("m", "df['a'] = 2")] + cells[3:]))
    check("in_sync false after reorder", not dag.in_sync(a, [cells[0], cells[2], cells[1]] + cells[3:]))
    check("in_sync false after markdown insert (positions shift)",
          not dag.in_sync(a, [cell("m1", "x", "markdown")] + cells))
    check("in_sync false for explicit outputVariable change",
          not dag.in_sync(a, cells[:3] + [cell("s", "SELECT * FROM df", "sql", outputVariable="z")] + cells[4:]))
    check("in_sync false for None / wrong schema", not dag.in_sync(None, cells)
          and not dag.in_sync({**a, "schema": 2}, cells))
    long_id = "s3://bucket/" + "k" * 5000
    ns = dag.normalize_sources([{"kind": "s3", "id": long_id}, {"kind": "s3", "id": long_id},
                                {"kind": "url"}, "junk", {"kind": "athena", "id": "t", "extra": 1}])
    check("normalize_sources dedupes, drops invalid, keeps order, no truncation",
          ns == [{"kind": "s3", "id": long_id}, {"kind": "athena", "id": "t"}])
    check("normalize_sources(None) is None", dag.normalize_sources(None) is None)
    a2 = dict(a)
    a2["pending"] = {"x": {}}
    sv = dag.strip_for_vm(a2)
    check("strip_for_vm drops runtime + pending", "runtime" not in sv and "pending" not in sv
          and sv["nodes"] == a["nodes"])
    e = dag.empty_doc()
    check("empty_doc shape", e == {"schema": 1, "version": 0, "built_at": None, "run_seq": 0, "nodes": [],
                                   "edges": [], "issues": [], "runtime": {"clock": 0, "vars": {}, "runs": {}},
                                   "pending": {}})
    for i in range(2100):
        dag._static_facts("code", f"v{i} = {i}", None)
    check("static LRU bounded at 2048", len(dag._static_cache) <= 2048)
    check("RunCapture.code_hash uses the contract hash",
          runs[2].code_hash == cell_code_hash("sql", "SELECT * FROM df"))
    check("SQL run: no mutates, writes = output variable", node(a, "s")["writes"] == ["q"]
          and node(a, "s")["mutates"] == [])


# ── Port of web/src/services/__tests__/depGraph.test.mjs ──────────────────────
# Built as runtime captures (what the new executor reports): reads = the stored
# varsRead, created = varsWritten, mutated = static mutated ∩ reads. Live
# provenance is the runtime section; it only affects out-of-order fallbacks.

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
    "product_demand": {"defined_by": "demand", "modified_by": [], "last_cell": "demand", "last_exec": 1},
    "product_reco": {"defined_by": "reco", "modified_by": [], "last_cell": "reco", "last_exec": 2},
    "season_multiplier": {"defined_by": "holiday", "modified_by": [], "last_cell": "holiday", "last_exec": 3},
    "products360": {"defined_by": "builder", "modified_by": ["mutator"], "last_cell": "mutator", "last_exec": 5},
    "stockout_risk_days": {"defined_by": "mutator", "modified_by": [], "last_cell": "mutator", "last_exec": 5},
    "kb_query": {"defined_by": "kbparams", "modified_by": [], "last_cell": "kbparams", "last_exec": 6},
    "kb_top_k": {"defined_by": "kbparams", "modified_by": [], "last_cell": "kbparams", "last_exec": 6},
}
READERS = {"r31", "r32", "r42"}


def nb_cells():
    out = []
    for cid, ctype, code, _r, w, _s in NB:
        c = cell(cid, code, ctype)
        if ctype == "sql":
            c["outputVariable"] = w[0]
        out.append(c)
    return out


def nb_run(cid):
    cid_, ctype, code, reads, writes, sources = next(x for x in NB if x[0] == cid)
    if ctype == "sql":
        return RunCapture(cid, "sql", code, writes[0], True, tuple(reads), (), (), (), tuple(sources), NOW)
    mutated = sorted(set(analyze_cell("code", code)["mutated"]) & set(reads))
    return RunCapture(cid, "code", code, None, True, tuple(reads), tuple(writes), (), tuple(mutated),
                      tuple(sources), NOW)


def nb_build(provenance):
    rt = {"clock": 11, "vars": copy.deepcopy(provenance), "runs": {}}
    return build(nb_cells(), [nb_run(x[0]) for x in NB], rt)


def t_depgraph_port():
    for label, prov in (("with live provenance", PROVENANCE), ("stored cells only (no provenance)", {})):
        print(f"\ndepGraph port: mutation chain ({label})")
        d = nb_build(prov)
        check("mutator mutates products360", node(d, "mutator")["mutates"] == ["products360"])
        check("mutator depends on the builder", source_of(d, "mutator", "products360") == "builder")
        check("readers read the mutated frame", all(source_of(d, r, "products360") == "mutator" for r in READERS))
        owners = [n["cell_id"] for n in d["nodes"] if "products360" in n["writes"]]
        check("owner of products360 (last producer) is mutator", owners[-1] == "mutator")
        check("no self-loops", not any(e["from"] == e["to"] for e in d["edges"]))
        check("no cycles", not issues(d, "cycle"))

        print(f"depGraph port: reactive downstream sets ({label})")
        check("downstream(mutator) == readers", downstream(d, ["mutator"]) == READERS)
        check("downstream(builder)", downstream(d, ["builder"]) == {"mutator"} | READERS)
        check("downstream(holiday)", downstream(d, ["holiday"]) == {"mutator"} | READERS)
        check("downstream(demand)", downstream(d, ["demand"]) == {"builder", "mutator"} | READERS)
        check("downstream(kbparams)", downstream(d, ["kbparams"]) == {"kb"})
        check("downstream(r42) empty", downstream(d, ["r42"]) == set())
        check("downstream(unrelated) empty", downstream(d, ["unrelated"]) == set())

        print(f"depGraph port: lineage of PY-17 ({label})")
        up = upstream(d, "r42")
        order = [n["cell_id"] for n in d["nodes"] if n["cell_id"] in up | {"r42"}]
        check("lineage steps in order", order == ["demand", "reco", "holiday", "builder", "mutator", "r42"])
        srcs = {s["id"]: [n["cell_id"] for n in d["nodes"] if n["cell_id"] in up | {"r42"}
                          and any(x["id"] == s["id"] for x in n["sources"] or [])]
                for n in d["nodes"] for s in n["sources"] or []}
        check("athena source attributed to demand", srcs["microvm_demo_db.orders"] == ["demand"])
        check("dynamodb source attributed to reco", srcs["ecommerce-recommendations"] == ["reco"])
        check("url source attributed to holiday", srcs["https://date.nager.at"] == ["holiday"])
        check("url not read by r42 itself",
              not any(s["id"] == "https://date.nager.at" for s in node(d, "r42")["sources"]))
        check("bedrock_kb read by r42 itself",
              any(s["id"] == "AVSDQZX2CR" for s in node(d, "r42")["sources"]))
        check("url source raises external_state on holiday (trust reason)",
              [i["source"]["id"] for i in issues(d, "external_state", "holiday")] == ["https://date.nager.at"])
        check("mutator varsIn = products360, season_multiplier",
              sorted(e["var"] for e in edges(d, "mutator")) == ["products360", "season_multiplier"])

    print("\ndepGraph port: staleness")
    d = nb_build(PROVENANCE)
    check("all current after the full run", all(n["status"] == "current" for n in d["nodes"]))
    s = dag.apply_runs(d, [nb_run("mutator")], None, "vm-1", trigger="execute", now=NOW)
    check("changing a PY-10 slider marks the products360 readers stale", set(stale_ids(s)) == READERS)
    check("r42 stale via mutator", node(s, "r42")["stale_reason"]["via"] == ["mutator"])
    s = dag.apply_runs(d, [nb_run("builder")], None, "vm-1", trigger="execute", now=NOW)
    check("re-running the builder marks the mutator and then the readers stale",
          set(stale_ids(s)) == {"mutator"} | READERS)
    check("readers still read the mutated version",
          all(source_of(s, r, "products360") == "mutator" for r in READERS))
    s = dag.apply_runs(d, [nb_run("holiday")], None, "vm-1", trigger="execute", now=NOW)
    check("changing the holiday multiplier stales PY-10 and its downstream",
          set(stale_ids(s)) == {"mutator"} | READERS)

    print("\ndepGraph port: mutation signals (static analysis replaces the regex)")

    def static_mut(code):
        d = build([cell("b", "df = make()"), cell("a", code)])
        return node(d, "a")["mutates"]

    def runtime_mut(**kw):
        base = dict(cell_id="a", cell_type="code", code="", output_variable=None, ok=True, reads=(),
                    created=(), rebound=(), mutated=(), sources=None, executed_at=NOW)
        base.update(kw)
        d = build([cell("b", "df = make()"), cell("a", base["code"], base["cell_type"])], [RunCapture(**base)])
        return node(d, "a")["mutates"]

    check("explicit mutated list", runtime_mut(reads=("df",), mutated=("df",)) == ["df"])
    check("read + rebind is a definition, not a mutation (provenance is not a producer signal)",
          runtime_mut(code="df = df.dropna()", reads=("df",), rebound=("df",)) == [])
    for code in ("df['c'] = 1", 'df.loc[df.a > 1, "b"] = 0', "df['c'] += 1",
                 'df.sort_values("a", inplace=True)', "df.append(1)", "del df['c']"):
        check(f"mutation: {code}", static_mut(code) == ["df"])
    for code in ("x = df['c']", "df['c'] == 1", "df['c'] <= 1", 'y = df.sort_values("a")', "# df['c'] = 1"):
        check(f"not a mutation: {code}", static_mut(code) == [])
    check("other name: df2['c'] = 1 does not mutate df", "df" not in static_mut("df2['c'] = 1"))
    check("SQL cells never mutate",
          runtime_mut(cell_type="sql", code="SELECT * FROM df", reads=("df",), mutated=("df",)) == [])
    check("a cell that doesn't read the name can't mutate it",
          runtime_mut(code="df['c'] = 1", reads=(), mutated=("df",)) == [])

    print("\ndepGraph port: two mutators chain in order")
    cells = [cell("b", "df = make()"), cell("m1", "df['a'] = 1"), cell("mid", "n = len(df)"),
             cell("m2", 'df.sort_values("a", inplace=True)'), cell("end", "top = df.head()")]
    runs = [RunCapture("b", "code", cells[0]["code"], None, True, (), ("df",), (), (), None, NOW),
            RunCapture("m1", "code", cells[1]["code"], None, True, ("df",), (), (), ("df",), None, NOW),
            RunCapture("mid", "code", cells[2]["code"], None, True, ("df",), ("n",), (), (), None, NOW),
            RunCapture("m2", "code", cells[3]["code"], None, True, ("df",), (), (), ("df",), None, NOW),
            RunCapture("end", "code", cells[4]["code"], None, True, ("df",), ("top",), (), (), None, NOW)]
    d = build(cells, runs)
    check("inputs: m1←b, mid←m1, m2←m1, end←m2",
          [source_of(d, c, "df") for c in ("m1", "mid", "m2", "end")] == ["b", "m1", "m1", "m2"])
    check("downstream(m1)", downstream(d, ["m1"]) == {"end", "m2", "mid"})
    check("downstream(m2)", downstream(d, ["m2"]) == {"end"})
    check("no cycles", not issues(d, "cycle"))

    print("\ndepGraph port: plain def-use and out-of-order reads")
    cells = [cell("r", "y = x"), cell("w", "x = 1")]
    runs = [RunCapture("r", "code", "y = x", None, True, ("x",), ("y",), (), (), None, NOW),
            RunCapture("w", "code", "x = 1", None, True, (), ("x",), (), (), None, NOW)]
    d = build(cells, runs)
    check("single fallback edge w → r via x",
          [(e["from"], e["to"], e["var"]) for e in d["edges"]] == [("w", "r", "x")])
    d = build([cell("a", "x = x")], [RunCapture("a", "code", "x = x", None, True, ("x",), ("x",), (), (),
                                                None, NOW)])
    check("read+write x with no earlier producer → no self-edge", d["edges"] == [])


def t_change_check_hashes():
    """delta-plan §0.1: the builder's output keeps graph_hash on a no-op rebuild
    and on a leaf re-run (clocks are runtime channel); an upstream re-run moves
    the graph (downstream stale_reason)."""
    from app.notebook.dag_contract import graph_hash, runtime_hash
    print("\nchange check (graph_hash / runtime_hash of builder output)")
    cells = [cell("a", "x = 1"), cell("b", "y = x + 1"), cell("c", "z = y * 2")]
    d = build(cells, [sim_run(c) for c in cells], {"clock": 3, "vars": {}, "runs": {}})
    same = dag.apply_cells(d, cells, trigger="save", now=NOW)
    check("apply_cells(prev, same cells): graph_hash and runtime_hash unchanged",
          graph_hash(same) == graph_hash(d) and runtime_hash(same) == runtime_hash(d))
    leaf = dag.apply_runs(d, [sim_run(cells[2])], {"clock": 4, "vars": {}, "runs": {}}, "vm-1",
                          trigger="execute", now=NOW)
    check("leaf re-run: same graph_hash, different runtime_hash",
          graph_hash(leaf) == graph_hash(d) and runtime_hash(leaf) != runtime_hash(d))
    leaf2 = dag.apply_runs(leaf, [sim_run(cells[2])], {"clock": 5, "vars": {}, "runs": {}}, "vm-1",
                           trigger="execute", now=NOW)
    check("second leaf re-run: graph_hash still unchanged", graph_hash(leaf2) == graph_hash(d))
    up = dag.apply_runs(d, [sim_run(cells[0])], {"clock": 4, "vars": {}, "runs": {}}, "vm-1",
                        trigger="execute", now=NOW)
    check("upstream re-run: graph_hash changes (downstream stale_reason)",
          graph_hash(up) != graph_hash(d)
          and node(up, "b")["stale_reason"] is not None and node(d, "b")["stale_reason"] is None)
    stripped = dag.strip_for_vm(dict(d, graph_hash="g", runtime_version=3, runtime_hash="r"))
    check("strip_for_vm keeps the stamps, drops runtime/pending",
          stripped["graph_hash"] == "g" and stripped["runtime_version"] == 3
          and "runtime" not in stripped and "pending" not in stripped)


ALL = [t_edge_rule_and_chain, t_no_self_edge, t_multiple_definitions, t_out_of_order, t_cycle,
       t_missing_producer, t_edited_fallback, t_sql_output_variable_rule, t_pending, t_seeding, t_statuses,
       t_issue_codes, t_augassign_parity, t_reconcile_matrix, t_cold_attach, t_nit2,
       t_determinism_and_helpers, t_depgraph_port, t_change_check_hashes]


def test_dag_builder():
    """pytest entry point."""
    _checks.clear()
    for t in ALL:
        t()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    for t in ALL:
        t()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
