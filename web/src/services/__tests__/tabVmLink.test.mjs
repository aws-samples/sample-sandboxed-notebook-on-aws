// Unit tests for services/tabVmLink.js and vmOwnership.detachedSessionVm
// (restore-arch-bug: a restore that succeeded right before a page reload must not
// leave the tab "disconnected" while its restored VM runs).
// Run with: node --test web/src/services/__tests__/tabVmLink.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { vmLinkFromLaunch, patchStoredTabs, persistTabVmLink, sessionRelinkPatch, restoreOffer } from '../tabVmLink.js'
import { detachedSessionVm, staleVmRepoint, terminateTargetForTab, instanceSessionMismatch } from '../vmOwnership.js'
import { archLabel } from '../costRates.js'
import { tierVcpu, fmtTierVcpu } from '../vmBurst.js'

const launch = { microvmId: 'vm-new', architecture: 'x86_64', memory_mib: 1024, sessionId: 'sess-A', endpoint: 'e' }

test('vmLinkFromLaunch: maps the /launch response to tab fields, needs a vm id', () => {
  assert.deepEqual(vmLinkFromLaunch(launch),
    { microvmId: 'vm-new', microvmArch: 'x86_64', microvmMemory: 1024, sessionId: 'sess-A', sessionSaved: false })
  assert.deepEqual(vmLinkFromLaunch({ microvmId: 'vm-x' }), { microvmId: 'vm-x', sessionSaved: false })
  assert.equal(vmLinkFromLaunch({}), null)
  assert.equal(vmLinkFromLaunch(null), null)
})

test('patchStoredTabs: merges into the matching tab only; null when nothing to write', () => {
  const raw = JSON.stringify([
    { id: '84', name: 'TSF', microvmId: null, microvmArch: 'x86_64', sessionSaved: true, _cells: [{ id: 'c' }] },
    { id: '71', name: 'VoC', microvmId: 'vm-71' },
  ])
  const out = JSON.parse(patchStoredTabs(raw, 84, vmLinkFromLaunch(launch)))
  assert.equal(out[0].microvmId, 'vm-new')
  assert.equal(out[0].microvmArch, 'x86_64')
  assert.equal(out[0].sessionSaved, false)
  assert.deepEqual(out[0]._cells, [{ id: 'c' }])
  assert.deepEqual(out[1], { id: '71', name: 'VoC', microvmId: 'vm-71' })
  assert.equal(patchStoredTabs(raw, '999', { microvmId: 'x' }), null)
  assert.equal(patchStoredTabs(null, '84', { microvmId: 'x' }), null)
  assert.equal(patchStoredTabs('not json', '84', { microvmId: 'x' }), null)
  assert.equal(patchStoredTabs(raw, '84', null), null)
})

test('persistTabVmLink: writes immediately; never throws on a broken storage', () => {
  const mem = { v: JSON.stringify([{ id: '84', microvmId: null }]), getItem() { return this.v }, setItem(_k, v) { this.v = v } }
  persistTabVmLink('84', launch, mem)
  assert.equal(JSON.parse(mem.v)[0].microvmId, 'vm-new')
  const broken = { getItem() { throw new Error('quota') }, setItem() { throw new Error('quota') } }
  assert.doesNotThrow(() => persistTabVmLink('84', launch, broken))
})

const inst = {
  'vm-new': { state: 'RUNNING', session_id: 'sess-A', architecture: 'x86_64', memory_mib: 1024 },
  'vm-other': { state: 'RUNNING', session_id: 'sess-B' },
  'vm-dead': { state: 'TERMINATED', session_id: 'sess-C' },
}

test('detachedSessionVm: a disconnected tab with no vm id re-links to its own live VM', () => {
  const own = detachedSessionVm(inst, { sessionId: 'sess-A', microvmId: null, status: 'disconnected', sessionSaved: true })
  assert.equal(own[0], 'vm-new')
  assert.equal(own[1].architecture, 'x86_64')
})

test('detachedSessionVm: no action for other sessions, dead VMs, launches, linked tabs, or the just-terminated VM', () => {
  assert.equal(detachedSessionVm(inst, { sessionId: 'sess-Z', status: 'disconnected' }), null)
  assert.equal(detachedSessionVm(inst, { sessionId: 'sess-C', status: 'disconnected' }), null)
  assert.equal(detachedSessionVm(inst, { sessionId: 'sess-A', status: 'launching' }), null)
  assert.equal(detachedSessionVm(inst, { sessionId: 'sess-A', status: 'connected' }), null)
  assert.equal(detachedSessionVm(inst, { sessionId: 'sess-A', microvmId: 'vm-x', status: 'disconnected' }), null)
  assert.equal(detachedSessionVm(inst, { sessionId: '', status: 'disconnected' }), null)
  assert.equal(detachedSessionVm(inst, { sessionId: 'sess-A', status: 'disconnected', _terminatedVmId: 'vm-new' }), null)
})

test('vmLinkFromLaunch: a pool-miss cold launch maps its effective arch/tier; nothing is invented', () => {
  // /launch response after a 409 → cold launch at x86_64/1024 (no "reused", no pool fields).
  const cold = { microvmId: 'microvm-cold', endpoint: 'ep', name: 'nb', sessionId: 'sess-A', status: 'running', memory_mib: 1024, architecture: 'x86_64' }
  assert.deepEqual(vmLinkFromLaunch(cold),
    { microvmId: 'microvm-cold', microvmArch: 'x86_64', microvmMemory: 1024, sessionId: 'sess-A', sessionSaved: false })
  // A response without arch/tier leaves them unset (the poll fills them), never arm64/2048.
  const bare = vmLinkFromLaunch({ microvmId: 'microvm-x' })
  assert.equal('microvmArch' in bare, false)
  assert.equal('microvmMemory' in bare, false)
})

test('sessionRelinkPatch: a lost-link tab adopts the server-relinked session → Option A', () => {
  const lost = { id: '89', sessionId: null, status: 'disconnected', microvmId: null }
  const nb = { id: 89, session_id: 'sess-A', checkpoint_enabled: true, session_relinked: true }
  assert.deepEqual(sessionRelinkPatch(lost, nb, {}), { sessionId: 'sess-A', checkpointEnabled: true, sessionSaved: true })
  // A live VM already serves it: adopt the session but do not claim a saved checkpoint.
  assert.deepEqual(sessionRelinkPatch(lost, nb, { 'vm-1': { session_id: 'sess-A', state: 'RUNNING' } }),
    { sessionId: 'sess-A', checkpointEnabled: true })
})

test('sessionRelinkPatch: no change for linked/connected/launching tabs or without a server session', () => {
  const nb = { session_id: 'sess-A', checkpoint_enabled: true }
  assert.equal(sessionRelinkPatch({ sessionId: 'sess-X', status: 'disconnected' }, nb), null)
  assert.equal(sessionRelinkPatch({ sessionId: null, status: 'connected' }, nb), null)
  assert.equal(sessionRelinkPatch({ sessionId: null, status: 'launching' }, nb), null)
  assert.equal(sessionRelinkPatch({ sessionId: null, status: 'disconnected' }, { session_id: null, checkpoint_enabled: true }), null)
  assert.equal(sessionRelinkPatch({ sessionId: null, status: 'disconnected' }, { session_id: 'sess-A', checkpoint_enabled: false }), null)
  assert.equal(sessionRelinkPatch(null, nb), null)
})

test('sessionRelinkPatch: carries the server-recorded arch/tier for a tab that has none', () => {
  const nb = { session_id: 'sess-A', checkpoint_enabled: true, session_arch: 'x86_64', session_memory_mib: 1024 }
  // Lost-link tab: session + recorded arch/tier, so Option A shows "1 GB · 0.5 vCPU · x86_64".
  assert.deepEqual(sessionRelinkPatch({ sessionId: null, status: 'disconnected' }, nb, {}),
    { sessionId: 'sess-A', checkpointEnabled: true, microvmArch: 'x86_64', microvmMemory: 1024, sessionSaved: true })
  // Tab with the same session but no arch/tier: fill only those.
  assert.deepEqual(sessionRelinkPatch({ sessionId: 'sess-A', status: 'disconnected' }, nb, {}),
    { microvmArch: 'x86_64', microvmMemory: 1024 })
  // A tab's own arch/tier is never overwritten.
  assert.equal(sessionRelinkPatch({ sessionId: 'sess-A', status: 'disconnected', microvmArch: 'arm64', microvmMemory: 2048 }, nb, {}), null)
  assert.deepEqual(sessionRelinkPatch({ sessionId: 'sess-A', status: 'disconnected', microvmArch: 'x86_64' }, nb, {}),
    { microvmMemory: 1024 })
  // Another session on the tab: untouched. Server without the fields: unchanged behaviour.
  assert.equal(sessionRelinkPatch({ sessionId: 'sess-X', status: 'disconnected' }, nb, {}), null)
  assert.equal(sessionRelinkPatch({ sessionId: 'sess-A', status: 'disconnected' }, { session_id: 'sess-A', checkpoint_enabled: true }, {}), null)
})

// The crash reproduced live (headless Chrome, notebook 90): a tab bound to a VM
// that is gone, checkpointEnabled, no sessionId. The poll set sessionSaved, the
// Notebook opened the Connect panel, and Option A ran tab.sessionId.slice(-12):
// "Cannot read properties of null (reading 'slice')" -> app-wide error screen.
const LOST = { id: '90', sessionId: null, status: 'disconnected', sessionSaved: true, checkpointEnabled: true,
  microvmId: 'microvm-dead', microvmEndpoint: null, mode: null }
test('restoreOffer: a saved tab without a session id gets no Option A (the crash shape)', () => {
  // The pre-fix inline render expression throws on this shape.
  assert.throws(() => LOST.sessionId.slice(-12), /reading 'slice'/)
  assert.equal(restoreOffer(LOST, 'checkpoint'), null)
  assert.equal(restoreOffer({ ...LOST, sessionId: undefined }, 'checkpoint'), null)
  assert.equal(restoreOffer({ ...LOST, sessionId: 123 }, 'checkpoint'), null)
  assert.equal(restoreOffer(undefined, 'checkpoint'), null)
})

test('restoreOffer: offered only for a saved, disconnected tab with a session', () => {
  const tab = { ...LOST, sessionId: '686c6771-8936-4ada-8349-1b6516da2f25' }
  assert.deepEqual(restoreOffer(tab, 'checkpoint'), { sessionTail: '1b6516da2f25' })
  assert.deepEqual(restoreOffer(tab, 'eternal'), { sessionTail: '1b6516da2f25' })
  assert.equal(restoreOffer(tab, 'none'), null)
  assert.equal(restoreOffer({ ...tab, sessionSaved: false }, 'checkpoint'), null)
  assert.equal(restoreOffer({ ...tab, status: 'connected' }, 'checkpoint'), null)
})

test('restore-path helpers never throw on partial tab / instance shapes', () => {
  const tabs = [LOST, { id: '1' }, { id: '2', microvmId: 'vm-1', sessionId: 's', microvmArch: undefined, microvmMemory: undefined },
    { id: '3', microvmId: 'vm-1', sessionId: null, status: 'connected' }, { id: '4', status: 'launching', microvmId: null }]
  const instMaps = [undefined, null, {}, { 'vm-1': null }, { 'vm-1': {} },
    { 'vm-1': { state: 'PENDING', session_id: 's' } },                       // <60 s entry, no arch/size
    { 'vm-1': { state: 'RUNNING', session_id: null, architecture: null, memory_mib: null } },
    { 'vm-2': { state: 'RUNNING', session_id: 's', architecture: 'x86_64', memory_mib: 1024 } }]
  for (const tab of tabs) {
    for (const inst of instMaps) {
      assert.doesNotThrow(() => detachedSessionVm(inst || {}, tab), `detachedSessionVm ${JSON.stringify([tab, inst])}`)
      assert.doesNotThrow(() => staleVmRepoint(inst || {}, tab), `staleVmRepoint ${JSON.stringify([tab, inst])}`)
      assert.doesNotThrow(() => terminateTargetForTab(inst || {}, tab), `terminateTargetForTab ${JSON.stringify([tab, inst])}`)
      assert.doesNotThrow(() => sessionRelinkPatch(tab, { session_id: 's', checkpoint_enabled: true }, inst || {}))
      assert.doesNotThrow(() => restoreOffer(tab, 'checkpoint'))
      for (const i of Object.values(inst || {})) assert.doesNotThrow(() => instanceSessionMismatch(i || {}, tab))
    }
    // The Option A / Connected spec line: arch label and vCPU text on a missing tier.
    assert.doesNotThrow(() => `${archLabel(tab.microvmArch)} ${fmtTierVcpu(tierVcpu(Number(tab.microvmMemory)))}`)
  }
  for (const r of [undefined, null, {}, { microvmId: '' }, { microvmId: 'vm', architecture: null }]) {
    assert.doesNotThrow(() => vmLinkFromLaunch(r))
  }
})
