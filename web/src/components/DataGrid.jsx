import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { formatCell, NULL_TOKENS } from '../services/dfCellFormat'
import { TABLE_ROWS_PER_PAGE, TABLE_PAGE_SIZE_OPTIONS } from '../constants'
import { detectType, dtypeToType, typeBadge, computeColumnStats, fmtNum } from '../services/dfStats'
import DataChart from './DataChart'

/** RFC-4180 CSV: quote fields containing comma, quote, CR or LF; escape " as "". */
function toCsv(columns, rows) {
  const esc = (v) => {
    const s = String(v ?? '')
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [columns.map(esc).join(',')]
  for (const row of rows) lines.push(row.map(esc).join(','))
  return lines.join('\r\n')
}

/**
 * DataGrid — renders a DataFrame from the VM's structured JSON payload
 * ({ columns, cells, dtypes, total_rows, shown_rows, total_cols, shown_cols }).
 *
 * Features (all client-side over the already-received rows):
 *  - Per-cell formatting via formatCell (ported _linkify_urls): links, images,
 *    number grouping, NaN/bool badges, long-text truncation.
 *  - Click-to-sort by column (type-aware: number/date/string), 3-state toggle.
 *  - Global search box (filters across all cells, case-insensitive).
 *  - Pagination with a page-size selector + "Showing X–Y of Z".
 *  - Sticky header + scrollable body (~15-row viewport via .df-table-scroll).
 *  - Cap note when the VM truncated rows/cols (first N of M).
 */

// An image cell that falls back to a plain link (the URL) if the image fails to
// load — avoids a broken-image icon with no context for dead/blocked image URLs.
function ImageCell({ url }) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    const text = url.length <= 60 ? url : url.slice(0, 57) + '...'
    return <a href={url} target="_blank" rel="noopener noreferrer">{text}</a>
  }
  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      <img src={url} className="df-cell-img" alt="img" onError={() => setFailed(true)} />
    </a>
  )
}

function CellContent({ raw }) {
  const f = formatCell(raw)
  switch (f.kind) {
    case 'null':
      return <span className="df-cell-null">{f.text}</span>
    case 'bool':
      return <span className={`df-cell-bool ${f.value ? 'df-bool-true' : 'df-bool-false'}`}>{f.text}</span>
    case 'img':
      return <ImageCell url={f.url} />
    case 'link':
      return <a href={f.url} target="_blank" rel="noopener noreferrer">{f.text}</a>
    case 'email':
      return <a href={`mailto:${f.email}`}>{f.email}</a>
    case 'number':
      return <span className={f.negative ? 'df-cell-negative' : undefined}>{f.text}</span>
    default:
      return <span title={f.title}>{f.text}</span>
  }
}

// Column type detection (detectType/dtypeToType) now lives in services/dfStats so
// the grid, the header type badges, the column-summary popover, and the chart all
// agree on one definition. dtypeToType is the former local dtypeToSortType.

export default function DataGrid({ payload }) {
  const { columns = [], cells = [], dtypes = [], total_rows = 0, shown_rows = 0, total_cols = 0, shown_cols = 0 } = payload || {}

  // Trust the server dtypes only when the array lines up 1:1 with columns; a length
  // mismatch (or absent dtypes) means we sample cell values to infer the sort type.
  const dtypesUsable = Array.isArray(dtypes) && dtypes.length === columns.length

  // Effective type per column ('number' | 'date' | 'string'). Prefer the server
  // dtype; fall back to sampling the column's values (same logic the sort uses, so
  // alignment, badges, sort, and the chart all agree). Cheap (≤20 samples/col).
  const colTypes = useMemo(() => {
    return columns.map((_, c) => {
      const serverType = dtypesUsable ? dtypeToType(dtypes[c]) : null
      return serverType || detectType(cells.map(r => String(r[c] ?? '')))
    })
  }, [columns, cells, dtypes, dtypesUsable])

  // Numeric columns → RIGHT-ALIGN (+ tabular figures) so decimals line up and
  // magnitudes are scannable, the way a polished data table renders numbers.
  const numericCols = useMemo(() => colTypes.map(t => t === 'number'), [colTypes])

  // Default per-column pixel widths, used to render the table with table-layout:
  // fixed from the START. This is what makes wrapping + the max-width cap actually
  // work: in table-layout:auto the browser sizes columns to content and ignores
  // cell max-width/wrapping (so a long text column just goes wide and the table
  // scrolls — the bug where "Wrap" appeared to do nothing). We estimate each
  // column's natural width from its header + a sample of cell lengths, then CLAMP
  // to [MIN, MAX]. Fixed layout then honors these, wraps overflow within the
  // column, and drag-resize overrides them per column.
  const DEFAULT_COL_MIN = 80
  const DEFAULT_COL_MAX = 340
  const CH_PX = 7.3  // ~avg monospace-ish char width at our font size
  const defaultColWidths = useMemo(() => {
    return columns.map((h, c) => {
      // Sample up to 40 cell values for a cheap "typical longest" estimate.
      let maxChars = String(h ?? '').length + 4  // header + room for badge/caret
      const step = Math.max(1, Math.floor(cells.length / 40))
      for (let r = 0; r < cells.length; r += step) {
        const len = String(cells[r]?.[c] ?? '').length
        if (len > maxChars) maxChars = len
      }
      const px = Math.round(maxChars * CH_PX) + 24  // + cell padding
      return Math.max(DEFAULT_COL_MIN, Math.min(DEFAULT_COL_MAX, px))
    })
  }, [columns, cells])

  const [sortCol, setSortCol] = useState(null)
  const [sortDir, setSortDir] = useState(null)  // 'asc' | 'desc' | null
  const [query, setQuery] = useState('')
  const [pageSize, setPageSize] = useState(TABLE_ROWS_PER_PAGE)
  const [page, setPage] = useState(0)
  const [copied, setCopied] = useState(false)
  const [view, setView] = useState('table')       // 'table' | 'chart'
  const [statsCol, setStatsCol] = useState(null)   // column index whose summary popover is open
  const [statsAnchor, setStatsAnchor] = useState(null)  // viewport rect of the clicked badge (for the portal popover position)
  const [chartCopied, setChartCopied] = useState(false)
  // Wrap vs. single-line cell text (toolbar toggle). Default OFF — each cell on one
  // line, clipped with an ellipsis, for a compact one-row-per-record scan (the
  // natural default row height). Turn ON to wrap long values within the column.
  const [wrapText, setWrapText] = useState(false)
  const gridRef = useRef(null)
  const chartSvgRef = useRef(null)
  // Per-column pixel widths for drag-to-resize (index -> px). Empty = auto width.
  // A <colgroup> applies these so the whole column (header + body) resizes together.
  const [colWidths, setColWidths] = useState({})
  const resizeRef = useRef(null)  // { index, startX, startWidth } while dragging

  // Drag-to-resize a column: pointer handlers on the little grip in each <th>.
  // Uses window listeners during the drag so it keeps tracking outside the header,
  // and setPointerCapture-free (window move/up) so it survives fast drags.
  const startResize = useCallback((e, index) => {
    e.preventDefault()
    e.stopPropagation()  // don't trigger the header's click-to-sort
    const th = e.currentTarget.closest('th')
    // The table is always fixed-layout with explicit widths, so the current
    // rendered header width is this column's starting width. Drag adjusts just it;
    // all other columns keep their (default or previously-set) widths.
    const startWidth = th ? th.getBoundingClientRect().width : 120
    resizeRef.current = { index, startX: e.clientX, startWidth }

    const onMove = (ev) => {
      const r = resizeRef.current
      if (!r) return
      const next = Math.max(48, Math.round(r.startWidth + (ev.clientX - r.startX)))
      setColWidths(prev => (prev[r.index] === next ? prev : { ...prev, [r.index]: next }))
    }
    const onUp = () => {
      resizeRef.current = null
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'  // no text selection while dragging
  }, [])

  // Double-click a grip to reset that column back to its computed default width.
  const resetColWidth = useCallback((e, index) => {
    e.preventDefault(); e.stopPropagation()
    setColWidths(prev => {
      if (!(index in prev)) return prev
      const next = { ...prev }; delete next[index]; return next
    })
  }, [])

  // Close the column-summary popover on outside click or Esc.
  const closeStats = useCallback(() => { setStatsCol(null); setStatsAnchor(null) }, [])
  useEffect(() => {
    if (statsCol === null) return
    const onDown = (e) => {
      if (!e.target.closest('.df-col-popover') && !e.target.closest('.df-col-badge')) {
        closeStats()
      }
    }
    const onKey = (e) => { if (e.key === 'Escape') closeStats() }
    // The popover is portaled to <body> with FIXED positioning (so the table's
    // overflow:auto can't clip it — the bug this fixes). A fixed element doesn't
    // move with scroll, so close on any scroll/resize rather than let it drift.
    // Capture-phase scroll listener catches scrolling inside the table too.
    const onReflow = () => closeStats()
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onReflow, true)
    window.addEventListener('resize', onReflow)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onReflow, true)
      window.removeEventListener('resize', onReflow)
    }
  }, [statsCol, closeStats])

  // 1. Filter (search across all cells)
  const filtered = useMemo(() => {
    if (!query.trim()) return cells
    const q = query.trim().toLowerCase()
    return cells.filter(row => row.some(v => String(v ?? '').toLowerCase().includes(q)))
  }, [cells, query])

  // 2. Sort (over the filtered set)
  const sorted = useMemo(() => {
    if (sortCol === null || sortDir === null) return filtered
    const rows = [...filtered]
    // Prefer the server-sent dtype for this column; fall back to value sampling when
    // the dtype is missing, unusable, or an object/string/category we can't trust.
    const type = colTypes[sortCol] || detectType(rows.map(r => String(r[sortCol] ?? '')))
    rows.sort((a, b) => {
      const va = String(a[sortCol] ?? '')
      const vb = String(b[sortCol] ?? '')
      const aEmpty = !va || NULL_TOKENS.has(va)
      const bEmpty = !vb || NULL_TOKENS.has(vb)
      if (aEmpty && bEmpty) return 0
      if (aEmpty) return 1
      if (bEmpty) return -1
      let cmp = 0
      if (type === 'number') cmp = parseFloat(va.replace(/[$,€£%\s]/g, '')) - parseFloat(vb.replace(/[$,€£%\s]/g, ''))
      else if (type === 'date') cmp = new Date(va) - new Date(vb)
      else cmp = va.localeCompare(vb, undefined, { numeric: true, sensitivity: 'base' })
      // A comparator returning NaN (unparseable number/date) yields implementation-
      // defined ordering — fall back to a stable string compare in that case.
      if (Number.isNaN(cmp)) cmp = va.localeCompare(vb, undefined, { numeric: true, sensitivity: 'base' })
      return sortDir === 'desc' ? -cmp : cmp
    })
    return rows
  }, [filtered, sortCol, sortDir, colTypes])

  // 3. Paginate
  const totalFiltered = sorted.length
  const pageCount = Math.max(1, Math.ceil(totalFiltered / pageSize))
  const safePage = Math.min(page, pageCount - 1)
  const start = safePage * pageSize
  const pageRows = sorted.slice(start, start + pageSize)

  const handleHeaderClick = (colIdx) => {
    if (sortCol !== colIdx) { setSortCol(colIdx); setSortDir('asc') }
    else if (sortDir === 'asc') setSortDir('desc')
    else { setSortCol(null); setSortDir(null) }
    setPage(0)
  }

  const onSearch = (e) => { setQuery(e.target.value); setPage(0) }
  const onPageSize = (e) => { setPageSize(Number(e.target.value)); setPage(0) }

  // Copy the current view (filtered + sorted, all pages) as CSV. Uses the raw
  // cell strings so the clipboard holds plain data, not the display markup.
  const onCopyCsv = useCallback(async () => {
    const csv = toCsv(columns, sorted)
    try {
      await navigator.clipboard.writeText(csv)
    } catch {
      // Fallback for non-secure contexts where the async clipboard API is blocked.
      const ta = document.createElement('textarea')
      ta.value = csv
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch { /* ignore */ }
      document.body.removeChild(ta)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [columns, sorted])

  // Copy the current chart as a PNG image to the clipboard. The live SVG styles
  // come from CSS classes (var()-driven fills/strokes) that won't resolve once the
  // node is detached, so we CLONE it and inline each element's computed paint +
  // font styles, add a solid background (transparent PNG on a dark app looks
  // broken when pasted elsewhere), rasterize via an <img>+canvas, then write an
  // image/png ClipboardItem. Best-effort with a clear failure signal.
  const onCopyChart = useCallback(async () => {
    const svg = chartSvgRef.current
    if (!svg) return
    try {
      const w = svg.width?.baseVal?.value || 720
      const h = svg.height?.baseVal?.value || 320
      const clone = svg.cloneNode(true)

      // Inline computed styles for every element so the standalone SVG keeps its
      // themed colors/fonts (classes + CSS vars don't travel with the node).
      const srcEls = svg.querySelectorAll('*')
      const dstEls = clone.querySelectorAll('*')
      const PROPS = ['fill', 'stroke', 'stroke-width', 'opacity', 'font-size', 'font-weight', 'font-family', 'text-anchor', 'dominant-baseline']
      srcEls.forEach((el, i) => {
        const cs = window.getComputedStyle(el)
        const d = dstEls[i]
        if (!d) return
        let style = ''
        for (const p of PROPS) {
          const v = cs.getPropertyValue(p)
          if (v) style += `${p}:${v};`
        }
        d.setAttribute('style', style)
      })
      clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
      clone.setAttribute('width', w)
      clone.setAttribute('height', h)

      // Solid background matching the chart surface.
      const bg = window.getComputedStyle(svg.closest('.df-chart') || svg).backgroundColor || '#0b1020'
      const svgStr = new XMLSerializer().serializeToString(clone)
      const svg64 = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svgStr)}`

      const scale = 2  // crisp on hi-dpi
      const canvas = document.createElement('canvas')
      canvas.width = w * scale
      canvas.height = h * scale
      const ctx = canvas.getContext('2d')
      ctx.scale(scale, scale)
      ctx.fillStyle = bg && bg !== 'rgba(0, 0, 0, 0)' ? bg : '#0b1020'
      ctx.fillRect(0, 0, w, h)

      await new Promise((resolve, reject) => {
        const img = new Image()
        img.onload = () => { ctx.drawImage(img, 0, 0, w, h); resolve() }
        img.onerror = reject
        img.src = svg64
      })

      const blob = await new Promise((res) => canvas.toBlob(res, 'image/png'))
      if (!blob) throw new Error('toBlob returned null')
      await navigator.clipboard.write([new window.ClipboardItem({ 'image/png': blob })])
      setChartCopied(true)
      setTimeout(() => setChartCopied(false), 1500)
    } catch (err) {
      console.warn('[DataGrid] Copy chart failed:', err)
      // Surface a brief failure so the user knows to retry / that the browser
      // blocked image clipboard (e.g. non-secure context or unsupported browser).
      setChartCopied('error')
      setTimeout(() => setChartCopied(false), 2000)
    }
  }, [])

  if (columns.length === 0) {
    return <div className="df-grid-empty">No columns to display.</div>
  }

  const rangeStart = totalFiltered === 0 ? 0 : start + 1
  const rangeEnd = Math.min(start + pageSize, totalFiltered)

  return (
    <div className="df-grid" ref={gridRef}>
      {/* Toolbar: search + page-size */}
      <div className="df-grid-toolbar">
        <input
          className="df-grid-search"
          type="text"
          placeholder="Search…"
          value={query}
          onChange={onSearch}
        />
        <div className="df-grid-toolbar-right">
          {/* Table / Chart view toggle. Chart renders over the current filtered+
              sorted rows, so search still applies in chart view. */}
          <div className="df-view-toggle" role="tablist" aria-label="View">
            <button
              type="button"
              role="tab"
              aria-selected={view === 'table'}
              className={`df-view-btn${view === 'table' ? ' active' : ''}`}
              onClick={() => setView('table')}
            >Table</button>
            <button
              type="button"
              role="tab"
              aria-selected={view === 'chart'}
              className={`df-view-btn${view === 'chart' ? ' active' : ''}`}
              onClick={() => { setView('chart'); setStatsCol(null) }}
            >Chart</button>
          </div>
          {view === 'chart' ? (
            <button
              type="button"
              className="cell-output-action-btn df-grid-copy"
              onClick={onCopyChart}
              title="Copy the chart as a PNG image"
            >
              {chartCopied === 'error' ? (
                <>
                  <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> Copy failed
                </>
              ) : chartCopied ? (
                <>
                  <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Copied
                </>
              ) : (
                <>
                  <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg> Copy Chart
                </>
              )}
            </button>
          ) : (
            <button
              type="button"
              className="cell-output-action-btn df-grid-copy"
              onClick={onCopyCsv}
              title={query.trim() ? 'Copy the filtered rows as CSV' : 'Copy all shown rows as CSV'}
            >
              {copied ? (
                <>
                  <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Copied
                </>
              ) : (
                <>
                  <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> Copy CSV
                </>
              )}
            </button>
          )}
          {view === 'table' && (
            <label className="df-grid-pagesize">
              Rows
              <select value={pageSize} onChange={onPageSize}>
                {TABLE_PAGE_SIZE_OPTIONS.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          )}
          {view === 'table' && (
            /* Wrap-text toggle: ON = cells wrap within the column (multi-line);
               OFF = each cell on one line, clipped with an ellipsis (compact scan). */
            <button
              type="button"
              className={`cell-output-action-btn df-grid-wrap${wrapText ? ' active' : ''}`}
              onClick={() => setWrapText(w => !w)}
              aria-pressed={wrapText}
              title={wrapText ? 'Wrap text: on (click for single-line rows)' : 'Wrap text: off (click to wrap long cells)'}
            >
              <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="3" y1="6" x2="21" y2="6"/>
                <path d="M3 12h15a3 3 0 0 1 0 6h-4"/>
                <polyline points="16 16 14 18 16 20"/>
                <line x1="3" y1="18" x2="10" y2="18"/>
              </svg>
              Wrap
            </button>
          )}
        </div>
      </div>

      {view === 'chart' ? (
        <DataChart columns={columns} rows={sorted} dtypes={dtypes} svgRef={chartSvgRef} />
      ) : (
      <>
      <div className="output-html df-table-scroll">
        {(() => {
          // Effective width per column = the user's dragged width if set, else the
          // computed default. The table is ALWAYS table-layout:fixed (df-table-fixed)
          // with an explicit total width = sum of these, so wrapping + the cap work
          // and drag-resize is unambiguous in both directions. This replaces the
          // old auto-layout default (which ignored cell max-width and never wrapped).
          const effWidths = columns.map((_, i) =>
            typeof colWidths[i] === 'number' ? colWidths[i] : defaultColWidths[i])
          const totalW = effWidths.reduce((s, w) => s + w, 0)
          return (
        <table
          className={`df-table-fixed${wrapText ? '' : ' df-nowrap'}`}
          style={{ width: `${totalW}px` }}
        >
          {/* colgroup carries each column's effective width so header + body cells
              in a column size together under the fixed layout. */}
          <colgroup>
            {effWidths.map((w, i) => (
              <col key={i} style={{ width: `${w}px` }} />
            ))}
          </colgroup>
          <thead>
            <tr>
              {columns.map((h, i) => {
                const badge = typeBadge(colTypes[i])
                return (
                <th
                  key={i}
                  onClick={() => handleHeaderClick(i)}
                  data-sort={sortCol === i ? sortDir : undefined}
                  data-numeric={numericCols[i] ? '' : undefined}
                >
                  <span className="df-th-inner">
                    {/* Type badge — click opens the column summary popover; the
                        stopPropagation keeps it from also toggling the sort. */}
                    <button
                      type="button"
                      className={`df-col-badge df-col-badge-${colTypes[i]}`}
                      title={`${badge.title} — click for column stats`}
                      aria-label={`${badge.title} — column stats`}
                      onClick={(e) => {
                        e.stopPropagation()
                        if (statsCol === i) { closeStats(); return }
                        // Anchor the portaled popover to this badge's viewport rect.
                        setStatsAnchor(e.currentTarget.getBoundingClientRect())
                        setStatsCol(i)
                      }}
                    >{badge.glyph}</button>
                    <span className="df-th-label" title={h}>{h}</span>
                  </span>
                  {/* Drag-to-resize grip on the column's right edge. Drag to size,
                      double-click to reset to auto. pointerDown stops the sort click. */}
                  <span
                    className="df-col-resizer"
                    onPointerDown={(e) => startResize(e, i)}
                    onDoubleClick={(e) => resetColWidth(e, i)}
                    onClick={(e) => e.stopPropagation()}
                    title="Drag to resize · double-click to reset"
                  />
                </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row, rIdx) => (
              <tr key={start + rIdx}>
                {row.map((val, cIdx) => (
                  <td key={cIdx} data-numeric={numericCols[cIdx] ? '' : undefined}>
                    {/* Cell content lives in a fixed-width block div, NOT relying
                        on table-layout for its width. This makes wrap/nowrap
                        deterministic regardless of any table-layout quirks: the div
                        is exactly the column width and wraps within it (wrap on) or
                        clips to one ellipsis'd line (wrap off). */}
                    <div className="df-cell-inner" style={{ width: `${effWidths[cIdx]}px` }}>
                      <CellContent raw={val} />
                    </div>
                  </td>
                ))}
              </tr>
            ))}
            {pageRows.length === 0 && (
              <tr><td className="df-grid-noresults" colSpan={columns.length}>No matching rows.</td></tr>
            )}
          </tbody>
        </table>
          )
        })()}
      </div>

      {/* Footer: range + pager */}
      <div className="df-grid-footer">
        <div className="df-grid-range">
          {(() => {
            const base = query.trim()
              ? `Showing ${rangeStart}–${rangeEnd} of ${totalFiltered} matches`
              : `Showing ${rangeStart}–${rangeEnd} of ${shown_rows.toLocaleString()} rows`
            const parts = []
            if (total_rows > shown_rows) parts.push(`${total_rows.toLocaleString()} total`)
            if (total_cols > shown_cols) parts.push(`${shown_cols} of ${total_cols} cols`)
            return parts.length ? `${base} (${parts.join(', ')})` : base
          })()}
        </div>
        {pageCount > 1 && (
          <div className="df-grid-pager">
            <button onClick={() => setPage(0)} disabled={safePage === 0} title="First">«</button>
            <button onClick={() => setPage(safePage - 1)} disabled={safePage === 0} title="Previous">‹</button>
            <span className="df-grid-pageinfo">Page {safePage + 1} of {pageCount}</span>
            <button onClick={() => setPage(safePage + 1)} disabled={safePage >= pageCount - 1} title="Next">›</button>
            <button onClick={() => setPage(pageCount - 1)} disabled={safePage >= pageCount - 1} title="Last">»</button>
          </div>
        )}
      </div>
      </>
      )}
      {/* Column-stats popover, PORTALED to <body> with fixed positioning so the
          table's overflow:auto scroll box can never clip it (the clipping bug on
          short tables). Positioned from the clicked badge's viewport rect. */}
      {statsCol !== null && statsAnchor && createPortal(
        <ColumnSummary
          name={columns[statsCol]}
          type={colTypes[statsCol]}
          rows={sorted}
          colIdx={statsCol}
          total_rows={total_rows}
          shown_rows={shown_rows}
          anchor={statsAnchor}
        />,
        document.body,
      )}
    </div>
  )
}

// ─── Column summary popover (Tier 1) ────────────────────────────────────────
// Anchored under a column's type badge. Stats are computed over the CURRENT rows
// (filtered + sorted = what's shown), labeled accordingly when the payload is
// capped. Numeric columns get min/max/mean/median; others get top values.
function ColumnSummary({ name, type, rows, colIdx, total_rows, shown_rows, anchor }) {
  const stats = useMemo(() => computeColumnStats(rows, colIdx, type), [rows, colIdx, type])
  const capped = total_rows > shown_rows
  // Fixed position computed from the anchor (badge) rect. Starts just below the
  // badge; after mount we measure the popover and flip up / clamp horizontally so
  // it stays fully on-screen regardless of how short the table is or where the
  // column sits. `anchor` is a DOMRect in viewport coords (position: fixed space).
  const popRef = useRef(null)
  const [pos, setPos] = useState(() =>
    anchor ? { top: Math.round(anchor.bottom + 4), left: Math.round(anchor.left), visibility: 'hidden' } : null
  )
  useEffect(() => {
    if (!anchor || !popRef.current) return
    const el = popRef.current
    const w = el.offsetWidth
    const h = el.offsetHeight
    const margin = 8
    const vw = window.innerWidth
    const vh = window.innerHeight
    // Horizontal: prefer left-aligned to the badge; if it would overflow the right
    // edge, shift left so the whole popover fits (min margin from the left edge).
    let left = anchor.left
    if (left + w + margin > vw) left = Math.max(margin, vw - w - margin)
    left = Math.max(margin, left)
    // Vertical: prefer below the badge; if it would overflow the bottom, flip above.
    let top = anchor.bottom + 4
    if (top + h + margin > vh && anchor.top - h - 4 >= margin) top = anchor.top - h - 4
    top = Math.max(margin, Math.min(top, vh - h - margin))
    setPos({ top: Math.round(top), left: Math.round(left), visibility: 'visible' })
  }, [anchor])
  return (
    <div
      ref={popRef}
      className="df-col-popover df-col-popover-portal"
      role="dialog"
      aria-label={`Stats for ${name}`}
      onClick={(e) => e.stopPropagation()}
      style={pos ? { top: pos.top, left: pos.left, visibility: pos.visibility } : undefined}
    >
      <div className="df-col-popover-head">
        <span className="df-col-popover-name" title={name}>{name}</span>
        <span className={`df-col-popover-type df-col-badge-${type}`}>{type}</span>
      </div>
      <dl className="df-col-popover-stats">
        <div><dt>Rows</dt><dd>{stats.total.toLocaleString()}</dd></div>
        <div><dt>Non-null</dt><dd>{stats.nonNull.toLocaleString()}</dd></div>
        <div><dt>Nulls</dt><dd>{stats.nulls.toLocaleString()}</dd></div>
        <div><dt>Unique</dt><dd>{stats.unique.toLocaleString()}</dd></div>
        {stats.numeric && (
          <>
            <div><dt>Min</dt><dd>{fmtNum(stats.numeric.min)}</dd></div>
            <div><dt>Max</dt><dd>{fmtNum(stats.numeric.max)}</dd></div>
            <div><dt>Mean</dt><dd>{fmtNum(stats.numeric.mean)}</dd></div>
            <div><dt>Median</dt><dd>{fmtNum(stats.numeric.median)}</dd></div>
          </>
        )}
      </dl>
      {stats.top && stats.top.length > 0 && (
        <div className="df-col-popover-top">
          <div className="df-col-popover-top-title">Top values</div>
          <ul>
            {stats.top.map((t, i) => (
              <li key={i}>
                <span className="df-col-top-val" title={t.value}>{t.value === '' ? '(empty)' : t.value}</span>
                <span className="df-col-top-count">{t.count.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {capped && (
        <div className="df-col-popover-note">Over the {shown_rows.toLocaleString()} shown rows (of {total_rows.toLocaleString()}).</div>
      )}
    </div>
  )
}
