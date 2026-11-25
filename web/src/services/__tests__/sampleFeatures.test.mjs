// Validates web/public/samples/index.json `showcasedFeatures` against the single
// vocabulary in services/sampleFeatures.js.
// Run with: node --test web/src/services/__tests__/sampleFeatures.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  SHOWCASED_FEATURES, AWS_SERVICE_FEATURES, PLATFORM_FEATURES, showcasedFeaturesOf, filterSamples,
} from '../sampleFeatures.js'

const INDEX = fileURLToPath(new URL('../../../public/samples/index.json', import.meta.url))
const samples = JSON.parse(readFileSync(INDEX, 'utf8'))
const VOCAB = new Set(SHOWCASED_FEATURES)

test('vocabulary has no duplicate labels', () => {
  assert.equal(VOCAB.size, SHOWCASED_FEATURES.length)
})

test('AWS service labels use the official "Amazon …" names', () => {
  for (const f of AWS_SERVICE_FEATURES) assert.match(f, /^Amazon /, f)
  for (const f of PLATFORM_FEATURES) assert.doesNotMatch(f, /^(Amazon|AWS) /, f)
})

test('every sample has a showcasedFeatures array drawn from the vocabulary', () => {
  assert.ok(Array.isArray(samples) && samples.length > 0)
  for (const s of samples) {
    assert.ok(Array.isArray(s.showcasedFeatures), `${s.id}: showcasedFeatures must be an array`)
    assert.equal(new Set(s.showcasedFeatures).size, s.showcasedFeatures.length, `${s.id}: duplicate feature`)
    for (const f of s.showcasedFeatures) {
      assert.ok(VOCAB.has(f), `${s.id}: "${f}" is not in services/sampleFeatures.js`)
    }
  }
})

test('every vocabulary label is used by at least one sample', () => {
  const used = new Set(samples.flatMap(s => s.showcasedFeatures || []))
  for (const f of SHOWCASED_FEATURES) assert.ok(used.has(f), `"${f}" is unused`)
})

test('filterSamples: empty or blank query returns every sample', () => {
  assert.equal(filterSamples(samples, '').length, samples.length)
  assert.equal(filterSamples(samples, '   ').length, samples.length)
  assert.deepEqual(filterSamples(null, 'x'), [])
})

test('filterSamples matches the name, case-insensitively, trimmed substring', () => {
  const ids = filterSamples(samples, '  burst DEMO ').map(s => s.id)
  assert.deepEqual(ids, ['burst_demo'])
})

test('filterSamples matches any showcased-feature label', () => {
  const ids = filterSamples(samples, 'knowledge bases').map(s => s.id)
  assert.ok(ids.includes('aws_data_sources') && ids.includes('helper_functions'))
  for (const s of filterSamples(samples, 'sagemaker ai')) {
    assert.ok(s.name.toLowerCase().includes('sagemaker ai') || s.showcasedFeatures.includes('Amazon SageMaker AI'))
  }
})

test('filterSamples does not match the description', () => {
  // "Plotly" appears only in the sales_analysis description, not in a name or feature.
  assert.deepEqual(filterSamples(samples, 'plotly'), [])
  assert.deepEqual(filterSamples([{ name: 'A', description: 'zebra', showcasedFeatures: [] }], 'zebra'), [])
})

test('showcasedFeaturesOf treats missing or malformed values as no pills', () => {
  assert.deepEqual(showcasedFeaturesOf({}), [])
  assert.deepEqual(showcasedFeaturesOf(null), [])
  assert.deepEqual(showcasedFeaturesOf({ showcasedFeatures: 'Amazon S3' }), [])
  assert.deepEqual(showcasedFeaturesOf({ showcasedFeatures: ['Amazon S3', '', 3] }), ['Amazon S3'])
})
