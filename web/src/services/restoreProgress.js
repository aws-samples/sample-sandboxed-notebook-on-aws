// Pure rules for the restore progress line (no React, no DOM).
//
// While a restore /launch is pending, the VM reinstalls the user-installed
// packages listed in the checkpoint's requirements.txt (GET /notebooks/{id}
// `session_packages`) before it returns, which can take tens of seconds. The
// notebook view shows "Reinstalling N packages (a, b)..." for that time.

// 'pandas[sql]>=2.0' -> 'pandas', 'statsmodels==0.15.0' -> 'statsmodels'.
export function packageBaseName(spec = '') {
  return String(spec || '').trim().split(/[<>=!~[;@\s]/, 1)[0].trim()
}

// The package list the restore will reinstall, from GET /notebooks/{id}
// `session_packages`: a clean array of non-empty specs, or null when unknown.
export function sessionPackages(nb) {
  const list = nb?.session_packages
  if (!Array.isArray(list)) return null
  return list.map(s => String(s ?? '').trim()).filter(s => s && !s.startsWith('#'))
}

// "Reinstalling 2 packages (statsmodels, bottle)...", or '' when there is
// nothing to reinstall. Every name is listed (no truncation).
export function reinstallProgressText(specs) {
  const names = []
  for (const s of Array.isArray(specs) ? specs : []) {
    const n = packageBaseName(s)
    if (n && !names.includes(n)) names.push(n)
  }
  if (names.length === 0) return ''
  const noun = names.length === 1 ? 'package' : 'packages'
  return `Reinstalling ${names.length} ${noun} (${names.join(', ')})...`
}

// The progress line for a tab: only while its restore is pending.
export function restoreProgressText(tab) {
  if (!tab || tab.status !== 'launching') return ''
  return reinstallProgressText(tab._restorePackages)
}
