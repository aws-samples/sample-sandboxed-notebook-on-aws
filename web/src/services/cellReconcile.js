import { hasImageOutput } from './cellOutput.js'
// Store → local cell reconciliation for useNotebookCells' persist effect (STEP 1).
//
// The tab store (tab._cells) is authoritative for a cell's TERMINAL execution
// result: a batch keeps streaming after a tab switch and writes each result into the
// store even while the hook is unmounted, so on remount the local copy can be staler.
//
// This must run as a FUNCTIONAL state update — reconcileCellsFromStore(prev, store,
// base) inside setCells(prev => …). The effect sees the render's `cells` (`base`)
// and that render's store snapshot, but by the time the update applies, React may
// already hold newer local state (a cell finalized or promoted to running after the
// render). Replacing state with an array computed from the render's `cells` dropped
// those updates — the "next cell stuck running forever" batch race.
//
// Rules, per local cell (matched by id):
//  - adopt the store copy when the store copy is terminal (success/error/
//    interrupted, or carries output/html/image/error) and the local copy is stale
//    (running/pending, or no output) — but ONLY if the live cell is still the very
//    object the render saw (`base`). A cell that changed after the render is newer
//    than the store snapshot taken with that render, so it is never overwritten;
//  - self-heal the impossible running + finishedAt state (finishedAt is written only
//    by a terminal finalize) to its terminal status, on the live cell.
// Returns `prev` itself (same identity) when nothing changes, so React bails out.

const TERMINAL = new Set(['success', 'error', 'interrupted'])

const hasResult = (c) => !!(c.output || c.html || hasImageOutput(c) || c.error)

// Demote running + finishedAt (corrupt) to the cell's real terminal status, else null.
function healed(c) {
  if (c && c.status === 'running' && c.finishedAt) {
    return { ...c, status: c.error ? 'error' : 'success' }
  }
  return null
}

/**
 * @param {Array} prev  latest local cells (the functional-update argument)
 * @param {Array} store the store snapshot (tab._cells) of the render that scheduled this
 * @param {Array} [base] that render's local cells; defaults to `prev`
 * @returns {Array} `prev` when unchanged, else a new array
 */
export function reconcileCellsFromStore(prev, store, base = prev) {
  // Same precondition as before: only reconcile against a store of the same shape.
  if (!Array.isArray(prev) || !Array.isArray(store) || store.length !== prev.length) return prev
  const storeById = new Map(store.map(c => [String(c.id), c]))
  const baseById = new Map((Array.isArray(base) ? base : []).map(c => [String(c && c.id), c]))
  let changed = false
  const next = prev.map(local => {
    if (!local) return local
    const s = storeById.get(String(local.id))
    if (s && s !== local && baseById.get(String(local.id)) === local) {
      const storeTerminal = TERMINAL.has(s.status) || hasResult(s)
      const localStale = local.status === 'running' || local.status === 'pending' || !hasResult(local)
      if (storeTerminal && localStale) {
        changed = true
        return s  // take the store's finished cell (status + output/timing)
      }
    }
    const h = healed(local)
    if (h) {
      changed = true
      return h
    }
    return local
  })
  return changed ? next : prev
}
