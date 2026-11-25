/**
 * CellEditor — CodeMirror 6 editor wrapper for notebook cells.
 *
 * Replaces the textarea + Prism overlay approach with a proper code editor
 * that provides syntax highlighting, autocomplete, bracket matching, and
 * keybindings out of the box.
 *
 * Props:
 *   code         - current cell code (controlled from parent)
 *   language     - 'python' | 'sql'
 *   placeholder  - placeholder text when empty
 *   onCodeChange - callback(newCode) on every edit
 *   onExecute    - callback() for Shift+Enter
 *   onFocus      - callback() when editor gains focus
 *   readOnly     - boolean
 *   variables    - array of namespace variable names for autocomplete
 *   dataSources  - { items: [], schemas: {} } for table/file completion
 *   sessionId    - session ID for API calls
 *   highlight    - {start, end, reveal, revealLine} 1-based inclusive line range to tint
 *                  (focus-mode Explanation panel); a new `reveal` scrolls to `revealLine`
 */

import { useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { EditorState, Compartment, Annotation, RangeSet, StateField, StateEffect } from '@codemirror/state'
import { EditorView, keymap, placeholder as cmPlaceholder, lineNumbers, drawSelection, highlightActiveLine, highlightSpecialChars, Decoration, GutterMarker, gutter, tooltips, hoverTooltip, showTooltip } from '@codemirror/view'
import { defaultKeymap, indentWithTab, history, historyKeymap } from '@codemirror/commands'
import { python } from '@codemirror/lang-python'
import { sql as sqlLang } from '@codemirror/lang-sql'
import { syntaxHighlighting, indentOnInput, bracketMatching, HighlightStyle } from '@codemirror/language'
import { autocompletion, closeBrackets, closeBracketsKeymap, startCompletion, snippetCompletion } from '@codemirror/autocomplete'
import { tags } from '@lezer/highlight'
import { apiFetch } from '../services/apiFetch'
// ─── Helper-function metadata (autocomplete) ────────────────────────────
// The pre-loaded notebook helpers (read_s3_csv, plot_bar, ...) are the SAME for
// every cell/session (fixed at build time), so we fetch /proxy/helpers and share
// the result across all editors via a module-level cache. Each entry:
//   { name, category, signature, params:[{name,type,default,optional}], returns,
//     summary, doc }
//
// IMPORTANT — only a NON-EMPTY result is cached. A freshly-launched VM is cold and
// its first /proxy/helpers can return non-ok or an empty list; the OLD code cached
// that empty list (and kept the resolved-empty promise) forever, so helper
// autocomplete stayed empty until a full page reload landed on a warm VM. We now
// treat empty/failed as "not loaded yet" (retryable) and re-fetch when the session
// changes, so type-ahead self-heals once the VM is warm — no reload needed.
// SHARED, self-retrying loader. A freshly-launched VM is cold: its first
// /proxy/helpers can 404 (VM not yet registered in the proxy session map) or
// return an empty list. We must keep trying until it's warm — but the retry MUST
// be owned HERE, at the module level, NOT per-editor. Previously every CellEditor
// ran its own retry loop, so a 24-cell notebook fired ~24 parallel /proxy/helpers
// requests each poll (the fan-out that produced dozens of 404s on open). Now the
// retry lives inside ONE shared promise: all editors await the same in-flight
// fetch, so it's a SINGLE request stream regardless of cell count. The promise
// resolves only once helpers actually load (or after a bounded number of tries).
let _helpersPromise = null
let _helpersCache = []
let _helpersSession = null   // session the cache was populated for

async function _fetchHelpersWithRetry(sessionId) {
  for (let attempt = 1; attempt <= 10; attempt++) {
    // Bail if the session changed out from under us (tab switch / relaunch).
    if (_helpersSession !== sessionId) return _helpersCache
    try {
      const r = await apiFetch(`/proxy/helpers`, { headers: { 'X-Session-Id': sessionId } })
      if (r.ok) {
        const data = await r.json()
        const list = Array.isArray(data.helpers) ? data.helpers : []
        if (list.length) {
          _helpersCache = list      // latch the real, non-empty result
          return list
        }
      }
      // Non-ok (e.g. 404 VM-not-ready) or empty (cold VM) → wait and retry.
    } catch {
      // transient network error → wait and retry
    }
    await new Promise(res => setTimeout(res, Math.min(500 + attempt * 750, 5000)))
  }
  return _helpersCache  // gave up (still empty) — a later session change re-arms
}

function loadHelpers(sessionId) {
  // Serve the cache only if it's non-empty AND belongs to this session.
  if (_helpersCache.length && _helpersSession === sessionId) {
    return Promise.resolve(_helpersCache)
  }
  if (!sessionId) return Promise.resolve([])
  // A new session invalidates a prior (possibly empty) in-flight fetch/cache, so
  // the shared promise re-arms exactly once for the new session.
  if (_helpersSession !== sessionId) {
    _helpersSession = sessionId
    _helpersCache = []
    _helpersPromise = null
  }
  // ONE shared, self-retrying promise for this session — every editor awaits it.
  if (_helpersPromise) return _helpersPromise
  _helpersPromise = _fetchHelpersWithRetry(sessionId)
  return _helpersPromise
}

// Build a CodeMirror snippet template from a param list so selecting a function
// inserts callable args with tab-through placeholders, skipping *args/**kwargs and
// optional tails. e.g. {name:'read_s3_csv',params:[bucket,key]} → "read_s3_csv(${bucket}, ${key})".
// Shared by helper functions AND catalog functions/methods (same params shape).
function callSnippetTemplate(name, params) {
  // Insert the REQUIRED args (no default, not *args/**kwargs) as tab-through
  // placeholders. Keyword-only params (after a bare * in the Python signature)
  // MUST be named at the call site, so scaffold them as `name=${name}` — this is
  // what prevents "takes 0 positional arguments but 1 was given" when the user
  // fills the placeholder positionally. Optional params stay out (the signature
  // hint still shows them); positional-or-keyword requireds insert bare.
  const required = (params || []).filter(p => !p.name.startsWith('*') && !p.optional)
  const inner = required.map(p => p.kw_only ? `${p.name}=\${${p.name}}` : `\${${p.name}}`).join(', ')
  return `${name}(${inner})`
}

// ─── Completion catalog (autocomplete) ──────────────────────────────────
// Static, build-time introspection of builtins + pre-baked libraries served by
// GET /completions-catalog. Like helpers, it's identical for every session, so we
// fetch ONCE and share via a module-level cache. Shape:
//   { builtins:[entry], modules:{alias:[entry]}, classes:{name:[entry]} }
// entry = { name, kind, signature, params[], returns, summary }
// Same shared-retry pattern as loadHelpers (see that comment): ONE self-retrying
// promise per session that all editors await, instead of N per-editor retry loops.
let _catalogPromise = null
let _catalogCache = null  // null = not loaded; object = loaded with real content
let _catalogSession = null

async function _fetchCatalogWithRetry(sessionId) {
  for (let attempt = 1; attempt <= 10; attempt++) {
    if (_catalogSession !== sessionId) return _catalogCache
    try {
      const r = await apiFetch(`/proxy/completions-catalog`, { headers: { 'X-Session-Id': sessionId } })
      if (r.ok) {
        const data = await r.json()
        const cat = data && typeof data === 'object'
          ? { builtins: data.builtins || [], modules: data.modules || {}, classes: data.classes || {} }
          : null
        const hasContent = cat && (cat.builtins.length || Object.keys(cat.modules).length || Object.keys(cat.classes).length)
        if (hasContent) {
          _catalogCache = cat  // latch real content
          return cat
        }
      }
      // Non-ok / empty (cold VM) → wait and retry.
    } catch {
      // transient → retry
    }
    await new Promise(res => setTimeout(res, Math.min(500 + attempt * 750, 5000)))
  }
  return _catalogCache  // gave up — best-effort; helpers are the user-visible gap
}

function loadCatalog(sessionId) {
  if (_catalogCache && _catalogSession === sessionId) return Promise.resolve(_catalogCache)
  if (!sessionId) return Promise.resolve(null)
  if (_catalogSession !== sessionId) {
    _catalogSession = sessionId
    _catalogCache = null
    _catalogPromise = null
  }
  if (_catalogPromise) return _catalogPromise
  _catalogPromise = _fetchCatalogWithRetry(sessionId)
  return _catalogPromise
}

// ─── Signature help (parameter hints) ───────────────────────────────────
// The autocomplete popup shows a function's signature while you're TYPING the
// name, but disappears the moment you type '('. Signature help fills that gap:
// once the caret is inside foo( … ), it shows a tooltip with the full signature
// and BOLDS the parameter you're currently on, advancing as you type commas —
// the behaviour every mainstream editor (VS Code, PyCharm, …) provides.
//
// CodeMirror 6 ships no signature-help out of the box, so we build it from the
// SAME metadata the completion source uses (helpers + static catalog). The
// lookup + tooltip are driven off module-level refs supplied at extension-build
// time so the closure always sees the latest loaded metadata.

// Find a function's parameter metadata by bare name across every source we know:
// pre-loaded helpers, catalog builtins, catalog module functions, and catalog
// class methods. Returns { name, params, returns } or null. First match wins;
// helpers rank first since they're the notebook's own API.
function lookupSignature(name, helpers, catalog) {
  if (!name) return null
  for (const h of (helpers || [])) {
    if (h.name === name) return { name, params: h.params || [], returns: h.returns }
  }
  if (catalog) {
    for (const b of (catalog.builtins || [])) {
      if (b.name === name) return { name, params: b.params || [], returns: b.returns }
    }
    for (const fns of Object.values(catalog.modules || {})) {
      for (const f of fns) {
        if (f.name === name) return { name, params: f.params || [], returns: f.returns }
      }
    }
    for (const methods of Object.values(catalog.classes || {})) {
      for (const m of methods) {
        if (m.name === name) return { name, params: m.params || [], returns: m.returns }
      }
    }
  }
  return null
}

// Scan backwards from `pos` to locate the innermost function call the caret sits
// inside, and which argument index it's on. Walks the text tracking bracket depth
// and skipping string literals, so nested calls, brackets, and commas inside
// strings/sub-calls don't mis-count. Returns { name, callFrom, argIndex } or null.
//   e.g.  "download_s3("bucket", key|"   → { name:'download_s3', argIndex:1 }
function findEnclosingCall(state, pos) {
  const text = state.doc.toString()
  let depth = 0          // net ) ] } seen while scanning left (closed calls to skip)
  let argIndex = 0       // commas at the call's own depth, left of the caret
  let i = pos - 1
  // A string scanner that, when we hit a quote scanning leftward, jumps to the
  // matching opening quote so we never interpret commas/parens inside a literal.
  const skipStringBackward = (idx, quote) => {
    let j = idx - 1
    while (j >= 0) {
      if (text[j] === quote && text[j - 1] !== '\\') return j - 1
      j--
    }
    return -1
  }
  while (i >= 0) {
    const ch = text[i]
    if (ch === '"' || ch === "'") { i = skipStringBackward(i, ch); continue }
    if (ch === ')' || ch === ']' || ch === '}') { depth++; i--; continue }
    if (ch === '(' || ch === '[' || ch === '{') {
      if (depth > 0) { depth--; i--; continue }
      // Unmatched opener at our level. Only '(' preceded by an identifier is a call.
      if (ch !== '(') return null
      let k = i - 1
      while (k >= 0 && /\s/.test(text[k])) k--
      let end = k
      while (k >= 0 && /[\w.]/.test(text[k])) k--
      const ident = text.slice(k + 1, end + 1)
      if (!ident) return null
      // For dotted/attribute calls (df.merge, pd.read_csv) hint on the last segment.
      const name = ident.split('.').pop()
      return { name, callFrom: i, argIndex }
    }
    if (ch === ',' && depth === 0) { argIndex++; i--; continue }
    i--
  }
  return null
}

// Render the signature tooltip DOM: "name(p0, p1, …) -> ret" with the active
// parameter wrapped in a bold span. Params past the last one (e.g. while typing
// into *args) clamp to the final param so the hint doesn't vanish.
function renderSignatureDOM(sig, argIndex) {
  const dom = document.createElement('div')
  dom.className = 'cm-signature-tooltip'
  const params = sig.params || []
  const nameEl = document.createElement('span')
  nameEl.className = 'cm-signature-name'
  nameEl.textContent = sig.name
  dom.appendChild(nameEl)
  dom.appendChild(document.createTextNode('('))
  if (params.length === 0) {
    dom.appendChild(document.createTextNode(')'))
  } else {
    // Clamp the active index: a trailing *args/**kwargs stays highlighted for any
    // further args; otherwise highlight the last positional once we run past it.
    let active = argIndex
    const starIdx = params.findIndex(p => p.name && p.name.startsWith('*'))
    if (active > params.length - 1) active = starIdx >= 0 ? starIdx : params.length - 1
    params.forEach((p, idx) => {
      if (idx > 0) dom.appendChild(document.createTextNode(', '))
      const span = document.createElement('span')
      const cls = ['cm-signature-param']
      if (idx === active) cls.push('cm-signature-param-active')
      if (p.kw_only) cls.push('cm-signature-param-kwonly')
      span.className = cls.join(' ')
      const hasDefault = p.default !== undefined && p.default !== null && p.default !== ''
      let label
      if (p.kw_only) {
        // Keyword-only params (after a bare * in the signature) MUST be passed by
        // name. Render as `name=<default>` (or `name=` when required) so the "must
        // name it" requirement is obvious — this is exactly the mistake that raises
        // "takes 0 positional arguments but 1 was given".
        label = `${p.name}=${hasDefault ? p.default : ''}`
      } else {
        label = p.name || ''
        if (p.type) label += `: ${p.type}`
        if (hasDefault) label += ` = ${p.default}`
        else if (p.optional) label += ' = …'
      }
      span.textContent = label
      dom.appendChild(span)
    })
    dom.appendChild(document.createTextNode(')'))
  }
  if (sig.returns) {
    const ret = document.createElement('span')
    ret.className = 'cm-signature-return'
    ret.textContent = ` -> ${sig.returns}`
    dom.appendChild(ret)
  }
  return dom
}

// Build the signature-help extension. `getMeta()` returns the current
// { helpers, catalog } so the tooltip always reflects the latest loaded metadata
// (they load asynchronously after a cold VM start). Enabled for Python only.
function signatureHelp(getMeta, isEnabled) {
  const field = StateField.define({
    create: () => null,
    update(value, tr) {
      if (!isEnabled()) return null
      // Recompute on any doc change or selection move.
      if (!tr.docChanged && !tr.selection && value === null) return value
      const state = tr.state
      const sel = state.selection.main
      if (!sel.empty) return null
      const call = findEnclosingCall(state, sel.head)
      if (!call) return null
      const { helpers, catalog } = getMeta()
      const sig = lookupSignature(call.name, helpers, catalog)
      if (!sig) return null
      return {
        pos: sel.head,
        above: true,
        strictSide: false,
        arrow: false,
        create() {
          return { dom: renderSignatureDOM(sig, call.argIndex) }
        },
      }
    },
    provide: f => showTooltip.computeN([f], state => {
      const t = state.field(f)
      return t ? [t] : []
    }),
  })
  return field
}

// ─── Theme: matches our CSS custom properties ───────────────────────────
const editorTheme = EditorView.theme({
  '&': {
    fontSize: '13px',
    fontFamily: 'var(--font-mono)',
    backgroundColor: 'transparent',
  },
  '.cm-content': {
    padding: '12px',
    caretColor: 'var(--cm-cursor)',
    lineHeight: '1.6',
    minHeight: '40px',
  },
  '&.cm-focused': {
    outline: 'none',
  },
  '.cm-line': {
    padding: '0',
  },
  '.cm-gutters': {
    display: 'none', // We use our own gutter (cell numbers)
  },
  '.cm-activeLine': {
    backgroundColor: 'var(--cm-active-line)',
  },
  '.cm-selectionBackground, ::selection': {
    backgroundColor: 'var(--cm-selection) !important',
  },
  '.cm-cursor': {
    borderLeftColor: 'var(--cm-cursor)',
    borderLeftWidth: '2px',
  },
  '.cm-matchingBracket': {
    backgroundColor: 'var(--cm-bracket-bg)',
    outline: '1px solid var(--cm-bracket-outline)',
  },
  '.cm-tooltip': {
    backgroundColor: 'var(--surface-3)',
    border: '1px solid var(--border-default)',
    borderRadius: 'var(--radius-md)',
    boxShadow: 'var(--shadow-lg)',
  },
  '.cm-tooltip-autocomplete': {
    '& > ul': {
      fontFamily: 'var(--font-mono)',
      fontSize: '12px',
      maxHeight: '200px',
    },
    '& > ul > li': {
      padding: '4px 8px',
      color: 'var(--text-primary)',
    },
    '& > ul > li[aria-selected]': {
      backgroundColor: 'var(--accent-primary-subtle)',
      color: 'var(--text-primary)',
    },
  },
  '.cm-completionIcon': {
    opacity: '0.6',
    marginRight: '4px',
  },
  '.cm-completionLabel': {
    color: 'var(--text-primary)',
  },
  '.cm-completionDetail': {
    color: 'var(--text-muted)',
    fontStyle: 'italic',
    marginLeft: '8px',
  },
  '.cm-placeholder': {
    color: 'var(--text-muted)',
    fontStyle: 'normal',
  },
  // Scrollbar
  '.cm-scroller': {
    overflow: 'hidden !important',
  },
}, { dark: true }) // Will be overridden per-theme via class

// ─── Syntax highlighting colors using theme variables ───────────────────
const syntaxColors = HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--cm-keyword)', fontWeight: 'var(--cm-keyword-weight)' },
  { tag: tags.string, color: 'var(--cm-string)' },
  { tag: tags.number, color: 'var(--cm-number)' },
  { tag: tags.bool, color: 'var(--cm-bool)' },
  { tag: tags.null, color: 'var(--cm-bool)' },
  { tag: tags.comment, color: 'var(--cm-comment)', fontStyle: 'var(--cm-comment-style)' },
  { tag: tags.variableName, color: 'var(--cm-text)' },
  { tag: tags.definition(tags.variableName), color: 'var(--cm-definition)' },
  { tag: tags.propertyName, color: 'var(--cm-property)' },
  { tag: tags.className, color: 'var(--cm-class)' },
  { tag: tags.operator, color: 'var(--cm-operator)' },
  { tag: tags.punctuation, color: 'var(--cm-operator)' },
  { tag: tags.bracket, color: 'var(--cm-bracket)' },
  { tag: tags.meta, color: 'var(--cm-meta)' },
  { tag: tags.typeName, color: 'var(--cm-type)' },
  { tag: tags.attributeName, color: 'var(--cm-attribute)' },
])

// ─── Explanation line-range highlight ───────────────────────────────────
// A StateField of line decorations (never touches the doc). Set/cleared through
// `setExplainRange`; mapped through edits; clamped to the current doc.
const setExplainRange = StateEffect.define()
const explainRangeField = StateField.define({
  create: () => Decoration.none,
  update(deco, tr) {
    deco = deco.map(tr.changes)
    for (const e of tr.effects) {
      if (!e.is(setExplainRange)) continue
      if (!e.value) { deco = Decoration.none; continue }
      const doc = tr.state.doc
      const from = Math.max(1, e.value.start)
      const to = Math.min(doc.lines, e.value.end)
      const ranges = []
      for (let n = from; n <= to; n++) {
        ranges.push(Decoration.line({ class: 'cm-explain-range' }).range(doc.line(n).from))
      }
      deco = ranges.length ? Decoration.set(ranges) : Decoration.none
    }
    return deco
  },
  provide: (f) => EditorView.decorations.from(f),
})

// ─── Compartments for dynamic reconfiguration ───────────────────────────
// (created per-instance so they don't conflict)

// ─── Search highlight — using CM6's native search extension ─────────────
import { search, setSearchQuery as cmSetSearchQuery, SearchQuery, findNext } from '@codemirror/search'

// ─── Annotation to mark external (programmatic) changes ─────────────────
// When we dispatch changes from the value prop, we annotate the transaction
// so the updateListener knows not to call onCodeChange back (prevents loop).
// This is the same pattern used by @uiw/react-codemirror.
const ExternalChange = Annotation.define()

export default function CellEditor({
  code = '',
  language = 'python',
  placeholder = '',
  onCodeChange,
  onExecute,
  onFocus,
  readOnly = false,
  variables = [],
  dataSources = { items: [], schemas: {} },
  sessionId = null,
  searchQuery = '',
  searchActiveOccurrence = -1,
  // Error line highlighting: 1-indexed line to mark (null = none), the message for
  // the hover tooltip, whether the gutter marker is enabled (user toggle), and a
  // click handler for the marker (wired to AI Fix).
  errorLine = null,
  // Secondary cell-local call-chain lines (helper frames leading to the crash). The
  // deepest frame is `errorLine` (primary); these are the call sites, marked subtly.
  errorChainLines = [],
  errorMessage = '',
  showErrorGutter = true,
  onFixError = null,
  // Ruff lint diagnostics for THIS cell (cell-relative positions), as returned by
  // the /lint endpoint: [{line, col, end_line, end_col, code, message, severity,
  // fixable}]. Rendered as inline squiggles with a hover tooltip. [] / null clears.
  lintDiagnostics = null,
  // Explanation line-range highlight (see above); null = none.
  highlight = null,
}) {
  const containerRef = useRef(null)
  const viewRef = useRef(null)
  const compartmentsRef = useRef(null)

  // Lazily create compartments — only once, tied to the editor lifetime
  if (!compartmentsRef.current) {
    compartmentsRef.current = {
      lang: new Compartment(),
      readOnly: new Compartment(),
      completion: new Compartment(),
      error: new Compartment(),  // error-line decoration + gutter marker
      lint: new Compartment(),   // ruff lint squiggles + hover tooltip
    }
  }
  const { lang: langCompartment, readOnly: readOnlyCompartment, completion: completionCompartment, error: errorCompartment, lint: lintCompartment } = compartmentsRef.current

  // Stable refs so the (mount-only) error extension builder reads current values.
  const errorMessageRef = useRef(errorMessage)
  const onFixErrorRef = useRef(onFixError)
  const errorChainLinesRef = useRef(errorChainLines)
  useEffect(() => { errorMessageRef.current = errorMessage }, [errorMessage])
  useEffect(() => { onFixErrorRef.current = onFixError }, [onFixError])
  useEffect(() => { errorChainLinesRef.current = errorChainLines }, [errorChainLines])

  // Latest lint diagnostics for the hover-tooltip closure (built once at mount).
  const lintDiagnosticsRef = useRef(lintDiagnostics)
  useEffect(() => { lintDiagnosticsRef.current = lintDiagnostics }, [lintDiagnostics])

  // Stable refs for callbacks (avoid recreating editor on every render)
  const onCodeChangeRef = useRef(onCodeChange)
  const onExecuteRef = useRef(onExecute)
  const onFocusRef = useRef(onFocus)
  const variablesRef = useRef(variables)
  const dataSourcesRef = useRef(dataSources)
  const sessionIdRef = useRef(sessionId)
  const searchQueryRef = useRef(searchQuery)
  // Current language for the signature-help extension (built once at mount; reads
  // this ref so it enables/disables live when the cell language switches).
  const languageRef = useRef(language)
  // Helper-function metadata + static completion catalog (both shared module
  // caches; refs so the stable completionSource closure sees the latest once loaded).
  const helpersRef = useRef(_helpersCache)
  const catalogRef = useRef(_catalogCache)

  useEffect(() => { onCodeChangeRef.current = onCodeChange }, [onCodeChange])
  useEffect(() => { onExecuteRef.current = onExecute }, [onExecute])
  useEffect(() => { onFocusRef.current = onFocus }, [onFocus])
  useEffect(() => { variablesRef.current = variables }, [variables])
  useEffect(() => { dataSourcesRef.current = dataSources }, [dataSources])
  useEffect(() => { sessionIdRef.current = sessionId }, [sessionId])
  // Load helper metadata + completion catalog once a session exists (module-cached,
  // so only the first editor actually fetches). Populates helpersRef/catalogRef for
  // the completion source. A freshly-launched VM is cold, so the first fetch can
  // come back empty; loadHelpers/loadCatalog leave empty results UNcached, and this
  // effect RETRIES with backoff until each is populated (or the session changes /
  // the editor unmounts). This is what makes helper type-ahead self-heal after a
  // cold start instead of staying empty until a full page reload.
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    // The retry-until-warm now lives INSIDE loadHelpers/loadCatalog as ONE shared
    // per-session promise, so every editor just awaits it — no per-editor retry
    // loop (which caused N parallel /proxy/helpers requests on open). Whichever
    // editor calls first drives the single fetch; the rest ride the same promise.
    ;(async () => {
      const [h, c] = await Promise.all([loadHelpers(sessionId), loadCatalog(sessionId)])
      if (!alive) return
      if (Array.isArray(h) && h.length) helpersRef.current = h
      if (c) catalogRef.current = c
    })()
    return () => { alive = false }
  }, [sessionId])

  // Language extension
  const getLangExtension = useCallback((lang) => {
    if (lang === 'sql') return sqlLang()
    return python()
  }, [])

  // ─── Error-line extension builder ─────────────────────────────────────
  // Returns the extension(s) that mark `line` (1-indexed) as the error line: a
  // full-line background decoration (always, when there's an error line) plus,
  // when `withGutter` is true, a clickable red marker in the line-number gutter.
  // Returns [] when there's no error line — clearing all markers. Guards against a
  // stale line number that exceeds the current doc (after edits/shrink).
  const buildErrorExtension = useCallback((line, withGutter) => {
    if (!line || line < 1) return []
    const msg = errorMessageRef.current || 'Error on this line'
    // Secondary call-chain lines (helper frames leading to the crash), if any. Dedup
    // and drop the primary line so it isn't double-decorated.
    const chain = Array.from(new Set(errorChainLinesRef.current || []))
      .filter(n => typeof n === 'number' && n >= 1 && n !== line)

    const lineDecoField = EditorView.decorations.compute([], (state) => {
      const decos = []
      if (line <= state.doc.lines) {
        decos.push(
          Decoration.line({
            class: 'cm-error-line',
            attributes: { title: msg },  // native hover tooltip with the error message
          }).range(state.doc.line(line).from),
        )
      }
      // Subtler highlight for each call-chain line (the path to the crash point).
      for (const cl of chain) {
        if (cl > state.doc.lines) continue
        decos.push(
          Decoration.line({
            class: 'cm-error-chain-line',
            attributes: { title: 'In the call chain that raised this error' },
          }).range(state.doc.line(cl).from),
        )
      }
      // Decoration.set requires ranges sorted by position.
      decos.sort((a, b) => a.from - b.from)
      return decos.length ? Decoration.set(decos) : Decoration.none
    })

    if (!withGutter) return [lineDecoField]

    // Gutter marker — a red dot on the error line. Clicking it triggers AI Fix.
    const marker = new class extends GutterMarker {
      toDOM() {
        const el = document.createElement('span')
        el.className = 'cm-error-gutter-dot'
        el.title = onFixErrorRef.current ? `${msg}\n(click to fix with AI)` : msg
        if (onFixErrorRef.current) {
          el.style.cursor = 'pointer'
          el.onmousedown = (e) => { e.preventDefault(); e.stopPropagation(); onFixErrorRef.current() }
        }
        return el
      }
    }()

    const errorGutter = gutter({
      class: 'cm-error-gutter',
      markers: (view) => {
        if (line > view.state.doc.lines) return RangeSet.empty
        const pos = view.state.doc.line(line).from
        return RangeSet.of([marker.range(pos)])
      },
    })

    return [lineDecoField, errorGutter]
  }, [])

  // ─── Lint-squiggle extension builder ──────────────────────────────────
  // Underlines each ruff diagnostic (cell-relative line/col, 1-indexed) with a
  // wavy mark and attaches ONE hover tooltip that reports every diagnostic under
  // the cursor as "CODE: message". Positions are clamped to the current doc so a
  // diagnostic that's gone stale after an edit can't throw. Returns [] when there
  // are no diagnostics, which clears the layer.
  const buildLintExtension = useCallback((diagnostics) => {
    const diags = Array.isArray(diagnostics) ? diagnostics : []
    if (diags.length === 0) return []

    // Resolve a {line, col[, end_line, end_col]} diagnostic to absolute [from, to)
    // document offsets. col is 1-indexed (ruff); CM columns are 0-indexed from the
    // line start. Falls back to marking a single character when there's no end.
    const resolve = (state, d) => {
      const line = d.line
      if (!line || line < 1 || line > state.doc.lines) return null
      const l = state.doc.line(line)
      const from = Math.min(l.from + Math.max((d.col || 1) - 1, 0), l.to)
      let to = from + 1
      if (d.end_line && d.end_col && d.end_line >= 1 && d.end_line <= state.doc.lines) {
        const el = state.doc.line(d.end_line)
        to = Math.min(el.from + Math.max((d.end_col || 1) - 1, 0), el.to)
      }
      // Guarantee a non-empty, in-bounds range so Decoration.mark never throws.
      if (to <= from) to = Math.min(from + 1, l.to > from ? l.to : from + 1)
      if (to <= from) return null  // empty line / EOF token — nothing to underline
      return { from, to }
    }

    const decoField = EditorView.decorations.compute([], (state) => {
      const ranges = []
      for (const d of diags) {
        const r = resolve(state, d)
        if (!r) continue
        ranges.push(
          Decoration.mark({
            class: 'cm-lint-squiggle',
            attributes: { title: d.code ? `${d.code}: ${d.message}` : d.message },
          }).range(r.from, r.to),
        )
      }
      // Decoration.set requires ranges sorted by `from` (then by side); sort to be safe.
      ranges.sort((a, b) => a.from - b.from || a.to - b.to)
      return ranges.length ? Decoration.set(ranges, true) : Decoration.none
    })

    // Rich hover tooltip (CM-native, escapes the cell's overflow via tooltips()).
    // Reads diagnostics from the ref so it always reflects the latest lint pass.
    const hover = hoverTooltip((view, pos) => {
      const current = lintDiagnosticsRef.current
      const list = Array.isArray(current) ? current : []
      const hits = []
      let lo = pos, hi = pos
      for (const d of list) {
        const r = resolve(view.state, d)
        if (r && pos >= r.from && pos <= r.to) {
          hits.push(d)
          lo = Math.min(lo, r.from)
          hi = Math.max(hi, r.to)
        }
      }
      if (hits.length === 0) return null
      return {
        pos: lo,
        end: hi,
        above: true,
        create() {
          const dom = document.createElement('div')
          dom.className = 'cm-lint-tooltip'
          for (const d of hits) {
            const row = document.createElement('div')
            row.className = 'cm-lint-tooltip-row'
            const code = document.createElement('span')
            code.className = 'cm-lint-tooltip-code'
            code.textContent = d.code || 'lint'
            row.appendChild(code)
            row.appendChild(document.createTextNode(' ' + (d.message || '')))
            dom.appendChild(row)
          }
          return { dom }
        },
      }
    }, { hoverTime: 200 })

    return [decoField, hover]
  }, [])

  // ─── Autocomplete source ─────────────────────────────────────────────
  const completionSource = useCallback(async (context) => {
    const line = context.state.doc.lineAt(context.pos)
    const textBefore = line.text.slice(0, context.pos - line.from)

    // --- Dot completion ---
    const dotMatch = textBefore.match(/(\w+)\.\w*$/)
    if (dotMatch) {
      const prefix = dotMatch[1]
      const partial = textBefore.match(/\.(\w*)$/)?.[1] || ''

      // SQL: schema.table completion (e.g. database_name. or dynamodb.)
      if (language === 'sql') {
        const schemas = dataSourcesRef.current?.schemas || {}
        const columns = dataSourcesRef.current?.columns || {}
        const tables = schemas[prefix]
        if (tables) {
          const options = tables
            .filter(t => t.toLowerCase().startsWith(partial.toLowerCase()) || partial === '')
            .map(t => {
              // Quote table name if it contains non-identifier characters (hyphens, spaces, etc.)
              const needsQuotes = /[^a-zA-Z0-9_]/.test(t)
              return {
                label: needsQuotes ? `"${t}"` : t,
                type: 'text',
                detail: `table in ${prefix}`,
              }
            })
          if (options.length > 0) {
            return { from: context.pos - partial.length, validFor: /^[\w"]*$/, options }
          }
        }
        // SQL: table.column completion (e.g. sales_data.revenue)
        const colNames = columns[prefix]
        if (colNames) {
          const options = colNames
            .filter(c => c.toLowerCase().startsWith(partial.toLowerCase()) || partial === '')
            .map(c => ({ label: c, type: 'property', detail: `column in ${prefix}` }))
          if (options.length > 0) {
            return { from: context.pos - partial.length, validFor: /^\w*$/, options }
          }
        }
      }

      // Python dot-completion. Three complementary sources, most-precise first:
      //   (a) MODULE functions from the static catalog when the prefix is a known
      //       module alias (pd., np., px., ...) — precise + signature snippets, and
      //       works WITHOUT running the cell.
      //   (b) LIVE introspection via /introspect for real variables (df., after the
      //       cell has run) — exact, correctly scoped.
      //   (c) COMMON class-method pool from the catalog (DataFrame/Series/str/...),
      //       appended below live results so there's still guidance before a run.
      if (language === 'python') {
        const pl = partial.toLowerCase()
        const catalog = catalogRef.current
        const dotOpts = []
        const seen = new Set()

        // (a) Known module alias → its catalog functions (precise).
        const modFns = catalog?.modules?.[prefix]
        if (modFns && modFns.length) {
          modFns
            .filter(f => (partial === '' || f.name.toLowerCase().startsWith(pl)) && f.name !== partial)
            .forEach(f => {
              if (seen.has(f.name)) return
              seen.add(f.name)
              dotOpts.push(snippetCompletion(callSnippetTemplate(f.name, f.params), {
                label: f.name, type: 'function', detail: f.signature || '()',
                info: f.summary || undefined, boost: 2,
              }))
            })
          // A module alias is unambiguous — return its functions directly.
          if (dotOpts.length) return { from: context.pos - partial.length, validFor: /^\w*$/, options: dotOpts }
        }

        // (b) Live introspection on a real variable (needs the cell to have run).
        if (sessionIdRef.current) {
          try {
            const headers = { 'Content-Type': 'application/json', 'X-Session-Id': sessionIdRef.current }
            const resp = await apiFetch(`/proxy/introspect`, {
              method: 'POST', headers,
              body: JSON.stringify({ variable: prefix, partial }),
            })
            if (resp.ok) {
              const data = await resp.json()
              for (const c of (data.completions || [])) {
                if (seen.has(c.name)) continue
                seen.add(c.name)
                dotOpts.push({
                  label: c.name,
                  type: c.type || 'property',
                  detail: c.detail || '',
                  boost: c.name.startsWith('_') ? -1 : 1,  // live = boost 1 (above pooled methods)
                })
              }
            }
          } catch (err) {
            if (err.name !== 'AbortError') {
              console.warn('[autocomplete] Introspect failed for', prefix, ':', err.message)
            }
          }
        }

        // (c) Common class-method pool (all catalogued classes) — de-duped against
        // what we already have, ranked BELOW live results (boost -0.5) since it's a
        // heuristic (we don't know prefix's real type before a run).
        if (catalog?.classes) {
          for (const methods of Object.values(catalog.classes)) {
            for (const m of methods) {
              if (m.name !== partial && (partial === '' || m.name.toLowerCase().startsWith(pl)) && !seen.has(m.name)) {
                seen.add(m.name)
                dotOpts.push(snippetCompletion(callSnippetTemplate(m.name, m.params), {
                  label: m.name, type: 'method', detail: m.signature || '()',
                  info: m.summary || undefined, boost: -0.5,
                }))
              }
            }
          }
        }

        if (dotOpts.length) return { from: context.pos - partial.length, validFor: /^\w*$/, options: dotOpts }
      }

      return null
    }

    // --- Python: bracket column completion (df[' or df[" → suggest columns) ---
    if (language === 'python') {
      const bracketMatch = textBefore.match(/(\w+)\[['"](\w*)$/)
      if (bracketMatch) {
        const varName = bracketMatch[1]
        const partial = bracketMatch[2]
        const columns = dataSourcesRef.current?.columns || {}
        // Check if this variable has known columns (from catalog or namespace)
        const colNames = columns[varName]
        if (colNames) {
          const options = colNames
            .filter(c => c.toLowerCase().startsWith(partial.toLowerCase()) || partial === '')
            .map(c => ({ label: c, type: 'property', detail: `column in ${varName}` }))
          if (options.length > 0) {
            return { from: context.pos - partial.length, validFor: /^\w*$/, options }
          }
        }
      }
    }

    // --- SQL: column suggestions after SELECT/WHERE/ORDER BY (context-aware) ---
    if (language === 'sql') {
      const columns = dataSourcesRef.current?.columns || {}
      // Detect if we're in a position where column names make sense
      const sqlContext = textBefore.match(/\b(?:SELECT|WHERE|AND|OR|ORDER\s+BY|GROUP\s+BY|HAVING|ON|SET)\s+(?:.*,\s*)?(\w*)$/i)
      if (sqlContext) {
        const partial = sqlContext[1] || ''
        // Find which table is referenced in the FROM clause of this cell
        const fullDoc = context.state.doc.toString()
        const fromMatch = fullDoc.match(/\bFROM\s+(?:dynamodb\.)?"?(\w[\w-]*)"?/i)
          || fullDoc.match(/\bFROM\s+\w+\.(\w+)/i)
          || fullDoc.match(/\bFROM\s+(\w+)/i)
        if (fromMatch) {
          const tableName = fromMatch[1]
          const colNames = columns[tableName]
          if (colNames && partial.length >= 1) {
            const options = colNames
              .filter(c => c.toLowerCase().startsWith(partial.toLowerCase()))
              .map(c => ({ label: c, type: 'property', detail: `column` }))
            if (options.length > 0) {
              return { from: context.pos - partial.length, validFor: /^\w*$/, options }
            }
          }
        }
      }
    }

    // --- Word-based completion (variables, keywords, datasources) ---
    const wordMatch = textBefore.match(/(\w+)$/)
    if (!wordMatch) return null
    const partial = wordMatch[1]

    // Need at least 2 chars (unless explicit trigger via Ctrl+Space)
    if (partial.length < 2 && !context.explicit) return null

    const options = []

    // Namespace variables (always relevant)
    const vars = variablesRef.current || []
    vars
      .filter(v => v.toLowerCase().startsWith(partial.toLowerCase()) && v !== partial)
      .forEach(v => options.push({ label: v, type: 'variable', boost: 2 }))

    // Data source items (tables, schemas, URIs)
    const dsItems = dataSourcesRef.current?.items || []
    dsItems
      .filter(d => d.label.toLowerCase().startsWith(partial.toLowerCase()) && d.label !== partial)
      .forEach(d => options.push({ label: d.label, type: 'text', detail: d.detail || d.type, boost: 1 }))

    // Pre-loaded HELPER FUNCTIONS (Python only) — read_s3_csv, plot_bar, etc.
    // These live in the VM namespace but aren't language keywords, so we surface
    // them explicitly with their signature (detail), docstring (info), and a
    // snippet apply that scaffolds the required args with tab-through placeholders.
    // Boosted above generic keywords since they're the notebook's own API.
    if (language === 'python') {
      const pl = partial.toLowerCase()
      const helpers = helpersRef.current || []
      helpers
        .filter(h => h.name.toLowerCase().startsWith(pl) && h.name !== partial)
        .forEach(h => options.push(snippetCompletion(callSnippetTemplate(h.name, h.params), {
          label: h.name,
          type: 'function',
          detail: h.signature.replace(h.name, ''),  // just the "(args) -> ret" tail
          info: h.summary || h.doc || undefined,
          boost: 3,
        })))

      // BUILTINS from the static catalog (round, sum, open, sorted, ...) — called
      // bare, so they belong in the word branch. Signature in detail, doc in info,
      // param snippet on apply. Ranked just below helpers.
      const catalog = catalogRef.current
      if (catalog) {
        (catalog.builtins || [])
          .filter(b => b.name.toLowerCase().startsWith(pl) && b.name !== partial)
          .forEach(b => options.push(snippetCompletion(callSnippetTemplate(b.name, b.params), {
            label: b.name,
            type: b.kind === 'builtin' && (b.params || []).length === 0 ? 'variable' : 'function',
            detail: b.signature || '()',
            info: b.summary || undefined,
            boost: 2,
          })))
      }
    }

    // Language-specific keywords (require 2+ chars)
    if (partial.length >= 2) {
      if (language === 'python') {
        const pyKeywords = [
          'print', 'len', 'range', 'enumerate', 'zip', 'map', 'filter',
          'sorted', 'reversed', 'list', 'dict', 'set', 'tuple', 'str', 'int', 'float',
          'bool', 'type', 'isinstance', 'hasattr', 'getattr', 'setattr',
          'import', 'from', 'def', 'class', 'return', 'yield', 'lambda',
          'if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally',
          'with', 'as', 'raise', 'pass', 'break', 'continue', 'and', 'or', 'not', 'in', 'is',
          'True', 'False', 'None',
          'pandas', 'numpy', 'plt', 'pd', 'np', 'px',
          'DataFrame', 'Series', 'read_csv', 'read_parquet', 'read_json', 'read_excel',
          'groupby', 'merge', 'concat', 'pivot_table', 'describe', 'value_counts',
          'matplotlib', 'plotly', 'seaborn', 'boto3',
        ]
        pyKeywords
          .filter(b => b.toLowerCase().startsWith(partial.toLowerCase()) && b !== partial)
          .forEach(b => options.push({ label: b, type: 'keyword' }))
      } else if (language === 'sql') {
        const sqlKeywords = [
          'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'BETWEEN', 'LIKE', 'IS', 'NULL',
          'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
          'CREATE', 'TABLE', 'DROP', 'ALTER', 'ADD', 'COLUMN',
          'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER', 'FULL', 'CROSS', 'ON',
          'GROUP', 'BY', 'ORDER', 'ASC', 'DESC', 'HAVING', 'LIMIT', 'OFFSET',
          'AS', 'DISTINCT', 'COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
          'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
          'UNION', 'ALL', 'EXCEPT', 'INTERSECT',
          'EXISTS', 'ANY', 'SOME',
          'CAST', 'COALESCE', 'NULLIF', 'IFNULL',
          'WITH', 'RECURSIVE',
          'OVER', 'PARTITION', 'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LAG', 'LEAD',
          'VARCHAR', 'INTEGER', 'FLOAT', 'DOUBLE', 'BOOLEAN', 'DATE', 'TIMESTAMP',
          'read_csv', 'read_parquet', 'read_json', 'read_csv_auto',
          'INSTALL', 'LOAD', 'DESCRIBE', 'SHOW', 'TABLES', 'COLUMNS',
          'STRFTIME', 'DATE_TRUNC', 'DATE_PART', 'EXTRACT',
        ]
        sqlKeywords
          .filter(k => k.toLowerCase().startsWith(partial.toLowerCase()) && k.toLowerCase() !== partial.toLowerCase())
          .forEach(k => options.push({ label: k, type: 'keyword' }))
      }
    }

    if (options.length === 0) return null
    return {
      from: context.pos - partial.length,
      validFor: /^\w*$/,
      options,
    }
  }, [language])

  // ─── Create editor instance ───────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return
    if (viewRef.current) return // Already created

    const state = EditorState.create({
      doc: code,
      extensions: [
        // Core
        lineNumbers(),
        highlightActiveLine(),
        highlightSpecialChars(),
        drawSelection(),
        EditorView.lineWrapping,
        indentOnInput(),
        bracketMatching(),
        closeBrackets(),
        history(),
        editorTheme,
        syntaxHighlighting(syntaxColors),

        // Placeholder
        cmPlaceholder(placeholder),

        // Language (in compartment for dynamic switching)
        langCompartment.of(getLangExtension(language)),

        // Read-only (in compartment)
        readOnlyCompartment.of(EditorState.readOnly.of(readOnly)),

        // Render tooltips (incl. the autocomplete popup + its info panel) into
        // document.body, so they escape the editor/cell's `overflow: hidden` (which
        // was clipping the completion info panel at the cell's right edge).
        tooltips({ parent: document.body }),

        // Autocomplete
        completionCompartment.of(
          autocompletion({
            override: [completionSource],
            activateOnTyping: true,
            maxRenderedOptions: 30,
          })
        ),

        // Error-line decoration + gutter marker (in a compartment so it can be
        // reconfigured reactively when the error line / toggle changes; survives
        // the editorVersion remount because it's rebuilt from props on mount).
        errorCompartment.of(buildErrorExtension(errorLine, showErrorGutter)),

        // Ruff lint squiggles + hover tooltip (compartment so it reconfigures when
        // a new lint pass arrives; rebuilt from props on the editorVersion remount).
        lintCompartment.of(buildLintExtension(lintDiagnostics)),

        // Explanation range highlight (line decorations; driven by the `highlight` prop).
        explainRangeField,

        // Signature help (parameter hints) — shows the enclosing call's signature
        // with the active parameter bolded once the caret is inside foo( … ), the
        // gap the autocomplete popup leaves the moment you type '('. Reads the same
        // helper/catalog metadata via refs (stable), Python-only.
        signatureHelp(
          () => ({ helpers: helpersRef.current, catalog: catalogRef.current }),
          () => languageRef.current === 'python',
        ),

        // Search highlighting (CM6 native — panel hidden, highlights only)
        search({ top: false, createPanel: () => ({ dom: document.createElement('span') }) }),

        // Keybindings
        keymap.of([
          {
            key: 'Shift-Enter',
            run: () => {
              if (onExecuteRef.current) onExecuteRef.current()
              return true
            },
          },
          ...closeBracketsKeymap,
          ...historyKeymap,
          indentWithTab,
          ...defaultKeymap,
        ]),

        // Update listener — sync code changes back to parent
        EditorView.updateListener.of((update) => {
          if (update.docChanged && onCodeChangeRef.current &&
              !update.transactions.some(tr => tr.annotation(ExternalChange))) {
            onCodeChangeRef.current(update.state.doc.toString())
          }
          if (update.focusChanged && update.view.hasFocus && onFocusRef.current) {
            onFocusRef.current()
          }
          // Trigger completion on '.' (dot-completion for Python objects and SQL schemas)
          if (update.docChanged) {
            update.changes.iterChanges((fromA, toA, fromB, toB, inserted) => {
              if (inserted.toString() === '.') {
                setTimeout(() => startCompletion(update.view), 10)
              }
            })
          }
        }),
      ],
    })

    const view = new EditorView({ state, parent: containerRef.current })
    viewRef.current = view

    return () => {

      view.destroy()
      viewRef.current = null
    }
  }, []) // Only run once on mount

  // ─── Sync external code changes INTO the editor ───────────────────────
  // Uses the ExternalChange annotation so the updateListener knows to skip
  // calling onCodeChange (prevents echo loop). Same pattern as @uiw/react-codemirror.

  useLayoutEffect(() => {
    const view = viewRef.current
    if (!view) return
    const currentDoc = view.state.doc.toString()
    if (currentDoc !== code) {
      view.dispatch({
        changes: { from: 0, to: currentDoc.length, insert: code },
        annotations: [ExternalChange.of(true)],
      })
    }
  })

  // ─── Switch language dynamically ─────────────────────────────────────
  useEffect(() => {

    const view = viewRef.current
    if (!view) return
    languageRef.current = language
    view.dispatch({
      effects: langCompartment.reconfigure(getLangExtension(language)),
    })
  }, [language, getLangExtension])

  // ─── Update read-only state ───────────────────────────────────────────
  useEffect(() => {

    const view = viewRef.current
    if (!view) return
    view.dispatch({
      effects: readOnlyCompartment.reconfigure(EditorState.readOnly.of(readOnly)),
    })
  }, [readOnly])

  // ─── Update autocomplete when variables change ────────────────────────
  useEffect(() => {

    const view = viewRef.current
    if (!view) return
    view.dispatch({
      effects: completionCompartment.reconfigure(
        autocompletion({
          override: [completionSource],
          activateOnTyping: true,
          maxRenderedOptions: 30,
        })
      ),
    })
  }, [variables, completionSource])

  // ─── Update error-line marker when the error line / chain / toggle changes ────
  // errorChainLines is a fresh array each render, so key on a stable string signature
  // to avoid needless reconfigures while still catching real changes.
  const errorChainSig = Array.isArray(errorChainLines) ? errorChainLines.join(',') : ''
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    view.dispatch({
      effects: errorCompartment.reconfigure(buildErrorExtension(errorLine, showErrorGutter)),
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [errorLine, errorChainSig, showErrorGutter, errorMessage, buildErrorExtension])

  // ─── Update lint squiggles when a new lint pass arrives ───────────────
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    view.dispatch({
      effects: lintCompartment.reconfigure(buildLintExtension(lintDiagnostics)),
    })
  }, [lintDiagnostics, buildLintExtension])

  // ─── Explanation range highlight + reveal ─────────────────────────────
  const hlStart = highlight?.start || 0
  const hlEnd = highlight?.end || 0
  const hlReveal = highlight?.reveal || 0
  const hlRevealLine = highlight?.revealLine || hlStart
  const hlRevealLineRef = useRef(hlRevealLine)
  hlRevealLineRef.current = hlRevealLine
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    view.dispatch({ effects: setExplainRange.of(hlStart ? { start: hlStart, end: hlEnd } : null) })
  }, [hlStart, hlEnd])
  // Scroll only when `reveal` changes (a click), never on hover.
  useEffect(() => {
    const view = viewRef.current
    const target = hlRevealLineRef.current
    if (!view || !hlReveal || !target) return
    const line = view.state.doc.line(Math.min(Math.max(target, 1), view.state.doc.lines))
    view.dispatch({ effects: EditorView.scrollIntoView(line.from, { y: 'start', yMargin: 16 }) })
  }, [hlReveal])

  // ─── Update search highlighting ──────────────────────────────────────
  useEffect(() => {
    searchQueryRef.current = searchQuery

    const view = viewRef.current
    if (!view) return
    // Set the search query (highlights all matches)
    const q = new SearchQuery({
      search: searchQuery || '',
      caseSensitive: false,
      literal: true,
    })
    view.dispatch({ effects: cmSetSearchQuery.of(q) })

    // Advance to the active occurrence within this cell
    if (searchQuery && searchActiveOccurrence >= 0) {
      // Move cursor to start of document, then findNext N times
      view.dispatch({ selection: { anchor: 0 } })
      for (let i = 0; i <= searchActiveOccurrence; i++) {
        findNext(view)
      }
      // Scroll the MATCH itself into view (not just the cell). For a tall cell,
      // centering the cell can leave a match near the bottom off-screen; this
      // scrolls to the exact selection so the highlighted term is always visible.
      // Deferred so it runs after the outer cell scroll settles (that scroll
      // targets the cell top; this refines to the match position).
      const sel = view.state.selection.main
      setTimeout(() => {
        try {
          view.dispatch({ effects: EditorView.scrollIntoView(sel.head, { y: 'center' }) })
        } catch { /* view may have unmounted */ }
      }, 120)
    }
    // `code` is a dep so that after a Replace edits this cell's text, we re-apply
    // the search highlight and re-advance to the active occurrence — otherwise the
    // highlight would stick to the pre-edit position (or vanish) even though the
    // occurrence index is unchanged. The code-sync layoutEffect runs first, so the
    // doc is already updated when this re-runs.
  }, [searchQuery, searchActiveOccurrence, code])

  return (
    <div
      ref={containerRef}
      className={`cell-cm-editor${searchQuery && searchActiveOccurrence >= 0 ? ' cm-searching' : ''}`}
    />
  )
}
