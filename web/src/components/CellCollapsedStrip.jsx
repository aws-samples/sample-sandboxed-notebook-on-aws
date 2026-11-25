import { collapsedPreview, lineCountLabel, collapsedStatusHint } from '../services/cellCollapse'
import { IconAlertTriangle } from './Icons'

/**
 * The one-row body of a collapsed cell (code, SQL and Markdown cells): the first
 * source line, an "N lines" pill and a compact status hint (Running… / the error's
 * first line / Interrupted / an output summary such as "100 rows × 8 columns").
 * Clicking it expands the cell; the gutter chevron is the keyboard control.
 */
export default function CellCollapsedStrip({ cell, onExpand }) {
  const preview = collapsedPreview(cell)
  const hint = collapsedStatusHint(cell)
  return (
    <div className="cell-collapsed-summary" onClick={onExpand} title="Collapsed cell. Click to expand">
      <span className={`cell-collapsed-text${preview ? '' : ' cell-collapsed-text-empty'}`}>
        {preview || 'Empty cell'}
      </span>
      <span className="cell-collapsed-lines">{lineCountLabel(cell.code)}</span>
      {hint && (
        <span className="cell-collapsed-hint" data-tone={hint.tone} title={hint.text}>
          {hint.tone === 'running' && <span className="cell-collapsed-spinner" aria-hidden="true" />}
          {hint.tone === 'error' && <IconAlertTriangle width={11} height={11} aria-hidden="true" />}
          <span className="cell-collapsed-hint-text">{hint.text}</span>
        </span>
      )}
    </div>
  )
}
