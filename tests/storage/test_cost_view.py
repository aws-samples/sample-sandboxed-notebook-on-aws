#!/usr/bin/env python3
# test-kind: offline
"""
Tests for the v_vm_sessions cost-reconstruction VIEW (offline, SQLite).

Validates that the view's running_secs/suspended_secs (reconstructed in SQL from
vm_state_log) MATCH CostTracker's Python reconstruction from the same log — i.e.
the view is a faithful, centralized replacement for the interval-sum logic. Also
does a rough performance check of the per-session rollup at scale so we can see
how the view behaves before deciding whether to drop the base columns.

Run:
    python3 tests/storage/test_cost_view.py
"""

import os
import sys
import time
import sqlite3
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _fresh():
    import proxy.storage.sqlite_db as sq
    tmp = tempfile.mkdtemp(prefix="costview_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "cv.db")
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    s = sq.SqliteStorage()
    s.initialize()
    return s, sq.DB_PATH


def test_view_matches_cost_tracker():
    print("\n=== v_vm_sessions matches CostTracker reconstruction ===")
    from proxy.platform.cost_tracker import CostTracker
    import datetime as _dt

    storage, db_path = _fresh()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    base = _dt.datetime(2026, 8, 24, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def _seed(vm, session_id, rows, memory_mib=4096, burst=0.0):
        """rows: list of (offset_secs, new_state). Bulk-insert with KNOWN large
        intervals so the view-vs-CostTracker comparison exercises real durations
        (not sub-second sleeps)."""
        conn.execute("INSERT INTO vm_sessions (microvm_id, session_id, memory_mib, state, burst_mb_seconds) VALUES (?,?,?,?,?)",
                     (vm, session_id, memory_mib, rows[-1][1], burst))
        prev = None
        for off, st in rows:
            ts = (base + _dt.timedelta(seconds=off)).isoformat()
            conn.execute("INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?,?,?,?)",
                         (vm, ts, prev, st))
            prev = st
        conn.commit()

    # vm-A TERMINATED: RUNNING@0 -> SUSPENDED@100 -> RUNNING@130 -> SUSPENDED@180 -> TERMINATED@250
    #   running = 100 + 50 = 150 ; suspended = 30 + 70 = 100
    _seed("vm-A", "S1",
          [(0, "RUNNING"), (100, "SUSPENDED"), (130, "RUNNING"), (180, "SUSPENDED"), (250, "TERMINATED")],
          burst=5000.0)
    # vm-C NOT terminated (last state RUNNING@300): CostTracker.load_from_db loads
    #   only NON-terminated VMs, so use this one for the direct parity check.
    #   Fixed intervals up to the last row; the final RUNNING tail runs to now — we
    #   compare only the settled portion by keeping the tail tiny relative to it.
    _seed("vm-C", "S1",
          [(0, "RUNNING"), (100, "SUSPENDED"), (130, "RUNNING")], memory_mib=2048)

    view_rows = {r["microvm_id"]: r for r in
                 conn.execute("SELECT * FROM v_vm_sessions").fetchall()}
    ct = CostTracker()
    ct.load_from_db(storage)

    # --- Parity on a loadable (non-terminated) VM: both reconstruct from the SAME
    # log, so the SETTLED intervals must agree. vm-C settled: running=100 (0->100),
    # suspended=30 (100->130); the open RUNNING tail (130->now) is identical in both
    # (both measure to ~now), so totals match within ±2s.
    v = view_rows["vm-C"]; c = ct.get_cost("vm-C")
    check(f"[vm-C] running_secs view({v['running_secs']}) ≈ CostTracker({c['running_secs']}) (settled 100 + live tail)",
          abs(v["running_secs"] - c["running_secs"]) <= 2 and v["running_secs"] >= 100)
    check(f"[vm-C] suspended_secs view({v['suspended_secs']}) == CostTracker({c['suspended_secs']}) == 30",
          v["suspended_secs"] == 30 and abs(c["suspended_secs"] - 30) <= 1)

    # --- The view ALSO covers TERMINATED VMs (which CostTracker.load_from_db skips,
    # since it loads only active VMs). This is exactly why the view is the better
    # source for historical / per-session totals. Exact fixed-interval math:
    va = view_rows["vm-A"]
    check(f"[vm-A TERMINATED] view running_secs == 150 (CostTracker skips it)", va["running_secs"] == 150)
    check(f"[vm-A TERMINATED] view suspended_secs == 100", va["suspended_secs"] == 100)
    check("[vm-A] burst_mb_seconds passthrough", abs(va["burst_mb_seconds"] - 5000.0) < 0.5)
    # vm-A log: RUNNING, SUSPENDED, RUNNING, SUSPENDED, TERMINATED →
    #   resume_count = 2 (into-RUNNING rows), suspend_count = 2 (into-SUSPENDED rows).
    check(f"[vm-A] view resume_count == 2 (got {va['resume_count']})", va["resume_count"] == 2)
    check(f"[vm-A] view suspend_count == 2 (got {va['suspend_count']})", va["suspend_count"] == 2)

    # A VM with NO state log rows still appears (LEFT JOIN), zeros for time.
    storage.vm_session_create("vm-empty", session_id="S1", memory_mib=1024)
    conn2 = sqlite3.connect(db_path); conn2.row_factory = sqlite3.Row
    # remove its auto RUNNING log to simulate a bare row
    conn2.execute("DELETE FROM vm_state_log WHERE microvm_id = 'vm-empty'"); conn2.commit()
    r = conn2.execute("SELECT * FROM v_vm_sessions WHERE microvm_id='vm-empty'").fetchone()
    check("[vm-empty] present with 0 running/suspended (LEFT JOIN)",
          r is not None and r["running_secs"] == 0 and r["suspended_secs"] == 0)
    check("[vm-empty] resume_count/suspend_count COALESCE to 0 (LEFT JOIN)",
          r is not None and r["resume_count"] == 0 and r["suspend_count"] == 0)


def test_per_session_rollup_and_perf():
    print("\n=== per-session rollup via view + rough perf ===")
    storage, db_path = _fresh()

    # Simulate a session recovered many times: N VMs, each a short lifecycle,
    # all sharing session_id 'BIG'. Plus noise: other sessions.
    N = 300
    import datetime as _dt
    conn = sqlite3.connect(db_path)
    base = _dt.datetime(2026, 8, 24, 12, 0, 0, tzinfo=_dt.timezone.utc)
    # Bulk insert directly for speed (bypass sleeps): 3 transitions per VM.
    for i in range(N):
        vm = f"vm-{i}"
        sess = "BIG" if i % 2 == 0 else f"other-{i}"
        conn.execute("INSERT INTO vm_sessions (microvm_id, session_id, memory_mib, state, burst_mb_seconds) VALUES (?,?,?,?,?)",
                     (vm, sess, 4096, "TERMINATED", 100.0 * i))
        t0 = (base + _dt.timedelta(seconds=i * 300)).isoformat()
        t1 = (base + _dt.timedelta(seconds=i * 300 + 100)).isoformat()
        t2 = (base + _dt.timedelta(seconds=i * 300 + 130)).isoformat()
        t3 = (base + _dt.timedelta(seconds=i * 300 + 180)).isoformat()
        conn.executemany("INSERT INTO vm_state_log (microvm_id, timestamp, old_state, new_state) VALUES (?,?,?,?)",
                         [(vm, t0, None, "RUNNING"), (vm, t1, "RUNNING", "SUSPENDED"),
                          (vm, t2, "SUSPENDED", "RUNNING"), (vm, t3, "RUNNING", "TERMINATED")])
    conn.commit()

    # Per-session rollup through the view.
    t = time.perf_counter()
    row = conn.execute(
        "SELECT COUNT(*) AS vms, SUM(running_secs) AS run, SUM(suspended_secs) AS sus, SUM(burst_mb_seconds) AS burst "
        "FROM v_vm_sessions WHERE session_id = ?", ("BIG",)
    ).fetchone()
    elapsed_ms = (time.perf_counter() - t) * 1000

    vms, run, sus, burst = row
    # Each BIG vm: running = 100 + 50 = 150, suspended = 30.
    check(f"rollup vm count = {N//2}", vms == N // 2)
    check(f"rollup running_secs = 150 × {N//2}", run == 150 * (N // 2))
    check(f"rollup suspended_secs = 30 × {N//2}", sus == 30 * (N // 2))
    print(f"    ⏱  per-session rollup over {N} VMs ({N*4} log rows): {elapsed_ms:.1f} ms")
    # Not a hard assertion (machine-dependent), but flag if it's alarmingly slow.
    check(f"rollup completes < 500ms ({elapsed_ms:.1f}ms)", elapsed_ms < 500)

    # EXPLAIN to confirm the session index is used for the base-table filter.
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM v_vm_sessions WHERE session_id = ?", ("BIG",)).fetchall()
    plan_txt = " | ".join(p[-1] for p in plan)
    print(f"    query plan: {plan_txt}")
    check("plan uses idx_vm_sessions_session", "idx_vm_sessions_session" in plan_txt)


def main():
    test_view_matches_cost_tracker()
    test_per_session_rollup_and_perf()
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nCOST VIEW: {passed}/{total} checks passed")
    if passed != total:
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL COST-VIEW CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
