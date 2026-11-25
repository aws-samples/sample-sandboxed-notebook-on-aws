// Unit tests for services/vmOwnership.js (a tab may only use its own session's VM).
// Run with: node --test web/src/services/__tests__/vmOwnership.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  instanceSessionMismatch, staleVmRepoint, terminateTargetForTab,
} from '../vmOwnership.js'

const tabA = { id: 't1', name: 'NB A', sessionId: 'sess-A', microvmId: 'vm-A' }

test('instanceSessionMismatch: only when both set and different', () => {
  assert.equal(instanceSessionMismatch({ session_id: 'sess-B' }, tabA), true)
  assert.equal(instanceSessionMismatch({ session_id: 'sess-A' }, tabA), false)
  // Either side unknown → not a mismatch (no flapping, unattached VMs unchanged).
  assert.equal(instanceSessionMismatch({ session_id: '' }, tabA), false)
  assert.equal(instanceSessionMismatch({}, tabA), false)
  assert.equal(instanceSessionMismatch({ session_id: 'sess-B' }, { sessionId: '' }), false)
  assert.equal(instanceSessionMismatch({ session_id: '' }, { sessionId: '' }), false)
  assert.equal(instanceSessionMismatch(undefined, tabA), false)
})

const instMap = {
  'vm-B': { state: 'RUNNING', session_id: 'sess-B', name: 'other notebook' },
  'vm-A': { state: 'RUNNING', session_id: 'sess-A', memory_mib: 2048 },
  'vm-spare': { state: 'RUNNING', session_id: null },
  'vm-A-old': { state: 'SUSPENDED', session_id: 'sess-A' },
}

test('staleVmRepoint: matching or unknown sessions → null (no action)', () => {
  assert.equal(staleVmRepoint(instMap, { sessionId: 'sess-A', microvmId: 'vm-A' }), null)
  assert.equal(staleVmRepoint(instMap, { sessionId: 'sess-A', microvmId: 'vm-spare' }), null)
  assert.equal(staleVmRepoint(instMap, { sessionId: '', microvmId: 'vm-B' }), null)
  assert.equal(staleVmRepoint(instMap, { sessionId: 'sess-A', microvmId: 'vm-gone' }), null)
  assert.equal(staleVmRepoint(instMap, { sessionId: 'sess-A', microvmId: null }), null)
})

test('staleVmRepoint: foreign id re-points to the session\'s own alive VM', () => {
  const r = staleVmRepoint(instMap, { sessionId: 'sess-A', microvmId: 'vm-B' })
  assert.ok(r)
  assert.equal(r.repoint[1].session_id, 'sess-A')
  assert.ok(['vm-A', 'vm-A-old'].includes(r.repoint[0]))
})

test('staleVmRepoint: foreign id and no own VM → repoint null', () => {
  const inst = { 'vm-B': { state: 'RUNNING', session_id: 'sess-B' },
    'vm-A-dead': { state: 'TERMINATED', session_id: 'sess-A' } }
  assert.deepEqual(staleVmRepoint(inst, { sessionId: 'sess-A', microvmId: 'vm-B' }), { repoint: null })
})

test('staleVmRepoint: after re-pointing, the next poll is a no-op (no flap)', () => {
  const tab = { sessionId: 'sess-A', microvmId: 'vm-B' }
  const r = staleVmRepoint(instMap, tab)
  assert.equal(staleVmRepoint(instMap, { ...tab, microvmId: r.repoint[0] }), null)
})

test('terminateTargetForTab: own / unknown → tab id; foreign → own VM or null', () => {
  assert.equal(terminateTargetForTab(instMap, { sessionId: 'sess-A', microvmId: 'vm-A' }), 'vm-A')
  assert.equal(terminateTargetForTab(instMap, { sessionId: 'sess-A', microvmId: 'vm-spare' }), 'vm-spare')
  assert.equal(terminateTargetForTab({}, { sessionId: 'sess-A', microvmId: 'vm-x' }), 'vm-x')
  assert.equal(terminateTargetForTab(instMap, { sessionId: 'sess-A', microvmId: null }), null)
  const own = terminateTargetForTab(instMap, { sessionId: 'sess-A', microvmId: 'vm-B' })
  assert.ok(['vm-A', 'vm-A-old'].includes(own))
  assert.equal(terminateTargetForTab({ 'vm-B': { state: 'RUNNING', session_id: 'sess-B' } },
    { sessionId: 'sess-A', microvmId: 'vm-B' }), null)
})
