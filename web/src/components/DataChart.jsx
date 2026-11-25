import { useState, useMemo } from 'react'
import { columnType, parseNumeric, histogramBins, fmtNum } from '../services/dfStats'
import { W, H, BASE_M, chartMargins, labelStep } from '../services/chartGeometry'
import { AGGREGATIONS, buildCategorySeries, buildScatter } from '../services/chartData'
import { useDataTheme } from '../hooks/useDataTheme'

/**
 * DataChart — a dependency-free SVG chart view over a DataGrid's rows.
 *
 * Props:
 *   columns : string[]        column headers
 *   rows    : string[][]      display-string cells (the grid's filtered+sorted view)
 *   dtypes  : string[]        optional pandas dtypes, 1:1 with columns
 *
 * Charts (all rendered as plain SVG, no chart lib):
 *   bar        X=any (category/label), Y=numeric      — one bar per row
 *   line       X=any (ordered index), Y=numeric        — connected points
 *   scatter    X=numeric, Y=numeric                    — points
 *   histogram  single numeric column, binned           — distribution
 *
 * Everything is computed over the passed-in rows (the shown/filtered view), so a
 * capped payload charts "what's shown" — consistent with the grid's sort/search.
 * We cap the number of plotted rows for bar/line/scatter to keep the SVG light.
 */

const CHART_TYPES = [
  { id: 'bar', label: 'Bar' },
  { id: 'line', label: 'Line' },
  { id: 'scatter', label: 'Scatter' },
  { id: 'histogram', label: 'Histogram' },
]

// Plot-area geometry (W, H, BASE_M from services/chartGeometry). Top/bottom margins
// never change, so the plot height is constant; left margin / plot width are per-render.
const PH = H - BASE_M.top - BASE_M.bottom

// Bar / Line group-by-X + aggregate, Scatter even sampling, and the point caps live in
// services/chartData.js (pure, Node-testable). Histogram uses all values (it bins them).

function niceTicks(min, max, count = 5) {
  if (!(max > min)) return [min]
  const span = max - min
  const step0 = span / count
  const mag = Math.pow(10, Math.floor(Math.log10(step0)))
  const norm = step0 / mag
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag
  const start = Math.ceil(min / step) * step
  const ticks = []
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(v)
  return ticks
}

export default function DataChart({ columns = [], rows = [], dtypes = [], svgRef }) {
  // Column types (server dtype first, else sampled), computed once per data set.
  const types = useMemo(
    () => columns.map((_, c) => columnType(rows.map(r => r[c]), dtypes?.[c])),
    [columns, rows, dtypes]
  )
  const numericCols = useMemo(
    () => columns.map((_, c) => c).filter(c => types[c] === 'number'),
    [columns, types]
  )

  const [chartType, setChartType] = useState('bar')
  // Sensible defaults: X = first non-numeric (label) column if any else col 0;
  // Y = first numeric column.
  const defaultX = useMemo(() => {
    const firstNonNum = columns.findIndex((_, c) => types[c] !== 'number')
    return firstNonNum >= 0 ? firstNonNum : 0
  }, [columns, types])
  const defaultY = numericCols.length ? numericCols[0] : 0

  const [xCol, setXCol] = useState(defaultX)
  const [yCol, setYCol] = useState(defaultY)
  // How Bar / Line combine all rows that share an X value (pivot-style; Sum default).
  const [agg, setAgg] = useState('sum')

  // Clamp selections if they fall out of range for the current chart (e.g. switch
  // to scatter needs a numeric X). We don't force-mutate state; we derive validity
  // and show a hint instead, so the user's picks are preserved where possible.
  const xIsNumeric = types[xCol] === 'number'

  const rendered = useMemo(() => {
    if (columns.length === 0 || rows.length === 0) {
      return { error: 'No data to chart.' }
    }

    if (chartType === 'histogram') {
      if (types[yCol] !== 'number') {
        return { error: 'Histogram needs a numeric column. Pick one under “Column”.' }
      }
      const nums = rows.map(r => parseNumeric(r[yCol])).filter(n => !Number.isNaN(n))
      const hb = histogramBins(nums, 20)
      if (!hb) return { error: 'No numeric values to bin.' }
      return { kind: 'histogram', hb, count: nums.length }
    }

    if (chartType === 'scatter') {
      if (!xIsNumeric || types[yCol] !== 'number') {
        return { error: 'Scatter needs numeric X and Y columns.' }
      }
      const s = buildScatter(rows, xCol, yCol)
      return s.error ? s : { kind: 'scatter', ...s }
    }

    // bar / line — GROUP BY X over all shown rows, aggregate Y, order by X.
    const s = buildCategorySeries(chartType, rows, xCol, yCol, types[xCol], types[yCol], agg)
    return s.error ? s : { kind: chartType, ...s }
  }, [chartType, columns.length, rows, types, xCol, yCol, xIsNumeric, agg])

  const aggLabel = (AGGREGATIONS.find(a => a.id === agg) || AGGREGATIONS[0]).label

  return (
    <div className="df-chart">
      <div className="df-chart-controls">
        <div className="df-chart-types" role="tablist" aria-label="Chart type">
          {CHART_TYPES.map(t => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={chartType === t.id}
              className={`df-chart-type${chartType === t.id ? ' active' : ''}`}
              onClick={() => setChartType(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="df-chart-selects">
          {chartType !== 'histogram' && (
            <label className="df-chart-select">
              X
              <select value={xCol} onChange={e => setXCol(Number(e.target.value))}>
                {columns.map((c, i) => (
                  <option key={i} value={i}>{c}{types[i] === 'number' ? '  #' : ''}</option>
                ))}
              </select>
            </label>
          )}
          <label className="df-chart-select">
            {chartType === 'histogram' ? 'Column' : 'Y'}
            <select value={yCol} onChange={e => setYCol(Number(e.target.value))}>
              {columns.map((c, i) => (
                <option key={i} value={i}>{c}{types[i] === 'number' ? '  #' : ''}</option>
              ))}
            </select>
          </label>
          {(chartType === 'bar' || chartType === 'line') && (
            <label className="df-chart-select" title="How rows that share the same X value are combined">
              Agg
              <select value={agg} onChange={e => setAgg(e.target.value)}>
                {AGGREGATIONS.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
              </select>
            </label>
          )}
        </div>
      </div>

      {rendered.error ? (
        <div className="df-chart-empty">{rendered.error}</div>
      ) : (
        <ChartSvg
          rendered={rendered}
          xLabel={chartType === 'histogram' ? columns[yCol] : columns[xCol]}
          yLabel={chartType === 'histogram' ? 'count'
            : (chartType === 'bar' || chartType === 'line')
              ? (agg === 'count' ? 'count of rows' : `${aggLabel.toLowerCase()} of ${columns[yCol]}`)
              : columns[yCol]}
          svgRef={svgRef}
        />
      )}

      {rendered.capped && rendered.note && (
        <div className="df-chart-note">{rendered.note}</div>
      )}
    </div>
  )
}

// ─── SVG renderer ─────────────────────────────────────────────────────────────

function ChartSvg({ rendered, xLabel, yLabel, svgRef }) {
  const { kind } = rendered
  const theme = useDataTheme()

  // Compute the y-domain (and x-domain for scatter) shared by axes + marks.
  let yMin = 0, yMax = 1, xMin = 0, xMax = 1
  if (kind === 'histogram') {
    yMax = Math.max(1, ...rendered.hb.bins.map(b => b.count))
    yMin = 0
  } else if (kind === 'scatter') {
    const xs = rendered.pts.map(p => p.x); const ys = rendered.pts.map(p => p.y)
    xMin = Math.min(...xs); xMax = Math.max(...xs)
    yMin = Math.min(...ys); yMax = Math.max(...ys)
  } else {
    const ys = rendered.pts.map(p => p.y)
    yMin = Math.min(0, ...ys); yMax = Math.max(0, ...ys)
  }
  if (yMin === yMax) { yMax = yMin + 1 }
  if (xMin === xMax) { xMax = xMin + 1 }

  const yScale = (v) => BASE_M.top + PH - ((v - yMin) / (yMax - yMin)) * PH
  const yTicks = niceTicks(yMin, yMax, 5)
  const yTickLabels = yTicks.map(fmtNum)
  // Light and dark widen the left margin to fit their larger tick labels; ember gets BASE_M.
  const m = chartMargins(theme, yTickLabels)
  const pw = W - m.left - m.right

  return (
    <div className="df-chart-svg-wrap">
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="df-chart-svg" role="img" aria-label={`${kind} chart`}>
        {/* Y gridlines + labels */}
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={m.left} y1={yScale(t)} x2={m.left + pw} y2={yScale(t)} className="df-chart-grid" />
            <text x={m.left - 8} y={yScale(t)} className="df-chart-axis-label" textAnchor="end" dominantBaseline="middle">{yTickLabels[i]}</text>
          </g>
        ))}
        {/* Axes */}
        <line x1={m.left} y1={m.top} x2={m.left} y2={m.top + PH} className="df-chart-axis" />
        <line x1={m.left} y1={m.top + PH} x2={m.left + pw} y2={m.top + PH} className="df-chart-axis" />

        {kind === 'bar' && <BarMarks pts={rendered.pts} yScale={yScale} m={m} pw={pw} theme={theme} />}
        {kind === 'line' && <LineMarks pts={rendered.pts} yScale={yScale} m={m} pw={pw} theme={theme} />}
        {kind === 'scatter' && <ScatterMarks pts={rendered.pts} xMin={xMin} xMax={xMax} yScale={yScale} m={m} pw={pw} />}
        {kind === 'histogram' && <HistMarks bins={rendered.hb.bins} yScale={yScale} m={m} pw={pw} theme={theme} />}

        {/* Axis titles */}
        <text x={m.left + pw / 2} y={H - 8} className="df-chart-axis-title" textAnchor="middle">{xLabel}</text>
        <text x={14} y={m.top + PH / 2} className="df-chart-axis-title" textAnchor="middle" transform={`rotate(-90 14 ${m.top + PH / 2})`}>{yLabel}</text>
      </svg>
    </div>
  )
}

function BarMarks({ pts, yScale, m, pw, theme }) {
  const n = pts.length
  const bandW = pw / n
  const barW = Math.max(1, Math.min(bandW * 0.8, 48))
  const y0 = yScale(0)
  // Show at most ~12 x labels to avoid overlap (light/dark also space them by label width).
  const labelEvery = labelStep(theme, n, Math.ceil(n / 12), pts.map(p => truncLabel(p.label)), pw)
  return (
    <g>
      {pts.map((p, i) => {
        const cx = m.left + i * bandW + bandW / 2
        const yTop = yScale(p.y)
        const h = Math.abs(y0 - yTop)
        return (
          <g key={i}>
            <rect x={cx - barW / 2} y={Math.min(y0, yTop)} width={barW} height={Math.max(1, h)} className="df-chart-bar">
              <title>{`${p.label}: ${fmtNum(p.y)}`}</title>
            </rect>
            {i % labelEvery === 0 && (
              <text x={cx} y={m.top + PH + 16} className="df-chart-axis-label" textAnchor="middle">{truncLabel(p.label)}</text>
            )}
          </g>
        )
      })}
    </g>
  )
}

function LineMarks({ pts, yScale, m, pw, theme }) {
  const n = pts.length
  const x = (i) => m.left + (n === 1 ? pw / 2 : (i / (n - 1)) * pw)
  const d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i)} ${yScale(p.y)}`).join(' ')
  const labelEvery = labelStep(theme, n, Math.ceil(n / 12), pts.map(p => truncLabel(p.label)), pw)
  return (
    <g>
      <path d={d} className="df-chart-line" fill="none" />
      {pts.map((p, i) => (
        <circle key={i} cx={x(i)} cy={yScale(p.y)} r={2.5} className="df-chart-dot">
          <title>{`${p.label}: ${fmtNum(p.y)}`}</title>
        </circle>
      ))}
      {pts.map((p, i) => i % labelEvery === 0 && (
        <text key={`l${i}`} x={x(i)} y={m.top + PH + 16} className="df-chart-axis-label" textAnchor="middle">{truncLabel(p.label)}</text>
      ))}
    </g>
  )
}

function ScatterMarks({ pts, xMin, xMax, yScale, m, pw }) {
  const xScale = (v) => m.left + ((v - xMin) / (xMax - xMin)) * pw
  const xTicks = niceTicks(xMin, xMax, 5)
  return (
    <g>
      {xTicks.map((t, i) => (
        <text key={i} x={xScale(t)} y={m.top + PH + 16} className="df-chart-axis-label" textAnchor="middle">{fmtNum(t)}</text>
      ))}
      {pts.map((p, i) => (
        <circle key={i} cx={xScale(p.x)} cy={yScale(p.y)} r={3} className="df-chart-dot">
          <title>{`(${fmtNum(p.x)}, ${fmtNum(p.y)})`}</title>
        </circle>
      ))}
    </g>
  )
}

function HistMarks({ bins, yScale, m, pw, theme }) {
  const n = bins.length
  const bandW = pw / n
  const barW = Math.max(1, bandW * 0.92)
  const y0 = yScale(0)
  const labelEvery = labelStep(theme, n, Math.ceil(n / 8), bins.map(b => fmtNum(b.x0)), pw)
  return (
    <g>
      {bins.map((b, i) => {
        const cx = m.left + i * bandW + bandW / 2
        const yTop = yScale(b.count)
        return (
          <g key={i}>
            <rect x={cx - barW / 2} y={yTop} width={barW} height={Math.max(0, y0 - yTop)} className="df-chart-bar">
              <title>{`[${fmtNum(b.x0)}, ${fmtNum(b.x1)}): ${b.count}`}</title>
            </rect>
            {i % labelEvery === 0 && (
              <text x={cx} y={m.top + PH + 16} className="df-chart-axis-label" textAnchor="middle">{fmtNum(b.x0)}</text>
            )}
          </g>
        )
      })}
    </g>
  )
}

function truncLabel(s) {
  const str = String(s ?? '')
  return str.length <= 10 ? str : str.slice(0, 9) + '…'
}
