// Unit tests for services/lineage.js over STORED DAG documents (real builder
// output from tests/fixtures/gen_dag_fixtures.py → tests/fixtures/dag_*.json).
// Run with: node --test web/src/services/__tests__/lineage.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  buildLineage, lineageTrustLevels, buildExplainPayload, relativeTime, matchCatalog, stepTitle,
  cellIssueSummary, issueMessage, staleReasonText, groupDependencyIssues,
} from '../lineage.js'
import { cellBadgeMap } from '../../utils/cellBadge.js'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..', 'tests', 'fixtures')
const scenarios = (name) => JSON.parse(readFileSync(join(FIXTURES, name), 'utf8')).scenarios
const LIN = scenarios('dag_lineage.json')

const TABLE_HTML = JSON.stringify({
  __df_table__: true,
  columns: ['month', 'revenue'],
  cells: [['2024-01', 'SECRET_VALUE_123'], ['2024-02', '9876.54']],
  total_rows: 2, shown_rows: 2, total_cols: 2, shown_cols: 2,
})

const CATALOG = [
  { source_type: 'athena', source_id: 'microvm_demo_db.orders', display_name: 'Demo orders' },
  { source_type: 's3', source_id: 's3://b/data/', display_name: 'Data folder' },
]

function ran(cell) {
  return { executionNumber: 1, lastExecutedCode: cell.code, executedAt: '2024-01-01T00:00:00Z', ...cell }
}

// Live cells for the param → SQL → Python notebook (ids '1'..'5', 4 = markdown).
function fixture(name = 'base') {
  const sc = LIN[name]
  const [c1, c2, c3, c4, c5] = sc.cells
  const cells = [
    ran({ ...c1 }),
    ran({ ...c2, html: TABLE_HTML, engine: 'athena' }),
    ran({ ...c3, html: TABLE_HTML, output: 'TOTAL_SECRET_OUTPUT' }),
    { ...c4 },
    ran({ ...c5, output: '1' }),
  ]
  return { cells, dag: sc.doc }
}

test('steps are ordered upstream-first with badges and titles; unrelated cells excluded', () => {
  const { cells, dag } = fixture()
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.deepEqual(lin.steps.map(s => s.cellId), ['1', '2', '3'])
  assert.deepEqual(lin.steps.map(s => s.badge), ['PY-1', 'SQL-1', 'PY-2'])
  assert.equal(lin.steps[0].title, 'region = "na"')
  assert.equal(lin.steps[1].title, 'Orders for the selected region')
  assert.equal(lin.steps[2].title, 'Revenue by month')
  assert.deepEqual(lin.steps[2].varsIn, ['orders_df'])
  assert.deepEqual(lin.steps[1].varsIn, ['region'])
  assert.equal(lin.steps[1].engine, 'athena')
  assert.equal(lin.cycle, false)
  assert.deepEqual(lin.issues, [])
})

test('catalogued source and current steps → green', () => {
  const { cells, dag } = fixture()
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.sources.length, 1)
  assert.equal(lin.sources[0].inCatalog, true)
  assert.equal(lin.sources[0].catalogRef.display_name, 'Demo orders')
  assert.deepEqual(lin.sources[0].steps, [{ cellId: '2', badge: 'SQL-1' }])
  assert.equal(lin.trust.level, 'green')
  assert.deepEqual(lin.trust.reasons, ['No uncatalogued sources recorded; every step is current'])
})

test('url and unknown s3 sources → amber with reasons; external_state goes to notes only', () => {
  const { cells, dag } = fixture('url_s3')
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.trust.level, 'amber')
  assert.ok(lin.trust.reasons.some(r => r.includes('https://example.com/data.csv') && r.includes('URL')))
  assert.ok(lin.trust.reasons.includes('s3://other/x.csv is not in the data catalog — via PY-2'))
  assert.ok(lin.notes.includes("PY-2: reads url https://example.com/data.csv; the cell won't re-run when the source changes"))
  assert.ok(!lin.trust.reasons.some(r => r.includes("won't re-run")), 'info issues never become trust reasons')
  assert.deepEqual(lin.issues.map(i => [i.cellId, i.code, i.severity]),
    [['3', 'external_state', 'info'], ['3', 'external_state', 'info']])
})

test('raw HTTP host source (VM http_capture) → ad-hoc url chip and amber trust', () => {
  const { cells, dag } = fixture('url_host')
  const lin = buildLineage(3, cells, dag, CATALOG)
  const src = lin.sources.find(s => s.kind === 'url')
  assert.deepEqual(src, {
    kind: 'url', id: 'https://date.nager.at', inCatalog: false, catalogRef: null,
    steps: [{ cellId: '3', badge: 'PY-2' }], readByThisCell: true,
  })
  assert.equal(lin.trust.level, 'amber')
  assert.ok(lin.trust.reasons.some(r => r.includes('https://date.nager.at') && r.includes('URL')))
  assert.ok(!lin.trust.reasons.some(r => r.includes('No uncatalogued sources recorded')))
  assert.equal(lineageTrustLevels(cells, dag, CATALOG).get('3'), 'amber')
})

test('s3 folder catalog entry matches files under it', () => {
  assert.deepEqual(matchCatalog('s3', 's3://b/data/x.csv', CATALOG), { source_id: 's3://b/data/', display_name: 'Data folder' })
  assert.equal(matchCatalog('s3', 's3://b/other.csv', CATALOG), null)
  assert.equal(matchCatalog('url', 's3://b/data/x.csv', CATALOG), null)
  assert.ok(matchCatalog('athena', 'MICROVM_DEMO_DB.ORDERS', CATALOG))
})

test('edited step (live code differs from last run) → amber', () => {
  const { cells, dag } = fixture()
  cells[1].code = cells[1].code + ' LIMIT 5'
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.steps[1].status, 'edited')
  assert.equal(lin.trust.level, 'amber')
  assert.ok(lin.trust.reasons.includes('SQL-1 edited since last run'))
})

test('never-run upstream → amber', () => {
  const { cells, dag } = fixture('never_run_upstream')
  cells[1] = { ...LIN.never_run_upstream.cells[1] }
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.deepEqual(lin.steps.map(s => s.cellId), ['1', '2', '3'], 'the static edge keeps the never-run producer')
  assert.equal(lin.steps[1].status, 'stale')
  assert.equal(lin.trust.level, 'amber')
  assert.ok(lin.trust.reasons.includes('SQL-1 has not been run'))
  assert.ok(!lin.notes.some(n => n.startsWith('SQL-1')), 'never-run step is not flagged as not-captured')
})

test('upstream error → red', () => {
  const { cells, dag } = fixture()
  cells[1].error = 'boom'
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.trust.level, 'red')
  assert.deepEqual(lin.trust.reasons, ['SQL-1 errored'])
})

test('variable produced by a deleted cell → amber missing_producer (same text as before)', () => {
  const { cells, dag } = fixture('missing_producer')
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.trust.level, 'amber')
  assert.ok(lin.trust.reasons.includes('"lookup" has no producing cell (deleted, not run, or left by an AI exploration)'))
  assert.deepEqual(lin.issues.map(i => [i.code, i.severity, i.var]), [['missing_producer', 'amber', 'lookup']])
})

test('helper names are not inputs', () => {
  const { cells, dag } = fixture()
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.ok(!lin.steps[2].varsIn.includes('read_athena'))
  assert.ok(!lin.trust.reasons.some(r => r.includes('read_athena')))
})

test('node.sources null on an executed step → amber + note', () => {
  const { cells, dag } = fixture('sources_not_captured')
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.trust.level, 'amber')
  assert.equal(lin.steps[2].sourcesCaptured, false)
  assert.ok(lin.notes.includes('PY-2: sources not captured — re-run'))
  assert.ok(lin.trust.reasons.includes('PY-2: sources not captured — re-run'))
})

test('dependency cycle is detected from the stored issues and terminates', () => {
  const sc = scenarios('dag_cycle.json').ran
  const cells = sc.cells.map((c, i) => ran({ ...c, output: String(i + 1) }))
  const lin = buildLineage('a', cells, sc.doc, [])
  assert.equal(lin.cycle, true)
  assert.deepEqual(lin.steps.map(s => s.cellId), ['a', 'b'])
  assert.equal(lin.trust.level, 'amber')
  assert.ok(lin.trust.reasons.includes('dependency cycle'))
  assert.equal(lin.trust.reasons.filter(r => r === 'dependency cycle').length, 1)
  assert.ok(lin.trust.reasons.includes('PY-1 reads x from PY-2, which comes after it in the notebook'))
})

test('issue-driven trust: amber codes weaken trust, info codes are notes only', () => {
  const sc = scenarios('dag_issues.json').every_code
  const cells = sc.cells.map(c => ran({ ...c, output: 'ok' }))
  const badges = cellBadgeMap(cells)
  // i4 reads df (redefined by i3 — the nearest earlier producer) and the missing `ghost`.
  const lin = buildLineage('i4', cells, sc.doc, [])
  assert.deepEqual(lin.steps.map(s => s.cellId), ['i3', 'i4'])
  assert.ok(lin.trust.reasons.includes('"ghost" has no producing cell (deleted, not run, or left by an AI exploration)'))
  assert.ok(lin.notes.includes('PY-3: redefines df, also defined in PY-1'))
  assert.equal(lin.issues[0].severity, 'amber', 'amber first')
  // i2 mutates the frame i1 loaded from url/s3/local/athena sources.
  const lin2 = buildLineage('i2', cells, sc.doc, [{ source_type: 'athena', source_id: 'microvm_demo_db.orders' }])
  assert.ok(lin2.notes.includes('PY-2: mutates df defined in PY-1'))
  assert.ok(lin2.notes.includes("PY-1: reads local /tmp/x.csv; the cell won't re-run when the source changes"))
  assert.ok(!lin2.notes.some(n => n.includes('microvm_demo_db.orders')), 'athena raises no external_state issue')
  // dynamic_names and out_of_order are amber reasons with the step badge.
  const dyn = buildLineage('i5', cells, sc.doc, [])
  assert.ok(dyn.trust.reasons.includes('PY-5 uses exec(); its reads and writes may be incomplete'), dyn.trust.reasons.join(' | '))
  const ooo = buildLineage('i6', cells, sc.doc, [])
  assert.ok(ooo.trust.reasons.includes('PY-6 reads early from PY-7, which comes after it in the notebook'))
  // Info-only lineage stays green.
  const mut = scenarios('dag_two_mutators.json').ran
  const mcells = mut.cells.map(c => ran({ ...c, output: 'ok' }))
  const mlin = buildLineage('end', mcells, mut.doc, [])
  assert.equal(mlin.trust.level, 'green')
  assert.ok(mlin.notes.includes('PY-2: mutates df defined in PY-1'))
  // Messages are rendered from structured fields (badges, not cell ids) and never truncated.
  const issue = sc.doc.issues.find(i => i.code === 'cycle' && i.cell_id === 'i8')
  assert.equal(issueMessage(issue, badges), 'is part of a dependency cycle with PY-9')
  assert.equal(issueMessage({ code: 'external_state', source: { kind: 'url', id: 'https://x/' + 'a'.repeat(300) } }, badges).length,
    "reads url https://x/; the cell won't re-run when the source changes".length + 300)
})

test('cellIssueSummary drives the accessible issue badge', () => {
  const sc = scenarios('dag_issues.json').every_code
  const badges = cellBadgeMap(sc.cells)
  assert.equal(cellIssueSummary(sc.doc, 'i7', badges), null)
  const i8 = cellIssueSummary(sc.doc, 'i8', badges)
  assert.equal(i8.count, 2)
  assert.equal(i8.amber, 2)
  assert.equal(i8.ariaLabel, '2 dependency notes, 2 warnings')
  const i1 = cellIssueSummary(sc.doc, 'i1', badges)
  assert.equal(i1.count, 3)
  assert.equal(i1.amber, 0)
  assert.equal(i1.ariaLabel, '3 dependency notes')
  assert.ok(i1.title.includes('https://example.com/feed.json'))
  assert.equal(cellIssueSummary(null, 'i1', badges), null)
})

test('finding 5: node edited + live code === lastExecutedCode → current; + stale_reason → stale', () => {
  const st = scenarios('dag_status.json')
  const live = (sc) => sc.cells.map(c => ran({ ...c, code: (sc.live_code || {})[c.id] || c.code, output: 'ok' }))
  const lin = buildLineage('c3', live(st.edited_unsaved), st.edited_unsaved.doc, [])
  assert.deepEqual(lin.steps.map(s => s.status), ['current', 'current', 'current'])
  assert.equal(lin.trust.level, 'green')
  const lin2 = buildLineage('c3', live(st.edited_with_reason), st.edited_with_reason.doc, [])
  assert.deepEqual(lin2.steps.map(s => s.status), ['current', 'stale', 'stale'])
  assert.ok(lin2.trust.reasons.includes('PY-2 is stale (upstream changed)'))
  const typed = live(st.edited_unsaved)
  typed[1] = { ...typed[1], code: 'y = x + 3' }
  assert.equal(buildLineage('c3', typed, st.edited_unsaved.doc, []).steps[1].status, 'edited')
  const cold = buildLineage('c3', live(st.not_in_checkpoint), st.not_in_checkpoint.doc, [])
  assert.ok(cold.trust.reasons.includes("PY-1: Not in the current session's memory — re-run to update"))
})

test('staleReasonText renders vars and via badges, and the not-in-memory text', () => {
  const st = scenarios('dag_status.json')
  const badges = cellBadgeMap(st.edited_with_reason.cells)
  const node = (sc, id) => sc.doc.nodes.find(n => n.cell_id === id)
  assert.equal(staleReasonText(node(st.edited_with_reason, 'c2').stale_reason, badges),
    'Stale: upstream x changed since this cell last ran (via PY-1) — re-run to update')
  assert.equal(staleReasonText(node(st.edited_with_reason, 'c3').stale_reason, badges),
    'Stale: an upstream dependency changed since this cell last ran (via PY-2) — re-run to update')
  assert.equal(staleReasonText(node(st.not_in_checkpoint, 'c1').stale_reason, badges),
    "Not in the current session's memory — re-run to update")
  assert.equal(staleReasonText(null, badges), null)
})

test('inputs use the dropdown option label', () => {
  const { cells, dag } = fixture()
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.deepEqual(lin.inputs, [{ cellId: '1', varName: 'region', label: 'Region', value: 'North America' }])
})

test('lineageTrustLevels covers only output-bearing non-markdown cells', () => {
  const { cells, dag } = fixture()
  cells[0].output = undefined
  const levels = lineageTrustLevels(cells, dag, CATALOG)
  assert.deepEqual([...levels.keys()].sort(), ['2', '3', '5'])
  assert.equal(levels.get('3'), 'green')
  assert.equal(levels.get('5'), 'green')
})

test('without a document yet: steps are the cell itself and nothing is flagged as not captured', () => {
  const { cells } = fixture()
  const lin = buildLineage(3, cells, null, CATALOG)
  assert.deepEqual(lin.steps.map(s => s.cellId), ['3'])
  assert.equal(lin.steps[0].status, 'current')
  assert.deepEqual(lin.notes, [])
})

test('buildExplainPayload carries shape, not values', () => {
  const { cells, dag } = fixture()
  const lin = buildLineage(3, cells, dag, CATALOG)
  const payload = buildExplainPayload(lin, cells)
  assert.equal(payload.output.var, 'monthly')
  assert.equal(payload.output.kind, 'table')
  assert.deepEqual(payload.output.columns, ['month', 'revenue'])
  assert.equal(payload.output.rows, 2)
  assert.equal(payload.output.cols, 2)
  assert.deepEqual(payload.steps.map(s => s.badge), ['PY-1', 'SQL-1', 'PY-2'])
  assert.equal(payload.steps[1].code, cells[1].code)
  assert.deepEqual(payload.sources, [{ kind: 'athena', id: 'microvm_demo_db.orders' }])
  const json = JSON.stringify(payload)
  assert.ok(!json.includes('SECRET_VALUE_123'))
  assert.ok(!json.includes('9876.54'))
  assert.ok(!json.includes('TOTAL_SECRET_OUTPUT'))
})

test('stepTitle skips directives and truncates', () => {
  assert.equal(stepTitle({ type: 'code', code: '# @app-hide\n\nx = 1' }, 'PY-1'), 'x = 1')
  assert.equal(stepTitle({ type: 'markdown', code: '# Title' }, 'MD-1'), 'MD-1')
  assert.equal(stepTitle({ type: 'code', code: '' }, 'PY-9'), 'PY-9')
  const long = stepTitle({ type: 'code', code: '# ' + 'a'.repeat(200) }, 'PY-1')
  assert.equal(long.length, 80)
  assert.ok(long.endsWith('…'))
})

test('relativeTime buckets', () => {
  const now = Date.parse('2024-01-10T12:00:00Z')
  const ago = (s) => new Date(now - s * 1000).toISOString()
  assert.equal(relativeTime(null, now), 'never')
  assert.equal(relativeTime('not a date', now), 'never')
  assert.equal(relativeTime(ago(10), now), 'just now')
  assert.equal(relativeTime(ago(-60), now), 'just now')
  assert.equal(relativeTime(ago(60), now), '1 min ago')
  assert.equal(relativeTime(ago(5 * 60), now), '5 min ago')
  assert.equal(relativeTime(ago(3600), now), '1 hour ago')
  assert.equal(relativeTime(ago(3 * 3600), now), '3 hours ago')
  assert.equal(relativeTime(ago(86400), now), '1 day ago')
  assert.equal(relativeTime(ago(3 * 86400), now), '3 days ago')
})

// ─── deleted producer still in memory (deleted-cell flag F1, item 5) ─────────
const DELETED_IN_MEMORY = '"lookup" was defined by a deleted cell. It\'s in memory now but won\'t survive a restart, a Run All on a fresh VM, or a restore.'

test('missing_producer: deleted producer + in memory → explained text', () => {
  const issue = { code: 'missing_producer', var: 'lookup', related: [] }
  assert.equal(issueMessage(issue, new Map(), null, { deletedInMemory: new Set(['lookup']) }), DELETED_IN_MEMORY)
  const { dag } = fixture('missing_producer')
  const cellId = dag.issues.find(i => i.code === 'missing_producer').cell_id
  const s = cellIssueSummary(dag, cellId, new Map(), { deletedInMemory: new Set(['lookup']) })
  assert.ok(s.messages.includes(DELETED_IN_MEMORY))
  assert.ok(s.title.includes(DELETED_IN_MEMORY))
})

test('missing_producer: genuinely missing (no ctx, or ctx without the name) → existing text unchanged', () => {
  const issue = { code: 'missing_producer', var: 'lookup', related: [] }
  const plain = issueMessage(issue, new Map(), null)
  assert.equal(plain, '"lookup" has no producing cell (deleted, not run, or left by an AI exploration)')
  assert.equal(issueMessage(issue, new Map(), null, { deletedInMemory: new Set(['other']) }), plain)
  const { dag } = fixture('missing_producer')
  const cellId = dag.issues.find(i => i.code === 'missing_producer').cell_id
  assert.ok(cellIssueSummary(dag, cellId, new Map()).messages.includes(plain))
})

// ── Dependency-notes grouping (drawer): one callout per (cell, source kind) ──────────
const ext = (cellId, kind, id) => ({
  cellId, badge: `PY-${cellId}`, code: 'external_state', severity: 'info', var: null,
  message: `reads ${kind} ${id}; the cell won't re-run when the source changes`, source: { kind, id },
})
const B = 's3://microvm-sandbox-artifacts-123456789012-us-west-2/samples/'

test('groupDependencyIssues: two S3 files read by one cell → ONE entry naming both files', () => {
  const out = groupDependencyIssues([
    ext('5', 's3', B + 'clickstream_events.csv'), ext('5', 's3', B + 'inventory_daily.csv'),
  ])
  assert.equal(out.length, 1)
  assert.equal(out[0].shortDetail, 'reads 2 S3 sources: clickstream_events.csv, inventory_daily.csv')
  assert.equal(out[0].count, 2)
  assert.ok(out[0].message.includes(B + 'clickstream_events.csv') && out[0].message.includes(B + 'inventory_daily.csv'),
    'the full ids stay in the message (tooltip / screen reader)')
  assert.ok(out[0].message.endsWith("the cell won't re-run when the sources change"))
  assert.equal(out[0].sources.length, 2)
  assert.equal(out[0].code, 'external_state')
})

test('groupDependencyIssues: different kinds and different cells stay separate; singles are untouched', () => {
  const a = ext('5', 's3', B + 'a.csv'), u = ext('5', 'url', 'https://date.nager.at'), c = ext('6', 's3', B + 'b.csv')
  const out = groupDependencyIssues([a, u, c])
  assert.equal(out.length, 3)
  assert.deepEqual(out, [a, u, c], 'a group of one passes through as the same object content')
  assert.ok(out.every(i => !i.shortDetail))
})

test('groupDependencyIssues: order follows the first member; non-external issues pass through in place', () => {
  const mp = { cellId: '5', badge: 'PY-5', code: 'missing_producer', severity: 'amber', var: 'x', message: 'm', source: null }
  const out = groupDependencyIssues([ext('5', 's3', B + 'a.csv'), mp, ext('5', 's3', B + 'b.csv')])
  assert.equal(out.length, 2)
  assert.equal(out[0].count, 2)
  assert.equal(out[1], mp)
})

test('groupDependencyIssues: same file name in different folders keeps the full id so they stay distinct', () => {
  const out = groupDependencyIssues([ext('5', 's3', 's3://b/one/data.csv'), ext('5', 's3', 's3://b/two/data.csv')])
  assert.equal(out[0].shortDetail, 'reads 2 S3 sources: s3://b/one/data.csv, s3://b/two/data.csv')
})

test('groupDependencyIssues: empty / missing input and issues without a source are safe', () => {
  assert.deepEqual(groupDependencyIssues(undefined), [])
  assert.deepEqual(groupDependencyIssues([]), [])
  const noSrc = { cellId: '1', code: 'external_state', message: 'x', source: null }
  assert.deepEqual(groupDependencyIssues([noSrc, noSrc]), [noSrc, noSrc])
})

test('buildLineage keeps each stored issue (one per source) and now carries its source', () => {
  const { cells, dag } = fixture('url_s3')
  const lin = buildLineage(3, cells, dag, CATALOG)
  assert.equal(lin.issues.filter(i => i.code === 'external_state').length, 2, 'storage granularity unchanged')
  assert.ok(lin.issues.filter(i => i.code === 'external_state').every(i => i.source && i.source.kind))
  assert.equal(groupDependencyIssues(lin.issues).length, 2, 'url + s3 are different kinds → two callouts')
})
