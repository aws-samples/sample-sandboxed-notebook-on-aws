"""
Sandboxed Notebook on AWS Proxy — Application Entrypoint

A lightweight proxy that:
1. Manages MicroVM lifecycle (launch, suspend, resume, terminate, rotation)
2. Proxies requests to MicroVMs with auth tokens injected
3. Relays WebSocket terminal sessions to MicroVM shell (SHELL_INGRESS)
4. Serves notebook CRUD, metrics, session management, and AI chat APIs
5. Keeps AWS credentials server-side (never exposed to the browser)

Usage:
    python3 -m uvicorn proxy.server:app --port 8081

Architecture:
    proxy/
      server.py                 ← This file (app setup, lifespan, health, router wiring)
      storage/                  ← Shared storage layer (interface + SQLite and Postgres impls)
      platform/                 ← Smart MicroVM Service layer (reusable, app-agnostic)
        microvm_manager.py      ← VM lifecycle state (tokens, cost, reactive recovery)
        cost_tracker.py         ← Burst + baseline cost tracking with DB persistence
        vm_detail.py            ← VM card detail payload
        vm_metrics_history.py   ← Per-run metrics history payload
        burst_time.py           ← Burst-time summary from stored metrics
        auth.py                 ← Shared-secret auth, CORS origins, bind guard
        aws_clients.py          ← Cached boto3 clients
        http_clients.py         ← Shared httpx clients (proxy → VM, aux)
        defaults.py             ← Product defaults (DEFAULT_MEMORY_MIB)
        errors.py               ← Catch-all error handler, json_error, metric lines
        package_classifier.py   ← PyPI-based package category detection
        datasources/            ← Provider registry + schema discovery (S3, DynamoDB,
                                  Athena, Aurora, Bedrock KB, SageMaker endpoints,
                                  CloudWatch Logs, local files)
        routes/
          microvm.py            ← Launch, terminate, suspend, resume, proxy, instances, costs
          sessions.py           ← S3 session checkpoints, data sources, secrets/env
          metrics.py            ← Image tiers
          terminal.py           ← WebSocket terminal relay → VM shell (SHELL_INGRESS)
      notebook/                 ← Notebook application layer (specific to this project)
        ai/                     ← AI module (Strands SDK + Amazon Bedrock)
        routes/
          ai.py                 ← AI chat, explain, fix, suggest-tag
          notebooks.py          ← Notebook CRUD
          intel.py, logs.py, format.py ← Workbook intel, VM logs, cell formatting
"""

import os
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from proxy.storage import storage, _connection_string
from proxy.platform.microvm_manager import MicrovmManager, POLL_INTERVAL_MS, AWS_REGION, IMAGE_ARN
from proxy.platform import http_clients
from proxy.platform.auth import (
    require_proxy_auth,
    allowed_origins,
    assert_loopback_bind,
    AUTH_HEADER,
)

# Timestamped lines (HH:MM:SS.mmm); the `LEVEL:name:message` tail is the default
# basicConfig shape, so existing greps like 'INFO:ai_assistant.notebook_agent:' still match.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs every OUTBOUND request at INFO (VM /metrics, /files, /data-catalog,
# ...). Failures still surface through the proxy's own warnings and the access
# middleware times the inbound request, so keep only httpx warnings and errors.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Guard the actual bind the installer uses (`-m uvicorn proxy.server:app
# --host $PROXY_BIND_HOST`). Runs at import time — i.e. the moment uvicorn loads
# this module — so a non-loopback bind is refused before the app starts, rather
# than hidden in a __main__ block the installer never executes.
assert_loopback_bind("PROXY_BIND_HOST", "proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle (replaces the deprecated @app.on_event hooks).

    Startup: start the housekeeping loop and restore session-registry + cost
    tracking state from the DB. Shutdown (ECS deploy/drain): cancel housekeeping,
    flush cost records (handoff-safe burst persistence), and close terminal relays.
    """
    # --- startup ---
    # Explicitly size the default asyncio executor that backs every
    # asyncio.to_thread(...) offload (DB via astorage, control-plane SDK calls,
    # STS/S3 staging). Python's default, min(32, cpu+4), is only ~6-8 workers on a
    # 2-4 vCPU host, which would serialize these blocking I/O offloads.
    # max(16, cpu*4) is sized for I/O-bound work. Overridable via
    # PROXY_THREAD_POOL_SIZE (see scripts/config.sh).
    _pool_default = max(16, (os.cpu_count() or 4) * 4)
    try:
        _pool_size = int(os.environ.get("PROXY_THREAD_POOL_SIZE") or _pool_default)
    except ValueError:
        _pool_size = _pool_default
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_pool_size, thread_name_prefix="proxy-to-thread")
    )
    logger.info(f"Default to_thread executor sized to {_pool_size} workers "
                f"(cpu_count={os.cpu_count()}, override=PROXY_THREAD_POOL_SIZE)")

    # Shared proxy->VM and aux httpx clients (connection reuse, connect-phase
    # transport retries). Closed in step 4 of shutdown below.
    http_clients.startup()

    app.state.housekeeping_task = asyncio.create_task(_housekeeping_loop())
    app.state.vm_manager.restore_session_registry_from_db()
    # Prune session_registry rows whose VM is provably dead (orphans left by a
    # VM that died while the proxy was down, or a teardown that wiped VMs without
    # clearing this table). Without this, the per-request routing map resolves
    # those rows to dead Lambda URLs and every client poll 502s forever. Run as a
    # background task so a slow control-plane can't delay the app coming up; it's
    # fully guarded internally.
    app.state.reconcile_task = asyncio.create_task(
        app.state.vm_manager.reconcile_session_registry()
    )
    app.state.vm_manager.cost_tracker.load_from_db(storage)

    # Pre-warm the cloud-catalog cache at startup, in the BACKGROUND. The first
    # discover_full_catalog() fans out to S3/DynamoDB/Athena/Aurora (~7s); it's
    # cached (CATALOG_CACHE_TTL_SECONDS) so later launches are instant, but a
    # COLD cache means the FIRST user launch eats the full ~7s. Warming it here
    # — while the stack is still finishing startup and nobody is launching yet —
    # moves that one-time cost off the first user launch. A launch that races in
    # before this finishes JOINS the same single-flight discovery (registry.py).
    # After startup the GET /instances poll keeps the cache warm (refresh-ahead),
    # so it never goes cold while a UI is open. bind_main_loop pins the loop the
    # registry's background refreshes run on (worker-thread sync callers hand
    # work to it instead of discovering on their own loop).
    from proxy.platform.datasources import registry as _catalog_registry
    _catalog_registry.bind_main_loop()

    async def _prewarm_catalog_cache():
        try:
            from proxy.platform.datasources import registry
            entries = await registry.discover_full_catalog()
            logger.info(f"Catalog cache pre-warmed: {len(entries)} cloud source(s) "
                        "— first launch will hit the cache")
        except Exception as e:
            logger.warning(f"Catalog pre-warm failed (first launch will "
                           f"discover on demand): {e}")

    app.state.catalog_prewarm_task = asyncio.create_task(_prewarm_catalog_cache())
    try:
        yield
    finally:
        # --- shutdown --- (order matters: stop housekeeping, flush cost, close relays)
        # 1. Stop housekeeping (and the one-shot startup reconcile, if still running).
        for _attr in ("housekeeping_task", "reconcile_task", "catalog_prewarm_task"):
            task = getattr(app.state, _attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        # 1b. Cancel + await the in-flight background catalog refresh (keep-warm /
        # stale-while-revalidate), so no task is left pending at loop close.
        try:
            await _catalog_registry.shutdown_catalog_refresh()
        except Exception as e:
            logger.warning(f"Graceful shutdown: catalog refresh cancel failed: {e}")
        # 2. Flush cost (handoff-safe burst persistence).
        try:
            n = app.state.vm_manager.cost_tracker.persist_all(storage)
            logger.info(f"Graceful shutdown: flushed cost for {n} MicroVM record(s)")
        except Exception as e:
            logger.warning(f"Graceful shutdown: cost flush failed: {e}")
        # 3. Close active terminal relays.
        terminals = list(getattr(app.state, "active_terminals", set()))
        for ws in terminals:
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception:
                pass
        if terminals:
            logger.info(f"Graceful shutdown: closed {len(terminals)} terminal relay(s)")
        # 4. Close the shared httpx clients.
        try:
            await http_clients.aclose()
        except Exception as e:
            logger.warning(f"Graceful shutdown: http client close failed: {e}")


# --- FastAPI App ---
# A global dependency applies require_proxy_auth to EVERY route on this app
# (C1). The bind is not visible from the included routers, so per the security
# default the whole listener is gated rather than trusting the network. GET
# /health is exempted below with an explicit per-route `dependencies=[]`.
app = FastAPI(
    title="Sandboxed Notebook on AWS — Token Proxy",
    lifespan=lifespan,
    dependencies=[Depends(require_proxy_auth)],
)

# C2: CORS allowlist (loopback UI origins only) instead of the previous
# wildcard. allow_credentials is False (we authenticate with an explicit header,
# not cookies, so credentialed cross-origin is neither needed nor safe), and the
# methods/headers are enumerated — notably X-Proxy-Auth so the browser can send
# the shared secret on cross-origin requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Session-Id", AUTH_HEADER],
)

# --- Request-timing access log ---
# The proxy previously logged NO per-request latency (only httpx lines for its
# OUTBOUND calls to VMs), so it was impossible to tell whether e.g. a notebook
# save (PUT /notebooks/{id}) or a VM launch (POST /microvm/*) was the slow part of
# "opening a notebook". This logs one line per request with method/path/status/ms.
# High-frequency UI pollers are demoted to DEBUG (fast+2xx only) so they don't
# drown the useful lines — same policy as the VM app's access log.
import time as _time

# Path PREFIXES the UI polls frequently (instances list, per-VM metrics, session
# cost, logs stream). Matched by prefix because many carry an id segment.
_PROXY_QUIET_PREFIXES = (
    "/instances",
    "/metrics",
    "/microvm/",           # proxied VM calls (metrics/variables/etc. fan-out)
    "/datasources/catalog",
    "/sessions/costs",     # POST every 15 s from useInstances (DB read only)
    "/logs",
)
# Prefixes quiet for GET only: the read side is polled (intel status every 4 s
# while generating; data-source list refreshes), while the writes on the same
# prefix (POST /workbook-intel/generate, DELETE /datasources/s3-file, SageMaker
# lifecycle, promote-attachment) stay at INFO.
_PROXY_QUIET_GET_PREFIXES = (
    "/workbook-intel",
    "/datasources",
)
# LOG_ACCESS_POLLS=1 restores INFO access lines for the polled paths (debugging).
_LOG_ACCESS_POLLS = os.environ.get("LOG_ACCESS_POLLS", "").strip().lower() in ("1", "true", "yes", "on")

def _proxy_quiet(path: str, method: str = "GET") -> bool:
    if _LOG_ACCESS_POLLS:
        return False
    if any(path.startswith(p) for p in _PROXY_QUIET_PREFIXES):
        return True
    return method == "GET" and any(path.startswith(p) for p in _PROXY_QUIET_GET_PREFIXES)

@app.middleware("http")
async def _proxy_access_log(request: Request, call_next):
    start = _time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (_time.perf_counter() - start) * 1000
        logger.warning(f"\u2190 {request.method} {request.url.path} 500 {elapsed_ms:.0f}ms (unhandled)")
        raise
    elapsed_ms = (_time.perf_counter() - start) * 1000
    path = request.url.path
    line = f"\u2190 {request.method} {path} {response.status_code} {elapsed_ms:.0f}ms"
    # Demote fast, successful high-frequency polls to DEBUG; keep slow (>1s) or
    # non-2xx visible so a slow save/launch/proxied call always surfaces.
    if _proxy_quiet(path, request.method) and elapsed_ms < 1000 and 200 <= response.status_code < 400:
        logger.debug(line)
    elif response.status_code >= 500:
        logger.warning(line)
    else:
        logger.info(line)
    return response

# --- Initialize storage ---
storage.initialize(connection_string=_connection_string)

# --- Initialize MicroVM manager (shared state) ---
app.state.vm_manager = MicrovmManager()
# Active terminal WebSocket relays, tracked so graceful shutdown can close them
# cleanly on an ECS deploy/drain instead of hard-cutting the connection.
app.state.active_terminals = set()
# Handle to the housekeeping background task so shutdown can cancel it.
app.state.housekeeping_task = None

# --- Register route modules ---
# Platform layer (MicroVM lifecycle, sessions, metrics)
from proxy.platform.routes.microvm import router as microvm_router, get_persistence_mode, _pool_enabled
from proxy.platform.routes.sessions import router as sessions_router
from proxy.platform.routes.metrics import router as metrics_router

# Notebook layer (AI, notebook CRUD)
from proxy.notebook.routes.notebooks import router as notebooks_router
from proxy.notebook.routes.ai import router as ai_router
from proxy.notebook.routes.logs import router as logs_router
from proxy.notebook.routes.intel import router as intel_router
from proxy.notebook.routes.format import router as format_router
from proxy.platform.routes.terminal import router as terminal_router

app.include_router(microvm_router)
app.include_router(notebooks_router)
app.include_router(metrics_router)
app.include_router(sessions_router)
app.include_router(ai_router)
app.include_router(logs_router)
app.include_router(intel_router)
app.include_router(format_router)
app.include_router(terminal_router)

# --- Centralized error handling + metrics ---
# App-level catch-all: any exception escaping any route is mapped to a clean HTTP
# status, logged with a traceback, and recorded as a structured metric line.
from proxy.platform.errors import install_error_handling
install_error_handling(app, namespace="MicroVMNotebookProxy")


# --- Background tasks ---
METRICS_RETENTION_HOURS = int(os.environ.get("METRICS_RETENTION_HOURS", "24"))


async def _housekeeping_loop():
    """Periodic cleanup: old metrics data."""
    while True:
        await asyncio.sleep(60)
        try:
            # Sync DB delete — run on the default executor, off the event loop.
            await asyncio.to_thread(storage.metrics_cleanup, hours=METRICS_RETENTION_HOURS)
        except Exception:
            pass


# --- Health endpoint ---
# GET /health is the ONLY route exempt from the app-level auth dependency. A
# FastAPI app-level dependency runs on every route and cannot be cleared by a
# route-level `dependencies=[]`, so the exemption is implemented inside
# require_proxy_auth itself (it allows GET /health through). Liveness/health
# checks must work without a token.
@app.get("/health")
async def health():
    vm_manager = app.state.vm_manager
    return {
        "status": "proxy running",
        "region": AWS_REGION,
        "image_arn": IMAGE_ARN or "(not configured)",
        "cached_tokens": await vm_manager.token_cache.size(),
        "active_instances": len(vm_manager.active_microvms),
        "poll_interval_ms": POLL_INTERVAL_MS,
        "persistence_mode": get_persistence_mode(),
        "max_lifetime_seconds": int(os.environ.get("MAX_LIFETIME_SECONDS", "28800")),
        "idle_timeout_seconds": int(os.environ.get("IDLE_TIMEOUT_SECONDS", "60")),
        "pool_enabled": _pool_enabled(),
    }


# --- Reactive recovery timings (per-phase breakdown of the last recovery) ---
@app.get("/recovery-timings")
async def recovery_timings():
    """Per-phase timing of the most recent reactive recovery:
    launch_ms → wait_healthy_ms → restore_ms (proxy-observed) → relink_ms,
    plus restore_pkl_ms (the VM's own in-VM restore time) and total_ms."""
    return app.state.vm_manager.last_recovery_timings or {}


# --- Package categories endpoint ---
@app.get("/package-categories")
async def package_categories(session_id: str = None):
    """
    Return package categorization data for the frontend.
    - categories: static mapping of package → category
    - import_aliases: recommended import statements
    - category_order: display order for category groups
    - user_installed: packages installed by the user in this session (with categories),
      from the VM's own list (live /vm-status, else S3 requirements.txt) merged
      with the in-memory /track-install record (see proxy/platform/user_packages.py)
    """
    from proxy.platform.package_classifier import get_all_categories, get_all_import_aliases, get_category_order
    from proxy.platform import user_packages as up
    from proxy.platform.routes.microvm import _detail_s3_read, _detail_vm_status_fetch

    vm_manager = app.state.vm_manager
    user_packages = []
    if session_id:
        try:
            user_packages = await up.user_installed(
                vm_manager, session_id,
                vm_status_fetch=lambda mid: _detail_vm_status_fetch(vm_manager, mid),
                s3_read=lambda sid: _detail_s3_read(vm_manager, sid))
        except Exception as e:
            logger.warning(f"/package-categories user list fell back to memory: {type(e).__name__}")
            user_packages = vm_manager.get_user_installed_packages(session_id)

    return {
        "categories": get_all_categories(),
        "import_aliases": get_all_import_aliases(),
        "category_order": get_category_order(),
        "user_installed": user_packages,
    }


@app.post("/track-install")
async def track_install(request: Request):
    """
    Track a user-installed package for categorization.
    Called by the frontend after a successful pip install.
    Body: { "session_id": "...", "package": "seaborn" }
    """
    from proxy.platform.package_classifier import classify_and_cache, get_import_alias

    body = await request.json()
    session_id = body.get("session_id", "")
    package = body.get("package", "")

    if not package:
        return Response(status_code=400, content='{"error": "package required"}', media_type="application/json")

    # Classify the package (static first, then PyPI lookup)
    category = await classify_and_cache(package)
    import_alias = get_import_alias(package)

    # Store in vm_manager (only if session provided)
    if session_id:
        vm_manager = app.state.vm_manager
        vm_manager.record_user_install(session_id, package, category)
        from proxy.platform.user_packages import invalidate
        invalidate(session_id)

    return {"package": package, "category": category, "import_alias": import_alias}
