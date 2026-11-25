/**
 * VM card section heading as a collapse toggle: the blue uppercase title
 * (.vm-section-title) with the app's rotating chevron (.rh-chevron). The `id`
 * stays on the button so the section's aria-labelledby keeps working.
 */
export default function VmSectionToggle({ id, label, expanded, onToggle, controls, className = '' }) {
  return (
    <button
      type="button"
      id={id}
      className={`vm-section-title vm-section-toggle${className ? ` ${className}` : ''}`}
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={(e) => { e.stopPropagation(); onToggle() }}
    >
      <svg className={`rh-chevron${expanded ? ' open' : ''}`} width={10} height={10} viewBox="0 0 24 24" fill="none"
        stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="9 18 15 12 9 6" />
      </svg>
      <span>{label}</span>
    </button>
  )
}
