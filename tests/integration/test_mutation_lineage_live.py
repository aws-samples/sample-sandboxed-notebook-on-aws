#!/usr/bin/env python3
# test-kind: manual
"""
LIVE integration test — the persisted notebook DAG (design.md §12 Integration):
in-place mutation chains, lineage, reactive downstream sets and the dag.json
checkpoint copy, end to end against the running stack.

The bug it guards (seen in the "AWS Data Sources" sample, PY-17): a cell that
mutates a DataFrame in place (`products['score'] = ...`, `.sort_values(...,
inplace=True)`) used to record the frame as READ but not WRITTEN, so the lineage
walk stopped at the mutator and reactive re-runs missed the readers after it.
Dependencies are now inferred ONCE, by the proxy's builder (proxy/notebook/dag.py),
and stored per notebook in `notebook_dag`; the web only traverses that document.

Notebook shape exercised (one throwaway notebook, cells c1..c7):
    c1  holiday fetch: requests → date.nager.at        writes holidays, mult (source: url)
    c2  raw = read_athena(orders)                      writes raw      (source: athena)
    c3  products = raw.copy()                          builder of products
    c4  risk_days = 14; products['score'] = …          in-place mutator #1 (subscript)
    c5  products.sort_values(…, inplace=True) …        in-place mutator #2 (methods)
    c6  top = products.head(3)                         reader
    c7  z = 5                                          unrelated (must never re-run)

Checks:
  1. STATIC   — POST /notebooks {name, cells, session_id} → the response carries the
                stored `dag` built from the code: mutators read AND mutate products,
                edges c3→c4 defines/mutate, c4→c5 mutates/mutate, c5→c6 mutates/read.
                (STATIC_ONLY=1 stops here — no VM, no image rebuild needed.)
  2. RUNTIME  — POST /launch {notebookId, sessionId, checkpointEnabled} links the
                session to the notebook; c1..c7 run via /proxy/execute with
                X-Session-Id + cell_id and every response carries `dag_delta` (or
                the full `dag`); a local doc from GET /notebooks/{id}/dag advanced
                by apply_delta equals GET afterwards (rev, graph_hash,
                runtime_hash, graph projection); re-running the leaf c6 twice
                gives the unchanged marker (same version); re-running c1 gives a
                changing delta whose apply matches GET; GET
                /notebooks/{id}/dag holds runtime facts, the same edges, downstream
                sets via BFS over the stored edges, c7 isolated, sources per step,
                cross_cell_mutation on c4 and c5, external_state on c1 only (no issue
                for c2's athena source); the notebook_dag row (read-only SQLite) and
                runtime.vars.products.last_cell == 'c5'.
  3. LINEAGE  — web/src/services/lineage.js buildLineage(c6, cells, dag) over the
                stored document: steps reach c1..c5, sources attributed per step;
                depGraph.graphFromDag downstream sets / cycles / inputsOf, and the
                owner of products (runtime.vars.last_cell + last writing node) is c5.
  4. CHECKPOINT (SKIP_CHECKPOINT=1 skips) — POST /checkpoint-save, then
                sessions/{sid}/dag.json + manifest.json from S3 (real-AWS read with a
                clean env): dag.json present, provenance.json absent, checkpoint ids
                match, graph_hash verifies (checkpoint stamp excluded) and equals the
                served graph, runtime.vars.products.last_cell == 'c5', no modules /
                dunders in runtime.vars; terminate, relaunch with restoreFromSession,
                GET …/dag: runtime restored (same checks), c1..c6 not
                not_in_checkpoint, and the first execute's dag_delta applies to the
                restored doc without a resync.

A section that raises records one [FAIL] and the later sections still run.

What must be live: the proxy (builder + routes) restarted after the DAG change, and
the MicroVM IMAGE rebuilt (app/ is baked into the image) for checks 2-4.

LIVE test: creates a notebook + launches MicroVMs (cleaned up at the end), makes two
small public HTTP calls from the VM and one small Athena query. Not part of the
offline suite. Run only when the stack is up:

    env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
        .venv/bin/python tests/integration/test_mutation_lineage_live.py
    # STATIC_ONLY=1 … check 1 only;  SKIP_CHECKPOINT=1 … checks 1-3

Exit 0 = all checks passed; 1 = a failure or the stack wasn't reachable.
The proxy auth token is read from the running proxy's environment and only ever
sent as a request header (never printed).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from app.notebook.dag_contract import (  # noqa: E402  (stdlib-only shared contract)
    DeltaMismatch, apply_delta, graph_hash, graph_projection, rev, runtime_hash,
)
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8081").rstrip("/")
AUTH_HEADER = "X-Proxy-Auth"
STATIC_ONLY = os.environ.get("STATIC_ONLY") == "1"
SKIP_CHECKPOINT = os.environ.get("SKIP_CHECKPOINT") == "1"
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
DB_PATH = os.path.join(ROOT, "proxy", "data", "microvm.db")
SESSION_ID = str(uuid.uuid4())

# ── output helpers (same style as the other live tests) ──────────────────────
_failures: list[str] = []


def ok(msg):   print(f"  [PASS] {msg}")
def bad(msg):  print(f"  [FAIL] {msg}"); _failures.append(msg)
def info(msg): print(f"  ... {msg}")
def head(msg): print(f"\n== {msg} ==")


def check(cond, msg, detail=""):
    (ok if cond else bad)(msg + (f" — {detail}" if detail and not cond else ""))
    return cond


# ── the notebook under test ───────────────────────────────────────────────────
CELLS = [
    {"id": "c1", "type": "code", "code":
        "import requests\n"
        "holidays = requests.get('https://date.nager.at/api/v3/publicholidays/2026/US', timeout=30).json()\n"
        "mult = 1 + len(holidays) / 100\n"},
    {"id": "c2", "type": "code", "code":
        "raw = read_athena('SELECT * FROM microvm_demo_db.orders LIMIT 20')\n"},
    {"id": "c3", "type": "code", "code":
        "products = raw.copy()\n"},
    {"id": "c4", "type": "code", "code":
        '# @param {"type": "slider", "min": 1, "max": 30, "default": 14, "label": "Risk days"}\n'
        "risk_days = 14\n"
        "products['score'] = range(len(products))\n"
        "products['score'] = products['score'] * risk_days * mult\n"},
    {"id": "c5", "type": "code", "code":
        "products.sort_values('score', ascending=False, inplace=True)\n"
        "products.reset_index(drop=True, inplace=True)\n"},
    {"id": "c6", "type": "code", "code":
        "top = products.head(3)\n"
        "print(len(top))\n"},
    {"id": "c7", "type": "code", "code":
        "z = 5\n"},
]
IDS = [c["id"] for c in CELLS]
MUTATORS = ("c4", "c5")
NOISE = {"__builtins__", "requests", "pd", "display", "read_athena"}
RUNTIME_NOISE = NOISE | {"os", "np", "json", "sys"}
# Live resources the sections hand to each other and to cleanup (survives a section's exception).
STATE: dict = {"vm": None}
CHAIN_EDGES = [("c3", "c4", "defines", "mutate"), ("c4", "c5", "mutates", "mutate"),
               ("c5", "c6", "mutates", "read")]


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _token() -> str:
    """The running proxy's per-run token, read from its environment (never printed)."""
    if os.environ.get("PROXY_AUTH_TOKEN"):
        return os.environ["PROXY_AUTH_TOKEN"]
    pid = subprocess.run(["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    if not pid:
        return ""
    env = subprocess.run(["ps", "eww", "-p", pid[0]], capture_output=True, text=True).stdout
    for tok in env.split():
        if tok.startswith("PROXY_AUTH_TOKEN="):
            return tok.split("=", 1)[1]
    return ""


TOKEN = _token()


def call(method, path, body=None, timeout=180, session=True):
    headers = {"Content-Type": "application/json", AUTH_HEADER: TOKEN}
    if session:
        headers["X-Session-Id"] = SESSION_ID
    req = urllib.request.Request(PROXY_URL + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def call_retry(method, path, body=None, attempts=8, **kw):
    """Retry the cold-start 404/502 window right after /launch."""
    for i in range(attempts):
        try:
            return call(method, path, body, **kw)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 502, 503) or i == attempts - 1:
                raise
            time.sleep(4)


def launch(nid, *, restore=False):
    body = {"notebookName": "[live test] mutation lineage", "notebookId": str(nid),
            "memoryMiB": 2048, "idleTimeoutSeconds": 300,
            "checkpointEnabled": not SKIP_CHECKPOINT, "sessionId": SESSION_ID}
    if restore:
        body["restoreFromSession"] = SESSION_ID
    res = call("POST", "/launch", body, timeout=300)
    return res.get("microvmId") or res.get("microvm_id"), res


# ── stored-document helpers ───────────────────────────────────────────────────
def node(doc, cid):
    return next((n for n in doc.get("nodes") or [] if n.get("cell_id") == cid), None)


def edge_tuples(doc):
    return {(e["from"], e["to"], e["kind"], e["use"]) for e in doc.get("edges") or [] if e.get("var") == "products"}


def downstream(doc, start):
    """BFS over the stored edges (what depGraph.collectDownstream does)."""
    out = {}
    for e in doc.get("edges") or []:
        out.setdefault(e["from"], set()).add(e["to"])
    seen, queue = set(), [start]
    while queue:
        cur = queue.pop(0)
        for n in out.get(cur, ()):
            if n not in seen and n != start:
                seen.add(n)
                queue.append(n)
    return seen


def issues(doc, code=None, cid=None):
    return [i for i in doc.get("issues") or [] if (code is None or i["code"] == code)
            and (cid is None or i["cell_id"] == cid)]


def check_chain(label, doc):
    for cid in MUTATORS:
        n = node(doc, cid) or {}
        check("products" in n.get("reads", []) and "products" in n.get("mutates", [])
              and "products" in n.get("writes", []),
              f"{label}: mutator {cid} reads AND mutates products",
              f"reads={n.get('reads')} writes={n.get('writes')} mutates={n.get('mutates')}")
    check("products" in (node(doc, "c3") or {}).get("writes", []), f"{label}: builder c3 writes products")
    c6 = node(doc, "c6") or {}
    check("products" in c6.get("reads", []) and "products" not in c6.get("writes", []),
          f"{label}: reader c6 reads (not writes) products", json.dumps(c6.get("writes")))
    have = edge_tuples(doc)
    for e in CHAIN_EDGES:
        check(e in have, f"{label}: edge {e[0]}→{e[1]} products {e[2]}/{e[3]}", str(sorted(have)))
    noisy = {n["cell_id"]: sorted(NOISE & (set(n.get("reads", [])) | set(n.get("writes", []))))
             for n in doc.get("nodes") or []}
    noisy = {k: v for k, v in noisy.items() if v}
    check(not noisy, f"{label}: no dunders / modules / injected helpers in node facts", str(noisy))


# ── node harness over the real web services ───────────────────────────────────
NODE_HARNESS = r"""
import fs from 'node:fs'
const [, , depPath, linPath, dataPath] = process.argv
const dep = await import('file://' + depPath)
const lin = await import('file://' + linPath)
const { cells, dag } = JSON.parse(fs.readFileSync(dataPath, 'utf8'))
const g = dep.graphFromDag(dag)
const down = (ids) => [...dep.collectDownstream(ids, g.downstream)].sort()
const L = lin.buildLineage('c6', cells, dag, [])
// graphFromDag returns {edges, upstream, downstream, inputsOf, nodeById} (no
// ownerOf): the owner of a variable comes from the DAG document itself —
// runtime.vars[v].last_cell (VM-captured) and the last node, in position order,
// that writes or mutates v (static facts).
const owner = Object.fromEntries(Object.entries(dag.runtime?.vars ?? {})
  .map(([v, i]) => [v, i?.last_cell ?? null]))
const lastWriter = {}
for (const n of [...g.nodeById.values()]) {
  for (const v of [...(n.writes || []), ...(n.mutates || [])]) lastWriter[v] = String(n.cell_id)
}
console.log(JSON.stringify({
  owner,
  lastWriter,
  inputsOf: Object.fromEntries([...g.inputsOf].map(([c, m]) => [c, Object.fromEntries(m)])),
  cycle: [...dep.findCycleCells(g.downstream)],
  down: { c1: down(['c1']), c3: down(['c3']), c4: down(['c4']), c5: down(['c5']), c7: down(['c7']) },
  lineage: { steps: L.steps.map(s => s.cellId), sources: L.sources, trust: L.trust, issues: L.issues },
}))
"""


def run_web(cells, doc):
    with tempfile.TemporaryDirectory() as d:
        data = os.path.join(d, "data.json")
        script = os.path.join(d, "harness.mjs")
        with open(data, "w") as f:
            json.dump({"cells": cells, "dag": doc}, f)
        with open(script, "w") as f:
            f.write(NODE_HARNESS)
        out = subprocess.run(
            ["node", script, os.path.join(ROOT, "web/src/services/depGraph.js"),
             os.path.join(ROOT, "web/src/services/lineage.js"), data],
            capture_output=True, text=True, cwd=os.path.join(ROOT, "web"))
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip())
        return json.loads(out.stdout)


# ── AWS (real-AWS reads with a clean env: the shell exports dummy keys) ──────
def _aws(*args) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
    out = subprocess.run(["aws", *args, "--region", AWS_REGION], capture_output=True, text=True, env=env,
                         timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"aws {args[0]} {args[1]} failed: {out.stderr.strip()[:300]}")
    return out.stdout


_BUCKET: list[str] = []


def _bucket():
    if not _BUCKET:
        account = json.loads(_aws("sts", "get-caller-identity"))["Account"]
        _BUCKET.append(f"microvm-sandbox-artifacts-{account}-{AWS_REGION}")
    return _BUCKET[0]


def s3_json(key):
    return json.loads(_aws("s3", "cp", f"s3://{_bucket()}/{key}", "-"))


def s3_exists(key) -> bool:
    """head-object: True present, False on 404; any other error raises."""
    try:
        _aws("s3api", "head-object", "--bucket", _bucket(), "--key", key)
        return True
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return False
        raise


def db_dag_row(nid):
    """The notebook_dag row, read-only (the live DB is never opened for writing)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT version, dag_json FROM notebook_dag WHERE notebook_id = ?", (int(nid),)).fetchone()
        edges = conn.execute("SELECT from_cell_id, to_cell_id, var_name, kind, use_kind FROM v_nb_dag_edges "
                             "WHERE notebook_id = ?", (int(nid),)).fetchall()
    finally:
        conn.close()
    return row, edges


# ── checks ────────────────────────────────────────────────────────────────────
def static_checks():
    head("1. static (POST /notebooks → stored dag)")
    nb = call("POST", "/notebooks", {"name": "[live test] mutation lineage", "cells": CELLS,
                                      "session_id": SESSION_ID}, session=False)
    nb = nb.get("notebook", nb)
    doc = nb.get("dag")
    if not check(isinstance(doc, dict) and doc.get("schema") == 1, "POST /notebooks response carries the dag",
                 f"keys={sorted(nb)}"):
        return nb.get("id"), None
    check([n["cell_id"] for n in doc["nodes"]] == IDS, "one node per cell, in order")
    check(all(n["facts_origin"] == "static" for n in doc["nodes"]), "static facts before any run")
    check_chain("static", doc)
    return nb["id"], doc


class Mirror:
    """The browser's view of the document: a full doc from GET /notebooks/{id}/dag,
    advanced by each execute response's `dag` (full) or `dag_delta`
    (dag_contract.apply_delta). A base mismatch resyncs from GET (counted)."""

    def __init__(self, nid):
        self.nid = nid
        self.doc = call("GET", f"/notebooks/{nid}/dag", session=False)
        self.resyncs = 0

    def take(self, r):
        if isinstance(r.get("dag"), dict):
            self.doc = r["dag"]
            return "dag"
        d = r.get("dag_delta")
        if not isinstance(d, dict):
            return None
        try:
            self.doc = apply_delta(self.doc, d)
        except DeltaMismatch as e:
            info(f"delta {e.reason} mismatch (base {d.get('base_version')}/{d.get('base_runtime_version')} vs "
                 f"local {rev(self.doc)}): resync from GET")
            self.resyncs += 1
            self.doc = call("GET", f"/notebooks/{self.nid}/dag", session=False)
        return "dag_delta"


def check_mirror(label, mirror, served):
    check(rev(mirror.doc) == rev(served) and graph_hash(mirror.doc) == graph_hash(served)
          and runtime_hash(mirror.doc) == runtime_hash(served)
          and graph_projection(mirror.doc) == graph_projection(served),
          f"{label}: local doc (GET + applied deltas) == GET /notebooks/{{id}}/dag",
          f"local rev={rev(mirror.doc)} served rev={rev(served)}")


def runtime_checks(nid):
    head("2. runtime (real MicroVM, captured by the proxy)")
    t = time.time()
    vm, res = launch(nid)
    STATE["vm"] = vm
    if not check(bool(vm), "VM launched", json.dumps(res)[:300]):
        return None, None
    info(f"VM ready in {time.time() - t:.1f}s")
    mirror = Mirror(nid)
    for i, c in enumerate(CELLS):
        r = call_retry("POST", "/proxy/execute", {"code": c["code"], "cell_id": c["id"]})
        if not check(not r.get("error"), f"{c['id']} ran", str(r.get("error"))[:300]):
            return vm, None
        kind = mirror.take(r)
        if i == 0:
            check(kind is not None, "execute response carries dag_delta (or the full dag)",
                  "no 'dag'/'dag_delta' — the session is not linked to the notebook (did /launch persist the "
                  "link?)" if kind is None else "")
        else:
            check(kind is not None, f"{c['id']} execute response carries dag_delta / dag",
                  str(r.get("dag_error")))
    doc = call("GET", f"/notebooks/{nid}/dag", session=False)
    check_mirror("after c1..c7", mirror, doc)
    check(mirror.resyncs <= 1, "at most one resync (an attach racing the first GET)", str(mirror.resyncs))

    # Change check: the leaf c6 re-run twice → the second is unchanged (version kept).
    leaf = next(c for c in CELLS if c["id"] == "c6")
    r1 = call_retry("POST", "/proxy/execute", {"code": leaf["code"], "cell_id": "c6"})
    mirror.take(r1)
    r2 = call_retry("POST", "/proxy/execute", {"code": leaf["code"], "cell_id": "c6"})
    mirror.take(r2)
    d2 = r2.get("dag_delta") or {}
    check(d2.get("unchanged") is True and d2.get("version") == d2.get("base_version"),
          "second c6 re-run → dag_delta.unchanged with the same version",
          json.dumps({k: d2.get(k) for k in ("unchanged", "base_version", "version", "base_runtime_version",
                                             "runtime_version")}))
    check(not ({"nodes_upsert", "nodes_remove", "edges_add", "edges_remove", "issues"} & set(d2)),
          "second c6 re-run carries no graph ops", str(sorted(d2)))
    # A changing run: c1 re-run makes its downstream stale.
    c1 = next(c for c in CELLS if c["id"] == "c1")
    r3 = call_retry("POST", "/proxy/execute", {"code": c1["code"], "cell_id": "c1"})
    kind = mirror.take(r3)
    d3 = r3.get("dag_delta") or r3.get("dag") or {}
    check(kind is not None and d3.get("unchanged") is not True,
          "c1 re-run → a changing dag_delta (or the full dag)", str(sorted(d3))[:300])
    after = call("GET", f"/notebooks/{nid}/dag", session=False)
    check_mirror("after the c6/c1 re-runs", mirror, after)
    # Restore "every node current" for the checks below: re-run c1's downstream.
    for c in CELLS[1:]:
        mirror.take(call_retry("POST", "/proxy/execute", {"code": c["code"], "cell_id": c["id"]}))
    doc = call("GET", f"/notebooks/{nid}/dag", session=False)
    check_mirror("after re-running c2..c7", mirror, doc)
    check(all(n["facts_origin"] == "runtime" for n in doc["nodes"]), "every node has runtime facts",
          str([(n["cell_id"], n["facts_origin"]) for n in doc["nodes"]]))
    check(all(n["status"] == "current" for n in doc["nodes"]), "every node current after the run",
          str([(n["cell_id"], n["status"]) for n in doc["nodes"]]))
    check_chain("runtime", doc)
    check({"c5", "c6"} <= downstream(doc, "c4"), "downstream(c4) ⊇ {c5, c6}", str(downstream(doc, "c4")))
    check("c6" in downstream(doc, "c5"), "downstream(c5) ∋ c6")
    check({"c4", "c5", "c6"} <= downstream(doc, "c3"), "downstream(c3) ⊇ {c4, c5, c6}")
    check({"c4", "c5", "c6"} <= downstream(doc, "c1"), "downstream(c1) ⊇ {c4, c5, c6} (via mult)")
    check(not any("c7" in (e["from"], e["to"]) for e in doc["edges"]), "c7 is isolated (no edges)")
    src = {n["cell_id"]: n.get("sources") for n in doc["nodes"]}
    check(any(s["kind"] == "url" and s["id"] == "https://date.nager.at" for s in src["c1"] or []),
          "sources: c1 recorded https://date.nager.at", str(src["c1"]))
    check(any(s["kind"] == "athena" for s in src["c2"] or []), "sources: c2 recorded its Athena table",
          str(src["c2"]))
    check(src["c6"] == [], "sources: reader c6 recorded none of its own", str(src["c6"]))
    check(issues(doc, "cross_cell_mutation", "c4") and issues(doc, "cross_cell_mutation", "c5"),
          "issues: cross_cell_mutation on c4 and c5", json.dumps(issues(doc, "cross_cell_mutation")))
    ext = issues(doc, "external_state")
    check([i["cell_id"] for i in ext] == ["c1"] and ext[0]["source"]["kind"] == "url",
          "issues: external_state on c1 (url) only — none for c2's athena source", json.dumps(ext))
    rt_products = ((doc.get("runtime") or {}).get("vars") or {}).get("products") or {}
    check(rt_products.get("last_cell") == "c5", "runtime: last mutator c5 owns products",
          f"last_cell={rt_products.get('last_cell')}")
    try:
        row, view_edges = db_dag_row(nid)
        check(row is not None and row[0] == doc["version"], "notebook_dag row holds the served version",
              f"row={row[0] if row else None} served={doc['version']}")
        check(("c4", "c5", "products", "mutates", "mutate") in set(view_edges),
              "v_nb_dag_edges exposes c4→c5 products mutates/mutate", str(view_edges[:10]))
    except sqlite3.Error as e:
        info(f"notebook_dag row check skipped (SQLite not readable here: {e}) — Aurora backend?")
    return vm, doc


def lineage_checks(doc):
    head("3. lineage + reactive (web services over the stored dag)")
    cells = [{**c, "lastExecutedCode": c["code"], "executionNumber": i + 1, "output": "ok"}
             for i, c in enumerate(CELLS)]
    g = run_web(cells, doc)
    steps, sources = g["lineage"]["steps"], g["lineage"]["sources"]
    info(f"steps={steps}")
    check({"c1", "c2", "c3", "c4", "c5", "c6"} <= set(steps),
          "lineage(c6) reaches builder c3, raw c2, both mutators and the holiday cell", str(steps))
    check("c7" not in steps, "lineage(c6) excludes unrelated c7", str(steps))
    by_id = {s["id"]: s for s in sources}
    url = by_id.get("https://date.nager.at")
    check(url is not None and [st["cellId"] for st in url["steps"]] == ["c1"],
          "the URL source is attributed to step c1", json.dumps(url))
    athena = next((s for s in sources if s["kind"] == "athena"), None)
    check(athena is not None and [st["cellId"] for st in athena["steps"]] == ["c2"],
          "the Athena source is attributed to step c2", json.dumps(athena))
    check(not any(s["readByThisCell"] for s in sources), "c6's own sources are empty ('Read by this cell')")
    down = g["down"]
    check({"c5", "c6"} <= set(down["c4"]) and "c6" in down["c5"], "depGraph: mutator downstream sets",
          json.dumps(down))
    check(all("c7" not in v for v in down.values()), "depGraph: unrelated c7 is never re-run")
    check(not g["cycle"], "depGraph: no dependency cycle", str(g["cycle"]))
    check(g["owner"].get("products") == "c5", "dag: runtime.vars.products.last_cell (owner) == c5",
          str(g["owner"]))
    check(g["lastWriter"].get("products") == "c5", "dag: last node writing/mutating products == c5",
          str(g["lastWriter"]))
    check((g["inputsOf"].get("c6") or {}).get("products") == "c5",
          "depGraph: inputsOf(c6).products resolves to the last mutator c5", json.dumps(g["inputsOf"].get("c6")))


def runtime_noise(doc):
    """Runtime var names that must never be recorded: dunders, modules, injected helpers."""
    names = ((doc or {}).get("runtime") or {}).get("vars") or {}
    return sorted(v for v in names if v in RUNTIME_NOISE or v.startswith("__"))


def checkpoint_checks(nid, vm):
    head("4. checkpoint (dag.json in S3) + restore")
    served = call("GET", f"/notebooks/{nid}/dag", session=False)
    res = call("POST", "/checkpoint-save", timeout=300)
    if not check(res.get("success") is not False, "POST /checkpoint-save", json.dumps(res)[:300]):
        return vm
    prefix = f"sessions/{SESSION_ID}"
    try:
        dag_json = s3_json(f"{prefix}/dag.json")
        ok("dag.json present in the S3 checkpoint")
    except Exception as e:
        bad(f"dag.json present in the S3 checkpoint — {type(e).__name__}: {e}")
        return vm
    check(not s3_exists(f"{prefix}/provenance.json"), "provenance.json is NOT written (dag.json replaces it)")
    manifest = s3_json(f"{prefix}/manifest.json")
    cid = (dag_json.get("checkpoint") or {}).get("id")
    check(bool(cid) and cid == manifest.get("checkpoint_id"), "dag.json and manifest.json checkpoint ids match",
          f"dag={cid} manifest={manifest.get('checkpoint_id')}")
    # The `checkpoint` stamp is added at save time and is excluded from the graph hash.
    no_stamp = {k: v for k, v in dag_json.items() if k != "checkpoint"}
    stamped = dag_json.get("graph_hash")
    check(isinstance(stamped, str) and stamped == graph_hash(dag_json) == graph_hash(no_stamp),
          "dag.json graph_hash verifies (recomputed with the checkpoint stamp excluded)",
          f"stamped={stamped} recomputed={graph_hash(no_stamp)}")
    check(graph_hash(no_stamp) == graph_hash(served), "dag.json graph == the served document's graph",
          f"dag.json={graph_hash(no_stamp)} served={graph_hash(served)}")
    last = (((dag_json.get("runtime") or {}).get("vars") or {}).get("products") or {}).get("last_cell")
    check(last == "c5", "dag.json runtime.vars.products.last_cell == 'c5'", f"last_cell={last}")
    check(not runtime_noise(dag_json), "dag.json runtime.vars holds no modules / dunders / helpers",
          str(runtime_noise(dag_json)))
    call("POST", "/terminate", {"microvmId": vm}, timeout=300)
    STATE["vm"] = None
    info(f"terminated {vm}; relaunching with restoreFromSession")
    vm2, res = launch(nid, restore=True)
    STATE["vm"] = vm2
    if not check(bool(vm2), "VM relaunched with restore", json.dumps(res)[:300]):
        return None
    # The proxy attaches the restored VM's runtime section asynchronously: poll briefly.
    deadline = time.time() + 60
    while True:
        doc = call_retry("GET", f"/notebooks/{nid}/dag", session=False)
        rt = doc.get("runtime") or {}
        if rt.get("vm_id") == vm2 or time.time() > deadline:
            break
        time.sleep(3)
    check(rt.get("vm_id") == vm2, "runtime section comes from the restored VM", f"vm_id={rt.get('vm_id')}")
    check(((rt.get("vars") or {}).get("products") or {}).get("last_cell") == "c5",
          "restored runtime: products.last_cell == 'c5'")
    check(not runtime_noise(doc), "restored runtime.vars holds no modules / dunders (pd/os/requests/__builtins__)",
          str(runtime_noise(doc)))
    missing = [n["cell_id"] for n in doc["nodes"] if n["cell_id"] in IDS[:6] and n.get("not_in_checkpoint")]
    check(not missing, "c1..c6 are not marked not_in_checkpoint after the restore", str(missing))
    # First change after the restore: the execute response's dag_delta must apply to
    # the restored document as-is (no base mismatch → no resync / full doc). The
    # proxy→VM PATCH /dag itself is not exposed by any proxy route (its 409 → full
    # PUT fallback only logs), so the browser-facing delta is what is observable.
    mirror = Mirror(nid)
    leaf = next(c for c in CELLS if c["id"] == "c6")
    r = call_retry("POST", "/proxy/execute", {"code": leaf["code"], "cell_id": "c6"})
    check(not r.get("error"), "c6 re-runs on the restored namespace", str(r.get("error"))[:300])
    kind = mirror.take(r)
    check(kind is not None and mirror.resyncs == 0,
          f"first execute after restore → {kind} applies to the restored doc (no base mismatch / resync)",
          f"kind={kind} resyncs={mirror.resyncs}")
    check_mirror("after the first post-restore run", mirror, call("GET", f"/notebooks/{nid}/dag", session=False))
    info("proxy→VM PATCH /dag after restore: not observable through the proxy API (logs only)")
    return vm2


def main() -> int:
    if not TOKEN:
        print("proxy not running on :8081 (no PROXY_AUTH_TOKEN found) — start the stack first")
        return 1
    try:
        call("GET", "/health", session=False, timeout=10)
    except Exception as e:
        print(f"proxy not reachable at {PROXY_URL}: {e}")
        return 1
    nid, doc = None, None

    def section(name, fn, *args):
        """Run one section; an exception records ONE [FAIL] and later sections still run."""
        try:
            return fn(*args)
        except Exception as e:
            bad(f"{name}: harness error: {type(e).__name__}: {e}")
            return None

    try:
        static = section("1. static", static_checks)
        nid = static[0] if static else None
        if not STATIC_ONLY and nid is not None:
            rt = section("2. runtime", runtime_checks, nid)
            doc = rt[1] if rt else None
            if doc is None and STATE["vm"]:
                # Section 2 stopped early: the later sections still run on the served doc.
                doc = section("2. runtime (GET dag)", call, "GET", f"/notebooks/{nid}/dag", None, 60, False)
            if doc is not None:
                section("3. lineage", lineage_checks, doc)
            if not SKIP_CHECKPOINT and STATE["vm"]:
                section("4. checkpoint", checkpoint_checks, nid, STATE["vm"])  # keeps STATE["vm"] current
            elif not SKIP_CHECKPOINT:
                bad("4. checkpoint: not run (no live VM from section 2)")
    finally:
        vm = STATE["vm"]
        try:
            if vm:
                call("POST", "/terminate", {"microvmId": vm}, timeout=300)
            if nid is not None:
                call("DELETE", f"/notebooks/{nid}", session=False)
            info("cleanup: notebook deleted" + (", VM terminated" if vm else ""))
        except Exception as e:  # cleanup must never mask the result
            info(f"cleanup failed: {type(e).__name__}: {e}")
    print(f"\nRESULT: {'all checks passed' if not _failures else f'{len(_failures)} check(s) FAILED'}")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
