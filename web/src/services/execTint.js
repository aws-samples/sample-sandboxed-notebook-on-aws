// Pure helpers for the Outline's "Tint cell by execution time" (heatmap) state.
//
// The state lives on each notebook's tab (`_heatmapOn`), not in the Outline panel.
// The panel unmounts when the user switches panels (or notebooks), so panel-local
// state would be lost before a Run All finishes. Keeping it on the tab means a run
// that finishes while the user is on another notebook still tints its own notebook
// when the user returns, and never tints a different one.

// Patch applied to a tab when its Run All completes (clean or with an error). It
// turns the tint on for that notebook and stamps the completion time. Each completion
// sets the flag again, so a new run always re-enables the tint.
export function tintAfterRunAll(now) {
  return { _lastRunAllAt: now, _heatmapOn: true }
}

// Patch applied to a tab when its Run All STARTS. It turns the tint off for that
// notebook, so the previous run's colours don't sit on cells that are about to run
// again. tintAfterRunAll turns it back on when the run completes.
export function tintBeforeRunAll() {
  return { _heatmapOn: false }
}
// True when the tint is on for this tab. A tab that has never completed a Run All
// (or whose tint was toggled off) reads as off.
export function isTintOn(tab) {
  return tab?._heatmapOn === true
}

// Patch for the Outline toggle button: flips the tint for this tab only.
export function tintTogglePatch(tab) {
  return { _heatmapOn: !isTintOn(tab) }
}
