// Pure formatting helpers for the Live MicroVMs card (MicroVMsPanel.jsx): the
// Overview Lifecycle section (one fact per row: started, source, restored from,
// running/suspended time, resumes/suspends, auto-suspend, expiry) and
// Checkpoint rows, and the Resources tab's Environment section. No React or
// config imports, so node --test can load it.
//
// Every helper returns null when it has nothing to show, so the panel can hide the
// row. Colour states always come with text (countdownSuffix) so they are not
// colour-only.

const MIN = 60
const HOUR = 3600
const DAY = 86400

/** Epoch seconds, epoch ms-free number, or ISO string → epoch seconds; else null. */
export function toEpochSec(value) {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? value : null
  if (typeof value === 'string') {
    const ms = Date.parse(value)
    return Number.isFinite(ms) ? ms / 1000 : null
  }
  return null
}

/** 'just now' / '1 min ago' / '4 min ago' / '2 h 5 min ago' / '3 days ago'; null when unknown. */
export function formatAgo(value, nowMs = Date.now()) {
  const sec = toEpochSec(value)
  if (sec === null) return null
  const diff = Math.max(0, nowMs / 1000 - sec)
  if (diff < 45) return 'just now'
  if (diff < HOUR) {
    const m = Math.max(1, Math.round(diff / MIN))
    return m >= 60 ? '1 h ago' : `${m} min ago`
  }
  if (diff < DAY) {
    const h = Math.floor(diff / HOUR)
    const m = Math.floor((diff % HOUR) / MIN)
    return m ? `${h} h ${m} min ago` : `${h} h ago`
  }
  const d = Math.floor(diff / DAY)
  return d === 1 ? '1 day ago' : `${d} days ago`
}

/** Local wall-clock time 'hh:mm' (locale formatted) for an epoch/ISO value; null when unknown. */
export function formatClock(value) {
  const sec = toEpochSec(value)
  if (sec === null) return null
  return new Date(sec * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** Local wall-clock time with seconds 'hh:mm:ss' (same locale options as formatClock); null when unknown. */
export function formatClockSec(value) {
  const sec = toEpochSec(value)
  if (sec === null) return null
  return new Date(sec * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/**
 * Compact local time for tight labels: no leading zero and no AM/PM ('7:12', '19:12'),
 * same locale otherwise; null when unknown. The full time stays in tooltips.
 */
export function formatClockShort(value) {
  const sec = toEpochSec(value)
  if (sec === null) return null
  return new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' })
    .formatToParts(new Date(sec * 1000))
    .filter(p => p.type !== 'dayPeriod')
    .map(p => p.value)
    .join('')
    .trim()
}
/**
 * Version for display: drops trailing '.0' parts only ('1.0' → '1', '2.0.0' → '2'),
 * keeps a real minor part ('3.1', '3.10'). Non-numeric values pass through; null when unknown.
 */
export function formatVersion(v) {
  if (v === null || v === undefined || v === '') return null
  const s = String(v).trim()
  if (!/^\d+(\.\d+)*$/.test(s)) return s
  return s.replace(/(\.0+)+$/, '')
}
/** Remaining seconds → '6h 32m' (≥ 1 h), '4m 05s', or 'expired'. */
export function formatCountdown(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return null
  if (sec <= 0) return 'expired'
  const s = Math.floor(sec)
  if (s >= HOUR) return `${Math.floor(s / HOUR)}h ${Math.floor((s % HOUR) / MIN)}m`
  return `${Math.floor(s / MIN)}m ${String(s % MIN).padStart(2, '0')}s`
}

/** 'critical' under 10 min, 'warning' under 30 min, '' otherwise (or unknown). */
export function countdownLevel(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return ''
  if (sec < 600) return 'critical'
  if (sec < 1800) return 'warning'
  return ''
}

/** Text that goes with the countdown colour, so the state is not colour-only. */
export function countdownSuffix(level) {
  if (level === 'critical') return ' · ending very soon'
  if (level === 'warning') return ' · ending soon'
  return ''
}

/** Bytes → '800 KB' / '12.3 MB' / '410 MB' / '1.2 GB'; null when unknown. */
export function formatBytes(n) {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) return null
  const KB = 1024
  const MB = KB * 1024
  const GB = MB * 1024
  if (n >= GB) return `${trim1(n / GB)} GB`
  if (n >= MB) {
    const v = n / MB
    return v >= 100 ? `${Math.round(v)} MB` : `${trim1(v)} MB`
  }
  if (n >= KB) return `${Math.round(n / KB)} KB`
  return `${Math.round(n)} B`
}

function trim1(v) {
  const r = Math.round(v * 10) / 10
  return Number.isInteger(r) ? String(r) : r.toFixed(1)
}

/** MiB → GB with one decimal, trailing '.0' dropped: 1638.4 → '1.6', 2048 → '2'. */
export function formatGbFromMb(mb) {
  if (mb === null || mb === undefined || !Number.isFinite(mb)) return null
  return trim1(mb / 1024)
}

/** Checkpoint rows (Overview > Checkpoint), one fact per row: { saved, variables, size }.
 *  saved: '4 min ago' / 'Not yet'; variables: '46', or for an older checkpoint
 *  (vars_approx) '~186 saved objects (older checkpoint)'; size: '12.3 MB', the
 *  WHOLE saved state (proxy: checkpoint_total_kb). A missing part is null (the
 *  row is omitted); a 0 / missing size is never shown as "0 B". null without cp. */
/** The "Last suspend save" row from checkpoint.last_suspend_save ({status:
 *  ok|skipped|timed_out|failed, reason, duration_s, ...}): null for a missing or
 *  ok outcome, else { suspendSave: 'Timed out after 45 s, not saved' |
 *  'Skipped: <reason>' | 'Failed: <reason>', suspendSaveDetail: reason }. */
export function suspendSaveRow(sus) {
  if (!sus || typeof sus !== 'object' || !sus.status || sus.status === 'ok') return null
  const reason = typeof sus.reason === 'string' ? sus.reason : ''
  const short = reason.length > 80 ? `${reason.slice(0, 79)}…` : reason
  let text
  if (sus.status === 'timed_out') {
    const secs = Number.isFinite(sus.duration_s) ? ` after ${Math.round(sus.duration_s)} s` : ''
    text = `Timed out${secs}, not saved`
  } else if (sus.status === 'skipped') {
    text = short ? `Skipped: ${short}` : 'Skipped'
  } else {
    text = short ? `Failed: ${short}` : 'Failed'
  }
  return { suspendSave: text, suspendSaveDetail: reason || text }
}

export function checkpointRows(cp, nowMs = Date.now()) {
  if (!cp) return null
  const sus = suspendSaveRow(cp.last_suspend_save) || {}
  if (!cp.saved_at) return { saved: 'Not yet', variables: null, size: null, ...sus }
  const saved = formatAgo(cp.saved_at, nowMs) || formatShortDateTime(cp.saved_at)
  let variables = null
  if (Number.isFinite(cp.vars)) {
    // vars_approx: an older checkpoint that only recorded the whole saved
    // namespace (helpers and _-names included), not the user-variable count.
    variables = cp.vars_approx
      ? `~${cp.vars} saved ${cp.vars === 1 ? 'object' : 'objects'} (older checkpoint)`
      : String(cp.vars)
  }
  const size = Number.isFinite(cp.size_kb) && cp.size_kb > 0 ? formatBytes(cp.size_kb * 1024) : null
  // Variables that save left out (proxy: checkpoint.not_saved [{name, reason,
  // kind}]): notSaved is the names ('big, df'), notSavedDetail the reasons.
  // Both keys are present only when something was left out.
  const ns = Array.isArray(cp.not_saved) ? cp.not_saved.filter(i => i && i.name) : []
  if (!ns.length) return { saved, variables, size, ...sus }
  return {
    saved, variables, size, ...sus,
    notSaved: ns.map(i => i.name).join(', '),
    notSavedDetail: ns.map(i => (i.reason || i.name)
      + (i.kept_previous === true ? ' (previously saved copy kept)' : '')).join('\n'),
  }
}
/** 'Peak 1.6 of 2 GB · burst used 3×' from usage stats and/or the cost dict; null when nothing. */
export function memorySummary(usage, cost) {
  const parts = []
  if (usage && Number.isFinite(usage.peak_mem_mb) && usage.peak_mem_mb > 0) {
    const total = formatGbFromMb(usage.memory_mb)
    parts.push(total ? `Peak ${formatGbFromMb(usage.peak_mem_mb)} of ${total} GB` : `Peak ${formatGbFromMb(usage.peak_mem_mb)} GB`)
  }
  const episodes = usage && Number.isFinite(usage.burst_episodes) ? usage.burst_episodes : 0
  if (episodes > 0) parts.push(`burst used ${episodes}×`)
  else if (cost && Number(cost.burst_mb_seconds) > 0) parts.push('burst used')
  return parts.length ? parts.join(' · ') : null
}

/** '5h' / '1h 51m' / '45m' / '30s'. */
export function shortDuration(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(sec) || sec < 0) return null
  const s = Math.round(sec)
  if (s < MIN) return `${s}s`
  if (s < HOUR) return `${Math.floor(s / MIN)}m`
  const h = Math.floor(s / HOUR)
  const m = Math.floor((s % HOUR) / MIN)
  return m ? `${h}h ${m}m` : `${h}h`
}

/** Lifecycle counters, one fact per row: { running, suspended, resumes, suspends }.
 *  Durations as shortDuration ('15m', '1h 51m'), counts as strings ('5'). A
 *  zero / unknown value is null, so its row is omitted (one rule for all four). */
export function lifecycleCounters(cost) {
  const dur = (v) => (Number(v) > 0 ? shortDuration(Number(v)) : null)
  const cnt = (v) => (Number(v) > 0 ? String(Math.round(Number(v))) : null)
  if (!cost) return { running: null, suspended: null, resumes: null, suspends: null }
  return { running: dur(cost.running_secs), suspended: dur(cost.suspended_secs),
           resumes: cnt(cost.resume_count), suspends: cnt(cost.suspend_count) }
}
/** First `max` package names plus how many more. */
export function packagesSummary(names, total, max = 3) {
  const list = Array.isArray(names) ? names.filter(n => typeof n === 'string' && n) : []
  const count = Number.isFinite(total) && total >= list.length ? total : list.length
  const shown = list.slice(0, max)
  return { shown, more: Math.max(0, count - shown.length) }
}

/** Short local date-time 'Nov 12, 01:00' for an epoch/ISO value. */
export function formatShortDateTime(value) {
  const sec = toEpochSec(value)
  if (sec === null) return null
  return new Date(sec * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/** Auto-suspend row from the idle timeout SETTING (idlePolicy
 *  maxIdleDurationSeconds), not elapsed time: 'after 1m idle', 'after 30s idle',
 *  'after 1m 30s idle', 'after 1h 5m idle'; null when unset. */
export function idleSuspendLabel(sec) {
  const n = Number(sec)
  if (!sec || !Number.isFinite(n) || n <= 0) return null
  const s = Math.round(n)
  const h = Math.floor(s / HOUR)
  const m = Math.floor((s % HOUR) / MIN)
  const r = s % MIN
  const parts = []
  if (h) parts.push(`${h}h`)
  if (m) parts.push(`${m}m`)
  if (r && !h) parts.push(`${r}s`)
  return `after ${parts.join(' ') || '0s'} idle`
}

/** Lifecycle origin rows, one fact per row: { source, restoredFrom }.
 *  source: 'Warm pool' / 'New VM' (null when unknown); restoredFrom:
 *  'Checkpoint · Nov 12, 08:43 PM' ('Checkpoint' without a time), null when the
 *  VM did not restore a checkpoint (the row is omitted). null without origin. */
export function lifecycleOrigin(origin) {
  if (!origin) return null
  const source = origin.source === 'pool' ? 'Warm pool' : origin.source === 'fresh' ? 'New VM' : null
  let restoredFrom = null
  if (origin.restored) {
    const when = formatShortDateTime(origin.restored_checkpoint_at)
    restoredFrom = when ? `Checkpoint · ${when}` : 'Checkpoint'
  }
  return { source, restoredFrom }
}

/** 'notebook-microvm-arm64-2048 · v3' ('1.0' shows as 'v1'); null without a name. */
export function imageLabel(image) {
  if (!image || !image.name) return null
  return image.version !== null && image.version !== undefined && image.version !== ''
    ? `${image.name} · v${formatVersion(image.version)}`
    : image.name
}

/** Image name from an image ARN ('...:microvm-image:name' or '.../name'). */
export function imageNameFromArn(arn) {
  if (!arn || typeof arn !== 'string') return null
  const last = arn.split(':').pop().split('/').pop()
  return last || null
}
