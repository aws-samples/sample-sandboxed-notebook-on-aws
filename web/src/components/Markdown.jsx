import { useMermaidMarkdown } from '../hooks/useMermaidMarkdown'

/**
 * Markdown — the single shared markdown renderer for the app.
 *
 * Renders `content` (markdown) to sanitized HTML with math (KaTeX) and diagrams
 * (Mermaid) baked into the HTML string (see useMermaidMarkdown). Replaces the
 * ~dozen duplicated `sanitizeMarkdown(marked.parse(...))` + dangerouslySetInnerHTML
 * call sites so KaTeX/Mermaid work everywhere consistently.
 *
 * Props:
 *   content    — markdown source string.
 *   className  — class for the wrapper div (keep each surface's existing class so
 *                styling is unchanged, e.g. "md-rendered", "ai-msg-md", "output-markdown").
 *   as         — wrapper element tag (default 'div').
 *   ...rest    — forwarded to the wrapper (onDoubleClick, etc.).
 */
export default function Markdown({ content, className, as: Tag = 'div', ...rest }) {
  const html = useMermaidMarkdown(content)
  return (
    <Tag className={className} dangerouslySetInnerHTML={{ __html: html }} {...rest} />
  )
}
