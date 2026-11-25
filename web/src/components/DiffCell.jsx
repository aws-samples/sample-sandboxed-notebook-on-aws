import { useRef, useEffect } from 'react'
import { EditorState } from '@codemirror/state'
import { EditorView, lineNumbers } from '@codemirror/view'
import { syntaxHighlighting } from '@codemirror/language'
import { unifiedMergeView } from '@codemirror/merge'
import { sharedEditorTheme, sharedSyntaxColors, langExtensionForType } from './editorShared'

/**
 * DiffCell — a read-only, syntax-highlighted unified diff for ONE cell, rendered
 * with the SAME CodeMirror theme + token colors as the notebook cell editor.
 *
 * Shows `current` as the document with `original` (the selected version's code)
 * diffed in: removed lines (in the version, gone now) are marked as deletions,
 * added lines (present now) as insertions. Language highlighting follows the cell
 * TYPE (python / sql / markdown-as-text), so each cell in the notebook-level diff
 * looks like its own cell.
 */
export default function DiffCell({ original, current, type = 'code', plain = false }) {
  const hostRef = useRef(null)
  const viewRef = useRef(null)

  useEffect(() => {
    if (!hostRef.current) return
    // Tear down any previous view (props changed → rebuild).
    if (viewRef.current) { viewRef.current.destroy(); viewRef.current = null }

    const extensions = [
      lineNumbers(),
      sharedEditorTheme,
      syntaxHighlighting(sharedSyntaxColors),
      langExtensionForType(type),
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
      EditorView.lineWrapping,
    ]
    // `plain` = a read-only code view (unchanged cells / removed-cell body); no
    // diff overlay. Otherwise show the unified diff of `current` against `original`
    // (gutter + accept/reject controls off — restore/revert are explicit buttons).
    if (!plain) {
      extensions.push(unifiedMergeView({
        original: original || '',
        mergeControls: false,
        gutter: false,
      }))
    }

    const state = EditorState.create({ doc: current || '', extensions })
    const view = new EditorView({ state, parent: hostRef.current })
    viewRef.current = view
    return () => { view.destroy(); viewRef.current = null }
  }, [original, current, type, plain])

  return <div className="diffcell-cm" ref={hostRef} />
}
