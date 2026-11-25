#!/usr/bin/env python3
# test-kind: offline
"""
config.sh <-> pool_manager/config.py wiring sanity check (Phase 4 —
pool-phase45-config-proxy, plan.md item 7).

Sources scripts/config.sh in a clean subprocess environment (POOL_* unset
first) and asserts:
  - every POOL_* var the design (§7) + pool_manager/config.py's load_config()
    expects is present/exported, with POOL_ENABLED defaulting to "false";
  - feeding those exact sourced values into pool_manager.config.load_config()
    (via a monkeypatched os.environ) yields PoolConfig.enabled is False and
    PoolConfig.backend == "aws" (config.sh forces aws unconditionally) —
    proving config.sh's defaults and pool_manager/config.py's reader agree on
    names/semantics.

No AWS calls are exercised here (config.sh calls `aws sts get-caller-identity`
for ACCOUNT_ID, but that failing/returning empty does not affect the POOL_*
vars under test).

Run:
    .venv/bin/python tests/pool/test_config_wiring.py
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pool_manager.config as pool_config  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_POOL_VARS = (
    "POOL_ENABLED", "POOL_SIZE_PER_IMAGE", "POOL_SIZE_OVERRIDES_ARM64",
    "POOL_SIZE_OVERRIDES_X86_64", "POOL_MAX_AGE_HOURS", "POOL_MANAGER_PORT", "POOL_MANAGER_URL",
    "POOL_FILL_CONCURRENCY", "POOL_VM_BACKEND",
)

# ── tiny test harness (matches tests/pool/test_pool_grant.py convention) ───
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _source_config_sh() -> dict:
    """Source scripts/config.sh in a clean subprocess (POOL_* unset first)
    and return the resulting environment as a dict. Uses `env` after sourcing
    so we get the fully-resolved values regardless of shell quoting."""
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("POOL_")
                 and k not in ("IMAGE_ARCHES", "DEFAULT_IMAGE_ARCH")}
    # Keep PATH/HOME etc. so `aws`, `tr`, etc. resolve normally.
    script = "source scripts/config.sh >/dev/null 2>&1; env"
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO_ROOT, env=clean_env, capture_output=True, text=True, timeout=60,
    )
    result = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k] = v
    return result


# ── Section 1: every POOL_* var is exported, POOL_ENABLED defaults false ──
def test_config_sh_exports_pool_vars_with_defaults():
    env = _source_config_sh()

    for var in _POOL_VARS:
        check(f"{var} is exported by scripts/config.sh", var in env)

    check("POOL_ENABLED defaults to 'false'", env.get("POOL_ENABLED") == "false")
    check("POOL_SIZE_PER_IMAGE defaults to '2'", env.get("POOL_SIZE_PER_IMAGE") == "2")
    check("POOL_MAX_AGE_HOURS defaults to '2'", env.get("POOL_MAX_AGE_HOURS") == "2")
    check("POOL_MANAGER_PORT defaults to '8090'", env.get("POOL_MANAGER_PORT") == "8090")
    check("POOL_MANAGER_URL derives its default from POOL_MANAGER_PORT",
          env.get("POOL_MANAGER_URL") == "http://localhost:8090")
    check("POOL_FILL_CONCURRENCY defaults to '4'", env.get("POOL_FILL_CONCURRENCY") == "4")
    check("POOL_VM_BACKEND is forced to 'aws'", env.get("POOL_VM_BACKEND") == "aws")
    check("POOL_SIZE_OVERRIDES_ARM64 default is valid JSON with the expected tiers",
          env.get("POOL_SIZE_OVERRIDES_ARM64") == '{"1024":1,"2048":2,"4096":2,"8192":1}')
    check("POOL_SIZE_OVERRIDES_X86_64 default is 1 spare at 2048, 0 elsewhere",
          env.get("POOL_SIZE_OVERRIDES_X86_64") == '{"1024":0,"2048":1,"4096":0,"8192":0}')

    return env


# ── Section 2: those exact values satisfy pool_manager/config.py's reader ──
def test_sourced_values_satisfy_pool_manager_config(env):
    saved = dict(os.environ)
    try:
        for var in _POOL_VARS + ("IMAGE_ARCHES", "MICROVM_SIZES_JSON"):
            if var in env:
                os.environ[var] = env[var]
        cfg = pool_config.load_config()
        check("load_config() over config.sh's sourced defaults: enabled is False",
              cfg.enabled is False)
        check("load_config() over config.sh's sourced defaults: backend == 'aws'",
              cfg.backend == "aws")
        check("load_config() over config.sh's sourced defaults: size_per_image == 2",
              cfg.size_per_image == 2)
        check("load_config() over config.sh's sourced defaults: max_age_hours == 2.0",
              cfg.max_age_hours == 2.0)
        check("load_config() over config.sh's sourced defaults: port == 8090",
              cfg.port == 8090)
        check("load_config() over config.sh's sourced defaults: fill_concurrency == 4",
              cfg.fill_concurrency == 4)
        check("load_config() parses config.sh's POOL_SIZE_OVERRIDES_ARM64 JSON correctly",
              cfg.target_for("arm64", 2048) == 2 and cfg.target_for("arm64", 8192) == 1)
        check("load_config() parses config.sh's POOL_SIZE_OVERRIDES_X86_64: x86_64 2048 -> 1",
              cfg.target_for("x86_64", 2048) == 1)
        check("load_config() over config.sh's defaults: x86_64 1024/4096/8192 -> 0",
              all(cfg.target_for("x86_64", s) == 0 for s in (1024, 4096, 8192)))
        check("load_config() over config.sh's defaults: arches == ['arm64', 'x86_64']",
              cfg.arches == ["arm64", "x86_64"])
        check("load_config() over config.sh's defaults: x86_64 sizes == [1024,2048,4096,8192]",
              cfg.sizes_by_arch.get("x86_64") == [1024, 2048, 4096, 8192])
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== config.sh <-> pool_manager/config.py wiring sanity check ===")
    print("\n-- scripts/config.sh exports every POOL_* var, POOL_ENABLED=false by default --")
    env = test_config_sh_exports_pool_vars_with_defaults()
    print("\n-- those exact sourced values satisfy pool_manager.config.load_config() --")
    test_sourced_values_satisfy_pool_manager_config(env)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL CONFIG WIRING CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_config_wiring():
    _checks.clear()
    env = test_config_sh_exports_pool_vars_with_defaults()
    test_sourced_values_satisfy_pool_manager_config(env)
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "config wiring failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
