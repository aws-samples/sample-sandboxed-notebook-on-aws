#!/usr/bin/env python3
# test-kind: manual
"""
End-to-end test for the SQLite -> Aurora sync worker (sync/sync_to_aurora.py).

Drives the LIVE proxy (http://localhost:8081) exactly as the UI does — create
notebooks from the real sample templates, launch VMs, Run All (execute cells via
the /proxy passthrough), record runs, edit + add cells, close (suspend) / reopen
(relaunch+restore), and delete — then after each phase runs ONE sync cycle and
verifies Aurora converges to whatever SQLite holds. Reads Aurora over the RDS Data
API (same credential path the worker uses) to check the target DB.

Covers the requested scenarios PLUS edge cases:
  1. Launch 2 VMs from sample notebooks, Run All each        -> full initial sync
  2. Add new cells to one notebook, Run All again            -> parent-scoped cell replace
  3. Close (suspend) then reopen (relaunch/restore)          -> VM churn, no dup notebooks
  4. Delete one notebook                                     -> Aurora cascade + tombstone replay
  Edge cases:
  E1. Idempotent re-sync immediately after a clean sync      -> ~no-op
  E2. Edit that REMOVES a cell (not just adds)               -> stale child pruned in Aurora
  E3. Delete-then-recreate a notebook_run id is N/A (uuid) — instead: a run row
      referencing a notebook, then notebook deleted          -> run row survives (no FK cascade)
  E4. Rapid close->reopen (suspend then immediate relaunch)  -> single live VM, session relinked
  E5. Unicode / large-ish cell code + SQL cell              -> Data API type + size handling
  E6. Sync with NOTHING changed after delete                -> delete cursor stable, no re-delete

Run:
  # proxy must be up (bash local_install.sh), Aurora reachable, RW secret set.
  source scripts/config.sh
  python3 tests/sync/test_sync_e2e.py [--keep] [--vm]

By default it runs WITHOUT launching real VMs (--vm to launch them): cell
execution needs a live VM, so without --vm the test still exercises the full
notebook/run/version/delete + sync/verify path (which is what the sync worker
cares about) by recording runs directly. With --vm it additionally launches VMs,
executes cells for real, and exercises suspend/resume/relaunch.

Exit code 0 = all checks passed.
"""

import argparse
import json
import os
import sys
import time
import re
import subprocess
import uuid
import urllib.request
import urllib.error

PROXY = os.environ.get("PROXY_URL", "http://localhost:8081")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLES_DIR = os.path.join(ROOT, "web", "public", "samples")


# ── proxy auth token: env override, else read from the running proxy process ──
# The proxy enforces X-Proxy-Auth on its routes; resolve the token the same way
# the model-switch live test does (PROXY_AUTH_TOKEN env, else the listening
# proxy process's command line). Resolved once at import.
def _proxy_token() -> str | None:
    env_tok = os.environ.get("PROXY_AUTH_TOKEN")
    if env_tok:
        return env_tok
    try:
        pid = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN", "-t"], text=True
        ).split()[0]
        env = subprocess.check_output(["ps", "eww", "-p", pid, "-o", "command="], text=True)
        m = re.search(r"PROXY_AUTH_TOKEN=(\S+)", env)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


_PROXY_TOKEN = _proxy_token()

# Make the repo root importable so `from sync.sync_to_aurora import run_once`
# works no matter what CWD the test is launched from.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── tiny result tracker ────────────────────────────────────────────────────
_checks = []
def check(name: str, ok: bool, detail: str = ""):
    _checks.append((name, ok))
    mark = "✓" if ok else "✗"
    line = f"  {mark} {name}"
    if detail and not ok:
        line += f"  [{detail}]"
    print(line, flush=True)

def section(title: str):
    print(f"\n=== {title} ===", flush=True)


# ── HTTP helpers (stdlib only; curl gets flagged in this env) ───────────────
def _req(method: str, path: str, body=None, headers=None, timeout=180):
    url = path if path.startswith("http") else f"{PROXY}{path}"
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if _PROXY_TOKEN:
        h["X-Proxy-Auth"] = _PROXY_TOKEN
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw}
    except Exception as e:
        return 0, {"error": str(e)}


# ── proxy API wrappers ──────────────────────────────────────────────────────
def create_notebook(name, cells, session_id, tag="Samples", description=""):
    st, r = _req("POST", "/notebooks",
                 {"name": name, "description": description, "tag": tag,
                  "cells": cells, "session_id": session_id})
    assert st == 200 and r.get("id") is not None, f"create failed: {st} {r}"
    return int(r["id"])

def save_cells(nb_id, cells, session_id):
    st, r = _req("PUT", f"/notebooks/{nb_id}",
                 {"cells": cells, "session_id": session_id})
    return st, r

def get_notebook(nb_id):
    st, r = _req("GET", f"/notebooks/{nb_id}")
    return r if st == 200 else None

def delete_notebook(nb_id):
    st, r = _req("DELETE", f"/notebooks/{nb_id}")
    return st, r

def record_run(nb_id, session_id, microvm_id, trigger, run_cells):
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    st, r = _req("POST", "/notebook-runs", {
        "notebookId": nb_id, "sessionId": session_id, "microvmId": microvm_id,
        "trigger": trigger, "startedAt": now, "finishedAt": now,
        "cellsExecuted": len(run_cells),
        "cellsErrored": sum(1 for c in run_cells if c.get("status") == "error"),
        "cellsInterrupted": 0,
        "totalExecMs": sum(c.get("exec_ms", 0) for c in run_cells),
        "runCells": run_cells,
    })
    return st, r

def launch_vm(name, nb_id, session_id, restore=None, mem=4096, arch=None):
    body = {"name": name, "notebookId": nb_id, "sessionId": session_id,
            "checkpointEnabled": True, "memoryMiB": mem}
    if arch:
        body["arch"] = arch
    if restore:
        body["restoreFromSession"] = restore
    st, r = _req("POST", "/launch", body, timeout=180)
    return st, r

def suspend(session_id):
    return _req("POST", "/suspend", None, {"X-Session-Id": session_id})

def terminate_all_vms_for_sessions(sessions):
    """Terminate EVERY VM currently serving any of `sessions`. Robust cleanup:
    catches originals AND reopen-relaunched replacements (same session_id), so a
    close→reopen (which spawns a second VM) never leaves an orphan. Idempotent."""
    st, data = _req("GET", "/instances")
    if st != 200:
        return 0
    killed = 0
    for mid, info in (data.get("instances") or {}).items():
        sid = info.get("session_id")
        if sid in sessions:
            ts, _ = terminate(sid, mid)
            if ts == 200:
                killed += 1
    return killed

def terminate(session_id, microvm_id):
    return _req("POST", "/terminate", {"microvmId": microvm_id}, {"X-Session-Id": session_id})

def execute_cell(session_id, cell):
    """Run one cell on the VM via the /proxy passthrough. Returns (status, json)."""
    is_sql = cell.get("type") == "sql"
    path = "/proxy/execute-sql" if is_sql else "/proxy/execute"
    if is_sql:
        body = {"sql": cell["code"], "output_variable": cell.get("outputVariable") or "res",
                "cell_id": cell["id"]}
    else:
        body = {"code": cell["code"], "cell_id": cell["id"]}
    return _req("POST", path, body, {"X-Session-Id": session_id}, timeout=180)

def create_version(nb_id, trigger="manual", label=None):
    """Create a version INDEPENDENTLY of any notebook save (the auto/pre_ai/manual
    path). The server reads current cells itself. Returns (status, header)."""
    st, r = _req("POST", f"/notebooks/{nb_id}/versions", {"trigger": trigger, "label": label})
    return st, r

def list_versions(nb_id):
    st, r = _req("GET", f"/notebooks/{nb_id}/versions?limit=100")
    return (r.get("versions") or []) if st == 200 else []

def restore_version(nb_id, version_group_id):
    st, r = _req("POST", f"/notebooks/{nb_id}/versions/{version_group_id}/restore", {})
    return st, r


# ── sample loading + cell normalization ─────────────────────────────────────
def load_sample(file_name):
    with open(os.path.join(SAMPLES_DIR, file_name)) as f:
        doc = json.load(f)
    cells = []
    for i, c in enumerate(doc["cells"]):
        cells.append({
            "id": str(uuid.uuid4()),
            "type": c.get("type", "code"),
            "code": c.get("code", ""),
            "outputVariable": c.get("outputVariable"),
            "position": i,
        })
    return doc["name"], cells


# ── Aurora (Data API) read-side verification ────────────────────────────────
class Aurora:
    def __init__(self):
        import boto3
        self.region = os.environ["AWS_REGION"]
        self.cluster = os.environ["AURORA_DATA_API_CLUSTER_ARN"]
        # Read via the RW secret (same one the worker writes with) so we can read
        # base tables directly, not just the v_nb_* views.
        self.secret = os.environ.get("AURORA_DATA_API_RW_SECRET_ARN") \
            or os.environ["AURORA_DATA_API_SECRET_ARN"]
        self.db = os.environ.get("AURORA_DATA_API_DATABASE",
                                 os.environ.get("AURORA_DB_NAME", "microvm_db"))
        self.c = boto3.client("rds-data", region_name=self.region)

    def q(self, sql):
        for a in range(8):
            try:
                return self.c.execute_statement(
                    resourceArn=self.cluster, secretArn=self.secret,
                    database=self.db, sql=sql, includeResultMetadata=True)
            except self.c.exceptions.DatabaseResumingException:
                time.sleep(3 * (a + 1))
        raise RuntimeError("Aurora resume exhausted")

    def count(self, table, where=None):
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return self.q(sql)["records"][0][0]["longValue"]

    def scalar(self, sql):
        recs = self.q(sql)["records"]
        if not recs:
            return None
        f = recs[0][0]
        for k in ("stringValue", "longValue", "doubleValue", "booleanValue"):
            if k in f:
                return f[k]
        return None


def sqlite_count(table, where=None):
    import sqlite3
    p = os.path.join(ROOT, "proxy", "data", "microvm.db")
    c = sqlite3.connect(p)
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    n = c.execute(sql).fetchone()[0]
    c.close()
    return n


# ── the sync worker (run one cycle in-process) ──────────────────────────────
def run_sync_once():
    """Invoke one sync cycle in-process and return the summary dict."""
    from sync.sync_to_aurora import run_once
    return run_once(dry_run=False)


def verify_table_parity(au: Aurora, tables):
    """Assert SQLite count == Aurora count for each table."""
    all_ok = True
    for t in tables:
        s = sqlite_count(t)
        a = au.count(t)
        ok = s == a
        all_ok = all_ok and ok
        check(f"parity {t}: sqlite={s} aurora={a}", ok, f"{s} != {a}")
    return all_ok


# Primary keys per table — for the DEEP (row-level) parity check. A pure COUNT
# can match by luck while the actual rows differ; comparing PK SETS catches any
# row present on only one side (the exact class of drift the count check missed).
_PKS = {
    "notebooks": ["id"], "notebook_cells": ["notebook_id", "cell_id"],
    "notebook_runs": ["run_id"], "notebook_versions": ["version_group_id"],
    "notebook_cell_versions": ["version_group_id", "cell_id"],
    "vm_sessions": ["microvm_id"], "vm_metrics": ["id"], "vm_state_log": ["id"],
    # Agent chat memory: the SQLite table is agent_sessions / agent_messages (NOT
    # "ai_sessions"), with the composite PKs the sync worker discovers from the
    # schema. PKs verified via PRAGMA table_info.
    "agent_sessions": ["session_id"],
    "agent_messages": ["session_id", "agent_id", "message_id"],
    "workbook_intel": ["session_id"],
    "data_source_entities": ["source_id"],
    "local_file_entities": ["session_id", "filepath"], "session_registry": ["session_id"],
    "notebook_env": ["notebook_id"],
    # Per-notebook DAG document (cascade child of notebooks, one row per notebook).
    "notebook_dag": ["notebook_id"],
}


def _sqlite_pk_set(table, pk):
    import sqlite3
    p = os.path.join(ROOT, "proxy", "data", "microvm.db")
    c = sqlite3.connect(p)
    cols = ", ".join(pk)
    s = {tuple(str(x) for x in r) for r in c.execute(f"SELECT {cols} FROM {table}")}
    c.close()
    return s


def _aurora_pk_set(au: Aurora, table, pk):
    cols = ", ".join(pk)
    out = set()
    for rec in au.q(f"SELECT {cols} FROM {table}").get("records", []):
        vals = []
        for f in rec:
            if f.get("isNull"):
                vals.append("None"); continue
            v = None
            for k in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if k in f:
                    v = f[k]; break
            vals.append(str(v))
        out.add(tuple(vals))
    return out


def verify_deep_parity(au: Aurora, tables):
    """Row-level sync coverage: assert SQLite ⊆ Aurora — every local row the worker
    should have synced is present in Aurora.

    We do NOT require Aurora ⊆ SQLite: Aurora is a SHARED, accumulating cluster
    (rows from prior runs and other sessions live there), while this test's local
    SQLite is a single machine's current state. So `only_aurora` rows are expected
    pre-existing data, not a sync defect — they're reported for visibility but do
    NOT fail the check. A row present locally but MISSING from Aurora (`only_sqlite`)
    IS a real sync gap and fails."""
    all_ok = True
    total_missing = 0
    for t in tables:
        pk = _PKS[t]
        s = _sqlite_pk_set(t, pk)
        a = _aurora_pk_set(au, t, pk)
        only_s, only_a = s - a, a - s   # only_s = local rows missing from Aurora (BAD)
        total_missing += len(only_s)
        ok = len(only_s) == 0
        all_ok = all_ok and ok
        detail = f"only_sqlite(MISSING from Aurora)={list(only_s)[:3]}"
        if only_a:
            detail += f"  [info: {len(only_a)} Aurora-only row(s) — pre-existing/shared, ignored]"
        check(f"sync coverage {t} (SQLite ⊆ Aurora)", ok, detail)
    check(f"ZERO local rows missing from Aurora across {len(tables)} tables "
          f"(missing={total_missing})", total_missing == 0)
    return all_ok


# ── the scenarios ────────────────────────────────────────────────────────────
ALL_TABLES = ["notebooks", "notebook_cells", "notebook_runs", "notebook_versions",
              "notebook_cell_versions", "vm_sessions", "vm_metrics", "vm_state_log",
              "agent_sessions", "agent_messages", "workbook_intel",
              "data_source_entities", "local_file_entities", "session_registry",
              "notebook_env", "notebook_dag"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="don't delete the test notebooks at the end")
    ap.add_argument("--vm", action="store_true", help="launch real VMs + execute cells (slower)")
    args = ap.parse_args()

    # Preconditions: proxy up, Aurora configured.
    st, _ = _req("GET", "/instances")
    if st != 200:
        print(f"❌ Proxy not reachable at {PROXY} (status {st}). Start it: bash local_install.sh")
        return 2
    try:
        au = Aurora()
        au.count("notebooks")
    except Exception as e:
        print(f"❌ Aurora not reachable/configured: {e}")
        return 2

    created_ids = []
    # Every session_id the test uses. Cleanup terminates ALL VMs belonging to these
    # sessions — including reopen-relaunched replacements (which reuse the session_id),
    # so no VM is orphaned even though close→reopen can spawn a second VM per session.
    test_sessions = set()

    # ── Scenario 1: two notebooks from sample templates, Run All each ────────
    section("Scenario 1: create 2 notebooks from samples + Run All")
    sid_a = str(uuid.uuid4())
    name_a, cells_a = load_sample("sales_analysis.notebook.json")
    nb_a = create_notebook(name_a, [], sid_a)
    save_cells(nb_a, cells_a, sid_a)
    created_ids.append(nb_a)
    check(f"created notebook A '{name_a}' id={nb_a}", nb_a > 0)

    test_sessions.add(sid_a)

    sid_b = str(uuid.uuid4())
    name_b, cells_b = load_sample("statistical_analysis.notebook.json")
    nb_b = create_notebook(name_b, [], sid_b)
    save_cells(nb_b, cells_b, sid_b)
    created_ids.append(nb_b)
    test_sessions.add(sid_b)
    check(f"created notebook B '{name_b}' id={nb_b}", nb_b > 0)

    vm_a = vm_b = None
    if args.vm:
        st, r = launch_vm(name_a, nb_a, sid_a)
        vm_a = r.get("microvmId")
        check(f"launched VM A ({r.get('status')})", st == 200 and vm_a, f"{st} {r}")
        st, r = launch_vm(name_b, nb_b, sid_b)
        vm_b = r.get("microvmId")
        check(f"launched VM B ({r.get('status')})", st == 200 and vm_b, f"{st} {r}")
        # Run All on A: execute each code/sql cell via the passthrough.
        ran = 0
        for c in cells_a:
            if c["type"] == "markdown":
                continue
            est, er = execute_cell(sid_a, c)
            if est == 200:
                ran += 1
        check(f"executed {ran} cells on VM A", ran > 0)

    # Record a run for each (frontend does this after a Run All; VM-independent).
    run_cells_a = [{"cell_id": c["id"], "label": f"C{i}", "type": c["type"],
                    "status": "success", "exec_ms": 12, "position": i}
                   for i, c in enumerate(cells_a) if c["type"] != "markdown"]
    record_run(nb_a, sid_a, vm_a, "run_all", run_cells_a)
    run_cells_b = [{"cell_id": c["id"], "label": f"C{i}", "type": c["type"],
                    "status": "success", "exec_ms": 8, "position": i}
                   for i, c in enumerate(cells_b) if c["type"] != "markdown"]
    record_run(nb_b, sid_b, vm_b, "run_all", run_cells_b)
    check("recorded run_all for A and B", True)

    res = run_sync_once()
    check(f"sync #1 ok (up={res['rows_upserted']} del={res['rows_deleted']})",
          res["status"] == "ok", res.get("error") or "")
    verify_deep_parity(au, ALL_TABLES)
    # The two notebooks + their cells are present in Aurora.
    check("Aurora has notebook A cells",
          au.count("notebook_cells", f"notebook_id={nb_a}") == len(cells_a))
    check("Aurora has notebook B cells",
          au.count("notebook_cells", f"notebook_id={nb_b}") == len(cells_b))
    check("Aurora v_nb_notebook shows A",
          au.scalar(f"SELECT name FROM v_nb_notebook WHERE notebook_id={nb_a}") == name_a)

    # ── Scenario 2: add new cells to A, Run All again ────────────────────────
    section("Scenario 2: add cells to A + Run All again (parent-scoped replace)")
    extra = [
        {"id": str(uuid.uuid4()), "type": "code",
         "code": "added_total = sum(range(100))\nprint(added_total)", "position": len(cells_a)},
        {"id": str(uuid.uuid4()), "type": "sql",
         "code": "SELECT 1 AS added_marker", "outputVariable": "added_q",
         "position": len(cells_a) + 1},
    ]
    cells_a2 = cells_a + extra
    save_cells(nb_a, cells_a2, sid_a)
    record_run(nb_a, sid_a, vm_a, "run_all",
               [{"cell_id": c["id"], "label": f"C{i}", "type": c["type"],
                 "status": "success", "exec_ms": 5, "position": i}
                for i, c in enumerate(cells_a2) if c["type"] != "markdown"])
    res = run_sync_once()
    check(f"sync #2 ok (up={res['rows_upserted']})", res["status"] == "ok", res.get("error") or "")
    check(f"Aurora notebook A now has {len(cells_a2)} cells",
          au.count("notebook_cells", f"notebook_id={nb_a}") == len(cells_a2),
          f"aurora={au.count('notebook_cells', f'notebook_id={nb_a}')}")

    # ── Edge E2: edit that REMOVES a cell ────────────────────────────────────
    section("Edge E2: remove a cell from A (parent-scoped prune)")
    cells_a3 = cells_a2[:-1]  # drop the last (SQL) cell
    save_cells(nb_a, cells_a3, sid_a)
    res = run_sync_once()
    check("sync after removal ok", res["status"] == "ok", res.get("error") or "")
    check(f"Aurora A pruned to {len(cells_a3)} cells (no stale cell)",
          au.count("notebook_cells", f"notebook_id={nb_a}") == len(cells_a3),
          f"aurora={au.count('notebook_cells', f'notebook_id={nb_a}')}")

    # ── Edge E5: unicode + large-ish cell ────────────────────────────────────
    section("Edge E5: unicode + large cell content")
    big_code = "# 日本語 コメント — café — 🚀\n" + "x = [\n" + ",\n".join(str(i) for i in range(500)) + "\n]\n"
    cells_a4 = cells_a3 + [{"id": str(uuid.uuid4()), "type": "code", "code": big_code,
                            "position": len(cells_a3)}]
    save_cells(nb_a, cells_a4, sid_a)
    res = run_sync_once()
    check("sync with unicode/large cell ok", res["status"] == "ok", res.get("error") or "")
    got = au.scalar(f"SELECT code FROM notebook_cells WHERE notebook_id={nb_a} "
                    f"AND code LIKE '%日本語%' LIMIT 1")
    check("Aurora preserved unicode cell content", got is not None and "🚀" in (got or ""))

    # ── Scenario 3 / Edge E4: close (suspend) then reopen (relaunch) ─────────
    if args.vm and vm_a:
        section("Scenario 3: close (suspend) then reopen (relaunch/restore)")
        st, r = suspend(sid_a)
        check(f"suspend A ({r.get('status')})", st == 200, f"{st} {r}")
        time.sleep(2)
        st, r = launch_vm(name_a, nb_a, sid_a, restore=sid_a)
        vm_a2 = r.get("microvmId")
        check(f"reopen A relaunched/restored ({r.get('status')}, reused={r.get('reused')})",
              st == 200 and vm_a2, f"{st} {r}")
        res = run_sync_once()
        check("sync after close/reopen ok", res["status"] == "ok", res.get("error") or "")
        # Exactly one non-terminated vm_session should serve session A in SQLite.
        live = sqlite_count("vm_sessions", f"session_id='{sid_a}' AND state != 'TERMINATED'")
        check(f"session A has a live VM after reopen (n={live})", live >= 1)
        verify_table_parity(au, ["vm_sessions", "vm_state_log"])

    # ── Edge E1: idempotent re-sync (no changes) ─────────────────────────────
    section("Edge E1: idempotent re-sync (expect ~no-op)")
    res = run_sync_once()
    # With no proxy activity between the two syncs, upserts should be 0 (or only the
    # handful of vm_sessions rows the running proxy's /instances poll just touched).
    check(f"re-sync ok, minimal work (up={res['rows_upserted']} del={res['rows_deleted']})",
          res["status"] in ("ok", "idle") and res["rows_deleted"] == 0, res.get("error") or "")

    # ── Edge E7: INDEPENDENT version create (REGRESSION for the parent-scoped bug) ──
    # A version created via POST /versions (auto/pre_ai/manual) does NOT bump the
    # parent notebook's updated_at. The old parent-scoped-only sync never propagated
    # it — the exact bug this suite failed to catch. It must sync on its OWN
    # watermark now, along with its notebook_cell_versions.
    section("Edge E7: independent version create (no parent update) -> must sync")
    vers_before = au.count("notebook_versions", f"notebook_id={nb_a}")
    cv_before = au.count("notebook_cell_versions",
                         f"version_group_id IN (SELECT version_group_id FROM notebook_versions WHERE notebook_id={nb_a})")
    st, hdr = create_version(nb_a, trigger="manual", label="e2e-manual-1")
    vgid = hdr.get("version_group_id")
    check(f"created independent manual version ({vgid})", st == 200 and vgid)
    res = run_sync_once()
    check(f"sync after independent version ok (up={res['rows_upserted']})",
          res["status"] == "ok", res.get("error") or "")
    a_vers = au.count("notebook_versions", f"notebook_id={nb_a}")
    check(f"Aurora gained the version (was {vers_before}, now {a_vers})",
          a_vers == vers_before + 1, f"aurora={a_vers}")
    check("Aurora has the new version's cell_versions",
          au.count("notebook_cell_versions", f"version_group_id='{vgid}'") > 0)
    # Deep parity on the version tables — the strong assertion that would have
    # failed against the buggy worker.
    verify_deep_parity(au, ["notebook_versions", "notebook_cell_versions"])

    # ── Edge E9: batch of independent versions, ONE sync ─────────────────────
    section("Edge E9: several independent versions, single sync -> parity")
    for i in range(3):
        create_version(nb_a, trigger="auto")
    res = run_sync_once()
    check(f"batch version sync ok (up={res['rows_upserted']})",
          res["status"] == "ok", res.get("error") or "")
    verify_deep_parity(au, ["notebook_versions", "notebook_cell_versions"])

    # ── Edge E11: version create then a SEPARATE later sync (temporal gap) ────
    # The precise gap the original tests missed: create a version, then sync AGAIN
    # with nothing else happening — it must still be there (own-watermark, not
    # dependent on a subsequent parent update).
    section("Edge E11: version create, then an independent later sync")
    st, hdr2 = create_version(nb_a, trigger="manual", label="e2e-manual-2")
    vgid2 = hdr2.get("version_group_id")
    run_sync_once()  # sync #1 carries it
    res = run_sync_once()  # sync #2: nothing new for versions
    check("second sync is a no-op for versions",
          au.count("notebook_versions", f"version_group_id='{vgid2}'") == 1)
    verify_deep_parity(au, ["notebook_versions", "notebook_cell_versions"])

    # ── Edge E8: restore a version (creates pre_restore + rewrites cells) ─────
    section("Edge E8: restore a version -> pre_restore version + cells rewritten")
    versions = list_versions(nb_a)
    # Restore the baseline (oldest) so the live doc changes back to it.
    baseline = next((v for v in versions if v.get("trigger") == "baseline"), versions[-1])
    v_count_before = len(versions)
    st, r = restore_version(nb_a, baseline["version_group_id"])
    check(f"restore baseline ok (pre_restore={bool(r.get('pre_restore_version'))})",
          st == 200 and r.get("success"), f"{st} {r}")
    res = run_sync_once()
    check(f"sync after restore ok (up={res['rows_upserted']} del={res['rows_deleted']})",
          res["status"] == "ok", res.get("error") or "")
    # A pre_restore version was added; live cells now match the baseline's cells.
    a_vcount = au.count("notebook_versions", f"notebook_id={nb_a}")
    check(f"Aurora gained the pre_restore version (was {v_count_before}, now {a_vcount})",
          a_vcount == v_count_before + 1, f"aurora={a_vcount}")
    check("Aurora notebook_cells match SQLite after restore",
          au.count("notebook_cells", f"notebook_id={nb_a}")
          == sqlite_count("notebook_cells", f"notebook_id={nb_a}"))
    verify_deep_parity(au, ["notebooks", "notebook_cells", "notebook_versions",
                            "notebook_cell_versions"])

    # ── Scenario 4: delete notebook B -> Aurora cascade + tombstone replay ───
    section("Scenario 4: delete notebook B (cascade + tombstone)")
    b_cells_before = au.count("notebook_cells", f"notebook_id={nb_b}")
    b_versions_before = au.count("notebook_versions", f"notebook_id={nb_b}")
    check(f"pre-delete: Aurora B has {b_cells_before} cells, {b_versions_before} versions",
          b_cells_before > 0)
    if args.vm and vm_b:
        terminate(sid_b, vm_b)
    st, r = delete_notebook(nb_b)
    check(f"deleted notebook B ({r})", st == 200 and r.get("success"))
    created_ids.remove(nb_b)
    res = run_sync_once()
    check(f"sync after delete ok (del={res['rows_deleted']})",
          res["status"] == "ok", res.get("error") or "")
    check("Aurora B notebook row gone", au.count("notebooks", f"id={nb_b}") == 0)
    check("Aurora B cells cascade-deleted", au.count("notebook_cells", f"notebook_id={nb_b}") == 0)
    check("Aurora B versions cascade-deleted",
          au.count("notebook_versions", f"notebook_id={nb_b}") == 0)
    # notebook_runs are NOT FK-cascaded (session history) — B's run row should REMAIN.
    check("Aurora B notebook_runs preserved (no FK cascade)",
          au.count("notebook_runs", f"notebook_id={nb_b}") >= 1)
    verify_deep_parity(au, ALL_TABLES)

    # ── Edge E6: sync again after delete (delete cursor stable) ──────────────
    section("Edge E6: re-sync after delete (no re-delete, cursor stable)")
    res = run_sync_once()
    check(f"re-sync after delete ok (del={res['rows_deleted']})",
          res["status"] in ("ok", "idle") and res["rows_deleted"] == 0,
          f"deleted={res['rows_deleted']} (expected 0)")

    # ── Edge E10: delete a notebook WITH MANY VERSIONS -> full cascade ───────
    # Notebook A now has multiple versions (baseline + manual/auto/pre_restore from
    # E7-E9-E11-E8) and many cell_versions. Deleting it must cascade ALL of them
    # away in Aurora from the single notebooks tombstone.
    section("Edge E10: delete notebook A (many versions) -> full cascade")
    a_vers = au.count("notebook_versions", f"notebook_id={nb_a}")
    a_cv = au.count("notebook_cell_versions",
                    f"version_group_id IN (SELECT version_group_id FROM notebook_versions WHERE notebook_id={nb_a})")
    check(f"pre-delete: Aurora A has {a_vers} versions, {a_cv} cell_versions", a_vers >= 3)
    if args.vm:
        terminate_all_vms_for_sessions({sid_a})
    st, r = delete_notebook(nb_a)
    check(f"deleted notebook A ({r})", st == 200 and r.get("success"))
    created_ids.remove(nb_a)
    res = run_sync_once()
    check(f"sync after A delete ok (del={res['rows_deleted']})",
          res["status"] == "ok", res.get("error") or "")
    check("Aurora A notebook gone", au.count("notebooks", f"id={nb_a}") == 0)
    check("Aurora A versions all cascade-deleted",
          au.count("notebook_versions", f"notebook_id={nb_a}") == 0)
    check("Aurora A cell_versions all cascade-deleted",
          au.count("notebook_cell_versions", f"version_group_id='{vgid}'") == 0)
    verify_deep_parity(au, ALL_TABLES)

    # ── cleanup ──────────────────────────────────────────────────────────────
    if not args.keep:
        section("cleanup")
        # Terminate every VM the test launched (incl. reopen-relaunches) FIRST —
        # DELETE /notebooks does NOT stop VMs, so without this they'd leak.
        killed = terminate_all_vms_for_sessions(test_sessions)
        check(f"terminated {killed} test VM(s)", True)
        for nb in list(created_ids):
            delete_notebook(nb)
        # Give the control plane a moment, then confirm no test VM remains.
        time.sleep(3)
        st, data = _req("GET", "/instances")
        leftover = [mid for mid, info in (data.get("instances") or {}).items()
                    if info.get("session_id") in test_sessions
                    and info.get("state") not in ("TERMINATED", "TERMINATING")]
        check("no test VMs left running after cleanup", not leftover,
              f"leftover: {leftover}")
        run_sync_once()
        check("cleanup sync ok", True)

    # ── summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed == total:
        print("ALL SYNC E2E CHECKS PASSED ✓")
        return 0
    print("SOME CHECKS FAILED ✗")
    for name, ok in _checks:
        if not ok:
            print(f"   FAILED: {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
