#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: /execute log lines carry an origin tag and a scratch label
(app/notebook/code_engine.py, live-check-2 F2).
Covers: a user cell logs `▶ Executing code` / `✓ OK … vars=[…]` (unchanged);
an agent run logs `[agent]` + `scratch_vars=`; an internal run (profiler, schema
discovery) logs `[internal]` + `scratch_vars=` on the ▶, ✓ and ✗ lines and leaves
nothing in /variables; a non-str or unknown origin is untagged and does not raise;
`_log_execute_outcome` labels vars/scratch_vars directly.
Run with python3 (needs pandas + fastapi):
    python3 tests/vm/test_internal_log_tag.py
Exits non-zero on any failure.
"""
import logging
import os
import sys
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")
# Stub plotly when absent (same as tests/vm/test_agent_scratch_exec.py).
try:
    import plotly  # noqa: F401
except ImportError:
    _plotly = types.ModuleType("plotly")
    _px = types.ModuleType("plotly.express")
    _plotly.express = _px
    sys.modules.setdefault("plotly", _plotly)
    sys.modules.setdefault("plotly.express", _px)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.notebook import code_engine, routes  # noqa: E402
from app.notebook.dag_store import DagStore  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class _Catalog:
    def refresh_local_files(self):
        pass


def _run():
    log = logging.getLogger("app.notebook.code_engine")
    h = _ListHandler()
    log.addHandler(h)
    prev_level = log.level
    log.setLevel(logging.INFO)
    try:
        ex = SandboxExecutor()
        ex.execute("x = 10", cell_id="c-x")
        app = FastAPI()
        app.include_router(code_engine.router)
        app.include_router(routes.router)
        app.state.executor = ex
        app.state.session_state = {"request_count": 0}
        app.state.dag_store = DagStore()
        app.state.data_catalog = _Catalog()
        client = TestClient(app)

        def run(body):
            h.lines.clear()
            r = client.post("/execute", json=body)
            return r, list(h.lines)

        # user cell: unchanged
        r, lines = run({"code": "u = 1"})
        joined = "\n".join(lines)
        check("user: ▶ Executing code (untagged)", any(ln.startswith("▶ Executing code") for ln in lines), joined)
        check("user: ✓ OK … vars=['u']", any("✓ OK" in ln and " vars=['u']" in ln for ln in lines), joined)
        check("user: no [agent]/[internal]/scratch_vars",
              "[agent]" not in joined and "[internal]" not in joined and "scratch_vars" not in joined, joined)

        # agent run
        r, lines = run({"code": "a = 1", "origin": "agent", "agent_scope": "chat:t"})
        joined = "\n".join(lines)
        check("agent: ▶ [agent] Executing code", any(ln.startswith("▶ [agent] Executing code") for ln in lines), joined)
        check("agent: ✓ [agent] OK … scratch_vars=['a']",
              any("✓ [agent] OK" in ln and " scratch_vars=['a']" in ln for ln in lines), joined)
        check("agent: no bare ' vars=['", not any(" vars=[" in ln for ln in lines), joined)

        # internal run (profiler-style)
        r, lines = run({"code": "df = 1", "origin": "internal"})
        joined = "\n".join(lines)
        check("internal: 200", r.status_code == 200, str(r.status_code))
        check("internal: ▶ [internal] Executing code",
              any(ln.startswith("▶ [internal] Executing code") for ln in lines), joined)
        check("internal: ✓ [internal] OK … scratch_vars=['df']",
              any("✓ [internal] OK" in ln and " scratch_vars=['df']" in ln for ln in lines), joined)
        vs = client.get("/variables").json()["variables"]
        check("internal: df not in /variables (scratch)", "df" not in vs and "x" in vs, str(list(vs)))

        # internal error
        r, lines = run({"code": "{}['k']", "origin": "internal"})
        joined = "\n".join(lines)
        check("internal error: ✗ [internal] ERROR", any("✗ [internal] ERROR" in ln for ln in lines), joined)
        check("internal error: error line starts '    [internal] '",
              any(ln.startswith("    [internal] ") for ln in lines), joined)

        # non-str / unknown origin: untagged, no raise
        for bad in (["x"], "other"):
            r, lines = run({"code": "b = 2", "origin": bad})
            joined = "\n".join(lines)
            check(f"origin={bad!r}: 200 and untagged",
                  r.status_code == 200 and "[agent]" not in joined and "[internal]" not in joined
                  and any(ln.startswith("▶ Executing code") for ln in lines), joined)

        # direct helper
        res = SimpleNamespace(success=True, variables_created=["z"], execution_time_ms=1,
                              peak_mem_mb=0, peak_cpu_pct=0, variables_read=[], error=None)
        h.lines.clear()
        code_engine._log_execute_outcome(res, "z=1")
        check("helper: default logs ' vars=['z']'", any(" vars=['z']" in ln for ln in h.lines), str(h.lines))
        h.lines.clear()
        code_engine._log_execute_outcome(res, "z=1", "[internal] ", scratch=True)
        check("helper: scratch=True logs ' scratch_vars=['z']'",
              any("✓ [internal] OK" in ln and " scratch_vars=['z']" in ln for ln in h.lines), str(h.lines))
    finally:
        log.removeHandler(h)
        log.setLevel(prev_level)


def test_main():
    _fails.clear()
    _run()
    assert not _fails, _fails


if __name__ == "__main__":
    _run()
    print()
    if _fails:
        print(f"FAILED: {len(_fails)}")
        for f in _fails:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
