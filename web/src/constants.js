/** Centralized UI/behavior constants — single source of truth. */

// VM instance polling interval (ms) — how often the frontend checks for VM state changes.
// 15s (not 10s) to ease ListMicrovms pressure: each /instances poll can fan out to a
// paginated list_microvms on the proxy, so a longer cadence cuts control-plane traffic
// ~33% with negligible UX lag on state dots / auto-connect / termination detection.
// The server may still override this at runtime via /config's poll_interval_ms (App.jsx).
export const DEFAULT_POLL_INTERVAL_MS = 15000

// Default MicroVM memory tier (MiB), used when a VM record has no memory_mib and
// before /image-tiers answers. The server value is DEFAULT_MEMORY_MIB in
// scripts/config.sh, returned by /image-tiers as `default_memory_mib`.
export const DEFAULT_MEMORY_MIB = 2048
// Intel generation polling intervals (ms)
export const INTEL_GENERATING_POLL_MS = 4000
export const INTEL_IDLE_POLL_MS = 10000
export const INTEL_MAX_POLL_ATTEMPTS = 200

// DataFrame grid: rows per page (client-side pagination of the VM's JSON payload).
// The VM caps the payload at NOTEBOOK_MAX_DISPLAY_ROWS (1000); the grid pages it.
export const TABLE_ROWS_PER_PAGE = 100
// Page-size options offered in the grid's page-size selector.
export const TABLE_PAGE_SIZE_OPTIONS = [25, 50, 100]

// ─── VM suspend gate ──────────────────────────────────────────────────────────
// A suspended MicroVM is asleep to save money. Idle/refresh fetches (/files,
// /variables, /packages, /datasources/catalog) must NOT be sent to a suspended VM,
// because hitting it forces an auto-resume — defeating the cost-saving suspend and
// causing 502→retry storms on the single-worker proxy (which then queue unrelated
// requests like Run History behind them). Only genuine user actions (running a
// cell) should wake a VM. This helper reads the authoritative state from the
// `instances` map (the single source of truth from useInstances).
//
// IMPORTANT: gate on SUSPENDED specifically — NOT "anything that isn't RUNNING".
// A launching/connecting/resuming VM must still be allowed through so the initial
// connect-time catalog/files fetch works; only a confirmed-asleep VM is skipped.
export function isVmSuspended(instances, microvmId) {
  if (!microvmId || !instances) return false
  const state = instances[microvmId]?.state
  return state === 'SUSPENDED' || state === 'SUSPENDING'
}

// ─── Session-alive resolution ─────────────────────────────────────────────────
// The lifecycle states in which a VM is still USABLE (running now, or asleep but
// recoverable in ~1s). Everything else (TERMINATED/TERMINATING/FAILED/UNKNOWN) is
// gone. RESUMING/PENDING are included so a VM mid-wake still reads as alive.
export const VM_ALIVE_STATES = ['RUNNING', 'SUSPENDED', 'SUSPENDING', 'PENDING', 'RESUMING']

// Resolve a session's LIVE VM from the instances map, keyed by session_id rather
// than by an exact microvm id. This is the fix for the "stale microvmId" class of
// bugs: a tab can hold an OLD vm id (reopened notebook, or the session rotated to a
// new VM) while a perfectly-alive VM for the SAME session sits in `instances` under
// a different key. Looking up by microvmId then wrongly reads "gone" — greying the
// row and popping the Connect/Restore panel for a VM that's merely SUSPENDED.
//
// Returns the [id, info] entry of the session's alive VM, or null if none. Callers
// use it both as a truthiness check ("is this session connected?") and to recover
// the real, current vm id (entry[0]) so the tab can self-heal.
export function findSessionVm(instances, sessionId) {
  if (!sessionId || !instances) return null
  for (const [id, info] of Object.entries(instances)) {
    if (info && info.session_id === sessionId && VM_ALIVE_STATES.includes((info.state || '').toUpperCase())) {
      return [id, info]
    }
  }
  return null
}

// Convenience boolean wrapper around findSessionVm for the common "is the session's
// VM alive?" check.
export function isSessionVmAlive(instances, sessionId) {
  return findSessionVm(instances, sessionId) !== null
}
