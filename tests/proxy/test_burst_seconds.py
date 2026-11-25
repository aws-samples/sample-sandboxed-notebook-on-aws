#!/usr/bin/env python3
# test-kind: offline
"""
Burst time on the VM card's Cost tab: storage.metrics_get_burst (SQLite) and
proxy/platform/burst_time.py. No network. Cases: no burst, one episode, two
episodes, a big gap, a first sample above the tier (bills nothing, still an
episode), a suspended gap, mem_used_mb of 0/NULL ignored (also as "previous"),
per-VM isolation, and consistency with the real MicroVMCostRecord
(record_burst_sample): same MB-seconds, peak and episodes.
Run: python3 tests/proxy/test_burst_seconds.py
"""
import asyncio
import os
import sys
import tempfile
import threading
import time as _time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from proxy.platform import burst_time  # noqa: E402
from proxy.platform.cost_tracker import MicroVMCostRecord  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


TIER = 2048
BASE = 1_800_000_000.0
MID = "vm-burst"


def _storage():
    import proxy.storage.sqlite_db as sq
    tmp = tempfile.mkdtemp(prefix="burst_sqlite_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "burst.db")
    sq._local = threading.local()
    st = sq.SqliteStorage()
    st.initialize()
    return st


def _feed(st, mid, series):
    for t, u in series:
        st.metrics_record(mid, 10.0, 20.0, u, 30.0, 1000.0, 0, 0, 3, 1.0,
                          recorded_at=BASE + t)


def _tracker(series, tier=TIER):
    rec = MicroVMCostRecord(microvm_id="t", memory_mib=tier)
    real = _time.time
    try:
        for t, u in series:
            _time.time = lambda t=t: BASE + t
            rec.record_burst_sample(u)
    finally:
        _time.time = real
    return rec


def _near(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_cases():
    print("test_cases")
    st = _storage()
    series = {
        "none": [(0, 500), (1, 900), (2, 2048), (3, 100)],
        "one": [(0, 500), (1, 3000), (2, 3000), (3, 3500), (4, 500)],
        "two": [(0, 500), (1, 3000), (2, 2048), (3, 4096), (4, 4096), (5, 100)],
        "gap": [(0, 500), (1, 600), (121, 3048)],
        "first": [(0, 3000), (1, 3000), (2, 100)],
        "susp": [(0, 3000), (1, 1000), (3601, 1000), (3602, 2500)],
        "zero": [(0, 3000), (1, 0), (2, 3000), (3, 3000)],
    }
    for mid, ser in series.items():
        _feed(st, mid, ser)
    _feed(st, "other", [(0, 9000), (1, 9000)])
    g = lambda m: st.metrics_get_burst(m, TIER)  # noqa: E731

    n = g("none")
    check("no burst: burst_samples 0 -> fields {}", n["burst_samples"] == 0
          and burst_time.burst_fields(n, TIER) == {})
    check("unknown VM -> None -> {}", g("vm-nope") is None
          and burst_time.burst_fields(None, TIER) == {})
    o = g("one")
    check("one episode: 3 s, 1 episode", _near(o["burst_seconds"], 3.0)
          and o["burst_episodes"] == 1 and o["peak_mem_mb"] == 3500)
    check("one episode: MB-seconds = 952*1 + 952*1 + 1452*1",
          _near(o["burst_mb_seconds"], 952 + 952 + 1452))
    t = g("two")
    check("two episodes (a drop back to the tier splits them)",
          t["burst_episodes"] == 2 and _near(t["burst_seconds"], 1 + 1 + 1))
    b = g("gap")
    check("big gap billed whole at the newer overage",
          _near(b["burst_seconds"], 120.0) and _near(b["burst_mb_seconds"], 1000 * 120))
    f = g("first")
    check("first sample above the tier: bills 1 s (2nd sample), 1 episode",
          _near(f["burst_seconds"], 1.0) and f["burst_episodes"] == 1)
    only_first = [(0, 3000)]
    _feed(st, "single", only_first)
    s1 = g("single")
    check("lone first sample bills 0 s but counts the episode",
          s1["burst_seconds"] == 0 and s1["burst_episodes"] == 1
          and burst_time.burst_fields(s1, TIER) == {})
    sp = g("susp")
    check("suspended gap: below-tier samples bill nothing; first-above bills 0, "
          "then only the 1 s step above", _near(sp["burst_seconds"], 1.0)
          and sp["burst_episodes"] == 2)
    z = g("zero")
    check("0 MB sample ignored entirely (also as previous): samples 3, 1 episode",
          z["samples"] == 3 and z["burst_episodes"] == 1
          and _near(z["burst_seconds"], 2 + 1))
    check("other VM not counted", g("other")["samples"] == 2 and o["samples"] == 5)
    st.metrics_record("nullmem", 1.0, 1.0, None, 1.0, 1.0, 0, 0, 1, 1.0, recorded_at=BASE)
    check("NULL mem_used_mb only -> None", g("nullmem") is None)
    flds = burst_time.burst_fields(o, TIER)
    check("fields: seconds, peak over tier, episodes",
          flds == {"burst_seconds": 3.0, "burst_peak_over_mb": 1452.0, "burst_episodes": 1})


def test_consistency():
    print("test_consistency")
    st = _storage()
    sets = {
        "a": [(0, 500), (1, 3000), (2, 3000), (3, 3500), (4, 500)],
        "b": [(0, 500), (1, 3000), (2, 2048), (3, 4096), (4, 4096), (5, 100)],
        "c": [(0, 500), (1, 600), (121, 3048), (122, 2000), (200, 2600)],
        "d": [(0, 3000), (1, 3000), (2, 100)],
        "e": [(0, 100), (1, 5000), (7, 5000), (8, 100), (3608, 4000)],
    }
    for mid, ser in sets.items():
        _feed(st, mid, ser)
        rec = _tracker(ser)
        d = st.metrics_get_burst(mid, TIER)
        check(f"[{mid}] MB-seconds equal the tracker's",
              _near(d["burst_mb_seconds"], rec.burst_mb_seconds))
        check(f"[{mid}] peak and episodes equal the tracker's",
              d["peak_mem_mb"] == rec.peak_mem_mb and d["burst_episodes"] == rec.burst_episodes)
        if d["burst_seconds"] > 0:
            # seconds x average overage ~ MB-seconds (exact for a constant overage)
            avg = d["burst_mb_seconds"] / d["burst_seconds"]
            check(f"[{mid}] seconds x average overage = MB-seconds",
                  _near(d["burst_seconds"] * avg, d["burst_mb_seconds"]) and 0 < avg <= d["peak_mem_mb"] - TIER)


class _FakeStorage:
    def __init__(self, stats):
        self.stats, self.calls = stats, []

    async def metrics_get_burst(self, mid, base):
        self.calls.append((mid, base))
        if isinstance(self.stats.get(mid), Exception):
            raise self.stats[mid]
        return self.stats.get(mid)

    def __getattr__(self, name):  # tripwire: nothing but metrics_get_burst
        raise AssertionError(f"unexpected storage/VM call: {name}")


def test_fields_for_costs():
    print("test_fields_for_costs")
    burst_time.clear_cache()
    stats = {
        "vm-b": {"samples": 5, "burst_samples": 3, "burst_seconds": 4.123,
                 "burst_mb_seconds": 100.0, "burst_episodes": 3, "peak_mem_mb": 4200.0},
        "vm-x": RuntimeError("db down"),
    }
    fs = _FakeStorage(stats)
    costs = {
        "vm-b": {"memory_gb": 2.0, "burst_mb_seconds": 100.0},
        "vm-0": {"memory_gb": 2.0, "burst_mb_seconds": 0.0},
        "vm-x": {"memory_gb": 2.0, "burst_mb_seconds": 5.0},
    }
    asyncio.run(burst_time.add_burst_fields(fs, costs))
    check("only VMs with burst_mb_seconds > 0 are queried",
          sorted(m for m, _ in fs.calls) == ["vm-b", "vm-x"] and fs.calls[0][1] == 2048)
    check("fields merged into the cost dict",
          costs["vm-b"]["burst_seconds"] == 4.1 and costs["vm-b"]["burst_peak_over_mb"] == 2152.0
          and costs["vm-b"]["burst_episodes"] == 3)
    check("no burst -> no fields; DB error -> no fields (UI shows old text)",
          "burst_seconds" not in costs["vm-0"] and "burst_seconds" not in costs["vm-x"])
    n = len(fs.calls)
    asyncio.run(burst_time.add_burst_fields(fs, {"vm-b": {"memory_gb": 2.0, "burst_mb_seconds": 1.0}}))
    check("10 s cache: no second DB read", len(fs.calls) == n)
    src = open(os.path.join(ROOT, "proxy/platform/routes/microvm.py")).read()
    check("list_instances wires add_burst_fields (cost dict only)",
          "burst_time.add_burst_fields(astorage, burst_costs)" in src)


if __name__ == "__main__":
    test_cases()
    test_consistency()
    test_fields_for_costs()
    bad = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(bad)}/{len(_checks)} passed")
    sys.exit(1 if bad else 0)
