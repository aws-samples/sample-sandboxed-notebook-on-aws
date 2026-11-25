/**
 * Collapsed VM card sections (Overview: Notebook / Lifecycle / Checkpoint;
 * Resources: History / VM lifetime / Environment). Pure helpers over an immutable
 * Set of "<vmId>\u0000<section>" keys; every section is expanded by default.
 * The Set lives in MicroVMsPanel state, so /instances polling re-renders and tab
 * switches keep it (not persisted across reloads).
 */
export const VM_SECTIONS = Object.freeze(['notebook', 'lifecycle', 'checkpoint', 'history', 'lifetime', 'environment'])

export function sectionKey(vmId, section) {
  return `${String(vmId ?? '')}\u0000${String(section ?? '')}`
}

export function isSectionCollapsed(collapsed, vmId, section) {
  return !!collapsed && collapsed.has(sectionKey(vmId, section))
}

/** New Set with the section's collapsed state flipped (input not mutated). */
export function toggleSection(collapsed, vmId, section) {
  const next = new Set(collapsed || [])
  const k = sectionKey(vmId, section)
  if (next.has(k)) next.delete(k); else next.add(k)
  return next
}
