// Persisted chart images: load a cell's chart PNG from the authenticated proxy
// route and hand it to an <img> as a Blob object URL.
//
// Pure module (no config import; fetch / URL functions are injected) so it is
// unit-tested with node --test (services/__tests__/cellImage.test.mjs).
//
// Why a Blob: GET /notebooks/{id}/cells/{cell}/image sits behind the proxy's
// X-Proxy-Auth gate, and an <img src> cannot send that header. So the request
// goes through apiFetch (which adds the header), the PNG becomes a Blob, and the
// <img> gets a `blob:` object URL. URLs are refcounted per key (the inline cell
// and the focus overlay share one fetch) and revoked when the last user releases.
//
// The client never chooses which S3 object is read: the route reads the stored
// key for (notebook, cell) itself. `notebookIdFromKey` only tells us which
// notebook the cell's key belongs to, and `sha` lets the server reject a stale
// request after a re-run.

import { fillEnvelopeImages, isEnvelope, parseOutputsEnvelope } from './cellOutputs.js'

const KEY_RE = /^notebook-outputs\/(\d+)\/[A-Za-z0-9_-]{1,130}\/([0-9a-f]{64})\.png$/

/** The sha256 hex of a well-formed chart key, else null. */
export function imageShaFromKey(key) {
  const m = typeof key === 'string' ? KEY_RE.exec(key) : null
  return m ? m[2] : null
}

/** The notebook id a well-formed chart key belongs to, else null. */
export function notebookIdFromKey(key) {
  const m = typeof key === 'string' ? KEY_RE.exec(key) : null
  return m ? m[1] : null
}

/**
 * Proxy path for a cell's chart, or null for a malformed key.
 * `{ any: true }` selects any image the cell lists (`?key=<sha>`: the images of a
 * multi-output cell); the default `?sha=<sha>` only matches the cell's LAST image.
 */
export function cellImagePath(cellId, key, { any = false } = {}) {
  const nid = notebookIdFromKey(key)
  const sha = imageShaFromKey(key)
  if (!nid || !sha || cellId == null || cellId === '') return null
  return `/notebooks/${nid}/cells/${encodeURIComponent(String(cellId))}/image?${any ? 'key' : 'sha'}=${sha}`
}

/**
 * Refcounted, deduplicated loader.
 *   acquire(cellId, key, { any }) → Promise<{status:'ok', url} | {status:'missing'} | {status:'error'}>
 *   release(key)         → drop one reference; revokes the URL at zero
 * 404 (and a malformed key) → 'missing', cached for the session (no refetch).
 * Other HTTP errors, network errors and a non-PNG body → 'error', not cached
 * (a later acquire retries).
 */
export function createCellImageLoader({ fetchImpl, createObjectURL, revokeObjectURL }) {
  const entries = new Map()   // key -> { refs, promise, url }
  const missing = new Set()

  async function load(cellId, key, entry, opts) {
    const path = cellImagePath(cellId, key, opts)
    if (!path) {
      missing.add(key)
      return { status: 'missing' }
    }
    let resp
    try {
      resp = await fetchImpl(path)
    } catch {
      return { status: 'error' }
    }
    if (resp.status === 404) {
      missing.add(key)
      return { status: 'missing' }
    }
    if (!resp.ok) return { status: 'error' }
    let blob
    try {
      blob = await resp.blob()
    } catch {
      return { status: 'error' }
    }
    if (!blob || blob.type !== 'image/png') return { status: 'error' }
    if (entry.refs <= 0) return { status: 'error' }   // released while loading
    entry.url = createObjectURL(blob)
    return { status: 'ok', url: entry.url }
  }

  // Every acquire(key) must be paired with exactly one release(key), whatever
  // the outcome — the refcount is per key, independent of the load result.
  function acquire(cellId, key, opts = {}) {
    if (!key) return Promise.resolve({ status: 'missing' })
    let entry = entries.get(key)
    if (!entry) {
      entry = { refs: 0, promise: null, url: null }
      entries.set(key, entry)
    }
    entry.refs += 1
    if (missing.has(key)) return Promise.resolve({ status: 'missing' })
    if (!entry.promise) {
      const p = load(cellId, key, entry, opts).then((res) => {
        // 'missing' is remembered in `missing`; 'error' retries on the next acquire.
        if (res.status !== 'ok' && entry.promise === p) entry.promise = null
        return res
      })
      entry.promise = p
    }
    return entry.promise
  }

  function release(key) {
    const entry = entries.get(key)
    if (!entry) return
    entry.refs -= 1
    if (entry.refs > 0) return
    entries.delete(key)
    if (entry.url) {
      revokeObjectURL(entry.url)
      entry.url = null
    }
  }

  return { acquire, release, _entries: entries, _missing: missing }
}

/** Blob → data URL (export embeds the chart in a self-contained file). */
export function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(r.error)
    r.readAsDataURL(blob)
  })
}

/**
 * Fetch a cell's persisted chart as a data URL, or null on any failure.
 * Used by exports (never by copy-to-clipboard, which must not await a fetch).
 */
export async function loadCellImageDataUrl(fetchImpl, cellId, key, toDataUrl = blobToDataUrl, opts = {}) {
  const path = cellImagePath(cellId, key, opts)
  if (!path) return null
  try {
    const resp = await fetchImpl(path)
    if (!resp.ok) return null
    const blob = await resp.blob()
    if (!blob || blob.type !== 'image/png') return null
    return await toDataUrl(blob)
  } catch {
    return null
  }
}

/**
 * Cells for an export: any cell with a persisted key but no in-memory image gets
 * `image` filled with a data URL fetched through the route. A cell whose fetch
 * fails is exported without an image (never a broken <img>). Input is not mutated.
 */
export async function resolveExportImages(cells, loadDataUrl) {
  return Promise.all((cells || []).map(async (c) => {
    if (!c) return c
    // Multi-output cell: fill the `src` of every envelope image that has a key.
    if (isEnvelope(c.html)) return resolveEnvelopeImages(c, loadDataUrl)
    if (c.image || !c.imageS3Key) return c
    const url = await loadDataUrl(c.id, c.imageS3Key)
    return url ? { ...c, image: url } : c
  }))
}

async function resolveEnvelopeImages(c, loadDataUrl) {
  const items = parseOutputsEnvelope(c.html)
  if (!items || !items.some(it => it.t === 'image' && !it.src && it.key)) return c
  const urls = await Promise.all(items.filter(it => it.t === 'image').map(
    it => (it.src || !it.key ? null : loadDataUrl(c.id, it.key, { any: true }))))
  return urls.some(Boolean) ? { ...c, html: fillEnvelopeImages(c.html, urls) } : c
}
