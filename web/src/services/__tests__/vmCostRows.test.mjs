// Unit tests for services/vmCostRows.js (Live MicroVMs card: Cost tab rows).
// Run with: node --test web/src/services/__tests__/vmCostRows.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { vmCostRows, burstQualifier } from '../vmCostRows.js'
import { costRateLabels } from '../costRates.js'

// Same as Sidebar.jsx's formatDuration (passed to the panel as a prop).
const formatDuration = (secs) => {
  if (!secs || secs < 60) return `${secs || 0}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`
}

// The user's screenshot: a 2 GB arm64 VM (old proxy: no cost.rates → arm64 fallback).
const ARM_COST = {
  running_secs: 376, running_cost_usd: 0.01317,
  snapshot_read_cost_usd: 0.0062, resume_count: 2,
  suspended_secs: 94, suspended_cost_usd: 0.000006,
  snapshot_write_cost_usd: 0.0228, suspend_count: 3,
  burst_cost_usd: 0, total_cost_usd: 0.042176,
}

const rowsFor = (cost, memGb) => vmCostRows(cost, { memGb, rateLabels: costRateLabels(cost), formatDuration })

test('arm64 (fallback rates) reproduces the approved rows', () => {
  const { rows, total } = rowsFor(ARM_COST, 2)
  assert.deepEqual(rows.map(r => r.key), ['compute', 'reads', 'storage', 'writes'])
  assert.deepEqual(rows[0], {
    key: 'compute', label: 'Compute', qualifier: '6m 16s',
    rate: '1 vCPU × $0.0000277/s + 2 GB × $0.0000037/s', amount: '$0.013170',
  })
  assert.deepEqual(rows[1], {
    key: 'reads', label: 'Snapshot reads', qualifier: '2 (launch/resume)',
    rate: '2 GB × $0.00155/GB each', amount: '$0.006200',
  })
  assert.deepEqual(rows[2], {
    key: 'storage', label: 'Snapshot storage', qualifier: '1m 34s suspended',
    rate: '2 GB × $0.08/GB-month', amount: '$0.000006',
  })
  assert.deepEqual(rows[3], {
    key: 'writes', label: 'Snapshot writes', qualifier: '3 (suspend)',
    rate: '2 GB × $0.00380/GB each', amount: '$0.022800',
  })
  assert.equal(total, '$0.042176')
})

test('x86_64: cost.rates gives the x86 rate labels; 4 GB → 2 vCPU', () => {
  const cost = {
    ...ARM_COST,
    rates: {
      vcpu_per_sec: 0.0000333333, memory_per_gb_sec: 0.0000044444,
      snapshot_per_gb_month: 0.09, snapshot_read_per_gb: 0.00186, snapshot_write_per_gb: 0.00456,
    },
  }
  const { rows } = rowsFor(cost, 4)
  assert.equal(rows[0].rate, '2 vCPU × $0.0000333/s + 4 GB × $0.0000044/s')
  assert.equal(rows[1].rate, '4 GB × $0.00186/GB each')
  assert.equal(rows[2].rate, '4 GB × $0.09/GB-month')
  assert.equal(rows[3].rate, '4 GB × $0.00456/GB each')
})

test('rows are hidden at 0; compute always shows', () => {
  const { rows, total } = rowsFor({ running_secs: 30, running_cost_usd: 0.001, total_cost_usd: 0.001 }, 2)
  assert.deepEqual(rows.map(r => r.key), ['compute'])
  assert.equal(rows[0].qualifier, '30s')
  assert.equal(total, '$0.001000')
})

test('burst row in the same style (fallbacks from burst_mb_seconds)', () => {
  const cost = { ...ARM_COST, burst_cost_usd: 0.000005, burst_mb_seconds: 126 }
  const burst = rowsFor(cost, 2).rows.find(r => r.key === 'burst')
  assert.deepEqual(burst, {
    key: 'burst', label: 'Burst', qualifier: 'above 2 GB baseline',
    rate: '0.123 GB·s × $0.0000037 + 0.062 vCPU·s × $0.0000277', amount: '$0.000005',
  })
})

test('no repeated parentheticals, no Subtotal, no emoji', () => {
  const cost = { ...ARM_COST, burst_cost_usd: 0.000005, burst_mb_seconds: 126 }
  for (const r of rowsFor(cost, 2).rows) {
    const text = `${r.label} ${r.qualifier}`
    assert.doesNotMatch(text, /\((compute|snapshot storage|snapshot read)/)
    assert.doesNotMatch(text, /snapshot read ·|snapshot write ·|Subtotal/)
    assert.doesNotMatch(`${text} ${r.rate}`, /[\u{1F300}-\u{1FAFF}\u26A1]/u)
  }
})

test('singular counts and no cost', () => {
  const { rows } = rowsFor({ ...ARM_COST, resume_count: 1, suspend_count: 1 }, 2)
  assert.equal(rows[1].qualifier, '1 (launch/resume)')
  assert.equal(rows[3].qualifier, '1 (suspend)')
  assert.deepEqual(vmCostRows(null, { memGb: 2, rateLabels: costRateLabels(null), formatDuration }), { rows: [], total: null })
})

const burstRow = (extra, gb = 2) =>
  rowsFor({ ...ARM_COST, burst_cost_usd: 0.000175, burst_mb_seconds: 8663, ...extra }, gb).rows.find(r => r.key === 'burst')
test('burst row shows time, peak and one episode', () => {
  const r = burstRow({ burst_seconds: 4.1, burst_peak_over_mb: 2150, burst_episodes: 1 })
  assert.equal(r.qualifier, '4.1 s above 2 GB baseline · peak +2.1 GB · 1 episode')
  assert.equal(r.label, 'Burst')
  assert.equal(r.amount, '$0.000175')
})
test('burst row: plural episodes and a duration of a minute or more', () => {
  const r = burstRow({ burst_seconds: 65, burst_peak_over_mb: 2150, burst_episodes: 3 })
  assert.equal(r.qualifier, '1 min 5 s above 2 GB baseline · peak +2.1 GB · 3 episodes')
})
test('burst row: unknown time keeps the old text (no 0 s, no guess)', () => {
  for (const extra of [{}, { burst_seconds: 0 }, { burst_seconds: null }, { burst_seconds: undefined }]) {
    assert.equal(burstRow(extra).qualifier, 'above 2 GB baseline')
  }
})
test('burst row: time known without peak or episodes', () => {
  assert.equal(burstRow({ burst_seconds: 0.5 }).qualifier, '500 ms above 2 GB baseline')
  assert.equal(burstRow({ burst_seconds: 4.1, burst_episodes: 0 }).qualifier, '4.1 s above 2 GB baseline')
})
test('burstQualifier: peak/episodes can show without a time', () => {
  assert.equal(burstQualifier({ burst_peak_over_mb: 1024, burst_episodes: 2 }, '4'),
    'above 4 GB baseline · peak +1 GB · 2 episodes')
  assert.equal(burstQualifier(null, '2'), 'above 2 GB baseline')
})
