/**
 * dfCellFormat — client-side port of the VM's old `_linkify_urls` cell enhancer.
 *
 * The VM now sends each DataFrame cell as the STRING pandas' to_html would render
 * (via format_array), and we apply the same 7 transforms here so the rendered grid
 * looks identical to the previous server-rendered table:
 *   1. empty            -> plain empty
 *   2. NaN/nan/None/NaT -> muted-italic null
 *   3. True/False       -> colored bool badge
 *   4. image URL        -> thumbnail link
 *   5. URL              -> clickable link (shortened >60 chars)
 *   6. email            -> mailto link
 *   7. number           -> comma-grouped; negatives get a "negative" class
 *   8. long text (>80)  -> truncated with full-text title
 *
 * Returns a descriptor the grid renders: { kind, ... } — never raw HTML, so the
 * grid can render safe React elements (no dangerouslySetInnerHTML for cell values).
 */

// Tokens pandas' formatter emits for missing values. Shared with DataGrid so the
// null-styling and sort-empties-last logic stay in lock-step. Includes "<NA>"
// (pandas nullable dtypes) and "<NaN>"/"nan" variants.
export const NULL_TOKENS = new Set(['NaN', 'nan', 'None', 'NaT', '<NA>', ''])
const IMG_RE = /^https?:\/\/\S+\.(png|jpg|jpeg|gif|svg|webp)(\?\S*)?$/i
const URL_RE = /^https?:\/\/\S+$/
const EMAIL_RE = /^[\w.+-]+@[\w-]+\.[\w.-]+$/
const NUM_RE = /^-?\d[\d,]*\.?\d*$/

// Reproduces Python f'{num:,.2f}' (2 decimals, comma grouping) and f'{int(num):,}'.
function groupInt(n) {
  return n.toLocaleString('en-US', { maximumFractionDigits: 0 })
}
function group2dp(n) {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/**
 * Classify + format one cell string. `raw` is the pandas-formatted string.
 * Returns one of:
 *   { kind: 'null', text }
 *   { kind: 'bool', value: true|false, text }
 *   { kind: 'img', url }
 *   { kind: 'link', url, text }        // clickable http(s) URL, text may be shortened
 *   { kind: 'email', email }
 *   { kind: 'number', text, negative } // formatted number string
 *   { kind: 'text', text, title? }     // plain (title set when truncated)
 */
export function formatCell(raw) {
  const content = raw == null ? '' : String(raw)
  const trimmed = content.trim()

  // 1 + 2: empty / null tokens
  if (!trimmed) return { kind: 'text', text: content }
  if (NULL_TOKENS.has(trimmed)) return { kind: 'null', text: trimmed }

  // 3: booleans
  if (trimmed === 'True') return { kind: 'bool', value: true, text: 'True' }
  if (trimmed === 'False') return { kind: 'bool', value: false, text: 'False' }

  // 4: image URL
  if (IMG_RE.test(trimmed)) return { kind: 'img', url: trimmed }

  // 5: plain URL (shorten display >60 chars, matching url[:57] + '...')
  if (URL_RE.test(trimmed)) {
    const text = trimmed.length <= 60 ? trimmed : trimmed.slice(0, 57) + '...'
    return { kind: 'link', url: trimmed, text }
  }

  // 6: email
  if (EMAIL_RE.test(trimmed)) return { kind: 'email', email: trimmed }

  // 7: number — comma-group + 2dp, but ONLY for negatives or |n|>=1000.
  //    Small positives (e.g. "42", "3.14", "999") pass through unchanged.
  if (NUM_RE.test(trimmed)) {
    const num = parseFloat(trimmed.replace(/,/g, ''))
    if (!Number.isNaN(num)) {
      const hasDot = trimmed.includes('.')
      if (num < 0) {
        const text = hasDot ? group2dp(num) : groupInt(num)
        return { kind: 'number', text, negative: true }
      } else if (Math.abs(num) >= 1000) {
        const text = hasDot ? group2dp(num) : groupInt(num)
        return { kind: 'number', text, negative: false }
      }
      // else: leave small positive numbers as-is (fall through to plain text)
    }
  }

  // 8: long text — render in FULL. We used to truncate to 77 chars here, but that
  // hard cap meant widening a column could never reveal more text (the characters
  // were dropped before render). The grid now controls visible width via
  // drag-to-resize + CSS wrapping, so return the full value. Keep the full text in
  // `title` too so a hover tooltip still works for long, wrapped, or narrow cells.
  if (content.length > 80) {
    return { kind: 'text', text: content, title: content }
  }

  return { kind: 'text', text: content }
}

/** HTML-escape a string for building export HTML/MD. */
export function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * If `html` is the VM's structured DataFrame JSON payload, parse + return it;
 * otherwise return null. Shared by CellOutput (render) and the export paths.
 */
export function parseTablePayload(html) {
  if (typeof html !== 'string' || html.charCodeAt(0) !== 123 /* '{' */) return null
  if (!html.includes('"__df_table__"')) return null
  try {
    const obj = JSON.parse(html)
    return obj && obj.__df_table__ ? obj : null
  } catch {
    return null
  }
}

/** Render a table payload as a standalone HTML <table> (for HTML export). */
export function tablePayloadToHtml(payload) {
  const { columns = [], cells = [], total_rows = 0, shown_rows = 0, total_cols = 0, shown_cols = 0 } = payload || {}
  const head = '<thead><tr>' + columns.map(c => `<th>${escapeHtml(c)}</th>`).join('') + '</tr></thead>'
  const body = '<tbody>' + cells.map(row =>
    '<tr>' + row.map(v => `<td>${escapeHtml(v)}</td>`).join('') + '</tr>'
  ).join('') + '</tbody>'
  let note = ''
  if (total_rows > shown_rows) note += `<div class="df-truncation-note">Showing first ${shown_rows} of ${total_rows} rows.</div>`
  if (total_cols > shown_cols) note += `<div class="df-truncation-note">Showing first ${shown_cols} of ${total_cols} columns.</div>`
  return `<table class="df-table">${head}${body}</table>${note}`
}

/** Render a table payload as a GitHub-flavored Markdown pipe table (for MD export). */
export function tablePayloadToMarkdown(payload) {
  const { columns = [], cells = [], total_rows = 0, shown_rows = 0 } = payload || {}
  if (columns.length === 0) return '*(empty table)*'
  const esc = (v) => String(v ?? '').replace(/\|/g, '\\|').replace(/\n/g, ' ')
  const header = '| ' + columns.map(esc).join(' | ') + ' |'
  const sep = '| ' + columns.map(() => '---').join(' | ') + ' |'
  const rows = cells.map(row => '| ' + row.map(esc).join(' | ') + ' |').join('\n')
  let md = `${header}\n${sep}\n${rows}\n`
  if (total_rows > shown_rows) md += `\n*Showing first ${shown_rows} of ${total_rows} rows.*\n`
  return md
}
