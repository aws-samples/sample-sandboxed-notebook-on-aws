"""
AwsVmBackend — real `lambda-microvms` VmBackend (design §2-§8).

Implements the same VmBackend contract as FakeVmBackend, against the real
AWS control plane and the in-VM /health, /rebind, /restore-state endpoints.
Deliberately does NOT import proxy code (no `from proxy...`) — it mirrors
proxy/platform/microvm_manager.py's call shapes in spirit, keeping a clean
cut between the standalone pool_manager service and the proxy (design §1).
"""

import asyncio
import functools
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

from pool_manager.config import REBIND_TIMEOUT_SECONDS, RESTORE_TIMEOUT_SECONDS
from pool_manager.vm_backend import (
    BackendError,
    QuotaExceededError,
    RUNNING,
    VM_DEAD_STATES,
    VmBackend,
    VmHandle,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0

# resume() poll schedule: a warm spare usually reaches RUNNING within a second
# of ResumeMicrovm, so poll get_microvm first after 0.3 s, then every 0.2 s for
# the first ~3 s, then every 0.5 s. A throttled get_microvm doubles the next
# delay (never below the normal one), capped at 2 s. Mirrors the proxy's
# cold_launch_poll_delay (proxy/platform/routes/microvm.py).
_RESUME_FIRST_DELAY_S = 0.3
_RESUME_FAST_DELAY_S = 0.2
_RESUME_FAST_WINDOW_S = 3.0
_RESUME_SLOW_DELAY_S = 0.5
_RESUME_MAX_DELAY_S = 2.0
_RESUME_THROTTLE_CODES = {
    "ThrottlingException", "TooManyRequestsException", "Throttling", "RequestLimitExceeded",
}

# Seams so tests can drive resume() with a fake clock (used only by resume).
_monotonic = time.monotonic


async def _sleep(s: float) -> None:
    await asyncio.sleep(s)


def resume_poll_delay(attempt: int, elapsed_s: float, prev_delay: float = 0.0,
                      throttled: bool = False) -> float:
    """Seconds to sleep BEFORE resume poll number `attempt` (1-based).

    0.3 s before the first poll, 0.2 s while `elapsed_s` (time since the poll
    phase began) is under 3 s, 0.5 s after that. After a throttled poll the
    delay doubles from `prev_delay` (never below the normal delay), capped at
    _RESUME_MAX_DELAY_S."""
    if attempt <= 1:
        base = _RESUME_FIRST_DELAY_S
    elif elapsed_s < _RESUME_FAST_WINDOW_S:
        base = _RESUME_FAST_DELAY_S
    else:
        base = _RESUME_SLOW_DELAY_S
    if throttled:
        return min(_RESUME_MAX_DELAY_S, max(base, prev_delay * 2))
    return base

# Control-plane client tuning: botocore "standard" retry mode (exponential
# backoff + jitter on throttling/transient errors), bounded connect/read
# timeouts so a hung control-plane call can't pin an executor thread forever,
# and TCP keep-alive for the long-lived pooled connections.
_LAMBDA_CLIENT_CONFIG = Config(
    retries={"mode": "standard"},
    connect_timeout=5,
    read_timeout=20,
    tcp_keepalive=True,
)


class _Borrowed:
    """Async context manager that lends out a shared AsyncClient WITHOUT
    closing it on exit, so call sites keep the `async with factory() as c:`
    shape while reusing one connection pool across calls."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map_client_error(e: Exception) -> Exception:
    """Map a boto3/botocore exception onto the VmBackend error surface
    (design §8). Quota breaches -> QuotaExceededError; everything else ->
    generic BackendError. Fails safe: an unrecognized/unconfirmed quota error
    code simply falls through to the generic BackendError below it."""
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ServiceQuotaExceededException":  # unconfirmed, design §8
            return QuotaExceededError(str(e))
    return BackendError(str(e))


class AwsVmBackend(VmBackend):
    """Real VmBackend implementation against AWS `lambda-microvms`.

    Constructed with every value it needs as a keyword argument — no env
    reads inside __init__ (design §2). The boto3 client is built EAGERLY so
    that (a) tests can attach a botocore.stub.Stubber immediately after
    construction with no warm-up call, and (b) there's no check-then-act
    race on client construction under PoolManager's concurrent fill loop.
    """

    def __init__(
        self,
        *,
        region: str,
        image_arn_base: str,
        exec_role_arn: str,
        ingress_connector: str,
        shell_ingress_connector: str,
        egress_connector: str,
        idle_timeout_seconds: int,
        max_duration_seconds: int,
        auth_token_ttl_minutes: int = 30,
        http_timeout_seconds: float = REBIND_TIMEOUT_SECONDS,
        restore_timeout_seconds: float = RESTORE_TIMEOUT_SECONDS,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.region = region
        self.image_arn_base = image_arn_base
        self.exec_role_arn = exec_role_arn
        self.ingress_connector = ingress_connector
        self.shell_ingress_connector = shell_ingress_connector
        self.egress_connector = egress_connector
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_duration_seconds = max_duration_seconds
        self.auth_token_ttl_minutes = auth_token_ttl_minutes
        self.http_timeout_seconds = http_timeout_seconds
        # /restore-state does heavy S3 I/O (download + deserialize the namespace +
        # files) and can legitimately run tens of seconds — far longer than the 10s
        # control-call timeout. Using the 10s default for it made the pool's httpx
        # client give up ("restore_state failed (no response)") on a VM that was
        # still working, failing the grant and forcing a slow cold-launch fallback.
        # Give restore its own budget (RESTORE_TIMEOUT_SECONDS); the proxy's
        # restore-grant bound (PROXY_GRANT_RESTORE_BOUND_SECONDS) sits above the
        # whole grant budget and is the outer bound for the user-facing launch.
        self.restore_timeout_seconds = restore_timeout_seconds
        self.executor = executor

        self._token_cache: dict[str, dict] = {}
        self._lambda_client = boto3.client(
            "lambda-microvms", region_name=self.region, config=_LAMBDA_CLIENT_CONFIG)
        # One shared AsyncClient for /health, /rebind, /restore-state (built
        # lazily on first use, closed by aclose() from the pool lifespan).
        self._http: httpx.AsyncClient | None = None
        self._http_loop = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _client(self):
        return self._lambda_client

    def _shared_http(self) -> httpx.AsyncClient:
        """Lazily build the shared AsyncClient. Transport `retries=2` retries
        ONLY connection establishment (ConnectError/ConnectTimeout — the request
        was provably never sent), so the non-idempotent POSTs get no re-send.
        Pooled connections are bound to the event loop they were opened on, so
        the client is rebuilt if called from a different running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            # NOTE: with a custom transport httpx intentionally does NOT honor
            # HTTP(S)_PROXY / ALL_PROXY / NO_PROXY env vars (boto3 is unaffected).
            # See SETUP.md → Configuration ("Outbound HTTP proxy env vars").
            self._http = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(
                    retries=2,
                    limits=httpx.Limits(max_connections=None,
                                        max_keepalive_connections=50,
                                        keepalive_expiry=15.0),
                ),
                timeout=self.http_timeout_seconds,
            )
            self._http_loop = loop
        return self._http

    def _http_client_factory(self, timeout: float | None = None):
        """Seam for tests: override on an instance to inject a MockTransport-
        backed AsyncClient. Default lends out the shared client via a
        non-closing async context manager. Call sites pass an explicit
        per-request `timeout=` (so `timeout` here is accepted for seam
        compatibility; a test override may ignore it)."""
        return _Borrowed(self._shared_http())

    async def aclose(self) -> None:
        """Close the shared AsyncClient (called from the pool lifespan).
        Idempotent."""
        client, self._http, self._http_loop = self._http, None, None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def _call(self, fn, **kwargs):
        """Run a blocking boto3 call off the event loop via
        loop.run_in_executor (NOT asyncio.to_thread, which ignores
        self.executor — design §4)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, functools.partial(fn, **kwargs))

    def _image_arn(self, arch: str, size_mib: int) -> str:
        return f"{self.image_arn_base}-{arch}-{size_mib}"

    async def _get_auth_token(self, microvm_id: str) -> str:
        cached = self._token_cache.get(microvm_id)
        if cached and time.time() < cached["expires_at"]:
            return cached["token"]
        resp = await self._call(
            self._client().create_microvm_auth_token,
            microvmIdentifier=microvm_id,
            expirationInMinutes=self.auth_token_ttl_minutes,
            allowedPorts=[{"allPorts": {}}],
        )
        token = resp["authToken"]["X-aws-proxy-auth"]
        self._token_cache[microvm_id] = {
            "token": token,
            "expires_at": time.time() + (self.auth_token_ttl_minutes - 5) * 60,
        }
        return token

    # ------------------------------------------------------------------
    # VmBackend
    # ------------------------------------------------------------------

    async def launch(self, arch: str, size_mib: int, fake_session_id: str) -> VmHandle:
        run_hook_payload = json.dumps({
            "session_id": fake_session_id,
            "notebook_name": None,
            "notebook_id": None,
            "checkpoint_enabled": False,
            "persistence_mode": "none",
            "memory_mib": size_mib,
            "architecture": arch,
        })
        try:
            resp = await self._call(
                self._client().run_microvm,
                imageIdentifier=self._image_arn(arch, size_mib),
                ingressNetworkConnectors=[self.ingress_connector, self.shell_ingress_connector],
                egressNetworkConnectors=[self.egress_connector],
                idlePolicy={
                    "autoResumeEnabled": True,
                    "maxIdleDurationSeconds": self.idle_timeout_seconds,
                    "suspendedDurationSeconds": self.max_duration_seconds,
                },
                maximumDurationInSeconds=self.max_duration_seconds,
                executionRoleArn=self.exec_role_arn,
                runHookPayload=run_hook_payload,
            )
        except Exception as e:
            raise _map_client_error(e) from e
        return VmHandle(
            vm_id=resp["microvmId"],
            endpoint=resp["endpoint"],
            arch=arch,
            size_mib=size_mib,
            launched_at=_now_iso(),
            meta={},
        )

    async def wait_healthy(self, vm: VmHandle, timeout: float) -> None:
        token = await self._get_auth_token(vm.vm_id)
        deadline = time.monotonic() + timeout
        async with self._http_client_factory() as client:
            while True:
                try:
                    resp = await client.get(
                        f"https://{vm.endpoint}/health",
                        headers={"X-aws-proxy-auth": token},
                        timeout=self.http_timeout_seconds,
                    )
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise BackendError(f"wait_healthy timed out for {vm.vm_id}")
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def suspend(self, vm: VmHandle) -> None:
        try:
            await self._call(self._client().suspend_microvm, microvmIdentifier=vm.vm_id)
        except Exception as e:
            raise _map_client_error(e) from e

    async def resume(self, vm: VmHandle, timeout: float) -> str:
        t_start = _monotonic()
        try:
            await self._call(self._client().resume_microvm, microvmIdentifier=vm.vm_id)
        except Exception as e:
            raise _map_client_error(e) from e
        t_poll = _monotonic()

        deadline = t_poll + timeout
        polls = 0
        delay = 0.0
        throttled = False
        state = None
        while True:
            # Sleep BEFORE each get_microvm: the VM is never RUNNING the
            # instant ResumeMicrovm returns, so an immediate poll is wasted.
            delay = resume_poll_delay(polls + 1, _monotonic() - t_poll, delay, throttled)
            await _sleep(delay)
            polls += 1
            throttled = False
            try:
                detail = await self._call(self._client().get_microvm, microvmIdentifier=vm.vm_id)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code not in _RESUME_THROTTLE_CODES:
                    # (d) VM vanished mid-resume — never observed a dead state,
                    # so this is NOT case (b); it is a hard failure.
                    raise _map_client_error(e) from e
                throttled = True  # back off and poll again
            else:
                state = detail["state"]
                if state == RUNNING or state in VM_DEAD_STATES:
                    # (a) RUNNING, or (b) dead: returned, not raised; the
                    # caller (PoolManager.grant) decides what a dead state means.
                    t_end = _monotonic()
                    logger.info(
                        f"resume {vm.vm_id}: resume_call={(t_poll - t_start) * 1000:.0f}ms "
                        f"resume_poll={(t_end - t_poll) * 1000:.0f}ms polls={polls} state={state}")
                    return state
            if _monotonic() >= deadline:
                # (c) still alive but never reached RUNNING in time.
                raise BackendError(f"resume timed out for {vm.vm_id}, last state={state}")

    async def rebind(self, vm: VmHandle, identity_payload: dict) -> None:
        token = await self._get_auth_token(vm.vm_id)
        try:
            async with self._http_client_factory() as client:
                resp = await client.post(
                    f"https://{vm.endpoint}/rebind",
                    headers={"X-aws-proxy-auth": token},
                    json=identity_payload,
                    timeout=self.http_timeout_seconds,
                )
        except httpx.HTTPError as e:
            raise BackendError(f"rebind failed (no response) for {vm.vm_id}: {e}") from e
        if resp.status_code == 404:
            raise BackendError(
                f"rebind failed (404) for {vm.vm_id}: image may predate /rebind "
                f"(requires image rebuild, see design §9)"
            )
        if resp.status_code != 200:
            raise BackendError(f"rebind failed ({resp.status_code}) for {vm.vm_id}: {resp.text}")

    async def restore_state(self, vm: VmHandle, restore_from_session: str) -> None:
        token = await self._get_auth_token(vm.vm_id)
        try:
            # Restore does heavy S3 I/O on the VM; use the long restore timeout, not
            # the 10s control-call default (see restore_timeout_seconds). Passing
            # the timeout to the factory keeps the test seam intact.
            async with self._http_client_factory(
                    timeout=self.restore_timeout_seconds) as client:
                resp = await client.post(
                    f"https://{vm.endpoint}/restore-state",
                    headers={"X-aws-proxy-auth": token},
                    json={"session_id": restore_from_session},
                    timeout=self.restore_timeout_seconds,
                )
        except httpx.HTTPError as e:
            raise BackendError(f"restore_state failed (no response) for {vm.vm_id}: {e}") from e
        if resp.status_code != 200:
            raise BackendError(f"restore_state failed ({resp.status_code}) for {vm.vm_id}: {resp.text}")
        data = resp.json()
        if not data.get("success"):
            raise BackendError(data.get("error"))

    async def terminate(self, vm: VmHandle) -> None:
        try:
            await self._call(self._client().terminate_microvm, microvmIdentifier=vm.vm_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return  # already gone — goal state achieved
            raise _map_client_error(e) from e
        except Exception as e:
            raise _map_client_error(e) from e

    async def get_state(self, vm_id: str) -> str:
        try:
            detail = await self._call(self._client().get_microvm, microvmIdentifier=vm_id)
            return detail["state"]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = str(e)
            if code == "ResourceNotFoundException" or "ResourceNotFound" in msg or "NotFound" in msg:
                return "TERMINATED"
            raise BackendError(str(e)) from e

    async def list_all(self) -> list[VmHandle]:
        items = []
        next_token = None
        # Request the API max page (50; ListMicrovms rejects maxResults > 50) so a
        # list bloated by not-yet-reaped TERMINATED VMs is walked in ~1/5 the
        # round-trips (measured: 5 pages vs ~22 for a 219-VM account). Each
        # round-trip is paced by adaptive-retry backoff under ListMicrovms
        # throttling, so page COUNT — not row count — dominates latency; fewer
        # pages directly cuts reconcile time and throttle exposure. See the full
        # rationale + benchmark in MicrovmManager.list_vms() (proxy).
        for _ in range(40):  # max_pages safety bound: 40 * 50 = 2000 VMs
            kwargs = {"maxResults": 50}
            if next_token:
                kwargs["nextToken"] = next_token
            try:
                resp = await self._call(self._client().list_microvms, **kwargs)
            except Exception as e:
                raise _map_client_error(e) from e
            items.extend(resp.get("items", []) or [])
            next_token = resp.get("nextToken")
            if not next_token:
                break
        # list_microvms returns VMs in EVERY state, including TERMINATED/
        # TERMINATING/FAILED ones that linger in the control plane for a while.
        # list_all() is consumed by reconcile_once() (stale-row detection: a
        # pool_vms row whose VM is not in this list is gone) and by
        # reset_local_pool's local-dev orphan sweep. A dead VM must count as
        # gone, and re-terminating it every cycle would be a wasted API call that
        # floods the logs, so dead states are excluded here.
        return [
            VmHandle(
                vm_id=item["microvmId"],
                endpoint=item.get("endpoint", ""),
                arch="",
                size_mib=0,
                launched_at=item.get("launchedAt", ""),
                # image_arn / started_at feed the local-dev orphan sweep
                # (reset_local_pool), which only touches THIS app's image family
                # and only VMs older than a grace window.
                meta={"state": item.get("state"),
                      "image_arn": item.get("imageArn", ""),
                      "started_at": item.get("startedAt")},
            )
            for item in items
            if item.get("state") not in VM_DEAD_STATES
        ]
