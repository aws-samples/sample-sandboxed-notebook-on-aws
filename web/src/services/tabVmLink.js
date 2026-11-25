// Write a just-restored VM link into the saved tabs right away.
//
// The tabs autosave (useTabsPersistence) is debounced 1.5 s. When a restore's
// /launch succeeded and the page reloaded inside that window (the "Reload
// Application" button of the error screen), the saved tab still said
// "no VM, disconnected" although the restored VM was running. The restore
// handlers call persistTabVmLink(tab.id, launchResult) as soon as /launch
// returns, before any later render can throw.

const STORAGE_KEY = 'microvm-notebooks'

// The tab fields a /launch response determines (snake_case response → tab keys).
export function vmLinkFromLaunch(result) {
  const patch = {
    microvmId: result?.microvmId,
    microvmArch: result?.architecture,
    microvmMemory: result?.memory_mib,
    sessionId: result?.sessionId,
  }
  for (const k of Object.keys(patch)) if (patch[k] == null || patch[k] === '') delete patch[k]
  if (!patch.microvmId) return null
  return { ...patch, sessionSaved: false }
}

// Pure: the saved-tabs JSON with `patch` merged into the tab `tabId`, or null
// when there is nothing to write (no saved tabs, unknown tab, bad JSON).
export function patchStoredTabs(raw, tabId, patch) {
  if (!raw || !patch) return null
  let tabs
  try { tabs = JSON.parse(raw) } catch { return null }
  if (!Array.isArray(tabs)) return null
  let hit = false
  const next = tabs.map(t => {
    if (String(t?.id) !== String(tabId)) return t
    hit = true
    return { ...t, ...patch }
  })
  return hit ? JSON.stringify(next) : null
}

// A tab that lost its session link (sessionId null: an older reload, or a
// localStorage copy from before the server re-links) while the server knows the
// notebook's last checkpointed session (GET /notebooks/{id} fills session_id,
// see proxy/notebook/session_relink.py). Returns the tab patch that brings back
// Option A "Restore from previous session", or null. sessionSaved only when no
// live VM serves that session (else the instance poll re-links the tab to it).
export function sessionRelinkPatch(tab, nb, instances = {}) {
  if (!tab || tab.status === 'connected' || tab.status === 'launching') return null
  const sid = nb?.session_id
  if (!sid || !nb?.checkpoint_enabled) return null
  // The arch/tier the server pins this session's restore to (session_arch /
  // session_memory_mib from the latest vm_sessions row), for a tab that has
  // none, so Option A shows "x86_64" instead of nothing or a default ARM64.
  const tier = {}
  if (!tab.microvmArch && nb.session_arch) tier.microvmArch = nb.session_arch
  if (!tab.microvmMemory && nb.session_memory_mib) tier.microvmMemory = nb.session_memory_mib
  if (tab.sessionId) {
    // Same session, only its arch/tier missing on the tab: fill those.
    return tab.sessionId === sid && Object.keys(tier).length ? tier : null
  }
  const live = Object.values(instances || {}).some(i => i?.session_id === sid && i.state !== 'TERMINATED')
  return { sessionId: sid, checkpointEnabled: true, ...tier, ...(live ? {} : { sessionSaved: true }) }
}

// Whether the Connect dialog may offer Option A (restore) for this tab: a saved
// checkpoint AND a session id to restore from. The instance poll sets
// sessionSaved on a tab whose VM vanished even when the tab has no sessionId
// (an older or lost-link tab); rendering Option A for it threw
// "Cannot read properties of null (reading 'slice')" on tab.sessionId and the
// whole app fell into the error screen. Returns { sessionTail } or null.
export function restoreOffer(tab, persistenceMode) {
  if (!tab || tab.status === 'connected' || !tab.sessionSaved || persistenceMode === 'none') return null
  const sid = typeof tab.sessionId === 'string' ? tab.sessionId : ''
  if (!sid) return null
  return { sessionTail: sid.slice(-12) }
}

export function persistTabVmLink(tabId, launchResult, storage = globalThis.localStorage) {
  try {
    const next = patchStoredTabs(storage.getItem(STORAGE_KEY), tabId, vmLinkFromLaunch(launchResult))
    if (next) storage.setItem(STORAGE_KEY, next)
  } catch { /* best-effort: the debounced autosave writes it again */ }
}
