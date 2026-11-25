#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: one Plotly figure object yields at most one chart per cell run
(app/notebook/executor.py `_collect_outputs`). The plot_* helpers in
app/notebook/helpers/viz.py display the figure themselves AND return it, so a
helper call as the cell's last expression used to list the same chart twice
('Chart 1 · Plotly' and 'Chart 2 · Plotly'). Different figures must still each
show. Standalone (no pytest, no AWS):
    .venv/bin/python tests/vm/test_duplicate_chart_dedupe.py
Exits non-zero on any failure. The matplotlib case SKIPs when matplotlib is missing.
"""
import os
import sys
import types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
_duck = types.ModuleType("duckdb")  # .venv has no duckdb; the executor type-checks against it
_duck.DuckDBPyRelation = type("DuckDBPyRelation", (), {})
sys.modules.setdefault("duckdb", _duck)
import logging  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402
logging.disable(logging.WARNING)
try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False
import plotly.graph_objects  # noqa: E402,F401  (preloaded in the VM; enables fig.show recording)
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


ex = SandboxExecutor()
setup = ex.execute(
    "import pandas as pd\n"
    "pv = pd.DataFrame({'reviews': [1, 2, 0], 'negative_pct': [.1, .2, .3],\n"
    "                   'revenue': [5, 6, 7], 'category': ['a', 'b', 'a']})",
    cell_id="setup")
check("setup cell runs", setup.success, setup.error)


def run(code):
    r = ex.execute(code, cell_id="c1")
    if not r.success:
        print("      error:", r.error)
    return [it["t"] for it in r.outputs], r


SCATTER = ("plot_scatter(pv[pv['reviews'] > 0],\n"
           "             x='negative_pct', y='revenue', size='reviews', color='category',\n"
           "             title='Revenue vs negative-sentiment share (bubble = # reviews)')")

print("\nsame figure: one chart")
k, _ = run("# Portfolio map: revenue vs negativity...\n" + SCATTER)
check("(a) helper call as the last expression: one chart (the reported bug)", k == ["plotly"], k)
k, _ = run(SCATTER + "\nx = 1")
check("(b) helper call followed by a statement: one chart", k == ["plotly"], k)
k, _ = run("fig = " + SCATTER + "\nfig")
check("(c) fig = helper(...) then fig: one chart", k == ["plotly"], k)
k, r = run("fig = " + SCATTER + "\nfig")
check("    the chart carries the fig label", r.outputs[0].get("label") == "fig", r.outputs[0].get("label"))
k, _ = run("import plotly.express as px\nfig = px.bar(pv, x='category', y='revenue')\nfig.show()\nfig")
check("(d) fig.show() then fig: one chart", k == ["plotly"], k)
k, _ = run("import plotly.express as px\nfig = px.bar(pv, x='category', y='revenue')\ndisplay(fig)\nfig.show()")
check("display(fig) then fig.show(): one chart", k == ["plotly"], k)
k, _ = run("fig = plot_line(pv, x='category', y='revenue')\ndisplay(fig)")
check("helper then display(fig): one chart", k == ["plotly"], k)
k, _ = run("for _ in range(2):\n    plot_bar(pv, x='category', y='revenue')")
check("helper twice in a loop: two different figures, two charts", k == ["plotly", "plotly"], k)

if HAVE_MPL:
    k, _ = run("import matplotlib.pyplot as plt\nf = plt.figure()\nplt.plot([1, 2])\nplt.show()\nf")
    check("(e) matplotlib plt.show() then the figure as last expression: one image", k == ["image"], k)
else:
    print("SKIP  (e) matplotlib not installed")

print("\ndifferent figures: each shows")
k, _ = run(SCATTER + "\nplot_bar(pv, x='category', y='revenue')")
check("two helper calls, the second last: two charts", k == ["plotly", "plotly"], k)
k, r = run("import plotly.express as px\nf1 = px.bar(pv, x='category', y='revenue')\n"
           "f2 = px.line(pv, x='category', y='revenue')\nf1.show()\nf2.show()")
check("fig.show() on two figures: two charts", k == ["plotly", "plotly"], k)
check("    in call order with labels", [it.get("label") for it in r.outputs] == ["f1", "f2"],
      [it.get("label") for it in r.outputs])
k, _ = run("import plotly.express as px\nf1 = px.bar(pv, x='category', y='revenue')\n"
           "f2 = px.line(pv, x='category', y='revenue')\nf1.show()\nf2")
check("f1.show() then f2 as last expression: two charts", k == ["plotly", "plotly"], k)

print("\ntables unchanged")
k, _ = run("display(pv)\npv")
check("display(df) then df: still two tables (dedupe is figures only)", k == ["table", "table"], k)

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nall passed")
