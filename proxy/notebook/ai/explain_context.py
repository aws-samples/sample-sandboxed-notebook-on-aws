"""Context builder for "Explain this code" (pure, no I/O).

Given the saved cells and the notebook's DAG document, select the cells the
target cell depends on (transitively), budget their code in WHOLE cells nearest
first, order them topologically, and label each with its badge (PY-2, SQL-1)
and the variables it provides downstream. Only existing DAG data is used
(``nodes``, ``edges``, ``issues``); nothing is analysed or executed here.
"""
from collections import deque

from proxy.notebook.ai.constants import EXPLAIN_UPSTREAM_CHAR_BUDGET

_PREFIX = {"code": "PY", "sql": "SQL", "markdown": "MD"}


def split_lines(code):
    """Canonical line split shared with the web (CRLF normalised, trailing
    newlines ignored). Line numbers in explanations are 1-based on this list."""
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return text.split("\n") if text else []


def badge_labels(cells):
    """{cell_id(str): "PY-1"}: per-type counters in cell order (same rule as
    web/src/utils/cellBadge.js)."""
    counters = {}
    out = {}
    for c in cells or []:
        t = c.get("type") or "code"
        counters[t] = counters.get(t, 0) + 1
        out[str(c.get("id"))] = f"{_PREFIX.get(t, 'PY')}-{counters[t]}"
    return out


def _dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_context(cells, doc, cell_id, own_code=None, budget=EXPLAIN_UPSTREAM_CHAR_BUDGET):
    """Return the explain context.

    ``{target:{cell_id,label,type,code,line_count},
       upstream:[{cell_id,label,type,code,provides}] (topological),
       omitted_labels:[...], missing_inputs:[vars], cycle:bool,
       upstream_available:bool}``

    ``own_code`` (the unsaved editor text) overrides the saved code of the
    target only. Markdown cells and the target itself are never upstream.
    """
    cells = cells or []
    cell_id = str(cell_id)
    labels = badge_labels(cells)
    by_id = {str(c.get("id")): c for c in cells}
    pos = {str(c.get("id")): i for i, c in enumerate(cells)}

    saved = by_id.get(cell_id)
    ttype = (saved.get("type") if saved else None) or "code"
    tcode = own_code if own_code is not None else ((saved or {}).get("code") or "")
    target = {
        "cell_id": cell_id,
        "label": labels.get(cell_id, "this cell"),
        "type": ttype,
        "code": tcode,
        "line_count": len(split_lines(tcode)),
    }
    result = {"target": target, "upstream": [], "omitted_labels": [], "missing_inputs": [],
              "cycle": False, "upstream_available": False}
    if not isinstance(doc, dict) or not isinstance(doc.get("edges"), list):
        return result
    result["upstream_available"] = True

    edges = [e for e in doc["edges"] if isinstance(e, dict)]
    incoming = {}
    for e in edges:
        incoming.setdefault(str(e.get("to")), []).append(e)

    # BFS over incoming edges (backward edges included for reachability; the
    # visited set guarantees termination on cycles).
    dist = {cell_id: 0}
    queue = deque([cell_id])
    while queue:
        cur = queue.popleft()
        for e in incoming.get(cur, []):
            src = str(e.get("from"))
            if src in dist or src not in by_id:
                continue
            if (by_id[src].get("type") or "code") == "markdown":
                continue
            dist[src] = dist[cur] + 1
            queue.append(src)
    candidates = [c for c in dist if c != cell_id]
    tpos = pos.get(cell_id, 0)
    candidates.sort(key=lambda c: (dist[c], abs(tpos - pos[c]), pos[c]))

    # Budget: whole cells, nearest first; stop at the first cell that does not
    # fit so everything farther away is dropped too.
    kept = []
    used = 0
    omitted = []
    for i, c in enumerate(candidates):
        n = len(by_id[c].get("code") or "")
        if used + n > budget:
            omitted = candidates[i:]
            break
        kept.append(c)
        used += n
    kept_set = set(kept)
    scope = kept_set | {cell_id}

    # Topological order over the kept set: DFS post-order on forward edges,
    # predecessors visited in notebook order. Visited-on-entry never raises on
    # cycles; backward edges are ignored here.
    order = []
    seen = set()

    def visit(n):
        seen.add(n)
        preds = sorted((str(e.get("from")) for e in incoming.get(n, [])
                        if not e.get("backward") and str(e.get("from")) in kept_set),
                       key=lambda x: pos[x])
        for p in preds:
            if p not in seen:
                visit(p)
        order.append(n)

    for n in sorted(kept_set, key=lambda x: pos[x]):
        if n not in seen:
            visit(n)

    provides = {}
    for e in edges:
        src, dst = str(e.get("from")), str(e.get("to"))
        if src in kept_set and dst in scope and e.get("var"):
            provides.setdefault(src, []).append(str(e["var"]))
    result["upstream"] = [
        {"cell_id": n, "label": labels[n], "type": by_id[n].get("type") or "code",
         "code": by_id[n].get("code") or "", "provides": _dedupe(provides.get(n, []))}
        for n in order
    ]
    result["omitted_labels"] = [labels[c] for c in sorted(omitted, key=lambda x: pos[x])]

    missing = []
    cycle = any(e.get("backward") and str(e.get("from")) in scope and str(e.get("to")) in scope
                for e in edges)
    for iss in doc.get("issues") or []:
        if not isinstance(iss, dict) or str(iss.get("cell_id")) not in scope:
            continue
        if iss.get("code") == "missing_producer" and iss.get("var"):
            missing.append(str(iss["var"]))
        elif iss.get("code") == "cycle":
            cycle = True
    result["missing_inputs"] = _dedupe(missing)
    result["cycle"] = bool(cycle)
    return result
