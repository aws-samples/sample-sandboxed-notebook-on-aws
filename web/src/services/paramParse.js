/**
 * paramParse — pure @param annotation parsers (no React), extracted from
 * components/ParamWidgets.jsx so services (appViewModel, lineage) load under plain
 * Node. ParamWidgets re-exports these, so existing importers keep working.
 */

/**
 * Parse @param annotations from cell code.
 * Returns array of: { varName, type, config, lineIndex, valueLine }
 */
export function parseParams(code) {
  if (!code || !code.includes('@param')) return []

  const lines = code.split('\n')
  const params = []

  for (let i = 0; i < lines.length - 1; i++) {
    const line = lines[i].trim()
    // Match: # @param {type: "slider", ...}
    const match = line.match(/^#\s*@param\s*(\{.+\})\s*$/)
    if (!match) continue

    // Parse the JSON config. Prefer strict JSON (preserves apostrophes in custom
    // labels); fall back to single-quote→double-quote for author-written configs.
    let config
    try {
      config = JSON.parse(match[1])
    } catch {
      try {
        config = JSON.parse(match[1].replace(/'/g, '"'))
      } catch {
        continue
      }
    }

    // Next non-empty line should be the variable assignment
    let assignLine = i + 1
    while (assignLine < lines.length && !lines[assignLine].trim()) assignLine++
    if (assignLine >= lines.length) continue

    const assignMatch = lines[assignLine].match(/^(\w+)\s*=\s*(.+)$/)
    if (!assignMatch) continue

    const varName = assignMatch[1]
    const currentValue = assignMatch[2].trim()

    params.push({
      varName,
      type: config.type || 'text',
      config,
      paramLineIndex: i,
      valueLineIndex: assignLine,
      currentValue,
    })
  }

  return params
}

/**
 * Normalize a dropdown's `options` into a uniform [{label, value}] list.
 * Accepts either the legacy flat form (["US","UK"]) — where label === value === the
 * string — or the object form ([{label:"Sales (11)", value:11}]) where `value` may
 * be a non-string (int/float/bool) that gets written into the code as-is.
 * Malformed entries are skipped defensively.
 */
export function normalizeOptions(options) {
  if (!Array.isArray(options)) return []
  const out = []
  for (const opt of options) {
    if (opt && typeof opt === 'object' && !Array.isArray(opt)) {
      if (!('value' in opt)) continue
      out.push({ label: String(opt.label ?? opt.value), value: opt.value })
    } else {
      // Scalar (string/number/bool): label and value are the same.
      out.push({ label: String(opt), value: opt })
    }
  }
  return out
}

/**
 * Parse current Python value from code.
 */
export function parsePythonValue(valueStr, type) {
  const trimmed = valueStr.trim()
  if (type === 'checkbox') return trimmed === 'True'
  if (type === 'dropdown') return parseLiteral(trimmed)
  if (type === 'text' || type === 'date') {
    return trimmed.replace(/^["']|["']$/g, '')
  }
  const num = Number(trimmed)
  return isNaN(num) ? trimmed : num
}

/** Inverse of formatLiteral: a Python literal token → its typed JS value. */
function parseLiteral(trimmed) {
  if (trimmed === 'True') return true
  if (trimmed === 'False') return false
  if (trimmed === 'None') return null
  // Quoted string → strip quotes (and unescape \").
  if (/^["'].*["']$/.test(trimmed)) {
    return trimmed.slice(1, -1).replace(/\\"/g, '"')
  }
  // Bare numeric → number; anything else → the raw token as a string.
  const num = Number(trimmed)
  return trimmed !== '' && !isNaN(num) ? num : trimmed
}
