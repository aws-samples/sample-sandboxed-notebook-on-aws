// Unit tests for services/execTint.js (the per-notebook execution-time tint state).
// Run with: node --test web/src/services/__tests__/execTint.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { tintAfterRunAll, tintBeforeRunAll, isTintOn, tintTogglePatch } from '../execTint.js'

// Apply a patch the way App.updateTab does: shallow merge onto the tab.
const apply = (tab, patch) => ({ ...tab, ...patch })

test('isTintOn: only an explicit _heatmapOn: true reads as on', () => {
  assert.equal(isTintOn({}), false)
  assert.equal(isTintOn({ _heatmapOn: false }), false)
  assert.equal(isTintOn({ _heatmapOn: true }), true)
  assert.equal(isTintOn(null), false)
  assert.equal(isTintOn(undefined), false)
})

test('tintAfterRunAll: a completed Run All turns the tint on and stamps the time', () => {
  const patch = tintAfterRunAll(1234)
  assert.deepEqual(patch, { _lastRunAllAt: 1234, _heatmapOn: true })
  assert.equal(isTintOn(apply({ id: 'A' }, patch)), true)
})

test('tintAfterRunAll: a later completion re-enables a tint the user turned off', () => {
  let tab = { id: 'A', _heatmapOn: true, _lastRunAllAt: 1 }
  tab = apply(tab, tintTogglePatch(tab))          // user turns it off
  assert.equal(isTintOn(tab), false)
  tab = apply(tab, tintAfterRunAll(2))            // next Run All finishes
  assert.equal(isTintOn(tab), true)
  assert.equal(tab._lastRunAllAt, 2)
})

test('tintTogglePatch: flips the flag for this tab, both directions', () => {
  const on = { _heatmapOn: true }
  const off = {}
  assert.deepEqual(tintTogglePatch(on), { _heatmapOn: false })
  assert.deepEqual(tintTogglePatch(off), { _heatmapOn: true })
  assert.deepEqual(tintTogglePatch(null), { _heatmapOn: true })
})

test('per notebook: a completion on A does not change B', () => {
  const a = { id: 'A' }
  const b = { id: 'B', _heatmapOn: false }
  const aAfter = apply(a, tintAfterRunAll(5))
  assert.equal(isTintOn(aAfter), true)
  assert.equal(isTintOn(b), false)
  assert.equal(b._heatmapOn, false)
})

test('tintBeforeRunAll: starting a Run All turns the tint off; completion turns it back on', () => {
  let tab = { id: 'a', _heatmapOn: true, _lastRunAllAt: 1 }
  tab = apply(tab, tintBeforeRunAll())
  assert.equal(isTintOn(tab), false)
  assert.equal(tab._lastRunAllAt, 1)  // the start does not stamp a completion
  tab = apply(tab, tintAfterRunAll(2))
  assert.equal(isTintOn(tab), true)
  assert.equal(tab._lastRunAllAt, 2)
})
