import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { IconX, IconLineage, IconSparkles, IconAlertTriangle } from './Icons'
import { apiFetch } from '../services/apiFetch'
import { buildExplainPayload, relativeTime, sourceStepLabels, groupDependencyIssues, lineageStatusText } from '../services/lineage.js'
import { deriveDisplayVar } from '../services/appViewModel'
import { highlightBasic } from '../services/basicHighlight'
import { isModalOpen } from '../services/modalLayer.js'
import './LineageDrawer.css'

/**
 * LineageDrawer — "How was this computed?" for one cell's output.
 *
 * A non-modal-looking (no scrim) but focus-trapped right drawer, portalled to
 * document.body like VersionHistoryDrawer. Sections: Summary (on-demand AI
 * one-liner lead card; the trust pill and status line live in the header, the trust
 * reasons sit under the card), Data sources, Dependency notes (the stored DAG's issues
 * for the steps, as callouts, warnings first), Inputs, and the Lineage container: one
 * timeline rail with the source, the Steps (upstream cells in lineage order with
 * read-only code, the drawer's own cell highlighted) and Used by (transitive downstream
 * cells, in notebook order, same node cards), then a Freshness footer. All lineage data
 * comes pre-computed from services/lineage.js (buildLineage).
 */

const TRUST_LABEL = { green: 'Trusted', amber: 'Review', red: 'Upstream error' }
// Dependency-note callouts: a bold effect line per issue code (the stored message keeps the
// detail). 'warning' = amber callout; 'info' = accent callout. Amber-severity issues are
// always warnings; external_state (a source the cell won't re-run for) is a warning too.
const ISSUE_HEAD = {
  external_state: { head: "Won't re-run when the source changes", tone: 'warning' },
  missing_producer: { head: 'Variable has no producing cell', tone: 'warning' },
  dynamic_names: { head: 'Reads and writes may be incomplete', tone: 'warning' },
  out_of_order: { head: 'Reads from a cell that comes later', tone: 'warning' },
  cycle: { head: 'Part of a dependency cycle', tone: 'warning' },
  cross_cell_mutation: { head: 'Changes a variable defined elsewhere', tone: 'info' },
  multiple_definitions: { head: 'Variable defined in more than one cell', tone: 'info' },
}
function issueCallout(iss) {
  const known = ISSUE_HEAD[iss.code]
  const tone = iss.severity === 'amber' || known?.tone === 'warning' ? 'warning' : 'info'
  const head = known?.head || (tone === 'warning' ? 'Dependency warning' : 'Dependency note')
  // external_state's message ends with the effect ("; the cell won't re-run ..."), which the
  // bold line already says; the detail keeps the rest.
  // A grouped entry (several files of one kind in one cell) brings its own short detail.
  const detail = iss.shortDetail || (iss.code === 'external_state' ? iss.message.split(';')[0] : iss.message)
  return { tone, head, detail }
}
const kindClass = (kind) => `lineage-kind-${String(kind || '').replace(/[^a-z0-9_-]/gi, '')}`
const STATUS_LABEL = { current: 'Current', stale: 'Stale', edited: 'Edited', error: 'Error' }
const KIND_LABEL = {
  athena: 'Athena', aurora: 'Aurora', dynamodb: 'DynamoDB', s3: 'S3', local: 'Local',
  url: 'URL', bedrock_kb: 'KB', cloudwatch: 'Logs',
}

// Summaries keyed by the exact request payload — reopening the drawer for an
// unchanged lineage never re-asks the model. Failures are not cached.
const summaryCache = new Map()

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

// User-resizable width (left-edge handle, same drag pattern as the AI Assistant
// panel). Clamps match the AI panel (240–900px); the drawer also never grows
// past the viewport minus VIEWPORT_MARGIN. Persisted across sessions.
const WIDTH_KEY = 'microvm-lineage-drawer-width'
const DEFAULT_WIDTH = 440
const MIN_WIDTH = 240
const MAX_WIDTH = 900
const VIEWPORT_MARGIN = 48
const KEY_STEP = 16       // Arrow keys
const KEY_STEP_BIG = 64   // Shift+Arrow

const viewportWidth = () => (typeof window !== 'undefined' ? window.innerWidth : MAX_WIDTH + VIEWPORT_MARGIN)
/** Largest width allowed right now: MAX_WIDTH, or the viewport minus the margin (never below MIN_WIDTH). */
const maxWidthFor = (vw) => Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, vw - VIEWPORT_MARGIN))
const clampWidth = (w, vw = viewportWidth()) => Math.round(Math.min(maxWidthFor(vw), Math.max(MIN_WIDTH, w)))

function readStoredWidth() {
  try {
    const n = Number.parseInt(localStorage.getItem(WIDTH_KEY), 10)
    return Number.isFinite(n) ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, n)) : DEFAULT_WIDTH
  } catch {
    return DEFAULT_WIDTH
  }
}

function storeWidth(w) {
  try { localStorage.setItem(WIDTH_KEY, String(w)) } catch {}
}

/**
 * Width state + handlers for the drawer's left-edge resize handle: mouse drag
 * (document listeners, col-resize cursor, no text selection, `dragging` while
 * active so CSS can drop transitions) and keyboard (Arrow keys ±16px, Shift
 * ±64px, Home/End = min/max). The stored width is written on drag end / key.
 */
function useDrawerWidth(drawerRef) {
  const [width, setWidth] = useState(readStoredWidth)
  const [vw, setVw] = useState(viewportWidth)
  const [dragging, setDragging] = useState(false)
  const cleanupRef = useRef(null)

  useEffect(() => {
    const onResize = () => setVw(viewportWidth())
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  // Unmount mid-drag (e.g. Esc while dragging): drop the document listeners.
  useEffect(() => () => cleanupRef.current?.(), [])

  const onMouseDown = (e) => {
    if (e.button !== 0) return
    e.preventDefault()
    // Start from the RENDERED width (the CSS may cap the stored one on a small viewport).
    const startWidth = drawerRef.current ? drawerRef.current.getBoundingClientRect().width : width
    const startX = e.clientX
    let latest = clampWidth(startWidth)
    setDragging(true)
    const onMove = (ev) => {
      // Right-anchored: moving the left edge left widens the drawer.
      latest = clampWidth(startWidth + (startX - ev.clientX))
      setWidth(latest)
    }
    const stop = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      cleanupRef.current = null
    }
    const onUp = () => {
      stop()
      setDragging(false)
      storeWidth(latest)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    cleanupRef.current = stop
  }

  const onKeyDown = (e) => {
    const step = e.shiftKey ? KEY_STEP_BIG : KEY_STEP
    const cur = clampWidth(width, vw)
    let next = null
    if (e.key === 'ArrowLeft') next = cur + step
    else if (e.key === 'ArrowRight') next = cur - step
    else if (e.key === 'Home') next = MIN_WIDTH
    else if (e.key === 'End') next = maxWidthFor(vw)
    if (next === null) return
    e.preventDefault()
    next = clampWidth(next, vw)
    setWidth(next)
    storeWidth(next)
  }

  // `cssWidth` is the chosen width; the stylesheet caps it to the viewport (and
  // keeps small screens full width). `width` is what the handle reports.
  return { cssWidth: width, width: clampWidth(width, vw), min: MIN_WIDTH, max: maxWidthFor(vw), dragging, onMouseDown, onKeyDown }
}

/**
 * Dependency-notes badge for a cell (stored DAG issues): count, plus "!" when any
 * is a warning, so colour is never the only signal. Opens the lineage drawer.
 */
export function DagIssueBadge({ summary, onClick }) {
  if (!summary) return null
  return (
    <button
      type="button"
      className={`dag-issue-badge${summary.amber ? ' dag-issue-badge-amber' : ''}`}
      aria-haspopup="dialog"
      aria-label={summary.ariaLabel}
      title={summary.title}
      onClick={onClick}
    >
      {summary.count}{summary.amber ? '!' : ''}
    </button>
  )
}

/** Lineage-icon trigger shown on App-tab blocks and in the notebook cell Output
 *  header. Same icon as the Outline row Lineage button (IconLineage). */
export function LineageTrigger({ level, showDot = true, onClick }) {
  const label = TRUST_LABEL[level] || 'unknown'
  return (
    <button
      type="button"
      className="lineage-trigger"
      aria-haspopup="dialog"
      aria-label={`How was this computed? Trust: ${label}`}
      title="How was this computed?"
      onClick={onClick}
    >
      <IconLineage className="lineage-trigger-icon" width={12} height={12} aria-hidden="true" focusable="false" /> <span className="lineage-trigger-label">How was this computed?</span>
      {showDot && <span className={`lineage-dot${level ? ` lineage-dot-${level}` : ''}`} aria-hidden="true" />}
    </button>
  )
}

function badgeClass(type) {
  return `cell-type-badge ${type === 'sql' ? 'cell-type-sql' : 'cell-type-code'}`
}

function useLineageSummary(payload) {
  const key = useMemo(() => JSON.stringify(payload), [payload])
  const [state, setState] = useState(() => (summaryCache.has(key) ? { status: 'ok', text: summaryCache.get(key) } : { status: 'loading', text: null }))

  useEffect(() => {
    if (summaryCache.has(key)) { setState({ status: 'ok', text: summaryCache.get(key) }); return }
    const ctrl = new AbortController()
    setState({ status: 'loading', text: null })
    ;(async () => {
      try {
        const resp = await apiFetch('/ai/explain-lineage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: key,
          signal: ctrl.signal,
        })
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const data = await resp.json()
        if (!data || typeof data.summary !== 'string' || !data.summary.trim()) throw new Error('no summary')
        summaryCache.set(key, data.summary)
        setState({ status: 'ok', text: data.summary })
      } catch {
        if (!ctrl.signal.aborted) setState({ status: 'error', text: null })
      }
    })()
    return () => ctrl.abort()
  }, [key])

  return state
}

export default function LineageDrawer({ lineage, cells, viewMode, highlight = false, onToggleHighlight, onOpenStep, onClose, opener: openerProp = null }) {
  const titleId = useId()
  const drawerRef = useRef(null)
  const closeRef = useRef(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [showStatusNote, setShowStatusNote] = useState(false)
  const resize = useDrawerWidth(drawerRef)

  const cellById = useMemo(() => new Map((cells || []).map(c => [String(c.id), c])), [cells])
  const rootCell = cellById.get(String(lineage.cellId)) || {}
  const rootStep = lineage.steps.find(s => s.cellId === String(lineage.cellId))
  const outputVar = deriveDisplayVar(rootCell) || rootCell.outputVariable || null

  const payload = useMemo(() => buildExplainPayload(lineage, cells), [lineage, cells])
  const summary = useLineageSummary(payload)

  // Focus management: remember the opener, focus Close, restore on unmount.
  // Prefer the opener the parent recorded at click time: on a re-target the
  // previous drawer instance restores focus to ITS opener while unmounting, so
  // document.activeElement here would be the wrong (first) trigger.
  useEffect(() => {
    const opener = openerProp || document.activeElement
    closeRef.current?.focus()
    // preventScroll: returning focus to an Outline row must not scroll the list.
    return () => { if (opener && typeof opener.focus === 'function' && opener.isConnected) opener.focus({ preventScroll: true }) }
  }, [])

  // Esc closes; Tab/Shift+Tab stay inside the drawer. While a confirm dialog is open
  // on top of the drawer, the dialog owns Escape and Tab, so the drawer stands down.
  useEffect(() => {
    const onKey = (e) => {
      if (isModalOpen()) return
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation(); onClose?.()
        return
      }
      if (e.key !== 'Tab' || !drawerRef.current) return
      const items = [...drawerRef.current.querySelectorAll(FOCUSABLE)].filter(el => el.offsetParent !== null)
      if (!items.length) return
      const first = items[0], last = items[items.length - 1]
      const idx = items.indexOf(document.activeElement)
      if (e.shiftKey && idx <= 0) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && (idx === -1 || idx === items.length - 1)) { e.preventDefault(); first.focus() }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])

  // Click outside closes, except: another lineage trigger (re-targets the drawer),
  // a cell Delete button (the drawer stays open while the confirm dialog is used),
  // and any click while a confirm dialog is open.
  useEffect(() => {
    const onDown = (e) => {
      const t = e.target
      if (drawerRef.current?.contains(t)) return
      if (isModalOpen()) return
      if (t && t.closest && t.closest('.lineage-trigger, .dag-issue-badge, .outline-lineage-btn, .outline-delete-btn, .cell-delete-btn')) return
      onClose?.()
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [onClose])

  const toggleCode = (id) => setExpanded(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  const { trust, sources, inputs, steps } = lineage
  const usedBy = lineage.usedBy || []
  const issues = lineage.issues || []

  // One timeline node + card (same markup for upstream Steps and downstream Used by).
  const renderNode = (s, i, group) => {
    const codeId = `${titleId}-code-${group}-${i}`
    const open = expanded.has(s.cellId)
    const code = cellById.get(s.cellId)?.code || ''
    const isCurrent = group === 'steps' && s.cellId === String(lineage.cellId)
    const kindName = group === 'steps' ? 'step' : 'used'
    return (
      <li
        key={`${group}:${s.cellId}`}
        className={`lineage-node lineage-node-${kindName} lineage-node-st-${s.status}${isCurrent ? ' lineage-node-current' : ''}`}
      >
        <span className="lineage-marker" aria-hidden="true">{group === 'steps' ? i + 1 : ''}</span>
        <div className="lineage-card">
          <div className="lineage-card-top">
            <span className={badgeClass(s.type)}>{s.badge}</span>
            {isCurrent && <span className="lineage-this">THIS CELL</span>}
            <span className={`lineage-status lineage-status-${s.status}`}>{STATUS_LABEL[s.status] || s.status}</span>
          </div>
          <p className="lineage-step-title">{s.title}</p>
          <div className="lineage-step-meta">
            {s.varsIn.length > 0 && (
              <span className="lineage-step-vars">
                uses {s.varsIn.map(v => <code key={v} className="lineage-var-chip">{v}</code>)}
              </span>
            )}
            <span className="lineage-step-time" title={s.executedAt || 'Not run'}>
              {s.executedAt ? `ran ${relativeTime(s.executedAt)}` : 'not run'}
            </span>
          </div>
          <div className="lineage-step-actions">
            <button
              type="button"
              className="lineage-act lineage-code-btn"
              aria-expanded={open}
              aria-controls={codeId}
              aria-label={`${open ? 'Hide' : 'Show'} code of ${s.badge}`}
              onClick={() => toggleCode(s.cellId)}
            >
              <span className="lineage-act-glyph" aria-hidden="true">{'{ }'}</span>{open ? 'Hide' : 'Code'}
            </button>
            <button
              type="button"
              className="lineage-act lineage-open-btn"
              aria-label={`Open ${s.badge} in notebook`}
              title={`Open ${s.badge} in the notebook`}
              onClick={() => onOpenStep?.(s.cellId)}
            >
              <span className="lineage-act-glyph" aria-hidden="true">↗</span>Open
            </button>
          </div>
          <pre
            id={codeId}
            className="lineage-code"
            hidden={!open}
            dangerouslySetInnerHTML={{ __html: open ? highlightBasic(code, s.type === 'sql' ? 'sql' : 'python') : '' }}
          />
        </div>
      </li>
    )
  }
  // Info issues also appear in `notes`; they are listed once, under Dependency notes.
  const issueNotes = new Set(issues.map(i => `${i.badge}: ${i.message}`))
  const sourceNotes = lineage.notes.filter(n => !issueNotes.has(n))
  const newest = steps.reduce((acc, s) => {
    const t = s.executedAt ? Date.parse(s.executedAt) : NaN
    return Number.isNaN(t) || (acc && Date.parse(acc) >= t) ? acc : s.executedAt
  }, null)
  const status = lineageStatusText(steps, newest ? relativeTime(newest) : null)
  const statusNoteId = `${titleId}-status-note`

  return createPortal(
    <aside
      ref={drawerRef}
      className={`lineage-drawer${resize.dragging ? ' lineage-drawer-dragging' : ''}`}
      style={{ '--lineage-drawer-width': `${resize.cssWidth}px` }}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
    >
      <div
        className="lineage-resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize panel"
        aria-valuenow={resize.width}
        aria-valuemin={resize.min}
        aria-valuemax={resize.max}
        tabIndex={0}
        title="Drag or use the arrow keys to resize"
        onMouseDown={resize.onMouseDown}
        onKeyDown={resize.onKeyDown}
      />
      <header className="lineage-header">
        <div className="lineage-header-text">
          <h2 id={titleId} className="lineage-title">How was this computed?</h2>
          <div className="lineage-subtitle">
            {rootStep && <span className={badgeClass(rootStep.type)}>{rootStep.badge}</span>}
            <div className={`lineage-trust lineage-trust-${trust.level || 'none'}`}>
              <span className={`lineage-dot${trust.level ? ` lineage-dot-${trust.level}` : ''}`} aria-hidden="true" />
              <span className="lineage-trust-label">{TRUST_LABEL[trust.level] || 'Unknown'}</span>
            </div>
            {outputVar && <code className="lineage-subtitle-var">{outputVar}</code>}
          </div>
          <div className="lineage-status-line" title={status.tooltip} aria-describedby={statusNoteId}>
            <span className="lineage-status-text">{status.text}</span>
            <button
              type="button"
              className="lineage-status-info"
              aria-expanded={showStatusNote}
              aria-controls={statusNoteId}
              aria-label="Which cells are counted?"
              title={status.tooltip}
              onClick={() => setShowStatusNote(v => !v)}
            >
              <span className="lineage-status-info-mark" aria-hidden="true">i</span>
            </button>
          </div>
          <p id={statusNoteId} className="lineage-status-note" hidden={!showStatusNote}>{status.tooltip}</p>
        </div>
        <button ref={closeRef} type="button" className="lineage-close" aria-label="Close" title="Close (Esc)" onClick={onClose}>
          <IconX width={14} height={14} />
        </button>
      </header>

      <div className="lineage-body">
        <section className="lineage-section lineage-summary" aria-labelledby={`${titleId}-summary`}>
          <div className="lineage-summary-card">
            <h3 id={`${titleId}-summary`} className="lineage-section-title">
              <IconSparkles className="lineage-summary-icon" width={12} height={12} aria-hidden="true" focusable="false" />
              Summary
            </h3>
            <p className="lineage-summary-text" aria-live="polite">
              {summary.status === 'loading' && <span className="lineage-muted">Summarizing…</span>}
              {summary.status === 'ok' && summary.text}
              {summary.status === 'error' && <span className="lineage-muted">Summary unavailable</span>}
            </p>
          </div>
          {trust.reasons.length > 0 && <ul className="lineage-reasons">
            {trust.reasons.map(r => <li key={r}>{r}</li>)}
          </ul>}
        </section>

        <section className="lineage-section lineage-sources" aria-labelledby={`${titleId}-sources`}>
          <h3 id={`${titleId}-sources`} className="lineage-section-title">Data sources</h3>
          {sources.length === 0 ? (
            <p className="lineage-muted">No external sources recorded</p>
          ) : (
            [
              { key: 'own', title: 'Read by this cell', items: sources.filter(s => s.readByThisCell) },
              { key: 'upstream', title: 'Upstream', items: sources.filter(s => !s.readByThisCell) },
            ].filter(g => g.items.length > 0).map(g => (
              <div key={g.key} className={`lineage-source-group lineage-source-group-${g.key}`}>
                <h4 id={`${titleId}-sources-${g.key}`} className="lineage-source-group-title">{g.title}</h4>
                <ul className="lineage-chips" aria-labelledby={`${titleId}-sources-${g.key}`}>
                  {g.items.map(s => {
                    // Upstream chips name the step(s) that read the source; a source the
                    // selected cell reads itself only notes OTHER steps that read it too.
                    const others = (s.steps || []).filter(st => st.cellId !== String(lineage.cellId))
                    const via = others.length ? `${s.readByThisCell ? 'also via' : 'via'} ${sourceStepLabels(others).join(', ')}` : ''
                    return (
                      <li
                        key={`${s.kind}:${s.id}`}
                        className={`lineage-chip ${kindClass(s.kind)} ${s.inCatalog ? 'lineage-chip-catalog' : 'lineage-chip-adhoc'}`}
                        title={`${s.id} — ${s.inCatalog ? 'In data catalog' : 'Ad-hoc source'}${via ? ` — ${via}` : ''}`}
                      >
                        <span className="lineage-chip-kind">{KIND_LABEL[s.kind] || s.kind}</span>
                        <span className="lineage-chip-id">{s.catalogRef?.display_name || s.id}</span>
                        {!s.inCatalog && <span className="lineage-chip-note">ad-hoc</span>}
                        {via && <span className="lineage-chip-via">{via}</span>}
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))
          )}
          {sourceNotes.length > 0 && (
            <ul className="lineage-notes">
              {sourceNotes.map(n => <li key={n}>{n}</li>)}
            </ul>
          )}
        </section>

        {issues.length > 0 && (
          <section className="lineage-section lineage-issues" aria-labelledby={`${titleId}-issues`}>
            <h3 id={`${titleId}-issues`} className="lineage-section-title">Dependency notes</h3>
            <ul className="lineage-issue-list">
              {groupDependencyIssues(issues).map((iss, i) => {
                const c = issueCallout(iss)
                const badge = badgeClass(steps.find(st => st.cellId === iss.cellId)?.type)
                return (
                  <li
                    key={`${iss.cellId}:${iss.code}:${iss.var || ''}:${i}`}
                    className={`lineage-issue lineage-issue-${iss.severity} lineage-issue-${c.tone}`}
                    title={`${iss.badge} ${iss.message}`}
                  >
                    {c.tone === 'warning'
                      ? <IconAlertTriangle className="lineage-issue-icon" width={16} height={16} aria-hidden="true" focusable="false" />
                      : <span className="lineage-issue-mark" aria-hidden="true">i</span>}
                    <div className="lineage-issue-body">
                      <p className="lineage-issue-head">
                        <span className="lineage-sr-only">{c.tone === 'warning' ? 'Warning: ' : 'Note: '}</span>
                        {c.head}
                      </p>
                      <span className="lineage-issue-text">
                        <span className={badge}>{iss.badge}</span>
                        {c.detail}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ul>
          </section>
        )}

        {inputs.length > 0 && (
          <section className="lineage-section lineage-inputs" aria-labelledby={`${titleId}-inputs`}>
            <h3 id={`${titleId}-inputs`} className="lineage-section-title">Inputs</h3>
            <dl className="lineage-input-list">
              {inputs.map(inp => (
                <div key={`${inp.cellId}:${inp.varName}`} className="lineage-input-row">
                  <dt>{inp.label}</dt>
                  <dd>{String(inp.value ?? '')}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}

        <section className="lineage-section lineage-lineage" aria-labelledby={`${titleId}-lineage`}>
          <div className="lineage-section-head lineage-lineage-head">
            <div className="lineage-lineage-titles">
              <h3 id={`${titleId}-lineage`} className="lineage-section-title">Lineage</h3>
              <span className="lineage-lineage-sub">source → upstream cells → this cell → used by</span>
            </div>
            {viewMode === 'notebook' && (
              <button
                type="button"
                className="lineage-btn lineage-highlight-btn"
                aria-pressed={highlight}
                onClick={onToggleHighlight}
              >
                Highlight in notebook
              </button>
            )}
          </div>
          <ol className="lineage-timeline" aria-label="Lineage chain">
            {sources.length > 0 && (
              <li className={`lineage-node lineage-node-source ${kindClass(sources[0].kind)}`}>
                <span className="lineage-marker" aria-hidden="true" />
                <p className="lineage-rail-label-text">Source</p>
                {/* The full list (with "via <cell>") is the Data sources section above;
                    repeating it here duplicated 9+ chips, so the rail only points to it. */}
                <p className="lineage-src-summary">
                  {sources.length === 1 ? '1 data source' : `${sources.length} data sources`}
                  {' · listed under Data sources above'}
                </p>
              </li>
            )}
            {steps.map((s, i) => renderNode(s, i, 'steps'))}
            <li className="lineage-rail-label"><p className="lineage-rail-label-text">Used by</p></li>
            {usedBy.length === 0 ? (
              <li className="lineage-node lineage-node-end">
                <span className="lineage-marker" aria-hidden="true" />
                <p className="lineage-end-text">End of chain · No later cells depend on this cell</p>
              </li>
            ) : (
              usedBy.map((s, i) => renderNode(s, i, 'used-by'))
            )}
          </ol>
        </section>
      </div>

      <footer className="lineage-footer">
        <span title={newest || undefined}>Last run {newest ? relativeTime(newest) : 'never'}</span>
        <span aria-hidden="true"> · </span>
        <span className="lineage-footer-status" title={status.tooltip}>{status.footer}</span>
        {lineage.cycle && <span className="lineage-cycle"> · Dependency cycle detected</span>}
      </footer>
    </aside>,
    document.body
  )
}
