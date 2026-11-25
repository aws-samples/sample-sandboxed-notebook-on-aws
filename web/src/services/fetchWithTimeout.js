/**
 * Fetch wrapper that adds an automatic timeout via AbortController.
 * Drop-in replacement for `fetch()` — same signature + optional timeout override.
 *
 * Usage:
 *   import { fetchWithTimeout } from '../services/fetchWithTimeout'
 *   const resp = await fetchWithTimeout(url, { method: 'POST', body: ... })
 *   const resp = await fetchWithTimeout(url, { timeout: 60000 })  // override
 */

import { API_TIMEOUT_MS, PROXY_TOKEN } from '../config'

export async function fetchWithTimeout(url, options = {}) {
  const { timeout = API_TIMEOUT_MS, ...fetchOptions } = options

  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeout)

  // If caller already provided a signal, chain them
  if (fetchOptions.signal) {
    const externalSignal = fetchOptions.signal
    externalSignal.addEventListener('abort', () => controller.abort())
  }

  // Every fetchWithTimeout target is the authenticated proxy (PROXY_URL or the
  // tab's microvmEndpoint, which is `${PROXY_URL}/proxy`), so inject the C1
  // X-Proxy-Auth header here too when a token is set.
  const headers = { ...(fetchOptions.headers || {}) }
  if (PROXY_TOKEN) {
    headers['X-Proxy-Auth'] = PROXY_TOKEN
  }

  try {
    const resp = await fetch(url, { ...fetchOptions, headers, signal: controller.signal })
    return resp
  } finally {
    clearTimeout(timeoutId)
  }
}
