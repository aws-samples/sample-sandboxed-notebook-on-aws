import { useState, useMemo, useCallback, useEffect, useRef } from 'react'
import { isTintOn } from '../../services/execTint'
import { bannerVisible, bannerText } from '../../services/runBanner'
import { requestRunHistoryReveal } from '../../services/runHistoryReveal'
import { jumpToCell } from '../../services/cellJump'
import ActionBanner from '../ActionBanner'
import { IconX, IconNotebook, IconEraser, IconChevronDown, IconChevronRight, IconLoader, IconLineage } from '../Icons'
import PanelState from './PanelState'
import PanelExpandButton from './PanelExpandButton'
import OutlineMap from './OutlineMap'
import { INSERT_FLASH_MS } from '../../hooks/useNotebookCells'
import { effectiveStatus, graphFromDag, relatedCells } from '../../services/depGraph'
import { staleReasonText } from '../../services/lineage'
import { cellBadgeMap } from '../../utils/cellBadge'
import { formatDurationShort } from '../../utils/formatDuration'
import { hasImageOutput } from '../../services/cellOutput'
import FilterPills from '../FilterPills'

// After the user scrolls the outline (wheel, touch, pointer, or scroll keys on the
// list), automatic reveal of the active row is held off for this long.
const USER_SCROLL_HOLD_MS = 4000
const USER_SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '])

// Extract a short window of `text` centered on the first match of `query`, so a
// hit buried in a long line (e.g. a markdown paragraph) is shown WITH the match
// visible rather than a fragment from the line's start. Adds leading/trailing "…"
// when the window is clipped. Collapses whitespace so wrapped prose reads as one
// tidy line. Returns a trimmed head of the text when there's no match.
function snippetAround(text, query, radius = 40) {
  const clean = String(text).replace(/\s+/g, ' ').trim()
  const q = (query || '').trim()
  if (!q) return clean.slice(0, radius * 2)
  const at = clean.toLowerCase().indexOf(q.toLowerCase())
  if (at < 0) return clean.slice(0, radius * 2)
  let start = Math.max(0, at - radius)
  let end = Math.min(clean.length, at + q.length + radius)
  // Snap to word boundaries so we don't cut mid-word (keeps it readable).
  if (start > 0) { const sp = clean.indexOf(' ', start); if (sp !== -1 && sp < at) start = sp + 1 }
  if (end < clean.length) { const sp = clean.lastIndexOf(' ', end); if (sp > at + q.length) end = sp }
  return (start > 0 ? '…' : '') + clean.slice(start, end) + (end < clean.length ? '…' : '')
}

// Split `text` around every case-insensitive occurrence of `query` and wrap the
// matches in <mark> so the search term is visible wherever it appears (label or
// snippet). Returns the raw text when there's no query/match, so non-search
// rendering is unchanged. Query is treated as a literal (escaped), not a regex.
function highlightMatch(text, query) {
  if (!query || !text) return text
  const q = query.trim()
  if (!q) return text
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = String(text).split(new RegExp(`(${escaped})`, 'ig'))
  if (parts.length === 1) return text  // no match
  const qLc = q.toLowerCase()
  return parts.map((part, i) =>
    part.toLowerCase() === qLc
      ? <mark key={i} className="outline-search-hit">{part}</mark>
      : part
  )
}

export default function OutlinePanel({
  cells,
  activeTab,
  onScrollToCell,
  onFixCell,
  onReorderCellIds,
  onRunFromCell,
  onRequestDeleteCells,
  onRunCells,
  onClearOutputs,
  onToggleHeatmap,
  onClearRunBanner,
  lineageCellId = null,
  onLineageCellChange,
  onClose,
  expanded,
  onToggleExpand,
}) {
  const [outlineSearch, setOutlineSearch] = useState('')
  const [outlineDragId, setOutlineDragId] = useState(null)
  const [outlineDragOverId, setOutlineDragOverId] = useState(null)
  const [collapsedSections, setCollapsedSections] = useState(new Set())
  const [selectMode, setSelectMode] = useState(false)
  const [selectedCells, setSelectedCells] = useState(new Set())
  // Active cell (scroll-sync): id of the cell currently most-visible in the viewport
  const [activeCellId, setActiveCellId] = useState(null)
  // Flash of just-inserted cells is applied via direct DOM class toggling in the
  // notebook-cells-inserted listener (below) — no React state needed.
  const flashTimerRef = useRef(null)
  // Type/status filter: 'all' | 'code' | 'markdown' | 'sql' | 'errors'
  const [typeFilter, setTypeFilter] = useState('all')
  // Lineage focus is owned by App (lineageCellId, per notebook tab) and set by a row's
  // Lineage button or the output-header "How was this computed?" trigger. It is not
  // hover state and is not persisted.
  // Heatmap mode (#8): tint rows by relative execution time (green→amber→red).
  // The state lives on the active notebook's tab (`_heatmapOn`, see services/execTint.js),
  // not in this panel. The panel unmounts when the user switches panels or notebooks,
  // so a Run All that finishes while another notebook is showing still tints its own
  // notebook when the user returns. useNotebookCells sets the flag on the tab when a
  // Run All completes (clean or with an error). The toggle flips it for the active tab.
  const heatmapMode = isTintOn(activeTab)
  // Run banner (services/runBanner.js): per notebook, stored on the tab. Shown
  // for the active notebook only, while its cells still match the finished run.
  const runBanner = activeTab?._runBanner
  const showRunBanner = bannerVisible(runBanner, cells)
  const openRunHistory = () => {
    if (!activeTab) return
    onClearRunBanner?.(activeTab.id)
    // Switches the sidebar to Notebooks; the card then opens the newest run.
    requestRunHistoryReveal(activeTab.id, { expandLatest: true, stamp: runBanner?.stamp ?? null, trigger: runBanner?.trigger ?? null })
  }
  const goToFirstError = () => {
    if (!activeTab) return
    const id = runBanner?.firstErrorId
    onClearRunBanner?.(activeTab.id)
    if (id != null) jumpToCell(id)
  }

  // Run state for the scroll fix. The execution-time tint no longer listens here.
  useEffect(() => {
    const onRunStatus = (e) => {
      runActiveRef.current = e.detail === 'running'
    }
    window.addEventListener('outline-run-status', onRunStatus)
    return () => window.removeEventListener('outline-run-status', onRunStatus)
  }, [])
  // When a cell is explicitly clicked/focused in the notebook, that selection is
  // authoritative for a short window. The scroll-sync observer must NOT override
  // the highlight during this window, otherwise it stomps the clicked row with
  // whatever cell happens to be most-visible after the focus scroll.
  const clickGuardUntil = useRef(0)
  // The .outline-list scroller (for revealing the active row inside it, never the page).
  const listRef = useRef(null)
  // Last time the USER scrolled the outline, and whether a batch run is in progress
  // (from 'outline-run-status'). Both gate the automatic reveal in onActiveCell.
  const lastUserScrollRef = useRef(0)
  const runActiveRef = useRef(false)
  // Latest outline items, readable from the (mount-only) click listener without
  // stale closures — used to reveal a clicked cell that sits under collapsed parents.
  const itemsRef = useRef([])

  // Scroll-sync: observe cells in the DOM and highlight the most-visible one.
  // Cells render with [data-cell-id="..."]. We track intersection ratios and
  // pick the cell with the largest visible area as "active".
  useEffect(() => {
    // The notebook's real scroller. Falls back to the viewport (null root) when it
    // isn't mounted, e.g. the App view.
    const container = document.querySelector('.cells-container')
    const cellEls = Array.from(document.querySelectorAll('[data-cell-id]'))
    if (cellEls.length === 0) return

    const ratios = new Map()
    const observer = new IntersectionObserver(
      (entries) => {
        // Respect a recent explicit click — don't fight the click-to-select highlight.
        if (Date.now() < clickGuardUntil.current) return
        for (const entry of entries) {
          const id = entry.target.getAttribute('data-cell-id')
          if (entry.isIntersecting) ratios.set(id, entry.intersectionRatio)
          else ratios.delete(id)
        }
        // Pick the most-visible cell
        let bestId = null
        let bestRatio = 0
        for (const [id, ratio] of ratios) {
          if (ratio > bestRatio) { bestRatio = ratio; bestId = id }
        }
        if (bestId) setActiveCellId(bestId)
      },
      { root: container, threshold: [0, 0.25, 0.5, 0.75, 1] }
    )
    cellEls.forEach(el => observer.observe(el))
    return () => observer.disconnect()
  }, [cells.length, activeTab?.id])

  // Click-to-select: when a cell is clicked/focused in the notebook, highlight
  // its row in the outline and scroll the row into view (helps in long notebooks).
  useEffect(() => {
    const onActiveCell = (e) => {
      const id = e.detail?.cellId
      if (!id) return
      // Make this selection authoritative for ~1s so the scroll-sync observer
      // (which fires when focusing scrolls the cell) can't override it.
      clickGuardUntil.current = Date.now() + 1000
      setActiveCellId(id)
      // If the clicked cell is hidden under collapsed parent sections, expand
      // those ancestors so its row is actually visible in the tree.
      const clicked = itemsRef.current.find(it => it.id === id)
      if (clicked && clicked.ancestors && clicked.ancestors.length) {
        setCollapsedSections(prev => {
          if (!clicked.ancestors.some(aid => prev.has(aid))) return prev
          const next = new Set(prev)
          clicked.ancestors.forEach(aid => next.delete(aid))
          return next
        })
      }
      // Reveal the row inside the outline list only. scrollIntoView would also scroll
      // the page and any other ancestor, and it fought the user's own outline scrolling.
      setTimeout(() => {
        const list = listRef.current
        if (!list) return
        // The user is scrolling the outline right now; leave their position alone.
        if (Date.now() - lastUserScrollRef.current < USER_SCROLL_HOLD_MS) return
        const row = list.querySelector(`.outline-item[data-outline-id="${CSS.escape(id)}"]`)
        if (!row) return
        const listRect = list.getBoundingClientRect()
        const rowRect = row.getBoundingClientRect()
        if (rowRect.top >= listRect.top && rowRect.bottom <= listRect.bottom) return  // already visible
        // Center the row in the list (the browser clamps at the ends).
        const offset = rowRect.top - listRect.top - (list.clientHeight - rowRect.height) / 2
        // Instant by default. Smooth only when motion is allowed and no run is active.
        const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
        const smooth = !reduceMotion && !runActiveRef.current
        list.scrollTo({ top: list.scrollTop + offset, behavior: smooth ? 'smooth' : 'instant' })
      }, 30)
    }
    window.addEventListener('notebook-active-cell', onActiveCell)
    return () => window.removeEventListener('notebook-active-cell', onActiveCell)
  }, [])

  // Record the user's own scroll intent on the outline list. Programmatic scrolls
  // (our reveal, the insert scroll) never fire wheel, touch, pointer, or key events,
  // so this tells user scrolling apart from ours without reacting to every scroll event.
  useEffect(() => {
    const list = listRef.current
    if (!list) return
    const markUserScroll = () => { lastUserScrollRef.current = Date.now() }
    const onKeyDown = (e) => { if (USER_SCROLL_KEYS.has(e.key)) markUserScroll() }
    const opts = { passive: true }
    list.addEventListener('wheel', markUserScroll, opts)
    list.addEventListener('touchmove', markUserScroll, opts)
    list.addEventListener('pointerdown', markUserScroll, opts)
    list.addEventListener('keydown', onKeyDown)
    return () => {
      list.removeEventListener('wheel', markUserScroll)
      list.removeEventListener('touchmove', markUserScroll)
      list.removeEventListener('pointerdown', markUserScroll)
      list.removeEventListener('keydown', onKeyDown)
    }
  }, [])

  // When cells are inserted (e.g. from the AI assistant), reveal the newly added
  // rows at the bottom of the outline. This panel only mounts when open, so the
  // listener existing means the Outline is open.
  useEffect(() => {
    const onInserted = (e) => {
      const ids = (e.detail?.cellIds || []).map(String)
      if (!ids.length) return
      const wanted = new Set(ids)
      // The outline rebuilds from tab._cells, which syncs a few async hops AFTER
      // the insert — so the new rows may not exist yet. Poll for them (up to ~1.5s),
      // then apply the flash class DIRECTLY to the DOM nodes. Direct DOM class
      // toggling sidesteps every state/prop-sync race: we flash exactly the rows
      // that are actually rendered for those ids, the instant they appear.
      const firstId = ids[0]
      const lastId = ids[ids.length - 1]
      let tries = 0
      const tryFlash = () => {
        const matches = Array.from(
          document.querySelectorAll('.outline-list .outline-item[data-outline-id]')
        ).filter(el => wanted.has(el.getAttribute('data-outline-id')))
        // The outline rebuilds row-by-row across async hops, so an early poll pass
        // can see ONLY the first inserted row. If we acted then, we'd scroll to that
        // first row and stop — which is the bug (outline lands on the block's start
        // and the trailing new rows stay below the fold). So wait until the LAST
        // inserted id has actually rendered (that's the bottom of the new block),
        // and only then flash + scroll. Fall back to whatever rendered once the poll
        // budget is exhausted, so we still do SOMETHING if a row never appears.
        const lastRendered = matches.some(el => el.getAttribute('data-outline-id') === lastId)
        const budgetLeft = tries++ < 12
        if ((matches.length === 0 || !lastRendered) && budgetLeft) {
          flashTimerRef.current = setTimeout(tryFlash, 150)
          return
        }
        if (matches.length === 0) return  // gave up — rows never appeared
        for (const el of matches) {
          // Restart the animation if the class is already present.
          el.classList.remove('outline-item-flash')
          void el.offsetWidth  // force reflow so re-adding restarts the animation
          el.classList.add('outline-item-flash')
          setTimeout(() => el.classList.remove('outline-item-flash'), INSERT_FLASH_MS)
        }
        // Guard the scroll-sync observer for a moment so it doesn't immediately stomp
        // this selection with whatever is most-visible after the insert scroll (same
        // guard the click-to-select path uses).
        clickGuardUntil.current = Date.now() + 1200
        // Keep the FIRST inserted row as the active/highlighted one (marks where the
        // new block begins, matching the notebook which scrolls to the block's start).
        setActiveCellId(firstId)
        // SCROLL so the whole newly added block is visible. Inserts always append, so
        // the new rows are the TAIL of the outline. scrollIntoView on the last row is
        // unreliable here: the outline is a heading TREE, so the block's last cell can
        // be a nested child row that renders after the scroll, leaving us pinned on the
        // block's heading. Instead scroll the .outline-list container itself to the
        // bottom — that guarantees the trailing inserted rows are visible regardless of
        // nesting. Re-assert after a settle delay because the outline rebuilds its rows
        // from tab._cells a few async hops later (which resets scrollTop).
        const scrollToBottom = () => {
          const list = document.querySelector('.outline-list')
          if (list) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' })
        }
        scrollToBottom()
        setTimeout(scrollToBottom, 300)
        setTimeout(scrollToBottom, 650)
      }
      tryFlash()
    }
    window.addEventListener('notebook-cells-inserted', onInserted)
    return () => {
      window.removeEventListener('notebook-cells-inserted', onInserted)
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current)
    }
  }, [])

  // Build cell outline items with a heading-depth hierarchy.
  // Markdown cells with # headings define the tree; their heading level (1-6)
  // drives nesting. Code/SQL/plain-markdown cells attach to the current section.
  // The stored DAG document for the active notebook (traversal only).
  const dag = activeTab?._dag || null
  const dagGraph = useMemo(() => graphFromDag(dag), [dag])
  const cellOutlineItems = useMemo(() => {
    const badges = cellBadgeMap(cells)
    // Independent per-type counters → PY-1, SQL-1, MD-1 badges. These match the
    // in-cell type badges exactly (Cell.jsx renders the same PY-n / SQL-n / MD-n
    // from the typeOrdinal computed in Notebook.jsx by the same order-walk).
    let pyCounter = 0
    let sqlCounter = 0
    let mdCounter = 0
    const sectionStack = []            // [{ id, level }] — currently-open heading sections
    const headingCounters = [0, 0, 0, 0, 0, 0]  // for 1 / 1.1 / 1.2 numbering

    return cells.map((cell, idx) => {
      let label = ''
      let icon = ''
      const cellType = cell.type || 'code'
      let headingLevel = null

      if (cellType === 'markdown') {
        const firstLine = (cell.code || '').split('\n').find(l => l.trim()) || ''
        const m = firstLine.match(/^(#{1,6})\s+/)
        if (m) {
          headingLevel = m[1].length
          label = firstLine.replace(/^#+\s*/, '')
        } else {
          label = firstLine.slice(0, 60) || '(text)'
        }
        mdCounter++
        icon = `MD-${mdCounter}`
      } else if (cellType === 'sql') {
        sqlCounter++
        const lines = (cell.code || '').split('\n').filter(l => l.trim() && !l.trim().startsWith('--'))
        label = lines.length > 0 ? lines[0].trim().slice(0, 50) : '(empty)'
        icon = `SQL-${sqlCounter}`
      } else {
        pyCounter++
        const lines = (cell.code || '').split('\n').filter(l => l.trim())
        const meaningfulLine = lines.find(l => !l.trim().startsWith('import ') && !l.trim().startsWith('from ') && !l.trim().startsWith('#'))
        if (meaningfulLine) {
          label = meaningfulLine.trim().slice(0, 50)
        } else if (lines.length > 0) {
          const pkgs = lines.filter(l => l.trim().startsWith('import ') || l.trim().startsWith('from '))
            .map(l => l.replace(/^(import |from )/, '').split(/[\s,.]/)[0])
            .slice(0, 3)
          label = `imports: ${pkgs.join(', ')}`
        } else {
          label = '(empty)'
        }
        icon = `PY-${pyCounter}`
      }

      // Compute hierarchy: depth, ancestor section ids, and section number
      let depth, ancestors, sectionNumber = ''
      if (headingLevel != null) {
        // Close any open sections at the same or deeper level
        while (sectionStack.length && sectionStack[sectionStack.length - 1].level >= headingLevel) {
          sectionStack.pop()
        }
        ancestors = sectionStack.map(s => s.id)
        depth = sectionStack.length
        // Numbering: bump this level, reset deeper levels
        headingCounters[headingLevel - 1]++
        for (let l = headingLevel; l < 6; l++) headingCounters[l] = 0
        sectionNumber = headingCounters.slice(0, headingLevel).filter(n => n > 0).join('.')
        sectionStack.push({ id: cell.id, level: headingLevel })
      } else {
        // Code / SQL / plain-markdown: nest under the current open section
        ancestors = sectionStack.map(s => s.id)
        depth = sectionStack.length
      }

      return {
        id: cell.id, idx, label, icon, cellType,
        headingLevel, isSection: headingLevel != null, depth, ancestors, sectionNumber,
        code: cell.code || '',            // full source — searched in addition to the label
        hasOutput: !!(cell.output || cell.html || hasImageOutput(cell)),
        hasError: !!cell.error,
        isInterrupted: cell.status === 'interrupted',
        isRunning: cell.status === 'running',
        // Queued in a batch (Run All / run-from): its previous result is stale, so
        // it shows no tick until this run reaches it.
        isQueued: cell.status === 'pending',
        // Edited = live code vs last run (effectiveStatus; never the node's edited).
        isStale: effectiveStatus({ code: cell.code, lastExecutedCode: cell.lastExecutedCode }, dagGraph.nodeById.get(String(cell.id))) === 'edited',
        isDataflowStale: !!dagGraph.nodeById.get(String(cell.id))?.stale_reason,
        dataflowStaleVars: dagGraph.nodeById.get(String(cell.id))?.stale_reason?.vars || [],
        dataflowStaleTitle: staleReasonText(dagGraph.nodeById.get(String(cell.id))?.stale_reason, badges),
        // Only code/SQL cells that are DAG nodes get a Lineage button (markdown never does).
        hasLineage: cellType !== 'markdown' && dagGraph.nodeById.has(String(cell.id)),
        // Lint issue count from the last lint pass — gated on freshness (code hasn't
        // changed since it was linted) so it matches the in-cell indicator exactly.
        // Clears to 0 when a re-lint finds the cell clean (lint:[]) or after an edit.
        lintCount: (cell.lint && cell.lintCode != null && cell.code === cell.lintCode)
          ? cell.lint.length : 0,
        aiExplanation: cell.aiExplanation || null,
        executionTime: cell.executionTime,
        finishedAt: cell.finishedAt || 0,
      }
    })
  }, [cells, dagGraph])

  // Keep the ref current so the mount-only click listener sees fresh items.
  itemsRef.current = cellOutlineItems

  // Summary stats
  const stats = useMemo(() => {
    const executed = cellOutlineItems.filter(i => i.hasOutput || i.hasError)
    const errors = cellOutlineItems.filter(i => i.hasError)
    const totalTime = cellOutlineItems.reduce((sum, i) => sum + (i.executionTime || 0), 0)
    const lastFinishedAt = cellOutlineItems.reduce((mx, i) => (i.finishedAt > mx ? i.finishedAt : mx), 0)
    return { executedCount: executed.length, errorCount: errors.length, totalTime, lastFinishedAt }
  }, [cellOutlineItems])

  // Lineage focus: the focused cell's transitive upstream and downstream, with depth
  // (1 = direct, more = indirect). Same closure as the "How was this computed?"
  // drawer (depGraph.relatedCells). Active only when the focused cell is a DAG node.
  const lineageFocusId = lineageCellId ? String(lineageCellId) : null
  const lineageActive = !!lineageFocusId && dagGraph.nodeById.has(lineageFocusId)
  const related = useMemo(
    () => (lineageActive ? relatedCells(dagGraph, lineageFocusId) : null),
    [lineageActive, lineageFocusId, dagGraph]
  )

  // Per-row relation: { kind: 'focus' | 'upstream' | 'downstream', depth } or null.
  // Markdown never has a relation. Cheap Map lookups against the memo above.
  const relationOf = useCallback((item) => {
    if (!related || item.cellType === 'markdown') return null
    const id = String(item.id)
    if (id === lineageFocusId) return { kind: 'focus', depth: 0 }
    const up = related.upstream.get(id)
    if (up != null) return { kind: 'upstream', depth: up }
    const down = related.downstream.get(id)
    if (down != null) return { kind: 'downstream', depth: down }
    return null
  }, [related, lineageFocusId])

  // Tooltip text that explains the relation of a row to the focused cell.
  const relationTip = (rel) => {
    if (!rel) return null
    if (rel.kind === 'focus') return 'Focused cell: its inputs and outputs are highlighted'
    const steps = rel.depth > 1 ? ` through ${rel.depth} steps` : ' directly'
    return rel.kind === 'upstream'
      ? `Input to the focused cell${steps}`
      : `Uses the focused cell's output${steps}`
  }

  // Max execution time across executed cells — the denominator for heatmap (#8)
  // intensity. 0 when nothing has run (heatmap then adds no tint).
  const maxExecTime = useMemo(
    () => cellOutlineItems.reduce((mx, i) => (i.executionTime > mx ? i.executionTime : mx), 0),
    [cellOutlineItems]
  )

  // Map a cell's execution time to a 0–4 heatmap bucket (green→amber→red). Buckets
  // (not a continuous gradient) keep the tint readable and align to CSS classes.
  const heatBucket = useCallback((ms) => {
    if (!heatmapMode || !ms || maxExecTime <= 0) return null
    const r = ms / maxExecTime
    if (r >= 0.8) return 4
    if (r >= 0.55) return 3
    if (r >= 0.3) return 2
    if (r >= 0.1) return 1
    return 0
  }, [heatmapMode, maxExecTime])

  // An item is visible if none of its ancestor sections are collapsed.
  const isItemVisible = useCallback((item) => {
    return !item.ancestors.some(aid => collapsedSections.has(aid))
  }, [collapsedSections])

  // Subtree of a section = all items that list it as an ancestor.
  const getSubtree = useCallback((sectionId) => {
    return cellOutlineItems.filter(i => i.ancestors.includes(sectionId))
  }, [cellOutlineItems])

  const toggleSection = (sectionId) => {
    setCollapsedSections(prev => {
      const next = new Set(prev)
      if (next.has(sectionId)) next.delete(sectionId)
      else next.add(sectionId)
      return next
    })
  }

  // Ids of every heading section — used by the expand/collapse-all toggle.
  const sectionIds = useMemo(
    () => cellOutlineItems.filter(i => i.isSection).map(i => i.id),
    [cellOutlineItems]
  )
  const hasSections = sectionIds.length > 0
  const anyCollapsed = sectionIds.some(id => collapsedSections.has(id))

  // One smart toggle: expand everything if anything is collapsed, otherwise
  // collapse every section.
  const toggleExpandAll = () => {
    setCollapsedSections(anyCollapsed ? new Set() : new Set(sectionIds))
  }

  // Apply type/status filter predicate
  const matchesTypeFilter = useCallback((item) => {
    switch (typeFilter) {
      case 'code': return item.cellType === 'code'
      case 'markdown': return item.cellType === 'markdown'
      case 'sql': return item.cellType === 'sql'
      case 'errors': return item.hasError
      default: return true
    }
  }, [typeFilter])

  // A flat filtered list is used when EITHER a text search or a non-"all"
  // type filter is active (both break the section grouping into a flat view).
  const isFiltering = !!outlineSearch || typeFilter !== 'all'
  const searchLc = outlineSearch.trim().toLowerCase()
  const filteredOutline = useMemo(() => {
    if (!isFiltering) return null

    // No text query (type-filter only) → keep DOCUMENT ORDER, no snippet/ranking.
    if (!searchLc) {
      return cellOutlineItems.filter(matchesTypeFilter)
    }

    // Text search: score each item so the most relevant matches float to the top,
    // and capture WHERE the match was + a snippet line so the row can show context.
    //
    // Rank tiers (higher = better):
    //   4  label/heading match      — the human-facing title of the cell
    //   3  variable-DEFINITION line — `name = ...` / `def name` / `class name` etc.
    //   2  other source-line match  — matched somewhere in the body
    //   1  AI-explanation match     — matched the cell's note, not its content
    // Ties break on document order (idx), preserving the notebook's natural flow.
    const scored = []
    for (const item of cellOutlineItems) {
      if (!matchesTypeFilter(item)) continue

      const labelHit = item.label.toLowerCase().includes(searchLc)
      const codeLines = item.code.split('\n')
      // First source line that contains the query (for the snippet + line number).
      let codeLineIdx = -1
      for (let i = 0; i < codeLines.length; i++) {
        if (codeLines[i].toLowerCase().includes(searchLc)) { codeLineIdx = i; break }
      }
      const explHit = item.aiExplanation
        ? item.aiExplanation.toLowerCase().includes(searchLc)
        : false

      if (!labelHit && codeLineIdx < 0 && !explHit) continue  // no match anywhere

      let matchRank = 1
      let matchSnippet = null   // { text, lineNo } | null — a body line to show under the label
      if (labelHit) {
        // The title already shows the match (highlighted). Showing a second prose
        // line that repeats it just looks like a confusing near-duplicate, so a
        // label match gets NO snippet — the highlighted title is the context.
        matchRank = 4
      } else if (codeLineIdx >= 0) {
        const line = codeLines[codeLineIdx]
        // Is this line a variable/def/class definition? Rank those above plain body hits.
        const isDef = /^\s*(?:def |class |async def )/.test(line)
          || /^\s*[A-Za-z_][\w.]*\s*(?:[:,\w\s]*)?=(?!=)/.test(line)   // assignment (not ==)
        matchRank = isDef ? 3 : 2
        matchSnippet = { text: snippetAround(line, outlineSearch), lineNo: codeLineIdx + 1 }
      } else if (explHit) {
        matchRank = 1
        matchSnippet = { text: snippetAround(item.aiExplanation, outlineSearch), lineNo: null, isNote: true }
      }

      scored.push({ ...item, matchRank, matchSnippet })
    }

    // Sort by rank desc, then document order asc (stable, predictable).
    scored.sort((a, b) => (b.matchRank - a.matchRank) || (a.idx - b.idx))
    return scored
  }, [isFiltering, searchLc, cellOutlineItems, matchesTypeFilter])

  // Compact duration for the outline rail (m/s/ms). Delegates to the shared helper
  // so the outline and the cell-output footer format times identically — and long
  // runs read as "1m 36s" instead of a wall of milliseconds.
  const formatTime = (ms) => formatDurationShort(ms)

  // Relative "time ago" for the last-run timestamp (epoch ms).
  const formatAgo = (ts) => {
    if (!ts) return ''
    const secs = Math.max(0, Math.floor((Date.now() - ts) / 1000))
    if (secs < 10) return 'just now'
    if (secs < 60) return `${secs}s ago`
    const mins = Math.floor(secs / 60)
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h ago`
    return `${Math.floor(hrs / 24)}d ago`
  }

  // Selection handlers
  const toggleSelect = useCallback((id, e) => {
    if (e?.shiftKey && selectedCells.size > 0) {
      // Shift-click: range select
      const allIds = cellOutlineItems.map(i => i.id)
      const lastSelected = [...selectedCells].pop()
      const lastIdx = allIds.indexOf(lastSelected)
      const currentIdx = allIds.indexOf(id)
      const [start, end] = lastIdx < currentIdx ? [lastIdx, currentIdx] : [currentIdx, lastIdx]
      setSelectedCells(prev => {
        const next = new Set(prev)
        for (let i = start; i <= end; i++) next.add(allIds[i])
        return next
      })
    } else {
      // If this is a heading section, select/deselect its entire subtree too
      const item = cellOutlineItems.find(i => i.id === id)
      const subtree = item?.isSection ? getSubtree(id) : []
      setSelectedCells(prev => {
        const next = new Set(prev)
        const wasSelected = next.has(id)
        if (wasSelected) {
          next.delete(id)
          subtree.forEach(c => next.delete(c.id))
        } else {
          next.add(id)
          subtree.forEach(c => next.add(c.id))
        }
        return next
      })
    }
  }, [cellOutlineItems, selectedCells, getSubtree])

  const selectAll = () => setSelectedCells(new Set(cellOutlineItems.map(i => i.id)))
  const selectNone = () => setSelectedCells(new Set())

  // Every delete goes through the app-level confirmation (requestDeleteCells in App).
  // Focus falls back to the outline list if the clicked control is gone afterwards.
  const handleBulkDelete = () => {
    if (selectedCells.size === 0 || !onRequestDeleteCells) return
    onRequestDeleteCells([...selectedCells], {
      focusFallbackSelector: '.outline-list',
      afterDelete: () => { setSelectedCells(new Set()); setSelectMode(false) },
    })
  }

  const handleDeleteSingle = (id) => {
    if (onRequestDeleteCells) onRequestDeleteCells([id], { focusFallbackSelector: '.outline-list' })
  }

  // Tree-aware block move. The selected contiguous block moves as a unit and
  // steps over the ADJACENT SIBLING UNIT at the same tree level (same depth and
  // same parent). Sections move past whole sibling subtrees; leaf cells move
  // past adjacent leaf siblings within their parent and never cross the heading.
  // Returns the new ordered id array, or null if the move isn't possible.
  const sameParent = (a, b) => a.length === b.length && a.every((v, i) => v === b[i])

  const computeBlockMove = useCallback((direction) => {
    if (selectedCells.size === 0) return null
    const items = cellOutlineItems
    const selIdx = items.filter(i => selectedCells.has(i.id)).map(i => i.idx).sort((a, b) => a - b)
    const s = selIdx[0]
    const e = selIdx[selIdx.length - 1]
    // Must be a contiguous block — otherwise the move is ambiguous.
    if (e - s + 1 !== selIdx.length) return null

    const top = items[s]
    const anchorDepth = top.depth
    const parent = top.ancestors
    const order = items.map(i => i.id)
    const block = order.slice(s, e + 1)

    if (direction === 'up') {
      // Find the start of the previous sibling unit (scan up until same depth+parent).
      let j = -1
      for (let k = s - 1; k >= 0; k--) {
        if (items[k].depth < anchorDepth) break            // reached parent/ancestor — no prior sibling
        if (items[k].depth === anchorDepth && sameParent(items[k].ancestors, parent)) { j = k; break }
        // depth > anchorDepth → inside a previous sibling's subtree, keep scanning up
      }
      if (j < 0) return null
      const sibling = order.slice(j, s)                    // [j .. s-1] = whole previous sibling unit
      return [...order.slice(0, j), ...block, ...sibling, ...order.slice(e + 1)]
    } else {
      // Find the start of the next sibling unit (scan down until same depth+parent).
      let k = -1
      for (let m = e + 1; m < items.length; m++) {
        if (items[m].depth < anchorDepth) break            // reached parent boundary — no next sibling
        if (items[m].depth === anchorDepth && sameParent(items[m].ancestors, parent)) { k = m; break }
      }
      if (k < 0) return null
      // Next sibling unit spans [k .. end) where end = next item at depth <= anchorDepth.
      let end = items.length
      for (let m = k + 1; m < items.length; m++) {
        if (items[m].depth <= anchorDepth) { end = m; break }
      }
      const sibling = order.slice(k, end)
      return [...order.slice(0, s), ...sibling, ...block, ...order.slice(end)]
    }
  }, [cellOutlineItems, selectedCells])

  const canMoveUp = useMemo(() => computeBlockMove('up') !== null, [computeBlockMove])
  const canMoveDown = useMemo(() => computeBlockMove('down') !== null, [computeBlockMove])

  const handleMoveSelected = (direction) => {
    const newOrder = computeBlockMove(direction)
    if (!newOrder) return
    if (onReorderCellIds) onReorderCellIds(newOrder)
  }

  // Tree-aware drag: dragging a section heading moves its ENTIRE subtree as a
  // unit; dragging a leaf cell moves just that cell. Drop inserts the block
  // before/after the target row depending on drag direction (so you can also
  // drop at the very end). Returns the new ordered id array, or null for a no-op.
  const computeDragMove = useCallback((draggedId, targetId) => {
    if (!draggedId || draggedId === targetId) return null
    const items = cellOutlineItems
    const order = items.map(i => i.id)
    const dragItem = items.find(i => i.id === draggedId)
    const targetItem = items.find(i => i.id === targetId)
    if (!dragItem || !targetItem) return null

    const blockIds = dragItem.isSection
      ? [draggedId, ...getSubtree(draggedId).map(i => i.id)]
      : [draggedId]
    const blockSet = new Set(blockIds)
    if (blockSet.has(targetId)) return null   // can't drop a section into its own subtree

    const remaining = order.filter(id => !blockSet.has(id))
    let insertAt = remaining.indexOf(targetId)
    if (insertAt < 0) return null
    // Dragging downward drops AFTER the target; upward drops BEFORE it.
    if (dragItem.idx < targetItem.idx) insertAt += 1
    return [...remaining.slice(0, insertAt), ...blockIds, ...remaining.slice(insertAt)]
  }, [cellOutlineItems, getSubtree])

  // Run every executable (code/SQL) cell in a section's subtree, in order.
  const runSection = (sectionId) => {
    if (!onRunCells) return
    const indices = getSubtree(sectionId)
      .filter(i => i.cellType !== 'markdown')
      .map(i => i.idx)
      .sort((a, b) => a - b)
    if (indices.length) onRunCells(indices)
  }

  // Delete a section heading together with all of its descendants (with confirm).
  const requestDeleteSection = (sectionId) => {
    if (!onRequestDeleteCells) return
    const ids = [sectionId, ...getSubtree(sectionId).map(i => i.id)]
    onRequestDeleteCells(ids, { focusFallbackSelector: '.outline-list' })
  }

  const toggleSelectMode = () => {
    if (selectMode) {
      setSelectedCells(new Set())
    }
    setSelectMode(!selectMode)
  }

  const renderItem = (item) => {
    // Lineage relation class (#1) and heatmap tint class (#8) for this row. Both
    // are inert unless their state is on, so normal rows are unchanged.
    const rel = relationOf(item)
    const isFocus = rel?.kind === 'focus'
    const heat = heatBucket(item.executionTime)
    let lineageCls = ''
    if (lineageActive) {
      if (!rel) lineageCls = ' outline-item-lineage-dim'
      else if (isFocus) lineageCls = ' outline-item-lineage-focus'
      else lineageCls = ` outline-item-lineage-${rel.kind}${rel.depth > 1 ? ' outline-item-lineage-indirect' : ''}`
    }
    const heatCls = heat != null ? ` outline-heat-${heat}` : ''
    return (
    <div
      key={item.id}
      data-outline-id={item.id}
      style={{ paddingLeft: `${10 + (isFiltering ? 0 : item.depth) * 14}px` }}
      className={`outline-item outline-item-${item.cellType}${item.isSection ? ` outline-item-h${item.headingLevel}` : ''}${outlineDragOverId === item.id ? ' outline-item-dragover' : ''}${item.isSection ? ' outline-item-section' : ''}${selectedCells.has(item.id) ? ' outline-item-selected' : ''}${activeCellId === item.id && !selectMode ? ' outline-item-active' : ''}${item.isRunning ? ' outline-item-running' : ''}${lineageCls}${heatCls}`}
      onClick={(e) => {
        if (selectMode) {
          toggleSelect(item.id, e)
        } else if (item.isSection && !isFiltering) {
          // In the normal TREE view a heading toggles its section open/closed.
          // But in a flat search/filter list there's no subtree to collapse, so a
          // heading (markdown) click would appear to "do nothing" — instead treat
          // it like any other row and navigate to the cell.
          toggleSection(item.id)
        } else {
          onScrollToCell && onScrollToCell(item.idx)
        }
      }}
      draggable={!isFiltering && !selectMode}
      onDragStart={() => setOutlineDragId(item.id)}
      onDragOver={(e) => { e.preventDefault(); if (outlineDragId && outlineDragId !== item.id) setOutlineDragOverId(item.id) }}
      onDrop={() => {
        const newOrder = computeDragMove(outlineDragId, item.id)
        if (newOrder && onReorderCellIds) onReorderCellIds(newOrder)
        setOutlineDragId(null)
        setOutlineDragOverId(null)
      }}
      onDragEnd={() => { setOutlineDragId(null); setOutlineDragOverId(null) }}
      title={relationTip(rel)
        ? `${relationTip(rel)}\n${item.aiExplanation || item.label}`
        : (item.aiExplanation || item.label)}
    >
      {/* Indent guide rails — one vertical line per nesting level (absolute) */}
      {!isFiltering && item.depth > 0 && Array.from({ length: item.depth }).map((_, i) => (
        <span key={`guide-${i}`} className="outline-indent-guide" style={{ left: `${16 + i * 14}px` }} />
      ))}
      {/* Checkbox in select mode */}
      {selectMode && (
        <input
          type="checkbox"
          className="outline-checkbox"
          checked={selectedCells.has(item.id)}
          onChange={() => toggleSelect(item.id)}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      {!selectMode && (
        // Collapse chevron only in the TREE view. When filtering, headings navigate
        // (not collapse), so show the spacer instead of a misleading chevron.
        item.isSection && !isFiltering
          ? <span className="outline-section-chevron">{collapsedSections.has(item.id) ? '▸' : '▾'}</span>
          : <span className="outline-chevron-spacer" />
      )}
      <span className={`outline-item-icon outline-icon-${item.cellType}`}>
        {item.icon}
      </span>
      {item.isSection && item.sectionNumber && !isFiltering && (
        <span className="outline-section-number">{item.sectionNumber}</span>
      )}
      <span className="outline-item-label">
        {/* Title — always a single truncated line. When searching, the query is
            highlighted inside it; otherwise it's shown verbatim. */}
        <span className="outline-item-title">
          {searchLc ? highlightMatch(item.label, outlineSearch) : item.label}
        </span>
        {/* Match snippet: a short window CENTERED on the matched text (from the cell
            source or AI note) so the user sees WHY this row matched. Only present on
            text-search results whose match wasn't already visible in the title. */}
        {item.matchSnippet && (
          <span className="outline-item-snippet">
            {item.matchSnippet.isNote
              ? <span className="outline-snippet-tag">note</span>
              : item.matchSnippet.lineNo != null && <span className="outline-snippet-line">L{item.matchSnippet.lineNo}</span>}
            <span className="outline-snippet-text">{highlightMatch(item.matchSnippet.text, outlineSearch)}</span>
          </span>
        )}
      </span>
      {/* Lint indicator — amber warning triangle + count when the last lint pass
          flagged this cell (freshness-gated in cellOutlineItems). Independent of run
          status, so it renders before the run/status block. Clears when a re-lint
          finds the cell clean or the code is edited. */}
      {item.lintCount > 0 && (
        <span className="outline-item-status outline-status-lint" title={`${item.lintCount} lint issue${item.lintCount === 1 ? '' : 's'}`}>
          <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
          <span className="outline-lint-count">{item.lintCount}</span>
        </span>
      )}
      {item.isRunning ? (
        <IconLoader width={12} height={12} className="outline-status-running" />
      ) : (
        <>
          {item.isQueued && <span className="outline-item-status outline-status-queued" title="Queued: waiting for the cells before it" aria-label="Queued">○</span>}
          {item.isStale && <span className="outline-item-status outline-status-stale" title="Modified since last run">●</span>}
          {item.isDataflowStale && !item.isStale && (
            <span
              className="outline-item-status outline-status-dataflow-stale"
              title={item.dataflowStaleTitle || undefined}
            >●</span>
          )}
          {/* Interrupted takes precedence over error (an interrupted cell also has
              an error string) — show the same paused ◼ marker as the Notebooks panel. */}
          {item.isInterrupted ? (
            <span className="outline-item-status outline-status-interrupted" title="Execution stopped by user">◼</span>
          ) : item.hasError ? (
            <span className="outline-item-status outline-status-error">✗</span>
          ) : item.hasOutput && !item.isQueued && !item.isRunning ? (
            <span className="outline-item-status outline-status-success" title={item.executionTime ? `Executed in ${formatTime(item.executionTime)}` : 'Executed'}>
              ✓{item.executionTime != null && <span className="outline-exec-time">{formatTime(item.executionTime)}</span>}
            </span>
          ) : null}
        </>
      )}
      {/* Hover actions (only in non-select mode). The Lineage button lives in this
          group, ahead of Run and Delete: the group is an overlay in light and dark
          themes, so a separate sibling slot could sit under it. */}
      {!selectMode && (
        <div className="outline-hover-actions">
          {/* Lineage button (DAG cells only, never markdown). Toggles the focus shared with
              the "How was this computed?" drawer: same cell again closes it, another cell
              moves the focus. Stops propagation so the row's scroll-to-cell click doesn't run. */}
          {item.hasLineage && (
            <button
              type="button"
              className={`outline-lineage-btn${isFocus ? ' active' : ''}`}
              aria-label={`Show lineage for ${item.label || item.icon}`}
              aria-pressed={isFocus}
              title={isFocus ? 'Close lineage' : 'Show lineage: how this cell is computed, with related cells highlighted'}
              onClick={(e) => { e.stopPropagation(); onLineageCellChange?.(isFocus ? null : item.id) }}
            >
              <IconLineage width={12} height={12} aria-hidden="true" />
            </button>
          )}
          {item.isSection ? (
            <>
              {onRunCells && (
                <button
                  className="outline-action-btn outline-run-from"
                  onClick={(e) => { e.stopPropagation(); runSection(item.id) }}
                  title="Run all cells in this section"
                >▶</button>
              )}
              {onRequestDeleteCells && (
                <button
                  className="outline-action-btn outline-delete-btn"
                  onClick={(e) => { e.stopPropagation(); requestDeleteSection(item.id) }}
                  title="Delete section and its contents"
                >✕</button>
              )}
            </>
          ) : (
            <>
              {/* Fix it (#7): only for error cells. Navigates to the cell and fires
                  the AI fix (via onFixCell → notebook-fix-cell). Runs the same
                  handleAiFix the in-cell ⚡ Fix button uses. */}
              {item.hasError && onFixCell && (
                <button
                  className="outline-action-btn outline-fix-btn"
                  onClick={(e) => { e.stopPropagation(); onFixCell(item.id) }}
                  title="Fix this error with AI (jumps to the cell)"
                >⚡ Fix</button>
              )}
              {onRunFromCell && (
                <button
                  className="outline-action-btn outline-run-from"
                  onClick={(e) => { e.stopPropagation(); onRunFromCell(item.idx) }}
                  title="Run from here"
                >▶</button>
              )}
              {onRequestDeleteCells && (
                <button
                  className="outline-action-btn outline-delete-btn"
                  onClick={(e) => { e.stopPropagation(); handleDeleteSingle(item.id) }}
                  title="Delete cell"
                >✕</button>
              )}
            </>
          )}
        </div>
      )}
    </div>
    )
  }

  // Cell map (#3) on the right edge: proportional ticks, section bands, a status lane
  // for exceptions and the notebook viewport window. See OutlineMap.jsx and
  // services/outlineMap.js. Always shown (short notebooks too); always reflects ALL
  // cells regardless of the current search/type filter.
  const renderMinimap = () => {
    if (cellOutlineItems.length === 0) return null
    return (
      <OutlineMap
        items={cellOutlineItems}
        activeCellId={activeCellId}
        lineageActive={lineageActive}
        relationOf={relationOf}
        onScrollToCell={onScrollToCell}
        notebookKey={activeTab?.id}
      />
    )
  }

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Outline</span>
        <span className="sidebar-panel-count">{cells.length} cells</span>
        {stats.errorCount > 0 && (
          <span className="outline-error-badge">{stats.errorCount} error{stats.errorCount > 1 ? 's' : ''}</span>
        )}
        {hasSections && !selectMode && (
          <button
            className="outline-expand-toggle"
            onClick={toggleExpandAll}
            title={anyCollapsed ? 'Expand all sections' : 'Collapse all sections'}
          >
            {anyCollapsed ? <IconChevronDown width={12} height={12} /> : <IconChevronRight width={12} height={12} />}
          </button>
        )}
        {/* Heatmap toggle (#8): only meaningful once at least one cell has timing. */}
        {!selectMode && maxExecTime > 0 && (
          <button
            className={`outline-header-toggle ${heatmapMode ? 'active' : ''}`}
            onClick={() => onToggleHeatmap?.()}
            title={heatmapMode ? 'Execution-time heatmap on (click to turn off)' : 'Tint cells by execution time (slowest = red)'}
          >
            {/* flame/heat glyph */}
            <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 2s5 4.5 5 9a5 5 0 0 1-10 0c0-1.6.7-3 1.5-4 .2 1.2 1 2 2 2 .3-2.4-.5-5-1.5-7 1.5.5 3 1.8 3 0z"/>
            </svg>
          </button>
        )}
        <button
          className={`outline-select-toggle ${selectMode ? 'active' : ''}`}
          onClick={toggleSelectMode}
          title={selectMode ? 'Exit select mode' : 'Multi-select mode'}
        >
          ☑
        </button>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      {activeTab && <div className="sidebar-scope-pill"><IconNotebook width={12} height={12} /> {activeTab.name}</div>}

      {/* Bulk action bar (visible when cells selected) */}
      {selectMode && selectedCells.size > 0 && (
        <div className="outline-bulk-bar">
          <span className="outline-bulk-count">{selectedCells.size} selected</span>
          <button className="outline-bulk-btn" onClick={selectAll} title="Select all">All</button>
          <button className="outline-bulk-btn" onClick={selectNone} title="Deselect all">None</button>
          <button className="outline-bulk-btn outline-bulk-icon" onClick={() => handleMoveSelected('up')} disabled={!canMoveUp} title={canMoveUp ? 'Move up (past previous sibling)' : 'No sibling above'}>
            <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="18 15 12 9 6 15"/></svg>
          </button>
          <button className="outline-bulk-btn outline-bulk-icon" onClick={() => handleMoveSelected('down')} disabled={!canMoveDown} title={canMoveDown ? 'Move down (past next sibling)' : 'No sibling below'}>
            <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          {onRunCells && (
            <button className="outline-bulk-btn outline-bulk-icon outline-bulk-run" onClick={() => {
              const indices = cellOutlineItems.filter(i => selectedCells.has(i.id) && i.cellType !== 'markdown').map(i => i.idx).sort((a, b) => a - b)
              if (indices.length > 0) onRunCells(indices)
            }} title="Run selected cells">
              <svg width={13} height={13} viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            </button>
          )}
          {onClearOutputs && (
            <button className="outline-bulk-btn outline-bulk-icon" onClick={() => {
              onClearOutputs([...selectedCells])
            }} title="Clear outputs of selected cells">
              <IconEraser width={14} height={14} />
            </button>
          )}
          <button className="outline-bulk-btn outline-bulk-icon outline-bulk-delete" onClick={handleBulkDelete} title="Delete selected">
            <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      )}

      {/* Summary bar */}
      {stats.totalTime > 0 && !selectMode && (
        <div className="outline-summary">
          <span>Last run: {stats.executedCount} {stats.executedCount === 1 ? 'cell' : 'cells'} executed</span>
          <span>·</span>
          <span>Total: {formatTime(stats.totalTime)}</span>
          {stats.lastFinishedAt > 0 && <><span>·</span><span>{formatAgo(stats.lastFinishedAt)}</span></>}
        </div>
      )}

      {/* Run banner (Run All and any other run): click opens this notebook's Run
          History, "Go to first error" jumps to the failed cell, x dismisses, and it
          dismisses itself 10 s after it is first shown (paused on hover/focus). */}
      {showRunBanner && !selectMode && (() => {
        const t = bannerText(runBanner)
        return (
          <ActionBanner
            banner={{
              id: `run-${runBanner.stamp}`,
              tone: runBanner.tone,
              lead: `${t.lead},`,
              detail: `${t.detail}.`,
              action: t.action,
              secondary: t.secondary,
            }}
            onAction={openRunHistory}
            onSecondary={goToFirstError}
            onDismiss={() => onClearRunBanner?.(activeTab.id)}
            dismissLabel="Dismiss run summary"
          />
        )
      })()}
      {/* Type/status filter chips */}
      {!selectMode && (
        <FilterPills
          className="outline-filter-chips"
          ariaLabel="Filter cells by type"
          value={typeFilter}
          onChange={setTypeFilter}
          options={[
            { key: 'all', label: 'All' },
            { key: 'code', label: 'Code' },
            { key: 'sql', label: 'SQL' },
            { key: 'markdown', label: 'MD' },
            { key: 'errors', label: `Errors${stats.errorCount > 0 ? ` (${stats.errorCount})` : ''}`, className: stats.errorCount > 0 ? 'has-errors' : '' },
          ].map(chip => ({ ...chip, title: `Show ${chip.label}` }))}
        />
      )}

      <div className="outline-search">
        <input
          className="outline-search-input"
          type="text"
          placeholder="Jump to or search..."
          value={outlineSearch}
          onChange={(e) => setOutlineSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape' && outlineSearch) { e.preventDefault(); setOutlineSearch('') } }}
        />
        {outlineSearch && (
          <button
            className="outline-search-clear"
            onClick={() => setOutlineSearch('')}
            title="Clear search (Esc)"
            aria-label="Clear search"
          >
            <IconX width={12} height={12} />
          </button>
        )}
      </div>
      <div className="outline-body-wrap">
        <div className="sidebar-panel-body outline-list" ref={listRef} tabIndex={-1}>
          {(filteredOutline || cellOutlineItems).length === 0 && (
            cells.length === 0
              ? <PanelState
                  variant="empty"
                  icon={<IconNotebook width={24} height={24} />}
                  message="No cells in this notebook"
                  hint="Add a cell to see the outline here."
                />
              : <PanelState
                  variant="empty"
                  icon={<IconNotebook width={24} height={24} />}
                  message={typeFilter === 'errors' ? 'No cells with errors' : 'No matching cells'}
                />
          )}
          {/* Filter/search mode: flat list (no nesting) */}
          {filteredOutline && filteredOutline.map(item => renderItem(item))}
          {/* Normal mode: nested hierarchy — hide items under collapsed sections
              (unless in select mode, where everything stays visible for bulk ops) */}
          {!filteredOutline && cellOutlineItems
            .filter(item => selectMode || isItemVisible(item))
            .map(item => renderItem(item))}
        </div>
        {/* Density minimap on the right edge — hidden in select mode to keep the
            bulk-select UI uncluttered. */}
        {!selectMode && renderMinimap()}
      </div>

    </div>
  )
}
