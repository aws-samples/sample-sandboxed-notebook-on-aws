// Unit tests for services/packageFilter.js (Packages panel: text + "User installed" filter).
// Run with: node --test web/src/services/__tests__/packageFilter.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { filterPackages, countUserPackages, isUserPackage } from '../packageFilter.js'

const PKGS = [
  { name: 'numpy', version: '2.0.0' },
  { name: 'pandas', version: '2.2.3' },
  { name: 'Seaborn', version: '0.13.2' },
  { name: 'statsmodels', version: '0.15.0' },
  { name: 'pandas-stubs', version: '2.2.0' },
]
const USER = new Set(['seaborn', 'statsmodels', 'pandas-stubs'])
const names = (list) => list.map(p => p.name)

test('filterPackages: no filters returns every package in order', () => {
  assert.deepEqual(names(filterPackages(PKGS)), names(PKGS))
  assert.deepEqual(names(filterPackages(PKGS, { text: '  ' })), names(PKGS))
})

test('filterPackages: text filter is a case-insensitive substring match', () => {
  assert.deepEqual(names(filterPackages(PKGS, { text: 'PANDAS' })), ['pandas', 'pandas-stubs'])
  assert.deepEqual(names(filterPackages(PKGS, { text: 'sea' })), ['Seaborn'])
})

test('filterPackages: userOnly keeps only user-installed packages (case-insensitive)', () => {
  assert.deepEqual(names(filterPackages(PKGS, { userOnly: true, userSet: USER })), ['Seaborn', 'statsmodels', 'pandas-stubs'])
})

test('filterPackages: userOnly combines with the text filter', () => {
  assert.deepEqual(names(filterPackages(PKGS, { text: 'pandas', userOnly: true, userSet: USER })), ['pandas-stubs'])
  assert.deepEqual(names(filterPackages(PKGS, { text: 'numpy', userOnly: true, userSet: USER })), [])
})

test('filterPackages: userOnly with no user packages is empty; bad input is safe', () => {
  assert.deepEqual(filterPackages(PKGS, { userOnly: true, userSet: new Set() }), [])
  assert.deepEqual(filterPackages(PKGS, { userOnly: true }), [])
  assert.deepEqual(filterPackages(null, { text: 'x' }), [])
})

test('countUserPackages / isUserPackage', () => {
  assert.equal(countUserPackages(PKGS, USER), 3)
  assert.equal(countUserPackages(PKGS, new Set()), 0)
  assert.equal(countUserPackages(undefined, USER), 0)
  // Only listed packages count: a tracked name pip no longer lists is not counted.
  assert.equal(countUserPackages(PKGS.slice(0, 2), USER), 0)
  assert.equal(isUserPackage('SEABORN', USER), true)
  assert.equal(isUserPackage('numpy', USER), false)
  assert.equal(isUserPackage('numpy', null), false)
})
