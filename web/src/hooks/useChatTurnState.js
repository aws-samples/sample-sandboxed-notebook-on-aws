/**
 * useChatTurnState — per-notebook client state for the AI chat's turn view
 * (collapsed/expanded, inserted block → cell ids, Intel card title, ask time,
 * answered approval steps), persisted in localStorage:
 *
 *   microvm-chat-turns:<notebookId>  →  { v: 1, sessionId, turns: { <turnKey>: {...} } }
 *
 * Same pattern as useIntelCardState (keyed by notebook id, cleared on notebook
 * delete, TTL pruning via `seen`). Kept out of the `microvm-notebooks` tab blob on
 * purpose. The agent's stored messages are untouched; turns are derived from them
 * (services/chatTurns.groupTurns).
 *
 * Notebook switches: <AiChatPanel> stays mounted across tabs, so the state is
 * { nb, store } and is re-derived DURING render when the id changes
 * (syncTurnState) — no render ever pairs one notebook's store with another's id.
 * Writes are debounced and always use the id captured with the data; a pending
 * write is flushed before another notebook's store is read.
 */

import { useState, useEffect, useCallback } from 'react'
import {
  parseStore, emptyStore, setTurnEntry, collapseAll as collapseAllKeys,
  pruneStore, syncTurnState,
} from '../services/chatTurns'
import { logWarn } from '../services/logger'

const KEY_PREFIX = 'microvm-chat-turns:'
const WRITE_DEBOUNCE_MS = 300

const storageKey = (nb) => `${KEY_PREFIX}${nb}`

function readStore(nb) {
  if (nb == null) return emptyStore()
  let raw = null
  try { raw = localStorage.getItem(storageKey(nb)) } catch { return emptyStore() }
  if (!raw) return emptyStore()
  let parsed
  try { parsed = JSON.parse(raw) } catch {
    logWarn('chatTurns', 'corrupt chat-turn state ignored', { notebookId: nb })
    return emptyStore()
  }
  return parseStore(parsed)   // wrong version / shape → empty
}

function writeStore(nb, store) {
  if (nb == null) return
  try {
    localStorage.setItem(storageKey(nb), JSON.stringify(store))
  } catch (e) {
    // Quota / disabled storage: state stays in memory for this session.
    logWarn('chatTurns', 'could not persist chat-turn state', { notebookId: nb, error: e?.message })
  }
}

/** Delete a notebook's persisted turn state (notebook delete path). */
export function clearChatTurnState(notebookId) {
  if (notebookId == null) return
  try { localStorage.removeItem(storageKey(notebookId)) } catch { /* ignore */ }
}

// Pending debounced write, shared module-wide so a flush can run from render.
let _pending = null   // { nb, store, timer }
function flushPending() {
  const p = _pending
  if (!p) return
  _pending = null
  clearTimeout(p.timer)
  writeStore(p.nb, p.store)
}

/**
 * @param notebookId  the active notebook (tab) id
 * @param sessionId   the tab's chat session id (null before the first VM)
 * @returns { store, setTurn(key, patch, opts?), collapseAll(keys), touch(liveKeys), clear() }
 */
export function useChatTurnState(notebookId, sessionId) {
  const nb = notebookId != null ? String(notebookId) : null
  const sid = sessionId || null
  const [state, setState] = useState(() => syncTurnState(null, nb, sid, readStore))

  // Adjust state on prop change, during render (React's documented pattern).
  let cur = state
  if (state.nb !== nb) flushPending()   // the re-read below must see the latest write
  const synced = syncTurnState(state, nb, sid, readStore)
  if (synced !== state) {
    cur = synced
    setState(synced)
  }

  // Debounced write of a locally-changed store, under the id captured with it.
  useEffect(() => {
    if (!state.dirty || state.nb == null) return
    if (_pending && _pending.nb !== state.nb) flushPending()
    if (_pending) clearTimeout(_pending.timer)
    const entry = { nb: state.nb, store: state.store, timer: 0 }
    entry.timer = setTimeout(() => {
      if (_pending === entry) _pending = null
      writeStore(entry.nb, entry.store)
    }, WRITE_DEBOUNCE_MS)
    _pending = entry
  }, [state])

  // Flush on unmount (panel closed) so nothing is lost.
  useEffect(() => () => flushPending(), [])

  // All mutators are bound to the notebook they were created for: an update that
  // lands after a switch is dropped instead of being applied to the new store.
  const update = useCallback((fn) => {
    setState(prev => {
      if (prev.nb !== nb) return prev
      const store = fn(prev.store)
      return store === prev.store ? prev : { nb: prev.nb, store, dirty: true }
    })
  }, [nb])

  const setTurn = useCallback((key, patch, opts) => {
    update(store => setTurnEntry(store, key, patch, opts))
  }, [update])

  const collapseAll = useCallback((keys) => {
    update(store => collapseAllKeys(store, keys))
  }, [update])

  const touch = useCallback((liveKeys) => {
    update(store => pruneStore(store, liveKeys))
  }, [update])

  const clear = useCallback(() => {
    if (_pending && _pending.nb === nb) { clearTimeout(_pending.timer); _pending = null }
    clearChatTurnState(nb)
    setState({ nb, store: emptyStore(sid), dirty: false })
  }, [nb, sid])

  return { store: cur.store, setTurn, collapseAll, touch, clear }
}

export default useChatTurnState
