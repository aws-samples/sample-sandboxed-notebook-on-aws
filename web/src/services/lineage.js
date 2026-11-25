/**
 * lineage — pure "How was this computed?" model for a cell's output.
 *
 * Walks the STORED notebook DAG (depGraph.js graphFromDag) upstream from a cell
 * to the ordered list of steps that produced it, unions the data sources those
 * steps read (node.sources, captured by the VM — identifiers only), resolves each
 * source against the data catalog, collects the @param inputs involved and
 * grades the result with a trust level:
 *   red   — an upstream step errored
 *   amber — something weakens confidence (ad-hoc/URL source, stale/edited/never-run
 *           step, amber DAG issue: missing producer / out-of-order read / dynamic
 *           names / dependency cycle, sources not captured)
 *   green — every recorded source is catalogued and every step is current (reads
 *           through the reader helpers / SQL engine and raw requests/httpx/urllib
 *           HTTP — recorded by host — are captured; raw boto3 is not)
 *
 * No React, no network, no import.meta — loads under plain Node for tests.
 */
import { effectiveStatus, graphFromDag, orderByPosition, relatedCells } from './depGraph.js'
import { cellBadgeMap } from '../utils/cellBadge.js'
import { collectInputs, deriveDisplayVar } from './appViewModel.js'
import { firstRichKind, firstTable, isMultiOutput } from './cellOutputs.js'
import { hasImageOutput } from './cellOutput.js'

const TITLE_MAX = 80
const NOT_CAPTURED = 'sources not captured — re-run'
// Worded as "recorded": some reads bypass capture (raw boto3,
// pd.read_csv('s3://…')), so green can't claim full coverage.
const GREEN_REASON = 'No uncatalogued sources recorded; every step is current'

const DIRECTIVE_RE = /^(#|--)\s*@(param|app-show|app-hide)\b/i

// Parsed @param inputs per cell object. Cells are replaced (not mutated) on
// edit, so an unchanged cell reuses its parse across keystrokes elsewhere.
const inputsCache = new WeakMap()
function cellInputs(cell) {
  let hit = inputsCache.get(cell)
  if (!hit) {
    hit = collectInputs([cell]).map(inp => (
      { cellId: String(cell.id), varName: inp.varName, label: inp.label, value: inp.displayValue }
    ))
    inputsCache.set(cell, hit)
  }
  return hit
}
function hasOutput(cell) {
  return !!(cell.output || cell.html || hasImageOutput(cell))
}

function neverRun(cell) {
  return cell.lastExecutedCode == null && cell.executionNumber == null && !hasOutput(cell)
}

function truncate(text) {
  return text.length > TITLE_MAX ? text.slice(0, TITLE_MAX - 1).trimEnd() + '…' : text
}

/**
 * Short human title for a step: its leading comment (`#` for code, `--` for SQL)
 * if any, else its first meaningful line. Markdown/empty cells fall back to the badge.
 */
export function stepTitle(cell, badge = '') {
  if (!cell || cell.type === 'markdown') return badge
  const prefix = cell.type === 'sql' ? '--' : '#'
  for (const raw of (cell.code || '').split('\n')) {
    const line = raw.trim()
    if (!line || DIRECTIVE_RE.test(line)) continue
    if (line.startsWith(prefix)) {
      const text = line.slice(prefix.length).trim()
      if (text) return truncate(text)
      continue
    }
    return truncate(line)
  }
  return badge
}

/**
 * 'error' | 'edited' | 'stale' | 'current' for one step: effectiveStatus over the
 * live cell and its stored DAG node (design §8.1). Without a node (document not
 * arrived yet) a cell that never ran is stale, as before.
 */
export function stepStatus(cell, node) {
  const st = effectiveStatus(cell, node)
  if (st === 'current' && !node && cell && neverRun(cell)) return 'stale'
  return st
}

/**
 * Resolve a source {kind, id} against catalog entries [{source_type, source_id,
 * display_name?}]. Exact (case-insensitive) id match, or a folder entry (id ends
 * in '/') that prefixes the source id. URL sources are never catalogued.
 * Returns {source_id, display_name} or null.
 */
export function matchCatalog(kind, id, entries) {
  if (kind === 'url' || !id) return null
  const want = String(id).toLowerCase()
  for (const e of entries || []) {
    if (!e || e.source_type !== kind || !e.source_id) continue
    const sid = String(e.source_id).toLowerCase()
    if (sid === want || (sid.endsWith('/') && want.startsWith(sid))) {
      return { source_id: e.source_id, display_name: e.display_name || e.source_id }
    }
  }
  return null
}

/** Display labels for a source's reading steps: their badges ("PY-8"), or "cell <id>". */
export function sourceStepLabels(steps) {
  return (steps || []).map(st => st.badge || `cell ${st.cellId}`)
}

const AMBER_CODES = new Set(['missing_producer', 'cycle', 'dynamic_names', 'out_of_order'])

function labelOf(id, badges) {
  const k = String(id)
  return (badges && badges.get(k)) || `cell ${k}`
}

/**
 * Full, untruncated text for one stored DAG issue, rendered from its structured
 * fields ({code, var, related, source}) with cell badges for related cells.
 * `node` (optional) supplies the dynamic-name constructs; unknown codes fall back
 * to the builder's own `detail`.
 * `ctx.deletedInMemory` (optional Set of names): a missing producer that was a
 * deleted cell while the variable is still in memory gets an explanation.
 */
export function issueMessage(issue, badges, node, ctx = {}) {
  if (!issue) return ''
  const v = issue.var
  const related = (Array.isArray(issue.related) ? issue.related : []).map(id => labelOf(id, badges))
  switch (issue.code) {
    case 'cross_cell_mutation':
      return `mutates ${v} defined in ${related.join(', ') || 'another cell'}`
    case 'multiple_definitions':
      return `redefines ${v}, also defined in ${related.join(', ')}`
    case 'external_state': {
      const src = issue.source || {}
      return `reads ${src.kind} ${src.id}; the cell won't re-run when the source changes`
    }
    case 'dynamic_names': {
      const dyn = node && Array.isArray(node.dynamic) && node.dynamic.length ? node.dynamic.join(', ') : null
      return dyn ? `uses ${dyn}; its reads and writes may be incomplete` : (issue.detail || 'uses dynamic names')
    }
    case 'missing_producer':
      if (ctx && ctx.deletedInMemory && typeof ctx.deletedInMemory.has === 'function' && ctx.deletedInMemory.has(v)) {
        return `"${v}" was defined by a deleted cell. It's in memory now but won't survive a restart, a Run All on a fresh VM, or a restore.`
      }
      return `"${v}" has no producing cell (deleted, not run, or left by an AI exploration)`
    case 'out_of_order':
      return `reads ${v} from ${related.join(', ')}, which comes after it in the notebook`
    case 'cycle':
      return `is part of a dependency cycle with ${related.join(', ')}`
    default:
      return issue.detail || String(issue.code || '')
  }
}

const EXT_KIND_LABEL = { s3: 'S3', url: 'URL', local: 'local file' }

/** Short display name for an external source: the last path segment of an S3 key or a
 *  local path; a URL stays whole. The full id is always kept for the tooltip. */
function externalSourceName(kind, id) {
  const s = String(id ?? '')
  if (kind === 's3') {
    const key = s.replace(/^s3:\/\/[^/]+\/?/, '')
    return key.split('/').filter(Boolean).pop() || s
  }
  if (kind === 'local') return s.split('/').filter(Boolean).pop() || s
  return s
}

/**
 * The drawer's Dependency-notes list shows ONE callout per (cell, source kind) instead of
 * one per file: a cell that reads two S3 files has two stored `external_state` issues whose
 * messages differ only at the end of a long shared bucket path, which read as a duplicate.
 * Grouped entries carry `shortDetail` ("reads 2 S3 sources: a.csv, b.csv"), a full
 * `message` (every full id, for the tooltip / screen readers) and `sources`. Everything
 * else, and a group of one, passes through unchanged. The stored issues and
 * `lineage.issues` are not modified.
 */
export function groupDependencyIssues(issues) {
  const list = Array.isArray(issues) ? issues : []
  const groups = new Map()
  const out = []
  for (const iss of list) {
    const src = iss && iss.code === 'external_state' ? iss.source : null
    if (!src || !src.kind) { out.push(iss); continue }
    const key = `${iss.cellId}\u0000${src.kind}`
    let g = groups.get(key)
    if (!g) { g = { first: iss, items: [], slot: out.length }; groups.set(key, g); out.push(null) }
    g.items.push(iss)
  }
  for (const g of groups.values()) {
    if (g.items.length === 1) { out[g.slot] = g.items[0]; continue }
    const kind = g.first.source.kind
    const label = EXT_KIND_LABEL[kind] || kind
    const ids = g.items.map(i => String(i.source.id ?? ''))
    const names = ids.map(id => externalSourceName(kind, id))
    // Same file name from different folders: fall back to the full id so they stay distinct.
    const shown = names.map((n, i) => (names.indexOf(n) === i && names.lastIndexOf(n) === i) ? n : ids[i])
    out[g.slot] = {
      ...g.first,
      source: g.first.source,
      sources: g.items.map(i => i.source),
      count: g.items.length,
      shortDetail: `reads ${g.items.length} ${label} sources: ${shown.join(', ')}`,
      message: `reads ${g.items.length} ${label} sources: ${ids.join(', ')}; the cell won't re-run when the sources change`,
    }
  }
  return out
}

export const NOT_IN_MEMORY_TEXT = "Not in the current session's memory — re-run to update"

/**
 * Tooltip text for a node's stored stale_reason ({kind, vars, via}); via cells are
 * shown by badge. Null when the node is not stale.
 */
export function staleReasonText(reason, badges) {
  if (!reason) return null
  if (reason.kind === 'not_in_checkpoint') return NOT_IN_MEMORY_TEXT
  const via = (Array.isArray(reason.via) ? reason.via : []).map(id => labelOf(id, badges))
  const viaText = via.length ? ` (via ${via.join(', ')})` : ''
  const vars = Array.isArray(reason.vars) ? reason.vars : []
  return vars.length
    ? `Stale: upstream ${vars.join(', ')} changed since this cell last ran${viaText} — re-run to update`
    : `Stale: an upstream dependency changed since this cell last ran${viaText} — re-run to update`
}

/** 'amber' for issues that weaken trust, else 'info' (stored severity wins). */
export function issueSeverity(issue) {
  if (!issue) return 'info'
  if (issue.severity === 'amber' || issue.severity === 'info') return issue.severity
  return AMBER_CODES.has(issue.code) ? 'amber' : 'info'
}

/**
 * Per-cell issue summary for the Cell issue badge:
 *   { count, amber, messages: [string], ariaLabel, title }  or null when none.
 * Messages are full text (never truncated), amber first.
 */
export function cellIssueSummary(dag, cellId, badges, ctx = {}) {
  const graph = graphFromDag(dag)
  const id = String(cellId)
  const issues = (dag && Array.isArray(dag.issues) ? dag.issues : []).filter(i => String(i.cell_id) === id)
  if (!issues.length) return null
  const node = graph.nodeById.get(id)
  const rows = issues
    .map(i => ({ severity: issueSeverity(i), message: issueMessage(i, badges, node, ctx) }))
    .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'amber' ? -1 : 1))
  const amber = rows.filter(r => r.severity === 'amber').length
  const count = rows.length
  const noun = `${count} dependency note${count === 1 ? '' : 's'}`
  const ariaLabel = amber ? `${noun}, ${amber} warning${amber === 1 ? '' : 's'}` : noun
  const title = rows.map(r => `${r.severity === 'amber' ? '⚠ ' : ''}${r.message}`).join('\n')
  return { count, amber, messages: rows.map(r => r.message), ariaLabel, title }
}

/**
 * Build the lineage of `cellId`.
 *   cells          — live notebook cells (code, lastExecutedCode, error, output fields)
 *   dag            — the stored DAG document (graphFromDag); null = not arrived yet
 *   catalogEntries — data catalog entries [{source_type, source_id, display_name}]
 *   opts           — { graph?, badges? } (precomputed, optional); trustOnly skips
 *                    step titles and @param inputs (trust level only)
 * Returns { cellId, steps, usedBy, sources, inputs, trust: {level, reasons}, cycle, notes, issues }.
 * `usedBy` = transitive downstream cells (same step shape, notebook order, no markdown).
 * `issues` = every stored issue of the steps, amber first:
 *   [{ cellId, badge, code, severity, var, message }].
 */
export function buildLineage(cellId, cells, dag, catalogEntries = [], opts = {}) {
  const list = cells || []
  const root = String(cellId)
  const graph = opts.graph || graphFromDag(dag)
  const badges = opts.badges || cellBadgeMap(list)
  const byId = new Map(list.map(c => [String(c.id), c]))

  const related = relatedCells(graph, root)
  const visited = new Set([root, ...related.upstream.keys()])
  const stepIds = orderByPosition(visited, list).filter(id => byId.get(id).type !== 'markdown')
  const stepCells = stepIds.map(id => byId.get(id))
  const nodeOf = (id) => graph.nodeById.get(id) || null

  const toStep = (cell) => {
    const id = String(cell.id)
    const node = nodeOf(id)
    const resolved = graph.inputsOf.get(id) || new Map()
    return {
      cellId: id,
      badge: badges.get(id) || '',
      title: opts.trustOnly ? '' : stepTitle(cell, badges.get(id) || ''),
      type: cell.type || 'code',
      // A read that resolved to another cell (incl. a mutator reading the
      // builder's frame) is an input.
      varsIn: [...resolved.keys()].filter(v => resolved.get(v) !== id).sort(),
      executedAt: cell.executedAt || (node && node.executed_at) || null,
      status: stepStatus(cell, node),
      engine: cell.engine || null,
      sourcesCaptured: !!node && Array.isArray(node.sources),
    }
  }
  const steps = stepCells.map(toStep)
  // Transitive dependents, notebook order, markdown excluded. Same step shape and
  // status logic as `steps`; not part of trust, sources or the Explain payload.
  const usedBy = orderByPosition(related.downstream.keys(), list)
    .filter(id => byId.get(id).type !== 'markdown')
    .map(id => toStep(byId.get(id)))

  // Sources: deduped union of every step's captured reads (node.sources), each
  // attributed to the step(s) that read it. `readByThisCell` = the selected
  // output's own cell read it (the drawer groups "Read by this cell" / "Upstream").
  const sources = []
  const byKey = new Map()
  for (const id of stepIds) {
    const node = nodeOf(id)
    if (!node || !Array.isArray(node.sources)) continue
    for (const s of node.sources) {
      if (!s || !s.kind || !s.id) continue
      const key = `${s.kind}\u0000${s.id}`
      let src = byKey.get(key)
      if (!src) {
        const catalogRef = matchCatalog(s.kind, s.id, catalogEntries)
        src = { kind: s.kind, id: s.id, inCatalog: !!catalogRef, catalogRef, steps: [], readByThisCell: false }
        byKey.set(key, src)
        sources.push(src)
      }
      if (!src.steps.some(st => st.cellId === id)) src.steps.push({ cellId: id, badge: badges.get(id) || '' })
      if (id === root) src.readByThisCell = true
    }
  }

  // Trust-only callers (lineageTrustLevels) skip the @param parse entirely.
  const inputs = []
  if (!opts.trustOnly) {
    for (const cell of stepCells) inputs.push(...cellInputs(cell))
  }

  // Stored issues of the steps (position order, then amber first).
  const stepSet = new Set(stepIds)
  const pos = new Map(stepIds.map((id, i) => [id, i]))
  const issues = (dag && Array.isArray(dag.issues) ? dag.issues : [])
    .filter(i => stepSet.has(String(i.cell_id)))
    .map(i => {
      const id = String(i.cell_id)
      return {
        cellId: id, badge: badges.get(id) || '', code: i.code, severity: issueSeverity(i),
        var: i.var ?? null, message: issueMessage(i, badges, nodeOf(id)),
        source: i.source ?? null,
      }
    })
    .sort((a, b) => {
      if (a.severity !== b.severity) return a.severity === 'amber' ? -1 : 1
      return pos.get(a.cellId) - pos.get(b.cellId)
    })
  const cycle = issues.some(i => i.code === 'cycle')

  const notes = []
  const red = []
  const amber = []
  steps.forEach((step, i) => {
    const cell = stepCells[i]
    const node = nodeOf(step.cellId)
    if (step.status === 'error') { red.push(`${step.badge} errored`); return }
    const notRun = node ? node.status === 'never_run' : neverRun(cell)
    if (step.status === 'edited') amber.push(`${step.badge} edited since last run`)
    else if (step.status === 'stale') {
      if (notRun) amber.push(`${step.badge} has not been run`)
      else if (node && node.stale_reason && node.stale_reason.kind === 'not_in_checkpoint') {
        amber.push(`${step.badge}: ${NOT_IN_MEMORY_TEXT}`)
      } else amber.push(`${step.badge} is stale (upstream changed)`)
    }
    if (node && (step.type === 'code' || step.type === 'sql') && !notRun && !neverRun(cell) && !step.sourcesCaptured) {
      const msg = `${step.badge}: ${NOT_CAPTURED}`
      amber.push(msg)
      notes.push(msg)
    }
  })
  for (const s of sources) {
    const via = ` — via ${sourceStepLabels(s.steps).join(', ')}`
    if (s.kind === 'url') amber.push(`${s.id} is fetched from a URL (not in the data catalog)${via}`)
    else if (!s.inCatalog) amber.push(`${s.id} is not in the data catalog${via}`)
  }
  const seenMissing = new Set()
  for (const iss of issues) {
    if (iss.severity !== 'amber') { notes.push(`${iss.badge}: ${iss.message}`); continue }
    if (iss.code === 'missing_producer') {
      if (seenMissing.has(iss.var)) continue
      seenMissing.add(iss.var)
      amber.push(iss.message)
    } else if (iss.code === 'cycle') {
      continue
    } else {
      amber.push(`${iss.badge} ${iss.message}`)
    }
  }
  if (cycle) amber.push('dependency cycle')

  let trust
  if (red.length) trust = { level: 'red', reasons: red }
  else if (amber.length) trust = { level: 'amber', reasons: amber }
  else trust = { level: 'green', reasons: [GREEN_REASON] }

  return { cellId: root, steps, usedBy, sources, inputs, trust, cycle, notes, issues }
}

/**
 * Trust level per output-bearing (non-markdown) cell, for the per-cell dot.
 * Builds the graph once. Returns Map<String(cellId), 'green'|'amber'|'red'>.
 */
export function lineageTrustLevels(cells, dag, catalogEntries = [], opts = {}) {
  const list = cells || []
  const shared = {
    ...opts,
    graph: opts.graph || graphFromDag(dag),
    trustOnly: true,
    badges: opts.badges || cellBadgeMap(list),
  }
  const out = new Map()
  for (const cell of list) {
    if (cell.type === 'markdown') continue
    if (!hasOutput(cell) && !cell.error) continue
    out.set(String(cell.id), buildLineage(cell.id, list, dag, catalogEntries, shared).trust.level)
  }
  return out
}

/**
 * Payload for the proxy's lineage-summary call: the output's shape (never its
 * values), each step's code and the source identifiers.
 */
export function buildExplainPayload(lineage, cells) {
  const cell = (cells || []).find(c => String(c.id) === String(lineage.cellId)) || {}
  const html = typeof cell.html === 'string' ? cell.html : ''
  // The drawer describes the FIRST table of a multi-output cell (the adapter never
  // looks for tables or `data-plotly` inside the raw envelope string).
  const table = firstTable(cell)
  let kind = 'text'
  if (isMultiOutput(cell)) {
    const first = firstRichKind(cell)
    kind = first === 'table' ? 'table' : first ? 'chart' : 'text'
  } else if (html.includes('data-plotly') || hasImageOutput(cell)) kind = 'chart'
  else if (table) kind = 'table'
  const byId = new Map((cells || []).map(c => [String(c.id), c]))
  return {
    output: {
      var: deriveDisplayVar(cell) || cell.outputVariable || null,
      kind,
      columns: table && Array.isArray(table.columns) ? table.columns.map(String) : [],
      rows: (table && table.total_rows) ?? cell.resultRows ?? null,
      cols: (table && table.total_cols) ?? cell.resultCols ?? null,
    },
    steps: lineage.steps.map(s => ({ badge: s.badge, type: s.type, code: (byId.get(s.cellId) || {}).code || '' })),
    sources: lineage.sources.map(({ kind: k, id }) => ({ kind: k, id })),
  }
}

/** "just now" / "5 min ago" / "3 hours ago" / "2 days ago"; 'never' for missing/invalid. */
export function relativeTime(iso, nowMs = Date.now()) {
  if (!iso) return 'never'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return 'never'
  const s = (nowMs - t) / 1000
  if (s < 45) return 'just now'
  if (s < 90) return '1 min ago'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  const h = Math.floor(s / 3600)
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`
  const d = Math.floor(s / 86400)
  return `${d} day${d === 1 ? '' : 's'} ago`
}

// Reason words for steps that are not current, in display order. A stale step that has
// no run time has never been run.
const NOT_CURRENT_ORDER = ['errored', 'edited', 'not run', 'stale']
function notCurrentWord(step) {
  if (step.status === 'error') return 'errored'
  if (step.status === 'edited') return 'edited'
  if (step.status === 'stale') return step.executedAt ? 'stale' : 'not run'
  return null
}

/**
 * Lineage drawer status text over `lineage.steps` (this cell + every non-markdown cell it
 * depends on). `ranAgo` is an already-formatted relative time ("1 min ago") or null.
 * Returns:
 *   summary — "All 5 cells behind this output are up to date" / "3 of 5 cells …" /
 *             "This cell has no upstream dependencies"
 *   reason  — the not-current cells, grouped by state ("PY-3 edited, PY-4, PY-5 stale"), or ''
 *   text    — header line: summary · reason · ran <ranAgo>
 *   footer  — summary · reason (the footer shows "Last run" separately)
 *   tooltip — which cells are counted
 *   counted — the counted cells' labels, in lineage order
 */
export function lineageStatusText(steps, ranAgo = null) {
  const list = Array.isArray(steps) ? steps : []
  const n = list.length
  const current = list.filter(s => s.status === 'current').length
  const counted = sourceStepLabels(list)

  let summary
  if (n === 0) summary = 'No cells behind this output'
  else if (n === 1) summary = 'This cell has no upstream dependencies'
  else if (current === n) summary = n === 2 ? 'Both cells behind this output are up to date' : `All ${n} cells behind this output are up to date`
  else if (current === 0) summary = `None of the ${n} cells behind this output are up to date`
  else summary = `${current} of ${n} cells behind this output are up to date`

  const groups = new Map()
  list.forEach((s, i) => {
    const word = notCurrentWord(s)
    if (!word) return
    if (!groups.has(word)) groups.set(word, [])
    groups.get(word).push(counted[i])
  })
  const reason = NOT_CURRENT_ORDER.filter(w => groups.has(w)).map(w => `${groups.get(w).join(', ')} ${w}`).join(', ')

  const footer = reason ? `${summary} · ${reason}` : summary
  const text = ranAgo ? `${footer} · ran ${ranAgo}` : footer

  const tail = 'Markdown and unrelated cells are not counted.'
  let tooltip
  if (n === 0) tooltip = `No code or SQL cells are counted. ${tail}`
  else if (n === 1) tooltip = `Counts only this cell (${counted[0]}); it does not depend on any other cell. ${tail}`
  else tooltip = `Counts this cell and every cell it depends on (${counted.join(', ')}). ${tail}`

  return { summary, reason, text, footer, tooltip, counted }
}
