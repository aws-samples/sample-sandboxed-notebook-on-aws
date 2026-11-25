#!/usr/bin/env python3
# test-kind: offline
"""
pool_manager.config test suite (Phase 3 — FEAT-002, plan.md item 1 verify).

Exercises PoolConfig / load_config():
  - POOL_MAX_AGE_HOURS hard 2h clamp (5 -> 2)
  - size override precedence over POOL_SIZE_PER_IMAGE
  - default fallback for unlisted sizes
  - sizes_by_arch parsing from MICROVM_SIZES_JSON / IMAGE_SIZES / default
  - target_for(arch, size) including target 0

No AWS, no network. Pure env-var + dataclass unit tests.

Run:
    .venv/bin/python tests/pool/test_pool_config.py
"""

import importlib
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pool_manager.config as pool_config  # noqa: E402


# ── tiny test harness (matches tests/pool/test_pool_storage.py convention) ──
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


class EnvSandbox:
    """Snapshot os.environ on entry, restore on exit — config tests must not
    leak POOL_*/MICROVM_*/IMAGE_* vars into each other or the real process."""

    _KEYS = (
        "POOL_ENABLED", "POOL_SIZE_PER_IMAGE", "POOL_SIZE_OVERRIDES_ARM64",
        "POOL_SIZE_OVERRIDES_X86_64", "POOL_MAX_AGE_HOURS", "POOL_MANAGER_PORT", "POOL_FILL_CONCURRENCY",
        "POOL_VM_BACKEND", "IMAGE_ARCHES", "MICROVM_SIZES_JSON", "IMAGE_SIZES",
    )

    def __enter__(self):
        self._snapshot = dict(os.environ)
        for k in self._KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._snapshot)


def _reload():
    """Reload pool_manager.config so any module-level constants re-evaluate
    (defensive — current config.py reads env only inside functions, but this
    keeps the test robust to that implementation detail)."""
    return importlib.reload(pool_config)


# ── Section 1: POOL_MAX_AGE_HOURS clamp ────────────────────────────────────
def test_max_age_hours_clamp():
    with EnvSandbox():
        os.environ["POOL_MAX_AGE_HOURS"] = "5"
        cfg = _reload().load_config()
        check("POOL_MAX_AGE_HOURS=5 is clamped to the 2h ceiling",
              cfg.max_age_hours == 2.0)
        check("max_age_seconds reflects the clamped value (2h = 7200s)",
              cfg.max_age_seconds == 7200.0)

    with EnvSandbox():
        os.environ["POOL_MAX_AGE_HOURS"] = "2"
        cfg = _reload().load_config()
        check("POOL_MAX_AGE_HOURS=2 (exactly at ceiling) is left unclamped",
              cfg.max_age_hours == 2.0)

    with EnvSandbox():
        os.environ["POOL_MAX_AGE_HOURS"] = "0.5"
        cfg = _reload().load_config()
        check("POOL_MAX_AGE_HOURS=0.5 (under ceiling) is left unclamped",
              cfg.max_age_hours == 0.5)

    with EnvSandbox():
        # default (unset) must also respect the 2h ceiling
        cfg = _reload().load_config()
        check("default POOL_MAX_AGE_HOURS (unset) is <= 2h ceiling",
              cfg.max_age_hours <= 2.0)

    with EnvSandbox():
        os.environ["POOL_MAX_AGE_HOURS"] = "100"
        cfg = _reload().load_config()
        check("an extreme POOL_MAX_AGE_HOURS=100 is still clamped to 2h",
              cfg.max_age_hours == 2.0)


# ── Section 2: override precedence over POOL_SIZE_PER_IMAGE ───────────────
def test_override_precedence():
    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "3"
        os.environ["POOL_SIZE_OVERRIDES_ARM64"] = json.dumps({"2048": 7})
        cfg = _reload().load_config()
        check("override JSON wins over POOL_SIZE_PER_IMAGE for a listed size",
              cfg.target_for("arm64", 2048) == 7)
        check("POOL_SIZE_PER_IMAGE is the fallback for an unlisted size on arm64",
              cfg.target_for("arm64", 4096) == 3)
        check("ARM64 overrides don't leak to another arch — x86_64 ignores them",
              cfg.target_for("x86_64", 2048) == 3)

    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "3"
        os.environ["POOL_SIZE_OVERRIDES_ARM64"] = json.dumps({"1024": 1, "2048": 2})
        os.environ["POOL_SIZE_OVERRIDES_X86_64"] = json.dumps(
            {"1024": 0, "2048": 1, "4096": 0, "8192": 0})
        cfg = _reload().load_config()
        check("POOL_SIZE_OVERRIDES_X86_64 is honored for x86_64 (2048 -> 1)",
              cfg.target_for("x86_64", 2048) == 1)
        check("POOL_SIZE_OVERRIDES_X86_64 target 0 is honored (1024/4096/8192 -> 0)",
              all(cfg.target_for("x86_64", s) == 0 for s in (1024, 4096, 8192)))
        check("arm64 is unaffected by the x86_64 overrides",
              cfg.target_for("arm64", 1024) == 1 and cfg.target_for("arm64", 2048) == 2
              and cfg.target_for("arm64", 4096) == 3)
        check("size_overrides_by_arch holds the parsed x86_64 map",
              cfg.size_overrides_by_arch == {"x86_64": {1024: 0, 2048: 1, 4096: 0, 8192: 0}})

    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "3"
        cfg = _reload().load_config()
        check("POOL_SIZE_OVERRIDES_X86_64 unset -> x86_64 uses size_per_image",
              cfg.target_for("x86_64", 2048) == 3 and cfg.size_overrides_by_arch == {})

    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "3"
        os.environ["POOL_SIZE_OVERRIDES_X86_64"] = "not valid json"
        cfg = _reload().load_config()
        check("invalid POOL_SIZE_OVERRIDES_X86_64 is ignored (size_per_image)",
              cfg.target_for("x86_64", 2048) == 3 and cfg.size_overrides_by_arch == {})

    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "5"
        os.environ["POOL_SIZE_OVERRIDES_ARM64"] = json.dumps({"1024": 0, "2048": 4})
        cfg = _reload().load_config()
        check("override of 0 is honored (not treated as falsy/missing)",
              cfg.target_for("arm64", 1024) == 0)
        check("a second override key in the same JSON blob is honored",
              cfg.target_for("arm64", 2048) == 4)


# ── Section 3: default fallback for unlisted sizes ─────────────────────────
def test_default_fallback():
    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "2"
        # no POOL_SIZE_OVERRIDES_ARM64 at all
        cfg = _reload().load_config()
        check("size_per_image is used when no overrides are configured at all",
              cfg.target_for("arm64", 8192) == 2)
        check("target_for falls back to size_per_image for any arch when "
              "there are no overrides",
              cfg.target_for("x86_64", 1024) == 2)

    with EnvSandbox():
        os.environ["POOL_SIZE_OVERRIDES_ARM64"] = "not valid json"
        os.environ["POOL_SIZE_PER_IMAGE"] = "9"
        cfg = _reload().load_config()
        check("invalid override JSON is ignored, falling back to size_per_image",
              cfg.target_for("arm64", 2048) == 9)
        check("invalid override JSON results in an empty size_overrides dict",
              cfg.size_overrides == {})


# ── Section 4: sizes_by_arch parsing ────────────────────────────────────────
def test_sizes_by_arch_parsing():
    with EnvSandbox():
        os.environ["MICROVM_SIZES_JSON"] = json.dumps({"arm64": [1024, 2048], "x86_64": [4096]})
        cfg = _reload().load_config()
        check("MICROVM_SIZES_JSON is parsed into sizes_by_arch verbatim",
              cfg.sizes_by_arch == {"arm64": [1024, 2048], "x86_64": [4096]})

    with EnvSandbox():
        os.environ["IMAGE_SIZES"] = "1024 4096"
        cfg = _reload().load_config()
        check("IMAGE_SIZES is used (as arm64 sizes) when MICROVM_SIZES_JSON is unset",
              cfg.sizes_by_arch == {"arm64": [1024, 4096]})

    with EnvSandbox():
        cfg = _reload().load_config()
        check("default sizes_by_arch falls back to [1024,2048,4096,8192] on arm64",
              cfg.sizes_by_arch == {"arm64": [1024, 2048, 4096, 8192]})

    with EnvSandbox():
        os.environ["MICROVM_SIZES_JSON"] = "{not valid json"
        os.environ["IMAGE_SIZES"] = "2048"
        cfg = _reload().load_config()
        check("invalid MICROVM_SIZES_JSON falls back to IMAGE_SIZES",
              cfg.sizes_by_arch == {"arm64": [2048]})

    with EnvSandbox():
        os.environ["MICROVM_SIZES_JSON"] = json.dumps({"arm64": []})
        os.environ["IMAGE_SIZES"] = "2048 4096"
        cfg = _reload().load_config()
        check("MICROVM_SIZES_JSON with an empty size list for every arch "
              "falls back to IMAGE_SIZES",
              cfg.sizes_by_arch == {"arm64": [2048, 4096]})

    with EnvSandbox():
        os.environ["IMAGE_ARCHES"] = "arm64 x86_64"
        cfg = _reload().load_config()
        check("arches reflects IMAGE_ARCHES split on whitespace",
              cfg.arches == ["arm64", "x86_64"])


# ── Section 5: target_for including target 0 ───────────────────────────────
def test_target_for_zero():
    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "0"
        cfg = _reload().load_config()
        check("POOL_SIZE_PER_IMAGE=0 means target_for returns 0 for any tier "
              "(valid 'no pool for this tier')",
              cfg.target_for("arm64", 2048) == 0)

    with EnvSandbox():
        os.environ["POOL_SIZE_PER_IMAGE"] = "4"
        os.environ["POOL_SIZE_OVERRIDES_ARM64"] = json.dumps({"8192": 0})
        cfg = _reload().load_config()
        check("an explicit override of 0 for one size does not zero other sizes",
              cfg.target_for("arm64", 8192) == 0 and cfg.target_for("arm64", 2048) == 4)


# ── Section 6: other config fields read correctly ──────────────────────────
def test_other_fields():
    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "true"
        os.environ["POOL_MANAGER_PORT"] = "9999"
        os.environ["POOL_FILL_CONCURRENCY"] = "8"
        os.environ["POOL_VM_BACKEND"] = "fake"
        cfg = _reload().load_config()
        check("POOL_ENABLED=true parses to True", cfg.enabled is True)
        check("POOL_MANAGER_PORT is read as int", cfg.port == 9999)
        check("POOL_FILL_CONCURRENCY is read as int", cfg.fill_concurrency == 8)
        check("POOL_VM_BACKEND is read verbatim (lowercased)", cfg.backend == "fake")

    with EnvSandbox():
        os.environ["POOL_ENABLED"] = "0"
        cfg = _reload().load_config()
        check("POOL_ENABLED=0 parses to False", cfg.enabled is False)

    with EnvSandbox():
        cfg = _reload().load_config()
        check("default POOL_VM_BACKEND is 'aws'", cfg.backend == "aws")
        check("default POOL_MANAGER_PORT is 8090", cfg.port == 8090)
        check("default POOL_FILL_CONCURRENCY is 4", cfg.fill_concurrency == 4)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== pool_manager.config ===")
    print("\n-- POOL_MAX_AGE_HOURS 2h clamp --")
    test_max_age_hours_clamp()
    print("\n-- override precedence over POOL_SIZE_PER_IMAGE --")
    test_override_precedence()
    print("\n-- default fallback for unlisted sizes --")
    test_default_fallback()
    print("\n-- sizes_by_arch parsing --")
    test_sizes_by_arch_parsing()
    print("\n-- target_for including target 0 --")
    test_target_for_zero()
    print("\n-- other config fields --")
    test_other_fields()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL CONFIG CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_config():
    _checks.clear()
    for fn in (test_max_age_hours_clamp, test_override_precedence,
               test_default_fallback, test_sizes_by_arch_parsing,
               test_target_for_zero, test_other_fields):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool config failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
