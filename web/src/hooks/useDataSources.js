import { useState, useEffect, useCallback, useRef } from 'react'
import { PROXY_URL } from '../config'
import { apiFetch } from '../services/apiFetch'
import { isVmSuspended, isSessionVmAlive } from '../constants'
import { fetchWithTimeout } from '../services/fetchWithTimeout'
import { createRefreshCoalescer } from '../services/refreshCoalescer'

/**
 * useDataSources — registry-driven discovery of external data sources for the
 * Data Sources panel + editor autocomplete. Owns the generic `sources` +
 * `sourceTypes` list and the enriched `/datasources/catalog` entries (column
 * schemas + entity-doc flags), and the fetch lifecycle:
 *   - lazy-load when the Data panel opens
 *   - (re)fetch on connect once a sessionId lands, retrying until the enriched
 *     catalog resolves (so entity-intel icons never get stuck missing)
 *   - re-fetch on the 'refresh-datasources' event (e.g. after intel generation)
 *
 * The raw /datasources response is forwarded to the parent via onSyncDataSources
 * (for consumers like the editor autocomplete).
 */
export function useDataSources({ activePanel, activeTab, onRefreshFiles, onSyncDataSources, instances = {} }) {
  // Current VM-state map, read inside the stable fetchDataSources callback via a
  // ref so we don't add `instances` (a new object every poll tick) to its
  // deps — that would recreate the callback and re-arm the connect effects.
  const instancesRef = useRef(instances)
  instancesRef.current = instances

  // Keep the parent callbacks in refs so fetchDataSources does NOT depend on their
  // identity. onSyncDataSources is an inline arrow in App.jsx (new every render);
  // depending on it would recreate fetchDataSources on every parent render and
  // re-arm the effects below. Refs let us call the latest callback while keeping
  // fetchDataSources stable (depends only on sessionId).
  const onRefreshFilesRef = useRef(onRefreshFiles)
  onRefreshFilesRef.current = onRefreshFiles
  const onSyncDataSourcesRef = useRef(onSyncDataSources)
  onSyncDataSourcesRef.current = onSyncDataSources
  // Latest activeTab, read inside the sessionId-keyed rehydrate effect without
  // making that effect depend on (and re-run with) every tab-object mutation.
  const activeTabRef = useRef(activeTab)
  activeTabRef.current = activeTab

  const [sources, setSources] = useState([])
  const [sourceTypes, setSourceTypes] = useState([])
  const [catalogEntries, setCatalogEntries] = useState([])  // enriched entries from /datasources/catalog
  const [dsLoading, setDsLoading] = useState(false)
  const [dsFetched, setDsFetched] = useState(false)
  // True once the enriched /datasources/catalog (has_entity_doc + schemas) has
  // been successfully loaded for the current session. Distinct from dsFetched,
  // which only tracks the plain source list. Drives the connect re-fetch so the
  // entity intel icons appear without a manual refresh.
  const [catalogLoaded, setCatalogLoaded] = useState(false)
  // True once the LIGHTWEIGHT source list (/datasources → sources + source_types)
  // has resolved for the current session. This is the type/identity info the SQL
  // engine needs to ROUTE a query (source_type/source_id/metadata) — it does NOT
  // require column schemas or entity docs. It's the Run/Run-All gate signal:
  // available seconds earlier than catalogLoaded (which additionally waits on the
  // VM cold-start round-trip + full schema discovery). catalogLoaded still gates
  // the Data panel's schema popovers + entity-intel sparkles, just not Run.
  const [sourcesReady, setSourcesReady] = useState(false)

  // NOTE: this callback MUST depend on activeTab.sessionId. The enriched catalog
  // (which carries has_entity_doc → the entity intel icons) is only fetched when
  // a sessionId is present. If this closed over a stale activeTab, the catalog
  // fetch would be skipped on the first connect, dsFetched would latch true, and
  // the icons would never appear until a manual refresh.
  const sessionId = activeTab?.sessionId
  // `force` = an EXPLICIT refresh signal (manual refresh button, or a
  // 'refresh-datasources' event fired after a file upload / intel completion /
  // delete). On a force we bypass the suspend gate and fetch the enriched catalog
  // live, because the data genuinely just changed (e.g. a new local file now has an
  // entity doc → sparkle) and showing the stale cached entries would be wrong.
  // Briefly waking a suspended VM is justified here — it's a real data change the
  // user triggered, not idle churn. Non-forced calls keep the cache-first,
  // suspend-skipping behavior.
  // `refreshDiscovery` (default = force) adds ?refresh=true to GET /datasources so
  // the proxy skips its short discovery cache. Only the manual Refresh button
  // needs that: mutation routes invalidate the cache themselves and SageMaker
  // endpoint status is never cached, so event-driven refreshes pass false.
  const fetchDataSources = useCallback(async (force = false, { refreshDiscovery = force } = {}) => {
    setDsLoading(true)
    // Also refresh local VM files
    if (onRefreshFilesRef.current) onRefreshFilesRef.current()
    // Track whether we got everything we need. We only "latch" dsFetched=true once
    // the enriched catalog has actually been retrieved (or there is genuinely no
    // session to enrich against). Otherwise we leave it false so the connect
    // effect retries once the session id lands / the VM stops returning 502.
    let catalogResolved = false
    try {
      // Send X-Session-Id so the S3 provider also returns THIS session's promoted
      // attachments (user-data/<session_id>/...) in the source list — not just the
      // account-wide shared sources.
      const resp = await fetchWithTimeout(`${PROXY_URL}/datasources${refreshDiscovery ? '?refresh=true' : ''}`, {
        headers: sessionId ? { 'X-Session-Id': sessionId } : {},
      })
      if (resp.ok) {
        const data = await resp.json()
        setSources(data.sources || [])
        setSourceTypes(data.source_types || [])
        // The lightweight source list is now present → SQL routing has the
        // type/identity it needs, so Run/Run-All can be enabled. This resolves
        // well before the enriched catalog fetch below (which waits on the VM
        // cold-start + full schema discovery). /datasources is proxy-side
        // enumeration, so it succeeds even while the VM is still warming.
        setSourcesReady(true)

        // Fetch full catalog (with column schemas + entity-doc enrichment) when a
        // session is active. The proxy retries the VM internally on transient 502s.
        // SUSPEND GATE: the enriched catalog fetch resyncs against the VM, which
        // would wake a sleeping VM. Skip it while suspended and keep whatever
        // catalog we already have.
        //
        // CRITICAL: when we skip due to suspend we must NOT setCatalogLoaded(true).
        // catalogLoaded means "the enriched catalog is actually present"; it only
        // resets on a sessionId change, and the connect effect re-fetches while it
        // is false. If we latched it true here with an empty/stale catalog, resuming
        // the SAME session's VM would never re-fetch (catalogLoaded stays true), so
        // the Data Sources panel — and the Run/Run All gate that reads _catalogLoaded
        // — would be stuck on an empty catalog. So we leave catalogLoaded FALSE and
        // only mark the local `catalogResolved` (which clears the panel's loading
        // spinner and latches dsFetched) so the panel isn't stuck loading. The
        // connect effect is additionally guarded on !suspended so it doesn't spin
        // retrying WHILE suspended; it fires once on resume (VM leaves SUSPENDED) and
        // fetches the real catalog then.
        // Skip the live catalog fetch ONLY when suspended AND this isn't a forced
        // (explicit) refresh. A forced refresh (upload/intel-complete/manual) fetches
        // even if suspended, so newly-generated entity docs (sparkles) show up.
        const suspended = isVmSuspended(instancesRef.current, activeTab?.microvmId)
        if (sessionId && (!suspended || force)) {
          try {
            const catalogResp = await apiFetch(
              `/datasources/catalog?refresh=${force ? 'true' : 'false'}`,
              { headers: { 'X-Session-Id': sessionId } },
            )
            if (catalogResp.ok) {
              const catalog = await catalogResp.json()
              data._catalog = catalog  // Attach catalog entries with column info
              setCatalogEntries(catalog.entries || [])
              catalogResolved = true
              setCatalogLoaded(true)
            }
          } catch {}
        } else if (sessionId && suspended) {
          // Suspended + not forced: don't hit the VM and don't latch catalogLoaded
          // (see note above). Clear only the panel-loading spinner; existing
          // catalogEntries stay displayed.
          catalogResolved = true
        }

        if (onSyncDataSourcesRef.current) onSyncDataSourcesRef.current(data)
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        console.warn('[datasources] Fetch timed out')
      }
    }
    setDsLoading(false)
    // Only mark as fetched once the enriched catalog resolved. When there is no
    // session yet, mark fetched so the panel isn't stuck in a loading state — the
    // connect effect will re-run and re-fetch once a sessionId is available.
    if (catalogResolved || !sessionId) {
      setDsFetched(true)
    }
  }, [sessionId])

  // Lazy-load data sources when panel is active
  useEffect(() => {
    if (activePanel === 'data' && !dsFetched) {
      fetchDataSources()
    }
  }, [activePanel, dsFetched, fetchDataSources])

  // Fast, LIGHTWEIGHT source-list fetch — the ONLY thing the Run / Run All gate
  // needs (source type/identity for SQL routing; NOT column schemas). It hits just
  // /datasources (proxy-side enumeration — no VM round-trip, succeeds while the VM
  // is still warming) and flips `sourcesReady`.
  //
  // WHY SEPARATE from the connect effect below: `sourcesReady` used to be set only
  // inside fetchDataSources(), which the connect effect gates on
  // `status === 'connected' && !catalogLoaded`. So the lightweight Run signal
  // inherited the HEAVY catalog path's latency — the VM would show green (driven by
  // the fast instances-map poll) while Run All stayed disabled for a long time,
  // waiting on the status flip + full schema discovery. This effect decouples the
  // two: it triggers on the SAME fast signal as the green icon (isSessionVmAlive
  // reads the already-polled instances map — it never wakes a VM), so Run enables
  // as soon as the VM is actually alive. The connect effect still owns the enriched
  // catalog (schemas + intel sparkles) on its own, slower schedule.
  const sessionVmAlive = isSessionVmAlive(instances, activeTab?.sessionId)
  const fetchSourceList = useCallback(async () => {
    if (!sessionId) return
    try {
      const resp = await fetchWithTimeout(`${PROXY_URL}/datasources`, {
        headers: { 'X-Session-Id': sessionId },
      })
      if (resp.ok) {
        const data = await resp.json()
        setSources(data.sources || [])
        setSourceTypes(data.source_types || [])
        setSourcesReady(true)  // Run / Run All can enable now — routing info is present
        if (onSyncDataSourcesRef.current) onSyncDataSourcesRef.current(data)
      }
    } catch { /* transient — the effect re-fires on the next poll tick */ }
  }, [sessionId])
  useEffect(() => {
    // Fire as soon as the session has a live VM and we don't yet have the list.
    // A suspended VM keeps its cached sourcesReady (set on a prior alive tick or by
    // rehydrate), so we don't need to hit it here.
    if (sessionId && sessionVmAlive && !sourcesReady) {
      fetchSourceList()
    }
  }, [sessionId, sessionVmAlive, sourcesReady, fetchSourceList])

  // Also fetch data sources on connect (so AI chat always has the info).
  // This re-fires when the sessionId lands (not just on the status flip), and
  // keeps trying until the enriched catalog has actually loaded — so a transient
  // VM 502 or a status-before-sessionId race can't leave the icons missing.
  //
  // SUSPEND-AWARE: gate on !suspended AND include the VM's state in the deps. A
  // suspended VM still reports tab.status === 'connected', so status alone won't
  // re-fire this on resume. By keying on the live vmState, when the VM leaves
  // SUSPENDED (→ RUNNING) this effect re-runs and — since catalogLoaded is still
  // false (we never latched it during suspend) — fetches the real catalog. While
  // suspended, the guard skips the call so we don't spin against a sleeping VM.
  const vmState = instances[activeTab?.microvmId]?.state
  const suspendedNow = isVmSuspended(instances, activeTab?.microvmId)
  // Does THIS notebook already have a cached enriched catalog? If so, the rehydrate
  // effect above restored it and we must NOT auto-fetch — otherwise we'd re-hit the
  // VM on every return (the exact behavior we're removing). Checked directly off the
  // tab (not via catalogLoaded state) to avoid a same-commit race: on a sessionId
  // change both effects run in the same pass, and catalogLoaded's setter from
  // rehydrate hasn't applied yet, so relying on it here could still fire a fetch.
  const hasCachedCatalog = Array.isArray(activeTab?._dataSources?._catalog?.entries)
  useEffect(() => {
    if (activeTab?.status === 'connected' && sessionId && !catalogLoaded
        && !suspendedNow && !hasCachedCatalog) {
      fetchDataSources()
    }
  }, [activeTab?.status, sessionId, catalogLoaded, suspendedNow, vmState, hasCachedCatalog, fetchDataSources])

  // On active-session change (tab switch or a newly linked VM): REHYDRATE from the
  // tab's per-notebook cache instead of re-fetching. The catalog is session-scoped,
  // so each notebook has its own; but once we've loaded it, coming back to that
  // notebook should show the cached data INSTANTLY with no VM round-trip. Only when
  // a notebook has no cache yet (true first load) do we clear state so the connect
  // effect below fetches it once. Manual refresh + 'refresh-datasources' events
  // still force a live fetch. This is what stops the "reloads every time I come
  // back" behavior and avoids waking/hitting the VM on every tab switch.
  useEffect(() => {
    const t = activeTabRef.current
    const cached = t?._dataSources
    const cachedEntries = cached?._catalog?.entries
    if (cached && Array.isArray(cachedEntries)) {
      // Restore the exact shape the panel + Run gate consume. No fetch.
      setSources(cached.sources || [])
      setSourceTypes(cached.source_types || [])
      setCatalogEntries(cachedEntries)
      setCatalogLoaded(!!t._catalogLoaded)
      // A cached catalog implies the source list was already resolved for this
      // notebook, so Run stays enabled instantly on return (no re-fetch).
      setSourcesReady(true)
      setDsFetched(true)
    } else {
      // No cache for this notebook yet → clear so the connect effect does the
      // one-time initial fetch. (A newly linked VM also lands here.)
      setSources([])
      setSourceTypes([])
      setCatalogEntries([])
      setCatalogLoaded(false)
      setSourcesReady(false)
      setDsFetched(false)
    }
  }, [sessionId])

  // Re-fetch data sources when intel generation completes (local file entity
  // docs are created during intel generation — this ensures sparkle icons
  // appear for local files without requiring the user to manually refresh)
  //
  // COALESCED (services/refreshCoalescer.js): a burst of events (a delete fires
  // one at once, intel completion another, the panel a nudge) becomes ONE fetch
  // once the burst has been quiet for REFRESH_COALESCE_MS, with at most one fetch
  // in flight and one rerun queued behind it. The manual Refresh button calls
  // fetchDataSources(true) directly, so it is never delayed.
  useEffect(() => {
    const coalescer = createRefreshCoalescer({
      run: (force) => fetchDataSources(force, { refreshDiscovery: false }),
    })
    // Force by default: these events follow a real data change (upload/intel/
    // delete), so bypass the cache-first + suspend gates and fetch the enriched
    // catalog live. A sender passes detail.force === false for a plain re-check
    // (the panel's post-action nudge), which stays suspend-gated.
    const handler = (e) => coalescer.request(e?.detail?.force !== false)
    window.addEventListener('refresh-datasources', handler)
    return () => {
      window.removeEventListener('refresh-datasources', handler)
      coalescer.cancel()
    }
  }, [fetchDataSources])

  // ── Auto-poll while a SageMaker endpoint is TRANSITIONING ────────────────
  // /datasources is otherwise a snapshot (fetched on open / connect / manual
  // refresh / data-change), so a just-deployed endpoint would stay amber
  // ("Creating") until the user hit Refresh. When discovery shows any endpoint in a
  // transitional state (Creating/Updating/SystemUpdating/RollingBack), we poll
  // /datasources every 30s so its status dot flips to green (InService) or red
  // (Failed) on its own — then STOP as soon as none are transitional. Self-limiting:
  // one timer, keyed on whether a transition is in flight, cleared on unmount. Zero
  // polling at rest (the common case), so it can't become a runaway loop.
  // 'Deleting' is included so a just-deleted endpoint (which lingers in list_endpoints
  // as 'Deleting' for a bit) keeps getting re-discovered until it's actually gone —
  // otherwise its row (and the delete spinner on it) would sit until a manual refresh.
  const SM_TRANSITIONAL = ['Creating', 'Updating', 'SystemUpdating', 'RollingBack', 'Deleting']
  const hasTransitioningEndpoint = sources.some(
    s => s.source_type === 'sagemaker_endpoint' && SM_TRANSITIONAL.includes(s.status)
  )
  useEffect(() => {
    if (!hasTransitioningEndpoint) return  // nothing provisioning → no polling
    const id = setInterval(() => {
      // force=true → live re-discovery (bypasses the suspend/cache gate); it re-runs
      // list_endpoints so the dot reflects the current control-plane status
      // (sagemaker_endpoint is never served from the proxy's discovery cache, so
      // ?refresh=true is not needed for it).
      fetchDataSources(true, { refreshDiscovery: false })
    }, 30000)   // 30s: endpoint provisioning takes minutes, so a slower cadence is
                // plenty and avoids the jarring panel re-render every few seconds.
    return () => clearInterval(id)
    // Re-arms when the transitioning flag flips (a new deploy starts) and tears down
    // when it clears (all endpoints settled). fetchDataSources is stable (sessionId-keyed).
  }, [hasTransitioningEndpoint, fetchDataSources])

  return { sources, sourceTypes, catalogEntries, dsLoading, catalogLoaded, sourcesReady, fetchDataSources }
}
