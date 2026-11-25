// Unit tests for services/cellOutput.js (chart-output gates, the run-completion
// autosave signature, id restore) and copyCellOutput's persisted-chart note.
// Run with: node --test web/src/services/__tests__/cellOutput.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { hasImageOutput, restoreCellIds, runCompletionSignature } from '../cellOutput.js'
import { copyCellOutput } from '../exportCellOutput.js'

const KEY = `notebook-outputs/3/c1/${'a'.repeat(64)}.png`

test('hasImageOutput: fresh image or persisted key', () => {
  assert.equal(hasImageOutput({ image: 'data:image/png;base64,AA' }), true)
  assert.equal(hasImageOutput({ imageS3Key: KEY }), true)
  assert.equal(hasImageOutput({ image: null, imageS3Key: null }), false)
  assert.equal(hasImageOutput({}), false)
  assert.equal(hasImageOutput(null), false)
})

test('copyCellOutput: a key-only chart gets the image note and performs no fetch', async () => {
  let copied = null
  const origFetch = globalThis.fetch
  let fetched = false
  globalThis.fetch = async () => { fetched = true; throw new Error('no fetch expected') }
  Object.defineProperty(globalThis.navigator, 'clipboard', {
    configurable: true, value: { writeText: async (t) => { copied = t } },
  })
  try {
    assert.equal(await copyCellOutput({ imageS3Key: KEY }), true)
    assert.match(copied, /image output/)
    assert.equal(fetched, false)
    copied = null
    assert.equal(await copyCellOutput({ output: 'hi', imageS3Key: KEY }), true)
    assert.equal(copied, 'hi')
  } finally {
    globalThis.fetch = origFetch
    delete globalThis.navigator.clipboard
  }
})

const tab = (cells) => [{ id: 1, _cells: cells }]

test('runCompletionSignature: changes once per run (executedAt + key in one patch)', () => {
  const before = runCompletionSignature(tab([{ id: 'c1', executedAt: null, imageS3Key: null }]))
  const after = runCompletionSignature(tab([{ id: 'c1', executedAt: 't1', imageS3Key: KEY }]))
  assert.notEqual(before, after)
  // Status flips / output streaming do not change it.
  assert.equal(runCompletionSignature(tab([{ id: 'c1', executedAt: 't1', imageS3Key: KEY, status: 'running', output: 'x' }])), after)
})

test('runCompletionSignature: a clear that nulls the key changes it (the null gets saved)', () => {
  const a = runCompletionSignature(tab([{ id: 'c1', executedAt: 't1', imageS3Key: KEY }]))
  const b = runCompletionSignature(tab([{ id: 'c1', executedAt: 't1', imageS3Key: null }]))
  assert.notEqual(a, b)
})

test('runCompletionSignature: base64 image never affects it', () => {
  const a = runCompletionSignature(tab([{ id: 'c1', executedAt: 't1', imageS3Key: KEY }]))
  const b = runCompletionSignature(tab([{ id: 'c1', executedAt: 't1', imageS3Key: KEY, image: 'data:image/png;base64,AA' }]))
  assert.equal(a, b)
})

test('restoreCellIds: preserved id keeps its key; re-minted id drops it', () => {
  let n = 0
  const mint = () => `new-${++n}`
  const out = restoreCellIds([
    { id: 'a', imageS3Key: KEY },
    { id: 'a', imageS3Key: KEY },   // duplicate → re-minted
    { imageS3Key: KEY },             // missing → re-minted
  ], mint)
  assert.deepEqual(out.map(c => c.id), ['a', 'new-1', 'new-2'])
  assert.equal(out[0].imageS3Key, KEY)
  assert.equal(out[1].imageS3Key, null)
  assert.equal(out[2].imageS3Key, null)
})
