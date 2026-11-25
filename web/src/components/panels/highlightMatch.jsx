// Wrap each case-insensitive occurrence of `query` in <mark> so the search term is
// visible in a side-panel row. Literal match (query is regex-escaped). Shared by the
// Snippets and Samples panels so both highlight the same way.
export function highlightMatch(text, query) {
  const q = (query || '').trim()
  if (!q || !text) return text
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = String(text).split(new RegExp(`(${escaped})`, 'ig'))
  if (parts.length === 1) return text
  const qLc = q.toLowerCase()
  return parts.map((part, i) =>
    part.toLowerCase() === qLc
      ? <mark key={i} className="outline-search-hit">{part}</mark>
      : part
  )
}
