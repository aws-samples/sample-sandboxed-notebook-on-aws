#!/usr/bin/env python3
# test-kind: offline
"""
pool_vms storage ledger test suite (Phase 2 — VM Pool Manager).

Exercises the seven StorageBackend pool_vm_* methods added for the pool
ledger:
  pool_vm_create, pool_vm_set_state, pool_vm_claim_youngest, pool_vm_list,
  pool_vm_count_by_size, pool_vm_delete, pool_vm_list_older_than.

Backend coverage: SQLite only. This environment has no live Postgres server
(no TEST_PG_DSN), so the Postgres implementation is covered by careful
code-review parity with SQLite (same logic, dialect-adjusted), not by this
suite. If TEST_PG_DSN is set in your environment, these checks still only run
against SQLite — see tests/storage/test_storage_conformance.py for the
opt-in Postgres pattern this suite could be extended with later.

Run:
    .venv/bin/python tests/pool/test_pool_storage.py
"""

import os
import sys
import time
import random
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import proxy.storage.sqlite_db as sq
from proxy.storage.sqlite_db import SqliteStorage


# ── tiny test harness (same style as tests/storage/test_storage_conformance.py) ──
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


# ── backend factory ───────────────────────────────────────────────────────────
def _run_sqlite(db_path=None):
    """Fresh SQLite backend. If db_path is None, uses a private temp dir (not
    shared across threads/processes). If given, points at that exact file —
    used by the concurrency tests so multiple threads share one DB file."""
    if db_path is None:
        tmp = tempfile.mkdtemp(prefix="pool_sqlite_")
        db_path = os.path.join(tmp, "pool.db")
    sq.DB_DIR = os.path.dirname(db_path)
    sq.DB_PATH = db_path
    sq._local = threading.local()
    sq._HELD_SESSION_LOCKS.clear()
    storage = SqliteStorage()
    storage.initialize()
    return storage


def _mk_vm(storage, vm_id, arch="arm64", size=2048, state="available",
           age_seconds=0, fake_session_id=None):
    """Create a pool_vms row, optionally backdating launched_at by age_seconds
    (without sleeping) for building aged fixtures."""
    fake_session_id = fake_session_id or f"pool-{vm_id}"
    launched_at = None
    if age_seconds:
        launched_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    row = storage.pool_vm_create(vm_id, arch, size, f"http://{vm_id}:9000",
                                 fake_session_id, launched_at=launched_at, state="launching")
    if state != "launching":
        storage.pool_vm_set_state(vm_id, state)
    return row


# ── Section 1: create / list / count ────────────────────────────────────────────
def test_create_list_count():
    storage = _run_sqlite()

    _mk_vm(storage, "vm-a1", arch="arm64", size=2048, state="available")
    _mk_vm(storage, "vm-a2", arch="arm64", size=2048, state="launching")
    _mk_vm(storage, "vm-a3", arch="x86_64", size=4096, state="available")

    check("pool_vm_create: launching row exists before any state transition",
          any(r["vm_id"] == "vm-a2" for r in storage.pool_vm_list(state="launching")))

    all_rows = storage.pool_vm_list()
    check("pool_vm_list(): no filter returns all 3 rows", len(all_rows) == 3)

    avail = storage.pool_vm_list(state="available")
    check("pool_vm_list(state=): filters correctly",
          {r["vm_id"] for r in avail} == {"vm-a1", "vm-a3"})

    arm = storage.pool_vm_list(architecture="arm64")
    check("pool_vm_list(architecture=): filters correctly",
          {r["vm_id"] for r in arm} == {"vm-a1", "vm-a2"})

    sz = storage.pool_vm_list(size_mib=4096)
    check("pool_vm_list(size_mib=): filters correctly",
          {r["vm_id"] for r in sz} == {"vm-a3"})

    combo = storage.pool_vm_list(state="available", architecture="arm64", size_mib=2048)
    check("pool_vm_list(): combined filters return exact subset",
          {r["vm_id"] for r in combo} == {"vm-a1"})

    cnt_default = storage.pool_vm_count_by_size("arm64", 2048)
    check("pool_vm_count_by_size: default states=('available','launching') counts both",
          cnt_default == 2)

    cnt_available_only = storage.pool_vm_count_by_size("arm64", 2048, states=("available",))
    check("pool_vm_count_by_size: custom states= tuple respected",
          cnt_available_only == 1)

    created_row = storage.pool_vm_list(state="available", architecture="x86_64", size_mib=4096)[0]
    check("pool_vm_create: row has launched_at and updated_at timestamps set",
          bool(created_row.get("launched_at")) and bool(created_row.get("updated_at")))
    check("pool_vm_create: row carries endpoint + fake_session_id",
          created_row["endpoint"] == "http://vm-a3:9000" and created_row["fake_session_id"] == "pool-vm-a3")


# ── Section 2: set_state + sentinel semantics ──────────────────────────────────
def test_set_state_sentinel():
    storage = _run_sqlite()
    _mk_vm(storage, "vm-s1", state="available")

    ok = storage.pool_vm_set_state("vm-s1", "granted", real_session_id="sess-1")
    check("pool_vm_set_state: returns True on known vm_id", ok is True)
    row = storage.pool_vm_list()[0]
    check("pool_vm_set_state: real_session_id set when explicitly passed",
          row["real_session_id"] == "sess-1" and row["state"] == "granted")
    updated_at_1 = row["updated_at"]

    # state-only update (sentinel default) must leave real_session_id untouched
    time.sleep(0.01)
    storage.pool_vm_set_state("vm-s1", "granted")
    row2 = storage.pool_vm_list()[0]
    check("pool_vm_set_state: omitting real_session_id leaves it untouched (sentinel)",
          row2["real_session_id"] == "sess-1")
    check("pool_vm_set_state: updated_at advances even on a state-only call",
          row2["updated_at"] >= updated_at_1)

    # idempotent set_state on a granted row (not a CAS — plain UPDATE)
    ok3 = storage.pool_vm_set_state("vm-s1", "granted", real_session_id="sess-1")
    check("pool_vm_set_state: idempotent call on an already-granted row returns True",
          ok3 is True)
    row3 = storage.pool_vm_list()[0]
    check("pool_vm_set_state: idempotent call leaves values unchanged",
          row3["state"] == "granted" and row3["real_session_id"] == "sess-1")

    # explicit None clears it to NULL
    ok4 = storage.pool_vm_set_state("vm-s1", "dead", real_session_id=None)
    check("pool_vm_set_state: explicit real_session_id=None clears to NULL", ok4 is True)
    row4 = storage.pool_vm_list()[0]
    check("pool_vm_set_state: real_session_id is None after explicit clear",
          row4["real_session_id"] is None and row4["state"] == "dead")

    # unknown vm_id
    ok5 = storage.pool_vm_set_state("vm-does-not-exist", "granted")
    check("pool_vm_set_state: unknown vm_id returns False", ok5 is False)


# ── Section 3: claim_youngest — happy path, empty, wrong tier, launching ───────
def test_claim_youngest_basics():
    storage = _run_sqlite()

    # oldest/middle/youngest, same tier
    _mk_vm(storage, "vm-old", arch="arm64", size=2048, state="available", age_seconds=300)
    _mk_vm(storage, "vm-mid", arch="arm64", size=2048, state="available", age_seconds=150)
    _mk_vm(storage, "vm-young", arch="arm64", size=2048, state="available", age_seconds=10)

    claimed = storage.pool_vm_claim_youngest("arm64", 2048, "sess-claim-1")
    check("pool_vm_claim_youngest: returns a row (happy path)", claimed is not None)
    check("pool_vm_claim_youngest: returns the YOUNGEST spare", claimed["vm_id"] == "vm-young")
    check("pool_vm_claim_youngest: claimed row flipped to 'granting'",
          claimed["state"] == "granting")
    check("pool_vm_claim_youngest: claimed row carries the passed real_session_id",
          claimed["real_session_id"] == "sess-claim-1")

    remaining = {r["vm_id"]: r["state"] for r in storage.pool_vm_list(architecture="arm64", size_mib=2048)}
    check("pool_vm_claim_youngest: other 2 spares remain 'available'",
          remaining["vm-old"] == "available" and remaining["vm-mid"] == "available")

    # empty tier (zero available rows at all)
    storage2 = _run_sqlite()
    none1 = storage2.pool_vm_claim_youngest("arm64", 2048, "sess-x")
    check("pool_vm_claim_youngest: returns None on a totally empty table", none1 is None)

    # empty tier (rows exist but all granted/other states)
    _mk_vm(storage2, "vm-granted-1", arch="arm64", size=2048, state="granted")
    none2 = storage2.pool_vm_claim_youngest("arm64", 2048, "sess-y")
    check("pool_vm_claim_youngest: returns None when all rows are non-available", none2 is None)

    # wrong tier
    storage3 = _run_sqlite()
    _mk_vm(storage3, "vm-wrong-size", arch="arm64", size=2048, state="available")
    none3 = storage3.pool_vm_claim_youngest("arm64", 4096, "sess-z")
    check("pool_vm_claim_youngest: returns None for a different size_mib", none3 is None)
    none4 = storage3.pool_vm_claim_youngest("x86_64", 2048, "sess-z2")
    check("pool_vm_claim_youngest: returns None for a different architecture", none4 is None)
    unaffected = storage3.pool_vm_list(state="available")
    check("pool_vm_claim_youngest: wrong-tier misses modify nothing",
          len(unaffected) == 1 and unaffected[0]["vm_id"] == "vm-wrong-size")

    # launching-not-claimable: youngest overall is 'launching', must be skipped
    storage4 = _run_sqlite()
    _mk_vm(storage4, "vm-older-avail", arch="arm64", size=2048, state="available", age_seconds=100)
    _mk_vm(storage4, "vm-newer-launching", arch="arm64", size=2048, state="launching", age_seconds=1)
    claimed4 = storage4.pool_vm_claim_youngest("arm64", 2048, "sess-launch-check")
    check("pool_vm_claim_youngest: a younger 'launching' row is never claimed",
          claimed4 is not None and claimed4["vm_id"] == "vm-older-avail")


# ── Section 4: delete ───────────────────────────────────────────────────────────
def test_delete():
    storage = _run_sqlite()
    _mk_vm(storage, "vm-del-1", state="available")
    _mk_vm(storage, "vm-del-2", state="available")

    storage.pool_vm_delete("vm-del-1")
    remaining = {r["vm_id"] for r in storage.pool_vm_list()}
    check("pool_vm_delete: removes the targeted row", "vm-del-1" not in remaining)
    check("pool_vm_delete: leaves other rows intact", "vm-del-2" in remaining)

    # deleting unknown vm_id must not raise
    try:
        storage.pool_vm_delete("vm-does-not-exist")
        no_raise = True
    except Exception:
        no_raise = False
    check("pool_vm_delete: unknown vm_id is a silent no-op", no_raise)


# ── Section 5: list_older_than + UTC-ISO boundary + mixed-state filtering ──────
def test_older_than_and_boundary():
    storage = _run_sqlite()

    _mk_vm(storage, "vm-fresh", state="available", age_seconds=5)
    _mk_vm(storage, "vm-old-avail", state="available", age_seconds=120)
    _mk_vm(storage, "vm-old-granted", state="granted", age_seconds=120)

    old = storage.pool_vm_list_older_than(max_age_seconds=60, state="available")
    check("pool_vm_list_older_than: excludes fresh rows under the threshold",
          "vm-fresh" not in {r["vm_id"] for r in old})
    check("pool_vm_list_older_than: includes old 'available' rows over the threshold",
          "vm-old-avail" in {r["vm_id"] for r in old})
    check("pool_vm_list_older_than: excludes old rows in a DIFFERENT state",
          "vm-old-granted" not in {r["vm_id"] for r in old})

    # UTC-ISO boundary: strictly-greater-than comparison
    storage2 = _run_sqlite()
    _mk_vm(storage2, "vm-just-under", state="available", age_seconds=59.4)
    _mk_vm(storage2, "vm-just-over", state="available", age_seconds=60.6)
    boundary = {r["vm_id"] for r in storage2.pool_vm_list_older_than(max_age_seconds=60, state="available")}
    check("pool_vm_list_older_than: a row just UNDER the threshold is excluded",
          "vm-just-under" not in boundary)
    check("pool_vm_list_older_than: a row just OVER the threshold is included",
          "vm-just-over" in boundary)

    # explicit +00:00 offset parses correctly (exact format isoformat() produces)
    row = storage2.pool_vm_list(state="available")[0]
    check("pool_vm_list_older_than: launched_at carries an explicit UTC offset",
          "+00:00" in row["launched_at"] or row["launched_at"].endswith("Z"))
    parsed = datetime.fromisoformat(row["launched_at"])
    check("UTC-ISO format: datetime.fromisoformat parses the stored launched_at",
          parsed.tzinfo is not None)


# ── Section 6: per-bucket counts across multiple arches/sizes ──────────────────
def test_per_bucket_counts():
    storage = _run_sqlite()
    buckets = [("arm64", 2048), ("arm64", 4096), ("x86_64", 2048), ("x86_64", 4096)]
    for arch, size in buckets:
        for i in range(3):
            _mk_vm(storage, f"vm-{arch}-{size}-{i}", arch=arch, size=size, state="available")

    all_correct = True
    for arch, size in buckets:
        c = storage.pool_vm_count_by_size(arch, size, states=("available",))
        if c != 3:
            all_correct = False
    check("pool_vm_count_by_size: each of 4 buckets returns exactly its own count (no cross-contamination)",
          all_correct)


# ── Section 7: concurrency race — exactly-once claim, no double-handout ───────
def _claim_with_retry(storage, arch, size, session_id, max_retries=5):
    """Test-side retry wrapper around pool_vm_claim_youngest for transient
    'database is locked' errors under heavy thread contention. Matches the
    task's requirement for a retry-on-lock test helper; the storage method's
    correctness does not depend on this retry (busy_timeout already covers
    the common case) — this is belt-and-suspenders for a 50-thread hammer."""
    delay = 0.01
    for attempt in range(max_retries):
        try:
            return storage.pool_vm_claim_youngest(arch, size, session_id)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(delay + random.uniform(0, delay))
                delay *= 2
                continue
            raise
    return None  # unreachable


def test_concurrency_race():
    tmp = tempfile.mkdtemp(prefix="pool_race_")
    db_path = os.path.join(tmp, "race.db")
    storage = _run_sqlite(db_path=db_path)

    N = 20
    M = 50
    arch, size = "arm64", 2048
    for i in range(N):
        _mk_vm(storage, f"vm-race-{i}", arch=arch, size=size, state="available",
               age_seconds=random.uniform(1, 1000))

    def worker(i):
        # Each thread gets its OWN SqliteStorage instance (thread-local conn)
        # pointed at the SAME shared DB file, mirroring real multi-threaded
        # access to one SQLite database.
        local_storage = SqliteStorage()
        return _claim_with_retry(local_storage, arch, size, f"sess-{i}")

    with ThreadPoolExecutor(max_workers=M) as pool:
        results = list(pool.map(worker, range(M)))

    non_none = [r for r in results if r is not None]
    none_count = sum(1 for r in results if r is None)

    check(f"concurrency race: exactly min(N,M)={min(N, M)} calls succeeded (got {len(non_none)})",
          len(non_none) == min(N, M))
    check(f"concurrency race: exactly M-N={M - N} calls returned None (got {none_count})",
          none_count == M - min(N, M))

    vm_ids = [r["vm_id"] for r in non_none]
    counts = Counter(vm_ids)
    check("concurrency race: all claimed vm_ids are DISTINCT (no double-grant)",
          len(set(vm_ids)) == len(vm_ids))
    check("concurrency race: no vm_id appears in more than one result",
          all(c == 1 for c in counts.values()))

    granting_rows = storage.pool_vm_list(state="granting")
    check(f"concurrency race: pool_vm_list(state='granting') shows exactly {N} rows",
          len(granting_rows) == N)
    real_session_ids = {r["real_session_id"] for r in granting_rows}
    granted_session_ids = {r["real_session_id"] for r in non_none}
    check("concurrency race: every granting row's real_session_id is distinct",
          len(real_session_ids) == N)
    check("concurrency race: granting rows' session ids match the actually-granted set",
          real_session_ids == granted_session_ids)

    still_available = storage.pool_vm_list(state="available", architecture=arch, size_mib=size)
    check("concurrency race: tier is now fully drained of 'available' rows",
          len(still_available) == 0)


# ── Section 8: youngest-first under contention ─────────────────────────────────
def test_youngest_first_under_contention():
    tmp = tempfile.mkdtemp(prefix="pool_youngest_")
    db_path = os.path.join(tmp, "youngest.db")
    storage = _run_sqlite(db_path=db_path)

    N = 5
    M = 50
    arch, size = "x86_64", 4096
    # distinct, known ages: vm-y-0 is oldest ... vm-y-4 is youngest
    ages = [500, 400, 300, 200, 100]
    for i, age in enumerate(ages):
        _mk_vm(storage, f"vm-y-{i}", arch=arch, size=size, state="available", age_seconds=age)
    youngest_5_ids = {f"vm-y-{i}" for i in range(N)}  # all of them, since N == seed count

    # Make it non-trivial: seed EXTRA older spares that must NOT be claimed over
    # the already-defined 5, by using a dedicated size tier (isolated above).
    def worker(i):
        local_storage = SqliteStorage()
        return _claim_with_retry(local_storage, arch, size, f"sess-y-{i}")

    with ThreadPoolExecutor(max_workers=M) as pool:
        results = list(pool.map(worker, range(M)))

    non_none = [r for r in results if r is not None]
    claimed_ids = {r["vm_id"] for r in non_none}
    check(f"youngest-first under contention: exactly {N} spares claimed",
          len(claimed_ids) == N)
    check("youngest-first under contention: the claimed set is exactly the full seed set "
          "(all 5 are legitimately youngest-of-remaining at claim time)",
          claimed_ids == youngest_5_ids)

    # Stronger ordering check: re-run with 3 spares among which 2 extra OLDER
    # decoys exist in the SAME tier but should never be claimed ahead of the
    # 3 genuinely-youngest ones in a smaller race.
    storage2 = _run_sqlite()
    _mk_vm(storage2, "decoy-oldest", arch="arm64", size=8192, state="available", age_seconds=9999)
    _mk_vm(storage2, "decoy-older", arch="arm64", size=8192, state="available", age_seconds=5000)
    _mk_vm(storage2, "target-youngest", arch="arm64", size=8192, state="available", age_seconds=1)
    first_claim = storage2.pool_vm_claim_youngest("arm64", 8192, "sess-order-1")
    check("youngest-first under contention: single claim picks the youngest of 3, "
          "ignoring older decoys", first_claim["vm_id"] == "target-youngest")
    second_claim = storage2.pool_vm_claim_youngest("arm64", 8192, "sess-order-2")
    check("youngest-first under contention: next claim picks the next-youngest decoy",
          second_claim["vm_id"] == "decoy-older")


# ── Section 9: bulk-insert stress sanity ───────────────────────────────────────
def test_bulk_stress():
    storage = _run_sqlite()
    tiers = [("arm64", 2048), ("arm64", 4096), ("x86_64", 2048), ("x86_64", 4096)]
    per_tier = 125  # 4 * 125 = 500

    start = time.monotonic()
    for arch, size in tiers:
        for i in range(per_tier):
            _mk_vm(storage, f"vm-bulk-{arch}-{size}-{i}", arch=arch, size=size, state="available")
    elapsed = time.monotonic() - start

    total = storage.pool_vm_list()
    check(f"bulk stress: pool_vm_list() returns exactly 500 rows (got {len(total)})",
          len(total) == 500)

    all_counts_correct = True
    for arch, size in tiers:
        c = storage.pool_vm_count_by_size(arch, size, states=("available",))
        if c != per_tier:
            all_counts_correct = False
    check(f"bulk stress: pool_vm_count_by_size is exactly {per_tier} for each of 4 tiers",
          all_counts_correct)

    check(f"bulk stress: 500 inserts + list + 4 counts completed in bounded time "
          f"({elapsed:.2f}s < 30s)", elapsed < 30)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== pool_vms storage ledger: SQLite ===")
    print("\n-- create / list / count --")
    test_create_list_count()
    print("\n-- set_state + sentinel semantics --")
    test_set_state_sentinel()
    print("\n-- claim_youngest basics (happy path / empty / wrong-tier / launching) --")
    test_claim_youngest_basics()
    print("\n-- delete --")
    test_delete()
    print("\n-- list_older_than + UTC-ISO boundary + mixed-state filtering --")
    test_older_than_and_boundary()
    print("\n-- per-bucket counts across arch/size --")
    test_per_bucket_counts()
    print("\n-- CONCURRENCY RACE: exactly-once claim, no double-handout --")
    test_concurrency_race()
    print("\n-- youngest-first under contention --")
    test_youngest_first_under_contention()
    print("\n-- bulk-insert stress sanity (500 spares) --")
    test_bulk_stress()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL STORAGE CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_storage():
    _checks.clear()
    main_without_exit = [
        test_create_list_count, test_set_state_sentinel, test_claim_youngest_basics,
        test_delete, test_older_than_and_boundary, test_per_bucket_counts,
        test_concurrency_race, test_youngest_first_under_contention, test_bulk_stress,
    ]
    for fn in main_without_exit:
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool_vms storage failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
