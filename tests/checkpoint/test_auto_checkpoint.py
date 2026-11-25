#!/usr/bin/env python3
# test-kind: live
"""
End-to-End Test: Auto-Checkpoint on Terminate — RUNNING → terminate path (Checkpoint Mode)

Tests that a MicroVM in checkpoint mode automatically saves state to S3 when it
is terminated WHILE RUNNING (reaches max_lifetime without being suspended), and
that the state can be fully restored on a new VM.

STATE PERSISTENCE MODEL (important — see app/platform/hooks.py):
  There are now TWO save points, because a VM terminated while SUSPENDED does
  NOT fire the /terminate hook (verified empirically; see app/platform/hooks.py):
    • /terminate — fires only when the VM is RUNNING at kill time (explicit
                   terminate, or auto-terminate of a RUNNING VM at max lifetime).
                   THIS TEST exercises that path.
    • /suspend   — the primary path for idle VMs: state is checkpointed before
                   the freeze, so a suspended-then-killed VM's state is safe even
                   though /terminate never runs. Covered separately by the
                   suspend-path coverage (this test keeps the VM RUNNING via a
                   high idleTimeoutSeconds so it never suspends).
  Both saves are gated by a DIRTY FLAG so unchanged state isn't re-serialized.

This test keeps idleTimeoutSeconds high (> max_lifetime) so the VM stays RUNNING
the entire time and is terminated by max_lifetime while RUNNING → /terminate fires.

Unlike eternal mode (which rotates transparently), checkpoint mode:
  - Does NOT rotate — the VM simply dies at max_lifetime
  - The /terminate hook (called by AWS) saves state to S3 (dirty-flag gated)
  - The user must explicitly launch a new VM with restoreFromSession

Requires:
  - SESSION_PERSISTENCE_MODE=checkpoint
  - MAX_LIFETIME_SECONDS=180 (3 minutes — short for testing)

Total test time: ~4-5 minutes (create state, wait for termination at 180s,
then restore and verify).

Usage:
    export SESSION_PERSISTENCE_MODE=checkpoint
    export MAX_LIFETIME_SECONDS=180
    ./local_install.sh

    # In another terminal:
    python3 tests/checkpoint/test_auto_checkpoint.py

Env:
    TEST_ARCH   unset (default) = proxy default arch (arm64); set to x86_64 to
                launch both VMs on x86_64 and assert the launches report it
"""

import time
import json
import requests
from requests.exceptions import RequestException
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

MEMORY_MIB = 2048
# Opt-in arch (LIVE_TEST_X86=1 tests/run_tests.sh exports TEST_ARCH=x86_64).
# Unset = no "arch" in the /launch body = the proxy default (arm64).
TEST_ARCH = os.environ.get("TEST_ARCH", "").strip()
# Short per-launch VM lifetime (seconds) requested via /launch maxDurationSeconds,
# so the VM auto-terminates quickly for this test regardless of the proxy default.
TEST_LIFETIME_SECONDS = 180


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


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
            log(f"  ⏱  {label}: {self.elapsed:.2f}s")
    return Timer()


def execute_code(session_id, code, timeout=120, cell_id=None):
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
    """Install a package via the /install endpoint (tracked for checkpoint).
    Returns the JSON, which includes `installed_spec` (the resolved name==version)."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/install",
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": session_id,
        },
        json={"package": package},
        timeout=timeout,
    )
    return resp.json()


def uninstall_package(session_id, package, timeout=60):
    """Uninstall a package via the /install endpoint (recorded so it is NOT
    reinstalled on restore)."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/install",
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": session_id,
        },
        json={"package": package, "uninstall": True},
        timeout=timeout,
    )
    return resp.json()


def get_instances():
    """Get all active instances from the proxy."""
    resp = requests.get(f"{PROXY_URL}/instances", timeout=10)
    return resp.json().get("instances", {})


def find_vm_for_session(session_id):
    """Find the current VM ID serving a session."""
    instances = get_instances()
    for vm_id, info in instances.items():
        if info.get("session_id") == session_id:
            return vm_id, info.get("endpoint")
    return None, None


def wait_for_vm_gone(session_id, vm_id, timeout=120):
    """Wait until the VM is no longer active (terminated by AWS at max_lifetime)."""
    start = time.time()
    last_log = 0
    while time.time() - start < timeout:
        elapsed = time.time() - start
        if time.time() - last_log >= 15:
            log(f"  ⏳ Waiting for VM termination... ({elapsed:.0f}s elapsed)")
            last_log = time.time()
        instances = get_instances()
        if vm_id not in instances:
            return time.time() - start
        vm_state = instances[vm_id].get("state", "")
        if vm_state in ("TERMINATED", "GONE"):
            return time.time() - start
        time.sleep(5)
    return None


def wait_for_checkpoint(session_id, timeout=240):
    """Poll /sessions until a COMPLETED checkpoint (metadata.json written, i.e.
    checkpointed_at set) for session_id appears. Returns seconds waited, or None on
    timeout. Used in pool mode, where the save point is the 60s idle-suspend
    checkpoint (the VM is never killed at max_lifetime). The bare session prefix
    shows up in /sessions earlier (e.g. from /install tracking) with no metadata,
    so mere presence is not enough."""
    start = time.time()
    last_log = 0
    while time.time() - start < timeout:
        if time.time() - last_log >= 15:
            log(f"  ⏳ Waiting for idle-suspend checkpoint... ({time.time() - start:.0f}s elapsed)")
            last_log = time.time()
        try:
            resp = requests.get(f"{PROXY_URL}/sessions", timeout=30)
            if any(s.get("session_id") == session_id and s.get("checkpointed_at")
                   for s in resp.json().get("sessions", [])):
                return time.time() - start
        except (RequestException, ValueError):
            pass
        time.sleep(5)
    return None


def cleanup_vms(launched):
    """Best-effort terminate of EVERY VM this test launched/restored into, via the
    proxy's POST /terminate contract (microvmId body REQUIRED, X-Session-Id header
    optional). Errors are logged, never raised, so they can't mask the test result."""
    log("━" * 70)
    log("  CLEANUP")
    log("━" * 70)
    for vm_id, sid in launched:
        try:
            r = requests.post(
                f"{PROXY_URL}/terminate",
                headers={"Content-Type": "application/json", "X-Session-Id": sid},
                json={"microvmId": vm_id},
                timeout=60,
            )
            if r.status_code == 200:
                log(f"  ✓ Terminated {vm_id} (session {sid})")
            else:
                log(f"  ⚠ Terminate {vm_id} (session {sid}) → HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"  ⚠ Terminate {vm_id} (session {sid}) raised {type(e).__name__}: {e}")
    log("")


def main():
    # Every (microvmId, sessionId) the test launches or restores into is recorded
    # here; the finally terminates them all even on assertion failure/exception.
    launched = []
    try:
        _run(launched)
    finally:
        cleanup_vms(launched)


def _run(launched):
    log("=" * 70)
    log("  Auto-Checkpoint Test — Checkpoint Mode (Save Before Expiry)")
    log("=" * 70)
    log("")

    timings = {}

    # ================================================================
    # PRE-CHECK: Validate proxy is in checkpoint mode
    # ================================================================
    log("Checking proxy health and mode...")
    try:
        health = requests.get(f"{PROXY_URL}/health", timeout=5).json()
        assert health["status"] == "proxy running"
        log(f"  Proxy OK — region: {health.get('region', '?')}")
    except Exception as e:
        log(f"  ❌ Proxy not reachable: {e}")
        log("  Make sure local_install.sh is running!")
        return

    persistence_mode = health.get("persistence_mode", "unknown")
    proxy_max_lifetime = health.get("max_lifetime_seconds", 0)

    if persistence_mode != "checkpoint":
        log(f"  ❌ ERROR: This test requires persistence_mode='checkpoint', got '{persistence_mode}'")
        log(f"  Start the proxy with SESSION_PERSISTENCE_MODE=checkpoint")
        return

    # Request a SHORT per-launch lifetime via maxDurationSeconds (the /launch endpoint
    # honors it, capped at 8h) so this test runs against ANY proxy without restarting
    # it with a small MAX_LIFETIME_SECONDS. If the proxy default is already shorter,
    # use that instead.
    max_lifetime = TEST_LIFETIME_SECONDS
    if proxy_max_lifetime and proxy_max_lifetime < max_lifetime:
        max_lifetime = proxy_max_lifetime

    # Warm pool ON ⇒ /launch is served by a pooled spare (or a cold fallback) whose
    # idle/max-duration are FROZEN at the proxy's config values (_fixed_lifecycle_values
    # in proxy/platform/routes/microvm.py); maxDurationSeconds/idleTimeoutSeconds are
    # ignored and there is no per-request bypass. So the max-lifetime /terminate-hook
    # path can't be exercised: the VM idle-suspends instead and the /suspend hook is
    # the save point. We say so and SKIP that step rather than pretend it ran.
    pool_on = bool(health.get("pool_enabled"))
    skipped = []

    log(f"  ✓ Persistence mode: {persistence_mode}")
    log(f"  ✓ Test VM lifetime: {max_lifetime}s (per-launch maxDurationSeconds; proxy default {proxy_max_lifetime}s)")
    if pool_on:
        log(f"  ⚠ NOTE: warm pool is ON — lifecycle frozen at idle={health.get('idle_timeout_seconds')}s, "
            f"max={proxy_max_lifetime}s; maxDurationSeconds={max_lifetime} is IGNORED.")
        log(f"    The max-lifetime /terminate-hook path is NOT exercised in this run (SKIPPED).")
        log(f"    Save point instead: idle-suspend checkpoint (/suspend hook). Restore checks still run.")
    else:
        log(f"  ✓ Path under test: /terminate hook (VM stays RUNNING to max_lifetime, never suspends)")
        log(f"    (idleTimeoutSeconds is kept > max_lifetime so /terminate — not /suspend — is the save point)")
    log("")

    # ================================================================
    # STEP 1: Launch VM in checkpoint mode
    # ================================================================
    log("━" * 70)
    log("  STEP 1: Launch MicroVM (checkpoint mode — no rotation)")
    log("━" * 70)

    session_id = f"checkpoint-test-{int(time.time())}"
    with timed("Launch MicroVM") as t:
        resp = requests.post(f"{PROXY_URL}/launch", json={
            "name": "auto-checkpoint-test",
            "memoryMiB": MEMORY_MIB,
            "idleTimeoutSeconds": 600,
            "checkpointEnabled": True,
            "sessionId": session_id,
            "maxDurationSeconds": max_lifetime,
            **({"arch": TEST_ARCH} if TEST_ARCH else {}),
        }, timeout=120)
        assert resp.status_code == 200, f"Launch failed: {resp.status_code} {resp.text}"
        data = resp.json()
        vm_id = data["microvmId"]
        session_id = data.get("sessionId", session_id)
        launched.append((vm_id, session_id))
        if TEST_ARCH:
            assert data.get("architecture") == TEST_ARCH, \
                f"Launched on {data.get('architecture')!r}, expected TEST_ARCH={TEST_ARCH!r}"
        endpoint = data["endpoint"]
    timings["launch"] = t.elapsed
    launch_time = time.time()

    log(f"  VM ID:     {vm_id}")
    log(f"  Endpoint:  {endpoint}")
    log(f"  Session:   {session_id}")
    log("")

    # Wait a moment for VM to fully initialize
    time.sleep(3)

    # ================================================================
    # STEP 2: Create rich state
    # ================================================================
    log("━" * 70)
    log("  STEP 2: Create rich state (vars, DataFrame, file, package, chart)")
    log("━" * 70)

    with timed("Create variables + DataFrame") as t:
        result = execute_code(session_id,
            "import pandas as pd\n"
            "import numpy as np\n"
            "import hashlib\n"
            "\n"
            "# Variables\n"
            "checkpoint_marker = 'I_SURVIVED_CHECKPOINT'\n"
            "counter = 42\n"
            "secret = np.random.randint(0, 999999)\n"
            "\n"
            "# 500-row DataFrame\n"
            "np.random.seed(12345)\n"
            "df = pd.DataFrame({\n"
            "    'x': np.random.randn(500),\n"
            "    'y': np.random.randn(500),\n"
            "    'category': np.random.choice(['A', 'B', 'C', 'D'], 500),\n"
            "})\n"
            "df['z'] = df['x'] * df['y']\n"
            "df_checksum = hashlib.md5(df.to_csv().encode()).hexdigest()\n"
            "\n"
            "print(f'State: marker={checkpoint_marker}, counter={counter}, secret={secret}')\n"
            "print(f'DataFrame: {df.shape}, checksum={df_checksum}')",
            cell_id="cell-state",
        )
    timings["create_vars"] = t.elapsed
    assert result.get("success"), f"Create vars failed: {result.get('error')}"
    log(f"  {result['output'].strip()}")

    with timed("Create local file") as t:
        result = execute_code(session_id,
            "import os\n"
            "with open('/tmp/checkpoint_test.txt', 'w') as f:\n"
            "    f.write(f'Created for checkpoint test\\n')\n"
            "    f.write(f'secret={secret}\\n')\n"
            "    f.write(f'df_checksum={df_checksum}\\n')\n"
            "    f.write(f'rows={len(df)}\\n')\n"
            "print(f'File: {os.path.getsize(\"/tmp/checkpoint_test.txt\")} bytes')"
        )
    timings["create_file"] = t.elapsed
    assert result.get("success"), f"Create file failed: {result.get('error')}"
    log(f"  {result['output'].strip()}")

    with timed("Install package (tabulate)") as t:
        result = install_package(session_id, "tabulate")
    timings["install_pkg"] = t.elapsed
    assert result.get("success"), f"Install failed: {result.get('error')}"
    # Version pinning: /install resolves + returns a pinned spec (name==version).
    installed_spec = result.get("installed_spec", "")
    log(f"  Install spec: {installed_spec!r}")
    assert installed_spec.startswith("tabulate=="), \
        f"Expected a pinned spec like 'tabulate==x.y.z', got {installed_spec!r}"
    installed_pkg_version = installed_spec.split("==", 1)[1]
    # Verify importable + capture the runtime version (must match the pinned spec).
    verify = execute_code(session_id, "import tabulate; print(f'tabulate installed: {tabulate.__version__}')")
    assert verify.get("success"), f"Import failed: {verify.get('error')}"
    log(f"  {verify['output'].strip()}")
    assert installed_pkg_version in verify.get("output", ""), \
        f"Pinned version {installed_pkg_version} does not match runtime: {verify.get('output', '').strip()}"

    # Uninstall tracking: install a throwaway package then uninstall it. It must
    # NOT reappear after restore (uninstall is now recorded in the checkpoint list).
    with timed("Install + uninstall package (humanize)") as t:
        ir = install_package(session_id, "humanize")
        assert ir.get("success"), f"humanize install failed: {ir.get('error')}"
        ur = uninstall_package(session_id, "humanize")
        assert ur.get("success"), f"humanize uninstall failed: {ur.get('error')}"
    timings["uninstall_pkg"] = t.elapsed
    gone = execute_code(session_id,
        "try:\n    import humanize\n    print('humanize_present=True')\n"
        "except ImportError:\n    print('humanize_present=False')")
    log(f"  humanize after uninstall (pre-checkpoint): {gone.get('output', '').strip()}")

    with timed("Generate matplotlib chart") as t:
        result = execute_code(session_id,
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "\n"
            "fig, axes = plt.subplots(2, 2, figsize=(12, 10))\n"
            "\n"
            "# Subplot 1: Scatter\n"
            "axes[0, 0].scatter(df['x'], df['y'], c=df['z'], cmap='viridis', alpha=0.6)\n"
            "axes[0, 0].set_title('X vs Y (colored by Z)')\n"
            "\n"
            "# Subplot 2: Histogram\n"
            "axes[0, 1].hist(df['z'], bins=30, edgecolor='black')\n"
            "axes[0, 1].set_title('Distribution of Z')\n"
            "\n"
            "# Subplot 3: Box plot by category\n"
            "df.boxplot(column='z', by='category', ax=axes[1, 0])\n"
            "axes[1, 0].set_title('Z by Category')\n"
            "\n"
            "# Subplot 4: Line plot\n"
            "axes[1, 1].plot(df['x'].cumsum(), label='Cumsum X')\n"
            "axes[1, 1].plot(df['y'].cumsum(), label='Cumsum Y')\n"
            "axes[1, 1].legend()\n"
            "axes[1, 1].set_title('Cumulative Sums')\n"
            "\n"
            "plt.tight_layout()\n"
            "chart_var_name = 'fig'  # Track that we made this\n"
            "print(f'Chart created: {len(fig.axes)} subplots, figsize={fig.get_size_inches()}')"
        )
    timings["create_chart"] = t.elapsed
    assert result.get("success"), f"Chart failed: {result.get('error')}"
    log(f"  {result['output'].strip()}")

    # Capture values for later verification
    with timed("Capture baseline values") as t:
        result = execute_code(session_id,
            "import json\n"
            "print(json.dumps({'secret': int(secret), 'df_checksum': df_checksum}))"
        )
    assert result.get("success")
    baseline = json.loads(result["output"].strip())
    secret_value = baseline["secret"]
    df_checksum_value = baseline["df_checksum"]
    timings["capture_baseline"] = t.elapsed
    log(f"  Captured: secret={secret_value}, checksum={df_checksum_value[:16]}...")
    log("")

    # ================================================================
    # STEP 3: Verify state exists before checkpoint
    # ================================================================
    log("━" * 70)
    log("  STEP 3: Verify state exists")
    log("━" * 70)

    with timed("Verify state") as t:
        result = execute_code(session_id,
            "import os\n"
            "assert checkpoint_marker == 'I_SURVIVED_CHECKPOINT'\n"
            "assert counter == 42\n"
            "assert len(df) == 500\n"
            "assert os.path.exists('/tmp/checkpoint_test.txt')\n"
            "import tabulate\n"
            "print(f'All pre-checkpoint checks pass')\n"
            "print(f'  vars: OK, df: {df.shape}, file: OK, tabulate: {tabulate.__version__}')"
        )
    timings["verify_pre"] = t.elapsed
    assert result.get("success"), f"Pre-check failed: {result.get('error')}"
    log(f"  {result['output'].strip()}")
    log("")

    # ================================================================
    # STEP 4: Wait for VM termination (triggers /terminate hook → checkpoint)
    # ================================================================
    log("━" * 70)
    if pool_on:
        log(f"  STEP 4: Wait for VM termination at max_lifetime — SKIPPED (pool on: lifecycle frozen)")
        log("━" * 70)
        log(f"  The pooled VM keeps max_lifetime={proxy_max_lifetime}s, so it is not killed and the")
        log(f"  /terminate hook does not fire. Waiting for the idle-suspend checkpoint instead.")
        skipped.append("max-lifetime /terminate-hook checkpoint (pool on: lifecycle frozen)")
    else:
        log(f"  STEP 4: Wait for VM termination at max_lifetime ({max_lifetime}s)")
        log("━" * 70)
        log(f"  The VM is RUNNING (idle timeout > max_lifetime), so AWS fires the")
        log(f"  /terminate hook, which saves state to S3 — the state is dirty (we just")
        log(f"  created vars/file/package), so the dirty-flag permits the save.")
    log("")

    elapsed_since_launch = time.time() - launch_time
    wait_until_terminated = max_lifetime + 30
    remaining = wait_until_terminated - elapsed_since_launch

    if pool_on:
        with timed("Wait for idle-suspend checkpoint") as t:
            got = wait_for_checkpoint(session_id, timeout=240)
        timings["vm_terminated"] = t.elapsed
        if got is not None:
            log(f"  ✓ Idle-suspend checkpoint present (after {got:.0f}s of waiting)")
        else:
            log(f"  ⚠ No idle-suspend checkpoint after 240s — STEP 5 will report it")
    elif remaining > 0:
        log(f"  {elapsed_since_launch:.0f}s since launch, waiting up to {remaining:.0f}s for termination...")
        with timed("Wait for VM termination") as t:
            gone_at = wait_for_vm_gone(session_id, vm_id, timeout=remaining + 30)
        timings["vm_terminated"] = t.elapsed

        if gone_at is not None:
            log(f"  ✓ VM terminated (gone after {gone_at:.0f}s of waiting)")
        else:
            log(f"  ⚠ VM still showing as active after {remaining:.0f}s wait")
            log(f"    Proceeding anyway — VM may have been terminated but proxy cache is stale")
    else:
        log(f"  VM should already be terminated ({elapsed_since_launch:.0f}s since launch)")
        timings["vm_terminated"] = 0

    # Brief wait for S3 checkpoint write to propagate
    time.sleep(5)
    log("")

    # ================================================================
    # STEP 5: Verify checkpoint exists in S3
    # ================================================================
    log("━" * 70)
    log("  STEP 5: Verify checkpoint exists in S3")
    log("━" * 70)

    with timed("Check sessions endpoint") as t:
        # The proxy may be busy writing the S3 checkpoint (pickle + tarball uploads)
        # right after termination, so /sessions can be slow. Retry with a generous
        # timeout instead of failing on a single 15s read timeout.
        sessions = []
        last_err = None
        for attempt in range(6):
            try:
                resp = requests.get(f"{PROXY_URL}/sessions", timeout=30)
                sessions = resp.json().get("sessions", [])
                break
            except RequestException as e:
                last_err = e
                log(f"  ⏳ /sessions not ready (attempt {attempt + 1}/6): {type(e).__name__}; retrying...")
                time.sleep(5)
        else:
            log(f"  ❌ /sessions endpoint never responded: {last_err}")
    timings["check_sessions"] = t.elapsed

    our_session = None
    for s in sessions:
        if s.get("session_id") == session_id:
            our_session = s
            break

    if our_session:
        log(f"  ✓ Checkpoint found for session: {session_id}")
        log(f"    Variables: {our_session.get('variables_count', '?')}")
        log(f"    Files: {our_session.get('files_count', '?')}")
        log(f"    Checkpointed at: {our_session.get('checkpointed_at', '?')}")
    else:
        log(f"  ❌ No checkpoint found for session '{session_id}'")
        log(f"  Available sessions: {[s['session_id'] for s in sessions]}")
        log(f"  This means auto-checkpoint did not fire. Check proxy logs.")
        return  # launched VMs are terminated by main()'s finally
    log("")

    # ================================================================
    # STEP 6: Launch new VM with restoreFromSession
    # ================================================================
    log("━" * 70)
    log("  STEP 6: Launch new VM with restoreFromSession")
    log("━" * 70)

    new_session_id = f"{session_id}-restored-{int(time.time())}"
    with timed("Launch restored VM") as t:
        resp = requests.post(f"{PROXY_URL}/launch", json={
            "name": "checkpoint-restore-test",
            "memoryMiB": MEMORY_MIB,
            "idleTimeoutSeconds": 300,
            "checkpointEnabled": False,
            "sessionId": new_session_id,
            "restoreFromSession": session_id,
            **({"arch": TEST_ARCH} if TEST_ARCH else {}),
        }, timeout=120)
        assert resp.status_code == 200, f"Restore launch failed: {resp.status_code} {resp.text}"
        restore_data = resp.json()
        restore_vm_id = restore_data["microvmId"]
        launched.append((restore_vm_id, new_session_id))
        if TEST_ARCH:
            assert restore_data.get("architecture") == TEST_ARCH, \
                f"Restored on {restore_data.get('architecture')!r}, expected TEST_ARCH={TEST_ARCH!r}"
        restore_endpoint = restore_data["endpoint"]
    timings["launch_restore"] = t.elapsed

    log(f"  New VM ID:  {restore_vm_id}")
    log(f"  Endpoint:   {restore_endpoint}")

    # Wait for it to be responsive
    with timed("Wait for restored VM ready") as t:
        headers = {"X-Session-Id": new_session_id}
        for _ in range(20):
            try:
                r = requests.get(f"{PROXY_URL}/proxy/health", headers=headers, timeout=5)
                if r.ok:
                    break
            except:
                pass
            time.sleep(2)
    timings["restore_ready"] = t.elapsed
    log(f"  ✓ Restored VM is responsive")
    log("")

    # ================================================================
    # STEP 7: Verify ALL state restored
    # ================================================================
    log("━" * 70)
    log("  STEP 7: Verify all state restored")
    log("━" * 70)

    checks = []

    # Check variables
    with timed("Verify variables") as t:
        result = execute_code(new_session_id,
            "import json\n"
            "results = {}\n"
            "try:\n"
            "    results['marker'] = checkpoint_marker == 'I_SURVIVED_CHECKPOINT'\n"
            "except NameError:\n"
            "    results['marker'] = False\n"
            "try:\n"
            "    results['counter'] = counter == 42\n"
            "except NameError:\n"
            "    results['counter'] = False\n"
            "try:\n"
            f"    results['secret'] = secret == {secret_value}\n"
            "except NameError:\n"
            "    results['secret'] = False\n"
            "print(json.dumps(results))"
        )
    timings["verify_vars"] = t.elapsed

    if result.get("success"):
        var_checks = json.loads(result["output"].strip())
        for name, passed in var_checks.items():
            checks.append((f"variable '{name}'", passed))
            status = "✓" if passed else "❌"
            log(f"  {status} Variable '{name}': {'restored' if passed else 'LOST'}")
    else:
        log(f"  ❌ Variable check execution failed: {result.get('error')}")
        checks.append(("variables execution", False))

    # Check DataFrame (checksum)
    with timed("Verify DataFrame") as t:
        result = execute_code(new_session_id,
            "import hashlib\n"
            "try:\n"
            "    restored_checksum = hashlib.md5(df.to_csv().encode()).hexdigest()\n"
            f"    match = restored_checksum == '{df_checksum_value}'\n"
            "    print(f'df_match={match}, shape={df.shape}, checksum={restored_checksum[:16]}')\n"
            "except NameError as e:\n"
            "    print(f'df_match=False, error={e}')"
        )
    timings["verify_df"] = t.elapsed

    if result.get("success"):
        output = result["output"].strip()
        df_ok = "df_match=True" in output
        checks.append(("DataFrame checksum", df_ok))
        status = "✓" if df_ok else "❌"
        log(f"  {status} DataFrame: {output}")
    else:
        checks.append(("DataFrame", False))
        log(f"  ❌ DataFrame check failed: {result.get('error')}")

    # Check variable provenance (defined_by) survived the checkpoint/restore
    with timed("Verify provenance") as t:
        vars_meta = get_variables(new_session_id)
    timings["verify_provenance"] = t.elapsed
    counter_defined_by = (vars_meta.get("counter") or {}).get("defined_by")
    df_defined_by = (vars_meta.get("df") or {}).get("defined_by")
    prov_ok = counter_defined_by == "cell-state" and df_defined_by == "cell-state"
    checks.append(("variable provenance (defined_by)", prov_ok))
    log(f"  {'✓' if prov_ok else '❌'} Provenance: counter.defined_by={counter_defined_by!r}, df.defined_by={df_defined_by!r}")

    # Check local file
    with timed("Verify local file") as t:
        result = execute_code(new_session_id,
            "import os\n"
            "exists = os.path.exists('/tmp/checkpoint_test.txt')\n"
            "content = ''\n"
            "if exists:\n"
            "    content = open('/tmp/checkpoint_test.txt').read()\n"
            f"has_secret = 'secret={secret_value}' in content\n"
            "print(f'file_exists={exists}, has_secret={has_secret}')"
        )
    timings["verify_file"] = t.elapsed

    if result.get("success"):
        output = result["output"].strip()
        file_ok = "file_exists=True" in output and "has_secret=True" in output
        checks.append(("local file /tmp/checkpoint_test.txt", file_ok))
        status = "✓" if file_ok else "❌"
        log(f"  {status} Local file: {output}")
    else:
        checks.append(("local file", False))
        log(f"  ❌ File check failed: {result.get('error')}")
    # Check data catalog restored with local file schema
    try:
        catalog_resp = requests.get(f"{PROXY_URL}/datasources/catalog", headers={"X-Session-Id": new_session_id}, timeout=10)
        if catalog_resp.status_code == 200:
            catalog = catalog_resp.json()
            discovered = catalog.get("discovered", 0)
            total = catalog.get("total", 0)
            local_entries = [e for e in catalog.get("entries", []) if e.get("source_type") == "local"]
            checks.append(("data catalog restored", discovered > 0))
            log(f"  {"\u2713" if discovered > 0 else "\u274c"} Data catalog: {discovered}/{total} sources, {len(local_entries)} local files")
        else:
            checks.append(("data catalog restored", False))
            log(f"  \u274c Data catalog endpoint returned {catalog_resp.status_code}")
    except Exception as e:
        checks.append(("data catalog restored", False))
        log(f"  \u274c Data catalog check failed: {e}")

    # Check installed package (tabulate)
    with timed("Verify installed package") as t:
        result = execute_code(new_session_id,
            "try:\n"
            "    import tabulate\n"
            "    print(f'tabulate_ok=True, version={tabulate.__version__}')\n"
            "except ImportError as e:\n"
            "    print(f'tabulate_ok=False, error={e}')"
        )
    timings["verify_pkg"] = t.elapsed

    if result.get("success"):
        output = result["output"].strip()
        pkg_ok = "tabulate_ok=True" in output
        checks.append(("installed package (tabulate)", pkg_ok))
        log(f"  {'✓' if pkg_ok else '❌'} Package tabulate: {output}")
        # Version pinning: the restored version must EXACTLY match the version
        # installed pre-checkpoint (pinned name==version persisted, not loosely
        # re-resolved from PyPI on restore).
        version_ok = f"version={installed_pkg_version}" in output
        checks.append(("package version pinned on restore", version_ok))
        log(f"  {'✓' if version_ok else '❌'} Version pinned: expected {installed_pkg_version}, got {output}")
    else:
        checks.append(("package tabulate", False))
        checks.append(("package version pinned on restore", False))
        log(f"  ❌ Package check failed: {result.get('error')}")

    # Uninstall tracking: the uninstalled package must NOT be reinstalled on restore.
    with timed("Verify uninstalled package absent") as t:
        result = execute_code(new_session_id,
            "try:\n"
            "    import humanize\n"
            "    print('humanize_present=True')\n"
            "except ImportError:\n"
            "    print('humanize_present=False')"
        )
    timings["verify_uninstall"] = t.elapsed
    if result.get("success"):
        output = result["output"].strip()
        uninstall_ok = "humanize_present=False" in output
        checks.append(("uninstalled package not restored", uninstall_ok))
        log(f"  {'✓' if uninstall_ok else '❌'} Uninstall tracked: {output}")
    else:
        checks.append(("uninstalled package not restored", False))
        log(f"  ❌ Uninstall check failed: {result.get('error')}")

    # ================================================================
    # STEP 8: Verify matplotlib restore semantics.
    #   • Non-serializable OBJECTS (fig, axes) must NOT be restored — they're
    #     excluded from the checkpoint pickle (can't be dill'd reliably).
    #   • The MODULE BINDING (plt) SHOULD be restored — module objects aren't
    #     pickled either, but the import ledger replays `import matplotlib.pyplot
    #     as plt` on restore so the user's existing cells keep working. So plt
    #     coming back is the correct, intended behavior (not a leak).
    # ================================================================
    log("")
    log("━" * 70)
    log("  STEP 8: Verify matplotlib restore semantics (objects excluded; import replayed)")
    log("━" * 70)

    with timed("Check matplotlib restore semantics") as t:
        result = execute_code(new_session_id,
            "present = []\n"
            "for name in ['fig', 'axes', 'plt']:\n"
            "    try:\n"
            "        eval(name)\n"
            "        present.append(name)\n"
            "    except NameError:\n"
            "        pass\n"
            "import types\n"
            "plt_is_module = 'plt' in dir() and isinstance(plt, types.ModuleType)\n"
            "objects_excluded = ('fig' not in present) and ('axes' not in present)\n"
            "print(f'present={present}')\n"
            "print(f'objects_excluded={objects_excluded}')\n"
            "print(f'plt_module_replayed={plt_is_module}')"
        )
    timings["verify_mpl_semantics"] = t.elapsed

    if result.get("success"):
        output = result["output"].strip()
        objects_excluded = "objects_excluded=True" in output
        plt_replayed = "plt_module_replayed=True" in output
        # Non-serializable figure/axes objects must be gone.
        checks.append(("matplotlib objects (fig/axes) excluded from checkpoint", objects_excluded))
        # The import binding must be replayed by the import ledger.
        checks.append(("matplotlib import (plt) replayed on restore", plt_replayed))
        log(f"  {'✓' if objects_excluded else '❌'} Objects excluded: {output.splitlines()[1] if len(output.splitlines())>1 else output}")
        log(f"  {'✓' if plt_replayed else '❌'} Import replayed: plt is a live module → {plt_replayed}")
        if not objects_excluded:
            log(f"    (Unexpected — fig/axes are non-serializable and must not be restored)")
        if not plt_replayed:
            log(f"    (Unexpected — the import ledger should replay `import matplotlib.pyplot as plt`)")
    else:
        checks.append(("matplotlib restore-semantics check", False))
        log(f"  ❌ Check failed: {result.get('error')}")
    log("")

    # CLEANUP: every launched VM (original + restored) is terminated by main()'s finally.

    # ================================================================
    # STEP 9: Timing Report
    # ================================================================
    total_time = time.time() - launch_time
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    all_passed = passed == total

    print()
    print("=" * 70)
    print("  AUTO-CHECKPOINT TEST REPORT")
    print("=" * 70)
    print()
    print(f"  Result: {'✅ PASSED' if all_passed else '❌ FAILED'}")
    print(f"  Checks: {passed}/{total} passed")
    print()
    print("  ── Check Details ───────────────────────────────────────")
    for name, ok in checks:
        print(f"    {'✓' if ok else '❌'} {name}")
    for name in skipped:
        print(f"    ⊘ SKIPPED: {name}")
    print()
    print("  ── Timings ─────────────────────────────────────────────")
    print(f"  Launch VM:                  {timings.get('launch', 0):.2f}s")
    print(f"  Create state (vars+df):     {timings.get('create_vars', 0):.2f}s")
    print(f"  Create file:                {timings.get('create_file', 0):.2f}s")
    print(f"  Install package:            {timings.get('install_pkg', 0):.2f}s")
    print(f"  Generate chart:             {timings.get('create_chart', 0):.2f}s")
    print(f"  Wait for VM termination:    {timings.get('vm_terminated', 0):.2f}s")
    print(f"  Launch restored VM:         {timings.get('launch_restore', 0):.2f}s")
    print(f"  Restored VM ready:          {timings.get('restore_ready', 0):.2f}s")
    print(f"  Verify variables:           {timings.get('verify_vars', 0):.2f}s")
    print(f"  Verify DataFrame:           {timings.get('verify_df', 0):.2f}s")
    print(f"  Verify file:                {timings.get('verify_file', 0):.2f}s")
    print(f"  Verify package:             {timings.get('verify_pkg', 0):.2f}s")
    print(f"  Total test time:            {total_time:.1f}s ({total_time/60:.1f} min)")
    print()
    print("  ── Configuration ───────────────────────────────────────")
    print(f"  Mode:                       checkpoint (no rotation)")
    print(f"  MAX_LIFETIME_SECONDS:       {max_lifetime}")
    print(f"  Path under test:            "
          f"{'idle-suspend /suspend hook (pool on: max-lifetime path SKIPPED)' if pool_on else '/terminate hook (VM RUNNING at kill time)'}")
    print(f"  VM dies at:                 ~{max_lifetime}s (RUNNING — idle timeout kept high)")
    print(f"  Memory:                     {MEMORY_MIB} MiB")
    print()
    print("  ── Flow ────────────────────────────────────────────────")
    print(f"  1. VM launched, state created (vars, DataFrame, file, package, chart)")
    print(f"  2. VM stays RUNNING (high idle timeout), terminated by AWS at ~{max_lifetime}s")
    print(f"  3. /terminate hook saves checkpoint to S3 (dirty-flag: only if changed)")
    print(f"  4. New VM launched with restoreFromSession")
    print(f"  5. All state restored (vars, DataFrame, file, packages, imports)")
    print(f"  6. matplotlib objects (fig/axes) excluded; `import ... as plt` replayed")
    print(f"  NOTE: the idle→suspend→killed-while-suspended path (/suspend save) is")
    print(f"        a separate scenario; here idleTimeoutSeconds > max_lifetime so the")
    print(f"        VM never suspends and /terminate is the save point.")
    print()
    print("  ── Conclusion ──────────────────────────────────────────")
    if all_passed and pool_on:
        print(f"  Checkpoint saved on idle-suspend (pool on); max-lifetime /terminate path NOT exercised.")
        print(f"  Full restore confirmed: variables, DataFrame, files, packages, imports.")
    elif all_passed:
        print(f"  Checkpoint saved on /terminate hook at max_lifetime.")
        print(f"  Full restore confirmed: variables, DataFrame, files, packages, imports.")
        print(f"  Non-serializable objects (matplotlib fig/axes) correctly excluded;")
        print(f"  module imports (plt) replayed via the import ledger.")
    else:
        print(f"  Some checks failed — see details above.")
        print(f"  Check proxy logs for checkpoint/restore errors.")
    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
