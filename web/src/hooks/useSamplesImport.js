import { useState, useCallback } from 'react'
import { apiFetch } from '../services/apiFetch'
import { readApiError } from '../services/apiError'
import { newCellId } from './useNotebookCells'
import { SLOTS } from '../services/actionBanner'
import { clearOn as clearBannersOn } from '../services/bannerStore'
// A new import starting removes the previous import banner (same-op-start).
const clearImportBanner = () => clearBannersOn('same-op-start', { slot: SLOTS.NOTEBOOKS, match: b => b.kind === 'import' })

/**
 * useSamplesImport — welcome-screen sample gallery + "Import from Git URL" + loading
 * a bundled sample notebook. Each path creates a new tab (via createTab) and opens it.
 *
 * Tab creation is threaded in via createTab/setTabs/setActiveTabId/setShowAiChat.
 * onImportResult({ tabId, ok, name, error }) reports the outcome for the "Imported"
 * banner in the Notebooks panel (App).
 */
export function useSamplesImport({ createTab, setTabs, setActiveTabId, setShowAiChat, onImportResult }) {
  const [showGitImport, setShowGitImport] = useState(false)
  const [gitImportUrl, setGitImportUrl] = useState('')
  const [gitImportLoading, setGitImportLoading] = useState(false)

  // Sample gallery (welcome screen) — lazily loads the sample manifest on first open
  const [showSampleGallery, setShowSampleGallery] = useState(false)
  const [samples, setSamples] = useState([])
  const [samplesLoaded, setSamplesLoaded] = useState(false)

  const loadSample = useCallback(async (sampleUrl, sampleName) => {
    clearImportBanner()
    try {
      const resp = await fetch(sampleUrl)
      const notebook = await resp.json()

      const tab = await createTab(sampleName || notebook.name, notebook.description || '', 'Samples')
      tab._loadedCells = notebook.cells
      tab._cells = notebook.cells.map((c) => ({
        id: c.id || newCellId(),
        type: c.type || 'code',
        code: c.code || '',
        output: c.output || null,
        error: c.error || null,
        html: c.html || null,
        image: c.image || null,
        imageS3Key: null,
        aiExplanation: c.aiExplanation || null,
        outputVariable: c.outputVariable || null,
      }))
      setTabs(prev => [...prev, { ...tab }])
      setActiveTabId(tab.id)
      setShowAiChat(true)
      onImportResult?.({ tabId: tab.id, ok: true, name: tab.name || sampleName })
    } catch (err) {
      alert(`Failed to load sample: ${err.message}`)
      onImportResult?.({ ok: false, error: `Failed to load sample: ${err.message}` })
    }
  }, [onImportResult])

  const toggleSampleGallery = useCallback(async () => {
    setShowGitImport(false)
    setShowSampleGallery(v => !v)
    if (!samplesLoaded) {
      try {
        // Load the manifest and, in parallel, ask the proxy whether Aurora is
        // configured. Samples flagged `requiresAurora` (the analytics workbook)
        // only work with --AuroraSetup, so hide them otherwise — running one
        // without Aurora just errors on every read_aurora() cell.
        const [mResp, iResp] = await Promise.all([
          fetch('/samples/index.json'),
          apiFetch(`/instances`).catch(() => null),
        ])
        const data = await mResp.json()
        let auroraOn = false
        try { auroraOn = !!(iResp && (await iResp.json()).aurora_configured) } catch {}
        const list = Array.isArray(data) ? data : []
        setSamples(auroraOn ? list : list.filter(s => !s.requiresAurora))
      } catch { setSamples([]) }
      setSamplesLoaded(true)
    }
  }, [samplesLoaded])

  const importFromGitUrl = useCallback(async () => {
    if (!gitImportUrl.trim()) return
    clearImportBanner()
    setGitImportLoading(true)
    try {
      const resp = await apiFetch(`/import-from-url`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: gitImportUrl.trim() }),
      })
      if (!resp.ok) {
        const msg = await readApiError(resp, `Import failed: ${resp.status}`)
        alert(msg)
        onImportResult?.({ ok: false, error: msg })
        setGitImportLoading(false)
        return
      }
      const notebook = await resp.json()
      const tab = await createTab(notebook.name || 'Imported', notebook.description || '', 'Imported')
      tab._loadedCells = notebook.cells
      tab._cells = notebook.cells.map((c) => ({
        id: c.id || newCellId(),
        type: c.type || 'code',
        code: c.code || '',
        output: null,
        error: null,
        html: null,
        image: null,
        imageS3Key: null,
        outputVariable: c.outputVariable || null,
      }))
      tab.sourceUrl = notebook.source_url
      setTabs(prev => [...prev, { ...tab }])
      setActiveTabId(tab.id)
      setShowGitImport(false)
      setGitImportUrl('')
      setShowAiChat(true)
      onImportResult?.({ tabId: tab.id, ok: true, name: tab.name || notebook.name })
    } catch (err) {
      alert(`Import error: ${err.message}`)
      onImportResult?.({ ok: false, error: err.message })
    }
    setGitImportLoading(false)
  }, [gitImportUrl, onImportResult])

  return {
    loadSample,
    showGitImport,
    setShowGitImport,
    gitImportUrl,
    setGitImportUrl,
    gitImportLoading,
    importFromGitUrl,
    showSampleGallery,
    setShowSampleGallery,
    toggleSampleGallery,
    samples,
  }
}
