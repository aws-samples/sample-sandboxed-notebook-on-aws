// Unit tests for services/singleFlight.js (post-restore hydration burst dedupe).
// Run with: node --test web/src/services/__tests__/singleFlight.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createSingleFlight, sharedJson } from '../singleFlight.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

test('concurrent same-key calls run fn once and share the result', async () => {
  const f = createSingleFlight()
  const d = deferred()
  let calls = 0
  const fn = () => { calls++; return d.promise }
  const a = f.run('k', fn)
  const b = f.run('k', fn)
  d.resolve(42)
  assert.deepEqual(await Promise.all([a, b]), [42, 42])
  assert.equal(calls, 1)
})

test('different keys run separately', async () => {
  const f = createSingleFlight()
  let calls = 0
  const fn = async () => ++calls
  const [a, b] = await Promise.all([f.run('a', fn), f.run('b', fn)])
  assert.equal(calls, 2)
  assert.notEqual(a, b)
})

test('a key is re-runnable after it settles', async () => {
  const f = createSingleFlight()
  let calls = 0
  const fn = async () => ++calls
  assert.equal(await f.run('k', fn), 1)
  assert.equal(f.size(), 0)
  assert.equal(await f.run('k', fn), 2)
})

test('a rejection reaches every caller and is not cached', async () => {
  const f = createSingleFlight()
  const d = deferred()
  let calls = 0
  const fn = () => { calls++; return d.promise }
  const a = f.run('k', fn)
  const b = f.run('k', fn)
  d.reject(new Error('boom'))
  await assert.rejects(a, /boom/)
  await assert.rejects(b, /boom/)
  assert.equal(f.size(), 0)
  assert.equal(await f.run('k', async () => { calls++; return 'ok' }), 'ok')
  assert.equal(calls, 2)
})

test('a synchronous throw in fn rejects and is not cached', async () => {
  const f = createSingleFlight()
  await assert.rejects(f.run('k', () => { throw new Error('sync') }), /sync/)
  assert.equal(f.size(), 0)
})

test('sharedJson parses the body once for all callers', async () => {
  const f = createSingleFlight()
  let fetches = 0
  let parses = 0
  const d = deferred()
  const doFetch = () => {
    fetches++
    return d.promise.then(() => ({ ok: true, status: 200, json: async () => { parses++; return { x: 1 } } }))
  }
  const a = sharedJson('k', doFetch, f)
  const b = sharedJson('k', doFetch, f)
  d.resolve()
  const [ra, rb] = await Promise.all([a, b])
  assert.deepEqual(ra, { ok: true, status: 200, data: { x: 1 } })
  assert.deepEqual(rb, ra)
  assert.equal(fetches, 1)
  assert.equal(parses, 1)
})

test('sharedJson gives {ok:false, data:null} on a non-ok response', async () => {
  const f = createSingleFlight()
  let parses = 0
  const r = await sharedJson('k', async () => ({ ok: false, status: 503, json: async () => { parses++; return {} } }), f)
  assert.deepEqual(r, { ok: false, status: 503, data: null })
  assert.equal(parses, 0)
})

// --- fresh (state-change) callers: rerun once after the current fetch ---

const tick = () => new Promise(r => setTimeout(r, 0))

test('hydration burst of N plain callers is one fetch', async () => {
  const f = createSingleFlight()
  const d = deferred()
  let calls = 0
  const fn = () => { calls++; return d.promise }
  const ps = Array.from({ length: 12 }, () => f.run('packages', fn))
  d.resolve(['a'])
  const res = await Promise.all(ps)
  assert.equal(calls, 1)
  assert.ok(res.every(r => r[0] === 'a'))
  assert.equal(f.size(), 0)
})

test('fresh request during a flight: exactly 2 fetches, late callers get the second result', async () => {
  const f = createSingleFlight()
  const fetches = []
  const fn = () => { const d = deferred(); fetches.push(d); return d.promise }
  const early = [f.run('k', fn), f.run('k', fn)] // burst before the install finished
  const late = [f.run('k', fn, { fresh: true }), f.run('k', fn, { fresh: true })]
  const plainAfterDirty = f.run('k', fn)
  assert.equal(fetches.length, 1, 'the rerun waits for the current fetch')
  fetches[0].resolve('stale')
  await tick()
  assert.equal(fetches.length, 2, 'one rerun starts after the first settles')
  fetches[1].resolve('fresh')
  assert.deepEqual(await Promise.all(early), ['stale', 'stale'])
  assert.deepEqual(await Promise.all(late), ['fresh', 'fresh'])
  assert.equal(await plainAfterDirty, 'fresh')
  assert.equal(fetches.length, 2)
  assert.equal(f.size(), 0)
})

test('fresh request with nothing in flight starts at once (manual Refresh)', async () => {
  const f = createSingleFlight()
  let calls = 0
  const d = deferred()
  const p = f.run('k', () => { calls++; return d.promise }, { fresh: true })
  assert.equal(calls, 1)
  const joined = f.run('k', () => { calls++; return 'x' }) // the effect's plain call joins it
  d.resolve('now')
  assert.deepEqual(await Promise.all([p, joined]), ['now', 'now'])
  assert.equal(calls, 1)
})

test('the rerun uses the latest fresh caller fn', async () => {
  const f = createSingleFlight()
  const d = deferred()
  f.run('k', () => d.promise)
  const a = f.run('k', async () => 'old-closure', { fresh: true })
  const b = f.run('k', async () => 'new-closure', { fresh: true })
  d.resolve('first')
  assert.deepEqual(await Promise.all([a, b]), ['new-closure', 'new-closure'])
})

test('first fetch failing still runs the rerun; rerun error reaches late callers; nothing wedged', async () => {
  const f = createSingleFlight()
  const fetches = []
  const fn = () => { const d = deferred(); fetches.push(d); return d.promise }
  const early = f.run('k', fn)
  const late = f.run('k', fn, { fresh: true })
  fetches[0].reject(new Error('first failed'))
  await assert.rejects(early, /first failed/)
  await tick()
  assert.equal(fetches.length, 2)
  fetches[1].reject(new Error('rerun failed'))
  await assert.rejects(late, /rerun failed/)
  assert.equal(f.size(), 0)
  assert.equal(await f.run('k', async () => 'recovered'), 'recovered')
})

test('a fresh caller during the rerun queues one more rerun', async () => {
  const f = createSingleFlight()
  const fetches = []
  const fn = () => { const d = deferred(); fetches.push(d); return d.promise }
  f.run('k', fn)
  const second = f.run('k', fn, { fresh: true })
  fetches[0].resolve(1)
  await tick()
  const third = f.run('k', fn, { fresh: true })
  fetches[1].resolve(2)
  await tick()
  assert.equal(fetches.length, 3)
  fetches[2].resolve(3)
  assert.equal(await second, 2)
  assert.equal(await third, 3)
  assert.equal(f.size(), 0)
})

test('sharedJson passes fresh through', async () => {
  const f = createSingleFlight()
  let fetches = 0
  const d = deferred()
  const doFetch = () => {
    fetches++
    const n = fetches
    return (n === 1 ? d.promise : Promise.resolve()).then(() => ({ ok: true, status: 200, json: async () => ({ n }) }))
  }
  const a = sharedJson('k', doFetch, f)
  const b = sharedJson('k', doFetch, f, { fresh: true })
  d.resolve()
  assert.deepEqual((await a).data, { n: 1 })
  assert.deepEqual((await b).data, { n: 2 })
  assert.equal(fetches, 2)
})
