import { useEffect, useRef, useState, useMemo } from 'react'
import { createPortal } from 'react-dom'
import Markdown from './Markdown'
import { IconCollapse, IconPlay, IconStop, IconEraser, IconPencil, IconChevronDown, IconChevronRight, IconX } from './Icons'
import CellEditor from './CellEditor'
import CellOutput from './CellOutput'
import ParamWidgets, { parseParams } from './ParamWidgets'
import VariableDetailModal from './VariableDetailModal'
import DiffCell from './DiffCell'
import { FixVerificationBadge, FixVerificationNotice } from './FixVerification'
import { showDragOverlay, hideDragOverlay } from '../utils/dragOverlay'
import { exportCellOutputHtml, exportCellOutputMarkdown, copyCellOutput, copyErrorWithContext } from '../services/exportCellOutput'
import { effectiveStatus } from '../services/depGraph'
import { staleReasonText } from '../services/lineage'
import { LineageTrigger } from './LineageDrawer'
import ExplainPanel, { ExplainButton, ExplainStrip } from './ExplainPanel'
import { useExplainPanelLayout, useExplainHighlight } from '../hooks/useExplainPanelLayout'
import { highlightsValid } from '../services/explainPanel'
import { hasImageOutput } from '../services/cellOutput'
import { withPersistedImage } from '../services/cellImageApi'

// Same compact type icons the inline cell uses for the "variables defined here" chips.
const VAR_TYPE_ICONS = {
  DataFrame: '📊', Series: '📈', ndarray: '🔢', list: '[ ]', dict: '{ }',
  tuple: '( )', str: 'abc', int: '#', float: '#.', bool: '⊘', NoneType: '∅',
}
const getVarTypeIcon = (type) => VAR_TYPE_ICONS[type] || '◇'

// Focus / "expand" mode for a single code or SQL cell (Option A).
//
// Renders the SAME CellEditor + CellOutput the cell uses, but in a large blurred
// full-screen overlay so a wide DataFrame or big chart is easy to read. It's a
// PORTAL to document.body — the notebook column has its own overflow/stacking, so
// rendering in place would clip the overlay.
//
// It is a thin presentational shell: every edit / run / fix flows through the SAME
// handlers the inline cell passes down, so notebook state stays the single source
// of truth. A fresh CellEditor instance is bound to cell.code/onCodeChange (we do
// NOT relocate the inline editor's DOM node), which keeps this simple and correct;
// the only cost is that in-editor undo history doesn't carry across open/close.
export default function CellFocusOverlay({
  cell,
  dagNode = null,
  cellBadgeById = null,
  badge,
  notebookName,
  isConnected,
  catalogLoaded = true,
  generating = false,
  aiBusy = false,
  aiFixing = false,
  variableNames = [],
  dataSourceNames = null,
  sessionId,
  editorVersion,
  errorLine = null,
  showErrorGutter = true,
  onCodeChange,
  onExecute,
  onInterrupt,
  onFixError,
  onClearOutput,
  onFormat,
  isFormatting = false,
  onSetAiExplanation,
  cellVars = [],
  microvmEndpoint,
  onJumpToCell,
  // SQL result-variable bar (SQL cells only).
  onOutputVarChange,
  sqlVarPlaceholder,
  // AI fix preview / loading (mirrors the inline cell's fix UI).
  aiResult,
  onApplyFix,
  onCancelAi,
  onDismissFix,
  // "How was this computed?" — same trust dot + opener as the inline cell's
  // Output header. Null hides the trigger (e.g. no lineage wiring).
  lineageTrust = null,
  onOpenLineage = null,
  // "Explain this code" (useCellExplain object, or null to hide the button/card).
  explain = null,
  explainBadgeToId = null,
  onClose,
}) {
  // Esc closes; lock body scroll while open so the notebook behind can't scroll.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      // Esc inside the Explanation panel closes the panel only (handled there).
      if (e.target?.closest?.('.cell-explain-panel')) return
      e.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
  }, [onClose])

  const hasOutput = cell.output || cell.error || cell.html || hasImageOutput(cell)
  // A fix PREVIEW is present (not just loading): the actionable diff panel below.
  // When it is, the output pane above is only the short error that prompted the
  // fix, so we shrink the output's share and hand the freed space to the fix
  // panel instead of letting output keep its full editor/output split ratio.
  const hasFixPreview = !!(aiResult && !aiResult.loading && aiResult.type === 'fix' && aiResult.content)
  const running = cell.status === 'running' || generating

  // Output collapse — mirrors the inline cell's collapsible "Output" header bar.
  const [outputCollapsed, setOutputCollapsed] = useState(false)
  // Inputs (param sliders) collapse — mirrors App mode's collapsible "Inputs"
  // panel so the frozen control strip can be hidden to reclaim editor space.
  const [inputsCollapsed, setInputsCollapsed] = useState(false)
  const paramCount = useMemo(
    () => (cell.type !== 'markdown' && cell.code ? parseParams(cell.code).length : 0),
    [cell.type, cell.code]
  )

  // Status indicators mirrored from the inline cell head (lint / stale dots).
  // Same freshness gate as Cell.jsx: lint diagnostics are only valid while the
  // code still matches what was linted (cell.lintCode); once edited, cell-relative
  // positions are stale so we drop them until the next lint pass.
  const lintDiagnostics = (cell.lint && cell.lintCode != null && cell.code === cell.lintCode)
    ? cell.lint
    : null
  // Edit-stale: code changed since the last execution (re-run to refresh output).
  // effectiveStatus over the live code only (the node's own `edited` is ignored
  // while a live cell exists — design §8.1), same as the inline cell.
  const editStale = effectiveStatus({ code: cell.code, lastExecutedCode: cell.lastExecutedCode }, dagNode) === 'edited'
    && cell.status !== 'running'
  // Dataflow-stale: the stored DAG node carries a stale_reason. Suppressed when
  // edit-stale is already showing (same precedence as inline).
  const nodeStale = dagNode ? dagNode.stale_reason : null
  const dataflowStale = !!nodeStale && cell.status !== 'running' && !editStale

  // Variable viewer opened from a "defined here" chip (same as the inline cell).
  const [viewerVar, setViewerVar] = useState(null)
  // Variables section collapse — mirrors the inline cell's collapsible bar.
  const [varsCollapsed, setVarsCollapsed] = useState(false)

  // In-place note editing. Editing/adding a note happens HERE in focus mode (no
  // bouncing back to the inline cell). Saving writes through the same
  // onSetAiExplanation(text|null, edited, source) contract the inline editor uses,
  // so the note persists identically. noteSource tracks whether we're authoring a
  // user note ('user') or editing an AI note ('ai').
  const [editingNote, setEditingNote] = useState(false)
  const [noteDraft, setNoteDraft] = useState('')
  const [noteSource, setNoteSource] = useState('user')

  const beginAddNote = () => {
    setNoteDraft('')
    setNoteSource('user')
    setEditingNote(true)
  }
  const beginEditNote = () => {
    setNoteDraft(cell.aiExplanation || '')
    setNoteSource(cell.aiExplanationSource || 'ai')
    setEditingNote(true)
  }
  const saveNote = () => {
    const text = noteDraft.trim()
    const changed = text !== (cell.aiExplanation || '').trim()
    setEditingNote(false)
    if (!onSetAiExplanation) return
    if (noteSource === 'user') onSetAiExplanation(text || null, false, 'user')
    else onSetAiExplanation(text || null, text ? (changed || !!cell.aiExplanationEdited) : false, 'ai')
  }
  const cancelNote = () => setEditingNote(false)

  // Draggable split between the editor pane and the output pane. editorPct is the
  // editor's share of the body height (%). Drag the handle to rebalance; clamped so
  // neither pane can be squeezed to nothing.
  const bodyRef = useRef(null)
  const [editorPct, setEditorPct] = useState(40)
  const draggingRef = useRef(false)

  // Explanation panel: open/width (session-remembered), stacked switch, and the
  // section <-> editor line highlight. Highlights only apply while the explained
  // code still has the same line count as the editor.
  const panel = useExplainPanelLayout(!!explain && ['done', 'loading', 'error'].includes(explain.state))
  const explainResult = explain && explain.state === 'done' ? explain.result : null
  const linked = !!explainResult && highlightsValid(explain.snapshotCode, cell.code)
  const hl = useExplainHighlight({ active: !!explain && panel.open && linked })
  const clearHighlight = hl.clear
  const explainAt = explain ? explain.at : 0
  useEffect(() => { clearHighlight() }, [explainAt, clearHighlight])
  const closeExplain = () => {
    panel.setOpen(false)
    // Keep keyboard users in the toolbar instead of dropping focus to <body>.
    requestAnimationFrame(() => document.querySelector('.cell-focus-head .cell-explain-btn')?.focus())
  }
  const toggleExplain = () => {
    if (panel.open) { closeExplain(); return }
    panel.setOpen(true)
    if (explain && explain.state === 'idle' && explain.canExplain) explain.run()
  }

  useEffect(() => {
    const onMove = (e) => {
      if (!draggingRef.current || !bodyRef.current) return
      const rect = bodyRef.current.getBoundingClientRect()
      const pct = ((e.clientY - rect.top) / rect.height) * 100
      setEditorPct(Math.min(80, Math.max(15, pct)))
    }
    const onUp = () => {
      if (!draggingRef.current) return
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      // Remove the transparent capture overlay now the drag is done.
      hideDragOverlay()
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      hideDragOverlay()
    }
  }, [])

  const startDrag = (e) => {
    e.preventDefault()
    draggingRef.current = true
    document.body.style.cursor = 'row-resize'
    document.body.style.userSelect = 'none'
    // Cover the window with a transparent, max-z-index overlay so dragging OVER an
    // embedded Plotly iframe doesn't let the iframe swallow the mousemove/mouseup
    // (same fix used by the notebook's panel/sidebar resizers). Without it the drag
    // gets stuck the moment the cursor crosses a chart.
    showDragOverlay('row-resize')
  }

  return createPortal(
    <div className="cell-focus-backdrop" onClick={onClose}>
      <div
        className={`cell-focus-panel ${cell.type === 'sql' ? 'cell-focus-sql' : ''}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="cell-focus-head">
          <span className="cell-focus-badge">{badge}</span>
          <span className="cell-focus-title">{cell.type === 'sql' ? 'SQL cell' : 'Python cell'} — focus mode</span>
          {/* Status indicators mirrored from the inline cell head: kernel exec count,
              lint issues, and the edit-/dataflow-stale dots. Placed on the title bar
              so focus mode surfaces the same cues as the notebook cell. */}
          {cell.executionNumber ? (
            <span className="cell-focus-exec-num" title="Kernel execution count">{`[${cell.executionNumber}]`}</span>
          ) : null}
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
          {editStale && (
            <span className="cell-stale-badge" title="Code modified since last execution — re-run to update output">●</span>
          )}
          {dataflowStale && (
            <span
              className="cell-dataflow-stale-badge"
              title={staleReasonText(nodeStale, cellBadgeById)}
            >
              ●
            </span>
          )}
          <div className="cell-focus-head-actions">
            {running ? (
              <button className="cell-focus-run cell-focus-stop" onClick={onInterrupt} title="Stop execution">
                <IconStop width={13} height={13} />
              </button>
            ) : (
              <button
                className="cell-focus-run"
                onClick={onExecute}
                disabled={!isConnected || !catalogLoaded}
                title={!catalogLoaded ? 'Loading data sources…' : 'Run cell (Shift+Enter)'}
              >
                <IconPlay width={13} height={13} />
              </button>
            )}
            {onClearOutput && hasOutput && (
              <button className="cell-focus-icon-btn" onClick={onClearOutput} title="Clear output">
                <IconEraser width={13} height={13} />
              </button>
            )}
            {/* Format this cell — Python via ruff, SQL via sqlglot (server-side /format).
                Same availability as the inline cell's Format button: non-empty code/SQL
                cell, connected. Spinner while the single-cell format runs. */}
            {onFormat && (cell.type === 'code' || cell.type === 'sql') && (cell.code || '').trim() && (
              <button
                className="cell-focus-icon-btn"
                onClick={onFormat}
                disabled={isFormatting || !isConnected}
                title={cell.type === 'sql' ? 'Format this SQL cell (sqlglot)' : 'Format this cell (ruff)'}
              >
                {isFormatting ? <span className="cell-gen-spinner" /> : (
                  <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="14" y2="12" /><line x1="4" y1="18" x2="18" y2="18" />
                  </svg>
                )}
              </button>
            )}
            {explain && (
              <ExplainButton explain={explain} className="cell-focus-icon-btn" allowEmpty pressed={panel.open} onClick={toggleExplain} />
            )}
            {onSetAiExplanation && !cell.aiExplanation && !editingNote && (
              <button className="cell-focus-icon-btn" onClick={beginAddNote} title="Add a note to this cell">
                <IconPencil width={13} height={13} />
              </button>
            )}
            <button className="cell-focus-close" onClick={onClose} title="Exit focus mode (Esc)">
              <IconCollapse width={14} height={14} />
            </button>
          </div>
        </div>

        <div className={`cell-focus-split${panel.stacked ? ' is-stacked' : ''}`} ref={panel.splitRef}>
        <div className="cell-focus-body" ref={bodyRef}>
          <div
            className="cell-focus-editor"
            style={(hasOutput && !outputCollapsed)
              ? { flexBasis: `${editorPct}%`, flexGrow: 0 }
              : { flexBasis: 'auto', flexGrow: 1 }}
          >
            {/* Param sliders + SQL var bar are FROZEN — they sit in a non-scrolling
                header so they stay visible while the code scrolls beneath them.
                Only .cell-focus-editor-scroll (the CellEditor) scrolls. The Inputs
                strip is also collapsible (mirrors App mode) to reclaim space. */}
            {cell.type !== 'markdown' && cell.code && cell.code.includes('@param') && (
              <div className={`cell-focus-editor-sticky ${inputsCollapsed ? 'cell-focus-inputs-collapsed' : ''}`}>
                <button
                  type="button"
                  className="cell-focus-inputs-title"
                  onClick={() => setInputsCollapsed(v => !v)}
                  aria-expanded={!inputsCollapsed}
                  title={inputsCollapsed ? 'Show inputs' : 'Hide inputs'}
                >
                  <span className="cell-focus-inputs-chevron">
                    {inputsCollapsed ? <IconChevronRight width={11} height={11} /> : <IconChevronDown width={11} height={11} />}
                  </span>
                  Inputs
                  {inputsCollapsed && paramCount > 0 && (
                    <span className="cell-focus-inputs-count">
                      {paramCount} {paramCount === 1 ? 'control' : 'controls'}
                    </span>
                  )}
                </button>
                {!inputsCollapsed && (
                  <ParamWidgets
                    code={cell.code}
                    onCodeChange={onCodeChange}
                    onExecute={onExecute}
                    sessionId={sessionId}
                  />
                )}
              </div>
            )}
            {/* SQL result-variable bar — name the query result (SQL cells only). */}
            {cell.type === 'sql' && (
              <div className="sql-output-var cell-focus-editor-sticky">
                <span className="sql-output-var-label">→</span>
                <input
                  className="sql-output-var-input"
                  type="text"
                  value={cell.outputVariable || ''}
                  onChange={(e) => onOutputVarChange && onOutputVarChange(e.target.value)}
                  placeholder={sqlVarPlaceholder || ''}
                  title="Variable name for the query result (accessible in subsequent cells)"
                  spellCheck={false}
                />
              </div>
            )}
            <div className="cell-focus-editor-scroll">
              <CellEditor
                key={editorVersion}
                code={cell.code}
                language={cell.type === 'sql' ? 'sql' : 'python'}
                placeholder={cell.type === 'sql'
                  ? 'Write SQL or describe what you want... (Shift+Enter runs or generates)'
                  : 'Type Python code or describe what you want in plain English... (Shift+Enter runs code or generates from NLP)'}
                onCodeChange={onCodeChange}
                onExecute={onExecute}
                variables={variableNames}
                dataSources={dataSourceNames}
                sessionId={sessionId}
                errorLine={errorLine}
                errorMessage={cell.error || ''}
                showErrorGutter={showErrorGutter}
                onFixError={onFixError}
                lintDiagnostics={lintDiagnostics}
                highlight={hl.highlight}
              />
            </div>
          </div>

          {hasOutput && !outputCollapsed && (
            <div
              className="cell-focus-splitter"
              onMouseDown={startDrag}
              title="Drag to resize"
              role="separator"
              aria-orientation="horizontal"
            >
              <span className="cell-focus-splitter-grip" />
            </div>
          )}

          {hasOutput && (
            <div
              className={`cell-focus-output cell-output cell-output--result ${cell.error ? 'cell-output-error' : ''} ${outputCollapsed ? 'cell-output-collapsed' : ''}`}
              style={outputCollapsed
                ? undefined
                : hasFixPreview
                  // Don't reserve the full split for output — size toward content
                  // (it scrolls internally) so the fix panel below gets the room.
                  ? { flexBasis: 'auto', flexGrow: 0, flexShrink: 1 }
                  : { flexBasis: `${100 - editorPct}%` }}
            >
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
                      {onOpenLineage && (
                        <LineageTrigger
                          level={lineageTrust}
                          showDot
                          onClick={(e) => { e.stopPropagation(); onOpenLineage() }}
                        />
                      )}
                      <div className="cell-output-export-wrap">
                        <button className="cell-output-action-btn" title="Export output" onClick={(e) => {
                          e.stopPropagation()
                          const menu = e.currentTarget.nextElementSibling
                          menu.style.display = menu.style.display === 'block' ? 'none' : 'block'
                        }}>
                          <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Export
                        </button>
                        <div className="cell-output-export-menu" style={{ display: 'none' }}>
                          <button onClick={async (e) => { e.currentTarget.parentElement.style.display = 'none'; exportCellOutputHtml(await withPersistedImage(cell), notebookName, badge) }}>HTML</button>
                          <button onClick={async (e) => { e.currentTarget.parentElement.style.display = 'none'; exportCellOutputMarkdown(await withPersistedImage(cell), notebookName, badge) }}>Markdown</button>
                        </div>
                      </div>
                      {/* Single authoritative Copy — copies the ENTIRE output
                          (text + any table as Markdown), shown whenever output
                          exists. Mirrors the inline cell exactly. */}
                      {(cell.output || cell.html || hasImageOutput(cell)) && (
                        <button
                          className="cell-output-action-btn"
                          title="Copy the entire output to the clipboard"
                          onClick={async (e) => {
                            const ok = await copyCellOutput(cell)
                            if (ok) {
                              const btn = e.currentTarget
                              btn.classList.add('is-copied')
                              setTimeout(() => btn.classList.remove('is-copied'), 1500)
                            }
                          }}
                        >
                          <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> <span className="cell-output-copy-label">Copy</span>
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="cell-focus-output-scroll">
                    <CellOutput
                      cell={cell}
                      dagNode={dagNode}
                      isConnected={isConnected}
                      onFix={onFixError}
                      onCopyError={copyErrorWithContext}
                      aiBusy={aiBusy}
                      aiFixing={aiFixing}
                    />
                  </div>
                </>
              )}
            </div>
          )}

          {/* Note — edited IN PLACE here (no bouncing back to the inline cell).
              Shows the editor while editingNote, else the read-only ✨/📝 note. */}
          {editingNote ? (
            <div className="cell-focus-note cell-ai-result cell-ai-result-explain">
              <div className="cell-ai-result-header">
                <span className="cell-ai-badge">{noteSource === 'user' ? '📝 Note' : '✨ AI'}</span>
              </div>
              <div className="cell-ai-explain-edit">
                <textarea
                  className="cell-ai-explain-textarea"
                  value={noteDraft}
                  onChange={(e) => setNoteDraft(e.target.value)}
                  autoFocus
                  rows={Math.min(12, Math.max(3, noteDraft.split('\n').length + 1))}
                  placeholder="Describe this cell… (Markdown supported)"
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelNote() }
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveNote() }
                  }}
                />
                <div className="cell-ai-explain-edit-actions">
                  <button className="cell-ai-apply-btn" onClick={saveNote}>Save</button>
                  <button className="cell-ai-dismiss-btn" onClick={cancelNote}>Cancel</button>
                </div>
              </div>
            </div>
          ) : cell.aiExplanation ? (
            <div className="cell-focus-note cell-ai-result cell-ai-result-explain">
              <div className="cell-ai-result-header">
                <span className="cell-ai-badge">
                  {(cell.aiExplanationSource || 'ai') === 'user' ? '📝 Note' : '✨ AI'}
                </span>
                {cell.aiExplanationEdited && (cell.aiExplanationSource || 'ai') !== 'user' && (
                  <span className="cell-ai-badge cell-ai-badge-edited" title="This comment was manually edited">
                    <IconPencil width={9} height={9} /> Manually edited
                  </span>
                )}
                {onSetAiExplanation && (
                  <div className="cell-ai-header-actions">
                    <button className="cell-ai-edit" title="Edit note" onClick={beginEditNote}>
                      <IconPencil width={11} height={11} />
                    </button>
                    <button
                      className="cell-ai-dismiss"
                      title="Remove note"
                      onClick={() => onSetAiExplanation(null)}
                    >
                      <IconX width={10} height={10} />
                    </button>
                  </div>
                )}
              </div>
              <Markdown className="cell-ai-explain-text" content={cell.aiExplanation} />
            </div>
          ) : null}

          {/* AI activity — mirrors the inline cell. A loading row while a fix/explain
              is in flight, then a fix PREVIEW (diff + Apply/Dismiss) so a fix
              triggered in focus mode can be reviewed and applied here. */}
          {aiResult?.loading && (
            <div className="cell-focus-note cell-ai-result cell-ai-loading">
              <span className="cell-ai-spinner" />
              <span className="cell-ai-loading-text">{aiResult.type === 'fix' ? 'Fixing...' : 'Explaining...'}</span>
              {onCancelAi && <button className="cell-ai-cancel-btn" onClick={onCancelAi}>Cancel</button>}
            </div>
          )}
          {aiResult && !aiResult.loading && aiResult.type === 'fix' && aiResult.content && (
            <div className="cell-focus-note cell-ai-result cell-ai-result-fix">
              {/* Header with the ✨ AI badge + dismiss — mirrors the inline cell so
                  focus mode reads identically. */}
              <div className="cell-ai-result-header">
                <span className="cell-ai-badge">✨ AI</span>
                <FixVerificationBadge aiResult={aiResult} />
                {onDismissFix && (
                  <div className="cell-ai-header-actions">
                    <button className="cell-ai-dismiss" onClick={onDismissFix} title="Dismiss">
                      <IconX width={10} height={10} />
                    </button>
                  </div>
                )}
              </div>
              <div className="cell-ai-fix-preview">
                {/* Not-verified warning (full last-run error) before the diff/Apply —
                    same component as the inline cell. */}
                <FixVerificationNotice aiResult={aiResult} />
                {/* "Why it failed" — explanation from the same single Fix call,
                    collapsed by default, ABOVE the diff. Matches the inline cell. */}
                {aiResult.explanation && (
                  <details className="cell-ai-fix-why">
                    <summary className="cell-ai-fix-why-summary">Why it failed</summary>
                    <Markdown className="cell-ai-fix-why-body" content={aiResult.explanation} />
                  </details>
                )}
                <div className="cell-ai-fix-diff">
                  <DiffCell
                    original={cell.lastExecutedCode ?? cell.code ?? ''}
                    current={aiResult.content}
                    type={cell.type}
                  />
                </div>
                <div className="cell-ai-fix-actions">
                  {onApplyFix && <button className="cell-ai-apply-btn" onClick={onApplyFix}>Apply Fix</button>}
                  {onDismissFix && <button className="cell-ai-dismiss-btn" onClick={onDismissFix}>Dismiss</button>}
                </div>
              </div>
            </div>
          )}

          {/* Variables defined/modified here — mirrors the inline cell's chips.
              Openable chips launch the variable viewer (above this overlay); a
              chip whose value was later replaced is greyed with a jump link. */}
          {cellVars.length > 0 && (
            <div className={`cell-focus-vars cell-output cell-output--vars ${varsCollapsed ? 'cell-output-collapsed' : ''}`}>
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
                  <div className="cell-vars-chips">
                    {cellVars.map(v => (
                      v.ownsNow ? (
                        <button
                          key={v.name}
                          className={`cell-var-chip cell-var-${v.kind}`}
                          onClick={() => setViewerVar(v.name)}
                          title={`${v.kind === 'defined' ? 'Defined' : 'Modified'} here${v.type ? ` · ${v.type}` : ''}${v.shape ? ` · ${v.shape}` : ''} — click to inspect`}
                        >
                          <span className="cell-var-chip-icon">{getVarTypeIcon(v.type)}</span>
                          <span className="cell-var-chip-name">{v.name}</span>
                        </button>
                      ) : (
                        <span
                          key={v.name}
                          className={`cell-var-chip cell-var-${v.kind} cell-var-replaced`}
                          title={`${v.name} was set here, but its current value was later replaced${v.replacedByLabel ? ` in ${v.replacedByLabel}` : ''}`}
                        >
                          <span className="cell-var-chip-icon">{getVarTypeIcon(v.type)}</span>
                          <span className="cell-var-chip-name">{v.name}</span>
                          {v.replacedByLabel && (
                            <>
                              <span className="cell-var-replaced-arrow">→</span>
                              <button
                                className="cell-var-replaced-link"
                                onClick={() => onJumpToCell && onJumpToCell(v.replacedById)}
                                title={`Current value of ${v.name} is set in ${v.replacedByLabel} — jump there`}
                              >{v.replacedByLabel}</button>
                            </>
                          )}
                        </span>
                      )
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
        </div>

        {explain && panel.open && (
          <>
            {!panel.stacked && (
              <div
                className="cell-explain-splitter"
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize explanation panel"
                aria-valuenow={Math.round(panel.width)}
                tabIndex={0}
                onMouseDown={panel.startDrag}
                onKeyDown={panel.onSplitterKey}
                title="Drag to resize"
              >
                <span className="cell-explain-splitter-grip" />
              </div>
            )}
            <ExplainPanel
              explain={explain}
              badgeToId={explainBadgeToId}
              onJump={onJumpToCell}
              onCollapse={closeExplain}
              stacked={panel.stacked}
              width={panel.width}
              linked={linked}
              pinned={hl.pinned}
              onHover={hl.hover}
              onPin={hl.pin}
              announcement={hl.announcement}
              onViewNotes={explain.viewNotes}
            />
          </>
        )}
        {explain && !panel.open && (
          <ExplainStrip onOpen={toggleExplain} stacked={panel.stacked} busy={explain.busy} />
        )}
        </div>

        {viewerVar && (
          <VariableDetailModal
            name={viewerVar}
            endpoint={microvmEndpoint}
            sessionId={sessionId}
            onClose={() => setViewerVar(null)}
            className="modal-card-lg var-viewer-in-focus"
          />
        )}
      </div>
    </div>,
    document.body
  )
}
