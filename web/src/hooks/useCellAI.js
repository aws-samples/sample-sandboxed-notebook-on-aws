import { useState, useEffect, useRef } from 'react'
import { PROXY_URL, AI_TIMEOUT_MS, AI_FIX_TIMEOUT_MS } from '../config'
import { fetchWithTimeout } from '../services/fetchWithTimeout'

/**
 * useCellAI — the AI + execution-routing logic for a code/SQL cell:
 *   - smartExecute: NLP-vs-code detection → generate or run
 *   - handleGenerate: NLP → code (Python/SQL) via /ai/chat/sync
 *   - handleAiFix / handleAiCancel / handleApplyFix (explain lives in useCellExplain)
 * Owns aiResult, generating, and editorVersion (bumped to remount the editor when
 * code is replaced by Apply Fix / generate). Extracted from Cell.jsx.
 */
export function useCellAI({
  cell,
  index,
  isConnected,
  aiAvailable,
  microvmId,
  sessionId,
  variables = {},
  dataSources = null,
  getNotebookContext,
  onExecute,
  onCodeChange,
  onClearOutput,
}) {
  const [aiResult, setAiResult] = useState(
    cell.aiExplanation ? { type: 'explain', content: cell.aiExplanation, loading: false } : null
  )
  const [generating, setGenerating] = useState(false)
  // Version counter — incremented on external code changes (Apply Fix, AI generate)
  // Forces CellEditor to remount with fresh content
  const [editorVersion, setEditorVersion] = useState(0)
  const aiAbortRef = useRef(null)
  // Distinguishes a user-initiated cancel (handleAiCancel — clears the panel itself)
  // from a timeout abort (fetchWithTimeout fired its AbortController). Both surface as
  // err.name === 'AbortError', but only the timeout case must clear the "Fixing..."
  // spinner here — otherwise loading:true sticks forever and the UI spins even after
  // the backend has finished.
  const fixUserCancelledRef = useRef(false)
  // Tracks consecutive Fix clicks on the SAME error, so a retry can ask the backend
  // to raise the temperature (explore a different fix instead of re-deriving the
  // same near-deterministic one). Keyed by the error text: a NEW/changed error is a
  // fresh problem → attempt resets to 1. { key: <error string>, attempt: <n> }.
  const fixAttemptRef = useRef({ key: null, attempt: 0 })

  // Sync aiResult when cell.aiExplanation changes externally (e.g. from Annotate /
  // Clear annotations). When it's set, show/refresh the note; when it's cleared to
  // null, remove the note — but only if it's an 'explain' note, so a Fix result the
  // user is actively viewing isn't wiped out.
  useEffect(() => {
    if (cell.aiExplanation) {
      if (!aiResult || aiResult.content !== cell.aiExplanation) {
        setAiResult({ type: 'explain', content: cell.aiExplanation, loading: false })
      }
    } else if (aiResult?.type === 'explain') {
      setAiResult(null)
    }
  }, [cell.aiExplanation])

  // Smart execute: detects NLP vs code and routes accordingly
  const smartExecute = () => {
    const code = (cell.code || '').trim()
    if (cell.type === 'sql') {
      // For SQL cells: detect NLP (doesn't look like SQL) → generate SQL
      if (code && aiAvailable && isConnected) {
        const looksLikeSql = /^(SELECT|INSERT INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+(TABLE|VIEW|INDEX|DATABASE)|DROP\s+(TABLE|VIEW)|ALTER\s+TABLE|WITH\s+\w+\s+AS|SHOW\s+(TABLES|DATABASES|COLUMNS)|DESCRIBE|EXPLAIN)\b/i.test(code) || code.trimStart().startsWith('--')
        if (!looksLikeSql && !generating) {
          handleGenerate()
          return
        }
      }
      onExecute()
      return
    }
    if (code && aiAvailable && isConnected) {
      const looksLikeCode = /^(import |from |def |class |for |while |if |#|[a-zA-Z_]\w*\s*[=([]|print\(|plt\.|pd\.|np\.)/.test(code) || code.includes('=') || code.includes('(')
      if (!looksLikeCode && !generating) {
        handleGenerate()
        return
      }
    }
    onExecute()
  }

  const handleAiFix = async () => {
    setAiResult({ type: 'fix', content: '', loading: true })
    // Bump the attempt counter when Fix is clicked repeatedly on the SAME error
    // (the previous fix didn't resolve it) so the backend can raise temperature and
    // try a genuinely different fix. A different error string resets to attempt 1.
    const errKey = cell.error || ''
    if (fixAttemptRef.current.key === errKey) {
      fixAttemptRef.current.attempt += 1
    } else {
      fixAttemptRef.current = { key: errKey, attempt: 1 }
    }
    const fixAttempt = fixAttemptRef.current.attempt
    const controller = new AbortController()
    aiAbortRef.current = controller
    fixUserCancelledRef.current = false
    try {
      const resp = await fetchWithTimeout(`${PROXY_URL}/ai/fix`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        // The agentic fix verifies its own fix in-VM before returning, so it can run
        // far longer than a plain generation — use the dedicated (longer) ceiling.
        timeout: AI_FIX_TIMEOUT_MS,
        body: JSON.stringify({
          code: cell.code || '',
          error: cell.error || '',
          // Rich debug context so the fix reasons over the FULL failure, not just the
          // one-line message: the multi-frame traceback text and a snapshot of the
          // failing frame's local variable values (safe reprs, captured server-side).
          traceback: cell.errorTraceback || '',
          error_locals: cell.errorLocals && typeof cell.errorLocals === 'object' ? cell.errorLocals : {},
          attempt: fixAttempt,
          cell_type: cell.type || 'code',
          microvm_id: microvmId || '',
          session_id: sessionId || '',
          variables: Object.keys(variables || {}),
          data_sources: dataSources || null,
          cells: ((getNotebookContext ? getNotebookContext() : []) || []).slice(0, index).map(c => ({
            type: c.type || 'code',
            code: (c.code || '').slice(0, 300),
            output: (c.output || '').slice(0, 100),
          })),
        }),
      })
      if (resp.ok) {
        const data = await resp.json()
        // One call returns BOTH the corrected code and a plain-language explanation.
        // The explanation rides on the fix result (shown collapsed above the diff) —
        // it is NOT a cell note, so it never touches aiExplanation/persistence.
        setAiResult({
          type: 'fix',
          content: data.fixed_code || '',
          explanation: data.explanation || '',
          // Verification status. `verified` is true ONLY when the fix agent's last
          // execute_code / execute_sql run succeeded on the exact returned code (the
          // one-shot markdown/no-session path is always false). `attempts` = number of
          // execute_code / execute_sql test runs. When not verified, verifyReason is
          // no_run | last_run_failed | code_mismatch and verifyError is the last
          // run's full error. Rendered by FixVerification (missing = not verified).
          verified: typeof data.verified === 'boolean' ? data.verified : undefined,
          attempts: typeof data.attempts === 'number' ? data.attempts : undefined,
          verifyReason: typeof data.verify_reason === 'string' ? data.verify_reason : undefined,
          verifyError: typeof data.verify_error === 'string' ? data.verify_error : undefined,
          loading: false,
        })
      } else {
        setAiResult({ type: 'fix', content: 'Failed to fix error', explanation: '', loading: false })
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        // A user cancel already cleared the panel in handleAiCancel — leave it.
        // A TIMEOUT abort (fetchWithTimeout) must clear the spinner here, or
        // loading:true sticks forever and the UI keeps showing "Fixing...".
        if (!fixUserCancelledRef.current) {
          setAiResult({
            type: 'fix',
            content: '',
            explanation: 'The fix took too long and timed out. Try again — the AI may have made progress you can build on.',
            loading: false,
          })
        }
      } else {
        setAiResult({ type: 'fix', content: `Error: ${err.message}`, loading: false })
      }
    } finally {
      aiAbortRef.current = null
    }
  }

  const handleAiCancel = () => {
    // Mark this abort as user-initiated so the fetch's catch doesn't also post a
    // "timed out" message — this path owns the state and clears the panel.
    fixUserCancelledRef.current = true
    if (aiAbortRef.current) aiAbortRef.current.abort()
    setAiResult(null)
  }

  const handleApplyFix = () => {
    if (aiResult?.type === 'fix' && aiResult.content) {
      // Restore point BEFORE the AI edit lands — the editor remount below wipes
      // CodeMirror's undo history, so this version is the only way back to the
      // pre-fix code. App.jsx debounces this into one 'pre_ai' version per burst.
      try { window.dispatchEvent(new CustomEvent('notebook-pre-ai')) } catch {}
      onCodeChange(aiResult.content)
      setEditorVersion(v => v + 1)
      setAiResult(null)
      // The old error/output belonged to the previous (broken) code — clear it so the
      // cell no longer shows a stale error after the fix is applied.
      if (onClearOutput) onClearOutput()
      // Applying the fix replaces the whole cell body (and remounts the editor), so
      // the viewport can be left mid-cell or scrolled to where the fix panel was.
      // Scroll back to the START of this cell so the user sees the fixed code from
      // its first line. Deferred to the next frame so it runs after the editor
      // remount settles and the fix panel unmounts (which changes layout height).
      try {
        requestAnimationFrame(() => {
          const el = document.querySelector(`[data-cell-id="${cell.id}"]`)
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        })
      } catch {}
    }
  }

  const handleGenerate = async () => {
    if (!cell.code?.trim() || generating) return
    setGenerating(true)
    try {
      const isSqlCell = cell.type === 'sql'
      const prompt = isSqlCell
        ? `Generate a SQL query for the following request. This is a SQL cell using DuckDB. Use proper DuckDB syntax: local files as '/tmp/file.csv', S3 as read_csv('s3://...'), Athena tables as database.table. Return ONLY the SQL wrapped in \`\`\`sql, no explanations:\n\n${cell.code}`
        : `Generate Python code for the following request. This is a PYTHON code cell — return ONLY Python code, never SQL. Do NOT use \`\`\`sql blocks. Return ONLY the code wrapped in \`\`\`python, no explanations:\n\n${cell.code}`

      const resp = await fetchWithTimeout(`${PROXY_URL}/ai/chat/sync`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        timeout: AI_TIMEOUT_MS,
        body: JSON.stringify({
          message: prompt,
          active_cell_index: index,
          cells: ((getNotebookContext ? getNotebookContext() : []) || []).slice(0, index).map(c => ({
            type: c.type || 'code',
            code: (c.code || '').slice(0, 200),
            output: (c.output || '').slice(0, 100),
          })),
          microvm_id: microvmId || '',
          session_id: sessionId || '',
          // Pass the data-source catalog (table/file inventory + column schemas)
          // so the model writes SQL/Python against the REAL columns instead of
          // guessing names. Without this the /ai/chat/sync turn carried no catalog
          // at all, which is why generated SQL referenced non-existent columns
          // (e.g. customers.country). Same object AiChatPanel sends on /ai/chat.
          data_sources: dataSources || null,
        }),
      })
      if (resp.ok) {
        const data = await resp.json()
        let code = data.response || ''
        // Extract code from markdown fences using regex (robust to whitespace/formatting)
        if (isSqlCell) {
          const sqlMatch = code.match(/```sql\s*\n?([\s\S]*?)```/i)
          if (sqlMatch?.[1]) {
            code = sqlMatch[1].trim()
          } else if (code.startsWith('```') && code.endsWith('```')) {
            code = code.split('\n').slice(1, -1).join('\n').trim()
          }
        } else {
          const pyMatch = code.match(/```python\s*\n?([\s\S]*?)```/i)
          if (pyMatch?.[1]) {
            code = pyMatch[1].trim()
          } else {
            // Fallback: AI may have returned SQL despite being told Python
            const anyMatch = code.match(/```(?:sql|javascript)?\s*\n?([\s\S]*?)```/i)
            if (anyMatch?.[1]) {
              code = anyMatch[1].trim()
            } else if (code.startsWith('```') && code.endsWith('```')) {
              code = code.split('\n').slice(1, -1).join('\n').trim()
            }
          }
        }
        if (code) {
          // Snapshot before replacing the cell's code (editor remount wipes undo).
          try { window.dispatchEvent(new CustomEvent('notebook-pre-ai')) } catch {}
          onCodeChange(code)
          setEditorVersion(v => v + 1)
        }
      }
    } catch (err) {
      console.warn('[generate] AI code generation failed:', err.message)
    }
    setGenerating(false)
  }

  return {
    aiResult,
    setAiResult,
    generating,
    editorVersion,
    // Force the CellEditor to remount and re-read cell.code — used for EXTERNAL code
    // replacements that don't originate in the editor (e.g. per-cell version revert),
    // mirroring what Apply Fix / generate do via setEditorVersion. Without a remount
    // the already-mounted CodeMirror keeps showing the old doc until a page reload.
    bumpEditorVersion: () => setEditorVersion(v => v + 1),
    smartExecute,
    handleAiFix,
    handleAiCancel,
    handleApplyFix,
  }
}
