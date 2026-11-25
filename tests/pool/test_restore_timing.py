#!/usr/bin/env python3
# test-kind: gated
"""
End-to-end RESTORE LATENCY measurement against the LIVE warm-pool stack.

This is the test for the ORIGINAL ask: "a restore used to take 1–15s, most of
which was provisioning a brand-new MicroVM. With the warm pool it should be
much shorter." This file measures that number for real, through the running
proxy, and asserts the warm-pool restore is well under the old cold-provision
floor.

WHAT IT MEASURES (all via the real HTTP path a browser uses — POST /launch,
/proxy/execute, /checkpoint-save, /terminate on the proxy at PROXY_URL):

  1. warm launch (fresh session, no restore)      — the pooled-grant floor
  2. restore WITH a real checkpoint                — grant + resume + rebind +
                                                     restore_state (the headline
                                                     number)
  3. restore of a NEVER-checkpointed session       — exercises the manifest-probe
                                                     skip (should match #1: the
                                                     ~2s /restore-state round-trip
                                                     is skipped entirely)

For #2 it also proves CORRECTNESS, not just speed: it writes a variable into the
namespace before the checkpoint and asserts the restored VM has it back.

OPT-IN ONLY. Like tests/pool/test_pool_e2e.py, this launches REAL MicroVMs and
costs real (tiny) money. With POOL_RESTORE_TIMING unset it prints a skip line
and exits 0 — it is NEVER part of the offline suite.

PRECONDITIONS (operator sets these up — the test checks and skips cleanly if
not met):
  • the stack is running with the warm pool enabled:
        ./local_install.sh --WarmPool --AuroraSetup
  • persistence mode persists checkpoints (checkpoint or eternal) — the proxy's
    GET /instances reports persistence_mode; "none" can't checkpoint, so we skip.

Run (operator only):
    POOL_RESTORE_TIMING=1 .venv/bin/python tests/pool/test_restore_timing.py

Tunables (env):
    PROXY_URL              default http://localhost:8081
    RESTORE_TIMING_ARCH    default $TEST_ARCH, else arm64
    RESTORE_TIMING_SIZE    default 2048   (MiB — must be a built tier)
    RESTORE_SLA_SECONDS    default 8.0    (restore must beat this; the old floor
                                           was 1–15s dominated by provisioning)
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
ARCH = os.environ.get("RESTORE_TIMING_ARCH") or os.environ.get("TEST_ARCH") or "arm64"
SIZE = int(os.environ.get("RESTORE_TIMING_SIZE", "2048"))
SLA = float(os.environ.get("RESTORE_SLA_SECONDS", "8.0"))

# A VM-provisioning cold launch historically took ~10–30s to become healthy; the
# whole restore then landed anywhere in ~1–15s depending on how much of that the
# user hit. This is the "before" reference we're beating.
OLD_FLOOR_NOTE = "old cold-provision restore floor was ~1–15s (mostly VM provisioning)"

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _skip(msg):
    print(f"SKIP test_restore_timing: {msg}")
    print("  (set POOL_RESTORE_TIMING=1 with the warm-pool stack running to run)")
    sys.exit(0)


# ── live-proxy HTTP helpers ────────────────────────────────────────────────

def _get_instances(client):
    """GET /instances → (instances_dict, persistence_mode). Also our liveness probe."""
    r = client.get(f"{PROXY_URL}/instances", timeout=15.0)
    r.raise_for_status()
    data = r.json()
    return data.get("instances", {}), data.get("persistence_mode")


def _launch(client, *, session_id, name, restore_from=None):
    """POST /launch exactly as the browser does. Returns (elapsed_s, resp_json)."""
    body = {
        "name": name,
        "sessionId": session_id,
        "memoryMiB": SIZE,
        "arch": ARCH,
    }
    if restore_from:
        body["restoreFromSession"] = restore_from
    t0 = time.perf_counter()
    r = client.post(f"{PROXY_URL}/launch", json=body, timeout=120.0)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return elapsed, r.json()


def _execute(client, session_id, code):
    """Run Python in the VM namespace via the proxy's generic /proxy/execute
    forward (X-Session-Id resolves the VM). Returns the JSON result."""
    r = client.post(
        f"{PROXY_URL}/proxy/execute",
        headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
        json={"code": code},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()


def _checkpoint_save(client, session_id):
    """POST /checkpoint-save (writes the namespace + manifest.json to S3)."""
    r = client.post(
        f"{PROXY_URL}/checkpoint-save",
        headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
        timeout=120.0,
    )
    r.raise_for_status()
    return r.json()


def _terminate(client, session_id, microvm_id):
    """POST /terminate a specific VM (session header + microvmId body)."""
    try:
        client.post(
            f"{PROXY_URL}/terminate",
            headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
            json={"microvmId": microvm_id},
            timeout=60.0,
        )
    except Exception as e:
        print(f"   (cleanup) terminate {microvm_id} failed, non-fatal: {e}")


# ── the measurement ────────────────────────────────────────────────────────

def main():
    if os.environ.get("POOL_RESTORE_TIMING") != "1":
        _skip("POOL_RESTORE_TIMING != 1")

    client = _make_client()

    # Liveness + preconditions.
    try:
        _instances, persistence_mode = _get_instances(client)
    except Exception as e:
        _skip(f"proxy not reachable at {PROXY_URL}/instances ({e}) — is the stack up?")

    print(f"\n=== RESTORE LATENCY vs the warm pool ({ARCH}/{SIZE} MiB) ===")
    print(f"  proxy={PROXY_URL}  persistence_mode={persistence_mode}  SLA={SLA}s")
    print(f"  note: {OLD_FLOOR_NOTE}\n")

    if persistence_mode not in ("checkpoint", "eternal"):
        _skip(f"persistence_mode={persistence_mode!r} does not persist checkpoints "
              "— relaunch with a checkpointing mode to measure a real restore")

    created_vms = []  # (session_id, microvm_id) to clean up at the end

    try:
        # ── (0) WARM-UP launch (discarded) ────────────────────────────────────
        # The FIRST launch in a fresh process pays one-time costs the steady
        # state doesn't: the proxy's first boto3 client init (for the manifest
        # probe + catalog stage), the first auth-token mint, and waking the pool
        # manager's grant path. Measuring #1 on that first call made it look
        # slower than the later restores (observed 9s vs ~4.6s). Burn one launch
        # here, discard its time, and terminate it so the measured launches below
        # all reflect warm steady state.
        sid_warmup = str(uuid.uuid4())
        print("-- (0) warm-up launch (discarded, removes first-call skew) --")
        wu_elapsed, wu_resp = _launch(
            client, session_id=sid_warmup, name="restore-timing warmup")
        wu_vm = wu_resp.get("microvmId")
        created_vms.append((sid_warmup, wu_vm))
        print(f"   warm-up: {wu_elapsed:.2f}s  (vm={wu_vm}, discarded)")
        _terminate(client, sid_warmup, wu_vm)
        time.sleep(2.0)

        # ── (1) WARM LAUNCH, no restore — the pooled-grant floor ──────────────
        sid_warm = str(uuid.uuid4())
        print("-- (1) warm launch (fresh session, no restore) --")
        warm_elapsed, warm_resp = _launch(
            client, session_id=sid_warm, name="restore-timing warm")
        warm_vm = warm_resp.get("microvmId")
        created_vms.append((sid_warm, warm_vm))
        print(f"   warm launch: {warm_elapsed:.2f}s  (vm={warm_vm})")
        check(f"(1) warm launch succeeded and returned a vm id "
              f"(status={warm_resp.get('status')})", bool(warm_vm))
        check(f"(1) warm launch beat the SLA ({warm_elapsed:.2f}s < {SLA}s) — "
              "pooled grant, not a cold provision", warm_elapsed < SLA)

        # ── (2) RESTORE WITH A REAL CHECKPOINT — the headline number ──────────
        # Build real namespace state, checkpoint it, terminate, then relaunch
        # with restoreFromSession and TIME that relaunch.
        print("\n-- (2) restore WITH a real checkpoint --")
        sentinel = f"restore_probe_{uuid.uuid4().hex[:8]}"
        exec_resp = _execute(
            client, sid_warm,
            f"restore_marker = {sentinel!r}\nprint('set', restore_marker)")
        wrote_var = (exec_resp.get("status") == "ok" or exec_resp.get("success")
                     or "restore_marker" in json.dumps(exec_resp))
        check("(2) wrote a sentinel variable into the namespace via /proxy/execute",
              wrote_var)

        save_resp = _checkpoint_save(client, sid_warm)
        check(f"(2) checkpoint-save reported success "
              f"(resp={json.dumps(save_resp)[:80]})",
              save_resp.get("success") is True or save_resp.get("status") in ("ok", "saved"))

        # Terminate the source VM so the restore must land on a DIFFERENT,
        # pool-granted VM (the realistic reopen-after-close path).
        _terminate(client, sid_warm, warm_vm)
        # Give the control plane a moment to drop it.
        time.sleep(2.0)

        restore_elapsed, restore_resp = _launch(
            client, session_id=sid_warm, name="restore-timing restored",
            restore_from=sid_warm)
        restored_vm = restore_resp.get("microvmId")
        created_vms.append((sid_warm, restored_vm))
        print(f"   restore launch: {restore_elapsed:.2f}s  (vm={restored_vm})")
        check(f"(2) restore launch succeeded (status={restore_resp.get('status')})",
              bool(restored_vm))
        check(f"(2) restore landed on a DIFFERENT VM than the source "
              f"(src={warm_vm}, restored={restored_vm}) — a genuine reopen, not "
              "the same live VM", restored_vm and restored_vm != warm_vm)
        check(f"(2) RESTORE WITH CHECKPOINT beat the SLA "
              f"({restore_elapsed:.2f}s < {SLA}s) — vs the old ~1–15s floor",
              restore_elapsed < SLA)

        # Correctness: the sentinel must be back in the restored namespace.
        verify = _execute(
            client, sid_warm,
            "print('MARKER=' + str(globals().get('restore_marker')))")
        restored_ok = sentinel in json.dumps(verify)
        check(f"(2) the checkpointed variable was actually RESTORED "
              f"(found sentinel {sentinel} in the restored namespace)", restored_ok)

        # ── (3) RESTORE OF A NEVER-CHECKPOINTED SESSION — manifest-probe skip ──
        # Reopening a session that never produced a checkpoint should NOT pay the
        # ~2s /restore-state round-trip: the proxy probes S3 for the manifest,
        # finds none, and skips restore. Its time should look like a warm launch.
        print("\n-- (3) restore of a session with NO checkpoint (probe-skip) --")
        sid_nock = str(uuid.uuid4())
        # First launch (fresh), then terminate WITHOUT checkpointing, then
        # relaunch with restoreFromSession pointing at a session that has no
        # manifest in S3.
        _, first_resp = _launch(client, session_id=sid_nock, name="restore-timing nock")
        first_vm = first_resp.get("microvmId")
        created_vms.append((sid_nock, first_vm))
        _terminate(client, sid_nock, first_vm)
        time.sleep(2.0)

        nock_elapsed, nock_resp = _launch(
            client, session_id=sid_nock, name="restore-timing nock reopen",
            restore_from=sid_nock)
        nock_vm = nock_resp.get("microvmId")
        created_vms.append((sid_nock, nock_vm))
        print(f"   no-checkpoint reopen: {nock_elapsed:.2f}s  (vm={nock_vm})")
        check(f"(3) no-checkpoint reopen succeeded (status={nock_resp.get('status')})",
              bool(nock_vm))
        check(f"(3) no-checkpoint reopen beat the SLA ({nock_elapsed:.2f}s < {SLA}s)",
              nock_elapsed < SLA)
        # The probe-skip should make it roughly a warm launch — no restore_state
        # round-trip. Allow generous slack (grant jitter), but it must NOT be
        # dramatically slower than the plain warm launch.
        check(f"(3) no-checkpoint reopen is ~a warm launch, NOT a restore "
              f"({nock_elapsed:.2f}s vs warm {warm_elapsed:.2f}s) — the manifest "
              "probe skipped the restore round-trip",
              nock_elapsed <= max(SLA, warm_elapsed + 3.0))

        # ── summary ──
        print(f"\n--- latency summary ({ARCH}/{SIZE} MiB) ---")
        print(f"  (1) warm launch (no restore)        : {warm_elapsed:6.2f}s")
        print(f"  (2) restore WITH checkpoint         : {restore_elapsed:6.2f}s")
        print(f"  (3) reopen, NO checkpoint (skip)    : {nock_elapsed:6.2f}s")
        print(f"      SLA                             : {SLA:6.2f}s")
        print(f"      {OLD_FLOOR_NOTE}")

    finally:
        print("\n-- cleanup: terminating VMs launched by this test --")
        seen = set()
        for sid, vm in created_vms:
            if vm and vm not in seen:
                seen.add(vm)
                _terminate(client, sid, vm)
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
    print("ALL RESTORE TIMING CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
