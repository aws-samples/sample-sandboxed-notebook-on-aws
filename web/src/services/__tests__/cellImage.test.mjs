// Unit tests for services/cellImage.js — persisted chart loading (fetch with the
// auth header → Blob → refcounted object URL), the 404 fallback, and export
// embedding. Run with: node --test web/src/services/__tests__/cellImage.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cellImagePath, createCellImageLoader, imageShaFromKey, loadCellImageDataUrl,
  notebookIdFromKey, resolveExportImages,
} from '../cellImage.js'

const SHA = 'a'.repeat(64)
const KEY = `notebook-outputs/12/c1/${SHA}.png`

const pngBlob = () => new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: 'image/png' })
const resp = (status, blob = pngBlob()) => ({ status, ok: status >= 200 && status < 300, blob: async () => blob })

function harness(handler) {
  const calls = []
  const created = []
  const revoked = []
  let n = 0
  const loader = createCellImageLoader({
    fetchImpl: async (path) => { calls.push(path); return handler(path, calls.length) },
    createObjectURL: (blob) => { const u = `blob:test/${++n}`; created.push([u, blob]); return u },
    revokeObjectURL: (u) => revoked.push(u),
  })
  return { loader, calls, created, revoked }
}

test('key parsing: sha + notebook id; malformed keys → null', () => {
  assert.equal(imageShaFromKey(KEY), SHA)
  assert.equal(notebookIdFromKey(KEY), '12')
  for (const bad of [null, '', 'x', `notebook-outputs/12/c1/${SHA.toUpperCase()}.png`,
    `notebook-outputs/12/../c1/${SHA}.png`, `notebook-outputs/ab/c1/${SHA}.png`, `${KEY}.jpg`]) {
    assert.equal(imageShaFromKey(bad), null, String(bad))
    assert.equal(notebookIdFromKey(bad), null, String(bad))
  }
})

test('the route path encodes the cell id and carries the sha', () => {
  assert.equal(cellImagePath('c1', KEY), `/notebooks/12/cells/c1/image?sha=${SHA}`)
  assert.equal(cellImagePath('a/b?c', KEY), `/notebooks/12/cells/a%2Fb%3Fc/image?sha=${SHA}`)
  assert.equal(cellImagePath('c1', 'garbage'), null)
})

test('200 → ok with an object URL', async () => {
  const h = harness(() => resp(200))
  const r = await h.loader.acquire('c1', KEY)
  assert.equal(r.status, 'ok')
  assert.equal(r.url, 'blob:test/1')
  assert.deepEqual(h.calls, [`/notebooks/12/cells/c1/image?sha=${SHA}`])
})

test('malformed key → missing with no fetch', async () => {
  const h = harness(() => resp(200))
  assert.deepEqual(await h.loader.acquire('c1', 'notebook-outputs/x'), { status: 'missing' })
  assert.equal(h.calls.length, 0)
})

test('404 → missing, cached for the session (no refetch)', async () => {
  const h = harness(() => resp(404))
  assert.deepEqual(await h.loader.acquire('c1', KEY), { status: 'missing' })
  h.loader.release(KEY)
  assert.deepEqual(await h.loader.acquire('c1', KEY), { status: 'missing' })
  h.loader.release(KEY)
  assert.equal(h.calls.length, 1)
  assert.equal(h.created.length, 0)
})

test('500 / network error / wrong blob type → error, not cached (a retry refetches)', async () => {
  for (const handler of [
    () => resp(500),
    () => { throw new Error('network down') },
    () => resp(200, new Blob(['<html>'], { type: 'text/html' })),
  ]) {
    const h = harness(handler)
    assert.deepEqual(await h.loader.acquire('c1', KEY), { status: 'error' })
    h.loader.release(KEY)
    assert.deepEqual(await h.loader.acquire('c1', KEY), { status: 'error' })
    h.loader.release(KEY)
    assert.equal(h.calls.length, 2)
    assert.equal(h.created.length, 0)
  }
})

test('in-flight requests for the same key are deduplicated', async () => {
  let release
  const gate = new Promise(r => { release = r })
  const h = harness(async () => { await gate; return resp(200) })
  const p1 = h.loader.acquire('c1', KEY)
  const p2 = h.loader.acquire('c1', KEY)   // e.g. the focus overlay
  release()
  const [a, b] = await Promise.all([p1, p2])
  assert.equal(h.calls.length, 1)
  assert.equal(a.url, b.url)
})

test('refcounted release revokes exactly once, at zero', async () => {
  const h = harness(() => resp(200))
  const a = await h.loader.acquire('c1', KEY)
  await h.loader.acquire('c1', KEY)
  h.loader.release(KEY)
  assert.deepEqual(h.revoked, [])
  h.loader.release(KEY)
  assert.deepEqual(h.revoked, [a.url])
  h.loader.release(KEY)   // extra release is a no-op
  assert.deepEqual(h.revoked, [a.url])
})

test('released before the fetch resolves → no object URL is created (no leak)', async () => {
  let release
  const gate = new Promise(r => { release = r })
  const h = harness(async () => { await gate; return resp(200) })
  const p = h.loader.acquire('c1', KEY)
  h.loader.release(KEY)
  release()
  assert.equal((await p).status, 'error')
  assert.equal(h.created.length, 0)
})

test('a new key (re-run) is a separate fetch; the old URL is revoked on release', async () => {
  const h = harness(() => resp(200))
  const KEY2 = `notebook-outputs/12/c1/${'b'.repeat(64)}.png`
  const a = await h.loader.acquire('c1', KEY)
  h.loader.release(KEY)
  const b = await h.loader.acquire('c1', KEY2)
  assert.notEqual(a.url, b.url)
  assert.deepEqual(h.revoked, [a.url])
})

test('loadCellImageDataUrl: data URL on success, null on 404 / bad type / error', async () => {
  const toDataUrl = async () => 'data:image/png;base64,AAA'
  assert.equal(await loadCellImageDataUrl(async () => resp(200), 'c1', KEY, toDataUrl), 'data:image/png;base64,AAA')
  assert.equal(await loadCellImageDataUrl(async () => resp(404), 'c1', KEY, toDataUrl), null)
  assert.equal(await loadCellImageDataUrl(async () => resp(200, new Blob(['x'], { type: 'text/plain' })), 'c1', KEY, toDataUrl), null)
  assert.equal(await loadCellImageDataUrl(async () => { throw new Error('x') }, 'c1', KEY, toDataUrl), null)
  assert.equal(await loadCellImageDataUrl(async () => resp(200), 'c1', 'bad', toDataUrl), null)
})

test('resolveExportImages fills key-only cells, keeps fresh images, drops failed fetches', async () => {
  const cells = [
    { id: 'a', imageS3Key: KEY },
    { id: 'b', image: 'data:image/png;base64,FRESH', imageS3Key: KEY },
    { id: 'c', imageS3Key: KEY },
    { id: 'd', output: 'text' },
  ]
  const asked = []
  const out = await resolveExportImages(cells, async (id) => { asked.push(id); return id === 'a' ? 'data:image/png;base64,A' : null })
  assert.deepEqual(asked, ['a', 'c'])
  assert.equal(out[0].image, 'data:image/png;base64,A')
  assert.equal(out[1].image, 'data:image/png;base64,FRESH')
  assert.equal(out[2].image, undefined)
  assert.equal(out[3], cells[3])
  assert.equal(cells[0].image, undefined, 'input not mutated')
})

test('multi-output images load by ?key= (any image of the cell); the default stays ?sha=', async () => {
  assert.equal(cellImagePath('c1', KEY), `/notebooks/12/cells/c1/image?sha=${SHA}`)
  assert.equal(cellImagePath('c1', KEY, { any: true }), `/notebooks/12/cells/c1/image?key=${SHA}`)
  assert.equal(cellImagePath('c1', 'bad', { any: true }), null)
  const h = harness(() => resp(200))
  const r = await h.loader.acquire('c1', KEY, { any: true })
  assert.equal(r.status, 'ok')
  assert.deepEqual(h.calls, [`/notebooks/12/cells/c1/image?key=${SHA}`])
  const legacy = harness(() => resp(200))
  await legacy.loader.acquire('c1', KEY)
  assert.deepEqual(legacy.calls, [`/notebooks/12/cells/c1/image?sha=${SHA}`])
  const urls = []
  const data = await loadCellImageDataUrl(async (p) => { urls.push(p); return resp(200) }, 'c1', KEY, async () => 'data:x', { any: true })
  assert.equal(data, 'data:x')
  assert.deepEqual(urls, [`/notebooks/12/cells/c1/image?key=${SHA}`])
})
