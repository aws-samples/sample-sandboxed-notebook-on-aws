/**
 * chatExport — the AI Assistant chat as a static HTML or Markdown file.
 *
 * Mirrors the redesigned chat panel (ChatTurn.jsx): one section per TURN with a
 * "Turn N: title" heading, the question bubble (attachments, the Intel source tag
 * on card turns, the full question), the cells/time meta line, the folded tool
 * steps (shown expanded), then the assistant bubbles. Everything is expanded and
 * static: no buttons or toggles, no turn rail, no context-window muting (that is
 * presentation only). Code renders as static cards (Python / SQL / Markdown label,
 * type-coloured edge, the app's syntax colours; Markdown cards show their source).
 *
 * Pure and Node-loadable; the panel passes its turn store and the mermaid renderer.
 */
import {
  groupTurns, turnTitle, questionLabel, parseIntelPrompt, splitStatusNotes, turnSteps,
  turnBlocks, normalizeFences, insertableType, langKey,
} from './chatTurns.js'
import {
  esc, exportCss, readExportTheme, defaultExportTheme, codeCard, renderExportMarkdown,
  stripControls, inlineMermaid, createExportMarked,
} from './exportRender.js'
import { fenced } from './notebookExport.js'
import { sanitizeMarkdown } from './sanitize.js'

const APPROVAL_LABEL = { approved: 'Approved', rejected: 'Rejected', declined: 'Declined (new message)' }
const _sanitizeDefault = (h) => { try { return sanitizeMarkdown(h) } catch { return h } }
const _defaultTime = (ts) => {
  if (!ts) return ''
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}
const _themeOf = (opt) => (opt && opt.vars ? opt : opt && opt.name ? defaultExportTheme(opt.name)
  : typeof opt === 'string' ? defaultExportTheme(opt) : readExportTheme())

/**
 * One view per turn, derived exactly like the panel's turnViews (complete titles).
 * turnMeta: { [turnKey]: { cardTitle?, steps?, askedAt? } } (the panel's turn store).
 */
export function chatTurnViews(messages, turnMeta = {}) {
  return groupTurns(messages || []).map((t, i) => {
    const stored = (turnMeta && turnMeta[t.key]) || {}
    const cardTitle = stored.cardTitle || t.user?.intelCard?.title || null
    const userText = t.user?.content || ''
    const attachments = Array.isArray(t.user?.attachments) ? t.user.attachments : []
    const hasUser = !!t.user
    const notes = t.assistant.flatMap(a => splitStatusNotes(a.msg.content).notes)
    return {
      n: i + 1,
      turn: t,
      title: turnTitle({ contents: t.assistant.map(a => a.msg.content || ''), cardTitle, userText, attachments, hasUser, complete: true }),
      label: questionLabel({ cardTitle, userText, attachments, hasUser }),
      isCard: hasUser && (!!cardTitle || !!parseIntelPrompt(userText)),
      cells: turnBlocks(t).length,
      steps: turnSteps({ notes, stored: stored.steps }).steps,
      askedAt: stored.askedAt || t.user?.timestamp || null,
      attachments,
    }
  })
}

const _stepText = (s) => (s.kind === 'note' ? s.text
  : `${APPROVAL_LABEL[s.decision] || s.decision}: ${s.action === 'confirm_install' ? `Install ${s.detail}` : 'Run code'}`)

const CHAT_CSS = `
.x-turn{margin:28px 0 0;padding-top:4px;border-top:1px solid var(--border-subtle)}
.x-turn>h2{margin:16px 0 10px}
.x-q{margin:0 0 6px 15%;padding:10px 14px;border-radius:12px;background:color-mix(in srgb,var(--accent-primary) 10%,var(--surface-1));border:1px solid color-mix(in srgb,var(--accent-primary) 22%,var(--border-default))}
.x-who{font-size:12px;font-weight:600;color:var(--text-secondary)}
.x-time{font-size:11px;color:var(--text-tertiary);margin-left:6px}
.x-q-text{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:4px}
.x-q-label{font-weight:600;margin-top:4px}
.x-q-full{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:6px;padding-top:6px;border-top:1px solid var(--border-subtle);color:var(--text-secondary);font-size:13px}
.x-src{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent-primary);margin-right:6px}
.x-atts{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.x-att{font-size:12px;padding:1px 8px;border-radius:6px;border:1px solid var(--border-default);color:var(--text-secondary);background:var(--surface-1)}
.x-turn-meta{margin:0 0 10px 15%;font-size:12px;color:var(--text-tertiary);text-align:right}
.x-steps{margin:8px 0;padding:8px 12px;border:1px solid var(--border-subtle);border-radius:8px;background:var(--surface-0);font-size:12px;color:var(--text-secondary)}
.x-steps-title{font-weight:600;color:var(--text-tertiary)}
.x-steps ol{margin:6px 0 0;padding-left:20px}
.x-a{margin:8px 0;padding:10px 14px;border-radius:12px;background:var(--surface-1);border:1px solid var(--border-default)}
.x-a.x-a-error{border-color:var(--accent-danger)}
.x-a-body>:first-child{margin-top:4px}.x-a-body>:last-child{margin-bottom:0}
.x-a-body{overflow-wrap:anywhere}
.x-a-body table{display:block;width:fit-content;max-width:100%;overflow-x:auto}
footer{text-align:center;padding:24px 0 0;color:var(--text-tertiary);font-size:11px;border-top:1px solid var(--border-subtle);margin-top:32px}
`

const _fileBase = (name) => String(name || 'Notebook').replace(/\s+/g, '-')

/**
 * Static, self-contained HTML of the chat. opts: notebookName, messages, turnMeta,
 * theme (default: the user's current theme), sanitize, renderMermaid, now,
 * formatTime (ts → string).
 */
export async function buildChatHTML({
  notebookName, messages, turnMeta = {}, theme: themeOpt, sanitize = _sanitizeDefault,
  renderMermaid = null, now = new Date(), formatTime = _defaultTime,
} = {}) {
  const theme = _themeOf(themeOpt)
  const nb = notebookName || 'Notebook'
  const views = chatTurnViews(messages, turnMeta)
  let body = `<h1>${esc(nb)} — AI Chat</h1>`
  body += `<p class="x-meta">Generated: ${esc(formatTime(now))} · ${views.length} turn${views.length === 1 ? '' : 's'}</p>`
  for (const v of views) {
    const { turn: t } = v
    body += `<section class="x-turn"><h2>Turn ${v.n}: ${esc(v.title)}</h2>`
    if (t.user) {
      const time = formatTime(t.user.timestamp || v.askedAt)
      body += `<div class="x-q"><span class="x-who">You</span>${time ? `<span class="x-time">${esc(time)}</span>` : ''}`
      if (v.attachments.length) {
        body += `<div class="x-atts">${v.attachments.map(a => `<span class="x-att">${esc(a?.name || 'attachment')}</span>`).join('')}</div>`
      }
      const text = t.user.content || ''
      if (v.isCard) {
        body += `<div class="x-q-label"><span class="x-src">Intel</span>${esc(v.label)}</div>`
        if (text && text.trim() !== v.label) body += `<div class="x-q-full">${esc(text)}</div>`
      } else if (text) {
        body += `<div class="x-q-text">${esc(text)}</div>`
      }
      body += `</div>`
    }
    const meta = [v.cells > 0 ? `${v.cells} cell${v.cells === 1 ? '' : 's'}` : '', formatTime(v.askedAt)].filter(Boolean)
    if (meta.length) body += `<p class="x-turn-meta">${esc(meta.join(' · '))}</p>`
    if (v.steps.length) {
      body += `<div class="x-steps"><div class="x-steps-title">${v.steps.length} step${v.steps.length === 1 ? '' : 's'}</div><ol>`
      for (const s of v.steps) {
        body += `<li>${esc(_stepText(s))}`
        if (s.kind !== 'note' && s.action !== 'confirm_install' && s.detail) body += codeCard(String(s.detail), 'code')
        body += `</li>`
      }
      body += `</ol></div>`
    }
    for (const { msg } of t.assistant) {
      const time = formatTime(msg.timestamp)
      const html = await inlineMermaid(renderExportMarkdown(splitStatusNotes(msg.content).body, { sanitize, chat: true }), renderMermaid)
      body += `<div class="x-a${msg.isError ? ' x-a-error' : ''}"><span class="x-who">AI Assistant</span>`
        + `${time ? `<span class="x-time">${esc(time)}</span>` : ''}<div class="x-a-body">${html}</div></div>`
    }
    body += `</section>`
  }
  body += `<footer><strong>Sandboxed Notebook on AWS</strong><br>Developed by the AWS Startup SA Team<br>&copy; ${now.getFullYear()} Amazon Web Services, Inc.</footer>`
  const content = `<!doctype html><html lang="en" data-theme="${theme.name}"><head><meta charset="UTF-8">`
    + `<meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(nb)} — AI Chat</title>`
    + `<style>${exportCss(theme)}${CHAT_CSS}</style></head><body><div class="x-doc">${stripControls(body)}</div></body></html>`
  return { content, mime: 'text/html', filename: `${_fileBase(nb)}-ai-chat.html` }
}

const _lexer = createExportMarked()
const FENCE_TAG = { code: 'python', sql: 'sql', markdown: 'markdown' }

/**
 * Re-emit an assistant body's top-level fenced blocks with the right language tag
 * (bare / py → python, sql, markdown; others keep their own). Prose is verbatim.
 */
export function refenceMarkdown(body) {
  const src = normalizeFences(String(body || ''))
  if (src.indexOf('```') === -1 && src.indexOf('~~~') === -1) return src
  let tokens
  try { tokens = _lexer.lexer(src) } catch { return src }
  let out = ''
  for (const tk of tokens) {
    if (tk.type === 'code' && /^\s*(`{3,}|~{3,})/.test(tk.raw || '')) {
      const ins = insertableType(tk.lang)
      const tag = ins ? FENCE_TAG[ins] : langKey(tk.lang)
      const trail = (tk.raw.match(/\n*$/) || [''])[0]
      out += fenced(tk.text || '', tag) + trail
    } else {
      out += tk.raw || ''
    }
  }
  return out
}

/** The chat as Markdown: same turn structure and headings as the HTML. */
export function buildChatMarkdown({
  notebookName, messages, turnMeta = {}, now = new Date(), formatTime = _defaultTime,
} = {}) {
  const nb = notebookName || 'Notebook'
  const views = chatTurnViews(messages, turnMeta)
  let md = `# ${nb} — AI Chat History\n\n*Generated: ${formatTime(now)}*\n\n---\n\n`
  for (const v of views) {
    const { turn: t } = v
    md += `## Turn ${v.n}: ${v.title}\n\n`
    if (t.user) {
      const time = formatTime(t.user.timestamp || v.askedAt)
      md += `**You**${time ? ` *(${time})*` : ''}${v.isCard ? ' · Intel' : ''}\n\n`
      if (v.attachments.length) {
        md += `Attachments:\n\n${v.attachments.map(a => `- ${a?.name || 'attachment'}`).join('\n')}\n\n`
      }
      const text = t.user.content || ''
      if (text) md += `${text.split('\n').map(l => (l ? `> ${l}` : '>')).join('\n')}\n\n`
    }
    const meta = [v.cells > 0 ? `${v.cells} cell${v.cells === 1 ? '' : 's'}` : '', formatTime(v.askedAt)].filter(Boolean)
    if (meta.length) md += `*${meta.join(' · ')}*\n\n`
    if (v.steps.length) {
      md += `**Steps**\n\n`
      for (const s of v.steps) {
        md += `1. ${_stepText(s)}\n`
        if (s.kind !== 'note' && s.action !== 'confirm_install' && s.detail) {
          md += `\n${fenced(String(s.detail), 'python').split('\n').map(l => `   ${l}`).join('\n')}\n`
        }
      }
      md += `\n`
    }
    for (const { msg } of t.assistant) {
      const time = formatTime(msg.timestamp)
      md += `**AI Assistant**${time ? ` *(${time})*` : ''}\n\n${refenceMarkdown(splitStatusNotes(msg.content).body).trim()}\n\n`
    }
    md += `---\n\n`
  }
  md += `*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*\n`
  return { content: md, mime: 'text/markdown', filename: `${_fileBase(nb)}-ai-chat.md` }
}
