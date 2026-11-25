"""
pool_manager FastAPI server — /grant /status /health /admin/refill
/admin/drain + lifespan-managed background loops (design §8). Mirrors
proxy/server.py's lifespan + asyncio.create_task background-loop pattern.

Usage:
    python3 -m pool_manager.server
or:
    python3 -m uvicorn pool_manager.server:app --port 8090
"""

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from proxy.storage import storage as default_storage
from proxy.platform.auth import assert_loopback_bind, require_pool_auth

from pool_manager.backend_factory import build_backend
from pool_manager.config import PoolConfig, load_config
from pool_manager.manager import (GrantError, PoolEmpty, PoolManager, grant_log_context,
                                  redact_grant_text)

# Same timestamped shape as the proxy (proxy/server.py) so pool lines can be
# correlated with proxy lines in the shared terminal log.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs every spare-VM /health poll at INFO while minting; keep warnings only.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Guard the installer bind (`-m uvicorn pool_manager.server:app
# --host $POOL_BIND_HOST`) at import time — before the module-level
# `app = create_app()` below — so a non-loopback bind is refused rather than
# relying on the __main__ block (which the installer never runs). Loopback is
# the default, so a normal local install is unaffected.
assert_loopback_bind("POOL_BIND_HOST", "pool_manager")


# Backend construction lives in the side-effect-free pool_manager.backend_factory
# module (so callers like pool_manager.reset_local_pool can build a backend
# without importing this server module and triggering its module-level
# `app = create_app()`). Kept as a thin alias here for back-compat.
_build_backend = build_backend


def create_app(config: PoolConfig = None, backend=None, storage=None) -> FastAPI:
    """FastAPI app factory, test-injectable: pass config/backend/storage to
    build an app over a fake backend + temp storage without touching env vars
    or the real proxy storage singleton."""
    config = config or load_config()
    backend = backend if backend is not None else _build_backend(config)
    storage = storage if storage is not None else default_storage

    manager = PoolManager(backend=backend, storage=storage, config=config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A standalone pool_manager (e.g. its own container) may start against a
        # DB the proxy never initialized. Create only the tables it uses, if
        # missing; existing tables and rows are left untouched.
        await asyncio.to_thread(storage.initialize_pool_manager_schema)
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()
            # Close the backend's shared HTTP client (AwsVmBackend); backends
            # without one (FakeVmBackend) simply have no aclose().
            aclose = getattr(backend, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="VM Pool Manager", lifespan=lifespan)
    app.state.manager = manager
    app.state.config = config

    @app.post("/grant", dependencies=[Depends(require_pool_auth)])
    async def grant(request: Request):
        # Parse + validate the body BEFORE handing it to the manager. A malformed
        # request (non-JSON body, or missing a required field) must come back as a
        # clean 400 — NOT an unhandled KeyError/ValueError that escapes to the ASGI
        # layer, logs a traceback, and resets the connection (which then surfaces as
        # "Connection reset by peer" on the NEXT request, disrupting other in-flight
        # grants). One bad client must never destabilize the manager under load.
        try:
            req = await request.json()
        except Exception:
            return JSONResponse(status_code=400,
                                content={"error": "BAD_REQUEST",
                                         "detail": "body is not valid JSON"})
        if not isinstance(req, dict):
            return JSONResponse(status_code=400,
                                content={"error": "BAD_REQUEST",
                                         "detail": "body must be a JSON object"})
        missing = [f for f in ("arch", "size", "session_id") if req.get(f) in (None, "")]
        if missing:
            return JSONResponse(status_code=400,
                                content={"error": "BAD_REQUEST",
                                         "detail": f"missing required field(s): {', '.join(missing)}"})
        try:
            result = await manager.grant(req)
            return result
        except PoolEmpty:
            return JSONResponse(status_code=409, content={"error": "POOL_EMPTY"})
        except GrantError as e:
            # Already redacted by manager.grant; masked again in case a caller
            # raises GrantError with a raw message.
            return JSONResponse(status_code=502, content={"error": "GRANT_FAILED",
                                                          "detail": redact_grant_text(e, req)})
        except Exception as e:
            # Last-resort guard: ANY other unexpected error becomes a clean 500 so
            # it never escapes to uvicorn as an unhandled ASGI exception (which
            # resets the connection and breaks the next caller). Log it so the
            # root cause is still visible. NEVER the request itself: it carries the
            # user's plaintext env_vars values and secret refs. Only the session/
            # tier identifiers, plus the traceback with any echoed value masked
            # (logger.error, not logger.exception, so the raw message isn't added).
            tb = redact_grant_text("".join(traceback.format_exception(e)), req)
            logger.error(f"grant: unexpected error ({grant_log_context(req)})\n{tb}")
            return JSONResponse(status_code=500,
                                content={"error": "INTERNAL", "detail": redact_grant_text(e, req)})

    @app.get("/status")
    async def status():
        return await manager.status_async()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        """Readiness probe (distinct from /health liveness).

        Liveness (/health)  = 'the process is up' — dependency-free, always 200.
        Readiness (/ready)  = 'the DB is reachable AND the pool is at target' —
        the signal an ALB/ECS readiness check should gate traffic on, matching
        what local_install.sh already polls /status for.

        Returns 200 only when BOTH are true, else 503. Strictly READ-ONLY:
        it never mints or mutates a spare.
          (a) DB reachable: a cheap round-trip through the SAME storage the
              manager uses (storage.pool_vm_list()); a successful return proves
              the backend connection works. Any exception -> not ready.
          (b) Pool filled: every configured tier has at least `target` spares in
              the 'available' state (ready spares, not merely rows — a tier whose
              rows are all 'granting'/'granted' is NOT filled for readiness). A
              tier with target 0 is trivially satisfied.
        """
        # (a) DB reachability.
        try:
            await asyncio.to_thread(storage.pool_vm_list)
        except Exception as e:
            logger.warning(f"/ready: DB unreachable: {e}")
            return JSONResponse(status_code=503,
                                content={"status": "not_ready",
                                         "reason": "db_unreachable"})

        # (b) Pool-at-target, using the same per-tier view as /status.
        status = await manager.status_async()
        tiers_report = {}
        below_target = False
        for key, tier in status["tiers"].items():
            target = tier["target"]
            available = tier["counts"].get("available", 0)
            tiers_report[key] = {"available": available, "target": target}
            if available < target:
                below_target = True

        if below_target:
            return JSONResponse(status_code=503,
                                content={"status": "not_ready",
                                         "reason": "pool_below_target",
                                         "tiers": tiers_report})
        return JSONResponse(status_code=200,
                            content={"status": "ready", "tiers": tiers_report})

    @app.post("/admin/refill", dependencies=[Depends(require_pool_auth)])
    async def admin_refill():
        await manager.fill_once()
        return await manager.status_async()

    @app.post("/admin/drain", dependencies=[Depends(require_pool_auth)])
    async def admin_drain():
        """Terminate + delete every pool_vms row the backend knows about,
        including granting/granted — a full teardown for ops/tests."""
        rows = await asyncio.to_thread(storage.pool_vm_list)
        backend_vms = {vm.vm_id: vm for vm in await backend.list_all()}
        for row in rows:
            vm_id = row["vm_id"]
            vm = backend_vms.get(vm_id)
            if vm is not None:
                try:
                    await backend.terminate(vm)
                except Exception as e:
                    logger.warning(f"admin/drain: terminate failed for {vm_id}: {e}")
            await asyncio.to_thread(storage.pool_vm_delete, vm_id)
        return {"drained": len(rows)}

    return app


# Module-level app for `uvicorn pool_manager.server:app`.
app = create_app()
if __name__ == "__main__":
    import uvicorn
    _config = app.state.config
    # Bind to loopback only: the pool manager is a LOCAL service reached by the
    # proxy over localhost (POOL_MANAGER_URL defaults to http://localhost:...).
    # /grant and /admin/* require POOL_AUTH_TOKEN (require_pool_auth), but
    # /status, /health and /ready are unauthenticated, so the control plane is
    # kept off the local network.
    uvicorn.run(app, host="127.0.0.1", port=_config.port)
