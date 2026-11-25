import { useState, useEffect, useMemo, useRef } from 'react'

/**
 * useNotebookSearch — find-in-notebook (Cmd+F) state, match computation, and
 * navigation. Extracted from Notebook.jsx.
 *
 * @param {Array} cells - the notebook cells (searched by their `code`)
 * @param {Function} setActiveCellId - focus/scroll a cell when a match is selected
 *
 * Returns search state + setters, next/prev navigation, and the two derived
 * lookups the cell list consumes (searchMatchCellIds, searchActiveOccurrenceMap).
 */
export function useNotebookSearch(cells, setActiveCellId, enabled = true, updateCellCode = null) {
  const [showSearch, setShowSearch] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [replaceQuery, setReplaceQuery] = useState('')
  const [searchMatches, setSearchMatches] = useState([]) // [{cellId, pos}]
  const [searchActiveIdx, setSearchActiveIdx] = useState(0)
  const searchInputRef = useRef(null)

  // Always-current cells for the replace ops (which read a cell's latest code at
  // click time without adding `cells` to their dependency arrays).
  const cellsRef = useRef(cells)
  cellsRef.current = cells

  const scrollToCellId = (cellId) => {
    setTimeout(() => {
      const el = document.querySelector(`[data-cell-id="${cellId}"]`)
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 50)
  }

  // When notebook find is disabled (e.g. App view), make sure any open search
  // bar is closed — the browser's native Cmd+F then searches the rendered app.
  useEffect(() => {
    if (!enabled && showSearch) {
      setShowSearch(false)
      setSearchQuery('')
      setReplaceQuery('')
      setSearchMatches([])
    }
  }, [enabled])  // eslint-disable-line react-hooks/exhaustive-deps

  // Cmd/Ctrl+F and Cmd/Ctrl+R both open the find+replace bar (find and replace are
  // always shown together on one row). Only when enabled (notebook view); in App
  // view we let the browser's native find run.
  //
  // IMPORTANT: NEVER intercept when Shift is held, so Cmd+Shift+R (hard reload)
  // always reaches the browser. Previously `e.key === 'R'` (Shift makes the key
  // uppercase) matched here, so hard-refresh silently opened the find bar instead
  // of reloading. We also no longer hijack plain Cmd+R — overriding the universal
  // page-reload shortcut is surprising and low-value (Replace already lives in the
  // Cmd+F bar). Find is bound to Cmd+F only.
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (!enabled) return
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key === 'f') {
        e.preventDefault()
        setShowSearch(true)
        setTimeout(() => searchInputRef.current?.focus(), 50)
      }
      if (e.key === 'Escape' && showSearch) {
        setShowSearch(false)
        setSearchQuery('')
        setReplaceQuery('')
        setSearchMatches([])
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [showSearch, enabled])

  // Track the last query the matches were computed for, so we can tell a QUERY
  // change (→ reset to the first match) apart from a CELLS change caused by a
  // replace/edit (→ PRESERVE the active position). Without this, every replace
  // re-ran this effect and snapped back to match #1, losing the user's place.
  const lastQueryRef = useRef('')

  // Recompute matches when the query OR the cells change.
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchMatches([])
      setSearchActiveIdx(0)
      lastQueryRef.current = ''
      return
    }
    const q = searchQuery.toLowerCase()
    const matches = []
    cells.forEach(cell => {
      if (cell.code && cell.code.toLowerCase().includes(q)) {
        // Count occurrences in this cell
        let idx = 0
        const code = cell.code.toLowerCase()
        while ((idx = code.indexOf(q, idx)) !== -1) {
          matches.push({ cellId: cell.id, pos: idx })
          idx += q.length
        }
      }
    })
    setSearchMatches(matches)

    const queryChanged = lastQueryRef.current !== searchQuery
    lastQueryRef.current = searchQuery

    if (queryChanged) {
      // New search term → jump to the first match.
      setSearchActiveIdx(0)
      if (matches.length > 0) {
        setActiveCellId(matches[0].cellId)
        scrollToCellId(matches[0].cellId)
      }
    } else {
      // Cells changed (e.g. a replace removed an occurrence). Keep the active
      // index in place — the next match now occupies the vacated slot — but clamp
      // it so it never points past the end, and re-focus the (new) active match.
      setSearchActiveIdx(prev => {
        if (matches.length === 0) return 0
        const clamped = Math.min(prev, matches.length - 1)
        const m = matches[clamped]
        if (m) { setActiveCellId(m.cellId); scrollToCellId(m.cellId) }
        return clamped
      })
    }
  }, [searchQuery, cells])

  const searchNext = () => {
    if (searchMatches.length === 0) return
    const nextIdx = (searchActiveIdx + 1) % searchMatches.length
    setSearchActiveIdx(nextIdx)
    setActiveCellId(searchMatches[nextIdx].cellId)
    scrollToCellId(searchMatches[nextIdx].cellId)
  }

  const searchPrev = () => {
    if (searchMatches.length === 0) return
    const prevIdx = (searchActiveIdx - 1 + searchMatches.length) % searchMatches.length
    setSearchActiveIdx(prevIdx)
    setActiveCellId(searchMatches[prevIdx].cellId)
    scrollToCellId(searchMatches[prevIdx].cellId)
  }

  // Replace the currently-active match, then advance to the next one. String-level:
  // splices the replacement into the owning cell's code at the match position and
  // writes it back via updateCellCode. The matches-recompute effect (keyed on
  // `cells`) re-derives searchMatches after the edit lands, so positions stay
  // correct; we keep the active index in range so focus follows the next match.
  const replaceCurrent = () => {
    if (!updateCellCode || searchMatches.length === 0 || !searchQuery) return
    const match = searchMatches[searchActiveIdx]
    if (!match) return
    const cell = (cellsRef.current || []).find(c => c.id === match.cellId)
    if (!cell || typeof cell.code !== 'string') return
    // `match.pos` is an index into the lowercased code; it's identical to the index
    // in the original string (lowercasing doesn't change length/positions), so we
    // splice directly on the original code to preserve surrounding case.
    const before = cell.code.slice(0, match.pos)
    const after = cell.code.slice(match.pos + searchQuery.length)
    updateCellCode(match.cellId, before + replaceQuery + after)
    // Index management + re-scroll is handled by the recompute effect: because the
    // replaced occurrence is gone, the NEXT match slides into the current index, so
    // keeping the same index naturally advances to it (clamped when we hit the end).
  }

  // Replace EVERY match across all cells in one pass. Case-insensitive to mirror the
  // find semantics (find uses toLowerCase().includes). One updateCellCode per cell
  // that contains a match, so each is a single undoable edit + one persisted change.
  const replaceAll = () => {
    if (!updateCellCode || !searchQuery) return
    const q = searchQuery.toLowerCase()
    for (const cell of (cellsRef.current || [])) {
      if (!cell.code || typeof cell.code !== 'string') continue
      const lower = cell.code.toLowerCase()
      if (!lower.includes(q)) continue
      // Rebuild the string case-insensitively, preserving the parts between matches.
      let result = ''
      let i = 0
      while (true) {
        const hit = lower.indexOf(q, i)
        if (hit === -1) { result += cell.code.slice(i); break }
        result += cell.code.slice(i, hit) + replaceQuery
        i = hit + q.length
      }
      updateCellCode(cell.id, result)
    }
    setSearchActiveIdx(0)
  }

  // Pre-compute search match data for performance (avoids recalculating per-cell in JSX)
  const searchMatchCellIds = useMemo(() => {
    if (!showSearch || !searchQuery) return new Set()
    return new Set(searchMatches.map(m => m.cellId))
  }, [showSearch, searchQuery, searchMatches])

  const searchActiveOccurrenceMap = useMemo(() => {
    if (!showSearch || !searchQuery || searchMatches.length === 0) return {}
    const activeMatch = searchMatches[searchActiveIdx]
    if (!activeMatch) return {}
    // Count which occurrence within the active cell is highlighted
    let countInCell = 0
    for (let i = 0; i < searchActiveIdx; i++) {
      if (searchMatches[i].cellId === activeMatch.cellId) countInCell++
    }
    return { [activeMatch.cellId]: countInCell }
  }, [showSearch, searchQuery, searchMatches, searchActiveIdx])

  return {
    showSearch,
    setShowSearch,
    searchQuery,
    setSearchQuery,
    replaceQuery,
    setReplaceQuery,
    searchMatches,
    setSearchMatches,
    searchActiveIdx,
    searchInputRef,
    searchNext,
    searchPrev,
    replaceCurrent,
    replaceAll,
    searchMatchCellIds,
    searchActiveOccurrenceMap,
  }
}

