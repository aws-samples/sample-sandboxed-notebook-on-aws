// Tab model for the expanded VM card in the Live MicroVMs panel (MicroVMsPanel.jsx).
//
// The card used to stack Instance / Lifecycle / Resources / Cost Breakdown sections.
// It now shows one tab at a time. The selected tab is remembered for the PANEL (not
// per VM) in localStorage, so opening another VM's card shows the same tab. Cost is
// hidden for a VM without cost data; if the remembered tab is hidden, that VM shows
// Overview (the stored choice is kept for the next VM). Resources is shown for EVERY
// VM: History (the per-run chart + lifetime bars) and Environment (image, base,
// Python, packages) apply to suspended VMs and pool spares too.

export const VM_TAB_STORAGE_KEY = 'microvm-vm-detail-tab'
export const DEFAULT_VM_TAB = 'overview'

// Display order. Overview = Notebook, Lifecycle and Checkpoint sections.
export const VM_DETAIL_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'cost', label: 'Cost' },
  { id: 'resources', label: 'Resources' },
]

const KNOWN = new Set(VM_DETAIL_TABS.map(t => t.id))

/**
 * Tabs to show for one VM. Overview and Resources are always there; Cost only with cost
 * data. `hasResources` is still accepted (older callers) and ignored.
 */
export function visibleVmTabs({ hasCost = false } = {}) {
  const has = { overview: true, cost: !!hasCost, resources: true }
  return VM_DETAIL_TABS.filter(t => has[t.id])
}

/** A stored value (localStorage string, possibly null/garbage) → a known tab id, else Overview. */
export function parseStoredVmTab(raw) {
  return typeof raw === 'string' && KNOWN.has(raw) ? raw : DEFAULT_VM_TAB
}

/** The tab to show for this VM: the remembered one if visible for it, else Overview. */
export function resolveVmTab(selected, visibleTabs) {
  const ids = (visibleTabs || []).map(t => (typeof t === 'string' ? t : t.id))
  return ids.includes(selected) ? selected : DEFAULT_VM_TAB
}

/**
 * WAI-ARIA tabs keyboard model: Left/Right move (wrapping), Home/End jump.
 * Returns the new index, or null when the key isn't a tab-navigation key.
 */
export function nextVmTabIndex(key, index, count) {
  if (count <= 0) return null
  switch (key) {
    case 'ArrowRight': return (index + 1) % count
    case 'ArrowLeft': return (index - 1 + count) % count
    case 'Home': return 0
    case 'End': return count - 1
    default: return null
  }
}
