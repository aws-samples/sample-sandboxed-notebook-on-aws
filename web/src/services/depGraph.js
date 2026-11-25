/**
 * depGraph — traversal over the STORED notebook DAG document (design §8.1).
 *
 * The proxy's builder (proxy/notebook/dag.py) is the only place that infers
 * dependencies. It stores one document per notebook — nodes (one per code/SQL
 * cell with reads/writes/mutates/sources/status/stale_reason), edges (nearest
 * earlier producer per read variable), issues and the VM-owned `runtime`
 * section (variable provenance + clock). The browser receives that document
 * inline as a full document (GET/POST/PUT notebook, GET /notebooks/{id}/dag,
 * the first capture) or as a `dag_delta` (execute responses and the batch `dag`
 * frame; applied by ./dagDelta.js) and only walks it here: no inference, no
 * regexes.
 *
 * Pure functions — no React, no network, no import.meta — unit-testable under
 * plain Node (web/src/services/__tests__/depGraph.test.mjs).
 */

import { compareRev, revOf } from './dagDelta.js'

const EMPTY_GRAPH = Object.freeze({
  edges: [],
  upstream: new Map(),
  downstream: new Map(),
  inputsOf: new Map(),
  nodeById: new Map(),
})

const graphCache = new WeakMap()

/** True for a document the web may use (readers treat other schemas as absent). */
export function isDagDoc(doc) {
  return !!doc && typeof doc === 'object' && doc.schema === 1 && Array.isArray(doc.nodes)
}

/**
 * Adjacency views of a stored DAG document, cached per document object.
 *   edges      — doc.edges as stored ({from, to, var, kind, use, origin, backward})
 *   upstream   — Map<cellId, Set<cellId>>  cells this cell directly depends on
 *   downstream — Map<cellId, Set<cellId>>  cells that directly depend on this cell
 *   inputsOf   — Map<cellId, Map<varName, cellId>> which cell each read resolved to
 *   nodeById   — Map<cellId, node>
 */
export function graphFromDag(doc) {
  if (!isDagDoc(doc)) return EMPTY_GRAPH
  const hit = graphCache.get(doc)
  if (hit) return hit
  const edges = Array.isArray(doc.edges) ? doc.edges : []
  const upstream = new Map()
  const downstream = new Map()
  const inputsOf = new Map()
  const nodeById = new Map()
  const nodes = [...doc.nodes].sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
  for (const n of nodes) nodeById.set(String(n.cell_id), n)
  const ensure = (map, key) => {
    if (!map.has(key)) map.set(key, new Set())
    return map.get(key)
  }
  for (const e of edges) {
    const from = String(e.from)
    const to = String(e.to)
    ensure(upstream, to).add(from)
    ensure(downstream, from).add(to)
    if (!inputsOf.has(to)) inputsOf.set(to, new Map())
    inputsOf.get(to).set(e.var, from)
  }
  const graph = { edges, upstream, downstream, inputsOf, nodeById }
  graphCache.set(doc, graph)
  return graph
}

/**
 * Cells that participate in a dependency CYCLE (A → … → A). Reactive auto-run must
 * never enqueue these — a cycle would re-trigger forever. DFS with a recursion
 * stack over the `downstream` adjacency. Returns a Set<cellId>.
 */
export function findCycleCells(downstream) {
  const onCycle = new Set()
  const WHITE = 0, GRAY = 1, BLACK = 2
  const color = new Map()
  const nodes = new Set()
  for (const [from, tos] of downstream.entries()) {
    nodes.add(String(from))
    for (const t of tos) nodes.add(String(t))
  }
  const stack = []
  const dfs = (u) => {
    color.set(u, GRAY)
    stack.push(u)
    for (const v of (downstream.get(u) || [])) {
      const vv = String(v)
      if (color.get(vv) === GRAY) {
        const idx = stack.lastIndexOf(vv)
        for (let i = idx; i < stack.length; i++) onCycle.add(stack[i])
      } else if ((color.get(vv) || WHITE) === WHITE) {
        dfs(vv)
      }
    }
    stack.pop()
    color.set(u, BLACK)
  }
  for (const n of nodes) {
    if ((color.get(n) || WHITE) === WHITE) dfs(n)
  }
  return onCycle
}

function walk(seedCellIds, adjacency) {
  const out = new Set()
  const queue = [...seedCellIds].map(String)
  const seeds = new Set(queue)
  while (queue.length) {
    const cur = queue.shift()
    for (const next of (adjacency.get(cur) || [])) {
      const n = String(next)
      if (out.has(n) || seeds.has(n)) continue
      out.add(n)
      queue.push(n)
    }
  }
  return out
}

/** Breadth-first depth of every cell reachable from `seed` through `adjacency`. */
function depthsFrom(seed, adjacency) {
  const depth = new Map()
  const seen = new Set([seed])
  let frontier = [seed]
  let level = 0
  while (frontier.length) {
    level += 1
    const next = []
    for (const cur of frontier) {
      for (const n of (adjacency.get(cur) || [])) {
        const id = String(n)
        if (seen.has(id)) continue
        seen.add(id)
        depth.set(id, level)
        next.push(id)
      }
    }
    frontier = next
  }
  return depth
}

/**
 * Related cells of `cellId` in the stored graph (the single closure used by the
 * drawer's steps / Used by and the Outline highlight).
 *   upstream   — Map<cellId, depth>  cells this cell depends on, transitively
 *   downstream — Map<cellId, depth>  cells that depend on this cell, transitively
 * depth 1 = direct edge, more than 1 = indirect. The seed is never included and
 * cycles terminate (each cell is visited once, at its shortest depth).
 */
export function relatedCells(graph, cellId) {
  const seed = String(cellId)
  return {
    upstream: depthsFrom(seed, graph.upstream),
    downstream: depthsFrom(seed, graph.downstream),
  }
}

/** Transitive dependents of the seed cells (seeds excluded); cycle-safe BFS. */
export function collectDownstream(seedCellIds, downstream) {
  return walk(seedCellIds, downstream)
}

/** Transitive dependencies of the seed cells (seeds excluded); cycle-safe BFS. */
export function collectUpstream(seedCellIds, upstream) {
  return walk(seedCellIds, upstream)
}

/** Order cell ids by their position in `cells` (top → bottom); unknown ids dropped. */
export function orderByPosition(cellIds, cells) {
  const pos = new Map((cells || []).map((c, i) => [String(c.id), i]))
  return [...cellIds]
    .map(String)
    .filter(id => pos.has(id))
    .sort((a, b) => pos.get(a) - pos.get(b))
}

/**
 * Nodes the stored document marks dataflow-stale: Map<cellId, stale_reason>.
 * Includes `edited` nodes that carry a stale_reason (the builder evaluates
 * staleness for current AND edited nodes, design §3.5).
 */
export function staleNodes(doc) {
  const out = new Map()
  if (!isDagDoc(doc)) return out
  for (const n of doc.nodes) {
    if (n && n.stale_reason) out.set(String(n.cell_id), n.stale_reason)
  }
  return out
}

/** Names in `runtimeVars` (doc.runtime.vars) whose last_exec clock is > `sinceClock`. */
export function changedVarsSince(runtimeVars, sinceClock) {
  const changed = new Set()
  const since = typeof sinceClock === 'number' ? sinceClock : -1
  for (const [name, info] of Object.entries(runtimeVars || {})) {
    const le = info && typeof info.last_exec === 'number' ? info.last_exec : -1
    if (le > since) changed.add(name)
  }
  return changed
}

/**
 * Run baseline captured at drain start: the document's runtime clock and the VM
 * it belongs to ({clock, vmId}; clock 0 / vmId null when there is no runtime).
 */
export function runBaseline(doc) {
  const rt = doc && doc.runtime
  return {
    clock: rt && typeof rt.clock === 'number' ? rt.clock : 0,
    vmId: rt && rt.vm_id != null ? String(rt.vm_id) : null,
  }
}

/**
 * Variables `runtime` changed since `baseline` (from runBaseline). Clocks are
 * per VM: when the runtime belongs to a DIFFERENT VM than the baseline (a cold
 * launch restarted the clock), compare against 0 instead of the old VM's clock.
 */
export function changedVarsSinceBaseline(runtime, baseline) {
  const vars = runtime && runtime.vars
  const rtVm = runtime && runtime.vm_id != null ? String(runtime.vm_id) : null
  const b = baseline || { clock: 0, vmId: null }
  const switched = rtVm != null && b.vmId != null && rtVm !== b.vmId
  return changedVarsSince(vars, switched ? 0 : b.clock)
}

/** The maximum last_exec clock across `runtimeVars` (0 if none). */
export function maxClock(runtimeVars) {
  let m = 0
  for (const info of Object.values(runtimeVars || {})) {
    const le = info && typeof info.last_exec === 'number' ? info.last_exec : 0
    if (le > m) m = le
  }
  return m
}

/**
 * Should `next` replace `cur`? Accept a usable document when there is none yet,
 * or when it is newer: a higher (version, runtime_version) pair (a runtime-only
 * write keeps version and built_at and raises runtime_version), or — only when
 * the pairs are equal — a later built_at.
 */
export function newerDag(cur, next) {
  if (!isDagDoc(next)) return false
  if (!isDagDoc(cur)) return true
  const c = compareRev(revOf(next), revOf(cur))
  if (c !== 0) return c > 0
  return typeof next.built_at === 'string' && typeof cur.built_at === 'string' && next.built_at > cur.built_at
}

const PROVENANCE_KEYS = ['defined_by', 'defined_at', 'modified_by', 'last_cell', 'last_exec']

/**
 * Overlay variable provenance from the DAG runtime section onto the /variables
 * value map. With a document the VM's own provenance fields are ignored (a name
 * the document does not know gets null / []); without one the map is returned
 * unchanged.
 */
export function withDagProvenance(variables, doc) {
  if (!variables || !isDagDoc(doc)) return variables || {}
  const rtVars = doc.runtime && doc.runtime.vars && typeof doc.runtime.vars === 'object' ? doc.runtime.vars : {}
  const out = {}
  for (const [name, info] of Object.entries(variables)) {
    const prov = rtVars[name] || null
    const merged = { ...(info || {}) }
    for (const k of PROVENANCE_KEYS) {
      if (prov && prov[k] !== undefined) merged[k] = prov[k]
      else merged[k] = k === 'modified_by' ? [] : null
    }
    out[name] = merged
  }
  return out
}

/**
 * 'error' | 'edited' | 'stale' | 'current' for one cell (design §8.1 steps 1-5).
 * While a live cell exists, `code !== lastExecutedCode` is the ONLY edited signal
 * (the node is `edited` until the autosave PUT lands, finding 5); the node's own
 * `edited` is honoured only without a live cell.
 */
export function effectiveStatus(cell, node) {
  if ((cell && (cell.error || cell.status === 'error')) || (node && node.status === 'error')) return 'error'
  if (cell) {
    if (cell.lastExecutedCode != null && cell.code !== cell.lastExecutedCode) return 'edited'
  } else if (node && node.status === 'edited') {
    return 'edited'
  }
  if (node && (node.stale_reason || node.status === 'never_run')) return 'stale'
  return 'current'
}
