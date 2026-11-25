// The fields every POST /launch body shares (proxy/platform/routes/microvm.py
// launch_microvm reads `name`, `notebookId`, `sessionId`, `restoreFromSession`).
// Callers spread their own extras (memoryMiB, arch, idle/max timeouts, secrets).
// `checkpointEnabled` is not sent: the proxy derives it from its persistence mode.
export function buildLaunchPayload(tab, { restore = false } = {}) {
  const payload = {
    name: tab.name,
    notebookId: tab.id,   // the notebook's DB integer id (as string; proxy coerces to int)
    sessionId: tab.sessionId,
  }
  if (restore && tab.sessionId) payload.restoreFromSession = tab.sessionId
  return payload
}
