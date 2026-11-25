// Unit tests for services/costRates.js (per-arch rate labels on the VM Cost tab).
// Run with: node --test web/src/services/__tests__/costRates.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ARM64_FALLBACK_RATES, costRateLabels, archLabel } from '../costRates.js'

const ARM = { ...ARM64_FALLBACK_RATES }
const X86 = {
  vcpu_per_sec: 0.0000326557,
  memory_per_gb_sec: 0.0000043235,
  snapshot_per_gb_month: 0.08,
  snapshot_read_per_gb: 0.00155,
  snapshot_write_per_gb: 0.0038,
}

test('costRateLabels: arm64 rates give the strings the Cost tab always showed', () => {
  assert.deepEqual(costRateLabels({ rates: ARM }), {
    vcpu: '0.0000277', memory: '0.0000037', read: '0.00155', write: '0.00380', storage: '0.08',
  })
})

test('costRateLabels: x86_64 rates give the x86 vCPU/memory strings, same snapshot strings', () => {
  assert.deepEqual(costRateLabels({ rates: X86 }), {
    vcpu: '0.0000327', memory: '0.0000043', read: '0.00155', write: '0.00380', storage: '0.08',
  })
})

test('costRateLabels: missing cost/rates falls back to arm64', () => {
  const arm = costRateLabels({ rates: ARM })
  assert.deepEqual(costRateLabels({}), arm)
  assert.deepEqual(costRateLabels(undefined), arm)
  assert.deepEqual(costRateLabels(null), arm)
})

test('costRateLabels: a missing or non-numeric single rate falls back per key', () => {
  const labels = costRateLabels({ rates: { vcpu_per_sec: 0.0000326557, memory_per_gb_sec: 'x' } })
  assert.equal(labels.vcpu, '0.0000327')
  assert.equal(labels.memory, '0.0000037')
  assert.equal(labels.write, '0.00380')
})

test('archLabel: x86_64 vs ARM64; a missing arch is unknown, never ARM64', () => {
  assert.equal(archLabel('x86_64'), 'x86_64')
  assert.equal(archLabel('arm64'), 'ARM64')
  assert.equal(archLabel(undefined), 'unknown')
  assert.equal(archLabel(null), 'unknown')
  assert.equal(archLabel(''), 'unknown')
})
