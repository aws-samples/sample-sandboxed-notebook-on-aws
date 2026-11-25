// Tiny snapshot helper for the export tests: compares `actual` with
// __snapshots__/<name>; UPDATE_SNAPSHOTS=1 (or a missing file) writes it.
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '__snapshots__')

export function matchSnapshot(name, actual) {
  const file = join(DIR, name)
  if (process.env.UPDATE_SNAPSHOTS === '1' || !existsSync(file)) {
    mkdirSync(DIR, { recursive: true })
    writeFileSync(file, actual)
    return
  }
  assert.equal(actual, readFileSync(file, 'utf8'), `snapshot ${name} differs (UPDATE_SNAPSHOTS=1 to accept)`)
}
