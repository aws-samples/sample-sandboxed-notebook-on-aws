// Pure helpers for the shared single-select filter pills (components/FilterPills.jsx).
// The look is the Outline panel's type-filter pill (.outline-filter-chip); these
// helpers only build its class list and the WAI-ARIA radio-group keyboard model.

/** Class list for one pill: the shared pill class, `active` when selected, plus any extra. */
export function filterPillClassName(selected, extra = '') {
  return ['outline-filter-chip', selected ? 'active' : '', extra || ''].filter(Boolean).join(' ')
}

/**
 * Radio-group keyboard model: Left/Up and Right/Down move (wrapping), Home/End jump.
 * Returns the new index, or null when the key is not a navigation key.
 */
export function nextFilterPillIndex(key, index, count) {
  if (count <= 0) return null
  const from = index >= 0 && index < count ? index : 0
  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown': return (from + 1) % count
    case 'ArrowLeft':
    case 'ArrowUp': return (from - 1 + count) % count
    case 'Home': return 0
    case 'End': return count - 1
    default: return null
  }
}

/** Index of the selected option, or 0 (the roving tab stop) when none matches. */
export function selectedFilterPillIndex(options, value) {
  const i = (options || []).findIndex(o => o.key === value)
  return i < 0 ? 0 : i
}
