// Unit tests for services/restoreChoice.js (restore on a different instance type).
// Run with: node --test web/src/services/__tests__/restoreChoice.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  RESTORE_MEMORY_FACTOR, RESTORE_WARN_RATIO, RESTORE_STRONG_RATIO, RESTORE_ARCH_WARNING,
  restoreMemoryWarning, restoreArchWarning, restoreChoiceState, restoreOverridePayload,
  restoreBannerText, memoryLabel, nearestTier, tierLabel, specText,
} from '../restoreChoice.js'
import { CLEAR_RULES, shouldClear } from '../actionBanner.js'

// A checkpoint whose need (size x factor) is `pct` percent of `tierMiB`.
const kbFor = (pct, tierMiB) => (pct / 100) * tierMiB / RESTORE_MEMORY_FACTOR * 1024

test('thresholds are the documented constants', () => {
  assert.equal(RESTORE_MEMORY_FACTOR, 2)
  assert.equal(RESTORE_WARN_RATIO, 0.5)
  assert.equal(RESTORE_STRONG_RATIO, 0.9)
})

test('memory warning boundaries at 49 / 50 / 90 / 91 percent of a smaller tier', () => {
  const at = (pct) => restoreMemoryWarning({ checkpointKb: kbFor(pct, 1024), savedMemoryMiB: 2048, memoryMiB: 1024 }).level
  assert.equal(at(49), 'none')
  assert.equal(at(50), 'warn')
  assert.equal(at(90), 'warn')
  assert.equal(at(91), 'strong')
})

test('memory warning text names the size and the tier', () => {
  const w = restoreMemoryWarning({ checkpointKb: kbFor(60, 1024), savedMemoryMiB: 2048, memoryMiB: 1024 })
  assert.match(w.message, /1 GB/)
  assert.match(w.message, /307 MB/)
  const s = restoreMemoryWarning({ checkpointKb: kbFor(95, 1024), savedMemoryMiB: 2048, memoryMiB: 1024 })
  assert.match(s.message, /may run out of memory/)
})

test('the rule applies to the chosen tier whatever the saved tier: a big checkpoint warns on a larger or equal tier, a small one never', () => {
  const big = kbFor(200, 2048) // needs 8192 MiB
  assert.equal(restoreMemoryWarning({ checkpointKb: big, savedMemoryMiB: 2048, memoryMiB: 4096 }).level, 'strong')
  assert.equal(restoreMemoryWarning({ checkpointKb: big, savedMemoryMiB: 2048, memoryMiB: 2048 }).level, 'strong')
  const small = kbFor(20, 1024) // needs about 205 MiB
  for (const memoryMiB of [1024, 2048, 4096, 8192]) {
    for (const savedMemoryMiB of [1024, 2048, 8192]) {
      assert.equal(restoreMemoryWarning({ checkpointKb: small, savedMemoryMiB, memoryMiB }).level, 'none')
    }
  }
})
// Absolute sizes (KB as in metadata.json checkpoint_total_kb); need = size x 2.
const MB = 1024
const GB = 1024 * 1024
const ABSOLUTE_CASES = [
  // [label, checkpointKb, tier MiB, level]
  ['1.5 GB into 1 GB', 1.5 * GB, 1024, 'strong'], // 3072 / 1024 = 3.0
  ['600 MB into 1 GB', 600 * MB, 1024, 'strong'], // 1200 / 1024 = 1.17
  ['300 MB into 1 GB', 300 * MB, 1024, 'warn'], // 600 / 1024 = 0.59
  ['200 MB into 1 GB', 200 * MB, 1024, 'none'], // 400 / 1024 = 0.39
  ['1.5 GB into 2 GB', 1.5 * GB, 2048, 'strong'], // 3072 / 2048 = 1.5
  ['1.5 GB into 4 GB', 1.5 * GB, 4096, 'warn'], // 3072 / 4096 = 0.75
  ['1.5 GB into 8 GB', 1.5 * GB, 8192, 'none'], // 3072 / 8192 = 0.375
]
test('absolute sizes: the same level for the default restore, a smaller and a larger tier', () => {
  assert.equal(1.5 * GB, 1572864)
  for (const [label, checkpointKb, memoryMiB, level] of ABSOLUTE_CASES) {
    for (const savedMemoryMiB of [512, memoryMiB, 16384]) {
      const got = restoreMemoryWarning({ checkpointKb, savedMemoryMiB, memoryMiB }).level
      assert.equal(got, level, `${label} (saved ${savedMemoryMiB} MiB)`)
    }
  }
})
test('absolute sizes: the default restore of a 1.5 GB checkpoint saved at 1 GB shows a strong warning', () => {
  const s = restoreChoiceState({ savedArch: 'x86_64', savedMemoryMiB: 1024, checkpointKb: 1572864 })
  assert.equal(s.changed, false)
  assert.equal(s.tone, 'red')
  assert.equal(s.showNotice, true)
  assert.deepEqual(s.warnings.map(w => [w.kind, w.level]), [['memory', 'strong']])
  assert.match(s.warnings[0].message, /1\.5 GB/)
  assert.match(s.warnings[0].message, /1 GB can hold/)
  assert.deepEqual(restoreOverridePayload(s), {})
  // An override from the saved 2048 down to 1024 gives the same strong warning.
  const o = restoreChoiceState({ savedArch: 'x86_64', savedMemoryMiB: 2048, memoryMiB: 1024, checkpointKb: 1572864 })
  assert.equal(o.tone, 'red')
  assert.equal(o.warnings.find(w => w.kind === 'memory')?.level, 'strong')
})
test('absolute sizes: an unknown checkpoint size gives no memory warning at any tier', () => {
  for (const checkpointKb of [null, undefined, 0, -5, 'x', NaN]) {
    for (const memoryMiB of [512, 1024, 2048]) {
      assert.equal(restoreMemoryWarning({ checkpointKb, savedMemoryMiB: 1024, memoryMiB }).level, 'none')
    }
  }
})

test('an unknown checkpoint size gives no memory warning', () => {
  for (const checkpointKb of [null, undefined, 0, 'x']) {
    assert.equal(restoreMemoryWarning({ checkpointKb, savedMemoryMiB: 4096, memoryMiB: 512 }).level, 'none')
  }
})

test('arch change warning text', () => {
  assert.equal(restoreArchWarning('arm64', 'arm64'), '')
  assert.equal(restoreArchWarning('arm64', 'x86_64'), RESTORE_ARCH_WARNING)
  assert.equal(RESTORE_ARCH_WARNING, 'Packages are re-installed for the new architecture; some may not have a build for it. After the restore you will see which ones could not be installed')
})

test('default choice: saved values, default button; no warning for a small checkpoint, a memory warning for a big one', () => {
  const s = restoreChoiceState({ savedArch: 'arm64', savedMemoryMiB: 2048, checkpointKb: kbFor(30, 2048) })
  assert.equal(s.changed, false)
  assert.equal(s.arch, 'arm64')
  assert.equal(s.memoryMiB, 2048)
  assert.equal(s.tone, 'neutral')
  assert.deepEqual(s.warnings, [])
  assert.equal(s.showNotice, false)
  assert.equal(s.buttonText, 'Restore session on a new VM')
  assert.equal(s.summaryText, 'Restores on the instance type the session was saved on.')
  assert.deepEqual(restoreOverridePayload(s), {})
  // A checkpoint whose need is 95 percent of the saved tier: strong warning on the default too.
  const big = restoreChoiceState({ savedArch: 'arm64', savedMemoryMiB: 2048, checkpointKb: kbFor(95, 2048) })
  assert.equal(big.changed, false)
  assert.equal(big.tone, 'red')
  assert.equal(big.showNotice, true)
  assert.deepEqual(big.warnings.map(w => w.kind), ['memory'])
  assert.equal(big.buttonText, 'Restore session on a new VM')
  assert.deepEqual(restoreOverridePayload(big), {})
  // Same values picked explicitly are still the default.
  const same = restoreChoiceState({ savedArch: 'arm64', savedMemoryMiB: 2048, arch: 'arm64', memoryMiB: 2048 })
  assert.deepEqual(restoreOverridePayload(same), {})
})

test('changed arch and tier: button, summary, amber tone, payload', () => {
  const s = restoreChoiceState({ savedArch: 'arm64', savedMemoryMiB: 2048, arch: 'x86_64', memoryMiB: 4096 })
  assert.equal(s.changed, true)
  assert.equal(s.archChanged, true)
  assert.equal(s.memoryChanged, true)
  assert.equal(s.tone, 'amber')
  assert.equal(s.buttonText, 'Restore on x86_64 · 🧠 4.0 GB')
  assert.equal(s.summaryText, 'Restores on x86_64 / 4 GB instead of the saved ARM64 / 2 GB.')
  assert.deepEqual(s.warnings.map(w => w.kind), ['arch'])
  assert.deepEqual(restoreOverridePayload(s), { restoreOverride: true, arch: 'x86_64', memoryMiB: 4096 })
})

test('larger tier only: changed, neutral, no warning for a checkpoint that fits', () => {
  const s = restoreChoiceState({ savedArch: 'arm64', savedMemoryMiB: 2048, memoryMiB: 8192, checkpointKb: kbFor(30, 2048) })
  assert.equal(s.changed, true)
  assert.equal(s.archChanged, false)
  assert.equal(s.tone, 'neutral')
  assert.deepEqual(s.warnings, [])
  assert.equal(s.buttonText, 'Restore on ARM64 · 🧠 8.0 GB')
  assert.deepEqual(restoreOverridePayload(s), { restoreOverride: true, arch: 'arm64', memoryMiB: 8192 })
})

test('smaller tier: amber for a warning, red for a strong warning', () => {
  const warn = restoreChoiceState({ savedArch: 'x86_64', savedMemoryMiB: 2048, memoryMiB: 1024, checkpointKb: kbFor(60, 1024) })
  assert.equal(warn.tone, 'amber')
  assert.equal(warn.buttonText, 'Restore on x86_64 · 🧠 1.0 GB')
  const strong = restoreChoiceState({ savedArch: 'x86_64', savedMemoryMiB: 2048, memoryMiB: 512, checkpointKb: kbFor(95, 512) })
  assert.equal(strong.tone, 'red')
  assert.equal(strong.buttonText, 'Restore on x86_64 · 🧠 0.5 GB')
})

test('memoryLabel uses one decimal only when needed', () => {
  assert.equal(memoryLabel(4096), '4 GB')
  assert.equal(memoryLabel(512), '0.5 GB')
  assert.equal(memoryLabel(1536), '1.5 GB')
  assert.equal(memoryLabel(null), '')
})

test('tierLabel matches the proxy /image-tiers label exactly (memory and vCPU glyphs)', () => {
  // proxy/platform/routes/metrics.py _tier: f"\U0001f9e0 {mem / 1024:.1f} GB \u00b7 \u26a1 {vcpu} vCPU"
  assert.equal(tierLabel(1024), '\u{1F9E0} 1.0 GB \u00b7 \u26A1 0.5 vCPU')
  assert.equal(tierLabel(2048), '🧠 2.0 GB · ⚡ 1.0 vCPU')
  assert.equal(tierLabel('4096'), '🧠 4.0 GB · ⚡ 2.0 vCPU')
  assert.equal(tierLabel(8192), '🧠 8.0 GB · ⚡ 4.0 vCPU')
  assert.equal(tierLabel(512), '🧠 0.5 GB · ⚡ 0.25 vCPU')
  assert.equal(tierLabel(1536), '🧠 1.5 GB · ⚡ 0.75 vCPU')
  assert.equal(tierLabel(null), '')
  assert.equal(tierLabel(0), '')
  assert.equal(tierLabel('x'), '')
  // The memory part only (footer button).
  assert.equal(tierLabel(1024, { vcpu: false }), '🧠 1.0 GB')
  assert.equal(tierLabel(null, { vcpu: false }), '')
})

test('specText: tier glyph label then the arch; a missing arch reads unknown, never ARM64', () => {
  assert.equal(specText('arm64', 2048), '🧠 2.0 GB · ⚡ 1.0 vCPU · ARM64')
  assert.equal(specText('x86_64', 1024), '🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64')
  assert.equal(specText('', 1024), '🧠 1.0 GB · ⚡ 0.5 vCPU · unknown')
  assert.equal(specText('arm64', null), 'ARM64')
})

test('nearestTier keeps an offered tier and snaps otherwise', () => {
  assert.equal(nearestTier([1024, 2048, 4096], 2048), 2048)
  assert.equal(nearestTier([1024, 4096], 3500), 4096)
  assert.equal(nearestTier([], 2048), null)
})

test('banner: failed packages, plural and singular', () => {
  const two = restoreBannerText({
    verified: true, arch: 'x86_64', packages_total: 5,
    packages_failed: [{ name: 'a', reason: 'r1' }, { name: 'b', reason: 'r2' }],
  })
  assert.deepEqual(two, {
    kind: 'restore-report', tone: 'error', lead: 'Restored on x86_64.',
    detail: '2 packages could not be installed: a, b.', action: 'Open Packages',
  })
  const one = restoreBannerText({ verified: true, packages_total: 3, packages_failed: [{ name: 'a', reason: '' }] }, { arch: 'arm64' })
  assert.equal(one.lead, 'Restored on ARM64.')
  assert.equal(one.detail, '1 package could not be installed: a.')
})

test('banner: all packages re-installed', () => {
  const ok = restoreBannerText({ verified: true, arch: 'x86_64', packages_total: 4, packages_failed: [] })
  assert.equal(ok.tone, 'success')
  assert.equal(`${ok.lead} ${ok.detail}`, 'Restored on x86_64, all 4 packages re-installed.')
  assert.equal(ok.action, '')
  const single = restoreBannerText({ verified: true, arch: 'x86_64', packages_total: 1, packages_failed: [] })
  assert.equal(`${single.lead} ${single.detail}`, 'Restored on x86_64, the 1 package re-installed.')
})

test('banner: missing or unverified report says not verified', () => {
  for (const report of [null, undefined, { verified: false, reason: 'timeout' }, {}]) {
    const b = restoreBannerText(report, { arch: 'x86_64' })
    assert.equal(b.tone, 'info')
    assert.equal(`${b.lead} ${b.detail}`, 'Restored on x86_64. Packages not verified.')
  }
})

test('banner: the /launch restore_memory_check adds one sentence for warn and strong only', () => {
  const report = { verified: true, arch: 'x86_64', packages_total: 2, packages_failed: [] }
  const strong = restoreBannerText(report, { memoryCheck: { level: 'strong', checkpoint_kb: 1572864, tier_mib: 1024 } })
  assert.equal(strong.tone, 'info')
  assert.equal(`${strong.lead} ${strong.detail}`,
    'Restored on x86_64, all 2 packages re-installed. The saved state is about 1.5 GB, close to or more than 1 GB can hold; it may run out of memory.')
  const warn = restoreBannerText(report, { memoryCheck: { level: 'warn', checkpoint_kb: 307200, tier_mib: 1024 } })
  assert.equal(warn.detail, 'all 2 packages re-installed. The saved state is about 300 MB; on 1 GB little memory is left for new work.')
  for (const memoryCheck of [null, undefined, { level: 'none' }, { level: 'unknown' }]) {
    const b = restoreBannerText(report, { memoryCheck })
    assert.equal(b.tone, 'success')
    assert.equal(b.detail, 'all 2 packages re-installed.')
  }
  // A failed-package banner keeps its error tone and action.
  const failed = restoreBannerText({ verified: true, packages_total: 1, packages_failed: [{ name: 'a' }] },
    { memoryCheck: { level: 'strong', checkpoint_kb: 1572864, tier_mib: 1024 } })
  assert.equal(failed.tone, 'error')
  assert.equal(failed.action, 'Open Packages')
  assert.match(failed.detail, /could not be installed: a\. The saved state is about 1\.5 GB/)
})
test('restore-report banner clears like the sandbox banners', () => {
  assert.ok(CLEAR_RULES['restore-report'])
  assert.equal(shouldClear('restore-report', 'activate-target-tab'), true)
  assert.equal(shouldClear('restore-report', 'dismiss'), true)
  assert.equal(shouldClear('restore-report', 'scroll'), false)
})
