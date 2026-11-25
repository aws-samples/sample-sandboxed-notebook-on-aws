// Pure rules for the Outline's temporary run banner ("Run All finished: ...",
// "Ran 3 cells in 4.2 s, ..."). Shared banner rules (timer, slots, clearing) live
// in services/actionBanner.js; this file holds the run-specific summary and text.
//
// State lives on each notebook's tab as `_runBanner` (see services/execTint.js for
// why: the Outline panel unmounts when the user switches panels or notebooks, and a
// run can end while another notebook is showing). The banner is written by a
// tab-id-bound update, so it only ever lands on the notebook that ran.
//
// WHICH RUNS GET A BANNER:
//   Run All        always (buildBanner, written by executeAllCells)
//   any other run  run-from, run-selected, a reactive cascade, a single cell
//                  (buildRunBanner, written by finishRun) when it took >= 2 s, had
//                  an error, or was stopped (actionBanner.shouldShowRunBanner).
//
// WHEN THE BANNER GOES AWAY:
//   Cleared (the tab's `_runBanner` is set to null):
//     auto-dismiss   10 s after it is first visible in the Outline; hover or keyboard
//                    focus pauses it, leaving restarts the full 10 s (ActionBanner)
//     dismiss        the user presses the x button
//     open-history   the user clicks the banner (it navigates to Run History), or
//                    the Notebooks panel shows this notebook's Run History
//     secondary      "Go to first error" (jumps to the failed cell)
//     run-start      any run starts: Run All, run-from, run-selected, a single cell
//                    run, a reactive cascade
//   Hidden without clearing (derived, see bannerVisible):
//     edit           any change to the cell list: a cell's code, type, order, or a cell
//                    added or removed (compared through a signature taken when the
//                    run finished).
//     other notebook the banner is per notebook; the Outline only shows the active
//                    notebook's banner.
//   Never clears it: scrolling, hovering, selecting or focusing a cell, toggling the
//   heat map, switching the sidebar panel.
import { formatDuration } from '../utils/formatDuration.js'
import { shouldShowRunBanner } from './actionBanner.js'


// Patch that removes the banner from a tab.
export function clearBannerPatch() {
  return { _runBanner: null }
}

// Cheap, order-sensitive signature of the cell list: id, type and a hash of the code.
// Statuses, outputs and timings are left out, so running cells never changes it.
export function cellsSignature(cells) {
  let out = ''
  for (const c of cells || []) {
    const code = c.code || ''
    let h = 5381
    for (let i = 0; i < code.length; i++) h = ((h * 33) ^ code.charCodeAt(i)) >>> 0
    out += `${c.id}:${c.type || 'code'}:${code.length}:${h};`
  }
  return out
}

const toneOf = (errors, stopped) => (errors > 0 ? 'error' : stopped > 0 ? 'warning' : 'success')

// First failed cell in notebook order (ids compared as strings), or null.
function firstErrorIn(errorIds, cells) {
  if (errorIds.length === 0) return null
  const pos = new Map((cells || []).map((c, i) => [String(c.id), i]))
  const at = id => (pos.has(String(id)) ? pos.get(String(id)) : Infinity)
  return [...errorIds].sort((a, b) => at(a) - at(b))[0]
}

// Summarise a finished Run All into a banner, or null when there is nothing to show.
//   cells       latest cell list (with statuses)
//   ranIds      ids the Run All asked to run
//   startedAt   epoch ms when the run started (cells finished before it are ignored,
//               so an earlier run's statuses never count when a Run All was refused)
//   finishedAt  epoch ms when the run ended
// executed = cells that reached success or error in this run. stopped = every other
// cell of the run (interrupted, still queued, reset to idle by the interrupt).
// Nothing finished -> null.
export function buildBanner({ cells, ranIds, startedAt, finishedAt }) {
  const ids = new Set((ranIds || []).map(String))
  let executed = 0
  let total = 0
  const errorIds = []
  for (const c of cells || []) {
    if (!ids.has(String(c.id))) continue
    total++
    const finishedThisRun = typeof c.finishedAt === 'number' && c.finishedAt >= startedAt
    if (c.status === 'success' && finishedThisRun) executed++
    else if (c.status === 'error' && finishedThisRun) { executed++; errorIds.push(c.id) }
  }
  if (executed === 0) return null
  const errors = errorIds.length
  const stopped = total - executed
  const elapsedMs = Math.max(0, (finishedAt || 0) - (startedAt || 0))
  return {
    trigger: 'run_all', stamp: finishedAt, executed, errors, stopped, elapsedMs,
    tone: toneOf(errors, stopped), firstErrorId: firstErrorIn(errorIds, cells), sig: cellsSignature(cells),
  }
}

// Summarise any other finished run (run-from, run-selected, reactive, single cell)
// from the per-run outcome tally, or null when it gets no banner.
//   results     Map cellId -> { status } of the cells that finished (or were
//               interrupted) in this run
//   ranIds      every cell id the run asked to run
//   cells       latest cell list (signature and first-error order)
//   trigger     run_from | reactive | single
export function buildRunBanner({ results, ranIds, cells, startedAt, finishedAt, trigger = 'single' }) {
  const all = new Set([...(ranIds || [])].map(String))
  let executed = 0
  const errorIds = []
  for (const [id, o] of results || []) {
    all.add(String(id))
    if (o?.status === 'success') executed++
    else if (o?.status === 'error') { executed++; errorIds.push(id) }
  }
  if (executed === 0) return null
  const errors = errorIds.length
  const stopped = all.size - executed
  const elapsedMs = Math.max(0, (finishedAt || 0) - (startedAt || 0))
  if (!shouldShowRunBanner({ elapsedMs, errors, stopped })) return null
  return {
    trigger, stamp: finishedAt, executed, errors, stopped, elapsedMs,
    tone: toneOf(errors, stopped), firstErrorId: firstErrorIn(errorIds, cells), sig: cellsSignature(cells),
  }
}

// The banner is shown for the active notebook only, while its cells still match the
// run that produced it.
export function bannerVisible(banner, cells) {
  return !!banner && banner.sig === cellsSignature(cells)
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

// Banner copy. Returns { lead, detail, action, secondary } so the UI can style the
// parts.
//   lead       "Run All finished: 12 cells in 26.5 s" | "Ran 3 cells in 4.2 s"
//              "Run All stopped: ..." | "Run stopped: ..." when cells were left unrun
//   detail     "0 errors" | "2 errors" | "2 errors, 3 not run"
//   action     "View run history"
//   secondary  "Go to first error" when a cell failed, else null
export function bannerText(b) {
  const time = formatDuration(b.elapsedMs)
  const cells = plural(b.executed, 'cell', 'cells')
  const span = time ? ` in ${time}` : ''
  let lead
  if (b.trigger === 'run_all' || b.trigger == null) {
    lead = `${b.stopped > 0 ? 'Run All stopped' : 'Run All finished'}: ${cells}${span}`
  } else {
    lead = b.stopped > 0 ? `Run stopped: ${cells}${span}` : `Ran ${cells}${span}`
  }
  const parts = [plural(b.errors, 'error', 'errors')]
  if (b.stopped > 0) parts.push(`${b.stopped} not run`)
  const secondary = b.errors > 0 && b.firstErrorId != null ? 'Go to first error' : null
  return { lead, detail: parts.join(', '), action: 'View run history', secondary }
}
