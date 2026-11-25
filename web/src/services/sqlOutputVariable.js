/**
 * Default output variable name of a SQL cell, derived from its FROM clause
 * (external sources only); in-memory DataFrame queries get 'result' so they never
 * overwrite the source frame. Python port: app/notebook/static_analysis.py
 * derive_sql_output_variable — parity-tested on tests/fixtures/sql_output_variable_cases.json.
 */
export function deriveSqlOutputVariable(sql) {
  if (!sql || !sql.trim()) return 'result'
  const fromMatch = sql.match(/\bFROM\s+dynamodb\."?([a-zA-Z_][\w-]*)"?/i)
    || sql.match(/\bFROM\s+'\/tmp\/([^']+)'/i)
    || sql.match(/\bFROM\s+read_(?:csv|json|parquet)\('[^']*\/([^'/]+)'\)/i)
    || sql.match(/\bFROM\s+[a-zA-Z_]\w*\.([a-zA-Z_]\w*)/i)
  if (fromMatch) {
    const raw = fromMatch[1] || 'result'
    const cleaned = raw.replace(/\.\w+$/, '').replace(/[^a-zA-Z0-9_]/g, '_').replace(/^_+|_+$/g, '')
    if (cleaned && /^[a-zA-Z_]/.test(cleaned)) return cleaned
  }
  return 'result'
}
