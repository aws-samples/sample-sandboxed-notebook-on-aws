// "Explain this code" — pure helpers (no React, no import.meta: loadable in Node).
//   cache key, upstream signature, line slicing, defensive response normalisation,
//   the notes markdown builder, relative age and the in-session result store.
import { relatedCells } from './depGraph.js'

/** Synchronous FNV-1a 32-bit hash (hex). Enough for a client-side cache key. */
export function hashText(str) {
  let h = 0x811c9dc5
  const s = String(str ?? '')
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16).padStart(8, '0')
}

/**
 * Signature of the code of every transitive upstream cell (ids ordered by cell
 * position), from the stored DAG graph. Changes when any upstream cell's code
 * changes or the set of upstream cells changes.
 */
export function upstreamSig(graph, cells, cellId) {
  const up = relatedCells(graph, cellId).upstream
  const pos = new Map((cells || []).map((c, i) => [String(c.id), i]))
  const byId = new Map((cells || []).map((c) => [String(c.id), c]))
  return [...up.keys()]
    .map(String)
    .filter((id) => pos.has(id) && (byId.get(id).type || 'code') !== 'markdown')
    .sort((a, b) => pos.get(a) - pos.get(b))
    .map((id) => `${id}:${hashText(byId.get(id).code)}`)
    .join('|')
}

/** Cache key: notebook id + cell id + hash of the code + hash of the upstream code. */
export function explainKey({ notebookId, cellId, code, upstreamSig: sig }) {
  return [String(notebookId), String(cellId), hashText(code), hashText(sig || '')].join(':')
}

/** Canonical line split (matches the proxy's split_lines): CRLF normalised, trailing newlines ignored. */
export function splitLines(code) {
  const text = String(code ?? '').replace(/\r\n?/g, '\n').replace(/\n+$/, '')
  return text ? text.split('\n') : []
}

/** Whole lines start..end (1-based, inclusive), clamped to the code. */
export function sliceLines(code, start, end) {
  const lines = splitLines(code)
  if (!lines.length) return ''
  const a = Math.min(Math.max(Math.trunc(Number(start)) || 1, 1), lines.length)
  const b = Math.min(Math.max(Math.trunc(Number(end)) || lines.length, a), lines.length)
  return lines.slice(a - 1, b).join('\n')
}

const str = (v) => (typeof v === 'string' ? v : '')

/**
 * Defensive shape check of the proxy response against the code the card will
 * slice. Ranges are re-clamped and made contiguous; an unusable `sections`
 * becomes one whole-cell section so the card always has real code to show.
 */
export function normalizeResult(resp, code) {
  const n = splitLines(code).length
  const r = resp && typeof resp === 'object' ? resp : {}
  let sections = []
  if (Array.isArray(r.sections)) {
    sections = r.sections
      .filter((s) => s && typeof s === 'object')
      .map((s) => ({
        title: str(s.title).trim(),
        start_line: Math.trunc(Number(s.start_line)),
        end_line: Math.trunc(Number(s.end_line)),
        explanation: str(s.explanation),
      }))
      .filter((s) => Number.isFinite(s.start_line) && Number.isFinite(s.end_line))
      .map((s) => ({
        ...s,
        start_line: Math.min(Math.max(s.start_line, 1), Math.max(n, 1)),
        end_line: Math.min(Math.max(s.end_line, 1), Math.max(n, 1)),
      }))
      .filter((s) => s.end_line >= s.start_line)
      .sort((a, b) => a.start_line - b.start_line)
    let pos = 1
    const fixed = []
    for (const s of sections) {
      if (s.end_line < pos) continue
      fixed.push({ ...s, start_line: pos })
      pos = s.end_line + 1
    }
    if (fixed.length) {
      fixed[fixed.length - 1].end_line = Math.max(n, 1)
      for (let i = 0; i < fixed.length - 1; i++) fixed[i].end_line = fixed[i + 1].start_line - 1
    }
    sections = fixed
  }
  if (!sections.length) {
    sections = [{ title: 'Explanation', start_line: 1, end_line: Math.max(n, 1), explanation: str(r.overview) }]
  }
  sections = sections.map((s) => ({ ...s, title: s.title || `Lines ${s.start_line}-${s.end_line}` }))
  return {
    overview: str(r.overview),
    sections,
    depends_on: (Array.isArray(r.depends_on) ? r.depends_on : [])
      .filter((d) => d && typeof d === 'object' && str(d.cell))
      .map((d) => ({
        cell: str(d.cell),
        provides: (Array.isArray(d.provides) ? d.provides : []).map(String),
        why: str(d.why),
      })),
    notes: str(r.notes),
    model: str(r.model),
    omittedCount: Number(r.omitted_count) || 0,
    omittedLabels: Array.isArray(r.omitted_labels) ? r.omitted_labels.map(String) : [],
    missingInputs: !!r.missing_inputs,
  }
}

/** Fence long enough that it never collides with backticks inside the code. */
function fenceFor(code) {
  let longest = 2
  for (const m of String(code).matchAll(/`+/g)) longest = Math.max(longest, m[0].length)
  return '`'.repeat(longest + 1)
}

export function localDate(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** Markdown appended to the cell's notes. `lang` is 'python' or 'sql'. */
export function buildNotesBlock(result, { code, lang = 'python', date } = {}) {
  const out = [`### Explanation (${date || localDate()})`, '']
  if (result.overview) out.push(result.overview.trim(), '')
  for (const s of result.sections) {
    const snippet = sliceLines(code, s.start_line, s.end_line)
    const fence = fenceFor(snippet)
    out.push(`#### ${s.title}`, '', `${fence}${lang}`, snippet, fence, '')
    if (s.explanation && s.explanation.trim()) out.push(s.explanation.trim(), '')
  }
  if (result.depends_on.length) {
    const parts = result.depends_on.map((d) => {
      const prov = d.provides.length ? ` (${d.provides.join(', ')})` : ''
      return `${d.cell}${prov}${d.why ? ` — ${d.why}` : ''}`
    })
    out.push(`**Depends on:** ${parts.join('; ')}`, '')
  }
  if (result.notes && result.notes.trim()) out.push(`**Notes:** ${result.notes.trim()}`, '')
  return out.join('\n').replace(/\n+$/, '')
}


/** The block without its heading line (the part that identifies a duplicate). */
function bodyOf(block) {
  const i = block.indexOf('\n')
  return i < 0 ? '' : block.slice(i + 1).trim()
}

/** Append to existing notes: blank line + horizontal rule when notes exist; never overwrite. */
export function appendToNotes(existing, block) {
  const cur = String(existing ?? '').replace(/\s+$/, '')
  return cur ? `${cur}\n\n---\n\n${block}` : block
}

/** True when the notes already contain this explanation (date-independent). */
export function isAlreadyAdded(existing, block) {
  const body = bodyOf(block)
  return !!body && String(existing ?? '').includes(body)
}

export function relativeAge(ts, now = Date.now()) {
  const s = Math.max(0, Math.floor((now - ts) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} h ago`
  return `${Math.floor(h / 24)} d ago`
}

// In-session store (module level: survives re-render and tab switches while the
// page lives; never persisted). Bounded LRU.
const STORE_MAX = 200
const store = new Map()
const skey = (nbId, cellId) => `${nbId}\u0000${cellId}`

export function getExplain(nbId, cellId) {
  return store.get(skey(nbId, cellId))
}
export function setExplain(nbId, cellId, entry) {
  const k = skey(nbId, cellId)
  store.delete(k)
  store.set(k, entry)
  while (store.size > STORE_MAX) store.delete(store.keys().next().value)
}
export function dismissExplain(nbId, cellId) {
  store.delete(skey(nbId, cellId))
}
export function explainStoreSize() {
  return store.size
}
