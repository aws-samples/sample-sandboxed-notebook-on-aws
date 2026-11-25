// Pure rules for restoring a saved session on a different instance type (no React).
//
// By default a restore runs on the architecture and memory tier the session was
// saved on (the server pins them). The user may pick another architecture or tier
// in the Connect dialog (Option A, "Restore on a different instance type"); the
// client then sends restoreOverride: true with arch and memoryMiB. No choice ever
// blocks the restore: these rules only produce warnings and texts.
//
// Memory warning, for the CHOSEN tier whatever its size relative to the saved
// tier (the default restore included):
//   need  = checkpoint size (MiB) x RESTORE_MEMORY_FACTOR
//   ratio = need / chosen tier MiB
//   ratio < RESTORE_WARN_RATIO                    no warning
//   RESTORE_WARN_RATIO <= ratio <= STRONG_RATIO   warning
//   ratio > RESTORE_STRONG_RATIO                  strong warning
// An unknown checkpoint size gives no memory warning. The proxy applies the same
// rule at /launch (proxy/platform/restore_choice.py restore_memory_assessment,
// returned as restore_memory_check); tests/proxy/test_restore_choice.py fails
// when the constants here and there differ.
import { archLabel as costArchLabel } from './costRates.js'
export const RESTORE_MEMORY_FACTOR = 2
export const RESTORE_WARN_RATIO = 0.5
export const RESTORE_STRONG_RATIO = 0.9
export const RESTORE_ARCH_WARNING = 'Packages are re-installed for the new architecture; some may not have a build for it. After the restore you will see which ones could not be installed'
// "x86_64", "ARM64"; '' when unknown.
export function archLabel(arch) {
  return arch ? costArchLabel(arch) : ''
}
// 4096 -> "4 GB", 512 -> "0.5 GB", 1536 -> "1.5 GB" (one decimal only when needed).
export function memoryLabel(mib) {
  const gb = Number(mib) / 1024
  if (!Number.isFinite(gb) || gb <= 0) return ''
  return Number.isInteger(gb) ? `${gb} GB` : `${Math.round(gb * 10) / 10} GB`
}
// The tier label with the memory and vCPU glyphs, in the exact format of the
// proxy's GET /image-tiers `label` (proxy/platform/routes/metrics.py _tier):
// 1024 -> "🧠 1.0 GB · ⚡ 0.5 vCPU", 2048 -> "🧠 2.0 GB · ⚡ 1.0 vCPU",
// 512 -> "🧠 0.5 GB · ⚡ 0.25 vCPU"; '' when unknown. The one source of every tier
// text in the Connect dialog (Launch and Restore selects, summaries, buttons).
// { vcpu: false } gives the memory part only ("🧠 1.0 GB"), for the footer
// button, which must fit one line at 380 px.
export function tierLabel(mib, { vcpu = true } = {}) {
  const m = Number(mib)
  if (!Number.isFinite(m) || m <= 0) return ''
  const mem = `\u{1F9E0} ${(m / 1024).toFixed(1)} GB`
  if (!vcpu) return mem
  const c = m / 2048
  return `${mem} \u00b7 \u26A1 ${Number.isInteger(c) ? c.toFixed(1) : String(c)} vCPU`
}
// "🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64"; the arch reads "unknown" when it is missing.
export function specText(arch, mib) {
  return [tierLabel(mib), archLabel(arch) || 'unknown'].filter(Boolean).join(' \u00b7 ')
}
// 2048 KB -> "2 MB", 1536000 KB -> "1.5 GB".
function sizeLabel(kb) {
  const mb = kb / 1024
  if (mb < 1024) return `${Math.max(1, Math.round(mb))} MB`
  return `${Math.round((mb / 1024) * 10) / 10} GB`
}
const positive = (v) => {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : null
}
// savedMemoryMiB is accepted for callers but does not change the result: the
// check compares the checkpoint with the chosen tier only.
export function restoreMemoryWarning({ checkpointKb, memoryMiB } = {}) {
  const none = { level: 'none', message: '' }
  const mem = positive(memoryMiB)
  const kb = positive(checkpointKb)
  if (!mem || !kb) return none
  const ratio = (kb / 1024) * RESTORE_MEMORY_FACTOR / mem
  if (ratio < RESTORE_WARN_RATIO) return none
  const size = sizeLabel(kb)
  const tier = memoryLabel(mem)
  if (ratio > RESTORE_STRONG_RATIO) {
    return {
      level: 'strong',
      message: `The saved state is about ${size}, close to or more than ${tier} can hold. The restore may run out of memory; a larger tier is safer.`,
    }
  }
  return {
    level: 'warn',
    message: `The saved state is about ${size}. On ${tier} little memory is left for new work.`,
  }
}
export function restoreArchWarning(savedArch, arch) {
  if (!savedArch || !arch || savedArch === arch) return ''
  return RESTORE_ARCH_WARNING
}
const spec = (arch, mib) => [archLabel(arch), memoryLabel(mib)].filter(Boolean).join(' / ')
// Everything the dialog shows for the current choice.
export function restoreChoiceState({ savedArch, savedMemoryMiB, arch, memoryMiB, checkpointKb } = {}) {
  const effArch = arch || savedArch || ''
  const effMem = positive(memoryMiB) || positive(savedMemoryMiB)
  const archChanged = !!savedArch && !!effArch && effArch !== savedArch
  const memoryChanged = !!positive(savedMemoryMiB) && !!effMem && effMem !== Number(savedMemoryMiB)
  const changed = archChanged || memoryChanged
  const warnings = []
  const archMsg = restoreArchWarning(savedArch, effArch)
  if (archMsg) warnings.push({ kind: 'arch', level: 'warn', message: archMsg })
  const mem = restoreMemoryWarning({ checkpointKb, memoryMiB: effMem })
  if (mem.level !== 'none') warnings.push({ kind: 'memory', level: mem.level, message: mem.message })
  const tone = mem.level === 'strong' ? 'red' : warnings.length ? 'amber' : 'neutral'
  // The dialog's warning element shows for a changed choice or any warning.
  const showNotice = changed || warnings.length > 0
  // The footer button names the chosen arch and memory ("Restore on ARM64 ·
  // 🧠 1.0 GB"; the dialog draws the arch as ArchBadge, whose innerText is the
  // same label). The vCPU is in the summary and the select above it.
  const buttonText = changed
    ? `Restore on ${[archLabel(effArch) || 'unknown', tierLabel(effMem, { vcpu: false })].filter(Boolean).join(' \u00b7 ')}`
    : 'Restore session on a new VM'
  const summaryText = changed
    ? `Restores on ${spec(effArch, effMem)} instead of the saved ${spec(savedArch, savedMemoryMiB)}.`
    : 'Restores on the instance type the session was saved on.'
  return {
    arch: effArch, memoryMiB: effMem, savedArch: savedArch || '', changed, archChanged, memoryChanged,
    warnings, tone, buttonText, summaryText, showNotice,
  }
}
// The extra /launch body keys: none for the default (the server pins the saved
// values), else the explicit choice.
export function restoreOverridePayload(state) {
  if (!state || !state.changed) return {}
  return { restoreOverride: true, arch: state.arch, memoryMiB: state.memoryMiB }
}
// The banner after a restore, from the /launch `restore_report`.
//   { verified, arch, packages_total, packages_failed: [{name, reason}] } or
//   { verified: false, reason } (the VM could not report).
// memoryCheck is the /launch `restore_memory_check`
//   { level: none|warn|strong|unknown, checkpoint_kb, tier_mib, ... }
// warn and strong add one sentence to the detail (and a success banner turns info).
export function restoreBannerText(report, { arch = '', memoryCheck = null } = {}) {
  const base = restorePackagesBannerText(report, { arch })
  const kb = positive(memoryCheck?.checkpoint_kb)
  const tier = memoryLabel(memoryCheck?.tier_mib)
  const level = memoryCheck?.level
  if (!kb || !tier || (level !== 'warn' && level !== 'strong')) return base
  const sentence = level === 'strong'
    ? `The saved state is about ${sizeLabel(kb)}, close to or more than ${tier} can hold; it may run out of memory.`
    : `The saved state is about ${sizeLabel(kb)}; on ${tier} little memory is left for new work.`
  return { ...base, tone: base.tone === 'success' ? 'info' : base.tone, detail: `${base.detail} ${sentence}` }
}
function restorePackagesBannerText(report, { arch = '' } = {}) {
  const where = archLabel(arch || report?.arch || '')
  const on = where ? `Restored on ${where}` : 'Restored'
  const failed = Array.isArray(report?.packages_failed)
    ? report.packages_failed.map(p => String(p?.name || '')).filter(Boolean)
    : []
  if (failed.length > 0) {
    const n = failed.length
    return {
      kind: 'restore-report', tone: 'error', lead: `${on}.`,
      detail: `${n} ${n === 1 ? 'package' : 'packages'} could not be installed: ${failed.join(', ')}.`,
      action: 'Open Packages',
    }
  }
  if (!report || report.verified !== true) {
    return { kind: 'restore-report', tone: 'info', lead: `${on}.`, detail: 'Packages not verified.', action: '' }
  }
  const total = Number(report.packages_total) || 0
  const detail = total === 0
    ? 'no packages to re-install.'
    : total === 1 ? 'the 1 package re-installed.' : `all ${total} packages re-installed.`
  return { kind: 'restore-report', tone: 'success', lead: `${on},`, detail, action: '' }
}
// Nearest tier to `target` in `tiers` (memory_mib numbers): the exact one when offered.
export function nearestTier(tiers = [], target) {
  const list = (tiers || []).map(Number).filter(n => n > 0)
  if (list.length === 0) return null
  const t = Number(target)
  if (list.includes(t)) return t
  return list.reduce((best, n) => (Math.abs(n - t) < Math.abs(best - t) ? n : best), list[0])
}
