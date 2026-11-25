import { useState, useEffect, useRef, useId } from 'react'
import { PROXY_URL, BACKEND_URL } from '../config'
import { apiFetch } from '../services/apiFetch'
import { sharedJson } from '../services/singleFlight'
import { readApiError } from '../services/apiError'
import { buildLaunchPayload } from '../services/vmLaunch'
import { persistTabVmLink, sessionRelinkPatch, restoreOffer } from '../services/tabVmLink'
import { BURST_FACTOR, tierVcpu, fmtTierVcpu } from '../services/vmBurst'
import { restoreChoiceState, restoreOverridePayload, nearestTier, tierLabel } from '../services/restoreChoice'
import {
  SECTION_RESTORE, SECTION_LAUNCH, openSection, toggledSection, restoreSectionSummary,
  launchSectionSummary, restoreNotes, strongestNote, footerHint, checkpointSizeLabel,
} from '../services/connectDialog'
import { sessionPackages } from '../services/restoreProgress'
import { DEFAULT_MEMORY_MIB } from '../constants'
import { ArchBadge, IconChevronRight, IconRocket, IconRestore, IconCheck, IconServer } from './Icons'
import './ConnectionPanel.css'

// The spec as the dialog shows it everywhere: the tier glyph label
// ("🧠 1.0 GB · ⚡ 0.5 vCPU", services/restoreChoice.js tierLabel) and the arch
// badge (icon + label; "unknown" without an arch). innerText reads as specText().
function SpecText({ arch, memoryMiB }) {
  return <>{`${tierLabel(memoryMiB)} · `}<ArchBadge arch={arch || undefined} /></>
}
// A section summary from services/connectDialog.js (restoreSectionSummary,
// launchSectionSummary): "<tier> · <arch badge> · <tag>".
function SummaryText({ parts }) {
  if (!parts) return null
  if (!parts.tier) return parts.tag
  return <>{`${parts.tier} · `}<ArchBadge arch={parts.arch || undefined} />{` · ${parts.tag}`}</>
}

// The tiny lock before a "Fixed by warm pool" hint (the old dialog's 🔒, drawn
// as an SVG so it takes the hint's warning colour).
function LockGlyph() {
  return (
    <svg className="launch-spec-lock-icon" width="10" height="10" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path fill="currentColor" d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2h.5A1 1 0 0 1 13 8v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h.5Zm1.5 0h4V5a2 2 0 0 0-4 0v2Z" />
    </svg>
  )
}

// Focusable controls for the dialog's focus trap.
const FOCUSABLE = 'button:not([disabled]), select:not([disabled]), input:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'

// One collapsible section of the dialog (accordion: the parent keeps exactly one
// open). The header is a real <button> (Enter / Space toggle natively) with
// aria-expanded / aria-controls; the closed panel stays mounted but inert, so
// its 150 ms height / opacity transition can run (none with reduced motion).
// `single`: the only section shown (no checkpoint), it cannot close.
function DialogSection({ id, title, summary, open, single, onToggle, children }) {
  const headId = `${id}-head`
  const panelId = `${id}-panel`
  return (
    <section className={`cp-sec${open ? ' cp-sec-open' : ''}`} data-open={open ? 'true' : 'false'}>
      <h3 className="cp-sec-h">
        <button
          type="button"
          id={headId}
          className="cp-sec-head"
          aria-expanded={open}
          aria-controls={panelId}
          aria-disabled={single || undefined}
          onClick={single ? undefined : onToggle}
        >
          <IconChevronRight className="cp-sec-chev" width={12} height={12} aria-hidden="true" focusable="false" />
          <span className="cp-sec-title">{title}</span>
          {summary && <span className="cp-sec-sum">{summary}</span>}
        </button>
      </h3>
      <div id={panelId} role="region" aria-labelledby={headId} className="cp-sec-panel" inert={!open}>
        <div className="cp-sec-inner">
          <div className="cp-sec-content">{children}</div>
        </div>
      </div>
    </section>
  )
}

export default function ConnectionPanel({ tab, onConnect, onUpdateTab, onDismiss }) {
  const [mode, setMode] = useState(null) // null until detected, then 'local' | 'microvm'
  const [persistenceMode, setPersistenceMode] = useState('eternal')
  const [maxLifetime, setMaxLifetime] = useState(28800)
  const [idleDefault, setIdleDefault] = useState(60)
  const [poolEnabled, setPoolEnabled] = useState(false)
  // The last failure is also kept on the tab (`_connectError`): this panel unmounts
  // while launching, so local state alone would lose the reason on the remount.
  const [error, setError] = useState(tab._connectError || null)
  const [launchMemory, setLaunchMemory] = useState(String(tab.microvmMemory || DEFAULT_MEMORY_MIB))
  // The tier to pre-select: /image-tiers `default_memory_mib` (config.sh DEFAULT_MEMORY_MIB).
  const [defaultMemory, setDefaultMemory] = useState(DEFAULT_MEMORY_MIB)
  const [launchIdleTimeout, setLaunchIdleTimeout] = useState(String(tab.idleTimeoutSeconds || idleDefault))
  const [launchMaxDuration, setLaunchMaxDuration] = useState(String(tab.maxDurationSeconds || maxLifetime || '28800'))
  const [imageTiers, setImageTiers] = useState([])          // tiers for the SELECTED arch
  const [tiersByArch, setTiersByArch] = useState({})        // {arch: [tier,...]}
  const [availableArches, setAvailableArches] = useState([]) // arches with built images
  const [launchArch, setLaunchArch] = useState(tab.microvmArch || '')
  const [availableSecrets, setAvailableSecrets] = useState([])
  const [selectedSecrets, setSelectedSecrets] = useState([]) // [{name, arn, envVar, secretKey?}]
  const [directEnvVars, setDirectEnvVars] = useState([]) // [{key, value}]
  const [launchTab, setLaunchTab] = useState('spec') // 'spec' | 'secrets'
  const [expandedSecret, setExpandedSecret] = useState(null) // ARN of secret showing keys
  const [secretKeys, setSecretKeys] = useState({}) // {arn: [key1, key2, ...]}
  // Option A: the saved instance type from GET /notebooks/{id} (session_arch,
  // session_memory_mib, session_checkpoint_kb) and the user's choice. null choice
  // = the saved value; the "Restore on a different instance type" section starts
  // collapsed.
  const [restoreSaved, setRestoreSaved] = useState(null) // {arch, memoryMiB, checkpointKb, packages}
  const [restoreArch, setRestoreArch] = useState(null)
  const [restoreMemory, setRestoreMemory] = useState(null)
  const [restoreOpen, setRestoreOpen] = useState(false)
  // The section the user opened (null = the default: Restore when offered, else
  // Launch; see services/connectDialog.js openSection). All section state (the
  // restore choice, the Launch fields) lives here, so it survives switching.
  const [chosenSection, setChosenSection] = useState(null)
  const cardRef = useRef(null)
  const uid = useId().replace(/:/g, '')

  // Initial focus on the dialog itself (not a button, so a stray Enter launches
  // nothing); focus returns to the element that had it when the dialog closes.
  useEffect(() => {
    const prev = document.activeElement
    cardRef.current?.focus({ preventScroll: true })
    return () => {
      try { if (prev && prev.isConnected && typeof prev.focus === 'function') prev.focus({ preventScroll: true }) } catch {}
    }
  }, [])

  // Escape closes the dialog (as Dismiss), when the key comes from the dialog
  // or from no focused element (focus on the page body).
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      const card = cardRef.current
      const t = e.target
      if (!card || !(card.contains(t) || t === document.body || t === document.documentElement)) return
      e.preventDefault()
      onDismiss()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onDismiss])

  // Focus trap: Tab / Shift+Tab cycle through the dialog's visible controls.
  const trapFocus = (e) => {
    if (e.key !== 'Tab') return
    const card = cardRef.current
    if (!card) return
    const items = [...card.querySelectorAll(FOCUSABLE)]
      .filter(el => !el.closest('[inert]') && !el.closest('[hidden]') && el.getClientRects().length > 0)
    if (items.length === 0) { e.preventDefault(); return }
    const first = items[0]
    const last = items[items.length - 1]
    const active = document.activeElement
    if (e.shiftKey && (active === first || active === card || !card.contains(active))) {
      e.preventDefault(); last.focus()
    } else if (!e.shiftKey && (active === last || !card.contains(active))) {
      e.preventDefault(); first.focus()
    }
  }

  // Auto-detect which mode we're in
  useEffect(() => {
    apiFetch(`/health`)
      .then(r => r.json())
      .then(data => {
        if (data.image_arn && data.image_arn !== '(not configured)') {
          setMode('microvm')
          if (data.persistence_mode) setPersistenceMode(data.persistence_mode)
          if (data.max_lifetime_seconds) setMaxLifetime(data.max_lifetime_seconds)
          if (data.idle_timeout_seconds) setIdleDefault(data.idle_timeout_seconds)
          if (typeof data.pool_enabled === 'boolean') setPoolEnabled(data.pool_enabled)
          fetchRestoreState()
          fetchImageTiers()
        } else {
          setMode('local')
        }
      })
      .catch(() => setMode('local'))
  }, [])

  // Pre-seed the secrets/env picker from the notebook's PERSISTED env so a
  // reconnect/relaunch shows (and re-saves) what was previously configured — rather
  // than launching with an empty set and silently dropping the stored refs. Only
  // seeds when we actually have stored entries and the local picker is still empty
  // (don't stomp an in-progress edit).
  useEffect(() => {
    if (!tab.id) return
    let cancelled = false
    ;(async () => {
      try {
        const resp = await apiFetch(`/notebooks/${tab.id}/env`)
        if (!resp.ok) return
        const data = await resp.json()
        if (cancelled) return
        const storedSecrets = data.secrets || []
        // env_vars are now write-only PRESENCE MARKERS ({KEY:{set:bool}}), not values —
        // seed each stored key with an EMPTY value (the real value stays server-side).
        // The launch POST filters e.key && e.value, so empty-valued keys are simply
        // not resent, and the proxy merges the persisted env in at launch anyway.
        const storedEnv = data.env_vars || {}
        setSelectedSecrets(prev => (prev.length === 0 && storedSecrets.length)
          ? storedSecrets.map(s => ({ name: s.name || '', arn: s.arn || '', envVar: s.envVar || '', secretKey: s.secretKey || '' }))
          : prev)
        setDirectEnvVars(prev => (prev.length === 0 && Object.keys(storedEnv).length)
          ? Object.keys(storedEnv).map(key => ({ key, value: '' }))
          : prev)
      } catch { /* no persisted env / offline — start empty */ }
    })()
    return () => { cancelled = true }
  }, [tab.id])

  // When Option A is offered, read the instance type the server pins a default
  // restore to and the checkpoint size (for the smaller-tier warning). This runs
  // apart from fetchRestoreState, which reads the notebook only for tabs that lack
  // a session or an arch, and Option A may appear only after it marks the tab saved.
  const restoreSid = restoreOffer(tab, persistenceMode) && mode === 'microvm' ? tab.sessionId : null
  useEffect(() => {
    if (!restoreSid || !tab.id) return
    let cancelled = false
    ;(async () => {
      try {
        const resp = await apiFetch(`/notebooks/${tab.id}`)
        if (!resp.ok) return
        const nb = await resp.json()
        if (cancelled || nb?.session_id !== restoreSid) return
        setRestoreSaved({
          arch: nb.session_arch || null,
          memoryMiB: Number(nb.session_memory_mib) || null,
          checkpointKb: Number(nb.session_checkpoint_kb) || null,
          // The user packages the restore reinstalls (checkpoint requirements.txt),
          // shown as "Reinstalling N packages (...)..." while it is pending.
          packages: sessionPackages(nb),
        })
      } catch { /* keep the tab's values */ }
    })()
    return () => { cancelled = true }
  }, [restoreSid, tab.id])

  const fetchImageTiers = async () => {
    try {
      const resp = await sharedJson('image-tiers|connect', () => apiFetch(`/image-tiers`))
      if (!resp.ok) return
      const data = resp.data

      // New arch-grouped shape (with graceful fallback to the old flat `tiers`).
      const byArch = data.tiersByArch || (data.tiers?.length ? { [data.defaultArch || 'arm64']: data.tiers } : {})
      const arches = data.arches || Object.keys(byArch)
      if (arches.length === 0) return
      const defMem = Number(data.default_memory_mib) || DEFAULT_MEMORY_MIB
      setDefaultMemory(defMem)

      setTiersByArch(byArch)
      setAvailableArches(arches)

      // Pick the arch: keep the tab's prior choice if still available, else the
      // server-suggested default, else the first available.
      const arch = (tab.microvmArch && arches.includes(tab.microvmArch))
        ? tab.microvmArch
        : (arches.includes(data.defaultArch) ? data.defaultArch : arches[0])
      setLaunchArch(arch)

      const tiers = byArch[arch] || []
      setImageTiers(tiers)
      // Re-default when there's no prior size, or the prior size isn't offered for
      // this arch (arches may offer different tiers).
      const priorOffered = tiers.some(t => t.memory_mib === Number(tab.microvmMemory))
      if ((!tab.microvmMemory || !priorOffered) && tiers.length > 0) {
        const defaultTier = tiers.find(t => t.memory_mib === defMem) || tiers[0]
        setLaunchMemory(String(defaultTier.memory_mib))
      }
    } catch {}
  }

  // When the user changes arch, swap the size dropdown to that arch's tiers and
  // re-default the selected size (the chosen size may not exist for the new arch).
  const handleArchChange = (arch) => {
    setLaunchArch(arch)
    const tiers = tiersByArch[arch] || []
    setImageTiers(tiers)
    if (tiers.length > 0 && !tiers.some(t => String(t.memory_mib) === launchMemory)) {
      const def = tiers.find(t => t.memory_mib === defaultMemory) || tiers[0]
      setLaunchMemory(String(def.memory_mib))
    }
  }

  const fetchSecrets = async () => {
    try {
      const resp = await apiFetch(`/secrets`)
      if (resp.ok) {
        const data = await resp.json()
        setAvailableSecrets(data.secrets || [])
      }
    } catch {}
  }

  const fetchSecretKeys = async (arn) => {
    if (secretKeys[arn]) return // Already fetched
    try {
      const resp = await apiFetch(`/secrets/keys?secret_id=${encodeURIComponent(arn)}`)
      if (resp.ok) {
        const data = await resp.json()
        setSecretKeys(prev => ({ ...prev, [arn]: data.keys || [] }))
      }
    } catch {}
  }

  // The dialog never lists existing VMs to attach to: a notebook connects only by
  // restoring onto a NEW VM or launching a NEW VM. /instances is read here only
  // to reconcile this tab's restore state.
  const fetchRestoreState = async () => {
    try {
      const resp = await apiFetch(`/instances`)
      if (resp.ok) {
        const data = await resp.json()
        const instMap = data.instances || {}

        // No session on the tab: adopt the notebook's last checkpointed session
        // from the server, so Option A (restore) is offered instead of only a
        // fresh launch on a new session. A tab with a session but no arch takes
        // the session's recorded arch/tier (shown on Option A).
        if ((!tab.sessionId || !tab.microvmArch) && tab.id) {
          const nbResp = await apiFetch(`/notebooks/${tab.id}`)
          const patch = nbResp.ok ? sessionRelinkPatch(tab, await nbResp.json(), instMap) : null
          if (patch) onUpdateTab(patch)
          if (patch && !tab.sessionId) return
        }

        // Reconcile THIS tab's restore state from the /instances response, so
        // the "Restore Session" button appears the moment the panel opens instead
        // of waiting up to a full instance-poll interval (15 s) for the background
        // loop to notice the VM is gone. If this tab has a checkpoint-enabled
        // session but its VM is absent (or not alive) here, the checkpoint in S3 is
        // the newest state → mark it saved now. We only flip it ON (never off), so
        // this can't fight the live-VM path (which reconnects when the VM IS found).
        if (tab.status !== 'connected' && !tab.sessionSaved
            && tab.sessionId && tab.checkpointEnabled) {
          const bySession = Object.values(instMap).some(i => i.session_id === tab.sessionId && i.state !== 'TERMINATED')
          const byId = tab.microvmId && instMap[tab.microvmId] && instMap[tab.microvmId].state !== 'TERMINATED'
          if (!bySession && !byId) {
            onUpdateTab({ sessionSaved: true })
          }
        }
      }
    } catch {}
  }

  // Report a sandbox operation to App ('sandbox-op' event), which shows the
  // "Sandbox ready" / failure banner in the Notebooks and Live MicroVMs panels. This
  // panel unmounts while the tab is launching, so App (always mounted) owns it.
  //   start:  sandboxOp('restore')            -> returns the finish reporter
  //   finish: done({ ok: true }) / done({ ok: false, error })
  const sandboxOp = (op) => {
    const tabId = tab.id
    const startedAt = Date.now()
    const send = (detail) => {
      try { window.dispatchEvent(new CustomEvent('sandbox-op', { detail: { tabId, op, ...detail } })) } catch {}
    }
    send({ phase: 'start' })
    return ({ ok, error = '' }) => send({ phase: 'end', ok, error, elapsedMs: Date.now() - startedAt })
  }

  const handleConnectLocal = async () => {
    const url = BACKEND_URL
    const done = sandboxOp('connect')
    onUpdateTab({ status: 'connecting', _connectError: null })
    setError(null)

    try {
      const response = await fetch(`${url}/health`)
      if (response.ok) {
        onUpdateTab({ microvmId: null })
        onConnect(url)
        done({ ok: true })
      } else {
        const msg = `Health check failed: ${response.status}`
        setError(msg)
        onUpdateTab({ status: 'disconnected', _connectError: msg })
        done({ ok: false, error: msg })
      }
    } catch {
      const msg = `Cannot reach local backend. Run: python3 -m uvicorn app.server:app --port ${BACKEND_URL.split(':').pop()}`
      setError(msg)
      onUpdateTab({ status: 'disconnected', _connectError: msg })
      done({ ok: false, error: msg })
    }
  }

  // Option A choice. Saved = what the server pins a default restore to, falling
  // back to the tab's last known values.
  const savedArch = restoreSaved?.arch || tab.microvmArch || ''
  const savedMemoryMiB = restoreSaved?.memoryMiB || Number(tab.microvmMemory) || null
  const restoreChoice = restoreChoiceState({
    savedArch, savedMemoryMiB, arch: restoreArch, memoryMiB: restoreMemory,
    checkpointKb: restoreSaved?.checkpointKb,
  })
  // Tiers offered for an arch (memory_mib), plus the saved tier on the saved arch.
  const restoreTiersFor = (arch) => {
    const list = (tiersByArch[arch] || []).map(t => Number(t.memory_mib)).filter(n => n > 0)
    if (arch === savedArch && savedMemoryMiB && !list.includes(savedMemoryMiB)) list.push(savedMemoryMiB)
    return list.sort((a, b) => a - b)
  }
  // A new arch keeps the saved tier when that arch offers it, else the nearest one.
  const handleRestoreArchChange = (arch) => {
    setRestoreArch(arch)
    setRestoreMemory(nearestTier(restoreTiersFor(arch), savedMemoryMiB) ?? savedMemoryMiB)
  }

  // Report the restore's package re-install result to App ('restore-report'
  // event), which shows it in the Notebooks, Live MicroVMs and Packages panels.
  const reportRestoreReport = (result) => {
    try {
      window.dispatchEvent(new CustomEvent('restore-report', {
        detail: {
          tabId: tab.id, report: result?.restore_report || null, arch: result?.architecture || '',
          memoryCheck: result?.restore_memory_check || null,
        },
      }))
    } catch {}
  }

  const handleRestoreSession = async () => {
    const done = sandboxOp('restore')
    // _restorePackages: the notebook view's "Reinstalling N packages (...)..." line
    // while this restore is pending (services/restoreProgress.js); cleared at the end.
    onUpdateTab({ status: 'launching', _connectError: null, _restorePackages: restoreSaved?.packages || null })
    setError(null)

    try {
      const response = await apiFetch(`/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...buildLaunchPayload(tab, { restore: true }),
          // Default restore: no arch / memoryMiB, the server pins the saved values.
          // Only a choice that differs from them adds restoreOverride: true + arch +
          // memoryMiB (see services/restoreChoice.js).
          ...restoreOverridePayload(restoreChoice),
          // When the pool is enabled these are frozen deployment-wide (the proxy
          // enforces this server-side regardless), so send the fixed values rather
          // than a possibly-stale pre-lock selection.
          idleTimeoutSeconds: poolEnabled ? idleDefault : parseInt(launchIdleTimeout),
          maxDurationSeconds: poolEnabled ? maxLifetime : parseInt(launchMaxDuration),
          // NOTE: restore deliberately does NOT send Option B's memory tier or
          // secrets. The instance type comes from Option A (saved values unless the
          // user picked another one), and the Instance Spec + Secrets config belongs
          // to Option B (fresh launch). Restore re-injects whatever secrets/env are
          // already PERSISTED on the notebook (server merges them in), so a restored
          // VM still gets the notebook's established secrets.
        }),
      })

      if (response.ok) {
        const result = await response.json()
        persistTabVmLink(tab.id, result)  // saved now, not after the 1.5 s autosave
        onUpdateTab({
          microvmId: result.microvmId,
          microvmEndpoint: `${PROXY_URL}/proxy`,
          // The server decides memory/arch on restore (the saved tier, or the
          // user's explicit choice), so read the ACTUAL values it echoes back — NOT
          // the panel's launchMemory. The response field is snake_case (memory_mib /
          // architecture); using the wrong case made the tab show the stale panel
          // tier (e.g. "1 GB") while the real VM was the saved tier ("2 GB").
          microvmMemory: result.memory_mib || restoreChoice.memoryMiB || tab.microvmMemory,
          microvmArch: result.architecture || restoreChoice.arch || tab.microvmArch,
          // Restore reuses the notebook's session; fall back to tab.sessionId so a
          // malformed response can never silently rebind the notebook to a new id.
          sessionId: result.sessionId || tab.sessionId,
          checkpointEnabled: true,
          sessionSaved: false,
          status: 'connected',
          mode: 'microvm',
          _restorePackages: null,
        })
        onDismiss()
        done({ ok: true })
        // After done(): the report banner replaces "Sandbox ready" in its slots.
        reportRestoreReport(result)
      } else {
        const msg = await readApiError(response, `Restore failed: ${response.status}`)
        setError(msg)
        onUpdateTab({ status: 'disconnected', _connectError: msg, _restorePackages: null })
        done({ ok: false, error: msg })
      }
    } catch (err) {
      const msg = `Restore error: ${err.message}`
      setError(msg)
      onUpdateTab({ status: 'disconnected', _connectError: msg, _restorePackages: null })
      done({ ok: false, error: msg })
    }
  }

  const handleLaunchMicroVM = async ({ cleanStart = false } = {}) => {
    const done = sandboxOp('launch')
    onUpdateTab({ status: 'launching', _connectError: null })
    setError(null)

    try {
      const response = await apiFetch(`/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: tab.name,
          notebookId: tab.id,   // the notebook's DB integer id (as string; proxy coerces to int)
          memoryMiB: parseInt(launchMemory),
          arch: launchArch || undefined,
          // When the pool is enabled these are frozen deployment-wide (the proxy
          // enforces this server-side regardless), so send the fixed values rather
          // than a possibly-stale pre-lock selection.
          idleTimeoutSeconds: poolEnabled ? idleDefault : parseInt(launchIdleTimeout),
          maxDurationSeconds: poolEnabled ? maxLifetime : parseInt(launchMaxDuration),
          checkpointEnabled: true,
          // A notebook keeps ONE session_id for its whole lifetime — the VM under
          // it may rotate (idle-suspend, 8h max-lifetime reap, manual relaunch), but
          // everything session-scoped (workbook intel, checkpoints, chat transcript,
          // cost history) is keyed on this id. So REUSE the notebook's existing
          // sessionId; only mint a fresh UUID for a genuinely new notebook that has
          // none yet. Minting a new one here (the old behavior) orphaned the prior
          // session's intel/checkpoint on every "launch new VM" for an existing nb.
          sessionId: tab.sessionId || crypto.randomUUID(),
          // If the notebook already has a saved session, a plain launch is effectively
          // a restore onto a fresh VM — carry restoreFromSession so its S3 state comes
          // back. EXCEPT when cleanStart is set (the "Launch a fresh VM" option): the
          // user explicitly wants an EMPTY VM, so we skip the restore even though a
          // saved session exists.
          ...(tab.sessionId && !cleanStart ? { restoreFromSession: tab.sessionId } : {}),
          secrets: selectedSecrets.filter(s => s.envVar).map(s => ({ name: s.name, arn: s.arn, envVar: s.envVar, secretKey: s.secretKey || '' })),
          envVars: directEnvVars.filter(e => e.key && e.value).reduce((acc, e) => ({ ...acc, [e.key]: e.value }), {}),
        }),
      })

      if (response.ok) {
        const result = await response.json()

        // PERSIST the secret/env references against the notebook so they survive
        // suspend/resume and relaunch. The proxy re-injects this blob into every
        // future /launch (incl. restore), so a restored VM gets its secrets back
        // WITHOUT the client having to resend them. Secret VALUES are never sent —
        // only references (arn/envVar/secretKey); the VM resolves them at launch.
        // Best-effort: a persistence hiccup doesn't fail the (already-succeeded)
        // launch — the current VM already has them injected via the /launch body.
        try {
          await apiFetch(`/notebooks/${tab.id}/env`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              env_vars: directEnvVars.filter(e => e.key && e.value).reduce((acc, e) => ({ ...acc, [e.key]: e.value }), {}),
              secrets: selectedSecrets.filter(s => s.envVar).map(s => ({ name: s.name, arn: s.arn, envVar: s.envVar, secretKey: s.secretKey || '' })),
            }),
          })
        } catch { /* non-fatal — current VM already has them; relaunch will refetch */ }

        onUpdateTab({
          microvmId: result.microvmId,
          microvmEndpoint: `${PROXY_URL}/proxy`,
          // Prefer the server-echoed actual tier/arch (it may snap to a nearest
          // valid tier) over what we sent; fall back to the panel values.
          microvmMemory: result.memory_mib || parseInt(launchMemory),
          microvmArch: result.architecture || launchArch,
          idleTimeoutSeconds: poolEnabled ? idleDefault : parseInt(launchIdleTimeout),
          maxDurationSeconds: poolEnabled ? maxLifetime : parseInt(launchMaxDuration),
          // Keep the notebook's stable session_id. The proxy echoes back the id we
          // sent, but fall back to the existing tab.sessionId so a relaunch can never
          // silently rebind the notebook to a different session.
          sessionId: result.sessionId || tab.sessionId,
          // Reflect the SERVER's actual persistence mode — not a hardcoded true.
          // In 'none' (ephemeral) mode nothing is checkpointed to S3, so the tab
          // must not claim checkpointEnabled (which would wrongly offer Restore /
          // mark sessionSaved later). Only 'eternal'/'checkpoint' persist.
          checkpointEnabled: persistenceMode !== 'none',
          status: 'connected',
          mode: 'microvm',
          _envVars: [
            ...selectedSecrets.filter(s => s.envVar).map(s => ({ key: s.envVar, source: 'sm', secretName: s.name })),
            ...directEnvVars.filter(e => e.key && e.value).map(e => ({ key: e.key, source: 'direct' })),
          ],
          // A previous restore's package report does not describe this VM.
          _restoreReport: null,
        })
        onDismiss()
        done({ ok: true })
        // A launch that carried restoreFromSession is a restore too: show its report.
        if (result.restore_report) reportRestoreReport(result)
      } else {
        const msg = await readApiError(response, `Launch failed: ${response.status}`)
        setError(msg)
        onUpdateTab({ status: 'disconnected', _connectError: msg })
        done({ ok: false, error: msg })
      }
    } catch (err) {
      const msg = `Proxy error: ${err.message}`
      setError(msg)
      onUpdateTab({ status: 'disconnected', _connectError: msg })
      done({ ok: false, error: msg })
    }
  }

  // Option A (restore) needs a saved checkpoint AND a session id (restoreOffer).
  const restore = restoreOffer(tab, persistenceMode)
  // The Restore section shows only with a checkpoint of this notebook's session.
  const hasRestore = !!(restore && mode === 'microvm')
  const section = openSection(chosenSection, hasRestore)
  const toggleSection = (clicked) => setChosenSection(toggledSection(section, clicked, hasRestore))
  const notes = restoreNotes(restoreChoice)
  // The strongest active warning, pinned above the footer so a short window
  // cannot scroll it out of view (the full notes stay in the Restore body).
  const pinned = hasRestore && section === SECTION_RESTORE ? strongestNote(notes) : null
  // The pinned strip repeats a note that is already in the body, so it shows only
  // while that inline note is scrolled out of view (otherwise the same warning
  // appears twice on screen).
  const bodyRef = useRef(null)
  const [pinInView, setPinInView] = useState(false)
  useEffect(() => {
    const body = bodyRef.current
    if (!pinned || !body || typeof IntersectionObserver === 'undefined') { setPinInView(false); return undefined }
    const el = body.querySelector(`.restore-note[data-kind="${pinned.kind}"]`)
    if (!el) { setPinInView(false); return undefined }
    const io = new IntersectionObserver(
      ([entry]) => setPinInView(entry.intersectionRatio >= 0.99),
      { root: body, threshold: [0, 0.5, 0.99, 1] })
    io.observe(el)
    return () => io.disconnect()
  }, [pinned?.kind, pinned?.level, pinned?.message, section])
  const showPin = !!pinned && !pinInView
  const busy = tab.status === 'launching'
  // With a saved session, Launch is a fresh, EMPTY VM (cleanStart drops the
  // auto-restore); otherwise it is the plain first launch of this notebook.
  const cleanStart = !!tab.sessionSaved
  const ckptSize = checkpointSizeLabel(restoreSaved?.checkpointKb)
  const archOptionLabel = (a) => (a === 'arm64' ? 'ARM64 (Graviton)' : a === 'x86_64' ? 'x86_64 (Intel/AMD)' : a)
  const warmPoolTitle = "Fixed by the warm pool: idle suspend and max duration are set at VM launch and can't change on a live VM"
  const maxDurationText = parseInt(launchMaxDuration) >= 3600
    ? `${Math.floor(parseInt(launchMaxDuration) / 3600)}h`
    : `${Math.floor(parseInt(launchMaxDuration) / 60)}m`

  let hint = ''
  let primary = null
  if (mode === 'local') {
    primary = (
      <button className="connect-btn" onClick={handleConnectLocal} disabled={tab.status === 'connecting'}>
        {tab.status === 'connecting'
          ? <><span className="cp-spinner" aria-hidden="true" /><span>Connecting...</span></>
          : <><IconServer className="cp-btn-icon" strokeWidth={1.75} aria-hidden="true" focusable="false" /><span>Connect to Local</span></>}
      </button>
    )
  } else if (mode === 'microvm' && section === SECTION_RESTORE) {
    hint = footerHint(SECTION_RESTORE, { choice: restoreChoice })
    primary = (
      <button className="connect-btn" data-action="restore" onClick={handleRestoreSession} disabled={busy} aria-busy={busy || undefined}>
        {busy
          // The busy ring takes the icon's 16 px slot, so the label does not move.
          ? <><span className="cp-spinner" aria-hidden="true" /><span>Restoring...</span></>
          : <>
              <IconRestore className="cp-btn-icon" />
              {restoreChoice.changed
                ? <span>Restore on <ArchBadge arch={restoreChoice.arch || undefined} />{` · ${tierLabel(restoreChoice.memoryMiB, { vcpu: false })}`}</span>
                : <span>{restoreChoice.buttonText}</span>}
            </>}
      </button>
    )
  } else if (mode === 'microvm') {
    hint = footerHint(SECTION_LAUNCH, { cleanStart })
    primary = (
      <button className="connect-btn" data-action="launch" onClick={() => handleLaunchMicroVM({ cleanStart })} disabled={busy} aria-busy={busy || undefined}>
        {busy
          ? <><span className="cp-spinner" aria-hidden="true" /><span>Launching...</span></>
          : <><IconRocket className="cp-btn-icon" /><span>Launch new VM</span></>}
      </button>
    )
  }

  const launchConfig = (
    <div className="launch-config">
      {tab.status !== 'connected' && !tab.sessionSaved && tab.microvmId && (
        <div className="connection-subhint">
          Previously connected to <code>{tab.microvmId.replace('microvm-', '').slice(0, 12)}...</code>. Launch a new MicroVM.
        </div>
      )}
      <div className="launch-config-tabs" role="tablist" aria-label="Launch settings">
        <button
          type="button" role="tab" aria-selected={launchTab === 'spec'}
          className={`launch-tab ${launchTab === 'spec' ? 'launch-tab-active' : ''}`}
          onClick={() => setLaunchTab('spec')}
        >Instance spec</button>
        <button
          type="button" role="tab" aria-selected={launchTab === 'secrets'}
          className={`launch-tab ${launchTab === 'secrets' ? 'launch-tab-active' : ''}`}
          onClick={() => { setLaunchTab('secrets'); if (availableSecrets.length === 0) fetchSecrets(); }}
        >Secrets &amp; env vars{selectedSecrets.length + directEnvVars.length > 0 ? ` (${selectedSecrets.length + directEnvVars.length})` : ''}</button>
      </div>

      {launchTab === 'spec' && (
      <>
      <div className="launch-config-specs">
        {/* Architecture FIRST: it determines which image sizes are available.
            Editable select when >1 arch is built; read-only display otherwise. */}
        {availableArches.length > 1 ? (
          <label className="launch-spec-item launch-spec-editable">
            <span className="launch-spec-label">CPU architecture</span>
            {/* Native select (text-only options); the icon for the current
                choice sits beside it. */}
            <span className="launch-spec-select-row">
              <select className="launch-spec-select" value={launchArch} onChange={(e) => handleArchChange(e.target.value)}>
                {availableArches.map(a => (
                  <option key={a} value={a}>{archOptionLabel(a)}</option>
                ))}
              </select>
              <ArchBadge arch={launchArch} iconOnly />
            </span>
          </label>
        ) : (
          <div className="launch-spec-item">
            <span className="launch-spec-label">CPU architecture</span>
            <span className="launch-spec-value">
              {/* A missing arch reads "unknown" (ArchBadge), never ARM64. */}
              <ArchBadge
                arch={launchArch}
                label={launchArch === 'x86_64' ? 'x86_64 (Intel/AMD)' : launchArch === 'arm64' ? 'ARM64 (Graviton)' : (launchArch || undefined)}
              />
            </span>
          </div>
        )}
        <label className="launch-spec-item launch-spec-editable">
          {/* With >1 arch, say whose sizes the list shows. */}
          <span className="launch-spec-label">Baseline size{availableArches.length > 1 && <ArchBadge arch={launchArch} />}</span>
          <select className="launch-spec-select" value={launchMemory} onChange={(e) => setLaunchMemory(e.target.value)}>
            {imageTiers.length > 0 ? (
              imageTiers.map(tier => (
                <option key={tier.memory_mib} value={tier.memory_mib}>
                  {/* Native options are text only: the memory and vCPU glyphs of
                      the server's tier label, built by the shared tierLabel. */}
                  {tierLabel(tier.memory_mib) || tier.label}
                </option>
              ))
            ) : (
              [512, 1024, 2048, 4096, 8192].map(m => (
                <option key={m} value={m}>{tierLabel(m)}</option>
              ))
            )}
          </select>
          <span className="launch-spec-peak">Bursts to <strong>{(parseInt(launchMemory) / 1024 * BURST_FACTOR).toFixed(1)} GB · {fmtTierVcpu(tierVcpu(parseInt(launchMemory)) * BURST_FACTOR)} vCPU</strong> at peak ({BURST_FACTOR}×)</span>
        </label>
        <label className="launch-spec-item launch-spec-editable">
          <span className="launch-spec-label">Idle suspend</span>
          <select
            className="launch-spec-select"
            value={poolEnabled ? String(idleDefault) : launchIdleTimeout}
            onChange={(e) => setLaunchIdleTimeout(e.target.value)}
            disabled={poolEnabled}
            title={poolEnabled ? warmPoolTitle : undefined}
          >
            <option value="60">1 minute</option>
            <option value="120">2 minutes</option>
            <option value="300">5 minutes</option>
            <option value="900">15 minutes</option>
            <option value="1800">30 minutes</option>
            <option value="3600">1 hour</option>
            <option value="7200">2 hours</option>
          </select>
          {poolEnabled && (
            <span className="launch-spec-peak launch-spec-lock" title={warmPoolTitle}><LockGlyph />Fixed by warm pool</span>
          )}
        </label>
        <label className="launch-spec-item launch-spec-editable">
          <span className="launch-spec-label">Max duration</span>
          <select
            className="launch-spec-select"
            value={poolEnabled ? String(maxLifetime) : launchMaxDuration}
            onChange={(e) => setLaunchMaxDuration(e.target.value)}
            disabled={poolEnabled}
            title={poolEnabled ? warmPoolTitle : undefined}
          >
            <option value="1800">30 minutes</option>
            <option value="3600">1 hour</option>
            <option value="7200">2 hours</option>
            <option value="14400">4 hours</option>
            <option value="28800">8 hours</option>
          </select>
          {poolEnabled && (
            <span className="launch-spec-peak launch-spec-lock" title={warmPoolTitle}><LockGlyph />Fixed by warm pool</span>
          )}
        </label>
      </div>
      <div className="launch-config-note">
        Scales up to <strong>{BURST_FACTOR}× baseline</strong> under peak load; burst resources are billed only while used. Resumes on traffic in about 1 s.
      </div>
      </>
      )}

      {launchTab === 'secrets' && (
      <div className="launch-secrets-body">
        {/* Secrets Manager picker */}
        <div className="launch-secrets-group">
          <label className="launch-secrets-label">From AWS Secrets Manager:</label>
          <select
            className="launch-spec-select"
            value={expandedSecret || ''}
            onChange={async (e) => {
              const arn = e.target.value
              setExpandedSecret(arn)
              if (arn) await fetchSecretKeys(arn)
            }}
          >
            <option value="">Select a secret...</option>
            {availableSecrets.map(s => (
              <option key={s.arn} value={s.arn}>{s.name}</option>
            ))}
          </select>

          {/* Plaintext secret (no JSON keys): let the user inject the whole
              value into an env var. hooks.py injects the entire SecretString
              when no secretKey is set. */}
          {expandedSecret && secretKeys[expandedSecret] && secretKeys[expandedSecret].length === 0 && (() => {
            const secret = availableSecrets.find(s => s.arn === expandedSecret)
            const whole = selectedSecrets.find(s => s.arn === expandedSecret && !s.secretKey)
            const isSelected = Boolean(whole)
            return (
              <div className="launch-secret-keys-panel">
                <div className="launch-secret-keys-title">
                  <code>{secret?.name?.split('/').pop()}</code> is a plaintext secret — inject the whole value:
                </div>
                <div className="launch-secret-keys">
                  <div className="launch-secret-key-row">
                    <label className="launch-secret-check">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={(e) => {
                          if (e.target.checked) {
                            const envVar = (secret?.name?.split('/').pop() || 'SECRET').toUpperCase().replace(/[^A-Z0-9]+/g, '_')
                            setSelectedSecrets(prev => [...prev, { name: secret?.name || '', arn: expandedSecret, secretKey: '', envVar }])
                          } else {
                            setSelectedSecrets(prev => prev.filter(s => !(s.arn === expandedSecret && !s.secretKey)))
                          }
                        }}
                      />
                      <code className="launch-secret-key-name">(entire value)</code>
                    </label>
                    {isSelected && (
                      <>
                        <span className="launch-secret-arrow">→</span>
                        <input
                          className="launch-secret-envvar-inline"
                          type="text"
                          value={whole.envVar}
                          onChange={(e) => setSelectedSecrets(prev => prev.map(s => (s.arn === expandedSecret && !s.secretKey) ? { ...s, envVar: e.target.value } : s))}
                        />
                      </>
                    )}
                  </div>
                </div>
              </div>
            )
          })()}

          {/* Show keys for selected JSON secret */}
          {expandedSecret && secretKeys[expandedSecret] && secretKeys[expandedSecret].length > 0 && (
            <div className="launch-secret-keys-panel">
              <div className="launch-secret-keys-title">
                Keys in: <code>{availableSecrets.find(s => s.arn === expandedSecret)?.name?.split('/').pop()}</code>
              </div>
              <div className="launch-secret-keys">
                {secretKeys[expandedSecret].map(key => {
                  const isKeySelected = selectedSecrets.some(s => s.arn === expandedSecret && s.secretKey === key)
                  const envVar = selectedSecrets.find(s => s.arn === expandedSecret && s.secretKey === key)?.envVar || ''
                  return (
                    <div key={key} className="launch-secret-key-row">
                      <label className="launch-secret-check">
                        <input
                          type="checkbox"
                          checked={isKeySelected}
                          onChange={(e) => {
                            if (e.target.checked) {
                              const secret = availableSecrets.find(s => s.arn === expandedSecret)
                              setSelectedSecrets(prev => [...prev, { name: secret?.name || '', arn: expandedSecret, secretKey: key, envVar: key.toUpperCase() }])
                            } else {
                              setSelectedSecrets(prev => prev.filter(s => !(s.arn === expandedSecret && s.secretKey === key)))
                            }
                          }}
                        />
                        <code className="launch-secret-key-name">{key}</code>
                      </label>
                      {isKeySelected && (
                        <>
                          <span className="launch-secret-arrow">→</span>
                          <input
                            className="launch-secret-envvar-inline"
                            type="text"
                            value={envVar}
                            onChange={(e) => setSelectedSecrets(prev => prev.map(s => (s.arn === expandedSecret && s.secretKey === key) ? { ...s, envVar: e.target.value } : s))}
                          />
                        </>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Show all selected keys (across all secrets) */}
          {selectedSecrets.length > 0 && (
            <div className="launch-secret-selected-summary">
              <div className="launch-secrets-label">Selected ({selectedSecrets.length}):</div>
              {selectedSecrets.map((s, idx) => (
                <div key={idx} className="launch-secret-selected-row">
                  <code>{s.secretKey}</code>
                  <span className="launch-secret-arrow">→</span>
                  <code className="launch-secret-envvar-display">{s.envVar}</code>
                  <button className="launch-envvar-remove" aria-label={`Remove ${s.envVar}`} onClick={() => setSelectedSecrets(prev => prev.filter((_, i) => i !== idx))}>✕</button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Direct key-value env vars */}
        <div className="launch-secrets-group">
          <label className="launch-secrets-label">Direct env vars:</label>
          {directEnvVars.map((env, idx) => (
            <div key={idx} className="launch-envvar-row">
              <input
                className="launch-envvar-key"
                type="text"
                placeholder="KEY"
                value={env.key}
                onChange={(e) => setDirectEnvVars(prev => prev.map((v, i) => i === idx ? { ...v, key: e.target.value } : v))}
              />
              <input
                className="launch-envvar-value"
                type="password"
                placeholder="value"
                value={env.value}
                onChange={(e) => setDirectEnvVars(prev => prev.map((v, i) => i === idx ? { ...v, value: e.target.value } : v))}
              />
              <button className="launch-envvar-remove" aria-label="Remove variable" onClick={() => setDirectEnvVars(prev => prev.filter((_, i) => i !== idx))}>✕</button>
            </div>
          ))}
          <button className="launch-envvar-add" onClick={() => setDirectEnvVars(prev => [...prev, { key: '', value: '' }])}>+ Add variable</button>
        </div>
      </div>
      )}
    </div>
  )

  return (
    // Click outside the card does nothing (as before); mousedown on the scrim
    // keeps focus inside the dialog.
    <div className="connection-panel" onMouseDown={(e) => { if (e.target === e.currentTarget) e.preventDefault() }}>
      <div
        className="connection-card"
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${uid}-title`}
        tabIndex={-1}
        onKeyDown={trapFocus}
      >
        {/* ── Header (fixed) ── */}
        <div className="cp-head">
          <div className="connection-title" id={`${uid}-title`}>
            {tab.status === 'connected' ? 'Switch Sandbox' : 'Connect to Sandbox'}
            {/* The space keeps the dialog's accessible name readable. */}
            {tab.name && ' '}
            {tab.name && (
              <span className="connection-title-notebook" style={{marginLeft: '4px', color: 'var(--accent-primary, #ff9900)', fontWeight: 600}}>
                {tab.name}
              </span>
            )}
            {mode === 'microvm' && (
              <span
                className={`vm-mode-badge vm-mode-${persistenceMode}`}
                style={{marginLeft: '8px', fontSize: '12px'}}
                title={persistenceMode === 'eternal'
                  ? 'Eternal mode: if the VM is reclaimed, the session is automatically recovered on your next request (state restored from S3).'
                  : 'Checkpoint mode: session state is auto-saved to S3 on suspend/expiry. Restore it on your next visit.'}
              >
                {persistenceMode === 'eternal' ? 'eternal' : 'checkpoint'}
              </span>
            )}
          </div>
          <div className="connection-desc">
            {tab.status === 'connected'
              ? 'Launch a new MicroVM for this notebook.'
              : persistenceMode === 'checkpoint'
                ? `Each notebook connects to its own sandbox. The VM expires after ${maxDurationText}; state is auto-saved to S3 before expiry.`
                : 'Each notebook connects to its own execution sandbox for isolated, stateful Python.'}
          </div>
        </div>

        {/* ── Body (the only part that scrolls) ── */}
        <div className="cp-body" ref={bodyRef}>
          {/* Connected status first when already connected */}
          {tab.status === 'connected' && (
            <div className="connection-info connection-info-top">
              <div className="connection-info-content">
                <div className="connection-info-head">
                  <div className="connection-info-status">Connected</div>
                  <button className="dismiss-btn dismiss-btn-primary" onClick={onDismiss}>
                    <IconCheck className="cp-btn-icon" strokeWidth={1.75} aria-hidden="true" focusable="false" /><span>Dismiss</span>
                  </button>
                </div>
                {tab.microvmId && (
                  <div className="connection-info-details">
                    <div className="connection-info-row">
                      <span className="connection-info-label">MicroVM</span>
                      <code className="connection-info-id">{tab.microvmId}</code>
                    </div>
                    <div className="connection-info-row">
                      <span className="connection-info-label">Spec</span>
                      <span className="connection-info-spec">
                        {tab.microvmMemory
                          ? <SpecText arch={tab.microvmArch} memoryMiB={tab.microvmMemory} />
                          : 'Fetching...'}
                      </span>
                    </div>
                    {tab.idleTimeoutSeconds && (
                      <div className="connection-info-row">
                        <span className="connection-info-label">Idle Suspend</span>
                        <span className="connection-info-spec">
                          {tab.idleTimeoutSeconds >= 3600
                            ? `${Math.floor(tab.idleTimeoutSeconds / 3600)}h ${Math.floor((tab.idleTimeoutSeconds % 3600) / 60)}m`
                            : tab.idleTimeoutSeconds >= 60
                              ? `${Math.floor(tab.idleTimeoutSeconds / 60)} minute${Math.floor(tab.idleTimeoutSeconds / 60) > 1 ? 's' : ''}`
                              : `${tab.idleTimeoutSeconds} seconds`}
                        </span>
                      </div>
                    )}
                    {tab.sessionId && (
                      <div className="connection-info-row">
                        <span className="connection-info-label">Session</span>
                        <code className="connection-info-id">{tab.sessionId}</code>
                      </div>
                    )}
                    {tab.checkpointEnabled && (
                      <div className="connection-info-row">
                        <span className="connection-info-label">Restore</span>
                        <span className="connection-info-spec">Enabled: state saves to S3 on termination</span>
                      </div>
                    )}
                  </div>
                )}
                <div className="connection-info-note">
                  Variables persist across cell executions. Namespace variables,
                  env vars &amp; secrets are managed in the <strong>Secrets &amp; Env</strong> panel.
                </div>
              </div>
            </div>
          )}

          {mode === null && <div className="connection-detecting">Detecting environment...</div>}

          {mode === 'local' && (
            <div className="connection-hint">
              Connects directly to the FastAPI sandbox on <code>localhost:{BACKEND_URL.split(':').pop()}</code>.
              No auth token required.
            </div>
          )}

          {mode === 'microvm' && (
            <div className="cp-sections">
              {/* ── Restore the saved session ──────────────────────────────
                  Shown only when a checkpoint of this notebook's session exists.
                  By default the restore runs on the SAVED memory tier + arch (the
                  server pins them). "Restore on a different instance type" lets
                  the user pick another arch or tier, with warnings
                  (services/restoreChoice.js); it never blocks the restore. */}
              {hasRestore && (
                <DialogSection
                  id={`${uid}-restore`}
                  title="Restore from previous session"
                  summary={<SummaryText parts={restoreSectionSummary({ savedArch, savedMemoryMiB, choice: restoreChoice })} />}
                  open={section === SECTION_RESTORE}
                  onToggle={() => toggleSection(SECTION_RESTORE)}
                >
                  <div className="cp-card cp-saved">
                    <div className="cp-label">Saved instance</div>
                    <div className="cp-saved-spec">
                      {savedMemoryMiB
                        // One span = one flex item, so the badge sits in the text run.
                        ? <span><SpecText arch={savedArch} memoryMiB={savedMemoryMiB} /></span>
                        : <span>Saved tier</span>}
                      <span className="cp-tag" title="A restore uses the instance type the session was saved on, unless you pick another one below.">saved</span>
                    </div>
                    <div className="cp-meta">
                      Session <code>{restore.sessionTail}</code>
                      {ckptSize && <> · checkpoint {ckptSize}</>}
                    </div>
                  </div>
                  {savedArch && savedMemoryMiB && (
                    <div className="restore-alt">
                      <button
                        type="button"
                        className="restore-alt-toggle"
                        aria-expanded={restoreOpen}
                        aria-controls={`${uid}-alt`}
                        onClick={() => setRestoreOpen(o => !o)}
                      >
                        <IconChevronRight className="restore-alt-chev" width={12} height={12} aria-hidden="true" focusable="false" />
                        <span className="restore-alt-text">Restore on a different instance type</span>
                        <span className="restore-alt-state">{restoreChoice.changed ? 'changed' : 'default: saved'}</span>
                      </button>
                      <div id={`${uid}-alt`} className="restore-alt-body" hidden={!restoreOpen}>
                        <label className="restore-alt-field">
                          <span className="cp-label">CPU architecture</span>
                          {/* Same as Launch: the icon for the current choice beside the select. */}
                          <span className="launch-spec-select-row">
                            <select
                              className="launch-spec-select"
                              value={restoreChoice.arch}
                              onChange={(e) => handleRestoreArchChange(e.target.value)}
                            >
                              {[...new Set([savedArch, ...availableArches].filter(Boolean))].map(a => (
                                <option key={a} value={a}>
                                  {`${archOptionLabel(a)}${a === savedArch ? ' (saved)' : ''}`}
                                </option>
                              ))}
                            </select>
                            <ArchBadge arch={restoreChoice.arch} iconOnly />
                          </span>
                        </label>
                        <label className="restore-alt-field">
                          <span className="cp-label">Memory tier<ArchBadge arch={restoreChoice.arch || undefined} /></span>
                          <select
                            className="launch-spec-select"
                            value={restoreChoice.memoryMiB || ''}
                            onChange={(e) => setRestoreMemory(Number(e.target.value))}
                          >
                            {restoreTiersFor(restoreChoice.arch).map(m => (
                              <option key={m} value={m}>
                                {`${tierLabel(m)}${restoreChoice.arch === savedArch && m === savedMemoryMiB ? ' (saved)' : ''}`}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                    </div>
                  )}
                  {/* The choice and its warnings. Warn only: never blocks the restore. */}
                  {notes.length > 0 && (
                    <div className="restore-notes" role="status">
                      {notes.map(n => (
                        <div key={n.kind} className={`restore-note restore-note-${n.level}`} data-kind={n.kind}>
                          <div className="restore-note-title">{n.title}</div>
                          {n.message && <div className="restore-note-msg">{n.message}</div>}
                        </div>
                      ))}
                    </div>
                  )}
                </DialogSection>
              )}

              <DialogSection
                id={`${uid}-launch`}
                title="Launch a new VM"
                summary={hasRestore ? <SummaryText parts={launchSectionSummary({ memoryMiB: launchMemory, arch: launchArch })} /> : null}
                open={section === SECTION_LAUNCH}
                single={!hasRestore}
                onToggle={() => toggleSection(SECTION_LAUNCH)}
              >
                {launchConfig}
              </DialogSection>
            </div>
          )}
        </div>

        {/* ── Pinned above the footer: the strongest warning, the launch
            progress and the last error (kept on the tab as _connectError). ── */}
        {(showPin || busy || error) && (
          <div className="cp-pinzone">
            {showPin && (
              // aria-hidden: the same note is announced once, in the body.
              // One line (the short text, ellipsis when narrow); the full note
              // stays in the body.
              <div
                className={`cp-pin restore-note restore-note-${pinned.level}`}
                aria-hidden="true"
                data-kind={pinned.kind}
                title={[pinned.title, pinned.message].filter(Boolean).join('. ')}
              >
                <span className="restore-note-title">{pinned.short || pinned.title}</span>
              </div>
            )}
            {busy && (
              <div className="connection-launching">
                Provisioning a new Firecracker VM... This takes a few seconds.
              </div>
            )}
            {error && (
              <div className="connection-error" role="alert">
                {error}
              </div>
            )}
          </div>
        )}

        {/* ── Footer (fixed): hint, Dismiss, primary action ── */}
        <div className="cp-foot">
          <span className="cp-foot-hint">{hint}</span>
          <div className="cp-foot-actions">
            {tab.status !== 'connected' && <button className="dismiss-btn" onClick={onDismiss}>Dismiss</button>}
            {primary}
          </div>
        </div>
      </div>
    </div>
  )
}
