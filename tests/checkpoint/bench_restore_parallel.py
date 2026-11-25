#!/usr/bin/env python3
# test-kind: manual
"""
Benchmark: restore-path S3 download latency (parallel GETs).

Creates a checkpoint with MANY LARGE variables + SEVERAL LARGE files (each > the
64 KB DELTA_SMALL_VAR_BYTES threshold, so each lands as its OWN S3 object — the
case parallel GETs actually help). Saves via explicit /terminate (fires the
/terminate hook → S3 checkpoint), then launches a fresh VM restoring from that
session and reads GET /proxy/checkpoint-timings for the real in-VM restore
breakdown (download_pkl / download_files / deserialize / total_ms).

Run it TWICE against the SAME checkpoint to compare parallel vs sequential:
  1) with the shipped parallel code (ThreadPoolExecutor max_workers=16), and
  2) with max_workers temporarily forced to 1 (sequential baseline),
by passing --restore-only --session <id> on the second run so no new checkpoint
is created (apples-to-apples: identical S3 objects, only the download concurrency
differs between the two VM images).

Usage:
  # Full run: build a big checkpoint, then restore + report timings
  python3 tests/checkpoint/bench_restore_parallel.py --n-large-vars 24 --var-kb 400 --n-large-files 8 --file-kb 500

  # Restore-only against an existing session (for the sequential baseline image)
  python3 tests/checkpoint/bench_restore_parallel.py --restore-only --session <session_id>
Env:
  TEST_ARCH   unset (default) = proxy default arch (arm64); set to x86_64 to
              benchmark on x86_64 (asserts each launch reports that arch)
"""

import argparse
import time
import json
import boto3
import requests
import os
import re
import subprocess

PROXY_URL = "http://localhost:8081"

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
# One fresh connection per call, exactly like the module-level requests.get/post
# this replaces: a pooled keep-alive socket can be closed by the server's idle
# keep-alive timeout just as a ~5s poll reuses it (RemoteDisconnected).
_session.headers["Connection"] = "close"
if _tok:
    _session.headers["X-Proxy-Auth"] = _tok
else:
    print("WARNING: no proxy auth token (set PROXY_AUTH_TOKEN, or run the proxy on :8081) "
          "— proxy calls will fail with 401", flush=True)
requests = _session  # noqa: F811 — route all requests.get/post through the authed session

AWS_REGION = "us-west-2"
MEMORY_MIB = 4096  # headroom for the big namespace
# Opt-in arch: unset = no "arch" in the /launch body = the proxy default (arm64).
TEST_ARCH = os.environ.get("TEST_ARCH", "").strip()
_ARCH_BODY = {"arch": TEST_ARCH} if TEST_ARCH else {}
def _assert_arch(data, label):
    """When TEST_ARCH is set, the launch must report that arch."""
    if TEST_ARCH:
        assert data.get("architecture") == TEST_ARCH, \
            f"{label} ran on {data.get('architecture')!r}, expected TEST_ARCH={TEST_ARCH!r}"


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def execute_code(session_id, code, timeout=180, cell_id=None):
    body = {"code": code}
    if cell_id is not None:
        body["cell_id"] = cell_id
    r = requests.post(
        f"{PROXY_URL}/proxy/execute",
        headers={"Content-Type": "application/json", "X-Session-Id": session_id},
        json=body, timeout=timeout,
    )
    return r.json()


def wait_for_running(microvm_id, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        inst = requests.get(f"{PROXY_URL}/instances").json().get("instances", {}).get(microvm_id, {})
        if inst.get("state") == "RUNNING":
            return
        time.sleep(3)
    raise TimeoutError(f"{microvm_id} not RUNNING in {timeout}s")


def wait_for_terminated(microvm_id, timeout=120):
    c = boto3.client("lambda-microvms", region_name=AWS_REGION)
    start = time.time()
    while time.time() - start < timeout:
        try:
            if c.get_microvm(microvmIdentifier=microvm_id).get("state") == "TERMINATED":
                return
        except Exception:
            return
        time.sleep(3)
    raise TimeoutError(f"{microvm_id} not TERMINATED in {timeout}s")


def wait_responsive(session_id, timeout=90):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if requests.get(f"{PROXY_URL}/proxy/health",
                            headers={"X-Session-Id": session_id}, timeout=5).ok:
                return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"session {session_id} not responsive in {timeout}s")


def build_checkpoint(args):
    session_id = f"bench-restore-{int(time.time())}"
    log(f"Launching checkpoint VM ({MEMORY_MIB} MiB), session={session_id} ...")
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "bench-restore-build",
        "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 1800,
        "checkpointEnabled": True,
        "sessionId": session_id,
        "maxDurationSeconds": 1800,
        **_ARCH_BODY,
    }, timeout=120)
    assert r.status_code == 200, f"launch failed: {r.text}"
    _assert_arch(r.json(), "checkpoint launch")
    vm_id = r.json()["microvmId"]
    session_id = r.json().get("sessionId", session_id)
    wait_for_running(vm_id)
    time.sleep(2)

    # Create N_LARGE_VARS vars that each pickle to > 64 KB (own S3 object).
    # A float64 numpy array of var_kb*1024/8 elements is ~var_kb KB; well over the
    # 64 KB threshold at the default 400 KB, so each becomes its own object.
    log(f"Creating {args.n_large_vars} large vars (~{args.var_kb} KB each) "
        f"+ {args.n_large_files} large files (~{args.file_kb} KB each) ...")
    res = execute_code(session_id,
        "import numpy as np, pandas as pd, os\n"
        f"N_VARS = {args.n_large_vars}\n"
        f"VAR_ELEMS = {args.var_kb} * 1024 // 8\n"
        "np.random.seed(7)\n"
        "for i in range(N_VARS):\n"
        "    globals()[f'bigvar_{i}'] = np.random.randn(VAR_ELEMS)\n"
        f"N_FILES = {args.n_large_files}\n"
        f"FILE_ROWS = {args.file_kb} * 1024 // 16\n"
        "for i in range(N_FILES):\n"
        "    pd.DataFrame({'x': np.arange(FILE_ROWS), 'y': np.random.randn(FILE_ROWS)})"
        ".to_csv(f'/tmp/bigfile_{i}.csv', index=False)\n"
        "nbytes = sum(globals()[f'bigvar_{i}'].nbytes for i in range(N_VARS))\n"
        "fbytes = sum(os.path.getsize(f'/tmp/bigfile_{i}.csv') for i in range(N_FILES))\n"
        "print(f'vars_total_mb={nbytes/1e6:.1f} files_total_mb={fbytes/1e6:.1f}')",
        cell_id="cell-bench", timeout=240,
    )
    assert res.get("success"), f"state creation failed: {res.get('error')}"
    log(f"  {res['output'].strip()}")

    # Install real packages so the restore-side pip step does meaningful work
    # (this is what the pip-overlap optimization — item #3 — is meant to speed up).
    if args.pip:
        pkgs = [p.strip() for p in args.pip.split(",") if p.strip()]
        log(f"Installing {len(pkgs)} packages into the checkpoint session: {', '.join(pkgs)} ...")
        for pkg in pkgs:
            r = requests.post(f"{PROXY_URL}/proxy/install",
                              headers={"Content-Type": "application/json", "X-Session-Id": session_id},
                              json={"package": pkg}, timeout=300)
            ok = r.json().get("success") if r.ok else False
            log(f"  install {pkg}: {'ok' if ok else 'FAILED ' + str(r.text[:120])}")

    log("Terminating RUNNING VM (proxy save-before-terminate → S3 checkpoint) ...")
    # /terminate REQUIRES microvmId in the body (closes the close↔reopen race) and
    # the proxy checkpoints to S3 before killing the VM. Header alone → 400.
    tr = requests.post(f"{PROXY_URL}/terminate",
                       headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                       json={"microvmId": vm_id}, timeout=120)
    assert tr.status_code == 200, f"terminate failed ({tr.status_code}): {tr.text[:300]}"
    wait_for_terminated(vm_id, timeout=180)
    time.sleep(3)  # S3 consistency settle
    log(f"  Checkpoint saved for session: {session_id}")
    return session_id


def restore_and_time(restore_from, label):
    new_session = f"{restore_from}-restored-{int(time.time())}"
    log(f"[{label}] Launching restore VM from session {restore_from} ...")
    t0 = time.time()
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "bench-restore-run",
        "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 1800,
        "checkpointEnabled": False,
        "sessionId": new_session,
        "restoreFromSession": restore_from,
        **_ARCH_BODY,
    }, timeout=180)
    assert r.status_code == 200, f"restore launch failed: {r.text}"
    _assert_arch(r.json(), f"[{label}] restore launch")
    vm_id = r.json()["microvmId"]
    wait_for_running(vm_id)
    wait_responsive(new_session)
    wall = time.time() - t0

    rt = {}
    try:
        resp = requests.get(f"{PROXY_URL}/proxy/checkpoint-timings",
                            headers={"X-Session-Id": new_session}, timeout=15)
        if resp.ok:
            rt = resp.json().get("last_restore", {}) or {}
    except Exception as e:
        log(f"  Could not fetch timings: {e}")

    # Sanity: confirm a large var actually restored
    chk = execute_code(new_session, "print('ok' if 'bigvar_0' in globals() else 'MISSING')")
    restored_ok = "ok" in chk.get("output", "")

    requests.post(f"{PROXY_URL}/terminate",
                  headers={"X-Session-Id": new_session, "Content-Type": "application/json"},
                  json={"microvmId": vm_id}, timeout=60)
    return {"label": label, "wall_s": wall, "restore_timings_ms": rt,
            "restored_ok": restored_ok, "vm_id": vm_id, "session": new_session}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-large-vars", type=int, default=24)
    ap.add_argument("--var-kb", type=int, default=400)
    ap.add_argument("--n-large-files", type=int, default=8)
    ap.add_argument("--file-kb", type=int, default=500)
    ap.add_argument("--pip", default="", help="comma-separated packages to install in the checkpoint (e.g. 'tabulate,humanize,scikit-learn')")
    ap.add_argument("--restore-only", action="store_true",
                    help="skip building; restore from --session (for the sequential baseline run)")
    ap.add_argument("--session", default=None, help="existing session id to restore from")
    ap.add_argument("--label", default="parallel", help="label for this run (e.g. parallel / sequential)")
    args = ap.parse_args()

    try:
        health = requests.get(f"{PROXY_URL}/health", timeout=5).json()
    except Exception as e:
        log(f"Proxy not reachable: {e}")
        return 1
    log(f"Proxy OK — mode={health.get('persistence_mode')} region={health.get('region')}")

    if args.restore_only:
        assert args.session, "--restore-only requires --session <id>"
        session_id = args.session
    else:
        session_id = build_checkpoint(args)

    res = restore_and_time(session_id, args.label)

    rt = res["restore_timings_ms"]
    print()
    print("=" * 64)
    print(f"  RESTORE BENCHMARK — {res['label']}")
    print("=" * 64)
    print(f"  Source session:     {session_id}")
    print(f"  Restored OK:        {res['restored_ok']}")
    print(f"  Wall (launch→ready):{res['wall_s']:.1f}s")
    print(f"  ── in-VM restore breakdown (ms) ──")
    print(f"    download_pkl:   {rt.get('download_pkl', 0):.0f}")
    print(f"    deserialize:    {rt.get('deserialize', 0):.0f}")
    print(f"    download_files: {rt.get('download_files', 0):.0f}")
    print(f"    extract_files:  {rt.get('extract_files', 0):.0f}")
    print(f"    packages:       {rt.get('packages', 0):.0f}")
    print(f"    TOTAL:          {rt.get('total_ms', 0):.0f}")
    print("=" * 64)
    print(f"  To compare sequential, rebuild images with max_workers=1 and run:")
    print(f"    python3 tests/checkpoint/bench_restore_parallel.py --restore-only "
          f"--session {session_id} --label sequential")
    print()
    print("RESULT_JSON " + json.dumps({"label": res["label"], "session": session_id,
          "wall_s": res["wall_s"], "restore_timings_ms": rt, "restored_ok": res["restored_ok"]}))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
