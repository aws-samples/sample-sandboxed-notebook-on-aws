// App-side wiring of services/cellImage.js to the authenticated proxy (apiFetch
// adds X-Proxy-Auth). Kept separate so cellImage.js stays pure / node-testable.
import { apiFetch } from './apiFetch'
import { loadCellImageDataUrl, resolveExportImages } from './cellImage'

/**
 * Cells for a download export: persisted-only charts (imageS3Key, no in-memory
 * image) are fetched and embedded as data URLs; a failed fetch exports without
 * the image. Never used for copy-to-clipboard (that must not await a fetch).
 */
export function withPersistedImages(cells) {
  return resolveExportImages(cells, (cellId, key, opts) => loadCellImageDataUrl(apiFetch, cellId, key, undefined, opts))
}

/** One cell (per-cell output export). */
export async function withPersistedImage(cell) {
  const [c] = await withPersistedImages([cell])
  return c
}
