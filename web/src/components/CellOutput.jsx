import { useRef, useEffect, useState, useMemo, useId } from 'react'
import DataGrid from './DataGrid'
import { cellOutputItems, isMultiOutput } from '../services/cellOutputs'
import { getExpanded, keyFor, setExpanded } from '../services/outputExpansion'
import Markdown from './Markdown'
import { formatDuration } from '../utils/formatDuration'
import { buildPlotlySrcDoc } from '../services/plotlyTheme'
import { useDataTheme } from '../hooks/useDataTheme'
import PersistedCellImage from './PersistedCellImage'

// Text/markdown output in a height-capped, scrollable block. Long output (many
// lines OR many chars) is collapsed to a bounded, scrollable viewport with a
// "Show all / Collapse" toggle + a size note, so a big print()/dict dump can't
// push the whole notebook down. Copy/Export live ONCE in the OUTPUT header
// (Cell.jsx) and cover the entire output — no per-block toolbar here (that was a
// confusing duplicate of the header controls).
function TextOutput({ text, isMarkdown }) {
  const [expanded, setExpanded] = useState(false)
  const lineCount = (text.match(/\n/g)?.length ?? 0) + 1
  // "Long" = enough to be worth capping. The CSS caps height; this only drives the
  // toggle + note so the user knows there's more and can expand to full height.
  const isLong = lineCount > 24 || text.length > 2000

  const body = isMarkdown ? (
    <Markdown className="output-text output-markdown" content={text} />
  ) : (
    <pre className="output-text">{text}</pre>
  )

  return (
    <div className={`output-text-wrap${isLong && !expanded ? ' output-text-capped' : ''}`}>
      {body}
      {isLong && (
        <button
          className="output-text-toggle"
          onClick={() => setExpanded(e => !e)}
          title={expanded ? 'Collapse output' : 'Show the full output'}
        >
          {expanded
            ? '▲ Collapse'
            : `▼ Show all (${lineCount.toLocaleString()} lines)`}
        </button>
      )}
    </div>
  )
}

// Heuristic: does this text output look like MARKDOWN worth rendering as rich HTML?
// A lot of cell text output is plain (dtype dumps, numbers, repr) and must stay
// verbatim in a <pre>. But print()-ed LLM output (e.g. ask_bedrock_kb) is markdown
// and reads terribly as raw asterisks/hashes. We only switch to rich rendering when
// the text shows clear markdown structure, so plain output is never mangled.
export function looksLikeMarkdown(text) {
  if (!text || text.length < 12) return false
  const signals = [
    /(^|\n)#{1,6}\s+\S/,            // ATX heading
    /\*\*[^\s*][^*]*\*\*/,          // **bold**
    /(^|\n)\s*[-*+]\s+\S/,          // bullet list
    /(^|\n)\s*\d+\.\s+\S/,          // numbered list
    /(^|\n)\s*>\s+\S/,              // blockquote
    /\[[^\]]+\]\([^)]+\)/,          // [link](url)
    /(^|\n)\|.+\|.*\n\s*\|[\s:|-]+\|/, // pipe table (header + separator row)
    /(^|\n)```/,                    // fenced code block
  ]
  let hits = 0
  for (const re of signals) if (re.test(text)) hits++
  // Require at least two distinct markdown signals so a stray "*" or "#1" in plain
  // output doesn't trigger rich rendering.
  return hits >= 2
}

// Injected into the Plotly iframe so it can report its real rendered height back to
// the parent. The iframe is sandboxed (allow-scripts, no allow-same-origin), so the
// parent can't read its DOM — but postMessage still works across the opaque origin.
// We measure the actual .plotly-graph-div height (falling back to body scrollHeight),
// re-measure a few times as Plotly renders async, and observe later relayouts.
const PLOTLY_HEIGHT_REPORTER =
  '\n<script>(function(){' +
  'function report(){try{' +
  "var d=document.querySelector('.plotly-graph-div');" +
  'var ph=d?Math.ceil(d.getBoundingClientRect().height):0;' +
  'var bh=document.body?document.body.scrollHeight:0;' +
  'var v=Math.max(ph,bh)||0;' +
  "if(v>0){parent.postMessage({__plotlyHeight:v},'*');}" +
  '}catch(e){}}' +
  "window.addEventListener('load',function(){report();" +
  '[120,350,700,1200].forEach(function(t){setTimeout(report,t);});' +
  "try{var d=document.querySelector('.plotly-graph-div');" +
  'if(d&&window.ResizeObserver){new ResizeObserver(report).observe(d);}}catch(e){}' +
  '});})();<\/script>'

function PlotlyFrame({ html }) {
  const iframeRef = useRef(null)
  const [height, setHeight] = useState(600)

  // Light and dark: also inject the re-theme script (ember srcDoc is unchanged).
  const theme = useDataTheme()
  const srcDoc = buildPlotlySrcDoc(html, theme, PLOTLY_HEIGHT_REPORTER)

  useEffect(() => {
    const onMessage = (e) => {
      // Only react to messages from THIS iframe's window.
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      const h = e.data && e.data.__plotlyHeight
      if (typeof h === 'number' && h > 0) {
        setHeight(Math.min(2400, Math.max(120, Math.round(h) + 8)))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  return (
    <div className="output-plotly">
      <iframe
        ref={iframeRef}
        srcDoc={srcDoc}
        sandbox="allow-scripts"
        style={{ width: '100%', height: `${height}px`, border: 'none', borderRadius: '8px' }}
        title="Plotly Chart"
      />
    </div>
  )
}

// One collapsible table / chart / image of a multi-output cell. A real <button>
// (aria-expanded) with a one-line caption; the body is NOT mounted until opened, so
// a collapsed Plotly iframe or DataGrid costs nothing. The open/closed choice is
// kept for the page session (services/outputExpansion.js). The first rich output
// of the cell starts open, the later ones collapsed. The parent keys this component
// by item position + content hash, so a re-run with new content resets it.
function CollapsibleOutput({ cellId, item, children }) {
  const storeKey = keyFor(cellId, item.index, item.sig)
  const [open, setOpen] = useState(() => getExpanded(storeKey, item.defaultExpanded))
  const regionId = useId()
  const toggle = () => {
    setExpanded(storeKey, !open)
    setOpen(!open)
  }
  return (
    <div className={`output-item${open ? ' output-item-open' : ''}`}>
      <button
        type="button"
        className="output-item-toggle"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={toggle}
        title={open ? 'Collapse this output' : 'Show this output'}
      >
        <span className="output-item-chevron" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="output-item-caption">{item.caption}</span>
        <span className="output-item-action" aria-hidden="true">{open ? 'Hide' : 'Show'}</span>
      </button>
      <div id={regionId} className="output-item-body">
        {open && children}
      </div>
    </div>
  )
}

// The body of one rich item (table grid, Plotly frame or image).
function RichBody({ cellId, item, multi }) {
  if (item.t === 'table') return <DataGrid payload={item.payload} />
  if (item.t === 'plotly') return <PlotlyFrame html={item.html} />
  if (item.src) {
    return (
      <div className="output-image">
        <img src={item.src} alt={multi ? item.caption : 'Plot output'} />
      </div>
    )
  }
  // After a reload only the image's S3 key survives: fetch it (authenticated).
  return (
    <PersistedCellImage
      cellId={cellId}
      imageKey={item.key}
      anyKey={!item.legacy}
      alt={multi ? item.caption : 'Plot output'}
    />
  )
}

/**
 * CellOutput — renders a cell's execution output (images, text, Plotly charts, HTML
 * tables, error, and timing). A cell can hold SEVERAL tables / charts / images with
 * printed text between them (services/cellOutputs.js); a cell with one of each keeps
 * the original image, text, table/chart layout. Presentational and read-only; extracted from Cell.jsx
 * so both the notebook editor and (later) App mode can share the same renderers.
 *
 * Pass onFix (+ aiBusy/aiFixing) to show the inline "Fix with AI" button on errors;
 * omit onFix for a purely read-only render (e.g. App mode).
 */
export default function CellOutput({ cell, dagNode = null, isConnected = false, onFix = null, onCopyError = null, aiBusy = false, aiFixing = false }) {
  // Transient "Copied!" feedback for the copy-error button.
  const [errCopied, setErrCopied] = useState(false)

  // The ordered items to draw, built ONCE per distinct output (not on every render:
  // CellOutput re-renders whenever its parent Cell does). Envelope cells give their
  // stored list; every other cell gives its legacy fields in the original order
  // (image, text, table/chart). See services/cellOutputs.js.
  const multi = isMultiOutput(cell)
  const items = useMemo(
    () => cellOutputItems(cell),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cell.html, cell.output, cell.image, cell.imageS3Key, cell.id],
  )
  const copyError = async () => {
    if (!onCopyError) return
    const ok = await onCopyError(cell)
    if (ok) {
      setErrCopied(true)
      setTimeout(() => setErrCopied(false), 1500)
    }
  }
  // Compact, high-signal run metrics. Time is always shown; peak memory, peak CPU,
  // and vars-read are appended when present (guest-sampled; null on older VMs or
  // trivial cells). Explains WHY a cell was heavy — a fast cell can still allocate a
  // large DataFrame.
  let runMeta = null
  if (cell.executionTime != null) {
    const parts = [`Executed in ${formatDuration(cell.executionTime)}`]
    if (typeof cell.peakMemMb === 'number' && cell.peakMemMb > 0) {
      parts.push(`peak ${cell.peakMemMb >= 1024 ? (cell.peakMemMb / 1024).toFixed(1) + ' GB' : Math.round(cell.peakMemMb) + ' MB'}`)
    }
    if (typeof cell.peakCpuPct === 'number' && cell.peakCpuPct > 0) {
      parts.push(`${Math.round(cell.peakCpuPct)}% CPU`)
    }
    // Reads come from the cell's stored DAG node (proxy-captured run facts).
    const nRead = dagNode && Array.isArray(dagNode.reads) ? dagNode.reads.length : 0
    if (nRead > 0) parts.push(`read ${nRead} var${nRead === 1 ? '' : 's'}`)
    runMeta = parts.join('  ·  ')
  }
  // Error actions live on the run footer line (see below), only for error outputs.
  const showCopy = !!(cell.error && onCopyError)
  const showFix = !!(cell.error && onFix && isConnected)
  const hasErrorActions = showCopy || showFix
  return (
    <>
      {items.map((item) => {
        if (item.t === 'text') {
          return <TextOutput key={`t${item.index}`} text={item.text} isMarkdown={looksLikeMarkdown(item.text)} />
        }
        if (!multi) {
          // Single-output cell: drawn directly, exactly as before (no header).
          return <RichBody key={`r${item.index}`} cellId={cell.id} item={item} multi={false} />
        }
        return (
          <CollapsibleOutput key={`r${item.index}:${item.sig}`} cellId={cell.id} item={item}>
            <RichBody cellId={cell.id} item={item} multi />
          </CollapsibleOutput>
        )
      })}
      {cell.error && (
        <div className="output-error-wrap">
          <pre className="output-error">{cell.error}</pre>
          {/* Full multi-frame traceback (library-internal frames pruned server-side),
              collapsed by default so the one-line error stays the headline. Only shown
              when the backend supplied one (runtime errors; not syntax/timeout). */}
          {cell.errorTraceback && (
            <details className="output-traceback">
              <summary className="output-traceback-summary">Show traceback</summary>
              <pre className="output-traceback-body">{cell.errorTraceback}</pre>
            </details>
          )}
        </div>
      )}
      {/* Run footer. For an ERROR output the Copy / Fix actions sit on this line,
          right-aligned after the run stats (flex-wrap drops them onto their own line
          at narrow widths) — never overlaid on the error text. Rendered even without
          run stats so the actions never disappear. Successful outputs keep the plain
          stats line. */}
      {hasErrorActions ? (
        <div className="output-footer">
          {runMeta && <div className="output-meta">{runMeta}</div>}
          <div className="output-error-actions" role="group" aria-label="Error actions">
            {/* Copy the error WITH context (code + traceback + locals) for pasting
                into a ticket/chat/search. No VM needed — pure clipboard. */}
            {showCopy && (
              <button type="button" className="output-error-btn" onClick={copyError} title="Copy error + code + traceback to clipboard">
                {errCopied ? '✓ Copied' : '📋 Copy'}
              </button>
            )}
            {showFix && (
              <button type="button" className="output-error-fix-btn" onClick={onFix} disabled={aiBusy} title="Fix this error with AI">
                {aiFixing ? 'Fixing...' : '⚡ Fix'}
              </button>
            )}
          </div>
        </div>
      ) : runMeta && (
        <div className="output-meta">{runMeta}</div>
      )}
    </>
  )
}
