#!/usr/bin/env python3
# test-kind: offline
"""
Unit tests: in-place mutation in the static def-use analysis
(app/notebook/static_analysis.py) and its effect on runtime provenance.

A cell that changes an existing variable in place (`df['c'] = …`,
`df.sort_values(…, inplace=True)`, `items.append(x)`, `del df['c']`) must be
reported as reading AND writing it (plus a "mutated" list), so the dependency
graph (web/src/services/depGraph.js) chains builder → mutator → readers. The
executor folds assigned_targets into a run's "modified" set, so the LAST
mutator becomes the variable's provenance last_cell.

Standalone (no pytest, no AWS): run with
    .venv/bin/python tests/vm/test_static_mutation.py
Exits non-zero on any failure. Each check prints PASS/FAIL.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("AWS_REGION", "us-west-2")
# .venv lacks plotly and duckdb — stub them BEFORE importing app code.
_plotly = types.ModuleType("plotly")
_px = types.ModuleType("plotly.express")
_plotly.express = _px
sys.modules.setdefault("plotly", _plotly)
sys.modules.setdefault("plotly.express", _px)
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))

from app.notebook.static_analysis import analyze_cell, assigned_targets, mutated_names  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


# Shape of the "AWS Data Sources" sample (products360 builder → PY-10 mutator → readers).
BUILDER = ("products = raw.merge(other, on='id')\n"
           "products['n'] = products['n'].fillna(0)\n")
SUBSCRIPT_MUTATOR = ("risk_days = 14\n"
                     "products['score'] = range(len(products))\n"
                     "products['score'] = products['score'] * risk_days * mult\n")
INPLACE_MUTATOR = ("products.sort_values('score', ascending=False, inplace=True)\n"
                   "products.reset_index(drop=True, inplace=True)\n")
READER = "top = products.head(3)\nprint(len(top))\n"


def test_static_mutation():
    b = analyze_cell("code", BUILDER)
    check("builder writes products, does not read or mutate it",
          "products" in b["writes"] and "products" not in b["reads"] and b["mutated"] == [], repr(b))

    m = analyze_cell("code", SUBSCRIPT_MUTATOR)
    check("subscript mutator reads AND writes products",
          "products" in m["reads"] and "products" in m["writes"], repr(m))
    check("subscript mutator: mutated == [products]", m["mutated"] == ["products"], repr(m))
    check("subscript mutator still writes risk_days and reads mult",
          "risk_days" in m["writes"] and "mult" in m["reads"], repr(m))

    i = analyze_cell("code", INPLACE_MUTATOR)
    check("inplace=True method mutator reads AND writes products",
          "products" in i["reads"] and "products" in i["writes"] and i["mutated"] == ["products"], repr(i))

    r = analyze_cell("code", READER)
    check("reader reads (not writes / mutates) products",
          "products" in r["reads"] and "products" not in r["writes"] and r["mutated"] == [], repr(r))

    check("mutating container methods (append / update) count",
          mutated_names("items.append(1)\nconfig.update(a=1)\n") == {"items", "config"})
    check("del on a subscript / attribute counts",
          mutated_names("del df['c']\ndel obj.attr\n") == {"df", "obj"})
    check("augmented assignment on a subscript and on a name count",
          mutated_names("df['c'] += 1\ntotal += 1\n") == {"df", "total"})
    check("chained subscript/attribute target resolves to the root name",
          mutated_names("df.loc[df['a'] > 1, 'b'] = 0\n") == {"df"})
    check("non-mutating method calls do not count",
          mutated_names("x = df.sort_values('a')\ny = df.head()\n") == set())
    check("in-place writes inside a def body (not run by the cell) do not count",
          mutated_names("def f():\n    df['c'] = 1\n") == set())
    check("assigned_targets includes in-place method call bases (feeds runtime provenance)",
          "products" in assigned_targets(INPLACE_MUTATOR))

    h = analyze_cell("code", "raw = read_athena('SELECT 1')\ndisplay(raw)\n")
    check("injected helpers / display not listed as reads",
          "read_athena" not in h["reads"] and "display" not in h["reads"], repr(h))

    s = analyze_cell("sql", "SELECT * FROM products", "result")
    check("SQL cell: no mutated list entries", s["mutated"] == [] and s["reads"] == ["products"], repr(s))


def test_runtime_provenance_last_mutator():
    """The executor uses assigned_targets for its "modified" set: after builder →
    subscript mutator → inplace mutator, products' last_cell is the LAST mutator."""
    try:
        import pandas  # noqa: F401
    except ImportError:
        print("SKIP  runtime provenance (pandas not installed)")
        return
    from app.notebook.executor import SandboxExecutor
    ex = SandboxExecutor()
    ex.reset()
    cells = [
        ("c2", "import pandas as pd\nraw = pd.DataFrame({'id': [1, 2, 3]})\nmult = 1.1\n"),
        ("c3", "products = raw.copy()\n"),
        ("c4", SUBSCRIPT_MUTATOR),
        ("c5", INPLACE_MUTATOR),
        ("c6", READER),
    ]
    results = {}
    for cid, code in cells:
        res = ex.execute(code, cell_id=cid)
        results[cid] = res
        check(f"{cid} runs", res.success, res.error)
    # Runtime def-use lists: an in-place mutator reports the frame as mutated AND
    # written (so stored cells chain builder → mutator → readers after a reload).
    for cid in ("c4", "c5"):
        r = results[cid]
        check(f"runtime {cid}: variables_mutated == ['products']",
              r.variables_mutated == ["products"], repr(r.variables_mutated))
        check(f"runtime {cid}: products in variables_created and variables_read",
              "products" in r.variables_created and "products" in r.variables_read,
              f"created={r.variables_created} read={r.variables_read}")
    check("runtime c3 (builder): products created, not mutated",
          "products" in results["c3"].variables_created and results["c3"].variables_mutated == [],
          f"created={results['c3'].variables_created} mutated={results['c3'].variables_mutated}")
    check("runtime c6 (reader): nothing mutated, products only read",
          results["c6"].variables_mutated == [] and "products" not in results["c6"].variables_created,
          f"created={results['c6'].variables_created} mutated={results['c6'].variables_mutated}")
    prov = ex.get_var_provenance().get("products") or {}
    check("provenance: products defined_by c3", prov.get("defined_by") == "c3", repr(prov))
    check("provenance: products last_cell is the last mutator c5", prov.get("last_cell") == "c5", repr(prov))
    check("provenance: modified_by lists both mutators", prov.get("modified_by") == ["c4", "c5"], repr(prov))
    # Re-running a cell that REBINDS a var is a definition, not a mutation.
    rerun = ex.execute("products = raw.copy()\n", cell_id="c3")
    check("re-running the builder (rebind) is not a mutation",
          rerun.success and rerun.variables_mutated == [], repr(rerun.variables_mutated))


if __name__ == "__main__":
    test_static_mutation()
    test_runtime_provenance_last_mutator()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("ALL PASS")
