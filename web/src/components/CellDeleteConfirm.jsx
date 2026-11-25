import { ConfirmModal } from './Modal'
import './CellDeleteConfirm.css'

/**
 * Confirmation for deleting one or more cells. The wording comes from
 * planCellDelete() (services/cellDelete.js); this component only lays it out.
 * Long cell text is shown with CSS ellipsis and a title tooltip, not cut in JS.
 */
export default function CellDeleteConfirm({ plan, onConfirm, onCancel, focusFallbackSelector }) {
  return (
    <ConfirmModal
      title={plan.title}
      confirmLabel="Delete"
      confirmDanger
      onConfirm={onConfirm}
      onCancel={onCancel}
      focusFallbackSelector={focusFallbackSelector}
    >
      {plan.count === 1 ? (
        <code className="cell-delete-summary" title={plan.items[0].summary}>{plan.items[0].summary}</code>
      ) : (
        <ul className="cell-delete-list">
          {plan.items.map((item, i) => (
            <li key={`${item.badge}-${i}`} className="cell-delete-item">
              {item.badge && <span className="cell-delete-badge">{item.badge}</span>}
              <code className="cell-delete-text" title={item.summary}>{item.summary}</code>
            </li>
          ))}
          {plan.moreCount > 0 && <li className="cell-delete-more">+{plan.moreCount} more</li>}
        </ul>
      )}
      <DependentsNote note={plan.dependentsNote} />
      <p className="cell-delete-undo">{plan.undoNote}</p>
    </ConfirmModal>
  )
}

/**
 * "This cell defines `x`, used by PY-3 and PY-5. They'll keep working until the
 * kernel restarts." Only when other live cells read a variable that no other
 * live cell defines; otherwise nothing renders and the dialog is unchanged.
 * Names and cell lists are shown in full (they wrap, never cut).
 */
function DependentsNote({ note }) {
  if (!note) return null
  if (note.kind === 'single') {
    const [item] = note.items
    return (
      <p className="cell-delete-deps" role="note">
        {note.subject} {note.verb} <code>{item.name}</code>, used by {item.usedByText}. They'll keep working until the kernel restarts.
      </p>
    )
  }
  return (
    <div className="cell-delete-deps" role="note">
      <p>{note.intro}</p>
      <ul className="cell-delete-deps-list" aria-label="Variables other cells use">
        {note.items.map(item => (
          <li key={item.name}><code>{item.name}</code> used by {item.usedByText}</li>
        ))}
      </ul>
    </div>
  )
}
