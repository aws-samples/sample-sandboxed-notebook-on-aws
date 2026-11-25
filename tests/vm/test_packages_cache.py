#!/usr/bin/env python3
# test-kind: offline
"""
app/notebook/package_list.py: the GET /packages list cached on a site-packages
fingerprint. Works on private tempfile.mkdtemp dirs of name-ver.dist-info/METADATA
passed as paths=[tmp]; nothing is installed, no AWS, no network.
Cases: hit; miss on install; uninstall; terminal-style pip (no lock); upgrade of a
baseline package; same-mtime-tick change; concurrent cold rebuild is single-flight;
failed-install leftovers (~oo-1.0.dist-info); restore reinstall (pip_lock with no fs
change); nothing cached while pip_generation is odd.
Run:
    .venv/bin/python tests/vm/test_packages_cache.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from importlib.metadata import distributions

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.notebook import package_list as pl  # noqa: E402
from app.platform import pip_helper as ph  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}" + (f"  [{detail}]" if not ok and detail else ""))


def mk(root, name, ver, dirname=None):
    d = os.path.join(root, dirname or f"{name}-{ver}.dist-info")
    os.makedirs(d)
    with open(os.path.join(d, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n")
    return d


class Counter:
    """Wraps pl._enumerate, counting calls (optionally slowed down)."""
    def __init__(self, delay=0.0):
        self.n = 0
        self.delay = delay
        self._real = pl._enumerate
        self._lock = threading.Lock()

    def __call__(self, paths):
        with self._lock:
            self.n += 1
        if self.delay:
            time.sleep(self.delay)
        return self._real(paths)


def names(pkgs):
    return {p["name"] for p in pkgs}


def versions(pkgs):
    return {p["name"]: p["version"] for p in pkgs}


def _same_tick(*paths_and_stats):
    """Put back the mtimes captured in (path, os.stat_result) pairs."""
    for p, st in paths_and_stats:
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


class _FakeClock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


def metadata_window_sections(tmp, paths, cnt):
    """followups-batch part 2: the dist-info-created-before-METADATA window."""
    real_clock = pl._clock
    clock = _FakeClock()
    pl._clock = clock
    try:
        print("[dist-info dir first, METADATA later]")
        pl.list_packages(paths)
        parent_st = os.stat(tmp)
        d = os.path.join(tmp, "late-1.0.dist-info")
        os.makedirs(d)
        with open(os.path.join(d, "RECORD"), "w") as f:
            f.write("")
        r = pl.list_packages(paths)
        check("mid-install: package not listed yet", "late" not in names(r))
        check("mid-install: fingerprint flags incomplete", pl.incomplete(pl.fingerprint(paths)))
        check("mid-install: list kept only with a TTL", pl._cache is not None and pl._cache[2] is not None,
              str(pl._cache and pl._cache[2]))
        dir_st = os.stat(d)
        n0 = cnt.n
        with open(os.path.join(d, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: late\nVersion: 1.0\n")
        # Same mtime tick for both directories: only the METADATA stat changed.
        _same_tick((d, dir_st), (tmp, parent_st))
        r = pl.list_packages(paths)
        check("after METADATA write (same dir mtimes): new package listed", "late" in names(r), str(names(r)))
        check("after METADATA write: one rebuild, before any TTL expiry", cnt.n == n0 + 1, f"n={cnt.n - n0}")
        check("complete again: stored without TTL", pl._cache[2] is None)
        pl.list_packages(paths)
        check("complete again: then a hit", cnt.n == n0 + 1)

        print("[incomplete install not cached past the TTL]")
        d2 = os.path.join(tmp, "stuck-1.0.dist-info")
        os.makedirs(d2)  # a leftover with no METADATA at all
        n0 = cnt.n
        pl.list_packages(paths)
        pl.list_packages(paths)
        check("within the TTL: one walk", cnt.n == n0 + 1, f"n={cnt.n - n0}")
        clock.now += pl.INCOMPLETE_TTL_SEC + 0.01
        pl.list_packages(paths)
        check("after the TTL: re-read", cnt.n == n0 + 2, f"n={cnt.n - n0}")
        with open(os.path.join(d2, "METADATA"), "w") as f:
            f.write("")
        clock.now += pl.INCOMPLETE_TTL_SEC + 0.01
        check("empty METADATA still incomplete", pl.incomplete(pl.fingerprint(paths)))
        shutil.rmtree(d2)
        check("leftover removed: complete", not pl.incomplete(pl.fingerprint(paths)))

        print("[baseline upgrade in place, same tick]")
        d3 = mk(tmp, "base", "1.0")
        r = pl.list_packages(paths)
        check("base 1.0 listed", versions(r).get("base") == "1.0")
        sts = [(d3, os.stat(d3)), (tmp, os.stat(tmp))]
        with open(os.path.join(d3, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: base\nVersion: 1.10\n")  # size differs
        _same_tick(*sts)
        r = pl.list_packages(paths)
        check("in-place METADATA rewrite -> new version", versions(r).get("base") == "1.10", str(versions(r)))

        print("[two changes in one mtime tick]")
        meta = os.path.join(d3, "METADATA")
        sts = [(meta, os.stat(meta)), (d3, os.stat(d3)), (tmp, os.stat(tmp))]
        fp0 = pl.fingerprint(paths)
        with open(meta, "w") as f:
            f.write("Metadata-Version: 2.1\nName: base\nVersion: 1.2\n")
        fp1 = pl.fingerprint(paths)
        with open(meta, "w") as f:
            f.write("Metadata-Version: 2.1\nName: base\nVersion: 1.333\n")
        _same_tick(*sts)  # every mtime back to the tick of the first write
        fp2 = pl.fingerprint(paths)
        check("first write changes the key", fp1 != fp0)
        check("second write in the same tick (all mtimes equal) still changes the key via size",
              fp2 != fp0 and fp2 != fp1)
        check("list follows", versions(pl.list_packages(paths)).get("base") == "1.333")

        print("[uninstall, same tick]")
        sts = [(tmp, os.stat(tmp))]
        shutil.rmtree(d3)
        _same_tick(*sts)
        check("uninstalled package gone", "base" not in names(pl.list_packages(paths)))

        print("[concurrent rebuild after a METADATA write is single-flight]")
        d4 = os.path.join(tmp, "conc-1.0.dist-info")
        os.makedirs(d4)
        pl.list_packages(paths)
        with open(os.path.join(d4, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: conc\nVersion: 1.0\n")
        slow = Counter(delay=0.2)
        pl._enumerate = slow
        barrier = threading.Barrier(8)
        results = [None] * 8

        def worker(i):
            barrier.wait()
            results[i] = pl.list_packages(paths)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        check("8 threads -> one enumeration", slow.n == 1, f"n={slow.n}")
        check("all see the new package", all(r is not None and "conc" in names(r) for r in results))
        pl._enumerate = cnt
        shutil.rmtree(d4)
    finally:
        pl._clock = real_clock
    benchmark()


def benchmark():
    """Stat-only fingerprint over a VM-sized site-packages (about 120 dist-infos)."""
    import builtins
    print("[fingerprint micro-benchmark, 120 dist-infos]")
    root = tempfile.mkdtemp(prefix="pkgbench-")
    try:
        for i in range(120):
            d = mk(root, f"pkg{i:03d}", "1.0")
            for extra in ("RECORD", "WHEEL", "INSTALLER", "top_level.txt"):
                with open(os.path.join(d, extra), "w") as f:
                    f.write("x\n")
            os.makedirs(os.path.join(root, f"pkg{i:03d}"))
        bench_paths = [root]
        pl.fingerprint(bench_paths)  # warm the dentry cache
        real_open = builtins.open
        opened = []

        def spy_open(*a, **k):
            opened.append(a[0] if a else k.get("file"))
            return real_open(*a, **k)
        builtins.open = spy_open
        try:
            times = []
            for _ in range(30):
                t0 = time.perf_counter()
                pl.fingerprint(bench_paths)
                times.append((time.perf_counter() - t0) * 1000)
        finally:
            builtins.open = real_open
        times.sort()
        med, p95 = times[len(times) // 2], times[int(len(times) * 0.95) - 1]
        print(f"    warm fingerprint: median {med:.2f} ms, p95 {p95:.2f} ms, max {times[-1]:.2f} ms")
        check("warm fingerprint median < 20 ms", med < 20.0, f"{med:.2f} ms")
        check("fingerprint opens no file", opened == [], str(opened[:3]))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    real_enum = pl._enumerate
    tmp = tempfile.mkdtemp(prefix="pkgcache-")
    paths = [tmp]
    try:
        mk(tmp, "foo", "1.0")
        mk(tmp, "Alpha", "0.1")
        cnt = Counter()
        pl._enumerate = cnt
        pl._cache = None

        print("[hit]")
        a = pl.list_packages(paths)
        b = pl.list_packages(paths)
        check("two calls -> one enumeration", cnt.n == 1, f"n={cnt.n}")
        check("same result", a == b)
        check("names lowercased, versions kept", versions(a) == {"foo": "1.0", "alpha": "0.1"}, str(a))
        a[0]["name"] = "mutated"
        check("callers get a copy", "mutated" not in names(pl.list_packages(paths)))

        print("[install]")
        n0 = cnt.n
        mk(tmp, "bar", "2.0")
        r = pl.list_packages(paths)
        check("install -> rebuild", cnt.n == n0 + 1, f"n={cnt.n}")
        check("new package listed", "bar" in names(r))

        print("[uninstall]")
        n0 = cnt.n
        shutil.rmtree(os.path.join(tmp, "bar-2.0.dist-info"))
        r = pl.list_packages(paths)
        check("uninstall -> rebuild", cnt.n == n0 + 1)
        check("removed package gone", "bar" not in names(r))

        print("[terminal pip, no lock]")
        gen0 = ph.pip_generation()
        n0 = cnt.n
        d = mk(tmp, "term", "1.0")
        r1 = pl.list_packages(paths)
        shutil.rmtree(d)
        r2 = pl.list_packages(paths)
        check("generation untouched", ph.pip_generation() == gen0)
        check("create and remove both rebuild", cnt.n == n0 + 2, f"n={cnt.n - n0}")
        check("listed then gone", "term" in names(r1) and "term" not in names(r2))

        print("[upgrade baseline]")
        n0 = cnt.n
        old = os.path.join(tmp, "foo-1.0.dist-info")
        new = os.path.join(tmp, "foo-2.0.dist-info")
        os.rename(old, new)
        with open(os.path.join(new, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n")
        r = pl.list_packages(paths)
        check("upgrade -> rebuild", cnt.n == n0 + 1)
        check("new version reported", versions(r).get("foo") == "2.0", str(r))

        print("[same mtime tick]")
        pl.list_packages(paths)  # cache current state
        st = os.stat(tmp)
        fp_before = pl.fingerprint(paths)
        mk(tmp, "tick", "1.0")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        check("dir mtime restored", os.stat(tmp).st_mtime_ns == st.st_mtime_ns)
        check("fingerprint still differs", pl.fingerprint(paths) != fp_before)
        check("list has the new package", "tick" in names(pl.list_packages(paths)))

        print("[concurrent cold rebuild]")
        slow = Counter(delay=0.2)
        pl._enumerate = slow
        pl._cache = None
        barrier = threading.Barrier(8)
        results = [None] * 8

        def worker(i):
            barrier.wait()
            results[i] = pl.list_packages(paths)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        check("8 threads -> one enumeration", slow.n == 1, f"n={slow.n}")
        check("all threads got the same list", all(x == results[0] for x in results))
        pl._enumerate = cnt

        print("[failed-install leftovers]")
        n0 = cnt.n
        mk(tmp, "oo", "1.0", dirname="~oo-1.0.dist-info")
        r = pl.list_packages(paths)
        check("leftover dir -> rebuild", cnt.n == n0 + 1)
        expect = set()
        for dist in distributions(path=paths):
            nm = (dist.metadata["Name"] or "").strip()
            if nm:
                expect.add(nm.lower())
        check("result equals what distributions() reports", names(r) == expect,
              f"{names(r)} vs {expect}")

        print("[restore reinstall: pip_lock, no fs change]")
        pl.list_packages(paths)
        n0 = cnt.n
        with ph.pip_lock():
            pass
        pl.list_packages(paths)
        check("pip_lock acquire/release -> rebuild", cnt.n == n0 + 1, f"n={cnt.n - n0}")
        pl.list_packages(paths)
        check("then a hit again", cnt.n == n0 + 1)

        print("[no caching while generation is odd]")
        n0 = cnt.n
        with ph.pip_lock():
            check("generation odd while locked", ph.pip_generation() % 2 == 1)
            pl.list_packages(paths)
            pl.list_packages(paths)
            check("each call enumerates (not cached)", cnt.n == n0 + 2, f"n={cnt.n - n0}")
        check("generation even after release", ph.pip_generation() % 2 == 0)
        pl.list_packages(paths)
        pl.list_packages(paths)
        check("after release: one rebuild, then a hit", cnt.n == n0 + 3, f"n={cnt.n - n0}")

        print("[fingerprint]")
        fp = pl.fingerprint([tmp, os.path.join(tmp, "missing")])
        check("missing dir -> (path, None, 0, '', False)",
              fp[2][1] == (os.path.join(tmp, "missing"), None, 0, "", False), str(fp[2][1]))
        check("paths and generation in the key",
              fp[0] == (tmp, os.path.join(tmp, "missing")) and fp[1] == ph.pip_generation())

        metadata_window_sections(tmp, paths, cnt)

        print("[warm]")
        pl._enumerate = real_enum
        pl._cache = None
        pl.warm()
        check("warm() builds the default (sys.path) cache", pl._cache is not None)
    finally:
        pl._enumerate = real_enum
        pl._cache = None
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
