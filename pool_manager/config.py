"""
pool_manager configuration — read from the environment at import time, in the
same style the proxy uses (proxy/platform/microvm_manager.py: IMAGE_ARCHES =
os.environ.get("IMAGE_ARCHES", "arm64").split(); proxy/platform/routes/
microvm.py::_load_sizes_by_arch parses MICROVM_SIZES_JSON). See design §7.

Stdlib-only on purpose: the proxy imports this module for the shared grant
budget, so it must not pull in the pool manager's server or AWS backend.
"""

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Hard ceiling on POOL_MAX_AGE_HOURS (design §7, §11 risk #1): a spare granted
# at this age still leaves ~6h of the AWS 8h lifetime for the user's session.
# The pool manager MUST clamp any configured value above this to this ceiling.
_MAX_AGE_HOURS_CEILING = 2.0
# Grant budget. A claimed spare stays in 'granting' while the grant runs its three
# steps one after another: resume the suspended spare, POST /rebind (identity),
# then for a restore grant POST /restore-state (S3 pull of the checkpoint). The
# step timeouts below are the only place these numbers live; the pool manager,
# AwsVmBackend and the proxy all derive their bounds from them so the stale-row
# reaper never tears down a grant that is still inside its budget, and the proxy
# never gives up on a restore grant the pool is still working on.
RESUME_TIMEOUT_SECONDS = 30.0
REBIND_TIMEOUT_SECONDS = 10.0       # AwsVmBackend control-call (/rebind, /health) timeout
RESTORE_TIMEOUT_SECONDS = 120.0     # AwsVmBackend /restore-state timeout
GRANT_BUDGET_SECONDS = RESUME_TIMEOUT_SECONDS + REBIND_TIMEOUT_SECONDS + RESTORE_TIMEOUT_SECONDS
# reconcile_once reaps a 'granting' row (and an orphaned 'granted' row) only
# after this age, measured from updated_at: the full budget plus a margin.
RECONCILE_GRANT_TIMEOUT_SECONDS = GRANT_BUDGET_SECONDS + 30.0
# The proxy's outer HTTP bound on a restore grant: above the budget, so the pool
# answers (success or GrantError) before the proxy abandons the request.
PROXY_GRANT_RESTORE_BOUND_SECONDS = GRANT_BUDGET_SECONDS + 15.0

_DEFAULT_SIZES = [1024, 2048, 4096, 8192]
_DEFAULT_ARCH = "arm64"


def _load_sizes_by_arch() -> dict:
    """Mirror of proxy/platform/routes/microvm.py::_load_sizes_by_arch — parses
    MICROVM_SIZES_JSON -> {arch:[sizes]}, falling back to IMAGE_SIZES then the
    hardcoded default."""
    raw = os.environ.get("MICROVM_SIZES_JSON")
    if raw:
        try:
            parsed = json.loads(raw)
            out = {a: [int(m) for m in mems] for a, mems in parsed.items() if mems}
            if out:
                return out
        except (ValueError, TypeError) as e:
            logger.warning(f"MICROVM_SIZES_JSON invalid ({e}); falling back to IMAGE_SIZES/default")
    legacy = [int(s) for s in os.environ.get("IMAGE_SIZES", "1024 2048 4096 8192").split()]
    return {_DEFAULT_ARCH: legacy or _DEFAULT_SIZES}


def _load_size_overrides(var_name: str = "POOL_SIZE_OVERRIDES_ARM64") -> dict:
    """Parse a POOL_SIZE_OVERRIDES_<ARCH> var (JSON {size:count}) into {int
    size: int count}. Unset/invalid -> {} (no overrides; everything uses
    size_per_image)."""
    raw = os.environ.get(var_name)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {int(size): int(count) for size, count in parsed.items()}
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"{var_name} invalid ({e}); ignoring overrides")
        return {}


# Non-default arches that have their own POOL_SIZE_OVERRIDES_<ARCH> var.
_ARCH_OVERRIDE_VARS = {"x86_64": "POOL_SIZE_OVERRIDES_X86_64"}


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class PoolConfig:
    """Pool manager configuration. Fields mirror design §7's config.sh vars.
    Construct via load_config() rather than directly, so the 2h clamp + the
    sizes/overrides parsing are always applied consistently."""

    enabled: bool = False
    size_per_image: int = 2
    size_overrides: dict = field(default_factory=dict)  # {size_mib: count} for arm64 (POOL_SIZE_OVERRIDES_ARM64)
    # {arch: {size_mib: count}} for the non-default arches, e.g.
    # {"x86_64": {2048: 1, 1024: 0}} from POOL_SIZE_OVERRIDES_X86_64.
    size_overrides_by_arch: dict = field(default_factory=dict)
    max_age_hours: float = 2.0
    port: int = 8090
    fill_concurrency: int = 4
    backend: str = "fake"
    # Simulated launch delay for the FAKE backend only (seconds). Models the real
    # ~10s AWS provisioning floor so a standalone fake-backed run behaves like
    # production. Ignored by the real AwsVmBackend, where launch latency
    # is whatever AWS actually takes. Tests construct FakeVmBackend() directly and
    # keep this at 0 for speed.
    fake_launch_delay_seconds: float = 10.0
    # How long a mint waits for a freshly-launched spare's /health to go 200
    # before giving up and discarding it. A cold MicroVM can take well over the
    # old 30s (observed: /health 502s past 30s, then 200 shortly after) — too
    # tight a budget abandoned a VM that was ABOUT to become healthy, leaving the
    # tier short (available = target-1) until the next fill tick retried, so the
    # pool visibly sat at N-1 "for quite some time". 90s covers the slow cold
    # boots; a VM that truly never comes up is still cleaned up, just later.
    mint_health_timeout_seconds: float = 90.0
    arches: list = field(default_factory=lambda: [_DEFAULT_ARCH])
    sizes_by_arch: dict = field(default_factory=dict)

    @property
    def max_age_seconds(self) -> float:
        return self.max_age_hours * 3600.0

    def target_for(self, arch: str, size: int) -> int:
        """Per-tier target spare count. The arch's own override JSON wins
        (arm64: size_overrides; others: size_overrides_by_arch[arch]); else
        size_per_image. 0 is a valid 'no pool for this tier'."""
        if arch == _DEFAULT_ARCH:
            overrides = self.size_overrides
        else:
            overrides = self.size_overrides_by_arch.get(arch, {})
        if size in overrides:
            return overrides[size]
        return self.size_per_image


def load_config() -> PoolConfig:
    """Read §7 env vars and build a PoolConfig. POOL_MAX_AGE_HOURS is clamped
    to a hard 2h ceiling — any value > 2 becomes 2."""
    max_age_hours = float(os.environ.get("POOL_MAX_AGE_HOURS", "2"))
    if max_age_hours > _MAX_AGE_HOURS_CEILING:
        logger.warning(
            f"POOL_MAX_AGE_HOURS={max_age_hours} exceeds the {_MAX_AGE_HOURS_CEILING}h "
            f"ceiling — clamping to {_MAX_AGE_HOURS_CEILING}h"
        )
        max_age_hours = _MAX_AGE_HOURS_CEILING

    # Default MUST match scripts/config.sh's POOL_VM_BACKEND default ("aws").
    # config.sh is the operational single source of truth (sourced by
    # local_install.sh); this fallback only fires in standalone/test use where
    # config.sh was never sourced. Keep the two in sync to avoid drift.
    backend = os.environ.get("POOL_VM_BACKEND", "aws").strip().lower()

    return PoolConfig(
        enabled=_parse_bool(os.environ.get("POOL_ENABLED", "false")),
        size_per_image=int(os.environ.get("POOL_SIZE_PER_IMAGE", "2")),
        size_overrides=_load_size_overrides("POOL_SIZE_OVERRIDES_ARM64"),
        size_overrides_by_arch={
            arch: ov for arch, var in _ARCH_OVERRIDE_VARS.items()
            if (ov := _load_size_overrides(var))
        },
        max_age_hours=max_age_hours,
        port=int(os.environ.get("POOL_MANAGER_PORT", "8090")),
        fill_concurrency=int(os.environ.get("POOL_FILL_CONCURRENCY", "4")),
        backend=backend,
        fake_launch_delay_seconds=float(os.environ.get("POOL_FAKE_LAUNCH_DELAY_SECONDS", "10")),
        mint_health_timeout_seconds=float(os.environ.get("POOL_MINT_HEALTH_TIMEOUT_SECONDS", "90")),
        arches=os.environ.get("IMAGE_ARCHES", _DEFAULT_ARCH).split(),
        sizes_by_arch=_load_sizes_by_arch(),
    )
