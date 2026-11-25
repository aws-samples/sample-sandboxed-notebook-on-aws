import { IconExpand, IconCollapse } from '../Icons'

/**
 * Shared expand/collapse toggle for a sidebar panel header. Renders inline with the
 * other header buttons (same .sidebar-panel-action styling as the close button), so
 * every panel gets a consistent "maximize" control in its top button row.
 *
 * Renders nothing if the host didn't wire the toggle (onToggleExpand absent), so a
 * panel that opts out simply shows no button.
 */
export default function PanelExpandButton({ expanded, onToggleExpand }) {
  if (!onToggleExpand) return null
  return (
    <button
      className="sidebar-panel-action"
      onClick={onToggleExpand}
      title={expanded ? 'Collapse panel' : 'Expand panel'}
      aria-label={expanded ? 'Collapse panel' : 'Expand panel'}
    >
      {expanded ? <IconCollapse width={14} height={14} /> : <IconExpand width={14} height={14} />}
    </button>
  )
}
