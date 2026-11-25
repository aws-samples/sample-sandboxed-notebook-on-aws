// Pure helpers shared by the two cell-execution paths in hooks/useNotebookCells.js
// (executeCell → /execute|/execute-sql, executeBatch → /execute-batch). Kept free of
// React/DOM so the decisions are node-testable (see __tests__/runRequest.test.mjs).

// Delay before the one-shot retry after a reactive-recovery 503 (eternal mode).
export const RECOVERY_RETRY_DELAY_MS = 3000

// Shown on a single cell while the one-shot recovery retry is pending.
export const RECONNECTING_OUTPUT = '🔄 Reconnecting your session… (your work is safe)'

// Shown on a single cell when the recovery retry also failed.
export const RECOVERY_EXHAUSTED_ERROR = '⚠️ Couldn\'t reconnect your session automatically. Your work is safe in storage — use "♻ Restore Session" in the connection panel to restore it on a new MicroVM.'

// The notice shown when a run is blocked because the data-source list hasn't loaded.
export const SOURCES_LOADING_NOTICE = 'Loading data sources… run again once the Data panel finishes loading.'

const HTML_RE = /<\s*(!doctype|html|head|body|title|h[1-6]|p|div|pre|center|hr|br)\b/i

// Plain text of an HTML error page (e.g. an nginx/ALB 502 page). Drops script/style
// blocks and tags, decodes the common entities and collapses whitespace.
export function stripHtml(html) {
  return String(html)
    .replace(/<(script|style)\b[\s\S]*?<\/\1\s*>/gi, ' ')
    .replace(/<[^>]*>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, '\'')
    .replace(/&amp;/gi, '&')
    .replace(/\s+/g, ' ')
    .trim()
}

function _messageFromJson(json) {
  if (!json || typeof json !== 'object') return ''
  // The proxy routes send {"error": ...} (the recovery 503 also carries a raw
  // `detail`, so `error` wins); FastAPI's HTTPException (e.g. the 401 auth check)
  // sends {"detail": ...}, which is a list of objects for a 422.
  for (const key of ['error', 'detail', 'message']) {
    const v = json[key]
    if (v == null || v === '') continue
    return typeof v === 'string' ? v : JSON.stringify(v)
  }
  return ''
}

// Parse a non-OK HTTP response body into { status, message, json }. `json` is the
// parsed object (null when the body is not JSON). The message is never truncated.
export function parseErrorBody(status, text, statusText = '') {
  const raw = text == null ? '' : String(text)
  let json = null
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') json = parsed
  } catch { /* not JSON */ }
  let message = json ? _messageFromJson(json) : ''
  if (!message && !json) message = HTML_RE.test(raw) ? stripHtml(raw) : raw.trim()
  if (!message) message = statusText || 'no error details in the response'
  return { status, message, json }
}

// Read a fetch Response that is not OK. Never throws (an unreadable body still
// yields a message from the status text).
export async function readErrorResponse(resp) {
  let text = ''
  try { text = await resp.text() } catch { /* body unreadable */ }
  return parseErrorBody(resp.status, text, resp.statusText || '')
}

// True for the proxy's reactive-recovery failure (eternal mode): the VM died and
// a replacement couldn't be restored yet, but state is safe and the next request
// re-attempts recovery from the same checkpoint.
export function isRetryableRecovery(status, json) {
  return status === 503 && !!json && json.recovery === 'failed' && !!json.retryable
}

// The shared retry policy: retry ONCE after a retryable recovery 503, never for
// anything else (401/400/404/502/... surface straight away).
export function shouldRetryRecovery(status, json, alreadyRetried) {
  return !alreadyRetried && isRetryableRecovery(status, json)
}

// User-visible error put on every not-yet-finished cell of a batch that the
// server rejected before streaming. Full server message, no truncation.
export function batchFailureError(trigger, failure) {
  const label = trigger === 'run_all' ? 'Run All' : 'Run'
  return `${label} failed: ${failure.message} (HTTP ${failure.status})`
}

// The data-source-ready gate (shared by executeCell and executeBatch). SQL routing
// needs each source's TYPE, so nothing runs before the source list loads. A
// SUSPENDED VM is the exception: it skips its fetches while asleep, so the list
// stays unloaded until a run wakes it; blocking would deadlock.
export function runGate({ sourcesReady, suspended }) {
  if (sourcesReady || suspended) return { allowed: true, notice: null }
  return { allowed: false, notice: SOURCES_LOADING_NOTICE }
}

// Endpoint for this run. A tab restored after a page refresh can have a null
// microvmEndpoint; if the VM is still listed in /instances the run goes through
// the session-routed proxy base. `recovered` = the endpoint was rebuilt here, so
// the caller treats the tab as connected for this run.
export function recoverEndpoint(currentEndpoint, instance, proxyBase) {
  if (currentEndpoint) return { endpoint: currentEndpoint, recovered: false }
  if (instance && instance.endpoint) return { endpoint: proxyBase, recovered: true }
  return { endpoint: null, recovered: false }
}
