import { Fragment, useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback, useId } from 'react'
import { IconRefresh } from '../Icons'
import VmSectionToggle from './VmSectionToggle'
import { PROXY_URL } from '../../config'
import { fetchWithTimeout } from '../../services/fetchWithTimeout'
import { formatDuration } from '../../utils/formatDuration'
import { formatClock, formatClockSec, formatClockShort } from '../../services/vmCardState'
import {
  PLOT_H, groupLabelParts, labelWords, pickLabelTier, groupTooltip, pointText, groupGeometry, nearestPoint, historyHeader,
  currentValues, currentText, endLabelTops, memDanger, bulletRows, historyEmpty, memText,
} from '../../services/vmMetricsChart'
import {
  windowScale, axisTicks, refLines, burstGeometry, burstPointText, burstGroupTooltip, burstCurrent, burstCurrentText,
  burstBulletRows, fmtMult, tierVcpu, fmtTierVcpu,
} from '../../services/vmBurst'

// Past CPU / memory / disk for the Live MicroVMs card's Resources tab.
//
// Data: GET /instances/{id}/metrics-history?groups=10, which reads ONLY the
// proxy's DB (never the VM, so it can't wake a suspended VM or reset a running
// VM's idle timer). Always THIS VM only. Fetched once when the Resources tab is
// shown, and again only on the refresh button. NEVER on a timer.
//
// Layout (vm-run-chart/user-approved-layout.md): ONE chart whose x axis is the
// run groups side by side (idle and suspended time removed). Each group is as
// wide as its duration, with a minimum width so short runs stay readable; thin
// dividers separate groups, a label sits under each one. CPU % (filled area +
// line) and memory % of the tier limit (line) share one 0–100 % scale. When the
// groups don't fit, the row scrolls horizontally and opens at the newest group.
// Below: a one-line legend and three lifetime bullet bars (memory, CPU, disk).
// Burst (vm-history-burst): when memory went above its tier (1×) in the shown groups the
// scale grows to 2× or 4× of the tier (services/vmBurst.js), the gutter shows multiples,
// memory above 1× is amber over a faint band, and reference lines mark 1× / 2× / the 4×
// out-of-memory limit. A VM that stayed within its tier renders exactly as before.
// The chart + legend (or the empty / loading / error state) sit in ONE inset card
// (.vm-history-card, vm-history-inset-card) so the legend reads as the chart's;
// the title row stays above it and the lifetime bars stay flat below it.

/** Below this chart width (the inset card's content box) the current values move into the header (no end-label column). */
const NARROW_PX = 220

/** Zero-length round-capped segments: dots that stay round when the SVG stretches. */
function Dots({ dots, className }) {
  if (!dots.length) return null
  return (
    <path className={className} d={dots.map(d => `M${d.x} ${d.y} l0.01 0`).join(' ')}
      vectorEffect="non-scaling-stroke" strokeLinecap="round" />
  )
}

function RunGroup({ g, geo, parts, tier, index, isFirst, isLast, tip, showPeak, showMemLast, burst, onPoint, onGroup, onHide }) {
  const clipId = `vmh-clip-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const words = labelWords(parts, tier)
  const errored = g.kind === 'run' && g.cells_errored > 0
  // The value label goes left of the dot near the right edge (and in the newest group) so it never
  // overflows the scroller's right end; never in the oldest group, where it would leave the left end.
  const x = geo.cpuPeak?.x ?? 0
  const peakFlip = showPeak && geo.cpuPeak && (isFirst ? (isLast && x > 55) : (x > 55 || (isLast && x > 25)))
  const peakBelow = showPeak && geo.cpuPeak && geo.cpuPeak.y < 14
  return (
    <div
      className="vm-history-group"
      style={{ flexGrow: Math.max(1, g.duration_sec || 1) }}
      tabIndex={0}
      role="img"
      aria-label={tip}
      data-group-id={g.id}
      onFocus={() => onGroup(index)}
      onBlur={onHide}
      onKeyDown={(e) => { if (e.key === 'Escape') onHide() }}
      onPointerLeave={onHide}
    >
      <div className="vm-history-area" onPointerMove={(e) => onPoint(e, index)}>
        <svg className="vm-history-svg" viewBox={`0 0 100 ${PLOT_H}`} preserveAspectRatio="none"
          overflow="visible" aria-hidden="true" focusable="false">
          {burst && (<>
            <defs>
              <clipPath id={clipId} clipPathUnits="userSpaceOnUse">
                <rect x="-5" y="-5" width="110" height={geo.y1x + 5} />
              </clipPath>
            </defs>
            <rect className="vm-history-burst-band" x="0" y="0" width="100" height={geo.y1x} />
          </>)}
          {geo.cpuArea && <path className="vm-history-cpu-area" d={geo.cpuArea} />}
          {geo.cpuLine && <path className="vm-history-cpu-line" d={geo.cpuLine} vectorEffect="non-scaling-stroke" />}
          {geo.memLine && <path className="vm-history-mem-line" d={geo.memLine} vectorEffect="non-scaling-stroke" />}
          {burst && geo.cpuLine && <path className="vm-history-cpu-line is-burst" d={geo.cpuLine} clipPath={`url(#${clipId})`} vectorEffect="non-scaling-stroke" />}
          {burst && geo.memLine && <path className="vm-history-mem-line is-burst" d={geo.memLine} clipPath={`url(#${clipId})`} vectorEffect="non-scaling-stroke" />}
          <Dots dots={geo.cpuDots} className="vm-history-dot vm-history-dot-cpu" />
          <Dots dots={geo.memDots} className="vm-history-dot vm-history-dot-mem" />
        </svg>
        {showPeak && geo.cpuPeak && (
          <span className={`vm-history-peak${peakFlip ? ' is-left' : ''}${peakBelow ? ' is-below' : ''}${burst && peakBelow ? ' is-on-axis' : ''}`}
            style={{ left: `${geo.cpuPeak.x}%`, top: `${(geo.cpuPeak.y / PLOT_H) * 100}%` }} aria-hidden="true">
            <span className="vm-history-peak-dot" />
            <span className="vm-history-peak-label">{burst ? fmtMult(geo.cpuPeak.mult) : `${Math.round(geo.cpuPeak.value)}%`}</span>
          </span>
        )}
        {showMemLast && geo.memLast && (
          <span className="vm-history-memlast" aria-hidden="true"
            style={{ left: `${geo.memLast.x}%`, top: `${(geo.memLast.y / PLOT_H) * 100}%` }} />
        )}
      </div>
      <div className="vm-history-glabel" data-fit={tier} onPointerEnter={() => onGroup(index)}>
        {errored && <><span className="vm-history-errdot" aria-hidden="true" />{' '}</>}
        {words.map((w, k) => (
          <Fragment key={k}>{k > 0 && ' '}<span className="vm-history-gword">{w}</span></Fragment>
        ))}
      </div>
    </div>
  )
}

export default function VmMetricsHistory({
  vmId, active, isUnattached, memoryMib, burstSeconds = null,
  // Collapsible headings (state kept by MicroVMsPanel per VM id + section).
  historyOpen = true, lifetimeOpen = true, onToggleSection = () => {},
}) {
  const [nonce, setNonce] = useState(0)
  const [entry, setEntry] = useState({ data: null, loading: false, error: false })
  const [narrow, setNarrow] = useState(false)
  const [fitTick, setFitTick] = useState(0)
  const [tip, setTip] = useState(null)
  const [tiers, setTiers] = useState([])
  const sectionRef = useRef(null)
  // The inset card (callback ref: it mounts/unmounts with the chart / empty states).
  const [cardEl, setCardEl] = useState(null)
  const canvasRef = useRef(null)
  const plotRef = useRef(null)
  const scrollRef = useRef(null)
  const tipRef = useRef(null)

  // Fetch once per showing of the Resources tab (+ refresh button).
  // No interval, no retry timer. The 0 ms defer only collapses React StrictMode's
  // dev mount → unmount → mount into ONE request; it is not a refresh.
  useEffect(() => {
    if (!active || isUnattached || !vmId) return undefined
    let alive = true
    const ctrl = new AbortController()
    const kick = setTimeout(async () => {
      setEntry(prev => ({ ...prev, loading: true }))
      try {
        const resp = await fetchWithTimeout(
          `${PROXY_URL}/instances/${encodeURIComponent(vmId)}/metrics-history?groups=10`,
          { timeout: 8000, signal: ctrl.signal })
        const data = resp.ok ? await resp.json() : null
        if (!alive) return
        if (data && !data.error) setEntry({ data, loading: false, error: false })
        else setEntry(prev => ({ data: prev.data, loading: false, error: true }))
      } catch {
        if (alive) setEntry(prev => ({ data: prev.data, loading: false, error: true }))
      }
    }, 0)
    return () => { alive = false; clearTimeout(kick); ctrl.abort() }
  }, [vmId, active, isUnattached, nonce])

  const data = entry.data
  const mode = isUnattached ? 'empty' : (data ? (historyEmpty(data) ? 'empty' : 'chart') : null)
  const groups = useMemo(() => (mode === 'chart' ? data.groups.filter(g => (g.points || []).length) : []), [mode, data])
  const limit = data?.memory_limit_mb ?? memoryMib ?? null
  // Burst: memory above its tier in the shown groups -> the multiples scale (1× → stays as before).
  const ms = useMemo(() => windowScale(groups, limit), [groups, limit])
  const burst = ms.burst
  const geos = useMemo(
    () => groups.map(g => (burst ? burstGeometry(g, { scale: ms.scale, limit }) : groupGeometry(g))),
    [groups, burst, ms.scale, limit])
  const summary = data?.summary || null
  const danger = memDanger(summary, limit)
  const curPlain = useMemo(() => currentValues(groups), [groups])
  const cur = useMemo(() => (burst ? burstCurrent(curPlain, limit) : curPlain), [burst, curPlain, limit])
  const tips = useMemo(
    () => groups.map(g => (burst ? burstGroupTooltip(g, formatClock, limit) : groupTooltip(g, formatClock))),
    [groups, burst, limit])
  const refs = useMemo(() => (burst ? refLines(ms.scale, limit) : []), [burst, ms.scale, limit])
  const labelParts = useMemo(() => groups.map(g => groupLabelParts(g, formatClock, formatClockShort)), [groups])
  // The CPU-peak direct label goes in the shown group holding the highest CPU point.
  const peakIndex = useMemo(() => {
    let best = -1
    geos.forEach((geo, i) => {
      if (geo.cpuPeak && (best < 0 || geo.cpuPeak.value > geos[best].cpuPeak.value)) best = i
    })
    return best
  }, [geos])
  const lastIndex = groups.length - 1
  const bars = useMemo(() => burstBulletRows(bulletRows(summary, limit), summary, limit), [summary, limit])

  // Narrow mode follows the width the chart actually gets: the inset card's content
  // box (its padding + border excluded), else the section's (no timer, no fetch).
  useLayoutEffect(() => {
    const el = cardEl || sectionRef.current
    if (!el || typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver((entries) => {
      setNarrow(entries[entries.length - 1].contentRect.width < NARROW_PX)
      setFitTick(t => t + 1)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [cardEl])

  // Every group gets a visible label. The minimum group width is sized (in px, from
  // the label's own font) so the compact time ('7:12') always fits; each label then
  // shows the widest tier that fits its group: '07:12 PM Run All' → '7:12 Run All'
  // → '7:12'. Words never break; the full text stays in the tooltip / aria-label.
  // Runs before the scroll-to-end effect, since it can change the row's width.
  useLayoutEffect(() => {
    const sec = sectionRef.current
    const labs = scrollRef.current?.querySelectorAll('.vm-history-glabel')
    if (!sec || !labs?.length) return
    if (!canvasRef.current) canvasRef.current = document.createElement('canvas')
    const ctx = canvasRef.current.getContext('2d')
    if (!ctx) return
    const cs = getComputedStyle(labs[0])
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`
    const measure = (w) => ctx.measureText(w || '').width
    const padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)
    const shortMax = Math.max(0, ...labelParts.map(p => measure(p.short)))
    // + 1 px divider (border-box) + 1 px slack.
    sec.style.setProperty('--vm-run-min-w', `${Math.ceil(shortMax + padX) + 2}px`)
    const next = Array.from(labs, (el, i) => pickLabelTier(labelParts[i], el.clientWidth - padX - 0.5, measure))
    setTiers(prev => (prev.length === next.length && prev.every((t, i) => t === next[i]) ? prev : next))
  }, [labelParts, narrow, fitTick])

  // Open scrolled to the newest group (right end); also after re-expanding History
  // (the chart remounts; its card's ResizeObserver re-measures narrow / label tiers).
  useLayoutEffect(() => {
    const sc = scrollRef.current
    if (sc) sc.scrollLeft = sc.scrollWidth
  }, [groups, narrow, historyOpen])

  // The CPU-peak value label is shown only while it is fully inside the scroller's viewport
  // (a half-scrolled '92%' would read as '2%').
  const peakVisibility = () => {
    const sc = scrollRef.current
    const lab = sc?.querySelector('.vm-history-peak-label')
    if (!lab) return
    const sr = sc.getBoundingClientRect()
    lab.classList.remove('is-offscreen')
    const lr = lab.getBoundingClientRect()
    if (lr.left < sr.left - 0.5 || lr.right > sr.right + 0.5) lab.classList.add('is-offscreen')
  }
  useLayoutEffect(peakVisibility, [groups, narrow, fitTick])

  // Tooltip: placed over the plot from the target's rect, clamped inside the plot.
  useLayoutEffect(() => {
    const el = tipRef.current
    const plot = plotRef.current
    if (!el || !plot || !tip) return
    const w = el.offsetWidth
    const pw = plot.clientWidth
    el.style.left = `${Math.max(0, Math.min(pw - w, tip.x - w / 2))}px`
  }, [tip])

  const hide = useCallback(() => setTip(null), [])
  const anchorX = (rectLeft, frac, width) => {
    const plot = plotRef.current
    return plot ? rectLeft + frac * width - plot.getBoundingClientRect().left : 0
  }
  const showGroup = useCallback((i) => {
    const plot = plotRef.current
    const node = scrollRef.current?.querySelectorAll('.vm-history-group')[i]
    if (!plot || !node) return
    const r = node.getBoundingClientRect()
    const pr = plot.getBoundingClientRect()
    setTip({ x: r.left + r.width / 2 - pr.left, point: null, group: tips[i], gi: i, focus: true })
  }, [tips])
  // Focusing a group can scroll the row: keep a focus tooltip on its group, drop a hover one.
  const onScroll = () => {
    peakVisibility()
    if (tip?.focus) showGroup(tip.gi)
    else setTip(null)
  }
  const showPoint = (e, i) => {
    const r = e.currentTarget.getBoundingClientRect()
    if (!r.width) return
    const geo = geos[i]
    const idx = nearestPoint(geo.xs, ((e.clientX - r.left) / r.width) * 100)
    if (idx < 0) return
    const p = groups[i].points[idx]
    setTip({ x: anchorX(r.left, geo.xs[idx] / 100, r.width), point: burst ? burstPointText(p, limit, formatClockSec) : pointText(p, formatClockSec), group: tips[i] })
  }

  const titleId = `vm-history-title-${vmId}`
  const header = mode === 'chart' ? historyHeader({ ...data, groups }) : null
  const curText = narrow ? (burst ? burstCurrentText(cur) : currentText(cur)) : null
  const staleNote = entry.error && data
    ? <div className="vm-cost-hint" role="status">Couldn't refresh; showing the last result.</div>
    : null
  const endY = (frac) => PLOT_H - Math.min(1, Math.max(0, frac)) * PLOT_H
  const ends = !narrow && cur ? (burst ? endLabelTops(
    Number.isFinite(cur.mem) ? endY(cur.mem / ms.scale) : null,
    Number.isFinite(cur.cpu) ? endY(cur.cpu / ms.scale) : null,
  ) : endLabelTops(
    Number.isFinite(cur.memPct) ? PLOT_H - (Math.min(100, Math.max(0, cur.memPct)) / 100) * PLOT_H : null,
    Number.isFinite(cur.cpu) ? PLOT_H - (Math.min(100, Math.max(0, cur.cpu)) / 100) * PLOT_H : null,
  )) : null
  const tierV = burst ? tierVcpu(limit) : null

  return (
    <div className={`vm-res-section vm-history${narrow ? ' is-narrow' : ''}`} role="group" aria-labelledby={titleId}
      ref={sectionRef}>
      <div className="vm-history-head">
        <VmSectionToggle id={titleId} label="History" expanded={historyOpen}
          controls={`vm-history-body-${vmId}`} onToggle={() => onToggleSection('history')} />
        {header && (
          <div className="vm-history-meta">
            {header}{curText && <> · <span className="vm-history-cur">{curText.replace(/(CPU|Mem) /g, '$1\u00a0')}</span></>}
          </div>
        )}
        {!isUnattached && (
          <button type="button" className="vm-history-refresh" aria-label="Refresh history" title="Refresh history"
            onClick={() => setNonce(n => n + 1)} disabled={entry.loading}>
            <IconRefresh width={11} height={11} aria-hidden="true" />
          </button>
        )}
      </div>
      {/* Collapsed: the chart is unmounted (not hidden) so re-expanding re-measures it. */}
      <div className="vm-section-body" id={`vm-history-body-${vmId}`} hidden={!historyOpen}>
      {historyOpen && mode === 'empty' && (
        <div className="vm-history-card" ref={setCardEl}>
          <div className="vm-history-empty">
            <div>No metrics recorded for this VM yet.</div>
            <div className="vm-history-note">Samples are recorded while cells run.</div>
          </div>
        </div>
      )}
      {historyOpen && !mode && (entry.loading || entry.error) && (
        <div className="vm-history-card" ref={setCardEl}>
          {entry.loading && <div className="vm-history-empty" aria-live="polite">Loading history…</div>}
          {entry.error && (
            <div className="vm-history-empty">
              Couldn't load history{' '}
              <button type="button" className="vm-link-btn" onClick={() => setNonce(n => n + 1)}>Retry</button>
            </div>
          )}
        </div>
      )}
      {historyOpen && mode === 'chart' && (<>
        {staleNote}
        <div className="vm-history-card" ref={setCardEl}>
          <div className="vm-history-chart">
            {burst ? (
              <div className="vm-history-gutter is-multiples" aria-hidden="true">
                {axisTicks(ms.scale).map(t => (
                  <span key={t.label} className="vm-history-ylabel" style={{ '--f': 1 - t.mult / ms.scale }}>{t.label}</span>
                ))}
              </div>
            ) : (
              <div className="vm-history-gutter" aria-hidden="true">
                <span className="vm-history-ylabel">100%</span>
                <span className="vm-history-ylabel">0</span>
              </div>
            )}
            <div className={`vm-history-plot${burst ? ' is-burst' : ''}`} ref={plotRef}>
              {burst ? (<>
                {refs.map(l => (
                  <div key={l.key} className={`vm-history-hline vm-history-ref vm-history-ref-${l.kind}`}
                    style={{ '--f': 1 - l.mult / ms.scale }} aria-hidden="true" />
                ))}
                <div className="vm-history-hline vm-history-base" aria-hidden="true" />
                {refs.map(l => (
                  <span key={l.key} className={`vm-history-reflabel vm-history-reflabel-${l.kind}${l.mult >= ms.scale ? ' is-below' : ''}`}
                    style={{ '--f': 1 - l.mult / ms.scale }} title={l.desc}>
                    <span className="is-long">{l.label}</span><span className="is-short">{l.short}</span>
                  </span>
                ))}
                <span className="vm-history-sr">
                  {`Scale: multiples of the ${memText(limit)} tier. `}{refs.map(l => l.desc).join(' ')}{ms.oom ? ' Memory went above the out-of-memory limit.' : ''}
                </span>
              </>) : (<>
                <div className={`vm-history-hline vm-history-limit${danger ? ' is-danger' : ''}`} aria-hidden="true" />
                <div className="vm-history-hline vm-history-grid" aria-hidden="true" />
                <div className="vm-history-hline vm-history-base" aria-hidden="true" />
              </>)}
              <div className="vm-history-scroll" ref={scrollRef} onScroll={onScroll}>
                <div className="vm-history-groups">
                  {groups.map((g, i) => (
                    <RunGroup key={g.id} g={g} geo={geos[i]} parts={labelParts[i]} tier={tiers[i] || 'full'} index={i} isFirst={i === 0} isLast={i === lastIndex} tip={tips[i]}
                      showPeak={i === peakIndex} showMemLast={i === lastIndex} burst={burst}
                      onPoint={showPoint} onGroup={showGroup} onHide={hide} />
                  ))}
                </div>
              </div>
              {tip && (
                <div className="vm-history-tooltip" ref={tipRef} aria-hidden="true">
                  {tip.point && <div className="vm-history-tooltip-point">{tip.point}</div>}
                  <div className="vm-history-tooltip-group">{tip.group}</div>
                </div>
              )}
            </div>
            {ends && (
              <div className="vm-history-ends">
                {ends.mem !== null && (
                  <span className="vm-history-end vm-history-end-mem" style={{ marginTop: `${ends.mem}px` }}>
                    {burst
                      ? <>
                          <span className="vm-history-end-value">Mem {fmtMult(cur.mem)}</span>
                          {Number.isFinite(cur.memMb) && <span className="vm-history-end-sub">{memText(cur.memMb)}</span>}
                        </>
                      : <>
                          <span className="vm-history-end-value">Mem {Math.round(cur.memPct)}%</span>
                          {Number.isFinite(cur.memMb) && <span className="vm-history-end-sub">{memText(cur.memMb)}</span>}
                        </>}
                  </span>
                )}
                {ends.cpu !== null && (
                  <span className="vm-history-end vm-history-end-cpu" style={{ marginTop: `${ends.cpu}px` }}>
                    <span className="vm-history-end-value">CPU {burst ? fmtMult(cur.cpu) : `${Math.round(cur.cpu)}%`}</span>
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="vm-history-legend">
            <span><span className="vm-history-key vm-history-key-cpu" aria-hidden="true" />
              {burst && tierV ? `CPU (× of ${fmtTierVcpu(tierV)} vCPU)` : 'CPU'}</span>
            <span><span className="vm-history-key vm-history-key-mem" aria-hidden="true" />
              {burst ? `Memory (× of ${memText(limit)} tier)`
                : (Number.isFinite(limit) && limit > 0 ? `Memory (% of ${memText(limit)})` : 'Memory %')}</span>
            {burst && <span><span className="vm-history-key vm-history-key-burst" aria-hidden="true" />burst (billed)</span>}
            <span><span className="vm-history-key vm-history-key-div" aria-hidden="true" />run boundary · idle/suspended time removed</span>
          </div>
        </div>
        {bars.length > 0 && (
          <div className={`vm-history-bars${bars.some(b => b.scale4) ? ' is-stacked' : ''}`} role="group" aria-labelledby={`vm-lifetime-title-${vmId}`} id={`vm-lifetime-body-${vmId}`}>
            <VmSectionToggle id={`vm-lifetime-title-${vmId}`} label="VM lifetime" expanded={lifetimeOpen}
              className="vm-history-bars-caption" controls={`vm-lifetime-body-${vmId}`}
              onToggle={() => onToggleSection('lifetime')} />
            {lifetimeOpen && bars.map(b => (
              <div key={b.key} className={`vm-history-bar-row vm-history-bar-${b.key}`}>
                <span className="vm-history-bar-label">{b.label}</span>
                <span className="vm-history-bar" aria-hidden="true">
                  {Number.isFinite(b.frac) && b.scale4 && !b.danger && (<>
                    <span className="vm-history-bar-fill"
                      style={{ width: `${Math.round(Math.min(b.frac, b.baseFrac) * 1000) / 10}%` }} />
                    {b.frac > b.baseFrac && (
                      <span className="vm-history-bar-fill-burst"
                        style={{ left: `${b.baseFrac * 100}%`, width: `${Math.round((b.frac - b.baseFrac) * 1000) / 10}%` }} />
                    )}
                  </>)}
                  {Number.isFinite(b.frac) && !(b.scale4 && !b.danger) && (
                    <span className={`vm-history-bar-fill${b.danger ? ' is-danger' : ''}`}
                      style={{ width: `${Math.round(b.frac * 1000) / 10}%` }} />
                  )}
                  {b.marks && b.marks.map(m => (
                    <span key={m} className="vm-history-bar-mark" style={{ left: `${m * 100}%` }} />
                  ))}
                  {b.limit && <span className="vm-history-bar-tick" />}
                </span>
                <span className="vm-history-bar-value">{b.value}</span>
              </div>
            ))}
            {lifetimeOpen && burstSeconds > 0 && (
              <div className="vm-history-burst-note">Burst: {formatDuration(burstSeconds * 1000)} billed above baseline</div>
            )}
          </div>
        )}
      </>)}
      </div>
    </div>
  )
}
