// Unit tests for services/vmCardState.js (Live MicroVMs card: State band, timing, Runtime).
// Run with: node --test web/src/services/__tests__/vmCardState.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  toEpochSec, formatAgo, formatClock, formatCountdown, countdownLevel, countdownSuffix,
  formatBytes, formatGbFromMb, checkpointRows, suspendSaveRow, memorySummary, idleSuspendLabel, lifecycleOrigin, lifecycleCounters,
  shortDuration, packagesSummary,
  imageLabel, imageNameFromArn, formatShortDateTime, formatVersion, formatClockSec, formatClockShort,
} from '../vmCardState.js'

const NOW_MS = Date.parse('2026-11-12T10:00:00Z')
const NOW = NOW_MS / 1000

test('toEpochSec: numbers, ISO strings, junk', () => {
  assert.equal(toEpochSec(NOW), NOW)
  assert.equal(toEpochSec('2026-11-12T10:00:00Z'), NOW)
  assert.equal(toEpochSec(null), null)
  assert.equal(toEpochSec(''), null)
  assert.equal(toEpochSec('nope'), null)
  assert.equal(toEpochSec(0), null)
  assert.equal(toEpochSec(NaN), null)
})

test('formatAgo: boundaries and units', () => {
  assert.equal(formatAgo(NOW - 10, NOW_MS), 'just now')
  assert.equal(formatAgo(NOW - 44, NOW_MS), 'just now')
  assert.equal(formatAgo(NOW - 60, NOW_MS), '1 min ago')
  assert.equal(formatAgo(NOW - 240, NOW_MS), '4 min ago')
  assert.equal(formatAgo(NOW - (2 * 3600 + 5 * 60), NOW_MS), '2 h 5 min ago')
  assert.equal(formatAgo(NOW - 2 * 3600, NOW_MS), '2 h ago')
  assert.equal(formatAgo(NOW - 86400, NOW_MS), '1 day ago')
  assert.equal(formatAgo(NOW - 3 * 86400, NOW_MS), '3 days ago')
  assert.equal(formatAgo(NOW + 100, NOW_MS), 'just now')  // clock skew → never negative
})

test('formatAgo: ISO input and nulls', () => {
  assert.equal(formatAgo('2026-11-12T09:56:00Z', NOW_MS), '4 min ago')
  assert.equal(formatAgo(null, NOW_MS), null)
  assert.equal(formatAgo('garbage', NOW_MS), null)
})

test('formatClock / formatShortDateTime: same toLocale* call; null for unknown', () => {
  const t = NOW + 6 * 3600
  assert.equal(formatClock(t), new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
  assert.equal(formatClock(null), null)
  assert.equal(formatShortDateTime('2026-11-12T01:00:00Z'), new Date(Date.parse('2026-11-12T01:00:00Z'))
    .toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))
})

test('formatCountdown: h m above an hour, m ss below, expired', () => {
  assert.equal(formatCountdown(6 * 3600 + 32 * 60 + 10), '6h 32m')
  assert.equal(formatCountdown(3600), '1h 0m')
  assert.equal(formatCountdown(3599), '59m 59s')
  assert.equal(formatCountdown(245), '4m 05s')
  assert.equal(formatCountdown(0), 'expired')
  assert.equal(formatCountdown(-5), 'expired')
  assert.equal(formatCountdown(null), null)
})

test('countdownLevel: 599/600/1799/1800 boundaries', () => {
  assert.equal(countdownLevel(599), 'critical')
  assert.equal(countdownLevel(600), 'warning')
  assert.equal(countdownLevel(1799), 'warning')
  assert.equal(countdownLevel(1800), '')
  assert.equal(countdownLevel(0), 'critical')
  assert.equal(countdownLevel(null), '')
})

test('countdownSuffix: text goes with the colour', () => {
  assert.equal(countdownSuffix('warning'), ' · ending soon')
  assert.equal(countdownSuffix('critical'), ' · ending very soon')
  assert.equal(countdownSuffix(''), '')
})

test('formatBytes / formatGbFromMb', () => {
  assert.equal(formatBytes(800 * 1024), '800 KB')
  assert.equal(formatBytes(12.3 * 1024 * 1024), '12.3 MB')
  assert.equal(formatBytes(410 * 1024 * 1024), '410 MB')
  assert.equal(formatBytes(1.2 * 1024 ** 3), '1.2 GB')
  assert.equal(formatBytes(2 * 1024 ** 3), '2 GB')
  assert.equal(formatBytes(500), '500 B')
  assert.equal(formatBytes(null), null)
  assert.equal(formatBytes(-1), null)
  assert.equal(formatGbFromMb(1638.4), '1.6')
  assert.equal(formatGbFromMb(2048), '2')
  assert.equal(formatGbFromMb(null), null)
})

test('checkpointRows: one fact per row, partial, none, null', () => {
  const cp = { saved_at: '2026-11-12T09:56:00Z', vars: 46, size_kb: 12595.2 }
  assert.deepEqual(checkpointRows(cp, NOW_MS), { saved: '4 min ago', variables: '46', size: '12.3 MB' })
  assert.deepEqual(checkpointRows({ saved_at: '2026-11-12T09:56:00Z', vars: 1 }, NOW_MS), { saved: '4 min ago', variables: '1', size: null })
  assert.deepEqual(checkpointRows({ saved_at: '2026-11-12T09:56:00Z' }, NOW_MS), { saved: '4 min ago', variables: null, size: null })
  assert.deepEqual(checkpointRows({ saved_at: null }, NOW_MS), { saved: 'Not yet', variables: null, size: null })
  assert.equal(checkpointRows(null, NOW_MS), null)
})
test('suspendSaveRow / checkpointRows: last suspend save outcome', () => {
  assert.equal(suspendSaveRow(null), null)
  assert.equal(suspendSaveRow({ status: 'ok', reason: 'no changes' }), null)
  assert.deepEqual(suspendSaveRow({ status: 'timed_out', reason: 'budget exceeded', duration_s: 45.4 }),
    { suspendSave: 'Timed out after 45 s, not saved', suspendSaveDetail: 'budget exceeded' })
  assert.deepEqual(suspendSaveRow({ status: 'timed_out', reason: '' }),
    { suspendSave: 'Timed out, not saved', suspendSaveDetail: 'Timed out, not saved' })
  assert.deepEqual(suspendSaveRow({ status: 'skipped', reason: "big: not enough memory to save 'big'" }),
    { suspendSave: "Skipped: big: not enough memory to save 'big'", suspendSaveDetail: "big: not enough memory to save 'big'" })
  assert.deepEqual(suspendSaveRow({ status: 'failed', reason: 'the notebook ran out of memory while saving' }),
    { suspendSave: 'Failed: the notebook ran out of memory while saving',
      suspendSaveDetail: 'the notebook ran out of memory while saving' })
  const sus = { status: 'timed_out', reason: 'r', duration_s: 45 }
  assert.deepEqual(checkpointRows({ saved_at: null, last_suspend_save: sus }, NOW_MS),
    { saved: 'Not yet', variables: null, size: null, suspendSave: 'Timed out after 45 s, not saved', suspendSaveDetail: 'r' })
  assert.deepEqual(checkpointRows({ saved_at: '2026-11-12T09:56:00Z', vars: 1, last_suspend_save: { status: 'ok' } }, NOW_MS),
    { saved: '4 min ago', variables: '1', size: null })
})

test('checkpointRows: the screenshot rows (whole saved state size)', () => {
  const cp = { saved_at: '2026-11-12T09:59:00Z', vars: 88, size_kb: 16793.6 }
  assert.deepEqual(checkpointRows(cp, NOW_MS), { saved: '1 min ago', variables: '88', size: '16.4 MB' })
})
test('checkpointRows: older checkpoint total is labelled saved objects (older checkpoint)', () => {
  const cp = { saved_at: '2026-11-12T09:56:00Z', vars: 186, vars_approx: true, size_kb: null }
  assert.equal(checkpointRows(cp, NOW_MS).variables, '~186 saved objects (older checkpoint)')
  assert.equal(checkpointRows({ ...cp, vars: 1 }, NOW_MS).variables, '~1 saved object (older checkpoint)')
  assert.equal(checkpointRows(cp, NOW_MS).size, null)
})
test('checkpointRows: a 0 / missing / non-number size is null (never "0 B")', () => {
  for (const size_kb of [0, null, undefined, -1, NaN]) {
    assert.equal(checkpointRows({ saved_at: '2026-11-12T09:56:00Z', vars: 3, size_kb }, NOW_MS).size, null)
  }
})

test('idleSuspendLabel: the idle-timeout setting as "after … idle"', () => {
  assert.equal(idleSuspendLabel(60), 'after 1m idle')
  assert.equal(idleSuspendLabel(30), 'after 30s idle')
  assert.equal(idleSuspendLabel(90), 'after 1m 30s idle')
  assert.equal(idleSuspendLabel(3600), 'after 1h idle')
  assert.equal(idleSuspendLabel(3900), 'after 1h 5m idle')
  assert.equal(idleSuspendLabel(0), null)
  assert.equal(idleSuspendLabel(null), null)
  assert.equal(idleSuspendLabel(-5), null)
})

test('lifecycleOrigin: Source and Restored from, one fact each', () => {
  const at = '2026-11-12T08:43:00Z'
  const when = formatShortDateTime(at)
  assert.deepEqual(lifecycleOrigin({ source: 'pool', restored: true, restored_checkpoint_at: at }),
    { source: 'Warm pool', restoredFrom: `Checkpoint · ${when}` })
  assert.deepEqual(lifecycleOrigin({ source: 'fresh', restored: true, restored_checkpoint_at: null }),
    { source: 'New VM', restoredFrom: 'Checkpoint' })
  assert.deepEqual(lifecycleOrigin({ source: 'pool', restored: false }), { source: 'Warm pool', restoredFrom: null })
  assert.deepEqual(lifecycleOrigin({ source: 'fresh', restored: false }), { source: 'New VM', restoredFrom: null })
  assert.deepEqual(lifecycleOrigin({ source: null, restored: false }), { source: null, restoredFrom: null })
  assert.equal(lifecycleOrigin(null), null)
})

test('lifecycleCounters: durations + counts; zero/unknown → null (row omitted)', () => {
  assert.deepEqual(lifecycleCounters({ running_secs: 900, suspended_secs: 38 * 60, resume_count: 5, suspend_count: 6 }),
    { running: '15m', suspended: '38m', resumes: '5', suspends: '6' })
  assert.deepEqual(lifecycleCounters({ running_secs: 3600 + 51 * 60, suspended_secs: 0, resume_count: 0, suspend_count: 1 }),
    { running: '1h 51m', suspended: null, resumes: null, suspends: '1' })
  assert.deepEqual(lifecycleCounters(null), { running: null, suspended: null, resumes: null, suspends: null })
})

test('memorySummary: peak + episodes, burst without usage, nothing', () => {
  assert.equal(memorySummary({ peak_mem_mb: 1638.4, memory_mb: 2048, burst_episodes: 3 }), 'Peak 1.6 of 2 GB · burst used 3×')
  assert.equal(memorySummary({ peak_mem_mb: 1638.4, memory_mb: 4096, burst_episodes: 0 }), 'Peak 1.6 of 4 GB')
  assert.equal(memorySummary(null, { burst_mb_seconds: 12 }), 'burst used')
  assert.equal(memorySummary(null, { burst_mb_seconds: 0 }), null)
  assert.equal(memorySummary(null, null), null)
})

test('shortDuration', () => {
  assert.equal(shortDuration(30), '30s')
  assert.equal(shortDuration(45 * 60), '45m')
  assert.equal(shortDuration(5 * 3600), '5h')
  assert.equal(shortDuration(3600 + 51 * 60), '1h 51m')
  assert.equal(shortDuration(null), null)
})

test('packagesSummary: shown + more, total larger than the list, empty', () => {
  assert.deepEqual(packagesSummary(['a', 'b', 'c', 'd', 'e'], 7), { shown: ['a', 'b', 'c'], more: 4 })
  assert.deepEqual(packagesSummary(['a', 'b'], 2), { shown: ['a', 'b'], more: 0 })
  assert.deepEqual(packagesSummary(['a', 'b', 'c', 'd'], undefined, 2), { shown: ['a', 'b'], more: 2 })
  assert.deepEqual(packagesSummary(null, 0), { shown: [], more: 0 })
})

test('imageLabel / imageNameFromArn', () => {
  assert.equal(imageLabel({ name: 'notebook-microvm-arm64-2048', version: '3' }), 'notebook-microvm-arm64-2048 · v3')
  assert.equal(imageLabel({ name: 'notebook-microvm-arm64-2048', version: null }), 'notebook-microvm-arm64-2048')
  assert.equal(imageLabel({ name: 'notebook-microvm-arm64-2048', version: '1.0' }), 'notebook-microvm-arm64-2048 · v1')
  assert.equal(imageLabel({ name: 'n', version: '3.1' }), 'n · v3.1')
  assert.equal(imageLabel(null), null)
  assert.equal(imageNameFromArn('arn:aws:lambda:us-east-1:1:microvm-image:notebook-microvm-arm64-2048'), 'notebook-microvm-arm64-2048')
  assert.equal(imageNameFromArn('arn:x:y/notebook-microvm-x86_64-4096'), 'notebook-microvm-x86_64-4096')
  assert.equal(imageNameFromArn(null), null)
})

test('formatVersion: drop trailing .0 parts only', () => {
  assert.equal(formatVersion('1.0'), '1')
  assert.equal(formatVersion('2.0.0'), '2')
  assert.equal(formatVersion('3.1'), '3.1')
  assert.equal(formatVersion('3.10'), '3.10')
  assert.equal(formatVersion('1.0.1'), '1.0.1')
  assert.equal(formatVersion('3'), '3')
  assert.equal(formatVersion(4), '4')
  assert.equal(formatVersion('beta'), 'beta')
  assert.equal(formatVersion(null), null)
  assert.equal(formatVersion(''), null)
})

test('formatClockSec: same toLocaleTimeString options plus seconds; null for unknown', () => {
  const t = 1_800_000_012
  assert.equal(formatClockSec(t), new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }))
  assert.equal(formatClockSec(null), null)
})

test('formatClockShort: no leading zero, no AM/PM; null for unknown', () => {
  const t = 1_800_000_012
  const s = formatClockShort(t)
  assert.match(s, /^\d{1,2}[:.]\d\d$/)
  assert.ok(!/AM|PM/i.test(s), s)
  assert.ok(formatClock(t).includes(s.slice(-2)))
  assert.equal(formatClockShort(null), null)
})
