#!/usr/bin/env python3
# test-kind: offline
"""
Packages panel user-installed set (proxy/platform/user_packages.py), the source of
/package-categories `user_installed` (the "user" badge and "User installed (n)").
Pure fakes, asyncio.run, no network:
  - RUNNING VM: the set comes from the VM's /vm-status `packages`.
  - VM not RUNNING (suspended / no VM) or /vm-status failing: S3 requirements.txt.
  - Proxy restart (empty in-memory dict) still flags the restored package.
  - A panel install (/track-install) is flagged at once, even over a cached list.
  - A successful uninstall forwarded through /proxy/install unflags; a failed one
    does not.
  - name==ver vs name, case and PEP 503 spellings (scikit_learn / scikit-learn)
    match the web's lower-case check; one entry group per distribution.
  - Short-TTL cache: no second VM call inside CACHE_SECONDS.
Run:
    .venv/bin/python tests/proxy/test_user_packages.py
"""
import asyncio
import json
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from proxy.platform import package_classifier  # noqa: E402
from proxy.platform import user_packages as up  # noqa: E402
from proxy.platform.microvm_manager import MicrovmManager  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


SID = "sess-1"
VM = "microvm-abc"


async def _fake_classify(name):
    return {"statsmodels": "Statistics"}.get(name.lower(), "Other")


package_classifier.classify_and_cache = _fake_classify


class FakeManager:
    """The MicrovmManager surface user_packages uses, with the real dict methods."""
    record_user_install = MicrovmManager.record_user_install
    record_user_uninstall = MicrovmManager.record_user_uninstall
    get_user_installed_packages = MicrovmManager.get_user_installed_packages

    def __init__(self, vm_id=VM, state="RUNNING"):
        self._user_installed = {}
        self.active_microvms = {vm_id: {}} if vm_id else {}
        self.vm_id, self.state = vm_id, state
        self.state_calls = 0

    async def get_session_vm_async(self, sid):
        return {"vm_id": self.vm_id, "endpoint": "x"} if self.vm_id else None

    async def get_vm_state(self, mid):
        self.state_calls += 1
        return self.state


class Sources:
    def __init__(self, live=None, reqs=None, slow=False):
        self.live, self.reqs, self.slow = live, reqs, slow
        self.vm_calls = self.s3_calls = 0

    async def vm_status_fetch(self, mid):
        self.vm_calls += 1
        if self.slow:
            await asyncio.sleep(10)
        return None if self.live is None else {"packages": list(self.live), "packages_total": len(self.live)}

    async def s3_read(self, sid):
        self.s3_calls += 1
        return {"metadata": None, "requirements": None if self.reqs is None else list(self.reqs)}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def run(mgr, src, clock=None):
    return asyncio.run(up.user_installed(mgr, SID, vm_status_fetch=src.vm_status_fetch,
                                         s3_read=src.s3_read, monotonic=clock or Clock()))


def web_set(entries):
    """packageFilter.js: new Set(user_installed.map(p => p.package.toLowerCase()))."""
    return {str(p["package"]).lower() for p in entries}


def web_count(vm_packages, entries):
    s = web_set(entries)
    return sum(1 for p in vm_packages if p["name"].lower() in s)


VM_LIST = [{"name": "numpy", "version": "2.0"}, {"name": "statsmodels", "version": "0.15.0"},
           {"name": "pandas", "version": "2.2"}]


def test_from_vm_status():
    up._CACHE.clear()
    mgr, src = FakeManager(), Sources(live=["statsmodels==0.15.0"], reqs=["should-not-be-read==1"])
    out = run(mgr, src)
    check("RUNNING VM: statsmodels flagged from /vm-status", "statsmodels" in web_set(out))
    check("RUNNING VM: category from the classifier", any(p["category"] == "Statistics" for p in out))
    check("RUNNING VM: S3 not read", src.s3_calls == 0)
    check("RUNNING VM: web count is 1", web_count(VM_LIST, out) == 1)
    check("response shape is [{package, category}]",
          all(set(p) == {"package", "category"} for p in out))


def test_fallback_s3():
    up._CACHE.clear()
    mgr, src = FakeManager(state="SUSPENDED"), Sources(live=["x==1"], reqs=["# comment", "statsmodels==0.15.0", ""])
    out = run(mgr, src)
    check("SUSPENDED: VM never called", src.vm_calls == 0)
    check("SUSPENDED: flagged from S3 requirements.txt", web_set(out) == {"statsmodels"})
    up._CACHE.clear()
    mgr, src = FakeManager(vm_id=None), Sources(reqs=["statsmodels==0.15.0"])
    check("no VM for the session: S3 used", "statsmodels" in web_set(run(mgr, src)))
    up._CACHE.clear()
    mgr, src = FakeManager(), Sources(live=["a==1"], reqs=["statsmodels==0.15.0"], slow=True)
    orig = up.VM_STATUS_TIMEOUT_SEC
    up.VM_STATUS_TIMEOUT_SEC = 0.05
    try:
        out = run(mgr, src)
    finally:
        up.VM_STATUS_TIMEOUT_SEC = orig
    check("slow /vm-status: falls back to S3", web_set(out) == {"statsmodels"})
    up._CACHE.clear()
    mgr = FakeManager()
    mgr.active_microvms[VM] = {"_502_strikes": 3}
    src = Sources(live=["a==1"], reqs=["statsmodels==0.15.0"])
    out = run(mgr, src)
    check("unhealthy VM (3 strikes): no state/VM call, S3 used",
          mgr.state_calls == 0 and src.vm_calls == 0 and web_set(out) == {"statsmodels"})
    up._CACHE.clear()
    mgr, src = FakeManager(state="SUSPENDED"), Sources(reqs=None)
    check("nothing durable and empty dict: empty list", run(mgr, src) == [])


def test_proxy_restart():
    up._CACHE.clear()
    mgr = FakeManager()   # fresh process: empty _user_installed
    src = Sources(live=["statsmodels==0.15.0"])
    out = run(mgr, src)
    check("proxy restart (empty dict): restored package still flagged",
          mgr._user_installed == {} and "statsmodels" in web_set(out))


def test_install_flags_immediately():
    up._CACHE.clear()
    mgr, src, clock = FakeManager(), Sources(live=[]), Clock()
    run(mgr, src, clock)   # caches an empty VM list
    # /track-install: record + invalidate (same as proxy/server.py track_install)
    mgr.record_user_install(SID, "seaborn", "Visualization")
    up.invalidate(SID)
    out = run(mgr, src, clock)
    check("panel install flagged immediately", "seaborn" in web_set(out))
    check("panel install keeps its category",
          any(p["package"] == "seaborn" and p["category"] == "Visualization" for p in out))
    # Even with a stale cache entry (no invalidate) the in-memory record is merged.
    mgr.record_user_install(SID, "plotly", "Visualization")
    check("in-memory record merged over a cached list", "plotly" in web_set(run(mgr, src, clock)))
    # Forwarded install invalidates the cache, so a cell/agent install shows on next load.
    src.live = ["polars==1.0"]
    up.note_install_forward(mgr, SID, json.dumps({"package": "polars"}).encode(),
                            types.SimpleNamespace(status_code=200, body=b'{"success": true}'))
    check("forwarded install invalidates the cache", "polars" in web_set(run(mgr, src, clock)))


def test_uninstall_unflags():
    up._CACHE.clear()
    mgr, src, clock = FakeManager(), Sources(live=["statsmodels==0.15.0"]), Clock()
    mgr.record_user_install(SID, "statsmodels", "Statistics")
    check("before uninstall: flagged", "statsmodels" in web_set(run(mgr, src, clock)))
    body = json.dumps({"package": "statsmodels", "uninstall": True}).encode()
    up.note_install_forward(mgr, SID, body,
                            types.SimpleNamespace(status_code=200, body=b'{"success": false, "error": "x"}'))
    check("failed uninstall keeps the record", mgr.get_user_installed_packages(SID) != [])
    src.live = []   # the VM dropped it from its list
    up.note_install_forward(mgr, SID, body, types.SimpleNamespace(status_code=200, body=b'{"success": true}'))
    out = run(mgr, src, clock)
    check("successful uninstall drops the in-memory record", mgr.get_user_installed_packages(SID) == [])
    check("successful uninstall unflags", "statsmodels" not in web_set(out))
    mgr.record_user_install(SID, "Scikit_Learn", "ML")
    mgr.record_user_uninstall(SID, "scikit-learn==1.5.0")
    check("uninstall matches across spelling and pin", mgr.get_user_installed_packages(SID) == [])


def test_names():
    check("base_name strips pins/extras/urls",
          [up.base_name(s) for s in ("statsmodels==0.15.0", "pkg[extra]>=1", "foo @ git+https://x",
                                     "-e .", "# c", "", "Bar.Baz~=2")]
          == ["statsmodels", "pkg", "foo", None, None, None, "Bar.Baz"])
    check("canonical_name is PEP 503",
          up.canonical_name("Scikit__Learn.x") == "scikit-learn-x")
    up._CACHE.clear()
    mgr, src = FakeManager(), Sources(live=["scikit_learn==1.5.0", "Statsmodels==0.15.0"])
    mgr.record_user_install(SID, "scikit-learn", "ML")
    mgr.record_user_install(SID, "statsmodels", "Statistics")
    out = run(mgr, src)
    s = web_set(out)
    check("scikit_learn on VM matches pip-list 'scikit-learn' and 'scikit_learn'",
          {"scikit-learn", "scikit_learn"} <= s)
    check("name==ver on VM and bare name in memory: one group, no duplicates",
          len(out) == len({(p["package"]) for p in out}))
    check("case-insensitive: Statsmodels==0.15.0 flags 'statsmodels'", "statsmodels" in s)
    check("in-memory category reused for the same distribution",
          all(p["category"] == "ML" for p in out if "scikit" in p["package"].lower()))
    vm = [{"name": "scikit-learn", "version": "1.5.0"}, {"name": "statsmodels", "version": "0.15.0"},
          {"name": "numpy", "version": "2"}]
    check("web count counts each listed package once", web_count(vm, out) == 2)


def test_cache():
    up._CACHE.clear()
    mgr, src, clock = FakeManager(), Sources(live=["statsmodels==0.15.0"]), Clock()
    run(mgr, src, clock)
    run(mgr, src, clock)
    check("within TTL: one VM call", src.vm_calls == 1)
    clock.t += up.CACHE_SECONDS + 1
    run(mgr, src, clock)
    check("after TTL: refetched", src.vm_calls == 2)


def main():
    for fn in (test_from_vm_status, test_fallback_s3, test_proxy_restart, test_install_flags_immediately,
               test_uninstall_unflags, test_names, test_cache):
        print(fn.__name__)
        fn()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    sys.exit(main())
