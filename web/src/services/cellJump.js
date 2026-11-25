/**
 * cellJump — scroll to, flash and focus a notebook cell by id (the same pattern as
 * Notebook.jsx openLineageStep and the cell-revert jump in App.jsx). DOM-only.
 */

function cellEl(cellId) {
  if (cellId == null || typeof document === 'undefined') return null
  const id = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(String(cellId)) : String(cellId)
  return document.querySelector(`.cell[data-cell-id="${id}"]`)
}

// True when the cell is rendered in the Notebook view right now.
export function isCellInDom(cellId) {
  return !!cellEl(cellId)
}

// Scroll the cell to the top of the notebook (its scroll-margin-top clears the
// sticky toolbar), restart the flash, make it the active cell and (optionally) move
// keyboard focus to the cell container — not its editor, which would re-scroll.
// Returns false when the cell isn't in the DOM.
export function jumpToCell(cellId, { focus = true } = {}) {
  const el = cellEl(cellId)
  if (!el) return false
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  el.classList.remove('cell-flash'); void el.offsetWidth; el.classList.add('cell-flash')
  window.dispatchEvent(new CustomEvent('notebook-active-cell', { detail: { cellId } }))
  if (focus) {
    if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1')
    el.focus({ preventScroll: true })
  }
  return true
}
