/**
 * HTML sanitization utility using DOMPurify.
 * All user-generated or executor-generated HTML must pass through this
 * before being rendered via dangerouslySetInnerHTML to prevent XSS attacks.
 */
import DOMPurify from 'dompurify'

/**
 * Sanitize markdown-rendered HTML (more permissive for code blocks).
 * Used for AI responses and user markdown cells.
 */
export function sanitizeMarkdown(dirty) {
  if (!dirty) return ''
  // IMPORTANT: do NOT set USE_PROFILES here. DOMPurify's DEFAULT config already
  // permits HTML + SVG + MathML (so KaTeX spans/MathML and mermaid <svg> survive)
  // AND honors ALLOW_DATA_ATTR. Setting USE_PROFILES switches DOMPurify into a
  // strict profile-allowlist mode where ALLOW_DATA_ATTR is ignored — which stripped
  // our data-mermaid attribute, so the mermaid pass found no placeholders and the
  // diagram rendered as its raw-source fallback. Extending the default (below) keeps
  // data-* AND the SVG/MathML the default already allows.
  return DOMPurify.sanitize(dirty, {
    // input/button: markdown checkboxes/buttons. (The mermaid SVG itself does NOT
    // pass through here — it's spliced in AFTER sanitize by spliceCachedMermaid,
    // because DOMPurify strips the foreignObject label content. Only the mermaid
    // PLACEHOLDER div + its data-mermaid attribute go through sanitize.)
    ADD_TAGS: ['input', 'button'],
    // data-mermaid identifies the placeholder for the post-sanitize splice; style/
    // aria-hidden are emitted by KaTeX; the rest are pre-existing markdown needs.
    ADD_ATTR: ['target', 'class', 'checked', 'disabled', 'type', 'data-lang', 'data-mermaid', 'style', 'aria-hidden'],
    ALLOW_DATA_ATTR: true,
  })
}
