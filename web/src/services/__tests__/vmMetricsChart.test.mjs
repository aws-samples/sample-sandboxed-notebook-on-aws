// Unit tests for services/vmMetricsChart.js (run-grouped VM history chart, Resources tab).
// Run with: node --test web/src/services/__tests__/vmMetricsChart.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as chart from '../vmMetricsChart.js'
import {
  HISTORY_TRIGGER_LABEL, LIMIT_DANGER_PCT, DOT_POINTS, PLOT_H, memText, groupLabel, groupLabelParts,
  LABEL_TIERS, labelWords, pickLabelTier,
  groupTooltip, pointText, pointXs, groupGeometry, nearestPoint, activeText, historyHeader,
  currentValues, currentText, endLabelTops, memDanger, bulletRows, historyEmpty,
} from '../vmMetricsChart.js'

// Deterministic clocks: UTC 'HH:MM' / 'HH:MM:SS'
const T0 = Date.UTC(2026, 10, 12, 15, 31, 0) / 1000
const fmt = (t) => (Number.isFinite(t) ? new Date(t * 1000).toISOString().slice(11, 16) : null)
const fmtSec = (t) => (Number.isFinite(t) ? new Date(t * 1000).toISOString().slice(11, 19) : null)

const pt = (t, cpu, memMb, memPct, extra = {}) => ({ t, t_end: t, cpu, cpu_avg: cpu, mem_mb: memMb, mem_pct: memPct, disk_pct: 23, samples: 1, ...extra })

const RUN = {
  id: 'run:r1', kind: 'run', run_id: 'r1', trigger: 'run_all', start: T0, end: T0 + 70, duration_sec: 70,
  samples: 12, cells_executed: 12, cells_errored: 1, cells_interrupted: 0,
  cpu_peak: 26, cpu_avg: 3, mem_peak_mb: 447, mem_peak_pct: 21.8,
  points: [pt(T0 + 12, 21, 447, 21.8), pt(T0 + 13, 26, 440, 21.5), pt(T0 + 14, 3, 430, 21)],
}
const CELLS = {
  id: 'cells:1', kind: 'cells', run_id: null, trigger: null, start: T0 + 540, end: T0 + 560, duration_sec: 20,
  samples: 4, cells_executed: null, cells_errored: null, cells_interrupted: null,
  cpu_peak: 40, cpu_avg: 20, mem_peak_mb: 512, mem_peak_pct: 25, points: [pt(T0 + 540, 40, 512, 25)],
}

test('removed redesign helpers are gone', () => {
  for (const name of ['xScale', 'niceMax', 'seriesSegments', 'gapBands', 'runBands', 'limitSegments', 'pointAt',
    'headingText', 'compactSummaryText', 'diskText', 'memoryAxis', 'historyMode', 'hoverText',
    'MIN_CHART_SAMPLES', 'MIN_CHART_SPAN_SEC', 'FEW_POINTS']) {
    assert.equal(chart[name], undefined, name)
  }
  assert.equal(HISTORY_TRIGGER_LABEL.run_all, 'Run All')
  assert.equal(LIMIT_DANGER_PCT, 85)
  assert.equal(PLOT_H, 72)
})

test('memText', () => {
  assert.equal(memText(447), '447 MB')
  assert.equal(memText(1900), '1.9 GB')
  assert.equal(memText(2048), '2 GB')
  assert.equal(memText(null), null)
})

test('groupLabel / groupLabelParts', () => {
  assert.equal(groupLabel(RUN, fmt), '15:31 Run All')
  assert.equal(groupLabel(CELLS, fmt), '15:40 cells')
  assert.equal(groupLabel({ ...RUN, trigger: 'weird' }, fmt), '15:31 Run')
  assert.deepEqual(groupLabelParts(RUN, fmt), { time: '15:31', what: 'Run All', short: '15:31' })
  const short = (t) => fmt(t).replace(/^0/, '')
  assert.deepEqual(groupLabelParts(RUN, (t) => `0${fmt(t)} PM`, short), { time: '015:31 PM', what: 'Run All', short: '15:31' })
  assert.equal(groupLabel(null, fmt), '')
})

test('label tiers: widest that fits, short always as the last resort', () => {
  assert.deepEqual(LABEL_TIERS, ['full', 'mid', 'short'])
  const parts = { time: '07:12 PM', what: 'Run All', short: '7:12' }
  assert.deepEqual(labelWords(parts, 'full'), ['07:12 PM', 'Run All'])
  assert.deepEqual(labelWords(parts, 'mid'), ['7:12', 'Run All'])
  assert.deepEqual(labelWords(parts, 'short'), ['7:12'])
  const measure = (w) => w.length * 5 // 5 px per character
  assert.equal(pickLabelTier(parts, 40, measure), 'full') // '07:12 PM' = 40
  assert.equal(pickLabelTier(parts, 39, measure), 'mid') // 'Run All' = 35
  assert.equal(pickLabelTier(parts, 34, measure), 'short') // '7:12' = 20
  assert.equal(pickLabelTier(parts, 5, measure), 'short') // never hidden
  assert.equal(pickLabelTier({ time: '', what: 'cells', short: '' }, 10, measure), 'short')
})

test('groupTooltip: run and cells', () => {
  assert.equal(groupTooltip({ ...RUN, end: T0 + 60 }, fmt),
    'Run All · 15:31–15:32 · 12 cells, 1 error · CPU peak 26% · Mem peak 447 MB')
  assert.equal(groupTooltip(CELLS, fmt), 'Cells · 15:40 · CPU peak 40% · Mem peak 512 MB')
  assert.equal(groupTooltip({ ...CELLS, end: T0 + 600 }, fmt), 'Cells · 15:40–15:41 · CPU peak 40% · Mem peak 512 MB')
  assert.equal(groupTooltip({ ...RUN, trigger: 'single', end: T0 + 5, cells_executed: 1, cells_errored: 0 }, fmt),
    'Cell · 15:31 · 1 cell · CPU peak 26% · Mem peak 447 MB')
  assert.equal(groupTooltip({ ...RUN, cells_errored: 2, cells_interrupted: 1, end: T0 + 3600 }, fmt),
    'Run All · 15:31–16:31 · 12 cells, 2 errors, 1 stopped · CPU peak 26% · Mem peak 447 MB')
  assert.equal(groupTooltip(null, fmt), '')
})

test('pointText', () => {
  assert.equal(pointText(pt(T0 + 12, 21.4, 447, 21.8), fmtSec), '15:31:12 · CPU 21% · Mem 447 MB (22%)')
  assert.equal(pointText(pt(T0, 5, 447, null), fmtSec), '15:31:00 · CPU 5% · Mem 447 MB')
  assert.equal(pointText(pt(T0, null, null, 40), fmtSec), '15:31:00 · Mem 40%')
  assert.equal(pointText(null, fmtSec), '')
})

test('pointXs: padded linear time; one point centred', () => {
  assert.deepEqual(pointXs([pt(0, 1, 1, 1)]), [50])
  assert.deepEqual(pointXs([pt(0, 1, 1, 1), pt(5, 1, 1, 1), pt(10, 1, 1, 1)]), [4, 50, 96])
  assert.deepEqual(pointXs([pt(3, 1, 1, 1), pt(3, 1, 1, 1)]), [50, 50])
  assert.deepEqual(pointXs([]), [])
  // chunk midpoints
  assert.deepEqual(pointXs([{ t: 0, t_end: 2 }, { t: 3, t_end: 5 }]), [4, 96])
})

test('groupGeometry: 3 points → lines, area, dots, peak, last memory', () => {
  const g = groupGeometry(RUN)
  assert.equal(g.cpuLine, 'M4 56.88 L50 53.28 L96 69.84')
  assert.ok(g.cpuArea.startsWith('M4 72 L4 56.88') && g.cpuArea.endsWith('L96 72 Z'))
  assert.equal(g.memLine.split('L').length, 3)
  assert.equal(g.cpuDots.length, 3, '≤ DOT_POINTS → every point is a dot')
  assert.equal(g.memDots.length, 3)
  assert.deepEqual(g.cpuPeak, { x: 50, y: 53.28, value: 26, index: 1 })
  assert.equal(g.memLast.index, 2)
  assert.equal(g.memLast.mb, 430)
  assert.equal(DOT_POINTS, 5)
})

test('groupGeometry: 1 point → dots only; many points → no dots; y clamped', () => {
  const one = groupGeometry(CELLS)
  assert.equal(one.cpuLine, '')
  assert.equal(one.cpuArea, '')
  assert.deepEqual(one.cpuDots, [{ x: 50, y: 43.2 }])
  assert.deepEqual(one.memDots, [{ x: 50, y: 54 }])
  const many = groupGeometry({ points: Array.from({ length: 30 }, (_, i) => pt(i, i === 7 ? 140 : 10, 100, -5)) })
  assert.equal(many.cpuDots.length, 0)
  assert.equal(many.memDots.length, 0)
  assert.equal(many.cpuPeak.y, 0, 'over 100 % clamps to the top')
  assert.equal(many.memLast.y, 72, 'under 0 % clamps to the baseline')
  assert.equal(many.cpuLine.split('L').length, 30)
  assert.deepEqual(groupGeometry({ points: [] }).cpuDots, [])
})

test('nearestPoint', () => {
  assert.equal(nearestPoint([4, 50, 96], 60), 1)
  assert.equal(nearestPoint([4, 50, 96], 90), 2)
  assert.equal(nearestPoint([], 10), -1)
})

test('activeText / historyHeader', () => {
  assert.equal(activeText(40), '40s')
  assert.equal(activeText(192), '3m 12s')
  assert.equal(activeText(3900), '1h 05m')
  assert.equal(activeText(null), null)
  assert.equal(historyHeader({ groups: [RUN, CELLS, RUN, RUN], groups_total: 4, active_sec: 192 }), '4 runs · 3m 12s active')
  assert.equal(historyHeader({ groups: [RUN], groups_total: 1, active_sec: 40 }), '1 run · 40s active')
  assert.equal(historyHeader({ groups: Array(10).fill(RUN), groups_total: 23, active_sec: 500 }), 'last 10 of 23 runs · 8m 20s active')
  assert.equal(historyHeader({ groups: [] }), null)
})

test('currentValues / currentText', () => {
  assert.deepEqual(currentValues([RUN, { ...CELLS, points: [] }]), { cpu: 3, memPct: 21, memMb: 430 })
  assert.equal(currentText({ cpu: 3.2, memPct: 21.8, memMb: 447 }), 'CPU 3% · Mem 22%')
  assert.equal(currentValues([]), null)
  assert.equal(currentText(null), null)
})

test('endLabelTops: at the values, never overlapping, inside the plot', () => {
  assert.deepEqual(endLabelTops(56, 69), { mem: 36, cpu: 60 })
  const a = endLabelTops(40, 42)
  assert.ok(a.cpu >= a.mem + 24 && a.cpu + 12 <= 72)
  const b = endLabelTops(70, 68)
  assert.ok(b.mem + 24 <= 72 && (b.cpu + 12 <= b.mem || b.cpu >= b.mem + 24))
  assert.deepEqual(endLabelTops(null, 10), { mem: null, cpu: 4 })
})

test('memDanger / bulletRows', () => {
  const summary = {
    cpu: { peak: 26, avg: 3 }, memory: { peak_mb: 447, limit_mb: 2048 }, disk: { peak_pct: 23, last_pct: 23 },
  }
  assert.deepEqual(bulletRows(summary, 2048).map(r => [r.key, r.label, r.value]), [
    ['memory', 'Peak memory', '447 MB of 2 GB'],
    ['cpu', 'Peak CPU', '26% · avg 3%'],
    ['disk', 'Disk', '23% used'],
  ])
  assert.equal(bulletRows(summary, 2048)[0].frac, 447 / 2048)
  assert.equal(bulletRows(summary, 2048)[0].danger, false)
  const hot = { ...summary, memory: { peak_mb: 1843, limit_mb: 2048 } }
  assert.equal(memDanger(hot, 2048), true)
  assert.equal(bulletRows(hot, 2048)[0].danger, true)
  assert.equal(bulletRows(hot, 2048)[0].value, '1.8 GB of 2 GB')
  const noLimit = bulletRows({ memory: { peak_mb: 447 }, disk: { peak_pct: 30 } }, null)
  assert.deepEqual(noLimit.map(r => [r.key, r.value, r.frac]), [['memory', '447 MB', null], ['disk', 'peak 30%', 0.3]])
  assert.deepEqual(bulletRows(null, 2048), [])
  assert.equal(memDanger(null, 2048), false)
})

test('historyEmpty', () => {
  assert.equal(historyEmpty(null), true)
  assert.equal(historyEmpty({ groups: [] }), true)
  assert.equal(historyEmpty({ groups: [{ points: [] }] }), true)
  assert.equal(historyEmpty({ groups: [CELLS] }), false)
})
