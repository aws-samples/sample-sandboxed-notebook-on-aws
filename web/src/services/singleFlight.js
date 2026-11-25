// Pure single-flight dedupe for the post-restore hydration burst. Several effects
// ask for the same read (/packages, /package-categories, /image-tiers) within a
// second of a VM connecting; while one request for a key is in flight, later
// callers get the same promise instead of a second request. The entry is dropped
// when the promise settles, so the next call after that fetches fresh data, and a
// rejection reaches every caller that shared it and is never cached.
//
// A caller that asks after a state change (an install or uninstall finished, an
// explicit Refresh) passes { fresh: true }. With nothing in flight it starts at
// once, like any call. With a fetch in flight it must not get that fetch's
// result, which may predate the change: it marks the flight dirty and gets the
// result of exactly ONE more fetch that starts when the current one settles
// (resolved or rejected). Every fresh caller that arrives before that rerun
// starts shares it, and so does a plain caller arriving after the flight went
// dirty (the rerun is the newer data). The rerun uses the latest caller's fn.
// No React or DOM imports, so node --test can load it.
export function createSingleFlight() {
  const inflight = new Map() // key -> { promise, rerun: { fn, promise, resolve, reject } | null }

  const start = (key, fn) => {
    let p
    try {
      p = Promise.resolve(fn())
    } catch (e) {
      p = Promise.reject(e)
    }
    const entry = { promise: null, rerun: null }
    const settle = () => {
      if (inflight.get(key) !== entry) return
      const rerun = entry.rerun
      if (!rerun) {
        inflight.delete(key)
        return
      }
      // The queued rerun becomes the in-flight fetch for this key.
      const next = start(key, rerun.fn)
      next.promise.then(rerun.resolve, rerun.reject)
    }
    entry.promise = p.finally(settle)
    inflight.set(key, entry)
    return entry
  }

  return {
    /**
     * @param {string} key
     * @param {() => any} fn
     * @param {{fresh?: boolean}} [opts]
     */
    run(key, fn, { fresh = false } = {}) {
      const entry = inflight.get(key)
      if (!entry) return start(key, fn).promise
      if (entry.rerun) {
        if (fresh) entry.rerun.fn = fn
        return entry.rerun.promise
      }
      if (!fresh) return entry.promise
      let resolve, reject
      const promise = new Promise((res, rej) => { resolve = res; reject = rej })
      entry.rerun = { fn, promise, resolve, reject }
      return promise
    },
    size() {
      return inflight.size
    },
  }
}

/** Process-wide instance shared by the UI components. */
export const sharedFlight = createSingleFlight()

/**
 * Run `doFetch` once per key while in flight and parse its body once, so callers
 * never share a consumed Response.
 * @param {string} key
 * @param {() => Promise<Response>} doFetch
 * @param {{run: Function}} [flight] defaults to sharedFlight
 * @param {{fresh?: boolean}} [opts] fresh: called after a state change; never
 *   gets a fetch that was already in flight (see createSingleFlight)
 * @returns {Promise<{ok: boolean, status: number, data: any}>}
 */
export function sharedJson(key, doFetch, flight = sharedFlight, opts = {}) {
  return flight.run(key, async () => {
    const r = await doFetch()
    const data = r.ok ? await r.json() : null
    return { ok: r.ok, status: r.status, data }
  }, opts)
}
