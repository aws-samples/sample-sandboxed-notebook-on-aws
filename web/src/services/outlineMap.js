// Pure geometry for the Outline's cell map (the strip on the right of the Outline).
//
// The map mirrors the notebook's scroll container (.cells-container). Each cell owns
// one SEGMENT of the notebook's content: from its top to the next cell's top (the
// first segment also takes the leading padding, the last one the trailing padding).
// Segments are scaled onto the strip so their map heights stay proportional to the
// notebook, with a floor (`minPx`) so tiny cells stay visible and clickable. The
// floor is paid for by shrinking the other segments, so the strip always fills the
// available height exactly. The mapping between notebook and strip is piecewise
// linear per segment, so the viewport window, clicks and drags all use the same
// geometry the ticks are drawn with.
//
// Nothing here touches the DOM; OutlineMap.jsx measures and renders.

export const MAP_MIN_SEGMENT_PX = 4   // smallest segment on the strip
export const MAP_MIN_TICK_PX = 2      // smallest drawn tick inside a segment
export const MAP_TICK_GAP_PX = 1      // gap kept below each tick so neighbours stay apart
export const MAP_MIN_WINDOW_PX = 8    // smallest viewport window, so it stays grabbable

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

// Scale `lengths` so they sum to `total`, keeping each at least `minPx` and the rest
// proportional to their length. Lengths that fall below the floor are pinned to it and
// the remainder is shared out again (water filling). When even the floors do not fit,
// every segment gets an equal share.
export function fitHeights(lengths, total, minPx = MAP_MIN_SEGMENT_PX) {
  const n = lengths.length
  if (n === 0) return []
  if (!(total > 0)) return lengths.map(() => 0)
  if (minPx * n >= total) return lengths.map(() => total / n)
  const pinned = new Array(n).fill(false)
  let out = []
  for (let pass = 0; pass <= n; pass++) {
    let freeLen = 0
    let pinnedCount = 0
    for (let i = 0; i < n; i++) {
      if (pinned[i]) pinnedCount++
      else freeLen += Math.max(0, lengths[i])
    }
    const remaining = total - pinnedCount * minPx
    const freeCount = n - pinnedCount
    out = lengths.map((len, i) => {
      if (pinned[i]) return minPx
      return freeLen > 0 ? (Math.max(0, len) * remaining) / freeLen : remaining / freeCount
    })
    let changed = false
    for (let i = 0; i < n; i++) {
      if (!pinned[i] && out[i] < minPx) { pinned[i] = true; changed = true }
    }
    if (!changed) break
  }
  return out
}

// Fallback rects when the notebook is not measurable (no .cells-container, e.g. the
// App view, or a cell has not rendered yet): equal heights, so the map still shows
// structure and order.
export function uniformRects(count, cellPx = 40, gapPx = 8) {
  return Array.from({ length: count }, (_, i) => ({ top: i * (cellPx + gapPx), height: cellPx }))
}

// rects: [{ top, height }] in notebook content coordinates, in notebook order.
// contentHeight: the scroll container's scrollHeight. mapHeight: strip height in px.
// Returns { mapHeight, contentHeight, segments: [{ start, end, y, h, tickY, tickH }] }.
export function layoutMap({ rects, contentHeight, mapHeight, minPx = MAP_MIN_SEGMENT_PX }) {
  const n = rects.length
  const lastBottom = n ? rects[n - 1].top + rects[n - 1].height : 0
  const total = Math.max(contentHeight || 0, lastBottom, 1)
  const starts = []
  for (let i = 0; i < n; i++) {
    const prev = i === 0 ? 0 : starts[i - 1]
    starts.push(i === 0 ? 0 : clamp(rects[i].top, prev, total))
  }
  const ends = starts.map((s, i) => (i + 1 < n ? starts[i + 1] : total))
  const heights = fitHeights(ends.map((e, i) => e - starts[i]), mapHeight, minPx)
  let y = 0
  const segments = []
  for (let i = 0; i < n; i++) {
    const start = starts[i]
    const end = ends[i]
    const h = heights[i]
    const len = end - start
    const k = len > 0 ? h / len : 0
    // Draw the cell's own extent inside its segment, not the margin below it.
    const room = Math.max(MAP_MIN_TICK_PX, h - MAP_TICK_GAP_PX)
    const tickH = clamp(rects[i].height * k, MAP_MIN_TICK_PX, room)
    const offset = clamp((rects[i].top - start) * k, 0, Math.max(0, h - tickH))
    segments.push({ start, end, y, h, tickY: y + offset, tickH })
    y += h
  }
  return { mapHeight, contentHeight: total, segments }
}

// Index of the segment covering `value` on the given axis ('content' or 'map').
function segmentAt(segments, value, axis) {
  let lo = 0
  let hi = segments.length - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    const s = segments[mid]
    if ((axis === 'content' ? s.start : s.y) <= value) lo = mid
    else hi = mid - 1
  }
  return lo
}

// Notebook content offset (px) -> strip offset (px).
export function contentToMap(layout, contentY) {
  const segs = layout.segments
  if (!segs.length) return 0
  const v = clamp(contentY, 0, layout.contentHeight)
  const s = segs[segmentAt(segs, v, 'content')]
  const len = s.end - s.start
  return s.y + (len > 0 ? ((v - s.start) / len) * s.h : 0)
}

// Strip offset (px) -> notebook content offset (px).
export function mapToContent(layout, mapY) {
  const segs = layout.segments
  if (!segs.length) return 0
  const last = segs[segs.length - 1]
  const v = clamp(mapY, 0, last.y + last.h)
  const s = segs[segmentAt(segs, v, 'map')]
  return s.start + (s.h > 0 ? ((v - s.y) / s.h) * (s.end - s.start) : 0)
}

// The viewport window on the strip for the notebook's current scroll position.
export function viewportWindow(layout, scrollTop, clientHeight, minPx = MAP_MIN_WINDOW_PX) {
  const top = contentToMap(layout, scrollTop)
  const bottom = contentToMap(layout, scrollTop + clientHeight)
  const height = Math.max(minPx, bottom - top)
  return { top: clamp(top, 0, Math.max(0, layout.mapHeight - height)), height }
}

const maxScroll = (scrollHeight, clientHeight) => Math.max(0, scrollHeight - clientHeight)

// Click on an empty part of the strip: centre the notebook viewport on that point.
export function scrollTopForMapClick(layout, mapY, clientHeight, scrollHeight) {
  return clamp(mapToContent(layout, mapY) - clientHeight / 2, 0, maxScroll(scrollHeight, clientHeight))
}

// Dragging the window: the window's top edge follows the pointer.
export function scrollTopForWindowTop(layout, windowTopY, clientHeight, scrollHeight) {
  return clamp(mapToContent(layout, windowTopY), 0, maxScroll(scrollHeight, clientHeight))
}

// Tick role: a markdown heading is a wide section band, every other cell is a
// narrower, indented tick.
export function tickRole(item) {
  return item.isSection ? 'band' : 'cell'
}

// Status lane: exceptions only. Running beats error beats stale (edited since the
// last run, or upstream data changed). Successful, unrun and markdown cells: null.
export function statusLaneKind(item) {
  if (item.cellType === 'markdown') return null
  if (item.isRunning) return 'run'
  if (item.hasError) return 'err'
  if (item.isStale || item.isDataflowStale) return 'stale'
  return null
}
