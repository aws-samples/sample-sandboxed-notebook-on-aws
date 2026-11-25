// Pure trailing-edge coalescer for the Data Sources 'refresh-datasources' event
// (proxy-log-chatter B3). A burst of refresh requests (a delete fires one right
// away, the panel nudges, intel completion fires another) becomes ONE fetch after
// the burst goes quiet for `delayMs`. While a fetch is in flight, new requests
// mark a single rerun that starts right after it finishes, so there is never more
// than one fetch running and never more than one queued. `force` is sticky across
// the merged requests: if any request in the batch was forced, the fetch is forced.
// No React or DOM imports, so node --test can load it; timers are injectable.

/** Default quiet period before a coalesced refresh runs (ms). */
export const REFRESH_COALESCE_MS = 1500

/**
 * @param {object} opts
 * @param {(force: boolean) => Promise<any>|any} opts.run  the real fetch
 * @param {number} [opts.delayMs]
 * @param {Function} [opts.setTimer]   defaults to setTimeout
 * @param {Function} [opts.clearTimer] defaults to clearTimeout
 */
export function createRefreshCoalescer({ run, delayMs = REFRESH_COALESCE_MS,
  setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  let timer = null
  let inFlight = false
  let rerun = false
  let pendingForce = false
  let disposed = false

  const flush = async () => {
    timer = null
    if (disposed) return
    if (inFlight) { rerun = true; return }
    inFlight = true
    const force = pendingForce
    pendingForce = false
    try {
      await run(force)
    } catch { /* the fetch reports its own errors */ }
    inFlight = false
    if (rerun && !disposed) {
      rerun = false
      flush()
    }
  }

  return {
    /** Ask for a refresh; `force` = bypass the suspend gate / refresh the catalog. */
    request(force = false) {
      if (disposed) return
      pendingForce = pendingForce || !!force
      if (timer !== null) clearTimer(timer)
      timer = setTimer(flush, delayMs)
    },
    /** Drop any pending refresh (unmount / session change). */
    cancel() {
      disposed = true
      if (timer !== null) clearTimer(timer)
      timer = null
      rerun = false
    },
    /** For tests and diagnostics. */
    state() { return { pending: timer !== null, inFlight, rerun, pendingForce } },
  }
}
