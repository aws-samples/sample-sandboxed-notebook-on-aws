import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../services/apiFetch'
import { createCellImageLoader } from '../services/cellImage'

// One loader for the whole app: the inline cell, the focus overlay and App mode
// share a fetch + object URL per chart key (refcounted, revoked at zero).
// apiFetch adds X-Proxy-Auth — an <img src> pointing at the route could not.
const loader = createCellImageLoader({
  fetchImpl: (path) => apiFetch(path),
  createObjectURL: (blob) => URL.createObjectURL(blob),
  revokeObjectURL: (url) => URL.revokeObjectURL(url),
})

/**
 * A cell's PERSISTED chart (after reload/reopen the base64 image is gone; only
 * `imageS3Key` remains). Fetched lazily when near the viewport through the
 * authenticated proxy route, rendered from a Blob object URL. Never renders an
 * <img> without a resolved URL; `anyKey` loads any image the cell lists (the images of a
 * multi-output cell, `?key=`) instead of only its last one (`?sha=`); a missing object shows an explicit
 * "Chart unavailable — re-run the cell", other failures offer Retry.
 */
export default function PersistedCellImage({ cellId, imageKey, anyKey = false, alt = 'Plot output' }) {
  const ref = useRef(null)
  const [visible, setVisible] = useState(false)
  const [state, setState] = useState({ status: 'loading', url: null })
  const [attempt, setAttempt] = useState(0)

  // Lazy: start loading when the placeholder is within 300px of the viewport.
  useEffect(() => {
    if (visible) return undefined
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setVisible(true)
      return undefined
    }
    const io = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) {
        setVisible(true)
        io.disconnect()
      }
    }, { rootMargin: '300px' })
    io.observe(el)
    return () => io.disconnect()
  }, [visible])

  useEffect(() => {
    if (!visible || !imageKey) return undefined
    let cancelled = false
    setState({ status: 'loading', url: null })
    loader.acquire(cellId, imageKey, { any: anyKey }).then((res) => {
      if (!cancelled) setState({ status: res.status, url: res.url || null })
    })
    return () => {
      cancelled = true
      loader.release(imageKey)
    }
  }, [visible, cellId, imageKey, anyKey, attempt])

  if (state.status === 'ok' && state.url) {
    return (
      <div className="output-image">
        <img
          src={state.url}
          alt={alt}
          onError={() => setState({ status: 'missing', url: null })}
        />
      </div>
    )
  }
  if (state.status === 'missing') {
    return (
      <div className="output-image-unavailable" role="status">
        Chart unavailable — re-run the cell
      </div>
    )
  }
  if (state.status === 'error') {
    return (
      <div className="output-image-unavailable" role="status">
        Chart couldn&apos;t be loaded.{' '}
        <button type="button" className="output-text-toggle" onClick={() => setAttempt(a => a + 1)}>
          Retry
        </button>
      </div>
    )
  }
  return (
    <div
      ref={ref}
      className="output-image-loading"
      role="status"
      aria-label="Loading chart"
      style={{ minHeight: 160 }}
    />
  )
}
