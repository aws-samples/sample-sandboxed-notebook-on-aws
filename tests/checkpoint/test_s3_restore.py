#!/usr/bin/env python3
# test-kind: live
"""
End-to-End Test: MicroVM Session Checkpoint & Restore via S3
(explicit-terminate path — VM RUNNING at terminate)

This script:
1. Launches a MicroVM with checkpoint enabled
2. Creates state (variables, DataFrames, local files, packages)
3. Explicitly terminates the RUNNING MicroVM (POST /terminate) → /terminate hook
   fires and saves the checkpoint to S3
4. Validates the checkpoint exists in S3
5. Launches a new MicroVM with restore from the checkpoint
6. Validates all state was restored correctly
7. Reports timing for each operation

STATE PERSISTENCE MODEL (see app/platform/hooks.py):
  State is saved by TWO hooks, gated by a dirty-flag:
    • /terminate — fires only when the VM is RUNNING at kill time. This test
                   hits that path via an explicit POST /terminate while RUNNING.
    • /suspend   — the save point for idle VMs (a VM killed while SUSPENDED
                   never fires /terminate). Not exercised here — this test
                   terminates the VM directly without letting it suspend.

Usage:
    python3 tests/checkpoint/test_s3_restore.py
Env:
    TEST_ARCH   unset (default) = proxy default arch (arm64); set to x86_64 to
                launch every VM on x86_64 and assert the launches report it
"""

import time
import json
import boto3
import requests
import os
import re
import subprocess

# --- Configuration ---
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
MEMORY_MIB = 2048  # Use smallest tier for faster test
# Opt-in arch (LIVE_TEST_X86=1 tests/run_tests.sh exports TEST_ARCH=x86_64).
# Unset = no "arch" in the /launch body = the proxy default (arm64).
TEST_ARCH = os.environ.get("TEST_ARCH", "").strip()
_ARCH_BODY = {"arch": TEST_ARCH} if TEST_ARCH else {}
def _assert_arch(data, label):
    """When TEST_ARCH is set, the launch must report that arch."""
    if TEST_ARCH:
        assert data.get("architecture") == TEST_ARCH, \
            f"{label} ran on {data.get('architecture')!r}, expected TEST_ARCH={TEST_ARCH!r}"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def artifacts_bucket():
    """The checkpoint S3 bucket: microvm-sandbox-artifacts-{account}-{region}."""
    account = boto3.client("sts").get_caller_identity()["Account"]
    return f"microvm-sandbox-artifacts-{account}-{AWS_REGION}"


def s3_get_text(bucket, key):
    """Fetch an S3 object as text, or None if it doesn't exist (404)."""
    s3 = boto3.client("s3", region_name=AWS_REGION)
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        # HeadObject-style 404 surfaces as ClientError, not NoSuchKey, on get_object
        if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def timed(label):
    """Context manager to time operations."""
    class Timer:
        def __init__(self):
            self.elapsed = 0
        def __enter__(self):
            self.start = time.time()
            return self
        def __exit__(self, *args):
            self.elapsed = time.time() - self.start
            log(f"  ⏱  {label}: {self.elapsed:.1f}s")
    return Timer()


def wait_for_running(microvm_id, timeout=90):
    """Poll until MicroVM is RUNNING."""
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(f"{PROXY_URL}/instances")
        instances = resp.json().get("instances", {})
        inst = instances.get(microvm_id, {})
        if inst.get("state") == "RUNNING":
            return inst
        time.sleep(3)
    raise TimeoutError(f"MicroVM {microvm_id} did not reach RUNNING in {timeout}s")


def wait_for_terminated(microvm_id, timeout=90):
    """Poll until MicroVM is TERMINATED."""
    client = boto3.client("lambda-microvms", region_name=AWS_REGION)
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = client.get_microvm(microvmIdentifier=microvm_id)
            if resp.get("state") == "TERMINATED":
                return
        except Exception:
            return  # Might get ResourceNotFound after termination
        time.sleep(3)
    raise TimeoutError(f"MicroVM {microvm_id} did not terminate in {timeout}s")


def execute_code(session_id, code, timeout=60, cell_id=None):
    """Execute code on the MicroVM via the proxy using session_id only.
    Pass cell_id to record variable provenance (which cell created/modified vars)."""
    body = {"code": code}
    if cell_id is not None:
        body["cell_id"] = cell_id
    resp = requests.post(
        f"{PROXY_URL}/proxy/execute",
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": session_id,
        },
        json=body,
        timeout=timeout,
    )
    return resp.json()


def get_variables(session_id, timeout=30):
    """Fetch the namespace variables (incl. provenance) via the proxy."""
    resp = requests.get(
        f"{PROXY_URL}/proxy/variables",
        headers={"X-Session-Id": session_id},
        timeout=timeout,
    )
    return resp.json().get("variables", {})


def install_package(session_id, package, timeout=120):
    """Install a package via /install. Returns JSON incl. `installed_spec`
    (resolved name==version, persisted for reproducible restore)."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/install",
        headers={"Content-Type": "application/json", "X-Session-Id": session_id},
        json={"package": package},
        timeout=timeout,
    )
    return resp.json()


def uninstall_package(session_id, package, timeout=60):
    """Uninstall a package via /install (recorded so it is NOT reinstalled on restore)."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/install",
        headers={"Content-Type": "application/json", "X-Session-Id": session_id},
        json={"package": package, "uninstall": True},
        timeout=timeout,
    )
    return resp.json()


def main():
    print("=" * 60)
    print("  MicroVM Session Checkpoint & Restore — E2E Test")
    print("=" * 60)
    print()

    timings = {}

    # ================================================================
    # PHASE 0: Baseline — Launch a bare VM (no checkpoint, no restore)
    # ================================================================
    log("PHASE 0: Baseline — launching bare VM (no checkpoint, no restore)...")

    with timed("Bare VM launch") as t:
        resp = requests.post(f"{PROXY_URL}/launch", json={
            "name": "baseline-timing",
            "memoryMiB": MEMORY_MIB,
            "idleTimeoutSeconds": 60,
            "checkpointEnabled": False,
            **_ARCH_BODY,
        })
        baseline_data = resp.json()
        assert "microvmId" in baseline_data, f"Baseline launch failed: {baseline_data}"
        _assert_arch(baseline_data, "Baseline launch")
        baseline_vm_id = baseline_data["microvmId"]
        baseline_session_id = baseline_data.get("sessionId", "")
    timings["bare_launch"] = t.elapsed

    # Quick health check to confirm it's responsive
    with timed("Bare VM first response") as t:
        headers = {"X-Session-Id": baseline_session_id}
        for _ in range(10):
            try:
                r = requests.get(f"{PROXY_URL}/proxy/health", headers=headers, timeout=5)
                if r.ok:
                    break
            except:
                pass
            time.sleep(1)
    timings["bare_first_response"] = t.elapsed

    log(f"  Bare VM: {baseline_vm_id}")
    log(f"  Launch: {timings['bare_launch']:.1f}s, First response: {timings['bare_first_response']:.1f}s")

    # Terminate baseline VM (/terminate requires microvmId in the body)
    requests.post(f"{PROXY_URL}/terminate",
                  headers={"X-Session-Id": baseline_session_id, "Content-Type": "application/json"},
                  json={"microvmId": baseline_vm_id})
    log(f"  Terminated baseline VM")
    log("")

    # ================================================================
    # PHASE 1: Launch MicroVM with checkpoint enabled
    # ================================================================
    log("PHASE 1: Launching MicroVM with checkpoint enabled...")

    with timed("Launch MicroVM") as t:
        session_id = f"test-checkpoint-{int(time.time())}"
        resp = requests.post(f"{PROXY_URL}/launch", json={
            "name": "test-checkpoint",
            "memoryMiB": MEMORY_MIB,
            "idleTimeoutSeconds": 1800,
            "checkpointEnabled": True,
            "sessionId": session_id,
            **_ARCH_BODY,
        })
        launch_data = resp.json()
        assert "microvmId" in launch_data, f"Launch failed: {launch_data}"
        _assert_arch(launch_data, "Launch")
        microvm_id = launch_data["microvmId"]
        endpoint = launch_data["endpoint"]
    timings["launch"] = t.elapsed

    log(f"  MicroVM ID: {microvm_id}")
    log(f"  Endpoint:   {endpoint}")
    log(f"  Session ID: {session_id}")

    # Wait for it to be fully running
    with timed("Wait for RUNNING") as t:
        wait_for_running(microvm_id)
    timings["wait_running"] = t.elapsed

    # Give the /run hook a moment to complete
    time.sleep(2)

    # ================================================================
    # PHASE 2: Create state (variables, DataFrame, local file)
    # ================================================================
    log("")
    log("PHASE 2: Creating state in the MicroVM...")

    result = execute_code(session_id, """
import pandas as pd
import numpy as np

# Create some variables
x = 42
message = "Hello from checkpoint test!"
numbers = list(range(1, 101))

# Create a DataFrame
df = pd.DataFrame({
    'name': ['Alice', 'Bob', 'Charlie', 'Diana', 'Eve'],
    'score': [95, 87, 92, 78, 99],
    'city': ['NYC', 'SF', 'LA', 'Chicago', 'Boston']
})

# Create a numpy array
matrix = np.random.rand(10, 10)

print(f"Created: x={x}, message='{message}', len(numbers)={len(numbers)}")
print(f"DataFrame shape: {df.shape}")
print(f"Matrix shape: {matrix.shape}")
""", cell_id="cell-setup")
    assert result.get("success"), f"Execution failed: {result.get('error')}"
    log(f"  Variables created: {result.get('output', '').strip()}")

    # Create a local file in /tmp
    result = execute_code(session_id, """
import csv

# Write a CSV file to /tmp
data = [
    ['id', 'product', 'price'],
    [1, 'Widget', 9.99],
    [2, 'Gadget', 19.99],
    [3, 'Doohickey', 4.99],
    [4, 'Thingamajig', 14.99],
    [5, 'Whatchamacallit', 7.99],
]

with open('/tmp/test_products.csv', 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerows(data)

print("Created /tmp/test_products.csv (5 products)")
""")
    assert result.get("success"), f"File creation failed: {result.get('error')}"
    log(f"  {result.get('output', '').strip()}")

    # Verify state
    result = execute_code(session_id, """
import os
print(f"Variables: x={x}, message='{message[:20]}...', df.shape={df.shape}")
print(f"File exists: {os.path.exists('/tmp/test_products.csv')}")
print(f"File size: {os.path.getsize('/tmp/test_products.csv')} bytes")
""")
    log(f"  State verification: {result.get('output', '').strip()}")

    # Install a package (pinned to its resolved version) + install-then-uninstall a
    # throwaway one, to exercise version pinning and uninstall tracking across restore.
    log("  Installing tabulate (pinned) + install/uninstall humanize...")
    ir = install_package(session_id, "tabulate")
    assert ir.get("success"), f"tabulate install failed: {ir.get('error')}"
    installed_spec = ir.get("installed_spec", "")
    assert installed_spec.startswith("tabulate=="), \
        f"Expected pinned spec 'tabulate==x.y.z', got {installed_spec!r}"
    installed_pkg_version = installed_spec.split("==", 1)[1]
    log(f"    Install spec: {installed_spec!r}")
    hr = install_package(session_id, "humanize")
    assert hr.get("success"), f"humanize install failed: {hr.get('error')}"
    ur = uninstall_package(session_id, "humanize")
    assert ur.get("success"), f"humanize uninstall failed: {ur.get('error')}"

    # ── EAGER PACKAGE FLUSH: requirements.txt must be written to S3 the instant a
    #    package is installed/uninstalled — BEFORE any suspend/terminate checkpoint.
    #    (Regression guard: a package installed then lost when its capturing
    #    checkpoint is interrupted. flush_packages() writes it immediately.)
    bucket = artifacts_bucket()
    reqs_key = f"sessions/{session_id}/requirements.txt"
    log(f"  Verifying eager package flush at s3://{bucket}/{reqs_key} (no checkpoint yet)...")
    time.sleep(1)  # tiny settle for S3 read-after-write
    reqs = s3_get_text(bucket, reqs_key)
    assert reqs is not None, \
        "requirements.txt was NOT flushed to S3 at install time (eager flush_packages missing)"
    reqs_lines = [l.strip() for l in reqs.splitlines() if l.strip()]
    assert any(l.startswith("tabulate==") for l in reqs_lines), \
        f"tabulate not in eagerly-flushed requirements.txt: {reqs_lines}"
    assert not any(l.startswith("humanize") for l in reqs_lines), \
        f"uninstalled 'humanize' must NOT be in requirements.txt: {reqs_lines}"
    log(f"  ✅ Eager package flush verified (requirements.txt = {reqs_lines})")

    log(f"  Session ID: {session_id}")
    restore_session_id = session_id

    # ================================================================
    # PHASE 2b: Verify data catalog discovered the local file
    # ================================================================
    log("")
    log("PHASE 2b: Triggering local file catalog refresh + verify...")
    # Trigger refresh so the catalog picks up the new file
    try:
        requests.post(f"{PROXY_URL}/proxy/data-catalog/refresh-local", headers={"X-Session-Id": session_id}, timeout=5)
    except Exception:
        pass
    time.sleep(3)  # Give background scan time
    try:
        catalog_resp = requests.get(f"{PROXY_URL}/datasources/catalog", headers={"X-Session-Id": session_id}, timeout=10)
        if catalog_resp.status_code == 200:
            catalog = catalog_resp.json()
            entries = catalog.get("entries", [])
            local_entries = [e for e in entries if e.get("source_type") == "local"]
            log(f"  Catalog total: {catalog.get('total', 0)} sources, local files: {len(local_entries)}")
            products_entry = next((e for e in local_entries if "test_products" in e.get("source_id", "")), None)
            if products_entry and products_entry.get("status") == "discovered":
                cols = [c["name"] for c in products_entry.get("columns", [])]
                log(f"  \u2705 test_products.csv discovered: columns={cols}")
            else:
                log(f"  \u26a0 test_products.csv not yet discovered (status={products_entry.get('status') if products_entry else 'missing'})")
        else:
            log(f"  \u26a0 Catalog endpoint returned {catalog_resp.status_code}")
    except Exception as e:
        log(f"  \u26a0 Could not check catalog: {e}")

    # ── EAGER CATALOG FLUSH: data_catalog.json must be written to S3 when discovery
    #    (or a local-file refresh) completes — BEFORE any suspend/terminate. The
    #    refresh above fires the discovery-complete callback → flush_data_catalog().
    catalog_key = f"sessions/{session_id}/data_catalog.json"
    log(f"  Verifying eager catalog flush at s3://{bucket}/{catalog_key} (no checkpoint yet)...")
    catalog_flushed = None
    for _ in range(8):  # poll up to ~8s: refresh scan + callback run asynchronously
        body = s3_get_text(bucket, catalog_key)
        if body:
            catalog_flushed = body
            break
        time.sleep(1)
    if catalog_flushed is not None:
        try:
            cj = json.loads(catalog_flushed)
            n = cj.get("total", len(cj.get("entries", [])))
        except Exception:
            n = "?"
        log(f"  ✅ Eager catalog flush verified (data_catalog.json in S3, {n} entries)")
    else:
        # Non-fatal: the catalog eager-flush is a perf/robustness optimization, not a
        # correctness guarantee (restore can re-discover). Warn loudly but don't fail
        # the whole restore test on it.
        log("  ⚠ data_catalog.json NOT eagerly flushed to S3 (flush_data_catalog may not be wired)")

    # ================================================================
    # PHASE 3: Terminate MicroVM (triggers checkpoint)
    # ================================================================
    log("")
    log("PHASE 3: Terminating RUNNING MicroVM (explicit /terminate → fires hook → S3 checkpoint)...")

    with timed("Terminate MicroVM") as t:
        resp = requests.post(f"{PROXY_URL}/terminate",
                             headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                             json={"microvmId": microvm_id})
        assert resp.status_code == 200, f"Terminate failed: {resp.text}"
    timings["terminate_request"] = t.elapsed

    with timed("Wait for TERMINATED") as t:
        wait_for_terminated(microvm_id)
    timings["wait_terminated"] = t.elapsed

    # ================================================================
    # PHASE 4: Validate checkpoint in S3
    # ================================================================
    log("")
    log("PHASE 4: Validating checkpoint in S3...")

    time.sleep(3)  # Give S3 a moment for consistency

    with timed("Check S3 checkpoint") as t:
        resp = requests.get(f"{PROXY_URL}/sessions")
        sessions = resp.json().get("sessions", [])
    timings["list_sessions"] = t.elapsed

    log(f"  Found {len(sessions)} session(s) in S3:")
    for s in sessions:
        log(f"    - {s['session_id']} (vars: {s.get('variables_count', '?')}, files: {s.get('files_count', '?')})")

    our_session = None
    for s in sessions:
        if "test-checkpoint" in s["session_id"]:
            our_session = s
            break

    save_timings_ms = {}
    if our_session:
        log(f"  ✅ Checkpoint found: {our_session['session_id']}")
        log(f"     Variables: {our_session.get('variables_count', '?')}")
        log(f"     Files: {our_session.get('files_count', '?')}")
        log(f"     Checkpointed at: {our_session.get('checkpointed_at', '?')}")
        save_timings_ms = our_session.get('save_timings_ms', {})
        if save_timings_ms:
            log(f"     Save breakdown (inside VM):")
            log(f"       Serialize (dill):     {save_timings_ms.get('serialize', 0):.0f}ms")
            log(f"       Upload checkpoint.pkl:{save_timings_ms.get('upload_pkl', 0):.0f}ms")
            log(f"       Archive files:        {save_timings_ms.get('archive_files', 0):.0f}ms")
            log(f"       Save package list:    {save_timings_ms.get('packages', 0):.0f}ms")
            log(f"       Upload metadata:      {save_timings_ms.get('metadata', 0):.0f}ms")
            log(f"       TOTAL (inside VM):    {sum(save_timings_ms.values()):.0f}ms")
        restore_session_id = our_session["session_id"]
    else:
        log("  ❌ No checkpoint found for our session!")
        log("     This may mean the terminate hook didn't fire or checkpoint failed.")
        log("     Check the MicroVM logs for errors.")
        print("\n" + "=" * 60)
        print("  TEST RESULT: FAILED (no checkpoint)")
        print("=" * 60)
        return

    # ================================================================
    # PHASE 5: Launch new MicroVM with restore
    # ================================================================
    log("")
    log("PHASE 5: Launching new MicroVM with session restore...")

    with timed("Launch MicroVM (with restore)") as t:
        restore_session_id_new = f"{restore_session_id}-restored-{int(time.time())}"
        resp = requests.post(f"{PROXY_URL}/launch", json={
            "name": "test-restore",
            "memoryMiB": MEMORY_MIB,
            "idleTimeoutSeconds": 1800,
            "checkpointEnabled": False,
            "sessionId": restore_session_id_new,
            "restoreFromSession": restore_session_id,
            **_ARCH_BODY,
        })
        restore_data = resp.json()
        assert "microvmId" in restore_data, f"Restore launch failed: {restore_data}"
        _assert_arch(restore_data, "Restore launch")
        restore_microvm_id = restore_data["microvmId"]
    timings["launch_restore"] = t.elapsed

    log(f"  New MicroVM ID: {restore_microvm_id}")

    with timed("Wait for RUNNING (restore)") as t:
        wait_for_running(restore_microvm_id)
    timings["wait_running_restore"] = t.elapsed

    # Measure time until restored VM responds
    with timed("Restore VM first response") as t:
        headers = {"X-Session-Id": restore_session_id_new}
        for _ in range(20):
            try:
                r = requests.get(f"{PROXY_URL}/proxy/health", headers=headers, timeout=5)
                if r.ok:
                    break
            except:
                pass
            time.sleep(1)
    timings["restore_first_response"] = t.elapsed

    # ================================================================
    # PHASE 6: Validate restored state
    # ================================================================
    log("")
    log("PHASE 6: Validating restored state...")

    checks_passed = 0
    checks_total = 0

    # Check variables
    result = execute_code(restore_session_id_new, "print(f'x={x}')")
    checks_total += 1
    if result.get("success") and "x=42" in result.get("output", ""):
        log("  ✅ Variable 'x' restored: x=42")
        checks_passed += 1
    else:
        log(f"  ❌ Variable 'x' NOT restored: {result.get('error') or result.get('output')}")

    result = execute_code(restore_session_id_new, "print(f'message={message}')")
    checks_total += 1
    if result.get("success") and "Hello from checkpoint test" in result.get("output", ""):
        log("  ✅ Variable 'message' restored")
        checks_passed += 1
    else:
        log(f"  ❌ Variable 'message' NOT restored: {result.get('error') or result.get('output')}")

    result = execute_code(restore_session_id_new, "print(f'len(numbers)={len(numbers)}')")
    checks_total += 1
    if result.get("success") and "len(numbers)=100" in result.get("output", ""):
        log("  ✅ Variable 'numbers' restored (100 items)")
        checks_passed += 1
    else:
        log(f"  ❌ Variable 'numbers' NOT restored: {result.get('error') or result.get('output')}")

    # Check DataFrame
    result = execute_code(restore_session_id_new, "print(f'df.shape={df.shape}, columns={list(df.columns)}')")
    checks_total += 1
    if result.get("success") and "(5, 3)" in result.get("output", ""):
        log("  ✅ DataFrame 'df' restored: (5, 3)")
        checks_passed += 1
    else:
        log(f"  ❌ DataFrame 'df' NOT restored: {result.get('error') or result.get('output')}")

    # Check numpy array
    result = execute_code(restore_session_id_new, "print(f'matrix.shape={matrix.shape}')")
    checks_total += 1
    if result.get("success") and "(10, 10)" in result.get("output", ""):
        log("  ✅ Numpy array 'matrix' restored: (10, 10)")
        checks_passed += 1
    else:
        log(f"  ❌ Numpy array 'matrix' NOT restored: {result.get('error') or result.get('output')}")

    # Check package restore: installed pkg present + version pinned; uninstalled pkg absent.
    result = execute_code(restore_session_id_new,
        "try:\n"
        "    import tabulate\n"
        "    print(f'tabulate_ok=True, version={tabulate.__version__}')\n"
        "except ImportError as e:\n"
        "    print(f'tabulate_ok=False, error={e}')"
    )
    pkg_out = result.get("output", "")
    checks_total += 1
    if result.get("success") and "tabulate_ok=True" in pkg_out:
        log("  ✅ Package 'tabulate' restored")
        checks_passed += 1
    else:
        log(f"  ❌ Package 'tabulate' NOT restored: {result.get('error') or pkg_out}")
    checks_total += 1
    if f"version={installed_pkg_version}" in pkg_out:
        log(f"  ✅ Package version pinned on restore: {installed_pkg_version}")
        checks_passed += 1
    else:
        log(f"  ❌ Package version NOT pinned: expected {installed_pkg_version}, got {pkg_out.strip()}")

    result = execute_code(restore_session_id_new,
        "try:\n"
        "    import humanize\n"
        "    print('humanize_present=True')\n"
        "except ImportError:\n"
        "    print('humanize_present=False')"
    )
    checks_total += 1
    if result.get("success") and "humanize_present=False" in result.get("output", ""):
        log("  ✅ Uninstalled package 'humanize' correctly NOT restored")
        checks_passed += 1
    else:
        log(f"  ❌ Uninstalled package reappeared: {result.get('error') or result.get('output')}")

    # Check variable provenance (defined_by) survived the checkpoint/restore
    vars_meta = get_variables(restore_session_id_new)
    checks_total += 1
    x_defined_by = (vars_meta.get("x") or {}).get("defined_by")
    df_defined_by = (vars_meta.get("df") or {}).get("defined_by")
    if x_defined_by == "cell-setup" and df_defined_by == "cell-setup":
        log("  ✅ Provenance restored: x & df defined_by='cell-setup'")
        checks_passed += 1
    else:
        log(f"  ❌ Provenance NOT restored: x.defined_by={x_defined_by!r}, df.defined_by={df_defined_by!r}")

    # Check local file
    result = execute_code(restore_session_id_new, """
import os
exists = os.path.exists('/tmp/test_products.csv')
size = os.path.getsize('/tmp/test_products.csv') if exists else 0
print(f'file_exists={exists}, size={size}')
""")
    checks_total += 1
    if result.get("success") and "file_exists=True" in result.get("output", ""):
        log("  ✅ File '/tmp/test_products.csv' restored")
        checks_passed += 1
    else:
        log(f"  ❌ File NOT restored: {result.get('error') or result.get('output')}")

    # Check file content
    result = execute_code(restore_session_id_new, """
import csv
with open('/tmp/test_products.csv', 'r') as f:
    reader = csv.reader(f)
    rows = list(reader)
print(f'rows={len(rows)}, header={rows[0]}, first_data={rows[1]}')
""")
    checks_total += 1
    if result.get("success") and "Widget" in result.get("output", ""):
        log("  ✅ File content verified (products data intact)")
        checks_passed += 1
    else:
        log(f"  ❌ File content check failed: {result.get('error') or result.get('output')}")

    # ================================================================
    # Check data catalog restored
    checks_total += 1
    try:
        catalog_resp = requests.get(f"{PROXY_URL}/datasources/catalog", headers={"X-Session-Id": restore_session_id_new}, timeout=10)
        if catalog_resp.status_code == 200:
            catalog = catalog_resp.json()
            entries = catalog.get("entries", [])
            local_entries = [e for e in entries if e.get("source_type") == "local"]
            products_entry = next((e for e in local_entries if "test_products" in e.get("source_id", "")), None)
            if products_entry and products_entry.get("columns"):
                cols = [c["name"] for c in products_entry.get("columns", [])]
                log(f"  \u2705 Data catalog restored: test_products.csv columns={cols}")
                checks_passed += 1
            else:
                log(f"  \u274c Data catalog NOT restored (test_products.csv missing or no columns)")
        else:
            log(f"  \u274c Catalog endpoint returned {catalog_resp.status_code}")
    except Exception as e:
        log(f"  \u274c Data catalog check failed: {e}")
    print()
    # PHASE 6b: Fetch internal checkpoint timings from the restored VM
    # ================================================================
    log("")
    log("PHASE 6b: Fetching checkpoint timing breakdown from VM...")
    checkpoint_timings = {}
    try:
        headers = {"X-Session-Id": restore_session_id_new}
        resp = requests.get(f"{PROXY_URL}/proxy/checkpoint-timings", headers=headers, timeout=10)
        if resp.ok:
            checkpoint_timings = resp.json()
            if checkpoint_timings.get("last_restore"):
                rt = checkpoint_timings["last_restore"]
                log(f"  Restore breakdown (inside VM):")
                log(f"    Download checkpoint.pkl: {rt.get('download_pkl', 0):.0f}ms")
                log(f"    Deserialize (dill):      {rt.get('deserialize', 0):.0f}ms")
                log(f"    Download files.tar.gz:   {rt.get('download_files', 0):.0f}ms")
                log(f"    Extract files:           {rt.get('extract_files', 0):.0f}ms")
                log(f"    Install packages:        {rt.get('packages', 0):.0f}ms")
                log(f"    Total (inside VM):       {rt.get('total_ms', 0):.0f}ms")
            else:
                log("  No restore timings available (endpoint returned empty)")
    except Exception as e:
        log(f"  Could not fetch timings: {e}")

    # ================================================================
    # PHASE 7: Cleanup — terminate ALL test VMs
    # ================================================================
    log("")
    log("PHASE 7: Cleanup...")
    # /terminate requires microvmId; resolve each session's live VM from /instances.
    try:
        _insts = requests.get(f"{PROXY_URL}/instances", timeout=10).json().get("instances", {})
    except Exception:
        _insts = {}
    _sid_to_vm = {info.get("session_id"): vid for vid, info in _insts.items()}
    for sid in [baseline_session_id, session_id, restore_session_id_new]:
        vid = _sid_to_vm.get(sid)
        if not vid:
            continue  # already gone (e.g. baseline/original terminated earlier)
        try:
            requests.post(f"{PROXY_URL}/terminate",
                          headers={"X-Session-Id": sid, "Content-Type": "application/json"},
                          json={"microvmId": vid}, timeout=10)
            log(f"  Terminated session {sid[:20]}...")
        except Exception:
            pass
    log("  All test VMs cleaned up")

    # ================================================================
    # REPORT
    # ================================================================
    print()
    print("=" * 60)
    print("  END-TO-END TEST REPORT")
    print("=" * 60)
    print()
    print(f"  Result: {'✅ PASSED' if checks_passed == checks_total else '❌ PARTIAL FAILURE'}")
    print(f"  Checks: {checks_passed}/{checks_total} passed")
    print()
    print("  ── Timings ─────────────────────────────────────────────")
    print(f"  Bare VM launch (baseline):  {timings.get('bare_launch', 0):.1f}s")
    print(f"  Bare VM first response:     {timings.get('bare_first_response', 0):.1f}s")
    print(f"  Launch (with checkpoint):   {timings.get('launch', 0):.1f}s")
    print(f"  Wait for RUNNING:           {timings.get('wait_running', 0):.1f}s")
    print(f"  Terminate (request):        {timings.get('terminate_request', 0):.1f}s")
    print(f"  Wait for TERMINATED:        {timings.get('wait_terminated', 0):.1f}s")
    print(f"  List sessions (S3):         {timings.get('list_sessions', 0):.1f}s")
    print(f"  Launch + Restore:           {timings.get('launch_restore', 0):.1f}s")
    print(f"  Wait for RUNNING (restore): {timings.get('wait_running_restore', 0):.1f}s")
    print(f"  Restore VM first response:  {timings.get('restore_first_response', 0):.1f}s")
    print()
    total_checkpoint = timings.get('terminate_request', 0) + timings.get('wait_terminated', 0)
    total_restore = timings.get('launch_restore', 0) + timings.get('wait_running_restore', 0) + timings.get('restore_first_response', 0)
    bare_total = timings.get('bare_launch', 0) + timings.get('bare_first_response', 0)
    restore_overhead = total_restore - bare_total
    print(f"  ── Summary ─────────────────────────────────────────────")
    print(f"  Bare VM (no restore):    {bare_total:.1f}s")
    print(f"  Total checkpoint time:   {total_checkpoint:.1f}s (serialize + S3 upload)")
    print(f"  Total restore time:      {total_restore:.1f}s (launch + S3 download + deserialize)")
    print(f"  Restore overhead (Δ):    {restore_overhead:.1f}s (time added by S3 restore vs bare launch)")
    print(f"  Session ID:              {restore_session_id}")
    print(f"  Checkpoint MicroVM:      {microvm_id}")
    print(f"  Restore MicroVM:         {restore_microvm_id}")
    print()
    print(f"  ── Interpretation ──────────────────────────────────────")
    if restore_overhead < 2:
        print(f"  S3 restore adds <2s overhead — negligible vs VM provisioning")
    elif restore_overhead < 5:
        print(f"  S3 restore adds ~{restore_overhead:.0f}s — moderate (mostly S3 download + deserialize)")
    else:
        print(f"  S3 restore adds ~{restore_overhead:.0f}s — significant (consider EFS for large checkpoints)")

    if checkpoint_timings.get("last_restore"):
        rt = checkpoint_timings["last_restore"]
        print()
        print(f"  ── Checkpoint Internals (inside VM) ────────────────────")
        if save_timings_ms:
            print(f"  SAVE (backup):")
            print(f"    Serialize (dill):      {save_timings_ms.get('serialize', 0):.0f}ms")
            print(f"    S3 upload (pkl):       {save_timings_ms.get('upload_pkl', 0):.0f}ms")
            print(f"    Archive + upload files: {save_timings_ms.get('archive_files', 0):.0f}ms")
            print(f"    Save package list:     {save_timings_ms.get('packages', 0):.0f}ms")
            print(f"    Upload metadata:       {save_timings_ms.get('metadata', 0):.0f}ms")
            print(f"    TOTAL (inside VM):     {sum(save_timings_ms.values()):.0f}ms")
            print()
        print(f"  RESTORE:")
        print(f"    S3 download (pkl):     {rt.get('download_pkl', 0):.0f}ms")
        print(f"    Deserialize (dill):    {rt.get('deserialize', 0):.0f}ms")
        print(f"    S3 download (files):   {rt.get('download_files', 0):.0f}ms")
        print(f"    Extract files:         {rt.get('extract_files', 0):.0f}ms")
        print(f"    pip install packages:  {rt.get('packages', 0):.0f}ms")
        print(f"    TOTAL (inside VM):     {rt.get('total_ms', 0):.0f}ms")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
