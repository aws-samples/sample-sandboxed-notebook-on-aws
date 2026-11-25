/**
 * IntelAccordion — the Workbook Intelligence surface, now living at the top of the
 * AI Assistant panel. Four single-open collapsible sections (Alerts, Analyses,
 * Visualizations, Investigate); only ONE section is open at a time. Each card is
 * clickable and sends a (context-enriched) prompt to the chat below.
 *
 * Per-card UI state (client-side, localStorage, keyed by notebookId — see
 * useIntelCardState):
 *   • ACTIONED — clicking a card marks it actioned → ✓ icon + muted (secondary) text.
 *     Counts stay as totals of what's VISIBLE (viewed ≠ removed).
 *   • DELETED  — a per-card trash HIDES the card (client-only; the S3 report is
 *     untouched). Section header trash hides the whole section; the topbar clears
 *     all. Hidden cards are recoverable via a per-section "show N hidden" toggle.
 *     A hidden card stays hidden across regenerations only while its CONTENT is
 *     unchanged (a reworded regeneration is new content and reappears).
 *
 * Otherwise presentational — report data + lifecycle come from useWorkbookIntel via props.
 *
 * Props:
 *   notebookId    — stable key for per-card state (ticks/hides)
 *   intel data:   analyses, visualizations, investigations, alerts, fullReport, hasContent
 *   status:       status, reportStatus, isUpdating, updateReason, generatedAt
 *   openSection:  'alerts' | 'analyses' | 'viz' | 'investigate' | null  (controlled)
 *   onToggleSection(key)  — user clicked a section header
 *   onCardClick(prompt, context, {cardTitle})  — user clicked a card (parent sends to chat + collapses)
 *   onOpenReport()        — open the full-report modal
 *   onRegenerate()        — manual refresh
 *   onCollapse()          — collapse the whole intel region to the strip
 */

import { useMemo } from 'react'
import { IconBarChart, IconChartLine, IconSearch, IconAlertTriangle, IconPlay, IconIntel, IconTrash } from './Icons'
import PanelState from './panels/PanelState'
import { useIntelCardState, intelCardKey } from '../hooks/useIntelCardState'

const ALERT_ICONS = { pii: '🔒', quality: '⚠️', duplicate: '📋', performance: '⚡' }
const CATEGORY_ICONS = { aggregation: '📊', join: '🔗', trend: '📈', correlation: '🔬' }

// Build the same context-enriched prompt the old IntelPanel used.
function buildPrompt(task, context) {
  return context && context.trim()
    ? `Context (from Workbook Intel):\n${context.trim()}\n\nTask: ${task}`
    : task
}

// Small tick shown on an actioned card.
function IconCheck({ width = 12, height = 12 }) {
  return (
    <svg width={width} height={height} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
  )
}

function SectionHeader({ icon, label, count, sectionKey, openSection, onToggle, onDeleteSection, hasVisible, alert, investigate }) {
  const open = openSection === sectionKey
  return (
    <div className={`intel-acc-header-row ${alert ? 'intel-acc-header-alert' : ''} ${investigate ? 'intel-acc-header-investigate' : ''}${open && hasVisible ? ' intel-acc-header-row-del' : ''}`}>
      <button
        className={`intel-acc-header ${open ? 'open' : ''}`}
        onClick={() => onToggle(sectionKey)}
        aria-expanded={open}
      >
        <svg className={`intel-acc-chevron ${open ? 'open' : ''}`} width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
        {icon}
        <span className="intel-acc-label">{label}</span>
        <span className={`intel-acc-count ${alert ? 'intel-acc-count-alert' : ''} ${investigate ? 'intel-acc-count-investigate' : ''}`}>{count}</span>
      </button>
      {/* Hide the whole section (client-only). Only shown when the section is open
          and has visible cards. */}
      {open && hasVisible && (
        <button className="intel-section-del" onClick={onDeleteSection} title={`Hide all ${label}`} aria-label={`Hide all ${label}`}>
          <IconTrash width={12} height={12} />
        </button>
      )}
    </div>
  )
}

export default function IntelAccordion({
  notebookId = null,
  notebookName = '', analyses = [], visualizations = [], investigations = [], alerts = [], fullReport = '', hasContent,
  status, phase, reportStatus, isUpdating, updateReason, generatedAt,
  openSection, onToggleSection, onCardClick, onOpenReport, onRegenerate, onCollapse,
}) {
  // The full set of card keys currently present (for pruning orphan localStorage
  // entries). Built from every section's items.
  const liveKeys = useMemo(() => {
    const s = new Set()
    for (const it of alerts) s.add(intelCardKey('alerts', it))
    for (const it of investigations) s.add(intelCardKey('investigations', it))
    for (const it of analyses) s.add(intelCardKey('analyses', it))
    for (const it of visualizations) s.add(intelCardKey('visualizations', it))
    return s
  }, [alerts, investigations, analyses, visualizations])

  const cardState = useIntelCardState(notebookId, liveKeys)
  const { isActioned, markActioned, toggleDeleted, deleteKeys, undeleteKeys } = cardState

  // Split each section's items into visible (not deleted) + hidden, each tagged with
  // its stable key. Recomputed when the cards OR the persisted card-state blob change
  // (a hide/undelete mutates blob → re-partition → re-render). All four partitions
  // are derived in one memo so the deps are explicit (cards + blob).
  const blob = cardState.blob
  const { alertsP, investigationsP, analysesP, vizP } = useMemo(() => {
    const split = (section, items) => {
      const visible = []
      const hidden = []
      for (const item of items) {
        const key = intelCardKey(section, item)
        ;(blob[key]?.deleted ? hidden : visible).push({ item, key })
      }
      return { visible, hidden }
    }
    return {
      alertsP: split('alerts', alerts),
      investigationsP: split('investigations', investigations),
      analysesP: split('analyses', analyses),
      vizP: split('visualizations', visualizations),
    }
  }, [alerts, investigations, analyses, visualizations, blob])

  // cardTitle rides along as metadata (client-side only) so the chat can label the
  // turn with the card's title instead of the long "Context (from Workbook Intel)" text.
  const runCard = (section, key, prompt, context, cardTitle) => {
    markActioned(key)
    onCardClick?.(buildPrompt(prompt, context), context, { cardTitle })
  }

  // "To-do" counts for the attention sections (Alerts, Investigate): the header
  // badge should reflect OUTSTANDING items — i.e. visible cards NOT yet actioned
  // (green ✓) — so acting on a card decrements the count, matching the FAB/attention
  // semantics. Analyses/Visualizations are reference lists (not to-dos), so they
  // keep showing the full visible total.
  const openCount = (partition) => partition.visible.reduce((n, c) => n + (isActioned(c.key) ? 0 : 1), 0)
  const alertsOpen = openCount(alertsP)
  const investigateOpen = openCount(investigationsP)
  const analysesOpen = openCount(analysesP)
  const vizOpen = openCount(vizP)

  const clearAll = () => {
    deleteKeys([
      ...alertsP.visible.map(c => c.key),
      ...investigationsP.visible.map(c => c.key),
      ...analysesP.visible.map(c => c.key),
      ...vizP.visible.map(c => c.key),
    ])
  }
  const anyVisible = alertsP.visible.length + investigationsP.visible.length
    + analysesP.visible.length + vizP.visible.length > 0

  // `phase` is the authoritative lifecycle signal from the backend (via useWorkbookIntel):
  //   phase1  → job running, no new/pruned cards yet  (big spinner if nothing to show)
  //   phase2  → cards are live; prose full_report still finalizing
  //   ready   → completed report at rest
  //   not_generated → nothing yet
  // Fall back to the legacy status/reportStatus combo if an older backend omits phase.
  const ph = phase
    || (status === 'generating' ? 'phase1'
        : status === 'ready' ? (reportStatus === 'generating' ? 'phase2' : 'ready')
        : 'not_generated')
  const isPhase1 = ph === 'phase1'
  const isPhase2 = ph === 'phase2'
  const isReady = ph === 'ready'
  const inProgress = isPhase1 || isPhase2

  // Section wrapper: the category (stripe + icon colour, via --intel-cat) and the
  // open state, so an open header and its card list render as one joined group.
  const sectionClass = (key) => `intel-acc-section intel-acc-cat-${key}${openSection === key ? ' intel-acc-section-open' : ''}`

  // A "show N hidden / hide again" footer for a section that has hidden cards.
  const HiddenFooter = ({ hidden }) => {
    if (hidden.length === 0) return null
    return (
      <button
        className="intel-hidden-toggle"
        onClick={() => undeleteKeys(hidden.map(c => c.key))}
        title="Restore hidden cards"
      >
        Show {hidden.length} hidden
      </button>
    )
  }

  return (
    <div className="intel-acc">
      {/* Header bar — the WHOLE bar toggles collapse; action buttons stopPropagation. */}
      <div className="intel-acc-topbar" role="button" tabIndex={0} title="Collapse Intel" onClick={onCollapse}>
        <IconIntel width={14} height={14} />
        <span className="intel-acc-title">{notebookName ? `${notebookName} Intel` : 'Workbook Intel'}</span>
        <div className="intel-acc-topbar-actions" onClick={(e) => e.stopPropagation()}>
          {/* Timestamp only for a completed report. */}
          {isReady && generatedAt && (
            <span className="intel-meta">{new Date(generatedAt).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
          )}
          {/* Full-report button enables ONLY when Phase 2 is done (phase='ready') and the
              prose report exists. During phase2 the report isn't ready yet. */}
          {isReady && fullReport && (
            <button className="intel-btn-report" onClick={onOpenReport} title="View full report">
              <svg width={17} height={17} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /></svg>
            </button>
          )}
          {/* Phase 2: cards are live but the prose report is still being written — show a
              disabled document icon so the user knows the report is coming. */}
          {isPhase2 && (
            <button className="intel-btn-icon" disabled title="Full report generating…">
              <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /></svg>
            </button>
          )}
          {/* Regenerate is disabled for the WHOLE job (both phases), enabled only at rest. */}
          <button className="intel-btn-icon" onClick={onRegenerate} disabled={inProgress} title="Refresh intelligence">
            <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></svg>
          </button>
          {/* Clear-all (rightmost): hide every visible card (client-only). Shown at rest
              with content. Placed last so it's the far-right action in the topbar. */}
          {isReady && anyVisible && (
            <button className="intel-btn-icon intel-btn-clear" onClick={clearAll} title="Hide all cards">
              <IconTrash width={14} height={14} />
            </button>
          )}
        </div>
        {/* Visual collapse affordance (whole bar is already clickable). */}
        <svg className="intel-acc-collapse-caret" width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="18 15 12 9 6 15" /></svg>
      </div>

      {/* Fresh-generation spinner — Phase 1 with nothing to show yet (first generation,
          or a delta before its pre-update cards were adopted). */}
      {isPhase1 && !hasContent && (
        <div className="intel-empty">
          <div className="intel-spinner" />
          <p>{isUpdating
            ? (updateReason === 'deletion' ? 'Updating the report after the file was removed…' : 'Updating the report with the new data…')
            : 'Analyzing data sources…'}</p>
          <p className="intel-empty-hint">10-20 seconds</p>
        </div>
      )}

      {status === 'error' && (
        <PanelState variant="empty" icon={<IconIntel width={24} height={24} />} message="Couldn't load intelligence" hint="Try refreshing." action={{ label: 'Retry', onClick: onRegenerate }} />
      )}

      {/* Completed but produced no insight cards. Only at rest (phase='ready') — never
          mid-job, where empty-content just means Phase 1 hasn't saved cards yet. */}
      {isReady && !hasContent && (
        <PanelState variant="empty" icon={<IconIntel width={24} height={24} />} message="No structured insights" hint="The report produced no insight cards." action={{ label: 'Regenerate', onClick: onRegenerate }} />
      )}

      {/* Accordion sections — render whenever we have content (even mid-update). */}
      {hasContent && (
        <div className="intel-acc-sections">
          {/* "Updating" strip — visible for the WHOLE delta, across BOTH phases. */}
          {isUpdating && inProgress && (
            <div className="intel-updating-strip">
              <div className="intel-spinner intel-spinner-sm" />
              <span>{isPhase1
                ? (updateReason === 'deletion'
                    ? 'Updating after a data source was removed — related insights will be pruned shortly…'
                    : 'Updating with newly uploaded data — new insights will appear shortly…')
                : (updateReason === 'deletion'
                    ? 'Insights pruned — finalizing the full report…'
                    : 'New insights added — finalizing the full report…')}</span>
            </div>
          )}

          {/* Alerts (first, default-open when present). Rendered when the section has
              any card (visible or hidden) so the "show hidden" affordance persists
              even after every card in a section is hidden. */}
          {(alertsP.visible.length > 0 || alertsP.hidden.length > 0) && (
            <div className={sectionClass('alerts')}>
              <SectionHeader icon={<IconAlertTriangle width={13} height={13} />} label="Alerts" count={alertsOpen} sectionKey="alerts" openSection={openSection} onToggle={onToggleSection} onDeleteSection={() => deleteKeys(alertsP.visible.map(c => c.key))} hasVisible={alertsP.visible.length > 0} alert />
              {openSection === 'alerts' && (
                <div className="intel-cards">
                  {alertsP.visible.map(({ item, key }) => (
                    <div key={key} className={`intel-card intel-card-alert intel-alert-${item.severity} ${isActioned(key) ? 'intel-card-actioned' : ''}`} role="button" tabIndex={0} title="Run in AI chat" onClick={() => runCard('alerts', key, item.action || item.message, item.message, item.message)}>
                      <span className="intel-card-icon">{ALERT_ICONS[item.type] || '⚠️'}</span>
                      <span className="intel-card-title">{item.message}</span>
                      {isActioned(key) && <span className="intel-card-check" aria-hidden="true"><IconCheck width={12} height={12} /></span>}
                      <span className="intel-card-run" aria-hidden="true"><IconPlay width={12} height={12} /></span>
                      <button className="intel-card-del" title="Hide this card" aria-label="Hide this card" onClick={(e) => { e.stopPropagation(); toggleDeleted(key) }}><IconTrash width={11} height={11} /></button>
                    </div>
                  ))}
                  <HiddenFooter hidden={alertsP.hidden} />
                </div>
              )}
            </div>
          )}

          {/* Investigate */}
          {(investigationsP.visible.length > 0 || investigationsP.hidden.length > 0) && (
            <div className={sectionClass('investigate')}>
              <SectionHeader icon={<IconSearch width={13} height={13} />} label="Investigate" count={investigateOpen} sectionKey="investigate" openSection={openSection} onToggle={onToggleSection} onDeleteSection={() => deleteKeys(investigationsP.visible.map(c => c.key))} hasVisible={investigationsP.visible.length > 0} investigate />
              {openSection === 'investigate' && (
                <div className="intel-cards">
                  {investigationsP.visible.map(({ item, key }) => (
                    <div key={key} className={`intel-card intel-card-investigate ${isActioned(key) ? 'intel-card-actioned' : ''}`} role="button" tabIndex={0} title="Run in AI chat" onClick={() => runCard('investigations', key, item.prompt, item.reason, item.title)}>
                      <span className="intel-card-icon">🔍</span>
                      <div className="intel-card-body">
                        <span className="intel-card-title">{item.title}</span>
                        {item.reason && <span className="intel-card-reason">{item.reason}</span>}
                      </div>
                      {isActioned(key) && <span className="intel-card-check" aria-hidden="true"><IconCheck width={12} height={12} /></span>}
                      <span className="intel-card-run" aria-hidden="true"><IconPlay width={12} height={12} /></span>
                      <button className="intel-card-del" title="Hide this card" aria-label="Hide this card" onClick={(e) => { e.stopPropagation(); toggleDeleted(key) }}><IconTrash width={11} height={11} /></button>
                    </div>
                  ))}
                  <HiddenFooter hidden={investigationsP.hidden} />
                </div>
              )}
            </div>
          )}

          {/* Analyses */}
          {(analysesP.visible.length > 0 || analysesP.hidden.length > 0) && (
            <div className={sectionClass('analyses')}>
              <SectionHeader icon={<IconBarChart width={13} height={13} />} label="Analyses" count={analysesOpen} sectionKey="analyses" openSection={openSection} onToggle={onToggleSection} onDeleteSection={() => deleteKeys(analysesP.visible.map(c => c.key))} hasVisible={analysesP.visible.length > 0} />
              {openSection === 'analyses' && (
                <div className="intel-cards">
                  {analysesP.visible.map(({ item, key }) => (
                    <div key={key} className={`intel-card ${isActioned(key) ? 'intel-card-actioned' : ''}`} role="button" tabIndex={0} title="Run in AI chat" onClick={() => runCard('analyses', key, item.prompt, item.title, item.title)}>
                      <span className="intel-card-icon">{CATEGORY_ICONS[item.category] || '📊'}</span>
                      <span className="intel-card-title">{item.title}</span>
                      {isActioned(key) && <span className="intel-card-check" aria-hidden="true"><IconCheck width={12} height={12} /></span>}
                      <span className="intel-card-run" aria-hidden="true"><IconPlay width={12} height={12} /></span>
                      <button className="intel-card-del" title="Hide this card" aria-label="Hide this card" onClick={(e) => { e.stopPropagation(); toggleDeleted(key) }}><IconTrash width={11} height={11} /></button>
                    </div>
                  ))}
                  <HiddenFooter hidden={analysesP.hidden} />
                </div>
              )}
            </div>
          )}

          {/* Visualizations */}
          {(vizP.visible.length > 0 || vizP.hidden.length > 0) && (
            <div className={sectionClass('viz')}>
              <SectionHeader icon={<IconChartLine width={13} height={13} />} label="Visualizations" count={vizOpen} sectionKey="viz" openSection={openSection} onToggle={onToggleSection} onDeleteSection={() => deleteKeys(vizP.visible.map(c => c.key))} hasVisible={vizP.visible.length > 0} />
              {openSection === 'viz' && (
                <div className="intel-cards">
                  {vizP.visible.map(({ item, key }) => (
                    <div key={key} className={`intel-card ${isActioned(key) ? 'intel-card-actioned' : ''}`} role="button" tabIndex={0} title="Run in AI chat" onClick={() => runCard('visualizations', key, item.prompt, item.title, item.title)}>
                      <span className="intel-card-icon">📉</span>
                      <span className="intel-card-title">{item.title}</span>
                      <span className="intel-card-badge">{item.chart_type}</span>
                      {isActioned(key) && <span className="intel-card-check" aria-hidden="true"><IconCheck width={12} height={12} /></span>}
                      <span className="intel-card-run" aria-hidden="true"><IconPlay width={12} height={12} /></span>
                      <button className="intel-card-del" title="Hide this card" aria-label="Hide this card" onClick={(e) => { e.stopPropagation(); toggleDeleted(key) }}><IconTrash width={11} height={11} /></button>
                    </div>
                  ))}
                  <HiddenFooter hidden={vizP.hidden} />
                </div>
              )}
            </div>
          )}

        </div>
      )}
    </div>
  )
}
