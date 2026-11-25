/**
 * useWorkbookIntel — encapsulates the full Workbook Intelligence lifecycle so it can
 * live inside the AI Assistant panel (and anywhere else) without dragging the fetch /
 * polling / generation state into the presentation layer.
 *
 * Responsibilities:
 *  - GET /workbook-intel with X-Session-Id (one fetch, then fast polling only while a
 *    generation is in flight; see "Event-driven polling" below)
 *  - status machine: loading | not_generated | generating | ready | error
 *  - Phase-2 prose report status (reportStatus: generating | ready)
 *  - incremental update / deletion state (isUpdating + updateReason)
 *  - module-level _generatingMap so a "generating" state survives unmount/remount
 *  - regenerate() → POST /workbook-intel/generate (manual refresh)
 *  - stale-report guard: ignore a fetched report older than a pending regenerate
 *
 * Returns a plain data object; the consumer owns all rendering + expand/collapse UI.
 */

import { useState, useEffect, useCallback, useRef } from 'react'
import { apiFetch } from '../services/apiFetch'
import { createIntelRefreshEdge } from '../services/intelRefreshEdge'
// Persist "generating" across unmount/remount (same as the old IntelPanel module cache).
const _generatingMap = new Map()  // sessionId → timestamp when generation started

export function useWorkbookIntel(sessionId) {
  const [intel, setIntel] = useState(null)
  const [status, setStatus] = useState('loading')      // loading|not_generated|generating|ready|error
  const [phase, setPhase] = useState(null)             // phase1|phase2|ready|not_generated (authoritative, from backend)
  const [generatedAt, setGeneratedAt] = useState(null)
  const [isUpdating, setIsUpdating] = useState(false)   // incremental delta/prune in flight
  const [updateReason, setUpdateReason] = useState('addition')  // 'addition' | 'deletion'
  const [reportStatus, setReportStatus] = useState('ready')     // Phase-2 prose report

  const regeneratingRef = useRef(null)   // timestamp when a regenerate was triggered
  const prevSessionRef = useRef(null)
  // Update-context (mode/reason) is STICKY across polls. The backend carries it on
  // every in-progress response, but we latch it here so a single field-less or
  // transitional payload can never reset the "updating" affordance mid-job. Cleared
  // only when the job reaches phase='ready' (or a fresh session/regenerate starts).
  const updateCtxRef = useRef(null)      // { mode, reason } for the in-flight update
  // Signature of the last-applied AT-REST report ("<generated_at>"). Once we've
  // settled on a ready report, subsequent polls return the SAME report but with
  // fresh JSON object/string references — re-running the setters would re-render the
  // whole panel (and re-parse every chat message) on every poll for no change. This lets
  // the ready branch bail out when the report is unchanged. Reset on session change.
  const readySigRef = useRef(null)
  // GROUND-TRUTH in-flight signal: the phase the LAST fetch saw from the SERVER
  // ('phase1'/'phase2' = generating). The poll loop keys continuation off THIS, not
  // only the client-side latches (_generatingMap / regeneratingRef) — otherwise a
  // mount-time fetch that lands mid-generation WITHOUT a latch set (e.g. the
  // intel-generation-started event was dropped because the hook's sessionId had just
  // changed on a freshly-launched VM) polls once, sees no latch, and stops — never
  // catching the eventual 'ready'. Then only a full page reload shows the report.
  const serverInFlightRef = useRef(false)
  // Timestamp of the LAST trigger (intel-generation-started / regenerate) for this
  // session. The POST /generate that the trigger fires is async and the GET that
  // reports is_generating=True only flips once the job is enqueued — but the FIRST
  // poll tick can race AHEAD of the POST landing server-side and see 'not_generated'
  // for a sub-second window. Without a grace window that first fetch clobbers the
  // optimistic latch (sets serverInFlightRef=false, deletes _generatingMap) and the
  // loop stops after ONE tick — the exact "RunAll generated it but the panel never
  // updated until a page reload" bug. During this window we treat 'not_generated' as
  // "job still spinning up" so the loop keeps polling until the backend reports the
  // real 'generating'/'ready'. Cleared on session change.
  const triggerAtRef = useRef(0)
  const TRIGGER_GRACE_MS = 15000  // ignore a transient not_generated for 15s post-trigger
  // ONE 'refresh-datasources' when a generation this poller watched reaches
  // 'ready' (entity docs → Data Sources sparkles). Replaces the per-trigger
  // watchers useNotebookFiles used to start; see services/intelRefreshEdge.js.
  const refreshEdgeRef = useRef(null)
  if (refreshEdgeRef.current === null) refreshEdgeRef.current = createIntelRefreshEdge()

  // On session change: reset and restore any in-flight generating state for the new session.
  useEffect(() => {
    if (prevSessionRef.current !== sessionId) {
      prevSessionRef.current = sessionId
      serverInFlightRef.current = false
      triggerAtRef.current = 0
      readySigRef.current = null
      refreshEdgeRef.current.reset()
      setIntel(null)
      setGeneratedAt(null)
      setPhase(null)
      updateCtxRef.current = null
      regeneratingRef.current = _generatingMap.get(sessionId) || null
      setStatus(regeneratingRef.current ? 'generating' : 'loading')
    }
  }, [sessionId])

  const fetchIntel = useCallback(async () => {
    if (!sessionId) { setStatus('not_generated'); return }
    try {
      const resp = await apiFetch(`/workbook-intel`, {
        headers: { 'X-Session-Id': sessionId },
      })
      if (!resp.ok) return
      const data = await resp.json()

      // ── Single source of truth: the backend's `phase` field ──────────────────
      // The backend emits phase on every branch:
      //   phase1  → job running, new/pruned cards NOT saved yet (status='generating')
      //   phase2  → cards saved with report_status='generating', prose still writing
      //             (status='ready' so cards render now)
      //   ready   → completed report at rest (report_status='ready')
      //   not_generated → no report (or stale meta w/ missing S3)
      // Older backends (pre-restart) won't send phase; derive a compatible fallback
      // from status/report_status so an un-restarted proxy still behaves sanely.
      let ph = data.phase
      if (!ph) {
        if (data.status === 'ready' && data.intel) {
          ph = (data.report_status || data.intel?.report_status || 'ready') === 'ready' ? 'ready' : 'phase2'
        } else if (data.status === 'generating') {
          ph = 'phase1'
        } else {
          ph = 'not_generated'
        }
      }

      // Record the SERVER's in-flight signal so the poll loop keeps polling while the
      // backend is still generating, even if no client latch was set (see
      // serverInFlightRef). phase1/phase2 = generating; ready/not_generated = settled.
      serverInFlightRef.current = (ph === 'phase1' || ph === 'phase2')
      if (serverInFlightRef.current) refreshEdgeRef.current.observe(ph)

      // Latch the in-flight update context (mode/reason) so it survives across polls
      // and can't be reset by a transitional payload. Refreshed on every in-progress
      // response, cleared when we reach 'ready'.
      if (ph === 'phase1' || ph === 'phase2') {
        const mode = data.mode || (data.has_existing ? 'update' : 'full')
        const reason = data.reason || updateCtxRef.current?.reason || 'addition'
        updateCtxRef.current = { mode, reason }
      }

      if (ph === 'ready') {
        // FINAL report at rest. Stale-report guard: during a regenerate, ignore a
        // completed report older than the trigger (leftover from before the refresh).
        if (regeneratingRef.current && data.generated_at) {
          const genTime = new Date(data.generated_at).getTime()
          if (genTime < regeneratingRef.current) {
            return  // stale FINAL report — keep whatever generating state we have
          }
        }
        // No-change bail: if we've already settled on this exact ready report and
        // there's no in-flight regenerate, do NOTHING. Re-running the setters with
        // fresh JSON references would re-render the panel on every poll and
        // re-parse every chat message — the "flicker every few seconds". Skipping
        // here keeps state referentially stable so React bails out of the re-render.
        const sig = `ready:${data.generated_at || ''}`
        if (refreshEdgeRef.current.observe('ready', { readySig: sig })) {
          try { window.dispatchEvent(new CustomEvent('refresh-datasources')) } catch {}
        }
        if (!regeneratingRef.current && readySigRef.current === sig) {
          return
        }
        readySigRef.current = sig
        if (data.intel) setIntel(data.intel)
        setGeneratedAt(data.generated_at)
        setPhase('ready')
        setStatus('ready')
        setReportStatus('ready')
        setIsUpdating(false)
        updateCtxRef.current = null
        regeneratingRef.current = null
        _generatingMap.delete(sessionId)
        return
      }

      if (ph === 'phase2') {
        // Cards (first-gen / merged / pruned) are live NOW; prose full_report still
        // regenerating. Render them, keep the updating affordance up, hold the
        // regenerate guard so the eventual FINAL report is still accepted.
        if (data.intel) setIntel(data.intel)
        if (data.generated_at) setGeneratedAt(data.generated_at)
        setPhase('phase2')
        setStatus('ready')            // back-compat: cards render on status==='ready'
        setReportStatus('generating')
        const ctx = updateCtxRef.current || {}
        setIsUpdating(ctx.mode === 'update')
        if (ctx.reason) setUpdateReason(ctx.reason)
        return
      }

      if (ph === 'phase1') {
        // Job running, no new cards yet. Show the pre-update report (if any) under the
        // spinner strip; a first generation has nothing yet.
        setPhase('phase1')
        setStatus('generating')
        setReportStatus('generating')
        const ctx = updateCtxRef.current || {}
        setIsUpdating(ctx.mode === 'update')
        if (ctx.reason) setUpdateReason(ctx.reason)
        if (data.intel) {
          setIntel(prev => prev || data.intel)
          if (data.generated_at) setGeneratedAt(prev => prev || data.generated_at)
        }
        return
      }

      // not_generated. TWO ways to get here, handled differently:
      //
      // (a) Within TRIGGER_GRACE_MS of a trigger we just fired → this is almost
      //     certainly the first poll RACING AHEAD of the async POST /generate landing
      //     server-side (is_generating flips only once the job is enqueued). Treat it
      //     as "job spinning up": KEEP serverInFlightRef true and the _generatingMap
      //     latch so the loop keeps polling until the backend reports the real
      //     'generating'. Clobbering here is exactly what stranded the panel until a
      //     page reload.
      // (b) No recent trigger (a genuinely empty notebook, or the job truly finished
      //     with no report) → settle: clear the latch so the loop stops instead of
      //     fast-polling forever on a notebook that will never reach 'ready'.
      const withinTriggerGrace = triggerAtRef.current
        && (Date.now() - triggerAtRef.current) < TRIGGER_GRACE_MS
      refreshEdgeRef.current.observe('not_generated', { withinGrace: !!withinTriggerGrace })
      if (withinTriggerGrace) {
        // Spin-up window: hold the in-flight signal so the poll loop continues.
        serverInFlightRef.current = true
        return
      }
      // don't clobber a locally-tracked regenerate we just kicked.
      if (!regeneratingRef.current) {
        setPhase('not_generated')
        setStatus('not_generated')
        // Release the generating latch: if a trigger set _generatingMap but the
        // backend reports no generation in flight, clear it so the poll loop settles
        // instead of fast-polling forever on a notebook that will never reach 'ready'.
        _generatingMap.delete(sessionId)
      }
    } catch {
      refreshEdgeRef.current.observe('error')
      setStatus('error')
    }
  }, [sessionId])

  // ── Event-driven polling (no perpetual heartbeat) ────────────────────────
  // Intel only changes via the 4 FRONTEND triggers (first_run_all, file_upload,
  // file_delete, manual) — there is NO server-initiated generation (the documented
  // `catalog_ready` trigger is unused; the VM never POSTs /generate). So instead of
  // polling forever, we:
  //   1. fetch ONCE on mount / session-change,
  //   2. poll (fast) ONLY while a generation is in flight, and
  //   3. STOP as soon as it settles (ready / error / not_generated).
  // Trigger sites dispatch `intel-generation-started` (and regenerate() does too) to
  // (re)enter the poll loop; a mount-time fetch that lands mid-generation also enters
  // it. At rest we do zero network work.
  const pollTimerRef = useRef(null)
  // Monotonic token identifying the CURRENT poll chain. Every startPolling() bumps
  // it; a running tick chain bound to an older token exits instead of rescheduling.
  // Without this, each `intel-generation-started` event (or re-entry) spawned a NEW
  // tick chain WITHOUT stopping the previous one, so chains accumulated and the
  // endpoint got hammered many times per second (a runaway poll). One live chain now.
  const pollRunIdRef = useRef(0)
  useEffect(() => {
    let cancelled = false

    const clearTimer = () => {
      if (pollTimerRef.current) { clearTimeout(pollTimerRef.current); pollTimerRef.current = null }
    }

    // One fetch, then decide whether to schedule another based on the fetched state.
    // `myRunId` binds this chain to the startPolling() that launched it; if a newer
    // startPolling() ran (bumping pollRunIdRef), this chain is stale and must stop.
    // `ticks` bounds the fast-poll so a generation that never reaches 'ready' (e.g. a
    // backend that stops emitting 'generating') can't spin forever — after the cap we
    // clear the generating latch and settle, rather than poll indefinitely.
    const MAX_POLL_TICKS = 150   // ~10 min at 4s — generous ceiling, then give up
    const tick = async (myRunId, ticks) => {
      if (cancelled || myRunId !== pollRunIdRef.current) return
      await fetchIntel()
      if (cancelled || myRunId !== pollRunIdRef.current) return
      // Re-read the authoritative "still generating?" signal. GROUND TRUTH is the
      // SERVER's phase from the fetch we just did (serverInFlightRef); the client
      // latches (_generatingMap from a trigger, regeneratingRef from a manual regen)
      // are additional reasons to keep polling (they cover the gap before the first
      // in-flight response lands). Keying on the server signal is what fixes "RunAll
      // generated the report but the live panel never updated until a page reload":
      // even if the trigger event was dropped, the mount fetch sees 'generating' and
      // the loop now keeps polling until 'ready'.
      const stillGenerating = serverInFlightRef.current
        || _generatingMap.has(sessionId) || !!regeneratingRef.current
      if (stillGenerating && ticks < MAX_POLL_TICKS) {
        pollTimerRef.current = setTimeout(() => tick(myRunId, ticks + 1), 4000)  // bounded fast poll while in flight
      } else {
        // Settled, superseded, or hit the ceiling — stop and release the latch so a
        // stuck 'generating' state can't keep any future chain spinning.
        if (ticks >= MAX_POLL_TICKS) { _generatingMap.delete(sessionId); regeneratingRef.current = null }
        clearTimer()
      }
    }

    // Enter the poll loop. Bumps the run token so any previously-running tick chain
    // (from a prior mount re-entry or a burst of trigger events) sees a stale token
    // and exits — guaranteeing exactly ONE live chain at a time (idempotent).
    const startPolling = () => {
      clearTimer()
      const myRunId = ++pollRunIdRef.current
      tick(myRunId, 0)
    }

    // Initial load for this session.
    startPolling()

    // A trigger fired (upload / delete / first-run-all / manual): (re)enter polling.
    const onGenStarted = (e) => {
      const sid = e?.detail?.sessionId
      if (sid && sid !== sessionId) return   // event for another session — ignore
      _generatingMap.set(sessionId, Date.now())
      // Optimistically assume in-flight so the FIRST tick keeps polling even if its
      // fetch races ahead of the backend registering the job (returns not_generated
      // for a sub-second window right after /generate was POSTed). The grace window
      // (triggerAtRef) makes that transient not_generated non-fatal to the loop; the
      // next fetch that sees the real phase corrects this.
      triggerAtRef.current = Date.now()
      serverInFlightRef.current = true
      refreshEdgeRef.current.arm()
      startPolling()
    }
    window.addEventListener('intel-generation-started', onGenStarted)

    return () => {
      cancelled = true
      pollRunIdRef.current++   // invalidate any in-flight tick chain so it can't reschedule
      clearTimer()
      window.removeEventListener('intel-generation-started', onGenStarted)
    }
    // fetchIntel is keyed on sessionId; re-run only when the session changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, fetchIntel])

  // Manual refresh — blows away current content and kicks a fresh generation.
  const regenerate = useCallback(async () => {
    if (!sessionId) return
    setIntel(null)
    setStatus('generating')
    setPhase('phase1')
    setReportStatus('generating')
    // A manual regenerate is a full rebuild, not a delta — no "updating" context.
    updateCtxRef.current = null
    setIsUpdating(false)
    regeneratingRef.current = Date.now()
    _generatingMap.set(sessionId, Date.now())
    triggerAtRef.current = Date.now()   // open the spin-up grace window (see fetchIntel)
    serverInFlightRef.current = true
    // Re-enter the poll loop (event-driven polling; no perpetual heartbeat).
    try { window.dispatchEvent(new CustomEvent('intel-generation-started', { detail: { sessionId } })) } catch {}
    try {
      await apiFetch(`/workbook-intel/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': sessionId },
        body: JSON.stringify({ trigger: 'manual' }),
      })
    } catch {
      setStatus('error')
    }
  }, [sessionId])

  // Derived collections + counts (consumer renders from these).
  const analyses = intel?.suggested_analyses || []
  const visualizations = intel?.visualizations || []
  const investigations = intel?.investigations || []
  const alerts = intel?.alerts || []
  const relationships = intel?.relationships || []
  const fullReport = intel?.full_report || ''
  const hasContent = analyses.length > 0 || visualizations.length > 0
    || investigations.length > 0 || alerts.length > 0

  return {
    intel,
    status,
    phase,
    reportStatus,
    isUpdating,
    updateReason,
    generatedAt,
    regenerate,
    // derived
    analyses,
    visualizations,
    investigations,
    alerts,
    relationships,
    fullReport,
    hasContent,
    counts: {
      analyses: analyses.length,
      visualizations: visualizations.length,
      investigations: investigations.length,
      alerts: alerts.length,
      suggestions: analyses.length + visualizations.length + investigations.length,
    },
  }
}

export default useWorkbookIntel
