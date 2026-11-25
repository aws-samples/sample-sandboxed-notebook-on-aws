#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the single-flight proxy auth token
(proxy/platform/microvm_manager.py::MicrovmManager.get_auth_token / _mint_auth_token).

Covers:
  - 8 concurrent callers on a cache miss => exactly 1 CreateMicrovmAuthToken, same token;
  - the first waiter cancelled (wait_for 0.05 s) => the mint still completes and is
    cached; the next call makes 0 new mints;
  - fail-then-succeed => 2 SDK calls, token returned and cached;
  - fail-twice => every waiter raises, nothing cached, the next call mints again;
  - the in-flight dict is empty after each flight;
  - no token value appears in captured logs.

No AWS, no network: get_lambda_launch_client is replaced by an in-process fake.

Run:
    .venv/bin/python tests/proxy/test_auth_token_single_flight.py
"""
import asyncio
import logging
import os
import sys
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class _FakeLaunchClient:
    """create_microvm_auth_token counts calls, blocks on a gate, and can fail."""

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times
        self.gate = threading.Event()
        self._lock = threading.Lock()

    def create_microvm_auth_token(self, microvmIdentifier, expirationInMinutes, allowedPorts):
        with self._lock:
            self.calls += 1
            n = self.calls
        self.gate.wait(5)
        if n <= self.fail_times:
            raise RuntimeError("simulated control-plane failure")
        return {"authToken": {"X-aws-proxy-auth": f"SECRET-TOKEN-{microvmIdentifier}-{n}"}}


def _manager(client):
    from proxy.platform.microvm_manager import MicrovmManager
    mgr = MicrovmManager()
    mgr.get_lambda_launch_client = lambda: client
    return mgr


async def _release_soon(client, delay=0.1):
    await asyncio.sleep(delay)
    client.gate.set()


async def _concurrent():
    client = _FakeLaunchClient()
    mgr = _manager(client)
    rel = asyncio.create_task(_release_soon(client))
    toks = await asyncio.gather(*(mgr.get_auth_token("vm-a") for _ in range(8)))
    await rel
    check("8 concurrent callers -> 1 mint", client.calls == 1, client.calls)
    check("8 concurrent callers -> same token", len(set(toks)) == 1, toks)
    check("in-flight empty after concurrent flight", not mgr._token_inflight, mgr._token_inflight)
    cached = await mgr.token_cache.get("vm-a")
    check("token cached", cached and cached["token"] == toks[0])


async def _cancel_first_waiter():
    client = _FakeLaunchClient()
    mgr = _manager(client)
    cancelled = False
    try:
        await asyncio.wait_for(mgr.get_auth_token("vm-b"), 0.05)
    except asyncio.TimeoutError:
        cancelled = True
    check("first waiter timed out (cancelled)", cancelled)
    check("mint still in flight after waiter cancel", "vm-b" in mgr._token_inflight)
    client.gate.set()
    for _ in range(100):
        if not mgr._token_inflight:
            break
        await asyncio.sleep(0.02)
    cached = await mgr.token_cache.get("vm-b")
    check("mint completed + cached after cancel", bool(cached and cached.get("token")))
    check("in-flight empty after cancelled flight", not mgr._token_inflight, mgr._token_inflight)
    before = client.calls
    tok = await mgr.get_auth_token("vm-b")
    check("next call makes 0 new mints", client.calls == before, (before, client.calls))
    check("next call returns cached token", tok == cached["token"])


async def _fail_then_succeed():
    client = _FakeLaunchClient(fail_times=1)
    client.gate.set()
    mgr = _manager(client)
    tok = await mgr.get_auth_token("vm-c")
    check("fail-then-succeed -> 2 SDK calls", client.calls == 2, client.calls)
    cached = await mgr.token_cache.get("vm-c")
    check("fail-then-succeed -> cached", cached and cached["token"] == tok)
    check("in-flight empty after retry flight", not mgr._token_inflight)


async def _fail_twice():
    client = _FakeLaunchClient(fail_times=2)
    mgr = _manager(client)
    rel = asyncio.create_task(_release_soon(client, 0.05))
    res = await asyncio.gather(*(mgr.get_auth_token("vm-d") for _ in range(4)),
                               return_exceptions=True)
    await rel
    check("fail-twice -> every waiter raises",
          all(isinstance(r, RuntimeError) for r in res), res)
    check("fail-twice -> 2 SDK calls total", client.calls == 2, client.calls)
    check("fail-twice -> nothing cached", await mgr.token_cache.get("vm-d") is None)
    check("in-flight empty after failed flight", not mgr._token_inflight)
    client.fail_times = 0
    tok = await mgr.get_auth_token("vm-d")
    check("next call after failure mints again", client.calls == 3 and tok.endswith("-3"),
          (client.calls, tok))


async def _fail_no_waiters():
    # All waiters cancelled, then the mint fails: no "never retrieved" noise.
    client = _FakeLaunchClient(fail_times=2)
    mgr = _manager(client)
    try:
        await asyncio.wait_for(mgr.get_auth_token("vm-e"), 0.02)
    except asyncio.TimeoutError:
        pass
    client.gate.set()
    for _ in range(100):
        if not mgr._token_inflight:
            break
        await asyncio.sleep(0.02)
    check("failed flight with no waiters -> in-flight empty", not mgr._token_inflight)
    check("failed flight with no waiters -> nothing cached",
          await mgr.token_cache.get("vm-e") is None)


def main():
    cap = _LogCapture()
    root = logging.getLogger()
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    asyncio_errors = []
    try:
        for fn in (_concurrent, _cancel_first_waiter, _fail_then_succeed, _fail_twice,
                   _fail_no_waiters):
            print(f"\n-- {fn.__name__.strip('_')} --")

            async def _run(fn=fn):
                asyncio.get_running_loop().set_exception_handler(
                    lambda loop, ctx: asyncio_errors.append(ctx.get("message")))
                await fn()

            asyncio.run(_run())
    finally:
        root.removeHandler(cap)
    print("\n-- logs --")
    check("no 'never retrieved' asyncio errors", not asyncio_errors, asyncio_errors)
    check("no token value in logs", not any("SECRET-TOKEN" in l for l in cap.lines))
    check("mint logged 'Fetching new auth token'",
          any("Fetching new auth token for vm-a" in l for l in cap.lines))
    warn = [l for l in cap.lines if "retrying once" in l]
    check("retry WARNING names exception type only",
          warn and all("RuntimeError" in l and "simulated" not in l for l in warn), warn)

    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
