// Pure helpers for the compact per-cell breakdown in the Notebooks panel's Run
// History (NotebooksPanel.jsx). Kept here so node tests can cover them.
//
// A run with many cells shows a "Top 10 / All" toggle. Top 10 = the 10 cells
// with the highest value for the active metric (Duration, CPU or Memory),
// highest first; ties keep notebook order. All = every cell in notebook order.
// Runs with 15 cells or fewer have no toggle and always show every cell.

export const TOP_N = 10
// The toggle appears only when a run has MORE than this many cells.
export const TOGGLE_THRESHOLD = 15
export const DEFAULT_CELL_VIEW = 'top'
// sessionStorage key: the choice is remembered for the browser session.
export const CELL_VIEW_STORAGE_KEY = 'microvm-rh-cell-view'

export function hasTopToggle(cellCount) {
  return Number.isFinite(cellCount) && cellCount > TOGGLE_THRESHOLD
}

// Stored value → 'top' | 'all' (anything unknown falls back to the default).
export function parseCellView(v) {
  return v === 'all' || v === 'top' ? v : DEFAULT_CELL_VIEW
}

// cells: run_cells in notebook order. valueOf(cell) → number for the active
// metric. view: 'top' | 'all'.
// isError(cell) (optional) → true for a failed cell.
// Returns { cells, toggle, total, hidden, hiddenErrors }:
//   cells        – the rows to render (Top 10 sorted desc, or all in notebook order)
//   toggle       – whether the Top 10 / All control is shown
//   total        – number of cells in the run
//   hidden       – cells not shown (0 unless Top 10 is active)
//   hiddenErrors – failed cells among the hidden ones, so the "+N more" line can
//                  say a failure is out of view (Top 10 ranks on the metric only)
export function selectRunCells(cells, valueOf, view, isError) {
  const list = Array.isArray(cells) ? cells : []
  const total = list.length
  const toggle = hasTopToggle(total)
  if (!toggle || parseCellView(view) === 'all') {
    return { cells: list, toggle, total, hidden: 0, hiddenErrors: 0 }
  }
  const num = (c) => {
    const v = Number(valueOf(c))
    return Number.isFinite(v) ? v : 0
  }
  const top = list
    .map((c, i) => ({ c, i, v: num(c) }))
    // Highest first; equal values keep notebook order (stable on index).
    .sort((a, b) => (b.v - a.v) || (a.i - b.i))
    .slice(0, TOP_N)
    .map(x => x.c)
  const shown = new Set(top)
  const hiddenErrors = typeof isError === 'function'
    ? list.filter(c => !shown.has(c) && isError(c)).length
    : 0
  return { cells: top, toggle, total, hidden: total - top.length, hiddenErrors }
}

// Tooltip text for a cell's @params: "Shipping market: UK · Min units sold: 0".
// When the single line would be long, one "key: value" per line instead. The
// text is never cut.
export const PARAM_TIP_LINE_MAX = 48
export function paramTipText(entries) {
  if (!Array.isArray(entries) || entries.length === 0) return ''
  const parts = entries.map(e => `${e.key}: ${e.value}`)
  const line = parts.join(' · ')
  return line.length <= PARAM_TIP_LINE_MAX ? line : parts.join('\n')
}
