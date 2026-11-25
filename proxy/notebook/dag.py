"""
Persisted per-notebook DAG — the pure builder (design.md §3).

Part of: proxy.notebook

Turns each cell's static facts (app.notebook.static_analysis) and its latest
captured runtime facts into ONE JSON-serialisable document per notebook: nodes
(one per code/SQL cell), edges (nearest-earlier producer per read variable),
issues, the VM-owned `runtime` section and `pending` captures for cells that
are not saved yet. This module is the only place that infers dependencies.

Pure: stdlib + app.notebook.static_analysis + app.notebook.dag_contract only.
No I/O and no wall-clock reads — `now` is injected (ISO-8601 UTC, ms, `Z`).
Every `apply_*` deep-copies `prev` (never mutates its input), updates node
facts and calls `finalize`. Storage stamps `notebook_id` / `version` /
`runtime_version` / `graph_hash` / `runtime_hash` / `built_at` afterwards and
skips the write when the graph and runtime hashes did not change
(proxy/storage/_dag_doc.py::stamp_doc, app/notebook/dag_contract.py).
"""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from app.notebook.dag_contract import DAG_SCHEMA, cell_code_hash
from app.notebook.static_analysis import analyze_cell, derive_sql_output_variable

SCHEMA = DAG_SCHEMA

# Source kinds that raise an `external_state` issue (brief: url / s3 / file).
EXTERNAL_STATE_KINDS = frozenset({"url", "s3", "local"})

# Pending captures for a cell id that never got saved are dropped after this.
PENDING_TTL_SECONDS = 120

_STATIC_CACHE_SIZE = 2048


# ─── Run captures ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunCapture:
    """One captured execution (proxy side, from the /execute* response).

    `code` is used transiently (hash + static facts of the executed code) and
    is never written into the document."""
    cell_id: str
    cell_type: str
    code: str
    output_variable: str | None
    ok: bool
    reads: tuple
    created: tuple
    rebound: tuple
    mutated: tuple
    sources: tuple | None
    executed_at: str

    @property
    def code_hash(self) -> str:
        return cell_code_hash(self.cell_type, self.code)


# ─── Document skeleton ─────────────────────────────────────────────────────────

def empty_doc() -> dict:
    return {
        "schema": SCHEMA,
        "version": 0,
        "built_at": None,
        "run_seq": 0,
        "nodes": [],
        "edges": [],
        "issues": [],
        "runtime": {"clock": 0, "vars": {}, "runs": {}},
        "pending": {},
    }


def _start(prev: dict | None, trigger: str) -> dict:
    doc = copy.deepcopy(prev) if isinstance(prev, dict) and prev.get("schema") == SCHEMA else empty_doc()
    for key, default in empty_doc().items():
        if key not in doc:
            doc[key] = copy.deepcopy(default)
    if not isinstance(doc.get("runtime"), dict):
        doc["runtime"] = {"clock": 0, "vars": {}, "runs": {}}
    if not isinstance(doc.get("pending"), dict):
        doc["pending"] = {}
    doc["trigger"] = trigger
    return doc


def _new_node(cell_id: str, position: int, cell_type: str, output_variable, explicit: bool,
              code_hash: str) -> dict:
    """A node with every schema key in document order (byte-stable JSON)."""
    return {
        "cell_id": cell_id,
        "position": position,
        "cell_type": cell_type,
        "output_variable": output_variable,
        "output_variable_explicit": explicit,
        "code_hash": code_hash,
        "run_hash": None,
        "run_output_variable": None,
        "facts_hash": code_hash,
        "facts_origin": "static",
        "reads": [],
        "writes": [],
        "mutates": [],
        "sources": None,
        "dynamic": [],
        "level": 0,
        "status": "never_run",
        "stale_reason": None,
        "executed_at": None,
        "last_run_seq": None,
        "last_ok_seq": None,
        "last_run_ok": None,
        "not_in_checkpoint": False,
    }


_FACT_KEYS = ("facts_hash", "facts_origin", "reads", "writes", "mutates", "sources", "dynamic")
_RUN_KEYS = ("run_hash", "run_output_variable", "executed_at", "last_run_seq", "last_ok_seq",
             "last_run_ok", "not_in_checkpoint")


def _copy_keys(dst: dict, src: dict, keys) -> None:
    for k in keys:
        if k in src:
            dst[k] = copy.deepcopy(src[k])


# ─── Facts ─────────────────────────────────────────────────────────────────────

_static_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _static_facts(cell_type: str, code: str, output_variable) -> dict:
    """Static facts of `code`, memoised by (type, code_hash, output_variable).
    Returns fresh lists: {reads, writes, mutates, dynamic, mutated}."""
    t = "sql" if cell_type == "sql" else "code"
    key = (t, cell_code_hash(t, code), output_variable)
    hit = _static_cache.get(key)
    if hit is None:
        a = analyze_cell(t, code or "", output_variable) if t == "sql" else analyze_cell(t, code or "")
        reads = set(a["reads"])
        mutated = set(a["mutated"])
        hit = {
            "reads": tuple(sorted(reads)),
            "writes": tuple(sorted(set(a["writes"]))),
            "mutates": tuple(sorted(mutated & reads)),
            "mutated": tuple(sorted(mutated)),
            "dynamic": tuple(sorted(set(a.get("dynamic") or []))),
        }
        _static_cache[key] = hit
        if len(_static_cache) > _STATIC_CACHE_SIZE:
            _static_cache.popitem(last=False)
    else:
        _static_cache.move_to_end(key)
    return {k: list(v) for k, v in hit.items()}


def _sql_output_variable(cell: dict):
    explicit = cell.get("outputVariable")
    if isinstance(explicit, str) and explicit:
        return explicit, True
    return derive_sql_output_variable(cell.get("code") or ""), False


def _set_static(node: dict, code: str) -> None:
    f = _static_facts(node["cell_type"], code, node["output_variable"])
    node["facts_hash"] = node["code_hash"]
    node["facts_origin"] = "static"
    node["reads"], node["writes"], node["mutates"], node["dynamic"] = (
        f["reads"], f["writes"], f["mutates"], f["dynamic"])
    node["sources"] = None


def _names(values) -> set:
    return {v for v in (values or ()) if isinstance(v, str) and v}


def _run_output_variable(run: RunCapture):
    """The variable a SQL run wrote: the request's output variable (the SQL
    engine default is 'result'); None for code cells and failed SQL runs."""
    if run.cell_type != "sql" or not run.ok:
        return None
    return run.output_variable or "result"


def _run_facts(run: RunCapture) -> dict:
    """§3.2 facts of one captured run (successful → runtime, failed → static)."""
    is_sql = run.cell_type == "sql"
    static = _static_facts("sql" if is_sql else "code", run.code,
                           (run.output_variable or "result") if is_sql else None)
    if not run.ok:
        return {"facts_origin": "static", "reads": static["reads"], "writes": static["writes"],
                "mutates": static["mutates"], "dynamic": static["dynamic"]}
    reads = _names(run.reads)
    if is_sql:
        ov = _run_output_variable(run)
        writes = {ov} if ov and ov.isidentifier() else set()
        mutates: set = set()
    else:
        rebound = _names(run.rebound)
        mutates = (_names(run.mutated) | (rebound & set(static["mutated"]))) & reads
        writes = _names(run.created) | rebound | mutates
    return {"facts_origin": "runtime", "reads": sorted(reads), "writes": sorted(writes),
            "mutates": sorted(mutates), "dynamic": static["dynamic"]}


def normalize_sources(v) -> list[dict] | None:
    """Captured sources → [{kind, id}] string pairs, de-duplicated, order kept,
    no length truncation. None (or a non-list) = not captured."""
    if v is None or not isinstance(v, (list, tuple)):
        return None
    out, seen = [], set()
    for s in v:
        if not isinstance(s, dict):
            continue
        kind, sid = s.get("kind"), s.get("id")
        if not isinstance(kind, str) or not isinstance(sid, str) or not kind or not sid:
            continue
        if (kind, sid) in seen:
            continue
        seen.add((kind, sid))
        out.append({"kind": kind, "id": sid})
    return out


def _apply_run_to_node(node: dict, run: RunCapture, facts: dict, seq: int) -> None:
    rh = run.code_hash
    node["facts_hash"] = rh
    node["run_hash"] = rh
    node["facts_origin"] = facts["facts_origin"]
    for k in ("reads", "writes", "mutates", "dynamic"):
        node[k] = list(facts[k])
    node["sources"] = normalize_sources(run.sources)
    node["run_output_variable"] = _run_output_variable(run)
    node["executed_at"] = run.executed_at
    node["last_run_seq"] = seq
    if run.ok:
        node["last_ok_seq"] = seq
    node["last_run_ok"] = bool(run.ok)
    node["not_in_checkpoint"] = False


# ─── Time ──────────────────────────────────────────────────────────────────────

def _parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─── apply_cells ───────────────────────────────────────────────────────────────

def apply_cells(prev: dict | None, cells: list[dict], *, trigger: str, now: str) -> dict:
    """Rebuild nodes from the stored cell list (open / create / save / version
    restore), keeping runtime facts whose code hash still matches (§3.3).

    `cells` use the storage cell-mapper keys: id, type, code, outputVariable,
    lastExecutedCode, error, executedAt. Markdown cells are not nodes, but
    `position` is the index in the FULL list."""
    doc = _start(prev, trigger)
    prev_nodes = {n.get("cell_id"): n for n in doc.get("nodes") or [] if isinstance(n, dict)}
    pending = doc["pending"]
    nodes = []
    seen_ids = set()
    for idx, cell in enumerate(cells or []):
        ctype = cell.get("type") or "code"
        if ctype == "markdown":
            continue
        cid = str(cell.get("id"))
        seen_ids.add(cid)
        code = cell.get("code") or ""
        is_sql = ctype == "sql"
        ctype = "sql" if is_sql else "code"
        if is_sql:
            ov, explicit = _sql_output_variable(cell)
        else:
            ov, explicit = None, False
        ch = cell_code_hash(ctype, code)
        node = _new_node(cid, idx, ctype, ov, explicit, ch)
        p = prev_nodes.get(cid)
        pend = pending.get(cid) if isinstance(pending.get(cid), dict) else None

        if p is not None and p.get("facts_origin") == "runtime" and p.get("facts_hash") == ch:
            # Step 2: runtime facts survive unless an explicit SQL output variable
            # differs from the one the last run wrote.
            _copy_keys(node, p, _RUN_KEYS)
            if not is_sql or not explicit or ov == p.get("run_output_variable"):
                _copy_keys(node, p, _FACT_KEYS)
            else:
                _set_static(node, code)
        elif (p is not None and p.get("facts_origin") == "static" and p.get("facts_hash") == ch
              and p.get("output_variable") == ov):
            # Step 3: unchanged static facts, no re-analysis.
            _copy_keys(node, p, _FACT_KEYS)
            _copy_keys(node, p, _RUN_KEYS)
        elif pend is not None and pend.get("code_hash") == ch:
            # Step 4: adopt a capture that ran before the cell was saved.
            if p is not None:
                _copy_keys(node, p, _RUN_KEYS)
            ok = bool(pend.get("ok"))
            facts = pend.get("facts") or {}
            node["facts_hash"] = ch
            node["run_hash"] = ch
            node["facts_origin"] = "runtime" if ok else "static"
            for k in ("reads", "writes", "mutates", "dynamic"):
                node[k] = sorted(_names(facts.get(k)))
            node["sources"] = normalize_sources(pend.get("sources"))
            node["run_output_variable"] = pend.get("output_variable")
            node["last_run_seq"] = pend.get("run_seq")
            if ok:
                node["last_ok_seq"] = pend.get("run_seq")
            node["last_run_ok"] = ok
            node["executed_at"] = pend.get("executed_at")
            node["not_in_checkpoint"] = False
            del pending[cid]
        elif p is not None:
            # Step 5: code changed → static facts, run bookkeeping kept (→ edited).
            _set_static(node, code)
            _copy_keys(node, p, _RUN_KEYS)
        else:
            # Step 6: brand-new node; seed run history from lastExecutedCode.
            _set_static(node, code)
            last = cell.get("lastExecutedCode")
            if isinstance(last, str) and last:
                node["run_hash"] = cell_code_hash(ctype, last)
                node["last_run_seq"] = 0
                node["last_ok_seq"] = 0
                node["last_run_ok"] = not cell.get("error")
                node["executed_at"] = cell.get("executedAt")
        nodes.append(node)
    doc["nodes"] = nodes

    # Expire pending captures whose cell never got saved.
    now_dt = _parse_ts(now)
    for cid in list(pending):
        if cid in seen_ids:
            continue
        cap = _parse_ts((pending[cid] or {}).get("captured_at")) if isinstance(pending[cid], dict) else None
        if cap is None or (now_dt is not None and (now_dt - cap).total_seconds() > PENDING_TTL_SECONDS):
            del pending[cid]
    return finalize(doc)


# ─── apply_runs ────────────────────────────────────────────────────────────────

def apply_runs(prev: dict | None, runs: list[RunCapture], runtime: dict | None,
               vm_id: str | None, *, trigger: str, now: str) -> dict:
    """Record captured runs (§3.2). A run for a cell id that is not a node goes
    to `pending` (adopted by the next apply_cells whose code hash matches).
    `runtime`, when given, replaces the runtime section (vm_id stamped)."""
    doc = _start(prev, trigger)
    by_id = {n["cell_id"]: n for n in doc["nodes"]}
    for run in runs or []:
        doc["run_seq"] = int(doc.get("run_seq") or 0) + 1
        seq = doc["run_seq"]
        facts = _run_facts(run)
        cid = str(run.cell_id)
        node = by_id.get(cid)
        if node is not None:
            _apply_run_to_node(node, run, facts, seq)
        else:
            doc["pending"][cid] = {
                "captured_at": now,
                "cell_type": "sql" if run.cell_type == "sql" else "code",
                "code_hash": run.code_hash,
                "output_variable": _run_output_variable(run),
                "run_seq": seq,
                "ok": bool(run.ok),
                "facts": {k: list(facts[k]) for k in ("reads", "writes", "mutates", "dynamic")},
                "sources": normalize_sources(run.sources),
                "executed_at": run.executed_at,
            }
    if runtime is not None:
        doc["runtime"] = {**copy.deepcopy(runtime), "vm_id": vm_id}
    return finalize(doc)


# ─── apply_runtime / reconcile_restored ────────────────────────────────────────

def _clock(rt) -> int:
    try:
        return int((rt or {}).get("clock") or 0)
    except (TypeError, ValueError):
        return 0


def _merge_runtime(doc: dict, prev_rt: dict, runtime: dict, vm_id) -> None:
    """Replace doc.runtime with the report's, unless prev comes from the same VM
    with a higher clock (a capture landed during the attach)."""
    if (isinstance(prev_rt, dict) and prev_rt.get("vm_id") == vm_id
            and _clock(prev_rt) > _clock(runtime)):
        doc["runtime"] = copy.deepcopy(prev_rt)
    else:
        doc["runtime"] = {**copy.deepcopy(runtime or {}), "vm_id": vm_id}


def _may_mark(node: dict, attach_seq) -> bool:
    """Never mark a node that ran after the attach read the VM report."""
    seq = node.get("last_run_seq")
    if seq is None:
        return False
    return attach_seq is None or seq <= attach_seq


def is_cold_attach(prev: dict | None, report: dict, vm_id: str | None) -> bool:
    """§3.7 cold-attach condition, evaluated on `prev` (inside the RMW): the VM
    restored nothing (`origin == "empty"`, runtime clock 0) and the document's
    runtime came from a different VM."""
    prev_rt = (prev or {}).get("runtime") or {}
    return ((report or {}).get("origin") == "empty"
            and _clock((report or {}).get("runtime")) == 0
            and prev_rt.get("vm_id") != vm_id)


def apply_runtime(prev: dict | None, runtime: dict, vm_id: str | None, *, trigger: str,
                  empty_namespace: bool = False, attach_seq: int | None = None) -> dict:
    """Replace the runtime section (runtime affects fallback owners) and
    re-finalize. `empty_namespace=True` (cold attach): every node that ran and
    writes something is marked `not_in_checkpoint` — only when prev's runtime
    is from another VM, and never for nodes with last_run_seq > attach_seq."""
    prev_rt = (prev or {}).get("runtime") if isinstance(prev, dict) else None
    cold = empty_namespace and (prev_rt or {}).get("vm_id") != vm_id
    doc = _start(prev, trigger)
    _merge_runtime(doc, prev_rt, runtime, vm_id)
    if cold:
        for n in doc["nodes"]:
            if n.get("writes") and _may_mark(n, attach_seq):
                n["not_in_checkpoint"] = True
    return finalize(doc)


def _provenance_in(node: dict, vars_: dict) -> bool:
    writes = node.get("writes") or []
    if not writes:
        return True
    cid = node["cell_id"]
    for v in writes:
        info = vars_.get(v)
        if not isinstance(info, dict):
            continue
        touched = {info.get("defined_by"), info.get("last_cell")} | set(info.get("modified_by") or [])
        if cid in touched:
            return True
    return False


def reconcile_restored(prev: dict | None, report: dict, vm_id: str, *, now: str,
                       attach_seq: int | None = None) -> dict:
    """After a VM restore: take the runtime from the VM `GET /dag` report and
    decide per node whether its effects are in the restored namespace (§3.7
    matrix). Nodes not in → `not_in_checkpoint = true`. `now` is accepted for
    API symmetry (no time-dependent rule applies)."""
    report = report or {}
    prev_rt = (prev or {}).get("runtime") if isinstance(prev, dict) else None
    doc = _start(prev, "reconcile")
    _merge_runtime(doc, prev_rt, report.get("runtime") or {}, vm_id)
    rt = doc["runtime"]
    vars_ = rt.get("vars") if isinstance(rt.get("vars"), dict) else {}
    runs = rt.get("runs") if isinstance(rt.get("runs"), dict) else {}
    source = report.get("runtime_source")
    consistent = bool(report.get("consistent"))
    for n in doc["nodes"]:
        if not _may_mark(n, attach_seq):
            continue
        if source == "dag.json" and consistent:
            entry = runs.get(n["cell_id"])
            if isinstance(entry, dict):
                is_in = entry.get("code_hash") == n.get("run_hash")
            else:
                is_in = _provenance_in(n, vars_)
        elif source in ("dag.json", "provenance.json"):
            is_in = _provenance_in(n, vars_)
        else:
            is_in = not (n.get("writes") or [])
        n["not_in_checkpoint"] = not is_in
    return finalize(doc)


# ─── finalize: edges, levels, cycles, statuses, issues ────────────────────────

def _sorted_unique(values) -> list:
    return sorted({v for v in (values or []) if isinstance(v, str)})


def _tarjan(ids: list, succ: dict) -> list:
    """Iterative Tarjan SCC; returns the list of components (lists of ids)."""
    index, low, on_stack, stack, comps = {}, {}, set(), [], []
    counter = 0
    for root in ids:
        if root in index:
            continue
        work = [(root, iter(succ.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(succ.get(w, ()))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)
    return comps


def _base_status(n: dict) -> str:
    if n.get("last_run_seq") is None:
        return "never_run"
    if n.get("run_hash") != n.get("code_hash"):
        return "edited"
    # An explicit SQL output variable that differs from the one the last run
    # wrote is an edit; a run that recorded no output variable (failed SQL run,
    # history seeded from lastExecutedCode) cannot be compared.
    if (n.get("cell_type") == "sql" and n.get("output_variable_explicit")
            and n.get("run_output_variable") is not None
            and n.get("output_variable") != n.get("run_output_variable")):
        return "edited"
    if n.get("last_run_ok") is False:
        return "error"
    if n.get("not_in_checkpoint"):
        return "stale"
    return "current"


def _issue(cell_id, code, severity, var, related, source, detail) -> dict:
    return {"cell_id": cell_id, "code": code, "severity": severity, "var": var,
            "related": related, "source": source, "detail": detail}


def finalize(doc: dict) -> dict:
    """Recompute the derived parts of `doc` IN PLACE (sorted nodes, edges,
    levels, statuses, stale reasons, issues) deterministically; returns doc."""
    nodes = sorted(doc.get("nodes") or [], key=lambda n: n["position"])
    for n in nodes:
        for k in ("reads", "writes", "mutates", "dynamic"):
            n[k] = _sorted_unique(n.get(k))
        n["sources"] = normalize_sources(n.get("sources"))
    doc["nodes"] = nodes
    by_id = {n["cell_id"]: n for n in nodes}
    pos = {n["cell_id"]: n["position"] for n in nodes}
    rt = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
    rt_vars = rt.get("vars") if isinstance(rt.get("vars"), dict) else {}

    producers: dict = {}
    for n in nodes:
        for v in n["writes"]:
            producers.setdefault(v, []).append(n)

    edges, issues = [], []
    in_edges = {n["cell_id"]: [] for n in nodes}
    resolved: dict = {}  # (consumer id, var) → producer id
    for n in nodes:
        cid = n["cell_id"]
        for v in n["reads"]:
            prods = producers.get(v, [])
            earlier = [p for p in prods if p["position"] < n["position"]]
            backward = False
            src = earlier[-1] if earlier else None
            if src is None:
                info = rt_vars.get(v) if isinstance(rt_vars.get(v), dict) else {}
                lc = info.get("last_cell")
                if isinstance(lc, str) and lc in by_id and lc != cid:
                    src = by_id[lc]
                elif prods and prods[-1]["cell_id"] != cid:
                    src = prods[-1]
                if src is not None:
                    backward = True
                    issues.append(_issue(
                        cid, "out_of_order", "amber", v, [src["cell_id"]], None,
                        f"reads {v} from cell {src['cell_id']}, which comes after it in the notebook"))
                elif n["facts_origin"] == "runtime":
                    issues.append(_issue(
                        cid, "missing_producer", "amber", v, [], None,
                        f"reads {v}, which no cell in this notebook defines"))
            if src is None:
                continue
            e = {
                "from": src["cell_id"], "to": cid, "var": v,
                "kind": "mutates" if v in src["mutates"] else "defines",
                "use": "mutate" if v in n["mutates"] else "read",
                "origin": "runtime" if (src["facts_origin"] == "runtime"
                                        and n["facts_origin"] == "runtime") else "static",
                "backward": backward,
            }
            edges.append(e)
            in_edges[cid].append(e)
            resolved[(cid, v)] = src["cell_id"]
    edges.sort(key=lambda e: (pos[e["to"]], e["var"], pos[e["from"]]))
    doc["edges"] = edges

    # Levels (forward in-edges only; position order makes this well-defined).
    for n in nodes:
        fwd = [by_id[e["from"]]["level"] for e in in_edges[n["cell_id"]] if not e["backward"]]
        n["level"] = 1 + max(fwd) if fwd else 0

    # Cycles (Tarjan over all edges).
    succ: dict = {}
    for e in edges:
        succ.setdefault(e["from"], [])
        if e["to"] not in succ[e["from"]]:
            succ[e["from"]].append(e["to"])
    for comp in _tarjan([n["cell_id"] for n in nodes], succ):
        if len(comp) < 2:
            continue
        members = sorted(comp, key=lambda c: pos[c])
        for c in members:
            others = [m for m in members if m != c]
            issues.append(_issue(c, "cycle", "amber", None, others, None,
                                 f"is part of a dependency cycle with cells {', '.join(others)}"))

    # Statuses + staleness.
    def seq(n):
        s = n.get("last_ok_seq")
        return s if s is not None else -1

    base = {n["cell_id"]: _base_status(n) for n in nodes}
    reason: dict = {}
    for n in nodes:
        cid = n["cell_id"]
        if base[cid] == "stale" and n.get("not_in_checkpoint"):
            reason[cid] = {"kind": "not_in_checkpoint", "vars": [], "via": []}
            continue
        if base[cid] not in ("current", "edited"):
            continue
        vs, via = set(), set()
        for e in in_edges[cid]:
            if seq(by_id[e["from"]]) > seq(n):
                vs.add(e["var"])
                via.add(e["from"])
        if vs:
            reason[cid] = {"kind": "upstream_changed", "vars": sorted(vs),
                           "via": sorted(via, key=lambda c: pos[c])}
    out_edges: dict = {}
    for e in edges:
        out_edges.setdefault(e["from"], []).append(e["to"])
    seeds = [n["cell_id"] for n in nodes if n["cell_id"] in reason]
    queue, visited, reached = list(seeds), set(seeds), []
    while queue:
        cur = queue.pop(0)
        for nxt in out_edges.get(cur, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            if base[nxt] in ("current", "edited") and nxt not in reason:
                reached.append(nxt)
                queue.append(nxt)
    stale_set = set(reason) | set(reached)
    for cid in reached:
        via = sorted({e["from"] for e in in_edges[cid] if e["from"] in stale_set},
                     key=lambda c: pos[c])
        reason[cid] = {"kind": "upstream_stale", "vars": [], "via": via}
    for n in nodes:
        cid = n["cell_id"]
        st = base[cid]
        n["stale_reason"] = reason.get(cid)
        if st == "current" and cid in reason:
            st = "stale"
        n["status"] = st

    # Issues: cross-cell mutation, multiple definitions, external state, dynamic.
    readers = {v for n in nodes for v in n["reads"]}
    definers: dict = {}
    for n in nodes:
        for v in sorted(set(n["writes"]) - set(n["mutates"])):
            definers.setdefault(v, []).append(n["cell_id"])
    for n in nodes:
        cid = n["cell_id"]
        for v in n["mutates"]:
            p = resolved.get((cid, v))
            if p is not None and p != cid:
                issues.append(_issue(cid, "cross_cell_mutation", "info", v, [p], None,
                                     f"mutates {v} defined in cell {p}"))
    for v, ids in definers.items():
        if len(ids) < 2 or v not in readers:
            continue
        for i, cid in enumerate(ids[1:], start=1):
            earlier = ids[:i]
            issues.append(_issue(cid, "multiple_definitions", "info", v, list(earlier), None,
                                 f"redefines {v}, also defined in cell{'s' if len(earlier) > 1 else ''} "
                                 f"{', '.join(earlier)}"))
    for n in nodes:
        for s in n["sources"] or []:
            if s["kind"] in EXTERNAL_STATE_KINDS:
                issues.append(_issue(n["cell_id"], "external_state", "info", None, [], dict(s),
                                     f"reads {s['kind']} {s['id']}; the cell won't re-run when "
                                     f"the source changes"))
        if n["dynamic"]:
            issues.append(_issue(n["cell_id"], "dynamic_names", "amber", None, [], None,
                                 f"uses {', '.join(n['dynamic'])}; its reads and writes may be "
                                 f"incomplete"))
    issues.sort(key=lambda i: (pos[i["cell_id"]], i["code"], i["var"] or "",
                               (i["source"] or {}).get("id", "")))
    doc["issues"] = issues
    return doc


# ─── Helpers for callers ───────────────────────────────────────────────────────

def in_sync(doc: dict | None, cells: list[dict]) -> bool:
    """True if `doc` already reflects `cells`: same node ids in the same
    positions with the same code hash and output variable."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return False
    want = []
    for idx, cell in enumerate(cells or []):
        ctype = cell.get("type") or "code"
        if ctype == "markdown":
            continue
        is_sql = ctype == "sql"
        ov = _sql_output_variable(cell)[0] if is_sql else None
        want.append((str(cell.get("id")), idx,
                     cell_code_hash("sql" if is_sql else "code", cell.get("code") or ""), ov))
    have = [(n.get("cell_id"), n.get("position"), n.get("code_hash"), n.get("output_variable"))
            for n in doc.get("nodes") or [] if isinstance(n, dict)]
    return have == want


def strip_for_vm(doc) -> dict:
    """The document as pushed to the VM (PUT, or the PATCH delta base): without
    the runtime and pending sections. Every other key is kept, including the
    stamps (version, runtime_version, graph_hash, runtime_hash), so the VM copy
    and dag.json carry the revision the delta protocol compares."""
    return {k: copy.deepcopy(v) for k, v in (doc or {}).items() if k not in ("runtime", "pending")}
