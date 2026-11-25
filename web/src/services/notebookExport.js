/**
 * Notebook export/save builders — pure functions that turn a notebook
 * (tab metadata + cells) into downloadable content. Extracted from Notebook.jsx
 * to keep the component focused on rendering/state.
 *
 * Each builder returns { content, mime, filename } (the HTML builders are async:
 * mermaid renders to SVG at export time). Call downloadTextFile() with that result
 * to trigger the browser download.
 *
 * Node-loadable (`.js` imports, injected sanitizer/mermaid renderer) so the
 * exporters are unit-tested under `node --test`.
 */
import { appShows, deriveDisplayVar, collectInputs } from './appViewModel.js'
import { parseTablePayload, tablePayloadToHtml, tablePayloadToMarkdown } from './dfCellFormat.js'
import { cellOutputItems, isEnvelope, isMultiOutput, stripEnvelopeImages } from './cellOutputs.js'
import { cellBadgeList } from '../utils/cellBadge.js'
import { formatDuration } from '../utils/formatDuration.js'
import { hashString } from './chatTurns.js'
import { PLOTLY_LIGHT_RETHEME, PLOTLY_DARK_RETHEME } from './plotlyTheme.js'
import {
  esc as _esc, exportCss, readExportTheme, defaultExportTheme, codeCard,
  renderExportMarkdown, stripControls, inlineMermaid,
} from './exportRender.js'
import { sanitizeMarkdown } from './sanitize.js'

/** Trigger a browser download for text content. */
export function downloadTextFile({ content, mime, filename }) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// ── Persisted cell fields ─────────────────────────────────────────────────────

/**
 * Every cell field the notebook persists (proxy/storage/sqlite_db.py
 * `_row_to_cell`, mirrored by useTabsPersistence `buildNotebookSavePayload`),
 * minus `imageS3Key` (it belongs to the source notebook; the server validates it)
 * plus `image` (the chart as a data URL; exports resolve persisted charts first).
 * The native format round-trips exactly these.
 */
export const PERSISTED_CELL_FIELDS = Object.freeze([
  'id', 'type', 'code', 'output', 'error', 'html', 'image', 'outputVariable',
  'executionNumber', 'executionTime', 'lastExecutedCode',
  'aiExplanation', 'aiExplanationEdited', 'aiExplanationSource', 'generated',
  'peakMemMb', 'peakCpuPct', 'executedAt',
])

const _num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const _str = (v) => (v == null || v === '' ? null : String(v))
const _type = (t) => (t === 'sql' || t === 'markdown' ? t : 'code')

/**
 * The persisted projection of a cell: an explicit pick of PERSISTED_CELL_FIELDS
 * with the same defaults the save payload uses. Never spreads the cell, so
 * transient web state (errorLocals, tracebacks, lint, status) can't leak out.
 */
export function persistedCell(c) {
  const cell = c || {}
  return {
    id: cell.id == null ? null : String(cell.id),
    type: _type(cell.type),
    code: String(cell.code ?? ''),
    output: _str(cell.output),
    error: _str(cell.error),
    html: _str(cell.html),
    image: _str(cell.image),
    outputVariable: _str(cell.outputVariable),
    executionNumber: _num(cell.executionNumber),
    executionTime: _num(cell.executionTime),
    lastExecutedCode: cell.lastExecutedCode == null ? null : String(cell.lastExecutedCode),
    aiExplanation: _str(cell.aiExplanation),
    aiExplanationEdited: !!cell.aiExplanationEdited,
    aiExplanationSource: _str(cell.aiExplanationSource),
    generated: _str(cell.generated),
    peakMemMb: _num(cell.peakMemMb),
    peakCpuPct: _num(cell.peakCpuPct),
    executedAt: _str(cell.executedAt),
  }
}

const _fileBase = (name, sep) => String(name || 'Notebook').replace(/\s+/g, sep)

/** Native .notebook.json (our own format; round-trips every persisted field). */
export function buildNativeNotebook(tab, cells, { now = new Date() } = {}) {
  const notebook = {
    formatVersion: 2,
    name: tab.name,
    description: tab.description || '',
    tag: tab.tag || null,
    // Informational only: an import never binds to this VM.
    microvmId: tab.microvmId || null,
    savedAt: now.toISOString(),
    cells: (cells || []).map(persistedCell),
    // The stored dependency DAG document (informational; importers ignore it —
    // the proxy rebuilds the DAG from the imported cells).
    dag: tab._dag || null,
  }
  return {
    content: JSON.stringify(notebook, null, 2),
    mime: 'application/json',
    filename: `${_fileBase(tab.name, '_')}.notebook.json`,
  }
}

// ── HTML export ───────────────────────────────────────────────────────────────

const TRUST_LABEL = { green: 'Trusted', amber: 'Review', red: 'Upstream error' }
const _fmtNow = (d) => d.toLocaleString()
const _sanitizeDefault = (h) => { try { return sanitizeMarkdown(h) } catch { return h } }
const _themeOf = (opt) => (opt && opt.vars ? opt : opt && opt.name ? defaultExportTheme(opt.name)
  : typeof opt === 'string' ? defaultExportTheme(opt) : readExportTheme())
const _retheme = (name) => (name === 'light' ? PLOTLY_LIGHT_RETHEME : name === 'dark' ? PLOTLY_DARK_RETHEME : '')

// Static run metrics, worded like the cell's run footer (CellOutput.jsx).
function _runMeta(cell) {
  const parts = []
  if (cell.executionTime != null) parts.push(`Executed in ${formatDuration(cell.executionTime)}`)
  if (typeof cell.peakMemMb === 'number' && cell.peakMemMb > 0) {
    parts.push(`peak ${cell.peakMemMb >= 1024 ? (cell.peakMemMb / 1024).toFixed(1) + ' GB' : Math.round(cell.peakMemMb) + ' MB'}`)
  }
  if (typeof cell.peakCpuPct === 'number' && cell.peakCpuPct > 0) parts.push(`${Math.round(cell.peakCpuPct)}% CPU`)
  return parts.join(' · ')
}

const OFFLINE_NOTE = '<p class="x-plotly-offline">Interactive chart: loads plotly.js from the internet.</p>'
// Hides the offline note once plotly.js has loaded (the file stays static otherwise).
const OFFLINE_SCRIPT = '<script>window.addEventListener("load",function(){if(window.Plotly){'
  + 'document.querySelectorAll(".x-plotly-offline").forEach(function(n){n.style.display="none"})}})</script>'

// One cell output (HTML payload) as static export HTML.
function _htmlOutput(htmlStr, theme, sanitize) {
  if (!htmlStr) return ''
  // A multi-output envelope that did not parse (corrupt): never dump its raw JSON.
  if (isEnvelope(htmlStr)) return `<div class="table-scroll"><em>(table unavailable)</em></div>`
  if (htmlStr.includes('data-plotly')) {
    // Kernel Plotly snippet (references the plotly CDN) + the app's re-theme script.
    return `<div class="x-plotly">${htmlStr}${_retheme(theme.name)}${OFFLINE_NOTE}</div>`
  }
  const tbl = parseTablePayload(htmlStr)
  if (tbl) return `<div class="table-scroll">${tablePayloadToHtml(tbl)}</div>`
  // Looks like a DataFrame payload but failed to parse (corrupt/truncated) — emit a
  // placeholder rather than dumping the raw JSON string into the export.
  if (htmlStr.includes('"__df_table__"')) return `<div class="table-scroll"><em>(table unavailable)</em></div>`
  return `<div class="table-scroll">${stripControls(sanitize(htmlStr))}</div>`
}

// Multi-output cell (several tables / charts / images, text in between): EVERY item
// in order, expanded and static (no buttons, no collapsing), each table / chart /
// image under a small plain caption. Text items are the printed output, so the
// cell's `output` is not emitted again. Returns { html, hasPlotly }.
const MULTI_CSS = '.x-out-caption{margin:10px 0 4px;font-size:12px;font-weight:600;color:var(--text-tertiary)}'
  + '.x-out-caption:first-child{margin-top:0}.x-out>img{max-width:100%}'
function _multiOutputHtml(cell, theme, badge) {
  let html = ''
  let hasPlotly = false
  for (const it of cellOutputItems(cell)) {
    if (it.t === 'text') {
      html += `<pre>${_esc(it.text)}</pre>`
      continue
    }
    html += `<p class="x-out-caption">${_esc(it.caption)}</p>`
    if (it.t === 'table') html += `<div class="table-scroll">${tablePayloadToHtml(it.payload)}</div>`
    else if (it.t === 'plotly') {
      hasPlotly = true
      html += `<div class="x-plotly">${it.html}${_retheme(theme.name)}${OFFLINE_NOTE}</div>`
    } else if (it.src) html += `<img src="${_esc(it.src)}" alt="${_esc(it.caption)} of ${_esc(badge)}"/>`
    else html += '<div class="table-scroll"><em>(image unavailable)</em></div>'
  }
  return { html, hasPlotly }
}
function _paramsStrip(cell) {
  const inputs = collectInputs([cell])
  if (!inputs.length) return ''
  return `<div class="x-params"><span class="x-params-title">Parameters</span>`
    + inputs.map(i => `<span class="x-param"><span class="x-param-label">${_esc(i.label)}</span> = `
      + `<span class="x-param-value">${_esc(i.displayValue ?? i.value)}</span></span>`).join('')
    + `</div>`
}

const NB_CSS = `
.x-cell{margin:16px 0;border:1px solid var(--border-default);border-radius:10px;background:var(--surface-1);overflow:hidden}
.x-cell-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border-subtle);font-size:12px;color:var(--text-tertiary)}
.x-cell-head .x-exec{font-family:var(--font-mono)}
.x-cell-head .x-run{margin-left:auto}
.x-cell>.x-code{margin:0;border:0;border-radius:0}
.x-params{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;padding:8px 12px;border-bottom:1px solid var(--border-subtle);font-size:12px}
.x-params-title{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-tertiary)}
.x-param-label{font-family:var(--font-mono);color:var(--text-secondary)}
.x-param-value{font-weight:600;color:var(--text-primary)}
.x-out{padding:10px 12px;border-top:1px solid var(--border-subtle);background:var(--cell-output-bg)}
.x-out>pre{color:var(--text-primary)}
.x-out>pre.x-error{color:var(--accent-danger)}
.x-ai-note{padding:8px 12px;border-top:1px solid var(--border-subtle);font-size:13px;color:var(--text-secondary)}
.x-ai-note>:first-child{margin-top:0}.x-ai-note>:last-child{margin-bottom:0}
.x-md-cell{padding:2px 4px}
footer{text-align:center;padding:24px 0 0;color:var(--text-tertiary);font-size:11px;border-top:1px solid var(--border-subtle);margin-top:32px}
`

const _footer = (now) => `<footer><strong>Sandboxed Notebook on AWS</strong><br>Developed by the AWS Startup SA Team<br>&copy; ${now.getFullYear()} Amazon Web Services, Inc.</footer>`

const _doc = (theme, title, extraCss, body) => `<!doctype html><html lang="en" data-theme="${theme.name}"><head><meta charset="UTF-8">`
  + `<meta name="viewport" content="width=device-width, initial-scale=1"><title>${_esc(title)}</title>`
  + `<style>${exportCss(theme)}${extraCss}</style></head><body><div class="x-doc">${body}</div></body></html>`

/**
 * Self-contained, static HTML export of the workbook. opts:
 *  theme          export theme (default: the user's current theme, readExportTheme)
 *  sanitize       HTML sanitizer (default DOMPurify sanitizeMarkdown)
 *  renderMermaid  src → {svg}|{error}|null (markdown.js renderMermaidSvg)
 *  trustByCell    Map(cellId → 'green'|'amber'|'red') (lineageTrustLevels)
 *  now            Date for the "Generated" line
 */
export async function buildNotebookHTML(tab, cells, opts = {}) {
  const { sanitize = _sanitizeDefault, renderMermaid = null, trustByCell = null, now = new Date(), formatTime = _fmtNow } = opts
  const theme = _themeOf(opts.theme)
  const nbName = tab.name || 'Notebook'
  const list = cells || []
  let body = `<h1>${_esc(nbName)}</h1>`
  if (tab.description) body += `<p class="x-desc">${_esc(tab.description)}</p>`
  body += `<p class="x-meta">Generated: ${_esc(formatTime(now))} · ${list.length} cells</p>`
  const badges = cellBadgeList(list)  // PY-n / SQL-n / MD-n, aligned by position
  let hasPlotly = false
  let hasMulti = false
  for (let i = 0; i < list.length; i++) {
    const cell = list[i]
    const type = _type(cell.type)
    // Markdown cells render as prose (mermaid diagrams as SVG).
    if (type === 'markdown') {
      const md = await inlineMermaid(renderExportMarkdown(cell.code || '', { sanitize }), renderMermaid)
      body += `<section class="x-md-cell">${md}</section>`
      continue
    }
    const trust = trustByCell && typeof trustByCell.get === 'function' ? trustByCell.get(cell.id) : null
    const run = _runMeta(cell)
    body += `<section class="x-cell" data-lang="${type}">`
    body += `<div class="x-cell-head"><span class="x-badge" data-lang="${type}">${_esc(badges[i])}</span>`
    if (cell.executionNumber != null) body += `<span class="x-exec">[${_esc(cell.executionNumber)}]</span>`
    if (cell.outputVariable) body += `<span class="x-outvar">→ ${_esc(cell.outputVariable)}</span>`
    if (trust && TRUST_LABEL[trust]) body += `<span class="x-trust x-trust-${trust}">${TRUST_LABEL[trust]}</span>`
    if (run) body += `<span class="x-run">${_esc(run)}</span>`
    body += `</div>`
    body += _paramsStrip(cell)
    body += codeCard(cell.code || '', type)
    if (isMultiOutput(cell)) {
      hasMulti = true
      const multi = _multiOutputHtml(cell, theme, badges[i])
      if (multi.hasPlotly) hasPlotly = true
      body += `<div class="x-out">${multi.html}`
      if (cell.error) body += `<pre class="x-error">${_esc(cell.error)}</pre>`
      body += `</div>`
    } else if (cell.output || cell.html || cell.image || cell.error) {
      body += `<div class="x-out">`
      if (cell.output) body += `<pre>${_esc(cell.output)}</pre>`
      if (cell.html) {
        if (cell.html.includes('data-plotly')) hasPlotly = true
        body += _htmlOutput(cell.html, theme, sanitize)
      }
      if (cell.image) body += `<img src="${_esc(cell.image)}" alt="Chart output of ${_esc(badges[i])}"/>`
      if (cell.error) body += `<pre class="x-error">${_esc(cell.error)}</pre>`
      body += `</div>`
    }
    if (cell.aiExplanation) {
      body += `<div class="x-ai-note">${await inlineMermaid(renderExportMarkdown(cell.aiExplanation, { sanitize }), renderMermaid)}</div>`
    }
    body += `</section>`
  }
  body += _footer(now)
  if (hasPlotly) body += OFFLINE_SCRIPT
  return {
    content: _doc(theme, nbName, hasMulti ? NB_CSS + MULTI_CSS : NB_CSS, stripControls(body)),
    mime: 'text/html',
    filename: `${_fileBase(nbName, '-')}.html`,
  }
}

// ── Markdown export ───────────────────────────────────────────────────────────

/** A fenced block whose fence is longer than any backtick run inside the code. */
export function fenced(code, lang = '') {
  const s = String(code ?? '')
  const longest = (s.match(/`+/g) || []).reduce((m, r) => Math.max(m, r.length), 0)
  const f = '`'.repeat(Math.max(3, longest + 1))
  return `${f}${lang}\n${s}${s.endsWith('\n') ? '' : '\n'}${f}`
}

// A cell note as a Markdown blockquote. Every line of a multi-line note is its own
// `> ` line (blank lines as `>`), so headings, lists and code fences stay inside the
// quote. Only a single-line note is italicised (italics cannot span blocks). The
// ✨ marker sits on the first line; when that line starts a block (heading, list,
// fence, quote, table) the marker gets a line of its own so the block still renders.
const _BLOCK_START = /^\s{0,3}(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|`{3,}|~{3,}|\||---|\*\*\*)/
export function noteBlockquote(note) {
  const text = String(note ?? '').replace(/\r\n?/g, '\n').replace(/^\n+/, '').replace(/\s+$/, '')
  if (!text) return ''
  const lines = text.split('\n')
  if (lines.length === 1) return `> \u2728 *${lines[0].trim()}*`
  const q = (l) => (l.trim() === '' ? '>' : `> ${l}`)
  if (_BLOCK_START.test(lines[0])) return ['> \u2728', '>', ...lines.map(q)].join('\n')
  return [`> \u2728 ${lines[0]}`, ...lines.slice(1).map(q)].join('\n')
}
// Multi-output cell as Markdown: every item in order, each table / chart / image
// under its plain caption (tables as pipe tables, charts and images as one-line notes).
function _multiOutputMarkdown(cell, imageBase) {
  let md = ''
  for (const it of cellOutputItems(cell)) {
    if (it.t === 'text') {
      md += `${fenced(it.text)}\n\n`
      continue
    }
    md += `*${it.caption}*\n\n`
    if (it.t === 'table') md += `${tablePayloadToMarkdown(it.payload)}\n\n`
    else if (it.t === 'plotly') md += `*(interactive chart — view HTML export)*\n\n`
    else md += `![${it.caption}](${imageBase}-${it.ordinal}.png)\n\n`
  }
  return md
}
/** Markdown export. */
export function buildNotebookMarkdown(tab, cells, { now = new Date(), formatTime = _fmtNow } = {}) {
  const nbName = tab.name || 'Notebook'
  const list = cells || []
  let md = `# ${nbName}\n\n`
  if (tab.description) md += `> ${tab.description}\n\n`
  md += `*Generated: ${formatTime(now)} · ${list.length} cells*\n\n---\n\n`
  const badges = cellBadgeList(list)  // PY-n / SQL-n / MD-n, aligned by position
  list.forEach((cell, i) => {
    const type = _type(cell.type)
    // Markdown cells emit their content verbatim (```mermaid stays fenced).
    if (type === 'markdown') {
      md += `${cell.code || ''}\n\n---\n\n`
      return
    }
    md += `## ${badges[i]}${cell.executionNumber != null ? ` [${cell.executionNumber}]` : ''}\n\n`
    const run = _runMeta(cell)
    if (run) md += `*${run}*\n\n`
    const inputs = collectInputs([cell])
    if (inputs.length) {
      md += `**Parameters:**\n\n`
      for (const p of inputs) md += `- **${p.label}:** ${p.displayValue ?? p.value}\n`
      md += `\n`
    }
    md += `${fenced(cell.code || '', type === 'sql' ? 'sql' : 'python')}\n\n`
    if (isMultiOutput(cell)) {
      md += `**Output:**\n\n${_multiOutputMarkdown(cell, `plot-${badges[i]}`)}`
    } else if (cell.output) md += `**Output:**\n\n${fenced(cell.output)}\n\n`
    if (!isMultiOutput(cell) && cell.html) {
      const _tbl = parseTablePayload(cell.html)
      md += _tbl ? tablePayloadToMarkdown(_tbl) + '\n\n'
        : cell.html.includes('data-plotly') ? `*(interactive chart — view HTML export)*\n\n` : ''
    }
    if (!isMultiOutput(cell) && cell.image) md += `![Plot](plot-${badges[i]}.png)\n\n`
    if (cell.error) md += `**Error:**\n\n${fenced(cell.error)}\n\n`
    if (cell.aiExplanation) md += `${noteBlockquote(cell.aiExplanation)}\n\n`
    md += `---\n\n`
  })
  md += `\n*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*\n`
  return {
    content: md,
    mime: 'text/markdown',
    filename: `${_fileBase(nbName, '-')}.md`,
  }
}

// ── Jupyter .ipynb ────────────────────────────────────────────────────────────

const NBF_ID_RE = /^[a-zA-Z0-9-_]{1,64}$/
// nbformat multiline string: list of lines, each but the last keeping its '\n'.
const _lines = (s) => {
  const str = String(s ?? '')
  if (!str) return []
  return str.split('\n').map((l, i, a) => (i < a.length - 1 ? l + '\n' : l)).filter((l, i, a) => !(i === a.length - 1 && l === ''))
}

// "Traceback ... \nValueError: bad" → { ename: 'ValueError', evalue: 'bad' }.
function _errorParts(text) {
  const lines = String(text || '').split('\n').map(l => l.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = lines[i].match(/^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit)):\s*(.*)$/)
    if (m) return { ename: m[1], evalue: m[2] }
  }
  return { ename: 'Error', evalue: lines[lines.length - 1] || '' }
}

/**
 * Jupyter .ipynb (nbformat 4.5). Standard fields carry the code and outputs;
 * every MicroVM Notebook extra lives under `metadata.microvm_notebook` (notebook
 * and cell level) so our importer restores the cell exactly. SQL cells are code
 * cells with a `%%sql` first line (what the importer and Jupyter SQL magics expect).
 */
export function buildIPYNB(tab, cells) {
  const nbName = tab.name || 'Notebook'
  const used = new Set()
  const cellId = (raw) => {
    let id = raw != null && NBF_ID_RE.test(String(raw)) ? String(raw) : `c-${hashString(String(raw ?? ''))}`
    let n = 1
    const base = id.slice(0, 60)
    while (used.has(id)) id = `${base}-${n++}`
    used.add(id)
    return id
  }
  const ipynb = {
    nbformat: 4,
    nbformat_minor: 5,
    metadata: {
      kernelspec: { display_name: 'Python 3', language: 'python', name: 'python3' },
      language_info: { name: 'python', version: '3.11' },
      microvm_notebook: {
        format_version: 1,
        name: tab.name || '',
        description: tab.description || '',
        tag: tab.tag || null,
        // Informational (the proxy rebuilds the DAG from the cells on import).
        dag: tab._dag || null,
      },
    },
    cells: (cells || []).map((raw, i) => {
      const c = persistedCell(raw)
      const id = cellId(c.id ?? `cell-${i}`)
      // Extras: every persisted field except code/output/image (those are standard).
      const extras = { ...c }
      delete extras.code
      delete extras.output
      delete extras.image
      // The images of a multi-output cell travel as image/png outputs; keep only their S3 keys here.
      extras.html = stripEnvelopeImages(extras.html)
      const metadata = { microvm_notebook: extras }
      if (c.type === 'markdown') {
        return { cell_type: 'markdown', id, metadata, source: _lines(c.code) }
      }
      const source = _lines(c.type === 'sql' ? `%%sql\n${c.code}` : c.code)
      const outputs = []
      if (isMultiOutput(c)) {
        // One output per item, in order: stream / display_data (html or image/png).
        for (const it of cellOutputItems(c)) {
          if (it.t === 'text') outputs.push({ output_type: 'stream', name: 'stdout', text: _lines(it.text) })
          else if (it.t === 'table') {
            outputs.push({ output_type: 'display_data', data: { 'text/html': _lines(tablePayloadToHtml(it.payload)), 'text/plain': ['<DataFrame>'] }, metadata: {} })
          } else if (it.t === 'plotly') {
            outputs.push({ output_type: 'display_data', data: { 'text/html': _lines(it.html), 'text/plain': ['<Plotly figure>'] }, metadata: {} })
          } else {
            const m = String(it.src || '').match(/^data:(image\/[a-zA-Z0-9.+-]+);base64,(.*)$/s)
            // `image` = this image's position among the cell's images, so an import can put it back.
            if (m) outputs.push({ output_type: 'display_data', data: { [m[1]]: m[2], 'text/plain': ['<Figure>'] }, metadata: { microvm_notebook: { image: it.ordinal } } })
          }
        }
      } else if (c.output) outputs.push({ output_type: 'stream', name: 'stdout', text: _lines(c.output) })
      if (!isMultiOutput(c) && c.html) {
        const tbl = parseTablePayload(c.html)
        const html = tbl ? tablePayloadToHtml(tbl) : c.html
        const plain = tbl ? '<DataFrame>' : c.html.includes('data-plotly') ? '<Plotly figure>' : '<HTML output>'
        outputs.push({ output_type: 'display_data', data: { 'text/html': _lines(html), 'text/plain': [plain] }, metadata: {} })
      }
      if (!isMultiOutput(c) && c.image) {
        const m = String(c.image).match(/^data:(image\/[a-zA-Z0-9.+-]+);base64,(.*)$/s)
        if (m) outputs.push({ output_type: 'display_data', data: { [m[1]]: m[2], 'text/plain': ['<Figure>'] }, metadata: {} })
      }
      if (c.error) {
        const { ename, evalue } = _errorParts(c.error)
        outputs.push({ output_type: 'error', ename, evalue, traceback: String(c.error).split('\n') })
      }
      return {
        cell_type: 'code',
        id,
        metadata,
        source,
        outputs,
        execution_count: Number.isInteger(c.executionNumber) && c.executionNumber >= 0 ? c.executionNumber : null,
      }
    }),
  }
  return {
    content: JSON.stringify(ipynb, null, 1),
    mime: 'application/json',
    filename: `${_fileBase(nbName, '_')}.ipynb`,
  }
}

// ============================================================
// App-view exports — the consumer projection (inputs + rendered markdown/charts/
// tables, no code). Used when exporting from App view instead of the workbook.
// ============================================================

const APP_HTML_STYLE = `
:root{--accent:#5b9fff;--accent-soft:rgba(91,159,255,.14);--accent-border:rgba(91,159,255,.38);--bg:#0f1017;--panel:#16161c;--text:#f2f4ff;--text-dim:#bfc4dc;--border:rgba(255,255,255,.08)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.55}
.app-doc{max-width:920px;margin:0 auto;padding:32px 28px 80px}
h1{font-size:26px;margin:0 0 6px}
.desc{color:var(--text-dim);margin:0 0 24px;font-size:14px}
.gen-meta{color:#5c6280;font-size:12px;margin:0 0 22px}
.inputs{background:linear-gradient(180deg,var(--accent-soft),transparent 62%),var(--panel);border:1px solid var(--accent-border);border-radius:12px;padding:16px 18px;margin-bottom:26px;box-shadow:0 0 22px rgba(91,159,255,.16),0 12px 30px rgba(0,0,0,.4)}
.inputs-title{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.inputs-title::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px 1px rgba(91,159,255,.7)}
.inputs-grid{display:flex;flex-wrap:wrap;gap:12px}
.input{display:flex;flex-direction:column;gap:3px;background:rgba(91,159,255,.06);border:1px solid var(--accent-border);border-radius:8px;padding:8px 13px;min-width:120px}
.input-label{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:600;color:var(--text-dim);letter-spacing:.02em}
.input-value{font-size:17px;font-weight:700;color:var(--accent);line-height:1.1}
.block{margin:0 0 26px}
.block.md{color:var(--text)}
.var{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;font-weight:700;color:var(--accent);background:var(--accent-soft);border:1px solid var(--accent-border);border-radius:6px;padding:3px 10px;margin-bottom:10px;box-shadow:0 0 12px rgba(91,159,255,.16)}
h2{border-bottom:1px solid var(--accent-border);padding-bottom:6px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:4px 0}
th,td{border:1px solid var(--border);padding:6px 10px;text-align:left}
th{background:rgba(255,255,255,.04);color:var(--text)}
.table-scroll{max-height:420px;overflow:auto;border:1px solid var(--border);border-radius:8px;margin:4px 0}
.table-scroll table{margin:0}
.table-scroll thead th{position:sticky;top:0;z-index:1;background:#1c1d26;box-shadow:inset 0 -1px 0 var(--border)}
.out-text{background:#0b0c12;border:1px solid var(--border);border-radius:8px;padding:12px;overflow-x:auto;font-size:13px;color:var(--text)}
.out-img img{max-width:100%;border-radius:8px}
img{max-width:100%}
.x-mermaid{margin:10px 0;overflow-x:auto;text-align:center}
.x-mermaid svg{max-width:100%;height:auto}
.x-note{color:var(--text-dim);font-size:12px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);color:#5c6280;font-size:12px;text-align:center}
`

// Wrap a DataFrame table in a bounded, scrollable container (≈15 rows + sticky
// header) to match the in-app table. Plotly snippets are left as-is (not tables).
function _wrapHtmlOutput(htmlStr) {
  if (!htmlStr) return ''
  // A multi-output envelope that did not parse (corrupt): never dump its raw JSON.
  if (isEnvelope(htmlStr)) return `<div class="table-scroll"><em>(table unavailable)</em></div>`
  if (htmlStr.includes('data-plotly')) return htmlStr
  const tbl = parseTablePayload(htmlStr)
  if (tbl) return `<div class="table-scroll">${tablePayloadToHtml(tbl)}</div>`
  // Looks like a DataFrame payload but failed to parse (corrupt/truncated) — emit a
  // placeholder rather than dumping the raw JSON string into the export.
  if (htmlStr.includes('"__df_table__"')) return `<div class="table-scroll"><em>(table unavailable)</em></div>`
  return `<div class="table-scroll">${htmlStr}</div>`
}

function _appOutputHTML(cell) {
  if (isMultiOutput(cell)) {
    // Every item in order, expanded and static, each table / chart / image under a caption.
    return cellOutputItems(cell).map((it) => {
      if (it.t === 'text') return `<pre class="out-text">${_esc(it.text)}</pre>`
      const cap = `<p class="x-note">${_esc(it.caption)}</p>`
      if (it.t === 'table') return `${cap}<div class="table-scroll">${tablePayloadToHtml(it.payload)}</div>`
      if (it.t === 'plotly') return `${cap}${it.html}`
      return it.src ? `${cap}<div class="out-img"><img src="${_esc(it.src)}" alt="${_esc(it.caption)}"/></div>`
        : `${cap}<div class="table-scroll"><em>(image unavailable)</em></div>`
    }).join('')
  }
  if (cell.image) return `<div class="out-img"><img src="${cell.image}" alt="chart"/></div>`
  if (cell.html) return _wrapHtmlOutput(cell.html)   // DataFrame table (scrollable) or Plotly snippet
  if (cell.output) return `<pre class="out-text">${_esc(cell.output)}</pre>`
  return ''
}

/** App view → self-contained styled HTML (inputs + rendered content, no code). */
export async function buildAppHTML(tab, cells, { sanitize = _sanitizeDefault, renderMermaid = null, now = new Date() } = {}) {
  const nbName = tab.name || 'App'
  const inputs = collectInputs(cells)
  const presentation = cells.filter(appShows)
  let body = `<h1>${_esc(nbName)}</h1>`
  if (tab.description) body += `<p class="desc">${_esc(tab.description)}</p>`
  body += `<p class="gen-meta">Generated: ${_esc(now.toLocaleString())}</p>`
  if (inputs.length) {
    body += `<div class="inputs"><div class="inputs-title">Inputs</div><div class="inputs-grid">`
    for (const inp of inputs) {
      body += `<div class="input"><span class="input-label">${_esc(inp.label)}</span><span class="input-value">${_esc(inp.displayValue ?? inp.value)}</span></div>`
    }
    body += `</div></div>`
  }
  for (const cell of presentation) {
    if (cell.type === 'markdown') {
      body += `<div class="block md">${await inlineMermaid(renderExportMarkdown(cell.code || '', { sanitize }), renderMermaid)}</div>`
      continue
    }
    const varName = deriveDisplayVar(cell)
    body += `<div class="block">${varName ? `<div class="var">${_esc(varName)}</div>` : ''}${_appOutputHTML(cell)}</div>`
  }
  body += `<footer><strong>Sandboxed Notebook on AWS</strong><br>Developed by the AWS Startup SA Team<br>&copy; ${now.getFullYear()} Amazon Web Services, Inc.</footer>`
  // Markdown code cards reuse the shared static card styles (this app page is dark:
  // the copied dark tokens), then the app page's own rules on top.
  const html = `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${_esc(nbName)}</title><style>${exportCss(defaultExportTheme('dark'))}${APP_HTML_STYLE}</style></head><body><div class="app-doc">${stripControls(body)}</div></body></html>`
  return {
    content: html,
    mime: 'text/html',
    filename: `${_fileBase(nbName, '-')}-app.html`,
  }
}

/** App view → Markdown (inputs + markdown/metrics; charts/tables noted). */
export function buildAppMarkdown(tab, cells) {
  const nbName = tab.name || 'App'
  const inputs = collectInputs(cells)
  const presentation = cells.filter(appShows)
  let md = `# ${nbName}\n\n`
  if (tab.description) md += `> ${tab.description}\n\n`
  md += `*Generated: ${new Date().toLocaleString()}*\n\n`
  if (inputs.length) {
    md += `## Inputs\n\n`
    for (const inp of inputs) md += `- **${inp.label}:** ${inp.displayValue ?? inp.value}\n`
    md += `\n`
  }
  for (const cell of presentation) {
    if (cell.type === 'markdown') {
      md += `${cell.code || ''}\n\n`
      continue
    }
    const varName = deriveDisplayVar(cell)
    if (varName) md += `### ${varName}\n\n`
    if (isMultiOutput(cell)) {
      for (const it of cellOutputItems(cell)) {
        if (it.t === 'text') md += `${fenced(it.text)}\n\n`
        else if (it.t === 'table') md += `*${it.caption}*\n\n${tablePayloadToMarkdown(it.payload)}\n\n`
        else md += `*${it.caption}*\n\n_(${it.t === 'plotly' ? 'interactive chart' : 'chart'} — see HTML export)_\n\n`
      }
    } else if (cell.image) {
      md += `_(chart — see HTML export for the rendered image)_\n\n`
    } else if (cell.html) {
      if (cell.html.includes('data-plotly')) {
        md += `_(interactive chart — see HTML export)_\n\n`
      } else {
        const _tbl = parseTablePayload(cell.html)
        md += _tbl ? tablePayloadToMarkdown(_tbl) + '\n\n' : `_(table — see HTML export for full rendering)_\n\n`
      }
    } else if (cell.output) {
      md += `${fenced(cell.output)}\n\n`
    }
  }
  md += `\n*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*\n`
  return {
    content: md,
    mime: 'text/markdown',
    filename: `${_fileBase(nbName, '-')}-app.md`,
  }
}
