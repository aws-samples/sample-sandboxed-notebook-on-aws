// One-shot request to open a notebook's Run History in the Notebooks panel.
//
// The run banner (Outline) asks for this, then the sidebar switches to the Notebooks
// panel. That panel is usually NOT mounted yet when the request is made, so the
// request is also parked here: the panel takes it when it mounts. A panel that is
// already mounted (peek overlay) gets the window event instead.
//
// `expandLatest` (the run banner sets it): the Run History card should also open the
// newest run's drill-down. It is parked per notebook and taken once by that
// notebook's card (takeExpandLatest), whether the card mounts after the request or
// was already mounted. `stamp` is the banner's run end time (epoch ms), used to wait
// for the run's own record before opening the newest row (see latestRunToOpen).
let pending = null
const expandRequests = new Map()  // notebookId -> { stamp, trigger }
export function requestRunHistoryReveal(notebookId, { expandLatest = false, stamp = null, trigger = null } = {}) {
  pending = String(notebookId)
  if (expandLatest) expandRequests.set(pending, { stamp, trigger })
  window.dispatchEvent(new CustomEvent('open-notebooks-panel'))
  window.dispatchEvent(new CustomEvent('reveal-run-history', { detail: { notebookId: pending, expandLatest } }))
}
// Returns the parked notebook id once, then null.
export function takeRunHistoryReveal() {
  const id = pending
  pending = null
  return id
}
// Returns the parked "open the newest run" request for this notebook once, then null.
export function takeExpandLatest(notebookId) {
  const key = String(notebookId)
  const req = expandRequests.get(key) || null
  expandRequests.delete(key)
  return req
}

// Clocks: the banner stamp and the run record's finished_at both come from this
// browser's clock, taken a few ms apart at the end of the run (the record first).
const STAMP_SKEW_MS = 1500

// Which run row to open after a banner click, or null to wait for fresher data.
//   runs       the fetched runs, newest first (as the card lists them)
//   stamp      the banner's run end time (epoch ms), or null
//   cellScope  the card's cell scope (null = All cells; else a cell_id)
//   filtered   the list is filtered to Run All only
//   trigger    the banner's run kind (run_all | run_from | reactive | single)
// Freshness: the newest fetched run must have finished at or after the banner run
// (within STAMP_SKEW_MS). With no stamp, or a Run All filter that can never list
// the banner's (non Run All) run, the newest row is taken as is. The row opened is
// the first row of the current scope: in a per-cell scope, the newest run that
// includes that cell.
export function latestRunToOpen(runs, { stamp = null, cellScope = null, filtered = false, trigger = null } = {}) {
  const list = Array.isArray(runs) ? runs : []
  if (list.length === 0) return null
  const checkStamp = Number.isFinite(stamp) && !(filtered && trigger !== 'run_all')
  if (checkStamp) {
    const ts = list[0].finished_at ? Date.parse(list[0].finished_at) : NaN
    if (!(ts >= stamp - STAMP_SKEW_MS)) return null
  }
  const first = cellScope
    ? list.find(r => (Array.isArray(r.run_cells) ? r.run_cells : []).some(c => c.cell_id === cellScope))
    : list[0]
  return first ? first.run_id : null
}
