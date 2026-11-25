// Unit tests for the Variables panel origin badge + clean-up plan.
// Run: node --test src/services/__tests__/*.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { orphanBadge, planUntrackedCleanup, liveVarUse, cleanupCandidates, joinLabels, deletedProducerVars } from '../variableOrigin.js'
import { cellBadgeMap } from '../../utils/cellBadge.js'
import { withDagProvenance } from '../depGraph.js'

const BASE = 'Not produced by any cell (e.g. left by an AI exploration or a deleted cell). Code that uses it fails on a fresh run.'
const CELLS = new Set(['c1', 'c2'])

test('agent orphan → "AI" badge with the agent title', () => {
  const b = orphanBadge({ orphan: true, orphan_reason: 'agent', defined_by: null }, CELLS)
  assert.deepEqual(b, { kind: 'agent', label: 'AI', title: `Left by an AI exploration (a run with no cell). ${BASE}` })
})

test('deleted-cell orphan → "No cell" badge with the deleted title', () => {
  const b = orphanBadge({ orphan: true, orphan_reason: 'deleted cell', defined_by: null }, CELLS)
  assert.deepEqual(b, { kind: 'deleted', label: 'No cell', title: `Its producing cell was deleted. ${BASE}` })
})

test('unknown / internal orphan → "No cell" badge with the base title', () => {
  for (const reason of ['unknown', 'internal', null, undefined]) {
    const b = orphanBadge({ orphan: true, orphan_reason: reason, defined_by: null }, CELLS)
    assert.deepEqual(b, { kind: 'unknown', label: 'No cell', title: BASE }, String(reason))
  }
})

test('cell variable → no badge', () => {
  assert.equal(orphanBadge({ orphan: false, orphan_reason: null, defined_by: 'c1' }, CELLS), null)
  assert.equal(orphanBadge({ orphan: false, defined_by: 'c1' }, null), null)
})

test('a live cell id overrides the VM "deleted cell" verdict (new, unsaved cell)', () => {
  // DAG overlay restored the raw producer id; the cell exists in the notebook now.
  assert.equal(orphanBadge({ orphan: true, orphan_reason: 'deleted cell', defined_by: 'c2' }, CELLS), null)
})

test('a producer id missing from the live cells → deleted, even if the VM says not orphan', () => {
  const b = orphanBadge({ orphan: false, orphan_reason: null, defined_by: 'gone' }, CELLS)
  assert.equal(b.kind, 'deleted')
  assert.equal(b.label, 'No cell')
})

test('numeric cell ids compare as strings', () => {
  assert.equal(orphanBadge({ orphan: false, defined_by: 7 }, new Set(['7'])), null)
})

test('without live cells the VM verdict is used', () => {
  assert.equal(orphanBadge({ orphan: true, orphan_reason: 'deleted cell', defined_by: null }, null).kind, 'deleted')
  assert.equal(orphanBadge({ orphan: false, defined_by: 'gone' }, null), null)
})

test('old VM image (no orphan field): "" fallback → "No cell" (not AI), otherwise none', () => {
  assert.equal(orphanBadge({ defined_by: '' }, CELLS).kind, 'nocell')
  assert.equal(orphanBadge({ defined_by: '' }, null).label, 'No cell')
  assert.equal(orphanBadge({ defined_by: null }, CELLS), null)
  assert.equal(orphanBadge({}, CELLS), null)
  assert.equal(orphanBadge(null, CELLS), null)
  assert.equal(orphanBadge(undefined, null), null)
})

test('withDagProvenance keeps orphan fields so the badge survives the overlay', () => {
  const doc = { schema: 1, nodes: [], edges: [], runtime: { vars: { probe: { defined_by: '', last_exec: 3 } } } }
  const vars = withDagProvenance({ probe: { type: 'int', orphan: true, orphan_reason: 'agent', defined_by: null } }, doc)
  assert.equal(vars.probe.defined_by, '')
  assert.equal(orphanBadge(vars.probe, CELLS).kind, 'agent')
})

test('cleanup plan lists the intersection of flagged and server-untracked names, sorted', () => {
  const plan = planUntrackedCleanup(
    ['dynamodb_sentiment', 'cols', 'now_a_cell_var', 'cols'],
    { visible: [{ name: 'cols', reason: 'agent' }, { name: 'dynamodb_sentiment', reason: 'agent' }, { name: 'server_only', reason: 'agent' }], internal: [] },
  )
  assert.deepEqual(plan.names, ['cols', 'dynamodb_sentiment'])
  assert.deepEqual(plan.internal, [])
  assert.equal(plan.count, 2)
  assert.equal(plan.total, 2)
  assert.deepEqual(plan.deleteNames, ['cols', 'dynamodb_sentiment'])
  assert.equal(plan.title, 'Clean up 2 untracked variables?')
  assert.equal(plan.confirmLabel, 'Delete 2')
  assert.equal(plan.internalNote, '')
})

test('cleanup plan adds internal leftovers to the delete list and the label', () => {
  const plan = planUntrackedCleanup(['x'], { visible: [{ name: 'x', reason: 'deleted cell' }], internal: ['_df', '_stats', '__upload_df_1'] })
  assert.equal(plan.count, 1)
  assert.equal(plan.total, 4)
  assert.deepEqual(plan.deleteNames, ['x', '_df', '_stats', '__upload_df_1'])
  assert.equal(plan.title, 'Clean up 1 untracked variable?')
  assert.equal(plan.confirmLabel, 'Delete 4')
  assert.equal(plan.internalNote, 'Also removes 3 hidden internal leftovers: _df, _stats, __upload_df_1')
})

test('cleanup plan with only internal leftovers', () => {
  const plan = planUntrackedCleanup([], { visible: [], internal: ['_df'] })
  assert.equal(plan.count, 0)
  assert.equal(plan.title, 'Clean up untracked leftovers?')
  assert.equal(plan.confirmLabel, 'Delete 1')
  assert.equal(plan.internalNote, 'Also removes 1 hidden internal leftover: _df')
})

test('cleanup plan keeps long names whole (no JS truncation)', () => {
  const long = 'a_very_long_variable_name_'.repeat(10)
  const plan = planUntrackedCleanup([long], { visible: [{ name: long }], internal: [] })
  assert.equal(plan.names[0], long)
  assert.equal(plan.names[0].length, long.length)
})

test('cleanup plan tolerates a malformed server list', () => {
  for (const bad of [null, undefined, {}, { visible: 'x', internal: 3 }]) {
    const plan = planUntrackedCleanup(['a'], bad)
    assert.equal(plan.count, 0)
    assert.deepEqual(plan.deleteNames, [])
  }
})

// ─── no-cell writer (deleted-cell-flag item 13) ──────────────────────────────
test('"no cell" reason (UI / API write) → "No cell" badge with the outside-any-cell title', () => {
  const b = orphanBadge({ orphan: true, orphan_reason: 'no cell', defined_by: null }, CELLS)
  assert.deepEqual(b, { kind: 'nocell', label: 'No cell', title: 'Set outside any cell (UI or API). Code that uses it fails on a fresh run.' })
  assert.deepEqual(cleanupCandidates(new Map([['v', b]])), ['v'])
})

// ─── deleted-cell flag (F1): DAG over the live cells ─────────────────────────
const LIVE = [
  { id: 'c2', type: 'code' }, { id: 'q1', type: 'sql' }, { id: 'c4', type: 'code' },
]
const node = (cell_id, position, { reads = [], writes = [], mutates = [] } = {}) =>
  ({ cell_id, position, reads, writes, mutates })
// c1 (deleted) defined x and z; c2 and c4 read x; nobody reads z.
const DAG = {
  schema: 1, edges: [], issues: [],
  nodes: [
    node('c1', 0, { writes: ['x', 'z'] }),
    node('c2', 1, { reads: ['x'], writes: ['y'] }),
    node('q1', 2, { writes: ['df'] }),
    node('c4', 3, { reads: ['x', 'df'], writes: ['df'], mutates: ['df'] }),
  ],
  runtime: { vars: { x: { defined_by: 'c1', last_cell: 'c1' }, z: { defined_by: 'c1', last_cell: 'c1' }, y: { defined_by: 'c2' } } },
}
const ctxFor = (name, cells = LIVE, dag = DAG) =>
  ({ name, use: liveVarUse(cells, dag), badges: cellBadgeMap(cells) })
const liveIds = (cells = LIVE) => new Set(cells.map(c => c.id))

test('liveVarUse: only live cells, in live order; a mutate-only cell is not a definer', () => {
  const u = liveVarUse(LIVE, DAG)
  assert.deepEqual([...u.cellIds], ['c2', 'q1', 'c4'])
  assert.deepEqual(u.readers.get('x'), ['c2', 'c4'])
  assert.equal(u.definers.has('x'), false)   // c1 is not live
  assert.deepEqual(u.definers.get('df'), ['q1'])  // c4 only mutates df
  assert.deepEqual(liveVarUse(LIVE, null).definers, new Map())
})

test('case 2: deleted producer, no live reader → plain "deleted" badge, cleanable', () => {
  const info = { orphan: true, orphan_reason: 'deleted cell', defined_by: 'c1' }
  const b = orphanBadge(info, liveIds(), ctxFor('z'))
  assert.equal(b.kind, 'deleted')
  assert.equal(b.label, 'No cell')
  assert.equal(b.inUse, undefined)
  assert.deepEqual(cleanupCandidates(new Map([['z', b]])), ['z'])
})

test('case 3: deleted producer, live readers → amber in-use badge with UI labels, excluded from clean-up', () => {
  const info = { orphan: true, orphan_reason: 'deleted cell', defined_by: null, last_cell: 'c1' }
  const b = orphanBadge(info, liveIds(), ctxFor('x'))
  assert.equal(b.kind, 'deleted')
  assert.equal(b.inUse, true)
  assert.deepEqual(b.usedBy, ['c2', 'c4'])
  assert.equal(b.label, 'deleted cell · used by PY-1, PY-2')
  assert.equal(b.title,
    "Its producing cell was deleted, but PY-1 and PY-2 still read it. It's in memory now but won't survive a restart, " +
    "a Run All on a fresh VM, or a restore. Not included in Clean up: deleting it would break those cells.")
  const one = orphanBadge(info, liveIds(), ctxFor('x', LIVE, { ...DAG, nodes: DAG.nodes.filter(n => n.cell_id !== 'c4') }))
  assert.match(one.title, /but PY-1 still reads it\..*break that cell\.$/)
  // the same row via defined_by (DAG overlay restored the raw producer id)
  assert.equal(orphanBadge({ orphan: false, defined_by: 'c1' }, liveIds(), ctxFor('x')).inUse, true)
  const badges = new Map([['x', b], ['z', orphanBadge(info, liveIds(), ctxFor('z'))]])
  assert.deepEqual(cleanupCandidates(badges), ['z'])
})

test('case 1: another live cell defines it → no badge', () => {
  const dag = { ...DAG, nodes: [...DAG.nodes, node('c5', 4, { writes: ['x'] })] }
  const cells = [...LIVE, { id: 'c5', type: 'code' }]
  const info = { orphan: true, orphan_reason: 'deleted cell', defined_by: 'c1' }
  assert.equal(orphanBadge(info, liveIds(cells), ctxFor('x', cells, dag)), null)
})

test('re-definition clears the in-use flag with no run (same info, new DAG)', () => {
  const info = { orphan: true, orphan_reason: 'deleted cell', defined_by: 'c1' }
  assert.equal(orphanBadge(info, liveIds(), ctxFor('x')).inUse, true)
  const cells = [{ id: 'c9', type: 'code' }, ...LIVE]
  const dag = { ...DAG, nodes: [node('c9', -1, { writes: ['x'] }), ...DAG.nodes] }
  assert.equal(orphanBadge(info, liveIds(cells), ctxFor('x', cells, dag)), null)
})

test('without ctx the old "deleted" behaviour holds', () => {
  const b = orphanBadge({ orphan: true, orphan_reason: 'deleted cell', defined_by: null }, CELLS)
  assert.equal(b.kind, 'deleted')
  assert.equal(b.inUse, undefined)
})

test('long names and many labels are kept whole', () => {
  const long = 'v'.repeat(300)
  const cells = Array.from({ length: 12 }, (_, i) => ({ id: `r${i}`, type: 'code' }))
  const dag = { schema: 1, edges: [], issues: [], nodes: cells.map((c, i) => node(c.id, i, { reads: [long] })) }
  const b = orphanBadge({ orphan: true, orphan_reason: 'deleted cell', defined_by: 'gone' }, liveIds(cells), ctxFor(long, cells, dag))
  assert.equal(b.usedBy.length, 12)
  assert.equal(b.label, `deleted cell · used by ${cells.map((_, i) => `PY-${i + 1}`).join(', ')}`)
})

test('joinLabels', () => {
  assert.equal(joinLabels([]), '')
  assert.equal(joinLabels(['A']), 'A')
  assert.equal(joinLabels(['A', 'B']), 'A and B')
  assert.equal(joinLabels(['A', 'B', 'C']), 'A, B and C')
})

test('deletedProducerVars: in-memory names whose producer is not a live cell', () => {
  const vars = {
    x: { defined_by: null, orphan: true, orphan_reason: 'deleted cell', last_cell: 'c1' },
    y: { defined_by: 'c2' },
    z: { defined_by: 'c1' },
    k: { defined_by: '', orphan: true, orphan_reason: 'no cell' },
    q: { defined_by: null, orphan: true, orphan_reason: 'deleted cell' },
  }
  assert.deepEqual([...deletedProducerVars(vars, LIVE, DAG)].sort(), ['q', 'x', 'z'])
  assert.deepEqual([...deletedProducerVars(vars, [...LIVE, { id: 'c1' }], DAG)].sort(), ['q'])
  assert.equal(deletedProducerVars(null, LIVE, DAG).size, 0)
})
