/**
 * chatTurns — pure helpers for the AI chat's turn view (no React, no DOM).
 *
 * The chat's message list stays the source of truth (`tab._chatMessages`, a flat
 * [{role, content, timestamp?, attachments?, isError?, intelCard?}] array). A TURN is
 * DERIVED from it: one user question plus every assistant bubble until the next user
 * question. Per-turn UI state (collapsed, inserted cells, card title, approval steps)
 * lives in a separate per-notebook client store (see hooks/useChatTurnState.js); the
 * helpers here compute keys, titles, steps, insert status and store transitions.
 *
 * Code-block parsing is shared with the chat renderer: `normalizeFences` + marked's
 * own lexer (same `breaks` + KaTeX options as the panel's global `marked`), so the
 * blocks counted/inserted here are exactly the cards the panel renders.
 */

import { Marked } from 'marked'
import markedKatex from 'marked-katex-extension'

// ── Fences ────────────────────────────────────────────────────────────────────

// Normalize code-fence artifacts before markdown rendering AND extraction.
//
// The model occasionally emits malformed fences that make marked mis-render a
// response as one giant block. We see three slips:
//   1. A ``` glued to preceding text on the same line — e.g.
//      "...orders (Athena).```markdown". marked only treats ``` as a fence when it
//      starts a line, so a glued fence leaks as literal text.
//   2. A duplicated opener — ```lang immediately followed by another ```lang.
//   3. A NEW language opener (```python) appearing while a prior fence (```markdown)
//      is still open because the model forgot the closing ```. marked then swallows
//      the second block as literal content of the first.
//
// This preprocessor walks the text line-by-line, tracks whether a fence is open,
// and (a) breaks glued fences onto their own line, (b) auto-closes a still-open
// fence before a new language-opener, and (c) drops an immediate duplicate opener.
// It only moves/inserts/merges fence delimiters — never code or prose content.
//
// Fixed vs. the original in-panel version: (a) only a fence glued to NON-whitespace
// text is split, so an indented (list-nested) fence line stays intact and marked
// dedents its body; (b) info strings (```python title="a") are accepted, so their
// closer is no longer mistaken for an opener; auto-closers keep the opener's indent.
export function normalizeFences(md) {
  if (!md || md.indexOf('```') === -1) return md
  // Step 1: split a fence glued to NON-whitespace text only.
  const pre = md.replace(/([^\s`])[ \t]*(```)/g, '$1\n$2')
  const lines = pre.split('\n')
  const out = []
  let openLang = null              // language of the currently-open fence, or null
  let openInd = ''                 // indent of the currently-open fence
  // Info strings allowed: lang = first word; anything after it is ignored.
  const fenceRe = /^(\s*)```([A-Za-z0-9_+-]*)(?:[ \t]+[^`\n]*)?\s*$/
  for (const line of lines) {
    const m = line.match(fenceRe)
    if (!m) { out.push(line); continue }
    const ind = m[1]
    const lang = m[2] || ''
    if (openLang === null) {
      openLang = lang; openInd = ind; out.push(line)       // opening a fence
    } else if (lang === '') {
      openLang = null; out.push(line)                     // a bare ``` closes it
    } else {
      // A NEW language opener while a fence is still open: close the prior block
      // first, then open the new one — unless it's an immediate duplicate opener.
      const prevWasOpener = out.length > 0 && fenceRe.test(out[out.length - 1])
      if (prevWasOpener && lang === openLang) continue
      out.push(openInd + '```')
      out.push(line)
      openLang = lang; openInd = ind
    }
  }
  // A fence left open at EOF is closed so the tail doesn't leak.
  if (openLang !== null) out.push(openInd + '```')
  return out.join('\n')
}

// Fence languages that map to a notebook cell type (Insert-able, tracked).
// Other fences (bash, json, …) still render as cards with their own Insert button,
// but they are not counted, tracked, or part of "Insert all".
export const INSERTABLE = Object.freeze({ '': 'code', python: 'code', py: 'code', sql: 'sql', markdown: 'markdown' })

// First word of a fence info string, lowercased: 'Python title="a"' → 'python'.
export const langKey = (lang) => String(lang || '').trim().split(/\s+/)[0].toLowerCase()

export function insertableType(lang) {
  const k = langKey(lang)
  return Object.prototype.hasOwnProperty.call(INSERTABLE, k) ? INSERTABLE[k] : null
}

// Same lexing options/extensions as the panel's global `marked` (breaks + KaTeX), so
// a $$…$$ block or a fence is tokenized identically. The renderer is irrelevant here.
const _lexer = new Marked({ breaks: true }).use(markedKatex({ throwOnError: false, nonStandard: true }))

function memo(cache, max, key, compute) {
  if (cache.has(key)) return cache.get(key)
  const v = compute()
  if (cache.size >= max) cache.delete(cache.keys().next().value)   // evict oldest
  cache.set(key, v)
  return v
}

const _tokCache = new Map()
function lexTokens(md) {
  return memo(_tokCache, 200, md, () => {
    try { return _lexer.lexer(md) } catch (e) {
      console.debug('[chatTurns] lexer failed', e)
      return []
    }
  })
}

// Extract the insertable fenced code blocks of an assistant message as {code, type}
// items, in document order. type is 'sql' | 'markdown' | 'code' (code === Python).
// Walks marked's own code tokens of the normalizeFences'd text, so it matches the
// rendered cards exactly. Empty blocks are skipped. Callers must not mutate the
// (cached) result.
const _blockCache = new Map()
export function extractCodeBlocks(content) {
  const md = normalizeFences(content || '')
  return memo(_blockCache, 200, md, () => {
    const out = []
    _lexer.walkTokens(lexTokens(md), (t) => {
      if (t.type !== 'code') return
      const type = insertableType(t.lang)
      if (!type || !String(t.text || '').trim()) return
      out.push({ code: t.text.trim(), type })
    })
    return out
  })
}

// ── Hashing / keys ────────────────────────────────────────────────────────────

// 32-bit FNV-1a → base36. Stable, tiny, good enough for UI keys.
export function hashString(s) {
  let h = 0x811c9dc5
  const str = String(s ?? '')
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(36)
}

// What identifies a question across live and reloaded transcripts. Attachment turns
// key on the attachment names (the reloaded text carries inlined file text).
export function turnFingerprint(userMsg) {
  if (!userMsg) return ''
  const atts = Array.isArray(userMsg.attachments) ? userMsg.attachments : []
  if (atts.length) return 'att:' + atts.map(a => a?.name || '').join('|')
  return String(userMsg.content || '').trim()
}

export function blockKey(type, code, n) {
  return `${type}:${hashString(String(code || '').trim())}#${n}`
}

// ── Turns ─────────────────────────────────────────────────────────────────────

/**
 * Group the flat message list into turns.
 * Turn = { key, ordinal, userIndex, user, assistant: [{msg, index}] }
 * key = `${hash(fingerprint)}#${k}`, k = earlier turns with the same fingerprint,
 * so a key doesn't depend on the turn's position.
 */
export function groupTurns(messages) {
  const turns = []
  const seen = new Map()
  let cur = null
  const open = (user, userIndex) => {
    const h = hashString(user ? turnFingerprint(user) : '')
    const k = seen.get(h) || 0
    seen.set(h, k + 1)
    cur = { key: `${h}#${k}`, ordinal: turns.length, userIndex, user, assistant: [] }
    turns.push(cur)
  }
  ;(messages || []).forEach((m, i) => {
    if (!m) return
    if (m.role === 'user') { open(m, i); return }
    if (!cur) open(null, -1)          // defensive: a leading assistant-only bubble
    cur.assistant.push({ msg: m, index: i })
  })
  return turns
}

// ── AI context window ─────────────────────────────────────────────────────────

/**
 * How many of the latest turns are in the agent's context, from the agent's sliding
 * window size in MESSAGES (GET /ai/config agent_conversation_window_size; one
 * question + one answer per turn, so window // 2). Anything unusable (missing,
 * non-numeric, < 2: an older proxy or a failed fetch) → 0, meaning "unknown: treat
 * every turn as in context". Never a hardcoded window.
 */
export function contextTurnCount(windowSize) {
  const w = typeof windowSize === 'number' ? windowSize : NaN
  return Number.isFinite(w) && w >= 2 ? Math.floor(w / 2) : 0
}

/**
 * Keys of the turns that are NOT in the agent's context: everything before the last
 * `n` question turns (turns as built by groupTurns, so resumes / auto-decline
 * continuations are part of their turn and never count). A turn is out when at least
 * `n` question turns come after it; a leading assistant-only turn (no question)
 * follows the same rule. `n` missing / 0 → empty set (all in context).
 */
export function outOfContextKeys(turns, n) {
  const out = new Set()
  const list = Array.isArray(turns) ? turns : []
  if (!(Number.isInteger(n) && n > 0)) return out
  let after = 0                     // question turns after the one being looked at
  for (let i = list.length - 1; i >= 0; i--) {
    const t = list[i]
    if (after >= n) out.add(t.key)
    if (t.user) after++
  }
  return out
}

// ── Status notes (folded steps) ───────────────────────────────────────────────

const NOTE_LINE_RE = /^_\((.+)\)_\s*$/
const NOTE_TAIL_RE = /_\(([^()\n]+)\)_\s*$/
const DECLINE_RE = /^(Declined the pending \S+ request|Declined \d+ pending requests \(.*\)) because a new message was sent; (it was|they were) not run\.$/
const FENCE_OPEN_RE = /^\s*(`{3,}|~{3,})/

/**
 * Split the agent's status notes out of an assistant message. Removes, OUTSIDE code
 * fences only: whole `_(note)_` lines; a `_(note)_` glued to the end of a line
 * (resume after a non-newline start); the persisted auto-decline notice. Returns
 * {notes: string[] (content order), body}. Code text is never rewritten.
 * A model-written line of exactly these shapes would fold too (accepted: low risk).
 */
const _noteCache = new Map()
export function splitStatusNotes(content) {
  const src = String(content || '')
  if (src.indexOf('_(') === -1 && src.indexOf('Declined ') === -1) return { notes: [], body: src }
  return memo(_noteCache, 200, src, () => {
    const notes = []
    const out = []
    let fence = null               // { ch, len } of the open fence, or null
    let dropBlank = false          // swallow the blank line that followed a note
    for (const line of src.split('\n')) {
      if (fence) {
        const t = line.trim()
        if (t.length >= fence.len && t[0] === fence.ch && t.split('').every(c => c === fence.ch)) fence = null
        out.push(line)
        continue
      }
      const fo = line.match(FENCE_OPEN_RE)
      if (fo) {
        fence = { ch: fo[1][0], len: fo[1].length }
        dropBlank = false
        out.push(line)
        continue
      }
      if (dropBlank && !line.trim()) { dropBlank = false; continue }
      dropBlank = false
      const whole = line.match(NOTE_LINE_RE)
      if (whole) { notes.push(whole[1].trim()); dropBlank = true; continue }
      const trimmed = line.trim()
      if (DECLINE_RE.test(trimmed)) { notes.push(trimmed); dropBlank = true; continue }
      const tail = line.match(NOTE_TAIL_RE)
      if (tail && line.slice(0, tail.index).trim()) {
        notes.push(tail[1].trim())
        out.push(line.slice(0, tail.index).replace(/\s+$/, ''))
        continue
      }
      out.push(line)
    }
    if (!notes.length) return { notes: [], body: src }
    while (out.length && !out[0].trim()) out.shift()
    return { notes, body: out.join('\n') }
  })
}

/**
 * Classify a turn's tool activity into the folded "N steps" list and the visible
 * pending approval. Notes (content order) come first, then the answered approvals
 * recorded client-side. A PENDING approval is never folded: it's returned separately
 * so the UI keeps it actionable.
 * @returns {{ steps: Array<{kind:'note',text}|{kind:'approval',...}>, pending: object|null }}
 */
export function turnSteps({ notes = [], stored = [], pendingAction = null } = {}) {
  const steps = notes.map(text => ({ kind: 'note', text }))
  for (const s of Array.isArray(stored) ? stored : []) {
    if (s && s.kind === 'approval' && s.decision) steps.push(s)
  }
  return { steps, pending: pendingAction || null }
}

// The insertable blocks of a whole turn, numbered across its bubbles:
// [{code, type, key, bubble}] in render order. Extracted PER bubble so an unclosed
// fence in one bubble can't swallow the next.
export function turnBlocks(turn) {
  const out = []
  const counts = new Map()
  ;(turn?.assistant || []).forEach(({ msg }, bubble) => {
    for (const b of extractCodeBlocks(splitStatusNotes(msg?.content).body)) {
      const base = `${b.type}:${hashString(b.code)}`
      const n = counts.get(base) || 0
      counts.set(base, n + 1)
      out.push({ code: b.code, type: b.type, key: `${base}#${n}`, bubble })
    }
  })
  return out
}

// ── Titles ────────────────────────────────────────────────────────────────────

// Strip inline markdown syntax from a heading's text (never cuts the string).
export function stripInlineMarkdown(s) {
  // Backslash escapes are protected first (so `\*` survives the emphasis pass), then
  // restored as the literal character at the end.
  return String(s || '')
    .replace(/\\([\\`*_{}[\]()#+\-.!>|~])/g, (_, c) => `\uE000${c.charCodeAt(0).toString(16)}\uE001`)
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]+>/g, '')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/(^|[^\w*])\*(?!\s)([^*]+?)\*(?!\w)/g, '$1$2')
    .replace(/(^|[^\w])_(?!\s)([^_]+?)_(?!\w)/g, '$1$2')
    .replace(/~~(.+?)~~/g, '$1')
    .replace(/`+/g, '')
    .replace(/\uE000([0-9a-f]+)\uE001/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/\s+/g, ' ')
    .trim()
}

function firstHeading(md) {
  let found = null
  _lexer.walkTokens(lexTokens(normalizeFences(md)), (t) => {
    if (found) return
    if (t.type === 'heading') {
      const txt = stripInlineMarkdown(t.text)
      if (txt) found = txt
    } else if (t.type === 'code' && langKey(t.lang) === 'markdown') {
      const inner = firstHeading(t.text || '')
      if (inner) found = inner
    }
  })
  return found
}

/**
 * Title of an assistant reply: its first markdown heading (document order, nested
 * containers and ```markdown fences included), inline syntax stripped. While still
 * streaming (`complete: false`) only COMPLETE lines are lexed, so a half-streamed
 * heading never shows ("F", "Fu", …); the caller shows the fallback meanwhile. This
 * cuts parser input only, never user-visible text. Returns null when none.
 */
const _titleCache = new Map()
export function extractTitle(content, { complete = true } = {}) {
  const raw = String(content || '')
  const src = complete ? raw : raw.slice(0, raw.lastIndexOf('\n') + 1)
  if (!src.trim()) return null
  return memo(_titleCache, 200, src, () => firstHeading(splitStatusNotes(src).body))
}

const INTEL_RE = /^Context \(from Workbook Intel\):\n([\s\S]*?)\n\nTask: ([\s\S]*)$/

// Parse the prompt IntelAccordion.buildPrompt builds. → {context, task} | null
export function parseIntelPrompt(text) {
  const m = String(text || '').match(INTEL_RE)
  return m ? { context: m[1], task: m[2] } : null
}

const firstLine = (s) => String(s || '').split('\n').map(l => l.trim()).find(Boolean) || ''

// The short label for a question (header row 2, fallback title).
export function questionLabel({ cardTitle, userText, attachments, hasUser = true } = {}) {
  if (cardTitle && String(cardTitle).trim()) return String(cardTitle).trim()
  const intel = parseIntelPrompt(userText)
  if (intel) {
    const t = firstLine(intel.task)
    if (t) return t
  }
  const line = firstLine(userText)
  if (line) return line
  const att = Array.isArray(attachments) ? attachments.find(a => a?.name) : null
  if (att) return `Attachment: ${att.name}`
  return hasUser ? 'Question' : 'Conversation'
}

/**
 * Turn title, no model call: first heading of the assistant reply → Intel card
 * title → question label. `contents` = the turn's assistant bubble contents.
 */
export function turnTitle({ contents = [], cardTitle, userText, attachments, hasUser = true, complete = true } = {}) {
  for (const c of contents) {
    const h = extractTitle(c, { complete })
    if (h) return h
  }
  return questionLabel({ cardTitle, userText, attachments, hasUser })
}

// Comparison key for "is this the same text?": leading heading hashes dropped,
// inline markdown stripped, whitespace collapsed, trimmed, case-folded.
export function normalizeForCompare(s) {
  return stripInlineMarkdown(String(s || '').replace(/^\s*#{1,6}\s+/, '')).toLowerCase()
}

/**
 * Whether a COLLAPSED turn shows its question as a second line under the title.
 * Only when it adds something: a turn whose title is the question itself (no reply
 * heading yet, or a heading that just repeats the question / card title) shows it
 * once. `question` = the turn's question label (card title for card turns, the
 * typed question's first line otherwise).
 */
export function showQuestionSubtitle({ title, question } = {}) {
  const q = normalizeForCompare(question)
  if (!q) return false
  return normalizeForCompare(title) !== q
}

// ── Rail open state ───────────────────────────────────────────────────────────

/**
 * Open state of the turn rail's floating title list. Two independent reasons keep
 * it open: `hover` (pointer over the rail/list; cleared by the component after a
 * short grace delay once the pointer has left) and `keyboard` (keyboard focus
 * inside, i.e. :focus-visible or a navigation key). A mouse click focusing a dot is
 * NOT a keyboard reason. Everything that dismisses clears both.
 * Events: 'pointer-enter' | 'pointer-left' (grace expired) | 'focus-visible' |
 * 'nav-key' | 'focus-out' | 'escape' | 'select' | 'outside' | 'scroll' | 'resize'.
 * Returns the SAME state when nothing changes.
 */
export const RAIL_CLOSED = Object.freeze({ hover: false, keyboard: false })
export function railOpenReducer(state, event) {
  const s = state || RAIL_CLOSED
  let next
  switch (event) {
    case 'pointer-enter': next = { ...s, hover: true }; break
    case 'pointer-left': next = { ...s, hover: false }; break
    case 'focus-visible':
    case 'nav-key': next = { ...s, keyboard: true }; break
    case 'focus-out': next = { ...s, keyboard: false }; break
    case 'escape':
    case 'select':
    case 'outside':
    case 'scroll':
    case 'resize': next = RAIL_CLOSED; break
    default: return s
  }
  return next.hover === s.hover && next.keyboard === s.keyboard ? s : next
}
export const railIsOpen = (s) => !!(s && (s.hover || s.keyboard))

// ── Insert status ─────────────────────────────────────────────────────────────

/**
 * @param blocks       turnBlocks(turn)
 * @param insertedMap  { [blockKey]: cellId } from the store
 * @param liveCellIds  Set of the notebook's current cell ids
 * A block counts as inserted only while its cell still exists.
 */
export function insertStatus(blocks, insertedMap, liveCellIds) {
  const map = insertedMap || {}
  const live = liveCellIds || new Set()
  const cellIdFor = (key) => {
    const id = map[key]
    return id != null && live.has(id) ? id : null
  }
  const pending = []
  let inserted = 0
  for (const b of blocks || []) {
    if (cellIdFor(b.key)) inserted++
    else pending.push(b)
  }
  return { total: (blocks || []).length, inserted, pending, cellIdFor }
}

// ── Collapse rules / store transitions ────────────────────────────────────────

// A streaming (or approval-paused) turn is never collapsed; otherwise the explicit
// stored state wins, defaulting to "latest expanded, older collapsed".
export function effectiveCollapsed({ isLatest, isStreaming, stored } = {}) {
  if (isStreaming) return false
  const c = stored?.collapsed
  return typeof c === 'boolean' ? c : !isLatest
}

export const STORE_VERSION = 1
export const emptyStore = (sessionId = null) => ({ v: STORE_VERSION, sessionId: sessionId || null, turns: {} })

// Parse a persisted blob; anything unexpected → empty store.
export function parseStore(raw) {
  if (!raw) return emptyStore()
  try {
    const s = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (!s || s.v !== STORE_VERSION || typeof s.turns !== 'object' || s.turns === null) return emptyStore()
    return { v: STORE_VERSION, sessionId: s.sessionId || null, turns: s.turns }
  } catch {
    return emptyStore()
  }
}

export function setTurnEntry(store, key, patch, { replace = false, now = Date.now() } = {}) {
  const prev = store.turns[key] || {}
  const p = typeof patch === 'function' ? patch(prev) : patch
  const next = replace ? { ...p } : { ...prev, ...p }
  if (typeof next.seen !== 'number') next.seen = now
  return { ...store, turns: { ...store.turns, [key]: next } }
}

export function collapseAll(store, keys, now = Date.now()) {
  if (!keys || !keys.length) return store
  const turns = { ...store.turns }
  for (const k of keys) turns[k] = { ...turns[k], collapsed: true, seen: turns[k]?.seen ?? now }
  return { ...store, turns }
}

export const PRUNE_TTL_MS = 30 * 24 * 60 * 60 * 1000
export const SEEN_REFRESH_MS = 60 * 60 * 1000

// Refresh `seen` for present keys (at most hourly) and drop entries absent for the
// TTL. Returns the SAME store when nothing changed.
export function pruneStore(store, liveKeys, now = Date.now()) {
  const live = liveKeys instanceof Set ? liveKeys : new Set(liveKeys || [])
  let changed = false
  const turns = {}
  for (const [k, v] of Object.entries(store.turns)) {
    const seen = typeof v?.seen === 'number' ? v.seen : null
    if (live.has(k)) {
      if (seen == null || now - seen > SEEN_REFRESH_MS) { turns[k] = { ...v, seen: now }; changed = true } else turns[k] = v
    } else if (seen == null) {
      turns[k] = { ...v, seen: now }; changed = true
    } else if (now - seen > PRUNE_TTL_MS) {
      changed = true
    } else {
      turns[k] = v
    }
  }
  return changed ? { ...store, turns } : store
}

/**
 * The hook's per-render state sync: state = { nb, store, dirty }. When the notebook
 * id changes, the store is re-read for THAT id in the same step (never pairing one
 * notebook's store with another's id); otherwise the store is re-bound to the
 * session. Returns the SAME state when nothing changes. `read(nb)` → store.
 */
export function syncTurnState(state, nb, sessionId, read) {
  if (!state || state.nb !== nb) {
    const loaded = read(nb)
    const store = reconcileSession(loaded, sessionId)
    return { nb, store, dirty: store !== loaded }
  }
  const store = reconcileSession(state.store, sessionId)
  return store === state.store ? state : { nb, store, dirty: true }
}

// Bind the store to the chat session. No session yet → keep. First binding
// (null → id, e.g. the first VM launch) → adopt and keep the turns. A different
// session → a different conversation → empty.
export function reconcileSession(store, sessionId) {
  if (!sessionId) return store
  if (!store.sessionId) return { ...store, sessionId }
  if (store.sessionId !== sessionId) return emptyStore(sessionId)
  return store
}
