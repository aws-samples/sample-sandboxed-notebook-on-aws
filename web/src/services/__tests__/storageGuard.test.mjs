// node --test web/src/services/__tests__/storageGuard.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fitCellsForStorage, TRIMMED_NOTE } from '../storageGuard.js'

const cell = (id, htmlLen, extra = {}) => ({ id, code: `code ${id}`, output: null, html: htmlLen ? 'x'.repeat(htmlLen) : null, ...extra })
const tab = (id, cells) => ({ id, name: `t${id}`, _cells: cells })

test('under budget: same array, nothing changed, no copy', () => {
  const tabs = [tab(1, [cell('a', 100), cell('b', 0)])]
  const r = fitCellsForStorage(tabs, 10_000)
  assert.equal(r.tabs, tabs)
  assert.deepEqual(r.trimmedCellIds, [])
})

test('over budget: the largest cell html goes first, and it stops as soon as the save fits', () => {
  const tabs = [tab(1, [cell('small', 1000), cell('big', 50_000)]), tab(2, [cell('mid', 20_000)])]
  const before = JSON.stringify(tabs)
  const r = fitCellsForStorage(tabs, 40_000)
  assert.deepEqual(r.trimmedCellIds, ['big'])
  assert.equal(r.tabs[0]._cells[1].html, null)
  assert.ok(r.tabs[0]._cells[1].output.includes(TRIMMED_NOTE))
  assert.equal(r.tabs[0]._cells[0].html.length, 1000, 'small cell kept')
  assert.equal(r.tabs[1]._cells[0].html.length, 20_000, 'mid cell kept (already fits)')
  assert.ok(JSON.stringify(r.tabs).length <= 40_000)
  assert.equal(JSON.stringify(tabs), before, 'input not mutated')
})

test('several cells are trimmed largest-first until it fits; the note is appended to existing output', () => {
  const tabs = [tab(1, [cell('a', 30_000, { output: 'printed' }), cell('b', 25_000), cell('c', 20_000)])]
  const r = fitCellsForStorage(tabs, 30_000)
  assert.deepEqual(r.trimmedCellIds, ['a', 'b'])
  assert.equal(r.tabs[0]._cells[0].output, `printed\n${TRIMMED_NOTE}`)
  assert.equal(r.tabs[0]._cells[2].html.length, 20_000)
})

test('never touches code, ids or other fields; cells without html are skipped', () => {
  const tabs = [tab(1, [cell('a', 60_000, { imageS3Key: 'k', status: 'success' }), cell('t', 0)])]
  const r = fitCellsForStorage(tabs, 1_000)
  const c = r.tabs[0]._cells[0]
  assert.equal(c.code, 'code a')
  assert.equal(c.imageS3Key, 'k')
  assert.equal(c.status, 'success')
  assert.deepEqual(r.trimmedCellIds, ['a'])
  assert.equal(r.tabs[0]._cells[1], tabs[0]._cells[1])
})

test('nothing trimmable (no html anywhere): returns the input unchanged', () => {
  const tabs = [tab(1, [cell('a', 0, { code: 'y'.repeat(5000) })])]
  const r = fitCellsForStorage(tabs, 100)
  assert.equal(r.tabs, tabs)
  assert.deepEqual(r.trimmedCellIds, [])
})

test('tolerates junk input', () => {
  assert.deepEqual(fitCellsForStorage(null).trimmedCellIds, [])
  assert.deepEqual(fitCellsForStorage([{ id: 1 }, null], 1).trimmedCellIds, [])
})
