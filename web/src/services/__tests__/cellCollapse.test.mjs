// node --test web/src/services/__tests__/cellCollapse.test.mjs
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  isCellCollapsed, setCellCollapsed, _resetCellCollapse,
  cellLineCount, lineCountLabel, collapsedPreview, collapsedStatusHint,
} from '../cellCollapse.js'
import { buildOutputsEnvelope } from '../cellOutputs.js'

beforeEach(() => _resetCellCollapse())

const table = (rows, cols) => ({
  __df_table__: true, columns: Array.from({ length: cols }, (_, i) => `c${i}`), cells: [],
  dtypes: [], total_rows: rows, shown_rows: 0, total_cols: cols, shown_cols: cols,
})

test('collapsed state is remembered per cell id (string/number agnostic) and defaults to expanded', () => {
  assert.equal(isCellCollapsed('a'), false)
  setCellCollapsed('a', true)
  setCellCollapsed(7, true)
  assert.equal(isCellCollapsed('a'), true)
  assert.equal(isCellCollapsed('7'), true)
  assert.equal(isCellCollapsed('b'), false)
  setCellCollapsed('a', false)
  assert.equal(isCellCollapsed('a'), false)
  assert.equal(isCellCollapsed(null), false)
  setCellCollapsed(null, true)   // ignored
  assert.equal(isCellCollapsed(null), false)
})

test('line count and pill label', () => {
  assert.equal(cellLineCount(''), 0)
  assert.equal(cellLineCount('   \n '), 0)
  assert.equal(cellLineCount('x = 1'), 1)
  assert.equal(cellLineCount('a\nb\nc\n'), 3)   // a trailing newline is not a line
  assert.equal(lineCountLabel(''), 'Empty')
  assert.equal(lineCountLabel('x'), '1 line')
  assert.equal(lineCountLabel('a\nb'), '2 lines')
})

test('preview is the first non-blank line, not truncated; markdown loses heading/inline markup', () => {
  const long = '-- Athena: revenue, units, price, margin by region and quarter, ' + 'x'.repeat(200)
  assert.equal(collapsedPreview({ type: 'sql', code: `\n\n${long}\nSELECT 1` }), long)
  assert.equal(collapsedPreview({ type: 'code', code: '    x = 1   \ny' }), '    x = 1')
  assert.equal(collapsedPreview({ type: 'markdown', code: '# Revenue **analysis** of `sales`\n\ntext' }), 'Revenue analysis of sales')
  assert.equal(collapsedPreview({ type: 'markdown', code: '- see [the docs](http://x)' }), 'see the docs')
  assert.equal(collapsedPreview({ type: 'code', code: '' }), '')
})

test('status hint: running and error win over output; markdown has none', () => {
  assert.deepEqual(collapsedStatusHint({ type: 'code', status: 'running', output: 'old' }), { tone: 'running', text: 'Running…' })
  assert.deepEqual(
    collapsedStatusHint({ type: 'code', status: 'error', error: "\nNameError: name 'x' is not defined\nmore", output: 'partial' }),
    { tone: 'error', text: "NameError: name 'x' is not defined" })
  assert.deepEqual(collapsedStatusHint({ type: 'code', status: 'interrupted' }), { tone: 'warning', text: 'Interrupted' })
  assert.equal(collapsedStatusHint({ type: 'markdown', code: '# x', status: 'error', error: 'x' }), null)
  assert.equal(collapsedStatusHint({ type: 'code', status: 'idle' }), null)
  assert.equal(collapsedStatusHint(null), null)
})

test('status hint: output summaries for table / chart / image / text / multi-output', () => {
  assert.deepEqual(collapsedStatusHint({ type: 'sql', status: 'success', html: JSON.stringify(table(100, 8)) }),
    { tone: 'neutral', text: '100 rows × 8 columns' })
  assert.deepEqual(collapsedStatusHint({ type: 'sql', status: 'success', html: JSON.stringify(table(1, 1)) }),
    { tone: 'neutral', text: '1 row × 1 column' })
  assert.deepEqual(collapsedStatusHint({ type: 'sql', status: 'success', html: JSON.stringify(table(12345, 3)) }),
    { tone: 'neutral', text: '12,345 rows × 3 columns' })
  assert.deepEqual(collapsedStatusHint({ type: 'code', status: 'success', html: '<div data-plotly="true"></div>' }),
    { tone: 'neutral', text: 'Chart' })
  assert.deepEqual(collapsedStatusHint({ type: 'code', status: 'success', imageS3Key: 'k.png', output: 'saved' }),
    { tone: 'neutral', text: 'Image' })
  assert.deepEqual(collapsedStatusHint({ type: 'code', status: 'success', output: '\n  step 0\nstep 1' }),
    { tone: 'neutral', text: 'step 0' })
  const env = buildOutputsEnvelope([
    { t: 'table', payload: table(2, 2) }, { t: 'text', text: 'between' },
    { t: 'table', payload: table(3, 3) }, { t: 'plotly', html: '<div data-plotly="true"></div>' },
  ])
  assert.deepEqual(collapsedStatusHint({ type: 'code', status: 'success', html: env }),
    { tone: 'neutral', text: '2 tables · 1 chart' })
})
