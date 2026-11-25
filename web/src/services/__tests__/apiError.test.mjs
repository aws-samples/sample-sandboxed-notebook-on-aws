// Unit tests for services/apiError.js (readApiError).
// Run with: node --test web/src/services/__tests__/apiError.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readApiError } from '../apiError.js'

const resp = (status, body) => new Response(body, { status })

test('error with a quote and a newline is returned in full', async () => {
  const msg = 'Bucket "a-b" not found\nCheck the name'
  assert.equal(await readApiError(resp(404, JSON.stringify({ error: msg }))), msg)
})

test('{error, message}: error wins', async () => {
  const body = JSON.stringify({ error: 'throttled', message: 'try later' })
  assert.equal(await readApiError(resp(429, body)), 'throttled')
})

test('{detail: string} from an HTTPException', async () => {
  assert.equal(await readApiError(resp(401, JSON.stringify({ detail: 'Unauthorized' }))), 'Unauthorized')
})

test('{detail: array} from a 422 shows the messages', async () => {
  const body = JSON.stringify({ detail: [
    { loc: ['body', 'name'], msg: 'field required', type: 'missing' },
    { loc: ['query', 'n'], msg: 'not an int', type: 'int_parsing' },
  ] })
  assert.equal(await readApiError(resp(422, body)), 'body.name: field required; query.n: not an int')
})

test('{message} only', async () => {
  assert.equal(await readApiError(resp(500, JSON.stringify({ message: 'boom' }))), 'boom')
})

test('plain-text and HTML bodies', async () => {
  assert.equal(await readApiError(resp(502, 'Bad Gateway\n')), 'Bad Gateway')
  assert.equal(await readApiError(resp(502, '<html><body><h1>502 Bad Gateway</h1></body></html>')), '502 Bad Gateway')
})

test('empty body and {} use the fallback', async () => {
  assert.equal(await readApiError(resp(503, ''), 'Launch failed: 503'), 'Launch failed: 503')
  assert.equal(await readApiError(resp(500, '{}')), 'HTTP 500')
})

test('unreadable body never throws', async () => {
  const broken = { status: 500, text: () => Promise.reject(new Error('locked')) }
  assert.equal(await readApiError(broken, 'Delete failed: 500'), 'Delete failed: 500')
})
