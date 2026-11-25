// Unit tests for services/cellReconcile.js — the store → local reconcile that
// useNotebookCells' persist effect applies as a FUNCTIONAL state update.
// Run with: node --test web/src/services/__tests__/cellReconcile.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reconcileCellsFromStore } from '../cellReconcile.js'

// Minimal React-like state queue: setState(value | fn) applied in call order.
function stateQueue(initial) {
  let state = initial
  return {
    get: () => state,
    set: (u) => { state = typeof u === 'function' ? u(state) : u },
  }
}
const finalize = (c, n) => ({ ...c, status: 'success', executionNumber: n, finishedAt: 1000 + n, startedAt: null })

test('regression: output-less terminal cell + next cell finalized after the render → next ends finalized', () => {
  // Render N: A finished with NO output (`x = 1`), B promoted to running. patchCell
  // writes local and store with separate objects, so store A !== local A.
  const a = finalize({ id: 'a', type: 'code', code: 'x = 1', output: null }, 1)
  const b = { id: 'b', type: 'code', code: 'print(x)', status: 'running', output: null, startedAt: 5 }
  const renderCells = [a, b]
  const renderStore = [{ ...a }, { ...b }]
  const st = stateQueue(renderCells)
  // After render N, before its effect runs: B's result frame finalizes B.
  st.set(prev => prev.map(c => (c.id === 'b' ? finalize({ ...c, output: '2' }, 2) : c)))
  // The persist effect of render N: decision against the render, functional update.
  assert.notEqual(reconcileCellsFromStore(renderCells, renderStore), renderCells, 'render N wants to adopt A')
  st.set(prev => reconcileCellsFromStore(prev, renderStore, renderCells))
  const [A, B] = st.get()
  assert.equal(A, renderStore[0], 'A adopted from the store')
  assert.equal(B.status, 'success', 'B is finalized, not running')
  assert.equal(B.executionNumber, 2)
  assert.equal(B.output, '2')
})

test('the old non-functional merge reproduced the bug (sanity)', () => {
  const a = finalize({ id: 'a', code: 'x = 1', output: null }, 1)
  const b = { id: 'b', code: 'print(x)', status: 'running', output: null }
  const renderCells = [a, b]
  const renderStore = [{ ...a }, { ...b }]
  const st = stateQueue(renderCells)
  st.set(prev => prev.map(c => (c.id === 'b' ? finalize({ ...c, output: '2' }, 2) : c)))
  st.set(reconcileCellsFromStore(renderCells, renderStore))  // value from the stale render
  assert.equal(st.get()[1].status, 'running')
})

test('store copy still wins for a cell genuinely terminal in the store (remount)', () => {
  // Local seeded at mount (stale running); the store got the result while unmounted.
  const local = [{ id: 'a', status: 'running', output: null }, { id: 'b', status: 'idle', output: null }]
  const store = [{ id: 'a', status: 'success', output: 'hi', executionNumber: 3 }, local[1]]
  const out = reconcileCellsFromStore(local, store)
  assert.equal(out[0], store[0])
  assert.equal(out[1], local[1])
})

test('a live cell newer than the render (promoted to running) is not overwritten by an old store result', () => {
  const old = { id: 'a', status: 'idle', output: null }
  const renderCells = [old]
  const renderStore = [{ id: 'a', status: 'success', output: 'previous run' }]
  const live = [{ ...old, status: 'running', startedAt: 9 }]
  const out = reconcileCellsFromStore(live, renderStore, renderCells)
  assert.equal(out, live, 'unchanged identity')
  assert.equal(out[0].status, 'running')
})

test('self-heal: running + finishedAt is demoted to its terminal status', () => {
  const cells = [
    { id: 'a', status: 'running', finishedAt: 1, error: null, output: 'ok' },
    { id: 'b', status: 'running', finishedAt: 2, error: 'boom' },
  ]
  const out = reconcileCellsFromStore(cells, cells)
  assert.deepEqual(out.map(c => c.status), ['success', 'error'])
})

test('no-op returns the same array (no re-render); shape mismatch is a no-op', () => {
  const cells = [{ id: 'a', status: 'success', output: 'x' }]
  assert.equal(reconcileCellsFromStore(cells, cells), cells)
  assert.equal(reconcileCellsFromStore(cells, [{ ...cells[0] }]), cells)
  const mismatch = [{ id: 'a', status: 'running', output: null }]
  assert.equal(reconcileCellsFromStore(mismatch, [{ id: 'a', status: 'success', output: 'y' }, { id: 'b' }]), mismatch)
  assert.equal(reconcileCellsFromStore(mismatch, null), mismatch)
})

// Chart persistence: a cell whose only result is a persisted chart key counts as
// having a result (hasImageOutput), so a key-only local cell is not overwritten by
// an older store copy, and a key-only store copy is adopted over a stale local one.
const IMG_KEY = `notebook-outputs/3/a/${'a'.repeat(64)}.png`
test('hasResult: a key-only (persisted chart) local cell is not stale', () => {
  const local = [{ id: 'a', status: 'idle', imageS3Key: IMG_KEY }]
  const store = [{ id: 'a', status: 'success', output: 'old' }]
  assert.equal(reconcileCellsFromStore(local, store), local)
})
test('hasResult: a key-only store copy is adopted over an output-less local cell', () => {
  const local = [{ id: 'a', status: 'idle' }]
  const store = [{ id: 'a', status: 'idle', imageS3Key: IMG_KEY }]
  const out = reconcileCellsFromStore(local, store)
  assert.equal(out[0], store[0])
})
