/**
 * IntelReportModal — the full Workbook Intelligence report in a modal, with
 * Markdown + HTML export. Extracted verbatim from the old IntelPanel so it can be
 * reused now that Intel lives inside the AI Assistant panel.
 *
 * Props:
 *   intel        — the intel object ({ suggested_analyses, visualizations, investigations, alerts, full_report })
 *   generatedAt  — ISO timestamp string
 *   notebookName — display name of the notebook the report is for (optional;
 *                  omitted from the header/exports when empty)
 *   onClose      — close handler
 */

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { marked } from 'marked'
import { renderMarkdown, spliceCachedMermaid, ensureMermaidRendered, onMermaidThemeChange } from '../services/markdown'
import { sanitizeMarkdown } from '../services/sanitize'
import { IconX } from './Icons'

// Severity → icon. The report used to render the severity as a bracketed text
// label ([HIGH]/[MEDIUM]/[LOW]) both in the modal-built Alerts list and inside the
// model-authored full_report prose. A small colored icon reads faster and is less
// noisy than the ALL-CAPS token. title= keeps the word available on hover for
// accessibility / disambiguation.
const SEVERITY_ICON = {
  high: '🔴',
  medium: '🟠',
  low: '🟡',
}
function severityIconHtml(sev) {
  const s = String(sev || '').toLowerCase()
  const icon = SEVERITY_ICON[s] || '⚠️'
  const label = s ? s.charAt(0).toUpperCase() + s.slice(1) : 'Alert'
  return `<span class="intel-sev-icon" title="${label} severity" aria-label="${label} severity">${icon}</span>`
}
// Replace any [HIGH]/[MEDIUM]/[LOW] tokens (as the model tends to emit them in the
// full_report prose) with the matching severity icon. Case-insensitive; leaves all
// other bracketed text untouched.
function replaceSeverityTokens(html) {
  return String(html || '').replace(/\[(HIGH|MEDIUM|LOW)\]/gi, (_, sev) => severityIconHtml(sev))
}

// Escape text interpolated into the exported standalone HTML (title/heading).
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}

// Filesystem-safe base for the export filename: word chars, dot and dash only,
// no leading/trailing separators, capped length. Falls back to 'workbook'.
function filenameBase(name) {
  const base = String(name || '').trim().replace(/[^\w.-]+/g, '_').replace(/^[_.-]+|[_.-]+$/g, '').slice(0, 80)
  return base || 'workbook'
}

export default function IntelReportModal({ intel, generatedAt, notebookName, onClose }) {
  const nbName = String(notebookName || '').trim()
  const reportTitle = nbName ? `Intelligence Report — ${nbName}` : 'Intelligence Report'
  const exportFilename = (ext) => `${filenameBase(nbName)}-intel-${new Date().toISOString().slice(0, 10)}.${ext}`
  const analyses = intel?.suggested_analyses || []
  const visualizations = intel?.visualizations || []
  const investigations = intel?.investigations || []
  const alerts = intel?.alerts || []
  const fullReport = intel?.full_report || ''

  const exportMarkdown = () => {
    let md = `# ${reportTitle}\n\n*Generated: ${generatedAt ? new Date(generatedAt).toLocaleString() : 'Unknown'}*\n\n---\n\n`
    if (fullReport) { md += `${fullReport}\n\n` }
    if (alerts.length > 0) {
      md += `---\n\n## Alerts\n\n`
      alerts.forEach((al, i) => { md += `${i + 1}. **[${(al.severity || '').toUpperCase()}]** ${al.message}\n   *Action:* ${al.action || 'N/A'}\n\n` })
    }
    const hasNextStepsMd = analyses.length > 0 || visualizations.length > 0 || investigations.length > 0
    if (hasNextStepsMd) {
      md += `---\n\n## Recommended Next Steps\n\n`
      if (analyses.length > 0) {
        md += `### Suggested Analyses\n\n`
        analyses.forEach((a, i) => { md += `${i + 1}. **${a.title}**\n   ${a.prompt}\n\n` })
      }
      if (visualizations.length > 0) {
        md += `### Recommended Visualizations\n\n`
        visualizations.forEach((v, i) => { md += `${i + 1}. **${v.title}** *(${v.chart_type})*\n   ${v.prompt}\n\n` })
      }
      if (investigations.length > 0) {
        md += `### Further Investigations\n\n`
        investigations.forEach((inv, i) => { md += `${i + 1}. **${inv.title}**\n   ${inv.reason}\n   *Prompt:* ${inv.prompt}\n\n` })
      }
    }
    md += `\n---\n\n*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*\n`
    const blob = new Blob([md], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = exportFilename('md'); a.click()
    URL.revokeObjectURL(url)
  }

  const exportHtml = () => {
    let bodyHtml = ''
    if (fullReport) { bodyHtml += marked.parse(fullReport, { breaks: true }) }
    if (alerts.length > 0) {
      bodyHtml += `<hr><h2>⚠️ Alerts</h2><ul>`
      alerts.forEach(al => { bodyHtml += `<li><strong style="color:#ff5c5c">[${(al.severity || '').toUpperCase()}]</strong> ${al.message}<br><em style="color:#aaa">Action: ${al.action || 'N/A'}</em></li>` })
      bodyHtml += `</ul>`
    }
    const hasNextStepsHtml = analyses.length > 0 || visualizations.length > 0 || investigations.length > 0
    if (hasNextStepsHtml) {
      bodyHtml += `<hr><h2>Recommended Next Steps</h2>`
      if (analyses.length > 0) {
        bodyHtml += `<h3>Suggested Analyses</h3><ol>`
        analyses.forEach(a => { bodyHtml += `<li><strong>${a.title}</strong><br><em>${a.prompt}</em></li>` })
        bodyHtml += `</ol>`
      }
      if (visualizations.length > 0) {
        bodyHtml += `<h3>Recommended Visualizations</h3><ol>`
        visualizations.forEach(v => { bodyHtml += `<li><strong>${v.title}</strong> <span style="color:#888">(${v.chart_type})</span><br><em>${v.prompt}</em></li>` })
        bodyHtml += `</ol>`
      }
      if (investigations.length > 0) {
        bodyHtml += `<h3>Further Investigations</h3><ol>`
        investigations.forEach(inv => { bodyHtml += `<li><strong>${inv.title}</strong><br>${inv.reason}<br><em>Prompt: ${inv.prompt}</em></li>` })
        bodyHtml += `</ol>`
      }
    }
    bodyHtml += `<hr style="margin-top:32px;border:none;border-top:1px solid #333"><footer style="text-align:center;padding:12px;color:#666;font-size:11px"><strong>Sandboxed Notebook on AWS</strong><br>Developed by the AWS Startup SA Team<br>&copy; ${new Date().getFullYear()} Amazon Web Services, Inc.</footer>`
    const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${escapeHtml(reportTitle)}</title><style>body{font-family:-apple-system,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;line-height:1.6;color:#e0e0e0;background:#1a1a2e}h1,h2,h3{color:#fff}code{background:#2d2d44;padding:2px 6px;border-radius:3px}pre{background:#2d2d44;padding:12px;border-radius:6px;overflow-x:auto}li{margin-bottom:8px}em{color:#aaa}table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}th{text-align:left;padding:6px 10px;background:#2d2d44;color:#fff;border:1px solid #3d3d5c}td{padding:5px 10px;border:1px solid #3d3d5c}tr:nth-child(even){background:#1f1f35}</style></head><body><h1>${escapeHtml(reportTitle)}</h1><p><em>Generated: ${generatedAt ? new Date(generatedAt).toLocaleString() : 'Unknown'}</em></p>${bodyHtml}</body></html>`
    const blob = new Blob([html], { type: 'text/html' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = exportFilename('html'); a.click()
    URL.revokeObjectURL(url)
  }

  const [, setMermaidTick] = useState(0)

  const bodyHtml = (() => {
    let html = ''
    // renderMarkdown adds KaTeX math + mermaid placeholders; then swap [SEVERITY]
    // tokens in the model-authored report prose for severity icons.
    if (fullReport) { html += replaceSeverityTokens(renderMarkdown(fullReport)) }
    if (alerts.length > 0) {
      html += `<hr style="margin:24px 0"><h2>⚠️ Alerts</h2><ul>`
      // Lead each alert with a severity ICON instead of the bracketed [HIGH]/[MEDIUM] text.
      alerts.forEach(al => { html += `<li>${severityIconHtml(al.severity)} ${al.message}<br><em style="color:var(--text-muted)">Action: ${al.action || 'N/A'}</em></li>` })
      html += `</ul>`
    }
    const hasNextSteps = analyses.length > 0 || visualizations.length > 0 || investigations.length > 0
    if (hasNextSteps) {
      html += `<hr style="margin:24px 0"><h2>Recommended Next Steps</h2>`
      if (analyses.length > 0) {
        html += `<h3>Suggested Analyses</h3><ol>`
        analyses.forEach(a => { html += `<li><strong>${a.title}</strong><br><span style="color:var(--text-muted)">${a.prompt}</span></li>` })
        html += `</ol>`
      }
      if (visualizations.length > 0) {
        html += `<h3>Recommended Visualizations</h3><ol>`
        visualizations.forEach(v => { html += `<li><strong>${v.title}</strong> <span style="opacity:0.6">(${v.chart_type})</span><br><span style="color:var(--text-muted)">${v.prompt}</span></li>` })
        html += `</ol>`
      }
      if (investigations.length > 0) {
        html += `<h3>Further Investigations</h3><ol>`
        investigations.forEach(inv => { html += `<li><strong>${inv.title}</strong><br>${inv.reason}<br><span style="color:var(--text-muted)"><em>${inv.prompt}</em></span></li>` })
        html += `</ol>`
      }
    }
    return html || '<p>No report content available.</p>'
  })()

  // Sanitize the assembled body, THEN splice cached mermaid SVGs in (DOMPurify
  // strips foreignObject label content, so the trusted SVG must bypass it). The
  // effect renders uncached diagrams and bumps the tick so this recomputes with the
  // SVG baked into the HTML string React owns (survives re-renders).
  const safeBodyHtml = spliceCachedMermaid(sanitizeMarkdown(bodyHtml))
  useEffect(() => {
    let cancelled = false
    ensureMermaidRendered(bodyHtml).then((changed) => {
      if (changed && !cancelled) setMermaidTick((t) => t + 1)
    })
    return () => { cancelled = true }
  }, [bodyHtml])

  // Re-theme live on an app theme toggle: the shared observer cleared the mermaid
  // cache, so bumping the tick re-renders — recomputing safeBodyHtml and re-running
  // the effect above to render the SVGs under the new theme.
  useEffect(() => onMermaidThemeChange(() => setMermaidTick((t) => t + 1)), [])

  // Portal to <body> (same as Modal.jsx / CellFocusOverlay): this modal is opened
  // from inside the AI Assistant panel, and any transform / filter / contain /
  // will-change:transform on that panel or an ancestor (e.g. its slide-in animation)
  // makes `position: fixed` resolve against the PANEL instead of the viewport — the
  // report then renders inside the right panel, uncentered and clipped.
  return createPortal(
    <div className="intel-modal-overlay" onClick={onClose}>
      <div className="intel-modal" onClick={e => e.stopPropagation()}>
        <div className="intel-modal-header">
          <div className="intel-modal-title">
            <h3 title={reportTitle}>
              Intelligence Report
              {nbName && <span className="intel-modal-nbname"> — {nbName}</span>}
            </h3>
            {generatedAt && <span className="intel-modal-date">{new Date(generatedAt).toLocaleString()}</span>}
          </div>
          <div className="intel-modal-actions">
            <div className="intel-export-wrapper">
              <button className="intel-header-action" onClick={exportMarkdown} title="Export as Markdown">
                <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                <span style={{ fontSize: '9px', marginLeft: '2px' }}>.md</span>
              </button>
              <button className="intel-header-action" onClick={exportHtml} title="Export as HTML">
                <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                <span style={{ fontSize: '9px', marginLeft: '2px' }}>.html</span>
              </button>
            </div>
            <button onClick={onClose}><IconX width={14} height={14} /></button>
          </div>
        </div>
        <div className="intel-modal-body" dangerouslySetInnerHTML={{ __html: safeBodyHtml }} />
      </div>
    </div>,
    document.body
  )
}
