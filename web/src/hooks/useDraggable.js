import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * useDraggable — makes a fixed/floating element grabbable with the mouse and
 * remembers where the user dropped it across sessions (and across notebooks),
 * persisting to localStorage under `storageKey`.
 *
 * Until the user drags it the first time, `position` is null so the element
 * keeps its CSS default anchor (e.g. bottom-center for the HUD, bottom-right
 * for the AI FAB). Once dragged, an explicit {x, y} top-left position (in px,
 * viewport coordinates) is applied and saved.
 *
 * Click-vs-drag: the handlers only begin an actual move once the pointer has
 * travelled past DRAG_THRESHOLD px, so a plain click on a draggable button
 * still fires its onClick. Read `didDragRef.current` inside a click handler to
 * suppress the click when it was really the end of a drag.
 *
 * Returns:
 *   position    — {x, y} | null (null = use CSS default anchor)
 *   dragging    — true while a drag is in progress
 *   onPointerDown — attach to the element (or its drag handle)
 *   didDragRef  — ref; true if the last pointer interaction was a drag
 *   reset       — clear the saved position (back to CSS default)
 */
const DRAG_THRESHOLD = 4 // px of travel before a press becomes a drag

function loadSaved(storageKey) {
  try {
    const raw = localStorage.getItem(storageKey)
    if (!raw) return null
    const p = JSON.parse(raw)
    if (typeof p?.x === 'number' && typeof p?.y === 'number') return p
  } catch { /* ignore malformed/absent */ }
  return null
}

export function useDraggable(storageKey) {
  const [position, setPosition] = useState(() => loadSaved(storageKey))
  const [dragging, setDragging] = useState(false)

  // Mutable drag bookkeeping (avoids re-renders mid-drag).
  const stateRef = useRef({
    pointerId: null,
    startX: 0, startY: 0,      // pointer position at press
    originX: 0, originY: 0,    // element top-left at press
    moved: false,
    width: 0, height: 0,
  })
  const didDragRef = useRef(false)

  // Clamp a top-left position so the element stays fully on screen.
  const clamp = useCallback((x, y, w, h) => {
    const maxX = Math.max(0, window.innerWidth - w)
    const maxY = Math.max(0, window.innerHeight - h)
    return { x: Math.min(Math.max(0, x), maxX), y: Math.min(Math.max(0, y), maxY) }
  }, [])

  const onPointerMove = useCallback((e) => {
    const s = stateRef.current
    if (s.pointerId === null) return
    const dx = e.clientX - s.startX
    const dy = e.clientY - s.startY
    if (!s.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return
    s.moved = true
    didDragRef.current = true
    setDragging(true)
    setPosition(clamp(s.originX + dx, s.originY + dy, s.width, s.height))
  }, [clamp])

  const endDrag = useCallback(() => {
    const s = stateRef.current
    if (s.pointerId === null) return
    s.pointerId = null
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', endDrag)
    window.removeEventListener('pointercancel', endDrag)
    setDragging(false)
    if (s.moved) {
      // Persist wherever it landed.
      setPosition(prev => {
        if (prev) {
          try { localStorage.setItem(storageKey, JSON.stringify(prev)) } catch { /* quota/denied */ }
        }
        return prev
      })
    }
  }, [onPointerMove, storageKey])

  const onPointerDown = useCallback((e) => {
    // Only primary button / touch / pen.
    if (e.button != null && e.button !== 0) return
    const el = e.currentTarget
    const rect = el.getBoundingClientRect()
    const s = stateRef.current
    s.pointerId = e.pointerId ?? 1
    s.startX = e.clientX
    s.startY = e.clientY
    s.originX = rect.left
    s.originY = rect.top
    s.width = rect.width
    s.height = rect.height
    s.moved = false
    didDragRef.current = false
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', endDrag)
    window.addEventListener('pointercancel', endDrag)
  }, [onPointerMove, endDrag])

  // Keep it on-screen if the window shrinks after it was placed.
  useEffect(() => {
    if (!position) return
    const onResize = () => setPosition(p => (p ? clamp(p.x, p.y, stateRef.current.width, stateRef.current.height) : p))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [position, clamp])

  // Safety: detach global listeners on unmount.
  useEffect(() => () => {
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', endDrag)
    window.removeEventListener('pointercancel', endDrag)
  }, [onPointerMove, endDrag])

  const reset = useCallback(() => {
    try { localStorage.removeItem(storageKey) } catch { /* ignore */ }
    setPosition(null)
  }, [storageKey])

  return { position, dragging, onPointerDown, didDragRef, reset }
}
