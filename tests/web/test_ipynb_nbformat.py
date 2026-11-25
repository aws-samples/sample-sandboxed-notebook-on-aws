#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: the web app's .ipynb export (web/src/services/notebookExport.js
buildIPYNB) is a valid nbformat 4.5 notebook.

Schema validation uses the official Jupyter validator (nbformat.validate, which
ships the nbformat JSON schemas), so no third-party schema file is vendored here.

Covers: nbformat schema validation, unique nbformat cell ids, SQL cells as code
cells with a %%sql first line, our extras under metadata.microvm_notebook, and no
secret/env value from the fixture's poison fields.

Needs node (the exporter is the real web module) and nbformat, a test-only
dependency pinned in tests/requirements-dev.txt:
    uv pip install --python .venv/bin/python -r tests/requirements-dev.txt
    .venv/bin/python tests/web/test_ipynb_nbformat.py
Exits non-zero on any failure. Without nbformat: exits 0 with SKIP (pytest: skipped).
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "export_ipynb_fixture.mjs")
POISON = "SECRET_VALUE_X"

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


def _export(*args):
    out = subprocess.run(["node", FIXTURE, *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout


def _nbformat():
    try:
        import nbformat
    except ImportError:
        return None
    return nbformat


def _run(nbformat):
    raw = _export()
    nb = json.loads(raw)
    try:
        nbformat.validate(nbformat.reads(raw, as_version=4))
        check("nbformat.validate (official nbformat schema)", True)
    except Exception as e:  # noqa: BLE001
        check("nbformat.validate (official nbformat schema)", False, str(e))
    check("nbformat 4 / minor 5", nb.get("nbformat") == 4 and nb.get("nbformat_minor") == 5)

    ids = [c.get("id") for c in nb["cells"]]
    check("every cell has an id", all(ids), str(ids))
    check("cell ids are unique", len(set(ids)) == len(ids), str(ids))

    sql = [c for c in nb["cells"] if (c.get("metadata", {}).get("microvm_notebook") or {}).get("type") == "sql"]
    check("SQL cells are code cells starting with %%sql",
          bool(sql) and all(c["cell_type"] == "code" and c["source"][0].startswith("%%sql") for c in sql))
    check("notebook extras under metadata.microvm_notebook",
          nb["metadata"].get("microvm_notebook", {}).get("name") == "Export Fixture")
    check("every cell carries metadata.microvm_notebook.id",
          all((c.get("metadata", {}).get("microvm_notebook") or {}).get("id") for c in nb["cells"]))
    check("no secret / env value in the export", POISON not in raw)
    check("error output has ename/evalue/traceback",
          any(o.get("output_type") == "error" and o.get("ename") == "ValueError"
              for c in nb["cells"] for o in c.get("outputs", [])))


def _run_multi(nbformat):
    """A cell with several tables / charts / images (outputs envelope): one nbformat
    output per item, in order, and the whole notebook still validates."""
    raw = _export("multi")
    nb = json.loads(raw)
    try:
        nbformat.validate(nbformat.reads(raw, as_version=4))
        check("multi-output notebook: nbformat.validate", True)
    except Exception as e:  # noqa: BLE001
        check("multi-output notebook: nbformat.validate", False, str(e))
    cell = next(c for c in nb["cells"] if (c["metadata"].get("microvm_notebook") or {}).get("id") == "cell-py-multi")
    kinds = []
    for o in cell["outputs"]:
        if o["output_type"] == "stream":
            kinds.append("stream")
        elif "image/png" in o["data"]:
            kinds.append("png")
        else:
            kinds.append(o["data"]["text/plain"][0])
    check("multi-output cell: one output per item, in order",
          kinds == ["stream", "<DataFrame>", "stream", "<DataFrame>", "<DataFrame>", "<Plotly figure>",
                    "<Plotly figure>", "png", "png", "png", "stream"], str(kinds))
    check("multi-output cell: 3 image/png outputs (one per image item)",
          len([o for o in cell["outputs"] if "image/png" in o.get("data", {})]) == 3)
    err = next(c for c in nb["cells"] if (c["metadata"].get("microvm_notebook") or {}).get("id") == "cell-py-multi-err")
    check("error after displays: tables first, error output last",
          [o["output_type"] for o in err["outputs"]] == ["stream", "display_data", "stream", "display_data", "error"])
    check("no base64 image copy inside the cell's metadata extras",
          "data:image" not in json.dumps(cell["metadata"]))
    check("no secret / env value in the multi-output export", POISON not in raw)
SKIP_MSG = "nbformat not installed (uv pip install -r tests/requirements-dev.txt)"


def test_main():
    nbf = _nbformat()
    if nbf is None:
        import pytest
        pytest.skip(SKIP_MSG)
    _fails.clear()
    _run(nbf)
    assert not _fails, _fails


if __name__ == "__main__":
    nbf = _nbformat()
    if nbf is None:
        print(f"SKIP  {SKIP_MSG}")
        sys.exit(0)
    _run(nbf)
    _run_multi(nbf)
    print()
    if _fails:
        print(f"FAILED: {len(_fails)}")
        for f in _fails:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
