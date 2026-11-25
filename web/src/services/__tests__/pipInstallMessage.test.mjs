// Unit tests for services/pipInstallMessage.js (other-arch hint after a pip
// install fails for lack of a wheel).
// Run with: node --test web/src/services/__tests__/pipInstallMessage.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { NO_WHEEL_KINDS, otherArchHint } from '../pipInstallMessage.js'

const BOTH = ['arm64', 'x86_64']

test('NO_WHEEL_KINDS lists exactly the no-wheel failure kinds', () => {
  assert.deepEqual(NO_WHEEL_KINDS, ['no_distribution', 'source_build_failed', 'source_build_timeout', 'sdist_download_timeout'])
})

test('version_not_found never adds the arch hint (pin missing, other versions exist)', () => {
  for (const arch of ['arm64', 'x86_64']) {
    assert.equal(otherArchHint({ errorKind: 'version_not_found', arch, offeredArches: BOTH }), '')
  }
})

test('sdist_download_timeout gets the hint (pip found no wheel for this arch)', () => {
  assert.equal(
    otherArchHint({ errorKind: 'sdist_download_timeout', arch: 'x86_64', offeredArches: BOTH }),
    'Running the notebook on ARM64 may help: it may have a prebuilt wheel.',
  )
})

test('unknown kind degrades to no hint; older VM no_distribution keeps it', () => {
  assert.equal(otherArchHint({ errorKind: 'some_future_kind', arch: 'arm64', offeredArches: BOTH }), '')
  assert.equal(
    otherArchHint({ errorKind: 'no_distribution', arch: 'arm64', offeredArches: BOTH }),
    'Running the notebook on x86_64 may help: it may have a prebuilt wheel.',
  )
})

test('arm64 failure, x86_64 offered → suggests x86_64', () => {
  for (const errorKind of NO_WHEEL_KINDS) {
    assert.equal(
      otherArchHint({ errorKind, arch: 'arm64', offeredArches: BOTH }),
      'Running the notebook on x86_64 may help: it may have a prebuilt wheel.',
    )
  }
})

test('x86_64 failure, arm64 offered → suggests ARM64', () => {
  assert.equal(
    otherArchHint({ errorKind: 'source_build_timeout', arch: 'x86_64', offeredArches: BOTH }),
    'Running the notebook on ARM64 may help: it may have a prebuilt wheel.',
  )
})

test('no hint when the other arch is not offered', () => {
  assert.equal(otherArchHint({ errorKind: 'no_distribution', arch: 'arm64', offeredArches: ['arm64'] }), '')
  assert.equal(otherArchHint({ errorKind: 'no_distribution', arch: 'x86_64', offeredArches: ['x86_64'] }), '')
  assert.equal(otherArchHint({ errorKind: 'no_distribution', arch: 'arm64', offeredArches: [] }), '')
  assert.equal(otherArchHint({ errorKind: 'no_distribution', arch: 'arm64' }), '')
})

test('no hint for kinds unrelated to wheels', () => {
  for (const errorKind of ['download_timeout', 'timeout', 'other', 'ok', '', undefined]) {
    assert.equal(otherArchHint({ errorKind, arch: 'arm64', offeredArches: BOTH }), '')
  }
})

test('no hint for an unknown or missing arch', () => {
  assert.equal(otherArchHint({ errorKind: 'no_distribution', arch: 'riscv64', offeredArches: BOTH }), '')
  assert.equal(otherArchHint({ errorKind: 'no_distribution', offeredArches: BOTH }), '')
  assert.equal(otherArchHint(), '')
})

test('hint never claims the other arch "has" a wheel', () => {
  const h = otherArchHint({ errorKind: 'no_distribution', arch: 'arm64', offeredArches: BOTH })
  assert.ok(!/has a prebuilt wheel/.test(h))
  assert.ok(/may have a prebuilt wheel/.test(h))
})
