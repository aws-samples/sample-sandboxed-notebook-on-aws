"""
User-installed packages for the Packages panel (/package-categories).

The badge and the "User installed (n)" chip used to read only the proxy's
in-memory MicrovmManager._user_installed dict, filled by the panel's own
/track-install. That dict is empty after a proxy restart and never filled by a
restore, a cell's install_package(...) or the AI agent's install. The Live
MicroVMs card reads the VM's own list instead, which every install path records
and restore reloads from S3. This module derives the panel's set from that same
durable list:

  1. the session's VM, when the control plane says it is RUNNING right now:
     GET /vm-status `packages` (bounded to VM_STATUS_TIMEOUT_SEC). A suspended VM
     is never called (it would auto-resume on traffic);
  2. otherwise S3 sessions/<sid>/requirements.txt (the VM flushes it at every
     install/uninstall and save);
  3. merged with the in-memory dict (covers persistence mode 'none', where the VM
     writes nothing to S3, and the instant after a panel install).

The result is cached per session for CACHE_SECONDS and dropped on any
install/uninstall through the proxy (invalidate()).

Names are emitted in every PEP 503 spelling (original, `-`, `_`) so the web
panel's lower-case match against the VM's `pip list` names works for
scikit_learn vs scikit-learn without a web change. The response shape is the
existing [{package, category}].
"""
import asyncio
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

VM_STATUS_TIMEOUT_SEC = 3.0
CLASSIFY_TIMEOUT_SEC = 2.0
CACHE_SECONDS = 10.0

# {session_id: (monotonic_ts, [base names] | None, source)}
_CACHE: dict[str, tuple] = {}

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def canonical_name(name: str) -> str:
    """PEP 503 normalized name: lower-case, runs of -, _ and . become one '-'."""
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


def base_name(spec) -> str | None:
    """Distribution name of a requirement line ('statsmodels==0.15.0',
    'pkg[extra]>=1', 'pkg @ git+...'); None for blanks, comments and options."""
    s = str(spec or "").strip()
    if not s or s.startswith("#") or s.startswith("-"):
        return None
    m = _NAME_RE.match(s)
    return m.group(1) if m else None


def name_variants(name: str) -> list[str]:
    """The given spelling plus its '-' and '_' PEP 503 forms (deduped, ordered)."""
    canon = canonical_name(name)
    out = []
    for v in (str(name), canon, canon.replace("-", "_")):
        if v and v.lower() not in [o.lower() for o in out]:
            out.append(v)
    return out


def invalidate(session_id: str | None) -> None:
    """Drop a session's cached list (after an install/uninstall)."""
    if session_id:
        _CACHE.pop(session_id, None)


def note_install_forward(vm_manager, session_id: str | None, body: bytes, resp) -> None:
    """After a POST /proxy/install (install or uninstall) was forwarded: drop the
    session's cached list, and on a successful uninstall drop the package from
    the in-memory record so it leaves the set at once."""
    if not session_id:
        return
    invalidate(session_id)
    try:
        req = json.loads(body or b"{}")
    except Exception:
        return
    if not (isinstance(req, dict) and req.get("uninstall") and req.get("package")):
        return
    if getattr(resp, "status_code", None) != 200:
        return
    try:
        data = json.loads(getattr(resp, "body", b"") or b"{}")
    except Exception:
        return
    if isinstance(data, dict) and data.get("success"):
        vm_manager.record_user_uninstall(session_id, str(req["package"]))


async def durable_names(vm_manager, session_id: str, *, vm_status_fetch, s3_read,
                        monotonic=time.monotonic) -> tuple[list[str] | None, str]:
    """(base names, source) from the VM's own list: 'vm' (live /vm-status),
    'checkpoint' (S3 requirements.txt), or (None, 'none') when neither is
    available. Cached CACHE_SECONDS per session. Never raises."""
    now = monotonic()
    hit = _CACHE.get(session_id)
    if hit is not None and now - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    names, source = None, "none"
    vm_id = None
    try:
        sv = await vm_manager.get_session_vm_async(session_id)
        vm_id = (sv or {}).get("vm_id")
    except Exception:
        vm_id = None
    if vm_id:
        try:
            local = vm_manager.active_microvms.get(vm_id) or {}
            healthy = (local.get("_502_strikes") or 0) < 3
            state = await vm_manager.get_vm_state(vm_id) if healthy else None
        except Exception:
            state = None
        if state == "RUNNING":
            try:
                live = await asyncio.wait_for(vm_status_fetch(vm_id), VM_STATUS_TIMEOUT_SEC)
            except Exception:
                live = None
            if isinstance(live, dict) and isinstance(live.get("packages"), list):
                names, source = [n for n in map(base_name, live["packages"]) if n], "vm"
    if names is None:
        try:
            s3 = await s3_read(session_id)
            reqs = (s3 or {}).get("requirements")
        except Exception as e:
            logger.debug(f"user packages S3 read failed for {session_id}: {type(e).__name__}")
            reqs = None
        if reqs is not None:
            names, source = [n for n in map(base_name, reqs) if n], "checkpoint"
    _CACHE[session_id] = (now, names, source)
    return names, source


async def _category(name: str, known: dict) -> str:
    if known.get(canonical_name(name)):
        return known[canonical_name(name)]
    from proxy.platform.package_classifier import classify_and_cache
    try:
        return await asyncio.wait_for(classify_and_cache(name), CLASSIFY_TIMEOUT_SEC)
    except Exception:
        return "Other"


async def user_installed(vm_manager, session_id: str, *, vm_status_fetch, s3_read,
                         monotonic=time.monotonic) -> list[dict]:
    """[{package, category}] for /package-categories: the durable list merged
    with the in-memory dict, each name in all of its PEP 503 spellings."""
    memory = list(vm_manager.get_user_installed_packages(session_id) or [])
    names, _source = await durable_names(vm_manager, session_id, vm_status_fetch=vm_status_fetch,
                                         s3_read=s3_read, monotonic=monotonic)
    known = {canonical_name(p.get("package")): p.get("category") for p in memory if p.get("package")}
    ordered: dict[str, str] = {}   # canonical → first spelling seen
    for n in list(names or []) + [p.get("package") for p in memory if p.get("package")]:
        ordered.setdefault(canonical_name(n), n)
    out = []
    for canon, spelling in ordered.items():
        category = await _category(spelling, known)
        for v in name_variants(spelling):
            out.append({"package": v, "category": category})
    return out
