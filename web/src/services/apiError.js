// Read the error message out of a non-OK API response. The proxy sends
// {"error": msg} (some routes add "message"); FastAPI's HTTPException sends
// {"detail": string | array}; proxies in front of it can send HTML or plain text.
// Parsing is shared with the cell-run path (runRequest.parseErrorBody), so every
// reader shows the same text. Never throws.
import { parseErrorBody } from './runRequest.js'

// FastAPI validation errors (422) are a list of {loc, msg, type}; show the
// messages ("body.name: field required") instead of the raw JSON.
function detailListMessage(json) {
  const d = json && typeof json === 'object' && !json.error ? json.detail : null
  if (!Array.isArray(d) || !d.length) return ''
  return d.map(item => {
    if (!item || typeof item !== 'object') return String(item)
    const loc = Array.isArray(item.loc) ? item.loc.join('.') : ''
    const msg = item.msg ?? JSON.stringify(item)
    return loc ? `${loc}: ${msg}` : String(msg)
  }).join('; ')
}

// `fallback` is used when the body carries no message (empty body, `{}`), e.g.
// `Launch failed: 502`. Defaults to `HTTP <status>`.
export async function readApiError(resp, fallback) {
  let text = ''
  try { text = await resp.text() } catch { /* body unreadable */ }
  const status = resp?.status
  const { message, json } = parseErrorBody(status, text, fallback || `HTTP ${status}`)
  return detailListMessage(json) || message
}
