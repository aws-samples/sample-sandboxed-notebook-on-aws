// Pure helpers for a cell's chart output (no React, no config) — unit-tested in
// services/__tests__/cellOutput.test.mjs.
//
// A cell's chart is either the fresh base64 `image` from the execute response
// (displayed immediately, never persisted) or, after a save/reload, only its
// persisted S3 location `imageS3Key` (fetched through the authenticated proxy
// route by PersistedCellImage). Every "does this cell have output?" gate must
// treat both the same, or a reloaded chart-only cell looks empty.

/** True when the cell has a chart: a fresh base64 image or a persisted key. */
export const hasImageOutput = (c) => !!(c && (c.image || c.imageS3Key))

// RUN-COMPLETION signature (see useTabsPersistence): one entry per cell keyed on
// `executedAt` plus the chart key. Both are set in the SAME patch as a run's
// result, so a run changes this exactly once; clearing a chart cell's output
// nulls the key and changes it too (that save persists the null and the proxy
// deletes the orphaned object).
export function runCompletionSignature(tabs) {
  return JSON.stringify((tabs || []).map(tab => [
    String(tab.id),
    (tab._cells || []).map(c => [c.id, c.executedAt || null, c.imageS3Key || null]),
  ]))
}

// Initial cell list from a tab's stored `_cells` (useNotebookCells initializer):
// PRESERVE each permanent id; mint a new one only when missing or duplicated.
// A re-minted cell drops its imageS3Key — the key belongs to the old id, and
// storage would reject it for the new one anyway.
export function restoreCellIds(cells, mintId) {
  const seen = new Set()
  return (cells || []).map(c => {
    let id = c.id
    if (!id || seen.has(id)) {
      seen.add(id)
      id = mintId()
      seen.add(id)
      return { ...c, id, imageS3Key: null }
    }
    seen.add(id)
    return { ...c, id }
  })
}
