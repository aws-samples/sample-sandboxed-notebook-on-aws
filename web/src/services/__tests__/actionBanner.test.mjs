// Unit tests for services/actionBanner.js (shared action banner rules).
// Run with: node --test web/src/services/__tests__/actionBanner.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  BANNER_AUTO_DISMISS_MS, RUN_BANNER_MIN_MS, SLOTS, TONES, CLEAR_RULES, NON_CLEARING_EVENTS,
  makeBanner, replaceInSlot, shouldClear, shouldShowRunBanner, isAlreadyViewing,
  countdownInit, countdownStep, countdownRemaining,
  fileListText, intelBannerText, sandboxBannerText, shouldShowSandboxBanner, importBannerText,
} from '../actionBanner.js'

const T = 1_000_000
const steps = (events, start = countdownInit()) =>
  events.reduce((s, [ev, now]) => countdownStep(s, ev, now), start)

test('constants: 10 s auto-dismiss, 2 s run threshold, slots and tones', () => {
  assert.equal(BANNER_AUTO_DISMISS_MS, 10_000)
  assert.equal(RUN_BANNER_MIN_MS, 2_000)
  assert.deepEqual(Object.values(SLOTS).sort(), ['ai', 'bottom', 'microvms', 'notebooks', 'outline', 'packages'])
  assert.deepEqual([...TONES], ['success', 'error', 'warning', 'info'])
})

test('countdown: does not start before the banner is visible', () => {
  const s = countdownInit()
  assert.equal(s.started, false)
  assert.equal(countdownRemaining(s, T), null)
  assert.equal(countdownStep(s, 'expire', T + 60_000).expired, false)
})

test('countdown: starts on first visible and expires after 10 s', () => {
  const s = steps([['visible', T]])
  assert.equal(s.started, true)
  assert.equal(countdownRemaining(s, T), 10_000)
  assert.equal(countdownRemaining(s, T + 9_000), 1_000)
  assert.equal(countdownStep(s, 'expire', T + 9_999).expired, false)
  assert.equal(countdownStep(s, 'expire', T + 10_000).expired, true)
})

test('countdown: hover pauses, leaving restarts the full 10 s', () => {
  let s = steps([['visible', T], ['hover-in', T + 8_000]])
  assert.equal(countdownRemaining(s, T + 30_000), null)
  assert.equal(countdownStep(s, 'expire', T + 30_000).expired, false)
  s = countdownStep(s, 'hover-out', T + 30_000)
  assert.equal(countdownRemaining(s, T + 30_000), 10_000)
  assert.equal(countdownStep(s, 'expire', T + 39_999).expired, false)
  assert.equal(countdownStep(s, 'expire', T + 40_000).expired, true)
})

test('countdown: keyboard focus pauses; it resumes only when both hover and focus left', () => {
  let s = steps([['visible', T], ['focus-in', T + 1_000], ['hover-in', T + 2_000], ['hover-out', T + 3_000]])
  assert.equal(countdownRemaining(s, T + 3_000), null)
  s = countdownStep(s, 'focus-out', T + 5_000)
  assert.equal(countdownRemaining(s, T + 5_000), 10_000)
})

test('countdown: a hidden page pauses, visible again restarts the full 10 s', () => {
  let s = steps([['visible', T], ['hidden', T + 4_000]])
  assert.equal(countdownRemaining(s, T + 20_000), null)
  s = countdownStep(s, 'visible', T + 20_000)
  assert.equal(countdownRemaining(s, T + 20_000), 10_000)
})

test('countdown: hovered when first shown waits for the pointer to leave', () => {
  let s = steps([['hover-in', T], ['visible', T]])
  assert.equal(s.started, true)
  assert.equal(countdownRemaining(s, T), null)
  s = countdownStep(s, 'hover-out', T + 2_000)
  assert.equal(countdownRemaining(s, T + 2_000), 10_000)
})

test('countdown: expired is final and repeated events are no-ops', () => {
  const done = steps([['visible', T], ['expire', T + 10_000]])
  assert.equal(done.expired, true)
  assert.equal(countdownStep(done, 'hover-out', T + 11_000), done)
  assert.equal(countdownStep(done, 'visible', T + 11_000), done)
  const s = steps([['visible', T]])
  assert.equal(countdownStep(s, 'visible', T + 5_000), s)      // already visible: no restart
  assert.equal(countdownStep(s, 'hover-out', T + 5_000), s)    // was not hovered
  assert.equal(countdownStep(s, 'nonsense', T + 5_000), s)
})

test('makeBanner: fresh ids, known tones only', () => {
  const a = makeBanner({ kind: 'run', slot: 'outline', tone: 'success', lead: 'x', createdAt: T })
  const b = makeBanner({ kind: 'run', slot: 'outline', tone: 'success', lead: 'x', createdAt: T })
  assert.notEqual(a.id, b.id)
  assert.equal(a.kind, 'run')
  assert.equal(a.slot, 'outline')
  assert.equal(makeBanner({ kind: 'x', tone: 'blue' }).tone, 'info')
})

test('replaceInSlot: the newer banner wins, an older one is ignored', () => {
  const old = makeBanner({ kind: 'import', createdAt: T })
  const newer = makeBanner({ kind: 'sandbox-ready', createdAt: T + 1 })
  assert.equal(replaceInSlot(null, old), old)
  assert.equal(replaceInSlot(old, newer), newer)
  assert.equal(replaceInSlot(newer, old), newer)
  assert.equal(replaceInSlot(old, null), old)
  assert.equal(replaceInSlot(null, null), null)
  const again = replaceInSlot(old, { ...old })
  assert.notEqual(again.id, old.id)                                   // same id: fresh one
})

test('shouldClear: every kind clears on dismiss, action, auto-dismiss and the same operation starting', () => {
  for (const kind of [...Object.keys(CLEAR_RULES), 'unknown-kind']) {
    for (const e of ['dismiss', 'action', 'secondary', 'auto-dismiss', 'same-op-start']) {
      assert.equal(shouldClear(kind, e), true, `${kind} ${e}`)
    }
  }
})

test('shouldClear: per-kind events', () => {
  assert.equal(shouldClear('run', 'run-start'), true)
  assert.equal(shouldClear('run', 'open-history'), true)
  assert.equal(shouldClear('intel-ready', 'open-report'), true)
  assert.equal(shouldClear('intel-updated', 'intel-start'), true)
  assert.equal(shouldClear('sandbox-ready', 'activate-target-tab'), true)
  assert.equal(shouldClear('import', 'activate-target-tab'), true)
  assert.equal(shouldClear('package-install', 'uninstall-package'), true)
  assert.equal(shouldClear('terminal', 'terminal-visible-focused'), true)
  // and not across kinds
  assert.equal(shouldClear('run', 'open-report'), false)
  assert.equal(shouldClear('package-install', 'activate-target-tab'), false)
  assert.equal(shouldClear('unknown-kind', 'run-start'), false)
})

test('shouldClear: passive events never clear any banner', () => {
  for (const kind of Object.keys(CLEAR_RULES)) {
    for (const e of NON_CLEARING_EVENTS) assert.equal(shouldClear(kind, e), false, `${kind} ${e}`)
  }
  for (const rules of Object.values(CLEAR_RULES)) {
    assert.equal(rules.some(e => NON_CLEARING_EVENTS.includes(e)), false)
  }
})

test('shouldShowRunBanner: 2 s threshold, or any error or stopped cell', () => {
  assert.equal(shouldShowRunBanner({ elapsedMs: 0 }), false)
  assert.equal(shouldShowRunBanner({ elapsedMs: 1_999 }), false)
  assert.equal(shouldShowRunBanner({ elapsedMs: 2_000 }), true)
  assert.equal(shouldShowRunBanner({ elapsedMs: 100, errors: 1 }), true)
  assert.equal(shouldShowRunBanner({ elapsedMs: 100, stopped: 1 }), true)
  assert.equal(shouldShowRunBanner(), false)
})

test('isAlreadyViewing: per-kind rules', () => {
  // AI report already open for this notebook
  assert.equal(isAlreadyViewing('intel-ready', { activeTabId: 1, tabId: '1', showReport: true }), true)
  assert.equal(isAlreadyViewing('intel-ready', { activeTabId: 1, tabId: 1, showReport: false }), false)
  assert.equal(isAlreadyViewing('intel-updated', { activeTabId: 2, tabId: 1, showReport: true }), false)
  // sandbox / import: that notebook's Notebook view is open
  assert.equal(isAlreadyViewing('sandbox-ready', { activeTabId: 'a', tabId: 'a', view: 'notebook' }), true)
  assert.equal(isAlreadyViewing('sandbox-ready', { activeTabId: 'b', tabId: 'a', view: 'notebook' }), false)
  assert.equal(isAlreadyViewing('import', { activeTabId: 'a', tabId: 'a', view: 'connect' }), false)
  assert.equal(isAlreadyViewing('import', { activeTabId: 'a', tabId: 'a', view: 'notebook' }), true)
  // terminal: visible and focused
  assert.equal(isAlreadyViewing('terminal', { terminalVisible: true, terminalFocused: true }), true)
  assert.equal(isAlreadyViewing('terminal', { terminalVisible: true, terminalFocused: false }), false)
  assert.equal(isAlreadyViewing('terminal', { terminalVisible: false, terminalFocused: true }), false)
  // run and package banners always show
  assert.equal(isAlreadyViewing('run', { activeTabId: 1, tabId: 1 }), false)
  assert.equal(isAlreadyViewing('package-install', {}), false)
  assert.equal(isAlreadyViewing('sandbox-ready', { view: 'notebook' }), false)   // no tab id
})

test('fileListText: one, two, many', () => {
  assert.equal(fileListText([]), '')
  assert.equal(fileListText(['a.csv']), 'a.csv')
  assert.equal(fileListText(['a.csv', 'b.csv']), 'a.csv and b.csv')
  assert.equal(fileListText(['a.csv', 'b.csv', 'c.csv']), 'a.csv and 2 more files')
  assert.equal(fileListText(['a.csv', null, '']), 'a.csv')
})
test('intelBannerText: full report and delta wording', () => {
  assert.deepEqual(intelBannerText({ mode: 'full' }), {
    kind: 'intel-ready', tone: 'success', lead: 'Workbook Intel report is ready.', detail: '', action: 'View full report',
  })
  const up = intelBannerText({ mode: 'update', trigger: 'file_upload', fileNames: ['sales.csv'] })
  assert.equal(up.kind, 'intel-updated')
  assert.equal(`${up.lead} ${up.detail}`, 'Workbook Intel updated for sales.csv.')
  assert.equal(up.action, 'View full report')
  const del = intelBannerText({ mode: 'update', trigger: 'file_delete', fileNames: ['old.csv'] })
  assert.equal(`${del.lead} ${del.detail}`, 'Workbook Intel updated after removing old.csv.')
  const none = intelBannerText({ mode: 'update' })
  assert.equal(none.lead, 'Workbook Intel updated.')
  assert.equal(none.detail, '')
})
test('sandboxBannerText: ready per operation, and failures', () => {
  const r = sandboxBannerText({ op: 'restore', ok: true, elapsedMs: 12_300, name: 'Sales' })
  assert.equal(r.kind, 'sandbox-ready')
  assert.equal(r.tone, 'success')
  assert.equal(`${r.lead} ${r.detail}`, 'Sandbox ready for Sales, restored in 12.3 s.')
  assert.equal(r.action, 'Open notebook')
  assert.equal(sandboxBannerText({ op: 'launch', elapsedMs: 4000 }).detail, 'launched in 4.0 s.')
  assert.equal(sandboxBannerText({ op: 'connect', elapsedMs: 2500 }).detail, 'connected in 2.5 s.')
  const f = sandboxBannerText({ op: 'launch', ok: false, error: 'No capacity', name: 'Sales' })
  assert.equal(f.kind, 'sandbox-failed')
  assert.equal(f.tone, 'error')
  assert.equal(`${f.lead} ${f.detail}`, 'Sandbox launch failed for Sales: No capacity')
  assert.equal(f.action, 'Open Connect')
  const f2 = sandboxBannerText({ op: 'restore', ok: false, error: '' })
  assert.equal(`${f2.lead} ${f2.detail}`, 'Sandbox restore failed: Unknown error')
  assert.equal(sandboxBannerText({ op: 'launch', ok: false, error: 'Launch failed: 503' }).detail, '503')
  assert.equal(sandboxBannerText({ op: 'restore', ok: false, error: 'Restore error: timeout' }).detail, 'timeout')
})
test('shouldShowSandboxBanner: success only after 2 s, failure always', () => {
  assert.equal(shouldShowSandboxBanner({ ok: true, elapsedMs: 1999 }), false)
  assert.equal(shouldShowSandboxBanner({ ok: true, elapsedMs: 2000 }), true)
  assert.equal(shouldShowSandboxBanner({ ok: false, elapsedMs: 10 }), true)
})
test('importBannerText: success and failure', () => {
  const ok = importBannerText({ ok: true, name: 'Churn Analysis' })
  assert.deepEqual(ok, { kind: 'import', tone: 'success', lead: 'Imported', detail: 'Churn Analysis.', action: 'Open it' })
  const bad = importBannerText({ ok: false, error: 'Import failed: 404' })
  assert.equal(bad.tone, 'error')
  assert.equal(bad.detail, '404')   // repeated prefix dropped
  assert.equal(importBannerText({ ok: false, error: 'Bad URL' }).detail, 'Bad URL')
  assert.equal(bad.action, '')
})
test('sandbox and import banners clear when their notebook is activated', () => {
  for (const kind of ['sandbox-ready', 'sandbox-failed', 'import']) {
    assert.equal(shouldClear(kind, 'activate-target-tab'), true)
    assert.equal(shouldClear(kind, 'same-op-start'), true)
  }
  assert.equal(shouldClear('intel-ready', 'intel-start'), true)
  assert.equal(shouldClear('intel-updated', 'open-report'), true)
})
