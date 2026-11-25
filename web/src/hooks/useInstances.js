import { useState, useEffect, useRef, useCallback } from 'react'
import { PROXY_URL } from '../config'
import { apiFetch } from '../services/apiFetch'
import { logError } from '../services/logger'
import { readApiError } from '../services/apiError'
import { saveFailureFromTerminate, terminateBody } from '../services/terminateSave'
import { findSessionVm } from '../constants'
import { instanceSessionMismatch, staleVmRepoint, detachedSessionVm } from '../services/vmOwnership'
import { appInstancesPollActive } from '../services/instancesPoll'

// How long a tab may sit at status:'launching' before the poll treats it as STUCK
// and reconciles it (attach to a live session VM, else disconnect). A genuine
// launch+restore is tens of seconds; this is comfortably beyond that so we never
// interrupt a real launch, only rescue a wedged one.
const LAUNCHING_STUCK_MS = 45000

/**
 * useInstances — owns MicroVM instance state and lifecycle. This is THE single
 * source of truth for VM state: it polls /instances, reconciles tab connection
 * status (auto-connect / rotation / termination), tracks per-VM metrics, and
 * exposes the lifecycle actions (suspend, terminate+save).
 *
 * Tab mutations are threaded in via setTabs/setModal so
 * this hook can keep tabs in sync without owning them.
 */
export function useInstances({ tabs, setTabs, setModal, pollIntervalMs }) {
  const [instances, setInstances] = useState({})
  const [vmMetrics, setVmMetrics] = useState({})  // microvm_id -> latest metrics
  // Durable, server-calculated cost payloads keyed by stable session_id. Unlike
  // instances, these remain available after a VM is TERMINATED because the new
  // /sessions/costs endpoint reads full DB session history.
  const [sessionCosts, setSessionCosts] = useState({})
  const [sessionCostTotal, setSessionCostTotal] = useState(null)
  // Live mirror of `instances` for use inside the stable refreshMetrics callback
  // (which must not depend on `instances` — that changes on every poll tick and would
  // recreate the callback, re-arming every metrics effect). Lets refreshMetrics
  // skip polling a microvmId that /instances no longer knows about (a terminated /
  // zombie VM), which is what caused a dead VM to be metrics-polled forever — each
  // poll a 5s-timeout 502 that saturates the single-worker proxy.
  const instancesRef = useRef({})
  instancesRef.current = instances
  // tabId -> epoch ms when it entered 'launching'. Lets the poll RESCUE a tab that
  // got stuck at 'launching' (e.g. its reopen/launch promise never settled). The
  // poll normally leaves 'launching' tabs alone (a launch legitimately takes tens
  // of seconds); after LAUNCHING_STUCK_MS we reconcile them so they can't hang the
  // "Launching..." pill forever.
  const launchingSinceRef = useRef({})

  // Session IDs are stable across VM rotation/termination. Keep the dependency as
  // a sorted primitive key so ordinary cell/output patches do NOT re-arm cost
  // requests; only opening/closing/rebinding a notebook session does.
  const sessionIdsKey = [...new Set((tabs || []).map(t => t?.sessionId).filter(Boolean))]
    .sort()
    .join('|')

  // New API only: POST /sessions/costs returns per-session totals AND an aggregate
  // already summed on the server. The client stores/displays those figures verbatim
  // and never adds individual VM/session costs itself.
  const fetchSessionCosts = useCallback(async () => {
    const sessionIds = sessionIdsKey ? sessionIdsKey.split('|') : []
    if (sessionIds.length === 0) {
      setSessionCosts({})
      setSessionCostTotal(null)
      return
    }
    try {
      const resp = await apiFetch(`/sessions/costs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: sessionIds }),
      })
      if (!resp.ok) return
      const data = await resp.json()
      setSessionCosts(data.session_costs || {})
      setSessionCostTotal(data.total || null)
    } catch (e) {
      logError('fetchSessionCosts', e)
    }
  }, [sessionIdsKey])

  // Refresh durable session costs on the same cadence as the VM poll. Unlike the
  // live instances map this keeps a terminated session's spend visible because the
  // endpoint reads vm_sessions/vm_state_log history from storage.
  useEffect(() => {
    fetchSessionCosts()
    const interval = setInterval(fetchSessionCosts, pollIntervalMs)
    return () => clearInterval(interval)
  }, [fetchSessionCosts, pollIntervalMs])

  // Fetch metrics for a specific VM (called after cell execution, not on a timer)
  const refreshMetrics = useCallback(async (microvmId) => {
    if (!microvmId) return
    // Don't poll metrics for a VM that /instances no longer knows about — it's
    // terminated / a zombie, and each poll is a wasted 5s-timeout round-trip to a
    // dead endpoint that backs up the single-worker proxy. Guard only when the
    // instances map is POPULATED (a known id has since disappeared); if it's empty
    // we haven't loaded yet, or a just-launched VM isn't in the poll snapshot yet,
    // so we allow the fetch (the proxy also self-heals via strike-based eviction).
    const inst = instancesRef.current
    if (inst && Object.keys(inst).length > 0 && !inst[microvmId]) return
    try {
      const resp = await apiFetch(`/instances/metrics?microvm_id=${microvmId}`)
      if (resp.ok) {
        const data = await resp.json()
        if (data.metrics) setVmMetrics(prev => ({ ...prev, ...data.metrics }))
      }
    } catch (e) { logError('refreshMetrics', e) }
  }, [])

  // Fetch instances periodically — this is THE SINGLE SOURCE OF TRUTH for VM state.
  // No copies (_vmState) are stored on tabs. Components derive state from `instances[tab.microvmId]`.
  const fetchInstances = useCallback(async () => {
    try {
      const resp = await apiFetch(`/instances`)
      if (resp.ok) {
        const data = await resp.json()
        const inst = data.instances || {}
        setInstances(inst)

        // Prune stale metric snapshots. vmMetrics is otherwise only ever merged
        // into (never cleaned), so a VM that died left its LAST CPU/Mem/Disk
        // reading frozen in state — and the panel renders the bar on
        // (vmMetrics[id] && state==='RUNNING'), so a ghost VM briefly still shown
        // as RUNNING kept a frozen-but-live-looking bar. Keep a snapshot only when
        // the VM is still listed AND actually RUNNING (and not flagged unhealthy);
        // drop it otherwise so the bar disappears deterministically the moment the
        // VM is gone, non-RUNNING, or striking out.
        setVmMetrics(prev => {
          let changed = false
          const next = {}
          for (const id of Object.keys(prev)) {
            const vm = inst[id]
            if (vm && vm.state === 'RUNNING' && !vm.unhealthy) next[id] = prev[id]
            else changed = true
          }
          return changed ? next : prev
        })

        // Only sync connection-related info on tabs (endpoint, status)
        // NOT vm state — that comes from `instances` directly
        setTabs(prev => {
          let changed = false
          const nowMs = Date.now()
          const updated = prev.map(tab => {
            // Track how long a tab has been 'launching' so we can rescue it if the
            // reopen/launch promise wedged. Record on entry, clear on exit.
            if (tab.status === 'launching') {
              if (!launchingSinceRef.current[tab.id]) launchingSinceRef.current[tab.id] = nowMs
            } else if (launchingSinceRef.current[tab.id]) {
              delete launchingSinceRef.current[tab.id]
            }
            const launchingStuck = tab.status === 'launching' &&
              (nowMs - (launchingSinceRef.current[tab.id] || nowMs)) > LAUNCHING_STUCK_MS

            // RESCUE a tab wedged at 'launching' with NO VM id (launch never
            // returned one). If a live VM now serves its session, adopt it; else
            // fall back to the manual restore panel (disconnected + sessionSaved).
            if (launchingStuck && !tab.microvmId) {
              const liveEntry = findSessionVm(inst, tab.sessionId)
              delete launchingSinceRef.current[tab.id]
              changed = true
              if (liveEntry) {
                return {
                  ...tab, microvmId: liveEntry[0], microvmEndpoint: `${PROXY_URL}/proxy`,
                  microvmMemory: liveEntry[1].memory_mib || tab.microvmMemory,
                  microvmArch: liveEntry[1].architecture || tab.microvmArch,
                  status: 'connected', mode: 'microvm',
                }
              }
              return { ...tab, status: 'disconnected', microvmEndpoint: null, sessionSaved: true }
            }

            // Disconnected tab without a VM id whose session has a live VM (e.g. a
            // reload before the restore result was saved): re-link to it.
            const ownVm = detachedSessionVm(inst, tab)
            if (ownVm) {
              changed = true
              return {
                ...tab, microvmId: ownVm[0], microvmEndpoint: `${PROXY_URL}/proxy`,
                microvmMemory: ownVm[1].memory_mib || tab.microvmMemory,
                microvmArch: ownVm[1].architecture || tab.microvmArch,
                sessionSaved: false, status: 'connected', mode: 'microvm',
              }
            }

            if (tab.microvmId && inst[tab.microvmId]) {
              // SESSION MISMATCH self-heal: the id is alive but belongs to ANOTHER
              // session (another notebook's VM, e.g. attached from a stale Connect
              // list or persisted in notebooks.microvm_id). The proxy routes this
              // tab by its own sessionId, so the id is stale: re-point to the
              // session's own live VM, else drop it and surface the restore panel.
              // Acts only when BOTH session ids are set and differ, so it can't flap.
              const stale = staleVmRepoint(inst, tab)
              if (stale) {
                if (stale.repoint) {
                  const [ownVmId, ownInfo] = stale.repoint
                  changed = true
                  return {
                    ...tab,
                    microvmId: ownVmId,
                    microvmEndpoint: `${PROXY_URL}/proxy`,
                    microvmMemory: ownInfo.memory_mib || tab.microvmMemory,
                    microvmArch: ownInfo.architecture || tab.microvmArch,
                    status: 'connected',
                    mode: 'microvm',
                  }
                }
                if (tab.status === 'launching' && !launchingStuck) return tab
                if (launchingStuck) delete launchingSinceRef.current[tab.id]
                changed = true
                return { ...tab, microvmId: null, status: 'disconnected', microvmEndpoint: null, sessionSaved: true }
              }
              const vmState = inst[tab.microvmId].state || 'UNKNOWN'
              const endpoint = inst[tab.microvmId].endpoint

              // Auto-connect: tab has a VM that is RUNNING/SUSPENDED but tab is not connected
              if ((tab.status === 'connecting' || tab.status === 'disconnected') && (vmState === 'RUNNING' || vmState === 'SUSPENDED') && endpoint) {
                changed = true
                return {
                  ...tab,
                  microvmEndpoint: `${PROXY_URL}/proxy`,
                  microvmMemory: inst[tab.microvmId].memory_mib || tab.microvmMemory,
                  microvmArch: inst[tab.microvmId].architecture || tab.microvmArch,
                  // Restore sessionId from the instance if the tab is missing it.
                  // Every VM request needs X-Session-Id; on a fresh start / reconnect
                  // the tab can have a microvmId but a lost sessionId, which makes
                  // /execute etc. 400 with "X-Session-Id header required". The proxy
                  // knows the VM's session, so adopt it. Never clobber an existing one.
                  sessionId: tab.sessionId || inst[tab.microvmId].session_id || tab.sessionId,
                  status: 'connected',
                  mode: 'microvm',
                }
              }

              // Backfill a missing sessionId on an ALREADY-connected tab. The
              // auto-connect branch above only runs for connecting/disconnected
              // tabs; a tab that reconnected (or was restored) as 'connected' but
              // WITHOUT a sessionId would otherwise stay broken — every VM request
              // 400s ("X-Session-Id header required"). The proxy knows this VM's
              // session, so adopt it. Only patches when genuinely missing so it
              // can't clobber a valid id or churn the tab needlessly.
              if (!tab.sessionId && inst[tab.microvmId].session_id) {
                changed = true
                return { ...tab, sessionId: inst[tab.microvmId].session_id }
              }
            } else if (tab.microvmId && !inst[tab.microvmId]) {
              // VM not in instances → might be terminated OR rotated to a new VM.
              // Don't interfere with a tab that's LEGITIMATELY launching a new VM
              // (a launch takes tens of seconds) — UNLESS it's been stuck past the
              // threshold, in which case fall through to reconcile (rotation branch
              // attaches to a live session VM; else the termination branch below
              // flips it to disconnected so the manual restore surfaces).
              if (tab.status === 'launching' && !launchingStuck) return tab
              if (launchingStuck) delete launchingSinceRef.current[tab.id]

              // The stored microvmId isn't in the map — either the session ROTATED
              // to a new VM, or the tab holds a STALE id (reopened notebook) while a
              // live VM for the same session sits under a different key. Self-heal by
              // re-pointing to the session's live VM. findSessionVm matches ONLY
              // alive states (RUNNING/SUSPENDED/…), so we never adopt a terminated VM
              // that happens to share the session_id. It's a pure read of the polled
              // map — no VM request — so re-pointing (and marking connected) does not
              // wake a suspended VM; the VM-hitting idle fetches remain suspend-gated.
              if (tab.sessionId) {
                const sessionVm = findSessionVm(inst, tab.sessionId)
                if (sessionVm) {
                  const [newVmId, newInfo] = sessionVm
                  changed = true
                  return {
                    ...tab,
                    microvmId: newVmId,
                    microvmEndpoint: `${PROXY_URL}/proxy`,
                    microvmMemory: newInfo.memory_mib || tab.microvmMemory,
                    microvmArch: newInfo.architecture || tab.microvmArch,
                    status: 'connected',
                    mode: 'microvm',
                  }
                }
              }

              // Not rotation — actual termination
              const updates = {
                status: 'disconnected',
                microvmEndpoint: null,
                sessionSaved: true,
              }
              if (tab.status !== 'disconnected' || !tab.sessionSaved) {
                changed = true
                return { ...tab, ...updates }
              }
            }
            return tab
          })
          return changed ? updated : prev
        })
      }
    } catch {
      // Proxy not available
    }
  }, [])

  // Helper: immediately update a single VM's state in the instances map
  // Used after successful cell execution on a suspended VM (don't wait for poll)
  const markVmRunning = useCallback((microvmId) => {
    setInstances(prev => {
      if (!prev[microvmId]) return prev
      if (prev[microvmId].state === 'RUNNING') return prev
      return { ...prev, [microvmId]: { ...prev[microvmId], state: 'RUNNING' } }
    })
  }, [])

  // Latest tabs, read inside the poll tick WITHOUT making the poll effect depend
  // on `tabs` (which changes on every edit and would tear down + re-arm the
  // interval, firing an extra /instances each time — the same churn class we hit
  // in the Sidebar badge poll).
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs

  useEffect(() => {
    // GATE the control-plane poll so an IDLE app (no notebooks open, nothing to
    // watch) is silent instead of hammering ListMicrovms on every poll — that
    // volume (×N tabs, under the account's rate limit) is what drove the
    // throttling / signature-expiry spiral. We poll ONLY when there is something
    // whose live state we actually need to track, and only while the tab is
    // visible:
    //   • "something to watch" = at least one tab bound to a VM or session
    //     (microvmId or sessionId). The MicroVMs/Connection panels do their OWN
    //     on-demand /instances fetch when opened, so the continuous poll doesn't
    //     need a panel signal — it exists to reconcile OPEN notebooks' VM state
    //     (auto-connect, rotation self-heal, termination → Restore panel).
    //   • "visible" = document not hidden; a backgrounded tab shouldn't poll.
    // A VM's state only changes meaningfully for a tab that's attached to it, so
    // when no tab references any VM/session there is nothing for the poll to do.
    const hasSomethingToWatch = () => appInstancesPollActive(tabsRef.current)

    const maybeFetch = () => {
      if (document.visibilityState === 'hidden') return
      if (!hasSomethingToWatch()) return
      fetchInstances()
    }

    // One fetch on mount regardless, so the initial VM list / "0 VMs" badge is
    // correct even before any notebook is opened; the recurring poll is gated.
    fetchInstances()
    const interval = setInterval(maybeFetch, pollIntervalMs)

    // When the tab becomes visible again, do an immediate catch-up fetch (its
    // state may have changed while hidden) — but only if there's something to
    // watch. Avoids a stale UI on tab refocus without polling while hidden.
    const onVisible = () => {
      if (document.visibilityState === 'visible') maybeFetch()
    }
    document.addEventListener('visibilitychange', onVisible)

    // Metrics are NOT polled continuously — that would keep VMs awake.
    // Instead, metrics are fetched on-demand after cell execution via refreshMetrics().

    return () => {
      clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [fetchInstances, pollIntervalMs])

  // Terminate & Save: terminates attached VM, detaches from notebook but preserves sessionId for restore
  // { withoutSaving: true } = the user chose "Terminate without saving" after a failed save.
  const terminateAndSave = useCallback(async (microvmId, { withoutSaving = false } = {}) => {
    try {
      const tab = tabs.find(t => t.microvmId === microvmId)
      // Refuse a mismatched pair: the tab claims a VM of ANOTHER session. Sending
      // it would (before the proxy's 409 guard) checkpoint that VM into this tab's
      // session and kill the other notebook's VM. No request is sent.
      if (tab && instanceSessionMismatch(instances[microvmId], tab)) {
        setModal({ type: 'vmSessionMismatch', microvmId, notebookName: tab.name })
        return
      }
      // Session from the VM first, consistent with suspendInstance.
      const sid = instances[microvmId]?.session_id || tab?.sessionId
      if (!sid) return
      const resp = await apiFetch(`/terminate`, {
        method: 'POST',
        headers: { 'X-Session-Id': sid, 'Content-Type': 'application/json' },
        body: JSON.stringify(terminateBody(microvmId, { withoutSaving })),
      })
      // A failed pre-terminate save: 409 = the proxy kept the VM running (offer
      // "Terminate without saving"); 200 + checkpoint.vmLost = the VM died while
      // saving (out of memory). Either way show the reason (services/terminateSave.js).
      const failure = saveFailureFromTerminate(resp.status, await resp.clone().json().catch(() => null))
      if (failure?.kind === 'kept') {
        setModal({ type: 'terminateSaveFailed', microvmId, message: failure.message })
        fetchInstances()
        return
      }
      // Only detach the notebook (and claim a clean saved shutdown) if the terminate
      // ACTUALLY succeeded. On failure the VM may still be running or the pre-terminate
      // checkpoint may have failed — detaching + setting sessionSaved would falsely
      // tell the UI it's safe to restore. Keep the tab attached and surface the error.
      if (!resp.ok) {
        let msg = `Terminate failed (${resp.status})`
        msg = await readApiError(resp, msg)
        logError('terminateAndSave', msg)
        window.dispatchEvent(new CustomEvent('vm-action-error', { detail: { action: 'terminateAndSave', microvmId, error: msg } }))
        fetchInstances()  // resync so the UI reflects the VM's real (still-alive) state
        return
      }
      // Detach from notebook tab but keep sessionId for restore
      setTabs(prev => prev.map(t => {
        if (t.microvmId !== microvmId) return t
        return {
          ...t,
          microvmId: null,
          microvmEndpoint: null,
          status: 'disconnected',
          mode: null,
          sessionSaved: true, // Signal that checkpoint was saved — enables "Restore Session"
          _terminatedVmId: microvmId, // never re-adopted by the poll (detachedSessionVm)
        }
      }))
      if (failure?.kind === 'lost') setModal({ type: 'terminateSaveLost', microvmId, message: failure.message })
      fetchInstances()
    } catch (e) {
      logError('terminateAndSave', e)
      window.dispatchEvent(new CustomEvent('vm-action-error', { detail: { action: 'terminateAndSave', microvmId, error: String(e?.message || e) } }))
      fetchInstances()
    }
  }, [fetchInstances, tabs, instances, setModal])

  // Suspend: asks the proxy (/suspend) to suspend the VM
  const suspendInstance = useCallback(async (microvmId) => {
    try {
      const sid = instances[microvmId]?.session_id || tabs.find(t => t.microvmId === microvmId)?.sessionId
      if (sid) await apiFetch(`/suspend`, { method: 'POST', headers: { 'X-Session-Id': sid } })
      // Immediately update instances state so UI reflects suspension without waiting for poll
      setInstances(prev => {
        if (!prev[microvmId]) return prev
        return { ...prev, [microvmId]: { ...prev[microvmId], state: 'SUSPENDED' } }
      })
    } catch {}
  }, [instances, tabs])

  return {
    instances,
    vmMetrics,
    sessionCosts,
    sessionCostTotal,
    refreshMetrics,
    markVmRunning,
    fetchInstances,
    fetchSessionCosts,
    terminateAndSave,
    suspendInstance,
  }
}
