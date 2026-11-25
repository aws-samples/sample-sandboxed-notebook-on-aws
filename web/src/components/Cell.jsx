import { useState, useEffect, useMemo, useRef, useCallback, memo } from 'react'
import Markdown from './Markdown'
import MarkdownCell from './MarkdownCell'
import CellEditor from './CellEditor'
import { IconPlay, IconTrash, IconX, IconStop, IconChevronDown, IconChevronRight, IconGripVertical, IconEraser, IconCode, IconDatabase, IconPencil, IconExpand } from './Icons'
import ParamWidgets from './ParamWidgets'
import CellOutput from './CellOutput'
import { DagIssueBadge, LineageTrigger } from './LineageDrawer'
import { effectiveStatus } from '../services/depGraph'
import { staleReasonText } from '../services/lineage'
import CellFocusOverlay from './CellFocusOverlay'
import CellCollapsedStrip from './CellCollapsedStrip'
import VariableDetailModal from './VariableDetailModal'
import DiffCell from './DiffCell'
import { FixVerificationBadge, FixVerificationNotice } from './FixVerification'
import { useCellAI } from '../hooks/useCellAI'
import { useCellExplain } from '../hooks/useCellExplain'
import { ExplainButton } from './ExplainPanel'
import { requestPanelOpen as requestExplainPanelOpen } from '../services/explainPanel'
import { exportCellOutputHtml, exportCellOutputMarkdown, copyCellOutput, copyErrorWithContext } from '../services/exportCellOutput'
import { getTypeIcon as getVarTypeIcon, getTypeColor as getVarTypeColor, kindOf as varKindOf, KIND_ORDER as VAR_KIND_ORDER } from '../utils/varTypeMeta'
import './Cell.css'
import './CellEditor.css'
import { hasImageOutput } from '../services/cellOutput'
import { withPersistedImage } from '../services/cellImageApi'
import { isCellCollapsed, setCellCollapsed } from '../services/cellCollapse'

// Extract the 1-indexed error line for the editor gutter, or null if unknown.
// Resolution order:
//   1. Discrete backend field cell.errorLineno (Python executor sets this).
//   2. Python-style "(line N)" in the message (older VMs / fallback).
//   3. SQL-engine style "line R:C" (e.g. Athena/DuckDB: "line 5:37 Column ...").
// Returns null when no line can be resolved — callers use that to hide the marker
// AND the "Line" toggle, so we never offer a control that can't do anything.
function deriveErrorLine(cell) {
  if (!cell || cell.status !== 'error') return null
  if (typeof cell.errorLineno === 'number' && cell.errorLineno > 0) return cell.errorLineno
  const err = cell.error || ''
  const paren = /\(line (\d+)\)/.exec(err)
  if (paren) return parseInt(paren[1], 10)
  // SQL "line R:C" (row:col). Take the row. Word-boundary + not immediately part of
  // "(line N)" which is already handled above.
  const sql = /\bline (\d+):\d+/.exec(err)
  if (sql) return parseInt(sql[1], 10)
  return null
}

// Per-cell "variables defined here" chips reuse the SAME type icon/color/kind
// helpers as the Variables side panel (shared util) so the two always agree.

// Derive a default variable name from SQL (based on the primary table being queried)
function deriveSqlVarName(sql) {
  if (!sql || !sql.trim()) return 'result'
  // Try to extract table name from FROM clause
  const fromMatch = sql.match(/\bFROM\s+dynamodb\."?([a-zA-Z_][\w\-]*)"?/i)
    || sql.match(/\bFROM\s+'\/tmp\/([^']+)'/i)
    || sql.match(/\bFROM\s+read_(?:csv|json|parquet)\('[^']*\/([^'/]+)'\)/i)
    || sql.match(/\bFROM\s+[a-zA-Z_]\w*\.([a-zA-Z_]\w*)/i)
    || sql.match(/\bFROM\s+([a-zA-Z_]\w*)/i)
  if (fromMatch) {
    const raw = fromMatch[1] || 'result'
    // Sanitize: remove extension, replace non-identifier chars with underscore
    const cleaned = raw.replace(/\.\w+$/, '').replace(/[^a-zA-Z0-9_]/g, '_').replace(/^_+|_+$/g, '')
    if (cleaned && /^[a-zA-Z_]/.test(cleaned)) return cleaned
  }
  return 'result'
}

// Ticking timer component for running cells. Computes elapsed from the cell's
// real startedAt (epoch ms) so it survives a tab-switch remount instead of
// resetting to 0. Falls back to mount-time if startedAt is missing.
function ElapsedTimer({ startedAt }) {
  const startRef = useRef(startedAt || Date.now())
  const [elapsed, setElapsed] = useState((( Date.now() - startRef.current) / 1000).toFixed(1))
  useEffect(() => {
    startRef.current = startedAt || startRef.current
    const tick = () => setElapsed(((Date.now() - startRef.current) / 1000).toFixed(1))
    tick()
    const interval = setInterval(tick, 500)
    return () => clearInterval(interval)
  }, [startedAt])
  return <span className="cell-timer">{elapsed}s</span>
}

export default memo(function Cell({
  cell,
  index,
  typeOrdinal,
  isConnected,
  catalogLoaded = true,
  isActive,
  isFlashing,
  isDragOver,
  hasSearchMatch,
  onFocus,
  onExecute,
  onInterrupt,
  onCodeChange,
  onOutputVarChange,
  onAddBelow,
  onSetAiExplanation,
  onDelete,
  onClearOutput,
  onFormat,
  isFormatting = false,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
  searchQuery,
  searchActiveOccurrence,
  cellBadgeById: cellBadgeByIdProp,
  getNotebookContext,
  notebookName,
  microvmId,
  sessionId,
  aiAvailable,
  variables = {},
  // This cell's node in the stored DAG (stale_reason, reads) and its issue
  // summary ({count, amber, ariaLabel, title}) — both from Notebook.jsx.
  dagNode = null,
  dagIssues = null,
  microvmEndpoint,
  dataSources = null,
  // "How was this computed?" — trust level for the Output-header trigger dot,
  // the opener, and Highlight-in-notebook state (step number / dimmed).
  lineageTrust = null,
  onOpenLineage,
  lineageStep = null,
  lineageDim = false,
  // "Explain this code": the saved notebook's id and a stable accessor for the
  // upstream-code signature (cache key) from the stored DAG.
  notebookId = null,
  getExplainUpstreamSig = null,
}) {
  // Whole-cell collapse: the cell shrinks to a one-row strip (preview, line count,
  // status hint) and hides code, output, variables and notes. Kept for the page
  // session by cell id (services/cellCollapse.js), so a tab switch keeps it.
  const [collapsed, setCollapsedState] = useState(() => isCellCollapsed(cell.id))
  const setCollapsed = useCallback((v) => { setCellCollapsed(cell.id, v); setCollapsedState(v) }, [cell.id])
  const [outputCollapsed, setOutputCollapsed] = useState(false)
  const [varsCollapsed, setVarsCollapsed] = useState(false)
  // Focus / "expand" mode: render this cell's editor + output in a large blurred
  // full-screen overlay (code + SQL cells only). Presentational — all edits/runs
  // still flow through the same handlers below.
  const [focused, setFocused] = useState(false)
  // Inline editing of the AI comment (✨ note). When saved with changes we mark the
  // cell aiExplanationEdited so a "Manually edited" badge shows next to the AI badge.
  const [editingExplain, setEditingExplain] = useState(false)
  const [explainDraft, setExplainDraft] = useState('')

  // Variable names for autocomplete
  const variableNames = useMemo(() => Object.keys(variables || {}), [variables])

  // --- Per-cell variables (Option A): what this cell defined/modified. ---
  // Derived from provenance (defined_by / modified_by / last_cell) so it survives
  // reload/restore and stays consistent with the Variables panel. A chip is
  // "openable" only when this cell still owns the variable's current value
  // (last_cell === cell.id); otherwise the value was replaced by a later cell, so
  // we grey it out and show a jump link to the cell that owns the value now.
  const [cellViewerVar, setCellViewerVar] = useState(null)

  // Per-type ordinal badge (PY-4 / SQL-2 / MD-1) for each cell id — the SAME
  // scheme the cell badge, Outline, and Variables panel use, so a "replaced by"
  // jump link reads identically to the target cell's badge. The map is computed
  // ONCE in Notebook.jsx (memoized on an id:type signature) and passed down, so it
  // no longer recomputes O(N) inside every cell on every keystroke (was O(N²)).
  const cellBadgeById = useMemo(() => cellBadgeByIdProp || new Map(), [cellBadgeByIdProp])
  // This cell's own badge (for export titles/filenames), matching what's shown on it.
  const thisCellBadge = cellBadgeById.get(String(cell.id)) || `PY-${index + 1}`
  // Edited / dataflow-stale indicators (design §8.1): the live code vs the last run
  // is the only edited signal; staleness is the stored node's stale_reason.
  const editedSinceRun = effectiveStatus({ code: cell.code, lastExecutedCode: cell.lastExecutedCode }, dagNode) === 'edited'
  const dagStaleReason = dagNode ? dagNode.stale_reason : null

  const cellVars = useMemo(() => {
    if (cell.type === 'sql' || cell.type === 'markdown') return []
    const cid = String(cell.id)
    const out = []
    for (const [name, info] of Object.entries(variables || {})) {
      const definedHere = String(info.defined_by) === cid
      const modifiedHere = Array.isArray(info.modified_by) && info.modified_by.some(c => String(c) === cid)
      const ownsNow = String(info.last_cell) === cid
      if (!definedHere && !modifiedHere && !ownsNow) continue
      const ownerBadge = cellBadgeById.get(String(info.last_cell))
      out.push({
        name,
        type: info.type,
        shape: info.shape,
        preview: info.preview,            // short value repr (scalars) for inline display
        varKind: varKindOf(info.type),    // Data | Collections | Scalars | Other (matches side panel)
        // Inspectable = anything EXCEPT a plain scalar that already shows in full
        // inline. The detail modal renders a DataGrid for tabular values, a JSON
        // tree for dict/list/collections, and the full text otherwise. Most scalars
        // (int/float/bool/None/short str) show their whole value on the chip, so
        // they're inert — EXCEPT a long string, whose inline preview is TRUNCATED by
        // the executor (short repr capped with a trailing "..."): those stay
        // clickable so the modal can show the full string.
        inspectable: varKindOf(info.type) !== 'Scalars'
          || (info.type === 'str' && String(info.preview ?? info.value ?? '').endsWith('...')),
        kind: definedHere ? 'defined' : 'modified',
        ownsNow,
        replacedById: info.last_cell,
        replacedByLabel: ownsNow || ownerBadge == null ? null : ownerBadge,
      })
    }
    // Defined first, then modified; alphabetical within each group.
    out.sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : (a.kind === 'defined' ? -1 : 1)))
    return out
  }, [variables, cell.id, cell.type, cellBadgeById])

  // Group the chips by coarse kind (Data / Collections / Scalars / Other) in the
  // SAME order the Variables side panel uses, so the two surfaces read identically.
  const cellVarGroups = useMemo(() => {
    const byKind = {}
    for (const v of cellVars) (byKind[v.varKind] ||= []).push(v)
    return VAR_KIND_ORDER.filter(k => byKind[k]?.length).map(k => [k, byKind[k]])
  }, [cellVars])

  const jumpToCell = (targetId) => {
    const el = document.querySelector(`[data-cell-id="${targetId}"]`)
    // Align the TOP of the target cell to the top of the viewport ('start'), not
    // its center — otherwise a tall cell lands mid-code and the user has to scroll
    // up to find the beginning. 'nearest' would no-op if the cell is already partly
    // visible, so use 'start' to guarantee the cell head is at the top.
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // Datasource identifiers for autocomplete (table names, file paths, schemas)
  const dataSourceNames = useMemo(() => {
    if (!dataSources) return { items: [], schemas: {} }
    const items = []
    const schemas = {} // schema_name → [table_names]

    // S3 files — suggest the read_csv('s3://...') pattern
    if (dataSources.s3) {
      dataSources.s3.forEach(f => {
        const uri = f.uri || `s3://${f.bucket}/${f.key}`
        items.push({ label: uri, type: 's3', detail: 'S3 file' })
      })
    }

    // Local sandbox files — suggest '/tmp/filename'
    // (uploadedFiles come separately but we get them from the sidebar sync)

    // DynamoDB tables — schema is "dynamodb", table names need quotes
    if (dataSources.dynamodb) {
      items.push({ label: 'dynamodb', type: 'schema', detail: 'DynamoDB schema' })
      const dynTables = []
      dataSources.dynamodb.forEach(t => {
        const name = t.name || t.table_name || t
        const label = typeof name === 'string' ? name : String(name)
        dynTables.push(label)
        items.push({ label: `dynamodb."${label}"`, type: 'dynamodb', detail: 'DynamoDB table' })
      })
      schemas['dynamodb'] = dynTables
    }

    // Athena tables — schema is the database name (from API response)
    if (dataSources.athena && dataSources.athena.length > 0) {
      const dbName = dataSources.athena[0]?.database
      if (dbName) {
        items.push({ label: dbName, type: 'schema', detail: 'Athena database' })
        const athenaTables = []
        dataSources.athena.forEach(t => {
          const name = t.name || t.table_name || t
          const label = typeof name === 'string' ? name : String(name)
          athenaTables.push(label)
          items.push({ label, type: 'athena', detail: 'Athena table' })
          items.push({ label: `${dbName}.${label}`, type: 'athena', detail: 'Athena (qualified)' })
        })
        schemas[dbName] = athenaTables
      } else {
        // No database name — just add raw table names
        dataSources.athena.forEach(t => {
          const name = t.name || t.table_name || t
          const label = typeof name === 'string' ? name : String(name)
          items.push({ label, type: 'athena', detail: 'Athena table' })
        })
      }
    }

    // Aurora PostgreSQL — only in the generic `sources` list (no legacy grouped
    // array), source_id = 'database.schema.table' (e.g. microvm_db.public.v_nb_runs).
    // SQL is 3-part database.schema.table, so we key the completion schema by the
    // DATABASE: typing 'microvm_db.' suggests the schema-qualified views
    // ('public.v_nb_*'), matching the Data Sources panel hierarchy + the SQL snippet.
    const auroraSources = (dataSources.sources || []).filter(s => s.source_type === 'aurora')
    if (auroraSources.length > 0) {
      const byDb = {}  // database → ['public.v_nb_x', ...]
      for (const s of auroraSources) {
        const parts = (s.source_id || '').split('.')
        if (parts.length < 3) continue
        const db = parts[0]
        const schemaTable = parts.slice(1).join('.')  // 'public.v_nb_x'
        ;(byDb[db] = byDb[db] || []).push(schemaTable)
        // Fully-qualified name for word-based completion (typing 'microvm_db').
        items.push({ label: s.source_id, type: 'aurora', detail: 'Aurora (qualified)' })
      }
      for (const [db, tbls] of Object.entries(byDb)) {
        items.push({ label: db, type: 'schema', detail: 'Aurora database' })
        // 'microvm_db.' → suggest the schema-qualified views (public.v_nb_*).
        schemas[db] = tbls
      }
    }

    // Build column map from catalog entries (for column-level autocomplete)
    // columns: { "table_name": ["col1", "col2", ...], "dynamodb.table": [...], ... }
    const columns = {}
    if (dataSources._catalog?.entries) {
      for (const entry of dataSources._catalog.entries) {
        if (entry.status === 'discovered' && entry.columns?.length > 0) {
          const colNames = entry.columns.map(c => c.name)
          // Key by display_name (bare table name) for easy lookup
          columns[entry.display_name] = colNames
          // Also key by source_id for qualified references
          columns[entry.source_id] = colNames
        }
      }
    }
    // Also extract Athena columns from the basic /datasources response (always available)
    if (dataSources.athena) {
      dataSources.athena.forEach(t => {
        if (t.columns?.length > 0 && !columns[t.name]) {
          columns[t.name] = t.columns.map(c => c.name || c)
        }
      })
    }

    return { items, schemas, columns }
  }, [dataSources])

  // AI + execution routing (smart execute / explain / fix / generate) lives in a hook.
  const {
    aiResult, setAiResult, generating, editorVersion, bumpEditorVersion,
    smartExecute, handleAiFix, handleAiCancel, handleApplyFix,
  } = useCellAI({
    cell, index, isConnected, aiAvailable, microvmId, sessionId,
    variables, dataSources, getNotebookContext,
    onExecute, onCodeChange, onClearOutput,
  })
  const explain = useCellExplain({
    cell, notebookId, getUpstreamSig: getExplainUpstreamSig, onSetAiExplanation,
  })
  const explainBadgeToId = useMemo(() => {
    const m = new Map()
    for (const [id, label] of cellBadgeById) m.set(label, id)
    return m
  }, [cellBadgeById])

  // Per-cell version revert (Version History → "Apply this cell to notebook"):
  // App.jsx handles the same 'notebook-revert-cell' event by patching this cell's
  // code in state, but the already-mounted CodeMirror won't re-read the new code
  // without a remount. Bump editorVersion here so the editor remounts and shows the
  // reverted code immediately — the SAME mechanism Apply Fix / focus mode use.
  useEffect(() => {
    const onRevert = (e) => {
      if (String(e.detail?.cellId) !== String(cell.id)) return
      // Defer to the next tick so the state patch (cell.code) lands before we
      // remount — the fresh editor then mounts with the reverted code as its doc.
      setTimeout(() => bumpEditorVersion(), 0)
    }
    window.addEventListener('notebook-revert-cell', onRevert)
    return () => window.removeEventListener('notebook-revert-cell', onRevert)
  }, [cell.id, bumpEditorVersion])

  // Programmatic AI-fix trigger. The Outline panel's "Fix it" button (and any other
  // remote caller) dispatches a `notebook-fix-cell` window event with this cell's id;
  // we run the same handleAiFix() the in-cell ⚡ Fix button uses. Guarded to this
  // cell, to executable (code/SQL) cells that actually have an error, and to a
  // connected + AI-available session — mirroring when the in-cell button is shown.
  useEffect(() => {
    const onFixRequest = (e) => {
      if (e.detail?.cellId !== cell.id) return
      if (cell.type === 'markdown') return
      if (!cell.error || !isConnected || !aiAvailable) return
      if (aiResult?.loading) return  // a fix/explain is already in flight
      // The fix preview renders inside the cell: open a collapsed cell so it shows.
      setCollapsed(false)
      handleAiFix()
    }
    window.addEventListener('notebook-fix-cell', onFixRequest)
    return () => window.removeEventListener('notebook-fix-cell', onFixRequest)
  }, [cell.id, cell.type, cell.error, isConnected, aiAvailable, aiResult?.loading, handleAiFix, setCollapsed])

  // Error-line marker: the line to highlight in the editor (or null). Cleared as
  // soon as the user edits the code (the stale line no longer means anything) — we
  // track whether the code changed since the errored run via lastExecutedCode.
  const codeEditedSinceError = cell.lastExecutedCode != null && cell.code !== cell.lastExecutedCode
  const errorLine = codeEditedSinceError ? null : deriveErrorLine(cell)
  // Secondary call-chain lines: when the error surfaced through a helper defined in
  // THIS cell (e.g. df.apply(decide) → decide()), the backend reports several cell
  // frames. The deepest is `errorLine` (the crash point, primary marker); the others
  // are the call sites leading to it (secondary markers). Same freshness gate.
  const errorChainLines = (codeEditedSinceError || !Array.isArray(cell.errorFrames))
    ? []
    : cell.errorFrames
        .map(f => (typeof f?.lineno === 'number' ? f.lineno : null))
        .filter(n => n && n !== errorLine)

  // Lint squiggles: show the ruff diagnostics from the last lint pass, but only
  // while the code still matches what we linted (cell.lintCode). Once the user
  // edits, the cell-relative positions are stale, so we drop them until the next
  // lint pass — same freshness gate as the error line above.
  const lintDiagnostics = (cell.lint && cell.lintCode != null && cell.code === cell.lintCode)
    ? cell.lint
    : null

  // User toggle: show the red gutter marker (persisted). The full-line highlight
  // always shows on error; only the gutter dot is gated by this preference.
  const [showErrorGutter, setShowErrorGutter] = useState(() => {
    try { return localStorage.getItem('microvm-error-gutter') !== 'off' } catch { return true }
  })
  useEffect(() => {
    const onToggle = (e) => setShowErrorGutter(!!e.detail?.on)
    window.addEventListener('microvm-error-gutter-changed', onToggle)
    return () => window.removeEventListener('microvm-error-gutter-changed', onToggle)
  }, [])

  const statusColor =
    cell.status === 'running' ? 'cell-running'
    : cell.status === 'success' ? 'cell-success'
    : cell.status === 'interrupted' ? 'cell-interrupted'
    : cell.status === 'error' ? 'cell-error'
    : 'cell-idle'

  // --- MARKDOWN CELL ---
  if (cell.type === 'markdown') {
    return (
      <MarkdownCell
        cell={cell}
        typeOrdinal={typeOrdinal}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed(!collapsed)}
        isActive={isActive}
        isFlashing={isFlashing}
        isDragOver={isDragOver}
        hasSearchMatch={hasSearchMatch}
        searchQuery={searchQuery}
        searchActiveOccurrence={searchActiveOccurrence}
        onFocus={onFocus}
        onCodeChange={onCodeChange}
        onAddBelow={onAddBelow}
        onDelete={onDelete}
        onDragStart={onDragStart}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onDragEnd={onDragEnd}
      />
    )
  }

  // --- CODE CELL ---

  return (
    <div
      className={`cell ${statusColor} ${isActive ? 'cell-active' : ''} ${isFlashing ? 'cell-flash' : ''} ${isDragOver ? 'cell-drag-over' : ''} ${hasSearchMatch ? 'cell-search-match' : ''} ${cell.type === 'sql' ? 'cell-sql' : ''}${collapsed ? ' cell-collapsed' : ''}${typeof lineageStep === 'number' ? ' cell-lineage-step' : ''}${lineageDim ? ' cell-lineage-dim' : ''}`}
      data-cell-id={cell.id}
      data-lineage-step={typeof lineageStep === 'number' ? lineageStep : undefined}
      onClick={onFocus}
      onDragOver={(e) => { e.preventDefault(); onDragOver?.() }}
      onDrop={(e) => { e.preventDefault(); onDrop?.() }}
      onDragEnd={onDragEnd}
    >
      <div
        className="cell-gutter"
        onDoubleClick={(e) => {
          // Double-click the left gutter to open focus mode — but not when the
          // double-click lands on an interactive child (drag handle / collapse btn),
          // so those keep their own behavior.
          if (e.target.closest('.cell-drag-handle, .cell-collapse-btn, .dag-issue-badge')) return
          e.stopPropagation()
          setFocused(true)
        }}
        title="Double-click to expand (focus mode)"
      >
        <span
          className="cell-drag-handle"
          draggable
          onDragStart={onDragStart}
          title="Drag to reorder"
        >
          <IconGripVertical width={12} height={12} />
        </span>
        <button
          type="button"
          className="cell-collapse-btn"
          onClick={() => setCollapsed(!collapsed)}
          title={collapsed ? 'Expand cell' : 'Collapse cell'}
          aria-label={collapsed ? 'Expand cell' : 'Collapse cell'}
          aria-expanded={!collapsed}
        >
          {collapsed ? <IconChevronRight width={12} height={12} /> : <IconChevronDown width={12} height={12} />}
        </button>
        {cell.executionNumber ? (
          <span className="cell-number" title="Kernel execution count">{`[${cell.executionNumber}]`}</span>
        ) : null}
        <span className={`cell-type-badge ${cell.type === 'sql' ? 'cell-type-sql' : cell.type === 'markdown' ? 'cell-type-md' : 'cell-type-code'}`} title={cell.type === 'sql' ? 'SQL cell' : cell.type === 'markdown' ? 'Markdown cell' : 'Python cell'}>
          {cell.type === 'sql'
            ? <><IconDatabase width={10} height={10} /><span className="cell-type-label">SQL{typeOrdinal ? `-${typeOrdinal}` : ''}</span></>
            : cell.type === 'markdown'
              ? <><IconPencil width={10} height={10} /><span className="cell-type-label">MD{typeOrdinal ? `-${typeOrdinal}` : ''}</span></>
              : <><IconCode width={10} height={10} /><span className="cell-type-label">PY{typeOrdinal ? `-${typeOrdinal}` : ''}</span></>
          }
        </span>
        {cell.status === 'running' && <ElapsedTimer startedAt={cell.startedAt} />}
        {/* Lint indicator — shows when the last lint pass flagged issues in THIS
            cell (and the code hasn't changed since). Disappears when a re-lint finds
            the cell clean (lint:[]) or the user edits it (freshness gate via
            lintDiagnostics). Amber, matching the inline squiggle color. */}
        {lintDiagnostics && lintDiagnostics.length > 0 && (
          <span
            className="cell-lint-badge"
            title={`${lintDiagnostics.length} lint issue${lintDiagnostics.length === 1 ? '' : 's'}:\n` +
              lintDiagnostics.map(d => `  ${d.code ? d.code + ': ' : ''}${d.message} (line ${d.line})`).join('\n')}
          >
            <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            <span className="cell-lint-badge-count">{lintDiagnostics.length}</span>
          </span>
        )}
        {editedSinceRun && cell.status !== 'running' && (
          <span className="cell-stale-badge" title="Code modified since last execution — re-run to update output">●</span>
        )}
        {/* Dataflow-stale cue (Tier 1 reactive): the stored DAG marks this cell
            stale (an upstream cell re-ran after it, or its effects are not in the
            session's memory). Distinct from the edit-stale badge above (which
            tracks code edits). Amber, informational only. */}
        {dagStaleReason && cell.status !== 'running' && !editedSinceRun && (
          <span
            className="cell-dataflow-stale-badge"
            title={staleReasonText(dagStaleReason, cellBadgeById)}
          >
            ●
          </span>
        )}
        {/* DAG issue badge (FR-5.5) — in the header so it shows on EVERY cell,
            including cells with no or collapsed output (mutators, exec() cells).
            Opens the lineage drawer, which lists the issues. */}
        <DagIssueBadge
          summary={dagIssues}
          onClick={(e) => { e.stopPropagation(); onOpenLineage?.(cell.id) }}
        />
      </div>

      <div className="cell-content">
        {/* Collapsed cell: one row with the first code line, the line count and a
            status hint (running / error / output summary). Click to expand. */}
        {collapsed && <CellCollapsedStrip cell={cell} onExpand={() => setCollapsed(false)} />}

        {/* Code editor.
            IMPORTANT: skip the inline ParamWidgets while this cell is FOCUSED — the
            focus overlay renders its own ParamWidgets, and mounting BOTH for the same
            cell makes them fight over cell.code: the inline instance re-syncs on the
            overlay's change and its own debounced rebuild clobbers the moved slider
            value back, so the cell re-runs with UNCHANGED code. That leaves the
            provenance clock un-advanced, so the reactive cascade sees no changed vars
            and never marks/re-runs downstream cells. One live instance = correct
            cascade in both inline and focus mode. */}
        {!collapsed && !focused && cell.type !== 'markdown' && cell.code && cell.code.includes('@param') && (
          <ParamWidgets
            code={cell.code}
            onCodeChange={onCodeChange}
            onExecute={smartExecute}
            sessionId={sessionId}
          />
        )}
        {/* SQL result-variable bar — full-width row above the editor */}
        {!collapsed && cell.type === 'sql' && (
          <div className="sql-output-var">
            <span className="sql-output-var-label">→</span>
            <input
              className="sql-output-var-input"
              type="text"
              value={cell.outputVariable || ''}
              onChange={(e) => onOutputVarChange && onOutputVarChange(e.target.value)}
              placeholder={deriveSqlVarName(cell.code)}
              title="Variable name for the query result (accessible in subsequent cells)"
              spellCheck={false}
            />
          </div>
        )}
        {!collapsed && (
          <div className="cell-input">
            <CellEditor
              key={editorVersion}
              code={cell.code}
              language={cell.type === 'sql' ? 'sql' : 'python'}
              placeholder={cell.type === 'sql'
                ? 'Write SQL or describe what you want... (Shift+Enter runs or generates)'
                : 'Type Python code or describe what you want in plain English... (Shift+Enter runs code or generates from NLP)'}
              onCodeChange={onCodeChange}
              onExecute={smartExecute}
              onFocus={onFocus}
              variables={variableNames}
              dataSources={dataSourceNames}
              sessionId={sessionId}
              searchQuery={searchQuery}
              searchActiveOccurrence={searchActiveOccurrence}
              errorLine={errorLine}
              errorChainLines={errorChainLines}
              errorMessage={cell.error || ''}
              showErrorGutter={showErrorGutter}
              onFixError={aiAvailable && isConnected && cell.error ? handleAiFix : null}
              lintDiagnostics={lintDiagnostics}
            />
            <div className="cell-actions">
              {cell.status === 'running' || generating ? (
                <button
                  className="cell-run-btn cell-stop-btn"
                  onClick={generating ? () => {} : onInterrupt}
                  title={generating ? 'Generating code...' : 'Stop execution'}
                >
                  {generating ? <span className="cell-gen-spinner" /> : <IconStop width={12} height={12} />}
                </button>
              ) : (
                <button
                  className="cell-run-btn"
                  onClick={smartExecute}
                  disabled={!isConnected || cell.status === 'running' || !catalogLoaded}
                  title={!catalogLoaded ? 'Loading data sources… available once the Data panel finishes loading' : 'Run cell (Shift+Enter)'}
                >
                  <IconPlay width={12} height={12} />
                </button>
              )}
              <button className="cell-action-btn cell-expand-btn" onClick={(e) => { e.stopPropagation(); setFocused(true) }} title="Expand cell (focus mode)">
                <IconExpand width={13} height={13} />
              </button>
              <button className="cell-action-btn" onClick={(e) => { e.stopPropagation(); onAddBelow('code') }} title="Add code cell below">
                <IconCode width={14} height={14} />
              </button>
              <button className="cell-action-btn cell-add-sql-btn" onClick={(e) => { e.stopPropagation(); onAddBelow('sql') }} title="Add SQL cell below">
                <IconDatabase width={12} height={12} />
              </button>
              <button className="cell-action-btn cell-add-md-btn" onClick={(e) => { e.stopPropagation(); onAddBelow('markdown') }} title="Add text cell below">
                M
              </button>
              {/* Clear output — lives in the toolbar (consistent with the other cell
                  actions) and only appears when this cell actually HAS output. */}
              {onClearOutput && (cell.output || cell.error || cell.html || hasImageOutput(cell)) && (
                <button className="cell-action-btn cell-clear-btn" onClick={(e) => { e.stopPropagation(); onClearOutput() }} title="Clear output">
                  <IconEraser width={13} height={13} />
                </button>
              )}
              {/* Format this cell. Python cells go through ruff, SQL cells through
                  sqlglot — both handled server-side by /format. Only for non-empty
                  code/SQL cells and only when connected (the VM does the formatting).
                  Unlike Clear output, it does NOT depend on the cell having run — you
                  format while writing. Spinner while the single-cell /format runs. */}
              {onFormat && (cell.type === 'code' || cell.type === 'sql') && (cell.code || '').trim() && (
                <button
                  className="cell-action-btn cell-format-btn"
                  onClick={(e) => { e.stopPropagation(); onFormat() }}
                  disabled={isFormatting}
                  title={cell.type === 'sql' ? 'Format this SQL cell (sqlglot)' : 'Format this cell (ruff)'}
                >
                  {isFormatting ? <span className="cell-gen-spinner" /> : (
                    <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="14" y2="12" /><line x1="4" y1="18" x2="18" y2="18" />
                    </svg>
                  )}
                </button>
              )}
              {/* Add a manual note — only when the cell has no note yet. Opens the
                  same editor the AI note uses, but tagged as user-authored. */}
              {!aiResult && (
                <button
                  className="cell-action-btn cell-add-note-btn"
                  onClick={(e) => {
                    e.stopPropagation()
                    setAiResult({ type: 'explain', content: '', loading: false, source: 'user' })
                    setExplainDraft('')
                    setEditingExplain(true)
                  }}
                  title="Add a note to this cell"
                >
                  <IconPencil width={13} height={13} />
                </button>
              )}
              {aiAvailable && (cell.type === 'code' || cell.type === 'sql') && (
                // Opens focus mode with the Explanation panel open and starts (or reuses) the explanation.
                <ExplainButton
                  explain={explain}
                  className="cell-action-btn"
                  onClick={() => {
                    requestExplainPanelOpen()
                    setFocused(true)
                    explain.run()
                  }}
                />
              )}
              {aiAvailable && cell.code?.trim() && cell.error && (
                <button
                  className="cell-action-btn cell-ai-action-btn cell-ai-fix-btn"
                  onClick={(e) => { e.stopPropagation(); handleAiFix() }}
                  disabled={aiResult?.loading}
                  title="Fix error with AI"
                >🔧</button>
              )}
              {/* Delete is LAST in the toolbar — a destructive action kept at the far
                  end (after the Fix button) so it's out of the way of the common
                  add/run/fix actions and less likely to be hit by accident. */}
              <button className="cell-action-btn cell-delete-btn" onClick={(e) => { e.stopPropagation(); onDelete() }} title="Delete cell">
                <IconTrash width={14} height={14} />
              </button>
            </div>
          </div>
        )}

        {!collapsed && (cell.output || cell.error || cell.html || hasImageOutput(cell)) && (
          <div className={`cell-output cell-output--result ${cell.error ? 'cell-output-error' : ''} ${outputCollapsed ? 'cell-output-collapsed' : ''}`}>
            {outputCollapsed ? (
              <div className="cell-output-collapse-bar" onClick={() => setOutputCollapsed(false)}>
                <IconChevronRight width={10} height={10} />
                <span>Output hidden — click to expand</span>
              </div>
            ) : (
              <>
                <div className="cell-output-collapse-bar" onClick={() => setOutputCollapsed(true)}>
                  <IconChevronDown width={10} height={10} />
                  <span className="cell-focus-section-title">Output</span>
                  <div className="cell-output-actions" onClick={(e) => e.stopPropagation()}>
                    <LineageTrigger
                      level={lineageTrust}
                      showDot
                      onClick={(e) => { e.stopPropagation(); onOpenLineage?.(cell.id) }}
                    />
                    <div className="cell-output-export-wrap">
                      <button className="cell-output-action-btn" title="Export output" onClick={(e) => {
                        e.stopPropagation()
                        const menu = e.currentTarget.nextElementSibling
                        menu.style.display = menu.style.display === 'block' ? 'none' : 'block'
                      }}>
                        <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Export
                      </button>
                      <div className="cell-output-export-menu" style={{ display: 'none' }}>
                        <button onClick={async () => exportCellOutputHtml(await withPersistedImage(cell), notebookName, thisCellBadge)}>HTML</button>
                        <button onClick={async () => exportCellOutputMarkdown(await withPersistedImage(cell), notebookName, thisCellBadge)}>Markdown</button>
                      </div>
                    </div>
                    {/* Clear moved to the cell toolbar (shown only when output exists). */}
                    {/* Copy output: the SINGLE, authoritative copy for this cell —
                        copies EVERYTHING in the output (text + any table as Markdown).
                        Shown whenever there's output. The DataGrid still has its own
                        "Copy CSV" for the raw table, which is complementary (CSV vs
                        the whole output). */}
                    {(cell.output || cell.html || hasImageOutput(cell)) && (
                      <button
                        className="cell-output-action-btn"
                        onClick={async (e) => {
                          const ok = await copyCellOutput(cell)
                          if (ok) {
                            const btn = e.currentTarget
                            btn.classList.add('is-copied')
                            setTimeout(() => btn.classList.remove('is-copied'), 1500)
                          }
                        }}
                        title="Copy the entire output to the clipboard"
                      >
                        <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> <span className="cell-output-copy-label">Copy</span>
                      </button>
                    )}
                  </div>
                </div>
                <CellOutput
                  cell={cell}
                  dagNode={dagNode}
                  isConnected={isConnected}
                  onFix={handleAiFix}
                  onCopyError={copyErrorWithContext}
                  aiBusy={aiResult?.loading}
                  aiFixing={aiResult?.loading && aiResult?.type === 'fix'}
                />
              </>
            )}
          </div>
        )}

        {/* Variables this cell defined/modified (code cells only). Clicking an
            owned variable opens the full tabular viewer; a variable whose value
            was later replaced is greyed with a jump link to the owning cell. */}
        {!collapsed && cellVars.length > 0 && (
          <div className={`cell-output cell-output--vars ${varsCollapsed ? 'cell-output-collapsed' : ''}`}>
            {varsCollapsed ? (
              <div className="cell-output-collapse-bar" onClick={() => setVarsCollapsed(false)}>
                <IconChevronRight width={10} height={10} />
                <span>Variables hidden — click to expand</span>
              </div>
            ) : (
              <>
                <div className="cell-output-collapse-bar" onClick={() => setVarsCollapsed(true)}>
                  <IconChevronDown width={10} height={10} />
                  <span className="cell-focus-section-title">Variables</span>
                </div>
                {/* Grouped by kind (Data / Collections / Scalars / Other) with the
                    SAME type icons + colors as the Variables side panel. Scalars
                    show their value inline; data/collections show their shape. */}
                {cellVarGroups.map(([groupName, groupVars]) => (
                  <div key={groupName} className="cell-vars-group">
                    <div className="cell-vars-group-label">{groupName}</div>
                    <div className="cell-vars-chips">
                      {groupVars.map(v => {
                        // Right-side meta: scalars show their VALUE (primary content →
                        // higher contrast); data/collections show their SHAPE (secondary
                        // → quiet). Two classes so the value reads clearly, the shape stays muted.
                        const isScalarVal = v.varKind === 'Scalars'
                        const meta = isScalarVal
                          ? (v.preview != null ? String(v.preview) : null)
                          : (v.shape || null)
                        const metaClass = isScalarVal ? 'cell-var-chip-value' : 'cell-var-chip-meta'
                        const iconEl = <span className={`cell-var-chip-icon ${getVarTypeColor(v.type)}`}>{getVarTypeIcon(v.type)}</span>
                        // Openable when this cell owns the value AND it's inspectable
                        // (anything but a plain scalar) — DataFrames/arrays open the
                        // grid, dict/list open a JSON tree. Scalars show inline only.
                        if (v.ownsNow && v.inspectable) {
                          return (
                            <button
                              key={v.name}
                              className={`cell-var-chip cell-var-${v.kind}`}
                              onClick={(e) => { e.stopPropagation(); setCellViewerVar(v.name) }}
                              title={`${v.kind === 'defined' ? 'Defined' : 'Modified'} here${v.type ? ` · ${v.type}` : ''}${v.shape ? ` · ${v.shape}` : ''} — click to inspect`}
                            >
                              {iconEl}
                              <span className="cell-var-chip-name">{v.name}</span>
                              {meta && <span className={metaClass}>{meta}</span>}
                            </button>
                          )
                        }
                        if (v.ownsNow) {
                          // Owned but non-tabular (scalar/collection): show inline, not clickable.
                          return (
                            <span
                              key={v.name}
                              className={`cell-var-chip cell-var-${v.kind} cell-var-static`}
                              title={`${v.kind === 'defined' ? 'Defined' : 'Modified'} here${v.type ? ` · ${v.type}` : ''}`}
                            >
                              {iconEl}
                              <span className="cell-var-chip-name">{v.name}</span>
                              {meta && <span className={metaClass}>{meta}</span>}
                            </span>
                          )
                        }
                        // Replaced elsewhere — greyed, with a jump link to the owning cell.
                        return (
                          <span
                            key={v.name}
                            className={`cell-var-chip cell-var-${v.kind} cell-var-replaced`}
                            title={`${v.name} was set here, but its current value was later replaced${v.replacedByLabel ? ` in ${v.replacedByLabel}` : ''}`}
                          >
                            {iconEl}
                            <span className="cell-var-chip-name">{v.name}</span>
                            {v.replacedByLabel && (
                              <>
                                <span className="cell-var-replaced-arrow">→</span>
                                <button
                                  className="cell-var-replaced-link"
                                  onClick={(e) => { e.stopPropagation(); jumpToCell(v.replacedById) }}
                                  title={`Current value of ${v.name} is set in ${v.replacedByLabel} — jump there`}
                                >{v.replacedByLabel}</button>
                              </>
                            )}
                          </span>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </>
            )}
          </div>
        )}

        {cellViewerVar && (
          <VariableDetailModal
            name={cellViewerVar}
            endpoint={microvmEndpoint}
            sessionId={sessionId}
            onClose={() => setCellViewerVar(null)}
          />
        )}

        {/* AI Result (explain or fix preview) — shown outside output section */}
        {!collapsed && aiResult?.loading && (
          <div className="cell-ai-result cell-ai-loading">
            <span className="cell-ai-spinner" />
            <span className="cell-ai-loading-text">{aiResult.type === 'fix' ? 'Fixing...' : 'Explaining...'}</span>
            <button className="cell-ai-cancel-btn" onClick={handleAiCancel}>Cancel</button>
          </div>
        )}
        {!collapsed && aiResult && !aiResult.loading && (() => {
          // Note origin: a fresh manual note carries source on aiResult; a persisted
          // one carries it on the cell. Default 'ai' for legacy/AI-generated notes.
          const noteSource = aiResult.source || cell.aiExplanationSource || 'ai'
          const isUserNote = aiResult.type === 'explain' && noteSource === 'user'
          return (
          <div className={`cell-ai-result cell-ai-result-${aiResult.type}`}>
            <div className="cell-ai-result-header">
              <span className="cell-ai-badge">{isUserNote ? '📝 Note' : '✨ AI'}</span>
              {aiResult.type === 'explain' && !isUserNote && cell.aiExplanationEdited && (
                <span className="cell-ai-badge cell-ai-badge-edited" title="This comment was manually edited">
                  <IconPencil width={9} height={9} /> Manually edited
                </span>
              )}
              {/* Verification badge for a fix with code. "Verified" ONLY when the
                  agent's last execute_code run succeeded on the exact returned code;
                  otherwise "Not verified" (+ the warning above the diff). State
                  mapping: services/fixVerification.js. */}
              <FixVerificationBadge aiResult={aiResult} />
              <div className="cell-ai-header-actions">
                {aiResult.type === 'explain' && !editingExplain && (
                  <button
                    className="cell-ai-edit"
                    title="Edit comment"
                    onClick={() => { setExplainDraft(aiResult.content || ''); setEditingExplain(true) }}
                  >
                    <IconPencil width={11} height={11} />
                  </button>
                )}
                <button className="cell-ai-dismiss" onClick={() => { setEditingExplain(false); setAiResult(null); if (onSetAiExplanation) onSetAiExplanation(null) }}>
                  <IconX width={10} height={10} />
                </button>
              </div>
            </div>
            {aiResult.type === 'explain' && (
              editingExplain ? (
                <div className="cell-ai-explain-edit">
                  <textarea
                    className="cell-ai-explain-textarea"
                    value={explainDraft}
                    onChange={(e) => setExplainDraft(e.target.value)}
                    autoFocus
                    rows={Math.min(12, Math.max(3, explainDraft.split('\n').length + 1))}
                    placeholder="Describe this cell… (Markdown supported)"
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') { e.preventDefault(); setEditingExplain(false) }
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                        e.preventDefault()
                        const text = explainDraft.trim()
                        const changed = text !== (aiResult.content || '').trim()
                        setEditingExplain(false)
                        // User notes: source 'user', no "edited" badge. AI notes:
                        // keep the edited flag so an edited AI note is marked.
                        if (onSetAiExplanation) {
                          if (isUserNote) onSetAiExplanation(text || null, false, 'user')
                          else onSetAiExplanation(text || null, text ? (changed || !!cell.aiExplanationEdited) : false, 'ai')
                        }
                      }
                    }}
                  />
                  <div className="cell-ai-explain-edit-actions">
                    <button
                      className="cell-ai-apply-btn"
                      onClick={() => {
                        const text = explainDraft.trim()
                        const changed = text !== (aiResult.content || '').trim()
                        setEditingExplain(false)
                        if (onSetAiExplanation) {
                          if (isUserNote) onSetAiExplanation(text || null, false, 'user')
                          else onSetAiExplanation(text || null, text ? (changed || !!cell.aiExplanationEdited) : false, 'ai')
                        }
                      }}
                    >Save</button>
                    <button
                      className="cell-ai-dismiss-btn"
                      onClick={() => {
                        setEditingExplain(false)
                        // Cancelling a brand-new (never-saved) manual note removes the
                        // empty placeholder note entirely instead of leaving a blank card.
                        if (isUserNote && !cell.aiExplanation) setAiResult(null)
                      }}
                    >Cancel</button>
                  </div>
                </div>
              ) : (
                <Markdown className="cell-ai-explain-text" content={aiResult.content} />
              )
            )}
            {/* Fix produced no code (timeout, "Failed to fix error", or an error
                string) — there's no diff to show, so surface the message on its own
                instead of rendering an empty panel. */}
            {aiResult.type === 'fix' && !aiResult.content && (
              <div className="cell-ai-fix-preview">
                <Markdown
                  className="cell-ai-fix-why-body"
                  content={aiResult.explanation || 'No fix could be generated. Try again.'}
                />
                <div className="cell-ai-fix-actions">
                  <button className="cell-ai-apply-btn" onClick={() => handleAiFix()}>Try Again</button>
                  <button className="cell-ai-dismiss-btn" onClick={() => setAiResult(null)}>Dismiss</button>
                </div>
              </div>
            )}
            {aiResult.type === 'fix' && aiResult.content && (
              <div className="cell-ai-fix-preview">
                {/* Not-verified warning (with the last run's full error) — first in
                    the preview so it's seen before the diff / Apply. */}
                <FixVerificationNotice aiResult={aiResult} />
                {/* "Why it failed" — the explanation returned alongside the fix (same
                    single model call). Collapsed by default and placed ABOVE the diff so
                    the actionable diff stays the focus; expand to read the reasoning.
                    This is transient fix context, NOT a persisted cell note. */}
                {aiResult.explanation && (
                  <details className="cell-ai-fix-why">
                    <summary className="cell-ai-fix-why-summary">Why it failed</summary>
                    <Markdown className="cell-ai-fix-why-body" content={aiResult.explanation} />
                  </details>
                )}
                {/* Diff the fix against the code that actually ERRORED (lastExecutedCode),
                    not necessarily today's cell.code — the user may have started editing
                    since the error. Falls back to cell.code if no execution snapshot exists
                    yet. Same read-only unified-diff CodeMirror view used by version history,
                    so a fix reads as "here's what changed" instead of a flat code dump. */}
                <div className="cell-ai-fix-diff">
                  <DiffCell
                    original={cell.lastExecutedCode ?? cell.code ?? ''}
                    current={aiResult.content}
                    type={cell.type}
                  />
                </div>
                <div className="cell-ai-fix-actions">
                  <button className="cell-ai-apply-btn" onClick={handleApplyFix}>Apply Fix</button>
                  <button className="cell-ai-dismiss-btn" onClick={() => setAiResult(null)}>Dismiss</button>
                </div>
              </div>
            )}
          </div>
          )
        })()}
      </div>

      {focused && (
        <CellFocusOverlay
          cell={cell}
          dagNode={dagNode}
          cellBadgeById={cellBadgeById}
          badge={thisCellBadge}
          notebookName={notebookName}
          isConnected={isConnected}
          catalogLoaded={catalogLoaded}
          generating={generating}
          aiBusy={aiResult?.loading}
          aiFixing={aiResult?.loading && aiResult?.type === 'fix'}
          variableNames={variableNames}
          dataSourceNames={dataSourceNames}
          sessionId={sessionId}
          editorVersion={editorVersion}
          errorLine={errorLine}
          errorChainLines={errorChainLines}
          showErrorGutter={showErrorGutter}
          onCodeChange={onCodeChange}
          onExecute={smartExecute}
          onInterrupt={onInterrupt}
          onFixError={aiAvailable && isConnected && cell.error ? handleAiFix : null}
          onFormat={onFormat}
          isFormatting={isFormatting}
          onClearOutput={onClearOutput ? () => onClearOutput() : null}
          onSetAiExplanation={onSetAiExplanation}
          cellVars={cellVars}
          microvmEndpoint={microvmEndpoint}
          onJumpToCell={(id) => { setFocused(false); jumpToCell(id) }}
          onOutputVarChange={onOutputVarChange}
          sqlVarPlaceholder={cell.type === 'sql' ? deriveSqlVarName(cell.code) : ''}
          aiResult={aiResult}
          onApplyFix={handleApplyFix}
          onCancelAi={handleAiCancel}
          onDismissFix={() => setAiResult(null)}
          lineageTrust={lineageTrust}
          // The lineage drawer lives in the notebook view, so leave focus mode
          // first (same pattern as onJumpToCell), then open it for this cell.
          onOpenLineage={onOpenLineage ? () => { setFocused(false); onOpenLineage(cell.id) } : null}
          explain={aiAvailable && (cell.type === 'code' || cell.type === 'sql') ? explain : null}
          explainBadgeToId={explainBadgeToId}
          onClose={() => setFocused(false)}
        />
      )}
    </div>
  )
})
