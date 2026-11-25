// Per-arch MicroVM rate labels for the Live MicroVMs Cost tab, and the short
// arch label used on VM spec lines.
//
// The proxy's cost dict (proxy/platform/cost_tracker.py::price_quantities)
// carries the effective `rates` it priced the VM with (per arch, from
// MICROVM_PRICING_JSON). Older proxies don't send `rates`, so the arm64
// (Graviton) rates are the fallback — the same numbers the UI hard-coded before.

export const ARM64_FALLBACK_RATES = Object.freeze({
  vcpu_per_sec: 0.0000276944,
  memory_per_gb_sec: 0.0000036667,
  snapshot_per_gb_month: 0.08,
  snapshot_read_per_gb: 0.00155,
  snapshot_write_per_gb: 0.00380,
})

function rate(rates, key) {
  const v = rates?.[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : ARM64_FALLBACK_RATES[key]
}

// costRateLabels(cost) → display strings (no "$"), e.g. for arm64:
//   { vcpu: '0.0000277', memory: '0.0000037', read: '0.00155',
//     write: '0.00380', storage: '0.08' }
export function costRateLabels(cost) {
  const rates = cost?.rates
  return {
    vcpu: rate(rates, 'vcpu_per_sec').toFixed(7),
    memory: rate(rates, 'memory_per_gb_sec').toFixed(7),
    read: rate(rates, 'snapshot_read_per_gb').toFixed(5),
    write: rate(rates, 'snapshot_write_per_gb').toFixed(5),
    storage: rate(rates, 'snapshot_per_gb_month').toFixed(2),
  }
}

// archLabel(arch) → 'x86_64' / 'ARM64'; 'unknown' when the arch is missing.
// A missing arch is NOT labelled ARM64: that showed x86_64 VMs as ARM.
export function archLabel(arch) {
  if (arch === 'x86_64') return 'x86_64'
  if (arch === 'arm64') return 'ARM64'
  return arch ? String(arch) : 'unknown'
}
