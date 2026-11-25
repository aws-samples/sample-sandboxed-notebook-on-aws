import { useState, useId, useRef, useEffect, useLayoutEffect, useMemo } from 'react'
import { highlightBasic } from '../services/basicHighlight'
import { showQuestionSubtitle } from '../services/chatTurns'

/**
 * One conversation turn in the AI chat. The question appears ONCE per turn:
 *  - Expanded: the question bubble IS the sticky header (collapse toggle, question,
 *    counts, time, Insert all + status in one sticky element). While pinned it goes
 *    compact (question on one line, ellipsis + tooltip; attachments and the card's
 *    full-question disclosure hidden) inside a box of unchanged height, so a long
 *    bubble scrolls away normally and its last strip becomes the pinned bar
 *    (usePinGeometry). Then the folded tool steps and the assistant
 *    bubbles. The reply's own heading is visible in the reply, so no title line.
 *  - Collapsed: one line with the extracted title + counts + time, and the question
 *    as a small second line only when it differs from the title. The panel owns all state and handlers; this
 * component only lays the turn out. Assistant bubbles are rendered by the panel
 * (`renderBubble`) so the existing bubble markup and handlers stay there.
 */

const APPROVAL_LABEL = { approved: 'Approved', rejected: 'Rejected', declined: 'Declined (new message)' }

function StepsFold({ steps }) {
  const [open, setOpen] = useState(false)
  const listId = useId()
  if (!steps.length) return null
  return (
    <div className={`ai-steps${open ? ' ai-steps-open' : ''}`}>
      <button
        type="button"
        className="ai-steps-toggle"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen(o => !o)}
      >
        <span className="ai-steps-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
        {steps.length} step{steps.length === 1 ? '' : 's'}
      </button>
      <ol id={listId} className="ai-steps-list" hidden={!open}>
        {open && steps.map((s, i) => (
          <li key={i} className={`ai-step ai-step-${s.kind}`}>
            {s.kind === 'note' ? (
              <span className="ai-step-note">{s.text}</span>
            ) : (
              <>
                <span className={`ai-step-decision ai-step-${s.decision}`}>
                  {APPROVAL_LABEL[s.decision] || s.decision}: {s.action === 'confirm_install' ? `Install ${s.detail}` : 'Run code'}
                </span>
                {s.action !== 'confirm_install' && s.detail && (
                  // highlightBasic HTML-escapes as it emits (same as the approval card).
                  <pre className="ai-code-pre ai-step-code"><code
                    className="language-python"
                    dangerouslySetInnerHTML={{ __html: highlightBasic(String(s.detail), 'python') }}
                  /></pre>
                )}
              </>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function Counts({ total, inserted }) {
  if (!(total > 0)) return null
  return (
    <span className="ai-turn-counts">
      {total} cell{total === 1 ? '' : 's'}{inserted > 0 ? ` · ${inserted} inserted` : ''}
    </span>
  )
}

/**
 * Pinned ("stuck") detection for the sticky question header: a 1px sentinel at the
 * section top + --pin-shift, observed against the message list. It leaves the
 * list's top edge exactly when the header starts sticking. Fires only on transitions.
 */
function useStuck(sentinelRef, enabled) {
  const [stuck, setStuck] = useState(false)
  useEffect(() => {
    const el = sentinelRef.current
    if (!enabled || !el || typeof IntersectionObserver === 'undefined') { setStuck(false); return }
    const root = el.closest('.ai-panel-messages')
    const io = new IntersectionObserver(([e]) => {
      const top = e.rootBounds ? e.rootBounds.top : 0
      setStuck(!e.isIntersecting && e.boundingClientRect.top < top)
    }, { root, threshold: 0 })
    io.observe(el)
    return () => io.disconnect()
  }, [sentinelRef, enabled])
  return stuck
}

/**
 * Pin geometry, so pinning never changes the header's size (no layout shift, no
 * content vanishing under the pointer, nothing for the stuck state to flip on):
 *  - --pin-full-h: the header's natural (full) height. While pinned the header box
 *    keeps exactly this height and lays its compact content out at its bottom.
 *  - --pin-shift: full height minus the compact height. The header's sticky `top`
 *    is pulled up by it, so a tall bubble scrolls away normally until only a
 *    compact-height strip is left, and that strip becomes the pinned bar.
 * Measured on header resize only (ResizeObserver, never per scroll event). The
 * compact height comes from a pure-CSS measuring class (.ai-turn-measure applies
 * the same compact rules as .ai-turn-stuck, without the frozen height). Skipped
 * while pinned: the frozen box doesn't resize, and the last values stay valid.
 */
function usePinGeometry(sectionRef, headRef, enabled) {
  useLayoutEffect(() => {
    const sec = sectionRef.current
    const head = headRef.current
    if (!enabled || !sec || !head) return
    const measure = () => {
      if (sec.classList.contains('ai-turn-stuck')) return
      const full = head.getBoundingClientRect().height
      sec.classList.add('ai-turn-measure')
      const compact = head.getBoundingClientRect().height
      sec.classList.remove('ai-turn-measure')
      sec.style.setProperty('--pin-full-h', `${full}px`)
      sec.style.setProperty('--pin-shift', `${Math.max(0, full - compact)}px`)
    }
    measure()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    ro?.observe(head)
    return () => {
      ro?.disconnect()
      sec.style.removeProperty('--pin-full-h')
      sec.style.removeProperty('--pin-shift')
    }
  }, [sectionRef, headRef, enabled])
}

export default function ChatTurn({
  view, ordinal, time, onToggle, onInsertAll, canInsert, attachIcon, renderBubble, children, contextTurns = 0,
}) {
  const bodyId = useId()
  const fullId = useId()
  const [showFull, setShowFull] = useState(false)
  const sentinelRef = useRef(null)
  const sectionRef = useRef(null)
  const headRef = useRef(null)
  const { turn, title, label, isCard, cardTitle, status, steps, collapsed, isStreaming } = view
  // Older than the agent's context window: muted, still fully usable. Meaning is
  // also carried by the divider, the aria-label suffix and the bubble tooltip.
  const outOfContext = view.inContext === false
  const ctxSuffix = outOfContext ? ', not in AI context' : ''
  const ctxTooltip = outOfContext
    ? `Not in the AI's context: it only remembers the last ${contextTurns} conversation${contextTurns === 1 ? '' : 's'}`
    : undefined
  const stuck = useStuck(sentinelRef, !collapsed)
  usePinGeometry(sectionRef, headRef, !collapsed)
  const total = status.total
  const inserted = status.inserted
  const pendingCount = status.pending.length
  const user = turn.user
  const atts = Array.isArray(user?.attachments) ? user.attachments : []
  // What the question shows as: the card title for card turns, else the typed text.
  // An attachments-only question shows its label only while pinned (chips are
  // hidden then); CSS shows it, so the compact height can be measured unpinned.
  const qText = isCard ? (cardTitle || label) : (user?.content || '')
  const qLabelOnly = !isCard && !user?.content && !!label
  const qTooltip = isCard ? (cardTitle || label) : (user?.content || label)
  const toggleName = `${collapsed ? 'Expand' : 'Collapse'} turn ${ordinal + 1}: ${title}${ctxSuffix}`

  // Compact-while-pinned is pure CSS on .ai-turn-stuck (see usePinGeometry): the
  // header keeps its full height while pinned, so nothing moves.
  const pinned = stuck && !collapsed

  // The body doesn't depend on the pinned / full-question state: don't re-render
  // every assistant bubble when the header pins or unpins.
  const assistant = turn.assistant
  const body = useMemo(() => (collapsed ? <div id={bodyId} hidden /> : (
    <div id={bodyId} className="ai-turn-body">
      <StepsFold steps={steps} />
      {assistant.map((a, offset) => renderBubble(a, offset))}
      {children}
    </div>
  )), [collapsed, bodyId, steps, assistant, renderBubble, children])

  const toggle = (
    <button
      type="button"
      className="ai-turn-toggle"
      aria-expanded={!collapsed}
      aria-controls={bodyId}
      aria-label={toggleName}
      aria-disabled={isStreaming ? 'true' : undefined}
      title={isStreaming ? 'Still answering' : undefined}
      onClick={() => { if (!isStreaming) onToggle(view) }}
    >
      <span className="ai-turn-caret" aria-hidden="true">{collapsed ? '▸' : '▾'}</span>
      {collapsed && <span className="ai-turn-title" title={title}>{title}</span>}
    </button>
  )

  // One tree for both states: the toggle keeps the same element (section > header
  // > first row > button), so React never remounts it and keyboard focus stays on
  // it across expand/collapse.
  const showSub = collapsed && !!user && showQuestionSubtitle({ title, question: label })
  return (
    <section
      ref={sectionRef}
      className={`ai-turn${collapsed ? ' ai-turn-collapsed' : ''}${pinned ? ' ai-turn-stuck' : ''}${outOfContext ? ' ai-turn-out' : ''}`}
      data-turn-key={turn.key}
      aria-label={`Turn ${ordinal + 1}: ${title}${ctxSuffix}`}
    >
      <span ref={sentinelRef} className="ai-turn-sentinel" aria-hidden="true" />
      {/* Collapsed: the whole folded card toggles on click (mouse convenience; the
          toggle button stays the keyboard control and handles its own clicks). */}
      <div
        ref={headRef}
        className={`ai-turn-header${collapsed ? '' : ' ai-turn-header-open'}`}
        onClick={collapsed ? (e) => { if (!isStreaming && !e.target.closest('button')) onToggle(view) } : undefined}
      >
        <div className={collapsed ? 'ai-turn-row' : 'ai-turn-qrow'} title={collapsed ? ctxTooltip : undefined}>
          {toggle}
          {collapsed ? (
            <>
              <Counts total={total} inserted={inserted} />
              {time && <span className="ai-turn-time">{time}</span>}
            </>
          ) : user ? (
            <div className="ai-msg ai-msg-user ai-turn-q">
              <div className="ai-msg-content" title={ctxTooltip}>
                {atts.length > 0 && (
                  <div className="ai-msg-attachments">
                    {atts.map((att, ai) => (
                      <span key={ai} className="ai-attach-chip ai-attach-chip-msg" title={att.name}>
                        <span className="ai-attach-icon">{attachIcon(att.mime, att.name)}</span>
                        <span className="ai-attach-name">{att.name}</span>
                      </span>
                    ))}
                  </div>
                )}
                {qText && (
                  <div className="ai-turn-qtext" title={pinned ? qTooltip : undefined}>
                    {isCard && <span className="ai-turn-src">Intel</span>}
                    {qText}
                  </div>
                )}
                {qLabelOnly && (
                  <div className="ai-turn-qtext ai-turn-qlabel" title={pinned ? qTooltip : undefined}>{label}</div>
                )}
                {isCard && (
                  <div className="ai-turn-full">
                    <button
                      type="button"
                      className="ai-turn-full-btn"
                      aria-expanded={showFull}
                      aria-controls={fullId}
                      onClick={() => setShowFull(v => !v)}
                    >
                      {showFull ? 'Hide full question' : 'Show full question'}
                    </button>
                    <div id={fullId} className="ai-turn-full-q" hidden={!showFull}>{user.content}</div>
                  </div>
                )}
              </div>
            </div>
          ) : <span className="ai-turn-q-none" />}
        </div>
        {collapsed ? (showSub && (
          <div className="ai-turn-row ai-turn-sub">
            <span className="ai-turn-question" title={label}>
              {isCard && <span className="ai-turn-src">Intel</span>}
              {label}
            </span>
          </div>
        )) : (
          <div className="ai-turn-row ai-turn-meta">
            <Counts total={total} inserted={inserted} />
            {time && <span className="ai-turn-time">{time}</span>}
            {total > 0 && canInsert && (
              <>
                <span className="ai-turn-insert-status" aria-live="polite">Inserted {inserted} of {total}</span>
                <button
                  type="button"
                  className="ai-turn-insert-all"
                  disabled={isStreaming || pendingCount === 0}
                  onClick={() => onInsertAll(view)}
                  title={pendingCount === 0
                    ? 'Every cell from this answer is in the notebook'
                    : `Insert the ${pendingCount} cell${pendingCount === 1 ? '' : 's'} not inserted yet, in order (already-inserted cells are skipped)`}
                >
                  {pendingCount === 0 ? 'All inserted' : 'Insert all'}
                </button>
              </>
            )}
          </div>
        )}
      </div>
      {body}
    </section>
  )
}
