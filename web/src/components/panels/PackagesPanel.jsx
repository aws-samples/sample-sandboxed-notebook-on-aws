import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { IconRefresh, IconDownload, IconX, IconNotebook, IconPackage } from '../Icons'
import PanelState from './PanelState'
import PanelExpandButton from './PanelExpandButton'
import FilterPills from '../FilterPills'
import { PROXY_URL } from '../../config'
import { fetchWithTimeout } from '../../services/fetchWithTimeout'
import { sharedJson } from '../../services/singleFlight'
import { downloadTextFile } from '../../services/notebookExport'
import { otherArchHint } from '../../services/pipInstallMessage'
import { SLOTS } from '../../services/actionBanner'
import { useSlotBanner, clear as clearBanner } from '../../services/bannerStore'
import ActionBanner from '../ActionBanner'
import { filterPackages, countUserPackages } from '../../services/packageFilter'

/**
 * Sort PyPI version strings newest-first. Not a full PEP 440 implementation, but a
 * pragmatic comparator that gets the ordering right for the vast majority of real
 * packages: compare the dotted numeric release segments left-to-right, and treat a
 * version carrying a pre-release/dev marker (a, b, rc, dev, post-less) as lower
 * than the otherwise-equal final release so stable versions surface first.
 */
function compareVersionsDesc(a, b) {
  const parse = (v) => {
    const isPre = /(a|b|rc|alpha|beta|dev|pre)/i.test(v)
    const nums = (v.match(/\d+/g) || []).map(Number)
    return { nums, isPre }
  }
  const pa = parse(a)
  const pb = parse(b)
  const len = Math.max(pa.nums.length, pb.nums.length)
  for (let i = 0; i < len; i++) {
    const da = pa.nums[i] ?? 0
    const db = pb.nums[i] ?? 0
    if (da !== db) return db - da        // higher number first
  }
  // Equal numeric release → finals rank above pre-releases.
  if (pa.isPre !== pb.isPre) return pa.isPre ? 1 : -1
  return 0
}

// A package the last restore could not re-install (restore report, App.jsx).
const RESTORE_FAILED_TEXT = 'Failed to re-install on this architecture'
// PEP 503 name normalization: "Foo_Bar" and "foo.bar" are "foo-bar".
const normPkgName = (n) => String(n || '').toLowerCase().replace(/[-_.]+/g, '-')

export default function PackagesPanel({
  packages,
  pkgLoading,
  pkgFetched,
  pkgSuspended = false,
  activeTab,
  fetchPackages,
  onInstallPackage,
  onUninstallPackage,
  onInsertCode,
  onClose,
  expanded,
  onToggleExpand,
}) {
  const [installPkg, setInstallPkg] = useState('')
  const [installStatus, setInstallStatus] = useState(null)
  const [installMessage, setInstallMessage] = useState('')
  // pip's raw output for a failed install (shown in the Details expander). While
  // set, the error stays up until dismissed or the next install attempt.
  const [installDetails, setInstallDetails] = useState('')
  // Pending auto-clear of the status line; cancelled by the next attempt so a
  // stale timer can't hide a newer error the user is still reading.
  const clearTimerRef = useRef(null)
  const scheduleClear = (ms) => {
    clearTimeout(clearTimerRef.current)
    clearTimerRef.current = setTimeout(() => setInstallStatus(null), ms)
  }
  useEffect(() => () => clearTimeout(clearTimerRef.current), [])
  const dismissInstallError = () => {
    clearTimeout(clearTimerRef.current)
    setInstallStatus(null)
    setInstallDetails('')
  }
  // Arches this deployment offers (/image-tiers), for the "other arch may have
  // a wheel" hint. Empty until loaded / on error: then no hint is shown.
  const [offeredArches, setOfferedArches] = useState([])
  const [pkgFilter, setPkgFilter] = useState('')
  // 'all' | 'user': the "User installed" chip limits the list to user installs.
  const [pkgScope, setPkgScope] = useState('all')
  const [viewMode, setViewMode] = useState('grouped') // 'flat' | 'grouped'
  // Version picker: versions available on PyPI for the currently-typed package, and
  // the one the user chose ('' = latest). Populated by a debounced PyPI lookup keyed
  // off the base name typed in the install field.
  const [pkgVersions, setPkgVersions] = useState([])   // e.g. ['2.2.3', '2.2.2', ...] newest-first
  const [selectedVersion, setSelectedVersion] = useState('')
  const [versionsLoading, setVersionsLoading] = useState(false)

  // Category data from proxy (fetched once)
  const [categoryMap, setCategoryMap] = useState({})
  const [importAliases, setImportAliases] = useState({})
  const [categoryOrder, setCategoryOrder] = useState([])
  const [userInstalled, setUserInstalled] = useState([])

  // Fetch categories from proxy on mount
  useEffect(() => {
    const sessionId = activeTab?.sessionId || ''
    sharedJson(`package-categories|${sessionId}`,
      () => fetchWithTimeout(`${PROXY_URL}/package-categories${sessionId ? `?session_id=${sessionId}` : ''}`))
      .then(r => r.data)
      .then(data => {
        if (data) {
          setCategoryMap(data.categories || {})
          setImportAliases(data.import_aliases || {})
          setCategoryOrder(data.category_order || [])
          setUserInstalled(data.user_installed || [])
        }
      })
      .catch(() => {})
  }, [activeTab?.sessionId])

  // Offered arches: one-shot on mount (deployment-wide, not per session).
  useEffect(() => {
    let cancelled = false
    sharedJson('image-tiers|panel', () => fetchWithTimeout(`${PROXY_URL}/image-tiers`))
      .then(r => r.data)
      .then(data => {
        if (!cancelled && data) {
          setOfferedArches(data.arches || Object.keys(data.tiersByArch || {}))
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  const userInstalledSet = useMemo(
    () => new Set(userInstalled.map(p => p.package?.toLowerCase())),
    [userInstalled]
  )

  // The bare package name typed in the install field, stripped of any version
  // operator / extras the user typed themselves (e.g. "pandas==2.0" → "pandas",
  // "requests[socks]>=2" → "requests"). Empty if nothing usable is typed.
  const typedBaseName = useMemo(() => {
    const raw = installPkg.trim()
    if (!raw) return ''
    return raw.split(/[<>=!~[\s]/)[0].trim()
  }, [installPkg])

  // Whether the user has already typed their own version operator — if so we defer
  // entirely to their spec and hide the picker (don't fight an explicit request).
  const userTypedVersion = /[<>=!~]/.test(installPkg)

  // Debounced PyPI version lookup for the typed package. PyPI's JSON API returns a
  // `releases` object keyed by version; we sort newest-first (PEP 440-ish: numeric
  // segment compare, pre-releases sink below finals) and cap the list so the
  // dropdown stays snappy. Silently no-ops if offline/blocked — the field still
  // works as a free-text spec. Reuses the same endpoint as the typo pre-check.
  useEffect(() => {
    const name = typedBaseName
    if (!name || name.length < 2 || userTypedVersion) {
      setPkgVersions([])
      setSelectedVersion('')
      return
    }
    let cancelled = false
    setVersionsLoading(true)
    const t = setTimeout(async () => {
      try {
        const resp = await fetch(`https://pypi.org/pypi/${encodeURIComponent(name)}/json`)
        if (!resp.ok) { if (!cancelled) { setPkgVersions([]); setVersionsLoading(false) } return }
        const data = await resp.json()
        const releases = data?.releases || {}
        const versions = Object.keys(releases)
          // Drop yanked/empty releases (no distributable files).
          .filter(v => Array.isArray(releases[v]) && releases[v].length > 0)
          .sort(compareVersionsDesc)
          .slice(0, 60)
        if (!cancelled) {
          setPkgVersions(versions)
          setSelectedVersion('')   // default to "latest"
          setVersionsLoading(false)
        }
      } catch {
        if (!cancelled) { setPkgVersions([]); setVersionsLoading(false) }
      }
    }, 350)
    return () => { cancelled = true; clearTimeout(t) }
  }, [typedBaseName, userTypedVersion])

  const handleInstallPkg = async () => {
    if (!installPkg.trim() || !activeTab?.microvmEndpoint) return
    const typed = installPkg.trim()
    const typedHasOperator = /[<>=!~]/.test(typed)
    // Compose the final spec: if the user picked a version from the dropdown (and
    // didn't already type their own operator), pin it as name==version. Otherwise
    // pass exactly what they typed (bare name = latest, or their own spec).
    const pkgName = (selectedVersion && !typedHasOperator)
      ? `${typedBaseName}==${selectedVersion}`
      : typed
    const hasVersion = typedHasOperator || !!selectedVersion
    const baseName = pkgName.split(/[<>=!~[]/)[0].trim()
    const baseLower = baseName.toLowerCase()
    // A new attempt replaces any error still on screen.
    clearTimeout(clearTimerRef.current)
    setInstallDetails('')

    // Already installed? Skip the slow pip call unless a specific version is asked for.
    const existing = packages.find(p => p.name.toLowerCase() === baseLower)
    if (existing && !hasVersion) {
      setInstallStatus('success')
      setInstallMessage(`${existing.name} is already installed (v${existing.version})`)
      setInstallPkg('')
      scheduleClear(5000)
      return
    }

    // Soft typo pre-check against PyPI — avoids a slow, doomed install. Skipped
    // silently if blocked (CORS) or offline; pip still validates authoritatively.
    try {
      const probe = await fetch(`https://pypi.org/pypi/${encodeURIComponent(baseName)}/json`)
      if (probe.status === 404) {
        setInstallStatus('error')
        setInstallMessage(`"${baseName}" not found on PyPI — check the name`)
        scheduleClear(6000)
        return
      }
    } catch { /* pre-check unavailable — let pip decide */ }

    setInstallStatus('installing')
    setInstallMessage(`Installing ${pkgName}… (can take a minute)`)
    const result = await onInstallPackage(pkgName, { offeredArches })
    if (result.success) {
      setInstallStatus('success')
      setInstallMessage(`Installed ${result.installed_spec || pkgName}`)
      setInstallPkg('')
      setSelectedVersion('')
      setPkgVersions([])
      // Notify proxy to track + classify the install
      try {
        const resp = await fetchWithTimeout(`${PROXY_URL}/track-install`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: activeTab?.sessionId || '', package: baseName }),
        })
        if (resp.ok) {
          const data = await resp.json()
          setUserInstalled(prev => [...prev, { package: baseName, category: data.category }])
        } else {
          console.warn('[packages] track-install failed:', resp.status)
        }
      } catch (err) {
        console.warn('[packages] track-install error:', err.message)
      }
    } else {
      const hint = otherArchHint({ errorKind: result.errorKind, arch: result.arch, offeredArches })
      setInstallStatus('error')
      setInstallMessage((result.error || 'Install failed') + (hint ? ' ' + hint : ''))
      setInstallDetails(result.details || '')
      // With pip output attached the error stays until dismissed / next attempt,
      // so the Details expander remains usable.
      if (result.details) return
    }
    scheduleClear(5000)
  }

  // Install banner (published by Sidebar for panel installs, by useNotebookCells
  // for pip cells). Click: a failure opens and shows the Details block when it is
  // still on screen; a success scrolls the list to the package row, refreshing the
  // list first when the row is not there yet (a cell install).
  const installBanner = useSlotBanner(SLOTS.PACKAGES)
  const detailsRef = useRef(null)
  const bodyRef = useRef(null)
  const pendingScrollRef = useRef(null)
  const openDetailsPendingRef = useRef(false)
  useEffect(() => {
    const el = detailsRef.current
    if (!openDetailsPendingRef.current || !el) return
    openDetailsPendingRef.current = false
    el.open = true
    el.scrollIntoView({ block: 'nearest' })
  }, [installDetails, installStatus])
  const scrollToPackage = useCallback((name) => {
    const lower = String(name || '').toLowerCase()
    if (!lower || !bodyRef.current) return false
    const row = [...bodyRef.current.querySelectorAll('[data-pkg-name]')]
      .find(el => el.getAttribute('data-pkg-name') === lower)
    if (!row) return false
    row.scrollIntoView({ block: 'center' })
    return true
  }, [])
  useEffect(() => {
    if (!pendingScrollRef.current || pkgLoading || !pkgFetched) return
    // One try once the refreshed list is in; a name that is not listed is dropped.
    scrollToPackage(pendingScrollRef.current)
    pendingScrollRef.current = null
  }, [packages, pkgLoading, pkgFetched, scrollToPackage])
  const onInstallBannerAction = () => {
    if (!installBanner) return
    clearBanner(SLOTS.PACKAGES, installBanner.id)
    if (installBanner.kind === 'restore-report') {
      // Published to several panels: acting on it here removes it from all of them.
      for (const s of Object.values(SLOTS)) clearBanner(s, installBanner.id)
      // "Open Packages" here: the failed packages are listed at the top.
      setPkgFilter('')
      bodyRef.current?.querySelector('.pkg-restore-failed-group')?.scrollIntoView({ block: 'nearest' })
      return
    }
    const { ok, pkg, details } = installBanner.payload || {}
    if (!ok) {
      const el = detailsRef.current
      if (el) {
        el.open = true
        el.scrollIntoView({ block: 'nearest' })
      } else if (details) {
        // The panel remounted since the failure: rebuild the error block from the
        // banner, then open its Details once it renders.
        clearTimeout(clearTimerRef.current)
        setInstallStatus('error')
        setInstallMessage(installBanner.detail || 'Install failed')
        setInstallDetails(details)
        openDetailsPendingRef.current = true
      }
      return
    }
    setPkgFilter('')
    if (!scrollToPackage(pkg)) {
      pendingScrollRef.current = pkg
      fetchPackages()
    }
  }

  const handleUninstall = async (pkgName) => {
    if (!onUninstallPackage) return
    const result = await onUninstallPackage(pkgName)
    if (result.success) {
      setUserInstalled(prev => prev.filter(p => p.package?.toLowerCase() !== pkgName.toLowerCase()))
      fetchPackages()
    }
  }

  const handleImport = (pkgName) => {
    if (!onInsertCode) return
    const alias = importAliases[pkgName.toLowerCase()]
    const code = alias || `import ${pkgName.replace(/-/g, '_')}`
    onInsertCode(code)
  }

  // Download the full installed package list as a pip-style requirements.txt.
  // Mirrors exactly what the panel shows (the VM's `pip list`), pinned name==version,
  // sorted case-insensitively so the file is stable/diffable.
  const handleDownloadRequirements = () => {
    if (!packages.length) return
    const content = [...packages]
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()))
      .map(p => `${p.name}==${p.version}`)
      .join('\n') + '\n'
    const base = (activeTab?.name || 'requirements').replace(/[^\w.-]+/g, '_')
    downloadTextFile({ content, mime: 'text/plain', filename: `${base}-requirements.txt` })
  }

  const getCategory = useCallback((pkgName) => {
    // Check user-installed list first (has PyPI-determined category)
    const userPkg = userInstalled.find(p => p.package?.toLowerCase() === pkgName.toLowerCase())
    if (userPkg?.category) return userPkg.category
    // Then static mapping
    return categoryMap[pkgName.toLowerCase()] || 'Other'
  }, [categoryMap, userInstalled])

  const filteredPackages = useMemo(
    () => filterPackages(packages, { text: pkgFilter, userOnly: pkgScope === 'user', userSet: userInstalledSet }),
    [packages, pkgFilter, pkgScope, userInstalledSet]
  )
  const userPkgCount = useMemo(() => countUserPackages(packages, userInstalledSet), [packages, userInstalledSet])

  // Grouped view
  const groupedPackages = useMemo(() => {
    const groups = {}
    for (const pkg of filteredPackages) {
      const cat = getCategory(pkg.name)
      if (!groups[cat]) groups[cat] = []
      groups[cat].push(pkg)
    }
    // Sort: use order from proxy, fallback to alphabetical for unknown categories
    const order = categoryOrder.length > 0 ? categoryOrder : Object.keys(groups).sort()
    const orderedGroups = order.filter(k => groups[k]?.length > 0).map(k => ({ name: k, packages: groups[k] }))
    // Add any categories not in the order list
    const ordered = new Set(order)
    Object.keys(groups).filter(k => !ordered.has(k) && groups[k]?.length > 0).forEach(k => {
      orderedGroups.push({ name: k, packages: groups[k] })
    })
    return orderedGroups
  }, [filteredPackages, getCategory, categoryOrder])

  const isUserPkg = (name) => userInstalledSet.has(name.toLowerCase())

  // The last restore's failed packages (activeTab._restoreReport.packages_failed),
  // keyed by normalized name ("Foo_Bar" == "foo-bar").
  const restoreFailed = useMemo(() => {
    const list = activeTab?._restoreReport?.packages_failed
    const map = new Map()
    for (const p of Array.isArray(list) ? list : []) {
      if (p && p.name) map.set(normPkgName(p.name), { name: String(p.name), reason: String(p.reason || '') })
    }
    return map
  }, [activeTab?._restoreReport])
  const restoreNotInstalled = useMemo(() => {
    if (restoreFailed.size === 0) return []
    const listed = new Set(packages.map(p => normPkgName(p.name)))
    return [...restoreFailed].filter(([k]) => !listed.has(k)).map(([, v]) => v)
  }, [restoreFailed, packages])

  const renderPkgItem = (pkg) => {
    const failed = restoreFailed.get(normPkgName(pkg.name))
    return (
    <div key={pkg.name} data-pkg-name={pkg.name.toLowerCase()} className={`pkg-sidebar-item ${isUserPkg(pkg.name) ? 'pkg-user-installed' : ''}${failed ? ' pkg-restore-failed' : ''}`} title={failed?.reason || undefined}>
      <span className="pkg-sidebar-name">
        {pkg.name}
        {isUserPkg(pkg.name) && <span className="pkg-user-badge">user</span>}
        {failed && <span className="pkg-restore-failed-note">{RESTORE_FAILED_TEXT}</span>}
      </span>
      <span className="pkg-actions">
        <button
          className="pkg-action-btn pkg-import-btn"
          onClick={(e) => { e.stopPropagation(); handleImport(pkg.name) }}
          title={`Insert: ${importAliases[pkg.name.toLowerCase()] || `import ${pkg.name.replace(/-/g, '_')}`}`}
        >↵</button>
        {isUserPkg(pkg.name) && onUninstallPackage && (
          <button
            className="pkg-action-btn pkg-uninstall-btn"
            onClick={(e) => { e.stopPropagation(); handleUninstall(pkg.name) }}
            title={`Uninstall ${pkg.name}`}
          >✕</button>
        )}
      </span>
      <span className="pkg-sidebar-version">{pkg.version}</span>
    </div>
    )
  }

  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">Packages</span>
        <span className="sidebar-panel-count">{packages.length}</span>
        <button
          className={`sidebar-panel-action pkg-view-toggle ${viewMode === 'grouped' ? 'active' : ''}`}
          onClick={() => setViewMode(v => v === 'flat' ? 'grouped' : 'flat')}
          title={viewMode === 'flat' ? 'Group by category' : 'Flat list'}
        >
          ≡
        </button>
        <button
          className="sidebar-panel-action"
          onClick={handleDownloadRequirements}
          disabled={!packages.length}
          title={packages.length ? 'Download requirements.txt' : 'No packages to download'}
        >
          <IconDownload width={14} height={14} />
        </button>
        <button className="sidebar-panel-action" onClick={fetchPackages} title="Refresh">
          <IconRefresh width={14} height={14} />
        </button>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      {activeTab && <div className="sidebar-scope-pill"><IconNotebook width={12} height={12} /> {activeTab.name}</div>}
      {installBanner && (
        <ActionBanner
          banner={installBanner}
          className="slot-banner"
          onAction={onInstallBannerAction}
          onDismiss={() => clearBanner(SLOTS.PACKAGES, installBanner.id)}
          dismissLabel="Dismiss install message"
        />
      )}

      {/* Install input: package name + optional version picker. The name field
          still accepts a full pip spec (e.g. pandas==2.0.3, requests>=2.28); when
          the user just types a name, the version dropdown lets them pin a specific
          release from PyPI (default = latest). */}
      <div className="pkg-sidebar-install">
        <input
          className="pkg-sidebar-input"
          type="text"
          value={installPkg}
          onChange={(e) => setInstallPkg(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') handleInstallPkg() }}
          placeholder="Add a new package"
          disabled={installStatus === 'installing' || !activeTab?.microvmEndpoint}
        />
        {/* Version picker — shown once a name is typed and the user hasn't written
            their own operator. Disabled while versions load or if none were found
            (offline / private pkg), where it just falls back to "latest". */}
        {typedBaseName && !userTypedVersion && (
          <select
            className={`pkg-sidebar-version-select${selectedVersion ? ' pinned' : ''}`}
            value={selectedVersion}
            onChange={(e) => setSelectedVersion(e.target.value)}
            disabled={installStatus === 'installing' || versionsLoading || pkgVersions.length === 0}
            title={versionsLoading ? 'Loading versions from PyPI…' : pkgVersions.length ? 'Pick a version to pin (default: latest)' : 'No versions found — will install latest'}
          >
            <option value="">{versionsLoading ? 'loading…' : 'latest'}</option>
            {pkgVersions.map(v => <option key={v} value={v}>{v}</option>)}
          </select>
        )}
        <button
          className="pkg-sidebar-btn"
          onClick={handleInstallPkg}
          disabled={!installPkg.trim() || installStatus === 'installing' || !activeTab?.microvmEndpoint}
          title={selectedVersion ? `Install ${typedBaseName}==${selectedVersion}` : 'Install'}
        >
          {installStatus === 'installing' ? '...' : '+'}
        </button>
      </div>
      {installStatus === 'installing' && <div className="pkg-sidebar-msg pkg-msg-installing">{installMessage}</div>}
      {installStatus === 'success' && <div className="pkg-sidebar-msg pkg-msg-success">{installMessage}</div>}
      {installStatus === 'error' && !installDetails && <div className="pkg-sidebar-msg pkg-msg-error">{installMessage}</div>}
      {/* Error with pip output: persistent, dismissible, raw output on demand. */}
      {installStatus === 'error' && installDetails && (
        <div className="pkg-sidebar-msg pkg-msg-error pkg-msg-persistent" role="alert">
          <div className="pkg-msg-row">
            <span className="pkg-msg-text">{installMessage}</span>
            <button
              type="button"
              className="pkg-msg-dismiss"
              onClick={dismissInstallError}
              aria-label="Dismiss install error"
              title="Dismiss"
            >
              <IconX width={10} height={10} />
            </button>
          </div>
          <details className="pkg-msg-details" ref={detailsRef}>
            <summary>Details</summary>
            <pre>{installDetails}</pre>
          </details>
        </div>
      )}

      {/* Filter row: text filter + All / User installed filter pills (combined; the
          shared Outline pill look, see FilterPills). */}
      <div className="pkg-sidebar-filter">
        <div className="pkg-filter-field">
          <input
            className="outline-search-input"
            type="text"
            value={pkgFilter}
            onChange={(e) => setPkgFilter(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape' && pkgFilter) { e.preventDefault(); setPkgFilter('') } }}
            placeholder="Filter packages..."
          />
          {pkgFilter && (
            <button
              className="outline-search-clear"
              onClick={() => setPkgFilter('')}
              title="Clear filter (Esc)"
              aria-label="Clear filter"
            >
              <IconX width={12} height={12} />
            </button>
          )}
        </div>
        <FilterPills
          className="pkg-scope-pills"
          ariaLabel="Show packages"
          value={pkgScope}
          onChange={setPkgScope}
          options={[
            { key: 'all', label: 'All', title: 'Show all packages' },
            { key: 'user', label: `User installed (${userPkgCount})`, title: 'Show only packages you installed' },
          ]}
        />
      </div>

      <div className="sidebar-panel-body" ref={bodyRef}>
        {!activeTab?.microvmEndpoint && (
          <PanelState variant="empty" icon={<IconPackage width={24} height={24} />} message="Connect to a MicroVM" hint="Launch or attach a VM to manage packages." />
        )}
        {/* Suspended VM with nothing cached yet: don't spin forever (we won't wake a
            sleeping VM to run `pip list`). Show why + how to bring the list back. */}
        {activeTab?.microvmEndpoint && pkgSuspended && !pkgFetched && packages.length === 0 && (
          <PanelState variant="empty" icon={<IconPackage width={24} height={24} />} message="VM suspended" hint="Resume the VM or run a cell to load its packages." />
        )}
        {/* Loading: actively fetching, OR not-yet-fetched with no cache AND the VM is
            awake (a suspended VM is handled above, so it can't spin indefinitely). */}
        {activeTab?.microvmEndpoint && !pkgSuspended && (pkgLoading || (!pkgFetched && packages.length === 0)) && (
          <PanelState variant="loading" message="Loading packages…" />
        )}
        {activeTab?.microvmEndpoint && pkgFetched && !pkgLoading && packages.length === 0 && (
          <PanelState variant="empty" icon={<IconPackage width={24} height={24} />} message="No packages found" />
        )}

        {/* Packages the last restore could not re-install and that pip does not
            list: shown first so the user sees them (activeTab._restoreReport). */}
        {activeTab?.microvmEndpoint && !pkgLoading && restoreNotInstalled.length > 0 && (
          <div className="pkg-group pkg-restore-failed-group">
            <div className="pkg-group-header">
              Not installed
              <span className="pkg-group-count">{restoreNotInstalled.length}</span>
            </div>
            {restoreNotInstalled.map(p => (
              <div key={p.name} data-pkg-name={String(p.name).toLowerCase()} className="pkg-sidebar-item pkg-restore-failed" title={p.reason || undefined}>
                <span className="pkg-sidebar-name">
                  {p.name}
                  <span className="pkg-restore-failed-note">{RESTORE_FAILED_TEXT}</span>
                  {p.reason && <span className="pkg-restore-failed-reason">{p.reason}</span>}
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Packages listed, but none pass the scope chip + text filter. */}
        {activeTab?.microvmEndpoint && !pkgLoading && packages.length > 0 && filteredPackages.length === 0 && (
          pkgScope === 'user' && userPkgCount === 0
            ? <PanelState variant="empty" icon={<IconPackage width={24} height={24} />} message="No user-installed packages" hint="Packages you add above appear here." />
            : <PanelState variant="empty" icon={<IconPackage width={24} height={24} />} message="No matching packages" hint={pkgScope === 'user' ? 'No user-installed package matches the filter.' : 'No package matches the filter.'} />
        )}

        {/* Flat view */}
        {!pkgLoading && viewMode === 'flat' && filteredPackages.map(renderPkgItem)}

        {/* Grouped view */}
        {!pkgLoading && viewMode === 'grouped' && groupedPackages.map(group => (
          <div key={group.name} className="pkg-group">
            <div className="pkg-group-header">
              {group.name}
              <span className="pkg-group-count">{group.packages.length}</span>
            </div>
            {group.packages.map(renderPkgItem)}
          </div>
        ))}
      </div>
    </div>
  )
}
