/**
 * NotebookToolbar — presentational toolbar for the notebook view.
 * Extracted from Notebook.jsx. All behavior lives in the callbacks/state passed
 * in as props; this component only renders the buttons and the VM status pill.
 */
import { useState, useEffect, useRef } from 'react'
import { IconPlayAll, IconRefresh, IconFolderOpen, IconStop, IconSearch, IconX, IconZap, IconSun, IconMoon, IconFlame, IconNotebook, IconEraser } from './Icons'
import { isVmSuspended, findSessionVm } from '../constants'
import { hasImageOutput } from '../services/cellOutput'

// Transient autosave hint next to the notebook title (the Colab/Docs pattern):
// muted "Saving…" while a write is in flight, a brief "Saved" right after, then
// it fades out. No dot, no permanent badge. Reflects the 3s working-copy save —
// distinct from the disk-icon "Save version". Renders nothing when idle.
function SaveHint() {
  const [label, setLabel] = useState('')  // '' = show nothing
  const hideRef = useRef(null)

  useEffect(() => {
    const onEvt = (e) => {
      const status = e.detail?.status
      if (hideRef.current) { clearTimeout(hideRef.current); hideRef.current = null }
      if (status === 'saving' || status === 'pending') {
        setLabel('Saving…')
      } else if (status === 'saved') {
        setLabel('Saved')
        hideRef.current = setTimeout(() => setLabel(''), 2000)  // fade out when idle
      } else if (status === 'error') {
        setLabel('Save failed — kept locally')
        hideRef.current = setTimeout(() => setLabel(''), 4000)
      }
    }
    window.addEventListener('notebook-autosave', onEvt)
    return () => {
      window.removeEventListener('notebook-autosave', onEvt)
      if (hideRef.current) clearTimeout(hideRef.current)
    }
  }, [])

  if (!label) return null
  return <span className="toolbar-save-hint">{label}</span>
}

export default function NotebookToolbar({
  tab,
  instances = {},
  cells,
  vmAlive,
  viewMode = 'notebook',
  setViewMode,
  isExecuting,
  runProgress,
  interruptExecution,
  executeAllCells,
  staleCount = 0,
  runStaleDownstream,
  reactiveEnabled = false,
  setReactiveEnabled,
  hasReactiveSurface = false,
  onNewNotebook,
  loadNotebook,
  onOpenVersions,
  exportMenuPos,
  setExportMenuPos,
  setShowSearch,
  searchInputRef,
  aiAvailable,
  autoDocumentNotebook,
  isAnnotating,
  clearAnnotations,
  hasAnnotations,
  clearAllOutputs,
  lintNotebook,
  formatNotebook,
  isLinting = false,
  isFormatting = false,
  onCloseTab,
  onDeleteTab,
  setShowConnection,
  theme,
  onToggleTheme,
}) {
  // Any runnable code or SQL cell? Lint/Format act on Python (ruff) AND SQL
  // (sqlglot) cells, so show the controls when either kind is present; hide only
  // for an all-markdown notebook where there's nothing to lint or format.
  const hasCodeCells = cells.some(c => (c.type === 'code' || c.type === 'sql') && (c.code || '').trim())
  return (
    <div className="notebook-toolbar">
      <div className="toolbar-scrollable">
        <div className="toolbar-brand">
          {setViewMode && (
            <div
              className="toolbar-viewmode"
              role="tablist"
              aria-label="View mode"
              data-active={viewMode === 'app' ? 'app' : 'notebook'}
            >
              {/* Sliding thumb — the accent pill that animates between segments. */}
              <span className="viewmode-thumb" aria-hidden="true" />
              <button
                className={`viewmode-seg ${viewMode === 'notebook' ? 'viewmode-seg-active' : ''}`}
                onClick={() => setViewMode('notebook')}
                role="tab"
                aria-selected={viewMode === 'notebook'}
                title="Edit the notebook"
              >
                Notebook
              </button>
              <button
                className={`viewmode-seg ${viewMode === 'app' ? 'viewmode-seg-active' : ''}`}
                onClick={() => setViewMode('app')}
                role="tab"
                aria-selected={viewMode === 'app'}
                title="Preview as an app (code hidden, inputs on top)"
              >
                App
              </button>
            </div>
          )}
          <IconZap width={14} height={14} />
          <span className="toolbar-notebook-name" title={tab.name}>{tab.name || 'Untitled'}</span>
          {viewMode !== 'app' && <SaveHint />}
        </div>

        <span className="toolbar-divider" />

        <div className="toolbar-group toolbar-group-run" title="Run">
          {/* Run All is gated on the LIGHTWEIGHT source list (tab._sourcesReady) —
              the type/identity the SQL engine needs to route a query — NOT the full
              enriched catalog (column schemas + entity docs, which don't block a Run).
              This enables Run seconds earlier (as soon as /datasources resolves) than
              the old _catalogLoaded gate, which also waited on the VM cold-start
              round-trip + full schema discovery. EXCEPT when the VM is suspended: a
              suspended VM skips its fetches so _sourcesReady stays false; gating on it
              alone would DEADLOCK (Run All disabled → can't run a cell → VM never
              resumes → never loads). Running a cell auto-resumes the VM, so we allow
              Run All when suspended. */}
          <button
            className="toolbar-btn toolbar-btn-run-all"
            onClick={executeAllCells}
            disabled={isExecuting || !vmAlive || (!tab._sourcesReady && !isVmSuspended(instances, tab.microvmId))}
            title={(!tab._sourcesReady && !isVmSuspended(instances, tab.microvmId)) ? 'Loading data sources… available once the source list finishes loading' : 'Run all cells'}
          >
            <IconPlayAll width={14} height={14} />
            {runProgress && <span className="toolbar-progress">{runProgress.current}/{runProgress.total}</span>}
          </button>
          {/* ONE button. Click = run the dataflow-stale dependent cells now (shows
              a count, disabled when none). Right-click = toggle REACTIVE auto-run.
              Amber + spin when reactive is ON so the live mode is unmistakable. */}
          {/* One button, three states:
              • Reactive ON  → pressed/amber, NO count; click turns it off. Dependents
                auto-run on change, so there's nothing to run manually.
              • Reactive OFF + N stale → shows N; click RUNS those N cells (no toggle).
              • Reactive OFF + none → not-pressed; click turns Reactive on.
              Rule: a count is shown only when reactive is OFF; a visible count means
              "click runs", otherwise "click toggles".
              Hidden entirely when the notebook has no reactive surface (no @param
              and no dependency edges) — there'd be nothing for it to act on. */}
          {hasReactiveSurface && (
          <button
            className={`toolbar-btn toolbar-btn-run-stale${reactiveEnabled ? ' reactive' : ''}${!reactiveEnabled && staleCount > 0 ? ' has-stale' : ''}`}
            onClick={() => {
              if (!reactiveEnabled && staleCount > 0) {
                if (!isExecuting && vmAlive) runStaleDownstream()   // action: run the dependents
              } else {
                setReactiveEnabled && setReactiveEnabled(!reactiveEnabled)  // toggle reactive
              }
            }}
            aria-pressed={reactiveEnabled}
            title={
              reactiveEnabled
                ? 'Reactive ON — dependent cells auto-run when a cell/param changes. Click to turn off.'
                : (staleCount > 0
                    ? `Run ${staleCount} dependent cell${staleCount === 1 ? '' : 's'} whose inputs changed`
                    : 'Click to turn on Reactive auto-run of dependent cells')
            }
          >
            <IconRefresh width={14} height={14} />
            {!reactiveEnabled && staleCount > 0 && <span className="toolbar-stale-count">{staleCount}</span>}
          </button>
          )}
          {cells.some(c => c.status === 'running') && (
            <button
              className="toolbar-btn toolbar-btn-stop"
              onClick={interruptExecution}
              title="Stop execution"
            >
              <IconStop width={14} height={14} />
            </button>
          )}
        </div>

        <span className="toolbar-divider" />

        <div className="toolbar-group toolbar-group-notebook" title="Notebook actions">
          {onNewNotebook && (
            <button className="toolbar-btn" onClick={onNewNotebook} title="New notebook">
              <IconNotebook width={14} height={14} />
            </button>
          )}
          <button className="toolbar-btn toolbar-btn-open" onClick={loadNotebook} title="Open notebook">
            <IconFolderOpen width={14} height={14} />
          </button>
          <button className="toolbar-btn toolbar-btn-versions" onClick={onOpenVersions} title="Version history">
            {/* Counterclockwise clock — the standard "history / revisions" glyph. */}
            <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 3v5h5" /><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8" /><path d="M12 7v5l3 2" />
            </svg>
          </button>
          <button className="toolbar-btn" onClick={(e) => {
            const rect = e.currentTarget.getBoundingClientRect()
            setExportMenuPos(exportMenuPos ? null : { top: rect.bottom + 4, left: rect.left })
          }} title="Download / export notebook">
            <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </button>
          {viewMode !== 'app' && (
            <button className="toolbar-btn toolbar-btn-find" onClick={() => { setShowSearch(true); setTimeout(() => searchInputRef.current?.focus(), 50) }} title="Find in notebook (Cmd+F)">
              <IconSearch width={14} height={14} />
            </button>
          )}
          {aiAvailable && (
            <button
              className={`toolbar-btn toolbar-btn-autodoc ${isAnnotating ? 'toolbar-btn-loading' : ''}`}
              onClick={autoDocumentNotebook}
              disabled={isAnnotating}
              title="Auto-document notebook with AI (title, section headers, per-cell comments)"
            >
              {isAnnotating ? <span className="toolbar-spinner" /> : <IconZap width={14} height={14} />}
            </button>
          )}
          {aiAvailable && hasAnnotations && !isAnnotating && clearAnnotations && (
            <button
              className="toolbar-btn"
              onClick={clearAnnotations}
              title="Clear AI documentation (generated title/sections + per-cell comments)"
            >
              <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3l18 18"/><path d="M10.5 5.5A5 5 0 0 1 19 9c0 2-1 3.5-2.5 5"/><path d="M7.5 7.5A5 5 0 0 0 5 11c0 3 3 5 7 5"/></svg>
            </button>
          )}
          <button
            className="toolbar-btn"
            onClick={clearAllOutputs}
            disabled={!cells.some(c => c.output || c.error || c.html || hasImageOutput(c))}
            title="Clear all cell outputs"
          >
            <IconEraser width={14} height={14} />
          </button>
          {/* Format — reformat every code cell with ruff (each cell independently,
              so no blank lines are injected at cell boundaries). */}
          {formatNotebook && hasCodeCells && (
            <button
              className={`toolbar-btn ${isFormatting ? 'toolbar-btn-loading' : ''}`}
              onClick={formatNotebook}
              disabled={isFormatting}
              title="Format all code & SQL cells (ruff / sqlglot)"
            >
              {isFormatting ? <span className="toolbar-spinner" /> : (
                <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="14" y2="12" /><line x1="4" y1="18" x2="18" y2="18" />
                </svg>
              )}
            </button>
          )}
          {/* Lint — run ruff over the whole notebook and show per-cell squiggles. */}
          {lintNotebook && hasCodeCells && (
            <button
              className={`toolbar-btn ${isLinting ? 'toolbar-btn-loading' : ''}`}
              onClick={lintNotebook}
              disabled={!vmAlive || isLinting}
              title="Lint all code & SQL cells (ruff / sqlglot) — shows inline warnings"
            >
              {isLinting ? <span className="toolbar-spinner" /> : (
                <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
                </svg>
              )}
            </button>
          )}
          {onCloseTab && (
            <button className="toolbar-btn toolbar-btn-close" onClick={() => onCloseTab(tab.id)} title="Close notebook (keeps it in the database — reopen anytime)">
              <IconX width={14} height={14} />
            </button>
          )}
          {/* Destructive delete — set apart from Close and rendered in a danger
              style so it isn't mistaken for the everyday close. Routes through the
              SAME confirm modal + permanent-delete handler the Notebooks panel
              uses (wipes cells + all version history); no separate code path. */}
          {onDeleteTab && (
            <button className="toolbar-btn toolbar-btn-delete" onClick={() => onDeleteTab()} title="Delete notebook permanently (wipes all cells and version history)">
              <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
            </button>
          )}
        </div>

      </div>

      <div className="toolbar-pinned">
        {(() => {
          // VM state derived from instances (single source of truth). Prefer the
          // exact-id hit, but fall back to the SESSION's live VM so a rotated or
          // reopened tab (stale microvmId) whose VM is actually RUNNING/SUSPENDED
          // isn't mislabeled "Terminated". Both are pure reads of the polled map —
          // no VM request, so this never wakes a suspended VM.
          let vmInfo = tab.microvmId ? instances[tab.microvmId] : null
          if (!vmInfo) {
            const sessionVm = findSessionVm(instances, tab.sessionId)
            if (sessionVm) vmInfo = sessionVm[1]
          }
          const vmState = vmInfo ? vmInfo.state : null
          const isSuspended = vmState === 'SUSPENDED'
          // Terminated only when the tab HAS a vm id, that id is gone, AND the
          // session has no live VM anywhere (true death, not a rotation/stale id).
          const isTerminated = tab.microvmId && !instances[tab.microvmId] && !vmInfo
          const isUnhealthy = !!vmInfo?.unhealthy
          return (
            <div className="toolbar-status" onClick={() => setShowConnection(true)} title="Click to manage connection">
              <span className={`status-dot ${isUnhealthy ? 'status-terminated' : isSuspended ? 'status-suspended' : isTerminated ? 'status-terminated' : `status-${tab.status}`}`} />
              <span className="status-text">
                {isUnhealthy ? 'Unhealthy' :
                 isSuspended ? 'Suspended' :
                 isTerminated ? 'Terminated' :
                 tab.status === 'connected' ? 'Running' :
                 tab.status === 'connecting' ? 'Connecting...' :
                 tab.status === 'launching' ? 'Launching...' :
                 'Disconnected'}
              </span>
              {tab.microvmId && tab.status === 'connected' && (
                <span className="status-id" title={tab.microvmId}>{tab.microvmId.slice(-12)}</span>
              )}
            </div>
          )
        })()}

        <button className="toolbar-theme-btn" onClick={onToggleTheme} title={`Switch theme (${theme})`}>
          {theme === 'dark' ? <IconSun width={14} height={14} /> : theme === 'light' ? <IconFlame width={14} height={14} /> : <IconMoon width={14} height={14} />}
        </button>
      </div>
    </div>
  )
}
