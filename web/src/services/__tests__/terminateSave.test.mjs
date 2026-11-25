// Unit tests for services/terminateSave.js (failed pre-terminate save messages)
// and the "Not saved" checkpoint row of services/vmCardState.js.
// Run with: node --test web/src/services/__tests__/terminateSave.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { saveFailureFromTerminate, terminateBody, KEPT_RUNNING } from '../terminateSave.js'
import { checkpointRows } from '../vmCardState.js'

const NOW_MS = Date.parse('2026-11-12T10:00:00Z')

test('409 saveFailed: the proxy message is shown as is', () => {
  const error = `Save failed: not enough memory to save 'big' (needs about 1300 MiB, 900 MiB available). The other variables were saved. ${KEPT_RUNNING}`
  assert.deepEqual(saveFailureFromTerminate(409, { error, saveFailed: true, reason: 'x' }), { kind: 'kept', message: error })
})

test('409 saveFailed without an error string: built from the reason', () => {
  assert.deepEqual(saveFailureFromTerminate(409, { saveFailed: true, reason: 'VM busy' }),
    { kind: 'kept', message: 'Save failed: VM busy. The VM was kept running so you can free memory or save again.' })
})

test('200 with checkpoint.vmLost: the VM stopped while saving', () => {
  const body = { status: 'terminated', checkpoint: { success: false, vmLost: true, error: 'the notebook server stopped while saving, most likely because it ran out of memory' } }
  assert.deepEqual(saveFailureFromTerminate(200, body), {
    kind: 'lost',
    message: 'Save failed: the notebook server stopped while saving, most likely because it ran out of memory. The VM stopped, so state that was only in its memory is lost.',
  })
})

test('no failure: plain success, other errors, bad bodies', () => {
  assert.equal(saveFailureFromTerminate(200, { status: 'terminated', microvmId: 'vm', sessionId: 's' }), null)
  assert.equal(saveFailureFromTerminate(409, { error: 'microvmId belongs to a different session' }), null)
  assert.equal(saveFailureFromTerminate(502, { error: 'Terminate failed: x' }), null)
  assert.equal(saveFailureFromTerminate(409, null), null)
  assert.equal(saveFailureFromTerminate(200, 'text'), null)
})

test('terminateBody: the flag only when terminating without saving', () => {
  assert.deepEqual(terminateBody('vm-1'), { microvmId: 'vm-1' })
  assert.deepEqual(terminateBody('vm-1', { withoutSaving: true }), { microvmId: 'vm-1', terminateWithoutSaving: true })
})

test('checkpointRows: not-saved variables get their own row with the reasons', () => {
  const cp = {
    saved_at: '2026-11-12T09:56:00Z', vars: 3, size_kb: 10,
    not_saved: [
      { name: 'big', kind: 'memory', reason: "not enough memory to save 'big' (needs about 1300 MiB, 900 MiB available)" },
      { name: 'conn', kind: 'unpicklable', reason: 'Connection: cannot pickle' },
    ],
  }
  const rows = checkpointRows(cp, NOW_MS)
  assert.equal(rows.notSaved, 'big, conn')
  assert.equal(rows.notSavedDetail, "not enough memory to save 'big' (needs about 1300 MiB, 900 MiB available)\nConnection: cannot pickle")
  assert.equal('notSaved' in checkpointRows({ ...cp, not_saved: [] }, NOW_MS), false)
  const kept = checkpointRows({ ...cp, not_saved: [{ ...cp.not_saved[0], kept_previous: true }] }, NOW_MS)
  assert.equal(kept.notSavedDetail, "not enough memory to save 'big' (needs about 1300 MiB, 900 MiB available) (previously saved copy kept)")
})
