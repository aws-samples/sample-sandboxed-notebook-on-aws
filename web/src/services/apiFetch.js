/**
 * Thin fetch wrapper for the authenticated proxy (C1).
 *
 * Resolves a path against PROXY_URL and injects the X-Proxy-Auth header carrying
 * PROXY_TOKEN when a token is set. Drop-in replacement for the old bare proxy
 * fetch call sites — pass the path (leading slash optional) instead of the full
 * URL:
 *
 *   const resp = await apiFetch('/instances')
 *   const resp = await apiFetch(`/notebooks/${id}`, { method: 'PUT', body })
 *
 * The header is only attached when PROXY_TOKEN is non-empty, so the UI still
 * works against an unauthenticated proxy in local dev.
 */

import { PROXY_URL, PROXY_TOKEN } from '../config'

export function apiFetch(path, options = {}) {
  const url = `${PROXY_URL}${path}`
  const headers = { ...(options.headers || {}) }
  if (PROXY_TOKEN) {
    headers['X-Proxy-Auth'] = PROXY_TOKEN
  }
  return fetch(url, { ...options, headers })
}
