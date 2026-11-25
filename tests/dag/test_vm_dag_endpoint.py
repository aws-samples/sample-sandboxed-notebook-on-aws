#!/usr/bin/env python3
# test-kind: offline
"""
VM side of the notebook DAG (design §5.2-§5.5, §12 test_vm_dag_endpoint).

Offline: FastAPI TestClient over the REAL VM app (`app.server`, imported without
running its lifespan — no hooks, no AWS), with a real SandboxExecutor.

Covers:
  • PUT/GET /dag: 403 without `X-Dag-Writer: proxy` (both verbs), 400 validation
    cases, 413 at DAG_MAX_BYTES + 1 (declared and streamed), origin empty → pushed,
    runtime/pending/checkpoint discarded on PUT; PUT validates runtime_version and
    keeps the stamps.
  • PATCH /dag (delta): 403 without the marker, 413 over the cap, 400 validation,
    409 no_document / base_mismatch (with the VM's rev) / hash_mismatch (doc
    unchanged), 200 → GET doc == apply_delta(PUT doc, delta) (a full document),
    runtime/pending in the delta ignored, replay → 409; a restored dag.json copy
    (with its `checkpoint` stamp) drops the stamp from the VM copy, reports it in
    GET /dag, and the first PATCH after the restore → 200.
  • /execute carries runtime with runs[cell_id].code_hash == cell_code_hash
    ('code', code) and variables_rebound (incl. AugAssign); /execute-sql carries
    runtime on success and error; /set-variable and /reset carry runtime.
  • /execute-batch: exactly one runtime frame, after the last cell_result and
    before done; no cell_result.result has a 'runtime' key.
  • Thread safety: runtime_section concurrent with worker-thread execute (16
    iterations) and with record_var_write/set_variable (2000 iterations).
  • GET /dag while another thread holds _exec_lock → 503 {"busy": true} in ~5 s.

Run (system python3 — needs duckdb/dill/pandas):
    python3 tests/dag/test_vm_dag_endpoint.py
"""
import json
import logging
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


PROXY = {"X-Dag-Writer": "proxy"}


def _drop_stub_modules():
    """Other suites (test_cell_vars_filter, test_static_mutation, test_lineage_sources)
    install bare ModuleType stubs for plotly/duckdb at import time; in a shared
    pytest session those would shadow the real packages app.server needs."""
    for name in ("plotly.express", "plotly", "duckdb"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__spec__", None) is None and not hasattr(mod, "__file__"):
            del sys.modules[name]


def _vm_app():
    """The real VM FastAPI app (lifespan not run by a bare TestClient)."""
    _drop_stub_modules()
    import app.server as server
    logging.disable(logging.WARNING)  # app.server configures DEBUG logging; keep output readable
    return server.app


def _sse_frames(text):
    frames = []
    for line in text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: "):]))
    return frames


def _doc(**over):
    d = {"schema": 1, "version": 3, "nodes": [], "edges": [], "issues": []}
    d.update(over)
    return d


def _stamped_pair():
    """Two successive stamped builder docs (prev → next: an upstream re-run)."""
    from proxy.notebook import dag
    from proxy.notebook.dag import RunCapture
    from proxy.storage._dag_doc import stamp_doc
    now = "2026-05-01T10:00:00.000Z"
    cells = [{"id": "a", "type": "code", "code": "x = 1"}, {"id": "b", "type": "code", "code": "y = x + 1"}]
    runs = [RunCapture("a", "code", "x = 1", None, True, (), ("x",), (), (), None, now),
            RunCapture("b", "code", "y = x + 1", None, True, ("x",), ("y",), (), (), None, now)]
    d0 = dag.apply_runs(dag.apply_cells(None, cells, trigger="open", now=now), runs,
                        {"clock": 2, "vars": {}, "runs": {}}, "vm", trigger="execute", now=now)
    prev = stamp_doc(None, d0, 0, 1, now)[0]
    d1 = dag.apply_runs(prev, runs[:1], {"clock": 3, "vars": {"x": {"defined_by": "a"}}, "runs": {}}, "vm",
                        trigger="execute", now=now)
    nxt = stamp_doc(prev, d1, prev["version"], 1, "2026-05-01T10:00:01.000Z")[0]
    return dag.strip_for_vm(prev), dag.strip_for_vm(nxt), prev, nxt


def _patch_checks(app, client):
    import copy
    from app.notebook.dag_contract import DAG_MAX_BYTES, apply_delta, diff_docs
    from app.notebook.dag_store import DagStore
    print("\nVM PATCH /dag (delta)")
    vm_prev, vm_next, full_prev, full_next = _stamped_pair()
    delta = diff_docs(full_prev, full_next)   # the proxy diffs full docs; the VM ignores runtime/pending
    check("fixture delta carries graph ops and runtime keys",
          "nodes_upsert" in delta and "runtime" in delta)
    r = client.patch("/dag", json=delta)
    check("PATCH without marker → 403", r.status_code == 403 and r.json() == {"error": "proxy-internal endpoint"})
    r = client.patch("/dag", content=b" " * (DAG_MAX_BYTES + 1),
                     headers={**PROXY, "Content-Type": "application/json"})
    check("PATCH DAG_MAX_BYTES+1 → 413", r.status_code == 413)
    for label, raw, field in (
        ("not JSON", b"{nope", "body"),
        ("delta 2", json.dumps(dict(delta, delta=2)).encode(), "delta"),
        ("schema 2", json.dumps(dict(delta, schema=2)).encode(), "schema"),
        ("bool version", json.dumps(dict(delta, version=True)).encode(), "version"),
        ("base_runtime_version missing",
         json.dumps({k: v for k, v in delta.items() if k != "base_runtime_version"}).encode(),
         "base_runtime_version"),
        ("graph_hash int", json.dumps(dict(delta, graph_hash=1)).encode(), "graph_hash"),
        ("nodes_upsert dict", json.dumps(dict(delta, nodes_upsert={})).encode(), "nodes_upsert"),
        ("edges_remove bad key", json.dumps(dict(delta, edges_remove=[["a", "b"]])).encode(), "edges_remove"),
    ):
        r = client.patch("/dag", content=raw, headers={**PROXY, "Content-Type": "application/json"})
        check(f"PATCH {label} → 400 '{field} invalid'", r.status_code == 400
              and r.json() == {"error": f"{field} invalid"})

    saved_store = app.state.dag_store
    app.state.dag_store = DagStore()
    try:
        r = client.patch("/dag", json=delta, headers=PROXY)
        check("PATCH with no document → 409 no_document",
              r.status_code == 409 and r.json() == {"error": "no_document"})
        client.put("/dag", json=vm_prev, headers=PROXY)
        r = client.patch("/dag", json=dict(delta, base_version=delta["base_version"] + 5), headers=PROXY)
        check("PATCH base mismatch → 409 with the VM's version and runtime_version",
              r.status_code == 409 and r.json() == {"error": "base_mismatch", "version": vm_prev["version"],
                                                    "runtime_version": vm_prev["runtime_version"]})
        tampered = copy.deepcopy(delta)
        tampered["nodes_upsert"][0]["reads"] = ["tampered"]
        r = client.patch("/dag", json=tampered, headers=PROXY)
        after = client.get("/dag", headers=PROXY).json()["doc"]
        check("PATCH tampered op → 409 hash_mismatch, stored doc unchanged",
              r.status_code == 409 and r.json() == {"error": "hash_mismatch"} and after == vm_prev)
        r = client.patch("/dag", json=delta, headers=PROXY)
        body = client.get("/dag", headers=PROXY).json()
        check("PATCH valid → 200 {ok, version, runtime_version}",
              r.status_code == 200 and r.json() == {"ok": True, "version": full_next["version"],
                                                    "runtime_version": full_next["runtime_version"]})
        vm_delta = {k: v for k, v in delta.items() if k not in ("runtime", "pending")}
        check("GET /dag doc == apply_delta(PUT doc, delta) — a full document",
              body["doc"] == apply_delta(vm_prev, vm_delta) == vm_next and body["origin"] == "pushed")
        check("runtime/pending keys of the delta are ignored (not stored)",
              not ({"runtime", "pending"} & set(body["doc"])))
        r = client.patch("/dag", json=delta, headers=PROXY)
        check("replaying the same delta → 409 base_mismatch (already applied)",
              r.status_code == 409 and r.json()["error"] == "base_mismatch")

        # Restore → PATCH: dag.json carries the `checkpoint` save stamp; the
        # restored VM copy must still verify the next delta (no 409 → full PUT).
        print("\nVM restore → PATCH /dag")
        stamp = {"id": "ck1", "saved_at": "2026-05-01T10:00:02+00:00", "session_id": "s-old",
                 "microvm_id": "vm-old"}
        dag_json = {**copy.deepcopy(vm_prev), "runtime": {"clock": 2, "vars": {}, "runs": {}},
                    "checkpoint": stamp}
        restored = {k: v for k, v in dag_json.items() if k != "runtime"}  # what manager._restore_dag passes
        app.state.dag_store = DagStore()
        app.state.dag_store.set_restored(restored, "s-old", "dag.json", True)
        body = client.get("/dag", headers=PROXY).json()
        check("restored VM copy has no checkpoint/runtime/pending key",
              body["origin"] == "restored" and body["doc"] == vm_prev)
        check("restore report keeps the checkpoint stamp", body.get("checkpoint") == stamp
              and body["consistent"] is True and body["runtime_source"] == "dag.json")
        r = client.patch("/dag", json=delta, headers=PROXY)
        check("first PATCH after a restore → 200 (no hash_mismatch)",
              r.status_code == 200 and r.json()["version"] == full_next["version"])
        check("... and the VM copy equals the next document",
              client.get("/dag", headers=PROXY).json()["doc"] == vm_next)
    finally:
        app.state.dag_store = saved_store


def _run():
    from fastapi.testclient import TestClient
    from app.notebook.dag_contract import DAG_MAX_BYTES, cell_code_hash
    from app.notebook.executor import RuntimeSectionBusy

    app = _vm_app()
    client = TestClient(app)
    executor = app.state.executor

    print("\nVM /dag auth marker")
    r = client.get("/dag")
    check("GET /dag without marker → 403", r.status_code == 403
          and r.json() == {"error": "proxy-internal endpoint"})
    r = client.put("/dag", json=_doc())
    check("PUT /dag without marker → 403", r.status_code == 403
          and r.json() == {"error": "proxy-internal endpoint"})
    r = client.put("/dag", json=_doc(), headers={"X-Dag-Writer": "browser"})
    check("PUT /dag with another marker value → 403", r.status_code == 403)
    r = client.get("/dag", headers={"X-Dag-Writer": "Proxy"})
    check("GET /dag marker is exact ('Proxy' rejected)", r.status_code == 403)

    print("\nVM GET /dag initial state")
    r = client.get("/dag", headers=PROXY)
    body = r.json()
    check("GET /dag → 200", r.status_code == 200)
    check("origin 'empty' before any push", body.get("origin") == "empty" and body.get("doc") is None)
    check("GET /dag body keys", set(body) == {"origin", "doc", "runtime", "restored_from",
                                              "runtime_source", "consistent", "checkpoint"})
    check("runtime has clock/vars/runs", isinstance(body.get("runtime"), dict)
          and {"clock", "vars", "runs"} <= set(body["runtime"]))

    print("\nVM PUT /dag validation")
    bad = [
        ("not JSON", b"{not json", "body"),
        ("list body", json.dumps([1]).encode(), "body"),
        ("schema 2", json.dumps(_doc(schema=2)).encode(), "schema"),
        ("schema true", json.dumps(_doc(schema=True)).encode(), "schema"),
        ("schema missing", json.dumps({k: v for k, v in _doc().items() if k != "schema"}).encode(), "schema"),
        ("version -1", json.dumps(_doc(version=-1)).encode(), "version"),
        ("version '1'", json.dumps(_doc(version="1")).encode(), "version"),
        ("version true", json.dumps(_doc(version=True)).encode(), "version"),
        ("version 1.5", json.dumps(_doc(version=1.5)).encode(), "version"),
        ("nodes dict", json.dumps(_doc(nodes={})).encode(), "nodes"),
        ("edges missing", json.dumps({k: v for k, v in _doc().items() if k != "edges"}).encode(), "edges"),
        ("issues null", json.dumps(_doc(issues=None)).encode(), "issues"),
    ]
    for label, raw, field in bad:
        r = client.put("/dag", content=raw, headers={**PROXY, "Content-Type": "application/json"})
        check(f"PUT {label} → 400 '{field} invalid'", r.status_code == 400
              and r.json() == {"error": f"{field} invalid"})
    check("invalid PUTs left origin 'empty'", client.get("/dag", headers=PROXY).json()["origin"] == "empty")

    print("\nVM PUT /dag size cap")
    big = b" " * (DAG_MAX_BYTES + 1)
    r = client.put("/dag", content=big, headers={**PROXY, "Content-Type": "application/json"})
    check("PUT DAG_MAX_BYTES+1 (Content-Length) → 413", r.status_code == 413)

    def _chunks():
        step = 1024 * 1024
        sent = 0
        while sent < DAG_MAX_BYTES + 1:
            n = min(step, DAG_MAX_BYTES + 1 - sent)
            sent += n
            yield b" " * n

    r = client.put("/dag", content=_chunks(), headers={**PROXY, "Content-Type": "application/json"})
    check("PUT DAG_MAX_BYTES+1 streamed (no Content-Length) → 413", r.status_code == 413)
    pad = DAG_MAX_BYTES - len(json.dumps(_doc(version=0)).encode())
    at_cap = json.dumps(_doc(version=0)).encode()[:-1] + b" " * pad + b"}"
    check("fixture is exactly DAG_MAX_BYTES", len(at_cap) == DAG_MAX_BYTES)
    r = client.put("/dag", content=at_cap, headers={**PROXY, "Content-Type": "application/json"})
    check("PUT exactly DAG_MAX_BYTES → 200", r.status_code == 200)

    print("\nVM PUT /dag → origin pushed")
    pushed = _doc(version=7, nodes=[{"cell_id": "c1"}], runtime={"clock": 99}, pending={"x": {}},
                  checkpoint={"id": "zzz"}, trigger="execute")
    r = client.put("/dag", json=pushed, headers=PROXY)
    check("PUT valid → 200 {ok, version}", r.status_code == 200 and r.json() == {"ok": True, "version": 7})
    body = client.get("/dag", headers=PROXY).json()
    check("origin 'pushed' after PUT", body["origin"] == "pushed")
    check("doc stored (version, nodes, trigger)", body["doc"]["version"] == 7
          and body["doc"]["nodes"] == [{"cell_id": "c1"}] and body["doc"]["trigger"] == "execute")
    check("runtime/pending/checkpoint discarded from stored doc",
          not ({"runtime", "pending", "checkpoint"} & set(body["doc"])))
    check("GET runtime is the live executor section (not the pushed one)",
          body["runtime"]["clock"] != 99)
    for label, bad_rv in (("runtime_version -1", -1), ("runtime_version true", True), ("runtime_version '2'", "2")):
        r = client.put("/dag", json=_doc(version=8, runtime_version=bad_rv), headers=PROXY)
        check(f"PUT {label} → 400 'runtime_version invalid'", r.status_code == 400
              and r.json() == {"error": "runtime_version invalid"})
    r = client.put("/dag", json=_doc(version=8, runtime_version=4, graph_hash="g", runtime_hash="h"),
                   headers=PROXY)
    kept = client.get("/dag", headers=PROXY).json()["doc"]
    check("PUT keeps the stamps (runtime_version, graph_hash, runtime_hash)",
          r.status_code == 200 and kept["runtime_version"] == 4 and kept["graph_hash"] == "g"
          and kept["runtime_hash"] == "h")

    _patch_checks(app, client)

    print("\nVM /execute runtime + variables_rebound")
    client.post("/reset")
    code1 = "x = 1\n"
    r = client.post("/execute", json={"code": code1, "cell_id": "c1"}).json()
    rt = r.get("runtime") or {}
    check("/execute response carries runtime", isinstance(r.get("runtime"), dict))
    check("runtime.runs[c1].code_hash == cell_code_hash('code', code)",
          rt.get("runs", {}).get("c1", {}).get("code_hash") == cell_code_hash("code", code1))
    check("runtime.runs[c1].ok and clock == runtime.clock",
          rt["runs"]["c1"]["ok"] is True and rt["runs"]["c1"]["clock"] == rt["clock"])
    check("runtime.vars.x defined by c1", rt.get("vars", {}).get("x", {}).get("defined_by") == "c1")
    check("new var is not rebound", r.get("variables_rebound") == [])
    r = client.post("/execute", json={"code": "x = 2", "cell_id": "c2"}).json()
    check("rebind of existing x → variables_rebound ['x']", r.get("variables_rebound") == ["x"])
    r = client.post("/execute", json={"code": "x += 1", "cell_id": "c3"}).json()
    check("AugAssign x += 1 → variables_rebound ['x']", r.get("variables_rebound") == ["x"])
    code_err = "raise ValueError('boom')"
    r = client.post("/execute", json={"code": code_err, "cell_id": "c4"}).json()
    run4 = (r.get("runtime") or {}).get("runs", {}).get("c4", {})
    check("/execute error still carries runtime with ok False",
          r.get("success") is False and run4.get("ok") is False
          and run4.get("code_hash") == cell_code_hash("code", code_err))
    r = client.post("/execute", json={"code": "y = 1"}).json()
    check("/execute without cell_id: runtime present, no ledger entry for ''",
          isinstance(r.get("runtime"), dict) and "" not in r["runtime"]["runs"])

    print("\nVM /execute-sql runtime")
    sql = "  SELECT 1 AS one\n"
    r = client.post("/execute-sql", json={"sql": sql, "cell_id": "s1", "output_variable": "one_df"}).json()
    run = (r.get("runtime") or {}).get("runs", {}).get("s1", {})
    check("/execute-sql success carries runtime", r.get("success") is True and isinstance(r.get("runtime"), dict))
    check("runtime.runs[s1].code_hash == cell_code_hash('sql', raw sql)",
          run.get("code_hash") == cell_code_hash("sql", sql) and run.get("ok") is True)
    check("SQL output var in runtime.vars", r["runtime"]["vars"].get("one_df", {}).get("last_cell") == "s1")
    r = client.post("/execute-sql", json={"sql": "SELECT * FROM no_such_table_xyz", "cell_id": "s2"}).json()
    run = (r.get("runtime") or {}).get("runs", {}).get("s2", {})
    check("/execute-sql error carries runtime with ok False",
          r.get("success") is False and run.get("ok") is False
          and run.get("code_hash") == cell_code_hash("sql", "SELECT * FROM no_such_table_xyz"))

    print("\nVM /set-variable + /reset runtime")
    r = client.post("/set-variable", json={"name": "injected", "value": 5, "cell_id": "c9"}).json()
    check("/set-variable carries runtime with the injected var",
          r.get("success") is True and r.get("runtime", {}).get("vars", {}).get("injected", {}).get("last_cell") == "c9")
    r = client.post("/reset").json()
    check("/reset carries an empty runtime", r.get("runtime") == {"clock": 0, "vars": {}, "runs": {}})

    print("\nVM /execute-batch frames")
    batch_sql = "  SELECT 2 AS two\n"
    cells = [
        {"cell_id": "b1", "type": "code", "code": "a = 1"},
        {"cell_id": "bm", "type": "markdown", "code": "# hi"},
        {"cell_id": "b2", "type": "sql", "code": batch_sql, "output_variable": "two_df"},
        {"cell_id": "b3", "type": "code", "code": "a += 1"},
    ]
    r = client.post("/execute-batch", json={"cells": cells})
    frames = _sse_frames(r.text)
    types_ = [f.get("type") for f in frames]
    check("batch frames: 3 cell_result, 1 runtime, then done",
          types_ == ["cell_result", "cell_result", "cell_result", "runtime", "done"])
    check("no cell_result.result has a 'runtime' key",
          all("runtime" not in (f.get("result") or {}) for f in frames if f.get("type") == "cell_result"))
    rt_frames = [f for f in frames if f.get("type") == "runtime"]
    runs = (rt_frames[0].get("runtime") or {}).get("runs", {}) if rt_frames else {}
    check("runtime frame: ledger for b1/b2/b3 with executed hashes",
          runs.get("b1", {}).get("code_hash") == cell_code_hash("code", "a = 1")
          and runs.get("b2", {}).get("code_hash") == cell_code_hash("sql", batch_sql)
          and runs.get("b3", {}).get("code_hash") == cell_code_hash("code", "a += 1"))
    check("markdown cell not in the ledger", "bm" not in runs)
    b3 = next((f for f in frames if f.get("cell_id") == "b3"), {})
    check("batch cell result carries variables_rebound", (b3.get("result") or {}).get("variables_rebound") == ["a"])
    r = client.post("/execute-batch", json={"cells": []})
    check("empty batch still emits exactly one runtime frame before done",
          [f.get("type") for f in _sse_frames(r.text)] == ["runtime", "done"])

    print("\nThread safety")
    errors = []
    done = threading.Event()

    def _exec_loop():
        try:
            for i in range(16):
                executor.execute(f"t{i} = list(range({i}))\nt{i}.append(1)", f"tc{i}")
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            done.set()

    th = threading.Thread(target=_exec_loop)
    th.start()
    reads = 0
    try:
        while not done.is_set():
            executor.runtime_section()
            reads += 1
    except Exception as e:
        errors.append(e)
    th.join()
    check(f"runtime_section concurrent with 16 worker-thread executes never raises ({reads} reads)",
          not errors)

    errors.clear()
    done.clear()
    start = threading.Barrier(2)

    def _write_loop():
        try:
            start.wait(5)
            for i in range(2000):
                executor.record_var_write(f"v{i}", "c1")
                executor.set_variable(f"w{i % 50}", i, "c2")
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            done.set()

    th = threading.Thread(target=_write_loop)
    th.start()
    reads = 0
    start.wait(5)
    try:
        while not done.is_set() or reads == 0:
            executor.runtime_section()
            reads += 1
    except Exception as e:
        errors.append(e)
    th.join()
    check(f"record_var_write/set_variable (2000) vs runtime_section never raises ({reads} reads)",
          not errors and reads > 0)

    print("\nBusy lock")
    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with executor._exec_lock:
            holding.set()
            release.wait(15)

    th = threading.Thread(target=_hold)
    th.start()
    holding.wait(5)
    try:
        executor.runtime_section(lock_timeout=0.1)
        busy_raised = False
    except RuntimeSectionBusy:
        busy_raised = True
    check("runtime_section(lock_timeout) raises RuntimeSectionBusy when held elsewhere", busy_raised)
    t0 = time.monotonic()
    r = client.get("/dag", headers=PROXY)
    elapsed = time.monotonic() - t0
    release.set()
    th.join()
    check(f"GET /dag while _exec_lock is held → 503 busy ({elapsed:.1f}s)",
          r.status_code == 503 and r.json() == {"busy": True})
    check("busy answer within ~6 s", 4.0 <= elapsed <= 6.5)
    check("GET /dag works again after release", client.get("/dag", headers=PROXY).status_code == 200)
    client.post("/reset")


def test_vm_dag_endpoint():
    """pytest entry point."""
    _checks.clear()
    try:
        _run()
    finally:
        logging.disable(logging.NOTSET)  # undo _vm_app()'s silencing for later suites
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
