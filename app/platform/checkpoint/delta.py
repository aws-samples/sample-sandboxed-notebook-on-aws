"""
Delta checkpoint core — per-variable and per-file change detection.

Part of: app.platform.checkpoint (infrastructure layer)

Instead of re-pickling the whole namespace (or re-tarring every /tmp file) on
each suspend, we content-address each variable and cheaply signature each file,
then only re-upload what actually changed since the last save. A manifest maps
name → {hash/signature, size, object} and is the source of truth for restore.

All functions here are PURE (no S3, no self) so they can be unit-tested directly
(see tests/checkpoint/test_delta_core.py and test_file_delta_core.py).
"""

import os

from .deadline import SaveCancelled, check


# ─────────────────────────────────────────────────────────────────────────────
#  DELTA CHECKPOINT CORE (Option A: per-variable objects + manifest)
#
#  Instead of re-pickling the whole namespace on every suspend, we pickle each
#  variable independently, content-hash its bytes, and only re-upload variables
#  whose hash changed since the last save. A manifest maps var name → {hash,size}
#  and is the source of truth for restore.
#
#  These functions are PURE (no S3, no self) so they can be unit-tested directly.
# ─────────────────────────────────────────────────────────────────────────────

# Vars whose pickled size is <= this are batched into one shared object instead of
# getting their own S3 object, to avoid a PUT-per-tiny-var explosion.
DELTA_SMALL_VAR_BYTES = 65536  # 64 KB

# One pickle protocol for every variable, so a value hashes the same whether it
# is pickled in memory or streamed to a spool file. Protocol 5 needs Python 3.8+
# to load (the VM runs 3.11).
PICKLE_PROTOCOL = 5

# Streaming save (see build_var_blobs(spool_dir=...)). A variable is pickled into
# a file under the spool directory instead of into a bytes object. dill uses the
# pure-Python pickler, which still copies one numpy/pandas buffer while it writes
# it, so the measured save peak is the live value plus about 1x its data size
# (it was 3x with dill.dumps: the copy plus the BytesIO result plus the live
# value). The spool directory is hidden and the files end in ".part", so the
# /tmp file checkpoint globs (*.pkl, *.csv, ...) never pick them up.
DEFAULT_SPOOL_DIR = "/tmp/.checkpoint-spool"
# Memory kept free while a variable is pickled (page cache, the server itself,
# the upload threads). A variable whose estimated pickle copy does not fit in
# MemAvailable minus this is skipped instead of risking the out-of-memory killer.
SAVE_MEMORY_HEADROOM_BYTES = 256 * 1024 * 1024
# Disk kept free in the spool directory's file system.
SAVE_DISK_HEADROOM_BYTES = 64 * 1024 * 1024

# Prefixes of the reasons build_var_blobs records for a skipped variable; the
# manager maps them to the not-saved kind ("memory", "disk", "unpicklable").
NOT_ENOUGH_MEMORY = "not enough memory to save"
NOT_ENOUGH_DISK = "not enough disk space to save"

_MIB = 1024 * 1024

# A spool write larger than this is split into slices of this size, with a save
# deadline check before each one (see _HashingSpool.write).
SPOOL_WRITE_SLICE = 16 * _MIB


def _pickle_one(value, namespace=None) -> bytes:
    """Pickle a single value with the checkpoint pickler (dill, with `namespace`
    written as the notebook-namespace marker; see nspickle). Raises on failure
    (caller decides fallback)."""
    from . import nspickle
    return nspickle.dumps(value, namespace)


def _hash_bytes(b: bytes) -> str:
    """Stable content hash of pickled bytes (used to detect real changes)."""
    import hashlib
    return hashlib.sha256(b).hexdigest()


def mem_available_bytes() -> int | None:
    """MemAvailable from /proc/meminfo in bytes; None where it is not readable
    (macOS, tests). The VM has no swap and no cgroup memory file, and the kernel
    out-of-memory killer fires when this reaches zero (measured on the 1024 tier)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def disk_free_bytes(path: str) -> int | None:
    """Free bytes on the file system holding `path`; None when unknown."""
    import shutil
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def estimate_value_bytes(value, _depth: int = 0, _seen: set | None = None) -> int:
    """Rough size in bytes of the data `value` holds, used to predict the memory a
    pickle of it needs. Exact for numpy arrays (nbytes), pandas objects (shallow
    memory_usage) and bytes-like values; containers are summed a few levels deep;
    anything else falls back to sys.getsizeof. Never raises."""
    import sys
    seen = _seen if _seen is not None else set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    try:
        if isinstance(value, (bytes, bytearray)):
            return len(value)
        if isinstance(value, memoryview):
            return value.nbytes
        nb = getattr(value, "nbytes", None)
        if isinstance(nb, int) and type(value).__module__.startswith("numpy"):
            return nb
        mu = getattr(value, "memory_usage", None)
        if callable(mu) and type(value).__module__.startswith("pandas"):
            total = mu(index=True, deep=False)
            return int(total.sum()) if hasattr(total, "sum") else int(total)
        if _depth < 3:
            if isinstance(value, dict):
                items = list(value.items())[:10000]
                return sys.getsizeof(value) + sum(
                    estimate_value_bytes(k, _depth + 1, seen) + estimate_value_bytes(v, _depth + 1, seen)
                    for k, v in items)
            if isinstance(value, (list, tuple, set, frozenset)):
                items = list(value)[:10000]
                return sys.getsizeof(value) + sum(estimate_value_bytes(v, _depth + 1, seen) for v in items)
        return sys.getsizeof(value)
    except Exception:
        return 0


class _HashingSpool:
    """File-like sink for dill.dump: hashes every write and keeps the first
    DELTA_SMALL_VAR_BYTES in memory. The first write that would go past that
    moves the data to a new file in spool_dir and every later write goes there,
    so a large pickle never exists as one bytes object."""

    def __init__(self, spool_dir: str, limit: int = DELTA_SMALL_VAR_BYTES, deadline=None):
        import hashlib
        import io
        self._dir = spool_dir
        self._limit = limit
        self._deadline = deadline
        self._buf = io.BytesIO()
        self._file = None
        self.path: str | None = None
        self.size = 0
        self._sha = hashlib.sha256()

    def write(self, b) -> int:
        n = len(b) if not isinstance(b, memoryview) else b.nbytes
        if self._deadline is None or n <= SPOOL_WRITE_SLICE:
            check(self._deadline, "serialize")
            return self._write(b, n)
        # A large buffer (one numpy array) is written in slices, with a deadline
        # check before each one; slicing a memoryview copies nothing.
        try:
            mv = memoryview(b).cast("B")
        except (TypeError, ValueError):  # not C-contiguous: one write
            check(self._deadline, "serialize")
            return self._write(b, n)
        for off in range(0, n, SPOOL_WRITE_SLICE):
            check(self._deadline, "serialize")
            part = mv[off:off + SPOOL_WRITE_SLICE]
            self._write(part, part.nbytes)
        return n

    def _write(self, b, n: int) -> int:
        self._sha.update(b)
        if self._file is None and self.size + n > self._limit:
            import tempfile
            os.makedirs(self._dir, exist_ok=True)
            fd, self.path = tempfile.mkstemp(dir=self._dir, suffix=".part")
            self._file = os.fdopen(fd, "wb")
            self._file.write(self._buf.getbuffer())
            self._buf = None
        if self._file is not None:
            self._file.write(b)
        else:
            self._buf.write(b)
        self.size += n
        return n

    def finish(self) -> dict:
        """Close the sink; the blob dict for build_var_blobs."""
        h = self._sha.hexdigest()
        if self._file is not None:
            self._file.close()
            return {"path": self.path, "hash": h, "size": self.size}
        return {"bytes": self._buf.getvalue(), "hash": h, "size": self.size}

    def discard(self) -> None:
        """Close and delete any spool file (pickling failed part-way)."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _preflight(name: str, need: int, mem_available, disk_free, spool_dir: str) -> str | None:
    """Reason to skip `name` when its estimated pickle (`need` bytes) cannot fit in
    memory or in the spool file system; None when it fits or nothing is known."""
    if need <= DELTA_SMALL_VAR_BYTES:
        return None
    avail = mem_available() if mem_available else None
    if avail is not None and need + SAVE_MEMORY_HEADROOM_BYTES > avail:
        return (f"{NOT_ENOUGH_MEMORY} '{name}' (needs about {need / _MIB:.0f} MiB, "
                f"{max(avail - SAVE_MEMORY_HEADROOM_BYTES, 0) / _MIB:.0f} MiB available)")
    free = disk_free(spool_dir) if disk_free else None
    if free is not None and need + SAVE_DISK_HEADROOM_BYTES > free:
        return (f"{NOT_ENOUGH_DISK} '{name}' (needs about {need / _MIB:.0f} MiB in "
                f"{spool_dir}, {max(free - SAVE_DISK_HEADROOM_BYTES, 0) / _MIB:.0f} MiB free)")
    return None


def build_var_blobs(namespace_to_save: dict, *, spool_dir: str | None = None,
                    mem_available=mem_available_bytes, disk_free=disk_free_bytes,
                    namespace: dict | None = None, deadline=None,
                    prev_manifest: dict | None = None) -> tuple[dict, list]:
    """Pickle each variable independently and hash it.

    Returns:
      blobs: {name: {"bytes": <pickle>, "hash": <sha256>, "size": <int>}}; with
             spool_dir, a pickle larger than DELTA_SMALL_VAR_BYTES is a file
             instead: {"path": <spool file>, "hash", "size"} (the caller uploads
             and deletes it; release_blobs() deletes them all)
      skipped: [(name, reason)] for vars that could not be pickled individually,
               or (spool_dir only) would not fit in memory or on disk

    Per-variable pickling is what enables delta upload. Sharing between
    variables is kept by the by-name references below (with `namespace`):
    an object bound at top level is stored once, under its canonical name.

    Without spool_dir every pickle is built in memory with dill.dumps (the old
    behaviour, kept for the pure unit tests). With spool_dir each variable is
    streamed through _HashingSpool, one at a time, after a pre-flight check of
    its estimated size against mem_available() and disk_free(spool_dir). A
    MemoryError or a full disk while pickling skips that variable only.

    namespace: the live notebook namespace dict (executor._namespace). Every
    reference to it, including the `__globals__` of each function, method and
    class defined in a cell, is written as the notebook-namespace marker
    (nspickle), so a function costs only its code, defaults and closure. None
    pickles exactly like dill.dump.

    deadline: a SaveDeadline (the suspend save) checked before each variable
    and between spool-write slices. When it runs out, the spool file being
    written and every blob built so far are deleted and SaveCancelled is
    raised; a cancel is never recorded as a skipped variable.

    By-name references (with `namespace`, see nspickle RefMap): a class,
    function or aliasable value (list, dict, DataFrame, notebook-class
    instance, ...) bound at top level is pickled by value only in its own
    (canonical) variable; every other variable writes a reference to it by
    name, and a closure cell shared by two top-level functions is written as
    a reference to the function that owns it. Such a blob gets "refs": [names
    it references] (the restore loads those first) and, when a reference
    cycle had to be broken, "by_value": [names embedded by value instead].
    The canonical variables are pickled first; one that is not saved is
    dropped from the map and the variables that referenced it are pickled
    again with it by value. A cycle between two of them (X references Y and Y
    references X) is broken by pickling X again with Y embedded by value.

    prev_manifest: the previous save's manifest "vars" ({name: entry}); a
    value keeps the canonical name it had there (nspickle._value_owner), so
    adding or deleting an alias does not re-upload the value.
    """
    from . import nspickle
    blobs: dict = {}
    skipped_by_name: dict = {}
    refs = (nspickle.build_ref_map(namespace_to_save, namespace, prev=prev_manifest)
            if namespace is not None else None)
    excludes: dict = {}

    def _pickle_var(name, value) -> None:
        """Pickle one variable into blobs[name], or record it in skipped_by_name."""
        exclude = excludes.get(name, ())
        if spool_dir is None:
            try:
                import io
                buf = io.BytesIO()
                p = nspickle.NamespacePickler(buf, namespace, protocol=PICKLE_PROTOCOL, refs=refs,
                                              self_name=name, exclude=exclude)
                p.dump(value)
                b = buf.getvalue()
                blobs[name] = _with_refs({"bytes": b, "hash": _hash_bytes(b), "size": len(b)}, p)
            except Exception as e:
                skipped_by_name[name] = f"{type(value).__name__}: {e}"
            return
        need = estimate_value_bytes(value)
        _globs = getattr(value, "__globals__", None) if callable(value) else None
        if _globs is not None and _globs is not namespace:
            # dill pickles a function whose globals are not the notebook namespace
            # together with a copy of those globals. A function bound to
            # `namespace` writes it as the marker and costs only its code,
            # defaults and closure.
            need += sum(estimate_value_bytes(v) for k, v in namespace_to_save.items() if k != name)
        reason = _preflight(name, need, mem_available, disk_free, spool_dir)
        if reason:
            skipped_by_name[name] = reason
            return
        sink = _HashingSpool(spool_dir, deadline=deadline)
        try:
            p = nspickle.NamespacePickler(sink, namespace, protocol=PICKLE_PROTOCOL, refs=refs,
                                          self_name=name, exclude=exclude)
            p.dump(value)
            blobs[name] = _with_refs(sink.finish(), p)
        except SaveCancelled:
            sink.discard()
            raise
        except MemoryError:
            sink.discard()
            est = f", estimated {need / _MIB:.0f} MiB" if need >= _MIB else ""
            skipped_by_name[name] = f"{NOT_ENOUGH_MEMORY} '{name}' (ran out of memory while pickling{est})"
        except OSError as e:
            sink.discard()
            import errno
            if getattr(e, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
                skipped_by_name[name] = f"{NOT_ENOUGH_DISK} '{name}' ({spool_dir} is full)"
            else:
                skipped_by_name[name] = f"{type(value).__name__}: {e}"
        except Exception as e:
            sink.discard()
            skipped_by_name[name] = f"{type(value).__name__}: {e}"

    def _repickle(names) -> None:
        for n in sorted(names):
            check(deadline, "serialize")
            old = blobs.pop(n, None)
            if old is not None:
                release_blobs({n: old})
            skipped_by_name.pop(n, None)
            _pickle_var(n, namespace_to_save[n])

    # Canonical (referenced) variables first, so one that cannot be saved is
    # known before the variables that reference it.
    names = list(namespace_to_save)
    if refs:
        names.sort(key=lambda n: refs.canonical(namespace_to_save[n]) != n)
    try:
        for name in names:
            check(deadline, "serialize")
            _pickle_var(name, namespace_to_save[name])
        dropped: set = set()
        while refs:
            # A canonical variable that was not saved: nothing may reference
            # it by name (the reference could not be resolved).
            lost = {n for n in skipped_by_name
                    if n not in dropped and refs.canonical(namespace_to_save[n]) == n}
            for n in lost:
                refs.drop(n)
            dropped |= lost
            redo = {n for n, b in blobs.items() if lost.intersection(b.get("refs", ()))}
            if redo:
                _repickle(redo)
                continue
            edge = _find_ref_cycle_edge(blobs)
            if edge is None:
                break
            x, y = edge
            tries = [(x, y)]
            if x in blobs[y].get("refs", ()):
                tries.append((y, x))
            for a, b in tries:
                # `a` embeds `b` by value; dill cannot do that for every pair
                # (two classes whose class attributes reference each other).
                excludes.setdefault(a, set()).add(b)
                _repickle({a})
                if a in blobs:
                    break
                excludes[a].discard(b)
                _repickle({a})
            else:
                # Neither side can hold the other by value: not saved (they
                # could not be saved before by-name references either).
                for n in (x, y):
                    old = blobs.pop(n, None)
                    if old is not None:
                        release_blobs({n: old})
                    skipped_by_name[n] = (f"{type(namespace_to_save[n]).__name__}: reference cycle "
                                          f"between '{x}' and '{y}' that cannot be pickled")
    except SaveCancelled:
        release_blobs(blobs)
        raise
    blobs = {n: blobs[n] for n in namespace_to_save if n in blobs}
    skipped = [(n, skipped_by_name[n]) for n in namespace_to_save if n in skipped_by_name]
    return blobs, skipped


def _with_refs(blob: dict, pickler) -> dict:
    """Add the by-name references a NamespacePickler wrote ("refs") and the
    ones it embedded by value to break a cycle ("by_value") to `blob`."""
    if pickler.found_refs:
        blob["refs"] = sorted(pickler.found_refs)
    if pickler.embedded:
        blob["by_value"] = sorted(pickler.embedded)
    return blob


def reference_order(names, refs_of: dict) -> list:
    """`names` ordered so each one comes after the names it references
    (refs_of: {name: [referenced names]}; references outside `names` are
    ignored). Ties keep sorted order; a cycle is cut where it is met (the
    restore resolves a reference on demand anyway)."""
    pool = set(names)
    out: list = []
    state: dict = {}
    for root in sorted(pool):
        if state.get(root):
            continue
        stack = [(root, iter(sorted(r for r in refs_of.get(root, ()) if r in pool)))]
        state[root] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                out.append(node)
                stack.pop()
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(sorted(r for r in refs_of.get(nxt, ()) if r in pool))))
    return out


def _find_ref_cycle_edge(blobs: dict):
    """(x, y) for one reference x -> y that closes a cycle in the "refs" graph
    of `blobs` (y is being visited when x references it), else None. Names are
    visited in sorted order, so the result is stable: y is loaded later than x
    by the restore, and x is the one that embeds y by value."""
    graph = {n: [r for r in b.get("refs", ()) if r in blobs] for n, b in blobs.items()}
    state: dict = {}   # name -> 1 visiting, 2 done
    for root in sorted(graph):
        if state.get(root):
            continue
        stack = [(root, iter(graph[root]))]
        state[root] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                continue
            s = state.get(nxt)
            if s == 1:
                return node, nxt
            if s is None:
                state[nxt] = 1
                stack.append((nxt, iter(graph[nxt])))
    return None


def release_blobs(blobs: dict) -> None:
    """Delete the spool files of streamed blobs. Never raises."""
    for meta in blobs.values():
        p = meta.get("path") if isinstance(meta, dict) else None
        if p:
            try:
                os.unlink(p)
            except OSError:
                pass


def not_saved_kind(reason: str) -> str:
    """'memory', 'disk' or 'unpicklable' for a build_var_blobs skip reason."""
    if reason.startswith(NOT_ENOUGH_MEMORY):
        return "memory"
    if reason.startswith(NOT_ENOUGH_DISK):
        return "disk"
    return "unpicklable"


def compute_delta(blobs: dict, prev_manifest: dict) -> dict:
    """Compare freshly-built blobs against the previous manifest.

    prev_manifest: {name: {"hash":..., "size":..., "object": <s3 key suffix>}}

    Returns a plan:
      {
        "changed":   [names to (re)upload — new or hash differs],
        "unchanged": [names whose hash matches prev — skip upload, keep manifest entry],
        "deleted":   [names in prev_manifest but not in blobs — remove object],
        "manifest":  {name: {"hash","size"}}  # the NEW full manifest
      }
    """
    changed, unchanged = [], []
    new_manifest: dict = {}
    for name, meta in blobs.items():
        new_manifest[name] = {"hash": meta["hash"], "size": meta["size"]}
        prev = prev_manifest.get(name)
        if prev and prev.get("hash") == meta["hash"]:
            unchanged.append(name)
        else:
            changed.append(name)
    deleted = [n for n in prev_manifest.keys() if n not in blobs]
    return {
        "changed": changed,
        "unchanged": unchanged,
        "deleted": deleted,
        "manifest": new_manifest,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  DELTA FILE CHECKPOINT CORE (per-file objects + manifest)
#
#  Same idea as the variable delta, but for /tmp data files. Change detection is
#  even cheaper than for vars: the OS already tracks (size, mtime), so we don't
#  have to read a file to know whether it changed. Only changed/new files are
#  re-uploaded; removed files' objects are deleted.
#
#  Signature = (size, mtime_ns). This is what rsync-style tools use. It can be
#  fooled by a same-size same-mtime rewrite (rare in notebook use); callers may
#  opt into a content hash for the safe tier. These functions are PURE (no S3).
# ─────────────────────────────────────────────────────────────────────────────

# Files whose size is <= this are batched into one object instead of getting
# their own S3 object (avoids a PUT-per-tiny-file).
DELTA_SMALL_FILE_BYTES = 65536  # 64 KB


def file_signature(path: str) -> tuple:
    """Cheap change signature for a file: (size, mtime_ns). os.stat only — no read."""
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


def compute_file_delta(disk_files: dict, prev_manifest: dict) -> dict:
    """Decide which files to (re)upload / delete, given current disk state.

    disk_files:   {name: {"path": abs_path, "size": int, "mtime_ns": int}}
    prev_manifest:{name: {"size": int, "mtime_ns": int, "object": <key suffix>}}

    Returns:
      {
        "changed":   [names to (re)upload — new or (size,mtime) differs],
        "unchanged": [names whose signature matches prev — skip],
        "deleted":   [names in prev but no longer on disk — remove object],
        "manifest":  {name: {"size","mtime_ns"}}  # NEW full manifest (objects added by caller)
      }
    """
    changed, unchanged = [], []
    new_manifest: dict = {}
    for name, meta in disk_files.items():
        new_manifest[name] = {"size": meta["size"], "mtime_ns": meta["mtime_ns"]}
        prev = prev_manifest.get(name)
        if prev and prev.get("size") == meta["size"] and prev.get("mtime_ns") == meta["mtime_ns"]:
            unchanged.append(name)
        else:
            changed.append(name)
    deleted = [n for n in prev_manifest.keys() if not n.startswith("__") and n not in disk_files]
    return {
        "changed": changed,
        "unchanged": unchanged,
        "deleted": deleted,
        "manifest": new_manifest,
    }
