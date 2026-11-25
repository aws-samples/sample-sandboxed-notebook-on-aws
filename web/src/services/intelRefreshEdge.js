// Pure edge detector: when should the single Workbook Intel poller
// (hooks/useWorkbookIntel.js) fire ONE 'refresh-datasources'? (proxy-log-chatter B2)
//
// Intel generation creates the entity docs that drive the Data Sources sparkle
// icons, so the panel must refresh once a generation it is waiting for finishes.
// This replaces the per-trigger watchers that useNotebookFiles used to start (one
// extra 4 s /workbook-intel poll chain per upload/delete, never cancelled, and up
// to ~33 min of polling when the backend no-oped the trigger).
//
// Rules:
//   * arm on a trigger (intel-generation-started for this session, or regenerate);
//   * remember that the server reported a generation in flight (phase1/phase2);
//   * on 'ready': fire once if we saw a generation in flight, or if we were armed
//     and the report changed (a job fast enough to finish between two polls).
//     A mount-time 'ready' for an unchanged report never fires;
//   * 'not_generated' after the trigger grace window, or 'error': disarm silently
//     (the backend no-oped; nothing changed, nothing to refresh);
//   * reset() on session change.

export function createIntelRefreshEdge() {
  let armed = false
  let sawInFlight = false
  let lastReadySig = null

  return {
    /** A trigger fired for this session. */
    arm() { armed = true },
    /**
     * Feed one fetched phase. Returns true exactly when the caller should
     * dispatch 'refresh-datasources'.
     * @param {string} phase       phase1 | phase2 | ready | not_generated | error
     * @param {object} [o]
     * @param {string} [o.readySig]       identity of the ready report (generated_at)
     * @param {boolean} [o.withinGrace]   not_generated is still inside the trigger grace window
     */
    observe(phase, { readySig = null, withinGrace = false } = {}) {
      if (phase === 'phase1' || phase === 'phase2') {
        sawInFlight = true
        return false
      }
      if (phase === 'ready') {
        const changed = readySig !== lastReadySig
        const fire = sawInFlight || (armed && changed)
        lastReadySig = readySig
        // An armed trigger that still sees the OLD report (the poll raced ahead
        // of POST /generate) stays armed until the new report or a phase lands.
        if (fire) { armed = false; sawInFlight = false }
        return fire
      }
      if (phase === 'not_generated') {
        if (!withinGrace) { armed = false; sawInFlight = false }
        return false
      }
      if (phase === 'error') { armed = false; sawInFlight = false }
      return false
    },
    reset() { armed = false; sawInFlight = false; lastReadySig = null },
    state() { return { armed, sawInFlight, lastReadySig } },
  }
}
