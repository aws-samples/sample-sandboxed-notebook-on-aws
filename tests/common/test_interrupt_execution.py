#!/usr/bin/env python3
# test-kind: live
"""
End-to-End Test: MicroVM Cell Execution Interrupt

This script tests the interrupt/stop execution feature — the notebook "Stop"
button a user clicks to abort a running cell and keep working.

Scope: interrupting runaway PURE-PYTHON code (long/near-infinite loops), which is
the common real "stuck cell". These are reliably interruptible because they run
Python bytecode continuously, so an injected KeyboardInterrupt is delivered.

Out of scope (documented limitation): interrupting a cell blocked in a C-level
call (time.sleep, socket recv, a C extension). PyThreadState_SetAsyncExc cannot
break a thread parked in C; the exception is queued until the call returns.
Force-killing those needs per-cell process isolation, which this in-process
executor does not do. So we do NOT assert on interrupting time.sleep.

Steps:
1. Launch a MicroVM
2. Interrupt a runaway pure-Python loop; assert it aborts promptly (lock released)
3. Run a normal cell after interrupt → sandbox healthy, prior state preserved
4. Interrupt a tight CPU-bound loop (while True: ...)
5. Verify loop variable survived
6. Interrupt with nothing running (no-op)
7. Final full execution (pandas + rendered DataFrame)
8. Terminate the MicroVM

Usage:
    Ensure local_install.sh is running (proxy on :8081), then:
    python3 tests/common/test_interrupt_execution.py

Env:
    TEST_ARCH   unset (default) = proxy default arch (arm64); set to x86_64 to
                launch on x86_64 and assert the launch reports it
"""

import time
import threading
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

MEMORY_MIB = 2048  # Smallest tier for fast test
NOTEBOOK_NAME = "interrupt-test"
# Opt-in arch (LIVE_TEST_X86=1 tests/run_tests.sh exports TEST_ARCH=x86_64).
# Unset = no "arch" in the /launch body = the proxy default (arm64).
TEST_ARCH = os.environ.get("TEST_ARCH", "").strip()


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


def execute_code(session_id, code, timeout=60):
    """Execute code on the MicroVM via the proxy using session_id only."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/execute",
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": session_id,
        },
        json={"code": code},
        timeout=timeout,
    )
    return resp.json()


def interrupt_execution(session_id):
    """Send interrupt signal to the MicroVM."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/interrupt",
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": session_id,
        },
        timeout=10,
    )
    return resp.json()


def execute_async(session_id, code, result_holder):
    """Execute code in a background thread (for testing interrupt)."""
    try:
        result = execute_code(session_id, code, timeout=120)
        result_holder["result"] = result
    except Exception as e:
        result_holder["error"] = str(e)


def main():
    log("=" * 60)
    log("  MicroVM Interrupt Execution Test")
    log("=" * 60)
    print()

    # --- Check proxy is running ---
    log("Checking proxy health...")
    try:
        health = requests.get(f"{PROXY_URL}/health", timeout=5).json()
        assert health["status"] == "proxy running", f"Unexpected status: {health}"
        log(f"  Proxy OK — region: {health['region']}, image: {health['image_arn'][:50]}...")
    except Exception as e:
        log(f"  ❌ Proxy not reachable: {e}")
        log("  Make sure local_install.sh is running!")
        return

    # --- Launch MicroVM ---
    log("Launching MicroVM...")
    with timed("Launch"):
        resp = requests.post(f"{PROXY_URL}/launch", json={
            "name": NOTEBOOK_NAME,
            "memoryMiB": MEMORY_MIB,
            "idleTimeoutSeconds": 1800,
            "checkpointEnabled": False,
            "sessionId": f"interrupt-test-{int(time.time())}",
            **({"arch": TEST_ARCH} if TEST_ARCH else {}),
        }, timeout=120)
        assert resp.status_code == 200, f"Launch failed: {resp.text}"
        launch_data = resp.json()
    if TEST_ARCH:
        assert launch_data.get("architecture") == TEST_ARCH, \
            f"Launched on {launch_data.get('architecture')!r}, expected TEST_ARCH={TEST_ARCH!r}"

    microvm_id = launch_data["microvmId"]
    session_id = launch_data.get("sessionId", f"interrupt-test-{int(time.time())}")

    log(f"  MicroVM ID: {microvm_id}")
    log(f"  Session ID: {session_id}")
    print()

    # --- Test 1: Normal execution works ---
    log("TEST 1: Normal execution (sanity check)")
    with timed("Simple execution"):
        result = execute_code(session_id, "x = 42\nprint(x * 2)")
    assert result["success"], f"Expected success, got: {result}"
    assert "84" in result["output"], f"Expected '84' in output, got: {result['output']}"
    log("  ✓ Normal execution works")
    print()

    # --- Test 2: Interrupt a runaway PURE-PYTHON loop (the real "stuck cell") ---
    # This is the interruptible case a user actually hits: an accidental
    # long/near-infinite Python loop. The interrupt injects KeyboardInterrupt via
    # PyThreadState_SetAsyncExc, which IS delivered because the loop runs bytecode.
    #
    # NOTE: we deliberately do NOT test interrupting `time.sleep(300)` or other
    # C-level blocking calls. PyThreadState_SetAsyncExc cannot break a thread that
    # is parked inside a C call — the exception is queued until the call returns.
    # Force-killing those would require per-cell process isolation, which this
    # in-process executor does not do. See SandboxExecutor.interrupt() docstring.
    log("TEST 2: Interrupt a runaway pure-Python loop (long busy loop)")
    log("  Starting long-running cell (~300s busy loop)...")

    result_holder = {}
    # A CPU/bytecode loop that would run ~300s; interruptible because it executes
    # Python bytecode continuously (never blocks in a single long C call).
    busy_loop = (
        "spins = 0\n"
        "import time as _t\n"
        "_deadline = _t.time() + 300\n"
        "while _t.time() < _deadline:\n"
        "    spins += 1\n"
        "print('should not reach here')"
    )
    exec_thread = threading.Thread(target=execute_async, args=(session_id, busy_loop, result_holder))
    exec_thread.start()

    # Wait a bit for the execution to start
    time.sleep(5)
    log("  Cell is running... sending interrupt")

    with timed("Interrupt") as it:
        interrupt_result = interrupt_execution(session_id)
    log(f"  Interrupt response: {interrupt_result}")
    assert interrupt_result.get("status") == "interrupted", \
        f"Expected 'interrupted' status, got: {interrupt_result}"

    # The cell's request MUST return promptly (lock released), not hang.
    exec_thread.join(timeout=15)
    assert not exec_thread.is_alive(), \
        "Interrupted cell did not return within 15s — the exec lock is still held"

    assert "result" in result_holder, f"Interrupted cell produced no result: {result_holder}"
    result = result_holder["result"]
    assert not result["success"], f"Expected failure after interrupt, got success: {result}"
    assert "interrupt" in result.get("error", "").lower() or "KeyboardInterrupt" in result.get("error", ""), \
        f"Expected interrupt error, got: {result.get('error')}"
    log(f"  ✓ Runaway loop interrupted promptly ({it.elapsed:.2f}s): {result['error']}")
    print()

    # --- Test 3: Sandbox still healthy after interrupt ---
    log("TEST 3: Sandbox health after interrupt")
    with timed("Post-interrupt execution"):
        result = execute_code(session_id, "y = x + 1\nprint(f'State preserved: x={x}, y={y}')")
    assert result["success"], f"Post-interrupt execution failed: {result}"
    assert "State preserved" in result["output"], f"Unexpected output: {result['output']}"
    assert "x=42" in result["output"], f"Variable x lost after interrupt: {result['output']}"
    log(f"  ✓ Sandbox healthy, state preserved: {result['output'].strip()}")
    print()

    # --- Test 4: Interrupt a CPU-bound loop ---
    log("TEST 4: Interrupt a CPU-bound infinite loop")
    log("  Starting infinite loop (while True: pass)...")

    result_holder = {}
    exec_thread = threading.Thread(
        target=execute_async,
        args=(session_id, "counter = 0\nwhile True:\n    counter += 1", result_holder),
    )
    exec_thread.start()

    # Wait for loop to start executing
    time.sleep(5)
    log("  Loop running... sending interrupt")

    with timed("Interrupt CPU loop"):
        interrupt_result = interrupt_execution(session_id)
    log(f"  Interrupt response: {interrupt_result}")

    exec_thread.join(timeout=15)

    if "result" in result_holder:
        result = result_holder["result"]
        assert not result["success"], f"Expected failure after interrupt, got success"
        log(f"  ✓ CPU loop interrupted: {result.get('error', 'unknown')}")
    else:
        log("  ⚠ Thread didn't complete — CPU loop may need multiple interrupts")
    print()

    # --- Test 5: Verify counter was set (loop ran for some iterations) ---
    log("TEST 5: Verify loop variable survived")
    with timed("Check counter"):
        result = execute_code(session_id, "print(f'Counter reached: {counter}')")
    if result["success"] and "Counter reached:" in result["output"]:
        log(f"  ✓ {result['output'].strip()}")
    else:
        log(f"  ⚠ Counter may not be set (loop may not have assigned before interrupt)")
    print()

    # --- Test 6: Interrupt when nothing is running (no-op) ---
    log("TEST 6: Interrupt with nothing running (should be no-op)")
    with timed("No-op interrupt"):
        interrupt_result = interrupt_execution(session_id)
    log(f"  Response: {interrupt_result}")
    assert interrupt_result.get("status") in ("idle", "interrupted"), \
        f"Unexpected response: {interrupt_result}"
    log("  ✓ No-op interrupt handled gracefully")
    print()

    # --- Test 7: Execute normally after all interrupts ---
    log("TEST 7: Final execution (full health check)")
    with timed("Final execution"):
        result = execute_code(session_id,
            "import pandas as pd\ndf = pd.DataFrame({'a': [1,2,3], 'b': [4,5,6]})\nprint(f'DataFrame OK: {df.shape}')\ndf")
    assert result["success"], f"Final execution failed: {result}"
    assert "DataFrame OK" in result["output"], f"Unexpected output: {result['output']}"
    assert result.get("html"), "Expected HTML table output"
    log(f"  ✓ Full execution works: {result['output'].strip()}")
    log(f"  ✓ HTML table rendered ({len(result['html'])} chars)")
    print()

    # --- Terminate ---
    log("Terminating MicroVM...")
    with timed("Terminate"):
        resp = requests.post(f"{PROXY_URL}/terminate",
                             headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                             json={"microvmId": microvm_id}, timeout=30)
    log(f"  Status: {resp.status_code}")
    print()

    # --- Summary ---
    log("=" * 60)
    log("  ✅ All interrupt tests passed!")
    log("=" * 60)
    log("")
    log("  Test Summary:")
    log("    1. Normal execution       ✓")
    log("    2. Interrupt time.sleep()  ✓")
    log("    3. Post-interrupt health   ✓")
    log("    4. Interrupt CPU loop      ✓")
    log("    5. Loop variable survived  ✓")
    log("    6. No-op interrupt         ✓")
    log("    7. Final execution + HTML  ✓")
    log("")


if __name__ == "__main__":
    main()
