/**
 * Packages panel list filtering (pure, unit-tested).
 *
 * `packages` is the VM's `pip list` ([{ name, version }]); `userSet` holds the
 * lower-cased names of user-installed packages (the proxy's `user_installed`).
 */

/** Whether `name` is user-installed (case-insensitive). */
export function isUserPackage(name, userSet) {
  return !!userSet && userSet.has(String(name || '').toLowerCase())
}

/** Packages matching the text filter (substring, case-insensitive) and, with
 *  `userOnly`, only the user-installed ones. Order is preserved. */
export function filterPackages(packages, { text = '', userOnly = false, userSet = null } = {}) {
  const list = Array.isArray(packages) ? packages : []
  const needle = String(text || '').trim().toLowerCase()
  return list.filter(p => {
    const name = String(p?.name || '')
    if (needle && !name.toLowerCase().includes(needle)) return false
    if (userOnly && !isUserPackage(name, userSet)) return false
    return true
  })
}

/** Number of listed packages that are user-installed (the "User installed" chip count). */
export function countUserPackages(packages, userSet) {
  const list = Array.isArray(packages) ? packages : []
  return list.reduce((n, p) => n + (isUserPackage(p?.name, userSet) ? 1 : 0), 0)
}
