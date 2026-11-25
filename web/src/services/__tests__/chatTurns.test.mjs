// Unit tests for services/chatTurns.js (AI chat turn grouping, titles, steps,
// insert status, store transitions).
// Run with: node --test web/src/services/__tests__/chatTurns.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import {
  normalizeFences, extractCodeBlocks, langKey, insertableType, INSERTABLE,
  hashString, turnFingerprint, blockKey, groupTurns, turnBlocks,
  splitStatusNotes, turnSteps, stripInlineMarkdown, extractTitle, parseIntelPrompt,
  questionLabel, turnTitle, insertStatus, effectiveCollapsed, collapseAll,
  pruneStore, reconcileSession, setTurnEntry, parseStore, emptyStore, syncTurnState,
  PRUNE_TTL_MS, SEEN_REFRESH_MS,
  normalizeForCompare, showQuestionSubtitle, railOpenReducer, railIsOpen, RAIL_CLOSED,
  contextTurnCount, outOfContextKeys,
} from '../chatTurns.js'

const F = '```'
const user = (content, extra = {}) => ({ role: 'user', content, ...extra })
const ai = (content, extra = {}) => ({ role: 'assistant', content, ...extra })

// Panel-equivalent marked config (breaks + KaTeX) for the parity test.
const panelMarked = new Marked({ breaks: true }).use(markedKatex({ throwOnError: false, nonStandard: true }))
function panelCodeTokens(md) {
  const out = []
  panelMarked.walkTokens(panelMarked.lexer(normalizeFences(md)), (t) => {
    if (t.type !== 'code') return
    const k = langKey(t.lang)
    if (!(k in INSERTABLE) || !t.text.trim()) return
    out.push({ code: t.text.trim(), type: INSERTABLE[k] })
  })
  return out
}

// ── normalizeFences ──────────────────────────────────────────────────────────

test('normalizeFences: glued fence is split onto its own line', () => {
  const out = normalizeFences(`Here it is.${F}python\nx = 1\n${F}`)
  assert.equal(out, `Here it is.\n${F}python\nx = 1\n${F}`)
})

test('normalizeFences: list-nested fence opener line stays intact', () => {
  const md = `1. Step\n   ${F}python\n   import x\n   y = 1\n   ${F}\n`
  assert.equal(normalizeFences(md), md)
})

test('normalizeFences: duplicate opener collapsed, missing closer auto-closed', () => {
  assert.equal(normalizeFences(`${F}python\n${F}python\nx=1\n${F}`), `${F}python\nx=1\n${F}`)
  assert.equal(normalizeFences(`${F}markdown\n# T\n${F}python\nx=1\n${F}`), `${F}markdown\n# T\n${F}\n${F}python\nx=1\n${F}`)
})

test('normalizeFences: unclosed fence at EOF is closed', () => {
  assert.equal(normalizeFences(`${F}sql\nselect 1`), `${F}sql\nselect 1\n${F}`)
})

test('normalizeFences: info-string fence closer is not mistaken for an opener', () => {
  const md = `${F}python title="a"\nx=1\n${F}\nSome prose\n${F}sql\nselect 1\n${F}`
  assert.equal(normalizeFences(md), md)
})

test('normalizeFences: no fences → unchanged', () => {
  assert.equal(normalizeFences('plain text'), 'plain text')
  assert.equal(normalizeFences(''), '')
})

// ── extractCodeBlocks ────────────────────────────────────────────────────────

test('extractCodeBlocks: list-nested python fence comes back dedented', () => {
  const blocks = extractCodeBlocks(`1. Step\n   ${F}python\n   import x\n   y = 1\n   ${F}\n`)
  assert.deepEqual(blocks, [{ code: 'import x\ny = 1', type: 'code' }])
})

test('extractCodeBlocks: info-string fence then prose then sql', () => {
  const blocks = extractCodeBlocks(`${F}python title="a"\nx=1\n${F}\nSome prose\n${F}sql\nselect 1\n${F}`)
  assert.deepEqual(blocks, [{ code: 'x=1', type: 'code' }, { code: 'select 1', type: 'sql' }])
})

test('extractCodeBlocks: json/bash before python keep python, no prose leaks', () => {
  const md = `Intro\n${F}json\n{"a":1}\n${F}\nMiddle prose\n${F}bash\npip install x\n${F}\n${F}python\nprint(1)\n${F}\nEnd.`
  assert.deepEqual(extractCodeBlocks(md), [{ code: 'print(1)', type: 'code' }])
})

test('extractCodeBlocks: Python capital, py, ~~~sql, mermaid skipped, bare fence = code', () => {
  const md = `${F}Python\na=1\n${F}\n${F}py\nb=2\n${F}\n~~~sql\nselect 2\n~~~\n${F}mermaid\ngraph TD\n${F}\n${F}\nc=3\n${F}`
  assert.deepEqual(extractCodeBlocks(md), [
    { code: 'a=1', type: 'code' }, { code: 'b=2', type: 'code' },
    { code: 'select 2', type: 'sql' }, { code: 'c=3', type: 'code' },
  ])
})

test('extractCodeBlocks: unclosed fence at EOF is extracted (matches the rendered card)', () => {
  assert.deepEqual(extractCodeBlocks(`Text\n${F}python\nx = 1\ny`), [{ code: 'x = 1\ny', type: 'code' }])
})

test('extractCodeBlocks: glued and duplicate-opener cases', () => {
  assert.deepEqual(extractCodeBlocks(`See (Athena).${F}markdown\n# Title\n${F}`), [{ code: '# Title', type: 'markdown' }])
  assert.deepEqual(extractCodeBlocks(`${F}sql\n${F}sql\nselect 1\n${F}`), [{ code: 'select 1', type: 'sql' }])
})

test('extractCodeBlocks: empty fences are not insertable cells', () => {
  // ```markdown with a nested ```python: auto-close leaves a stray trailing ``` fence.
  const md = `${F}markdown\n# T\n${F}python\nx=1\n${F}\n${F}`
  const blocks = extractCodeBlocks(md)
  assert.ok(blocks.every(b => b.code.length > 0))
  assert.deepEqual(blocks, [{ code: '# T', type: 'markdown' }, { code: 'x=1', type: 'code' }])
  assert.deepEqual(extractCodeBlocks(`${F}python\n\n${F}`), [])
})

test('extractCodeBlocks: parity with the panel marked config over a corpus', () => {
  const corpus = [
    `# Funnel Segmentation: Device × Channel × Cohort\n\n${F}markdown\n## Overview\nText $x^2$\n${F}\n${F}python\nimport pandas as pd\ndf = pd.read_csv('a.csv')\n${F}\n${F}sql\nSELECT * FROM t\n${F}\nDone.`,
    `1. First\n   ${F}python\n   a = 1\n   ${F}\n2. Second\n   ${F}sql\n   select 1\n   ${F}`,
    `> quoted\n> ${F}python\n> q = 1\n> ${F}`,
    `$$\nE = mc^2\n$$\n${F}python title="x"\nz = 3\n${F}\nprose${F}sql\nselect 9\n${F}`,
  ]
  for (const md of corpus) assert.deepEqual(extractCodeBlocks(md), panelCodeTokens(md))
})

test('langKey / insertableType', () => {
  assert.equal(langKey(''), '')
  assert.equal(langKey('Python'), 'python')
  assert.equal(langKey('python title="a"'), 'python')
  assert.equal(langKey('  sql  '), 'sql')
  assert.equal(langKey('mermaid'), 'mermaid')
  assert.equal(insertableType('mermaid'), null)
  assert.equal(insertableType('bash'), null)
  assert.equal(insertableType('constructor'), null)
  assert.equal(insertableType('SQL'), 'sql')
})

// ── groupTurns / keys ────────────────────────────────────────────────────────

test('groupTurns: empty and single', () => {
  assert.deepEqual(groupTurns([]), [])
  const t = groupTurns([user('hi'), ai('hello')])
  assert.equal(t.length, 1)
  assert.equal(t[0].userIndex, 0)
  assert.equal(t[0].assistant.length, 1)
  assert.equal(t[0].assistant[0].index, 1)
})

test('groupTurns: multi-step turns with resume, notes, error bubble, paused bubble', () => {
  const msgs = [
    user('Q1'),
    ai('_(Switched to Haiku: GLM is cooling down)_\n\n# Answer 1\n' + `${F}python\nx=1\n${F}`, { timestamp: 't' }),
    user('Q2'),
    ai('partial before approval'),                       // paused bubble (no timestamp)
    user('Q3'),
    ai('Error: boom', { isError: true }),
    user('Q4'),
  ]
  const turns = groupTurns(msgs)
  assert.equal(turns.length, 4)
  assert.deepEqual(turns.map(t => t.assistant.length), [1, 1, 1, 0])
  assert.deepEqual(turns.map(t => t.ordinal), [0, 1, 2, 3])
})

test('groupTurns: multi-bubble turn and leading assistant', () => {
  const turns = groupTurns([ai('welcome'), user('Q'), ai('a'), ai('b')])
  assert.equal(turns.length, 2)
  assert.equal(turns[0].userIndex, -1)
  assert.equal(turns[0].user, null)
  assert.equal(turns[1].assistant.length, 2)
})

test('groupTurns: identical questions get #0/#1; keys survive removing an unrelated turn', () => {
  const a = groupTurns([user('same'), ai('1'), user('other'), ai('2'), user('same'), ai('3')])
  assert.ok(a[0].key.endsWith('#0'))
  assert.ok(a[2].key.endsWith('#1'))
  assert.equal(a[0].key.split('#')[0], a[2].key.split('#')[0])
  const b = groupTurns([user('same'), ai('1'), user('same'), ai('3')])
  assert.equal(b[1].key, a[2].key)
  const c = groupTurns([user('x'), ai('?'), user('other'), ai('2')])
  assert.equal(c[1].key, a[1].key)
})

test('groupTurns: attachment turns key on names regardless of inlined text', () => {
  const att = [{ name: 'data.csv', mime: 'text/csv' }]
  const live = groupTurns([user('explain', { attachments: att })])
  const reloaded = groupTurns([user('explain\n[S3 hint...]\ncol1,col2\n1,2', { attachments: att })])
  assert.equal(live[0].key, reloaded[0].key)
  assert.equal(turnFingerprint(user('', { attachments: att })), 'att:data.csv')
})

test('hashString is stable and short', () => {
  assert.equal(hashString('abc'), hashString('abc'))
  assert.notEqual(hashString('abc'), hashString('abd'))
  assert.match(hashString(''), /^[0-9a-z]+$/)
})

// ── turnBlocks ───────────────────────────────────────────────────────────────

test('turnBlocks: per-bubble extraction, occurrence numbering across bubbles', () => {
  const turn = groupTurns([
    user('Q'),
    ai(`${F}python\nx=1\n${F}\n${F}python\nunclosed = 1`),
    ai(`${F}python\nx=1\n${F}\n${F}sql\nselect 1\n${F}`),
  ])[0]
  const blocks = turnBlocks(turn)
  assert.deepEqual(blocks.map(b => b.code), ['x=1', 'unclosed = 1', 'x=1', 'select 1'])
  assert.equal(blocks[0].key, blockKey('code', 'x=1', 0))
  assert.equal(blocks[2].key, blockKey('code', 'x=1', 1))
  assert.deepEqual(blocks.map(b => b.bubble), [0, 0, 1, 1])
})

test('turnBlocks: notes are not part of blocks; code keys unchanged by notes', () => {
  const withNote = groupTurns([user('Q'), ai(`_(note)_\n\n${F}python\nx=1\n${F}`)])[0]
  const without = groupTurns([user('Q'), ai(`${F}python\nx=1\n${F}`)])[0]
  assert.deepEqual(turnBlocks(withNote).map(b => b.key), turnBlocks(without).map(b => b.key))
})

// ── splitStatusNotes / turnSteps ─────────────────────────────────────────────

test('splitStatusNotes: leading notes, mid-content note after resume, glued tail note', () => {
  const md = `_(Switched to Haiku 5.5: GLM 5.3 is cooling down)_\n\n# Title\nText\n\n_(Approved: running code)_\n\nMore text_(resumed)_\nEnd`
  const { notes, body } = splitStatusNotes(md)
  assert.deepEqual(notes, ['Switched to Haiku 5.5: GLM 5.3 is cooling down', 'Approved: running code', 'resumed'])
  assert.equal(body, '# Title\nText\n\nMore text\nEnd')
})

test('splitStatusNotes: both decline-notice forms', () => {
  const one = 'Done.\n\nDeclined the pending execute_code request because a new message was sent; it was not run.'
  const many = 'Declined 2 pending requests (execute_code, install_package) because a new message was sent; they were not run.\n\nNext answer'
  assert.deepEqual(splitStatusNotes(one).notes.length, 1)
  assert.equal(splitStatusNotes(one).body, 'Done.\n')
  assert.equal(splitStatusNotes(many).notes.length, 1)
  assert.equal(splitStatusNotes(many).body, 'Next answer')
})

test('splitStatusNotes: note-like lines inside fences are kept', () => {
  const md = [
    `${F}python`, '_(not a note)_', F,
    `1. item`, `   ${F}python title="a"`, '   _(nested)_', `   ${F}`,
    '~~~sql', '_(tilde)_', '~~~',
  ].join('\n')
  const { notes, body } = splitStatusNotes(md)
  assert.deepEqual(notes, [])
  assert.equal(body, md)
  assert.deepEqual(extractCodeBlocks(body), extractCodeBlocks(md))
})

test('splitStatusNotes: no notes → identical body', () => {
  const md = `# T\n${F}python\nx=1\n${F}`
  assert.equal(splitStatusNotes(md).body, md)
})

test('turnSteps: notes then answered approvals; a pending approval is never folded', () => {
  const pending = { action: 'confirm_execute', code: 'x=1', interrupt_id: 'i1' }
  const { steps, pending: p } = turnSteps({
    notes: ['cooldown'],
    stored: [{ kind: 'approval', action: 'confirm_execute', decision: 'approved', detail: 'y=2', at: 't' }],
    pendingAction: pending,
  })
  assert.deepEqual(steps.map(s => s.kind), ['note', 'approval'])
  assert.equal(p, pending)
  assert.ok(!steps.includes(pending))
  assert.deepEqual(turnSteps({}).steps, [])
  assert.equal(turnSteps({}).pending, null)
})

// ── Titles ───────────────────────────────────────────────────────────────────

test('stripInlineMarkdown', () => {
  assert.equal(stripInlineMarkdown('**Funnel** _Segmentation_: `Device` × [Channel](http://x)'), 'Funnel Segmentation: Device × Channel')
  assert.equal(stripInlineMarkdown('snake_case_name and \\*literal\\*'), 'snake_case_name and *literal*')
  assert.equal(stripInlineMarkdown('<b>Bold</b>  ![img](u)'), 'Bold img')
})

test('extractTitle: prose heading with inline markdown', () => {
  assert.equal(extractTitle('Intro\n\n## **Funnel** Segmentation: `Device` × Channel\n\nmore'), 'Funnel Segmentation: Device × Channel')
})

test('extractTitle: heading inside a ```markdown fence', () => {
  assert.equal(extractTitle(`${F}markdown\n# Funnel Segmentation: Device × Channel × Cohort\nBody\n${F}`), 'Funnel Segmentation: Device × Channel × Cohort')
})

test('extractTitle: python # comment is never the title (plain, list-nested, info-string)', () => {
  assert.equal(extractTitle(`${F}python\n# load data\nx=1\n${F}`), null)
  assert.equal(extractTitle(`1. Step\n   ${F}python\n   # comment\n   ${F}\n`), null)
  assert.equal(extractTitle(`${F}python title="a"\n# comment\n${F}\n\n# Real Title`), 'Real Title')
})

test('extractTitle: notes before the heading are ignored', () => {
  assert.equal(extractTitle('_(Switched model)_\n\n# The Title\n'), 'The Title')
})

test('extractTitle: streaming — partial last line ignored, complete line accepted', () => {
  assert.equal(extractTitle('# Funnel Segm', { complete: false }), null)
  assert.equal(extractTitle('# Funnel Segm', { complete: true }), 'Funnel Segm')
  assert.equal(extractTitle('# Funnel Segmentation\n', { complete: false }), 'Funnel Segmentation')
})

test('extractTitle: streaming inside a still-open ```markdown fence', () => {
  assert.equal(extractTitle(`${F}markdown\n# Funnel Segm`, { complete: false }), null)
  assert.equal(extractTitle(`${F}markdown\n# Funnel Segmentation\n`, { complete: false }), 'Funnel Segmentation')
})

test('extractTitle: none', () => {
  assert.equal(extractTitle('just prose'), null)
  assert.equal(extractTitle(''), null)
})

test('parseIntelPrompt', () => {
  const p = parseIntelPrompt('Context (from Workbook Intel):\nRevenue dropped 12%\n\nline 2\n\nTask: Investigate the drop\nby region')
  assert.deepEqual(p, { context: 'Revenue dropped 12%\n\nline 2', task: 'Investigate the drop\nby region' })
  assert.equal(parseIntelPrompt('What is x?'), null)
})

test('questionLabel / turnTitle fallback order', () => {
  const intel = 'Context (from Workbook Intel):\nctx\n\nTask: Investigate churn\nmore'
  assert.equal(questionLabel({ cardTitle: 'Churn spike', userText: intel }), 'Churn spike')
  assert.equal(questionLabel({ userText: intel }), 'Investigate churn')
  assert.equal(questionLabel({ userText: '\n  Plot revenue\nby month' }), 'Plot revenue')
  assert.equal(questionLabel({ userText: '', attachments: [{ name: 'a.csv' }] }), 'Attachment: a.csv')
  assert.equal(questionLabel({ hasUser: false }), 'Conversation')
  // heading → card title → question
  assert.equal(turnTitle({ contents: ['# Heading\n'], cardTitle: 'Card', userText: 'q' }), 'Heading')
  assert.equal(turnTitle({ contents: ['no heading'], cardTitle: 'Card', userText: intel }), 'Card')
  assert.equal(turnTitle({ contents: ['no heading'], userText: 'q line' }), 'q line')
  // streaming: fallback until the heading line is complete, then it updates
  assert.equal(turnTitle({ contents: ['# Hea'], userText: 'q', complete: false }), 'q')
  assert.equal(turnTitle({ contents: ['# Heading\nbody'], userText: 'q', complete: false }), 'Heading')
  // the first bubble with a heading wins
  assert.equal(turnTitle({ contents: ['none', '# Second\n'], userText: 'q' }), 'Second')
})

// ── insertStatus ─────────────────────────────────────────────────────────────

test('insertStatus: counts, deleted cell = not inserted, pending order', () => {
  const blocks = ['a', 'b', 'c', 'd'].map((c, i) => ({ code: c, type: 'code', key: `k${i}` }))
  const st = insertStatus(blocks, { k0: 'cell-1', k2: 'cell-gone' }, new Set(['cell-1', 'other']))
  assert.equal(st.total, 4)
  assert.equal(st.inserted, 1)
  assert.deepEqual(st.pending.map(b => b.key), ['k1', 'k2', 'k3'])
  assert.equal(st.cellIdFor('k0'), 'cell-1')
  assert.equal(st.cellIdFor('k2'), null)
  const none = insertStatus([], undefined, undefined)
  assert.equal(none.total, 0)
  assert.equal(none.pending.length, 0)
})

// ── Collapse / store ─────────────────────────────────────────────────────────

test('effectiveCollapsed: streaming never collapsed; default latest expanded, older collapsed', () => {
  assert.equal(effectiveCollapsed({ isLatest: true, isStreaming: true, stored: { collapsed: true } }), false)
  assert.equal(effectiveCollapsed({ isLatest: true }), false)
  assert.equal(effectiveCollapsed({ isLatest: false }), true)
  assert.equal(effectiveCollapsed({ isLatest: false, stored: { collapsed: false } }), false)
  assert.equal(effectiveCollapsed({ isLatest: true, stored: { collapsed: true } }), true)
})

test('collapseAll: a manual expand is kept until the next send collapses everything', () => {
  let s = emptyStore('s1')
  s = collapseAll(s, ['a', 'b'], 1)
  s = setTurnEntry(s, 'a', { collapsed: false })           // manual expand of an older turn
  assert.equal(effectiveCollapsed({ isLatest: false, stored: s.turns.a }), false)
  assert.equal(effectiveCollapsed({ isLatest: false, stored: s.turns.b }), true)
  s = collapseAll(s, ['a', 'b', 'c'], 2)                   // next send
  assert.equal(s.turns.a.collapsed, true)
  assert.equal(s.turns.c.collapsed, true)
  assert.equal(collapseAll(s, []), s)
})

test('setTurnEntry: merge, function patch, replace drops stale state', () => {
  let s = emptyStore()
  s = setTurnEntry(s, 'k', { collapsed: true, inserted: { b1: 'c1' } }, { now: 5 })
  s = setTurnEntry(s, 'k', prev => ({ inserted: { ...prev.inserted, b2: 'c2' } }))
  assert.deepEqual(s.turns.k.inserted, { b1: 'c1', b2: 'c2' })
  assert.equal(s.turns.k.collapsed, true)
  assert.equal(s.turns.k.seen, 5)
  s = setTurnEntry(s, 'k', { askedAt: 'now' }, { replace: true, now: 9 })
  assert.deepEqual(s.turns.k, { askedAt: 'now', seen: 9 })
})

test('pruneStore: TTL by age, refresh seen at most hourly, same ref when unchanged', () => {
  const now = 10 * PRUNE_TTL_MS
  const s = { v: 1, sessionId: 's', turns: {
    live: { seen: now - 10 },
    stale: { seen: now - PRUNE_TTL_MS - 1 },
    recent: { seen: now - 1000 },
    legacy: {},
    oldLive: { seen: now - SEEN_REFRESH_MS - 1 },
  } }
  const p = pruneStore(s, new Set(['live', 'oldLive']), now)
  assert.deepEqual(Object.keys(p.turns).sort(), ['legacy', 'live', 'oldLive', 'recent'])
  assert.equal(p.turns.oldLive.seen, now)
  assert.equal(p.turns.legacy.seen, now)
  const q = pruneStore(p, new Set(['live', 'oldLive']), now)
  assert.equal(q, p)
})

test('reconcileSession: adopt, keep, reset', () => {
  const s = { v: 1, sessionId: null, turns: { k: { collapsed: false } } }
  const adopted = reconcileSession(s, 's1')
  assert.equal(adopted.sessionId, 's1')
  assert.deepEqual(adopted.turns, s.turns)
  assert.equal(reconcileSession(adopted, 's1'), adopted)
  assert.equal(reconcileSession(adopted, null), adopted)
  const reset = reconcileSession(adopted, 's2')
  assert.equal(reset.sessionId, 's2')
  assert.deepEqual(reset.turns, {})
})

test('syncTurnState: a notebook switch reads the new notebook store in the same step', () => {
  const stores = {
    A: { v: 1, sessionId: 'sA', turns: { a: { collapsed: false } } },
    B: { v: 1, sessionId: 'sB', turns: { b: { inserted: { x: 'c1' } } } },
  }
  const reads = []
  const read = (nb) => { reads.push(nb); return stores[nb] || emptyStore() }
  let st = syncTurnState(null, 'A', 'sA', read)
  assert.equal(st.nb, 'A')
  assert.equal(st.store, stores.A)
  assert.equal(st.dirty, false)
  assert.equal(syncTurnState(st, 'A', 'sA', read), st)        // stable: no re-read
  // Switch to B: B's store is read for B in the same step (A's store is never
  // reconciled against B's session, which would reset it).
  st = syncTurnState(st, 'B', 'sB', read)
  assert.equal(st.nb, 'B')
  assert.deepEqual(st.store.turns, stores.B.turns)
  assert.deepEqual(reads, ['A', 'B'])
  // A session change on the same notebook resets (dirty, so it's persisted).
  const reset = syncTurnState(st, 'B', 'sNew', read)
  assert.deepEqual(reset.store.turns, {})
  assert.equal(reset.dirty, true)
  // First binding adopts.
  const fresh = syncTurnState(null, 'C', 'sC', () => ({ v: 1, sessionId: null, turns: { k: {} } }))
  assert.equal(fresh.store.sessionId, 'sC')
  assert.ok(fresh.store.turns.k)
  assert.equal(fresh.dirty, true)
})

test('parseStore: corrupt, wrong version, valid', () => {
  assert.deepEqual(parseStore('{not json'), emptyStore())
  assert.deepEqual(parseStore(JSON.stringify({ v: 99, turns: {} })), emptyStore())
  assert.deepEqual(parseStore(null), emptyStore())
  const ok = parseStore(JSON.stringify({ v: 1, sessionId: 's', turns: { a: { collapsed: true } } }))
  assert.equal(ok.sessionId, 's')
  assert.equal(ok.turns.a.collapsed, true)
})

// ── Question de-dup (collapsed subtitle) ──────────────────────────────────────

test('normalizeForCompare: trims, strips inline markdown + heading hashes, folds case/space', () => {
  assert.equal(normalizeForCompare('  ## **Revenue**  by   `Month` '), 'revenue by month')
  assert.equal(normalizeForCompare('Plot\n revenue'), 'plot revenue')
  assert.equal(normalizeForCompare(null), '')
})
test('showQuestionSubtitle: card title equal to the title shows once', () => {
  const cardTitle = 'Churn spike in the mobile cohort'
  // No reply heading yet: the title falls back to the card title.
  const title = turnTitle({ contents: ['No heading here.'], cardTitle, userText: 'Context (from Workbook Intel):\nx\n\nTask: y' })
  assert.equal(title, cardTitle)
  assert.equal(showQuestionSubtitle({ title, question: cardTitle }), false)
  // Same text, different case/spacing.
  assert.equal(showQuestionSubtitle({ title: 'churn spike in the  MOBILE cohort ', question: cardTitle }), false)
})
test('showQuestionSubtitle: typed first line equal to the title shows once', () => {
  const userText = 'Plot revenue by month\nuse the events table'
  const title = turnTitle({ contents: [''], userText })
  const question = questionLabel({ userText })
  assert.equal(title, 'Plot revenue by month')
  assert.equal(question, 'Plot revenue by month')
  assert.equal(showQuestionSubtitle({ title, question }), false)
})
test('showQuestionSubtitle: markdown-decorated heading equal to the question shows once', () => {
  const title = turnTitle({ contents: ['## **Plot _revenue_ by `month`**\n\nbody'], userText: 'plot revenue by month' })
  assert.equal(title, 'Plot revenue by month')
  assert.equal(showQuestionSubtitle({ title, question: 'plot revenue by month' }), false)
  // A raw decorated string on either side compares equal too.
  assert.equal(showQuestionSubtitle({ title: '# Plot **revenue** by month', question: '*Plot* revenue by month' }), false)
})
test('showQuestionSubtitle: different texts show both; empty question never shows', () => {
  assert.equal(showQuestionSubtitle({ title: 'Funnel Segmentation: Device × Channel × Cohort', question: 'Segment the funnel by device, channel and cohort' }), true)
  assert.equal(showQuestionSubtitle({ title: 'Mobile churn by channel', question: 'Churn spike in the mobile cohort' }), true)
  assert.equal(showQuestionSubtitle({ title: 'Anything', question: '' }), false)
  assert.equal(showQuestionSubtitle({ title: '', question: '  ' }), false)
})

// ── Rail open state ───────────────────────────────────────────────────────────

test('railOpenReducer: hover and keyboard reasons, dismissals clear both', () => {
  let s = railOpenReducer(RAIL_CLOSED, 'pointer-enter')
  assert.equal(railIsOpen(s), true)
  assert.equal(railIsOpen(railOpenReducer(s, 'pointer-left')), false)
  // Keyboard focus inside keeps it open after the pointer leaves.
  s = railOpenReducer(railOpenReducer(s, 'focus-visible'), 'pointer-left')
  assert.equal(railIsOpen(s), true)
  assert.equal(railIsOpen(railOpenReducer(s, 'focus-out')), false)
  for (const ev of ['escape', 'select', 'outside', 'scroll', 'resize']) {
    assert.deepEqual(railOpenReducer({ hover: true, keyboard: true }, ev), RAIL_CLOSED, ev)
  }
  // A navigation key re-opens after Escape.
  assert.equal(railIsOpen(railOpenReducer(railOpenReducer(s, 'escape'), 'nav-key')), true)
})
test('railOpenReducer: same reference when nothing changes, unknown events ignored', () => {
  assert.equal(railOpenReducer(RAIL_CLOSED, 'pointer-left'), RAIL_CLOSED)
  assert.equal(railOpenReducer(RAIL_CLOSED, 'outside'), RAIL_CLOSED)
  const open = { hover: true, keyboard: false }
  assert.equal(railOpenReducer(open, 'pointer-enter'), open)
  assert.equal(railOpenReducer(open, 'bogus'), open)
  assert.equal(railOpenReducer(undefined, 'bogus'), RAIL_CLOSED)
})
// ── AI context window ────────────────────────────────────────────────────────
const convo = (k) => Array.from({ length: k }, (_, i) => [user(`Question ${i + 1}`), ai(`Answer ${i + 1}`)]).flat()
const outOrdinals = (turns, n) => {
  const out = outOfContextKeys(turns, n)
  return turns.filter(t => out.has(t.key)).map(t => t.ordinal)
}
test('contextTurnCount: window of messages → question/answer turns, unusable → 0', () => {
  assert.equal(contextTurnCount(10), 5)
  assert.equal(contextTurnCount(11), 5)
  assert.equal(contextTurnCount(2), 1)
  for (const bad of [undefined, null, 0, 1, -4, NaN, Infinity, '10', {}]) assert.equal(contextTurnCount(bad), 0, String(bad))
})
test('outOfContextKeys: n=5 with 3 and 5 turns → all in context', () => {
  assert.deepEqual(outOrdinals(groupTurns(convo(3)), 5), [])
  assert.deepEqual(outOrdinals(groupTurns(convo(5)), 5), [])
})
test('outOfContextKeys: n=5 with 8 turns → the first 3 are out', () => {
  const turns = groupTurns(convo(8))
  assert.equal(turns.length, 8)
  assert.deepEqual(outOrdinals(turns, 5), [0, 1, 2])
})
test('outOfContextKeys: n missing / 0 / invalid → nothing out', () => {
  const turns = groupTurns(convo(8))
  for (const n of [undefined, null, 0, -1, 2.5, '5']) assert.equal(outOfContextKeys(turns, n).size, 0, String(n))
  assert.equal(outOfContextKeys(undefined, 5).size, 0)
})
test('outOfContextKeys: resumes / approval / auto-decline continuations are part of their turn', () => {
  const msgs = [
    user('Q1'), ai('A1'),
    user('Q2'), ai('Part one, waiting for approval'), ai('_(Approved: Run code)_\n\nPart two after resume'),
    user('Q3'), ai('A3 start'),
    ai('Declined the pending execute_code request because a new message was sent; it was not run.'),
    user('Q4'), ai('A4'), ai('A4 continued'),
    user('Q5'), ai('A5'),
    user('Q6'), ai('A6'), ai('A6 resumed'), ai('A6 resumed again'),
  ]
  const turns = groupTurns(msgs)
  assert.equal(turns.length, 6)
  // 6 question turns, n=5 → only Q1 is out; the extra assistant bubbles add nothing.
  assert.deepEqual(outOrdinals(turns, 5), [0])
  assert.deepEqual(outOrdinals(turns, 2), [0, 1, 2, 3])
})
test('outOfContextKeys: a leading assistant-only turn follows the same rule; duplicate questions keyed apart', () => {
  const lead = groupTurns([ai('Welcome back'), ...convo(2)])
  assert.equal(lead[0].user, null)
  assert.deepEqual(outOrdinals(lead, 5), [])
  assert.deepEqual(outOrdinals(lead, 2), [0])
  const dup = groupTurns([user('Same'), ai('a'), user('Same'), ai('b'), user('Other'), ai('c')])
  assert.deepEqual(outOrdinals(dup, 2), [0])
})
