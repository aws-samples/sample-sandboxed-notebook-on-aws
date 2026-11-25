// Unit tests for services/vmSectionCollapse.js (collapsible VM card sections).
// Run with: node --test web/src/services/__tests__/vmSectionCollapse.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { VM_SECTIONS, sectionKey, isSectionCollapsed, toggleSection } from '../vmSectionCollapse.js'

test('every section is expanded by default', () => {
  for (const s of VM_SECTIONS) assert.equal(isSectionCollapsed(new Set(), 'vm-a', s), false)
  assert.equal(isSectionCollapsed(null, 'vm-a', 'history'), false)
})

test('toggleSection flips one (vm, section) pair and does not mutate the input', () => {
  const empty = new Set()
  const one = toggleSection(empty, 'vm-a', 'history')
  assert.equal(empty.size, 0)
  assert.equal(isSectionCollapsed(one, 'vm-a', 'history'), true)
  assert.equal(isSectionCollapsed(one, 'vm-a', 'environment'), false)
  assert.equal(isSectionCollapsed(one, 'vm-b', 'history'), false)
  const back = toggleSection(one, 'vm-a', 'history')
  assert.equal(isSectionCollapsed(back, 'vm-a', 'history'), false)
  assert.equal(isSectionCollapsed(one, 'vm-a', 'history'), true)
})

test('sectionKey keeps vm id and section apart', () => {
  assert.notEqual(sectionKey('a', 'bc'), sectionKey('ab', 'c'))
  const s = toggleSection(toggleSection(new Set(), 'vm-a', 'notebook'), 'vm-b', 'lifetime')
  assert.deepEqual([...s].sort(), [sectionKey('vm-a', 'notebook'), sectionKey('vm-b', 'lifetime')].sort())
})

test('the Cost tab has no collapsible section', () => {
  assert.equal(VM_SECTIONS.includes('cost'), false)
  assert.deepEqual([...VM_SECTIONS], ['notebook', 'lifecycle', 'checkpoint', 'history', 'lifetime', 'environment'])
})
