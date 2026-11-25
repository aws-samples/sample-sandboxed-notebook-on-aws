// Source scan: every cell object literal in web/src that sets the base64 `image`
// must also say what happens to the persisted chart key (`imageS3Key`: null for a
// reset / new / imported cell, or an explicit keep). A new reset site that forgot
// the key would otherwise keep showing (and re-saving) a stale chart.
// Also: no persisted cell shape may carry a base64 image.
// Run with: node --test web/src/services/__tests__/imageKeyPairing.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

// Not cell STATE: the native .notebook.json export embeds the chart as a data URL
// (`image`) and deliberately omits imageS3Key — a key belongs to one notebook and
// storage drops it on import anyway.
const EXEMPT_FILES = new Set(['services/notebookExport.js'])

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (name === '__tests__' || name === 'node_modules') continue
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(js|jsx)$/.test(name)) out.push(p)
  }
  return out
}

// The smallest enclosing `{ … }` around position i (brace matching, ignoring
// braces inside quotes/template strings well enough for these literals).
function enclosingObject(text, i) {
  let depth = 0
  let start = -1
  for (let j = i; j >= 0; j--) {
    const ch = text[j]
    if (ch === '}') depth++
    else if (ch === '{') {
      if (depth === 0) { start = j; break }
      depth--
    }
  }
  if (start < 0) return ''
  depth = 0
  for (let j = start; j < text.length; j++) {
    const ch = text[j]
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) return text.slice(start, j + 1)
    }
  }
  return text.slice(start)
}

test('every `image:` cell literal also sets imageS3Key', () => {
  const offenders = []
  let sites = 0
  for (const file of walk(SRC)) {
    if (EXEMPT_FILES.has(relative(SRC, file))) continue
    const text = readFileSync(file, 'utf8')
    const re = /\bimage:\s*(null|[a-zA-Z_.]+\.image\b)/g
    let m
    while ((m = re.exec(text))) {
      sites++
      const obj = enclosingObject(text, m.index)
      if (!/\bimageS3Key\s*:/.test(obj)) {
        const line = text.slice(0, m.index).split('\n').length
        offenders.push(`${relative(SRC, file)}:${line}`)
      }
    }
  }
  assert.ok(sites >= 19, `scan found only ${sites} sites — the pattern drifted`)
  assert.deepEqual(offenders, [])
})

test('persisted cell shapes never carry base64 (save payload + localStorage writers)', () => {
  const text = readFileSync(join(SRC, 'hooks', 'useTabsPersistence.js'), 'utf8')
  const payload = text.slice(text.indexOf('function buildNotebookSavePayload'), text.indexOf('export function useTabsPersistence'))
  assert.ok(payload.includes('imageS3Key: c.imageS3Key || null'))
  assert.ok(!/\bimage:\s*c\.image/.test(payload), 'save payload must not include the base64 image')
  for (const m of text.matchAll(/\bimage:\s*([^,\n}]+)/g)) {
    assert.equal(m[1].trim(), 'null', `useTabsPersistence writes image: ${m[1]}`)
  }
})
