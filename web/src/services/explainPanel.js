// Pure helpers + the in-session store for the focus-mode Explanation panel.
import { splitLines } from './explain.js'

export const PANEL_MIN_WIDTH = 280
export const PANEL_MAX_FRACTION = 0.55
export const PANEL_DEFAULT_WIDTH = 380
// Below this overlay width the panel stacks UNDER the code/outputs.
export const STACK_BREAKPOINT = 900

// The panel width lives for the page session (module scope, like the explain result
// store); a reload resets it. Open/closed state is not remembered (see
// requestPanelOpen below).
let prefs = { width: PANEL_DEFAULT_WIDTH }
export const getPanelPrefs = () => ({ ...prefs })
export const setPanelPrefs = (patch) => { prefs = { ...prefs, ...patch } }
export const resetPanelPrefs = () => { prefs = { width: PANEL_DEFAULT_WIDTH }; openRequested = false }

// One-shot "open the panel" request from the notebook's Explain button. Focus mode
// otherwise opens the panel only when the cell already has an explanation (or one is
// being generated), so the remembered open/closed state no longer decides it. The
// overlay peeks at the request while mounting and clears it in an effect (an effect,
// not the state initializer, because StrictMode runs initializers twice).
let openRequested = false
export const requestPanelOpen = () => { openRequested = true }
export const peekPanelOpenRequest = () => openRequested
export const clearPanelOpenRequest = () => { openRequested = false }

/** Clamp a panel width to [280px, 55% of the container]. */
export function clampPanelWidth(width, containerWidth) {
  const max = Math.max(PANEL_MIN_WIDTH, Math.floor((containerWidth || 0) * PANEL_MAX_FRACTION))
  const w = Number.isFinite(width) ? Math.round(width) : PANEL_DEFAULT_WIDTH
  return Math.min(max, Math.max(PANEL_MIN_WIDTH, w))
}

export const isStacked = (containerWidth) => containerWidth > 0 && containerWidth < STACK_BREAKPOINT

/** "Lines 16–28" (en dash) or "Line 5". */
export function rangeChip(start, end) {
  return start === end ? `Line ${start}` : `Lines ${start}\u2013${end}`
}

/** Screen-reader text for the live region. */
export function highlightAnnouncement(start, end) {
  if (!start) return ''
  return start === end ? `Line ${start} highlighted` : `Lines ${start} to ${end} highlighted`
}

/**
 * Section line numbers refer to the code that was explained. They are only valid
 * for the current editor while the line count is unchanged.
 */
export function highlightsValid(snapshotCode, currentCode) {
  return splitLines(snapshotCode || '').length === splitLines(currentCode || '').length
}

/** The range handed to the editor: the pinned (clicked) one, else the hovered/focused one. */
export function activeRange(pinned, hovered) {
  return hovered || pinned || null
}
