// Maps an AI Fix result (from POST /ai/fix, stored as aiResult by useCellAI) to the
// verification state the fix panel renders — shared by the inline cell and focus
// mode so both say exactly the same thing.
//
// Contract with the proxy: `verified` is true ONLY when the fix agent's LAST
// execute_code run succeeded AND ran the exact code being returned. Otherwise the
// response carries `verify_reason` (no_run | last_run_failed | code_mismatch) and
// `verify_error` (the last run's full error text). A missing `verified` is treated
// as NOT verified — we never claim a fix was tested unless the server says so.

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/**
 * @param {object|null} aiResult  { type, content, verified, attempts, verifyReason, verifyError }
 * @returns {null | {
 *   status: 'verified'|'unverified',
 *   reason: null|'no_run'|'last_run_failed'|'code_mismatch',
 *   badge: string,        // short badge label (text, not colour-only)
 *   title: string,        // badge tooltip
 *   message: string|null, // warning sentence shown above the diff (unverified only)
 *   error: string,        // FULL last-run error text ('' when none) — never truncated
 * }}
 *   null when there is no fix code to apply (nothing to badge).
 */
export function fixVerificationState(aiResult) {
  if (!aiResult || aiResult.type !== 'fix' || !aiResult.content) return null
  const attempts = typeof aiResult.attempts === 'number' && aiResult.attempts > 0 ? aiResult.attempts : 0

  if (aiResult.verified === true) {
    const runs = attempts || 1
    return {
      status: 'verified',
      reason: null,
      badge: `✓ Verified · ${plural(runs, 'test run')}`,
      title: `This exact code ran without error in the sandbox before it was suggested (${plural(runs, 'test run')}).`,
      message: null,
      error: '',
    }
  }

  const reason = ['no_run', 'last_run_failed', 'code_mismatch'].includes(aiResult.verifyReason)
    ? aiResult.verifyReason
    : 'no_run'
  const error = typeof aiResult.verifyError === 'string' ? aiResult.verifyError : ''
  const runs = attempts ? ` (${plural(attempts, 'test run')})` : ''
  let message
  let title
  if (reason === 'last_run_failed') {
    message = "Couldn't verify — the last test run still failed:"
    title = `The last sandbox test run of this fix failed${runs}. Review the error before applying.`
  } else if (reason === 'code_mismatch') {
    message = "Not verified — the returned code wasn't the code that was tested."
    title = `The last successful sandbox run used different code than this fix${runs}. Review before applying.`
  } else {
    message = 'Not verified — this fix was not test-run (no connected VM, a markdown cell, or the AI did not run it).'
    title = 'This fix was never run in the sandbox. Review before applying.'
  }
  return {
    status: 'unverified',
    reason,
    badge: '⚠ Not verified',
    title,
    message,
    error: reason === 'last_run_failed' ? error : '',
  }
}
