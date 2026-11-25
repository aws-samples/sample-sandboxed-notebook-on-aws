// Unit tests for services/vmBurst.js (memory / CPU burst scale for the VM history chart).
// Run with: node --test web/src/services/__tests__/vmBurst.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  BURST_FACTOR, tierVcpu, fmtMult, memMult, cpuMult, fitScale, windowScale, axisTicks, refLines, pickRefLabel,
  burstGeometry, burstPointText, burstGroupTooltip, burstCurrent, burstCurrentText, burstBulletRows,
} from '../vmBurst.js'
import { bulletRows, PLOT_H } from '../vmMetricsChart.js'

const T0 = Date.UTC(2026, 10, 12, 9, 51, 12) / 1000
const fmtSec = (t) => new Date(t * 1000).toISOString().slice(11, 19)
const fmt = (t) => new Date(t * 1000).toISOString().slice(11, 16)
const P2G = 2048 // 2 GB tier
const pt = (t, cpu, mem, limit = P2G) => ({ t, t_end: t, cpu, mem_mb: mem, mem_pct: (mem / limit) * 100 })
const grp = (points, extra = {}) => ({ id: 'g', kind: 'run', trigger: 'run_all', start: T0, end: T0 + 60, duration_sec: 60, points, ...extra })

test('the burst ceiling is 4x', () => assert.equal(BURST_FACTOR, 4))

test('fitScale: no burst -> 1x; edges; above 4x clamps and marks OOM', () => {
  assert.deepEqual(fitScale(0), { scale: 1, oom: false })
  assert.deepEqual(fitScale(0.9), { scale: 1, oom: false })
  assert.deepEqual(fitScale(1), { scale: 1, oom: false })
  assert.deepEqual(fitScale(1.0004), { scale: 1, oom: false }) // rounding noise stays 1x
  assert.deepEqual(fitScale(1.3), { scale: 2, oom: false })
  assert.deepEqual(fitScale(2), { scale: 2, oom: false })
  assert.deepEqual(fitScale(2.05), { scale: 4, oom: false })
  assert.deepEqual(fitScale(3.6), { scale: 4, oom: false })
  assert.deepEqual(fitScale(4), { scale: 4, oom: false })
  assert.deepEqual(fitScale(4.4), { scale: 4, oom: true })
  assert.deepEqual(fitScale(null), { scale: 1, oom: false })
})

test('windowScale: memory within the tier stays 1x whatever the CPU does', () => {
  const g = grp([pt(T0, 100, 2048), pt(T0 + 1, 100, 1500)])
  const s = windowScale([g], P2G)
  assert.equal(s.burst, false)
  assert.equal(s.scale, 1)
})

test('windowScale: 1.3x -> 2x, 2.05x -> 4x, 3.6x -> 4x, > 4x -> 4x + oom', () => {
  const at = (mb, cpu = 10) => windowScale([grp([pt(T0, cpu, mb)])], P2G)
  assert.deepEqual([at(2662).burst, at(2662).scale], [true, 2])
  assert.deepEqual([at(4198).burst, at(4198).scale, at(4198).oom], [true, 4, false])
  assert.deepEqual([at(7373).scale, at(7373).oom], [4, false])
  assert.deepEqual([at(9000).scale, at(9000).oom], [4, true])
})

test('windowScale: once memory bursts, the CPU peak must fit the axis too', () => {
  // memory 1.3x alone is 2x; CPU 60 % = 2.4x vCPU forces 4x
  assert.equal(windowScale([grp([pt(T0, 10, 2662)])], P2G).scale, 2)
  assert.equal(windowScale([grp([pt(T0, 60, 2662)])], P2G).scale, 4)
  assert.equal(windowScale([grp([pt(T0, 40, 2662)])], P2G).scale, 2) // 40 % = 1.6x
})

test('windowScale: uses only the shown groups; unknown limit -> no burst', () => {
  assert.equal(windowScale([grp([pt(T0, 5, 800)])], P2G).burst, false)
  assert.equal(windowScale([grp([pt(T0, 5, 5000)])], null).burst, false)
  assert.equal(windowScale([], P2G).burst, false)
})

test('1 GB / 0.5 vCPU tier', () => {
  assert.equal(tierVcpu(1024), 0.5)
  assert.equal(tierVcpu(2048), 1)
  assert.equal(tierVcpu(8192), 4)
  const w = windowScale([grp([pt(T0, 10, 1500, 1024)])], 1024)
  assert.deepEqual([w.burst, w.scale], [true, 2])
  assert.deepEqual(refLines(2, 1024).map(l => l.label), ['1 GB · 1×', '2 GB · 2×'])
  assert.equal(refLines(4, 1024)[2].label, '4 GB · 4× · out-of-memory limit')
  // CPU 100 % = 4x of 0.5 vCPU = 2.0 vCPU
  assert.equal(burstPointText({ t: T0, cpu: 100, mem_mb: 1500 }, 1024, fmtSec),
    '09:51:12 · Mem 1.5 GB (1.46× · burst) · CPU 2.0 vCPU (4× · burst)')
})

test('CPU mapping: % of all pre-allocated cores -> x tier vCPU', () => {
  assert.equal(cpuMult(100), 4)
  assert.equal(cpuMult(25), 1)
  assert.equal(cpuMult(60), 2.4)
  assert.equal(cpuMult(null), null)
})

test('memMult prefers mem_mb / limit, else mem_pct', () => {
  assert.equal(memMult({ mem_mb: 4096 }, 2048), 2)
  assert.equal(memMult({ mem_pct: 150 }, 2048), 1.5)
  assert.equal(memMult({}, 2048), null)
})

test('fmtMult trims decimals', () => {
  assert.equal(fmtMult(2), '2×')
  assert.equal(fmtMult(2.05), '2.05×')
  assert.equal(fmtMult(1.3), '1.3×')
  assert.equal(fmtMult(2.0498), '2.05×')
})

test('axisTicks: multiples gutter', () => {
  assert.deepEqual(axisTicks(2).map(t => t.label), ['0', '1×', '2×'])
  assert.deepEqual(axisTicks(4).map(t => t.label), ['0', '1×', '2×', '4×'])
})

test('refLines: 2x line only at scale >= 2, the OOM line only at 4x, real GB values', () => {
  assert.deepEqual(refLines(1, P2G), [])
  const l2 = refLines(2, P2G)
  assert.deepEqual(l2.map(l => [l.kind, l.label]), [['base', '2 GB · 1×'], ['grid', '4 GB · 2×']])
  const l4 = refLines(4, P2G)
  assert.deepEqual(l4.map(l => l.kind), ['base', 'grid', 'oom'])
  assert.equal(l4[2].label, '8 GB · 4× · out-of-memory limit')
  assert.equal(l4[2].short, '8 GB · 4×')
  assert.match(l4[2].desc, /out-of-memory limit/)
})

test('pickRefLabel: long when it fits, else the short one', () => {
  const line = refLines(4, P2G)[2]
  const measure = (t) => t.length * 5
  assert.equal(pickRefLabel(line, 1000, measure), line.label)
  assert.equal(pickRefLabel(line, 60, measure), line.short)
})

test('burstGeometry: 1x line sits at 1/scale of the height; clamps to the top', () => {
  const g = grp([pt(T0, 100, 4198), pt(T0 + 30, 25, 2048), pt(T0 + 60, 0, 1024)])
  const geo = burstGeometry(g, { scale: 4, limit: P2G })
  assert.equal(geo.y1x, PLOT_H * 0.75)
  assert.equal(geo.cpuPeak.mult, 4)
  assert.equal(geo.cpuPeak.y, 0) // 100 % CPU = 4x = the top
  assert.equal(geo.memLast.mult, 0.5)
  assert.ok(geo.memLine.startsWith('M'))
  // 2.05x memory on a 4x scale is a bit under halfway up
  const first = geo.memLine.split(' L')[0].split(' ')
  assert.ok(Math.abs(Number(first[1]) - (PLOT_H - (2.05 / 4) * PLOT_H)) < 0.5)
  const two = burstGeometry(g, { scale: 2, limit: P2G })
  assert.equal(two.y1x, PLOT_H / 2)
  assert.equal(two.cpuPeak.y, 0) // clamped
})

test('burstPointText: the user case, 4.1 GB on 2 GB', () => {
  const p = { t: T0, cpu: 38, mem_mb: 4198, mem_pct: 205 }
  assert.equal(burstPointText(p, P2G, fmtSec), '09:51:12 · Mem 4.1 GB (2.05× · burst) · CPU 1.5 vCPU (1.52× · burst)')
  assert.equal(burstPointText({ t: T0, cpu: 12, mem_mb: 1024 }, P2G, fmtSec), '09:51:12 · Mem 1 GB (0.5×) · CPU 0.5 vCPU (0.48×)')
})

test('burstPointText: 2.4 vCPU example', () => {
  assert.match(burstPointText({ t: T0, cpu: 60, mem_mb: 100 }, P2G, fmtSec), /CPU 2\.4 vCPU \(2\.4× · burst\)/)
})

test('burstGroupTooltip adds the burst peak only when the run burst', () => {
  const g = grp([pt(T0, 40, 4198)], { cells_executed: 12, cells_errored: 0, cpu_peak: 40, mem_peak_mb: 4198 })
  const t = burstGroupTooltip(g, fmt, P2G)
  assert.match(t, /Mem peak 4\.1 GB · burst peak 4\.1 GB \(2\.05×\)$/)
  assert.match(t, /CPU peak 1\.6 vCPU \(1\.6× · burst\)/)
  const calm = burstGroupTooltip(grp([pt(T0, 10, 900)], { cpu_peak: 10, mem_peak_mb: 900 }), fmt, P2G)
  assert.ok(!calm.includes('burst peak'))
})

test('burstCurrent / burstCurrentText', () => {
  const c = burstCurrent({ cpu: 60, memPct: 205, memMb: 4198 }, P2G)
  assert.equal(c.cpu, 2.4)
  assert.ok(Math.abs(c.mem - 2.0498) < 0.001)
  assert.equal(burstCurrentText(c), 'CPU 2.4× · Mem 2.05×')
  assert.equal(burstCurrent(null, P2G), null)
})

const SUM = (peakMb, cpuPeak = 100, cpuAvg = 42) => ({
  memory: { peak_mb: peakMb, limit_mb: P2G }, cpu: { peak: cpuPeak, avg: cpuAvg }, disk: { last_pct: 23 },
})

test('burstBulletRows: no burst -> rows are the very same objects as today', () => {
  const s = SUM(1900, 95, 20)
  const rows = bulletRows(s, P2G)
  assert.strictEqual(burstBulletRows(rows, s, P2G), rows)
  assert.strictEqual(burstBulletRows(bulletRows(SUM(2048), P2G), SUM(2048), P2G).length, 3)
  const exact = bulletRows(SUM(2048), P2G)
  assert.strictEqual(burstBulletRows(exact, SUM(2048), P2G), exact) // exactly 1x is not a burst
})

test('burstBulletRows: the user case, 4.1 GB on 2 GB, CPU 100 % avg 42 %', () => {
  const s = SUM(4198)
  const rows = burstBulletRows(bulletRows(s, P2G), s, P2G)
  const mem = rows.find(r => r.key === 'memory')
  const cpu = rows.find(r => r.key === 'cpu')
  assert.equal(mem.value, '4.1 GB · 2.05× of 2 GB (burst)')
  assert.equal(mem.scale4, true)
  assert.deepEqual(mem.marks, [0.25, 0.5])
  assert.equal(mem.baseFrac, 0.25)
  assert.ok(Math.abs(mem.frac - 0.5124) < 0.001)
  assert.equal(mem.danger, false) // 51 % of 4x
  assert.equal(cpu.value, '4.0 vCPU · 4× of 1 vCPU (burst) · avg 1.7 vCPU')
  assert.equal(cpu.frac, 1)
  assert.ok(!cpu.value.includes('100%'))
  assert.equal(rows.find(r => r.key === 'disk').value, '23% used') // untouched
})

test('burstBulletRows: danger only at >= 85 % of 4x; OOM text above 4x', () => {
  const near = burstBulletRows(bulletRows(SUM(7373), P2G), SUM(7373), P2G).find(r => r.key === 'memory') // 3.6x
  assert.equal(near.danger, true)
  const edge = burstBulletRows(bulletRows(SUM(6964), P2G), SUM(6964), P2G).find(r => r.key === 'memory') // 3.4x
  assert.equal(edge.danger, true)
  const under = burstBulletRows(bulletRows(SUM(6900), P2G), SUM(6900), P2G).find(r => r.key === 'memory')
  assert.equal(under.danger, false)
  const oom = burstBulletRows(bulletRows(SUM(9000), P2G), SUM(9000), P2G).find(r => r.key === 'memory')
  assert.equal(oom.frac, 1)
  assert.match(oom.value, /over the out-of-memory limit/)
})

test('burstBulletRows: 1 GB tier, CPU below the tier vCPU', () => {
  const s = { memory: { peak_mb: 1500, limit_mb: 1024 }, cpu: { peak: 10, avg: 4 }, disk: {} }
  const rows = burstBulletRows(bulletRows(s, 1024), s, 1024)
  assert.equal(rows.find(r => r.key === 'memory').value, '1.5 GB · 1.46× of 1 GB (burst)')
  assert.equal(rows.find(r => r.key === 'cpu').value, '0.2 vCPU of 0.5 vCPU · avg 0.1 vCPU')
  assert.equal(rows.find(r => r.key === 'cpu').burst, false)
})
