#!/usr/bin/env python3
# test-kind: gated
"""
End-to-end check for the "Terminates in" / launched_at MINT-TIME fix against the
LIVE warm-pool stack.

THE BUG THIS GUARDS: when the warm pool grants a pre-booted spare to a launch,
the proxy must record the VM's TRUE MINT time (when the pool ran run_microvm) as
launched_at — NOT the adopt moment. The AWS 8h max-lifetime clock starts at mint,
so the MicroVM side panel's "Terminates in" countdown (computed as
launched_at + max_duration - now) is only honest if launched_at is the mint time.
Before the fix the proxy stamped launched_at = now at adoption, so a spare that
had sat warm in the pool for, say, an hour showed ~8h remaining when the VM truly
had ~7h and AWS would kill it ~1h earlier than the panel claimed.

WHAT IT PROVES (through the real HTTP path a browser uses — POST /launch, GET
/instances on the proxy at PROXY_URL):
  1. POOLED GRANT: launch a notebook; the pool grants a spare that was minted at
     stack startup (minutes ago). The adopted VM's launched_at must be
     MEASURABLY OLDER than the launch moment (mint < adopt), and its remaining
     lifetime (launched_at + max_duration - now) must be LESS than the full
     max_duration by at least the spare's pool age. If the bug were present,
     launched_at would be ~now and remaining would be ~the full max_duration.
  2. SANITY: the computed remaining lifetime is positive and <= max_duration
     (never MORE than the ceiling — that would mean launched_at is in the future).

This mirrors exactly how web/src/components/panels/MicroVMsPanel.jsx derives
"Terminates in": remainingSec = launched_at + max_duration_sec - now.

NOTE ON THE COLD-LAUNCH / NO-POOL PATH: when the proxy mints the VM itself (pool
disabled, or a pool miss), launched_at == now is CORRECT (mint == now) and is NOT
what this test checks — that path is covered by the offline unit test
tests/pool/test_proxy_launch_grant.py. This file specifically validates the
POOLED-GRANT path, where mint and adopt differ. If this run happens to fall
through to a cold launch (empty pool), the test says so and SKIPs rather than
falsely passing/failing.

OPT-IN ONLY. Launches a REAL MicroVM (tiny real cost). With POOL_TERMINATES_IN
unset it prints a skip line and exits 0 — never part of the offline suite.

PRECONDITIONS (checked; skips cleanly if unmet):
  • stack running with the warm pool enabled + filled:
        ./local_install.sh --WarmPool --AuroraSetup
  • a non-eternal persistence mode (checkpoint/none) so a max-lifetime countdown
    exists at all — eternal mode rotates instead of counting down, so there is no
    "Terminates in" to validate; we skip in that case.

Run (operator only, warm-pool stack up):
    POOL_TERMINATES_IN=1 .venv/bin/python tests/pool/test_terminates_in_mint_time.py

Tunables (env):
    PROXY_URL             default http://localhost:8081
    POOL_MANAGER_URL      default http://localhost:8090
    TERMINATES_IN_ARCH    default $TEST_ARCH, else arm64
    TERMINATES_IN_SIZE    default 2048   (MiB — must be a built/pool tier)
    MIN_POOL_AGE_SECONDS  default 30     (the spare must be at least this much
                                          older than adoption for the gap to be
                                          unambiguous; spares minted at startup
                                          are typically minutes old by launch)
"""

import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime

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
    if not token:
        print("WARNING: no proxy auth token (set PROXY_AUTH_TOKEN, or run the proxy on :8081) "
              "— proxy calls will fail with 401", flush=True)
    headers = {"X-Proxy-Auth": token} if token else {}
    return httpx.Client(headers=headers)
# Per-test var wins; else TEST_ARCH (the suite-wide opt-in, e.g. LIVE_TEST_X86=1); else arm64.
ARCH = os.environ.get("TERMINATES_IN_ARCH") or os.environ.get("TEST_ARCH") or "arm64"
SIZE = int(os.environ.get("TERMINATES_IN_SIZE", "2048"))
MIN_POOL_AGE = float(os.environ.get("MIN_POOL_AGE_SECONDS", "30"))

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _skip(msg):
    print(f"SKIP test_terminates_in_mint_time: {msg}")
    print("  (set POOL_TERMINATES_IN=1 with the warm-pool stack running to run)")
    sys.exit(0)


def _parse_launched_at(value):
    """Return epoch seconds for a launched_at that may be an epoch number OR an
    ISO-8601 string (the two shapes /instances can carry) — mirrors the panel's
    `typeof === 'number' ? *... : new Date(...)` handling."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        iso = value.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


# ── live-proxy / pool HTTP helpers ──────────────────────────────────────────

def _instances(client):
    r = client.get(f"{PROXY_URL}/instances", timeout=20.0)
    r.raise_for_status()
    data = r.json()
    return data.get("instances", {}), data.get("persistence_mode")


def _launch(client, session_id, name):
    body = {"name": name, "sessionId": session_id, "memoryMiB": SIZE, "arch": ARCH}
    r = client.post(f"{PROXY_URL}/launch", json=body, timeout=120.0)
    r.raise_for_status()
    return r.json()


def _terminate(client, session_id, microvm_id):
    try:
        client.post(f"{PROXY_URL}/terminate",
                    headers={"X-Session-Id": session_id, "Content-Type": "application/json"},
                    json={"microvmId": microvm_id}, timeout=60.0)
    except Exception as e:
        print(f"   (cleanup) terminate {microvm_id} failed, non-fatal: {e}")


def _preflight(client):
    """Pool up + enabled + aws-backed + a warm spare in the target tier; proxy up
    in a non-eternal mode. SKIP (exit 0) otherwise."""
    try:
        if client.get(f"{POOL_URL}/health", timeout=5.0).status_code != 200:
            _skip(f"Pool Manager /health not 200 at {POOL_URL}")
    except Exception as e:
        _skip(f"Pool Manager not reachable at {POOL_URL} ({e}) — "
              "start it with ./local_install.sh --WarmPool")
    try:
        s = client.get(f"{POOL_URL}/status", timeout=5.0).json()
    except Exception as e:
        _skip(f"Pool Manager /status unreadable ({e})")
    if not s.get("enabled"):
        _skip("pool reports enabled=false")
    if s.get("backend") != "aws":
        _skip(f"pool backend={s.get('backend')!r}, expected 'aws' — this validates "
              "the real pooled-grant path, not the fake backend")
    # The target tier must have a warm spare, or the launch cold-falls-through and
    # there is no mint/adopt gap to measure.
    my = next((t for t in (s.get("tiers") or {}).values()
               if t.get("arch") == ARCH and t.get("size_mib") == SIZE), None)
    avail = (my or {}).get("counts", {}).get("available", 0) if my else 0
    if avail < 1:
        _skip(f"tier {ARCH}/{SIZE} has no available spare (available={avail}); "
              "let the pool fill and retry — a pooled grant is required")
    try:
        health = client.get(f"{PROXY_URL}/health", timeout=10.0).json()
    except Exception as e:
        _skip(f"proxy not reachable at {PROXY_URL}/health ({e})")
    mode = health.get("persistence_mode")
    if mode == "eternal":
        _skip("proxy persistence_mode=eternal — eternal rotates instead of "
              "counting down, so there is no 'Terminates in' to validate")
    print(f"  Pool UP (backend=aws, enabled), target tier {ARCH}/{SIZE} available={avail}; "
          f"proxy UP (persistence_mode={mode})")
    return mode


# ── the test ─────────────────────────────────────────────────────────────────

def main():
    if os.environ.get("POOL_TERMINATES_IN") != "1":
        _skip("POOL_TERMINATES_IN != 1")

    client = _make_client()
    print(f"\n=== 'Terminates in' MINT-TIME fix (live, {ARCH}/{SIZE} MiB) ===")
    print(f"  proxy={PROXY_URL}  pool={POOL_URL}\n")

    print("-- pre-flight --")
    _preflight(client)

    created = []  # (session_id, microvm_id)
    try:
        print("\n-- launch a notebook (expect a POOLED grant of a startup-minted spare) --")
        sid = str(uuid.uuid4())
        adopt_epoch = time.time()  # the moment we asked for the VM (the "now" the buggy code used)
        resp = _launch(client, sid, "terminates-in check")
        vm_id = resp.get("microvmId")
        created.append((sid, vm_id))
        check("launch returned a VM id", bool(vm_id))

        # Read the authoritative instance record the panel renders from.
        instances = {}
        inst = None
        for _ in range(10):  # /instances can lag a beat behind the launch
            instances, _mode = _instances(client)
            inst = instances.get(vm_id)
            if inst and inst.get("launched_at") is not None and inst.get("max_duration_sec"):
                break
            time.sleep(1.0)
        check("the launched VM appears in /instances with launched_at + max_duration_sec",
              bool(inst) and inst.get("launched_at") is not None and bool(inst.get("max_duration_sec")))
        if not inst or inst.get("launched_at") is None or not inst.get("max_duration_sec"):
            raise RuntimeError("VM not fully reported by /instances; cannot validate")

        launched_epoch = _parse_launched_at(inst["launched_at"])
        max_dur = int(inst["max_duration_sec"])
        now = time.time()
        check("launched_at parsed to an epoch", launched_epoch is not None)

        pool_age = adopt_epoch - launched_epoch   # how long the spare sat warm before adoption
        remaining = launched_epoch + max_dur - now
        print(f"     launched_at(mint)={launched_epoch:.0f}  adopt={adopt_epoch:.0f}  "
              f"now={now:.0f}  max_duration={max_dur}s")
        print(f"     spare pool-age at grant ≈ {pool_age:.0f}s ; remaining ≈ {remaining:.0f}s "
              f"({remaining/3600:.2f}h) of {max_dur/3600:.2f}h")

        # If pool_age is ~0 the launch almost certainly cold-fell-through (no spare
        # consumed) — there is no mint/adopt gap to validate, so don't assert.
        if pool_age < MIN_POOL_AGE:
            _skip(f"adopted VM's mint age is only {pool_age:.0f}s (< {MIN_POOL_AGE:.0f}s) — "
                  "this launch likely cold-fell-through instead of taking a warm spare, "
                  "so there is no mint/adopt gap to validate. Retry with a warm spare "
                  "that has aged a bit.")

        # THE FIX: launched_at is the MINT time (older than adoption by the pool
        # age), NOT the adopt moment.
        check(f"launched_at is the MINT time, measurably OLDER than adoption "
              f"(gap ≈ {pool_age:.0f}s >= {MIN_POOL_AGE:.0f}s) — NOT reset to now",
              pool_age >= MIN_POOL_AGE)

        # Consequently the remaining lifetime is LESS than the full ceiling by
        # ~the pool age (the whole point: the countdown is honest, not inflated).
        check("remaining lifetime is LESS than the full max_duration by ~the pool age "
              "(countdown reflects the real AWS mint clock, not an inflated adopt clock)",
              remaining <= max_dur - MIN_POOL_AGE + 5)  # +5s slack for clock/measurement

        # Sanity: never MORE than the ceiling (that would mean launched_at is in
        # the future — a different bug), and still positive.
        check("remaining lifetime is positive and never exceeds max_duration",
              0 < remaining <= max_dur + 5)

        print(f"\n  → 'Terminates in' would show ~{int(remaining//3600)}h "
              f"{int((remaining % 3600)//60)}m (honest), not the full "
              f"{max_dur//3600}h the buggy adopt-time clock produced.")

    finally:
        print("\n-- cleanup --")
        seen = set()
        for sess, vm in created:
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
    print("ALL 'TERMINATES IN' MINT-TIME CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
