// Unit tests for services/errorDetails.js (ErrorBoundary "Copy details").
// Run with: node --test web/src/services/__tests__/errorDetails.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { errorDetails, saveLastError, LAST_ERROR_KEY } from '../errorDetails.js'

const AT = new Date('2026-11-19T01:00:00Z')

test('errorDetails: message, scope, time, stack and component stack', () => {
  const err = new TypeError("Cannot read properties of null (reading 'slice')")
  err.stack = "TypeError: Cannot read properties of null (reading 'slice')\n    at ConnectionPanel (ConnectionPanel.jsx:801:70)"
  const text = errorDetails(err, { componentStack: '\n    at ConnectionPanel\n    at ErrorBoundary' }, { scope: 'connect', at: AT, url: 'http://localhost:5173/' })
  assert.match(text, /^Error: Cannot read properties of null \(reading 'slice'\)$/m)
  assert.match(text, /^Scope: connect$/m)
  assert.match(text, /^Time: 2026-11-19T01:00:00.000Z$/m)
  assert.match(text, /^URL: http:\/\/localhost:5173\/$/m)
  assert.match(text, /at ConnectionPanel \(ConnectionPanel.jsx:801:70\)/)
  assert.match(text, /Component stack:\nat ConnectionPanel\n    at ErrorBoundary/)
})

test('errorDetails: never throws on odd values', () => {
  for (const e of [undefined, null, 'boom', 42, {}, { message: '' }]) {
    assert.doesNotThrow(() => errorDetails(e, undefined, { at: AT }))
  }
  assert.match(errorDetails('boom', null, { at: AT }), /^Error: boom$/m)
  assert.match(errorDetails(undefined, null, { at: AT }), /^Error: Unknown error$/m)
})

test('saveLastError: stores the report; a broken storage is ignored', () => {
  const store = {}
  saveLastError('x', { setItem: (k, v) => { store[k] = v } })
  assert.equal(store[LAST_ERROR_KEY], 'x')
  assert.doesNotThrow(() => saveLastError('x', { setItem: () => { throw new Error('quota') } }))
})
