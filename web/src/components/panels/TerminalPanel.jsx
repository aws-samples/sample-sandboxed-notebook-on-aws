import { useState, useEffect, useRef, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { SearchAddon } from '@xterm/addon-search'
import '@xterm/xterm/css/xterm.css'
import { IconX, IconRefresh, IconTerminal } from '../Icons'
import { PROXY_URL, PROXY_TOKEN } from '../../config'
import { apiFetch } from '../../services/apiFetch'
import { showDragOverlay, hideDragOverlay } from '../../utils/dragOverlay'
import PanelState from './PanelState'
import {
  SLOTS, TERMINAL_IDLE_MS, TERMINAL_FOCUS_EVENT, makeBanner, endsWithPrompt, terminalCommandFinished,
  commandFromPromptLine, terminalBannerText,
} from '../../services/actionBanner'
import { publish as publishBanner, clearOn as clearBannersOn } from '../../services/bannerStore'

const IDLE_TIMEOUT = 60000 // 60s — matches VM idle suspend, disconnect terminal WS to allow suspend

const DARK_THEME = {
  background: '#0a0a0f',
  foreground: '#e2e4f0',
  cursor: '#5b9fff',
  selectionBackground: 'rgba(91, 159, 255, 0.3)',
  black: '#1a1a2e',
  red: '#ff5c5c',
  green: '#34eaad',
  yellow: '#ffc14d',
  blue: '#5b9fff',
  magenta: '#b97aff',
  cyan: '#5cc2d4',
  white: '#e2e4f0',
  brightBlack: '#5c6280',
  brightRed: '#ff7a7a',
  brightGreen: '#5cffc4',
  brightYellow: '#ffd580',
  brightBlue: '#7db5ff',
  brightMagenta: '#d4a0ff',
  brightCyan: '#7dd8e6',
  brightWhite: '#ffffff',
}

// Apple-style light palette (design.md §8.5): every ANSI color ≥ 4.5:1 on white;
// ANSI "white" maps to readable grays because it assumes a dark background.
const LIGHT_THEME = {
  background: '#ffffff',
  foreground: '#1d1d1f',
  cursor: '#007aff',
  cursorAccent: '#ffffff',
  selectionBackground: 'rgba(0, 122, 255, 0.20)',
  black: '#1d1d1f',
  red: '#d70015',
  green: '#1e7a34',
  yellow: '#b25000',
  blue: '#0066cc',
  magenta: '#9b2393',
  cyan: '#00788a',
  white: '#6e6e73',
  brightBlack: '#6e6e73',
  brightRed: '#c41a16',
  brightGreen: '#1d7d36',
  brightYellow: '#c93400',
  brightBlue: '#0040dd',
  brightMagenta: '#8944ab',
  brightCyan: '#007d8f',
  brightWhite: '#3a3a3c',
}
// Apple-style dark palette (dark theme only; ember keeps DARK_THEME): background = the
// dark content surface (#1c1c1e), every ANSI color ≥ 4.5:1 on it; ANSI "black" maps to
// readable grays (mirror of the light palette's "white" mapping).
const APPLE_DARK_THEME = {
  background: '#1c1c1e',
  foreground: '#f5f5f7',
  cursor: '#0a84ff',
  cursorAccent: '#1c1c1e',
  selectionBackground: 'rgba(10, 132, 255, 0.35)',
  black: '#8e8e93',
  red: '#ff6961',
  green: '#30d158',
  yellow: '#ffd60a',
  blue: '#64aeff',
  magenta: '#da8fff',
  cyan: '#40c8e0',
  white: '#d1d1d6',
  brightBlack: '#98989d',
  brightRed: '#ff8078',
  brightGreen: '#5ee07e',
  brightYellow: '#ffe55c',
  brightBlue: '#8ac2ff',
  brightMagenta: '#e5a8ff',
  brightCyan: '#70d7ff',
  brightWhite: '#ffffff',
}
const xtermTheme = (theme) => (theme === 'light' ? LIGHT_THEME : theme === 'dark' ? APPLE_DARK_THEME : DARK_THEME)
const AI_PLACEHOLDERS = [
  "download iris dataset from GitHub",
  "install scikit-learn",
  "check disk space and memory usage",
  "clone a sample data repo",
  "list all CSV files recursively",
  "show system info (OS, Python version)",
  "compress all files in /tmp into a tar.gz",
]

export default function TerminalPanel({ activeTab, onClose, theme = 'dark', visible = true }) {
  const terminalRef = useRef(null)
  const xtermRef = useRef(null)
  const wsRef = useRef(null)
  const fitAddonRef = useRef(null)
  const searchAddonRef = useRef(null)
  const idleTimerRef = useRef(null)
  const idleDisconnectedRef = useRef(false) // True when WS was closed by idle timer
  const [status, setStatus] = useState('disconnected')

  // ---- "Command finished" banner (slot 'bottom') ---------------------------------
  // The shell stream has no exit status, so a command counts as finished when the
  // output after Enter goes quiet and ends on a prompt (terminalCommandFinished).
  // Published only while the terminal is hidden (another bottom tab is showing) or
  // not focused; removed when the terminal is visible and focused again.
  const visibleRef = useRef(visible)
  visibleRef.current = visible
  const cmdRef = useRef(null)          // { submittedAt, lastOutputAt, tail, command }
  const finishTimerRef = useRef(null)
  const sessionIdRef = useRef(activeTab?.sessionId)
  sessionIdRef.current = activeTab?.sessionId
  const isTermFocused = () => {
    const ta = xtermRef.current?.textarea
    return !!ta && document.hasFocus() && document.activeElement === ta
  }
  const clearIfSeen = () => {
    if (visibleRef.current && isTermFocused()) {
      clearBannersOn('terminal-visible-focused', { slot: SLOTS.BOTTOM })
    }
  }
  const resetCommand = () => {
    clearTimeout(finishTimerRef.current)
    finishTimerRef.current = null
    cmdRef.current = null
  }
  const checkFinished = () => {
    const c = cmdRef.current
    if (!c || !endsWithPrompt(c.tail)) return  // still running (or waiting for input)
    cmdRef.current = null
    const now = Math.max(Date.now(), (c.lastOutputAt || 0) + TERMINAL_IDLE_MS)
    if (!terminalCommandFinished({ ...c, now })) return
    if (visibleRef.current && isTermFocused()) return
    publishBanner(SLOTS.BOTTOM, makeBanner({
      ...terminalBannerText({ command: c.command, elapsedMs: c.lastOutputAt - c.submittedAt }),
      slot: SLOTS.BOTTOM,
      payload: { sessionId: sessionIdRef.current || null },
    }))
  }
  // Enter pressed: a new command starts (replaces any earlier banner).
  const noteSubmit = () => {
    let command = ''
    const term = xtermRef.current
    if (term) {
      try {
        const buf = term.buffer.active
        let y = buf.baseY + buf.cursorY
        let text = buf.getLine(y)?.translateToString(true) || ''
        // A long command wraps over several rows: join them back.
        while (y > 0 && buf.getLine(y)?.isWrapped) {
          y -= 1
          text = (buf.getLine(y)?.translateToString(true) || '') + text
        }
        command = commandFromPromptLine(text)
      } catch { /* buffer unavailable: banner without the command */ }
    }
    resetCommand()
    cmdRef.current = { submittedAt: Date.now(), lastOutputAt: null, tail: '', command }
    clearBannersOn('same-op-start', { slot: SLOTS.BOTTOM })
  }
  const noteOutput = (text) => {
    const c = cmdRef.current
    if (!c || !text) return
    c.lastOutputAt = Date.now()
    c.tail = (c.tail + text).slice(-512)
    clearTimeout(finishTimerRef.current)
    finishTimerRef.current = setTimeout(checkFinished, TERMINAL_IDLE_MS)
  }
  // The xterm and websocket handlers are created once; they call the latest version.
  const noteSubmitRef = useRef(noteSubmit)
  noteSubmitRef.current = noteSubmit
  const noteOutputRef = useRef(noteOutput)
  noteOutputRef.current = noteOutput
  const clearIfSeenRef = useRef(clearIfSeen)
  clearIfSeenRef.current = clearIfSeen

  // Idle timer: disconnects WebSocket after IDLE_TIMEOUT of no user input
  const resetIdleTimer = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    idleDisconnectedRef.current = false
    idleTimerRef.current = setTimeout(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        idleDisconnectedRef.current = true
        wsRef.current.close()
        wsRef.current = null
        setStatus('disconnected')
      }
    }, IDLE_TIMEOUT)
  }, [])

  const connect = useCallback(() => {
    if (!activeTab?.sessionId) {
      setStatus('error')
      return
    }
    if (wsRef.current && wsRef.current.readyState <= WebSocket.OPEN) {
      return
    }
    // Don't auto-reconnect if we were idle-disconnected (user must type to reconnect)
    if (idleDisconnectedRef.current) {
      return
    }

    setStatus('connecting')

    const wsProxyUrl = PROXY_URL.replace('http://', 'ws://').replace('https://', 'wss://')
    let wsUrl = `${wsProxyUrl}/ws/terminal?session_id=${encodeURIComponent(activeTab.sessionId)}`

    // C1 auth for the terminal relay. Preferred: carry the shared secret in a WS
    // subprotocol named `x-proxy-auth.<token>` (matches the server's subprotocol
    // gate). Browsers can't set WS headers, so we also append `&token=` as a
    // query fallback. Both are omitted when no token is configured so the UI
    // still connects to an unauthenticated proxy in local dev.
    let wsProtocols
    if (PROXY_TOKEN) {
      wsUrl += `&token=${encodeURIComponent(PROXY_TOKEN)}`
      wsProtocols = [`x-proxy-auth.${PROXY_TOKEN}`]
    }

    const ws = wsProtocols ? new WebSocket(wsUrl, wsProtocols) : new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      setStatus('connected')
      resetIdleTimer() // Start idle countdown immediately on connect
      // NOTE: the "Connected to MicroVM shell" banner is printed on session_init
      // (the VM's real shell-ready signal) — NOT here on socket open. The proxy
      // may still be waking a suspended VM at this point (it streams a "Waking…"
      // status), so announcing "Connected" on onopen was misleading.
    }

    ws.onmessage = (event) => {
      if (!xtermRef.current) return
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data)
          if (msg.type === 'session_init') {
            // The VM's shell is actually attached now — safe to show the banner.
            xtermRef.current.write('\x1b[2m  Connected to MicroVM shell (python3, pip, git, curl available)\x1b[0m\r\n')
            return
          }
          if (msg.type === 'session_error') {
            xtermRef.current.write(`\r\n\x1b[31m Shell error: ${msg.reason || msg.error}\x1b[0m\r\n`)
            return
          }
        } catch {}
        xtermRef.current.write(event.data)
        noteOutputRef.current(event.data)
      } else if (event.data instanceof Blob) {
        event.data.arrayBuffer().then(buf => {
          const bytes = new Uint8Array(buf)
          xtermRef.current?.write(bytes)
          noteOutputRef.current(new TextDecoder().decode(bytes))
        })
      }
    }

    ws.onerror = () => {
      idleDisconnectedRef.current = false // Allow retry on error
      setStatus('error')
    }
    ws.onclose = () => setStatus('disconnected')
  }, [activeTab?.sessionId, resetIdleTimer])

  // Initialize xterm.js (once)
  useEffect(() => {
    if (!terminalRef.current || xtermRef.current) return

    // The backing PTY on the VM (SHELL_INGRESS) runs at the default 80 columns
    // and we have no protocol to resize it. If the frontend fit its width to the
    // (wider) panel, bash would wrap at 80 while xterm wrapped at ~120 — the two
    // disagree and long/pasted lines render smeared. Pinning the terminal to a
    // fixed 80 cols makes both sides agree, so wrapping is clean. We still fit
    // ROWS to the panel height for scrollback; only the width is fixed.
    const TERM_COLS = 80
    const term = new Terminal({
      cursorBlink: true,
      cols: TERM_COLS,
      fontSize: 13,
      // xterm needs a literal font string (it can't read CSS vars); same SF Mono
      // stack as --font-mono in theme.css, used in every theme.
      fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      theme: xtermTheme(theme),
    })

    const fitAddon = new FitAddon()
    const webLinksAddon = new WebLinksAddon()
    const searchAddon = new SearchAddon()
    term.loadAddon(fitAddon)
    term.loadAddon(webLinksAddon)
    term.loadAddon(searchAddon)
    term.open(terminalRef.current)

    // Fit rows to the container height but keep width pinned at 80 cols. We let
    // FitAddon compute the natural size, then restore the fixed column count.
    const fitRowsOnly = () => {
      try {
        fitAddon.fit()
        if (term.rows && term.cols !== TERM_COLS) term.resize(TERM_COLS, term.rows)
      } catch {}
    }
    fitRowsOnly()

    xtermRef.current = term
    fitAddonRef.current = { fit: fitRowsOnly }  // all call sites fit rows-only
    searchAddonRef.current = searchAddon

    // Focusing the visible terminal removes its "command finished" banner.
    const onTermFocus = () => clearIfSeenRef.current()
    term.textarea?.addEventListener('focus', onTermFocus)

    term.onData((data) => {
      if (data.includes('\r')) noteSubmitRef.current()
      resetIdleTimer() // User typed — reset idle countdown
      idleDisconnectedRef.current = false // Allow reconnection
      // Reconnect if disconnected
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        connect()
        const waitAndSend = () => {
          if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(data)
          } else {
            setTimeout(waitAndSend, 100)
          }
        }
        setTimeout(waitAndSend, 300)
        return
      }
      wsRef.current.send(data)
    })

    const observer = new ResizeObserver(() => {
      fitRowsOnly()
    })
    observer.observe(terminalRef.current)

    if (activeTab?.sessionId) {
      setTimeout(() => connect(), 100)
    }

    return () => {
      observer.disconnect()
      term.textarea?.removeEventListener('focus', onTermFocus)
      clearTimeout(finishTimerRef.current)
      // The terminal is closed: its banner would point at nothing.
      clearBannersOn('dismiss', { slot: SLOTS.BOTTOM, match: b => b.kind === 'terminal' })
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      term.dispose()
      xtermRef.current = null
      fitAddonRef.current = null
    }
  }, [])

  // Reconnect when active session changes (switching notebooks)
  const prevSessionRef = useRef(activeTab?.sessionId)
  useEffect(() => {
    const newSession = activeTab?.sessionId
    if (newSession !== prevSessionRef.current) {
      prevSessionRef.current = newSession
      // A command of the previous session's shell can no longer finish here.
      clearTimeout(finishTimerRef.current)
      cmdRef.current = null
      idleDisconnectedRef.current = false // Reset idle flag for new session
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      if (xtermRef.current) {
        xtermRef.current.clear()
        xtermRef.current.write('\x1b[2J\x1b[H')
      }
      if (newSession && xtermRef.current) {
        setStatus('disconnected')
        // Delay to allow VM to resume from suspended state
        setTimeout(() => connect(), 3000)
      } else {
        setStatus('disconnected')
      }
    }
  }, [activeTab?.sessionId, connect])

  useEffect(() => {
    if (fitAddonRef.current) {
      setTimeout(() => { try { fitAddonRef.current?.fit() } catch {} }, 50)
    }
  })

  // Shown again (bottom tab switched back): drop the banner if it is also focused.
  useEffect(() => {
    if (visible) clearIfSeenRef.current()
  }, [visible])

  // The banner was clicked: App made this tab visible; focus the shell once the
  // display change has been applied (focus fires the clear above).
  useEffect(() => {
    const onFocusRequest = () => {
      setTimeout(() => {
        try { fitAddonRef.current?.fit() } catch {}
        xtermRef.current?.focus()
      }, 50)
    }
    window.addEventListener(TERMINAL_FOCUS_EVENT, onFocusRequest)
    return () => window.removeEventListener(TERMINAL_FOCUS_EVENT, onFocusRequest)
  }, [])

  // Update terminal theme when app theme changes
  useEffect(() => {
    if (xtermRef.current) {
      xtermRef.current.options.theme = xtermTheme(theme)
    }
  }, [theme])

  const [panelHeight, setPanelHeight] = useState(240)
  const isResizing = useRef(false)

  const handleResizeStart = useCallback((e) => {
    e.preventDefault()
    isResizing.current = true
    const startY = e.clientY
    const startHeight = panelHeight
    // Overlay prevents an embedded Plotly iframe from stealing the drag's mouse events.
    showDragOverlay('row-resize')

    const handleMouseMove = (e) => {
      const delta = startY - e.clientY
      const newHeight = Math.min(Math.max(startHeight + delta, 120), window.innerHeight * 0.7)
      setPanelHeight(newHeight)
    }

    const handleMouseUp = () => {
      isResizing.current = false
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      hideDragOverlay()
      if (fitAddonRef.current) {
        setTimeout(() => { try { fitAddonRef.current?.fit() } catch {} }, 50)
      }
    }

    document.body.style.cursor = 'row-resize'
    document.body.style.userSelect = 'none'
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
  }, [panelHeight])

  const [showAiInput, setShowAiInput] = useState(false)
  const [aiQuery, setAiQuery] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [aiResult, setAiResult] = useState('') // Generated command shown for review
  const [showSearch, setShowSearch] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const aiInputRef = useRef(null)
  const searchInputRef = useRef(null)

  const handleAiSuggest = async () => {
    if (!aiQuery.trim() || aiLoading) return
    setAiLoading(true)
    setAiResult('')
    resetIdleTimer() // Keep terminal alive while AI is working

    // Grab last 30 lines of terminal buffer for context
    let terminalHistory = ''
    if (xtermRef.current) {
      const buf = xtermRef.current.buffer.active
      const totalLines = buf.length
      const startLine = Math.max(0, totalLines - 30)
      const lines = []
      for (let i = startLine; i < totalLines; i++) {
        const line = buf.getLine(i)
        if (line) {
          const text = line.translateToString(true)
          if (text.trim()) lines.push(text)
        }
      }
      terminalHistory = lines.join('\n')
    }

    try {
      const resp = await apiFetch(`/terminal/suggest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          description: aiQuery.trim(),
          terminal_history: terminalHistory,
        }),
      })
      if (resp.ok) {
        const data = await resp.json()
        if (data.command) {
          setAiResult(data.command)
          setAiQuery('')
        }
      }
    } catch {}
    setAiLoading(false)
  }

  const handleAiExecute = () => {
    if (!aiResult || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    noteSubmit()
    if (cmdRef.current) cmdRef.current.command = aiResult.trim()
    wsRef.current.send(aiResult + '\r')
    setAiResult('')
    setShowAiInput(false)
    resetIdleTimer()
    idleDisconnectedRef.current = false
  }

  const handleReconnect = () => {
    idleDisconnectedRef.current = false // Allow reconnection
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    if (xtermRef.current) {
      xtermRef.current.clear()
    }
    setTimeout(() => connect(), 200)
  }

  return (
    <div className="terminal-bottom-panel" style={{ height: panelHeight }}>
      <div className="terminal-resize-handle" onMouseDown={handleResizeStart} />
      <div className="terminal-panel-header">
        <span className="terminal-panel-title">Terminal</span>
        <span className={`terminal-status terminal-status-${status}`} />
        <button
          className={`terminal-panel-btn ${showSearch ? 'terminal-panel-btn-active' : ''}`}
          onClick={() => {
            setShowSearch(v => !v)
            setShowAiInput(false)
            setTimeout(() => searchInputRef.current?.focus(), 50)
          }}
          title="Search terminal (Ctrl+F)"
        >
          <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
        </button>
        <button
          className={`terminal-panel-btn ${showAiInput ? 'terminal-panel-btn-active' : ''}`}
          onClick={() => {
            setShowAiInput(v => !v)
            setShowSearch(false)
            setTimeout(() => aiInputRef.current?.focus(), 50)
          }}
          title="AI: describe what you want"
        >
          <svg width={14} height={14} viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 001.423 1.423l1.183.394-1.183.394a2.25 2.25 0 00-1.423 1.423z"/>
          </svg>
        </button>
        <button className="terminal-panel-btn" onClick={handleReconnect} title="Reconnect">
          <IconRefresh width={13} height={13} />
        </button>
        <button className="terminal-panel-btn" onClick={onClose} title="Close terminal">
          <IconX width={12} height={12} />
        </button>
      </div>
      {showAiInput && (
        <div className="terminal-ai-bar">
          {!aiResult ? (
            <>
              <input
                ref={aiInputRef}
                className="terminal-ai-input"
                type="text"
                placeholder={`e.g. '${AI_PLACEHOLDERS[Math.floor(Date.now() / 10000) % AI_PLACEHOLDERS.length]}'`}
                value={aiQuery}
                onChange={e => setAiQuery(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') handleAiSuggest()
                  if (e.key === 'Escape') { setShowAiInput(false); setAiQuery('') }
                }}
                disabled={aiLoading}
              />
              <button
                className="terminal-ai-submit"
                onClick={handleAiSuggest}
                disabled={aiLoading || !aiQuery.trim()}
              >
                {aiLoading ? <span className="terminal-ai-spinner" /> : '→'}
              </button>
            </>
          ) : (
            <>
              <code className="terminal-ai-result">{aiResult}</code>
              <button className="terminal-ai-submit terminal-ai-run" onClick={handleAiExecute} title="Run command">▶</button>
              <button className="terminal-ai-submit terminal-ai-discard" onClick={() => setAiResult('')} title="Discard">✕</button>
            </>
          )}
        </div>
      )}
      {showSearch && (
        <div className="terminal-ai-bar">
          <input
            ref={searchInputRef}
            className="terminal-ai-input"
            type="text"
            placeholder="Search terminal output..."
            value={searchQuery}
            onChange={e => {
              setSearchQuery(e.target.value)
              if (searchAddonRef.current && e.target.value) {
                searchAddonRef.current.findNext(e.target.value)
              }
            }}
            onKeyDown={e => {
              if (e.key === 'Enter' && searchAddonRef.current) {
                if (e.shiftKey) {
                  searchAddonRef.current.findPrevious(searchQuery)
                } else {
                  searchAddonRef.current.findNext(searchQuery)
                }
              }
              if (e.key === 'Escape') { setShowSearch(false); setSearchQuery('') }
            }}
          />
          <button className="terminal-ai-submit" onClick={() => searchAddonRef.current?.findPrevious(searchQuery)} title="Previous">↑</button>
          <button className="terminal-ai-submit" onClick={() => searchAddonRef.current?.findNext(searchQuery)} title="Next">↓</button>
        </div>
      )}
      <div
        ref={terminalRef}
        className="terminal-container"
        style={{
          display: activeTab?.sessionId ? 'block' : 'none',
          background: xtermTheme(theme).background,
        }}
      />
      {!activeTab?.sessionId && (
        <PanelState
          variant="empty"
          icon={<IconTerminal width={24} height={24} />}
          message="No active session"
          hint="Launch or attach a MicroVM to use the terminal."
        />
      )}
    </div>
  )
}
