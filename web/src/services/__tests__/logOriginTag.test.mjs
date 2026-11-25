// Unit tests for services/logOriginTag.js (the Logs panel's [agent]/[internal] tag parser).
// Run with: node --test web/src/services/__tests__/logOriginTag.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseOriginTag } from '../logOriginTag.js'

const TS = '2026-09-04 22:31:24,932 [INFO] '

for (const origin of ['agent', 'internal']) {
  test(`${origin}: ▶ / ✓ / ✗ lines without a prefix`, () => {
    assert.deepEqual(parseOriginTag(`▶ [${origin}] Executing code (len=5)`),
      { origin, message: '▶ Executing code (len=5)' })
    assert.deepEqual(parseOriginTag(`  ✓ [${origin}] OK (3ms) scratch_vars=['df'] (len=5)`),
      { origin, message: "  ✓ OK (3ms) scratch_vars=['df'] (len=5)" })
    assert.deepEqual(parseOriginTag(`  ✗ [${origin}] ERROR (3ms) [KeyError] (len=5)`),
      { origin, message: '  ✗ ERROR (3ms) [KeyError] (len=5)' })
  })

  test(`${origin}: ▶ / ✓ / ✗ lines keep the timestamp [LEVEL] prefix`, () => {
    assert.deepEqual(parseOriginTag(`${TS}▶ [${origin}] Executing code (len=5)`),
      { origin, message: `${TS}▶ Executing code (len=5)` })
    assert.deepEqual(parseOriginTag(`${TS}  ✓ [${origin}] OK (3ms)`),
      { origin, message: `${TS}  ✓ OK (3ms)` })
    const W = '2026-09-04 22:31:24.932 [WARNING] '
    assert.deepEqual(parseOriginTag(`${W}  ✗ [${origin}] ERROR (3ms)`),
      { origin, message: `${W}  ✗ ERROR (3ms)` })
  })
}

test('indented error-detail line', () => {
  assert.deepEqual(parseOriginTag("    [internal] KeyError: 'x'"),
    { origin: 'internal', message: "    KeyError: 'x'" })
  assert.deepEqual(parseOriginTag(`${TS}    [agent] NameError: name 'q' is not defined`),
    { origin: 'agent', message: `${TS}    NameError: name 'q' is not defined` })
})

test('user lines are untouched', () => {
  const lines = ['▶ Executing code (len=5)', "  ✓ OK (3ms) vars=['a'] (len=5)", `${TS}▶ Executing code (len=5)`]
  for (const l of lines) assert.deepEqual(parseOriginTag(l), { origin: null, message: l })
})

test('a stray mid-message tag is not a tag', () => {
  assert.deepEqual(parseOriginTag('foo [internal] bar'), { origin: null, message: 'foo [internal] bar' })
  assert.deepEqual(parseOriginTag(`${TS}#5 [internal] load build definition`),
    { origin: null, message: `${TS}#5 [internal] load build definition` })
  assert.deepEqual(parseOriginTag('[other] x'), { origin: null, message: '[other] x' })
})

test('non-strings pass through', () => {
  assert.deepEqual(parseOriginTag(null), { origin: null, message: null })
  assert.deepEqual(parseOriginTag(undefined), { origin: null, message: undefined })
  assert.deepEqual(parseOriginTag(42), { origin: null, message: 42 })
})
