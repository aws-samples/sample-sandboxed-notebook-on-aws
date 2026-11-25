#!/usr/bin/env python3
# test-kind: live
"""
End-to-End Test: Save-on-Suspend per-step timing with BIG DataFrames (Checkpoint Mode)

Purpose
-------
Exercise and TIME the /suspend checkpoint path — the primary save point for idle
VMs (a VM killed while SUSPENDED never fires /terminate; see
app/platform/hooks.py).

This test (launched with idleTimeoutSeconds=60 so idle auto-suspend is possible):
  1. Launches a checkpoint-mode VM.
  2. ROUND 1: creates a BIG DataFrame + vars, then EXPLICITLY suspends the VM
     (POST /suspend). The /suspend hook checkpoints to S3 (state is dirty).
     After resume, it reads the VM's per-step save timings + sizes.
  3. ROUND 2: mutates state (doubles the DataFrame, adds a column), EXPLICITLY
     suspends again. A second S3 save happens (dirty). Reads its timings + sizes.
  4. ROUND 3: makes another change, then goes IDLE (no traffic) for ~60s so the
     PLATFORM auto-suspends the VM — the real production trigger. The /suspend
     hook fires the same way → third S3 save. Reads its timings + sizes.
  5. Reports per-step time + bytes for ALL THREE saves side by side, and checks
     that save #2 > #1 (more data) and that the idle-auto-suspend save happened.

Why explicit suspend (not idle wait)?
  The proxy exposes POST /suspend (session-scoped) which calls suspend-microvm.
  That's deterministic and fast, vs. waiting ~60s+ for idle auto-suspend. The
  /suspend LIFECYCLE HOOK fires the same way either path, so the save behavior
  under test is identical.

Reading save timings:
  A SUSPENDED VM is frozen and cannot serve HTTP, so we RESUME before reading
  GET /proxy/checkpoint-timings (which returns last_save / last_save_sizes /
  last_save_summary from the VM's CheckpointManager).

Requires:
  - SESSION_PERSISTENCE_MODE=checkpoint
  - proxy running (./local_install.sh)

Usage:
    python3 tests/checkpoint/test_suspend_checkpoint_timing.py
    python3 tests/checkpoint/test_suspend_checkpoint_timing.py --rows 3000000
"""

import os
import re
import subprocess
import time
import argparse
import requests

PROXY_URL = "http://localhost:8081"
MEMORY_MIB = 4096  # bigger tier — big DataFrame needs headroom for serialize
AWS_REGION = "us-west-2"


# ── proxy auth: the proxy enforces X-Proxy-Auth. Resolve the token (env override,
# else the listening proxy process's command line) and route every call through a
# requests.Session that sends it by default — so the existing requests.get/post
# calls below authenticate with no per-call-site change. `requests` is rebound to
# that session at module scope.
def _proxy_token():
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


_session = requests.Session()
_tok = _proxy_token()
if _tok:
    _session.headers["X-Proxy-Auth"] = _tok
requests = _session  # noqa: F811 — route all requests.get/post through the authed session

# Default big DataFrame size (rows). ~3 numeric float64 cols → ~24 bytes/row,
# so 2M rows ≈ 48 MB in memory, tens of MB serialized — enough to make
# serialize + upload times meaningful (not sub-millisecond noise).
DEFAULT_ROWS = 2_000_000


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def execute_code(session_id, code, timeout=120, cell_id=None):
    body = {"code": code}
    if cell_id is not None:
        body["cell_id"] = cell_id
    resp = requests.post(
        f"{PROXY_URL}/proxy/execute",
        headers={"Content-Type": "application/json", "X-Session-Id": session_id},
        json=body, timeout=timeout,
    )
    return resp.json()


def get_state(session_id):
    """Current VM state for a session, via the proxy /instances view."""
    try:
        resp = requests.get(f"{PROXY_URL}/instances", timeout=15)
        for vm_id, info in resp.json().get("instances", {}).items():
            if info.get("session_id") == session_id:
                return vm_id, info.get("state", "UNKNOWN")
    except Exception as e:
        log(f"    (get_state error: {e})")
    return None, "NOT_FOUND"


def wait_for_state(session_id, want, timeout=90):
    """Poll until the session's VM reaches `want` state (or timeout)."""
    start = time.time()
    last = None
    while time.time() - start < timeout:
        _vm, st = get_state(session_id)
        if st != last:
            log(f"    [{int(time.time()-start)}s] state={st}")
            last = st
        if st == want:
            return True
        time.sleep(3)
    return False


def suspend(session_id, timeout=30):
    return requests.post(f"{PROXY_URL}/suspend", headers={"X-Session-Id": session_id}, timeout=timeout)


def resume(session_id, timeout=60):
    return requests.post(f"{PROXY_URL}/resume", headers={"X-Session-Id": session_id}, timeout=timeout)


def get_save_timings(session_id, timeout=30):
    """Read the VM's last-save per-step timings + sizes (after resume)."""
    resp = requests.get(
        f"{PROXY_URL}/proxy/checkpoint-timings",
        headers={"X-Session-Id": session_id}, timeout=timeout,
    )
    return resp.json()


def wait_responsive(session_id, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{PROXY_URL}/proxy/health", headers={"X-Session-Id": session_id}, timeout=5)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def do_suspend_round(session_id, label):
    """Suspend → confirm SUSPENDED → resume → read last save timings."""
    log(f"  {label}: POST /suspend ...")
    r = suspend(session_id)
    if r.status_code != 200:
        log(f"    ⚠ suspend returned {r.status_code}: {r.text[:200]}")
    # Confirm the VM actually reached SUSPENDED (the /suspend hook — and thus the
    # save — runs during the RUNNING→SUSPENDED transition).
    got = wait_for_state(session_id, "SUSPENDED", timeout=90)
    log(f"    reached SUSPENDED: {got}")
    # Resume so we can read the timings the VM recorded during suspend.
    log(f"  {label}: POST /resume ...")
    resume(session_id)
    wait_for_state(session_id, "RUNNING", timeout=90)
    wait_responsive(session_id)
    timings = get_save_timings(session_id)
    return timings


def do_idle_suspend_round(session_id, label, idle_timeout=60):
    """Wait for the platform to AUTO-suspend the VM after `idle_timeout`s of no
    traffic (the real production trigger), then resume and read save timings.

    IMPORTANT: send NO requests to the VM while waiting — any traffic resets the
    idle clock. We watch state via the control-plane /instances view only (that
    is proxy-side, not VM-endpoint traffic, so it doesn't reset idle).
    """
    log(f"  {label}: going idle — NO traffic for ~{idle_timeout}s, waiting for auto-suspend ...")
    # Give it idle_timeout + generous headroom (we observed ~72s to SUSPENDED).
    got = wait_for_state(session_id, "SUSPENDED", timeout=idle_timeout + 90)
    log(f"    auto-suspended: {got}")
    if not got:
        log("    ⚠ VM did not auto-suspend in time — save timings may be stale")
    log(f"  {label}: POST /resume ...")
    resume(session_id)
    wait_for_state(session_id, "RUNNING", timeout=90)
    wait_responsive(session_id)
    return get_save_timings(session_id)


def print_breakdown(label, timings):
    save = timings.get("last_save", {}) or {}
    sizes = timings.get("last_save_sizes", {}) or {}
    summary = timings.get("last_save_summary", {}) or {}
    order = ["serialize", "upload_pkl", "archive_files", "packages", "metadata", "data_catalog", "dag", "imports"]
    print()
    print(f"  ── {label} — per-step SAVE breakdown ──────────────────")
    print(f"    {'step':<16}{'time(ms)':>12}{'size(KB)':>12}")
    for k in order:
        if k in save:
            ms = save.get(k, 0)
            kb = sizes.get(k, 0) / 1024
            print(f"    {k:<16}{ms:>12.1f}{kb:>12.1f}")
    print(f"    {'-'*40}")
    print(f"    {'TOTAL':<16}{save.get('total_ms', 0):>12.1f}{summary.get('size_kb', 0):>12.1f}")
    print(f"    vars={summary.get('vars')} files={summary.get('files')} "
          f"packages={summary.get('packages')} pkl_kb={summary.get('pkl_kb')}")
    return save, sizes, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="big DataFrame row count")
    args = ap.parse_args()

    print("=" * 72)
    print("  Save-on-Suspend Timing Test — big DataFrames, two suspends")
    print("=" * 72)

    # Preflight: proxy + checkpoint mode
    try:
        health = requests.get(f"{PROXY_URL}/health", timeout=5).json()
    except Exception as e:
        log(f"❌ Proxy not reachable: {e}")
        return 1
    if health.get("persistence_mode") != "checkpoint":
        log(f"❌ Requires persistence_mode=checkpoint, got {health.get('persistence_mode')}")
        return 1
    log(f"Proxy OK — checkpoint mode, region {health.get('region')}")

    session_id = f"suspend-timing-{int(time.time())}"

    # idleTimeoutSeconds=60 (platform minimum) so ROUND 3 can trigger a REAL
    # idle auto-suspend just by going quiet for ~60s. Rounds 1-2 suspend
    # explicitly (fast, deterministic) before the idle timer would fire.
    log(f"Launching VM ({MEMORY_MIB} MiB), session={session_id}, idleTimeout=60s ...")
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "suspend-timing-test",
        "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 60,
        "checkpointEnabled": True,
        "sessionId": session_id,
        "maxDurationSeconds": 1800,
    }, timeout=120)
    assert r.status_code == 200, f"launch failed: {r.text}"
    _launch_json = r.json()
    session_id = _launch_json.get("sessionId", session_id)
    microvm_id = _launch_json.get("microvmId")  # needed to terminate on cleanup
    time.sleep(3)

    results = {}
    try:
        # ── ROUND 1: create a BIG DataFrame + vars ──────────────────────────
        log("")
        log(f"ROUND 1: create big DataFrame ({args.rows:,} rows) + vars ...")
        res = execute_code(session_id,
            "import pandas as pd, numpy as np, hashlib\n"
            "np.random.seed(1)\n"
            f"N = {args.rows}\n"
            "big = pd.DataFrame({\n"
            "    'a': np.random.randn(N),\n"
            "    'b': np.random.randn(N),\n"
            "    'c': np.random.randint(0, 1_000_000, N),\n"
            "})\n"
            "marker = 'ROUND1'\n"
            "counter = 1\n"
            "print(f'big shape={big.shape}, mem={big.memory_usage(deep=True).sum()/1e6:.1f} MB')",
            cell_id="cell-r1", timeout=180,
        )
        assert res.get("success"), f"round1 create failed: {res.get('error')}"
        log(f"  {res['output'].strip()}")

        results["round1"] = do_suspend_round(session_id, "ROUND 1 explicit suspend")

        # ── ROUND 2: mutate/grow state → second save (explicit suspend) ─────
        log("")
        log("ROUND 2: grow the DataFrame + add columns/vars (changes → dirty) ...")
        res = execute_code(session_id,
            "big2 = pd.concat([big, big], ignore_index=True)   # double the rows\n"
            "big2['d'] = big2['a'] * big2['b']                 # extra column\n"
            "marker = 'ROUND2'\n"
            "counter = 2\n"
            "extra = list(range(100000))\n"
            "print(f'big2 shape={big2.shape}, mem={big2.memory_usage(deep=True).sum()/1e6:.1f} MB')",
            cell_id="cell-r2", timeout=180,
        )
        assert res.get("success"), f"round2 mutate failed: {res.get('error')}"
        log(f"  {res['output'].strip()}")

        results["round2"] = do_suspend_round(session_id, "ROUND 2 explicit suspend")

        # ── ROUND 3: mutate, then let it AUTO-suspend on idle (real trigger) ─
        log("")
        log("ROUND 3: make a change, then go idle → wait for AUTOMATIC suspend (60s) ...")
        res = execute_code(session_id,
            "big2['e'] = big2['c'] * 2                          # another change\n"
            "marker = 'ROUND3'\n"
            "counter = 3\n"
            "print(f'big2 shape={big2.shape} (round3 change applied)')",
            cell_id="cell-r3", timeout=180,
        )
        assert res.get("success"), f"round3 mutate failed: {res.get('error')}"
        log(f"  {res['output'].strip()}")

        results["round3"] = do_idle_suspend_round(session_id, "ROUND 3 idle auto-suspend", idle_timeout=60)

        # ── ROUND 4: TINY change on top of the big namespace → delta showcase ─
        # This is the apples-to-apples delta test: the large DataFrames (~200 MB)
        # are UNCHANGED; we only touch one small scalar. A full save would re-upload
        # the whole namespace again; the delta save should upload only a few KB.
        log("")
        log("ROUND 4: change ONE small scalar (big DataFrames untouched) → delta showcase ...")
        res = execute_code(session_id,
            "counter = 4                                        # only a tiny change\n"
            "marker = 'ROUND4'\n"
            "print(f'counter={counter} (big2 untouched, shape={big2.shape})')",
            cell_id="cell-r4", timeout=60,
        )
        assert res.get("success"), f"round4 mutate failed: {res.get('error')}"
        log(f"  {res['output'].strip()}")

        results["round4"] = do_suspend_round(session_id, "ROUND 4 explicit suspend (tiny change)")

        # ── ROUND 5a: create several /tmp files → first file save (all uploaded)
        log("")
        log("ROUND 5a: write several /tmp files (one big) → first file save ...")
        res = execute_code(session_id,
            "import numpy as np, pandas as pd\n"
            "pd.DataFrame({'x': np.arange(2_000_000)}).to_csv('/tmp/bigfile.csv', index=False)\n"
            "open('/tmp/small1.txt','w').write('hello 1')\n"
            "open('/tmp/small2.txt','w').write('hello 2')\n"
            "import os\n"
            "print('files:', {f: os.path.getsize('/tmp/'+f) for f in ['bigfile.csv','small1.txt','small2.txt']})",
            cell_id="cell-r5a", timeout=120,
        )
        assert res.get("success"), f"round5a failed: {res.get('error')}"
        log(f"  {res['output'].strip()}")
        results["round5a"] = do_suspend_round(session_id, "ROUND 5a explicit suspend (all files)")

        # ── ROUND 5b: change ONE small file (bigfile.csv untouched) → file delta
        log("")
        log("ROUND 5b: change ONE small file (bigfile.csv untouched) → file-delta showcase ...")
        res = execute_code(session_id,
            "open('/tmp/small1.txt','w').write('hello 1 CHANGED')\n"
            "print('changed small1.txt only')",
            cell_id="cell-r5b", timeout=60,
        )
        assert res.get("success"), f"round5b failed: {res.get('error')}"
        log(f"  {res['output'].strip()}")
        results["round5b"] = do_suspend_round(session_id, "ROUND 5b explicit suspend (one file changed)")

    finally:
        log("")
        log("Cleanup: terminating session ...")
        try:
            # /terminate REQUIRES the microvmId in the body (it targets a specific
            # VM so a stale terminate can't kill a just-relinked VM). Resolve the
            # session's CURRENT vm (it may have rotated across the idle-suspend/
            # resume cycles), falling back to the launch id.
            vid = None
            try:
                inst = requests.get(f"{PROXY_URL}/instances", timeout=15).json().get("instances", {})
                for _vid, _info in inst.items():
                    if _info.get("session_id") == session_id and \
                       _info.get("state") not in ("TERMINATED", "TERMINATING", "FAILED"):
                        vid = _vid
                        break
            except Exception:
                pass
            vid = vid or microvm_id
            if vid:
                requests.post(
                    f"{PROXY_URL}/terminate",
                    headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                    json={"microvmId": vid}, timeout=30)
        except Exception:
            pass

    # ── Report ──────────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  SAVE-ON-SUSPEND TIMING REPORT (per step, three saves)")
    print("=" * 72)
    s1, z1, sum1 = print_breakdown("SAVE #1 — ROUND 1 (explicit suspend)", results.get("round1", {}))
    s2, z2, sum2 = print_breakdown("SAVE #2 — ROUND 2 (explicit suspend)", results.get("round2", {}))
    s3, z3, sum3 = print_breakdown("SAVE #3 — ROUND 3 (idle AUTO-suspend)", results.get("round3", {}))
    s4, z4, sum4 = print_breakdown("SAVE #4 — ROUND 4 (TINY change — delta showcase)", results.get("round4", {}))

    print()
    print("  ── Summary (total per save) ────────────────────────────")
    for i, (s, summ, kind) in enumerate([
        (s1, sum1, "explicit"), (s2, sum2, "explicit"), (s3, sum3, "idle auto"),
        (s4, sum4, "tiny-delta"),
    ], start=1):
        print(f"  Save #{i} ({kind:<10}): size={summ.get('size_kb')} KB, "
              f"pkl={summ.get('pkl_kb')} KB, total={s.get('total_ms')} ms, "
              f"serialize={s.get('serialize')} ms, upload_pkl={s.get('upload_pkl')} ms")

    # ── Full-save baseline (recorded pre-delta, same test/workload) ──────────
    print()
    print("  ── Full-save BASELINE (pre-delta, for comparison) ──────")
    print("  Save #1 (explicit ): size=46886.6 KB, total=877.1 ms, upload_pkl=661.5 ms")
    print("  Save #2 (explicit ): size=172247.5 KB, total=2058.4 ms, upload_pkl=1794.0 ms")
    print("  Save #3 (idle auto): size=203497.6 KB, total=2486.9 ms, upload_pkl=2194.4 ms")
    print("  (Baseline re-uploaded the ENTIRE namespace on every suspend.)")

    # Sanity + delta checks:
    #  - #2 should be larger than #1 (round 2 doubled the rows + added a column).
    #  - #3 proves the idle auto-suspend path also checkpoints (the production trigger).
    #  - #4 is the DELTA WIN: a tiny change on top of ~200 MB should upload only KB.
    print()
    if sum1 and sum2:
        grew = sum2.get("size_kb", 0) > sum1.get("size_kb", 0)
        print(f"  {'✓' if grew else '⚠'} Save #2 > Save #1 (round 2 doubled data): {grew}")
    if sum3:
        auto_saved = (s3.get("total_ms", 0) or 0) > 0 and sum3.get("size_kb")
        print(f"  {'✓' if auto_saved else '⚠'} Save #3 happened via IDLE AUTO-SUSPEND "
              f"(production trigger): {bool(auto_saved)}")
    if sum4 and sum3:
        # upload_pkl SIZE column = bytes actually uploaded this save (delta bytes in delta mode).
        d4 = z4.get("upload_pkl", 0)
        d3 = z3.get("upload_pkl", 0)
        # Delta win: round-4 uploaded far fewer bytes than round-3 (big DataFrames unchanged).
        tiny = d4 < d3 * 0.10 if d3 else False  # <10% of the previous full-ish save
        print(f"  {'✓' if tiny else '❌'} Save #4 (tiny var change) uploaded only "
              f"{d4/1024:.1f} KB vs Save #3's {d3/1024:.1f} KB — var-delta avoided a full re-upload: {tiny}")
        if d3:
            print(f"      → var-delta uploaded {100.0*d4/d3:.2f}% of the prior save's bytes")

    # ── FILE DELTA check: ROUND 5b changed one small file; bigfile.csv untouched.
    # archive_files SIZE = bytes uploaded for /tmp files this save (delta in delta mode).
    r5a = results.get("round5a", {}); r5b = results.get("round5b", {})
    z5a = (r5a.get("last_save_sizes") or {}); z5b = (r5b.get("last_save_sizes") or {})
    fa = z5a.get("archive_files", 0); fb = z5b.get("archive_files", 0)
    print()
    print("  ── File-delta (ROUND 5a first save vs 5b one-file-changed) ──")
    print(f"    5a files uploaded: {fa/1024:.1f} KB | 5b files uploaded: {fb/1024:.1f} KB")
    if fa:
        file_tiny = fb < fa * 0.10
        print(f"  {'✓' if file_tiny else '❌'} Save 5b (one small file changed) uploaded only "
              f"{fb/1024:.1f} KB vs 5a's {fa/1024:.1f} KB — file-delta skipped the big CSV: {file_tiny}")
        print(f"      → file-delta uploaded {100.0*fb/fa:.2f}% of the first file-save's bytes")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
