// Unit tests for services/connectDialog.js (Connect to Sandbox dialog texts).
// Run with: node --test web/src/services/__tests__/connectDialog.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  SECTION_RESTORE, SECTION_LAUNCH, openSection, toggledSection, savedInstanceText,
  restoreSectionSummary, launchSectionSummary, restoreNotes, strongestNote, footerHint,
  checkpointSizeLabel,
} from '../connectDialog.js'
import { restoreChoiceState, RESTORE_ARCH_WARNING, tierLabel } from '../restoreChoice.js'

const saved = { savedArch: 'x86_64', savedMemoryMiB: 1024 }

test('default open section: Restore with a checkpoint, else Launch', () => {
  assert.equal(openSection(null, true), SECTION_RESTORE)
  assert.equal(openSection(null, false), SECTION_LAUNCH)
  assert.equal(openSection(SECTION_LAUNCH, true), SECTION_LAUNCH)
  assert.equal(openSection(SECTION_RESTORE, true), SECTION_RESTORE)
  // A pick of Restore never shows a section that is not offered.
  assert.equal(openSection(SECTION_RESTORE, false), SECTION_LAUNCH)
})

test('accordion: one section open at a time', () => {
  assert.equal(toggledSection(SECTION_RESTORE, SECTION_LAUNCH, true), SECTION_LAUNCH)
  assert.equal(toggledSection(SECTION_LAUNCH, SECTION_RESTORE, true), SECTION_RESTORE)
  // The open header opens the other section.
  assert.equal(toggledSection(SECTION_RESTORE, SECTION_RESTORE, true), SECTION_LAUNCH)
  assert.equal(toggledSection(SECTION_LAUNCH, SECTION_LAUNCH, true), SECTION_RESTORE)
  // Launch only: it stays open.
  assert.equal(toggledSection(SECTION_LAUNCH, SECTION_LAUNCH, false), SECTION_LAUNCH)
})

test('saved instance text; a missing arch reads unknown, never ARM64', () => {
  assert.equal(savedInstanceText('x86_64', 1024), '🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64')
  assert.equal(savedInstanceText('arm64', 2048), '🧠 2.0 GB · ⚡ 1.0 vCPU · ARM64')
  assert.equal(savedInstanceText('', 1024), '🧠 1.0 GB · ⚡ 0.5 vCPU · unknown')
  assert.equal(savedInstanceText(null, 512), '🧠 0.5 GB · ⚡ 0.25 vCPU · unknown')
})
test('restore summary parts: saved, changed, and unknown tier', () => {
  const def = restoreChoiceState({ ...saved })
  assert.deepEqual(restoreSectionSummary({ ...saved, choice: def }),
    { tier: '🧠 1.0 GB · ⚡ 0.5 vCPU', arch: 'x86_64', tag: 'saved', text: '🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64 · saved' })
  const ch = restoreChoiceState({ ...saved, arch: 'arm64', memoryMiB: 1024 })
  assert.deepEqual(restoreSectionSummary({ ...saved, choice: ch }),
    { tier: '🧠 1.0 GB · ⚡ 0.5 vCPU', arch: 'arm64', tag: 'changed', text: '🧠 1.0 GB · ⚡ 0.5 vCPU · ARM64 · changed' })
  const mem = restoreChoiceState({ ...saved, memoryMiB: 2048 })
  assert.equal(restoreSectionSummary({ ...saved, choice: mem }).text, '🧠 2.0 GB · ⚡ 1.0 vCPU · x86_64 · changed')
  assert.deepEqual(restoreSectionSummary({ savedArch: '', savedMemoryMiB: null, choice: restoreChoiceState({}) }),
    { tier: '', arch: '', tag: 'saved', text: 'saved' })
})
test('launch summary parts from the Launch controls (same tier label as the selects)', () => {
  assert.equal(launchSectionSummary({ memoryMiB: '1024', arch: 'x86_64' }).text, '🧠 1.0 GB · ⚡ 0.5 vCPU · x86_64 · fresh')
  assert.equal(launchSectionSummary({ memoryMiB: 4096, arch: 'arm64' }).text, '🧠 4.0 GB · ⚡ 2.0 vCPU · ARM64 · fresh')
  assert.equal(launchSectionSummary({ memoryMiB: 1024, arch: '' }).text, '🧠 1.0 GB · ⚡ 0.5 vCPU · unknown · fresh')
  assert.equal(launchSectionSummary({ memoryMiB: 1024, arch: 'arm64' }).tier, tierLabel(1024))
})
test('notes: none for the default restore of a small checkpoint', () => {
  assert.deepEqual(restoreNotes(restoreChoiceState({ ...saved, checkpointKb: 34 })), [])
  assert.equal(strongestNote([]), null)
})

test('notes: arch change is amber with the architecture warning', () => {
  const notes = restoreNotes(restoreChoiceState({ ...saved, arch: 'arm64', memoryMiB: 1024, checkpointKb: 34 }))
  assert.equal(notes.length, 1)
  assert.equal(notes[0].level, 'warn')
  assert.equal(notes[0].title, 'Restores on ARM64 / 1 GB instead of the saved x86_64 / 1 GB')
  assert.equal(notes[0].message, `${RESTORE_ARCH_WARNING}.`)
  assert.equal(strongestNote(notes), notes[0])
  // The pinned strip's one-line text is short and not a copy of the note.
  assert.equal(notes[0].short, 'Restores on ARM64, not the saved x86_64: some packages may not install')
  assert.ok(!notes[0].short.includes(notes[0].title) && !notes[0].short.includes(notes[0].message))
})

test('notes: a larger tier on the same arch is a neutral note, never pinned', () => {
  const notes = restoreNotes(restoreChoiceState({ ...saved, memoryMiB: 4096, checkpointKb: 34 }))
  assert.equal(notes.length, 1)
  assert.equal(notes[0].level, 'neutral')
  assert.equal(notes[0].message, '')
  assert.equal(strongestNote(notes), null)
})

test('strongest note: the red memory risk wins over the amber arch note', () => {
  // 1.5 GB checkpoint on 1 GB: need 3 GB > 0.9 x 1 GB -> strong.
  const choice = restoreChoiceState({ ...saved, arch: 'arm64', memoryMiB: 1024, checkpointKb: 1.5 * 1024 * 1024 })
  const notes = restoreNotes(choice)
  assert.deepEqual(notes.map(n => n.level), ['warn', 'strong'])
  assert.equal(notes[1].title, 'May run out of memory')
  assert.equal(notes[1].short, 'May run out of memory on 1 GB')
  assert.equal(notes[1].message, choice.warnings.find(w => w.kind === 'memory').message)
  assert.equal(strongestNote(notes).kind, 'memory')
})

test('strongest note: amber memory warning on the default restore', () => {
  // need = 300 MB x 2 = 600 MB on 1 GB -> 0.59 -> warn.
  const notes = restoreNotes(restoreChoiceState({ ...saved, checkpointKb: 300 * 1024 }))
  assert.deepEqual(notes.map(n => [n.kind, n.level]), [['memory', 'warn']])
  assert.equal(notes[0].title, 'Little memory left')
  assert.equal(notes[0].short, 'Little memory left on 1 GB')
  assert.equal(strongestNote(notes).kind, 'memory')
})

test('strongest note: among amber notes the first one', () => {
  const choice = restoreChoiceState({ ...saved, arch: 'arm64', memoryMiB: 1024, checkpointKb: 300 * 1024 })
  const notes = restoreNotes(choice)
  assert.deepEqual(notes.map(n => n.level), ['warn', 'warn'])
  assert.equal(strongestNote(notes).kind, 'choice')
})

test('footer hints', () => {
  assert.equal(footerHint(SECTION_RESTORE, { choice: restoreChoiceState({ ...saved }) }), 'Restores variables, files and packages')
  assert.equal(footerHint(SECTION_RESTORE, { choice: restoreChoiceState({ ...saved, arch: 'arm64' }) }), 'Changed from the saved instance')
  assert.equal(footerHint(SECTION_LAUNCH, { cleanStart: true }), 'Starts empty. Your saved session stays untouched')
  assert.equal(footerHint(SECTION_LAUNCH, { cleanStart: false }), 'Starts a new MicroVM for this notebook')
})

test('checkpoint size label', () => {
  assert.equal(checkpointSizeLabel(34), '34 KB')
  assert.equal(checkpointSizeLabel(2048), '2 MB')
  assert.equal(checkpointSizeLabel(1.5 * 1024 * 1024), '1.5 GB')
  assert.equal(checkpointSizeLabel(null), '')
  assert.equal(checkpointSizeLabel(0), '')
})
