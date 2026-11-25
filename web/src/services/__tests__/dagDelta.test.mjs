// Unit tests for services/dagDelta.js — applying the proxy's DAG deltas.
// The cases are real builder output stamped by storage's stamp_doc and diffed by
// app/notebook/dag_contract.py::diff_docs (tests/fixtures/gen_dag_fixtures.py →
// tests/fixtures/dag_deltas.json), so these checks are Python/JS parity checks.
// Run with: node --test web/src/services/__tests__/dagDelta.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  applyDagDelta, canonicalJson, classifyDelta, compareRev, createDagSync, graphHash, revOf,
} from '../dagDelta.js'
import { newerDag } from '../depGraph.js'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', 'tests', 'fixtures')
const CASES = JSON.parse(readFileSync(join(FIXTURES, 'dag_deltas.json'), 'utf8')).cases
const clone = (v) => JSON.parse(JSON.stringify(v))

test('every fixture case: applyDagDelta(prev, delta) deep-equals next; graphHash matches Python', async () => {
  const names = Object.keys(CASES)
  assert.ok(names.length >= 10, `cases: ${names}`)
  for (const name of names) {
    const c = CASES[name]
    const prevBefore = clone(c.prev)
    const deltaBefore = clone(c.delta)
    assert.equal(classifyDelta(c.prev, c.delta), name === 'nothing_changed' ? 'stale' : 'apply', name)
    const out = applyDagDelta(c.prev, c.delta)
    assert.deepEqual(out, c.next, name)
    assert.equal(await graphHash(c.next), c.graph_hash, `${name}: graphHash(next) == Python graph_hash`)
    assert.equal(await graphHash(out), c.delta.graph_hash, name)
    assert.deepEqual(c.prev, prevBefore, `${name}: prev not mutated`)
    assert.deepEqual(c.delta, deltaBefore, `${name}: delta not mutated`)
  }
})

test('non-ASCII var and emoji source id hash identically to Python', async () => {
  const c = CASES.non_ascii
  assert.ok(canonicalJson(c.next.nodes).includes('données'))
  assert.ok(canonicalJson(c.next.nodes).includes('🚀'))
  assert.equal(await graphHash(c.next), c.graph_hash)
})

test('graphHash ignores stamps, runtime, pending, run_seq and node clocks', async () => {
  const d = clone(CASES.upstream_rerun.next)
  const h = await graphHash(d)
  const v = { ...clone(d), version: 99, runtime_version: 5, built_at: 'x', trigger: 't', graph_hash: 'g',
    runtime_hash: 'r', notebook_id: 3, run_seq: 1234, runtime: { clock: 0 }, pending: { z: {} } }
  v.nodes = v.nodes.map(n => ({ ...n, executed_at: 'later', last_run_seq: 77, last_ok_seq: 77 }))
  assert.equal(await graphHash(v), h)
  const g = clone(d)
  g.nodes[0].reads = [...g.nodes[0].reads, 'zz']
  assert.notEqual(await graphHash(g), h)
})

test('dag.json checkpoint stamp is never hashed (Python/JS parity fixture)', async () => {
  // tests/fixtures/dag_hash_parity.json: the same stamped doc with and without
  // the `checkpoint` key the VM adds at save; graph_hash is Python's.
  const fx = JSON.parse(readFileSync(join(FIXTURES, 'dag_hash_parity.json'), 'utf8'))
  assert.ok(fx.doc_with_checkpoint.checkpoint && !('checkpoint' in fx.doc_without_checkpoint))
  assert.equal(await graphHash(fx.doc_with_checkpoint), fx.graph_hash)
  assert.equal(await graphHash(fx.doc_without_checkpoint), fx.graph_hash)
  // A delta onto a restored copy that still carries the stamp verifies.
  const c = CASES.upstream_rerun
  const restored = { ...clone(c.prev), checkpoint: fx.doc_with_checkpoint.checkpoint }
  const out = applyDagDelta(restored, c.delta)
  assert.equal(await graphHash(out), c.delta.graph_hash)
})

test('leaf re-run is unchanged with clocks/runtime only; nothing_changed is the marker', () => {
  const leaf = CASES.leaf_rerun.delta
  assert.equal(leaf.unchanged, true)
  assert.equal(leaf.version, leaf.base_version)
  assert.ok(leaf.clocks && leaf.runtime)
  for (const k of ['nodes_upsert', 'nodes_remove', 'edges_add', 'edges_remove', 'issues']) assert.ok(!(k in leaf), k)
  const m = CASES.nothing_changed.delta
  assert.deepEqual(revOf({ version: m.version, runtime_version: m.runtime_version }),
    [m.base_version, m.base_runtime_version])
})

test('classifyDelta: stale / apply / gap', () => {
  const doc = { version: 3, runtime_version: 7 }
  const d = (bv, brv, v, rv) => ({ base_version: bv, base_runtime_version: brv, version: v, runtime_version: rv })
  assert.equal(classifyDelta(doc, d(3, 7, 3, 8)), 'apply')
  assert.equal(classifyDelta(doc, d(3, 7, 4, 7)), 'apply')
  assert.equal(classifyDelta(doc, d(3, 7, 3, 7)), 'stale', 'unchanged marker for the current rev')
  assert.equal(classifyDelta(doc, d(2, 1, 3, 6)), 'stale', 'older target')
  assert.equal(classifyDelta(doc, d(3, 8, 3, 9)), 'gap', 'missed a delta')
  assert.equal(classifyDelta(doc, d(2, 0, 4, 0)), 'gap')
  assert.equal(classifyDelta(null, d(0, 0, 1, 1)), 'gap', 'no local doc')
  assert.equal(compareRev([1, 9], [2, 0]), -1)
  assert.equal(compareRev([2, 1], [2, 0]), 1)
  assert.deepEqual(revOf({ version: 4 }), [4, 0])
})

// ── createDagSync: arrival order, resync, single flight ────────────────────────

function harness(start, { fullDoc = null, hash } = {}) {
  const state = { doc: start, fetches: 0, applied: [] }
  const sync = createDagSync({
    getDoc: () => state.doc,
    applyDoc: (d) => {
      if (!newerDag(state.doc, d)) return false
      state.doc = d
      state.applied.push(revOf(d))
      return true
    },
    fetchFull: async () => {
      state.fetches += 1
      await new Promise(r => setTimeout(r, 5))
      return fullDoc
    },
    ...(hash ? { hash } : {}),
  })
  return { state, sync }
}

// A chain of three real deltas v1 → v2 → v3 → v4 built from the fixtures.
function chain3() {
  const c = CASES.upstream_rerun
  const d1 = c.delta
  const n1 = c.next
  // A second and third delta on top: runtime-only bumps (no graph ops).
  const mk = (base, clock) => {
    const next = clone(base)
    next.runtime = { ...next.runtime, clock }
    next.runtime_version = base.runtime_version + 1
    next.runtime_hash = `rh-${clock}`
    const delta = {
      delta: 1, schema: 1, base_version: base.version, base_runtime_version: base.runtime_version,
      version: next.version, runtime_version: next.runtime_version, graph_hash: base.graph_hash,
      runtime_hash: next.runtime_hash, unchanged: true, notebook_id: base.notebook_id, built_at: base.built_at,
      trigger: base.trigger, runtime: { set: { clock }, unset: [] },
    }
    return { next, delta }
  }
  const s2 = mk(n1, 100)
  const s3 = mk(s2.next, 101)
  return { start: c.prev, docs: [n1, s2.next, s3.next], deltas: [d1, s2.delta, s3.delta] }
}

test('in-order deltas apply without any fetch; settled() resolves to the applied doc', async () => {
  const { start, docs, deltas } = chain3()
  const { state, sync } = harness(start)
  for (const d of deltas) sync.ingest({ dag_delta: d })
  await sync.settled()
  assert.equal(state.fetches, 0)
  assert.deepEqual(state.doc, docs[2], 'the doc the reactive path reads after settled()')
})

test('version mismatch → exactly one fetch at settled()', async () => {
  const { start, docs, deltas } = chain3()
  const { state, sync } = harness(start, { fullDoc: docs[2] })
  await sync.ingest({ dag_delta: deltas[1] })   // base v2 while the doc is v1 → gap
  assert.equal(state.fetches, 0, 'no fetch per frame; deferred to settled()')
  await sync.settled()
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[2])
  await sync.settled()
  assert.equal(state.fetches, 1, 'no further fetch once resynced')
})

test('out-of-order frames (v3→v4 before v2→v3): one fetch, then the stale delta is dropped', async () => {
  const { start, docs, deltas } = chain3()
  const { state, sync } = harness(start, { fullDoc: docs[2] })
  await sync.ingest({ dag_delta: deltas[0] })   // v1 → v2 applies
  await sync.ingest({ dag_delta: deltas[2] })   // late-arriving order: gap
  await sync.settled()
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[2])
  await sync.ingest({ dag_delta: deltas[1] })   // older target now → stale, dropped
  await sync.settled()
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[2])
})

test('two gaps and concurrent settled() calls → a single in-flight fetch', async () => {
  const { start, docs, deltas } = chain3()
  const { state, sync } = harness(start, { fullDoc: docs[2] })
  sync.ingest({ dag_delta: deltas[1] })
  sync.ingest({ dag_delta: deltas[2] })
  await Promise.all([sync.settled(), sync.settled(), sync.settled()])
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[2])
})

test('gap ingested while a fetch is in flight → second fetch; settled() resolves on the newer doc', async () => {
  const { start, docs, deltas } = chain3()
  const state = { doc: start, fetches: 0 }
  const releases = []
  // fetch #1 read the row before B's write (returns docs[1]); fetch #2 sees it (docs[2]).
  const results = [docs[1], docs[2]]
  const sync = createDagSync({
    getDoc: () => state.doc,
    applyDoc: (d) => { if (!newerDag(state.doc, d)) return false; state.doc = d; return true },
    fetchFull: () => {
      const doc = results[state.fetches]
      state.fetches += 1
      return new Promise(r => releases.push(() => r(doc)))
    },
  })
  await sync.ingest({ dag_delta: deltas[1] })          // run A: gap (local v1, base v2)
  const settledA = sync.settled()
  await new Promise(r => setTimeout(r, 0))
  assert.equal(state.fetches, 1, 'A started the single-flight fetch')
  await sync.ingest({ dag_delta: deltas[2] })          // run B: gap while A's fetch is in flight
  let bDone = false
  const settledB = sync.settled().then(() => { bDone = true })
  releases[0]()                                         // A's (older) fetch returns docs[1]
  for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0))
  assert.deepEqual(state.doc, docs[1], 'older fetch applied')
  assert.equal(state.fetches, 2, 'the newer gap triggers its own fetch')
  assert.equal(bDone, false, 'B must not resolve on the older fetch')
  releases[1]()
  await Promise.all([settledA, settledB])
  assert.deepEqual(state.doc, docs[2], 'B decides on the doc that includes its run')
  assert.deepEqual(revOf(state.doc), revOf(docs[2]))
})

test('hash mismatch after apply → resync (the bad doc is never adopted)', async () => {
  const { start, docs, deltas } = chain3()
  const bad = clone(deltas[0])
  bad.nodes_upsert[0].reads = ['tampered']
  const { state, sync } = harness(start, { fullDoc: docs[0] })
  await sync.ingest({ dag_delta: bad })
  assert.deepEqual(state.doc, start, 'not applied before the resync')
  await sync.settled()
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[0])
})

test('no WebCrypto (hash → null) counts as a mismatch → resync', async () => {
  const { start, docs, deltas } = chain3()
  const { state, sync } = harness(start, { fullDoc: docs[0], hash: async () => null })
  await sync.ingest({ dag_delta: deltas[0] })
  await sync.settled()
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[0])
})

test('full dag payload is adopted; dag_error / missing payload / requestResync → resync', async () => {
  const { start, docs } = chain3()
  const h = harness(null, { fullDoc: docs[2] })
  await h.sync.ingest({ dag: start })
  assert.deepEqual(h.state.doc, start)
  await h.sync.settled()
  assert.equal(h.state.fetches, 0)
  await h.sync.ingest({ dag_error: 'capture_failed' })
  await h.sync.settled()
  assert.equal(h.state.fetches, 1)
  assert.deepEqual(h.state.doc, docs[2])
  h.sync.requestResync()
  await h.sync.settled()
  assert.equal(h.state.fetches, 2)
})

test('no local doc + delta → gap → fetch', async () => {
  const { docs, deltas } = chain3()
  const { state, sync } = harness(null, { fullDoc: docs[0] })
  await sync.ingest({ dag_delta: deltas[0] })
  await sync.settled()
  assert.equal(state.fetches, 1)
  assert.deepEqual(state.doc, docs[0])
})

test('a full doc arriving first makes the matching delta stale (save PUT racing an execute)', async () => {
  const { start, docs, deltas } = chain3()
  const { state, sync } = harness(start)
  await sync.ingest({ dag: docs[0] })
  await sync.ingest({ dag_delta: deltas[0] })
  await sync.settled()
  assert.equal(state.fetches, 0)
  assert.deepEqual(state.doc, docs[0])
})
