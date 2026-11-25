// DataChart plot geometry. Pure module (no imports) so it can be unit-checked with Node.
// The Apple-style themes (light and dark share theme-light.css) fill the container (width
// 100%, like every theme) with slightly larger tick labels than ember (9px viewBox units
// vs 8px), so the left margin and the x-label step grow to keep labels from overlapping.
// CH below is sized for 11px glyphs, which over-reserves for 9px — deliberately
// conservative. Ember (and any unknown theme) gets today's constants back unchanged
// (same object / same step), so its geometry is identical.
export const W = 720, H = 320
export const BASE_M = Object.freeze({ top: 16, right: 16, bottom: 56, left: 64 })
const CH = 6.6        // px per glyph at 11px system UI font (0.6em; conservative for digits/commas)
const TICK_GAP = 8    // tick label → y axis (today's M.left - 8)
const TITLE_BAND = 24 // rotated y title at x=14, 12px → occupies x≈8–20, plus 4px clearance
const X_GAP = 8       // minimum gap between neighboring x labels
const maxLen = (labels) => labels.reduce((m, s) => Math.max(m, String(s).length), 0)
// Themes whose chart labels use the shared Apple-style sizing (theme-light.css).
const SPACED = new Set(['light', 'dark'])

// Other themes (ember) get BASE_M back unchanged (same object), so their geometry is identical to today.
export function chartMargins(theme, yTickLabels) {
  if (!SPACED.has(theme)) return BASE_M
  return { ...BASE_M, left: Math.max(BASE_M.left, TITLE_BAND + TICK_GAP + Math.ceil(maxLen(yTickLabels) * CH)) }
}

// baseEvery is today's value (ceil(n/12) for bar/line, ceil(n/8) for histogram).
export function labelStep(theme, n, baseEvery, labels, plotWidth) {
  if (!SPACED.has(theme) || n === 0) return baseEvery
  const need = maxLen(labels) * CH + X_GAP
  return Math.max(baseEvery, Math.ceil((n * need) / plotWidth))
}
