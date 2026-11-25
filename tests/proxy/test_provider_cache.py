#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the shared provider instances in
proxy/platform/datasources/registry.py (code-review I-B-05).

Covers:
  - repeated get_provider("s3") returns one instance;
  - a second get_schema for the same source, through a second get_provider
    call, is served from the provider's own cache (the blocking AWS call is
    patched and counted);
  - the local provider (requires_vm_execution) is built per call with its own
    execute_fn;
  - all_providers / provider_metadata reuse the shared instances;
  - 16 concurrent get_provider calls on a reset registry build one instance;
  - reset_provider_instances drops the shared instances.

No AWS: the S3 schema fetch is replaced by a counting stub.
Run:
    .venv/bin/python tests/proxy/test_provider_cache.py
"""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")

from proxy.platform.datasources import registry  # noqa: E402
from proxy.platform.datasources.interface import SourceSchema  # noqa: E402
from proxy.platform.datasources.s3 import S3SchemaProvider  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


def test_shared_instance_and_schema_cache():
    print("\n-- shared instance + schema cache --")
    registry.reset_provider_instances()
    a = registry.get_provider("s3")
    b = registry.get_provider("s3")
    check("get_provider('s3') returns the same instance", a is b)

    calls = []
    orig = S3SchemaProvider._get_schema_blocking

    def _fake(self, source_id, bucket, key):
        calls.append(source_id)
        return SourceSchema(source_type="s3", source_id=source_id, display_name=key)

    S3SchemaProvider._get_schema_blocking = _fake
    try:
        sid = "s3://bucket/data/file.csv"
        first = asyncio.run(registry.get_provider("s3").get_schema(sid))
        second = asyncio.run(registry.get_provider("s3").get_schema(sid))
    finally:
        S3SchemaProvider._get_schema_blocking = orig
    check("first get_schema fetched once", calls == [sid], calls)
    check("second get_schema (new get_provider call) served from cache",
          len(calls) == 1 and second is first, calls)

    check("all_providers reuses the shared s3 instance",
          any(p is a for p in registry.all_providers()))
    meta = {m["source_type"] for m in registry.provider_metadata()}
    check("provider_metadata lists every registered type",
          meta == set(registry._REGISTRY), sorted(meta))


def test_local_provider_not_shared():
    print("\n-- local provider stays per call --")
    local_types = [t for t, c in registry._REGISTRY.items()
                   if getattr(c, "requires_vm_execution", False)]
    check("a VM-executed source type exists", bool(local_types))
    if not local_types:
        return
    t = local_types[0]

    async def fn1(sid, code):
        return None

    async def fn2(sid, code):
        return None

    p1 = registry.get_provider(t, execute_fn=fn1)
    p2 = registry.get_provider(t, execute_fn=fn2)
    check("local provider: a new instance per call", p1 is not p2)
    check("local provider: each keeps its own execute_fn",
          p1._execute_fn is fn1 and p2._execute_fn is fn2)


def test_concurrent_build_once():
    print("\n-- concurrent first use --")
    registry.reset_provider_instances()
    built = []
    orig_init = S3SchemaProvider.__init__

    def _counting_init(self, *a, **k):
        built.append(1)
        orig_init(self, *a, **k)

    S3SchemaProvider.__init__ = _counting_init
    barrier = threading.Barrier(16)
    got = [None] * 16

    def _worker(i):
        barrier.wait()
        got[i] = registry.get_provider("s3")

    try:
        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        S3SchemaProvider.__init__ = orig_init
    check("16 concurrent get_provider calls return one instance",
          len({id(x) for x in got}) == 1 and got[0] is not None)
    check("the instance was constructed once", len(built) == 1, len(built))

    registry.reset_provider_instances()
    check("reset_provider_instances gives a fresh instance",
          registry.get_provider("s3") is not got[0])


def main():
    test_shared_instance_and_schema_cache()
    test_local_provider_not_shared()
    test_concurrent_build_once()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
