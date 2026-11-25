// Unit tests for services/cellOutputs.js — the single adapter for multi-output
// cells (envelope parse/build, the build rule for a run result, legacy items,
// numbering/captions, base64 stripping). Run with:
//   node --test web/src/services/__tests__/cellOutputs.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildOutputsEnvelope, cellOutputItems, fillEnvelopeImages, firstRichKind, firstTable,
  hasRichOutput, isEnvelope, isMultiOutput, normalizeItem, outputCaption,
  outputFieldsFromResult, parseOutputsEnvelope, richCounts, stripEnvelopeImages,
} from '../cellOutputs.js'

const tbl = (cols = ['a'], rows = 2, extra = {}) => ({
  __df_table__: true, columns: cols, dtypes: cols.map(() => 'int64'),
  cells: Array.from({ length: Math.min(rows, 2) }, (_, i) => cols.map(() => String(i))),
  total_rows: rows, shown_rows: Math.min(rows, 2), total_cols: cols.length, shown_cols: cols.length, ...extra,
})
const SHA = (c) => c.repeat(64)
const key = (c) => `notebook-outputs/7/c1/${SHA(c)}.png`
const PNG = 'data:image/png;base64,iVBORw0KGgo='
const PLY = '<div class="plotly-chart" data-plotly="true"><div>fig</div></div>'

test('isEnvelope: prefix only', () => {
  assert.equal(isEnvelope('{"__outputs__":1,"items":[]}'), true)
  for (const v of [null, undefined, '', '{"__df_table__":true}', PLY, 'x{"__outputs__"']) assert.equal(isEnvelope(v), false)
})

test('build → parse round trip; compact; __outputs__ first; unknown fields and invalid items dropped', () => {
  const items = [
    { t: 'text', text: 'a\n', extra: 1 },
    { t: 'table', payload: tbl(['x', 'y'], 12), label: 'upstream', junk: true },
    { t: 'plotly', html: PLY, label: 'fig2' },
    { t: 'image', src: PNG, key: key('a') },
    { t: 'image' }, { t: 'bogus' }, null, 'x', { t: 'table', payload: { nope: 1 } }, { t: 'text', text: 5 },
  ]
  const s = buildOutputsEnvelope(items)
  assert.ok(s.startsWith('{"__outputs__":1,"items":['))
  assert.ok(!s.includes('\n') && !s.includes(': '))
  const back = parseOutputsEnvelope(s)
  assert.equal(back.length, 4)
  assert.deepEqual(back[0], { t: 'text', text: 'a\n' })
  assert.deepEqual(back[1], { t: 'table', payload: tbl(['x', 'y'], 12), label: 'upstream' })
  assert.deepEqual(back[2], { t: 'plotly', html: PLY, label: 'fig2' })
  assert.deepEqual(back[3], { t: 'image', src: PNG, key: key('a') })
  assert.equal(buildOutputsEnvelope(back), s, 'stable')
})

test('a table payload given as the legacy JSON string is stored as the object', () => {
  const it = normalizeItem({ t: 'table', payload: JSON.stringify(tbl()) })
  assert.deepEqual(it.payload, tbl())
  assert.equal(normalizeItem({ t: 'table', payload: '{bad' }), null)
})

test('parse: non-envelope / malformed / wrong version / wrong shape → null, never throws', () => {
  for (const v of [null, undefined, 5, '', '<div>', JSON.stringify(tbl()), '{"__outputs__":1,"items":', '{"__outputs__":2,"items":[]}',
    '{"__outputs__":1,"items":{}}', '{"__outputs__":1}']) {
    assert.equal(parseOutputsEnvelope(v), null, String(v))
  }
  assert.deepEqual(parseOutputsEnvelope('{"__outputs__":1,"items":[]}'), [])
})

const run = (outputs, extra = {}) => ({ success: true, output: 'o', html: '', image: '', image_s3_key: null, outputs, ...extra })
const T = (label) => ({ t: 'table', payload: tbl(['a'], 3), ...(label ? { label } : {}) })
const P = () => ({ t: 'plotly', html: PLY })
const I = (c, withKey = true) => ({ t: 'image', src: PNG, ...(withKey ? { key: key(c) } : {}) })
const X = (text) => ({ t: 'text', text })

test('build rule: old VM (no outputs), outputs [], text only → today\'s exact legacy mapping', () => {
  const legacy = { html: '{"__df_table__":true}', image: PNG, image_s3_key: key('a') }
  for (const outputs of [undefined, [], [X('hi')], 'junk']) {
    const f = outputFieldsFromResult({ ...legacy, outputs })
    assert.deepEqual(f, { html: legacy.html, image: PNG, imageS3Key: key('a') }, JSON.stringify(outputs))
  }
  assert.deepEqual(outputFieldsFromResult({}), { html: null, image: null, imageS3Key: null })
  assert.deepEqual(outputFieldsFromResult(null), { html: null, image: null, imageS3Key: null })
  // SQL result: html only, no outputs key
  assert.deepEqual(outputFieldsFromResult({ html: '{"__df_table__":true}', output: 'Query OK' }),
    { html: '{"__df_table__":true}', image: null, imageS3Key: null })
})

test('build rule: ONE rich item (text before it is fine) → legacy fields, no envelope', () => {
  const html = JSON.stringify(tbl(['a'], 3))
  let f = outputFieldsFromResult(run([X('before\n'), T()], { html }))
  assert.deepEqual(f, { html, image: null, imageS3Key: null })
  f = outputFieldsFromResult(run([T('df')], { html }))
  assert.equal(f.html, html)
  f = outputFieldsFromResult(run([P()], { html: PLY }))
  assert.deepEqual(f, { html: PLY, image: null, imageS3Key: null })
  f = outputFieldsFromResult(run([X('t\n'), I('a')], { image: PNG, image_s3_key: key('a') }))
  assert.deepEqual(f, { html: null, image: PNG, imageS3Key: key('a') })
})

test('build rule: error run with one displayed table keeps the legacy fields (html from the item)', () => {
  const f = outputFieldsFromResult(run([T()], { success: false, html: '' }))
  assert.equal(f.image, null)
  assert.deepEqual(JSON.parse(f.html), tbl(['a'], 3))
  const g = outputFieldsFromResult(run([I('b')], { success: false, image: '' }))
  assert.deepEqual(g, { html: null, image: PNG, imageS3Key: key('b') })
})

test('build rule: 2+ rich items or text AFTER a rich item → envelope; image null; imageS3Key = LAST image key', () => {
  let f = outputFieldsFromResult(run([T(), T('b'), I('a'), X('tail'), I('c')], { image: PNG, image_s3_key: key('c') }))
  assert.ok(isEnvelope(f.html))
  assert.equal(f.image, null)
  assert.equal(f.imageS3Key, key('c'))
  assert.equal(parseOutputsEnvelope(f.html).length, 5)
  f = outputFieldsFromResult(run([T(), X('after\n')], { html: JSON.stringify(tbl()) }))
  assert.ok(isEnvelope(f.html), 'text after one rich item')
  assert.equal(f.imageS3Key, null)
  f = outputFieldsFromResult(run([I('a'), I('b')], { image: PNG }))
  assert.equal(f.imageS3Key, key('b'), 'last image item key even without image_s3_key')
  f = outputFieldsFromResult(run([T(), T()], { success: false }))
  assert.ok(isEnvelope(f.html), 'error run keeps what was displayed')
})

test('legacy cells: items in the old draw order, all expanded, no envelope', () => {
  const cell = { id: 'c1', image: PNG, output: 'txt', html: JSON.stringify(tbl(['a'], 3)) }
  const its = cellOutputItems(cell)
  assert.deepEqual(its.map(i => i.t), ['image', 'text', 'table'])
  assert.ok(its.every(i => i.t === 'text' || i.defaultExpanded))
  assert.equal(isMultiOutput(cell), false)
  const persisted = cellOutputItems({ id: 'c1', imageS3Key: key('a'), output: '' })
  assert.deepEqual(persisted.map(i => [i.t, i.key, i.legacy]), [['image', key('a'), true]])
  assert.deepEqual(cellOutputItems({ id: 'c1', html: PLY }).map(i => i.t), ['plotly'])
  assert.deepEqual(cellOutputItems({ id: 'c1', html: '<table></table>' }), [])
  assert.deepEqual(cellOutputItems(null), [])
})

test('envelope cells: first rich expanded, the rest collapsed; per-kind numbering; captions', () => {
  const html = buildOutputsEnvelope([X('hello\n'), T('direct'), X('mid\n'), T('upstream'), P(), { t: 'plotly', html: PLY, label: 'fig2' }, I('a'), I('b'), T()])
  const its = cellOutputItems({ id: 'c1', html, output: 'ignored' })
  assert.deepEqual(its.map(i => i.t), ['text', 'table', 'text', 'table', 'plotly', 'plotly', 'image', 'image', 'table'])
  assert.deepEqual(its.filter(i => i.t !== 'text').map(i => i.defaultExpanded), [true, false, false, false, false, false, false])
  assert.deepEqual(its.map(i => i.index), [0, 1, 2, 3, 4, 5, 6, 7, 8])
  assert.deepEqual(its.filter(i => i.t === 'table').map(i => i.ordinal), [1, 2, 3])
  assert.equal(its[1].caption, 'Table 1 · direct — 3 rows × 1 col')
  assert.equal(its[3].caption, 'Table 2 · upstream — 3 rows × 1 col')
  assert.equal(its[4].caption, 'Chart 1 · Plotly')
  assert.equal(its[5].caption, 'Chart 2 · fig2 · Plotly')
  assert.equal(its[7].caption, 'Image 2')
  assert.equal(its[8].caption, 'Table 3 — 3 rows × 1 col')
  assert.ok(its.filter(i => i.t !== 'text').every(i => i.sig))
  assert.equal(new Set(its.filter(i => i.t === 'table').map(i => i.sig)).size >= 1, true)
  assert.equal(isMultiOutput({ html }), true)
  assert.deepEqual(richCounts({ html }), { table: 3, plotly: 2, image: 2 })
  assert.equal(firstRichKind({ html }), 'table')
  assert.equal(hasRichOutput({ html }), true)
  assert.equal(hasRichOutput({ output: 'x' }), false)
})

test('caption uses total_rows/total_cols and pluralizes', () => {
  assert.equal(outputCaption({ t: 'table', ordinal: 2, label: 'upstream', payload: tbl(['a', 'b'], 12) }), 'Table 2 · upstream — 12 rows × 2 cols')
  assert.equal(outputCaption({ t: 'table', ordinal: 1, payload: tbl(['a'], 1) }), 'Table 1 — 1 row × 1 col')
  assert.equal(outputCaption({ t: 'table', ordinal: 1, payload: tbl(['a'], 5000) }), 'Table 1 — 5000 rows × 1 col')
  assert.equal(outputCaption({ t: 'plotly', ordinal: 3 }), 'Chart 3 · Plotly')
  assert.equal(outputCaption({ t: 'image', ordinal: 2 }), 'Image 2')
  assert.equal(outputCaption({ t: 'text' }), '')
})

test('Plotly detection never looks at the raw envelope string (it contains the substring)', () => {
  const html = buildOutputsEnvelope([T(), T()])
  assert.ok(!html.includes('data-plotly'))
  const withPly = buildOutputsEnvelope([T(), P()])
  assert.ok(withPly.includes('data-plotly'))
  assert.deepEqual(cellOutputItems({ html }).map(i => i.t), ['table', 'table'])
  // a table whose cell text mentions data-plotly stays a table
  const tricky = buildOutputsEnvelope([{ t: 'table', payload: tbl(['a'], 1, { cells: [['data-plotly="true"']] }) }, T()])
  assert.deepEqual(cellOutputItems({ html: tricky }).map(i => i.t), ['table', 'table'])
})

test('firstTable: first table of an envelope, the legacy table, or null', () => {
  const html = buildOutputsEnvelope([P(), T('b'), T('c')])
  assert.deepEqual(firstTable({ html }), tbl(['a'], 3))
  assert.deepEqual(firstTable({ html: JSON.stringify(tbl(['z'], 1)) }), tbl(['z'], 1))
  assert.equal(firstTable({ html: PLY }), null)
  assert.equal(firstTable({}), null)
})

test('stripEnvelopeImages removes every data:image, keeps keys; other strings unchanged', () => {
  const html = buildOutputsEnvelope([T(), I('a'), I('b'), { t: 'image', src: PNG }])
  assert.ok(html.includes('data:image'))
  const stripped = stripEnvelopeImages(html)
  assert.ok(!stripped.includes('data:image'))
  const items = parseOutputsEnvelope(stripped)
  // the third image had no key (never persisted): nothing to show once stripped, so parse drops it
  assert.deepEqual(items.filter(i => i.t === 'image').map(i => i.key), [key('a'), key('b')])
  assert.equal(stripEnvelopeImages(stripped), stripped, 'idempotent / byte identical when nothing to strip')
  for (const v of [null, undefined, '', PLY, JSON.stringify(tbl()), '{"__outputs__":1,"items":']) assert.equal(stripEnvelopeImages(v), v)
})

test('fillEnvelopeImages: i-th data URL → i-th image item; existing src kept; extras ignored', () => {
  const html = stripEnvelopeImages(buildOutputsEnvelope([I('a'), T(), I('b'), I('c')]))
  const filled = parseOutputsEnvelope(fillEnvelopeImages(html, ['data:image/png;base64,AAA', null, 'data:image/png;base64,CCC']))
  assert.deepEqual(filled.filter(i => i.t === 'image').map(i => i.src), ['data:image/png;base64,AAA', undefined, 'data:image/png;base64,CCC'])
  assert.equal(fillEnvelopeImages(PLY, ['x']), PLY)
  assert.equal(fillEnvelopeImages(html, []), html)
})
