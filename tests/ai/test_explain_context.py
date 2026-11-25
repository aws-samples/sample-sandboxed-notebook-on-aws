#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for proxy.notebook.ai.explain_context (upstream selection from
DAG fixtures, budget + omission, cycles, missing producers).

Run:  .venv/bin/python tests/ai/test_explain_context.py   (exit 0 = pass)
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def fx(name, scenario):
    with open(os.path.join(_ROOT, "tests", "fixtures", name), encoding="utf-8") as f:
        s = json.load(f)["scenarios"][scenario]
    return s["cells"], s["doc"]


def main() -> int:
    from proxy.notebook.ai.explain_context import build_context, badge_labels, split_lines
    from proxy.notebook.ai.constants import EXPLAIN_UPSTREAM_CHAR_BUDGET

    print("linear chain (dag_lineage base)")
    cells, doc = fx("dag_lineage.json", "base")
    ctx = build_context(cells, doc, "3")
    check("labels PY-1/SQL-1/PY-2", [u["label"] for u in ctx["upstream"]] == ["PY-1", "SQL-1"]
          and ctx["target"]["label"] == "PY-2", str(ctx["upstream"]))
    check("topological order (param before sql)", [u["cell_id"] for u in ctx["upstream"]] == ["1", "2"])
    check("provides", ctx["upstream"][0]["provides"] == ["region"]
          and ctx["upstream"][1]["provides"] == ["orders_df"])
    check("upstream_available", ctx["upstream_available"] is True)
    check("no omissions / cycle / missing", not ctx["omitted_labels"] and not ctx["cycle"]
          and ctx["missing_inputs"] == [])
    check("unrelated + markdown cells excluded", all(u["cell_id"] not in ("4", "5") for u in ctx["upstream"]))
    check("badge_labels counts per type", badge_labels(cells)["4"] == "MD-1")

    print("diamond")
    cells, doc = fx("dag_explain.json", "diamond")
    ctx = build_context(cells, doc, "d")
    ids = [u["cell_id"] for u in ctx["upstream"]]
    check("each ancestor once", sorted(ids) == ["a", "b", "c"] and len(ids) == 3, str(ids))
    check("topological: a first", ids[0] == "a")
    a = next(u for u in ctx["upstream"] if u["cell_id"] == "a")
    check("a provides x once", a["provides"] == ["x"], str(a["provides"]))

    print("cycle (dag_cycle ran)")
    cells, doc = fx("dag_cycle.json", "ran")
    try:
        ctx = build_context(cells, doc, "a")
        ok = True
    except Exception as e:  # noqa: BLE001
        ok, ctx = False, {"cycle": False, "upstream": []}
        print("   raised", type(e).__name__)
    check("terminates without raising", ok)
    check("cycle flagged", ctx["cycle"] is True)
    check("target never upstream of itself", all(u["cell_id"] != "a" for u in ctx["upstream"]))

    print("missing producer (dag_lineage missing_producer)")
    cells, doc = fx("dag_lineage.json", "missing_producer")
    ctx = build_context(cells, doc, "3")
    check("missing_inputs has lookup", ctx["missing_inputs"] == ["lookup"], str(ctx["missing_inputs"]))
    check("no placeholder cell", [u["cell_id"] for u in ctx["upstream"]] == ["1", "2"])

    print("many ancestors beyond budget")
    cells, doc = fx("dag_explain.json", "many_ancestors")
    ctx = build_context(cells, doc, "m30")
    total = sum(len(u["code"]) for u in ctx["upstream"])
    full = {c["id"]: c["code"] for c in cells}
    check("total <= budget", total <= EXPLAIN_UPSTREAM_CHAR_BUDGET, str(total))
    check("some omitted", len(ctx["omitted_labels"]) > 0)
    kept_ids = {u["cell_id"] for u in ctx["upstream"]}
    check("nearest kept", "m29" in kept_ids and "m28" in kept_ids)
    check("farthest omitted", "m0" not in kept_ids)
    kept_n = len(kept_ids)
    check("kept + omitted = all ancestors", kept_n + len(ctx["omitted_labels"]) == 30,
          f"{kept_n}+{len(ctx['omitted_labels'])}")
    expected_omitted = [f"PY-{i + 1}" for i in range(30 - kept_n)]
    check("omitted labels are exactly the farthest", ctx["omitted_labels"] == expected_omitted,
          str(ctx["omitted_labels"][:4]))
    check("no cell partially included", all(u["code"] == full[u["cell_id"]] for u in ctx["upstream"]))
    tiny = build_context(cells, doc, "m30", budget=0)
    check("budget 0 keeps nothing, omits all", tiny["upstream"] == [] and len(tiny["omitted_labels"]) == 30)

    print("unsaved code, markdown, no DAG")
    cells, doc = fx("dag_lineage.json", "base")
    ctx = build_context(cells, doc, "3", own_code="monthly = 1\nmonthly\n")
    check("own_code overrides target code only", ctx["target"]["code"].startswith("monthly = 1")
          and ctx["upstream"][1]["code"].startswith("-- Orders"))
    check("line_count ignores trailing newline", ctx["target"]["line_count"] == 2)
    md = build_context(cells, doc, "4")
    check("markdown target has no upstream", md["upstream"] == [])
    nodag = build_context(cells, None, "3")
    check("DAG None -> upstream_available false", nodag["upstream_available"] is False
          and nodag["upstream"] == [])
    check("split_lines CRLF", split_lines("a\r\nb\r\n") == ["a", "b"])

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED: {_failures}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
