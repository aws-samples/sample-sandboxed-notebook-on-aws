import { useState, useEffect, useMemo } from 'react'
import { IconX } from '../Icons'
import PanelState from './PanelState'
import PanelExpandButton from './PanelExpandButton'
import { highlightMatch } from './highlightMatch'
import { showcasedFeaturesOf, filterSamples } from '../../services/sampleFeatures'

export default function SamplesPanel({ onLoadSample, onClose, expanded, onToggleExpand }) {
  const [samples, setSamples] = useState([])
  const [search, setSearch] = useState('')

  useEffect(() => {
    fetch('/samples/index.json')
      .then(r => r.json())
      .then(setSamples)
      .catch(() => {})
  }, [])

  // Search box: same markup, classes and keys (Esc clears) as the Snippets panel;
  // matches the sample name OR any showcased-feature label (filterSamples).
  const searchLc = search.trim().toLowerCase()
  const filtered = useMemo(() => filterSamples(samples, search), [samples, search])

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Samples</span>
        <span className="sidebar-panel-count">{searchLc ? `${filtered.length} of ${samples.length}` : samples.length}</span>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      <div className="outline-search">
        <input
          className="outline-search-input"
          type="text"
          placeholder="Search samples..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape' && search) { e.preventDefault(); setSearch('') } }}
          spellCheck={false}
        />
        {search && (
          <button
            className="outline-search-clear"
            onClick={() => setSearch('')}
            title="Clear search (Esc)"
            aria-label="Clear search"
          >
            <IconX width={12} height={12} />
          </button>
        )}
      </div>
      <div className="sidebar-panel-body">
        {samples.length === 0 && (
          <PanelState variant="loading" message="Loading samples…" />
        )}
        {searchLc && samples.length > 0 && filtered.length === 0 && (
          <div className="sidebar-panel-hint" style={{ padding: '12px', fontSize: '12px', color: 'var(--text-muted)' }}>
            No samples match “{search.trim()}”.
          </div>
        )}
        <div className="sidebar-samples-list">
          {filtered.map(sample => (
            <button
              key={sample.id}
              // Shared side-panel row (.sidebar-item: padding, hover, active, focus
              // ring, light/dark + ember) like the Notebooks rows; .sidebar-sample-item
              // only resets the <button> chrome. The start page keeps its .sample-card.
              type="button"
              className="sidebar-item sidebar-sample-item"
              onClick={() => {
                onLoadSample(`/samples/${sample.file}`, sample.name)
                // Opening a sample from THIS panel should reveal the newly-created
                // notebook in context: switch the sidebar to the Notebooks panel.
                // The Sidebar owns activePanel and listens for this event (same
                // mechanism as 'notebook-cells-inserted' → Outline).
                try { window.dispatchEvent(new CustomEvent('open-notebooks-panel')) } catch { /* no-op */ }
              }}
              title={sample.description || sample.name}
            >
              <span className="sample-card-icon">{sample.icon}</span>
              <span className="sample-card-text">
                <span className="sample-card-name">{searchLc ? highlightMatch(sample.name, search) : sample.name}</span>
                {sample.description && <span className="sample-card-desc">{sample.description}</span>}
                {showcasedFeaturesOf(sample).length > 0 && (
                  <span className="sample-feature-pills" aria-label="Showcased features">
                    {showcasedFeaturesOf(sample).map(f => (
                      <span key={f} className="sample-feature-pill">{searchLc ? highlightMatch(f, search) : f}</span>
                    ))}
                  </span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
