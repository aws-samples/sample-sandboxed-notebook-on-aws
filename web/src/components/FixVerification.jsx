// Verification badge + warning for an AI Fix preview. Used by the inline cell
// (Cell.jsx) and focus mode (CellFocusOverlay.jsx) so both render the same state.
// The state mapping lives in services/fixVerification.js (unit-tested).
import { fixVerificationState } from '../services/fixVerification'

// Header badge: "✓ Verified · N test runs" or "⚠ Not verified". Text + icon carry
// the meaning; colour is only reinforcement.
export function FixVerificationBadge({ aiResult }) {
  const state = fixVerificationState(aiResult)
  if (!state) return null
  return (
    <span
      className={`cell-ai-badge ${state.status === 'verified' ? 'cell-ai-badge-verified' : 'cell-ai-badge-unverified'}`}
      title={state.title}
    >
      {state.badge}
    </span>
  )
}

// Warning shown ABOVE the diff (so it's seen before Apply) when the fix is not
// verified. The last run's error is shown in FULL in a scrollable, keyboard-
// focusable block — never truncated.
export function FixVerificationNotice({ aiResult }) {
  const state = fixVerificationState(aiResult)
  if (!state || state.status === 'verified') return null
  return (
    <div className="cell-ai-fix-verify-warning" role="status">
      <div className="cell-ai-fix-verify-message">
        <span aria-hidden="true">⚠ </span>{state.message}
      </div>
      {state.error && (
        <pre
          className="cell-ai-fix-verify-error"
          tabIndex={0}
          aria-label="Error from the last test run"
        >{state.error}</pre>
      )}
    </div>
  )
}
