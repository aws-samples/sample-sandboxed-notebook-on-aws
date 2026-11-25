import { useEffect, useRef, useCallback } from 'react'
import { apiFetch } from '../services/apiFetch'
import { readApiError } from '../services/apiError'
import { authHeaders } from '../config'
import { logError } from '../services/logger'
import { isVmSuspended } from '../constants'

/**
 * useNotebookFiles — sandbox (VM /tmp) files + S3 user-data file operations for
 * the active tab: list/refresh, upload, delete (local + S3), each wired to a
 * debounced Notebook Intel (re)generation (useWorkbookIntel then refreshes the Data
 * Sources panel once it is ready). Files are stored per-tab on `_localFiles`.
 */
export function useNotebookFiles({ tabs, setTabs, activeTabId, instances = {} }) {
  const intelTriggerTimer = useRef(null)
  const uploadsInFlight = useRef(0)  // batch counter: only trigger intel once ALL uploads settle
  const intelDeleteTimer = useRef(null)
  const intelS3DeleteTimer = useRef(null)
  // Names of the files uploaded in the current batch; carried on the
  // intel-generation-started event so the AI panel banner can name them.
  const uploadedNames = useRef([])

  // Always-current tabs snapshot read via a ref. CRITICAL: fetchFiles (and the
  // other callbacks) must NOT depend on the `tabs` array, or they'd be recreated
  // on every tab mutation (cell outputs, metrics, status). fetchFiles flows out as
  // `onRefreshFiles` into useDataSources, whose fetchDataSources + connect effect
  // depend on it — so an unstable fetchFiles made the connect effect re-run on
  // every keystroke/execution tick, hammering the VM's /files (429 storm) and the
  // /datasources/catalog resync (timeouts). Reading tabs from a ref keeps these
  // callbacks stable so they only run on real triggers (tab switch / connect).
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs

  // Same ref pattern for the instances (VM-state) map: read the CURRENT state
  // inside the stable fetch callbacks without adding `instances` to their deps
  // (which would recreate them every poll tick and re-trigger the connect
  // effect → the /files 429-storm this hook was hardened against).
  const instancesRef = useRef(instances)
  instancesRef.current = instances

  // Fetch files from the active MicroVM (stored per-tab to avoid cross-VM contamination)
  const fetchFiles = useCallback(async () => {
    const activeTab = (tabsRef.current || []).find(t => t.id === activeTabId)
    if (!activeTab || activeTab.status !== 'connected') {
      return
    }
    // Suspend gate: never wake a sleeping VM just to refresh its file list. The
    // last-known _localFiles stay shown; a real user action (running a cell) or an
    // explicit resume will bring the VM back and a subsequent refresh will update.
    if (isVmSuspended(instancesRef.current, activeTab.microvmId)) {
      return
    }

    const headers = authHeaders({})
    if (activeTab.sessionId) {
      headers['X-Session-Id'] = activeTab.sessionId
    }

    try {
      const resp = await fetch(`${activeTab.microvmEndpoint}/files`, { headers })
      if (resp.ok) {
        const data = await resp.json()
        const files = (data.files || []).map(f => ({
          name: f.name,
          size: f.size,
          last_modified: f.last_modified,   // ISO mtime → panel shows relative "2d ago"
          variable: f.name.split('/').pop().replace(/\.[^.]+$/, '').replace(/[-\s.]/g, '_'),
          status: 'ready',
        }))
        // Store files on the tab object so each VM has its own file list
        setTabs(prev => prev.map(t => t.id === activeTabId ? { ...t, _localFiles: files } : t))
      }
    } catch {
      // Ignore — might not be connected yet
    }
  }, [activeTabId, setTabs])

  // Refresh files when active tab changes or connects. Keyed on primitives
  // (activeTabId + the active tab's status) so it fires only on a real tab switch
  // or a connect transition — never on unrelated tab-object churn.
  const activeStatus = tabs.find(t => t.id === activeTabId)?.status
  useEffect(() => {
    fetchFiles()
  }, [activeTabId, activeStatus, fetchFiles])

  // Data Sources refresh after an intel (re)generation: NOT watched here any more.
  // Every trigger below dispatches 'intel-generation-started', which puts the single
  // useWorkbookIntel poller into its in-flight loop; that poller fires ONE
  // 'refresh-datasources' when the generation reaches 'ready' (services/
  // intelRefreshEdge.js). The old per-trigger watcher here added one extra 4 s
  // /workbook-intel chain per upload/delete and could poll ~33 min on a no-op.

  // Called when one upload settles: decrement the in-flight count and, ONLY once
  // the whole batch has drained (count back to 0), fire a single 'file_upload'
  // intel trigger. A short debounce coalesces the final settles and lets the VM
  // finish registering the files. If intel already exists this runs a delta that
  // now reliably includes every file just uploaded.
  const armIntelTrigger = useCallback((sessionId, fileName) => {
    if (fileName) uploadedNames.current.push(fileName)
    uploadsInFlight.current = Math.max(0, uploadsInFlight.current - 1)
    if (uploadsInFlight.current > 0) return  // batch not finished yet
    clearTimeout(intelTriggerTimer.current)
    intelTriggerTimer.current = setTimeout(() => {
      if (uploadsInFlight.current > 0) return  // a new upload started during the wait
      // Tell the intel panel to (re)enter its poll loop (event-driven polling).
      const fileNames = uploadedNames.current
      uploadedNames.current = []
      try { window.dispatchEvent(new CustomEvent('intel-generation-started', { detail: { sessionId, trigger: 'file_upload', fileNames } })) } catch {}
      apiFetch(`/workbook-intel/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': sessionId },
        body: JSON.stringify({ trigger: 'file_upload' }),
      })
        .catch(() => {})
    }, 800)
  }, [])

  const uploadFile = useCallback(async (file) => {
    // Find the active tab's connection to upload to
    const activeTab = (tabsRef.current || []).find(t => t.id === activeTabId)
    if (!activeTab || activeTab.status !== 'connected') {
      alert('Connect to a MicroVM first before uploading files.')
      return
    }

    // Batch tracking: count this upload as in-flight so the intel trigger only fires
    // once EVERY upload in a batch (e.g. the 2 "Load samples" files) has settled —
    // this guarantees the delta run sees all newly uploaded files, not just the first.
    uploadsInFlight.current += 1

    const size = file.size < 1024 * 1024
      ? `${(file.size / 1024).toFixed(1)} KB`
      : `${(file.size / (1024 * 1024)).toFixed(1)} MB`

    // Add to list immediately (uploading state)
    setTabs(prev => prev.map(t => t.id === activeTabId
      ? { ...t, _localFiles: [...(t._localFiles || []), { name: file.name, size, variable: null, status: 'uploading' }] }
      : t
    ))

    // Read as base64
    const reader = new FileReader()
    reader.onload = async (ev) => {
      const base64 = ev.target.result.split(',')[1]

      const headers = authHeaders({ 'Content-Type': 'application/json' })
      if (activeTab.sessionId) {
        headers['X-Session-Id'] = activeTab.sessionId
      }

      try {
        const response = await fetch(`${activeTab.microvmEndpoint}/upload`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ filename: file.name, data: base64 }),
        })
        const result = await response.json()

        setTabs(prev => prev.map(t => t.id === activeTabId
          ? { ...t, _localFiles: (t._localFiles || []).map(f =>
              f.name === file.name
                ? { ...f, variable: result.variable_name || null, status: result.success ? 'ready' : 'error' }
                : f
            ) }
          : t
        ))

        // This upload settled — arm the (batch-aware) intel trigger.
        if (result.success && activeTab.sessionId) {
          armIntelTrigger(activeTab.sessionId, file.name)
        } else {
          uploadsInFlight.current = Math.max(0, uploadsInFlight.current - 1)
        }
      } catch {
        uploadsInFlight.current = Math.max(0, uploadsInFlight.current - 1)
        setTabs(prev => prev.map(t => t.id === activeTabId
          ? { ...t, _localFiles: (t._localFiles || []).map(f =>
              f.name === file.name ? { ...f, status: 'error', variable: 'failed' } : f
            ) }
          : t
        ))
      }
    }
    reader.readAsDataURL(file)
  }, [activeTabId, setTabs, armIntelTrigger])

  const deleteFile = useCallback(async (filename) => {
    // Remove from frontend state immediately (optimistic)
    setTabs(prev => prev.map(t => t.id === activeTabId
      ? { ...t, _localFiles: (t._localFiles || []).filter(f => f.name !== filename) }
      : t
    ))
    // Delete from the VM's /tmp via the dedicated endpoint, which removes the file
    // AND reconciles the data catalog (prunes the entry) in one server-side step.
    // The old approach ran a raw os.remove via /proxy/execute, which deleted the
    // file but left a phantom catalog entry — so the deleted file kept showing up
    // in the Data Sources panel and the Workbook Intel report.
    const activeTab = (tabsRef.current || []).find(t => t.id === activeTabId)
    if (activeTab?.sessionId) {
      try {
        await apiFetch(`/proxy/delete-file`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Session-Id': activeTab.sessionId },
          body: JSON.stringify({ filename }),
        })
      } catch (e) { logError('deleteFile', e) }

      // Deletion intel: prune insights tied to the removed file. Mirrors the upload
      // flow — debounced 2s (batches rapid deletes), then trigger (useWorkbookIntel refreshes on ready).
      // The backend no-ops if there's no existing report to prune.
      clearTimeout(intelDeleteTimer.current)
      const triggerSessionId = activeTab.sessionId
      intelDeleteTimer.current = setTimeout(() => {
        try { window.dispatchEvent(new CustomEvent('intel-generation-started', { detail: { sessionId: triggerSessionId, trigger: 'file_delete', fileNames: [filename] } })) } catch {}
        apiFetch(`/workbook-intel/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Session-Id': triggerSessionId },
          body: JSON.stringify({ trigger: 'file_delete', deleted_source: `/tmp/${filename}` }),
        })
          .catch(() => {})
      }, 2000)
    }
  }, [activeTabId, setTabs])

  // Delete a deletable data source via its provider (restricted server-side by the
  // provider's is_deletable()). Named for its first use (S3 files under deletable
  // prefixes like user-data/) but now generic: it passes the source's `source_type`
  // so the route dispatches to the right provider. SageMaker endpoints
  // (source_type="sagemaker_endpoint") tear down the endpoint + config + model, so
  // we confirm first — that's a live, billed resource, not just a file. Mirrors the
  // local-file delete otherwise: remove the row, refresh the panel, then trigger a
  // debounced Notebook Intel deletion update. `src` is the discovered source object;
  // src.source_id is the canonical id (S3 URI, endpoint name, ...).
  const deleteS3File = useCallback(async (src) => {
    const sourceId = src?.source_id
    if (!sourceId) return
    const sourceType = src?.source_type || 's3'
    // NOTE: confirmation for consequential deletes (e.g. a live SageMaker endpoint)
    // is handled by the CALLER via the in-app ConfirmModal (DataSourcesPanel), not a
    // native window.confirm() here — so the dialog matches the app's styling and this
    // hook stays render-free (it just performs the delete when invoked).
    try {
      const resp = await apiFetch(
        `/datasources/s3-file?source_id=${encodeURIComponent(sourceId)}&source_type=${encodeURIComponent(sourceType)}`,
        { method: 'DELETE' },
      )
      if (!resp.ok) {
        alert(await readApiError(resp, `Delete failed: ${resp.status}`))
        return
      }
    } catch (e) {
      logError('deleteS3File', e)
      return
    }

    // Remove the row immediately (backend discover() will also no longer list it).
    window.dispatchEvent(new CustomEvent('refresh-datasources'))

    // Deletion intel: prune insights tied to the removed S3 file. Session-scoped like
    // local deletes; debounced 2s. Backend no-ops if there's no existing report.
    const activeTab = (tabsRef.current || []).find(t => t.id === activeTabId)
    if (activeTab?.sessionId) {
      clearTimeout(intelS3DeleteTimer.current)
      const triggerSessionId = activeTab.sessionId
      intelS3DeleteTimer.current = setTimeout(() => {
        const removedName = src?.name || String(sourceId).split('/').pop()
        try { window.dispatchEvent(new CustomEvent('intel-generation-started', { detail: { sessionId: triggerSessionId, trigger: 'file_delete', fileNames: [removedName] } })) } catch {}
        apiFetch(`/workbook-intel/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Session-Id': triggerSessionId },
          body: JSON.stringify({ trigger: 'file_delete', deleted_source: sourceId }),
        })
          .catch(() => {})
      }, 2000)
    }
  }, [activeTabId])

  // Stop / start a SageMaker INSTANCE endpoint from the panel. A proxy-side
  // control-plane call (no VM needed), mirroring deleteS3File: POST the action, then
  // fire 'refresh-datasources' so discovery re-runs and the status dot updates. After
  // a 'start' the endpoint reports 'Creating', which the useDataSources auto-poll
  // already treats as transitional → it polls until InService with no extra wiring.
  // `action` is 'stop' | 'start'; `src` is the discovered endpoint source object.
  const lifecycleEndpoint = useCallback(async (src, action) => {
    const sourceId = src?.source_id
    if (!sourceId) return
    try {
      const resp = await apiFetch(
        `/datasources/sagemaker-endpoint/lifecycle`
          + `?source_id=${encodeURIComponent(sourceId)}&action=${encodeURIComponent(action)}`,
        { method: 'POST' },
      )
      if (!resp.ok) {
        alert(await readApiError(resp, `${action} failed: ${resp.status}`))
        return
      }
    } catch (e) {
      logError('lifecycleEndpoint', e)
      return
    }
    // Re-discover so the row's status dot flips (stop → Stopped, start → Creating →
    // InService via the auto-poll).
    window.dispatchEvent(new CustomEvent('refresh-datasources'))
  }, [])

  return { fetchFiles, uploadFile, deleteFile, deleteS3File, lifecycleEndpoint }
}
