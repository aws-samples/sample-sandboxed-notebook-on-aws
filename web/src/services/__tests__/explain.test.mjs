// Unit tests for services/explain.js (pure helpers for "Explain this code").
// Run with: node --test web/src/services/__tests__/explain.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  hashText, upstreamSig, explainKey, sliceLines, splitLines, normalizeResult, buildNotesBlock,
  appendToNotes, isAlreadyAdded, relativeAge, getExplain, setExplain, dismissExplain, explainStoreSize,
} from '../explain.js'
import { graphFromDag } from '../depGraph.js'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', 'tests', 'fixtures')
const scenarios = (name) => JSON.parse(readFileSync(join(FIXTURES, name), 'utf8')).scenarios
const LIN = scenarios('dag_lineage.json').base
const EXP = scenarios('dag_explain.json')

test('sliceLines: first, last, single, clamp, CRLF', () => {
  const code = 'a\nb\nc\nd'
  assert.equal(sliceLines(code, 1, 2), 'a\nb')
  assert.equal(sliceLines(code, 3, 4), 'c\nd')
  assert.equal(sliceLines(code, 2, 2), 'b')
  assert.equal(sliceLines(code, 0, 99), 'a\nb\nc\nd')
  assert.equal(sliceLines(code, 4, 2), 'd')
  assert.equal(sliceLines('a\r\nb\r\nc\r\n', 2, 3), 'b\nc')
  assert.equal(sliceLines('', 1, 3), '')
  assert.deepEqual(splitLines('a\nb\n\n'), ['a', 'b'])
})

test('explainKey changes with cell code and upstream code, not with unrelated cells', () => {
  const graph = graphFromDag(LIN.doc)
  const cells = LIN.cells.map((c) => ({ ...c }))
  const sig = (cs) => upstreamSig(graph, cs, '3')
  const key = (cs, code) => explainKey({ notebookId: 1, cellId: '3', code, upstreamSig: sig(cs) })
  const base = key(cells, cells[2].code)
  assert.equal(base, key(cells, cells[2].code))
  assert.notEqual(base, key(cells, cells[2].code + ' '))
  const upChanged = cells.map((c) => (c.id === '2' ? { ...c, code: c.code + '\n-- x' } : c))
  assert.notEqual(base, key(upChanged, cells[2].code))
  const transitive = cells.map((c) => (c.id === '1' ? { ...c, code: 'region = "eu"' } : c))
  assert.notEqual(base, key(transitive, cells[2].code))
  const unrelated = cells.map((c) => (c.id === '5' ? { ...c, code: 'other = 2' } : c))
  assert.equal(base, key(unrelated, cells[2].code))
  assert.notEqual(base, explainKey({ notebookId: 2, cellId: '3', code: cells[2].code, upstreamSig: sig(cells) }))
})

test('upstreamSig: ordered by position, markdown excluded, many ancestors', () => {
  const g = graphFromDag(EXP.diamond.doc)
  const s = upstreamSig(g, EXP.diamond.cells, 'd')
  assert.deepEqual(s.split('|').map((x) => x.split(':')[0]), ['a', 'b', 'c'])
  const m = EXP.many_ancestors
  const ms = upstreamSig(graphFromDag(m.doc), m.cells, 'm30')
  assert.equal(ms.split('|').length, 30)
  assert.equal(hashText('abc'), hashText('abc'))
  assert.notEqual(hashText('abc'), hashText('abd'))
})

const CODE = 'import x\nmonthly = df.groupby("m").sum()\nmonthly'
const RESULT = normalizeResult({
  overview: 'Sums by month.',
  sections: [
    { title: 'Setup', start_line: 1, end_line: 1, explanation: 'Imports.' },
    { title: 'Aggregate', start_line: 2, end_line: 3, explanation: 'Groups rows.' },
  ],
  depends_on: [{ cell: 'SQL-1', provides: ['df'], why: 'Reads orders.' }],
  notes: '',
}, CODE)

test('normalizeResult: repairs ranges and falls back to one section', () => {
  const r = normalizeResult({ overview: 'O', sections: [
    { title: '', start_line: 2, end_line: 9, explanation: 'b' },
    { title: 'A', start_line: 1, end_line: 1, explanation: 'a' }] }, CODE)
  assert.deepEqual(r.sections.map((s) => [s.start_line, s.end_line]), [[1, 1], [2, 3]])
  assert.equal(r.sections[1].title, 'Lines 2-3')
  const f = normalizeResult({ overview: 'O', sections: 'x' }, CODE)
  assert.equal(f.sections.length, 1)
  assert.deepEqual([f.sections[0].start_line, f.sections[0].end_line], [1, 3])
  assert.equal(normalizeResult(null, CODE).sections.length, 1)
})

test('notes block format', () => {
  const block = buildNotesBlock(RESULT, { code: CODE, lang: 'python', date: '2026-05-01' })
  const lines = block.split('\n')
  assert.equal(lines[0], '### Explanation (2026-05-01)')
  assert.ok(block.includes('Sums by month.'))
  assert.ok(block.includes('#### Setup\n\n```python\nimport x\n```\n\nImports.'))
  assert.ok(block.includes('#### Aggregate\n\n```python\nmonthly = df.groupby("m").sum()\nmonthly\n```\n\nGroups rows.'))
  assert.ok(block.includes('**Depends on:** SQL-1 (df) — Reads orders.'))
  assert.ok(buildNotesBlock(RESULT, { code: CODE, lang: 'sql', date: 'd' }).includes('```sql'))
})

test('single-section result: exactly one fenced block with the whole cell', () => {
  const r = normalizeResult({ overview: 'O', sections: [{ title: 'All', start_line: 1, end_line: 3, explanation: 'E' }] }, CODE)
  const block = buildNotesBlock(r, { code: CODE, lang: 'python', date: 'd' })
  assert.equal((block.match(/```python/g) || []).length, 1)
  assert.ok(block.includes('```python\n' + CODE + '\n```'))
})

test('code containing backtick fences gets a longer fence', () => {
  const code = 'x = """\n```\n"""'
  const r = normalizeResult({ overview: 'O', sections: [{ title: 'A', start_line: 1, end_line: 3, explanation: 'E' }] }, code)
  assert.ok(buildNotesBlock(r, { code, date: 'd' }).includes('````python'))
})

test('appendToNotes: empty, existing, idempotent detection', () => {
  const block = buildNotesBlock(RESULT, { code: CODE, lang: 'python', date: '2026-05-01' })
  assert.equal(appendToNotes('', block), block)
  assert.equal(appendToNotes('  \n', block), block)
  const next = appendToNotes('my note\n', block)
  assert.equal(next, 'my note\n\n---\n\n' + block)
  assert.ok(next.startsWith('my note'))
  assert.equal(isAlreadyAdded('my note', block), false)
  assert.equal(isAlreadyAdded(next, block), true)
  const otherDay = buildNotesBlock(RESULT, { code: CODE, lang: 'python', date: '2030-01-01' })
  assert.equal(isAlreadyAdded(next, otherDay), true)
  const again = isAlreadyAdded(next, block) ? next : appendToNotes(next, block)
  assert.equal(again, next)
})

test('relativeAge', () => {
  const now = 1_000_000_000
  assert.equal(relativeAge(now - 5_000, now), 'just now')
  assert.equal(relativeAge(now - 3 * 60_000, now), '3 min ago')
  assert.equal(relativeAge(now - 2 * 3_600_000, now), '2 h ago')
  assert.equal(relativeAge(now - 3 * 86_400_000, now), '3 d ago')
})

test('store: set/get/dismiss and LRU bound', () => {
  setExplain('nb', 'c1', { key: 'k1' })
  assert.equal(getExplain('nb', 'c1').key, 'k1')
  dismissExplain('nb', 'c1')
  assert.equal(getExplain('nb', 'c1'), undefined)
  for (let i = 0; i < 250; i++) setExplain('nb', `cell${i}`, { key: String(i) })
  assert.ok(explainStoreSize() <= 200)
  assert.equal(getExplain('nb', 'cell0'), undefined)
  assert.equal(getExplain('nb', 'cell249').key, '249')
})
