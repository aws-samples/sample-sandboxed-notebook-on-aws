#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: AI-agent / internal probes run in a SCRATCH overlay, never in the
user namespace (app/notebook/scratch.py, SandboxExecutor.execute(scratch=...),
/execute origin "agent" + agent_scope / "internal", /execute-sql agent runs that
never bind, the /variables/scratch routes and the upload probe).

Covers: a throwaway probe leaves nothing; a rebind keeps the user binding; reads
of user DataFrames work; in-place mutation DOES reach the user object (the
documented limit); result fields equal a normal run on a twin executor; `global`,
probe-defined functions and import aliases stay in scratch; same-scope chaining,
scope isolation, clear, LRU cap and TTL; provenance / clock / run ledger / import
ledger untouched; checkpoint dirty flag and save filter; normal cells unchanged;
SQL bind_output=False; the executor lock still covers a probe; the HTTP routes
(agent chain, internal throwaway, no-origin persists, profiler code, upload).

Run with python3 (needs duckdb + pandas + fastapi):
    python3 tests/vm/test_agent_scratch_exec.py
Exits non-zero on any failure.
"""
import base64
import os
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
# Stub plotly when absent (same as tests/vm/test_var_inspect.py).
try:
    import plotly  # noqa: F401
except ImportError:
    _plotly = types.ModuleType("plotly")
    _px = types.ModuleType("plotly.express")
    _plotly.express = _px
    sys.modules.setdefault("plotly", _plotly)
    sys.modules.setdefault("plotly.express", _px)

import logging  # noqa: E402
import pandas as pd  # noqa: E402
from app.notebook.executor import SandboxExecutor, RuntimeSectionBusy  # noqa: E402
from app.notebook.scratch import ScratchScopes, valid_scope  # noqa: E402

logging.disable(logging.WARNING)

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


def make_executor():
    ex = SandboxExecutor()
    ex.execute("import pandas as pd\norders = pd.DataFrame({'id': [1, 2, 3]})\n"
               "lst = [1, 2]\nx = 10", cell_id="c-orders")
    return ex


def state(ex):
    import copy
    return (copy.deepcopy(ex._var_provenance), ex._prov_clock, copy.deepcopy(ex._runs),
            dict(ex._imports))


def user_names(ex):
    return {k for k in ex._namespace if not k.startswith("__")}


# ─── ScratchScopes ────────────────────────────────────────────────────────────
print("\nScratchScopes")
check("valid_scope accepts chat:<sid>", valid_scope("chat:75535d90-2166-4fa0"))
check("valid_scope rejects spaces/empty/non-str/too long",
      not valid_scope("a b") and not valid_scope("") and not valid_scope(None)
      and not valid_scope(5) and not valid_scope("x" * 129) and valid_scope("x" * 128))
s = ScratchScopes()
a = s.get("a")
a["v"] = 1
check("get returns the same dict for a scope", s.get("a") is a and s.peek("a") == {"v": 1})
check("peek of unknown scope is None and does not create", s.peek("zz") is None and len(s) == 1)
check("clear drops a scope", s.clear("a") is True and s.peek("a") is None and s.clear("a") is False)
lru = ScratchScopes(max_scopes=8)
for i in range(8):
    lru.get(f"s{i}")["i"] = i
lru.get("s0")  # touch: s1 is now the oldest
lru.get("s8")
check("cap 8: the least recently used scope is evicted",
      len(lru) == 8 and lru.peek("s1") is None and lru.peek("s0") == {"i": 0}
      and lru.peek("s8") == {})
clock = [1000.0]
ttl = ScratchScopes(ttl_s=1800, now=lambda: clock[0])
ttl.get("idle")["a"] = 1
ttl.get("busy")
clock[0] += 1000
ttl.get("busy")
clock[0] += 900  # idle: 1900 s idle > TTL; busy: 900 s
check("TTL evicts an idle scope, keeps a recently used one",
      ttl.peek("idle") is None and ttl.peek("busy") == {})
lru.clear_all()
check("clear_all empties", len(lru) == 0)

# ─── Executor: throwaway probes ───────────────────────────────────────────────
print("\nExecutor scratch runs")
ex = make_executor()
before_names = user_names(ex)
before_state = state(ex)
r = ex.execute("probe_x = 1\nprint(len(orders))", scratch={})
check("probe reads a user DataFrame", r.success and r.output == "3\n", r.output + r.error)
check("throwaway probe leaves no new name", "probe_x" not in ex._namespace)
r = ex.execute("x = 99\norders = None", scratch={})
check("rebind in a probe keeps the user binding",
      r.success and ex._namespace["x"] == 10 and isinstance(ex._namespace["orders"], pd.DataFrame))
r = ex.execute("del x", scratch={})
check("del in a probe keeps the user binding", r.success and ex._namespace.get("x") == 10)
r = ex.execute("lst.append(3)\norders['new'] = 1", scratch={})
check("in-place mutation DOES reach the user object (documented limit)",
      r.success and ex._namespace["lst"] == [1, 2, 3] and "new" in ex._namespace["orders"].columns)
ex._namespace["orders"].drop(columns=["new"], inplace=True)
ex._namespace["lst"].pop()
r = ex.execute("def helper(n):\n    global g\n    g = n * 2\n    return g\nprint(helper(len(orders)))",
               scratch={})
check("probe-defined function + global work inside the probe",
      r.success and r.output == "6\n" and "g" not in ex._namespace and "helper" not in ex._namespace)
sc = {}
r = ex.execute("import json as jj\nimport collections", scratch=sc)
check("import alias stays in the scratch",
      r.success and "jj" in sc and "collections" in sc and "jj" not in ex._namespace
      and "jj" not in ex._imports and "collections" not in ex._imports)
check("display shim / __builtins__ never copied to scratch",
      "display" not in sc and "__builtins__" not in sc)
check("user names unchanged after the probes", user_names(ex) == before_names,
      str(user_names(ex) ^ before_names))
check("provenance, clock, run ledger and imports unchanged by probes", state(ex) == before_state)
r = ex.execute("print(1)", cell_id="c-probe", scratch={})
check("a probe with a cell_id still records no run", "c-probe" not in ex._runs)
check("display shim of a cell untouched by probes",
      ex._display_fn is not None and ex._namespace.get("display") is ex._display_fn)

# ─── Same-scope chaining ──────────────────────────────────────────────────────
print("\nScope chaining")
s1 = ex.scratch_scope("chat:t1")
r1 = ex.execute("a = len(orders)", scratch=s1)
r2 = ex.execute("print(a+1)", scratch=ex.scratch_scope("chat:t1"))
check("same scope chains: a = len(orders) then print(a+1) → 4",
      r1.success and r2.success and r2.output == "4\n", r2.output + r2.error)
r3 = ex.execute("print(a)", scratch=ex.scratch_scope("chat:other"))
check("a different scope cannot see it", not r3.success and "NameError" in r3.error)
r = ex.execute("x = x + 1", scratch=s1)
check("a scope's rebind shadows only within the scope",
      ex._namespace["x"] == 10 and s1["x"] == 11)
r = ex.execute("print(x)", scratch=s1)
check("chained probe sees the scope's rebind", r.output == "11\n")
r = ex.execute("del x", scratch=s1)
r2 = ex.execute("print(x)", scratch=s1)
check("del in a scope removes the scratch name; the user's value shows again",
      "x" not in s1 and r2.output == "10\n")
check("scratch_variables lists the scope's names labelled",
      set(ex.scratch_variables("chat:t1")) == {"a"}
      and ex.scratch_variables("chat:t1")["a"]["scratch"] is True
      and ex.scratch_variables("chat:t1")["a"]["orphan"] is False)
check("scope names absent from get_variables / schema",
      "a" not in ex.get_variables()
      and "a" not in {e["name"] for e in ex.get_variable_schemas()["variables"]})
st, body = ex.inspect_variable("a", scope="chat:t1")
check("inspect_variable in scope resolves scratch", st == 200 and body.get("scratch") is True)
st, _ = ex.inspect_variable("a")
check("inspect_variable without scope does not see scratch", st == 404)
check("clear_scratch drops the scope", ex.clear_scratch("chat:t1") is True
      and ex.scratch_variables("chat:t1") == {})
r = ex.execute("print(a)", scratch=ex.scratch_scope("chat:t1"))
check("after clear the name is gone", not r.success)
check("still no user-ns change after the turn", user_names(ex) == before_names)
ex.reset()
check("reset() clears every scope", len(ex._scratch) == 0)

# ─── Result fields identical to a normal run (twin executors) ─────────────────
print("\nResult parity probe vs cell")
FIELDS = ("success", "output", "error", "error_lineno", "traceback", "error_frames",
          "error_locals", "html", "image", "variables_created", "variables_read",
          "variables_mutated", "variables_rebound", "sources_read")
CODES = {
    "print + df expression": "print('hi')\nimport sys\nsys.stderr.write('warn\\n')\norders.head(2)",
    "runtime error": "y = 1\nz = orders['nope']",
    "syntax error": "def (:",
    "zero division": "q = 1/0",
    "rebind + mutate": "x = x + 1\nlst.append(9)",
}
for label, code in CODES.items():
    ea, eb = make_executor(), make_executor()
    ra = ea.execute(code)
    rb = eb.execute(code, scratch={})
    diffs = [f for f in FIELDS if getattr(ra, f) != getattr(rb, f)]
    check(f"parity: {label}", not diffs, f"differs: {diffs}")
    check(f"parity: {label}: timing/peaks present",
          isinstance(rb.execution_time_ms, float) and rb.peak_mem_mb is not None)

# ─── Checkpoint dirty flag + save filter ──────────────────────────────────────
print("\nCheckpoint")
from app.platform.checkpoint.manager import CheckpointManager  # noqa: E402
from app.platform.checkpoint.namespace_filter import _filter_namespace_for_save  # noqa: E402
ex = make_executor()
cm = CheckpointManager(ex, {})
cm._saved_fingerprint = cm._current_fingerprint()
scope = ex.scratch_scope("fix:abc")
ex.execute("probe_only = orders.copy()\nprobe_n = 5", scratch=scope)
check("after a probe the session is NOT dirty", cm.is_dirty() is False)
saved, _skipped = _filter_namespace_for_save(ex._namespace)
check("save filter holds no scratch names",
      "probe_only" not in saved and "probe_n" not in saved and "orders" in saved)
r = ex.execute("cell_var = 1", cell_id="c-new")
check("after a normal cell run the session IS dirty", cm.is_dirty() is True)
check("normal cell persists and stamps provenance",
      ex._namespace.get("cell_var") == 1
      and ex._var_provenance["cell_var"]["defined_by"] == "c-new" and "c-new" in ex._runs)

# ─── SQL agent run never binds ────────────────────────────────────────────────
print("\nSQL")
from app.notebook.sql_engine import _run_sql_cell_blocking  # noqa: E402
ex = make_executor()
clock0 = ex._prov_clock
res = _run_sql_cell_blocking(ex, None, "SELECT 1 AS a", "_fix_test_result", None,
                             capture_runtime=True, bind_output=False)
check("bind_output=False succeeds with row_count 1",
      res.get("success") is True and res.get("row_count") == 1, str(res.get("error")))
check("bind_output=False binds nothing, no provenance write",
      "_fix_test_result" not in ex._namespace and "_fix_test_result" not in ex._var_provenance
      and ex._prov_clock == clock0)
check("bind_output=False response still echoes output_variable",
      res.get("output_variable") == "_fix_test_result" and "runtime" in res)
res = _run_sql_cell_blocking(ex, None, "SELECT * FROM orders", "out_df", "c-sql", bind_output=True)
check("bind_output=True (cells) still binds + records the run",
      res.get("success") is True and "out_df" in ex._namespace and "c-sql" in ex._runs)

# ─── Lock covers a probe ──────────────────────────────────────────────────────
print("\nLock")
ex = make_executor()
t = threading.Thread(target=ex.execute, args=("import time\ntime.sleep(0.3)",),
                     kwargs={"scratch": {}})
t.start()
time.sleep(0.05)
busy = False
try:
    ex.runtime_section(lock_timeout=0.05)
except RuntimeSectionBusy:
    busy = True
t.join()
check("runtime_section(lock_timeout=0.05) raises RuntimeSectionBusy during a probe", busy)

# ─── HTTP routes ──────────────────────────────────────────────────────────────
print("\nRoutes")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.notebook import code_engine, routes  # noqa: E402
from app.notebook.dag_store import DagStore  # noqa: E402


class _Catalog:
    def refresh_local_files(self):
        pass


app = FastAPI()
app.include_router(code_engine.router)
app.include_router(routes.router)
ex = make_executor()
app.state.executor = ex
app.state.session_state = {"request_count": 0}
app.state.dag_store = DagStore()
app.state.data_catalog = _Catalog()
client = TestClient(app)

b1 = client.post("/execute", json={"code": "a = len(orders)", "origin": "agent",
                                   "agent_scope": "chat:live-test"}).json()
b2 = client.post("/execute", json={"code": "print(a+1)", "origin": "agent",
                                   "agent_scope": "chat:live-test"}).json()
check("route: agent scope chains → 4", b1["success"] and b2["output"] == "4\n", str(b2))
check("route: agent response keeps the shape (runtime + variables_created)",
      "runtime" in b1 and b1["variables_created"] == ["a"])
vs = client.get("/variables").json()["variables"]
schema = client.get("/variables/schema").json()["variables"]
check("route: /variables and /variables/schema exclude scratch",
      "a" not in vs and "a" not in {e["name"] for e in schema} and "orders" in vs)
scr = client.post("/variables/scratch", json={"scope": "chat:live-test"}).json()
check("route: /variables/scratch lists it labelled",
      scr["count"] == 1 and scr["variables"]["a"]["scratch"] is True, str(scr))
check("route: /variables/scratch 400 on invalid scope",
      client.post("/variables/scratch", json={"scope": "bad scope"}).status_code == 400
      and client.post("/variables/scratch/clear", json={}).status_code == 400)
ins = client.post("/variables/inspect", json={"name": "a", "sample_rows": 3,
                                               "scope": "chat:live-test"})
check("route: inspect with scope sees the scratch name",
      ins.status_code == 200 and ins.json().get("scratch") is True)
cl = client.post("/variables/scratch/clear", json={"scope": "chat:live-test"}).json()
check("route: clear → cleared true, then empty",
      cl == {"cleared": True}
      and client.post("/variables/scratch", json={"scope": "chat:live-test"}).json()["count"] == 0
      and client.post("/variables/scratch/clear", json={"scope": "chat:live-test"}).json()
      == {"cleared": False})
b = client.post("/execute", json={"code": "print(a)", "origin": "agent",
                                  "agent_scope": "chat:live-test"}).json()
check("route: name gone after clear", b["success"] is False)
b = client.post("/execute", json={"code": "old_client = 1", "origin": "agent"}).json()
check("route: agent without scope = throwaway", b["success"] and "old_client" not in ex._namespace)
b = client.post("/execute", json={"code": "internal_v = 1\n_hidden = 2", "origin": "internal"}).json()
check("route: internal leaves nothing",
      b["success"] and "internal_v" not in ex._namespace and "_hidden" not in ex._namespace
      and "internal_v" not in ex._var_provenance)
b = client.post("/execute", json={"code": "kept = 1", "cell_id": "c-kept"}).json()
check("route: no origin persists (a cell)", b["success"] and ex._namespace.get("kept") == 1
      and ex._var_provenance["kept"]["defined_by"] == "c-kept")
b = client.post("/execute", json={"code": "kept2 = 2"}).json()
check("route: no origin + no cell id still persists (unchanged)", ex._namespace.get("kept2") == 2)

# Profiler code (batch/entity_discovery) as internal leaves nothing.
from batch.entity_discovery import _build_local_profile_code  # noqa: E402
csv_path = "/tmp/_agent_scratch_test_profile.csv"
pd.DataFrame({"k": [1, 2, 2], "v": ["a", "b", None]}).to_csv(csv_path, index=False)
names_before = set(ex._namespace)
prov_before = set(ex._var_provenance)
try:
    b = client.post("/execute", json={"code": _build_local_profile_code(csv_path, 100),
                                      "origin": "internal"}).json()
finally:
    os.remove(csv_path)
check("profiler as internal: success with the stats marker",
      b["success"] and "__ENTITY_STATS_JSON__" in b["output"], b.get("error", ""))
check("profiler as internal leaves no _df/_stats/_pd",
      set(ex._namespace) == names_before and set(ex._var_provenance) == prov_before
      and "_df" not in ex._namespace and "_stats" not in ex._namespace)

# Upload probe goes through scratch.
names_before = set(ex._namespace)
clock_before = ex._prov_clock
up_name = "_agent_scratch_test_upload.csv"
data = base64.b64encode(b"c1,c2\n1,2\n3,4\n").decode()
try:
    u = client.post("/upload", json={"filename": up_name, "data": data}).json()
finally:
    try:
        os.remove(os.path.join("/tmp", up_name))
    except OSError:
        pass
check("upload succeeds with the shape", u.get("success") is True and u.get("shape") == "(2, 2)", str(u))
check("upload probe leaves no __upload_* in namespace or provenance, clock unchanged",
      set(ex._namespace) == names_before
      and not any(k.startswith("__upload_") for k in ex._var_provenance)
      and ex._prov_clock == clock_before)

print()
if _fails:
    print(f"FAILED: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
