// Unit tests for services/outlineMap.js (geometry of the Outline cell map).
// Run with: node --test web/src/services/__tests__/outlineMap.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  fitHeights, layoutMap, uniformRects, contentToMap, mapToContent, viewportWindow,
  scrollTopForMapClick, scrollTopForWindowTop, tickRole, statusLaneKind,
  MAP_MIN_SEGMENT_PX, MAP_MIN_TICK_PX, MAP_MIN_WINDOW_PX,
} from '../outlineMap.js'

const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`)
const sum = (xs) => xs.reduce((a, b) => a + b, 0)

// Notebook: 16px top padding, cells separated by 12px margins, 100px bottom padding.
function rectsFrom(heights, pad = 16, gap = 12) {
  let top = pad
  return heights.map((h) => { const r = { top, height: h }; top += h + gap; return r })
}
const contentOf = (rects, bottomPad = 100) => rects.at(-1).top + rects.at(-1).height + bottomPad

test('fitHeights: proportional when no floor applies, and sums to the total', () => {
  const out = fitHeights([100, 300, 600], 100, 4)
  near(out[0], 10); near(out[1], 30); near(out[2], 60)
  near(sum(out), 100)
})

test('fitHeights: tiny lengths are pinned to the floor and the rest stays proportional', () => {
  const out = fitHeights([1, 1, 500, 1000], 150, 4)
  assert.equal(out[0], 4); assert.equal(out[1], 4)
  near(sum(out), 150)
  near(out[3] / out[2], 2)   // the unpinned ones keep their ratio
})

test('fitHeights: equal shares when the floors alone do not fit; empty and zero cases', () => {
  assert.deepEqual(fitHeights([10, 2000, 5], 6, 4), [2, 2, 2])
  assert.deepEqual(fitHeights([], 100), [])
  assert.deepEqual(fitHeights([10, 20], 0), [0, 0])
  const zeros = fitHeights([0, 0, 0, 0], 40, 4)
  near(sum(zeros), 40); zeros.forEach((h) => near(h, 10))
})

test('layoutMap: tick heights follow cell heights (a chart cell is taller than a one-liner)', () => {
  const rects = rectsFrom([40, 400, 40, 600, 60])
  const layout = layoutMap({ rects, contentHeight: contentOf(rects), mapHeight: 500 })
  const t = layout.segments.map((s) => s.tickH)
  assert.ok(t[1] > t[0] * 5, `chart tick ${t[1]} vs one-liner ${t[0]}`)
  near(t[3] / t[1], 600 / 400, 0.01)
  near(t[0], t[2], 1e-9)
})

test('layoutMap: segments tile the whole strip in order with no gaps or overlap', () => {
  const rects = rectsFrom([30, 200, 30, 30, 500, 80, 30])
  const layout = layoutMap({ rects, contentHeight: contentOf(rects), mapHeight: 420 })
  let y = 0
  for (const s of layout.segments) {
    near(s.y, y)
    assert.ok(s.tickY >= s.y - 1e-9 && s.tickY + s.tickH <= s.y + s.h + 1e-9, 'tick inside its segment')
    y += s.h
  }
  near(y, 420)
  near(layout.segments[0].start, 0)
  near(layout.segments.at(-1).end, layout.contentHeight)
})

test('layoutMap: a minimum keeps tiny cells visible in a long notebook', () => {
  const heights = Array.from({ length: 45 }, (_, i) => (i === 10 ? 4000 : 20))
  const rects = rectsFrom(heights)
  const layout = layoutMap({ rects, contentHeight: contentOf(rects), mapHeight: 300 })
  for (const s of layout.segments) {
    assert.ok(s.h >= MAP_MIN_SEGMENT_PX - 1e-9, `segment ${s.h}`)
    assert.ok(s.tickH >= MAP_MIN_TICK_PX - 1e-9, `tick ${s.tickH}`)
  }
  near(sum(layout.segments.map((s) => s.h)), 300)
})

test('contentToMap and mapToContent are inverses and monotonic', () => {
  const rects = rectsFrom([30, 400, 30, 30, 700, 50])
  const layout = layoutMap({ rects, contentHeight: contentOf(rects), mapHeight: 350 })
  let prev = -1
  for (let y = 0; y <= layout.contentHeight; y += 37) {
    const m = contentToMap(layout, y)
    assert.ok(m >= prev, 'monotonic')
    prev = m
    near(mapToContent(layout, m), y, 1e-6)
  }
  near(contentToMap(layout, 0), 0)
  near(contentToMap(layout, layout.contentHeight), 350)
  near(contentToMap(layout, -50), 0)
  near(mapToContent(layout, 9999), layout.contentHeight)
})

test('contentToMap: the top of each cell maps inside its tick segment', () => {
  const rects = rectsFrom([30, 400, 30, 600])
  const layout = layoutMap({ rects, contentHeight: contentOf(rects), mapHeight: 300 })
  rects.forEach((r, i) => {
    const s = layout.segments[i]
    const m = contentToMap(layout, r.top)
    assert.ok(m >= s.y - 1e-9 && m <= s.y + s.h + 1e-9)
  })
})

test('viewportWindow: tracks scroll, covers the full strip when everything fits, has a floor', () => {
  const rects = rectsFrom(Array.from({ length: 40 }, () => 120))
  const content = contentOf(rects)
  const layout = layoutMap({ rects, contentHeight: content, mapHeight: 400 })
  const top = viewportWindow(layout, 0, 800)
  near(top.top, 0)
  const mid = viewportWindow(layout, 2000, 800)
  assert.ok(mid.top > top.top)
  near(mid.top, contentToMap(layout, 2000))
  near(mid.height, contentToMap(layout, 2800) - contentToMap(layout, 2000))
  const end = viewportWindow(layout, content - 800, 800)
  near(end.top + end.height, 400, 1e-6)
  // Short notebook: the whole thing is visible, so the window fills the strip.
  const sr = rectsFrom([60, 300, 60])
  const sl = layoutMap({ rects: sr, contentHeight: contentOf(sr), mapHeight: 400 })
  const all = viewportWindow(sl, 0, contentOf(sr))
  near(all.top, 0); near(all.height, 400)
  // A huge notebook still gets a grabbable window.
  const big = rectsFrom(Array.from({ length: 40 }, () => 20000))
  const bl = layoutMap({ rects: big, contentHeight: contentOf(big), mapHeight: 400 })
  assert.ok(viewportWindow(bl, 5000, 800).height >= MAP_MIN_WINDOW_PX)
})

test('scrollTopForMapClick centres the viewport on the clicked point and clamps at the ends', () => {
  const rects = rectsFrom(Array.from({ length: 30 }, () => 200))
  const content = contentOf(rects)
  const layout = layoutMap({ rects, contentHeight: content, mapHeight: 300 })
  const y = contentToMap(layout, 3000)
  near(scrollTopForMapClick(layout, y, 800, content), 3000 - 400, 1e-6)
  assert.equal(scrollTopForMapClick(layout, 0, 800, content), 0)
  near(scrollTopForMapClick(layout, 300, 800, content), content - 800)
})

test('scrollTopForWindowTop: the window top follows the pointer, clamped to the scroll range', () => {
  const rects = rectsFrom(Array.from({ length: 30 }, () => 200))
  const content = contentOf(rects)
  const layout = layoutMap({ rects, contentHeight: content, mapHeight: 300 })
  const y = contentToMap(layout, 1500)
  near(scrollTopForWindowTop(layout, y, 800, content), 1500, 1e-6)
  assert.equal(scrollTopForWindowTop(layout, -20, 800, content), 0)
  near(scrollTopForWindowTop(layout, 300, 800, content), content - 800)
  // Round trip with the window: dragging to where the window already is does not move it.
  const w = viewportWindow(layout, 2400, 800)
  near(scrollTopForWindowTop(layout, w.top, 800, content), 2400, 1e-6)
})

test('uniformRects + empty layout are safe', () => {
  const r = uniformRects(3)
  assert.equal(r.length, 3)
  assert.ok(r[1].top > r[0].top)
  const empty = layoutMap({ rects: [], contentHeight: 0, mapHeight: 200 })
  assert.deepEqual(empty.segments, [])
  assert.equal(contentToMap(empty, 100), 0)
  assert.equal(mapToContent(empty, 100), 0)
})

test('tickRole: headings are bands, everything else is a cell tick', () => {
  assert.equal(tickRole({ isSection: true, cellType: 'markdown' }), 'band')
  assert.equal(tickRole({ isSection: false, cellType: 'markdown' }), 'cell')
  assert.equal(tickRole({ isSection: false, cellType: 'code' }), 'cell')
  assert.equal(tickRole({ isSection: false, cellType: 'sql' }), 'cell')
})

test('statusLaneKind: only exceptions mark the lane', () => {
  const base = { cellType: 'code', isRunning: false, hasError: false, isStale: false, isDataflowStale: false, hasOutput: false }
  assert.equal(statusLaneKind(base), null)                                   // unrun
  assert.equal(statusLaneKind({ ...base, hasOutput: true }), null)           // success
  assert.equal(statusLaneKind({ ...base, hasError: true }), 'err')
  assert.equal(statusLaneKind({ ...base, isStale: true, hasOutput: true }), 'stale')
  assert.equal(statusLaneKind({ ...base, isDataflowStale: true }), 'stale')
  assert.equal(statusLaneKind({ ...base, isRunning: true, hasError: true }), 'run')
  assert.equal(statusLaneKind({ ...base, hasError: true, isStale: true }), 'err')
  assert.equal(statusLaneKind({ ...base, cellType: 'markdown', isStale: true }), null)
  assert.equal(statusLaneKind({ ...base, cellType: 'sql', isStale: true }), 'stale')
})
