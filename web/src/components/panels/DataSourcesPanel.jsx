import { useState, useMemo, useEffect, useRef, useCallback } from 'react'
import { ConfirmModal } from '../Modal'
import { IconUpload, IconDatabase, IconRefresh, IconX, IconNotebook, IconTable, IconCode, IconFolderOpen, IconCloud, IconServer } from '../Icons'

// Wrap each case-insensitive occurrence of `query` in <mark> so the search term is
// visible in a source's name. Literal match (query is regex-escaped).
function highlightMatch(text, query) {
  const q = (query || '').trim()
  if (!q || !text) return text
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = String(text).split(new RegExp(`(${escaped})`, 'ig'))
  if (parts.length === 1) return text
  const qLc = q.toLowerCase()
  return parts.map((part, i) =>
    part.toLowerCase() === qLc
      ? <mark key={i} className="outline-search-hit">{part}</mark>
      : part
  )
}
// Compact relative age from an ISO timestamp: "just now" / "5m" / "3h" / "2d" /
// "4w" / "5mo" / "2y". Returns '' for a missing/unparseable value so callers can
// skip it. Used to show each file's last-modified in the panel (S3 + local).
function relativeTime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, (Date.now() - t) / 1000)
  if (s < 45) return 'just now'
  const m = s / 60
  if (m < 60) return `${Math.round(m)}m ago`
  const h = m / 60
  if (h < 24) return `${Math.round(h)}h ago`
  const d = h / 24
  if (d < 7) return `${Math.round(d)}d ago`
  if (d < 30) return `${Math.round(d / 7)}w ago`
  if (d < 365) return `${Math.round(d / 30)}mo ago`
  return `${Math.round(d / 365)}y ago`
}

// Secondary meta line for a Sandbox (/tmp) file row: size, plus a relative
// last-modified age when the VM reported one (mirrors the S3 rows).
function localFileMeta(file) {
  const rel = relativeTime(file.last_modified)
  return rel ? `${file.size} · ${rel}` : file.size
}

import PanelExpandButton from './PanelExpandButton'
import FileTypeIcon from './datasources/FileTypeIcon'
import SchemaExpander from './datasources/SchemaExpander'
import InsertActions from './datasources/InsertActions'
import EntityDocBadge from './datasources/EntityDocBadge'
import { PUBLIC_APIS } from './datasources/publicApis'

// Icon for a source-type group header, keyed by the provider's metadata `icon`.
// A new/unknown icon key falls back to a generic database glyph.
function GroupIcon({ iconKey, ...props }) {
  switch (iconKey) {
    // S3 = cloud object storage (the old IconBucket read like a trash can).
    case 's3': return <IconCloud {...props} className="sidebar-icon-s3" />
    case 'dynamodb': return <IconDatabase {...props} className="sidebar-icon-dynamodb" />
    case 'athena': return <IconTable {...props} className="sidebar-icon-athena" />
    case 'local': return <IconUpload {...props} className="sidebar-icon-file-csv" />
    // Bedrock Knowledge Base = a document/retrieval source → notebook/book glyph.
    case 'bedrock_kb': return <IconNotebook {...props} className="sidebar-icon-bedrock-kb" />
    // SageMaker endpoint = a deployed, callable model → server/compute glyph
    // (distinct from the storage/table sources).
    case 'sagemaker_endpoint': return <IconServer {...props} className="sidebar-icon-sagemaker-endpoint" />
    default: return <IconDatabase {...props} />
  }
}

// Icon for an individual source row (S3 uses a file-type icon based on extension).
function RowIcon({ src }) {
  if (src.source_type === 's3') {
    return <span className="sidebar-file-icon sidebar-icon-s3"><FileTypeIcon filename={src.display_name} /></span>
  }
  if (src.source_type === 'athena') {
    // S3 Tables (Apache Iceberg) ride the Athena source_type but get a distinct
    // tint so they read differently from plain Glue/Athena tables (Option B).
    const iceberg = src.table_format === 'iceberg'
    return (
      <span className={`sidebar-file-icon ${iceberg ? 'sidebar-icon-iceberg' : 'sidebar-icon-athena'}`}>
        <IconTable width={13} height={13} />
      </span>
    )
  }
  if (src.source_type === 'dynamodb') {
    // A DynamoDB row is a TABLE → use the table glyph (matching Athena tables). The
    // DB/cylinder glyph stays on the DYNAMODB group header, not on the leaf rows.
    return <span className="sidebar-file-icon sidebar-icon-dynamodb"><IconTable width={13} height={13} /></span>
  }
  if (src.source_type === 'bedrock_kb') {
    // A Knowledge Base leaf is the KB itself (a document collection) → book glyph.
    return <span className="sidebar-file-icon sidebar-icon-bedrock-kb"><IconNotebook width={13} height={13} /></span>
  }
  if (src.source_type === 'sagemaker_endpoint') {
    // A deployed endpoint → server/compute glyph (you invoke it, not read it), with
    // a small control-plane status dot: green=InService, amber=provisioning,
    // red=Failed/OutOfService, gray=Deleting/unknown.
    const { cls, label } = smEndpointStatusDot(src.status)
    return (
      <span className="sidebar-file-icon sidebar-icon-sagemaker-endpoint sm-ep-icon-wrap" title={label}>
        <IconServer width={13} height={13} />
        <span className={`sm-ep-status-dot ${cls}`} />
      </span>
    )
  }
  return <span className="sidebar-file-icon"><IconDatabase width={13} height={13} /></span>
}

// Map a SageMaker EndpointStatus (from the control plane, carried in src.status) to
// a status-dot color class + human label. Serverless endpoints that scaled to zero
// are still 'InService' (green) — SageMaker has no separate "dormant" state.
function smEndpointStatusDot(status) {
  const s = String(status || '')
  if (s === 'InService') return { cls: 'sm-ep-dot-active', label: 'InService — ready to invoke' }
  if (s === 'Failed' || s === 'OutOfService') return { cls: 'sm-ep-dot-failed', label: `${s}` }
  if (s === 'Deleting') return { cls: 'sm-ep-dot-gone', label: 'Deleting' }
  if (['Creating', 'Updating', 'SystemUpdating', 'RollingBack'].includes(s))
    return { cls: 'sm-ep-dot-pending', label: `${s} — provisioning` }
  // Stopped = an instance endpoint torn down by sm_stop_endpoint; its config + model
  // remain, so it's resumable via Start. A distinct (grey, non-pulsing) state.
  if (s === 'Stopped') return { cls: 'sm-ep-dot-stopped', label: 'Stopped — start to resume' }
  return { cls: 'sm-ep-dot-gone', label: s || 'unknown status' }
}

// Short mode pill shown next to an endpoint name: 'Serverless' ($0 idle) vs
// 'Instance' (dedicated, bills continuously). Returns null when the mode is unknown
// (e.g. a foreign endpoint) so we don't render a misleading badge.
function endpointModeBadge(src) {
  const mode = src.mode
  if (mode === 'serverless') {
    return { text: 'Serverless', cls: 'ds-ep-badge-serverless',
             title: 'Serverless endpoint — scales to zero, $0 while idle' }
  }
  if (mode === 'instance') {
    // Badge is just the MODE ("Instance") — same visual weight as the "Serverless"
    // pill. The instance TYPE lives on the meta line (detail: "Creating · ml.m5.large
    // · us-west-2"), so including it in the badge too both duplicated it and made the
    // instance pill wider than the serverless one. Title still names the type.
    const it = src.instance_type ? ` (${src.instance_type})` : ''
    return { text: 'Instance', cls: 'ds-ep-badge-instance',
             title: `Instance endpoint${it} — dedicated compute, bills continuously while running` }
  }
  return null
}

// Secondary meta line for a source row, composed from the generic detail plus a
// type-specific hint (region for DynamoDB, database for Athena).
function sourceMeta(src) {
  let meta = src.detail || src.size || ''
  if (src.source_type === 'dynamodb' && src.region) meta = meta ? `${meta} · ${src.region}` : src.region
  // S3 objects carry a last_modified ISO ts → append a compact relative age.
  if (src.source_type === 's3' && src.last_modified) {
    const rel = relativeTime(src.last_modified)
    if (rel) meta = meta ? `${meta} · ${rel}` : rel
  }
  // NOTE: we deliberately do NOT append the Athena database to the meta line. The
  // database is already visible in BOTH views: grouped nests the row under a
  // `microvm_demo_db` folder, and flat renders the fully-qualified name
  // `microvm_demo_db.customers`. Appending "· microvm_demo_db" duplicated it either
  // way, so the subtitle stays just the column count (e.g. "8 cols").
  return meta
}

// Entity-doc lookup key. entityDocMap is keyed by the catalog entry's source_id,
// which for EVERY source type (including DynamoDB) is the BARE id the provider
// emits (e.g. "ecommerce-reviews"). The proxy handles the storage-side quirk that
// DynamoDB docs are persisted under "dynamodb.<table>" (it retries that prefix in
// _enrich_catalog_entries) and still sets has_entity_doc on the bare entry. So the
// frontend must look up by the bare source_id too — a prior "dynamodb." prefix here
// re-introduced the mismatch and hid the DynamoDB sparkle.
function entityDocKey(src) {
  return src.source_id
}

// Grouping seam: return the folder path segments for a source in grouped view.
// This is the ONE place per-source-type hierarchy is defined, so adding a new
// hierarchy later (e.g. Postgres: [server, database]) is a localized change and
// the generic tree renderer picks it up unchanged.
//   - s3   : the key prefix folders (samples/foo.csv -> ["samples"])
//   - local: the on-disk directory (/tmp/foo.csv -> ["/tmp"]); derived from the
//            real path so widening the VM scan later surfaces new folders for free
//   - athena: the database (microvm_demo_db.customers -> ["microvm_demo_db"])
//   - others (dynamodb, ...): [] -> flat, no grouping
// Returns { segments: string[], leaf: string } — leaf is the row's short label.
// A trained-model artifact (by extension) isn't SQL-queryable — used to hide the
// SQL insert on Sandbox (local) rows, mirroring the proxy's `queryable=false` flag
// for S3 model objects. Kept in the front-end because local files render from the
// `uploadedFiles` prop, not through the generic (queryable-aware) row renderer.
const MODEL_FILE_EXTS = new Set(['joblib', 'jbl', 'pkl', 'pickle', 'sav', 'onnx'])
function isModelFile(name) {
  const n = (name || '').toLowerCase()
  if (n.endsWith('model.tar.gz')) return true
  const ext = n.includes('.') ? n.split('.').pop() : ''
  return MODEL_FILE_EXTS.has(ext)
}

function getGroupPath(src) {
  const type = src.source_type
  if (type === 's3') {
    const parts = (src.display_name || src.source_id || '').split('/').filter(Boolean)
    const leaf = parts.length ? parts.pop() : (src.display_name || src.source_id)
    return { segments: parts, leaf }
  }
  if (type === 'local') {
    // NOTE: currently a forward-looking seam. The Sandbox section renders local
    // files from the `uploadedFiles` prop (its own shape) with a hardcoded /tmp
    // folder, and the generic cloud loop filters out `local` — so this branch is
    // not hit today. It's here so that if local files ever flow through the
    // generic `sources` array, grouping-by-directory works with no extra wiring.
    // source_id is the absolute path (e.g. /tmp/sales.csv). Folder = dirname.
    const path = src.source_id || src.display_name || ''
    const parts = path.split('/').filter(Boolean)
    const leaf = parts.length ? parts.pop() : path
    const dir = '/' + parts.join('/')  // e.g. "/tmp"
    return { segments: parts.length ? [dir] : [], leaf }
  }
  if (type === 'athena') {
    const db = src.database || (src.source_id || '').split('.')[0]
    const leaf = src.display_name || (src.source_id || '').split('.').slice(1).join('.') || src.source_id
    return { segments: db ? [db] : [], leaf }
  }
  if (type === 'aurora') {
    // Aurora source_id is `database.schema.table` (3-part). Group by DATABASE so
    // the tree is provider → database (e.g. "microvm_db") → schema.table, mirroring
    // how Athena nests provider → database → table. The leaf keeps the SCHEMA-
    // qualified name (e.g. "public.v_nb_notebook") so objects are distinguishable
    // across schemas — that's display_name (schema.table); we only strip the
    // leading "database." prefix.
    const parts = (src.source_id || '').split('.')
    const db = src.database || src.metadata?.database || (parts.length >= 3 ? parts[0] : '')
    const leaf = src.display_name
      || (parts.length >= 3 ? parts.slice(1).join('.') : (src.source_id))
    return { segments: db ? [db] : [], leaf }
  }
  return { segments: [], leaf: src.display_name || src.source_id }
}

// Build a nested folder tree from a flat source list using getGroupPath. Each
// node is { folders: Map, files: [] }; leaves carry a _leafLabel. Source-agnostic.
function buildTree(srcs) {
  const root = { folders: new Map(), files: [] }
  for (const src of srcs) {
    const { segments, leaf } = getGroupPath(src)
    let node = root
    for (const part of segments) {
      if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), files: [] })
      node = node.folders.get(part)
    }
    node.files.push({ ...src, _leafLabel: leaf })
  }
  return root
}

// Count all files in a tree node (recursively) — for the folder file-count badge.
function countTreeFiles(node) {
  let n = node.files.length
  for (const child of node.folders.values()) n += countTreeFiles(child)
  return n
}

export default function DataSourcesPanel({
  onEndpointLifecycle,
  uploadedFiles,
  onUploadFile,
  onDeleteFile,
  onDeleteS3File,
  onInsertCode,
  activeTab,
  sources = [],
  sourceTypes = [],
  dsLoading,
  catalogEntries = [],
  fetchDataSources,
  onClose,
  expanded,
  onToggleExpand,
}) {
  const [sandboxExpanded, setSandboxExpanded] = useState(true)
  const [publicApisExpanded, setPublicApisExpanded] = useState(true)
  const [search, setSearch] = useState('')
  // Pending destructive delete awaiting in-app confirmation (styled ConfirmModal,
  // not a native window.confirm). Currently used for SageMaker endpoints — a live,
  // billed, irreversible teardown. Holds the source object; null = no dialog.
  const [deleteTarget, setDeleteTarget] = useState(null)
  // Pending endpoint STOP awaiting confirmation. Stop tears down a live, billed
  // endpoint (resumable via Start) — worth a confirm, but not danger-styled since it
  // keeps the config + model. Holds the source object; null = no dialog.
  const [stopTarget, setStopTarget] = useState(null)

  // --- Per-row PENDING action state (spinner + disabled controls) --------------
  // Keyed by a stable row id → the in-flight action: 'deleting' | 'stopping' |
  // 'starting'. Set synchronously on click so the row shows immediate feedback (a
  // spinner, no delete/stop/start buttons) instead of looking unchanged while the
  // async control-plane call + background re-discovery run. Cleared by the reconcile
  // effect below when the row's REAL state catches up to the action (row removed, or
  // status moved), so the spinner tracks actual state change — exactly the ask. A
  // per-row safety timeout is a backstop so a dropped event can't strand a spinner.
  const [pendingActions, setPendingActions] = useState({})   // { rowKey: 'deleting'|'stopping'|'starting' }
  const pendingTimers = useRef({})
  const PENDING_SAFETY_MS = 90000   // generous: endpoint create/delete can take minutes

  const rowKey = (src) => `${src.source_type}:${src.source_id}`
  const fileKey = (name) => `local:/tmp/${name}`

  // ONE follow-up re-check after a row action. A refresh right after delete/stop
  // often catches the endpoint STILL present (SageMaker lingers it as 'Deleting'
  // for a few seconds); this nudge catches a quick disappearance / status flip, and
  // the 30 s transitional auto-poll in useDataSources covers the slower ones. It is
  // NON-forced (detail.force=false: suspend-gated, no catalog resync), coalesced
  // with other refresh events, and a newer action replaces a pending nudge.
  const NUDGE_REFRESH_MS = 6000
  const refreshNudgeTimers = useRef([])
  const nudgeRefresh = useCallback(() => {
    for (const t of refreshNudgeTimers.current) clearTimeout(t)
    refreshNudgeTimers.current = [setTimeout(
      () => window.dispatchEvent(new CustomEvent('refresh-datasources', { detail: { force: false } })),
      NUDGE_REFRESH_MS,
    )]
  }, [])

  const markPending = useCallback((key, action) => {
    setPendingActions(prev => ({ ...prev, [key]: action }))
    if (pendingTimers.current[key]) clearTimeout(pendingTimers.current[key])
    pendingTimers.current[key] = setTimeout(() => {
      setPendingActions(prev => {
        if (!(key in prev)) return prev
        const next = { ...prev }; delete next[key]; return next
      })
      delete pendingTimers.current[key]
    }, PENDING_SAFETY_MS)
  }, [])

  const clearPending = useCallback((key) => {
    setPendingActions(prev => {
      if (!(key in prev)) return prev
      const next = { ...prev }; delete next[key]; return next
    })
    if (pendingTimers.current[key]) {
      clearTimeout(pendingTimers.current[key]); delete pendingTimers.current[key]
    }
  }, [])

  // Clear a pending flag the moment the row's REAL state reflects the action:
  //   deleting → the row is no longer in the live lists (gone from discovery)
  //   stopping → the endpoint's status became 'Stopped' (or it's gone)
  //   starting → the endpoint left the stopped/settled state (now Creating/InService)
  // This ties the spinner to actual state, not a timer (the timer is only a backstop).
  useEffect(() => {
    const liveEndpoints = {}
    for (const s of sources) {
      if (s.source_type === 'sagemaker_endpoint') liveEndpoints[rowKey(s)] = s.status
    }
    const liveSourceKeys = new Set(sources.map(rowKey))
    const liveFileKeys = new Set((uploadedFiles || []).map(f => fileKey(f.name)))

    for (const [key, action] of Object.entries(pendingActions)) {
      const isEndpoint = key.startsWith('sagemaker_endpoint:')
      if (action === 'deleting') {
        const present = liveSourceKeys.has(key) || liveFileKeys.has(key)
        if (!present) clearPending(key)
      } else if (action === 'stopping') {
        const st = liveEndpoints[key]
        if (st === undefined || st === 'Stopped' || st === 'Failed') clearPending(key)
      } else if (action === 'starting') {
        const st = liveEndpoints[key]
        // Settled once it's no longer 'Stopped' (it moved to Creating/InService/etc.).
        if (st !== undefined && st !== 'Stopped') clearPending(key)
      }
      void isEndpoint
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources, uploadedFiles])

  // Clear every timer on unmount.
  useEffect(() => () => {
    for (const t of Object.values(pendingTimers.current)) clearTimeout(t)
    for (const t of refreshNudgeTimers.current) clearTimeout(t)
  }, [])

  // Display mode for the source lists (mirrors the Packages panel toggle).
  // 'grouped' (default) nests by natural hierarchy — Athena by database, S3 by
  // folder, local by /tmp path. 'flat' shows every item as a fully-qualified
  // single row. Purely a client-side view preference.
  const [viewMode, setViewMode] = useState('grouped') // 'grouped' | 'flat'
  // Collapse state for the registry-driven cloud source groups, keyed by source_type.
  const [collapsedGroups, setCollapsedGroups] = useState(() => new Set())
  // Collapse state for S3 folder nodes in the tree view, keyed by full folder path.
  const [collapsedFolders, setCollapsedFolders] = useState(() => new Set())

  const searchLc = search.trim().toLowerCase()

  const toggleGroup = (st) => setCollapsedGroups(prev => {
    const next = new Set(prev)
    next.has(st) ? next.delete(st) : next.add(st)
    return next
  })

  const toggleFolder = (path) => setCollapsedFolders(prev => {
    const next = new Set(prev)
    next.has(path) ? next.delete(path) : next.add(path)
    return next
  })

  // Group discovered sources by source_type for generic rendering.
  const sourcesByType = useMemo(() => {
    const map = {}
    for (const src of sources) {
      (map[src.source_type] ||= []).push(src)
    }
    return map
  }, [sources])

  // Build a lookup map from source_id → entity doc metadata (from the enriched catalog)
  const entityDocMap = useMemo(() => {
    const map = {}
    for (const entry of catalogEntries) {
      if (entry.has_entity_doc) {
        map[entry.source_id] = {
          business_description: entry.business_description || '',
          quality_flags: entry.quality_flags || [],
        }
      }
    }
    return map
  }, [catalogEntries])

  const handleFileUpload = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.csv,.xlsx,.xls,.parquet,.json,.txt'
    input.multiple = true
    input.onchange = (e) => {
      Array.from(e.target.files || []).forEach(file => {
        onUploadFile(file)
      })
    }
    input.click()
  }

  // Render a single discovered-source row (shared by flat groups + S3 tree leaves).
  // displayName overrides the visible label (e.g. just the filename in the tree);
  // indentPx adds left padding for tree nesting.
  const renderSourceRow = (src, displayName = null, indentPx = 0) => {
    const key = `${src.source_type}-${src.source_id}`
    const eKey = entityDocKey(src)
    // Offer the SQL insert button ONLY for source types that support SQL (from the
    // provider registry metadata). A retrieve-only source like a Bedrock Knowledge
    // Base (supports_sql=false) is Python-only; showing SQL would insert a
    // "No SQL snippet available" placeholder. Default true if metadata is missing.
    const stMeta = sourceTypes.find(t => t.source_type === src.source_type)
    // SQL is offered when BOTH the source TYPE supports it AND this specific row is
    // queryable. A model artifact (.joblib/.onnx/SageMaker model.tar.gz) sets
    // queryable=false so we hide SQL for it while keeping SQL for data files in the
    // same S3 group. `queryable` is absent for most rows → treated as true.
    const rowSupportsSql = (stMeta ? stMeta.supports_sql !== false : true)
      && src.queryable !== false
    return (
      <div
        key={key}
        className="sidebar-file-item sidebar-ds-row"
        style={indentPx ? { paddingLeft: `${indentPx}px` } : undefined}
        title={`Insert code for ${src.display_name}`}
      >
        <RowIcon src={src} />
        <div className="sidebar-file-info">
          <span className="sidebar-file-name">
            {displayName || src.display_name}
            {src.table_format === 'iceberg' && (
              <span className="ds-iceberg-badge" title="Amazon S3 Tables (Apache Iceberg) — supports time-travel & snapshots">Iceberg</span>
            )}
          </span>
          {/* Endpoint mode pill lives on its OWN line BELOW the name, not inline
              after it: endpoint names are long (microvm-ep-<name>-<ts>) and an inline
              badge gets pushed past the panel's right edge and clipped, so the user
              never sees whether it's Serverless or Instance. On its own row it's
              always fully visible regardless of name length. */}
          {src.source_type === 'sagemaker_endpoint' && (() => {
            const b = endpointModeBadge(src)
            return b ? (
              <span className="ds-ep-badge-line">
                <span className={`ds-ep-badge ${b.cls}`} title={b.title}>{b.text}</span>
              </span>
            ) : null
          })()}
          <span className="sidebar-file-meta">{sourceMeta(src)}</span>
        </div>
        {/* While an action is in flight, replace the row's buttons with a spinner so
            the user sees immediate feedback and can't re-fire. Cleared when the row's
            real state catches up (gone / status changed). */}
        {(() => {
          const pending = pendingActions[rowKey(src)]
          if (!pending) return null
          const label = pending === 'deleting' ? 'Deleting…'
            : pending === 'stopping' ? 'Stopping…' : 'Starting…'
          return <span className="sidebar-item-spinner ds-row-spinner" title={label} aria-label={label} />
        })()}
        {/* Stop / Start — INSTANCE endpoints only (serverless has no lifecycle).
            Stop when running (InService), Start when Stopped. Stop interrupts a live,
            billed endpoint → route through the ConfirmModal; Start is a safe resume so
            it fires directly. Hidden while this row has an action in flight. */}
        {!pendingActions[rowKey(src)] && src.source_type === 'sagemaker_endpoint' && src.mode === 'instance' && onEndpointLifecycle && (
          <>
            {src.status === 'InService' && (
              <button
                className="sidebar-file-action"
                onClick={(e) => { e.stopPropagation(); setStopTarget(src) }}
                title="Stop endpoint (pause billing; keeps config + model to resume)"
              >
                ⏸
              </button>
            )}
            {src.status === 'Stopped' && (
              <button
                className="sidebar-file-action"
                onClick={(e) => {
                  e.stopPropagation()
                  markPending(rowKey(src), 'starting')
                  onEndpointLifecycle(src, 'start')
                  nudgeRefresh()
                }}
                title="Start endpoint (recreate from its retained config)"
              >
                ▶
              </button>
            )}
          </>
        )}
        {!pendingActions[rowKey(src)] && src.deletable && onDeleteS3File && (
          <button
            className="sidebar-file-delete"
            onClick={(e) => {
              e.stopPropagation()
              // A SageMaker endpoint is a live, billed, irreversible teardown →
              // route through the styled in-app ConfirmModal. An S3 file delete is
              // cheap/recoverable enough to delete immediately (existing behavior).
              if (src.source_type === 'sagemaker_endpoint') {
                setDeleteTarget(src)
              } else {
                markPending(rowKey(src), 'deleting')
                onDeleteS3File(src)
                nudgeRefresh()
              }
            }}
            title={src.source_type === 'sagemaker_endpoint' ? 'Delete endpoint (tears down endpoint + config + model)' : 'Delete file from S3'}
          >
            <IconX width={11} height={11} />
          </button>
        )}
        {/* Inline insert actions — always mounted (discoverable), dimmed at rest
            and revealed on row hover. No click-to-discover needed. */}
        <InsertActions
          sourceType={src.source_type}
          sourceId={src.source_id}
          onInsert={onInsertCode}
          showSql={rowSupportsSql}
        />
        {/* Schema expander. For a Bedrock KB this shows the RETRIEVAL RESULT shape
            (text/score/source_uri/source_type/metadata) rather than stored columns —
            the useful thing to show for a retrieve-only source. */}
        <SchemaExpander sourceType={src.source_type} sourceId={src.source_id} onInsertCode={onInsertCode} />
        {entityDocMap[eKey] && (
          <EntityDocBadge
            sourceId={eKey}
            businessDescription={entityDocMap[eKey].business_description}
            qualityFlags={entityDocMap[eKey].quality_flags}
            sessionId={activeTab?.sessionId}
          />
        )}
      </div>
    )
  }

  // Render a folder tree node recursively: folders (collapsible) then files.
  // Source-agnostic — used for S3 prefixes, Athena databases, and local dirs.
  // `keyPrefix` namespaces the collapse key so folders of the same name across
  // source types (or an Athena db named like an S3 folder) never collide.
  // `folderIconClass` tints the folder glyph to match the source type.
  const renderTreeNode = (node, pathPrefix, depth, keyPrefix, folderIconClass) => {
    const els = []
    for (const [folderName, child] of node.folders) {
      const fullPath = pathPrefix ? `${pathPrefix}/${folderName}` : folderName
      const collapseKey = `${keyPrefix}:${fullPath}`
      const collapsed = collapsedFolders.has(collapseKey)
      els.push(
        <div
          key={`fold-${collapseKey}`}
          className="ds-tree-folder"
          style={{ paddingLeft: `${12 + depth * 14}px` }}
          onClick={() => toggleFolder(collapseKey)}
          title={fullPath}
        >
          <span className="ds-tree-chevron">{collapsed ? '▸' : '▾'}</span>
          {/* Athena folders ARE databases and Aurora folders ARE schemas →
              show a DB glyph, not a generic folder. S3 prefixes / local dirs are
              true folders, so keep the folder icon. */}
          {keyPrefix === 'athena' || keyPrefix === 'aurora'
            ? <IconDatabase width={12} height={12} className={folderIconClass} />
            : <IconFolderOpen width={12} height={12} className={folderIconClass} />}
          <span className="ds-tree-folder-name">{folderName}</span>
          <span className="ds-tree-folder-count">{countTreeFiles(child)}</span>
        </div>
      )
      if (!collapsed) els.push(...renderTreeNode(child, fullPath, depth + 1, keyPrefix, folderIconClass))
    }
    for (const src of node.files) {
      els.push(renderSourceRow(src, src._leafLabel, 12 + depth * 14 + 16))
    }
    return els
  }

  // Flat, cross-section search results. When a query is active we bypass the
  // grouped/tree view entirely (like the Outline does when filtering) and show a
  // single flat list of every matching item across Sandbox files, cloud sources,
  // and Public APIs — matched on their visible/identifying text and highlighted.
  const renderSearchResults = () => {
    const q = searchLc

    // Sandbox files — match the filename.
    const fileMatches = (uploadedFiles || []).filter(f => (f.name || '').toLowerCase().includes(q))

    // Cloud sources (all types) — match name, fully-qualified id, detail, database.
    const cloudMatches = sources
      .filter(s => s.source_type !== 'local')
      .filter(s => [s.display_name, s.source_id, s.detail, s.database]
        .some(v => v && String(v).toLowerCase().includes(q)))

    // Public APIs — match name + description.
    const apiMatches = PUBLIC_APIS.filter(a =>
      (a.name || '').toLowerCase().includes(q) || (a.desc || '').toLowerCase().includes(q))

    const total = fileMatches.length + cloudMatches.length + apiMatches.length
    if (total === 0) {
      return (
        <div className="sidebar-empty-inline" style={{ padding: '12px' }}>
          No data sources match “{search.trim()}”.
        </div>
      )
    }

    return (
      <>
        {fileMatches.length > 0 && (
          <>
            <div className="sidebar-subheader">
              <IconUpload width={11} height={11} className="sidebar-icon-file-csv" /> Sandbox Files
              <span className="sidebar-subheader-count">{fileMatches.length}</span>
            </div>
            {fileMatches.map(file => (
              <div
                key={file.name}
                className="sidebar-file-item sidebar-ds-row"
                title={`Insert code for '/tmp/${file.name}'`}
              >
                <span className="sidebar-file-icon sidebar-icon-file-csv">
                  <FileTypeIcon filename={file.name} />
                </span>
                <div className="sidebar-file-info">
                  <span className="sidebar-file-name" title={`/tmp/${file.name}`}>{highlightMatch(file.name, search)}</span>
                  <span className="sidebar-file-meta">{file.status === 'uploading' ? 'uploading…' : localFileMeta(file)}</span>
                </div>
                {activeTab?.sessionId && (
                  <InsertActions sourceType="local" sourceId={`/tmp/${file.name}`} onInsert={onInsertCode} showSql={!isModelFile(file.name)} />
                )}
                {activeTab?.sessionId && (
                  <SchemaExpander sourceType="local" sourceId={`/tmp/${file.name}`} onInsertCode={onInsertCode} sessionId={activeTab.sessionId} />
                )}
              </div>
            ))}
          </>
        )}

        {cloudMatches.length > 0 && (
          <>
            <div className="sidebar-subheader">
              <IconDatabase width={11} height={11} /> Cloud Sources
              <span className="sidebar-subheader-count">{cloudMatches.length}</span>
            </div>
            {/* Fully-qualified label (like flat view) with the match highlighted, so
                results are unambiguous across databases/folders/schemas. */}
            {cloudMatches.map(src => {
              const label = (src.source_type === 'athena' || src.source_type === 'aurora')
                ? src.source_id
                : (src.display_name || src.source_id)
              return renderSourceRow(src, highlightMatch(label, search))
            })}
          </>
        )}

        {apiMatches.length > 0 && (
          <>
            <div className="sidebar-subheader">
              🌐 Public APIs
              <span className="sidebar-subheader-count">{apiMatches.length}</span>
            </div>
            {apiMatches.map(api => (
              <div key={api.id} className="sidebar-file-item sidebar-ds-row" title={api.desc}>
                <span className="sidebar-file-icon">{api.icon}</span>
                <div className="sidebar-file-info">
                  <span className="sidebar-file-name">{highlightMatch(api.name, search)}</span>
                  <span className="sidebar-file-meta">{highlightMatch(api.desc, search)}</span>
                </div>
                <div className="ds-insert-actions">
                  <button
                    className="ds-insert-btn ds-insert-python"
                    onClick={(e) => { e.stopPropagation(); onInsertCode(api.code, 'code') }}
                    title="Insert Python code for this API"
                  >
                    <IconCode width={11} height={11} /> Python
                  </button>
                </div>
              </div>
            ))}
          </>
        )}
      </>
    )
  }

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Data Sources</span>
        <button
          className={`sidebar-panel-action pkg-view-toggle ${viewMode === 'grouped' ? 'active' : ''}`}
          onClick={() => setViewMode(v => v === 'flat' ? 'grouped' : 'flat')}
          title={viewMode === 'flat' ? 'Group by database / folder' : 'Flat list'}
        >
          ≡
        </button>
        <button className="sidebar-panel-action" onClick={() => fetchDataSources(true)} title="Refresh">
          <IconRefresh width={14} height={14} />
        </button>
        <button className="sidebar-panel-action" onClick={handleFileUpload} title="Upload file">
          <IconUpload width={14} height={14} />
        </button>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      {activeTab && <div className="sidebar-scope-pill"><IconNotebook width={12} height={12} /> {activeTab.name}</div>}
      <div className="outline-search">
        <input
          className="outline-search-input"
          type="text"
          placeholder="Search data sources..."
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
        {searchLc ? renderSearchResults() : (<>
        {/* Sandbox Files */}
        <div className="sidebar-subheader sidebar-subheader-toggle" onClick={() => setSandboxExpanded(!sandboxExpanded)}>
          <IconUpload width={11} height={11} className="sidebar-icon-file-csv" /> Sandbox Files <span className="sidebar-subheader-hint" title="Only files under /tmp on the VM are listed. Files elsewhere in the VM are not shown.">scoped to /tmp</span>
          {uploadedFiles.length > 0 && <span className="sidebar-subheader-count">{uploadedFiles.length} files</span>}
          <span className="sidebar-subheader-chevron">{sandboxExpanded ? '▾' : '▸'}</span>
        </div>
        {sandboxExpanded && uploadedFiles.length > 0 ? (
          <>
            {/* Grouped view nests the files under a collapsible /tmp folder
                (the VM directory they live in); flat view shows the full path
                per row. The folder is path-derived, so if the VM scan is later
                widened beyond /tmp, additional folders appear here for free. */}
            {(() => {
              const grouped = viewMode === 'grouped'
              const tmpCollapsed = collapsedFolders.has('local:/tmp')
              const rowIndent = grouped ? 12 + 16 : 0
              const header = grouped ? (
                <div
                  key="local-tmp-folder"
                  className="ds-tree-folder"
                  style={{ paddingLeft: '12px' }}
                  onClick={() => toggleFolder('local:/tmp')}
                  title="/tmp"
                >
                  <span className="ds-tree-chevron">{tmpCollapsed ? '▸' : '▾'}</span>
                  <IconFolderOpen width={12} height={12} className="sidebar-icon-file-csv" />
                  <span className="ds-tree-folder-name">/tmp</span>
                  <span className="ds-tree-folder-count">{uploadedFiles.length}</span>
                </div>
              ) : null
              if (grouped && tmpCollapsed) return header
              const rows = uploadedFiles.map(file => (
                <div
                  key={file.name}
                  className="sidebar-file-item sidebar-ds-row"
                  style={rowIndent ? { paddingLeft: `${rowIndent}px` } : undefined}
                  title={`Insert code for '/tmp/${file.name}'`}
                >
                  <span className="sidebar-file-icon sidebar-icon-file-csv">
                    <FileTypeIcon filename={file.name} />
                  </span>
                  <div className="sidebar-file-info">
                    <span className="sidebar-file-name" title={`/tmp/${file.name}`}>{grouped ? file.name : `/tmp/${file.name}`}</span>
                    {/* Secondary line = size only. The derived variable name used to
                        be appended here but it just repeats the filename (minus ext),
                        so it was redundant. Keep the transient "uploading…" state. */}
                    <span className="sidebar-file-meta">{file.status === 'uploading' ? 'uploading…' : (pendingActions[fileKey(file.name)] ? 'deleting…' : localFileMeta(file))}</span>
                  </div>
                  {pendingActions[fileKey(file.name)] === 'deleting' ? (
                    <span className="sidebar-item-spinner ds-row-spinner" title="Deleting…" aria-label="Deleting…" />
                  ) : (
                    <button
                      className="sidebar-file-delete"
                      onClick={(e) => {
                        e.stopPropagation()
                        markPending(fileKey(file.name), 'deleting')
                        onDeleteFile(file.name)
                        nudgeRefresh()
                      }}
                      title="Remove"
                    >
                      <IconX width={11} height={11} />
                    </button>
                  )}
                  {activeTab?.sessionId && (
                    <InsertActions sourceType="local" sourceId={`/tmp/${file.name}`} onInsert={onInsertCode} showSql={!isModelFile(file.name)} />
                  )}
                  {activeTab?.sessionId && (
                    <SchemaExpander sourceType="local" sourceId={`/tmp/${file.name}`} onInsertCode={onInsertCode} sessionId={activeTab.sessionId} />
                  )}
                  {entityDocMap[`/tmp/${file.name}`] && (
                    <EntityDocBadge
                      sourceId={`/tmp/${file.name}`}
                      businessDescription={entityDocMap[`/tmp/${file.name}`].business_description}
                      qualityFlags={entityDocMap[`/tmp/${file.name}`].quality_flags}
                      sessionId={activeTab?.sessionId}
                    />
                  )}
                </div>
              ))
              return header ? [header, ...rows] : rows
            })()}
          </>
        ) : sandboxExpanded ? (
          <div className="sidebar-empty-inline">
            No files in sandbox.
            {activeTab?.status === 'connected' && (
              <span
                className="sidebar-load-samples-pill"
                onClick={async () => {
                  try {
                    const filenames = ['sales_targets_q3.csv', 'competitor_prices.csv']
                    for (const name of filenames) {
                      const resp = await fetch(`/samples/data/${name}`)
                      if (resp.ok) {
                        const blob = await resp.blob()
                        const file = new File([blob], name, { type: 'text/csv' })
                        onUploadFile(file)
                      }
                    }
                  } catch (err) {
                    console.error('Failed to load sample files:', err)
                  }
                }}
                title="Upload bundled sample data files to the sandbox"
              >
                Load samples
              </span>
            )}
          </div>
        ) : null}



        {/* Cloud data sources — rendered generically from the provider registry.
            Adding a new source type on the backend makes a new group appear here
            with no frontend change. 'local' is handled by the Sandbox section above. */}
        {sourceTypes.filter(st => st.source_type !== 'local').map(st => {
          const groupSources = sourcesByType[st.source_type] || []
          const collapsed = collapsedGroups.has(st.source_type)
          return (
            <div key={st.source_type}>
              <div className="sidebar-subheader sidebar-subheader-toggle" onClick={() => toggleGroup(st.source_type)}>
                <GroupIcon iconKey={st.icon} width={11} height={11} /> {st.display_name}
                {groupSources.length > 0 && <span className="sidebar-subheader-count">{groupSources.length}</span>}
                {/* Loading lives INLINE in the header (a small spinner), not as a
                    separate row below it. A per-group "Loading..." row got inserted
                    and removed on every 30s discovery refresh, so the whole tree
                    jumped up and down. Inline keeps the layout rock-steady: existing
                    rows stay put while the header shows a quiet refreshing state. */}
                {dsLoading && (
                  <span className="sidebar-subheader-spinner" title="Refreshing data sources…" aria-label="Loading" />
                )}
                <span className="sidebar-subheader-chevron">{collapsed ? '▸' : '▾'}</span>
              </div>
              {!collapsed && (
                <>
                  {/* Only show the empty state once loading has settled AND the group
                      is genuinely empty — never flip to "No … found." mid-refresh. */}
                  {!dsLoading && groupSources.length === 0 && (
                    <div className="sidebar-empty-inline">No {st.display_name} found.</div>
                  )}
                  {/* Grouped view nests sources by their natural hierarchy
                      (S3 prefixes, Athena databases, Aurora schemas); flat view
                      lists every source as one fully-qualified row. Types with no
                      grouping (DynamoDB, etc.) render flat in either mode. */}
                  {viewMode === 'grouped' && (st.source_type === 's3' || st.source_type === 'athena' || st.source_type === 'aurora')
                    ? renderTreeNode(
                        buildTree(groupSources), '', 0, st.source_type,
                        st.source_type === 's3' ? 'sidebar-icon-s3'
                          : st.source_type === 'aurora' ? 'sidebar-icon-aurora'
                          : 'sidebar-icon-athena'
                      )
                    : groupSources.map(src => renderSourceRow(
                        src,
                        // Flat view shows a fully-qualified label so rows are
                        // unambiguous across databases/folders/schemas.
                        viewMode === 'flat'
                          ? ((src.source_type === 'athena' || src.source_type === 'aurora') ? src.source_id : (src.display_name || src.source_id))
                          : null
                      ))}
                </>
              )}
            </div>
          )
        })}

        {/* Public APIs */}
        <div className="sidebar-subheader sidebar-subheader-toggle" onClick={() => setPublicApisExpanded(!publicApisExpanded)}>
          🌐 Public APIs
          <span className="sidebar-subheader-count">{PUBLIC_APIS.length} sources</span>
          <span className="sidebar-subheader-chevron">{publicApisExpanded ? '▾' : '▸'}</span>
        </div>
        {publicApisExpanded && PUBLIC_APIS.map(api => (
          <div
            key={api.id}
            className="sidebar-file-item sidebar-ds-row"
            title={api.desc}
          >
            <span className="sidebar-file-icon">{api.icon}</span>
            <div className="sidebar-file-info">
              <span className="sidebar-file-name">{api.name}</span>
              <span className="sidebar-file-meta">{api.desc}</span>
            </div>
            {/* Public APIs are Python-only; insert their prebuilt snippet directly. */}
            <div className="ds-insert-actions">
              <button
                className="ds-insert-btn ds-insert-python"
                onClick={(e) => { e.stopPropagation(); onInsertCode(api.code, 'code') }}
                title="Insert Python code for this API"
              >
                <IconCode width={11} height={11} /> Python
              </button>
            </div>
          </div>
        ))}
        </>)}
      </div>

      {/* Styled confirm for a destructive endpoint teardown (replaces the native
          window.confirm). Renders only when a delete is pending. */}
      {deleteTarget && (
        <ConfirmModal
          title="Delete SageMaker endpoint?"
          message={`Delete "${deleteTarget.display_name || deleteTarget.source_id}"? This tears down the endpoint, its config, and the model. It can't be undone — you'd re-run sm_deploy_serverless / sm_deploy_instance to recreate it.`}
          confirmLabel="Delete endpoint"
          cancelLabel="Cancel"
          confirmDanger
          onConfirm={() => {
            markPending(rowKey(deleteTarget), 'deleting')
            onDeleteS3File(deleteTarget)
            nudgeRefresh()
            setDeleteTarget(null)
          }}
          onCancel={() => setDeleteTarget(null)}
        />
      )}

      {/* Styled confirm for STOPPING an instance endpoint (pauses billing but keeps
          config + model so it can be resumed). Not danger-styled — it's reversible. */}
      {stopTarget && (
        <ConfirmModal
          title="Stop SageMaker endpoint?"
          message={`Stop "${stopTarget.display_name || stopTarget.source_id}"? This tears down the running instance endpoint to pause its billing, but KEEPS its config and model — press Start (or call sm_start_endpoint) to resume it. In-flight requests will fail until it's back.`}
          confirmLabel="Stop endpoint"
          cancelLabel="Cancel"
          onConfirm={() => {
            markPending(rowKey(stopTarget), 'stopping')
            onEndpointLifecycle(stopTarget, 'stop')
            nudgeRefresh()
            setStopTarget(null)
          }}
          onCancel={() => setStopTarget(null)}
        />
      )}
    </div>
  )
}
