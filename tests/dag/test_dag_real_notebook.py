#!/usr/bin/env python3
# test-kind: offline
"""
The DAG builder on a real notebook: 'AWS Data Sources' (44 cells), extracted
read-only from proxy/data/microvm.db into tests/fixtures/aws_data_sources_cells.json
by tests/fixtures/extract_aws_data_sources.py.

AC-3 is asserted on two builds:
  1. static-only: apply_cells(None, cells) from the code alone;
  2. simulated runtime: one RunCapture per code/SQL cell. The stored history
     predates the executor's variables_mutated / variables_rebound fields (PY-10
     at pos 29 does not list products360 in its stored writes), so the new
     executor's fields are SIMULATED from the stored lists plus static analysis:
       reads   = stored vars_read
       created = stored vars_written
       mutated = analyze_cell(code).mutated ∩ reads
       rebound = (analyze_cell(code).writes ∩ reads) − mutated
     SQL cells use the stored reads and their output variable.

Both builds must give downstream(pos 29) == {31,32,34,35,42},
downstream(pos 27) ⊇ {29,31,32,34,35,42}, and lineage(pos 42) containing pos 27
plus its upstream SQL / S3 cells.

Run:
    python3 tests/dag/test_dag_real_notebook.py
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app.notebook.static_analysis import analyze_cell  # noqa: E402
from proxy.notebook import dag  # noqa: E402
from proxy.notebook.dag import RunCapture  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "aws_data_sources_cells.json")
NOW = "2026-05-01T10:00:00.000Z"

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _load():
    with open(FIXTURE, encoding="utf-8") as fh:
        rows = json.load(fh)
    cells = [{"id": r["id"], "type": r["type"], "code": r["code"],
              "outputVariable": r["output_variable"] if r["type"] == "sql" else None} for r in rows]
    return rows, cells


def _capture(row):
    if row["type"] == "sql":
        ov = row["output_variable"] or "result"
        return RunCapture(row["id"], "sql", row["code"], ov, True, tuple(row["vars_read"]), (), (), (),
                          tuple(row["sources_read"]), NOW)
    a = analyze_cell("code", row["code"])
    reads = set(row["vars_read"])
    mutated = set(a["mutated"]) & reads
    rebound = (set(a["writes"]) & reads) - mutated
    return RunCapture(row["id"], "code", row["code"], None, True, tuple(sorted(reads)),
                      tuple(row["vars_written"]), tuple(sorted(rebound)), tuple(sorted(mutated)),
                      tuple(row["sources_read"]), NOW)


def _closure(doc, start, forward):
    adj = {}
    for e in doc["edges"]:
        a, b = (e["from"], e["to"]) if forward else (e["to"], e["from"])
        adj.setdefault(a, set()).add(b)
    seen, queue = set(), [start]
    while queue:
        cur = queue.pop()
        for n in adj.get(cur, ()):
            if n not in seen and n != start:
                seen.add(n)
                queue.append(n)
    return seen


def _assert_ac3(label, doc, rows):
    by_pos = {n["position"]: n["cell_id"] for n in doc["nodes"]}
    pos_of = {v: k for k, v in by_pos.items()}
    row_at = {r["position"]: r for r in rows}

    def down(p):
        return {pos_of[c] for c in _closure(doc, by_pos[p], True)}

    lineage = {pos_of[c] for c in _closure(doc, by_pos[42], False)}
    d29 = down(29)
    check(f"[{label}] downstream(pos 29) == {{31,32,34,35,42}} (got {sorted(d29)})",
          d29 == {31, 32, 34, 35, 42})
    d27 = down(27)
    check(f"[{label}] downstream(pos 27) ⊇ {{29,31,32,34,35,42}} (got {sorted(d27)})",
          d27 >= {29, 31, 32, 34, 35, 42})
    check(f"[{label}] lineage(pos 42) contains pos 27 and pos 29", {27, 29} <= lineage)
    sql_up = {p for p in lineage if row_at[p]["type"] == "sql"}
    check(f"[{label}] lineage(pos 42) contains the SQL cells feeding pos 27 (got {sorted(sql_up)})",
          {6, 8, 10, 13, 15, 18} <= sql_up)
    check(f"[{label}] lineage(pos 42) contains the S3 cell pos 17 (demand_signal / inventory)", 17 in lineage)
    n29 = doc["nodes"][[n["position"] for n in doc["nodes"]].index(29)]
    check(f"[{label}] pos 29 mutates products360", n29["mutates"] == ["products360"])
    return d29


def _run():
    if not os.path.exists(FIXTURE):
        print("  (fixture missing — skipped)")
        return
    rows, cells = _load()
    check("fixture has 44 cells", len(rows) == 44)
    print("\nstatic-only build")
    static = dag.apply_cells(None, cells, trigger="open", now=NOW)
    check("one node per code/SQL cell", len(static["nodes"]) == sum(1 for r in rows if r["type"] != "markdown"))
    _assert_ac3("static", static, rows)
    print("\nsimulated runtime build")
    runs = [_capture(r) for r in rows if r["type"] != "markdown"]
    runtime = dag.apply_runs(static, runs, {"clock": len(runs), "vars": {}, "runs": {}}, "vm-sim",
                             trigger="batch", now=NOW)
    check("all nodes runtime + current",
          all(n["facts_origin"] == "runtime" and n["status"] == "current" for n in runtime["nodes"]))
    _assert_ac3("runtime", runtime, rows)
    pos_of = {n["cell_id"]: n["position"] for n in runtime["nodes"]}
    ext = {(pos_of[i["cell_id"]], i["source"]["kind"]) for i in runtime["issues"]
           if i["code"] == "external_state"}
    check("external_state for the url (25), local (23) and s3 (17) cells; none for athena/dynamodb/bedrock",
          {(25, "url"), (23, "local"), (17, "s3")} <= ext
          and not any(k in ("athena", "dynamodb", "bedrock_kb") for _, k in ext))


def test_dag_real_notebook():
    """pytest entry point."""
    _checks.clear()
    _run()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
