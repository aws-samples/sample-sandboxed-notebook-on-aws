"""
Notebook sandbox for AWS Lambda MicroVMs — application entrypoint

This FastAPI app runs inside a MicroVM and provides:
1. Code execution endpoints (the agent/UI sends code here)
2. Lifecycle hooks (AWS calls these during MicroVM state transitions)

The key insight: this is a STATEFUL server. The executor namespace persists
across HTTP requests AND across suspend/resume cycles. When the MicroVM
suspends, all memory is snapshotted. On resume, it's exactly as it was.

Architecture:
    app/
      server.py           ← This file (app setup, shared state, pre-loaded libs)
      platform/
        hooks.py          ← MicroVM lifecycle hooks (ready, validate, run, suspend,
                            resume, terminate) and the proxy-called /checkpoint-save,
                            /restore-state, /rebind
        checkpoint/       ← S3 checkpoint/restore package (manager, delta, archive,
                            namespace_filter)
        errors.py         ← app-level error handler + error metric line
      notebook/
        executor.py       ← SandboxExecutor class (code execution engine)
        code_engine.py    ← /execute, /execute-batch, /set-variable, /set-env,
                            /refresh-secrets
        sql_engine.py     ← SQL execution: /execute-sql with auto-routing
        routes.py         ← variables, lint, introspect, helpers, files, packages,
                            data catalog, health/metrics, interrupt/reset
        dag_store.py      ← PUT/GET /dag (proxy-pushed dependency graph)
        vm_status.py      ← GET /vm-status (Live MicroVMs card)
        data_catalog.py   ← schema catalog (proxy-pushed cloud + local /tmp files)
        var_inspect.py    ← variable schema / inspect payloads
        scratch.py        ← AI agent exploration scratch scopes
        helpers/          ← helper functions injected into every cell
"""

import os
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.notebook.executor import SandboxExecutor

# --- Neutralize dill's global pickle patch (MUST run before joblib is used) ---
# The VM uses dill for session checkpoint/restore (serializing the notebook
# namespace). Importing dill has a process-wide SIDE EFFECT: it registers its own
# reducers into the standard pickle dispatch, so afterwards even plain pickle/joblib
# emit dill-specific opcodes (e.g. a builtin `slice` — as in a ColumnTransformer's
# output_indices_ — serializes as dill._dill._load_type). Any artifact written that
# way then FAILS to load anywhere dill isn't installed: a SageMaker serving container,
# a plain-sklearn consumer, etc. (observed: endpoint /ping 500, "No module named
# 'dill'", 180s timeout). dill.extend(use_dill=False) reverts pickle/joblib to the
# STANDARD-library behaviour while leaving dill's OWN Pickler intact — so checkpoint/
# restore keeps working, but save_model / sm_deploy_* produce portable, dill-free
# artifacts. This is the documented fix for the well-known dill↔joblib interaction;
# it must run at startup, before joblib binds to the (patched) reduce path.
try:
    import dill as _dill
    _dill.extend(use_dill=False)
except Exception:  # noqa: BLE001 — if dill is absent/changed, nothing to neutralize.
    pass

# --- Logging ---
# Unified log format for all loggers (app + uvicorn access + uvicorn error)
# This ensures consistent output in CloudWatch for the logs panel.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
# Emit at DEBUG so the demoted high-frequency poll lines (see the _access_log
# middleware) are actually written with a [DEBUG] tag. They're noise by default —
# the Logs panel hides DEBUG unless you pick the "Debug" level filter — but being
# emitted means they're there when you need to troubleshoot (e.g. confirm the UI
# is polling, or see a fast poll's latency). App INFO lines are unaffected.
logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
)
# Third-party libraries are chatty at DEBUG (botocore/urllib3 wire logs, etc.).
# Keep them at WARNING so our DEBUG channel stays limited to OUR access lines.
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Override uvicorn's formatters to match our format
for _logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _uv_logger = logging.getLogger(_logger_name)
    _uv_logger.handlers = []  # Remove uvicorn's default handlers
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(LOG_FORMAT))
    _uv_logger.addHandler(_handler)
    _uv_logger.propagate = False

# Filter out noisy "Invalid HTTP request received" warnings (platform health probes)
class _InvalidHttpFilter(logging.Filter):
    def filter(self, record):
        return "Invalid HTTP request received" not in record.getMessage()

logging.getLogger("uvicorn.error").addFilter(_InvalidHttpFilter())

# Silence uvicorn's built-in access log ("... GET /metrics HTTP/1.1 200"). We
# replace it with our own request-timing middleware (below) that adds latency +
# context and drops the high-frequency pollers to DEBUG, so we don't want the
# duplicate line. Raising the level to WARNING suppresses the per-request INFO
# access lines without touching uvicorn's error channel.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Endpoints the UI polls (often several times/second) or hits repeatedly for
# read-only panel/autocomplete data. Their access lines are logged at DEBUG (i.e.
# hidden at the default INFO level) so they don't drown the genuinely useful lines
# (executions, reads/writes, checkpoints, errors). Still available if the level is
# lowered. All are reads with no side effects (GETs, plus the read-only POST
# /introspect) — safe to demote.
_HIGH_FREQUENCY_PATHS = frozenset({
    "/metrics",
    "/variables",
    "/data-catalog",
    "/helpers",
    "/files",
    "/packages",
    "/completions-catalog",
    "/introspect",
    "/health",
    "/checkpoint-timings",
})

# =============================================================================
# PRE-LOADED LIBRARIES (Hot-loaded into the MicroVM image snapshot)
# =============================================================================
# Everything imported here is captured in the Firecracker memory snapshot at
# image build time. When a MicroVM launches, these are already in memory —
# eliminating first-cell import latency.
#
# ADD NEW LIBRARIES HERE if they're:
#   - Large (>100ms import time)
#   - Commonly used in notebook cells
#   - Pre-installed in requirements.txt
#
# DO NOT add libraries here that are only used by the proxy or internal code.
# This section is ONLY for user-facing notebook performance.
# =============================================================================

# Data manipulation
import pandas
import pandas.io.parsers       # read_csv (lazy-loaded by default)
import pandas.io.excel         # read_excel (lazy-loaded by default)
import numpy
import numpy.random
import numpy.linalg

# Visualization
import matplotlib
matplotlib.use('Agg')          # Non-interactive backend (required before pyplot import)
import matplotlib.pyplot
import matplotlib.figure
import plotly
import plotly.express
import plotly.graph_objects

# Statistics. `import scipy` alone is ~free (submodules load lazily); scipy.stats is
# the heavy one (~520 modules, ~50-60 MB RSS). Cold on a 1-vCPU VM it cost 22-29s
# when a restore replayed `from scipy import stats` from the import ledger.
import scipy.stats

# Machine learning. A restore that replayed sklearn imports from the import ledger
# (unpickling a fitted Pipeline/ColumnTransformer/ensemble) measured 13.6-14.8 s of
# cold imports. Import-only: no threads, network or credentials. joblib comes in as
# a dependency; it binds after dill.extend(use_dill=False) above, so the portable
# (dill-free) pickle behaviour is kept.
import sklearn
import sklearn.ensemble
import sklearn.pipeline
import sklearn.compose
import sklearn.preprocessing
import sklearn.model_selection
import sklearn.metrics
import sklearn.linear_model

# SQL engine
import duckdb

# AWS SDK
import boto3
import boto3.session

def run_all_warmups():
    """Single entry point for ALL cold-start pre-warming, run once at module import
    so the cost is baked into the Firecracker memory snapshot instead of landing on
    the user's first cell. Each sub-warmup is independent and best-effort (any
    failure is non-fatal). The costs this eliminates, all confirmed via VM logs:

      1. DuckDB httpfs  — INSTALL/LOAD httpfs on the shared connection (~4.7s cold).
      2. boto3 clients  — first client()/resource() per service loads its botocore
                          model (1-3s per service). We warm the MODEL only, NOT creds.
      3. output render  — first DataFrame.to_html / matplotlib savefig / plotly
                          to_html / plotly.express chart call lazily import heavy
                          rendering machinery (0.5-3s; px first-call was ~3s on ARM).
      4. pip           — a helper process with pip pre-imported (~15s of cold
                          imports on the first pip run of a VM); see step 4.

    Keep ALL new pre-warm logic inside this function so there is one place to audit.

    ⚠️ CREDENTIAL HAZARD (this runs at IMAGE-BUILD time under the BUILD role):
    This function executes during image creation, while the app is being snapshotted
    — and at that point the process is running under the image's BUILD role, not the
    per-VM execution role. Anything that RESOLVES AWS credentials here caches them
    into the boto3 process-global DEFAULT_SESSION, which is then frozen into the
    Firecracker snapshot. Every VM launched from the image would restore that snapshot
    and reuse the stale BUILD-role creds for all runtime AWS access (S3/Glue/Athena/
    DynamoDB/checkpoints), authenticating as the wrong role. (This exact bug caused SQL
    cells to fail with "table does not exist" and checkpoint AccessDenied.)
    Rules to keep this safe:
      - Warm boto3 with a THROWAWAY explicit Session (below), never the default session,
        and do NOT make any call that resolves credentials (no get_caller_identity, no
        real API calls) — creating a client only loads the offline botocore model.
      - The /run and /resume hooks additionally reset boto3.DEFAULT_SESSION so the first
        real call always re-resolves creds under the live execution role. Belt and
        suspenders: source hygiene here + hard reset at hook time. See app/platform/
        hooks.py::_reset_boto3_credentials.
    """
    region = os.environ.get("AWS_REGION", "us-west-2")

    # 1. DuckDB shared connection + httpfs. A throwaway connection does NOT help —
    #    the query path (_run_duckdb_query) uses a specific module-level connection,
    #    so we must warm that exact one (warmup_duckdb does so).
    try:
        from app.notebook.sql_engine import warmup_duckdb
        warmup_duckdb()
    except Exception as e:
        logger.warning(f"warmup[duckdb]: skipped (non-fatal): {e}")

    # 2. boto3 client/resource MODELS only. Creating a client/resource loads the
    #    service's botocore model (the slow cold cost) WITHOUT making a network call
    #    or resolving credentials — so this warms the model without caching creds.
    #    We deliberately use a THROWAWAY local Session (and session.resource for the
    #    DynamoDB model) so we never populate boto3's process-global DEFAULT_SESSION;
    #    if we did, it would freeze build-role creds into the snapshot (see the
    #    CREDENTIAL HAZARD note in this function's docstring). Do NOT add any call
    #    here that resolves creds (get_caller_identity, list_*, etc.).
    try:
        session = boto3.Session()
        for svc in ("sts", "s3", "dynamodb", "athena", "glue", "secretsmanager"):
            try:
                session.client(svc, region_name=region)
            except Exception as e:
                logger.warning(f"warmup[boto3]: client('{svc}') failed (non-fatal): {e}")
        # resource() also loads the resource model used by the DynamoDB scan path —
        # via the SAME throwaway session, never boto3.resource() (which would touch
        # the global default session).
        try:
            session.resource("dynamodb", region_name=region)
        except Exception as e:
            logger.warning(f"warmup[boto3]: resource('dynamodb') failed (non-fatal): {e}")
    except Exception as e:
        logger.warning(f"warmup[boto3]: skipped (non-fatal): {e}")

    # 3. Output-render machinery (pandas HTML, matplotlib Agg, plotly go + express).
    try:
        # pandas HTML formatter — use a REALISTIC mixed-dtype, multi-column frame
        # with the SAME to_html kwargs the executor uses (classes/max_rows/max_cols),
        # and enough rows to trigger the truncation path. A tiny 1-col frame does NOT
        # warm the per-dtype column formatters (float/datetime/string) or the
        # max_rows/max_cols truncation machinery — observed render_dataframe≈1.2s on
        # the 0.5 vCPU tier for the first large table despite the old tiny warmup.
        try:
            import datetime as _dt
            _n = 60  # mixed dtypes so the per-column formatter warms all paths
            _wdf = pandas.DataFrame({
                "int_col": range(_n),
                "float_col": [i * 1.5 for i in range(_n)],
                "str_col": [f"item-{i}" for i in range(_n)],
                "date_col": [_dt.datetime(2024, 1, 1) + _dt.timedelta(days=i) for i in range(_n)],
                "bool_col": [i % 2 == 0 for i in range(_n)],
            })
            # DataFrame output now serializes via build_df_table_payload, which uses
            # pandas' per-column format_array + json.dumps. Warm that exact path so the
            # first real table render doesn't pay the format_array/json first-call cost.
            try:
                from app.notebook.executor import build_df_table_payload as _bdtp
                _bdtp(_wdf)
            except Exception as e:
                logger.warning(f"warmup[render]: df table payload failed (non-fatal): {e}")
        except Exception as e:
            logger.warning(f"warmup[render]: dataframe warmup failed (non-fatal): {e}")
        # matplotlib figure → PNG (savefig loads the Agg canvas machinery)
        try:
            import io as _io
            _fig = matplotlib.pyplot.figure()
            matplotlib.pyplot.plot([0, 1], [0, 1])
            _fig.savefig(_io.BytesIO(), format="png")
            matplotlib.pyplot.close(_fig)
        except Exception as e:
            logger.warning(f"warmup[render]: matplotlib savefig failed (non-fatal): {e}")
        # plotly graph_objects → HTML snippet (same call the executor uses)
        try:
            plotly.graph_objects.Figure().to_html(full_html=False, include_plotlyjs="cdn")
        except Exception as e:
            logger.warning(f"warmup[render]: plotly.to_html failed (non-fatal): {e}")
        # plotly.express FIRST-CALL warmup — px is a heavy layer on top of
        # graph_objects. The first px.<chart>() call lazily loads
        # plotly.express._chart_types, the dataframe-processing pipeline, and color
        # modules (observed ~3s on the ARM VM for the first px.area(); subsequent px
        # calls ~8ms). The graph_objects warmup above does NOT cover this path.
        # Exercise the common chart constructors; each can lazy-load independently.
        try:
            import plotly.express as _px
            _pxdf = pandas.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6], "c": ["a", "b", "a"]})
            _px.area(_pxdf, x="x", y="y", color="c")
            _px.box(_pxdf, x="c", y="y", color="c", points="all")
            _px.scatter(_pxdf, x="x", y="y", color="c", size="y")
            _px.line(_pxdf, x="x", y="y", color="c")
            _px.bar(_pxdf, x="c", y="y", color="c")
            del _pxdf
        except Exception as e:
            logger.warning(f"warmup[render]: plotly.express failed (non-fatal): {e}")
    except Exception as e:
        logger.warning(f"warmup[render]: skipped (non-fatal): {e}")

    # 4. pip helper process. A `python -m pip` subprocess imports ~650 modules from
    #    disk on every run; on a fresh or resumed VM the first read of each disk
    #    block is slow and the page cache does not survive the snapshot, so the
    #    first pip run paid ~15 s of imports on 0.5 vCPU (restore reinstalls and the
    #    first /install). This starts a SEPARATE interpreter (exec, not a fork of
    #    this process, so no boto3 / DEFAULT_SESSION state is shared) that only
    #    IMPORTS pip: no pip command, no network, every AWS_* variable removed from
    #    its environment, so nothing resolves credentials. It waits on a root-only
    #    Unix socket and is captured in the memory snapshot; each install forks from
    #    it (app/platform/pip_helper.py), and falls back to a plain subprocess when it
    #    is not there. Image builds only (the Dockerfile sets DOCKER_CONTAINER=1):
    #    importing app.server in tests or a local run starts no helper.
    #    NOTEBOOK_PIP_HELPER=0 in the build environment turns it off (~47 MB RSS).
    try:
        if os.environ.get("DOCKER_CONTAINER") == "1":
            from app.platform import pip_helper
            _info = pip_helper.start_at_build()
            if _info.get("error"):
                logger.warning(f"warmup[pip]: helper not ready (non-fatal, installs use a subprocess): {_info}")
            else:
                logger.info(f"warmup[pip]: helper {_info}")
    except Exception as e:
        logger.warning(f"warmup[pip]: skipped (non-fatal): {e}")

    # 5. package list. GET /packages serves a cached list keyed on a site-packages
    #    fingerprint (app/notebook/package_list.py). Building it here puts the
    #    image's list in the snapshot, so a restore with no user packages hits it.
    #    Filesystem reads only: no network, no credentials.
    try:
        from app.notebook import package_list
        package_list.warm()
    except Exception as e:
        logger.warning(f"warmup[packages]: skipped (non-fatal): {e}")


# Run all cold-start pre-warming once at import (baked into the memory snapshot).
run_all_warmups()

# --- Persistent State ---
# These survive across requests AND across suspend/resume.
executor = SandboxExecutor()

session_state = {
    "microvm_id": None,
    "session_id": None,
    "notebook_id": None,
    "notebook_name": None,
    "persistence_mode": None,
    "started_at": None,
    "request_count": 0,
    "suspend_count": 0,
    "resume_count": 0,
    "checkpoint_enabled": False,
    "artifacts_bucket": None,
}

# --- FastAPI App ---
app = FastAPI(
    title="Agent Code Sandbox",
    description="Isolated code execution environment for AI agents",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request-timing access log ---
# Replaces uvicorn's default access line (which has no latency) with one that
# includes the response time and, where available, the session/notebook this VM
# is serving — so a multi-VM CloudWatch stream is attributable and slow endpoints
# are visible. High-frequency poller paths (/metrics, /variables, /data-catalog)
# log at DEBUG so they don't drown the useful lines at the default INFO level.
import time as _time

@app.middleware("http")
async def _access_log(request, call_next):
    start = _time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled error still gets a timed line before the error handler runs.
        elapsed_ms = (_time.perf_counter() - start) * 1000
        logger.warning(f"← {request.method} {request.url.path} 500 {elapsed_ms:.0f}ms (unhandled)")
        raise
    elapsed_ms = (_time.perf_counter() - start) * 1000
    path = request.url.path
    # Session context (best-effort) for attribution across VM log streams.
    sess = session_state.get("session_id")
    nb = session_state.get("notebook_name")
    ctx = ""
    if nb:
        ctx += f" nb={nb}"
    if sess:
        ctx += f" sess={str(sess)[:8]}"
    line = f"← {request.method} {path} {response.status_code} {elapsed_ms:.0f}ms{ctx}"
    # High-frequency poll/read endpoints are demoted to DEBUG to keep the log
    # readable — BUT only when they're fast AND successful. A slow (>1s) or
    # non-2xx response on those paths is exactly the signal we DON'T want to hide
    # (e.g. a 15s /packages, or a failing /variables), so it stays at INFO/WARNING.
    _quiet = (
        path in _HIGH_FREQUENCY_PATHS
        and elapsed_ms < 1000
        and 200 <= response.status_code < 400
    )
    if _quiet:
        logger.debug(line)
    elif response.status_code >= 500:
        logger.warning(line)
    else:
        logger.info(line)
    return response

# Expose shared state via app.state for route modules
app.state.executor = executor
app.state.session_state = session_state

# Data catalog (background schema discovery — populated by /run hook)
from app.notebook.data_catalog import DataCatalog
app.state.data_catalog = DataCatalog()

# Checkpoint manager (class-based — holds refs to executor + session_state + data_catalog)
# Notebook DAG document pushed by the proxy (VM GET/PUT /dag) and saved with each
# checkpoint as dag.json (thread-safe; one per VM process).
from app.notebook.dag_store import DagStore
app.state.dag_store = DagStore()

from app.platform.checkpoint import CheckpointManager
app.state.checkpoint_manager = CheckpointManager(
    executor, session_state, data_catalog=app.state.data_catalog,
    dag_store=app.state.dag_store,
)

# Persist the data catalog to S3 the moment background discovery finishes, rather
# than waiting for the (heavy, interruptible) suspend checkpoint — a mid-suspend
# freeze could otherwise lose it and force slow re-discovery on the restored VM.
app.state.data_catalog._on_complete_callback = app.state.checkpoint_manager.flush_data_catalog

# Wire the executor + checkpoint manager into the install_package helper so a cell
# can install packages via the SAME path as the Packages side-panel (pip install +
# record + flush, so it survives VM suspend/restore).
from app.notebook.helpers.packages import _register_runtime as _register_pkg_runtime
_register_pkg_runtime(executor, app.state.checkpoint_manager)

# --- Register route modules ---
from app.platform.hooks import router as hooks_router, proxy_router as hooks_proxy_router
from app.notebook.routes import router as routes_router
from app.notebook.code_engine import router as code_router
from app.notebook.sql_engine import router as sql_router
from app.notebook.dag_store import router as dag_router
from app.notebook.vm_status import router as vm_status_router

app.include_router(hooks_router)
app.include_router(hooks_proxy_router)
app.include_router(routes_router)
app.include_router(code_router)
app.include_router(sql_router)
app.include_router(dag_router)
app.include_router(vm_status_router)

# --- Centralized error handling + metrics ---
# App-level catch-all: any exception escaping any route is mapped to a clean HTTP
# status, logged with a traceback, and recorded as a structured metric line.
from app.platform.errors import install_error_handling
install_error_handling(app, namespace="MicroVMNotebookSandbox")
