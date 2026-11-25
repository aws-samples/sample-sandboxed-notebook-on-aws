// Pure burst helpers for the VM History chart and the VM lifetime bars (vm-history-burst).
// No React or config imports, so node --test can load it. The no-burst path never calls
// into this module's geometry / text builders, so a VM that never exceeded its tier renders
// exactly as before (vmMetricsChart.js is untouched).
//
// Where the 4× comes from: AWS pre-allocates 4× the tier's memory AND vCPU to every MicroVM
// from boot (README.md "Burst Model"; tests/common/test_burst_behavior.py docstring). Usage
// above the tier (1×) is billed as burst (proxy/platform/cost_tracker.py record_burst_sample
// / price_quantities); exceeding 4× is an out-of-memory crash. BURST_FACTOR is the one
// web-side copy of the 4×; the launch dialog (components/ConnectionPanel.jsx) imports it
// and tierVcpu for its "Bursts to" line.
//
// CPU: the VM's `cpu.percent` is psutil.cpu_percent() over ALL pre-allocated cores
// (app/notebook/routes.py:971,990; psutil.cpu_count() inside the VM is 4× the tier's vCPU),
// so 100 % means "4× vCPU fully busy" and 25 % is exactly the tier's vCPU (1×).
//
// Memory: `mem_mb` is the kernel process RSS and `mem_pct` = mem_mb / tier limit
// (proxy/platform/vm_metrics_history.py _mem_pct), so it is already relative to the tier
// and can exceed 100 %.

import { PLOT_H, DOT_POINTS, LIMIT_DANGER_PCT, memText, pointXs, groupTooltip } from './vmMetricsChart.js'

/** Burst ceiling as a multiple of the tier (memory and vCPU). */
export const BURST_FACTOR = 4
/** The chart's selectable tops, in multiples of the tier. */
export const BURST_SCALES = [1, 2, 4]
/** Lifetime bars: danger red when the peak reaches this fraction of the 4× ceiling. */
export const BURST_DANGER_FRAC = LIMIT_DANGER_PCT / 100

const r2 = (v) => Math.round(v * 100) / 100
const r3 = (v) => Math.round(v * 1000) / 1000
const validLimit = (limitMb) => Number.isFinite(limitMb) && limitMb > 0

/** The tier's vCPU: 2 GB : 1 vCPU (cost_tracker.price_quantities), so 1 GB = 0.5. */
export function tierVcpu(limitMb) {
  return validLimit(limitMb) ? limitMb / 1024 / 2 : null
}

/** '2×' | '2.05×' | '1.3×' (up to 2 decimals, trailing zeros dropped). */
export function fmtMult(m) {
  return `${r2(m)}×`
}

/** '4.0' (one decimal) for used vCPU; the tier's own vCPU is '1' | '0.5'. */
export const fmtVcpu = (v) => v.toFixed(1)
export const fmtTierVcpu = (v) => String(r2(v))

/** Memory as a multiple of the tier for one point; null when unknown. */
export function memMult(p, limitMb) {
  if (!p) return null
  if (Number.isFinite(p.mem_mb) && validLimit(limitMb)) return p.mem_mb / limitMb
  return Number.isFinite(p.mem_pct) ? p.mem_pct / 100 : null
}

/** CPU % of ALL pre-allocated cores → multiple of the tier's vCPU (100 % = 4×, 25 % = 1×). */
export function cpuMult(cpuPct) {
  return Number.isFinite(cpuPct) ? (cpuPct / 100) * BURST_FACTOR : null
}

/** Smallest scale (1×/2×/4×) that fits the peak multiple; above 4× clamps at 4× and sets `oom`. */
export function fitScale(peak) {
  const m = Number.isFinite(peak) ? r3(peak) : 0
  if (m <= 1) return { scale: 1, oom: false }
  if (m <= 2) return { scale: 2, oom: false }
  if (m <= BURST_FACTOR) return { scale: 4, oom: false }
  return { scale: 4, oom: true }
}

/**
 * The chart's scale for the SHOWN groups. Memory decides whether there is a burst at all
 * (so a VM whose memory stayed within its tier is untouched); once there is, the CPU peak
 * (as × tier vCPU) must fit the same axis too.
 * → { burst, scale, oom, memPeak, cpuPeak }  (peaks are multiples; null when no data)
 */
export function windowScale(groups, limitMb) {
  const none = { burst: false, scale: 1, oom: false, memPeak: null, cpuPeak: null }
  if (!validLimit(limitMb)) return none
  let memPeak = null
  let cpuPeak = null
  for (const g of groups || []) {
    for (const p of g.points || []) {
      const m = memMult(p, limitMb)
      if (m !== null && (memPeak === null || m > memPeak)) memPeak = m
      const c = cpuMult(p.cpu)
      if (c !== null && (cpuPeak === null || c > cpuPeak)) cpuPeak = c
    }
  }
  if (memPeak === null || r3(memPeak) <= 1) return { ...none, memPeak, cpuPeak }
  const { scale, oom } = fitScale(Math.max(memPeak, cpuPeak ?? 0))
  return { burst: true, scale, oom, memPeak, cpuPeak }
}

/** Y-axis gutter labels as {mult, label}: '0', '1×', then '2×' and '4×' up to the scale. */
export function axisTicks(scale) {
  const out = [{ mult: 0, label: '0' }, { mult: 1, label: '1×' }]
  if (scale >= 2) out.push({ mult: 2, label: '2×' })
  if (scale >= 4) out.push({ mult: 4, label: '4×' })
  return out
}

/**
 * Reference lines (memory, real GB values):
 *   1×  '2 GB · 1×'                         muted: billing for burst starts above it
 *   2×  '4 GB · 2×'                         faint gridline, only when the scale is ≥ 2×
 *   4×  '8 GB · 4× · out-of-memory limit'   danger, only when the scale is 4×
 * `short` is the label when `label` doesn't fit; `desc` goes to the tooltip / aria.
 */
export function refLines(scale, limitMb) {
  if (!validLimit(limitMb) || scale <= 1) return []
  const gb = (m) => memText(limitMb * m)
  const lines = [{
    key: 'base', kind: 'base', mult: 1,
    label: `${gb(1)} · 1×`, short: `${gb(1)} · 1×`,
    desc: `${gb(1)} · 1×: the tier baseline. Memory above this line is billed as burst.`,
  }]
  if (scale >= 2) {
    lines.push({
      key: 'x2', kind: 'grid', mult: 2,
      label: `${gb(2)} · 2×`, short: `${gb(2)} · 2×`,
      desc: `${gb(2)} · 2× the tier.`,
    })
  }
  if (scale >= BURST_FACTOR) {
    lines.push({
      key: 'oom', kind: 'oom', mult: BURST_FACTOR,
      label: `${gb(BURST_FACTOR)} · 4× · out-of-memory limit`, short: `${gb(BURST_FACTOR)} · 4×`,
      desc: `${gb(BURST_FACTOR)} · 4×: the out-of-memory limit. A VM that goes above it crashes.`,
    })
  }
  return lines
}

/** The long label when it fits `avail` px, else the short one. `measure(text)` → px. */
export function pickRefLabel(line, avail, measure) {
  return measure(line.label) <= avail ? line.label : line.short
}

const yOf = (m, scale, h) => r2(h - (Math.min(scale, Math.max(0, m)) / scale) * h)
const linePath = (pts) => pts.map((p, i) => `${i ? 'L' : 'M'}${p.x} ${p.y}`).join(' ')

/**
 * One group's SVG geometry on the multiples scale (same shape as groupGeometry, plus
 * `y1x`, the y of the 1× line, for the burst band and the amber clip). CPU is drawn as
 * × tier vCPU (cpu % × 4 / 100), memory as × tier memory; both clamp to the scale top.
 */
export function burstGeometry(g, { height = PLOT_H, scale = 4, limit = null } = {}) {
  const points = g?.points || []
  const xs = pointXs(points)
  const cpu = []
  const mem = []
  points.forEach((p, i) => {
    const c = cpuMult(p.cpu)
    if (c !== null) cpu.push({ x: xs[i], y: yOf(c, scale, height), v: c, pct: p.cpu, i })
    const m = memMult(p, limit)
    if (m !== null) mem.push({ x: xs[i], y: yOf(m, scale, height), v: m, i })
  })
  const few = points.length <= DOT_POINTS
  let cpuPeak = null
  for (const p of cpu) if (!cpuPeak || p.v > cpuPeak.mult) cpuPeak = { x: p.x, y: p.y, value: p.pct, mult: p.v, index: p.i }
  const last = mem.length ? mem[mem.length - 1] : null
  return {
    xs,
    y1x: yOf(1, scale, height),
    cpuArea: cpu.length > 1
      ? `M${cpu[0].x} ${height} ${cpu.map(p => `L${p.x} ${p.y}`).join(' ')} L${cpu[cpu.length - 1].x} ${height} Z`
      : '',
    cpuLine: cpu.length > 1 ? linePath(cpu) : '',
    memLine: mem.length > 1 ? linePath(mem) : '',
    cpuDots: few || cpu.length === 1 ? cpu.map(({ x, y }) => ({ x, y })) : [],
    memDots: few || mem.length === 1 ? mem.map(({ x, y }) => ({ x, y })) : [],
    cpuPeak,
    memLast: last ? { x: last.x, y: last.y, mult: last.v, mb: points[last.i].mem_mb ?? null, index: last.i } : null,
  }
}

function cpuPart(pct, limitMb, { peak = false } = {}) {
  const c = cpuMult(pct)
  const tier = tierVcpu(limitMb)
  if (c === null || tier === null) return null
  const used = fmtVcpu(c * tier)
  return `CPU${peak ? ' peak' : ''} ${used} vCPU (${fmtMult(c)}${c > 1 ? ' · burst' : ''})`
}

/** Point hover text on the multiples scale: '09:51:12 · Mem 4.1 GB (2.05× · burst) · CPU 1.5 vCPU (1.52× · burst)'. */
export function burstPointText(p, limitMb, fmtClockSec) {
  if (!p) return ''
  const fmt = fmtClockSec || (() => null)
  const parts = [fmt(p.t)]
  const m = memMult(p, limitMb)
  if (m !== null) {
    const mb = Number.isFinite(p.mem_mb) ? memText(p.mem_mb) : null
    const tag = `${fmtMult(m)}${r3(m) > 1 ? ' · burst' : ''}`
    parts.push(mb ? `Mem ${mb} (${tag})` : `Mem ${tag}`)
  }
  parts.push(cpuPart(p.cpu, limitMb))
  return parts.filter(Boolean).join(' · ')
}

/** Group tooltip on the multiples scale: the plain tooltip, CPU peak in vCPU, plus 'burst peak 4.1 GB (2.05×)'. */
export function burstGroupTooltip(g, fmtClock, limitMb) {
  let text = groupTooltip(g, fmtClock)
  if (!g || !validLimit(limitMb)) return text
  if (Number.isFinite(g.cpu_peak)) {
    const plain = `CPU peak ${Math.round(g.cpu_peak)}%`
    const conv = cpuPart(g.cpu_peak, limitMb, { peak: true })
    if (conv && text.includes(plain)) text = text.replace(plain, conv)
  }
  if (Number.isFinite(g.mem_peak_mb) && r3(g.mem_peak_mb / limitMb) > 1) {
    text += ` · burst peak ${memText(g.mem_peak_mb)} (${fmtMult(g.mem_peak_mb / limitMb)})`
  }
  return text
}

/** Newest values on the multiples scale: {cpu (× tier vCPU), mem (× tier), memMb}; null parts when unknown. */
export function burstCurrent(cur, limitMb) {
  if (!cur) return null
  const memFromPct = Number.isFinite(cur.memPct) ? cur.memPct / 100 : null
  const mem = Number.isFinite(cur.memMb) && validLimit(limitMb) ? cur.memMb / limitMb : memFromPct
  return { cpu: cpuMult(cur.cpu), mem, memMb: Number.isFinite(cur.memMb) ? cur.memMb : null }
}

/** Narrow-header text 'CPU 2.4× · Mem 2.05×'; null without values. */
export function burstCurrentText(cur) {
  if (!cur) return null
  const parts = []
  if (Number.isFinite(cur.cpu)) parts.push(`CPU ${fmtMult(cur.cpu)}`)
  if (Number.isFinite(cur.mem)) parts.push(`Mem ${fmtMult(cur.mem)}`)
  return parts.length ? parts.join(' · ') : null
}

/**
 * Lifetime bars with the burst treatment (VM lifetime, all samples). `rows` is
 * bulletRows(summary, limit). When the lifetime memory peak is within the tier, the rows
 * come back untouched. Otherwise the memory and CPU rows go onto a 0–4× scale:
 *   scale4: true, marks [0.25, 0.5] (1× and 2×), baseFrac 0.25 (amber above it),
 *   memory value '4.1 GB · 2.05× of 2 GB (burst)', danger only when the peak ≥ 85 % of 4×,
 *   CPU value '4.0 vCPU · 4× of 1 vCPU (burst) · avg 1.7 vCPU'.
 */
export function burstBulletRows(rows, summary, limitMb) {
  const peakMb = summary?.memory?.peak_mb
  if (!validLimit(limitMb) || !Number.isFinite(peakMb) || r3(peakMb / limitMb) <= 1) return rows
  const tier = tierVcpu(limitMb)
  const frac = (m) => Math.min(1, Math.max(0, m / BURST_FACTOR))
  const base = { scale4: true, marks: [1 / BURST_FACTOR, 2 / BURST_FACTOR], baseFrac: 1 / BURST_FACTOR }
  return rows.map((row) => {
    if (row.key === 'memory') {
      const m = peakMb / limitMb
      const oom = r3(m) > BURST_FACTOR
      return {
        ...row, ...base,
        frac: frac(m),
        value: `${memText(peakMb)} · ${fmtMult(m)} of ${memText(limitMb)} (burst${oom ? ', over the out-of-memory limit' : ''})`,
        danger: frac(m) >= BURST_DANGER_FRAC,
        burst: true,
        limit: true,
      }
    }
    if (row.key === 'cpu' && Number.isFinite(summary?.cpu?.peak)) {
      const c = cpuMult(summary.cpu.peak)
      const used = (pct) => `${fmtVcpu(cpuMult(pct) * tier)} vCPU`
      const head = c > 1
        ? `${used(summary.cpu.peak)} · ${fmtMult(c)} of ${fmtTierVcpu(tier)} vCPU (burst)`
        : `${used(summary.cpu.peak)} of ${fmtTierVcpu(tier)} vCPU`
      const avg = Number.isFinite(summary.cpu.avg) ? ` · avg ${used(summary.cpu.avg)}` : ''
      return { ...row, ...base, frac: frac(c), value: head + avg, burst: c > 1, danger: false }
    }
    return row
  })
}
