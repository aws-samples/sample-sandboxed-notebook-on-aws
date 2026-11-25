import { useRef, useState, useCallback, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'
import { useDraggable } from '../hooks/useDraggable'
import { IconPlay } from './Icons'

/**
 * AiFab — the floating "open AI Assistant" button shown when the AI panel is
 * closed. Draggable to anywhere on screen, with its position remembered across
 * sessions and notebooks. A real click still opens the panel; a click that was
 * actually the end of a drag is suppressed.
 *
 * While closed, the FAB also surfaces Workbook Intel so the user notices it
 * without opening the panel:
 *   • a count badge for ATTENTION items (Alerts + Investigate) — the two buckets
 *     the accordion already treats as attention-worthy. Analyses/Visualizations
 *     are excluded so the badge stays a triage signal, not a noise counter.
 *   • the badge is always the danger (red) color, mirroring the in-panel Alerts
 *     count (a red bucket identity, NOT colored by severity) so the closed FAB
 *     never disagrees with the open panel. Per-card severity is still shown inside
 *     the panel via each card's left-accent bar.
 *   • hidden entirely at 0.
 *   • while intel is still GENERATING with nothing to show yet, we show a small
 *     pulsing dot (not a number) so a mid-generation "0" doesn't read as
 *     "all clear".
 *   • hovering/focusing the FAB reveals a PREVIEW popover listing those attention
 *     items. Clicking a row opens the panel AND runs that item's prompt in chat —
 *     the exact same context-enriched prompt the in-panel Intel cards send.
 */

const ALERT_ICONS = { pii: '🔒', quality: '⚠️', duplicate: '📋', performance: '⚡' }
const MAX_PREVIEW_ROWS = 5

// Mirror IntelAccordion.buildPrompt so a preview click sends the SAME prompt the
// in-panel card would — the assistant can't tell where the click came from.
function buildPrompt(task, context) {
  return context && context.trim()
    ? `Context (from Workbook Intel):\n${context.trim()}\n\nTask: ${task}`
    : task
}

export default function AiFab({
  onOpen,
  alerts = [],
  investigations = [],
  intelPhase = null,
  intelHasContent = false,
}) {
  const { position, dragging, onPointerDown, didDragRef } = useDraggable('microvm.aiFab.pos')
  const btnRef = useRef(null)
  const closeTimerRef = useRef(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [anchorRect, setAnchorRect] = useState(null)

  const moved = !!position
  const style = moved
    ? { left: `${position.x}px`, top: `${position.y}px`, right: 'auto', bottom: 'auto' }
    : undefined

  // The BADGE counts ALERTS ONLY — aligning with the in-panel model, where Alerts
  // are their own red pill and Investigate is folded into "suggestions" (not an
  // alert). Counting investigations here made the closed-panel badge (8) disagree
  // with the open panel's "4 alerts", so the badge now mirrors the alert count.
  // OUTSTANDING only: an actioned card (green ✓ — carried on the item as _actioned)
  // is "handled", so it's excluded from these counts. This keeps the FAB badge, the
  // popover header pills, and the tooltip in lock-step with the in-panel Alerts /
  // Investigate headers, which also count only un-actioned cards. Acting on a card
  // therefore decrements the closed-panel badge too.
  const alertCount = alerts.reduce((n, a) => n + (a._actioned ? 0 : 1), 0)
  const investigateCount = investigations.reduce((n, iv) => n + (iv._actioned ? 0 : 1), 0)

  // Alerts are a RED-identity bucket, matching the in-panel Alerts count (which is
  // always red regardless of severity). The FAB is a mirror of the panel when it's
  // closed, so its alert badge/pill are always the danger color — severity is still
  // conveyed inside the panel via each card's left-accent bar. (We intentionally do
  // NOT recolor by highest severity here; that made the closed FAB disagree with the
  // open panel — e.g. a medium-only set showed orange on the FAB but red in the panel.)
  const severityClass = 'ai-fab-badge-high'

  // "Working" = a generation/update is in flight and there are no cards to count yet.
  const generating = (intelPhase === 'phase1' || intelPhase === 'phase2') && !intelHasContent

  const showBadge = alertCount > 0
  const badgeLabel = alertCount > 99 ? '99+' : String(alertCount)

  // Unified preview rows: alerts first (attention), then investigations. Each row
  // carries the exact task/context an in-panel card would send.
  const rows = [
    ...alerts.map((a) => ({
      kind: 'alert',
      severity: a.severity,
      icon: ALERT_ICONS[a.type] || '⚠️',
      title: a.message,
      actioned: !!a._actioned,
      prompt: buildPrompt(a.action || a.message, a.message),
    })),
    ...investigations.map((iv) => ({
      kind: 'investigate',
      icon: '🔍',
      title: iv.title,
      sub: iv.reason,
      actioned: !!iv._actioned,
      prompt: buildPrompt(iv.prompt, iv.reason),
    })),
  ]
  // Surface OUTSTANDING items first: an actioned (handled) row drops to the bottom of
  // the preview so the un-actioned attention items lead. Stable within each group
  // (alerts before investigations is preserved among equally-actioned rows).
  const orderedRows = [...rows.filter(r => !r.actioned), ...rows.filter(r => r.actioned)]
  const visibleRows = orderedRows.slice(0, MAX_PREVIEW_ROWS)
  const overflow = orderedRows.length - visibleRows.length

  const cancelClose = useCallback(() => {
    if (closeTimerRef.current) { clearTimeout(closeTimerRef.current); closeTimerRef.current = null }
  }, [])

  const openPreview = useCallback(() => {
    if (rows.length === 0) return
    cancelClose()
    if (btnRef.current) setAnchorRect(btnRef.current.getBoundingClientRect())
    setPreviewOpen(true)
  }, [rows.length, cancelClose])

  // Close on a short delay so the pointer can travel from the FAB to the popover
  // (and between rows) without the popover vanishing under it (safe-triangle).
  const scheduleClose = useCallback(() => {
    cancelClose()
    closeTimerRef.current = setTimeout(() => setPreviewOpen(false), 200)
  }, [cancelClose])

  // Run an intel item: open the panel, then dispatch the prompt once the panel has
  // mounted its 'ai-chat-send' listener. onOpen() flips showAiChat; the panel
  // mounts on the next render, so a short delay before dispatch is enough. (The
  // panel itself then auto-sends ~200ms after receiving the event.)
  // The detail carries the row's title too, so the chat can label the turn with it.
  const runRow = useCallback((r) => {
    setPreviewOpen(false)
    onOpen()
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent('ai-chat-send', { detail: { prompt: r.prompt, cardTitle: r.title } }))
    }, 140)
  }, [onOpen])

  // Keep the popover anchored to the FAB if the viewport changes while it's open.
  useLayoutEffect(() => {
    if (!previewOpen) return
    const sync = () => { if (btnRef.current) setAnchorRect(btnRef.current.getBoundingClientRect()) }
    window.addEventListener('resize', sync)
    window.addEventListener('scroll', sync, true)
    return () => { window.removeEventListener('resize', sync); window.removeEventListener('scroll', sync, true) }
  }, [previewOpen])

  // Tooltip mirrors the panel's split: alerts are the headline attention signal,
  // with investigations mentioned as a secondary count.
  const titleParts = []
  if (alertCount > 0) titleParts.push(`${alertCount} alert${alertCount === 1 ? '' : 's'}`)
  if (investigateCount > 0) titleParts.push(`${investigateCount} to investigate`)
  const title = titleParts.length
    ? `Open AI Assistant — ${titleParts.join(', ')} (drag to reposition)`
    : generating
      ? 'Open AI Assistant — analyzing your data… (drag to reposition)'
      : 'Open AI Assistant (drag to reposition)'

  // Popover placement: anchor to the FAB rect and open toward the side with room.
  // Default FAB is bottom-right, so it opens UP and its right edge aligns with the
  // FAB's right edge. Flip to open downward if the FAB is near the top.
  const preview = previewOpen && anchorRect && (() => {
    const GAP = 10
    const openUp = anchorRect.top > window.innerHeight / 2
    const posStyle = {
      position: 'fixed',
      right: `${Math.max(8, window.innerWidth - anchorRect.right)}px`,
    }
    if (openUp) posStyle.bottom = `${window.innerHeight - anchorRect.top + GAP}px`
    else posStyle.top = `${anchorRect.bottom + GAP}px`

    return createPortal(
      <div
        className={`ai-fab-preview ${openUp ? 'ai-fab-preview-up' : 'ai-fab-preview-down'}`}
        style={posStyle}
        onMouseEnter={cancelClose}
        onMouseLeave={scheduleClose}
        role="dialog"
        aria-label="Workbook Intel — needs attention"
      >
        <div className="ai-fab-preview-head">
          <span className="ai-fab-preview-title">Workbook Intel</span>
          {/* Two distinct chips mirroring the panel: red "alerts" + accent
              "investigate" — never summed into one number. */}
          {alertCount > 0 && (
            <span className={`ai-fab-preview-count ${severityClass}`}>{alertCount} alert{alertCount === 1 ? '' : 's'}</span>
          )}
          {investigateCount > 0 && (
            <span className="ai-fab-preview-count ai-fab-preview-count-investigate">{investigateCount} investigate</span>
          )}
        </div>
        <div className="ai-fab-preview-rows">
          {visibleRows.map((r, i) => (
            <button
              key={i}
              className={`ai-fab-preview-row ${r.kind === 'alert' ? `ai-fab-preview-alert-${r.severity || 'low'}` : 'ai-fab-preview-investigate'} ${r.actioned ? 'ai-fab-preview-row-actioned' : ''}`}
              onClick={() => runRow(r)}
              title="Run in AI chat"
            >
              <span className="ai-fab-preview-icon">{r.icon}</span>
              <span className="ai-fab-preview-text">
                <span className="ai-fab-preview-row-title">{r.title}</span>
                {r.sub && <span className="ai-fab-preview-row-sub">{r.sub}</span>}
              </span>
              {r.actioned
                ? <span className="ai-fab-preview-check" aria-hidden="true"><svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg></span>
                : <span className="ai-fab-preview-run" aria-hidden="true"><IconPlay width={11} height={11} /></span>}
            </button>
          ))}
        </div>
        {overflow > 0 && (
          <button className="ai-fab-preview-more" onClick={onOpen}>
            +{overflow} more in the assistant
          </button>
        )}
        <button className="ai-fab-preview-open" onClick={onOpen}>Open Assistant</button>
      </div>,
      document.body,
    )
  })()

  return (
    <>
      <button
        ref={btnRef}
        className={`ai-fab${dragging ? ' ai-fab-dragging' : ''}`}
        style={style}
        onPointerDown={onPointerDown}
        onClick={() => { if (!didDragRef.current) onOpen() }}
        onMouseEnter={openPreview}
        onMouseLeave={scheduleClose}
        onFocus={openPreview}
        onBlur={scheduleClose}
        title={title}
      >
        ✨
        {showBadge && (
          <span
            key={alertCount}
            className={`ai-fab-badge ${severityClass}`}
            aria-label={`${alertCount} alert${alertCount === 1 ? '' : 's'}`}
          >
            {badgeLabel}
          </span>
        )}
        {!showBadge && generating && (
          <span className="ai-fab-badge ai-fab-badge-working" aria-label="Analyzing data" />
        )}
      </button>
      {preview}
    </>
  )
}
