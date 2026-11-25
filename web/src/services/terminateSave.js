// Outcome of POST /terminate when its pre-terminate save did not work.
//
// The proxy keeps the VM running when the save fails and answers 409
//   {"error": "Save failed: <reason>. The VM was kept running so you can free
//    memory or save again.", "saveFailed": true, "reason", "notSaved"}.
// When the notebook server died during the save (out of memory), the VM is
// gone: the proxy cleans up and answers 200 with
//   {"status": "terminated", "checkpoint": {"success": false, "error", "vmLost": true}}.
// Body {"terminateWithoutSaving": true} skips the save.

export const KEPT_RUNNING = 'The VM was kept running so you can free memory or save again.'

/** { kind: 'kept' | 'lost', message } for a /terminate response whose save
 *  failed, or null (no failure, or a response that is not a save failure). */
export function saveFailureFromTerminate(status, body) {
  if (!body || typeof body !== 'object') return null
  if (status === 409 && body.saveFailed === true) {
    const message = typeof body.error === 'string' && body.error
      ? body.error
      : `Save failed: ${body.reason || 'unknown reason'}. ${KEPT_RUNNING}`
    return { kind: 'kept', message }
  }
  const cp = body.checkpoint
  if (status >= 200 && status < 300 && cp && cp.success === false && cp.vmLost === true) {
    const reason = String(cp.error || 'the notebook server stopped while saving')
    return {
      kind: 'lost',
      message: `Save failed: ${reason}. The VM stopped, so state that was only in its memory is lost.`,
    }
  }
  return null
}

/** Request body for /terminate. */
export function terminateBody(microvmId, { withoutSaving = false } = {}) {
  return withoutSaving ? { microvmId, terminateWithoutSaving: true } : { microvmId }
}
