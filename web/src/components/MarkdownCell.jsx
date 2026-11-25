import { useState, useRef, useEffect } from 'react'
import { useMermaidMarkdown } from '../hooks/useMermaidMarkdown'
import { IconCode, IconCheck, IconTrash, IconGripVertical, IconDatabase, IconPencil, IconChevronDown, IconChevronRight } from './Icons'
import CellCollapsedStrip from './CellCollapsedStrip'
import './Cell.css'

/**
 * Markdown/text cell with edit/render modes.
 * Double-click rendered view to edit. Shift+Enter or Esc to render.
 */
export default function MarkdownCell({
  cell,
  typeOrdinal,
  // Whole-cell collapse (state owned by Cell.jsx, kept per page session).
  collapsed = false,
  onToggleCollapsed,
  isActive,
  isFlashing,
  isDragOver,
  hasSearchMatch,
  searchQuery,
  searchActiveOccurrence = -1,
  onFocus,
  onCodeChange,
  onAddBelow,
  onDelete,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}) {
  const mdTextareaRef = useRef(null)
  const mdRenderedRef = useRef(null)
  const [mdEditing, setMdEditing] = useState(!cell.code)

  // Sanitized HTML with KaTeX math + mermaid SVGs baked in (mermaid renders post-
  // mount then rebuilds the string, so the SVG is React-owned and survives re-renders).
  const renderedHtml = useMermaidMarkdown(cell.code)

  // Auto-resize markdown textarea
  useEffect(() => {
    const el = mdTextareaRef.current
    if (el && mdEditing) {
      el.style.height = 'auto'
      el.style.height = `${el.scrollHeight}px`
    }
  }, [cell.code, mdEditing])

  // Scroll the active search match into view (parity with code cells, where the
  // current match is auto-scrolled). Runs after render so the .search-highlight-active
  // mark exists; deferred so it wins over the outer cell scroll.
  useEffect(() => {
    if (mdEditing || collapsed || searchActiveOccurrence < 0) return
    const t = setTimeout(() => {
      const el = mdRenderedRef.current?.querySelector('.search-highlight-active')
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 120)
    return () => clearTimeout(t)
  }, [searchQuery, searchActiveOccurrence, mdEditing, collapsed, cell.code])

  return (
    <div
      className={`cell cell-markdown${collapsed ? ' cell-collapsed' : ''} ${isActive ? 'cell-active' : ''} ${isFlashing ? 'cell-flash' : ''} ${isDragOver ? 'cell-drag-over' : ''} ${hasSearchMatch ? 'cell-search-match' : ''}`}
      data-cell-id={cell.id}
      onClick={onFocus}
      onDragOver={(e) => { e.preventDefault(); onDragOver?.() }}
      onDrop={(e) => { e.preventDefault(); onDrop?.() }}
      onDragEnd={onDragEnd}
    >
      <div className="cell-gutter">
        <span
          className="cell-drag-handle"
          draggable
          onDragStart={onDragStart}
          title="Drag to reorder"
        >
          <IconGripVertical width={12} height={12} />
        </span>
        {onToggleCollapsed && (
          <button
            type="button"
            className="cell-collapse-btn"
            onClick={onToggleCollapsed}
            title={collapsed ? 'Expand cell' : 'Collapse cell'}
            aria-label={collapsed ? 'Expand cell' : 'Collapse cell'}
            aria-expanded={!collapsed}
          >
            {collapsed ? <IconChevronRight width={12} height={12} /> : <IconChevronDown width={12} height={12} />}
          </button>
        )}
        <span className="cell-type-badge cell-type-text" title="Markdown cell">MD{typeOrdinal ? `-${typeOrdinal}` : ''}</span>
      </div>
      <div className="cell-content">
        {collapsed ? (
          <CellCollapsedStrip cell={cell} onExpand={onToggleCollapsed} />
        ) : mdEditing ? (
          <div className="md-edit-area">
            <textarea
              ref={mdTextareaRef}
              className="md-textarea"
              value={cell.code}
              onChange={(e) => onCodeChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape' || (e.key === 'Enter' && e.shiftKey)) {
                  e.preventDefault()
                  setMdEditing(false)
                }
              }}
              placeholder="Write markdown here... (Shift+Enter or Esc to render)"
              spellCheck={true}
              autoFocus
            />
            <div className="md-edit-hint">Shift+Enter to render · Esc to close</div>
          </div>
        ) : (
          <div
            className="md-rendered"
            ref={mdRenderedRef}
            onDoubleClick={() => setMdEditing(true)}
            dangerouslySetInnerHTML={{ __html: (() => {
              // renderedHtml (from useMermaidMarkdown) already has KaTeX + mermaid
              // SVGs baked in and is sanitized; we only layer search highlighting on top.
              let html = cell.code ? renderedHtml : '<p class="md-placeholder">Double-click to edit markdown</p>'
              if (searchQuery && searchQuery.trim() && cell.code) {
                const escaped = searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                const regex = new RegExp(`(${escaped})`, 'gi')
                // Walk matches in document order; the one at searchActiveOccurrence
                // gets the high-contrast active class (same amber as code cells).
                let occ = 0
                html = html.replace(/>([^<]*)</g, (match, text) => {
                  const replaced = text.replace(regex, (m) => {
                    const cls = occ === searchActiveOccurrence
                      ? 'search-highlight search-highlight-active'
                      : 'search-highlight'
                    occ++
                    return `<mark class="${cls}">${m}</mark>`
                  })
                  return '>' + replaced + '<'
                })
              }
              return html
            })() }}
          />
        )}
        {/* No hover toolbar on a collapsed cell: expand it first (chevron / strip). */}
        {!collapsed && (
        <div className="cell-actions md-actions">
          {mdEditing ? (
            <button className="cell-action-btn" onClick={() => setMdEditing(false)} title="Render markdown">
              <IconCheck width={14} height={14} />
            </button>
          ) : (
            <button className="cell-action-btn" onClick={() => setMdEditing(true)} title="Edit markdown">
              <IconPencil width={14} height={14} />
            </button>
          )}
          <button className="cell-action-btn" onClick={(e) => { e.stopPropagation(); onAddBelow('code') }} title="Add code cell below">
            <IconCode width={14} height={14} />
          </button>
          <button className="cell-action-btn cell-add-sql-btn" onClick={(e) => { e.stopPropagation(); onAddBelow('sql') }} title="Add SQL cell below">
            <IconDatabase width={12} height={12} />
          </button>
          <button className="cell-action-btn cell-add-md-btn" onClick={(e) => { e.stopPropagation(); onAddBelow('markdown') }} title="Add text cell below">
            M
          </button>
          <button className="cell-action-btn cell-delete-btn" onClick={(e) => { e.stopPropagation(); onDelete() }} title="Delete cell">
            <IconTrash width={14} height={14} />
          </button>
        </div>
        )}
      </div>
    </div>
  )
}
