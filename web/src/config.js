/**
 * Runtime configuration derived from Vite environment variables.
 * Set VITE_PROXY_PORT and VITE_BACKEND_PORT when launching the dev server.
 * Falls back to defaults if not set.
 */
export const PROXY_PORT = import.meta.env.VITE_PROXY_PORT || '8081'
export const BACKEND_PORT = import.meta.env.VITE_BACKEND_PORT || '8080'

export const PROXY_URL = `http://localhost:${PROXY_PORT}`
export const BACKEND_URL = `http://localhost:${BACKEND_PORT}`

// Shared secret for the authenticated proxy (C1). The installer mints a per-run
// token and injects it as VITE_PROXY_TOKEN; the UI sends it in the X-Proxy-Auth
// header (and the terminal WS subprotocol). Empty string disables injection so
// the UI still works against an unauthenticated proxy in local dev.
export const PROXY_TOKEN = import.meta.env.VITE_PROXY_TOKEN || ''

// Header helper for RAW fetch() calls to the proxy (e.g. `${tab.microvmEndpoint}/...`)
// that build their own headers object instead of going through apiFetch/
// fetchWithTimeout. Attaches the C1 X-Proxy-Auth header carrying PROXY_TOKEN when a
// token is set, otherwise returns the extras unchanged — fail-open on token absence,
// matching apiFetch.js so the UI still works against an unauthenticated proxy.
export function authHeaders(extra = {}) {
  return PROXY_TOKEN ? { ...extra, 'X-Proxy-Auth': PROXY_TOKEN } : { ...extra }
}

// Network timeout for API calls (ms)
// AWS API calls (Glue, DynamoDB, S3) can be slow under throttling
export const API_TIMEOUT_MS = 30000

// Longer timeout for AI generation calls (model inference takes 10-30s)
export const AI_TIMEOUT_MS = 60000

// The agentic "Fix" runs a verify-before-suggest loop: it generates a candidate
// fix, EXECUTES it in the VM, learns from the failure, and reworks — up to
// FIX_VERIFY_MAX_ATTEMPTS (backend, default 4) times. Each round is a model call
// plus one or more tool round-trips, so end-to-end can run well past AI_TIMEOUT_MS
// (observed ~75s for 7 tool calls). Give it a dedicated, generous ceiling so the
// frontend doesn't abort a fix that's about to succeed and leave the UI spinning.
export const AI_FIX_TIMEOUT_MS = 180000
// "Explain this code": one model call (+ at most one JSON retry) on the proxy,
// which enforces its own 75 s deadline; this ceiling sits just above it.
export const AI_EXPLAIN_TIMEOUT_MS = 90000

// How many runs to show in a notebook's Run History card (UI display concern).
// When the "Run All" filter is on, the server returns the last N of that kind.
export const RUN_HISTORY_LIMIT = 15
