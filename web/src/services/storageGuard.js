// localStorage size guard (pure, node-testable).
//
// The browser cache of every tab's cells (`microvm-notebooks`) shares one ~5 MB
// origin quota. Multi-output cells can hold several 1000-row tables or Plotly
// snippets, and when the write throws, the fallback re-saves EVERY tab with all
// outputs nulled. fitCellsForStorage runs first: if the serialized save is over
// budget it drops the rich output (`html`) of the cells that hold the most of it,
// largest first, and stops as soon as the save fits. A dropped cell gets a visible
// note appended to its text output. Only this browser copy is trimmed; the server
// copy (autosave) is untouched, and `code` is never touched.

export const STORAGE_BUDGET_CHARS = 4_000_000

export const TRIMMED_NOTE =
  "[Rich outputs omitted from this browser's cache (too large) — re-run the cell to see them]"

/**
 * @param {Array} tabsToSave tabs as written to localStorage (each with `_cells`)
 * @param {number} maxChars serialized-size budget
 * @returns {{tabs: Array, trimmedCellIds: string[]}} `tabs` is the SAME array when
 *   it already fits; otherwise a copy with the largest cells' `html` removed.
 */
export function fitCellsForStorage(tabsToSave, maxChars = STORAGE_BUDGET_CHARS) {
  const tabs = Array.isArray(tabsToSave) ? tabsToSave : []
  let total = JSON.stringify(tabs).length
  if (total <= maxChars) return { tabs: tabsToSave, trimmedCellIds: [] }
  const candidates = []
  tabs.forEach((tab, ti) => {
    ;(tab && tab._cells ? tab._cells : []).forEach((c, ci) => {
      if (c && typeof c.html === 'string' && c.html) {
        candidates.push({ ti, ci, size: JSON.stringify(c.html).length })
      }
    })
  })
  candidates.sort((a, b) => b.size - a.size)
  const drop = new Set()
  for (const cand of candidates) {
    if (total <= maxChars) break
    drop.add(`${cand.ti}:${cand.ci}`)
    // `html: "<big>"` becomes `html: null`, and the note is appended to `output`.
    total += 'null'.length - cand.size + JSON.stringify(TRIMMED_NOTE).length
  }
  if (drop.size === 0) return { tabs: tabsToSave, trimmedCellIds: [] }
  const trimmedCellIds = []
  const out = tabs.map((tab, ti) => {
    if (!tab || !Array.isArray(tab._cells)) return tab
    let touched = false
    const cells = tab._cells.map((c, ci) => {
      if (!drop.has(`${ti}:${ci}`)) return c
      touched = true
      trimmedCellIds.push(String(c.id))
      return { ...c, html: null, output: c.output ? `${c.output}\n${TRIMMED_NOTE}` : TRIMMED_NOTE }
    })
    return touched ? { ...tab, _cells: cells } : tab
  })
  return { tabs: out, trimmedCellIds }
}
