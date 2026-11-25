/**
 * Basic, self-contained syntax highlighting for AI-assistant code cards.
 *
 * Deliberately NOT the Lezer/CodeMirror highlighter (that one, fed back through
 * marked, produced doubled text). This is a single-pass regex tokenizer that
 * scans the code left-to-right and consumes every character EXACTLY once, so it
 * can never duplicate content. It operates on the RAW code and HTML-escapes as it
 * emits, returning a self-contained HTML string of <span class="tok-*"> tokens.
 *
 * Coverage is intentionally light (comments, strings, numbers, keywords,
 * builtins) — "basic" highlighting, good enough to read Python/SQL at a glance.
 */

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

const PY_KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break',
  'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for',
  'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or',
  'pass', 'raise', 'return', 'try', 'while', 'with', 'yield', 'match', 'case',
])

const SQL_KEYWORDS = new Set([
  'select', 'from', 'where', 'group', 'by', 'order', 'having', 'join', 'inner',
  'left', 'right', 'outer', 'full', 'on', 'as', 'and', 'or', 'not', 'in', 'is',
  'null', 'like', 'between', 'union', 'all', 'distinct', 'limit', 'offset',
  'insert', 'into', 'values', 'update', 'set', 'delete', 'create', 'table',
  'view', 'with', 'case', 'when', 'then', 'else', 'end', 'count', 'sum', 'avg',
  'min', 'max', 'desc', 'asc', 'cast', 'over', 'partition',
])

// Identifier chars (ASCII). A token is [A-Za-z_][A-Za-z0-9_]*
function isIdentStart(c) { return /[A-Za-z_]/.test(c) }
function isIdent(c) { return /[A-Za-z0-9_]/.test(c) }

/**
 * Highlight `code` for `lang` ('python' | 'sql'). Returns HTML with tok-* spans.
 * Single left-to-right scan; unknown langs / anything not matched falls through
 * as plain escaped text.
 */
export function highlightBasic(code, lang) {
  const kw = lang === 'sql' ? SQL_KEYWORDS : PY_KEYWORDS
  const kwLower = lang === 'sql'  // SQL keywords are case-insensitive
  const lineComment = lang === 'sql' ? '--' : '#'

  let out = ''
  let i = 0
  const n = code.length

  const tok = (cls, text) => { out += `<span class="tok-${cls}">${esc(text)}</span>` }
  const plain = (text) => { out += esc(text) }

  while (i < n) {
    const c = code[i]

    // Line comment → to end of line
    if (code.startsWith(lineComment, i)) {
      let j = i
      while (j < n && code[j] !== '\n') j++
      tok('comment', code.slice(i, j))
      i = j
      continue
    }

    // Strings: ' " and triple-quoted (python). Scan to matching close, honoring
    // backslash escapes. Never emits partial — consumes the whole literal.
    if (c === '"' || c === "'") {
      const triple = code.startsWith(c + c + c, i)
      const quote = triple ? c + c + c : c
      let j = i + quote.length
      while (j < n) {
        if (code[j] === '\\') { j += 2; continue }
        if (code.startsWith(quote, j)) { j += quote.length; break }
        j++
      }
      tok('string', code.slice(i, Math.min(j, n)))
      i = Math.min(j, n)
      continue
    }

    // Numbers: 123, 12.5, 1e-3, 0xFF
    if (/[0-9]/.test(c) || (c === '.' && /[0-9]/.test(code[i + 1] || ''))) {
      let j = i
      while (j < n && /[0-9a-fA-FxX_.eE+-]/.test(code[j])) {
        // stop a trailing +/- that isn't part of an exponent
        if ((code[j] === '+' || code[j] === '-') && !/[eE]/.test(code[j - 1])) break
        j++
      }
      tok('number', code.slice(i, j))
      i = j
      continue
    }

    // Identifiers / keywords
    if (isIdentStart(c)) {
      let j = i
      while (j < n && isIdent(code[j])) j++
      const word = code.slice(i, j)
      const cmp = kwLower ? word.toLowerCase() : word
      if (kw.has(cmp)) {
        tok('keyword', word)
      } else if (code[j] === '(') {
        // name immediately followed by '(' → treat as a function/definition call
        tok('function', word)
      } else {
        plain(word)
      }
      i = j
      continue
    }

    // Anything else (operators, punctuation, whitespace): emit as-is (escaped).
    plain(c)
    i++
  }

  return out
}

/**
 * Light inline markdown highlighting for the chat's markdown cards.
 *
 * Same safety contract as highlightBasic: a single left-to-right pass that
 * consumes every character exactly once and HTML-escapes as it emits, so it can
 * never duplicate content. This does NOT render markdown to formatted HTML — it
 * shows the markdown SOURCE, tinting the syntax markers (#, *, -, >, `, links)
 * and emphasis text so it reads better while staying verbatim. No `marked`, no
 * nested render, no doubling risk.
 *
 * The <code>'s textContent stays equal to the raw source (spans don't change
 * textContent), so Copy/Insert are unaffected.
 */

// Inline pass over a single line's content (no trailing newline). Handles
// `code`, **bold**, *italic*/_italic_, and [text](url). One left-to-right scan.
function mdInline(line) {
  let out = ''
  let i = 0
  const n = line.length
  const tok = (cls, text) => { out += `<span class="tok-md-${cls}">${esc(text)}</span>` }
  const raw = (cls, text) => { out += `<span class="tok-md-${cls}">${text}</span>` } // text already-escaped html

  while (i < n) {
    const c = line[i]

    // Inline code: `...` (single backtick span, to next backtick on the line)
    if (c === '`') {
      let j = i + 1
      while (j < n && line[j] !== '`') j++
      if (j < n) { // closed on this line
        tok('code', line.slice(i, j + 1))
        i = j + 1
        continue
      }
    }

    // Bold: **...** or __...__  (check before single * / _)
    if ((c === '*' || c === '_') && line[i + 1] === c) {
      const marker = c + c
      const close = line.indexOf(marker, i + 2)
      if (close !== -1) {
        tok('bold', line.slice(i, close + 2))
        i = close + 2
        continue
      }
    }

    // Italic: *...* or _..._  (single marker, non-empty, no space right after)
    if ((c === '*' || c === '_') && line[i + 1] !== c && line[i + 1] !== ' ' && line[i + 1] !== undefined) {
      const close = line.indexOf(c, i + 1)
      if (close !== -1) {
        tok('italic', line.slice(i, close + 1))
        i = close + 1
        continue
      }
    }

    // Link: [text](url)
    if (c === '[') {
      const rb = line.indexOf(']', i + 1)
      if (rb !== -1 && line[rb + 1] === '(') {
        const rp = line.indexOf(')', rb + 2)
        if (rp !== -1) {
          // [text] tinted as link text, (url) tinted as link url — one construct.
          raw('link', esc(line.slice(i, rb + 1)))
          raw('link-url', esc(line.slice(rb + 1, rp + 1)))
          i = rp + 1
          continue
        }
      }
    }

    // Plain char
    out += esc(c)
    i++
  }
  return out
}

export function highlightMarkdown(src) {
  const lines = src.split('\n')
  const rendered = lines.map((line) => {
    // Heading: leading #{1,6} + space
    const h = /^(\s*)(#{1,6})(\s+)(.*)$/.exec(line)
    if (h) {
      const [, lead, hashes, sp, rest] = h
      return esc(lead) +
        `<span class="tok-md-heading-marker">${esc(hashes)}</span>` + esc(sp) +
        `<span class="tok-md-heading">${mdInline(rest)}</span>`
    }

    // Blockquote: leading > (one or more)
    const bq = /^(\s*)(>+\s?)(.*)$/.exec(line)
    if (bq) {
      const [, lead, marker, rest] = bq
      return esc(lead) +
        `<span class="tok-md-quote-marker">${esc(marker)}</span>` +
        `<span class="tok-md-quote">${mdInline(rest)}</span>`
    }

    // Unordered list: leading -, * or + then a space
    const ul = /^(\s*)([-*+])(\s+)(.*)$/.exec(line)
    if (ul) {
      const [, lead, bullet, sp, rest] = ul
      return esc(lead) +
        `<span class="tok-md-list-marker">${esc(bullet)}</span>` + esc(sp) +
        mdInline(rest)
    }

    // Ordered list: leading digits + . or ) then a space
    const ol = /^(\s*)(\d+[.)])(\s+)(.*)$/.exec(line)
    if (ol) {
      const [, lead, marker, sp, rest] = ol
      return esc(lead) +
        `<span class="tok-md-list-marker">${esc(marker)}</span>` + esc(sp) +
        mdInline(rest)
    }

    // Plain line → inline pass only
    return mdInline(line)
  })
  return rendered.join('\n')
}
