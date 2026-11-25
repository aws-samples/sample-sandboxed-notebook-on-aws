"""
Sandboxed Python Code Executor

Part of: app.notebook (application layer)

Maintains a persistent namespace across executions — variables, functions,
imports all survive between calls. This is the core of the agent sandbox:
AI-generated code runs here, accumulating state like a notebook kernel.
"""

import io
import os
import re
import ast
import sys
import json
import time
import copy
import ctypes
import logging
import threading
import traceback
import platform
import signal
import subprocess
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Static def-use analysis (dependency-free, shared with the proxy's DAG builder,
# proxy/notebook/dag.py). SandboxExecutor exposes these as _assigned_targets /
# _referenced_names so runtime and open-time dependency graphs use the SAME logic.
from .static_analysis import assigned_targets, rebound_names, referenced_names
from . import read_capture
from . import http_capture
from .dag_contract import cell_code_hash
from .pip_diagnostics import describe_pip_failure, normalize_arch
from .scratch import ScratchScopes, valid_scope

# pip install wall-clock limit. The proxy forwarder, the UI fetch and the AI
# tools wait longer than this (180s) so the classified message gets back.
PIP_INSTALL_TIMEOUT_S = 120
# How long an install waits for another pip run on this VM (a restore, a second
# install) to finish before it gives up. Wait + pip timeout + the 10 s kill grace
# stays under the 180 s the proxy forwarder and the UI wait.
PIP_LOCK_WAIT_S = 40


class RuntimeSectionBusy(Exception):
    """runtime_section(lock_timeout=...) could not take _exec_lock in time (a cell
    is holding it). Callers with a deadline (VM GET /dag, checkpoint save) map it to
    503 busy / an unlocked best-effort snapshot."""

# VM runtime knobs injected by the proxy at launch (config.sh → runHookPayload
# env_vars → /run hook → os.environ). These MUST be read at CALL time, not module
# import: this module is imported into the Firecracker snapshot at image-build time,
# which is BEFORE the /run hook injects the launch env vars. Reading at import would
# freeze the build-time defaults (the old EXECUTION_TIMEOUT_SECONDS bug).
DEFAULT_MAX_DISPLAY_ROWS = 50
# Per-cell caps on the ordered `outputs` list (tables, Plotly charts, images):
# at most this many rich items and about this many serialized bytes. Beyond that
# the list ends with a "… N more outputs not shown" text item (never silent).
MAX_RICH_OUTPUTS = 20
MAX_OUTPUT_BYTES = 5_000_000
# display()/show() calls remember at most this many object references per run
# (the rest are only counted), so a runaway loop cannot pin unbounded memory.
MAX_DISPLAY_REFS = 200


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Timeout budget and error texts live in run_limits (shared with run_guard and the
# helpers without importing this module); re-exported here under the old names.
from .run_limits import (  # noqa: E402,F401
    DEFAULT_EXECUTION_TIMEOUT_SECONDS, TIMEOUT_ERROR, INTERRUPT_ERROR,
    execution_timeout_seconds)
from .run_guard import _PythonRunGuard  # noqa: E402


def max_display_rows() -> int:
    """Max DataFrame/SQL rows to serialize to the UI (the client paginates these)."""
    return _env_int("NOTEBOOK_MAX_DISPLAY_ROWS", DEFAULT_MAX_DISPLAY_ROWS)


# Sentinel key marking the `html` field as a structured DataFrame table payload
# (JSON) rather than raw HTML. The frontend detects this and renders its paginated
# grid; per-cell formatting (links/numbers/NaN/bools) is applied client-side.
DF_TABLE_SENTINEL = "__df_table__"


def build_df_table_payload(df) -> str:
    """Serialize a pandas DataFrame to a compact JSON table payload for the client.

    Sends per-cell STRINGS formatted exactly as pandas' to_html would render them
    (via the same column formatter), so the frontend's ported cell-enhancer
    (links, number formatting, NaN/bool badges, truncation) reproduces the old
    look 1:1. Caps rows at NOTEBOOK_MAX_DISPLAY_ROWS and columns at 20; reports the
    true totals so the UI can show "showing X of Y" notes.

    Returns a JSON string: {"__df_table__": true, "columns": [...], "cells": [[...]],
    "dtypes": [...], "total_rows": int, "shown_rows": int, "total_cols": int,
    "shown_cols": int}
    """
    import pandas as pd
    from pandas.io.formats.format import format_array

    MAX_ROWS = max_display_rows()
    MAX_COLS = 20

    total_rows = len(df)
    total_cols = df.shape[1] if hasattr(df, "shape") and len(df.shape) > 1 else 1

    view = df.head(MAX_ROWS) if total_rows > MAX_ROWS else df
    if total_cols > MAX_COLS:
        view = view.iloc[:, :MAX_COLS]

    columns = [str(c) for c in view.columns]

    # Access columns BY POSITION (iloc), not by label: duplicate column names make
    # view[label] return a DataFrame (not a Series), which breaks .dtype / _values.
    # Positional access always yields a Series, so dup-named frames serialize fine.
    dtypes = []
    col_strings = []
    # format_array honors pandas' display.max_colwidth (default 50), which would
    # truncate long cell strings to "…" AT THE SOURCE — before the payload is even
    # built — so the frontend could never show or reveal the full text (widening a
    # column did nothing). Serialize with max_colwidth disabled so the client
    # receives FULL cell values; the DataGrid then controls visible width via
    # column resize + wrapping.
    with pd.option_context("display.max_colwidth", None):
        for j in range(len(columns)):
            col = view.iloc[:, j]
            dtypes.append(str(col.dtype))
            try:
                vals = format_array(col._values, None)
                col_strings.append([v.strip() for v in vals])
            except Exception:
                col_strings.append([("" if pd.isna(v) else str(v)) for v in col.tolist()])

    n = len(view)
    cells = [[col_strings[j][i] for j in range(len(columns))] for i in range(n)]

    return json.dumps({
        DF_TABLE_SENTINEL: True,
        "columns": columns,
        "dtypes": dtypes,
        "cells": cells,
        "total_rows": int(total_rows),
        "shown_rows": int(n),
        "total_cols": int(total_cols),
        "shown_cols": len(columns),
    })


# ── Error traceback helpers ────────────────────────────────────────────────
# Cells are compiled with the filename "<sandbox>", so user-code frames are exactly
# those whose filename is "<sandbox>". Everything else (pandas, numpy, our helpers'
# library internals) is library noise we drop from the surfaced traceback.
_CELL_FILENAME = "<sandbox>"
_MAX_LOCAL_REPR = 300          # cap each local's repr so a huge DataFrame can't bloat the payload
_MAX_LOCALS = 40               # cap how many locals we serialize from the failing frame
_MAX_TB_CHARS = 8000           # hard cap on the cleaned traceback text


def _safe_repr(value, limit: int = _MAX_LOCAL_REPR) -> str:
    """repr() a value defensively: never raise (a __repr__ can throw), and truncate
    so a giant object can't bloat the payload. Used for failing-frame locals."""
    try:
        r = repr(value)
    except Exception:
        try:
            r = f"<unreprable {type(value).__name__}>"
        except Exception:
            return "<unreprable>"
    if len(r) > limit:
        r = r[:limit] + f"… (+{len(r) - limit} chars)"
    return r


def _build_error_debug(exc: BaseException) -> tuple[list, dict, str]:
    """From a caught exception, build the debugging triple:
      • error_frames: [{lineno, name, line}] for CELL frames only (filename ==
        "<sandbox>"), in call order (deepest last) — the cell lines involved.
      • error_locals: {name: safe_repr} of the DEEPEST frame that is in the cell,
        or the deepest frame overall if the error surfaced entirely in a library.
      • traceback text: the standard formatted traceback with pure-library frames
        pruned (cell + our-helper frames kept), capped in length.
    All best-effort — any failure returns empty pieces rather than raising."""
    error_frames: list = []
    error_locals: dict = {}
    tb_text = ""
    try:
        # Structured cell-local frames (lineno maps 1:1 to the editor).
        summary = traceback.extract_tb(exc.__traceback__)
        for f in summary:
            if f.filename == _CELL_FILENAME:
                error_frames.append({"lineno": f.lineno, "name": f.name, "line": (f.line or "").strip()})
    except Exception:
        error_frames = []

    try:
        # Walk the raw frame objects to grab locals of the deepest CELL frame (the
        # crash point in the user's own code). Fall back to the very deepest frame
        # when the error never entered cell code (e.g. deep inside a library call).
        tb = exc.__traceback__
        deepest = None
        deepest_cell = None
        while tb is not None:
            deepest = tb
            if tb.tb_frame.f_code.co_filename == _CELL_FILENAME:
                deepest_cell = tb
            tb = tb.tb_next
        target = deepest_cell or deepest
        if target is not None:
            flocals = target.tb_frame.f_locals
            for i, (k, v) in enumerate(flocals.items()):
                if i >= _MAX_LOCALS:
                    break
                if k.startswith("__"):
                    continue  # skip dunders / import machinery noise
                error_locals[k] = _safe_repr(v)
    except Exception:
        error_locals = {}

    try:
        # Cleaned traceback text: format the full chain, then drop frames that are
        # neither cell code nor our own helpers (keep the signal, cut library depth).
        full = traceback.format_exception(type(exc), exc, exc.__traceback__)
        kept = []
        for chunk in full:
            is_frame = chunk.startswith("  File ")
            if not is_frame:
                kept.append(chunk)  # header / "Traceback" / exception line
                continue
            # Keep a frame chunk if it's cell code or one of our helper modules.
            if (f'File "{_CELL_FILENAME}"' in chunk
                    or "/app/notebook/helpers" in chunk
                    or "app.notebook.helpers" in chunk):
                kept.append(chunk)
        tb_text = "".join(kept).strip()
        # If pruning removed everything but the header (error surfaced entirely in a
        # library), fall back to the full traceback so the user isn't left with nothing.
        if tb_text.count("File ") == 0:
            tb_text = "".join(full).strip()
        if len(tb_text) > _MAX_TB_CHARS:
            tb_text = tb_text[:_MAX_TB_CHARS] + "\n… (traceback truncated)"
    except Exception:
        tb_text = ""

    return error_frames, error_locals, tb_text


@dataclass
class ExecutionResult:
    """Result of a code execution."""
    success: bool
    output: str = ""
    error: str = ""
    error_lineno: int | None = None  # 1-indexed line in the cell where the error occurred (maps directly to the editor since code is compiled unwrapped)
    # Full debugging context for a runtime error (all empty/None on success):
    #   traceback     — cleaned multi-frame traceback text (cell + helper frames,
    #                   library-internal frames dropped), for the collapsible view.
    #   error_frames  — structured cell-local frames [{lineno, name, line}] deepest
    #                   -last, so the editor can mark every cell line in the chain.
    #   error_locals  — safe repr of the FAILING frame's local variables {name: repr}
    #                   (truncated), so AI Fix can see the values at the crash point.
    traceback: str = ""
    error_frames: list = field(default_factory=list)
    error_locals: dict = field(default_factory=dict)
    variables_created: list[str] = field(default_factory=list)
    variables_read: list[str] = field(default_factory=list)  # free vars this cell consumes (def-use / reactive graph)
    variables_mutated: list[str] = field(default_factory=list)  # pre-existing vars changed in place (also in variables_created)
    # Pre-existing user vars this cell REBOUND at the top level (`x = ...`, and
    # AugAssign `x += 1`). A rebind is a definition; the proxy DAG builder maps
    # AugAssign targets back to mutates with the static facts (design §3.2).
    variables_rebound: list[str] = field(default_factory=list)
    execution_time_ms: float = 0.0
    # Peak resource use sampled DURING this cell's exec() (guest side). peak_mem_mb
    # is the executor process RSS high-water mark (this kernel's footprint, not the
    # whole VM); peak_cpu_pct is best-effort (short cells under ~100ms may read low
    # since CPU% needs a sampling interval). Both 0.0 if psutil is unavailable.
    peak_mem_mb: float = 0.0
    peak_cpu_pct: float = 0.0
    html: str = ""      # HTML table output (for DataFrames)
    image: str = ""     # Base64 PNG image (for matplotlib plots): the LAST figure's
    # Ordered rich outputs: [{"t":"text","text"}, {"t":"table","payload","label"?},
    # {"t":"plotly","html","label"?}, {"t":"image","src"}]. [] unless it holds at least
    # one rich item. `output`/`html`/`image` above stay filled for older web builds.
    outputs: list = field(default_factory=list)
    installed_spec: str = ""  # For install_package: resolved "name==version" pin
    # install_package failures only (see pip_diagnostics): the failure kind
    # (e.g. "source_build_timeout"), pip's captured output tail, and the VM arch.
    install_error_kind: str = ""
    install_details: str = ""
    install_arch: str = ""
    # Data sources the cell's reader helpers touched, as [{kind, id}] IDENTIFIERS
    # only (table names, s3:// URIs, paths, URLs without query) — never SQL text or
    # data. Recorded by helpers._common._log_read via read_capture (lineage drawer).
    sources_read: list = field(default_factory=list)
    # Runtime section {clock, vars, runs} captured under _exec_lock at the end of
    # the run, only when execute(..., capture_runtime=True) (the /execute route).
    runtime: dict | None = None


class _ResourceSampler:
    """Background peak-CPU/memory sampler for a single cell's execution.

    Runs a daemon thread that polls this process's RSS + CPU% every ~100ms while a
    cell executes, keeping the high-water mark of each. Cheap and self-contained: if
    psutil is missing or any poll raises, it degrades to zeros rather than failing
    the cell. Memory is the executor PROCESS RSS (the notebook kernel's footprint),
    so it attributes memory to the code that ran — not the whole VM.

    Usage:
        s = _ResourceSampler(); s.start()
        ... run cell ...
        s.stop()  # peak_mem_mb / peak_cpu_pct now populated
    """

    _INTERVAL_S = 0.1  # poll cadence — fine enough for sub-second cells, cheap enough

    def __init__(self):
        self.peak_mem_mb = 0.0
        self.peak_cpu_pct = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._psutil = None
        try:
            import psutil
            self._psutil = psutil
            self._proc = psutil.Process()
            # CPU: use SYSTEM-WIDE cpu_percent (same source as the VM side panel's
            # /metrics route), so per-cell CPU is on the same 0–100% scale the panel
            # shows — NOT the per-process cpu_percent (which reports 0–N×100% across
            # cores and misattributes bursts to short cells). Prime it (first call
            # always returns 0.0 as it measures since the previous call).
            psutil.cpu_percent(interval=None)
            # Seed the memory peak with the pre-exec baseline so even a cell too fast
            # to be sampled reports the process's current RSS rather than 0.
            self.peak_mem_mb = self._proc.memory_info().rss / (1024 * 1024)
        except Exception:
            self._proc = None  # psutil missing / unavailable → sampler is a no-op

    def _run(self):
        while not self._stop.is_set():
            try:
                rss_mb = self._proc.memory_info().rss / (1024 * 1024)
                if rss_mb > self.peak_mem_mb:
                    self.peak_mem_mb = rss_mb
                # System-wide CPU % (0–100), matching the VM panel.
                cpu = self._psutil.cpu_percent(interval=None)
                if cpu > self.peak_cpu_pct:
                    self.peak_cpu_pct = cpu
            except Exception:
                pass  # transient read error — keep sampling
            self._stop.wait(self._INTERVAL_S)

    def start(self):
        if self._proc is None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._proc is None:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        # One final read so a cell that finished between polls still captures its
        # end-state RSS (peak allocation often lands right before return).
        try:
            rss_mb = self._proc.memory_info().rss / (1024 * 1024)
            if rss_mb > self.peak_mem_mb:
                self.peak_mem_mb = rss_mb
            cpu = self._psutil.cpu_percent(interval=None)
            if cpu > self.peak_cpu_pct:
                self.peak_cpu_pct = cpu
        except Exception:
            pass


class SandboxExecutor:
    """
    Executes Python code in a persistent namespace.

    Key properties:
    - Variables persist across executions (stateful, like a Jupyter kernel)
    - stdout/stderr are captured and returned
    - Exceptions are caught and reported without crashing the sandbox
    - The namespace can be introspected (list variables, get types/values)
    """

    def __init__(self):
        self._namespace: dict[str, Any] = {}
        self._execution_count: int = 0
        self._history: list[dict] = []
        # Variable provenance: name → {defined_by, defined_at, modified_by[], last_cell, last_exec}.
        # cell_* fields hold the frontend cell id; *_at/_exec use a monotonic _prov_clock
        # that advances on every code execute AND SQL write, so recency is unambiguous
        # even when cells are re-run out of order.
        # Only user variables are recorded (see _is_provenance_noise): no dunders,
        # modules, or sandbox-injected helpers/display still bound to the injected object.
        self._var_provenance: dict[str, dict] = {}
        self._prov_clock: int = 0
        # The display() shim of the latest execute (rebound per run); lets the
        # provenance filter recognise it outside execute().
        self._display_fn = None
        # Import ledger: binding-name → exact import source line that produced it.
        # Modules can't be pickled, so checkpoint save() drops them; on restore we
        # replay these lines to re-establish `import pandas as pd` etc. Keyed by the
        # bound name so re-running an import is idempotent (overwrites, no dupes).
        self._imports: dict[str, str] = {}
        self._created_at = datetime.now(timezone.utc)
        self._exec_thread_id: int | None = None
        # The running Python cell's interrupt/timeout guard (run_guard._PythonRunGuard),
        # set for the duration of one execute() run, else None. interrupt() tries
        # it first.
        self._py_run = None
        # The running SQL cell's interrupt/timeout guard (run_guard._SqlRunGuard),
        # set by the SQL engine for the duration of one SQL cell, else None.
        # interrupt() falls through to it when no Python cell is running.
        self._sql_run = None
        # Serializes execute() end-to-end. execute() swaps the PROCESS-GLOBAL
        # sys.stdout/sys.stderr to StringIO and restores them in a finally, so two
        # overlapping execute() calls would stomp each other's redirection: the one
        # that finishes first restores sys.stdout while the other is still mid-exec(),
        # leaking the other cell's remaining prints to the real fd (→ the VM log /
        # CloudWatch). The FastAPI /execute + /execute-batch handlers share an
        # asyncio.Lock, but other callers do NOT (the /validate hook, checkpoint
        # import-replay), so relying on each handler to grab it left gaps. Making
        # mutual exclusion a property of the executor itself closes ALL of them at
        # once. RLock (reentrant) so a nested execute() on the same thread can't
        # self-deadlock. execute() runs via asyncio.to_thread, so a threading
        # primitive is the correct choice.
        #
        # The same lock guards the runtime state (_var_provenance, _prov_clock,
        # _runs): every writer and reader holds it, except the documented
        # best-effort runtime_section_unlocked() used by checkpoint save. Because
        # those off-lock callers can hold it for a whole cell, runtime_section()
        # is only ever called from a worker thread, never on the event loop.
        self._exec_lock = threading.RLock()
        # Run ledger: cell_id → {code_hash, clock, ok} of the LAST run of that cell
        # (code and SQL). Lets the proxy tell which code produced the live values.
        self._runs: dict[str, dict] = {}
        # Agent exploration scratch: scope → dict of names its probes made (see
        # app/notebook/scratch.py). Accessed only under _exec_lock. Never part of
        # _namespace, the provenance or the checkpoint.
        self._scratch = ScratchScopes()
        # Pre-import heavy packages to reduce first-cell execution time
        self._preload_packages()
        # Lineage: record raw outbound HTTP (requests/httpx/urllib) made from
        # cells as "url" sources, grouped by host. Idempotent; never raises.
        try:
            http_capture.install()
        except Exception:
            pass
        # Inject helper functions into the namespace (available in every cell)
        self._inject_helpers()
        # Set default Plotly dark theme (matches the notebook UI)
        self._set_plotly_defaults()

    def _preload_packages(self):
        """Pre-import heavy packages into the interpreter cache (not the namespace).
        This makes the first user cell that imports these packages much faster."""
        import importlib
        for pkg in ['pandas', 'numpy', 'matplotlib', 'matplotlib.pyplot', 'boto3', 'requests']:
            try:
                importlib.import_module(pkg)
            except ImportError:
                pass

    def _inject_helpers(self):
        """Inject helper functions into the namespace so they're available in every cell."""
        try:
            from app.notebook import helpers
            # Add all public functions from helpers to the namespace
            for name in dir(helpers):
                if not name.startswith('_'):
                    obj = getattr(helpers, name)
                    if callable(obj):
                        self._namespace[name] = obj
        except ImportError:
            pass  # Helpers not available (shouldn't happen in normal operation)

    @staticmethod
    def _helper_names() -> list[str]:
        """Public helper names injected into every cell's namespace by
        _inject_helpers (read_s3_csv, read_athena, plot_bar, …). Computed from the
        SAME source (dir(helpers) + public + callable) so the lint allow-list can
        never drift from what's actually injected. Used to tell ruff these names are
        defined (via --builtins) so calling a helper isn't a false F821 'undefined
        name'. Returns [] if the helpers module can't be imported."""
        try:
            from app.notebook import helpers
        except ImportError:
            return []
        names = []
        for name in dir(helpers):
            if name.startswith("_"):
                continue
            try:
                if callable(getattr(helpers, name)):
                    names.append(name)
            except Exception:
                continue
        return names

    @staticmethod
    def _injected_helpers() -> dict:
        """{name: object} for the helpers _inject_helpers puts in the namespace
        (same selection: public + callable). {} if helpers can't be imported."""
        try:
            from app.notebook import helpers
        except ImportError:
            return {}
        out = {}
        for name in dir(helpers):
            if name.startswith("_"):
                continue
            try:
                obj = getattr(helpers, name)
            except Exception:
                continue
            if callable(obj):
                out[name] = obj
        return out

    def _user_var_names(self, names, injected: dict, ns: dict | None = None) -> list[str]:
        """Filter `names` down to genuine user variables for a cell's def-use
        lists (variables_read / variables_created → varsRead / varsWritten).

        Dropped: dunders (`__builtins__`, which exec() adds), module objects
        (`import pandas as pd`, `from os import path` — modules are not data
        dependencies, so dropping them from BOTH reads and writes keeps the
        reactive graph consistent), and sandbox-injected names (`display`, the
        read_* / plot_* helpers) still bound to the injected object. A cell that
        rebinds an injected name (`display = 5`, `def read_athena(...)`) keeps it.
        User functions, classes and DataFrames are kept.

        ns: the namespace the run executed in (a scratch overlay for agent probes);
        defaults to the user namespace."""
        if ns is None:
            ns = self._namespace
        out = []
        for name in names:
            if name.startswith("__") and name.endswith("__"):
                continue
            if name not in ns:
                continue
            val = ns[name]
            if isinstance(val, types.ModuleType):
                continue
            if name in injected and val is injected[name]:
                continue
            out.append(name)
        return out

    def _namespace_names(self) -> list[str]:
        """Public names currently DEFINED in the live kernel namespace — variables,
        functions, classes, and imported modules the user created in prior runs, plus
        anything restored from a checkpoint or injected from the UI.

        Fed to ruff as --builtins so a reference to a name that exists in the running
        kernel (but isn't defined in the current cell text) isn't a false F821. This
        aligns lint with the notebook's real truth — the live kernel — instead of the
        static cell text. Trade-off: a typo that happens to collide with a lingering
        binding won't be flagged; for a stateful notebook that's the right call.

        We include callables/modules here (unlike get_variables, which hides them
        from the Variables panel) because a cell can legitimately REFERENCE a
        function/module defined earlier. Dunders and the `_`-prefixed internals
        (display shim, provenance scratch) are skipped — they're not user names, and
        a valid identifier check keeps the --builtins list clean."""
        out = []
        try:
            for name in list(self._namespace.keys()):
                if name.startswith("_"):
                    continue  # dunders + internal scratch (display is added separately)
                if name.isidentifier():
                    out.append(name)
        except Exception:
            return []
        return out

    def _set_plotly_defaults(self):
        """Set Plotly default template to dark theme (matches the notebook UI).
        Users can override with: pio.templates.default = 'plotly' (for light)."""
        try:
            import plotly.io as pio
            pio.templates.default = 'plotly_dark'
        except ImportError:
            pass

    # ---- Variable provenance (which cell created / modified each variable) ----

    @staticmethod
    def _var_fingerprint(obj) -> tuple:
        """Cheap change-signature: object identity + shape/len. id() catches
        reassignment (new object); shape/len catches in-place ops that change size."""
        try:
            shape = getattr(obj, "shape", None)
            if shape is not None:
                sig = tuple(shape)
            elif hasattr(obj, "__len__"):
                sig = len(obj)
            else:
                sig = None
        except Exception:
            sig = None
        return (id(obj), sig)

    # Def-use static analysis lives in app.notebook.static_analysis (dependency-free,
    # shared with the proxy's DAG builder, proxy/notebook/dag.py) so runtime and
    # open-time graphs are computed by the SAME code. These thin wrappers keep the
    # existing call sites (self._assigned_targets / self._referenced_names) intact.
    _assigned_targets = staticmethod(assigned_targets)
    _rebound_names = staticmethod(rebound_names)
    _referenced_names = staticmethod(referenced_names)

    @staticmethod
    def _extract_imports(code: str) -> dict[str, str]:
        """Map each import binding introduced by `code` to a re-runnable import line.

        Returns {binding_name: source_line}. Covers:
          import x                → {"x": "import x"}
          import x.y              → {"x": "import x.y"}   (binds top package name `x`)
          import x as z           → {"z": "import x as z"}
          from x import a, b as c → {"a": "from x import a", "c": "from x import b as c"}
          from x import *         → {"x:*": "from x import *"}  (starred: no single name)

        Keying by the bound name makes replay idempotent: re-importing overwrites the
        same key rather than accumulating duplicate lines.
        """
        out: dict[str, str] = {}
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        out[alias.asname] = f"import {alias.name} as {alias.asname}"
                    else:
                        # `import x.y.z` binds the top-level package name `x`.
                        top = alias.name.split(".")[0]
                        out[top] = f"import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                # Preserve relative imports' dots and the module (may be None for `from . import x`).
                mod = ("." * (node.level or 0)) + (node.module or "")
                for alias in node.names:
                    if alias.name == "*":
                        out[f"{mod}:*"] = f"from {mod} import *"
                    elif alias.asname:
                        out[alias.asname] = f"from {mod} import {alias.name} as {alias.asname}"
                    else:
                        out[alias.name] = f"from {mod} import {alias.name}"
        return out

    def get_imports(self) -> dict[str, str]:
        """Import ledger (for checkpoint save)."""
        return self._imports

    def set_imports(self, data: dict):
        """Restore the import ledger (from checkpoint) so future checkpoints keep it."""
        if isinstance(data, dict):
            self._imports.update({k: v for k, v in data.items() if isinstance(v, str)})

    @staticmethod
    def _new_prov(cid: str, clock: int) -> dict:
        """A fresh provenance entry. A write with no cell id carries an explicit
        `writer` marker of its real origin (var_inspect.classify_origin): agent and
        internal runs execute in a scratch overlay and never reach provenance, so a
        no-cell write here always comes from the UI or an API call ("user")."""
        p = {"defined_by": cid, "defined_at": clock,
             "modified_by": [], "last_cell": cid, "last_exec": clock}
        if cid == "":
            p["writer"] = "user"
        return p

    def _update_provenance(self, cell_id, created, modified, deleted, redefined=None):
        """Record which cell created/modified/deleted variables in this execution.

        redefined: names this cell REBOUND at the top level (a real `x = ...`) that
        already existed. These are treated as fresh definitions — their `defined_by`
        is re-stamped to this cell — so re-running (or RunAll after a restore) repairs
        a stale `defined_by` and the Variables panel can show "Defined in" again."""
        redefined = redefined or set()
        self._prov_clock += 1
        clock = self._prov_clock
        cid = cell_id or ""
        for name in deleted:
            self._var_provenance.pop(name, None)
        for name in created:
            self._var_provenance[name] = self._new_prov(cid, clock)
        for name in modified:
            if name in created:
                continue
            p = self._var_provenance.get(name)
            if p is None:
                # Existed but untracked (e.g. restored from checkpoint) — treat this
                # touch as its definition point so it still gets a jump target.
                self._var_provenance[name] = self._new_prov(cid, clock)
            else:
                if name in redefined:
                    # A genuine rebind — this cell (re)defines the variable, so it
                    # owns the definition now. Repairs a stale/unresolvable
                    # defined_by carried over from a prior run or session restore.
                    p["defined_by"] = cid
                    p["defined_at"] = clock
                if not p["modified_by"] or p["modified_by"][-1] != cid:
                    p["modified_by"].append(cid)
                p["last_cell"] = cid
                p["last_exec"] = clock

    def record_var_write(self, name: str, cell_id):
        """Record a variable write that didn't go through execute() — e.g. the SQL
        engine storing its result DataFrame directly into the namespace."""
        if not name or (name.startswith("__") and name.endswith("__")):
            return
        # Called from worker threads (SQL engine) that do not hold the asyncio
        # execute lock — the runtime state is guarded by _exec_lock (re-entrant).
        with self._exec_lock:
            self._prov_clock += 1
            clock = self._prov_clock
            cid = cell_id or ""
            p = self._var_provenance.get(name)
            if p is None:
                self._var_provenance[name] = self._new_prov(cid, clock)
            else:
                if not p["modified_by"] or p["modified_by"][-1] != cid:
                    p["modified_by"].append(cid)
                p["last_cell"] = cid
                p["last_exec"] = clock

    def set_variable(self, name: str, value, cell_id=None) -> None:
        """Inject a value directly into the live namespace WITHOUT running user code —
        e.g. a variable pushed in from the UI's "inject variable" action. Mirrors what
        exec() would do for `name = value`, then stamps provenance via the same seam
        the SQL engine uses so the injected var shows the correct origin in the
        Variables panel and participates in reactive dataflow. Names starting with
        `_` are namespace-internal and get_variables() hides them, so we reject them
        here rather than silently create an invisible var."""
        if not name or not name.isidentifier():
            raise ValueError(f"Invalid variable name: {name!r}")
        if name.startswith("_"):
            raise ValueError("Variable names starting with '_' are reserved")
        with self._exec_lock:
            self._namespace[name] = value
            self.record_var_write(name, cell_id)

    def _is_provenance_noise(self, name, injected: dict) -> bool:
        """True for a provenance name that is not a user variable: a dunder
        (`__builtins__`), a name bound to a module, an injected helper / the
        display() shim still bound to the injected object, or — when the name is
        not bound at all — `display` or an import-ledger binding (an old
        checkpoint's module entry before/without its import replay)."""
        if not isinstance(name, str) or (name.startswith("__") and name.endswith("__")):
            return True
        ns = self._namespace
        if name in ns:
            val = ns[name]
            if isinstance(val, types.ModuleType):
                return True
            if name in injected and val is injected[name]:
                return True
            return name == "display" and val is self._display_fn
        return name == "display" or name in self._imports

    def _user_provenance(self, prov: dict) -> dict:
        """`prov` without the noise entries (see _is_provenance_noise). Tolerates
        provenance restored from old checkpoints that still holds such names."""
        injected = self._injected_helpers()
        return {k: v for k, v in list(prov.items()) if not self._is_provenance_noise(k, injected)}

    def get_var_provenance(self) -> dict:
        """Provenance map of user variables (for checkpoint save fallback paths)."""
        return self._user_provenance(self._var_provenance)

    def set_var_provenance(self, data: dict):
        """Restore provenance map (from checkpoint) and advance the clock past it.
        Used by the provenance.json restore fallback (no run ledger). Noise
        entries an old checkpoint may hold (dunders, modules, helpers) are dropped."""
        if not isinstance(data, dict):
            return
        with self._exec_lock:
            # The clock still advances past every entry, dropped or not.
            try:
                self._prov_clock = max([0] + [int(v.get("last_exec", 0)) for v in data.values()])
            except Exception:
                self._prov_clock = 0
            self._var_provenance = self._user_provenance(data)

    # ---- Runtime section (DAG): clock + provenance + run ledger ----

    def record_run(self, cell_id, cell_type: str, code: str, ok: bool) -> None:
        """Record the last run of a cell in the run ledger: the hash of the text it
        executed (dag_contract.cell_code_hash normalises SQL inside the hash), the
        provenance clock after the run, and whether it succeeded. No-op without a
        cell id. Called BEFORE runtime_section() on every path, so a response's
        runtime.runs already contains the cell that just ran."""
        if not cell_id:
            return
        with self._exec_lock:
            self._runs[str(cell_id)] = {
                "code_hash": cell_code_hash(cell_type, code if isinstance(code, str) else ""),
                "clock": self._prov_clock,
                "ok": bool(ok),
            }

    def _build_runtime_section(self) -> dict:
        return {
            "clock": self._prov_clock,
            # User variables only — also hides noise a restored old checkpoint
            # brought in until its import replay rebinds the modules.
            "vars": copy.deepcopy(self._user_provenance(self._var_provenance)),
            "runs": copy.deepcopy(self._runs),
        }

    def runtime_section(self, lock_timeout: float | None = None) -> dict:
        """Deep copy of {clock, vars, runs} taken under _exec_lock.

        lock_timeout None waits indefinitely (immediate when the caller already
        holds the RLock, e.g. inside _execute_locked); otherwise raises
        RuntimeSectionBusy when the lock is not acquired in time. Call only from a
        worker thread — an off-lock caller may hold the lock for a whole cell."""
        timeout = -1 if lock_timeout is None else lock_timeout
        if not self._exec_lock.acquire(timeout=timeout):
            raise RuntimeSectionBusy(f"_exec_lock not acquired within {lock_timeout}s")
        try:
            return self._build_runtime_section()
        finally:
            self._exec_lock.release()

    def runtime_section_unlocked(self) -> dict:
        """BEST-EFFORT copy of the runtime section WITHOUT taking _exec_lock. Used
        only by checkpoint save when the lock is busy; may raise RuntimeError
        (dict changed size during iteration) — the caller retries."""
        return self._build_runtime_section()

    def set_runtime_section(self, section: dict) -> None:
        """Restore the runtime section (from dag.json). vars/runs must be dicts;
        the clock becomes max(section.clock, max vars[*].last_exec)."""
        if not isinstance(section, dict):
            return
        vars_ = section.get("vars")
        runs = section.get("runs")
        vars_ = vars_ if isinstance(vars_, dict) else {}
        runs = runs if isinstance(runs, dict) else {}
        vars_ = {k: v for k, v in vars_.items() if isinstance(k, str) and isinstance(v, dict)}
        runs = {k: v for k, v in runs.items() if isinstance(k, str) and isinstance(v, dict)}
        clock = section.get("clock")
        if isinstance(clock, bool) or not isinstance(clock, int):
            clock = 0
        try:
            last = max([0] + [int(v.get("last_exec", 0) or 0) for v in vars_.values()])
        except Exception:
            last = 0
        with self._exec_lock:
            # Old checkpoints may hold dunder/module/helper entries: dropped
            # (the clock above still covers them).
            self._var_provenance = self._user_provenance(vars_)
            self._runs = runs
            self._prov_clock = max(clock, last)

    def interrupt(self) -> bool:
        """
        Interrupt the running cell by raising KeyboardInterrupt in the exec thread.
        Returns True if an interrupt was injected, False if nothing was running.
        At most one KeyboardInterrupt is injected per Python cell (timeout timer and
        Stop share it): a Stop after the timer or an earlier Stop already fired, or
        once the cell left exec(), returns False (the route answers "idle").

        Reliably stops runaway PURE-PYTHON code (e.g. `while True: ...`): the async
        exception is delivered the next time the thread runs bytecode (injected
        once, see below).

        A running SQL cell (no Python cell running) is interrupted through its
        guard (self._sql_run), which also cancels the query itself: DuckDB
        con.interrupt(), Athena stop_query_execution.

        LIMITATION: PyThreadState_SetAsyncExc cannot break a thread blocked in a
        C-level call (time.sleep, socket recv, a C extension). Such a cell only
        aborts when that call returns — the exception is queued until then. Forcing
        those to stop would require per-cell process isolation (kill the process),
        which this in-process executor does not do.
        """
        py_run = self._py_run
        if py_run is None:
            sql_run = self._sql_run
            return sql_run.interrupt() if sql_run is not None else False
        return py_run.interrupt()

    def execute(self, code: str, cell_id=None, capture_runtime: bool = False,
                scratch: dict | None = None) -> ExecutionResult:
        """
        Execute Python code in the persistent namespace.

        All variables defined will be available in subsequent executions.
        This is how AI agents build up state across multi-step tasks.

        cell_id (optional): the frontend cell id, recorded as provenance so the
        Variables panel can show which cell created/modified each variable.

        SERIALIZED: acquires the executor's internal lock for the WHOLE execution so
        the process-global sys.stdout/stderr swap done in _execute_locked can never
        overlap another execute() call (which would leak the other cell's prints to
        the real fd → the VM log). This makes mutual exclusion a property of the
        executor, not of each FastAPI handler — closing the /validate and
        import-replay bypasses that didn't share the handler-level asyncio lock.

        capture_runtime: when True (only the /execute route), the result carries
        `runtime` = runtime_section() taken at the end of the run while the lock is
        still held (after record_run), on success and on error.

        scratch (optional dict): run as an EXPLORATION probe (AI agent / internal
        caller) instead of a cell. The code executes against an overlay
        `{**namespace, **scratch}` (shallow: references, no copies), so it reads
        every user variable; afterwards every name it created or rebound is written
        into `scratch` (never into the user namespace) and names it deleted are
        removed from `scratch` only. Provenance, the import ledger, the run ledger
        and the display() shim bookkeeping are left untouched, so the run is
        invisible to /variables, the DAG runtime and the checkpoint. The result
        fields are computed exactly as for a cell. LIMIT: in-place mutation of a
        shared object (df['c'] = …, list.append) still reaches the user's object.
        Functions defined in a probe keep that run's overlay as their globals.
        """
        with self._exec_lock:
            return self._execute_locked(code, cell_id, capture_runtime=capture_runtime,
                                        scratch=scratch)

    def _execute_locked(self, code: str, cell_id=None, capture_runtime: bool = False,
                        scratch: dict | None = None) -> ExecutionResult:
        """Real execute() body — ALWAYS call via execute() so the _exec_lock is held
        (the sys.stdout/stderr swap below is process-global and not concurrency-safe)."""
        self._execution_count += 1
        start = datetime.now(timezone.utc)
        is_scratch = scratch is not None
        # The namespace this run executes in. A scratch run gets a fresh overlay
        # dict; self._namespace itself is never reassigned (off-lock readers such
        # as /variables and checkpoint save keep seeing only the user dict).
        ns = {**self._namespace, **scratch} if is_scratch else self._namespace

        # Capture stdout/stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture

        variables_before = set(ns.keys())
        # Snapshot fingerprints of existing vars so we can detect which were modified.
        before_fps = {k: self._var_fingerprint(ns[k]) for k in variables_before}
        # Scratch write-back compares bindings by identity. Every pre-run object is
        # still referenced (by the user ns or the scratch), so ids cannot be reused.
        before_ids = {k: id(v) for k, v in ns.items()} if is_scratch else {}

        # Provide a display() function (like Jupyter's IPython.display.display)
        # that allows users to explicitly render objects from anywhere in the code
        # (inside if/else blocks, loops, functions, etc.)
        # Ordered display events: (stdout offset, kind, object). Kinds: "display"
        # (a rich object passed to display()), "mpl" (list of matplotlib figures at
        # a plt.show() call), "plotly" (a figure whose .show() was called). Only
        # references are kept here; serialization happens in the capture phase.
        display_events: list = []
        display_overflow = [0]

        def _record_event(kind, obj):
            if len(display_events) >= MAX_DISPLAY_REFS:
                display_overflow[0] += 1
                return
            try:
                off = stdout_capture.tell()
            except Exception:
                off = len(stdout_capture.getvalue())
            display_events.append((off, kind, obj))

        def _display_fn(*objs):
            """Display objects — works from inside conditionals, loops, and functions."""
            for obj in objs:
                if self._rich_kind(obj):
                    _record_event("display", obj)

        ns['display'] = _display_fn
        if not is_scratch:
            # A scratch run's shim lives in its overlay only.
            self._display_fn = _display_fn

        # Track current thread for interrupt support
        self._exec_thread_id = threading.current_thread().ident

        # Clear any lingering matplotlib figure from previous execution
        # (prevents plots from bleeding into subsequent cells)
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
        except Exception:
            pass

        # Execution timeout — prevents infinite loops from hanging the VM.
        # run_state: armed while exec/eval may still be running; reason is the
        # first of "timeout" / "interrupt" (later ones are no-ops).
        guard = _PythonRunGuard(self)

        # Start recording the data-source IDENTIFIERS this cell's reader helpers
        # touch (lineage "Sources"); ended in the finally below.
        _sources_read: list = []
        read_capture.begin()
        # Start sampling peak CPU/memory for the duration of exec() (stopped in the
        # finally below). Attributes resource use to THIS cell for the run-history
        # per-cell breakdown. No-op if psutil is unavailable.
        sampler = _ResourceSampler()
        sampler.start()

        # 1-indexed line in the cell where an error occurred, if any. Set by the
        # except handlers below; stays None on success or when no cell frame exists.
        error_lineno = None
        # Richer debug context for a runtime error (populated by the except handler).
        error_frames: list = []
        error_locals: dict = {}
        error_traceback = ""
        # plt.show() / Plotly fig.show() only RECORD (offset, figure) for this run;
        # restored in the finally below, still under _exec_lock.
        _restore_show = self._install_show_wrappers(_record_event)

        # Value of the cell's last top-level bare expression (shown as an output
        # by the capture phase). has_last_value is False when the cell does not end
        # in a bare expression, ends in a display(...) call (already recorded as a
        # display event) or fails.
        last_value = None
        has_last_value = False
        try:
            # Armed only inside this try, so a timeout / Stop can never land
            # outside the handlers below.
            guard.start()
            try:
                # Parse once. If the last top-level statement is a bare expression, the
                # rest of the cell runs as the body (exec) and that expression as an eval.
                # Both are compiled from the SAME tree, so tracebacks and error_lineno
                # point at the user's lines, and both are compiled before anything runs.
                # They run here, inside the timeout timer / interrupt window and under
                # _exec_lock, so the last expression executes exactly once.
                tree = ast.parse(code, "<sandbox>", "exec")
                last_expr = None
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    last_expr = ast.Expression(body=tree.body.pop().value)
                compiled = compile(tree, "<sandbox>", "exec")
                expr_compiled = (compile(last_expr, "<sandbox>", "eval")
                                 if last_expr is not None else None)
                exec(compiled, ns)
                if expr_compiled is not None:
                    value = eval(expr_compiled, ns)
                    if not self._is_display_call(last_expr.body):
                        last_value, has_last_value = value, True
            finally:
                # Disarm BEFORE any handler below runs. A KeyboardInterrupt injected
                # earlier surfaces here at the latest and is handled by
                # `except KeyboardInterrupt` below.
                guard.finish()
            success = True
            error = ""
        except KeyboardInterrupt:
            guard.finish()  # a KeyboardInterrupt raised inside finish() itself
            success = False
            error = guard.error_message()
        except SyntaxError as e:
            # SyntaxError is raised at COMPILE time, so it never enters the user
            # traceback frames — but it carries its own 1-indexed .lineno pointing at
            # the offending line in the cell. Surface it the same way as runtime errors.
            success = False
            error_type = type(e).__name__
            error_msg = e.msg or str(e)
            if isinstance(e.lineno, int) and e.lineno > 0:
                error_lineno = e.lineno
                error = f"{error_type}: {error_msg} (line {error_lineno})"
            else:
                error = f"{error_type}: {error_msg}"
        except Exception as e:
            success = False
            error_type = type(e).__name__
            error_msg = str(e)
            # Build the full debugging context: structured cell frames (all of them,
            # deepest last — a helper called from the cell yields several), a safe
            # snapshot of the failing frame's locals, and a cleaned multi-frame
            # traceback (library-internal frames pruned). The one-line `error` string
            # + `error_lineno` are kept for backward compat (message + editor gutter);
            # error_lineno points at the DEEPEST cell line (the actual crash point).
            error_frames, error_locals, error_traceback = _build_error_debug(e)
            if error_frames:
                error_lineno = error_frames[-1].get("lineno")
                error = f"{error_type}: {error_msg} (line {error_lineno})"
            else:
                error = f"{error_type}: {error_msg}"
        finally:
            guard.finish()  # no-op when the inner finally already ran
            # Stop resource sampling and capture the peaks (cheap; joins its thread).
            sampler.stop()
            _sources_read = read_capture.end()
            # Clear the thread-id FIRST so a later interrupt() finds nothing to
            # inject into (it returns False when _exec_thread_id is None).
            _finished_tid = self._exec_thread_id
            self._exec_thread_id = None
            # Defensively drain any pending async KeyboardInterrupt that an
            # interrupt() or the timeout timer queued on THIS thread after the cell
            # already ended. Without this, the pending exception could fire on the
            # NEXT cell that reuses this pooled worker thread and spuriously fail
            # it. Passing NULL (0) cancels a pending async exc.
            if _finished_tid:
                try:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(_finished_tid), ctypes.c_void_p(0)
                    )
                except Exception:
                    pass
            _restore_show()
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        variables_after = set(ns.keys())
        new_vars = list(variables_after - variables_before)
        # Pre-existing variables this cell changed IN PLACE (df['c'] = ..., an
        # inplace=True sort, list.append, ...) without rebinding them. Filled on
        # success below; reported as variables_mutated and folded into the cell's
        # written list so the dependency graph chains builder → mutator → readers.
        inplace_vars: set = set()
        # Pre-existing vars this cell rebound at the top level (filled on success);
        # reported as variables_rebound.
        redefined_vars: set = set()

        # Provenance: attribute created/modified/deleted vars to this cell.
        # created = new keys; deleted = removed keys; modified = surviving vars whose
        # fingerprint changed (reassignment or shape-changing in-place op) UNION the
        # AST-assigned targets that still exist (catches in-place subscript/attr writes
        # like df['c'] = ... that don't change shape). Only track on success.
        if success:
            deleted_vars = variables_before - variables_after
            modified_vars = set()
            common = variables_before & variables_after
            for name in common:
                if before_fps.get(name) != self._var_fingerprint(ns.get(name)):
                    modified_vars.add(name)
            for name in self._assigned_targets(code):
                if name in variables_after and name not in variables_before:
                    continue  # already counted as created
                if name in variables_after:
                    modified_vars.add(name)
            # `redefined` = names this cell REBINDS at the top level (plain `x = ...`,
            # not `df['c'] = ...`) that already existed. Re-running the cell that
            # defines a variable IS a definition, so its `defined_by` must be
            # re-stamped to THIS cell — otherwise a var carried over from a prior
            # run or a session restore keeps a stale `defined_by` (pointing at a
            # cell id that no longer exists) and the Variables panel can't show
            # "Defined in". Restricted to true rebinds so an in-place edit cell
            # can't steal ownership from the creating cell.
            redefined_vars = {n for n in self._rebound_names(code)
                              if n in variables_after and n not in new_vars}
            # A rebind is a definition even if the value is unchanged (e.g. re-running
            # `x = 5`): its fingerprint may match, so it wouldn't be flagged modified.
            # Fold it into modified so _update_provenance always re-stamps defined_by.
            inplace_vars = (modified_vars - redefined_vars) & variables_before
            modified_vars |= redefined_vars
            # Provenance records user variables only (same filter as the def-use
            # lists below): dunders (`__builtins__`), modules and untouched
            # injected helpers/display are dropped; a name that turned into one of
            # those (e.g. `x = 1` then `import os as x`) loses its entry.
            _prov_injected = {**self._injected_helpers(), "display": _display_fn}
            _touched = set(new_vars) | modified_vars
            _kept = set(self._user_var_names(_touched, _prov_injected, ns))
            # A scratch run (agent / internal probe) never touches provenance or
            # the import ledger: _prov_clock (the checkpoint dirty fingerprint)
            # and the DAG runtime vars stay exactly as they were.
            if not is_scratch:
                self._update_provenance(cell_id, created=[n for n in new_vars if n in _kept],
                                        modified=modified_vars & _kept,
                                        deleted=deleted_vars | (_touched - _kept),
                                        redefined=redefined_vars & _kept)
                # Record any imports this cell established so they can be replayed after a
                # checkpoint restore (modules themselves aren't picklable). Only bindings
                # that actually survived in the namespace are kept, so a failed/partial
                # import (e.g. `import nonexistent`) is not remembered. Starred imports
                # (`from x import *`) are kept regardless since they bind unknown names.
                try:
                    for _name, _line in self._extract_imports(code).items():
                        if _name.endswith(":*") or _name in self._namespace:
                            self._imports[_name] = _line
                except Exception:
                    pass
        elif not is_scratch:
            # A failed cell keeps the bindings made before the exception (and any
            # in-place mutation), but provenance is only recorded on success. Still
            # advance the clock (as every successful run does, via
            # _update_provenance) so the checkpoint dirty fingerprint sees the change
            # and /suspend does not skip the save.
            self._prov_clock += 1

        output = stdout_capture.getvalue()
        if stderr_capture.getvalue():
            output += stderr_capture.getvalue()

        html_output = ""
        image_output = ""
        outputs: list = []
        # The output-capture phase does not run the cell again (the last
        # expression's value was taken inside the timed exec above), but the
        # capturers still call into user objects: __repr__/_repr_html_/to_html,
        # DataFrame/Plotly/matplotlib rendering. That runs AFTER the finally above
        # restored the real stdout/stderr, so a print() from such a method would
        # write straight to the process fd and so to the VM's CloudWatch logs (raw,
        # prefix-less lines). Redirect stdout/stderr to a THROWAWAY buffer for the
        # capture phase and discard it. Restored again in the finally.
        # (Still under _exec_lock, so the process-global swap is safe.)
        # Runs on error too (display/show/figure items only: no last-expression
        # value, no namespace scan) so work done before the exception is kept.
        _sink = io.StringIO()
        _cap_out, _cap_err = sys.stdout, sys.stderr
        sys.stdout = _sink
        sys.stderr = _sink
        try:
            outputs = self._collect_outputs(
                code, ns, list(display_events), display_overflow[0],
                stdout_capture.getvalue(), stderr_capture.getvalue(),
                success, variables_before,
                last_value=last_value, has_last_value=has_last_value)
        except Exception:
            outputs = []
        finally:
            sys.stdout = _cap_out
            sys.stderr = _cap_err
        if success:
            # Legacy single-slot fields for older web builds: html = the first
            # table/Plotly item, image = the LAST figure's PNG.
            for _it in outputs:
                if _it["t"] == "table":
                    html_output = json.dumps(_it["payload"])
                    break
                if _it["t"] == "plotly":
                    html_output = _it["html"]
                    break
            for _it in reversed(outputs):
                if _it["t"] == "image":
                    image_output = _it["src"]
                    break

        # Free variables this cell reads from the shared namespace (def-use "use"
        # half). Static from the code, so computed regardless of success. Restrict
        # to names that actually exist in the namespace so we don't emit reads for
        # builtins/locals-that-looked-free — keeps the frontend graph to real vars.
        # Both lists are then narrowed to user variables (no dunders, modules or
        # untouched injected helpers/display) — see _user_var_names.
        injected = self._injected_helpers()
        injected["display"] = _display_fn
        try:
            read_names = self._referenced_names(code)
            reads = sorted(self._user_var_names(read_names, injected, ns))
        except Exception:
            reads = []
        try:
            written = self._user_var_names(new_vars, injected, ns)
        except Exception:
            written = []
        # In-place mutation = a pre-existing var this cell READ and changed without
        # rebinding (same rule as static_analysis.mutated_names). It is both a read
        # and a write of that var, so it also goes into `written`.
        try:
            mutated = sorted(n for n in self._user_var_names(inplace_vars, injected, ns)
                             if n in reads)
        except Exception:
            mutated = []
        written = list(written) + [n for n in mutated if n not in written]
        # Rebinding an existing user variable is a definition (incl. AugAssign
        # targets; the proxy maps those back to mutates with the static facts).
        try:
            rebound = sorted(self._user_var_names(redefined_vars, injected, ns))
        except Exception:
            rebound = []

        result = ExecutionResult(
            success=success,
            output=output,
            error=error,
            error_lineno=error_lineno,
            traceback=error_traceback,
            error_frames=error_frames,
            error_locals=error_locals,
            variables_created=written,
            variables_read=reads,
            variables_mutated=mutated,
            variables_rebound=rebound,
            execution_time_ms=round(elapsed_ms, 2),
            peak_mem_mb=round(sampler.peak_mem_mb, 1),
            peak_cpu_pct=round(sampler.peak_cpu_pct, 1),
            html=html_output,
            image=image_output,
            outputs=outputs,
            sources_read=_sources_read,
        )

        # Record history (capped at 1000 entries to prevent unbounded memory growth)
        self._history.append({
            "execution_number": self._execution_count,
            "code": code[:500],  # truncate for history
            "success": success,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(self._history) > 1000:
            self._history = self._history[-1000:]

        if is_scratch:
            # Keep what the probe made for later probes of the same scope: every
            # name new or rebound (by identity) vs the pre-run overlay goes into the
            # scratch; names the probe deleted leave the scratch only (the user's
            # binding stays). On error too, like a cell's partial bindings.
            for name, val in ns.items():
                if name == "__builtins__" or (name == "display" and val is _display_fn):
                    continue
                if before_ids.get(name) != id(val):
                    scratch[name] = val
            for name in variables_before - variables_after:
                scratch.pop(name, None)
        else:
            # Run ledger — record_run BEFORE the runtime section so runtime.runs
            # contains this run. A scratch run is not a cell run: skipped.
            self.record_run(cell_id, "code", code, success)
        # (Only for the /execute route) the runtime section. Still under the
        # (re-entrant) _exec_lock, on success and on error.
        if capture_runtime:
            result.runtime = self.runtime_section()

        return result

    # ── Ordered rich outputs (tables, Plotly charts, images) ───────────────────
    @staticmethod
    def _rich_kind(obj) -> str:
        """Cheap type check, no serialization: 'table', 'plotly', 'mpl' or ''."""
        t = type(obj)
        name = t.__name__
        mod = t.__module__ or ""
        if 'pandas' in mod and name in ('DataFrame', 'Series'):
            return 'table'
        if 'polars' in mod and name in ('DataFrame', 'LazyFrame'):
            return 'table'
        if 'plotly' in mod and name == 'Figure':
            return 'plotly'
        if mod.startswith('matplotlib') and name == 'Figure':
            return 'mpl'
        return ''

    @staticmethod
    def _table_payload(val) -> str:
        """The __df_table__ JSON for a pandas DataFrame/Series or polars frame."""
        name = type(val).__name__
        if 'polars' in (type(val).__module__ or ''):
            pdf = val.collect().to_pandas() if name == 'LazyFrame' else val.to_pandas()
            return build_df_table_payload(pdf)
        if name == 'Series':
            # Show the index alongside the values (matches the old to_html view).
            val = val.reset_index()
        elif hasattr(val, 'columns'):
            if hasattr(val.columns, 'nlevels') and val.columns.nlevels > 1:
                val = val.copy()
                val.columns = ['_'.join(str(c) for c in col).strip('_') for col in val.columns]
            if val.index.name or (hasattr(val.index, 'names') and any(n for n in val.index.names if n)):
                val = val.reset_index()
        return build_df_table_payload(val)

    @staticmethod
    def _figure_png(fig) -> str:
        """A matplotlib figure as a base64 PNG data URL (the same savefig call the
        single-figure capture always used)."""
        import base64
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight',
                    facecolor='#1e1e2e', edgecolor='none')
        buf.seek(0)
        return "data:image/png;base64," + base64.b64encode(buf.read()).decode('utf-8')

    @staticmethod
    def _label_for(obj, ns: dict):
        """First public name in the run namespace bound (`is`) to obj, else None."""
        try:
            for k, v in list(ns.items()):
                if v is obj and isinstance(k, str) and not k.startswith('_') and k != 'display':
                    return k
        except Exception:
            pass
        return None

    def _rich_item(self, obj, ns: dict):
        """A table or plotly output item for obj (None for anything else)."""
        kind = self._rich_kind(obj)
        if kind == 'table':
            # The payload is the __df_table__ object itself (not a JSON string
            # inside JSON); the legacy `html` string is json.dumps(payload).
            item = {"t": "table", "payload": json.loads(self._table_payload(obj))}
        elif kind == 'plotly':
            item = {"t": "plotly", "html": self._render_plotly_html(obj)}
        else:
            return None
        label = self._label_for(obj, ns)
        if label:
            item["label"] = label
        return item

    @staticmethod
    def _open_figures() -> list:
        """Open matplotlib figures in figure-number order ([] without matplotlib)."""
        try:
            import matplotlib
            matplotlib.use('Agg')  # Ensure non-interactive backend
            import matplotlib.pyplot as plt
            return [plt.figure(n) for n in plt.get_fignums()]
        except Exception:
            return []

    @staticmethod
    def _install_show_wrappers(record):
        """Wrap plt.show and Plotly BaseFigure.show for ONE run so the figures they
        are called with are recorded in call order (record(kind, obj)). They record
        only: PNG/HTML serialization stays in the capture phase after exec, so
        execution_time_ms is unaffected (and a figure is rendered in its state at the
        end of the run, like display()). Returns the function that restores both.
        Matplotlib is Agg here and Plotly has no headless renderer, so the originals
        would not show anything in the cell; they are not called."""
        restores = []
        try:
            plt = sys.modules.get('matplotlib.pyplot')
            if plt is not None:
                orig_show = plt.show
                recorded = set()

                def _plt_show(*args, **kwargs):
                    try:
                        from matplotlib import _pylab_helpers
                        figs = [m.canvas.figure for m in _pylab_helpers.Gcf.get_all_fig_managers()]
                    except Exception:
                        figs = []
                    new = [f for f in figs if id(f) not in recorded]
                    for f in new:
                        recorded.add(id(f))
                    if new:
                        record("mpl", new)

                plt.show = _plt_show
                restores.append(lambda: setattr(plt, 'show', orig_show))
        except Exception:
            pass
        try:
            bdt = sys.modules.get('plotly.basedatatypes')
            if bdt is not None:
                cls = bdt.BaseFigure
                orig_fig_show = cls.show

                def _fig_show(self, *args, **kwargs):
                    record("plotly", self)

                cls.show = _fig_show
                restores.append(lambda: setattr(cls, 'show', orig_fig_show))
        except Exception:
            pass

        def _restore():
            for fn in restores:
                try:
                    fn()
                except Exception:
                    pass
        return _restore

    @staticmethod
    def _is_display_call(node) -> bool:
        """True if the expression node is a call to display(...). Its object is
        already recorded as a display event, so a cell ending in display(x) must
        not list x again as the last expression's value. (Pure AST check, no eval.)"""
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'display')

    def _collect_outputs(self, code: str, ns: dict, events: list, overflow: int,
                         out_text: str, err_text: str, success: bool,
                         variables_before: set, last_value=None,
                         has_last_value: bool = False) -> list:
        """Build the ordered output list for one run (capture phase, after exec).

        last_value / has_last_value: the last top-level expression's value, taken
        during the run itself (never re-evaluated here).

        Order: display()/show() events in call order, then the last expression's
        value, then still-open matplotlib figures (figure-number order), with the
        printed text split into segments at each item's stdout offset and stderr as
        one trailing text item. Returns [] unless at least one rich item exists.
        Capped at MAX_RICH_OUTPUTS items / MAX_OUTPUT_BYTES, then a note item."""
        rich: list = []          # (stdout offset, item)
        more = overflow          # rich outputs that were not kept
        used = 0
        seen_figs: list = []     # keeps figure ids (matplotlib and Plotly) stable while we run
        seen_ids: set = set()
        total = len(out_text)

        def full() -> bool:
            return len(rich) >= MAX_RICH_OUTPUTS

        def add(off: int, item) -> None:
            nonlocal more, used
            size = (len(json.dumps(item["payload"])) if item["t"] == "table"
                    else len(item.get("html") or item.get("src") or ""))
            # The first rich item is always kept (a single big table showed before
            # the caps existed); the byte cap applies to later items only.
            if full() or (rich and used + size > MAX_OUTPUT_BYTES):
                more += 1
                return
            used += size
            rich.append((off, item))

        def add_fig(off: int, fig) -> None:
            nonlocal more
            if id(fig) in seen_ids:
                return
            seen_ids.add(id(fig))
            seen_figs.append(fig)
            try:
                if not fig.get_axes():  # matplotlib figures without axes are skipped
                    return
                if full():
                    more += 1
                    return
                add(off, {"t": "image", "src": self._figure_png(fig)})
            except Exception:
                pass

        def add_obj(off: int, obj) -> None:
            nonlocal more
            kind = self._rich_kind(obj)
            if kind == 'mpl':
                add_fig(off, obj)
                return
            if kind == 'plotly':
                # One chart per figure object per run, at its first emission (like
                # matplotlib above). A plot_* helper displays the figure AND returns
                # it, so `plot_x(...)` as the last line, `fig.show()` then `fig`, etc.
                # would otherwise list the same figure twice. Every item is rendered
                # after the run, so a repeat could only ever be an identical copy.
                if id(obj) in seen_ids:
                    return
                seen_ids.add(id(obj))
                seen_figs.append(obj)
            if full():
                more += 1
                return
            try:
                item = self._rich_item(obj, ns)
            except Exception:
                item = None
            if item:
                add(off, item)

        for off, kind, obj in events:
            if kind == "mpl":
                for fig in obj:
                    add_fig(off, fig)
            else:
                add_obj(off, obj)

        # Figures still open at the end (figure-number order).
        open_figs = self._open_figures()
        if success and has_last_value:
            val = last_value
            if val is not None and self._rich_kind(val):
                add_obj(total, val)
        for fig in open_figs:
            add_fig(total, fig)
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
        except Exception:
            pass
        # Namespace scan: a figure the cell only assigned (never displayed). Last
        # resort, only when nothing else was rich.
        if success and not rich:
            html = self._capture_plotly_figure(code, variables_before, ns)
            if html:
                rich.append((total, {"t": "plotly", "html": html}))
        if not rich:
            return []

        items: list = []
        pos = 0
        for off, item in rich:
            off = min(max(off, pos), total)
            if off > pos:
                items.append({"t": "text", "text": out_text[pos:off]})
            items.append(item)
            pos = off
        if pos < total:
            items.append({"t": "text", "text": out_text[pos:]})
        if err_text:
            items.append({"t": "text", "text": err_text})
        if more:
            items.append({"t": "text", "text": f"… {more} more outputs not shown"})
        return items

    def _capture_plotly_figure(self, code: str, variables_before: set, ns: dict | None = None) -> str:
        """Check namespace for any Plotly Figure created or reassigned during this execution.

        This handles cases where a Plotly figure is created inside an if/else block
        or stored in a variable but not returned as the last expression.

        For well-known names ('fig', 'figure', 'chart'), the namespace persists across
        cells, so those names are frequently REUSED (e.g. every chart cell writes
        `fig = px.something(...)`). Checking "is this variable name new?" would skip
        every figure after the first cell that happens to use one of these names,
        silently dropping the chart. Instead, we parse the code that just ran (via ast)
        to see whether it actually assigns one of these names anywhere — including
        inside if/else/for blocks — and only then treat the current value as this
        execution's output. This avoids relying on object identity/memory-address
        comparisons, which are unreliable in CPython due to id() reuse after garbage
        collection.

        ns: the namespace the run executed in (default: the user namespace).
        """
        if ns is None:
            ns = self._namespace
        try:
            import plotly.graph_objects as go

            # Look for entirely new variables that are Plotly Figures
            for var_name in set(ns.keys()) - variables_before - {'__builtins__'}:
                val = ns.get(var_name)
                if isinstance(val, go.Figure):
                    return self._render_plotly_html(val)

            # Also check well-known figure variable names, but only if THIS cell's
            # code actually assigned to that name (at any nesting level).
            assigned_names = self._assigned_names(code)
            for var_name in ('fig', 'figure', 'chart'):
                if var_name not in assigned_names:
                    continue  # this cell didn't touch the name — don't re-show a stale figure
                val = ns.get(var_name)
                if isinstance(val, go.Figure):
                    return self._render_plotly_html(val)
        except ImportError:
            pass
        except Exception:
            pass
        return ""

    @staticmethod
    def _render_plotly_html(fig) -> str:
        """Render a Plotly Figure to the HTML snippet used for cell output."""
        html = fig.to_html(
            full_html=False,
            include_plotlyjs='cdn',
            config={'responsive': True, 'displayModeBar': True},
        )
        return f'<div class="plotly-chart" data-plotly="true">{html}</div>'

    @staticmethod
    def _assigned_names(code: str) -> set:
        """Return every simple name assigned anywhere in the code (any nesting level,
        including inside if/else/for/while/functions). Used to detect whether a cell
        writes to a well-known variable name like `fig`, without relying on
        before/after namespace diffing (which breaks when the same name is reused
        across cells) or object identity (which is unreliable due to id() reuse).
        """
        import ast
        names = set()
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return names
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            elif isinstance(node, ast.NamedExpr):  # walrus operator: fig := ...
                targets = [node.target]
            for target in targets:
                for n in ast.walk(target):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        return names

    def install_package(self, package: str) -> ExecutionResult:
        """Install a pip package into the sandbox runtime."""
        import re

        # SECURITY: Validate package name to prevent command injection and flag injection.
        # Valid pip package names: letters, digits, hyphens, dots, underscores.
        # Optionally with version specifier (e.g. pandas==2.0, requests>=2.28).
        PACKAGE_NAME_PATTERN = re.compile(
            r'^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?'  # package name
            r'(\[([a-zA-Z0-9._-]+,?\s*)*\])?'  # optional extras like [dev,test]
            r'([><=!~]=?[a-zA-Z0-9.*]+)?$'  # optional version specifier
        )

        package = package.strip()
        if not package or not PACKAGE_NAME_PATTERN.match(package):
            return ExecutionResult(
                success=False,
                error=f"Invalid package name: '{package}'. Use a valid PyPI package name (e.g. 'pandas', 'requests>=2.28').",
            )

        # SECURITY: Reject anything that looks like pip flags
        if package.startswith('-') or '--' in package or ' ' in package:
            return ExecutionResult(
                success=False,
                error=f"Invalid package name: '{package}'. Package names cannot contain flags or spaces.",
            )

        # Log at the executor level so a pip install triggered from CELL CODE
        # (executor.install_package directly) is visible too — the /install endpoint
        # adds its own richer lines, but that path is bypassed when a cell calls this.
        from app.platform import pip_helper

        # No --quiet: the "Building wheel for X" lines are what pip_diagnostics
        # classifies. --no-input: never wait on a prompt (there is no terminal).
        pip_args = ["install", "--progress-bar", "off", "--disable-pip-version-check",
                    "--no-input", package]
        pip_env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        def _pip_subprocess():
            # Popen + communicate (not run) so a timeout still leaves us the
            # partial output, which is how we tell "building from source timed
            # out" from "still downloading". A new session/process group lets us
            # kill pip's build children too, so they can't hold the pipes open
            # after the timeout.
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip"] + pip_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=pip_env,
                start_new_session=True,
            )
            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=PIP_INSTALL_TIMEOUT_S)
            except subprocess.TimeoutExpired as first:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired as second:
                    # Pipes still held open: use whatever was captured so far.
                    stdout = second.stdout or first.stdout or ""
                    stderr = second.stderr or first.stderr or ""
                    if isinstance(stdout, bytes):
                        stdout = stdout.decode("utf-8", "replace")
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode("utf-8", "replace")
            return stdout or "", stderr or "", (None if timed_out else proc.returncode), timed_out

        _pip_start = time.time()
        logger.info(f"📦 pip install {package} (starting, {PIP_INSTALL_TIMEOUT_S}s timeout)")
        try:
            # One pip at a time on this VM (restore, other installs, uninstall).
            try:
                with pip_helper.pip_lock(wait_s=PIP_LOCK_WAIT_S):
                    _pip_start = time.time()
                    # The pre-imported pip helper (forks from the snapshot, no cold
                    # imports); None = not available, so run the plain subprocess.
                    res = pip_helper.run_via_helper(pip_args, timeout=PIP_INSTALL_TIMEOUT_S, env=pip_env)
                    if res is not None:
                        via = "helper"
                        stdout, stderr, timed_out = res.stdout, res.stderr, res.timed_out
                        returncode = None if timed_out else res.returncode
                    else:
                        via = "subprocess"
                        stdout, stderr, returncode, timed_out = _pip_subprocess()
            except pip_helper.PipBusy as busy:
                logger.warning(f"  ✗ pip install {package}: another pip run held the lock for {PIP_LOCK_WAIT_S}s")
                return ExecutionResult(success=False, error=str(busy))
            _pip_ms = (time.time() - _pip_start) * 1000
            if returncode == 0:
                logger.info(f"  ✓ pip install {package} ({_pip_ms:.0f}ms, via {via})")
                # Resolve the actual installed version so we can persist a pinned
                # spec (name==x.y.z) — this makes checkpoint/restore reproducible
                # instead of re-resolving loosely from PyPI on every restore.
                base = re.split(r'[<>=!~\[]', package, maxsplit=1)[0].strip()
                spec = package
                try:
                    import importlib.metadata as _md
                    version = _md.version(base)
                    spec = f"{base}=={version}"
                except Exception:
                    spec = package  # fall back to whatever the user typed
                return ExecutionResult(
                    success=True,
                    output=f"Successfully installed {spec}",
                    installed_spec=spec,
                )
            else:
                # Classify (no wheel for this arch, source build failed/timed
                # out, still downloading, …) so the user sees what happened.
                arch = normalize_arch(platform.machine())
                combined = stdout + stderr
                diag = describe_pip_failure(
                    package, combined, stderr,
                    timed_out=timed_out, returncode=returncode, arch=arch,
                    python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
                    timeout_s=PIP_INSTALL_TIMEOUT_S,
                )
                rc_text = "timeout" if timed_out else returncode
                logger.warning(f"  ✗ pip install {package} failed [{diag['kind']}] ({arch}, rc={rc_text}, {_pip_ms:.0f}ms, via {via}): {diag['message'][:300]}")
                logger.info(f"  pip output tail:\n{combined[-1500:]}")
                return ExecutionResult(
                    success=False,
                    error=diag["message"],
                    install_error_kind=diag["kind"],
                    install_details=combined[-20000:],
                    install_arch=arch,
                )
        except Exception as e:
            logger.warning(f"  ✗ pip install {package} error: {type(e).__name__}: {e}")
            return ExecutionResult(
                success=False,
                error=str(e),
            )

    # ---- Inline lint + format (ruff) -----------------------------------------
    #
    # A notebook is N cells that SHARE a runtime namespace but are SEPARATE text
    # fragments. ruff is a static analyzer over a single file's text, so the two
    # operations treat that split differently:
    #
    #   • lint  → concatenate ALL cells into one virtual module so cross-cell names
    #             resolve (a `df` defined in cell 1 and used in cell 5 is NOT flagged
    #             F821, and "imported but unused" is accurate across the notebook).
    #             ruff reports blob line numbers; we remap them back to (cell, line).
    #   • format→ run ruff format on EACH cell independently. Layout never crosses
    #             cell boundaries, so per-cell is correct AND avoids the whole-blob
    #             hazard of ruff injecting blank lines at the cell seams (the "two
    #             blank lines around a top-level def" rule firing at the boundary).

    # Ruff subprocess timeout — ruff is a fast native binary; even a large notebook
    # lints/formats in well under a second, so a small ceiling just bounds a hang.
    _RUFF_TIMEOUT_S = 30

    # Notebook-hostile lint rules to silence. Notebooks legitimately do things ruff
    # dislikes by default; suppressing these keeps the inline hints useful instead of
    # noisy. Kept deliberately small — everything else stays on.
    #   E402  module import not at top of file  (imports commonly appear mid-notebook)
    #   E501  line too long                      (display/URLs; not worth nagging inline)
    #   F404  late __future__ import
    #   B018  useless expression                 (a bare `df.head()` last line DISPLAYS it)
    #   F401  imported but unused                (an import in cell 1 may be used only in
    #                                             a cell the user hasn't written yet)
    #   W292  no newline at end of file           (cells never carry a trailing newline
    #                                             by design — this would fire on the last
    #                                             cell of EVERY notebook; pure artifact)
    _LINT_IGNORE = ["E402", "E501", "F404", "B018", "F401", "W292"]

    # Separator inserted between cells when building the lint blob. A newline-only
    # join would let cell N's trailing open construct bleed into cell N+1; a comment
    # line is a hard statement boundary ruff won't merge across, and it occupies
    # exactly ONE line so offset math stays trivial (see _build_lint_blob).
    _CELL_SEP = "# --- __cell_boundary__ ---"

    # SQL cells are formatted/linted with sqlglot (a pure-Python parser/transpiler),
    # NOT ruff — ruff only understands Python. sqlglot has no rule engine, so its
    # "lint" is limited to a SYNTAX check: a query that won't parse yields one
    # diagnostic (like a Python SyntaxError), nothing more. We use the DuckDB dialect
    # to match the notebook's SQL engine so valid DuckDB syntax (read_csv(...),
    # dynamodb."tbl", etc.) is never mis-flagged or mangled.
    _SQL_DIALECT = "duckdb"
    _SQL_LINT_CODE = "SQL-SYNTAX"  # synthetic code shown in the editor for a parse error

    # Notebook SQL cells use {name} placeholders (interpolated from namespace scalars
    # by the SQL engine at run time — see sql_engine._interpolate_scalars). These are
    # NOT valid SQL: sqlglot parses a bare `{min_units}` as a struct/map literal and
    # REGENERATES it as `{'min_units': min_units}` on format (silently corrupting the
    # query), and flags it as a syntax error on lint. So before handing SQL to
    # sqlglot for lint we MASK each {name} to a plain-identifier sentinel
    # `__param_<name>__`, which avoids false lint errors while still parsing the real
    # query structure. Placeholders inside string literals ('{market}') are masked
    # the same way (the sentinel is opaque text inside a string). Formatting (which
    # also unmasks) lives in the proxy's format_engine.
    _SQL_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")

    @classmethod
    def _mask_sql_placeholders(cls, sql: str) -> str:
        """{name} → __param_name__ so sqlglot treats it as a plain identifier."""
        return cls._SQL_PLACEHOLDER_RE.sub(lambda m: f"__param_{m.group(1)}__", sql)

    @staticmethod
    def _normalize_cells(cells) -> list[dict]:
        """Coerce the request's cell list into [{'id': str, 'code': str, 'type': str}]
        and drop malformed entries. Preserves order (notebook cell order defines the
        Python lint blob). `type` is 'sql' for SQL cells, else 'code' (Python)."""
        out = []
        if not isinstance(cells, list):
            return out
        for i, c in enumerate(cells):
            if not isinstance(c, dict):
                continue
            code = c.get("code", "")
            if not isinstance(code, str):
                continue
            cid = c.get("id")
            ctype = "sql" if c.get("type") == "sql" else "code"
            out.append({
                "id": str(cid) if cid is not None else str(i),
                "code": code,
                "type": ctype,
            })
        return out

    def _build_lint_blob(self, cells: list[dict]) -> tuple[str, list[tuple[int, int, str]]]:
        """Concatenate the PYTHON cells into one virtual module for whole-notebook
        linting. SQL cells are excluded (they aren't Python — they're linted
        separately by sqlglot), so a SQL cell never contributes lines to the blob
        and can't shift a Python cell's offsets.

        Returns (blob, spans) where spans is a list of (start_line, end_line, cell_id),
        1-indexed and inclusive, giving the blob line range each Python cell occupies.
        A single-line _CELL_SEP is placed BETWEEN cells (never before the first), so a
        diagnostic on a separator line maps to no cell and is dropped in the remap.
        """
        py_cells = [c for c in cells if c["type"] == "code"]
        parts: list[str] = []
        spans: list[tuple[int, int, str]] = []
        line = 1
        for idx, cell in enumerate(py_cells):
            if idx > 0:
                parts.append(self._CELL_SEP)
                line += 1  # the separator occupies exactly one blob line
            code = cell["code"]
            n = code.count("\n") + 1  # lines this cell contributes to the blob
            spans.append((line, line + n - 1, cell["id"]))
            parts.append(code)
            line += n
        return "\n".join(parts), spans

    def _lint_sql_cell(self, code: str) -> list[dict]:
        """Basic SQL lint via sqlglot: a SYNTAX check only (sqlglot has no rule
        engine). Returns a list with at most ONE diagnostic — the first parse error —
        in the same shape as the ruff diagnostics, so the editor renders it identically.
        A parseable (or empty) query returns []."""
        if not code.strip():
            return []
        try:
            import sqlglot
            from sqlglot.errors import ParseError
        except ImportError:
            # sqlglot missing (older image) → no SQL lint rather than a hard failure.
            return []
        # Mask {name} placeholders so a parameterized query isn't falsely flagged as
        # a syntax error (a bare {min_units} isn't valid SQL until interpolated).
        # NOTE: the sentinel is longer than {name}, so a genuine parse error ON a
        # placeholder line may report a slightly shifted column — an acceptable edge
        # cost, since a cleanly-parsing query returns no diagnostic at all.
        masked = self._mask_sql_placeholders(code)
        try:
            sqlglot.parse_one(masked, dialect=self._SQL_DIALECT)
            return []
        except ParseError as e:
            errs = getattr(e, "errors", None) or []
            first = errs[0] if errs else {}
            try:
                line = int(first.get("line")) if first.get("line") is not None else 1
            except (TypeError, ValueError):
                line = 1
            try:
                col = int(first.get("col")) if first.get("col") is not None else 1
            except (TypeError, ValueError):
                col = 1
            msg = first.get("description") or "SQL syntax error"
            # Collapse to a single line — sqlglot descriptions can embed a multi-line
            # context snippet that would break the editor's one-line tooltip.
            msg = " ".join(str(msg).split())
            return [{
                "line": max(line, 1),
                "col": max(col, 1),
                "code": self._SQL_LINT_CODE,
                "message": msg,
                "severity": "warning",
                "fixable": False,
            }]
        except Exception:
            # Any other sqlglot hiccup → no diagnostic (never fail the lint pass).
            return []

    def lint_notebook(self, cells) -> dict:
        """Lint the whole notebook (cross-cell context) and return per-cell diagnostics.

        Input: ordered [{'id', 'code'}] cells. Output:
            {"success": bool, "diagnostics": {cell_id: [ {line, col, end_line,
             end_col, code, message, severity, fixable}, ... ]}, "error": str}
        line/col are 1-indexed and RELATIVE TO THE CELL (remapped from the blob).
        """
        norm = self._normalize_cells(cells)
        if not norm:
            return {"success": True, "diagnostics": {}}

        # SQL cells are linted individually by sqlglot (syntax check); Python cells
        # go through the ruff blob below. Seed EVERY cell (both types) with an empty
        # list so the client can clear stale markers on any now-clean cell.
        diagnostics: dict[str, list] = {c["id"]: [] for c in norm}
        for c in norm:
            if c["type"] == "sql":
                diagnostics[c["id"]] = self._lint_sql_cell(c["code"])

        py_cells = [c for c in norm if c["type"] == "code"]
        if not py_cells:
            # SQL-only notebook — nothing for ruff to do; return the SQL diagnostics.
            return {"success": True, "diagnostics": diagnostics}

        blob, spans = self._build_lint_blob(norm)

        # Names available in a cell WITHOUT being defined in its own text, so ruff
        # shouldn't flag them F821 (it only sees the cell text). Three sources:
        #   • injected helper functions (read_s3_csv, read_athena, plot_bar, …),
        #   • the per-cell `display()` shim,
        #   • everything currently DEFINED IN THE LIVE KERNEL namespace — vars/funcs/
        #     modules from prior runs, checkpoint-restored, or UI-injected. This is
        #     what makes lint track the notebook's real state: once the user has run a
        #     cell defining a name (or restored/injected one), referencing it elsewhere
        #     is no longer a false "undefined name".
        # Deduplicated; order doesn't matter to ruff. Real undefined names still fire.
        builtin_names = sorted(set(self._helper_names()) | set(self._namespace_names()) | {"display"})

        cmd = [
            sys.executable, "-m", "ruff", "check",
            "--output-format", "json",
            # Explicit selection: ruff's DEFAULT set is narrow (E4/E7/E9 + F), so
            # classic bugs like `== None` (E711) or a bare-except (E722) wouldn't
            # fire. Select the pycodestyle errors/warnings (E,W), Pyflakes (F), and
            # bugbear (B) — the high-signal families — then subtract the
            # notebook-hostile ones below.
            "--select", "E,W,F,B",
            "--ignore", ",".join(self._LINT_IGNORE),
            "--no-cache",
        ]
        # Register the injected helpers + live-namespace names as `builtins` so ruff
        # doesn't flag them F821. NOTE: `builtins` is a ruff SETTING (top-level key),
        # NOT a CLI flag — there is no `--builtins`. We pass it as an inline TOML
        # override via `--config "builtins = [...]"`. Skip entirely when there are no
        # names (empty array is pointless) to keep the invocation minimal.
        if builtin_names:
            toml_array = "[" + ", ".join(json.dumps(n) for n in builtin_names) + "]"
            cmd += ["--config", f"builtins = {toml_array}"]
        cmd.append("-")  # read source from stdin
        try:
            proc = subprocess.run(
                cmd, input=blob, capture_output=True, text=True,
                timeout=self._RUFF_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            logger.warning("  ✗ ruff check timed out (%ss)", self._RUFF_TIMEOUT_S)
            return {"success": False, "error": f"Lint timed out ({self._RUFF_TIMEOUT_S}s limit)", "diagnostics": {}}
        except FileNotFoundError:
            return {"success": False, "error": "ruff is not installed in this sandbox", "diagnostics": {}}
        except Exception as e:
            logger.warning("  ✗ ruff check error: %s: %s", type(e).__name__, e)
            return {"success": False, "error": str(e), "diagnostics": {}}

        # ruff exits 1 when it finds violations (that's normal, not an error). A
        # non-empty stderr with unparseable stdout is a real failure.
        try:
            raw = json.loads(proc.stdout) if proc.stdout.strip() else []
        except json.JSONDecodeError:
            err = (proc.stderr or "").strip() or "ruff produced no parseable output"
            logger.warning("  ✗ ruff check failed (rc=%s): %s", proc.returncode, err[:200])
            return {"success": False, "error": err, "diagnostics": {}}

        # Merge ruff's Python diagnostics INTO the dict already holding SQL results
        # (don't overwrite it). _remap_lint only writes cell ids present in `spans`
        # (Python cells), so SQL entries are untouched.
        self._remap_lint(raw, spans, diagnostics)
        return {"success": True, "diagnostics": diagnostics}

    def _remap_lint(self, raw: list, spans: list[tuple[int, int, str]], out: dict) -> None:
        """Map ruff's blob-relative diagnostics back to per-cell, cell-relative ones,
        writing them INTO `out` (a {cell_id: [diagnostics]} dict pre-seeded by the
        caller — which may already hold SQL diagnostics for other cells).

        A diagnostic whose start line falls inside a cell's span is attributed to
        that cell and its line numbers are rebased to the cell (blob_line - span_start
        + 1). Diagnostics landing on a separator line (between spans) belong to no
        cell and are dropped."""
        def locate(blob_line: int):
            for start, end, cid in spans:
                if start <= blob_line <= end:
                    return cid, start
            return None, None

        for d in raw:
            if not isinstance(d, dict):
                continue
            loc = d.get("location") or {}
            end_loc = d.get("end_location") or {}
            try:
                blob_line = int(loc.get("row"))
                col = int(loc.get("column"))
            except (TypeError, ValueError):
                continue
            cid, start = locate(blob_line)
            if cid is None:
                continue  # separator line or out of range → not a user cell

            entry = {
                "line": blob_line - start + 1,  # rebase to the cell
                "col": col,
                "code": d.get("code") or "",
                "message": d.get("message") or "",
                "severity": "warning",  # ruff findings are lint-level; editor styles them uniformly
                "fixable": bool((d.get("fix") or {}).get("applicability")),
            }
            # Rebase the end position too when it stays inside THIS cell's span, so
            # the editor can underline an exact range; otherwise omit it and the
            # editor falls back to marking just the start token.
            try:
                end_line = int(end_loc.get("row"))
                end_col = int(end_loc.get("column"))
            except (TypeError, ValueError):
                end_line = end_col = None
            if end_line is not None:
                span_end = next((e for s, e, c in spans if c == cid), None)
                if span_end is not None and blob_line <= end_line <= span_end:
                    entry["end_line"] = end_line - start + 1
                    entry["end_col"] = end_col
            out.setdefault(cid, []).append(entry)

    # NOTE: Notebook FORMAT (Python via ruff, SQL via sqlglot) moved to the PROXY
    # (proxy/notebook/format_engine.py + POST /format). Formatting is a pure text
    # transform that needs no kernel, so it no longer runs in the VM. LINT stays here
    # (it's namespace-aware). ruff + sqlglot remain in the VM's requirements.txt for
    # lint. _normalize_cells and _mask_sql_placeholders are used by lint_notebook /
    # _lint_sql_cell.

    def get_variables(self, redact_secrets: bool = False, cell_ids=None) -> dict[str, dict]:
        """
        Return all user-defined variables with type, preview, and metadata.

        Provides rich info for the variable explorer:
        - type: Python type name
        - value: short repr (truncated to 100 chars)
        - size: human-readable memory size
        - shape: for DataFrames/arrays/lists (e.g. "500 rows × 8 cols")
        - preview: first few items or .head() for collections

        redact_secrets=True (GET /variables/redacted — the AI agents' view) masks any
        configured secret's value in `value`/`preview`. The Variables panel calls the
        plain form: it shows the user their own values.

        Every entry carries `orphan` / `orphan_reason` (var_inspect.classify_origin:
        no cell produces it); an orphan's `defined_by` is null. cell_ids: the DAG
        document's cell ids (None = unknown, never "deleted cell").
        """
        variables = self._get_variables(cell_ids=cell_ids)
        if redact_secrets:
            self._redact_entries(variables)
        return variables

    @staticmethod
    def _redact_entries(variables: dict) -> None:
        """Mask configured secret values in each entry's value/preview (in place)."""
        from . import var_inspect
        secrets = var_inspect.secret_values()
        if secrets:
            for entry in variables.values():
                for key in ("value", "preview"):
                    if isinstance(entry.get(key), str):
                        entry[key] = var_inspect.redact_text(entry[key], secrets)

    def get_variable_schemas(self, cell_ids=None) -> dict:
        """Live schema of every user variable (GET /variables/schema): columns/dtypes
        for pandas/numpy containers, type only for anything else. Read-only — no user
        repr()/__getattr__ runs (see var_inspect). Entries carry orphan fields."""
        from . import var_inspect
        return var_inspect.build_schema(
            list(self._namespace.items()), dict(self._var_provenance),
            origin_of=lambda n, p: var_inspect.classify_origin(n, p, cell_ids))

    def inspect_variable(self, name, sample_rows=3, scope=None, cell_ids=None) -> tuple[int, dict]:
        """One variable's schema + up to 3 safe sample rows (POST /variables/inspect).
        Returns (http_status, body). With a valid exploration `scope`, a name in that
        scope's scratch is resolved first and reported with "scratch": true."""
        from . import var_inspect
        if not var_inspect.validate_name(name):
            return 400, {"error": "invalid variable name"}
        secrets = var_inspect.secret_values()
        if valid_scope(scope):
            with self._exec_lock:
                sc = self._scratch.peek(scope)
                found = sc is not None and name in sc
                obj = sc[name] if found else None
            if found and var_inspect.is_user_variable(name, obj):
                body = var_inspect.inspect_entry(name, obj, None, sample_rows, secrets,
                                                 origin=(False, None))
                body["scratch"] = True
                return 200, body
        ns = self._namespace
        if name not in ns:
            return 404, {"error": "variable not found"}
        obj = ns[name]
        if not var_inspect.is_user_variable(name, obj):
            return 404, {"error": "variable not found"}
        prov = self._var_provenance.get(name)
        body = var_inspect.inspect_entry(name, obj, prov, sample_rows, secrets,
                                         origin=var_inspect.classify_origin(name, prov, cell_ids))
        return 200, body

    # ---- Agent exploration scratch (see app/notebook/scratch.py) ----

    def scratch_scope(self, scope: str) -> dict:
        """The scope's scratch dict (created on demand) for execute(scratch=...)."""
        with self._exec_lock:
            return self._scratch.get(scope)

    def clear_scratch(self, scope: str) -> bool:
        """Drop one exploration scope. True when it existed."""
        with self._exec_lock:
            return self._scratch.clear(scope)

    def scratch_variables(self, scope: str, redact_secrets: bool = True) -> dict[str, dict]:
        """get_variables-style entries for the names in one scope's scratch, each
        with "scratch": true (and orphan false: it is not a notebook variable)."""
        with self._exec_lock:
            sc = self._scratch.peek(scope)
            items = list(sc.items()) if sc else []
        variables = self._get_variables(items=items, scratch=True)
        if redact_secrets:
            self._redact_entries(variables)
        return variables

    # ---- Untracked (orphan) variables: listing + user-confirmed clean-up ----

    def _untracked_locked(self, cell_ids) -> tuple[dict, list]:
        """({visible name: reason}, [internal names]). Caller holds _exec_lock."""
        from . import var_inspect
        injected = self._injected_helpers()
        visible: dict = {}
        internal: list = []
        for name, obj in list(self._namespace.items()):
            if not isinstance(name, str) or (name.startswith("__") and name.endswith("__")):
                continue
            prov = self._var_provenance.get(name)
            if name.startswith("_"):
                if isinstance(obj, types.ModuleType) or (name in injected and obj is injected[name]):
                    continue
                if isinstance(prov, dict) and prov.get("defined_by") == "":
                    internal.append(name)
                elif name.startswith("__upload_"):
                    internal.append(name)
                continue
            if not var_inspect.is_user_variable(name, obj):
                continue
            orphan, reason = var_inspect.classify_origin(name, prov, cell_ids)
            if orphan:
                visible[name] = reason
        # Provenance-only ghosts of the old upload probe (no namespace binding).
        for name in list(self._var_provenance.keys()):
            if isinstance(name, str) and name.startswith("__upload_") and name not in internal:
                internal.append(name)
        return visible, internal

    def untracked(self, cell_ids=None) -> dict:
        """GET /variables/untracked: {"visible": [{name, reason}], "internal": [names]}.
        visible = user variables no cell produces; internal = `_`-prefixed leftovers
        of system probes (no cell id) and `__upload_*` provenance ghosts."""
        with self._exec_lock:
            visible, internal = self._untracked_locked(cell_ids)
        return {"visible": [{"name": n, "reason": r} for n, r in visible.items()],
                "internal": internal}

    def delete_untracked(self, names, cell_ids=None) -> dict:
        """Delete the given names, but ONLY those still untracked right now (a name
        a cell produces is skipped). Pops the binding and its provenance; advances
        _prov_clock once when anything was deleted so the checkpoint re-saves.
        Returns {"deleted": [...], "skipped": [...]}."""
        deleted: list = []
        skipped: list = []
        with self._exec_lock:
            visible, internal = self._untracked_locked(cell_ids)
            allowed = set(visible) | set(internal)
            for name in names:
                if not isinstance(name, str) or name not in allowed or name in deleted:
                    if name not in skipped:
                        skipped.append(name)
                    continue
                self._namespace.pop(name, None)
                self._var_provenance.pop(name, None)
                deleted.append(name)
            if deleted:
                self._prov_clock += 1
        return {"deleted": deleted, "skipped": skipped}

    def _get_variables(self, items=None, cell_ids=None, scratch: bool = False) -> dict[str, dict]:
        """get_variables() body (unredacted). items: (name, value) pairs to format
        (default: the user namespace). scratch=True formats exploration-scratch
        entries: no provenance, orphan false, "scratch": true."""
        from . import var_inspect
        # Keep the filters in sync with _count_user_variables() (get_stats uses it).
        variables = {}
        if items is None:
            items = list(self._namespace.items())  # list() = snapshot to avoid mutation during iteration
        for name, value in items:
            if name.startswith("__") and name.endswith("__"):
                continue
            if name.startswith("_"):
                continue  # skip private/internal vars (display, _auto_display, etc.)
            # Skip modules, functions, classes
            try:
                type_name = type(value).__name__
                module = getattr(type(value), '__module__', '') or ''
            except Exception:
                continue
            if type_name in ('module', 'function', 'builtin_function_or_method', 'type'):
                continue

            # NumPy SCALARS (np.int64, np.float64, np.bool_, np.datetime64, a 0-d
            # ndarray, etc.) are conceptually plain scalars, but type(value).__name__
            # is "int64"/"float64"/… which the UI doesn't recognize — so they wrongly
            # landed in the "Other" group with a "shape ()" label. Normalize them to
            # their Python-scalar type name so they group as Scalars, show a real
            # value, and carry no bogus shape. Detected via numpy's `ndim == 0`.
            is_numpy_scalar = False
            if 'numpy' in module:
                try:
                    if getattr(value, 'ndim', None) == 0:
                        is_numpy_scalar = True
                        py = getattr(value, 'item', lambda: value)()
                        # Map to the closest Python scalar type name for grouping/icon.
                        if isinstance(py, bool):
                            type_name = 'bool'
                        elif isinstance(py, int):
                            type_name = 'int'
                        elif isinstance(py, float):
                            type_name = 'float'
                        elif isinstance(py, complex):
                            type_name = 'complex'
                        elif isinstance(py, str):
                            type_name = 'str'
                        # else (e.g. datetime64) — leave the numpy name; it still
                        # won't get a shape, and the value repr below is clean.
                except Exception:
                    pass

            try:
                # Basic repr (short) — with safety timeout
                try:
                    # For a numpy scalar, repr(value) is the ugly "np.float64(1017.67)".
                    # Show the plain Python value ("1017.67") instead.
                    val_repr = repr(value.item()) if is_numpy_scalar else repr(value)
                    if len(val_repr) > 100:
                        val_repr = val_repr[:100] + "..."
                except Exception:
                    val_repr = f"<{type_name}>"

                # Size
                try:
                    size_bytes = sys.getsizeof(value)
                    if size_bytes < 1024:
                        size_str = f"{size_bytes} B"
                    elif size_bytes < 1024 * 1024:
                        size_str = f"{size_bytes / 1024:.1f} KB"
                    else:
                        size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                except Exception:
                    size_str = ""

                # Shape / length
                shape = ""
                preview = val_repr
                preview_type = "text"  # always "text" now (tabular types use the grid)
                # DataFrame/Series: only the shape label is used by the UI. The
                # Variables panel renders tabular types via their column schema +
                # the "View data" grid (fetched lazily from /variable-detail), so we
                # deliberately DON'T build an inline HTML preview here — it was
                # discarded client-side (VariablePreviewRenderer skips tabular types).
                if 'pandas' in module and type_name == 'DataFrame':
                    try:
                        shape = f"{value.shape[0]} rows × {value.shape[1]} cols"
                    except Exception:
                        shape = "DataFrame"
                elif 'pandas' in module and type_name == 'Series':
                    try:
                        shape = f"{len(value)} items"
                    except Exception:
                        shape = "Series"
                elif 'numpy' in module and hasattr(value, 'shape') and not is_numpy_scalar:
                    # Real arrays get a shape; 0-d numpy scalars do NOT (they're scalars).
                    shape = f"shape {value.shape}"
                elif isinstance(value, (list, tuple)):
                    shape = f"{len(value)} items"
                    if len(value) > 5:
                        preview = repr(value[:5])[:-1] + ", ...]"
                        if len(preview) > 100:
                            preview = preview[:100] + "..."
                elif isinstance(value, dict):
                    shape = f"{len(value)} keys"
                    if len(value) > 3:
                        keys = list(value.keys())[:3]
                        preview = "{" + ", ".join(f"{repr(k)}: ..." for k in keys) + ", ...}"
                elif isinstance(value, str) and len(value) > 50:
                    shape = f"{len(value)} chars"

                prov = None if scratch else self._var_provenance.get(name)
                if scratch:
                    orphan, reason = False, None
                else:
                    orphan, reason = var_inspect.classify_origin(name, prov, cell_ids)
                variables[name] = {
                    "type": type_name,
                    "value": val_repr,
                    "size": size_str,
                    "shape": shape,
                    "preview": preview,
                    "preview_type": preview_type,
                    "defined_by": prov.get("defined_by") if prov and not orphan else None,
                    "defined_at": prov.get("defined_at") if prov else None,
                    "modified_by": prov.get("modified_by", []) if prov else [],
                    "last_cell": prov.get("last_cell") if prov else None,
                    "last_exec": prov.get("last_exec") if prov else None,
                    # No cell produces it (AI exploration / deleted cell / unknown).
                    "orphan": orphan,
                    "orphan_reason": reason,
                }
                if scratch:
                    variables[name]["scratch"] = True
            except Exception:
                variables[name] = {
                    "type": type_name,
                    "value": "<unable to inspect>",
                    "size": "",
                    "shape": "",
                    "preview": "",
                }
        return variables

    def _count_user_variables(self) -> int:
        """Count the variables get_variables() would return, without building them.

        get_stats() only needs the COUNT, so this skips the repr / sys.getsizeof /
        shape work (which can be very slow for large objects). Keep the filters in
        sync with get_variables(): every name that survives these skip rules always
        yields an entry there (including the "<unable to inspect>" fallback).
        """
        count = 0
        for name, value in list(self._namespace.items()):  # snapshot, same as get_variables
            if name.startswith("__") and name.endswith("__"):
                continue
            if name.startswith("_"):
                continue
            try:
                type_name = type(value).__name__
                getattr(type(value), '__module__', '')
            except Exception:
                continue
            if type_name in ('module', 'function', 'builtin_function_or_method', 'type'):
                continue
            count += 1
        return count

    def reset(self):
        """Clear the namespace — fresh sandbox state."""
        self._namespace.clear()
        self._execution_count = 0
        self._history.clear()
        with self._exec_lock:
            self._var_provenance.clear()
            self._prov_clock = 0
            self._runs.clear()
            self._scratch.clear_all()
        self._imports.clear()

    def get_stats(self) -> dict:
        """Return executor statistics."""
        return {
            "execution_count": self._execution_count,
            "variables_count": self._count_user_variables(),
            "created_at": self._created_at.isoformat(),
            "history_length": len(self._history),
        }
