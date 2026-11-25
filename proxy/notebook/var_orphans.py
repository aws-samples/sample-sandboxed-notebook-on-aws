"""Deleted-cell flag (F1), proxy side: DAG-aware override of the VM's
"deleted cell" orphan reason, computed from the notebook's CURRENT cells.

The VM classifies a variable as a "deleted cell" orphan when its producing cell
id is not in the DAG document it holds. Two things are added here:

1. Before a variables read/delete is forwarded, the VM copy of the DAG is
   brought to the current DB document (`dag_service.sync_vm_copy`, a no-op when
   current), so a deleted cell's variables are flagged right away, with no cell
   run needed.
2. After the VM answers, entries whose reason is "deleted cell" are adjusted
   using the DB document's LIVE nodes:
   - a live cell defines the name  -> not an orphan (it still has an owner);
   - live cells read it            -> still an orphan, plus `used_by: [ids]`,
                                      and excluded from the clean-up;
   - otherwise                     -> unchanged (a plain "deleted cell" orphan).

`variables/delete` is guarded server-side: names that a live cell defines or
reads, whose recorded producer is a cell no longer in the notebook, are stripped
from the body before the VM sees them and reported in `skipped` and `in_use`.

No-cell writer relabel (item 13): the VM labels a no-cell write "agent" on
older images (defined_by "" → "agent"), even when a UI control or an API call
wrote it. The proxy sees every write's caller (agent tools send origin "agent"),
so it records the last no-cell writer per session (`note_writes`) and turns a VM
"agent" reason into "no cell" unless the proxy saw an agent write that name.
Newer images report "no cell" themselves; then this is a no-op.

Never raises: any failure forwards the original request/response unchanged.
Never logs bodies or headers.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict

from fastapi import Response

from proxy.notebook import dag_service
from proxy.storage import astorage

logger = logging.getLogger(__name__)

HOOK_PATHS = {
    "variables", "variables/redacted", "variables/schema", "variables/inspect",
    "variables/untracked", "variables/delete",
}

DELETED = "deleted cell"
AGENT = "agent"
NO_CELL = "no cell"

WRITE_PATHS = {"execute", "execute-sql", "set-variable"}
# session id -> {name: "agent" | "user"}: the last no-cell writer the proxy saw.
# In-process only (lost on a proxy restart: such entries then read "no cell").
_writers: "OrderedDict[str, dict]" = OrderedDict()
MAX_SESSIONS = 2000


def _has_cell_id(v) -> bool:
    return (isinstance(v, str) and v != "") or (isinstance(v, int) and not isinstance(v, bool))


def record_writes(sid, names, writer) -> None:
    if not sid or not names:
        return
    m = _writers.pop(sid, None) or {}
    for n in names:
        if isinstance(n, str) and n:
            m[n] = writer
    _writers[sid] = m
    while len(_writers) > MAX_SESSIONS:
        _writers.popitem(last=False)


def writer_of(sid, name):
    return (_writers.get(sid) or {}).get(name)


async def note_writes(p: str, request, resp) -> None:
    """Record who made a no-cell write (POST execute / execute-sql / set-variable
    with no cell id): "agent" when the caller sent origin "agent" (the AI tools),
    else "user" (UI control, API call). Never raises."""
    try:
        if p not in WRITE_PATHS or request.method != "POST" or resp.status_code != 200:
            return
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict) or _has_cell_id(body.get("cell_id")):
            return
        writer = AGENT if body.get("origin") == AGENT else "user"
        if p == "set-variable":
            names = [body.get("name")]
        else:
            data = json.loads(getattr(resp, "body", b"") or b"{}")
            if not isinstance(data, dict):
                return
            names = _strs(data.get("variables_created")) + _strs(data.get("variables_rebound"))
            if p == "execute-sql":
                names += [data.get("output_variable") or body.get("output_variable")]
        record_writes(request.headers.get("X-Session-Id"), [n.strip() if isinstance(n, str) else n
                                                             for n in names], writer)
    except Exception:
        logger.debug("var_orphans: note_writes skipped", exc_info=False)


def relabel(sid, name, reason):
    """A VM "agent" reason is kept only when the proxy saw an agent write `name`."""
    if reason == AGENT and writer_of(sid, name) != AGENT:
        return NO_CELL
    return reason


def _strs(v) -> list:
    return [x for x in v if isinstance(x, str)] if isinstance(v, (list, tuple)) else []


def live_use(doc) -> tuple:
    """(live_ids, definers, readers) from the document's live nodes, in notebook
    order. Definers are `writes - mutates` (same rule as dag.finalize); readers
    are `reads`. A missing or malformed doc gives empty values."""
    live_ids: set = set()
    definers: dict = {}
    readers: dict = {}
    nodes = doc.get("nodes") if isinstance(doc, dict) else None
    if not isinstance(nodes, list):
        return live_ids, definers, readers

    def _pos(n):
        p = n.get("position")
        return p if isinstance(p, (int, float)) and not isinstance(p, bool) else float("inf")

    for n in sorted((n for n in nodes if isinstance(n, dict)), key=_pos):
        cid = n.get("cell_id")
        if not isinstance(cid, str) or not cid:
            continue
        live_ids.add(cid)
        mutates = set(_strs(n.get("mutates")))
        for v in _strs(n.get("writes")):
            if v not in mutates and cid not in definers.setdefault(v, []):
                definers[v].append(cid)
        for v in _strs(n.get("reads")):
            if cid not in readers.setdefault(v, []):
                readers[v].append(cid)
    return live_ids, definers, readers


def adjust_entry(name, entry, use, sid=None):
    """Override one VM variable entry: a no-cell "agent" reason is relabelled
    (see relabel); a "deleted cell" reason is decided with the live DAG (`use`,
    None when the session has no notebook)."""
    if not isinstance(entry, dict):
        return entry
    if entry.get("orphan_reason") == AGENT:
        entry["orphan_reason"] = relabel(sid, name, AGENT)
    if entry.get("orphan_reason") != DELETED or use is None:
        return entry
    _, definers, readers = use
    if definers.get(name):
        entry["orphan"] = False
        entry["orphan_reason"] = None
    elif readers.get(name):
        entry["used_by"] = list(readers[name])
    return entry


def adjust_payload(p: str, data, use, sid=None):
    """Apply adjust_entry across one VM payload, by path shape."""
    if not isinstance(data, dict):
        return data
    if p in ("variables", "variables/redacted"):
        vs = data.get("variables")
        if isinstance(vs, dict):
            for name, entry in vs.items():
                adjust_entry(name, entry, use, sid)
    elif p == "variables/schema":
        vs = data.get("variables")
        if isinstance(vs, list):
            for entry in vs:
                if isinstance(entry, dict):
                    adjust_entry(entry.get("name"), entry, use, sid)
    elif p == "variables/inspect":
        adjust_entry(data.get("name"), data, use, sid)
    elif p == "variables/untracked":
        _, definers, readers = use if use is not None else (set(), {}, {})
        visible, in_use = [], []
        for item in data.get("visible") or []:
            if isinstance(item, dict) and item.get("reason") == AGENT:
                item["reason"] = relabel(sid, item.get("name"), AGENT)
            if isinstance(item, dict) and item.get("reason") == DELETED:
                name = item.get("name")
                if definers.get(name):
                    continue
                if readers.get(name):
                    in_use.append({"name": name, "used_by": list(readers[name])})
                    continue
            visible.append(item)
        data["visible"] = visible
        data["in_use"] = in_use
    return data


def guard_delete_names(names, doc, use) -> tuple:
    """(keep, in_use): names a live cell defines or reads, whose stored runtime
    producer is a non-empty cell id no longer in the notebook, are not deleted.
    Agent, internal and unknown orphans (defined_by "" or missing) are untouched."""
    live_ids, definers, readers = use
    rt = doc.get("runtime") if isinstance(doc, dict) else None
    rvars = rt.get("vars") if isinstance(rt, dict) else None
    rvars = rvars if isinstance(rvars, dict) else {}
    keep, in_use = [], []
    for name in names:
        prov = rvars.get(name) if isinstance(name, str) else None
        by = prov.get("defined_by") if isinstance(prov, dict) else None
        if (isinstance(by, str) and by and by not in live_ids
                and (definers.get(name) or readers.get(name))):
            in_use.append(name)
        else:
            keep.append(name)
    return keep, in_use


async def before_forward(p: str, request):
    """Sync the VM's DAG copy and read the DB document. Returns a context for
    after_forward, or None (scratch session / any failure: no override)."""
    try:
        try:
            dag_service.use_vm_manager(getattr(request.app.state, "vm_manager", None))
        except Exception:
            pass
        sid = request.headers.get("X-Session-Id")
        nid = await dag_service.notebook_for_session(sid)
        if nid is None:
            # Scratch session: no DAG override, only the no-cell writer relabel.
            return {"sid": sid, "use": None, "stripped": []}
        await dag_service.sync_vm_copy(sid, nid)
        doc = await astorage.notebook_dag_get(nid)
        if not isinstance(doc, dict):
            return {"sid": sid, "use": None, "stripped": []}
        use = live_use(doc)
        stripped: list = []
        if p == "variables/delete" and request.method == "POST":
            try:
                body = json.loads(await request.body() or b"{}")
            except (ValueError, TypeError):
                body = None
            if isinstance(body, dict) and isinstance(body.get("names"), list):
                keep, stripped = guard_delete_names(body["names"], doc, use)
                if stripped:
                    # Starlette caches the body on `request._body`; the forward
                    # (_proxy_request_impl) reads it from there, so the VM only
                    # ever sees the kept names.
                    request._body = json.dumps({**body, "names": keep}).encode()
        return {"sid": sid, "use": use, "stripped": stripped}
    except Exception:
        logger.debug("var_orphans: before_forward skipped", exc_info=False)
        return None


def after_forward(p: str, resp, ctx):
    """Overlay the DAG-aware flags on a 200 JSON reply; anything else unchanged."""
    try:
        if ctx is None or getattr(resp, "status_code", None) != 200:
            return resp
        raw = getattr(resp, "body", None)
        if not raw:
            return resp
        data = json.loads(raw)
        if not isinstance(data, dict):
            return resp
        data = adjust_payload(p, data, ctx.get("use"), ctx.get("sid"))
        if p == "variables/delete":
            stripped = list(ctx.get("stripped") or [])
            skipped = data.get("skipped") if isinstance(data.get("skipped"), list) else []
            data["skipped"] = skipped + [n for n in stripped if n not in skipped]
            data["in_use"] = stripped
        return Response(content=json.dumps(data), status_code=200,
                        media_type="application/json")
    except Exception:
        logger.debug("var_orphans: after_forward skipped", exc_info=False)
        return resp
