/**
 * Human-friendly duration formatting for execution times (input is MILLISECONDS).
 *
 * Picks the most readable unit for the magnitude instead of always printing ms:
 *   - < 1 s          → "873 ms"        (whole ms — sub-second precision isn't useful below 1s)
 *   - 1 s – 60 s     → "35.9 s"        (one decimal second)
 *   - 1 min – 60 min → "1 min 36 s"    (minutes + whole seconds; drops "0 s" on a round minute)
 *   - ≥ 60 min       → "1 h 5 min"     (hours + whole minutes; drops "0 min" on a round hour)
 *
 * `formatDurationShort` is the compact variant (m/s/ms suffixes, no spaces) for
 * tight spots like the Outline rail; `formatDuration` is the spelled-out variant
 * for the cell-output footer where there's room to read it.
 */

export function formatDuration(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  if (ms < 3600000) {
    const totalSec = Math.round(ms / 1000)
    const m = Math.floor(totalSec / 60)
    const s = totalSec % 60
    return s === 0 ? `${m} min` : `${m} min ${s} s`
  }
  const totalMin = Math.round(ms / 60000)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  return m === 0 ? `${h} h` : `${h} h ${m} min`
}

export function formatDurationShort(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 3600000) {
    const totalSec = Math.round(ms / 1000)
    const m = Math.floor(totalSec / 60)
    const s = totalSec % 60
    return s === 0 ? `${m}m` : `${m}m ${s}s`
  }
  const totalMin = Math.round(ms / 60000)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  return m === 0 ? `${h}h` : `${h}h ${m}m`
}
