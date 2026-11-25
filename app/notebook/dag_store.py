"""
Notebook DAG document held by the VM (design §5.4-§5.5).

Part of: app.notebook (application layer)

The proxy is the only writer of the DAG (it builds it, stores it in the DB and
pushes a copy here); the VM keeps the last pushed document so every checkpoint
can save it as dag.json next to the pickled values, and reports it back (plus its
own runtime section) when the proxy attaches after a restore.

Endpoints (proxy-internal; same reachability as /checkpoint-save and
/restore-state: only via the Lambda MicroVM endpoint, which requires the per-VM
X-aws-proxy-auth token — plus the `X-Dag-Writer: proxy` marker below):
  PUT /dag   - store the proxy's full document (runtime/pending/checkpoint discarded)
  PATCH /dag - apply a delta (app.notebook.dag_contract.diff_docs format) to the
               stored document; 409 when there is no document, the base revision
               differs or the graph hash does not verify (the proxy then PUTs the
               full document). The stored copy is always a full document, so
               every checkpoint's dag.json is complete.
  GET /dag   - {origin, doc, runtime, restored_from, runtime_source, consistent,
               checkpoint} — `checkpoint` is the restored dag.json's save stamp
               (None unless restored from dag.json); it is never part of `doc`.
"""

import asyncio
import copy
import json
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .dag_contract import (
    DAG_DELTA_FORMAT,
    DAG_MAX_BYTES,
    DAG_SCHEMA,
    DeltaMismatch,
    apply_delta as _apply_delta,
    rev,
)
from .executor import RuntimeSectionBusy

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dag"])

# Keys the VM never keeps from a pushed document: runtime is VM-owned (reported
# live from the executor), pending is proxy bookkeeping, checkpoint is written
# only into the S3 copy at save time.
_DISCARDED_KEYS = ("runtime", "pending", "checkpoint")

# GET /dag waits at most this long for the executor's _exec_lock.
_GET_LOCK_TIMEOUT_S = 5.0


class DagStore:
    """Thread-safe holder of the proxy-pushed DAG document (one per VM process).

    origin: "empty" (nothing pushed or restored), "pushed" (PUT /dag), or
    "restored" (checkpoint restore ran; doc may be None when the checkpoint had
    no dag.json). restored_from / runtime_source / consistent describe the last
    restore: source session id, "dag.json" | "provenance.json" | None, and whether
    dag.json's checkpoint id matched the manifest (None when not from dag.json)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.doc: dict | None = None
        self.origin: str = "empty"
        self.restored_from: str | None = None
        self.runtime_source: str | None = None
        self.consistent: bool | None = None
        # dag.json's `checkpoint` stamp of the last restore (None otherwise).
        self.restored_checkpoint: dict | None = None

    def put(self, doc: dict) -> None:
        with self._lock:
            self.doc = copy.deepcopy(doc)
            self.origin = "pushed"

    def apply_delta(self, delta: dict) -> tuple:
        """Apply a (validated) delta under the lock → (status, body). The stored
        document only changes on success (200); 409 otherwise."""
        with self._lock:
            if self.doc is None:
                return 409, {"error": "no_document"}
            base = (delta["base_version"], delta["base_runtime_version"])
            have = rev(self.doc)
            if have != base:
                return 409, {"error": "base_mismatch", "version": have[0], "runtime_version": have[1]}
            try:
                out = _apply_delta(self.doc, delta, verify=True)
            except DeltaMismatch as e:
                return 409, {"error": "hash_mismatch" if e.reason == "hash" else f"{e.reason}_mismatch"}
            for key in _DISCARDED_KEYS:
                out.pop(key, None)
            self.doc = out
            self.origin = "pushed"
            return 200, {"ok": True, "version": out.get("version"),
                         "runtime_version": out.get("runtime_version")}

    def set_restored(self, doc, session_id, runtime_source, consistent) -> None:
        """Hold a document restored from dag.json. The VM copy never keeps the
        discarded keys (runtime / pending / the `checkpoint` save stamp) — the same
        shape PUT and PATCH store — so the first PATCH after a restore verifies.
        The checkpoint stamp is kept separately for the restore report."""
        with self._lock:
            if isinstance(doc, dict):
                ckpt = doc.get("checkpoint")
                self.doc = {k: copy.deepcopy(v) for k, v in doc.items() if k not in _DISCARDED_KEYS}
                self.restored_checkpoint = copy.deepcopy(ckpt) if isinstance(ckpt, dict) else None
            else:
                self.doc = None
                self.restored_checkpoint = None
            self.origin = "restored"
            self.restored_from = session_id
            self.runtime_source = runtime_source
            self.consistent = consistent

    def cell_ids(self) -> set | None:
        """Set of str(cell_id) of the document's nodes — the notebook's cells as the
        proxy last pushed them. None when there is no document (or no node list),
        so orphan classification never reports "deleted cell" without one."""
        with self._lock:
            doc = self.doc
            nodes = doc.get("nodes") if isinstance(doc, dict) else None
            if not isinstance(nodes, list):
                return None
            return {str(n["cell_id"]) for n in nodes
                    if isinstance(n, dict) and n.get("cell_id") is not None}

    def snapshot(self) -> dict | None:
        """Deep copy of the current document (None when there is none)."""
        with self._lock:
            return copy.deepcopy(self.doc) if self.doc is not None else None

    def report(self) -> dict:
        """Deep copy of the whole state (GET /dag body minus runtime)."""
        with self._lock:
            return {
                "origin": self.origin,
                "doc": copy.deepcopy(self.doc) if self.doc is not None else None,
                "restored_from": self.restored_from,
                "runtime_source": self.runtime_source,
                "consistent": self.consistent,
                "checkpoint": copy.deepcopy(self.restored_checkpoint),
            }


def _require_proxy_writer(request: Request):
    """Both /dag verbs are proxy-internal: the proxy's own client sends
    `X-Dag-Writer: proxy` (its browser forward refuses /proxy/dag). Returns a 403
    response when the marker is missing, else None."""
    if request.headers.get("x-dag-writer") != "proxy":
        logger.warning(f"/dag {request.method} rejected: missing X-Dag-Writer marker")
        return JSONResponse(status_code=403, content={"error": "proxy-internal endpoint"})
    return None


def _bad_request(field: str, method: str = "PUT") -> JSONResponse:
    logger.warning(f"{method} /dag rejected: {field} invalid")
    return JSONResponse(status_code=400, content={"error": f"{field} invalid"})


def _too_large(method: str = "PUT") -> JSONResponse:
    logger.warning(f"{method} /dag rejected: body exceeds {DAG_MAX_BYTES} bytes")
    return JSONResponse(status_code=413, content={"error": "body too large"})


def _nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


async def _read_json_body(request: Request, method: str):
    """Capped body read + JSON dict parse, shared by PUT and PATCH →
    (dict, None) or (None, error response)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > DAG_MAX_BYTES:
                return None, _too_large(method)
        except ValueError:
            return None, _bad_request("content-length", method)

    # Read with a running cap so a missing/wrong Content-Length can't push more
    # than DAG_MAX_BYTES into memory.
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > DAG_MAX_BYTES:
            return None, _too_large(method)
        chunks.append(chunk)
    raw = b"".join(chunks)

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, _bad_request("body", method)
    if not isinstance(body, dict):
        return None, _bad_request("body", method)
    return body, None


@router.put("/dag")
async def put_dag(request: Request):
    """Store the proxy's DAG document. Does not take the execute lock (no
    namespace access)."""
    denied = _require_proxy_writer(request)
    if denied is not None:
        return denied
    doc, err = await _read_json_body(request, "PUT")
    if err is not None:
        return err
    if doc.get("schema") != DAG_SCHEMA or isinstance(doc.get("schema"), bool):
        return _bad_request("schema")
    version = doc.get("version")
    if not _nonneg_int(version):
        return _bad_request("version")
    if "runtime_version" in doc and not _nonneg_int(doc.get("runtime_version")):
        return _bad_request("runtime_version")
    for key in ("nodes", "edges", "issues"):
        if not isinstance(doc.get(key), list):
            return _bad_request(key)

    for key in _DISCARDED_KEYS:
        doc.pop(key, None)
    request.app.state.dag_store.put(doc)
    return {"ok": True, "version": version}


# Delta op keys and the type each must have (validated before apply).
_DELTA_OP_TYPES = {
    "nodes_upsert": list, "nodes_remove": list, "edges_add": list, "edges_remove": list,
    "issues": list, "graph_set": dict, "graph_unset": list, "clocks": dict,
}


def _delta_invalid(delta: dict):
    """The first invalid field of a PATCH body, or None."""
    if delta.get("delta") != DAG_DELTA_FORMAT or isinstance(delta.get("delta"), bool):
        return "delta"
    if delta.get("schema") != DAG_SCHEMA or isinstance(delta.get("schema"), bool):
        return "schema"
    for key in ("base_version", "base_runtime_version", "version", "runtime_version"):
        if not _nonneg_int(delta.get(key)):
            return key
    if not isinstance(delta.get("graph_hash"), str):
        return "graph_hash"
    for key, typ in _DELTA_OP_TYPES.items():
        if key in delta and not isinstance(delta[key], typ):
            return key
    for n in delta.get("nodes_upsert") or []:
        if not isinstance(n, dict) or not isinstance(n.get("cell_id"), str):
            return "nodes_upsert"
    for e in delta.get("edges_add") or []:
        if not isinstance(e, dict):
            return "edges_add"
    for k in delta.get("edges_remove") or []:
        if not isinstance(k, list) or len(k) != 4:
            return "edges_remove"
    return None


@router.patch("/dag")
async def patch_dag(request: Request):
    """Apply a delta from the proxy to the stored document (same gate and size
    cap as PUT). `runtime` / `pending` in the delta are ignored (the VM owns its
    runtime; pending is proxy bookkeeping). 409 → the proxy sends the full doc."""
    denied = _require_proxy_writer(request)
    if denied is not None:
        return denied
    delta, err = await _read_json_body(request, "PATCH")
    if err is not None:
        return err
    bad = _delta_invalid(delta)
    if bad is not None:
        return _bad_request(bad, "PATCH")
    delta.pop("runtime", None)
    delta.pop("pending", None)
    status, body = request.app.state.dag_store.apply_delta(delta)
    if status != 200:
        logger.info(f"PATCH /dag → {status} {body.get('error')}")
        return JSONResponse(status_code=status, content=body)
    return body


@router.get("/dag")
async def get_dag(request: Request):
    """Report the stored document, the live runtime section and the last restore."""
    denied = _require_proxy_writer(request)
    if denied is not None:
        return denied

    executor = request.app.state.executor
    try:
        # Worker thread with a bound: a cell may hold _exec_lock for its whole run.
        runtime = await asyncio.to_thread(executor.runtime_section, _GET_LOCK_TIMEOUT_S)
    except RuntimeSectionBusy:
        logger.info("GET /dag busy: _exec_lock held by a running cell")
        return JSONResponse(status_code=503, content={"busy": True})

    report = request.app.state.dag_store.report()
    return {
        "origin": report["origin"],
        "doc": report["doc"],
        "runtime": runtime,
        "restored_from": report["restored_from"],
        "runtime_source": report["runtime_source"],
        "consistent": report["consistent"],
        "checkpoint": report["checkpoint"],
    }
