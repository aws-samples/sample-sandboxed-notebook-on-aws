#!/usr/bin/env python3
# test-kind: offline
"""
Snapshot DATA read/write charges — direct parity between the two MIRRORED pricing
functions (proxy platform-side price_quantities and the notebook guest-side
estimate_session_cost). The two must agree byte-for-byte on the new snapshot
read/write arithmetic, and both must fold the new costs into total_cost_usd.

Snapshot data READ  = $0.00155 per GB, billed per transition INTO RUNNING.
Snapshot data WRITE = $0.00380 per GB, billed per transition INTO SUSPENDED.
GB per operation = the VM's memory tier (memory_mib / 1024).

Run:
    .venv/bin/python tests/storage/test_snapshot_rw_cost.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def test_snapshot_rw_parity():
    print("\n=== snapshot read/write parity: price_quantities vs estimate_session_cost ===")
    from proxy.platform.cost_tracker import price_quantities
    from app.notebook.helpers.cost import estimate_session_cost

    # 8192 MiB → 8 GB. resume_count=3, suspend_count=2.
    mem_gb = 8192 / 1024.0
    exp_read = round(3 * mem_gb * 0.00155, 6)   # 3 × 8 × 0.00155 = 0.0372
    exp_write = round(2 * mem_gb * 0.00380, 6)  # 2 × 8 × 0.00380 = 0.0608

    a = price_quantities(0, 0, 0, 8192, resume_count=3, suspend_count=2)
    b = estimate_session_cost(0, 0, 0, 8192, resume_count=3, suspend_count=2)

    check(f"price_quantities snapshot_read_cost_usd == {exp_read} (got {a['snapshot_read_cost_usd']})",
          a["snapshot_read_cost_usd"] == exp_read)
    check(f"price_quantities snapshot_write_cost_usd == {exp_write} (got {a['snapshot_write_cost_usd']})",
          a["snapshot_write_cost_usd"] == exp_write)
    check("estimate_session_cost agrees on snapshot_read_cost_usd",
          b["snapshot_read_cost_usd"] == a["snapshot_read_cost_usd"])
    check("estimate_session_cost agrees on snapshot_write_cost_usd",
          b["snapshot_write_cost_usd"] == a["snapshot_write_cost_usd"])
    check("both report resume_count=3 / suspend_count=2 as ints",
          a["resume_count"] == 3 and a["suspend_count"] == 2
          and b["resume_count"] == 3 and b["suspend_count"] == 2)
    # Both fold the snapshot costs into the total (compute+storage are 0 here).
    check("price_quantities total == read + write (folded in)",
          a["total_cost_usd"] == round(exp_read + exp_write, 6))
    check("estimate_session_cost total == read + write (folded in)",
          b["total_cost_usd"] == round(exp_read + exp_write, 6))


def test_zero_counts_no_charge():
    print("\n=== zero counts → no snapshot charge, keys still present ===")
    from proxy.platform.cost_tracker import price_quantities
    from app.notebook.helpers.cost import estimate_session_cost

    a = price_quantities(100, 0, 0, 4096)
    b = estimate_session_cost(100, 0, 0, 4096)
    for d, label in ((a, "price_quantities"), (b, "estimate_session_cost")):
        check(f"{label}: snapshot read/write default to 0.0",
              d["snapshot_read_cost_usd"] == 0.0 and d["snapshot_write_cost_usd"] == 0.0)
        check(f"{label}: resume_count/suspend_count default to 0",
              d["resume_count"] == 0 and d["suspend_count"] == 0)


def main():
    test_snapshot_rw_parity()
    test_zero_counts_no_charge()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nSNAPSHOT R/W COST: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL SNAPSHOT R/W COST CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
