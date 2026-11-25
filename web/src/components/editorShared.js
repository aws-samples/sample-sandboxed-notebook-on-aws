/**
 * Shared CodeMirror theme + syntax highlighting, extracted so the notebook cell
 * editor AND the version-diff view render code with the SAME look (font, colors,
 * token highlighting). These reference our CSS custom properties, so all three
 * app themes (dark/light/ember) drive them automatically.
 *
 * Keep this the single source of truth: if the cell editor's look changes, change
 * it here and both surfaces stay in sync.
 */
import { EditorView } from '@codemirror/view'
import { HighlightStyle } from '@codemirror/language'
import { python } from '@codemirror/lang-python'
import { sql as sqlLang } from '@codemirror/lang-sql'
import { tags } from '@lezer/highlight'

// Base editor chrome — mirrors CellEditor.editorTheme (fonts, padding, selection,
// active line). The diff view adds its own change-chunk colors on top via CSS.
export const sharedEditorTheme = EditorView.theme({
  '&': {
    fontSize: '13px',
    fontFamily: 'var(--font-mono)',
    backgroundColor: 'transparent',
  },
  '.cm-content': {
    padding: '10px 12px',
    caretColor: 'var(--cm-cursor)',
    lineHeight: '1.6',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-line': { padding: '0' },
  '.cm-activeLine': { backgroundColor: 'transparent' },
  '.cm-selectionBackground, ::selection': {
    backgroundColor: 'var(--cm-selection) !important',
  },
  '.cm-cursor': { borderLeftColor: 'var(--cm-cursor)', borderLeftWidth: '2px' },
  // Line-number gutter kept (unlike the cell editor, which hides it in favor of a
  // custom cell-number gutter) — a diff reads better with line numbers.
  '.cm-gutters': {
    backgroundColor: 'transparent',
    border: 'none',
    color: 'var(--cm-gutter)',
  },
  '.cm-scroller': { overflow: 'auto' },
}, { dark: true })

// Token colors — identical mapping to CellEditor.syntaxColors.
export const sharedSyntaxColors = HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--cm-keyword)', fontWeight: 'var(--cm-keyword-weight)' },
  { tag: tags.string, color: 'var(--cm-string)' },
  { tag: tags.number, color: 'var(--cm-number)' },
  { tag: tags.bool, color: 'var(--cm-bool)' },
  { tag: tags.null, color: 'var(--cm-bool)' },
  { tag: tags.comment, color: 'var(--cm-comment)', fontStyle: 'var(--cm-comment-style)' },
  { tag: tags.variableName, color: 'var(--cm-text)' },
  { tag: tags.definition(tags.variableName), color: 'var(--cm-definition)' },
  { tag: tags.propertyName, color: 'var(--cm-property)' },
  { tag: tags.className, color: 'var(--cm-class)' },
  { tag: tags.operator, color: 'var(--cm-operator)' },
  { tag: tags.punctuation, color: 'var(--cm-operator)' },
  { tag: tags.bracket, color: 'var(--cm-bracket)' },
  { tag: tags.meta, color: 'var(--cm-meta)' },
  { tag: tags.typeName, color: 'var(--cm-type)' },
  { tag: tags.attributeName, color: 'var(--cm-attribute)' },
])

// Language extension for a cell type. Markdown has no highlighting mode here (the
// notebook cell editor treats it as plain text too), so it returns [].
export function langExtensionForType(type) {
  if (type === 'sql') return sqlLang()
  if (type === 'markdown') return []
  return python()
}

// Per-type identity used by the diff cell headers so they match the notebook's
// PY / SQL / MD badges and accent colors (theme.css tokens).
export const CELL_TYPE_META = {
  code: { label: 'PY', accent: 'var(--lang-python, var(--entity-code))', subtle: 'var(--lang-python-subtle, var(--entity-code-subtle))' },
  sql: { label: 'SQL', accent: 'var(--lang-sql, var(--entity-sql))', subtle: 'var(--lang-sql-subtle, var(--entity-sql-subtle))' },
  markdown: { label: 'MD', accent: 'var(--entity-data)', subtle: 'var(--entity-data-subtle)' },
}
