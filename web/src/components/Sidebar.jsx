import { useState, useEffect, useCallback, useRef, Fragment } from 'react'
import { IconNotebook, IconDatabase, IconCode, IconPackage, IconServer, IconBraces, IconTerminal, IconLogs } from './Icons'
import { PROXY_URL } from '../config'
import { isVmSuspended } from '../constants'
import { fetchWithTimeout } from '../services/fetchWithTimeout'
import { sharedJson } from '../services/singleFlight'
import { useDataSources } from '../hooks/useDataSources'
import { SLOTS, makeBanner, packageBannerText, shouldShowPackageBanner } from '../services/actionBanner'
import { publish as publishBanner, clearOn as clearBannersOn } from '../services/bannerStore'
import { otherArchHint } from '../services/pipInstallMessage'
import './Sidebar.css'

// Panel components
import NotebooksPanel from './panels/NotebooksPanel'
import OutlinePanel from './panels/OutlinePanel'
import DataSourcesPanel from './panels/DataSourcesPanel'
import SamplesPanel from './panels/SamplesPanel'
import VariablesPanel from './panels/VariablesPanel'
import PackagesPanel from './panels/PackagesPanel'
import MicroVMsPanel from './panels/MicroVMsPanel'
import AboutPanel from './panels/AboutPanel'
import SnippetsPanel from './panels/SnippetsPanel'
import EnvPanel from './panels/EnvPanel'

// Activity bar icon components
function IconOutline({ width = 16, height = 16 }) {
  return (
    <svg width={width} height={height} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="15" y2="12" /><line x1="3" y1="18" x2="11" y2="18" />
    </svg>
  )
}

function IconSamples({ width = 16, height = 16 }) {
  return (
    <svg width={width} height={height} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </svg>
  )
}

function IconKey({ width = 16, height = 16 }) {
  return (
    <svg width={width} height={height} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7.5" cy="15.5" r="4.5" /><path d="M10.7 12.3 21 2" /><path d="m17 6 3 3" /><path d="m14 9 3 3" />
    </svg>
  )
}
export default function Sidebar({
  tabs,
  activeTabId,
  attachedIds,
  uploadedFiles,
  onSelectTab,
  onNewNotebook,
  onCloseTab,
  onDeleteNotebook,
  onReopenNotebook,
  onRenameTab,
  onUploadFile,
  onDeleteFile,
  onDeleteS3File,
  onEndpointLifecycle,
  onLoadSample,
  onInsertCode,
  cells = [],
  variables = {},
  activeTab = null,
  onScrollToCell,
  onFixCell,
  onReorderCellIds,
  onRunFromCell,
  onRequestDeleteCells,
  onRunCells,
  onClearOutputs,
  onToggleHeatmap,
  onClearRunBanner,
  lineageCellId = null,
  onLineageCellChange,
  onTerminateAndSave,
  onSuspendInstance,
  onRefreshInstances,
  onUpdateTabTag,
  onSyncPackages,
  onSyncDataSources,
  onSyncCatalogLoaded,
  onSyncSourcesReady,
  onRefreshFiles,
  instances = {},
  sessionCosts = {},
  sessionCostTotal = null,
  showTerminal = false,
  onToggleTerminal,
  showLogs = false,
  onToggleLogs,
}) {
  // Activity bar state — which panel is active (null = collapsed)
  const [activePanel, setActivePanel] = useState(() => {
    try {
      return localStorage.getItem('microvm-sidebar-panel') || 'notebooks'
    } catch { return 'notebooks' }
  })

  // Panel resize
  const [panelWidth, setPanelWidth] = useState(() => {
    try { return parseInt(localStorage.getItem('microvm-sidebar-width')) || 240 } catch { return 240 }
  })
  const isResizing = useRef(false)
  // Drives a class that disables .sidebar-panel's width transition WHILE actively
  // dragging the resize handle. That transition exists for the fullscreen-expand
  // glide (see `expanded` below); without suppressing it here, every mousemove's
  // setPanelWidth also animates over 320ms, so the panel perpetually lags/chases
  // the cursor instead of tracking it 1:1 (the reported "delay while shrinking").
  const [isDraggingResize, setIsDraggingResize] = useState(false)

  // Fullscreen expand — the committed panel glides OUT to cover the notebook (and
  // anything to its right), then glides back to its docked width on collapse. Not
  // persisted (an expanded panel shouldn't survive a reload and hide the notebook).
  const [expanded, setExpanded] = useState(false)
  // Collapse the expand whenever the panel closes or switches, so a hidden expand
  // state can't linger and re-expand the next panel unexpectedly.
  useEffect(() => { if (!activePanel) setExpanded(false) }, [activePanel])

  // Peek mode — hovering a (non-bottom) activity-bar icon slides the corresponding
  // panel in as a TRANSIENT overlay over the committed panel; leaving the icon (and
  // the overlay) slides it back. Only a CLICK commits the change (togglePanel), just
  // like today. peekPanel is display-only and never persisted, and it deliberately
  // does NOT drive `activePanel` — panels key their data fetches off activePanel, so
  // hovering must not trigger network work (option A: peek shows already-loaded state).
  // `peekPanel` = which panel the overlay RENDERS. It stays set through the slide-out
  // so the exit animation has something to animate; it's cleared (unmounted) only
  // after the close transition finishes. `peekOpen` drives the CSS open/closed state
  // (slid-in vs slid-out) so BOTH directions animate via a transform transition.
  const [peekPanel, setPeekPanel] = useState(null)
  const [peekOpen, setPeekOpen] = useState(false)
  const peekOpenTimer = useRef(null)     // hover-intent delay before opening
  const peekCloseTimer = useRef(null)    // grace delay before starting the close
  const peekUnmountTimer = useRef(null)  // waits out the slide-out
  const pendingPeekRef = useRef(null)    // when switching A→B: the panel to open once A finishes sliding out
  const PEEK_OPEN_MS = 180
  const PEEK_CLOSE_MS = 200
  const PEEK_ANIM_MS = 320               // must match the CSS transition duration

  const clearPeekTimers = () => {
    for (const t of [peekOpenTimer, peekCloseTimer, peekUnmountTimer]) {
      if (t.current) { clearTimeout(t.current); t.current = null }
    }
  }
  // Open the overlay for `panelId`: mount it (slid-out), then flip to open on the next
  // frame so the transform transition actually runs (mount + open in one tick = no anim).
  const openPeek = (panelId) => {
    if (peekUnmountTimer.current) { clearTimeout(peekUnmountTimer.current); peekUnmountTimer.current = null }
    pendingPeekRef.current = null
    setPeekPanel(panelId)
    setPeekOpen(false)  // ensure we start from the slid-out position
    requestAnimationFrame(() => requestAnimationFrame(() => setPeekOpen(true)))
  }
  // Slide the current panel out; when the slide-out completes, either open a pending
  // panel (a switch A→B) or unmount (a plain close). One codepath drives both so a
  // switch always animates: A slides out, THEN B slides in.
  const startPeekClose = () => {
    setPeekOpen(false)
    if (peekUnmountTimer.current) clearTimeout(peekUnmountTimer.current)
    peekUnmountTimer.current = setTimeout(() => {
      const next = pendingPeekRef.current
      pendingPeekRef.current = null
      if (next) openPeek(next)     // switch: now slide the new panel in
      else setPeekPanel(null)      // plain close: unmount
    }, PEEK_ANIM_MS)
  }
  // Hover intent. If a DIFFERENT panel is already peeking, switch: remember the new
  // panel as pending and slide the current one out (startPeekClose opens it after).
  // Otherwise open normally after the intent delay.
  const schedulePeek = (panelId) => {
    if (peekCloseTimer.current) { clearTimeout(peekCloseTimer.current); peekCloseTimer.current = null }
    if (peekPanel === panelId) {   // same panel — cancel any in-flight close, ensure open
      pendingPeekRef.current = null
      if (peekUnmountTimer.current) { clearTimeout(peekUnmountTimer.current); peekUnmountTimer.current = null }
      if (!peekOpen) setPeekOpen(true)
      return
    }
    if (peekOpenTimer.current) clearTimeout(peekOpenTimer.current)
    if (peekPanel) {
      // A different panel is currently mounted (opening, open, or mid-close) → SWITCH
      // after the intent delay: slide the current one out, then slide the new one in.
      // Keyed off `peekPanel` (mount) rather than `peekOpen` so a fast hover during the
      // opening frames still animates as a switch instead of an abrupt swap.
      peekOpenTimer.current = setTimeout(() => {
        pendingPeekRef.current = panelId
        startPeekClose()
      }, PEEK_OPEN_MS)
    } else {
      // Nothing mounted → open directly after the intent delay.
      peekOpenTimer.current = setTimeout(() => openPeek(panelId), PEEK_OPEN_MS)
    }
  }
  // Schedule a peek close after a grace delay (cancelled if the mouse re-enters the
  // icon or the overlay before it fires — that's what makes icon→overlay travel work).
  const schedulePeekClose = () => {
    if (peekOpenTimer.current) { clearTimeout(peekOpenTimer.current); peekOpenTimer.current = null }
    if (peekCloseTimer.current) clearTimeout(peekCloseTimer.current)
    peekCloseTimer.current = setTimeout(startPeekClose, PEEK_CLOSE_MS)
  }
  const cancelPeekClose = () => {
    if (peekCloseTimer.current) { clearTimeout(peekCloseTimer.current); peekCloseTimer.current = null }
    if (peekUnmountTimer.current) { clearTimeout(peekUnmountTimer.current); peekUnmountTimer.current = null }
    pendingPeekRef.current = null
    if (peekPanel && !peekOpen) setPeekOpen(true)  // re-entered mid-close → slide back in
  }
  // Commit-clears-peek: a click/keyboard commit drops the overlay immediately (no anim).
  const closePeekNow = () => { clearPeekTimers(); pendingPeekRef.current = null; setPeekOpen(false); setPeekPanel(null) }
  useEffect(() => () => clearPeekTimers(), [])  // cleanup on unmount

  // A panel's "X" close button. Clears any transient peek AND the committed panel so
  // it works from both contexts: in the committed panel it just collapses the sidebar;
  // in a peek overlay it dismisses the overlay (which would otherwise stay stuck, since
  // setActivePanel(null) alone leaves peekPanel set and the overlay visible).
  const handlePanelClose = () => { closePeekNow(); setActivePanel(null) }

  // Package manager state
  const [packages, setPackages] = useState([])
  const [pkgLoading, setPkgLoading] = useState(false)
  const [pkgFetched, setPkgFetched] = useState(false)

  // External data sources — registry-driven discovery + enriched catalog, fetch
  // lifecycle, and the raw response forwarded to the parent (editor autocomplete).
  const { sources, sourceTypes, catalogEntries, dsLoading, catalogLoaded, sourcesReady, fetchDataSources } = useDataSources({
    activePanel,
    activeTab,
    onRefreshFiles,
    onSyncDataSources,
    instances,
  })

  // Report catalog-loaded state up so the notebook execution guard (in
  // useNotebookCells) can block Run/Run All until the data source catalog has
  // finished loading for this session. Stored on the tab as _catalogLoaded.
  //
  // IMPORTANT: depend ONLY on catalogLoaded, NOT on onSyncCatalogLoaded. The
  // parent passes onSyncCatalogLoaded as an inline arrow (new identity every
  // render); including it here made this effect fire on EVERY render, calling
  // updateTab → setTabs → re-render → fire again = infinite loop ("Maximum update
  // depth", tab OOM-crash). A ref keeps the latest callback without re-arming.
  const onSyncCatalogLoadedRef = useRef(onSyncCatalogLoaded)
  onSyncCatalogLoadedRef.current = onSyncCatalogLoaded
  // Push catalogLoaded up to the ACTIVE tab's _catalogLoaded. Keyed on BOTH
  // catalogLoaded AND the active session id: the value alone isn't enough now that
  // we rehydrate from cache on tab switch. useDataSources is a single instance
  // tracking the active tab; switching between two already-loaded notebooks leaves
  // catalogLoaded true→true (no change), so a value-only dep would NOT re-sync the
  // flag onto the newly-active tab — leaving its Run All wrongly disabled. Adding
  // the session id makes this fire on every switch so the active tab always gets
  // the current flag. (Guarded by App's updateTab no-op-on-unchanged, so re-pushing
  // the same value can't loop.)
  const activeSessionId = activeTab?.sessionId
  useEffect(() => {
    if (onSyncCatalogLoadedRef.current) onSyncCatalogLoadedRef.current(catalogLoaded)
  }, [catalogLoaded, activeSessionId])

  // Push the lightweight sourcesReady up to the ACTIVE tab's _sourcesReady — the
  // signal that now gates Run/Run All (SQL routing only needs source type/identity,
  // not column schemas; see useDataSources). Mirrors the catalogLoaded sync exactly:
  // ref-held callback (avoid inline-arrow re-arm loop) + keyed on the session id so
  // a tab switch between two already-loaded notebooks re-syncs the flag onto the
  // newly-active tab.
  const onSyncSourcesReadyRef = useRef(onSyncSourcesReady)
  onSyncSourcesReadyRef.current = onSyncSourcesReady
  useEffect(() => {
    if (onSyncSourcesReadyRef.current) onSyncSourcesReadyRef.current(sourcesReady)
  }, [sourcesReady, activeSessionId])

  // VM badge state. The live-VM count/countdown is derived ENTIRELY from the
  // `instances` prop, which the parent (useInstances) owns and keeps current via
  // its single, gated /instances poll. The Sidebar does NOT run its own
  // /instances poll — a second always-on poller hammered ListMicrovms on an idle
  // app (no notebooks open → parent map empty → old fallback gate fired every
  // 15s), feeding the throttling spiral. One source of truth, one (gated) poll.
  const vmBadgeInstances = instances

  // persistence_mode is a SERVER-WIDE setting (not per-VM), so fetch it ONCE on
  // mount rather than polling. It only tints the checkpoint-countdown badge, and
  // that badge only renders when VMs exist (i.e. when a notebook is open and the
  // parent poll is active) — so a one-shot read is sufficient and never idle-polls.
  const [persistenceMode, setPersistenceMode] = useState('eternal')

  // Persist active panel
  useEffect(() => {
    try {
      if (activePanel) localStorage.setItem('microvm-sidebar-panel', activePanel)
      else localStorage.removeItem('microvm-sidebar-panel')
    } catch {}
    // Broadcast the active panel so other views (e.g. WelcomeScreen) can react —
    // e.g. hide its "Browse Notebooks" button when the Notebooks panel is open.
    // Fires for every change: icon click, keyboard shortcut, or programmatic toggle.
    try { window.dispatchEvent(new CustomEvent('sidebar-panel-changed', { detail: activePanel })) } catch {}
  }, [activePanel])

  // Persist panel width
  useEffect(() => {
    try { localStorage.setItem('microvm-sidebar-width', String(panelWidth)) } catch {}
  }, [panelWidth])

  const handleResizeStart = useCallback((e) => {
    e.preventDefault()
    isResizing.current = true
    setIsDraggingResize(true)
    const startX = e.clientX
    const startWidth = panelWidth

    const handleMouseMove = (e) => {
      if (!isResizing.current) return
      const delta = e.clientX - startX
      const newWidth = Math.min(720, Math.max(180, startWidth + delta))
      setPanelWidth(newWidth)
    }

    const handleMouseUp = () => {
      isResizing.current = false
      setIsDraggingResize(false)
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }

    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }, [panelWidth])

  const togglePanel = (panel) => {
    closePeekNow()  // a click commits — drop any transient peek overlay
    setActivePanel(prev => prev === panel ? null : panel)
  }

  // Run All status for the Outline activity-bar icon: null | 'running' | 'error'.
  // Driven by the 'outline-run-status' window event dispatched from Notebook.jsx.
  // 'running' pulses green while Run All executes; 'error' turns the icon red and
  // PERSISTS after the run so the user knows to open Outline and find the failed cell.
  const [outlineRunStatus, setOutlineRunStatus] = useState(null)
  useEffect(() => {
    const handler = (e) => {
      const status = e.detail  // 'running' | 'error' | 'clear'
      if (status === 'running') {
        setOutlineRunStatus('running')
        closePeekNow()             // Run All force-opens Outline as the committed panel;
        setActivePanel('outline')  // drop any transient peek so it doesn't linger on top
      } else if (status === 'error') {
        setOutlineRunStatus('error')
      } else {
        setOutlineRunStatus(null)  // 'clear' / anything else
      }
    }
    window.addEventListener('outline-run-status', handler)
    return () => window.removeEventListener('outline-run-status', handler)
  }, [])

  // When cells are inserted (AI "Insert N Cells", per-block Insert, or a data-source
  // snippet), force-open the Outline panel so the user watches the new rows appear
  // (they also flash there via 'notebook-cells-inserted' inside OutlinePanel). This
  // fires for EVERY insert path since they all end by dispatching this event once
  // the batch has rendered. Mirrors how Run All force-opens Outline above.
  useEffect(() => {
    const handler = () => {
      closePeekNow()             // drop any transient peek so Outline is the committed panel
      setActivePanel('outline')
    }
    window.addEventListener('notebook-cells-inserted', handler)
    return () => window.removeEventListener('notebook-cells-inserted', handler)
  }, [])

  // Opening a sample from the Samples panel force-opens the Notebooks panel, so the
  // user lands looking at their (now newly-added) notebook in context rather than
  // still staring at the samples list. Same mechanism as the Outline handler above.
  useEffect(() => {
    const handler = () => {
      closePeekNow()             // drop any transient peek so Notebooks is the committed panel
      setActivePanel('notebooks')
    }
    window.addEventListener('open-notebooks-panel', handler)
    return () => window.removeEventListener('open-notebooks-panel', handler)
  }, [])
  // Open a named panel without toggling it closed when it is already open (a
  // banner action such as the restore report's "Open Packages", SlotBanner.jsx).
  useEffect(() => {
    const handler = (e) => {
      if (typeof e?.detail !== 'string' || !e.detail) return
      closePeekNow()
      setActivePanel(e.detail)
    }
    window.addEventListener('open-sidebar-panel', handler)
    return () => window.removeEventListener('open-sidebar-panel', handler)
  }, [])

  // Listen for keyboard shortcut events from App
  useEffect(() => {
    const handler = (e) => togglePanel(e.detail)
    window.addEventListener('toggle-sidebar-panel', handler)
    return () => window.removeEventListener('toggle-sidebar-panel', handler)
  }, [])

  // Delete a variable from the session namespace, then ask the notebook to
  // refresh its variable list (which re-fetches from the VM's /variables).
  const handleDeleteVariable = useCallback(async (name) => {
    if (!activeTab?.microvmEndpoint) return
    const headers = { 'Content-Type': 'application/json' }
    if (activeTab.sessionId) headers['X-Session-Id'] = activeTab.sessionId
    try {
      await fetchWithTimeout(`${activeTab.microvmEndpoint}/execute`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ code: `del ${name}` }),
      })
    } catch (e) {
      console.warn('Delete variable failed:', e)
    }
    window.dispatchEvent(new CustomEvent('refresh-variables'))
  }, [activeTab?.microvmEndpoint, activeTab?.sessionId])

  // Current VM-state map, read inside the stable fetchPackages callback without
  // adding `instances` to its deps (which would recreate it every poll tick
  // and restart the cold-start retry effect).
  const instancesRef = useRef(instances)
  instancesRef.current = instances

  // --- Package fetching ---
  // fresh: called after the list changed (install/uninstall/Refresh), so a fetch
  // already in flight is not reused (sharedJson runs one more after it).
  const fetchPackages = useCallback(async ({ fresh = false } = {}) => {
    if (!activeTab?.microvmEndpoint || activeTab?.status !== 'connected') return false
    // Suspend gate: don't wake a sleeping VM to run `pip list`. The last package
    // list stays shown; a resume or cell run brings the VM back and the retry
    // effect re-fetches.
    if (isVmSuspended(instancesRef.current, activeTab?.microvmId)) return false
    setPkgLoading(true)
    let ok = false
    try {
      const headers = { 'Content-Type': 'application/json' }
      if (activeTab.sessionId) {
        headers['X-Session-Id'] = activeTab.sessionId
      }
      // `pip list` is a slow subprocess and a freshly launched VM may still be
      // cold-starting, so allow a generous timeout.
      // Shared with any identical request already in flight (post-restore burst),
      // except after an install/uninstall/Refresh: then a fetch that started
      // before the change is not reused (one rerun after it instead).
      const resp = await sharedJson(
        `packages|${activeTab.microvmEndpoint}|${activeTab.sessionId || ''}`,
        () => fetchWithTimeout(`${activeTab.microvmEndpoint}/packages`, {
          method: 'GET',
          headers,
          timeout: 45000,
        }),
        undefined,
        { fresh },
      )
      if (resp.ok) {
        const data = resp.data
        if (data && Array.isArray(data.packages)) {
          const pkgList = data.packages.map(p => ({ name: p.name, version: p.version }))
          setPackages(pkgList)
          if (onSyncPackages) onSyncPackages(pkgList)
          // Success = a well-formed response, even if the list is EMPTY. An empty
          // pip list is a complete result, not a cold-start miss — conflating the
          // two (ok = length>0) left the panel spinning forever on a VM with no
          // packages. The retry loop below latches on this and stops.
          ok = true
        }
      }
    } catch {}
    setPkgLoading(false)
    // Latch "fetched" on a successful response (incl. empty) so the panel can show
    // the list or "No packages found" instead of an endless spinner. A network/
    // cold-start miss leaves ok=false, so the retry effect tries again.
    if (ok) setPkgFetched(true)
    return ok
  }, [activeTab?.microvmEndpoint, activeTab?.sessionId, activeTab?.status])

  // Clear package state when the VM disconnects.
  useEffect(() => {
    if (!activeTab?.microvmEndpoint || activeTab?.status !== 'connected') {
      setPackages([])
      setPkgFetched(false)
    }
  }, [activeTab?.microvmEndpoint, activeTab?.status])

  // Load packages once connected, retrying with backoff — a newly launched VM is
  // cold and its first /packages call can be slow or briefly fail. Cancels on
  // success, disconnect, or tab switch.
  useEffect(() => {
    if (pkgFetched || !activeTab?.microvmEndpoint || activeTab?.status !== 'connected') return
    // Don't spin against a suspended VM: fetchPackages is gated on suspend (returns
    // false without waking it), so retrying would loop forever with the spinner
    // stuck. Bail here — the "VM disconnect/state" effect and a resume/cell-run
    // re-trigger this effect when the VM is live again.
    if (isVmSuspended(instances, activeTab?.microvmId)) return
    let cancelled = false
    ;(async () => {
      for (let attempt = 1; attempt <= 8 && !cancelled; attempt++) {
        const ok = await fetchPackages()
        if (ok || cancelled) return
        await new Promise(r => setTimeout(r, Math.min(2000 + attempt * 1000, 8000)))
      }
    })()
    return () => { cancelled = true }
  }, [pkgFetched, activeTab?.microvmEndpoint, activeTab?.status, activeTab?.microvmId, instances, fetchPackages])

  // On tab switch, REHYDRATE package state from the tab's own cache (tab._packages,
  // kept in sync via onSyncPackages) instead of wiping + re-fetching every time.
  // Returning to an already-loaded notebook is then instant (no spinner, no /packages
  // round-trip — which also can't happen while the VM is suspended). A tab that was
  // never loaded has no cache, so it falls through to the normal fetch/retry effect.
  useEffect(() => {
    const cached = activeTab?._packages
    if (Array.isArray(cached) && cached.length > 0) {
      setPackages(cached)
      setPkgFetched(true)   // treat cache as a completed fetch — no reload on return
    } else {
      setPackages([])
      setPkgFetched(false)
    }
    // Only re-run on the identity of the active tab, not on _packages changing
    // (that's updated BY fetchPackages and would loop).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTabId])

  // Packages action banner (slot 'packages'): published here, the one place every
  // panel install goes through, so it still shows if the user left the panel while
  // pip ran. `offeredArches` (from the panel) feeds the other-arch hint.
  const publishInstallBanner = (pkgName, result, elapsedMs, offeredArches) => {
    const ok = !!result.success
    if (!shouldShowPackageBanner({ ok, elapsedMs })) return
    const hint = ok ? '' : otherArchHint({ errorKind: result.errorKind, arch: result.arch, offeredArches })
    const baseName = pkgName.split(/[<>=!~[\s]/)[0].trim()
    publishBanner(SLOTS.PACKAGES, makeBanner({
      ...packageBannerText({
        ok, pkg: ok ? (result.installed_spec || pkgName) : pkgName, elapsedMs,
        error: result.error, hint, hasDetails: !!result.details,
      }),
      slot: SLOTS.PACKAGES,
      // pip output rides on the banner, so "Show details" still works after the
      // Packages panel remounted and lost its own error block.
      payload: { pkg: baseName, ok, details: ok ? '' : (result.details || '') },
    }))
  }

  const handleInstallPackage = async (pkgName, { offeredArches = [] } = {}) => {
    if (!activeTab?.microvmEndpoint) return { success: false, error: 'No VM connected' }
    // A new install replaces the previous install banner (same-op-start).
    clearBannersOn('same-op-start', { slot: SLOTS.PACKAGES })
    const startedAt = Date.now()
    const done = (result) => {
      publishInstallBanner(pkgName, result, Date.now() - startedAt, offeredArches)
      return result
    }
    try {
      const headers = { 'Content-Type': 'application/json' }
      if (activeTab.sessionId) {
        headers['X-Session-Id'] = activeTab.sessionId
      }
      const resp = await fetchWithTimeout(`${activeTab.microvmEndpoint}/install`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ package: pkgName }),
        // pip gives up at 120s in the VM and then returns a classified message
        // ("No prebuilt wheel for X on arm64; …"); wait past that to show it.
        timeout: 180000,
      })
      const result = await resp.json()
      if (result.success) {
        setPkgFetched(false) // trigger re-fetch
        // Fetch now as well: when pkgFetched was already false (a fetch in
        // flight) the line above re-runs nothing, and that fetch may predate
        // the install. The retry effect's own call joins this one.
        fetchPackages({ fresh: true })
        return done({ success: true, installed_spec: result.installed_spec })
      } else {
        return done({
          success: false,
          error: result.error || 'Install failed',
          errorKind: result.error_kind,
          details: result.details,
          arch: result.arch,
        })
      }
    } catch (err) {
      return done({ success: false, error: err.message })
    }
  }

  // --- persistence_mode: one-shot fetch on mount (NOT a poll) ---------------
  // Replaces the old 15s /instances badge poll. We only need the server-wide
  // persistence mode (for the checkpoint-countdown badge tint); the live-VM
  // count comes from the `instances` prop. A single read on mount avoids the
  // idle /instances storm the old fallback poll caused.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const resp = await fetchWithTimeout(`${PROXY_URL}/instances`)
        if (!cancelled && resp.ok) {
          const data = await resp.json()
          if (data.persistence_mode) setPersistenceMode(data.persistence_mode)
        }
      } catch {
        // leave the 'eternal' default; the mode only affects a cosmetic tint.
      }
    })()
    return () => { cancelled = true }
  }, [])

  const formatDuration = (secs) => {
    if (!secs || secs < 60) return `${secs || 0}s`
    if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`
    return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`
  }

  // Activity bar items
  // Grouped for visual separation in the activity bar (a divider is rendered whenever
  // the group changes). Group 1: workspace nav. Group 2: inspect the notebook + its env.
  // Group 3: runtime/interaction. Group 4: reusable content/templates.
  const activityItems = [
    { id: 'notebooks', group: 1, icon: <IconNotebook width={18} height={18} />, title: 'Notebooks (⌥1)', color: 'var(--accent-primary)' },
    { id: 'outline', group: 2, icon: <IconOutline width={18} height={18} />, title: 'Cell Outline (⌥2)', color: 'var(--entity-markdown)' },
    { id: 'data', group: 2, icon: <IconDatabase width={18} height={18} />, title: 'Data Sources (⌥3)', color: 'var(--entity-data)' },
    { id: 'variables', group: 2, icon: <IconBraces width={18} height={18} />, title: 'Variables (⌥4)', color: 'var(--entity-number)' },
    { id: 'packages', group: 2, icon: <IconPackage width={18} height={18} />, title: 'Packages (⌥5)', color: 'var(--entity-bool)' },
    { id: 'env', group: 2, icon: <IconKey width={18} height={18} />, title: 'Secrets & Env', color: 'var(--entity-string)' },
    { id: 'terminal', group: 3, icon: <IconTerminal width={18} height={18} />, title: 'Terminal (⌥6)', color: 'var(--entity-string)' },
    { id: 'logs', group: 3, icon: <IconLogs width={18} height={18} />, title: 'MicroVM Logs (⌥7)', color: 'var(--entity-code)' },
    { id: 'snippets', group: 4, icon: <IconCode width={18} height={18} />, title: 'Snippets (⌥9)', color: 'var(--entity-number)' },
    { id: 'samples', group: 4, icon: <IconSamples width={18} height={18} />, title: 'Sample Notebooks', color: 'var(--ds-samples)' },
  ]

  // Icons that support peek-on-hover: the panel-swapping items (NOT terminal/logs,
  // which are separate bottom-panel toggles). The two bottom rail buttons
  // (microvms, about) are panels too, so they peek as well.
  const PEEKABLE = new Set(['notebooks', 'outline', 'data', 'variables', 'packages', 'env', 'snippets', 'samples', 'microvms', 'about'])

  // Render the panel body for a given panel id. Extracted so the committed panel and
  // the transient peek overlay render the exact same component with identical props.
  const renderPanel = (panel) => {
    switch (panel) {
      case 'notebooks':
        return (
          <NotebooksPanel
            tabs={tabs}
            activeTabId={activeTabId}
            instances={vmBadgeInstances}
            sessionCosts={sessionCosts}
            sessionCostTotal={sessionCostTotal}
            onSelectTab={onSelectTab}
            onNewNotebook={onNewNotebook}
            onCloseTab={onCloseTab}
            onDeleteNotebook={onDeleteNotebook}
            onReopenNotebook={onReopenNotebook}
            onRenameTab={onRenameTab}
            onUpdateTabTag={onUpdateTabTag}
            onClearRunBanner={onClearRunBanner}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'outline':
        return (
          <OutlinePanel
            cells={cells}
            activeTab={activeTab}
            onScrollToCell={onScrollToCell}
            onFixCell={onFixCell}
            onReorderCellIds={onReorderCellIds}
            onRunFromCell={onRunFromCell}
            onRequestDeleteCells={onRequestDeleteCells}
            onRunCells={onRunCells}
            onClearOutputs={onClearOutputs}
            onToggleHeatmap={onToggleHeatmap}
            onClearRunBanner={onClearRunBanner}
            lineageCellId={lineageCellId}
            onLineageCellChange={onLineageCellChange}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'data':
        return (
          <DataSourcesPanel
            uploadedFiles={uploadedFiles}
            onUploadFile={onUploadFile}
            onDeleteFile={onDeleteFile}
            onDeleteS3File={onDeleteS3File}
            onEndpointLifecycle={onEndpointLifecycle}
            onInsertCode={onInsertCode}
            activeTab={activeTab}
            sources={sources}
            sourceTypes={sourceTypes}
            dsLoading={dsLoading}
            catalogEntries={catalogEntries}
            fetchDataSources={fetchDataSources}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'snippets':
        return (
          <SnippetsPanel
            activeTab={activeTab}
            onInsertCode={onInsertCode}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'env':
        return (
          <EnvPanel
            activeTab={activeTab}
            instances={instances}
            onInsertCode={onInsertCode}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'samples':
        return (
          <SamplesPanel
            onLoadSample={onLoadSample}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'variables':
        return (
          <VariablesPanel
            variables={variables}
            activeTab={activeTab}
            cells={cells}
            onScrollToCell={onScrollToCell}
            onInsertCode={onInsertCode}
            onDeleteVariable={handleDeleteVariable}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'packages':
        return (
          <PackagesPanel
            packages={packages}
            pkgLoading={pkgLoading}
            pkgFetched={pkgFetched}
            pkgSuspended={isVmSuspended(instances, activeTab?.microvmId)}
            activeTab={activeTab}
            fetchPackages={() => { setPkgFetched(false); fetchPackages({ fresh: true }) }}
            onInstallPackage={handleInstallPackage}
            onUninstallPackage={async (pkgName) => {
              if (!activeTab?.microvmEndpoint) return { success: false, error: 'No VM connected' }
              try {
                const headers = { 'Content-Type': 'application/json' }
                if (activeTab.sessionId) headers['X-Session-Id'] = activeTab.sessionId
                const resp = await fetchWithTimeout(`${activeTab.microvmEndpoint}/install`, {
                  method: 'POST',
                  headers,
                  body: JSON.stringify({ package: pkgName, uninstall: true }),
                })
                const result = await resp.json()
                if (result.success) {
                  // Uninstalling a package removes its install banner.
                  const lower = String(pkgName).toLowerCase()
                  clearBannersOn('uninstall-package', {
                    slot: SLOTS.PACKAGES,
                    match: b => String(b.payload?.pkg || '').toLowerCase() === lower,
                  })
                }
                return result.success ? { success: true } : { success: false, error: result.error }
              } catch (err) {
                return { success: false, error: err.message }
              }
            }}
            onInsertCode={onInsertCode}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'microvms':
        return (
          <MicroVMsPanel
            tabs={tabs}
            activeTab={activeTab}
            attachedIds={attachedIds}
            instances={instances}
            onRefreshInstances={onRefreshInstances}
            onTerminateAndSave={onTerminateAndSave}
            onSuspendInstance={onSuspendInstance}
            onSelectTab={onSelectTab}
            formatDuration={formatDuration}
            onClose={handlePanelClose}
            expanded={expanded}
            onToggleExpand={() => setExpanded(e => !e)}
          />
        )
      case 'about':
        return <AboutPanel onClose={handlePanelClose} expanded={expanded} onToggleExpand={() => setExpanded(e => !e)} />
      default:
        return null
    }
  }

  return (
    <aside className={`sidebar ${activePanel ? '' : 'sidebar-collapsed'}`}>
      {/* Activity Bar — always visible thin icon strip */}
      <div className="activity-bar">
        {activityItems.map((item, idx) => (
          <Fragment key={item.id}>
            {idx > 0 && activityItems[idx - 1].group !== item.group && (
              <div className="activity-bar-divider" aria-hidden="true" />
            )}
          <button
            className={`activity-bar-item ${
              item.id === 'terminal'
                ? (showTerminal ? 'activity-bar-item-active' : '')
                : item.id === 'logs'
                  ? (showLogs ? 'activity-bar-item-active' : '')
                  : (activePanel === item.id ? 'activity-bar-item-active' : '')
            } ${
              item.id === 'outline' && outlineRunStatus === 'running'
                ? 'activity-bar-item-run-active'
                : item.id === 'outline' && outlineRunStatus === 'error'
                  ? 'activity-bar-item-run-error'
                  : ''
            }`}
            onClick={() => {
              if (item.id === 'terminal') {
                onToggleTerminal?.()
              } else if (item.id === 'logs') {
                onToggleLogs?.()
              } else {
                if (item.id === 'outline' && outlineRunStatus === 'error') {
                  setOutlineRunStatus(null)  // user is opening Outline to inspect — clear the red flag
                }
                togglePanel(item.id)
              }
            }}
            onMouseEnter={PEEKABLE.has(item.id) ? () => schedulePeek(item.id) : undefined}
            onMouseLeave={PEEKABLE.has(item.id) ? schedulePeekClose : undefined}
            title={item.title}
            style={
              (item.id === 'terminal' ? showTerminal : activePanel === item.id)
                ? { color: item.color, borderColor: item.color }
                : {}
            }
          >
            {item.icon}
            {/* Open-notebook count — a neutral, informational badge on the Notebooks
                icon so the number of open notebooks is visible even when the panel
                is collapsed. Hidden at 0 and when the Notebooks panel is the active
                (open) panel, since the panel itself then lists them. */}
            {item.id === 'notebooks' && activePanel !== 'notebooks' && tabs.length > 0 && (
              <span className="activity-bar-badge activity-bar-badge-count" aria-label={`${tabs.length} open notebook${tabs.length === 1 ? '' : 's'}`}>
                {tabs.length > 99 ? '99+' : tabs.length}
              </span>
            )}
          </button>
          </Fragment>
        ))}
        {/* MicroVMs at bottom */}
        <div className="activity-bar-spacer" />
        <button
          className={`activity-bar-item activity-bar-item-bottom ${activePanel === 'microvms' ? 'activity-bar-item-active' : ''} ${(() => {
            if (persistenceMode !== 'checkpoint') return ''
            const now = Date.now()
            for (const inst of Object.values(vmBadgeInstances)) {
              if (!inst.launched_at || !inst.max_duration_sec) continue
              const launchMs = typeof inst.launched_at === 'number' ? inst.launched_at * 1000 : new Date(inst.launched_at).getTime()
              const remaining = Math.max(0, (launchMs + inst.max_duration_sec * 1000 - now) / 1000)
              if (remaining <= 60) return 'activity-bar-item-critical'
              if (remaining <= 300) return 'activity-bar-item-warning'
            }
            return ''
          })()}`}
          onClick={() => togglePanel('microvms')}
          onMouseEnter={() => schedulePeek('microvms')}
          onMouseLeave={schedulePeekClose}
          title="Live MicroVMs"
          style={activePanel === 'microvms' ? { color: 'var(--accent-primary)', borderColor: 'var(--accent-primary)' } : {}}
        >
          <IconServer width={18} height={18} />
          {Object.values(vmBadgeInstances).filter(i => i.state === 'RUNNING').length > 0 && (
            <span className="activity-bar-badge">
              {Object.values(vmBadgeInstances).filter(i => i.state === 'RUNNING').length}
            </span>
          )}
        </button>
        <button
          className={`activity-bar-item activity-bar-item-bottom ${activePanel === 'about' ? 'activity-bar-item-active' : ''}`}
          onClick={() => togglePanel('about')}
          onMouseEnter={() => schedulePeek('about')}
          onMouseLeave={schedulePeekClose}
          title="About"
          style={activePanel === 'about' ? { color: 'var(--accent-primary)', borderColor: 'var(--accent-primary)' } : {}}
        >
          <svg width={18} height={18} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        </button>
      </div>

      {/* Panel Content — the COMMITTED panel (persisted, click-driven), shown as today.
          While a peek overlay is showing a DIFFERENT panel, hide the committed content
          so no sliver of it can show around the overlay (they're the same width, but
          this removes any sub-pixel/border edge case entirely). Layout is preserved so
          the width and resize handle don't jump. */}
      {activePanel && (
        <div
          className={`sidebar-panel${peekPanel && peekPanel !== activePanel ? ' sidebar-panel-under-peek' : ''}${expanded ? ' sidebar-panel-expanded' : ''}${isDraggingResize ? ' sidebar-panel-dragging' : ''}`}
          style={expanded
            ? { width: 'calc(100vw - 44px)' }
            : { width: `${panelWidth}px`, minWidth: `${panelWidth}px` }}
        >
          {renderPanel(activePanel)}
          {/* Resize handle — hidden while expanded (width is fixed to the overlay size). */}
          {!expanded && <div className="sidebar-resize-handle" onMouseDown={handleResizeStart} />}
        </div>
      )}

      {/* Peek overlay — a TRANSIENT panel that slides in over the committed one while
          the user hovers a different icon. Absolutely positioned against the sidebar
          so the committed panel underneath doesn't shift. Never persisted; a click
          commits (togglePanel) and clears it. Staying on the overlay keeps it open
          (cancelPeekClose); leaving it schedules the slide-back. */}
      {/* Width is reduced by the 8px left gutter (left:52 vs the committed panel's
          left:44) so the peek's RIGHT edge aligns with the committed panel's right
          edge — keeps it from overhanging the editor and lines up the resize handle.
          The peek now also starts at left:44 (flush with the rail, no gutter), so it
          uses the same width as the committed panel. */}
      {peekPanel && peekPanel !== activePanel && (
        <div
          className={`sidebar-panel sidebar-panel-peek${peekOpen ? ' sidebar-panel-peek-open' : ''}`}
          style={{ width: `${Math.max(120, panelWidth)}px`, minWidth: `${Math.max(120, panelWidth)}px` }}
          onMouseEnter={cancelPeekClose}
          onMouseLeave={schedulePeekClose}
        >
          {renderPanel(peekPanel)}
        </div>
      )}
    </aside>
  )
}
