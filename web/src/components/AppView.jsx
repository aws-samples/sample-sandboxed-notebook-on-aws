import { useMemo, useState } from 'react'
import Markdown from './Markdown'
import ParamWidgets, { parseParams } from './ParamWidgets'
import CellOutput from './CellOutput'
import { LineageTrigger } from './LineageDrawer'
import { appShows, deriveDisplayVar } from '../services/appViewModel'
import { IconPlayAll, IconChevronDown, IconChevronRight } from './Icons'
import './AppView.css'
import { hasImageOutput } from '../services/cellOutput'

/**
 * AppView — Phase 1 of app publishing: an author-only, live "consumer" view of a
 * workbook. Reuses the author's current MicroVM/session (zero new infra) — it is
 * purely a presentation layer over the existing cells + the @param re-run loop.
 *
 * Layout: title/description, an "Inputs" control panel (all @param widgets), then
 * the presentation content (markdown + charts/tables/metric outputs). Code editors
 * are hidden. Changing an input re-runs that input's own cell; the cells that depend
 * on it follow through the dataflow cascade (useNotebookCells). If nothing has been
 * run yet, we show a "Run all to preview" prompt instead of empty charts.
 */

const EMPTY_TRUST = new Map()

export default function AppView({ tab = null, cells = [], isConnected = false, isExecuting = false, catalogLoaded = true, onRunAll, executeCell, updateCellCode, trustByCell = EMPTY_TRUST, onOpenLineage }) {
  // All cells that expose @param controls, with their index (for run-from-cell).
  const paramCells = useMemo(
    () => cells
      .map((cell, index) => ({ cell, index, params: parseParams(cell.code || '') }))
      .filter(entry => entry.params.length > 0),
    [cells]
  )

  const presentationCells = useMemo(() => cells.filter(appShows), [cells])

  // Let the viewer collapse the Inputs panel (e.g. to focus on the output once the
  // sliders are set). Local UI state — defaults to expanded on mount.
  const [inputsCollapsed, setInputsCollapsed] = useState(false)

  const hasAnyOutput = cells.some(c => c.type !== 'markdown' && (c.output || c.html || hasImageOutput(c)))

  // Stale = a runnable cell's code changed since it was last executed.
  const isStale = cells.some(
    c => c.type !== 'markdown' && c.code?.trim() && c.lastExecutedCode != null && c.lastExecutedCode !== c.code
  )

  // A param changed: run ONLY that param's own cell. App mode is inherently live,
  // so useNotebookCells force-cascades the true dependents after the drain (via
  // the dataflow graph) — that runs exactly the cells that depend on the changed
  // value, not every cell positioned below it (the old 'run-from-cell' behavior).
  const runFrom = (cellId) => {
    if (executeCell) executeCell(cellId)
  }

  return (
    <div className="app-view">
      <div className="app-view-inner">
        {paramCells.length > 0 && (
          <section className={`app-controls ${inputsCollapsed ? 'app-controls-collapsed' : ''}`}>
            <button
              type="button"
              className="app-controls-title"
              onClick={() => setInputsCollapsed(v => !v)}
              aria-expanded={!inputsCollapsed}
              title={inputsCollapsed ? 'Show inputs' : 'Hide inputs'}
            >
              <span className="app-controls-chevron">
                {inputsCollapsed ? <IconChevronRight width={12} height={12} /> : <IconChevronDown width={12} height={12} />}
              </span>
              Inputs
              {inputsCollapsed && (
                <span className="app-controls-count">{paramCells.length} {paramCells.length === 1 ? 'group' : 'groups'}</span>
              )}
            </button>
            {!inputsCollapsed && (
              <>
                {paramCells.map(({ cell }) => (
                  <ParamWidgets
                    key={cell.id}
                    code={cell.code}
                    onCodeChange={(newCode) => updateCellCode(cell.id, newCode)}
                    onExecute={() => runFrom(cell.id)}
                    sessionId={tab?.sessionId || null}
                  />
                ))}
                {!isConnected && (
                  <div className="app-controls-hint">Connect a MicroVM to make inputs live.</div>
                )}
              </>
            )}
          </section>
        )}

        {isStale && hasAnyOutput && (
          <div className="app-stale-banner">
            <span>Outputs may be out of date.</span>
            <button
              className="app-run-btn app-run-btn-sm"
              onClick={onRunAll}
              disabled={!isConnected || isExecuting || !catalogLoaded}
              title={!catalogLoaded ? 'Loading data sources…' : undefined}
            >
              {isExecuting ? 'Running…' : 'Run all'}
            </button>
          </div>
        )}

        {!hasAnyOutput ? (
          <div className="app-empty">
            <IconPlayAll width={26} height={26} />
            <p className="app-empty-title">This app hasn’t been run yet</p>
            <p className="app-empty-sub">Run all cells to render the app preview.</p>
            <button
              className="app-run-btn"
              onClick={onRunAll}
              disabled={!isConnected || isExecuting || !catalogLoaded}
              title={!catalogLoaded ? 'Loading data sources…' : undefined}
            >
              {isExecuting ? 'Running…' : 'Run all to preview'}
            </button>
            {!isConnected && <p className="app-empty-hint">Connect a MicroVM first.</p>}
          </div>
        ) : (
          <div className="app-content">
            {presentationCells.map(cell => {
              if (cell.type === 'markdown') {
                return (
                  <div key={cell.id} className="app-block app-block-markdown">
                    <Markdown className="md-rendered app-markdown" content={cell.code || ''} />
                  </div>
                )
              }
              const varName = deriveDisplayVar(cell)
              return (
                <div key={cell.id} className={`app-block app-block-${cell.type}`}>
                  <div className="app-block-var">
                    {varName && <span className="app-var-name">{varName}</span>}
                    <LineageTrigger
                      level={trustByCell.get(String(cell.id))}
                      showDot
                      onClick={() => onOpenLineage?.(cell.id)}
                    />
                  </div>
                  <CellOutput cell={cell} isConnected={false} />
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
