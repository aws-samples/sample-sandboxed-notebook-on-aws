import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { PROXY_URL, authHeaders } from '../config'
import { apiFetch } from '../services/apiFetch'
import { isVmSuspended, DEFAULT_MEMORY_MIB } from '../constants'
import {
  changedVarsSinceBaseline, collectDownstream, findCycleCells, graphFromDag, isDagDoc, newerDag, orderByPosition,
  runBaseline, staleNodes, withDagProvenance,
} from '../services/depGraph'
import { fetchNotebookDag } from '../services/notebooks'
import { buildLaunchPayload } from '../services/vmLaunch'
import { persistTabVmLink } from '../services/tabVmLink'
import { createDagSync } from '../services/dagDelta'
import { reconcileCellsFromStore } from '../services/cellReconcile'
import { deriveSqlOutputVariable } from '../services/sqlOutputVariable'
import { parseParams, parsePythonValue } from '../components/ParamWidgets'
import { hasImageOutput, restoreCellIds } from '../services/cellOutput'
import { outputFieldsFromResult } from '../services/cellOutputs'
import { tintAfterRunAll, tintBeforeRunAll } from '../services/execTint'
import { buildBanner, buildRunBanner, clearBannerPatch } from '../services/runBanner'
import {
  SLOTS, makeBanner, packageBannerText, pipCellOutcome, pipInstallPackages, shouldShowPackageBanner,
} from '../services/actionBanner'
import { publish as publishBanner } from '../services/bannerStore'
import {
  RECOVERY_RETRY_DELAY_MS, RECONNECTING_OUTPUT, RECOVERY_EXHAUSTED_ERROR,
  readErrorResponse, isRetryableRecovery, shouldRetryRecovery, batchFailureError, runGate, recoverEndpoint,
} from '../services/runRequest'

// How long just-inserted (AI-assistant) cells stay highlighted, in the notebook
// AND in the Outline panel. The cellFlash* animations in Cell.css and the
// outlineFlash* animations in Sidebar.css both last 3 s; change them together.
export const INSERT_FLASH_MS = 3000

// A brief acknowledgment flash for actions that may make NO visible change (e.g.
// Format on an already-formatted cell). Short so it reads as "ran, done", not the
// longer "look here, new content" insert highlight.
const ACK_FLASH_MS = 700

/**
 * useNotebookCells — owns the notebook's cell state and everything that mutates
 * or runs it: cell CRUD, drag-to-reorder, tab persistence/sync, and the full
 * execution pipeline (executeCell/executeAllCells/interrupt + the
 * sequential queue, the outline "run from"/"run selected" listeners, variable
 * fetching, and metrics-on-execution). Extracted from Notebook.jsx as one
 * cohesive unit because these pieces share refs (prevCellsRef, executionQueue)
 * and would be fragile to split.
 */

// Permanent, document-owned cell identity (like Jupyter's nbformat cell `id`,
// Deepnote/Hex GUIDs): generated ONCE at cell creation, persisted with the
// notebook, and never regenerated on reload/restore. This is what variable
// provenance ("Defined in cell X") points at, so it must survive a session
// restore — the VM stores this id in provenance and the reloaded notebook must
// still carry the same id for it to resolve. NOT to be confused with the
// per-run execution counter (executionNumber), which is expected to change.
export function newCellId() {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  } catch {}
  // Fallback for environments without crypto.randomUUID.
  return `cell-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

// Default output variable name of a SQL cell (shared with the Python builder's
// derive_sql_output_variable; parity-tested in depGraph.test.mjs).
const _deriveSqlVarName = deriveSqlOutputVariable

// Condense a cell's error into a compact, tooltip-friendly string for the Run
// History record. Python tracebacks are multi-line and can be long; the LAST
// line is the actual exception ("NameError: name 'x' is not defined"), which is
// what a hover most wants — so we prefer that, then cap length. The full error
// still lives in the cell's own output; this is only the persisted summary that
// survives reload in run_cells_json (kept small so many runs don't bloat it).
const _ERROR_MAX_LEN = 600
function _shortError(err) {
  if (!err) return null
  let s = String(err).trim()
  if (!s) return null
  const lines = s.split('\n').map(l => l.trim()).filter(Boolean)
  // Prefer the exception line (last non-empty) when it looks like "SomeError: msg".
  const last = lines[lines.length - 1]
  if (last && /^[A-Za-z_.]*(Error|Exception|Warning)\b/.test(last)) {
    s = last
  } else {
    s = lines.join(' ')
  }
  if (s.length > _ERROR_MAX_LEN) s = s.slice(0, _ERROR_MAX_LEN - 1) + '…'
  return s
}

// Packages panel banner for cells that ran `pip install` (best effort: package
// names are parsed from the code, failure is the cell error or pip's "ERROR:"
// lines). `results` is the run's Map(cellId -> {status, execMs, error}). One banner
// per run: the first failure, else every installed package. Interrupted cells skip.
function publishPipCellBanner(results, cells) {
  if (!results || results.size === 0) return
  const byId = new Map(cells.map(c => [String(c.id), c]))
  const names = []
  let elapsedMs = 0
  let failure = null
  let anyPip = false
  for (const [cellId, r] of results) {
    const cell = byId.get(String(cellId))
    if (!cell || cell.type !== 'code' || r.status === 'interrupted') continue
    const pkgs = pipInstallPackages(cell.code)
    if (!pkgs) continue
    anyPip = true
    elapsedMs += r.execMs || 0
    const outcome = pipCellOutcome({ status: r.status, output: cell.output, error: r.error || cell.error })
    if (!outcome.ok && !failure) failure = { pkgs, error: outcome.error }
    names.push(...pkgs)
  }
  if (!anyPip) return
  const ok = !failure
  if (!shouldShowPackageBanner({ ok, elapsedMs })) return
  const pkgs = ok ? names : failure.pkgs
  publishBanner(SLOTS.PACKAGES, makeBanner({
    ...packageBannerText({ ok, pkg: pkgs, elapsedMs, error: ok ? '' : failure.error }),
    slot: SLOTS.PACKAGES,
    payload: { pkg: (pkgs[0] || '').split(/[<>=!~[\s]/)[0].trim(), ok, fromCell: true },
  }))
}

function createCell(type = 'code') {
  return {
    id: newCellId(),
    type,  // 'code' | 'markdown' | 'sql'
    code: '',
    output: null,
    error: null,
    html: null,
    image: null,
    imageS3Key: null, // persisted chart location (S3 key) — set by a run, saved by autosave
    status: 'idle', // idle | running | success | error
    executionNumber: null,
    executionTime: null,
    lastExecutedCode: null, // snapshot of code at execution time (for staleness detection)
    outputVariable: type === 'sql' ? 'result' : null, // SQL cells store result as a named DataFrame
  }
}

export function useNotebookCells({ tab, instances = {}, onUpdateTab, onPatchCell, onMarkVmRunning, onRefreshMetrics, viewMode }) {
  const [cells, setCells] = useState(() => {
    // Restore cells from tab state (persists across tab switches)
    if (tab._cells && Array.isArray(tab._cells) && tab._cells.length > 0) {
      // PRESERVE each cell's permanent id across reload/restore so variable
      // provenance keeps resolving. Only mint a new stable id when one is missing
      // or duplicated (never blindly regenerate — that's what used to orphan
      // provenance on every reload).
      // A re-minted cell drops its imageS3Key (the key belongs to the old id).
      return restoreCellIds(tab._cells, newCellId)
    }
    // If tab has pre-loaded cells (from sample), use them. Preserve any id the
    // sample carries; otherwise mint a permanent one.
    if (tab._loadedCells && Array.isArray(tab._loadedCells)) {
      return tab._loadedCells.map(c => {
        const type = c.type || 'code'
        return {
          id: c.id || newCellId(),
          type,
          code: c.code || '',
          output: c.output || null,
          error: c.error || null,
          html: c.html || null,
          image: c.image || null,
          imageS3Key: null, // a sample's chart key would belong to another notebook
          status: 'idle',
          executionNumber: c.executionNumber || null,
          executionTime: c.executionTime || null,
          lastExecutedCode: c.lastExecutedCode || null,
          // Preserve the SQL result variable name from the sample; without this
          // SQL cells lost their defined name and fell back to the derived default.
          outputVariable: type === 'sql'
            ? (c.outputVariable || _deriveSqlVarName(c.code || ''))
            : (c.outputVariable ?? null),
        }
      })
    }
    return [createCell()]
  })
  const [activeCellId, setActiveCellId] = useState(null)
  // Last cell id announced to the Outline via 'notebook-active-cell'. The sync
  // effect below only announces when the active id CHANGES; cell-list churn (e.g.
  // a cell rewrite during Run All) leaves the id as it was and must not re-announce.
  const lastNotifiedActiveRef = useRef(null)
  // Announce the active cell to the Outline. Explicit selections (focus, click)
  // call this directly and always announce, even when the id is unchanged, so
  // re-clicking a scrolled-away cell still reveals its row. The effect below calls
  // it only when the id actually changes.
  const notifyActiveCell = useCallback((cellId) => {
    lastNotifiedActiveRef.current = cellId
    window.dispatchEvent(new CustomEvent('notebook-active-cell', { detail: { cellId } }))
  }, [])
  const [dragOverId, setDragOverId] = useState(null)
  // Seed from the tab's PERSISTED variables (tab._variables) rather than always
  // starting empty. Notebook is keyed by tab.id in App.jsx, so switching away and
  // back REMOUNTS this hook — a bare {} start meant the per-cell "Variables" chip
  // blocks (derived from this state via defined_by) vanished until the next
  // /variables fetch. But fetchVariables is SUSPEND-GATED, so if the VM had idle-
  // suspended, that refill never happened and the blocks stayed gone — even though
  // the sidebar (which reads tab._variables directly) still showed them. Seeding
  // from the persisted copy keeps cells and sidebar consistent across remounts and
  // while suspended; a later fetch overwrites it with the live namespace.
  const [variables, setVariables] = useState(() => tab?._variables || {})
  const [isExecuting, setIsExecuting] = useState(false)
  const [runProgress, setRunProgress] = useState(null) // { current: N, total: M } during Run All
  // Ids (as strings) of just-inserted cells to briefly flash. Driven via state so
  // the highlight survives React re-renders (a direct DOM class gets overwritten).
  const [flashCellIds, setFlashCellIds] = useState(() => new Set())
  const flashTimerRef = useRef(null)

  const prevCellsRef = useRef(cells)
  const draggedCellRef = useRef(null)
  const executionQueue = useRef([])
  const tagSuggestedRef = useRef(false)
  const prevExecutingRef = useRef(false)
  // Current VM-state map, read inside stable callbacks (fetchVariables) without
  // adding `instances` to their deps — same rationale as tabsRef elsewhere.
  const instancesRef = useRef(instances)
  instancesRef.current = instances

  // Always-current ref to onPatchCell so an in-flight execution's completion
  // handler writes results straight into the App-level tab store — even if this
  // Notebook has since unmounted (user switched tabs). setCells alone is a no-op
  // on an unmounted component, which is what left cells stuck on "running".
  const onPatchCellRef = useRef(onPatchCell)
  useEffect(() => { onPatchCellRef.current = onPatchCell }, [onPatchCell])

  // Tracks the in-flight execution "run" (a queue drain = one run boundary).
  // trigger is set by the caller (batch runs override it); cellIds accumulate
  // as cells are enqueued; startedAt marks the drain start. On drain completion
  // we POST a summary to /notebook-runs (write-only history via the DB layer).
  const runTriggerRef = useRef('single')
  const runCellIdsRef = useRef(new Set())
  const runStartedAtRef = useRef(0)
  // The newest stored DAG document (proxy-built; design §8.2). The ref is read by
  // stable callbacks (drain baseline, finishRun) without a stale closure; `dag`
  // state re-renders consumers. applyDag only ever moves FORWARD (newerDag).
  const [dag, setDag] = useState(() => (isDagDoc(tab._dag) ? tab._dag : null))
  const latestDagRef = useRef(dag)
  const onUpdateTabRef = useRef(onUpdateTab)
  onUpdateTabRef.current = onUpdateTab
  const applyDag = useCallback((doc, { fromTab = false } = {}) => {
    if (!newerDag(latestDagRef.current, doc)) return false
    latestDagRef.current = doc
    setDag(doc)
    if (!fromTab && onUpdateTabRef.current) onUpdateTabRef.current({ _dag: doc })
    return true
  }, [])
  // Execute responses and the batch `dag` frame carry a `dag_delta` (or the full
  // `dag`); dagSync applies them in arrival order on top of latestDagRef and, on a
  // gap / hash mismatch / missing payload, resyncs ONCE via GET
  // /notebooks/{id}/dag when finishRun awaits settled() — before any reactive
  // decision reads the document (delta-plan §0.8).
  const tabIdRef = useRef(tab.id)
  tabIdRef.current = tab.id
  const dagSyncRef = useRef(null)
  if (dagSyncRef.current === null) {
    dagSyncRef.current = createDagSync({
      getDoc: () => latestDagRef.current,
      applyDoc: (doc) => applyDag(doc),
      fetchFull: () => (tabIdRef.current != null ? fetchNotebookDag(tabIdRef.current) : null),
    })
  }
  // Baseline for "which variables changed in this run": the DAG runtime clock and
  // its VM at run start (captured per drain into a local const, so overlapping runs
  // can't poison each other's baseline). A result from a different VM compares
  // against clock 0 (changedVarsSinceBaseline).
  const dagBaselineNow = () => runBaseline(latestDagRef.current)
  // Phase 2 reactive auto-run. reactiveRef mirrors the per-notebook toggle (read
  // inside the drain closure without a stale capture). cascadeVisitedRef tracks
  // cells already auto-run in the CURRENT cascade chain so a cell can't re-trigger
  // itself (cycles, x=x+1) — reset when a fresh user-initiated run starts.
  const reactiveRef = useRef(!!tab._reactive)
  useEffect(() => { reactiveRef.current = !!tab._reactive }, [tab._reactive])
  // App mode is inherently live: moving a control should update the app, so we
  // ALWAYS cascade dependents there, regardless of the notebook reactive toggle.
  const appModeRef = useRef(viewMode === 'app')
  useEffect(() => { appModeRef.current = viewMode === 'app' }, [viewMode])
  const cascadeVisitedRef = useRef(new Set())
  // Per-drain identity. Every drain increments drainGenRef at start and captures its
  // own gen; its async completion (.then) bails if a NEWER drain has since started —
  // this makes all the cascade state single-owner and removes the overlapping-run
  // races (stale baselines, clobbered visited set) the old shared booleans caused.
  const drainGenRef = useRef(0)
  // The exact cell ids the reactive cascade just enqueued. The next drain is a
  // cascade CONTINUATION iff every cell it runs is in this set (preserve the visited
  // set); otherwise it's a fresh user run (reset visited). Tracking the actual cells
  // — not a predicted gen number — is robust to timing/interleaving. Cleared when a
  // cascade chain terminates so the next fresh run always starts clean.
  const pendingCascadeCellsRef = useRef(null)
  const CASCADE_MAX_DEPTH = 50  // hard cap: never auto-run more than this many cells in one chain
  const runInterruptedRef = useRef(false)  // set by interruptExecution; read by recordRun
  // Synchronous drain guard. isExecuting (React state) is stale within a Run All's
  // tight await loop, so each executeCell used to spin up its OWN drain -> one
  // notebook_runs row PER cell. This ref flips synchronously so only the FIRST
  // executeCell establishes the drain; the rest just enqueue -> ONE row per run.
  const isDrainingRef = useRef(false)
  // Abort handle for the in-flight batch run (/execute-batch SSE). Interrupt aborts
  // the stream (client side) in addition to POSTing /interrupt (server side).
  const batchAbortRef = useRef(null)
  // Post-run routine (recordRun + staleness + reactive cascade), shared by the
  // per-cell drain and the batch path. Held in a ref so both can call it without a
  // declaration-order dependency (it's defined once executeCell exists).
  const finishRunRef = useRef(null)
  // Outline run banner of a run whose reactive cascade is still going: the cascade's
  // finishRun summarises both together (see finishRun).
  const runBannerChainRef = useRef(null)
  // Ref to executeBatch so the shared post-run routine (finishRun) can trigger the
  // reactive cascade through the batch path without a declaration-order dependency.
  const executeBatchRef = useRef(null)
  const interruptedCellIdsRef = useRef(new Set())  // cells marked interrupted this run
  // Per-run tally of terminal cell outcomes, accumulated AS cells finish (inside
  // the drain, which keeps running even if the Notebook unmounts on a tab
  // switch). recordRun POSTs from THIS — so a run still records its summary
  // after you navigate away, without reading component state post-unmount.
  // Map cellId -> { status, execMs }.
  const runResultsRef = useRef(new Map())

  // Patch a cell in BOTH the local component state (for the mounted case) and the
  // tab store (unmount-safe). Store write is keyed by cellId, so it lands in the
  // right notebook regardless of which tab is active.
  const patchCell = useCallback((cellId, patch) => {
    // NOTE: we intentionally do NOT suppress 'running' writes here based on
    // runResultsRef. An earlier guard did that to stop a finished cell being
    // re-marked running (the "middle cell stuck running with output" bug), but it
    // ALSO dropped the legitimate promote-to-running for tail cells in a fast batch
    // (whose result frames coalesce), leaving the Outline with no spinner. The
    // stuck-cell case is handled correctly by TWO other, non-blocking mechanisms:
    //   1. the promote-next guard only fires for a cell still 'pending' (an already-
    //      finalized cell is 'success'/'error', so it's never re-promoted), and
    //   2. the persist-effect self-heal demotes any impossible finishedAt+'running'
    //      cell to its terminal status.
    // Those catch the corruption without ever blocking a real running write.
    setCells(prev => prev.map(c => (c.id === cellId ? { ...c, ...patch } : c)))
    if (onPatchCellRef.current) onPatchCellRef.current(cellId, patch)
  }, [])

  // Terminal write for a cell's execution outcome. No-ops if the cell was
  // interrupted by the user this run, so a late-returning /execute response
  // (interrupt makes the pending request resolve) can't downgrade the
  // 'interrupted' status back to 'error'/'success'.
  // `transient.variablesRead` = the execute response's variables_read, used only
  // for SQL @param attribution below (never stored on the cell).
  const finalizeCell = useCallback((cellId, patch, transient = {}) => {
    if (interruptedCellIdsRef.current.has(cellId)) return
    // Record this cell's terminal outcome into the run tally (unmount-safe: the
    // tally is a ref the drain owns, not component state). Only terminal patches
    // carry a finishedAt, so we key off that to avoid logging the 'running' patch.
    if (patch && patch.finishedAt) {
      const allCellsNow = prevCellsRef.current || []
      const c = allCellsNow.find(x => x.id === cellId)
      // @param values this cell is parameterized by, captured from the code AS IT
      // EXECUTED (not re-scanned later). Keyed by the human label when set, else
      // varName. Empty for cells with no params — which keeps the scoped display
      // honest: a cell contributes nothing unless it declares OR uses a param.
      const cellParams = {}
      if (c && c.code) {
        // (a) Params this cell DECLARES via @param annotations.
        for (const p of parseParams(c.code)) {
          const key = (p.config && p.config.label) || p.varName
          cellParams[key] = parsePythonValue(p.currentValue, p.type)
        }
        // (b) Params this SQL cell USES via {var} interpolation. SQL cells rarely
        // declare their own @param — they consume values declared elsewhere (e.g.
        // WHERE country = '{market}'). Attributing by usage makes a scoped SQL cell
        // show the params that actually shaped its result. We use the backend's
        // authoritative `variables_read` (transient.variablesRead) — the names DuckDB
        // actually substituted — rather than re-parsing {tokens} on the client.
        // That list mixes DataFrame reads with scalar reads; resolving each name
        // against the declared-@param map naturally keeps only the real params
        // (DataFrame names aren't in the map). Declaration (a) wins on collision.
        const sqlReads = Array.isArray(transient.variablesRead) ? transient.variablesRead : []
        if (c.type === 'sql' && sqlReads.length) {
          // Build varName -> {label, value} across ALL cells' @param declarations.
          const declaredByVar = {}
          for (const other of allCellsNow) {
            if (!other || !other.code) continue
            for (const p of parseParams(other.code)) {
              declaredByVar[p.varName] = {
                label: (p.config && p.config.label) || p.varName,
                value: parsePythonValue(p.currentValue, p.type),
              }
            }
          }
          for (const varName of sqlReads) {
            const d = declaredByVar[varName]
            if (d && !(d.label in cellParams)) cellParams[d.label] = d.value
          }
        }
      }
      runResultsRef.current.set(cellId, {
        status: patch.status,
        execMs: patch.executionTime || 0,
        type: c?.type || 'code',
        // The error message shown in the cell (only for failed cells), captured
        // here so it can be persisted into the run record and surfaced later as the
        // Run History cell tooltip — the live cell.error is gone after a reload, so
        // the run record is the only durable source. Truncated + single-lined to
        // keep run_cells_json small (the full traceback stays in the cell output).
        error: patch.status === 'error' && patch.error ? _shortError(patch.error) : null,
        // Result shape (SQL cells) — carried into run_cells for the drill-down.
        rows: patch.resultRows ?? null,
        cols: patch.resultCols ?? null,
        bytes: patch.resultBytes ?? null,
        // Client-clock start/end of this cell's execution span (epoch ms, browser
        // clock — same clock as the run-level started_at). Carried into run_cells so
        // run history can build a per-cell timeline: each cell placed at its real
        // offset from run start, width = its wall span, gaps between = overhead.
        clientStartedAt: patch.clientStartedAt ?? null,
        clientFinishedAt: patch.clientFinishedAt ?? null,
        // Peak CPU/memory sampled in the guest during this cell (null if absent).
        peakMemMb: patch.peakMemMb ?? null,
        peakCpuPct: patch.peakCpuPct ?? null,
        // Per-cell @param values (may be empty).
        params: cellParams,
      })
    }
    patchCell(cellId, patch)
  }, [patchCell])

  // Fetch metrics once when execution starts and once when it ends (for burst cost tracking).
  // Never poll an IDLE VM — that keeps it from suspending. The interval below is
  // gated strictly to isExecuting (see next effect), so an idle VM is never polled.
  useEffect(() => {
    if (isExecuting && !prevExecutingRef.current && onRefreshMetrics) {
      onRefreshMetrics() // Fetch on execution start
    }
    if (!isExecuting && prevExecutingRef.current && onRefreshMetrics) {
      // Fetch once after execution ends (delayed to capture final burst state)
      setTimeout(() => onRefreshMetrics(), 1000)
    }
    prevExecutingRef.current = isExecuting
  }, [isExecuting, onRefreshMetrics])

  // Live metrics WHILE a cell is running: poll every ~1.5s, but ONLY for the
  // duration of the run. The moment isExecuting flips false the interval is
  // cleared, so we never poll an idle VM (respects the suspend-cost rule). This
  // is what makes both the execution HUD pill and the VM side-panel gauges tick
  // live during a run. Cheap request/response — no new endpoint, ECS-safe.
  useEffect(() => {
    if (!isExecuting || !onRefreshMetrics) return
    const interval = setInterval(() => onRefreshMetrics(), 1500)
    return () => clearInterval(interval)
  }, [isExecuting, onRefreshMetrics])

  // Scroll new cell into view when added
  useEffect(() => {
    if (activeCellId) {
      const el = document.querySelector(`[data-cell-id="${activeCellId}"]`)
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [cells.length])

  // Persist cells to tab state (survives tab switches).
  //
  // The STORE (tab._cells) is authoritative for a cell's terminal execution result:
  // a batch run keeps streaming after a tab switch and writes each result straight
  // into the store via the unmount-safe patchTabCell path — even while THIS hook is
  // unmounted. So on remount, local `cells` (seeded once at mount) can be STALER than
  // the store for cells that finished during the switch. If we blindly wrote local
  // `cells` back here, we'd OVERWRITE the store's real output with a stale
  // running/no-output snapshot (the "results vanish after switching back" bug).
  //
  // Guard against that: before persisting, RECONCILE each cell against the store —
  // if the store has a TERMINAL result (success/error, or any output/html/image)
  // and our local copy is still running / has no output, adopt the store's cell.
  // This both prevents the clobber AND surfaces the store's output locally (a
  // setCells below re-renders the cell with its results). Cells we're actively
  // editing/running locally are unaffected (local is fresher there, so the store
  // isn't terminal-with-output for them).
  useEffect(() => {
    // STEP 1 — reconcile FROM the store. The store may hold terminal output that
    // landed (via the unmount-safe patchTabCell path) while this hook was unmounted
    // on a tab switch, but that local `cells` (seeded once at mount) is missing.
    // Adopt the store's finished cell for any local cell that is still running/
    // pending or has no output while the store's copy is terminal-with-output. This
    // runs even on the FIRST commit (it does NOT depend on `cells` having changed),
    // which is exactly when the remount seed is stale. Cells we're actively running
    // locally are unaffected (the store isn't terminal-with-output for them).
    // Also self-heals the impossible running + finishedAt state (finishedAt is
    // written only by finalizeCell's terminal patch — a stray promote-to-running
    // clobbered an already-finished cell; the "middle cell stuck running with
    // output" symptom). Rules: services/cellReconcile.js.
    //
    // The decision is made against this render's `cells` + store, but the update is
    // FUNCTIONAL: during a batch, a finalize/promote for another cell can be queued
    // after this render and before this effect runs. setCells(merged-from-`cells`)
    // used to replace state and drop it (the "next cell stuck running" race); the
    // updater instead merges into the latest state and only adopts store copies for
    // cells that are still exactly what this render saw.
    const store = tab._cells
    if (reconcileCellsFromStore(cells, store) !== cells) {
      setCells(prev => reconcileCellsFromStore(prev, store, cells))
      // Persist on the next commit (STEP 2 below), from the reconciled LATEST
      // state — never from this render's possibly-stale `cells`.
      return
    }

    // STEP 2 — persist local→store. We only push up when local genuinely changed
    // since the last persist (prevCellsRef). After an adopt the next commit's local
    // cells ARE the store objects for the adopted cells, so the following pass is a
    // no-op adopt + one persist (no loop).
    if (prevCellsRef.current !== cells) {
      prevCellsRef.current = cells
      onUpdateTab({ _cells: cells })
    }
  }, [cells, tab._cells])

  // Sync active cell index to tab so AI chat panel can reference it
  useEffect(() => {
    if (activeCellId) {
      const idx = cells.findIndex(c => c.id === activeCellId)
      if (idx >= 0) onUpdateTab({ _activeCellIndex: idx })
      // Notify the Outline panel only when the active cell itself changed, so it
      // can highlight the corresponding row. Cell-list changes alone are not news.
      if (activeCellId !== lastNotifiedActiveRef.current) notifyActiveCell(activeCellId)
    }
  }, [activeCellId, cells, notifyActiveCell])

  // Sync cells from tab._cells when the STRUCTURE changes EXTERNALLY (insert from
  // sidebar, reorder, delete, version restore) — a different set/order of cell ids.
  //
  // We must NOT re-sync on per-cell FIELD patches: patchCell writes local setCells
  // AND the App store (tab._cells) for unmount-safety, so every execution frame
  // produces a new tab._cells identity; blindly setCells(tab._cells) on those would
  // round-trip a possibly-lagging store snapshot over fresher local state and stomp
  // a just-finalized cell back to "running" (the batch-stream stuck-cell bug).
  //
  // FIELD-level reconciliation (adopting the store's terminal output that landed
  // while this hook was unmounted on a tab switch) is handled by the persist effect
  // above, which merges the store's finished cells into local on remount. So here we
  // only adopt genuine STRUCTURAL changes and otherwise leave field state to that
  // reconciler — keeping a single owner for store↔local field merges.
  useEffect(() => {
    if (!tab._cells || tab._cells === prevCellsRef.current) return
    const local = prevCellsRef.current || []
    const incoming = tab._cells
    const sameStructure =
      local.length === incoming.length &&
      local.every((c, i) => c.id === incoming[i].id)
    if (sameStructure) return  // field-only diff → the persist effect reconciles it
    // Genuine external structural change (insert/reorder/delete/restore) → adopt it.
    prevCellsRef.current = incoming
    setCells(incoming)
  }, [tab._cells])

  // Listen for code insertion events from sidebar (S3/DynamoDB clicks)
  useEffect(() => {
    // Multi-cell inserts fire one 'insert-code' event per cell. We snapshot the
    // cell count BEFORE the burst renders (the handler runs before React re-renders)
    // so that, once the whole batch is in the DOM, we can flash every inserted cell
    // and scroll to the START of the first one (user reviews/runs from there).
    let burstPreCount = null
    let scrollTimer = null
    // Optional per-event requestId (AI chat Insert tracking): which cell each request
    // landed in, reported back in the notebook-cells-inserted detail as `requests`.
    let burstRequests = []
    const handler = (e) => {
      const { code, type: cellType, requestId } = e.detail
      if (!code) return
      const insertType = cellType || 'code'
      // Snapshot pre-insert cell count on the first event of the burst.
      if (burstPreCount === null) burstPreCount = document.querySelectorAll('.cell').length
      // Id chosen OUTSIDE the updater so a re-run (StrictMode) records the same id.
      const freshId = newCellId()
      const slot = { requestId, cellId: null }
      if (requestId) burstRequests.push(slot)
      setCells(prev => {
        const lastCell = prev[prev.length - 1]
        if (lastCell && !lastCell.code.trim() && lastCell.type === insertType) {
          // Reuse the trailing empty cell if it's the same type
          slot.cellId = lastCell.id
          return prev.map((c, i) => i === prev.length - 1 ? { ...c, code } : c)
        }
        // Otherwise append a new cell at the bottom
        slot.cellId = freshId
        return [...prev, {
          id: freshId,
          type: insertType,
          code,
          output: null,
          error: null,
          html: null,
          image: null,
          imageS3Key: null,
          status: 'idle',
          executionNumber: null,
          executionTime: null,
        }]
      })
      // After the whole burst has rendered (debounced): flash each inserted cell
      // and scroll to the START of the first one. Computed from the live DOM so it
      // doesn't depend on React's async state updater.
      if (scrollTimer) clearTimeout(scrollTimer)
      scrollTimer = setTimeout(() => {
        const cells = Array.from(document.querySelectorAll('.cell'))
        const pre = burstPreCount ?? cells.length
        // If a trailing empty cell was reused, no new cell was appended, so the
        // inserted content is the last cell; otherwise it starts at index `pre`.
        const firstIdx = cells.length > pre ? pre : Math.max(0, cells.length - 1)
        // Collect the inserted cells' ids (from the DOM) and flash them via state,
        // so the highlight isn't wiped by an unrelated re-render.
        const insertedIds = cells.slice(firstIdx)
          .map(el => el.getAttribute('data-cell-id'))
          .filter(Boolean)
        if (insertedIds.length) {
          setFlashCellIds(new Set(insertedIds))
          if (flashTimerRef.current) clearTimeout(flashTimerRef.current)
          flashTimerRef.current = setTimeout(() => setFlashCellIds(new Set()), INSERT_FLASH_MS)
          // SELECT the first inserted cell (usually the block's markdown header) so it
          // becomes the notebook's active cell — the user reviews/runs the new block
          // from its start. (The Outline scrolls to the block's END separately so the
          // whole block is visible there; here in the notebook we anchor on the start.)
          setActiveCellId(insertedIds[0])
        }
        // Scroll to the START of the first inserted cell (review/run from there).
        // .cell has scroll-margin-top so it clears the sticky toolbar. Re-assert the
        // scroll after a short settle delay: inserted markdown can render async
        // (KaTeX/mermaid) and reflow AFTER the first scroll, which otherwise leaves
        // the cell head tucked under the toolbar / mid-cell.
        const scrollToStart = () => {
          const el = document.querySelector(`.cell[data-cell-id="${insertedIds[0]}"]`) || cells[firstIdx]
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        }
        scrollToStart()
        setTimeout(scrollToStart, 260)
        // Let an open Outline panel reveal the newly added rows AND flash them for
        // the same duration as the in-notebook cells (ids passed in the detail).
        const requests = burstRequests
          .filter(r => r.cellId)
          .map(r => ({ requestId: r.requestId, cellId: r.cellId }))
        window.dispatchEvent(new CustomEvent('notebook-cells-inserted', { detail: { cellIds: insertedIds, requests } }))
        burstRequests = []
        burstPreCount = null
        scrollTimer = null
      }, 180)
    }
    window.addEventListener('insert-code', handler)
    return () => {
      window.removeEventListener('insert-code', handler)
      if (scrollTimer) clearTimeout(scrollTimer)
    }
  }, [])

  const fetchVariables = useCallback(async () => {
    if (!tab.microvmEndpoint || tab.status !== 'connected') return
    // Suspend gate: don't wake a sleeping VM to re-read its namespace. The last
    // fetched `variables` stay shown; running a cell (a real user action) wakes the
    // VM and the post-run fetchVariables call refreshes it.
    if (isVmSuspended(instancesRef.current, tab.microvmId)) return
    const headers = authHeaders({ 'Content-Type': 'application/json' })
    if (tab.sessionId) {
      headers['X-Session-Id'] = tab.sessionId
    }
    // Retry-until-warm. A 404/502 here is the cold-start / registration race, NOT
    // an empty namespace: right after /launch (or right after a cell runs on a VM
    // that is still resuming) the proxy's session→VM map can briefly miss, so
    // /variables resolves to 404 (0ms, no VM forward) or 502 (VM mid auto-resume).
    // The /helpers + /completions-catalog loaders already retry through this window
    // (see CellEditor.jsx); fetchVariables previously did ONE fetch and, on a
    // transient 404, silently returned null — so the post-run refresh dropped the
    // just-created variables and the panel stayed "No variables yet" until the VM
    // idle-suspended (after which the suspend gate above blocks every later auto
    // refresh). Retry a few times with backoff; re-check the suspend gate each loop
    // so a VM that genuinely sleeps mid-retry stops cleanly.
    for (let attempt = 0; attempt < 4; attempt++) {
      if (attempt > 0) {
        await new Promise(r => setTimeout(r, 600 * attempt))  // 0, 600, 1200, 1800ms
        if (isVmSuspended(instancesRef.current, tab.microvmId)) break
      }
      try {
        const resp = await fetch(`${tab.microvmEndpoint}/variables`, { headers })
        if (resp.ok) {
          const data = await resp.json()
          const vars = data.variables || {}
          setVariables(vars)
          onUpdateTab({ _variables: vars })
          return vars
        }
        // Only 404 (not-yet-registered) and 502 (resuming) are transient — retry.
        // Any other status (e.g. 400/500) is a real error: stop.
        if (resp.status !== 404 && resp.status !== 502) break
      } catch { /* network blip — fall through to the next attempt */ }
    }
    return null
  }, [tab.microvmEndpoint, tab.sessionId, tab.status, tab.microvmId])

  // Re-fetch the variable namespace on demand (e.g. after the Variables panel
  // deletes a variable from the session).
  useEffect(() => {
    const handler = () => fetchVariables()
    window.addEventListener('refresh-variables', handler)
    return () => window.removeEventListener('refresh-variables', handler)
  }, [fetchVariables])

  // Per-cell version revert (Version History → "Apply this cell to notebook").
  // App.jsx also handles this event to patch the tab store, but that's a field-only
  // change (same cell ids), which the tab._cells → cells sync-back effect above
  // DELIBERATELY skips (local state is authoritative for field-only diffs). So the
  // reverted code would never reach the rendered `cells` without this — we apply it
  // to LOCAL state via setCells here (the authoritative source Cell renders from).
  // Cell.jsx separately bumps its editorVersion on the same event to remount the
  // editor so CodeMirror re-reads the new code immediately. Scoped to THIS notebook.
  useEffect(() => {
    const onRevertCell = (e) => {
      const { notebookId, cellId, code } = e.detail || {}
      if (String(notebookId) !== String(tab.id) || cellId == null) return
      setCells(prev => prev.map(c => (
        String(c.id) === String(cellId)
          ? { ...c, code: code || '', output: null, error: null, html: null, image: null, imageS3Key: null,
              executionNumber: null, executionTime: null, status: 'idle', lastExecutedCode: null }
          : c
      )))
    }
    window.addEventListener('notebook-revert-cell', onRevertCell)
    return () => window.removeEventListener('notebook-revert-cell', onRevertCell)
  }, [tab.id])

  // Variable VALUES once the notebook is connected (provenance comes from the DAG).
  useEffect(() => {
    if (tab.status === 'connected' && tab.microvmEndpoint) {
      fetchVariables()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchVariables])

  // DAG arrival paths (design §8.2):
  //  - tab._dag: reopen (App fetchNotebook → nb.dag), autosave PUT response,
  //    version restore response, create (POST response);
  //  - mount without a document, and every VM connect (the proxy awaits the
  //    attach/reconcile before answering) → GET /notebooks/{id}/dag.
  useEffect(() => {
    if (tab._dag) applyDag(tab._dag, { fromTab: true })
  }, [tab._dag, applyDag])
  const isConnected = tab.status === 'connected'
  useEffect(() => {
    if (tab.id == null) return
    if (latestDagRef.current && !isConnected) return
    let cancelled = false
    fetchNotebookDag(tab.id).then(doc => { if (!cancelled && doc) applyDag(doc) })
    return () => { cancelled = true }
  }, [tab.id, isConnected, applyDag])

  // Persist a run summary to the DB (via the /notebook-runs route → storage
  // interface). Write-only history; the live sidebar still derives from cells.
  // Fire-and-forget: never let a logging failure surface to the user.
  const recordRun = useCallback((startedAt, results, trigger = 'single') => {
    try {
      // results: Map cellId -> { status, execMs }, accumulated in the drain as
      // cells finished. Unmount-safe — no component-state read here.
      const outcomes = Array.from(results.values())
      const cellsExecuted = outcomes.length
      if (cellsExecuted === 0) return  // nothing ran — skip
      const cellsInterrupted = outcomes.filter(o => o.status === 'interrupted').length
      const cellsErrored = outcomes.filter(o => o.status === 'error').length
      const totalExecMs = outcomes.reduce((sum, o) => sum + (o.execMs || 0), 0)
      const notebookId = String(tab.id)

      // Per-cell breakdown for the run-history drill-down. Build a PY-n/SQL-n/MD-n
      // badge map from the full cell list (same scheme as the cell badges), then
      // emit one entry per executed cell in notebook order.
      const allCells = prevCellsRef.current || []
      const badgeCounters = { code: 0, sql: 0, markdown: 0 }
      const badgePrefix = { code: 'PY', sql: 'SQL', markdown: 'MD' }
      const badgeById = new Map()
      for (const c of allCells) {
        const t = c.type || 'code'
        badgeCounters[t] = (badgeCounters[t] || 0) + 1
        badgeById.set(c.id, `${badgePrefix[t] || 'PY'}-${badgeCounters[t]}`)
      }
      const posById = new Map(allCells.map((c, i) => [c.id, i]))
      const runCells = Array.from(results.entries())
        .map(([cellId, o]) => ({
          cell_id: String(cellId),
          label: badgeById.get(cellId) || '',
          type: o.type || 'code',
          status: o.status,
          exec_ms: Math.round(o.execMs || 0),
          // Error text for failed cells (truncated), so Run History can show WHY a
          // cell errored as a tooltip on the red row — durable across reloads.
          error: o.error || null,
          position: posById.has(cellId) ? posById.get(cellId) : 9999,
          // Client-clock execution span (ISO strings) for the per-cell timeline.
          // Same clock as the run's started_at/finished_at, so cells line up on the
          // run's wall-clock axis and the gaps between them are the per-cell overhead.
          started_at: o.clientStartedAt != null ? new Date(o.clientStartedAt).toISOString() : null,
          finished_at: o.clientFinishedAt != null ? new Date(o.clientFinishedAt).toISOString() : null,
          // Peak resource use during this cell (guest-sampled): process RSS MB +
          // best-effort CPU%. Explains WHY a cell was heavy (a fast cell can still
          // allocate a large DataFrame). Null for older VMs that don't report them.
          peak_mem_mb: o.peakMemMb ?? null,
          peak_cpu_pct: o.peakCpuPct ?? null,
          // Result shape (SQL cells; null for others).
          rows: o.rows ?? null,
          cols: o.cols ?? null,
          bytes: o.bytes ?? null,
          // @param values this cell declared (captured at exec time; may be {}).
          // The run-history UI accumulates these across the cells in scope.
          params: o.params || {},
        }))
        .sort((a, b) => a.position - b.position)

      apiFetch(`/notebook-runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          notebookId,
          sessionId: tab.sessionId || null,
          microvmId: tab.microvmId || null,
          trigger,
          startedAt: startedAt ? new Date(startedAt).toISOString() : null,
          finishedAt: new Date().toISOString(),
          cellsExecuted,
          cellsErrored,
          cellsInterrupted,
          totalExecMs,
          runCells,
        }),
      })
        .then(() => {
          // Let an open Run History (Notebooks panel) refresh for this notebook.
          window.dispatchEvent(new CustomEvent('notebook-run-recorded', { detail: { notebookId } }))
        })
        .catch(() => {})
    } catch {
      /* logging must never break execution */
    }
  }, [tab.id, tab.sessionId, tab.microvmId])

  // GUARD shared by executeCell and executeBatch: the data source list must be
  // resolved before ANY cell runs, so SQL routing knows each source's TYPE
  // (Athena/Aurora/DynamoDB/S3/local). Without it, an Athena/Aurora table could
  // misroute (fall through to DuckDB). Routing needs only type/identity —
  // source_type/source_id/metadata — NOT column schemas (verified in
  // sql_engine._get_routing_catalogs), so we gate on the LIGHTWEIGHT _sourcesReady
  // (from /datasources), matching the toolbar Run gate. This unblocks execution as
  // soon as the source list lands, rather than waiting on the full enriched catalog
  // (schemas + entity docs) that _catalogLoaded tracks. The VM's own routing catalog
  // is loaded synchronously in its /run hook (from the proxy-staged S3 catalog)
  // before it ever serves a cell, so a connected VM can always route correctly.
  // Entry points: executeCell (Run button, Shift+Enter, and per-cell
  // fallback inside executeBatch) and executeBatch (Run All via executeAllCells,
  // Outline "Run from here" / "Run selected", runStaleDownstream and the reactive
  // cascade). Both call this, so every path is gated. A blocked run auto-triggers
  // the fetch (refresh-datasources) and surfaces ONE brief "Loading data sources…"
  // notice (a blocked batch shows it once, not per cell, and leaves the cells
  // unchanged); buttons also disable on !_sourcesReady.
  // EXCEPTION: a SUSPENDED VM intentionally skips its fetches (suspend gate), so
  // _sourcesReady is false — but running a cell auto-resumes the VM and the source
  // list loads on resume. Blocking here would DEADLOCK (can't run → can't resume →
  // never loads), and it must match the toolbar's Run gate, which treats suspended
  // as runnable. So only block when the source list isn't ready AND not suspended.
  const canRunNow = useCallback(() => {
    const gate = runGate({
      sourcesReady: !!tab._sourcesReady,
      suspended: isVmSuspended(instancesRef.current, tab.microvmId),
    })
    if (gate.allowed) return true
    window.dispatchEvent(new CustomEvent('refresh-datasources'))
    window.dispatchEvent(new CustomEvent('datasource-catalog-loading', {
      detail: { message: gate.notice },
    }))
    return false
  }, [tab._sourcesReady, tab.microvmId])

  // Endpoint for THIS run, shared by executeCell and executeBatch. A tab restored
  // after a page refresh can have a null microvmEndpoint; if the VM is still listed
  // in /instances, rebuild the session-routed proxy endpoint, write it to the tab
  // for later runs, and return it so this run uses it too (the `tab` closure is
  // stale until the next render). `recovered` = the tab counts as connected now.
  const resolveEndpoint = useCallback(async () => {
    if (tab.microvmEndpoint) return { endpoint: tab.microvmEndpoint, recovered: false }
    let inst = null
    try {
      const instResp = await apiFetch(`/instances`)
      const instData = await instResp.json()
      inst = instData.instances?.[tab.microvmId] || null
    } catch {}
    const res = recoverEndpoint(null, inst, `${PROXY_URL}/proxy`)
    if (res.recovered) onUpdateTab({ microvmEndpoint: res.endpoint, status: 'connected' })
    return res
  }, [tab.microvmEndpoint, tab.microvmId, onUpdateTab])

  const executeCell = useCallback(async (cellId) => {
    // If no VM linked at all, can't execute
    if (!tab.microvmId) return

    // Data-source-ready gate (see canRunNow above).
    if (!canRunNow()) return
    // A single-cell run starts: drop the Outline run banner (services/runBanner.js).
    onUpdateTab(clearBannerPatch())

    // Ensure we have an endpoint (might be missing after page refresh). After a
    // successful recovery this run uses the recovered endpoint and counts as
    // connected, even though this closure's `tab` still holds the old values.
    // Only await when recovery is needed, so the common path enqueues synchronously
    // (the drain guard relies on that ordering, e.g. Run All's per-cell fallback).
    const { endpoint, recovered } = tab.microvmEndpoint
      ? { endpoint: tab.microvmEndpoint, recovered: false }
      : await resolveEndpoint()
    const connected = recovered || tab.status === 'connected' || tab.status === 'launching'

    if (!endpoint || !connected) {
      // Auto-restore: if VM was terminated but has a checkpoint, auto-launch + restore
      if (tab.sessionSaved && tab.sessionId && !tab._autoRestoreFailed) {
        // Set a guard state to prevent polling from interfering
        onUpdateTab({ status: 'launching' })
        setCells(prev => prev.map(c =>
          c.id === cellId ? { ...c, status: 'running', output: null, error: null } : c
        ))
        try {
          const resp = await apiFetch(`/launch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              ...buildLaunchPayload({ ...tab, name: tab.name || 'Restored' }, { restore: true }),
              memoryMiB: tab.microvmMemory || DEFAULT_MEMORY_MIB,
              // Same arch as the tier above (the server still pins the saved arch).
              ...(tab.microvmArch ? { arch: tab.microvmArch } : {}),
              idleTimeoutSeconds: tab.idleTimeoutSeconds || 60,
            }),
          })
          if (resp.ok) {
            const data = await resp.json()
            persistTabVmLink(tab.id, data)  // saved now, not after the 1.5 s autosave
            onUpdateTab({
              microvmId: data.microvmId,
              microvmEndpoint: `${PROXY_URL}/proxy`,
              microvmMemory: data.memory_mib || tab.microvmMemory || DEFAULT_MEMORY_MIB,
              microvmArch: data.architecture || tab.microvmArch,
              status: 'connected',
              mode: 'microvm',
              sessionSaved: false,
              checkpointEnabled: true,
              // Reuse the notebook's session; fall back to tab.sessionId so a
              // malformed response can't silently rebind the notebook.
              sessionId: data.sessionId || tab.sessionId,
            })
            // The VM is back, but this cell is not re-run: tell the user to run it again
            setCells(prev => prev.map(c =>
              c.id === cellId ? { ...c, status: 'idle', output: '♻️ Session restored — re-run this cell.', error: null } : c
            ))
          } else {
            onUpdateTab({ status: 'disconnected', _autoRestoreFailed: true })
            setCells(prev => prev.map(c =>
              c.id === cellId ? { ...c, status: 'error', error: '⚠️ Failed to auto-restore session. Use the connection panel to restore manually.' } : c
            ))
          }
        } catch (err) {
          onUpdateTab({ status: 'disconnected', _autoRestoreFailed: true })
          setCells(prev => prev.map(c =>
            c.id === cellId ? { ...c, status: 'error', error: `⚠️ Auto-restore failed: ${err.message}` } : c
          ))
        }
      }
      return
    }

    const cell = prevCellsRef.current.find(c => c.id === cellId)
    if (!cell || !cell.code.trim()) return
    if (cell.type === 'markdown') return  // Markdown cells don't execute

    // Queue execution — cells run sequentially to maintain state consistency.
    // `retriedRecovery` guards the one-shot auto-retry after a reactive-recovery
    // 503 (eternal mode) so we don't loop forever if recovery keeps failing.
    // Client-clock start of THIS cell's execution span (browser wall clock). Set on
    // the first attempt only, so a reactive-recovery retry doesn't reset it — the
    // span should cover the whole time the user waited for this cell, including the
    // reconnect. Captured here (not from the transient `startedAt` patch, which is
    // nulled at finalize) and threaded into the run tally so the run-history
    // timeline can place each cell on the run's wall-clock axis and show the gaps
    // (= per-cell overhead) between them. Same clock as the run-level started_at.
    // Stamped when doExecute ACTUALLY begins (see below), not here — during Run All
    // every cell's executeCell runs synchronously at enqueue time, so capturing it
    // here would give every cell the same enqueue timestamp. The sequential drain
    // invokes doExecute one at a time, so stamping inside it records each cell's real
    // execution-start; the gaps between cells' spans are then the true overhead.
    let cellStartedAt = 0
    const doExecute = async (retriedRecovery = false) => {
      // First attempt only: record the real start (a recovery retry re-enters
      // doExecute but the span should still cover the whole wait, incl. the reconnect).
      if (!retriedRecovery) cellStartedAt = Date.now()
      // Mark as running. startedAt (epoch ms) lets the elapsed timer compute from
      // a real start time, so it survives a tab-switch remount instead of resetting.
      patchCell(cellId, { status: 'running', output: null, error: null, startedAt: Date.now() })

      // Build headers
      const headers = authHeaders({ 'Content-Type': 'application/json' })
      if (tab.sessionId) {
        headers['X-Session-Id'] = tab.sessionId
      }

      try {
        // Route SQL cells to /execute-sql, code cells to /execute
        const isSql = cell.type === 'sql'
        const url = isSql
          ? `${endpoint}/execute-sql`
          : `${endpoint}/execute`
        const body = isSql
          ? JSON.stringify({ sql: cell.code, output_variable: cell.outputVariable || _deriveSqlVarName(cell.code), cell_id: String(cellId) })
          : JSON.stringify({ code: cell.code, cell_id: String(cellId) })

        const response = await fetch(url, {
          method: 'POST',
          headers,
          body,
        })

        const text = await response.text()
        let result
        try {
          result = JSON.parse(text)
        } catch {
          finalizeCell(cellId, { status: 'error', error: '⚠️ Sandbox unresponsive — it may have crashed or been terminated. Click "Terminate" in the MicroVMs panel and launch a new one.', startedAt: null, finishedAt: Date.now(), clientStartedAt: cellStartedAt, clientFinishedAt: Date.now() })
          return
        }

        // Reactive rotation (eternal mode): the proxy detected the VM had died
        // and could not restore a replacement after retries. State is SAFE in S3
        // (session_safe) and it's retryable — the next request re-attempts
        // recovery from the same checkpoint. Show a non-alarming "reconnecting"
        // state and auto-retry ONCE before surfacing a manual-restore hint.
        // Same policy as executeBatch (services/runRequest).
        if (isRetryableRecovery(response.status, result)) {
          if (shouldRetryRecovery(response.status, result, retriedRecovery)) {
            patchCell(cellId, { status: 'running', output: RECONNECTING_OUTPUT, error: null })
            await new Promise(r => setTimeout(r, RECOVERY_RETRY_DELAY_MS))
            return doExecute(true)  // one retry; recovery re-attempts server-side
          }
          finalizeCell(cellId, { status: 'error', error: RECOVERY_EXHAUSTED_ERROR, startedAt: null, finishedAt: Date.now(), clientStartedAt: cellStartedAt, clientFinishedAt: Date.now() })
          return
        }

        if (result.error && !('success' in result)) {
          finalizeCell(cellId, { status: 'error', error: result.error, startedAt: null, finishedAt: Date.now(), clientStartedAt: cellStartedAt, clientFinishedAt: Date.now() })
          return
        }

        finalizeCell(cellId, {
          status: result.success ? 'success' : 'error',
          output: result.output || null,
          error: result.error || null,
          // Discrete 1-indexed error line from the backend (null on success or when
          // no cell frame was in the traceback). The editor prefers this; if it's
          // absent (older VM / SQL), Cell.jsx falls back to parsing "(line N)".
          errorLineno: typeof result.error_lineno === 'number' ? result.error_lineno : null,
          // Rich debug context (empty on success): cleaned traceback text, the
          // cell-local frames [{lineno,name,line}], and the failing frame's locals.
          errorTraceback: result.traceback || null,
          errorFrames: Array.isArray(result.error_frames) ? result.error_frames : [],
          errorLocals: result.error_locals && typeof result.error_locals === 'object' ? result.error_locals : {},
          // html / image / imageS3Key: today's legacy fields for a single output; a
          // multi-output run (several tables / charts / images, or text after one)
          // stores the ordered list as an envelope in `html` (services/cellOutputs).
          // imageS3Key is the proxy's content-addressed S3 key of the LAST chart (null:
          // no chart, or not persisted). Set in the SAME patch as executedAt (one autosave).
          ...outputFieldsFromResult(result),
          executionNumber: result.execution_number,
          executionTime: result.execution_time_ms,
          lastExecutedCode: cell.code,
          // Result-shape metadata (SQL cells) for the run-history drill-down: how
          // many rows/cols the query produced and the frame's in-memory size. Lets
          // the drill-down explain why a cell was slow (a lot of data vs slow query).
          resultRows: typeof result.row_count === 'number' ? result.row_count : null,
          resultCols: typeof result.column_count === 'number' ? result.column_count : null,
          resultBytes: typeof result.size_bytes === 'number' ? result.size_bytes : null,
          // Peak resource use during this cell (guest-sampled) for run history.
          peakMemMb: typeof result.peak_mem_mb === 'number' ? result.peak_mem_mb : null,
          peakCpuPct: typeof result.peak_cpu_pct === 'number' ? result.peak_cpu_pct : null,
          // Lineage: when this run finished (client clock). Reads/writes/sources
          // live in the stored DAG (result.dag_delta / result.dag), not on the cell.
          executedAt: new Date().toISOString(),
          engine: result.engine || null,
          startedAt: null,
          finishedAt: Date.now(),
          clientStartedAt: cellStartedAt,
          clientFinishedAt: Date.now(),
        }, { variablesRead: result.variables_read })
        // The proxy captured this run into the notebook DAG and returns a delta
        // against the previous stored document (or the full one). Without either
        // (capture failed / scratch session) finishRun fetches the document.
        await dagSyncRef.current.ingest(result)

        // If execution succeeded and VM was suspended, immediately mark it as RUNNING
        // (don't wait for the 15 s poll — this is the ONE exception to poll-only updates)
        if (result.success && tab.microvmId && onMarkVmRunning) {
          onMarkVmRunning(tab.microvmId)
        }
      } catch (err) {
        finalizeCell(cellId, { status: 'error', error: `Connection error: ${err.message}`, startedAt: null, finishedAt: Date.now(), clientStartedAt: cellStartedAt, clientFinishedAt: Date.now() })
      }
    }

    // Add to queue and process sequentially
    executionQueue.current.push(doExecute)
    runCellIdsRef.current.add(cellId)
    // Only the FIRST caller establishes the drain (guard is a ref so it flips
    // synchronously — Run All enqueues many cells before React re-renders, so a
    // state-based guard would let each cell start its own drain). Subsequent
    // executeCell calls in the same run just enqueue and return; the running
    // drain below picks them up, giving exactly ONE run per batch.
    if (!isDrainingRef.current) {
      isDrainingRef.current = true
      setIsExecuting(true)
      runStartedAtRef.current = Date.now()
      const runStartedAt = runStartedAtRef.current
      // Identity for THIS drain. The async completion below compares against
      // drainGenRef.current and bails if a newer drain superseded it — so overlapping
      // runs (param debounce, cascade re-entry) can't clobber each other's state.
      const myGen = ++drainGenRef.current
      // Per-drain changed-vars baseline: the DAG runtime clock BEFORE this drain's
      // cells ran, captured into a LOCAL const (design §8.3).
      const baseline = dagBaselineNow()
      // Is this drain a reactive-cascade CONTINUATION? Yes iff the cell that started
      // it is one the cascade just enqueued. Otherwise it's a fresh user run → reset
      // the visited set so its dependents can run. Checking the actual enqueued cells
      // (not a predicted gen) is immune to timing/interleaving — the stale-visited bug.
      const pending = pendingCascadeCellsRef.current
      const isCascadeContinuation = !!pending && pending.has(String(cellId))
      if (!isCascadeContinuation) {
        cascadeVisitedRef.current = new Set()
      }
      // Consume the pending marker — a continuation drain has now claimed it.
      pendingCascadeCellsRef.current = null
      // Fresh run state (previous run's flags/tally must not leak).
      runInterruptedRef.current = false
      interruptedCellIdsRef.current = new Set()
      runResultsRef.current = new Map()
      // Burst sampling must run for the WHOLE execution even if the user switches
      // tabs (this Notebook unmounts). The React-effect poller is cleared on
      // unmount, so we ALSO poll from the drain — owned by this closure, bound to
      // THIS notebook's VM (onRefreshMetrics captures tab.microvmId). Without
      // this, a backgrounded still-running notebook's burst is never sampled and
      // its Cost Breakdown shows no Burst line. ~800ms cadence to catch spikes.
      let burstPoll = null
      if (onRefreshMetrics) {
        onRefreshMetrics()
        burstPoll = setInterval(() => onRefreshMetrics(), 800)
      }
      try {
        while (executionQueue.current.length > 0) {
          const next = executionQueue.current.shift()
          await next()
        }
      } finally {
        if (burstPoll) clearInterval(burstPoll)
        // One final poll to catch any post-last-cell burst tail.
        if (onRefreshMetrics) onRefreshMetrics()
        isDrainingRef.current = false
        setIsExecuting(false)
      }
      // Shared post-run routine: record the run, refresh vars, mark staleness, and
      // (reactive/app mode) auto-run dependents. Same for the per-cell drain and the
      // batch path (finishRunRef is set below so this closure can call it without a
      // declaration-order dependency).
      finishRunRef.current(runStartedAt, myGen, baseline)
      // Auto-tag: suggest a tag if enough cells have been executed and tag is still 'Drafts'
      if ((!tab.tag || tab.tag === 'Drafts') && !tagSuggestedRef.current) {
        tagSuggestedRef.current = true  // prevent re-fire during this attempt
        // Use a microtask to let state flush before checking outputs
        setTimeout(() => {
          const currentCells = prevCellsRef.current || []
          const executedCount = currentCells.filter(c => c.output || c.html || hasImageOutput(c) || c.error).length
          if (executedCount >= 2) {
            const cellData = currentCells.slice(0, 4).map(c => ({ type: c.type || 'code', code: (c.code || '').slice(0, 200) }))
            apiFetch(`/ai/suggest-tag`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: tab.name, description: tab.description || '', cells: cellData }),
            })
              .then(r => r.json())
              .then(data => { if (data.tag && data.tag !== 'Drafts') { onUpdateTab({ tag: data.tag }) } else { tagSuggestedRef.current = false } })
              .catch(() => { tagSuggestedRef.current = false })
          } else {
            tagSuggestedRef.current = false
          }
        }, 100)
      }
    }
  }, [tab.microvmEndpoint, tab.microvmId, tab.sessionId, tab.status, tab.tag, tab.name, tab.description, tab.id, isExecuting, patchCell, finalizeCell, recordRun, onRefreshMetrics, canRunNow, resolveEndpoint])

  // Shared post-run routine for BOTH the per-cell drain and the batch path: record
  // the run summary, refresh /variables, mark dataflow-stale downstream cells, and
  // (reactive/app mode) auto-run dependents. Extracted so /execute-batch reuses the
  // exact same staleness + cascade behavior as the sequential drain.
  const finishRun = useCallback((runStartedAt, myGen, baseline) => {
    const trigger = runTriggerRef.current || 'single'
    recordRun(runStartedAt, runResultsRef.current, trigger)
    const ranCellIds = new Set([...runCellIdsRef.current].map(String))
    // Outline run banner (services/runBanner.js) for every run except Run All
    // (executeAllCells writes that one). A reactive cascade that continues a run is
    // summarised together with it: the banner is written once the chain ends.
    const prevChain = runBannerChainRef.current
    runBannerChainRef.current = null
    const chain = trigger === 'reactive' && prevChain && prevChain.fromGen === myGen - 1 ? prevChain : null
    const bannerRun = {
      trigger: chain ? chain.trigger : trigger,
      startedAt: chain ? chain.startedAt : runStartedAt,
      results: new Map([...(chain ? chain.results : []), ...runResultsRef.current]),
      ranIds: new Set([...(chain ? chain.ranIds : []), ...runCellIdsRef.current]),
      finishedAt: Date.now(),
    }
    const pipResults = runResultsRef.current
    runResultsRef.current = new Map()
    runCellIdsRef.current = new Set()
    runTriggerRef.current = 'single'
    // Variable VALUES only — provenance and staleness come from the stored DAG.
    fetchVariables()
    // Every delta of this run applied (or the one resync fetch done) before the
    // reactive decision reads latestDagRef — never a document known to be behind.
    // Resolves true when a reactive cascade was started.
    const docReady = dagSyncRef.current.settled()
    // Packages banner for pip cells in this run, once their outputs are in state.
    Promise.resolve(docReady).catch(() => {}).then(() => publishPipCellBanner(pipResults, prevCellsRef.current || []))
    docReady.then(() => {
      if (drainGenRef.current !== myGen) return false
      const doc = latestDagRef.current
      if (!doc || !doc.runtime) return false
      const changed = changedVarsSinceBaseline(doc.runtime, baseline)
      if (changed.size === 0) return false
      if (!(reactiveRef.current || appModeRef.current)) return false
      const cellsNow = prevCellsRef.current || []
      const graph = graphFromDag(doc)
      const stale = staleNodes(doc)
      const cyclic = findCycleCells(graph.downstream)
      const visited = cascadeVisitedRef.current
      for (const id of ranCellIds) visited.add(id)
      const affected = [...collectDownstream(ranCellIds, graph.downstream)].filter(id => stale.has(id))
      const toRunCells = orderByPosition(affected, cellsNow)
        .map(id => cellsNow.find(c => String(c.id) === id))
        .filter(cell => {
          if (!cell) return false
          if (cyclic.has(String(cell.id))) return false
          if (visited.has(String(cell.id))) return false
          return cell.type !== 'markdown' && (cell.code || '').trim()
        })
      if (cyclic.size > 0) {
        window.dispatchEvent(new CustomEvent('reactive-cycle-warning', { detail: { cells: [...cyclic] } }))
      }
      if (toRunCells.length > 0 && visited.size < CASCADE_MAX_DEPTH) {
        toRunCells.forEach(c => visited.add(String(c.id)))
        runTriggerRef.current = 'reactive'
        // Cascade dependents run through the BATCH path (one request), preserving
        // the visited/pending markers so a continuation is recognized.
        pendingCascadeCellsRef.current = new Set(toRunCells.map(c => String(c.id)))
        if (bannerRun.trigger !== 'run_all') runBannerChainRef.current = { ...bannerRun, fromGen: myGen }
        executeBatchRef.current(toRunCells.map(c => c.id), 'reactive')
        return true
      }
      cascadeVisitedRef.current = new Set()
      pendingCascadeCellsRef.current = null
      return false
    }).catch(() => false).then((cascaded) => {
      if (cascaded || bannerRun.trigger === 'run_all') return
      // Skip when another run already started (it cleared the banner on start).
      if (drainGenRef.current !== myGen || isDrainingRef.current) return
      const banner = buildRunBanner({
        ...bannerRun, cells: prevCellsRef.current || [],
      })
      if (banner && onUpdateTabRef.current) onUpdateTabRef.current({ _runBanner: banner })
    })
    if (onRefreshMetrics) onRefreshMetrics()
  }, [recordRun, fetchVariables, onRefreshMetrics])
  finishRunRef.current = finishRun

  // Batch execution: run an ORDERED list of cells in ONE streaming request to the VM's
  // /execute-batch, consuming per-cell SSE frames as they arrive. Replaces N sequential
  // per-cell round-trips (the ~380ms/cell managed-endpoint latency) with one. Each
  // frame is mapped through finalizeCell EXACTLY as doExecute maps a single /execute
  // response, so UI/tally/recordRun/timeline are unchanged. `trigger` classifies the
  // run (run_all|run_from|reactive) for notebook_runs.
  const executeBatch = useCallback(async (cellIds, trigger = 'run_all') => {
    if (!tab.microvmId) return
    // Resolve cells in the given order; drop empty/markdown (server skips them too).
    const cellsNow = prevCellsRef.current || []
    const byId = new Map(cellsNow.map(c => [String(c.id), c]))
    const runCells = cellIds
      .map(id => byId.get(String(id)))
      .filter(c => c && (c.code || '').trim() && c.type !== 'markdown')
    if (runCells.length === 0) return

    // Data-source-ready gate (see canRunNow): one notice for the whole batch, and
    // the cells are left untouched.
    if (!canRunNow()) return
    // Endpoint for this run (recovered after a page refresh, same as executeCell).
    // Awaits only when the tab has no endpoint.
    let endpoint = tab.microvmEndpoint
    if (!endpoint) {
      endpoint = (await resolveEndpoint()).endpoint
      if (!endpoint) return
    }

    // One drain = one run: the batch IS the drain, so guard the same way (so a
    // concurrent single-cell run can't double-establish). If a drain is already
    // active, fall back to per-cell enqueue (rare; keeps correctness).
    if (isDrainingRef.current) {
      runCells.forEach(c => executeCell(c.id))
      return
    }
    isDrainingRef.current = true
    setIsExecuting(true)
    // Transient tab flag (never persisted): while a batch runs, the API autosave
    // holds run-driven saves and writes once when the batch ends (with a max-wait
    // safety save) — see useTabsPersistence. Cleared in the finally below.
    // Any run starting removes the Outline run banner (services/runBanner.js).
    if (onUpdateTabRef.current) onUpdateTabRef.current({ _batchRunning: true, ...clearBannerPatch() })
    runStartedAtRef.current = Date.now()
    const runStartedAt = runStartedAtRef.current
    const myGen = ++drainGenRef.current
    const baseline = dagBaselineNow()
    // Cascade continuation detection (mirror executeCell): if the first cell was one
    // the cascade enqueued, preserve the visited set; else reset for a fresh run.
    const pending = pendingCascadeCellsRef.current
    const isCascadeContinuation = !!pending && runCells.some(c => pending.has(String(c.id)))
    if (!isCascadeContinuation) cascadeVisitedRef.current = new Set()
    pendingCascadeCellsRef.current = null
    runInterruptedRef.current = false
    interruptedCellIdsRef.current = new Set()
    runResultsRef.current = new Map()
    runTriggerRef.current = trigger
    runCells.forEach(c => runCellIdsRef.current.add(c.id))

    // Progress label (N/total) next to the Run button — same UI as Run All, now for
    // ANY multi-cell batch (reactive cascade, run-from, run-selected). Run All seeds
    // runProgress BEFORE calling executeBatch, so only seed here when it isn't set,
    // and remember whether WE seeded it so only this call clears it (a cascade
    // continuation batch must not wipe an outer Run All's counter). Single-cell
    // batches don't get a label (nothing to count).
    let seededProgress = false
    setRunProgress(prev => {
      if (prev) return prev            // Run All (or an outer batch) already owns it
      if (runCells.length <= 1) return prev
      seededProgress = true
      return { current: 0, total: runCells.length }
    })

    // The VM runs the batch SEQUENTIALLY, so only ONE cell is actually executing
    // at a time. Reflect that in the UI: mark the FIRST cell running (its timer
    // ticks), and the rest as queued ('pending' → renders idle, NO ticking timer).
    // As each cell_result arrives we finalize that cell and flip the NEXT one to
    // running (below), so exactly one cell shows a live timer at any moment.
    const startStamp = Date.now()
    // executionTime/finishedAt are cleared too, so the previous run's time (and the
    // Outline tick that shows it) is gone until this run's result arrives.
    runCells.forEach((c, i) => patchCell(c.id, {
      status: i === 0 ? 'running' : 'pending',
      output: null,
      error: null,
      executionTime: null,
      finishedAt: null,
      startedAt: i === 0 ? startStamp : null,
    }))
    // Ordered id list so a finishing cell can promote its successor to running.
    const runOrderIds = runCells.map(c => String(c.id))

    // Burst metrics sampling for the whole run (same as the drain).
    let burstPoll = null
    if (onRefreshMetrics) { onRefreshMetrics(); burstPoll = setInterval(() => onRefreshMetrics(), 800) }

    const abort = new AbortController()
    batchAbortRef.current = abort
    // Per-cell client-clock start: stamped when a cell's frame is applied. We track
    // the previous frame's arrival so each cell's span starts where the last ended
    // (the batch shares one round-trip, so per-cell overhead ≈ 0 — exec_ms is the
    // real per-cell cost and comes from the VM).
    let prevArrival = Date.now()
    // Whether the stream carried the `{type:"dag"}` frame (absent → resync after).
    let sawDagFrame = false

    const headers = authHeaders({ 'Content-Type': 'application/json' })
    if (tab.sessionId) headers['X-Session-Id'] = tab.sessionId
    const batchBody = JSON.stringify({
      cells: runCells.map(c => ({
        cell_id: String(c.id),
        type: c.type === 'sql' ? 'sql' : 'code',
        code: c.code,
        output_variable: c.type === 'sql' ? (c.outputVariable || _deriveSqlVarName(c.code)) : undefined,
      })),
    })
    const postBatch = () => fetch(`${endpoint}/execute-batch`, {
      method: 'POST', headers, signal: abort.signal, body: batchBody,
    })
    try {
      let resp = await postBatch()
      // HTTP error BEFORE any stream (VM resuming 502, reactive-recovery 503, 401,
      // 400 no session, 404 session gone): the body is a JSON/HTML error, not SSE.
      // Same retry policy as executeCell (one retry after a retryable recovery 503);
      // if it still fails, every cell of this batch gets the server's message and
      // the run ends (tally, Run History, spinners) as a failed run below.
      if (!resp.ok) {
        let failure = await readErrorResponse(resp)
        if (shouldRetryRecovery(failure.status, failure.json, false)) {
          patchCell(runCells[0].id, { status: 'running', output: RECONNECTING_OUTPUT, error: null })
          await new Promise(r => {
            const t = setTimeout(r, RECOVERY_RETRY_DELAY_MS)
            abort.signal.addEventListener('abort', () => { clearTimeout(t); r() }, { once: true })
          })
          resp = await postBatch()  // throws AbortError if the user stopped the run
          if (resp.ok) patchCell(runCells[0].id, { output: null })
          else failure = await readErrorResponse(resp)
        }
        if (!resp.ok) {
          const error = batchFailureError(trigger, failure)
          const now = Date.now()
          for (const c of runCells) {
            if (!runResultsRef.current.has(c.id) && !interruptedCellIdsRef.current.has(c.id)) {
              finalizeCell(c.id, { status: 'error', error, startedAt: null, finishedAt: now, clientStartedAt: prevArrival, clientFinishedAt: now })
            }
          }
        }
      }
      // No reader on an HTTP error: the cells are already finalized above.
      const reader = resp.ok ? resp.body.getReader() : null
      const decoder = new TextDecoder()
      let buffer = ''
      while (reader) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          let frame
          try { frame = JSON.parse(line.slice(6)) } catch { continue }
          if (frame.type === 'cell_result') {
            const cid = frame.cell_id
            const cell = byId.get(String(cid))
            const result = frame.result || {}
            const now = Date.now()
            // Map the per-cell result EXACTLY like doExecute's finalizeCell.
            finalizeCell(cid, {
              status: result.success ? 'success' : 'error',
              output: result.output || null,
              error: result.error || null,
              errorLineno: typeof result.error_lineno === 'number' ? result.error_lineno : null,
              errorTraceback: result.traceback || null,
              errorFrames: Array.isArray(result.error_frames) ? result.error_frames : [],
              errorLocals: result.error_locals && typeof result.error_locals === 'object' ? result.error_locals : {},
              ...outputFieldsFromResult(result),
              executionNumber: result.execution_number,
              executionTime: result.execution_time_ms,
              lastExecutedCode: cell ? cell.code : undefined,
              resultRows: typeof result.row_count === 'number' ? result.row_count : null,
              resultCols: typeof result.column_count === 'number' ? result.column_count : null,
              resultBytes: typeof result.size_bytes === 'number' ? result.size_bytes : null,
              peakMemMb: typeof result.peak_mem_mb === 'number' ? result.peak_mem_mb : null,
              peakCpuPct: typeof result.peak_cpu_pct === 'number' ? result.peak_cpu_pct : null,
              executedAt: new Date(now).toISOString(),
              engine: result.engine || null,
              startedAt: null,
              finishedAt: now,
              clientStartedAt: prevArrival,
              clientFinishedAt: now,
            }, { variablesRead: result.variables_read })
            prevArrival = now
            // Advance the Run All toolbar counter (N/total) as each cell's result
            // arrives. Only active during a Run All (setRunProgress was seeded with
            // a total); for reactive/run-from batches runProgress is null and the
            // updater is a no-op, so the label stays hidden.
            setRunProgress(prev => prev ? { current: Math.min(prev.current + 1, prev.total), total: prev.total } : prev)
            // Sequential UI: promote the NEXT still-queued cell to running (its timer
            // starts now) so exactly one cell shows a live timer. The VM executes the
            // batch in order, so the successor of the cell that just finished is the
            // one now running on the VM.
            const finishedPos = runOrderIds.indexOf(String(cid))
            if (finishedPos >= 0 && finishedPos + 1 < runOrderIds.length) {
              const nextId = runOrderIds[finishedPos + 1]
              // Promote the successor to 'running' ONLY if it's still queued
              // ('pending'). This single guard is both correct AND safe: a successor
              // whose own result frame already arrived (coalesced/out-of-order) is
              // 'success'/'error', not 'pending', so we never re-mark a finished cell
              // running (the "stuck running with output" bug) — and we never block a
              // legitimately-queued cell from showing its spinner. We read
              // prevCellsRef for the freshest status; a missing entry defaults to
              // promote (first render before the store settles).
              const nextCell = (prevCellsRef.current || []).find(c => String(c.id) === nextId)
              if (!nextCell || nextCell.status === 'pending') {
                patchCell(nextId, { status: 'running', startedAt: Date.now() })
              }
            }
            if (result.success && tab.microvmId && onMarkVmRunning) onMarkVmRunning(tab.microvmId)
          } else if (frame.type === 'dag') {
            // The proxy's capture of the whole batch (one RMW), sent right before
            // `done`: a dag_delta (or the full dag), applied in arrival order.
            sawDagFrame = !!(frame.dag || frame.dag_delta)
            await dagSyncRef.current.ingest(frame)
          } else if (frame.type === 'error') {
            // Stream broke mid-run (VM death / network). Mark any not-yet-finalized
            // cells as errored so nothing hangs on "running".
            for (const c of runCells) {
              if (!runResultsRef.current.has(c.id) && !interruptedCellIdsRef.current.has(c.id)) {
                finalizeCell(c.id, { status: 'error', error: `Run interrupted: ${frame.error || 'stream ended'}`, startedAt: null, finishedAt: Date.now(), clientStartedAt: prevArrival, clientFinishedAt: Date.now() })
              }
            }
          }
          // frame.type === 'done' → loop ends naturally when the stream closes.
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        // Connection failure: error any cell that never got a frame.
        for (const c of runCells) {
          if (!runResultsRef.current.has(c.id) && !interruptedCellIdsRef.current.has(c.id)) {
            finalizeCell(c.id, { status: 'error', error: `Connection error: ${err.message}`, startedAt: null, finishedAt: Date.now(), clientStartedAt: prevArrival, clientFinishedAt: Date.now() })
          }
        }
      }
    } finally {
      // LEFTOVER SWEEP — the definitive net for "one middle cell stuck spinning".
      // A cell only reaches a terminal status when its matching cell_result frame is
      // applied. The `error`-frame and `catch` nets above only fire on an explicit
      // error frame or a thrown fetch — NOT on a CLEAN stream close (`done`) that
      // simply skipped/dropped one cell's frame. Such a cell is left at
      // 'running'/'pending' with NO finishedAt, so neither the finishedAt+running
      // self-heal nor the store-terminal reconcile can rescue it → it spins forever.
      // This runs on EVERY exit path (clean done, error frame, throw) after all
      // frames have been consumed: any run cell that never landed a result (and
      // wasn't user-interrupted) is finalized as an error so nothing hangs. A cell
      // whose frame DID arrive is in runResultsRef and is skipped, so this never
      // clobbers a real result. Skipped entirely on user abort — those cells are
      // finalized as 'interrupted' by the interrupt path, not errored here.
      if (!(batchAbortRef.current && batchAbortRef.current.signal.aborted)) {
        for (const c of runCells) {
          if (!runResultsRef.current.has(c.id) && !interruptedCellIdsRef.current.has(c.id)) {
            finalizeCell(c.id, {
              status: 'error',
              error: 'No result was returned for this cell (the run stream ended before it reported). Re-run this cell.',
              startedAt: null,
              finishedAt: Date.now(),
              clientStartedAt: prevArrival,
              clientFinishedAt: Date.now(),
            })
          }
        }
      }
      if (burstPoll) clearInterval(burstPoll)
      if (onRefreshMetrics) onRefreshMetrics()
      batchAbortRef.current = null
      isDrainingRef.current = false
      setIsExecuting(false)
      if (onUpdateTabRef.current) onUpdateTabRef.current({ _batchRunning: false })
      // Clear the progress label only if THIS batch seeded it (don't wipe an outer
      // Run All's counter from a nested cascade continuation).
      if (seededProgress) setRunProgress(null)
    }
    if (!sawDagFrame) dagSyncRef.current.requestResync()
    // Post-run: record + staleness + cascade (shared with the per-cell drain).
    finishRun(runStartedAt, myGen, baseline)
  }, [tab.microvmId, tab.microvmEndpoint, tab.sessionId, patchCell, finalizeCell, finishRun, executeCell, onRefreshMetrics, onMarkVmRunning, canRunNow, resolveEndpoint])
  executeBatchRef.current = executeBatch

  const executeAllCells = useCallback(async () => {
    if (!tab.microvmId) return
    // Gate before the Outline "running" pulse / progress / tint are set, so a
    // blocked Run All changes nothing (executeBatch would block it too).
    if (!canRunNow()) return
    const runnableCells = cells.filter(c => c.code.trim() && c.type !== 'markdown')
    const total = runnableCells.length

    // Mark this drain as a Run All for the notebook_runs summary. Set before the
    // first executeCell so the queue-drain (which posts the summary) sees it.
    runTriggerRef.current = 'run_all'

    // Signal the Outline activity-bar icon: open the panel + pulse green while running.
    window.dispatchEvent(new CustomEvent('outline-run-status', { detail: 'running' }))
    // Turn the execution-time tint off for this notebook while it runs; the
    // completion handler below turns it back on (tintAfterRunAll). The previous
    // run's ticks and times are cleared by executeBatch's per-cell reset.
    onUpdateTab({ ...tintBeforeRunAll(), ...clearBannerPatch() })
    const runAllStartedAt = Date.now()

    // Run the whole notebook in ONE streaming request (/execute-batch) instead of N
    // sequential per-cell round-trips — collapses ~380ms/cell of managed-endpoint
    // latency into a single round-trip while keeping live per-cell results via SSE.
    const ranIds = runnableCells.map(c => c.id)
    // Start at 0/total; executeBatch bumps `current` as each cell_result arrives.
    setRunProgress({ current: 0, total })
    await executeBatch(ranIds, 'run_all')
    setRunProgress(null)

    // FIRST Run All on this notebook → kick off Workbook Intel generation once.
    // The notebook now has real executed variables/data, so intel has something to
    // analyze. The in-memory `_firstRunAllDone` flag makes this once-per-session,
    // but it is NOT persisted — so a RESTORED/reopened notebook resets it. To avoid
    // needlessly regenerating an intel report that already exists on restore, we
    // additionally ask the backend whether a report is already present/in-flight and
    // only auto-generate when there's genuinely none yet. The manual Refresh button
    // (trigger:'manual', in useWorkbookIntel) is a SEPARATE path and always forces a
    // full regen — it is unaffected by this guard.
    if (tab.sessionId && !tab._firstRunAllDone) {
      const sid = tab.sessionId
      apiFetch(`/workbook-intel`, { headers: { 'X-Session-Id': sid } })
        .then(r => (r.ok ? r.json() : null))
        .then(d => {
          // Base-Intel rule: a Run All should generate the FULL report (Phase 1 +
          // Phase 2) whenever there is NO usable base Intel. "Usable base" means the
          // report is actually present ('ready') or already being built ('generating').
          // Everything else — 'not_generated' (new notebook OR a reopened one whose
          // artifacts were lost with the old VM, which the server now reports as
          // not_generated instead of a stale 'ready'), 'error', or an unreachable
          // status — has no base, so we (re)generate. The server routes a
          // 'first_run_all' trigger to a full generation. The manual Refresh button is
          // a separate path that always forces a full regen regardless of this guard.
          const alreadyHasReport = d && (d.status === 'ready' || d.status === 'generating')
          if (!alreadyHasReport) {
            // Tell the intel panel to (re)enter its poll loop (event-driven polling).
            try { window.dispatchEvent(new CustomEvent('intel-generation-started', { detail: { sessionId: sid } })) } catch {}
            apiFetch(`/workbook-intel/generate`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'X-Session-Id': sid },
              body: JSON.stringify({ trigger: 'first_run_all' }),
            }).catch(() => {})
          }
          // Only "consume" the once-per-session flag once we KNOW the outcome: a
          // report exists/is generating, OR we just kicked one off. We set it AFTER
          // the check (not optimistically before) so that if the very first Run All
          // this session found no base Intel, we don't burn the flag without acting —
          // a common failure was: an earlier Run All set _firstRunAllDone=true before
          // the async check, so the NEXT Run All skipped the whole block even though
          // no intel was ever generated. Now the flag is set only on a definitive
          // "has base or generation started", so a genuine no-report state keeps
          // retrying on subsequent Run Alls until intel actually exists.
          onUpdateTab({ _firstRunAllDone: true })
        })
        .catch(() => {
          // Status check failed (e.g. transient network) — leave _firstRunAllDone
          // unset so the next Run All retries the base-Intel check.
        })
    }

    // After the run, check the latest cell statuses for any error. Let React flush the
    // last setCells into prevCellsRef first (short yield), then read the freshest
    // snapshot so we see the statuses set during the run, not this closure's stale `cells`.
    setTimeout(() => {
      const latest = prevCellsRef.current || cells
      const firstError = latest.find(c => ranIds.includes(c.id) && c.status === 'error')
      // Red persists so the user can spot the failed cell in the Outline; green clears.
      window.dispatchEvent(new CustomEvent('outline-run-status', { detail: firstError ? 'error' : 'clear' }))
      // ALSO write the completion to THIS notebook's tab: the stamp plus the
      // execution-time tint flag (services/execTint.js). The Outline panel is
      // conditionally mounted and can be showing another notebook when the run ends,
      // so the one-shot event above is not enough. The tab state survives that, and
      // onUpdateTab is bound to this notebook's id, so only this notebook is tinted.
      // Set on every completion (clean OR error).
      const finishedAt = Date.now()
      onUpdateTab(tintAfterRunAll(finishedAt))
      // Run banner for the Outline (services/runBanner.js): stored on this tab, so it
      // is per notebook. Skipped if another run already started in the meantime.
      if (!isDrainingRef.current) {
        const banner = buildBanner({ cells: latest, ranIds, startedAt: runAllStartedAt, finishedAt })
        if (banner) onUpdateTab({ _runBanner: banner })
      }
      // Scroll the first failed cell into view so the user lands on the problem
      // after a Run All (instead of hunting for it). Only on error; a clean run
      // leaves scroll position alone.
      if (firstError) {
        const el = document.querySelector(`[data-cell-id="${firstError.id}"]`)
        // Land at the cell TOP (below the sticky toolbar via scroll-margin-top) so
        // the failed cell + its error are read from the start, not mid-code.
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    }, 150)
  }, [cells, tab.microvmEndpoint, tab.status, tab.sessionId, tab._firstRunAllDone, executeBatch, onUpdateTab, canRunNow])

  // Listen for "Run from cell" events from the outline panel — one batch request.
  useEffect(() => {
    const handleRunFrom = (e) => {
      const { cellIdx } = e.detail || {}
      if (cellIdx == null || !tab.microvmId) return
      const ids = cells.slice(cellIdx).filter(c => c.code.trim() && c.type !== 'markdown').map(c => c.id)
      executeBatch(ids, 'run_from')
    }
    window.addEventListener('notebook-run-from-cell', handleRunFrom)
    return () => window.removeEventListener('notebook-run-from-cell', handleRunFrom)
  }, [cells, tab.microvmId, executeBatch])

  // Listen for "Run selected cells" events from the outline panel — one batch request.
  useEffect(() => {
    const handleRunCells = (e) => {
      const { cellIndices } = e.detail || {}
      if (!cellIndices || !tab.microvmId) return
      const ids = cellIndices
        .map(idx => cells[idx])
        .filter(c => c && c.code.trim() && c.type !== 'markdown')
        .map(c => c.id)
      executeBatch(ids, 'run_from')
    }
    window.addEventListener('notebook-run-cells', handleRunCells)
    return () => window.removeEventListener('notebook-run-cells', handleRunCells)
  }, [cells, tab.microvmId, executeBatch])

  // Clear a SINGLE cell's output. Must write BOTH local state AND the tab store
  // (tab._cells), because the store-reconciliation effect (STEP 1 of the persist
  // effect) treats "local has no output but the store cell is terminal-with-output"
  // as a stale local copy and RE-ADOPTS the store's old output — which would
  // instantly un-clear the cell. Routing through patchCell (local + onPatchCell
  // store write) keeps the two in sync so there's nothing stale to re-adopt. We
  // also advance prevCellsRef so the persist effect sees no local→store delta.
  const clearCellOutput = useCallback((cellId) => {
    const cleared = { output: null, error: null, html: null, image: null, imageS3Key: null, executionNumber: null, executionTime: null, status: 'idle' }
    patchCell(cellId, cleared)
    prevCellsRef.current = (prevCellsRef.current || []).map(c =>
      c.id === cellId ? { ...c, ...cleared } : c)
  }, [patchCell])

  const clearAllOutputs = useCallback(() => {
    // Clear from the current baseline (prevCellsRef mirrors the latest committed
    // cells) and push the SAME array to local, the store, and the persist baseline
    // in one shot. Writing the store (onUpdateTab) is what prevents the reconcile
    // effect from re-adopting the pre-clear outputs (same latent bug as per-cell
    // clear); advancing prevCellsRef keeps the persist effect from seeing a delta.
    const next = (prevCellsRef.current || []).map(c => c.type === 'markdown' ? c : {
      ...c, output: null, error: null, html: null, image: null, imageS3Key: null, executionNumber: null, executionTime: null, status: 'idle'
    })
    prevCellsRef.current = next
    setCells(next)
    onUpdateTab({ _cells: next })
  }, [onUpdateTab])

  // ─── Inline lint + format (ruff) ──────────────────────────────────────────
  // Both call the VM directly (raw fetch to tab.microvmEndpoint + X-Session-Id),
  // matching every other data call in this hook. Lint sends the WHOLE notebook so
  // ruff resolves names across cells; the VM remaps diagnostics back per cell and
  // we store them on each cell as `cell.lint` (an array), which Cell.jsx threads
  // into CellEditor as squiggles — gated the same way as the error line (cleared
  // once the code is edited). Format runs each cell independently and rewrites the
  // changed cells' code in place.
  const [isLinting, setIsLinting] = useState(false)
  const [isFormatting, setIsFormatting] = useState(false)
  // Ids of cells with an in-flight single-cell format (per-cell button spinner).
  // A Set so multiple cells can format concurrently without clobbering each other.
  const [formattingCellIds, setFormattingCellIds] = useState(() => new Set())

  const _vmHeaders = useCallback(() => {
    const headers = authHeaders({ 'Content-Type': 'application/json' })
    if (tab.sessionId) headers['X-Session-Id'] = tab.sessionId
    return headers
  }, [tab.sessionId])

  const lintNotebook = useCallback(async () => {
    if (!tab.microvmEndpoint || tab.status !== 'connected') return
    // Lint both Python and SQL cells. The backend routes each by type — Python
    // cells through ruff (whole-notebook), SQL cells through sqlglot (per-cell
    // syntax check). Markdown is excluded. Snapshot code alongside each cell id so
    // we can gate stale diagnostics after the async round-trip.
    const cellsNow = prevCellsRef.current || []
    const lintable = cellsNow.filter(c => (c.type === 'code' || c.type === 'sql') && (c.code || '').trim())
    if (lintable.length === 0) {
      // Nothing to lint → clear any existing markers so the gutter doesn't go stale.
      setCells(prev => prev.map(c => (c.lint && c.lint.length ? { ...c, lint: [] } : c)))
      return
    }
    setIsLinting(true)
    try {
      const resp = await fetch(`${tab.microvmEndpoint}/lint`, {
        method: 'POST',
        headers: _vmHeaders(),
        body: JSON.stringify({ cells: lintable.map(c => ({ id: c.id, code: c.code, type: c.type })) }),
      })
      if (!resp.ok) return
      const data = await resp.json()
      if (!data.success) return
      const diags = data.diagnostics || {}
      // Snapshot the code we linted so we can attach a `lintCode` marker per cell;
      // Cell.jsx compares it to the live code to drop markers once the user edits.
      const lintedCode = new Map(lintable.map(c => [String(c.id), c.code]))
      setCells(prev => prev.map(c => {
        if (!lintedCode.has(String(c.id))) return c
        const cellDiags = diags[c.id] || diags[String(c.id)] || []
        return { ...c, lint: cellDiags, lintCode: lintedCode.get(String(c.id)) }
      }))
    } catch {
      // network/VM error — leave existing markers as-is (best effort)
    } finally {
      setIsLinting(false)
    }
  }, [tab.microvmEndpoint, tab.status, _vmHeaders])

  // Briefly flash a cell as an ACK that an action ran, even when it changed nothing
  // (e.g. Format on an already-formatted cell — previously a silent no-op that made
  // the button look broken). Uses the same flashCellIds state the insert highlight
  // uses (so it survives re-renders), but a SEPARATE short timer so it neither cancels
  // nor is cancelled by a 3s insert flash. ADDITIVE to the set so flashing one cell
  // doesn't clear another's highlight. Defined BEFORE the format callbacks that use it.
  const ackFlashTimersRef = useRef(new Map())  // cellId → timeout handle
  const flashCellBriefly = useCallback((cellId) => {
    const key = String(cellId)
    setFlashCellIds(prev => { const n = new Set(prev); n.add(key); return n })
    const timers = ackFlashTimersRef.current
    if (timers.has(key)) clearTimeout(timers.get(key))
    timers.set(key, setTimeout(() => {
      setFlashCellIds(prev => { const n = new Set(prev); n.delete(key); return n })
      timers.delete(key)
    }, ACK_FLASH_MS))
  }, [])

  // Clear any pending ack-flash timers on unmount so they don't fire a setState into
  // a torn-down component (the Map is keyed per cell; clear them all).
  useEffect(() => {
    const timers = ackFlashTimersRef.current
    return () => { timers.forEach(clearTimeout); timers.clear() }
  }, [])

  // Format runs on the PROXY (POST /format), NOT the VM: it's a pure text transform
  // that needs no kernel, so it works even when the VM is suspended/terminated — no
  // connected-guard, no session header. (LINT stays VM-side; it's namespace-aware.)
  const formatNotebook = useCallback(async () => {
    const cellsNow = prevCellsRef.current || []
    // Format both Python and SQL cells; the proxy routes by type (ruff / sqlglot).
    const formattable = cellsNow.filter(c => (c.type === 'code' || c.type === 'sql') && (c.code || '').trim())
    if (formattable.length === 0) return
    setIsFormatting(true)
    try {
      const resp = await apiFetch(`/format`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cells: formattable.map(c => ({ id: c.id, code: c.code, type: c.type })) }),
      })
      if (!resp.ok) return
      const data = await resp.json()
      if (!data.success) return
      const formatted = data.formatted || {}
      const changed = data.changed || {}
      // Rewrite only the cells the formatter actually changed. Route through
      // setCells so the editor's external-change sync picks up the new text.
      const changedIds = []
      setCells(prev => prev.map(c => {
        const key = changed[c.id] !== undefined ? c.id : String(c.id)
        if (changed[key]) {
          const next = formatted[key]
          if (typeof next === 'string' && next !== c.code) {
            changedIds.push(c.id)
            return { ...c, code: next }
          }
        }
        return c
      }))
      // Feedback: flash the cells that actually changed. If NONE changed (the whole
      // notebook was already formatted), briefly flash every formattable cell so the
      // Format button isn't a silent no-op (same reasoning as the per-cell path).
      const toFlash = changedIds.length > 0 ? changedIds : formattable.map(c => c.id)
      toFlash.forEach(flashCellBriefly)
    } catch {
      // network error — no-op (cells stay as they were)
    } finally {
      setIsFormatting(false)
    }
  }, [flashCellBriefly])

  // Format a SINGLE cell (the per-cell toolbar button). Same /format endpoint as
  // formatNotebook — formatting is per-cell anyway, so this just sends one cell —
  // but scoped so clicking format on one cell never rewrites the others. Tracks its
  // own in-flight id set so the button shows a spinner without gating the notebook.
  const formatCell = useCallback(async (cellId) => {
    const cell = (prevCellsRef.current || []).find(c => c.id === cellId)
    if (!cell || (cell.type !== 'code' && cell.type !== 'sql') || !(cell.code || '').trim()) return
    setFormattingCellIds(prev => { const n = new Set(prev); n.add(cellId); return n })
    try {
      const resp = await apiFetch(`/format`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cells: [{ id: cell.id, code: cell.code, type: cell.type }] }),
      })
      if (!resp.ok) return
      const data = await resp.json()
      if (!data.success) return
      const key = data.changed && data.changed[cell.id] !== undefined ? cell.id : String(cell.id)
      if (data.changed && data.changed[key]) {
        const next = (data.formatted || {})[key]
        // Guard against a stale write: only apply if the cell's code hasn't changed
        // since we sent it (the user could have kept typing during the round-trip).
        if (typeof next === 'string') {
          setCells(prev => prev.map(c =>
            (c.id === cellId && c.code === cell.code && next !== c.code) ? { ...c, code: next } : c))
        }
      }
      // ALWAYS acknowledge a successful format with a brief flash — including the
      // "already formatted, nothing changed" case. Formatting a clean cell (or a
      // Python cell whose only "unformatted" content is SQL inside a string literal,
      // which ruff can't touch) legitimately produces no diff; without this the
      // button looked dead. The flash tells the user it ran.
      flashCellBriefly(cellId)
    } catch {
      // network/VM error — leave the cell as-is
    } finally {
      setFormattingCellIds(prev => { const n = new Set(prev); n.delete(cellId); return n })
    }
  }, [flashCellBriefly])


  const interruptExecution = useCallback(async () => {
    if (!tab.microvmEndpoint || !tab.microvmId) return

    const headers = authHeaders({ 'Content-Type': 'application/json' })
    if (tab.sessionId) {
      headers['X-Session-Id'] = tab.sessionId
    }

    // Stop = stop everything: drop any cells still queued for this run so a
    // Run All / run-from halts instead of continuing after the current cell.
    executionQueue.current = []
    // Abort the in-flight batch stream (if any) so no further cell frames are applied.
    // The server still runs the current cell to completion, but the client stops
    // consuming and marks running cells interrupted below.
    if (batchAbortRef.current) { try { batchAbortRef.current.abort() } catch { /* noop */ } }
    runInterruptedRef.current = true
    // Tear down reactive-cascade state so a stopped cascade can't poison the next
    // run: bump the drain gen (any in-flight completion .then will see a newer gen
    // and bail, so it won't enqueue more reactive cells), clear the visited set, and
    // drop the pending continuation marker.
    drainGenRef.current++
    pendingCascadeCellsRef.current = null
    cascadeVisitedRef.current = new Set()

    try {
      await fetch(`${tab.microvmEndpoint}/interrupt`, {
        method: 'POST',
        headers,
      })
    } catch {}

    // Mark any running cell as interrupted — via patchCell so it's unmount-safe
    // and carries finishedAt (distinct 'interrupted' status, not a plain error).
    // Record the ids so finalizeCell ignores the late /execute response.
    const running = (prevCellsRef.current || []).filter(c => c.status === 'running')
    for (const c of running) {
      interruptedCellIdsRef.current.add(c.id)
      // Tally the interrupted cell so recordRun counts it (drain-owned ref). Its
      // client span runs from when it started running (startedAt, epoch ms) to now
      // (the moment the user interrupted) — so it still places on the timeline.
      runResultsRef.current.set(c.id, {
        status: 'interrupted',
        execMs: c.executionTime || 0,
        clientStartedAt: c.startedAt ?? null,
        clientFinishedAt: Date.now(),
      })
      patchCell(c.id, {
        status: 'interrupted',
        error: 'Execution interrupted by user',
        startedAt: null,
        finishedAt: Date.now(),
      })
    }
    // QUEUED cells of an interrupted batch run (status 'pending' — they were part
    // of this Run All but the VM never reached them). They didn't execute, so
    // reset them to idle rather than marking them interrupted-with-error. Guarding
    // finalizeCell against a late frame too.
    const queued = (prevCellsRef.current || []).filter(
      c => c.status === 'pending' && runCellIdsRef.current.has(c.id)
    )
    for (const c of queued) {
      interruptedCellIdsRef.current.add(c.id)
      patchCell(c.id, { status: 'idle', startedAt: null })
    }
  }, [tab.microvmEndpoint, tab.microvmId, tab.sessionId, patchCell])


  const updateCellCode = useCallback((cellId, code) => {
    setCells(prev => prev.map(c => c.id === cellId ? { ...c, code } : c))
  }, [])

  const updateCellOutputVar = useCallback((cellId, outputVariable) => {
    setCells(prev => prev.map(c => c.id === cellId ? { ...c, outputVariable } : c))
  }, [])

  const addCellBelow = useCallback((cellId, type = 'code') => {
    const newCell = createCell(type)
    setCells(prev => {
      const idx = prev.findIndex(c => c.id === cellId)
      if (idx === -1) return [...prev, newCell]  // fallback: add at end
      const next = [...prev]
      next.splice(idx + 1, 0, newCell)
      return next
    })
    setActiveCellId(newCell.id)
  }, [])

  const addCellAtEnd = useCallback((type = 'code') => {
    const newCell = createCell(type)
    setCells(prev => [...prev, newCell])
    setActiveCellId(newCell.id)
  }, [])


  // Drag-to-reorder handlers
  const handleDragStart = useCallback((cellId) => {
    draggedCellRef.current = cellId
  }, [])

  const handleDragOver = useCallback((cellId) => {
    if (draggedCellRef.current && draggedCellRef.current !== cellId) {
      setDragOverId(cellId)
    }
  }, [])

  const handleDrop = useCallback((targetCellId) => {
    const draggedId = draggedCellRef.current
    if (!draggedId || draggedId === targetCellId) {
      setDragOverId(null)
      draggedCellRef.current = null
      return
    }
    setCells(prev => {
      const draggedIdx = prev.findIndex(c => c.id === draggedId)
      const targetIdx = prev.findIndex(c => c.id === targetCellId)
      if (draggedIdx === -1 || targetIdx === -1) return prev
      const reordered = [...prev]
      const [moved] = reordered.splice(draggedIdx, 1)
      reordered.splice(targetIdx, 0, moved)
      return reordered
    })
    setDragOverId(null)
    draggedCellRef.current = null
  }, [])

  const handleDragEnd = useCallback(() => {
    setDragOverId(null)
    draggedCellRef.current = null
  }, [])

  const deleteCell = useCallback((cellId) => {
    setCells(prev => {
      if (prev.length <= 1) {
        // Last cell — replace with a fresh empty cell
        const fresh = createCell()
        setActiveCellId(fresh.id)
        return [fresh]
      }
      return prev.filter(c => c.id !== cellId)
    })
  }, [])

  // Ids of cells the stored DAG marks dataflow-stale (an upstream cell re-ran after
  // they last ran, transitively; includes not-in-checkpoint). Derived from the
  // document, so a code keystroke does not recompute it.
  const cellIdSig = cells.map(c => String(c.id)).join('|')
  const staleCellIds = useMemo(() => {
    const stale = staleNodes(dag)
    return cells.filter(c => stale.has(String(c.id))).map(c => c.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dag, cellIdSig])

  // Whether this notebook has any "reactive surface": at least one cross-cell
  // dependency edge in the stored DAG (static edges exist before anything runs).
  const hasReactiveSurface = !!dag && Array.isArray(dag.edges) && dag.edges.length > 0

  // Variables for the cell chips: values from /variables, provenance overlaid from
  // the DAG runtime section.
  const variablesWithProvenance = useMemo(() => withDagProvenance(variables, dag), [variables, dag])

  // Phase 2 reactive auto-run toggle (per notebook, persisted in tab state).
  const reactiveEnabled = !!tab._reactive
  const setReactiveEnabled = useCallback((on) => {
    onUpdateTab({ _reactive: !!on })
  }, [onUpdateTab])

  // Tier 1 action: re-run the dataflow-stale cells, in notebook order, through the
  // SAME sequential queue as everything else. This is an EXPLICIT user action (a
  // button) — NOT automatic reactive execution. The auto-run toggle is a later
  // slice; here we only offer one-click "catch these up".
  const runStaleDownstream = useCallback(() => {
    const stale = staleNodes(latestDagRef.current)
    const staleInOrder = (prevCellsRef.current || [])
      .filter(c => stale.has(String(c.id)) && c.type !== 'markdown' && (c.code || '').trim())
      .map(c => c.id)
    if (staleInOrder.length === 0) return
    executeBatch(staleInOrder, 'reactive')  // one batch request for the dependents
  }, [executeBatch])

  return {
    cells,
    setCells,
    activeCellId,
    setActiveCellId,
    notifyActiveCell,
    dragOverId,
    variables: variablesWithProvenance,
    dag,
    isExecuting,
    runProgress,
    flashCellIds,
    staleCellIds,
    runStaleDownstream,
    reactiveEnabled,
    setReactiveEnabled,
    hasReactiveSurface,
    fetchVariables,
    executeCell,
    executeAllCells,
    interruptExecution,
    clearAllOutputs,
    clearCellOutput,
    lintNotebook,
    formatNotebook,
    formatCell,
    isLinting,
    isFormatting,
    formattingCellIds,
    updateCellCode,
    updateCellOutputVar,
    addCellBelow,
    addCellAtEnd,
    handleDragStart,
    handleDragOver,
    handleDrop,
    handleDragEnd,
    deleteCell,
  }
}
