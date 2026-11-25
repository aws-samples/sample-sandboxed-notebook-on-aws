// Unit tests for services/runHistoryTop.js (Run History "Top 10 / All").
// Run with: node --test web/src/services/__tests__/runHistoryTop.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  TOP_N, TOGGLE_THRESHOLD, DEFAULT_CELL_VIEW, CELL_VIEW_STORAGE_KEY,
  hasTopToggle, parseCellView, selectRunCells, paramTipText, PARAM_TIP_LINE_MAX,
} from '../runHistoryTop.js'

// n cells in notebook order; each metric is derived from the index so the
// three metrics rank the cells differently.
const mk = (n) => Array.from({ length: n }, (_, i) => ({
  cell_id: `c${i}`, label: `PY-${i + 1}`,
  ms: (i * 37) % 101,          // duration
  cpu: (i * 13) % 29,          // cpu
  mem: 1000 - i * 10,          // memory: descending in notebook order
}))
const ids = (r) => r.cells.map(c => c.cell_id)
const by = (k) => (c) => c[k]

test('constants', () => {
  assert.equal(TOP_N, 10)
  assert.equal(TOGGLE_THRESHOLD, 15)
  assert.equal(DEFAULT_CELL_VIEW, 'top')
  assert.equal(typeof CELL_VIEW_STORAGE_KEY, 'string')
})

test('hasTopToggle: only above 15 cells', () => {
  assert.equal(hasTopToggle(0), false)
  assert.equal(hasTopToggle(10), false)
  assert.equal(hasTopToggle(15), false)
  assert.equal(hasTopToggle(16), true)
  assert.equal(hasTopToggle(37), true)
  assert.equal(hasTopToggle(NaN), false)
})

test('parseCellView: default is top', () => {
  assert.equal(parseCellView(null), 'top')
  assert.equal(parseCellView('junk'), 'top')
  assert.equal(parseCellView('top'), 'top')
  assert.equal(parseCellView('all'), 'all')
})

test('<=15 cells: no toggle, every cell in notebook order, whatever the view', () => {
  for (const n of [1, 10, 15]) {
    const cells = mk(n)
    for (const view of ['top', 'all']) {
      const r = selectRunCells(cells, by('ms'), view)
      assert.equal(r.toggle, false)
      assert.equal(r.total, n)
      assert.equal(r.hidden, 0)
      assert.deepEqual(ids(r), cells.map(c => c.cell_id))
    }
  }
})

test('>15 cells: toggle; All = every cell in notebook order', () => {
  const cells = mk(37)
  const r = selectRunCells(cells, by('ms'), 'all')
  assert.equal(r.toggle, true)
  assert.equal(r.total, 37)
  assert.equal(r.hidden, 0)
  assert.deepEqual(ids(r), cells.map(c => c.cell_id))
})

test('>15 cells: Top 10 = 10 highest for the metric, sorted descending', () => {
  const cells = mk(37)
  for (const k of ['ms', 'cpu', 'mem']) {
    const r = selectRunCells(cells, by(k), 'top')
    assert.equal(r.toggle, true)
    assert.equal(r.cells.length, 10)
    assert.equal(r.hidden, 27)
    const vals = r.cells.map(c => c[k])
    assert.deepEqual(vals, [...vals].sort((a, b) => b - a), `${k} sorted desc`)
    const min = Math.min(...vals)
    const outside = cells.filter(c => !r.cells.includes(c))
    assert.ok(outside.every(c => c[k] <= min), `${k}: nothing hidden beats the shown 10`)
  }
  // Metrics rank differently, so switching the metric re-sorts the Top 10.
  assert.notDeepEqual(ids(selectRunCells(cells, by('ms'), 'top')), ids(selectRunCells(cells, by('cpu'), 'top')))
  // Memory descends in notebook order → Top 10 = the first 10 cells.
  assert.deepEqual(ids(selectRunCells(cells, by('mem'), 'top')), cells.slice(0, 10).map(c => c.cell_id))
})

test('ties keep notebook order', () => {
  const cells = Array.from({ length: 20 }, (_, i) => ({ cell_id: `c${i}`, v: i % 2 === 0 ? 5 : 1 }))
  cells[13].v = 9
  const r = selectRunCells(cells, by('v'), 'top')
  assert.deepEqual(ids(r), ['c13', 'c0', 'c2', 'c4', 'c6', 'c8', 'c10', 'c12', 'c14', 'c16'])
  // All equal → the first 10 in notebook order.
  const flat = Array.from({ length: 16 }, (_, i) => ({ cell_id: `c${i}`, v: 3 }))
  assert.deepEqual(ids(selectRunCells(flat, by('v'), 'top')), flat.slice(0, 10).map(c => c.cell_id))
})

test('missing / non-numeric values sort as 0, input is not mutated', () => {
  const cells = Array.from({ length: 16 }, (_, i) => ({ cell_id: `c${i}`, v: i === 3 ? 7 : undefined }))
  const before = cells.map(c => c.cell_id)
  const r = selectRunCells(cells, by('v'), 'top')
  assert.equal(r.cells[0].cell_id, 'c3')
  assert.deepEqual(ids(r).slice(1), ['c0', 'c1', 'c2', 'c4', 'c5', 'c6', 'c7', 'c8', 'c9'])
  assert.deepEqual(cells.map(c => c.cell_id), before)
  assert.deepEqual(selectRunCells(null, by('v'), 'top'), { cells: [], toggle: false, total: 0, hidden: 0, hiddenErrors: 0 })
})

test('hiddenErrors: failed cells outside the Top 10 are counted, shown ones are not', () => {
  const cells = Array.from({ length: 20 }, (_, i) => ({ cell_id: `c${i}`, v: 100 - i, status: 'success' }))
  cells[2].status = 'error'   // inside the Top 10 → not counted
  cells[17].status = 'error'  // fast failure, outside → counted
  cells[19].status = 'error'
  const isErr = (c) => c.status === 'error'
  const r = selectRunCells(cells, by('v'), 'top', isErr)
  assert.equal(r.hidden, 10)
  assert.equal(r.hiddenErrors, 2)
  assert.ok(r.cells.includes(cells[2]))
  // All / small runs hide nothing.
  assert.equal(selectRunCells(cells, by('v'), 'all', isErr).hiddenErrors, 0)
  assert.equal(selectRunCells(cells.slice(0, 15), by('v'), 'top', isErr).hiddenErrors, 0)
  // No predicate → 0 (backwards compatible).
  assert.equal(selectRunCells(cells, by('v'), 'top').hiddenErrors, 0)
})

test('paramTipText: one line when short, one per line when long, never cut', () => {
  assert.equal(paramTipText([]), '')
  assert.equal(paramTipText(null), '')
  assert.equal(
    paramTipText([{ key: 'Shipping market', value: 'UK' }, { key: 'Min units sold', value: '0' }]),
    'Shipping market: UK · Min units sold: 0')
  const long = [{ key: 'Start date', value: '2024-01-01' }, { key: 'End date', value: '2024-12-31' }, { key: 'Region', value: 'eu-west-1' }]
  assert.ok(long.map(e => `${e.key}: ${e.value}`).join(' · ').length > PARAM_TIP_LINE_MAX)
  assert.equal(paramTipText(long), 'Start date: 2024-01-01\nEnd date: 2024-12-31\nRegion: eu-west-1')
  const huge = 'x'.repeat(300)
  assert.equal(paramTipText([{ key: 'Q', value: huge }]), `Q: ${huge}`)
})
