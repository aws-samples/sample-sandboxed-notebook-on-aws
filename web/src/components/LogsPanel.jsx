/**
 * LogsPanel — Bottom panel that streams CloudWatch logs from the MicroVM in real-time.
 * 
 * Uses Server-Sent Events (SSE) to tail logs from GET /logs/stream.
 * Shows lifecycle events, code executions, errors — everything happening inside the VM.
 */

import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { apiFetch } from '../services/apiFetch'
import { parseOriginTag } from '../services/logOriginTag'
import { IconX, IconEraser, IconLogs } from './Icons'
import { showDragOverlay, hideDragOverlay } from '../utils/dragOverlay'
import PanelState from './panels/PanelState'
import './LogsPanel.css'

const LOG_LEVEL_COLORS = {
  ERROR: 'log-error',
  WARN: 'log-warn',
  INFO: 'log-info',
  DEBUG: 'log-debug',
}

const MAX_LOG_LINES = 2000  // Cap to prevent memory issues

// Normalize a /logs/history event into a render row. History marks error-metric
// lines with kind==='metric' (parsed APP_METRIC); we render those as a distinct
// ERROR-level row (so error nav/filter catch them) with a compact message and an
// isMetric flag for special styling — mirroring the SSE 'metric' event shape.
// The VM /execute route stamps non-user runs with an "[agent]" (AI assistant
// probe) or "[internal]" (schema discovery / profiler) origin tag. parseOriginTag
// (services/logOriginTag.js) strips it; we surface it as a pill via log.origin.
const _ORIGIN_PILL_TITLE = {
  agent: 'AI assistant (agent) execution, run in a scratch',
  internal: 'System probe (schema discovery / profiler), run in a throwaway scratch',
}

// Split a log message into its leading "timestamp [LEVEL] " prefix and the rest,
// so the agent pill can render AFTER the level (…[INFO] [agent] ▶ …). Matches the
// VM log format "2026-09-04 22:31:24,932 [INFO] " (date + time with comma/dot
// millis, then a bracketed level). Returns { prefix, rest }; prefix is '' when the
// line doesn't match, so callers fall back to rendering the whole message with the
// pill at the start.
const _TS_LEVEL_RE = /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d+\s+\[[A-Z]+\]\s+)/
function splitLevelPrefix(msg) {
  if (typeof msg !== 'string') return { prefix: '', rest: msg }
  const m = msg.match(_TS_LEVEL_RE)
  if (!m) return { prefix: '', rest: msg }
  return { prefix: m[1], rest: msg.slice(m[1].length) }
}

function mapHistoryEvent(e, id) {
  if (e.kind === 'metric') {
    return {
      ...e,
      id,
      isMetric: true,
      level: 'ERROR',
      message: `⚠ error metric: ${e.error_type || 'Error'}${e.handler ? ` in ${e.handler}` : ''}`,
    }
  }
  const { origin, message } = parseOriginTag(e.message)
  return { ...e, id, origin, message }
}

export default function LogsPanel({ activeTab, onClose }) {
  const [logs, setLogs] = useState([])
  // Filter levels: ALL_NO_DEBUG (default — everything except the high-frequency
  // DEBUG poll noise), ALL (truly everything incl. DEBUG), or a single level.
  const [filter, setFilter] = useState('ALL_NO_DEBUG')
  const [search, setSearch] = useState('')     // text grep across messages
  const [autoScroll, setAutoScroll] = useState(true)
  const [connected, setConnected] = useState(false)
  const [wrap, setWrap] = useState(false)       // line-wrap toggle for long lines
  // Time-travel ("Load earlier logs"): the oldest timestamp (ms) currently loaded,
  // whether older history likely exists behind it, and an in-flight guard so rapid
  // clicks don't stack requests.
  const [oldestTs, setOldestTs] = useState(null)
  const [hasMoreEarlier, setHasMoreEarlier] = useState(false)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  // Pause/resume streaming: while paused, incoming SSE lines are held in a buffer
  // (ref, so buffering doesn't re-render) and flushed on resume. pausedCount mirrors
  // the buffer size for the header badge.
  const [paused, setPaused] = useState(false)
  const [pausedCount, setPausedCount] = useState(0)
  const pausedRef = useRef(false)
  const pauseBufferRef = useRef([])
  // "New logs below" pill: how many lines arrived while the user was scrolled up
  // (auto-scroll off). Cleared when they jump back to live.
  const [newBelow, setNewBelow] = useState(0)
  // Error navigation cursor (index into the error line list). A ref: nothing renders from it.
  const errorCursorRef = useRef(-1)
  // Per-notebook error-metric summary (VM-side APP_METRIC lines aggregated by the
  // proxy's /logs/metrics-summary). Drives the header "N errors" chip breakdown.
  const [metricSummary, setMetricSummary] = useState(null)
  const logsEndRef = useRef(null)
  const containerRef = useRef(null)
  const [panelHeight, setPanelHeight] = useState(200)
  const resizing = useRef(false)
  // Keep a live ref of autoScroll so the SSE loop (a stable closure) can read the
  // current value without being torn down/recreated on every scroll.
  const autoScrollRef = useRef(true)
  useEffect(() => { autoScrollRef.current = autoScroll }, [autoScroll])

  const sessionId = activeTab?.sessionId

  // Connect to SSE stream
  useEffect(() => {
    // Reset all view state when the session changes (new VM = fresh log context).
    setPaused(false)
    pausedRef.current = false
    pauseBufferRef.current = []
    setPausedCount(0)
    setNewBelow(0)
    errorCursorRef.current = -1

    // Clear the PRIOR notebook's logs immediately on every session change. This
    // panel is kept MOUNTED (display:none) across notebook switches (App.jsx), so
    // unlike a fresh mount it does NOT start from an empty `logs` state — it starts
    // from the previous session's lines. The history fetch below only REPLACES logs
    // on a successful, present `data.events`; if the new session is suspended / has
    // no history yet (or the fetch returns null/!ok), that replace never runs and
    // the previous notebook's logs would linger — the exact "stale logs after tab
    // switch, fixed by close+reopen (= remount → empty state)" bug. Resetting here
    // makes the mounted panel re-scope exactly like a fresh mount. Also reset the
    // time-travel window so "Load earlier" re-anchors to the new session.
    setLogs([])
    setOldestTs(null)
    setHasMoreEarlier(false)

    if (!sessionId) {
      setConnected(false)
      return
    }

    let aborted = false
    const abortController = new AbortController()

    // Both /logs/history and /logs/stream 404 until the proxy can resolve this
    // session to a VM (the vm_sessions row / session→VM mapping lands a beat AFTER
    // a notebook launches, and a suspended VM may not be resolvable immediately).
    // A brand-new notebook therefore 404s on the first attempt; without a retry the
    // panel would connect once, fail, and sit on "No logs yet" forever even though
    // the VM is up and running cells. So we POLL both endpoints until they resolve
    // (or the effect is torn down). Once the stream connects, the server itself
    // handles the "VM exists but hasn't produced a log stream yet" case (it emits
    // {type:'waiting'} and keeps polling), so no retry is needed past that point.
    const RETRY_MS = 2000

    // Fetch history, retrying while the session isn't resolvable yet. Resolves once
    // we get a real (non-null) payload, or bails silently when aborted.
    const loadHistory = async () => {
      while (!aborted) {
        try {
          const r = await apiFetch(`/logs/history?limit=200`, {
            headers: { 'X-Session-Id': sessionId },
            signal: abortController.signal,
          })
          const data = r.ok ? await r.json() : null
          if (aborted) return
          if (data?.events) {
            setLogs(data.events.map((e, i) => mapHistoryEvent(e, `hist-${i}`)))
            if (data.oldest) setOldestTs(data.oldest)
            setHasMoreEarlier(!!data.has_more)
            return
          }
          // Not resolvable yet (404 / no events) — wait and retry.
        } catch (e) {
          if (e?.name === 'AbortError' || aborted) return
        }
        await new Promise(res => setTimeout(res, RETRY_MS))
      }
    }
    loadHistory()

    // Then connect SSE for real-time, retrying the CONNECT while the session isn't
    // resolvable yet (404). Once connected we read until the stream ends or the
    // effect is torn down; a clean end (done) also retries in case the VM rotated.
    const connectSSE = async () => {
      while (!aborted) {
        let resp
        try {
          resp = await apiFetch(`/logs/stream?since=${Date.now() - 5000}`, {
            headers: { 'X-Session-Id': sessionId },
            signal: abortController.signal,
          })
        } catch (err) {
          if (err.name === 'AbortError' || aborted) return
          setConnected(false)
          await new Promise(res => setTimeout(res, RETRY_MS))
          continue
        }

        if (aborted) return
        if (!resp.ok) {
          // Session not resolvable to a VM yet (404) — back off and retry the connect.
          setConnected(false)
          await new Promise(res => setTimeout(res, RETRY_MS))
          continue
        }

        setConnected(true)
        try {
          const reader = resp.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''

          while (true) {
            const { done, value } = await reader.read()
            if (done || aborted) break

            buffer += decoder.decode(value, { stream: true })
            const lines = buffer.split('\n')
            buffer = lines.pop() || ''  // Keep incomplete line in buffer

            for (const line of lines) {
              // This chunk was already read before the effect was torn down; bail so
              // a stale stream (the PRIOR notebook's) can't append its lines into the
              // new session's view after the switch. Each run owns its own `aborted`.
              if (aborted) break
              if (line.startsWith('data: ')) {
                try {
                  const event = JSON.parse(line.slice(6))
                  if (event.type === 'log' || event.type === 'metric') {
                    // A 'metric' event is an APP_METRIC line the proxy parsed out;
                    // render it as a distinct ERROR-level line (so error nav/filter
                    // pick it up) with a compact synthesized message + a style flag.
                    const entry = event.type === 'metric'
                      ? {
                          id: `metric-${Date.now()}-${Math.random()}`,
                          type: 'log',
                          isMetric: true,
                          level: 'ERROR',
                          timestamp: event.timestamp,
                          time: event.time,
                          vm_id: event.vm_id,
                          error_type: event.error_type,
                          handler: event.handler,
                          message: `⚠ error metric: ${event.error_type || 'Error'}${event.handler ? ` in ${event.handler}` : ''}`,
                        }
                      : (() => {
                          const parsed = parseOriginTag(event.message)
                          return { ...event, message: parsed.message, origin: parsed.origin, id: `sse-${Date.now()}-${Math.random()}` }
                        })()
                    // Paused: hold the line in the buffer (no render) + bump the badge.
                    if (pausedRef.current) {
                      pauseBufferRef.current.push(entry)
                      setPausedCount(pauseBufferRef.current.length)
                      continue
                    }
                    setLogs(prev => {
                      const next = [...prev, entry]
                      return next.length > MAX_LOG_LINES ? next.slice(-MAX_LOG_LINES) : next
                    })
                    // If the user is scrolled up (auto-scroll off), count new arrivals
                    // so the "N new" jump-to-live pill can surface.
                    if (!autoScrollRef.current) setNewBelow(n => n + 1)
                  }
                } catch {}
              }
            }
          }
        } catch (err) {
          if (err.name === 'AbortError' || aborted) return
          // Stream dropped mid-flight (network blip, VM swap) — fall through to the
          // outer retry so we reconnect rather than silently going dark.
        }
        // The stream ended cleanly (server closed it, e.g. duration cap) or errored.
        // If we're still mounted on this session, back off and reconnect so a long-
        // lived panel keeps tailing across VM rotations.
        setConnected(false)
        if (aborted) return
        await new Promise(res => setTimeout(res, RETRY_MS))
      }
    }

    connectSSE()

    return () => {
      aborted = true
      abortController.abort()
      setConnected(false)
    }
  }, [sessionId])

  // Poll the per-notebook error-metric summary (VM-side APP_METRIC aggregation).
  // Refreshes on session change + every 15s so the header "N errors" chip reflects
  // errors across ALL of this session's VMs. Cleared when there's no session.
  useEffect(() => {
    if (!sessionId) { setMetricSummary(null); return }
    let cancelled = false
    const fetchSummary = async () => {
      try {
        const resp = await apiFetch(`/logs/metrics-summary?minutes=120`, {
          headers: { 'X-Session-Id': sessionId },
        })
        if (resp.ok && !cancelled) setMetricSummary(await resp.json())
      } catch { /* transient — keep the last summary */ }
    }
    fetchSummary()
    const iv = setInterval(fetchSummary, 15000)
    return () => { cancelled = true; clearInterval(iv) }
  }, [sessionId])

  // Auto-scroll to bottom
  useEffect(() => {
    if (autoScroll && logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'auto' })
    }
  }, [logs, autoScroll])

  // Detect manual scroll (disable auto-scroll when user scrolls up)
  const handleScroll = useCallback(() => {
    if (!containerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 40
    setAutoScroll(isAtBottom)
    // Back at the bottom → we're "live" again, so clear the new-logs pill counter.
    if (isAtBottom) setNewBelow(0)
  }, [])

  // Jump back to live: scroll to bottom, re-enable auto-scroll, clear the pill.
  const jumpToLive = useCallback(() => {
    setAutoScroll(true)
    setNewBelow(0)
    requestAnimationFrame(() => {
      logsEndRef.current?.scrollIntoView({ behavior: 'auto' })
    })
  }, [])

  // Pause/resume the live stream. On resume, flush everything buffered while paused
  // into the log list in one batch (respecting the max-lines cap).
  const togglePause = useCallback(() => {
    setPaused(p => {
      const next = !p
      pausedRef.current = next
      if (!next && pauseBufferRef.current.length) {
        const buffered = pauseBufferRef.current
        pauseBufferRef.current = []
        setPausedCount(0)
        setLogs(prev => {
          const merged = [...prev, ...buffered]
          return merged.length > MAX_LOG_LINES ? merged.slice(-MAX_LOG_LINES) : merged
        })
      }
      return next
    })
  }, [])

  // Resize handle
  const handleResizeStart = useCallback((e) => {
    e.preventDefault()
    resizing.current = true
    const startY = e.clientY
    const startHeight = panelHeight
    // Overlay prevents an embedded Plotly iframe from stealing the drag's mouse events.
    showDragOverlay('row-resize')

    const handleMove = (moveEvent) => {
      if (!resizing.current) return
      const delta = startY - moveEvent.clientY
      setPanelHeight(Math.max(100, Math.min(window.innerHeight * 0.7, startHeight + delta)))
    }

    const handleUp = () => {
      resizing.current = false
      document.removeEventListener('mousemove', handleMove)
      document.removeEventListener('mouseup', handleUp)
      hideDragOverlay()
    }

    document.addEventListener('mousemove', handleMove)
    document.addEventListener('mouseup', handleUp)
  }, [panelHeight])

  const clearLogs = () => {
    setLogs([])
    // Clearing resets the time-travel window too — nothing is "loaded" anymore, so
    // there's no anchor to page back from until the stream repopulates.
    setOldestTs(null)
    setHasMoreEarlier(false)
  }

  // "Load earlier logs" — page backward in time. Fetch the window ending just
  // before the oldest line we currently show, then PREPEND the returned (older)
  // events. Scroll position is preserved so the view doesn't jump: we measure the
  // scroll height before and after and re-anchor by the delta, keeping the line the
  // user was reading in place while older content grows above it.
  const loadEarlier = useCallback(async () => {
    if (!sessionId || !oldestTs || loadingEarlier) return
    setLoadingEarlier(true)
    const container = containerRef.current
    const prevScrollHeight = container ? container.scrollHeight : 0
    const prevScrollTop = container ? container.scrollTop : 0
    try {
      const resp = await apiFetch(`/logs/history?limit=200&until=${oldestTs - 1}`, {
        headers: { 'X-Session-Id': sessionId },
      })
      const data = resp.ok ? await resp.json() : null
      const older = data?.events || []
      if (older.length > 0) {
        const mapped = older.map((e, i) => mapHistoryEvent(e, `earlier-${oldestTs}-${i}`))
        setLogs(prev => [...mapped, ...prev])
        if (data.oldest) setOldestTs(data.oldest)
        setHasMoreEarlier(!!data.has_more)
        // Re-anchor scroll after the prepend so the viewport stays put.
        requestAnimationFrame(() => {
          const c = containerRef.current
          if (c) c.scrollTop = prevScrollTop + (c.scrollHeight - prevScrollHeight)
        })
      } else {
        // Nothing older came back — we've reached the beginning of available history.
        setHasMoreEarlier(false)
      }
    } catch {
      /* transient — leave the button so the user can retry */
    } finally {
      setLoadingEarlier(false)
    }
  }, [sessionId, oldestTs, loadingEarlier])

  // When the session has spanned more than one VM (rotation/recovery), show a short
  // per-line VM tag so it's clear which VM a line came from. Single-VM sessions
  // (the common case) show no tag to avoid clutter. Uses last 4 chars of the id.
  const distinctVmIds = useMemo(() => {
    const s = new Set()
    for (const l of logs) if (l.vm_id) s.add(l.vm_id)
    return s
  }, [logs])
  const showVmTag = distinctVmIds.size > 1
  const vmTag = useCallback((vmId) => (vmId ? String(vmId).slice(-4) : ''), [])

  // Level + text-search filter. Search is a case-insensitive substring over the
  // message. Both compose (level AND search).
  const searchLc = search.trim().toLowerCase()
  const filteredLogs = useMemo(() => {
    const _levelOk = (l) => {
      // ALL_NO_DEBUG (default): everything except the DEBUG poll/read noise.
      // ALL: truly everything, including DEBUG.
      // Otherwise: exactly the chosen level.
      if (filter === 'ALL_NO_DEBUG') return l.level !== 'DEBUG'
      if (filter === 'ALL') return true
      return l.level === filter
    }
    return logs.filter(l =>
      _levelOk(l) &&
      (!searchLc || (l.message || '').toLowerCase().includes(searchLc))
    )
  }, [logs, filter, searchLc])

  // Error count (over ALL logs, not the filtered view) + the ids of error rows in
  // the CURRENT filtered/rendered order, so prev/next jumps land on visible lines.
  const errorCount = useMemo(() => logs.reduce((n, l) => n + (l.level === 'ERROR' ? 1 : 0), 0), [logs])
  const errorIdsInView = useMemo(
    () => filteredLogs.filter(l => l.level === 'ERROR').map(l => l.id),
    [filteredLogs]
  )

  // Jump to the next/previous error line in the rendered list: scroll it into view
  // and briefly flash it. Cursor wraps around the error list.
  const jumpError = useCallback((dir) => {
    if (errorIdsInView.length === 0) return
    let next = errorCursorRef.current + (dir === 'next' ? 1 : -1)
    if (next >= errorIdsInView.length) next = 0
    if (next < 0) next = errorIdsInView.length - 1
    errorCursorRef.current = next
    const id = errorIdsInView[next]
    // Landing on an error means we're reading, not tailing — stop auto-scroll.
    setAutoScroll(false)
    requestAnimationFrame(() => {
      const el = containerRef.current?.querySelector(`[data-log-id="${CSS.escape(id)}"]`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        el.classList.remove('log-line-flash')
        void el.offsetWidth
        el.classList.add('log-line-flash')
        setTimeout(() => el.classList.remove('log-line-flash'), 1200)
      }
    })
  }, [errorIdsInView])

  // Serialize the currently-visible lines (respect level + search) for copy/export.
  const visibleText = useCallback(
    () => filteredLogs.map(l => l.message).join('\n'),
    [filteredLogs]
  )

  const copyVisible = useCallback(async () => {
    try { await navigator.clipboard.writeText(visibleText()) } catch { /* clipboard unavailable */ }
  }, [visibleText])

  const downloadVisible = useCallback(() => {
    const blob = new Blob([visibleText()], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
    a.href = url
    a.download = `microvm-logs-${stamp}.log`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }, [visibleText])

  // Split a message into text + <mark> segments highlighting search matches.
  const renderMessage = useCallback((msg) => {
    if (!searchLc) return msg
    const lc = msg.toLowerCase()
    const parts = []
    let i = 0
    while (i < msg.length) {
      const hit = lc.indexOf(searchLc, i)
      if (hit === -1) { parts.push(msg.slice(i)); break }
      if (hit > i) parts.push(msg.slice(i, hit))
      parts.push(<mark key={hit} className="log-hl">{msg.slice(hit, hit + searchLc.length)}</mark>)
      i = hit + searchLc.length
    }
    return parts
  }, [searchLc])

  return (
    <div className="logs-bottom-panel" style={{ height: panelHeight }}>
      <div className="logs-resize-handle" onMouseDown={handleResizeStart} />
      <div className="logs-panel-header">
        <span className="logs-panel-title">Logs</span>
        <span className={`logs-status ${connected ? 'logs-status-connected' : 'logs-status-disconnected'}`} />

        <div className="logs-search-wrap">
          <input
            className="logs-search-input"
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            onKeyDown={e => { if (e.key === 'Escape' && search) { e.preventDefault(); setSearch('') } }}
            placeholder="Search logs…"
            spellCheck={false}
          />
          {search && (
            <button
              className="logs-search-clear"
              onClick={() => setSearch('')}
              title="Clear search (Esc)"
              aria-label="Clear search"
            >
              <IconX width={11} height={11} />
            </button>
          )}
        </div>

        <select
          className="logs-filter-select"
          value={filter}
          onChange={e => setFilter(e.target.value)}
        >
          <option value="ALL_NO_DEBUG">All except Debug</option>
          <option value="ALL">All</option>
          <option value="INFO">Info</option>
          <option value="WARN">Warn</option>
          <option value="ERROR">Error</option>
          <option value="DEBUG">Debug</option>
        </select>

        {/* Per-notebook error-metric chip: aggregated VM-side APP_METRIC counts
            (across all this session's VMs), independent of what's scrolled into
            view. Tooltip breaks down by error_type. */}
        {metricSummary && metricSummary.total > 0 && (
          <span
            className="logs-metric-chip"
            title={
              'Notebook error metrics (last ' + (metricSummary.window_minutes || 0) + 'm):\n' +
              Object.entries(metricSummary.by_error_type || {})
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => `  ${k} ×${v}`)
                .join('\n')
            }
          >
            ⚠ {metricSummary.total} error metric{metricSummary.total > 1 ? 's' : ''}
          </span>
        )}

        {/* Error navigation: badge + prev/next, shown only when errors exist. */}
        {errorCount > 0 && (
          <span className="logs-error-nav" title={`${errorCount} error${errorCount > 1 ? 's' : ''}`}>
            <span className="logs-error-badge">{errorCount}</span>
            <button className="logs-panel-btn" onClick={() => jumpError('prev')} disabled={errorIdsInView.length === 0} title="Previous error">⌃</button>
            <button className="logs-panel-btn" onClick={() => jumpError('next')} disabled={errorIdsInView.length === 0} title="Next error">⌄</button>
          </span>
        )}

        {/* Pause/resume the live stream. While paused, the badge shows buffered lines. */}
        <button
          className={`logs-panel-btn ${paused ? 'active' : ''}`}
          onClick={togglePause}
          title={paused ? `Resume streaming${pausedCount ? ` (${pausedCount} buffered)` : ''}` : 'Pause streaming'}
        >
          {paused ? `▶${pausedCount ? ` ${pausedCount}` : ''}` : '❚❚'}
        </button>

        <label className="logs-autoscroll">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={e => { setAutoScroll(e.target.checked); if (e.target.checked) setNewBelow(0) }}
          />
          Auto-scroll
        </label>

        <button
          className={`logs-panel-btn ${wrap ? 'active' : ''}`}
          onClick={() => setWrap(w => !w)}
          title={wrap ? 'Disable line wrap' : 'Wrap long lines'}
        >⏎</button>

        <span className="logs-count">{filteredLogs.length} lines</span>

        <button className="logs-panel-btn" onClick={copyVisible} disabled={filteredLogs.length === 0} title="Copy visible logs">
          {/* Clipboard icon — same as the cell "Copy output" button. */}
          <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        </button>
        <button className="logs-panel-btn" onClick={downloadVisible} disabled={filteredLogs.length === 0} title="Download visible logs (.log)">
          {/* Download/export icon — same as the notebook toolbar + Intel export. */}
          <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </button>
        <button className="logs-panel-btn" onClick={clearLogs} title="Clear logs">
          <IconEraser width={12} height={12} />
        </button>
        <button className="logs-panel-btn" onClick={onClose} title="Close logs panel">
          <IconX width={12} height={12} />
        </button>
      </div>

      <div className="logs-container" ref={containerRef} onScroll={handleScroll}>
        {/* Time-travel: load older history above the current view. */}
        {sessionId && logs.length > 0 && hasMoreEarlier && (
          <button
            className="logs-load-earlier"
            onClick={loadEarlier}
            disabled={loadingEarlier}
            title="Fetch older log lines from CloudWatch"
          >
            {loadingEarlier ? 'Loading earlier logs…' : '↑ Load earlier logs'}
          </button>
        )}
        {!sessionId && (
          <PanelState
            variant="empty"
            icon={<IconLogs width={24} height={24} />}
            message="No active session"
            hint="Launch a notebook to stream its MicroVM logs here."
          />
        )}
        {sessionId && logs.length === 0 && (
          <PanelState
            variant="empty"
            icon={<IconLogs width={24} height={24} />}
            message="No logs yet"
            hint="Logs stream here as the notebook's MicroVM runs."
          />
        )}
        {sessionId && logs.length > 0 && filteredLogs.length === 0 && (
          <PanelState
            variant="empty"
            icon={<IconLogs width={24} height={24} />}
            message={searchLc
              ? `No logs match “${search.trim()}”`
              : (filter === 'ALL' || filter === 'ALL_NO_DEBUG')
                ? 'No logs yet'
                : `No ${filter.toLowerCase()} logs`}
            hint={searchLc ? 'Clear the search or widen the level filter.' : 'Try the “All” filter to see every log line.'}
          />
        )}
        {filteredLogs.map(log => (
          <div
            key={log.id}
            data-log-id={log.id}
            className={`log-line ${LOG_LEVEL_COLORS[log.level] || 'log-info'} ${wrap ? 'log-wrap' : ''} ${log.isMetric ? 'log-metric' : ''}`}
          >
            {showVmTag && log.vm_id && (
              <span className="log-vm-tag" title={log.vm_id}>{vmTag(log.vm_id)}</span>
            )}
            {log.origin ? (() => {
              // Place the origin (agent / internal) pill AFTER the "timestamp [LEVEL]" prefix:
              //   "2026-09-04 22:31:24,932 [INFO] " [agent] "▶ Executing code…"
              // If the line doesn't match that prefix (prefix === ''), the pill
              // sits at the very start, which is the sensible fallback.
              const { prefix, rest } = splitLevelPrefix(log.message)
              return (
                <span className="log-msg">
                  {prefix && renderMessage(prefix)}
                  <span className={`log-agent-pill log-origin-${log.origin}`} title={_ORIGIN_PILL_TITLE[log.origin]}>{log.origin}</span>
                  {' '}
                  {renderMessage(rest)}
                </span>
              )
            })() : (
              <span className="log-msg">{renderMessage(log.message)}</span>
            )}
          </div>
        ))}
        <div ref={logsEndRef} />
      </div>

      {/* "New logs below" pill — shown when scrolled up and lines arrived. */}
      {newBelow > 0 && !autoScroll && (
        <button className="logs-new-pill" onClick={jumpToLive} title="Jump to the latest logs">
          ↓ {newBelow} new log{newBelow > 1 ? 's' : ''}
        </button>
      )}
    </div>
  )
}
