// Unit tests for services/cellDelete.js (the "Delete cell" confirmation decision).
// Run with: node --test web/src/services/__tests__/cellDelete.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  planCellDelete, isEmptyCell, cellSummary, cellTypeName, DELETE_UNDO_NOTE, DELETE_LIST_LIMIT,
} from '../cellDelete.js'

const code = (id, text, extra = {}) => ({ id, type: 'code', code: text, output: null, error: null, html: null, image: null, imageS3Key: null, aiExplanation: null, ...extra })
const badges = (cells) => new Map(cells.map((c, i) => [String(c.id), `PY-${i + 1}`]))

test('isEmptyCell: no code, output, error, image, html or note means empty', () => {
  assert.equal(isEmptyCell(code('a', '')), true)
  assert.equal(isEmptyCell(code('a', '   \n\t ')), true)
  assert.equal(isEmptyCell(code('a', 'x = 1')), false)
  assert.equal(isEmptyCell(code('a', '', { output: 'hi' })), false)
  assert.equal(isEmptyCell(code('a', '', { error: 'boom' })), false)
  assert.equal(isEmptyCell(code('a', '', { html: '<b>x</b>' })), false)
  assert.equal(isEmptyCell(code('a', '', { image: 'png' })), false)
  assert.equal(isEmptyCell(code('a', '', { imageS3Key: 'k' })), false)
  assert.equal(isEmptyCell(code('a', '', { aiExplanation: 'why' })), false)
  assert.equal(isEmptyCell(null), true)
})

test('isEmptyCell: a cell whose only content is output still counts as non-empty', () => {
  assert.equal(isEmptyCell({ id: 'a', type: 'markdown', code: '', output: 'x' }), false)
})

test('cellSummary: first non-blank line, trimmed, full length (no cut)', () => {
  const long = 'df = spark.read.parquet("s3://bucket/very/long/path/that/goes/on/and/on/for/a/while/indeed.parquet")'
  assert.equal(cellSummary(code('a', `\n\n   ${long}\nprint(df)`)), long)
  assert.equal(cellSummary(code('a', '')), '(empty cell)')
  assert.equal(cellSummary(code('a', '', { output: 'x' })), '(no code)')
})

test('cellTypeName: code, SQL and text', () => {
  assert.equal(cellTypeName('code'), 'code')
  assert.equal(cellTypeName('sql'), 'SQL')
  assert.equal(cellTypeName('markdown'), 'text')
  assert.equal(cellTypeName(undefined), 'code')
})

test('single non-empty cell: confirms and titles it with its badge and type', () => {
  const cells = [code('x', 'import pandas as pd\nprint(1)')]
  const plan = planCellDelete(cells, badges(cells))
  assert.equal(plan.needsConfirm, true)
  assert.equal(plan.count, 1)
  assert.equal(plan.title, 'Delete PY-1 (code cell)?')
  assert.deepEqual(plan.items, [{ badge: 'PY-1', typeName: 'code', summary: 'import pandas as pd' }])
  assert.equal(plan.moreCount, 0)
  assert.equal(plan.undoNote, DELETE_UNDO_NOTE)
})

test('single SQL cell label uses the SQL type name', () => {
  const cells = [{ id: 's', type: 'sql', code: 'SELECT 1', output: null }]
  const plan = planCellDelete(cells, new Map([['s', 'SQL-2']]))
  assert.equal(plan.title, 'Delete SQL-2 (SQL cell)?')
})

test('single cell without a known badge still gets a sensible title', () => {
  const plan = planCellDelete([code('z', 'x = 1')], new Map())
  assert.equal(plan.title, 'Delete this code cell?')
  assert.equal(plan.items[0].badge, '')
})

test('single empty cell: no confirmation needed', () => {
  const cells = [code('e', '  ')]
  const plan = planCellDelete(cells, badges(cells))
  assert.equal(plan.needsConfirm, false)
  assert.equal(plan.count, 1)
})

test('bulk where every cell is empty: no confirmation', () => {
  const cells = [code('e1', ''), code('e2', '\n')]
  assert.equal(planCellDelete(cells, badges(cells)).needsConfirm, false)
})

test('bulk with one non-empty cell among empties: one confirmation for all of them', () => {
  const cells = [code('e1', ''), code('k', 'x = 1'), code('e2', '')]
  const plan = planCellDelete(cells, badges(cells))
  assert.equal(plan.needsConfirm, true)
  assert.equal(plan.count, 3)
  assert.equal(plan.title, 'Delete 3 cells?')
  assert.equal(plan.items.length, 3)
})

test('bulk of 2 to 5 cells lists every label with no "more" line', () => {
  const cells = [code('a', 'a = 1'), code('b', 'b = 2'), code('c', 'c = 3')]
  const plan = planCellDelete(cells, badges(cells))
  assert.equal(plan.title, 'Delete 3 cells?')
  assert.deepEqual(plan.items.map(i => i.badge), ['PY-1', 'PY-2', 'PY-3'])
  assert.equal(plan.moreCount, 0)
})

test('bulk of more than 5 cells lists 5 and reports the rest as +M more', () => {
  const cells = Array.from({ length: 9 }, (_, i) => code(`c${i}`, `v${i} = ${i}`))
  const plan = planCellDelete(cells, badges(cells))
  assert.equal(DELETE_LIST_LIMIT, 5)
  assert.equal(plan.count, 9)
  assert.equal(plan.title, 'Delete 9 cells?')
  assert.equal(plan.items.length, 5)
  assert.equal(plan.items[4].badge, 'PY-5')
  assert.equal(plan.moreCount, 4)
})

test('exactly 6 cells: 5 listed and 1 more', () => {
  const cells = Array.from({ length: 6 }, (_, i) => code(`c${i}`, `x${i}`))
  const plan = planCellDelete(cells, badges(cells))
  assert.equal(plan.items.length, 5)
  assert.equal(plan.moreCount, 1)
})

test('empty input: nothing to confirm and no title', () => {
  const plan = planCellDelete([], new Map())
  assert.equal(plan.needsConfirm, false)
  assert.equal(plan.count, 0)
  assert.equal(plan.title, '')
  assert.deepEqual(plan.items, [])
})

test('ids are matched as strings in the badge map', () => {
  const cells = [code(42, 'answer = 42')]
  const plan = planCellDelete(cells, new Map([['42', 'PY-7']]))
  assert.equal(plan.title, 'Delete PY-7 (code cell)?')
})

test('the undo note says deletes cannot be undone here and points to Version history', () => {
  assert.match(DELETE_UNDO_NOTE, /can't be undone/)
  assert.match(DELETE_UNDO_NOTE, /Version history/)
})
test('the undo note names Auto and Saved versions as the ones that can restore', () => {
  assert.match(DELETE_UNDO_NOTE, /Auto or Saved/)
  assert.doesNotMatch(DELETE_UNDO_NOTE, /you saved a version/)
})

// ─── dependents note (deleted-cell flag F1, item 4) ──────────────────────────
const dnode = (cell_id, position, { reads = [], writes = [], mutates = [] } = {}) =>
  ({ cell_id, position, reads, writes, mutates })
const DCELLS = [code('a', 'x = 1'), code('b', 'y = x + 1'), code('c', 'z = 5'), code('d', 'print(x, y)'), code('e', 'w = y')]
const DDAG = {
  schema: 1, edges: [], issues: [],
  nodes: [
    dnode('a', 0, { writes: ['x'] }),
    dnode('b', 1, { reads: ['x'], writes: ['y'] }),
    dnode('c', 2, { writes: ['z'] }),
    dnode('d', 3, { reads: ['x', 'y'] }),
    dnode('e', 4, { reads: ['y'], writes: ['w'] }),
  ],
}
const KEEP = "They'll keep working until the kernel restarts."

test('dependents: one variable → single-line text with readers by badge in notebook order', () => {
  const plan = planCellDelete([DCELLS[0]], badges(DCELLS), { allCells: DCELLS, dag: DDAG })
  assert.equal(plan.dependentsNote.kind, 'single')
  assert.deepEqual(plan.dependentsNote.items, [{ name: 'x', usedBy: ['PY-2', 'PY-4'], usedByText: 'PY-2 and PY-4' }])
  assert.equal(plan.dependentsNote.text, `This cell defines x, used by PY-2 and PY-4. ${KEEP}`)
})

test('dependents: several variables → multi with every name and reader list', () => {
  const cells = [code('m', 'x = 1; y = 2'), code('r1', 'x + y'), code('r2', 'y'), code('r3', 'y')]
  const dag = { schema: 1, edges: [], issues: [], nodes: [
    dnode('m', 0, { writes: ['y', 'x'] }), dnode('r1', 1, { reads: ['x', 'y'] }),
    dnode('r2', 2, { reads: ['y'] }), dnode('r3', 3, { reads: ['y'] }),
  ] }
  const note = planCellDelete([cells[0]], badges(cells), { allCells: cells, dag }).dependentsNote
  assert.equal(note.kind, 'multi')
  assert.deepEqual(note.items.map(i => [i.name, i.usedByText]), [['x', 'PY-2'], ['y', 'PY-2, PY-3 and PY-4']])
  assert.equal(note.intro, 'This cell defines variables other cells use. Those cells will keep working until the kernel restarts:')
  assert.equal(note.text, `${note.intro} x (used by PY-2); y (used by PY-2, PY-3 and PY-4)`)
})

test('dependents: a multi-cell delete says "These cells define"', () => {
  const note = planCellDelete([DCELLS[0], DCELLS[2]], badges(DCELLS), { allCells: DCELLS, dag: DDAG }).dependentsNote
  assert.equal(note.text, `These cells define x, used by PY-2 and PY-4. ${KEEP}`)
})

test('dependents: none → dependentsNote null and the rest of the plan unchanged', () => {
  const withCtx = planCellDelete([DCELLS[2]], badges(DCELLS), { allCells: DCELLS, dag: DDAG })
  assert.equal(withCtx.dependentsNote, null)
  const { dependentsNote, ...rest } = withCtx
  const { dependentsNote: d0, ...old } = planCellDelete([DCELLS[2]], badges(DCELLS))
  assert.equal(d0, null)
  assert.deepEqual(rest, old)
  assert.equal(dependentsNote, null)
})

test('dependents: another live cell also defines it → no dependent', () => {
  const cells = [...DCELLS, code('f', 'x = 3')]
  const dag = { ...DDAG, nodes: [...DDAG.nodes, dnode('f', 5, { writes: ['x'] })] }
  assert.equal(planCellDelete([cells[0]], badges(cells), { allCells: cells, dag }).dependentsNote, null)
})

test('dependents: a mutate-only reader/writer elsewhere does not count as a definer', () => {
  const cells = [...DCELLS, code('f', 'x.append(1)')]
  const dag = { ...DDAG, nodes: [...DDAG.nodes, dnode('f', 5, { reads: ['x'], writes: ['x'], mutates: ['x'] })] }
  const note = planCellDelete([cells[0]], badges(cells), { allCells: cells, dag }).dependentsNote
  assert.deepEqual(note.items[0].usedBy, ['PY-2', 'PY-4', 'PY-6'])
})

test('dependents: a reader that is also being deleted does not count', () => {
  const cells = [code('a', 'x = 1'), code('b', 'x + 1')]
  const dag = { schema: 1, edges: [], issues: [], nodes: [dnode('a', 0, { writes: ['x'] }), dnode('b', 1, { reads: ['x'] })] }
  assert.equal(planCellDelete(cells, badges(cells), { allCells: cells, dag }).dependentsNote, null)
})

test('dependents: a 300-char name is kept whole', () => {
  const long = 'n'.repeat(300)
  const cells = [code('a', `${long} = 1`), code('b', long)]
  const dag = { schema: 1, edges: [], issues: [], nodes: [dnode('a', 0, { writes: [long] }), dnode('b', 1, { reads: [long] })] }
  const note = planCellDelete([cells[0]], badges(cells), { allCells: cells, dag }).dependentsNote
  assert.equal(note.items[0].name, long)
  assert.ok(note.text.includes(long))
})

test('dependents: no dag / no allCells → null', () => {
  assert.equal(planCellDelete([DCELLS[0]], badges(DCELLS), { allCells: DCELLS }).dependentsNote, null)
  assert.equal(planCellDelete([DCELLS[0]], badges(DCELLS), { dag: DDAG }).dependentsNote, null)
})
