// Packages panel: the extra hint shown after a pip install fails because no
// wheel exists for the VM's CPU architecture.
//
// The VM's message (app/notebook/pip_diagnostics.py) never suggests switching
// arch, since the VM can't know what this deployment offers. The UI does know
// (/image-tiers), so it adds a hedged hint, and only when the other arch is
// actually offered. "may": we haven't checked PyPI for the other arch's wheel.

// Failure kinds where another arch could plausibly have a prebuilt wheel.
// sdist_download_timeout: pip picked a source package, so it found no wheel
// for this arch. Deliberately absent: version_not_found (the pin/specifier
// doesn't exist, other versions do: not an arch problem), download_timeout
// (network), and any kind this UI doesn't know (no hint, message shown as-is).
// An older VM reports a missing pin as no_distribution, so it keeps the hint
// it had before.
export const NO_WHEEL_KINDS = ['no_distribution', 'source_build_failed', 'source_build_timeout', 'sdist_download_timeout']

// arm64 <-> x86_64, with the labels the rest of the UI uses (archLabel).
const OTHER_ARCH = { arm64: 'x86_64', x86_64: 'arm64' }
const ARCH_LABEL = { arm64: 'ARM64', x86_64: 'x86_64' }

/**
 * @param {{errorKind?: string, arch?: string, offeredArches?: string[]}} args
 * @returns {string} the hint sentence, or '' when it doesn't apply.
 */
export function otherArchHint({ errorKind, arch, offeredArches } = {}) {
  if (!NO_WHEEL_KINDS.includes(errorKind)) return ''
  const other = OTHER_ARCH[arch]
  if (!other || !Array.isArray(offeredArches) || !offeredArches.includes(other)) return ''
  return `Running the notebook on ${ARCH_LABEL[other]} may help: it may have a prebuilt wheel.`
}
