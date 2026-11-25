// Unit tests for services/vmLaunch.js (shared /launch body fields).
// Run with: node --test web/src/services/__tests__/vmLaunch.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildLaunchPayload } from '../vmLaunch.js'

const tab = { id: '42', name: 'Sales report', sessionId: 'sess-1' }

test('restore payload sends name (not notebookName) and restoreFromSession', () => {
  const p = buildLaunchPayload(tab, { restore: true })
  assert.equal(p.name, 'Sales report')
  assert.equal('notebookName' in p, false)
  assert.equal(p.notebookId, '42')
  assert.equal(p.sessionId, 'sess-1')
  assert.equal(p.restoreFromSession, 'sess-1')
  assert.equal('checkpointEnabled' in p, false)
})

test('plain launch carries no restoreFromSession', () => {
  assert.equal('restoreFromSession' in buildLaunchPayload(tab), false)
})

test('restore without a session id sends no restoreFromSession', () => {
  const p = buildLaunchPayload({ id: '7', name: 'n' }, { restore: true })
  assert.equal('restoreFromSession' in p, false)
})

test('the body serializes with name as the JSON key', () => {
  const body = JSON.parse(JSON.stringify({ ...buildLaunchPayload(tab, { restore: true }), memoryMiB: 2048 }))
  assert.deepEqual(Object.keys(body).sort(), ['memoryMiB', 'name', 'notebookId', 'restoreFromSession', 'sessionId'])
})
