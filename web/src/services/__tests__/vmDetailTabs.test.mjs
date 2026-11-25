// Unit tests for services/vmDetailTabs.js (tabs in the expanded Live MicroVMs card).
// Run with: node --test web/src/services/__tests__/vmDetailTabs.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  VM_DETAIL_TABS, VM_TAB_STORAGE_KEY, DEFAULT_VM_TAB,
  visibleVmTabs, parseStoredVmTab, resolveVmTab, nextVmTabIndex,
} from '../vmDetailTabs.js'

const ids = (tabs) => tabs.map(t => t.id)

test('constants: Overview is the default and first tab; key follows microvm-* naming', () => {
  assert.equal(DEFAULT_VM_TAB, 'overview')
  assert.equal(VM_DETAIL_TABS[0].id, 'overview')
  assert.equal(VM_TAB_STORAGE_KEY, 'microvm-vm-detail-tab')
  assert.deepEqual(ids(VM_DETAIL_TABS), ['overview', 'cost', 'resources'])
})

test('visibleVmTabs: Resources is always shown; Cost only with cost data', () => {
  assert.deepEqual(ids(visibleVmTabs({ hasCost: true, hasResources: true })), ['overview', 'cost', 'resources'])
  assert.deepEqual(ids(visibleVmTabs({ hasCost: true, hasResources: false })), ['overview', 'cost', 'resources'])
  assert.deepEqual(ids(visibleVmTabs({ hasCost: false, hasResources: true })), ['overview', 'resources'])
  assert.deepEqual(ids(visibleVmTabs({ hasCost: false })), ['overview', 'resources'])
  assert.deepEqual(ids(visibleVmTabs()), ['overview', 'resources'])
})

test('visibleVmTabs: truthy/falsy values (objects, undefined) are coerced', () => {
  assert.deepEqual(ids(visibleVmTabs({ hasCost: { total_cost_usd: 1 }, hasResources: undefined })), ['overview', 'cost', 'resources'])
  assert.deepEqual(ids(visibleVmTabs({ hasCost: undefined })), ['overview', 'resources'])
})

test('parseStoredVmTab: first time (null) and unknown/garbage values fall back to Overview', () => {
  assert.equal(parseStoredVmTab(null), 'overview')
  assert.equal(parseStoredVmTab(undefined), 'overview')
  assert.equal(parseStoredVmTab(''), 'overview')
  assert.equal(parseStoredVmTab('lifecycle'), 'overview')   // a removed/old tab id
  assert.equal(parseStoredVmTab('COST'), 'overview')        // ids are case-sensitive
  assert.equal(parseStoredVmTab('{"cost":true}'), 'overview')
  assert.equal(parseStoredVmTab(42), 'overview')
})

test('parseStoredVmTab: known ids are kept', () => {
  for (const t of VM_DETAIL_TABS) assert.equal(parseStoredVmTab(t.id), t.id)
})

test('resolveVmTab: remembered tab is used when visible for this VM', () => {
  const vis = visibleVmTabs({ hasCost: true, hasResources: true })
  assert.equal(resolveVmTab('cost', vis), 'cost')
  assert.equal(resolveVmTab('resources', vis), 'resources')
  assert.equal(resolveVmTab('overview', vis), 'overview')
})

test('resolveVmTab: remembered tab hidden for this VM shows Overview', () => {
  assert.equal(resolveVmTab('cost', visibleVmTabs({ hasCost: false })), 'overview')
  assert.equal(resolveVmTab('resources', visibleVmTabs({})), 'resources')   // always visible now
})

test('resolveVmTab: unknown selection or empty list falls back to Overview; accepts id strings', () => {
  assert.equal(resolveVmTab('nope', visibleVmTabs({ hasCost: true })), 'overview')
  assert.equal(resolveVmTab('cost', []), 'overview')
  assert.equal(resolveVmTab('cost', null), 'overview')
  assert.equal(resolveVmTab('cost', ['overview', 'cost']), 'cost')
})

test('nextVmTabIndex: arrows wrap, Home/End jump, other keys ignored', () => {
  assert.equal(nextVmTabIndex('ArrowRight', 0, 3), 1)
  assert.equal(nextVmTabIndex('ArrowRight', 2, 3), 0)
  assert.equal(nextVmTabIndex('ArrowLeft', 0, 3), 2)
  assert.equal(nextVmTabIndex('ArrowLeft', 2, 3), 1)
  assert.equal(nextVmTabIndex('Home', 2, 3), 0)
  assert.equal(nextVmTabIndex('End', 0, 3), 2)
  assert.equal(nextVmTabIndex('Enter', 0, 3), null)
  assert.equal(nextVmTabIndex('ArrowDown', 0, 3), null)
  assert.equal(nextVmTabIndex('ArrowRight', 0, 1), 0)
  assert.equal(nextVmTabIndex('ArrowRight', 0, 0), null)
})
