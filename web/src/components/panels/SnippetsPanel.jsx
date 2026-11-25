import { useState, useEffect, useMemo, useCallback } from 'react'
import { IconX, IconCode, IconPlus, IconPencil, IconTrash, IconSave } from '../Icons'
import PanelExpandButton from './PanelExpandButton'
import { PROXY_URL } from '../../config'
import { fetchWithTimeout } from '../../services/fetchWithTimeout'
// GENERATED cold-start fallback — a FULLY SELF-CONTAINED list of ALL helpers
// (name/category/signature/returns/summary/desc/CODE), produced by
// scripts/gen_helpers_block.py from the SAME introspection the live /helpers
// endpoint uses + the curated code below. Used until the VM is warm and the live
// /helpers list replaces it. Regenerate with: python -m scripts.gen_helpers_block
import GENERATED_HELPERS from './helpersFallback.generated.json'
// Curated example CODE (name -> {desc, code}) — the SINGLE source of truth, shared
// with the Python generator (which bakes it into the fallback above). The frontend
// needs it directly for the LIVE /helpers path, which returns metadata only (no
// code), so we layer the curated example (or a synthesized one-liner) on by name.
import CURATED_SNIPPETS from './curated_snippets.json'
import { highlightMatch } from './highlightMatch'

// User snippets live in localStorage (not notebook-scoped, no auth/user table to
// hang them off — same idiom as microvm-theme / microvm-sidebar-panel). Shape:
// [{ id, name, desc, code }].
const USER_SNIPPETS_KEY = 'microvm-user-snippets'
const USER_CATEGORY = 'My Snippets'

function loadUserSnippets() {
  try {
    const raw = localStorage.getItem(USER_SNIPPETS_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter(s => s && s.name && s.code) : []
  } catch {
    return []
  }
}


// RECIPES — multi-STEP, multi-HELPER workflows (not a single call), loaded
// DYNAMICALLY from /recipes/index.json + /recipes/<file> at runtime, exactly like
// the sample notebooks (/samples/*). Each recipe file is
//   { name, desc, cells: [ { type: 'markdown'|'code', code }, ... ] }
// and every cell is inserted as its OWN notebook cell (markdown intro + code steps),
// so the user gets a ready-to-run, self-explaining mini-workflow. Curating them as
// external JSON (not a hardcoded array) means adding/editing a recipe is a data
// change — no component edit — and keeps this file lean.
const RECIPES_INDEX_URL = '/recipes/index.json'
const RECIPES_CATEGORY = 'Recipes'

// Curated EXAMPLE CODE, keyed by helper name (from curated_snippets.json — the
// single source of truth shared with the Python generator). Layered onto the LIVE
// /helpers metadata by name; a helper with no curated example gets a synthesized
// one-liner (synthSnippet). The category/grouping/order/count all come from the
// backend metadata, so adding a helper surfaces it here automatically.
const CURATED_CODE = CURATED_SNIPPETS

// Presentation-only: an emoji per category name (the backend doesn't send icons)
// and a preferred display order. Unknown categories get a default icon and sort
// after the known ones (alphabetically), so nothing is ever hidden.
const CATEGORY_ICONS = {
  'Data Loading': '📦',
  'Data Export': '💾',
  'Visualization': '📊',
  'Generative AI': '🤖',
  // ML split into in-VM ("Local") and remote SageMaker groups (backend section
  // headers "MACHINE LEARNING / LOCAL" and "… / SAGEMAKER"). 🎯 = local, ☁️ = remote.
  'Machine Learning / Local': '🎯',
  'Machine Learning / SageMaker': '☁️',
  'Machine Learning': '🎯',   // legacy fallback (pre-split)
  'Utilities': '🔧',
}
const CATEGORY_ORDER = [
  'Data Loading', 'Data Export', 'Visualization',
  'Generative AI',
  'Machine Learning / Local', 'Machine Learning / SageMaker',
  'Machine Learning',   // legacy fallback (pre-split), kept adjacent
  'Utilities',
]
const DEFAULT_CATEGORY_ICON = '🧩'

// Required positional param names for a helper, from the live /helpers `params`
// array when present, else parsed from the signature string (the generated
// fallback list carries only `signature`, not `params`). Returns a comma-joined
// placeholder arg list, e.g. "bucket, key".
function requiredArgs(h) {
  if (Array.isArray(h.params)) {
    return h.params
      .filter(p => !p.name.startsWith('*') && !p.optional)
      .map(p => p.name).join(', ')
  }
  // Parse from signature: "name(a: str, b=5, *, c=None) -> X" → required = params
  // with no '=' default and before a bare '*' (keyword-only marker), minus self.
  const sig = String(h.signature || '')
  const open = sig.indexOf('(')
  const close = sig.lastIndexOf(')')
  if (open === -1 || close <= open) return ''
  const inner = sig.slice(open + 1, close)
  if (!inner.trim()) return ''
  // Split on top-level commas (params here have no nested parens/brackets with
  // commas in practice; annotations like list[str] use [] not ()).
  const parts = []
  let depth = 0, cur = ''
  for (const ch of inner) {
    if ('([{'.includes(ch)) depth++
    else if (')]}'.includes(ch)) depth--
    if (ch === ',' && depth === 0) { parts.push(cur); cur = '' }
    else cur += ch
  }
  if (cur.trim()) parts.push(cur)
  const req = []
  for (let raw of parts) {
    const p = raw.trim()
    if (p === '*' || p.startsWith('*')) continue   // *args/**kwargs/bare-* marker
    if (p === 'self' || p === 'cls') continue
    const nm = p.split(/[:=]/)[0].trim()            // name before ':' annotation or '=' default
    if (p.includes('=')) continue                   // has a default → optional
    if (nm) req.push(nm)
  }
  return req.join(', ')
}

// Synthesize a minimal insert snippet for a helper we don't have a curated example
// for — a commented call using the required positional params as placeholders.
function synthSnippet(h) {
  const args = requiredArgs(h)
  const assign = (h.returns || '').includes('DataFrame') ? 'df = ' : ''
  const head = (h.returns || '').includes('DataFrame') ? '\ndf.head()' : ''
  return `# ${h.summary || h.name}\n${assign}${h.name}(${args})${head}`
}

// Group ready-made items ({name, category, desc, code}) into the panel's category
// sections, ordered by CATEGORY_ORDER (unknowns alphabetical, last) with an icon
// per category. Shared by the cold-start fallback (items already carry baked code)
// and the live path (code resolved first by buildCategoriesFromHelpers).
function groupHelperItems(items) {
  const byCat = {}
  for (const it of items) {
    const cat = it.category || 'Other'
    ;(byCat[cat] ||= []).push({ name: it.name, desc: it.desc, code: it.code })
  }
  const rank = (cat) => {
    const i = CATEGORY_ORDER.indexOf(cat)
    return i === -1 ? CATEGORY_ORDER.length : i
  }
  return Object.keys(byCat)
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map(cat => ({
      category: cat,
      icon: CATEGORY_ICONS[cat] || DEFAULT_CATEGORY_ICON,
      items: byCat[cat],
    }))
}

// Build the category list from the LIVE /helpers metadata (which carries NO code):
// resolve each helper's code — curated example by name if we have one, else a
// synthesized one-liner — then group. The sections mirror the backend exactly, so
// adding a helper surfaces it here automatically.
function buildCategoriesFromHelpers(helpers) {
  return groupHelperItems(helpers.map(h => ({
    name: h.name,
    category: h.category || 'Other',
    desc: CURATED_CODE[h.name]?.desc || h.summary || h.signature,
    code: CURATED_CODE[h.name]?.code || synthSnippet(h),
  })))
}

export default function SnippetsPanel({ activeTab, onInsertCode, onClose, expanded: panelExpanded, onToggleExpand }) {
  const [expanded, setExpanded] = useState({})
  const [search, setSearch] = useState('')
  // Categories to render. Once the VM is warm these are rebuilt from the LIVE
  // /helpers metadata (the effect below), with curated/synth code layered on by
  // name. Until then we seed from the GENERATED fallback — a self-contained list
  // (name/category/desc/CODE already baked in by the generator), the SAME helper
  // set built by the same introspection, so cold start shows the complete, current
  // API with real example code (not a hand-maintained subset that drifts).
  const [categories, setCategories] = useState(() =>
    groupHelperItems(
      (GENERATED_HELPERS.helpers || []).map(h => ({
        name: h.name, category: h.category, desc: h.desc || h.summary, code: h.code,
      }))
    )
  )

  // User-authored snippets (localStorage). Kept in SEPARATE state so the backend-
  // helpers effect's setCategories can never clobber them; they're concatenated
  // onto `categories` at render time (see combinedCategories below).
  const [userSnippets, setUserSnippets] = useState(loadUserSnippets)
  // Recipe MANIFEST (from /recipes/index.json), loaded once at mount — same pattern
  // as the sample gallery (/samples/index.json). Each item is {id, name, icon, file,
  // desc}; the recipe's cells are lazy-fetched from `file` only when inserted.
  const [recipes, setRecipes] = useState([])
  // Editor state: null = closed; { id?, name, desc, code } = adding (no id) or
  // editing (id set). The "+ New snippet" button and each user item's edit button
  // open this inline form.
  const [editor, setEditor] = useState(null)

  useEffect(() => {
    let cancelled = false
    fetch(RECIPES_INDEX_URL)
      .then(r => (r.ok ? r.json() : []))
      .then(list => { if (!cancelled && Array.isArray(list)) setRecipes(list) })
      .catch(() => { /* recipes just won't show if the manifest can't load */ })
    return () => { cancelled = true }
  }, [])

  // Persist user snippets on every change (single source of truth = state).
  useEffect(() => {
    try {
      localStorage.setItem(USER_SNIPPETS_KEY, JSON.stringify(userSnippets))
    } catch { /* quota / private mode — snippets just won't persist this session */ }
  }, [userSnippets])

  const saveEditor = useCallback(() => {
    const name = (editor?.name || '').trim()
    const code = (editor?.code || '').trim()
    if (!name || !code) return
    const desc = (editor?.desc || '').trim()
    setUserSnippets(prev => {
      if (editor.id) {
        return prev.map(s => s.id === editor.id ? { ...s, name, desc, code } : s)
      }
      const id = (crypto?.randomUUID?.() || `snip-${Date.now()}-${Math.random().toString(36).slice(2)}`)
      return [...prev, { id, name, desc, code }]
    })
    setEditor(null)
  }, [editor])

  const deleteUserSnippet = useCallback((id) => {
    setUserSnippets(prev => prev.filter(s => s.id !== id))
  }, [])

  // Insert a snippet OR a recipe. A recipe (multi-step) carries `# %%` cell markers;
  // we split on them and insert each block as its OWN cell via repeated onInsertCode
  // calls — this reuses the existing multi-cell "burst" path (each call dispatches
  // one insert-code event, and useNotebookCells flashes + scrolls the batch). A plain
  // snippet has no markers, so it inserts as a single cell exactly as before.
  const insertSnippet = useCallback(async (item) => {
    // A RECIPE carries a `_recipeFile` — lazy-fetch its {cells:[{type,code}]} and
    // insert EACH cell as its own notebook cell (markdown intro + code steps) via the
    // multi-cell "burst" path. A plain snippet has just `code` → one code cell.
    if (item?._recipe && item._recipeFile) {
      try {
        const resp = await fetch(`/recipes/${item._recipeFile}`)
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const recipe = await resp.json()
        const cells = Array.isArray(recipe?.cells) ? recipe.cells : []
        cells.forEach(c => {
          const code = (c?.code || '').trim()
          if (code) onInsertCode(code, c.type === 'markdown' ? 'markdown' : 'code')
        })
      } catch (e) {
        // Surface a clear failure rather than silently inserting nothing.
        alert(`Could not load recipe "${item.name}": ${e.message}`)
      }
      return
    }
    onInsertCode(item?.code || '')
  }, [onInsertCode])

  const sessionId = activeTab?.sessionId
  useEffect(() => {
    if (!sessionId) return
    let alive = true
    fetchWithTimeout(`${PROXY_URL}/proxy/helpers`, { headers: { 'X-Session-Id': sessionId } })
      .then(r => (r.ok ? r.json() : { helpers: [] }))
      .then(data => {
        if (!alive) return
        const helpers = Array.isArray(data.helpers) ? data.helpers : []
        if (!helpers.length) return  // cold VM — keep the curated fallback until warm
        // Build the WHOLE list from the backend metadata: the categories, grouping,
        // order, and counts are the backend's (single source of truth); curated code
        // is layered in by name. This replaces the fallback, so a backend category
        // rename/split (e.g. Machine Learning → Generative AI + Machine Learning)
        // shows up here with no frontend change.
        setCategories(buildCategoriesFromHelpers(helpers))
      })
      .catch(() => { /* offline / no VM — curated fallback still works */ })
    return () => { alive = false }
  }, [sessionId])

  const toggleCategory = (cat) => {
    // Sections default to OPEN, and the render reads open as `expanded[cat] !== false`.
    // So the toggle must flip the EFFECTIVE open-state, not `!prev[cat]`: on the first
    // click prev[cat] is undefined (open), and `!undefined` is true (still open) — which
    // is why it used to take two clicks. Compute current-open the same way the render
    // does, then store its negation.
    setExpanded(prev => {
      const currentlyOpen = prev[cat] !== false
      return { ...prev, [cat]: !currentlyOpen }
    })
  }

  // Combined list: the user's "My Snippets" category FIRST (so it's always on top
  // and easy to reach), then the curated + backend categories. User items carry
  // `_editable` + `_id` so the render can show edit/delete affordances for them
  // only. Built fresh from the two independent states so neither clobbers the other.
  const combinedCategories = useMemo(() => {
    const userCat = userSnippets.length
      ? [{
          category: USER_CATEGORY,
          icon: '⭐',
          items: userSnippets.map(s => ({
            name: s.name, desc: s.desc || '', code: s.code, _editable: true, _id: s.id,
          })),
        }]
      : []
    // Recipes sit right after the user's own snippets and ABOVE the per-helper
    // reference categories: they're the "how do I do X end-to-end" entry point, so
    // they're the most useful thing to see first. Items carry `_recipe` (so the
    // render labels the action "Cells") and `_recipeFile` (the JSON to lazy-fetch on
    // insert). Empty until the manifest loads, so the category simply doesn't render.
    const recipeCat = recipes.length
      ? [{
          category: RECIPES_CATEGORY,
          icon: '📋',
          items: recipes.map(r => ({
            name: r.name, desc: r.desc || '', _recipe: true, _recipeFile: r.file,
          })),
        }]
      : []
    return [...userCat, ...recipeCat, ...categories]
  }, [userSnippets, categories, recipes])

  // Search filters snippets by name, description, AND code body (so you can find a
  // helper by a symbol it uses). Matching keeps the category grouping but drops
  // empty categories; the render force-expands every group while searching so no
  // match hides under a collapsed header.
  const searchLc = search.trim().toLowerCase()
  const filteredCategories = useMemo(() => {
    if (!searchLc) return combinedCategories
    return combinedCategories
      .map(c => ({
        ...c,
        items: c.items.filter(s =>
          s.name.toLowerCase().includes(searchLc)
          || (s.desc || '').toLowerCase().includes(searchLc)
          || (s.code || '').toLowerCase().includes(searchLc)
        ),
      }))
      .filter(c => c.items.length > 0)
  }, [combinedCategories, searchLc])

  const totalFunctions = combinedCategories.reduce((n, c) => n + c.items.length, 0)
  const matchCount = filteredCategories.reduce((n, c) => n + c.items.length, 0)

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Snippets</span>
        <span className="sidebar-panel-count">{searchLc ? `${matchCount} of ${totalFunctions}` : `${totalFunctions} items`}</span>
        <PanelExpandButton expanded={panelExpanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel">
          <IconX width={12} height={12} />
        </button>
      </div>
      <div className="sidebar-panel-hint" style={{ padding: '6px 12px', fontSize: '11px', color: 'var(--text-muted)' }}>
        Pre-loaded helper functions — click to insert into current cell.
      </div>

      {/* Add / edit a user snippet. The button toggles an inline form; editing an
          existing user snippet opens the same form pre-filled. */}
      {!editor && (
        <div className="snippets-user-actions">
          <button
            className="snippets-new-btn"
            onClick={() => setEditor({ name: '', desc: '', code: '' })}
            title="Save your own reusable snippet"
          >
            <IconPlus width={12} height={12} /> New snippet
          </button>
        </div>
      )}
      {editor && (
        <div className="snippet-editor">
          <input
            className="snippet-editor-input"
            type="text"
            placeholder="name (e.g. load_orders)"
            value={editor.name}
            onChange={(e) => setEditor(ed => ({ ...ed, name: e.target.value }))}
            spellCheck={false}
            autoFocus
          />
          <input
            className="snippet-editor-input"
            type="text"
            placeholder="description (optional)"
            value={editor.desc}
            onChange={(e) => setEditor(ed => ({ ...ed, desc: e.target.value }))}
          />
          <textarea
            className="snippet-editor-code"
            placeholder="# snippet code — inserted into the current cell"
            value={editor.code}
            onChange={(e) => setEditor(ed => ({ ...ed, code: e.target.value }))}
            rows={5}
            spellCheck={false}
          />
          <div className="snippet-editor-actions">
            <button
              className="snippet-editor-save"
              onClick={saveEditor}
              disabled={!editor.name.trim() || !editor.code.trim()}
            >
              <IconSave width={12} height={12} /> {editor.id ? 'Update' : 'Save'}
            </button>
            <button className="snippet-editor-cancel" onClick={() => setEditor(null)}>Cancel</button>
          </div>
        </div>
      )}

      <div className="outline-search">
        <input
          className="outline-search-input"
          type="text"
          placeholder="Search snippets..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape' && search) { e.preventDefault(); setSearch('') } }}
          spellCheck={false}
        />
        {search && (
          <button
            className="outline-search-clear"
            onClick={() => setSearch('')}
            title="Clear search (Esc)"
            aria-label="Clear search"
          >
            <IconX width={12} height={12} />
          </button>
        )}
      </div>
      <div className="sidebar-panel-body">
        {searchLc && matchCount === 0 && (
          <div className="sidebar-panel-hint" style={{ padding: '12px', fontSize: '12px', color: 'var(--text-muted)' }}>
            No snippets match “{search.trim()}”.
          </div>
        )}
        {filteredCategories.map(category => {
          // While searching, force every (non-empty) category open so matches are
          // never hidden under a collapsed header; otherwise respect the user's
          // per-category expand/collapse state (default open).
          const isOpen = searchLc ? true : expanded[category.category] !== false
          return (
          <div key={category.category}>
            <div
              className="sidebar-subheader sidebar-subheader-toggle"
              onClick={() => toggleCategory(category.category)}
            >
              <span>{category.icon}</span>
              <span>{category.category}</span>
              <span className="sidebar-subheader-count">{category.items.length}</span>
              <span className="sidebar-subheader-chevron">{isOpen ? '▾' : '▸'}</span>
            </div>
            {isOpen && (
              <div className="snippets-list">
                {category.items.map(snippet => (
                  <div
                    key={snippet._id || snippet.name}
                    className={`snippet-item${snippet._recipe ? ' snippet-item-recipe' : ''}`}
                    onClick={() => insertSnippet(snippet)}
                    title={snippet._recipe
                      ? `Insert "${snippet.name}" as separate cells`
                      : `Insert ${snippet.name}()`}
                  >
                    <div className="snippet-item-text">
                      <div className="snippet-name">
                        <IconCode width={11} height={11} />
                        {/* Recipes are workflows, not a single function — show the
                            plain name (no "()"); plain snippets keep the call form. */}
                        <code>{searchLc
                          ? highlightMatch(snippet._recipe ? snippet.name : `${snippet.name}()`, search)
                          : (snippet._recipe ? snippet.name : `${snippet.name}()`)}</code>
                      </div>
                      <div className="snippet-desc">{searchLc ? highlightMatch(snippet.desc, search) : snippet.desc}</div>
                    </div>
                    {/* Hover-revealed actions. User snippets get edit + delete
                        alongside insert; curated/backend snippets get insert only.
                        Row click still inserts for all. */}
                    <div className="ds-insert-actions">
                      {snippet._editable && (
                        <>
                          <button
                            className="ds-insert-btn snippet-edit-btn"
                            onClick={(e) => { e.stopPropagation(); setEditor({ id: snippet._id, name: snippet.name, desc: snippet.desc, code: snippet.code }) }}
                            title="Edit this snippet"
                          >
                            <IconPencil width={11} height={11} />
                          </button>
                          <button
                            className="ds-insert-btn snippet-delete-btn"
                            onClick={(e) => { e.stopPropagation(); deleteUserSnippet(snippet._id) }}
                            title="Delete this snippet"
                          >
                            <IconTrash width={11} height={11} />
                          </button>
                        </>
                      )}
                      <button
                        className="ds-insert-btn ds-insert-python"
                        onClick={(e) => { e.stopPropagation(); insertSnippet(snippet) }}
                        title={snippet._recipe ? 'Insert this recipe as separate cells' : 'Insert Python code for this helper'}
                      >
                        <IconCode width={11} height={11} /> {snippet._recipe ? 'Cells' : 'Python'}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
          )
        })}
      </div>
    </div>
  )
}
