import { NULL_TOKENS } from './dfCellFormat'

/**
 * dfStats — shared, dependency-free column analysis over the DataGrid's already-
 * received string cells. This is the single source of truth for column TYPE
 * detection (used by the grid for alignment/sort, by the header type badges, and
 * by the chart) and for per-column summary STATS (the header popover).
 *
 * Everything here operates on the display strings the VM sent (payload.cells),
 * i.e. the shown/capped view — the same scope as the grid's sort/search. Callers
 * that show stats should label them "over shown rows" when the payload is capped.
 */

// A fully-numeric token after stripping currency/percent/commas/space. Anchored so
// messy strings like "123abc" are NOT treated as numbers. Kept in lock-step with
// the grid's original inline regexes (moved here so grid + badges + chart agree).
const NUMERIC_TOKEN_RE = /^[+-]?\d+(\.\d+)?$/
const DATE_TOKEN_RE = /^\d{4}-\d{2}-\d{2}/

/** True when a raw display string is empty or a null/NaN token. */
export function isNullToken(s) {
  return !s || NULL_TOKENS.has(s)
}

/** Parse a numeric display string ("$1,234.5", "57.1", "-3") → number, or NaN. */
export function parseNumeric(s) {
  if (isNullToken(s)) return NaN
  return parseFloat(String(s).replace(/[$,€£%\s]/g, ''))
}

/**
 * Infer a column's sort/render type from a sample of its display strings.
 * Returns 'number' | 'date' | 'string'. Date is checked before number so a
 * leading-numeric date ("2024-01-02") isn't mis-typed as a number.
 */
export function detectType(samples) {
  const clean = samples.filter(s => s && !NULL_TOKENS.has(s)).slice(0, 20)
  if (clean.length === 0) return 'string'
  if (clean.every(s => DATE_TOKEN_RE.test(s))) return 'date'
  if (clean.every(s => !DATE_TOKEN_RE.test(s) && NUMERIC_TOKEN_RE.test(s.replace(/[$,€£%\s]/g, '')))) return 'number'
  return 'string'
}

/**
 * Map a pandas dtype string (payload.dtypes) to a sort/render type, or null when
 * the dtype isn't safe to trust (object/string/category/bool/timedelta) so the
 * caller falls back to detectType() sampling.
 */
export function dtypeToType(dtype) {
  if (typeof dtype !== 'string') return null
  const d = dtype.toLowerCase()
  if (d.startsWith('datetime') || d.startsWith('period')) return 'date'
  if (d.startsWith('int') || d.startsWith('uint') || d.startsWith('float')) return 'number'
  return null
}

/**
 * Resolve a column's effective type: prefer the server dtype (when usable), else
 * sample the column's cell strings. `dtype` may be undefined.
 */
export function columnType(colCells, dtype) {
  return dtypeToType(dtype) || detectType(colCells.map(v => String(v ?? '')))
}

/** Short badge glyph + label + a11y title for a column type. */
export function typeBadge(type) {
  switch (type) {
    case 'number': return { glyph: '#', label: 'number', title: 'Numeric column' }
    case 'date': return { glyph: '📅', label: 'date', title: 'Date column' }
    default: return { glyph: 'A', label: 'text', title: 'Text column' }
  }
}

function median(sortedNums) {
  const n = sortedNums.length
  if (n === 0) return NaN
  const mid = Math.floor(n / 2)
  return n % 2 ? sortedNums[mid] : (sortedNums[mid - 1] + sortedNums[mid]) / 2
}

/**
 * Compute summary stats for one column over the provided rows (2-D array of
 * display strings) at column index `colIdx`.
 *
 * Returns:
 *   { type, total, nonNull, nulls, unique,
 *     numeric?: { min, max, mean, median },
 *     top?: [{ value, count }]   // for non-numeric: top values by frequency
 *   }
 */
export function computeColumnStats(rows, colIdx, type) {
  const total = rows.length
  let nulls = 0
  const values = []            // non-null display strings
  const uniq = new Set()

  for (const row of rows) {
    const raw = row[colIdx]
    const s = raw == null ? '' : String(raw)
    if (isNullToken(s)) { nulls++; continue }
    values.push(s)
    uniq.add(s)
  }

  const stats = {
    type,
    total,
    nonNull: values.length,
    nulls,
    unique: uniq.size,
  }

  if (type === 'number') {
    const nums = []
    for (const s of values) {
      const n = parseNumeric(s)
      if (!Number.isNaN(n)) nums.push(n)
    }
    if (nums.length) {
      nums.sort((a, b) => a - b)
      const sum = nums.reduce((acc, n) => acc + n, 0)
      stats.numeric = {
        min: nums[0],
        max: nums[nums.length - 1],
        mean: sum / nums.length,
        median: median(nums),
      }
    }
  } else {
    // Top values by frequency (works for strings and dates alike).
    const counts = new Map()
    for (const s of values) counts.set(s, (counts.get(s) || 0) + 1)
    stats.top = [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([value, count]) => ({ value, count }))
  }

  return stats
}

/**
 * Bin numeric values into `binCount` equal-width buckets for a histogram.
 * Returns { bins: [{ x0, x1, count }], min, max } or null when there's no
 * numeric data / zero range (caller shows a friendly note).
 */
export function histogramBins(nums, binCount = 20) {
  const clean = nums.filter(n => typeof n === 'number' && !Number.isNaN(n))
  if (clean.length === 0) return null
  let min = Infinity, max = -Infinity
  for (const n of clean) { if (n < min) min = n; if (n > max) max = n }
  if (min === max) {
    // Degenerate range — single bin holding everything.
    return { bins: [{ x0: min, x1: max, count: clean.length }], min, max }
  }
  const width = (max - min) / binCount
  const bins = Array.from({ length: binCount }, (_, i) => ({
    x0: min + i * width,
    x1: min + (i + 1) * width,
    count: 0,
  }))
  for (const n of clean) {
    let idx = Math.floor((n - min) / width)
    if (idx >= binCount) idx = binCount - 1  // max value lands in the last bin
    if (idx < 0) idx = 0
    bins[idx].count++
  }
  return { bins, min, max }
}

/** Compact number formatting for stat display (e.g. 1,234.57 → "1,234.57"). */
export function fmtNum(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return '—'
  if (Number.isInteger(n)) return n.toLocaleString()
  // Up to 4 significant-ish decimals, trimmed.
  const abs = Math.abs(n)
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 3 : 4
  return n.toLocaleString(undefined, { maximumFractionDigits: digits })
}
