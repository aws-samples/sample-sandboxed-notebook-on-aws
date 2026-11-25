// Unit tests for lineageStatusText (services/lineage.js): the lineage drawer's status line.
// Run with: node --test web/src/services/__tests__/lineageStatusText.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { lineageStatusText } from '../lineage.js'

const RAN = '2025-01-01T00:00:00Z'
const st = (badge, status = 'current', executedAt = RAN) => ({ cellId: badge, badge, status, executedAt })
const CHAIN = ['SQL-1', 'PY-2', 'PY-3', 'PY-6', 'PY-7']

test('all current: "All N cells behind this output are up to date" + ran', () => {
  const r = lineageStatusText(CHAIN.map(b => st(b)), '1 min ago')
  assert.equal(r.summary, 'All 5 cells behind this output are up to date')
  assert.equal(r.reason, '')
  assert.equal(r.text, 'All 5 cells behind this output are up to date · ran 1 min ago')
  assert.equal(r.footer, 'All 5 cells behind this output are up to date')
  assert.doesNotMatch(r.text, /steps/)
})

test('tooltip lists the counted cells in lineage order', () => {
  const r = lineageStatusText(CHAIN.map(b => st(b)), '1 min ago')
  assert.deepEqual(r.counted, CHAIN)
  assert.equal(r.tooltip, 'Counts this cell and every cell it depends on (SQL-1, PY-2, PY-3, PY-6, PY-7). Markdown and unrelated cells are not counted.')
})

test('some not current: "K of N" with the reason', () => {
  const steps = CHAIN.map(b => st(b, b === 'PY-3' ? 'edited' : 'current'))
  const r = lineageStatusText(steps, '1 min ago')
  assert.equal(r.summary, '4 of 5 cells behind this output are up to date')
  assert.equal(r.reason, 'PY-3 edited')
  assert.equal(r.text, '4 of 5 cells behind this output are up to date · PY-3 edited · ran 1 min ago')
  assert.equal(r.footer, '4 of 5 cells behind this output are up to date · PY-3 edited')
})

test('stale, error and never run are grouped by state (errored, edited, not run, stale)', () => {
  const steps = [st('SQL-1', 'stale'), st('PY-2', 'error'), st('PY-3', 'stale', null), st('PY-6', 'stale'), st('PY-7', 'edited')]
  const r = lineageStatusText(steps, null)
  assert.equal(r.summary, 'None of the 5 cells behind this output are up to date')
  assert.equal(r.reason, 'PY-2 errored, PY-7 edited, PY-3 not run, SQL-1, PY-6 stale')
  assert.equal(r.text, 'None of the 5 cells behind this output are up to date · PY-2 errored, PY-7 edited, PY-3 not run, SQL-1, PY-6 stale')
})

test('3 of 5 with one stale and one error', () => {
  const steps = CHAIN.map(b => st(b, b === 'PY-2' ? 'stale' : b === 'PY-6' ? 'error' : 'current'))
  const r = lineageStatusText(steps, 'just now')
  assert.equal(r.text, '3 of 5 cells behind this output are up to date · PY-6 errored, PY-2 stale · ran just now')
})

test('single step (1 of 1): no upstream dependencies', () => {
  const r = lineageStatusText([st('PY-1')], '2 hours ago')
  assert.equal(r.summary, 'This cell has no upstream dependencies')
  assert.equal(r.text, 'This cell has no upstream dependencies · ran 2 hours ago')
  assert.deepEqual(r.counted, ['PY-1'])
  assert.equal(r.tooltip, 'Counts only this cell (PY-1); it does not depend on any other cell. Markdown and unrelated cells are not counted.')
  assert.doesNotMatch(r.text, /1 of 1|cells/)
})

test('single step not current (0 of 1): reason appended, never "1 cells"', () => {
  const r = lineageStatusText([st('PY-1', 'stale', null)], null)
  assert.equal(r.text, 'This cell has no upstream dependencies · PY-1 not run')
  assert.doesNotMatch(r.text, /1 cells/)
})

test('N=2: "Both cells", and "1 of 2" when one is not current', () => {
  assert.equal(lineageStatusText([st('PY-1'), st('PY-2')], '5 min ago').text, 'Both cells behind this output are up to date · ran 5 min ago')
  const r = lineageStatusText([st('PY-1', 'edited'), st('PY-2')], '5 min ago')
  assert.equal(r.text, '1 of 2 cells behind this output are up to date · PY-1 edited · ran 5 min ago')
  assert.match(r.tooltip, /\(PY-1, PY-2\)/)
})

test('no steps and missing badges', () => {
  assert.equal(lineageStatusText([], null).text, 'No cells behind this output')
  assert.equal(lineageStatusText(null).summary, 'No cells behind this output')
  const r = lineageStatusText([{ cellId: '9', badge: '', status: 'current', executedAt: RAN }, st('PY-2')])
  assert.deepEqual(r.counted, ['cell 9', 'PY-2'])
})
