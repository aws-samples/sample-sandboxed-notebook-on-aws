/**
 * vmOwnership — does a MicroVM belong to a notebook tab?
 *
 * The proxy routes every kernel call (`/proxy/*`) by the tab's `X-Session-Id`
 * only, so the ONLY VM a tab can actually use is the one whose `session_id`
 * equals the tab's `sessionId`. A tab whose `microvmId` points at another
 * notebook's VM (a stale id) shows that VM's badge/metrics and, worse, sent a
 * mismatched session+VM pair to /terminate (which checkpointed the other VM
 * into this session's S3 prefix and killed it). These pure helpers are the
 * single shared ownership rule for the instance poll, Terminate & Save and
 * Delete. (There is no attach path: a notebook connects only by restoring onto
 * a new VM or launching a new VM.)
 *
 * Empty-session semantics (explicit, tested): instanceSessionMismatch is TRUE
 * only when BOTH session ids are non-empty strings and DIFFER. An unknown side
 * is NOT a mismatch, so the poll never flaps and an unlinked VM keeps today's
 * behaviour.
 */
import { findSessionVm } from '../constants.js'

const sid = (v) => (typeof v === 'string' && v.length > 0 ? v : null)

/** True only when both sessions are set and differ (the VM is another notebook's). */
export function instanceSessionMismatch(inst, tab) {
  const a = sid(inst?.session_id)
  const b = sid(tab?.sessionId)
  return a !== null && b !== null && a !== b
}

/**
 * Poll decision for a tab whose microvmId IS in the instances map.
 * Returns null when the id is fine (same session, or either session unknown),
 * otherwise { repoint: [id, info] | null }: the tab's own session VM to re-point
 * to, or null when the session has no live VM (caller falls back to
 * disconnected + sessionSaved).
 */
export function staleVmRepoint(instances, tab) {
  const current = tab?.microvmId ? instances?.[tab.microvmId] : null
  if (!current || !instanceSessionMismatch(current, tab)) return null
  return { repoint: findSessionVm(instances, tab.sessionId) }
}

/**
 * Poll decision for a tab with NO microvmId (disconnected) whose session has a
 * live VM: returns that [id, info] so the tab re-links to its own VM, else null.
 * This is how a tab gets its VM back after a page reload that happened before a
 * successful restore was persisted (the tab showed disconnected while the
 * restored VM ran). Never re-adopts the VM the user just terminated
 * (tab._terminatedVmId, set by Terminate & Save, guards a poll that was in
 * flight during the terminate), and never acts while a launch is in progress.
 */
export function detachedSessionVm(instances, tab) {
  if (!tab || tab.microvmId || !sid(tab.sessionId)) return null
  if (tab.status !== 'disconnected') return null
  const own = findSessionVm(instances, tab.sessionId)
  if (!own || own[0] === tab._terminatedVmId) return null
  return own
}

/**
 * Which VM a destructive per-tab action (Delete → terminate) may target: the
 * tab's microvmId unless it belongs to another session, in which case the tab's
 * own session VM (or null = send nothing).
 */
export function terminateTargetForTab(instances, tab) {
  if (!tab?.microvmId) return null
  const current = instances?.[tab.microvmId]
  if (!current || !instanceSessionMismatch(current, tab)) return tab.microvmId
  const own = findSessionVm(instances, tab.sessionId)
  return own ? own[0] : null
}
