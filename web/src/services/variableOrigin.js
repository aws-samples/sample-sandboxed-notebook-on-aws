// Variable origin for the Variables panel: is a variable produced by a cell, or
// left behind by something else (an AI exploration, a deleted cell, a write
// outside any cell, ...)?
//
// The VM classifies every variable (`orphan` + `orphan_reason` on /variables,
// see app/notebook/var_inspect.py classify_origin; the proxy refines it, see
// proxy/notebook/var_orphans.py). The web re-decides with the LIVE cell list
// when it can: a cell that was just created and run but not yet saved is
// unknown to the VM's DAG copy, so the VM can briefly report its variables as
// "deleted cell". A cell id the notebook has right now wins.
//
// Deleted-cell flag (F1): for a variable whose producing cell was deleted, the
// DAG over the notebook's CURRENT live cells decides (liveVarUse):
//   1. another live cell defines it -> no flag (it still has an owner);
//   2. no live cell reads it        -> "deleted cell", included in Clean up;
//   3. live cells read it           -> amber "deleted cell · used by PY-3, PY-5",
//                                      excluded from Clean up, still in memory.
//
// Pure helpers (no React) so they are unit-tested with node:test.

import { isDagDoc } from './depGraph.js'

const BASE_TITLE =
  'Not produced by any cell (e.g. left by an AI exploration or a deleted cell). ' +
  'Code that uses it fails on a fresh run.'

const KIND_PREFIX = {
  agent: 'Left by an AI exploration (a run with no cell). ',
  deleted: 'Its producing cell was deleted. ',
  unknown: '',
}

// A write outside any cell (a UI control, /set-variable, an API /execute with
// no cell id). Not an AI exploration (deleted-cell-flag item 13).
const NOCELL_TITLE = 'Set outside any cell (UI or API). Code that uses it fails on a fresh run.'

function badge(kind) {
  if (kind === 'nocell') return { kind, label: 'No cell', title: NOCELL_TITLE }
  return {
    kind,
    label: kind === 'agent' ? 'AI' : 'No cell',
    title: `${KIND_PREFIX[kind]}${BASE_TITLE}`,
  }
}

function kindOfReason(reason) {
  if (reason === 'agent') return 'agent'
  if (reason === 'deleted cell') return 'deleted'
  if (reason === 'no cell') return 'nocell'
  return 'unknown'
}

/** "A" · "A and B" · "A, B and C". Every label is kept whole. */
export function joinLabels(labels) {
  const l = (Array.isArray(labels) ? labels : []).filter(x => typeof x === 'string' && x !== '')
  if (l.length <= 1) return l.join('')
  return `${l.slice(0, -1).join(', ')} and ${l[l.length - 1]}`
}

const strs = (v) => (Array.isArray(v) ? v.filter(x => typeof x === 'string') : [])

/**
 * Who defines and who reads each variable, over the notebook's LIVE cells only
 * (a deleted cell's node may still be in the stored DAG until the next save).
 * Definers are `writes - mutates` (same rule as proxy/notebook/dag.py finalize);
 * readers are `reads`. Ids are listed in live-cell order.
 *
 * @param {Array<{id: any}>} cells  the notebook's live cells, in order
 * @param {object} dag              the tab's stored DAG document (tab._dag)
 * @returns {{cellIds: Set<string>, definers: Map<string,string[]>, readers: Map<string,string[]>}}
 */
export function liveVarUse(cells, dag) {
  const cellIds = new Set((Array.isArray(cells) ? cells : []).filter(Boolean).map(c => String(c.id)))
  const definers = new Map()
  const readers = new Map()
  if (!isDagDoc(dag)) return { cellIds, definers, readers }
  const nodeById = new Map()
  for (const n of dag.nodes) {
    if (n && typeof n === 'object' && n.cell_id != null) nodeById.set(String(n.cell_id), n)
  }
  const add = (map, name, id) => {
    const l = map.get(name)
    if (!l) map.set(name, [id])
    else if (!l.includes(id)) l.push(id)
  }
  for (const c of cells || []) {
    if (!c) continue
    const id = String(c.id)
    const n = nodeById.get(id)
    if (!n) continue
    const mutates = new Set(strs(n.mutates))
    for (const v of strs(n.writes)) if (!mutates.has(v)) add(definers, v, id)
    for (const v of strs(n.reads)) add(readers, v, id)
  }
  return { cellIds, definers, readers }
}

const IN_MEMORY_NOTE = "It's in memory now but won't survive a restart, a Run All on a fresh VM, or a restore."

/** The "deleted cell" badge, decided with the live DAG (cases 1-3 above). */
function deletedBadge(ctx) {
  const { name, use, badges } = ctx || {}
  if (!use || typeof name !== 'string') return badge('deleted')
  if ((use.definers.get(name) || []).length > 0) return null
  const ids = use.readers.get(name) || []
  if (ids.length === 0) return badge('deleted')
  const labels = ids.map(id => (badges && badges.get(id)) || id)
  return {
    kind: 'deleted',
    inUse: true,
    usedBy: ids,
    label: `deleted cell · used by ${labels.join(', ')}`,
    title: `Its producing cell was deleted, but ${joinLabels(labels)} still ${ids.length === 1 ? 'reads' : 'read'} it. ` +
      `${IN_MEMORY_NOTE} Not included in Clean up: deleting it would break ${ids.length === 1 ? 'that cell' : 'those cells'}.`,
  }
}

/**
 * Origin badge for one variable entry, or null when a cell produces it.
 *
 * @param {object} info     /variables entry (after withDagProvenance)
 * @param {Set<string>|null} cellIds  ids of the notebook's live cells; null when unknown
 * @param {{name?: string, use?: ReturnType<typeof liveVarUse>, badges?: Map<string,string>}} [ctx]
 *   with `name` + `use` a deleted producer is decided with the live DAG
 *   (null / plain "deleted" / in-use); without them the old behaviour holds.
 * @returns {null | {kind: 'agent'|'deleted'|'nocell'|'unknown', label: string, title: string,
 *   inUse?: true, usedBy?: string[]}}
 */
export function orphanBadge(info, cellIds, ctx = {}) {
  if (!info || typeof info !== 'object') return null
  const by = info.defined_by
  const hasId = by !== null && by !== undefined && String(by) !== ''
  if (hasId && cellIds) {
    return cellIds.has(String(by)) ? null : deletedBadge(ctx)
  }
  if (info.orphan === true) {
    const kind = kindOfReason(info.orphan_reason)
    return kind === 'deleted' ? deletedBadge(ctx) : badge(kind)
  }
  if (info.orphan === false) return null
  // Older VM images send no `orphan` field: an empty defined_by is what a run
  // without a cell id records (a UI / API write; not necessarily an AI probe).
  return by === '' ? badge('nocell') : null
}

/** Names whose badge may be cleaned up: every flagged one except "in use" (case 3). */
export function cleanupCandidates(originBadges) {
  const out = []
  for (const [name, b] of originBadges instanceof Map ? originBadges : []) {
    if (b && !b.inUse) out.push(name)
  }
  return out
}

/**
 * In-memory variables whose producer was a cell that is no longer in the
 * notebook. Used to explain the "1!" missing-producer badge.
 *
 * @param {object} variables  /variables map (name -> entry)
 * @param {Array<{id:any}>} cells  live cells
 * @param {object} dag        stored DAG document
 * @returns {Set<string>}
 */
export function deletedProducerVars(variables, cells, dag) {
  const out = new Set()
  if (!variables || typeof variables !== 'object') return out
  const live = new Set((Array.isArray(cells) ? cells : []).filter(Boolean).map(c => String(c.id)))
  const rt = isDagDoc(dag) && dag.runtime && typeof dag.runtime.vars === 'object' && dag.runtime.vars
    ? dag.runtime.vars : {}
  for (const [name, info] of Object.entries(variables)) {
    const prov = rt[name]
    let by = prov && prov.defined_by != null ? prov.defined_by : info && info.defined_by
    if ((by === null || by === undefined || by === '') && info && info.orphan_reason === 'deleted cell') {
      by = info.last_cell ?? (prov && prov.last_cell)
      if (by === null || by === undefined || by === '') { out.add(name); continue }
    }
    if (by !== null && by !== undefined && String(by) !== '' && !live.has(String(by))) out.add(name)
  }
  return out
}

/**
 * The confirmation plan for "Clean up N untracked variables".
 *
 * Only names that BOTH the panel flagged and the VM still reports as untracked
 * are listed (the VM re-checks again on delete). Internal leftovers
 * (`_`-prefixed probe names, `__upload_*`) are hidden from the panel, so they
 * are reported as a separate line. Labels are full strings (no truncation);
 * the UI clips long names with CSS.
 *
 * @param {string[]} flaggedNames  names the panel badged
 * @param {{visible?: {name: string}[], internal?: string[]}} serverList  GET /variables/untracked
 */
export function planUntrackedCleanup(flaggedNames, serverList) {
  const serverVisible = new Set(
    (Array.isArray(serverList?.visible) ? serverList.visible : [])
      .map(v => (v && typeof v === 'object' ? v.name : v))
      .filter(n => typeof n === 'string' && n !== ''),
  )
  const seen = new Set()
  const names = []
  for (const n of Array.isArray(flaggedNames) ? flaggedNames : []) {
    if (typeof n === 'string' && serverVisible.has(n) && !seen.has(n)) {
      seen.add(n)
      names.push(n)
    }
  }
  names.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }))
  const internal = (Array.isArray(serverList?.internal) ? serverList.internal : [])
    .filter(n => typeof n === 'string' && n !== '' && !seen.has(n))
  const count = names.length
  const total = count + internal.length
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
  return {
    names,
    internal,
    count,
    total,
    deleteNames: [...names, ...internal],
    title: count > 0
      ? `Clean up ${plural(count, 'untracked variable')}?`
      : 'Clean up untracked leftovers?',
    message: 'These were not produced by any cell. Deleting them removes them from the session; ' +
      'cells that read them will show a missing-producer issue.',
    internalNote: internal.length > 0
      ? `Also removes ${plural(internal.length, 'hidden internal leftover')}: ${internal.join(', ')}`
      : '',
    confirmLabel: internal.length > 0 ? `Delete ${total}` : `Delete ${count}`,
  }
}
