#!/usr/bin/env python3
# test-kind: offline
"""
Step 2 test: CostTracker.get_cost() / get_session_cost() now read QUANTITIES from
the v_vm_sessions VIEW (single source of truth) and price them in Python. This
verifies the view-backed path returns the correct dollar math and the exact dict
shape the UI consumes, and that the in-memory fallback still works.

Run:
    python3 tests/storage/test_cost_from_view.py
"""

import os
import sys
import datetime as _dt
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _fresh_storage():
    """Fresh temp SQLite AND repoint the shared singleton (get_cost imports it)."""
    import proxy.storage.sqlite_db as sq
    import proxy.storage as storage_pkg
    tmp = tempfile.mkdtemp(prefix="costview2_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "cv2.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    s = sq.SqliteStorage()
    s.initialize()
    storage_pkg.storage = s  # so `from proxy.storage import storage` inside get_cost() hits this
    return s


def _seed(storage, vm, session_id, rows, memory_mib=4096, burst=0.0):
    """rows: list of (offset_secs, new_state) with KNOWN intervals. Bulk-insert."""
    import sqlite3
    import proxy.storage.sqlite_db as sq
    conn = sqlite3.connect(sq.DB_PATH)
    base = _dt.datetime(2026, 8, 24, 12, 0, 0, tzinfo=_dt.timezone.utc)
    conn.execute("INSERT INTO vm_sessions (microvm_id, session_id, memory_mib, state, burst_mb_seconds) VALUES (?,?,?,?,?)",
                 (vm, session_id, memory_mib, rows[-1][1], burst))
    prev = None
    for off, st in rows:
        ts = (base + _dt.timedelta(seconds=off)).isoformat()
        conn.execute("INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?,?,?,?)",
                     (vm, ts, prev, st))
        prev = st
    conn.commit()
    conn.close()


def test_get_cost_from_view():
    print("\n=== get_cost() reads quantities from the view + prices in Python ===")
    from proxy.platform.cost_tracker import CostTracker, price_quantities
    storage = _fresh_storage()

    # vm-A TERMINATED, memory 4096: RUNNING 0->100, SUSPENDED 100->130, RUNNING 130->180, TERMINATED@180
    #   running = 100 + 50 = 150 ; suspended = 30 ; burst = 8192 MB·s
    _seed(storage, "vm-A", "S1",
          [(0, "RUNNING"), (100, "SUSPENDED"), (130, "RUNNING"), (180, "TERMINATED")],
          memory_mib=4096, burst=8192.0)

    ct = CostTracker()  # note: EMPTY in-memory records → proves it reads the view, not memory
    cost = ct.get_cost("vm-A")

    # vm-A log: RUNNING, SUSPENDED, RUNNING, TERMINATED →
    #   resume_count = 2 (two into-RUNNING rows), suspend_count = 1 (one into-SUSPENDED row).
    # Expected dollars = price_quantities on the known quantities, incl. the counts.
    expected = price_quantities(150, 30, 8192.0, 4096, resume_count=2, suspend_count=1)
    check(f"running_secs from view == 150 (got {cost['running_secs']})", cost["running_secs"] == 150)
    check(f"suspended_secs from view == 30 (got {cost['suspended_secs']})", cost["suspended_secs"] == 30)
    check(f"resume_count from view == 2 (got {cost['resume_count']})", cost["resume_count"] == 2)
    check(f"suspend_count from view == 1 (got {cost['suspend_count']})", cost["suspend_count"] == 1)
    check(f"snapshot_read_cost_usd == 2×4GB×0.00155 ({cost['snapshot_read_cost_usd']})",
          cost["snapshot_read_cost_usd"] == round(2 * (4096 / 1024) * 0.00155, 6))
    check(f"snapshot_write_cost_usd == 1×4GB×0.00380 ({cost['snapshot_write_cost_usd']})",
          cost["snapshot_write_cost_usd"] == round(1 * (4096 / 1024) * 0.00380, 6))
    check(f"total_cost_usd matches price_quantities ({cost['total_cost_usd']} == {expected['total_cost_usd']})",
          cost["total_cost_usd"] == expected["total_cost_usd"])
    check(f"burst_cost_usd > 0 (burst carried through: {cost['burst_cost_usd']})", cost["burst_cost_usd"] > 0)
    # UI dict shape
    for key in ("running_secs", "suspended_secs", "running_cost_usd", "suspended_cost_usd",
                "burst_cost_usd", "snapshot_read_cost_usd", "snapshot_write_cost_usd",
                "total_cost_usd", "memory_gb", "burst_mb_seconds", "resume_count", "suspend_count"):
        check(f"cost dict has '{key}'", key in cost)
    check("in-memory records is EMPTY (proves view-sourced)", len(ct._records) == 0)


def test_session_rollup_from_view():
    print("\n=== get_session_cost() rolls up view-backed per-VM costs ===")
    from proxy.platform.cost_tracker import CostTracker
    storage = _fresh_storage()

    # Two VMs that carried the same session (a recovery): sum should be both.
    _seed(storage, "vm-1", "S", [(0, "RUNNING"), (100, "TERMINATED")], memory_mib=2048)   # run 100
    _seed(storage, "vm-2", "S", [(0, "RUNNING"), (60, "SUSPENDED")], memory_mib=2048, burst=0)  # run 60 + live suspend tail

    ct = CostTracker()
    # Snapshot per-VM costs FIRST, then roll up, to minimize the live-suspend-tail
    # drift (vm-2 ends SUSPENDED → its suspended secs tick to now on each read).
    per1 = ct.get_cost("vm-1"); per2 = ct.get_cost("vm-2")
    roll = ct.get_session_cost(["vm-1", "vm-2"])

    # vm-1 running is a fixed terminated interval (100s); vm-2 running is a fixed
    # 60s interval (ended by SUSPENDED). Running is not live → exact-ish (±1s).
    check("session running_secs ≈ vm1+vm2 (±1s)",
          abs(roll["running_secs"] - (per1["running_secs"] + per2["running_secs"])) <= 1)
    # Total includes vm-2's live suspend tail measured at slightly different
    # instants across the calls → allow a small tolerance.
    check("session total_cost_usd ≈ vm1+vm2 (live-tail tolerant)",
          abs(roll["total_cost_usd"] - (per1["total_cost_usd"] + per2["total_cost_usd"])) < 1e-4)
    check("session vm_count == 2", roll["vm_count"] == 2)


def test_fallback_when_view_empty():
    print("\n=== fallback to in-memory when view has no row ===")
    from proxy.platform.cost_tracker import CostTracker
    _fresh_storage()  # fresh empty DB
    ct = CostTracker()
    # Unknown VM, nothing in view or memory → zero-cost dict (not a crash).
    z = ct.get_cost("nonexistent-vm")
    check("unknown VM → zero cost dict", z["total_cost_usd"] == 0.0 and z["running_secs"] == 0)


def main():
    test_get_cost_from_view()
    test_session_rollup_from_view()
    test_fallback_when_view_empty()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nCOST-FROM-VIEW: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL COST-FROM-VIEW CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
