import { filterPillClassName, nextFilterPillIndex, selectedFilterPillIndex } from '../services/filterPills'

/**
 * Shared single-select filter pills (Outline type filter, Packages scope, ...).
 * Look: the Outline pill (.outline-filter-chip): separate fully rounded pills,
 * neutral raised fill, selected = blue-tinted fill with blue text.
 * Semantics: role=radiogroup / radio + aria-checked, one tab stop (the selected
 * pill), arrow keys move and select, Home/End jump.
 *
 * options: [{ key, label, title?, className? }]
 * className: the container's layout class (the row it sits in owns spacing).
 */
export default function FilterPills({ options, value, onChange, ariaLabel, className }) {
  const tabStop = selectedFilterPillIndex(options, value)
  const onKeyDown = (e) => {
    const next = nextFilterPillIndex(e.key, tabStop, options.length)
    if (next == null) return
    e.preventDefault()
    onChange(options[next].key)
    const btns = e.currentTarget.querySelectorAll('[role="radio"]')
    if (btns[next]) btns[next].focus()
  }
  return (
    <div className={className} role="radiogroup" aria-label={ariaLabel} onKeyDown={onKeyDown}>
      {options.map((o, i) => (
        <button
          key={o.key}
          type="button"
          role="radio"
          aria-checked={value === o.key}
          tabIndex={i === tabStop ? 0 : -1}
          className={filterPillClassName(value === o.key, o.className)}
          onClick={() => onChange(o.key)}
          title={o.title}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}
