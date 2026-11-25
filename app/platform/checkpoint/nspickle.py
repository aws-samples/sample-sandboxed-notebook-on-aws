"""
Checkpoint pickler that writes the notebook namespace as a reference.

Part of: app.platform.checkpoint (infrastructure layer)

A function defined in a cell (and every method, lambda, nested function and
class body function) has the notebook namespace dict as its `__globals__`.
dill pickles a function whose globals are not a module `__dict__` together with
a copy of that dict, so every notebook function carried every variable: a
2-line helper next to a 1.3 GB array pickled to 1.3 GB, and after a restore its
globals were a detached copy, so it no longer saw the live variables.

NamespacePickler writes the namespace dict as the persistent id
NAMESPACE_MARKER instead of its contents (persistent_id matches the dict by
identity, at any depth), and NamespaceUnpickler turns that id back into the
namespace being restored. A restored function's `__globals__` is then the live
executor namespace, so it sees the current value of every global.

Why the FunctionType dispatch entry is replaced (not only persistent_id): for
a function whose globals are not a module dict, dill does not save
`__globals__` itself; it saves a new `{'__name__': ...}` dict and copies the
globals into it after the function is built, so persistent_id never sees the
namespace object. _save_ns_function runs dill's own by-value branch with the
namespace as the globals, so save(namespace) runs and the marker is written.
(A reducer_override that rebuilds the function recurses forever on a
self-referencing closure; dill's _save_with_postproc handles that case.)

Class and function references by name. Each variable is pickled on its own,
so a class defined in a cell used to be pickled by value inside every variable
that used it (the class variable and each instance), and after a restore every
instance had its own copy of the class (`type(c) is C` was False). With a
RefMap (build_ref_map) the pickler writes, anywhere in a variable's object
graph, a top-level notebook class as the persistent id (CLASS_MARKER, name)
and a top-level notebook function as (FUNCTION_MARKER, name), wrapped in a
reduce (`_ref`) so pickle's memo writes each reference once per pickle (a
bare persistent id is written again for every instance). The variable
that holds the class (or function) under its canonical name still pickles it
by value, as before. The unpickler resolves a by-name id through a `resolve`
callback, which the restore uses to load the referenced variable first.
  - Canonical name: a class or function bound to several top-level names
    (`D = C`) has one canonical name, the one equal to its `__name__`, else the
    first in sorted order; every other name pickles as just the reference.
  - A class that is no longer bound at top level (redefined by re-running its
    cell) is referenced by its name when that name now holds a notebook class
    with the same `__qualname__` and `__module__`, so after a restore its
    instances bind to the current definition (as unpickling an instance of a
    module class binds to the module's current class).
  - Classes that pickle by reference anyway (importable from a module), local
    classes and anything not bound at top level stay by value. Functions are
    matched by identity only.
  - `exclude`: names the pickler embeds by value instead (used to break a
    reference cycle between two variables).

Aliases of data and shared closure cells, by the same mechanism. Each variable
being pickled on its own also lost every alias between two top-level
variables (`b = a` restored as two lists, `cfg` inside `run` as a copy, a
16 MB DataFrame bound under 4 names was uploaded and restored 4 times), and
two functions from one factory call (`inc, get = make_counter()`) each got
their own copy of the shared closure cell, so after a restore `get()` no
longer saw `inc()`'s updates.
  - VALUE_MARKER ("mvm-ns-value", name): a top-level value for which
    aliasable() is true (list, dict, set, bytearray, collections containers,
    numpy arrays, pandas DataFrame/Series/Index, instances of notebook
    classes) is pickled by value only under its canonical name; anywhere else
    it is the reference. Library objects (a numpy dtype, a logger) are never
    in the map, so binding a library singleton at top level does not tie every
    value that reaches it to that name. The canonical name is sticky across
    saves (build_ref_map `prev`), else the first in sorted order.
  - CELL_MARKER ("mvm-ns-cell", "owner#i"): each closure cell of a top-level
    function is owned by the first function (sorted by canonical name) that
    holds it; every other function writes the cell as that id, resolved on
    load as owner.__closure__[i]. The reference counts as a reference to the
    owner variable (manifest "refs", restore order, failure reporting).
  - Not covered: a numpy view or a DataFrame column (a different object that
    shares a buffer), and immutable values (ints, strings, tuples), whose
    identity has no meaning.

Enum classes defined in a cell, which dill cannot pickle by value (their
members are instances of the class), are saved by _save_ns_type as their
metaclass, bases, body and member values and created again on load.

Every object that is not a function bound to the namespace, a by-name class,
function, value or cell, or a notebook Enum class, pickles exactly as with
dill.dump, so the bytes, and the delta hashes, of a data value that shares
nothing with another top-level variable are unchanged. Only load these
pickles through
NamespaceUnpickler / loads(): a plain dill.loads of a pickle that holds a
marker raises UnpicklingError.
"""

import collections
import copyreg
import enum
import io
import pickle
import sys
import types

import dill
from dill import _dill

NAMESPACE_MARKER = "notebook-namespace"
CLASS_MARKER = "mvm-ns-class"
FUNCTION_MARKER = "mvm-ns-function"
VALUE_MARKER = "mvm-ns-value"
CELL_MARKER = "mvm-ns-cell"
# Kinds resolved by name through `resolve(kind, name)`; a CELL_MARKER id
# ("owner#i") is resolved through its owner function instead.
_REF_KINDS = (CLASS_MARKER, FUNCTION_MARKER, VALUE_MARKER)
_MISSING = object()


class MissingReference(pickle.UnpicklingError):
    """A by-name class or function reference that cannot be resolved (the
    variable it names was not restored, or is part of the variable being
    loaded). `reason` names the reference, never a value."""

    def __init__(self, kind: str, name: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.name = name
        self.reason = reason


def _kind_label(kind: str) -> str:
    if kind == CLASS_MARKER:
        return "class"
    if kind == VALUE_MARKER:
        return "variable"
    return "function"


def _kind_matches(kind: str, value) -> bool:
    if kind == CLASS_MARKER:
        return isinstance(value, type)
    if kind == VALUE_MARKER:
        return True
    return isinstance(value, types.FunctionType)


def _importable(cls) -> bool:
    """True when `cls` is reachable as sys.modules[cls.__module__].<qualname>,
    so pickle saves it by reference anyway. Only modules already imported are
    looked at (nothing is imported). Never raises."""
    try:
        o = sys.modules.get(cls.__module__)
        if o is None:
            return False
        for part in cls.__qualname__.split("."):
            o = getattr(o, part, _MISSING)
            if o is _MISSING:
                return False
        return o is cls
    except Exception:
        return False


def ref_kind(value, namespace):
    """CLASS_MARKER for a class defined in the notebook (not importable),
    FUNCTION_MARKER for a plain function whose globals are `namespace`, else
    None."""
    try:
        if isinstance(value, type):
            return None if _importable(value) else CLASS_MARKER
        if type(value) is types.FunctionType and namespace is not None and value.__globals__ is namespace:
            return FUNCTION_MARKER
    except Exception:
        return None
    return None


# Container types whose top-level values are shared by name (VALUE_MARKER).
# Exact types only: a dict or list subclass from a library (matplotlib's
# rcParams) is left by value, like every other library object.
_ALIAS_TYPES = frozenset({list, dict, set, bytearray, collections.OrderedDict,
                          collections.defaultdict, collections.Counter, collections.deque})


def aliasable(value, namespace) -> bool:
    """True for a top-level value that other variables reference by name
    (VALUE_MARKER) instead of copying: a list, dict, set, bytearray or
    collections container, a numpy array, a pandas DataFrame, Series or Index,
    or an instance of a class defined in the notebook (not an Enum member).
    Library objects (a numpy dtype, a logger, pd.NaT) stay by value, so a
    top-level name bound to a library singleton does not make every value that
    reaches it reference that name. Never raises."""
    try:
        if value is namespace:
            return False
        t = type(value)
        if t in _ALIAS_TYPES:
            return True
        np = sys.modules.get("numpy")
        if np is not None and isinstance(value, np.ndarray):
            return True
        pd = sys.modules.get("pandas")
        if pd is not None and isinstance(value, (pd.DataFrame, pd.Series, pd.Index)):
            return True
        if isinstance(value, (type, tuple, enum.Enum, types.FunctionType, types.MethodType,
                              types.ModuleType, types.BuiltinFunctionType)):
            return False
        # An instance of a class defined in the notebook: cell classes get
        # __module__ "builtins" (the namespace has no __name__) and are not
        # importable from it.
        mod = (namespace or {}).get("__name__") or "builtins"
        return getattr(t, "__module__", None) == mod and not _importable(t)
    except Exception:
        return False


class RefMap:
    """Top-level notebook classes, functions, aliasable values and the closure
    cells of those functions, for one save's by-name references: by_id
    {id(obj): (kind, canonical name, obj)} (the object is kept so an id is
    never matched to another object; a cell's name is "owner#i") and, for the
    redefined-class rule, by_name {canonical name: entry} of classes whose
    `__qualname__` is that name."""

    def __init__(self, entries=None):
        self.by_id: dict = {}
        self.by_name: dict = {}
        for e in entries or ():
            self._add(e)

    def _add(self, entry) -> None:
        kind, name, obj = entry
        self.by_id[id(obj)] = entry
        if kind == CLASS_MARKER and getattr(obj, "__qualname__", None) == name:
            self.by_name[name] = entry

    def canonical(self, value):
        """The canonical name of `value` when it is in the map, else None."""
        e = self.by_id.get(id(value))
        return e[1] if e is not None and e[2] is value else None

    def drop(self, name: str) -> None:
        """Forget the class, function or value with canonical `name`, and the
        closure cells that function owns (its variable was not saved, so
        nothing may reference it by name)."""
        self.by_id = {k: e for k, e in self.by_id.items()
                      if e[1] != name and not (e[0] == CELL_MARKER and _cell_owner(e[1]) == name)}
        self.by_name.pop(name, None)

    def lookup(self, obj):
        """(kind, name) for a reference to `obj`, else None."""
        e = self.by_id.get(id(obj))
        if e is not None and e[2] is obj:
            return e[0], e[1]
        if self.by_name and issubclass(type(obj), type):
            try:
                qn = obj.__qualname__
                e = self.by_name.get(qn)
                if e is not None and obj.__module__ == e[2].__module__ and not _importable(obj):
                    return e[0], e[1]
            except Exception:
                return None
        return None

    def __bool__(self) -> bool:
        return bool(self.by_id)


def _cell_owner(cell_name: str) -> str:
    """The owner function's name of a CELL_MARKER name "owner#i"."""
    return cell_name.rpartition("#")[0]


def _value_owner(names: list, prev) -> str:
    """Canonical name of a value bound to `names`. Sticky: a name that held
    the value by value in the previous manifest `prev` ({name: entry}; present
    and its "refs" name none of `names`) stays the owner, so adding or deleting
    an alias does not move the value to another name and re-upload it. Else
    the first in sorted order."""
    if prev:
        group = set(names)
        kept = [n for n in names if isinstance(prev.get(n), dict)
                and not group.intersection(prev[n].get("refs") or ())]
        if kept:
            return min(kept)
    return min(names)


def build_ref_map(values: dict, namespace, prev=None) -> RefMap:
    """RefMap of the variables being saved (`values`):
      - classes and functions (ref_kind): a value bound to several names gets
        one canonical name, the one equal to its `__name__`, else the first in
        sorted order;
      - aliasable values (aliasable(), VALUE_MARKER): canonical name from
        _value_owner (sticky across saves through `prev`, the previous
        manifest's {name: entry}; else the first in sorted order);
      - each closure cell of a top-level function (CELL_MARKER): owned by the
        first function, by canonical name in sorted order, that holds it,
        named "owner#i" (i = its index in owner.__closure__)."""
    groups: dict = {}
    vgroups: dict = {}
    for name, v in values.items():
        kind = ref_kind(v, namespace)
        if kind is not None:
            groups.setdefault(id(v), (kind, v, []))[2].append(name)
        elif aliasable(v, namespace):
            vgroups.setdefault(id(v), (v, []))[1].append(name)
    entries = []
    for v, names in vgroups.values():
        entries.append((VALUE_MARKER, _value_owner(names, prev), v))
    fns = []
    for kind, v, names in groups.values():
        own = getattr(v, "__name__", None)
        canon = own if own in names else min(names)
        entries.append((kind, canon, v))
        if kind == FUNCTION_MARKER:
            fns.append((canon, v))
        if kind == CLASS_MARKER:
            # Pickling an instance caches `__slotnames__` in its class's
            # __dict__ (copyreg), which would change the class's own pickle
            # (and hash) between the first save and the next. Fill the cache
            # now, as pickling an instance does, so the class pickles the same
            # every time.
            try:
                copyreg._slotnames(v)
            except Exception:
                pass
    rm = RefMap(entries)
    for fname, fn in sorted(fns, key=lambda e: e[0]):
        for i, cell in enumerate(fn.__closure__ or ()):
            if id(cell) not in rm.by_id:
                rm._add((CELL_MARKER, f"{fname}#{i}", cell))
    return rm


def resolver_for(mapping: dict):
    """A `resolve` callback that looks a by-name reference up in `mapping`."""
    def _resolve(kind, name):
        v = mapping.get(name, _MISSING) if mapping is not None else _MISSING
        if v is _MISSING or not _kind_matches(kind, v):
            raise MissingReference(kind, name, f"references {_kind_label(kind)} '{name}', which was not restored")
        return v
    return _resolve


# Same protocol as delta.PICKLE_PROTOCOL (kept literal to avoid an import cycle;
# tests/checkpoint/test_function_namespace_ref.py checks they match).
_PROTOCOL = 5


def _create_ns_function(code, globs, name, defaults, closure):
    """Load-side constructor for a namespace-bound function. Unlike dill's
    _create_function it never replaces the globals with a new dict: when the
    namespace being restored is still empty, the function must still get it."""
    return types.FunctionType(code, globs, name, defaults, closure)


def _save_ns_function(pickler, obj):
    """FunctionType dispatch entry: dill's by-value function branch (F1 in
    dill 0.4.0 save_function) with `globs = the notebook namespace`, so the
    namespace is saved as the marker; any other function goes to dill."""
    ns = getattr(pickler, "_nb_namespace", None)
    if ns is None or obj.__globals__ is not ns or type(obj.__code__) is not types.CodeType:
        return _dill.save_function(pickler, obj)
    globs = ns
    closure = obj.__closure__
    state_dict = {}
    for fattrname in ("__doc__", "__kwdefaults__", "__annotations__"):
        fattr = getattr(obj, fattrname, None)
        if fattr is not None:
            state_dict[fattrname] = fattr
    if obj.__qualname__ != obj.__name__:
        state_dict["__qualname__"] = obj.__qualname__
    if "__name__" not in globs or obj.__module__ != globs["__name__"]:
        state_dict["__module__"] = obj.__module__
    state = obj.__dict__
    if type(state) is not dict:
        state_dict["__dict__"] = state
        state = None
    if state_dict:
        state = state, state_dict
    _dill._save_with_postproc(pickler, (_create_ns_function, (
        obj.__code__, globs, obj.__name__, obj.__defaults__, closure
    ), state), obj=obj, postproc_list=[])
    # Lift the closure cell update to the earliest function (as dill does, #458),
    # so a self-recursive closure gets its cell filled.
    _postproc = pickler._postproc
    if _postproc:
        topmost_postproc = next(iter(_postproc.values()), None)
        if closure and topmost_postproc:
            for cell in closure:
                possible_postproc = (setattr, (cell, "cell_contents", obj))
                try:
                    topmost_postproc.remove(possible_postproc)
                except ValueError:
                    continue
                pickler.save_reduce(*possible_postproc)
                pickler.write(pickle.POP)


# Keys of an Enum class __dict__ that the enum machinery creates (they differ
# between Python versions); everything else is the class body.
_ENUM_MACHINERY_KEYS = frozenset({
    "__new__", "__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
    "__static_attributes__", "_member_map_", "_member_names_", "_member_type_", "_new_member_",
    "_unhashable_values_", "_unhashable_values_map_", "_hashable_values_", "_use_args_",
    "_value2member_map_", "_value_repr_", "_all_bits_", "_boundary_", "_flag_mask_", "_inverted_",
    "_singles_mask_",
})


def _create_ns_enum(metacls, name, bases, body, members, module, qualname):
    """Load-side constructor for an Enum class pickled by value: the class is
    created again through its metaclass from its body and its members' values,
    so the members are new singletons of the new class."""
    classdict = metacls.__prepare__(name, bases)
    for k, v in body.items():
        classdict[k] = v
    for k, v in members.items():
        classdict[k] = v
    cls = metacls(name, bases, classdict)
    cls.__module__ = module
    cls.__qualname__ = qualname
    return cls


def _save_ns_type(pickler, obj):
    """Dispatch entry for classes whose metaclass is an Enum metaclass. dill
    cannot pickle an Enum class by value (its members are instances of the
    class, so it gives up and pickles it by a name that does not exist); a
    notebook Enum is saved as (metaclass, name, bases, body, member values)
    instead. Importable Enums and other classes go to dill's save_type."""
    if not isinstance(obj, enum.EnumMeta) or _importable(obj):
        return _dill.save_type(pickler, obj)
    d = obj.__dict__
    member_map = obj._member_map_
    body = {k: v for k, v in d.items() if k not in member_map and k not in _ENUM_MACHINERY_KEYS}
    # A `__new__` written in the class body is kept by the machinery as
    # _new_member_; the inherited one is not part of the body.
    new = d.get("_new_member_")
    inherited = {getattr(b, "_new_member_", None) for b in obj.__mro__[1:]}
    inherited.add(getattr(obj._member_type_, "__new__", None))
    if new is not None and new not in inherited:
        body["__new__"] = new
    members = {k: m._value_ for k, m in member_map.items()}
    _dill._save_with_postproc(pickler, (_create_ns_enum, (
        type(obj), obj.__name__, obj.__bases__, body, members, obj.__module__, obj.__qualname__
    )), obj=obj, postproc_list=[])


class _Ref:
    """Save-side placeholder for a by-name reference; NamespacePickler writes
    it as the persistent id (kind, name)."""

    __slots__ = ("kind", "name")

    def __init__(self, kind: str, name: str):
        self.kind = kind
        self.name = name


def _ref(value):
    """Load-side reduce function of a by-name reference: returns the object
    persistent_load resolved (the reduce makes the unpickler memoize it)."""
    return value


class _NamespaceDispatch(dict):
    """dill's dispatch table with the FunctionType entry replaced, and Enum
    metaclasses sent to _save_ns_type. Every other lookup goes to
    dill.Pickler.dispatch at call time, so types dill registers later (its
    numpy ufunc/dtype hooks) are still found."""

    def get(self, key, default=None):
        if key is types.FunctionType:
            return _save_ns_function
        if isinstance(key, type) and issubclass(key, enum.EnumMeta):
            return _save_ns_type
        return dill.Pickler.dispatch.get(key, default)


class NamespacePickler(dill.Pickler):
    """dill.Pickler that writes `namespace` (matched by identity) as
    NAMESPACE_MARKER and, with a RefMap `refs`, each class and function of the
    map as a by-name reference, except the one named `self_name` (the variable
    being pickled) and those in `exclude`. With namespace=None and no refs it
    behaves like dill.Pickler."""

    dispatch = _NamespaceDispatch()

    def __init__(self, file, namespace, protocol=_PROTOCOL, refs=None, self_name=None, exclude=()):
        super().__init__(file, protocol=protocol)
        self._nb_namespace = namespace
        self._nb_refs = refs if refs else None
        self._nb_self = self_name
        self._nb_exclude = frozenset(exclude)
        # Canonical names written as by-name references (the restore order).
        self.found_refs: set = set()
        # Names that would have been references but were embedded by value
        # because they are in `exclude`.
        self.embedded: set = set()

    def persistent_id(self, obj):
        if self._nb_namespace is not None and obj is self._nb_namespace:
            return NAMESPACE_MARKER
        if type(obj) is _Ref:
            return (obj.kind, obj.name)
        return None

    def reducer_override(self, obj):
        """A class, function, value or cell of the RefMap is saved as
        `_ref(<persistent id (kind, name)>)`. As a reduce (unlike a persistent id) it is memoized,
        so a list of 100,000 instances writes the reference once, not once
        per instance."""
        refs = self._nb_refs
        if refs is None:
            return NotImplemented
        # Fast path for the common miss (this runs for every object pickled):
        # not in the map by identity, and not a class for the redefined-class rule.
        e = refs.by_id.get(id(obj))
        if (e is None or e[2] is not obj) and not (refs.by_name and issubclass(type(obj), type)):
            return NotImplemented
        hit = refs.lookup(obj)
        if hit is None:
            return NotImplemented
        kind, name = hit
        # A closure cell is a reference to its owner function's variable.
        owner = _cell_owner(name) if kind == CELL_MARKER else name
        # The variable that holds the object under its canonical name pickles
        # it by value (and so does a class that subclasses its own former
        # definition, `class C(C)`): a reference to itself cannot be resolved.
        if owner == self._nb_self:
            return NotImplemented
        if owner in self._nb_exclude:
            self.embedded.add(owner)
            return NotImplemented
        self.found_refs.add(owner)
        return _ref, (_Ref(kind, name),)


class NamespaceUnpickler(dill.Unpickler):
    """dill.Unpickler that resolves NAMESPACE_MARKER to `namespace` (the live
    executor namespace), a by-name (CLASS_MARKER / FUNCTION_MARKER /
    VALUE_MARKER, name) id through `resolve(kind, name)`, and a
    (CELL_MARKER, "owner#i") id to cell i of the function `resolve` returns
    for owner. Any other persistent id, the marker without a namespace, or a
    by-name id without a resolver raises UnpicklingError."""

    def __init__(self, file, namespace, resolve=None):
        super().__init__(file)
        self._nb_namespace = namespace
        self._nb_resolve = resolve

    def persistent_load(self, pid):
        if type(pid) is str and pid == NAMESPACE_MARKER and self._nb_namespace is not None:
            return self._nb_namespace
        if (type(pid) is tuple and len(pid) == 2 and type(pid[1]) is str
                and self._nb_resolve is not None):
            if pid[0] in _REF_KINDS:
                return self._nb_resolve(pid[0], pid[1])
            if pid[0] == CELL_MARKER:
                return self._load_cell(pid[1])
        raise pickle.UnpicklingError(f"unknown persistent id {pid!r}")

    def _load_cell(self, cell_name: str):
        """A ("mvm-ns-cell", "owner#i") id: cell i of the restored owner
        function's closure (the owner is resolved, loaded first when pending,
        like a function reference)."""
        owner, sep, idx = cell_name.rpartition("#")
        if not sep or not owner or not idx.isdigit():
            raise pickle.UnpicklingError(f"unknown persistent id {(CELL_MARKER, cell_name)!r}")
        fn = self._nb_resolve(FUNCTION_MARKER, owner)
        closure = fn.__closure__ or ()
        i = int(idx)
        if i >= len(closure):
            raise MissingReference(CELL_MARKER, owner,
                                   f"references closure cell {i} of function '{owner}', which has no such cell")
        return closure[i]


def dump(value, file, namespace, refs=None, self_name=None) -> None:
    """Pickle `value` into `file`, writing `namespace` as the marker (and the
    classes and functions of `refs` by name)."""
    NamespacePickler(file, namespace, refs=refs, self_name=self_name).dump(value)


def dumps(value, namespace, refs=None, self_name=None) -> bytes:
    """Pickle `value` to bytes, writing `namespace` as the marker (and the
    classes and functions of `refs` by name)."""
    buf = io.BytesIO()
    NamespacePickler(buf, namespace, refs=refs, self_name=self_name).dump(value)
    return buf.getvalue()


class _BufferRaw(io.RawIOBase):
    """Read-only raw stream over a memoryview, so a bytearray (a ranged
    download) unpickles without the full-buffer copy io.BytesIO would make."""

    def __init__(self, view: memoryview):
        self._view = view.cast("B") if view.format != "B" or view.ndim != 1 else view
        self._pos = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = min(len(b), len(self._view) - self._pos)
        if n <= 0:
            return 0
        b[:n] = self._view[self._pos:self._pos + n]
        self._pos += n
        return n


def loads(b: bytes, namespace, resolve=None):
    """Unpickle `b`, binding the marker to `namespace`. A by-name reference is
    resolved by `resolve(kind, name)`; by default it is looked up in
    `namespace`. A bytearray or memoryview is read in place (no copy)."""
    if resolve is None and namespace is not None:
        resolve = resolver_for(namespace)
    if isinstance(b, (bytearray, memoryview)):
        f = io.BufferedReader(_BufferRaw(memoryview(b)))
    else:
        f = io.BytesIO(b)
    return NamespaceUnpickler(f, namespace, resolve).load()
