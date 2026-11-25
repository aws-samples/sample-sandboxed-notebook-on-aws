// Unit tests for services/instancesPoll.js (one /instances poll in the normal case; B7).
// Run with: node --test web/src/services/__tests__/instancesPoll.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { appInstancesPollActive, panelInstancesTick } from '../instancesPoll.js'

const vm = { 'vm-1': { state: 'RUNNING' } }

test('App poll is active only when a tab is bound to a VM or a session', () => {
  assert.equal(appInstancesPollActive([]), false)
  assert.equal(appInstancesPollActive(undefined), false)
  assert.equal(appInstancesPollActive([{ id: 1 }]), false)
  assert.equal(appInstancesPollActive([{ id: 1, microvmId: 'vm-1' }]), true)
  assert.equal(appInstancesPollActive([{ id: 1 }, { id: 2, sessionId: 's' }]), true)
})

test('normal case (bound tab + parent data): the panel interval skips -> one poll', () => {
  assert.equal(panelInstancesTick({ tabs: [{ microvmId: 'vm-1' }], parentInstances: vm, hasParentRefresh: true }), 'skip')
})

test('no bound tab but parent data (pool spares): the panel refreshes through App', () => {
  assert.equal(panelInstancesTick({ tabs: [{ id: 1 }], parentInstances: vm, hasParentRefresh: true }), 'parent')
})

test('no parent data: the panel fetches its own snapshot', () => {
  assert.equal(panelInstancesTick({ tabs: [], parentInstances: {}, hasParentRefresh: true }), 'local')
  assert.equal(panelInstancesTick({ tabs: [{ microvmId: 'vm-1' }], parentInstances: {}, hasParentRefresh: true }), 'local')
  assert.equal(panelInstancesTick({ tabs: [], parentInstances: vm, hasParentRefresh: false }), 'local')
})

test('one minute with the panel open and a bound tab: 4 polls, not 8', () => {
  const tabs = [{ microvmId: 'vm-1' }]
  let polls = 0
  for (let s = 15; s <= 60; s += 15) {
    if (appInstancesPollActive(tabs)) polls++   // useInstances.maybeFetch
    if (panelInstancesTick({ tabs, parentInstances: vm, hasParentRefresh: true }) !== 'skip') polls++
  }
  assert.equal(polls, 4)
})
