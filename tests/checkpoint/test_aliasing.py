#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: aliases between top-level variables and shared closure cells
survive checkpoint/restore. A list, dict, set, numpy array, pandas object or
notebook-class instance bound at top level is pickled by value only under its
canonical name; every other variable that reaches it writes
("mvm-ns-value", name). A closure cell shared by two top-level functions is
owned by the first function (sorted by name); the other writes
("mvm-ns-cell", "owner#i"), resolved as owner.__closure__[i]
(app/platform/checkpoint/nspickle.py). The restore adopts the unpickled values
without the old per-variable deepcopy (namespace_filter), so the shared
objects stay shared.
Drives the real SandboxExecutor + CheckpointManager.save()/restore() against the
in-memory FakeS3 of test_dag_checkpoint.py (no AWS, no MicroVM). Executor A runs
the cells and saves; a fresh executor B restores; the same mutation and probe
run in A (no checkpoint, the expected result) and in B. Cases:
  (1)-(9) the aliasing cases of .agents/tasks/class-identity/aliasing.md:
      `b = a`, a dict held in another dict, `df2 = df`, a numpy view (not
      covered, values still restore), an instance attribute holding a
      top-level list, a bound method, a shared closure cell, a closure over a
      top-level list, two closures in one variable; plus (10) an alias inside
      one variable, (11) a data cycle and (12) a class/value cycle, which keep
      identity on one side and are reported as by_value;
  (c) closure cells: the owner is the first function by name, the other
      references it; an owner that fails to restore fails both, and both are
      in last_restore_not_restored; an owner that is not saved leaves the
      other function with its own copy of the cell;
  (v) values: an owner that fails to restore fails its referrers; rebinding
      a name; a notebook-class instance as a dict key; an Enum member (not a
      value reference); a class holding its own instance (not saved, as
      before: dill cannot pickle it);
      order independence (manifest reversed, refs removed);
  (s) singletons: a numpy dtype, a logger and pd.NaT bound at top level are
      never references, and deleting them re-uploads nothing large;
  (d) sizes and delta: one DataFrame under 4 names is uploaded once; an
      in-place edit re-uploads only it; adding and deleting an alias keeps the
      canonical name (sticky) and uploads only small bytes; the restore holds
      one copy; the first save after the restore uploads 0 bytes;
  (n) the restore no longer copies values (_sanitize_restored_namespace keeps
      identity and still drops live runtime objects); marker guards.
Run:
    PYTHONPATH=. .venv/bin/python tests/checkpoint/test_aliasing.py
"""
import gc
import json
import logging
import os
import pickle
import io
import sys
import tempfile
import tracemalloc
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CHECKPOINT_SPOOL_DIR"] = tempfile.mkdtemp(prefix="alias-spool-")
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.platform.checkpoint import nspickle  # noqa: E402
from app.platform.checkpoint import manager as manager_mod  # noqa: E402
from app.platform.checkpoint.namespace_filter import _sanitize_restored_namespace  # noqa: E402
from test_dag_checkpoint import FakeS3, _Patched, _manager  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _try(fn):
    try:
        return fn()
    except Exception as e:
        return f"<{type(e).__name__}: {e}>"


def _run(ex, code, cid="c"):
    r = ex.execute(code, cid)
    if not r.success:
        raise RuntimeError(f"cell {cid} failed: {getattr(r, 'error', r)}")


def _save(ex, s3, session, mgr=None):
    mgr = mgr or _manager(ex)
    with _Patched(s3):
        mgr.save(session)
    return mgr


def _restore(s3, session):
    ex = SandboxExecutor()
    mgr = _manager(ex)
    with _Patched(s3):
        mgr.restore(session)
    return ex, mgr


def _manifest(s3, session):
    return json.loads(s3.store[f"sessions/{session}/manifest.json"])


def _probe(ex, expr):
    r = ex.execute(f"__probe__ = {expr}", "probe")
    if not r.success:
        return f"<error: {getattr(r, 'error', r)}>"
    return ex._namespace.pop("__probe__", None)


def _upload(mgr):
    return (mgr.last_save_sizes or {}).get("upload_pkl")


COUNTER = ("def make_counter():\n    n = 0\n    def inc():\n        nonlocal n\n        n += 1\n        return n\n"
           "    def get():\n        return n\n    return inc, get\n")

CASES = [
    ("1 list alias b = a", ["a = [1, 2, 3]\nb = a\n"],
     "b.append(4)", "(a is b, len(a))", True),
    ("2 dict held inside another variable", ["cfg = {'lr': 0.1}\nrun = {'cfg': cfg, 'epochs': 3}\n"],
     "cfg['lr'] = 0.5", "(run['cfg'] is cfg, run['cfg']['lr'])", True),
    ("3 DataFrame alias df2 = df", ["import pandas as pd\ndf = pd.DataFrame({'x': range(5)})\ndf2 = df\n"],
     "df2.loc[0, 'x'] = 99", "(df2 is df, int(df.loc[0, 'x']))", True),
    ("5 instance attribute holds a top-level list",
     ["class M:\n    pass\ndata = [1, 2]\nm = M()\nm.data = data\n"],
     "data.append(3)", "(m.data is data, len(m.data))", True),
    ("6 bound method add = acc.add",
     ["class Acc:\n    def __init__(self):\n        self.n = 0\n    def add(self):\n        self.n += 1\n"
      "acc = Acc()\nadd = acc.add\n"],
     "add()", "(add.__self__ is acc, acc.n)", True),
    ("7 shared closure cell inc, get = make_counter()", [COUNTER + "inc, get = make_counter()\ninc()\n"],
     "inc()", "(get(), inc.__closure__[0] is get.__closure__[0])", True),
    ("8 closure over a top-level list", ["data = [1]\ndef mk(d):\n    return lambda: len(d)\nf = mk(data)\n"],
     "data.append(2)", "f()", True),
    ("9 control: two closures in one variable", [COUNTER + "fns = make_counter()\nfns[0]()\n"],
     "fns[0]()", "fns[1]()", True),
    ("10 control: alias inside one variable", ["pair = [[1]] * 2\n"],
     "pair[0].append(2)", "(pair[0] is pair[1], pair[1])", True),
    # Not covered by design: a view is another object sharing a buffer. The
    # values still restore; only the sharing is lost (as before).
    ("4 numpy view v = arr[2:5] (not covered)", ["import numpy as np\narr = np.arange(10)\nv = arr[2:5]\n"],
     "pass", "(arr.tolist(), v.tolist())", True),
]


def _case(title, cells, mutate, expr):
    ex_a = SandboxExecutor()
    for i, c in enumerate(cells):
        _run(ex_a, c, f"c{i}")
    s3 = FakeS3()
    mgr_a = _save(ex_a, s3, "s")
    ex_b, mgr_b = _restore(s3, "s")
    for ex in (ex_a, ex_b):
        _run(ex, mutate, "mut")
    return _probe(ex_a, expr), _probe(ex_b, expr), _manifest(s3, "s")["vars"], mgr_a, mgr_b


def _run_cases():
    print("\n(1)-(10) aliasing cases: no checkpoint vs after restore")
    for title, cells, mutate, expr, _ in CASES:
        truth, got, man, mgr_a, mgr_b = _case(title, cells, mutate, expr)
        print(f"    {title}: expected {truth!r}, after restore {got!r}; "
              + ", ".join(f"{n}={m['size']}B" + (f" refs={m['refs']}" if m.get("refs") else "")
                          for n, m in sorted(man.items())))
        check(f"({title}) restored result equals the no-checkpoint result", truth == got)
        check(f"({title}) nothing not saved or not restored",
              not mgr_a.last_not_saved and not mgr_b.last_restore_not_restored)
        if title.startswith("1 "):
            check("(1) b is an 82-byte-scale reference to a", man["b"].get("refs") == ["a"] and man["b"]["size"] < 120)
        if title.startswith("3 "):
            check("(3) df2 is a reference to df", man["df2"].get("refs") == ["df"] and man["df2"]["size"] < 200)
        if title.startswith("6 "):
            check("(6) add references acc", man["add"].get("refs") == ["acc"])
        if title.startswith("7 "):
            check("(7) get owns the cell, inc references get",
                  man["inc"].get("refs") == ["get"] and not man["get"].get("refs"))
        if title.startswith("8 "):
            check("(8) f references data", "data" in (man["f"].get("refs") or ()))

    print("\n(11)-(12) cycles keep identity on one side and are reported")
    truth, got, man, _, mgr_b = _case("11", ["a = []\nb = [a]\na.append(b)\n"], "pass", "(a[0] is b, b[0] is a)")
    check("(11) data cycle: one side keeps identity", got == (True, False))
    check("(11) the by-value copy is reported", mgr_b.last_restore_by_value == [{"name": "b", "by_value": ["a"]}])
    truth, got, man, mgr_a, mgr_b = _case(
        "12", ["registry = []\nclass P:\n    reg = registry\nregistry.append(P())\n"],
        "pass", "(P.reg is registry, type(registry[0]).__name__)")
    check("(12) class/value cycle: both saved and restored, P.reg is registry",
          not mgr_a.last_not_saved and not mgr_b.last_restore_not_restored and got == (True, "P"))
    check("(12) the by-value copy is reported", bool(mgr_b.last_restore_by_value))


def _corrupt_small(s3, session, name):
    key = f"sessions/{session}/vars/__small__.pkl"
    fmt, d = manager_mod._decode_small_batch(s3.store[key])
    assert fmt == manager_mod.SMALL_BATCH_FORMAT and name in d
    d[name] = b"\x80\x05not a pickle"
    s3.store[key] = manager_mod._encode_small_batch(d)


def _run_cells():
    print("\n(c) shared closure cells")
    ex = SandboxExecutor()
    _run(ex, COUNTER + "inc, get = make_counter()\ninc()\n")
    ns = ex._namespace
    rm = nspickle.build_ref_map({k: ns[k] for k in ("inc", "get", "make_counter")}, ns)
    check("(c) the cell is owned by the first function by name: get#0",
          rm.lookup(ns["inc"].__closure__[0]) == (nspickle.CELL_MARKER, "get#0"))
    b = nspickle.dumps(ns["inc"], ns, refs=rm, self_name="inc")
    check("(c) inc's pickle holds the cell id", b"mvm-ns-cell" in b and b"get#0" in b)
    s3 = FakeS3()
    _save(ex, s3, "c")
    ex_b, mgr_b = _restore(s3, "c")
    nb = ex_b._namespace
    check("(c) after restore inc and get share one cell", nb["inc"].__closure__[0] is nb["get"].__closure__[0])
    _run(ex_b, "inc()\ninc()")
    check("(c) get() sees inc()'s updates", _probe(ex_b, "get()") == 3)

    print("\n(c) the owner function fails to restore")
    s3 = FakeS3()
    _save(ex, s3, "c2")
    _corrupt_small(s3, "c2", "get")
    ex_c, mgr_c = _restore(s3, "c2")
    nr = {i["name"]: i["reason"] for i in mgr_c.last_restore_not_restored}
    print(f"    not restored: {mgr_c.last_restore_not_restored}")
    check("(c) get and inc are both reported not restored", sorted(nr) == ["get", "inc"])
    check("(c) inc's reason names function 'get'", "function 'get'" in nr.get("inc", ""))
    check("(c) the rest restores", callable(ex_c._namespace.get("make_counter")))

    print("\n(c) the owner function is not saved")
    ex_d = SandboxExecutor()
    _run(ex_d, COUNTER + "inc, get = make_counter()\ninc()\nget.gen = (i for i in [1])\n")
    s3 = FakeS3()
    mgr_d = _save(ex_d, s3, "c3")
    man = _manifest(s3, "c3")["vars"]
    check("(c) get is not saved", [i["name"] for i in mgr_d.last_not_saved] == ["get"])
    check("(c) inc is saved with its own copy of the cell (no reference to get)",
          "inc" in man and not man["inc"].get("refs"))
    ex_e, mgr_e = _restore(s3, "c3")
    check("(c) inc restores and keeps counting", _try(lambda: ex_e._namespace["inc"]()) == 2
          and not mgr_e.last_restore_not_restored)

    print("\n(c) a cell id guard")
    for label, pid in (("malformed cell id", (nspickle.CELL_MARKER, "nohash")),):
        buf = io.BytesIO()
        p = pickle.Pickler(buf, protocol=5)
        p.persistent_id = lambda o, _pid=pid: _pid if o == "X" else None
        p.dump(["X"])
        r = _try(lambda: nspickle.loads(buf.getvalue(), {}))
        check(f"(c) {label} raises UnpicklingError", isinstance(r, str) and "UnpicklingError" in r)
    buf = io.BytesIO()
    p = pickle.Pickler(buf, protocol=5)
    p.persistent_id = lambda o: (nspickle.CELL_MARKER, "f#3") if o == "X" else None
    p.dump(["X"])
    target = {}
    exec("def f():\n    return 1\n", target)
    r = _try(lambda: nspickle.loads(buf.getvalue(), target))
    check("(c) an out-of-range cell index raises MissingReference naming the function",
          isinstance(r, str) and "MissingReference" in r and "function 'f'" in r)


def _run_values():
    print("\n(v) an owner value that fails to restore")
    ex = SandboxExecutor()
    _run(ex, "a = [1, 2]\nb = a\nkeep = 5\n")
    s3 = FakeS3()
    _save(ex, s3, "v")
    _corrupt_small(s3, "v", "a")
    ex_b, mgr_b = _restore(s3, "v")
    nr = {i["name"]: i["reason"] for i in mgr_b.last_restore_not_restored}
    check("(v) a and b are reported not restored, keep is restored",
          sorted(nr) == ["a", "b"] and ex_b._namespace.get("keep") == 5)
    check("(v) b's reason names variable 'a'", "variable 'a'" in nr.get("b", ""))

    print("\n(v) rebinding a name after a save")
    ex = SandboxExecutor()
    _run(ex, "a = [1]\nb = a\n")
    s3 = FakeS3()
    mgr = _save(ex, s3, "r")
    _run(ex, "a = [2]")
    _save(ex, s3, "r", mgr)
    ex_b, _ = _restore(s3, "r")
    check("(v) b keeps the old list, a the new one", _probe(ex_b, "(a, b, a is b)") == ([2], [1], False))

    print("\n(v) dict keys, Enum members, a class holding its own instance")
    ex = SandboxExecutor()
    _run(ex, "import enum\nclass K:\n    pass\nk = K()\nd = {k: 1}\n"
             "class Color(enum.Enum):\n    RED = 1\nr = Color.RED\nholder = {'c': Color.RED}\n"
             "class Cfg:\n    inst = None\nCfg.inst = Cfg()\ncfg = Cfg.inst\n")
    s3 = FakeS3()
    mgr = _save(ex, s3, "k")
    man = _manifest(s3, "k")["vars"]
    # dill cannot pickle a class whose class attribute holds its own instance,
    # with or without value references ("Can't pickle <class 'Cfg'>" before):
    # Cfg and cfg stay not saved, as before, and nothing else is affected.
    check("(v) only Cfg and cfg are not saved (as before value references)",
          sorted(i["name"] for i in mgr.last_not_saved) == ["Cfg", "cfg"])
    check("(v) an Enum member is not a value reference", "r" not in (man["holder"].get("refs") or ()))
    ex_b, mgr_b = _restore(s3, "k")
    check("(v) nothing not restored", mgr_b.last_restore_not_restored == [])
    check("(v) the dict key is the restored instance", _probe(ex_b, "d[k]") == 1)
    check("(v) Enum members keep identity", _probe(ex_b, "(r is Color.RED, holder['c'] is Color.RED)") == (True, True))


    print("\n(v) order independence")
    ex = SandboxExecutor()
    _run(ex, "import pandas as pd\nclass M:\n    pass\ncfg = {'lr': 0.1}\nrun = {'cfg': cfg}\n"
             "df = pd.DataFrame({'x': [1, 2]})\nm = M()\nm.df = df\nm.run = run\nzz = [m, df]\n")
    s3 = FakeS3()
    _save(ex, s3, "o")
    key = "sessions/o/manifest.json"
    orig = s3.store[key]
    expr = "(run['cfg'] is cfg, m.df is df, m.run is run, zz[0] is m, zz[1] is df)"
    for label, edit in (("manifest in reverse order", lambda v: dict(reversed(list(v.items())))),
                        ("refs removed", lambda v: {n: {k: x for k, x in e.items() if k != "refs"}
                                                    for n, e in v.items()})):
        doc = json.loads(orig)
        doc["vars"] = edit(doc["vars"])
        s3.store[key] = json.dumps(doc).encode()
        ex_b, mgr_b = _restore(s3, "o")
        check(f"(v) {label}: identities hold", _probe(ex_b, expr) == (True,) * 5
              and mgr_b.last_restore_not_restored == [])
    s3.store[key] = orig


def _run_singletons():
    print("\n(s) library singletons bound at top level")
    ex = SandboxExecutor()
    _run(ex, "import numpy as np\nimport pandas as pd\nimport logging\n"
             "dt = np.dtype('float64')\nlog = logging.getLogger('nb')\nnat = pd.NaT\n"
             "arr = np.zeros(100_000)\ndf = pd.DataFrame({'a': np.zeros(100_000), 't': [pd.NaT] * 100_000})\n"
             "holder = {'log': log, 'dt': dt}\n")
    ns = ex._namespace
    for n in ("dt", "log", "nat"):
        check(f"(s) {n} is not aliasable", not nspickle.aliasable(ns[n], ns))
    s3 = FakeS3()
    mgr = _save(ex, s3, "sg")
    man = _manifest(s3, "sg")["vars"]
    check("(s) arr, df and holder reference nothing", not any(man[n].get("refs") for n in ("arr", "df", "holder")))
    s3.put_keys.clear()
    _run(ex, "del dt\ndel nat")
    _save(ex, s3, "sg", mgr)
    large = [k for k in s3.put_keys if "/vars/" in k and not k.endswith("__small__.pkl")]
    check("(s) deleting dt and nat re-uploads no large variable", large == [])
    ex_b, _ = _restore(s3, "sg")
    # (Unpickling a numpy array builds a new dtype object; `is` was False
    # before value references too.)
    check("(s) arr and df restore with their float64 dtype",
          _probe(ex_b, "(arr.dtype == np.dtype('float64'), len(arr), str(df['a'].dtype))") == (True, 100_000, "float64"))


TOY = ("import numpy as np\nimport pandas as pd\n"
       "df = pd.DataFrame({'a': np.arange(250_000, dtype='float64'), 'b': np.ones(250_000)})\n"
       "raw = df\ncfg = {'data': df, 'lr': 0.1}\nclass Model:\n    pass\nmodel = Model()\nmodel.train = df\n")
DF_BYTES = 4_000_000


def _run_sizes():
    print("\n(d) sizes and delta: one 4 MB DataFrame as df, raw, cfg['data'], model.train")
    ex = SandboxExecutor()
    _run(ex, TOY)
    s3 = FakeS3()
    mgr = _save(ex, s3, "d")
    man = _manifest(s3, "d")["vars"]
    up1 = _upload(mgr)
    print(f"    save 1: uploaded {up1} B; " + ", ".join(f"{n}={m['size']}B" for n, m in sorted(man.items())))
    check("(d) save 1 uploads the DataFrame once", DF_BYTES <= up1 < DF_BYTES * 1.1)
    check("(d) raw, cfg and model reference df", all(man[n].get("refs") and "df" in man[n]["refs"]
                                                    for n in ("raw", "cfg", "model")))
    s3.put_keys.clear()
    _run(ex, "df.loc[0, 'a'] = -1.0")
    _save(ex, s3, "d", mgr)
    keys = sorted(k.split("/vars/", 1)[1] for k in s3.put_keys if "/vars/" in k)
    print(f"    save 2 (in-place edit): uploaded {_upload(mgr)} B, objects {keys}")
    check("(d) an in-place edit re-uploads only df", DF_BYTES <= _upload(mgr) < DF_BYTES * 1.1
          and all(k.startswith("df.") for k in keys))
    _run(ex, "aaa = df")
    _save(ex, s3, "d", mgr)
    man = _manifest(s3, "d")["vars"]
    print(f"    save 3 (aaa = df): uploaded {_upload(mgr)} B; aaa refs {man['aaa'].get('refs')}")
    check("(d) a new alias keeps df canonical (sticky) and uploads only a reference",
          _upload(mgr) < 10_000 and man["aaa"].get("refs") == ["df"] and not man["df"].get("refs"))
    _run(ex, "del aaa")
    _save(ex, s3, "d", mgr)
    print(f"    save 4 (del aaa): uploaded {_upload(mgr)} B")
    check("(d) deleting the alias uploads no large object", _upload(mgr) < 10_000)

    del ex
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    ex_b, mgr_b = _restore(s3, "d")
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    held, peak = cur - base, peak - base
    print(f"    restore: held {held} B, peak {peak} B")
    check("(d) the restore holds one copy of the DataFrame", held < DF_BYTES * 1.5)
    check("(d) the restore peak stays under 2 copies", peak < DF_BYTES * 2)
    check("(d) identities after restore",
          _probe(ex_b, "(raw is df, cfg['data'] is df, model.train is df, float(df.loc[0, 'a']))")
          == (True, True, True, -1.0))
    before = _manifest(s3, "d")["vars"]
    s3.put_keys.clear()
    _save(ex_b, s3, "d", mgr_b)
    after = _manifest(s3, "d")["vars"]
    changed = sorted(n for n in after if after[n]["hash"] != before.get(n, {}).get("hash"))
    large = [k for k in s3.put_keys if "/vars/" in k and not k.endswith("__small__.pkl")]
    print(f"    first save after restore, no edits: uploaded {_upload(mgr_b)} B, changed {changed}")
    # A class re-pickles to new bytes after a restore (class-identity work,
    # unchanged here); every data value keeps its hash.
    check("(d) the first save after the restore re-uploads no data (only the class Model changes)",
          large == [] and changed in ([], ["Model"]) and _upload(mgr_b) < 10_000)


def _run_no_copy():
    print("\n(n) restore adopts values without copying them")
    import threading
    shared = [1]
    ns = {"a": shared, "b": {"x": shared}, "lk": threading.Lock(), "futs": [threading.Lock()]}
    out, dropped = _sanitize_restored_namespace(ns, live_namespace={})
    check("(n) a and b are adopted as is", out["a"] is shared and out["b"]["x"] is shared)
    check("(n) live runtime objects are still dropped", "lk" not in out and "futs" not in out
          and sorted(d.split("(")[0] for d in dropped) == ["futs", "lk"])
    live = {}
    exec("x = 1", live)
    check("(n) globals() is never a value reference", not nspickle.aliasable(live, live)
          and id(live) not in nspickle.build_ref_map({"g": live}, live).by_id)


def main():
    logging.disable(logging.WARNING)
    print(f"python {sys.version.split()[0]}")
    _run_cases()
    _run_cells()
    _run_values()
    _run_singletons()
    _run_sizes()
    _run_no_copy()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        for n, ok in _checks:
            if not ok:
                print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
