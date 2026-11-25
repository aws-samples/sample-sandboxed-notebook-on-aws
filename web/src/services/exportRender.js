/**
 * exportRender — the shared, pure renderer behind the chat and notebook HTML/MD
 * exports (services/chatExport.js, services/notebookExport.js).
 *
 * Why a separate module: the live surfaces render through the panel's GLOBAL
 * `marked` (whose code renderer emits the chat's interactive code card: ▾ toggle,
 * Source / Copy / Insert buttons) and through markdown.js (which imports the KaTeX
 * CSS, so it can't load under Node). An export needs neither: it is a static,
 * self-contained file. So this module owns:
 *   - a DEDICATED Marked instance (never the global) whose code renderer emits a
 *     static code card: type label (Python / SQL / Markdown) as plain text, the
 *     type-coloured side edge, code coloured by the app's own highlighter
 *     (basicHighlight.js, the one the chat cards use), wrapped so no text is lost;
 *   - the export stylesheet, built ONLY from the app's theme tokens (read from the
 *     live page, or the copied defaults below when there is no DOM);
 *   - stripControls(): removes every interactive control from export HTML;
 *   - inlineMermaid(): splices rendered mermaid SVG into the placeholders.
 *
 * Pure and Node-loadable (`.js` imports only; no CSS imports, no DOM at import).
 */
import { Marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import { highlightBasic, highlightMarkdown } from './basicHighlight.js'
import { normalizeFences, langKey, insertableType } from './chatTurns.js'
import { sanitizeMarkdown } from './sanitize.js'

/** HTML-escape text (also quotes, so it is safe inside attributes). */
export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// ── Theme ─────────────────────────────────────────────────────────────────────

/** The custom properties the export stylesheet uses (all from theme.css). */
export const EXPORT_TOKENS = [
  '--surface-0', '--surface-1', '--surface-2',
  '--text-primary', '--text-secondary', '--text-tertiary', '--text-muted',
  '--border-subtle', '--border-default',
  '--cell-output-bg', '--console-bg',
  '--accent-primary', '--accent-danger', '--accent-warning', '--accent-success', '--accent-purple',
  '--link', '--font-sans', '--font-mono',
  '--cm-text', '--cm-keyword', '--cm-keyword-weight', '--cm-string', '--cm-number',
  '--cm-comment', '--cm-comment-style', '--cm-definition',
  '--lang-python', '--lang-sql', '--lang-markdown',
  '--ai-block-head-python', '--ai-block-head-sql', '--ai-block-head-markdown',
]

const FONT_SANS = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", Helvetica, Arial, sans-serif'
const FONT_MONO = 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco, Consolas, monospace'

/**
 * Resolved token values per theme, copied from theme.css (dark = the Apple dark
 * block, light = the Apple light block, ember = the ember block over the :root
 * base). Used when there is no DOM (unit tests) and as the fallback for any token
 * the live page doesn't define (e.g. ember has no --link).
 */
export const DEFAULT_EXPORT_THEMES = Object.freeze({
  dark: Object.freeze({
    '--surface-0': '#2c2c2e', '--surface-1': '#1c1c1e', '--surface-2': '#1c1c1e',
    '--text-primary': '#f5f5f7', '--text-secondary': '#d1d1d6', '--text-tertiary': '#aeaeb2', '--text-muted': '#aeaeb2',
    '--border-subtle': 'rgba(255, 255, 255, 0.08)', '--border-default': 'rgba(255, 255, 255, 0.12)',
    '--cell-output-bg': '#202022', '--console-bg': '#2c2c2e',
    '--accent-primary': '#64aeff', '--accent-danger': '#ff8078', '--accent-warning': '#ff9f0a',
    '--accent-success': '#30d158', '--accent-purple': '#da8fff',
    '--link': '#64aeff', '--font-sans': FONT_SANS, '--font-mono': FONT_MONO,
    '--cm-text': '#f5f5f7', '--cm-keyword': '#ff7ab2', '--cm-keyword-weight': '600', '--cm-string': '#ff8170',
    '--cm-number': '#d9c97c', '--cm-comment': '#98ae9f', '--cm-comment-style': 'normal', '--cm-definition': '#6bb3ff',
    '--lang-python': '#64aeff', '--lang-sql': '#ff9f0a', '--lang-markdown': '#da8fff',
    '--ai-block-head-python': 'color-mix(in srgb, #64aeff 8%, #202022)',
    '--ai-block-head-sql': 'color-mix(in srgb, #ff9f0a 8%, #202022)',
    '--ai-block-head-markdown': 'color-mix(in srgb, #da8fff 8%, #202022)',
  }),
  light: Object.freeze({
    '--surface-0': '#f5f5f7', '--surface-1': '#ffffff', '--surface-2': '#ffffff',
    '--text-primary': '#1d1d1f', '--text-secondary': '#424245', '--text-tertiary': '#6e6e73', '--text-muted': '#6e6e73',
    '--border-subtle': 'rgba(0, 0, 0, 0.08)', '--border-default': 'rgba(0, 0, 0, 0.10)',
    '--cell-output-bg': '#fbfbfd', '--console-bg': '#f5f5f7',
    '--accent-primary': '#0066cc', '--accent-danger': '#d70015', '--accent-warning': '#b25000',
    '--accent-success': '#1e7a34', '--accent-purple': '#8944ab',
    '--link': '#0066cc', '--font-sans': FONT_SANS, '--font-mono': FONT_MONO,
    '--cm-text': '#1d1d1f', '--cm-keyword': '#9b2393', '--cm-keyword-weight': '600', '--cm-string': '#c41a16',
    '--cm-number': '#1c00cf', '--cm-comment': '#476a50', '--cm-comment-style': 'normal', '--cm-definition': '#0f68a0',
    '--lang-python': '#0066cc', '--lang-sql': '#c2410c', '--lang-markdown': '#8944ab',
    '--ai-block-head-python': 'color-mix(in srgb, #0066cc 5%, #fbfbfd)',
    '--ai-block-head-sql': 'color-mix(in srgb, #c2410c 5%, #fbfbfd)',
    '--ai-block-head-markdown': 'color-mix(in srgb, #8944ab 5%, #fbfbfd)',
  }),
  ember: Object.freeze({
    '--surface-0': '#0a0a08', '--surface-1': '#111110', '--surface-2': '#1a1918',
    '--text-primary': '#f5f0e8', '--text-secondary': '#c8bfb0', '--text-tertiary': '#8f877a', '--text-muted': '#5e584e',
    '--border-subtle': 'rgba(255, 160, 60, 0.06)', '--border-default': 'rgba(255, 160, 60, 0.12)',
    '--cell-output-bg': '#0a0a08', '--console-bg': 'color-mix(in srgb, #0a0a08 70%, #000 30%)',
    '--accent-primary': '#ff6b2b', '--accent-danger': '#ff3333', '--accent-warning': '#ffb347',
    '--accent-success': '#39ff14', '--accent-purple': '#ff7eb3',
    '--link': '#ff6b2b', '--font-sans': FONT_SANS, '--font-mono': FONT_MONO,
    '--cm-text': '#f5f0e8', '--cm-keyword': '#ff7eb3', '--cm-keyword-weight': 'normal', '--cm-string': '#39ff14',
    '--cm-number': '#ffb347', '--cm-comment': '#5e584e', '--cm-comment-style': 'italic', '--cm-definition': '#ff6b2b',
    '--lang-python': '#4f8bff', '--lang-sql': '#e06c1d', '--lang-markdown': '#cba6f7',
    '--ai-block-head-python': 'color-mix(in srgb, #4f8bff 8%, #0a0a08)',
    '--ai-block-head-sql': 'color-mix(in srgb, #e06c1d 8%, #0a0a08)',
    '--ai-block-head-markdown': 'color-mix(in srgb, #cba6f7 8%, #0a0a08)',
  }),
})

const _themeName = (n) => (Object.prototype.hasOwnProperty.call(DEFAULT_EXPORT_THEMES, n) ? n : 'dark')

/** A theme object from the copied defaults: { name, vars }. */
export function defaultExportTheme(name = 'dark') {
  const n = _themeName(name)
  return { name: n, vars: { ...DEFAULT_EXPORT_THEMES[n] } }
}

/**
 * The export theme = the user's CURRENT app theme (<html data-theme>), with every
 * token read from the live page so the file matches what's on screen. No DOM →
 * the copied dark defaults (the app's default theme). Missing tokens fall back
 * to the copied value for that theme.
 */
export function readExportTheme(root) {
  const el = root || (typeof document !== 'undefined' ? document.documentElement : null)
  if (!el || typeof getComputedStyle !== 'function') return defaultExportTheme('dark')
  const name = _themeName(el.getAttribute('data-theme') || 'dark')
  const cs = getComputedStyle(el)
  const vars = {}
  for (const t of EXPORT_TOKENS) {
    const v = (cs.getPropertyValue(t) || '').trim()
    vars[t] = v || DEFAULT_EXPORT_THEMES[name][t]
  }
  return { name, vars }
}

// The markdown-source tint rules differ by theme family exactly as the app's do:
// light/dark = theme-light.css (C2 block), ember = AiChatPanel.css base rules.
const MD_TOK_LIGHT_DARK = `
.x-code .tok-md-heading,.x-code .tok-md-heading-marker{color:var(--text-primary);font-weight:600}
.x-code .tok-md-list-marker{color:var(--text-tertiary)}
.x-code .tok-md-quote-marker{color:var(--text-tertiary)}
.x-code .tok-md-quote{color:var(--text-secondary)}
.x-code .tok-md-code{color:var(--cm-string)}
.x-code .tok-md-bold{color:var(--text-primary);font-weight:700}
.x-code .tok-md-italic{color:var(--text-primary);font-style:italic}
.x-code .tok-md-link{color:var(--link)}
.x-code .tok-md-link-url{color:var(--text-tertiary)}`
const MD_TOK_EMBER = `
.x-code .tok-md-heading-marker{color:var(--lang-markdown);opacity:.6}
.x-code .tok-md-heading{color:var(--lang-markdown);font-weight:700}
.x-code .tok-md-list-marker{color:var(--accent-warning);font-weight:700}
.x-code .tok-md-quote-marker{color:var(--text-muted)}
.x-code .tok-md-quote{color:var(--text-secondary);font-style:italic}
.x-code .tok-md-code{color:var(--accent-success)}
.x-code .tok-md-bold{color:var(--text-primary);font-weight:700}
.x-code .tok-md-italic{color:var(--text-primary);font-style:italic}
.x-code .tok-md-link{color:var(--link)}
.x-code .tok-md-link-url{color:var(--text-muted)}`

/**
 * The export stylesheet: `:root{tokens}` plus static rules that use only those
 * tokens (Apple-like, matching the app's light/dark design and ember).
 */
export function exportCss(theme) {
  const t = theme && theme.vars ? theme : defaultExportTheme(theme && theme.name)
  const decl = Object.entries(t.vars).map(([k, v]) => `${k}:${v}`).join(';')
  const scheme = t.name === 'light' ? 'light' : 'dark'
  return `:root{color-scheme:${scheme};${decl}}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);color:var(--text-primary);font-family:var(--font-sans);font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.x-doc{max-width:960px;margin:0 auto;padding:32px 28px 64px}
h1{font-size:24px;font-weight:600;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:17px;font-weight:600;margin:28px 0 10px}
h3{font-size:15px;font-weight:600;margin:20px 0 8px}
a{color:var(--link)}
.x-desc{color:var(--text-secondary);margin:0 0 8px}
.x-meta{color:var(--text-tertiary);font-size:12px;margin:0 0 24px}
code{font-family:var(--font-mono);font-size:.92em}
:not(pre)>code{background:var(--surface-0);border:1px solid var(--border-subtle);border-radius:4px;padding:0 4px}
pre{margin:0;font-family:var(--font-mono);font-size:13px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;word-break:normal;max-width:100%}
pre code{background:none;border:0;padding:0;font-size:inherit}
.x-code{margin:10px 0;border:1px solid var(--border-default);border-radius:8px;background:var(--cell-output-bg);overflow:hidden;box-shadow:inset 3px 0 0 0 var(--lang-python)}
.x-code-lang{display:block;padding:4px 12px;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-tertiary);border-bottom:1px solid var(--border-subtle);box-shadow:inset 3px 0 0 0 var(--lang-python)}
.x-code pre{padding:10px 12px 10px 14px;color:var(--cm-text)}
.x-code[data-lang="code"]{box-shadow:inset 3px 0 0 0 var(--lang-python)}
.x-code[data-lang="sql"]{box-shadow:inset 3px 0 0 0 var(--lang-sql)}
.x-code[data-lang="markdown"]{box-shadow:inset 3px 0 0 0 var(--lang-markdown)}
.x-code[data-lang="code"] .x-code-lang{color:var(--lang-python);background:var(--ai-block-head-python);box-shadow:inset 3px 0 0 0 var(--lang-python)}
.x-code[data-lang="sql"] .x-code-lang{color:var(--lang-sql);background:var(--ai-block-head-sql);box-shadow:inset 3px 0 0 0 var(--lang-sql)}
.x-code[data-lang="markdown"] .x-code-lang{color:var(--lang-markdown);background:var(--ai-block-head-markdown);box-shadow:inset 3px 0 0 0 var(--lang-markdown)}
.x-code .tok-keyword{color:var(--cm-keyword);font-weight:var(--cm-keyword-weight)}
.x-code .tok-string{color:var(--cm-string)}
.x-code .tok-number{color:var(--cm-number)}
.x-code .tok-comment{color:var(--cm-comment);font-style:var(--cm-comment-style)}
.x-code .tok-function{color:var(--cm-definition)}${t.name === 'ember' ? MD_TOK_EMBER : MD_TOK_LIGHT_DARK}
table{border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{border:1px solid var(--border-default);padding:5px 10px;text-align:left;vertical-align:top}
th{background:var(--surface-0);font-weight:600}
.table-scroll{max-height:440px;overflow:auto;border:1px solid var(--border-default);border-radius:8px;margin:8px 0}
.table-scroll table{margin:0}
.table-scroll thead th{position:sticky;top:0;z-index:1}
.df-truncation-note{color:var(--text-tertiary);font-size:12px;padding:6px 10px}
img{max-width:100%}
blockquote{margin:8px 0;padding:2px 12px;border-left:3px solid var(--border-default);color:var(--text-secondary)}
hr{border:0;border-top:1px solid var(--border-subtle);margin:24px 0}
.x-mermaid{margin:10px 0;padding:12px;border:1px solid var(--border-subtle);border-radius:8px;background:var(--surface-1);overflow-x:auto;text-align:center}
.x-mermaid svg{max-width:100%;height:auto}
.x-note{color:var(--text-tertiary);font-size:12px;margin:4px 0}
.x-badge{display:inline-block;font-family:var(--font-mono);font-size:11px;font-weight:600;padding:1px 7px;border-radius:5px;border:1px solid var(--border-default);color:var(--text-secondary)}
.x-badge[data-lang="code"]{color:var(--lang-python)}
.x-badge[data-lang="sql"]{color:var(--lang-sql)}
.x-badge[data-lang="markdown"]{color:var(--lang-markdown)}
.x-trust{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;border:1px solid currentColor}
.x-trust-green{color:var(--accent-success)}
.x-trust-amber{color:var(--accent-warning)}
.x-trust-red{color:var(--accent-danger)}
.x-plotly .modebar-container,.x-plotly .modebar{display:none!important}
.x-plotly-offline{color:var(--text-tertiary);font-size:12px;font-style:italic}
@media print{body{background:#fff}.x-doc{max-width:none;padding:0}.x-code,.x-cell,.x-turn{break-inside:avoid}}
`
}

// ── Markdown → static HTML ────────────────────────────────────────────────────

const CODE_LABEL = { code: 'Python', sql: 'SQL', markdown: 'Markdown' }

/**
 * A static code card: plain type label + the code coloured by the app's
 * highlighter. `type` is a cell/insertable type ('code' | 'sql' | 'markdown'),
 * `label` overrides the label (other fence languages keep their own name).
 */
export function codeCard(code, type, label) {
  const src = String(code ?? '')
  const t = type === 'sql' || type === 'markdown' ? type : (type === 'code' || type === 'python' ? 'code' : null)
  const inner = t === 'sql' ? highlightBasic(src, 'sql')
    : t === 'markdown' ? highlightMarkdown(src)
      : t === 'code' ? highlightBasic(src, 'python')
        : esc(src)
  const lab = label || (t ? CODE_LABEL[t] : 'Code')
  return `<figure class="x-code" data-lang="${t || 'other'}"><figcaption class="x-code-lang">${esc(lab)}</figcaption>`
    + `<pre><code>${inner}</code></pre></figure>`
}

/**
 * A NEW Marked instance for exports (the global `marked` carries the chat's
 * interactive renderer). Math renders as MathML (no KaTeX CSS needed offline).
 */
export function createExportMarked() {
  const m = new Marked({ breaks: true })
  m.use(markedKatex({ throwOnError: false, nonStandard: true, output: 'mathml' }))
  m.use({
    renderer: {
      code(token) {
        const k = langKey(token.lang)
        const text = token.text || ''
        if (k === 'mermaid') {
          return `<div class="mermaid-diagram" data-mermaid="${encodeURIComponent(text)}">`
            + `<pre class="mermaid-src">${esc(text)}</pre></div>`
        }
        const ins = insertableType(token.lang)
        if (ins) return codeCard(text, ins)
        // Common aliases the notebook markdown renderer also colours.
        if (['python3', 'ipython'].includes(k)) return codeCard(text, 'code')
        if (['duckdb', 'postgres', 'postgresql', 'athena'].includes(k)) return codeCard(text, 'sql', 'SQL')
        return codeCard(text, null, k || 'Code')
      },
    },
  })
  return m
}

const _exportMarked = createExportMarked()

const _defaultSanitize = (html) => {
  try { return sanitizeMarkdown(html) } catch { return html }
}

/**
 * Render markdown to static export HTML: (chat) fence normalization → parse →
 * sanitize → strip controls. Mermaid stays as placeholders (see inlineMermaid).
 */
export function renderExportMarkdown(md, { sanitize = _defaultSanitize, chat = false } = {}) {
  const src = String(md ?? '')
  if (!src) return ''
  let html
  try {
    html = _exportMarked.parse(chat ? normalizeFences(src) : src)
  } catch {
    html = `<pre>${esc(src)}</pre>`
  }
  return stripControls(sanitize(html))
}

/**
 * Remove every interactive control: <button>, <select>, <textarea> (with their
 * content), checkboxes become a static ☑ / ☐ glyph, any other <input> is removed.
 */
export function stripControls(html) {
  return String(html ?? '')
    .replace(/<button\b[^>]*>[\s\S]*?<\/button\s*>/gi, '')
    .replace(/<select\b[^>]*>[\s\S]*?<\/select\s*>/gi, '')
    .replace(/<textarea\b[^>]*>[\s\S]*?<\/textarea\s*>/gi, '')
    .replace(/<(button|select|textarea)\b[^>]*\/?>/gi, '')
    .replace(/<input\b[^>]*>/gi, (tag) => {
      if (!/\btype\s*=\s*["']?checkbox/i.test(tag)) return ''
      return /\bchecked\b/i.test(tag) ? '☑' : '☐'
    })
}

// Same placeholder shape as markdown.js MERMAID_PLACEHOLDER_RE (attribute-order
// tolerant; swallows the inner source <pre>).
const MERMAID_RE = /<div\b[^>]*\bdata-mermaid="([^"]*)"[^>]*>[\s\S]*?<\/div>/g

const _safeDecode = (s) => { try { return decodeURIComponent(s) } catch { return s } }

/**
 * Replace each mermaid placeholder with `<div class="x-mermaid">svg</div>`, using
 * `renderMermaid(src) → {svg} | {error} | null`. Spliced AFTER sanitize, as the app
 * does (DOMPurify strips foreignObject labels). On error, or with no renderer, the
 * source stays as a static "Mermaid" code card plus a one-line note.
 */
export async function inlineMermaid(html, renderMermaid) {
  const s = String(html ?? '')
  if (s.indexOf('data-mermaid') === -1) return s
  const found = []
  for (const m of s.matchAll(MERMAID_RE)) found.push(m[1])
  const rendered = new Map()
  for (const enc of found) {
    if (rendered.has(enc)) continue
    const src = _safeDecode(enc)
    let res = null
    if (typeof renderMermaid === 'function') {
      try { res = await renderMermaid(src) } catch (e) { res = { error: e?.message || 'render failed' } }
    }
    rendered.set(enc, { src, res })
  }
  return s.replace(MERMAID_RE, (whole, enc) => {
    const { src, res } = rendered.get(enc) || { src: _safeDecode(enc), res: null }
    if (res && res.svg) return `<div class="x-mermaid">${res.svg}</div>`
    const note = res && res.error ? `Diagram not rendered: ${esc(res.error)}` : 'Diagram source (not rendered).'
    return `<p class="x-note">${note}</p>${codeCard(src, null, 'Mermaid')}`
  })
}
