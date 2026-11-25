// Shared cell-output export (HTML / Markdown) — SINGLE SOURCE OF TRUTH.
//
// Used by both the inline notebook cell (Cell.jsx) and the focus-mode overlay
// (CellFocusOverlay.jsx) so the two export paths can't drift. Each function builds
// a self-contained document from the cell's output (text / table HTML / image) and
// triggers a download. Table payloads are rendered via the same dfCellFormat
// helpers the rest of the app uses; a Plotly chart HTML passes through for HTML
// export and is noted as "export as HTML" in Markdown.
//
// A multi-output cell (several tables / charts / images with text in between, see
// cellOutputs.js) exports ALL of its items in order, expanded and static, each
// table / chart / image under a plain caption. The Copy / Export buttons sit once
// on the cell's OUTPUT bar, so they act on the whole output (every item), not on one.
// The build* functions are pure (text out) so they can be unit-tested.
import { parseTablePayload, tablePayloadToHtml, tablePayloadToMarkdown } from './dfCellFormat.js'
import { hasImageOutput } from './cellOutput.js'
import { cellOutputItems, isMultiOutput } from './cellOutputs.js'

function downloadBlob(content, mime, filename) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

const slug = (name) => (name || 'notebook').replace(/\s+/g, '-')

const HTML_STYLE = `body{font-family:system-ui;padding:20px;max-width:900px;margin:0 auto}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}th{background:#f5f5f5;font-weight:600}pre{background:#f5f5f5;padding:12px;border-radius:4px;overflow-x:auto;font-size:13px}details{margin-bottom:16px;border:1px solid #e0e0e0;border-radius:4px}summary{padding:8px 12px;cursor:pointer;font-weight:600;font-size:13px;color:#555}details pre{margin:0;border-radius:0 0 4px 4px;border-top:1px solid #e0e0e0}img{max-width:100%}h3{color:#333;margin-top:0}`
const HTML_MULTI_STYLE = '.cap{margin:16px 0 4px;font-size:12px;font-weight:600;color:#555}'
const escHtml = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** The single-cell HTML export document (text). `now` is injectable for tests. */
export function buildCellOutputHtml(cell, notebookName, badge, now = new Date()) {
  const multi = isMultiOutput(cell)
  let html = `<html><head><meta charset="UTF-8"><title>${notebookName || 'Notebook'} — ${badge}</title><style>${HTML_STYLE}${multi ? HTML_MULTI_STYLE : ''}</style></head><body>`
  html += `<h3>${notebookName || 'Notebook'} — ${badge} Output</h3>`
  html += `<p style="color:#888;font-size:12px">Generated: ${now.toLocaleString()}</p>`
  html += `<details><summary>Code</summary><pre>${(cell.code || '').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre></details>`
  if (multi) {
    // Every item in order, expanded and static, rich ones under a plain caption.
    for (const it of cellOutputItems(cell)) {
      if (it.t === 'text') { html += `<pre>${escHtml(it.text)}</pre>`; continue }
      html += `<p class="cap">${escHtml(it.caption)}</p>`
      if (it.t === 'table') html += tablePayloadToHtml(it.payload)
      else if (it.t === 'plotly') html += it.html  // plotly html passes through
      else if (it.src) html += `<img src="${escHtml(it.src)}" alt="${escHtml(it.caption)}"/>`
      else html += '<p><em>(image unavailable)</em></p>'
    }
  } else {
    if (cell.output) html += `<pre>${cell.output}</pre>`
    if (cell.html) {
      const tbl = parseTablePayload(cell.html)
      html += tbl ? tablePayloadToHtml(tbl) : cell.html  // plotly html passes through
    }
    if (cell.image) html += `<img src="${cell.image}" alt="Plot output"/>`
  }
  html += `<hr style="margin-top:32px;border:none;border-top:1px solid #e0e0e0"><footer style="text-align:center;padding:12px;color:#888;font-size:11px"><strong>Sandboxed Notebook on AWS</strong><br>Developed by the AWS Startup SA Team<br>&copy; ${now.getFullYear()} Amazon Web Services, Inc.</footer>`
  html += '</body></html>'
  return html
}

export function exportCellOutputHtml(cell, notebookName, badge) {
  const filename = `${slug(notebookName)}-${badge}-output.html`
  downloadBlob(buildCellOutputHtml(cell, notebookName, badge), 'text/html', filename)
}

// Copy the ENTIRE output of a cell to the clipboard as plain text — text output,
// a table (rendered as a Markdown pipe table), and a note for an image/chart. This
// is what the single OUTPUT-header "Copy" uses, so one button covers every output
// kind (previously it copied only cell.output and missed tables/images). Falls back
// to a hidden-textarea copy in non-secure contexts. Returns a Promise<boolean>.
/** The text copyCellOutput puts on the clipboard ('' when there is nothing to copy). */
export function buildCellOutputCopyText(cell) {
  const parts = []
  if (isMultiOutput(cell)) {
    // Every item in order: text, each table as a captioned Markdown table, charts
    // and images as one-line notes (they have no text form).
    for (const it of cellOutputItems(cell)) {
      if (it.t === 'text') parts.push(it.text)
      else if (it.t === 'table') parts.push(`${it.caption}\n\n${tablePayloadToMarkdown(it.payload)}`)
      else parts.push(`(${it.caption} — use Export ▸ HTML to save it)`)
    }
    return parts.join('\n\n')
  }
  if (cell.output) parts.push(cell.output)
  if (cell.html) {
    const tbl = parseTablePayload(cell.html)
    if (tbl) parts.push(tablePayloadToMarkdown(tbl))
    // A Plotly chart has no meaningful text form — skip it (Export→HTML covers it).
  }
  // Synchronous on purpose (no chart fetch): awaiting a fetch here would lose the
  // clipboard user activation. A persisted-only chart still gets the note.
  if (hasImageOutput(cell) && parts.length === 0) parts.push('(image output — use Export ▸ HTML to save the plot)')
  return parts.join('\n\n')
}

export async function copyCellOutput(cell) {
  const text = buildCellOutputCopyText(cell)
  if (!text) return false
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch { return false }
  }
}

// Copy a cell's ERROR together with the context needed to debug it elsewhere (a
// ticket, a chat, a search): the one-line error, the code that ran, the full
// traceback (when captured), and the failing frame's local values. Returns a
// Promise<boolean>. Mirrors copyCellOutput's clipboard + hidden-textarea fallback.
export async function copyErrorWithContext(cell) {
  if (!cell || !cell.error) return false
  const parts = [`Error:\n${cell.error}`]
  const code = cell.lastExecutedCode ?? cell.code ?? ''
  if (code) {
    const lang = cell.type === 'sql' ? 'sql' : 'python'
    parts.push(`Code:\n\`\`\`${lang}\n${code}\n\`\`\``)
  }
  if (cell.errorTraceback) {
    parts.push(`Traceback:\n\`\`\`\n${cell.errorTraceback}\n\`\`\``)
  }
  if (cell.errorLocals && typeof cell.errorLocals === 'object') {
    const entries = Object.entries(cell.errorLocals)
    if (entries.length) {
      parts.push('Local variables at the failure point:\n' + entries.map(([k, v]) => `  ${k} = ${v}`).join('\n'))
    }
  }
  const text = parts.join('\n\n')
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch { return false }
  }
}

/** The single-cell Markdown export document (text). `now` is injectable for tests. */
export function buildCellOutputMarkdown(cell, notebookName, badge, now = new Date()) {
  let md = `# ${notebookName || 'Notebook'} — ${badge}\n\n`
  md += `*Generated: ${now.toLocaleString()}*\n\n`
  md += `<details><summary>Code</summary>\n\n\`\`\`python\n${cell.code || ''}\n\`\`\`\n</details>\n\n`
  md += `## Output\n\n`
  if (isMultiOutput(cell)) {
    for (const it of cellOutputItems(cell)) {
      if (it.t === 'text') { md += `\`\`\`\n${it.text}\n\`\`\`\n\n`; continue }
      md += `*${it.caption}*\n\n`
      if (it.t === 'table') md += `${tablePayloadToMarkdown(it.payload)}\n\n`
      else if (it.t === 'plotly') md += `*(chart — export as HTML for full rendering)*\n\n`
      else md += `![${it.caption}](plot-${it.ordinal}.png)\n\n`
    }
  } else {
    if (cell.output) md += `\`\`\`\n${cell.output}\n\`\`\`\n\n`
    if (cell.html) {
      const tbl = parseTablePayload(cell.html)
      md += tbl ? tablePayloadToMarkdown(tbl) + '\n\n' : `*(chart — export as HTML for full rendering)*\n\n`
    }
    if (cell.image) md += `![Plot output](plot.png)\n\n`
    if (!cell.output && !cell.html && !cell.image) md += '*(empty output)*\n'
  }
  md += `\n---\n\n*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*\n`
  return md
}

export function exportCellOutputMarkdown(cell, notebookName, badge) {
  const filename = `${slug(notebookName)}-${badge}-output.md`
  downloadBlob(buildCellOutputMarkdown(cell, notebookName, badge), 'text/markdown', filename)
}
