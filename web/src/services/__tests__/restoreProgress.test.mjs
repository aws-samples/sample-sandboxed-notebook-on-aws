// Unit tests for services/restoreProgress.js (the "Reinstalling N packages" line).
// Run with: node --test web/src/services/__tests__/restoreProgress.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  packageBaseName, sessionPackages, reinstallProgressText, restoreProgressText,
} from '../restoreProgress.js'

test('base names drop pins, ranges, extras and markers', () => {
  assert.equal(packageBaseName('statsmodels==0.15.0'), 'statsmodels')
  assert.equal(packageBaseName('pandas[sql]>=2.0'), 'pandas')
  assert.equal(packageBaseName('requests~=2.28'), 'requests')
  assert.equal(packageBaseName('scikit_learn!=1.0'), 'scikit_learn')
  assert.equal(packageBaseName('foo ; python_version > "3"'), 'foo')
  assert.equal(packageBaseName('  bottle  '), 'bottle')
  assert.equal(packageBaseName(''), '')
  assert.equal(packageBaseName(null), '')
})

test('two packages: the brief\'s text', () => {
  assert.equal(reinstallProgressText(['statsmodels==0.15.0', 'bottle==0.13.4']),
    'Reinstalling 2 packages (statsmodels, bottle)...')
})

test('one package is singular', () => {
  assert.equal(reinstallProgressText(['bottle==0.13.4']), 'Reinstalling 1 package (bottle)...')
})

test('every name is listed, in order, no truncation', () => {
  const specs = Array.from({ length: 14 }, (_, i) => `pkg-with-a-long-name-${i}==1.${i}`)
  const text = reinstallProgressText(specs)
  assert.ok(text.startsWith('Reinstalling 14 packages (pkg-with-a-long-name-0, '))
  for (let i = 0; i < 14; i++) assert.ok(text.includes(`pkg-with-a-long-name-${i}`))
  assert.ok(text.endsWith('pkg-with-a-long-name-13)...'))
  assert.ok(!text.includes('more'))
})

test('duplicates by base name count once; blanks are ignored', () => {
  assert.equal(reinstallProgressText(['six', 'six==1.16.0', '', '  ']), 'Reinstalling 1 package (six)...')
})

test('nothing to reinstall: empty text', () => {
  assert.equal(reinstallProgressText([]), '')
  assert.equal(reinstallProgressText(null), '')
  assert.equal(reinstallProgressText(undefined), '')
  assert.equal(reinstallProgressText(['', '   ']), '')
})

test('sessionPackages reads GET /notebooks/{id} session_packages', () => {
  assert.deepEqual(sessionPackages({ session_packages: ['statsmodels==0.15.0', ' bottle==0.13.4 ', '', '# c'] }),
    ['statsmodels==0.15.0', 'bottle==0.13.4'])
  assert.deepEqual(sessionPackages({ session_packages: [] }), [])
  assert.equal(sessionPackages({ session_packages: null }), null)
  assert.equal(sessionPackages({}), null)
  assert.equal(sessionPackages(null), null)
})

test('the line shows only while the restore is pending', () => {
  const pkgs = ['statsmodels==0.15.0', 'bottle==0.13.4']
  assert.equal(restoreProgressText({ status: 'launching', _restorePackages: pkgs }),
    'Reinstalling 2 packages (statsmodels, bottle)...')
  assert.equal(restoreProgressText({ status: 'connected', _restorePackages: pkgs }), '')
  assert.equal(restoreProgressText({ status: 'disconnected', _restorePackages: pkgs }), '')
  assert.equal(restoreProgressText({ status: 'launching', _restorePackages: [] }), '')
  assert.equal(restoreProgressText({ status: 'launching', _restorePackages: null }), '')
  assert.equal(restoreProgressText({ status: 'launching' }), '')
  assert.equal(restoreProgressText(null), '')
})
