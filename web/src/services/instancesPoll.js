// Who polls GET /instances (proxy-log-chatter B7). Pure, so node --test can load it.
//
// App's recurring poll (hooks/useInstances.js maybeFetch, every 15 s while the
// browser tab is visible) runs only when some notebook tab is bound to a VM or a
// session. The MicroVMs panel adds its own 15 s interval for the case that poll
// does not cover (no notebook bound: only pool spares, or no notebook open), so
// e.g. a spare's "Suspended 9s" keeps advancing. When App's poll is already
// running and feeding the panel, the panel's interval would only double the
// /instances (ListMicrovms) traffic.

/** True when App's recurring /instances poll has something to watch. */
export function appInstancesPollActive(tabs) {
  return (tabs || []).some(t => t && (t.microvmId || t.sessionId))
}

/**
 * What the MicroVMs panel's own interval should do on a tick:
 *   'skip'   App's poll is active and already feeds the panel (parentInstances non-empty)
 *   'parent' refresh through App's fetch (it owns the shared instances map)
 *   'local'  fetch the panel's own snapshot (App has nothing to give it)
 */
export function panelInstancesTick({ tabs, parentInstances, hasParentRefresh }) {
  const parentHasData = !!parentInstances && Object.keys(parentInstances).length > 0
  if (parentHasData && appInstancesPollActive(tabs)) return 'skip'
  if (hasParentRefresh && parentHasData) return 'parent'
  return 'local'
}
