#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: a cell's last top-level expression runs exactly ONCE, inside the
timed and interruptible exec (app/notebook/executor.py `_execute_locked`), and
its value is still rendered in the same output position (code-review A-B-01).
Standalone (no pytest, no AWS):
    .venv/bin/python tests/vm/test_last_expr_single_eval.py
Exits non-zero on any failure. matplotlib / duckdb cases SKIP when missing.
"""
import os
import sys
import threading
import time
import types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False
    sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))  # import-time only
import logging  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.notebook.code_engine import _execute_isolated  # noqa: E402
logging.disable(logging.WARNING)
try:
    import matplotlib  # noqa: F401
    matplotlib.use("Agg")
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False
import plotly.graph_objects  # noqa: E402,F401  (preloaded in the VM)

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


def kinds(r):
    return [it["t"] for it in r.outputs]


# An external side-effect counter the cell code imports, so every exec path
# (user, agent scratch, internal scratch) can be checked the same way.
probe = types.ModuleType("_single_eval_probe")
probe.calls = []
sys.modules["_single_eval_probe"] = probe

BUMP = ("import _single_eval_probe as _p\n"
        "def bump():\n"
        "    _p.calls.append(1)\n"
        "    print('bumped')\n"
        "    return len(_p.calls)\n")

# 1. the bump() counter case (global counter, as in the review repro)
print("\nbump() counter")
ex = SandboxExecutor()
r = ex.execute("n = 0\ndef bump():\n    global n\n    n += 1\n    print('bump', n)\nbump()", cell_id="c1")
check("cell succeeds", r.success, r.error)
check("one print", r.output == "bump 1\n", repr(r.output))
check("n == 1", ex._namespace.get("n") == 1, ex._namespace.get("n"))
probe.calls.clear()
r = ex.execute(BUMP + "bump()", cell_id="c2")
check("probe counter called once", len(probe.calls) == 1, len(probe.calls))
check("  printed once", r.output == "bumped\n", repr(r.output))

# 2. rich last expressions keep their output position
print("\nrich last expression: position and count")
DF = "import pandas as pd\ndf1 = pd.DataFrame({'a': [1, 2]})\ndf2 = pd.DataFrame({'b': [3]})\n"
r = SandboxExecutor().execute(DF + "print('one')\ndisplay(df1)\nprint('two')\ndf2", cell_id="c1")
check("DataFrame last: text, table, text, table", kinds(r) == ["text", "table", "text", "table"], kinds(r))
check("  last table is df2", r.outputs[3]["payload"]["columns"] == ["b"])
r = SandboxExecutor().execute(DF + "df1", cell_id="c1")
check("DataFrame last only: one table", kinds(r) == ["table"], kinds(r))
PLY = "import plotly.graph_objects as go\n"
r = SandboxExecutor().execute(PLY + DF + "print('x')\ndisplay(df1)\nfig = go.Figure(go.Bar(x=[1], y=[2]))\nfig",
                              cell_id="c1")
check("Plotly last: text, table, plotly", kinds(r) == ["text", "table", "plotly"], kinds(r))
probe.calls.clear()
r = SandboxExecutor().execute(PLY + "import _single_eval_probe as _p\n"
                              "def mk():\n    _p.calls.append(1)\n    return go.Figure(go.Bar(x=[1], y=[2]))\nmk()",
                              cell_id="c1")
check("Plotly built by the last expression: one chart", kinds(r) == ["plotly"], kinds(r))
check("  builder ran once", len(probe.calls) == 1, len(probe.calls))
if HAVE_MPL:
    r = SandboxExecutor().execute("import matplotlib.pyplot as plt\nprint('p')\nf = plt.figure()\n"
                                  "plt.plot([1, 2])\nf", cell_id="c1")
    check("matplotlib figure last: text then one image", kinds(r) == ["text", "image"], kinds(r))
    r = SandboxExecutor().execute("import matplotlib.pyplot as plt\nplt.plot([1, 2])", cell_id="c1")
    check("plt.plot(...) last: one image", kinds(r) == ["image"], kinds(r))
    import matplotlib.pyplot as plt
    check("  (no stray open figures)", plt.get_fignums() == [], plt.get_fignums())
else:
    print("SKIP  matplotlib not installed")

# 3. display(df) as the last line
print("\ndisplay(df) last")
r = SandboxExecutor().execute(DF + "display(df1)", cell_id="c1")
check("shown once", kinds(r) == ["table"], kinds(r))
probe.calls.clear()
r = SandboxExecutor().execute(BUMP + DF + "display(df1 if bump() else df2)", cell_id="c1")
check("display(...) argument evaluated once", len(probe.calls) == 1, len(probe.calls))
check("  printed once, shown once", kinds(r) == ["text", "table"] and r.output == "bumped\n",
      (kinds(r), r.output))

# 4. a cell with only one expression
print("\nsingle-expression cell")
ex = SandboxExecutor()
ex.execute(BUMP, cell_id="c1")
probe.calls.clear()
r = ex.execute("bump()", cell_id="c2")
check("runs once", len(probe.calls) == 1 and r.output == "bumped\n", (len(probe.calls), r.output))
r = ex.execute("1 + 1", cell_id="c3")
check("non-rich value: success, no outputs", r.success and r.outputs == [], (r.success, r.outputs))

# 5. a cell ending with an assignment
print("\nassignment last")
probe.calls.clear()
r = ex.execute("v = bump()", cell_id="c4")
check("runs once, no value", len(probe.calls) == 1 and r.outputs == [] and ex._namespace["v"] == 1)
r = ex.execute(DF + "d = df1", cell_id="c5")
check("DataFrame assignment last: no table", r.outputs == [], kinds(r))

# 6. an error in an earlier line: the last expression does not run
print("\nerror before the last expression")
probe.calls.clear()
r = ex.execute("bump()\n1/0\nbump()", cell_id="c6")
check("fails at line 2", not r.success and r.error_lineno == 2, (r.success, r.error_lineno, r.error))
check("last expression did not run", len(probe.calls) == 1, len(probe.calls))
probe.calls.clear()
r = ex.execute("bump()\nx = (\nbump()", cell_id="c7")
check("syntax error: nothing runs", not r.success and r.error.startswith("SyntaxError") and probe.calls == [],
      (r.error, probe.calls))

# 7. an error IN the last expression: points at the user's line
print("\nerror in the last expression")
r = ex.execute("a = 1\nb = 0\n\na / b", cell_id="c8")
check("ZeroDivisionError at line 4", not r.success and r.error_lineno == 4, (r.error_lineno, r.error))
check("  error string names line 4", "(line 4)" in r.error, r.error)
check("  deepest cell frame is line 4",
      bool(r.error_frames) and r.error_frames[-1]["lineno"] == 4, r.error_frames)
check("  traceback cites <sandbox> line 4", 'File "<sandbox>", line 4' in r.traceback, r.traceback)
r = ex.execute("def f(x):\n    return x[5]\nz = 1\nf(\n  [1, 2])", cell_id="c9")
check("helper called from a multi-line last expression: deepest frame line 2, caller line 4",
      [f["lineno"] for f in r.error_frames] == [4, 2], r.error_frames)
check("  no value listed on error", r.outputs == [] or all(it["t"] == "text" for it in r.outputs), kinds(r))
r = ex.execute("x = 1\nx +", cell_id="c10")
check("syntax error in the last line: line 2", not r.success and r.error_lineno == 2, (r.error_lineno, r.error))

# 8. timeout / interrupt during a slow last expression
print("\ntimeout / interrupt in the last expression")


def run_bounded(fn, limit):
    """Run fn in a thread; returns (result, seconds) or (None, None) if it hangs."""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn()), daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(limit)
    return (box.get("r"), time.monotonic() - t0) if not t.is_alive() else (None, None)


os.environ["EXECUTION_TIMEOUT_SECONDS"] = "1"
ex = SandboxExecutor()
ex.execute("import time\ndef spin():\n    while True:\n        pass", cell_id="s0")
r, secs = run_bounded(lambda: ex.execute("x = 1\nspin()", cell_id="s1"), 15)
check("timeout honored in a busy last expression", r is not None and not r.success
      and "timed out" in r.error and secs < 5, (r and r.error, secs))
r, secs = run_bounded(lambda: ex.execute("time.sleep(3)", cell_id="s2"), 15)
check("timeout honored in time.sleep() as the last expression (fires when the C call returns)",
      r is not None and not r.success and "timed out" in r.error and secs < 6, (r and r.error, secs))
os.environ["EXECUTION_TIMEOUT_SECONDS"] = "60"
box = {}
t = threading.Thread(target=lambda: box.setdefault("r", ex.execute("spin()", cell_id="s3")), daemon=True)
t.start()
time.sleep(0.5)
injected = ex.interrupt()
t.join(10)
r = box.get("r")
check("interrupt() reaches a busy last expression", injected and r is not None and not r.success
      and "interrupted by user" in r.error, (injected, r and r.error))
r = ex.execute("x", cell_id="s4")
check("  sandbox healthy afterwards", r.success, r.error)
del os.environ["EXECUTION_TIMEOUT_SECONDS"]

# 9. agent / internal scratch paths: same single eval
print("\nagent / internal scratch")
ex = SandboxExecutor()
for origin, scope in (("agent", "turn-1"), ("agent", None), ("internal", None), ("", None)):
    probe.calls.clear()
    r = _execute_isolated(ex, BUMP + DF + "bump()\ndf1", None, origin, scope)
    label = origin or "user"
    check(f"{label}{'/' + scope if scope else ''}: bump runs once",
          r.success and len(probe.calls) == 1 and r.output == "bumped\n", (r.error, len(probe.calls), r.output))
    check(f"  {label}: last value rendered once", kinds(r) == ["text", "table"], kinds(r))
probe.calls.clear()
r = ex.execute(BUMP + "bump()", scratch={})
check("execute(scratch={}) runs the last expression once", len(probe.calls) == 1, len(probe.calls))

# 10. batch path goes through the same execute()
print("\nbatch path")
src = open(os.path.join(ROOT, "app/notebook/code_engine.py")).read()
check("execute-batch calls executor.execute (same single-eval body)", "executor.execute(" in src)

# 11. SQL cell unchanged
print("\nSQL")
sql_src = open(os.path.join(ROOT, "app/notebook/sql_engine.py")).read()
check("SQL engine never calls executor.execute / _collect_outputs (path untouched)",
      "executor.execute(" not in sql_src and "_collect_outputs" not in sql_src)
if HAVE_DUCKDB:
    from app.notebook.sql_engine import _run_sql_cell_blocking  # noqa: E402
    ex = SandboxExecutor()
    res = _run_sql_cell_blocking(ex, None, "SELECT 1 AS a", "out_df", "c-sql", bind_output=True)
    check("SELECT 1 runs and binds out_df", res.get("success") is True and "out_df" in ex._namespace, res)
else:
    print("SKIP  duckdb not installed (SQL run)")

print()
if _fails:
    print(f"{len(_fails)} FAILED: {_fails}")
    sys.exit(1)
print("ALL PASS")
