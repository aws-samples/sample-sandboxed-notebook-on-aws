// Multi-output cells across the notebook exporters / importer / lineage drawer / App view /
// persistence helpers (services/cellOutputs.js is the adapter they all use).
// Run with: node --test web/src/services/__tests__/multiOutputExports.test.mjs
// Snapshots (new names only): __snapshots__/notebook-multi-<theme>.html, notebook-multi.md,
// app-multi.html (UPDATE_SNAPSHOTS=1).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildNotebookHTML, buildNotebookMarkdown, buildIPYNB, buildNativeNotebook, buildAppHTML, buildAppMarkdown,
} from '../notebookExport.js'
import { parseNotebookFile, normalizeImportedCell } from '../notebookImport.js'
import { buildExplainPayload, buildLineage } from '../lineage.js'
import { appShows, deriveDisplayVar } from '../appViewModel.js'
import { cellOutputItems, parseOutputsEnvelope, stripEnvelopeImages, buildOutputsEnvelope } from '../cellOutputs.js'
import { resolveExportImages, cellImagePath } from '../cellImage.js'
import {
  fixtureTab, fixtureCellsMulti, multiOutputCell, stubMermaid, identitySanitize, FIXED_NOW, fmtTime,
} from './fixtures/exportFixtures.mjs'
import { matchSnapshot } from './fixtures/snapshot.mjs'

const opts = (theme) => ({ theme, sanitize: identitySanitize, renderMermaid: stubMermaid, now: FIXED_NOW, formatTime: fmtTime })
const html = (theme) => buildNotebookHTML(fixtureTab(), fixtureCellsMulti(), opts(theme)).then(r => r.content)
const order = (hay, needles) => {
  let at = 0
  for (const n of needles) {
    const i = hay.indexOf(n, at)
    assert.ok(i >= 0, `missing or out of order: ${n}`)
    at = i + n.length
  }
}
const CAPTIONS = ['Table 1 · direct — 2 rows × 2 cols', 'Table 2 · upstream — 12 rows × 1 col', 'Table 3 — 3 rows × 3 cols',
  'Chart 1 · fig1 · Plotly', 'Chart 2 · Plotly', 'Image 1', 'Image 2', 'Image 3']

test('notebook HTML: all items in order, expanded and static (no buttons, no collapsing), captions as plain text', async () => {
  for (const theme of ['light', 'dark', 'ember']) {
    const h = await html(theme)
    assert.ok(!/<(button|select|input|textarea|details)\b/i.test(h), theme)
    assert.ok(!h.includes('aria-expanded') && !h.includes('__outputs__'), theme)
    order(h, ['loading orders…', CAPTIONS[0], 'and the upstream view', CAPTIONS[1], CAPTIONS[2], CAPTIONS[3], CAPTIONS[4], CAPTIONS[5], CAPTIONS[6], CAPTIONS[7], 'done'])
    assert.equal((h.match(/<table class="df-table">/g) || []).length, 3 + 2, `${theme}: 3 tables + 2 from the error cell`)
    assert.equal((h.match(/<img /g) || []).length, 3)
    assert.equal((h.match(/class="x-plotly"/g) || []).length, 2)
    assert.ok(h.includes('x-plotly-offline'), 'offline note + script present')
  }
})

test('notebook HTML: the error cell keeps its displayed tables above the error', async () => {
  const h = await html('light')
  order(h, ['Table 2 · upstream', 'ValueError: boom'])
})

test('notebook Markdown: all items in order with captions', () => {
  const md = buildNotebookMarkdown(fixtureTab(), fixtureCellsMulti(), { now: FIXED_NOW, formatTime: fmtTime }).content
  order(md, ['loading orders…', `*${CAPTIONS[0]}*`, '| region | orders |', 'and the upstream view', `*${CAPTIONS[1]}*`, `*${CAPTIONS[2]}*`,
    `*${CAPTIONS[3]}*`, `*${CAPTIONS[4]}*`, '![Image 1](plot-PY-1-1.png)', '![Image 3](plot-PY-1-3.png)', 'done'])
  assert.ok(!md.includes('data:image') && !md.includes('__outputs__'))
})

test('snapshots: multi-output notebook (light, dark, markdown, App HTML)', async () => {
  matchSnapshot('notebook-multi-light.html', await html('light'))
  matchSnapshot('notebook-multi-dark.html', await html('dark'))
  matchSnapshot('notebook-multi.md', buildNotebookMarkdown(fixtureTab(), fixtureCellsMulti(), { now: FIXED_NOW, formatTime: fmtTime }).content)
  const app = await buildAppHTML(fixtureTab(), fixtureCellsMulti(), { sanitize: identitySanitize, renderMermaid: stubMermaid, now: FIXED_NOW })
  matchSnapshot('app-multi.html', app.content)
})

test('App HTML / Markdown: every item, static; no raw envelope; no "(table unavailable)"', async () => {
  const app = await buildAppHTML(fixtureTab(), fixtureCellsMulti(), { sanitize: identitySanitize, renderMermaid: stubMermaid, now: FIXED_NOW })
  assert.ok(!/<(button|input|select)\b/i.test(app.content))
  assert.ok(!app.content.includes('table unavailable') && !app.content.includes('__outputs__'))
  order(app.content, [CAPTIONS[0], CAPTIONS[1], CAPTIONS[2], CAPTIONS[3], CAPTIONS[4], CAPTIONS[5], CAPTIONS[7]])
  const md = buildAppMarkdown(fixtureTab(), fixtureCellsMulti()).content
  order(md, ['loading orders…', CAPTIONS[0], CAPTIONS[1], CAPTIONS[2], CAPTIONS[3], CAPTIONS[5]])
  assert.ok(!md.includes('data:image'))
})

test('a corrupt envelope is a placeholder, never raw JSON, in the notebook and App HTML exports', async () => {
  const bad = { id: 'x', type: 'code', code: 'x', html: '{"__outputs__":1,"items":[{"t":"plotly","html":"data-plotly' }
  const h = await buildNotebookHTML(fixtureTab(), [bad], opts('light')).then(r => r.content)
  assert.ok(h.includes('(table unavailable)') && !h.includes('__outputs__'))
  const a = await buildAppHTML(fixtureTab(), [bad], { sanitize: identitySanitize, now: FIXED_NOW }).then(r => r.content)
  assert.ok(a.includes('(table unavailable)') && !a.includes('__outputs__'))
})

test('.ipynb: one output per item in order (stream, display_data text/html, image/png)', () => {
  const nb = JSON.parse(buildIPYNB(fixtureTab(), fixtureCellsMulti()).content)
  const cell = nb.cells.find(c => c.metadata.microvm_notebook.id === 'cell-py-multi')
  const kinds = cell.outputs.map((o) => {
    if (o.output_type === 'stream') return `stream:${o.text.join('')}`
    const d = o.data
    return d['image/png'] ? 'png' : d['text/plain'][0]
  })
  assert.deepEqual(kinds, ['stream:loading orders…\n', '<DataFrame>', 'stream:and the upstream view\n', '<DataFrame>', '<DataFrame>',
    '<Plotly figure>', '<Plotly figure>', 'png', 'png', 'png', 'stream:done\n'])
  assert.deepEqual(cell.outputs.filter(o => o.data && o.data['image/png']).map(o => o.metadata.microvm_notebook.image), [1, 2, 3])
  // The stored extras carry the envelope with KEYS only (the pngs travel as outputs).
  assert.ok(!cell.metadata.microvm_notebook.html.includes('data:image'))
  assert.ok(cell.metadata.microvm_notebook.html.startsWith('{"__outputs__":1'))
  const err = nb.cells.find(c => c.metadata.microvm_notebook.id === 'cell-py-multi-err')
  assert.deepEqual(err.outputs.map(o => o.output_type), ['stream', 'display_data', 'stream', 'display_data', 'error'])
})

test('.ipynb export then import restores the same items and re-fills every image in order', () => {
  const cells = fixtureCellsMulti()
  const out = buildIPYNB(fixtureTab(), cells)
  const back = parseNotebookFile(JSON.parse(out.content), out.filename)
  const c = normalizeImportedCell(back.cells[1], () => 'nid')
  assert.equal(c.image, null)
  const want = cellOutputItems(cells[1])
  const got = cellOutputItems(c)
  assert.deepEqual(got.map(i => i.t), want.map(i => i.t))
  assert.deepEqual(got.filter(i => i.t === 'image').map(i => i.src), want.filter(i => i.t === 'image').map(i => i.src))
  assert.deepEqual(got.filter(i => i.t === 'table').map(i => i.payload), want.filter(i => i.t === 'table').map(i => i.payload))
  assert.deepEqual(got.filter(i => i.t === 'plotly').map(i => i.html), want.filter(i => i.t === 'plotly').map(i => i.html))
})

test('.notebook.json round trip preserves the envelope string exactly', () => {
  const cells = fixtureCellsMulti()
  const out = buildNativeNotebook(fixtureTab(), cells, { now: FIXED_NOW })
  const back = parseNotebookFile(JSON.parse(out.content), out.filename)
  assert.equal(back.cells[1].html, cells[1].html)
  assert.equal(back.cells[2].html, cells[2].html)
  assert.equal(normalizeImportedCell(back.cells[1], () => 'n').html, cells[1].html)
})

test('lineage drawer: first table of an envelope; kind follows the first rich item', () => {
  const cell = { ...multiOutputCell(), id: 'm1', lastExecutedCode: 'x' }
  const lin = { cellId: 'm1', steps: [], sources: [] }
  const p = buildExplainPayload(lin, [cell])
  assert.equal(p.output.kind, 'table')
  assert.deepEqual(p.output.columns, ['region', 'orders'])
  assert.equal(p.output.rows, 2)
  assert.equal(p.output.cols, 2)
  assert.ok(!JSON.stringify(p).includes('us-east-1'), 'shape only, never values')
  const chartFirst = { id: 'm2', html: buildOutputsEnvelope([{ t: 'plotly', html: '<div data-plotly="true"></div>' }, ...parseOutputsEnvelope(cell.html).filter(i => i.t === 'table')]) }
  assert.equal(buildExplainPayload({ cellId: 'm2', steps: [], sources: [] }, [chartFirst]).output.kind, 'chart')
  assert.equal(typeof buildLineage, 'function')
})

test('App view model: an envelope shows; the caption is the first table label, never the raw string', () => {
  const cell = multiOutputCell()
  assert.equal(appShows(cell), true)
  assert.equal(deriveDisplayVar(cell), 'direct')
  const noLabel = { ...cell, html: buildOutputsEnvelope([{ t: 'table', payload: parseOutputsEnvelope(cell.html)[1].payload }, { t: 'plotly', html: '<div data-plotly="true"></div>' }]), code: 'summary' }
  assert.equal(deriveDisplayVar(noLabel), 'summary')
  const plotlyFirst = { ...noLabel, html: buildOutputsEnvelope([{ t: 'plotly', html: '<div data-plotly="true"></div>' }, { t: 'table', payload: parseOutputsEnvelope(cell.html)[1].payload }]) }
  assert.equal(deriveDisplayVar(plotlyFirst), null)
  // the substring check never runs on the raw envelope: a table-only envelope is not "a chart"
  const tablesOnly = { ...cell, html: buildOutputsEnvelope(parseOutputsEnvelope(cell.html).filter(i => i.t === 'table').map(i => ({ ...i, label: undefined }))), code: 'both' }
  assert.equal(deriveDisplayVar(tablesOnly), 'both')
})

test('no base64 reaches a persisted form: stripEnvelopeImages / the stored cell fields', () => {
  const cell = multiOutputCell()
  assert.ok(cell.html.includes('data:image'))
  const stored = stripEnvelopeImages(cell.html)
  assert.ok(!stored.includes('data:image'))
  assert.equal(stored, multiOutputCell({ withSrc: false }).html)
  assert.equal(cellOutputItems({ ...cell, html: stored }).filter(i => i.t === 'image').every(i => i.key && !i.src), true)
})

test('export image resolution fills each keyed image from the route, in order, and survives a failed fetch', async () => {
  const stored = { ...multiOutputCell({ withSrc: false }), id: 'cell-py-multi' }
  const calls = []
  const load = async (cellId, key, opts) => { calls.push([cellId, key, opts]); return key.includes('b'.repeat(64)) ? null : `data:image/png;base64,${key.slice(-10, -4)}` }
  const [out] = await resolveExportImages([stored], load)
  assert.equal(calls.length, 3)
  assert.ok(calls.every(c => c[2] && c[2].any === true), 'multi-output images are fetched by ?key=')
  const imgs = cellOutputItems(out).filter(i => i.t === 'image')
  assert.equal(imgs[0].src.startsWith('data:image/png;base64,'), true)
  assert.equal(imgs[1].src, undefined, 'a failed fetch leaves that image without src')
  assert.equal(imgs[2].src.startsWith('data:image/png;base64,'), true)
  assert.equal(cellImagePath('c1', stored && parseOutputsEnvelope(stored.html)[7].key, { any: true }).includes('?key='), true)
})
