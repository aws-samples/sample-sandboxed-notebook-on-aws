// Unit tests for services/runHistoryReveal.js (run banner -> open the newest run).
// Run with: node --test web/src/services/__tests__/runHistoryReveal.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  requestRunHistoryReveal, takeRunHistoryReveal, takeExpandLatest, latestRunToOpen,
} from '../runHistoryReveal.js'

const T = Date.parse('2025-01-01T10:00:00.000Z')
const iso = (ms) => new Date(ms).toISOString()
const run = (id, ms, cellIds = ['a']) => ({
  run_id: id, finished_at: iso(ms), run_cells: cellIds.map(c => ({ cell_id: c })),
})

test('opens the newest row when it finished at/after the banner run', () => {
  const runs = [run('r2', T + 20), run('r1', T - 60000)]
  assert.equal(latestRunToOpen(runs, { stamp: T }), 'r2')
})

test('record a few ms before the banner stamp still matches', () => {
  assert.equal(latestRunToOpen([run('r2', T - 300)], { stamp: T }), 'r2')
})

test('waits (null) while the newest row is an older run', () => {
  const runs = [run('r1', T - 10000), run('r0', T - 60000)]
  assert.equal(latestRunToOpen(runs, { stamp: T }), null)
})

test('no stamp: newest row as is', () => {
  assert.equal(latestRunToOpen([run('r1', T - 10000)], {}), 'r1')
})

test('empty / missing runs: null', () => {
  assert.equal(latestRunToOpen([], { stamp: T }), null)
  assert.equal(latestRunToOpen(null, {}), null)
})

test('per-cell scope: newest run that includes the cell', () => {
  const runs = [run('r3', T + 5, ['b']), run('r2', T - 9000, ['a', 'b']), run('r1', T - 20000, ['a'])]
  assert.equal(latestRunToOpen(runs, { stamp: T, cellScope: 'a' }), 'r2')
  assert.equal(latestRunToOpen(runs, { stamp: T, cellScope: 'b' }), 'r3')
  assert.equal(latestRunToOpen(runs, { stamp: T, cellScope: 'zz' }), null)
})

test('Run All filter with a non Run All banner: newest listed row (no stamp wait)', () => {
  const runs = [run('ra', T - 30000)]
  assert.equal(latestRunToOpen(runs, { stamp: T, filtered: true, trigger: 'single' }), 'ra')
  assert.equal(latestRunToOpen(runs, { stamp: T, filtered: true, trigger: 'run_all' }), null)
})

test('request parks a one-shot expand request per notebook', () => {
  const events = []
  globalThis.window = new EventTarget()
  window.addEventListener('reveal-run-history', (e) => events.push(e.detail))
  requestRunHistoryReveal(7, { expandLatest: true, stamp: T, trigger: 'run_all' })
  assert.deepEqual(events.at(-1), { notebookId: '7', expandLatest: true })
  assert.equal(takeRunHistoryReveal(), '7')
  assert.equal(takeExpandLatest('8'), null)
  assert.deepEqual(takeExpandLatest(7), { stamp: T, trigger: 'run_all' })
  assert.equal(takeExpandLatest(7), null)
  // A plain reveal (no expandLatest) parks nothing.
  requestRunHistoryReveal(7)
  assert.equal(takeExpandLatest(7), null)
  delete globalThis.window
})
