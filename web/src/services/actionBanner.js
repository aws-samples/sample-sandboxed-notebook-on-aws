// Pure rules shared by every action banner (no React, no DOM).
//
// An action banner is a short, temporary message shown in a panel after something
// the user started has finished (a run, a report, a sandbox, an install, ...). One
// banner per slot (panel); a newer banner replaces the older one. Each banner has
// a main action (one button), a dismiss button and an optional secondary action.
//
// LIFETIME (all banners):
//   auto-dismiss   10 s after the banner is first visible in its panel. Hovering it
//                  or moving keyboard focus into it pauses the countdown; leaving
//                  restarts the full 10 s. A hidden browser tab pauses it too.
//   dismiss        the x button
//   action         the main action (and the secondary action) also remove it
//   same-op-start  the same operation starting again (a new run, a new install...)
//   per kind       see CLEAR_RULES below
//   Never removes a banner: scrolling, hovering, selecting or focusing a cell,
//   toggling the heat map (NON_CLEARING_EVENTS).

import { formatDuration } from '../utils/formatDuration.js'
export const BANNER_AUTO_DISMISS_MS = 10000
// Runs shorter than this that end cleanly get no banner (the result is already in
// view and an instant banner would be noise).
export const RUN_BANNER_MIN_MS = 2000

export const SLOTS = Object.freeze({
  OUTLINE: 'outline',
  AI: 'ai',
  NOTEBOOKS: 'notebooks',
  MICROVMS: 'microvms',
  PACKAGES: 'packages',
  BOTTOM: 'bottom',
})

export const TONES = Object.freeze(['success', 'error', 'warning', 'info'])

let seq = 0

// Build a banner. A fresh id every time, so a replacement always restarts the timer.
export function makeBanner({
  kind, slot, tone = 'info', lead = '', detail = '', action = '', secondary = null,
  payload = null, createdAt = Date.now(),
} = {}) {
  seq += 1
  return {
    id: `${kind || 'banner'}-${createdAt}-${seq}`,
    kind: kind || 'banner',
    slot: slot || null,
    tone: TONES.includes(tone) ? tone : 'info',
    lead, detail, action, secondary, payload, createdAt,
  }
}

// One banner per slot: the newer one wins. A next banner that reuses the current
// id gets a fresh one, so the countdown restarts.
export function replaceInSlot(current, next) {
  if (!next) return current || null
  if (!current) return next
  if ((next.createdAt || 0) < (current.createdAt || 0)) return current
  if (next.id === current.id) {
    seq += 1
    return { ...next, id: `${next.kind}-${next.createdAt}-${seq}` }
  }
  return next
}

const BASE_CLEAR = Object.freeze(['dismiss', 'action', 'secondary', 'auto-dismiss', 'same-op-start'])

// Extra events that clear a banner, per kind.
export const CLEAR_RULES = Object.freeze({
  run: Object.freeze([...BASE_CLEAR, 'open-history', 'run-start']),
  'intel-ready': Object.freeze([...BASE_CLEAR, 'open-report', 'intel-start']),
  'intel-updated': Object.freeze([...BASE_CLEAR, 'open-report', 'intel-start']),
  'sandbox-ready': Object.freeze([...BASE_CLEAR, 'activate-target-tab']),
  'sandbox-failed': Object.freeze([...BASE_CLEAR, 'activate-target-tab']),
  'restore-report': Object.freeze([...BASE_CLEAR, 'activate-target-tab']),
  import: Object.freeze([...BASE_CLEAR, 'activate-target-tab']),
  'package-install': Object.freeze([...BASE_CLEAR, 'uninstall-package']),
  terminal: Object.freeze([...BASE_CLEAR, 'terminal-visible-focused']),
})

export const NON_CLEARING_EVENTS = Object.freeze([
  'scroll', 'hover', 'select-cell', 'focus-cell', 'toggle-heatmap',
])

// True when `event` must remove a banner of `kind`. Unknown kinds use the base rules.
export function shouldClear(kind, event) {
  if (NON_CLEARING_EVENTS.includes(event)) return false
  const rules = CLEAR_RULES[kind] || BASE_CLEAR
  return rules.includes(event)
}

// A run gets a banner when it took a while, failed, or was stopped.
export function shouldShowRunBanner({ elapsedMs = 0, errors = 0, stopped = 0 } = {}) {
  return elapsedMs >= RUN_BANNER_MIN_MS || errors > 0 || stopped > 0
}

// True when the user is already looking at the place the banner would send them
// to, so the banner would add nothing.
//   ctx: { activeTabId, tabId, view, showReport, terminalVisible, terminalFocused }
export function isAlreadyViewing(kind, ctx = {}) {
  const sameTab = ctx.tabId != null && String(ctx.activeTabId) === String(ctx.tabId)
  switch (kind) {
    case 'intel-ready':
    case 'intel-updated':
      return sameTab && !!ctx.showReport
    case 'sandbox-ready':
    case 'import':
      return sameTab && ctx.view === 'notebook'
    case 'terminal':
      return !!ctx.terminalVisible && !!ctx.terminalFocused
    default:
      return false
  }
}

// ---- Countdown (pure reducer) ---------------------------------------------------
// State: { duration, started, visible, hover, focus, deadline, expired }.
// deadline is an epoch ms while counting, null while paused or not started.

export function countdownInit(duration = BANNER_AUTO_DISMISS_MS) {
  return { duration, started: false, visible: false, hover: false, focus: false, deadline: null, expired: false }
}

function arm(s, now) {
  const counting = s.started && s.visible && !s.hover && !s.focus && !s.expired
  return { ...s, deadline: counting ? now + s.duration : null }
}

// Events: visible | hidden | hover-in | hover-out | focus-in | focus-out | expire.
// Every resume (visible again, hover-out, focus-out) restarts the FULL duration.
export function countdownStep(state, event, now = Date.now()) {
  const s = state || countdownInit()
  if (s.expired) return s
  switch (event) {
    case 'visible':
      if (s.visible) return s
      return arm({ ...s, visible: true, started: true }, now)
    case 'hidden':
      return { ...s, visible: false, deadline: null }
    case 'hover-in':
      return { ...s, hover: true, deadline: null }
    case 'hover-out':
      if (!s.hover) return s
      return arm({ ...s, hover: false }, now)
    case 'focus-in':
      return { ...s, focus: true, deadline: null }
    case 'focus-out':
      if (!s.focus) return s
      return arm({ ...s, focus: false }, now)
    case 'expire':
      if (s.deadline != null && now >= s.deadline) return { ...s, expired: true, deadline: null }
      return s
    default:
      return s
  }
}

// Ms until the countdown expires, or null while it is paused / not started.
export function countdownRemaining(state, now = Date.now()) {
  if (!state || state.deadline == null) return null
  return Math.max(0, state.deadline - now)
}
// ---- Text helpers (one per banner kind) -----------------------------------------
// Each returns { kind, tone, lead, detail, action }; ActionBanner joins the parts
// with spaces, so the punctuation lives here.

// "a.csv", "a.csv and b.csv", "a.csv and 2 more files".
export function fileListText(names = []) {
  const list = (names || []).filter(Boolean).map(String)
  if (list.length === 0) return ''
  if (list.length === 1) return list[0]
  if (list.length === 2) return `${list[0]} and ${list[1]}`
  return `${list[0]} and ${list.length - 1} more files`
}

// Workbook Intel finished. mode 'full' = a complete (re)generation, 'update' = a
// delta after a file upload or removal (trigger 'file_upload' | 'file_delete').
export function intelBannerText({ mode = 'full', trigger = '', fileNames = [] } = {}) {
  const action = 'View full report'
  if (mode !== 'update') {
    return { kind: 'intel-ready', tone: 'success', lead: 'Workbook Intel report is ready.', detail: '', action }
  }
  const files = fileListText(fileNames)
  if (!files) return { kind: 'intel-updated', tone: 'success', lead: 'Workbook Intel updated.', detail: '', action }
  const detail = trigger === 'file_delete' ? `after removing ${files}.` : `for ${files}.`
  return { kind: 'intel-updated', tone: 'success', lead: 'Workbook Intel updated', detail, action }
}

const SANDBOX_VERB = Object.freeze({ restore: 'restored', launch: 'launched', connect: 'connected' })
const SANDBOX_FAIL = Object.freeze({
  restore: 'Sandbox restore failed', launch: 'Sandbox launch failed', connect: 'Connection failed',
})

// A sandbox finished starting (op 'restore' | 'launch' | 'connect').
export function sandboxBannerText({ op = 'launch', ok = true, elapsedMs = 0, error = '', name = '' } = {}) {
  if (ok) {
    const verb = SANDBOX_VERB[op] || SANDBOX_VERB.launch
    const where = name ? `for ${name}, ` : ''
    return {
      kind: 'sandbox-ready', tone: 'success', lead: 'Sandbox ready',
      detail: `${where}${verb} in ${formatDuration(elapsedMs)}.`, action: 'Open notebook',
    }
  }
  const where = name ? `for ${name}: ` : ': '
  // Drop a repeated "Launch failed:" / "Restore error:" prefix from the reason.
  const reason = String(error || '').replace(/^\s*(launch|restore) (failed|error):\s*/i, '').trim() || 'Unknown error'
  return {
    kind: 'sandbox-failed', tone: 'error',
    lead: `${SANDBOX_FAIL[op] || SANDBOX_FAIL.launch}${name ? '' : ':'}`,
    detail: name ? `${where}${reason}` : reason, action: 'Open Connect',
  }
}

// Success banners only for a sandbox that took a while; a failure always shows.
export function shouldShowSandboxBanner({ ok = true, elapsedMs = 0 } = {}) {
  return !ok || elapsedMs >= RUN_BANNER_MIN_MS
}

// A sample or notebook import finished.
export function importBannerText({ ok = true, name = '', error = '' } = {}) {
  if (ok) {
    return { kind: 'import', tone: 'success', lead: 'Imported', detail: `${name || 'notebook'}.`, action: 'Open it' }
  }
  // Drop a repeated "Import failed:" / "Import error:" prefix from the reason.
  const reason = String(error || '').replace(/^\s*import (failed|error):\s*/i, '').trim() || 'Unknown error'
  return { kind: 'import', tone: 'error', lead: 'Import failed:', detail: reason, action: '' }
}

// ---- Package install ------------------------------------------------------------

// A package install finished (from the Packages panel or a pip cell). `pkg` is the
// spec or a list of names; `hint` is the other-arch hint (services/pipInstallMessage).
// Failures with pip output get "Show details" (opens the Details block).
export function packageBannerText({ ok = true, pkg = '', elapsedMs = 0, error = '', hint = '', hasDetails = false } = {}) {
  const names = Array.isArray(pkg) ? fileListText(pkg).replace(/ more files$/, ' more packages') : String(pkg || '')
  if (ok) {
    const took = elapsedMs > 0 ? ` in ${formatDuration(elapsedMs)}` : ''
    return {
      kind: 'package-install', tone: 'success', lead: 'Installed',
      detail: `${names || 'packages'}${took}.`, action: 'Show in Packages',
    }
  }
  const reason = String(error || '').trim() || 'Install failed'
  return {
    kind: 'package-install', tone: 'error',
    lead: names ? `Install of ${names} failed:` : 'Install failed:',
    detail: hint ? `${reason} ${hint}` : reason,
    action: hasDetails ? 'Show details' : '',
  }
}

// Success banners only for an install that took a while; a failure always shows.
export function shouldShowPackageBanner({ ok = true, elapsedMs = 0 } = {}) {
  return !ok || elapsedMs >= RUN_BANNER_MIN_MS
}

// Options of `pip install` that take a value (the next token is not a package).
const PIP_VALUE_OPTS = new Set([
  '-r', '--requirement', '-c', '--constraint', '-e', '--editable', '-i', '--index-url',
  '--extra-index-url', '-f', '--find-links', '-t', '--target', '--prefix', '--root',
  '--upgrade-strategy', '--platform', '--python-version', '--implementation', '--abi',
  '--progress-bar', '--src', '--cache-dir', '--log', '--trusted-host', '--proxy', '--timeout',
])

// Package specs installed by a cell (`!pip install`, `%pip install`, `!pip3 install`,
// `!python -m pip install`, `!uv pip install`), or null when the code has none.
// Best effort: options and their values are skipped, specs keep their pins.
export function pipInstallPackages(code = '') {
  const found = []
  let any = false
  const re = /^\s*[!%]\s*(?:(?:python3?|py)\s+-m\s+)?(?:uv\s+)?pip3?\s+install\b(.*)$/gm
  let m
  while ((m = re.exec(String(code || ''))) !== null) {
    any = true
    const tokens = m[1].replace(/#.*$/, '').trim().split(/\s+/).filter(Boolean)
    for (let i = 0; i < tokens.length; i++) {
      const t = tokens[i]
      if (t.startsWith('-')) {
        if (!t.includes('=') && PIP_VALUE_OPTS.has(t)) i += 1
        continue
      }
      const spec = t.replace(/^['"]|['"]$/g, '')
      if (spec && !spec.startsWith('$') && !spec.startsWith('{')) found.push(spec)
    }
  }
  return any ? found : null
}

// Outcome of a finished pip cell: { ok, error }. A shell `!pip` that fails does not
// raise, so pip's own "ERROR:" lines in the output count as a failure.
export function pipCellOutcome({ status = 'success', output = '', error = '' } = {}) {
  const errLine = String(output || '').split(/\r?\n/).find(l => /^\s*ERROR:/.test(l))
  if (status === 'error') {
    const first = String(error || '').split(/\r?\n/).map(s => s.trim()).find(Boolean) || ''
    return { ok: false, error: (errLine || first || 'Install failed').trim().replace(/^ERROR:\s*/, '') }
  }
  // pip prints "ERROR: pip's dependency resolver ..." even when the install
  // went through, so "Successfully installed" wins.
  if (errLine && !/^\s*Successfully installed\b/m.test(String(output || ''))) {
    return { ok: false, error: errLine.trim().replace(/^ERROR:\s*/, '') }
  }
  return { ok: true, error: '' }
}

// ---- Terminal command finished --------------------------------------------------
// The terminal websocket carries raw shell output and no exit status. A command
// counts as finished when, after it was submitted (Enter), there was output, the
// output then went quiet for TERMINAL_IDLE_MS, and the last output line looks like
// a shell prompt (ends with "$", "#" or "%", optionally followed by a space; a
// "%" right after a digit is a progress percentage and does not count).
// Commands that came back within RUN_BANNER_MIN_MS get no banner.
export const TERMINAL_IDLE_MS = 1500
// Window event App dispatches after the terminal banner is clicked (it has just
// made the terminal the visible bottom tab): TerminalPanel focuses the shell.
export const TERMINAL_FOCUS_EVENT = 'terminal-focus-request'

// Strip ANSI escape sequences (CSI, OSC titles, single-char escapes).
export function stripAnsi(s = '') {
  return String(s || '')
    // eslint-disable-next-line no-control-regex
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
    // eslint-disable-next-line no-control-regex
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    // eslint-disable-next-line no-control-regex
    .replace(/\x1b[@-Z\\-_]/g, '')
}

// True when the text after the last line break looks like a shell prompt.
export function endsWithPrompt(tail = '') {
  const clean = stripAnsi(tail)
  const lastLine = clean.split(/[\r\n]/).pop() || ''
  // "%" after a digit is a progress percentage ("40%"), not a zsh prompt.
  return /\S/.test(lastLine) && /(?:[$#]|(?<!\d)%) ?$/.test(lastLine)
}

export function terminalCommandFinished({
  submittedAt = null, lastOutputAt = null, tail = '', now = Date.now(),
  idleMs = TERMINAL_IDLE_MS, minMs = RUN_BANNER_MIN_MS,
} = {}) {
  if (submittedAt == null || lastOutputAt == null) return false
  if (lastOutputAt < submittedAt) return false
  if (now - lastOutputAt < idleMs) return false
  if (lastOutputAt - submittedAt < minMs) return false
  return endsWithPrompt(tail)
}

// The command typed on a prompt line ("user@vm:~$ pip install x" -> "pip install x").
export function commandFromPromptLine(line = '') {
  const clean = stripAnsi(line).replace(/\s+$/, '')
  const m = clean.match(/^.*?[$#%] (.*)$/)
  return (m ? m[1] : clean).trim()
}

export function terminalBannerText({ command = '', elapsedMs = 0 } = {}) {
  const took = formatDuration(elapsedMs)
  return {
    kind: 'terminal', tone: 'info',
    lead: command ? 'Terminal command finished:' : 'Terminal command finished',
    detail: command ? `${command} (${took}).` : `in ${took}.`,
    action: 'Show terminal',
  }
}
