#!/usr/bin/env python3
# test-kind: offline
"""
VM POST /variable-detail (and /param-options) on pathological pandas frames.

Bug: a DataFrame with DUPLICATE column names (pd.concat of frames sharing 'name',
or a rename onto an existing label) made /variable-detail 500 with
"'DataFrame' object has no attribute 'dtype'": _build read obj[col], which is a
DataFrame for a duplicated label. Columns are now read by position.

Offline: FastAPI TestClient over the REAL VM app (`app.server`, lifespan not run)
with the real SandboxExecutor. Covers duplicate labels, MultiIndex / int / tuple
labels, empty frames, all-NaN columns, duplicate index, extension dtypes,
Categorical, tz-aware datetimes, nested objects, an index named like a column or
like the Series, unhashable Series values, the Variables-panel action snippets
(Describe/Head/Plot/Profile/Info/Delete) on a duplicate-column frame, and that a
checkpoint pickle round-trip (nspickle) keeps labels/duplicates/dtypes as-is.

Run: PYTHONPATH=. .venv/bin/python tests/vm/test_variable_detail_pathological.py
Prints SKIP and exits 0 when duckdb is not installed (app.server imports it).
"""
import json
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _drop_stub_modules():
    for name in ("plotly.express", "plotly", "duckdb"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__spec__", None) is None and not hasattr(mod, "__file__"):
            del sys.modules[name]


SETUP = """
import matplotlib
matplotlib.use('Agg')
import pandas as pd, numpy as np
a = pd.DataFrame({'name': ['x', 'y'], 'category': ['c', 'd'], 'v': [1, 2]})
b = pd.DataFrame({'name': ['p', 'q'], 'category': ['e', 'f'], 'w': [3.0, np.nan]})
dup = pd.concat([a, b], axis=1)
dup_rename = a.rename(columns={'v': 'name'})
mi = pd.DataFrame(np.arange(6).reshape(2, 3),
                  columns=pd.MultiIndex.from_tuples([('a', 'x'), ('a', 'y'), ('b', 'x')]))
nonstr = pd.DataFrame({0: [1, 2], 1: ['a', 'b'], (2, 3): [1.5, 2.5]})
empty = pd.DataFrame()
empty_dup = pd.DataFrame(columns=['a', 'a'])
allnan = pd.DataFrame({'a': [np.nan] * 3, 'b': [None] * 3})
dupidx = pd.DataFrame({'a': [1, 2, 3]}, index=[0, 0, 1])
ext = pd.DataFrame({
    'i': pd.array([1, None, 3], dtype='Int64'),
    's': pd.array(['a', None, 'c'], dtype='string'),
    'cat': pd.Categorical(['u', 'v', 'u']),
    'tz': pd.date_range('2024-01-01', periods=3, tz='US/Eastern'),
    'nest': [{'k': 1}, [1, 2], None],
})
ext_dup = pd.concat([ext, ext], axis=1)
idx_clash = pd.DataFrame({'a': [1, 2], 'idx': [3, 4]}, index=pd.Index([5, 6], name='idx'))
s_clash = pd.Series([1, 2], name='k', index=pd.Index([1, 2], name='k'))
s_unhashable = pd.Series([{'a': 1}, [1]])
opts_dup = pd.DataFrame([['L1', 1, 'x'], ['L2', 2, 'y']], columns=['label', 'value', 'label'])
"""

ALL = ["dup", "dup_rename", "mi", "nonstr", "empty", "empty_dup", "allnan", "dupidx",
       "ext", "ext_dup", "idx_clash", "s_clash", "s_unhashable"]

# The Variables panel's DataFrame action buttons (VariablesPanel.jsx) insert these.
ACTIONS = {
    "Describe": "{n}.describe()",
    "Head": "{n}.head(10)",
    "Plot": "import matplotlib.pyplot as plt\n\n{n}.plot(figsize=(10, 5), title='{n}')\nplt.tight_layout()\nplt.show()",
    "Profile": ('print(f"Shape: {{{n}.shape}}")\nprint(f"\\nDtypes:\\n{{{n}.dtypes}}")\n'
                'print(f"\\nNull counts:\\n{{{n}.isnull().sum()}}")\n'
                'print(f"\\nMemory: {{{n}.memory_usage(deep=True).sum() / 1024:.1f}} KB")'),
    "Info": "{n}.info()",
}


def _run():
    from fastapi.testclient import TestClient
    _drop_stub_modules()
    import app.server as server
    logging.disable(logging.CRITICAL)
    client = TestClient(server.app)
    executor = server.app.state.executor
    client.post("/reset")
    try:
        r = executor.execute(SETUP, "setup")
        check("setup cell ran", getattr(r, "success", False))

        bodies = {}
        for n in ALL:
            resp = client.post("/variable-detail", json={"name": n})
            bodies[n] = resp.json()
            check(f"{n}: 200", resp.status_code == 200)
            check(f"{n}: no degraded note", "note" not in bodies[n])
            check(f"{n}: table payload", "table" in bodies[n])

        # The reported case: labels in order, duplicates kept, real per-column stats.
        d = bodies["dup"]
        cols = [s["column"] for s in d["schema"]]
        check("dup: schema keeps order + duplicates",
              cols == ["name", "category", "v", "name", "category", "w"])
        check("dup: per-position dtype", [s["display_dtype"] for s in d["schema"]][2] == "int")
        check("dup: per-position null_count (w has 1 NaN)", d["schema"][5]["null_count"] == 1)
        check("dup: per-position unique", d["schema"][0]["unique"] == 2)
        table = json.loads(d["table"])
        check("dup: grid columns keep duplicates", table["columns"] == cols)
        check("dup: grid cells per position", table["cells"][0][:4] == ["x", "c", "1", "p"])

        check("dup_rename: 3 columns, two 'name'",
              [s["column"] for s in bodies["dup_rename"]["schema"]] == ["name", "category", "name"])
        check("dup_rename: renamed column keeps its int dtype",
              bodies["dup_rename"]["schema"][2]["display_dtype"] == "int")
        check("mi: MultiIndex labels flattened like cell output",
              [s["column"] for s in bodies["mi"]["schema"]] == ["a_x", "a_y", "b_x"])
        check("mi: grid labels match schema", json.loads(bodies["mi"]["table"])["columns"] == ["a_x", "a_y", "b_x"])
        check("nonstr: labels are JSON strings",
              [s["column"] for s in bodies["nonstr"]["schema"]] == ["0", "1", "(2, 3)"])
        check("empty: empty schema", bodies["empty"]["schema"] == [])
        check("empty_dup: two 'a' columns", [s["column"] for s in bodies["empty_dup"]["schema"]] == ["a", "a"])
        check("allnan: 100% null", all(s["null_pct"] == 100.0 for s in bodies["allnan"]["schema"]))
        check("dupidx: 3 rows", bodies["dupidx"]["total_rows"] == 3)
        ext_schema = {s["column"]: s for s in bodies["ext"]["schema"]}
        check("ext: Int64 null counted", ext_schema["i"]["null_count"] == 1)
        check("ext: category dtype", ext_schema["cat"]["dtype"] == "category")
        check("ext: tz-aware dtype", "US/Eastern" in ext_schema["tz"]["dtype"])
        check("ext: nested objects → unique None, no failure", ext_schema["nest"]["unique"] is None)
        check("ext_dup: 10 columns", len(bodies["ext_dup"]["schema"]) == 10)
        check("idx_clash: named index shown although a column shares its name",
              json.loads(bodies["idx_clash"]["table"])["columns"] == ["idx", "a", "idx"])
        check("s_clash: index + value columns",
              json.loads(bodies["s_clash"]["table"])["columns"] == ["k", "k"])
        check("s_unhashable: unique None", bodies["s_unhashable"]["schema"][0]["unique"] is None)

        # Unknown name stays a 404.
        check("unknown variable → 404", client.post("/variable-detail", json={"name": "nope"}).status_code == 404)

        # A failure inside _build degrades to the repr (200 + note), never a 500.
        import app.notebook.routes as routes_mod
        saved = routes_mod._column_labels
        routes_mod._column_labels = lambda cols: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            resp = client.post("/variable-detail", json={"name": "dup"})
            j = resp.json()
            check("internal failure → 200 (not 500)", resp.status_code == 200)
            check("internal failure → repr text + note", "name" in j.get("text", "") and "RuntimeError" in j.get("note", ""))
        finally:
            routes_mod._column_labels = saved

        # /param-options on a frame with a duplicated 'label' column.
        resp = client.post("/param-options", json={"name": "opts_dup"})
        check("param-options dup labels: 200", resp.status_code == 200)
        check("param-options dup labels: first 'label' + 'value' used",
              [o["label"] for o in resp.json().get("options", [])] == ["L1", "L2"]
              and [o["value"] for o in resp.json().get("options", [])] == [1, 2])

        # Other Variables-panel paths.
        check("/variables 200", client.get("/variables").status_code == 200)
        check("/variables/schema 200", client.get("/variables/schema").status_code == 200)
        check("/variables/inspect dup 200",
              client.post("/variables/inspect", json={"name": "dup"}).status_code == 200)

        # Action snippets on duplicate-column frames.
        for n in ("dup", "ext_dup", "mi"):
            for label, tpl in ACTIONS.items():
                r = executor.execute(tpl.format(n=n), f"act_{n}_{label}")
                check(f"{label} on {n} runs", getattr(r, "success", False))
        r = executor.execute("del dup_rename", "act_delete")
        check("Delete (del name) runs", getattr(r, "success", False)
              and "dup_rename" not in executor._namespace)

        # Checkpoint pickling keeps labels, duplicates and dtypes (restore does not
        # create or remove duplicate columns).
        from app.platform.checkpoint import nspickle
        ns = executor._namespace
        for n in ("dup", "mi", "nonstr", "ext", "ext_dup", "idx_clash", "s_clash"):
            obj = ns[n]
            back = nspickle.loads(nspickle.dumps(obj, ns), ns)
            if hasattr(obj, "columns"):
                same = (list(back.columns) == list(obj.columns)
                        and list(map(str, back.dtypes)) == list(map(str, obj.dtypes))
                        and back.columns.duplicated().sum() == obj.columns.duplicated().sum())
            else:
                same = back.name == obj.name and str(back.dtype) == str(obj.dtype)
            check(f"pickle round-trip keeps {n} labels/dtypes", same and back.equals(obj))
    finally:
        client.post("/reset")


def main():
    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("SKIP: duckdb not installed")
        return 0
    print("variable-detail on pathological frames")
    _run()
    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
