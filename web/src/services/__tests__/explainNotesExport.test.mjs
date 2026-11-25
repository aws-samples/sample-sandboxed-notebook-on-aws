// Multi-line cell notes (the "Explain this code" notes are multi-line Markdown with a
// heading, lists and a code fence) across the exporters.
// Run with: node --test web/src/services/__tests__/explainNotesExport.test.mjs
// Snapshots: __snapshots__/notebook-multiline-note.md, notebook-multiline-note.html (UPDATE_SNAPSHOTS=1).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildNotebookHTML, buildNotebookMarkdown, buildIPYNB, buildAppHTML, buildAppMarkdown, noteBlockquote,
} from '../notebookExport.js'
import { fixtureTab, stubMermaid, identitySanitize, FIXED_NOW, fmtTime } from './fixtures/exportFixtures.mjs'
import { matchSnapshot } from './fixtures/snapshot.mjs'
const F = '```'
export const MULTI_NOTE = [
  '### Explanation (2026-11-13)',
  '',
  'Totals the orders **by month**.',
  '',
  '- reads `orders_df`',
  '- writes `monthly`',
  '',
  `${F}python`,
  'monthly = orders_df.groupby("month").sum()',
  '',
  'monthly',
  F,
  '',
  '**Notes:** non-numeric columns are dropped.',
].join('\n')
const cells = () => [
  { id: 'c1', type: 'code', code: 'x = 1', output: '', aiExplanation: MULTI_NOTE, aiExplanationSource: 'user', executionNumber: 1 },
  { id: 'c2', type: 'code', code: 'y = 2', output: '', aiExplanation: 'One line note.', aiExplanationSource: 'user', executionNumber: 2 },
  { id: 'c3', type: 'code', code: 'z = 3', output: '', executionNumber: 3 },
]
const mdOpts = { now: FIXED_NOW, formatTime: fmtTime }
const md = () => buildNotebookMarkdown(fixtureTab(), cells(), mdOpts).content
test('noteBlockquote: single line is italic, with the sparkle marker', () => {
  assert.equal(noteBlockquote('Sets **x**.'), '> \u2728 *Sets **x**.*')
})
test('noteBlockquote: multi-line prose keeps the marker on the first line, no italics', () => {
  assert.equal(noteBlockquote('First line.\n\nSecond.'), '> \u2728 First line.\n>\n> Second.')
})
test('noteBlockquote: a first line that starts a block gets the marker on its own line', () => {
  assert.equal(noteBlockquote('### H\nbody'), '> \u2728\n>\n> ### H\n> body')
})
test('noteBlockquote: empty, CRLF and surrounding blank lines', () => {
  assert.equal(noteBlockquote(''), '')
  assert.equal(noteBlockquote(null), '')
  assert.equal(noteBlockquote('\r\nA\r\nB\r\n\r\n'), '> \u2728 A\n> B')
})
test('Markdown export: every line of a multi-line note is inside the blockquote', () => {
  const out = md()
  const start = out.indexOf('> \u2728\n>\n> ### Explanation (2026-11-13)')
  assert.ok(start >= 0, 'marker line then the heading, both quoted')
  const end = out.indexOf('> **Notes:** non-numeric columns are dropped.')
  assert.ok(end > start)
  const block = out.slice(start, end + 50).split('\n')
  const noteLines = block.slice(0, block.findIndex((l) => l.startsWith('> **Notes:**')) + 1)
  assert.ok(noteLines.every((l) => l === '>' || l.startsWith('> ')), 'no unquoted line inside the note')
  assert.equal(noteLines.length, MULTI_NOTE.split('\n').length + 2)
})
test('Markdown export: the code fence stays intact inside the quote', () => {
  const out = md()
  assert.ok(out.includes('> ```python\n> monthly = orders_df.groupby("month").sum()\n>\n> monthly\n> ```\n'))
  assert.ok(!/^### Explanation/m.test(out), 'no stray unquoted heading')
  assert.ok(!out.includes('*### Explanation'), 'no italic marker around the heading')
})
test('Markdown export: single-line notes stay italic', () => {
  assert.ok(md().includes('> \u2728 *One line note.*\n\n'))
})
test('HTML export renders the multi-line note as Markdown', async () => {
  const h = (await buildNotebookHTML(fixtureTab(), cells(), { theme: 'light', sanitize: identitySanitize, renderMermaid: stubMermaid, now: FIXED_NOW, formatTime: fmtTime })).content
  assert.ok(/<h3[^>]*>Explanation \(2026-11-13\)<\/h3>/.test(h))
  assert.ok(/<li>reads <code>orders_df<\/code><\/li>/.test(h))
  assert.ok(h.includes('groupby'))
  matchSnapshot('notebook-multiline-note.html', h)
})
test('ipynb keeps the note verbatim in cell metadata; App exports carry no notes', async () => {
  const nb = JSON.parse(buildIPYNB(fixtureTab(), cells()).content)
  assert.equal(nb.cells[0].metadata.microvm_notebook.aiExplanation, MULTI_NOTE, 'all lines, byte for byte')
  const appMd = buildAppMarkdown(fixtureTab(), cells()).content
  assert.ok(!appMd.includes('Explanation (2026') && !appMd.includes('One line note'))
  const appHtml = (await buildAppHTML(fixtureTab(), cells(), { sanitize: identitySanitize, renderMermaid: stubMermaid, now: FIXED_NOW })).content
  assert.ok(!appHtml.includes('Explanation (2026') && !appHtml.includes('One line note'))
})
test('snapshot: Markdown with a multi-line note', () => {
  matchSnapshot('notebook-multiline-note.md', md())
})
