"""Installed-package list for GET /packages, cached on a site-packages fingerprint.

Enumerating importlib.metadata.distributions() reads every METADATA file on
sys.path. On a freshly restored VM the page cache is cold, so the same walk the
UI asks for several times in its post-restore burst costs seconds each time.

The list is cached against fingerprint(): for every sys.path directory its mtime,
the number of *.dist-info / *.egg-info / *.egg-link entries and a hash of their
names, their own mtimes and the mtime and size of their METADATA / PKG-INFO file,
plus pip_helper.pip_generation(). Any install, uninstall or upgrade (by the
/install route, the install_package helper, `!pip` in a cell, a terminal pip, or
the restore reinstall) adds, removes or renames a metadata directory or writes
into one, so the fingerprint changes and the next call rebuilds. The hash catches
a change that lands in the same parent-directory mtime tick, and the METADATA
stat catches pip writing METADATA into a dist-info dir it created a moment
earlier. Only stat calls: no file is read to build the key.

A list built while pip holds pip_lock (odd generation) is never stored. A list
built while a *.dist-info dir has no METADATA (or an *.egg-info dir no PKG-INFO,
or the file is empty) is an install still being written, or a broken leftover:
it is returned but kept for at most INCOMPLETE_TTL_SEC, so the next calls re-read
without paying a full walk on every call when the leftover is permanent.
"""
import hashlib
import importlib
import os
import sys
import threading
import time

from app.platform.pip_helper import pip_generation

_META_SUFFIXES = (".dist-info", ".egg-info", ".egg-link")
INCOMPLETE_TTL_SEC = 1.0

_lock = threading.Lock()
_cache = None  # (fingerprint, list[dict], expires_at or None) or None
_clock = time.monotonic  # seam for tests


def _meta_stat(d: str, entry) -> tuple:
    """(own mtime_ns, metadata-file mtime_ns, metadata-file size, incomplete)
    for one metadata entry, from stat calls only."""
    try:
        own = entry.stat().st_mtime_ns
        is_dir = entry.is_dir()
    except OSError:
        return (None, None, -1, False)
    if not is_dir or entry.name.endswith(".egg-link"):
        return (own, None, -1, False)  # single-file egg-info, or egg-link
    meta = "METADATA" if entry.name.endswith(".dist-info") else "PKG-INFO"
    try:
        st = os.stat(os.path.join(d, entry.name, meta))
    except OSError:
        return (own, None, -1, True)
    return (own, st.st_mtime_ns, st.st_size, st.st_size == 0)


def _dir_entry(path: str) -> tuple:
    """(path, dir mtime_ns, metadata-entry count, hash, incomplete)."""
    d = path or "."
    try:
        mtime = os.stat(d).st_mtime_ns
        with os.scandir(d) as it:
            entries = sorted((e for e in it if e.name.endswith(_META_SUFFIXES)),
                             key=lambda e: e.name)
    except OSError:
        return (path, None, 0, "", False)
    h = hashlib.sha1()
    incomplete = False
    for e in entries:
        own, meta_mtime, meta_size, inc = _meta_stat(d, e)
        incomplete = incomplete or inc
        h.update(f"{e.name}\0{own}\0{meta_mtime}\0{meta_size}\n".encode("utf-8", "surrogateescape"))
    return (path, mtime, len(entries), h.hexdigest(), incomplete)


def fingerprint(paths=None) -> tuple:
    """A cheap key that changes whenever the installed distributions can have."""
    paths = list(sys.path) if paths is None else list(paths)
    return (tuple(paths), pip_generation(), tuple(_dir_entry(p) for p in paths))


def incomplete(fp: tuple) -> bool:
    """True when the fingerprint saw a metadata dir without its METADATA/PKG-INFO."""
    return any(e[4] for e in fp[2])


def _enumerate(paths) -> list[dict]:
    from importlib.metadata import distributions
    seen: dict[str, str] = {}
    for dist in distributions(path=paths):
        try:
            name = (dist.metadata["Name"] or "").strip()
        except Exception:
            name = ""
        if not name:
            continue
        ver = (dist.version or "").strip()
        # Dedupe by canonical name (multiple metadata dirs can shadow each other);
        # keep the first seen, which matches the resolver's import precedence.
        key = name.lower()
        if key not in seen:
            seen[key] = ver
    return [{"name": n, "version": v} for n, v in seen.items()]


def _copy(pkgs: list[dict]) -> list[dict]:
    return [dict(p) for p in pkgs]


def _hit(cached, fp) -> bool:
    return (cached is not None and cached[0] == fp
            and (cached[2] is None or _clock() < cached[2]))


def list_packages(paths=None) -> list[dict]:
    """The installed distributions as [{name, version}]. Thread-safe; concurrent
    callers on a stale cache share one rebuild."""
    global _cache
    paths = list(sys.path) if paths is None else list(paths)
    cached = _cache
    if _hit(cached, fingerprint(paths)):
        return _copy(cached[1])
    with _lock:
        fp_before = fingerprint(paths)
        cached = _cache
        if _hit(cached, fp_before):
            return _copy(cached[1])
        gen_before = fp_before[1]
        # Clears importlib.metadata's per-directory lookup cache, which is keyed
        # on the directory mtime and would miss a change in the same mtime tick.
        importlib.invalidate_caches()
        pkgs = _enumerate(paths)
        if gen_before % 2 == 0 and pip_generation() % 2 == 0:
            expires = _clock() + INCOMPLETE_TTL_SEC if incomplete(fp_before) else None
            _cache = (fp_before, pkgs, expires)
        return _copy(pkgs)


def warm() -> None:
    """Best-effort build of the cache (image build warmup)."""
    try:
        list_packages()
    except Exception:
        pass
