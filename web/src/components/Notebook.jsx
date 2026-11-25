import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import Cell from './Cell'
import ConnectionPanel from './ConnectionPanel'
import ErrorBoundary from './ErrorBoundary'
import { IconSearch, IconChevronUp, IconChevronDown, IconChevronRight, IconX, IconPencil, IconSave } from './Icons'
import { apiFetch } from '../services/apiFetch'
import { isVmSuspended, isSessionVmAlive, DEFAULT_MEMORY_MIB } from '../constants'
import { buildNativeNotebook, buildNotebookHTML, buildNotebookMarkdown, buildIPYNB, buildAppHTML, buildAppMarkdown, downloadTextFile } from '../services/notebookExport'
import NotebookToolbar from './NotebookToolbar'
import AppView from './AppView'
import ExecutionHud from './ExecutionHud'
import { useNotebookSearch } from '../hooks/useNotebookSearch'
import { useNotebookCells, newCellId } from '../hooks/useNotebookCells'
import { cellBadgeMap } from '../utils/cellBadge'
import VersionHistoryDrawer from './VersionHistoryDrawer'
import LineageDrawer from './LineageDrawer'
import { buildLineage, cellIssueSummary, lineageTrustLevels } from '../services/lineage.js'
import { deletedProducerVars } from '../services/variableOrigin.js'
import { graphFromDag } from '../services/depGraph'
import { upstreamSig } from '../services/explain'
import { withPersistedImages } from '../services/cellImageApi'
import { parseNotebookFile } from '../services/notebookImport'
import { renderMermaidSvg } from '../services/markdown'
import { restoreProgressText } from '../services/restoreProgress'
import './Notebook.css'

// A markdown cell produced by Auto-document. Tagged generated:'annotate' so re-run
// refreshes them (and "Clear annotations" can remove them) without touching authored cells.
function makeGeneratedMarkdown(code) {
  return {
    id: newCellId(),
    type: 'markdown',
    code,
    output: null, error: null, html: null, image: null, imageS3Key: null,
    status: 'idle', executionNumber: null, executionTime: null, lastExecutedCode: null,
    aiExplanation: null,
    generated: 'annotate',
  }
}

export default function Notebook({ tab, instances = {}, vmMetrics = {}, onUpdateTab, onPatchCell, onMarkVmRunning, onNewNotebook, onCloseTab, onDeleteNotebook, onRequestDeleteCells, onRefreshMetrics, theme, onToggleTheme, aiAvailable = false, lineageCellId = null, lineageOpener = null, onLineageCellChange }) {
  // Show the "Connect to Sandbox" panel on mount whenever this notebook isn't
  // actively connected. We intentionally do NOT also require !tab.microvmId: a
  // notebook reopened from the DB carries a STALE microvmId (its old, now-terminated
  // VM), which isn't a live connection — so keying off microvmId would wrongly hide
  // the panel exactly when the user needs it (to Restore the saved session onto a
  // new VM). The ConnectionPanel itself surfaces "Restore Session" when a checkpoint
  // exists. `status === 'connecting'`/`'launching'` still suppress it (handled at
  // render) — 'launching' covers auto-restore-on-reopen, so the panel never flashes
  // before the automatic restore takes over.
  const [showConnection, setShowConnection] = useState(
    tab.status !== 'connected' && tab.status !== 'launching'
  )
  const [isAnnotating, setIsAnnotating] = useState(false)
  // Per-workbook view: 'notebook' (editor) or 'app' (consumer preview). Local state
  // is per-tab because Notebook is keyed by tab.id in App.jsx (remounts per tab).
  const [viewMode, setViewMode] = useState('notebook')
  // The Outline cell map (OutlineMap.jsx) observes .cells-container, which App view
  // unmounts. Tell it when a fresh container is mounted (App -> Notebook) so it
  // re-attaches its observers and re-measures.
  useEffect(() => {
    if (viewMode !== 'app') window.dispatchEvent(new CustomEvent('notebook-cells-container'))
  }, [viewMode])
  const [exportMenuPos, setExportMenuPos] = useState(null)
  // Version history drawer (save / browse / diff / restore / per-cell revert).
  const [showVersions, setShowVersions] = useState(false)

  // Notebook description bar: collapsible + inline editable. descCollapsed hides the
  // text down to just the header row; editingDesc swaps the text for a textarea.
  const [descCollapsed, setDescCollapsed] = useState(false)
  const [editingDesc, setEditingDesc] = useState(false)
  const [descDraft, setDescDraft] = useState('')
  const beginEditDesc = useCallback(() => {
    setDescDraft(tab.description || '')
    setDescCollapsed(false)   // can't edit what's collapsed
    setEditingDesc(true)
  }, [tab.description])
  const saveDesc = useCallback(() => {
    const next = descDraft.trim()
    // Persist via the same partial-merge path rename/connect use.
    if (next !== (tab.description || '')) onUpdateTab({ description: next })
    setEditingDesc(false)
  }, [descDraft, tab.description, onUpdateTab])
  const cancelEditDesc = useCallback(() => setEditingDesc(false), [])

  // Cell state + CRUD + drag-reorder + tab persistence + execution — one cohesive hook.
  const {
    cells, setCells, activeCellId, setActiveCellId, notifyActiveCell, dragOverId,
    variables, dag, isExecuting, runProgress, flashCellIds,
    staleCellIds, runStaleDownstream, reactiveEnabled, setReactiveEnabled, hasReactiveSurface,
    executeCell, executeAllCells, interruptExecution,
    clearAllOutputs, clearCellOutput,
    lintNotebook, formatNotebook, formatCell, isLinting, isFormatting, formattingCellIds,
    updateCellCode, updateCellOutputVar, addCellBelow, addCellAtEnd,
    handleDragStart, handleDragOver, handleDrop, handleDragEnd, deleteCell,
  } = useNotebookCells({ tab, instances, onUpdateTab, onPatchCell, onMarkVmRunning, onRefreshMetrics, viewMode })

  // --- "How was this computed?" lineage drawer ---------------------------------
  // lineageCellId = the output cell whose lineage is open (null = closed). It is
  // owned by App (shared with the Outline's Lineage buttons) and set through
  // onLineageCellChange. lineageHighlight = Notebook-tab "Highlight in notebook"
  // mode (outlines the upstream steps with their numbers, dims the rest). Closing
  // resets it.
  const [lineageHighlight, setLineageHighlight] = useState(false)
  const catalogEntries = useMemo(
    () => [...(tab._dataSources?._catalog?.entries || []), ...(tab._dataSources?.sources || [])],
    [tab._dataSources]
  )
  // Trust dots + lineage walk the STORED DAG (traversal only; design §8.1).
  const trustByCell = useMemo(
    () => lineageTrustLevels(cells, dag, catalogEntries),
    [cells, dag, catalogEntries]
  )
  const lineage = useMemo(
    () => (lineageCellId && cells.some(c => String(c.id) === lineageCellId)
      ? buildLineage(lineageCellId, cells, dag, catalogEntries)
      : null),
    [lineageCellId, cells, dag, catalogEntries]
  )
  // The element that opened the drawer is recorded by App (lineageOpener) when the
  // focus is set, so focus returns to the trigger on close.
  const openLineage = useCallback((id) => onLineageCellChange?.(String(id)), [onLineageCellChange])
  const closeLineage = useCallback(() => { onLineageCellChange?.(null); setLineageHighlight(false) }, [onLineageCellChange])
  // The root cell can vanish while the drawer is open (cell deleted / notebook
  // reloaded) — `lineage` becomes null and the drawer unmounts. Clear the focus
  // too, so highlight mode doesn't silently re-apply on the next open.
  useEffect(() => {
    if (lineageCellId && !lineage) { onLineageCellChange?.(null); setLineageHighlight(false) }
  }, [lineageCellId, lineage, onLineageCellChange])
  const toggleLineageHighlight = useCallback(() => setLineageHighlight(v => !v), [])
  const highlightActive = !!(lineage && lineageHighlight && viewMode === 'notebook')
  const stepNumberById = useMemo(() => {
    const m = new Map()
    if (highlightActive) lineage.steps.forEach((s, i) => m.set(String(s.cellId), i + 1))
    return m
  }, [highlightActive, lineage])

  // --- Scroll-position restoration across tab switches -------------------------
  // <Notebook key={tab.id}> is remounted whenever the active tab changes, so the
  // scroll container is destroyed and recreated on every switch — the browser
  // can't restore scroll for a node that no longer exists. We persist the scroll
  // offset onto the tab object (the same _field pattern used for _cells,
  // _activeCellIndex, etc.) and restore it once cells have rendered.
  const bodyRef = useRef(null)
  // Latch the scroll offset to restore for THIS mount. Read once so later saves
  // (which mutate tab._scrollTop via onUpdateTab) don't move the target.
  const pendingScrollRef = useRef(tab._scrollTop || 0)
  const scrollSaveTimer = useRef(null)

  // Persist scroll offset (debounced) as the user scrolls, and flush on unmount.
  const persistScroll = useCallback((top) => {
    if (scrollSaveTimer.current) clearTimeout(scrollSaveTimer.current)
    scrollSaveTimer.current = setTimeout(() => {
      onUpdateTab({ _scrollTop: top })
    }, 200)
  }, [onUpdateTab])

  const handleBodyScroll = useCallback((e) => {
    persistScroll(e.currentTarget.scrollTop)
  }, [persistScroll])

  // Restore the latched scroll offset after cells paint.
  //
  // A single (even double) rAF is NOT enough: cells render heavy content
  // asynchronously (CodeMirror editors, rendered markdown, outputs) that keeps
  // GROWING the scroll height for many frames after mount. If we set scrollTop
  // once too early, the container is still short, the browser CLAMPS our value to
  // the current (small) max, and later growth leaves us stranded at the wrong
  // place. So we run a short SETTLE LOOP: re-assert the target on each frame until
  // either we actually reach it (within tolerance) or the content height stops
  // changing — then stop. Only 'notebook' view has the scroll container.
  useEffect(() => {
    if (viewMode !== 'notebook') return
    const target = pendingScrollRef.current
    if (!target) return

    let raf = 0
    const startedAt = performance.now()
    let lastHeight = -1
    let stableFrames = 0

    const tick = () => {
      const el = bodyRef.current
      if (!el) return
      el.scrollTop = target
      const h = el.scrollHeight
      // Reached target (allowing for sub-pixel/clamp rounding)?
      const reached = Math.abs(el.scrollTop - target) <= 2
      // Content height stopped changing across consecutive frames?
      if (h === lastHeight) stableFrames += 1
      else { stableFrames = 0; lastHeight = h }
      const settled = reached && stableFrames >= 3
      const timedOut = performance.now() - startedAt > 1200
      if (settled || timedOut) return
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => { if (raf) cancelAnimationFrame(raf) }
    // Run once per mount (per tab, since key={tab.id}).
  }, [viewMode])

  // Flush the last known scroll offset synchronously when this effect is torn down
  // (unmount, or onUpdateTab changing identity, which App recreates on each render)
  // so a fast tab switch doesn't drop the pending debounced save.
  useEffect(() => {
    return () => {
      if (scrollSaveTimer.current) clearTimeout(scrollSaveTimer.current)
      if (bodyRef.current) onUpdateTab({ _scrollTop: bodyRef.current.scrollTop })
    }
  }, [onUpdateTab])

  // --- Per-keystroke render-cost control ---------------------------------------
  // A Cell is memo()'d, but it used to receive `notebookContext={cells}`, whose
  // identity changes on EVERY keystroke (updateCellCode maps the array), so the
  // memo never held and all cells re-rendered — and each Cell then recomputed
  // cellBadgeMap(cells) (O(N)), giving O(N²) work per keypress. We break that here:
  //
  //  • cellsRef + getNotebookContext: the full cells array (with code) is only
  //    needed lazily, by the AI generate/chat path in useCellAI — never during a
  //    plain render. We hand Cell a STABLE accessor that reads the latest cells
  //    from a ref, so cells no longer has to be a render-affecting prop.
  //  • cellBadgeById: the ONLY thing render needs from "all cells" is the PY-/SQL-/
  //    MD- badge map, which depends solely on each cell's id + type. We compute it
  //    ONCE here, memoized on a cheap id:type signature, and pass the finished Map
  //    down. A keystroke (code change, same ids/types) leaves the signature — and
  //    thus the Map's identity — unchanged, so memo()'d cells don't re-render.
  const cellsRef = useRef(cells)
  cellsRef.current = cells
  const getNotebookContext = useCallback(() => cellsRef.current, [])

  const cellBadgeSig = cells.map(c => `${c.id}:${c.type || 'code'}`).join('|')
  const cellBadgeById = useMemo(
    () => cellBadgeMap(cells),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cellBadgeSig]
  )
  // Stored-DAG views for the cells: each Cell gets ITS node (stable per document)
  // and its issue summary, so a new document re-renders only through these props.
  const dagGraph = useMemo(() => graphFromDag(dag), [dag])
  // "Explain this code": stable accessor (reads refs) for the upstream-code signature
  // used in the explanation cache key, so Cell's memo is not defeated.
  const dagGraphRef = useRef(dagGraph)
  dagGraphRef.current = dagGraph
  const getExplainUpstreamSig = useCallback(
    (cellId) => upstreamSig(dagGraphRef.current, cellsRef.current, cellId), [])
  // In-memory variables whose producer was a deleted cell: their "1!" missing-
  // producer note explains they won't survive a restart. Keyed on the id:type
  // signature (not cells), so a keystroke does not rebuild it.
  const deletedInMemory = useMemo(
    () => deletedProducerVars(variables, cellsRef.current, dag),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [variables, dag, cellBadgeSig]
  )
  const issueSummaryById = useMemo(() => {
    const m = new Map()
    for (const id of dagGraph.nodeById.keys()) {
      const summary = cellIssueSummary(dag, id, cellBadgeById, { deletedInMemory })
      if (summary) m.set(id, summary)
    }
    return m
  }, [dag, dagGraph, cellBadgeById, deletedInMemory])

  // --- Stable per-cell handler bundles -----------------------------------------
  // Cell is memo()'d, but the parent used to hand it a FRESH arrow for every
  // callback on every render (onExecute={() => executeCell(cell.id)}, etc.). New
  // function identities each render defeated the memo, so all cells re-rendered on
  // each keystroke. The underlying hook callbacks (executeCell/updateCellCode/…)
  // are already useCallback-stable and take a cellId, so here we build ONE handler
  // bundle per cell id and cache it — the same function identities are reused on
  // subsequent renders, letting memo() actually hold. Bundles for removed cells are
  // pruned so the cache can't grow unbounded across a long session.
  // The cache Map is tied (via useMemo) to the SAME deps as getCellHandlers, so if
  // any underlying callback identity ever changes the whole cache is discarded and
  // bundles are rebuilt with the current closures — no silent stale-closure bugs.
  // (Today all deps are stable useCallbacks, so this effectively never resets.)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const handlersById = useMemo(() => new Map(), [setActiveCellId, executeCell, updateCellCode, updateCellOutputVar, addCellBelow, setCells, deleteCell, clearCellOutput, formatCell, handleDragStart, handleDragOver, handleDrop, onRequestDeleteCells])
  const getCellHandlers = useCallback((cellId) => {
    const cache = handlersById
    const key = String(cellId)
    let h = cache.get(key)
    if (!h) {
      h = {
        onFocus: () => { setActiveCellId(cellId); notifyActiveCell(cellId) },
        onExecute: () => executeCell(cellId),
        onCodeChange: (code) => updateCellCode(cellId, code),
        onOutputVarChange: (v) => updateCellOutputVar(cellId, v),
        onAddBelow: (type) => addCellBelow(cellId, type),
        onSetAiExplanation: (explanation, edited = false, source = 'ai') => {
          setCells(prev => prev.map(c => String(c.id) === key
            ? {
                ...c,
                aiExplanation: explanation,
                aiExplanationEdited: explanation ? edited : false,
                aiExplanationSource: explanation ? source : null,
              }
            : c))
        },
        onDelete: () => {
          // A cell delete always goes through the confirmation. Without the wired
          // handler the delete is refused, never run directly.
          if (!onRequestDeleteCells) {
            if (import.meta.env.DEV) console.error('Notebook: onRequestDeleteCells is not wired, so the cell delete was ignored.')
            return
          }
          onRequestDeleteCells([cellId], { cells: cellsRef.current, run: () => deleteCell(cellId), focusFallbackSelector: '.cells-container' })
        },
        onClearOutput: () => clearCellOutput(cellId),
        onFormat: () => formatCell(cellId),
        onDragStart: () => handleDragStart(cellId),
        onDragOver: () => handleDragOver(cellId),
        onDrop: () => handleDrop(cellId),
      }
      cache.set(key, h)
    }
    return h
  }, [handlersById])

  // Prune cached handler bundles for cells that no longer exist (keeps the cache
  // bounded; runs only when the id SET changes, not on keystrokes).
  const cellIdSig = cells.map(c => c.id).join('|')
  useEffect(() => {
    const live = new Set(cells.map(c => String(c.id)))
    for (const key of handlersById.keys()) {
      if (!live.has(key)) handlersById.delete(key)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cellIdSig, handlersById])

  // Sync connection panel visibility when tab status changes.
  //
  // The auto-focus-first-cell convenience must fire ONLY on a genuine
  // disconnected→connected TRANSITION (user just connected/restored a VM), not
  // every time an already-connected tab mounts. Notebook is keyed by tab.id and
  // remounts on every tab switch, so without this guard, switching back to a
  // connected notebook would run the 800ms auto-focus and yank the editor (and
  // scroll) to the first code cell — clobbering the scroll position we just
  // restored. Seed the ref with the mount-time status so a mount that's already
  // 'connected' is NOT treated as a fresh connection.
  const prevStatusRef = useRef(tab.status)
  useEffect(() => {
    const justConnected = tab.status === 'connected' && prevStatusRef.current !== 'connected'
    prevStatusRef.current = tab.status
    if (tab.status === 'connected') {
      // Auto-dismiss connection panel shortly after connecting
      const timer = setTimeout(() => {
        setShowConnection(false)
        // Clear any accidental text selection caused by overlay removal
        window.getSelection()?.removeAllRanges()
        // Auto-focus first code cell — ONLY on a fresh connection, so a plain
        // tab re-switch keeps the restored scroll position instead of jumping.
        if (justConnected) {
          const firstCodeCell = cells.find(c => c.type !== 'markdown')
          if (firstCodeCell) {
            setActiveCellId(firstCodeCell.id)
            setTimeout(() => {
              const el = document.querySelector(`[data-cell-id="${firstCodeCell.id}"] .cm-editor .cm-content`)
              if (el) el.focus()
            }, 100)
          }
        }
      }, 800)
      return () => clearTimeout(timer)
    }
  }, [tab.status])

  // Track whether this tab was ever CONNECTED this session. We only want to pop
  // the reconnect panel when a LIVE VM dies out from under active work — NOT when
  // a notebook is merely opened/reopened pointing at a VM that's already gone
  // (e.g. reopening a closed notebook, or after the user deliberately closed one).
  const wasConnectedRef = useRef(false)
  useEffect(() => {
    if (tab.status === 'connected') wasConnectedRef.current = true
  }, [tab.status])

  // Auto-show connection panel when a VM we WERE connected to disappears from
  // instances (unexpected death mid-session → offer reconnect/restore). Gated on
  // wasConnectedRef so opening/reopening a notebook with a stale/terminated VM
  // does NOT nag with a restore modal — Close means closed.
  //
  // Do NOT pop the panel merely because the tab's stored microvmId isn't a key in
  // `instances`: the session may have rotated to a NEW vm id, or the tab holds an
  // old id while a perfectly-alive (often SUSPENDED) VM for the SAME session sits
  // in the map under a different key. Only nag when the session has NO alive VM at
  // all. isSessionVmAlive is a pure read of the already-polled instances map — it
  // sends nothing to the VM, so this check can never wake a suspended VM.
  useEffect(() => {
    if (
      wasConnectedRef.current &&
      tab.microvmId &&
      Object.keys(instances).length > 0 &&
      !instances[tab.microvmId] &&
      !isSessionVmAlive(instances, tab.sessionId)
    ) {
      setShowConnection(true)
    }
  }, [tab.microvmId, tab.sessionId, instances])
  // Find-in-notebook (Cmd+F) — search state, match computation, and navigation.
  const {
    showSearch, setShowSearch,
    searchQuery, setSearchQuery,
    searchMatches, setSearchMatches,
    searchActiveIdx,
    searchInputRef,
    searchNext, searchPrev,
    replaceQuery, setReplaceQuery,
    replaceCurrent, replaceAll,
    searchMatchCellIds, searchActiveOccurrenceMap,
  } = useNotebookSearch(cells, setActiveCellId, viewMode !== 'app', updateCellCode)

  const handleConnect = useCallback((endpoint) => {
    onUpdateTab({
      microvmEndpoint: endpoint,
      status: 'connected',
    })
    setShowConnection(false)
  }, [onUpdateTab])

  // Auto-document: ONE notebook-aware pass that (re)generates a root title + intro,
  // "## section" markdown cells between logical clusters, and a per-cell ✨ comment.
  // Idempotent: previously auto-generated markdown is dropped and prior AI comments
  // cleared before regenerating (generated cells are tagged generated:'annotate').
  const autoDocumentNotebook = useCallback(async () => {
    if (!aiAvailable) return
    setIsAnnotating(true)
    try {
      // Base = current notebook minus prior auto-generated markdown. Clear only
      // AI-generated comments; PRESERVE user-authored notes (source 'user') so
      // "generate AI notes" never wipes a note the user wrote by hand.
      const base = cells
        .filter(c => c.generated !== 'annotate')
        .map(c => (c.aiExplanation && c.aiExplanationSource !== 'user'
          ? { ...c, aiExplanation: null, aiExplanationEdited: false, aiExplanationSource: null }
          : c))

      const payloadCells = base.map((c, i) => ({
        index: i,
        type: c.type || 'code',
        code: (c.code || '').slice(0, 2000),
        output: ((c.output || '') + (c.html ? ' [table]' : '')).slice(0, 300),
      }))

      const resp = await apiFetch(`/ai/annotate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cells: payloadCells }),
      })
      if (!resp.ok) {
        console.warn('[annotate] request failed:', resp.status)
        return
      }
      const data = await resp.json()

      // Attach per-cell comments (keyed by base index), stamped as AI-authored.
      // A user note (source 'user') is left untouched — the AI comment is skipped
      // for that cell so we don't overwrite the user's own words.
      const commentByIdx = new Map((data.comments || []).map(c => [c.index, c.comment]))
      const withComments = base.map((c, i) =>
        (commentByIdx.has(i) && c.aiExplanationSource !== 'user')
          ? { ...c, aiExplanation: commentByIdx.get(i), aiExplanationEdited: false, aiExplanationSource: 'ai' }
          : c)

      // Group section markdown by the base index it should precede.
      const sectionsByBefore = new Map()
      for (const s of (data.sections || [])) {
        if (!sectionsByBefore.has(s.before_index)) sectionsByBefore.set(s.before_index, [])
        sectionsByBefore.get(s.before_index).push(s.markdown)
      }

      const authoredH1 = withComments[0]?.type === 'markdown' && /^\s*#\s+/.test(withComments[0].code || '')
      const out = []
      if (data.root && !authoredH1) out.push(makeGeneratedMarkdown(data.root))
      withComments.forEach((c, i) => {
        const prevIsMarkdown = i > 0 && withComments[i - 1].type === 'markdown'
        if (sectionsByBefore.has(i) && !prevIsMarkdown) {
          for (const md of sectionsByBefore.get(i)) out.push(makeGeneratedMarkdown(md))
        }
        out.push(c)
      })
      if (sectionsByBefore.has(withComments.length)) {
        for (const md of sectionsByBefore.get(withComments.length)) out.push(makeGeneratedMarkdown(md))
      }

      setCells(out)
    } catch (err) {
      console.warn('[annotate] failed:', err?.message)
    } finally {
      setIsAnnotating(false)
    }
  }, [cells, aiAvailable])

  // Remove AI documentation: generated markdown cells + AI per-cell comments.
  // User-authored notes (source 'user') are preserved — "Clear annotations" only
  // clears what the AI added.
  const clearAnnotations = useCallback(() => {
    setCells(prev => prev
      .filter(c => c.generated !== 'annotate')
      .map(c => (c.aiExplanation && c.aiExplanationSource !== 'user'
        ? { ...c, aiExplanation: null, aiExplanationEdited: false, aiExplanationSource: null }
        : c)))
  }, [])

  // "Has AI annotations" = generated markdown OR an AI-authored comment. User
  // notes don't count (Clear annotations leaves them alone).
  const hasAnnotations = cells.some(c => c.generated === 'annotate' || (c.aiExplanation && c.aiExplanationSource !== 'user'))

  // Download the editable native source (.notebook.json). This is a FILE export,
  // not "save" — the working copy is autosaved, and named restore points are
  // versions (saveVersion below). Lives under the Download menu now.
  // Download exports embed persisted-only charts (fetched from the proxy route).
  const downloadNative = useCallback(async () => {
    const out = await withPersistedImages(cells)
    downloadTextFile(buildNativeNotebook({ ...tab, _dag: dag || tab._dag || null }, out))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab.name, tab.description, tab.tag, tab.microvmId, cells, dag])

  // Cmd+S → open the Version history drawer (where saving/restoring live now).
  // The 3s autosave already persists the working copy continuously, so Cmd+S is
  // about capturing a named restore point, which is the drawer's job.
  useEffect(() => {
    const handler = () => setShowVersions(true)
    window.addEventListener('notebook-save', handler)
    return () => window.removeEventListener('notebook-save', handler)
  }, [])

  // In App view, export the consumer app (inputs + rendered content, no code);
  // in Notebook view, export the full workbook. The HTML follows the current theme,
  // renders mermaid to SVG and shows the lineage trust levels as static pills.
  const exportNotebookHTML = useCallback(async () => {
    const out = await withPersistedImages(cells)
    downloadTextFile(viewMode === 'app'
      ? await buildAppHTML(tab, out, { renderMermaid: renderMermaidSvg })
      : await buildNotebookHTML(tab, out, { renderMermaid: renderMermaidSvg, trustByCell }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab.name, tab.description, cells, viewMode, trustByCell])
  const exportNotebookMD = useCallback(async () => {
    const out = await withPersistedImages(cells)
    downloadTextFile(viewMode === 'app' ? buildAppMarkdown(tab, out) : buildNotebookMarkdown(tab, out))
  }, [tab.name, tab.description, cells, viewMode])
  const downloadIPYNB = useCallback(async () => {
    setExportMenuPos(null)
    const out = await withPersistedImages(cells)
    downloadTextFile(buildIPYNB({ ...tab, _dag: dag || tab._dag || null }, out))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab.name, tab.description, tab.tag, cells, dag])
  // Open a .notebook.json / .ipynb file in a new tab (services/notebookImport.js
  // is the one parser; App's open-notebook handler normalizes the cells).
  const loadNotebook = useCallback(() => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.json,.notebook.json,.ipynb'
    input.onchange = (e) => {
      const file = e.target.files?.[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = (ev) => {
        try {
          const detail = parseNotebookFile(JSON.parse(ev.target.result), file.name)
          window.dispatchEvent(new CustomEvent('open-notebook', { detail }))
        } catch {
          alert('Invalid notebook file. Supported formats: .notebook.json, .ipynb')
        }
      }
      reader.readAsText(file)
    }
    input.click()
  }, [onUpdateTab])
  // Derive whether the linked VM is alive — used for toolbar/cell disabled state.
  // Prefer the exact-id hit, but fall back to the SESSION's live VM so a rotated or
  // reopened tab (stale microvmId) whose VM is actually RUNNING/SUSPENDED still reads
  // as connected. Both branches are pure reads of the instances map — no VM request,
  // so a SUSPENDED VM is never woken. This only enables the Run controls; it does NOT
  // trigger any idle fetch (those stay gated on isVmSuspended in the Sidebar/hooks).
  const vmAlive = !!(tab.microvmId && instances[tab.microvmId]) || isSessionVmAlive(instances, tab.sessionId)

  // Effective "ready to Run" signal for the App view + cell run buttons.
  //
  // TWO-TIER GATE: Run is gated on the LIGHTWEIGHT source list (tab._sourcesReady),
  // NOT the full enriched catalog (tab._catalogLoaded). The SQL engine only needs
  // source type/identity to ROUTE a query (verified: _get_routing_catalogs reads
  // source_type/source_id/metadata, never columns); column schemas + entity docs
  // are for the Data panel + autocomplete, which don't block a Run. Gating on the
  // cheap signal lets Run enable seconds earlier — as soon as /datasources resolves
  // — instead of waiting on the VM cold-start round-trip + full schema discovery.
  //
  // A SUSPENDED VM intentionally skips its fetches, so both flags stay false; gating
  // on them alone would DEADLOCK a suspended VM (can't run → can't resume → never
  // loads). Running a cell auto-resumes the VM, so treat suspended as "ready".
  //
  // Local files (SQL over '/tmp/foo.csv') don't depend on this either — DuckDB reads
  // the file off disk directly; files are present via synchronous checkpoint-restore
  // in /run or a completed upload, independent of the background schema scan.
  const runReady = !!tab._sourcesReady || isVmSuspended(instances, tab.microvmId)

  // Per-type ordinal for each cell (PY-1, SQL-1, MD-2 …), keyed by cell id.
  // Computed by walking the cells in order — the SAME scheme the Outline panel
  // uses — so a cell's type badge matches its Outline row exactly.
  const typeOrdinals = useMemo(() => {
    const counters = { code: 0, sql: 0, markdown: 0 }
    const map = {}
    for (const c of cells) {
      const t = c.type || 'code'
      counters[t] = (counters[t] || 0) + 1
      map[c.id] = counters[t]
    }
    return map
  }, [cells])

  // Lineage "Open ↗": scroll to + flash a step's cell (same pattern as the
  // cell-revert jump in App.jsx). From the App tab, close the drawer and switch
  // to the Notebook tab first; drop the mount-time scroll restore so it doesn't
  // pull the container back while the jump is in flight.
  const openLineageStep = useCallback((cellId) => {
    const jump = (moveFocus = false) => {
      const el = document.querySelector(`[data-cell-id="${cellId}"]`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        el.classList.remove('cell-flash'); void el.offsetWidth; el.classList.add('cell-flash')
        // From the App tab the drawer's opener (an AppView trigger) is unmounted
        // by the view switch, so hand focus to the target cell's editor instead
        // of letting it fall to <body>. Focus the cell container (programmatic
        // tabindex=-1) rather than its editor: focusing CodeMirror would scroll
        // its cursor into view and re-render the cell, cancelling the jump/flash.
        if (moveFocus) {
          if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1')
          el.focus({ preventScroll: true })
        }
      }
      window.dispatchEvent(new CustomEvent('notebook-active-cell', { detail: { cellId } }))
    }
    if (viewMode === 'app') {
      pendingScrollRef.current = 0
      closeLineage()
      setViewMode('notebook')
      setTimeout(() => jump(true), 120)
    } else {
      jump()
    }
  }, [viewMode, closeLineage])

  return (
    <>
    <div className="notebook">
      {/* Restore pending: the Connect dialog has closed and the VM is reinstalling
          the checkpoint's user packages (services/restoreProgress.js). Same look
          as the dialog's launch line (.connection-launching). */}
      {restoreProgressText(tab) && (
        <div className="connection-launching restore-progress" role="status" aria-live="polite">
          <span className="cp-spinner" aria-hidden="true" />
          <span className="restore-progress-text">{restoreProgressText(tab)}</span>
        </div>
      )}
      {showConnection && tab.status !== 'connecting' && tab.status !== 'launching' && (
        <ErrorBoundary scope="connect">
        <ConnectionPanel
          tab={tab}
          onConnect={handleConnect}
          onUpdateTab={onUpdateTab}
          onDismiss={() => setShowConnection(false)}
        />
        </ErrorBoundary>
      )}
      {!showConnection && (
        <>
        <NotebookToolbar
          tab={tab}
          instances={instances}
          cells={cells}
          vmAlive={vmAlive}
          viewMode={viewMode}
          setViewMode={setViewMode}
          isExecuting={isExecuting}
          runProgress={runProgress}
          interruptExecution={interruptExecution}
          executeAllCells={executeAllCells}
          staleCount={staleCellIds.length}
          runStaleDownstream={runStaleDownstream}
          reactiveEnabled={reactiveEnabled}
          setReactiveEnabled={setReactiveEnabled}
          hasReactiveSurface={hasReactiveSurface}
          onNewNotebook={onNewNotebook}
          loadNotebook={loadNotebook}
          onOpenVersions={() => setShowVersions(true)}
          exportMenuPos={exportMenuPos}
          setExportMenuPos={setExportMenuPos}
          setShowSearch={setShowSearch}
          searchInputRef={searchInputRef}
          aiAvailable={aiAvailable}
          autoDocumentNotebook={autoDocumentNotebook}
          isAnnotating={isAnnotating}
          clearAnnotations={clearAnnotations}
          hasAnnotations={hasAnnotations}
          clearAllOutputs={clearAllOutputs}
          lintNotebook={lintNotebook}
          formatNotebook={formatNotebook}
          isLinting={isLinting}
          isFormatting={isFormatting}
          onCloseTab={onCloseTab}
          onDeleteTab={onDeleteNotebook ? () => onDeleteNotebook(tab.id, tab.name) : undefined}
          setShowConnection={setShowConnection}
          theme={theme}
          onToggleTheme={onToggleTheme}
        />
        {/* Notebook description — collapsible + inline editable. Shows whenever a
            description exists or the editor is open; a chevron collapses it to just
            the header row, and the pencil (or clicking the text) opens inline edit. */}
        {(tab.description || editingDesc) && (
          <div className={`notebook-identity ${descCollapsed && !editingDesc ? 'notebook-identity-collapsed' : ''}`}>
            <button
              className="notebook-desc-toggle"
              onClick={() => setDescCollapsed(c => !c)}
              disabled={editingDesc}
              title={descCollapsed ? 'Show description' : 'Hide description'}
              aria-expanded={!descCollapsed}
            >
              {descCollapsed && !editingDesc
                ? <IconChevronRight width={12} height={12} />
                : <IconChevronDown width={12} height={12} />}
            </button>

            {editingDesc ? (
              <div className="notebook-desc-edit">
                <textarea
                  className="notebook-desc-textarea"
                  value={descDraft}
                  onChange={(e) => setDescDraft(e.target.value)}
                  autoFocus
                  rows={Math.min(8, Math.max(2, descDraft.split('\n').length + 1))}
                  placeholder="Describe this notebook… (what it does, how to use it)"
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') { e.preventDefault(); cancelEditDesc() }
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveDesc() }
                  }}
                />
                <div className="notebook-desc-edit-actions">
                  <button className="notebook-desc-save" onClick={saveDesc} title="Save (⌘/Ctrl+Enter)">
                    <IconSave width={12} height={12} /> Save
                  </button>
                  <button className="notebook-desc-cancel" onClick={cancelEditDesc} title="Cancel (Esc)">Cancel</button>
                </div>
              </div>
            ) : (
              <>
                <span
                  className="notebook-description"
                  onClick={beginEditDesc}
                  title={descCollapsed ? tab.description : 'Click to edit description'}
                >
                  {tab.description}
                </span>
                <button
                  className="notebook-desc-edit-btn"
                  onClick={beginEditDesc}
                  title="Edit description"
                >
                  <IconPencil width={12} height={12} />
                </button>
              </>
            )}
          </div>
        )}
        </>
      )}

      {showSearch && viewMode !== 'app' && (
        <div className="notebook-search-bar">
          {/* Find group: input + count + prev/next */}
          <div className="notebook-search-field notebook-search-find">
            <IconSearch width={13} height={13} className="notebook-search-icon" />
            <input
              ref={searchInputRef}
              className="notebook-search-input"
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.shiftKey ? searchPrev() : searchNext() }
                if (e.key === 'Escape') { setShowSearch(false); setSearchQuery(''); setReplaceQuery(''); setSearchMatches([]) }
              }}
              placeholder="Find in notebook..."
              autoFocus
            />
            {searchQuery && (
              <span className="notebook-search-count">
                {searchMatches.length > 0
                  ? `${searchActiveIdx + 1} of ${searchMatches.length} in ${new Set(searchMatches.map(m => m.cellId)).size} cells`
                  : 'No results'}
              </span>
            )}
          </div>
          <div className="notebook-search-actions">
            <button className="notebook-search-nav" onClick={searchPrev} disabled={searchMatches.length === 0} title="Previous (Shift+Enter)">
              <IconChevronUp width={14} height={14} />
            </button>
            <button className="notebook-search-nav" onClick={searchNext} disabled={searchMatches.length === 0} title="Next (Enter)">
              <IconChevronDown width={14} height={14} />
            </button>
          </div>

          {/* Divider between the find and replace groups */}
          <span className="notebook-search-divider" aria-hidden="true" />

          {/* Replace group: input + Replace / All, kept next to the replace input */}
          <div className="notebook-search-field notebook-search-replace-field">
            <input
              className="notebook-search-input"
              type="text"
              value={replaceQuery}
              onChange={(e) => setReplaceQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); replaceCurrent() }
                if (e.key === 'Escape') { setShowSearch(false); setSearchQuery(''); setReplaceQuery(''); setSearchMatches([]) }
              }}
              placeholder="Replace with..."
            />
          </div>
          <div className="notebook-search-actions">
            <button className="notebook-search-replace-btn" onClick={replaceCurrent} disabled={searchMatches.length === 0} title="Replace (Enter)">
              Replace
            </button>
            <button className="notebook-search-replace-btn" onClick={replaceAll} disabled={searchMatches.length === 0} title="Replace all matches">
              All
            </button>
          </div>

          {/* Flexible spacer pushes Close to the far right of the whole bar */}
          <span className="notebook-search-spacer" />
          <button
            className="notebook-search-close"
            onClick={() => { setShowSearch(false); setSearchQuery(''); setReplaceQuery(''); setSearchMatches([]) }}
            title="Close (Esc)"
          >
            <IconX width={14} height={14} />
          </button>
        </div>
      )}

      {viewMode === 'app' ? (
        <AppView
          tab={tab}
          cells={cells}
          isConnected={vmAlive}
          isExecuting={isExecuting}
          catalogLoaded={runReady}
          onRunAll={executeAllCells}
          executeCell={executeCell}
          updateCellCode={updateCellCode}
          trustByCell={trustByCell}
          onOpenLineage={openLineage}
        />
      ) : (
      <div className="notebook-body">
      <div className="cells-container" ref={bodyRef} onScroll={handleBodyScroll} tabIndex={-1}>
        {cells.map((cell, index) => {
          const h = getCellHandlers(cell.id)
          return (
          <Cell
            key={cell.id}
            cell={cell}
            index={index}
            typeOrdinal={typeOrdinals[cell.id]}
            isConnected={vmAlive}
            catalogLoaded={runReady}
            isFlashing={flashCellIds.has(String(cell.id))}
            isActive={cell.id === activeCellId}
            isDragOver={cell.id === dragOverId}
            hasSearchMatch={searchMatchCellIds.has(cell.id)}
            onFocus={h.onFocus}
            onExecute={h.onExecute}
            onInterrupt={interruptExecution}
            onCodeChange={h.onCodeChange}
            onOutputVarChange={h.onOutputVarChange}
            onAddBelow={h.onAddBelow}
            onSetAiExplanation={h.onSetAiExplanation}
            onDelete={h.onDelete}
            onClearOutput={h.onClearOutput}
            onFormat={h.onFormat}
            isFormatting={formattingCellIds.has(cell.id)}
            onDragStart={h.onDragStart}
            onDragOver={h.onDragOver}
            onDrop={h.onDrop}
            onDragEnd={handleDragEnd}
            searchQuery={showSearch ? searchQuery : ''}
            searchActiveOccurrence={searchActiveOccurrenceMap[cell.id] ?? -1}
            cellBadgeById={cellBadgeById}
            getNotebookContext={getNotebookContext}
            notebookName={tab.name}
            microvmId={tab.microvmId}
            microvmEndpoint={tab.microvmEndpoint}
            sessionId={tab.sessionId}
            aiAvailable={aiAvailable}
            variables={variables}
            dagNode={dagGraph.nodeById.get(String(cell.id)) || null}
            dagIssues={issueSummaryById.get(String(cell.id)) || null}
            dataSources={tab._dataSources}
            lineageTrust={trustByCell.get(String(cell.id)) || null}
            onOpenLineage={openLineage}
            lineageStep={stepNumberById.get(String(cell.id)) ?? null}
            lineageDim={highlightActive && !stepNumberById.has(String(cell.id))}
            notebookId={tab.id}
            getExplainUpstreamSig={getExplainUpstreamSig}
          />
          )
        })}
        <div className="add-cell-row">
          <button className="add-cell-btn" onClick={() => addCellAtEnd('code')}>
            + Code
          </button>
          <button className="add-cell-btn" onClick={() => addCellAtEnd('sql')}>
            + SQL
          </button>
          <button className="add-cell-btn" onClick={() => addCellAtEnd('markdown')}>
            + Text
          </button>
        </div>
      </div>

      </div>
      )}
    </div>
    {/* Download icon → unified FILE export: re-runnable source vs read-only render. */}
    {exportMenuPos && createPortal(
      <div className="toolbar-portal-menu" style={{ position: 'fixed', top: exportMenuPos.top, left: exportMenuPos.left, zIndex: 9999 }}>
        <div className="toolbar-portal-backdrop" onClick={() => setExportMenuPos(null)} />
        <div className="toolbar-portal-options">
          <div className="toolbar-portal-group-label">Source · re-runnable</div>
          <button onClick={() => { downloadNative(); setExportMenuPos(null) }}>Native (.notebook.json)</button>
          <button onClick={downloadIPYNB}>Jupyter (.ipynb)</button>
          <div className="toolbar-portal-group-label">Rendered · read-only</div>
          <button onClick={() => { exportNotebookHTML(); setExportMenuPos(null) }}>{viewMode === 'app' ? 'App view — HTML' : 'Notebook — HTML'}</button>
          <button onClick={() => { exportNotebookMD(); setExportMenuPos(null) }}>{viewMode === 'app' ? 'App view — Markdown' : 'Notebook — Markdown'}</button>
        </div>
      </div>,
      document.body
    )}
    {/* Version history drawer — the single home for save / browse / diff /
        restore / per-cell revert. Opened by the toolbar history icon or Cmd+S. */}
    {showVersions && (
      <VersionHistoryDrawer tab={tab} onClose={() => setShowVersions(false)} />
    )}
    {/* "How was this computed?" — lineage of one output (App blocks + cell Output header). */}
    {lineage && (
      <LineageDrawer
        key={lineage.cellId}
        lineage={lineage}
        cells={cells}
        viewMode={viewMode}
        highlight={lineageHighlight}
        onToggleHighlight={toggleLineageHighlight}
        onOpenStep={openLineageStep}
        onClose={closeLineage}
        opener={lineageOpener}
      />
    )}
    {tab.microvmId && (
      <ExecutionHud
        isExecuting={isExecuting}
        metrics={vmMetrics[tab.microvmId]}
        memoryMib={instances[tab.microvmId]?.memory_mib || tab.microvmMemory || DEFAULT_MEMORY_MIB}
      />
    )}
    </>
  )
}
