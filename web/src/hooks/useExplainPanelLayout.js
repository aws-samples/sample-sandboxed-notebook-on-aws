// Layout + highlight state for the focus-mode Explanation panel.
//  - useExplainPanelLayout: open/closed + width (remembered for the page session),
//    the stacked (narrow) switch, and the splitter drag / keyboard resize.
//  - useExplainHighlight: hovered / pinned section range -> the editor `highlight` prop.
import { useState, useRef, useCallback, useEffect, useLayoutEffect, useMemo } from 'react'
import { showDragOverlay, hideDragOverlay } from '../utils/dragOverlay'
import {
  getPanelPrefs, setPanelPrefs, clampPanelWidth, isStacked, activeRange, highlightAnnouncement,
  peekPanelOpenRequest, clearPanelOpenRequest,
} from '../services/explainPanel'

const KEY_STEP = 24

// `initialOpen`: the caller's rule for the first render (the overlay passes "this cell
// already has an explanation, or one is being generated"). A one-shot request from the
// notebook's Explain button also opens it. The remembered open/closed state of an
// earlier focus session is NOT used, so a cell with no data starts collapsed.
export function useExplainPanelLayout(initialOpen = false) {
  const [open, setOpenState] = useState(() => peekPanelOpenRequest() || !!initialOpen)
  useEffect(() => { clearPanelOpenRequest() }, [])
  const [width, setWidth] = useState(() => getPanelPrefs().width)
  const [containerW, setContainerW] = useState(0)
  const splitRef = useRef(null)
  const draggingRef = useRef(false)
  const dragStartRef = useRef({ x: 0, w: 0 })
  const widthRef = useRef(width)
  widthRef.current = width

  // The overlay body's own width decides beside vs stacked and the 55% cap.
  useLayoutEffect(() => {
    const el = splitRef.current
    if (!el) return undefined
    setContainerW(el.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver((entries) => {
      for (const en of entries) setContainerW(en.contentRect.width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const setOpen = useCallback((v) => {
    setOpenState(!!v)
  }, [])

  const stacked = isStacked(containerW)
  const effectiveWidth = clampPanelWidth(width, containerW)

  useEffect(() => {
    const onMove = (e) => {
      if (!draggingRef.current || !splitRef.current) return
      const rect = splitRef.current.getBoundingClientRect()
      const { x, w } = dragStartRef.current
      // Dragging left widens the panel (it sits on the right).
      setWidth(clampPanelWidth(w + (x - e.clientX), rect.width))
    }
    const onUp = () => {
      if (!draggingRef.current) return
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      hideDragOverlay()
      setPanelPrefs({ width: widthRef.current })
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      hideDragOverlay()
    }
  }, [])

  const startDrag = useCallback((e) => {
    e.preventDefault()
    draggingRef.current = true
    dragStartRef.current = { x: e.clientX, w: clampPanelWidth(widthRef.current, splitRef.current?.getBoundingClientRect().width || 0) }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    // Same trick as the editor/output splitter: a window-sized capture layer so a
    // Plotly iframe under the cursor cannot swallow the drag.
    showDragOverlay('col-resize')
  }, [])

  const onSplitterKey = useCallback((e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const rect = splitRef.current?.getBoundingClientRect()
    const base = clampPanelWidth(widthRef.current, rect?.width || 0)
    const next = clampPanelWidth(base + (e.key === 'ArrowLeft' ? KEY_STEP : -KEY_STEP), rect?.width || 0)
    setWidth(next)
    setPanelPrefs({ width: next })
  }, [])

  return { open, setOpen, width: effectiveWidth, stacked, containerW, splitRef, startDrag, onSplitterKey }
}

/**
 * `active` = the panel is open, has a result, and its line numbers still fit the code.
 * Hover / keyboard focus tint a section's lines; a click pins them and scrolls the
 * editor there. Both clear when `active` turns false.
 */
export function useExplainHighlight({ active }) {
  const [pinned, setPinned] = useState(null)
  const [hovered, setHovered] = useState(null)
  const counter = useRef(0)

  useEffect(() => {
    if (!active) {
      setPinned(null)
      setHovered(null)
    }
  }, [active])

  const hover = useCallback((range) => setHovered(range ? { start: range.start, end: range.end } : null), [])
  const pin = useCallback((range) => {
    counter.current += 1
    setPinned({ start: range.start, end: range.end, reveal: counter.current })
  }, [])
  const clear = useCallback(() => { setPinned(null); setHovered(null) }, [])

  const range = active ? activeRange(pinned, hovered) : null
  const highlight = useMemo(() => {
    if (!range) return null
    return {
      start: range.start,
      end: range.end,
      reveal: pinned ? pinned.reveal : 0,
      revealLine: pinned ? pinned.start : 0,
    }
  }, [range, pinned])
  const announcement = range ? highlightAnnouncement(range.start, range.end) : ''
  return { highlight, pinned, hover, pin, clear, announcement }
}
