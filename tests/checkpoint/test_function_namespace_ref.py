#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: notebook functions are checkpointed with the notebook namespace
written as a marker ("notebook-namespace", app/platform/checkpoint/nspickle.py)
and restored bound to the LIVE executor namespace.

Drives the real SandboxExecutor + CheckpointManager.save()/restore() against the
in-memory FakeS3 of test_dag_checkpoint.py (no AWS, no MicroVM). Executor A runs
the cells and saves; a fresh executor B restores. Cases:
  (a) a function using a global works after restore;
  (b) live globals: f.__globals__ is B's namespace and a new K is seen
      (before the change the restored globals were a detached copy);
  (c) a helper next to a 50 MiB array pickles to a few hundred bytes, the
      uploaded var bytes stay about one array, and the save peak holds no
      second copy;
  (d) a class with a method/staticmethod/property and an instance;
  (e) lambdas, a shared nonlocal closure, a self-recursive closure, a
      functools.wraps decorator, lru_cache, a generator function;
  (f) mutual recursion and a recursive function;
  (g) functions inside a dict and a list;
  (h) a function using an imported module (import ledger replay);
  (i) a pre-change pickle (function with embedded globals) loads, and a pickle
      with an unknown persistent id is reported in last_restore_not_restored /
      last_restore_report["vars_not_restored"] instead of failing the restore;
  (j) saving twice without changes uploads no var bytes;
  (k) the marker never shows up as a value; a user string equal to it
      round-trips; `g = globals()` restores as B's namespace; an unknown id and
      a plain dill.loads of a marker pickle raise UnpicklingError; nothing
      global was patched (plain pickle has no marker).
Run:
    PYTHONPATH=. .venv/bin/python tests/checkpoint/test_function_namespace_ref.py
"""
import io
import json
import logging
import os
import pickle
import sys
import tempfile
import tracemalloc

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CHECKPOINT_SPOOL_DIR"] = tempfile.mkdtemp(prefix="nsref-spool-")

import dill  # noqa: E402

from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.platform.checkpoint import delta as delta_mod  # noqa: E402
from app.platform.checkpoint import nspickle  # noqa: E402
from test_dag_checkpoint import FakeS3, _Patched, _manager  # noqa: E402

_checks = []
MiB = 1024 * 1024


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _try(fn):
    try:
        return fn()
    except Exception as e:
        return f"<{type(e).__name__}: {e}>"


CELLS = [
    ("c1", "import math\nimport functools\nK = 3\ndef f(x):\n    return x * K\n"),
    ("c2", "class C:\n    def m(self):\n        return K\n    @staticmethod\n    def s():\n        return K + 1\n"
           "    @property\n    def p(self):\n        return K + 2\nc = C()\n"),
    ("c3", "sq = lambda x: x * x + K\n"
           "def make_counter():\n    n = 0\n    def inc():\n        nonlocal n\n        n += 1\n        return n\n"
           "    def get():\n        return n\n    return inc, get\n"
           "pair = make_counter()\n"
           "def outer():\n    def inner(n):\n        return 0 if n == 0 else inner(n - 1) + K\n    return inner\n"
           "rec = outer()\n"
           "def deco(fn):\n    @functools.wraps(fn)\n    def w(*a):\n        return fn(*a) + K\n    return w\n"
           "@deco\ndef decorated(x):\n    return x\n"
           "@functools.lru_cache(maxsize=None)\ndef cached(x):\n    return x + K\n"
           "def gen():\n    yield K\n"),
    ("c4", "def is_even(n):\n    return True if n == 0 else is_odd(n - 1)\n"
           "def is_odd(n):\n    return False if n == 0 else is_even(n - 1)\n"
           "def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)\n"),
    ("c5", "cbs = {'inc': lambda x: x + K}\nfs = [f, lambda: K]\n"),
    ("c6", "def r():\n    return math.sqrt(16) + K - K\n"),
    ("c7", "s = 'notebook-namespace'\ng = globals()\n"),
]


def _restore_into_fresh(s3, session):
    ex_b = SandboxExecutor()
    mgr_b = _manager(ex_b)
    with _Patched(s3):
        mgr_b.restore(session)
    return ex_b, mgr_b


def _run_roundtrip():
    print("\nRound trip: executor A saves, fresh executor B restores")
    ex_a = SandboxExecutor()
    for cid, code in CELLS:
        res = ex_a.execute(code, cid)
        check(f"cell {cid} ran", res.success)
    s3 = FakeS3()
    mgr_a = _manager(ex_a)
    with _Patched(s3):
        mgr_a.save("sess-fn")
    man = json.loads(s3.store["sessions/sess-fn/manifest.json"])["vars"]
    check("functions are in the manifest", {"f", "C", "c", "sq", "rec", "is_even", "fs", "r"} <= set(man))
    check("every function var is a small var (no own object)",
          all(man[n]["object"] == "vars/__small__.pkl" for n in ("f", "rec", "is_even", "r", "C", "c")))

    ex_b, mgr_b = _restore_into_fresh(s3, "sess-fn")
    ns = ex_b._namespace
    f = ns.get("f")
    # (a)
    check("(a) f(2) == 6 after restore", _try(lambda: f(2)) == 6)
    # (b) live globals (the staleness fix)
    check("(b) f.__globals__ is the live executor namespace", getattr(f, "__globals__", None) is ns)
    ex_b.execute("K = 10", "k10")
    check("(b) after K = 10 in B, f(2) == 20 (live, not a stale copy)", _try(lambda: f(2)) == 20)
    # (d) class + instance
    check("(d) C().m() sees the new K", _try(lambda: ns["C"]().m()) == 10)
    check("(d) staticmethod and property", _try(lambda: (ns["C"].s(), ns["C"]().p)) == (11, 12))
    check("(d) restored instance c.m() sees the new K", _try(lambda: ns["c"].m()) == 10)
    check("(d) method __globals__ is the live namespace", ns["C"].m.__globals__ is ns)
    # (e)
    check("(e) lambda", _try(lambda: ns["sq"](3)) == 19)
    # Both closures are in ONE variable: values are pickled per variable, so
    # sharing (here a nonlocal cell) is kept within a variable, not across two.
    inc, get = ns["pair"]
    check("(e) nonlocal cell still shared by both closures", _try(lambda: (inc(), inc(), get())) == (1, 2, 2))
    check("(e) self-recursive closure", _try(lambda: ns["rec"](3)) == 30)
    check("(e) functools.wraps decorator", _try(lambda: (ns["decorated"](1), ns["decorated"].__name__)) == (11, "decorated"))
    check("(e) lru_cache function", _try(lambda: ns["cached"](1)) == 11)
    check("(e) generator function", _try(lambda: next(ns["gen"]())) == 10)
    # (f)
    check("(f) mutual recursion is_even(10)/is_odd(7)", _try(lambda: (ns["is_even"](10), ns["is_odd"](7))) == (True, True))
    check("(f) recursion fact(5)", _try(lambda: ns["fact"](5)) == 120)
    # (g)
    check("(g) function in a dict sees live K", _try(lambda: ns["cbs"]["inc"](1)) == 11)
    check("(g) functions in a list see live K", _try(lambda: (ns["fs"][0](2), ns["fs"][1]())) == (20, 10))
    check("(g) list holds the same restored function object as cbs' globals",
          getattr(ns["fs"][0], "__globals__", None) is ns)
    # (h)
    check("(h) function using an imported module (ledger replay)", _try(lambda: ns["r"]()) == 4.0)
    res = ex_b.execute("print(f(1), r(), is_even(4))", "use")
    check("(h) restored functions callable from a cell", res.success and "10 4.0 True" in (res.output or ""))
    # (k)
    check("(k) no namespace value is the marker string except the user's own `s`",
          [k for k, v in ns.items() if isinstance(v, str) and v == nspickle.NAMESPACE_MARKER] == ["s"])
    check("(k) user string equal to the marker round-trips", ns.get("s") == "notebook-namespace")
    if "g" in man:
        check("(k) g = globals() restores as the live namespace itself", ns.get("g") is ns)
    else:
        check("(k) g = globals() is not saved (filtered) and absent after restore", "g" not in ns or ns["g"] is ns)
    check("(k) restore recorded nothing as not restored", mgr_b.last_restore_not_restored == [])
    return ex_a, s3


def _run_size_and_memory():
    print("\n(c) helper next to a 50 MiB array")
    import numpy as np
    ex = SandboxExecutor()
    ex._namespace["np"] = np
    ex._namespace["big"] = np.random.default_rng(1).random(50 * MiB // 8)
    res = ex.execute("def helper():\n    return 1\n", "h")
    check("cell ran", res.success)
    helper = ex._namespace["helper"]
    before = len(dill.dumps(helper, protocol=5))
    after = len(nspickle.dumps(helper, ex._namespace))
    print(f"    helper pickle: plain dill {before} B (before), marker pickler {after} B (after)")
    check("(c) helper pickle is under 4096 B", after < 4096)
    s3 = FakeS3()
    mgr = _manager(ex)
    mgr._mem_available = lambda: None
    tracemalloc.start()
    with _Patched(s3):
        mgr.save("sess-big")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    up = mgr.last_save_sizes.get("upload_pkl", 0)
    print(f"    upload_pkl {up} B, save peak (tracemalloc) {peak / MiB:.1f} MiB for a 50 MiB array")
    man = json.loads(s3.store["sessions/sess-big/manifest.json"])["vars"]
    check("(c) helper's manifest entry is under 4096 B", man["helper"]["size"] < 4096)
    check("(c) uploaded var bytes < 51 MiB (one copy of the array)", up < 51 * MiB)
    check("(c) save peak < 1.5 x the array (no second copy)", peak < 1.5 * 50 * MiB)
    ex_b, _ = _restore_into_fresh(s3, "sess-big")
    check("(c) helper() works after restore", _try(lambda: ex_b._namespace["helper"]()) == 1)
    check("(c) big restored", getattr(ex_b._namespace.get("big"), "nbytes", 0) == 50 * MiB)


def _run_delta_unchanged(ex_a, s3):
    print("\n(j) second save with no changes")
    mgr = _manager(ex_a)
    with _Patched(s3):
        mgr.save("sess-j")
        m1 = json.loads(s3.store["sessions/sess-j/manifest.json"])
        puts_before = len(s3.put_keys)
        mgr.save("sess-j")
        m2 = json.loads(s3.store["sessions/sess-j/manifest.json"])
    var_puts = [k for k in s3.put_keys[puts_before:] if "/vars/" in k]
    check("(j) second save uploads 0 var bytes", mgr.last_save_sizes.get("upload_pkl") == 0)
    check("(j) no vars/ object written by the second save", var_puts == [])
    check("(j) same manifest hashes, functions included",
          {n: v["hash"] for n, v in m1["vars"].items()} == {n: v["hash"] for n, v in m2["vars"].items()}
          and "f" in m2["vars"])


def _run_old_format():
    print("\n(i) pre-change pickles")
    ex_a = SandboxExecutor()
    ex_a.execute("K = 3\ndef old_fn(x):\n    return x + K\nkeep = 5\n", "o1")
    s3 = FakeS3()
    mgr_a = _manager(ex_a)
    with _Patched(s3):
        mgr_a.save("sess-old")
    old_bytes = dill.dumps(ex_a._namespace["old_fn"], protocol=5)  # embedded globals (pre-change format)

    class _Other(dill.Pickler):
        def persistent_id(self, obj):
            return "other" if obj == "x-marker" else None
    buf = io.BytesIO()
    _Other(buf, protocol=5).dump(["x-marker"])
    bad_bytes = buf.getvalue()
    key = "sessions/sess-old/manifest.json"
    man = json.loads(s3.store[key])
    for name, b in (("old_fn", old_bytes), ("bad", bad_bytes)):
        s3.store[f"sessions/sess-old/vars/{name}.pkl"] = b
        man["vars"][name] = {"hash": "x", "size": len(b), "object": f"vars/{name}.pkl"}
    s3.store[key] = json.dumps(man).encode()
    try:
        ex_b, mgr_b = _restore_into_fresh(s3, "sess-old")
        ok = True
    except Exception as e:
        ok, ex_b, mgr_b = False, None, None
        print(f"    restore raised {type(e).__name__}: {e}")
    check("(i) restore does not raise", ok)
    if not ok:
        return
    ns = ex_b._namespace
    check("(i) the pre-change function loads and runs", _try(lambda: ns["old_fn"](1)) == 4)
    check("(i) the other vars restored", ns.get("keep") == 5)
    check("(i) the bad var is absent", "bad" not in ns)
    check("(i) last_restore_not_restored names it",
          [i["name"] for i in mgr_b.last_restore_not_restored] == ["bad"])
    rep = mgr_b.last_restore_report or {}
    check("(i) restore report lists vars_not_restored",
          [i.get("name") for i in rep.get("vars_not_restored", [])] == ["bad"])


def _run_marker_guards():
    print("\n(k) marker guards")
    ns = {}
    exec("Z = 1\ndef z():\n    return Z\n", ns)
    b = nspickle.dumps(ns["z"], ns)
    try:
        dill.loads(b)
        plain = "loaded"
    except pickle.UnpicklingError:
        plain = "UnpicklingError"
    except Exception as e:
        plain = type(e).__name__
    check("(k) plain dill.loads of a marker pickle raises UnpicklingError", plain == "UnpicklingError")
    try:
        nspickle.NamespaceUnpickler(io.BytesIO(b), None).load()
        none_ns = "loaded"
    except pickle.UnpicklingError:
        none_ns = "UnpicklingError"
    check("(k) the marker without a namespace raises UnpicklingError", none_ns == "UnpicklingError")
    target = {"Z": 7}
    check("(k) marker binds to the given namespace", nspickle.loads(b, target)() == 7)
    check("(k) nothing global patched: plain pickle carries no marker",
          b"notebook-namespace" not in pickle.dumps({"a": 1}) and b"notebook-namespace" not in dill.dumps({"a": 1}))
    data = {"a": [1, 2, 3], "s": "x"}
    check("(k) data values pickle to the same bytes as plain dill (hashes unchanged)",
          nspickle.dumps(data, ns) == dill.dumps(data, protocol=5))
    check("nspickle protocol matches delta.PICKLE_PROTOCOL", nspickle._PROTOCOL == delta_mod.PICKLE_PROTOCOL)


def _run():
    ex_a, s3 = _run_roundtrip()
    _run_size_and_memory()
    _run_delta_unchanged(ex_a, FakeS3())
    _run_old_format()
    _run_marker_guards()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
