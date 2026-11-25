/**
 * Notebook API client — CRUD operations on notebooks stored by the proxy (SQLite or
 * Postgres). The localStorage tab cache lives in hooks/useTabsPersistence.js.
 */

import { apiFetch } from './apiFetch'
/**
 * Fetch all notebooks from the API.
 */
export async function fetchNotebooks() {
  try {
    const resp = await apiFetch(`/notebooks`)
    if (resp.ok) {
      const data = await resp.json()
      return data.notebooks || []
    }
  } catch {
    // Proxy not available — the caller shows an empty/unavailable state
  }
  return null  // null = API unavailable
}

/**
 * Fetch a single notebook (with its cells) from the API — used to reopen a
 * closed notebook from the DB into a tab. Returns the notebook object
 * ({id, name, description, tag, cells, ...}) or null.
 */
export async function fetchNotebook(notebookId) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}`)
    if (resp.ok) return await resp.json()
  } catch {}
  return null
}

/**
 * The notebook's stored dependency DAG document (GET /notebooks/{id}/dag), or
 * null when unavailable. The proxy awaits a pending VM attach before answering.
 */
export async function fetchNotebookDag(notebookId) {
  if (notebookId == null) return null
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}/dag`)
    if (resp.ok) return await resp.json()
  } catch {}
  return null
}

/**
 * Save/update a notebook to the API.
 *
 * Return shape (always an object so callers can react to a recreate):
 *   { ok: true, ...body }                         — normal PUT success; `body` is the
 *                                                    parsed response ({success, dag})
 *   { ok: true, recreated: true, oldId, newId, dag } — the row was gone (DB reset /
 *                                                    rotation) so we POST-recreated
 *                                                    it and adopted a new id
 *   { ok: false }                                 — failed (localStorage is the net)
 *
 * 404-RECOVERY (survive a DB reset): the update handler is update-only — a
 * `PUT /notebooks/{id}` for an id that no longer exists returns 404 (this is by
 * design, not a bug). That happens when the SQLite DB is reset/rotated out from
 * under an open tab (e.g. an `--AuroraReset` teardown): the tab still holds the
 * pre-reset integer id, so every autosave 404s and even the Baseline version
 * never gets created. Instead of silently failing ("Save failed — kept locally"),
 * we transparently RE-CREATE the notebook via POST, preserving its stable
 * session_id (and cells/name/tag), and hand the new DB id back to the caller so
 * the open tab can adopt it and keep saving.
 */
export async function saveNotebook(notebook) {
  try {
    const resp = await apiFetch(`/notebooks/${notebook.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: notebook.name,
        description: notebook.description || '',
        tag: notebook.tag || 'Drafts',
        cells: notebook.cells || [],
        session_id: notebook.session_id || null,
        microvm_id: notebook.microvm_id || null,
        checkpoint_enabled: notebook.checkpoint_enabled ? 1 : 0,
      }),
    })
    if (resp.ok) {
      const body = await resp.json().catch(() => ({}))
      return { ...(body && typeof body === 'object' ? body : {}), ok: true }
    }
    if (resp.status === 404) {
      // The row is gone — recreate it and adopt the new id (see doc above).
      const created = await createNotebook({
        name: notebook.name,
        description: notebook.description || '',
        tag: notebook.tag || 'Drafts',
        cells: notebook.cells || [],
        session_id: notebook.session_id || null,
      })
      if (created && created.id != null) {
        return { ok: true, recreated: true, oldId: notebook.id, newId: created.id, dag: created.dag || null }
      }
    }
    return { ok: false }
  } catch {
    return { ok: false }
  }
}

/**
 * Create a new notebook via the API and RETURN the created row — crucially
 * including the DB-generated integer `id` (create-returns-id / Option A). The
 * client no longer mints ids: it POSTs name/tag/cells (+ its stable session_id)
 * and adopts the returned id as the notebook's permanent key. Returns the created
 * notebook object ({id, name, ...}) on success, or null on failure.
 */
export async function createNotebook(notebook) {
  try {
    const resp = await apiFetch(`/notebooks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        // NO id — the DB assigns it. session_id is the stable client UUID handle.
        name: notebook.name,
        description: notebook.description || '',
        tag: notebook.tag || 'Drafts',
        cells: notebook.cells || [],
        session_id: notebook.session_id || notebook.sessionId || null,
      }),
    })
    if (resp.ok) return await resp.json()
  } catch {}
  return null
}

/**
 * Delete a notebook from the API.
 */
export async function deleteNotebook(notebookId) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}`, { method: 'DELETE' })
    return resp.ok
  } catch {
    return false
  }
}

/**
 * Explicitly checkpoint a session's VM state to S3 (no terminate). Called on
 * notebook DELETE (before terminating the VM) so the LATEST in-memory state
 * (variables, /tmp files, packages) is guaranteed saved first — the proxy
 * resumes a suspended VM if needed. (Close no longer calls this: it SUSPENDS the
 * VM, which preserves in-memory state in the suspend snapshot for reopen.)
 * Bounded by a timeout so the action never hangs on a slow/dead VM; on
 * timeout/failure we resolve falsy and let the caller proceed to terminate
 * (which itself also checkpoints server-side as a fallback).
 */
export async function checkpointSession(sessionId, { timeoutMs = 30000 } = {}) {
  if (!sessionId) return false
  const ctrl = new AbortController()
  const t = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const resp = await apiFetch(`/checkpoint-save`, {
      method: 'POST',
      headers: { 'X-Session-Id': sessionId },
      signal: ctrl.signal,
    })
    if (!resp.ok) return false
    const data = await resp.json().catch(() => ({}))
    return !!data.success
  } catch {
    return false  // aborted/timed out/network — caller still terminates
  } finally {
    clearTimeout(t)
  }
}

// NOTE: migrateFromLocalStorage() was removed with the integer-PK refactor. It
// imported old client-minted string-id notebooks from localStorage into the API,
// but ids are now DB-generated integers (the DB owns id assignment), so there is
// no meaningful id to preserve on migration. New notebooks are created via POST
// /notebooks (create-returns-id); the workspace starts fresh.

// ============================================================
// NOTEBOOK VERSIONS — document history (snapshots of cells)
// ============================================================
// These version the DOCUMENT only. The server reads the CURRENT cells itself on
// create/restore, so the client just names the checkpoint + picks a trigger. The
// 3s autosave stays the untouched working copy.

/**
 * Create a version snapshot of a notebook's current cells.
 * trigger: 'manual' (default) | 'auto' | 'pre_ai' | 'pre_restore'.
 * Returns the version header { version_group_id, ... } or null on failure.
 */
export async function createNotebookVersion(notebookId, { label = null, trigger = 'manual' } = {}) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}/versions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, trigger }),
    })
    if (resp.ok) return await resp.json()
  } catch {}
  return null
}

/**
 * List a notebook's version headers, most recent first (no cell rows).
 */
export async function listNotebookVersions(notebookId, limit = 50) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}/versions?limit=${limit}`)
    if (resp.ok) {
      const data = await resp.json()
      return data.versions || []
    }
  } catch {}
  return []
}

/**
 * Get one version (header + its cells, ordered + restore-shaped).
 */
export async function getNotebookVersion(notebookId, versionGroupId) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}/versions/${versionGroupId}`)
    if (resp.ok) return await resp.json()
  } catch {}
  return null
}

/**
 * Restore a version onto the live notebook. The server snapshots current cells as
 * a 'pre_restore' version first, then overwrites. Returns { success, cells, ... }.
 */
export async function restoreNotebookVersion(notebookId, versionGroupId) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}/versions/${versionGroupId}/restore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    if (resp.ok) return await resp.json()
  } catch {}
  return null
}

// One 'pre_ai' checkpoint per burst: AI actions (Apply Fix, generate-into-cell,
// multi-cell agent apply) can fire several cell mutations back-to-back. We only
// want ONE restore point in front of the whole burst, so we suppress repeats for
// a short window per notebook. The FIRST call in a burst takes the snapshot
// immediately (before edits land); subsequent calls within the window are no-ops.
const _preAiRecent = new Map()  // notebookId -> last snapshot epoch ms
const PRE_AI_WINDOW_MS = 30 * 1000

/**
 * Take a 'pre_ai' version right before an AI edit, at most once per PRE_AI_WINDOW_MS
 * per notebook. Fire-and-forget; safe to call on every AI mutation. Returns the
 * header if a snapshot was taken, or null if suppressed / failed.
 */
export async function checkpointBeforeAI(notebookId, label = 'Before AI edit') {
  if (notebookId == null) return null
  const id = String(notebookId)
  const now = Date.now()
  const last = _preAiRecent.get(id) || 0
  if (now - last < PRE_AI_WINDOW_MS) return null  // already checkpointed this burst
  _preAiRecent.set(id, now)
  const header = await createNotebookVersion(id, { label, trigger: 'pre_ai' })
  if (header) {
    try {
      window.dispatchEvent(new CustomEvent('notebook-version-saved', { detail: { notebookId: id } }))
    } catch {}
  }
  return header
}

/**
 * Delete one version (any trigger) and its cell rows. Returns true on success.
 */
export async function deleteNotebookVersion(notebookId, versionGroupId) {
  try {
    const resp = await apiFetch(`/notebooks/${notebookId}/versions/${versionGroupId}`, {
      method: 'DELETE',
    })
    return resp.ok
  } catch {
    return false
  }
}

/**
 * Load AI chat messages for a session from the API.
 */
export async function loadChatMessages(sessionId) {
  if (!sessionId) return []
  try {
    const resp = await apiFetch(`/ai/chat/${sessionId}/messages`)
    if (resp.ok) {
      const data = await resp.json()
      return data.messages || []
    }
  } catch {}
  return []
}

// NOTE: chat messages are no longer saved from the frontend. The chat agent
// (via its Strands SessionManager) is the writer of record — it persists every
// message server-side as the conversation happens. The frontend is a pure reader
// (loadChatMessages above / GET /ai/chat/{id}/messages). The old saveChatMessages
// PUT was removed to avoid a redundant, drift-prone second write path.
