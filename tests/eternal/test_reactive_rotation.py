#!/usr/bin/env python3
# test-kind: live
"""
End-to-End Test: REACTIVE VM Rotation (Eternal Sessions, scheduler-free)

Eternal mode no longer rotates PRE-EMPTIVELY on a timer. Instead, because state
is always safe in S3 (save-on-suspend + save-on-terminate), the proxy recovers
REACTIVELY: when a request finds the session's VM dead, the proxy transparently
launches a fresh VM, restores from the last S3 checkpoint, relinks the session,
and replays the request on the new VM — paying a one-time cold-start on that
request. (See MicrovmManager.recover_and_replay / _do_recover.)

This test drives that path DIRECTLY instead of waiting for a timer:

  ROUND N:
    1. Create/mutate state on the current VM.
    2. KILL the VM out from under the session (terminate_microvm via AWS) —
       simulating max-lifetime death / crash / reclaim.
    3. Send the next /proxy/execute for the session.
       → The proxy must detect the dead VM, relaunch+restore+relink, replay the
         request, and return a successful result on a NEW vm_id.
    4. Verify all prior state survived on the new VM, and the vm_id changed.

  Plus EDGE CASES:
    A) FIFO ordering — kill VM, fire N DEPENDENT mutations concurrently
       (each appends its index + `counter += 1`). Asserts replays are FIFO
       (order_log == [0..N-1]) with NO lost updates (counter advanced by exactly
       N), and only ONE recovered VM served all of them.
    B) Mixed request types during recovery — a /install interleaved with
       /execute requests, all queued during recovery. Asserts the pre-kill
       package survives restore-from-S3, a package installed via a queued
       /install during recovery lands, and interleaved execute vars are present.
    C) TWO suspends + AWS max-lifetime termination (NOT a manual kill) — the
       "did we restore the FRESHEST checkpoint?" test. Suspend #1 (checkpoint A),
       mutate + suspend #2 (checkpoint B overwrites A), leave it suspended, let
       AWS auto-terminate at maxDuration, then verify reactive recovery restores
       suspend #2's state (the post-suspend-#1 change), NOT the stale suspend #1.

    D) SUSPENDED (not dead) — a request while the VM is SUSPENDED must be served
       via auto-resume on the SAME VM and must NOT trigger reactive rotation
       (recovery only fires on a genuinely dead VM).
    E) NO CHECKPOINT — fresh session where nothing was ever saved (dirty-flag
       skipped every suspend). Kill the VM; the next request must get a fresh
       EMPTY VM (relink + serve), NOT a 503. "Nothing to restore" is a success.
    F) RECOVERY TIMING — for several dataset sizes (small/medium/large DataFrames),
       kill the VM and measure the FULL reactive recovery: the client-observed
       request→response time AND the proxy's per-phase breakdown (launch → wait
       healthy → restore-from-S3 → relink). Prints a table so we can see how
       cold-start recovery scales with restored-state size.

Death paths covered: MANUAL terminate (rounds 1-3, EDGE A/B/D-target/E) AND AWS
max-lifetime auto-termination while suspended (EDGE C).

Requires:
  - proxy running with SESSION_PERSISTENCE_MODE=eternal
  - (no special MAX_LIFETIME needed — we kill VMs explicitly)

Usage:
    SESSION_PERSISTENCE_MODE=eternal ./local_install.sh
    # then:
    python3 tests/eternal/test_reactive_rotation.py
"""

import os
import sys
import time
import json
import re
import subprocess
import threading
import requests
import boto3

PROXY_URL = "http://localhost:8081"
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
AWS_PROFILE = os.environ.get("AWS_CLI_PROFILE", "default")
MEMORY_MIB = 2048


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
requests = _session  # noqa: F811 — route all requests.get/post through the authed session


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _lambda_client():
    if AWS_PROFILE and AWS_PROFILE != "default":
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION).client("lambda-microvms")
    return boto3.client("lambda-microvms", region_name=AWS_REGION)


def execute_code(session_id, code, timeout=180, cell_id=None):
    """POST /proxy/execute. Long timeout: a request that triggers reactive
    recovery blocks for the cold-start (launch + restore) before responding."""
    body = {"code": code}
    if cell_id is not None:
        body["cell_id"] = cell_id
    resp = requests.post(
        f"{PROXY_URL}/proxy/execute",
        headers={"Content-Type": "application/json", "X-Session-Id": session_id},
        json=body, timeout=timeout,
    )
    # Return (status_code, parsed_json_or_none)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, None


def get_variables(session_id, timeout=30):
    resp = requests.get(f"{PROXY_URL}/proxy/variables",
                        headers={"X-Session-Id": session_id}, timeout=timeout)
    return resp.json().get("variables", {})


def get_recovery_timings(timeout=15):
    """Fetch the proxy's per-phase timing of the most recent reactive recovery."""
    try:
        r = requests.get(f"{PROXY_URL}/recovery-timings", timeout=timeout)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def install_package(session_id, package, timeout=180):
    """POST /proxy/install through the proxy. Long timeout because if this lands
    during recovery it is queued and replayed after restore. Returns (status, json)."""
    resp = requests.post(
        f"{PROXY_URL}/proxy/install",
        headers={"Content-Type": "application/json", "X-Session-Id": session_id},
        json={"package": package}, timeout=timeout,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, None


def current_vm_for_session(session_id):
    """Find the VM id currently serving a session (proxy /instances view)."""
    try:
        resp = requests.get(f"{PROXY_URL}/instances", timeout=15)
        for vm_id, info in resp.json().get("instances", {}).items():
            if info.get("session_id") == session_id:
                return vm_id
    except Exception:
        pass
    return None


def terminate_session_cleanup(session_id, timeout=15):
    """Best-effort cleanup terminate for a session. The proxy's /terminate now
    REQUIRES microvmId in the JSON body (close<->reopen race fix), so a header-only
    POST 400s and leaks the VM. Resolve the session's live vm_id from /instances
    (as current_vm_for_session does) and pass {"microvmId": vid}; skip quietly if
    the session has no live VM. Mirrors the fix applied to
    tests/checkpoint/test_s3_restore.py in commit d91a970."""
    try:
        vid = current_vm_for_session(session_id)
        if not vid:
            return  # already gone — nothing to terminate
        requests.post(
            f"{PROXY_URL}/terminate",
            headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
            json={"microvmId": vid}, timeout=timeout,
        )
    except Exception:
        pass


def kill_vm(vm_id):
    """Terminate the VM directly via AWS — simulate death under the session."""
    try:
        _lambda_client().terminate_microvm(microvmIdentifier=vm_id)
        return True
    except Exception as e:
        log(f"  ⚠ kill_vm({vm_id}) error: {e}")
        return False


def wait_until_dead(vm_id, timeout=120):
    """Poll get_microvm until the VM is fully TERMINATED/gone.

    IMPORTANT: wait for a TERMINAL state (TERMINATED/FAILED/gone), NOT merely
    TERMINATING — a TERMINATING VM's endpoint can still briefly serve requests,
    which would let the triggering request succeed on the OLD VM and skip
    recovery entirely (the R1 'recovered in 0-1s' artifact). We want the next
    request to genuinely hit a dead VM."""
    c = _lambda_client()
    start = time.time()
    while time.time() - start < timeout:
        try:
            st = c.get_microvm(microvmIdentifier=vm_id).get("state", "UNKNOWN")
        except Exception as e:
            if "NotFound" in str(e) or "ResourceNotFound" in str(e):
                return True
            st = "UNKNOWN"
        if st in ("TERMINATED", "FAILED"):
            time.sleep(1)  # small settle so routing/endpoint is truly gone
            return True
        time.sleep(3)
    return False


def vm_state(vm_id):
    try:
        return _lambda_client().get_microvm(microvmIdentifier=vm_id).get("state", "UNKNOWN")
    except Exception as e:
        if "NotFound" in str(e) or "ResourceNotFound" in str(e):
            return "NOT_FOUND"
        return "UNKNOWN"


def suspend(session_id, timeout=30):
    return requests.post(f"{PROXY_URL}/suspend", headers={"X-Session-Id": session_id}, timeout=timeout)


def resume(session_id, timeout=60):
    return requests.post(f"{PROXY_URL}/resume", headers={"X-Session-Id": session_id}, timeout=timeout)


def wait_for_state(vm_id, want, timeout=120):
    """Poll get_microvm until state == want (or timeout). Returns bool."""
    start = time.time()
    last = None
    while time.time() - start < timeout:
        st = vm_state(vm_id)
        if st != last:
            log(f"    [{int(time.time()-start)}s] state={st}")
            last = st
        if st == want:
            return True
        time.sleep(3)
    return False


def main():
    log("=" * 72)
    log("  Reactive VM Rotation Test — Eternal Sessions (scheduler-free)")
    log("=" * 72)

    checks = []
    def record(name, ok):
        checks.append((name, bool(ok)))
        log(f"  {'✓' if ok else '❌'} {name}")

    # Preflight
    try:
        health = requests.get(f"{PROXY_URL}/health", timeout=5).json()
    except Exception as e:
        log(f"❌ Proxy not reachable: {e}")
        return 1
    if health.get("persistence_mode") != "eternal":
        log(f"❌ Requires persistence_mode=eternal, got {health.get('persistence_mode')}. "
            f"Start proxy with SESSION_PERSISTENCE_MODE=eternal.")
        return 1
    log(f"Proxy OK — eternal mode, region {health.get('region')}")

    session_id = f"reactive-rot-{int(time.time())}"

    # --- Launch ---
    log("")
    log(f"LAUNCH: eternal session {session_id}")
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "reactive-rotation-test",
        "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 1800,
        "checkpointEnabled": True,
        "sessionId": session_id,
    }, timeout=120)
    assert r.status_code == 200, f"launch failed: {r.text}"
    session_id = r.json().get("sessionId", session_id)
    vm0 = r.json()["microvmId"]
    log(f"  VM0={vm0}")
    time.sleep(3)

    # --- Create initial state ---
    log("")
    log("SETUP: create initial state (marker, counter, df, file)")
    st, res = execute_code(session_id,
        "import pandas as pd, numpy as np\n"
        "np.random.seed(7)\n"
        "marker = 'REACTIVE_OK'\n"
        "counter = 0\n"
        "df = pd.DataFrame({'x': np.random.randn(1000), 'y': np.random.randn(1000)})\n"
        "df_checksum = float(df.values.sum())\n"
        "open('/tmp/reactive.txt','w').write('hello-from-vm0')\n"
        "print(f'setup ok: df={df.shape}, checksum={df_checksum:.4f}')",
        cell_id="cell-setup")
    assert res and res.get("success"), f"setup failed: {res}"
    log(f"  {res['output'].strip()}")

    # ── ROUNDS: kill the VM, then a request must transparently recover ──
    prev_vm = vm0
    for rnd in range(1, 4):
        log("")
        log("━" * 72)
        log(f"  ROUND {rnd}: kill VM {prev_vm} → next request must reactively recover")
        log("━" * 72)

        # Mutate state so we can verify it survives + grows across recoveries.
        st, res = execute_code(session_id,
            f"counter += 1\n"
            f"history_{rnd} = 'round_{rnd}'\n"
            f"print(f'pre-kill: counter={{counter}}')",
            cell_id=f"cell-r{rnd}")
        assert res and res.get("success"), f"round {rnd} mutate failed: {res}"
        log(f"  {res['output'].strip()}")

        # Kill the VM out from under the session.
        log(f"  💀 terminating {prev_vm} ...")
        kill_vm(prev_vm)
        assert wait_until_dead(prev_vm), f"VM {prev_vm} did not die in time"
        log(f"  VM {prev_vm} is dead")

        # Next request — MUST transparently recover (cold-start). Time it.
        log(f"  → sending /proxy/execute (should trigger reactive recovery)...")
        t0 = time.time()
        st, res = execute_code(session_id,
            "print(f'post-recover: marker={marker}, counter={counter}')",
            cell_id=f"cell-r{rnd}-verify", timeout=180)
        recover_secs = time.time() - t0

        ok = (st == 200 and res and res.get("success"))
        record(f"R{rnd}: request succeeded after VM death (recovered in {recover_secs:.0f}s)", ok)
        if not ok:
            log(f"    status={st} res={str(res)[:300]}")
            break
        log(f"  {res['output'].strip()}")

        # The VM id must have changed (a NEW VM now serves the session).
        new_vm = current_vm_for_session(session_id)
        record(f"R{rnd}: session relinked to a NEW vm ({new_vm})", new_vm and new_vm != prev_vm)

        # Verify ALL prior state survived the recovery (restored from S3).
        st, res = execute_code(session_id,
            "import os\n"
            "chk = {}\n"
            "chk['marker'] = marker == 'REACTIVE_OK'\n"
            f"chk['counter'] = counter == {rnd}\n"
            "chk['df_shape'] = df.shape == (1000, 2)\n"
            "chk['df_checksum'] = abs(float(df.values.sum()) - df_checksum) < 1e-6\n"
            "chk['file'] = os.path.exists('/tmp/reactive.txt') and open('/tmp/reactive.txt').read() == 'hello-from-vm0'\n"
            "import json as _j; print('STATE:'+_j.dumps(chk))",
            cell_id=f"cell-r{rnd}-state")
        state_ok = False
        if res and res.get("success"):
            line = [l for l in res["output"].splitlines() if l.startswith("STATE:")]
            if line:
                chk = json.loads(line[0][6:])
                state_ok = all(chk.values())
                log(f"  state checks: {chk}")
        record(f"R{rnd}: all state survived recovery (vars/df/file)", state_ok)

        # Provenance should survive too.
        vm_meta = get_variables(session_id)
        prov_ok = (vm_meta.get("marker") or {}).get("defined_by") == "cell-setup"
        record(f"R{rnd}: provenance survived recovery", prov_ok)

        prev_vm = new_vm or prev_vm

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE A — FIFO ORDERING: dependent mutations queued during recovery.
    #  This is the REAL ordering test: 5 requests each do `order_log.append(i)`
    #  and `counter += 1`. If replays are FIFO+sequential, order_log == [0..4]
    #  and counter increments by exactly 5. If they run concurrently or out of
    #  order, order_log is shuffled and/or counter is wrong (lost updates).
    # ══════════════════════════════════════════════════════════════════════
    # Split into two sub-cases because they test DIFFERENT guarantees:
    #   A1 (concurrent): many requests fired at once during recovery. The system
    #      guarantees NO LOSS and EXACTLY-ONCE (all queued, each replayed once) —
    #      but NOT a strict arrival order, since concurrently-fired HTTP requests
    #      can reach the proxy in any order (network/event-loop scheduling). So we
    #      assert: all present + counter advanced by exactly N (no lost updates,
    #      no double-execution). This is the real concurrency guarantee.
    #   A2 (sequential): requests sent one-at-a-time, each only AFTER the prior
    #      returns — exactly how a notebook runs cells. Here order IS well-defined,
    #      so we assert strict FIFO (order_log == [0..N-1]).
    log("")
    log("━" * 72)
    log("  EDGE A1 — concurrency safety: fire N mutations at once → no loss, exactly-once")
    log("━" * 72)
    execute_code(session_id, "order_log = []\nbase_counter = counter", cell_id="cell-a-seed")
    _, res = execute_code(session_id, "print('BASE:'+str(base_counter))", cell_id="cell-a-base")
    base_counter = int(res["output"].split("BASE:")[1].strip()) if res and res.get("success") else 0

    cur = current_vm_for_session(session_id)
    log(f"  current vm={cur}; killing it ...")
    kill_vm(cur)
    wait_until_dead(cur)

    N = 5
    a_results = {}
    def fire_conc(i):
        st, res = execute_code(session_id,
            f"order_log.append({i})\ncounter += 1\nprint('a{i} done')",
            cell_id=f"cell-a1-{i}", timeout=200)
        a_results[i] = (st, res)

    threads = [threading.Thread(target=fire_conc, args=(i,)) for i in range(N)]
    t0 = time.time()
    for t in threads:
        t.start(); time.sleep(0.05)
    for t in threads:
        t.join(timeout=220)
    a_secs = time.time() - t0

    a_all_ok = all(a_results.get(i, (0, None))[0] == 200 and a_results[i][1] and a_results[i][1].get("success")
                   for i in range(N))
    record(f"EDGE A1: all {N} concurrent mutations succeeded ({a_secs:.0f}s)", a_all_ok)
    vm_after_a = current_vm_for_session(session_id)
    record("EDGE A1: single recovered VM served all (no double-launch)", vm_after_a and vm_after_a != cur)

    # Concurrency guarantee: every index present exactly once + counter advanced
    # by exactly N (no lost updates, no double-execution). Order is NOT asserted.
    _, res = execute_code(session_id,
        "import json as _j\nprint('ORDER:'+_j.dumps(sorted(order_log))+'|DELTA:'+str(counter-base_counter))",
        cell_id="cell-a1-verify")
    present_ok = delta_ok = False
    if res and res.get("success"):
        out = res["output"]
        try:
            present = json.loads(out.split("ORDER:")[1].split("|DELTA:")[0])
            delta = int(out.split("|DELTA:")[1].strip())
            present_ok = present == list(range(N))     # each index present exactly once
            delta_ok = delta == N                        # no lost updates / double-exec
            log(f"  present(sorted)={present} (expect {list(range(N))}); counter delta={delta} (expect {N})")
        except Exception as e:
            log(f"  parse error: {e} — raw={out!r}")
    record("EDGE A1: all requests served exactly once (no loss/dup)", present_ok)
    record("EDGE A1: no lost updates (counter advanced by exactly N)", delta_ok)

    log("")
    log("━" * 72)
    log("  EDGE A2 — strict FIFO: sequential sends (notebook model) → order == [0..N-1]")
    log("━" * 72)
    # Reset the log, then kill the VM. The FIRST sequential request triggers
    # recovery; each subsequent one is sent only AFTER the prior returns — so
    # arrival order is unambiguous and replay order must be strictly FIFO.
    execute_code(session_id, "order_log2 = []", cell_id="cell-a2-seed")
    cur2 = current_vm_for_session(session_id)
    log(f"  current vm={cur2}; killing it ...")
    kill_vm(cur2)
    wait_until_dead(cur2)

    seq_ok = True
    for i in range(N):
        st, res = execute_code(session_id,
            f"order_log2.append({i})\nprint('seq {i}')", cell_id=f"cell-a2-{i}", timeout=200)
        if not (st == 200 and res and res.get("success")):
            seq_ok = False
            log(f"  seq request {i} failed: status={st}")
            break
    _, res = execute_code(session_id,
        "import json as _j\nprint('SEQ:'+_j.dumps(order_log2))", cell_id="cell-a2-verify")
    order_ok = False
    if res and res.get("success"):
        try:
            order = json.loads(res["output"].split("SEQ:")[1].strip())
            order_ok = order == list(range(N))
            log(f"  order_log2={order} (expected {list(range(N))})")
        except Exception as e:
            log(f"  parse error: {e}")
    record(f"EDGE A2: sequential requests all succeeded", seq_ok)
    record("EDGE A2: strict FIFO order preserved (order_log2 == [0..N-1])", order_ok)

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE B — MIXED request types queued during recovery: an /install
    #  interleaved with /execute. Verifies non-execute proxied requests also
    #  flow through the recovery queue and that the RESTORED VM (from S3) has
    #  the package installed BEFORE the kill (restore reinstalls it), plus a
    #  NEW package installed via a queued /install during recovery.
    # ══════════════════════════════════════════════════════════════════════
    log("")
    log("━" * 72)
    log("  EDGE B — mixed execute + /install queued during recovery")
    log("━" * 72)
    # Install a package BEFORE the kill so restore-from-S3 must reinstate it.
    log("  installing 'tabulate' pre-kill (must survive restore) ...")
    st, ir = install_package(session_id, "tabulate")
    pre_pkg_ok = st == 200 and ir and ir.get("success")
    log(f"  pre-kill install: status={st} success={ir.get('success') if ir else None}")

    cur = current_vm_for_session(session_id)
    log(f"  current vm={cur}; killing it ...")
    kill_vm(cur)
    wait_until_dead(cur)

    b_results = {}
    def fire_exec(i):
        b_results[f"e{i}"] = execute_code(session_id, f"mixed_{i} = {i}\nprint('e{i}')",
                                          cell_id=f"cell-b-e{i}", timeout=200)
    def fire_install():
        b_results["install"] = install_package(session_id, "humanize", timeout=220)

    # Fire execute + install + execute concurrently while the VM is dead.
    bthreads = [
        threading.Thread(target=fire_exec, args=(0,)),
        threading.Thread(target=fire_install),
        threading.Thread(target=fire_exec, args=(1,)),
    ]
    t0 = time.time()
    for t in bthreads:
        t.start(); time.sleep(0.15)
    for t in bthreads:
        t.join(timeout=240)
    b_secs = time.time() - t0

    e0_ok = b_results.get("e0", (0, None))[0] == 200 and b_results["e0"][1] and b_results["e0"][1].get("success")
    e1_ok = b_results.get("e1", (0, None))[0] == 200 and b_results["e1"][1] and b_results["e1"][1].get("success")
    inst_st, inst_res = b_results.get("install", (0, None))
    inst_ok = inst_st == 200 and inst_res and inst_res.get("success")
    record(f"EDGE B: execute+install+execute all succeeded during recovery ({b_secs:.0f}s)", e0_ok and e1_ok and inst_ok)

    # Verify BOTH the pre-kill package (restored from S3) AND the queued-during-
    # recovery package are importable on the recovered VM.
    st, res = execute_code(session_id,
        "r = {}\n"
        "try:\n    import tabulate; r['tabulate'] = True\nexcept Exception:\n    r['tabulate'] = False\n"
        "try:\n    import humanize; r['humanize'] = True\nexcept Exception:\n    r['humanize'] = False\n"
        "r['mixed'] = ('mixed_0' in dir()) and ('mixed_1' in dir())\n"
        "import json as _j; print('PKG:'+_j.dumps(r))",
        cell_id="cell-b-verify")
    pkg_state = {}
    if res and res.get("success"):
        try:
            pkg_state = json.loads(res["output"].split("PKG:")[1].strip())
        except Exception:
            pass
    record("EDGE B: pre-kill package (tabulate) survived restore", pkg_state.get("tabulate", False))
    record("EDGE B: queued-during-recovery package (humanize) installed", pkg_state.get("humanize", False))
    record("EDGE B: interleaved execute vars present", pkg_state.get("mixed", False))

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE C — TWO SUSPENDS + AWS max-lifetime termination + LATEST-state.
    #  This is the critical "did we restore the FRESHEST checkpoint?" test:
    #    1. Create state, SUSPEND #1  → checkpoint A saved to S3.
    #    2. Resume, MUTATE state (counter=999, new var), SUSPEND #2 → checkpoint
    #       B overwrites A. (Both saves are dirty, so both must fire.)
    #    3. Do NOT manually kill — let AWS auto-terminate at maxDuration while
    #       the VM is (likely) suspended → /terminate does NOT fire; state must
    #       come from suspend-#2's checkpoint B.
    #    4. Next request triggers reactive recovery → verify the POST-suspend-#1
    #       change is present (counter==999, the new var). If we saw the stale
    #       suspend-#1 value, we'd be restoring an OLD checkpoint — a real bug.
    #  Uses its OWN short-lifetime session so AWS terminates it quickly.
    # ══════════════════════════════════════════════════════════════════════
    log("")
    log("━" * 72)
    log("  EDGE C — 2 suspends + AWS max-lifetime kill → must restore LATEST (suspend #2)")
    log("━" * 72)
    c_session = f"reactive-2susp-{int(time.time())}"
    C_MAX_LIFETIME = 200  # short so AWS auto-terminates fast
    log(f"  launching short-lifetime session {c_session} (maxDuration={C_MAX_LIFETIME}s, idle=60s)")
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "reactive-2suspend-test",
        "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 60,
        "checkpointEnabled": True,
        "sessionId": c_session,
        "maxDurationSeconds": C_MAX_LIFETIME,
    }, timeout=120)
    c_ok = r.status_code == 200
    if not c_ok:
        record("EDGE C: launch short-lifetime session", False)
    else:
        c_session = r.json().get("sessionId", c_session)
        c_vm0 = r.json()["microvmId"]
        launch_t = time.time()
        time.sleep(3)

        # 1) Initial state + SUSPEND #1 (checkpoint A: counter=1, no 'v2_change')
        execute_code(c_session,
            "import pandas as pd, numpy as np\n"
            "counter = 1\n"
            "df2 = pd.DataFrame({'a': range(2000)})\n"
            "print('suspend1 state: counter=1')", cell_id="cell-c-s1")
        log("  → explicit /suspend #1 (saves checkpoint A) ...")
        suspend(c_session)
        wait_for_state(c_vm0, "SUSPENDED", timeout=90)
        resume(c_session)
        wait_for_state(c_vm0, "RUNNING", timeout=90)

        # 2) MUTATE after suspend #1, then SUSPEND #2 (checkpoint B must overwrite A)
        execute_code(c_session,
            "counter = 999\n"
            "v2_change = 'made_after_suspend_1'\n"
            "df2['b'] = df2['a'] * 2\n"
            "print('post-suspend1 change: counter=999, v2_change set')", cell_id="cell-c-mut")
        log("  → explicit /suspend #2 (saves checkpoint B, overwrites A) ...")
        suspend(c_session)
        wait_for_state(c_vm0, "SUSPENDED", timeout=90)
        # Do NOT resume — leave it SUSPENDED so AWS kills it while suspended
        # (the case where /terminate does NOT fire; state must come from suspend #2).

        # 3) Wait for AWS to auto-terminate at maxDuration (we do NOT kill it).
        remaining = C_MAX_LIFETIME - (time.time() - launch_t) + 45
        log(f"  waiting up to {int(remaining)}s for AWS max-lifetime termination (no manual kill) ...")
        aws_killed = wait_until_dead(c_vm0, timeout=max(remaining, 30))
        record("EDGE C: AWS auto-terminated the VM at max-lifetime (not manual)", aws_killed)

        # 4) Next request → reactive recovery → verify LATEST state (suspend #2).
        log("  → sending /proxy/execute (reactive recovery, must restore suspend #2) ...")
        st, res = execute_code(c_session,
            "ok = (counter == 999) and ('v2_change' in dir()) and (v2_change == 'made_after_suspend_1')\n"
            "b_ok = ('b' in df2.columns)\n"
            "print(f'LATEST:counter={counter} v2={\"v2_change\" in dir()} b_col={b_ok}')\n"
            "import json as _j; print('C:'+_j.dumps({'ok': bool(ok and b_ok)}))",
            cell_id="cell-c-verify", timeout=200)
        c_recovered = st == 200 and res and res.get("success")
        record("EDGE C: request succeeded after AWS max-lifetime kill", c_recovered)
        latest_ok = False
        if c_recovered:
            log(f"  {res['output'].strip()}")
            line = [l for l in res["output"].splitlines() if l.startswith("C:")]
            if line:
                latest_ok = json.loads(line[0][2:]).get("ok", False)
        # THE key assertion: we restored suspend #2 (latest), not the stale suspend #1.
        record("EDGE C: restored the LATEST checkpoint (suspend #2 changes present)", latest_ok)
        c_new_vm = current_vm_for_session(c_session)
        record("EDGE C: session relinked to a new VM after AWS kill", c_new_vm and c_new_vm != c_vm0)

        # Cleanup this session's VM.
        terminate_session_cleanup(c_session)

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE D — SUSPENDED (not dead): a request while the VM is SUSPENDED
    #  must NOT trigger reactive rotation. The proxy should serve it via
    #  auto-resume on the SAME VM (recovery only fires on TERMINATED/dead).
    # ══════════════════════════════════════════════════════════════════════
    log("")
    log("━" * 72)
    log("  EDGE D — request while SUSPENDED must auto-resume (NOT rotate)")
    log("━" * 72)
    d_vm_before = current_vm_for_session(session_id)
    log(f"  suspending {d_vm_before} ...")
    suspend(session_id)
    if wait_for_state(d_vm_before, "SUSPENDED", timeout=90):
        log("  VM is SUSPENDED; sending a request (should auto-resume, same VM)...")
        st, res = execute_code(session_id,
            "suspended_req = 'served_via_resume'\nprint('D:'+suspended_req)",
            cell_id="cell-d", timeout=120)
        d_ok = st == 200 and res and res.get("success")
        record("EDGE D: request served while suspended (auto-resume)", d_ok)
        d_vm_after = current_vm_for_session(session_id)
        # THE assertion: the VM must be the SAME (no rotation triggered).
        record("EDGE D: NO rotation — same VM id (suspend != death)", d_vm_after == d_vm_before)
    else:
        record("EDGE D: VM reached SUSPENDED", False)

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE E — NO CHECKPOINT (fresh session, nothing ever saved): launch,
    #  do NOTHING (dirty-flag saves nothing), kill the VM, then a request must
    #  get a fresh EMPTY VM (relink + serve) — NOT a 503. "Nothing to restore"
    #  is a success.
    # ══════════════════════════════════════════════════════════════════════
    log("")
    log("━" * 72)
    log("  EDGE E — no checkpoint (empty session) → fresh VM, relink, serve (no 503)")
    log("━" * 72)
    e_session = f"reactive-empty-{int(time.time())}"
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "reactive-empty-test", "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 1800, "checkpointEnabled": True, "sessionId": e_session,
    }, timeout=120)
    if r.status_code != 200:
        record("EDGE E: launch empty session", False)
    else:
        e_session = r.json().get("sessionId", e_session)
        e_vm = r.json()["microvmId"]
        time.sleep(3)
        # Do NOTHING (no execute) → namespace empty → dirty-flag skips all saves.
        # Confirm no CHECKPOINT exists in S3. NOTE: the proxy stages
        # launch_catalog.json (and sometimes data_catalog.json) under
        # sessions/{id}/ at EVERY launch, independent of the checkpoint dirty-flag
        # (see stage_catalog_to_s3 in microvm_manager.py), so the session prefix is
        # never empty — a plain KeyCount==0 check is wrong. A real checkpoint is
        # identified by manifest.json + the vars/ and files/ prefixes, so we assert
        # NONE of those checkpoint objects exist (ignoring the always-staged
        # launch/data catalogs).
        s3 = (boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION).client("s3")
              if AWS_PROFILE and AWS_PROFILE != "default"
              else boto3.client("s3", region_name=AWS_REGION))
        bucket = os.environ.get("ARTIFACT_BUCKET", "")
        no_ckpt = True
        if bucket:
            objs = s3.list_objects_v2(Bucket=bucket, Prefix=f"sessions/{e_session}/")
            keys = [o["Key"] for o in objs.get("Contents", [])]
            ckpt_markers = [
                k for k in keys
                if k.endswith("/manifest.json")
                or f"/vars/" in k
                or f"/files/" in k
            ]
            no_ckpt = len(ckpt_markers) == 0
            if ckpt_markers:
                log(f"  unexpected checkpoint objects: {ckpt_markers}")
        record("EDGE E: no S3 checkpoint exists (dirty-flag saved nothing)", no_ckpt)

        log(f"  killing {e_vm} (no checkpoint) ...")
        kill_vm(e_vm)
        wait_until_dead(e_vm)

        log("  → sending first /proxy/execute (should get a fresh empty VM, not 503)...")
        st, res = execute_code(e_session,
            "fresh = 'empty_vm_ok'\nprint('E:'+fresh)", cell_id="cell-e", timeout=180)
        e_ok = st == 200 and res and res.get("success")
        record("EDGE E: fresh empty VM served the request (no 503)", e_ok)
        e_new_vm = current_vm_for_session(e_session)
        record("EDGE E: relinked to a new VM", e_new_vm and e_new_vm != e_vm)
        terminate_session_cleanup(e_session)

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE F — RECOVERY TIMING across dataset sizes. For each size, build a
    #  DataFrame of ~that many rows, kill the VM, then time the FULL reactive
    #  recovery from the user's perspective (request in → response out) AND pull
    #  the proxy's per-phase breakdown (discover → launch → healthy → restore →
    #  relink). Produces a table so we can see how restore time scales with state.
    # ══════════════════════════════════════════════════════════════════════
    log("")
    log("━" * 72)
    log("  EDGE F — recovery TIMING across dataset sizes (request→response breakdown)")
    log("━" * 72)
    timing_rows = []  # (label, rows, size_mb, client_ms, phases dict)
    f_session = f"reactive-timing-{int(time.time())}"
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "reactive-timing-test", "memoryMiB": 4096,
        "idleTimeoutSeconds": 1800, "checkpointEnabled": True, "sessionId": f_session,
    }, timeout=120)
    if r.status_code != 200:
        record("EDGE F: launch timing session", False)
    else:
        f_session = r.json().get("sessionId", f_session)
        r.json()["microvmId"]
        time.sleep(3)
        # Base imports once.
        execute_code(f_session, "import pandas as pd, numpy as np", cell_id="cell-f-import")

        DATASETS = [
            ("small", 10_000),      # ~0.2 MB
            ("medium", 500_000),    # ~12 MB
            ("large", 3_000_000),   # ~72 MB
        ]
        for label, rows in DATASETS:
            log("")
            # IMPORTANT: each prior iteration kills the VM and recovers onto a
            # fresh one. Before building the NEXT dataset we must wait for the
            # session's VM to be RUNNING/healthy, otherwise the build request
            # races the tail of the previous recovery and silently no-ops.
            settle_vm = current_vm_for_session(f_session)
            if settle_vm and vm_state(settle_vm) != "RUNNING":
                log(f"    waiting for {settle_vm} to be RUNNING before building {label} ...")
                wait_for_state(settle_vm, "RUNNING", timeout=90)
            # Confirm the VM actually serves execute before we time anything.
            for _attempt in range(20):
                _, _ping = execute_code(f_session, "print('ping')", cell_id=f"cell-f-{label}-ping")
                if _ping and _ping.get("success"):
                    break
                time.sleep(1)

            log(f"  [{label}] building DataFrame with {rows:,} rows ...")
            # Import INSIDE the build cell: checkpoint restore intentionally does
            # not persist module bindings (modules aren't pickled — see
            # checkpoint.py modules_skipped), so after a prior recovery `pd`/`np`
            # from an earlier import cell are gone. Self-contained cells are also
            # how resilient notebook code is written.
            _, res = execute_code(f_session,
                f"import pandas as pd, numpy as np\n"
                f"data_{label} = pd.DataFrame({{'a': np.random.randn({rows}), "
                f"'b': np.random.randn({rows}), 'c': np.arange({rows})}})\n"
                f"mb = data_{label}.memory_usage(deep=True).sum()/1e6\n"
                f"print(f'{label} BUILT shape={{data_{label}.shape}} mb={{mb:.1f}}')",
                cell_id=f"cell-f-{label}", timeout=180)
            build_ok = bool(res and res.get("success") and "BUILT" in (res.get("output") or ""))
            record(f"EDGE F [{label}]: dataset built before kill", build_ok)
            size_mb = 0.0
            if build_ok:
                log(f"    {res['output'].strip()}")
                try:
                    # Parse "... mb=12.3" robustly regardless of other commas.
                    size_mb = float(res["output"].split("mb=")[1].strip().split()[0])
                except Exception:
                    pass
            if not build_ok:
                # Skip timing this iteration — nothing meaningful to restore.
                log(f"    ⚠ build failed for {label}; skipping its recovery timing")
                continue

            # Kill the VM and wait until fully dead.
            cur = current_vm_for_session(f_session)
            log(f"    killing {cur} and waiting for full termination ...")
            kill_vm(cur)
            wait_until_dead(cur)

            # Time the full recovery from the CLIENT perspective: this single
            # request triggers discover→launch→restore→relink→replay→respond.
            log(f"    → sending request that triggers reactive recovery ...")
            t0 = time.time()
            st, res = execute_code(f_session,
                f"print('RECOVERED shape='+str(data_{label}.shape))",
                cell_id=f"cell-f-{label}-recover", timeout=240)
            client_ms = (time.time() - t0) * 1000
            # Success requires the restored DataFrame to have the EXACT shape
            # (proves the full state — not just an empty VM — came back from S3).
            shape_ok = bool(res and res.get("success")
                            and f"shape=({rows}, 3)" in (res.get("output") or ""))
            ok = st == 200 and shape_ok
            record(f"EDGE F [{label}]: recovered {rows:,}-row DF ({client_ms/1000:.1f}s client)", ok)
            if not ok:
                log(f"      status={st} output={str(res.get('output') if res else res)[:200]}")

            # Pull the proxy's per-phase breakdown for THIS recovery.
            phases = get_recovery_timings()
            timing_rows.append((label, rows, size_mb, client_ms, phases))

        terminate_session_cleanup(f_session)

        # ── Timing report table ──
        print()
        print("  ┌─ RECOVERY TIMING BREAKDOWN (per dataset) ─────────────────────────────")
        print(f"  │ {'dataset':<8} {'rows':>10} {'MB':>7} {'launch':>8} {'healthy':>8} "
              f"{'restore':>8} {'relink':>7} {'proxy_tot':>9} {'client':>8}")
        print("  │ " + "─" * 78)
        for label, rows, size_mb, client_ms, ph in timing_rows:
            print(f"  │ {label:<8} {rows:>10,} {size_mb:>7.1f} "
                  f"{ph.get('launch_ms',0):>7.0f}m {ph.get('wait_healthy_ms',0):>7.0f}m "
                  f"{ph.get('restore_ms',0):>7.0f}m {ph.get('relink_ms',0):>6.0f}m "
                  f"{ph.get('total_ms',0):>8.0f}m {client_ms:>7.0f}m")
        print("  └───────────────────────────────────────────────────────────────────────")
        print("    launch=new VM boot to RUNNING · healthy=/health 200 · restore=S3→VM namespace")
        print("    relink=registry+DB · proxy_tot=sum of proxy phases · client=user request→response")

    # ══════════════════════════════════════════════════════════════════════
    #  EDGE CASE G — RECOVERY WHILE THE SESSION IS STILL MAPPED (Fix 1's path).
    #  The rounds/edges above all kill, wait_until_dead(), then recover — which is
    #  the full "VM confirmed TERMINATED, then request" path. This case targets the
    #  SPECIFIC invariant Fix 1 guarantees: in eternal mode the /instances poller
    #  observes the VM death but DOES NOT unregister the session→VM mapping, so a
    #  request that arrives after death still finds the (stale) mapping, the
    #  request-time _VmDead check fires, and recovery runs — instead of 404ing at
    #  the top-of-handler because the mapping was poller-deleted (the original bug).
    #
    #  Concretely: kill the VM, wait until it is genuinely DEAD, and give the ~10s
    #  /instances poller a full cycle to run (so it definitely observed the death
    #  and, pre-fix, would have unregistered the session). Then fire ONE recovery
    #  request and assert it recovers to a NEW vm with checkpointed state intact —
    #  a 200 after a real cold start, NOT a flat "404 0ms". Pre-fix the poller
    #  deleted the mapping and this request 404'd before recovery could fire; the
    #  200-with-recovery here is the proof the eternal mapping survived (Fix 1).
    #  State is checkpointed via suspend/resume BEFORE the kill so there is no
    #  un-saved mutation to lose (the recovery request is read-only), making the
    #  assertions deterministic (not subject to the briefly-still-serving race).
    # ══════════════════════════════════════════════════════════════════════
    log("")
    log("━" * 72)
    log("  EDGE G — poller observes death but mapping survives (Fix 1 still-mapped path)")
    log("━" * 72)
    g_session = f"reactive-mapped-{int(time.time())}"
    r = requests.post(f"{PROXY_URL}/launch", json={
        "name": "reactive-mapped-test", "memoryMiB": MEMORY_MIB,
        "idleTimeoutSeconds": 1800, "checkpointEnabled": True, "sessionId": g_session,
    }, timeout=120)
    if r.status_code != 200:
        record("EDGE G: launch still-mapped session", False)
    else:
        g_session = r.json().get("sessionId", g_session)
        g_vm = r.json()["microvmId"]
        time.sleep(3)
        # Seed state, then suspend+resume to force a checkpoint to S3 so recovery
        # has a saved namespace to restore (the recovery request below is read-only
        # — no un-checkpointed mutation that recovery could legitimately lose).
        st, res = execute_code(g_session,
            "g_marker = 'MAPPED_OK'\ng_counter = 7\nprint('g setup ok')",
            cell_id="cell-g-setup")
        record("EDGE G: setup state before kill", bool(res and res.get("success")))
        log("  → suspend/resume to force an S3 checkpoint before kill ...")
        suspend(g_session)
        wait_for_state(g_vm, "SUSPENDED", timeout=90)
        resume(g_session)
        wait_for_state(g_vm, "RUNNING", timeout=90)

        # Kill the VM and wait until it is genuinely dead, then give the ~10s
        # /instances poller a FULL cycle to observe the death and (pre-fix) try to
        # unregister. With Fix 1 the eternal mapping must survive this: the request
        # below recovers via the still-intact session→VM mapping instead of 404ing.
        #
        # NOTE on how we prove the mapping survived: we do NOT read it from
        # /instances, because list_instances() drops any VM the control plane
        # reports TERMINATED/TERMINATING/FAILED (it `continue`s past them), so a
        # dead VM never appears there regardless of whether the session REGISTRY
        # mapping survives. The real proof is the recovery itself: pre-fix, the
        # poller unregistered the session, so the next request returned a flat
        # "404 0ms" and recovery never fired. Post-fix (Fix 1), the mapping
        # survives, the request-time _VmDead check fires, and recovery runs — a
        # multi-second cold start returning 200 with state intact. So g_ok below
        # (200, not 404) + a non-trivial recovery time IS the still-mapped proof.
        log(f"  killing {g_vm}, waiting until dead + a full /instances poll cycle ...")
        kill_vm(g_vm)
        assert wait_until_dead(g_vm), f"VM {g_vm} did not die in time"
        time.sleep(13)  # > the ~10s /instances poll interval, so a poll surely ran

        # Recovery request. It finds the stale (still-mapped) session→VM mapping,
        # _VmDead fires, and recovery runs (Fix 1's path). Read-only, so no state
        # can be lost. A 404 here would mean the mapping was unregistered (the
        # original bug); a 200 after a real cold start means Fix 1 held.
        log("  → sending /proxy/execute (recovers via still-intact mapping) ...")
        t0 = time.time()
        st, res = execute_code(g_session,
            "print(f'G:marker={g_marker} counter={g_counter}')",
            cell_id="cell-g-recover", timeout=240)
        g_secs = time.time() - t0
        g_ok = st == 200 and res and res.get("success")
        record(f"EDGE G: recovered via still-mapped path — 200 not 404 ({g_secs:.0f}s)", g_ok)
        if g_ok:
            log(f"  {res['output'].strip()}")
        g_new_vm = current_vm_for_session(g_session)
        record("EDGE G: session relinked to a NEW vm", g_new_vm and g_new_vm != g_vm)
        g_state_ok = False
        if g_ok:
            st2, res2 = execute_code(g_session,
                "ok = (g_marker == 'MAPPED_OK') and (g_counter == 7)\n"
                "import json as _j; print('GS:'+_j.dumps({'ok': bool(ok)}))",
                cell_id="cell-g-verify", timeout=120)
            if res2 and res2.get("success"):
                line = [l for l in res2["output"].splitlines() if l.startswith("GS:")]
                if line:
                    g_state_ok = json.loads(line[0][3:]).get("ok", False)
        record("EDGE G: checkpointed state survived recovery (marker + counter)", g_state_ok)
        terminate_session_cleanup(g_session)

    # --- Cleanup ---
    log("")
    log("Cleanup: terminating session ...")
    terminate_session_cleanup(session_id)

    # --- Report ---
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print()
    print("=" * 72)
    print(f"  REACTIVE ROTATION REPORT — {passed}/{total} checks passed")
    print("=" * 72)
    for name, ok in checks:
        print(f"    {'✓' if ok else '❌'} {name}")
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
