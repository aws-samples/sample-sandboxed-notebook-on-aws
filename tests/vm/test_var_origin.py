#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: orphan ("no producing cell") classification of variables and the
user-confirmed clean-up routes (app/notebook/var_inspect.classify_origin, the
orphan / orphan_reason fields on /variables, /variables/redacted,
/variables/schema and /variables/inspect, DagStore.cell_ids(),
GET /variables/untracked and POST /variables/delete).

Standalone (no pytest, no AWS):
    .venv/bin/python tests/vm/test_var_origin.py
Exits non-zero on any failure.
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
import logging  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.notebook import var_inspect, routes  # noqa: E402
from app.notebook.dag_store import DagStore  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402

logging.disable(logging.WARNING)

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


# ─── classify_origin ──────────────────────────────────────────────────────────
print("\nclassify_origin")
co = var_inspect.classify_origin
P = lambda d: {"defined_by": d, "defined_at": 1, "modified_by": [], "last_cell": d, "last_exec": 1}  # noqa: E731
check("no provenance → unknown", co("v", None, {"c1"}) == (True, "unknown"))
check("non-dict provenance → unknown", co("v", "junk", None) == (True, "unknown"))
check("defined_by '' without a writer marker (legacy) → no cell",
      co("dynamodb_sentiment", P(""), {"c1"}) == (True, "no cell"))
check("defined_by '' + writer 'user' (UI / API) → no cell",
      co("v", {**P(""), "writer": "user"}, {"c1"}) == (True, "no cell"))
check("defined_by '' + writer 'agent' → agent",
      co("v", {**P(""), "writer": "agent"}, {"c1"}) == (True, "agent"))
check("'_x' + writer 'agent' → still internal", co("_df", {**P(""), "writer": "agent"}, None) == (True, "internal"))
check("'_x' + defined_by '' → internal", co("_df", P(""), None) == (True, "internal"))
check("defined_by None → unknown", co("v", P(None), {"c1"}) == (True, "unknown"))
check("deleted cell (cell_ids known)", co("v", P("gone"), {"c1"}) == (True, "deleted cell"))
check("live cell → not orphan", co("v", P("c1"), {"c1"}) == (False, None))
check("cell_ids None → never 'deleted cell'", co("v", P("gone"), None) == (False, None))
check("non-str cell id compared as str", co("v", P(7), {"7"}) == (False, None))

# ─── DagStore.cell_ids ────────────────────────────────────────────────────────
print("\nDagStore.cell_ids")
ds = DagStore()
check("no document → None", ds.cell_ids() is None)
ds.put({"schema": 1, "version": 1, "nodes": [{"cell_id": "c1"}, {"cell_id": 2}, {"x": 1}],
        "edges": [], "issues": []})
check("node cell ids as strings", ds.cell_ids() == {"c1", "2"})
ds.put({"schema": 1, "nodes": "bad"})
check("nodes not a list → None", ds.cell_ids() is None)


# ─── Routes ───────────────────────────────────────────────────────────────────
def make():
    ex = SandboxExecutor()
    ex.execute("keep = 1", cell_id="c1")
    ex.execute("gone = [1, 2]", cell_id="c-gone")
    ex.execute("orph = {1, 2, 3}")           # no cell id: a UI / API write (not an agent)
    ex.execute("_df = 5")                    # profiler-style internal leftover
    ex._namespace["noprov"] = 7              # no provenance at all
    ex._var_provenance["__upload_df_1"] = P("")  # provenance-only upload ghost
    app = FastAPI()
    app.include_router(routes.router)
    app.state.executor = ex
    app.state.session_state = {"request_count": 0}
    store = DagStore()
    store.put({"schema": 1, "version": 1, "nodes": [{"cell_id": "c1"}], "edges": [], "issues": []})
    app.state.dag_store = store
    return ex, app, TestClient(app)


print("\n/variables payloads")
ex, app, client = make()
check("no-cell execute stamps writer 'user'", ex._var_provenance["orph"].get("writer") == "user")
check("a cell write carries no writer marker", "writer" not in ex._var_provenance["keep"])
ex.set_variable("ui_set", 3)
check("set_variable without a cell id stamps writer 'user' → 'no cell'",
      ex._var_provenance["ui_set"].get("writer") == "user"
      and var_inspect.classify_origin("ui_set", ex._var_provenance["ui_set"], {"c1"}) == (True, "no cell"))
ex._namespace.pop("ui_set", None)
ex._var_provenance.pop("ui_set", None)
for path in ("/variables", "/variables/redacted"):
    v = client.get(path).json()["variables"]
    check(f"{path}: cell variable unflagged with its producer",
          v["keep"]["orphan"] is False and v["keep"]["orphan_reason"] is None
          and v["keep"]["defined_by"] == "c1")
    check(f"{path}: deleted cell flagged, defined_by null, last_cell raw",
          v["gone"]["orphan"] is True and v["gone"]["orphan_reason"] == "deleted cell"
          and v["gone"]["defined_by"] is None and v["gone"]["last_cell"] == "c-gone")
    check(f"{path}: no-cell run flagged 'no cell' (not agent)", v["orph"]["orphan"] is True
          and v["orph"]["orphan_reason"] == "no cell" and v["orph"]["defined_by"] is None)
    check(f"{path}: no provenance flagged unknown", v["noprov"]["orphan_reason"] == "unknown")
sch = {e["name"]: e for e in client.get("/variables/schema").json()["variables"]}
check("/variables/schema: orphan fields",
      sch["gone"]["orphan"] is True and sch["gone"]["orphan_reason"] == "deleted cell"
      and sch["gone"]["defined_by"] is None and sch["keep"]["orphan"] is False
      and sch["keep"]["defined_by"] == "c1" and sch["orph"]["orphan_reason"] == "no cell")
ins = client.post("/variables/inspect", json={"name": "gone", "sample_rows": 3}).json()
check("/variables/inspect: orphan fields",
      ins["orphan"] is True and ins["orphan_reason"] == "deleted cell" and ins["defined_by"] is None)
ins = client.post("/variables/inspect", json={"name": "keep", "sample_rows": 3}).json()
check("/variables/inspect: cell variable unflagged", ins["orphan"] is False and ins["defined_by"] == "c1")
app.state.dag_store = DagStore()
v = client.get("/variables").json()["variables"]
check("no DAG document: a cell variable is never 'deleted cell'",
      v["gone"]["orphan"] is False and v["gone"]["defined_by"] == "c-gone")
app.state.dag_store = None
check("dag_store None: routes still work", client.get("/variables").status_code == 200)

print("\n/variables/untracked + /variables/delete")
ex, app, client = make()
u = client.get("/variables/untracked").json()
vis = {e["name"]: e["reason"] for e in u["visible"]}
check("untracked visible = deleted cell + no cell + unknown",
      vis == {"gone": "deleted cell", "orph": "no cell", "noprov": "unknown"}, str(vis))
check("untracked internal = _df + __upload_ ghost",
      set(u["internal"]) == {"_df", "__upload_df_1"}, str(u["internal"]))
clock0 = ex._prov_clock
d = client.post("/variables/delete",
                json={"names": ["gone", "orph", "noprov", "_df", "__upload_df_1", "keep"]})
body = d.json()
check("delete 200", d.status_code == 200)
check("deletes exactly the flagged names",
      sorted(body["deleted"]) == sorted(["gone", "orph", "noprov", "_df", "__upload_df_1"]))
check("refuses a cell-produced name (skipped)", body["skipped"] == ["keep"]
      and ex._namespace.get("keep") == 1 and "keep" in ex._var_provenance)
check("namespace + provenance no longer hold them",
      not ({"gone", "orph", "noprov", "_df"} & set(ex._namespace))
      and not ({"gone", "orph", "_df", "__upload_df_1"} & set(ex._var_provenance)))
check("clock bumped exactly once", ex._prov_clock == clock0 + 1)
check("returns runtime without the deleted names",
      isinstance(body.get("runtime"), dict) and body["runtime"]["clock"] == clock0 + 1
      and "gone" not in body["runtime"]["vars"] and "keep" in body["runtime"]["vars"])
clock1 = ex._prov_clock
d = client.post("/variables/delete", json={"names": ["keep", "missing"]}).json()
check("nothing deletable → nothing deleted, clock unchanged",
      d["deleted"] == [] and d["skipped"] == ["keep", "missing"] and ex._prov_clock == clock1)
check("invalid bodies → 400",
      client.post("/variables/delete", json={"names": "keep"}).status_code == 400
      and client.post("/variables/delete", json={"names": [1]}).status_code == 400
      and client.post("/variables/delete", json=[1]).status_code == 400)
u = client.get("/variables/untracked").json()
check("untracked empty after clean-up", u == {"visible": [], "internal": []}, str(u))

print()
if _fails:
    print(f"FAILED: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
