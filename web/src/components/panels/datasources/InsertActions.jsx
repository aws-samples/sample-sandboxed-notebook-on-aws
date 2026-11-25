import { useState } from 'react'
import { apiFetch } from '../../../services/apiFetch'
import { IconCode, IconDatabase } from '../../Icons'

/**
 * InsertActions — inline "insert code" buttons for a data-source row.
 *
 * Discoverability: unlike a click-only popover, these are ALWAYS mounted and
 * visible (dimmed at rest, brightened on row hover — the standard file-explorer
 * pattern), so a user doesn't have to guess that a row is actionable.
 *
 * Fetches the ready-to-run snippet from the backend DataSourceProvider and hands
 * it to the notebook via onInsert(code, cell_type). Whether SQL is offered is
 * source-dependent (public APIs, for instance, are Python-only).
 */
export default function InsertActions({ sourceType, sourceId, onInsert, showSql = true }) {
  const [loading, setLoading] = useState(null)  // 'python' | 'sql' | null

  const insert = async (language, e) => {
    e.stopPropagation()
    setLoading(language)
    try {
      const resp = await apiFetch(
        `/datasources/snippet?source_type=${encodeURIComponent(sourceType)}&source_id=${encodeURIComponent(sourceId)}&language=${language}`
      )
      if (resp.ok) {
        const data = await resp.json()
        onInsert(data.code, data.cell_type)
      }
    } catch (err) {
      console.warn('Snippet fetch failed:', err)
    }
    setLoading(null)
  }

  return (
    <div className="ds-insert-actions">
      <button
        className="ds-insert-btn ds-insert-python"
        onClick={(e) => insert('python', e)}
        disabled={loading !== null}
        title="Insert Python code for this source"
      >
        <IconCode width={11} height={11} /> Python
      </button>
      {showSql && (
        <button
          className="ds-insert-btn ds-insert-sql"
          onClick={(e) => insert('sql', e)}
          disabled={loading !== null}
          title="Insert SQL cell for this source"
        >
          <IconDatabase width={11} height={11} /> SQL
        </button>
      )}
    </div>
  )
}
