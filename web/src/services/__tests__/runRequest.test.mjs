// Unit tests for services/runRequest.js (cell-run error parsing, recovery retry
// policy, data-source gate, endpoint recovery).
// Run with: node --test web/src/services/__tests__/runRequest.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseErrorBody, readErrorResponse, stripHtml, isRetryableRecovery, shouldRetryRecovery,
  batchFailureError, runGate, recoverEndpoint, SOURCES_LOADING_NOTICE,
} from '../runRequest.js'

const RECOVERY_BODY = JSON.stringify({
  error: "Couldn't restore your session onto a new VM after retries. Your work is safe in storage — please retry.",
  recovery: 'failed', retryable: true, session_safe: true, session_id: 's1', detail: 'TimeoutError()',
})

test('parseErrorBody: JSON {error} wins over detail and is kept in full', () => {
  const long = 'x'.repeat(5000)
  const r = parseErrorBody(503, JSON.stringify({ error: long, detail: 'raw' }))
  assert.equal(r.status, 503)
  assert.equal(r.message, long)
  assert.equal(r.json.detail, 'raw')
})

test('parseErrorBody: FastAPI {detail} (401 auth) is used when there is no error', () => {
  const r = parseErrorBody(401, '{"detail": "invalid or missing proxy auth"}')
  assert.equal(r.message, 'invalid or missing proxy auth')
})

test('parseErrorBody: a non-string detail (422 list) is stringified', () => {
  const r = parseErrorBody(422, JSON.stringify({ detail: [{ loc: ['body'], msg: 'field required' }] }))
  assert.match(r.message, /field required/)
})

test('parseErrorBody: the proxy 400 X-Session-Id body', () => {
  const r = parseErrorBody(400, '{"error": "X-Session-Id header required"}')
  assert.equal(r.message, 'X-Session-Id header required')
})

test('parseErrorBody: non-JSON text is used as-is (trimmed)', () => {
  const r = parseErrorBody(500, '  Internal Server Error\n')
  assert.equal(r.message, 'Internal Server Error')
  assert.equal(r.json, null)
})

test('parseErrorBody: an HTML error page is reduced to its text', () => {
  const html = '<!DOCTYPE html><html><head><title>502 Bad Gateway</title><style>body{color:red}</style></head>'
    + '<body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx &amp; co</center></body></html>'
  const r = parseErrorBody(502, html)
  assert.equal(r.message, '502 Bad Gateway 502 Bad Gateway nginx & co')
  assert.doesNotMatch(r.message, /[<>]/)
})

test('parseErrorBody: an empty body falls back to the status text', () => {
  assert.equal(parseErrorBody(502, '', 'Bad Gateway').message, 'Bad Gateway')
  assert.equal(parseErrorBody(502, '').message, 'no error details in the response')
  assert.equal(parseErrorBody(500, '{}', 'Server Error').message, 'Server Error')
})

test('stripHtml: drops scripts and tags', () => {
  assert.equal(stripHtml('<p>a<script>alert(1)</script> <b>b</b></p>'), 'a b')
})

test('readErrorResponse: reads text from a Response-like object; never throws', async () => {
  const ok = await readErrorResponse({ status: 503, statusText: 'Service Unavailable', text: async () => RECOVERY_BODY })
  assert.equal(ok.status, 503)
  assert.equal(ok.json.recovery, 'failed')
  const broken = await readErrorResponse({ status: 502, statusText: 'Bad Gateway', text: async () => { throw new Error('x') } })
  assert.equal(broken.message, 'Bad Gateway')
})

test('retry policy: 503 recovery retryable → retry once', () => {
  const { status, json } = parseErrorBody(503, RECOVERY_BODY)
  assert.equal(isRetryableRecovery(status, json), true)
  assert.equal(shouldRetryRecovery(status, json, false), true)
  assert.equal(shouldRetryRecovery(status, json, true), false)
})

test('retry policy: 401 / 400 / plain 503 / non-retryable recovery → no retry', () => {
  for (const [status, body] of [
    [401, '{"detail": "invalid or missing proxy auth"}'],
    [400, '{"error": "X-Session-Id header required"}'],
    [503, '{"error": "busy"}'],
    [503, JSON.stringify({ error: 'x', recovery: 'failed', retryable: false })],
    [502, RECOVERY_BODY],
    [503, '<html><body>Service Unavailable</body></html>'],
  ]) {
    const p = parseErrorBody(status, body)
    assert.equal(shouldRetryRecovery(p.status, p.json, false), false, `${status} ${body}`)
  }
})

test('batchFailureError: full message with the HTTP status, labelled by trigger', () => {
  const f = parseErrorBody(503, RECOVERY_BODY)
  assert.equal(batchFailureError('run_all', f), `Run All failed: ${f.message} (HTTP 503)`)
  assert.equal(batchFailureError('run_from', parseErrorBody(401, '{"detail":"nope"}')), 'Run failed: nope (HTTP 401)')
})

test('runGate: sources not ready + not suspended → blocked with the notice', () => {
  assert.deepEqual(runGate({ sourcesReady: false, suspended: false }), { allowed: false, notice: SOURCES_LOADING_NOTICE })
  assert.equal(runGate({ sourcesReady: undefined, suspended: false }).allowed, false)
})

test('runGate: suspended VM → allowed (avoids the resume deadlock)', () => {
  assert.deepEqual(runGate({ sourcesReady: false, suspended: true }), { allowed: true, notice: null })
})

test('runGate: sources ready → allowed', () => {
  assert.equal(runGate({ sourcesReady: true, suspended: false }).allowed, true)
  assert.equal(runGate({ sourcesReady: true, suspended: true }).allowed, true)
})

test('recoverEndpoint: keeps a present endpoint, rebuilds a missing one only when the VM is listed', () => {
  const base = 'http://localhost:8081/proxy'
  assert.deepEqual(recoverEndpoint('http://e/proxy', null, base), { endpoint: 'http://e/proxy', recovered: false })
  assert.deepEqual(recoverEndpoint(null, { endpoint: 'vm.example' }, base), { endpoint: base, recovered: true })
  assert.deepEqual(recoverEndpoint(null, undefined, base), { endpoint: null, recovered: false })
  assert.deepEqual(recoverEndpoint('', { state: 'RUNNING' }, base), { endpoint: null, recovered: false })
})
