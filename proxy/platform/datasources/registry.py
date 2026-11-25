"""
Data source provider registry — the single source of truth for source types.

Every part of the system that needs to know about data source types (the
/datasources endpoints, the Data Sources panel, entity discovery, and the AI
assistant prompt) goes through this registry instead of hardcoding the 4 types.

Adding a new source type is therefore a two-step change:
  1. Implement DataSourceProvider in a new module.
  2. Add the class to _PROVIDER_CLASSES below.
Everything else (enumeration, schema, snippet, panel rendering, entity
discovery enumeration, AI prompt reader/SQL docs) derives from here.
"""

import asyncio
import logging
import threading

from .interface import DataSourceProvider, SourceRef
from .athena import AthenaSchemaProvider
from .dynamodb import DynamoDBSchemaProvider
from .s3 import S3SchemaProvider
from .local import LocalFileSchemaProvider
from .aurora import AuroraSchemaProvider
from .bedrock_kb import BedrockKBProvider
from .sagemaker_endpoint import SageMakerEndpointProvider
from .cloudwatch import CloudWatchLogsProvider

logger = logging.getLogger(__name__)

# Bounded concurrency for parallel schema discovery (see discover_full_catalog).
# Caps how many provider.get_schema() calls run at once so a large account can't
# fire hundreds of Glue/S3/DynamoDB/Data-API calls simultaneously and get
# throttled. 8 is a safe starting point.
MAX_DISCOVERY_CONCURRENCY = 8

# Ordered — controls display order in the panel and the AI prompt.
# Aurora enumerates only when the Data API is configured (its discover() no-ops
# otherwise), so it's safe to always register.
_PROVIDER_CLASSES: list[type[DataSourceProvider]] = [
    LocalFileSchemaProvider,
    S3SchemaProvider,
    DynamoDBSchemaProvider,
    AthenaSchemaProvider,
    AuroraSchemaProvider,
    # Bedrock Knowledge Base (S3 Vectors). discover() no-ops unless BEDROCK_KB_ID
    # is set (sourced from scripts/.kb_env when launched with --WithKnowledgeBase),
    # so it's safe to always register.
    BedrockKBProvider,
    # Live SageMaker real-time endpoints (microvm-ep-*). discover() lists this
    # project's endpoints via boto3; when none exist it simply contributes an empty
    # group, so it's safe to always register.
    SageMakerEndpointProvider,
    # CloudWatch Logs log groups. discover() enumerates groups under the configured
    # prefix(es) (default this platform's own /aws/lambda-microvms/*); supports_sql
    # is False (Insights/filter, not SQL). Safe to always register — an unset prefix
    # list makes discover() no-op.
    CloudWatchLogsProvider,
]

# source_type -> provider class. Built once at import.
_REGISTRY: dict[str, type[DataSourceProvider]] = {}
for _cls in _PROVIDER_CLASSES:
    try:
        _REGISTRY[_cls().source_type] = _cls
    except Exception as e:  # pragma: no cover — a broken provider shouldn't kill the app
        logger.warning(f"Failed to register provider {_cls.__name__}: {e}")
_SOURCE_TYPE: dict[type[DataSourceProvider], str] = {c: t for t, c in _REGISTRY.items()}

# One shared instance per source type, so a provider's own schema cache
# (self._cache, 5-min TTL) is reused across requests, tabs and sessions. Built
# lazily under a lock (double-checked). Providers fetch boto3 clients through
# aws_clients.get_client on every use and hold no client themselves, so sharing
# the instance does not pin credentials. Providers that run code on a VM
# (requires_vm_execution: local files) carry a per-request execute_fn and are
# still built per call.
_instances: dict[str, DataSourceProvider] = {}
_instances_lock = threading.Lock()


def _shared_instance(source_type: str, cls: type[DataSourceProvider]) -> DataSourceProvider:
    inst = _instances.get(source_type)
    if inst is None:
        with _instances_lock:
            inst = _instances.get(source_type)
            if inst is None:
                inst = cls()
                _instances[source_type] = inst
    return inst


def reset_provider_instances() -> None:
    """Drop the shared provider instances (and with them their schema caches).
    For tests that change provider env vars between cases."""
    with _instances_lock:
        _instances.clear()


def provider_classes() -> list[type[DataSourceProvider]]:
    """All registered provider classes, in display order."""
    return list(_PROVIDER_CLASSES)


def get_provider(source_type: str, execute_fn=None) -> DataSourceProvider | None:
    """
    The provider for a source type, or None if unknown. Cloud providers are
    shared instances (see _instances); a provider that requires VM execution
    (local files) is built per call with execute_fn.
    """
    cls = _REGISTRY.get(source_type)
    if cls is None:
        return None
    if getattr(cls, "requires_vm_execution", False):
        return cls(execute_fn=execute_fn)
    return _shared_instance(source_type, cls)


def all_providers(execute_fn=None) -> list[DataSourceProvider]:
    """Every registered provider, in display order (shared instances where possible)."""
    return [get_provider(_SOURCE_TYPE[cls], execute_fn=execute_fn)
            for cls in _PROVIDER_CLASSES if cls in _SOURCE_TYPE]


def provider_metadata() -> list[dict]:
    """
    Lightweight metadata for each source type (for the frontend to render the
    panel generically). Ordered.
    """
    meta = []
    for cls in _PROVIDER_CLASSES:
        source_type = _SOURCE_TYPE.get(cls)
        if source_type is None:
            continue
        meta.append({
            "source_type": source_type,
            "display_name": cls.display_name or source_type,
            "icon": cls.icon,
            "supports_sql": cls.supports_sql,
            "requires_vm_execution": cls.requires_vm_execution,
        })
    return meta


async def discover_all(session_id: str = None, execute_fn=None) -> list[SourceRef]:
    """
    Run discover() on every provider concurrently and aggregate the results.
    Providers that don't enumerate (e.g. local at the proxy level) return [].
    """
    providers = all_providers(execute_fn=execute_fn)
    results = await asyncio.gather(
        *(p.discover(session_id=session_id) for p in providers),
        return_exceptions=True,
    )
    refs: list[SourceRef] = []
    for p, res in zip(providers, results):
        if isinstance(res, Exception):
            logger.warning(f"discover() failed for {p.source_type}: {res}")
            continue
        refs.extend(res)
    return refs


# --- Full-catalog cache: keep-warm + stale-while-revalidate + single-flight ----
# discover_full_catalog() fans out to S3/DynamoDB/Athena/Aurora/... and takes
# several seconds (measured ~7-10s). It runs on EVERY VM launch, launch-with-
# restore, reactive recovery and AI-intel build, so it is cached globally.
#
# WHAT IS CACHED: the SHARED cloud catalog, i.e. discovery with session_id=None.
# The only session-scoped part of discovery is S3's user-data/<session_id>/
# promoted-file overlay (provider.discover_session); it is listed per call and
# merged on top (_session_overlay_entries), so the cache stays un-keyed AND a
# session never receives another session's promoted files.
#
# FRESHNESS (CATALOG_CACHE_TTL_SECONDS, default 300 from scripts/config.sh; the
# fallback below MUST match it; 0 disables the cache):
#   * KEEP-WARM / REFRESH-AHEAD: the UI polls GET /instances every ~15s; that
#     handler calls maybe_refresh_catalog_in_background(), which starts a
#     background refresh once the cache is >= CATALOG_CACHE_REFRESH_AHEAD_FRACTION
#     (default 0.8) of the TTL old. With a UI open the cache never goes cold.
#   * STALE-WHILE-REVALIDATE: a non-force discover_full_catalog() never blocks on
#     a populated cache. Expired entries are returned immediately and a
#     background refresh is kicked. Only a cache that has NEVER been populated
#     (entries is None: before the startup prewarm finished, or every discovery
#     so far failed) blocks — and it joins the in-flight refresh if there is one.
#   * force_refresh=True (the Data Sources panel refresh button) always waits for
#     a discovery that is running NOW: it joins an in-flight refresh if one exists
#     (its result is at most one discovery-duration old — fresher than anything
#     cached, and it keeps single-flight absolute), else starts one.
#
# SINGLE-FLIGHT + CROSS-LOOP DESIGN. At most one discovery runs at a time, as ONE
# asyncio.Task on the proxy's MAIN loop (_refresh_state["task"]); every trigger
# (/instances poll, /launch, startup prewarm, force_refresh) joins it. The old
# note here explained why an asyncio.Lock was avoided: discover_full_catalog_sync
# used to run discovery via asyncio.run() on a FRESH loop in a worker thread, and
# loop-bound primitives can't be shared across loops. The new rules avoid that
# entirely — nothing loop-bound is ever awaited from a different loop:
#   * The task is created and awaited ONLY on the main loop (bind_main_loop() in
#     the proxy lifespan; lazily bound by the first async caller otherwise).
#   * Sync callers in worker threads (discover_full_catalog_sync: intel context)
#     read the cache directly (a
#     plain dict — loop-agnostic) and, when populated, return it (kicking a
#     refresh via loop.call_soon_threadsafe if due). Only when they truly must
#     wait (empty cache / force) do they hand a coroutine to the main loop with
#     asyncio.run_coroutine_threadsafe and block on the concurrent.futures.Future
#     — in THEIR thread, never the main loop's.
#   * Async callers on a foreign loop do the same but await
#     asyncio.wrap_future(...) instead of blocking.
#   * Joiners use asyncio.shield() so a cancelled request (client disconnect)
#     never cancels the shared refresh.
#   * With no live main loop (scripts/batch, or a sync call made ON the main loop
#     thread, which can't wait on its own loop) the legacy standalone path runs:
#     discover on a fresh loop and write the cache. Not single-flight; no current
#     proxy caller takes it.
#
# FAILURES: a refresh that raises — or that returns 0 sources while a non-empty
# catalog is cached (all providers failing, e.g. network down, look exactly like
# that) — is logged at WARNING and does NOT touch the cached entries or their
# timestamp. Background triggers then back off exponentially
# (_FAILURE_BACKOFF_BASE_SECONDS doubling, capped at _FAILURE_BACKOFF_MAX_SECONDS)
# so a persistently failing provider can't cost a full discovery every 15s poll.
# force_refresh accepts an empty result (the user explicitly asked for live data).
#
# SHUTDOWN: shutdown_catalog_refresh() (proxy lifespan) cancels and awaits the
# in-flight task so no "Task was destroyed but it is pending" warning is emitted.
import os as _os
import time as _time
import concurrent.futures as _cf


# --- GET /datasources cache: short TTL + single-flight + explicit invalidation ---
# discover_all() fans out to every provider (S3 list, Glue GetTables, Aurora Data
# API, DynamoDB, CloudWatch, Bedrock KB, ...) and the Data Sources panel can ask
# for it many times in a few seconds (refresh events after a delete/upload). The
# GET /datasources route uses discover_all_cached(), which serves a recent result:
#   * KEY: session_id ("" when absent). S3 discover(session_id) includes that
#     session's promoted-file overlay (user-data/<session_id>/), so every session
#     has its own entry and can never receive another session's overlay.
#   * TTL: DATASOURCES_CACHE_TTL_SECONDS (default 20; 0 disables the cache). At
#     most _DS_CACHE_MAX_KEYS sessions are kept (oldest dropped first).
#   * UNCACHED TYPES: sagemaker_endpoint is discovered live on every call, so an
#     endpoint's status (Creating / Deleting / InService / Stopped) is never stale.
#   * PER PROVIDER: only providers whose discover() succeeded are stored; a failed
#     provider is retried on the next call (as before the cache existed).
#   * SINGLE-FLIGHT: concurrent misses for one key share one discovery task
#     (asyncio.shield, so a disconnecting client never cancels it). The task runs
#     on the caller's loop; a caller on another loop never joins it.
#   * INVALIDATION: invalidate_datasources_cache() bumps a generation counter and
#     drops every entry. Mutation routes call it (S3/SageMaker delete, SageMaker
#     stop/start, promote-attachment). A discovery that STARTED before the bump is
#     neither stored nor joined by later callers, so the next request always sees
#     the mutation.
#   * refresh=True (the panel's manual Refresh button) skips the cached entry; it
#     joins a discovery started in the current generation, else starts one.
# Every provider discover() offloads its blocking boto3 work to a thread, so
# nothing here blocks the event loop.
_UNCACHED_SOURCE_TYPES = frozenset({"sagemaker_endpoint"})
_DS_CACHE_MAX_KEYS = 64
# key -> {"at": monotonic, "gen": int, "by_type": {source_type: [SourceRef]}}
_ds_cache: dict = {}
# (key, gen) -> the single in-flight discovery task for that key
_ds_inflight: dict = {}
_ds_state: dict = {"gen": 0}


def _datasources_cache_ttl() -> float:
    try:
        return float(_os.environ.get("DATASOURCES_CACHE_TTL_SECONDS", "20"))
    except (TypeError, ValueError):
        return 20.0


def invalidate_datasources_cache(reason: str = "") -> None:
    """Drop every cached GET /datasources result. Call after any mutation that
    changes what discovery returns. Plain dict ops: cheap and loop-agnostic."""
    _ds_state["gen"] += 1
    _ds_cache.clear()
    logger.debug(f"[datasources] cache invalidated ({reason or 'unspecified'})")


async def _discover_types(providers: list, session_id) -> dict:
    """discover() on `providers` concurrently -> {source_type: refs} for the ones
    that succeeded (failures are logged and left out, as in discover_all)."""
    results = await asyncio.gather(*(p.discover(session_id=session_id) for p in providers),
                                   return_exceptions=True)
    out: dict = {}
    for p, res in zip(providers, results):
        if isinstance(res, Exception):
            logger.warning(f"discover() failed for {p.source_type}: {res}")
            continue
        out[p.source_type] = res
    return out


async def _cached_types(key: str, session_id, providers: list, refresh: bool) -> dict:
    gen = _ds_state["gen"]
    entry = _ds_cache.get(key)
    if (not refresh and entry is not None and entry["gen"] == gen
            and (_time.monotonic() - entry["at"]) < _datasources_cache_ttl()):
        by_type = dict(entry["by_type"])
        missing = [p for p in providers if p.source_type not in by_type]
        if missing:  # a provider failed last time: retry just that one, live
            by_type.update(await _discover_types(missing, session_id))
        return by_type
    loop = asyncio.get_running_loop()
    task = _ds_inflight.get((key, gen))
    if task is None or task.done() or task.get_loop() is not loop:
        async def _run() -> dict:
            try:
                by_type = await _discover_types(providers, session_id)
                if _ds_state["gen"] == gen:  # no invalidation since this started
                    _ds_cache.pop(key, None)
                    _ds_cache[key] = {"at": _time.monotonic(), "gen": gen, "by_type": by_type}
                    while len(_ds_cache) > _DS_CACHE_MAX_KEYS:
                        _ds_cache.pop(next(iter(_ds_cache)))
                return by_type
            finally:
                if _ds_inflight.get((key, gen)) is asyncio.current_task():
                    _ds_inflight.pop((key, gen), None)
        task = loop.create_task(_run(), name=f"datasources-discover:{key[:8] or '-'}")
        _ds_inflight[(key, gen)] = task
    return dict(await asyncio.shield(task))


async def discover_all_cached(session_id: str = None, refresh: bool = False) -> list[SourceRef]:
    """discover_all(session_id) behind the short TTL cache described above. Same
    result shape and provider order."""
    if _datasources_cache_ttl() <= 0:
        return await discover_all(session_id=session_id)
    providers = all_providers()
    cacheable = [p for p in providers if p.source_type not in _UNCACHED_SOURCE_TYPES]
    live = [p for p in providers if p.source_type in _UNCACHED_SOURCE_TYPES]
    cached_res, live_res = await asyncio.gather(
        _cached_types(session_id or "", session_id, cacheable, refresh),
        _discover_types(live, session_id))
    refs: list[SourceRef] = []
    for p in providers:
        src = live_res if p.source_type in _UNCACHED_SOURCE_TYPES else cached_res
        refs.extend(src.get(p.source_type, []))
    return refs


def _catalog_cache_ttl() -> float:
    try:
        return float(_os.environ.get("CATALOG_CACHE_TTL_SECONDS", "300"))
    except (TypeError, ValueError):
        return 300.0


def _refresh_ahead_fraction() -> float:
    """Fraction of the TTL after which a background refresh is due (0 < f <= 1).
    Default 0.8: with the 300s TTL a refresh starts at 240s, leaving ~60s for a
    ~10s discovery to land before entries would be served stale."""
    try:
        f = float(_os.environ.get("CATALOG_CACHE_REFRESH_AHEAD_FRACTION", "0.8"))
    except (TypeError, ValueError):
        return 0.8
    return f if 0.0 < f <= 1.0 else 0.8


_FAILURE_BACKOFF_BASE_SECONDS = 30.0
_FAILURE_BACKOFF_MAX_SECONDS = 300.0

_catalog_cache: dict = {"entries": None, "at": 0.0}
# loop: the main loop refreshes run on. task: the single in-flight refresh (also
# the strong reference that keeps it from being GC'd). failures/last_failure_at:
# consecutive-failure backoff state.
_refresh_state: dict = {"loop": None, "task": None, "failures": 0, "last_failure_at": 0.0}


def _catalog_log(msg: str) -> None:
    """Refresh-success line: INFO under PROXY_LAUNCH_TIMING (so the user can see
    the keep-warm working), DEBUG otherwise (one line per ~4 min is noise)."""
    if _os.environ.get("PROXY_LAUNCH_TIMING", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.info(msg)
    else:
        logger.debug(msg)


def bind_main_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Pin the loop background refreshes run on (call from the proxy lifespan,
    on the main loop). Resets any task left over from a previous loop."""
    loop = loop or asyncio.get_running_loop()
    if _refresh_state["loop"] is not loop:
        _refresh_state["loop"] = loop
        _refresh_state["task"] = None


def _live_main_loop() -> asyncio.AbstractEventLoop | None:
    """The bound main loop if it can still run callbacks, else None."""
    loop = _refresh_state["loop"]
    if loop is None or loop.is_closed() or not loop.is_running():
        return None
    return loop


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _in_backoff(now: float) -> bool:
    n = _refresh_state["failures"]
    if n <= 0:
        return False
    delay = min(_FAILURE_BACKOFF_BASE_SECONDS * (2 ** (n - 1)), _FAILURE_BACKOFF_MAX_SECONDS)
    return (now - _refresh_state["last_failure_at"]) < delay


def _refresh_due(now: float) -> bool:
    """True when a background refresh should start (cache empty, or at/after the
    refresh-ahead point). False when the cache is disabled (TTL <= 0)."""
    ttl = _catalog_cache_ttl()
    if ttl <= 0:
        return False
    if _catalog_cache["entries"] is None:
        return True
    return (now - _catalog_cache["at"]) >= ttl * _refresh_ahead_fraction()


def catalog_cache_state() -> str:
    """'hit' (fresh), 'stale' (expired but populated → served stale), 'miss'
    (never populated → caller blocks) or 'disabled' (TTL <= 0). For launch
    timing logs; cheap and side-effect free."""
    ttl = _catalog_cache_ttl()
    if ttl <= 0:
        return "disabled"
    if _catalog_cache["entries"] is None:
        return "miss"
    return "stale" if (_time.time() - _catalog_cache["at"]) >= ttl else "hit"


def _record_failure(reason: str, err: BaseException) -> None:
    _refresh_state["failures"] += 1
    _refresh_state["last_failure_at"] = _time.time()
    n = _refresh_state["failures"]
    delay = min(_FAILURE_BACKOFF_BASE_SECONDS * (2 ** (n - 1)), _FAILURE_BACKOFF_MAX_SECONDS)
    kept = _catalog_cache["entries"]
    logger.warning(
        f"Catalog refresh failed ({reason}; {type(err).__name__}: {err}) — keeping "
        f"{'the previous ' + str(len(kept)) + ' cached source(s)' if kept is not None else 'an empty cache'}; "
        f"failure #{n}, background retries backed off {delay:.0f}s"
    )


async def _run_refresh(reason: str, force: bool) -> tuple[bool, object]:
    """The single-flight body (runs as a task on the main loop). Never raises
    except CancelledError; returns (True, entries) or (False, exception) so a
    background task never logs 'exception was never retrieved'."""
    t0 = _time.perf_counter()
    try:
        entries = await _discover_full_catalog_uncached(session_id=None)
    except Exception as e:
        _record_failure(reason, e)
        return (False, e)
    prev = _catalog_cache["entries"]
    if not force and not entries and prev:
        err = RuntimeError(f"discovery returned 0 sources while {len(prev)} are cached "
                           "(treated as a provider/network failure)")
        _record_failure(reason, err)
        return (False, err)
    _catalog_cache["entries"] = entries
    _catalog_cache["at"] = _time.time()
    _refresh_state["failures"] = 0
    _catalog_log(f"[catalog] refreshed ({reason}): {len(entries)} cloud source(s) "
                 f"in {(_time.perf_counter() - t0) * 1000.0:.0f}ms")
    return (True, entries)


def _start_refresh(reason: str, force: bool = False) -> asyncio.Task:
    """Return the in-flight refresh task, or start one. MUST run on the main
    loop's thread (it's the only place the task is created)."""
    task = _refresh_state["task"]
    if task is not None and not task.done():
        return task
    task = asyncio.get_running_loop().create_task(_run_refresh(reason, force),
                                                  name=f"catalog-refresh:{reason}")
    _refresh_state["task"] = task
    return task


async def _await_refresh_on_main(reason: str, force: bool) -> list[dict]:
    """Join (or start) the single-flight refresh and unwrap its result. Runs on
    the main loop. On failure a non-force caller still gets a populated cache
    (it can only be here if the cache was empty, but another refresh may have
    filled it meanwhile); otherwise the error propagates as before."""
    task = _start_refresh(reason, force)
    ok, val = await asyncio.shield(task)
    if ok:
        return val
    if not force and _catalog_cache["entries"] is not None:
        return _catalog_cache["entries"]
    raise val


def _kick_if_due(reason: str) -> None:
    """Start a background refresh if one is due and not backed off. Main loop only."""
    now = _time.time()
    if _refresh_due(now) and not _in_backoff(now):
        _start_refresh(reason)


def maybe_refresh_catalog_in_background(reason: str = "instances-poll") -> bool:
    """Keep-warm trigger for the GET /instances poll. Non-blocking and O(1): it
    only inspects the cache age and, if due, schedules a fire-and-forget refresh
    task on the main loop — the caller never awaits it. Returns True when a NEW
    refresh was started. Never raises."""
    try:
        loop = _running_loop()
        if loop is None:
            return False
        if _live_main_loop() is None:
            bind_main_loop(loop)
        if _refresh_state["loop"] is not loop:
            return False  # only the main loop owns refresh tasks
        task = _refresh_state["task"]
        if task is not None and not task.done():
            return False
        now = _time.time()
        if not _refresh_due(now) or _in_backoff(now):
            return False
        _start_refresh(reason)
        return True
    except Exception as e:  # pragma: no cover — a keep-warm hiccup must never fail /instances
        logger.debug(f"catalog keep-warm trigger skipped: {e}")
        return False


def _swr_reason() -> str:
    ttl = _catalog_cache_ttl()
    return ("stale-while-revalidate"
            if (_time.time() - _catalog_cache["at"]) >= ttl else "refresh-ahead")


def _peek_or_submit(main: asyncio.AbstractEventLoop, force: bool):
    """From any thread that is NOT running `main`: return (entries, None) when the
    cache can answer (kicking a due refresh on the main loop), else (None, cf_future)
    where the future resolves to the main loop's single-flight result."""
    entries = _catalog_cache["entries"]
    if not force and _catalog_cache_ttl() > 0 and entries is not None:
        if _refresh_due(_time.time()):
            main.call_soon_threadsafe(_kick_if_due, _swr_reason())
        return entries, None
    reason = "force" if force else "cold-miss"
    return None, asyncio.run_coroutine_threadsafe(_await_refresh_on_main(reason, force), main)


async def _shared_catalog(force_refresh: bool) -> list[dict]:
    """The shared (session-independent) catalog per the rules above."""
    loop = asyncio.get_running_loop()
    if _live_main_loop() is None:
        bind_main_loop(loop)
    main = _refresh_state["loop"]
    if main is not loop:
        # Foreign loop: never touch the main loop's task from here.
        entries, fut = _peek_or_submit(main, force_refresh)
        return entries if fut is None else await asyncio.wrap_future(fut)
    entries = _catalog_cache["entries"]
    if not force_refresh and _catalog_cache_ttl() > 0 and entries is not None:
        _kick_if_due(_swr_reason())
        return entries
    return await _await_refresh_on_main("force" if force_refresh else "cold-miss", force_refresh)


async def _shared_catalog_standalone(force_refresh: bool) -> list[dict]:
    """Legacy path with no usable main loop: plain TTL cache, discover inline."""
    ttl = _catalog_cache_ttl()
    entries = _catalog_cache["entries"]
    if not force_refresh and ttl > 0 and entries is not None \
            and (_time.time() - _catalog_cache["at"]) < ttl:
        return entries
    entries = await _discover_full_catalog_uncached(session_id=None)
    _catalog_cache["entries"] = entries
    _catalog_cache["at"] = _time.time()
    return entries


async def _session_overlay_entries(session_id: str) -> list[dict]:
    """Catalog entries for the session-scoped sources only (provider.discover_session),
    with schemas. Best-effort: any failure yields [] (the shared catalog still goes out)."""
    try:
        providers = [get_provider(_SOURCE_TYPE[cls]) for cls in _PROVIDER_CLASSES
                     if cls in _SOURCE_TYPE
                     and cls.discover_session is not DataSourceProvider.discover_session]
        results = await asyncio.gather(*(p.discover_session(session_id) for p in providers),
                                       return_exceptions=True)
        pairs: list[tuple[DataSourceProvider, SourceRef]] = []
        for p, res in zip(providers, results):
            if isinstance(res, Exception):
                logger.warning(f"discover_session() failed for {p.source_type}: {res}")
                continue
            pairs.extend((p, ref) for ref in res)
        return await _entries_for_pairs(pairs, session_id)
    except Exception as e:
        logger.warning(f"Session-overlay catalog discovery failed for {session_id}: {e}")
        return []


def _merge_overlay(shared: list[dict], overlay: list[dict]) -> list[dict]:
    if not overlay:
        return shared
    seen = {e.get("source_id") for e in shared}
    return list(shared) + [e for e in overlay if e.get("source_id") not in seen]


async def discover_full_catalog(session_id: str = None,
                                force_refresh: bool = False) -> list[dict]:
    """Cached cloud catalog (see the block comment above for the full rules).

    Non-force: returns the cached shared catalog immediately — even when expired
    (stale-while-revalidate kicks one background refresh) — and blocks only when
    the cache has never been populated, joining the in-flight refresh. force_refresh
    =True waits for a discovery running now (joins in-flight or starts one). When
    session_id is given, that session's own overlay sources are merged on top."""
    shared = await _shared_catalog(force_refresh)
    if session_id:
        shared = _merge_overlay(shared, await _session_overlay_entries(session_id))
    return shared


async def shutdown_catalog_refresh() -> None:
    """Cancel + await the in-flight refresh (proxy lifespan shutdown) and unbind."""
    task = _refresh_state["task"]
    _refresh_state["task"] = None
    _refresh_state["loop"] = None
    if task is None or task.done():
        return
    task.cancel()
    if task.get_loop() is asyncio.get_running_loop():
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _discover_full_catalog_uncached(session_id: str = None) -> list[dict]:
    """
    Discover every CLOUD source AND its schema, in parallel, and return
    fully-formed catalog entries ready to push to the VM.

    This is the proxy-authoritative catalog: the proxy owns cloud enumeration +
    schema derivation (S3/DynamoDB/Athena/Aurora) and pushes the result to the
    VM, which no longer discovers cloud schemas itself (it only self-discovers
    local /tmp files). Local providers contribute nothing here (their proxy-side
    discover() returns []), so the result is cloud-only by construction.

    Two levels of parallelism:
      1. provider.discover() across all types concurrently (asyncio.gather).
      2. provider.get_schema() for EVERY discovered source concurrently, bounded
         by MAX_DISCOVERY_CONCURRENCY. Per the concurrency contract in
         interface.py, EVERY provider's discover()/get_schema() is non-blocking
         (cloud providers offload boto3 via asyncio.to_thread; local is natively
         async), so the gathers genuinely overlap instead of serializing on the
         event loop.

    Per-source failures are isolated: a source whose schema fetch raises is still
    returned as an entry with status="error" (never sinks the whole discovery).

    Returns a list of dicts in the SAME shape the VM DataCatalog.get_all() emits
    (source_type, source_id, display_name, columns[], row_count, size, status,
    error, metadata), so the VM can load them verbatim.
    """
    providers = all_providers()
    # Level 1: enumerate every type concurrently.
    disc_results = await asyncio.gather(
        *(p.discover(session_id=session_id) for p in providers),
        return_exceptions=True,
    )

    # Flatten to (provider, ref) pairs, skipping providers that failed to enumerate.
    pairs: list[tuple[DataSourceProvider, SourceRef]] = []
    for p, res in zip(providers, disc_results):
        if isinstance(res, Exception):
            logger.warning(f"discover() failed for {p.source_type}: {res}")
            continue
        for ref in res:
            pairs.append((p, ref))

    return await _entries_for_pairs(pairs, session_id)


async def _entries_for_pairs(pairs: list[tuple[DataSourceProvider, SourceRef]],
                             session_id: str = None) -> list[dict]:
    """Level 2 of discovery: fetch every (provider, ref) schema concurrently
    (bounded) and build the catalog entries, in input order."""
    # Level 2: fetch all schemas concurrently, bounded by a semaphore.
    sem = asyncio.Semaphore(MAX_DISCOVERY_CONCURRENCY)

    async def _schema_for(provider: DataSourceProvider, ref: SourceRef):
        async with sem:
            # get_schema is a non-blocking coroutine on every provider (concurrency
            # contract, interface.py): cloud providers offload their boto3 work via
            # asyncio.to_thread and local is natively async, so gather genuinely
            # overlaps them. The semaphore just bounds the fan-out.
            try:
                return await provider.get_schema(ref.source_id, session_id=session_id)
            except Exception as e:  # pragma: no cover — isolated per source
                logger.warning(f"get_schema failed for {ref.source_type}:{ref.source_id}: {e}")
                return e

    schemas = await asyncio.gather(*(_schema_for(p, r) for p, r in pairs))

    entries: list[dict] = []
    for (provider, ref), schema in zip(pairs, schemas):
        entries.append(_entry_from(ref, schema))
    return entries


def _entry_from(ref: SourceRef, schema) -> dict:
    """Build one fully-formed catalog entry dict (VM get_all() shape) from a
    discovered SourceRef + its SourceSchema (or an Exception/None on failure)."""
    from app.notebook.dtypes import normalize_dtype

    # Metadata the VM + panel expect (mirrors data_catalog registration).
    extra = ref.extra or {}
    metadata = {}
    # "catalog"/"table_format" carry the S3 Tables federated-catalog dimension
    # (Athena data-catalog name + "iceberg") so the VM query path can target the
    # right catalog and the panel can render S3 Tables distinctly. Without them in
    # this whitelist they'd be dropped before reaching the VM.
    for k in ("database", "schema", "region", "bucket", "key", "catalog", "table_format"):
        if k in extra and extra[k] is not None:
            metadata[k] = extra[k]

    base = {
        "source_type": ref.source_type,
        "source_id": ref.source_id,
        "display_name": ref.display_name,
        "columns": [],
        "row_count": ref.row_count,
        "size": ref.size,
        "status": "pending",
        "error": "",
        "metadata": metadata,
    }

    if isinstance(schema, Exception):
        base["status"] = "error"
        base["error"] = str(schema)
        return base
    if schema is None:
        # No schema resolved (e.g. source vanished) — surface as error so the
        # panel shows something actionable rather than a silent empty entry.
        base["status"] = "error"
        base["error"] = "schema unavailable"
        return base

    base["columns"] = [
        {
            "name": c.name,
            "dtype": normalize_dtype(c.dtype),
            "sample": c.sample,
            "nullable": c.nullable,
        }
        for c in schema.columns
    ]
    if schema.row_count is not None:
        base["row_count"] = schema.row_count
    if schema.size:
        base["size"] = schema.size
    base["status"] = "discovered"
    return base


def _run_coro_blocking(make_coro):
    """Run a coroutine to completion on a FRESH loop from sync code: directly when
    this thread has no running loop, else in a helper thread (asyncio.run can't
    nest)."""
    if _running_loop() is None:
        return asyncio.run(make_coro())
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(make_coro())).result()


def discover_full_catalog_sync(session_id: str = None, force_refresh: bool = False) -> list[dict]:
    """Synchronous wrapper around discover_full_catalog() for non-async callers
    (intel context). Shares the
    cache AND the main loop's single-flight refresh (see the block comment above):
    from a worker thread it returns the cached catalog when populated, else blocks
    THIS thread on the main loop's refresh via run_coroutine_threadsafe. The
    session overlay is listed on a fresh local loop (no main-loop state)."""
    main = _live_main_loop()
    if main is not None and _running_loop() is not main:
        entries, fut = _peek_or_submit(main, force_refresh)
        shared = entries if fut is None else fut.result()
    elif main is not None and not force_refresh and _catalog_cache_ttl() > 0 \
            and _catalog_cache["entries"] is not None:
        # Called ON the main loop thread (can't wait on our own loop): serve the
        # cache and schedule a due refresh — create_task is fine from this thread.
        _kick_if_due(_swr_reason())
        shared = _catalog_cache["entries"]
    else:
        shared = _run_coro_blocking(lambda: _shared_catalog_standalone(force_refresh))
    if session_id:
        shared = _merge_overlay(shared, _run_coro_blocking(lambda: _session_overlay_entries(session_id)))
    return shared


def discover_all_sync(session_id: str = None, execute_fn=None) -> list[SourceRef]:
    """
    Synchronous wrapper around discover_all() for non-async callers (e.g. the
    batch entity-discovery process). Safe whether or not an event loop is running.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to drive one directly.
        return asyncio.run(discover_all(session_id=session_id, execute_fn=execute_fn))
    # Called from within a running loop — run in a separate thread with its own loop.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(
            lambda: asyncio.run(discover_all(session_id=session_id, execute_fn=execute_fn))
        ).result()


def reader_docs() -> list[str]:
    """All built-in reader helper doc lines, aggregated across providers (for the AI prompt)."""
    lines: list[str] = []
    for cls in _PROVIDER_CLASSES:
        lines.extend(cls().reader_docs())
    return lines


def sql_syntax_docs() -> list[str]:
    """All SQL-cell syntax doc lines, aggregated across SQL-capable providers (for the AI prompt)."""
    lines: list[str] = []
    for cls in _PROVIDER_CLASSES:
        inst = cls()
        if inst.supports_sql:
            lines.extend(inst.sql_syntax_docs())
    return lines
