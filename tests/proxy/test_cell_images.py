#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for proxy/notebook/cell_images.py (chart PNG persistence).

Fake S3 client (no AWS), fake storage key lookup. Covers the key helpers,
prepare(), bucket() resolution, upload params / coalescing / failure logging,
get() (pending wait without cancelling, NoSuchKey, size/type checks), the
guarded orphan delete (H1 sequences), drain_pending, the paginated prefix
delete, and the off-loop rule (a 0.2 s sleeping fake S3 call never blocks a
10 ms ticker).

Run:  .venv/bin/python tests/proxy/test_cell_images.py   (exit 0 = pass)
"""
import asyncio
import base64
import hashlib
import logging
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from proxy.notebook import cell_images as ci  # noqa: E402
from proxy.storage import _cell_fields as cf  # noqa: E402
from proxy.storage import astorage  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


PNG = b"\x89PNG\r\n\x1a\n" + b"A" * 300
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 300
SHA = hashlib.sha256(PNG).hexdigest()
SHA_B = hashlib.sha256(PNG_B).hexdigest()


def data_url(png, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(png).decode()


class NoSuchKey(Exception):
    pass


class _Body:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def read(self, n=-1):
        return self.data if n is None or n < 0 else self.data[:n]

    def close(self):
        self.closed = True


class FakeS3:
    def __init__(self, sleep=0.0, fail_put=False, page_size=1000):
        self.objects = {}
        self.puts = []
        self.deletes = []
        self.lists = 0
        self.sleep = sleep
        self.fail_put = fail_put
        self.page_size = page_size
        self.content_length_override = None

    def put_object(self, **kw):
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail_put:
            raise RuntimeError("AccessDenied")
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        data = self.objects[Key]
        return {"Body": _Body(data),
                "ContentLength": self.content_length_override or len(data)}

    def delete_objects(self, Bucket, Delete):
        if self.sleep:
            time.sleep(self.sleep)
        keys = [o["Key"] for o in Delete["Objects"]]
        assert len(keys) <= 1000
        self.deletes.append(keys)
        for k in keys:
            self.objects.pop(k, None)
        return {}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        if self.sleep:
            time.sleep(self.sleep)
        self.lists += 1
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        more = start + self.page_size < len(keys)
        out = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + self.page_size)
        return out


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def warnings(self):
        return [r for r in self.records if r.levelno >= logging.WARNING]


LOGS = _Logs()
logging.getLogger("proxy.notebook.cell_images").addHandler(LOGS)
logging.getLogger("proxy.notebook.cell_images").setLevel(logging.DEBUG)

_REFERENCED = {}


async def _fake_keys(nid):
    # notebook_cell_image_keys_all shape: {cell_id: [key, ...]}
    return {c: [k] for c, k in _REFERENCED.get(int(nid), {}).items()}


def _reset(fake):
    ci._s3 = lambda: fake
    ci._bucket_cache = "bkt"
    ci._pending.clear()
    ci._recent_put.clear()
    _REFERENCED.clear()
    LOGS.records.clear()


async def _ticker(stop, ticks):
    while not stop.is_set():
        await asyncio.sleep(0.01)
        ticks.append(1)


async def _off_loop(coro_fn):
    """Run coro_fn() next to a 10 ms ticker; return the tick count."""
    stop, ticks = asyncio.Event(), []
    t = asyncio.get_running_loop().create_task(_ticker(stop, ticks))
    await coro_fn()
    stop.set()
    await t
    return len(ticks)


# ─── tests ─────────────────────────────────────────────────────────────────────

def t_key_helpers():
    print("key helpers")
    check("cell_key_segment: safe id kept", cf.cell_key_segment("c-1_A") == "c-1_A")
    check("cell_key_segment: numeric id == its str form", cf.cell_key_segment(7) == "7")
    seg = cf.cell_key_segment("../x/y")
    check("cell_key_segment: unsafe id hashed (h-<sha256>)",
          seg == "h-" + hashlib.sha256(b"../x/y").hexdigest())
    k = cf.image_key(12, "c1", SHA)
    check("image_key layout", k == f"notebook-outputs/12/c1/{SHA}.png")
    check("valid_image_key: own key accepted", cf.valid_image_key(12, "c1", k) == k)
    check("valid_image_key: str notebook id accepted", cf.valid_image_key("12", "c1", k) == k)
    for label, nid, cid, key in (
            ("other notebook", 13, "c1", k),
            ("other cell", 12, "c2", k),
            ("traversal", 12, "c1", f"notebook-outputs/12/c1/../../13/c1/{SHA}.png"),
            ("dot-dot segment", 12, "..", f"notebook-outputs/12/../{SHA}.png"),
            ("uppercase hex", 12, "c1", f"notebook-outputs/12/c1/{SHA.upper()}.png"),
            ("wrong extension", 12, "c1", f"notebook-outputs/12/c1/{SHA}.jpg"),
            ("other prefix", 12, "c1", f"sessions/12/c1/{SHA}.png"),
            ("leading slash", 12, "c1", f"/notebook-outputs/12/c1/{SHA}.png"),
            ("trailing newline", 12, "c1", k + "\n"),
            ("not a str", 12, "c1", 5),
            ("no cell id", 12, None, k)):
        check(f"valid_image_key rejects {label}", cf.valid_image_key(nid, cid, key) is None)
    hk = cf.image_key(3, "../x/y", SHA)
    check("valid_image_key accepts a hashed-segment key for its cell",
          cf.valid_image_key(3, "../x/y", hk) == hk)
    check("image_sha_from_key", cf.image_sha_from_key(k) == SHA and cf.image_sha_from_key("x") is None)


def t_prepare():
    print("prepare")
    LOGS.records.clear()
    r = ci.prepare(5, "c1", data_url(PNG))
    check("good PNG → (key, bytes)", r == (f"notebook-outputs/5/c1/{SHA}.png", PNG))
    n = len(LOGS.records)
    check("'' → None, no log", ci.prepare(5, "c1", "") is None and len(LOGS.records) == n)
    check("None → None", ci.prepare(5, "c1", None) is None)
    check("wrong mime → None", ci.prepare(5, "c1", data_url(PNG, "image/jpeg")) is None)
    check("bad base64 → None", ci.prepare(5, "c1", "data:image/png;base64,@@@") is None)
    check("non-PNG bytes → None", ci.prepare(5, "c1", data_url(b"GIF89a....")) is None)
    old = ci.MAX_IMAGE_BYTES
    ci.MAX_IMAGE_BYTES = 100
    try:
        LOGS.records.clear()
        check("over-size → None + WARNING",
              ci.prepare(5, "c1", data_url(PNG)) is None and LOGS.warnings())
    finally:
        ci.MAX_IMAGE_BYTES = old


async def t_bucket():
    print("bucket")
    saved_env = os.environ.get("ARTIFACT_BUCKET")
    from proxy.notebook import dag_service
    saved_mgr = dag_service._vm_manager
    try:
        ci._bucket_cache = ci._UNRESOLVED
        os.environ["ARTIFACT_BUCKET"] = "env-bkt"
        check("env ARTIFACT_BUCKET wins", await ci.bucket() == "env-bkt")
        os.environ.pop("ARTIFACT_BUCKET")
        calls = []

        class Mgr:
            def get_artifacts_bucket(self):
                calls.append(1)
                return "disc-bkt"
        dag_service._vm_manager = Mgr()
        ci._bucket_cache = ci._UNRESOLVED
        a, b = await ci.bucket(), await ci.bucket()
        check("fallback discovery runs once and is cached", a == b == "disc-bkt" and len(calls) == 1)
        check("bucket_sync uses the env only", ci.bucket_sync() is None)
        dag_service._vm_manager = None
        ci._bucket_cache = ci._UNRESOLVED
        ci._bucket_warned = False
        LOGS.records.clear()
        check("no env + no manager → None, WARNING once",
              await ci.bucket() is None and await ci.bucket() is None and len(LOGS.warnings()) == 1)
    finally:
        if saved_env is None:
            os.environ.pop("ARTIFACT_BUCKET", None)
        else:
            os.environ["ARTIFACT_BUCKET"] = saved_env
        dag_service._vm_manager = saved_mgr


async def t_upload_and_get():
    print("upload / get")
    fake = FakeS3()
    _reset(fake)
    key = f"notebook-outputs/5/c1/{SHA}.png"
    await ci.schedule_upload(key, PNG)
    put = fake.puts[-1]
    check("put params: Bucket/Key/Body, image/png, private cache, SSE, no ACL",
          put["Bucket"] == "bkt" and put["Key"] == key and put["Body"] == PNG
          and put["ContentType"] == "image/png" and put["CacheControl"] == "private, no-store"
          and put["ServerSideEncryption"] == "AES256" and "ACL" not in put)
    await ci.schedule_upload(key, PNG)
    check("a repeated key always PUTs again", len(fake.puts) == 2)
    check("pending cleared after upload; recent put recorded",
          key not in ci._pending and key in ci._recent_put)
    slow = FakeS3(sleep=0.1)
    _reset(slow)
    t1 = ci.schedule_upload(key, PNG)
    t2 = ci.schedule_upload(key, PNG)
    await asyncio.gather(t1, t2)
    check("concurrent identical uploads coalesce into one PUT", t1 is t2 and len(slow.puts) == 1)
    bad = FakeS3(fail_put=True)
    _reset(bad)
    await ci.schedule_upload(key, PNG)
    w = LOGS.warnings()
    check("a failed upload logs a WARNING with the key, never raises",
          len(w) == 1 and key in w[0].getMessage() and key not in ci._recent_put)

    # get
    slow = FakeS3(sleep=0.2)
    _reset(slow)
    ci.schedule_upload(key, PNG)
    got = await ci.get(key)
    check("get awaits a pending upload of the same key", got == PNG)
    _reset(slow)
    old = ci.GET_PENDING_WAIT_S
    ci.GET_PENDING_WAIT_S = 0.05
    try:
        t = ci.schedule_upload(key, PNG)
        try:
            await ci.get(f"notebook-outputs/5/c1/{SHA_B}.png")
        except ci.ImageMissing:
            pass
        try:
            await ci.get(key)   # times out waiting (0.05 s < 0.2 s); must not cancel t
        except ci.ImageMissing:
            pass
        check("get's timed-out wait does not cancel the shared upload", not t.cancelled())
        await t
        check("…which then completes and records the put", key in ci._recent_put and key in slow.objects)
    finally:
        ci.GET_PENDING_WAIT_S = old
    fake = FakeS3()
    _reset(fake)
    try:
        await ci.get(key)
        check("NoSuchKey → ImageMissing", False)
    except ci.ImageMissing:
        check("NoSuchKey → ImageMissing", True)
    fake.objects[key] = b"not a png"
    try:
        await ci.get(key)
        check("non-PNG object → ImageCorrupt", False)
    except ci.ImageCorrupt:
        check("non-PNG object → ImageCorrupt", True)
    fake.objects[key] = PNG
    fake.content_length_override = ci.MAX_IMAGE_BYTES + 1
    try:
        await ci.get(key)
        check("ContentLength over the cap → ImageCorrupt (before read)", False)
    except ci.ImageCorrupt:
        check("ContentLength over the cap → ImageCorrupt (before read)", True)
    ci._bucket_cache = None
    try:
        await ci.get(key)
        check("no bucket → BucketUnavailable", False)
    except ci.BucketUnavailable:
        check("no bucket → BucketUnavailable", True)


async def t_guarded_delete():
    print("guarded orphan delete")
    fake = FakeS3()
    _reset(fake)
    saved = astorage.__dict__.get("notebook_cell_image_keys_all")
    astorage.notebook_cell_image_keys_all = _fake_keys
    old_grace = ci.RECENT_PUT_GRACE_S
    try:
        ka = f"notebook-outputs/5/c1/{SHA}.png"
        kb = f"notebook-outputs/5/c1/{SHA_B}.png"
        # Clear → save → re-run identical: the re-run PUTs (recent), the clear's
        # orphan delete runs after and must keep the object.
        await ci.schedule_upload(ka, PNG)
        _REFERENCED[5] = {}
        await ci.delete_keys(5, {ka})
        check("clear → save → re-run identical: the object still exists", ka in fake.objects)
        # A→B→A
        _reset(fake)
        await ci.schedule_upload(ka, PNG)
        await ci.schedule_upload(kb, PNG_B)
        _REFERENCED[5] = {"c1": ka}
        await ci.delete_keys(5, {ka, kb})   # PUT A, B, A races: B and A both recent
        check("A→B→A: A exists (referenced again)", ka in fake.objects)
        check("A→B→A: B kept while within the recent-put grace", kb in fake.objects)
        # Referenced-again key never deleted, even outside the grace.
        ci.RECENT_PUT_GRACE_S = 0
        _REFERENCED[5] = {"c1": ka}
        await ci.delete_keys(5, {ka, kb})
        check("referenced-again key not deleted; unreferenced old key deleted",
              ka in fake.objects and kb not in fake.objects)
        # Pending key not deleted.
        slow = FakeS3(sleep=0.2)
        _reset(slow)
        ci.RECENT_PUT_GRACE_S = 0
        t = ci.schedule_upload(kb, PNG_B)
        await asyncio.sleep(0.01)
        await ci.delete_keys(5, {kb})
        await t
        check("pending upload's key not deleted", kb in slow.objects and not slow.deletes)
        # Batching ≤ 1000 per call.
        fake = FakeS3()
        _reset(fake)
        ci.RECENT_PUT_GRACE_S = 0
        many = {f"notebook-outputs/5/c{i}/{SHA}.png" for i in range(1500)}
        for k in many:
            fake.objects[k] = PNG
        await ci.delete_keys(5, many)
        check("delete_objects batched (1000 + 500)",
              [len(d) for d in fake.deletes] == [1000, 500] and not fake.objects)
        # Errors are swallowed (WARNING).
        _reset(fake)
        ci.RECENT_PUT_GRACE_S = 0

        def boom(**kw):
            raise RuntimeError("s3 down")
        fake.delete_objects = boom
        fake.objects[ka] = PNG
        await ci.delete_keys(5, {ka})
        check("delete error → WARNING, no raise", len(LOGS.warnings()) == 1)
        # Off-loop: a slow delete never blocks the loop.
        slow = FakeS3(sleep=0.2)
        _reset(slow)
        ci.RECENT_PUT_GRACE_S = 0
        slow.objects[ka] = PNG
        ticks = await _off_loop(lambda: ci.delete_keys(5, {ka}))
        check("off-loop: delete_keys (0.2 s delete) lets the loop tick ≥10×", ticks >= 10, str(ticks))
    finally:
        ci.RECENT_PUT_GRACE_S = old_grace
        if saved is None:
            astorage.__dict__.pop("notebook_cell_image_keys_all", None)
        else:
            astorage.notebook_cell_image_keys_all = saved


async def t_prefix_and_drain():
    print("prefix delete / drain")
    fake = FakeS3(page_size=2)
    _reset(fake)
    for i in range(5):
        fake.objects[f"notebook-outputs/7/c{i}/{SHA}.png"] = PNG
    fake.objects[f"notebook-outputs/70/c1/{SHA}.png"] = PNG
    fake.objects[f"notebook-outputs/8/c1/{SHA}.png"] = PNG
    await ci.delete_notebook_prefix(7)
    check("prefix delete paginates and removes only notebook-outputs/7/",
          fake.lists == 3 and sorted(fake.objects) == [f"notebook-outputs/70/c1/{SHA}.png",
                                                      f"notebook-outputs/8/c1/{SHA}.png"],
          str(sorted(fake.objects)))
    n = ci.delete_notebook_prefix_sync(8, "bkt")
    check("delete_notebook_prefix_sync returns the count", n == 1)
    saved_env = os.environ.pop("ARTIFACT_BUCKET", None)
    try:
        try:
            ci.delete_notebook_prefix_sync(8)
            check("sync prefix delete without ARTIFACT_BUCKET raises", False)
        except ci.BucketUnavailable:
            check("sync prefix delete without ARTIFACT_BUCKET raises", True)
    finally:
        if saved_env is not None:
            os.environ["ARTIFACT_BUCKET"] = saved_env
    # drain: a pending upload under the prefix lands before the list.
    slow = FakeS3(sleep=0.2)
    _reset(slow)
    k = f"notebook-outputs/9/c1/{SHA}.png"
    ci.schedule_upload(k, PNG)
    await asyncio.sleep(0)
    await ci.delete_notebook_prefix(9)
    check("pending upload finishes before the prefix delete (no straggler)", k not in slow.objects
          and slow.puts and slow.deletes == [[k]])
    # hung upload past the timeout: returns + WARNING
    slow = FakeS3(sleep=0.3)
    _reset(slow)
    ci.schedule_upload(k, PNG)
    await asyncio.sleep(0)
    t0 = time.monotonic()
    await ci.drain_pending("notebook-outputs/9/", timeout=0.05)
    check("drain past its timeout returns and logs WARNING",
          time.monotonic() - t0 < 0.25 and len(LOGS.warnings()) == 1)
    await asyncio.sleep(0.35)
    # off-loop: upload + prefix delete
    slow = FakeS3(sleep=0.2)
    _reset(slow)
    ticks = await _off_loop(lambda: ci.schedule_upload(k, PNG))
    check("off-loop: upload (0.2 s put) lets the loop tick ≥10×", ticks >= 10, str(ticks))
    ticks = await _off_loop(lambda: ci.delete_notebook_prefix(9))
    check("off-loop: prefix delete (0.2 s list+delete) lets the loop tick ≥10×", ticks >= 10, str(ticks))


async def main_async():
    await t_bucket()
    await t_upload_and_get()
    await t_guarded_delete()
    await t_prefix_and_drain()


def test_cell_images():
    _checks.clear()
    saved = (ci._s3, ci._bucket_cache, ci._bucket_warned)
    try:
        t_key_helpers()
        t_prepare()
        asyncio.run(main_async())
    finally:
        ci._s3, ci._bucket_cache, ci._bucket_warned = saved
        ci._pending.clear()
        ci._recent_put.clear()
    failed = [n for n, ok in _checks if not ok]
    assert not failed, failed


if __name__ == "__main__":
    try:
        test_cell_images()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print(f"\nALL PASS ({len(_checks)} checks)")
