// Pure helpers for the run-grouped VM history chart in the Live MicroVMs card's
// Resources tab (VmMetricsHistory.jsx). No React or config imports, so
// node --test can load it.
//
// Input is the GET /instances/{id}/metrics-history payload (vm-run-chart):
// `groups` are the last N run groups / ad-hoc "cells" bursts in time order, each
// with downsampled `points` ({t, t_end, cpu, cpu_avg, mem_mb, mem_pct, disk_pct}).
// The x axis is the groups side by side (idle and suspended time removed); CPU %
// and memory % of the tier limit share one 0–100 % scale. `summary` holds the VM
// lifetime peaks for the bullet bars.

import { formatGbFromMb } from './vmCardState.js'

/** Run trigger → short label (same wording as the Run History card). */
export const HISTORY_TRIGGER_LABEL = {
  run_all: 'Run All',
  run_from: 'From cell',
  single: 'Cell',
  reactive: 'Dependents',
}

/** Memory peak ≥ this % of the tier limit turns the 100 % line and the memory bar to the danger colour. */
export const LIMIT_DANGER_PCT = 85
/** At or below this many points, a group also draws every point as a dot. */
export const DOT_POINTS = 5
/** Plot height (px) and the viewBox height of each group's SVG. */
export const PLOT_H = 72
const X_PAD = 4

/** MB → '405 MB' / '1.9 GB'; null when not a number. */
export function memText(mb) {
  if (!Number.isFinite(mb)) return null
  return mb < 1024 ? `${Math.round(mb)} MB` : `${formatGbFromMb(mb)} GB`
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`
}

const pctText = (v) => `${Math.round(v)}%`

function triggerLabel(g) {
  if (g.kind === 'cells') return 'cells'
  return HISTORY_TRIGGER_LABEL[g.trigger] || 'Run'
}

/** Under-group label: '15:31 Run All' / '15:40 cells'. */
export function groupLabel(g, fmtClock) {
  if (!g) return ''
  const t = (fmtClock || (() => null))(g.start)
  return [t, triggerLabel(g)].filter(Boolean).join(' ')
}

/**
 * The label's parts {time, what, short} so it can wrap between them; `short` is the
 * compact time ('7:12') used when the full time doesn't fit the group.
 */
export function groupLabelParts(g, fmtClock, fmtShort) {
  return {
    time: (fmtClock || (() => null))(g?.start) || '',
    what: g ? triggerLabel(g) : '',
    short: (fmtShort || fmtClock || (() => null))(g?.start) || '',
  }
}

/** Label tiers, widest first: full time + trigger, compact time + trigger, compact time only. */
export const LABEL_TIERS = ['full', 'mid', 'short']

/** The words a label shows in a tier (each word is unbreakable; the label wraps between them). */
export function labelWords(parts, tier) {
  if (tier === 'short') return [parts.short].filter(Boolean)
  if (tier === 'mid') return [parts.short, parts.what].filter(Boolean)
  return [parts.time, parts.what].filter(Boolean)
}

/**
 * Widest tier whose widest word fits `avail` px; 'short' when none does
 * (the group's minimum width is sized so the short tier always fits).
 * `measure(word)` returns the word's rendered width in px.
 */
export function pickLabelTier(parts, avail, measure) {
  for (const tier of LABEL_TIERS) {
    const words = labelWords(parts, tier)
    if (Math.max(0, ...words.map(measure)) <= avail) return tier
  }
  return 'short'
}

/**
 * Group tooltip / aria-label:
 *   'Run All · 15:31–15:32 · 12 cells, 1 error · CPU peak 26% · Mem peak 447 MB'
 *   'Cells · 15:40–15:41 · CPU peak 40% · Mem peak 512 MB'
 */
export function groupTooltip(g, fmtClock) {
  if (!g) return ''
  const fmt = fmtClock || (() => null)
  const head = g.kind === 'cells' ? 'Cells' : (HISTORY_TRIGGER_LABEL[g.trigger] || 'Run')
  const a = fmt(g.start)
  const b = fmt(Number.isFinite(g.end) ? g.end : g.start)
  const time = a ? (b && b !== a ? `${a}–${b}` : a) : null
  const cells = []
  if (g.kind !== 'cells' && Number.isFinite(g.cells_executed)) {
    cells.push(plural(g.cells_executed, 'cell', 'cells'))
    if (g.cells_errored > 0) cells.push(plural(g.cells_errored, 'error', 'errors'))
    if (g.cells_interrupted > 0) cells.push(`${g.cells_interrupted} stopped`)
  }
  const cpu = Number.isFinite(g.cpu_peak) ? `CPU peak ${pctText(g.cpu_peak)}` : null
  const mem = Number.isFinite(g.mem_peak_mb) ? `Mem peak ${memText(g.mem_peak_mb)}` : null
  return [head, time, cells.length ? cells.join(', ') : null, cpu, mem].filter(Boolean).join(' · ')
}

/** Point hover text: '15:31:12 · CPU 21% · Mem 447 MB (22%)'. */
export function pointText(p, fmtClockSec) {
  if (!p) return ''
  const fmt = fmtClockSec || (() => null)
  const parts = [fmt(p.t)]
  if (Number.isFinite(p.cpu)) parts.push(`CPU ${pctText(p.cpu)}`)
  if (Number.isFinite(p.mem_mb)) {
    parts.push(Number.isFinite(p.mem_pct) ? `Mem ${memText(p.mem_mb)} (${pctText(p.mem_pct)})` : `Mem ${memText(p.mem_mb)}`)
  } else if (Number.isFinite(p.mem_pct)) {
    parts.push(`Mem ${pctText(p.mem_pct)}`)
  }
  return parts.filter(Boolean).join(' · ')
}

const r2 = (v) => Math.round(v * 100) / 100
const clampPct = (v) => Math.min(100, Math.max(0, v))

/** Point time used for x: the middle of its downsampled chunk. */
function pointTime(p) {
  return Number.isFinite(p.t_end) ? (p.t + p.t_end) / 2 : p.t
}

/** x (0–100, viewBox units) of each point in its group; 1 point (or no span) → 50. */
export function pointXs(points) {
  const ts = (points || []).map(pointTime)
  if (!ts.length) return []
  const t0 = ts[0]
  const t1 = ts[ts.length - 1]
  if (!(t1 > t0)) return ts.map(() => 50)
  return ts.map(t => r2(X_PAD + ((t - t0) / (t1 - t0)) * (100 - 2 * X_PAD)))
}

const yOf = (v, h) => r2(h - (clampPct(v) / 100) * h)

function linePath(pts) {
  return pts.map((p, i) => `${i ? 'L' : 'M'}${p.x} ${p.y}`).join(' ')
}

/**
 * One group's SVG geometry in a 0..100 × 0..height viewBox.
 * → { cpuArea, cpuLine, memLine (path strings, '' when < 2 points),
 *     cpuDots, memDots ([{x, y}], every point when the group has ≤ DOT_POINTS),
 *     cpuPeak {x, y, value, index} | null, memLast {x, y, pct, mb, index} | null, xs }
 */
export function groupGeometry(g, { height = PLOT_H } = {}) {
  const points = g?.points || []
  const xs = pointXs(points)
  const cpu = []
  const mem = []
  points.forEach((p, i) => {
    if (Number.isFinite(p.cpu)) cpu.push({ x: xs[i], y: yOf(p.cpu, height), v: p.cpu, i })
    if (Number.isFinite(p.mem_pct)) mem.push({ x: xs[i], y: yOf(p.mem_pct, height), v: p.mem_pct, i })
  })
  const few = points.length <= DOT_POINTS
  const cpuLine = cpu.length > 1 ? linePath(cpu) : ''
  const cpuArea = cpu.length > 1
    ? `M${cpu[0].x} ${height} ${cpu.map(p => `L${p.x} ${p.y}`).join(' ')} L${cpu[cpu.length - 1].x} ${height} Z`
    : ''
  let cpuPeak = null
  for (const p of cpu) if (!cpuPeak || p.v > cpuPeak.value) cpuPeak = { x: p.x, y: p.y, value: p.v, index: p.i }
  const last = mem.length ? mem[mem.length - 1] : null
  return {
    xs,
    cpuArea,
    cpuLine,
    memLine: mem.length > 1 ? linePath(mem) : '',
    cpuDots: few || cpu.length === 1 ? cpu.map(({ x, y }) => ({ x, y })) : [],
    memDots: few || mem.length === 1 ? mem.map(({ x, y }) => ({ x, y })) : [],
    cpuPeak,
    memLast: last ? { x: last.x, y: last.y, pct: last.v, mb: points[last.i].mem_mb ?? null, index: last.i } : null,
  }
}

/** Index of the point nearest to x (0–100) in a group; -1 for none. */
export function nearestPoint(xs, x) {
  let best = -1
  let bestD = Infinity
  ;(xs || []).forEach((px, i) => {
    const d = Math.abs(px - x)
    if (d < bestD) { best = i; bestD = d }
  })
  return best
}

/** Active time: '40s' | '3m 12s' | '1h 05m'. */
export function activeText(sec) {
  if (!Number.isFinite(sec) || sec < 0) return null
  const s = Math.round(sec)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

/** Header meta: '4 runs · 3m 12s active' | '1 run · 40s active' | 'last 10 of 23 runs · 8m 20s active'. */
export function historyHeader(payload) {
  const shown = (payload?.groups || []).length
  if (!shown) return null
  const total = Number.isFinite(payload.groups_total) && payload.groups_total > shown ? payload.groups_total : shown
  const count = total > shown ? `last ${shown} of ${total} runs` : plural(shown, 'run', 'runs')
  const active = activeText(payload.active_sec)
  return active ? `${count} · ${active} active` : count
}

/** Newest values for the end labels: {cpu, memPct, memMb} (null parts when unknown); null without points. */
export function currentValues(groups) {
  for (let gi = (groups || []).length - 1; gi >= 0; gi--) {
    const pts = groups[gi].points || []
    if (pts.length) {
      const p = pts[pts.length - 1]
      return {
        cpu: Number.isFinite(p.cpu) ? p.cpu : null,
        memPct: Number.isFinite(p.mem_pct) ? p.mem_pct : null,
        memMb: Number.isFinite(p.mem_mb) ? p.mem_mb : null,
      }
    }
  }
  return null
}

/** Narrow-header text 'CPU 3% · Mem 22%'; null without values. */
export function currentText(cur) {
  if (!cur) return null
  const parts = []
  if (Number.isFinite(cur.cpu)) parts.push(`CPU ${pctText(cur.cpu)}`)
  if (Number.isFinite(cur.memPct)) parts.push(`Mem ${pctText(cur.memPct)}`)
  return parts.length ? parts.join(' · ') : null
}

/**
 * Tops (px) of the right-edge end labels so they sit at their series' last
 * value without overlapping: the memory block (2 lines) and the CPU line.
 */
export function endLabelTops(memY, cpuY, { height = PLOT_H, memH = 24, cpuH = 12 } = {}) {
  const clampTop = (y, h) => Math.min(height - h, Math.max(0, y))
  let mem = Number.isFinite(memY) ? clampTop(memY - 6, memH) : null
  let cpu = Number.isFinite(cpuY) ? clampTop(cpuY - 6, cpuH) : null
  if (mem !== null && cpu !== null && cpu < mem + memH && mem < cpu + cpuH) {
    if (cpuY >= memY) {
      cpu = mem + memH
      if (cpu + cpuH > height) { cpu = height - cpuH; mem = cpu - memH }
    } else {
      mem = cpu + cpuH
      if (mem + memH > height) { mem = height - memH; cpu = mem - cpuH }
    }
  }
  return { mem, cpu }
}

/** True when the lifetime memory peak is ≥ LIMIT_DANGER_PCT of the limit. */
export function memDanger(summary, limitMb) {
  const m = summary?.memory || {}
  const limit = Number.isFinite(limitMb) && limitMb > 0 ? limitMb : m.limit_mb
  if (!Number.isFinite(m.peak_mb) || !(limit > 0)) return false
  return (m.peak_mb / limit) * 100 >= LIMIT_DANGER_PCT
}

/**
 * Lifetime bullet bars (Stephen Few): rows without data are skipped.
 * → [{key: 'memory', label: 'Peak memory', frac, value: '447 MB of 2 GB', danger},
 *    {key: 'cpu', label: 'Peak CPU', frac, value: '26% · avg 3%'},
 *    {key: 'disk', label: 'Disk', frac, value: '23% used' | 'peak 23%'}]
 */
export function bulletRows(summary, limitMb) {
  if (!summary) return []
  const rows = []
  const frac = (v) => Math.min(1, Math.max(0, v))
  const m = summary.memory || {}
  if (Number.isFinite(m.peak_mb)) {
    const limit = Number.isFinite(limitMb) && limitMb > 0 ? limitMb : (m.limit_mb > 0 ? m.limit_mb : null)
    rows.push({
      key: 'memory',
      label: 'Peak memory',
      frac: limit ? frac(m.peak_mb / limit) : null,
      value: limit ? `${memText(m.peak_mb)} of ${memText(limit)}` : memText(m.peak_mb),
      danger: memDanger(summary, limit),
      limit: !!limit,
    })
  }
  const c = summary.cpu || {}
  if (Number.isFinite(c.peak)) {
    rows.push({
      key: 'cpu',
      label: 'Peak CPU',
      frac: frac(c.peak / 100),
      value: Number.isFinite(c.avg) ? `${pctText(c.peak)} · avg ${pctText(c.avg)}` : pctText(c.peak),
      danger: false,
    })
  }
  const d = summary.disk || {}
  const used = Number.isFinite(d.last_pct) ? d.last_pct : null
  const v = used ?? (Number.isFinite(d.peak_pct) ? d.peak_pct : null)
  if (v !== null) {
    rows.push({
      key: 'disk',
      label: 'Disk',
      frac: frac(v / 100),
      value: used !== null ? `${pctText(used)} used` : `peak ${pctText(v)}`,
      danger: false,
    })
  }
  return rows
}

/** True when the payload has nothing to draw. */
export function historyEmpty(payload) {
  if (!payload) return true
  return !(payload.groups || []).some(g => (g.points || []).length)
}
