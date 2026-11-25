/**
 * notebookImport — the ONE parser for opened notebook files (.notebook.json and
 * Jupyter .ipynb), shared by the Notebook toolbar's Open, the Welcome screen's
 * Open file, and App's `open-notebook` handler. Pure, Node-loadable.
 *
 * Native files and our own .ipynb exports restore every persisted cell field
 * (see notebookExport PERSISTED_CELL_FIELDS); a foreign .ipynb imports its code,
 * `%%sql` cells, text outputs, errors and images.
 */
import { persistedCell } from './notebookExport.js'
import { fillEnvelopeImages, isEnvelope } from './cellOutputs.js'

const _join = (v) => (Array.isArray(v) ? v.join('') : v == null ? '' : String(v))

/**
 * A cell ready for a new tab: the persisted projection (types coerced), a fresh
 * id when the file has none, no S3 key (it would belong to the source notebook)
 * and an idle status. `newId` is a string or a () => string factory.
 */
export function normalizeImportedCell(c, newId) {
  const p = persistedCell(c)
  if (!p.id) p.id = typeof newId === 'function' ? newId() : (newId || null)
  // The chart comes in as a data URL; its S3 key never does (it belongs to the
  // source notebook; the cell re-persists its chart when it runs again).
  return { ...p, image: p.image, imageS3Key: null, status: 'idle' }
}

function _ipynbCell(c) {
  const src = _join(c.source)
  const extra = c.metadata && typeof c.metadata.microvm_notebook === 'object' && c.metadata.microvm_notebook
    ? c.metadata.microvm_notebook : null
  let type = c.cell_type === 'markdown' ? 'markdown' : 'code'
  let code = src
  if (type === 'code' && /^\s*%%sql\b/.test(src)) {
    type = 'sql'
    code = src.trimStart().replace(/^%%sql[^\n]*\n?/, '')
  }
  if (extra && (extra.type === 'sql' || extra.type === 'markdown' || extra.type === 'code')) {
    // Our own export: the metadata type wins (a %%sql prefix only on sql cells).
    if (extra.type !== 'sql' && type === 'sql') code = src
    type = extra.type
  }
  // Standard outputs.
  let output = null
  let error = null
  let image = null
  // Every image output, in item order (a multi-output cell carries several). An
  // output our own export tagged with its position among the cell's images goes back there.
  const images = []
  const texts = []
  for (const o of Array.isArray(c.outputs) ? c.outputs : []) {
    if (!o || typeof o !== 'object') continue
    if (o.output_type === 'stream') {
      if (o.name === 'stderr') error = (error ? error + '\n' : '') + _join(o.text)
      else texts.push(_join(o.text))
    } else if (o.output_type === 'error') {
      error = Array.isArray(o.traceback) && o.traceback.length
        ? o.traceback.join('\n') : `${o.ename || 'Error'}: ${o.evalue || ''}`
    } else if (o.output_type === 'display_data' || o.output_type === 'execute_result') {
      const d = o.data || {}
      const img = ['image/png', 'image/jpeg', 'image/gif'].find(k => d[k])
      if (img) {
        const url = `data:${img};base64,${_join(d[img]).replace(/\s+/g, '')}`
        const pos = o.metadata && o.metadata.microvm_notebook && o.metadata.microvm_notebook.image
        if (Number.isInteger(pos) && pos >= 1 && pos <= 1000 && images[pos - 1] === undefined) images[pos - 1] = url
        else images.push(url)
        continue
      }
      // Foreign text/html is NOT imported as an html output (it would render raw);
      // its text/plain (or nothing) is kept. Our own html comes back via metadata.
      if (d['text/plain'] != null && !d['text/html'] && !img) texts.push(_join(d['text/plain']))
      else if (d['text/plain'] != null && d['text/html'] && !extra) texts.push(_join(d['text/plain']))
    }
  }
  if (texts.length) output = texts.join('')
  image = images.find(Boolean) || null
  const base = { type, code, output, error, image, executionNumber: Number.isInteger(c.execution_count) ? c.execution_count : null }
  if (!extra) return base
  // A multi-output cell (html is the outputs envelope): its images go back INTO the
  // envelope, in order; the cell itself has no single `image`.
  const envelope = isEnvelope(extra.html)
  // Our own export: the exact persisted values win over the standard projection.
  return {
    ...base,
    ...extra,
    type,
    code,
    output,
    image: envelope ? null : image,
    ...(envelope ? { html: fillEnvelopeImages(extra.html, images) } : {}),
    error: extra.error !== undefined ? extra.error : error,
    executionNumber: extra.executionNumber !== undefined ? extra.executionNumber : base.executionNumber,
  }
}

/** Parse a Jupyter notebook object → { name, description, tag, cells }. */
export function parseIpynb(data, fileName = 'notebook.ipynb') {
  const meta = (data && data.metadata && data.metadata.microvm_notebook) || null
  const cells = (data.cells || [])
    .filter(c => c && (c.cell_type === 'code' || c.cell_type === 'markdown'))
    .map(_ipynbCell)
  const base = String(fileName || '').replace(/\.ipynb$/i, '') || 'Notebook'
  return {
    name: (meta && meta.name) || base,
    description: meta ? (meta.description || '') : `Imported from Jupyter: ${fileName}`,
    tag: (meta && meta.tag) || null,
    cells,
  }
}

/**
 * Parse an opened notebook file's JSON (ipynb or native) →
 * { name, description, tag, cells }. Throws on anything else.
 */
export function parseNotebookFile(data, fileName = '') {
  if (!data || typeof data !== 'object' || !Array.isArray(data.cells)) {
    throw new Error('Not a notebook file')
  }
  if (data.nbformat) return parseIpynb(data, fileName)
  return {
    name: data.name || String(fileName || '').replace(/\.notebook\.json$/i, '').replace(/\.json$/i, '') || 'Notebook',
    description: data.description || '',
    tag: data.tag || null,
    cells: data.cells,
  }
}
