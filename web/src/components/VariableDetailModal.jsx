import { useState, useEffect } from 'react'
import Modal from './Modal'
import DataGrid from './DataGrid'
import JsonTree from './JsonTree'
import Markdown from './Markdown'
import { looksLikeMarkdown } from './CellOutput'
import { parseTablePayload } from '../services/dfCellFormat'
import { authHeaders } from '../config'

// Standalone full-grid viewer for a single variable. Fetches /variable-detail
// itself (schema + a structured `table` payload) so it can be opened from either
// the Variables panel or directly from a notebook cell's "variables defined here"
// chips. For tabular values it renders the paginated DataGrid; for everything
// else it falls back to a JSON tree or the repr text the backend returns.
export default function VariableDetailModal({ name, endpoint, sessionId, onClose, className = 'modal-card-lg' }) {
  const [state, setState] = useState({ loading: true, data: null, error: null })

  useEffect(() => {
    if (!name || !endpoint) return
    let cancelled = false
    setState({ loading: true, data: null, error: null })
    ;(async () => {
      try {
        const headers = authHeaders({ 'Content-Type': 'application/json' })
        if (sessionId) headers['X-Session-Id'] = sessionId
        const resp = await fetch(`${endpoint}/variable-detail`, {
          method: 'POST', headers, body: JSON.stringify({ name }),
        })
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const data = await resp.json()
        if (!cancelled) setState({ loading: false, data, error: null })
      } catch (err) {
        if (!cancelled) setState({ loading: false, data: null, error: err.message })
      }
    })()
    return () => { cancelled = true }
  }, [name, endpoint, sessionId])

  const { loading, data, error } = state
  const rowsLabel = data?.total_rows != null
    ? ` · ${data.total_rows.toLocaleString()} rows${data.total_cols != null ? ` × ${data.total_cols} cols` : ''}`
    : ''

  return (
    <Modal title={`${name}${rowsLabel}`} onClose={onClose} className={className}>
      <div className="var-viewer">
        {loading && <div className="var-viewer-status">Loading data…</div>}
        {error && <div className="var-viewer-status var-viewer-error">Failed to load: {error}</div>}
        {(() => {
          const tablePayload = data?.table ? parseTablePayload(data.table) : null
          if (tablePayload) return <DataGrid payload={tablePayload} />
          if (data && data.json_tree !== undefined) return <JsonTree data={data.json_tree} />
          if (!loading && !error && data?.text) {
            // A string value is shown verbatim. If it looks like markdown (e.g. an
            // LLM answer with headers/bold/lists), render it formatted through the
            // shared Markdown component — same treatment print()-ed markdown gets in
            // cell output; otherwise show the raw text in a <pre> (real newlines,
            // since the backend returns the raw string, not its repr()).
            return looksLikeMarkdown(data.text)
              ? <Markdown className="var-viewer-text output-markdown" content={data.text} />
              : <pre className="var-viewer-text">{data.text}</pre>
          }
          return null
        })()}
        {data?.note && (
          <div className="var-viewer-note">{data.note}</div>
        )}
        {/* Row-truncation is surfaced by the DataGrid footer itself ("… of N rows"),
            so no separate hint is needed here. */}
      </div>
    </Modal>
  )
}
