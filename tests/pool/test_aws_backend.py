#!/usr/bin/env python3
# test-kind: offline
"""
AwsVmBackend offline test suite (Phase 6).

Exercises pool_manager/aws_backend.py's AwsVmBackend against:
  - botocore.stub.Stubber attached to the EAGERLY-built boto3 client
    (backend._lambda_client), so every control-plane call is asserted on its
    exact request params and fed a canned response — ZERO real AWS.
  - httpx.MockTransport injected via overriding backend._http_client_factory,
    so /health, /rebind, /restore-state are asserted on path/headers/body and
    fed canned responses — ZERO real network.

Covers (design §5/§8/§10, plan item 3):
  launch (exact run_microvm params incl. two-element ingressNetworkConnectors
    + single-element egress + frozen idlePolicy; quota -> QuotaExceededError;
    wrong error code -> generic BackendError), wait_healthy (ok / timeout),
    suspend/terminate (ok / ClientError -> BackendError / ResourceNotFound ->
    success), resume (a RUNNING / API-level ClientError / b dead-return /
    c timeout-while-alive / d vanished-mid-poll), rebind (verbatim payload +
    auth header + path; non-200; 404 rebuild hint; ConnectError), restore_state
    (body + auth; non-200; success:false -> error), get_state (RUNNING /
    not-found -> TERMINATED / other -> BackendError), list_all (single + multi
    page).

Run:
    .venv/bin/python tests/pool/test_aws_backend.py

Note: the wait_healthy/resume timeout cases sleep real ~1s intervals
(_POLL_INTERVAL_SECONDS); a few seconds of wall-clock is expected and fine.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from pool_manager.aws_backend import AwsVmBackend, _POLL_INTERVAL_SECONDS  # noqa: E402
from pool_manager.vm_backend import (  # noqa: E402
    BackendError,
    QuotaExceededError,
    RUNNING,
    VmHandle,
)


# ── tiny test harness (mirrors tests/pool/test_pool_grant.py) ───────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


def run_async(coro):
    return asyncio.run(coro)


# ── builders ────────────────────────────────────────────────────────────────
_REGION = "us-west-2"
_IMAGE_BASE = f"arn:aws:lambda:{_REGION}:123456789012:microvm-image:notebook-microvm"
_INGRESS = f"arn:aws:lambda:{_REGION}:aws:network-connector:aws-network-connector:HTTP_INGRESS"
_SHELL = f"arn:aws:lambda:{_REGION}:aws:network-connector:aws-network-connector:SHELL_INGRESS"
_EGRESS = f"arn:aws:lambda:{_REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
_EXEC_ROLE = "arn:aws:iam::123456789012:role/MicroVMSandboxExecRole"


def make_backend():
    """A backend with the eager client ready for Stubber attachment."""
    return AwsVmBackend(
        region=_REGION,
        image_arn_base=_IMAGE_BASE,
        exec_role_arn=_EXEC_ROLE,
        ingress_connector=_INGRESS,
        shell_ingress_connector=_SHELL,
        egress_connector=_EGRESS,
        idle_timeout_seconds=60,
        max_duration_seconds=28800,
    )


def _stub(backend):
    """Stubber attached directly to the eagerly-built client (no warm-up)."""
    return Stubber(backend._lambda_client)


def _handle(vm_id="mvm-1", endpoint="vm-1.example.aws"):
    return VmHandle(vm_id=vm_id, endpoint=endpoint, arch="arm64", size_mib=2048,
                    launched_at="2026-01-01T00:00:00+00:00", meta={})


def _mock_http(handler):
    """Make backend._http_client_factory return a MockTransport-backed client.
    `handler` is a function (httpx.Request) -> httpx.Response. Returns a list
    that captures every request seen, for post-hoc assertions."""
    seen = []

    def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(timeout=None):
        # Accept (and ignore) the optional timeout the production factory takes —
        # restore_state() now passes timeout=restore_timeout_seconds. Tests use a
        # MockTransport, so the real timeout value is irrelevant here.
        return httpx.AsyncClient(transport=httpx.MockTransport(_h))

    return factory, seen


def _auth_token_stub(stub, vm_id="mvm-1"):
    """Queue the create_microvm_auth_token call every HTTP method makes first."""
    stub.add_response(
        "create_microvm_auth_token",
        {"authToken": {"X-aws-proxy-auth": "tok-abc"}},
        {"microvmIdentifier": vm_id, "expirationInMinutes": 30,
         "allowedPorts": [{"allPorts": {}}]},
    )


# The real lambda-microvms RunMicrovm/GetMicrovm response shape requires these
# fields (verified against the botocore service model); Stubber validates canned
# responses against it, so every stubbed response must be complete. AwsVmBackend
# only reads microvmId/endpoint/state, but the response must still be valid.
from datetime import datetime, timezone  # noqa: E402


def _microvm_resp(vm_id="mvm-new", endpoint="vm-new.example.aws", state=RUNNING,
                  arch="arm64", size_mib=2048):
    return {
        "microvmId": vm_id,
        "state": state,
        "endpoint": endpoint,
        "imageArn": f"{_IMAGE_BASE}-{arch}-{size_mib}",
        "imageVersion": "1",
        "maximumDurationInSeconds": 28800,
        "startedAt": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def _list_item(vm_id="a", state=RUNNING, arch="arm64", size_mib=2048):
    """ListMicrovms item shape is LEANER than the Get/Run response: it allows
    only microvmId, state, imageArn, imageVersion, startedAt (no endpoint /
    maximumDurationInSeconds). Verified against the botocore service model."""
    return {
        "microvmId": vm_id,
        "state": state,
        "imageArn": f"{_IMAGE_BASE}-{arch}-{size_mib}",
        "imageVersion": "1",
        "startedAt": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


# ── launch ──────────────────────────────────────────────────────────────────
def test_launch_success_exact_params():
    backend = make_backend()
    stub = _stub(backend)
    expected = {
        "imageIdentifier": f"{_IMAGE_BASE}-arm64-2048",
        "ingressNetworkConnectors": [_INGRESS, _SHELL],
        "egressNetworkConnectors": [_EGRESS],
        "idlePolicy": {
            "autoResumeEnabled": True,
            "maxIdleDurationSeconds": 60,
            "suspendedDurationSeconds": 28800,
        },
        "maximumDurationInSeconds": 28800,
        "executionRoleArn": _EXEC_ROLE,
        "runHookPayload": __import__("json").dumps({
            "session_id": "spare-xyz", "notebook_name": None, "notebook_id": None,
            "checkpoint_enabled": False, "persistence_mode": "none",
            "memory_mib": 2048, "architecture": "arm64",
        }),
    }
    stub.add_response("run_microvm",
                      _microvm_resp(vm_id="mvm-new", endpoint="vm-new.example.aws"),
                      expected)
    with stub:
        h = run_async(backend.launch("arm64", 2048, "spare-xyz"))
    check("launch returns a VmHandle with vm_id + endpoint from the response",
          h.vm_id == "mvm-new" and h.endpoint == "vm-new.example.aws")
    check("launch passes the two-element ingressNetworkConnectors [ingress, shell]",
          True)  # proven: Stubber would have raised on param mismatch
    check("launch sets arch/size on the handle", h.arch == "arm64" and h.size_mib == 2048)
    stub.assert_no_pending_responses()


def test_launch_x86_64_uses_x86_image():
    """An x86_64 spare must launch from the x86_64 image of that size and
    carry x86_64 in both the run hook payload and the returned handle."""
    backend = make_backend()
    stub = _stub(backend)
    expected = {
        "imageIdentifier": f"{_IMAGE_BASE}-x86_64-2048",
        "ingressNetworkConnectors": [_INGRESS, _SHELL],
        "egressNetworkConnectors": [_EGRESS],
        "idlePolicy": {
            "autoResumeEnabled": True,
            "maxIdleDurationSeconds": 60,
            "suspendedDurationSeconds": 28800,
        },
        "maximumDurationInSeconds": 28800,
        "executionRoleArn": _EXEC_ROLE,
        "runHookPayload": __import__("json").dumps({
            "session_id": "spare-x86", "notebook_name": None, "notebook_id": None,
            "checkpoint_enabled": False, "persistence_mode": "none",
            "memory_mib": 2048, "architecture": "x86_64",
        }),
    }
    stub.add_response("run_microvm",
                      _microvm_resp(vm_id="mvm-x86", endpoint="vm-x86.example.aws",
                                    arch="x86_64"),
                      expected)
    with stub:
        h = run_async(backend.launch("x86_64", 2048, "spare-x86"))
    check("launch('x86_64', 2048) runs the -x86_64-2048 image with architecture "
          "x86_64 in the run hook payload", True)  # Stubber raises on mismatch
    check("the x86_64 launch handle reports arch x86_64 / size 2048",
          h.vm_id == "mvm-x86" and h.arch == "x86_64" and h.size_mib == 2048)
    stub.assert_no_pending_responses()


def test_launch_quota_maps_to_quota_error():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_client_error("run_microvm", service_error_code="ServiceQuotaExceededException",
                          service_message="too many")
    with stub:
        try:
            run_async(backend.launch("arm64", 2048, "spare-q"))
            raised = None
        except Exception as e:
            raised = e
    check("launch maps ServiceQuotaExceededException -> QuotaExceededError",
          isinstance(raised, QuotaExceededError))


def test_launch_other_error_maps_to_generic_backenderror():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_client_error("run_microvm", service_error_code="ValidationException",
                          service_message="bad")
    with stub:
        try:
            run_async(backend.launch("arm64", 2048, "spare-v"))
            raised = None
        except Exception as e:
            raised = e
    check("launch maps a non-quota ClientError -> generic BackendError (not Quota)",
          isinstance(raised, BackendError) and not isinstance(raised, QuotaExceededError))


# ── wait_healthy ────────────────────────────────────────────────────────────
def test_wait_healthy_ok():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, seen = _mock_http(lambda req: httpx.Response(200, json={"status": "ok"}))
    backend._http_client_factory = factory
    with stub:
        run_async(backend.wait_healthy(_handle(), timeout=5))
    check("wait_healthy returns on first 200", len(seen) == 1)
    check("wait_healthy GETs /health with the X-aws-proxy-auth header",
          seen[0].url.path == "/health" and seen[0].headers.get("X-aws-proxy-auth") == "tok-abc")


def test_wait_healthy_timeout():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, seen = _mock_http(lambda req: httpx.Response(503))
    backend._http_client_factory = factory
    # timeout just over one poll interval so the loop polls, sleeps once, then fails
    with stub:
        try:
            run_async(backend.wait_healthy(_handle(), timeout=_POLL_INTERVAL_SECONDS + 0.5))
            raised = None
        except Exception as e:
            raised = e
    check("wait_healthy raises BackendError on timeout (never-200)",
          isinstance(raised, BackendError))


# ── suspend / terminate ─────────────────────────────────────────────────────
def test_suspend_ok_and_error():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("suspend_microvm", {}, {"microvmIdentifier": "mvm-1"})
    with stub:
        run_async(backend.suspend(_handle()))
    check("suspend succeeds on a normal response", True)

    backend2 = make_backend()
    stub2 = _stub(backend2)
    stub2.add_client_error("suspend_microvm", service_error_code="InternalError")
    with stub2:
        try:
            run_async(backend2.suspend(_handle()))
            raised = None
        except Exception as e:
            raised = e
    check("suspend maps a ClientError -> BackendError", isinstance(raised, BackendError))


def test_terminate_ok_notfound_and_error():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("terminate_microvm", {}, {"microvmIdentifier": "mvm-1"})
    with stub:
        run_async(backend.terminate(_handle()))
    check("terminate succeeds on a normal response", True)

    backend2 = make_backend()
    stub2 = _stub(backend2)
    stub2.add_client_error("terminate_microvm", service_error_code="ResourceNotFoundException")
    with stub2:
        try:
            run_async(backend2.terminate(_handle()))
            ok = True
        except Exception:
            ok = False
    check("terminate treats ResourceNotFoundException as success (no raise)", ok)

    backend3 = make_backend()
    stub3 = _stub(backend3)
    stub3.add_client_error("terminate_microvm", service_error_code="InternalError")
    with stub3:
        try:
            run_async(backend3.terminate(_handle()))
            raised = None
        except Exception as e:
            raised = e
    check("terminate maps a non-notfound ClientError -> BackendError",
          isinstance(raised, BackendError))


# ── resume (four outcomes) ──────────────────────────────────────────────────
def test_resume_a_reaches_running():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("resume_microvm", {}, {"microvmIdentifier": "mvm-1"})
    stub.add_response("get_microvm", _microvm_resp(vm_id="mvm-1", state=RUNNING),
                      {"microvmIdentifier": "mvm-1"})
    with stub:
        run_async(backend.resume(_handle(), timeout=5))
    check("resume (a) returns normally once state reaches RUNNING", True)
    stub.assert_no_pending_responses()


def test_resume_api_level_error():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_client_error("resume_microvm", service_error_code="ThrottlingException")
    with stub:
        try:
            run_async(backend.resume(_handle(), timeout=5))
            raised = None
        except Exception as e:
            raised = e
    check("resume maps an API-level ClientError on resume_microvm -> BackendError",
          isinstance(raised, BackendError))


def test_resume_b_returns_dead_without_raising():
    """The real equivalent of commit 08b95ac: resume comes back with the VM in
    a dead state. resume() returns WITHOUT raising; a follow-up get_state()
    reports the dead state (that's what PoolManager.grant() checks)."""
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("resume_microvm", {}, {"microvmIdentifier": "mvm-1"})
    stub.add_response("get_microvm", _microvm_resp(vm_id="mvm-1", state="FAILED"),
                      {"microvmIdentifier": "mvm-1"})
    # the follow-up get_state() PoolManager would make
    stub.add_response("get_microvm", _microvm_resp(vm_id="mvm-1", state="FAILED"),
                      {"microvmIdentifier": "mvm-1"})
    with stub:
        try:
            run_async(backend.resume(_handle(), timeout=5))
            ok = True
        except Exception:
            ok = False
        state = run_async(backend.get_state("mvm-1"))
    check("resume (b) returns normally (no raise) when the VM comes back dead", ok)
    check("get_state() reports the dead state after a resume-returns-dead", state == "FAILED")


def test_resume_c_timeout_while_alive():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("resume_microvm", {}, {"microvmIdentifier": "mvm-1"})
    # One alive-but-not-RUNNING poll; timeout=0 means the deadline is already
    # past when the loop makes its first deadline check (after the first poll),
    # so it raises on the first iteration without a second get_microvm.
    stub.add_response("get_microvm", _microvm_resp(vm_id="mvm-1", state="RESUMING"),
                      {"microvmIdentifier": "mvm-1"})
    with stub:
        try:
            run_async(backend.resume(_handle(), timeout=0))
            raised = None
        except Exception as e:
            raised = e
    check("resume (c) raises BackendError when stuck alive-but-not-RUNNING past timeout",
          isinstance(raised, BackendError))


def test_resume_d_vanished_mid_poll():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("resume_microvm", {}, {"microvmIdentifier": "mvm-1"})
    stub.add_client_error("get_microvm", service_error_code="ResourceNotFoundException")
    with stub:
        try:
            run_async(backend.resume(_handle(), timeout=5))
            raised = None
        except Exception as e:
            raised = e
    check("resume (d) raises BackendError when get_microvm raises mid-poll (VM vanished)",
          isinstance(raised, BackendError))


# ── rebind ──────────────────────────────────────────────────────────────────
def test_rebind_success_payload_and_auth():
    import json as _json
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, seen = _mock_http(lambda req: httpx.Response(200, json={"status": "rebound"}))
    backend._http_client_factory = factory
    payload = {"session_id": "real-1", "notebook_id": "nb-9", "artifacts_bucket": "b"}
    with stub:
        run_async(backend.rebind(_handle(), payload))
    check("rebind POSTs /rebind with the auth header",
          seen[0].url.path == "/rebind" and seen[0].headers.get("X-aws-proxy-auth") == "tok-abc")
    check("rebind sends the identity_payload verbatim as the JSON body",
          _json.loads(seen[0].content) == payload)


def test_rebind_non_200():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, _ = _mock_http(lambda req: httpx.Response(500, text="boom"))
    backend._http_client_factory = factory
    with stub:
        try:
            run_async(backend.rebind(_handle(), {"session_id": "x"}))
            raised = None
        except Exception as e:
            raised = e
    check("rebind raises BackendError on a non-200", isinstance(raised, BackendError))


def test_rebind_404_rebuild_hint():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, _ = _mock_http(lambda req: httpx.Response(404, text="not found"))
    backend._http_client_factory = factory
    with stub:
        try:
            run_async(backend.rebind(_handle(), {"session_id": "x"}))
            raised = None
        except Exception as e:
            raised = e
    check("rebind 404 raises BackendError carrying the image-rebuild hint",
          isinstance(raised, BackendError) and "rebuild" in str(raised).lower())


def test_rebind_connect_error():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)

    def _boom(req):
        raise httpx.ConnectError("conn refused", request=req)

    factory, _ = _mock_http(_boom)
    backend._http_client_factory = factory
    with stub:
        try:
            run_async(backend.rebind(_handle(), {"session_id": "x"}))
            raised = None
        except Exception as e:
            raised = e
    check("rebind maps an httpx transport error -> BackendError", isinstance(raised, BackendError))


# ── restore_state ───────────────────────────────────────────────────────────
def test_restore_state_success_body_and_auth():
    import json as _json
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, seen = _mock_http(lambda req: httpx.Response(200, json={"success": True}))
    backend._http_client_factory = factory
    with stub:
        run_async(backend.restore_state(_handle(), "prior-sess-7"))
    check("restore_state POSTs /restore-state with the auth header",
          seen[0].url.path == "/restore-state"
          and seen[0].headers.get("X-aws-proxy-auth") == "tok-abc")
    check("restore_state body is {'session_id': restore_from}",
          _json.loads(seen[0].content) == {"session_id": "prior-sess-7"})


def test_restore_state_non_200():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, _ = _mock_http(lambda req: httpx.Response(500, text="err"))
    backend._http_client_factory = factory
    with stub:
        try:
            run_async(backend.restore_state(_handle(), "p"))
            raised = None
        except Exception as e:
            raised = e
    check("restore_state raises BackendError on non-200", isinstance(raised, BackendError))


def test_restore_state_success_false():
    backend = make_backend()
    stub = _stub(backend)
    _auth_token_stub(stub)
    factory, _ = _mock_http(
        lambda req: httpx.Response(200, json={"success": False, "error": "no checkpoint"}))
    backend._http_client_factory = factory
    with stub:
        try:
            run_async(backend.restore_state(_handle(), "p"))
            raised = None
        except Exception as e:
            raised = e
    check("restore_state 200 with success:false raises BackendError carrying the error",
          isinstance(raised, BackendError) and "no checkpoint" in str(raised))


# ── get_state ───────────────────────────────────────────────────────────────
def test_get_state_running():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("get_microvm", _microvm_resp(vm_id="mvm-1", state=RUNNING),
                      {"microvmIdentifier": "mvm-1"})
    with stub:
        state = run_async(backend.get_state("mvm-1"))
    check("get_state returns the control-plane state string", state == RUNNING)


def test_get_state_not_found():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_client_error("get_microvm", service_error_code="ResourceNotFoundException")
    with stub:
        state = run_async(backend.get_state("mvm-gone"))
    check("get_state maps ResourceNotFoundException -> 'TERMINATED'", state == "TERMINATED")


def test_get_state_other_error():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_client_error("get_microvm", service_error_code="InternalError")
    with stub:
        try:
            run_async(backend.get_state("mvm-1"))
            raised = None
        except Exception as e:
            raised = e
    check("get_state maps a non-notfound ClientError -> BackendError",
          isinstance(raised, BackendError))


# ── list_all (single + multi page) ──────────────────────────────────────────
def test_list_all_single_page():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("list_microvms",
                      {"items": [_list_item(vm_id="a", state=RUNNING),
                                 _list_item(vm_id="b", state="SUSPENDED")]},
                      {"maxResults": 50})
    with stub:
        handles = run_async(backend.list_all())
    check("list_all returns one handle per item on a single page",
          {h.vm_id for h in handles} == {"a", "b"})


def test_list_all_multi_page():
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("list_microvms",
                      {"items": [_list_item(vm_id="a")], "nextToken": "t1"},
                      {"maxResults": 50})
    stub.add_response("list_microvms",
                      {"items": [_list_item(vm_id="b")]},
                      {"maxResults": 50, "nextToken": "t1"})
    with stub:
        handles = run_async(backend.list_all())
    check("list_all walks nextToken and aggregates both pages",
          {h.vm_id for h in handles} == {"a", "b"})
    stub.assert_no_pending_responses()


def test_list_all_excludes_dead_states():
    """list_all() must drop TERMINATED/TERMINATING/FAILED VMs so reconcile never
    tries to re-terminate an already-dead VM every cycle."""
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("list_microvms", {"items": [
        _list_item(vm_id="live-run", state=RUNNING),
        _list_item(vm_id="live-susp", state="SUSPENDED"),
        _list_item(vm_id="dead-term", state="TERMINATED"),
        _list_item(vm_id="dead-terming", state="TERMINATING"),
        _list_item(vm_id="dead-failed", state="FAILED"),
    ]}, {"maxResults": 50})
    with stub:
        handles = run_async(backend.list_all())
    ids = {h.vm_id for h in handles}
    check("list_all keeps live VMs (RUNNING/SUSPENDED)",
          ids == {"live-run", "live-susp"})
    check("list_all excludes TERMINATED/TERMINATING/FAILED",
          not ({"dead-term", "dead-terming", "dead-failed"} & ids))


def test_list_all_includes_x86_64_vms():
    """Reconcile must see x86_64 VMs too (an x86 spare missing from list_all
    would look like a vanished VM). list_all() leaves arch blank on every
    handle — the pool DB row is the source of truth for arch — so this only
    asserts the x86 item is listed alongside the arm64 one."""
    backend = make_backend()
    stub = _stub(backend)
    stub.add_response("list_microvms", {"items": [
        _list_item(vm_id="arm-1", arch="arm64"),
        _list_item(vm_id="x86-1", arch="x86_64"),
    ]}, {"maxResults": 50})
    with stub:
        handles = run_async(backend.list_all())
    check("list_all returns x86_64 VMs alongside arm64 ones",
          {h.vm_id for h in handles} == {"arm-1", "x86-1"})


# ── runner ────────────────────────────────────────────────────────────────────
def main():
    print("\n=== AwsVmBackend (offline, Stubber + MockTransport) ===")
    print("\n-- launch --")
    test_launch_success_exact_params()
    test_launch_x86_64_uses_x86_image()
    test_launch_quota_maps_to_quota_error()
    test_launch_other_error_maps_to_generic_backenderror()
    print("\n-- wait_healthy --")
    test_wait_healthy_ok()
    test_wait_healthy_timeout()
    print("\n-- suspend / terminate --")
    test_suspend_ok_and_error()
    test_terminate_ok_notfound_and_error()
    print("\n-- resume (four outcomes) --")
    test_resume_a_reaches_running()
    test_resume_api_level_error()
    test_resume_b_returns_dead_without_raising()
    test_resume_c_timeout_while_alive()
    test_resume_d_vanished_mid_poll()
    print("\n-- rebind --")
    test_rebind_success_payload_and_auth()
    test_rebind_non_200()
    test_rebind_404_rebuild_hint()
    test_rebind_connect_error()
    print("\n-- restore_state --")
    test_restore_state_success_body_and_auth()
    test_restore_state_non_200()
    test_restore_state_success_false()
    print("\n-- get_state --")
    test_get_state_running()
    test_get_state_not_found()
    test_get_state_other_error()
    print("\n-- list_all --")
    test_list_all_single_page()
    test_list_all_multi_page()
    test_list_all_excludes_dead_states()
    test_list_all_includes_x86_64_vms()

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL AWS BACKEND CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
