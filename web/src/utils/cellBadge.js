/**
 * Per-type cell badge helper — the SINGLE source of the PY-n / SQL-n / MD-n
 * labels shown on cells. Walking the cells in order and counting per type gives
 * each cell a stable badge ("PY-4", "SQL-2", "MD-1") that matches the cell's own
 * type badge, the Outline panel, the Variables panel provenance links, and the
 * export headers. Keep every consumer on this so the numbering never diverges.
 */

const PREFIX = { code: 'PY', sql: 'SQL', markdown: 'MD' }

/** Map of cell id (as String) → badge label, e.g. { "abc": "PY-4" }. */
export function cellBadgeMap(cells) {
  const counters = { code: 0, sql: 0, markdown: 0 }
  const map = new Map()
  for (const c of cells || []) {
    const t = c.type || 'code'
    counters[t] = (counters[t] || 0) + 1
    map.set(String(c.id), `${PREFIX[t] || 'PY'}-${counters[t]}`)
  }
  return map
}

/** Ordered list of badge labels aligned to the input cells array (by position). */
export function cellBadgeList(cells) {
  const counters = { code: 0, sql: 0, markdown: 0 }
  return (cells || []).map((c) => {
    const t = c.type || 'code'
    counters[t] = (counters[t] || 0) + 1
    return `${PREFIX[t] || 'PY'}-${counters[t]}`
  })
}
