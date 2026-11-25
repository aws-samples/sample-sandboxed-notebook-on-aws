// Unit tests for services/filterPills.js (shared filter pills: class list + radio keyboard model).
// Run with: node --test web/src/services/__tests__/filterPills.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { filterPillClassName, nextFilterPillIndex, selectedFilterPillIndex } from '../filterPills.js'

test('filterPillClassName: shared Outline pill class, active when selected, extra appended', () => {
  assert.equal(filterPillClassName(false), 'outline-filter-chip')
  assert.equal(filterPillClassName(true), 'outline-filter-chip active')
  assert.equal(filterPillClassName(true, 'has-errors'), 'outline-filter-chip active has-errors')
  assert.equal(filterPillClassName(false, 'has-errors'), 'outline-filter-chip has-errors')
  assert.equal(filterPillClassName(false, ''), 'outline-filter-chip')
  assert.equal(filterPillClassName(false, undefined), 'outline-filter-chip')
})

test('filterPillClassName: matches the Outline chip classes it replaces', () => {
  // OutlinePanel used `outline-filter-chip${active ? ' active' : ''}${errs ? ' has-errors' : ''}`.
  for (const active of [false, true]) {
    for (const errs of [false, true]) {
      const old = `outline-filter-chip${active ? ' active' : ''}${errs ? ' has-errors' : ''}`
      assert.equal(filterPillClassName(active, errs ? 'has-errors' : ''), old)
    }
  }
})

test('nextFilterPillIndex: arrows wrap in both axes, Home/End jump', () => {
  assert.equal(nextFilterPillIndex('ArrowRight', 0, 5), 1)
  assert.equal(nextFilterPillIndex('ArrowDown', 4, 5), 0)
  assert.equal(nextFilterPillIndex('ArrowLeft', 0, 5), 4)
  assert.equal(nextFilterPillIndex('ArrowUp', 2, 5), 1)
  assert.equal(nextFilterPillIndex('Home', 3, 5), 0)
  assert.equal(nextFilterPillIndex('End', 0, 5), 4)
  assert.equal(nextFilterPillIndex('ArrowRight', 1, 2), 0)
})

test('nextFilterPillIndex: non-navigation keys and empty groups return null', () => {
  assert.equal(nextFilterPillIndex('Enter', 0, 3), null)
  assert.equal(nextFilterPillIndex(' ', 0, 3), null)
  assert.equal(nextFilterPillIndex('a', 0, 3), null)
  assert.equal(nextFilterPillIndex('ArrowRight', 0, 0), null)
})

test('nextFilterPillIndex: out-of-range start index is treated as 0', () => {
  assert.equal(nextFilterPillIndex('ArrowRight', -1, 3), 1)
  assert.equal(nextFilterPillIndex('ArrowLeft', 9, 3), 2)
})

test('selectedFilterPillIndex: index of the selected key, 0 when none matches', () => {
  const opts = [{ key: 'all' }, { key: 'user' }]
  assert.equal(selectedFilterPillIndex(opts, 'user'), 1)
  assert.equal(selectedFilterPillIndex(opts, 'all'), 0)
  assert.equal(selectedFilterPillIndex(opts, 'nope'), 0)
  assert.equal(selectedFilterPillIndex([], 'all'), 0)
  assert.equal(selectedFilterPillIndex(undefined, 'all'), 0)
})
