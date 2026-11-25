// Text for the error screen's "Copy details" button (components/ErrorBoundary.jsx):
// what threw, where (React component stack) and when, so a crash report can name
// the throwing component. The last report is also kept in localStorage under
// LAST_ERROR_KEY: "Reload Application" otherwise loses it.

export const LAST_ERROR_KEY = 'microvm-last-error'

export function errorDetails(error, info, { scope = 'app', at = new Date(), url = '' } = {}) {
  const message = (error && error.message) || String(error ?? 'Unknown error')
  const lines = [
    `Error: ${message}`,
    `Scope: ${scope}`,
    `Time: ${at.toISOString()}`,
  ]
  if (url) lines.push(`URL: ${url}`)
  if (error?.stack) lines.push('', 'Stack:', String(error.stack))
  if (info?.componentStack) lines.push('', 'Component stack:', String(info.componentStack).trim())
  return lines.join('\n')
}

export function saveLastError(text, storage = globalThis.localStorage) {
  try { storage.setItem(LAST_ERROR_KEY, text) } catch { /* best-effort */ }
}
