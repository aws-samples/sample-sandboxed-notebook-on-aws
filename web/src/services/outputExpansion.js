// Expansion state of the collapsible items of a multi-output cell (pure, node-testable).
//
// Later tables / charts / images of a cell start collapsed behind a one-line
// header and are not mounted until opened. Which ones the user opened is kept for
// the page session in a module-level map (it survives re-renders, tab switches
// and the focus overlay; a reload starts again from the defaults). The key holds
// the cell id, the item's position and a short hash of its content, so a re-run
// that produces different content starts from the defaults again.

const MAX_ENTRIES = 2000
const state = new Map()

/** Stable key for one item: cell id + index in the output list + content hash. */
export function keyFor(cellId, index, sig) {
  return `${cellId}|${index}|${sig || ''}`
}

/** The remembered state for `key`, or `fallback` when the user never toggled it. */
export function getExpanded(key, fallback) {
  return state.has(key) ? state.get(key) : !!fallback
}

/** Remember an explicit open/closed choice. */
export function setExpanded(key, value) {
  state.delete(key)
  state.set(key, !!value)
  while (state.size > MAX_ENTRIES) state.delete(state.keys().next().value)
}

/** Forget everything (tests). */
export function clearExpansion() {
  state.clear()
}
