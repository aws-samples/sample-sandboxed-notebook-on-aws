#!/usr/bin/env python3
# test-kind: offline
"""
static_analysis additions for the DAG builder (design §5.1):
  * dynamic_name_uses(code) — exec/eval/globals/locals/vars calls and
    setattr/delattr rooted at sys.modules / __main__ / __builtins__;
  * analyze_cell(...)["dynamic"];
  * derive_sql_output_variable(sql) — port of web _deriveSqlVarName, checked
    against tests/fixtures/sql_output_variable_cases.json (the same file the
    web parity test reads);
  * cell_code_hash sanity (shared contract).

Run:
    python3 tests/dag/test_static_dynamic_names.py
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app.notebook.dag_contract import cell_code_hash  # noqa: E402
from app.notebook.static_analysis import (  # noqa: E402
    analyze_cell, derive_sql_output_variable, dynamic_name_uses,
)

CASES = os.path.join(ROOT, "tests", "fixtures", "sql_output_variable_cases.json")

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _run():
    print("\ndynamic_name_uses positives")
    for code, want in [
        ("exec('x = 1')", ["exec()"]),
        ("y = eval('1 + 1')", ["eval()"]),
        ("g = globals()\nl = locals()", ["globals()", "locals()"]),
        ("v = vars()", ["vars()"]),
        ("def f():\n    return eval('2')", ["eval()"]),
        ("import sys\nsetattr(sys.modules[__name__], 'x', 1)", ["setattr()"]),
        ("import __main__\nsetattr(__main__, 'x', 1)", ["setattr()"]),
        ("delattr(__builtins__, 'print')", ["delattr()"]),
        ("import sys\ndelattr(sys.modules['__main__'].sub, 'x')", ["delattr()"]),
        ("exec('a')\nexec('b')\neval('c')", ["eval()", "exec()"]),
    ]:
        check(f"{code!r} → {want}", dynamic_name_uses(code) == want)

    print("\ndynamic_name_uses negatives")
    for code in [
        "df.eval('a + b')",
        "obj.exec()",
        "setattr(obj, 'x', 1)",
        "delattr(self, 'x')",
        "x = 1\nprint(x)",
        "# exec('x')",
        "s = 'eval(1)'",
        "def broken(:",
    ]:
        check(f"{code!r} → []", dynamic_name_uses(code) == [])

    print("\nanalyze_cell 'dynamic' key")
    a = analyze_cell("code", "x = eval('1')")
    check("Python cell: dynamic = ['eval()']", a["dynamic"] == ["eval()"])
    check("existing keys unchanged", set(a) == {"reads", "writes", "mutated", "dynamic"}
          and a["writes"] == ["x"])
    check("plain cell: dynamic = []", analyze_cell("code", "x = 1")["dynamic"] == [])
    check("SQL cell: dynamic = []", analyze_cell("sql", "SELECT eval(1)", "q")["dynamic"] == [])

    print("\nderive_sql_output_variable (JS parity fixtures)")
    with open(CASES, encoding="utf-8") as fh:
        cases = json.load(fh)
    check("fixture has cases", len(cases) >= 10)
    for c in cases:
        got = derive_sql_output_variable(c["sql"])
        check(f"{c['sql']!r} → {c['expected']!r} (got {got!r})", got == c["expected"])
    check("None → 'result'", derive_sql_output_variable(None) == "result")

    print("\ncell_code_hash")
    check("SQL strip inside the hash", cell_code_hash("sql", "  SELECT 1\n") == cell_code_hash("sql", "SELECT 1"))
    check("Python whitespace significant", cell_code_hash("code", "x=1 ") != cell_code_hash("code", "x=1"))


def test_static_dynamic_names():
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
