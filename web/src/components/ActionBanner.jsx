// Shared temporary banner shown in a panel after a user action finished (see
// services/actionBanner.js for the rules). One main button (the whole message), a
// separate dismiss button that never triggers the action, and an optional
// secondary link button.
//
// Auto-dismiss: 10 s after it is first visible (mounted while the page is visible),
// paused while hovered or while keyboard focus is inside it; leaving restarts the
// full 10 s. On expiry it calls onDismiss (same handler as the x button). A new
// banner id restarts the countdown.
import { useCallback, useEffect, useRef } from 'react'
import {
  BANNER_AUTO_DISMISS_MS, countdownInit, countdownStep, countdownRemaining,
} from '../services/actionBanner'

const ICON_PROPS = {
  width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 2.2, strokeLinecap: 'round', strokeLinejoin: 'round',
}

function ToneIcon({ tone }) {
  if (tone === 'error') {
    return <svg {...ICON_PROPS}><circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 16.5v.01"/></svg>
  }
  if (tone === 'warning') {
    return <svg {...ICON_PROPS}><circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/></svg>
  }
  if (tone === 'info') {
    return <svg {...ICON_PROPS}><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5v.01"/></svg>
  }
  return <svg {...ICON_PROPS}><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.8"/></svg>
}

export default function ActionBanner({
  banner,
  onAction,
  onSecondary,
  onDismiss,
  dismissLabel = 'Dismiss',
  durationMs = BANNER_AUTO_DISMISS_MS,
  className = '',
}) {
  const rootRef = useRef(null)
  const stateRef = useRef(countdownInit(durationMs))
  const timerRef = useRef(null)
  const onDismissRef = useRef(onDismiss)
  onDismissRef.current = onDismiss

  const step = useCallback((event) => {
    const now = Date.now()
    const next = countdownStep(stateRef.current, event, now)
    stateRef.current = next
    clearTimeout(timerRef.current)
    timerRef.current = null
    if (next.expired) {
      onDismissRef.current?.()
      return
    }
    const remaining = countdownRemaining(next, now)
    if (remaining != null) timerRef.current = setTimeout(() => step('expire'), remaining)
  }, [])

  const bannerId = banner?.id
  useEffect(() => {
    if (!bannerId) return undefined
    stateRef.current = countdownInit(durationMs)
    const el = rootRef.current
    // Keep the pause state if the pointer or focus is already inside on (re)start.
    if (el?.matches(':hover')) stateRef.current = countdownStep(stateRef.current, 'hover-in')
    if (el && el.contains(document.activeElement)) stateRef.current = countdownStep(stateRef.current, 'focus-in')
    const onVisibility = () => step(document.visibilityState === 'visible' ? 'visible' : 'hidden')
    onVisibility()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [bannerId, durationMs, step])

  if (!banner) return null
  const { tone = 'info', lead, detail, action, secondary } = banner

  const onFocus = (e) => {
    if (e.relatedTarget && rootRef.current?.contains(e.relatedTarget)) return
    step('focus-in')
  }
  const onBlur = (e) => {
    if (e.relatedTarget && rootRef.current?.contains(e.relatedTarget)) return
    step('focus-out')
  }

  return (
    <div
      ref={rootRef}
      className={`action-banner action-banner-${tone}${className ? ` ${className}` : ''}`}
      role="status"
      data-banner-id={banner.id}
      onPointerEnter={() => step('hover-in')}
      onPointerLeave={() => step('hover-out')}
      onFocus={onFocus}
      onBlur={onBlur}
    >
      <div className="action-banner-row">
        <button type="button" className="action-banner-main" onClick={onAction}>
          <span className="action-banner-icon" aria-hidden="true"><ToneIcon tone={tone} /></span>
          <span className="action-banner-text">
            {lead && <span className="action-banner-lead">{lead}</span>}
            {detail && <>{' '}<span className="action-banner-detail">{detail}</span></>}
            {action && <>{' '}<span className="action-banner-action">{action}</span></>}
          </span>
        </button>
        <button
          type="button"
          className="action-banner-dismiss"
          aria-label={dismissLabel}
          title="Dismiss"
          onClick={onDismiss}
        >
          <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
      {secondary && (
        <button type="button" className="action-banner-secondary" onClick={onSecondary}>
          {secondary}
        </button>
      )}
    </div>
  )
}
