// Unit tests for services/refreshCoalescer.js (Data Sources refresh coalescing, B3).
// Run with: node --test web/src/services/__tests__/refreshCoalescer.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRefreshCoalescer, REFRESH_COALESCE_MS } from '../refreshCoalescer.js'

// Fake clock: timers fire only when advance() passes their due time.
function fakeClock() {
  let now = 0
  let id = 0
  const timers = new Map()
  return {
    setTimer: (fn, ms) => { timers.set(++id, { fn, at: now + ms }); return id },
    clearTimer: (h) => { timers.delete(h) },
    async advance(ms) {
      const end = now + ms
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)
        if (!due.length) break
        const [h, t] = due[0]
        timers.delete(h)
        now = t.at
        t.fn()
        await flushMicrotasks()
      }
      now = end
    },
    pending: () => timers.size,
  }
}
const flushMicrotasks = async () => { for (let i = 0; i < 10; i++) await Promise.resolve() }
function deferred() {
  let resolve
  const p = new Promise(r => { resolve = r })
  return { p, resolve }
}

test('default quiet period is 1.5 s', () => assert.equal(REFRESH_COALESCE_MS, 1500))

test('a burst of requests runs one fetch on the trailing edge', async () => {
  const clock = fakeClock()
  const calls = []
  const c = createRefreshCoalescer({ run: async (f) => { calls.push(f) }, delayMs: 1500, ...clock })
  c.request(true); await clock.advance(500)
  c.request(true); await clock.advance(500)
  c.request(true); await clock.advance(1400)
  assert.equal(calls.length, 0, 'still inside the quiet window')
  await clock.advance(200)
  assert.deepEqual(calls, [true])
})

test('force is sticky across merged requests; all non-forced stays non-forced', async () => {
  const clock = fakeClock()
  const calls = []
  const c = createRefreshCoalescer({ run: async (f) => { calls.push(f) }, ...clock })
  c.request(false); c.request(true); c.request(false)
  await clock.advance(REFRESH_COALESCE_MS)
  c.request(false); c.request(false)
  await clock.advance(REFRESH_COALESCE_MS)
  assert.deepEqual(calls, [true, false])
})

test('requests during an in-flight fetch queue exactly one rerun after it', async () => {
  const clock = fakeClock()
  const runs = []
  let gate = deferred()
  const c = createRefreshCoalescer({
    run: async (f) => { runs.push(f); await gate.p },
    delayMs: 100, ...clock,
  })
  c.request(false)
  await clock.advance(100)
  assert.equal(runs.length, 1)
  assert.equal(c.state().inFlight, true)
  // Three more bursts while the first fetch is still running.
  for (let i = 0; i < 3; i++) { c.request(i === 1); await clock.advance(150) }
  assert.equal(runs.length, 1, 'never two fetches at once')
  assert.equal(c.state().rerun, true)
  const first = gate
  gate = deferred()
  first.resolve()
  await flushMicrotasks()
  assert.equal(runs.length, 2, 'one rerun right after the current fetch')
  assert.equal(runs[1], true, 'the rerun carries the forced flag of the queued requests')
  gate.resolve()
  await flushMicrotasks()
  await clock.advance(1000)
  assert.equal(runs.length, 2, 'no further runs')
})

test('a failing fetch does not wedge the coalescer', async () => {
  const clock = fakeClock()
  let n = 0
  const c = createRefreshCoalescer({ run: async () => { n++; throw new Error('boom') }, delayMs: 10, ...clock })
  c.request(); await clock.advance(10)
  c.request(); await clock.advance(10)
  assert.equal(n, 2)
  assert.equal(c.state().inFlight, false)
})

test('cancel drops a pending refresh and ignores later requests', async () => {
  const clock = fakeClock()
  let n = 0
  const c = createRefreshCoalescer({ run: async () => { n++ }, delayMs: 10, ...clock })
  c.request(true)
  c.cancel()
  c.request(true)
  await clock.advance(100)
  assert.equal(n, 0)
  assert.equal(clock.pending(), 0)
})

test('SageMaker delete burst budget: 4 deletes -> at most 2 forced fetches per delete', async () => {
  // Old behaviour: each delete = 1 immediate + 3 nudges + 1 watcher refresh = 5
  // forced fetches, 20 for the 4 deletes in the report. Now each delete sends its
  // immediate (forced) refresh plus ONE non-forced nudge 6 s later, and intel
  // completion sends ONE refresh; the coalescer merges whatever overlaps.
  const clock = fakeClock()
  const calls = []
  const c = createRefreshCoalescer({ run: async (f) => { calls.push(f) }, ...clock })
  const events = []
  for (const click of [0, 6700, 12000, 17300]) {        // clicks ~5-7 s apart
    events.push([click, true])                         // deleteS3File refresh
    events.push([click + 6000, false])                 // the single nudge
  }
  events.push([90000, true])                           // intel ready edge (once)
  events.sort((a, b) => a[0] - b[0])
  let t = 0
  for (const [at, force] of events) { await clock.advance(at - t); t = at; c.request(force) }
  await clock.advance(5000)
  assert.ok(calls.length <= 9, `expected <= 9 fetches, got ${calls.length}`)
  assert.ok(calls.filter(Boolean).length <= 5, `forced fetches: ${calls.filter(Boolean).length}`)
})
