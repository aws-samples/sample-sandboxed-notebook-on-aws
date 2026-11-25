import { memo, useEffect, useMemo, useRef, useState } from 'react'
import {
  layoutMap, uniformRects, viewportWindow, scrollTopForMapClick, scrollTopForWindowTop,
  tickRole, statusLaneKind,
} from '../../services/outlineMap'

// Pointer travel (px) before a press on the strip becomes a drag instead of a click.
const DRAG_THRESHOLD_PX = 3

// Measure every cell of the notebook's scroll container (.cells-container) in content
// coordinates. Reads only, batched into one animation frame by the caller.
function measureCells(container) {
  const cr = container.getBoundingClientRect()
  const base = container.scrollTop - cr.top - container.clientTop
  const rects = new Map()
  for (const el of container.querySelectorAll('.cell[data-cell-id]')) {
    const r = el.getBoundingClientRect()
    rects.set(el.getAttribute('data-cell-id'), { top: r.top + base, height: r.height })
  }
  return { rects, contentHeight: container.scrollHeight }
}

// Cell map on the right of the Outline: one tick per cell, sized like the cell in the
// notebook, headings as wide section bands, a status lane for exceptions, and a window
// showing the visible part of the notebook. Always reflects ALL cells (ignores the
// Outline's search/filter/collapse).
function OutlineMap({ items, activeCellId, lineageActive, relationOf, onScrollToCell, notebookKey }) {
  const [geom, setGeom] = useState(null)          // { rects: Map, contentHeight } | null
  const [mapHeight, setMapHeight] = useState(0)
  const [hover, setHover] = useState(null)        // floating label state | null
  const [dragging, setDragging] = useState(false)
  const wrapRef = useRef(null)
  const trackRef = useRef(null)
  const windowRef = useRef(null)
  const containerRef = useRef(null)
  const layoutRef = useRef(null)
  const dragRef = useRef(null)
  const onScrollToCellRef = useRef(onScrollToCell)
  onScrollToCellRef.current = onScrollToCell

  const cellIdsKey = useMemo(() => items.map(i => i.id).join('|'), [items])

  // Strip height: the track fills the Outline list area.
  useEffect(() => {
    const track = trackRef.current
    if (!track) return
    const ro = new ResizeObserver(() => setMapHeight(track.clientHeight))
    ro.observe(track)
    setMapHeight(track.clientHeight)
    return () => ro.disconnect()
  }, [])

  // Bumped when the notebook mounts a fresh .cells-container (App -> Notebook view,
  // see Notebook.jsx), so the geometry effect below re-attaches to the new element.
  const [containerGen, setContainerGen] = useState(0)
  // False while the observed container is detached (App view): the last notebook
  // geometry stays drawn, but there is no notebook viewport to show.
  const [containerLive, setContainerLive] = useState(true)
  useEffect(() => {
    const onMount = () => setContainerGen(g => g + 1)
    window.addEventListener('notebook-cells-container', onMount)
    return () => window.removeEventListener('notebook-cells-container', onMount)
  }, [])

  // Cell geometry. ResizeObserver on the container and on each cell catches output
  // changes, collapse/expand and window resizes; a childList MutationObserver catches
  // add/delete/reorder. All of them only schedule one batched measure per frame.
  // Scrolling never re-measures. A detached container (App view unmounts it; the
  // ResizeObserver then reports zero sizes) is never measured: that used to squash
  // every tick into the top of the strip until the Outline remounted.
  useEffect(() => {
    const container = document.querySelector('.cells-container')
    containerRef.current = container
    if (!container) { setGeom(null); setContainerLive(false); return }
    setContainerLive(true)
    let raf = 0
    const measure = () => {
      raf = 0
      if (!container.isConnected) { setContainerLive(false); return }
      setGeom(measureCells(container))
    }
    const schedule = () => { if (!raf) raf = requestAnimationFrame(measure) }
    const ro = new ResizeObserver(schedule)
    const observeAll = () => {
      ro.disconnect()
      ro.observe(container)
      container.querySelectorAll('.cell[data-cell-id]').forEach(el => ro.observe(el))
    }
    observeAll()
    const mo = new MutationObserver(() => { observeAll(); schedule() })
    mo.observe(container, { childList: true })
    schedule()
    return () => {
      if (raf) cancelAnimationFrame(raf)
      ro.disconnect()
      mo.disconnect()
    }
  }, [cellIdsKey, notebookKey, containerGen])

  const layout = useMemo(() => {
    if (!mapHeight || !items.length) return null
    const measured = geom && items.every(i => geom.rects.has(String(i.id)))
    const rects = measured ? items.map(i => geom.rects.get(String(i.id))) : uniformRects(items.length)
    const fallback = rects.at(-1).top + rects.at(-1).height
    return {
      ...layoutMap({ rects, contentHeight: measured ? geom.contentHeight : fallback, mapHeight }),
      measured,
    }
  }, [geom, items, mapHeight])
  layoutRef.current = layout

  // Viewport window: positioned straight on the DOM node from the scroll event (one
  // update per frame), so scrolling the notebook never re-renders the Outline.
  useEffect(() => {
    const container = containerRef.current
    const win = windowRef.current
    if (!container || !win || !layout || !layout.measured || !containerLive) return
    let raf = 0
    const place = () => {
      raf = 0
      const w = viewportWindow(layout, container.scrollTop, container.clientHeight)
      win.style.transform = `translateY(${w.top}px)`
      win.style.height = `${w.height}px`
      win.dataset.top = String(w.top)
      win.dataset.height = String(w.height)
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(place) }
    place()
    container.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      if (raf) cancelAnimationFrame(raf)
      container.removeEventListener('scroll', onScroll)
    }
  }, [layout, containerLive])

  // Press on the strip: a short press is a click (tick -> jump to that cell, empty
  // area -> centre the notebook there); moving past the threshold drags the window
  // (grabbed where pressed when inside it, else centred on the pointer).
  const trackY = (e) => e.clientY - trackRef.current.getBoundingClientRect().top
  const onPointerDown = (e) => {
    if (e.button !== 0 || !layoutRef.current) return
    const y = trackY(e)
    const win = windowRef.current
    const wTop = Number(win?.dataset.top || 0)
    const wH = Number(win?.dataset.height || 0)
    const inside = layoutRef.current.measured && containerRef.current?.isConnected && y >= wTop && y <= wTop + wH
    const tick = e.target.closest?.('[data-map-idx]')
    dragRef.current = {
      startY: y, moved: false, grab: inside ? y - wTop : wH / 2,
      idx: tick ? Number(tick.getAttribute('data-map-idx')) : null,
    }
    e.currentTarget.setPointerCapture?.(e.pointerId)
    e.preventDefault()
  }
  const onPointerMove = (e) => {
    const d = dragRef.current
    const container = containerRef.current
    const lay = layoutRef.current
    if (!d || !container?.isConnected || !lay?.measured) return
    const y = trackY(e)
    if (!d.moved && Math.abs(y - d.startY) < DRAG_THRESHOLD_PX) return
    if (!d.moved) { d.moved = true; setDragging(true); setHover(null) }
    container.scrollTop = scrollTopForWindowTop(lay, y - d.grab, container.clientHeight, container.scrollHeight)
  }
  const endPress = (e, cancelled) => {
    const d = dragRef.current
    dragRef.current = null
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    if (!d) return
    if (d.moved) { setDragging(false); return }
    if (cancelled) return
    if (d.idx != null) { onScrollToCellRef.current?.(d.idx); return }
    const container = containerRef.current
    const lay = layoutRef.current
    if (!container?.isConnected || !lay?.measured) return
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    container.scrollTo({
      top: scrollTopForMapClick(lay, d.startY, container.clientHeight, container.scrollHeight),
      behavior: reduceMotion ? 'instant' : 'smooth',
    })
  }

  const segs = layout?.segments || []

  return (
    <div className="outline-minimap-wrap" ref={wrapRef}>
      <div
        className={`outline-minimap${dragging ? ' dragging' : ''}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={(e) => endPress(e, false)}
        onPointerCancel={(e) => endPress(e, true)}
      >
        <div className="outline-minimap-track" ref={trackRef}>
          {items.map((item, i) => {
            const seg = segs[i]
            if (!seg) return null
            const role = tickRole(item)
            const isRunnable = item.cellType !== 'markdown'
            const notRun = isRunnable && !item.hasOutput && !item.hasError && !item.isRunning
            const lane = statusLaneKind(item)
            const statusWord = !isRunnable ? (item.isSection ? 'heading' : 'text')
              : item.isRunning ? 'running'
              : item.hasError ? 'error'
              : lane === 'stale' ? 'stale'
              : item.hasOutput ? 'ran' : 'not run'
            // Lineage focus: related ticks take the relation colour, indirect ones
            // fainter; the rest dim like their rows.
            const rel = lineageActive ? relationOf(item) : null
            const relCls = !lineageActive ? ''
              : !rel ? ' outline-minimap-rel-none'
              : rel.kind === 'focus' ? ' outline-minimap-rel-focus'
              : ` outline-minimap-rel-${rel.kind}${rel.depth > 1 ? ' outline-minimap-rel-indirect' : ''}`
            const pos = { top: `${seg.tickY}px`, height: `${seg.tickH}px` }
            return [
              <button
                key={item.id}
                type="button"
                data-map-idx={item.idx}
                className={`outline-minimap-tick outline-minimap-${role} outline-minimap-${item.cellType}${notRun ? ' outline-minimap-unrun' : ''}${relCls}${activeCellId === item.id ? ' active' : ''}`}
                style={pos}
                // Pointer presses are handled on the strip; this is keyboard activation.
                onClick={(e) => { if (e.detail === 0) onScrollToCellRef.current?.(item.idx) }}
                onMouseEnter={(e) => {
                  if (dragRef.current?.moved) return
                  const tr = e.currentTarget.getBoundingClientRect()
                  const wr = wrapRef.current?.getBoundingClientRect()
                  if (!wr) return
                  setHover({
                    id: item.id, icon: item.icon, label: item.label,
                    statusWord, cellType: item.cellType, isSection: item.isSection,
                    top: tr.top - wr.top + tr.height / 2,
                  })
                }}
                onMouseLeave={() => setHover(prev => (prev && prev.id === item.id ? null : prev))}
                aria-label={`Jump to ${item.icon}, ${statusWord}`}
              />,
              lane && (
                <span
                  key={`${item.id}-lane`}
                  className={`outline-minimap-status outline-minimap-status-${lane}`}
                  style={pos}
                  aria-hidden="true"
                />
              ),
            ]
          })}
          <div
            className="outline-minimap-window"
            ref={windowRef}
            hidden={!layout?.measured || !containerLive}
            aria-hidden="true"
          />
        </div>
      </div>
      {/* Hover label: badge + title + status beside the hovered tick. pointer-events
          off so it never steals the hover from the ticks. */}
      {hover && !dragging && (
        <div className="outline-minimap-label" style={{ top: `${hover.top}px` }} role="presentation">
          <span className={`outline-item-icon outline-icon-${hover.cellType}`}>{hover.icon}</span>
          <span className="outline-minimap-label-text">{hover.label || '(untitled)'}</span>
          {!hover.isSection && (
            <span className={`outline-minimap-label-status outline-minimap-label-${hover.statusWord.replace(/\s+/g, '-')}`}>{hover.statusWord}</span>
          )}
        </div>
      )}
    </div>
  )
}

export default memo(OutlineMap)
