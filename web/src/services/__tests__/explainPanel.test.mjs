// Pure helpers for the focus-mode Explanation panel.
// Run with: node --test web/src/services/__tests__/explainPanel.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  clampPanelWidth, isStacked, rangeChip, highlightAnnouncement, highlightsValid, activeRange,
  getPanelPrefs, setPanelPrefs, resetPanelPrefs, PANEL_DEFAULT_WIDTH,
} from '../explainPanel.js'
test('clampPanelWidth: min 280, max 55% of the overlay', () => {
  assert.equal(clampPanelWidth(100, 1288), 280)
  assert.equal(clampPanelWidth(400, 1288), 400)
  assert.equal(clampPanelWidth(2000, 1288), Math.floor(1288 * 0.55))
  assert.equal(clampPanelWidth(NaN, 1000), PANEL_DEFAULT_WIDTH)
  assert.equal(clampPanelWidth(300, 400), 280)
})
test('isStacked: below 900 px only', () => {
  assert.equal(isStacked(899), true)
  assert.equal(isStacked(900), false)
  assert.equal(isStacked(0), false)
})
test('rangeChip and announcement', () => {
  assert.equal(rangeChip(16, 28), 'Lines 16\u201328')
  assert.equal(rangeChip(5, 5), 'Line 5')
  assert.equal(highlightAnnouncement(16, 28), 'Lines 16 to 28 highlighted')
  assert.equal(highlightAnnouncement(5, 5), 'Line 5 highlighted')
  assert.equal(highlightAnnouncement(0, 0), '')
})
test('highlightsValid: only while the line count is unchanged', () => {
  assert.equal(highlightsValid('a\nb\nc', 'a\nb\nc'), true)
  assert.equal(highlightsValid('a\nb\nc', 'a\nX\nc'), true)
  assert.equal(highlightsValid('a\nb\nc', 'a\nb\nc\nd'), false)
  assert.equal(highlightsValid('a\nb\nc', 'a\nb'), false)
  assert.equal(highlightsValid('a\nb\n\n', 'a\nb'), true)
})
test('activeRange: hover wins over the pinned section', () => {
  const p = { start: 1, end: 2 }, h = { start: 5, end: 6 }
  assert.equal(activeRange(p, h), h)
  assert.equal(activeRange(p, null), p)
  assert.equal(activeRange(null, null), null)
})
test('the panel width is remembered for the session', () => {
  resetPanelPrefs()
  assert.deepEqual(getPanelPrefs(), { width: PANEL_DEFAULT_WIDTH })
  setPanelPrefs({ width: 444 })
  assert.deepEqual(getPanelPrefs(), { width: 444 })
  resetPanelPrefs()
})

test('open request: one-shot flag from the Explain button; prefs shape unchanged', async () => {
  const { requestPanelOpen, peekPanelOpenRequest, clearPanelOpenRequest } = await import('../explainPanel.js')
  resetPanelPrefs()
  assert.equal(peekPanelOpenRequest(), false)
  requestPanelOpen()
  assert.equal(peekPanelOpenRequest(), true)
  assert.equal(peekPanelOpenRequest(), true, 'peeking does not consume (StrictMode-safe)')
  clearPanelOpenRequest()
  assert.equal(peekPanelOpenRequest(), false)
  requestPanelOpen()
  resetPanelPrefs()
  assert.equal(peekPanelOpenRequest(), false, 'reset clears a pending request')
  assert.deepEqual(getPanelPrefs(), { width: PANEL_DEFAULT_WIDTH })
})
