#!/usr/bin/env python3
# test-kind: offline
"""
Shared DAG contract (app/notebook/dag_contract.py, design §2).

Pure part: cell_code_hash normalises inside the hash: SQL is stripped (the VM
runs the stripped statement), Python whitespace is significant, and the type
prefix keeps a SQL cell and a code cell with the same text apart.

VM part: the REAL VM app (app.server, TestClient, lifespan not run) runs the
DuckDB-only query '  SELECT 1\n' via /execute-sql and via /execute-batch; both
record runtime.runs[cid].code_hash == cell_code_hash('sql', '  SELECT 1\n')
(the proxy hashes the raw DB text, the VM the stripped text it executed). No
stub needed: an empty DataCatalog routes the query to DuckDB.

Run (system python3 — the VM part needs duckdb/dill/pandas):
    python3 tests/dag/test_dag_contract.py
"""
import hashlib
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app.notebook import dag_contract as C  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _run():
    print("\ndag_contract")
    h = C.cell_code_hash
    check("DAG_SCHEMA == 1", C.DAG_SCHEMA == 1)
    check("DAG_MAX_BYTES == 8 MiB", C.DAG_MAX_BYTES == 8 * 1024 * 1024)
    check("SQL: '  SELECT 1\\n' and 'SELECT 1' hash equal", h("sql", "  SELECT 1\n") == h("sql", "SELECT 1"))
    check("Python: whitespace is significant", h("code", "x = 1\n") != h("code", "x = 1")
          and h("code", "  x = 1") != h("code", "x = 1"))
    check("type prefix separates sql/code", h("sql", "SELECT 1") != h("code", "SELECT 1"))
    check("non-sql types hash as code", h("python", "x = 1") == h("code", "x = 1"))
    check("None code hashes like ''", h("code", None) == h("code", ""))
    check("hash is sha256 of '<type>\\n<executed text>'",
          h("sql", " SELECT 1 ") == hashlib.sha256(b"sql\nSELECT 1").hexdigest())
    check("executed_text strips SQL only", C.executed_text("sql", " a \n") == "a"
          and C.executed_text("code", " a \n") == " a \n")
    import ast
    src = open(C.__file__, encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    check("dag_contract imports stdlib only (copy, json, hashlib)", mods <= {"copy", "json", "hashlib"})
    check("delta contract importable on the VM side (diff_docs, apply_delta, graph_hash, send_full)",
          all(callable(getattr(C, f, None)) for f in
              ("diff_docs", "apply_delta", "graph_hash", "runtime_hash", "send_full", "delta_has_vm_changes")))
    _run_checkpoint_stamp()


def _run_checkpoint_stamp():
    """dag.json's `checkpoint` save stamp is a STAMP key: never part of the graph."""
    import copy
    import json
    print("\ndag_contract — checkpoint stamp")
    check("'checkpoint' is a STAMP key", "checkpoint" in C.STAMP_KEYS)
    path = os.path.join(ROOT, "tests", "fixtures", "dag_hash_parity.json")
    with open(path, encoding="utf-8") as fh:
        fx = json.load(fh)
    with_ck, without = fx["doc_with_checkpoint"], fx["doc_without_checkpoint"]
    check("fixture: the docs differ only by the checkpoint key",
          {k: v for k, v in with_ck.items() if k != "checkpoint"} == without and "checkpoint" in with_ck)
    check("graph_hash(doc_with_checkpoint) == graph_hash(doc_without) == fixture graph_hash",
          C.graph_hash(with_ck) == C.graph_hash(without) == fx["graph_hash"] == without["graph_hash"])
    check("checkpoint not in graph_projection", "checkpoint" not in C.graph_projection(with_ck))
    # A delta computed against the stored doc applies to the restored copy (with
    # the stamp) and verifies — the VM's first PATCH after a restore.
    nxt = copy.deepcopy(without)
    nxt["nodes"][0]["reads"] = sorted(set(nxt["nodes"][0].get("reads") or []) | {"zz_new"})
    nxt["graph_hash"] = C.graph_hash(nxt)
    nxt["version"] = without["version"] + 1
    delta = C.diff_docs(without, nxt)
    check("diff_docs never carries the checkpoint stamp",
          "checkpoint" not in (delta.get("graph_set") or {}) and "checkpoint" not in (delta.get("graph_unset") or []))
    try:
        out = C.apply_delta(with_ck, delta, verify=True)
        ok = out["graph_hash"] == nxt["graph_hash"]
    except C.DeltaMismatch:
        ok = False
    check("apply_delta(restored doc with checkpoint, delta) verifies (no hash mismatch)", ok)
    m = C.diff_docs(without, with_ck)
    check("diff_docs(doc, doc + checkpoint) is the unchanged marker", m["unchanged"] is True
          and not (set(m) & {"graph_set", "graph_unset", "nodes_upsert", "edges_add"}))


def _drop_stub_modules():
    """Other suites (test_cell_vars_filter, test_static_mutation, test_lineage_sources)
    install bare ModuleType stubs for plotly/duckdb at import time; in a shared
    pytest session those would shadow the real packages app.server needs."""
    for name in ("plotly.express", "plotly", "duckdb"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__spec__", None) is None and not hasattr(mod, "__file__"):
            del sys.modules[name]


def _run_vm():
    print("\ndag_contract — VM run ledger")
    import json
    import logging
    from fastapi.testclient import TestClient
    _drop_stub_modules()
    import app.server as server
    logging.disable(logging.WARNING)
    client = TestClient(server.app)
    raw = "  SELECT 1\n"
    want = C.cell_code_hash("sql", raw)

    client.post("/reset")
    r = client.post("/execute-sql", json={"sql": raw, "cell_id": "q1"}).json()
    got = (r.get("runtime") or {}).get("runs", {}).get("q1", {}).get("code_hash")
    check("/execute-sql '  SELECT 1\\n' → runs[q1].code_hash == cell_code_hash('sql', raw)",
          r.get("success") is True and got == want)

    r = client.post("/execute-batch", json={"cells": [{"cell_id": "q2", "type": "sql", "code": raw}]})
    frames = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    rt = next((f["runtime"] for f in frames if f.get("type") == "runtime"), {})
    check("/execute-batch same cell → runtime frame runs[q2].code_hash == cell_code_hash('sql', raw)",
          rt.get("runs", {}).get("q2", {}).get("code_hash") == want)
    check("batch SQL cell succeeded", any(f.get("type") == "cell_result" and f["result"].get("success")
                                          for f in frames))
    client.post("/reset")


def test_dag_contract():
    """pytest entry point."""
    _checks.clear()
    _run()
    try:
        _run_vm()
    finally:
        # _run_vm silences app.server's DEBUG logging; do not leak that into
        # later suites in a shared pytest session (test_dag_capture asserts logs).
        import logging
        logging.disable(logging.NOTSET)
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    _run()
    _run_vm()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
