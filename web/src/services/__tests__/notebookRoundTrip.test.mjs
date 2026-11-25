// Round-trip tests: import(export(nb)) == nb for every persisted field, for the
// native .notebook.json and our .ipynb; foreign .ipynb import; no secrets exported.
// Run with: node --test web/src/services/__tests__/notebookRoundTrip.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildNativeNotebook, buildIPYNB, persistedCell, PERSISTED_CELL_FIELDS } from '../notebookExport.js'
import { parseNotebookFile, normalizeImportedCell } from '../notebookImport.js'
import { fixtureTab, fixtureCells, POISON, FIXED_NOW } from './fixtures/exportFixtures.mjs'

// proxy/storage/sqlite_db.py `_row_to_cell` keys (keep in sync; postgres_db mirrors it).
const ROW_TO_CELL_KEYS = [
  'id', 'type', 'code', 'output', 'error', 'html', 'image', 'imageS3Key', 'outputVariable',
  'executionNumber', 'executionTime', 'lastExecutedCode', 'aiExplanation', 'aiExplanationEdited',
  'aiExplanationSource', 'generated', 'peakMemMb', 'peakCpuPct', 'executedAt',
]

const imported = (c) => {
  const n = normalizeImportedCell(c, () => 'new-id')
  assert.equal(n.imageS3Key, null)
  assert.equal(n.status, 'idle')
  delete n.imageS3Key
  delete n.status
  return n
}

test('PERSISTED_CELL_FIELDS = the stored cell fields minus imageS3Key', () => {
  assert.deepEqual([...PERSISTED_CELL_FIELDS].sort(), ROW_TO_CELL_KEYS.filter(k => k !== 'imageS3Key').sort())
  assert.deepEqual(Object.keys(persistedCell({})).sort(), [...PERSISTED_CELL_FIELDS].sort())
})

test('.notebook.json round-trips every persisted field and the notebook metadata', () => {
  const tab = fixtureTab()
  const cells = fixtureCells()
  const out = buildNativeNotebook(tab, cells, { now: FIXED_NOW })
  assert.equal(out.filename, 'Export_Fixture.notebook.json')
  const data = JSON.parse(out.content)
  assert.equal(data.formatVersion, 2)
  assert.deepEqual(data.dag, tab._dag)
  const back = parseNotebookFile(data, out.filename)
  assert.equal(back.name, tab.name)
  assert.equal(back.description, tab.description)
  assert.equal(back.tag, tab.tag)
  assert.equal(back.cells.length, cells.length)
  back.cells.forEach((c, i) => assert.deepEqual(imported(c), persistedCell(cells[i]), `cell ${i}`))
  // An exported image is kept (as a data URL); its S3 key never is.
  assert.ok(back.cells[4].image.startsWith('data:image/png;base64,'))
  assert.ok(!out.content.includes('notebook-outputs/'))
})

test('.ipynb round-trips every persisted field through metadata.microvm_notebook', () => {
  const tab = fixtureTab()
  const cells = fixtureCells()
  const out = buildIPYNB(tab, cells)
  const nb = JSON.parse(out.content)
  assert.equal(nb.nbformat, 4)
  assert.equal(nb.nbformat_minor, 5)
  // SQL is a code cell with a %%sql marker; ids are nbformat-safe and unique.
  const sql = nb.cells[2]
  assert.equal(sql.cell_type, 'code')
  assert.equal(sql.source[0], '%%sql\n')
  assert.equal(sql.execution_count, 4)
  const ids = nb.cells.map(c => c.id)
  assert.equal(new Set(ids).size, ids.length)
  for (const id of ids) assert.match(id, /^[a-zA-Z0-9-_]{1,64}$/)
  assert.equal(nb.cells[6].metadata.microvm_notebook.id, 'legacy id.8')
  // standard outputs
  const err = nb.cells[5].outputs.find(o => o.output_type === 'error')
  assert.equal(err.ename, 'ValueError')
  assert.equal(err.evalue, 'bad <input> value')
  assert.ok(nb.cells[4].outputs.some(o => o.output_type === 'display_data' && o.data['image/png']))
  assert.ok(nb.cells[2].outputs.some(o => o.output_type === 'display_data' && o.data['text/html'].join('').includes('<table')))
  const back = parseNotebookFile(nb, out.filename)
  assert.equal(back.name, tab.name)
  assert.equal(back.description, tab.description)
  assert.equal(back.tag, tab.tag)
  back.cells.forEach((c, i) => assert.deepEqual(imported(c), persistedCell(cells[i]), `cell ${i}`))
})

test('a foreign .ipynb (no extras) imports code, %%sql, text, errors and images', () => {
  const nb = {
    nbformat: 4, nbformat_minor: 4, metadata: {},
    cells: [
      { cell_type: 'markdown', metadata: {}, source: ['# T\n', 'text'] },
      { cell_type: 'code', metadata: {}, execution_count: 2, source: ['print(1)'], outputs: [{ output_type: 'stream', name: 'stdout', text: ['1\n'] }] },
      { cell_type: 'code', metadata: {}, execution_count: null, source: '%%sql\nSELECT 1', outputs: [{ output_type: 'execute_result', data: { 'text/plain': ['1'], 'text/html': ['<table></table>'] }, metadata: {}, execution_count: 3 }] },
      { cell_type: 'code', metadata: {}, execution_count: 4, source: ['1/0'], outputs: [{ output_type: 'error', ename: 'ZeroDivisionError', evalue: 'division by zero', traceback: ['Traceback', 'ZeroDivisionError: division by zero'] }] },
      { cell_type: 'code', metadata: {}, execution_count: 5, source: ['plot()'], outputs: [{ output_type: 'display_data', data: { 'image/png': 'iVBOR\n', 'text/plain': ['<Figure>'] }, metadata: {} }] },
      { cell_type: 'raw', metadata: {}, source: ['ignored'] },
    ],
  }
  const back = parseNotebookFile(nb, 'Foreign.ipynb')
  assert.equal(back.name, 'Foreign')
  assert.equal(back.description, 'Imported from Jupyter: Foreign.ipynb')
  assert.equal(back.cells.length, 5)
  const [md, py, sql, err, img] = back.cells.map(c => normalizeImportedCell(c, () => 'nid'))
  assert.equal(md.type, 'markdown')
  assert.equal(md.code, '# T\ntext')
  assert.equal(py.output, '1\n')
  assert.equal(py.executionNumber, 2)
  assert.equal(py.id, 'nid')
  assert.equal(sql.type, 'sql')
  assert.equal(sql.code, 'SELECT 1')
  assert.equal(sql.output, '1')
  assert.equal(sql.html, null)          // foreign HTML is not imported as html
  assert.equal(err.error, 'Traceback\nZeroDivisionError: division by zero')
  assert.equal(img.image, 'data:image/png;base64,iVBOR')
})

test('invalid files throw', () => {
  assert.throws(() => parseNotebookFile({ foo: 1 }, 'x.json'))
  assert.throws(() => parseNotebookFile(null, 'x.json'))
})

test('no secret, env value or transient state in the .notebook.json / .ipynb', () => {
  const tab = fixtureTab()
  const cells = fixtureCells()
  for (const out of [buildNativeNotebook(tab, cells, { now: FIXED_NOW }), buildIPYNB(tab, cells)]) {
    assert.ok(!out.content.includes(POISON), out.filename)
    for (const k of ['errorLocals', 'lintDiagnostics', '"status"', 'sessionId', 'secrets', 'env_vars']) {
      assert.ok(!out.content.includes(k), `${out.filename}: ${k}`)
    }
    // env refs stay as code
    assert.ok(out.content.includes('os.environ[\\"API_KEY\\"]'))
  }
})
