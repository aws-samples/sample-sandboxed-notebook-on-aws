// Whole-cell collapse (pure, node-testable): the one-row strip a collapsed cell
// shows instead of its code, output, variables and notes.
//
// The collapsed/expanded choice is kept for the page session in a module-level
// set keyed by cell id (same approach as services/outputExpansion.js). It
// survives re-renders, notebook-tab switches and Notebook ↔ App view switches
// (all of which remount the cells), and is never written to the notebook, so
// the saved notebook JSON is unchanged. A reload expands everything again.
import { cellOutputItems } from './cellOutputs.js'

const collapsedIds = new Set()

/** True when the cell with this id is collapsed (page session). */
export function isCellCollapsed(cellId) {
  return cellId != null && collapsedIds.has(String(cellId))
}

/** Record the collapsed state of a cell for the page session. */
export function setCellCollapsed(cellId, collapsed) {
  if (cellId == null) return
  if (collapsed) collapsedIds.add(String(cellId))
  else collapsedIds.delete(String(cellId))
}

/** Test hook: forget every stored state. */
export function _resetCellCollapse() {
  collapsedIds.clear()
}

/** Number of lines of the cell source (0 for an empty cell). */
export function cellLineCount(code) {
  const s = String(code || '')
  if (!s.trim()) return 0
  return s.replace(/\n+$/, '').split('\n').length
}

/** The pill text: "1 line", "14 lines", or "Empty". */
export function lineCountLabel(code) {
  const n = cellLineCount(code)
  if (n === 0) return 'Empty'
  return `${n} ${n === 1 ? 'line' : 'lines'}`
}

/**
 * The one-line preview shown on the strip: the first non-blank source line.
 * Markdown cells drop the heading hashes and the commonest inline markup so a
 * "# Title" reads "Title". CSS ellipsizes it; nothing is cut here.
 */
export function collapsedPreview(cell) {
  const code = String(cell?.code || '')
  const first = code.split('\n').find(l => l.trim()) || ''
  if (cell?.type === 'markdown') {
    return first
      .replace(/^\s*#{1,6}\s+/, '')
      .replace(/^\s*(?:[-*+]|\d+\.)\s+/, '')
      .replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, '$1$2')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .trim()
  }
  return first.trimEnd()
}

const plural = (n, one, many) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

function tableSummary(p) {
  const rows = Number.isFinite(p?.total_rows) ? p.total_rows : (p?.cells || []).length
  const cols = Number.isFinite(p?.total_cols) ? p.total_cols : (p?.columns || []).length
  return `${plural(rows, 'row', 'rows')} × ${plural(cols, 'column', 'columns')}`
}

/**
 * The compact status / output hint on the right of the strip, or null.
 *   running      → { tone: 'running', text: 'Running…' }
 *   error        → { tone: 'error', text: <first line of the error> }
 *   interrupted  → { tone: 'warning', text: 'Interrupted' }
 *   output       → { tone: 'neutral', text } — one table: "100 rows × 8 columns";
 *                  several rich outputs: "2 tables · 1 chart"; one chart: "Chart";
 *                  one image: "Image"; text only: its first non-blank line.
 * Markdown cells have no run state and get null.
 */
export function collapsedStatusHint(cell) {
  if (!cell || cell.type === 'markdown') return null
  if (cell.status === 'running') return { tone: 'running', text: 'Running…' }
  if (cell.error) {
    const line = String(cell.error).split('\n').find(l => l.trim()) || 'Error'
    return { tone: 'error', text: line.trim() }
  }
  if (cell.status === 'interrupted') return { tone: 'warning', text: 'Interrupted' }
  let items
  try { items = cellOutputItems(cell) } catch { items = [] }
  const rich = items.filter(it => it.t === 'table' || it.t === 'plotly' || it.t === 'image')
  if (rich.length === 1) {
    const it = rich[0]
    if (it.t === 'table') return { tone: 'neutral', text: tableSummary(it.payload) }
    return { tone: 'neutral', text: it.t === 'plotly' ? 'Chart' : 'Image' }
  }
  if (rich.length > 1) {
    const c = { table: 0, plotly: 0, image: 0 }
    for (const it of rich) c[it.t] += 1
    const parts = []
    if (c.table) parts.push(plural(c.table, 'table', 'tables'))
    if (c.plotly) parts.push(plural(c.plotly, 'chart', 'charts'))
    if (c.image) parts.push(plural(c.image, 'image', 'images'))
    return { tone: 'neutral', text: parts.join(' · ') }
  }
  const text = items.find(it => it.t === 'text')
  if (text) {
    const line = text.text.split('\n').find(l => l.trim())
    if (line) return { tone: 'neutral', text: line.trim() }
  }
  return null
}
