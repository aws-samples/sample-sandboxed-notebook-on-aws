// Unit tests for services/exportRender.js (static export renderer).
// Run with: node --test web/src/services/__tests__/exportRender.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  codeCard, createExportMarked, renderExportMarkdown, stripControls, inlineMermaid,
  readExportTheme, defaultExportTheme, exportCss, EXPORT_TOKENS, DEFAULT_EXPORT_THEMES,
} from '../exportRender.js'
import { stubMermaid, identitySanitize } from './fixtures/exportFixtures.mjs'

const F = '```'

test('codeCard colours Python, SQL and Markdown with tok-* spans and a plain type label', () => {
  const py = codeCard('import os\nx = "a"  # c\nf(1)', 'code')
  assert.match(py, /data-lang="code"/)
  assert.match(py, /<figcaption class="x-code-lang">Python<\/figcaption>/)
  for (const cls of ['tok-keyword', 'tok-string', 'tok-comment', 'tok-number', 'tok-function']) assert.ok(py.includes(`class="${cls}"`), cls)
  const sql = codeCard("SELECT a FROM t WHERE b = 'x' -- c", 'sql')
  assert.match(sql, />SQL</)
  assert.ok(sql.includes('<span class="tok-keyword">SELECT</span>'))
  const md = codeCard('# Title\n- item', 'markdown')
  assert.match(md, />Markdown</)
  assert.ok(md.includes('tok-md-heading'))
  assert.ok(!/<button/i.test(py + sql + md))
})

test('code text is escaped and never lost (textContent equals the source)', () => {
  const src = 'print("<b>&</b>")\n' + 'x'.repeat(500)
  const html = codeCard(src, 'code')
  const text = html.replace(/<figcaption[\s\S]*?<\/figcaption>/, '').replace(/<[^>]+>/g, '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
  assert.equal(text, src)
})

test('the export marked renders fences as static cards and mermaid as a placeholder', () => {
  const m = createExportMarked()
  const html = m.parse(`${F}python\nx = 1\n${F}\n\n${F}sql\nSELECT 1\n${F}\n\n${F}\nbare = True\n${F}\n\n${F}bash\nls -la\n${F}\n\n${F}mermaid\ngraph LR\nA-->B\n${F}`)
  assert.equal((html.match(/class="x-code"/g) || []).length, 4)
  assert.match(html, /data-lang="other"><figcaption class="x-code-lang">bash/)
  assert.match(html, /data-mermaid="graph%20LR%0AA--%3EB"/)
  assert.ok(!/ai-code|<button/i.test(html))
})

test('stripControls removes buttons/selects/textareas/inputs; checkboxes become glyphs', () => {
  const out = stripControls('<p>a<button class="x">Copy</button>b<select><option>1</option></select>'
    + '<textarea>t</textarea><input type="text" value="v"><input checked="" disabled="" type="checkbox"> done'
    + '<input type="checkbox"> todo<BUTTON>Insert</BUTTON></p>')
  assert.equal(out, '<p>ab☑ done☐ todo</p>')
})

test('renderExportMarkdown: chat fences are normalized and the result has no controls', () => {
  const html = renderExportMarkdown('Text.```python\nx = 1\n```\n\n- [x] ok\n\n<button>Run</button>', { sanitize: identitySanitize, chat: true })
  assert.ok(html.includes('class="x-code"'))
  assert.ok(html.includes('☑'))
  assert.ok(!/<(button|input|select)\b/i.test(html))
})

test('inlineMermaid splices the SVG; an error or no renderer keeps a static source card', async () => {
  const html = createExportMarked().parse(`${F}mermaid\ngraph LR\nA-->B\n${F}`)
  const ok = await inlineMermaid(html, stubMermaid)
  assert.match(ok, /<div class="x-mermaid"><svg data-stub="1"/)
  assert.ok(!ok.includes('mermaid-src'))
  const bad = await inlineMermaid(html, async () => ({ error: 'Parse error' }))
  assert.match(bad, /Diagram not rendered: Parse error/)
  assert.match(bad, /x-code-lang">Mermaid</)
  const none = await inlineMermaid(html, null)
  assert.match(none, /Diagram source \(not rendered\)/)
  const thrown = await inlineMermaid(html, async () => { throw new Error('boom') })
  assert.match(thrown, /boom/)
})

test('no DOM: readExportTheme falls back to the dark defaults; every theme defines every token', () => {
  const t = readExportTheme()
  assert.equal(t.name, 'dark')
  assert.deepEqual(t.vars, { ...DEFAULT_EXPORT_THEMES.dark })
  for (const name of ['dark', 'light', 'ember']) {
    for (const tok of EXPORT_TOKENS) assert.ok(DEFAULT_EXPORT_THEMES[name][tok], `${name} ${tok}`)
  }
  assert.equal(defaultExportTheme('nope').name, 'dark')
})

test('readExportTheme reads the live tokens of the current theme (fake DOM)', () => {
  const root = { getAttribute: () => 'light' }
  globalThis.getComputedStyle = () => ({ getPropertyValue: (t) => (t === '--cm-keyword' ? ' #123456 ' : '') })
  try {
    const t = readExportTheme(root)
    assert.equal(t.name, 'light')
    assert.equal(t.vars['--cm-keyword'], '#123456')
    assert.equal(t.vars['--cm-string'], DEFAULT_EXPORT_THEMES.light['--cm-string'])
  } finally {
    delete globalThis.getComputedStyle
  }
})

test('exportCss maps tok-* to the --cm-* palette, wraps code and uses the theme tokens', () => {
  const css = exportCss(defaultExportTheme('light'))
  assert.match(css, /--cm-keyword:#9b2393/)
  assert.match(css, /\.tok-keyword\{color:var\(--cm-keyword\)/)
  assert.match(css, /pre\{[^}]*white-space:pre-wrap/)
  assert.match(css, /color-scheme:light/)
  assert.match(exportCss(defaultExportTheme('ember')), /tok-md-heading\{color:var\(--lang-markdown\)/)
})
