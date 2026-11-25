import { useRef } from 'react'
import Markdown from './Markdown'
import { IconExplain, IconChevronRight, IconChevronDown } from './Icons'
import { relativeAge } from '../services/explain'
import { rangeChip } from '../services/explainPanel'
import './ExplainPanel.css'

/** Explain toggle / opener (cell toolbar and focus-mode toolbar). `className` carries the host's button class. */
export function ExplainButton({ explain, className, onClick, pressed, allowEmpty = false }) {
  const empty = !explain.canExplain && !allowEmpty
  const title = explain.busy ? 'Explaining\u2026' : empty ? 'Add some code to explain' : 'Explain this code'
  return (
    <button
      type="button"
      className={`${className} cell-explain-btn`}
      onClick={(e) => { e.stopPropagation(); onClick() }}
      disabled={empty}
      title={title}
      aria-label="Explain this code"
      aria-busy={explain.busy ? 'true' : 'false'}
      aria-pressed={pressed === undefined ? undefined : pressed}
      aria-controls={pressed === undefined ? undefined : 'cell-explain-panel'}
    >
      {explain.busy ? <span className="cell-gen-spinner" /> : <IconExplain width={13} height={13} />}
    </button>
  )
}

/** Thin strip shown while the panel is collapsed. */
export function ExplainStrip({ onOpen, stacked, busy }) {
  return (
    <button
      type="button"
      className={`cell-explain-strip${stacked ? ' is-stacked' : ''}`}
      onClick={onOpen}
      aria-label="Open explanation panel"
      aria-controls="cell-explain-panel"
      aria-expanded="false"
      title="Open explanation panel"
    >
      {busy ? <span className="cell-gen-spinner" /> : <IconExplain width={14} height={14} />}
    </button>
  )
}

function Section({ section, isPinned, linked, onHover, onPin }) {
  const range = { start: section.start_line, end: section.end_line }
  return (
    <li
      className={`cell-explain-section${isPinned ? ' is-pinned' : ''}`}
      onMouseEnter={linked ? () => onHover(range) : undefined}
      onMouseLeave={linked ? () => onHover(null) : undefined}
    >
      <button
        type="button"
        className="cell-explain-section-btn"
        data-explain-section=""
        aria-current={isPinned ? 'true' : undefined}
        onClick={linked ? () => onPin(range) : undefined}
        onFocus={linked ? () => onHover(range) : undefined}
        onBlur={linked ? () => onHover(null) : undefined}
        title={linked ? 'Highlight these lines in the editor' : 'The code changed, so these lines cannot be shown'}
      >
        <span className="cell-explain-section-title">{section.title}</span>
        <span className="cell-explain-range">{rangeChip(section.start_line, section.end_line)}</span>
      </button>
      {section.explanation && <Markdown className="cell-explain-text" content={section.explanation} />}
    </li>
  )
}

function Result({ explain, badgeToId, onJump, linked, pinned, onHover, onPin }) {
  const result = explain.result
  const omitted = result.omittedCount || 0
  return (
    <>
      {explain.stale && (
        <p className="cell-explain-stale" role="status">
          Code changed since this explanation ·{' '}
          <button type="button" className="cell-explain-link" onClick={explain.regenerate}>Regenerate</button>
        </p>
      )}
      {result.overview && <Markdown className="cell-explain-text cell-explain-overview" content={result.overview} />}
      <ol className="cell-explain-sections" aria-label="Explanation sections">
        {result.sections.map((s) => {
          const isPinned = !!pinned && pinned.start === s.start_line && pinned.end === s.end_line
          const sameAsOverview = result.sections.length === 1 && s.explanation.trim() === result.overview.trim()
          return (
            <Section
              key={`${s.start_line}-${s.end_line}`}
              section={sameAsOverview ? { ...s, explanation: '' } : s}
              isPinned={isPinned}
              linked={linked}
              onHover={onHover}
              onPin={onPin}
            />
          )
        })}
      </ol>
      {result.depends_on.length > 0 && (
        <div className="cell-explain-deps">
          <span className="cell-explain-deps-title">Depends on</span>
          <ul className="cell-explain-deps-list">
            {result.depends_on.map((d) => {
              const id = badgeToId?.get(d.cell)
              const kind = d.cell.startsWith('SQL') ? 'sql' : 'code'
              return (
                <li key={d.cell} className="cell-explain-dep">
                  {id != null ? (
                    <button
                      type="button"
                      className={`cell-type-badge cell-type-${kind} cell-explain-badge cell-explain-badge-link`}
                      onClick={() => onJump?.(id)}
                      title={`Close focus mode and go to ${d.cell}`}
                      aria-label={`Go to ${d.cell}`}
                    >{d.cell}</button>
                  ) : (
                    <span className={`cell-type-badge cell-type-${kind} cell-explain-badge`}>{d.cell}</span>
                  )}
                  {d.provides.map((p) => <code key={p} className="cell-explain-var">{p}</code>)}
                  {d.why && <span className="cell-explain-why">{d.why}</span>}
                </li>
              )
            })}
          </ul>
        </div>
      )}
      {result.notes && (
        <Markdown className="cell-explain-text cell-explain-notes" content={`**Notes:** ${result.notes}`} />
      )}
      {omitted > 0 && (
        <p className="cell-explain-line">
          {omitted} upstream {omitted === 1 ? 'cell' : 'cells'} not included
        </p>
      )}
      {explain.cached && !explain.stale && (
        <p className="cell-explain-line">
          Explained {relativeAge(explain.at)} ·{' '}
          <button type="button" className="cell-explain-link" onClick={explain.regenerate}>Regenerate</button>
        </p>
      )}
    </>
  )
}

/**
 * The focus-mode Explanation panel (right of the code, or under it when narrow).
 * `linked` = section line ranges still match the editor (hover/click highlight them).
 */
export default function ExplainPanel({
  explain, badgeToId, onJump, onCollapse, stacked, width, linked, pinned, onHover, onPin, announcement, onViewNotes,
}) {
  const panelRef = useRef(null)
  const { state, result } = explain
  const showResult = !!result && state === 'done'

  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      // Esc closes the panel; the overlay (focus mode) stays open.
      e.preventDefault()
      e.stopPropagation()
      onCollapse()
      return
    }
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End']
    if (!keys.includes(e.key) || !e.target.matches?.('[data-explain-section]')) return
    const btns = [...panelRef.current.querySelectorAll('[data-explain-section]')]
    const i = btns.indexOf(e.target)
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? btns.length - 1
      : Math.min(btns.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))
    e.preventDefault()
    btns[next]?.focus()
  }

  let body
  if (state === 'loading') {
    body = (
      <div className="cell-explain-state" role="status" aria-live="polite">
        <span className="cell-ai-spinner" />
        <span className="cell-explain-state-text">Explaining…</span>
        <button type="button" className="cell-explain-btn-sm" onClick={explain.cancel}>Cancel</button>
      </div>
    )
  } else if (state === 'error' || state === 'throttled') {
    body = (
      <div className="cell-explain-state cell-explain-problem" role="alert">
        <span className="cell-explain-state-text">
          {state === 'throttled'
            ? 'The AI is busy right now. Try again in a moment.'
            : 'Couldn\u2019t generate an explanation.'}
        </span>
        <button type="button" className="cell-explain-btn-sm" onClick={explain.regenerate}>Retry</button>
      </div>
    )
  } else if (showResult) {
    body = (
      <Result
        explain={explain}
        badgeToId={badgeToId}
        onJump={onJump}
        linked={linked}
        pinned={pinned}
        onHover={onHover}
        onPin={onPin}
      />
    )
  } else if (!explain.canExplain) {
    body = <p className="cell-explain-empty">Add some code to explain.</p>
  } else {
    body = (
      <div className="cell-explain-state">
        <span className="cell-explain-state-text">No explanation yet.</span>
        <button type="button" className="cell-explain-btn-sm cell-explain-btn-primary" onClick={() => explain.run()}>
          Explain this code
        </button>
      </div>
    )
  }

  return (
    <aside
      id="cell-explain-panel"
      ref={panelRef}
      className={`cell-explain-panel${stacked ? ' is-stacked' : ''}`}
      aria-label="Explanation"
      style={stacked ? undefined : { width }}
      onKeyDown={onKeyDown}
    >
      <header className="cell-explain-head">
        <span className="cell-explain-title">✦ Explanation</span>
        {result?.model && <span className="cell-explain-model">{result.model}</span>}
        <button
          type="button"
          className="cell-explain-collapse"
          onClick={onCollapse}
          aria-label="Collapse explanation panel"
          aria-controls="cell-explain-panel"
          aria-expanded="true"
          title="Collapse (Esc)"
        >
          {stacked ? <IconChevronDown width={14} height={14} /> : <IconChevronRight width={14} height={14} />}
        </button>
      </header>
      <div className="cell-explain-body">{body}</div>
      {showResult && (
        <footer className="cell-explain-foot">
          {explain.justAdded && (
            <p className="cell-explain-confirm" role="status">
              Added to notes ·{' '}
              <button type="button" className="cell-explain-link" onClick={onViewNotes}>View notes</button>
            </p>
          )}
          <div className="cell-explain-actions">
            <button type="button" className="cell-explain-btn-sm" onClick={explain.regenerate} disabled={explain.busy}>
              Regenerate
            </button>
            <button type="button" className="cell-explain-btn-sm" onClick={explain.copy} aria-live="polite">
              {explain.copied ? 'Copied' : 'Copy'}
            </button>
            <button
              type="button"
              className="cell-explain-btn-sm cell-explain-btn-primary"
              onClick={explain.addToNotes}
              disabled={explain.added}
              title={explain.added ? 'Already in this cell\u2019s notes' : 'Append this explanation to the cell notes'}
            >
              {explain.added ? 'Added ✓' : 'Add to cell notes'}
            </button>
          </div>
        </footer>
      )}
      <div className="cell-explain-sr" role="status" aria-live="polite">{announcement}</div>
    </aside>
  )
}
