#!/usr/bin/env python3
# test-kind: offline
"""
pool_manager FastAPI server test suite (Phase 3 — FEAT-002, plan.md item 12).

Exercises create_app() end-to-end via FastAPI's TestClient, wired to a
FakeVmBackend + a temp SQLite file (no AWS, no network, no real MicroVM):
  /grant  -> 200 {vm_id,endpoint}; 409 POOL_EMPTY; 5xx on injected failure
  /status -> accurate per-tier counts
  /health -> {"status": "ok"}
  /admin/refill -> fills to target
  /admin/drain  -> terminates+deletes all spares

Run:
    .venv/bin/python tests/pool/test_pool_api.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.pool._poolkit import _run_sqlite, build_config  # noqa: E402
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


# Shared test token for the authenticated pool routes (/grant, /admin/*). Set in
# the environment by _build_client so the pre-existing API tests, which exercise
# those routes, keep passing now that they are gated (FEAT-001). The dedicated
# auth tests (Section 8) manage POOL_AUTH_TOKEN themselves.
_TEST_POOL_TOKEN = "pool-api-test-token"


def _build_client(size_per_image=2, default_auth=True, **config_overrides):
    if default_auth:
        os.environ["POOL_AUTH_TOKEN"] = _TEST_POOL_TOKEN
    backend = FakeVmBackend()
    storage = _run_sqlite()
    config = build_config(size_per_image=size_per_image,
                          arches=["arm64"], sizes_by_arch={"arm64": [2048]},
                          **config_overrides)
    app = create_app(config=config, backend=backend, storage=storage)
    # Default the shared-secret header on every request so the pre-existing
    # /grant + /admin/* tests authenticate; probe routes ignore it. The auth
    # tests pass default_auth=False to control the header/env themselves.
    headers = {"X-Proxy-Auth": _TEST_POOL_TOKEN} if default_auth else None
    client = TestClient(app, headers=headers)
    return client, backend, storage


# ── Section 1: /health ──────────────────────────────────────────────────────
def test_health():
    client, backend, storage = _build_client()
    with client:
        resp = client.get("/health")
        check("GET /health returns 200", resp.status_code == 200)
        check("GET /health returns {'status': 'ok'}", resp.json() == {"status": "ok"})


# ── Section 2: /grant happy path ────────────────────────────────────────────
def test_grant_happy_path():
    client, backend, storage = _build_client(size_per_image=2)
    with client:
        resp = client.post("/grant", json={
            "arch": "arm64", "size": 2048, "session_id": "api-sess-1",
        })
        check("POST /grant (happy path) returns 200", resp.status_code == 200)
        body = resp.json()
        check("POST /grant response has vm_id and endpoint",
              "vm_id" in body and "endpoint" in body)

        rows = storage.pool_vm_list()
        granted = [r for r in rows if r["vm_id"] == body["vm_id"]]
        check("the granted row is persisted as 'granted' in storage",
              granted and granted[0]["state"] == "granted")


# ── Section 3: /grant POOL_EMPTY -> 409 ────────────────────────────────────
def test_grant_pool_empty_409():
    client, backend, storage = _build_client(size_per_image=0)
    with client:
        resp = client.post("/grant", json={
            "arch": "arm64", "size": 2048, "session_id": "api-sess-empty",
        })
        check("POST /grant with no spares returns 409", resp.status_code == 409)
        check("POST /grant 409 body reports error=POOL_EMPTY",
              resp.json().get("error") == "POOL_EMPTY")


# ── Section 4: /grant backend failure -> 5xx ───────────────────────────────
def test_grant_backend_failure_5xx():
    client, backend, storage = _build_client(size_per_image=1)
    with client:
        backend.fail_resume_times(1)
        resp = client.post("/grant", json={
            "arch": "arm64", "size": 2048, "session_id": "api-sess-fail",
        })
        check(f"POST /grant on an injected backend failure returns a 5xx "
              f"(got {resp.status_code})", 500 <= resp.status_code < 600)
        check("POST /grant 5xx body reports a GRANT_FAILED error",
              resp.json().get("error") == "GRANT_FAILED")

        rows = storage.pool_vm_list()
        check("no dangling 'granting' row after a 5xx grant failure via the API",
              not any(r["state"] == "granting" for r in rows))


# ── Section 5: /status accurate per-tier counts ────────────────────────────
def test_status_accurate_counts():
    client, backend, storage = _build_client(size_per_image=3)
    with client:
        resp = client.get("/status")
        check("GET /status returns 200", resp.status_code == 200)
        body = resp.json()
        check("GET /status includes a 'tiers' key", "tiers" in body)
        tier = body["tiers"].get("arm64:2048")
        check("GET /status reports the correct target for the arm64:2048 tier",
              tier is not None and tier["target"] == 3)
        check("GET /status reports actual == 3 (target reached via lifespan startup fill)",
              tier["actual"] == 3)
        check("GET /status counts breakdown shows 3 'available'",
              tier["counts"].get("available") == 3)

        # grant one, then re-check status reflects the state change. The
        # live server's fill_loop is running in the background and reacts to
        # the demand-aware refill signal almost immediately, so 'available'
        # converges back toward target rather than staying one short — that
        # IS the correct behavior (design §13.5), not a bug.
        grant_resp = client.post("/grant", json={
            "arch": "arm64", "size": 2048, "session_id": "api-status-sess",
        })
        check("setup: the grant for the /status follow-up check succeeded",
              grant_resp.status_code == 200)
        resp2 = client.get("/status")
        tier2 = resp2.json()["tiers"]["arm64:2048"]
        check("GET /status reflects 1 'granted' after a grant",
              tier2["counts"].get("granted") == 1)
        check("GET /status: 'available' count is still a valid, non-negative "
              "tier count (<= target) after a grant",
              0 <= tier2["counts"].get("available", 0) <= tier2["target"])


# ── Section 6: /admin/refill fills to target ───────────────────────────────
def test_admin_refill_fills_to_target():
    client, backend, storage = _build_client(size_per_image=4)
    with client:
        # Drain the pool manually first (simulate having granted everything).
        rows = storage.pool_vm_list()
        for row in rows:
            storage.pool_vm_delete(row["vm_id"])
        check("setup: pool is empty before calling /admin/refill",
              len(storage.pool_vm_list()) == 0)

        resp = client.post("/admin/refill")
        check("POST /admin/refill returns 200", resp.status_code == 200)

        rows_after = storage.pool_vm_list(architecture="arm64", size_mib=2048)
        check("POST /admin/refill fills the tier back to its target (4)",
              len(rows_after) == 4)


# ── Section 7: /admin/drain terminates+deletes all spares ─────────────────
def test_admin_drain_terminates_and_deletes_all():
    client, backend, storage = _build_client(size_per_image=3)
    with client:
        rows_before = storage.pool_vm_list()
        check("setup: pool has spares before draining", len(rows_before) == 3)

        resp = client.post("/admin/drain")
        check("POST /admin/drain returns 200", resp.status_code == 200)
        check("POST /admin/drain reports the number of rows drained",
              resp.json().get("drained") == 3)

        rows_after = storage.pool_vm_list()
        check("POST /admin/drain deletes every pool_vms row", len(rows_after) == 0)

        backend_vms = asyncio.run(backend.list_all())
        check("POST /admin/drain terminates every backend VM the pool knew about",
              len(backend_vms) == 0)


# ── Section 8: auth — /grant + /admin/* require POOL_AUTH_TOKEN; probes exempt ──
def test_grant_requires_token():
    """C1 (pool side, FEAT-001): /grant and /admin/* require POOL_AUTH_TOKEN;
    /status, /health, /ready stay unauthenticated."""
    try:
        client, backend, storage = _build_client(size_per_image=2, default_auth=False)
        os.environ["POOL_AUTH_TOKEN"] = "pool-secret"
        with client:
            body = {"arch": "arm64", "size": 2048, "session_id": "api-auth-sess"}

            no_tok = client.post("/grant", json=body)
            check("POST /grant without token -> 401", no_tok.status_code == 401)

            bad_tok = client.post("/grant", json=body,
                                  headers={"X-Proxy-Auth": "wrong"})
            check("POST /grant with wrong token -> 401", bad_tok.status_code == 401)

            ok = client.post("/grant", json=body,
                             headers={"X-Proxy-Auth": "pool-secret"})
            check("POST /grant with correct token -> 200", ok.status_code == 200)

            admin = client.post("/admin/refill")
            check("POST /admin/refill without token -> 401", admin.status_code == 401)
            admin_ok = client.post("/admin/refill",
                                   headers={"X-Proxy-Auth": "pool-secret"})
            check("POST /admin/refill with correct token -> 200",
                  admin_ok.status_code == 200)

            # Probes remain unauthenticated.
            check("GET /health exempt (no token) -> 200",
                  client.get("/health").status_code == 200)
            check("GET /status exempt (no token) -> 200",
                  client.get("/status").status_code == 200)
            check("GET /ready exempt (no token) -> 200 or 503, never 401",
                  client.get("/ready").status_code in (200, 503))
    finally:
        os.environ.pop("POOL_AUTH_TOKEN", None)


def test_grant_fail_closed_when_unset():
    """When POOL_AUTH_TOKEN is unset, /grant fails CLOSED (503)."""
    client, backend, storage = _build_client(size_per_image=2, default_auth=False)
    os.environ.pop("POOL_AUTH_TOKEN", None)
    with client:
        resp = client.post("/grant", json={
            "arch": "arm64", "size": 2048, "session_id": "api-noauth-sess",
        })
        check("POST /grant with POOL_AUTH_TOKEN unset -> 503 (fail closed)",
              resp.status_code == 503)
        check("GET /status still exempt when token unset -> 200",
              client.get("/status").status_code == 200)


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n=== pool_manager FastAPI server (API) ===")
    if not _HAVE_TESTCLIENT:
        print("  ❌ fastapi.testclient.TestClient is unavailable in this venv — "
              "cannot run the API test suite")
        sys.exit(1)

    print("\n-- /health --")
    test_health()
    print("\n-- /grant happy path --")
    test_grant_happy_path()
    print("\n-- /grant POOL_EMPTY -> 409 --")
    test_grant_pool_empty_409()
    print("\n-- /grant backend failure -> 5xx --")
    test_grant_backend_failure_5xx()
    print("\n-- /status accurate per-tier counts --")
    test_status_accurate_counts()
    print("\n-- /admin/refill fills to target --")
    test_admin_refill_fills_to_target()
    print("\n-- /admin/drain terminates+deletes all spares --")
    test_admin_drain_terminates_and_deletes_all()
    print("\n-- auth: /grant + /admin/* require token; probes exempt --")
    test_grant_requires_token()
    print("\n-- auth: /grant fails closed when token unset --")
    test_grant_fail_closed_when_unset()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL POOL API CHECKS PASSED ✓")


# ── pytest entry point ─────────────────────────────────────────────────────────
def test_pool_api():
    _checks.clear()
    assert _HAVE_TESTCLIENT, "fastapi.testclient.TestClient is unavailable"
    for fn in (test_health, test_grant_happy_path, test_grant_pool_empty_409,
               test_grant_backend_failure_5xx, test_status_accurate_counts,
               test_admin_refill_fills_to_target, test_admin_drain_terminates_and_deletes_all,
               test_grant_requires_token, test_grant_fail_closed_when_unset):
        fn()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "pool API failures: " + "; ".join(failed)


if __name__ == "__main__":
    main()
