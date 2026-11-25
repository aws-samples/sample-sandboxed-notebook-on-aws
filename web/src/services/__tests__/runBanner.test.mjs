// Unit tests for services/runBanner.js (rules + text of the Outline run banner).
// Run with: node --test web/src/services/__tests__/runBanner.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildBanner, buildRunBanner, bannerText, bannerVisible, cellsSignature, clearBannerPatch,
} from '../runBanner.js'

const T0 = 1_000_000
const cell = (id, status, extra = {}) => ({ id, type: 'code', code: `print(${id})`, status, finishedAt: T0 + 100, ...extra })
const build = (cells, ranIds = cells.map(c => c.id)) =>
  buildBanner({ cells, ranIds, startedAt: T0, finishedAt: T0 + 26_500 })

test('clean run: counts, elapsed time and success tone', () => {
  const b = build([cell('a', 'success'), cell('b', 'success'), cell('c', 'success')])
  assert.equal(b.executed, 3)
  assert.equal(b.errors, 0)
  assert.equal(b.stopped, 0)
  assert.equal(b.elapsedMs, 26_500)
  assert.equal(b.tone, 'success')
  assert.equal(b.stamp, T0 + 26_500)
  assert.equal(b.trigger, 'run_all')
  assert.equal(b.firstErrorId, null)
})

test('run with errors: error tone, failed cells still count as executed', () => {
  const b = build([cell('a', 'success'), cell('b', 'error'), cell('c', 'error')])
  assert.equal(b.executed, 3)
  assert.equal(b.errors, 2)
  assert.equal(b.tone, 'error')
  assert.equal(b.firstErrorId, 'b')
})

test('firstErrorId follows notebook order, not finish order', () => {
  const cells = [cell('a', 'success'), cell('b', 'error'), cell('c', 'error')]
  assert.equal(build(cells, ['c', 'b', 'a']).firstErrorId, 'b')
})

test('stopped run: some cells ran, the rest were interrupted or reset to idle', () => {
  const b = build([cell('a', 'success'), cell('b', 'interrupted'), cell('c', 'idle', { finishedAt: null })])
  assert.equal(b.executed, 1)
  assert.equal(b.stopped, 2)
  assert.equal(b.tone, 'warning')
})

test('errors win over stopped for the tone', () => {
  const b = build([cell('a', 'error'), cell('b', 'interrupted')])
  assert.equal(b.tone, 'error')
  assert.equal(b.stopped, 1)
})

test('stopped before any cell finished: nothing to show', () => {
  assert.equal(build([cell('a', 'interrupted'), cell('b', 'idle', { finishedAt: null })]), null)
  assert.equal(build([]), null)
})

test('statuses from an earlier run do not count (refused Run All)', () => {
  const old = { finishedAt: T0 - 5000 }
  assert.equal(build([cell('a', 'success', old), cell('b', 'error', old)]), null)
})

test('cells outside the Run All are ignored', () => {
  const b = build([cell('a', 'success'), cell('x', 'error')], ['a'])
  assert.equal(b.executed, 1)
  assert.equal(b.errors, 0)
})

test('text: clean, singular, errors, stopped', () => {
  assert.deepEqual(bannerText(build([cell('a', 'success'), cell('b', 'success')])), {
    lead: 'Run All finished: 2 cells in 26.5 s', detail: '0 errors', action: 'View run history', secondary: null,
  })
  const one = bannerText(buildBanner({ cells: [cell('a', 'error')], ranIds: ['a'], startedAt: T0, finishedAt: T0 + 400 }))
  assert.equal(one.lead, 'Run All finished: 1 cell in 400 ms')
  assert.equal(one.detail, '1 error')
  assert.equal(one.secondary, 'Go to first error')
  const stopped = bannerText(build([cell('a', 'success'), cell('b', 'error'), cell('c', 'interrupted')]))
  assert.equal(stopped.lead, 'Run All stopped: 2 cells in 26.5 s')
  assert.equal(stopped.detail, '1 error, 1 not run')
  const long = bannerText(buildBanner({ cells: [cell('a', 'success')], ranIds: ['a'], startedAt: T0, finishedAt: T0 + 96_000 }))
  assert.equal(long.lead, 'Run All finished: 1 cell in 1 min 36 s')
})

test('clearBannerPatch removes the banner from a tab', () => {
  const tab = { id: 'A', _runBanner: build([cell('a', 'success')]) }
  assert.equal({ ...tab, ...clearBannerPatch() }._runBanner, null)
})

test('bannerVisible: stays through run status changes, hides after any cell-list edit', () => {
  const cells = [cell('a', 'success'), cell('b', 'success')]
  const b = build(cells)
  assert.equal(bannerVisible(b, cells), true)
  // selecting/running/output changes do not alter the signature
  assert.equal(bannerVisible(b, cells.map(c => ({ ...c, status: 'idle', output: 'x', executionTime: 5 }))), true)
  // code edit
  assert.equal(bannerVisible(b, [{ ...cells[0], code: 'print(1) ' }, cells[1]]), false)
  // add, delete, move, convert
  assert.equal(bannerVisible(b, [...cells, cell('c', 'idle')]), false)
  assert.equal(bannerVisible(b, [cells[0]]), false)
  assert.equal(bannerVisible(b, [cells[1], cells[0]]), false)
  assert.equal(bannerVisible(b, [{ ...cells[0], type: 'sql' }, cells[1]]), false)
  assert.equal(bannerVisible(null, cells), false)
})

test('per notebook: a banner on tab A is not shown for tab B cells, and returns on A', () => {
  const aCells = [cell('a1', 'success')]
  const bCells = [cell('b1', 'success')]
  const tabA = { id: 'A', _runBanner: build(aCells) }
  const tabB = { id: 'B' }
  assert.equal(bannerVisible(tabB._runBanner, bCells), false)
  assert.equal(bannerVisible(tabA._runBanner, aCells), true)
})

test('cellsSignature is stable and order-sensitive', () => {
  const cs = [cell('a', 'idle'), cell('b', 'idle')]
  assert.equal(cellsSignature(cs), cellsSignature(cs.map(c => ({ ...c }))))
  assert.notEqual(cellsSignature(cs), cellsSignature([...cs].reverse()))
  assert.equal(cellsSignature(null), '')
})

// ---- buildRunBanner: runs other than Run All -------------------------------------
const results = (entries) => new Map(entries.map(([id, status]) => [id, { status }]))
const run = (entries, { ranIds = entries.map(e => e[0]), ms = 3_000, trigger = 'single', cells } = {}) =>
  buildRunBanner({
    results: results(entries), ranIds, startedAt: T0, finishedAt: T0 + ms, trigger,
    cells: cells || ranIds.map(id => cell(id, 'idle')),
  })

test('buildRunBanner: a run of 2 s or more gets a banner', () => {
  const b = run([['a', 'success'], ['b', 'success']], { trigger: 'run_from' })
  assert.equal(b.executed, 2)
  assert.equal(b.errors, 0)
  assert.equal(b.stopped, 0)
  assert.equal(b.tone, 'success')
  assert.equal(b.trigger, 'run_from')
  assert.equal(b.elapsedMs, 3_000)
  assert.equal(b.firstErrorId, null)
  assert.ok(b.sig)
})

test('buildRunBanner: an instant clean run gets none; an instant failed or stopped run does', () => {
  assert.equal(run([['a', 'success']], { ms: 400 }), null)
  assert.equal(run([['a', 'success']], { ms: 1_999 }), null)
  assert.ok(run([['a', 'success']], { ms: 2_000 }))
  const failed = run([['a', 'error']], { ms: 300 })
  assert.equal(failed.tone, 'error')
  assert.equal(failed.firstErrorId, 'a')
  const stopped = run([['a', 'success'], ['b', 'interrupted']], { ms: 300 })
  assert.equal(stopped.tone, 'warning')
  assert.equal(stopped.stopped, 1)
})

test('buildRunBanner: queued cells that never reported count as not run', () => {
  const b = run([['a', 'success']], { ranIds: ['a', 'b', 'c'], ms: 500 })
  assert.equal(b.executed, 1)
  assert.equal(b.stopped, 2)
})

test('buildRunBanner: nothing finished gives no banner', () => {
  assert.equal(run([['a', 'interrupted']], { ms: 5_000 }), null)
  assert.equal(run([], { ranIds: ['a'], ms: 5_000 }), null)
})

test('buildRunBanner: firstErrorId is the first failed cell in notebook order', () => {
  const cells = [cell('a', 'idle'), cell('b', 'idle'), cell('c', 'idle')]
  const b = run([['c', 'error'], ['a', 'success'], ['b', 'error']], { cells, ranIds: ['c', 'a', 'b'] })
  assert.equal(b.firstErrorId, 'b')
  assert.equal(b.errors, 2)
})

test('text for other runs: "Ran", "Run stopped", errors and the secondary action', () => {
  const clean = bannerText(run([['a', 'success'], ['b', 'success'], ['c', 'success']], { ms: 4_200 }))
  assert.deepEqual(clean, {
    lead: 'Ran 3 cells in 4.2 s', detail: '0 errors', action: 'View run history', secondary: null,
  })
  const one = bannerText(run([['a', 'error']], { ms: 2_500, trigger: 'reactive' }))
  assert.equal(one.lead, 'Ran 1 cell in 2.5 s')
  assert.equal(one.detail, '1 error')
  assert.equal(one.secondary, 'Go to first error')
  const stopped = bannerText(run([['a', 'success'], ['b', 'interrupted']], { ms: 2_500, trigger: 'run_from' }))
  assert.equal(stopped.lead, 'Run stopped: 1 cell in 2.5 s')
  assert.equal(stopped.detail, '0 errors, 1 not run')
})
