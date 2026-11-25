// Pure helpers for the Connect to Sandbox dialog (components/ConnectionPanel.jsx).
// No React. The dialog has two collapsible sections, one open at a time:
// 'restore' (shown only when a checkpoint of the notebook's session exists) and
// 'launch'. The restore rules themselves live in services/restoreChoice.js; this
// module only turns their result into the dialog's texts.
import { archLabel, memoryLabel, tierLabel, specText } from './restoreChoice.js'

export const SECTION_RESTORE = 'restore'
export const SECTION_LAUNCH = 'launch'

// The open section. `chosen` is the user's last pick (null until they pick one):
// Restore by default when it is offered, else Launch. A pick of Restore falls back
// to Launch when Restore is not offered (no checkpoint).
export function openSection(chosen, hasRestore) {
  if (!hasRestore) return SECTION_LAUNCH
  return chosen === SECTION_LAUNCH ? SECTION_LAUNCH : SECTION_RESTORE
}

// The section that opens after a click (or Enter / Space) on `clicked`'s header.
// Exactly one section stays open: a closed header opens its section; the open
// header opens the other one. With Restore not offered, Launch stays open.
export function toggledSection(current, clicked, hasRestore) {
  if (!hasRestore) return SECTION_LAUNCH
  if (clicked !== current) return clicked
  return clicked === SECTION_RESTORE ? SECTION_LAUNCH : SECTION_RESTORE
}

// "🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64"; the arch reads "unknown" when it is missing.
export function savedInstanceText(arch, memoryMiB) {
  return specText(arch, memoryMiB)
}

// A section header summary as parts, so the dialog can draw the arch as
// ArchBadge (icon + label) between the tier glyph label and the tag:
//   { tier: '🧠 1.0 GB · ⚡ 0.5 vCPU', arch: 'x86_64', tag: 'saved', text }
// `text` is the plain reading ("🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64 · saved").
// `tier` is '' (and `text` the tag alone) when the memory tier is unknown.
function summaryParts(arch, memoryMiB, tag) {
  const tier = tierLabel(memoryMiB)
  if (!tier) return { tier: '', arch: '', tag, text: tag }
  return { tier, arch: arch || '', tag, text: `${specText(arch, memoryMiB)} · ${tag}` }
}

// The Restore header summary: the saved spec with "saved", or the chosen spec
// with "changed".
export function restoreSectionSummary({ savedArch, savedMemoryMiB, choice } = {}) {
  if (choice?.changed) return summaryParts(choice.arch, choice.memoryMiB, 'changed')
  return summaryParts(savedArch, savedMemoryMiB, 'saved')
}

// The Launch header summary from the Launch controls, tagged "fresh".
export function launchSectionSummary({ memoryMiB, arch } = {}) {
  return summaryParts(arch, memoryMiB, 'fresh')
}

// The notes under the Restore selects, from restoreChoiceState():
//   level 'neutral' | 'warn' | 'strong'; title; message ('' when none).
// A changed choice gives the "Restores on X instead of the saved Y" note (amber
// with the architecture warning when the arch changed, else neutral); a memory
// warning gives its own note (amber 'warn', red 'strong').
export function restoreNotes(choice) {
  if (!choice) return []
  const notes = []
  const arch = (choice.warnings || []).find(w => w.kind === 'arch')
  const mem = (choice.warnings || []).find(w => w.kind === 'memory')
  // `short`: the one-line text of the strip pinned above the footer, shown only
  // while the full note is scrolled out of view (never a second copy of it).
  if (choice.changed || arch) {
    const to = archLabel(choice.arch)
    const from = archLabel(choice.savedArch)
    notes.push({
      kind: 'choice',
      level: arch ? 'warn' : 'neutral',
      title: String(choice.summaryText || '').replace(/\.$/, ''),
      message: arch ? withPeriod(arch.message) : '',
      short: arch && to && from
        ? `Restores on ${to}, not the saved ${from}: some packages may not install`
        : 'Changed from the saved instance type',
    })
  }
  if (mem) {
    const tier = memoryLabel(choice.memoryMiB)
    const title = mem.level === 'strong' ? 'May run out of memory' : 'Little memory left'
    notes.push({
      kind: 'memory',
      level: mem.level === 'strong' ? 'strong' : 'warn',
      title,
      message: mem.message,
      short: tier ? `${title} on ${tier}` : title,
    })
  }
  return notes
}
const withPeriod = (s) => (s && !/[.!?]$/.test(s) ? `${s}.` : s || '')

const RANK = { strong: 2, warn: 1 }
// The strongest active note (red memory risk first, else the first amber one);
// null when no note warns. Neutral notes are never pinned.
export function strongestNote(notes = []) {
  let best = null
  for (const n of notes || []) {
    const r = RANK[n?.level] || 0
    if (r > 0 && (!best || r > RANK[best.level])) best = n
  }
  return best
}

// Footer hint left of the buttons.
export function footerHint(section, { choice = null, cleanStart = false } = {}) {
  if (section === SECTION_RESTORE) {
    return choice?.changed ? 'Changed from the saved instance' : 'Restores variables, files and packages'
  }
  return cleanStart ? 'Starts empty. Your saved session stays untouched' : 'Starts a new MicroVM for this notebook'
}

// Checkpoint size: 34 -> "34 KB", 2048 -> "2 MB", 1572864 -> "1.5 GB"; '' when unknown.
export function checkpointSizeLabel(kb) {
  const n = Number(kb)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n < 1024) return `${Math.max(1, Math.round(n))} KB`
  const mb = n / 1024
  if (mb < 1024) return `${Math.round(mb)} MB`
  return `${Math.round((mb / 1024) * 10) / 10} GB`
}
