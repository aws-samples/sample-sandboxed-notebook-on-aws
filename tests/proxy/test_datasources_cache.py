#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the GET /datasources TTL cache (proxy-log-chatter B4):
registry.discover_all_cached() behind proxy/platform/routes/sessions.py
list_datasources.
Covers:
  - a second request within the TTL is a cache hit (no provider discover());
  - after the TTL expires the next request re-discovers;
  - ?refresh=true bypasses the cached entry;
  - each mutation route invalidates it: DELETE /datasources/s3-file (s3 and
    sagemaker_endpoint, success and failure), POST sagemaker-endpoint/lifecycle
    (stop/start), POST /datasources/promote-attachment;
  - per-session overlay isolation: session A never receives session B's
    promoted files, and the no-session list has neither;
  - single-flight: 8 concurrent misses for one key run ONE discovery;
  - a discovery that started before an invalidation is not stored;
  - sagemaker_endpoint is discovered live on every request (status never stale);
  - DATASOURCES_CACHE_TTL_SECONDS=0 disables the cache;
  - the provider order and the response shape are unchanged.
No AWS: registry.all_providers / get_provider are replaced by in-memory fakes;
the route runs in a FastAPI app over httpx.ASGITransport.
Run:
    .venv/bin/python tests/proxy/test_datasources_cache.py
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from proxy.platform.datasources import registry  # noqa: E402
from proxy.platform.datasources.interface import SourceRef  # noqa: E402
from proxy.platform.routes.sessions import router as sessions_router  # noqa: E402
_checks = []
def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
class FakeProvider:
    """Counts discover() calls; S3 adds a per-session overlay like the real one."""
    def __init__(self, source_type, delay=0.0):
        self.source_type = source_type
        self.calls = 0
        self.delay = delay
        self.items = [f"{source_type}-shared"]
        self.overlay: dict = {}   # session_id -> [names]
        self.status = "InService"
        self.fail_delete = False
        self.deleted = []
    async def discover(self, session_id=None):
        self.calls += 1
        names = list(self.items)          # snapshot at call time, like a real listing
        if session_id:
            names += self.overlay.get(session_id, [])
        if self.delay:
            await asyncio.sleep(self.delay)
        return [SourceRef(source_type=self.source_type, source_id=n, display_name=n,
                          extra={"status": self.status}) for n in names]
    # route hooks
    def is_deletable(self, source_id):
        return True
    def delete(self, source_id):
        if self.fail_delete:
            raise RuntimeError("boom")
        self.deleted.append(source_id)
    def supports_lifecycle(self, source_id):
        return True
    def lifecycle_action(self, source_id, action):
        return f"{action} ok"
    def promote_attachment(self, s3_uri, session_id):
        name = s3_uri.rsplit("/", 1)[-1]
        self.overlay.setdefault(session_id, []).append(name)
        return {"s3_uri": f"s3://b/user-data/{session_id}/{name}", "key": f"user-data/{session_id}/{name}", "name": name}
S3 = FakeProvider("s3")
ATHENA = FakeProvider("athena")
SM = FakeProvider("sagemaker_endpoint")
PROVIDERS = [S3, ATHENA, SM]
registry.all_providers = lambda execute_fn=None: list(PROVIDERS)
registry.get_provider = lambda st, execute_fn=None: {p.source_type: p for p in PROVIDERS}.get(st)
import proxy.platform.routes.sessions as sessions_mod  # noqa: E402
sessions_mod._trigger_entity_intel_async = lambda *a, **k: None   # no Bedrock/S3 in tests
class FakeVmManager:
    def get_artifacts_bucket(self):
        return "bucket"
app = FastAPI()
app.include_router(sessions_router)
app.state.vm_manager = FakeVmManager()
def reset(ttl="20"):
    os.environ["DATASOURCES_CACHE_TTL_SECONDS"] = ttl
    registry.invalidate_datasources_cache("test reset")
    registry._ds_inflight.clear()
    for p in PROVIDERS:
        p.calls = 0
async def get(client, session=None, refresh=False):
    headers = {"X-Session-Id": session} if session else {}
    r = await client.get("/datasources" + ("?refresh=true" if refresh else ""), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()
def names(body):
    return [s["source_id"] for s in body["sources"]]
async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("\n-- hit within TTL / miss after TTL / refresh=true --")
        reset()
        b1 = await get(c, "A")
        b2 = await get(c, "A")
        check("first request discovers every provider once", (S3.calls, ATHENA.calls) == (1, 1))
        check("second request within TTL is a cache hit", (S3.calls, ATHENA.calls) == (1, 1))
        check("cached response is identical", b1 == b2)
        check("provider order unchanged (s3, athena, sagemaker)",
              names(b1) == ["s3-shared", "athena-shared", "sagemaker_endpoint-shared"])
        check("response shape unchanged",
              {"sources", "source_types", "s3", "dynamodb", "athena", "artifact_bucket", "athena_workgroup"} <= set(b1))
        check("sagemaker_endpoint discovered live on every request", SM.calls == 2)
        SM.status = "Deleting"
        b3 = await get(c, "A")
        sm_rows = [s for s in b3["sources"] if s["source_type"] == "sagemaker_endpoint"]
        check("sagemaker status change is visible immediately (cached s3/athena)",
              sm_rows and sm_rows[0]["status"] == "Deleting" and S3.calls == 1)
        SM.status = "InService"
        await get(c, "A", refresh=True)
        check("?refresh=true bypasses the cache", S3.calls == 2 and ATHENA.calls == 2)
        await get(c, "A")
        check("refresh=true repopulated the cache", S3.calls == 2)
        registry._ds_cache["A"]["at"] -= 3600   # age the entry past the TTL
        await get(c, "A")
        check("after the TTL the next request re-discovers", S3.calls == 3)

        print("\n-- invalidation by every mutation route --")
        mutations = [
            ("DELETE s3-file (s3)", lambda: c.delete("/datasources/s3-file?source_id=s3://b/user-data/x.csv&source_type=s3"), 200),
            ("DELETE s3-file (sagemaker_endpoint)", lambda: c.delete("/datasources/s3-file?source_id=microvm-ep-1&source_type=sagemaker_endpoint"), 200),
            ("POST lifecycle stop", lambda: c.post("/datasources/sagemaker-endpoint/lifecycle?source_id=microvm-ep-1&action=stop"), 200),
            ("POST lifecycle start", lambda: c.post("/datasources/sagemaker-endpoint/lifecycle?source_id=microvm-ep-1&action=start"), 200),
            ("POST promote-attachment", lambda: c.post("/datasources/promote-attachment",
                                                     json={"session_id": "A", "s3_uri": "s3://b/attachments/A/u/new.csv"}), 200),
        ]
        for label, call, want in mutations:
            reset()
            await get(c, "A")
            before = S3.calls
            r = await call()
            check(f"{label} returns {want}", r.status_code == want, f"{r.status_code} {r.text[:120]}")
            await get(c, "A")
            check(f"{label} invalidates the cache", S3.calls == before + 1, f"calls {before}->{S3.calls}")
        reset()
        await get(c, "A")
        S3.fail_delete = True
        r = await c.delete("/datasources/s3-file?source_id=s3://b/user-data/y.csv&source_type=s3")
        S3.fail_delete = False
        before = S3.calls
        await get(c, "A")
        check("a FAILED delete still invalidates (500 + re-discover)", r.status_code == 500 and S3.calls == before + 1)
        reset()
        b = await get(c, "A")
        check("promoted file visible right after promote (invalidation)", "new.csv" in names(b))

        print("\n-- per-session overlay isolation --")
        reset()
        S3.overlay = {"A": ["a-only.csv"], "B": ["b-only.csv"]}
        a = await get(c, "A")
        bb = await get(c, "B")
        none = await get(c, None)
        a2 = await get(c, "A")
        check("session A sees its own overlay", "a-only.csv" in names(a))
        check("session A never sees B's overlay (fresh and cached)",
              "b-only.csv" not in names(a) and "b-only.csv" not in names(a2))
        check("session B sees only its own overlay", "b-only.csv" in names(bb) and "a-only.csv" not in names(bb))
        check("no-session list has neither overlay", not ({"a-only.csv", "b-only.csv"} & set(names(none))))
        check("one cache entry per session key", set(registry._ds_cache) == {"A", "B", ""})

        print("\n-- single-flight under concurrent calls --")
        reset()
        S3.delay = ATHENA.delay = 0.2
        bodies = await asyncio.gather(*(get(c, "A") for _ in range(8)))
        S3.delay = ATHENA.delay = 0.0
        check("8 concurrent misses run ONE discovery", (S3.calls, ATHENA.calls) == (1, 1), f"{S3.calls},{ATHENA.calls}")
        check("all 8 callers got the same list", all(names(x) == names(bodies[0]) for x in bodies))
        check("no in-flight task left behind", not registry._ds_inflight)

        print("\n-- invalidation during an in-flight discovery --")
        reset()
        S3.delay = 0.2
        t = asyncio.create_task(get(c, "A"))
        await asyncio.sleep(0.05)
        registry.invalidate_datasources_cache("mutation mid-flight")
        S3.items = ["s3-shared", "s3-added"]
        S3.delay = 0.0
        late = await get(c, "A")          # must not join the stale discovery
        stale = await t                   # finishes AFTER the fresh one
        check("a request after the invalidation does not join the stale discovery", "s3-added" in names(late))
        check("the in-flight request itself still got its (older) answer", "s3-added" not in names(stale))
        check("the stale discovery result was not stored over the fresh one",
              "s3-added" in [r.source_id for r in registry._ds_cache["A"]["by_type"]["s3"]])
        S3.items = ["s3-shared"]

        print("\n-- TTL 0 disables the cache --")
        reset("0")
        await get(c, "A"); await get(c, "A")
        check("DATASOURCES_CACHE_TTL_SECONDS=0 -> every request discovers", S3.calls == 2)
        check("nothing cached when disabled", not registry._ds_cache)
        reset()
async def _loop_check():
    print("\n-- the cache never blocks the event loop --")
    reset()
    S3.delay = 0.3
    ticks = 0
    async def ticker():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.02)
            ticks += 1
    await asyncio.gather(registry.discover_all_cached(session_id="A"), ticker())
    S3.delay = 0.0
    check("event loop kept running during a discovery", ticks == 10, f"ticks={ticks}")
asyncio.run(main())
asyncio.run(_loop_check())
failed = [n for n, ok in _checks if not ok]
print(f"\n{len(_checks) - len(failed)}/{len(_checks)} checks passed")
sys.exit(1 if failed else 0)
