// Parse the VM's execution-origin tag out of a log message.
//
// The VM /execute route (app/notebook/code_engine.py) stamps non-user runs:
//   "[agent] "    the AI assistant's execute_code probes (run in a scratch)
//   "[internal] " system probes: schema discovery, profiler (throwaway scratch)
// e.g. "2026-09-04 22:31:24,932 [INFO] ▶ [internal] Executing code (len=42)",
//      "  ✓ [agent] OK (3ms) scratch_vars=['a'] (len=5)",
//      "    [internal] KeyError: 'x'".
// The Logs panel shows the origin as a pill and drops the raw marker.
//
// The tag is matched ONLY where code_engine emits it: after an optional
// "timestamp [LEVEL] " prefix, optional indent and an optional ▶/✓/✗ marker. A
// stray "[internal]" mid-message (e.g. Docker's "[internal] load …" build output
// quoted later in a line) is not treated as a tag. The prefix and marker are kept.
const _ORIGIN_TAG_RE =
  /^((?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d+\s+\[[A-Z]+\]\s+)?\s*(?:[▶✓✗]\s+)?)\[(agent|internal)\]\s*/

// Returns { origin: 'agent' | 'internal' | null, message } with the tag removed.
export function parseOriginTag(msg) {
  if (typeof msg !== 'string') return { origin: null, message: msg }
  const m = msg.match(_ORIGIN_TAG_RE)
  if (!m) return { origin: null, message: msg }
  return { origin: m[2], message: m[1] + msg.slice(m[0].length) }
}
