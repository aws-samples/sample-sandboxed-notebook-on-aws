import { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import Notebook from './components/Notebook'
import Sidebar from './components/Sidebar'
import ErrorBoundary from './components/ErrorBoundary'
import AiChatPanel from './components/AiChatPanel'
import TerminalPanel from './components/panels/TerminalPanel'
import LogsPanel from './components/LogsPanel'
import { ConfirmModal, InputModal } from './components/Modal'
import CellDeleteConfirm from './components/CellDeleteConfirm'
import { planCellDelete } from './services/cellDelete'
import { cellBadgeMap } from './utils/cellBadge'
import WelcomeScreen from './components/WelcomeScreen'
import AiFab from './components/AiFab'
import { useWorkbookIntel } from './hooks/useWorkbookIntel'
import { useInstances } from './hooks/useInstances'
import { useTabsPersistence } from './hooks/useTabsPersistence'
import { useNotebookFiles } from './hooks/useNotebookFiles'
import { useSamplesImport } from './hooks/useSamplesImport'
import { newCellId } from './hooks/useNotebookCells'
import { normalizeImportedCell } from './services/notebookImport'
import { useTheme } from './hooks/useTheme'
import { useBottomPanel } from './hooks/useBottomPanel'
import { clearIntelCardState, useIntelCardState, intelCardKey } from './hooks/useIntelCardState'
import { clearChatTurnState } from './hooks/useChatTurnState'
import { IconTerminal, IconLogs } from './components/Icons'
import { PROXY_URL } from './config'
import { apiFetch } from './services/apiFetch'
import { logError } from './services/logger'
import { tintTogglePatch } from './services/execTint'
import { clearBannerPatch } from './services/runBanner'
import { persistTabVmLink } from './services/tabVmLink'
import { restoreBannerText } from './services/restoreChoice'
import { sessionPackages } from './services/restoreProgress'
import {
  SLOTS, makeBanner, isAlreadyViewing, intelBannerText, sandboxBannerText,
  shouldShowSandboxBanner, importBannerText, TERMINAL_FOCUS_EVENT,
} from './services/actionBanner'
import {
  publish as publishBanner, clearOn as clearBannersOn, clear as clearBanner, useSlotBanner,
} from './services/bannerStore'
import ActionBanner from './components/ActionBanner'
import { newerDag, withDagProvenance } from './services/depGraph'
import { terminateTargetForTab } from './services/vmOwnership'
import { showDragOverlay, hideDragOverlay } from './utils/dragOverlay'
import { INTEL_GENERATING_POLL_MS, INTEL_MAX_POLL_ATTEMPTS, DEFAULT_POLL_INTERVAL_MS } from './constants'
import { fetchNotebook, createNotebook as apiCreateNotebook, deleteNotebook as apiDeleteNotebook, loadChatMessages, checkpointBeforeAI, createNotebookVersion, checkpointSession } from './services/notebooks'
import './App.css'

// createTab is now ASYNC (create-returns-id / Option A). It POSTs to /notebooks
// FIRST so the DB assigns the notebook's permanent integer id, then returns a tab
// adopting that id (stored as a String so all existing ===/key/localStorage logic
// keeps working). The client no longer mints ids — this removes the old
// nextTabId++ counter and its PK-collision risk on a shared backend.
//
// Every caller MUST `await createTab(...)` before pushing the tab into state or
// mutating it (e.g. attaching a VM). A stable session_id (client UUID) is minted
// here and persisted with the notebook — one notebook has one session_id for its
// lifetime (the VM under it may rotate).
async function createTab(name, description, tag) {
  const sessionId = crypto.randomUUID()
  const created = await apiCreateNotebook({
    name: name || 'Untitled notebook',
    description: description || '',
    tag: tag || 'Drafts',
    cells: [],
    session_id: sessionId,
  })
  if (!created || created.id == null) {
    // The DB must assign the id; without it there is no notebook to open. Surface
    // the failure to the caller rather than fabricating a client id (which would
    // reintroduce the collision bug this refactor removes).
    throw new Error('Failed to create notebook (no id returned from API)')
  }
  return {
    id: String(created.id),  // canonical: tab.id is ALWAYS a string
    name: created.name || name || `Notebook ${created.id}`,
    description: created.description ?? (description || ''),
    tag: created.tag || tag || 'Drafts',
    sessionId,
    microvmEndpoint: null,
    microvmId: null,
    status: 'disconnected',
    mode: null,
    _dag: created.dag || null,
  }
}

export default function App() {
  const [tabs, setTabs] = useState(() => {
    try {
      const saved = localStorage.getItem('microvm-notebooks')
      if (saved) {
        const parsed = JSON.parse(saved)
        if (Array.isArray(parsed) && parsed.length > 0) {
          // Deduplicate by ID (guard against corrupted localStorage)
          const seen = new Set()
          const deduped = parsed.filter(t => {
            if (seen.has(t.id)) return false
            seen.add(t.id)
            return true
          })
          // Keep microvmId for reconnection, reset transient connection state
          return deduped.map(t => ({
            ...t,
            id: String(t.id),  // canonical: tab.id is ALWAYS a string (migrate old numeric ids)
            status: 'disconnected',
            microvmEndpoint: null,
            // Keep microvmId so we can auto-reconnect
            mode: null,
          }))
        }
      }
    } catch {}
    return []
  })
  const [activeTabId, setActiveTabId] = useState(() => {
    try {
      const saved = localStorage.getItem('microvm-active-tab')
      return saved ? JSON.parse(saved) : null
    } catch {}
    return null
  })
  const [pollIntervalMs, setPollIntervalMs] = useState(DEFAULT_POLL_INTERVAL_MS)

  // Persist tabs (localStorage + API, debounced), load/migrate from API on mount,
  // and persist the active tab id.
  useTabsPersistence({ tabs, setTabs, activeTabId, setActiveTabId })

  // Theme state (persisted, 3-way toggle)
  const { theme, toggleTheme } = useTheme()

  // Modal state
  const [modal, setModal] = useState(null)
  const [showAiChat, setShowAiChat] = useState(true)

  // MicroVM instances + lifecycle (polling, auto-connect/rotation/termination sync,
  // metrics, suspend/terminate+save). Single source of truth for VM state.
  const {
    instances, vmMetrics, sessionCosts, sessionCostTotal, refreshMetrics, markVmRunning, fetchInstances,
    terminateAndSave, suspendInstance,
  } = useInstances({ tabs, setTabs, setModal, pollIntervalMs })
  // Live mirror for the stable removeTabFromWorkspace callback (Delete's VM target).
  const instancesRef = useRef(instances)
  instancesRef.current = instances

  // Sandbox + S3 file operations (list/upload/delete) with Intel (re)generation.
  const { fetchFiles, uploadFile, deleteFile, deleteS3File, lifecycleEndpoint } = useNotebookFiles({ tabs, setTabs, activeTabId, instances })

  // Import finished banner (Notebooks panel). A successful import activates the new
  // tab in the same call, so its banner is parked here and published by the
  // activation effect below, after that activation has cleared older banners for
  // the tab (otherwise 'activate-target-tab' would remove it at once). Failures have
  // no tab to activate and publish right away.
  const pendingImportBannersRef = useRef([])
  const reportImportResult = useCallback(({ tabId = null, ok = true, name = '', error = '' } = {}) => {
    const text = importBannerText({ ok, name, error })
    const banner = makeBanner({ ...text, slot: SLOTS.NOTEBOOKS, payload: { tabId } })
    if (ok && tabId != null) pendingImportBannersRef.current.push(banner)
    else publishBanner(SLOTS.NOTEBOOKS, banner)
  }, [])

  // Welcome-screen sample gallery + git import + sample loading.
  const {
    loadSample, showGitImport, setShowGitImport, gitImportUrl, setGitImportUrl,
    gitImportLoading, importFromGitUrl, showSampleGallery, setShowSampleGallery,
    toggleSampleGallery, samples,
  } = useSamplesImport({ createTab, setTabs, setActiveTabId, setShowAiChat, onImportResult: reportImportResult })
  // Bottom panel (terminal / logs / intel) — open tabs, active tab, height.
  const {
    bottomPanelTabs, setBottomPanelTabs,
    bottomPanelActive, setBottomPanelActive,
    bottomPanelHeight, setBottomPanelHeight,
    toggleBottomTab, closeBottomTab,
  } = useBottomPanel()
  const bottomBanner = useSlotBanner(SLOTS.BOTTOM)
  const [aiAvailable, setAiAvailable] = useState(false)
  // Chat agent's sliding window in messages (GET /ai/config); null = unknown, so the
  // chat treats every turn as in the AI's context.
  const [aiWindowSize, setAiWindowSize] = useState(null)
  const [intelShownForSession, setIntelShownForSession] = useState(null) // tracks which session we auto-showed intel for

  // Workbook Intel is OWNED HERE (lifted out of AiChatPanel) so it stays mounted
  // whether the AI panel is open or closed — the closed-panel FAB reads the same
  // data (attention badge + hover preview). Still a SINGLE poll for the ACTIVE
  // session only (never one-per-open-notebook): the hook keys its 10s loop on this
  // one sessionId, exactly as before — the only change is it now also runs while
  // the panel is closed. Passed down to AiChatPanel (which no longer calls the hook
  // itself) and to AiFab (derived attention counts).
  const activeTabForIntel = tabs.find(t => t.id === activeTabId) || null
  const intelData = useWorkbookIntel(activeTabForIntel?.sessionId)

  // Keep the closed-panel FAB (badge + hover preview) IN SYNC with the in-panel
  // Intel accordion: both must honor the client-side per-card state (hidden cards
  // gone, actioned cards ticked). The accordion reads this state internally via
  // useIntelCardState; the FAB is a pure presentational component, so we filter the
  // arrays HERE (App owns both the notebook id and the intel data) and pass the
  // already-filtered alerts/investigations down. Same hook + key scheme as the
  // accordion, so the two can't diverge.
  const fabNotebookId = activeTabForIntel?.id != null ? String(activeTabForIntel.id) : null
  const fabLiveKeys = useMemo(() => {
    const s = new Set()
    for (const it of intelData.alerts) s.add(intelCardKey('alerts', it))
    for (const it of intelData.investigations) s.add(intelCardKey('investigations', it))
    return s
  }, [intelData.alerts, intelData.investigations])
  const fabCardState = useIntelCardState(fabNotebookId, fabLiveKeys)
  const fabBlob = fabCardState.blob
  // Filter out hidden cards and tag each surviving card with its actioned flag, so
  // the FAB preview mirrors the panel exactly (hidden gone, actioned shown green+✓).
  const fabAlerts = useMemo(
    () => intelData.alerts
      .filter(a => !fabBlob[intelCardKey('alerts', a)]?.deleted)
      .map(a => ({ ...a, _actioned: !!fabBlob[intelCardKey('alerts', a)]?.actioned })),
    [intelData.alerts, fabBlob],
  )
  const fabInvestigations = useMemo(
    () => intelData.investigations
      .filter(iv => !fabBlob[intelCardKey('investigations', iv)]?.deleted)
      .map(iv => ({ ...iv, _actioned: !!fabBlob[intelCardKey('investigations', iv)]?.actioned })),
    [intelData.investigations, fabBlob],
  )

  // Auto-open the AI Assistant when intel becomes available for the active tab
  // (intel now lives inside that panel and auto-expands there).
  //
  // This polls the backend's REAL generation state (see is_generating() in
  // proxy/notebook/ai/workbook_intel.py) instead of guessing a fixed time
  // window. The old version waited a blind 30s then polled every 15s for up
  // to 3 minutes before giving up permanently — since actual generation time
  // varies (agent tool calls + LLM latency), anything slower than 3 minutes
  // meant the tab silently never popped up even though generation succeeded
  // moments later. Now: check immediately, poll fast while status is
  // genuinely "generating", and only stop when the backend reports ready
  // (or error) — no arbitrary cutoff for the case we know is still running.
  const intelPollRef = useRef({ sessionId: null, cancelled: true })
  // Synchronous guard against double-opening a notebook: reopen is async (awaits
  // fetchNotebook before the tab lands in state), so a fast double-click could
  // otherwise create two tabs. Ids are added before the await and removed in a
  // finally. A ref (not state) so the check is immediate — no re-render lag.
  const reopenInFlightRef = useRef(new Set())
  // Live mirror of `tabs` for async callbacks (reopen/close) that must see the
  // CURRENT tab set, not a stale closure. Without this, closing then immediately
  // reopening a notebook read a pre-close `tabs` in reopenNotebook — the closing
  // tab still appeared "open", so reopen early-returned and SKIPPED auto-restore,
  // leaving the terminated tab showing the Restore popup. Updated every render.
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const activeTabIdRef = useRef(activeTabId)
  activeTabIdRef.current = activeTabId

  // Sandbox ready / failed banner, shown in the Notebooks and Live MicroVMs panels.
  // Reported by ConnectionPanel (restore, launch, connect; via the 'sandbox-op'
  // event) and by the auto-restore on reopen. A success under 2 s, or one for the
  // notebook the user is already looking at, gets no banner; a failure always does.
  const reportSandboxResult = useCallback(({ tabId, op = 'launch', ok = true, elapsedMs = 0, error = '' }) => {
    if (tabId == null || !shouldShowSandboxBanner({ ok, elapsedMs })) return
    if (ok && isAlreadyViewing('sandbox-ready', { activeTabId: activeTabIdRef.current, tabId, view: 'notebook' })) return
    const name = tabsRef.current.find(t => String(t.id) === String(tabId))?.name || ''
    const text = sandboxBannerText({ op, ok, elapsedMs, error, name })
    const banner = makeBanner({ ...text, slot: SLOTS.NOTEBOOKS, payload: { tabId } })
    publishBanner(SLOTS.NOTEBOOKS, banner)
    publishBanner(SLOTS.MICROVMS, banner)
  }, [])
  useEffect(() => {
    const onOp = (e) => {
      const d = e?.detail || {}
      if (d.tabId == null) return
      if (d.phase === 'start') {
        clearBannersOn('same-op-start', {
          match: b => (b.kind === 'sandbox-ready' || b.kind === 'sandbox-failed' || b.kind === 'restore-report') && String(b.payload?.tabId) === String(d.tabId),
        })
        return
      }
      reportSandboxResult(d)
    }
    window.addEventListener('sandbox-op', onOp)
    return () => window.removeEventListener('sandbox-op', onOp)
  }, [reportSandboxResult])
  // Restore package report (the /launch `restore_report`), from ConnectionPanel
  // ('restore-report' event) and the auto-restore on reopen. Shown in the
  // Notebooks, Live MicroVMs and Packages panels, always (it replaces "Sandbox
  // ready" in the same slot); kept on the tab for the Packages panel's failed rows.
  const reportRestoreReport = useCallback(({ tabId, report = null, arch = '', memoryCheck = null }) => {
    if (tabId == null) return
    setTabs(prev => prev.map(t => String(t.id) === String(tabId) ? { ...t, _restoreReport: report } : t))
    const text = restoreBannerText(report, { arch, memoryCheck })
    const payload = { tabId, ...(text.action ? { openPanel: 'packages' } : {}) }
    const banner = makeBanner({ ...text, slot: SLOTS.NOTEBOOKS, payload })
    publishBanner(SLOTS.NOTEBOOKS, banner)
    publishBanner(SLOTS.MICROVMS, banner)
    publishBanner(SLOTS.PACKAGES, banner)
  }, [])
  useEffect(() => {
    const onReport = (e) => reportRestoreReport(e?.detail || {})
    window.addEventListener('restore-report', onReport)
    return () => window.removeEventListener('restore-report', onReport)
  }, [reportRestoreReport])
  // Activating a notebook removes the sandbox / import banners that point at it,
  // then publishes any import banner parked by reportImportResult (unless that
  // notebook's Notebook view is already showing, i.e. it is connected).
  useEffect(() => {
    if (activeTabId != null) {
      clearBannersOn('activate-target-tab', { match: b => b.payload?.tabId != null && String(b.payload.tabId) === String(activeTabId) })
    }
    const parked = pendingImportBannersRef.current
    pendingImportBannersRef.current = []
    for (const b of parked) {
      const t = tabsRef.current.find(x => String(x.id) === String(b.payload.tabId))
      const view = t?.status === 'connected' ? 'notebook' : 'connect'
      if (isAlreadyViewing('import', { activeTabId, tabId: b.payload.tabId, view })) continue
      publishBanner(SLOTS.NOTEBOOKS, b)
    }
  }, [activeTabId])
  // A "Delete cell" awaiting confirmation: { tabId, ids, plan, finish, focusFallbackSelector }.
  const [pendingCellDelete, setPendingCellDelete] = useState(null)

  // Sidebar Variables panel: the /variables value map with provenance ("Defined
  // in") overlaid from the stored DAG runtime section (the VM's own fields ignored).
  const activeTabVariables = tabs.find(t => t.id === activeTabId)?._variables
  const activeTabDag = tabs.find(t => t.id === activeTabId)?._dag
  const sidebarVariables = useMemo(
    () => withDagProvenance(activeTabVariables || {}, activeTabDag),
    [activeTabVariables, activeTabDag],
  )
  // "How was this computed?" focus, shared by the Outline (Lineage button) and the
  // output-header trigger. { tabId, cellId } so it only applies to the notebook it
  // was set in; cleared when the active notebook changes. Not persisted.
  const [lineageFocus, setLineageFocus] = useState(null)
  const lineageOpenerRef = useRef(null)
  const lineageFocusCellId = lineageFocus && String(lineageFocus.tabId) === String(activeTabId) ? lineageFocus.cellId : null
  // Records the element that opened the drawer so focus can return to it on close.
  const setLineageCell = useCallback((cellId) => {
    if (cellId == null) { setLineageFocus(null); return }
    const el = document.activeElement
    lineageOpenerRef.current = el && el !== document.body ? el : null
    setLineageFocus({ tabId: String(activeTabId), cellId: String(cellId) })
  }, [activeTabId])
  useEffect(() => { setLineageFocus(null) }, [activeTabId])

  useEffect(() => {
    const activeTab = tabs.find(t => t.id === activeTabId)
    const sessionId = activeTab?.sessionId
    if (!sessionId || sessionId === intelShownForSession) return
    if (activeTab?.status !== 'connected') return

    // Guard against starting a duplicate poll loop for a session that's already
    // being watched (e.g. rapid re-renders before the deps below actually change)
    if (intelPollRef.current.sessionId === sessionId && !intelPollRef.current.cancelled) {
      return
    }

    const pollState = { sessionId, cancelled: false }
    intelPollRef.current = pollState

    const GENERATING_INTERVAL_MS = INTEL_GENERATING_POLL_MS
    const MAX_ATTEMPTS = INTEL_MAX_POLL_ATTEMPTS

    let attempts = 0

    const poll = async () => {
      if (pollState.cancelled || pollState.fetching) return
      attempts++

      pollState.fetching = true
      let data = null
      try {
        const resp = await apiFetch(`/workbook-intel`, { headers: { 'X-Session-Id': sessionId } })
        if (resp.ok) data = await resp.json()
      } catch {
        // network hiccup — fall through and retry at the idle interval
      } finally {
        pollState.fetching = false
      }

      if (pollState.cancelled) return

      if (data?.status === 'ready' && data.intel) {
        // Intel now lives inside the AI Assistant panel (it auto-expands there via
        // useWorkbookIntel). Open the AI panel so the freshly-generated intel is
        // visible; the panel handles the expand/collapse itself.
        setShowAiChat(true)
        setIntelShownForSession(sessionId)
        // Intel generation also creates local file entity docs — refresh the
        // data sources panel so sparkle icons appear for local files too
        window.dispatchEvent(new CustomEvent('refresh-datasources'))
        return  // done
      }

      if (data?.status === 'error') return  // generation failed — nothing to auto-open
      if (attempts >= MAX_ATTEMPTS) return   // safety net exhausted

      // This poller exists ONLY to auto-open the AI panel when a generation that's
      // already in flight completes. If nothing is generating (status
      // 'not_generated' — the common resting case), there is nothing to wait for, so
      // STOP after the first check instead of re-polling /workbook-intel every 10s
      // for the full attempt budget (~33 min of idle heartbeat). A real generation
      // is (re)entered via the useWorkbookIntel 'intel-generation-started' event on
      // its own path; this loop only needs to keep going WHILE status is 'generating'.
      if (data?.status !== 'generating') return  // settled / nothing in flight → done

      setTimeout(poll, GENERATING_INTERVAL_MS)
    }

    poll()  // check right away — no blind upfront wait

    return () => { pollState.cancelled = true }
    // NOTE: sessionId MUST be a dependency. On some connect paths `status` flips to
    // 'connected' before the tab's sessionId is set (e.g. auto-reconnect in
    // useInstances/useNotebookCells updates status without sessionId). Without
    // sessionId here, the effect runs once with sessionId=null, bails at the guard
    // above, and never re-runs when the id lands — so the poll never starts and the
    // AI panel never auto-opens even though generation completed.
  }, [activeTabId, tabs.find?.(t => t?.id === activeTabId)?.status, tabs.find?.(t => t?.id === activeTabId)?.sessionId, intelShownForSession])
  const [newNotebookName, setNewNotebookName] = useState('')
  const [newNotebookDesc, setNewNotebookDesc] = useState('')

  // Global keyboard shortcuts
  useEffect(() => {
    const handler = (e) => {
      // Don't hijack keystrokes while the user is typing in a text field
      // (e.g. the New Notebook name input) — otherwise shortcuts like ⌥-digit
      // or Cmd/Ctrl+key fire mid-typing and can close/steal focus from modals.
      const t = e.target
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) {
        return
      }
      // Ctrl+` or Cmd+` — toggle terminal
      if (e.key === '`' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault()
        toggleBottomTab('terminal')
      }
      // Ctrl+L or Cmd+L — toggle logs panel
      if (e.key === 'l' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault()
        toggleBottomTab('logs')
      }
      // Shortcuts follow the activity-bar's visual top-to-bottom order:
      // ⌥1 notebooks, ⌥2 outline, ⌥3 data, ⌥4 variables, ⌥5 packages (sidebar panels),
      // ⌥6 terminal, ⌥7 logs (bottom tabs), ⌥9 snippets (sidebar panel).
      if (e.altKey && !e.ctrlKey && !e.metaKey) {
        const digitMatch = e.code?.match(/^Digit([1-9])$/)
        if (digitMatch) {
          e.preventDefault()
          const idx = parseInt(digitMatch[1])
          const sidebarPanels = { 1: 'notebooks', 2: 'outline', 3: 'data', 4: 'variables', 5: 'packages', 9: 'snippets' }
          if (sidebarPanels[idx]) {
            window.dispatchEvent(new CustomEvent('toggle-sidebar-panel', { detail: sidebarPanels[idx] }))
          } else if (idx === 6) {
            toggleBottomTab('terminal')
          } else if (idx === 7) {
            toggleBottomTab('logs')
          }
        }
      }
      // Cmd+S / Ctrl+S — save notebook (prevent browser save dialog)
      if (e.key === 's' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        // Trigger save via custom event (Notebook component listens)
        window.dispatchEvent(new CustomEvent('notebook-save'))
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // Fetch poll interval from proxy config
  useEffect(() => {
    apiFetch(`/health`)
      .then(r => r.json())
      .then(data => {
        if (data.poll_interval_ms && data.poll_interval_ms > 0) {
          setPollIntervalMs(data.poll_interval_ms)
        }
      })
      .catch(() => {})

    // Check AI availability
    apiFetch(`/ai/config`)
      .then(r => r.json())
      .then(data => {
        setAiAvailable(data.ai_available === true)
        setAiWindowSize(typeof data.agent_conversation_window_size === 'number' ? data.agent_conversation_window_size : null)
      })
      .catch(() => { setAiAvailable(false); setAiWindowSize(null) })
  }, [])

  // NOTE: Auto-reconnect is handled by fetchInstances polling (runs on mount + every 10s).
  // It auto-connects any tab whose VM is RUNNING or SUSPENDED.
  // No separate mount effect needed — fetchInstances is the single source of truth.

  const addTab = useCallback(() => {
    // Default name = smallest unused "Notebook N" among open notebooks (fills
    // gaps, no collisions). NOT the internal tab-id counter, which is monotonic
    // and would show a misleading number like "Notebook 278".
    const used = new Set(
      tabs
        .map(t => /^Notebook (\d+)$/.exec(t.name || ''))
        .filter(Boolean)
        .map(m => parseInt(m[1], 10))
    )
    let n = 1
    while (used.has(n)) n++
    setNewNotebookName(`Notebook ${n}`)
    setNewNotebookDesc('')
    setModal({ type: 'newNotebook' })
  }, [tabs])

  const confirmNewNotebook = useCallback(async () => {
    // create-returns-id: POST first, adopt the DB id, THEN open the tab. Close the
    // modal only after the create succeeds so a failure keeps the user's input.
    try {
      const tab = await createTab(newNotebookName || undefined, newNotebookDesc)
      setTabs(prev => [...prev, tab])
      setActiveTabId(tab.id)
      setModal(null)
      setShowAiChat(true)
    } catch (err) {
      logError('Failed to create notebook', err)
      setModal({ type: 'error', message: 'Could not create the notebook. Please try again.' })
    }
  }, [newNotebookName, newNotebookDesc])

  // Drop a tab from the workspace. `intent` decides what happens to its VM:
  //   'suspend' (CLOSE, default) — SUSPEND the VM and LEAVE the session→VM
  //       registry intact. The suspended VM keeps the kernel state in memory, so
  //       a reopen just re-attaches (and auto-resumes on first request, ~1s) — no
  //       relaunch, no S3 restore, and no Restore popup. This also removes the
  //       close↔reopen race entirely: the session always has a live VM across the
  //       cycle, so the instance-poll never sees "VM terminated" and never flips
  //       the reopened tab to disconnected, and there's no terminate to collide
  //       with the reopen's launch.
  //   'terminate' (DELETE) — the notebook is being wiped, so TERMINATE the VM
  //       (checkpoint-before-terminate server-side) rather than leaving one
  //       suspended for a notebook that no longer exists.
  const removeTabFromWorkspace = useCallback((tabId, intent = 'suspend') => {
    const closingTab = tabsRef.current.find(t => t.id === tabId)
    if (closingTab && closingTab.microvmId && closingTab.mode === 'microvm' && closingTab.sessionId) {
      const sid = closingTab.sessionId
      if (intent === 'terminate') {
        // Never send another session's VM with this session's header: if the tab's
        // id belongs to another notebook, target this session's own live VM instead
        // (or none). The proxy also refuses a mismatched pair with 409.
        const terminateVmId = terminateTargetForTab(instancesRef.current, closingTab)
        // DELETE path: checkpoint then terminate the SPECIFIC VM (mandatory
        // microvmId so a concurrent relink can't make us kill a newer VM).
        ;(async () => {
          try { await checkpointSession(sid) } catch {}
          if (!terminateVmId) return
          // terminateWithoutSaving: the notebook is being deleted and was just
          // checkpointed above; a failed save must not leave its VM running.
          apiFetch(`/terminate`, {
            method: 'POST',
            headers: { 'X-Session-Id': sid, 'Content-Type': 'application/json' },
            body: JSON.stringify({ microvmId: terminateVmId, terminateWithoutSaving: true }),
          }).catch(() => {})
        })()
      } else {
        // CLOSE path: SUSPEND (no terminate). The VM's in-memory state is
        // preserved by the suspend snapshot, so no explicit S3 checkpoint is
        // needed — reopen re-attaches to this same VM. Fire-and-forget; the tab
        // is dropped from the UI immediately below so Close still feels instant.
        apiFetch(`/suspend`, {
          method: 'POST',
          headers: { 'X-Session-Id': sid },
        }).catch(() => {})
      }
    }
    setTabs(prev => prev.filter(t => t.id !== tabId))
    setActiveTabId(prev => {
      if (prev === tabId) {
        const remaining = tabsRef.current.filter(t => t.id !== tabId)
        return remaining.length > 0 ? remaining[0].id : null
      }
      return prev
    })
    // No `tabs` in deps — reads live state via tabsRef.current so a rapid
    // close→reopen can't operate on a stale tab set. Stable callback identity.
  }, [])

  // CLOSE (non-destructive): the notebook + cells + version history STAY in the
  // DB (autosave keeps them current). We just close the tab and stop the VM, so
  // the user can reopen it later from the Notebooks panel. No confirm needed —
  // nothing is lost.
  const closeTab = useCallback((tabId) => {
    removeTabFromWorkspace(tabId)
  }, [removeTabFromWorkspace])

  // DELETE (destructive): confirm first, then wipe the notebook row + cells +
  // version history from the DB and drop the tab. This is the old behavior, now
  // an explicit choice rather than what "close" does.
  const requestDeleteNotebook = useCallback((tabId, tabName) => {
    setModal({ type: 'deleteNotebook', tabId, tabName })
  }, [])

  const confirmDeleteNotebook = useCallback((tabId) => {
    // Drop the open tab (no-op if it was already CLOSED — deleting a closed
    // notebook doesn't change tabs.length, which is why the panel's DB-list
    // never refreshed and the stale "Closed" row lingered). TERMINATE its VM —
    // the notebook is being wiped, so we don't leave a suspended VM around.
    removeTabFromWorkspace(tabId, 'terminate')
    // Fire a 'notebook-deleted' event AFTER the server delete resolves so the
    // Notebooks panel re-fetches its DB list (loadDb) and the deleted row
    // disappears. Deleting a closed notebook produces no tabs.length change and
    // no version-saved event, so without this the panel had no refresh trigger.
    // Emitted up-front too (optimistic) so the row vanishes instantly, then
    // again on completion to reconcile with the server's truth.
    const notify = () => {
      try {
        window.dispatchEvent(new CustomEvent('notebook-deleted', { detail: { notebookId: String(tabId) } }))
      } catch {}
    }
    notify()  // optimistic: remove the row immediately
    // Drop this notebook's client-side Workbook Intel card-state (actioned/hidden
    // flags in localStorage). Keyed by notebook id, so this one call clears it all.
    clearIntelCardState(String(tabId))
    // Same for the AI chat's per-turn state (collapse / inserted cells).
    clearChatTurnState(String(tabId))
    apiDeleteNotebook(String(tabId))
      .catch(() => {})   // wipes cells + version history server-side
      .finally(notify)   // reconcile once the DELETE actually completed
    setModal(null)
  }, [removeTabFromWorkspace])

  // REOPEN a closed notebook from the DB into a tab. Keeps the DB id as the tab
  // id so autosave writes back to the same row and its version history matches.
  // If it's already open, just activate that tab.
  const reopenNotebook = useCallback(async (notebookId) => {
    const nbId = String(notebookId)  // canonical: tab.id is ALWAYS a string
    // DUPLICATE-OPEN GUARD. reopen is async (awaits fetchNotebook before it can
    // append the tab), so a fast double-click used to pass the `tabs.find` check
    // twice — the tab wasn't in state yet on the 2nd click — and open TWO copies.
    // Two layers now prevent that: (1) a synchronous in-flight ref, set BEFORE the
    // await, rejects a concurrent reopen of the same id; (2) the final append is an
    // atomic check-and-add inside setTabs (which sees live state via `prev`), so
    // even a race that slips past (1) can't add a second tab. If it's already open,
    // just activate it.
    //
    // Read tabsRef.current (LIVE), not the `tabs` closure: on close→immediate-reopen
    // the closure still held the just-closed tab, so this guard wrongly early-returned
    // and skipped auto-restore. The ref reflects the post-close set.
    if (tabsRef.current.find(t => String(t.id) === nbId)) { setActiveTabId(nbId); return }
    if (reopenInFlightRef.current.has(nbId)) { setActiveTabId(nbId); return }
    reopenInFlightRef.current.add(nbId)
    try {
    const nb = await fetchNotebook(notebookId)
    if (!nb) return
    const tab = {
      id: nbId,
      name: nb.name || `Notebook ${nbId}`,
      description: nb.description || '',
      tag: nb.tag || 'Drafts',
      microvmEndpoint: null,
      microvmId: nb.microvm_id || null,
      status: 'disconnected',
      mode: null,
      sessionId: nb.session_id || null,
      checkpointEnabled: nb.checkpoint_enabled || false,
      // A reopened notebook that has a checkpoint-enabled session in the DB
      // PROVABLY has a saved S3 checkpoint — so surface Restore immediately rather
      // than waiting up to a full poll interval (~10s) for the instance-poll loop
      // to infer termination and flip this flag.
      sessionSaved: !!(nb.session_id && nb.checkpoint_enabled),
      _cells: (nb.cells || []).map(c => ({
        id: c.id || newCellId(),
        type: c.type || 'code',
        code: c.code || '',
        output: c.output || null,
        error: c.error || null,
        html: c.html || null,
        image: null,
        // Keep the persisted chart location: the chart reloads from S3 by key.
        imageS3Key: c.imageS3Key || null,
        outputVariable: c.outputVariable ?? null,
        executionNumber: c.executionNumber ?? null,
        executionTime: c.executionTime ?? null,
        lastExecutedCode: c.lastExecutedCode ?? null,
        aiExplanation: c.aiExplanation || null,
        aiExplanationEdited: c.aiExplanationEdited || false,
        aiExplanationSource: c.aiExplanationSource || null,
        generated: c.generated || null,
        peakMemMb: typeof c.peakMemMb === 'number' ? c.peakMemMb : null,
        peakCpuPct: typeof c.peakCpuPct === 'number' ? c.peakCpuPct : null,
        executedAt: c.executedAt || null,
      })),
      // The stored dependency DAG (GET /notebooks/{id} carries it inline).
      _dag: nb.dag || null,
    }
    // AUTO-RESTORE ON EXPLICIT REOPEN. Reopening a checkpoint-enabled notebook is
    // itself the user's intent to resume, so we don't make them click "♻ Restore
    // Session" again — we launch a VM and restore from S3 immediately. (This is
    // DISTINCT from an unexpected mid-session VM death, which still surfaces the
    // ConnectionPanel popup via the instance-poll path — that flow is untouched.)
    // The server enforces same-arch/same-tier restore from the saved session, so we
    // only need to send session_id + restoreFromSession here. Fail-safe: on any
    // error we drop back to 'disconnected', which surfaces the manual Restore button.
    const canAutoRestore = !!(tab.sessionSaved && tab.sessionId && tab.checkpointEnabled)
    if (canAutoRestore) {
      tab.status = 'launching'
      // The user packages this restore reinstalls (checkpoint requirements.txt),
      // shown as "Reinstalling N packages (...)..." until it ends.
      tab._restorePackages = sessionPackages(nb)
    }
    // Atomic check-and-add: if another path already added this tab (race), keep the
    // existing one and DON'T append a duplicate. `added` gates the auto-restore
    // below so we never fire a second launch for an already-open notebook.
    let added = false
    setTabs(prev => {
      if (prev.some(t => String(t.id) === nbId)) return prev
      added = true
      return [...prev, tab]
    })
    setActiveTabId(tab.id)

    // Rehydrate the AI chat transcript for this reopened notebook. The mount-time
    // loader in useTabsPersistence only runs for notebooks present at PAGE LOAD, so
    // a runtime close→reopen would otherwise show an empty AI panel even though the
    // conversation is persisted server-side. Fetch it here (non-blocking) and merge
    // it onto the tab. Guarded on `added` so a race that didn't append won't stomp
    // the existing tab's messages.
    if (added && tab.sessionId) {
      loadChatMessages(tab.sessionId).then((msgs) => {
        if (msgs && msgs.length > 0) {
          setTabs(prev => prev.map(t => t.id === tab.id ? { ...t, _chatMessages: msgs } : t))
        }
      }).catch(() => { /* transcript load is best-effort; empty panel is acceptable */ })
    }

    if (canAutoRestore && added) {
      const restoreStartedAt = Date.now()
      ;(async () => {
        try {
          // INVARIANT: one notebook → one session → AT MOST ONE live VM. Before
          // launching a restore VM, check whether this session ALREADY has a live
          // VM (the proxy is authoritative — fetch /instances fresh, don't trust
          // possibly-stale React state). If so, ATTACH to it instead of launching a
          // second VM. Launching unconditionally is what caused two VMs to fight
          // over one session id — the registry re-points to the newer VM and the
          // tab, still holding the older VM id, gets "Session not found" on run.
          let liveVm = null
          try {
            const iResp = await apiFetch(`/instances`)
            if (iResp.ok) {
              const iData = await iResp.json()
              const entry = Object.entries(iData.instances || {}).find(
                ([, info]) => info.session_id === tab.sessionId &&
                  (info.state === 'RUNNING' || info.state === 'SUSPENDED')
              )
              if (entry) liveVm = { id: entry[0], info: entry[1] }
            }
          } catch { /* fall through to launch */ }

          let result
          if (liveVm) {
            // Attach to the existing VM — no new launch. Adopt its tier/arch.
            result = {
              microvmId: liveVm.id,
              sessionId: tab.sessionId,
              memory_mib: liveVm.info.memory_mib,
              architecture: liveVm.info.architecture,
            }
          } else {
            const resp = await apiFetch(`/launch`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                name: tab.name,
                notebookId: tab.id,
                checkpointEnabled: true,
                sessionId: tab.sessionId,
                restoreFromSession: tab.sessionId,
                // NOTE: arch + memoryMiB (and restoreOverride) intentionally
                // omitted — without restoreOverride the /launch route pins the
                // saved session's arch and tier. Only the Connect dialog's explicit
                // choice moves a restore to another instance type.
              }),
            })
            if (!resp.ok) throw new Error(`launch ${resp.status}`)
            result = await resp.json()
            persistTabVmLink(tab.id, result)  // saved now, not after the 1.5 s autosave
          }
          setTabs(prev => prev.map(t => t.id === tab.id ? {
            ...t,
            microvmId: result.microvmId,
            microvmEndpoint: `${PROXY_URL}/proxy`,
            microvmMemory: result.memory_mib || t.microvmMemory,
            microvmArch: result.architecture || t.microvmArch,
            sessionId: result.sessionId || t.sessionId,
            checkpointEnabled: true,
            sessionSaved: false,
            status: 'connected',
            mode: 'microvm',
            _restorePackages: null,
          } : t))
          // Force a data-source (re)fetch so the reopened tab gets _sourcesReady.
          // On reopen the notebook keeps its SAME sessionId, so useDataSources'
          // session-keyed sync effect sees no dep change and never re-pushes
          // sourcesReady onto the newly-created tab → Run/Run All would stay
          // wrongly disabled even though the VM is running. This event runs
          // fetchDataSources(true), whose setSourcesReady(true) re-fires the sync.
          // Deferred a tick so the active-tab state has committed before the
          // useDataSources handler (which reads the active tab) runs.
          setTimeout(() => window.dispatchEvent(new CustomEvent('refresh-datasources')), 0)
          reportSandboxResult({ tabId: tab.id, op: 'restore', ok: true, elapsedMs: Date.now() - restoreStartedAt })
          // A new VM was restored (not an attach to a live one): show its package report.
          if (!liveVm) reportRestoreReport({ tabId: tab.id, report: result.restore_report || null, arch: result.architecture || '', memoryCheck: result.restore_memory_check || null })
        } catch (err) {
          // Fall back to the manual path: 'disconnected' + sessionSaved keeps the
          // ConnectionPanel's "♻ Restore Session" button available so the user can
          // retry. Never leave the tab stuck in 'launching'.
          logError('Auto-restore on reopen failed', err)
          setTabs(prev => prev.map(t => t.id === tab.id ? {
            ...t, status: 'disconnected', sessionSaved: true, _restorePackages: null,
          } : t))
          reportSandboxResult({ tabId: tab.id, op: 'restore', ok: false, error: err?.message || '' })
        }
      })()
    }
    } finally {
      // Clear the synchronous in-flight guard. The fetchNotebook await is done and
      // the tab (if any) is in state, so a later reopen of this id will correctly
      // re-activate the existing tab rather than being rejected as a duplicate.
      reopenInFlightRef.current.delete(nbId)
    }
    // No `tabs` in deps: reopen reads LIVE state via tabsRef.current + the atomic
    // setTabs(prev=>...) check-and-add, so the callback identity stays stable and
    // can never close over a stale `tabs` (the close→reopen race). Everything else
    // it uses (refs, setters, fetchNotebook, PROXY_URL, reportSandboxResult,
    // reportRestoreReport) is stable.
  }, [reportSandboxResult, reportRestoreReport])

  const updateTab = useCallback((tabId, updates) => {
    setTabs(prev => {
      let changed = false
      const next = prev.map(t => {
        if (t.id !== tabId) return t
        // Skip the write when every updated field already equals the current value.
        // This breaks feedback loops where a child effect re-syncs an unchanged
        // value up (e.g. onSyncCatalogLoaded/onSyncDataSources via an inline arrow):
        // without this, each identical update still produced a new tabs array →
        // re-render → re-sync → "Maximum update depth" tab crash.
        const differs = Object.keys(updates).some(k => t[k] !== updates[k])
        if (!differs) return t
        changed = true
        return { ...t, ...updates }
      })
      return changed ? next : prev  // same reference when nothing changed → no re-render
    })
  }, [])

  // AI panel banner when Workbook Intel finishes (_aiBanner on the tab). Written only
  // after this session was SEEN generating or updating here, so a report that was
  // already ready on load never gets one. A full generation gives "report is ready";
  // a delta (isUpdating, after a file upload or removal) names the files, taken from
  // the intel-generation-started event. A new generation starting clears it.
  const intelTriggerRef = useRef(new Map())   // sessionId -> { trigger, fileNames }
  const intelWatchRef = useRef({ sid: null, inFlight: false, update: false })
  useEffect(() => {
    const onStart = (e) => {
      const sid = e?.detail?.sessionId
      if (!sid) return
      intelTriggerRef.current.set(sid, { trigger: e.detail.trigger || '', fileNames: e.detail.fileNames || [] })
      setTabs(prev => {
        let changed = false
        const next = prev.map(t => {
          if (t.sessionId !== sid || !t._aiBanner) return t
          changed = true
          return { ...t, _aiBanner: null }
        })
        return changed ? next : prev
      })
    }
    window.addEventListener('intel-generation-started', onStart)
    return () => window.removeEventListener('intel-generation-started', onStart)
  }, [])
  const intelSid = activeTabForIntel?.sessionId || null
  const intelTabId = activeTabForIntel?.id ?? null
  useEffect(() => {
    const w = intelWatchRef.current
    if (w.sid !== intelSid) {
      // New session: this render still carries the previous session's intel state
      // (the hook resets it in its own effect), so only start watching.
      intelWatchRef.current = { sid: intelSid, inFlight: false, update: false }
      return
    }
    if (!intelSid || intelTabId == null) return
    const { phase } = intelData
    if (phase === 'phase1' || phase === 'phase2') {
      w.inFlight = true
      if (intelData.isUpdating) w.update = true
      return
    }
    if (phase === 'ready' && w.inFlight) {
      const trig = intelTriggerRef.current.get(intelSid) || {}
      const text = intelBannerText({ mode: w.update ? 'update' : 'full', trigger: trig.trigger, fileNames: trig.fileNames })
      w.inFlight = false
      w.update = false
      intelTriggerRef.current.delete(intelSid)
      updateTab(intelTabId, {
        _aiBanner: makeBanner({ ...text, slot: SLOTS.AI, payload: { tabId: intelTabId, sessionId: intelSid } }),
      })
      return
    }
    if (phase === 'not_generated') { w.inFlight = false; w.update = false }
  }, [intelSid, intelTabId, intelData.phase, intelData.isUpdating, intelData.status, updateTab])
  // Single entry point for every USER-initiated cell delete (Outline row, bulk select,
  // section delete, in-cell toolbar). Decides via planCellDelete whether to confirm,
  // then runs the delete. Stable (reads live state through refs) so passing it down
  // does not rebuild the Notebook's per-cell handlers.
  //   options.run          what actually deletes (default: drop the ids from the tab's cells)
  //   options.cells        live cell objects to plan with (default: the tab's stored cells)
  //   options.afterDelete  runs after the delete, confirmed or not
  //   options.focusFallbackSelector  where focus goes if the trigger is gone afterwards
  const requestDeleteCells = useCallback((cellIds, options = {}) => {
    const ids = (cellIds || []).map(String)
    if (ids.length === 0) return
    const tabId = activeTabIdRef.current
    const storedCells = tabsRef.current.find(t => t.id === tabId)?._cells || []
    const cells = options.cells || storedCells
    const targets = cells.filter(c => ids.includes(String(c.id)))
    // allCells + the stored DAG: warn when other live cells read a variable only
    // these cells define (they keep working until the kernel restarts).
    const plan = planCellDelete(targets, cellBadgeMap(cells), {
      allCells: cells, dag: tabsRef.current.find(t => t.id === tabId)?._dag,
    })
    const finish = () => {
      if (options.run) options.run()
      else {
        const current = tabsRef.current.find(t => t.id === tabId)?._cells || []
        updateTab(tabId, { _cells: current.filter(c => !ids.includes(String(c.id))) })
      }
      options.afterDelete?.()
    }
    if (!plan.needsConfirm) { finish(); return }
    setPendingCellDelete({ tabId, ids, plan, finish, focusFallbackSelector: options.focusFallbackSelector })
  }, [updateTab])
  // Close the confirmation if its cells can no longer be deleted as planned: a tab
  // switch, or any of its cells disappearing (for example a run or an AI edit removed it).
  useEffect(() => {
    if (!pendingCellDelete) return
    const tab = tabs.find(t => t.id === pendingCellDelete.tabId)
    if (!tab || tab.id !== activeTabId) { setPendingCellDelete(null); return }
    const live = new Set((tab._cells || []).map(c => String(c.id)))
    if (pendingCellDelete.ids.some(id => !live.has(id))) setPendingCellDelete(null)
  }, [tabs, activeTabId, pendingCellDelete])

  // Merge a patch into a single cell of a tab's persisted _cells, keyed by cellId.
  // Store-authoritative + functional, so an in-flight cell execution that completes
  // AFTER the user switched away (its Notebook unmounted) still lands its result in
  // the correct notebook. Without this, the unmounted hook's setCells is a no-op and
  // the cell stays stuck on "running". No-op if the tab/cell no longer exists.
  const patchTabCell = useCallback((tabId, cellId, patch) => {
    setTabs(prev => prev.map(t => {
      if (t.id !== tabId || !Array.isArray(t._cells)) return t
      let changed = false
      const _cells = t._cells.map(c => {
        if (c.id !== cellId) return c
        changed = true
        return { ...c, ...patch }
      })
      return changed ? { ...t, _cells } : t
    }))
  }, [])

  const renameTab = useCallback((tabId, newName) => {
    setTabs(prev => prev.map(t => t.id === tabId ? { ...t, name: newName } : t))
  }, [])

  // Version restore: the proxy has already overwritten the persisted DOCUMENT and
  // taken a pre_restore snapshot. Swap the tab's live _cells to the restored set.
  // Only DOCUMENT fields are restored (id/type/code/outputVariable/aiExplanation/
  // generated); runtime output/exec state is cleared so the cells read as "not yet
  // run" — they rebuild on re-execution (versions never captured outputs).
  useEffect(() => {
    const onRestored = (e) => {
      const { notebookId, cells, dag } = e.detail || {}
      if (notebookId == null || !Array.isArray(cells)) return
      setTabs(prev => prev.map(t => {
        if (String(t.id) !== String(notebookId)) return t
        const _cells = cells.map(c => ({
          id: c.id || newCellId(),
          type: c.type || 'code',
          code: c.code || '',
          outputVariable: c.outputVariable ?? null,
          aiExplanation: c.aiExplanation || null,
          aiExplanationSource: c.aiExplanationSource || null,
          generated: c.generated || null,
          // Runtime state reset — outputs/dataflow are not versioned and rebuild
          // on re-run. Keeping stale output would misrepresent the restored code.
          output: null, error: null, html: null, image: null, imageS3Key: null,
          status: 'idle', executionNumber: null, executionTime: null,
          lastExecutedCode: null,
        }))
        // The restore response carries the DAG rebuilt from the restored cells.
        return { ...t, _cells, ...(newerDag(t._dag, dag) ? { _dag: dag } : {}) }
      }))
      // Make the restored notebook the active tab (usually already is, since the
      // drawer opens from its own toolbar, but this covers the general case).
      setActiveTabId(prev => (String(prev) === String(notebookId) ? prev : notebookId))
    }
    window.addEventListener('notebook-version-restored', onRestored)
    return () => window.removeEventListener('notebook-version-restored', onRestored)
  }, [])

  // Per-cell revert from the Version history drawer: replace ONE cell's code with
  // a version's code. We snapshot the current doc first (pre_restore) so the
  // revert is undoable, then patch just that cell and clear its runtime output
  // (the reverted code hasn't run yet). Autosave persists it.
  useEffect(() => {
    const onRevertCell = (e) => {
      const { notebookId, cellId, code } = e.detail || {}
      if (notebookId == null || cellId == null) return
      createNotebookVersion(String(notebookId), {
        label: 'Before cell revert', trigger: 'pre_restore',
      }).then(() => {
        window.dispatchEvent(new CustomEvent('notebook-version-saved', { detail: { notebookId } }))
      }).catch(() => {})
      patchTabCell(notebookId, cellId, {
        code: code || '',
        output: null, error: null, html: null, image: null, imageS3Key: null,
        status: 'idle', executionNumber: null, executionTime: null,
        lastExecutedCode: null,
      })
      // Make that notebook active, then scroll to + flash the reverted cell so the
      // user lands right on the change (the drawer closes itself on confirm).
      setActiveTabId(prev => (String(prev) === String(notebookId) ? prev : notebookId))
      setTimeout(() => {
        const el = document.querySelector(`[data-cell-id="${cellId}"]`)
        if (el) {
          // Align the cell TOP to the viewport (below the sticky toolbar via the
          // cell's scroll-margin-top), not center — so a tall reverted cell is read
          // from its start rather than landing mid-code.
          el.scrollIntoView({ behavior: 'smooth', block: 'start' })
          el.classList.remove('cell-flash'); void el.offsetWidth; el.classList.add('cell-flash')
        }
        window.dispatchEvent(new CustomEvent('notebook-active-cell', { detail: { cellId } }))
      }, 120)
    }
    window.addEventListener('notebook-revert-cell', onRevertCell)
    return () => window.removeEventListener('notebook-revert-cell', onRevertCell)
  }, [patchTabCell])

  // AI checkpoint: in-cell Apply Fix / generate dispatch 'notebook-pre-ai' just
  // before they replace a cell's code (the editor remount wipes CM undo). Snapshot
  // the ACTIVE notebook first; checkpointBeforeAI debounces bursts into one
  // 'pre_ai' version and refreshes the Versions panel.
  useEffect(() => {
    const onPreAi = () => { if (activeTabId != null) checkpointBeforeAI(String(activeTabId)) }
    window.addEventListener('notebook-pre-ai', onPreAi)
    return () => window.removeEventListener('notebook-pre-ai', onPreAi)
  }, [activeTabId])

  // Listen for "Open Notebook" events from Notebook toolbar (creates a new tab).
  // create-returns-id: the tab must be created in the DB (POST) before we can open
  // it, so the handler awaits createTab. The cells arrive via _loadedCells/_cells
  // and are persisted by the normal autosave PUT once the tab is live.
  useEffect(() => {
    const handler = (e) => {
      const { name, description, tag, cells } = e.detail
      clearBannersOn('same-op-start', { slot: SLOTS.NOTEBOOKS, match: b => b.kind === 'import' })
      ;(async () => {
        try {
          const tab = await createTab(name, description, tag || undefined)
          tab._loadedCells = cells
          // Every persisted field (services/notebookImport.js); imageS3Key is
          // dropped (a key would belong to the source notebook; storage drops it).
          tab._cells = (cells || []).map((c) => normalizeImportedCell(c, newCellId))
          setTabs(prev => [...prev, { ...tab }])
          setActiveTabId(tab.id)
          reportImportResult({ tabId: tab.id, ok: true, name: tab.name || name })
        } catch (err) {
          logError('Failed to open notebook (create failed)', err)
          setModal({ type: 'error', message: 'Could not open the notebook. Please try again.' })
          reportImportResult({ ok: false, error: 'Could not open the notebook. Please try again.' })
        }
      })()
    }
    window.addEventListener('open-notebook', handler)
    return () => window.removeEventListener('open-notebook', handler)
  }, [reportImportResult])

  const insertCode = useCallback((code, type) => {
    // Dispatch event for the active notebook to pick up
    window.dispatchEvent(new CustomEvent('insert-code', { detail: { code, type } }))
  }, [])

  const attachedIds = tabs.filter(t => t.microvmId).map(t => t.microvmId)

  return (
    <div className="app">
      <div className="app-body">
        <ErrorBoundary scope="sidebar">
        <Sidebar
          tabs={tabs}
          activeTabId={activeTabId}
          attachedIds={attachedIds}
          uploadedFiles={tabs.find(t => t.id === activeTabId)?._localFiles || []}
          onSelectTab={setActiveTabId}
          onNewNotebook={addTab}
          onCloseTab={closeTab}
          onDeleteNotebook={requestDeleteNotebook}
          onReopenNotebook={reopenNotebook}
          onRenameTab={renameTab}
          onUploadFile={uploadFile}
          onDeleteFile={deleteFile}
          onDeleteS3File={deleteS3File}
          onEndpointLifecycle={lifecycleEndpoint}
          onLoadSample={loadSample}
          onInsertCode={insertCode}
          cells={(tabs.find(t => t.id === activeTabId)?._cells) || []}
          variables={sidebarVariables}
          activeTab={tabs.find(t => t.id === activeTabId) || null}
          onTerminateAndSave={terminateAndSave}
          onSuspendInstance={suspendInstance}
          onRefreshInstances={fetchInstances}
          onUpdateTabTag={(tabId, tag) => updateTab(tabId, { tag })}
          onSyncPackages={(pkgList) => { if (activeTabId) updateTab(activeTabId, { _packages: pkgList }) }}
          onSyncDataSources={(ds) => { if (activeTabId) updateTab(activeTabId, { _dataSources: ds }) }}
          onSyncCatalogLoaded={(loaded) => { if (activeTabId) updateTab(activeTabId, { _catalogLoaded: loaded }) }}
          onSyncSourcesReady={(ready) => { if (activeTabId) updateTab(activeTabId, { _sourcesReady: ready }) }}
          onRefreshFiles={fetchFiles}
          instances={instances}
          vmMetrics={vmMetrics}
          sessionCosts={sessionCosts}
          sessionCostTotal={sessionCostTotal}
          onScrollToCell={(idx) => {
            const activeCells = tabs.find(t => t.id === activeTabId)?._cells || []
            const cell = activeCells[idx]
            if (cell) {
              setTimeout(() => {
                const el = document.querySelector(`[data-cell-id="${cell.id}"]`)
                if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
              }, 50)
            }
          }}
          onFixCell={(cellId) => {
            // "Fix it" from the Outline: scroll the errored cell to the top of the
            // viewport, then dispatch notebook-fix-cell so the Cell runs its own
            // handleAiFix() (same path as the in-cell ⚡ Fix button). The scroll goes
            // first (block:'start' puts the cell head in view) and the event fires
            // just after, so the user lands on the cell as the fix kicks off.
            const activeCells = tabs.find(t => t.id === activeTabId)?._cells || []
            const cell = activeCells.find(c => c.id === cellId)
            if (!cell) return
            const el = document.querySelector(`[data-cell-id="${cell.id}"]`)
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
            window.dispatchEvent(new CustomEvent('notebook-active-cell', { detail: { cellId } }))
            setTimeout(() => {
              window.dispatchEvent(new CustomEvent('notebook-fix-cell', { detail: { cellId } }))
            }, 60)
          }}
          onReorderCellIds={(orderedIds) => {
            // Bulk reorder: rebuild _cells to match the given id order in one update
            // (used by the outline's tree-aware block move up/down).
            const tab = tabs.find(t => t.id === activeTabId)
            if (!tab || !tab._cells) return
            const byId = new Map(tab._cells.map(c => [c.id, c]))
            const reordered = orderedIds.map(id => byId.get(id)).filter(Boolean)
            if (reordered.length === tab._cells.length) {
              updateTab(activeTabId, { _cells: reordered })
            }
          }}
          onRunFromCell={(cellIdx) => {
            window.dispatchEvent(new CustomEvent('notebook-run-from-cell', { detail: { cellIdx } }))
          }}
          onRequestDeleteCells={requestDeleteCells}
          onRunCells={(cellIndices) => {
            window.dispatchEvent(new CustomEvent('notebook-run-cells', { detail: { cellIndices } }))
          }}
          onClearOutputs={(cellIds) => {
            const currentCells = tabs.find(t => t.id === activeTabId)?._cells || []
            const updated = currentCells.map(c =>
              cellIds.includes(c.id) ? { ...c, output: null, error: null, html: null, image: null, imageS3Key: null, executionTime: null, executionNumber: null } : c
            )
            updateTab(activeTabId, { _cells: updated })
          }}
          onClearRunBanner={(tabId) => updateTab(tabId, clearBannerPatch())}
          onToggleHeatmap={() => {
            const tab = tabs.find(t => t.id === activeTabId)
            if (tab) updateTab(tab.id, tintTogglePatch(tab))
          }}
          lineageCellId={lineageFocusCellId}
          onLineageCellChange={setLineageCell}
          showTerminal={bottomPanelTabs.has('terminal')}
          onToggleTerminal={() => toggleBottomTab('terminal')}
          showLogs={bottomPanelTabs.has('logs')}
          onToggleLogs={() => toggleBottomTab('logs')}
        />
        </ErrorBoundary>
        <main className="app-main">
          {tabs.length === 0 && (
            <WelcomeScreen
              theme={theme}
              onToggleTheme={toggleTheme}
              onNewNotebook={addTab}
              showSampleGallery={showSampleGallery}
              onToggleSampleGallery={toggleSampleGallery}
              samples={samples}
              onLoadSample={loadSample}
              showGitImport={showGitImport}
              setShowGitImport={setShowGitImport}
              setShowSampleGallery={setShowSampleGallery}
              gitImportUrl={gitImportUrl}
              setGitImportUrl={setGitImportUrl}
              gitImportLoading={gitImportLoading}
              onImportFromGitUrl={importFromGitUrl}
            />
          )}
          {(() => {
            const tab = tabs.find(t => t.id === activeTabId)
            if (!tab) return null
            return (
            <Notebook
              key={tab.id}
              tab={tab}
              instances={instances}
              vmMetrics={vmMetrics}
              onUpdateTab={(updates) => updateTab(tab.id, updates)}
              onPatchCell={(cellId, patch) => patchTabCell(tab.id, cellId, patch)}
              onMarkVmRunning={markVmRunning}
              onNewNotebook={addTab}
              onCloseTab={closeTab}
              onDeleteNotebook={requestDeleteNotebook}
              onRequestDeleteCells={requestDeleteCells}
              lineageCellId={lineageFocusCellId}
              lineageOpener={lineageOpenerRef.current}
              onLineageCellChange={setLineageCell}
              theme={theme}
              onToggleTheme={toggleTheme}
              aiAvailable={aiAvailable}
              onRefreshMetrics={() => refreshMetrics(tab.microvmId)}
            />
            )
          })()}
          {bottomPanelTabs.size > 0 && (
            <div className="bottom-panel-container" style={{ height: bottomPanelHeight }}>
              <div
                className="bottom-panel-resize-handle"
                onMouseDown={(e) => {
                  e.preventDefault()
                  const startY = e.clientY
                  const startHeight = bottomPanelHeight
                  // Overlay so dragging over an embedded Plotly iframe doesn't steal the
                  // mouse events and break the resize.
                  showDragOverlay('row-resize')
                  const handleMove = (moveEvent) => {
                    const delta = startY - moveEvent.clientY
                    setBottomPanelHeight(Math.max(100, Math.min(window.innerHeight * 0.7, startHeight + delta)))
                  }
                  const handleUp = () => {
                    document.removeEventListener('mousemove', handleMove)
                    document.removeEventListener('mouseup', handleUp)
                    hideDragOverlay()
                  }
                  document.addEventListener('mousemove', handleMove)
                  document.addEventListener('mouseup', handleUp)
                }}
              />
              <div className="bottom-panel-tabs">
                {bottomPanelTabs.has('terminal') && (
                  <button
                    className={`bottom-panel-tab ${bottomPanelActive === 'terminal' ? 'bottom-panel-tab-active' : ''}`}
                    onClick={() => setBottomPanelActive('terminal')}
                  >
                    <IconTerminal width={14} height={14} />
                    Terminal
                    {bottomPanelTabs.size > 1 && (
                      <span className="bottom-panel-tab-close" onClick={(e) => { e.stopPropagation(); closeBottomTab('terminal') }}>&times;</span>
                    )}
                  </button>
                )}
                {bottomPanelTabs.has('logs') && (
                  <button
                    className={`bottom-panel-tab ${bottomPanelActive === 'logs' ? 'bottom-panel-tab-active' : ''}`}
                    onClick={() => setBottomPanelActive('logs')}
                  >
                    <IconLogs width={14} height={14} />
                    Logs
                    {bottomPanelTabs.size > 1 && (
                      <span className="bottom-panel-tab-close" onClick={(e) => { e.stopPropagation(); closeBottomTab('logs') }}>&times;</span>
                    )}
                  </button>
                )}
                <button
                  className="bottom-panel-close"
                  onClick={() => { setBottomPanelTabs(new Set()); setBottomPanelActive(null) }}
                  title="Close panel"
                >
                  &times;
                </button>
              </div>
              {/* Terminal "command finished" banner (slot 'bottom'), published by
                  TerminalPanel only while the terminal is hidden or not focused.
                  Click: show the terminal tab and focus the shell. */}
              {bottomBanner && (
                <ActionBanner
                  banner={bottomBanner}
                  className="slot-banner bottom-slot-banner"
                  onAction={() => {
                    clearBanner(SLOTS.BOTTOM, bottomBanner.id)
                    setBottomPanelActive('terminal')
                    window.dispatchEvent(new CustomEvent(TERMINAL_FOCUS_EVENT))
                  }}
                  onDismiss={() => clearBanner(SLOTS.BOTTOM, bottomBanner.id)}
                  dismissLabel="Dismiss terminal message"
                />
              )}
              {/* Keep each open panel MOUNTED and just toggle visibility, rather
                  than mounting only the active one. Unmounting tore down the
                  terminal's xterm + WebSocket and the logs' SSE stream on every
                  tab switch, so flipping terminal<->logs for the SAME notebook
                  needlessly reconnected both. Both panels key their reconnect on
                  activeTab.sessionId, so a NOTEBOOK switch still resets them; a
                  bottom-tab switch is now pure show/hide (no reload). The inactive
                  one is display:none — the terminal's ResizeObserver re-fits it
                  when it becomes visible again. */}
              {bottomPanelTabs.has('terminal') && (
                <div
                  className="bottom-panel-content"
                  style={{ display: bottomPanelActive === 'terminal' ? 'flex' : 'none' }}
                >
                  <TerminalPanel
                    activeTab={tabs.find(t => t.id === activeTabId) || null}
                    onClose={() => closeBottomTab('terminal')}
                    theme={theme}
                    visible={bottomPanelActive === 'terminal'}
                    embedded={bottomPanelTabs.size > 1}
                  />
                </div>
              )}
              {bottomPanelTabs.has('logs') && (
                <div
                  className="bottom-panel-content"
                  style={{ display: bottomPanelActive === 'logs' ? 'flex' : 'none' }}
                >
                  <LogsPanel
                    activeTab={tabs.find(t => t.id === activeTabId) || null}
                    onClose={() => closeBottomTab('logs')}
                  />
                </div>
              )}

            </div>
          )}
        </main>
        {showAiChat && (
          <AiChatPanel
            activeTab={tabs.find(t => t.id === activeTabId) || null}
            uploadedFiles={tabs.find(t => t.id === activeTabId)?._localFiles || []}
            intelData={intelData}
            onClearAiBanner={(tabId) => updateTab(tabId, { _aiBanner: null })}
            contextWindowSize={aiWindowSize}
            onClose={() => setShowAiChat(false)}
            onUpdateMessages={(msgs) => {
              // Update the in-tab transcript for live rendering only. Durable
              // persistence is now owned by the chat agent (its Strands
              // SessionManager writes every message to agent_sessions/agent_messages),
              // so the frontend no longer PUTs the transcript. On notebook reopen the
              // messages are rehydrated via loadChatMessages (GET /ai/chat/{id}/messages).
              updateTab(activeTabId, { _chatMessages: msgs })
            }}
            onUpdateSelectedModel={(id) => {
              // Remember the user's model choice PER NOTEBOOK (mirrors
              // onUpdateMessages). Stored as _selectedModelId on the tab and
              // persisted via useTabsPersistence; the panel reads it back from
              // activeTab and sends it as model_id on every turn.
              updateTab(activeTabId, { _selectedModelId: id })
            }}
            onUpdateCell={(code) => {
              const tab = tabs.find(t => t.id === activeTabId)
              if (!tab || !tab._cells || tab._activeCellIndex == null) return
              // Restore point before the AI assistant rewrites this cell.
              if (activeTabId != null) checkpointBeforeAI(String(activeTabId))
              const newCells = [...tab._cells]
              newCells[tab._activeCellIndex] = { ...newCells[tab._activeCellIndex], code }
              updateTab(activeTabId, { _cells: newCells })
            }}
            onInsertCells={(codeBlocks, types, requestIds) => {
              // Restore point before the assistant inserts AI-generated cells.
              if (activeTabId != null) checkpointBeforeAI(String(activeTabId))
              codeBlocks.forEach((code, i) => {
                const type = types?.[i] || 'code'
                // requestId (optional): the chat tracks which cell each block became
                // (echoed back in notebook-cells-inserted).
                const requestId = requestIds?.[i]
                window.dispatchEvent(new CustomEvent('insert-code', { detail: requestId ? { code, type, requestId } : { code, type } }))
              })
            }}
          />
        )}
      </div>

      {/* AI Chat toggle button (bottom-right, draggable + position remembered).
          Shown whenever the panel is closed and AI is available — NOT gated on
          having a notebook open, otherwise closing the last notebook leaves no
          way to reopen the assistant (the FAB would vanish on the welcome
          screen). The panel itself handles the no-active-notebook state. */}
      {!showAiChat && aiAvailable && (
        <AiFab
          onOpen={() => setShowAiChat(true)}
          alerts={fabAlerts}
          investigations={fabInvestigations}
          intelPhase={intelData.phase}
          intelHasContent={intelData.hasContent}
        />
      )}

      {/* Modals */}
      {modal?.type === 'newNotebook' && (
        <InputModal
          title="New Notebook"
          onSubmit={confirmNewNotebook}
          onCancel={() => setModal(null)}
          submitLabel="Create"
          fields={<>
            <div className="modal-input-group">
              <label className="modal-label">Name</label>
              <input
                className="modal-input"
                value={newNotebookName}
                onChange={(e) => setNewNotebookName(e.target.value)}
                placeholder="My Notebook"
                autoFocus
              />
            </div>
            <div className="modal-input-group">
              <label className="modal-label">Description (optional)</label>
              <input
                className="modal-input"
                value={newNotebookDesc}
                onChange={(e) => setNewNotebookDesc(e.target.value)}
                placeholder="What this notebook is about..."
              />
              <div className="modal-input-hint">Shown below the toolbar when the notebook is open.</div>
            </div>
          </>}
        />
      )}

      {modal?.type === 'deleteNotebook' && (
        <div className="modal-overlay" onClick={() => setModal(null)}>
          <div className="modal-card" onClick={e => e.stopPropagation()}>
            <div className="modal-title">Delete "{modal.tabName}"?</div>
            <div className="modal-body">
              <p className="modal-message">This permanently deletes the notebook, all its cells, and its entire version history from the database. It cannot be undone. To just put it away, use <b>Close</b> instead — a closed notebook can be reopened anytime.</p>
            </div>
            <div className="modal-actions">
              <button className="modal-btn modal-btn-cancel" onClick={() => setModal(null)}>Cancel</button>
              <button className="modal-btn modal-btn-danger" onClick={() => confirmDeleteNotebook(modal.tabId)}>Delete permanently</button>
            </div>
          </div>
        </div>
      )}

      {pendingCellDelete && (
        <CellDeleteConfirm
          plan={pendingCellDelete.plan}
          focusFallbackSelector={pendingCellDelete.focusFallbackSelector}
          onConfirm={() => { const p = pendingCellDelete; setPendingCellDelete(null); p.finish() }}
          onCancel={() => setPendingCellDelete(null)}
        />
      )}
      {modal?.type === 'vmSessionMismatch' && (
        <ConfirmModal
          title="Not terminated"
          onCancel={() => setModal(null)}
          onConfirm={() => setModal(null)}
          confirmLabel="OK"
          cancelLabel=""
        >
          <p className="modal-message">
            MicroVM <strong>{modal.microvmId.replace('microvm-', '').slice(0, 8)}</strong> belongs to another notebook's session, not to <strong>"{modal.notebookName}"</strong>.
          </p>
          <div className="modal-warning">
            Nothing was sent. On the next refresh the notebook reconnects to its own MicroVM, or offers a restore if it has none.
          </div>
        </ConfirmModal>
      )}
      {/* Terminate & Save whose save failed: the proxy kept the VM running. */}
      {modal?.type === 'terminateSaveFailed' && (
        <ConfirmModal
          title="Save failed"
          onCancel={() => setModal(null)}
          onConfirm={() => { const id = modal.microvmId; setModal(null); terminateAndSave(id, { withoutSaving: true }) }}
          confirmLabel="Terminate without saving"
          cancelLabel="Keep running"
          confirmDanger
        >
          <p className="modal-message">{modal.message}</p>
        </ConfirmModal>
      )}
      {/* Terminate & Save whose save stopped the VM (out of memory). */}
      {modal?.type === 'terminateSaveLost' && (
        <ConfirmModal
          title="Save failed"
          onCancel={() => setModal(null)}
          onConfirm={() => setModal(null)}
          confirmLabel="OK"
          cancelLabel=""
        >
          <p className="modal-message">{modal.message}</p>
        </ConfirmModal>
      )}

    </div>
  )
}
