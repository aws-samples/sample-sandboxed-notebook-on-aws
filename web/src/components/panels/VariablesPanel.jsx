import { useState, useCallback, useMemo, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import {
  IconX, IconChevronDown, IconChevronRight, IconNotebook, IconSearch,
  IconBarChart, IconChartLine, IconTable, IconSparkles, IconBraces, IconCode, IconTrash,
  IconPackage,
} from '../Icons'
import VariablePreviewRenderer from '../VariablePreviewRenderer'
import PanelState from './PanelState'
import PanelExpandButton from './PanelExpandButton'
import VariableDetailModal from '../VariableDetailModal'
import { ConfirmModal } from '../Modal'
import { orphanBadge, planUntrackedCleanup, liveVarUse, cleanupCandidates } from '../../services/variableOrigin'
import { cellBadgeMap } from '../../utils/cellBadge'
import { getTypeIcon, getTypeColor, kindOf, KIND_ORDER } from '../../utils/varTypeMeta'
import { authHeaders } from '../../config'

// Types that support the tabular "View data" viewer + column schema.
const TABULAR_TYPES = new Set(['DataFrame', 'Series', 'ndarray'])

// Scalar types whose value is short enough to show inline on the row (no expand
// needed). Collections (list/dict/tuple/set) intentionally excluded — they carry
// a `shape` (e.g. "12 items") in that slot and their full preview is in the detail.
const SCALAR_TYPES = new Set(['int', 'float', 'complex', 'bool', 'str', 'NoneType'])

// Section icon per coarse kind (the group-by-kind headers). Chosen to echo the
// type-icon vocabulary used on the rows: tabular data → table, collections → { },
// scalars → a value glyph, everything else → a generic package.
const KIND_ICONS = {
  Data: IconTable,
  Collections: IconBraces,
  Scalars: IconCode,
  Other: IconPackage,
}

// A signature that changes whenever a variable's value changes, used to
// invalidate the per-name detail cache (schema + table) on reassignment.
// last_exec (the provenance clock) bumps on EVERY write — including a SQL
// reassignment to a same-shape/same-size DataFrame, which is the case that
// type/shape/size alone can't detect. The rest are fallbacks when provenance
// is unavailable (older VM image without provenance fields).
function varSignature(info) {
  return `${info?.type || ''}|${info?.shape || ''}|${info?.size || ''}|${info?.last_exec ?? ''}`
}

// Parse a human size string ("1.2 KB", "3 MB", "512 B") into bytes for sorting.
function sizeToBytes(sizeStr) {
  if (!sizeStr) return 0
  const m = String(sizeStr).match(/([\d.]+)\s*(B|KB|MB|GB)?/i)
  if (!m) return 0
  const n = parseFloat(m[1]) || 0
  const unit = (m[2] || 'B').toUpperCase()
  const mult = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 }[unit] || 1
  return n * mult
}

export default function VariablesPanel({ variables, activeTab, cells = [], onScrollToCell, onInsertCode, onDeleteVariable, onClose, expanded, onToggleExpand }) {
  const [expandedVar, setExpandedVar] = useState(null)
  const [search, setSearch] = useState('')
  const [sortBy, setSortBy] = useState('name') // 'name' | 'type' | 'size'
  const [grouped, setGrouped] = useState(true)
  // Per-kind collapse state for the group-by-kind view. Sections default OPEN; the
  // render reads open as `collapsedGroups[kind] !== true`, mirroring the Snippets
  // panel so the two behave identically (click header to expand/collapse).
  const [collapsedGroups, setCollapsedGroups] = useState({})
  const toggleGroup = useCallback((kind) => {
    setCollapsedGroups(prev => ({ ...prev, [kind]: !prev[kind] }))
  }, [])
  // Lazily-fetched rich detail per variable: { [name]: { loading, data, error } }
  const [detailCache, setDetailCache] = useState({})
  const [viewerVar, setViewerVar] = useState(null) // name shown in the full-grid modal

  const canFetch = activeTab?.microvmEndpoint && activeTab?.status === 'connected'

  // Fetch rich detail (schema + full table) for ONE variable, on demand.
  const fetchDetail = useCallback(async (name) => {
    if (!canFetch) return
    setDetailCache(prev => {
      // Skip if already loaded or in-flight.
      if (prev[name] && (prev[name].loading || prev[name].data)) return prev
      return { ...prev, [name]: { loading: true, data: null, error: null } }
    })
    try {
      const headers = authHeaders({ 'Content-Type': 'application/json' })
      if (activeTab.sessionId) headers['X-Session-Id'] = activeTab.sessionId
      const resp = await fetch(`${activeTab.microvmEndpoint}/variable-detail`, {
        method: 'POST', headers, body: JSON.stringify({ name }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      setDetailCache(prev => ({ ...prev, [name]: { loading: false, data, error: null } }))
    } catch (err) {
      setDetailCache(prev => ({ ...prev, [name]: { loading: false, data: null, error: err.message } }))
    }
  }, [canFetch, activeTab?.microvmEndpoint, activeTab?.sessionId])

  const toggleExpand = useCallback((name, type) => {
    const next = expandedVar === name ? null : name
    setExpandedVar(next)
    if (next && TABULAR_TYPES.has(type) && canFetch) fetchDetail(name)
  }, [expandedVar, canFetch, fetchDetail])

  // The viewer modal now fetches its own detail, so opening it just needs the name.
  const openViewer = useCallback((name) => {
    setViewerVar(name)
  }, [])

  // Filter + sort (and optionally group) the variable entries.
  const entries = useMemo(() => Object.entries(variables), [variables])

  // --- Provenance: jump-to-cell + new/changed badges ---
  const cellIndexById = useMemo(() => {
    const m = new Map()
    cells.forEach((c, i) => m.set(c.id, i))
    return m
  }, [cells])

  // Per-type ordinal badge (PY-4 / SQL-2 / MD-1) for each cell id — the SAME
  // scheme the cell's own badge and the Outline panel use, so provenance links
  // ("Defined in") read identically to what the user sees on the cell.
  const cellBadgeById = useMemo(() => cellBadgeMap(cells), [cells])

  // Badge label (e.g. "PY-4") for a provenance cell id, or null if the cell is gone.
  const cellLabel = useCallback((cellId) => {
    if (cellId == null || cellId === '') return null
    return cellBadgeById.get(String(cellId)) ?? null
  }, [cellBadgeById])

  // Live cell ids: the origin badge re-decides the VM's "deleted cell" verdict
  // with them (a just-run, not-yet-saved cell is unknown to the VM's DAG copy).
  // null without cells, so the VM's own verdict is used.
  const cellIds = useMemo(
    () => (cells.length ? new Set(cells.map(c => String(c.id))) : null),
    [cells],
  )
  // Who defines / reads each variable over the LIVE cells (the tab's stored DAG,
  // restricted to cells the notebook has now). Decides a deleted producer:
  // another live definer -> no flag; live readers -> amber "in use" (kept out
  // of Clean up); otherwise a plain "deleted cell" flag.
  const dag = activeTab?._dag
  const use = useMemo(() => liveVarUse(cells, dag), [cells, dag])
  // name -> origin badge ('AI' / 'No cell') for variables no cell produces.
  const originBadges = useMemo(() => {
    const m = new Map()
    for (const [name, info] of entries) {
      const b = orphanBadge(info, cellIds, { name, use, badges: cellBadgeById })
      if (b) m.set(name, b)
    }
    return m
  }, [entries, cellIds, use, cellBadgeById])
  // Flagged names that Clean up may offer (a deleted cell's variable that live
  // cells still read is excluded: removing it would break those cells now).
  const cleanupNames = useMemo(() => cleanupCandidates(originBadges), [originBadges])

  // "Clean up N untracked variables": never automatic. Ask the VM which names are
  // still untracked, confirm the exact list, then delete exactly those (the VM
  // re-checks each name and skips any a cell now produces).
  const [cleanup, setCleanup] = useState(null) // null | { phase, plan, error, notice }
  const cleanupHeaders = useCallback((json) => {
    const headers = authHeaders(json ? { 'Content-Type': 'application/json' } : {})
    if (activeTab?.sessionId) headers['X-Session-Id'] = activeTab.sessionId
    return headers
  }, [activeTab?.sessionId])
  // Land the pending autosave first (2 s cap, like VersionHistoryDrawer), so the
  // server's untracked list already reflects a cell deleted a moment ago.
  const flushAutosave = useCallback((nbId) => new Promise((resolve) => {
    if (nbId == null) { resolve(); return }
    let done = false
    const finish = () => { if (done) return; done = true; window.removeEventListener('autosave-flushed', onFlushed); resolve() }
    const onFlushed = (e) => { if (String(e.detail?.notebookId) === String(nbId)) finish() }
    window.addEventListener('autosave-flushed', onFlushed)
    window.dispatchEvent(new CustomEvent('flush-autosave', { detail: { notebookId: nbId } }))
    setTimeout(finish, 2000)
  }), [])
  const startCleanup = useCallback(async () => {
    if (!canFetch) return
    setCleanup({ phase: 'loading', plan: null, error: null, notice: null })
    try {
      await flushAutosave(activeTab?.id)
      const resp = await fetch(`${activeTab.microvmEndpoint}/variables/untracked`, { headers: cleanupHeaders(false) })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const plan = planUntrackedCleanup(cleanupNames, await resp.json())
      if (plan.total === 0) {
        setCleanup({ phase: 'idle', plan: null, error: null, notice: 'Nothing to clean up: every listed variable now has a producing cell.' })
        window.dispatchEvent(new CustomEvent('refresh-variables'))
        return
      }
      setCleanup({ phase: 'confirm', plan, error: null, notice: null })
    } catch (err) {
      setCleanup({ phase: 'idle', plan: null, error: `Could not list untracked variables (${err.message}).`, notice: null })
    }
  }, [canFetch, activeTab?.microvmEndpoint, activeTab?.id, cleanupHeaders, cleanupNames, flushAutosave])
  const confirmCleanup = useCallback(async () => {
    const plan = cleanup?.plan
    if (!plan || !canFetch) return
    setCleanup({ phase: 'deleting', plan, error: null, notice: null })
    try {
      const resp = await fetch(`${activeTab.microvmEndpoint}/variables/delete`, {
        method: 'POST', headers: cleanupHeaders(true), body: JSON.stringify({ names: plan.deleteNames }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json().catch(() => ({}))
      const inUse = Array.isArray(data?.in_use) ? data.in_use.filter(n => typeof n === 'string') : []
      const skipped = (Array.isArray(data?.skipped) ? data.skipped : []).filter(n => !inUse.includes(n))
      const notices = []
      if (inUse.length) notices.push(`Kept ${inUse.join(', ')}: other cells still read ${inUse.length === 1 ? 'it' : 'them'}.`)
      if (skipped.length) notices.push(`Kept ${skipped.join(', ')}: a cell produces ${skipped.length === 1 ? 'it' : 'them'} now.`)
      setCleanup(notices.length
        ? { phase: 'idle', plan: null, error: null, notice: notices.join(' ') }
        : null)
    } catch (err) {
      setCleanup({ phase: 'idle', plan: null, error: `Clean up failed (${err.message}). Nothing was confirmed deleted.`, notice: null })
    }
    window.dispatchEvent(new CustomEvent('refresh-variables'))
  }, [cleanup, canFetch, activeTab?.microvmEndpoint, cleanupHeaders])
  const untrackedCount = cleanupNames.length

  const jumpToCell = useCallback((cellId) => {
    const idx = cellIndexById.get(cellId)
    if (idx != null && onScrollToCell) onScrollToCell(idx)
  }, [cellIndexById, onScrollToCell])

  // Highlight variables touched in the most recent execution. Baseline is captured
  // once on first load so nothing is badged until the user actually runs something
  // this session (avoids lighting up everything after a checkpoint restore).
  const maxExec = useMemo(
    () => entries.reduce((m, [, i]) => Math.max(m, i.last_exec || 0), 0),
    [entries],
  )
  const baselineExecRef = useRef(null)
  if (baselineExecRef.current === null && entries.length > 0) baselineExecRef.current = maxExec
  const badgeActive = maxExec > (baselineExecRef.current ?? maxExec)
  const badgeFor = (info) => {
    if (!badgeActive || (info.last_exec || 0) !== maxExec) return null
    return (info.defined_at || 0) === maxExec ? 'new' : 'changed'
  }

  // Invalidate cached rich detail when a variable's value changes. detailCache is
  // keyed by name only and never re-fetches once loaded, so a reassignment (e.g.
  // `df = <a different query>`) would otherwise keep showing the old schema. We
  // diff each variable's signature against the last render and evict changed ones;
  // if a changed variable is currently on screen we re-fetch its detail right away.
  const sigRef = useRef({})
  useEffect(() => {
    const stale = []
    const nextSigs = {}
    for (const [name, info] of entries) {
      const sig = varSignature(info)
      nextSigs[name] = sig
      const prevSig = sigRef.current[name]
      if (prevSig !== undefined && prevSig !== sig) stale.push(name)
    }
    sigRef.current = nextSigs
    if (stale.length === 0) return
    setDetailCache(prev => {
      if (!stale.some(n => prev[n])) return prev
      const next = { ...prev }
      for (const n of stale) delete next[n]
      return next
    })
    // Re-fetch immediately for any changed variable that's currently visible.
    for (const n of stale) {
      if ((n === expandedVar || n === viewerVar) && canFetch) {
        const info = variables[n]
        if (info && TABULAR_TYPES.has(info.type)) fetchDetail(n)
      }
    }
  }, [entries, variables, expandedVar, viewerVar, canFetch, fetchDetail])

  const filteredSorted = useMemo(() => {
    const q = search.trim().toLowerCase()
    let list = entries
    if (q) list = list.filter(([name, info]) =>
      name.toLowerCase().includes(q) || (info.type || '').toLowerCase().includes(q))
    const cmp = {
      name: (a, b) => a[0].localeCompare(b[0], undefined, { sensitivity: 'base' }),
      type: (a, b) => (a[1].type || '').localeCompare(b[1].type || '') || a[0].localeCompare(b[0]),
      size: (a, b) => sizeToBytes(b[1].size) - sizeToBytes(a[1].size),
    }[sortBy]
    return [...list].sort(cmp)
  }, [entries, search, sortBy])

  const groups = useMemo(() => {
    if (!grouped) return null
    const byKind = {}
    for (const entry of filteredSorted) {
      const k = kindOf(entry[1].type)
      ;(byKind[k] ||= []).push(entry)
    }
    return KIND_ORDER.filter(k => byKind[k]?.length).map(k => [k, byKind[k]])
  }, [grouped, filteredSorted])

  const renderRow = ([name, info]) => {
    const badge = badgeFor(info)
    const origin = originBadges.get(name)
    return (
    <div key={name} className="var-item">
      <div className="var-item-row" onClick={() => toggleExpand(name, info.type)}>
        <span className="var-expand-icon">
          {expandedVar === name ? <IconChevronDown width={10} height={10} /> : <IconChevronRight width={10} height={10} />}
        </span>
        <span className={`var-type-icon ${getTypeColor(info.type)}`}>{getTypeIcon(info.type)}</span>
        <span className="var-name">{name}</span>
        {origin && (origin.inUse ? (
          // Compact badge in the row; the full "used by" line is under the row.
          <span className="var-origin-badge var-origin-deleted var-origin-in-use" title={origin.title}
                role="img" aria-label={`${origin.label}: ${origin.title}`}>
            deleted cell
          </span>
        ) : (
          <span className={`var-origin-badge var-origin-${origin.kind}`} title={origin.title}>
            {origin.label}<span className="lineage-sr-only">: {origin.title}</span>
          </span>
        ))}
        {badge && <span className={`var-badge-dot var-badge-${badge}`} title={badge === 'new' ? 'Created in the last run' : 'Changed in the last run'} />}
        <span className="var-type">{info.type}</span>
        {/* Inline value for SCALARS (int/float/bool/str/None) right on the row, so
            you don't have to expand to read it — like a debugger's variables view.
            Tabular/collection types use the `shape` slot instead (they have no
            single scalar value worth inlining). `info.value` is the executor's
            short repr (<=100 chars). */}
        {info.shape
          ? <span className="var-shape">{info.shape}</span>
          : (SCALAR_TYPES.has(info.type) && info.value
              ? (info.type === 'str' && String(info.value).endsWith('...')
                  // A long string is truncated inline by the executor (short repr
                  // capped at ~100 chars with a trailing "..."). Make the value a
                  // click target that opens the FULL string in the detail modal,
                  // without toggling the row's expand. Short strings stay plain.
                  ? <span
                      className="var-inline-value var-inline-value-clickable"
                      title="Click to view the full string"
                      onClick={(e) => { e.stopPropagation(); openViewer(name, info.type) }}
                    >{info.value}</span>
                  : <span className="var-inline-value" title={info.value}>{info.value}</span>)
              : null)}
      </div>
      {origin?.inUse && (
        // Visual duplicate of the badge's aria-label (label + title), so hidden
        // from assistive tech to avoid reading it twice.
        <div className="var-origin-usedby" title={origin.title} aria-hidden="true">
          {origin.label}
        </div>
      )}
      {expandedVar === name && (
        <div className="var-detail">
          {info.size && (
            <div className="var-detail-row">
              <span className="var-detail-label">Size</span>
              <span className="var-detail-value">{info.size}</span>
            </div>
          )}
          {/* Shape intentionally omitted here — already shown in the row header. */}

          {/* Provenance: jump to the cell that created / last modified this variable */}
          {origin?.kind === 'deleted' && !cellLabel(info.defined_by) && (
            <div className="var-detail-row">
              <span className="var-detail-label">Defined in</span>
              <span className="var-detail-value">A deleted cell (still in memory)</span>
            </div>
          )}
          {cellLabel(info.defined_by) && (
            <div className="var-detail-row">
              <span className="var-detail-label">Defined in</span>
              <button className="var-prov-link" onClick={() => jumpToCell(info.defined_by)} title="Jump to the cell that created this variable">
                {cellLabel(info.defined_by)}
              </button>
            </div>
          )}
          {info.last_cell && info.last_cell !== info.defined_by && cellLabel(info.last_cell) && (
            <div className="var-detail-row">
              <span className="var-detail-label">Last modified in</span>
              <button className="var-prov-link" onClick={() => jumpToCell(info.last_cell)} title="Jump to the cell that last modified this variable">
                {cellLabel(info.last_cell)}
              </button>
            </div>
          )}

          {/* Column schema (DataFrame/Series) — lazily fetched */}
          {TABULAR_TYPES.has(info.type) && (() => {
            const d = detailCache[name]
            if (d?.loading) return <div className="var-schema-loading">Loading schema…</div>
            const schema = d?.data?.schema
            if (!schema || schema.length === 0) return null
            return (
              <div className="var-schema">
                <div className="var-schema-head">
                  <span className="var-schema-title">Columns</span>
                  <span className="var-schema-count">{schema.length}</span>
                </div>
                {schema.slice(0, 30).map((c, i) => (
                  <div key={i} className="var-schema-row">
                    <span className="var-schema-col" title={c.column}>{c.column}</span>
                    <span className="var-schema-dtype" title={c.display_dtype && c.display_dtype !== c.dtype ? `pandas dtype: ${c.dtype}` : undefined}>{c.display_dtype || c.dtype}</span>
                    <span className="var-schema-meta">
                      {c.null_pct > 0 ? `${c.null_pct}% null` : 'no nulls'}
                      {c.unique != null ? ` · ${c.unique.toLocaleString()} uniq` : ''}
                    </span>
                  </div>
                ))}
                {schema.length > 30 && <div className="var-schema-more">… {schema.length - 30} more columns</div>}
              </div>
            )
          })()}

          {/* Inline preview only for non-tabular types — DataFrames/Series/arrays
              have the column schema above + the "View data" grid, so the small
              head(3) preview would just be redundant clutter. */}
          {!TABULAR_TYPES.has(info.type) && (
            <div className="var-detail-preview">
              <VariablePreviewRenderer info={info} />
            </div>
          )}

          <div className="var-actions">
            {TABULAR_TYPES.has(info.type) ? (
              <button className="var-action-btn var-action-view" onClick={() => openViewer(name, info.type)} title="Open full data grid">
                <IconTable width={11} height={11} /> View data
              </button>
            ) : (
              // Non-tabular values (str, dict, list, …) open the same detail modal,
              // which shows the FULL value — the panel's inline preview truncates
              // long strings/reprs (executor caps info.value at ~100 chars), so this
              // is the way to read a long string, big dict, etc. in full.
              <button className="var-action-btn var-action-view" onClick={() => openViewer(name, info.type)} title="View the full value">
                <IconCode width={11} height={11} /> View full value
              </button>
            )}
            {onInsertCode && (['DataFrame', 'Series'].includes(info.type) ? (
              <>
                <button className="var-action-btn" onClick={() => onInsertCode(`${name}.describe()`)} title="Statistical summary"><IconBarChart width={11} height={11} /> Describe</button>
                <button className="var-action-btn" onClick={() => onInsertCode(`${name}.head(10)`)} title="First 10 rows"><IconTable width={11} height={11} /> Head</button>
                <button className="var-action-btn" onClick={() => onInsertCode(`import matplotlib.pyplot as plt\n\n${name}.plot(figsize=(10, 5), title='${name}')\nplt.tight_layout()\nplt.show()`)} title="Quick visualization"><IconChartLine width={11} height={11} /> Plot</button>
                <button className="var-action-btn" onClick={() => onInsertCode(`print(f"Shape: {${name}.shape}")\nprint(f"\\nDtypes:\\n{${name}.dtypes}")\nprint(f"\\nNull counts:\\n{${name}.isnull().sum()}")\nprint(f"\\nMemory: {${name}.memory_usage(deep=True).sum() / 1024:.1f} KB")`)} title="Data quality profile"><IconSparkles width={11} height={11} /> Profile</button>
                <button className="var-action-btn" onClick={() => onInsertCode(`${name}.info()`)} title="Column info"><IconBraces width={11} height={11} /> Info</button>
              </>
            ) : info.type === 'ndarray' ? (
              <>
                <button className="var-action-btn" onClick={() => onInsertCode(`print(f"Shape: {${name}.shape}, Dtype: {${name}.dtype}")\nprint(f"Min: {${name}.min():.4f}, Max: {${name}.max():.4f}, Mean: {${name}.mean():.4f}")`)} title="Array stats"><IconBarChart width={11} height={11} /> Stats</button>
                <button className="var-action-btn" onClick={() => onInsertCode(`import matplotlib.pyplot as plt\nplt.hist(${name}.flatten(), bins=30)\nplt.title('${name} distribution')\nplt.show()`)} title="Histogram"><IconChartLine width={11} height={11} /> Hist</button>
              </>
            ) : (
              <button className="var-action-btn" onClick={() => onInsertCode(`print(${name})`)} title="Print value"><IconCode width={11} height={11} /> Print</button>
            ))}
            {onDeleteVariable && (
              <button
                className="var-action-btn var-action-delete"
                onClick={() => onDeleteVariable(name)}
                title={`Delete '${name}' from the session namespace`}
              ><IconTrash width={11} height={11} /> Delete</button>
            )}
          </div>
        </div>
      )}
    </div>
    )
  }



  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Variables</span>
        <span className="sidebar-panel-count">{entries.length}</span>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      {activeTab && <div className="sidebar-scope-pill"><IconNotebook width={12} height={12} /> {activeTab.name}</div>}

      {(untrackedCount > 0 || cleanup?.error || cleanup?.notice) && (
        <div className="var-cleanup-bar">
          {untrackedCount > 0 && (
            <button
              type="button"
              className="var-action-btn var-action-delete var-cleanup-btn"
              onClick={startCleanup}
              disabled={!canFetch || cleanup?.phase === 'loading' || cleanup?.phase === 'deleting'}
              title={canFetch
                ? 'Review and delete variables that no cell produces (asks for confirmation)'
                : 'Connect the notebook to a VM to clean up variables'}
            >
              <IconTrash width={11} height={11} />
              <span className="var-cleanup-label">
                {cleanup?.phase === 'loading'
                  ? 'Checking untracked variables…'
                  : `Clean up ${untrackedCount} untracked variable${untrackedCount === 1 ? '' : 's'}`}
              </span>
            </button>
          )}
          {cleanup?.error && <div className="var-cleanup-msg var-cleanup-error" role="alert">{cleanup.error}</div>}
          {cleanup?.notice && <div className="var-cleanup-msg" role="status">{cleanup.notice}</div>}
        </div>
      )}

      {entries.length > 0 && (
        <div className="var-controls">
          <div className="var-search">
            <IconSearch width={13} height={13} />
            <input
              className="var-search-input"
              type="text"
              placeholder="Filter variables…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            {search && <button className="var-search-clear" onClick={() => setSearch('')} title="Clear"><IconX width={10} height={10} /></button>}
          </div>
          <select className="var-sort-select" value={sortBy} onChange={(e) => setSortBy(e.target.value)} title="Sort by">
            <option value="name">Name</option>
            <option value="type">Type</option>
            <option value="size">Size</option>
          </select>
          <button
            className={`var-group-btn ${grouped ? 'active' : ''}`}
            onClick={() => setGrouped(g => !g)}
            title="Group by kind"
          ><IconBraces width={12} height={12} /></button>
        </div>
      )}

      <div className="sidebar-panel-body">
        {entries.length === 0 && (
          <PanelState
            variant="empty"
            icon={<IconBraces width={24} height={24} />}
            message="No variables yet"
            hint="Execute a cell to see variables here."
          />
        )}
        {entries.length > 0 && filteredSorted.length === 0 && (
          <PanelState variant="empty" icon={<IconBraces width={24} height={24} />} message={`No variables match “${search}”`} />
        )}

        {grouped
          ? groups.map(([kind, items]) => {
              const KindIcon = KIND_ICONS[kind] || IconPackage
              const isOpen = collapsedGroups[kind] !== true
              return (
                <div key={kind}>
                  <div
                    className="sidebar-subheader sidebar-subheader-toggle"
                    onClick={() => toggleGroup(kind)}
                    title={isOpen ? `Collapse ${kind}` : `Expand ${kind}`}
                  >
                    <KindIcon width={12} height={12} />
                    <span>{kind}</span>
                    <span className="sidebar-subheader-count">{items.length}</span>
                    <span className="sidebar-subheader-chevron">{isOpen ? '▾' : '▸'}</span>
                  </div>
                  {isOpen && items.map(renderRow)}
                </div>
              )
            })
          : filteredSorted.map(renderRow)}
        {/* Environment variables & secrets now live in the dedicated
            "Secrets & Env" side panel — not shown here. */}
      </div>

      {/* Portaled to <body> (like Modal) so no sidebar ancestor clips the fixed overlay. */}
      {(cleanup?.phase === 'confirm' || cleanup?.phase === 'deleting') && cleanup.plan && createPortal(
        <ConfirmModal
          title={cleanup.plan.title}
          message={cleanup.plan.message}
          confirmLabel={cleanup.phase === 'deleting' ? 'Deleting…' : cleanup.plan.confirmLabel}
          confirmDanger
          onConfirm={cleanup.phase === 'deleting' ? undefined : confirmCleanup}
          onCancel={() => setCleanup(null)}
        >
          {cleanup.plan.names.length > 0 && (
            <ul className="var-cleanup-list" aria-label="Variables to delete">
              {cleanup.plan.names.map(n => (
                <li key={n} className="var-cleanup-item">
                  <code className="var-cleanup-name" title={n}>{n}</code>
                </li>
              ))}
            </ul>
          )}
          {cleanup.plan.internalNote && (
            <p className="var-cleanup-internal" title={cleanup.plan.internalNote}>{cleanup.plan.internalNote}</p>
          )}
        </ConfirmModal>,
        document.body,
      )}

      {/* Full data-grid viewer modal */}
      {viewerVar && (
        <VariableDetailModal
          name={viewerVar}
          endpoint={activeTab?.microvmEndpoint}
          sessionId={activeTab?.sessionId}
          onClose={() => setViewerVar(null)}
        />
      )}
    </div>
  )
}
