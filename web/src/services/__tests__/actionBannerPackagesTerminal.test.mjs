// Unit tests for the package-install and terminal helpers of services/actionBanner.js.
// Run with: node --test web/src/services/__tests__/actionBannerPackagesTerminal.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  packageBannerText, shouldShowPackageBanner, pipInstallPackages, pipCellOutcome,
  TERMINAL_IDLE_MS, stripAnsi, endsWithPrompt, terminalCommandFinished,
  commandFromPromptLine, terminalBannerText, shouldClear,
} from '../actionBanner.js'

test('packageBannerText: success names the package and the time', () => {
  const b = packageBannerText({ ok: true, pkg: 'polars==1.2.0', elapsedMs: 8200 })
  assert.equal(b.kind, 'package-install')
  assert.equal(b.tone, 'success')
  assert.equal(`${b.lead} ${b.detail}`, 'Installed polars==1.2.0 in 8.2 s.')
  assert.equal(b.action, 'Show in Packages')
})

test('packageBannerText: a list of packages from a cell', () => {
  assert.equal(packageBannerText({ ok: true, pkg: ['a', 'b'], elapsedMs: 3000 }).detail, 'a and b in 3.0 s.')
  assert.equal(packageBannerText({ ok: true, pkg: ['a', 'b', 'c'], elapsedMs: 3000 }).detail, 'a and 2 more packages in 3.0 s.')
})

test('packageBannerText: failure keeps the full reason plus the arch hint', () => {
  const b = packageBannerText({
    ok: false, pkg: 'torch', error: 'No prebuilt wheel for torch on arm64.',
    hint: 'Running the notebook on x86_64 may help: it may have a prebuilt wheel.', hasDetails: true,
  })
  assert.equal(b.tone, 'error')
  assert.equal(b.lead, 'Install of torch failed:')
  assert.equal(b.detail, 'No prebuilt wheel for torch on arm64. Running the notebook on x86_64 may help: it may have a prebuilt wheel.')
  assert.equal(b.action, 'Show details')
  assert.equal(packageBannerText({ ok: false, pkg: 'x', error: '' }).detail, 'Install failed')
  assert.equal(packageBannerText({ ok: false, pkg: 'x', error: 'boom' }).action, '')
})

test('shouldShowPackageBanner: quick successes skipped, failures always shown', () => {
  assert.equal(shouldShowPackageBanner({ ok: true, elapsedMs: 500 }), false)
  assert.equal(shouldShowPackageBanner({ ok: true, elapsedMs: 2000 }), true)
  assert.equal(shouldShowPackageBanner({ ok: false, elapsedMs: 10 }), true)
})

test('package banner clears on uninstall of that package and on a new install', () => {
  assert.equal(shouldClear('package-install', 'uninstall-package'), true)
  assert.equal(shouldClear('package-install', 'same-op-start'), true)
  assert.equal(shouldClear('package-install', 'scroll'), false)
})

test('pipInstallPackages: finds specs in pip cells, skips options', () => {
  assert.deepEqual(pipInstallPackages('!pip install -q pandas numpy==1.26'), ['pandas', 'numpy==1.26'])
  assert.deepEqual(pipInstallPackages('%pip install --upgrade "polars>=1"'), ['polars>=1'])
  assert.deepEqual(pipInstallPackages('!python -m pip install -i https://x/simple foo'), ['foo'])
  assert.deepEqual(pipInstallPackages('!uv pip install bar  # comment'), ['bar'])
  assert.deepEqual(pipInstallPackages('!pip3 install -r requirements.txt'), [])
  assert.deepEqual(pipInstallPackages('import os\n!pip install a\n!pip install b'), ['a', 'b'])
  assert.equal(pipInstallPackages('import pandas as pd'), null)
  assert.equal(pipInstallPackages('# pip install x'), null)
  assert.equal(pipInstallPackages(''), null)
})

test('pipCellOutcome: pip ERROR lines count as a failure', () => {
  assert.deepEqual(pipCellOutcome({ status: 'success', output: 'Successfully installed a-1.0' }), { ok: true, error: '' })
  assert.deepEqual(
    pipCellOutcome({ status: 'success', output: 'Collecting nope\nERROR: No matching distribution found for nope\n' }),
    { ok: false, error: 'No matching distribution found for nope' },
  )
  assert.equal(pipCellOutcome({
    status: 'success',
    output: "ERROR: pip's dependency resolver does not currently take into account all the packages\nSuccessfully installed a-1.0\n",
  }).ok, true)
  assert.deepEqual(pipCellOutcome({ status: 'error', error: 'CalledProcessError\ntrace' }), { ok: false, error: 'CalledProcessError' })
})

test('stripAnsi and endsWithPrompt', () => {
  assert.equal(stripAnsi('\x1b]0;user@vm: ~\x07\x1b[01;32muser@vm\x1b[00m:~$ '), 'user@vm:~$ ')
  assert.equal(endsWithPrompt('done\r\n\x1b[?2004huser@vm:~$ '), true)
  assert.equal(endsWithPrompt('done\r\nroot@vm:/# '), true)
  assert.equal(endsWithPrompt('done\r\nhost% '), true)
  assert.equal(endsWithPrompt('downloading 40%\r\n'), false)
  assert.equal(endsWithPrompt('downloading 40% '), false)
  assert.equal(endsWithPrompt('>>> '), false)
  assert.equal(endsWithPrompt(''), false)
})

test('terminalCommandFinished: needs a submit, output after it, idle and a prompt tail', () => {
  const base = { submittedAt: 1000, lastOutputAt: 5000, tail: 'ok\r\nuser@vm:~$ ', now: 5000 + TERMINAL_IDLE_MS }
  assert.equal(terminalCommandFinished(base), true)
  assert.equal(terminalCommandFinished({ ...base, submittedAt: null }), false)
  assert.equal(terminalCommandFinished({ ...base, lastOutputAt: null }), false)
  assert.equal(terminalCommandFinished({ ...base, lastOutputAt: 900 }), false)
  assert.equal(terminalCommandFinished({ ...base, now: 5000 + TERMINAL_IDLE_MS - 1 }), false)
  assert.equal(terminalCommandFinished({ ...base, tail: 'still going 40%' }), false)
  // A command that came back within 2 s gets no banner.
  assert.equal(terminalCommandFinished({ ...base, lastOutputAt: 2500, now: 2500 + TERMINAL_IDLE_MS }), false)
})

test('commandFromPromptLine and terminalBannerText', () => {
  assert.equal(commandFromPromptLine('user@vm:~$ pip install polars   '), 'pip install polars')
  assert.equal(commandFromPromptLine('\x1b[01;32mroot@vm\x1b[00m:/# make all'), 'make all')
  const b = terminalBannerText({ command: 'sleep 5', elapsedMs: 5100 })
  assert.equal(b.kind, 'terminal')
  assert.equal(b.tone, 'info')
  assert.equal(`${b.lead} ${b.detail}`, 'Terminal command finished: sleep 5 (5.1 s).')
  assert.equal(b.action, 'Show terminal')
  assert.equal(`${terminalBannerText({ elapsedMs: 3000 }).lead} ${terminalBannerText({ elapsedMs: 3000 }).detail}`, 'Terminal command finished in 3.0 s.')
  assert.equal(shouldClear('terminal', 'terminal-visible-focused'), true)
})
