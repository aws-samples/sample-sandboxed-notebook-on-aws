// Rows of the Live MicroVMs card's Cost tab (MicroVMsPanel.jsx): one flat row
// per cost line — label, a short qualifier, a muted rate line and the amount —
// then the total. Pure (no React/config imports), so node --test can load it.
//
// Rates come from costRateLabels(cost) (per arch, arm64 fallback), so x86_64
// VMs show their own rates.

import { formatDuration as formatDurationMs } from '../utils/formatDuration.js'
import { formatGbFromMb } from './vmCardState.js'

const usd = (n) => `$${(Number(n) || 0).toFixed(6)}`
const num = (v) => (v % 1 === 0 ? v.toFixed(0) : String(v))
const count = (n) => Number(n) || 0

/**
 * Qualifier of the Burst row: "4.1 s above 2 GB baseline · peak +2.1 GB · 1 episode".
 * Each part is shown only when known (burst_seconds > 0, burst_peak_over_mb > 0,
 * burst_episodes >= 1); with no known time it is the plain "above 2 GB baseline".
 * `formatMs` formats milliseconds (the app's formatDuration).
 */
export function burstQualifier(cost, gb, formatMs = formatDurationMs) {
  const secs = Number(cost?.burst_seconds)
  const over = Number(cost?.burst_peak_over_mb)
  const eps = Number(cost?.burst_episodes)
  const base = `above ${gb} GB baseline`
  const parts = [secs > 0 ? `${formatMs(secs * 1000)} ${base}` : base]
  if (over > 0) parts.push(`peak +${formatGbFromMb(over)} GB`)
  if (eps >= 1) parts.push(`${eps} ${eps === 1 ? 'episode' : 'episodes'}`)
  return parts.join(' · ')
}

/**
 * vmCostRows(cost, { memGb, rateLabels, formatDuration }) → { rows, total }
 * rows: [{ key, label, qualifier, rate, amount }] in display order:
 *   compute (always), reads (> 0), storage (suspended > 0), writes (> 0), burst (> 0).
 * total: '$0.042176'. Returns { rows: [], total: null } without a cost dict.
 */
export function vmCostRows(cost, { memGb, rateLabels, formatDuration }) {
  if (!cost) return { rows: [], total: null }
  const fmt = typeof formatDuration === 'function' ? formatDuration : (s) => `${s}s`
  const gb = num(memGb)
  const vcpu = num(memGb / 2)
  const rows = []
  rows.push({
    key: 'compute',
    label: 'Compute',
    qualifier: fmt(cost.running_secs || 0),
    rate: `${vcpu} vCPU × $${rateLabels.vcpu}/s + ${gb} GB × $${rateLabels.memory}/s`,
    amount: usd(cost.running_cost_usd),
  })
  if ((cost.snapshot_read_cost_usd || 0) > 0) {
    rows.push({
      key: 'reads',
      label: 'Snapshot reads',
      qualifier: `${count(cost.resume_count)} (launch/resume)`,
      rate: `${gb} GB × $${rateLabels.read}/GB each`,
      amount: usd(cost.snapshot_read_cost_usd),
    })
  }
  if ((cost.suspended_secs || 0) > 0) {
    rows.push({
      key: 'storage',
      label: 'Snapshot storage',
      qualifier: `${fmt(cost.suspended_secs)} suspended`,
      rate: `${gb} GB × $${rateLabels.storage}/GB-month`,
      amount: usd(cost.suspended_cost_usd),
    })
  }
  if ((cost.snapshot_write_cost_usd || 0) > 0) {
    rows.push({
      key: 'writes',
      label: 'Snapshot writes',
      qualifier: `${count(cost.suspend_count)} (suspend)`,
      rate: `${gb} GB × $${rateLabels.write}/GB each`,
      amount: usd(cost.snapshot_write_cost_usd),
    })
  }
  if ((cost.burst_cost_usd || 0) > 0) {
    const gbs = cost.burst_gb_seconds || (cost.burst_mb_seconds || 0) / 1024
    const vcpus = cost.burst_vcpu_seconds || ((cost.burst_mb_seconds || 0) / 1024 / 2)
    rows.push({
      key: 'burst',
      label: 'Burst',
      qualifier: burstQualifier(cost, gb),
      rate: `${gbs.toFixed(3)} GB·s × $${rateLabels.memory} + ${vcpus.toFixed(3)} vCPU·s × $${rateLabels.vcpu}`,
      amount: usd(cost.burst_cost_usd),
    })
  }
  return { rows, total: usd(cost.total_cost_usd) }
}
