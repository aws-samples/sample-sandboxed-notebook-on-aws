#!/usr/bin/env python3
# test-kind: offline
"""
VM metrics history grouped per run (vm-run-chart): proxy/platform/vm_metrics_history.py
and GET /instances/{microvm_id}/metrics-history in proxy/platform/routes/microvm.py.

No network, asyncio.run:
  - assembly with a fake async storage: run groups (3 samples draw; a 40 s Run All
    with 40 samples → 1 group of 30 points, peaks preserved), 6 runs + 2 ad-hoc
    bursts with idle/suspended time between (no sample dropped), in-progress and
    overlapping runs, 30 runs → the last 10 groups + lifetime peaks from a hidden
    run, groups clamped to [1, 20], memory % of the tier limit (or the VM %),
    empty VM, storage failure, per-VM only, pool spare.
  - route: id validation (400), unknown VM (404), 200 payload, the 10 s cache
    keyed by groups, an old ?range=… still 200, and the VM is NEVER called
    (tripwire vm_manager + http client).
  - real SQLite end to end through AsyncStorage (the storage interface).

Run:
    python3 tests/proxy/test_vm_metrics_history.py
    python3 -m pytest tests/proxy/test_vm_metrics_history.py -q
"""
import asyncio
import math
import os
import sys
import tempfile
import threading
import types
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from proxy.platform import vm_metrics_history as vmh  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _assert_since(start):
    failed = [n for n, ok in _checks[start:] if not ok]
    assert not failed, "; ".join(failed)


def _case(fn):
    """Run a check-style case; under pytest, fail on any failed check."""
    def wrapper():
        start = len(_checks)
        fn()
        _assert_since(start)
    wrapper.__name__ = fn.__name__
    return wrapper


NOW = 1_800_000_000.0
MID = "microvm-aaa111"
MID2 = "microvm-bbb222"


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class FakeStorage:
    """Async storage fake. `buckets` maps microvm_id -> list of (epoch, values) samples;
    bucketing/summary are computed like the backends do ([floor(lo), floor(hi)+1))."""

    def __init__(self, rows=None, samples=None, runs=None, siblings=None, fail=()):
        self.rows = rows or {}
        self.samples = samples or {}
        self.runs = runs or []
        self.siblings = siblings or {}
        self.fail = set(fail)
        self.calls = []

    def _win(self, mid, lo, hi):
        a, b = math.floor(lo), math.floor(hi) + 1
        return [s for s in self.samples.get(mid, []) if a <= s["t"] < b]

    async def vm_session_get(self, mid):
        self.calls.append(("vm_session_get", mid))
        return self.rows.get(mid)

    async def vm_session_list_for_session(self, sid):
        self.calls.append(("vm_session_list_for_session", sid))
        return self.siblings.get(sid, [])

    async def notebook_run_list_for_vms(self, ids, limit=200):
        self.calls.append(("notebook_run_list_for_vms", tuple(ids)))
        if "runs" in self.fail:
            raise RuntimeError("boom")
        return [r for r in self.runs if r["microvm_id"] in ids][:limit]

    async def metrics_get_summary(self, mid, lo, hi):
        self.calls.append(("metrics_get_summary", mid))
        if "summary" in self.fail:
            raise RuntimeError("boom")
        w = self._win(mid, lo, hi)
        if not w:
            return None

        def peak(k):
            best = max(w, key=lambda s: (s[k], -s["t"]))
            return best[k], best["t"]
        cpu, cpu_at = peak("cpu")
        mem, mem_at = peak("mem")
        disk, disk_at = peak("disk")
        return {"samples": len(w), "first_at": w[0]["t"], "last_at": w[-1]["t"],
                "cpu_avg": sum(s["cpu"] for s in w) / len(w), "cpu_max": cpu, "cpu_max_at": cpu_at,
                "mem_used_avg": sum(s["mem"] for s in w) / len(w), "mem_used_max": mem,
                "mem_used_max_at": mem_at, "mem_pct_max": None, "disk_pct_max": disk,
                "disk_pct_max_at": disk_at, "disk_used_max": 2000.0}

    async def metrics_get_bucketed(self, mid, lo, hi, bucket_sec):
        self.calls.append(("metrics_get_bucketed", mid, bucket_sec))
        if "bucketed" in self.fail:
            raise RuntimeError("boom")
        a = math.floor(lo)
        out = {}
        for s in self._win(mid, lo, hi):
            b = int((s["t"] - a) // bucket_sec)
            out.setdefault(b, []).append(s)
        rows = []
        for b in sorted(out):
            ss = out[b]
            rows.append({"bucket": b, "samples": len(ss), "t_first": ss[0]["t"], "t_last": ss[-1]["t"],
                         "cpu_avg": sum(x["cpu"] for x in ss) / len(ss),
                         "cpu_max": max(x["cpu"] for x in ss),
                         "mem_used_avg": sum(x["mem"] for x in ss) / len(ss),
                         "mem_used_max": max(x["mem"] for x in ss), "mem_pct_max": None,
                         "disk_pct_max": max(x["disk"] for x in ss), "disk_used_max": 2000.0})
        return rows


def _s(t, cpu=10.0, mem=800.0, disk=30.0):
    return {"t": float(t), "cpu": cpu, "mem": mem, "disk": disk}


def _build(fake, mid=MID, **kw):
    kw.setdefault("last_instance", {})
    return asyncio.run(vmh.build_metrics_history(mid, astorage=fake, now=lambda: NOW, **kw))


def _run(rid, start, end, trigger="run_all", mid=MID, executed=3, errored=0):
    return {"run_id": rid, "microvm_id": mid, "trigger": trigger, "notebook_id": 32,
            "started_at": _iso(start), "finished_at": _iso(end) if end is not None else None,
            "cells_executed": executed, "cells_errored": errored, "cells_interrupted": 0,
            "total_exec_ms": 1000.0}


def _vm(state="SUSPENDED", limit=2048):
    return {MID: {"microvm_id": MID, "state": state, "memory_mib": limit, "session_id": "s1"}}


def _pts(p):
    return sum(len(g["points"]) for g in p["groups"])


# ── assembly ────────────────────────────────────────────────────────────────
@_case
def test_short_run_draws():
    print("test_short_run_draws")
    t0 = NOW - 600
    fake = FakeStorage(rows=_vm(), samples={MID: [_s(t0 + k, cpu=5.0 + k) for k in range(3)]},
                       runs=[_run("r1", t0 - 0.5, t0 + 3, trigger="single", executed=1)])
    p = _build(fake)
    check("3 samples → 1 group", len(p["groups"]) == 1 and p["groups_total"] == 1)
    g = p["groups"][0]
    check("the group is the run", g["kind"] == "run" and g["id"] == "run:r1" and g["trigger"] == "single")
    check("3 points (no threshold)", len(g["points"]) == 3 and g["samples"] == 3)
    check("cells counts from the run", g["cells_executed"] == 1 and g["cells_errored"] == 0)
    check("duration = run window", abs(g["duration_sec"] - 3.5) < 0.01)
    check("not empty, active_sec = the group", p["empty"] is False and abs(p["active_sec"] - 3.5) < 0.01)
    check("no removed fields", not any(k in p for k in ("range", "window", "series", "runs", "states")))
    check("dead helpers removed", not any(hasattr(vmh, n) for n in (
        "RANGES", "RANGE_KEYS", "MAX_RANGE_SEC", "MAX_POINTS", "MIN_POINTS", "clamp_points",
        "bucket_seconds", "state_spans", "with_gaps", "_point", "_series_for")))
    check("bucketed at 1 s", [c for c in fake.calls if c[0] == "metrics_get_bucketed"][0][2] == 1)


@_case
def test_run_all_40s():
    print("test_run_all_40s")
    t0 = NOW - 3 * 3600      # the VM lived hours; the Run All is 40 s of it
    samples = [_s(t0 - 7200 + k, cpu=2.0) for k in range(5)]   # an early ad-hoc burst
    samples += [_s(t0 + k, cpu=26.0 if k == 17 else 3.0 + (k % 5), mem=447.0 if k == 33 else 400.0)
                for k in range(40)]
    fake = FakeStorage(rows=_vm(limit=2048), samples={MID: samples},
                       runs=[_run("ra", t0 - 0.3, t0 + 40, executed=12, errored=1)])
    p = _build(fake)
    g = p["groups"][-1]
    check("2 groups: the early burst + the Run All", [x["kind"] for x in p["groups"]] == ["cells", "run"])
    check("Run All: 40 samples → 30 points", g["samples"] == 40 and len(g["points"]) == 30)
    check("peak preserved: max(points.cpu) == cpu_peak == lifetime peak",
          max(x["cpu"] for x in g["points"]) == g["cpu_peak"] == p["summary"]["cpu"]["peak"] == 26.0)
    check("memory peak preserved", max(x["mem_mb"] for x in g["points"]) == g["mem_peak_mb"] == 447.0)
    check("mem_pct of the tier limit", g["mem_peak_pct"] == round(447 / 2048 * 100, 1)
          and all(x["mem_pct"] == round(x["mem_mb"] / 2048 * 100, 1) for x in g["points"]))
    check("points ascending, t_end >= t", all(a["t"] < b["t"] for a, b in zip(g["points"], g["points"][1:]))
          and all(x["t_end"] >= x["t"] for x in g["points"]))
    check("point samples sum to the group", sum(x["samples"] for x in g["points"]) == 40)
    check("errors carried", g["cells_errored"] == 1 and g["cells_executed"] == 12)


def _mixed():
    t0 = NOW - 4 * 3600
    samples, runs = [], []
    cursor = t0
    for i in range(6):
        n = 5 + i * 3
        runs.append(_run(f"r{i}", cursor, cursor + n, errored=1 if i == 2 else 0))
        samples += [_s(cursor + k) for k in range(n)]
        cursor += n + 900                       # idle / suspended stretch
        if i in (1, 4):                         # an ad-hoc burst between runs
            samples += [_s(cursor + k, cpu=40.0) for k in range(4)]
            cursor += 4 + 1200
    return samples, runs


@_case
def test_mixed_runs_and_bursts():
    print("test_mixed_runs_and_bursts")
    samples, runs = _mixed()
    fake = FakeStorage(rows=_vm(), samples={MID: samples}, runs=runs)
    p = _build(fake)
    kinds = [g["kind"] for g in p["groups"]]
    check("8 groups in order", kinds == ["run", "run", "cells", "run", "run", "run", "cells", "run"])
    check("run ids in order", [g["run_id"] for g in p["groups"] if g["kind"] == "run"]
          == ["r0", "r1", "r2", "r3", "r4", "r5"])
    check("cells groups have no run fields", all(g["run_id"] is None and g["cells_executed"] is None
                                                 and g["id"].startswith("cells:")
                                                 for g in p["groups"] if g["kind"] == "cells"))
    check("no sample dropped", sum(g["samples"] for g in p["groups"]) == len(samples))
    check("groups chronological, non-overlapping",
          all(a["end"] <= b["start"] for a, b in zip(p["groups"], p["groups"][1:])))
    check("active_sec excludes idle/suspended time",
          p["active_sec"] < 200 and abs(p["active_sec"] - sum(g["duration_sec"] for g in p["groups"])) < 0.5)


@_case
def test_bursts_split():
    print("test_bursts_split")
    t0 = NOW - 1000
    samples = [_s(t0 + k) for k in range(3)] + [_s(t0 + 2 + 11 + k) for k in range(3)] \
        + [_s(t0 + 100 + k) for k in range(2)] + [_s(t0 + 106 + k) for k in range(2)]
    # attach window = [103 - 2, 103 + 2]: takes t0+101 only; 100 → 106 is only 6 s apart
    runs = [_run("mid", t0 + 103, t0 + 103, trigger="single")]
    fake = FakeStorage(rows=_vm(), samples={MID: samples}, runs=runs)
    p = _build(fake)
    check("gap > 10 s splits a burst; a run in between splits too",
          [(g["kind"], g["samples"]) for g in p["groups"]]
          == [("cells", 3), ("cells", 3), ("cells", 1), ("run", 1), ("cells", 2)])
    check("no sample dropped", sum(g["samples"] for g in p["groups"]) == len(samples))


@_case
def test_in_progress_and_overlap():
    print("test_in_progress_and_overlap")
    t0 = NOW - 300
    samples = [_s(t0 + k) for k in range(60)]
    fake = FakeStorage(rows=_vm("RUNNING"), samples={MID: samples},
                       runs=[_run("live", t0, None)])
    p = _build(fake, last_instance={MID: {"state": "RUNNING"}})
    check("in-progress run takes every sample after its start",
          len(p["groups"]) == 1 and p["groups"][0]["run_id"] == "live" and p["groups"][0]["samples"] == 60)
    check("in-progress run ends at its last sample", abs(p["groups"][0]["end"] - (t0 + 59)) < 0.01)
    ov = FakeStorage(rows=_vm(), samples={MID: samples},
                     runs=[_run("outer", t0, t0 + 59), _run("inner", t0 + 20, t0 + 30, trigger="single")])
    po = _build(ov)
    by = {g["run_id"]: g for g in po["groups"]}
    # inner attaches [t0+18, t0+32] (± ATTACH_SLOP_SEC) → 15 rows; outer keeps the rest
    check("overlap: rows go to the latest-starting run that contains them",
          by["inner"]["samples"] == 15 and by["outer"]["samples"] == 45)
    check("overlap: no sample dropped", sum(g["samples"] for g in po["groups"]) == 60)
    nos = FakeStorage(rows=_vm(), samples={MID: samples},
                      runs=[_run("outer", t0, t0 + 59), _run("empty", NOW - 50, NOW - 40)])
    check("a run without samples makes no group", [g["run_id"] for g in _build(nos)["groups"]] == ["outer"])


@_case
def test_thirty_runs():
    print("test_thirty_runs")
    t0 = NOW - 3600
    samples, runs = [], []
    for i in range(30):
        s0 = t0 + i * 60
        runs.append(_run(f"r{i:02d}", s0, s0 + 10))
        hot = i == 3
        samples += [_s(s0 + k, cpu=95.0 if (hot and k == 4) else 30.0,
                       mem=1900.0 if (hot and k == 5) else 1000.0) for k in range(10)]
    fake = FakeStorage(rows=_vm(), samples={MID: samples}, runs=runs)
    p = _build(fake)
    check("groups_total 30, 10 returned", p["groups_total"] == 30 and len(p["groups"]) == 10)
    check("the newest 10", [g["run_id"] for g in p["groups"]] == [f"r{i:02d}" for i in range(20, 30)])
    check("lifetime peaks come from a hidden older run",
          p["summary"]["cpu"]["peak"] == 95.0 and p["summary"]["memory"]["peak_mb"] == 1900.0
          and max(g["cpu_peak"] for g in p["groups"]) == 30.0)
    check("active_sec counts ALL groups", abs(p["active_sec"] - 300) < 1)
    check("<= 300 points", _pts(p) <= 300)
    check("groups=0 → 1", len(_build(fake, groups=0)["groups"]) == 1)
    check("groups=99 → 20", len(_build(fake, groups=99)["groups"]) == 20)
    check("clamp_groups bounds", vmh.clamp_groups("x") == 10 and vmh.clamp_groups(-5) == 1
          and vmh.clamp_groups(21) == 20)


@_case
def test_mem_pct_without_limit():
    print("test_mem_pct_without_limit")
    fake = FakeStorage(rows={MID: {"microvm_id": MID, "state": "SUSPENDED"}},
                       samples={MID: [_s(NOW - 10 + k) for k in range(3)]})
    orig = fake.metrics_get_bucketed

    async def with_pct(*a):
        rows = await orig(*a)
        for r in rows:
            r["mem_pct_max"] = 37.5
        return rows
    fake.metrics_get_bucketed = with_pct
    p = _build(fake)
    g = p["groups"][0]
    check("no limit: memory % is the VM-reported %", p["memory_limit_mb"] is None
          and all(x["mem_pct"] == 37.5 for x in g["points"]) and g["mem_peak_pct"] == 37.5)
    check("ad-hoc group without any run", g["kind"] == "cells")


@_case
def test_empty_and_errors():
    print("test_empty_and_errors")
    fake = FakeStorage(rows={MID: {"microvm_id": MID, "state": "SUSPENDED", "memory_mib": 2048}})
    p = _build(fake)
    check("no samples: empty, summary null, no groups", p["empty"] is True and p["summary"] is None
          and p["groups"] == [] and p["groups_total"] == 0 and p["active_sec"] == 0 and "error" not in p)
    check("no samples: memory limit still reported", p["memory_limit_mb"] == 2048)
    bad = FakeStorage(rows={MID: {"microvm_id": MID}}, samples={MID: [_s(NOW - 10)]}, fail={"summary"})
    pe = _build(bad)
    check("summary failure: empty payload with error=unavailable", pe["empty"] and pe["error"] == "unavailable")
    bad2 = FakeStorage(rows={MID: {"microvm_id": MID}}, samples={MID: [_s(NOW - 10)]}, fail={"bucketed"})
    check("bucketed failure: error=unavailable", _build(bad2).get("error") == "unavailable")
    bad3 = FakeStorage(rows={MID: {"microvm_id": MID}}, samples={MID: [_s(NOW - 10)]}, fail={"runs"})
    p3 = _build(bad3)
    check("runs failure: samples still drawn as an ad-hoc group",
          len(p3["groups"]) == 1 and p3["groups"][0]["kind"] == "cells")
    check("summary disk.last_pct", _build(FakeStorage(rows=_vm(), samples={MID: [_s(NOW - 5, disk=20.0),
                                                                                  _s(NOW - 4, disk=23.0)]}))
          ["summary"]["disk"]["last_pct"] == 23.0)


@_case
def test_per_vm_only():
    print("test_per_vm_only")
    rows = {MID: {"microvm_id": MID, "session_id": "s1", "memory_mib": 2048, "state": "TERMINATED"},
            MID2: {"microvm_id": MID2, "session_id": "s1", "memory_mib": 4096, "state": "SUSPENDED"}}
    samples = {MID: [_s(NOW - 3000 + k, mem=1000.0) for k in range(300)],
               MID2: [_s(NOW - 1000 + k, mem=3000.0, cpu=70.0) for k in range(300)]}
    fake = FakeStorage(rows=rows, samples=samples, siblings={"s1": [rows[MID], rows[MID2]]},
                       runs=[_run("a", NOW - 2990, NOW - 2900, mid=MID),
                             _run("b", NOW - 990, NOW - 900, mid=MID2)])
    p = _build(fake, mid=MID2)
    check("payload has no scope / vms fields", "scope" not in p and "vms" not in p)
    check("groups only from this VM's samples", sum(g["samples"] for g in p["groups"]) == 300)
    check("runs only from this VM", [g["run_id"] for g in p["groups"] if g["kind"] == "run"] == ["b"])
    check("runs queried for this VM only",
          [c for c in fake.calls if c[0] == "notebook_run_list_for_vms"] == [("notebook_run_list_for_vms", (MID2,))])
    check("summary + limit are this VM's", p["summary"]["memory"]["peak_mb"] == 3000.0
          and p["summary"]["memory"]["limit_mb"] == 4096 and p["memory_limit_mb"] == 4096)
    check("only this VM's metrics were read",
          {c[1] for c in fake.calls if c[0].startswith("metrics_")} == {MID2})
    check("no session lookup", not any(c[0] == "vm_session_list_for_session" for c in fake.calls))
    spare = FakeStorage(rows={MID: {"microvm_id": MID, "state": "SUSPENDED", "session_id": None}})
    check("pool spare: empty", _build(spare)["empty"])


# ── route ──────────────────────────────────────────────────────────────────
class _Tripwire:
    """vm_manager stand-in: only `active_microvms` (a plain dict) may be read."""

    def __init__(self, active=None):
        object.__setattr__(self, "active_microvms", dict(active or {}))

    def __getattr__(self, name):
        raise AssertionError(f"vm_manager.{name} was touched (the history route must not call the VM)")


def _request(vm_manager):
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(vm_manager=vm_manager)))


@_case
def test_route():
    print("test_route")
    from proxy.platform.routes import microvm as routes
    from proxy.platform import http_clients

    def _boom(*a, **k):
        raise AssertionError("HTTP client used: the history route must not call the VM")
    saved = (routes.astorage, http_clients.vm_client, http_clients.request_idempotent,
             dict(routes._LAST_INSTANCE))
    import time as _t
    real = _t.time()  # the route uses the real clock
    fake = FakeStorage(rows={MID: {"microvm_id": MID, "state": "SUSPENDED", "memory_mib": 2048}},
                       samples={MID: [_s(real - 100 + k) for k in range(50)]})
    routes.astorage = fake
    http_clients.vm_client = _boom
    http_clients.request_idempotent = _boom
    req = _request(_Tripwire())

    def call(mid, groups=10):
        return asyncio.run(routes.get_instance_metrics_history(mid, req, groups=groups))
    try:
        vmh.clear_cache()
        for bad in ("../etc", "microvm-a/b", "", "microvm-" + "a" * 81):
            r = call(bad)
            check(f"bad id {bad[:20]!r} -> 400", getattr(r, "status_code", None) == 400)
        import inspect
        params = inspect.signature(routes.get_instance_metrics_history).parameters
        check("route takes groups only (no scope/range/points)",
              "groups" in params and not any(k in params for k in ("scope", "range_", "points")))
        check("unknown VM -> 404", getattr(call("microvm-zzz999"), "status_code", None) == 404)
        n0 = len(fake.calls)
        r = call(MID)
        check("valid -> 200 payload", isinstance(r, dict) and r["microvm_id"] == MID and not r["empty"]
              and len(r["groups"]) == 1)
        n1 = len(fake.calls)
        check("assembly read the DB", n1 > n0)
        r2 = call(MID)
        check("second call within TTL served from cache (no storage calls beyond the known-check)",
              r2 is r and [c[0] for c in fake.calls[n1:]] == ["vm_session_get"])
        n2 = len(fake.calls)
        call(MID, groups=5)
        check("cache keyed by groups (groups=5 rebuilt)",
              any(c[0] == "metrics_get_bucketed" for c in fake.calls[n2:]))
        vmh.clear_cache()
        call(MID)
        check("after cache clear, rebuilt", len([c for c in fake.calls if c[0] == "metrics_get_bucketed"]) == 3)
        # storage failure -> 200 with error, not cached
        vmh.clear_cache()
        fake.fail = {"summary"}
        e1 = call(MID)
        fake.fail = set()
        check("storage failure -> error payload", e1.get("error") == "unavailable" and e1["empty"])
        check("error payload not cached", vmh.cache_get((MID, 10)) is None)
        # cache bound
        vmh.clear_cache()
        for i in range(vmh._CACHE_MAX + 5):
            vmh.cache_put(("k", i), {})
        check("cache bounded", len(vmh._CACHE) <= vmh._CACHE_MAX)
        check("cache TTL expiry", vmh.cache_get(("k", vmh._CACHE_MAX + 4), now_mono=1e18) is None)
        # an old UI's ?range=lifetime: FastAPI ignores unknown query params → 200
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            app = FastAPI()
            app.include_router(routes.router)
            app.state.vm_manager = _Tripwire()
            vmh.clear_cache()
            resp = TestClient(app).get(f"/instances/{MID}/metrics-history?range=lifetime&points=200")
            check("?range=lifetime still 200 with groups", resp.status_code == 200
                  and "groups" in resp.json())
        except ImportError as e:
            print(f"  (skipped TestClient check: {e})")
    finally:
        routes.astorage, http_clients.vm_client, http_clients.request_idempotent, li = saved
        routes._LAST_INSTANCE.clear()
        routes._LAST_INSTANCE.update(li)
        vmh.clear_cache()


# ── real SQLite through the storage interface ─────────────────────────────────
@_case
def test_sqlite_end_to_end():
    print("test_sqlite_end_to_end")
    import time as _t
    import proxy.storage.sqlite_db as sq
    from proxy.storage.async_storage import AsyncStorage
    saved = (sq.DB_DIR, sq.DB_PATH, sq._local)
    tmp = tempfile.mkdtemp(prefix="vmh_sqlite_")
    try:
        sq.DB_DIR = tmp
        sq.DB_PATH = os.path.join(tmp, "vmh.db")
        sq._local = threading.local()
        st = sq.SqliteStorage()
        st.initialize()
        real = _t.time()
        st.vm_session_create(MID, session_id="sx", memory_mib=2048)
        st.vm_session_update_state(MID, "RUNNING")
        # an ad-hoc burst, idle, then a 40-sample Run All, then SUSPENDED
        for k in range(5):
            st.metrics_record(MID, 15.0, 30.0, 700.0, 20.0, 1500.0, 0, 0, 3, 1.0,
                              recorded_at=real - 1500 + k)
        for k in range(40):
            st.metrics_record(MID, 88.0 if k == 7 else 25.0, 90.0, 1900.0 if k == 9 else 900.0,
                              22.0, 1600.0, 0, 0, 3, 1.0, recorded_at=real - 600 + k)
        st.vm_session_update_state(MID, "SUSPENDED")
        st.notebook_run_create("e2e-run", session_id="sx", microvm_id=MID, trigger="run_all",
                               started_at=_iso(real - 600.5), finished_at=_iso(real - 560),
                               cells_executed=5, cells_errored=0, total_exec_ms=40000.0,
                               run_cells=[{"cell_id": "c"}])
        a = AsyncStorage(st)
        p = asyncio.run(vmh.build_metrics_history(MID, astorage=a, last_instance={MID: {"state": "SUSPENDED"}},
                                                  now=lambda: real + 500))
        check("SQLite: 2 groups (burst, Run All)", [g["kind"] for g in p["groups"]] == ["cells", "run"])
        g = p["groups"][-1]
        check("SQLite: the 40-sample Run All draws 1 group of 30 points",
              g["run_id"] == "e2e-run" and g["samples"] == 40 and len(g["points"]) == 30)
        sm = p["summary"]
        check("SQLite: summary peaks match inserted values",
              sm["samples"] == 45 and sm["cpu"]["peak"] == 88.0
              and sm["memory"]["peak_mb"] == 1900.0 and sm["memory"]["limit_mb"] == 2048
              and sm["disk"]["peak_pct"] == 22.0)
        check("SQLite: the cpu peak survives downsampling", max(x["cpu"] for x in g["points"]) == 88.0)
        check("SQLite: no retention field", "retention_hours" not in p)
    finally:
        sq.DB_DIR, sq.DB_PATH, sq._local = saved


TESTS = (test_short_run_draws, test_run_all_40s, test_mixed_runs_and_bursts, test_bursts_split,
         test_in_progress_and_overlap, test_thirty_runs, test_mem_pct_without_limit,
         test_empty_and_errors, test_per_vm_only, test_route, test_sqlite_end_to_end)


def main():
    for fn in TESTS:
        try:
            fn()
        except AssertionError:
            pass
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    sys.exit(main())
