// Unit tests for services/modelStatus.js (the chat model-fallback badge logic).
// Run with: node --test web/src/services/__tests__/modelStatus.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { modelDisplayName, formatCooldownMinutes, fallbackBadge } from '../modelStatus.js'

const MODELS = [
  { id: 'zai.glm-5.3', display_name: 'GLM 5.3' },
  { id: 'anthropic.claude-haiku-5-5', display_name: 'Haiku 5.5' },
  { id: 'no-name', display_name: '' },
]

const status = (over = {}) => ({
  type: 'model_status',
  requested: 'zai.glm-5.3',
  active: 'anthropic.claude-haiku-5-5',
  reason: 'cooldown',
  cooldown_remaining_s: 482,
  ...over,
})

test('modelDisplayName: uses the display name, falls back to the id', () => {
  assert.equal(modelDisplayName(MODELS, 'zai.glm-5.3'), 'GLM 5.3')
  assert.equal(modelDisplayName(MODELS, 'no-name'), 'no-name')
  assert.equal(modelDisplayName(MODELS, 'unknown.model'), 'unknown.model')
  assert.equal(modelDisplayName(undefined, 'zai.glm-5.3'), 'zai.glm-5.3')
  assert.equal(modelDisplayName(MODELS, ''), '')
})

test('formatCooldownMinutes: rounds up, at least 1 min, null when unusable', () => {
  assert.equal(formatCooldownMinutes(482), '9 min')
  assert.equal(formatCooldownMinutes(600), '10 min')
  assert.equal(formatCooldownMinutes(30), '1 min')
  assert.equal(formatCooldownMinutes(0), null)
  assert.equal(formatCooldownMinutes(null), null)
  assert.equal(formatCooldownMinutes(undefined), null)
  assert.equal(formatCooldownMinutes(-5), null)
  assert.equal(formatCooldownMinutes('abc'), null)
})

test('fallbackBadge: cooldown reads as the example wording, with display names', () => {
  const badge = fallbackBadge(status({ cooldown_remaining_s: 420 }), 'zai.glm-5.3', MODELS)
  assert.deepEqual(badge, {
    text: 'Using Haiku 5.5 (GLM 5.3 cooling down, ~7 min)',
    title: 'Using Haiku 5.5 (GLM 5.3 cooling down, ~7 min)',
  })
  assert.ok(!/anthropic\.|zai\./.test(badge.text), 'no raw ids in the visible text')
})

test('fallbackBadge: throttled and unavailable wording', () => {
  assert.equal(
    fallbackBadge(status({ reason: 'throttled', cooldown_remaining_s: 600 }), 'zai.glm-5.3', MODELS).text,
    'Using Haiku 5.5 (GLM 5.3 throttled, ~10 min)',
  )
  assert.equal(
    fallbackBadge(status({ reason: 'throttled', cooldown_remaining_s: null }), 'zai.glm-5.3', MODELS).text,
    'Using Haiku 5.5 (GLM 5.3 throttled)',
  )
  assert.equal(
    fallbackBadge(status({ reason: 'unavailable', cooldown_remaining_s: null }), 'zai.glm-5.3', MODELS).text,
    'Using Haiku 5.5 (GLM 5.3 unavailable)',
  )
})

test('fallbackBadge: cooldown without a remaining estimate omits the time', () => {
  assert.equal(
    fallbackBadge(status({ cooldown_remaining_s: null }), 'zai.glm-5.3', MODELS).text,
    'Using Haiku 5.5 (GLM 5.3 cooling down)',
  )
})

test('fallbackBadge: null when no fallback is in effect', () => {
  // reason null / active === requested: the badge clears.
  assert.equal(fallbackBadge(status({ reason: null, active: 'zai.glm-5.3',
    cooldown_remaining_s: null }), 'zai.glm-5.3', MODELS), null)
  assert.equal(fallbackBadge(status({ active: 'zai.glm-5.3' }), 'zai.glm-5.3', MODELS), null)
  assert.equal(fallbackBadge(null, 'zai.glm-5.3', MODELS), null)
  assert.equal(fallbackBadge(undefined, 'zai.glm-5.3', MODELS), null)
})

test('fallbackBadge: hidden once the dropdown points at another model', () => {
  // The status is for GLM 5.3; the user switched the dropdown to Haiku.
  assert.equal(fallbackBadge(status(), 'anthropic.claude-haiku-5-5', MODELS), null)
  // No selection at all (no model known yet) shows nothing.
  assert.equal(fallbackBadge(status(), undefined, MODELS), null)
  assert.equal(fallbackBadge(status(), '', MODELS), null)
})

test('fallbackBadge: unknown reason is ignored, not shown as a guess', () => {
  assert.equal(fallbackBadge(status({ reason: 'mystery' }), 'zai.glm-5.3', MODELS), null)
})
