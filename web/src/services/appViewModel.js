/**
 * appViewModel — pure helpers that define the "App view" projection of a notebook:
 * which cells are presentation content, the display variable/label for a cell's
 * output, and the flattened list of @param inputs. Shared by the live AppView
 * component and the static App export builders so both agree on what an app shows.
 */
import { parseParams, parsePythonValue, normalizeOptions } from './paramParse.js'
import { hasImageOutput } from './cellOutput.js'
import { cellOutputItems, isMultiOutput } from './cellOutputs.js'

// Upper bound on text output length App mode will render. A genuine formatted
// report (a per-row summary, an ask_bedrock_kb answer) should show; but a runaway
// print (a 10k-line dump) would flood the app, so we cap very long output out.
// Well above any realistic hand-formatted report.
const APP_TEXT_MAX_CHARS = 20000

// Patterns whose ENTIRE output is unmistakably an intermediate/debug dump rather
// than presentation content — hidden even though they're text. Deliberately narrow
// (only clear introspection noise) so we don't drop real reports.
//   - a bare pandas .info() dump (starts with "<class 'pandas...DataFrame'>")
//   - a lone dtypes listing ("col  int64\ncol2  object\n... dtype: object")
//   - a lone shape/length print like "(500, 8)" or "Shape: (500, 8)"
const _NOISE_PATTERNS = [
  /^<class 'pandas\./,                         // df.info()
  /\bdtype:\s*object\s*$/,                     // df.dtypes tail
  /^\s*\(\d+,\s*\d+\)\s*$/,                     // bare "(rows, cols)"
  /^Shape:\s*\(\d+,\s*\d+\)\s*$/i,             // "Shape: (rows, cols)"
]

function looksLikeNoiseDump(text) {
  return _NOISE_PATTERNS.some(re => re.test(text))
}

// Explicit author override in the cell's code: `# @app-hide` forces a cell OUT of
// the app view, `# @app-show` forces it IN (even if it'd otherwise be hidden).
// Checked first so intent always wins over the heuristic.
function appDirective(cell) {
  const code = cell.code || ''
  if (/(^|\n)\s*#\s*@app-hide\b/i.test(code)) return 'hide'
  if (/(^|\n)\s*#\s*@app-show\b/i.test(code)) return 'show'
  return null
}

// Decide whether a cell is "presentation" content worth showing to a consumer.
// Philosophy (Option B): SHOW BY DEFAULT — the app should mirror what the notebook
// shows — and hide only things that clearly aren't presentation content. This is
// the opposite of the old "hide unless short" rule, which silently dropped genuine
// formatted text reports just for being long.
//   - `# @app-show` / `# @app-hide` in the code -> explicit override, wins.
//   - markdown with content -> show
//   - chart (image) or table/plotly (html) -> show
//   - text output -> SHOW, unless it's an obvious debug/introspection dump or it
//     exceeds the hard length ceiling (runaway print).
//   - no output at all (imports, loads, assignments) -> hide.
export function appShows(cell) {
  const directive = appDirective(cell)
  if (directive === 'hide') return false

  if (cell.type === 'markdown') return !!cell.code?.trim()
  if (hasImageOutput(cell) || cell.html) return true

  if (cell.output) {
    const text = cell.output.trim()
    if (!text) return false
    if (directive === 'show') return true            // author forced it in
    if (text.length > APP_TEXT_MAX_CHARS) return false  // runaway dump — cap out
    if (looksLikeNoiseDump(text)) return false          // clear introspection noise
    return true                                          // otherwise: show it
  }

  // A cell explicitly marked @app-show but with no output yet still shouldn't
  // render an empty block; it'll appear once it produces output.
  return false
}

// The name of the DataFrame/variable a cell's output represents, for a caption
// above the table (context now that code is hidden).
export function deriveDisplayVar(cell) {
  if (cell.type === 'sql') {
    if (cell.outputVariable) return cell.outputVariable
    const code = cell.code || ''
    const m = code.match(/\bFROM\s+dynamodb\."?([a-zA-Z_][\w-]*)"?/i)
      || code.match(/\bFROM\s+'\/tmp\/([^']+)'/i)
      || code.match(/\bFROM\s+read_(?:csv|json|parquet)\('[^']*\/([^'/]+)'\)/i)
      || code.match(/\bFROM\s+[a-zA-Z_]\w*\.([a-zA-Z_]\w*)/i)
      || code.match(/\bFROM\s+([a-zA-Z_]\w*)/i)
    if (m) return m[1].replace(/\.\w+$/, '').replace(/[^a-zA-Z0-9_]/g, '_') || 'result'
    return 'result'
  }
  // code cell: only meaningful when the cell renders a DataFrame table
  if (!cell.html) return null
  if (isMultiOutput(cell)) {
    // Several outputs: the caption is the name of the FIRST table (its label), never
    // derived from the raw envelope string or a `data-plotly` look inside it.
    const items = cellOutputItems(cell).filter(it => it.t !== 'text')
    const table = items.find(it => it.t === 'table')
    if (table && table.label) return table.label
    if (!items.length || items[0].t !== 'table') return null
  } else if (cell.html.includes('data-plotly="true"')) return null
  const lines = (cell.code || '').split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#'))
  if (!lines.length) return null
  const last = lines[lines.length - 1]
  if (/^(print|display|plot_\w+)\s*\(/.test(last)) return null
  const m = last.match(/^([A-Za-z_]\w*)/)
  if (!m) return null
  const name = m[1]
  const KEYWORDS = ['print', 'display', 'return', 'import', 'from', 'for', 'if', 'while', 'with', 'def', 'class', 'try', 'except']
  return KEYWORDS.includes(name) ? null : name
}

// Flatten every @param across all cells into a display-ready inputs list.
export function collectInputs(cells = []) {
  const inputs = []
  for (const cell of cells) {
    for (const p of parseParams(cell.code || '')) {
      // Parse the assignment to its typed value (dropdown values may be numeric/
      // bool now, not just strings) instead of blindly stripping quotes.
      const value = parsePythonValue(p.currentValue, p.type)
      // For a dropdown, prefer the human-readable option LABEL for display; the
      // raw value (e.g. an int id) isn't meaningful to a consumer. Falls back to
      // the value itself for flat-string options or when no match is found.
      let displayValue = value
      if (p.type === 'dropdown' && p.config && p.config.options) {
        const opt = normalizeOptions(p.config.options).find(o => o.value === value)
        if (opt) displayValue = opt.label
      }
      inputs.push({
        label: (p.config && p.config.label) || p.varName,
        varName: p.varName,
        type: p.type,
        value,
        displayValue,
      })
    }
  }
  return inputs
}
