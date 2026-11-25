// DataChart data preparation. Pure functions (no React / DOM) so they can be
// unit-checked with Node.
//
// Bar / Line used to plot the first N rows in row order and use X only as the tick
// LABEL — so changing X just relabelled the same shape. They now GROUP BY the X
// value over ALL shown rows and AGGREGATE Y (sum / mean / count / min / max), then
// order the groups by X (numbers / dates ascending, text in natural order) — or,
// for bars over text categories, by the aggregated value (largest first) so the
// top categories survive the bar cap.

import { parseNumeric } from './dfStats.js'

export const AGGREGATIONS = [
  { id: 'sum', label: 'Sum' },
  { id: 'mean', label: 'Mean' },
  { id: 'count', label: 'Count' },
  { id: 'min', label: 'Min' },
  { id: 'max', label: 'Max' },
]

// Hard caps so a large frame can't produce thousands of SVG nodes.
export const MAX_BARS = 50          // readable bar count
export const MAX_LINE_POINTS = 500  // line vertices
export const MAX_SCATTER_POINTS = 500

const NULL_LABEL = '(null)'
const isBlank = (v) => v == null || String(v).trim() === '' || /^(nan|none|null|nat|<na>)$/i.test(String(v).trim())

const textCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

/** Sort key comparator for X values of a given column type. */
export function compareX(xType) {
  if (xType === 'number') {
    return (a, b) => numOrInf(a.key) - numOrInf(b.key) || textCollator.compare(a.label, b.label)
  }
  if (xType === 'date') {
    return (a, b) => dateOrInf(a.key) - dateOrInf(b.key) || textCollator.compare(a.label, b.label)
  }
  return (a, b) => textCollator.compare(a.label, b.label)
}
function numOrInf(s) { const n = parseNumeric(s); return Number.isNaN(n) ? Infinity : n }
function dateOrInf(s) { const t = Date.parse(s); return Number.isNaN(t) ? Infinity : t }

/**
 * Group `rows` by column `xCol` and aggregate column `yCol` with `agg`.
 * Returns [{ label, key, y, n }] in first-seen order (callers sort).
 * - count: counts rows per X (Y may be any type; nulls in Y are still counted rows).
 * - sum/mean/min/max: only numeric Y values contribute; a group with none is dropped.
 */
export function aggregateByX(rows, xCol, yCol, agg = 'sum') {
  const groups = new Map()
  for (const r of rows) {
    const rawX = r[xCol]
    const key = isBlank(rawX) ? NULL_LABEL : String(rawX)
    let g = groups.get(key)
    if (!g) { g = { key, label: key, n: 0, cnt: 0, sum: 0, min: Infinity, max: -Infinity }; groups.set(key, g) }
    g.n++
    if (agg === 'count') continue
    const y = parseNumeric(r[yCol])
    if (Number.isNaN(y)) continue
    g.cnt++; g.sum += y
    if (y < g.min) g.min = y
    if (y > g.max) g.max = y
  }
  const out = []
  for (const g of groups.values()) {
    let y
    if (agg === 'count') y = g.n
    else if (g.cnt === 0) continue
    else if (agg === 'mean') y = g.sum / g.cnt
    else if (agg === 'min') y = g.min
    else if (agg === 'max') y = g.max
    else y = g.sum
    out.push({ label: g.label, key: g.key, y, n: g.n })
  }
  return out
}

/**
 * Bar / line series. Returns { pts: [{ label, y, n, i }], groups, capped, note } or
 * { error }. `xType` is 'number' | 'date' | 'string'.
 */
export function buildCategorySeries(kind, rows, xCol, yCol, xType, yType, agg = 'sum') {
  if (agg !== 'count' && yType !== 'number') {
    return { error: 'Pick a numeric Y column (or use Count).' }
  }
  let series = aggregateByX(rows, xCol, yCol, agg)
  if (!series.length) return { error: agg === 'count' ? 'No rows to count.' : 'No numeric Y values to plot.' }
  const groups = series.length

  const byX = compareX(xType)
  let note = ''
  if (kind === 'bar') {
    if (xType === 'number' || xType === 'date') {
      series.sort(byX)
      if (series.length > MAX_BARS) { series = series.slice(0, MAX_BARS); note = `Showing the first ${MAX_BARS} of ${groups} X values.` }
    } else {
      // Text categories: largest first, so the cap keeps the top categories.
      series.sort((a, b) => b.y - a.y || byX(a, b))
      if (series.length > MAX_BARS) { series = series.slice(0, MAX_BARS); note = `Showing the top ${MAX_BARS} of ${groups} categories.` }
    }
  } else {
    // line: always ordered along X.
    series.sort(byX)
    if (series.length > MAX_LINE_POINTS) {
      series = downsample(series, MAX_LINE_POINTS)
      note = `Showing ${series.length} of ${groups} X values (evenly sampled).`
    }
  }
  return { pts: series.map((p, i) => ({ ...p, i })), groups, capped: !!note, note }
}

/** Scatter points from numeric X/Y, evenly sampled across ALL rows (not just the top). */
export function buildScatter(rows, xCol, yCol) {
  const all = []
  for (const r of rows) {
    const x = parseNumeric(r[xCol]); const y = parseNumeric(r[yCol])
    if (!Number.isNaN(x) && !Number.isNaN(y)) all.push({ x, y })
  }
  if (!all.length) return { error: 'No numeric (X, Y) pairs to plot.' }
  if (all.length <= MAX_SCATTER_POINTS) return { pts: all, capped: false, note: '' }
  const pts = downsample(all, MAX_SCATTER_POINTS)
  return { pts, capped: true, note: `Showing ${pts.length} of ${all.length} points (evenly sampled).` }
}

/** Evenly spaced sample of `arr` with exactly `max` items, keeping first and last. */
export function downsample(arr, max) {
  if (arr.length <= max) return arr
  const out = []
  const step = (arr.length - 1) / (max - 1)
  for (let i = 0; i < max; i++) out.push(arr[Math.round(i * step)])
  return out
}
