import { useState, useEffect, useLayoutEffect, useCallback, useRef, useId, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { IconPlus, IconX, IconNotebook, IconArchive, IconTable, ArchBadge } from '../Icons'
import { RUN_HISTORY_LIMIT } from '../../config'
import { apiFetch } from '../../services/apiFetch'
import { findSessionVm, DEFAULT_MEMORY_MIB } from '../../constants'
import PanelState from './PanelState'
import PanelExpandButton from './PanelExpandButton'
import { fetchNotebooks } from '../../services/notebooks'
import { formatDurationShort } from '../../utils/formatDuration'
import { hasImageOutput } from '../../services/cellOutput'
import { archLabel } from '../../services/costRates'
import { nextVmTabIndex } from '../../services/vmDetailTabs'
import { takeRunHistoryReveal, takeExpandLatest, latestRunToOpen } from '../../services/runHistoryReveal'
import { SLOTS } from '../../services/actionBanner'
import SlotBanner from './SlotBanner'
import { selectRunCells, parseCellView, paramTipText, CELL_VIEW_STORAGE_KEY, TOP_N } from '../../services/runHistoryTop'

// Human label + short badge text for a run's trigger.
const TRIGGER_LABEL = { run_all: 'Run All', run_from: 'From cell', single: 'Cell', reactive: 'Dependents' }
// Run History per-cell metric tab → wording for the Top 10 toggle / footnote.
const METRIC_NAME = { duration: 'duration', cpu: 'CPU', memory: 'memory' }
// Per-cell metric tabs (11 px icons, same size as the VM card's tab icons).
const METRIC_TAB_ICON = { width: 11, height: 11, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2.5, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }
const METRIC_TABS = [
  { id: 'duration', label: 'Duration', icon: <svg {...METRIC_TAB_ICON}><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/></svg> },
  { id: 'cpu', label: 'CPU', icon: <svg {...METRIC_TAB_ICON}><rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg> },
  { id: 'memory', label: 'Memory', icon: <svg {...METRIC_TAB_ICON}><rect x="2" y="7" width="20" height="10" rx="1.5"/><line x1="7" y1="17" x2="7" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="17" y1="17" x2="17" y2="21"/></svg> },
]

// Status of a single run row, from its counts.
function runStatus(run) {
  if ((run.cells_interrupted || 0) > 0) return 'interrupted'
  if ((run.cells_errored || 0) > 0) return 'error'
  return 'success'
}

function RunStatusGlyph({ status }) {
  if (status === 'interrupted') return <span className="rh-status rh-status-interrupted" title="Stopped">◼</span>
  if (status === 'error') return <span className="rh-status rh-status-error" title="Had errors">✕</span>
  return <span className="rh-status rh-status-success" title="Completed">✓</span>
}

// Derive a per-notebook execution status from its persisted cells, so the
// sidebar shows at-a-glance progress for ALL notebooks (not just the active
// one) — lets you switch away and later see if another notebook finished.
// Precedence: running (something in flight) → error (a cell failed) →
// success (ran cleanly with output) → null (idle / never run).
function deriveExecStatus(cells) {
  if (!Array.isArray(cells) || cells.length === 0) return null
  let sawError = false
  let sawInterrupted = false
  let sawOutput = false
  for (const c of cells) {
    if (c.status === 'running') return 'running'
    if (c.status === 'error') sawError = true
    if (c.status === 'interrupted') sawInterrupted = true
    if (c.output || c.html || hasImageOutput(c)) sawOutput = true
  }
  // Precedence: running > interrupted > error > success. Interrupted ranks above
  // error so a user-stopped run reads as "stopped", not "failed".
  if (sawInterrupted) return 'interrupted'
  if (sawError) return 'error'
  if (sawOutput) return 'success'
  return null
}

// Per-notebook run summary from persisted cells — mirrors the Outline panel's
// summary bar (executed count + total execution time). executionTime is in ms.
function deriveRunStats(cells) {
  if (!Array.isArray(cells)) return { executedCount: 0, totalTime: 0, lastFinishedAt: 0 }
  let executedCount = 0
  let totalTime = 0
  let lastFinishedAt = 0
  for (const c of cells) {
    if (c.output || c.html || hasImageOutput(c) || c.error) executedCount++
    if (c.executionTime) totalTime += c.executionTime
    if (c.finishedAt && c.finishedAt > lastFinishedAt) lastFinishedAt = c.finishedAt
  }
  return { executedCount, totalTime, lastFinishedAt }
}

// Shared compact duration formatting (ms → ms/s/m), tiered so long runs read as
// "1m 36s" rather than a wall of milliseconds. Same helper the Outline + cell
// footer use, so run-history times match everywhere.
const formatRunTime = (ms) => formatDurationShort(ms)

// Compact byte size (B / KB / MB) for SQL result frames in the run drill-down.
function formatBytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

// Compact memory size from a MEGABYTE value (peak_mem_mb): 312.5 → "313 MB",
// 2048 → "2.0 GB". Distinct from formatBytes (which takes raw bytes).
function formatMb(mb) {
  if (mb == null) return ''
  if (mb < 1024) return `${Math.round(mb)} MB`
  return `${(mb / 1024).toFixed(1)} GB`
}

// Compact row count (1234 → 1.2K, 2100000 → 2.1M).
function formatRows(n) {
  if (n == null) return ''
  if (n < 1000) return `${n}`
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`
  return `${(n / 1_000_000).toFixed(1)}M`
}

// Compact one value for a param chip: strings verbatim, booleans as on/off,
// numbers trimmed. Keeps the chip short (US · h=45) while the tooltip carries
// the full label=value list.
function formatParamValue(v) {
  if (v === true) return 'on'
  if (v === false) return 'off'
  if (v == null) return ''
  return String(v)
}

// Accumulate @param values over a list of run_cells entries (each may carry a
// `params` map). Params are per-cell now, so the set "in scope" is just the
// union over whichever cells ran — Run All unions every cell, a single-cell
// scope passes one entry (often no params), a cascade passes the cells that
// ran. On a key collision the later cell (by notebook position) wins, matching
// Python execution order. Returns a stable {key,value} list + tooltip, or null.
function accumulateParams(cellEntries) {
  if (!Array.isArray(cellEntries) || cellEntries.length === 0) return null
  // Order by position so "later wins" is deterministic (entries are usually
  // already sorted, but don't rely on it).
  const ordered = [...cellEntries].sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
  const merged = {}
  for (const c of ordered) {
    const p = c && c.params
    if (p && typeof p === 'object') {
      for (const [k, v] of Object.entries(p)) merged[k] = v
    }
  }
  const entries = Object.entries(merged).map(([k, v]) => ({ key: k, value: formatParamValue(v) }))
  if (entries.length === 0) return null
  const full = entries.map(e => `${e.key} = ${e.value}`).join('\n')
  return { entries, full, count: entries.length }
}

// Every distinct @param (key, value) pair over a run's cells, in notebook order,
// for the run's "Parameters" section. Unlike accumulateParams (later cell wins),
// a key set to different values by two cells is listed once per value, so every
// param a cell row used to show as a chip is still listed here.
function distinctRunParams(cellEntries) {
  if (!Array.isArray(cellEntries) || cellEntries.length === 0) return null
  const ordered = [...cellEntries].sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
  const seen = new Set()
  const entries = []
  for (const c of ordered) {
    const p = c && c.params
    if (!p || typeof p !== 'object') continue
    for (const [k, v] of Object.entries(p)) {
      const value = formatParamValue(v)
      const id = `${k}\u0000${value}`
      if (seen.has(id)) continue
      seen.add(id)
      entries.push({ key: k, value })
    }
  }
  return entries.length ? entries : null
}

// The app's @param marker (same tag glyph as the run row's param badge).
function ParamMarkIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/>
      <line x1="7" y1="7" x2="7.01" y2="7"/>
    </svg>
  )
}

// Relative "time ago" for the last-run timestamp (epoch ms). Recomputed on each
// render; the sidebar re-renders often enough that this stays fresh in practice.
function formatAgo(ts) {
  if (!ts) return ''
  const secs = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (secs < 10) return 'just now'
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

function ExecStatusIndicator({ status }) {
  if (status === 'running') {
    return <span className="nb-exec-status nb-exec-running" title="Executing…"><span className="nb-exec-spinner" /></span>
  }
  if (status === 'interrupted') {
    return <span className="nb-exec-status nb-exec-interrupted" title="Last run was stopped" aria-label="stopped">◼</span>
  }
  if (status === 'error') {
    return <span className="nb-exec-status nb-exec-error" title="Last run had an error" aria-label="error">✕</span>
  }
  if (status === 'success') {
    return <span className="nb-exec-status nb-exec-success" title="Last run completed successfully" aria-label="success">✓</span>
  }
  return null
}

// Session-cost pill shown NEXT TO THE NOTEBOOK NAME (title row). Uses the
// server-returned total for the stable session id — not a live VM's cost — so it
// remains visible after a VM terminates or the session rotates to a replacement.
// Styling reuses the Live MicroVMs panel's green .vm-item-cost chip. The tooltip
// breaks the total down per-VM (state + short id + its own cost) using the
// server's vm_costs map — the same breakdown the endpoint returns for a future
// drill-down UI, surfaced here for free via the native title tooltip.
function NotebookCostPill({ sessionId, sessionCosts }) {
  const payload = sessionId ? sessionCosts?.[sessionId] : null
  const vmCount = payload?.total?.vm_count || 0
  const cost = payload?.total_cost_usd
  if (vmCount === 0 || cost == null) return null

  const vmCosts = payload?.vm_costs || {}
  const vmList = Object.values(vmCosts)
  const breakdown = vmList
    // Oldest first, so the list reads as the session's VM history in order.
    .sort((a, b) => (a.launched_at || '').localeCompare(b.launched_at || ''))
    .map(vm => {
      const shortId = String(vm.microvm_id || '').replace(/^microvm-/, '').slice(0, 8)
      const state = (vm.state || 'UNKNOWN')
      const vmCostUsd = vm.cost?.total_cost_usd
      return `  vm ${shortId} · ${state}${vmCostUsd != null ? ` · $${vmCostUsd.toFixed(4)}` : ''}`
    })
    .join('\n')

  // The Live MicroVMs panel only lists RUNNING/SUSPENDED instances — not
  // terminated ones. If every VM behind this session has since terminated,
  // there's nothing there to show, so don't offer a click-through (or the "open
  // Live MicroVMs" hint) for a cost that's entirely historical.
  const hasLiveVm = vmList.some(vm => {
    const state = (vm.state || '').toUpperCase()
    return state !== 'TERMINATED' && state !== 'TERMINATING'
  })

  const title = `Cumulative session cost: $${cost.toFixed(4)} across ${vmCount} VM${vmCount === 1 ? '' : 's'}`
    + (breakdown ? `\n${breakdown}` : '')
    + (hasLiveVm ? '\n\nClick to open Live MicroVMs panel' : '')

  return (
    <span
      className={`vm-item-cost sidebar-item-cost-pill${hasLiveVm ? '' : ' sidebar-item-cost-pill-static'}`}
      title={title}
      onClick={hasLiveVm ? (e) => {
        e.stopPropagation()
        try { window.dispatchEvent(new CustomEvent('toggle-sidebar-panel', { detail: 'microvms' })) } catch {}
      } : undefined}
    >
      ${cost.toFixed(4)}
    </span>
  )
}

// Connected-VM subline for a notebook row: "● <state> · vm <shortid>".
// Sourced from the live instances map (keyed by the tab's microvmId). Renders
// nothing when the notebook has no live VM (disconnected / never launched /
// terminated) so idle notebooks stay clean. The state dot reuses the same
// vm-state-<state> classes as the Live MicroVMs panel. (Cost moved to the name
// row as a pill; see NotebookCostPill.) Clicking it opens the Live MicroVMs panel.
function NotebookVmMeta({ microvmId, sessionId, instances }) {
  // Prefer the exact-id hit, but fall back to the SESSION's live VM. A reopened or
  // rotated tab can hold a stale microvmId while a perfectly-alive (often SUSPENDED)
  // VM for the same session sits in `instances` under a different key — keying only
  // on microvmId would drop the subline and grey the row for a healthy VM. Both
  // lookups are pure reads of the polled map, so this never wakes a suspended VM.
  let effId = microvmId
  let inst = microvmId && instances ? instances[microvmId] : null
  if (!inst && sessionId) {
    const sessionVm = findSessionVm(instances, sessionId)
    if (sessionVm) { [effId, inst] = sessionVm }
  }
  if (!inst) return null
  const state = (inst.state || 'UNKNOWN').toUpperCase()
  if (state === 'TERMINATED' || state === 'TERMINATING') return null
  const shortId = String(effId).replace(/^microvm-/, '').slice(0, 8)
  const stateLabel = state.charAt(0) + state.slice(1).toLowerCase()  // RUNNING → Running
  // Tier spec, mirroring the Live MicroVMs panel's order + formatting:
  // "<mem> GB · <vcpu> vCPU · <arch> · <state>". memory_mib/architecture come from
  // the same /instances entry, so this stays in lockstep with that panel.
  const memGb = (inst.memory_mib || DEFAULT_MEMORY_MIB) / 1024
  const memLabel = memGb % 1 === 0 ? memGb.toFixed(0) : String(memGb)
  const vcpuLabel = (memGb / 2).toFixed(1)  // 2 GB : 1 vCPU
  const archText = archLabel(inst.architecture)
  return (
    <button
      type="button"
      className="sidebar-item-vm"
      title={`MicroVM ${effId} · ${memLabel} GB · ${vcpuLabel} vCPU · ${archText} · ${stateLabel} — open Live MicroVMs panel`}
      onClick={(e) => {
        e.stopPropagation()
        try { window.dispatchEvent(new CustomEvent('toggle-sidebar-panel', { detail: 'microvms' })) } catch {}
      }}
    >
      <span className={`sidebar-item-vm-dot vm-state-${state.toLowerCase()}`} />
      <span className="sidebar-item-vm-id">vm {shortId}</span>
      <span className="sidebar-item-vm-sep">·</span>
      <span className="sidebar-item-vm-spec">{memLabel} GB</span>
      <span className="sidebar-item-vm-sep">·</span>
      <span className="sidebar-item-vm-spec">{vcpuLabel} vCPU</span>
      <span className="sidebar-item-vm-sep">·</span>
      <span className="sidebar-item-vm-spec"><ArchBadge arch={inst.architecture} /></span>
      <span className="sidebar-item-vm-sep">·</span>
      <span className="sidebar-item-vm-state">{stateLabel}</span>
    </button>
  )
}

// Run History for one notebook: a duration sparkline (last ~10 runs) + a
// reverse-chronological list. Mounted only when the notebook row is expanded,
// so history is fetched lazily. Refreshes when a run for THIS notebook is
// recorded (via the 'notebook-run-recorded' window event).
// "Parameters" list inside an expanded run (all-cells drill-down and the
// single-cell scoped view). Collapsed by default — a parameter-heavy notebook
// would otherwise push the run list far down; the header shows the count and
// toggles the label = value pills. State is per drill-down (resets when the run
// card is re-opened).
function RunParamsDetail({ entries }) {
  const [open, setOpen] = useState(false)
  if (!entries || entries.length === 0) return null
  return (
    <div className="rh-params-detail">
      <button
        type="button"
        className="rh-params-detail-title rh-params-detail-toggle"
        aria-expanded={open}
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
      >
        <svg className={`rh-chevron${open ? ' open' : ''}`} width={10} height={10} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6" /></svg>
        <span>Parameters</span>
        <span className="rh-params-detail-count">{entries.length}</span>
      </button>
      {open && (
        <div className="rh-params-detail-list">
          {entries.map(e => (
            <span key={`${e.key}\u0000${e.value}`} className="rh-param-kv">
              <span className="rh-param-kv-key">{e.key}</span>
              <span className="rh-param-kv-val">{e.value}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function RunHistory({ notebookId }) {
  const [runs, setRuns] = useState(null)  // null = loading, [] = none
  const [error, setError] = useState(false)
  const [runAllOnly, setRunAllOnly] = useState(false)  // "Run All" filter pill
  const [selectedRunId, setSelectedRunId] = useState(null)  // linked sparkline ↔ list
  const [expandedRunId, setExpandedRunId] = useState(null)  // per-cell drill-down open
  const [cellScope, setCellScope] = useState(null)  // null = all cells; else a cell_id
  // Per-run drill-down metric: run_id → 'duration' | 'cpu' | 'memory'. Each expanded
  // run card carries its own lens (default duration), so the per-cell bars can be
  // re-read as wall-time, peak CPU, or peak memory without affecting other cards.
  const [metricByRun, setMetricByRun] = useState({})
  const rowRefs = useRef({})  // run_id → row DOM node (for scroll-into-view on bar click)
  // Long runs (>15 cells): "Top 10" (default) or "All". One choice for every run
  // card, remembered for the browser session.
  const [cellView, setCellViewState] = useState(() => {
    try { return parseCellView(sessionStorage.getItem(CELL_VIEW_STORAGE_KEY)) } catch { return parseCellView(null) }
  })
  const setCellView = (v) => {
    setCellViewState(v)
    try { sessionStorage.setItem(CELL_VIEW_STORAGE_KEY, v) } catch { /* storage unavailable */ }
  }
  // Metric row of the expanded run: [tabs] [legend] [Top 10 / All →] on ONE row.
  // When all three don't fit but tabs + option do, the legend wraps below first
  // (data-legend="below"); otherwise normal flex wrapping. Re-checked on resize.
  const metricSelRef = useRef(null)
  useLayoutEffect(() => {
    const el = metricSelRef.current
    if (!el) return undefined
    const fit = () => {
      const tabs = el.querySelector('.rh-metric-tabs')
      const legend = el.querySelector('.rh-cell-legend')
      const opt = el.querySelector('.rh-view-options')
      const cs = getComputedStyle(el)
      const avail = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
      // Narrow panel: the full tab track (~238 px) doesn't fit, so step down
      // instead of clipping: "compact" = tighter padding and icon-only inactive
      // tabs (the active one keeps its label); "icons" = every tab icon-only.
      // Labels stay in the DOM (visually hidden) and in each tab's title.
      if (tabs) {
        delete tabs.dataset.fit
        // The track is capped at the row (max-width: 100%), so any overflow,
        // even a sub-pixel one, shows up as scrollWidth > clientWidth.
        const over = () => tabs.scrollWidth > tabs.clientWidth || tabs.scrollWidth > avail + 0.5
        if (over()) {
          tabs.dataset.fit = 'compact'
          if (over()) tabs.dataset.fit = 'icons'
        }
      }
      if (!tabs || !legend || !opt) { delete el.dataset.legend; return }
      const gap = parseFloat(cs.columnGap) || 0
      const items = Array.from(legend.children)
      const legendW = items.reduce((w, x) => w + x.getBoundingClientRect().width, 0)
        + (parseFloat(getComputedStyle(legend).columnGap) || 0) * Math.max(0, items.length - 1)
      const tabsOpt = tabs.scrollWidth + opt.scrollWidth + gap
      if (tabsOpt + legendW + gap > avail + 0.5 && tabsOpt <= avail + 0.5) el.dataset.legend = 'below'
      else delete el.dataset.legend
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  }, [expandedRunId, metricByRun, cellView, runs, cellScope])
  // Per-cell row tooltip (hover AND keyboard focus): { id, lines, left, top, rowTop }.
  // Rendered in a portal so the card's overflow can't clip it.
  const [cellTip, setCellTip] = useState(null)
  const cellTipRef = useRef(null)
  const tipBaseId = useId()
  const cellTipAnchor = useRef(null)
  const showCellTip = (el, id, lines) => {
    cellTipAnchor.current = el
    const r = el.getBoundingClientRect()
    setCellTip({ id, lines, left: r.left, top: r.bottom + 4, rowTop: r.top })
  }
  const hideCellTip = (id) => setCellTip(t => (t && (id == null || t.id === id) ? null : t))
  // Keep the tooltip inside the viewport: flip above the row near the bottom
  // edge, clamp to the right edge.
  useLayoutEffect(() => {
    const el = cellTipRef.current
    if (!el || !cellTip) return
    const h = el.offsetHeight
    const w = el.offsetWidth
    const top = cellTip.top + h > window.innerHeight - 4 ? Math.max(4, cellTip.rowTop - h - 4) : cellTip.top
    const left = Math.max(4, Math.min(cellTip.left, window.innerWidth - w - 4))
    el.style.top = `${top}px`
    el.style.left = `${left}px`
    el.style.visibility = 'visible'
  }, [cellTip])
  // A fixed-position tooltip would drift on scroll/resize: follow its row (e.g.
  // focus scrolled it into view), or close when the row is gone.
  const tipOpen = !!cellTip
  useEffect(() => {
    if (!tipOpen) return
    const follow = () => {
      const el = cellTipAnchor.current
      if (!el || !el.isConnected) { setCellTip(null); return }
      const r = el.getBoundingClientRect()
      setCellTip(t => (t ? { ...t, left: r.left, top: r.bottom + 4, rowTop: r.top } : t))
    }
    window.addEventListener('scroll', follow, true)
    window.addEventListener('resize', follow)
    return () => { window.removeEventListener('scroll', follow, true); window.removeEventListener('resize', follow) }
  }, [tipOpen])

  // Each fetch gets a sequence number; loadedSeq = the one whose data is shown, so
  // the banner's "open the newest run" request can wait for data fetched after it.
  const loadSeqRef = useRef(0)
  const [loadedSeq, setLoadedSeq] = useState(0)
  const load = useCallback(() => {
    const seq = ++loadSeqRef.current
    const q = `limit=${RUN_HISTORY_LIMIT}${runAllOnly ? '&trigger=run_all' : ''}`
    apiFetch(`/notebooks/${encodeURIComponent(notebookId)}/runs?${q}`)
      .then(r => r.json())
      .then(data => { setRuns(Array.isArray(data.runs) ? data.runs : []); setError(false); setLoadedSeq(seq) })
      .catch(() => { setError(true); setRuns([]) })
  }, [notebookId, runAllOnly])

  useEffect(() => {
    load()
    const onRecorded = (e) => { if (String(e.detail?.notebookId) === String(notebookId)) load() }
    window.addEventListener('notebook-run-recorded', onRecorded)
    return () => window.removeEventListener('notebook-run-recorded', onRecorded)
  }, [load, notebookId])

  // Run banner click (services/runHistoryReveal.js, expandLatest): open the newest
  // run's drill-down once data fetched after the click is in and its newest run is
  // the banner's run (latestRunToOpen). The request is taken on mount (the card is
  // mounted by the reveal; the mount fetch above is the fresh one) or from the event
  // when the card is already mounted (refetch). A manual row toggle drops it. Opening
  // the card by its chevron never parks a request, so nothing opens then.
  const expandReqRef = useRef(null)  // { stamp, trigger, afterSeq }
  const [scrollRunId, setScrollRunId] = useState(null)
  useEffect(() => {
    const arm = (refetch) => {
      const req = takeExpandLatest(notebookId)
      if (!req) return
      if (refetch) load()
      expandReqRef.current = { ...req, afterSeq: loadSeqRef.current }
    }
    arm(false)
    const onReveal = (e) => {
      if (e.detail?.expandLatest && String(e.detail?.notebookId) === String(notebookId)) arm(true)
    }
    window.addEventListener('reveal-run-history', onReveal)
    return () => window.removeEventListener('reveal-run-history', onReveal)
  }, [load, notebookId])
  useEffect(() => {
    const req = expandReqRef.current
    if (!req || !runs || loadedSeq < req.afterSeq) return
    const id = latestRunToOpen(runs, { stamp: req.stamp, cellScope, filtered: runAllOnly, trigger: req.trigger })
    if (id == null) return  // run not recorded yet: the notebook-run-recorded refetch retries
    expandReqRef.current = null
    setExpandedRunId(id)
    setScrollRunId(id)
  }, [loadedSeq, runs, cellScope, runAllOnly])
  useEffect(() => {
    if (!scrollRunId) return
    const el = rowRefs.current[scrollRunId]
    if (el) {
      const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches
      el.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' })
    }
    setScrollRunId(null)
  }, [scrollRunId])

  // Cell options for the scope selector: union of every cell that has run,
  // keyed by its PERMANENT cell_id (stable across reorders) with the latest label
  // seen. Runs are newest-first, so the first label encountered is the freshest.
  const cellOptions = (() => {
    const seen = new Map()  // cell_id → label
    for (const r of (runs || [])) {
      for (const c of (Array.isArray(r.run_cells) ? r.run_cells : [])) {
        if (c.cell_id && !seen.has(c.cell_id)) seen.set(c.cell_id, c.label || c.cell_id)
      }
    }
    // Sort by label for a predictable dropdown order (PY-1, PY-2, SQL-1…).
    return Array.from(seen.entries())
      .map(([id, label]) => ({ id, label }))
      .sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true }))
  })()
  const scopeLabel = cellScope ? (cellOptions.find(o => o.id === cellScope)?.label || 'cell') : 'All cells'

  // Header: title + cell-scope selector + "Run All" filter pill. Always shown so
  // controls stay reachable in loading/empty/error states.
  const header = (
    <div className="rh-card-title">
      <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" />
      </svg>
      RUN HISTORY
      {cellOptions.length > 0 && (
        <select
          className="rh-scope-select"
          value={cellScope || ''}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => { setCellScope(e.target.value || null); setExpandedRunId(null); setSelectedRunId(null) }}
          title="Scope run history to a single cell"
        >
          <option value="">All cells</option>
          {cellOptions.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      )}
      <button
        type="button"
        className={`outline-filter-chip rh-filter-pill${runAllOnly ? ' active' : ''}`}
        aria-pressed={runAllOnly}
        onClick={(e) => { e.stopPropagation(); setRunAllOnly(v => !v) }}
        title={runAllOnly ? 'Showing Run All only — click to show all' : 'Show only Run All runs'}
      >
        Run All
      </button>
    </div>
  )

  if (runs === null) return <div className="rh-card" onClick={(e) => e.stopPropagation()}>{header}<PanelState variant="loading" message="Loading run history…" /></div>
  if (error) return <div className="rh-card" onClick={(e) => e.stopPropagation()}>{header}<PanelState variant="error" message="Couldn’t load run history" action={{ label: 'Retry', onClick: load }} /></div>
  if (runs.length === 0) {
    // No history at all → hide the whole card (nothing useful to show). When the
    // "Run All" filter is active, keep the card + header so the pill stays
    // reachable to switch back — otherwise the user would be stranded in an
    // empty filtered view with no way to clear the filter.
    if (!runAllOnly) return null
    return <div className="rh-card" onClick={(e) => e.stopPropagation()}>{header}<div className="rh-empty">No Run All runs yet</div></div>
  }

  // Normalized SERIES the whole view renders from. In "All cells" mode each item
  // is a whole run (ms = total_exec_ms, status = run status). In single-cell mode
  // each item is THIS cell's slice of a run (ms/status from its run_cells entry);
  // runs where the cell didn't execute are dropped. Downstream code reads only
  // s.ms / s.status, so the sparkline/summary/trend/rows work for both scopes.
  const series = (cellScope
    ? runs.map(r => {
        const c = (Array.isArray(r.run_cells) ? r.run_cells : []).find(x => x.cell_id === cellScope)
        if (!c) return null  // this cell didn't run in this run → gap (dropped)
        return {
          run_id: r.run_id, trigger: r.trigger, finished_at: r.finished_at,
          ms: c.exec_ms || 0, status: c.status || 'success', run: r, cell: c,
        }
      }).filter(Boolean)
    : runs.map(r => ({
        run_id: r.run_id, trigger: r.trigger, finished_at: r.finished_at,
        ms: r.total_exec_ms || 0, status: runStatus(r), run: r,
      }))
  )

  // If a cell scope is selected but that cell has no runs in the window, show a
  // gentle empty state rather than a blank card.
  if (cellScope && series.length === 0) {
    return <div className="rh-card" onClick={(e) => e.stopPropagation()}>{header}<div className="rh-empty">No runs recorded for {scopeLabel} yet</div></div>
  }

  // Sparkline: covers EVERY run shown in the list below (same window, capped at
  // RUN_HISTORY_LIMIT), ordered NEWEST→OLDEST left→right so each bar lines up 1:1
  // with its row — no listed run is missing a bar. Bar height ∝ duration vs the
  // window max, LINEAR — so the bars show true proportions (a 2x-longer run is a
  // 2x-taller bar), rather than the compressed √ scale used before.
  const spark = series.slice(0, RUN_HISTORY_LIMIT)
  const maxMs = Math.max(1, ...spark.map(s => s.ms))
  const barHeight = (ms) =>
    `${Math.round((Math.max(0, ms || 0) / maxMs) * 100)}%`

  // Summary stats over the shown series (respects both the Run All filter and scope).
  const durations = series.map(s => s.ms)
  const avgMs = durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : 0
  const fastestMs = durations.length ? Math.min(...durations) : 0
  const slowestMs = durations.length ? Math.max(...durations) : 0
  const errorRuns = series.filter(s => s.status === 'error').length

  // Trend delta: latest run vs the MEDIAN of prior runs — shown ONLY when the
  // "Run All" filter is active, so we're comparing like-for-like (whole-notebook
  // runs). Median is robust to a one-off slow run (cold start / network blip).
  // Suppressed when: <2 runs (no baseline), the latest run errored (its time is
  // misleading — it may have bailed early), or the delta is tiny (< 5% → "≈ usual").
  const mean = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0)
  // Baseline is the MEAN of prior runs, matching the "avg" shown in the summary
  // strip (so "than usual" refers to the same measure the user sees). With ≥3
  // runs it's a real average → "than usual"; with exactly 2 it's just
  // latest-vs-previous → "vs previous". Below 2 runs there's nothing to compare.
  // Apples-to-apples when: the Run All filter is on (every run = whole notebook),
  // OR a single cell is scoped (every item = the same cell). In either case the
  // series is comparable, so the trend is meaningful.
  const trendComparable = runAllOnly || !!cellScope
  let trend = null  // { pct, dir: 'slower'|'faster'|'same', ref: 'usual'|'previous' }
  if (trendComparable && series.length >= 2 && series[0].status !== 'error') {
    const latest = series[0].ms
    const priors = series.slice(1).map(s => s.ms)
    const baseline = mean(priors)
    const ref = series.length >= 3 ? 'usual' : 'previous'
    if (baseline > 0) {
      const pct = Math.round(((latest - baseline) / baseline) * 100)
      if (Math.abs(pct) < 5) trend = { pct: 0, dir: 'same', ref }
      else trend = { pct: Math.abs(pct), dir: pct > 0 ? 'slower' : 'faster', ref }
    }
  }

  // Clicking a bar selects its run and scrolls the matching list row into view;
  // clicking/hovering a row selects the run so the corresponding bar highlights.
  const selectRun = (runId) => {
    setSelectedRunId(runId)
    const el = rowRefs.current[runId]
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }

  return (
    <div className="rh-card" onClick={(e) => e.stopPropagation()}>
      {header}

      {/* Summary strip — at-a-glance aggregates over the shown series (respects
          the Run All filter and the cell scope). */}
      <div className="rh-summary">
        {cellScope && <span className="rh-summary-scope">{scopeLabel}</span>}
        {/* When the window is full (server caps at RUN_HISTORY_LIMIT), say "last N
            runs" so the stats are understood as a recent-window summary, not the
            notebook's entire run history. Below the cap, just "N runs". */}
        {series.length >= RUN_HISTORY_LIMIT ? (
          <span className="rh-summary-item" title={`Showing the most recent ${RUN_HISTORY_LIMIT} runs`}>last <b>{RUN_HISTORY_LIMIT}</b> runs</span>
        ) : (
          <span className="rh-summary-item"><b>{series.length}</b> {series.length === 1 ? 'run' : 'runs'}</span>
        )}
        <span className="rh-summary-sep">·</span>
        <span className="rh-summary-item">avg <b>{formatRunTime(Math.round(avgMs))}</b></span>
        <span className="rh-summary-sep">·</span>
        <span className="rh-summary-item">fastest <b>{formatRunTime(fastestMs)}</b></span>
        <span className="rh-summary-sep">·</span>
        <span className="rh-summary-item">slowest <b>{formatRunTime(slowestMs)}</b></span>
        {errorRuns > 0 && (
          <>
            <span className="rh-summary-sep">·</span>
            <span className="rh-summary-item rh-summary-error"><b>{errorRuns}</b> {errorRuns === 1 ? 'error' : 'errors'}</span>
          </>
        )}
        {trend && (() => {
          const subject = cellScope ? `${scopeLabel}` : 'Latest Run All'
          const baseTxt = trend.ref === 'usual' ? 'the average of prior runs' : 'the previous run'
          return (
            <>
              <span className="rh-summary-sep">·</span>
              {trend.dir === 'same' ? (
                <span className="rh-summary-item rh-trend-same" title={`${subject} (latest) is within 5% of ${baseTxt}`}>
                  latest ≈ {trend.ref === 'usual' ? 'avg' : 'previous'}
                </span>
              ) : (
                <span
                  className={`rh-summary-item rh-trend-${trend.dir}`}
                  title={`${subject} (latest) is ${trend.pct}% ${trend.dir} than ${baseTxt}`}
                >
                  latest {trend.dir === 'slower' ? '▲' : '▼'} <b>{trend.pct}%</b> {trend.dir} than {trend.ref === 'usual' ? 'avg' : 'previous'}
                </span>
              )}
            </>
          )
        })()}
      </div>

      {spark.length > 1 && (
        <div className="rh-sparkline-wrap">
          {/* #7 fast/slow gradient hint: dim at the bottom (fast) → warm at the top
              (slow), giving bar HEIGHT an implicit scale without a full axis. */}
          <div className="rh-spark-plot" title={cellScope ? `${scopeLabel} durations (newest → oldest)` : 'Run durations (newest → oldest)'}>
            {/* #1 average reference line — drawn at the avg duration on the SAME
                linear scale the bars use, so "above/below normal" is readable at a
                glance. */}
            {avgMs > 0 && (
              <div
                className="rh-spark-avg"
                style={{ bottom: `${Math.round((Math.max(0, avgMs) / maxMs) * 100)}%` }}
                title={`Average ${formatRunTime(Math.round(avgMs))}`}
              />
            )}
            <div className="rh-sparkline">
              {spark.map((s) => {
                const isSel = s.run_id === selectedRunId
                // One bar per run, height ∝ its duration (linear). The compute vs
                // overhead breakdown is NOT painted here — it lives as a text line in
                // the expanded run's analysis callout, and the per-cell execution
                // graph is the drill-down bars below. This strip stays a simple
                // run-over-run duration trend.
                const deltaPct = avgMs > 0 ? Math.round(((s.ms - avgMs) / avgMs) * 100) : 0
                const deltaTxt = avgMs > 0 && Math.abs(deltaPct) >= 1
                  ? ` · ${deltaPct > 0 ? '+' : ''}${deltaPct}% vs avg`
                  : ''
                return (
                  <button
                    key={s.run_id}
                    type="button"
                    className={`rh-bar rh-bar-${s.status}${isSel ? ' rh-bar-selected' : ''}`}
                    style={{ height: barHeight(s.ms) }}
                    title={`${TRIGGER_LABEL[s.trigger] || s.trigger}${deltaTxt}`}
                    onClick={(e) => { e.stopPropagation(); selectRun(s.run_id) }}
                    onMouseEnter={() => {
                      setSelectedRunId(s.run_id)
                      // #5 flash the matching list row so the bar↔row link is obvious.
                      const el = rowRefs.current[s.run_id]
                      if (el) {
                        el.classList.remove('rh-row-flash')
                        void el.offsetWidth
                        el.classList.add('rh-row-flash')
                      }
                    }}
                  />
                )
              })}
            </div>
          </div>
        </div>
      )}
      <div className="rh-list">
        {series.map((s, idx) => {
          const r = s.run
          const st = s.status
          const ts = s.finished_at ? Date.parse(s.finished_at) : 0
          const isSel = s.run_id === selectedRunId
          // All-cells mode: drill-down breaks the run into its cells. Single-cell
          // scope: no per-cell breakdown, but the row can still expand to show
          // THIS cell's params + result shape (rows/cols/size) for that run.
          const cellsDetail = cellScope ? [] : (Array.isArray(r.run_cells) ? r.run_cells : [])
          // The scoped cell's own params + shape, for the single-cell drill-down.
          const scopedParams = cellScope ? accumulateParams(s.cell ? [s.cell] : []) : null
          const scopedHasShape = cellScope && s.cell && s.cell.rows != null
          const canExpand = cellScope
            ? Boolean(scopedParams || scopedHasShape)
            : cellsDetail.length > 0
          const isExpanded = s.run_id === expandedRunId
          // Which metric the per-cell bars represent for THIS run card (default
          // duration). duration = wall span (finished-started) shown as a two-part
          // series (compute = exec_ms, then overhead = the rest); cpu = peak_cpu_pct;
          // memory = peak_mem_mb. Single-segment for cpu/memory.
          const metric = metricByRun[s.run_id] || 'duration'
          // Per-cell COMPUTE (in-VM exec) and WALL (client-clock span from the cell's
          // started_at→finished_at). OVERHEAD = wall − compute (VM round-trip, queue
          // wait, streaming) clamped ≥ 0. Wall falls back to compute when the span is
          // missing/invalid (older runs), so overhead is just 0 there.
          const cellCompute = (c) => Math.max(0, c.exec_ms || 0)
          const cellWall = (c) => {
            const a = c.started_at ? Date.parse(c.started_at) : null
            const b = c.finished_at ? Date.parse(c.finished_at) : null
            const span = (a != null && b != null && b >= a) ? (b - a) : null
            return span != null ? Math.max(span, cellCompute(c)) : cellCompute(c)
          }
          const cellOverhead = (c) => Math.max(0, cellWall(c) - cellCompute(c))
          // The value that drives bar LENGTH for the active metric. For Duration this
          // is now WALL (compute + overhead) so the two-part bar reads as the full
          // time; the segments split it into compute vs overhead. CPU/Memory single.
          const metricValue = (c) =>
            metric === 'cpu' ? (c.peak_cpu_pct || 0)
            : metric === 'memory' ? (c.peak_mem_mb || 0)
            : cellWall(c)
          // Right-aligned label per metric. Duration shows WALL (the whole bar).
          const metricLabel = (c) =>
            metric === 'cpu' ? `${Math.round(c.peak_cpu_pct || 0)}%`
            : metric === 'memory' ? formatMb(c.peak_mem_mb)
            : formatRunTime(cellWall(c))
          // Bar scale = max of the active metric across the run's cells (linear).
          const cellMax = Math.max(1, ...cellsDetail.map(metricValue))
          // Highlight the two cells the callout names: SLOWEST (max exec_ms) and
          // HEAVIEST (max peak_mem_mb). Computed once here so both the callout and the
          // per-cell rows tag the same cells. Only meaningful with >1 cell.
          const slowestId = cellsDetail.length > 1
            ? cellsDetail.reduce((hi, c) => ((c.exec_ms || 0) > (hi?.exec_ms ?? -1) ? c : hi), null)?.cell_id
            : null
          const memCandidates = cellsDetail.filter(c => c.peak_mem_mb != null)
          const heaviestId = memCandidates.length > 1
            ? memCandidates.reduce((hi, c) => (c.peak_mem_mb > (hi?.peak_mem_mb ?? -1) ? c : hi), null)?.cell_id
            : null
          // Rows to draw: every cell, or (runs > 15 cells, "Top 10" view) the 10
          // highest for the active metric. Re-sorts when the metric tab changes.
          const pick = selectRunCells(cellsDetail, metricValue, cellView, c => c.status === 'error')
          // Fixed columns (same on every row, so bars and values line up), sized
          // from ALL the run's cells, not just the shown ones, so the bar doesn't
          // shift when switching Top 10 ↔ All: label + value widths in ch, and only
          // the marker columns some cell of the run uses.
          const cols = {
            lead: cellsDetail.some(c => ((c.status === 'error') && c.error) || c.cell_id === slowestId),
            param: cellsDetail.some(c => c.params && typeof c.params === 'object' && Object.keys(c.params).length > 0),
            table: cellsDetail.some(c => c.rows != null),
            heavy: cellsDetail.some(c => c.cell_id === heaviestId),
          }
          const rowVars = {
            '--rh-badge-w': `${Math.max(3, ...cellsDetail.map(c => String(c.label || c.type || 'cell').length))}ch`,
            '--rh-val-w': `${Math.max(3, ...cellsDetail.map(c => String(metricLabel(c)).length))}ch`,
          }
          // Per-row delta vs the PREVIOUS (older) item — shown when the series is
          // comparable (Run All filter or a single-cell scope). Suppressed for the
          // oldest row, when either item errored, or under a 5% change (noise).
          let rowDelta = null
          if (trendComparable) {
            const prev = series[idx + 1]
            if (prev && st !== 'error' && prev.status !== 'error' && prev.ms > 0) {
              const p = Math.round(((s.ms - prev.ms) / prev.ms) * 100)
              if (Math.abs(p) >= 5) rowDelta = { pct: Math.abs(p), dir: p > 0 ? 'slower' : 'faster' }
            }
          }
          return (
            <Fragment key={s.run_id}>
            <div
              ref={(el) => { rowRefs.current[s.run_id] = el }}
              className={`rh-row rh-row-${st}${isSel ? ' rh-row-selected' : ''}${canExpand ? ' rh-row-expandable' : ''}`}
              title={
                // In single-cell (filtered) mode the run row IS the cell's slice, so
                // lead its tooltip with the error when it failed (the whole-run "All
                // cells" view surfaces errors on the per-cell breakdown rows instead).
                (cellScope && st === 'error' && s.cell?.error)
                  ? `⚠ ${s.cell.error}${s.finished_at ? `\n\n${new Date(ts).toLocaleString()}` : ''}`
                  : (s.finished_at ? new Date(ts).toLocaleString() : '')
              }
              onMouseEnter={() => setSelectedRunId(s.run_id)}
              onClick={canExpand ? () => { expandReqRef.current = null; setExpandedRunId(isExpanded ? null : s.run_id) } : undefined}
            >
              <span className="rh-expand-caret">
                {canExpand && (
                  <svg className={`rh-chevron${isExpanded ? ' open' : ''}`} width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
                )}
              </span>
              <RunStatusGlyph status={st} />
              <span className={`rh-trigger rh-trigger-${r.trigger}`}>{TRIGGER_LABEL[r.trigger] || r.trigger}</span>
              {cellScope ? (
                <span className="rh-cells">{scopeLabel}{st === 'error' ? <span className="rh-note rh-note-error"> · error</span> : st === 'interrupted' ? <span className="rh-note"> · stopped</span> : ''}{st === 'error' && s.cell?.error && <span className="rh-cell-err" title={s.cell.error} aria-label="Cell error"> ⚠</span>}</span>
              ) : (
                <span className="rh-cells">
                  {r.cells_executed} {r.cells_executed === 1 ? 'cell' : 'cells'}
                  {(r.cells_interrupted || 0) > 0 && <span className="rh-note"> · stopped</span>}
                  {(r.cells_errored || 0) > 0 && <span className="rh-note rh-note-error"> · {r.cells_errored} err</span>}
                </span>
              )}
              {rowDelta && (
                <span
                  className={`rh-row-delta rh-trend-${rowDelta.dir}`}
                  title={`${rowDelta.pct}% ${rowDelta.dir} than the previous run`}
                >
                  {rowDelta.dir === 'slower' ? '▲' : '▼'}{rowDelta.pct}%
                </span>
              )}
              {(() => {
                // Param badge: a compact "N params in scope" marker. Params are
                // per-cell, so we accumulate over the cells in scope — every cell
                // in all-cells mode, or just the scoped cell's own entry. In a
                // single-cell scope a cell with no @param shows no badge, which is
                // now honest (before, the workbook-wide map wrongly showed params
                // on every cell). Values live in the tooltip + expanded drill-down.
                // Row order is: percent-change delta, then this param pill, then
                // the run time — so the pill sits between the % and the duration.
                const scopedCells = cellScope ? (s.cell ? [s.cell] : []) : (Array.isArray(r.run_cells) ? r.run_cells : [])
                // Counted the same way as the run's Parameters section: distinct
                // key = value pairs (a key two cells set differently counts twice).
                const pe = distinctRunParams(scopedCells)
                if (!pe) return null
                const pc = { count: pe.length, full: pe.map(e => `${e.key} = ${e.value}`).join('\n') }
                return (
                  <span
                    className="rh-param-badge"
                    title={`Parameters for this run:\n${pc.full}`}
                  >
                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/>
                      <line x1="7" y1="7" x2="7.01" y2="7"/>
                    </svg>
                    {pc.count}
                  </span>
                )
              })()}
              <span className="rh-dur">{formatRunTime(s.ms)}</span>
              <span className="rh-ago">{ts ? formatAgo(ts) : ''}</span>
            </div>
            {isExpanded && !cellScope && (
              <div className="rh-celldetail" style={rowVars} onClick={(e) => e.stopPropagation()}>
                {(() => {
                  // Analysis callout above the per-cell bars: (a) time budget —
                  // wall-clock vs compute vs overhead, so a run that spent most of
                  // its time WAITING (VM scheduling, per-cell round-trips) reads
                  // differently from one that was compute-bound; (b) critical path —
                  // the single slowest cell and its share of compute, plus the top-3
                  // share, so it's obvious where to optimize. All from data already
                  // on the run (total_exec_ms, started/finished, per-cell exec_ms).
                  const compute = r.total_exec_ms || 0
                  const a = r.started_at ? Date.parse(r.started_at) : null
                  const b = r.finished_at ? Date.parse(r.finished_at) : null
                  const wall = (a != null && b != null && b >= a) ? Math.max(b - a, compute) : null
                  const overhead = wall != null ? Math.max(0, wall - compute) : null
                  const overheadPct = wall && wall > 0 ? Math.round((overhead / wall) * 100) : 0
                  // Critical path: rank cells by exec_ms (successful timing basis).
                  const ranked = [...cellsDetail].sort((x, y) => (y.exec_ms || 0) - (x.exec_ms || 0))
                  const top = ranked[0]
                  // Resource peaks across the run: the max per-cell memory high-water
                  // mark (the run's peak footprint) and the single most memory-heavy
                  // cell (memory critical path — distinct from the SLOWEST cell).
                  const memCells = cellsDetail.filter(c => c.peak_mem_mb != null)
                  const runPeakMem = memCells.length ? Math.max(...memCells.map(c => c.peak_mem_mb)) : null
                  const memTop = memCells.length
                    ? memCells.reduce((hi, c) => (c.peak_mem_mb > (hi?.peak_mem_mb ?? -1) ? c : hi), null)
                    : null
                  return (
                    <div className="rh-analysis">
                      {wall != null && (
                        <div className="rh-analysis-budget" title="Wall-clock time = compute (sum of cell run times) + overhead (VM scheduling, per-cell round-trips, waiting). Peak = the run's highest per-cell memory high-water mark.">
                          <span className="rh-analysis-metric"><span className="rh-analysis-swatch rh-analysis-swatch-wall" />wall <b>{formatRunTime(Math.round(wall))}</b></span>
                          <span className="rh-analysis-metric"><span className="rh-analysis-swatch rh-analysis-swatch-compute" />compute <b>{formatRunTime(Math.round(compute))}</b></span>
                          <span className="rh-analysis-metric"><span className="rh-analysis-swatch rh-analysis-swatch-overhead" />overhead <b>{formatRunTime(Math.round(overhead))}</b>{overheadPct > 0 ? ` (${overheadPct}%)` : ''}</span>
                          {runPeakMem != null && (
                            <span className="rh-analysis-metric"><span className="rh-analysis-swatch rh-analysis-swatch-mem" />peak mem <b>{formatMb(runPeakMem)}</b></span>
                          )}
                        </div>
                      )}
                      {/* Slowest + heaviest cell on ONE line: where the run spent its
                          time and where its memory footprint peaked. The heaviest half
                          is shown only when it's a DIFFERENT cell than the slowest (else
                          it'd just repeat). No compute-share/top-3 noise. */}
                      {top && ranked.length > 1 && (top.exec_ms || 0) > 0 && (
                        <div className="rh-analysis-critical" title="Slowest cell (most compute time) and heaviest cell (highest peak memory).">
                          slowest: <b>{top.label || top.type || 'cell'}</b> <span className="rh-analysis-val">{formatRunTime(Math.round(top.exec_ms || 0))}</span>
                          {memTop && memCells.length > 1 && memTop.cell_id !== top.cell_id && (
                            <span className="rh-analysis-share"> · heaviest: <b className="rh-analysis-heaviest">{memTop.label || memTop.type || 'cell'}</b> <span className="rh-analysis-val">{formatMb(memTop.peak_mem_mb)}</span></span>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })()}
                {/* Metric selector for this run's per-cell bars. Duration bars show a
                    two-part series (compute + overhead); CPU/Memory are single bars. */}
                <div className="rh-metric-select" ref={metricSelRef} onClick={(e) => e.stopPropagation()}>
                  {/* Segmented tab control — the per-cell bars below re-chart to the
                      selected metric. Same track + pill look and keyboard model as the
                      VM card's Overview / Cost / Resources tabs (.vm-detail-tabs):
                      roving tabindex, ←/→/Home/End move and select. */}
                  <div
                    className="rh-metric-tabs"
                    role="tablist"
                    aria-label="Per-cell metric"
                    onKeyDown={(e) => {
                      const cur = METRIC_TABS.findIndex(t => t.id === metric)
                      const next = nextVmTabIndex(e.key, cur < 0 ? 0 : cur, METRIC_TABS.length)
                      if (next == null) return
                      e.preventDefault(); e.stopPropagation()
                      setMetricByRun(prev => ({ ...prev, [s.run_id]: METRIC_TABS[next].id }))
                      const btns = e.currentTarget.querySelectorAll('[role="tab"]')
                      if (btns[next]) btns[next].focus()
                    }}
                  >
                    {METRIC_TABS.map(t => (
                      <button
                        key={t.id}
                        type="button"
                        role="tab"
                        aria-selected={metric === t.id}
                        tabIndex={metric === t.id ? 0 : -1}
                        className={`rh-metric-tab${metric === t.id ? ' active' : ''}`}
                        onClick={(e) => { e.stopPropagation(); setMetricByRun(prev => ({ ...prev, [s.run_id]: t.id })) }}
                        title={`Show per-cell ${t.label.toLowerCase()}`}
                      >
                        {t.icon}
                        <span className="rh-metric-tab-label">{t.label}</span>
                      </button>
                    ))}
                  </div>
                  {/* Same row as the tabs: the Duration legend, then (long runs) the
                      "Top 10 / All" view OPTION, right-aligned. When the row is too
                      narrow the legend wraps below first (data-legend="below", set by
                      the layout effect), then the option. The option reuses the app's
                      option-chip pattern (.outline-filter-chip — the Outline's type
                      filters and this card's "Run All" pill), not the tab look, so it
                      doesn't read as a second tab group. Radio semantics, arrows. */}
                  {/* Legend for the two-part Duration bar (compute vs overhead).
                      Only for the duration metric; CPU/Memory are single bars. */}
                  {metric === 'duration' && (
                    <span className="rh-cell-legend">
                      <span className="rh-cell-legend-item"><span className="rh-analysis-swatch rh-analysis-swatch-compute" />compute</span>
                      <span className="rh-cell-legend-item"><span className="rh-analysis-swatch rh-analysis-swatch-overhead" />overhead</span>
                    </span>
                  )}
                  {pick.toggle && (() => {
                    const opts = [
                      { id: 'top', label: `Top ${TOP_N}`, title: `Show the ${TOP_N} cells with the highest ${METRIC_NAME[metric]}` },
                      { id: 'all', label: `All (${pick.total})`, title: `Show all ${pick.total} cells in notebook order` },
                    ]
                    return (
                      <div
                        className="rh-view-options"
                        role="radiogroup"
                        aria-label="Cells shown"
                        onKeyDown={(e) => {
                          const cur = opts.findIndex(o => o.id === cellView)
                          const k = e.key === 'ArrowDown' ? 'ArrowRight' : e.key === 'ArrowUp' ? 'ArrowLeft' : e.key
                          const next = nextVmTabIndex(k, cur < 0 ? 0 : cur, opts.length)
                          if (next == null) return
                          e.preventDefault(); e.stopPropagation()
                          setCellView(opts[next].id); hideCellTip()
                          const btns = e.currentTarget.querySelectorAll('[role="radio"]')
                          if (btns[next]) btns[next].focus()
                        }}
                      >
                        {opts.map(v => (
                          <button
                            key={v.id}
                            type="button"
                            role="radio"
                            aria-checked={cellView === v.id}
                            tabIndex={cellView === v.id ? 0 : -1}
                            className={`outline-filter-chip rh-view-option${cellView === v.id ? ' active' : ''}`}
                            onClick={(e) => { e.stopPropagation(); setCellView(v.id); hideCellTip() }}
                            title={v.title}
                          >
                            {v.label}
                          </button>
                        ))}
                      </div>
                    )
                  })()}
                </div>
                {pick.cells.map((c, i) => {
                  const cst = c.status || 'success'
                  const pct = Math.round((Math.max(0, metricValue(c)) / cellMax) * 100)
                  const label = c.label || c.type || 'cell'
                  const cp = accumulateParams([c])
                  const hasShape = c.rows != null
                  const isSlowest = c.cell_id === slowestId
                  const isHeaviest = c.cell_id === heaviestId
                  const tipId = `${tipBaseId}-${idx}-${i}`
                  // ONE line per cell. The details the row used to show as chips
                  // (@params, SQL result size) plus the value breakdown live in the
                  // row's tooltip (hover AND keyboard focus) and, for screen
                  // readers, in its aria-describedby text. Never cut.
                  const metricDetail =
                    metric === 'cpu' ? `Peak CPU: ${Math.round(c.peak_cpu_pct || 0)}% of the VM${(c.peak_cpu_pct || 0) > 30 ? ' · burst (multiple cores active)' : ''}`
                    : metric === 'memory' ? (c.peak_mem_mb != null ? `Peak memory: ${formatMb(c.peak_mem_mb)} (process RSS)` : 'No memory sample')
                    : `${formatRunTime(cellWall(c))} wall = ${formatRunTime(cellCompute(c))} compute + ${formatRunTime(cellOverhead(c))} overhead`
                  const lines = [
                    { text: `${label} · ${metricLabel(c)}`, kind: 'head' },
                    { text: metricDetail },
                    // A failed cell leads with its persisted error (survives reload).
                    (cst === 'error' && c.error) ? { text: `⚠ ${c.error}`, kind: 'error' } : null,
                    cst === 'interrupted' ? { text: 'Stopped' } : null,
                    cp ? { text: paramTipText(cp.entries), kind: 'params' } : null,
                    hasShape ? { text: `${c.rows.toLocaleString()}×${c.cols}${c.bytes != null ? ` · ${formatBytes(c.bytes)}` : ''}`, kind: 'table' } : null,
                    isSlowest ? { text: 'Slowest cell (most compute)' } : null,
                    isHeaviest ? { text: 'Heaviest cell (peak memory)' } : null,
                    c.cell_id ? { text: `View ${label}'s history`, kind: 'hint' } : null,
                  ].filter(Boolean)
                  const openTip = (e) => showCellTip(e.currentTarget, tipId, lines)
                  return (
                    <Fragment key={c.cell_id || i}>
                    <button
                      type="button"
                      className={`rh-cellrow rh-cellrow-${cst}${isSlowest ? ' rh-cellrow-slowest' : ''}${isHeaviest ? ' rh-cellrow-heaviest' : ''}`}
                      aria-describedby={tipId}
                      onMouseEnter={openTip}
                      onMouseLeave={() => hideCellTip(tipId)}
                      onFocus={openTip}
                      onBlur={() => hideCellTip(tipId)}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape' && cellTip && cellTip.id === tipId) { e.stopPropagation(); hideCellTip(tipId) }
                      }}
                      onClick={(e) => {
                        e.stopPropagation()
                        if (!c.cell_id) return
                        hideCellTip()
                        setCellScope(c.cell_id); setExpandedRunId(null); setSelectedRunId(null)
                        // Also scroll the notebook to this cell (top-aligned) so
                        // clicking a per-cell run row jumps you to the cell, not just
                        // filters history. run_cells[].cell_id is the notebook cell's
                        // stable id === its [data-cell-id]. No-ops if the cell was
                        // since deleted. Deferred a tick so the filter state settles.
                        setTimeout(() => {
                          const el = document.querySelector(`[data-cell-id="${c.cell_id}"]`)
                          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
                        }, 0)
                      }}
                    >
                      {/* badge · lead flag · bar · markers · value, on one fixed-height
                          line. The lead flag and the markers sit in fixed-width slots,
                          so the bar and the value line up on every row. */}
                      <span className="rh-cellrow-main">
                        <span className="rh-cell-badge">{label}</span>
                        {/* Lead flag slot (when any shown row needs it): ⚠ for a failed cell, else 🕒 on the slowest. */}
                        {cols.lead && (
                          <span className="rh-cell-lead" aria-hidden="true">
                            {(cst === 'error' && c.error)
                              ? <span className="rh-cell-err">⚠</span>
                              : isSlowest ? <span className="rh-cell-flag-slow">🕒</span> : null}
                          </span>
                        )}
                        <span className="rh-cell-bar-track">
                          {/* Duration = TWO segments (compute, then overhead) that
                              together span the cell's wall time; CPU/Memory = one
                              solid bar. All scaled vs the run max of the active
                              metric. For an errored cell the compute segment takes
                              the danger color (the bar still shows how far it got). */}
                          {metric === 'duration' ? (
                            <>
                              <span
                                className={`rh-cell-bar rh-cell-bar-compute rh-cell-bar-${cst}`}
                                style={{ width: `${Math.round((cellCompute(c) / cellMax) * 100)}%` }}
                              />
                              <span
                                className="rh-cell-bar rh-cell-bar-overhead"
                                style={{ width: `${Math.round((cellOverhead(c) / cellMax) * 100)}%` }}
                              />
                            </>
                          ) : (
                            <span
                              className={`rh-cell-bar rh-cell-bar-${cst}`}
                              style={{ width: `${pct}%` }}
                            />
                          )}
                        </span>
                        {/* Marker slot, fixed columns (only those some shown row uses): @params · table result · heaviest. */}
                        <span className="rh-cell-marks" aria-hidden="true">
                          {cols.param && <span className="rh-cell-mark rh-cell-mark-param">{cp && <ParamMarkIcon />}</span>}
                          {cols.table && <span className="rh-cell-mark rh-cell-mark-table">{hasShape && <IconTable width={9} height={9} strokeWidth={2.5} aria-hidden="true" />}</span>}
                          {cols.heavy && <span className="rh-cell-mark rh-cell-flag-heavy">{isHeaviest ? '▲' : ''}</span>}
                        </span>
                        {/* Right-pinned value for the active metric so all cells line
                            up in a column: wall time, peak CPU % or peak memory. */}
                        <span className="rh-cell-dur">{metricLabel(c)}</span>
                      </span>
                    </button>
                    <span id={tipId} hidden>{lines.map(l => l.text).join('. ')}</span>
                    </Fragment>
                  )
                })}
                {pick.hidden > 0 && (
                  <div className="rh-cell-more">
                    +{pick.hidden} more
                    {/* Top 10 ranks on the metric only, so say when a failed cell is out of view. */}
                    {pick.hiddenErrors > 0 && (
                      <span className="rh-cell-more-err"> ({pick.hiddenErrors} failed, see All)</span>
                    )}
                    {' '}· top {TOP_N} by {METRIC_NAME[metric]}
                  </div>
                )}
                {(() => {
                  // The run's parameters, listed ONCE here (the rows only carry a
                  // marker): every distinct label = value pair over its cells.
                  const entries = distinctRunParams(cellsDetail)
                  if (!entries) return null
                  return <RunParamsDetail entries={entries} />
                })()}
              </div>
            )}
            {isExpanded && cellScope && (
              <div className="rh-celldetail rh-celldetail-scoped" onClick={(e) => e.stopPropagation()}>
                {/* Result shape for THIS cell on THIS run (SQL cells) — the same
                    rows × cols · size shown in the all-cells drill-down, surfaced
                    here so a single-cell view can explain a slow run by its data. */}
                {scopedHasShape && (
                  <div className="rh-scoped-line">
                    <span className="rh-scoped-label">Result</span>
                    <span
                      className="rh-cell-shape"
                      title={`${s.cell.rows.toLocaleString()} rows × ${s.cell.cols} cols${s.cell.bytes != null ? ` · ${formatBytes(s.cell.bytes)} in memory` : ''}`}
                    >
                      {formatRows(s.cell.rows)}×{s.cell.cols}{s.cell.bytes != null ? ` · ${formatBytes(s.cell.bytes)}` : ''}
                    </span>
                  </div>
                )}
                {/* Peak resource use for this cell on this run (guest-sampled). */}
                {s.cell && (s.cell.peak_mem_mb != null || s.cell.peak_cpu_pct != null) && (
                  <div className="rh-scoped-line">
                    <span className="rh-scoped-label">Peak</span>
                    <span className="rh-cell-res">
                      {s.cell.peak_mem_mb != null && <span className="rh-cell-res-mem" title="Peak memory (process RSS)">{formatMb(s.cell.peak_mem_mb)}</span>}
                      {s.cell.peak_cpu_pct != null && <span className="rh-cell-res-cpu" title="Peak CPU (% of the VM, same as the VM panel)">{Math.round(s.cell.peak_cpu_pct)}%</span>}
                    </span>
                  </div>
                )}
                {/* Params that shaped this cell's run (declared or, for SQL, used
                    via {var}). Full label = value list, readable without hovering. */}
                {scopedParams && <RunParamsDetail entries={scopedParams.entries} />}
              </div>
            )}
            </Fragment>
          )
        })}
      </div>
      {/* Per-cell row tooltip. Visual only (aria-hidden): screen readers get the
          same text from each row's aria-describedby. Hidden until placed. */}
      {cellTip && createPortal(
        <div className="rh-cell-tip" ref={cellTipRef} aria-hidden="true" style={{ top: cellTip.top, left: cellTip.left, visibility: 'hidden' }}>
          {cellTip.lines.map((l, k) => (
            <div key={k} className={`rh-cell-tip-line${l.kind ? ` rh-cell-tip-${l.kind}` : ''}`}>
              {l.kind === 'params' && <span className="rh-cell-tip-icon"><ParamMarkIcon /></span>}
              {l.kind === 'table' && <span className="rh-cell-tip-icon"><IconTable width={9} height={9} strokeWidth={2.5} aria-hidden="true" /></span>}
              <span className="rh-cell-tip-text">{l.text}</span>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </div>
  )
}

// Run-summary subline for a notebook row: "Last run: N cells executed · Total: X · Y ago".
// Sourced from the notebook's LATEST run record (GET /runs?limit=1) rather than the
// live cells' finishedAt — run records carry an authoritative finished_at that
// survives reload/reopen, whereas cell.finishedAt is a live-only field dropped on
// persist, which is why the "ago" was missing for reopened / DB-loaded notebooks.
// Refreshes when a run for THIS notebook is recorded (the 'notebook-run-recorded'
// event, same signal RunHistory uses). Falls back to the cell-derived stats when
// there's no run record yet (e.g. the very first run before it's posted) so the
// subline never regresses.
function NotebookRunMeta({ notebookId, fallback }) {
  const [run, setRun] = useState(null)  // latest run record, or null = none/not-loaded

  const load = useCallback(() => {
    apiFetch(`/notebooks/${encodeURIComponent(notebookId)}/runs?limit=1`)
      .then(r => r.json())
      .then(data => {
        const runs = Array.isArray(data.runs) ? data.runs : []
        setRun(runs[0] || null)
      })
      .catch(() => { /* leave as-is; fallback subline still renders */ })
  }, [notebookId])

  useEffect(() => {
    load()
    const onRecorded = (e) => { if (String(e.detail?.notebookId) === String(notebookId)) load() }
    window.addEventListener('notebook-run-recorded', onRecorded)
    return () => window.removeEventListener('notebook-run-recorded', onRecorded)
  }, [load, notebookId])

  // Prefer the authoritative run record; fall back to cell-derived stats.
  const executedCount = run ? (run.cells_executed || 0) : fallback.executedCount
  const totalMs = run ? (run.total_exec_ms || 0) : fallback.totalTime
  const finishedMs = run && run.finished_at ? Date.parse(run.finished_at) : fallback.lastFinishedAt
  if (!executedCount) return null

  const ago = finishedMs ? formatAgo(finishedMs) : ''
  return (
    <div
      className="sidebar-item-subline"
      title={finishedMs ? `Last run: ${new Date(finishedMs).toLocaleString()}` : undefined}
    >
      Last run: {executedCount} {executedCount === 1 ? 'cell' : 'cells'} executed
      {totalMs > 0 && ` · Total: ${formatRunTime(totalMs)}`}
      {ago && ` · ${ago}`}
    </div>
  )
}

export default function NotebooksPanel({
  tabs,
  activeTabId,
  instances = {},
  sessionCosts = {},
  sessionCostTotal = null,
  onSelectTab,
  onNewNotebook,
  onCloseTab,
  onDeleteNotebook,
  onReopenNotebook,
  onRenameTab,
  onUpdateTabTag,
  onClearRunBanner,
  onClose,
  expanded,
  onToggleExpand,
}) {
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [editingTagId, setEditingTagId] = useState(null)
  const [editTagValue, setEditTagValue] = useState('')
  const [collapsedTags, setCollapsedTags] = useState({})
  // Ids currently being reopened — gives INSTANT click feedback (spinner + disabled)
  // so the user isn't left staring at an unchanged row while reopen's async work
  // (fetch + VM launch) runs, and can't rage-click a second copy open. The row
  // leaves the "closed" list once it's actually open, which clears this naturally;
  // a timeout is a safety net in case reopen fails and the row stays.
  const [reopeningIds, setReopeningIds] = useState(new Set())
  const handleReopen = useCallback((id) => {
    const sid = String(id)
    setReopeningIds(prev => {
      if (prev.has(sid)) return prev  // already reopening — ignore repeat clicks
      return new Set(prev).add(sid)
    })
    onReopenNotebook && onReopenNotebook(id)
    // Safety net: clear the flag after a while so a failed reopen doesn't leave the
    // row stuck in a spinner (the successful path clears it by leaving the list).
    setTimeout(() => {
      setReopeningIds(prev => {
        if (!prev.has(sid)) return prev
        const n = new Set(prev); n.delete(sid); return n
      })
    }, 15000)
  }, [onReopenNotebook])
  // Run History opens by default for the ACTIVE notebook; syncs as you switch
  // notebooks. Users can still manually collapse/expand any notebook's history.
  const [expandedHistoryId, setExpandedHistoryId] = useState(activeTabId)
  useEffect(() => { setExpandedHistoryId(activeTabId) }, [activeTabId])
  // Expanding a notebook's history on purpose (toggle or row click) counts as
  // "opening the Run History": the Outline run banner for that notebook goes away.
  // The default-open state and a hover peek do not, so they never clear it.
  const openHistory = (tabId) => { setExpandedHistoryId(tabId); if (onClearRunBanner) onClearRunBanner(tabId) }
  // Reveal request from the Outline run banner: open this notebook's history and
  // scroll its row (the card sits right below it) into view. Taken once on mount
  // (the panel is mounted by the panel switch) or via the event if already mounted.
  // Both paths bump the scroll counter and the effect scrolls after the render.
  const [revealTick, setRevealTick] = useState(0)
  const revealIdRef = useRef(null)
  useEffect(() => {
    const go = (id) => { revealIdRef.current = String(id); setExpandedHistoryId(id); setRevealTick(n => n + 1) }
    const parked = takeRunHistoryReveal()
    if (parked) go(parked)
    const onReveal = (e) => { const id = takeRunHistoryReveal() || e.detail?.notebookId; if (id) go(id) }
    window.addEventListener('reveal-run-history', onReveal)
    return () => window.removeEventListener('reveal-run-history', onReveal)
  }, [])
  useEffect(() => {
    const id = revealIdRef.current
    if (!id) return
    const row = document.querySelector(`[data-nb-row="${CSS.escape(id)}"]`)
    if (row) { row.scrollIntoView({ block: 'start', behavior: 'auto' }); revealIdRef.current = null }
  }, [revealTick, expandedHistoryId])

  // All notebooks known to the DB — used to surface CLOSED notebooks (persisted
  // but not currently open in a tab) so they can be reopened. Refreshed on mount,
  // whenever the set of open tabs changes (a close/delete/open shifts what's
  // "closed"), and on version saves (a brand-new notebook first appears here).
  const [dbNotebooks, setDbNotebooks] = useState([])
  const openIds = new Set(tabs.map(t => String(t.id)))
  const loadDb = useCallback(() => {
    fetchNotebooks().then(list => { if (Array.isArray(list)) setDbNotebooks(list) })
  }, [])
  useEffect(() => { loadDb() }, [loadDb, tabs.length])
  useEffect(() => {
    const onSaved = () => loadDb()
    // 'notebook-deleted' fires from App.confirmDeleteNotebook. Deleting a CLOSED
    // notebook doesn't change tabs.length and emits no version-saved event, so
    // this is the only trigger that refreshes the closed list after a delete.
    // Optimistically drop the row right away (so it vanishes on the click), then
    // re-fetch to reconcile with the server.
    const onDeleted = (e) => {
      const gone = e.detail?.notebookId != null ? String(e.detail.notebookId) : null
      if (gone) setDbNotebooks(prev => prev.filter(nb => String(nb.id) !== gone))
      loadDb()
    }
    window.addEventListener('notebook-version-saved', onSaved)
    window.addEventListener('notebook-deleted', onDeleted)
    return () => {
      window.removeEventListener('notebook-version-saved', onSaved)
      window.removeEventListener('notebook-deleted', onDeleted)
    }
  }, [loadDb])
  // Closed = in the DB but not open in a tab. Shape them like a minimal tab so the
  // grouping-by-tag logic can render them alongside open tabs.
  const closedNotebooks = dbNotebooks
    .filter(nb => !openIds.has(String(nb.id)))
    .map(nb => ({
      id: nb.id,
      name: nb.name || `Notebook ${nb.id}`,
      description: nb.description || '',
      tag: nb.tag || 'Drafts',
      _closed: true,
      _cellCount: Array.isArray(nb.cells) ? nb.cells.length : (nb.cell_count ?? 0),
    }))

  const startRename = (e, tab) => {
    e.stopPropagation()
    setEditingId(tab.id)
    setEditValue(tab.name)
  }

  const commitRename = () => {
    if (editingId && editValue.trim()) {
      onRenameTab(editingId, editValue.trim())
    }
    setEditingId(null)
  }

  // Server-calculated aggregate for every open session. The bulk endpoint returns
  // this total directly; do NOT derive or sum it in the browser.
  const totalCost = sessionCostTotal

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Notebooks</span>
        <button
          className="sidebar-panel-action"
          onClick={onNewNotebook}
          title="New notebook"
        >
          <IconPlus width={14} height={14} />
        </button>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      {/* Sandbox ready / failed and import finished (services/bannerStore.js). */}
      <SlotBanner slot={SLOTS.NOTEBOOKS} tabs={tabs} onSelectTab={onSelectTab} />
      <div className="sidebar-panel-body">
        {/* Server-calculated total across open notebook sessions. The new bulk
            endpoint returns this aggregate; no notebook/VM cost is summed here. */}
        {(totalCost?.vm_count || 0) > 0 && (
          <div
            className="vm-total-cost-bar sidebar-total-cost-bar"
            title="Cumulative session cost across your open notebooks — open Live MicroVMs panel"
            onClick={() => { try { window.dispatchEvent(new CustomEvent('toggle-sidebar-panel', { detail: 'microvms' })) } catch {} }}
          >
            <span className="vm-total-cost-label">
              Total cost{totalCost.session_count > 1 ? ` · ${totalCost.session_count} notebooks` : ''}
            </span>
            <span className="vm-total-cost-value">${totalCost.total_cost_usd.toFixed(4)}</span>
          </div>
        )}
        {tabs.length === 0 && closedNotebooks.length === 0 && (
          <PanelState
            variant="empty"
            icon={<IconNotebook width={24} height={24} />}
            message="No notebooks yet"
            hint="Create a notebook or load a sample to get started."
          />
        )}
        {(() => {
          // Group OPEN tabs + CLOSED (DB-only) notebooks by tag so both show in the
          // same list. Open tabs keep all their rich state; closed ones are lighter
          // rows that can be reopened.
          const groups = {}
          ;[...tabs, ...closedNotebooks].forEach(tab => {
            const tag = tab.tag || 'Untitled'
            if (!groups[tag]) groups[tag] = []
            groups[tag].push(tab)
          })
          const tagOrder = Object.keys(groups).sort((a, b) => {
            // Drafts first, Samples last, rest alphabetical
            if (a === 'Drafts') return -1
            if (b === 'Drafts') return 1
            if (a === 'Samples') return 1
            if (b === 'Samples') return -1
            return a.localeCompare(b)
          })
          return tagOrder.map(tag => (
            <div key={tag} className="nb-tag-group">
              <div
                className="nb-tag-header"
                onClick={() => setCollapsedTags(prev => ({ ...prev, [tag]: !prev[tag] }))}
              >
                <span className="nb-tag-chevron">{collapsedTags[tag] ? <svg width={16} height={16} viewBox="0 0 24 24" fill="currentColor"><path d="M9 6l6 6-6 6z"/></svg> : <svg width={16} height={16} viewBox="0 0 24 24" fill="currentColor"><path d="M6 9l6 6 6-6z"/></svg>}</span>
                <svg className="nb-tag-icon" width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/>
                </svg>
                <span className="nb-tag-name">{tag}</span>
                {tag === 'Drafts' && (
                  <button
                    className="nb-tag-autotag-btn"
                    onClick={(e) => {
                      e.stopPropagation()
                      groups[tag].forEach(t => {
                        const cellData = (t._cells || []).slice(0, 4).map(c => ({ type: c.type || 'code', code: (c.code || '').slice(0, 200) }))
                        if (cellData.length >= 2) {
                          apiFetch(`/ai/suggest-tag`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ name: t.name, description: t.description || '', cells: cellData }),
                          })
                            .then(r => r.json())
                            .then(data => { if (data.tag && data.tag !== 'Drafts') onUpdateTabTag(t.id, data.tag) })
                            .catch(() => {})
                        }
                      })
                    }}
                    title="Auto-tag all drafts with AI"
                  >
                    <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
                  </button>
                )}
                <span className="nb-tag-count">{groups[tag].length}</span>
              </div>
              {!collapsedTags[tag] && groups[tag].map(tab => {
                // CLOSED (DB-only) notebook: a light row that reopens on click,
                // with a Delete action. No run history / exec status / rename.
                if (tab._closed) {
                  const isReopening = reopeningIds.has(String(tab.id))
                  return (
                    <div
                      key={tab.id}
                      className={`sidebar-item sidebar-item-closed nb-row-closed ${isReopening ? 'sidebar-item-reopening' : ''}`}
                      onClick={() => { if (!isReopening) handleReopen(tab.id) }}
                      title={isReopening ? `${tab.name} — reopening…` : `${tab.description || tab.name} — click to reopen`}
                      aria-busy={isReopening}
                    >
                      <span className="nb-history-toggle nb-closed-spacer" aria-hidden="true" />
                      {isReopening
                        ? <span className="sidebar-item-spinner" aria-hidden="true" />
                        : <IconArchive width={12} height={12} className="sidebar-item-archive-icon" aria-hidden="true" />}
                      <div className="sidebar-item-main">
                        <div className="sidebar-item-titlerow">
                          <span className="sidebar-item-label sidebar-item-label-closed">{tab.name}</span>
                          {!isReopening && <span className="sidebar-item-closed-badge">Archived</span>}
                        </div>
                        <div className="sidebar-item-subline">
                          {isReopening
                            ? 'Reopening…'
                            : `Closed · ${tab._cellCount} ${tab._cellCount === 1 ? 'cell' : 'cells'} · click to reopen`}
                        </div>
                      </div>
                      <button
                        className="sidebar-item-reopen"
                        onClick={(e) => { e.stopPropagation(); if (!isReopening) handleReopen(tab.id) }}
                        title="Reopen this notebook"
                        disabled={isReopening}
                      >
                        {isReopening ? 'Reopening…' : 'Reopen'}
                      </button>
                      <button
                        className="sidebar-item-delete"
                        onClick={(e) => { e.stopPropagation(); onDeleteNotebook && onDeleteNotebook(tab.id, tab.name) }}
                        title="Delete permanently (removes cells + version history)"
                        aria-label="Delete notebook permanently"
                        disabled={isReopening}
                      >
                        <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                      </button>
                    </div>
                  )
                }
                const historyOpen = expandedHistoryId === tab.id
                return (
                <Fragment key={tab.id}>
                <div
                  className={`sidebar-item ${tab.id === activeTabId ? 'sidebar-item-active' : ''}`}
                  data-nb-row={tab.id}
                  onClick={(e) => {
                    onSelectTab(tab.id)
                    // The whole row is the target: it opens the notebook AND its run
                    // history (a second click on the active row folds it again).
                    // Skipped for the 2nd click of a double-click (rename) and while
                    // the rename input itself is clicked.
                    if (e.detail > 1 || editingId === tab.id) return
                    // Switching TO another notebook never clears its run banner; only
                    // expanding the already-active notebook's history does.
                    if (tab.id === activeTabId && historyOpen) setExpandedHistoryId(null)
                    else if (tab.id === activeTabId) openHistory(tab.id)
                    else setExpandedHistoryId(tab.id)
                  }}
                  onDoubleClick={(e) => startRename(e, tab)}
                  title={tab.description || ''}
                >
                  {/* Run History disclosure — toggles the per-notebook history below. */}
                  <button
                    className="nb-history-toggle"
                    onClick={(e) => { e.stopPropagation(); if (historyOpen) setExpandedHistoryId(null); else openHistory(tab.id) }}
                    title={historyOpen ? 'Hide run history' : 'Show run history'}
                    aria-label="Toggle run history"
                  >
                    {historyOpen
                      ? <svg width={16} height={16} viewBox="0 0 24 24" fill="currentColor"><path d="M6 9l6 6 6-6z"/></svg>
                      : <svg width={16} height={16} viewBox="0 0 24 24" fill="currentColor"><path d="M9 6l6 6-6 6z"/></svg>}
                  </button>
                  <span className={`sidebar-item-dot sidebar-dot-${tab.status}`} />
                  <div className="sidebar-item-main">
                    <div className="sidebar-item-titlerow">
                      {editingId === tab.id ? (
                        <input
                          className="sidebar-rename-input"
                          value={editValue}
                          onChange={(e) => setEditValue(e.target.value)}
                          onBlur={commitRename}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') commitRename()
                            if (e.key === 'Escape') setEditingId(null)
                          }}
                          onClick={(e) => e.stopPropagation()}
                          autoFocus
                        />
                      ) : (
                        <span className="sidebar-item-label">{tab.name}</span>
                      )}
                      {/* Session cost pill — sits with the NAME (title row), styled
                          like the MicroVMs panel cost pill. Server-returned total for
                          the stable session, never a current-VM cost. */}
                      <NotebookCostPill sessionId={tab.sessionId} sessionCosts={sessionCosts} />
                    </div>
                    {/* Run summary subline — sourced from the notebook's latest run
                        record (reload-safe finished_at) with cell-derived stats as a
                        fallback. Mirrors the Outline summary bar. */}
                    <NotebookRunMeta notebookId={tab.id} fallback={deriveRunStats(tab._cells)} />
                    {/* Connected-VM subline (only when this notebook has a live VM):
                        state · short id. Cost intentionally lives in the title-row
                        pill, sourced from the durable server session-cost endpoint. */}
                    <NotebookVmMeta microvmId={tab.microvmId} sessionId={tab.sessionId} instances={instances} />
                  </div>
                  {/* Per-notebook execution status (spinner / ✓ / ✕ / ◼) — kept as a
                      direct row child so it vertically centers with the # and × controls. */}
                  <ExecStatusIndicator status={deriveExecStatus(tab._cells)} />
                  {/* Tag edit */}
                  {editingTagId === tab.id ? (
                    <input
                      className="sidebar-tag-input"
                      value={editTagValue}
                      onChange={(e) => setEditTagValue(e.target.value)}
                      onBlur={() => { if (editTagValue.trim()) onUpdateTabTag(tab.id, editTagValue.trim()); setEditingTagId(null) }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') { if (editTagValue.trim()) onUpdateTabTag(tab.id, editTagValue.trim()); setEditingTagId(null) }
                        if (e.key === 'Escape') setEditingTagId(null)
                      }}
                      onClick={(e) => e.stopPropagation()}
                      autoFocus
                      placeholder="Tag name"
                    />
                  ) : (
                    <button
                      className="sidebar-tag-btn"
                      onClick={(e) => { e.stopPropagation(); setEditingTagId(tab.id); setEditTagValue(tab.tag || '') }}
                      title="Change tag"
                    >
                      #
                    </button>
                  )}
                  <button
                    className="sidebar-item-delete"
                    onClick={(e) => { e.stopPropagation(); onDeleteNotebook && onDeleteNotebook(tab.id, tab.name) }}
                    title="Delete permanently (removes cells + version history)"
                    aria-label="Delete notebook permanently"
                  >
                    <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                  </button>
                  <button
                    className="sidebar-item-close"
                    onClick={(e) => { e.stopPropagation(); onCloseTab(tab.id) }}
                    title="Close (keeps it in the database — reopen anytime)"
                  >
                    <IconX width={12} height={12} />
                  </button>
                </div>
                {historyOpen && <RunHistory notebookId={tab.id} />}
                </Fragment>
                )
              })}
            </div>
          ))
        })()}
      </div>
    </div>
  )
}
