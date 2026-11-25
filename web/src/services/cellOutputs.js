// Multiple outputs per cell — the ONE adapter every consumer uses.
//
// A cell can show several tables, Plotly charts and images (matplotlib figures)
// with printed text interleaved, like Jupyter. The VM returns an ordered
// `outputs` list; the web stores it as a JSON envelope INSIDE the existing
// `cell.html` string, and only when the legacy single-slot fields (html / image /
// output) cannot show what the run produced:
//
//   {"__outputs__":1,"items":[ {"t":"text","text":"…"},
//     {"t":"table","payload":{__df_table__…},"label":"df"},
//     {"t":"plotly","html":"<div class=\"plotly-chart\" …>","label":"fig"},
//     {"t":"image","src":"data:image/png;base64,…","key":"notebook-outputs/…png"} ]}
//
// Any other cell (old notebooks, one rich output, SQL, text only) keeps today's
// exact `html` / `image` / `imageS3Key` strings. Base64 `src` lives in memory
// only: every persistence path runs stripEnvelopeImages first and the stored
// envelope carries just the S3 `key`.
//
// Pure module (no DOM, `.js` import extensions) so node --test can load it.
import { parseTablePayload } from './dfCellFormat.js'

const ENVELOPE_PREFIX = '{"__outputs__"'
const RICH = new Set(['table', 'plotly', 'image'])

/** True when `html` is a multi-output envelope string (cheap prefix check). */
export function isEnvelope(html) {
  return typeof html === 'string' && html.startsWith(ENVELOPE_PREFIX)
}

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)

/**
 * Validate one item and return a clean copy with only the known fields, or null.
 * A table `payload` may arrive as the __df_table__ object or as its JSON string
 * (the legacy `html` form); it is always returned as the object.
 */
export function normalizeItem(it) {
  if (!isObj(it)) return null
  const label = typeof it.label === 'string' && it.label ? { label: it.label } : {}
  switch (it.t) {
    case 'text':
      return typeof it.text === 'string' ? { t: 'text', text: it.text } : null
    case 'table': {
      let p = it.payload
      if (typeof p === 'string') p = parseTablePayload(p)
      return isObj(p) && p.__df_table__ ? { t: 'table', payload: p, ...label } : null
    }
    case 'plotly':
      return typeof it.html === 'string' && it.html ? { t: 'plotly', html: it.html, ...label } : null
    case 'image': {
      const src = typeof it.src === 'string' && it.src ? { src: it.src } : {}
      const key = typeof it.key === 'string' && it.key ? { key: it.key } : {}
      return src.src || key.key ? { t: 'image', ...src, ...key } : null
    }
    default:
      return null
  }
}

/** Items of an envelope string (validated), or null for a non-envelope / bad JSON. Never throws. */
export function parseOutputsEnvelope(html) {
  if (!isEnvelope(html)) return null
  let doc
  try {
    doc = JSON.parse(html)
  } catch {
    return null
  }
  if (!isObj(doc) || doc.__outputs__ !== 1 || !Array.isArray(doc.items)) return null
  return doc.items.map(normalizeItem).filter(Boolean)
}

/** Compact envelope string (`__outputs__` first) for the given items; unknown fields and invalid items are dropped. */
export function buildOutputsEnvelope(items) {
  const clean = (Array.isArray(items) ? items : []).map(normalizeItem).filter(Boolean)
  return JSON.stringify({ __outputs__: 1, items: clean })
}

const isRich = (it) => RICH.has(it.t)

/** Does a run's item list need the envelope (2+ rich items, or text after a rich item)? */
export function needsEnvelope(items) {
  const firstRich = items.findIndex(isRich)
  if (firstRich < 0) return false
  if (items.filter(isRich).length >= 2) return true
  return items.slice(firstRich + 1).some(it => it.t === 'text')
}

/**
 * The cell fields {html, image, imageS3Key} for a VM / proxy run result.
 *  - no rich item in `result.outputs` (old VM, SQL, text only, error with nothing shown):
 *    today's mapping of result.html / result.image / result.image_s3_key
 *  - exactly one rich item and no text after it: that item in the legacy fields
 *  - otherwise: html = envelope, image = null, imageS3Key = the LAST image's key
 */
export function outputFieldsFromResult(result) {
  const r = result || {}
  const legacy = {
    html: r.html || null,
    image: r.image || null,
    imageS3Key: r.image_s3_key || null,
  }
  const items = (Array.isArray(r.outputs) ? r.outputs : []).map(normalizeItem).filter(Boolean)
  const rich = items.filter(isRich)
  if (rich.length === 0) return legacy
  const images = rich.filter(it => it.t === 'image')
  const lastKey = (images.length ? images[images.length - 1].key : null) || r.image_s3_key || null
  if (needsEnvelope(items)) {
    return { html: buildOutputsEnvelope(items), image: null, imageS3Key: lastKey }
  }
  const one = rich[0]
  if (one.t === 'table') return { ...legacy, html: r.html || JSON.stringify(one.payload) }
  if (one.t === 'plotly') return { ...legacy, html: r.html || one.html }
  return { ...legacy, image: r.image || one.src || null, imageS3Key: one.key || r.image_s3_key || null }
}

/** `html` with every image item's base64 `src` removed (the stored form). Any other string is returned unchanged. */
export function stripEnvelopeImages(html) {
  if (!isEnvelope(html) || !html.includes('"src"')) return html
  let doc
  try {
    doc = JSON.parse(html)
  } catch {
    return html
  }
  if (!isObj(doc) || !Array.isArray(doc.items)) return html
  let changed = false
  const items = doc.items.map((it) => {
    if (isObj(it) && it.t === 'image' && 'src' in it) {
      changed = true
      const { src: _src, ...rest } = it
      return rest
    }
    return it
  })
  return changed ? JSON.stringify({ ...doc, items }) : html
}

/**
 * Fill the image items of an envelope with data URLs, in order. `dataUrls` is an
 * array (index i → the i-th image item) of data URLs or falsy for "none". Items
 * that already have a `src` keep it. Non-envelopes are returned unchanged.
 */
export function fillEnvelopeImages(html, dataUrls) {
  if (!isEnvelope(html) || !Array.isArray(dataUrls) || dataUrls.length === 0) return html
  const items = parseOutputsEnvelope(html)
  if (!items) return html
  let n = 0
  const filled = items.map((it) => {
    if (it.t !== 'image') return it
    const url = dataUrls[n++]
    return !it.src && typeof url === 'string' && url ? { ...it, src: url } : it
  })
  return buildOutputsEnvelope(filled)
}

function hashString(s) {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(36)
}

function contentSig(it) {
  if (it.t === 'table') return hashString(JSON.stringify(it.payload))
  if (it.t === 'plotly') return hashString(it.html)
  if (it.t === 'image') return hashString(it.key || it.src || '')
  return ''
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

/** The one-line caption of a rich item (no chevron): "Table 2 · upstream — 12 rows × 2 cols", "Chart 3 · Plotly", "Image 2". */
export function outputCaption(item) {
  const n = item.ordinal || 1
  if (item.t === 'table') {
    const p = item.payload || {}
    const rows = Number.isFinite(p.total_rows) ? p.total_rows : (p.cells || []).length
    const cols = Number.isFinite(p.total_cols) ? p.total_cols : (p.columns || []).length
    return `Table ${n}${item.label ? ` · ${item.label}` : ''} — ${plural(rows, 'row', 'rows')} × ${plural(cols, 'col', 'cols')}`
  }
  if (item.t === 'plotly') return `Chart ${n}${item.label ? ` · ${item.label}` : ''} · Plotly`
  if (item.t === 'image') return `Image ${n}`
  return ''
}

/**
 * The ordered items a cell shows, from the envelope or built from the legacy
 * fields in the order CellOutput always drew them (image, text, table/Plotly).
 * Every item: { t, index, ...fields, sig, ordinal, caption, defaultExpanded }.
 *  - envelope cells: only the first rich item is defaultExpanded
 *  - legacy cells: every item is (the old one-of-each view is unchanged)
 * Text items have no ordinal/caption/sig.
 */
export function cellOutputItems(cell) {
  if (!cell) return []
  const env = parseOutputsEnvelope(cell.html)
  let base
  let multi = false
  if (env) {
    base = env
    multi = true
  } else {
    base = []
    if (cell.image) base.push({ t: 'image', src: cell.image })
    else if (cell.imageS3Key) base.push({ t: 'image', key: cell.imageS3Key, legacy: true })
    if (cell.output) base.push({ t: 'text', text: cell.output })
    if (typeof cell.html === 'string' && cell.html && !isEnvelope(cell.html)) {
      if (cell.html.includes('data-plotly="true"')) base.push({ t: 'plotly', html: cell.html })
      else {
        const payload = parseTablePayload(cell.html)
        if (payload) base.push({ t: 'table', payload })
      }
    }
  }
  const counts = { table: 0, plotly: 0, image: 0 }
  let seenRich = false
  return base.map((it, index) => {
    if (!isRich(it)) return { ...it, index }
    counts[it.t] += 1
    const out = { ...it, index, ordinal: counts[it.t], sig: contentSig(it), defaultExpanded: !multi || !seenRich }
    seenRich = true
    out.caption = outputCaption(out)
    return out
  })
}

/** True when the cell's output is a multi-output envelope. */
export function isMultiOutput(cell) {
  return !!cell && isEnvelope(cell.html) && parseOutputsEnvelope(cell.html) !== null
}

/** The first table payload of a cell (envelope or legacy), else null. */
export function firstTable(cell) {
  const t = cellOutputItems(cell).find(it => it.t === 'table')
  return t ? t.payload : null
}

/** True when the cell has at least one table, chart or image to show. */
export function hasRichOutput(cell) {
  return cellOutputItems(cell).some(isRich)
}

/** The kind of the first rich item ('table' | 'plotly' | 'image') or null. */
export function firstRichKind(cell) {
  const it = cellOutputItems(cell).find(isRich)
  return it ? it.t : null
}

/** Counts of rich items by kind, e.g. { table: 3, plotly: 2, image: 3 }. */
export function richCounts(cell) {
  const c = { table: 0, plotly: 0, image: 0 }
  for (const it of cellOutputItems(cell)) if (isRich(it)) c[it.t] += 1
  return c
}
