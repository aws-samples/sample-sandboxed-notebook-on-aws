/**
 * chatCells — which notebook cells ride an AI-chat turn (the agent's
 * get_notebook_state reads them).
 *
 * Sending only the first 10 cells dropped the cell that renamed a column
 * (`reviews.rename(columns={'productId': 'product_id'})`), so the assistant wrote
 * code against the raw source column. This picks a bounded WINDOW instead:
 *   1. the active cell,
 *   2. cells around it (alternating outward, up to windowRadius),
 *   3. cells that WRITE a variable a window cell READS (from the stored DAG, or a
 *      regex fallback when there is none),
 *   4. the rest, nearest to the active cell first,
 * until maxCells or the character budget is reached. Cells come back in notebook
 * order, each tagged with its notebook position `index` (and `active: true` on the
 * active cell).
 *
 * Pure: no React, no network, no import.meta — unit-testable under plain Node
 * (web/src/services/__tests__/chatCells.test.mjs).
 */
import { isDagDoc } from './depGraph.js'

export const CHAT_CELL_DEFAULTS = Object.freeze({
  maxCells: 60,
  codeChars: 300,
  outputChars: 200,
  errorChars: 200,
  windowRadius: 15,
  budgetChars: 24000,
})

const WRITE_RE = /^\s*([A-Za-z_]\w*)\s*=(?!=)/gm
const IDENT_RE = /\b[A-Za-z_]\w*\b/g

function slice(cell, index, o) {
  return {
    index,
    type: cell?.type || 'code',
    code: String(cell?.code || '').slice(0, o.codeChars),
    output: String(cell?.output || '').slice(0, o.outputChars),
    error: String(cell?.error || '').slice(0, o.errorChars),
  }
}

const sizeOf = (c) => c.code.length + c.output.length + c.error.length

/** Map<cellIndex, {reads:Set, writes:Set}> from the DAG (by cell id) or a regex scan. */
function readsWrites(cells, dagDoc) {
  const out = new Map()
  if (isDagDoc(dagDoc)) {
    const byId = new Map()
    for (const n of dagDoc.nodes) {
      if (n && n.cell_id != null) byId.set(String(n.cell_id), n)
    }
    cells.forEach((c, i) => {
      const n = c && c.id != null ? byId.get(String(c.id)) : null
      if (n) {
        out.set(i, {
          reads: new Set(Array.isArray(n.reads) ? n.reads.map(String) : []),
          writes: new Set(Array.isArray(n.writes) ? n.writes.map(String) : []),
        })
      }
    })
    return out
  }
  cells.forEach((c, i) => {
    if ((c?.type || 'code') === 'markdown') return
    const code = String(c?.code || '')
    const writes = new Set([...code.matchAll(WRITE_RE)].map(m => m[1]))
    const reads = new Set(code.match(IDENT_RE) || [])
    out.set(i, { reads, writes })
  })
  return out
}

/**
 * @param {Array} cells      the tab's cells ({id, type, code, output, error})
 * @param {number|null} activeIndex  the active cell's position (null → 0)
 * @param {object|null} dagDoc       the stored DAG document (optional)
 * @param {object} [opts]            overrides for CHAT_CELL_DEFAULTS
 * @returns {Array<{index, type, code, output, error, active?}>} in notebook order
 */
export function selectChatCells(cells, activeIndex = null, dagDoc = null, opts = {}) {
  const o = { ...CHAT_CELL_DEFAULTS, ...(opts || {}) }
  const list = Array.isArray(cells) ? cells : []
  if (!list.length) return []
  const active = Number.isInteger(activeIndex) && activeIndex >= 0 && activeIndex < list.length
    ? activeIndex : 0
  const sliced = list.map((c, i) => slice(c, i, o))
  sliced[active].active = true

  const total = sliced.reduce((n, c) => n + sizeOf(c), 0)
  if (list.length <= o.maxCells && total <= o.budgetChars) return sliced

  const chosen = new Set()
  let used = 0
  const take = (i) => {
    if (chosen.has(i) || i < 0 || i >= list.length || chosen.size >= o.maxCells) return
    const sz = sizeOf(sliced[i])
    // The active cell always rides; others only while the budget allows.
    if (chosen.size > 0 && used + sz > o.budgetChars) return
    chosen.add(i)
    used += sz
  }

  // 1. active cell, 2. the window around it (alternating outward).
  take(active)
  const windowIdx = [active]
  for (let d = 1; d <= o.windowRadius; d++) {
    for (const i of [active - d, active + d]) {
      if (i >= 0 && i < list.length) {
        windowIdx.push(i)
        take(i)
      }
    }
  }

  // 3. writers of variables the window reads (nearest writer first).
  const rw = readsWrites(list, dagDoc)
  const windowSet = new Set(windowIdx)
  const needed = new Set()
  for (const i of windowIdx) {
    for (const v of rw.get(i)?.reads || []) needed.add(v)
  }
  const writers = []
  rw.forEach((info, i) => {
    if (windowSet.has(i)) return
    for (const v of info.writes) {
      if (needed.has(v)) { writers.push(i); break }
    }
  })
  writers.sort((a, b) => Math.abs(a - active) - Math.abs(b - active) || a - b)
  for (const i of writers) take(i)

  // 4. everything else, nearest to the active cell first.
  const rest = sliced.map((_, i) => i).filter(i => !chosen.has(i))
  rest.sort((a, b) => Math.abs(a - active) - Math.abs(b - active) || a - b)
  for (const i of rest) take(i)

  return [...chosen].sort((a, b) => a - b).map(i => sliced[i])
}
