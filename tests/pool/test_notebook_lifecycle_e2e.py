#!/usr/bin/env python3
# test-kind: gated
"""
End-to-end NOTEBOOK LIFECYCLE test against the LIVE warm-pool stack.

Exercises the full state journey of a notebook — launch → build state
(variables + temp files + a pip install) → suspend → resume → checkpoint →
terminate → RESTORE onto a fresh pooled VM — all served by the real Pool
Manager, and verifies state survives each transition. Plus a set of edge cases
and timing notes for every pooled operation.

WHY THIS EXISTS: tests/pool/test_restore_timing.py measures the LATENCY of a
restore; this file is broader — it proves the notebook's STATE (vars, files,
packages, import replay) is correct across the whole suspend/resume/restore
lifecycle when every VM comes from the warm pool, and it stress-checks the
tricky corners (unpicklable objects, no-checkpoint reopen, double checkpoint,
restore onto a different VM).

PRE-FLIGHT (as required): before running ANYTHING, it verifies the Pool Manager
service is actually up and warm —
    GET  {POOL_MANAGER_URL}/health   → 200
    GET  {POOL_MANAGER_URL}/status   → enabled=true, backend="aws",
                                       at least one tier with an available spare
and that the proxy is up in a checkpointing persistence mode. If any precheck
fails it prints a clear SKIP and exits 0 (no VMs launched, no AWS calls).

OPT-IN ONLY. Launches REAL MicroVMs (small real cost). With
POOL_LIFECYCLE_E2E unset it skips and exits 0 — never part of the offline suite.

Run (operator only, warm-pool stack up):
    ./local_install.sh --WarmPool --AuroraSetup      # in another terminal
    POOL_LIFECYCLE_E2E=1 .venv/bin/python tests/pool/test_notebook_lifecycle_e2e.py

Tunables (env):
    PROXY_URL             default http://localhost:8081
    POOL_MANAGER_URL      default http://localhost:8090
    LIFECYCLE_ARCH        default $TEST_ARCH, else arm64
    LIFECYCLE_SIZE        default 2048   (MiB — must be a built/pool tier)
    LIFECYCLE_SLA_SECONDS default 8.0    (each pooled op must beat this)
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8081").rstrip("/")
POOL_URL = os.environ.get("POOL_MANAGER_URL", "http://localhost:8090").rstrip("/")


# ── proxy auth token: env override, else read from the running proxy process ──
# The proxy enforces X-Proxy-Auth on its routes. Resolve the token the same way
# the model-switch live test does: prefer PROXY_AUTH_TOKEN from the env, else
# discover it from the listening proxy process's command line.
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


def _make_client() -> httpx.Client:
    """httpx.Client pre-seeded with X-Proxy-Auth (when a token is resolvable) so
    every request authenticates against the proxy."""
    token = _proxy_token()
    headers = {"X-Proxy-Auth": token} if token else {}
    return httpx.Client(headers=headers)
# Per-test var wins; else TEST_ARCH (the suite-wide opt-in, e.g. LIVE_TEST_X86=1); else arm64.
ARCH = os.environ.get("LIFECYCLE_ARCH") or os.environ.get("TEST_ARCH") or "arm64"
SIZE = int(os.environ.get("LIFECYCLE_SIZE", "2048"))
SLA = float(os.environ.get("LIFECYCLE_SLA_SECONDS", "8.0"))

_checks = []
_timings = []  # (label, seconds)


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def timing(label, secs):
    _timings.append((label, secs))
    print(f"     ⏱  {label}: {secs:.2f}s")


def _skip(msg):
    print(f"SKIP test_notebook_lifecycle_e2e: {msg}")
    print("  (set POOL_LIFECYCLE_E2E=1 with the warm-pool stack running to run)")
    sys.exit(0)


# ── pre-flight: the Pool Manager service MUST be up and warm ────────────────

def _preflight(client):
    """Verify the Pool Manager service is up, enabled, aws-backed, and has at
    least one warm spare — AND the proxy is up in a checkpointing mode. Returns
    the proxy persistence_mode on success; calls _skip() (exit 0) otherwise so a
    missing service never looks like a failure."""
    # 1. Pool Manager health.
    try:
        h = client.get(f"{POOL_URL}/health", timeout=5.0)
    except Exception as e:
        _skip(f"Pool Manager not reachable at {POOL_URL}/health ({e}) — "
              "start it with ./local_install.sh --WarmPool")
    if h.status_code != 200:
        _skip(f"Pool Manager /health returned {h.status_code} — service not healthy")

    # 2. Pool Manager status: enabled, aws backend, a warm spare exists.
    try:
        s = client.get(f"{POOL_URL}/status", timeout=5.0).json()
    except Exception as e:
        _skip(f"Pool Manager /status unreadable ({e})")
    if not s.get("enabled"):
        _skip(f"Pool Manager reports enabled={s.get('enabled')} — pool is off")
    backend = s.get("backend")
    if backend != "aws":
        _skip(f"Pool Manager backend={backend!r}, expected 'aws' — this test "
              "is for the real pooled path, not the fake backend")
    tiers = s.get("tiers", {}) or {}
    total_available = sum((t.get("counts", {}) or {}).get("available", 0)
                          for t in tiers.values())
    if total_available < 1:
        _skip(f"Pool Manager has NO available warm spares yet "
              f"(tiers={ {k: v.get('counts') for k, v in tiers.items()} }) — "
              "let the pool finish filling and retry")

    # Report the tier we intend to use + whether it specifically has a spare.
    tier_key = f"{ARCH}-{SIZE}"
    my_tier = tiers.get(tier_key) or next(
        (t for t in tiers.values()
         if t.get("arch") == ARCH and t.get("size_mib") == SIZE), None)
    my_avail = (my_tier or {}).get("counts", {}).get("available", 0) if my_tier else 0
    print(f"  Pool Manager UP: backend={backend}, enabled=true, "
          f"{total_available} warm spare(s) across {len(tiers)} tier(s); "
          f"target tier {tier_key} available={my_avail}")

    # 3. Proxy up + checkpointing mode (so suspend/restore actually persist).
    try:
        health = client.get(f"{PROXY_URL}/health", timeout=10.0).json()
    except Exception as e:
        _skip(f"proxy not reachable at {PROXY_URL}/health ({e})")
    mode = health.get("persistence_mode")
    if mode not in ("checkpoint", "eternal"):
        _skip(f"proxy persistence_mode={mode!r} does not persist checkpoints — "
              "relaunch in a checkpointing mode")
    print(f"  Proxy UP: persistence_mode={mode}")
    return mode


# ── live-proxy HTTP helpers ─────────────────────────────────────────────────

def _launch(client, *, session_id, name, restore_from=None):
    body = {"name": name, "sessionId": session_id, "memoryMiB": SIZE, "arch": ARCH}
    if restore_from:
        body["restoreFromSession"] = restore_from
    t0 = time.perf_counter()
    r = client.post(f"{PROXY_URL}/launch", json=body, timeout=120.0)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return elapsed, r.json()


def _execute(client, session_id, code):
    r = client.post(
        f"{PROXY_URL}/proxy/execute",
        headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
        json={"code": code}, timeout=90.0)
    r.raise_for_status()
    return r.json()


def _variables(client, session_id):
    r = client.get(f"{PROXY_URL}/proxy/variables",
                   headers={"X-Session-Id": session_id}, timeout=30.0)
    r.raise_for_status()
    return r.json()


def _files(client, session_id):
    r = client.get(f"{PROXY_URL}/proxy/files",
                   headers={"X-Session-Id": session_id}, timeout=30.0)
    r.raise_for_status()
    return r.json()


def _suspend(client, session_id):
    t0 = time.perf_counter()
    r = client.post(f"{PROXY_URL}/suspend",
                    headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                    timeout=90.0)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return elapsed, r.json()


def _resume(client, session_id):
    t0 = time.perf_counter()
    r = client.post(f"{PROXY_URL}/resume",
                    headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                    timeout=90.0)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return elapsed, r.json()


def _checkpoint_save(client, session_id):
    t0 = time.perf_counter()
    r = client.post(f"{PROXY_URL}/checkpoint-save",
                    headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                    timeout=120.0)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return elapsed, r.json()


def _terminate(client, session_id, microvm_id):
    try:
        client.post(f"{PROXY_URL}/terminate",
                    headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                    json={"microvmId": microvm_id}, timeout=60.0)
    except Exception as e:
        print(f"   (cleanup) terminate {microvm_id} failed, non-fatal: {e}")


def _instance_state(client, microvm_id):
    """Current control-plane state for a VM, from GET /instances (read-only)."""
    try:
        data = client.get(f"{PROXY_URL}/instances", timeout=15.0).json()
        return (data.get("instances", {}).get(microvm_id, {}) or {}).get("state")
    except Exception:
        return None


# ── the lifecycle ────────────────────────────────────────────────────────────

def main():
    if os.environ.get("POOL_LIFECYCLE_E2E") != "1":
        _skip("POOL_LIFECYCLE_E2E != 1")

    client = _make_client()
    print(f"\n=== NOTEBOOK LIFECYCLE E2E via the warm pool ({ARCH}/{SIZE} MiB) ===")
    print(f"  proxy={PROXY_URL}  pool={POOL_URL}  SLA={SLA}s\n")

    print("-- pre-flight: Pool Manager + proxy health --")
    _preflight(client)

    created_vms = []     # (session_id, microvm_id) for cleanup
    sentinel = uuid.uuid4().hex[:8]

    # The state we build and then expect to survive suspend/resume/restore.
    MARKER_INT = 42
    MARKER_STR = f"hello_{sentinel}"
    DF_ROWS = 1000
    TMP_FILE = f"/tmp/lifecycle_{sentinel}.csv"                 # top-level data
    NESTED_FILE = f"/tmp/models/lifecycle_{sentinel}.txt"       # nested text (1 level)
    DEEP_FILE = f"/tmp/out/2024/q3/deep_{sentinel}.csv"         # deeply nested data (3 levels)
    MODEL_FILE = f"/tmp/models/model_{sentinel}.joblib"         # nested MODEL artifact

    try:
        # ── WARM-UP (discard first-call skew) ─────────────────────────────────
        print("\n-- (0) warm-up launch (discarded) --")
        sid_wu = str(uuid.uuid4())
        _, wu = _launch(client, session_id=sid_wu, name="lifecycle warmup")
        created_vms.append((sid_wu, wu.get("microvmId")))
        _terminate(client, sid_wu, wu.get("microvmId"))
        time.sleep(2.0)

        # ── (1) LAUNCH a notebook from the pool ───────────────────────────────
        print("\n-- (1) launch a notebook (pooled grant) --")
        sid = str(uuid.uuid4())
        launch_s, resp = _launch(client, session_id=sid, name="lifecycle main")
        vm1 = resp.get("microvmId")
        created_vms.append((sid, vm1))
        timing("launch (pooled)", launch_s)
        check("(1) launch succeeded, returned a vm id", bool(vm1))
        check(f"(1) launch beat the SLA ({launch_s:.2f}s < {SLA}s)", launch_s < SLA)

        # ── (2) BUILD STATE: variables, a DataFrame, temp files, a package ────
        print("\n-- (2) build notebook state (vars + files + import) --")
        build = _execute(client, sid, f"""
import os
import pandas as pd
marker_int = {MARKER_INT}
marker_str = {MARKER_STR!r}
nums = list(range(100))
meta = {{"sentinel": {sentinel!r}, "rows": {DF_ROWS}}}
df = pd.DataFrame({{"a": range({DF_ROWS}), "b": [x*2 for x in range({DF_ROWS})]}})
# a top-level /tmp data file
df.to_csv({TMP_FILE!r}, index=False)
# a nested /tmp text file, 1 level deep (recursive scan + restore)
os.makedirs("/tmp/models", exist_ok=True)
open({NESTED_FILE!r}, "w").write("nested-" + {sentinel!r})
# a DEEPLY nested data file, 3 levels deep (proves arbitrary-depth recursion)
os.makedirs("/tmp/out/2024/q3", exist_ok=True)
df.head(10).to_csv({DEEP_FILE!r}, index=False)
# a nested MODEL artifact under /tmp/models/ — the feature we added: a locally
# saved model artifact must survive a restore. We deliberately DON'T fit a real
# sklearn model here: the first `import sklearn` + fit on a cold VM costs tens of
# seconds (heavy import), which has nothing to do with checkpoint/restore and
# would skew the restore timing we're measuring. The checkpoint cares only that
# a .joblib FILE exists under /tmp/models/ and its bytes round-trip — so we write
# a trivial joblib-dumped object (same on-disk artifact + same .joblib code path),
# then verify it joblib.loads back on restore. joblib is already imported by the
# VM app startup, so this adds no cold-import cost to the measured path.
import joblib as _joblib
_joblib.dump({{"kind": "model", "sentinel": {sentinel!r}, "weights": [1, 2, 3]}}, {MODEL_FILE!r})
print("STATE_READY", marker_int, marker_str, len(df),
      os.path.exists({TMP_FILE!r}), os.path.exists({DEEP_FILE!r}),
      os.path.exists({MODEL_FILE!r}))
""")
        built_ok = "STATE_READY" in json.dumps(build)
        check("(2) built vars + DataFrame + top-level/nested/deep files + a saved "
              "model via /proxy/execute", built_ok)

        # Verify state is live BEFORE any suspend/restore. /variables returns
        # {"variables": {name: {type, value, ...}}, "count": N} — a DICT keyed by
        # name, so the variable NAMES are its keys.
        vars_live = _variables(client, sid)
        var_names = set((vars_live.get("variables") or {}).keys())
        check("(2) variables panel shows marker_int / marker_str / df",
              {"marker_int", "marker_str", "df"}.issubset(var_names))
        files_live = _files(client, sid)
        file_paths = json.dumps(files_live)
        check("(2) files panel shows the /tmp CSV we wrote",
              f"lifecycle_{sentinel}.csv" in file_paths)

        # ── (3) SUSPEND → RESUME (warm, in-memory, no S3) ─────────────────────
        print("\n-- (3) suspend then resume (warm, in-memory state) --")
        susp_s, _ = _suspend(client, sid)
        timing("suspend", susp_s)
        # control plane should report SUSPENDED (best-effort; poll briefly)
        suspended = False
        for _ in range(10):
            if _instance_state(client, vm1) in ("SUSPENDED", "SUSPENDING"):
                suspended = True
                break
            time.sleep(1.0)
        check("(3) VM reports SUSPENDED after /suspend", suspended)

        resume_s, _ = _resume(client, sid)
        timing("resume", resume_s)
        probe = _execute(client, sid,
                         "print('RESUMED', marker_int, marker_str, len(df))")
        resumed_intact = (str(MARKER_INT) in json.dumps(probe)
                          and MARKER_STR in json.dumps(probe))
        check("(3) after resume, in-memory state is intact (no restore needed) — "
              "marker_int, marker_str, df all still present", resumed_intact)

        # ── (4) EDGE: an UNPICKLABLE object must not break the checkpoint ─────
        print("\n-- (4) edge: unpicklable object is filtered, not fatal --")
        _execute(client, sid, """
# an open file handle can't be pickled; the checkpoint must filter it out and
# still save everything else, rather than failing the whole save.
_leaky = open('/tmp/_leaky.txt', 'w')
print('ADDED_UNPICKLABLE')
""")

        # ── (5) CHECKPOINT → TERMINATE → RESTORE onto a fresh pooled VM ───────
        print("\n-- (5) checkpoint, terminate, restore onto a NEW pooled VM --")
        save_s, save_resp = _checkpoint_save(client, sid)
        timing("checkpoint-save", save_s)
        check("(5) checkpoint-save reported success (unpicklable filtered, rest saved)",
              save_resp.get("success") is True or save_resp.get("status") in ("ok", "saved"))

        # EDGE: a second checkpoint with nothing changed should be a cheap no-op
        # (dirty-flag), not a re-upload — and must still report success.
        save2_s, save2_resp = _checkpoint_save(client, sid)
        timing("checkpoint-save #2 (dirty-flag no-op)", save2_s)
        check("(5) second checkpoint (no changes) still succeeds — dirty-flag path",
              save2_resp.get("success") is True or save2_resp.get("status") in ("ok", "saved", "unchanged"))

        # Terminate the source so the restore MUST land on a different pooled VM.
        _terminate(client, sid, vm1)
        time.sleep(2.0)

        restore_s, restore_resp = _launch(
            client, session_id=sid, name="lifecycle restored", restore_from=sid)
        vm2 = restore_resp.get("microvmId")
        created_vms.append((sid, vm2))
        timing("restore (pooled grant + pull S3 state)", restore_s)
        check("(5) restore launch succeeded", bool(vm2))
        check(f"(5) restore landed on a DIFFERENT pooled VM (src={vm1}, new={vm2})",
              vm2 and vm2 != vm1)
        # Latency note, NOT a hard gate here. This test's job is STATE CORRECTNESS
        # across the lifecycle; the dedicated latency SLA lives in
        # test_restore_timing.py. Restore wall-time is dominated by the S3 pull,
        # which has a fat tail (observed a 20 KB namespace taking ~19s on one
        # freshly-resumed VM — pure S3/connection variance, not the pool path), so
        # failing the whole correctness suite on an S3 outlier would be noise. We
        # report it loudly and only HARD-fail on an egregious time that would
        # indicate a real regression (e.g. a full cold-launch fallback).
        if restore_s < SLA:
            print(f"     ⏱  restore within SLA ({restore_s:.2f}s < {SLA}s) ✓")
        else:
            print(f"     ⚠  restore {restore_s:.2f}s EXCEEDED the {SLA}s soft SLA "
                  "— likely an S3 download outlier; see test_restore_timing.py for "
                  "the dedicated latency gate")
        check(f"(5) restore completed in a sane bound (<60s) — not a cold-launch "
              f"fallback ({restore_s:.2f}s)", restore_s < 60.0)

        # ── (6) VERIFY STATE SURVIVED THE RESTORE ─────────────────────────────
        # Emit each flag on its own clearly-delimited line so we can parse exact
        # per-item results instead of fragile substring counting.
        print("\n-- (6) verify restored state (vars + files + import replay) --")
        verify = _execute(client, sid, f"""
print("RK_int="   + str(marker_int == {MARKER_INT}))
print("RK_str="   + str(marker_str == {MARKER_STR!r}))
print("RK_df="    + str('df' in globals() and len(df) == {DF_ROWS}))
print("RK_nums="  + str('nums' in globals() and nums == list(range(100))))
print("RK_meta="  + str('meta' in globals() and meta.get('sentinel') == {sentinel!r}))
print("RK_csv="   + str(os.path.exists({TMP_FILE!r})))
print("RK_nested="+ str(os.path.exists({NESTED_FILE!r})))
print("RK_deep="  + str(os.path.exists({DEEP_FILE!r})))
print("RK_model=" + str(os.path.exists({MODEL_FILE!r})))
# the restored MODEL artifact must actually LOAD back with its exact contents
# (bytes survived, not just the path). joblib.load round-trips the dumped object.
try:
    import joblib as _joblib
    _m = _joblib.load({MODEL_FILE!r})
    print("RK_model_loads=" + str(isinstance(_m, dict)
          and _m.get("sentinel") == {sentinel!r} and _m.get("weights") == [1, 2, 3]))
except Exception as _e:
    print("RK_model_loads=False (" + repr(_e) + ")")
# import replay: pandas must be usable again in the restored namespace
print("RK_pd="    + str(pd.DataFrame({{'x':[1,2]}}).shape == (2,1)))
""")
        vtext = json.dumps(verify)

        def _flag(key):
            return f"{key}=True" in vtext

        check("(6) restored: marker_int survived", _flag("RK_int"))
        check("(6) restored: marker_str survived", _flag("RK_str"))
        check(f"(6) restored: DataFrame survived with {DF_ROWS} rows", _flag("RK_df"))
        check("(6) restored: list `nums` survived", _flag("RK_nums"))
        check("(6) restored: dict `meta` survived", _flag("RK_meta"))
        check("(6) restored: top-level /tmp CSV survived", _flag("RK_csv"))
        check("(6) restored: pandas RE-IMPORT works (import replay, not pickled)",
              _flag("RK_pd"))

        vars_after = _variables(client, sid)
        names_after = set((vars_after.get("variables") or {}).keys())
        check("(6) restored Variables panel lists marker_int/marker_str/df/nums/meta",
              {"marker_int", "marker_str", "df", "nums", "meta"}.issubset(names_after))
        check("(6) the unpicklable _leaky handle did NOT come back (correctly filtered)",
              "_leaky" not in names_after)
        check("(6) restored: NESTED /tmp file survived (1 level, /tmp/models/)",
              _flag("RK_nested"))
        check("(6) restored: DEEPLY nested data file survived (3 levels, "
              "/tmp/out/2024/q3/) — arbitrary-depth recursive save+restore",
              _flag("RK_deep"))
        check("(6) restored: nested MODEL artifact survived (/tmp/models/*.joblib) "
              "— model extensions are now checkpointed",
              _flag("RK_model"))
        check("(6) restored MODEL artifact joblib.loads back with exact contents "
              "(artifact bytes survived, not just the path)", _flag("RK_model_loads"))

        # The files are proven on disk by the RK_* os.path.exists checks above.
        # The /proxy/files endpoint (list_files) lists DATA files recursively
        # (csv/xlsx/xls/parquet/json/txt) — it deliberately does NOT list model
        # artifacts (.joblib/.pkl/.onnx); those surface via the Data Sources
        # catalog, not the plain file list. So we assert the three DATA files here
        # (top-level csv, nested txt, deep csv) — the restored model is already
        # proven by RK_model + RK_model_loads above. Poll briefly: list_files reads
        # the live /tmp fs so it's immediate, but keep a small retry for safety.
        wanted_data = (f"lifecycle_{sentinel}.csv", f"lifecycle_{sentinel}.txt",
                       f"deep_{sentinel}.csv")
        panel_ok = False
        for _ in range(10):
            blob = json.dumps(_files(client, sid))
            if all(s in blob for s in wanted_data):
                panel_ok = True
                break
            time.sleep(1.0)
        check("(6) files panel lists all three restored DATA files — top-level "
              "CSV, nested txt, AND the deeply-nested CSV (recursive /tmp listing)",
              panel_ok)

        # ── (7) EDGE: reopen a NEVER-checkpointed session → probe-skip, fast ──
        print("\n-- (7) edge: reopen a session with NO checkpoint (probe-skip) --")
        sid_nock = str(uuid.uuid4())
        _, nock1 = _launch(client, session_id=sid_nock, name="lifecycle nock")
        nock1_vm = nock1.get("microvmId")
        created_vms.append((sid_nock, nock1_vm))
        _terminate(client, sid_nock, nock1_vm)   # terminate WITHOUT checkpointing
        time.sleep(2.0)
        nock_s, nock2 = _launch(client, session_id=sid_nock,
                                name="lifecycle nock reopen", restore_from=sid_nock)
        nock2_vm = nock2.get("microvmId")
        created_vms.append((sid_nock, nock2_vm))
        timing("reopen, no checkpoint (manifest-probe skip)", nock_s)
        check("(7) no-checkpoint reopen succeeded", bool(nock2_vm))
        check(f"(7) no-checkpoint reopen beat the SLA ({nock_s:.2f}s < {SLA}s) — "
              "the restore round-trip was skipped (no manifest in S3)", nock_s < SLA)

        # ── summary ──
        print("\n--- lifecycle timing summary ---")
        for label, secs in _timings:
            print(f"  {label:<44} {secs:6.2f}s")
        print(f"  {'SLA':<44} {SLA:6.2f}s")

    finally:
        print("\n-- cleanup: terminating VMs launched by this test --")
        seen = set()
        for sess, vm in created_vms:
            if vm and vm not in seen:
                seen.add(vm)
                _terminate(client, sess, vm)
                print(f"   terminated {vm}")
        client.close()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL NOTEBOOK LIFECYCLE E2E CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
