// Unit tests for the notebook HTML / Markdown exporters (services/notebookExport.js).
// Run with: node --test web/src/services/__tests__/notebookExportHtml.test.mjs
// Snapshots: __snapshots__/notebook-<theme>.html, notebook.md (UPDATE_SNAPSHOTS=1).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildNotebookHTML, buildNotebookMarkdown, buildAppHTML } from '../notebookExport.js'
import {
  fixtureTab, fixtureCells, fixtureTrust, stubMermaid, identitySanitize, FIXED_NOW, fmtTime, POISON,
} from './fixtures/exportFixtures.mjs'
import { matchSnapshot } from './fixtures/snapshot.mjs'

const opts = (theme) => ({
  theme, sanitize: identitySanitize, renderMermaid: stubMermaid, trustByCell: fixtureTrust, now: FIXED_NOW, formatTime: fmtTime,
})
const html = (theme = 'light') => buildNotebookHTML(fixtureTab(), fixtureCells(), opts(theme)).then(r => r.content)

test('notebook HTML has no interactive controls', async () => {
  for (const theme of ['light', 'dark', 'ember']) {
    const h = await html(theme)
    assert.ok(!/<button\b/i.test(h), `${theme}: button`)
    assert.ok(!/<select\b/i.test(h), `${theme}: select`)
    assert.ok(!/<input\b/i.test(h), `${theme}: input`)
    assert.ok(!/<textarea\b/i.test(h), `${theme}: textarea`)
    assert.ok(!/<details\b/i.test(h), `${theme}: details`)
  }
})

test('notebook HTML: coloured code, typed cards, rendered mermaid, outputs', async () => {
  const h = await html('light')
  assert.ok(h.includes('class="tok-keyword"'))
  assert.ok(h.includes('class="tok-string"'))
  assert.ok(h.includes('<figure class="x-code" data-lang="code"><figcaption class="x-code-lang">Python</figcaption>'))
  assert.ok(h.includes('<figure class="x-code" data-lang="sql"><figcaption class="x-code-lang">SQL</figcaption>'))
  assert.ok(h.includes('<span class="tok-keyword">SELECT</span>'))
  // mermaid → SVG, no raw source placeholder left
  assert.ok(h.includes('<div class="x-mermaid"><svg data-stub="1"'))
  assert.ok(!h.includes('mermaid-src'))
  assert.ok(!h.includes('data-mermaid'))
  // Plotly snippet + light re-theme + offline note
  assert.ok(h.includes('data-plotly="true"'))
  assert.ok(h.includes('plotly light retheme'))
  assert.ok(h.includes('class="x-plotly-offline"'))
  // DataFrame table
  assert.match(h, /<table class="df-table"><thead><tr><th>region<\/th><th>orders<\/th>/)
  // @param as static values
  assert.ok(h.includes('<span class="x-param-label">Region</span> = <span class="x-param-value">us-east-1</span>'))
  assert.ok(h.includes('<span class="x-param-label">limit</span> = <span class="x-param-value">25</span>'))
  // Lineage trust as static pills, run metrics as text
  assert.ok(h.includes('<span class="x-trust x-trust-green">Trusted</span>'))
  assert.ok(h.includes('<span class="x-trust x-trust-amber">Review</span>'))
  assert.ok(h.includes('<span class="x-trust x-trust-red">Upstream error</span>'))
  assert.ok(h.includes('Executed in 1.2 s · peak 182 MB · 63% CPU'))
  // image + task list glyphs
  assert.ok(h.includes('<img src="data:image/png;base64,'))
  assert.ok(h.includes('☑ load data') && h.includes('☐ review'))
  // self-contained, themed
  assert.match(h, /^<!doctype html><html lang="en" data-theme="light">/)
  assert.ok(h.includes('--cm-keyword:#9b2393'))
})

test('notebook HTML escapes text output and errors', async () => {
  const h = await html('dark')
  assert.ok(h.includes('<pre>&lt;b&gt;us-east-1&lt;/b&gt; 25'))
  assert.ok(!h.includes('<b>us-east-1</b>'))
  assert.ok(h.includes('<pre class="x-error">Traceback'))
  assert.ok(h.includes('ValueError: bad &lt;input&gt; value'))
  assert.ok(h.includes('plotly dark retheme'))
})

test('notebook Markdown: fenced python/sql/mermaid, no collapsibles or button text', () => {
  const md = buildNotebookMarkdown(fixtureTab(), fixtureCells(), { now: FIXED_NOW, formatTime: fmtTime }).content
  assert.ok(md.includes('```python\nimport os\n'))
  assert.ok(md.includes('```sql\nSELECT region, COUNT(*) AS orders\n'))
  assert.ok(md.includes('```mermaid\ngraph LR\n'))
  assert.ok(md.includes('**Error:**\n\n```\nTraceback'))
  assert.ok(md.includes('- **Region:** us-east-1'))
  assert.ok(md.includes('| region | orders |'))
  assert.ok(!/<details|<summary/i.test(md))
  for (const word of ['Copy', 'Insert', 'Source', '▾']) assert.ok(!md.includes(word), word)
})

test('fences are longer than any backtick run inside the code', () => {
  const cells = [{ id: 'a', type: 'code', code: 'x = """\n```\ninner\n```\n"""' }]
  const md = buildNotebookMarkdown({ name: 'N' }, cells, { now: FIXED_NOW, formatTime: fmtTime }).content
  assert.ok(md.includes('````python\nx = """\n```\ninner\n```\n"""\n````'))
})

test('no secret or transient value reaches the HTML / Markdown export', async () => {
  const h = await html('light')
  const md = buildNotebookMarkdown(fixtureTab(), fixtureCells(), { now: FIXED_NOW, formatTime: fmtTime }).content
  assert.ok(!h.includes(POISON))
  assert.ok(!md.includes(POISON))
})

test('app HTML renders markdown statically with mermaid SVG and no controls', async () => {
  const r = await buildAppHTML(fixtureTab(), fixtureCells(), { sanitize: identitySanitize, renderMermaid: stubMermaid, now: FIXED_NOW })
  assert.ok(r.content.includes('<svg data-stub="1"'))
  assert.ok(!/<(button|input|select)\b/i.test(r.content))
})

test('snapshots (light, dark, markdown)', async () => {
  matchSnapshot('notebook-light.html', await html('light'))
  matchSnapshot('notebook-dark.html', await html('dark'))
  matchSnapshot('notebook.md', buildNotebookMarkdown(fixtureTab(), fixtureCells(), { now: FIXED_NOW, formatTime: fmtTime }).content)
})
