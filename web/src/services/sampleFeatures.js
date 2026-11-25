// Single source of truth for the "Showcased Features" vocabulary of the sample
// notebooks (web/public/samples/index.json → each entry's `showcasedFeatures`).
// The Samples panel renders these labels as pills; the node test
// (__tests__/sampleFeatures.test.mjs) validates index.json against this list, so
// a feature keeps the same label in every sample. AWS services use their official
// names; platform features use short readable labels.

export const AWS_SERVICE_FEATURES = [
  'Amazon Athena',
  'Amazon S3',
  'Amazon S3 Tables',
  'Amazon DynamoDB',
  'Amazon Aurora PostgreSQL',
  'Amazon Bedrock',
  'Amazon Bedrock Knowledge Bases',
  'Amazon SageMaker AI',
  'Amazon CloudWatch Logs',
]

export const PLATFORM_FEATURES = [
  'SQL cells',
  'Cross-source SQL',
  'Interactive parameters',
  'Reactive execution',
  'Interactive charts',
  'Helper library',
  'Semantic search',
  'In-VM ML training',
  'ONNX export',
  'Model endpoints',
  'Memory & CPU burst',
  'Cost tracking',
  'Run history',
  'Public APIs',
]

export const SHOWCASED_FEATURES = [...AWS_SERVICE_FEATURES, ...PLATFORM_FEATURES]

// The pills to render for one index.json entry. Missing or non-array → none.
export function showcasedFeaturesOf(sample) {
  const list = sample?.showcasedFeatures
  return Array.isArray(list) ? list.filter(f => typeof f === 'string' && f) : []
}

// Samples panel search: same rule as the Snippets panel (trimmed, case-insensitive
// substring), matched against the sample's name OR any showcased-feature label.
// An empty query returns the list unchanged.
export function filterSamples(samples, query) {
  const list = Array.isArray(samples) ? samples : []
  const q = (query || '').trim().toLowerCase()
  if (!q) return list
  return list.filter(s =>
    String(s?.name || '').toLowerCase().includes(q)
    || showcasedFeaturesOf(s).some(f => f.toLowerCase().includes(q))
  )
}
