/**
 * dagDelta — the browser side of the DAG delta transport (delta-plan §0.4, §0.8).
 *
 * The proxy sends `dag_delta` on /proxy/execute* responses and the batch `dag`
 * frame (the full `dag` on the first document or when the delta would be large);
 * GET /notebooks/{id}/dag always returns the full document (the resync path).
 * The diff lives in ONE place (Python: app/notebook/dag_contract.py::diff_docs);
 * this module only APPLIES a delta, in the same order as dag_contract.apply_delta,
 * and verifies the result's graph_hash (sha256 of the canonical graph projection,
 * byte-identical to Python's).
 *
 * Revision = (version, runtime_version): both never decrease and every stored
 * write raises one, so lexicographic order is write order.
 *
 * Pure (no React, no network, no import.meta) — unit-tested under plain Node
 * (web/src/services/__tests__/dagDelta.test.mjs). Must not import depGraph.js.
 */

// Mirror of dag_contract.STAMP_KEYS (incl. `checkpoint`, the dag.json save stamp).
const STAMP_KEYS = ['version', 'built_at', 'notebook_id', 'trigger', 'graph_hash', 'runtime_version', 'runtime_hash', 'checkpoint']
const RUNTIME_TOP_KEYS = ['runtime', 'pending', 'run_seq']
const CLOCK_NODE_KEYS = ['executed_at', 'last_run_seq', 'last_ok_seq']
const EXCLUDED_TOP = new Set([...STAMP_KEYS, ...RUNTIME_TOP_KEYS])

const int0 = (v) => (typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : 0)

/** [version, runtime_version] of a document or delta target (absent → 0). */
export function revOf(doc) {
  if (!doc || typeof doc !== 'object') return [0, 0]
  return [int0(doc.version), int0(doc.runtime_version)]
}

/** Lexicographic compare of two [version, runtime_version] pairs (−1 / 0 / 1). */
export function compareRev(a, b) {
  if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1
  if (a[1] !== b[1]) return a[1] < b[1] ? -1 : 1
  return 0
}

/** Code-point string compare (Python's str ordering), not UTF-16 units. */
function cmpCodePoints(a, b) {
  const ia = a[Symbol.iterator]()
  const ib = b[Symbol.iterator]()
  for (;;) {
    const x = ia.next()
    const y = ib.next()
    if (x.done || y.done) return x.done === y.done ? 0 : (x.done ? -1 : 1)
    const cx = x.value.codePointAt(0)
    const cy = y.value.codePointAt(0)
    if (cx !== cy) return cx < cy ? -1 : 1
  }
}

function cmpPy(a, b) {
  if (typeof a === 'string' && typeof b === 'string') return cmpCodePoints(a, b)
  if (a === b) return 0
  if (a === null || a === undefined) return -1
  if (b === null || b === undefined) return 1
  return a < b ? -1 : 1
}

/**
 * Canonical JSON — the same text as Python's json.dumps(v, sort_keys=True,
 * ensure_ascii=False, separators=(",", ":")) for the values the graph holds
 * (ints, bools, strings, null, lists, dicts; no floats). A float or a lone
 * surrogate may serialize differently; the hash then mismatches and the
 * browser resyncs (fails safe).
 */
export function canonicalJson(v) {
  if (v === null || v === undefined) return 'null'
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(',')}]`
  if (typeof v === 'object') {
    const keys = Object.keys(v).filter(k => v[k] !== undefined).sort(cmpCodePoints)
    return `{${keys.map(k => `${JSON.stringify(k)}:${canonicalJson(v[k])}`).join(',')}}`
  }
  return JSON.stringify(v)
}

function nodeGraph(n) {
  if (!n || typeof n !== 'object' || Array.isArray(n)) return n
  const out = {}
  for (const [k, val] of Object.entries(n)) if (!CLOCK_NODE_KEYS.includes(k)) out[k] = val
  return out
}

/** The GRAPH part of a document (mirror of dag_contract.graph_projection). */
export function graphProjection(doc) {
  const out = {}
  for (const [k, v] of Object.entries(doc || {})) {
    if (EXCLUDED_TOP.has(k)) continue
    out[k] = k === 'nodes' && Array.isArray(v) ? v.map(nodeGraph) : v
  }
  return out
}

/**
 * sha256 hex of the canonical graph projection (== Python graph_hash), or null
 * when WebCrypto is unavailable (null counts as a mismatch → resync).
 */
export async function graphHash(doc) {
  const subtle = globalThis.crypto && globalThis.crypto.subtle
  if (!subtle || typeof TextEncoder === 'undefined') return null
  try {
    const bytes = new TextEncoder().encode(canonicalJson(graphProjection(doc)))
    const digest = await subtle.digest('SHA-256', bytes)
    return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return null
  }
}

/**
 * How `delta` relates to the local `doc`:
 *   'stale' — its target is not newer than the doc (incl. the unchanged marker
 *             for the current revision): drop it;
 *   'apply' — its base is the doc's revision;
 *   'gap'   — anything else (no local doc, a missed delta): resync.
 */
export function classifyDelta(doc, delta) {
  if (!doc || typeof doc !== 'object' || !delta || typeof delta !== 'object') return 'gap'
  const cur = revOf(doc)
  const target = [int0(delta.version), int0(delta.runtime_version)]
  if (compareRev(target, cur) <= 0) return 'stale'
  const base = [int0(delta.base_version), int0(delta.base_runtime_version)]
  return compareRev(base, cur) === 0 ? 'apply' : 'gap'
}

const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)))
const edgeKey = (e) => JSON.stringify([e.from, e.to, e.var, e.kind])

function applyRuntime(out, op) {
  if (!op || typeof op !== 'object') throw new Error('runtime op')
  if (op.drop) { delete out.runtime; return }
  if ('replace' in op) { out.runtime = clone(op.replace); return }
  if (!out.runtime || typeof out.runtime !== 'object') throw new Error('runtime base')
  const rt = { ...out.runtime }
  for (const k of op.unset || []) delete rt[k]
  for (const [k, v] of Object.entries(op.set || {})) rt[k] = clone(v)
  for (const section of ['vars', 'runs']) {
    const ups = op[`${section}_upsert`] || {}
    const rem = op[`${section}_remove`] || []
    if (Object.keys(ups).length === 0 && rem.length === 0) continue
    if (!rt[section] || typeof rt[section] !== 'object') throw new Error(`runtime ${section}`)
    const cur = { ...rt[section] }
    for (const name of rem) delete cur[name]
    for (const [name, v] of Object.entries(ups)) cur[name] = clone(v)
    rt[section] = cur
  }
  out.runtime = rt
}

/**
 * Apply a delta to a document (dag_contract.apply_delta order). Returns a NEW
 * object; never mutates `doc` or `delta`. Throws on a malformed op. The caller
 * checks the base revision (classifyDelta) and the graph hash.
 */
export function applyDagDelta(doc, delta) {
  const out = clone(doc)
  const byId = new Map((out.nodes || []).filter(n => n && typeof n === 'object').map(n => [n.cell_id, n]))
  for (const id of delta.nodes_remove || []) byId.delete(id)
  for (const n of delta.nodes_upsert || []) {
    if (!n || typeof n !== 'object' || !('cell_id' in n)) throw new Error('nodes_upsert')
    byId.set(n.cell_id, clone(n))
  }
  if ('nodes_upsert' in delta || 'nodes_remove' in delta) {
    out.nodes = [...byId.values()].sort((a, b) => cmpPy(a.position, b.position))
  }
  const pos = new Map([...byId].map(([id, n]) => [id, n.position]))
  if (['edges_add', 'edges_remove', 'nodes_upsert', 'nodes_remove'].some(k => k in delta)) {
    const edges = new Map((out.edges || []).map(e => [edgeKey(e), e]))
    for (const k of delta.edges_remove || []) {
      if (!Array.isArray(k) || k.length !== 4) throw new Error('edges_remove')
      edges.delete(JSON.stringify(k))
    }
    for (const e of delta.edges_add || []) {
      if (!e || typeof e !== 'object') throw new Error('edges_add')
      edges.set(edgeKey(e), clone(e))
    }
    for (const e of edges.values()) {
      if (!pos.has(e.to) || !pos.has(e.from)) throw new Error('edge endpoint')
    }
    out.edges = [...edges.values()].sort((a, b) =>
      cmpPy(pos.get(a.to), pos.get(b.to)) || cmpPy(a.var, b.var) || cmpPy(pos.get(a.from), pos.get(b.from)))
  }
  if ('issues' in delta) out.issues = clone(delta.issues)
  for (const k of delta.graph_unset || []) delete out[k]
  for (const [k, v] of Object.entries(delta.graph_set || {})) out[k] = clone(v)

  const clocks = delta.clocks
  if (clocks) {
    if ('run_seq' in clocks) out.run_seq = clocks.run_seq
    const nodes = new Map((out.nodes || []).map(n => [n.cell_id, n]))
    for (const [id, ck] of Object.entries(clocks.nodes || {})) {
      const n = nodes.get(id)
      if (!n || !ck || typeof ck !== 'object') throw new Error('clocks')
      for (const k of CLOCK_NODE_KEYS) if (k in ck) n[k] = ck[k]
    }
  }
  if ('runtime' in delta) applyRuntime(out, delta.runtime)
  if ('pending' in delta) {
    if (delta.pending === null) delete out.pending
    else out.pending = clone(delta.pending)
  }
  out.version = delta.version
  out.runtime_version = delta.runtime_version
  out.graph_hash = delta.graph_hash
  out.runtime_hash = delta.runtime_hash
  for (const k of ['built_at', 'trigger', 'notebook_id']) if (k in delta) out[k] = delta[k]
  return out
}

/**
 * Keeps one local document current from response payloads.
 *   getDoc()     — the current local document (or null)
 *   applyDoc(d)  — adopt a newer full document (the caller's forward-only setter)
 *   fetchFull()  — GET /notebooks/{id}/dag → full document (or null)
 *   hash(d)      — graph hash (default graphHash)
 *
 * ingest(payload): `payload.dag` (full) or `payload.dag_delta`. Payloads are
 * handled strictly in arrival order (one promise chain). A gap, a hash mismatch,
 * a malformed delta or a payload with neither field marks the doc as behind;
 * settled() then awaits the chain and does ONE single-flight fetchFull. Readers
 * that decide on the document (reactive auto-run) await settled() first.
 */
export function createDagSync({ getDoc, applyDoc, fetchFull, hash = graphHash }) {
  let chain = Promise.resolve()
  let needResync = false
  let inflight = null

  async function handle(payload) {
    if (!payload || typeof payload !== 'object') { needResync = true; return }
    if (payload.dag && typeof payload.dag === 'object') { applyDoc(payload.dag); return }
    const delta = payload.dag_delta
    if (!delta || typeof delta !== 'object') { needResync = true; return }
    const cur = getDoc()
    const kind = classifyDelta(cur, delta)
    if (kind === 'stale') return
    if (kind === 'gap') { needResync = true; return }
    let next
    try {
      next = applyDagDelta(cur, delta)
    } catch {
      needResync = true
      return
    }
    const h = await hash(next)
    if (!h || h !== delta.graph_hash) { needResync = true; return }
    applyDoc(next)
  }

  return {
    ingest(payload) {
      chain = chain.then(() => handle(payload)).catch(() => { needResync = true })
      return chain
    },
    requestResync() { needResync = true },
    // Loops until nothing is pending: a gap ingested while an older fetch is in
    // flight (that fetch may have read the row before the newer write) gets its
    // own fetch, so callers never resolve on a doc known to be behind. Still
    // single-flight; terminates because needResync is cleared before each fetch
    // and only a new ingest/requestResync sets it again.
    async settled() {
      await chain
      while (needResync || inflight) {
        if (!inflight) {
          needResync = false
          inflight = Promise.resolve()
            .then(() => fetchFull())
            .then(doc => { if (doc) applyDoc(doc) })
            .catch(() => {})
            .finally(() => { inflight = null })
        }
        await inflight
        await chain
      }
    },
  }
}
