// Pure sort logic for the Live MicroVMs panel (MicroVMsPanel.jsx). Lives here so
// node tests can cover it. The same comparator orders the notebook VM list and
// the Pool spares list. The choice is persisted under localStorage
// 'microvm-vm-sort' by the panel.

export const VM_SORT_STORAGE_KEY = 'microvm-vm-sort'
export const DEFAULT_VM_SORT = 'launched-desc'

// Display order of the sort <select>. Arch options come in a pair, like the
// newest/oldest and largest/smallest pairs.
export const VM_SORT_OPTIONS = Object.freeze([
  { value: 'launched-desc', label: 'Launched (newest first)' },
  { value: 'launched-asc', label: 'Launched (oldest first)' },
  { value: 'memory-desc', label: 'Memory (largest first)' },
  { value: 'memory-asc', label: 'Memory (smallest first)' },
  { value: 'state', label: 'State' },
  { value: 'arch-arm64', label: 'Architecture (ARM64 first)' },
  { value: 'arch-x86_64', label: 'Architecture (x86_64 first)' },
])

const SORT_VALUES = new Set(VM_SORT_OPTIONS.map(o => o.value))

// Stored value → a known sort key; anything unknown (old/garbled) → default.
export function parseStoredVmSort(v) {
  return SORT_VALUES.has(v) ? v : DEFAULT_VM_SORT
}

// 'x86_64' for x86_64, else 'arm64' (missing arch = arm64, same default as archLabel).
export function archKey(arch) {
  return arch === 'x86_64' ? 'x86_64' : 'arm64'
}

// Normalize launched_at (epoch seconds number OR ISO string) to a comparable ms number.
function launchedMs(inst) {
  const v = inst?.launched_at
  if (v == null) return 0
  return typeof v === 'number' ? v * 1000 : new Date(v).getTime()
}

// Group RUNNING first, then SUSPENDED, then everything else — for the 'state' sort.
function stateRank(inst) {
  const s = (inst?.state || '').toUpperCase()
  if (s === 'RUNNING') return 0
  if (s === 'SUSPENDED') return 1
  return 2
}

// Comparator for the chosen sort key. Operates on [id, inst] entries.
// Arch keys: the chosen arch first, then memory desc, then launched desc.
export function makeVmComparator(sortKey) {
  return (a, b) => {
    const ia = a[1], ib = b[1]
    switch (sortKey) {
      case 'launched-asc':
        return launchedMs(ia) - launchedMs(ib)
      case 'memory-desc':
        return (ib.memory_mib || 0) - (ia.memory_mib || 0)
      case 'memory-asc':
        return (ia.memory_mib || 0) - (ib.memory_mib || 0)
      case 'state':
        return stateRank(ia) - stateRank(ib)
      case 'arch-arm64':
      case 'arch-x86_64': {
        const first = sortKey === 'arch-x86_64' ? 'x86_64' : 'arm64'
        const ra = archKey(ia.architecture) === first ? 0 : 1
        const rb = archKey(ib.architecture) === first ? 0 : 1
        return (ra - rb)
          || ((ib.memory_mib || 0) - (ia.memory_mib || 0))
          || (launchedMs(ib) - launchedMs(ia))
      }
      case 'launched-desc':
      default:
        return launchedMs(ib) - launchedMs(ia)
    }
  }
}
