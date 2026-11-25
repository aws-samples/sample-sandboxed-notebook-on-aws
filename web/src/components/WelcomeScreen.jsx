/**
 * WelcomeScreen — the empty-state shown when no notebooks are open.
 * Presentational: New/Open/Sample/Git actions, the sample gallery, the git-import
 * input, and the getting-started hints. All state + handlers are passed in as props.
 */
import { useState, useEffect } from 'react'
import { IconSun, IconMoon, IconFlame } from './Icons'
import { parseNotebookFile } from '../services/notebookImport'

export default function WelcomeScreen({
  theme,
  onToggleTheme,
  onNewNotebook,
  showSampleGallery,
  onToggleSampleGallery,
  samples = [],
  onLoadSample,
  showGitImport,
  setShowGitImport,
  setShowSampleGallery,
  gitImportUrl,
  setGitImportUrl,
  gitImportLoading,
  onImportFromGitUrl,
}) {
  // Open the Notebooks side panel (the list of ALL notebooks saved in the DB —
  // open + closed, reopenable). Reuses the same event the ⌥1 shortcut fires, so
  // there's no extra wiring. This is the discoverable path to saved notebooks when
  // no notebook is open (welcome screen) — distinct from "Open File…" which imports
  // a file from disk.
  const handleBrowseNotebooks = () => {
    window.dispatchEvent(new CustomEvent('toggle-sidebar-panel', { detail: 'notebooks' }))
  }

  // Mirror the sidebar's active panel so we can HIDE the "Browse Notebooks" button
  // when the Notebooks panel is already open (the button would be redundant, and a
  // toggle would confusingly close it). The Sidebar owns `activePanel`, persists it
  // to localStorage['microvm-sidebar-panel'], and now emits a 'sidebar-panel-changed'
  // event on every change (via icon click, keyboard shortcut, or programmatic toggle)
  // — a single source of truth we track here without a refactor. Seed from
  // localStorage for the initial render before any event fires.
  const [activeSidebarPanel, setActiveSidebarPanel] = useState(() => {
    // Mirror Sidebar's seed: it defaults to 'notebooks' when nothing is persisted,
    // so match that here to avoid a first-paint flash of the button before the
    // Sidebar's mount effect broadcasts the real value.
    try { return localStorage.getItem('microvm-sidebar-panel') || 'notebooks' } catch { return 'notebooks' }
  })
  useEffect(() => {
    const onChanged = (e) => setActiveSidebarPanel(e.detail ?? null)
    window.addEventListener('sidebar-panel-changed', onChanged)
    return () => window.removeEventListener('sidebar-panel-changed', onChanged)
  }, [])
  const notebooksPanelOpen = activeSidebarPanel === 'notebooks'

  // Open a local notebook file (.notebook.json or Jupyter .ipynb) via a hidden input.
  const handleOpenFile = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.json,.notebook.json,.ipynb'
    input.onchange = (e) => {
      const file = e.target.files?.[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = (ev) => {
        try {
          // services/notebookImport.js: the one .ipynb / .notebook.json parser.
          const detail = parseNotebookFile(JSON.parse(ev.target.result), file.name)
          window.dispatchEvent(new CustomEvent('open-notebook', { detail }))
        } catch { alert('Invalid notebook file.') }
      }
      reader.readAsText(file)
    }
    input.click()
  }

  return (
    <div className="app-empty">
      <button className="app-empty-theme-btn" onClick={onToggleTheme} title={`Switch theme (${theme})`}>
        {theme === 'dark' ? <IconSun width={16} height={16} /> : theme === 'light' ? <IconFlame width={16} height={16} /> : <IconMoon width={16} height={16} />}
      </button>
      <div className="app-empty-icon">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
          <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" fill="rgba(137,180,250,0.25)" stroke="#89b4fa"/>
        </svg>
      </div>
      <h2 className="app-empty-title">Sandboxed Notebook on AWS</h2>
      <p className="app-empty-subtitle">AI-Powered Python & SQL Notebooks on Serverless Firecracker Sandboxes</p>
      <div className="app-empty-badges">
        <span className="app-empty-badge">🐍 Python</span>
        <span className="app-empty-badge">🗄️ SQL</span>
        <span className="app-empty-badge">✨ AI</span>
        <span className="app-empty-badge">🔥 Firecracker</span>
      </div>
      <div className="app-empty-actions">
        <button className="app-empty-btn app-empty-btn-primary" onClick={onNewNotebook}>
          + New Notebook
        </button>
        {!notebooksPanelOpen && (
          <button className="app-empty-btn" onClick={handleBrowseNotebooks} title="See all notebooks saved in the database (open or closed) and reopen one">
            Browse Notebooks
          </button>
        )}
        <button className="app-empty-btn" onClick={handleOpenFile} title="Import a notebook file from disk (.ipynb or .notebook.json)">
          Open File…
        </button>
        <button className={`app-empty-btn${showSampleGallery ? ' app-empty-btn-active' : ''}`} onClick={onToggleSampleGallery} aria-expanded={showSampleGallery}>
          Open Sample
          <svg className="app-empty-btn-chevron" width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <button className="app-empty-btn" onClick={() => { setShowSampleGallery(false); setShowGitImport(v => !v) }}>
          Import from Git URL
        </button>
      </div>
      <div className={`app-empty-samples-wrap${showSampleGallery ? ' open' : ''}`}>
        <div className="app-empty-samples-inner">
          <div className="app-empty-samples">
            {samples.length === 0 && (
              <div className="app-empty-samples-loading">Loading samples…</div>
            )}
            {samples.map(s => (
              <button
                key={s.id}
                className="sample-card"
                onClick={() => onLoadSample(`/samples/${s.file}`, s.name)}
                title={s.description || s.name}
                tabIndex={showSampleGallery ? 0 : -1}
              >
                <span className="sample-card-icon">{s.icon}</span>
                <span className="sample-card-text">
                  <span className="sample-card-name">{s.name}</span>
                  {s.description && <span className="sample-card-desc">{s.description}</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
      {showGitImport && (
        <div className="app-empty-git-import">
          <input
            className="app-empty-git-input"
            type="text"
            placeholder="Paste GitHub URL (e.g. https://github.com/user/repo/blob/main/notebook.ipynb)"
            value={gitImportUrl}
            onChange={e => setGitImportUrl(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') onImportFromGitUrl(); if (e.key === 'Escape') setShowGitImport(false) }}
            autoFocus
            disabled={gitImportLoading}
          />
          <button className="app-empty-git-btn" onClick={onImportFromGitUrl} disabled={gitImportLoading || !gitImportUrl.trim()}>
            {gitImportLoading ? 'Importing...' : 'Import'}
          </button>
        </div>
      )}
      {/* Getting-started hints — hidden while the sample gallery is open so the
          gallery has room to breathe (the two would otherwise compete for the same
          vertical space, cramping the cards behind an internal scrollbar). */}
      <div className={`app-empty-hints${showSampleGallery ? ' app-empty-hints-hidden' : ''}`} aria-hidden={showSampleGallery}>
        <div className="app-empty-hint">
          <span className="app-empty-hint-icon">1</span>
          <span>Create a notebook and connect to a MicroVM sandbox</span>
        </div>
        <div className="app-empty-hint">
          <span className="app-empty-hint-icon">2</span>
          <span>Write Python or SQL in cells — <kbd>Shift+Enter</kbd> to execute</span>
        </div>
        <div className="app-empty-hint">
          <span className="app-empty-hint-icon">3</span>
          <span>Use the <strong>AI assistant</strong> — toggle any cell to AI mode, describe what you want, and get code generated</span>
        </div>
        <div className="app-empty-hint">
          <span className="app-empty-hint-icon">4</span>
          <span>Click data sources in the sidebar to insert ready-to-run query code</span>
        </div>
        <div className="app-empty-hint">
          <span className="app-empty-hint-icon">5</span>
          <span>Open a <strong>terminal</strong> into the MicroVM or view its <strong>logs</strong> to troubleshoot</span>
        </div>
      </div>
    </div>
  )
}
