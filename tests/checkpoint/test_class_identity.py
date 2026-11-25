#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: class identity across checkpoint/restore. A class (or function)
bound at top level in the notebook is pickled by value only in its own
variable; every other variable references it by name ("mvm-ns-class" /
"mvm-ns-function", app/platform/checkpoint/nspickle.py), and the restore loads
the referenced variables first (manifest "refs"), so restored instances are
instances of the restored class.
Drives the real SandboxExecutor + CheckpointManager.save()/restore() against the
in-memory FakeS3 of test_dag_checkpoint.py (no AWS, no MicroVM). Executor A runs
the cells and saves; a fresh executor B restores. Cases:
  (a) type(c) is C, isinstance, subclass checks, multi-level inheritance,
      super()/cls/classmethod/staticmethod/property, __slots__, metaclass;
  (b) class-level state (counter, registry list) is shared by the class and
      every restored instance, and later instances update it;
  (c) dataclass (defaults, default_factory, frozen, fields), Enum identity
      (`color is Color.RED`), namedtuple, exception class and instance;
  (d) instances in a list and a dict, an alias `Alias = C`, a function in a
      dict (`cbs['cb'] is helper`), a function default holding a class;
  (e) a local class and a deleted class stay by value (and still restore);
  (f) sizes: an instance of a class with a 2 MB class attribute is a few
      bytes, the class itself carries the attribute;
  (g) re-running the class cell: the class variable is uploaded again, the
      instance variables are not, and after restore they use the new class;
  (h) order independence: manifest vars in reverse order, and with "refs"
      removed (on-demand loading);
  (i) a referenced class that is missing or not saved: the restore does not
      raise and names the variables it could not restore; an unpicklable
      class attribute is reported as not saved;
  (j) a reference cycle between two functions is broken (one embeds the
      other by value) and reported; a cycle through a class, which dill
      cannot pickle by value, is reported as not saved (as before); old
      checkpoints (classes by value) still restore;
  (k) marker guards: unknown by-name ids raise UnpicklingError, a plain
      dill.loads of a by-name pickle fails, a second save uploads nothing,
      and model export (joblib) carries no marker.
Run:
    PYTHONPATH=. .venv/bin/python tests/checkpoint/test_class_identity.py
"""
import io
import json
import logging
import os
import pickle
import sys
import tempfile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CHECKPOINT_SPOOL_DIR"] = tempfile.mkdtemp(prefix="clsid-spool-")
import dill  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.platform.checkpoint import nspickle  # noqa: E402
from test_dag_checkpoint import FakeS3, _Patched, _manager  # noqa: E402

_checks = []
BIG = 2_000_000


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _try(fn):
    try:
        return fn()
    except Exception as e:
        return f"<{type(e).__name__}: {e}>"


CLASS_C = ("class C(Base):\n"
           f"    big = bytes({BIG})\n"
           "    def who(self):\n        return 'C:' + super().who()\n"
           "    @classmethod\n    def make(cls):\n        return cls()\n"
           "    @staticmethod\n    def st():\n        return 7\n"
           "    @property\n    def p(self):\n        return 3\n")
CLASS_C2 = CLASS_C.replace("'C:'", "'C2:'")

CELLS = [
    ("imp", "import dataclasses\nimport enum\nimport collections\n"),
    ("base", "class Base:\n    count = 0\n    registry = []\n"
             "    def __init__(self):\n        Base.count += 1\n        Base.registry.append(type(self).__name__)\n"
             "    def who(self):\n        return 'base'\n"),
    ("clsC", CLASS_C),
    ("rest",
     "class D(C):\n    def who(self):\n        return 'D:' + super().who()\n"
     "class S:\n    __slots__ = ('a', 'b')\n    def __init__(self):\n        self.a = 1\n        self.b = 2\n"
     "@dataclasses.dataclass\nclass P:\n    x: int = 1\n    tags: list = dataclasses.field(default_factory=list)\n"
     "@dataclasses.dataclass(frozen=True)\nclass F:\n    z: int = 5\n"
     "class Color(enum.Enum):\n    RED = 1\n    GREEN = 2\n    def label(self):\n        return self.name.lower()\n"
     "NT = collections.namedtuple('NT', 'a b')\n"
     "class MyErr(Exception):\n    pass\n"
     "class Meta(type):\n    tag = 'meta'\n"
     "class WithMeta(metaclass=Meta):\n    pass\n"
     "Alias = C\n"
     "def helper(x):\n    return x + 1\n"
     "def factory(kind=C):\n    return kind()\n"
     "def make_local():\n    class Local:\n        v = 1\n    return Local()\n"
     "class Gone:\n    w = 2\n"),
    ("inst",
     "c = C()\nd = D()\nc2 = C.make()\ns = S()\np = P(tags=['a'])\nfz = F()\n"
     "color = Color.RED\npc = {'color': Color.GREEN}\nnt = NT(1, 2)\nerr = MyErr('boom')\nwm = WithMeta()\n"
     "lst = [C(), D()]\ndct = {'a': C(), 'b': d}\ncbs = {'cb': helper}\n"
     "loc = make_local()\ngone = Gone()\ndel Gone\n"),
]


def _restore_into_fresh(s3, session):
    ex_b = SandboxExecutor()
    mgr_b = _manager(ex_b)
    with _Patched(s3):
        mgr_b.restore(session)
    return ex_b, mgr_b


def _save(ex, s3, session):
    mgr = _manager(ex)
    with _Patched(s3):
        mgr.save(session)
    return mgr


def _manifest(s3, session):
    return json.loads(s3.store[f"sessions/{session}/manifest.json"])


def _run_roundtrip():
    print("\nRound trip: executor A saves, fresh executor B restores")
    ex_a = SandboxExecutor()
    for cid, code in CELLS:
        res = ex_a.execute(code, cid)
        check(f"cell {cid} ran", res.success)
    count_a = ex_a._namespace["Base"].count
    s3 = FakeS3()
    _save(ex_a, s3, "sess-cls")
    man = _manifest(s3, "sess-cls")["vars"]
    ex_b, mgr_b = _restore_into_fresh(s3, "sess-cls")
    ns = ex_b._namespace
    C, D, Base = ns.get("C"), ns.get("D"), ns.get("Base")
    c, d = ns.get("c"), ns.get("d")
    count_b = _try(lambda: Base.count)   # before the checks below create instances
    check("restore recorded nothing as not restored", mgr_b.last_restore_not_restored == [])
    # (a)
    check("(a) type(c) is C", type(c) is C)
    check("(a) isinstance(c, C) and isinstance(c, Base)", _try(lambda: isinstance(c, C) and isinstance(c, Base)) is True)
    check("(a) type(d) is D, isinstance(d, C)", type(d) is D and _try(lambda: isinstance(d, C)) is True)
    check("(a) issubclass(D, C) and issubclass(C, Base); D.__bases__[0] is C",
          _try(lambda: issubclass(D, C) and issubclass(C, Base) and D.__bases__[0] is C) is True)
    check("(a) super() chain: d.who() == 'D:C:base'", _try(lambda: d.who()) == "D:C:base")
    check("(a) classmethod returns the restored class", _try(lambda: type(C.make()) is C) is True)
    check("(a) type(c2) is C (made by the classmethod)", type(ns.get("c2")) is C)
    check("(a) staticmethod and property", _try(lambda: (c.st(), c.p)) == (7, 3))
    check("(a) __slots__ instance", _try(lambda: (type(ns["s"]) is ns["S"], ns["s"].a, ns["s"].b)) == (True, 1, 2))
    check("(a) metaclass: type(WithMeta) is Meta, type(wm) is WithMeta",
          _try(lambda: type(ns["WithMeta"]) is ns["Meta"] and type(ns["wm"]) is ns["WithMeta"]) is True)
    # (b)
    check("(b) Base.count kept its value", count_b == count_a)
    check("(b) c.count is C.count is Base.count", _try(lambda: c.count is C.count is Base.count) is True)
    check("(b) c.registry is Base.registry (one list)", _try(lambda: c.registry is Base.registry is d.registry) is True)
    n0 = _try(lambda: (Base.count, len(Base.registry)))
    res = ex_b.execute("e = C()\nn_after = Base.count\n", "new")
    check("(b) a new instance in B updates the shared counter seen by restored instances",
          res.success and _try(lambda: (ns["n_after"], c.count, d.count)) == (n0[0] + 1,) * 3)
    check("(b) the shared registry grew by one", _try(lambda: len(c.registry)) == n0[1] + 1)
    # (c)
    P, F, Color = ns.get("P"), ns.get("F"), ns.get("Color")
    import dataclasses
    check("(c) dataclass: type(p) is P, fields, values",
          _try(lambda: (type(ns["p"]) is P, [f.name for f in dataclasses.fields(ns["p"])], ns["p"].tags))
          == (True, ["x", "tags"], ["a"]))
    check("(c) dataclass default_factory gives a new list", _try(lambda: P().tags == [] and P().tags is not P().tags) is True)
    check("(c) dataclass equality with a new instance", _try(lambda: P(x=1, tags=["a"]) == ns["p"]) is True)
    check("(c) frozen dataclass: type(fz) is F and assignment raises FrozenInstanceError",
          type(ns.get("fz")) is F and "FrozenInstanceError" in str(_try(lambda: setattr(ns["fz"], "z", 1))))
    check("(c) Enum: color is Color.RED", ns.get("color") is _try(lambda: Color.RED))
    check("(c) Enum: Color(1) is color, member method", _try(lambda: (Color(1) is ns["color"], ns["color"].label())) == (True, "red"))
    check("(c) Enum in a dict: pc['color'] is Color.GREEN", _try(lambda: ns["pc"]["color"] is Color.GREEN) is True)
    check("(c) namedtuple: type(nt) is NT", _try(lambda: (type(ns["nt"]) is ns["NT"], ns["nt"].a, ns["nt"]._fields)) == (True, 1, ("a", "b")))
    res = ex_b.execute("try:\n    raise err\nexcept MyErr as x:\n    caught = str(x)\n", "exc")
    check("(c) exception instance is a MyErr and is caught by `except MyErr`",
          res.success and ns.get("caught") == "boom" and isinstance(ns.get("err"), ns.get("MyErr")))
    # (d)
    check("(d) list of instances: types are C and D",
          _try(lambda: [type(x) for x in ns["lst"]] == [C, D]) is True)
    check("(d) dict of instances: type(dct['a']) is C, isinstance(dct['b'], D)",
          _try(lambda: type(ns["dct"]["a"]) is C and isinstance(ns["dct"]["b"], D)) is True)
    check("(d) alias: Alias is C", ns.get("Alias") is C)
    check("(d) function by name: cbs['cb'] is helper", _try(lambda: ns["cbs"]["cb"] is ns["helper"]) is True)
    check("(d) function default holding a class: type(factory()) is C", _try(lambda: type(ns["factory"]()) is C) is True)
    # (e)
    check("(e) local class stays by value and restores", _try(lambda: (type(ns["loc"]).__name__, ns["loc"].v)) == ("Local", 1))
    check("(e) deleted class stays by value and restores", _try(lambda: (type(ns["gone"]).__name__, ns["gone"].w)) == ("Gone", 2))
    check("(e) local and deleted-class instances reference nothing by name",
          not man["loc"].get("refs") and not man["gone"].get("refs"))
    # manifest refs
    check("(manifest) refs: c->C, d->D, D->C, C->Base, lst->C,D, Alias->C, cbs->helper, factory->C",
          (man["c"].get("refs"), man["d"].get("refs"), man["D"].get("refs"), man["C"].get("refs"),
           man["lst"].get("refs"), man["Alias"].get("refs"), man["cbs"].get("refs"), man["factory"].get("refs"))
          == (["C"], ["D"], ["C"], ["Base"], ["C", "D"], ["C"], ["helper"], ["C"]))
    check("(manifest) Base and helper reference nothing", not man["Base"].get("refs") and not man["helper"].get("refs"))
    # (f) sizes
    print(f"    sizes: C {man['C']['size']} B, c {man['c']['size']} B, d {man['d']['size']} B, "
          f"lst {man['lst']['size']} B, Alias {man['Alias']['size']} B")
    ns_a = ex_a._namespace
    old_c = len(nspickle.dumps(ns_a["c"], ns_a))
    print(f"    c pickled with the class by value (before): {old_c} B")
    check("(f) C carries its 2 MB class attribute", man["C"]["size"] > BIG)
    check("(f) instance c is well under the class size (< 1 KB)", man["c"]["size"] < 1024)
    check("(f) list of two instances < 1 KB, alias < 200 B", man["lst"]["size"] < 1024 and man["Alias"]["size"] < 200)
    check("(f) by value the instance would carry the class (> 2 MB)", old_c > BIG)
    return ex_a, s3


def _run_redefine():
    print("\n(g) re-running the class cell")
    ex = SandboxExecutor()
    for cid, code in CELLS[:3]:
        ex.execute(code, cid)
    ex.execute("c = C()\nitems = [C(), C()]\nclass D(C):\n    pass\nd = D()\n", "inst")
    s3 = FakeS3()
    mgr = _manager(ex)
    with _Patched(s3):
        mgr.save("sess-redef")
        m1 = _manifest(s3, "sess-redef")["vars"]
        n_puts = len(s3.put_keys)
        res = ex.execute(CLASS_C2, "clsC")
        check("re-ran the class cell with a changed method", res.success)
        mgr.save("sess-redef")
        m2 = _manifest(s3, "sess-redef")["vars"]
    new_puts = [k for k in s3.put_keys[n_puts:] if "/vars/" in k]
    check("(g) the class variable C changed and was uploaded again",
          m1["C"]["hash"] != m2["C"]["hash"] and any(m2["C"]["object"] in k for k in new_puts))
    check("(g) instance variables are unchanged (same hash): c, items, d, D",
          all(m1[n]["hash"] == m2[n]["hash"] for n in ("c", "items", "d", "D")))
    check("(g) live: the old instance still uses the old class before the restore",
          _try(lambda: ex._namespace["c"].who()) == "C:base")
    ex_b, mgr_b = _restore_into_fresh(s3, "sess-redef")
    ns = ex_b._namespace
    check("(g) after restore c binds to the new class: type(c) is C, c.who() == 'C2:base'",
          type(ns.get("c")) is ns.get("C") and _try(lambda: ns["c"].who()) == "C2:base")
    check("(g) instances in a list use the new class", _try(lambda: [x.who() for x in ns["items"]]) == ["C2:base"] * 2)
    check("(g) subclass D now derives from the new C", _try(lambda: (ns["D"].__bases__[0] is ns["C"], ns["d"].who())) == (True, "C2:base"))
    check("(g) nothing reported as not restored", mgr_b.last_restore_not_restored == [])


def _run_order(s3):
    print("\n(h) order independence")
    man = _manifest(s3, "sess-cls")
    rev = dict(man)
    rev["vars"] = dict(reversed(list(man["vars"].items())))
    s3.store["sessions/sess-rev/manifest.json"] = json.dumps(rev).encode()
    strip = dict(man)
    strip["vars"] = {n: {k: v for k, v in m.items() if k != "refs"} for n, m in reversed(list(man["vars"].items()))}
    s3.store["sessions/sess-norefs/manifest.json"] = json.dumps(strip).encode()
    for k in [k for k in s3.store if k.startswith("sessions/sess-cls/vars/")]:
        s3.store[k.replace("sess-cls", "sess-rev")] = s3.store[k]
        s3.store[k.replace("sess-cls", "sess-norefs")] = s3.store[k]
    for sess, label in (("sess-rev", "manifest in reverse order"), ("sess-norefs", "reverse order and no refs (on demand)")):
        try:
            ex_b, mgr_b = _restore_into_fresh(s3, sess)
        except Exception as e:
            check(f"(h) {label}: restore raised {type(e).__name__}", False)
            continue
        ns = ex_b._namespace
        check(f"(h) {label}: identities hold",
              _try(lambda: type(ns["c"]) is ns["C"] and type(ns["d"]) is ns["D"] and ns["D"].__bases__[0] is ns["C"]
                   and ns["color"] is ns["Color"].RED and ns["Alias"] is ns["C"]
                   and [type(x) for x in ns["lst"]] == [ns["C"], ns["D"]]) is True)
        check(f"(h) {label}: nothing not restored", mgr_b.last_restore_not_restored == [])


def _run_missing():
    print("\n(i) missing or unsaved referenced class")
    ex = SandboxExecutor()
    ex.execute("class A:\n    pass\nclass B(A):\n    pass\na = A()\nb = B()\nkeep = 5\n", "m1")
    s3 = FakeS3()
    _save(ex, s3, "sess-miss")
    key = "sessions/sess-miss/manifest.json"
    man = json.loads(s3.store[key])
    del man["vars"]["A"]
    s3.store[key] = json.dumps(man).encode()
    try:
        ex_b, mgr_b = _restore_into_fresh(s3, "sess-miss")
        ok = True
    except Exception as e:
        ok = False
        print(f"    restore raised {type(e).__name__}: {e}")
    check("(i) restore with the class variable missing does not raise", ok)
    if ok:
        nr = {i["name"]: i["reason"] for i in mgr_b.last_restore_not_restored}
        check("(i) a, B and b are reported not restored, keep is restored",
              sorted(nr) == ["B", "a", "b"] and ex_b._namespace.get("keep") == 5)
        check("(i) the reason names the class", "class 'A'" in nr.get("a", "") and "which was not restored" in nr.get("a", ""))
        check("(i) restore report lists them", sorted(i["name"] for i in (mgr_b.last_restore_report or {}).get("vars_not_restored", []))
              == ["B", "a", "b"])
    # An unpicklable class attribute: the class and its instance are not saved.
    ex2 = SandboxExecutor()
    ex2.execute("class U:\n    g = (i for i in range(3))\nu = U()\nok_var = 1\n", "u")
    s3b = FakeS3()
    mgr = _save(ex2, s3b, "sess-unp")
    ns_names = {i["name"] for i in mgr.last_not_saved}
    check("(i) unpicklable class attribute: U and u are not saved, the save completes",
          {"U", "u"} <= ns_names and "ok_var" in _manifest(s3b, "sess-unp")["vars"])
    ex_c, mgr_c = _restore_into_fresh(s3b, "sess-unp")
    check("(i) ... and the rest restores", ex_c._namespace.get("ok_var") == 1 and mgr_c.last_restore_not_restored == [])


def _run_cycle_and_old():
    print("\n(j) reference cycle and old checkpoints")
    # Two functions that reference each other (an attribute and a default
    # argument): the one loaded first embeds the other by value.
    ex = SandboxExecutor()
    ex.execute("def fa():\n    return 1\ndef fb(other=fa):\n    return other() + 1\nfa.peer = fb\nkeep = 1\n", "cy")
    s3 = FakeS3()
    _save(ex, s3, "sess-cyc")
    man = _manifest(s3, "sess-cyc")["vars"]
    print(f"    cycle fa <-> fb: fa by_value {man['fa'].get('by_value')}, fb refs {man['fb'].get('refs')}")
    check("(j) the cycle fa <-> fb is broken: fa embeds fb by value, fb references fa by name",
          (man["fa"].get("by_value"), man["fa"].get("refs"), man["fb"].get("refs")) == (["fb"], None, ["fa"]))
    ex_b, mgr_b = _restore_into_fresh(s3, "sess-cyc")
    ns = ex_b._namespace
    check("(j) after restore fb's default is fa and both work",
          _try(lambda: (ns["fb"].__defaults__[0] is ns["fa"], ns["fb"](), ns["fa"].peer())) == (True, 2, 2))
    check("(j) the restore reports the by-value copy (fa.peer is a copy of fb)",
          mgr_b.last_restore_by_value == [{"name": "fa", "by_value": ["fb"]}]
          and (mgr_b.last_restore_report or {}).get("refs_by_value") == [{"name": "fa", "by_value": ["fb"]}]
          and _try(lambda: ns["fa"].peer is not ns["fb"]) is True)
    check("(j) nothing not restored", mgr_b.last_restore_not_restored == [])
    # A cycle through a class (class attributes, or a class attribute and a
    # default argument): dill cannot pickle a class by value inside such a
    # cycle in either direction (it could not before by-name references
    # either), so the cycle and the instances are reported as not saved.
    for code, a, b, inst in (
            ("class X:\n    pass\nclass Y:\n    pass\nX.other = Y\nY.other = X\nx = X()\nkeep = 1\n", "X", "Y", "x"),
            ("class K:\n    pass\ndef kf(kind=K):\n    return kind()\nK.factory = kf\nx = K()\nkeep = 1\n", "K", "kf", "x")):
        ex = SandboxExecutor()
        ex.execute(code, "cy2")
        s3 = FakeS3()
        mgr = _save(ex, s3, "sess-cyc2")
        ns_names = {i["name"]: i["reason"] for i in mgr.last_not_saved}
        check(f"(j) {a} <-> {b} cycle: {a}, {b} and {inst} are reported not saved, keep is saved",
              {a, b, inst} <= set(ns_names) and "keep" in _manifest(s3, "sess-cyc2")["vars"])
        check(f"(j) {a} <-> {b}: the reason names the cycle",
              "reference cycle between" in ns_names.get(a, "") and f"'{a}'" in ns_names.get(a, ""))
        ex_b, mgr_b = _restore_into_fresh(s3, "sess-cyc2")
        check(f"(j) {a} <-> {b}: the rest restores",
              ex_b._namespace.get("keep") == 1 and mgr_b.last_restore_not_restored == [])
    # Old checkpoint: classes embedded by value in each variable (no refs).
    ex2 = SandboxExecutor()
    ex2.execute("class O:\n    def v(self):\n        return 9\no = O()\n", "old")
    s3b = FakeS3()
    _save(ex2, s3b, "sess-old")
    key = "sessions/sess-old/manifest.json"
    m = json.loads(s3b.store[key])
    ns2 = ex2._namespace
    for name in ("O", "o"):
        b = nspickle.dumps(ns2[name], ns2)  # pre-change format: no by-name references
        s3b.store[f"sessions/sess-old/vars/{name}.pkl"] = b
        m["vars"][name] = {"hash": "x", "size": len(b), "object": f"vars/{name}.pkl"}
    s3b.store[key] = json.dumps(m).encode()
    try:
        ex_c, mgr_c = _restore_into_fresh(s3b, "sess-old")
        ok = True
    except Exception as e:
        ok = False
        print(f"    restore raised {type(e).__name__}: {e}")
    check("(j) old checkpoint restores without raising", ok)
    if ok:
        check("(j) old checkpoint: o works (its own class copy)", _try(lambda: ex_c._namespace["o"].v()) == 9)


def _run_guards(ex_a):
    print("\n(k) guards")
    ns = {}
    exec("class G:\n    pass\ng = G()\n", ns)
    vals = {"G": ns["G"], "g": ns["g"]}
    refs = nspickle.build_ref_map(vals, ns)
    b = nspickle.dumps(ns["g"], ns, refs=refs, self_name="g")
    check("(k) the instance pickle holds the by-name class id", b"mvm-ns-class" in b and len(b) < 200)
    for label, fn in (("plain dill.loads", lambda: dill.loads(b)),
                      ("loads with an empty namespace", lambda: nspickle.loads(b, {})),
                      ("loads with no namespace", lambda: nspickle.loads(b, None)),
                      ("a name bound to a non-class", lambda: nspickle.loads(b, {"G": 5}))):
        try:
            fn()
            r = "loaded"
        except pickle.UnpicklingError:
            r = "UnpicklingError"
        except Exception as e:
            r = type(e).__name__
        check(f"(k) {label} raises UnpicklingError", r == "UnpicklingError")
    target = {"G": ns["G"]}
    check("(k) loads resolves the id in the given namespace", type(nspickle.loads(b, target)) is ns["G"])
    many = [ns["G"]() for _ in range(10000)]
    by_name = nspickle.dumps(many, ns, refs=refs, self_name="many")
    by_value = nspickle.dumps(many, ns)
    print(f"    10,000 instances: by name {len(by_name)} B, class by value {len(by_value)} B")
    check("(k) the reference is written once per pickle, not once per instance",
          by_name.count(b"mvm-ns-class") == 1 and len(by_name) <= len(by_value))
    back = nspickle.loads(by_name, target)
    check("(k) ... and every instance restores as a G", all(type(x) is ns["G"] for x in back))

    class _Other(dill.Pickler):
        def persistent_id(self, obj):
            return ("mvm-ns-other", "G") if obj == "x" else None
    buf = io.BytesIO()
    _Other(buf, protocol=5).dump(["x"])
    try:
        nspickle.loads(buf.getvalue(), target)
        r = "loaded"
    except pickle.UnpicklingError:
        r = "UnpicklingError"
    check("(k) an unknown marker kind raises UnpicklingError", r == "UnpicklingError")
    # Second save with no changes uploads nothing.
    s3 = FakeS3()
    mgr = _manager(ex_a)
    with _Patched(s3):
        mgr.save("sess-k")
        m1 = _manifest(s3, "sess-k")
        n = len(s3.put_keys)
        mgr.save("sess-k")
        m2 = _manifest(s3, "sess-k")
    check("(k) second save uploads 0 var bytes", mgr.last_save_sizes.get("upload_pkl") == 0
          and not [k for k in s3.put_keys[n:] if "/vars/" in k])
    check("(k) same hashes and refs", {k: (v["hash"], v.get("refs")) for k, v in m1["vars"].items()}
          == {k: (v["hash"], v.get("refs")) for k, v in m2["vars"].items()})
    # Model export stays plain joblib (no checkpoint pickler, no marker).
    try:
        import joblib
        from sklearn.linear_model import LinearRegression
        from app.notebook.helpers import ml
        model = LinearRegression().fit([[0.0], [1.0], [2.0]], [0.0, 1.0, 2.0])
        art = ml._serialize_model(model, ".joblib")
        clean = b"mvm-ns-" not in art and b"notebook-namespace" not in art
        loaded = joblib.load(io.BytesIO(art))
        check("(k) model export: no marker in the artifact and it loads with joblib",
              clean and abs(float(loaded.predict([[3.0]])[0]) - 3.0) < 1e-6)
    except ImportError as e:
        print(f"    model export check skipped ({e})")
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "app", "notebook", "helpers", "ml.py")).read()
    check("(k) ml.py does not use the checkpoint pickler", "nspickle" not in src)


def _run():
    ex_a, s3 = _run_roundtrip()
    _run_redefine()
    _run_order(s3)
    _run_missing()
    _run_cycle_and_old()
    _run_guards(ex_a)


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
