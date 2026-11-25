// Unit tests for services/intelRefreshEdge.js (one Data Sources refresh per intel
// generation, from the single useWorkbookIntel poller; proxy-log-chatter B2).
// Run with: node --test web/src/services/__tests__/intelRefreshEdge.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createIntelRefreshEdge } from '../intelRefreshEdge.js'

test('mount-time ready report (no trigger) never fires', () => {
  const e = createIntelRefreshEdge()
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), false)
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), false)
})

test('trigger -> generating -> ready fires exactly once', () => {
  const e = createIntelRefreshEdge()
  e.observe('ready', { readySig: 'ready:A' })
  e.arm()
  assert.equal(e.observe('phase1'), false)
  assert.equal(e.observe('phase1'), false)
  assert.equal(e.observe('phase2'), false)
  assert.equal(e.observe('ready', { readySig: 'ready:B' }), true)
  assert.equal(e.observe('ready', { readySig: 'ready:B' }), false, 'later polls of the same report stay quiet')
})

test('a poll that races ahead of POST /generate (old report) does not fire, then the new report does', () => {
  const e = createIntelRefreshEdge()
  e.observe('ready', { readySig: 'ready:A' })
  e.arm()
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), false)
  assert.equal(e.state().armed, true, 'still waiting for the triggered job')
  assert.equal(e.observe('ready', { readySig: 'ready:B' }), true, 'fast job: changed report while armed')
})

test('first-ever report (no previous ready) fires when armed', () => {
  const e = createIntelRefreshEdge()
  e.observe('not_generated', { withinGrace: false })
  e.arm()
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), true)
})

test('server-observed generation without a client trigger still fires once', () => {
  // e.g. the mount fetch lands mid-generation started by another component.
  const e = createIntelRefreshEdge()
  assert.equal(e.observe('phase1'), false)
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), true)
})

test('backend no-op: not_generated inside the grace window keeps waiting, after it disarms', () => {
  const e = createIntelRefreshEdge()
  e.arm()
  assert.equal(e.observe('not_generated', { withinGrace: true }), false)
  assert.equal(e.state().armed, true)
  assert.equal(e.observe('not_generated', { withinGrace: false }), false)
  assert.equal(e.state().armed, false, 'the 33-minute watcher case: settled, nothing to refresh')
  assert.equal(e.observe('ready', { readySig: 'ready:Z' }), false, 'no late spurious refresh')
})

test('error disarms; reset clears everything (session change)', () => {
  const e = createIntelRefreshEdge()
  e.arm(); e.observe('phase1')
  assert.equal(e.observe('error'), false)
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), false)
  e.arm(); e.observe('phase1')
  e.reset()
  assert.deepEqual(e.state(), { armed: false, sawInFlight: false, lastReadySig: null })
  assert.equal(e.observe('ready', { readySig: 'ready:A' }), false)
})

test('4 queued delete jobs (report burst) -> one refresh at the end, not 4', () => {
  // The backend reports phase1 for the whole queue; the single poller sees ready once.
  const e = createIntelRefreshEdge()
  e.observe('ready', { readySig: 'ready:A' })
  let fired = 0
  for (let i = 0; i < 4; i++) e.arm()
  for (let i = 0; i < 37; i++) if (e.observe('phase1')) fired++   // ~2.5 min at 4 s
  if (e.observe('ready', { readySig: 'ready:D' })) fired++
  for (let i = 0; i < 3; i++) if (e.observe('ready', { readySig: 'ready:D' })) fired++
  assert.equal(fired, 1)
})
