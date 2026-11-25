/**
 * @param Input Widgets — Parse annotations from cell code and render interactive controls.
 * 
 * Syntax: # @param {type: "slider", min: 0, max: 100, step: 1, default: 50}
 *         variable_name = 50
 * 
 * Supported types: slider, dropdown, text, number, checkbox, date
 * 
 * Dropdowns take options either inline (static) or from a runtime variable:
 *   # @param {"type":"dropdown","options":["US","UK"]}            (flat strings)
 *   # @param {"type":"dropdown","options":[{"label":"…","value":11}]}  (typed)
 *   # @param {"type":"dropdown","optionsFrom":"nb_options"}       (runtime var)
 * `optionsFrom` names a variable the notebook computed; its value is resolved to a
 * [{label,value}] list by the VM's /param-options endpoint (the /variables map only
 * carries a truncated repr, so we must fetch the real structured value).
 * 
 * When a widget value changes, the code is updated (variable assignment line)
 * and the cell is auto-executed.
 */

import { useState, useEffect, useMemo, useRef } from 'react'
import { apiFetch } from '../services/apiFetch'
import { readApiError } from '../services/apiError'
import { parseParams, normalizeOptions, parsePythonValue } from '../services/paramParse.js'

// The pure parsers live in services/paramParse.js (Node-loadable); re-exported
// here so existing importers of ParamWidgets keep working.
export { parseParams, normalizeOptions, parsePythonValue }

/**
 * Format a value for insertion into Python code.
 */
/**
 * Emit a Python literal for a value written into a `var = <literal>` line.
 * For most types this is type-driven; for dropdown the VALUE's JS type decides the
 * literal, so a numeric option value lands as `notebook_id = 11` (not "11"), a bool
 * as `True/False`, and a string as a quoted literal.
 */
function formatPythonValue(value, type) {
  if (type === 'checkbox') return value ? 'True' : 'False'
  if (type === 'dropdown') return formatLiteral(value)
  if (type === 'text' || type === 'date') return `"${value}"`
  return String(value)
}

/** Render an arbitrary JS value as a Python literal (used by typed dropdown values). */
function formatLiteral(value) {
  if (typeof value === 'boolean') return value ? 'True' : 'False'
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (value === null || value === undefined) return 'None'
  // Fall back to a quoted string; escape embedded double quotes.
  return `"${String(value).replace(/"/g, '\\"')}"`
}

/**
 * Widget bar component — rendered above the code editor when @param annotations are detected.
 * 
 * Does NOT modify cell.code directly (avoids CellEditor remount issues).
 * Instead, stores widget values and provides them for execution via onExecuteWithParams.
 */
export default function ParamWidgets({ code, onCodeChange, onExecute, sessionId = null }) {
  const params = useMemo(() => parseParams(code), [code])
  const [values, setValues] = useState({})
  const [editingLabel, setEditingLabel] = useState(null)  // varName whose label is being renamed
  const userChanging = useRef(false)  // guard: true while a widget-initiated change propagates
  const executeTimer = useRef(null)   // debounce auto-execution
  // Resolved options for dropdowns that use `optionsFrom`: varName → { status:
  // 'loading'|'ready'|'error', options: [{label,value}], error?: string }. Fetched
  // from the VM's /param-options endpoint (the runtime variable's real value).
  const [resolvedOptions, setResolvedOptions] = useState({})

  // Which @param dropdowns declare optionsFrom — the set we must resolve from the
  // kernel. Recomputed from the parsed params (stable string key for the effect dep).
  const optionsFromVars = useMemo(
    () => params
      .filter(p => p.type === 'dropdown' && p.config && p.config.optionsFrom)
      .map(p => p.config.optionsFrom),
    [params]
  )
  const optionsFromKey = optionsFromVars.join(',')

  // Fetch resolved options whenever the set of optionsFrom vars (or the session)
  // changes. Best-effort: a not-yet-defined variable (cell hasn't run) 404s, which
  // we surface as an 'error' status so the dropdown shows a helpful placeholder;
  // it re-resolves on the next render after the source cell runs. Listens for
  // 'refresh-variables' (fired after any execution) to re-pull.
  const refreshOptions = useMemo(() => () => {
    if (!sessionId || optionsFromVars.length === 0) return
    for (const varName of optionsFromVars) {
      setResolvedOptions(prev => ({ ...prev, [varName]: { ...(prev[varName] || {}), status: 'loading' } }))
      apiFetch(`/proxy/param-options`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': sessionId },
        body: JSON.stringify({ name: varName }),
      })
        .then(async r => {
          if (!r.ok) throw new Error(await readApiError(r, `HTTP ${r.status}`))
          return r.json().catch(() => ({}))
        })
        .then(data => {
          setResolvedOptions(prev => ({
            ...prev,
            [varName]: { status: 'ready', options: Array.isArray(data.options) ? data.options : [] },
          }))
        })
        .catch(err => {
          setResolvedOptions(prev => ({
            ...prev,
            [varName]: { status: 'error', options: [], error: String(err.message || err) },
          }))
        })
    }
  }, [sessionId, optionsFromKey])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    // The optionsFrom variable is produced by a CELL that runs — which may happen
    // AFTER this widget first mounts (so the initial fetch 404s). Re-resolve on the
    // signals that indicate namespace changes: 'notebook-run-recorded' (fires after
    // any run completes) and 'refresh-variables' (variable panel refresh / delete).
    // Also do one short delayed retry to cover the race where the widget's own cell
    // just ran and the var lands a beat later.
    refreshOptions()
    const retry = setTimeout(() => refreshOptions(), 1200)
    const onChange = () => refreshOptions()
    window.addEventListener('refresh-variables', onChange)
    window.addEventListener('notebook-run-recorded', onChange)
    return () => {
      clearTimeout(retry)
      window.removeEventListener('refresh-variables', onChange)
      window.removeEventListener('notebook-run-recorded', onChange)
    }
  }, [refreshOptions])

  // Sync values from code on mount / code change — but NOT when the change
  // came from a widget interaction (to avoid overwriting mid-drag state)
  const paramKey = params.map(p => p.varName + p.currentValue).join(',')
  useEffect(() => {
    if (userChanging.current) {
      userChanging.current = false
      return
    }
    const newValues = {}
    for (const param of params) {
      newValues[param.varName] = parsePythonValue(param.currentValue, param.type)
    }
    setValues(newValues)
  }, [paramKey])

  if (params.length === 0) return null

  const handleChange = (param, newValue) => {
    userChanging.current = true  // prevent the sync effect from overwriting
    setValues(prev => ({ ...prev, [param.varName]: newValue }))

    // Build the modified code with updated values and execute
    const lines = code.split('\n')
    for (const p of params) {
      const val = p.varName === param.varName ? newValue : values[p.varName]
      const formatted = formatPythonValue(val !== undefined ? val : parsePythonValue(p.currentValue, p.type), p.type)
      lines[p.valueLineIndex] = `${p.varName} = ${formatted}`
    }
    const newCode = lines.join('\n')
    onCodeChange(newCode)

    // Debounced auto-execute: waits 400ms after last change (avoids flooding during slider drag)
    if (executeTimer.current) clearTimeout(executeTimer.current)
    executeTimer.current = setTimeout(() => onExecute(), 400)
  }

  // Rename a param's display label — persists into the @param JSON as `label`.
  // Purely cosmetic, so it does NOT trigger a re-run.
  const changeLabel = (param, rawLabel) => {
    const lines = code.split('\n')
    const cfg = { ...param.config }
    const next = (rawLabel || '').trim()
    if (next && next !== param.varName) cfg.label = next
    else delete cfg.label  // reset to the variable name
    const indent = (lines[param.paramLineIndex] || '').match(/^(\s*)/)?.[1] || ''
    lines[param.paramLineIndex] = `${indent}# @param ${JSON.stringify(cfg)}`
    onCodeChange(lines.join('\n'))
  }

  return (
    <div className="param-widgets-bar">
      {params.map(param => (
        <div key={param.varName} className="param-widget">
          {editingLabel === param.varName ? (
            <input
              className="param-label-input"
              autoFocus
              defaultValue={param.config.label || param.varName}
              onClick={(e) => e.stopPropagation()}
              onBlur={(e) => { changeLabel(param, e.target.value); setEditingLabel(null) }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { changeLabel(param, e.target.value); setEditingLabel(null) }
                else if (e.key === 'Escape') { setEditingLabel(null) }
              }}
            />
          ) : (
            <label
              className="param-label"
              title="Double-click to rename"
              onDoubleClick={() => setEditingLabel(param.varName)}
            >
              {param.config.label || param.varName}
            </label>
          )}
          {param.type === 'slider' && (
            <div className="param-slider-group">
              <input
                type="range"
                className="param-slider"
                min={param.config.min ?? 0}
                max={param.config.max ?? 100}
                step={param.config.step ?? 1}
                value={values[param.varName] ?? param.config.default ?? 50}
                onChange={e => handleChange(param, Number(e.target.value))}
              />
              <span className="param-value">{values[param.varName] ?? param.config.default}</span>
            </div>
          )}
          {param.type === 'number' && (
            <input
              type="number"
              className="param-number"
              min={param.config.min}
              max={param.config.max}
              step={param.config.step ?? 1}
              value={values[param.varName] ?? param.config.default ?? 0}
              onChange={e => handleChange(param, Number(e.target.value))}
            />
          )}
          {param.type === 'dropdown' && (() => {
            // Options come from EITHER a runtime variable (optionsFrom, resolved via
            // /param-options) OR the inline `options` list. In both cases we
            // normalize to {label, value}. The HTML <select> value is ALWAYS a
            // string, so we key each option by its index and translate back to the
            // typed value on change — preserving non-string values (e.g. an int id).
            const fromVar = param.config.optionsFrom
            let opts, phase = 'ready', phaseMsg = ''
            if (fromVar) {
              const r = resolvedOptions[fromVar]
              if (!r || r.status === 'loading') { phase = 'loading'; phaseMsg = 'Loading…' }
              else if (r.status === 'error') { phase = 'error'; phaseMsg = `Run the cell that defines ${fromVar}` }
              opts = normalizeOptions((r && r.options) || [])
            } else {
              opts = normalizeOptions(param.config.options)
            }
            const current = values[param.varName] ?? param.config.default
            const selectedIdx = opts.findIndex(o => o.value === current)
            const disabled = fromVar && (phase !== 'ready' || opts.length === 0)
            return (
              <select
                className="param-select"
                value={selectedIdx >= 0 ? String(selectedIdx) : ''}
                disabled={disabled}
                title={fromVar && phase !== 'ready' ? phaseMsg : undefined}
                onChange={e => {
                  const idx = Number(e.target.value)
                  const opt = opts[idx]
                  if (opt) handleChange(param, opt.value)
                }}
              >
                {(selectedIdx < 0 || disabled) && (
                  <option value="" disabled>
                    {phase === 'loading' ? 'Loading…'
                      : phase === 'error' ? `⚠ define ${fromVar} first`
                      : opts.length === 0 && fromVar ? `⚠ ${fromVar} is empty`
                      : 'Select…'}
                  </option>
                )}
                {opts.map((opt, idx) => (
                  <option key={idx} value={String(idx)}>{opt.label}</option>
                ))}
              </select>
            )
          })()}
          {param.type === 'text' && (
            <input
              type="text"
              className="param-text"
              placeholder={param.config.placeholder || ''}
              value={values[param.varName] ?? param.config.default ?? ''}
              onChange={e => handleChange(param, e.target.value)}
            />
          )}
          {param.type === 'checkbox' && (
            <input
              type="checkbox"
              className="param-checkbox"
              checked={values[param.varName] ?? param.config.default ?? false}
              onChange={e => handleChange(param, e.target.checked)}
            />
          )}
          {param.type === 'date' && (
            <input
              type="date"
              className="param-date"
              value={values[param.varName] ?? param.config.default ?? ''}
              onChange={e => handleChange(param, e.target.value)}
            />
          )}
        </div>
      ))}
    </div>
  )
}
