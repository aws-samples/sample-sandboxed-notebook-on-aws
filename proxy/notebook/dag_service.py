"""
Proxy orchestration for the persisted per-notebook DAG (design.md §4).

Part of: proxy.notebook

Async glue around the pure builder (proxy/notebook/dag.py) and the storage
methods (notebook_dag_get / notebook_dag_write / notebook_dag_update /
notebook_id_for_session). No inference happens here. The DB write stays
synchronous inside the run request (one locked RMW); there is no proxy-side
DAG cache. Owns:

  * safe(fn)                 — builder wrapper: a DAG failure never fails a save (§4.1)
  * notebook_for_session     — session → notebook lookup with a 60 s HIT-only cache
  * capture_forwarded        — capture on the generic /proxy/{path} forward (§4.3)
  * BatchCapture             — capture on the /proxy/execute-batch SSE relay (§4.4)
  * schedule_push / attach_soon / await_attach / flush
                             — VM copy of the document: push, attach, reconcile (§4.7)
  * get_or_build             — the open-path rebuild used by the notebook routes (§4.2)

Change check + delta transport (delta-plan §0.5, §0.7; the contract lives in
app/notebook/dag_contract.py):
  * storage skips the write when neither graph_hash nor runtime_hash changed;
  * /proxy/execute* responses and the batch `dag` frame carry `dag_delta`
    (diff of the RMW's prev → stored doc; the "unchanged marker" when nothing
    was written) or the full `dag` (first document, or the delta exceeds
    DELTA_FULL_RATIO of the full doc). The notebook routes and
    GET /notebooks/{id}/dag always return the full document (resync path);
  * the VM push is skipped unless the graph or the run clocks changed (the VM
    owns its runtime). The push loop PATCHes the cumulative delta from the doc
    the VM last acknowledged (`vm_base`, a non-authoritative mirror used only
    as a diff base) to the CURRENT DB doc, and falls back to a full PUT on 409 /
    no base / an image without PATCH. Intermediate deltas are never sent.

VM calls (GET/PUT/PATCH https://{endpoint}/dag) carry the per-VM
X-aws-proxy-auth token plus the fixed X-Dag-Writer: proxy marker. The
browser-facing forward refuses /proxy/dag (proxy/platform/routes/microvm.py::
_internal_target), so only this module writes the VM's document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi.responses import JSONResponse

from app.notebook.dag_contract import (
    DAG_MAX_BYTES,
    delta_has_vm_changes,
    diff_docs,
    json_bytes,
    rev,
    send_full,
)
from proxy.notebook.dag import (
    RunCapture,
    apply_cells,
    apply_runs,
    apply_runtime,
    in_sync,
    is_cold_attach,
    reconcile_restored,
    strip_for_vm,
)
from proxy.notebook import cell_images
from proxy.platform import http_clients
from proxy.storage import astorage
from proxy.storage._dag_doc import utc_now_ms

logger = logging.getLogger(__name__)

# ─── Tunables (module-level so tests can shorten the delays) ──────────────────
SESSION_CACHE_TTL = 60.0          # seconds a session → notebook HIT is trusted
ATTACH_BACKOFF = (2.0, 4.0, 8.0)  # retry delay after failed attach attempt 1, 2, 3
ATTACH_MAX_ATTEMPTS = 3           # give up (and resume pushes) after this many failures
EMPTY_RETRIES = 3                 # expect_restore: re-GET while origin == "empty"
EMPTY_RETRY_DELAY = 2.0
VM_TIMEOUT = 10.0                 # GET/PUT /dag (the VM's GET waits ≤ 5 s for its lock)
PUT_WARN_INTERVAL = 60.0          # PUT failures are logged once per VM per interval
MAX_CELL_ID_LEN = 256

DAG_WRITER_HEADER = "X-Dag-Writer"
DAG_WRITER_VALUE = "proxy"


def now_iso() -> str:
    """UTC now, ISO-8601 with milliseconds and a 'Z' suffix (the doc's time format)."""
    return utc_now_ms()


# ─── §4.1 safe build wrapper ───────────────────────────────────────────────────

def safe(fn, *, nb=None, trigger=None):
    """Wrap a builder call so it can never fail the caller's transaction.

    Returns `build(*args)` (works for both storage call shapes, `build(prev)` and
    `build(prev, cells)`): runs `fn(*args)`; on any exception logs it with the
    notebook id + trigger and returns None (storage then writes nothing and the
    cell save still commits). `build.failed` is set when that happened."""
    def build(*args):
        try:
            return fn(*args)
        except Exception:
            build.failed = True
            logger.exception("dag build failed nb=%s trigger=%s", nb, trigger)
            return None
    build.failed = False
    return build


def cells_build(cells, trigger: str, *, nb=None):
    """safe() apply_cells for notebook_create / notebook_update / version
    restore (storage calls it as `build(prev)` in the same transaction as the
    cell write; `cells` are the ones being written)."""
    # Storage skips cells without an id and numbers the kept cells' positions
    # gap-free (_write_cells); build from that same list so node.position ==
    # notebook_cells.position and in_sync holds on reopen.
    kept = [c for c in (cells or []) if isinstance(c, dict) and c.get("id") is not None]
    return safe(lambda prev: apply_cells(prev, kept, trigger=trigger, now=now_iso()),
                nb=nb, trigger=trigger)


# ─── VM manager handle ─────────────────────────────────────────────────────────
# One MicrovmManager per proxy process (app.state.vm_manager). Entry points that
# have it (capture via request.app.state, /launch, recover_and_replay) register it
# so the background push loop can resolve session → VM and the per-VM token.
_vm_manager = None


def use_vm_manager(vm_manager) -> None:
    global _vm_manager
    if vm_manager is not None:
        _vm_manager = vm_manager


# ─── Session → notebook cache (hits only) ─────────────────────────────────────
_session_cache: dict[str, tuple[int, float]] = {}


async def notebook_for_session(sid) -> int | None:
    """Notebook id linked to `sid` (most recently updated wins; storage rule).
    Only HITS are cached (60 s); a miss always asks storage, so a notebook
    linked a moment ago is captured on its very next execute."""
    if not sid:
        return None
    hit = _session_cache.get(sid)
    mono = time.monotonic()
    if hit is not None and hit[1] > mono:
        return hit[0]
    try:
        nid = await astorage.notebook_id_for_session(sid)
    except Exception as e:
        logger.warning("dag: notebook lookup for session %s failed: %s", sid, e)
        return None
    if nid is None:
        _session_cache.pop(sid, None)
        logger.debug("dag: session %s has no notebook (no capture)", sid)
        return None
    _session_cache[sid] = (int(nid), mono + SESSION_CACHE_TTL)
    return int(nid)


def invalidate_session(sid) -> None:
    if sid:
        _session_cache.pop(sid, None)


def invalidate_notebook(nid) -> None:
    if nid is None:
        return
    try:
        nid = int(nid)
    except (TypeError, ValueError):
        return
    for sid in [s for s, (n, _) in list(_session_cache.items()) if n == nid]:
        _session_cache.pop(sid, None)


# ─── Input normalisation (§10) ─────────────────────────────────────────────────

def _cell_id(v) -> str | None:
    """A str of 1–256 chars is used as is; a non-bool int becomes str(v);
    anything else is invalid (→ runtime-only capture)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str) and 1 <= len(v) <= MAX_CELL_ID_LEN:
        return v
    return None


def _strs(v) -> tuple:
    if not isinstance(v, (list, tuple)):
        return ()
    return tuple(x for x in v if isinstance(x, str))


def _valid_runtime(rt) -> dict | None:
    """VM runtime section: dict with `vars` dict, `runs` dict, int `clock`."""
    if not isinstance(rt, dict):
        return None
    clock = rt.get("clock")
    if not isinstance(rt.get("vars"), dict) or not isinstance(rt.get("runs"), dict):
        return None
    if not isinstance(clock, int) or isinstance(clock, bool):
        return None
    return rt


def _run_capture(cid: str, cell_type: str, code, output_variable, result: dict,
                 executed_at: str) -> RunCapture:
    sources = result.get("sources_read")
    return RunCapture(
        cell_id=cid,
        cell_type="sql" if cell_type == "sql" else "code",
        code=code if isinstance(code, str) else "",
        output_variable=output_variable if isinstance(output_variable, str) and output_variable else None,
        ok=result.get("success") is True,
        reads=_strs(result.get("variables_read")),
        created=_strs(result.get("variables_created")),
        rebound=_strs(result.get("variables_rebound")),
        mutated=_strs(result.get("variables_mutated")),
        sources=tuple(sources) if isinstance(sources, list) else None,
        executed_at=executed_at,
    )


def _parse_json_bytes(raw) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


async def _vm_id_for(sid) -> str | None:
    if _vm_manager is None or not sid:
        return None
    try:
        sv = await _vm_manager.get_session_vm_async(sid)
    except Exception:
        return None
    return (sv or {}).get("vm_id")


# ─── Response payload: full doc or delta (delta-plan §0.5) ────────────────────

def _payload(w) -> dict:
    """{"dag": full doc} | {"dag_delta": delta} | {} for one DagWrite. Runs after
    the RMW returned (outside the DB lock). Full doc when there was no prev
    document or the delta exceeds DELTA_FULL_RATIO of it; nothing written →
    the unchanged marker (never replaced by the full doc)."""
    if w is None or w.doc is None:
        return {}
    if w.prev is None:
        return {"dag": w.doc}
    delta = diff_docs(w.prev, w.doc)
    if w.wrote and send_full(json_bytes(delta), w.nbytes):
        return {"dag": w.doc}
    return {"dag_delta": delta}


# ─── §4.3 capture on the generic forward ───────────────────────────────────────

async def capture_forwarded(p: str, request, resp):
    """Post-process a 200 response of POST /proxy/{execute,execute-sql,
    set-variable,reset}: pop the VM's `runtime`, record the run (or just the
    runtime) in the notebook's document under the storage lock, and return the
    response with exactly one of `dag_delta` / `dag` / `dag_error` added.
    Never raises."""
    try:
        data = json.loads(resp.body)
    except Exception:
        return resp
    if not isinstance(data, dict):
        return resp
    try:
        use_vm_manager(getattr(request.app.state, "vm_manager", None))
    except Exception:
        pass
    runtime = _valid_runtime(data.pop("runtime", None))
    sid = request.headers.get("X-Session-Id")
    nid = await notebook_for_session(sid)
    if nid is None:
        return JSONResponse(data, status_code=200)

    body = _parse_json_bytes(await request.body())
    # Chart persistence: add the content-addressed S3 key next to the base64
    # image and upload in the background. Never affects capture or the response.
    cid_img = _cell_id(body.get("cell_id"))
    if (p == "execute" and cid_img and body.get("origin") != "agent"
            and (data.get("image") or cell_images.has_image_items(data.get("outputs")))):
        try:
            prep = cell_images.prepare(nid, cid_img, data["image"]) if data.get("image") else None
            if prep:
                data["image_s3_key"] = prep[0]
                cell_images.schedule_upload(*prep)
            # Every image of the ordered `outputs` list gets its own key too.
            last_key = cell_images.prepare_outputs(nid, cid_img, data.get("outputs"))
            if last_key and not data.get("image_s3_key"):
                data["image_s3_key"] = last_key
        except Exception:
            logger.debug("cell image: capture failed", exc_info=True)
    now = now_iso()
    run = None
    if p in ("execute", "execute-sql"):
        cid = _cell_id(body.get("cell_id"))
        if cid is None:
            logger.debug("dag: %s without a valid cell_id (session %s): runtime-only", p, sid)
        elif body.get("origin") == "agent":
            pass  # agent exploration: runtime-only (§4.5)
        elif p == "execute":
            run = _run_capture(cid, "code", body.get("code"), None, data, now)
        else:
            ov = data.get("output_variable") or body.get("output_variable")
            run = _run_capture(cid, "sql", body.get("sql"), ov, data, now)

    if run is None and runtime is None:
        return JSONResponse(data, status_code=200)

    vm_id = await _vm_id_for(sid)
    if run is not None:
        trigger = "execute"
        build = safe(lambda prev: apply_runs(prev, [run], runtime, vm_id, trigger="execute", now=now),
                     nb=nid, trigger=trigger)
    else:
        # Runtime-only (set-variable / reset / agent / no cell id): the VM's
        # section replaces the stored one unconditionally (a reset lowers it).
        trigger = "runtime"
        build = safe(lambda prev: apply_runs(prev, [], runtime, vm_id, trigger="runtime", now=now),
                     nb=nid, trigger=trigger)
    try:
        w = await astorage.notebook_dag_write(nid, build)
    except Exception as e:
        logger.warning("dag capture write failed nb=%s trigger=%s: %s", nid, trigger, e)
        w, build.failed = None, True
    if build.failed:
        data["dag_error"] = "capture_failed"
    elif w is not None and w.doc is not None:
        data.update(_payload(w))
        if w.wrote and w.vm_changed:
            schedule_push(sid, nid)
    return JSONResponse(data, status_code=200)


# ─── §4.4 capture on the /proxy/execute-batch SSE relay ───────────────────────

_background: set = set()
_UNSET_NID = object()


def _spawn(coro):
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


class BatchCapture:
    """Incremental SSE frame parser for one /execute-batch relay.

    `cell_result` frames are forwarded unchanged and captured; the VM's
    `runtime` frame is kept and not forwarded; on `done` ONE storage RMW
    records every run, then a `{"type":"dag","dag_delta":…}` (or
    `{"type":"dag","dag":…}`) frame is emitted before the `done` frame. If the stream ends before `done`, `close()` applies the
    captured runs in a background task (no frame)."""

    def __init__(self, sid, request_body: bytes, vm_id):
        self.sid = sid
        self.vm_id = vm_id
        self.runs: list[RunCapture] = []
        self.runtime = None
        self.finished = False
        self._img_nid = _UNSET_NID   # notebook id for chart keys, resolved once
        body = _parse_json_bytes(request_body)
        cells = body.get("cells") if isinstance(body.get("cells"), list) else []
        self.cells = {}
        for c in cells:
            if isinstance(c, dict):
                cid = _cell_id(c.get("cell_id"))
                if cid is not None:
                    self.cells[cid] = c

    async def relay(self, chunks):
        """Async generator: bytes in (VM stream) → bytes out (to the browser)."""
        buf = b""
        async for chunk in chunks:
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                async for out in self._handle(frame):
                    yield out
        if buf:
            yield buf

    async def _handle(self, frame: bytes):
        obj = None
        if frame.startswith(b"data: ") and b"\n" not in frame:
            try:
                obj = json.loads(frame[6:])
            except (ValueError, TypeError):
                obj = None
        kind = obj.get("type") if isinstance(obj, dict) else None
        if kind == "runtime":
            self.runtime = _valid_runtime(obj.get("runtime"))
            return
        if kind == "cell_result":
            self._capture(obj)
            res = obj.get("result")
            if isinstance(res, dict) and (res.get("image") or cell_images.has_image_items(res.get("outputs"))):
                if await self._attach_image(obj, res):
                    yield f"data: {json.dumps(obj)}\n\n".encode()
                    return
        elif kind == "done":
            payload = await self.finish()
            if payload:
                yield f"data: {json.dumps({'type': 'dag', **payload})}\n\n".encode()
        yield frame + b"\n\n"

    async def _attach_image(self, obj: dict, result: dict) -> bool:
        """Add `image_s3_key` to a cell_result's result and schedule the upload.
        True when the frame was changed (the caller re-serializes it)."""
        try:
            cid = _cell_id(obj.get("cell_id"))
            if cid is None or cid not in self.cells:
                return False
            if self._img_nid is _UNSET_NID:
                self._img_nid = await notebook_for_session(self.sid)
            if self._img_nid is None:
                return False
            changed = False
            prep = cell_images.prepare(self._img_nid, cid, result["image"]) if result.get("image") else None
            if prep:
                result["image_s3_key"] = prep[0]
                cell_images.schedule_upload(*prep)
                changed = True
            last_key = cell_images.prepare_outputs(self._img_nid, cid, result.get("outputs"))
            if last_key:
                if not result.get("image_s3_key"):
                    result["image_s3_key"] = last_key
                changed = True
            return changed
        except Exception:
            logger.debug("cell image: batch capture failed", exc_info=True)
            return False

    def _capture(self, obj: dict) -> None:
        cid = _cell_id(obj.get("cell_id"))
        cell = self.cells.get(cid) if cid is not None else None
        result = obj.get("result")
        if cell is None or not isinstance(result, dict):
            return
        ctype = (cell.get("type") or "code")
        ctype = ctype.lower() if isinstance(ctype, str) else "code"
        if ctype not in ("code", "sql"):
            return
        ov = (result.get("output_variable") or cell.get("output_variable")) if ctype == "sql" else None
        self.runs.append(_run_capture(cid, ctype, cell.get("code"), ov, result, now_iso()))

    async def finish(self):
        """One RMW for the whole batch; returns the frame payload
        ({"dag_delta": …} | {"dag": …}) or None."""
        self.finished = True
        if not self.runs and self.runtime is None:
            return None
        nid = await notebook_for_session(self.sid)
        if nid is None:
            return None
        runs, runtime, vm_id, now = list(self.runs), self.runtime, self.vm_id, now_iso()
        build = safe(lambda prev: apply_runs(prev, runs, runtime, vm_id, trigger="batch", now=now),
                     nb=nid, trigger="batch")
        try:
            w = await astorage.notebook_dag_write(nid, build)
        except Exception as e:
            logger.warning("dag batch capture write failed nb=%s: %s", nid, e)
            return None
        if w is not None and w.doc is not None and not build.failed:
            if w.wrote and w.vm_changed:
                schedule_push(self.sid, nid)
            return _payload(w) or None
        return None

    def close(self) -> None:
        """Called from the relay's finally: stream broke / client left before done."""
        if self.finished or not (self.runs or self.runtime is not None):
            return
        logger.warning("dag: batch stream for session %s ended before done; "
                       "applying %d captured run(s) in the background", self.sid, len(self.runs))
        try:
            _spawn(self.finish())
        except RuntimeError:
            pass  # no running loop (interpreter shutdown) — nothing to apply on


# ─── §4.7 VM push / attach / reconcile ─────────────────────────────────────────

class _SessionSync:
    """Per-session sync state (bound to the event loop that created it)."""

    def __init__(self, loop):
        self.loop = loop
        self.lock = asyncio.Lock()
        self.synced_vm_id = None
        self.dirty = False            # an execution-driven rebuild wants a PUT
        self.attach_wanted = False    # attach_soon asked for an attach (no PUT if already synced)
        self.expect_restore = False
        self.task = None
        self.attach_future = None
        self.attach_attempts: dict[str, int] = {}
        self.no_dag_support: set[str] = set()
        self.retry_handle = None
        # Non-authoritative mirror of the VM copy: the strip_for_vm doc the VM
        # last acknowledged (200), used ONLY as the PATCH diff base. Never read
        # by anyone else; cleared on attach, forget and any push failure.
        self.vm_base = None
        self.vm_base_vm = None
        self.no_patch: set[str] = set()   # VMs whose image has no PATCH /dag


_sync: dict[str, _SessionSync] = {}
_last_put_warn: dict[str, float] = {}
_no_support_logged: set[str] = set()


def _state(sid) -> _SessionSync:
    loop = asyncio.get_running_loop()
    st = _sync.get(sid)
    if st is None or st.loop is not loop:
        st = _SessionSync(loop)
        _sync[sid] = st
    return st


def _kick(sid, st: _SessionSync) -> None:
    if _sync.get(sid) is not st:
        return
    if st.task is None or st.task.done():
        st.task = st.loop.create_task(_push_loop(sid, st))


def schedule_push(sid, nid=None) -> None:
    """Mark the session dirty and start the coalescing push loop. Only
    execution-driven rebuilds (and attach) call this — never saves, so a save
    cannot wake a suspended VM."""
    if not sid:
        return
    try:
        st = _state(sid)
    except RuntimeError:
        return  # no running loop
    st.dirty = True
    _kick(sid, st)


def attach_soon(sid, nid=None, expect_restore: bool = False, *, vm_manager=None) -> None:
    """Background attach after a VM became ready for `sid` (/launch, recovery).
    Idempotent through synced_vm_id. The future is kept so
    GET /notebooks/{id}/dag can await it (≤ 5 s)."""
    if not sid:
        return
    use_vm_manager(vm_manager)
    try:
        st = _state(sid)
    except RuntimeError:
        return
    st.expect_restore = st.expect_restore or bool(expect_restore)
    st.attach_wanted = True
    if st.attach_future is None or st.attach_future.done():
        st.attach_future = st.loop.create_future()
    _kick(sid, st)


async def await_attach(sid, timeout: float = 5.0) -> None:
    """Wait (bounded) for an in-flight attach of `sid`; never raises."""
    st = _sync.get(sid) if sid else None
    if st is None:
        return
    try:
        if st.loop is not asyncio.get_running_loop():
            return
    except RuntimeError:
        return
    fut = st.attach_future
    if fut is None or fut.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout)
    except (asyncio.TimeoutError, Exception):
        pass


def _resolve_attach(st: _SessionSync) -> None:
    if st.attach_future is not None and not st.attach_future.done():
        st.attach_future.set_result(None)


def _schedule_retry(sid, st: _SessionSync, attempt: int) -> None:
    delay = ATTACH_BACKOFF[min(max(attempt, 1), len(ATTACH_BACKOFF)) - 1]
    if st.retry_handle is not None:
        st.retry_handle.cancel()
    st.retry_handle = st.loop.call_later(delay, _kick, sid, st)


async def _push_loop(sid, st: _SessionSync) -> None:
    retry_scheduled = False
    try:
        while st.dirty or st.attach_wanted:
            push = st.dirty
            st.dirty = False
            async with st.lock:
                outcome = await _sync_once(sid, st, push)
            if outcome == "retry":
                retry_scheduled = True
                break
    except Exception as e:
        logger.warning("dag push loop for session %s failed: %s", sid, e)
    finally:
        if not retry_scheduled:
            _resolve_attach(st)


async def _sync_once(sid, st: _SessionSync, push: bool) -> str:
    vm_manager = _vm_manager
    if vm_manager is None:
        st.attach_wanted = False
        return "stop"
    sv = await vm_manager.get_session_vm_async(sid)
    if not sv or not sv.get("vm_id"):
        st.attach_wanted = False
        return "stop"
    vm_id, endpoint = sv["vm_id"], sv.get("endpoint") or ""
    if vm_id in st.no_dag_support:
        st.attach_wanted = False
        return "stop"
    if st.synced_vm_id != vm_id:
        res = await _attach(sid, st, vm_manager, vm_id, endpoint)
        if res == "retry":
            st.dirty = st.dirty or push
            _schedule_retry(sid, st, st.attach_attempts.get(vm_id, 1))
            return "retry"
        if res == "no_support":
            st.attach_wanted = False
            return "stop"
        push = True  # after an attach the VM gets the current document
    st.attach_wanted = False
    _resolve_attach(st)
    if push:
        await _push_current(sid, st, vm_manager, vm_id, endpoint)
    return "ok"


def _vm_headers(token: str) -> dict:
    return {"X-aws-proxy-auth": token, DAG_WRITER_HEADER: DAG_WRITER_VALUE,
            "Content-Type": "application/json"}


class _AttachError(Exception):
    pass


async def _get_report(vm_manager, vm_id: str, endpoint: str):
    """VM GET /dag → report dict; None for 404 (image without the endpoint).
    Raises _AttachError on any other failure (incl. 503 {"busy": true})."""
    token = await vm_manager.get_auth_token(vm_id)
    resp = await http_clients.vm_client().get(
        f"https://{endpoint}/dag", headers=_vm_headers(token), timeout=VM_TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _AttachError(f"GET /dag returned {resp.status_code}")
    try:
        report = resp.json()
    except Exception:
        raise _AttachError("GET /dag returned non-JSON")
    if not isinstance(report, dict) or _valid_runtime(report.get("runtime")) is None:
        raise _AttachError("GET /dag report invalid")
    return report


def _attach_build(report: dict, vm_id: str, attach_seq: int):
    """RMW build for an attach. NOT wrapped in safe(): a builder exception is an
    attach failure (retried), not a silent no-op. The cold-attach condition is
    evaluated on `prev` inside the locked RMW."""
    origin = report.get("origin")
    runtime = report.get("runtime")

    def build(prev):
        if origin == "restored":
            return reconcile_restored(prev, report, vm_id, now=now_iso(), attach_seq=attach_seq)
        cold = origin == "empty" and is_cold_attach(prev, report, vm_id)
        return apply_runtime(prev, runtime, vm_id, trigger="attach",
                             empty_namespace=cold, attach_seq=attach_seq)
    return build


def _mark_no_support(st: _SessionSync, vm_id: str) -> None:
    st.synced_vm_id = vm_id
    st.no_dag_support.add(vm_id)
    st.attach_attempts.pop(vm_id, None)
    st.expect_restore = False
    if vm_id not in _no_support_logged:
        _no_support_logged.add(vm_id)
        logger.info("dag: vm %s has no /dag endpoint (older image); pushes disabled for it", vm_id)


async def _attach(sid, st: _SessionSync, vm_manager, vm_id: str, endpoint: str, nid=None) -> str:
    """Read the VM's report and fold it into the DB document. Returns
    'ok' | 'retry' | 'no_support'. No PUT happens before this returns 'ok'."""
    try:
        if nid is None:
            nid = await notebook_for_session(sid)
        prev_doc = await astorage.notebook_dag_get(nid) if nid is not None else None
        # attach_seq is read BEFORE the GET: runs captured after
        # this point are never marked not_in_checkpoint by this attach.
        attach_seq = int((prev_doc or {}).get("run_seq") or 0)
        report = await _get_report(vm_manager, vm_id, endpoint)
        if report is None:
            _mark_no_support(st, vm_id)
            return "no_support"
        if st.expect_restore and report.get("origin") == "empty":
            for i in range(EMPTY_RETRIES):
                logger.info("dag attach: vm %s reports origin=empty after a restore launch; "
                            "re-check %d/%d", vm_id, i + 1, EMPTY_RETRIES)
                await asyncio.sleep(EMPTY_RETRY_DELAY)
                report = await _get_report(vm_manager, vm_id, endpoint)
                if report is None:
                    _mark_no_support(st, vm_id)
                    return "no_support"
                if report.get("origin") != "empty":
                    break
        if nid is not None:
            await astorage.notebook_dag_update(nid, _attach_build(report, vm_id, attach_seq))
    except Exception as e:
        n = st.attach_attempts.get(vm_id, 0) + 1
        if n >= ATTACH_MAX_ATTEMPTS:
            logger.warning("dag attach gave up for vm %s: %s", vm_id, e)
            st.attach_attempts.pop(vm_id, None)
            st.synced_vm_id = vm_id
            st.expect_restore = False
            return "ok"
        st.attach_attempts[vm_id] = n
        logger.info("dag attach attempt %d/%d for vm %s failed: %s", n, ATTACH_MAX_ATTEMPTS, vm_id, e)
        return "retry"
    st.attach_attempts.pop(vm_id, None)
    st.synced_vm_id = vm_id
    st.expect_restore = False
    st.vm_base = None   # the next push is a full PUT
    return "ok"


def _warn_put(vm_id: str, detail) -> None:
    mono = time.monotonic()
    if mono - _last_put_warn.get(vm_id, -PUT_WARN_INTERVAL - 1) >= PUT_WARN_INTERVAL:
        _last_put_warn[vm_id] = mono
        logger.warning("dag push to vm %s failed: %s", vm_id, detail)


def _dumps(v) -> bytes:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _patch(vm_manager, vm_id: str, endpoint: str, body: bytes):
    """PATCH /dag → status code, or None on a transport error (warned)."""
    try:
        token = await vm_manager.get_auth_token(vm_id)
        resp = await http_clients.vm_client().patch(
            f"https://{endpoint}/dag", headers=_vm_headers(token), content=body,
            timeout=VM_TIMEOUT)
    except Exception as e:
        _warn_put(vm_id, f"PATCH {type(e).__name__}: {e}")
        return None
    return resp.status_code


async def _push_current(sid, st: _SessionSync, vm_manager, vm_id: str, endpoint: str, nid=None,
                        *, force: bool = False) -> bool:
    """Bring the VM copy to the CURRENT DB document (without runtime/pending).

    With an acknowledged base for this VM: PATCH the cumulative delta base →
    current (nothing at all when the VM-held part is unchanged, unless `force`:
    flush then sends the small marker PATCH, which also verifies the VM still
    holds the base). 409 / 404 / 405, no base, or a delta above the size guard →
    full PUT. Only ever diffs against the doc read now, so piled-up captures
    collapse into one push and no intermediate delta is sent."""
    if nid is None:
        nid = await notebook_for_session(sid)
    if nid is None:
        return True   # scratch session: nothing to push
    doc = await astorage.notebook_dag_get(nid)
    if not doc:
        return True   # no document yet: nothing to push
    vm_doc = strip_for_vm(doc)
    payload = _dumps(vm_doc)
    base = st.vm_base if st.vm_base_vm == vm_id else None
    if base is not None and vm_id not in st.no_patch:
        delta = diff_docs(base, vm_doc)
        if not force and (rev(base) == rev(vm_doc) or not delta_has_vm_changes(delta)):
            return True   # the VM-held part is current (runtime-only changes are never pushed)
        body = _dumps(delta)
        if not send_full(len(body), len(payload)):
            status = await _patch(vm_manager, vm_id, endpoint, body)
            if status == 200:
                st.vm_base = vm_doc
                return True
            if status in (404, 405):
                if vm_id not in st.no_patch:
                    logger.info("dag: vm %s has no PATCH /dag (older image); full PUTs only", vm_id)
                st.no_patch.add(vm_id)
            elif status != 409:
                if status is not None:
                    _warn_put(vm_id, f"PATCH status {status}")
                st.vm_base = None
                return False
            # 409 (base/hash mismatch, no document) or no PATCH → full PUT below.
    st.vm_base = None
    if len(payload) > DAG_MAX_BYTES:
        logger.warning("dag push skipped for vm %s: document is %d bytes (> %d)",
                       vm_id, len(payload), DAG_MAX_BYTES)
        return False
    try:
        token = await vm_manager.get_auth_token(vm_id)
        resp = await http_clients.vm_client().put(
            f"https://{endpoint}/dag", headers=_vm_headers(token), content=payload,
            timeout=VM_TIMEOUT)
    except Exception as e:
        _warn_put(vm_id, f"{type(e).__name__}: {e}")
        return False
    if resp.status_code == 404:
        _mark_no_support(st, vm_id)
        return False
    if resp.status_code != 200:
        _warn_put(vm_id, f"status {resp.status_code}")
        return False
    st.vm_base = vm_doc
    st.vm_base_vm = vm_id
    return True


def forget_vm(vm_id, sid=None) -> None:
    """Drop in-process state for a terminated/dead VM (and its session's sync
    state) so a long-lived proxy does not accumulate one entry per VM/session.
    The session entry is kept while it is busy or already serves another VM
    (a relaunch may be attaching it), so this can never cancel a live attach."""
    if vm_id:
        _last_put_warn.pop(vm_id, None)
        _no_support_logged.discard(vm_id)
    if not sid:
        return
    st = _sync.get(sid)
    if st is None:
        return
    if st.synced_vm_id not in (None, vm_id):
        return
    if st.dirty or st.attach_wanted or (st.task is not None and not st.task.done()):
        return
    if st.attach_future is not None and not st.attach_future.done():
        return
    if st.retry_handle is not None:
        st.retry_handle.cancel()
    _sync.pop(sid, None)


async def flush(sid, nid=None, *, vm_id=None, endpoint=None, timeout: float = 5.0) -> bool:
    """Save-before-checkpoint (proxy-initiated): ALWAYS bring the VM to the
    current DB document (attaching first if needed) — a PATCH from the VM's
    acknowledged base (at least the small marker PATCH, which confirms the VM
    still holds that base) or a full PUT — so code-only edits since the last
    execution reach dag.json. Bounded by `timeout`; never raises."""
    if not sid:
        return False
    try:
        return await asyncio.wait_for(_flush(sid, nid, vm_id, endpoint), timeout)
    except Exception as e:
        logger.warning("dag flush before checkpoint failed for session %s: %s",
                       sid, f"{type(e).__name__}: {e}")
        return False


async def sync_vm_copy(sid, nid=None, *, timeout: float = 3.0) -> bool:
    """Before a variables read/delete (deleted-cell flag, F1): bring the VM copy
    to the current DB document so the VM's own origin classification sees the
    SAVED cell list, without needing a cell run. Unlike `flush` it is not forced:
    nothing is sent when the VM-held part is already current. Only ever called on
    a request that is already going to the VM (never from a save, so a save alone
    never wakes a VM). Bounded by `timeout`; never raises."""
    if not sid:
        return False
    try:
        return await asyncio.wait_for(_flush(sid, nid, None, None, force=False), timeout)
    except Exception as e:
        logger.debug("dag sync before variables read failed for session %s: %s",
                     sid, type(e).__name__)
        return False


async def _flush(sid, nid, vm_id, endpoint, force=True) -> bool:
    vm_manager = _vm_manager
    if vm_manager is None:
        return False
    if nid is None:
        nid = await notebook_for_session(sid)
    if nid is None:
        return True   # scratch session: no document to flush
    st = _state(sid)
    async with st.lock:
        if not vm_id or not endpoint:
            sv = await vm_manager.get_session_vm_async(sid)
            if not sv or not sv.get("vm_id"):
                return False
            vm_id, endpoint = sv["vm_id"], sv.get("endpoint") or ""
        if vm_id in st.no_dag_support:
            return True   # older image: the VM cannot hold a document
        if st.synced_vm_id != vm_id:
            res = await _attach(sid, st, vm_manager, vm_id, endpoint, nid=nid)
            if res == "no_support":
                return True
            if res != "ok":
                return False   # no PUT before a successful attach
            _resolve_attach(st)
        return await _push_current(sid, st, vm_manager, vm_id, endpoint, nid=nid, force=force)


# ─── §4.2 open-path rebuild for the notebook routes ───────────────────────────

async def get_or_build(nid, cells=None):
    """The notebook's document, rebuilt from its stored cells when missing or
    out of sync. The outer in_sync check is only a fast path; the authoritative
    one runs inside the locked RMW on the cells storage reads there, so an open
    can never overwrite a newer save's document. Never contacts the VM."""
    try:
        doc = await astorage.notebook_dag_get(nid)
    except Exception as e:
        logger.warning("dag read failed nb=%s: %s", nid, e)
        return None
    if doc is not None and cells is not None and in_sync(doc, cells):
        return doc

    def build(prev, cells_in_txn):
        if in_sync(prev, cells_in_txn):
            return None
        return apply_cells(prev, cells_in_txn, trigger="open", now=now_iso())
    try:
        built = await astorage.notebook_dag_update(nid, safe(build, nb=nid, trigger="open"),
                                                   with_cells=True)
    except Exception as e:
        logger.warning("dag open rebuild failed nb=%s: %s", nid, e)
        return doc
    return built if built is not None else doc


def _reset_for_tests() -> None:
    """Drop all in-process state (tests only)."""
    global _vm_manager
    _vm_manager = None
    _session_cache.clear()
    _sync.clear()
    _last_put_warn.clear()
    _no_support_logged.clear()
