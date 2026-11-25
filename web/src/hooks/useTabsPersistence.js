import { useCallback, useEffect, useRef, useMemo } from 'react'
import { fetchNotebooks, saveNotebook as apiSaveNotebook, loadChatMessages, createNotebookVersion } from '../services/notebooks'
import { newerDag } from '../services/depGraph'
import { hasImageOutput, runCompletionSignature } from '../services/cellOutput'
import { stripEnvelopeImages } from '../services/cellOutputs'
import { fitCellsForStorage } from '../services/storageGuard'

// How often the background auto-version timer wakes to check for changes. This is
// deliberately DECOUPLED from the 3s autosave: autosave keeps the working copy
// fresh on every edit; auto-versioning only takes a durable snapshot occasionally,
// and only when the document actually changed — so a typing session yields a
// handful of restore points, not hundreds.
const AUTO_VERSION_INTERVAL_MS = 5 * 60 * 1000  // 5 minutes

// API autosave debounce (user edits and completed single-cell runs).
const AUTOSAVE_DEBOUNCE_MS = 3000
// While a batch run (Run All / run-from / reactive cascade) is in progress,
// run-driven saves are held until it ends; this is the longest a completed run
// waits for a safety save during a very long batch.
const BATCH_MAX_WAIT_MS = 30 * 1000

// Content signature for a tab's DOCUMENT — the same fields we version server-side.
// If this is unchanged since the last snapshot, the interval skips the tab (no
// empty/duplicate auto-versions).
function documentSignature(tab) {
  const cells = (tab._cells || []).map(c => [
    c.id,
    c.type || 'code',
    c.code || '',
    c.outputVariable ?? '',
    c.aiExplanation || '',
    c.aiExplanationSource || '',
    c.generated || '',
  ])
  return JSON.stringify(cells)
}

// USER-CONTENT signature — the subset of persisted content that represents an
// actual USER EDIT, used to drive the autosave debounce + the "Saving…/Saved"
// indicator. It DELIBERATELY excludes:
//   • VM-binding churn: sessionId / microvmId / checkpointEnabled — these change
//     when a VM launches/restores (NOT a user edit), and each change was re-arming
//     the 3s debounce, pinning the toolbar to "Saving…" for the whole (slow) VM
//     launch even though the actual save PUT is ~5ms.
//   • Volatile cell RUNTIME output: output / error / html —
//     these populate as cells hydrate/run during open, churning the signature and
//     re-arming the debounce. They regenerate on re-run and aren't the source of
//     truth, so they shouldn't schedule a save on their own. The per-run fields
//     executionNumber / executionTime / lastExecutedCode / peakMemMb /
//     peakCpuPct / executedAt are excluded here too — a COMPLETED run
//     is tracked separately by runCompletionSignature (below), which re-arms the
//     same debounce exactly once per finished cell run.
// Everything here still lives in buildNotebookSavePayload, so it's ALL persisted on
// the next save — this only controls WHEN the debounce re-arms, not WHAT is written.
function userContentSignature(tabs) {
  return JSON.stringify((tabs || []).map(tab => [
    String(tab.id),
    tab.name,
    tab.description || '',
    tab.tag || 'Drafts',
    (tab._cells || []).map(c => [
      c.id,
      c.type || 'code',
      c.code || '',
      c.outputVariable ?? null,
      c.aiExplanation || null,
      c.aiExplanationEdited || false,
      c.aiExplanationSource || null,
      c.generated || null,
    ]),
  ]))
}

// RUN-COMPLETION signature — one entry per cell keyed on `executedAt`, which the
// execute paths (single cell, Run All / run-from, reactive dependents) stamp in the
// SAME patch as the cell's result: executionNumber, peakMemMb/peakCpuPct, output.
// (Dependency facts live in the stored DAG, not on cells.) Without this, a run with no code edit never
// changed userContentSignature, so the debounced save never fired and those runtime
// fields were lost (only localStorage had them). `executedAt` changes once per
// finished run — not on run start, output streaming, status flips or VM-launch
// churn. During a batch run (Run All / run-from / reactive cascade) completions
// are HELD instead of re-arming the debounce — a gap ≥3s between two cells used
// to fire a save mid-run — and ONE save follows the batch end (plus a max-wait
// safety save every BATCH_MAX_WAIT_MS during very long batches).
// `imageS3Key` (the persisted chart's S3 location) is set in the same patch as
// executedAt, so a run still changes this once; clearing a chart cell's output
// (key → null) also changes it, so the null is saved and the object cleaned up.
// runCompletionSignature lives in services/cellOutput.js (pure, unit-tested).

// Ids of the tabs with a batch run in progress ('[]' when none). `_batchRunning`
// is a transient flag set by useNotebookCells.executeBatch; never persisted.
function batchRunningSignature(tabs) {
  return JSON.stringify((tabs || []).filter(t => t && t._batchRunning).map(t => String(t.id)))
}

// Build the API save payload for one tab — the SINGLE definition of what a
// persisted notebook looks like, reused by the debounced autosave AND the
// synchronous flush (so a manual "Save version" can force the latest cells to the
// server before it snapshots them). Keeping one builder prevents drift.
function buildNotebookSavePayload(tab) {
  const cells = (tab._cells || []).map(c => ({
    id: c.id,
    type: c.type || 'code',
    code: c.code || '',
    output: c.output || null,
    error: c.error || null,
    // A multi-output envelope keeps only each image's S3 key (never base64).
    html: stripEnvelopeImages(c.html) || null,
    outputVariable: c.outputVariable ?? null,
    aiExplanation: c.aiExplanation || null,
    aiExplanationEdited: c.aiExplanationEdited || false,
    aiExplanationSource: c.aiExplanationSource || null,
    generated: c.generated || null,
    // Per-run runtime fields (persisted, NOT part of any signature). Dependency
    // facts (reads/writes/sources/staleness) live in the stored DAG document.
    executionNumber: c.executionNumber ?? null,
    executionTime: c.executionTime ?? null,
    lastExecutedCode: c.lastExecutedCode ?? null,
    peakMemMb: typeof c.peakMemMb === 'number' ? c.peakMemMb : null,
    peakCpuPct: typeof c.peakCpuPct === 'number' ? c.peakCpuPct : null,
    executedAt: c.executedAt || null,
    // Chart: only the S3 LOCATION (validated server-side to this notebook/cell),
    // never the base64 `image`.
    imageS3Key: c.imageS3Key || null,
  }))
  return {
    id: String(tab.id),
    name: tab.name,
    description: tab.description || '',
    tag: tab.tag || 'Drafts',
    cells,
    session_id: tab.sessionId || null,
    microvm_id: tab.microvmId || null,
    checkpoint_enabled: tab.checkpointEnabled || false,
  }
}

/**
 * useTabsPersistence — persists notebook tabs to localStorage (debounced) and to
 * the API (debounced), loads notebooks from the API on first mount, and
 * persists the active tab id. Side-effect only hook (returns nothing).
 */
export function useTabsPersistence({ tabs, setTabs, activeTabId, setActiveTabId }) {
  const saveTimerRef = useRef(null)
  const apiSaveTimerRef = useRef(null)
  // Batch hold (see the API-save effect): the max-wait safety timer, whether a
  // completed run is not yet saved, and the signatures seen by the last effect run.
  const maxWaitTimerRef = useRef(null)
  const runDirtyRef = useRef(false)
  const lastSigsRef = useRef(null)
  // Auto-version bookkeeping: always-current tabs snapshot (so the interval reads
  // live state without re-arming on every keystroke) + last snapshotted signature
  // per tab id (seeded on load so we never version an untouched notebook).
  const tabsRef = useRef(tabs)
  const lastVersionSigRef = useRef({})
  // Sig of the auto-version request currently IN FLIGHT per tab id — dedupes
  // concurrent ticks without marking the sig "done" (that only happens on success).
  const inFlightVersionSigRef = useRef({})
  tabsRef.current = tabs

  // The PUT response carries the stored DAG document rebuilt from the saved cells;
  // hand it to the tab (newest wins — an execute response may already have
  // delivered a later version).
  const applySavedDags = useCallback((entries) => {
    const docs = new Map()
    for (const { tab, res } of entries || []) {
      if (!res || !res.ok || !res.dag) continue
      docs.set(String(res.recreated ? res.oldId : tab.id), res.dag)
    }
    if (docs.size === 0) return
    setTabs(prev => prev.map(t => {
      const doc = t && docs.get(String(t.id))
      return doc && newerDag(t._dag, doc) ? { ...t, _dag: doc } : t
    }))
  }, [setTabs])

  // Persist tabs to localStorage (debounced 1.5s to avoid thrashing during typing)
  useEffect(() => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    saveTimerRef.current = setTimeout(() => {
    // `_dag` (the stored dependency DAG) is never cached locally: it is re-fetched
    // from the proxy on reopen/mount.
    // `_batchRunning` is transient run state: a stale `true` must not survive a reload.
    // `_runBanner` (Outline run banner), `_aiBanner` (AI panel banner) and
    // `_connectError` (last Connect failure) are transient
    // too: never persisted.
    // `_restoreReport` (the last restore's package report) describes one VM: not persisted,
    // nor `_restorePackages` (the pending restore's package list).
    const toSave = tabs.map(({ _loadedCells, _dag, _batchRunning, _runBanner, _aiBanner, _connectError, _restoreReport, _restorePackages, ...tab }) => {
      // Persist cells with code and text outputs, but strip base64 images
      const cells = (tab._cells || []).map(c => ({
        id: c.id,
        type: c.type || 'code',
        code: c.code || '',
        output: c.output || null,
        error: c.error || null,
        html: stripEnvelopeImages(c.html) || null, // envelope: images by S3 key only
        image: null, // Strip base64 images (too large for localStorage)
        imageS3Key: c.imageS3Key || null, // keep: the chart reloads from S3 by key
        status: c.output || c.error || c.html || hasImageOutput(c) ? 'success' : 'idle',
        executionNumber: c.executionNumber || null,
        executionTime: c.executionTime || null,
        lastExecutedCode: c.lastExecutedCode || null,
        outputVariable: c.outputVariable ?? null, // SQL result var name — else it's lost on reload
        aiExplanation: c.aiExplanation || null,
        aiExplanationEdited: c.aiExplanationEdited || false,
        aiExplanationSource: c.aiExplanationSource || null,
        generated: c.generated || null,
        // Runtime fields for "How was this computed?" — survive reload.
        peakMemMb: typeof c.peakMemMb === 'number' ? c.peakMemMb : null,
        peakCpuPct: typeof c.peakCpuPct === 'number' ? c.peakCpuPct : null,
        executedAt: c.executedAt || null,
      }))
      return { ...tab, _cells: cells.length > 0 ? cells : undefined }
    })
    try {
      // One heavy envelope must not push the whole save over the quota and wipe
      // every tab's outputs: oversized rich outputs are dropped largest-first.
      localStorage.setItem('microvm-notebooks', JSON.stringify(fitCellsForStorage(toSave).tabs))
    } catch {
      // If localStorage is full (quota exceeded), save without outputs
      const minimal = tabs.map(({ _cells, _loadedCells, _dag, _batchRunning, _runBanner, _aiBanner, _connectError, _restoreReport, _restorePackages, ...rest }) => ({
        ...rest,
        _cells: (_cells || []).map(c => ({ id: c.id, type: c.type || 'code', code: c.code || '', output: null, error: null, html: null, image: null, imageS3Key: c.imageS3Key || null, status: 'idle', executionNumber: null, executionTime: null, lastExecutedCode: null, outputVariable: c.outputVariable ?? null })),
      }))
      try {
        localStorage.setItem('microvm-notebooks', JSON.stringify(minimal))
      } catch {}
    }
    }, 1500)
    return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current) }
  }, [tabs])

  // Signature of USER-authored content only. The API-save effect depends on THIS
  // (not the raw `tabs` object, and not the broader persistedSignature), so the 3s
  // debounce + "Saving…" indicator only re-arm on real edits — NOT on VM-launch
  // churn (sessionId/microvmId) or cell runtime output settling during open, which
  // used to keep the toolbar pinned to "Saving…" for the whole slow VM launch even
  // though the actual save PUT is ~5ms. The full content is still written (see
  // buildNotebookSavePayload); this only governs WHEN a save is scheduled.
  // Memoized on the `tabs` identity: the full-workspace JSON.stringify only runs
  // when the tabs array actually changes, not on every unrelated App re-render
  // (VM polling, metrics ticks, modal toggles, theme changes — all of which
  // re-render App but leave `tabs` untouched). Previously this stringify ran in
  // the hook body on EVERY render regardless of cause.
  const userSig = useMemo(() => userContentSignature(tabs), [tabs])
  // Completed cell runs (see runCompletionSignature) — re-arms the same debounce so
  // a run with no edits still persists its runtime fields.
  const runSig = useMemo(() => runCompletionSignature(tabs), [tabs])
  // Tabs with a batch run in progress (Run All / run-from / reactive cascade —
  // the transient `_batchRunning` flag executeBatch sets in useNotebookCells).
  const batchSig = useMemo(() => batchRunningSignature(tabs), [tabs])

  // The API write itself (shared by the debounce and the batch max-wait timer).
  // Reads everything from refs, so a timer armed by an older render still writes
  // the latest tabs. Any save captures the run fields too, so it clears the
  // pending run-driven state (runDirty + the max-wait timer).
  const runApiSave = () => {
    apiSaveTimerRef.current = null
    runDirtyRef.current = false
    if (maxWaitTimerRef.current) { clearTimeout(maxWaitTimerRef.current); maxWaitTimerRef.current = null }
    // NOTE: `image` (base64 PNG) is intentionally NOT persisted (largest field,
    // re-display only, regenerated on re-run) — see buildNotebookSavePayload.
    // Read the latest tabs from the ref so the write captures current content
    // even though the effect is keyed on the (debounced) content signature.
    //
    // ANTI-CLOBBER GUARD (data-loss prevention): only autosave tabs that are
    // SAFE to persist. During a crash/remount the tab list can transiently hold
    // half-constructed or empty tab objects; blindly writing those overwrote the
    // server's real notebook rows with nothing (observed: `notebooks` table went
    // to 0 rows after a render-loop crash). A tab is skipped unless it has a
    // valid id AND carries real content (≥1 cell OR a non-default name). This
    // means autosave can only ever ADD/UPDATE meaningful notebooks — it can never
    // erase a notebook by persisting an empty snapshot of it.
    const isSafeToSave = (tab) => {
      if (!tab || tab.id == null) return false
      const cellCount = (tab._cells || []).length
      const hasName = !!(tab.name && tab.name.trim() && tab.name !== 'Untitled')
      return cellCount > 0 || hasName
    }
    const savable = (tabsRef.current || []).filter(isSafeToSave)
    if (savable.length === 0) {
      // Nothing meaningful to persist (e.g. mid-crash empty tabs). Do NOT write —
      // leave the server's existing notebooks intact — and settle the indicator.
      try { window.dispatchEvent(new CustomEvent('notebook-autosave', { detail: { status: 'saved', at: Date.now() } })) } catch {}
      return
    }
    try { window.dispatchEvent(new CustomEvent('notebook-autosave', { detail: { status: 'saving' } })) } catch {}
    const writes = savable.map(tab =>
      apiSaveNotebook(buildNotebookSavePayload(tab))
        .then(res => ({ tab, res }))
        .catch(() => ({ tab, res: { ok: false } }))  // Non-blocking — localStorage is the safety net
    )
    // Report the aggregate outcome so the toolbar can show "Saved · just now"
    // or a failure hint. All-ok → saved; any failure → error (localStorage
    // still holds the data, so this is a soft warning, not data loss).
    Promise.all(writes).then(entries => {
      applySavedDags(entries)
      // If any save 404-recovered by recreating the row, adopt the new DB id on
      // the matching tab so the NEXT autosave targets the live row (else the tab
      // keeps 404ing forever against the dead id). Keyed by oldId; sessionId is
      // preserved through the recreate so identity/VM binding carries over.
      const recreated = entries.filter(e => e.res && e.res.recreated)
      if (recreated.length > 0) {
        const idMap = new Map(recreated.map(e => [String(e.res.oldId), String(e.res.newId)]))
        setTabs(prev => prev.map(t => {
          const nid = t && idMap.get(String(t.id))
          return nid ? { ...t, id: nid } : t
        }))
        // Keep the ACTIVE tab pointer in sync with the id swap. App resolves the
        // active notebook by id on every render, so if the active tab's id was
        // just remapped (404-recovery), NOT updating activeTabId leaves the open
        // tab "headless" — active-tab lookups fall back to null until the user
        // clicks another tab. Remap it here too.
        setActiveTabId(prev => {
          const nid = prev != null && idMap.get(String(prev))
          return nid || prev
        })
      }
      const ok = entries.every(e => e.res && e.res.ok)
      try {
        window.dispatchEvent(new CustomEvent('notebook-autosave', {
          detail: { status: ok ? 'saved' : 'error', at: Date.now() },
        }))
      } catch {}
    })
  }
  const runApiSaveRef = useRef(runApiSave)
  runApiSaveRef.current = runApiSave

  // Also persist to API (debounced, non-blocking).
  //   • User edits (userSig) always re-arm the 3s debounce — also during a batch.
  //   • Completed runs (runSig) re-arm it only when no batch is running. During a
  //     batch they are HELD: one save is scheduled (debounced) when the batch
  //     ends, plus a max-wait safety save at most every BATCH_MAX_WAIT_MS while
  //     completions keep arriving, so a crash mid-batch loses little.
  //   • Single-cell runs are not batches: unchanged 3s debounce.
  useEffect(() => {
    const prev = lastSigsRef.current
    lastSigsRef.current = { userSig, runSig, batchSig }
    const userChanged = !prev || prev.userSig !== userSig
    const runChanged = !prev || prev.runSig !== runSig
    const batchActive = batchSig !== '[]'
    const batchEnded = !!prev && prev.batchSig !== '[]' && !batchActive
    if (runChanged) runDirtyRef.current = true
    if (!userChanged && !runChanged && !batchEnded) return

    const arm = () => {
      if (apiSaveTimerRef.current) clearTimeout(apiSaveTimerRef.current)
      apiSaveTimerRef.current = setTimeout(() => runApiSaveRef.current(), AUTOSAVE_DEBOUNCE_MS)
    }
    // Signal "unsaved edits pending" the moment persisted content changes, so the
    // toolbar indicator can show a dot before the 3s debounce fires. The initial
    // mount also runs this effect (tabs just loaded), which is harmless — it flips
    // to 'saved' as soon as the debounced write completes.
    if (userChanged || runChanged) {
      try { window.dispatchEvent(new CustomEvent('notebook-autosave', { detail: { status: 'pending' } })) } catch {}
    }
    if (!batchActive) {
      // A batch end with nothing held (and no edit) needs no save.
      if (userChanged || runChanged || runDirtyRef.current) arm()
      return
    }
    // Batch running: an edit saves normally; run completions only (re)start the
    // max-wait clock — they never re-arm or cancel the debounce.
    if (userChanged) arm()
    if (runDirtyRef.current && !maxWaitTimerRef.current) {
      maxWaitTimerRef.current = setTimeout(() => {
        maxWaitTimerRef.current = null
        if (apiSaveTimerRef.current) { clearTimeout(apiSaveTimerRef.current); apiSaveTimerRef.current = null }
        runApiSaveRef.current()
      }, BATCH_MAX_WAIT_MS)
    }
  }, [userSig, runSig, batchSig])

  // Unmount: drop pending timers. Resetting lastSigsRef makes a re-mount (React
  // StrictMode's dev double-invoke) schedule its initial save again.
  useEffect(() => () => {
    if (apiSaveTimerRef.current) { clearTimeout(apiSaveTimerRef.current); apiSaveTimerRef.current = null }
    if (maxWaitTimerRef.current) { clearTimeout(maxWaitTimerRef.current); maxWaitTimerRef.current = null }
    lastSigsRef.current = null
  }, [])

  // Synchronous flush: write ONE notebook's current cells to the API immediately,
  // bypassing the 3s debounce, then emit 'autosave-flushed'. A manual "Save
  // version" awaits this first so the server-side snapshot can't capture pre-edit
  // cells (the version route reads the persisted cells as the source of truth).
  useEffect(() => {
    const onFlush = (e) => {
      const nbId = e.detail?.notebookId
      const tab = (tabsRef.current || []).find(t => t && String(t.id) === String(nbId))
      const done = (ok) => {
        try { window.dispatchEvent(new CustomEvent('autosave-flushed', { detail: { notebookId: nbId, ok } })) } catch {}
      }
      if (!tab) { done(false); return }
      // Same anti-clobber guard as the debounced autosave: never flush a tab with
      // no id and no content, or a version-save could overwrite a real notebook
      // with an empty snapshot.
      const cellCount = (tab._cells || []).length
      const hasName = !!(tab.name && tab.name.trim() && tab.name !== 'Untitled')
      if (tab.id == null || (cellCount === 0 && !hasName)) { done(false); return }
      apiSaveNotebook(buildNotebookSavePayload(tab)).then(res => {
        applySavedDags([{ tab, res }])
        // Adopt a recreated id (404-recovery) so the version snapshot that follows
        // this flush targets the live row, not the dead pre-reset id.
        if (res && res.recreated) {
          setTabs(prev => prev.map(t =>
            t && String(t.id) === String(res.oldId) ? { ...t, id: String(res.newId) } : t
          ))
          // Remap the active pointer too (see the debounced-save path) so the
          // just-flushed tab doesn't go headless after a 404-recovery recreate.
          setActiveTabId(prev =>
            prev != null && String(prev) === String(res.oldId) ? String(res.newId) : prev
          )
        }
        done(!!(res && res.ok))
      }).catch(() => done(false))
    }
    window.addEventListener('flush-autosave', onFlush)
    return () => window.removeEventListener('flush-autosave', onFlush)
  }, [])

  // Background auto-versioning — a SEPARATE interval, not hung off the 3s autosave.
  // Every AUTO_VERSION_INTERVAL_MS it walks the current tabs and, for any whose
  // document changed since its last snapshot, asks the server to take a
  // trigger='auto' version (the server reads the persisted cells itself, so we let
  // autosave land first by simply running on a slow cadence). Unchanged tabs are
  // skipped, so idle notebooks never accrue versions.
  useEffect(() => {
    const timer = setInterval(() => {
      const current = tabsRef.current || []
      // Prune signatures for tabs that are no longer open (closed since the last
      // tick). Without this the ref grows unbounded across a long session, and a
      // stale baseline could suppress the first auto-version after a close+reopen
      // of the same id. Keyed by String(id) to match how we write below.
      const openIds = new Set(current.filter(t => t && t.id != null).map(t => String(t.id)))
      for (const key of Object.keys(lastVersionSigRef.current)) {
        if (!openIds.has(String(key))) delete lastVersionSigRef.current[key]
      }
      for (const key of Object.keys(inFlightVersionSigRef.current)) {
        if (!openIds.has(String(key))) delete inFlightVersionSigRef.current[key]
      }
      current.forEach(tab => {
        if (!tab || tab.id == null) return
        if (!(tab._cells || []).length) return  // skip empty documents
        const sig = documentSignature(tab)
        // Fire an auto-version whenever the document differs from the last one WE
        // snapshotted this session. We intentionally do NOT pre-seed a baseline:
        // a freshly-opened/created notebook SHOULD get an initial auto version so
        // it has restore history, not just notebooks edited across a 5-min boundary.
        // The server DEDUPS against existing versions (verified), so this can never
        // create a duplicate — if the current doc already matches a stored version,
        // no row is written. That makes the client rule simple and correct:
        // "snapshot when changed since our last snapshot; server drops no-ops."
        const key = String(tab.id)
        if (lastVersionSigRef.current[key] === sig) return
        // In-flight guard: mark a SEPARATE ref so we don't fire a second request for
        // the same sig while one is pending — WITHOUT marking it "done". We only set
        // lastVersionSigRef AFTER the request succeeds, so a transient failure
        // doesn't permanently suppress retries (the previous code marked-then-failed
        // and the .catch never reverted it, so one failed request stopped
        // auto-snapshots until the document changed again).
        if (inFlightVersionSigRef.current[key] === sig) return
        inFlightVersionSigRef.current[key] = sig
        const nbId = tab.id
        createNotebookVersion(String(nbId), { trigger: 'auto' }).then(header => {
          // Success — NOW record the snapshotted signature so we don't re-snapshot
          // the same doc, and refresh any open Version-history drawer.
          lastVersionSigRef.current[key] = sig
          if (header) {
            window.dispatchEvent(new CustomEvent('notebook-version-saved', { detail: { notebookId: nbId } }))
          }
        }).catch(() => {
          // Leave lastVersionSigRef unset so the NEXT tick retries this same sig.
        }).finally(() => {
          if (inFlightVersionSigRef.current[key] === sig) delete inFlightVersionSigRef.current[key]
        })
      })
    }, AUTO_VERSION_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // On first mount: load notebooks from the API
  useEffect(() => {
    async function loadFromApi() {
      // (localStorage->API migration removed with the integer-PK refactor: ids are
      // DB-generated now, so there is nothing to migrate. The DB is the source of
      // truth; new notebooks are created via POST /notebooks.)

      // Whether THIS browser has ever had a workspace. The tabs localStorage writer
      // runs on every change and writes even an empty array ("[]"), so:
      //   • key ABSENT  → genuine first run (new user/browser) → seed open tabs from the DB.
      //   • key PRESENT (incl. "[]") → the user has a workspace and may have
      //     intentionally CLOSED everything. Do NOT re-open all DB notebooks, or a
      //     closed notebook would resurrect on refresh (and pop the restore panel).
      // This is the fix for "close a notebook → refresh → it reopens".
      let workspaceEverInitialized = false
      try { workspaceEverInitialized = localStorage.getItem('microvm-notebooks') !== null } catch {}

      // Fetch from API
      const apiNotebooks = await fetchNotebooks()
      if (apiNotebooks && apiNotebooks.length > 0) {
        if (tabs.length === 0 && !workspaceEverInitialized) {
          // FIRST RUN only: API has notebooks but this browser has no workspace yet —
          // seed the open tabs from the DB so a new user isn't staring at an empty app.
          const loaded = apiNotebooks.map(nb => ({
            id: String(nb.id),  // canonical: tab.id is ALWAYS a string
            name: nb.name,
            description: nb.description || '',
            tag: nb.tag || 'Drafts',
            _cells: nb.cells || [],
            microvmEndpoint: null,
            microvmId: nb.microvm_id || null,
            status: 'disconnected',
            mode: null,
            sessionId: nb.session_id || null,
            checkpointEnabled: nb.checkpoint_enabled || false,
          }))
          setTabs(loaded)
          // NOTE: we deliberately do NOT seed auto-version signatures on load.
          // Seeding would suppress the FIRST auto version for a loaded notebook,
          // so a notebook you open and don't edit would accrue no history. Letting
          // the first interval tick fire an auto (server dedups if a matching
          // version already exists) guarantees every content-bearing notebook has
          // at least a baseline restore point.
          if (loaded.length > 0 && !activeTabId) {
            setActiveTabId(loaded[0].id)
          }
        }
        // (No image enrichment on load: base64 images are never persisted — not in
        // the DB, the PUT payload or localStorage. A chart cell keeps only its
        // `imageS3Key`; PersistedCellImage fetches the PNG lazily through the
        // authenticated proxy route when the cell renders.)

        // Load chat messages from DB for each notebook (non-blocking)
        apiNotebooks.forEach(async (nb) => {
          const tabId = String(nb.id)
          const sessionId = nb.session_id
          if (sessionId) {
            const msgs = await loadChatMessages(sessionId)
            if (msgs && msgs.length > 0) {
              setTabs(prev => prev.map(t => t.id === tabId ? { ...t, _chatMessages: msgs } : t))
            }
          }
        })
      }
    }
    loadFromApi()
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // Persist the active tab id
  useEffect(() => {
    localStorage.setItem('microvm-active-tab', JSON.stringify(activeTabId))
  }, [activeTabId])
}
