#!/usr/bin/env python3
# test-kind: offline
"""Pure restore helpers: resolve_restore_choice and normalize_restore_report.
Run: .venv/bin/python tests/proxy/test_restore_choice.py
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from proxy.platform.restore_choice import resolve_restore_choice, normalize_restore_report, latest_vm_session  # noqa: E402

_checks = []
def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")

ARCHES = ["arm64", "x86_64"]
TIERS = {"arm64": [1024, 2048, 4096, 8192], "x86_64": [1024, 2048, 4096]}
def valid_for(a):
    return TIERS[a]
def rc(body):
    return resolve_restore_choice(body, "x86_64", 1024, ARCHES, valid_for)

check("no flag: saved values", rc({"arch": "arm64", "memoryMiB": 2048}) == ("x86_64", 1024, None))
check("flag not True: saved values", rc({"restoreOverride": "true", "arch": "arm64"}) == ("x86_64", 1024, None))
check("flag 1: saved values", rc({"restoreOverride": 1, "arch": "arm64"}) == ("x86_64", 1024, None))
check("override both", rc({"restoreOverride": True, "arch": "arm64", "memoryMiB": 8192}) == ("arm64", 8192, None))
check("missing arch falls back to saved", rc({"restoreOverride": True, "memoryMiB": 2048}) == ("x86_64", 2048, None))
check("missing memory falls back to saved", rc({"restoreOverride": True, "arch": "arm64"}) == ("arm64", 1024, None))
check("unknown arch is an error", rc({"restoreOverride": True, "arch": "riscv"})[2] is not None)
check("tier not offered for arch is an error (no snapping)",
      rc({"restoreOverride": True, "arch": "x86_64", "memoryMiB": 8192})[2] is not None)
check("non-int memory is an error", rc({"restoreOverride": True, "memoryMiB": "2048"})[2] is not None)
check("bool memory is an error", rc({"restoreOverride": True, "memoryMiB": True})[2] is not None)
check("error returns the saved values", rc({"restoreOverride": True, "arch": "riscv"})[:2] == ("x86_64", 1024))
check("None body is safe", resolve_restore_choice(None, "arm64", 2048, ARCHES, valid_for) == ("arm64", 2048, None))

n = normalize_restore_report
ok = n({"verified": True, "arch": "arm64", "python": "3.12", "packages_total": 4,
        "packages_failed": [{"name": "a", "reason": "r"}, "b", {"reason": "x"}, 7]})
check("verified report keeps failures", ok["verified"] is True and ok["packages_total"] == 4
      and ok["packages_failed"] == [{"name": "a", "reason": "r"}, {"name": "b", "reason": ""}])
check("no failures -> empty list", n({"verified": True, "packages_total": 0})["packages_failed"] == [])
check("None -> verified false", n(None)["verified"] is False)
check("string -> verified false", n("x")["verified"] is False)
check("verified false keeps the reason", n({"verified": False, "reason": "S3 down"}) == {"verified": False, "reason": "S3 down"})
check("verified missing -> verified false", n({"packages_total": 2})["verified"] is False)
check("bad total falls back to failed count", n({"verified": True, "packages_total": "x",
      "packages_failed": [{"name": "a"}]})["packages_total"] == 1)
check("bad packages_failed type does not raise", n({"verified": True, "packages_failed": 5})["verified"] in (True, False))

# A pool-granted VM keeps its MINT launched_at, so launched_at order alone picks the wrong row.
rows = [
    {"microvm_id": "pooled", "memory_mib": 2048, "launched_at": "2026-01-01T10:00:00+00:00", "terminated_at": "2026-01-01T12:00:00+00:00"},
    {"microvm_id": "cold", "memory_mib": 1024, "launched_at": "2026-01-01T11:00:00+00:00", "terminated_at": "2026-01-01T11:30:00+00:00"},
]
check("latest row is the last terminated, not the last launched", latest_vm_session(rows)["microvm_id"] == "pooled")
check("a running row (no terminated_at) is the latest",
      latest_vm_session(rows + [{"microvm_id": "live", "launched_at": "2026-01-01T09:00:00+00:00", "terminated_at": None}])["microvm_id"] == "live")
check("empty list -> None", latest_vm_session([]) is None and latest_vm_session(None) is None)
# restore_memory_assessment: absolute checkpoint sizes (KB, as metadata.json
# checkpoint_total_kb) against the chosen tier. need = size x 2; ratio = need / tier.
try:
    from proxy.platform.restore_choice import (  # noqa: E402
        restore_memory_assessment, RESTORE_MEMORY_FACTOR, RESTORE_WARN_RATIO, RESTORE_STRONG_RATIO)
except ImportError as e:
    restore_memory_assessment = None
    check(f"restore_memory_assessment importable ({e})", False)
MB, GB = 1024, 1024 * 1024
CASES = [
    ("1.5 GB into 1 GB", 1572864, 1024, "strong", 3072, 3.0),
    ("600 MB into 1 GB", 600 * MB, 1024, "strong", 1200, 1.17),
    ("300 MB into 1 GB", 300 * MB, 1024, "warn", 600, 0.59),
    ("200 MB into 1 GB", 200 * MB, 1024, "none", 400, 0.39),
    ("1.5 GB into 2 GB", int(1.5 * GB), 2048, "strong", 3072, 1.5),
    ("1.5 GB into 4 GB", int(1.5 * GB), 4096, "warn", 3072, 0.75),
    ("1.5 GB into 8 GB", int(1.5 * GB), 8192, "none", 3072, 0.38),
]
if restore_memory_assessment is not None:
    a = restore_memory_assessment
    for label, kb, tier, level, need, ratio in CASES:
        r = a(kb, tier)
        check(f"assessment {label}: {level}, need {need} MiB, ratio {ratio}",
              r["level"] == level and r["need_mib"] == need and r["tier_mib"] == tier
              and r["checkpoint_kb"] == kb and abs(r["ratio"] - ratio) < 0.01)
    for kb in (None, 0, -1, "x", True, float("nan")):
        r = a(kb, 1024)
        check(f"assessment unknown size {kb!r}: level unknown, no need/ratio",
              r["level"] == "unknown" and r["need_mib"] is None and r["ratio"] is None and r["tier_mib"] == 1024)
    check("assessment unknown tier: level unknown", a(1572864, None)["level"] == "unknown"
          and a(1572864, 0)["level"] == "unknown" and a(1572864, True)["level"] == "unknown")
    check("assessment boundaries: ratio 0.5 is warn, 0.9 is warn, just above 0.9 is strong",
          a(256 * MB, 1024)["level"] == "warn"
          and a(int(460.8 * MB), 1024)["level"] == "warn"
          and a(470 * MB, 1024)["level"] == "strong"
          and a(255 * MB, 1024)["level"] == "none")
    check("proxy constants are 2 / 0.5 / 0.9",
          (RESTORE_MEMORY_FACTOR, RESTORE_WARN_RATIO, RESTORE_STRONG_RATIO) == (2, 0.5, 0.9))
    # One rule in two places: the web dialog (web/src/services/restoreChoice.js)
    # must use the same numbers as the proxy.
    import re  # noqa: E402
    js_path = os.path.join(os.path.dirname(__file__), "..", "..", "web", "src", "services", "restoreChoice.js")
    with open(js_path, encoding="utf-8") as fh:
        js = fh.read()
    def _js_const(name):
        m = re.search(rf"export const {name}\s*=\s*([0-9.]+)", js)
        return float(m.group(1)) if m else None
    check("web constants == proxy constants (factor, warn, strong)",
          _js_const("RESTORE_MEMORY_FACTOR") == RESTORE_MEMORY_FACTOR
          and _js_const("RESTORE_WARN_RATIO") == RESTORE_WARN_RATIO
          and _js_const("RESTORE_STRONG_RATIO") == RESTORE_STRONG_RATIO)
passed = sum(1 for _, o in _checks if o)
print(f"RESULT: {passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
