// Unit tests for services/vmSort.js (Live MicroVMs panel sort).
// Run with: node --test web/src/services/__tests__/vmSort.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  VM_SORT_OPTIONS, DEFAULT_VM_SORT, VM_SORT_STORAGE_KEY,
  parseStoredVmSort, archKey, makeVmComparator,
} from '../vmSort.js'

// [id, inst] entries like Object.entries(instances).
const E = (id, inst) => [id, inst]
const ids = (entries, key) => [...entries].sort(makeVmComparator(key)).map(e => e[0])

const A = E('a', { launched_at: 100, memory_mib: 2048, state: 'SUSPENDED', architecture: 'arm64' })
const B = E('b', { launched_at: 300, memory_mib: 4096, state: 'RUNNING', architecture: 'x86_64' })
const C = E('c', { launched_at: '1970-01-01T00:03:20Z', memory_mib: 1024, state: 'PENDING' }) // 200s, no arch
const D = E('d', { launched_at: 50, memory_mib: 8192, state: 'RUNNING', architecture: 'x86_64' })
const ALL = [A, B, C, D]

test('VM_SORT_OPTIONS: existing 5 options unchanged, then the two arch options', () => {
  assert.deepEqual(VM_SORT_OPTIONS.map(o => [o.value, o.label]), [
    ['launched-desc', 'Launched (newest first)'],
    ['launched-asc', 'Launched (oldest first)'],
    ['memory-desc', 'Memory (largest first)'],
    ['memory-asc', 'Memory (smallest first)'],
    ['state', 'State'],
    ['arch-arm64', 'Architecture (ARM64 first)'],
    ['arch-x86_64', 'Architecture (x86_64 first)'],
  ])
  assert.equal(DEFAULT_VM_SORT, 'launched-desc')
  assert.equal(VM_SORT_STORAGE_KEY, 'microvm-vm-sort')
})

test('parseStoredVmSort: known values kept, unknown/missing fall back to default', () => {
  for (const o of VM_SORT_OPTIONS) assert.equal(parseStoredVmSort(o.value), o.value)
  assert.equal(parseStoredVmSort(null), 'launched-desc')
  assert.equal(parseStoredVmSort(undefined), 'launched-desc')
  assert.equal(parseStoredVmSort(''), 'launched-desc')
  assert.equal(parseStoredVmSort('arch'), 'launched-desc')
  assert.equal(parseStoredVmSort('ARCH-ARM64'), 'launched-desc')
})

test('archKey: x86_64 only for x86_64, everything else arm64', () => {
  assert.equal(archKey('x86_64'), 'x86_64')
  assert.equal(archKey('arm64'), 'arm64')
  assert.equal(archKey(undefined), 'arm64')
  assert.equal(archKey(null), 'arm64')
  assert.equal(archKey('aarch64'), 'arm64')
})

test('launched-desc (and unknown key): newest first, ISO and epoch mixed', () => {
  assert.deepEqual(ids(ALL, 'launched-desc'), ['b', 'c', 'a', 'd'])
  assert.deepEqual(ids(ALL, 'bogus'), ['b', 'c', 'a', 'd'])
})

test('launched-asc: oldest first', () => {
  assert.deepEqual(ids(ALL, 'launched-asc'), ['d', 'a', 'c', 'b'])
})

test('launched: missing launched_at counts as 0', () => {
  const N = E('n', { memory_mib: 2048 })
  assert.deepEqual(ids([A, N], 'launched-desc'), ['a', 'n'])
  assert.deepEqual(ids([A, N], 'launched-asc'), ['n', 'a'])
})

test('memory-desc / memory-asc', () => {
  assert.deepEqual(ids(ALL, 'memory-desc'), ['d', 'b', 'a', 'c'])
  assert.deepEqual(ids(ALL, 'memory-asc'), ['c', 'a', 'b', 'd'])
})

test('state: RUNNING, then SUSPENDED, then others; stable within a bucket', () => {
  assert.deepEqual(ids(ALL, 'state'), ['b', 'd', 'a', 'c'])
})

test('arch-arm64: arm64 (incl. missing arch) first, then memory desc', () => {
  assert.deepEqual(ids(ALL, 'arch-arm64'), ['a', 'c', 'd', 'b'])
})

test('arch-x86_64: x86_64 first, then memory desc', () => {
  assert.deepEqual(ids(ALL, 'arch-x86_64'), ['d', 'b', 'a', 'c'])
})

test('arch sort: same arch + same memory → launched desc (tertiary)', () => {
  const X1 = E('x1', { launched_at: 10, memory_mib: 2048, architecture: 'x86_64' })
  const X2 = E('x2', { launched_at: 30, memory_mib: 2048, architecture: 'x86_64' })
  const X3 = E('x3', { launched_at: 20, memory_mib: 4096, architecture: 'x86_64' })
  const R1 = E('r1', { launched_at: 40, memory_mib: 2048 })
  const R2 = E('r2', { launched_at: 50, memory_mib: 2048, architecture: 'arm64' })
  assert.deepEqual(ids([X1, R1, X2, R2, X3], 'arch-x86_64'), ['x3', 'x2', 'x1', 'r2', 'r1'])
  assert.deepEqual(ids([X1, R1, X2, R2, X3], 'arch-arm64'), ['r2', 'r1', 'x3', 'x2', 'x1'])
})
