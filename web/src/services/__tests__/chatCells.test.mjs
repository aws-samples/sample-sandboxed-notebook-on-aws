// Unit tests for services/chatCells.js (which cells ride an AI-chat turn).
// Run with: node --test web/src/services/__tests__/chatCells.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { selectChatCells, CHAT_CELL_DEFAULTS } from '../chatCells.js'

const mk = (n, f = (i) => `x${i} = ${i}`) =>
  Array.from({ length: n }, (_, i) => ({ id: `c${i}`, type: 'code', code: f(i), output: '', error: '' }))

test('defaults', () => {
  assert.equal(CHAT_CELL_DEFAULTS.maxCells, 60)
  assert.equal(CHAT_CELL_DEFAULTS.codeChars, 300)
})

test('<= 60 small cells: all of them, with notebook indexes, in order', () => {
  const out = selectChatCells(mk(12), 4, null)
  assert.equal(out.length, 12)
  assert.deepEqual(out.map(c => c.index), [...Array(12).keys()])
  assert.equal(out[4].active, true)
  assert.equal(out.filter(c => c.active).length, 1)
})

test('100 cells, active 70: window + DAG writer of a window read, <= 60, sorted', () => {
  const cells = mk(100)
  cells[3].code = "reviews = raw.rename(columns={'productId': 'product_id'})"
  cells[70].code = "reviews.groupby('product_id').size()"
  const dag = {
    schema: 1,
    nodes: cells.map((c, i) => ({
      cell_id: c.id,
      reads: i === 70 ? ['reviews'] : [],
      writes: i === 3 ? ['reviews'] : [`x${i}`],
    })),
  }
  const out = selectChatCells(cells, 70, dag)
  const idx = out.map(c => c.index)
  assert.ok(out.length <= 60)
  assert.ok(idx.includes(70))
  for (let i = 55; i <= 85; i++) assert.ok(idx.includes(i), `window cell ${i}`)
  assert.ok(idx.includes(3), 'DAG writer of `reviews` included')
  assert.deepEqual(idx, [...idx].sort((a, b) => a - b))
  assert.equal(out.find(c => c.index === 70).active, true)
})

test('regex fallback without a DAG finds the writer', () => {
  const cells = mk(100, (i) => `print(${i})`)
  cells[2].code = 'reviews = load()'
  cells[90].code = "reviews['product_id']"
  const out = selectChatCells(cells, 90, null, { maxCells: 40 })
  const idx = out.map(c => c.index)
  assert.ok(idx.includes(2))
  assert.ok(idx.includes(90))
  assert.ok(out.length <= 40)
})

test('character budget is enforced with long code', () => {
  const cells = mk(100, () => 'y'.repeat(1000))
  const out = selectChatCells(cells, 50, null)
  const size = out.reduce((n, c) => n + c.code.length + c.output.length + c.error.length, 0)
  assert.ok(size <= CHAT_CELL_DEFAULTS.budgetChars, `size ${size}`)
  assert.ok(out.some(c => c.index === 50 && c.active))
})

test('null / out-of-range active → index 0 is active', () => {
  const big = mk(80)
  let out = selectChatCells(big, null, null)
  assert.equal(out[0].index, 0)
  assert.equal(out[0].active, true)
  out = selectChatCells(mk(3), 99, null)
  assert.equal(out[0].active, true)
  assert.deepEqual(selectChatCells([], 0, null), [])
  assert.deepEqual(selectChatCells(null, 0, null), [])
})

test('slices code to 300 and output/error to 200', () => {
  const cells = [{ type: 'code', code: 'a'.repeat(500), output: 'o'.repeat(500), error: 'e'.repeat(500) },
    { type: 'markdown', code: '# hi' }]
  const out = selectChatCells(cells, 0, null)
  assert.equal(out[0].code.length, 300)
  assert.equal(out[0].output.length, 200)
  assert.equal(out[0].error.length, 200)
  assert.equal(out[1].type, 'markdown')
  assert.equal(out[1].output, '')
})
