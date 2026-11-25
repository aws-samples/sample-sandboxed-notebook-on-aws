import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { IconX, IconPlus, IconPencil, IconTrash, IconSave, IconRefresh, IconCode, IconUpload } from '../Icons'
import PanelExpandButton from './PanelExpandButton'
import { PROXY_URL } from '../../config'
import { fetchWithTimeout } from '../../services/fetchWithTimeout'
import { isSessionVmAlive } from '../../constants'

/**
 * EnvPanel — "Secrets & Env": one home for a notebook's environment configuration.
 *
 * Two sections, both notebook-scoped and persisted in notebook_env (survives
 * relaunch, re-injected by the /run hook):
 *   • SECRETS  — references to AWS Secrets Manager secrets (name → target env var,
 *                optional JSON key). VALUES are never shown or stored — only refs.
 *                A refresh action re-resolves rotated values onto the running VM.
 *   • ENV VARS — plain key/value pairs. Values are WRITE-ONLY (mirroring secrets):
 *                the GET returns only presence markers {set:bool}, never the stored
 *                value, so the UI shows a set/not-set indicator and NEVER reveals a
 *                value. Editing a var starts from an EMPTY field and applies the new
 *                value live to the running VM (/proxy/set-env) AND persists it.
 *
 * The persisted env is ONE object {env_vars:{}, secrets:[]}. The server now MERGES
 * env vars (only the keys in a PUT body are updated; a top-level `remove` array
 * deletes keys) and whole-object-replaces secrets — so a mutation only ever sends
 * the REAL values the user just typed, never the presence markers. Live actions
 * (set-env, refresh-secrets) require a running VM — disabled with a hint otherwise;
 * the persisted config is still fully editable offline.
 */
// Build a one-liner that reads an env var / secret value inside a cell. Both plain
// env vars and Secrets-Manager-backed secrets land in os.environ, so the same
// os.environ.get(...) snippet works for either — keyed by the target env var name.
// Build a one-liner that reads an env var / secret value inside a cell. Both plain
// env vars and Secrets-Manager-backed secrets land in os.environ, so we key off the
// target env var name. When `isJson` is true (a secret attached as its WHOLE value —
// no single key selected), the value is a JSON string, so the snippet parses it and
// shows how to pull a field; otherwise it's a single string value read directly.
function envSnippet(envVar, maybeJson = false) {
  // Local var name: lowercase + non-identifier chars → underscores (e.g. "DB-KEY"
  // → db_key) so the snippet is always valid Python. Fall back to "value".
  let local = String(envVar).toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^_+/, '')
  if (!local || !/^[a-z_]/.test(local)) local = 'value'
  const q = JSON.stringify(envVar)
  // Use the pre-loaded helpers — no boilerplate. get_secret/get_env read from
  // os.environ (where both secrets and env vars land) and handle JSON-vs-plaintext
  // automatically: a JSON value comes back as a dict/list, a plain value as the
  // string (no manual json.loads, no JSONDecodeError on a plaintext secret).
  if (maybeJson) {
    // Whole-value secret → get_secret (reads best for a Secrets-Manager secret).
    return `# ${local} is a dict if the secret is JSON, else the plain string\n${local} = get_secret(${q})\nprint(${q}, "→", type(${local}).__name__ if ${local} is not None else "✗ not set")`
  }
  // Plain env var → get_env.
  return `${local} = get_env(${q})\nprint(${q}, "→", ${local})`
}

// A valid POSIX-ish env var name: letters/digits/underscore, not starting with a
// digit. We validate parsed keys against this so a malformed .env line can't inject
// a bogus name.
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/

// Parse a .env file body into { vars, errors }.
//   vars   — { KEY: value } for every valid line, in file order (later wins).
//   errors — [{ line, text, reason }] for lines that were skipped.
// Handles: comments (# ...), blank lines, `export KEY=val`, quoted values
// ('single' / "double", with \n \t \r \\ unescaping inside double quotes),
// inline comments after an UNQUOTED value, and surrounding whitespace. Values may
// be empty (KEY=). A line with no '=' or an invalid key is reported as an error.
function parseDotenv(text) {
  const vars = {}
  const errors = []
  const lines = String(text || '').split(/\r?\n/)
  lines.forEach((raw, i) => {
    const lineNo = i + 1
    let line = raw.trim()
    if (!line || line.startsWith('#')) return  // blank / comment — skip silently
    if (line.startsWith('export ')) line = line.slice(7).trim()
    const eq = line.indexOf('=')
    if (eq === -1) {
      errors.push({ line: lineNo, text: raw.trim().slice(0, 60), reason: 'no "=" — not a KEY=VALUE line' })
      return
    }
    const key = line.slice(0, eq).trim()
    let value = line.slice(eq + 1).trim()
    if (!ENV_KEY_RE.test(key)) {
      errors.push({ line: lineNo, text: key.slice(0, 40), reason: 'invalid key (use letters, digits, underscore; not starting with a digit)' })
      return
    }
    if (value.length >= 2 && ((value[0] === '"' && value.endsWith('"')) || (value[0] === "'" && value.endsWith("'")))) {
      const quote = value[0]
      value = value.slice(1, -1)
      // Only double quotes interpret escapes (POSIX/dotenv convention).
      if (quote === '"') {
        value = value.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\r/g, '\r').replace(/\\"/g, '"').replace(/\\\\/g, '\\')
      }
    } else {
      // Unquoted: strip an inline comment ( value # comment ).
      const hash = value.indexOf(' #')
      if (hash !== -1) value = value.slice(0, hash).trim()
    }
    vars[key] = value
  })
  return { vars, errors }
}

export default function EnvPanel({ activeTab, instances = {}, onInsertCode, onClose, expanded: panelExpanded, onToggleExpand }) {
  const notebookId = activeTab?.id
  const sessionId = activeTab?.sessionId
  const vmAlive = isSessionVmAlive(instances, sessionId)

  // Persisted env for this notebook. env_vars are PRESENCE MARKERS from the GET:
  // { KEY: {set: bool} } — never the plaintext value (values are write-only).
  const [envVars, setEnvVars] = useState({})           // {KEY: {set: bool}}
  const [secrets, setSecrets] = useState([])           // [{name, arn, envVar, secretKey?}]
  const [error, setError] = useState('')

  // Available Secrets Manager secrets for the attach dropdown (names/ARNs only).
  const [available, setAvailable] = useState([])       // [{name, arn, description}]
  // Keys of the currently-selected secret in the attach form (JSON secrets only).
  const [secretKeys, setSecretKeys] = useState([])

  // Free-text filter across both sections (matches other panels' search).
  const [search, setSearch] = useState('')

  // Inline forms: null = closed. Editors carry the in-progress fields.
  const [envEditor, setEnvEditor] = useState(null)     // {key, value, isNew}
  const [secretEditor, setSecretEditor] = useState(null) // {arn, name, secretKey, envVar}
  const [refreshing, setRefreshing] = useState(false)
  const [refreshStatus, setRefreshStatus] = useState(null) // null | {kind:'ok'|'warn', text}
  const [busy, setBusy] = useState(false)
  // Hidden <input type=file> for the ".env" bulk import + a transient result notice.
  const dotenvInputRef = useRef(null)
  const [importStatus, setImportStatus] = useState(null) // null | {kind:'ok'|'warn'|'error', text}

  // ─── Load the persisted env for this notebook ────────────────────────────
  const loadEnv = useCallback(async () => {
    if (notebookId == null) return
    setError('')
    try {
      const r = await fetchWithTimeout(`${PROXY_URL}/notebooks/${notebookId}/env`)
      if (r.ok) {
        const d = await r.json()
        setEnvVars(d.env_vars || {})
        setSecrets(Array.isArray(d.secrets) ? d.secrets : [])
      } else {
        setError(`Couldn't load environment (HTTP ${r.status})`)
      }
    } catch (e) {
      setError(e.message || 'Failed to load environment')
    }
  }, [notebookId])

  useEffect(() => { loadEnv() }, [loadEnv])

  // Available secrets (needs AWS, not a VM) — the attach-dropdown options.
  const loadAvailable = useCallback(async () => {
    try {
      const r = await fetchWithTimeout(`${PROXY_URL}/secrets`)
      const d = r.ok ? await r.json() : { secrets: [] }
      setAvailable(Array.isArray(d.secrets) ? d.secrets : [])
    } catch { /* offline — attach dropdown just shows nothing */ }
  }, [])

  useEffect(() => { loadAvailable() }, [loadAvailable])

  // Header refresh — re-fetch the persisted env blob AND the available-secrets
  // list. Spins the icon while in flight; a data change flows in via the setters.
  const [reloading, setReloading] = useState(false)
  const refreshAll = useCallback(async () => {
    setReloading(true)
    try { await Promise.all([loadEnv(), loadAvailable()]) }
    finally { setReloading(false) }
  }, [loadEnv, loadAvailable])

  // ─── Persist helper ───────────────────────────────────────────────────────
  // The server MERGES env vars and whole-object-replaces secrets, so `envPatch`
  // carries ONLY the real values the user just entered (never the presence markers)
  // and `remove` lists keys to delete. A secret-only edit sends env_vars:{} (a
  // no-op merge) so it can't accidentally re-write env values as {set:true}.
  const persist = useCallback(async (envPatch, nextSecrets, remove = []) => {
    if (notebookId == null) return false
    try {
      const r = await fetchWithTimeout(`${PROXY_URL}/notebooks/${notebookId}/env`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ env_vars: envPatch, secrets: nextSecrets, remove }),
      })
      return r.ok
    } catch {
      return false
    }
  }, [notebookId])

  // ─── Live-inject one env var onto the running VM (no relaunch) ────────────
  // The proxy's /proxy/set-env DETERMINISTICALLY resumes a suspended VM and waits
  // for RUNNING before injecting (up to ~30s), so we give this a longer timeout
  // than the 30s default to outlast that wake. Still best-effort: the value is also
  // persisted (sealed) and re-injected on the next launch/restore regardless.
  const liveSetEnv = useCallback(async (key, value) => {
    if (!vmAlive || !sessionId) return
    try {
      await fetchWithTimeout(`${PROXY_URL}/proxy/set-env`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': sessionId },
        body: JSON.stringify({ key, value }),
        timeout: 45000,   // > server-side resume budget (~30s) + inject
      })
    } catch { /* best-effort; persisted value still re-injects on relaunch */ }
  }, [vmAlive, sessionId])

  // ─── ENV VAR mutations ────────────────────────────────────────────────────
  const saveEnvVar = useCallback(async () => {
    const key = (envEditor?.key || '').trim()
    const value = envEditor?.value ?? ''
    if (!key) return
    setBusy(true)
    // Optimistic: the row flips to set:true (we never hold the plaintext in state).
    setEnvVars(prev => ({ ...prev, [key]: { set: true } }))
    setEnvEditor(null)
    // MERGE: send only this one real value; other vars are left intact server-side.
    await persist({ [key]: value }, secrets)
    await liveSetEnv(key, value)           // apply to running VM if any
    setBusy(false)
  }, [envEditor, secrets, persist, liveSetEnv])

  const deleteEnvVar = useCallback(async (key) => {
    setBusy(true)
    setEnvVars(prev => { const next = { ...prev }; delete next[key]; return next })
    // Delete via the server's `remove` list (merge PUT with empty env patch).
    await persist({}, secrets, [key])
    // Note: we can't UNSET an env var on a running VM (os.environ persists for the
    // session); the deletion takes full effect on the next relaunch. Persisted blob
    // no longer carries it, so it won't be re-injected.
    setBusy(false)
  }, [secrets, persist])

  // ─── Bulk import from a .env file ─────────────────────────────────────────
  // Reads a chosen .env file, parses KEY=VALUE lines (comments/quotes/export
  // handled), VALIDATES keys, merges the valid ones into env vars (imported values
  // WIN over existing keys), persists ONCE, and live-injects each onto the running
  // VM. Invalid/non-KEY=VALUE lines are skipped and surfaced in a result notice.
  const importDotenv = useCallback(async (file) => {
    if (!file) return
    setImportStatus(null)
    let text = ''
    try {
      text = await file.text()
    } catch {
      setImportStatus({ kind: 'error', text: 'Could not read the file.' })
      return
    }
    const { vars, errors } = parseDotenv(text)
    const keys = Object.keys(vars)
    if (keys.length === 0) {
      setImportStatus({
        kind: 'warn',
        text: errors.length
          ? `No valid KEY=VALUE lines found (${errors.length} line${errors.length === 1 ? '' : 's'} skipped).`
          : 'No env vars found in the file.',
      })
      return
    }
    setBusy(true)
    const overwritten = keys.filter(k => k in envVars).length  // capture before optimistic update
    // Optimistic: mark every imported key as set:true (we don't keep plaintext).
    setEnvVars(prev => ({ ...prev, ...Object.fromEntries(keys.map(k => [k, { set: true }])) }))
    // MERGE: send only the imported REAL values; untouched vars survive server-side.
    const ok = await persist(vars, secrets)
    // Live-inject each imported var onto the running VM (best-effort, in parallel).
    if (vmAlive && sessionId) {
      await Promise.all(keys.map(k => liveSetEnv(k, vars[k])))
    }
    setBusy(false)
    // Result notice: how many imported, how many overwrote, how many skipped.
    const added = keys.length - overwritten
    const parts = []
    if (added) parts.push(`${added} added`)
    if (overwritten) parts.push(`${overwritten} updated`)
    if (errors.length) parts.push(`${errors.length} skipped`)
    setImportStatus({
      kind: ok ? (errors.length ? 'warn' : 'ok') : 'error',
      text: ok
        ? `Imported ${keys.length} env var${keys.length === 1 ? '' : 's'} (${parts.join(', ')}).`
          + (errors.length ? ` Skipped: ${errors.slice(0, 3).map(e => `line ${e.line}`).join(', ')}${errors.length > 3 ? '…' : ''}.` : '')
        : 'Imported to this session but saving failed — retry, or the values will be lost on relaunch.',
    })
  }, [envVars, secrets, persist, liveSetEnv, vmAlive, sessionId])

  const onDotenvFilePicked = useCallback((e) => {
    const file = e.target.files && e.target.files[0]
    e.target.value = ''  // reset so re-picking the same file fires change again
    if (file) importDotenv(file)
  }, [importDotenv])

  // ─── SECRET mutations ──────────────────────────────────────────────────────
  const openSecretEditor = useCallback((existing) => {
    setSecretEditor(existing || { arn: '', name: '', secretKey: '', envVar: '' })
    setSecretKeys([])
    // If editing an existing ref with an ARN, load its keys for the dropdown.
    if (existing?.arn) loadSecretKeys(existing.arn)
  }, [])

  const loadSecretKeys = useCallback((arn) => {
    if (!arn) { setSecretKeys([]); return }
    fetchWithTimeout(`${PROXY_URL}/secrets/keys?secret_id=${encodeURIComponent(arn)}`)
      .then(r => (r.ok ? r.json() : { keys: [] }))
      .then(d => setSecretKeys(Array.isArray(d.keys) ? d.keys : []))
      .catch(() => setSecretKeys([]))
  }, [])

  const saveSecret = useCallback(async () => {
    const e = secretEditor
    if (!e || !e.arn || !(e.envVar || '').trim()) return
    setBusy(true)
    const ref = {
      name: e.name || e.arn.split(':').slice(-1)[0],
      arn: e.arn,
      envVar: e.envVar.trim(),
      ...(e.secretKey ? { secretKey: e.secretKey } : {}),
    }
    // Replace an existing ref targeting the same envVar, else append.
    const next = (() => {
      const idx = secrets.findIndex(s => s.envVar === ref.envVar)
      if (idx >= 0) { const c = [...secrets]; c[idx] = ref; return c }
      return [...secrets, ref]
    })()
    setSecrets(next)
    setSecretEditor(null)
    // Secret-only edit: empty env patch (no-op merge) so env values aren't touched.
    await persist({}, next)
    setBusy(false)
  }, [secretEditor, secrets, persist])

  const detachSecret = useCallback(async (envVar) => {
    setBusy(true)
    const next = secrets.filter(s => s.envVar !== envVar)
    setSecrets(next)
    await persist({}, next)
    setBusy(false)
  }, [secrets, persist])

  // ─── Refresh secret values on the running VM ──────────────────────────────
  // Re-resolves the secret(s) from Secrets Manager into the VM's os.environ (picks
  // up rotations). Succeeds SILENTLY server-side, so we surface a transient status
  // on the button — otherwise a working refresh looks like it did nothing.
  const refreshSecrets = useCallback(async (only = null) => {
    if (!vmAlive || !sessionId) return
    const payload = only ? [only] : secrets
    if (payload.length === 0) return
    setRefreshing(true)
    setRefreshStatus(null)
    try {
      const r = await fetchWithTimeout(`${PROXY_URL}/proxy/refresh-secrets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': sessionId },
        body: JSON.stringify({ secrets: payload }),
      })
      const d = r.ok ? await r.json() : null
      if (d && d.success) {
        const n = (d.injected || []).length
        const failed = (d.failed || []).length
        setRefreshStatus(failed
          ? { kind: 'warn', text: `Refreshed ${n}, ${failed} failed` }
          : { kind: 'ok', text: `Refreshed ${n} secret${n === 1 ? '' : 's'} — re-run a cell to use` })
      } else {
        setRefreshStatus({ kind: 'warn', text: `Refresh failed (HTTP ${r.status})` })
      }
    } catch (e) {
      setRefreshStatus({ kind: 'warn', text: e.message || 'Refresh failed' })
    } finally {
      setRefreshing(false)
      // Fade the status out after a few seconds.
      setTimeout(() => setRefreshStatus(null), 4000)
    }
  }, [vmAlive, sessionId, secrets])

  const envEntries = useMemo(() => Object.entries(envVars), [envVars])

  // Apply the free-text filter — match ONLY on the entry's NAME (the bold
  // identifier): a secret's target env var, or an env var's key. Deliberately NOT
  // the secret's source path (e.g. "microvm/proxy-db/readonly") or JSON key — those
  // are secondary details and matching them is surprising (typing "x" hitting the
  // "x" in "proxy"). Env var values are masked, so they're never matched either.
  const q = search.trim().toLowerCase()
  const filteredSecrets = useMemo(() => {
    if (!q) return secrets
    return secrets.filter(s => (s.envVar || '').toLowerCase().includes(q))
  }, [secrets, q])
  const filteredEnvEntries = useMemo(() => {
    if (!q) return envEntries
    return envEntries.filter(([key]) => key.toLowerCase().includes(q))
  }, [envEntries, q])

  if (notebookId == null) {
    return (
      <div className="sidebar-panel-content">
        <div className="sidebar-panel-header">
          <span className="sidebar-panel-title">Secrets &amp; Env</span>
          <PanelExpandButton expanded={panelExpanded} onToggleExpand={onToggleExpand} />
          <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
        </div>
        <div className="sidebar-panel-hint" style={{ padding: 'var(--space-3)', fontSize: 'var(--font-size-sm)', color: 'var(--text-muted)' }}>
          Open a notebook to manage its secrets and environment variables.
        </div>
      </div>
    )
  }

  return (
    <div className="sidebar-panel-content env-panel">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Secrets &amp; Env</span>
        <span className="sidebar-panel-count">{secrets.length} secret{secrets.length === 1 ? '' : 's'} · {envEntries.length} var{envEntries.length === 1 ? '' : 's'}</span>
        <button className="sidebar-panel-action" onClick={refreshAll} disabled={reloading} title="Refresh">
          <IconRefresh width={14} height={14} className={reloading ? 'env-spin' : ''} />
        </button>
        <PanelExpandButton expanded={panelExpanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      <div className="sidebar-panel-hint" style={{ padding: '6px var(--space-3)', fontSize: 'var(--font-size-xs)', color: 'var(--text-muted)' }}>
        Injected into <code>os.environ</code> at launch · persists per notebook.
        {!vmAlive && <span className="env-vm-note"> Live changes apply on next launch (no running VM).</span>}
      </div>

      {(secrets.length > 0 || envEntries.length > 0) && (
        <div className="outline-search">
          <input
            className="outline-search-input"
            type="text"
            placeholder="Search secrets & env vars..."
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
      )}

      {error && <div className="sidebar-panel-hint env-error">{error}</div>}

      <div className="sidebar-panel-body">
        {/* ══════════════ SECRETS ══════════════ */}
        <div className="env-section-header env-section-secrets">
          <span>🔑 SECRETS (Secrets Manager)</span>
          <span className="env-section-count">{secrets.length}</span>
        </div>

        {secrets.length > 0 && (
          <>
            <button
              className="env-refresh-all"
              onClick={() => refreshSecrets()}
              disabled={!vmAlive || refreshing}
              title={vmAlive ? 'Re-resolve all secret values onto the running VM (picks up rotations)' : 'Requires a running VM'}
            >
              <IconRefresh width={12} height={12} className={refreshing ? 'env-spin' : ''} />
              {refreshing ? 'Refreshing…' : 'Refresh secret values on running VM'}
            </button>
            {refreshStatus && (
              <div className={`env-refresh-status env-refresh-${refreshStatus.kind}`}>
                {refreshStatus.kind === 'ok' ? '✓' : '⚠'} {refreshStatus.text}
              </div>
            )}
          </>
        )}

        {secrets.length > 0 && filteredSecrets.length === 0 && (
          <div className="env-empty-match">No secrets match “{search}”</div>
        )}

        {filteredSecrets.map(s => (
          <div key={s.envVar} className="env-row">
            <div className="env-row-text">
              <div className="env-row-name">{s.envVar}</div>
              <div className="env-row-sub">← {s.name || s.arn}{s.secretKey ? ` [${s.secretKey}]` : ''}</div>
            </div>
            <div className="env-row-actions">
              {onInsertCode && (
                <button className="env-row-btn env-row-btn-insert" onClick={() => onInsertCode(envSnippet(s.envVar, !s.secretKey), 'code')} title={`Insert code that reads ${s.envVar}${!s.secretKey ? ' (whole-value secret — handles JSON or plaintext)' : ''}`}><IconCode width={11} height={11} /></button>
              )}
              <button className="env-row-btn" onClick={() => openSecretEditor(s)} title="Edit mapping"><IconPencil width={11} height={11} /></button>
              <button className="env-row-btn" onClick={() => refreshSecrets(s)} disabled={!vmAlive || refreshing} title={vmAlive ? 'Refresh this secret on the running VM' : 'Requires a running VM'}><IconRefresh width={11} height={11} /></button>
              <button className="env-row-btn env-row-btn-danger" onClick={() => detachSecret(s.envVar)} title="Detach"><IconTrash width={11} height={11} /></button>
            </div>
          </div>
        ))}

        {!secretEditor && (
          <button className="env-add-btn env-add-secret" onClick={() => openSecretEditor(null)} disabled={busy}>
            <IconPlus width={11} height={11} /> Attach a secret
          </button>
        )}

        {secretEditor && (
          <div className="env-editor">
            <label className="env-editor-label">Secret</label>
            <select
              className="env-editor-input"
              value={secretEditor.arn}
              onChange={(e) => {
                const arn = e.target.value
                const sel = available.find(a => a.arn === arn)
                setSecretEditor(ed => ({ ...ed, arn, name: sel?.name || '', secretKey: '' }))
                loadSecretKeys(arn)
              }}
            >
              <option value="">Select a secret…</option>
              {available.map(a => <option key={a.arn} value={a.arn}>{a.name}</option>)}
            </select>

            {secretKeys.length > 0 && (
              <>
                <label className="env-editor-label">Key (JSON secret)</label>
                <select
                  className="env-editor-input"
                  value={secretEditor.secretKey || ''}
                  onChange={(e) => setSecretEditor(ed => ({ ...ed, secretKey: e.target.value }))}
                >
                  <option value="">(whole value)</option>
                  {secretKeys.map(k => <option key={k} value={k}>{k}</option>)}
                </select>
              </>
            )}

            <label className="env-editor-label">→ Env var name</label>
            <input
              className="env-editor-input"
              type="text"
              placeholder="MY_API_KEY"
              value={secretEditor.envVar}
              onChange={(e) => setSecretEditor(ed => ({ ...ed, envVar: e.target.value }))}
              spellCheck={false}
            />
            <div className="env-editor-actions">
              <button className="env-editor-cancel" onClick={() => setSecretEditor(null)}>Cancel</button>
              <button className="env-editor-save" onClick={saveSecret} disabled={!secretEditor.arn || !(secretEditor.envVar || '').trim()}>
                <IconSave width={11} height={11} /> Attach
              </button>
            </div>
          </div>
        )}

        {/* ══════════════ ENV VARS ══════════════ */}
        <div className="env-section-header env-section-env">
          <span>⚙ ENV VARS</span>
          <span className="env-section-count">{envEntries.length}</span>
        </div>

        {envEntries.length > 0 && filteredEnvEntries.length === 0 && (
          <div className="env-empty-match">No env vars match “{search}”</div>
        )}

        {filteredEnvEntries.map(([key, marker]) => (
          <div key={key} className="env-row">
            <div className="env-row-text">
              <div className="env-row-name">{key}</div>
              {/* Values are write-only — show a set/not-set indicator, never the value. */}
              <div className="env-row-sub">{marker && marker.set ? '•••••• set (value hidden)' : 'not set'}</div>
            </div>
            <div className="env-row-actions">
              {onInsertCode && (
                <button className="env-row-btn env-row-btn-insert" onClick={() => onInsertCode(envSnippet(key), 'code')} title={`Insert code that reads ${key}`}><IconCode width={11} height={11} /></button>
              )}
              {/* Edit starts EMPTY — the stored value is write-only, we can't prefill it. */}
              <button className="env-row-btn" onClick={() => setEnvEditor({ key, value: '', isNew: false })} title="Set a new value"><IconPencil width={11} height={11} /></button>
              <button className="env-row-btn env-row-btn-danger" onClick={() => deleteEnvVar(key)} title="Delete"><IconTrash width={11} height={11} /></button>
            </div>
          </div>
        ))}

        {/* Result notice for the last .env import (added/updated/skipped counts). */}
        {importStatus && (
          <div className={`env-import-notice env-import-${importStatus.kind}`}>{importStatus.text}</div>
        )}

        {/* Hidden picker for the bulk .env import. */}
        <input
          ref={dotenvInputRef}
          type="file"
          accept=".env,text/plain"
          style={{ display: 'none' }}
          onChange={onDotenvFilePicked}
        />

        {!envEditor && (
          <div className="env-add-row">
            <button className="env-add-btn env-add-env" onClick={() => setEnvEditor({ key: '', value: '', isNew: true })} disabled={busy}>
              <IconPlus width={11} height={11} /> Add env var
            </button>
            <button
              className="env-add-btn env-add-env env-import-btn"
              onClick={() => dotenvInputRef.current?.click()}
              disabled={busy}
              title="Import a .env file — parses KEY=VALUE lines, validates keys, and adds them as env vars"
            >
              <IconUpload width={11} height={11} /> Import .env
            </button>
          </div>
        )}

        {envEditor && (
          <div className="env-editor">
            <div className="env-editor-row">
              <input
                className="env-editor-input env-editor-key"
                type="text"
                placeholder="KEY"
                value={envEditor.key}
                disabled={!envEditor.isNew}   /* editing an existing var keeps its key */
                onChange={(e) => setEnvEditor(ed => ({ ...ed, key: e.target.value }))}
                spellCheck={false}
                autoFocus
              />
              <input
                className="env-editor-input env-editor-value"
                type="text"
                placeholder={envEditor.isNew ? 'value' : 'new value'}
                value={envEditor.value}
                onChange={(e) => setEnvEditor(ed => ({ ...ed, value: e.target.value }))}
                onKeyDown={(e) => { if (e.key === 'Enter') saveEnvVar() }}
                spellCheck={false}
              />
            </div>
            <div className="env-editor-actions">
              <button className="env-editor-cancel" onClick={() => setEnvEditor(null)}>Cancel</button>
              <button className="env-editor-save" onClick={saveEnvVar} disabled={!(envEditor.key || '').trim()}>
                <IconSave width={11} height={11} /> {envEditor.isNew ? 'Add' : 'Save'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

