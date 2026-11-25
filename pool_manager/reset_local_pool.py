"""Local-dev clean slate for the pool, in two phases:

  1. terminate every pool_vms MicroVM, THEN delete its row (per row);
  2. ORPHAN SWEEP: list live VMs from the control plane and terminate any that
     (a) were launched from THIS app's image family (MICROVM_IMAGE_ARN base),
     (b) are not linked to a notebook (no vm_sessions row, in any state),
     (c) are not in pool_vms, and
     (d) started more than ORPHAN_MIN_AGE_SEC ago (grace for a launch in flight,
         whose vm_sessions row is only written after run_microvm returns).
     These are spares whose pool_vms row was lost (stack killed mid-mint, a
     failed terminate whose row was still deleted, a reconcile that dropped a
     row on a transient list miss) — nothing else ever cleans them up, and they
     keep billing snapshot storage and show up as stale "POOL SPARE" rows.

Invoked by local_install.sh (both with and without --WarmPool), BEFORE the pool
manager starts. Never run against prod.

Imports pool_manager.backend_factory (side-effect-free), NEVER
pool_manager.server (whose module-level `app = create_app()` would eagerly
build a throwaway backend + PoolManager + FastAPI app). Exit-code contract:
non-zero on a fatal pre-/mid-enumeration failure (bad import / storage init /
missing creds), zero on completion (individual per-VM terminate warnings and a
failed orphan listing are tolerated), so local_install.sh can fail CLOSED.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from proxy.storage import storage
from pool_manager.config import load_config
from pool_manager.backend_factory import build_backend  # side-effect-free; NOT server
from pool_manager.vm_backend import VmHandle

logger = logging.getLogger(__name__)

# Never sweep a VM younger than this (seconds): a cold launch that's in flight has
# no vm_sessions row yet. Orphans seen in practice were hours old.
ORPHAN_MIN_AGE_SEC = int(os.environ.get("POOL_ORPHAN_MIN_AGE_SEC", "600"))


def _age_seconds(started_at) -> float | None:
    """Seconds since started_at (datetime or ISO string); None if unknown."""
    if not started_at:
        return None
    try:
        dt = started_at if isinstance(started_at, datetime) else \
            datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


async def _terminate_pool_rows(backend) -> None:
    rows = storage.pool_vm_list()
    if not rows:
        print(">> pool reset: pool_vms already empty — nothing to terminate")
        return
    print(f">> pool reset: terminating {len(rows)} pooled MicroVM(s) before wiping pool_vms")
    for row in rows:
        # Only vm_id is load-bearing for terminate(); the other fields are
        # populated for symmetry/logging. pool_vm_list() returns SELECT *, so
        # all fields are present.
        vm = VmHandle(
            vm_id=row["vm_id"],
            endpoint=row["endpoint"],
            arch=row["architecture"],
            size_mib=row["size_mib"],
            launched_at=row["launched_at"],
        )
        try:
            await backend.terminate(vm)  # idempotent: already-gone VM -> no-op
        except Exception as e:
            logging.warning("pool reset: terminate %s failed (continuing): %s", row["vm_id"], e)
        storage.pool_vm_delete(row["vm_id"])  # delete AFTER the terminate attempt, per row
    print(">> pool reset: pool_vms cleared")


async def _sweep_orphans(backend) -> None:
    image_base = getattr(backend, "image_arn_base", "") or ""
    if not image_base:
        # Fake backend (or no image configured): nothing real to sweep.
        print(">> pool reset: orphan sweep skipped (no real image family configured)")
        return
    prefix = f"{image_base}-"
    try:
        live = await backend.list_all()  # dead states already excluded
    except Exception as e:
        # Best-effort: a list failure must not block the install.
        logging.warning("pool reset: orphan sweep skipped — list failed: %s", e)
        return

    tracked_pool = {r["vm_id"] for r in storage.pool_vm_list()}
    swept = kept_young = 0
    for vm in live:
        meta = vm.meta or {}
        if not str(meta.get("image_arn", "")).startswith(prefix):
            continue                      # not this app's image family — never touch
        if vm.vm_id in tracked_pool:
            continue                      # still tracked by the pool ledger
        try:
            if storage.vm_session_get(vm.vm_id):
                continue                  # linked to a notebook (any state) — never touch
        except Exception as e:
            logging.warning("pool reset: vm_session lookup for %s failed — skipping it: %s", vm.vm_id, e)
            continue
        age = _age_seconds(meta.get("started_at"))
        if age is None or age < ORPHAN_MIN_AGE_SEC:
            kept_young += 1               # unknown/too young — could be a launch in flight
            continue
        try:
            await backend.terminate(vm)
            swept += 1
            print(f"   orphan terminated: {vm.vm_id} (state={meta.get('state')}, age={age/60:.0f}m)")
        except Exception as e:
            logging.warning("pool reset: orphan terminate %s failed (continuing): %s", vm.vm_id, e)
    msg = f">> pool reset: orphan sweep done — {swept} terminated"
    if kept_young:
        msg += f", {kept_young} skipped (younger than {ORPHAN_MIN_AGE_SEC}s or no start time)"
    print(msg)


async def _reset() -> None:
    backend = build_backend(load_config())  # AwsVmBackend from env, or fake if POOL_VM_BACKEND=fake
    await _terminate_pool_rows(backend)
    await _sweep_orphans(backend)


if __name__ == "__main__":
    try:
        asyncio.run(_reset())
    except Exception as e:  # fatal: bad import / storage init / missing creds
        logging.error("pool reset FAILED before completion: %s", e)
        sys.exit(1)  # pre-enumeration failure -> non-zero
    sys.exit(0)  # ran to completion (individual terminate warnings are tolerated)
