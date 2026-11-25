// Unit tests for the AI Fix verification-state mapping used by the fix panel
// (inline cell + focus mode). Run: node --test src/services/__tests__/*.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fixVerificationState } from '../fixVerification.js'

const RC_ERROR = "RuntimeError: restcountries returned an error payload {'status': 400, 'message': 'Bad Request'} (line 12)\n" +
  'x'.repeat(4000)

const fix = (extra) => ({ type: 'fix', content: 'df = load()', ...extra })

test('no panel state without fix code (nothing to apply)', () => {
  assert.equal(fixVerificationState(null), null)
  assert.equal(fixVerificationState({ type: 'explain', content: 'x' }), null)
  assert.equal(fixVerificationState({ type: 'fix', content: '', verified: false }), null)
})

test('verified → success badge with run count, no warning', () => {
  const s = fixVerificationState(fix({ verified: true, attempts: 3 }))
  assert.equal(s.status, 'verified')
  assert.equal(s.badge, '✓ Verified · 3 test runs')
  assert.equal(s.message, null)
  assert.equal(s.error, '')
  assert.equal(fixVerificationState(fix({ verified: true, attempts: 1 })).badge, '✓ Verified · 1 test run')
})

test('last_run_failed → warning + the FULL error text (not truncated)', () => {
  const s = fixVerificationState(fix({ verified: false, attempts: 3, verifyReason: 'last_run_failed', verifyError: RC_ERROR }))
  assert.equal(s.status, 'unverified')
  assert.equal(s.reason, 'last_run_failed')
  assert.equal(s.badge, '⚠ Not verified')
  assert.match(s.message, /last test run still failed/)
  assert.equal(s.error, RC_ERROR)
  assert.match(s.title, /3 test runs/)
})

test('code_mismatch → "returned code wasn\'t the code that was tested"', () => {
  const s = fixVerificationState(fix({ verified: false, attempts: 2, verifyReason: 'code_mismatch', verifyError: '' }))
  assert.equal(s.status, 'unverified')
  assert.equal(s.reason, 'code_mismatch')
  assert.match(s.message, /wasn't the code that was tested/)
  assert.equal(s.error, '')
})

test('no_run → "not test-run" warning', () => {
  const s = fixVerificationState(fix({ verified: false, attempts: 0, verifyReason: 'no_run' }))
  assert.equal(s.reason, 'no_run')
  assert.match(s.message, /not test-run/)
})

test('missing verified / unknown reason → treated as NOT verified (no_run)', () => {
  for (const r of [fix({}), fix({ verified: false, verifyReason: 'weird' }), fix({ verified: 'yes' })]) {
    const s = fixVerificationState(r)
    assert.equal(s.status, 'unverified')
    assert.equal(s.reason, 'no_run')
  }
})

test('verifyError is only shown for last_run_failed', () => {
  const s = fixVerificationState(fix({ verified: false, verifyReason: 'code_mismatch', verifyError: 'stale' }))
  assert.equal(s.error, '')
})
