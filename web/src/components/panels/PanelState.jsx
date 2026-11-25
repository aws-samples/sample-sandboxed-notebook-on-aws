/**
 * PanelState — shared empty / loading / error state for sidebar panels.
 *
 * Standardizes the "nothing here yet" / "loading…" / "couldn't load" moments
 * across all panels into one polished treatment: a muted icon, a concise
 * message, and an optional action button. Replaces the ad-hoc plain-text
 * `.sidebar-empty` / `.app-empty-samples-loading` / etc. usages.
 *
 * Usage:
 *   <PanelState variant="loading" message="Loading packages…" />
 *   <PanelState variant="empty" icon={<IconX/>} message="No notebooks open" />
 *   <PanelState variant="error" message="Couldn't load run history"
 *               action={{ label: 'Retry', onClick: reload }} />
 */
export default function PanelState({ variant = 'empty', icon, message, hint, action }) {
  return (
    <div className={`panel-state panel-state-${variant}`}>
      {variant === 'loading'
        ? <span className="panel-state-spinner" aria-hidden="true" />
        : icon && <span className="panel-state-icon" aria-hidden="true">{icon}</span>}
      <span className="panel-state-message">{message}</span>
      {hint && <span className="panel-state-hint">{hint}</span>}
      {action && (
        <button className="panel-state-action" onClick={action.onClick}>
          {action.label}
        </button>
      )}
    </div>
  )
}
