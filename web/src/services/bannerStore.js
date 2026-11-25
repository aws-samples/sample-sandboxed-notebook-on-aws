// In-memory store for panel-wide action banners, one banner per slot (see
// services/actionBanner.js for the slots and rules). Used by banners that are not
// tied to one notebook (sandbox, import, package install, terminal). Per-notebook
// banners live on the tab instead (`_runBanner`, `_aiBanner`).
//
// A banner stays in the store until it is cleared; the panel that shows it starts
// its 10 s countdown when it is first visible there.
import { useSyncExternalStore } from 'react'
import { replaceInSlot, shouldClear } from './actionBanner.js'

const slots = new Map()
const listeners = new Set()

function emit() {
  for (const fn of listeners) fn()
}

export function getSnapshot(slot) {
  return slots.get(slot) || null
}

export function subscribe(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

// Show `banner` in `slot` (replaces the current one when newer).
export function publish(slot, banner) {
  const next = replaceInSlot(slots.get(slot) || null, banner ? { ...banner, slot } : null)
  if (next === slots.get(slot)) return
  slots.set(slot, next)
  emit()
}

// Remove the banner in `slot`. With `id`, only if that banner is still the one shown.
export function clear(slot, id) {
  const cur = slots.get(slot)
  if (!cur) return
  if (id != null && cur.id !== id) return
  slots.delete(slot)
  emit()
}

// Remove every banner (in `slot`, or in all slots) that `event` clears for its kind,
// optionally limited by `match(banner)`.
export function clearOn(event, { slot = null, match = null } = {}) {
  let changed = false
  for (const [s, b] of slots) {
    if (slot != null && s !== slot) continue
    if (!shouldClear(b.kind, event)) continue
    if (match && !match(b)) continue
    slots.delete(s)
    changed = true
  }
  if (changed) emit()
}

// React hook: the current banner of `slot` (or null).
export function useSlotBanner(slot) {
  return useSyncExternalStore(subscribe, () => getSnapshot(slot), () => null)
}
