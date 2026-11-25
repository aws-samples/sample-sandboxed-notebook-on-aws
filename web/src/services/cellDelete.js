// Pure helpers for the "Delete cell" confirmation.
//
// Every user-facing delete path goes through App's requestDeleteCells, which uses
// planCellDelete() to decide whether to show the confirmation and what it says.
// Nothing here touches React or the DOM, so the decision is unit-tested directly.

import { joinLabels, liveVarUse } from './variableOrigin.js'

// Shown in the dialog. Deleting a cell has no undo in the notebook itself. The
// Version history restore is the only way back, and it restores the whole
// notebook, not one cell, so the text says that. Any earlier Auto (periodic) or
// Saved (manual) version counts, because both can be restored.
export const DELETE_UNDO_NOTE =
  "This can't be undone here. If a version from before this delete exists (Auto or Saved), Version history can restore the notebook to that version."

// How many cells the dialog lists by name before summarising the rest as "+M more".
export const DELETE_LIST_LIMIT = 5

const TYPE_NAMES = { code: 'code', sql: 'SQL', markdown: 'text' }

/** Human name for a cell type, used in the dialog: "code", "SQL", "text". */
export function cellTypeName(type) {
  return TYPE_NAMES[type || 'code'] || 'code'
}

/**
 * A cell is "empty" when there is nothing in it to lose: no code (ignoring
 * whitespace), no output, no error, no rendered html/image, and no AI explanation
 * or user note. Deleting an empty cell skips the confirmation.
 */
export function isEmptyCell(cell) {
  if (!cell) return true
  if (String(cell.code ?? '').trim() !== '') return false
  if (cell.output || cell.error || cell.html || cell.image || cell.imageS3Key) return false
  if (String(cell.aiExplanation ?? '').trim() !== '') return false
  return true
}

/**
 * One-line description of a cell for the dialog: its first non-blank line of code.
 * The full text is returned; the UI shows it with CSS ellipsis and a title tooltip,
 * so no characters are cut here.
 */
export function cellSummary(cell) {
  const firstLine = String(cell?.code ?? '').split('\n').find(l => l.trim() !== '')
  if (firstLine !== undefined) return firstLine.trim()
  return isEmptyCell(cell) ? '(empty cell)' : '(no code)'
}

/**
 * Decide whether a delete needs confirmation and build the dialog content.
 *
 * @param {Array<object>} cells      the cells about to be deleted (cell objects)
 * @param {Map<string,string>} badgeById  cell id (String) -> badge, e.g. "PY-23"
 * @param {{allCells?: Array<object>, dag?: object}} [ctx]  the notebook's live cells
 *   and the tab's stored DAG; with both, `dependentsNote` lists the variables
 *   these cells define that other live cells read and no other live cell defines
 * @returns {{
 *   needsConfirm: boolean,
 *   count: number,
 *   title: string,
 *   items: Array<{ badge: string, typeName: string, summary: string }>,
 *   moreCount: number,
 *   undoNote: string,
 *   dependentsNote: null | {
 *     kind: 'single'|'multi', subject: string, verb: string, intro?: string, text: string,
 *     items: Array<{ name: string, usedBy: string[], usedByText: string }>,
 *   },
 * }}
 *
 * needsConfirm is false when there is nothing to delete or every cell is empty.
 * Otherwise the whole set is confirmed with one dialog, including any empty cells
 * in it, so the count shown always matches what gets removed.
 */
export function planCellDelete(cells, badgeById = new Map(), ctx = {}) {
  const list = Array.isArray(cells) ? cells.filter(Boolean) : []
  const count = list.length
  const needsConfirm = count > 0 && !list.every(isEmptyCell)
  const describe = (cell) => {
    const badge = badgeById.get(String(cell.id)) || ''
    return { badge, typeName: cellTypeName(cell.type), summary: cellSummary(cell) }
  }
  const items = list.slice(0, DELETE_LIST_LIMIT).map(describe)
  const moreCount = Math.max(0, count - DELETE_LIST_LIMIT)

  let title = ''
  if (count === 1) {
    const { badge, typeName } = items[0]
    title = badge ? `Delete ${badge} (${typeName} cell)?` : `Delete this ${typeName} cell?`
  } else if (count > 1) {
    title = `Delete ${count} cells?`
  }

  const dependentsNote = buildDependentsNote(deleteDependents(list, badgeById, ctx), count)
  return { needsConfirm, count, title, items, moreCount, undoNote: DELETE_UNDO_NOTE, dependentsNote }
}

/**
 * Variables the cells being deleted define (writes - mutates, per their DAG
 * node) that another live, non-deleted cell reads, while no other live,
 * non-deleted cell defines them. Readers are listed by badge in notebook order.
 * Deduplicated, in target order and then by name. Names are never cut.
 */
export function deleteDependents(targets, badgeById = new Map(), ctx = {}) {
  const allCells = Array.isArray(ctx?.allCells) ? ctx.allCells.filter(Boolean) : []
  const dag = ctx?.dag
  if (!allCells.length || !dag || !Array.isArray(dag.nodes)) return []
  const targetIds = new Set((targets || []).map(c => String(c.id)))
  const remaining = allCells.filter(c => !targetIds.has(String(c.id)))
  const rest = liveVarUse(remaining, dag)
  const own = liveVarUse((targets || []).filter(Boolean), dag)
  const out = []
  const seen = new Set()
  for (const t of targets || []) {
    if (!t) continue
    const tid = String(t.id)
    const names = [...own.definers.entries()]
      .filter(([, ids]) => ids.includes(tid))
      .map(([name]) => name)
      .sort((a, b) => a.localeCompare(b))
    for (const name of names) {
      if (seen.has(name)) continue
      if ((rest.definers.get(name) || []).length > 0) continue
      const readers = rest.readers.get(name) || []
      if (readers.length === 0) continue
      seen.add(name)
      const usedBy = readers.map(id => badgeById.get(id) || id)
      out.push({ name, usedBy, usedByText: joinLabels(usedBy) })
    }
  }
  return out
}

const KEEP_NOTE = "They'll keep working until the kernel restarts."

function buildDependentsNote(items, count) {
  if (!items.length) return null
  const subject = count === 1 ? 'This cell' : 'These cells'
  const verb = count === 1 ? 'defines' : 'define'
  if (items.length === 1) {
    const [i] = items
    return {
      kind: 'single', subject, verb, items,
      text: `${subject} ${verb} ${i.name}, used by ${i.usedByText}. ${KEEP_NOTE}`,
    }
  }
  const intro = `${subject} ${verb} variables other cells use. Those cells will keep working until the kernel restarts:`
  return {
    kind: 'multi', subject, verb, intro, items,
    text: `${intro} ${items.map(i => `${i.name} (used by ${i.usedByText})`).join('; ')}`,
  }
}
