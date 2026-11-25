// Pure helpers for the AI chat's model-fallback badge (AiChatPanel.jsx).
//
// The agent streams a structured frame at the start of every interactive turn:
//   { type: 'model_status', requested, active, reason, cooldown_remaining_s }
// where requested/active are BASE model ids (the dropdown's values), reason is
// 'cooldown' | 'throttled' | 'unavailable' | null, and cooldown_remaining_s is a
// whole number of seconds or null. reason null with active === requested means no
// fallback is in effect.

// Display name for a base id, looked up in the curated list from GET /ai/models.
// Falls back to the id itself only when the list does not know the model.
export function modelDisplayName(models, id) {
  if (!id) return ''
  const row = (models || []).find(m => m && m.id === id)
  return (row && (row.display_name || row.id)) || id
}

// '~N min' wording for a cooldown, rounded up and at least 1 minute. Null when there
// is no usable remaining time (the caller then omits the estimate).
export function formatCooldownMinutes(seconds) {
  const s = Number(seconds)
  if (!Number.isFinite(s) || s <= 0) return null
  return `${Math.max(1, Math.ceil(s / 60))} min`
}

// The badge for the selected model, or null when nothing should be shown.
// Shown only when the status belongs to the currently selected model (so switching
// the dropdown hides it) and a fallback is actually in effect.
// Returns { text, title } where text is the visible one-liner and title the full
// tooltip (the same string, so nothing is lost when CSS ellipsis trims the line).
export function fallbackBadge(status, selectedId, models) {
  if (!status || !selectedId) return null
  if (status.requested !== selectedId) return null
  if (!status.reason || status.active === status.requested) return null

  const requested = modelDisplayName(models, status.requested)
  const active = modelDisplayName(models, status.active)
  const eta = formatCooldownMinutes(status.cooldown_remaining_s)

  let detail
  if (status.reason === 'cooldown') {
    detail = `${requested} cooling down${eta ? `, ~${eta}` : ''}`
  } else if (status.reason === 'throttled') {
    detail = `${requested} throttled${eta ? `, ~${eta}` : ''}`
  } else if (status.reason === 'unavailable') {
    detail = `${requested} unavailable`
  } else {
    return null
  }

  const text = `Using ${active} (${detail})`
  return { text, title: text }
}
