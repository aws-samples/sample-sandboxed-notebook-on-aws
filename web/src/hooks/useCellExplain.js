// "Explain this code" for one cell: request, in-session cache, Add to notes, Copy.
// Rendered by the focus-mode Explanation panel (components/ExplainPanel.jsx).
// Independent of useCellAI (Fix / note card state is never touched).
import { useState, useRef, useCallback } from 'react'
import { PROXY_URL, AI_EXPLAIN_TIMEOUT_MS } from '../config'
import { fetchWithTimeout } from '../services/fetchWithTimeout'
import {
  explainKey, normalizeResult, buildNotesBlock, appendToNotes, isAlreadyAdded,
  getExplain, setExplain,
} from '../services/explain'

const langOf = (cell) => (cell.type === 'sql' ? 'sql' : 'python')

export function useCellExplain({ cell, notebookId, getUpstreamSig, onSetAiExplanation }) {
  const nbId = String(notebookId ?? '')
  const cellId = String(cell.id)
  // `entry` = {key, result, code, at}; restored from the module store so the result
  // survives remounts (tab switches, virtualisation) while the page lives.
  const [state, setState] = useState(() => (getExplain(nbId, cellId) ? 'done' : 'idle'))
  const [entry, setEntry] = useState(() => getExplain(nbId, cellId) || null)
  const [cached, setCached] = useState(() => !!getExplain(nbId, cellId))
  const [justAdded, setJustAdded] = useState(false)
  const [copied, setCopied] = useState(false)
  const abortRef = useRef(null)
  const userCancelRef = useRef(false)

  const code = cell.code || ''
  const canExplain = code.trim().length > 0 && (cell.type === 'code' || cell.type === 'sql')
  const busy = state === 'loading'

  const currentKey = useCallback(() => explainKey({
    notebookId: nbId, cellId, code, upstreamSig: getUpstreamSig ? getUpstreamSig(cellId) : '',
  }), [nbId, cellId, code, getUpstreamSig])

  const run = useCallback(async ({ force = false } = {}) => {
    if (!canExplain || busy) return
    const key = currentKey()
    const hit = getExplain(nbId, cellId)
    if (!force && hit && hit.key === key) {
      setEntry(hit)
      setCached(true)
      setState('done')
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    userCancelRef.current = false
    setJustAdded(false)
    setState('loading')
    try {
      const resp = await fetchWithTimeout(`${PROXY_URL}/ai/explain-cell`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        timeout: AI_EXPLAIN_TIMEOUT_MS,
        body: JSON.stringify({ notebook_id: nbId, cell_id: cellId, code }),
      })
      if (resp.status === 429) {
        setState('throttled')
        return
      }
      if (!resp.ok) {
        setState('error')
        return
      }
      const data = await resp.json()
      const next = { key, result: normalizeResult(data, code), code, at: Date.now() }
      setExplain(nbId, cellId, next)
      setEntry(next)
      setCached(false)
      setState('done')
    } catch (err) {
      // A user cancel already reset the state; a timeout abort or network error is an error.
      if (!(err && err.name === 'AbortError' && userCancelRef.current)) setState('error')
    } finally {
      abortRef.current = null
    }
  }, [canExplain, busy, currentKey, nbId, cellId, code])

  const regenerate = useCallback(() => run({ force: true }), [run])

  const cancel = useCallback(() => {
    userCancelRef.current = true
    if (abortRef.current) abortRef.current.abort()
    setState(getExplain(nbId, cellId) ? 'done' : 'idle')
  }, [nbId, cellId])

  const block = entry ? buildNotesBlock(entry.result, { code: entry.code, lang: langOf(cell) }) : ''
  const added = !!entry && isAlreadyAdded(cell.aiExplanation, block)

  const addToNotes = useCallback(() => {
    if (!entry || added) return
    // Source 'user': Annotate / Clear annotations only remove AI-sourced notes.
    onSetAiExplanation?.(appendToNotes(cell.aiExplanation, block), false, 'user')
    setJustAdded(true)
  }, [entry, added, block, cell.aiExplanation, onSetAiExplanation])

  const copy = useCallback(async () => {
    if (!entry) return
    try {
      await navigator.clipboard.writeText(block)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard unavailable: leave the button unchanged
    }
  }, [entry, block])

  const viewNotes = useCallback(() => {
    // The saved note renders in the focus overlay, beside the code and outputs.
    document.querySelector('.cell-focus-panel .cell-ai-result-explain')
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [])

  const stale = !!entry && state === 'done' && entry.key !== currentKey()

  return {
    state, entry, result: entry?.result || null, snapshotCode: entry?.code || '', at: entry?.at || 0,
    cached, added, justAdded, copied, stale, canExplain, busy,
    run, regenerate, cancel, addToNotes, copy, viewNotes,
  }
}
