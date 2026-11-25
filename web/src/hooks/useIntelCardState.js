/**
 * useIntelCardState — client-side per-card state for Workbook Intel cards
 * (ACTIONED = clicked/viewed, DELETED = hidden), persisted in localStorage and
 * keyed by NOTEBOOK id.
 *
 * Why client-side + localStorage:
 *   Intel is regenerated on Run All / file upload / file delete, and the whole
 *   report is one blob in S3 (no per-card rows, no stable server id). So "which
 *   cards has the user clicked / hidden" is ephemeral, per-notebook UI state — not
 *   something to round-trip to the backend. We keep a small map in localStorage:
 *
 *     microvm-intel-cardstate:<notebookId>  →  { "<cardKey>": { actioned, deleted } }
 *
 * Card identity (cardKey): there is NO id field on a card, so we derive a stable
 * content key matching the backend's dedup scheme (proxy/notebook/ai/intel/delta.py
 * _dedup_key): analyses/visualizations/investigations key on `title`; alerts key on
 * `type + message`. Same content → same key, so a click/hide survives a regeneration
 * that reproduces the SAME card, and a reworded regeneration is (correctly) treated
 * as a new card that reappears.
 *
 * Keyed by NOTEBOOK id (not session): notebook↔session is 1:1 here, but the notebook
 * id is stable across VM rotation/reopen and is exactly what the notebook-delete path
 * holds — so ticks/hides persist for the notebook's life and cleanup on delete is a
 * single removeItem (see clearIntelCardState).
 *
 * Hygiene: each entry records `seen` (last time its card was present); entries whose
 * card has been absent for PRUNE_TTL_MS are pruned, so a long-lived notebook's blob
 * can't grow unbounded from cards that were regenerated away. Pruning is by AGE, not
 * by a single absence — see the prune effect for why.
 */

import { useState, useEffect, useCallback, useMemo, useRef } from 'react'

const KEY_PREFIX = 'microvm-intel-cardstate:'
// Card-state entries whose card hasn't been present for this long are pruned.
const PRUNE_TTL_MS = 30 * 24 * 60 * 60 * 1000
// Don't rewrite `seen` more often than this for a card that's still present.
const SEEN_REFRESH_MS = 60 * 60 * 1000

function storageKey(notebookId) {
  return `${KEY_PREFIX}${notebookId}`
}

// Stable per-card content key. MUST mirror the backend dedup scheme so a card keeps
// its identity across regenerations. `section` disambiguates the (rare) case of the
// same title living in two different sections.
export function intelCardKey(section, item) {
  const norm = (s) => String(s ?? '').trim().toLowerCase()
  if (section === 'alerts') {
    return `alerts|${norm(item.type)}|${norm(item.message)}`
  }
  // analyses | visualizations | investigations → keyed on title
  return `${section}|${norm(item.title)}`
}

function readBlob(notebookId) {
  if (notebookId == null) return {}
  try {
    const raw = localStorage.getItem(storageKey(notebookId))
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

// Bare localStorage write — NO event dispatch (that's done separately by callers,
// AFTER the React state update, to avoid re-entrant setState during an updater).
function writeBlob(notebookId, blob) {
  if (notebookId == null) return
  try {
    localStorage.setItem(storageKey(notebookId), JSON.stringify(blob))
  } catch { /* quota / disabled storage — non-fatal, state is best-effort */ }
}

// Monotonic token identifying the LAST writer, so an instance can ignore the sync
// event it itself dispatched (it already has the fresh state).
let _lastWriteToken = 0

// Broadcast that a notebook's card-state changed so OTHER live instances of the hook
// (in-panel accordion ↔ closed-panel FAB derivation in App) re-read and stay in sync.
// Returns the token the caller should record so it can skip its own echo.
function broadcastChange(notebookId) {
  const token = ++_lastWriteToken
  try {
    window.dispatchEvent(new CustomEvent('intel-cardstate-changed', {
      detail: { notebookId: String(notebookId), token },
    }))
  } catch { /* ignore */ }
  return token
}

/**
 * Delete ALL persisted intel card-state for a notebook. Call this when a notebook is
 * permanently deleted so its blob doesn't linger. Static (no hook) so the delete path
 * can call it directly.
 */
export function clearIntelCardState(notebookId) {
  if (notebookId == null) return
  try {
    localStorage.removeItem(storageKey(notebookId))
    broadcastChange(notebookId)  // notify live instances (FAB / accordion) to re-read
  } catch { /* ignore */ }
}

/**
 * @param notebookId  the notebook whose card-state we manage (stable across sessions)
 * @param liveKeys    the set of cardKeys present in the CURRENT intel (for pruning)
 * @returns { getState, isDeleted, isActioned, markActioned, toggleDeleted,
 *            deleteKeys, undeleteKeys, deletedCountFor }
 */
export function useIntelCardState(notebookId, liveKeys) {
  const [blob, setBlob] = useState(() => readBlob(notebookId))

  // True when the CURRENT blob value arrived from an incoming sync event (a re-read),
  // so the broadcast effect below does NOT re-broadcast it (which would ping-pong
  // forever between the accordion instance and the FAB instance).
  const fromEventRef = useRef(false)
  // The broadcast token this instance last emitted, so it ignores its own echo.
  const ownTokenRef = useRef(0)
  // Skip the broadcast effect's first run (mount) — the initial blob isn't a local edit.
  const mountedRef = useRef(false)

  // Reload the blob when the notebook changes (switching tabs).
  const prevNbRef = useRef(notebookId)
  useEffect(() => {
    if (prevNbRef.current !== notebookId) {
      prevNbRef.current = notebookId
      fromEventRef.current = true      // a notebook-switch read isn't a local edit
      setBlob(readBlob(notebookId))
    }
  }, [notebookId])

  // Cross-instance sync — receive: when ANOTHER instance writes this notebook's blob
  // (accordion delete/action ↔ FAB derivation in App), re-read so we reflect it.
  // We SKIP our own echo via the token, and mark the resulting blob as event-sourced
  // so our broadcast effect doesn't re-emit it.
  useEffect(() => {
    if (notebookId == null) return
    const onChanged = (e) => {
      const d = e?.detail || {}
      if (String(d.notebookId) !== String(notebookId)) return
      if (d.token && d.token === ownTokenRef.current) return  // our own echo — ignore
      fromEventRef.current = true
      setBlob(readBlob(notebookId))
    }
    window.addEventListener('intel-cardstate-changed', onChanged)
    return () => window.removeEventListener('intel-cardstate-changed', onChanged)
  }, [notebookId])

  // Cross-instance sync — broadcast: AFTER a LOCAL edit commits (blob changed and it
  // wasn't an event-sourced read), tell the other instances. Done in a post-commit
  // effect — NOT inside a setBlob updater — so it can't cause a re-entrant setState
  // (that re-entrancy was what silently swallowed per-card deletes). localStorage was
  // already written synchronously by the mutator; here we only notify.
  useEffect(() => {
    if (!mountedRef.current) { mountedRef.current = true; return }  // skip mount
    if (fromEventRef.current) { fromEventRef.current = false; return }  // don't echo a received change
    if (notebookId == null) return
    ownTokenRef.current = broadcastChange(notebookId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blob, notebookId])

  // PRUNE orphan keys — by AGE, never by a single absence. Each entry carries `seen`
  // (ms epoch of the last time its card was present). Keys present now get `seen`
  // refreshed; keys absent for longer than PRUNE_TTL_MS are dropped. Keeps the blob
  // bounded without ever deleting a tick just because ONE render lacked the card.
  //
  // Why not "drop anything not in liveKeys" (the old behaviour, which lost ticks):
  //   • Instances see different card subsets — App's FAB instance passes only
  //     alerts + investigations, so it wiped every analyses/visualizations tick.
  //   • On reload / tab switch there is a render where `notebookId` is the NEW
  //     notebook but the intel cards are still the PREVIOUS notebook's (useWorkbookIntel
  //     clears them in an effect). Pruning then wiped this notebook's ticks — alerts on
  //     shared sample files survived (same key in both notebooks), investigations didn't.
  const liveKeySig = useMemo(
    () => (liveKeys && liveKeys.size ? Array.from(liveKeys).sort().join('\u0001') : ''),
    [liveKeys],
  )
  useEffect(() => {
    if (notebookId == null || !liveKeys || liveKeys.size === 0) return
    const now = Date.now()
    setBlob(prev => {
      const next = {}
      let changed = false
      for (const [k, v] of Object.entries(prev)) {
        const seen = typeof v?.seen === 'number' ? v.seen : null
        if (liveKeys.has(k)) {
          // Refresh at most hourly to avoid a write on every intel poll.
          if (seen == null || now - seen > SEEN_REFRESH_MS) { next[k] = { ...v, seen: now }; changed = true }
          else next[k] = v
        } else if (seen == null) {
          next[k] = { ...v, seen: now }   // legacy entry: start its clock, don't drop
          changed = true
        } else if (now - seen > PRUNE_TTL_MS) {
          changed = true                  // unseen for the TTL → drop
        } else {
          next[k] = v
        }
      }
      if (!changed) return prev
      writeBlob(notebookId, next)
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notebookId, liveKeySig])

  const persist = useCallback((next) => {
    writeBlob(notebookId, next)
    setBlob(next)
  }, [notebookId])

  const getState = useCallback((cardKey) => blob[cardKey] || null, [blob])
  const isDeleted = useCallback((cardKey) => !!blob[cardKey]?.deleted, [blob])
  const isActioned = useCallback((cardKey) => !!blob[cardKey]?.actioned, [blob])

  const markActioned = useCallback((cardKey) => {
    setBlob(prev => {
      if (prev[cardKey]?.actioned) return prev  // already actioned — no churn
      const next = { ...prev, [cardKey]: { ...prev[cardKey], actioned: true, seen: Date.now() } }
      writeBlob(notebookId, next)
      return next
    })
  }, [notebookId])

  const toggleDeleted = useCallback((cardKey) => {
    setBlob(prev => {
      const cur = prev[cardKey] || {}
      const next = { ...prev, [cardKey]: { ...cur, deleted: !cur.deleted, seen: Date.now() } }
      writeBlob(notebookId, next)
      return next
    })
  }, [notebookId])

  // Bulk delete/undelete (section trash, clear-all, "show hidden" restore).
  const deleteKeys = useCallback((cardKeys) => {
    if (!cardKeys || cardKeys.length === 0) return
    setBlob(prev => {
      const next = { ...prev }
      const now = Date.now()
      for (const k of cardKeys) next[k] = { ...next[k], deleted: true, seen: now }
      writeBlob(notebookId, next)
      return next
    })
  }, [notebookId])

  const undeleteKeys = useCallback((cardKeys) => {
    if (!cardKeys || cardKeys.length === 0) return
    setBlob(prev => {
      const next = { ...prev }
      for (const k of cardKeys) if (next[k]) next[k] = { ...next[k], deleted: false }
      writeBlob(notebookId, next)
      return next
    })
  }, [notebookId])

  return {
    blob,
    getState,
    isDeleted,
    isActioned,
    markActioned,
    toggleDeleted,
    deleteKeys,
    undeleteKeys,
    persist,
  }
}

export default useIntelCardState
