"""
Shared notebook-DAG contract — stdlib only.

Part of: app.notebook (imported by the VM and by the proxy, the same way the
proxy imports app.notebook.static_analysis).

Holds what BOTH sides must agree on:
  * DAG_SCHEMA     — version of the persisted DAG document (proxy/notebook/dag.py).
  * DAG_MAX_BYTES  — one size cap for the storage write and the VM PUT/PATCH /dag.
  * cell_code_hash — identifies "the code a cell ran". Every caller passes the
    RAW cell text (DB code, request `code`/`sql`, batch cell `code`);
    normalisation happens only here, so the proxy (raw text) and the VM run
    ledger (the stripped SQL it executed) always produce the same hash.
  * The change check and the delta transport (the single Python home; the
    browser mirrors apply only, in web/src/services/dagDelta.js):
      - document partition: STAMP keys (never hashed), the RUNTIME channel
        (`runtime`, `pending`, `run_seq` and the per-node run clocks) and the
        GRAPH (everything else);
      - graph_hash / runtime_hash — sha256 of the canonical JSON of each part;
      - rev(doc) = (version, runtime_version), the write-order pair;
      - diff_docs(prev, new) → delta, apply_delta(doc, delta) → doc (pure;
        apply(prev, diff(prev, new)) == new);
      - send_full — the size guard (delta vs full document).
    Deltas are transport only: the DB row, the VM copy and dag.json always hold
    the FULL document.
"""

import copy
import json
from hashlib import sha256

DAG_SCHEMA = 1
DAG_MAX_BYTES = 8 * 1024 * 1024

# ─── Delta contract constants ──────────────────────────────────────────────────
DAG_DELTA_FORMAT = 1
# Send the full document instead of a delta when the delta is larger than this
# fraction of the full document (both measured as compact UTF-8 JSON bytes).
DELTA_FULL_RATIO = 0.5

# Never hashed: storage/transport stamps. `checkpoint` ({id, saved_at,
# session_id, microvm_id}) is the stamp the VM adds to dag.json at save time;
# a restored copy carries it, and it must not change the graph hash.
STAMP_KEYS = ("version", "built_at", "notebook_id", "trigger",
              "graph_hash", "runtime_version", "runtime_hash", "checkpoint")
# Top-level keys of the runtime channel (hashed by runtime_hash).
RUNTIME_TOP_KEYS = ("runtime", "pending", "run_seq")
# Per-node run clocks (runtime channel). Their consumer-visible effects
# (status, stale_reason, levels, issues) are derived by finalize and stay GRAPH.
CLOCK_NODE_KEYS = ("executed_at", "last_run_seq", "last_ok_seq")
# Keys of the graph handled by dedicated delta ops.
_GRAPH_LIST_KEYS = ("nodes", "edges", "issues")
_EXCLUDED_TOP = frozenset(STAMP_KEYS) | frozenset(RUNTIME_TOP_KEYS)
# Delta op keys that change the graph / the VM copy.
_GRAPH_OP_KEYS = ("nodes_upsert", "nodes_remove", "edges_add", "edges_remove",
                  "issues", "graph_set", "graph_unset")


def executed_text(cell_type: str, code: str) -> str:
    """The text the VM actually runs: /execute-sql and the batch SQL path strip
    the statement; Python code runs unstripped (whitespace is significant)."""
    return code.strip() if cell_type == "sql" else code


def cell_code_hash(cell_type: str, code: str) -> str:
    """sha256 hex of "<type>\\n<executed text>", type being 'sql' or 'code'.
    The SQL output variable is deliberately not part of the hash."""
    t = "sql" if cell_type == "sql" else "code"
    return sha256(f"{t}\n{executed_text(t, code or '')}".encode()).hexdigest()


# ─── Projections and hashes ───────────────────────────────────────────────────

def canonical_json(v) -> str:
    """Deterministic JSON: sorted keys, compact, non-ASCII kept as-is.

    The graph holds only ints, bools, strings and null (no floats), and every
    graph key is an ASCII schema name, so the browser's canonicalJson produces
    the same text. Should a float ever appear, the JS hash may differ; the
    browser then resyncs (fails safe)."""
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def _sha(v) -> str:
    return sha256(canonical_json(v).encode("utf-8", "surrogatepass")).hexdigest()


def _node_graph(node) -> dict:
    if not isinstance(node, dict):
        return node
    return {k: copy.deepcopy(v) for k, v in node.items() if k not in CLOCK_NODE_KEYS}


def graph_projection(doc) -> dict:
    """The GRAPH part: a deep copy without the STAMP and RUNTIME top-level keys
    and without each node's run clocks."""
    out = {}
    for k, v in (doc or {}).items():
        if k in _EXCLUDED_TOP:
            continue
        if k == "nodes" and isinstance(v, list):
            out[k] = [_node_graph(n) for n in v]
        else:
            out[k] = copy.deepcopy(v)
    return out


def clock_projection(doc) -> dict:
    """{"run_seq", "nodes": {cell_id: [executed_at, last_run_seq, last_ok_seq]}}."""
    nodes = {}
    for n in (doc or {}).get("nodes") or []:
        if isinstance(n, dict):
            nodes[n.get("cell_id")] = [n.get(k) for k in CLOCK_NODE_KEYS]
    return {"run_seq": (doc or {}).get("run_seq"), "nodes": nodes}


def runtime_projection(doc) -> dict:
    d = doc or {}
    return {"runtime": d.get("runtime"), "pending": d.get("pending"),
            "clocks": clock_projection(d)}


def graph_hash(doc) -> str:
    return _sha(graph_projection(doc))


def runtime_hash(doc) -> str:
    return _sha(runtime_projection(doc))


def _int0(v) -> int:
    if isinstance(v, bool):
        return 0
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def rev(doc) -> tuple:
    """(version, runtime_version) — both never decrease, every write raises at
    least one, so lexicographic order == write order. Absent reads as 0."""
    d = doc or {}
    return (_int0(d.get("version")), _int0(d.get("runtime_version")))


def stored_hashes(doc) -> tuple:
    """(graph_hash, runtime_hash) of a doc: the stamped values, recomputed when absent."""
    d = doc or {}
    g = d.get("graph_hash")
    r = d.get("runtime_hash")
    return (g if isinstance(g, str) else graph_hash(d),
            r if isinstance(r, str) else runtime_hash(d))


# ─── Delta ─────────────────────────────────────────────────────────────────────

class DeltaMismatch(Exception):
    """apply_delta could not produce the target: reason "base" (the doc is not at
    the delta's base rev), "hash" (graph_hash after apply differs) or "op" (an op
    refers to something the doc does not have / is malformed)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _edge_id(e) -> tuple:
    return (e.get("from"), e.get("to"), e.get("var"), e.get("kind"))


def _by_id(nodes) -> dict:
    return {n.get("cell_id"): n for n in nodes or [] if isinstance(n, dict)}


def _runtime_diff(prev_rt, new_rt):
    """One-level diff of the runtime section, or None when equal.
    {"set", "unset", "vars_upsert", "vars_remove", "runs_upsert", "runs_remove"};
    {"replace": value} when either side is not a dict."""
    if prev_rt == new_rt:
        return None
    if not isinstance(prev_rt, dict) or not isinstance(new_rt, dict):
        return {"replace": copy.deepcopy(new_rt)}
    out = {"set": {}, "unset": []}
    for k in sorted(set(prev_rt) | set(new_rt)):
        if k in ("vars", "runs") and isinstance(prev_rt.get(k), dict) and isinstance(new_rt.get(k), dict):
            p, n = prev_rt[k], new_rt[k]
            ups = {name: copy.deepcopy(v) for name, v in n.items() if name not in p or p[name] != v}
            rem = sorted(name for name in p if name not in n)
            if ups:
                out[f"{k}_upsert"] = ups
            if rem:
                out[f"{k}_remove"] = rem
            continue
        if k not in new_rt:
            out["unset"].append(k)
        elif k not in prev_rt or prev_rt[k] != new_rt[k]:
            out["set"][k] = copy.deepcopy(new_rt[k])
    return out


def diff_docs(prev: dict, new: dict) -> dict:
    """The delta that turns `prev` into `new` (pure; see the module docstring).

    Stamp fields are always present. Graph ops appear only when the graph
    changed (`unchanged` is False); runtime-channel keys only when that part
    changed. When nothing changed the result is the small unchanged marker."""
    prev = prev or {}
    new = new or {}
    g_prev, _ = stored_hashes(prev)
    g_new, r_new = stored_hashes(new)
    b_v, b_rv = rev(prev)
    v, rv = rev(new)
    delta = {
        "delta": DAG_DELTA_FORMAT,
        "schema": new.get("schema", DAG_SCHEMA),
        "base_version": b_v, "base_runtime_version": b_rv,
        "version": v, "runtime_version": rv,
        "graph_hash": g_new, "runtime_hash": r_new,
        "unchanged": g_prev == g_new,
    }
    for k in ("notebook_id", "built_at", "trigger"):
        if k in new:
            delta[k] = new[k]

    p_nodes, n_nodes = _by_id(prev.get("nodes")), _by_id(new.get("nodes"))
    upserted = set()
    if not delta["unchanged"]:
        rm = [cid for cid in p_nodes if cid not in n_nodes]
        ups = []
        for cid, n in n_nodes.items():
            p = p_nodes.get(cid)
            if p is None or _node_graph(p) != _node_graph(n):
                ups.append(copy.deepcopy(n))
                upserted.add(cid)
        if ups:
            delta["nodes_upsert"] = ups
        if rm:
            delta["nodes_remove"] = rm
        p_edges = {_edge_id(e): e for e in prev.get("edges") or [] if isinstance(e, dict)}
        n_edges = {_edge_id(e): e for e in new.get("edges") or [] if isinstance(e, dict)}
        e_rm = [list(k) for k, e in p_edges.items() if n_edges.get(k) != e]
        e_add = [copy.deepcopy(e) for k, e in n_edges.items() if p_edges.get(k) != e]
        if e_add:
            delta["edges_add"] = e_add
        if e_rm:
            delta["edges_remove"] = e_rm
        if prev.get("issues") != new.get("issues"):
            delta["issues"] = copy.deepcopy(new.get("issues"))
        gset, gunset = {}, []
        for k in set(prev) | set(new):
            if k in _EXCLUDED_TOP or k in _GRAPH_LIST_KEYS:
                continue
            if k not in new:
                gunset.append(k)
            elif k not in prev or prev[k] != new[k]:
                gset[k] = copy.deepcopy(new[k])
        if gset:
            delta["graph_set"] = gset
        if gunset:
            delta["graph_unset"] = sorted(gunset)

    clocks = {}
    if prev.get("run_seq") != new.get("run_seq"):
        clocks["run_seq"] = new.get("run_seq")
    c_nodes = {}
    for cid, n in n_nodes.items():
        if cid in upserted:
            continue
        p = p_nodes.get(cid) or {}
        if any(p.get(k) != n.get(k) for k in CLOCK_NODE_KEYS):
            c_nodes[cid] = {k: n.get(k) for k in CLOCK_NODE_KEYS}
    if c_nodes:
        clocks["nodes"] = c_nodes
    if clocks:
        delta["clocks"] = clocks

    if "runtime" in prev and "runtime" not in new:
        delta["runtime"] = {"drop": True}
    elif "runtime" in new:
        rt = _runtime_diff(prev.get("runtime"), new.get("runtime"))
        if rt is not None:
            delta["runtime"] = rt
    if prev.get("pending") != new.get("pending") or ("pending" in prev) != ("pending" in new):
        # Full replacement; None means "no pending section" (documents always carry one).
        delta["pending"] = copy.deepcopy(new.get("pending"))
    return delta


def _fail_if(cond, reason="op"):
    if cond:
        raise DeltaMismatch(reason)


def _apply_runtime(doc: dict, op) -> None:
    _fail_if(not isinstance(op, dict))
    if op.get("drop"):
        doc.pop("runtime", None)
        return
    if "replace" in op:
        doc["runtime"] = copy.deepcopy(op["replace"])
        return
    rt = doc.get("runtime")
    _fail_if(not isinstance(rt, dict))
    rt = dict(rt)
    for k in op.get("unset") or []:
        rt.pop(k, None)
    for k, val in (op.get("set") or {}).items():
        rt[k] = copy.deepcopy(val)
    for section in ("vars", "runs"):
        ups = op.get(f"{section}_upsert") or {}
        rem = op.get(f"{section}_remove") or []
        if not ups and not rem:
            continue
        cur = rt.get(section)
        _fail_if(not isinstance(cur, dict))
        cur = dict(cur)
        for name in rem:
            cur.pop(name, None)
        for name, val in ups.items():
            cur[name] = copy.deepcopy(val)
        rt[section] = cur
    doc["runtime"] = rt


def apply_delta(doc: dict, delta: dict, *, verify: bool = True) -> dict:
    """Apply `delta` to a deep copy of `doc` (never mutates either).

    Raises DeltaMismatch("base") unless rev(doc) == the delta's base rev,
    DeltaMismatch("hash") when verify and graph_hash(result) != delta.graph_hash,
    DeltaMismatch("op") for a malformed op. Order: nodes, edges, issues and other
    graph keys, then clocks, runtime, pending, then the stamps."""
    if not isinstance(doc, dict) or not isinstance(delta, dict):
        raise DeltaMismatch("base")
    base = (_int0(delta.get("base_version")), _int0(delta.get("base_runtime_version")))
    if rev(doc) != base:
        raise DeltaMismatch("base")
    out = copy.deepcopy(doc)
    try:
        nodes = _by_id(out.get("nodes"))
        for cid in delta.get("nodes_remove") or []:
            nodes.pop(cid, None)
        for n in delta.get("nodes_upsert") or []:
            _fail_if(not isinstance(n, dict) or "cell_id" not in n)
            nodes[n["cell_id"]] = copy.deepcopy(n)
        if "nodes_upsert" in delta or "nodes_remove" in delta:
            out["nodes"] = sorted(nodes.values(), key=lambda n: n.get("position"))
        pos = {cid: n.get("position") for cid, n in nodes.items()}

        # Re-sort edges whenever nodes changed too: a reorder moves positions,
        # and edges are ordered by them (finalize's key; (to, var) is unique).
        if any(k in delta for k in ("edges_add", "edges_remove", "nodes_upsert", "nodes_remove")):
            edges = {_edge_id(e): e for e in out.get("edges") or [] if isinstance(e, dict)}
            for k in delta.get("edges_remove") or []:
                _fail_if(not isinstance(k, (list, tuple)) or len(k) != 4)
                edges.pop(tuple(k), None)
            for e in delta.get("edges_add") or []:
                _fail_if(not isinstance(e, dict))
                edges[_edge_id(e)] = copy.deepcopy(e)
            for e in edges.values():
                _fail_if(e.get("to") not in pos or e.get("from") not in pos)
            out["edges"] = sorted(edges.values(),
                                  key=lambda e: (pos[e["to"]], e.get("var"), pos[e["from"]]))

        if "issues" in delta:
            out["issues"] = copy.deepcopy(delta["issues"])
        for k in delta.get("graph_unset") or []:
            out.pop(k, None)
        for k, val in (delta.get("graph_set") or {}).items():
            out[k] = copy.deepcopy(val)

        clocks = delta.get("clocks")
        if clocks is not None:
            _fail_if(not isinstance(clocks, dict))
            if "run_seq" in clocks:
                out["run_seq"] = clocks["run_seq"]
            if clocks.get("nodes"):
                by = _by_id(out.get("nodes"))
                for cid, ck in clocks["nodes"].items():
                    _fail_if(cid not in by or not isinstance(ck, dict))
                    for k in CLOCK_NODE_KEYS:
                        if k in ck:
                            by[cid][k] = ck[k]
        if "runtime" in delta:
            _apply_runtime(out, delta["runtime"])
        if "pending" in delta:
            if delta["pending"] is None:
                out.pop("pending", None)
            else:
                out["pending"] = copy.deepcopy(delta["pending"])
    except DeltaMismatch:
        raise
    except (TypeError, KeyError, AttributeError, ValueError):
        raise DeltaMismatch("op")

    out["version"] = delta.get("version")
    out["runtime_version"] = delta.get("runtime_version")
    out["graph_hash"] = delta.get("graph_hash")
    out["runtime_hash"] = delta.get("runtime_hash")
    for k in ("built_at", "trigger", "notebook_id"):
        if k in delta:
            out[k] = delta[k]
    if verify and graph_hash(out) != delta.get("graph_hash"):
        raise DeltaMismatch("hash")
    return out


def delta_has_vm_changes(delta: dict) -> bool:
    """True when the delta touches what the VM copy holds (graph ops or clocks);
    runtime/pending are VM-owned / proxy bookkeeping and never pushed."""
    return any(k in delta for k in _GRAPH_OP_KEYS) or "clocks" in delta


def json_bytes(v) -> int:
    """Compact UTF-8 JSON size in bytes (the size guard's unit)."""
    return len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))


def send_full(delta_bytes: int, full_bytes) -> bool:
    """Size guard: send the full document instead of the delta when there is no
    known full size or the delta exceeds DELTA_FULL_RATIO of it."""
    return full_bytes is None or delta_bytes > DELTA_FULL_RATIO * full_bytes
