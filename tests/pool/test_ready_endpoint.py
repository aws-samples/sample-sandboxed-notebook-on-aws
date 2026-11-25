#!/usr/bin/env python3
# test-kind: offline
"""
pool_manager GET /ready readiness-probe test suite.

/ready is the readiness probe (distinct from /health liveness): it returns 200
only when BOTH (a) the DB is reachable and (b) every configured tier is at or
above target in the 'available' state; otherwise 503. This suite exercises all
three outcomes end-to-end via FastAPI's TestClient wired to a FakeVmBackend + a
temp SQLite file (no AWS, no network).

To keep the three cases deterministic we drive storage DIRECTLY (via mk_aged_row)
and DO NOT enter the `with client:` lifespan context — so the manager's
background fill/recycle/reconcile loops never run and never mutate the rows we
seed. /ready is read-only, so it needs no running loops.

Run:
    .venv/bin/python tests/pool/test_ready_endpoint.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config, mk_aged_row  # noqa: E402
from pool_manager.fake_backend import FakeVmBackend  # noqa: E402
from pool_manager.server import create_app  # noqa: E402

try:
    from fastapi.testclient import TestClient
    _HAVE_TESTCLIENT = True
except ImportError:
    _HAVE_TESTCLIENT = False


# ── tiny test harness ────────────────────────────────────────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def _build(size_per_image=2, **config_overrides):
    """Build app + TestClient over a FakeVmBackend + temp SQLite. The client is
    returned WITHOUT entering its lifespan context so background loops stay off
    and seeded rows are stable."""
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=size_per_image,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]},
                          **config_overrides)
    app = create_app(config=config, backend=backend, storage=storage)
    client = TestClient(app)
    return client, backend, storage, config


# ── Section 1: 200 ready when every tier is at target ──────────────────────
def test_ready_200_when_pool_at_target():
    client, backend, storage, config = _build(size_per_image=2)
    # Seed the single arm64:2048 tier up to its target (2) with 'available' spares.
    mk_aged_row(storage, "vm-ready-1", arch="arm64", size=2048, state="available")
    mk_aged_row(storage, "vm-ready-2", arch="arm64", size=2048, state="available")

    resp = client.get("/ready")
    check("GET /ready returns 200 when the tier is at target", resp.status_code == 200)
    body = resp.json()
    check("GET /ready 200 body reports status=ready", body.get("status") == "ready")
    tier = body.get("tiers", {}).get("arm64:2048")
    check("GET /ready 200 body reports available>=target for the tier",
          tier is not None and tier["available"] == 2 and tier["target"] == 2)


# ── Section 2: 503 pool_below_target when a tier is short ───────────────────
def test_ready_503_when_tier_below_target():
    client, backend, storage, config = _build(size_per_image=2)
    # Only one 'available' spare for a tier whose target is 2 -> below target.
    mk_aged_row(storage, "vm-short-1", arch="arm64", size=2048, state="available")

    resp = client.get("/ready")
    check("GET /ready returns 503 when a tier is below target", resp.status_code == 503)
    body = resp.json()
    check("GET /ready 503 body reports reason=pool_below_target",
          body.get("reason") == "pool_below_target")
    check("GET /ready 503 body reports status=not_ready", body.get("status") == "not_ready")
    tier = body.get("tiers", {}).get("arm64:2048")
    check("GET /ready 503 body reports the shortfall (available=1,target=2)",
          tier is not None and tier["available"] == 1 and tier["target"] == 2)


# ── Section 2b: a tier of only 'granting' rows is NOT 'filled' ──────────────
def test_ready_503_when_rows_present_but_not_available():
    client, backend, storage, config = _build(size_per_image=2)
    # Target rows exist but none are in 'available' — readiness must still fail.
    mk_aged_row(storage, "vm-g-1", arch="arm64", size=2048, state="granting")
    mk_aged_row(storage, "vm-g-2", arch="arm64", size=2048, state="granted")

    resp = client.get("/ready")
    check("GET /ready returns 503 when rows exist but none are 'available'",
          resp.status_code == 503)
    check("GET /ready 503 body reports reason=pool_below_target (not merely row count)",
          resp.json().get("reason") == "pool_below_target")


# ── Section 3: 503 db_unreachable when storage.pool_vm_list raises ──────────
def test_ready_503_when_db_unreachable():
    client, backend, storage, config = _build(size_per_image=2)
    # Seed a healthy, at-target pool so the ONLY failing condition is the DB probe.
    mk_aged_row(storage, "vm-db-1", arch="arm64", size=2048, state="available")
    mk_aged_row(storage, "vm-db-2", arch="arm64", size=2048, state="available")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB connection failure")

    storage.pool_vm_list = _boom  # stub the cheap round-trip to throw

    resp = client.get("/ready")
    check("GET /ready returns 503 when the DB probe raises", resp.status_code == 503)
    check("GET /ready 503 body reports reason=db_unreachable",
          resp.json().get("reason") == "db_unreachable")


# ── Section 4: /health stays dependency-free (regression guard) ─────────────
def test_health_unaffected():
    client, backend, storage, config = _build(size_per_image=2)
    # No rows seeded, DB fine: liveness must still be 200 {"status":"ok"} even
    # though the pool is empty (readiness would be 503 here).
    resp = client.get("/health")
    check("GET /health still returns 200 regardless of pool state",
          resp.status_code == 200)
    check("GET /health still returns {'status': 'ok'}", resp.json() == {"status": "ok"})


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== pool_manager GET /ready ===")
    if not _HAVE_TESTCLIENT:
        print("  ❌ fastapi.testclient.TestClient is unavailable in this venv — "
              "cannot run the /ready test suite")
        sys.exit(1)

    print("\n-- 200 ready when pool at target --")
    test_ready_200_when_pool_at_target()
    print("\n-- 503 pool_below_target when a tier is short --")
    test_ready_503_when_tier_below_target()
    print("\n-- 503 pool_below_target when rows exist but not 'available' --")
    test_ready_503_when_rows_present_but_not_available()
    print("\n-- 503 db_unreachable when storage.pool_vm_list raises --")
    test_ready_503_when_db_unreachable()
    print("\n-- /health unaffected (liveness stays dependency-free) --")
    test_health_unaffected()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL /ready CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────
def test_ready_endpoint():
    _checks.clear()
    assert _HAVE_TESTCLIENT, "fastapi.testclient.TestClient is unavailable"
    for fn in (test_ready_200_when_pool_at_target,
               test_ready_503_when_tier_below_target,
               test_ready_503_when_rows_present_but_not_available,
               test_ready_503_when_db_unreachable,
               test_health_unaffected):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "/ready failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
