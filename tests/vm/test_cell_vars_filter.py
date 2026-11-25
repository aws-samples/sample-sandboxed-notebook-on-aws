#!/usr/bin/env python3
# test-kind: offline
"""
Unit tests: a cell's def-use lists (variables_read / variables_created, which the
frontend stores as varsRead / varsWritten) contain ONLY user variables.

Excluded: dunders (__builtins__), module objects bound by imports, and
sandbox-injected names (display, read_* helpers) unless the cell rebinds them.
Kept: genuine assignments, user functions/classes, DataFrames. Also checks the
open-time static estimate (static_analysis.analyze_cell) drops `import x` writes,
so the reactive graph agrees before and after a run. The runtime provenance
(runtime.vars) follows the same filter, including provenance restored from old
checkpoints (test_provenance_filtered).

Standalone (no pytest, no AWS): run with
    .venv/bin/python tests/vm/test_cell_vars_filter.py
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

from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.notebook.static_analysis import analyze_cell  # noqa: E402
from app.notebook import code_engine  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


CELL1 = '''
import json
import os.path
import pandas as pd
import collections as col
from os import path as ospath
from json import dumps
data = {"a": 1}
countries = pd.DataFrame({"c": ["us", "eu"]})
def helper_fn(x):
    return x + 1
class Thing:
    pass
display(countries)
'''


def test_writes_filtered():
    ex = SandboxExecutor()
    ex.reset()  # fresh namespace: __builtins__ / display / helpers all appear "new"
    res = ex.execute(CELL1, cell_id="c1")
    check("cell 1 succeeds", res.success, res.error)
    w = set(res.variables_created)
    check("real variables kept (data, countries, helper_fn, Thing)",
          {"data", "countries", "helper_fn", "Thing"} <= w, repr(sorted(w)))
    check("from-import of a function kept (dumps)", "dumps" in w, repr(sorted(w)))
    check("no dunders", not any(n.startswith("__") and n.endswith("__") for n in w), repr(sorted(w)))
    check("no modules (json, os, pd, col, ospath)",
          not ({"json", "os", "pd", "col", "ospath"} & w), repr(sorted(w)))
    check("injected display not reported", "display" not in w, repr(sorted(w)))
    d = code_engine._code_result_dict(res, ex)
    check("_code_result_dict variables_created matches", d["variables_created"] == res.variables_created)
    check("module still usable in namespace (not removed, only unreported)",
          isinstance(ex._namespace.get("pd"), types.ModuleType))


def test_reads_filtered():
    ex = SandboxExecutor()
    ex.execute(CELL1, cell_id="c1")
    helper = next((n for n in ex._injected_helpers() if n.startswith("read_")), None)
    code = ("total = len(countries) + helper_fn(data['a'])\n"
            "obj = Thing()\n"
            "s = pd.Series([1]) if json and col else None\n"
            "display(total)\n")
    if helper:
        code += f"_h = {helper}\n"
    res = ex.execute(code, cell_id="c2")
    check("cell 2 succeeds", res.success, res.error)
    r = set(res.variables_read)
    check("real reads kept (countries, helper_fn, data, Thing)",
          {"countries", "helper_fn", "data", "Thing"} <= r, repr(sorted(r)))
    check("module reads dropped (pd, json, col)", not ({"pd", "json", "col"} & r), repr(sorted(r)))
    check("display read dropped", "display" not in r, repr(sorted(r)))
    check("injected helper read dropped", helper is None or helper not in r, repr((helper, sorted(r))))
    check("writes are user vars only", set(res.variables_created) == {"total", "obj", "s"} | ({"_h"} if helper else set()),
          repr(res.variables_created))


def test_rebound_injected_names_kept():
    ex = SandboxExecutor()
    ex.reset()
    helper = next((n for n in ex._injected_helpers() if n.startswith("read_")), None)
    code = "display = 5\n"
    if helper:
        code += f"def {helper}(*a, **k):\n    return 1\n"
    res = ex.execute(code, cell_id="c1")
    check("rebind cell succeeds", res.success, res.error)
    check("rebound display is reported", "display" in res.variables_created, repr(res.variables_created))
    if helper:
        check("user-redefined helper is reported", helper in res.variables_created, repr(res.variables_created))
    res2 = ex.execute("y = repr(display)", cell_id="c2")
    # execute() re-injects the display shim each run, so the read resolves to the shim.
    check("shim re-injected per run: display read not reported", "display" not in res2.variables_read,
          repr(res2.variables_read))


def _noise(names):
    return sorted(n for n in names if (n.startswith("__") and n.endswith("__"))
                  or n in {"json", "os", "pd", "col", "ospath", "display", "time"})


def test_provenance_filtered():
    """runtime.vars (provenance) records user variables only — the same filter as
    the def-use lists — and tolerates noise restored from old checkpoints."""
    before = len(_fails)
    ex = SandboxExecutor()
    ex.reset()
    res = ex.execute(CELL1, cell_id="c1")
    check("provenance: cell 1 succeeds", res.success, res.error)
    rt = ex.runtime_section()
    names = set(rt["vars"])
    check("provenance: user vars recorded (data, countries, helper_fn, Thing, dumps)",
          {"data", "countries", "helper_fn", "Thing", "dumps"} <= names, repr(sorted(names)))
    check("provenance: no dunders / modules / display", not _noise(names), repr(_noise(names)))
    helper = next(iter(ex._injected_helpers()), None)
    check("provenance: no untouched injected helper", helper is None or helper not in names, repr(sorted(names)))
    check("provenance: internal map is clean too", not _noise(ex._var_provenance), repr(sorted(ex._var_provenance)))
    check("provenance: Variables panel still gets 'Defined in' for user vars",
          ex.get_variables().get("countries", {}).get("defined_by") == "c1")
    clock = rt["clock"]
    ex.execute("import time\nx = 1\n", cell_id="c2")
    rt2 = ex.runtime_section()
    check("provenance: re-run with an import records x, not time",
          "x" in rt2["vars"] and "time" not in rt2["vars"] and rt2["clock"] > clock, repr(sorted(rt2["vars"])))
    ex.execute("data = 2\n", cell_id="c3")
    ex.execute("import os as data\n", cell_id="c4")
    check("provenance: a variable rebound to a module loses its entry",
          "data" not in ex.runtime_section()["vars"])
    check("provenance: get_var_provenance (checkpoint fallback) is clean",
          not _noise(ex.get_var_provenance()))

    # Restore of an OLD checkpoint whose provenance still holds noise.
    old_entry = {"defined_by": "c1", "defined_at": 1, "modified_by": [], "last_cell": "c1", "last_exec": 9}
    old = {"__builtins__": dict(old_entry), "pd": dict(old_entry), "display": dict(old_entry),
           "np": dict(old_entry), "kept": dict(old_entry, last_exec=3)}
    ex2 = SandboxExecutor()
    ex2.reset()
    ex2.set_imports({"np": "import numpy as np"})  # ledger present, module not replayed yet
    ex2.set_runtime_section({"clock": 4, "vars": old, "runs": {}})
    rt3 = ex2.runtime_section()
    check("old dag.json runtime: dunder / display / ledger-module entries dropped, user var kept",
          # pd: not bound and not in the ledger yet, so not provably a module
          set(rt3["vars"]) == {"kept", "pd"}, repr(sorted(rt3["vars"])))
    ex2._namespace["pd"] = types.ModuleType("pandas")  # import replay rebinds pd
    check("old dag.json runtime: a module entry is hidden once the module is bound",
          set(ex2.runtime_section()["vars"]) == {"kept"})
    check("old dag.json runtime: clock still advances past dropped entries", rt3["clock"] == 9)
    ex3 = SandboxExecutor()
    ex3.reset()
    ex3.set_var_provenance(dict(old))
    v3 = ex3.runtime_section()["vars"]
    check("old provenance.json: dunder/display dropped, clock past every entry",
          "__builtins__" not in v3 and "display" not in v3 and "kept" in v3
          and ex3.runtime_section()["clock"] == 9, repr(sorted(v3)))
    assert len(_fails) == before, f"failed: {_fails[before:]}"


def test_static_analysis_consistent():
    a = analyze_cell("code", "import pandas as pd\nimport os.path\nfrom json import dumps\ndf = pd.DataFrame()\n")
    check("static writes drop `import x` bindings, keep from-imports + assignments",
          a["writes"] == ["df", "dumps"], repr(a))
    b = analyze_cell("code", "out = pd.concat([df])\n")
    check("static reads still list free names (no writer → no graph edge for pd)",
          a["writes"] and "pd" in b["reads"] and "df" in b["reads"], repr(b))
    s = analyze_cell("sql", "SELECT * FROM df", "result")
    # "dynamic" is the DAG builder's dynamic-name key (design §5.1); always [] for SQL.
    check("SQL cell analysis unchanged",
          s == {"reads": ["df"], "writes": ["result"], "mutated": [], "dynamic": []}, repr(s))


if __name__ == "__main__":
    test_writes_filtered()
    test_reads_filtered()
    test_rebound_injected_names_kept()
    test_provenance_filtered()
    test_static_analysis_consistent()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("ALL PASS")
