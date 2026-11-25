// node --test web/src/services/__tests__/outputExpansion.test.mjs
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { clearExpansion, getExpanded, keyFor, setExpanded } from '../outputExpansion.js'

beforeEach(() => clearExpansion())

test('defaults come from the fallback until the user toggles', () => {
  const k = keyFor('c1', 2, 'abc')
  assert.equal(getExpanded(k, false), false)
  assert.equal(getExpanded(k, true), true)
})

test('an explicit choice is remembered, in both directions, and overrides the default', () => {
  const k = keyFor('c1', 2, 'abc')
  setExpanded(k, true)
  assert.equal(getExpanded(k, false), true)
  setExpanded(k, false)
  assert.equal(getExpanded(k, true), false)
})

test('keys are per cell, per item position and per content hash (a re-run with new content resets)', () => {
  setExpanded(keyFor('c1', 2, 'abc'), true)
  assert.equal(getExpanded(keyFor('c2', 2, 'abc'), false), false)
  assert.equal(getExpanded(keyFor('c1', 3, 'abc'), false), false)
  assert.equal(getExpanded(keyFor('c1', 2, 'zzz'), false), false)
  assert.equal(getExpanded(keyFor('c1', 2, 'abc'), false), true)
})

test('bounded: the oldest entries are dropped past the cap', () => {
  for (let i = 0; i < 2100; i++) setExpanded(keyFor('c', i, 's'), true)
  assert.equal(getExpanded(keyFor('c', 0, 's'), false), false)
  assert.equal(getExpanded(keyFor('c', 2099, 's'), false), true)
})
