// Unit tests for services/chatExport.js (AI chat → static HTML / Markdown).
// Run with: node --test web/src/services/__tests__/chatExport.test.mjs
// Snapshots: __snapshots__/chat-<theme>.html, chat.md (UPDATE_SNAPSHOTS=1).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildChatHTML, buildChatMarkdown, refenceMarkdown } from '../chatExport.js'
import { groupTurns } from '../chatTurns.js'
import { fixtureChat, stubMermaid, identitySanitize, FIXED_NOW, fmtTime } from './fixtures/exportFixtures.mjs'
import { matchSnapshot } from './fixtures/snapshot.mjs'

const F = '```'
const messages = fixtureChat()
const turns = groupTurns(messages)
// The panel's turn store: an answered approval on turn 3.
const turnMeta = {
  [turns[2].key]: { steps: [{ kind: 'approval', decision: 'approved', action: 'run_code', detail: 'print("hi")' }] },
}
const base = { notebookName: 'Export Fixture', messages, turnMeta, now: FIXED_NOW, formatTime: fmtTime }
const html = (theme = 'light') => buildChatHTML({ ...base, theme, sanitize: identitySanitize, renderMermaid: stubMermaid }).then(r => r.content)

test('chat HTML has no interactive controls or panel button text', async () => {
  for (const theme of ['light', 'dark', 'ember']) {
    const h = await html(theme)
    assert.ok(!/<(button|select|input|textarea|details)\b/i.test(h), theme)
    const text = h.replace(/<style>[\s\S]*?<\/style>/, '').replace(/<[^>]+>/g, ' ')
    for (const word of ['Copy', 'Insert', 'Source', '▾', '▸']) assert.ok(!text.includes(word), `${theme}: ${word}`)
  }
})

test('chat HTML mirrors the turn view: headings, question, steps, typed coloured cards', async () => {
  const h = await html('light')
  assert.ok(h.includes('<h2>Turn 1: Orders per region</h2>'))
  assert.ok(h.includes('<h2>Turn 2: Data flow</h2>'))
  assert.ok(h.includes('<h2>Turn 3: Plot it</h2>'))
  assert.ok(h.includes('<span class="x-att">regions.csv</span>'))
  assert.ok(h.includes('<span class="x-src">Intel</span>Document the data flow'))
  assert.ok(h.includes('<li>Inspected variables</li>'))
  assert.ok(h.includes('<li>Approved: Run code<figure class="x-code" data-lang="code">'))
  for (const label of ['Python', 'SQL', 'Markdown']) assert.ok(h.includes(`<figcaption class="x-code-lang">${label}</figcaption>`), label)
  assert.ok(h.includes('class="tok-keyword"') && h.includes('class="tok-string"'))
  assert.ok(h.includes('tok-md-heading'))
  assert.ok(h.includes('<div class="x-mermaid"><svg data-stub="1"'))
  assert.ok(!h.includes('mermaid-src'))
  assert.ok(!h.includes('ai-code-block') && !h.includes('ai-turn-out'))
  // status note folded out of the body
  assert.ok(!h.includes('_(Inspected variables)_'))
  assert.match(h, /data-theme="light"/)
})

test('chat Markdown: same turn headings, fenced code with language tags, no button text', () => {
  const md = buildChatMarkdown(base).content
  assert.ok(md.includes('## Turn 1: Orders per region'))
  assert.ok(md.includes('## Turn 2: Data flow'))
  assert.ok(md.includes('## Turn 3: Plot it'))
  assert.ok(md.includes('```python\nimport pandas as pd\n'))
  assert.ok(md.includes('```sql\nSELECT region'))
  assert.ok(md.includes('```markdown\n# Flow\n'))
  assert.ok(md.includes('```mermaid\ngraph LR\n'))
  // the bare fence in turn 3 is Python (as in the panel)
  assert.ok(md.includes('```python\nimport plotly.express as px\n'))
  assert.ok(md.includes('1. Approved: Run code'))
  for (const word of ['Copy', 'Insert', 'Source', '▾']) assert.ok(!md.includes(word), word)
})

test('refenceMarkdown tags fences, keeps prose verbatim and lengthens fences around backticks', () => {
  assert.equal(refenceMarkdown('Just text.'), 'Just text.')
  assert.equal(refenceMarkdown(`Intro\n\n${F}py\nx = 1\n${F}\n\nEnd`), `Intro\n\n${F}python\nx = 1\n${F}\n\nEnd`)
  assert.equal(refenceMarkdown(`${F}\nbare = 1\n${F}`), `${F}python\nbare = 1\n${F}`)
  assert.equal(refenceMarkdown(`${F}bash\nls\n${F}`), `${F}bash\nls\n${F}`)
  const inner = refenceMarkdown(`~~~markdown\n${F}sql\nSELECT 1\n${F}\n~~~`)
  assert.equal(inner, `\`\`\`\`markdown\n${F}sql\nSELECT 1\n${F}\n\`\`\`\``)
})

test('snapshots (light, dark, markdown)', async () => {
  matchSnapshot('chat-light.html', await html('light'))
  matchSnapshot('chat-dark.html', await html('dark'))
  matchSnapshot('chat.md', buildChatMarkdown(base).content)
})
