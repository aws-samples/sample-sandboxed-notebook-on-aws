import { useState, useEffect, useCallback, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { IconX } from './Icons'
import { ConfirmModal } from './Modal'
import DiffCell from './DiffCell'
import { CELL_TYPE_META } from './editorShared'
import { cellBadgeMap } from '../utils/cellBadge'
import {
  listNotebookVersions,
  getNotebookVersion,
  createNotebookVersion,
  restoreNotebookVersion,
  deleteNotebookVersion,
} from '../services/notebooks'
import './VersionHistoryDrawer.css'

// Human label + tone for a version's trigger.
const VERSION_TRIGGER = {
  manual: { label: 'Saved', tone: 'manual' },
  auto: { label: 'Auto', tone: 'auto' },
  pre_ai: { label: 'Before AI', tone: 'guard' },
  pre_restore: { label: 'Before restore', tone: 'guard' },
  baseline: { label: 'Baseline', tone: 'guard' },
}

// A human descriptor for a version that works whether or not it has a user label.
// Named versions read as their name ("Test1"); unnamed ones (autos / pre_ai /
// pre_restore) read as their kind + age ("Auto · 24m ago"), so the button, the
// compare bar, and the confirm dialog never say a meaningless "Untitled".
function versionDescriptor(v) {
  if (!v) return 'this version'
  if (v.label && v.label.trim()) return v.label.trim()
  const kind = (VERSION_TRIGGER[v.trigger] || { label: v.trigger || 'Version' }).label
  const when = v.created_at ? formatAgo(Date.parse(v.created_at)) : ''
  return when ? `${kind} · ${when}` : kind
}

function formatAgo(ts) {
  if (!ts) return ''
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

// Map live tab cells to the version cell shape (id/type/code) for diffing.
function liveCellsOf(tab) {
  return (tab?._cells || []).map(c => ({ id: c.id, type: c.type || 'code', code: c.code || '' }))
}

/**
 * VersionHistoryDrawer — a right-side drawer that is the single home for a
 * notebook's document versions: list (left) + selected-version diff vs current
 * (right), with whole-notebook Restore and per-cell Revert. Opened from the disk
 * icon. Run History stays in the sidebar (it's execution telemetry, not editing).
 */
export default function VersionHistoryDrawer({ tab, onClose }) {
  const notebookId = tab?.id
  const liveCells = useMemo(() => liveCellsOf(tab), [tab])

  const [versions, setVersions] = useState(null)  // null = loading
  const [error, setError] = useState(false)
  const [selectedId, setSelectedId] = useState(null)   // TARGET version (list click)
  const [detail, setDetail] = useState(null)           // {header, cells} of TARGET
  const [detailLoading, setDetailLoading] = useState(false)
  // BASE of the comparison: 'current' (the live notebook, default) or a vgid.
  // The diff renders base → target; Restore/Revert always act on the TARGET.
  const [baseId, setBaseId] = useState('current')
  const [baseDetail, setBaseDetail] = useState(null)   // {cells} of BASE when a version
  const [busy, setBusy] = useState(false)
  const [saveOpen, setSaveOpen] = useState(false)      // inline save form revealed?
  const [saveLabel, setSaveLabel] = useState('')       // inline name field
  const [restoreTarget, setRestoreTarget] = useState(null)  // null | version header
  const [revertTarget, setRevertTarget] = useState(null)    // null | { cellId, code, label }
  const [deleteTarget, setDeleteTarget] = useState(null)    // null | version header
  const [expandedCells, setExpandedCells] = useState(() => new Set())  // unchanged cells opened by click
  const toggleCell = useCallback((cid) => {
    setExpandedCells(prev => {
      const next = new Set(prev)
      next.has(cid) ? next.delete(cid) : next.add(cid)
      return next
    })
  }, [])
  // Reset which unchanged cells are open whenever the compared pair changes.
  useEffect(() => { setExpandedCells(new Set()) }, [selectedId, baseId])

  // Resizable geometry — persisted so the user's preferred layout sticks.
  // (1) whole-drawer width; (2) the left version-list pane width within it.
  const [drawerWidth, setDrawerWidth] = useState(() => {
    const v = parseInt(localStorage.getItem('microvm.vhd.width'), 10)
    return Number.isFinite(v) ? v : 880
  })
  const [listWidth, setListWidth] = useState(() => {
    const v = parseInt(localStorage.getItem('microvm.vhd.listWidth'), 10)
    return Number.isFinite(v) ? v : 260
  })
  useEffect(() => { try { localStorage.setItem('microvm.vhd.width', String(drawerWidth)) } catch {} }, [drawerWidth])
  useEffect(() => { try { localStorage.setItem('microvm.vhd.listWidth', String(listWidth)) } catch {} }, [listWidth])

  // Drag the drawer's LEFT edge to widen/narrow the whole drawer. Dragging left
  // (toward screen center) grows it, so width += (startX - clientX).
  const startDrawerResize = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = drawerWidth
    const onMove = (ev) => {
      const next = Math.min(Math.round(window.innerWidth * 0.95), Math.max(520, startW + (startX - ev.clientX)))
      setDrawerWidth(next)
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }, [drawerWidth])

  // Drag the splitter between the version list and the diff pane.
  const startListResize = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = listWidth
    const onMove = (ev) => {
      // Cap the list at drawerWidth - 320 so the diff pane can't be crushed.
      const max = Math.max(200, drawerWidth - 320)
      const next = Math.min(max, Math.max(180, startW + (ev.clientX - startX)))
      setListWidth(next)
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }, [listWidth, drawerWidth])

  const load = useCallback(() => {
    if (notebookId == null) return
    listNotebookVersions(notebookId, 50)
      .then(v => {
        const list = Array.isArray(v) ? v : []
        setVersions(list)
        setError(false)
        // Auto-select the newest so the diff pane isn't empty on open.
        setSelectedId(prev => prev || (list[0]?.version_group_id ?? null))
      })
      .catch(() => { setError(true); setVersions([]) })
  }, [notebookId])

  useEffect(() => { load() }, [load])

  // Refresh when a version is saved elsewhere (e.g. Cmd+S quick-save).
  useEffect(() => {
    const onSaved = (e) => { if (String(e.detail?.notebookId) === String(notebookId)) load() }
    window.addEventListener('notebook-version-saved', onSaved)
    return () => window.removeEventListener('notebook-version-saved', onSaved)
  }, [load, notebookId])

  // Esc closes the drawer.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Fetch the TARGET version's cells for the diff pane.
  useEffect(() => {
    if (!selectedId) { setDetail(null); return }
    let cancelled = false
    setDetailLoading(true)
    setDetail(null)
    getNotebookVersion(String(notebookId), selectedId).then(d => {
      if (!cancelled) { setDetail(d); setDetailLoading(false) }
    })
    return () => { cancelled = true }
  }, [selectedId, notebookId])

  // Fetch the BASE version's cells when comparing against a stored version (not
  // 'current', which uses the live liveCells we already have).
  useEffect(() => {
    if (baseId === 'current') { setBaseDetail(null); return }
    let cancelled = false
    getNotebookVersion(String(notebookId), baseId).then(d => {
      if (!cancelled) setBaseDetail(d)
    })
    return () => { cancelled = true }
  }, [baseId, notebookId])

  // If the chosen base equals the target (would be an empty self-diff), snap the
  // base back to 'current' so the diff always compares two distinct points.
  useEffect(() => {
    if (baseId !== 'current' && baseId === selectedId) setBaseId('current')
  }, [baseId, selectedId])

  // Force the pending autosave to land BEFORE we snapshot, so a manual save right
  // after an edit can't version stale (pre-edit) cells — the version route reads
  // the persisted cells as the source of truth. Resolves on 'autosave-flushed' or
  // after a short timeout (never hang the save).
  const flushAutosave = (nbId) => new Promise((resolve) => {
    let done = false
    const finish = () => { if (done) return; done = true; window.removeEventListener('autosave-flushed', onFlushed); resolve() }
    const onFlushed = (e) => { if (String(e.detail?.notebookId) === String(nbId)) finish() }
    window.addEventListener('autosave-flushed', onFlushed)
    window.dispatchEvent(new CustomEvent('flush-autosave', { detail: { notebookId: nbId } }))
    setTimeout(finish, 2000)
  })

  const commitSave = async () => {
    if (busy) return
    const label = saveLabel.trim()
    setBusy(true)
    await flushAutosave(notebookId)  // ensure latest cells are persisted first
    const header = await createNotebookVersion(String(notebookId), { label: label || null, trigger: 'manual' })
    setBusy(false)
    setSaveLabel('')
    setSaveOpen(false)
    if (header) {
      // Refresh + select the freshly-saved version so its (empty) diff shows.
      setSelectedId(header.version_group_id)
      load()
    }
  }

  const commitRestore = async () => {
    const version = restoreTarget
    setRestoreTarget(null)
    if (!version) return
    setBusy(true)
    const res = await restoreNotebookVersion(String(notebookId), version.version_group_id)
    setBusy(false)
    if (res && res.success) {
      // App swaps the live cells to the restored version; the pre_restore snapshot
      // is already saved server-side.
      window.dispatchEvent(new CustomEvent('notebook-version-restored', {
        detail: { notebookId, cells: res.cells || [], dag: res.dag || null },
      }))
      window.dispatchEvent(new CustomEvent('notebook-version-saved', { detail: { notebookId } }))
      // Close the drawer so the user lands back on the now-restored notebook.
      onClose()
    }
  }

  const commitDelete = async () => {
    const version = deleteTarget
    setDeleteTarget(null)
    if (!version) return
    const vgid = version.version_group_id
    setBusy(true)
    const ok = await deleteNotebookVersion(String(notebookId), vgid)
    setBusy(false)
    if (ok) {
      // If the deleted version was the current target or base, reset the selection
      // so the diff pane doesn't point at a gone version.
      if (selectedId === vgid) { setSelectedId(null); setDetail(null) }
      if (baseId === vgid) { setBaseId('current') }
      load()
    }
  }

  // Per-cell revert: apply ONE cell's code from the selected version onto the live
  // notebook. App.jsx handles 'notebook-revert-cell' by patching just that cell
  // (and takes a pre_ai-style safety snapshot). Autosave then persists it.
  const commitRevertCell = () => {
    const t = revertTarget
    setRevertTarget(null)
    if (!t) return
    window.dispatchEvent(new CustomEvent('notebook-revert-cell', {
      detail: { notebookId, cellId: t.cellId, code: t.code },
    }))
    // Close the drawer so the user lands on the notebook; App scrolls to the cell.
    onClose()
  }

  // Resolve the BASE cell list: the live notebook ('current') or a version's cells.
  const baseCells = baseId === 'current' ? (liveCells || []) : (baseDetail?.cells || [])
  const baseReady = baseId === 'current' || baseDetail != null

  // Build the per-cell diff: BASE → TARGET. Removed = only in base; added = only
  // in target; changed = code differs. DiffCell renders base as `original` and
  // target as `current`, so "− base / + target" is consistent everywhere.
  const diffCells = useMemo(() => {
    if (!detail || !baseReady) return null
    const targetCells = detail.cells || []
    const baseById = new Map((baseCells || []).map(c => [String(c.id), c]))
    const targetById = new Map(targetCells.map(c => [String(c.id), c]))
    // Badge numbering follows the TARGET (the version you're inspecting/acting on);
    // base-only cells fall back to base numbering.
    const targetBadges = cellBadgeMap(targetCells)
    const baseBadges = cellBadgeMap(baseCells || [])
    const order = []
    const seen = new Set()
    for (const c of targetCells) { order.push(String(c.id)); seen.add(String(c.id)) }
    for (const c of (baseCells || [])) { if (!seen.has(String(c.id))) order.push(String(c.id)) }
    return order.map(cid => {
      const base = baseById.get(cid)
      const target = targetById.get(cid)
      const type = (target?.type || base?.type || 'code')
      // 'added' = present in target, absent from base; 'removed' = present in base,
      // absent from target; 'changed' = both present, code differs.
      const status = !base ? 'added' : !target ? 'removed'
        : (base.code || '') !== (target.code || '') ? 'changed' : 'same'
      const badge = targetBadges.get(cid) || baseBadges.get(cid) || (CELL_TYPE_META[type]?.label || 'PY')
      return { cid, base, target, type, status, badge }
    })
  }, [detail, baseCells, baseReady])

  // How many cells differ in the CURRENTLY-SHOWN diff (target vs the selected
  // base). Drives the "No differences / N differ" summary line.
  const shownDiffCount = useMemo(
    () => (diffCells || []).filter(d => d.status !== 'same').length,
    [diffCells],
  )

  // Whether the TARGET version is identical to the LIVE notebook — computed
  // against liveCells directly (NOT the selected base), because Restore always
  // replaces the live notebook regardless of what base the diff is showing. If
  // they match, Restore is a no-op, so the button is disabled. Compares the same
  // id/type/code shape the diff uses, order-insensitive by id.
  const matchesCurrent = useMemo(() => {
    if (!detail) return false
    const target = detail.cells || []
    const live = liveCells || []
    if (target.length !== live.length) return false
    const liveById = new Map(live.map(c => [String(c.id), c]))
    for (const t of target) {
      const l = liveById.get(String(t.id))
      if (!l) return false
      if ((l.type || 'code') !== (t.type || 'code')) return false
      if ((l.code || '') !== (t.code || '')) return false
    }
    return true
  }, [detail, liveCells])

  const drawer = (
    <div className="vhd-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <aside className="vhd-drawer" role="dialog" aria-label="Version history" style={{ width: `${drawerWidth}px` }}>
        {/* Drag the drawer's left edge to resize the whole panel. */}
        <div className="vhd-drawer-resize" onMouseDown={startDrawerResize} title="Drag to resize" />
        <header className="vhd-header">
          <div className="vhd-title">
            <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 3v5h5" /><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8" /><path d="M12 7v5l3 2" />
            </svg>
            Version history
            <span className="vhd-subtitle">{tab?.name}</span>
          </div>
          {saveOpen ? (
            <form
              className="vhd-save-form"
              onSubmit={(e) => { e.preventDefault(); commitSave() }}
            >
              <input
                className="vhd-save-input"
                type="text"
                value={saveLabel}
                onChange={(e) => setSaveLabel(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') { setSaveOpen(false); setSaveLabel('') } }}
                placeholder="Version name (optional)"
                autoFocus
              />
              <button type="submit" className="vhd-icon-btn" disabled={busy}>
                {busy ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                className="vhd-save-cancel"
                onClick={() => { setSaveOpen(false); setSaveLabel('') }}
                title="Cancel"
              >
                <IconX width={13} height={13} />
              </button>
            </form>
          ) : (
            <button className="vhd-icon-btn" onClick={() => setSaveOpen(true)} disabled={busy} title="Save a snapshot of the current notebook">
              + Save version
            </button>
          )}
          <button className="vhd-close" onClick={onClose} title="Close (Esc)" aria-label="Close"><IconX width={16} height={16} /></button>
        </header>

        <div className="vhd-body">
          {/* LEFT: version list */}
          <div className="vhd-list" style={{ flex: `0 0 ${listWidth}px`, width: `${listWidth}px` }}>
            {versions === null && <div className="vhd-empty">Loading versions…</div>}
            {error && <div className="vhd-empty">Couldn’t load versions.</div>}
            {versions && versions.length === 0 && !error && (
              <div className="vhd-empty">No versions yet.<br />Save one to create a restore point.</div>
            )}
            {versions && versions.map(v => {
              const meta = VERSION_TRIGGER[v.trigger] || { label: v.trigger, tone: 'auto' }
              const ts = v.created_at ? Date.parse(v.created_at) : 0
              const sel = v.version_group_id === selectedId
              return (
                <div
                  key={v.version_group_id}
                  className={`vhd-item vhd-item-${meta.tone}${sel ? ' vhd-item-selected' : ''}`}
                  onClick={() => setSelectedId(v.version_group_id)}
                  role="button"
                  tabIndex={0}
                  title={v.created_at ? new Date(ts).toLocaleString() : ''}
                >
                  <div className="vhd-item-top">
                    <span className={`vhd-trigger vhd-trigger-${meta.tone}`}>{meta.label}</span>
                    {/* Hide the item label when it just repeats the trigger badge
                        (e.g. the "As loaded" baseline: badge + label are the same
                        text). Otherwise show the user's label, or Untitled. */}
                    {(v.label && v.label.trim().toLowerCase() === (meta.label || '').trim().toLowerCase())
                      ? null
                      : <span className="vhd-item-label">{v.label || <span className="vhd-item-untitled">Untitled</span>}</span>}
                    <button
                      className="vhd-item-delete"
                      onClick={(e) => { e.stopPropagation(); setDeleteTarget(v) }}
                      disabled={busy}
                      title="Delete this version"
                      aria-label="Delete this version"
                    >
                      <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><line x1="10" y1="11" x2="10" y2="17" /><line x1="14" y1="11" x2="14" y2="17" />
                      </svg>
                    </button>
                  </div>
                  <div className="vhd-item-meta">
                    <span>{v.cell_count} {v.cell_count === 1 ? 'cell' : 'cells'}</span>
                    <span>·</span>
                    <span>{ts ? formatAgo(ts) : ''}</span>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Splitter: drag to rebalance the list vs diff panes. */}
          <div className="vhd-split" onMouseDown={startListResize} title="Drag to resize" />

          {/* RIGHT: diff of TARGET (list selection) against a chosen BASE. */}
          <div className="vhd-detail">
            {!selectedId && <div className="vhd-empty">Select a version to see what changed.</div>}
            {selectedId && (detailLoading || !baseReady) && <div className="vhd-empty">Loading diff…</div>}
            {selectedId && !detailLoading && baseReady && detail && (
              <>
                {/* GitHub-style directional compare bar: [Base ▾] → Target. The
                    diff reads base → target (− base / + target). Restore/Revert
                    always act on the TARGET onto the live notebook. */}
                <div className="vhd-compare-bar">
                  {/* BASE — the comparison point (quiet, secondary). */}
                  <span className="vhd-compare-side">
                    <span className="vhd-compare-cap">Base</span>
                    <select
                      className="vhd-base-select"
                      value={baseId}
                      onChange={(e) => setBaseId(e.target.value)}
                      title="Choose the base to compare against (this is NOT what gets restored)"
                    >
                      <option value="current">Current notebook</option>
                      {(versions || [])
                        .filter(v => v.version_group_id !== selectedId)
                        .map(v => (
                          <option key={v.version_group_id} value={v.version_group_id}>
                            {versionDescriptor(v)}
                          </option>
                        ))}
                    </select>
                  </span>
                  <span className="vhd-compare-arrow" aria-hidden="true">→</span>
                  {/* TARGET — the version being inspected AND the one Restore acts on. */}
                  <span className="vhd-compare-side">
                    <span className="vhd-compare-cap">Restoring</span>
                    <span className="vhd-compare-target" title="The version you selected — this is what Restore applies">
                      {versionDescriptor(detail)}
                    </span>
                  </span>
                  <button
                    className="vhd-restore-btn"
                    onClick={() => setRestoreTarget(detail)}
                    disabled={busy || matchesCurrent}
                    title={matchesCurrent
                      ? 'This version is identical to the current notebook — nothing to restore'
                      : `Replace the whole live notebook with "${versionDescriptor(detail)}"`}
                  >
                    Restore “{versionDescriptor(detail)}”
                  </button>
                </div>
                {/* Summary of what differs, above the FULL target-notebook view. */}
                {(() => {
                  const changed = shownDiffCount
                  const total = (diffCells || []).length
                  return (
                    <div className="vhd-diff-summary">
                      {changed === 0
                        ? <span>No differences — this version matches {baseId === 'current' ? 'the current notebook' : 'the base'}.</span>
                        : <span><b>{changed}</b> of {total} {total === 1 ? 'cell' : 'cells'} differ</span>}
                      <span className="vhd-diff-legend">
                        <span className="vhd-diff-line-del">− base ({baseId === 'current' ? 'current' : versionDescriptor(baseDetail)})</span>
                        <span className="vhd-diff-line-add">+ target ({versionDescriptor(detail)})</span>
                      </span>
                    </div>
                  )
                })()}
                {/* FULL target notebook in order: changed cells show the diff,
                    unchanged cells collapse (click to expand), removed cells (in
                    base, absent from target) appear as a slim placeholder so the
                    user sees the complete picture before restoring. */}
                <div className="vhd-diff">
                  {(diffCells || []).map(({ cid, base, target, type, status, badge }) => {
                    const meta = CELL_TYPE_META[type] || CELL_TYPE_META.code
                    // Per-cell apply targets cells that exist in the TARGET.
                    const canRevert = status === 'changed' || status === 'added'
                    const statusText = status === 'added' ? 'New in this version'
                      : status === 'removed' ? 'Will be removed on restore'
                      : status === 'changed' ? 'Changed'
                      : 'Unchanged'
                    const isUnchanged = status === 'same'
                    const isRemoved = status === 'removed'
                    const open = expandedCells.has(cid)
                    return (
                      <div
                        key={cid}
                        className={`vhd-diff-cell vhd-diff-${status} vhd-diff-type-${type}${isUnchanged ? ' vhd-diff-collapsed' : ''}`}
                        style={{ '--cell-accent': meta.accent, '--cell-accent-subtle': meta.subtle }}
                      >
                        <div
                          className={`vhd-diff-cell-head${(isUnchanged || isRemoved) ? ' vhd-diff-cell-head-toggle' : ''}`}
                          onClick={(isUnchanged || isRemoved) ? () => toggleCell(cid) : undefined}
                          role={(isUnchanged || isRemoved) ? 'button' : undefined}
                          title={(isUnchanged || isRemoved) ? (open ? 'Collapse' : 'Expand to view code') : undefined}
                        >
                          {(isUnchanged || isRemoved) && (
                            <svg className={`vhd-caret${open ? ' open' : ''}`} width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
                          )}
                          <span className="vhd-diff-badge">{badge}</span>
                          {/* Status glyph so each state reads at a glance and the icon
                              column stays aligned: + added, ~ changed, − removed
                              (danger), = unchanged (muted, so it aligns without
                              drawing the eye). */}
                          <span className={`vhd-diff-glyph vhd-diff-glyph-${status}`} aria-hidden="true">
                            {status === 'added' ? '+' : status === 'removed' ? '−' : status === 'changed' ? '~' : '='}
                          </span>
                          <span className={`vhd-diff-status vhd-diff-status-${status}`}>{statusText}</span>
                          {canRevert && (
                            <button
                              className="vhd-revert-btn"
                              onClick={(e) => { e.stopPropagation(); setRevertTarget({ cellId: cid, code: target?.code || '', label: badge }) }}
                              disabled={busy}
                              title="Replace just this cell in the live notebook with the target version's code"
                            >
                              Apply this cell to notebook
                            </button>
                          )}
                        </div>
                        {/* Body: changed → diff; unchanged/removed → plain code,
                            only mounted when expanded (keeps long notebooks light). */}
                        {status === 'changed' && (
                          <DiffCell original={base?.code || ''} current={target?.code || ''} type={type} />
                        )}
                        {status === 'added' && (
                          <DiffCell original={''} current={target?.code || ''} type={type} />
                        )}
                        {isUnchanged && open && (
                          <DiffCell plain current={target?.code || ''} type={type} />
                        )}
                        {isRemoved && open && (
                          <DiffCell plain current={base?.code || ''} type={type} />
                        )}
                      </div>
                    )
                  })}
                </div>
              </>
            )}
          </div>
        </div>
      </aside>

      {restoreTarget && (
        <ConfirmModal
          title="Restore this version?"
          message={`This replaces the current cells with "${versionDescriptor(restoreTarget)}". Your current cells are snapshotted first, so you can undo this.`}
          confirmLabel="Restore"
          cancelLabel="Cancel"
          onConfirm={commitRestore}
          onCancel={() => setRestoreTarget(null)}
        />
      )}
      {deleteTarget && (
        <ConfirmModal
          title="Delete this version?"
          message={`Permanently delete "${versionDescriptor(deleteTarget)}"? This removes the snapshot and its cells. It does not change your current notebook, and cannot be undone.`}
          confirmLabel="Delete"
          cancelLabel="Cancel"
          confirmDanger
          onConfirm={commitDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
      {revertTarget && (
        <ConfirmModal
          title="Revert this cell?"
          message={`Replace the ${revertTarget.label} cell's current code with this version's code. Only this one cell changes.`}
          confirmLabel="Revert cell"
          cancelLabel="Cancel"
          onConfirm={commitRevertCell}
          onCancel={() => setRevertTarget(null)}
        />
      )}
    </div>
  )

  return createPortal(drawer, document.body)
}
