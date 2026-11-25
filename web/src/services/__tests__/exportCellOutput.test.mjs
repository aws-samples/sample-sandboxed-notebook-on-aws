// Unit tests for the single-cell output export / copy (services/exportCellOutput.js):
// legacy cells stay byte-identical to the pre-change output (golden captured from the
// previous implementation), multi-output cells export EVERY item in order, expanded and static.
// Run with: node --test web/src/services/__tests__/exportCellOutput.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildCellOutputCopyText, buildCellOutputHtml, buildCellOutputMarkdown } from '../exportCellOutput.js'
import { buildOutputsEnvelope } from '../cellOutputs.js'
import { multiOutputCell, multiOutputErrorCell } from './fixtures/exportFixtures.mjs'

const golden = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'legacyCellOutput.json'), 'utf8'))
const NOW = { toLocaleString: () => 'FIXED-LOCALE', getFullYear: () => 2026 }
const DF = JSON.stringify({ __df_table__: true, columns: ['region', 'orders'], cells: [['us-east-1', '120'], ['eu-west-1', '80']], total_rows: 2, shown_rows: 2, total_cols: 2, shown_cols: 2 })
const PLY = '<div class="plotly-chart" data-plotly="true"><div id="f"></div></div>'
const PNG = 'data:image/png;base64,iVBORw0KGgo='
const LEGACY = {
  table: { id: 'c1', code: 'df', output: 'printed\n', html: DF },
  plotly: { id: 'c2', code: 'fig', output: '', html: PLY },
  image: { id: 'c3', code: 'plt.plot()', output: 'x', image: PNG },
  tableImage: { id: 'c4', code: 'df; plt', output: 'o', html: DF, image: PNG },
  text: { id: 'c5', code: 'print(1)', output: 'hi' },
  empty: { id: 'c6', code: '' },
}

test('legacy cells: HTML / Markdown / Copy are byte-identical to the previous implementation', () => {
  for (const [k, c] of Object.entries(LEGACY)) {
    assert.equal(buildCellOutputHtml(c, 'My NB', 'PY-1', NOW), golden[`html:${k}`], `html ${k}`)
    assert.equal(buildCellOutputMarkdown(c, 'My NB', 'PY-1', NOW), golden[`md:${k}`], `md ${k}`)
    assert.equal(buildCellOutputCopyText(c) || null, golden[`copy:${k}`] || null, `copy ${k}`)
  }
})

const pos = (hay, needle, from = 0) => {
  const i = hay.indexOf(needle, from)
  assert.ok(i >= 0, `missing ${needle}`)
  return i
}

test('multi-output HTML: every item in order, expanded, static, each rich one under a caption', () => {
  const h = buildCellOutputHtml(multiOutputCell(), 'My NB', 'PY-8', NOW)
  assert.ok(!/<(button|details\s+open|input|select)\b/i.test(h.replace('<details><summary>Code</summary>', '')))
  let at = 0
  for (const needle of ['loading orders…', 'Table 1 · direct — 2 rows × 2 cols', 'and the upstream view',
    'Table 2 · upstream — 12 rows × 1 col', 'Table 3 — 3 rows × 3 cols', 'Chart 1 · fig1 · Plotly', 'Chart 2 · Plotly',
    'Image 1', 'Image 2', 'Image 3', 'done']) {
    at = pos(h, needle, at) + needle.length
  }
  assert.equal((h.match(/<table class="df-table">/g) || []).length, 3)
  assert.equal((h.match(/<img /g) || []).length, 3)
  assert.equal((h.match(/class="plotly-chart"/g) || []).length, 2)
  assert.ok(!h.includes('__outputs__'), 'no raw envelope')
  assert.equal(h.split('loading orders…').length - 1, 2, 'text appears once in the code and once as output (not from cell.output too)')
})

test('multi-output HTML escapes text and captions; an image without src says so', () => {
  const html = buildOutputsEnvelope([{ t: 'text', text: '<b>x</b>' }, { t: 'table', label: '<i>t</i>', payload: JSON.parse(DF) }, { t: 'image', key: 'notebook-outputs/1/c/' + 'a'.repeat(64) + '.png' }, { t: 'text', text: 'z' }])
  const h = buildCellOutputHtml({ id: 'c', code: '', html }, 'NB', 'PY-1', NOW)
  assert.ok(h.includes('&lt;b&gt;x&lt;/b&gt;'))
  assert.ok(h.includes('&lt;i&gt;t&lt;/i&gt;'))
  assert.ok(h.includes('(image unavailable)'))
})

test('multi-output Markdown: every item in order with captions', () => {
  const md = buildCellOutputMarkdown(multiOutputCell(), 'My NB', 'PY-8', NOW)
  let at = 0
  for (const needle of ['loading orders…', '*Table 1 · direct — 2 rows × 2 cols*', '| region | orders |', 'and the upstream view',
    '*Table 2 · upstream', '*Table 3 —', '*Chart 1 · fig1 · Plotly*', '*Chart 2 · Plotly*', '![Image 1](plot-1.png)', '![Image 3](plot-3.png)', 'done']) {
    at = pos(md, needle, at) + needle.length
  }
  assert.ok(!md.includes('data:image'))
})

test('Copy (multi): text, every table as a captioned Markdown table, charts/images as notes; no base64, no HTML', () => {
  const t = buildCellOutputCopyText(multiOutputCell())
  let at = 0
  for (const needle of ['loading orders…', 'Table 1 · direct — 2 rows × 2 cols', '| region | orders |', 'and the upstream view',
    'Table 2 · upstream', 'Table 3 —', '(Chart 1 · fig1 · Plotly', '(Chart 2 · Plotly', '(Image 1', '(Image 3', 'done']) {
    at = pos(t, needle, at) + needle.length
  }
  assert.ok(!t.includes('data:image') && !t.includes('plotly-chart'))
})

test('error after displays: the items are exported, the cell error stays out of the output builders', () => {
  const c = multiOutputErrorCell()
  const h = buildCellOutputHtml(c, 'NB', 'PY-9', NOW)
  assert.equal((h.match(/<table class="df-table">/g) || []).length, 2)
  assert.ok(buildCellOutputCopyText(c).includes('Table 2 · upstream'))
})
