#!/usr/bin/env python3
# test-kind: offline
"""
Pooled-grant SUSPENDED cost inheritance (interpretation A).

When the warm pool grants a pre-booted spare, the VM sat SUSPENDED (snapshot
storage — real AWS cost) from its MINT time until the grant. The granted VM must
INHERIT that mint->grant SUSPENDED interval so its cost history starts at mint
(mostly suspended, now running), consistent with the launched_at fix that already
makes 'Launched'/'Terminates in' reflect mint time.

This suite proves the fix at two levels:

  1. STORAGE/VIEW: vm_session_create(..., prior_suspended_since=<mint_iso>) seeds
     TWO vm_state_log rows (NULL->SUSPENDED at mint, SUSPENDED->RUNNING at now), so
     v_vm_sessions (vm_cost_quantities) reconstructs suspended_secs ≈ (grant - mint)
     and a small running_secs. The COLD-launch call (no prior_suspended_since) stays
     unchanged: one NULL->RUNNING row, suspended_secs == 0.

  2. IN-MEMORY: CostTracker.record_prior_suspended(...) seeds the in-memory record
     so get_cost()'s in-memory fallback also reports suspended_secs > 0.

Backend coverage: SQLite only (no live Postgres in this env — the Postgres
implementation is kept in code-review parity, same as test_pool_storage.py).

Run:
    .venv/bin/python tests/pool/test_pooled_suspended_cost.py
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite
from proxy.platform.cost_tracker import CostTracker


# ── tiny test harness (same style as the other pool tests) ──────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _state_log_states(storage, microvm_id):
    """Ordered list of new_state values in the VM's state log (id order)."""
    return [e["new_state"] for e in storage.vm_state_log_get(microvm_id)]


# ── Section 1: STORAGE/VIEW — pooled grant seeds a mint->grant SUSPENDED interval ─
def test_pooled_grant_seeds_suspended_interval():
    storage = _run_sqlite()

    mint = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    created = storage.vm_session_create(
        "vm-pooled", session_id="s-pooled", memory_mib=2048,
        launched_at=mint, prior_suspended_since=mint,
    )
    check("pooled grant: vm_sessions row is RUNNING with launched_at = mint",
          created["state"] == "RUNNING" and created["launched_at"] == mint)

    states = _state_log_states(storage, "vm-pooled")
    check("pooled grant: state log has exactly two rows, SUSPENDED then RUNNING (id order)",
          states == ["SUSPENDED", "RUNNING"])

    q = storage.vm_cost_quantities("vm-pooled")
    check("pooled grant: v_vm_sessions returns a row", q is not None)
    check("pooled grant: suspended_secs ≈ 3600 (the pool residency)",
          q is not None and abs(q["suspended_secs"] - 3600) <= 5)
    check("pooled grant: running_secs is small (grant onward, ~0 just after adopt)",
          q is not None and q["running_secs"] <= 5)

    # Dollar math via the single pricing function: suspended cost must be > 0.
    # The pooled grant log is SUSPENDED then RUNNING → resume_count=1 (one
    # SUSPENDED->RUNNING read), suspend_count=1 (one into-SUSPENDED write).
    from proxy.platform.cost_tracker import price_quantities
    check("pooled grant: view exposes resume_count/suspend_count",
          q.get("resume_count") == 1 and q.get("suspend_count") == 1)
    cost = price_quantities(q["running_secs"], q["suspended_secs"],
                            q["burst_mb_seconds"], q["memory_mib"], q.get("architecture"),
                            resume_count=q["resume_count"], suspend_count=q["suspend_count"])
    check("pooled grant: priced suspended_cost_usd > 0 (snapshot storage billed)",
          cost["suspended_cost_usd"] > 0)
    check("pooled grant: snapshot_read_cost_usd > 0 (one SUSPENDED->RUNNING read)",
          cost["snapshot_read_cost_usd"] > 0)
    check("pooled grant: snapshot_write_cost_usd >= 0",
          cost["snapshot_write_cost_usd"] >= 0)


# ── Section 2: STORAGE/VIEW — cold launch is UNCHANGED (invariant) ─────────────
def test_cold_launch_unchanged():
    storage = _run_sqlite()

    before = datetime.now(timezone.utc).timestamp()
    created = storage.vm_session_create("vm-cold", session_id="s-cold", memory_mib=2048)
    check("cold launch: launched_at defaults to ~now",
          created["launched_at"] is not None
          and datetime.fromisoformat(created["launched_at"]).timestamp() >= before - 1.0)

    states = _state_log_states(storage, "vm-cold")
    check("cold launch: state log has exactly ONE row, NULL->RUNNING",
          states == ["RUNNING"])

    q = storage.vm_cost_quantities("vm-cold")
    check("cold launch: suspended_secs == 0 (no spurious interval)",
          q is not None and q["suspended_secs"] == 0)


# ── Section 3: STORAGE/VIEW — bad prior_suspended_since degrades to cold launch ─
def test_bad_prior_suspended_degrades_gracefully():
    storage = _run_sqlite()
    now_iso = datetime.now(timezone.utc).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    cases = [
        ("none", None),
        ("empty-string", ""),
        ("unparseable", "not-a-timestamp"),
        ("not-older-than-now (future)", future),
    ]
    for label, bad in cases:
        vm_id = f"vm-bad-{label.split()[0]}"
        storage.vm_session_create(vm_id, session_id=f"s-{vm_id}", memory_mib=2048,
                                  launched_at=now_iso, prior_suspended_since=bad)
        states = _state_log_states(storage, vm_id)
        check(f"bad prior_suspended_since ({label}): single NULL->RUNNING row",
              states == ["RUNNING"])
        q = storage.vm_cost_quantities(vm_id)
        check(f"bad prior_suspended_since ({label}): suspended_secs == 0",
              q is not None and q["suspended_secs"] == 0)


# ── Section 4: IN-MEMORY — CostTracker.record_prior_suspended seeds the record ──
def test_cost_tracker_record_prior_suspended():
    tracker = CostTracker()
    mint_epoch = time.time() - 3600  # 1h ago
    tracker.record_prior_suspended("vm-mem", mint_epoch, memory_mib=2048,
                                   architecture="arm64")

    # Compute directly from the in-memory record (bypass the view path).
    record = tracker._records["vm-mem"]
    states = [t.state for t in record.transitions]
    check("in-memory: record seeded with SUSPENDED then RUNNING",
          states == ["SUSPENDED", "RUNNING"])
    cost = record.compute()
    check("in-memory: compute() suspended_secs ≈ 3600",
          abs(cost["suspended_secs"] - 3600) <= 5)
    check("in-memory: compute() running_secs is small",
          cost["running_secs"] <= 5)
    check("in-memory: suspended_cost_usd > 0", cost["suspended_cost_usd"] > 0)

    # Bad mint epoch degrades to a plain RUNNING record (no suspended interval).
    tracker2 = CostTracker()
    tracker2.record_prior_suspended("vm-badmem", time.time() + 100,
                                    memory_mib=2048, architecture="arm64")
    states2 = [t.state for t in tracker2._records["vm-badmem"].transitions]
    check("in-memory: a future/bad mint epoch degrades to single RUNNING",
          states2 == ["RUNNING"])
    cost2 = tracker2._records["vm-badmem"].compute()
    check("in-memory: degraded record has suspended_secs == 0",
          cost2["suspended_secs"] == 0)


# ── Section 5: an x86_64 pooled grant is priced at the x86_64 rates ──────────
# The per-arch table as scripts/config.sh exports it (MICROVM_PRICING_JSON).
# Patched onto the module rather than reloading it from os.environ, so the
# other sections' module state is untouched.
_TWO_ARCH_PRICING = {
    "arm64": {"vcpu_per_sec": 0.0000276944, "memory_per_gb_sec": 0.0000036667,
              "snapshot_per_gb_month": 0.08, "snapshot_read_per_gb": 0.00155,
              "snapshot_write_per_gb": 0.00380},
    "x86_64": {"vcpu_per_sec": 0.0000326557, "memory_per_gb_sec": 0.0000043235,
               "snapshot_per_gb_month": 0.08, "snapshot_read_per_gb": 0.00155,
               "snapshot_write_per_gb": 0.00380},
}


def test_x86_pooled_grant_priced_at_x86_rates():
    import proxy.platform.cost_tracker as ct
    storage = _run_sqlite()
    mint = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    storage.vm_session_create(
        "vm-pooled-x86", session_id="s-pooled-x86", memory_mib=2048,
        architecture="x86_64", launched_at=mint, prior_suspended_since=mint,
    )
    q = storage.vm_cost_quantities("vm-pooled-x86")
    check("x86 pooled grant: the view reports architecture x86_64",
          q is not None and q.get("architecture") == "x86_64")

    saved = ct._PRICING
    ct._PRICING = _TWO_ARCH_PRICING
    try:
        # A fixed 1 h running interval so the rate difference is visible
        # (the real running_secs is ~0 right after adopt).
        x86 = ct.price_quantities(3600, q["suspended_secs"], 0, 2048, q.get("architecture"),
                                  resume_count=q["resume_count"],
                                  suspend_count=q["suspend_count"])
        arm = ct.price_quantities(3600, q["suspended_secs"], 0, 2048, "arm64",
                                  resume_count=q["resume_count"],
                                  suspend_count=q["suspend_count"])
    finally:
        ct._PRICING = saved

    check("x86 pooled grant: priced with the x86_64 vCPU rate",
          x86["rates"]["vcpu_per_sec"] == 0.0000326557)
    check("x86 pooled grant: priced with the x86_64 memory rate",
          x86["rates"]["memory_per_gb_sec"] == 0.0000043235)
    # 2 GB = 1 vCPU: 3600 s x (1 x vCPU rate + 2 x mem rate)
    expected_running = 3600 * (1 * 0.0000326557 + 2 * 0.0000043235)
    check("x86 pooled grant: running cost = 3600 s x (1 vCPU x 0.0000326557 + 2 GB x 0.0000043235)",
          abs(x86["running_cost_usd"] - round(expected_running, 6)) < 1e-6)
    check("x86 pooled grant: the inherited pool residency is still billed (suspended_cost > 0)",
          x86["suspended_cost_usd"] > 0)
    check("x86 pooled grant: running cost is higher than the same VM priced as arm64",
          x86["running_cost_usd"] > arm["running_cost_usd"])


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== Pooled-grant SUSPENDED cost inheritance (interpretation A) ===")
    print("\n-- storage/view: pooled grant seeds a mint->grant SUSPENDED interval --")
    test_pooled_grant_seeds_suspended_interval()
    print("\n-- storage/view: cold launch is UNCHANGED --")
    test_cold_launch_unchanged()
    print("\n-- storage/view: bad prior_suspended_since degrades to cold launch --")
    test_bad_prior_suspended_degrades_gracefully()
    print("\n-- in-memory: CostTracker.record_prior_suspended seeds the record --")
    test_cost_tracker_record_prior_suspended()
    print("\n-- x86_64 pooled grant priced at the x86_64 rates --")
    test_x86_pooled_grant_priced_at_x86_rates()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOLED SUSPENDED COST CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pooled_suspended_cost():
    _checks.clear()
    test_pooled_grant_seeds_suspended_interval()
    test_cold_launch_unchanged()
    test_bad_prior_suspended_degrades_gracefully()
    test_cost_tracker_record_prior_suspended()
    test_x86_pooled_grant_priced_at_x86_rates()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pooled suspended cost failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
