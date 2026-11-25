#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: the ordered `outputs` list the VM executor returns for one cell
(app/notebook/executor.py `_collect_outputs`, the display()/plt.show()/fig.show()
recording, caps, error runs, legacy fields) and its wire shape (code_engine).

Standalone (no pytest, no AWS):
    .venv/bin/python tests/vm/test_multi_output_executor.py
Exits non-zero on any failure. matplotlib cases SKIP when matplotlib is missing.
"""
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["NOTEBOOK_MAX_DISPLAY_ROWS"] = "50"
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))  # .venv has no duckdb

import logging  # noqa: E402
from app.notebook import executor as exmod  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.notebook.code_engine import _code_result_dict  # noqa: E402

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


def run(code, ex=None):
    ex = ex or SandboxExecutor()
    return ex.execute(code, cell_id="c1")


def kinds(r):
    return [it["t"] for it in r.outputs]


def table_cols(item):
    return item["payload"]["columns"]


DF = "import pandas as pd\ndf1 = pd.DataFrame({'a': [1, 2]})\ndf2 = pd.DataFrame({'b': [3]})\n"

# 1. the PY-31 cell
print("\nprint + display + print + display")
r = run(DF + "print('one')\ndisplay(df1)\nprint('two')\ndisplay(df2)\n")
check("text, table, text, table", kinds(r) == ["text", "table", "text", "table"], kinds(r))
check("text segments are the printed lines",
      r.outputs[0]["text"] == "one\n" and r.outputs[2]["text"] == "two\n")
check("tables are df1 then df2 with labels",
      table_cols(r.outputs[1]) == ["a"] and table_cols(r.outputs[3]) == ["b"]
      and r.outputs[1]["label"] == "df1" and r.outputs[3]["label"] == "df2")
check("legacy output is the full stdout", r.output == "one\ntwo\n")
check("legacy html is the FIRST rich item", r.html == json.dumps(r.outputs[1]["payload"]))
check("legacy image empty", r.image == "")

# 2. display(a) then bare b
print("\ndisplay(a) then bare b")
r = run(DF + "display(df1)\ndf2")
check("both listed, a then b", kinds(r) == ["table", "table"]
      and table_cols(r.outputs[0]) == ["a"] and table_cols(r.outputs[1]) == ["b"], kinds(r))

# 3. display(x) as the last line: not twice (explicit re-eval trap)
print("\ndisplay(x) last line")
r = run(DF + "display(df1)")
check("x listed exactly once", kinds(r) == ["table"], kinds(r))
r = run(DF + "x = df1\ndisplay(x)\n")
check("x listed once (assigned name)", kinds(r) == ["table"] and r.outputs[0].get("label") in ("x", "df1"))
r = run(DF + "display(df1, df2)")
check("display(a, b) lists both once", kinds(r) == ["table", "table"], kinds(r))
r = run(DF + "def show():\n    display(df1)\nshow()")
check("function that displays, called last: listed once", kinds(r) == ["table"], kinds(r))

# 4. two Plotly figures
print("\nPlotly")
PLY = "import plotly.graph_objects as go\n"
r = run(PLY + "f1 = go.Figure(go.Bar(x=[1], y=[2]))\nf2 = go.Figure(go.Bar(x=[3], y=[4]))\ndisplay(f1)\ndisplay(f2)")
check("two figures via display()", kinds(r) == ["plotly", "plotly"], kinds(r))
check("plotly items carry the snippet and labels",
      all('data-plotly="true"' in it["html"] for it in r.outputs)
      and [it["label"] for it in r.outputs] == ["f1", "f2"])
r = run(PLY + "fig1 = go.Figure(go.Bar(x=[1], y=[2]))\nfig2 = go.Figure(go.Bar(x=[3], y=[4]))")
check("two figures only assigned: namespace scan stays a single-figure fallback",
      kinds(r) == ["plotly"], kinds(r))
r = run(PLY + "fig = go.Figure(go.Bar(x=[1], y=[2]))\nfig.show()\nfig = go.Figure(go.Bar(x=[5], y=[6]))\nfig.show()")
check("fig.show() twice with a reused name: two charts in call order",
      kinds(r) == ["plotly", "plotly"], kinds(r))
check("  they are different figures", r.outputs[0]["html"] != r.outputs[1]["html"])
r = run(PLY + "fig = go.Figure(go.Bar(x=[1], y=[2]))\nfig")
check("single figure last expression", kinds(r) == ["plotly"])

# 5. matplotlib
print("\nmatplotlib")
if not HAVE_MPL:
    print("SKIP  matplotlib not installed")
else:
    MPL = "import matplotlib.pyplot as plt\n"
    r = run(MPL + "for i in range(3):\n    plt.figure()\n    plt.plot([i, i + 1])\n")
    check("3 figures in a loop, never closed, no show: 3 images", kinds(r) == ["image"] * 3, kinds(r))
    check("  distinct PNGs in figure order", len({it["src"] for it in r.outputs}) == 3
          and all(it["src"].startswith("data:image/png;base64,") for it in r.outputs))
    check("  legacy image is the LAST figure", r.image == r.outputs[-1]["src"])
    r = run(MPL + "for i in range(3):\n    plt.figure()\n    plt.plot([i, i + 1])\n    print('fig', i)\n    plt.show()\n")
    check("3 figures with plt.show() in the loop: image/text interleaved in call order",
          kinds(r) == ["text", "image", "text", "image", "text", "image"], kinds(r))
    check("  text sits before each figure", r.outputs[0]["text"] == "fig 0\n" and r.outputs[2]["text"] == "fig 1\n")
    r = run(MPL + "plt.plot([1, 2])\nplt.show()\nplt.plot([3, 4])\nplt.show()")
    check("plt.show() twice on one figure: captured once", kinds(r) == ["image"], kinds(r))
    r = run(MPL + "plt.figure()\nprint('empty')\n")
    check("figure without axes: no rich output", r.outputs == [])
    import matplotlib.pyplot as _plt
    check("figures are closed after the run", _plt.get_fignums() == [])
    # 6. table + chart + image interleaved
    r = run(DF + MPL + PLY + "print('a')\ndisplay(df1)\nprint('b')\nf = go.Figure(go.Bar(x=[1], y=[2]))\ndisplay(f)\n"
            "print('c')\nplt.plot([1, 2])\nplt.show()\nprint('d')\n")
    check("table + chart + image with prints",
          kinds(r) == ["text", "table", "text", "plotly", "text", "image", "text"], kinds(r))
    check("  legacy html is the table, image is the figure", r.html == json.dumps(r.outputs[1]["payload"])
          and r.image == r.outputs[5]["src"])
    r = run(DF + MPL + "plt.plot([1, 2])\ndf1")
    check("last-expression table then still-open figure", kinds(r) == ["table", "image"], kinds(r))

# 7. error after two displays
print("\nerror after displays")
r = run(DF + "print('p')\ndisplay(df1)\ndisplay(df2)\nraise ValueError('boom')")
check("success False, error set", r.success is False and "boom" in r.error)
check("both tables and the text before them are kept", kinds(r) == ["text", "table", "table"], kinds(r))
check("legacy html/image stay empty on an error run", r.html == "" and r.image == "")
r = run("print('x')\nraise ValueError('boom')")
check("error with no rich item: outputs []", r.outputs == [] and "x" in r.output)
if HAVE_MPL:
    r = run(MPL + "plt.plot([1, 2])\nraise ValueError('boom')")
    check("figure drawn before the error is kept", kinds(r) == ["image"] and r.image == "", kinds(r))
r = run("x = (")
check("syntax error: no outputs", r.success is False and r.outputs == [])

# 8. caps
print("\ncaps")
r = run(DF + "for i in range(25):\n    display(df1)\n")
check("25 rich items: 20 kept plus the note", kinds(r) == ["table"] * 20 + ["text"], kinds(r)[-3:])
check("  note has the right N", r.outputs[-1]["text"] == "… 5 more outputs not shown", r.outputs[-1])
r = run(DF + "for i in range(300):\n    display(df1)\n")
check("300 displays: refs are capped but still counted", r.outputs[-1]["text"] == "… 280 more outputs not shown",
      r.outputs[-1])
saved = exmod.MAX_OUTPUT_BYTES
exmod.MAX_OUTPUT_BYTES = 600
r = run("import pandas as pd\ndf = pd.DataFrame({'a': list(range(40))})\ndisplay(df)\ndisplay(df)\ndisplay(df)")
exmod.MAX_OUTPUT_BYTES = saved
check("byte cap yields the note", r.outputs and r.outputs[-1]["t"] == "text"
      and "more outputs not shown" in r.outputs[-1]["text"], r.outputs[-1:] if r.outputs else r.outputs)
exmod.MAX_OUTPUT_BYTES = 100
r = run("import pandas as pd\ndf = pd.DataFrame({'a': ['x' * 50] * 40})\ndf")
exmod.MAX_OUTPUT_BYTES = saved
check("one table above the byte cap is still shown", kinds(r) == ["table"] and r.html != "", kinds(r))
exmod.MAX_OUTPUT_BYTES = 100
r = run("import pandas as pd\ndf = pd.DataFrame({'a': ['x' * 50] * 40})\ndisplay(df)\ndisplay(df)")
exmod.MAX_OUTPUT_BYTES = saved
check("first oversized table kept, later one dropped with the note",
      kinds(r) == ["table", "text"] and r.outputs[-1]["text"] == "… 1 more outputs not shown", kinds(r))
if HAVE_MPL:
    r = run(MPL + "for i in range(23):\n    plt.figure()\n    plt.plot([i, 1])\n")
    check("23 unclosed figures: 20 images and the note",
          kinds(r).count("image") == 20 and r.outputs[-1]["t"] == "text"
          and "3 more outputs not shown" in r.outputs[-1]["text"], kinds(r)[-2:])
rows = run("import pandas as pd\ndisplay(pd.DataFrame({'a': range(200)}))\ndisplay(pd.DataFrame({'a': range(2)}))")
check("row cap NOTEBOOK_MAX_DISPLAY_ROWS still applies per table",
      rows.outputs[0]["payload"]["shown_rows"] == 50
      and rows.outputs[0]["payload"]["total_rows"] == 200)
wide = run("import pandas as pd\ndisplay(pd.DataFrame({f'c{i}': [1] for i in range(30)}))\ndisplay(pd.DataFrame({'a': [1]}))")
check("column cap of 20 still applies per table", wide.outputs[0]["payload"]["shown_cols"] == 20)

# 9. golden legacy shapes
print("\nlegacy shapes (golden)")
golden = json.load(open(os.path.join(ROOT, ".agents/tasks/multi-output-cells/golden_legacy.json")))
sys.path.insert(0, os.path.join(ROOT, ".agents/tasks/multi-output-cells"))
import golden_capture  # noqa: E402
got = golden_capture.run_all()
for name, want in golden.items():
    check(f"golden {name}: output/html/image/success/error byte-identical", got[name] == want,
          [f for f in want if want[f] != got[name].get(f)])
r = run("import pandas as pd\ndf = pd.DataFrame({'a': [1]})\ndf")
check("single DataFrame: outputs holds exactly that table; html equals it",
      kinds(r) == ["table"] and r.html == json.dumps(r.outputs[0]["payload"]))
r = run("print('only text')")
check("text-only cell: outputs [] and output filled", r.outputs == [] and r.output == "only text\n")

# 10. wire shape
print("\nwire shape")
ex = SandboxExecutor()
d = _code_result_dict(run("print('t')", ex), ex)
check("/execute dict for a text-only code cell has outputs == []", d["outputs"] == [])
d = _code_result_dict(run(DF + "display(df1)\ndisplay(df2)", ex), ex)
check("/execute dict carries outputs next to output/html/image",
      len(d["outputs"]) == 2 and "output" in d and d["html"] and "image" in d)
json.dumps(d)  # serializable
from app.notebook import sql_engine  # noqa: E402
import inspect  # noqa: E402
check("SQL engine result dict has no outputs key (single table by design)",
      '"outputs"' not in inspect.getsource(sql_engine))

# 11. scratch runs and wrapper restore
print("\nscratch + wrapper restore")
ex = SandboxExecutor()
orig_fig_show = sys.modules["plotly.basedatatypes"].BaseFigure.show
orig_plt_show = sys.modules["matplotlib.pyplot"].show if HAVE_MPL else None
r = ex.execute(DF + "display(df1)\ndisplay(df2)", scratch={})
check("a scratch (agent) run returns outputs", kinds(r) == ["table", "table"])
check("Plotly show restored after a run", sys.modules["plotly.basedatatypes"].BaseFigure.show is orig_fig_show)
if HAVE_MPL:
    check("plt.show restored after a run", sys.modules["matplotlib.pyplot"].show is orig_plt_show)
    ex.execute("import matplotlib.pyplot as plt\nplt.plot([1])\nplt.show()\nraise RuntimeError('x')")
    check("plt.show restored after an exception", sys.modules["matplotlib.pyplot"].show is orig_plt_show)
ex.execute("import plotly.graph_objects as go\ngo.Figure().show()\n1/0")
check("Plotly show restored after an exception", sys.modules["plotly.basedatatypes"].BaseFigure.show is orig_fig_show)
r = run("display(1)\ndisplay('s')\nprint('t')")
check("display() of non-rich objects is ignored as before", r.outputs == [])

print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
