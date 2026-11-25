import { useEffect, useMemo, useState } from 'react'
import { renderMarkdownSafe, renderMarkdown, ensureMermaidRendered, onMermaidThemeChange } from '../services/markdown'

/**
 * useMermaidMarkdown — turn markdown `content` into sanitized HTML with math
 * (KaTeX) and diagrams (Mermaid) baked directly into the returned string.
 *
 * Why a hook (vs. imperatively injecting SVG post-mount): consumers render the
 * HTML via dangerouslySetInnerHTML, so React OWNS that subtree. Any parent
 * re-render (status polls, focus changes, etc.) re-commits the HTML and would
 * wipe an imperatively-injected <svg>. Instead we:
 *   1. render markdown → HTML (mermaid fences become placeholders) + sanitize,
 *   2. after mount, render+cache each diagram's SVG (async, once per source),
 *   3. bump a tick so the memo re-runs and renderMarkdownSafe() splices the cached
 *      SVG INTO the HTML string — now it's React-owned and survives re-renders.
 *
 * Returns the final sanitized HTML string ready for dangerouslySetInnerHTML.
 * No mermaid bundle is loaded when the content has no diagrams.
 */
export function useMermaidMarkdown(content) {
  const [tick, setTick] = useState(0)

  // `tick` MUST be a dep: when a diagram finishes rendering the effect bumps tick,
  // and this memo must re-run so renderMarkdownSafe() splices the now-cached SVG
  // into the HTML. Without tick in the deps the memo stays frozen on the placeholder.
  const html = useMemo(
    // renderMarkdownSafe: renders markdown, sanitizes it, THEN splices trusted
    // mermaid SVGs in (DOMPurify strips foreignObject label content, so the SVG
    // must bypass it — see markdown.js). Re-runs when content or tick changes.
    () => renderMarkdownSafe(content || ''),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [content, tick],
  )

  useEffect(() => {
    let cancelled = false
    // Render any not-yet-cached diagrams, then force a rebuild so their SVG gets
    // spliced into the HTML. Cheap no-op when there are no (uncached) diagrams.
    ensureMermaidRendered(renderMarkdown(content || '')).then((changed) => {
      if (changed && !cancelled) setTick((t) => t + 1)
    })
    return () => { cancelled = true }
  }, [content, tick])

  // Re-theme live on an app theme toggle (no page reload): the shared observer has
  // already cleared the mermaid cache, so bumping tick re-runs the memo
  // (renderMarkdownSafe falls back to placeholders) and the effect above re-renders
  // the SVGs under the new theme. No-op cost for diagram-free content — the mermaid
  // bundle still only loads when ensureMermaidRendered sees an uncached diagram.
  useEffect(() => onMermaidThemeChange(() => setTick((t) => t + 1)), [])

  return html
}
