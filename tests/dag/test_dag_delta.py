#!/usr/bin/env python3
# test-kind: offline
"""
DAG change check + delta transport contract (app/notebook/dag_contract.py,
proxy/storage/_dag_doc.py::stamp_doc; delta-plan §0.1-§0.4).

  (a) graph_hash: key-order independent; blind to stamps, runtime, pending,
      run_seq and node clocks; moves on reads / status / stale_reason / edge use
      / issues / level.
  (b) runtime_hash moves on runtime vars, pending and clocks.
  (c) property: >= 300 seeded random sequences of builder operations (edits,
      inserts, deletes, reorders, SQL output variable changes, failed runs,
      unsaved-cell runs -> pending, runtime vars add/remove, vm_id switch, reset
      lowering the clock, cold attach), every step stamped with stamp_doc:
      apply_delta(prev, diff_docs(prev, new)) == new, also for strip_for_vm pairs.
  (d) the real 44-cell 'AWS Data Sources' fixture: open -> run every cell ->
      re-run the leaf twice -> edit a cell's reads -> re-run.
  (e) base mismatch -> DeltaMismatch("base"); tampered node -> "hash".
  (f) unchanged pair -> marker with no ops; leaf re-run -> unchanged + clocks/runtime.
  (g) a same-identity edge whose `use` changed is in both edges_remove and edges_add.
  (h) send_full thresholds.
  (i) non-ASCII var / source id hash stably.

Run:
    python3 tests/dag/test_dag_delta.py
"""
import copy
import importlib.util
import json
import os
import random
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from app.notebook import dag_contract as C  # noqa: E402
from app.notebook.static_analysis import analyze_cell  # noqa: E402
from proxy.notebook import dag  # noqa: E402
from proxy.notebook.dag import RunCapture  # noqa: E402
from proxy.storage._dag_doc import stamp_doc  # noqa: E402

NOW = "2026-05-01T10:00:00.000Z"
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


class Row:
    """A stamped row: successive stamp_doc writes (no DB)."""

    def __init__(self):
        self.doc = None
        self.n = 0

    def write(self, new):
        self.n += 1
        now = f"2026-05-01T10:{self.n // 60 % 60:02d}:{self.n % 60:02d}.000Z"
        rv = self.doc["version"] if self.doc else 0
        doc, wrote, g, r, vm = stamp_doc(self.doc, new, rv, 7, now)
        prev = self.doc
        self.doc = doc
        return prev, doc, wrote, g, r, vm


def cell(cid, code, ctype="code", **kw):
    c = {"id": cid, "type": ctype, "code": code}
    c.update(kw)
    return c


def sim_run(c, *, ok=True, reads=None, cid=None):
    ctype = c.get("type") or "code"
    ov = c.get("outputVariable") or "result"
    a = analyze_cell("sql", c["code"], ov) if ctype == "sql" else analyze_cell("code", c["code"])
    reads = list(a["reads"]) if reads is None else list(reads)
    mutated = sorted(set(a["mutated"]) & set(reads))
    created = [] if ctype == "sql" else sorted(set(a["writes"]) - set(mutated))
    return RunCapture(cid or c["id"], ctype, c["code"], c.get("outputVariable"), ok, tuple(reads),
                      tuple(created), (), tuple(mutated), None, NOW)


def rt(clock, vars_=None):
    return {"clock": clock, "vars": copy.deepcopy(vars_ or {}), "runs": {}}


# ─── (a) (b) (i) hashes ───────────────────────────────────────────────────────

def _base_doc():
    cells = [cell("a", "x = 1"), cell("b", "y = x + 1"), cell("c", "x.append(y)\nz = y")]
    d = dag.apply_cells(None, cells, trigger="open", now=NOW)
    return dag.apply_runs(d, [sim_run(c) for c in cells], rt(3, {"x": {"defined_by": "a"}}), "vm-1",
                          trigger="execute", now=NOW), cells


def t_hashes():
    print("\n(a)(b)(i) graph_hash / runtime_hash")
    d, _ = _base_doc()
    g, r = C.graph_hash(d), C.runtime_hash(d)
    perm = json.loads(json.dumps(d, sort_keys=True))
    rev_keys = {k: perm[k] for k in reversed(list(perm))}
    rev_keys["nodes"] = [{k: n[k] for k in reversed(list(n))} for n in perm["nodes"]]
    check("graph_hash is key-order independent", C.graph_hash(rev_keys) == g)
    check("runtime_hash is key-order independent", C.runtime_hash(rev_keys) == r)
    vol = copy.deepcopy(d)
    vol.update(version=99, built_at="x", trigger="t", notebook_id=5, graph_hash="h", runtime_version=4,
               runtime_hash="q", run_seq=77, runtime={"clock": 9}, pending={"p": {}})
    for n in vol["nodes"]:
        n.update(executed_at="later", last_run_seq=50, last_ok_seq=50)
    check("graph_hash ignores stamps, runtime, pending, run_seq and node clocks", C.graph_hash(vol) == g)

    def moved(mut):
        x = copy.deepcopy(d)
        mut(x)
        return C.graph_hash(x) != g
    check("graph_hash moves on reads", moved(lambda x: x["nodes"][1]["reads"].append("q")))
    check("graph_hash moves on status", moved(lambda x: x["nodes"][0].update(status="stale")))
    check("graph_hash moves on stale_reason",
          moved(lambda x: x["nodes"][2].update(stale_reason={"kind": "upstream_changed", "vars": [], "via": []})))
    check("graph_hash moves on edge use", moved(lambda x: x["edges"][0].update(use="mutate")))
    check("graph_hash moves on issues", moved(lambda x: x["issues"].append({"code": "x"})))
    check("graph_hash moves on level", moved(lambda x: x["nodes"][2].update(level=9)))

    def rmoved(mut):
        x = copy.deepcopy(d)
        mut(x)
        return C.runtime_hash(x) != r
    check("runtime_hash moves on runtime vars", rmoved(lambda x: x["runtime"]["vars"].update(k={})))
    check("runtime_hash moves on pending", rmoved(lambda x: x["pending"].update(k={})))
    check("runtime_hash moves on node clocks", rmoved(lambda x: x["nodes"][0].update(last_run_seq=99)))
    check("runtime_hash moves on run_seq", rmoved(lambda x: x.update(run_seq=99)))
    check("runtime_hash ignores graph and stamps",
          not rmoved(lambda x: (x["nodes"][0].update(status="stale"), x.update(version=5))))

    ua = dag.apply_cells(None, [cell("u", "données = 1")], trigger="open", now=NOW)
    ua = dag.apply_runs(ua, [RunCapture("u", "code", "données = 1", None, True, (), ("données",), (), (),
                                        ({"kind": "s3", "id": "s3://b/🚀.csv"},), NOW)], None, None,
                        trigger="execute", now=NOW)
    h1 = C.graph_hash(ua)
    h2 = C.graph_hash(json.loads(json.dumps(ua, ensure_ascii=True)))
    check("non-ASCII var / emoji source id: hash stable across (de)serialization", h1 == h2 and len(h1) == 64)
    check("canonical_json keeps non-ASCII as-is", "données" in C.canonical_json(ua["nodes"][0]["writes"]))
    check("hashes are sha256 hex of canonical projections",
          C.graph_hash(d) == __import__("hashlib").sha256(
              C.canonical_json(C.graph_projection(d)).encode("utf-8")).hexdigest())


# ─── round-trip helper ────────────────────────────────────────────────────────

_rt_stats = {"pairs": 0, "vm_pairs": 0, "fail": 0, "noop": 0}


def roundtrip(prev, new, label=""):
    """apply(prev, diff(prev,new)) == new for the stored pair and the VM pair."""
    if prev is None:
        return True
    ok = True
    delta = C.diff_docs(prev, new)
    out = C.apply_delta(prev, delta)
    if out != new:
        ok = False
    pv, nv = dag.strip_for_vm(prev), dag.strip_for_vm(new)
    vdelta = C.diff_docs(pv, nv)
    vout = C.apply_delta(pv, vdelta)
    if vout != nv:
        ok = False
    # The VM path ignores runtime/pending keys of the full delta.
    vfull = {k: v for k, v in delta.items() if k not in ("runtime", "pending")}
    if C.apply_delta(pv, vfull) != nv:
        ok = False
    _rt_stats["pairs"] += 1
    _rt_stats["vm_pairs"] += 1
    if not ok:
        _rt_stats["fail"] += 1
        if _rt_stats["fail"] <= 3:
            print(f"    round-trip failed: {label}")
    return ok


# ─── (c) random property test ─────────────────────────────────────────────────

VARS = ["v0", "v1", "v2", "v3", "v4", "données"]


def rand_code(rng):
    a, b = rng.choice(VARS), rng.choice(VARS)
    return rng.choice([
        f"{a} = 1",
        f"{a} = {b} + 1",
        f"{a}.append({b})",
        f"print({a})",
        f"{a}, {b} = {b}, {a}",
        f"{a} += 1",
        f"exec('{a} = 2')",
    ])


def rand_cell(rng, cid):
    if rng.random() < 0.2:
        a = rng.choice(VARS[:5])
        ov = rng.choice([None, "q_" + rng.choice(VARS[:5])])
        return cell(cid, f"SELECT * FROM t WHERE c = '{{{a}}}'", "sql", **({"outputVariable": ov} if ov else {}))
    if rng.random() < 0.08:
        return cell(cid, "# notes", "markdown")
    return cell(cid, rand_code(rng))


def rand_vars(rng, clock, cells):
    out = {}
    code_ids = [c["id"] for c in cells if c.get("type") != "markdown"] or ["c0"]
    for v in rng.sample(VARS, rng.randint(0, len(VARS))):
        cid = rng.choice(code_ids)
        out[v] = {"defined_by": cid, "defined_at": rng.randint(0, clock), "modified_by": [],
                  "last_cell": rng.choice(code_ids), "last_exec": clock}
    return out


def random_sequence(seed, steps=14):
    rng = random.Random(seed)
    row = Row()
    next_id = [0]

    def new_id():
        next_id[0] += 1
        return f"c{next_id[0]}"
    cells = [rand_cell(rng, new_id()) for _ in range(rng.randint(1, 6))]
    vm = "vm-a"
    clock = 0
    doc = dag.apply_cells(None, cells, trigger="open", now=NOW)
    prev, cur, *_ = row.write(doc)
    ok = True
    for step in range(steps):
        op = rng.choice(["edit", "insert", "delete", "reorder", "sqlov", "run", "run", "run", "fail",
                         "unsaved", "runtime", "vmswitch", "reset", "cold", "noop", "reconcile"])
        base = row.doc
        if op == "edit" and cells:
            i = rng.randrange(len(cells))
            cells[i] = dict(cells[i], code=rand_code(rng)) if cells[i]["type"] == "code" else rand_cell(rng, cells[i]["id"])
            new = dag.apply_cells(base, cells, trigger="save", now=NOW)
        elif op == "insert":
            cells.insert(rng.randint(0, len(cells)), rand_cell(rng, new_id()))
            new = dag.apply_cells(base, cells, trigger="save", now=NOW)
        elif op == "delete" and cells:
            cells.pop(rng.randrange(len(cells)))
            new = dag.apply_cells(base, cells, trigger="save", now=NOW)
        elif op == "reorder":
            rng.shuffle(cells)
            new = dag.apply_cells(base, cells, trigger="save", now=NOW)
        elif op == "sqlov":
            sql = [i for i, c in enumerate(cells) if c["type"] == "sql"]
            if sql:
                i = rng.choice(sql)
                cells[i] = dict(cells[i], outputVariable="o_" + rng.choice(VARS[:5]))
            new = dag.apply_cells(base, cells, trigger="save", now=NOW)
        elif op in ("run", "fail"):
            runnable = [c for c in cells if c["type"] != "markdown"]
            if not runnable:
                continue
            batch = rng.sample(runnable, rng.randint(1, len(runnable)))
            clock += 1
            new = dag.apply_runs(base, [sim_run(c, ok=(op == "run" or rng.random() < 0.5)) for c in batch],
                                 rt(clock, rand_vars(rng, clock, cells)), vm, trigger="execute", now=NOW)
        elif op == "unsaved":
            c = rand_cell(rng, new_id())
            if c["type"] == "markdown":
                continue
            clock += 1
            new = dag.apply_runs(base, [sim_run(c)], rt(clock), vm, trigger="execute", now=NOW)
            if rng.random() < 0.6:
                row.write(new)
                if not roundtrip(base, row.doc, f"seed {seed} step {step} unsaved"):
                    ok = False
                base = row.doc
                cells.append(c)
                new = dag.apply_cells(base, cells, trigger="save", now=NOW)
        elif op == "runtime":
            clock += 1
            new = dag.apply_runs(base, [], rt(clock, rand_vars(rng, clock, cells)), vm, trigger="runtime", now=NOW)
        elif op == "vmswitch":
            vm = f"vm-{rng.randint(0, 99)}"
            new = dag.apply_runtime(base, rt(clock, rand_vars(rng, clock, cells)), vm, trigger="attach")
        elif op == "reset":
            clock = rng.randint(0, max(0, clock - 1))
            new = dag.apply_runs(base, [], rt(clock), vm, trigger="runtime", now=NOW)
        elif op == "cold":
            vm = f"vm-cold-{rng.randint(0, 99)}"
            clock = 0
            new = dag.apply_runtime(base, rt(0), vm, trigger="attach", empty_namespace=True,
                                    attach_seq=base.get("run_seq"))
        elif op == "reconcile":
            report = {"origin": "restored", "runtime": rt(clock, rand_vars(rng, clock, cells)),
                      "runtime_source": rng.choice(["dag.json", "provenance.json", None]),
                      "consistent": rng.random() < 0.5}
            new = dag.reconcile_restored(base, report, vm, now=NOW, attach_seq=base.get("run_seq"))
        else:
            new = copy.deepcopy(base)
        prev, cur, wrote, *_ = row.write(new)
        if not wrote:
            _rt_stats["noop"] += 1
            d = C.diff_docs(prev, cur)
            if any(k in d for k in ("nodes_upsert", "nodes_remove", "edges_add", "edges_remove", "issues",
                                    "clocks", "runtime", "pending", "graph_set", "graph_unset")):
                ok = False
        if not roundtrip(prev, cur, f"seed {seed} step {step} op {op}"):
            ok = False
    return ok


def t_property():
    print("\n(c) round-trip property: apply(prev, diff(prev, new)) == new")
    results = [random_sequence(seed) for seed in range(320)]
    check(f"320 random sequences: every stamped pair round-trips "
          f"({_rt_stats['pairs']} pairs, {_rt_stats['noop']} no-op writes)", all(results))
    check("... and every strip_for_vm pair (incl. the VM path ignoring runtime/pending)",
          _rt_stats["fail"] == 0 and _rt_stats["vm_pairs"] == _rt_stats["pairs"])
    check("the generator produced no-op writes (change check exercised)", _rt_stats["noop"] > 0)


# ─── (d) the real 44-cell fixture ─────────────────────────────────────────────

def _real_module():
    spec = importlib.util.spec_from_file_location("_real_nb", os.path.join(ROOT, "tests", "dag", "test_dag_real_notebook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def t_real_notebook():
    print("\n(d) real 44-cell fixture (AWS Data Sources)")
    real = _real_module()
    rows, cells = real._load()
    row = Row()
    ok = True
    _p, d, *_ = row.write(dag.apply_cells(None, cells, trigger="open", now=NOW))
    code_rows = [r for r in rows if r["type"] in ("code", "sql")]
    clock = 0
    sizes = []
    for r in code_rows:
        clock += 1
        prev, d, wrote, *_ = row.write(dag.apply_runs(row.doc, [real._capture(r)], rt(clock), "vm-1",
                                                      trigger="execute", now=NOW))
        ok = roundtrip(prev, d, f"run {r['id']}") and ok
        sizes.append(C.json_bytes(C.diff_docs(prev, d)))
    check(f"run every cell in order ({len(code_rows)} runs): every step round-trips", ok)
    # leaf: a cell no edge leaves from
    has_out = {e["from"] for e in row.doc["edges"]}
    leaf = next(r for r in reversed(code_rows) if r["id"] not in has_out)
    deltas = []
    for _ in range(2):
        clock += 1
        prev, d, wrote, g_changed, *_ = row.write(dag.apply_runs(row.doc, [real._capture(leaf)], rt(clock), "vm-1",
                                                                 trigger="execute", now=NOW))
        deltas.append(C.diff_docs(prev, d))
        ok = roundtrip(prev, d, "leaf") and ok
    check("leaf re-run twice: second re-run keeps version (unchanged: true) and carries clocks/runtime only",
          deltas[1]["unchanged"] is True and deltas[1]["version"] == deltas[1]["base_version"]
          and set(deltas[1]) & {"nodes_upsert", "edges_add", "edges_remove", "issues"} == set()
          and "clocks" in deltas[1])
    full_bytes = C.json_bytes(row.doc)
    check(f"leaf re-run delta ({C.json_bytes(deltas[1])} B) is far smaller than the full doc ({full_bytes} B)",
          C.json_bytes(deltas[1]) * 10 < full_bytes)
    # edit a cell's reads: pick a code cell that reads something and add a read.
    target = next(c for c in cells if c["type"] == "code" and "=" in c["code"])
    edited = [dict(c, code=c["code"] + "\n_extra_probe = len(str(products360))") if c is target else c
              for c in cells]
    prev, d, wrote, g_changed, *_ = row.write(dag.apply_cells(row.doc, edited, trigger="save", now=NOW))
    ok_edit = roundtrip(prev, d, "edit") and wrote and g_changed
    check("edit changing a cell's reads: graph changes, round-trips", ok_edit)
    clock += 1
    rr = dict(next(r for r in rows if r["id"] == target["id"]))
    rr["code"] = next(c for c in edited if c["id"] == target["id"])["code"]
    prev, d, *_ = row.write(dag.apply_runs(row.doc, [real._capture(rr)], rt(clock), "vm-1",
                                           trigger="execute", now=NOW))
    check("re-run of the edited cell round-trips", roundtrip(prev, d, "rerun edited"))


# ─── (e) (f) (g) (h) ──────────────────────────────────────────────────────────

def t_mismatch_marker_edges_guard():
    print("\n(e)(f)(g)(h) mismatch, unchanged marker, edge identity, size guard")
    d0, cells = _base_doc()
    row = Row()
    _p, a, *_ = row.write(d0)
    _p, b, *_ = row.write(dag.apply_runs(a, [sim_run(cells[0])], rt(4), "vm-1", trigger="execute", now=NOW))
    delta = C.diff_docs(a, b)
    try:
        C.apply_delta(b, delta)
        check("base mismatch raises DeltaMismatch('base')", False)
    except C.DeltaMismatch as e:
        check("base mismatch raises DeltaMismatch('base')", e.reason == "base")
    bad = copy.deepcopy(delta)
    bad["nodes_upsert"][0]["reads"] = ["tampered"]
    try:
        C.apply_delta(a, bad)
        check("tampered node raises DeltaMismatch('hash')", False)
    except C.DeltaMismatch as e:
        check("tampered node raises DeltaMismatch('hash')", e.reason == "hash")
    a_before, delta_before = row_snapshot(a), row_snapshot(delta)
    out = C.apply_delta(a, delta)
    out["nodes"][0]["reads"].append("mutated-after")
    check("apply_delta does not mutate (or alias) its inputs",
          row_snapshot(a) == a_before and row_snapshot(delta) == delta_before
          and C.apply_delta(a, delta) == b)

    m = C.diff_docs(b, b)
    check("unchanged pair → marker: unchanged, base == target, no ops",
          m["unchanged"] is True and (m["base_version"], m["base_runtime_version"]) == (m["version"], m["runtime_version"])
          and set(m) == {"delta", "schema", "base_version", "base_runtime_version", "version", "runtime_version",
                         "graph_hash", "runtime_hash", "unchanged", "notebook_id", "built_at", "trigger"})
    check("unchanged marker is small (< 400 B)", C.json_bytes(m) < 400)
    _p, c, *_ = row.write(dag.apply_runs(b, [sim_run(cells[2])], rt(5), "vm-1", trigger="execute", now=NOW))
    _p, c2, *_ = row.write(dag.apply_runs(c, [sim_run(cells[2])], rt(6), "vm-1", trigger="execute", now=NOW))
    leaf = C.diff_docs(c, c2)
    check("leaf re-run → unchanged: true plus clocks and runtime only",
          leaf["unchanged"] is True and "clocks" in leaf and "runtime" in leaf
          and not any(k in leaf for k in ("nodes_upsert", "nodes_remove", "edges_add", "edges_remove", "issues")))
    check("leaf clocks list only the re-run node", list(leaf["clocks"]["nodes"]) == ["c"])
    check("delta_has_vm_changes: clocks yes, runtime-only no, marker no",
          C.delta_has_vm_changes(leaf) and not C.delta_has_vm_changes(m)
          and not C.delta_has_vm_changes({**m, "runtime": {"set": {"clock": 1}}}))

    # (g) same identity, different use
    p = copy.deepcopy(c2)
    n = copy.deepcopy(c2)
    n["edges"][0]["use"] = "mutate" if n["edges"][0]["use"] == "read" else "read"
    n["graph_hash"] = C.graph_hash(n)
    n["version"] = p["version"] + 1
    dd = C.diff_docs(p, n)
    ident = list(C._edge_id(n["edges"][0]))
    check("same-identity edge with changed use: in edges_remove and edges_add",
          ident in dd.get("edges_remove", []) and n["edges"][0] in dd.get("edges_add", []))
    check("... and applies back to the new doc", C.apply_delta(p, dd) == n)

    # (h) size guard
    check("send_full: no full size → full", C.send_full(10, None) is True)
    check("send_full: delta ≤ ratio·full → delta", C.send_full(50, 100) is False and C.send_full(49, 100) is False)
    check("send_full: delta > ratio·full → full", C.send_full(51, 100) is True)
    check("DELTA_FULL_RATIO == 0.5 and DAG_DELTA_FORMAT == 1", C.DELTA_FULL_RATIO == 0.5 and C.DAG_DELTA_FORMAT == 1)


def row_snapshot(d):
    return json.loads(json.dumps(d))


def t_fixture_cases():
    print("\nfixtures: tests/fixtures/dag_deltas.json")
    path = os.path.join(ROOT, "tests", "fixtures", "dag_deltas.json")
    with open(path, encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]
    bad = [k for k, c in cases.items() if C.apply_delta(c["prev"], c["delta"]) != c["next"]
           or C.graph_hash(c["next"]) != c["graph_hash"]]
    check(f"every fixture case applies to `next` and matches graph_hash ({len(cases)} cases)", not bad)
    check("fixture is current (diff_docs(prev, next) == stored delta)",
          all(C.diff_docs(c["prev"], c["next"]) == c["delta"] for c in cases.values()))


ALL = [t_hashes, t_property, t_real_notebook, t_mismatch_marker_edges_guard, t_fixture_cases]


def test_dag_delta():
    """pytest entry point."""
    _checks.clear()
    for k in _rt_stats:
        _rt_stats[k] = 0
    for t in ALL:
        t()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    for t in ALL:
        t()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
