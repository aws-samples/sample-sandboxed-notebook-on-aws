import { useState, useEffect, useCallback, useRef, useId, Children, isValidElement } from 'react'
import { createPortal } from 'react-dom'
import { IconRefresh, IconX, IconServer, IconArchive, IconChevronDown, IconChevronRight, IconCheck, ArchBadge } from '../Icons'
import { PROXY_URL } from '../../config'
import { fetchWithTimeout } from '../../services/fetchWithTimeout'
import { readApiError } from '../../services/apiError'
import { saveFailureFromTerminate, terminateBody } from '../../services/terminateSave'
import { ConfirmModal } from '../Modal'
import {
  VM_TAB_STORAGE_KEY, DEFAULT_VM_TAB, parseStoredVmTab, visibleVmTabs, resolveVmTab, nextVmTabIndex,
} from '../../services/vmDetailTabs'
import { costRateLabels } from '../../services/costRates'
import { vmCostRows } from '../../services/vmCostRows'
import {
  formatClock, formatCountdown, countdownLevel, countdownSuffix,
  checkpointRows, lifecycleCounters, lifecycleOrigin, idleSuspendLabel, formatShortDateTime,
  packagesSummary, imageLabel, imageNameFromArn, formatVersion,
} from '../../services/vmCardState'
import {
  VM_SORT_STORAGE_KEY, DEFAULT_VM_SORT, VM_SORT_OPTIONS, parseStoredVmSort, makeVmComparator,
} from '../../services/vmSort'
import PanelState from './PanelState'
import PanelExpandButton from './PanelExpandButton'
import SlotBanner from './SlotBanner'
import { SLOTS } from '../../services/actionBanner'
import VmMetricsHistory from './VmMetricsHistory'
import VmSectionToggle from './VmSectionToggle'
import { panelInstancesTick } from '../../services/instancesPoll'
import { isSectionCollapsed, toggleSection } from '../../services/vmSectionCollapse'
import { DEFAULT_POLL_INTERVAL_MS, DEFAULT_MEMORY_MIB } from '../../constants'

// "(launch/resume)" is one long word at 240 px: allow a line break after each "/"
// (<wbr>, so the text itself is unchanged).
function withSlashBreaks(text) {
  const parts = String(text).split('/')
  return parts.flatMap((p, i) => (i ? ['/', <wbr key={`w${i}`} />, p] : [p]))
}

// Comma-separated list clamped to 2 lines by CSS (line-clamp), with a
// "Show all (n)" / "Show less" toggle shown only when the text needs more than
// 2 lines (measured: full content height vs. 2 line heights, re-measured on resize).
function ClampedList({ text, count, expanded, onToggle }) {
  const ref = useRef(null)
  const listId = useId()
  const [overflows, setOverflows] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const measure = () => {
      const lh = parseFloat(getComputedStyle(el).lineHeight) || 16
      setOverflows(el.scrollHeight > 2 * lh + 1)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [text])
  return (
    <>
      <span
        ref={ref}
        id={listId}
        className={`vm-detail-value vm-detail-value-list${expanded ? '' : ' is-clamped'}`}
      >{text}</span>
      {(overflows || expanded) && (
        <button
          type="button"
          className="vm-list-toggle"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={onToggle}
        >{expanded ? 'Show less' : `Show all (${count})`}</button>
      )}
    </>
  )
}

export default function MicroVMsPanel({
  tabs,
  activeTab,
  attachedIds,
  instances: parentInstances,
  onRefreshInstances,
  onTerminateAndSave,
  onSuspendInstance,
  onSelectTab,
  formatDuration,
  onClose,
  expanded,
  onToggleExpand,
}) {
  const [localInstances, setLocalInstances] = useState({})
  const [vmLoading, setVmLoading] = useState(false)
  const [vmFetched, setVmFetched] = useState(false)
  // null = not yet known. Avoids briefly showing a misleading mode badge before
  // the /instances fetch reports the proxy's actual persistence_mode.
  const [persistenceMode, setPersistenceMode] = useState(null)
  const [expandedVmId, setExpandedVmId] = useState(null)
  const [vmActionInProgress, setVmActionInProgress] = useState(new Set())
  // Failed pre-terminate save of an unattached VM's Terminate: { id, kind, message } or null.
  const [terminateSaveFailure, setTerminateSaveFailure] = useState(null)

  // Sort order for the VM list — persisted like the sidebar's panel/width prefs.
  // Default 'launched-desc' preserves the previous hardcoded newest-first behavior;
  // an unknown stored value falls back to it.
  const [vmSort, setVmSort] = useState(() => {
    try { return parseStoredVmSort(localStorage.getItem(VM_SORT_STORAGE_KEY)) } catch { return DEFAULT_VM_SORT }
  })
  useEffect(() => {
    try { localStorage.setItem(VM_SORT_STORAGE_KEY, vmSort) } catch {}
  }, [vmSort])

  // Pool-spares section collapse state — expanded by default, persisted.
  const [poolSparesCollapsed, setPoolSparesCollapsed] = useState(() => {
    try { return localStorage.getItem('microvm-pool-spares-collapsed') === '1' } catch { return false }
  })
  useEffect(() => {
    try { localStorage.setItem('microvm-pool-spares-collapsed', poolSparesCollapsed ? '1' : '0') } catch {}
  }, [poolSparesCollapsed])

  // Selected tab of the expanded VM card (Overview / Cost / Resources). Remembered
  // for the PANEL, not per VM: opening another VM's card shows the same tab. Lives
  // here (not in the card) so /instances polling re-renders never reset it. A VM
  // for which the remembered tab is hidden shows Overview without overwriting it.
  const [vmDetailTab, setVmDetailTab] = useState(() => {
    try { return parseStoredVmTab(localStorage.getItem(VM_TAB_STORAGE_KEY)) } catch { return DEFAULT_VM_TAB }
  })
  useEffect(() => {
    try { localStorage.setItem(VM_TAB_STORAGE_KEY, vmDetailTab) } catch {}
  }, [vmDetailTab])

  // VM ids whose "User installed packages" list is expanded (default collapsed).
  // Kept here so /instances polling re-renders never reset it.
  const [pkgListOpen, setPkgListOpen] = useState(() => new Set())
  const togglePkgList = useCallback((id) => {
    setPkgListOpen(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  // Collapsed sections of the VM card, per VM id + section (all expanded by default).
  // Here (not in the card) so polling re-renders and tab switches keep them.
  const [collapsedSections, setCollapsedSections] = useState(() => new Set())
  const toggleVmSection = useCallback((vmId, section) => {
    setCollapsedSections(prev => toggleSection(prev, vmId, section))
  }, [])

  // Per-VM detail for the EXPANDED card only (Overview Checkpoint/Started + Resources > Environment):
  // GET /instances/{id}/detail on expand and when the VM's state changes. NO
  // periodic refresh: for a RUNNING VM the proxy calls the VM's /vm-status, and
  // ANY inbound request resets the VM's idle timer (idlePolicy
  // maxIdleDurationSeconds, default 60 s). A 60 s refresh therefore kept an idle
  // VM running forever while its card was open. Never part of the /instances
  // poll. A suspended VM's values come from its checkpoint metadata (no VM call).
  // Errors keep the previous data.
  // { [id]: { data, at } }; data null = fetched but unavailable.
  const [vmDetails, setVmDetails] = useState({})
  const expandedVmState = expandedVmId ? (parentInstances?.[expandedVmId]?.state ?? localInstances[expandedVmId]?.state) : undefined
  useEffect(() => {
    if (!expandedVmId) return undefined
    const id = expandedVmId
    let alive = true
    let ctrl = null
    const load = async () => {
      if (document.visibilityState === 'hidden') return
      ctrl?.abort()
      ctrl = new AbortController()
      try {
        const resp = await fetchWithTimeout(`${PROXY_URL}/instances/${encodeURIComponent(id)}/detail`, { timeout: 6000, signal: ctrl.signal })
        const data = resp.ok ? await resp.json() : null
        if (!alive) return
        setVmDetails(prev => (data || !prev[id]) ? { ...prev, [id]: { data: data || prev[id]?.data || null, at: Date.now() } } : prev)
      } catch {
        if (alive) setVmDetails(prev => prev[id] ? prev : { ...prev, [id]: { data: null, at: Date.now() } })
      }
    }
    load()
    return () => { alive = false; ctrl?.abort() }
  }, [expandedVmId, expandedVmState])

  // Tick every 5s to update idle progress bar on Suspend button
  const [, setTick] = useState(0)
  useEffect(() => {
    const interval = setInterval(() => setTick(t => t + 1), 5000)
    return () => clearInterval(interval)
  }, [])

  // Use parent instances (from App.jsx fast poll) when available, fallback to local
  const vmInstances = (parentInstances && Object.keys(parentInstances).length > 0)
    ? parentInstances : localInstances

  const fetchVmInstances = useCallback(async (showLoading = true) => {
    if (showLoading) setVmLoading(true)
    try {
      const resp = await fetchWithTimeout(`${PROXY_URL}/instances`)
      if (resp.ok) {
        const data = await resp.json()
        setLocalInstances(data.instances || {})
        if (data.persistence_mode) setPersistenceMode(data.persistence_mode)
      } else {
        setLocalInstances({})
      }
    } catch {
      setLocalInstances({})
    }
    setVmLoading(false)
    setVmFetched(true)
  }, [])

  // Initial fetch (only if parent isn't providing instances)
  useEffect(() => {
    if (!vmFetched && (!parentInstances || Object.keys(parentInstances).length === 0)) {
      fetchVmInstances()
    } else if (!vmFetched) {
      // Parent provides instances, but we still need persistence_mode
      fetchWithTimeout(`${PROXY_URL}/instances`)
        .then(r => r.ok ? r.json() : null)
        .then(data => { if (data?.persistence_mode) setPersistenceMode(data.persistence_mode) })
        .catch(() => {})
      setVmFetched(true)
    }
  }, [vmFetched, fetchVmInstances, parentInstances])

  // Live refresh WHILE THIS PANEL IS OPEN. App's recurring /instances poll
  // (useInstances.maybeFetch) is gated on an open notebook tab bound to a VM, so with
  // only pool spares (or no notebook open) the panel would otherwise show the
  // mount-time snapshot forever — e.g. a spare's "Suspended 9s" never advancing.
  // Polls only while mounted (= panel open) and the browser tab is visible. Reading
  // /instances is a control-plane list; it never touches or wakes a VM.
  // Latest values via a ref so ONE interval lives for the panel's lifetime (deps
  // would otherwise restart it on every poll result).
  const liveRefreshRef = useRef({})
  // When App's poll is already running (a notebook tab is bound) and feeding
  // parentInstances, this tick does nothing, so /instances is polled once per
  // interval, not twice (services/instancesPoll.js).
  liveRefreshRef.current = { onRefreshInstances, parentInstances, fetchVmInstances, tabs }
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === 'hidden') return
      const { onRefreshInstances: refresh, parentInstances: parent, fetchVmInstances: fetchLocal, tabs: allTabs } = liveRefreshRef.current
      const action = panelInstancesTick({ tabs: allTabs, parentInstances: parent, hasParentRefresh: !!refresh })
      if (action === 'parent') refresh()
      else if (action === 'local') fetchLocal(false)
    }
    const interval = setInterval(tick, DEFAULT_POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [])

  // Auto-expand the VM connected to the active notebook
  useEffect(() => {
    if (activeTab?.microvmId) {
      setExpandedVmId(activeTab.microvmId)
    }
  }, [activeTab?.microvmId])

  const handleVmResume = async (id) => {
    setVmActionInProgress(prev => new Set([...prev, id]))
    try {
      const sessionId = vmInstances[id]?.session_id
      if (!sessionId) throw new Error('No session ID for this VM')
      const resp = await fetchWithTimeout(`${PROXY_URL}/resume`, { method: 'POST', headers: { 'X-Session-Id': sessionId } })
      if (!resp.ok) {
        console.error(`[VM] Resume failed for ${id}:`, await readApiError(resp, String(resp.status)))
      }
      await fetchVmInstances()
    } catch (err) {
      console.error(`[VM] Resume error for ${id}:`, err.message)
    }
    setVmActionInProgress(prev => { const n = new Set(prev); n.delete(id); return n })
  }

  // Delegated-action wrappers for the ATTACHED-VM buttons. These call parent
  // props (onTerminateAndSave / onSuspendInstance) that do the real work but do
  // NOT touch this panel's vmActionInProgress set — so without a wrapper the
  // button's spinner/disabled state (driven by isActioning = vmActionInProgress
  // .has(id)) never fired, and a slow terminate-and-save (pre-terminate
  // checkpoint + /terminate, several seconds) looked like a dead click with no
  // feedback. Mark the id in-progress, await the prop, then clear it in finally
  // so the spinner shows for the real duration and the button can't be
  // double-clicked. Mirrors handleVmTerminate / handleVmResume below.
  const handleVmTerminateAndSave = async (id) => {
    if (!onTerminateAndSave) return
    setVmActionInProgress(prev => new Set([...prev, id]))
    try {
      await onTerminateAndSave(id)
    } catch (err) {
      console.error(`[VM] Terminate & Save error for ${id}:`, err?.message || err)
    } finally {
      setVmActionInProgress(prev => { const n = new Set(prev); n.delete(id); return n })
    }
  }

  const handleVmSuspend = async (id) => {
    if (!onSuspendInstance) return
    setVmActionInProgress(prev => new Set([...prev, id]))
    try {
      await onSuspendInstance(id)
    } catch (err) {
      console.error(`[VM] Suspend error for ${id}:`, err?.message || err)
    } finally {
      setVmActionInProgress(prev => { const n = new Set(prev); n.delete(id); return n })
    }
  }

  const handleVmTerminate = async (id, { withoutSaving = false } = {}) => {
    setVmActionInProgress(prev => new Set([...prev, id]))
    try {
      // Terminate is keyed on microvmId; X-Session-Id is OPTIONAL so an UNATTACHED
      // VM (no session_id — e.g. orphaned after a DB reset) is still terminable.
      const sessionId = vmInstances[id]?.session_id
      const headers = { 'Content-Type': 'application/json' }
      if (sessionId) headers['X-Session-Id'] = sessionId
      const resp = await fetchWithTimeout(`${PROXY_URL}/terminate`, {
        method: 'POST',
        headers,
        body: JSON.stringify(terminateBody(id, { withoutSaving })),
        // The pre-terminate save of a large state can take a while (the proxy
        // allows the VM 60 s, plus a resume of a suspended VM).
        timeout: 120000,
      })
      // A failed pre-terminate save keeps the VM running (409) or, if the VM died
      // while saving, reports it in the 200 body: show the reason either way.
      const failure = saveFailureFromTerminate(resp.status, await resp.clone().json().catch(() => null))
      if (failure) setTerminateSaveFailure({ id, ...failure })
      else if (!resp.ok) {
        console.error(`[VM] Terminate failed for ${id}:`, await readApiError(resp, String(resp.status)))
      }
      await fetchVmInstances()
    } catch (err) {
      console.error(`[VM] Terminate error for ${id}:`, err.message)
    }
    setVmActionInProgress(prev => { const n = new Set(prev); n.delete(id); return n })
  }

  // Comparator for the chosen sort key (services/vmSort.js). Operates on [id, inst] entries.
  const vmComparator = makeVmComparator(vmSort)

  // Live (non-terminated) VMs, split into user/notebook VMs and warm-pool spares.
  // A pool spare is unattached with no notebook link and no name (mirrors the
  // isUnattached test used in the row render below). Each group is sorted
  // independently by the same chosen key. .sort() is a stable sort in V8, so the
  // 'state' grouping keeps relative order within a state bucket.
  const liveEntries = Object.entries(vmInstances)
    .filter(([, inst]) => inst.state !== 'TERMINATED' && inst.state !== 'TERMINATING')
  const isPoolSpare = ([id, inst]) =>
    !attachedIds.includes(id) && inst.notebook_id == null && !inst.name
  const userVmEntries = liveEntries.filter(e => !isPoolSpare(e)).sort(vmComparator)
  const poolSpareEntries = liveEntries.filter(isPoolSpare).sort(vmComparator)

  // Per-VM row render — shared by both groups so the output is identical to before.
  const renderVmItem = ([id, inst]) => {
          const isExpanded = expandedVmId === id
          const isActive = activeTab?.microvmId === id
          const isActioning = vmActionInProgress.has(id)
          const state = inst.state || 'UNKNOWN'
          const attachedTab = tabs.find(t => t.microvmId === id)
          const memGb = (inst.memory_mib || DEFAULT_MEMORY_MIB) / 1024
          // Per-arch rates the proxy priced this VM with (arm64 fallback).
          const rateLabels = costRateLabels(inst.cost)
          const sessionCost = inst.session_cost?.total_cost_usd || inst.cost?.total_cost_usd || 0
          // Linked to a CLOSED notebook: no open tab references this VM (the user
          // closed the notebook), but the VM still carries a notebook link — it
          // was suspended rather than terminated on close, so it's still live and
          // billing here. Distinct from a genuinely "Unattached" VM (no notebook
          // link at all), which gets its own red styling elsewhere in this file.
          const isClosedNotebookVm = !attachedIds.includes(id) && (inst.notebook_id != null || !!inst.name)
          // Genuinely UNATTACHED = a warm-pool spare: no open tab, no notebook link,
          // and no notebook name at all. The exact inverse of isClosedNotebookVm
          // among the not-attached VMs. Gets its own non-striped treatment (a teal
          // accent + "Pool spare" badge) so it reads as unattached at a glance,
          // visually distinct from the diagonal-stripe "Notebook closed" rows.
          const isUnattached = !attachedIds.includes(id) && inst.notebook_id == null && !inst.name

          // Countdown urgency — for any non-eternal mode (checkpoint AND none),
          // the VM dies at max_lifetime. Eternal mode rotates instead, so no
          // countdown there. None mode follows checkpoint's presentation.
          let remainingSec = Infinity
          let expiryEpoch = null
          let vmUrgency = ''
          if (persistenceMode !== 'eternal' && inst.launched_at && inst.max_duration_sec) {
            const launchMs = typeof inst.launched_at === 'number'
              ? inst.launched_at * 1000
              : new Date(inst.launched_at).getTime()
            expiryEpoch = (launchMs + inst.max_duration_sec * 1000) / 1000
            remainingSec = Math.max(0, Math.floor((launchMs + inst.max_duration_sec * 1000 - Date.now()) / 1000))
            if (remainingSec <= 10) vmUrgency = 'vm-item-critical'
            else if (remainingSec <= 60) vmUrgency = 'vm-item-warning'
          }

          return (
            <div key={id} className="vm-session-group">
              {/* Session-level row — only in eternal mode */}
              {persistenceMode === 'eternal' && (
              <div className={`vm-session-header ${isClosedNotebookVm ? 'vm-session-header-closed' : ''}`}>
                <div className="vm-session-info">
                  <span className="vm-session-notebook">
                    {(() => {
                      // tab.id IS the DB notebook_id (create-returns-id). Prefer the
                      // open tab's id; fall back to the VM's notebook_id (unattached VMs).
                      const nbName = attachedTab?.name || inst.name || 'Notebook'
                      const nbId = attachedTab?.id ?? inst.notebook_id
                      return nbId != null && nbId !== '' ? `${nbName} (${nbId})` : nbName
                    })()}
                    {isClosedNotebookVm && (
                      <span className="vm-item-closed-badge">
                        <IconArchive width={10} height={10} /> Closed
                      </span>
                    )}
                  </span>
                </div>
                <span className="vm-session-cost">${sessionCost.toFixed(4)}</span>
              </div>
              )}

              {/* Current VM row */}
              <div className={`vm-item ${isActive ? 'vm-item-active' : ''} ${vmUrgency} ${isClosedNotebookVm ? 'vm-item-closed-nb' : ''} ${isUnattached ? 'vm-item-unattached' : ''}`}>
              <div className="vm-item-row" onClick={() => setExpandedVmId(isExpanded ? null : id)}>
                <span className={`vm-state-dot vm-state-${state.toLowerCase()}`} />
                <div className="vm-item-info">
                  <span className="vm-item-name">
                    {id}
                    {isClosedNotebookVm && (
                      <span className="vm-item-closed-badge" title="This notebook is closed — the VM is suspended in the background, reopen the notebook to reattach">
                        <IconArchive width={10} height={10} /> Notebook closed
                      </span>
                    )}
                  </span>
                  <span className="vm-item-meta">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="10" x2="6" y2="14"/><line x1="18" y1="10" x2="18" y2="14"/></svg>
                    {memGb} GB
                    <span className="vm-meta-sep">·</span>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9z"/></svg>
                    {(memGb / 2).toFixed(1)} vCPU
                    <span className="vm-meta-sep">·</span>
                    <ArchBadge arch={inst.architecture} />
                    <span className="vm-meta-sep">·</span>
                    {state.toLowerCase()}
                    {isUnattached && (
                      <span className="vm-item-unattached-badge" title="Warm-pool spare — pre-booted and suspended, not attached to any notebook. Vended to a notebook on launch.">
                        <IconServer width={10} height={10} /> Pool spare
                      </span>
                    )}
                  </span>
                </div>
                {inst.cost && <span className="vm-item-cost">${inst.cost.total_cost_usd.toFixed(4)}</span>}
              </div>
              {isExpanded && (() => {
                // Resources is shown for every VM (its Environment section applies to
                // suspended VMs and spares); Cost only with cost data. A remembered
                // tab that's hidden here shows Overview (the stored choice is kept).
                const detailTabs = visibleVmTabs({ hasCost: !!inst.cost })
                const shownTab = resolveVmTab(vmDetailTab, detailTabs)
                const detailEntry = vmDetails[id]
                const detail = detailEntry?.data || null
                const detailPending = !detailEntry
                const nowMs = Date.now()
                // Timing thresholds: amber < 30 min, red < 10 min (text + colour)
                const cdLevel = remainingSec < Infinity ? countdownLevel(remainingSec) : ''
                // Overview > Notebook: "Name (32)" / "Unattached" / "… — closed".
                // tab.id IS the DB notebook_id (create-returns-id); fall back to
                // inst.notebook_id for a VM not open as a tab here.
                const nbName = inst.name || attachedTab?.name
                const nbId = attachedTab?.id ?? inst.notebook_id
                const nbText = !nbName ? 'Unattached' : (nbId != null && nbId !== '' ? `${nbName} (${nbId})` : nbName)
                const nbClass = isClosedNotebookVm
                  ? 'vm-detail-closed-nb'
                  : (!attachedIds.includes(id) && !inst.name ? 'vm-detail-unattached' : 'vm-detail-linked')
                // Overview > Lifecycle: ONE fact per row (user feedback UF2): Started,
                // Source, Restored from, Running, Suspended, Resumes, Suspends,
                // Auto-suspend, Terminates in. A zero / unknown value omits its row.
                const startedText = formatShortDateTime(inst.launched_at)
                const origin = isUnattached ? null : lifecycleOrigin(detail?.origin)
                const counters = isUnattached ? lifecycleCounters(null) : lifecycleCounters(inst.cost)
                const lifeRows = [
                  ['Started', startedText],
                  ['Source', origin?.source],
                  ['Restored from', origin?.restoredFrom],
                  ['Running', counters.running],
                  ['Suspended', counters.suspended],
                  ['Resumes', counters.resumes],
                  ['Suspends', counters.suspends],
                ].filter(([, v]) => v)
                const idleText = idleSuspendLabel(inst.idle_timeout_sec)
                const showExpiry = persistenceMode !== 'eternal' && remainingSec < Infinity
                const hasLifecycle = !!(lifeRows.length || idleText || showExpiry)
                // Overview > Checkpoint (notebook VMs only): Saved, Variables, Size (UF2).
                // From the VM when it is RUNNING, else from the last checkpoint. The
                // size is the whole saved state; "changed since" carries the
                // unsaved-changes state.
                const cp = detail?.checkpoint || null
                const dirty = typeof detail?.dirty === 'boolean' ? detail.dirty : null
                const cpRows = isUnattached ? null : checkpointRows(cp, nowMs)
                const cpChanged = dirty === true && !!cp?.saved_at
                const cpAttn = cpChanged || (!!cdLevel && !!cp && !cp.saved_at)
                const untracked = Number.isFinite(detail?.variables?.untracked) ? detail.variables.untracked : 0
                // Resources > Environment
                const image = detail?.image || (inst.image_arn
                  ? { name: imageNameFromArn(inst.image_arn), version: inst.image_version ?? null, outdated: false }
                  : null)
                const imageText = imageLabel(image)
                // Base image of this image version (control plane, cached by the proxy); omitted when unknown.
                const base = detail?.image?.base || null
                const baseText = base?.name && base?.version ? `${base.name} · v${formatVersion(base.version)}` : null
                const pythonText = detail?.python ? `Python ${detail.python}` : null
                const pkgs = isUnattached ? null : detail?.packages
                // Every name the VM sent (it caps the list); "+N more" only past that cap.
                const pkgSum = pkgs ? packagesSummary(pkgs.names, pkgs.total, Infinity) : null
                const pkgText = pkgSum && pkgSum.shown.length
                  ? `${pkgSum.shown.join(', ')}${pkgSum.more ? ` +${pkgSum.more} more` : ''}`
                  : null
                const hasRuntime = !!(imageText || baseText || pythonText || pkgText || isUnattached)
                // Cost: flat rows (dot · label · qualifier / rate line / amount), then Total.
                const costRows = inst.cost ? vmCostRows(inst.cost, { memGb, rateLabels, formatDuration }) : null
                // Section heading toggle + body visibility for this card.
                const secOpen = (key) => !isSectionCollapsed(collapsedSections, id, key)
                const secTitle = (key, titleId, label) => (
                  <VmSectionToggle id={titleId} label={label} expanded={secOpen(key)}
                    controls={`vm-${key}-body-${id}`} onToggle={() => toggleVmSection(id, key)} />
                )
                return (
                <div className="vm-detail">
                  <VmDetailTabs vmId={id} tabs={detailTabs} selected={shownTab} onSelect={setVmDetailTab}>
                  {/* Overview: what this VM is and where it stands (cost: header pill + Cost tab) */}
                  <VmTabPanel tabId="overview">
                    <div className="vm-res-section vm-overview-notebook" role="group" aria-labelledby={`vm-notebook-title-${id}`}>
                      {secTitle('notebook', `vm-notebook-title-${id}`, 'Notebook')}
                      <div className="vm-section-body" id={`vm-notebook-body-${id}`} hidden={!secOpen('notebook')}>
                      <div className="vm-detail-row">
                        <span className="vm-detail-label">Name</span>
                        <span className={`vm-detail-value vm-detail-value-wrapline ${nbClass}`}>{nbText}{isClosedNotebookVm ? ' — closed' : ''}</span>
                      </div>
                      {attachedTab?.sessionId && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Session</span>
                          {/* Long id: CSS ellipsis + full value in the tooltip and the copy button. */}
                          <span className="vm-detail-value-wrap">
                            <code className="vm-detail-value vm-detail-value-ellipsis" title={attachedTab.sessionId}>{attachedTab.sessionId}</code>
                            <VmCopyButton value={attachedTab.sessionId} label="Copy session id" />
                          </span>
                        </div>
                      )}
                      {!attachedIds.includes(id) && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Status</span>
                          <span className="vm-detail-value">{state}</span>
                        </div>
                      )}
                      </div>
                    </div>

                    {hasLifecycle && (
                      <div className="vm-res-section vm-overview-lifecycle" role="group" aria-labelledby={`vm-lifecycle-title-${id}`}>
                        {secTitle('lifecycle', `vm-lifecycle-title-${id}`, 'Lifecycle')}
                        <div className="vm-section-body" id={`vm-lifecycle-body-${id}`} hidden={!secOpen('lifecycle')}>
                        {lifeRows.map(([label, value]) => (
                          <div key={label} className="vm-detail-row">
                            <span className="vm-detail-label">{label}</span>
                            <span className="vm-detail-value vm-detail-value-wrapline">{value}</span>
                          </div>
                        ))}
                        {idleText && (
                          <div className="vm-detail-row vm-idle-row">
                            <span className="vm-detail-label">Auto-suspend</span>
                            <span className="vm-detail-value vm-detail-value-wrapline">{idleText}</span>
                          </div>
                        )}
                        {showExpiry && (
                          <div className="vm-detail-row">
                            <span className="vm-detail-label">Terminates in</span>
                            <span className={`vm-detail-value vm-detail-value-wrapline ${cdLevel ? `vm-countdown-${cdLevel}` : ''}`}
                              title={`${formatCountdown(remainingSec)}${expiryEpoch ? ` (at ${formatClock(expiryEpoch)})` : ''}${countdownSuffix(cdLevel)}`}>
                              {formatCountdown(remainingSec)}
                              {expiryEpoch && remainingSec > 0 && <> <span className="vm-countdown-at">(at {formatClock(expiryEpoch)})</span></>}
                              {countdownSuffix(cdLevel)}
                            </span>
                          </div>
                        )}
                        </div>
                      </div>
                    )}

                    {cpRows && (
                      <div className="vm-res-section vm-overview-checkpoint" role="group" aria-labelledby={`vm-checkpoint-title-${id}`}>
                        {secTitle('checkpoint', `vm-checkpoint-title-${id}`, 'Checkpoint')}
                        <div className="vm-section-body" id={`vm-checkpoint-body-${id}`} hidden={!secOpen('checkpoint')}>
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Saved</span>
                          <span className={`vm-detail-value vm-detail-value-wrapline${cpAttn ? ' vm-state-attn' : ''}`}>
                            {cpRows.saved}{cpChanged ? ' · changed since' : ''}
                          </span>
                        </div>
                        {cpRows.variables && (
                          <div className="vm-detail-row">
                            <span className="vm-detail-label">Variables</span>
                            <span className="vm-detail-value vm-detail-value-wrapline">{cpRows.variables}</span>
                          </div>
                        )}
                        {cpRows.size && (
                          <div className="vm-detail-row">
                            <span className="vm-detail-label">Size</span>
                            <span className="vm-detail-value">{cpRows.size}</span>
                          </div>
                        )}
                        {cpRows.notSaved && (
                          <div className="vm-detail-row">
                            <span className="vm-detail-label">Not saved</span>
                            <span className="vm-detail-value vm-detail-value-wrapline vm-state-attn" title={cpRows.notSavedDetail}>
                              {cpRows.notSaved}
                            </span>
                          </div>
                        )}
                        {cpRows.suspendSave && (
                          <div className="vm-detail-row">
                            <span className="vm-detail-label">Last suspend save</span>
                            <span className="vm-detail-value vm-detail-value-wrapline vm-state-attn" title={cpRows.suspendSaveDetail}>
                              {cpRows.suspendSave}
                            </span>
                          </div>
                        )}
                        {untracked > 0 && (
                          <div className="vm-detail-row">
                            <span className="vm-detail-label">Untracked</span>
                            <span className="vm-detail-value">
                              <button
                                type="button"
                                className="vm-link-btn"
                                title="Open the Variables panel"
                                aria-label={`${untracked} untracked ${untracked === 1 ? 'variable' : 'variables'}, open the Variables panel`}
                                onClick={(e) => { e.stopPropagation(); window.dispatchEvent(new CustomEvent('toggle-sidebar-panel', { detail: 'variables' })) }}
                              >
                                {untracked}
                              </button>
                            </span>
                          </div>
                        )}
                        </div>
                      </div>
                    )}
                  </VmTabPanel>

                  {/* Resources: how hard it's working (History) and what it runs
                      (Environment). No live gauges here: the notebook's floating
                      "MicroVM · Live" bar shows live values. */}
                  <VmTabPanel tabId="resources">
                    {/* History chart grouped per run, from the proxy's DB (this VM only).
                        Fetched when this tab is shown, never on a timer. */}
                    <VmMetricsHistory
                      vmId={id}
                      active={shownTab === 'resources'}
                      isUnattached={isUnattached}
                      memoryMib={inst.memory_mib ?? null}
                      burstSeconds={inst.cost?.burst_seconds ?? null}
                      historyOpen={secOpen('history')}
                      lifetimeOpen={secOpen('lifetime')}
                      onToggleSection={(key) => toggleVmSection(id, key)}
                    />
                    <div className="vm-res-section vm-runtime" role="group" aria-labelledby={`vm-runtime-title-${id}`}>
                      {secTitle('environment', `vm-runtime-title-${id}`, 'Environment')}
                      <div className="vm-section-body" id={`vm-environment-body-${id}`} hidden={!secOpen('environment')}>
                      {detailPending && !imageText && (
                        <div className="vm-detail-row" aria-live="polite">
                          <span className="vm-detail-label">Loading…</span>
                        </div>
                      )}
                      {imageText && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Image</span>
                          <span className="vm-detail-value-wrap">
                            <span className="vm-detail-value vm-detail-value-id vm-detail-value-ellipsis" title={imageText}>{imageText}</span>
                            {image?.outdated && (
                              <span
                                className="vm-image-older"
                                title={`Built from image v${formatVersion(image.version)}; the current ${image.name} image is v${formatVersion(image.latest_version)}. New launches use v${formatVersion(image.latest_version)}.`}
                              >older image</span>
                            )}
                          </span>
                        </div>
                      )}
                      {baseText && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Base</span>
                          <span className="vm-detail-value vm-detail-value-id vm-detail-value-wrapline" title={base.arn || baseText}>{baseText}</span>
                        </div>
                      )}
                      {isUnattached && inst.architecture && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Architecture</span>
                          <span className="vm-detail-value"><ArchBadge arch={inst.architecture} /></span>
                        </div>
                      )}
                      {pythonText && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Python</span>
                          <span className="vm-detail-value">{detail.python}</span>
                        </div>
                      )}
                      {pkgText && (
                        <div className="vm-detail-row vm-detail-row-stacked">
                          <span className="vm-detail-label">User installed packages</span>
                          <ClampedList
                            text={pkgText}
                            count={Number.isFinite(pkgs.total) ? pkgs.total : (pkgs.names || []).length}
                            expanded={pkgListOpen.has(id)}
                            onToggle={() => togglePkgList(id)}
                          />
                        </div>
                      )}
                      {!detailPending && !hasRuntime && (
                        <div className="vm-detail-row">
                          <span className="vm-detail-label">Environment details unavailable</span>
                        </div>
                      )}
                      </div>
                    </div>
                  </VmTabPanel>

                  {/* Cost: what it has cost and why — flat rows, then the total */}
                  {costRows && (
                    <VmTabPanel tabId="cost">
                      {costRows.rows.map(r => (
                        <div key={r.key} className={`vm-cost-line vm-cost-line-${r.key}`}>
                          <span className="vm-cost-dot" aria-hidden="true" />
                          <div className="vm-cost-main">
                            <div className="vm-cost-label">{r.label}<span className="vm-cost-qual"> · {withSlashBreaks(r.qualifier)}</span></div>
                            <div className="vm-cost-rate">{r.rate}</div>
                          </div>
                          <span className="vm-cost-amount">{r.amount}</span>
                        </div>
                      ))}
                      <div className="vm-detail-row vm-detail-total">
                        <span className="vm-detail-label">Total</span>
                        <span className="vm-detail-value">{costRows.total}</span>
                      </div>
                    </VmTabPanel>
                  )}
                  </VmDetailTabs>

                  {/* Actions — outside the tabs, always visible whatever tab is selected */}
                  <div className="vm-detail-actions">
                    {state === 'RUNNING' && attachedIds.includes(id) && (
                      <>
                        {(() => {
                          const idlePct = (inst.idle_timeout_sec && inst.last_active && state === 'RUNNING')
                            ? Math.min(100, Math.max(0, ((Date.now() / 1000) - inst.last_active) / inst.idle_timeout_sec * 100))
                            : 0
                          return (
                            <button
                              className={`vm-action-btn vm-btn-suspend ${idlePct > 80 ? 'vm-btn-suspend-warn' : ''}`}
                              onClick={() => handleVmSuspend(id)}
                              disabled={isActioning}
                              style={idlePct > 0 ? { background: `linear-gradient(90deg, var(--accent-warning-subtle) ${idlePct}%, transparent ${idlePct}%)` } : {}}
                              title={idlePct > 0 ? `Idle: ${Math.round(idlePct)}% — auto-suspends in ${Math.max(0, Math.round(inst.idle_timeout_sec - ((Date.now() / 1000) - inst.last_active)))}s` : 'Suspend now'}
                            >
                              {isActioning ? <><span className="vm-btn-spinner" /> Suspending...</> : 'Suspend'}
                            </button>
                          )
                        })()}
                        <button className="vm-action-btn vm-btn-terminate" onClick={() => handleVmTerminateAndSave(id)} disabled={isActioning}>
                          {isActioning ? <><span className="vm-btn-spinner" /> {attachedTab?.checkpointEnabled ? 'Saving & terminating...' : 'Terminating...'}</> : (attachedTab?.checkpointEnabled ? 'Terminate & Save' : 'Terminate')}
                        </button>
                      </>
                    )}
                    {/* No Attach: a notebook connects only by restoring onto a NEW VM
                        or launching a NEW VM (Connect dialog), never by adopting this one. */}
                    {state === 'RUNNING' && !attachedIds.includes(id) && (
                      <button className="vm-action-btn vm-btn-terminate" onClick={() => handleVmTerminate(id)} disabled={isActioning}>
                        {isActioning ? <><span className="vm-btn-spinner" /> Terminating...</> : 'Terminate'}
                      </button>
                    )}
                    {state === 'SUSPENDED' && !attachedIds.includes(id) && (
                      <button className="vm-action-btn vm-btn-terminate" onClick={() => handleVmTerminate(id)} disabled={isActioning}>
                        {isActioning ? <><span className="vm-btn-spinner" /> Terminating...</> : 'Terminate'}
                      </button>
                    )}
                    {state === 'SUSPENDED' && attachedIds.includes(id) && (
                      <button className="vm-action-btn vm-btn-attach" onClick={async () => { await handleVmResume(id) }} disabled={isActioning}>
                        {isActioning ? <><span className="vm-btn-spinner" /> Resuming...</> : 'Resume'}
                      </button>
                    )}
                  </div>
                </div>
                )
              })()}
            </div>
            </div>
          )
  }

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Live MicroVMs</span>
        <span className="sidebar-panel-count">{Object.keys(vmInstances).length}</span>
        {persistenceMode && (
          <span className={`vm-mode-badge vm-mode-${persistenceMode}`}>
            {persistenceMode === 'eternal'
              ? '∞ eternal'
              : persistenceMode === 'checkpoint'
                ? '💾 checkpoint'
                : '○ none'}
          </span>
        )}
        <button className="sidebar-panel-action" onClick={async () => {
          // Refresh the source the panel actually RENDERS. `vmInstances` prefers
          // parentInstances (App's poll) whenever it's non-empty, so a local
          // fetchVmInstances() here would write to localInstances and be ignored
          // by the render — the refresh button would appear to do nothing (a
          // terminated VM would linger until App's next 15s poll tick). So when
          // the parent owns the data, trigger the PARENT's refetch; only fall
          // back to the local fetch when the parent isn't providing instances.
          setVmLoading(true)
          try {
            if (onRefreshInstances && parentInstances && Object.keys(parentInstances).length > 0) {
              await onRefreshInstances()
            } else {
              setVmFetched(false)
              await fetchVmInstances(false)
            }
          } finally {
            setVmLoading(false)
          }
        }} title="Refresh">
          <IconRefresh width={14} height={14} />
        </button>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      {/* Sandbox ready / failed (services/bannerStore.js); the click opens that notebook. */}
      <SlotBanner slot={SLOTS.MICROVMS} tabs={tabs} onSelectTab={onSelectTab} />
      <div className="sidebar-panel-body">
        {/* Overall cost (all notebooks) */}
        {Object.keys(vmInstances).length > 0 && (
          <div className="vm-total-cost-bar">
            <span className="vm-total-cost-label">
              {persistenceMode === 'eternal' ? 'Overall cost (all notebooks)' : 'Total cost'}
            </span>
            <span className="vm-total-cost-value">
              ${Object.values(vmInstances).filter(i => i.state !== 'TERMINATED' && i.state !== 'TERMINATING').reduce((sum, inst) => sum + (persistenceMode === 'eternal' ? (inst.session_cost?.total_cost_usd || inst.cost?.total_cost_usd || 0) : (inst.cost?.total_cost_usd || 0)), 0).toFixed(4)}
            </span>
          </div>
        )}
        {vmLoading && <PanelState variant="loading" message="Loading instances…" />}
        {!vmLoading && Object.keys(vmInstances).length === 0 && (
          <PanelState
            variant="empty"
            icon={<IconServer width={24} height={24} />}
            message="No MicroVM instances"
            hint="Launch one from a notebook to see it here."
          />
        )}
        {/* Sort control + grouped VM sections. Shown only when there are live VMs. */}
        {!vmLoading && (userVmEntries.length > 0 || poolSpareEntries.length > 0) && (
          <>
            <div className="vm-sort-bar">
              <label className="vm-sort-label" htmlFor="vm-sort-select">Sort</label>
              <select
                id="vm-sort-select"
                className="vm-sort-select"
                value={vmSort}
                onChange={(e) => setVmSort(e.target.value)}
                title="Sort MicroVM entries"
              >
                {VM_SORT_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              {/* Arch sort: show the two arch icons in the chosen order. Decorative;
                  the option text already says which arch comes first. */}
              {vmSort.startsWith('arch-') && (
                <span className="vm-sort-arch-order" aria-hidden="true">
                  {(vmSort === 'arch-x86_64' ? ['x86_64', 'arm64'] : ['arm64', 'x86_64']).map(a => (
                    <ArchBadge key={a} arch={a} iconOnly />
                  ))}
                </span>
              )}
            </div>

            {/* User / notebook VMs — attached VMs + closed-notebook VMs, rendered first. */}
            {userVmEntries.map(renderVmItem)}

            {/* Pool spares — collapsible, expanded by default. Hidden entirely when empty. */}
            {poolSpareEntries.length > 0 && (
              <div className="vm-pool-spares-section">
                <button
                  className="vm-pool-spares-header"
                  onClick={() => setPoolSparesCollapsed(c => !c)}
                  title={poolSparesCollapsed ? 'Expand pool spares' : 'Collapse pool spares'}
                >
                  {poolSparesCollapsed
                    ? <IconChevronRight width={12} height={12} />
                    : <IconChevronDown width={12} height={12} />}
                  <span className="vm-pool-spares-title">Pool spares</span>
                  <span className="vm-pool-spares-count">{poolSpareEntries.length}</span>
                </button>
                {!poolSparesCollapsed && poolSpareEntries.map(renderVmItem)}
              </div>
            )}
          </>
        )}
      </div>
      {/* Portaled to <body> (like VariablesPanel) so no sidebar ancestor clips the overlay. */}
      {terminateSaveFailure && createPortal(
        terminateSaveFailure.kind === 'kept' ? (
          <ConfirmModal
            title="Save failed"
            onCancel={() => setTerminateSaveFailure(null)}
            onConfirm={() => {
              const { id } = terminateSaveFailure
              setTerminateSaveFailure(null)
              handleVmTerminate(id, { withoutSaving: true })
            }}
            confirmLabel="Terminate without saving"
            cancelLabel="Keep running"
            confirmDanger
          >
            <p className="modal-message">{terminateSaveFailure.message}</p>
          </ConfirmModal>
        ) : (
          <ConfirmModal
            title="Save failed"
            onCancel={() => setTerminateSaveFailure(null)}
            onConfirm={() => setTerminateSaveFailure(null)}
            confirmLabel="OK"
            cancelLabel=""
          >
            <p className="modal-message">{terminateSaveFailure.message}</p>
          </ConfirmModal>
        ),
        document.body,
      )}
    </div>
  )
}

// Small icons for the VM card tabs, same size and stroke as the Run History metric
// tabs (Duration / CPU / Memory) so both tab controls look the same.
const VM_TAB_ICON_PROPS = { width: 11, height: 11, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2.5, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true, focusable: 'false' }
const VM_TAB_ICONS = {
  overview: <svg {...VM_TAB_ICON_PROPS}><circle cx="12" cy="12" r="9" /><line x1="12" y1="11" x2="12" y2="16" /><line x1="12" y1="8" x2="12.01" y2="8" /></svg>,
  cost: <svg {...VM_TAB_ICON_PROPS}><line x1="12" y1="2" x2="12" y2="22" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>,
  resources: <svg {...VM_TAB_ICON_PROPS}><rect x="6" y="6" width="12" height="12" rx="1.5" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" /></svg>,
}

/** Marker for one tab's content inside <VmDetailTabs>; rendered by VmDetailTabs. */
function VmTabPanel({ children }) {
  return children
}

/**
 * Tabs of the expanded VM card (WAI-ARIA tabs pattern, automatic activation):
 * role=tablist labelled with the VM id, role=tab buttons with aria-selected /
 * aria-controls and a roving tabindex, Left/Right (wrapping) + Home/End, and one
 * role=tabpanel per visible tab (non-selected ones `hidden`). `tabs` is the visible
 * list for this VM; children are <VmTabPanel tabId=…> (falsy children are skipped).
 * useId keeps the ids unique per card.
 */
function VmDetailTabs({ vmId, tabs, selected, onSelect, children }) {
  const baseId = useId()
  const tabRefs = useRef({})
  const panels = {}
  for (const child of Children.toArray(children)) {
    if (isValidElement(child) && child.props.tabId) panels[child.props.tabId] = child.props.children
  }
  const tabId = (t) => `${baseId}-tab-${t}`
  const panelId = (t) => `${baseId}-panel-${t}`
  const onKeyDown = (e) => {
    const current = tabs.findIndex(t => t.id === selected)
    const next = nextVmTabIndex(e.key, current < 0 ? 0 : current, tabs.length)
    if (next == null) return
    e.preventDefault()
    const t = tabs[next]
    onSelect(t.id)
    tabRefs.current[t.id]?.focus()
  }
  return (
    <>
      <div className="vm-detail-tabs" role="tablist" aria-label={`Details for ${vmId}`} onKeyDown={onKeyDown}>
        {tabs.map(t => {
          const isSel = t.id === selected
          return (
            <button
              key={t.id}
              ref={el => { tabRefs.current[t.id] = el }}
              type="button"
              role="tab"
              id={tabId(t.id)}
              className={`vm-detail-tab${isSel ? ' active' : ''}`}
              aria-selected={isSel}
              aria-controls={panelId(t.id)}
              tabIndex={isSel ? 0 : -1}
              onClick={() => onSelect(t.id)}
            >
              {VM_TAB_ICONS[t.id]}
              <span className="vm-detail-tab-label">{t.label}</span>
            </button>
          )
        })}
      </div>
      {tabs.map(t => (
        <div
          key={t.id}
          className="vm-detail-panel"
          role="tabpanel"
          id={panelId(t.id)}
          aria-labelledby={tabId(t.id)}
          tabIndex={0}
          hidden={t.id !== selected}
        >
          {panels[t.id]}
        </div>
      ))}
    </>
  )
}

/**
 * Small copy button for a long value shown with CSS ellipsis (e.g. the session id).
 * Copies the FULL value; a brief "Copied" state (same 1.5s feedback as the cell
 * output / error copy buttons), announced via a polite live region.
 */
function VmCopyButton({ value, label }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)
  useEffect(() => () => clearTimeout(timerRef.current), [])
  const copy = async (e) => {
    e.stopPropagation()
    try { await navigator.clipboard.writeText(value) } catch { return }
    setCopied(true)
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }
  return (
    <>
      <button
        type="button"
        className={`vm-copy-btn${copied ? ' is-copied' : ''}`}
        onClick={copy}
        aria-label={label}
        title={copied ? 'Copied' : label}
      >
        {copied
          ? <IconCheck width={11} height={11} aria-hidden="true" />
          : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>}
      </button>
      <span className="vm-copy-feedback" aria-live="polite">{copied ? 'Copied' : ''}</span>
    </>
  )
}
