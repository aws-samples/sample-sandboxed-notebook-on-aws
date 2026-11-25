import { useState, useRef, useLayoutEffect, useEffect, useReducer } from 'react'
import { railOpenReducer, railIsOpen, RAIL_CLOSED } from '../services/chatTurns'

/**
 * Turn rail: a thin column on the left of the chat's message list with one dot per
 * turn (the turn in view highlighted). Hover or keyboard focus opens it into a
 * floating list of turn titles; clicking a dot/title jumps to that turn.
 *
 * One set of real buttons serves as both dots and list rows (labels are visually
 * hidden while closed). Roving tabindex: ArrowUp/Down (wrap), Home/End move focus,
 * Enter/Space activate, Escape closes the floating list (focus stays on its dot).
 *
 * Closing (state machine in chatTurns.railOpenReducer):
 *  - hover-opened: the pointer left the rail column AND the list for HOVER_GRACE_MS
 *    (re-entering cancels the timer). While open only the 16px column and the list
 *    take pointer events, so the empty area around the list is "outside".
 *  - keyboard-opened (focus-visible / a navigation key; a mouse click focusing a dot
 *    does NOT open it): focus leaves the rail, or Escape.
 *  - a pointerdown outside (capture phase, never swallowed), choosing a title,
 *    the user scrolling the message list (wheel/touch; programmatic scrolls such
 *    as the streaming auto-scroll don't close it: the list floats over the wrap,
 *    not the scrolled content, so it stays positioned correctly), resizing the panel.
 *
 * @param items       [{ key, title, inContext? }] in conversation order. inContext === false
 *                    = older than the AI's context window: muted dot/row, an aria-label
 *                    suffix, and a separator before the first in-context item.
 * @param currentKey  key of the turn currently in view (owned by the panel)
 * @param onJump(key) scroll to (and expand) that turn
 * @param scrollRef   ref to the message list (scrolling it by wheel/touch closes the list)
 */
const HOVER_GRACE_MS = 200

export default function ChatTurnRail({ items, currentKey, onJump, scrollRef }) {
  const [railState, dispatch] = useReducer(railOpenReducer, RAIL_CLOSED)
  const [focusWithin, setFocusWithin] = useState(false)
  const [focusIdx, setFocusIdx] = useState(null)   // roving index while focus is inside
  const navRef = useRef(null)
  const btnRefs = useRef([])
  const leaveTimerRef = useRef(0)
  const open = railIsOpen(railState)

  const currentIdx = Math.max(0, items.findIndex(i => i.key === currentKey))
  const tabIdx = focusWithin && focusIdx != null && focusIdx < items.length ? focusIdx : currentIdx

  const cancelLeave = () => { clearTimeout(leaveTimerRef.current); leaveTimerRef.current = 0 }
  useEffect(() => cancelLeave, [])

  // Keep the current dot visible inside the (scrollable) closed rail by adjusting
  // the nav's own scrollTop — never scrollIntoView, which would move the messages.
  useLayoutEffect(() => {
    const nav = navRef.current
    const btn = btnRefs.current[currentIdx]
    if (!nav || !btn || nav.scrollHeight <= nav.clientHeight) return
    const top = btn.offsetTop
    const bottom = top + btn.offsetHeight
    if (top < nav.scrollTop) nav.scrollTop = top
    else if (bottom > nav.scrollTop + nav.clientHeight) nav.scrollTop = bottom - nav.clientHeight
  }, [currentIdx, items.length])

  // While open: outside pointerdown, message-list scroll and panel resize close it.
  useEffect(() => {
    if (!open) return
    const nav = navRef.current
    const onDown = (e) => {
      if (nav && !nav.contains(e.target)) { cancelLeave(); dispatch('outside') }
    }
    const onUserScroll = () => { cancelLeave(); dispatch('scroll') }
    const list = scrollRef?.current
    const wrap = nav?.parentElement
    let lastW = wrap ? wrap.clientWidth : 0
    const ro = wrap && typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(() => {
        const w = wrap.clientWidth
        if (w !== lastW) { lastW = w; cancelLeave(); dispatch('resize') }
      })
      : null
    document.addEventListener('pointerdown', onDown, true)
    list?.addEventListener('wheel', onUserScroll, { passive: true })
    list?.addEventListener('touchmove', onUserScroll, { passive: true })
    ro?.observe(wrap)
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      list?.removeEventListener('wheel', onUserScroll)
      list?.removeEventListener('touchmove', onUserScroll)
      ro?.disconnect()
    }
  }, [open, scrollRef])

  const focusAt = (i) => {
    const n = items.length
    if (!n) return
    const idx = ((i % n) + n) % n
    setFocusIdx(idx)
    btnRefs.current[idx]?.focus()
  }

  const onKeyDown = (e) => {
    const i = focusIdx ?? currentIdx
    const nav = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }
    if (e.key in nav) { e.preventDefault(); dispatch('nav-key'); focusAt(nav[e.key]) }
    else if (e.key === 'Escape') {
      if (open) {
        // Closes the list; focus stays on the dot it was on (a navigation key re-opens).
        e.preventDefault(); e.stopPropagation(); cancelLeave(); dispatch('escape')
      }
    }
  }

  return (
    <nav
      ref={navRef}
      className={`ai-rail${open ? ' ai-rail-open' : ''}`}
      aria-label="Conversation turns"
      onPointerEnter={() => { cancelLeave(); dispatch('pointer-enter') }}
      onPointerLeave={() => {
        cancelLeave()
        leaveTimerRef.current = setTimeout(() => { leaveTimerRef.current = 0; dispatch('pointer-left') }, HOVER_GRACE_MS)
      }}
      onFocus={(e) => {
        setFocusWithin(true)
        // Keyboard focus opens the list; a mouse click that focuses a dot doesn't.
        let kb = false
        try { kb = e.target.matches(':focus-visible') } catch { kb = false }
        if (kb) dispatch('focus-visible')
      }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) {
          setFocusWithin(false); setFocusIdx(null); dispatch('focus-out')
        }
      }}
      onKeyDown={onKeyDown}
    >
      <ul className="ai-rail-list">
        {items.map((it, i) => {
          const isCurrent = it.key === currentKey
          const out = it.inContext === false
          // Decorative (the buttons' labels carry the meaning); keeps the keyed rows
          // in place when it moves.
          const sep = i > 0 && !out && items[i - 1].inContext === false
          return [
            sep && (
              <li key="ai-rail-sep" className="ai-rail-sep" aria-hidden="true">
                <span className="ai-rail-sep-text">Above: not in AI context</span>
              </li>
            ),
            <li key={it.key}>
              <button
                ref={(el) => { btnRefs.current[i] = el }}
                type="button"
                className={`ai-rail-item${isCurrent ? ' ai-rail-current' : ''}${out ? ' ai-rail-out' : ''}`}
                aria-label={`Go to turn ${i + 1}: ${it.title}${out ? ', not in AI context' : ''}`}
                aria-current={isCurrent ? 'true' : undefined}
                tabIndex={i === tabIdx ? 0 : -1}
                title={open ? it.title : undefined}
                onFocus={() => setFocusIdx(i)}
                onClick={() => { cancelLeave(); dispatch('select'); onJump(it.key) }}
              >
                <span className="ai-rail-dot" aria-hidden="true" />
                <span className="ai-rail-label" aria-hidden="true">{it.title}</span>
              </button>
            </li>,
          ]
        })}
      </ul>
    </nav>
  )
}
