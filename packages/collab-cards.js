// Room command cards (C6/C7): /handoff, /decide, /summarize, /issue and /assign become structured events on the
// server; this renders them. Every value is escaped; the only actions are the ones the server says this viewer
// may take (`can_answer`, `can_confirm`), and the server re-checks them from the credential anyway.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const COMMAND_EVENTS = new Set(['handoff', 'handoff_updated', 'decision', 'summary_request', 'issue_draft', 'assignment', 'github_written']);
export const isCommandEvent = (type) => COMMAND_EVENTS.has(type);

const TONE = { pending: 'var(--status-attention)', accepted: 'var(--status-ok)', declined: 'var(--status-failed)', expired: 'var(--text-muted)',
  draft: 'var(--status-attention)', created: 'var(--status-ok)', assigned: 'var(--status-ok)' };
const party = (p) => (p && p.name ? `@${esc(p.name)}` : 'someone');
const status = (s) => `<span class="cx-cmdcard__status" data-status style="--tc:${TONE[s] || 'var(--text-muted)'}">${esc(s || '')}</span>`;
const issueRef = (o, r, n) => `${esc(o)}/${esc(r)}#${Number(n) || ''}`;
// what the confirm dialog shows of the issue text: the first ~400 characters, ZWJs (anti-mention) removed
const summarize = (body) => { const t = String(body || '').replaceAll('\u200d', ''); return t.length > 400 ? `${t.slice(0, 400)}…` : t; };
const safeUrl = (u) => (typeof u === 'string' && /^https:\/\/github\.com\/[A-Za-z0-9._/-]+$/.test(u) ? u : '');
const head = (title, e, extra = '') => `<div class="cx-taskcard__head"><b>${title}</b>${extra}<time>${esc(new Date(e.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}</time></div>`;

// {className, html, attrs} for one command event. ctx.who(sender) names the poster (defaults to "someone").
export function commandCard(e, ctx = {}) {
  const who = ctx.who ? ctx.who(e.sender) : 'someone';
  const card = (kind, html, attrs = {}) => ({ className: `cx-taskcard cx-cmdcard cx-cmdcard--${kind}`, html, attrs });
  if (e.event_type === 'handoff') {
    const h = e.handoff || {};
    const buttons = h.can_answer ? `<div class="cx-row cx-cmdcard__actions"><button type="button" class="hd-btn hd-btn--sm" data-handoff-answer="accept" data-handoff-id="${esc(h.handoff_id)}">Accept</button><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-handoff-answer="decline" data-handoff-id="${esc(h.handoff_id)}">Decline</button></div>` : '';
    return card('handoff', `${head('Handoff', e, status(h.status))}<div>${party(h.from)} → ${party(h.to)}</div>${h.note ? `<div class="cx-cmdcard__note">${esc(h.note)}</div>` : ''}${buttons}`, { 'data-handoff': h.handoff_id || '' });
  }
  if (e.event_type === 'handoff_updated') {
    const h = e.handoff || {};
    return card('handoff', `${head('Handoff', e, status(h.status))}<div>${party(h.to)} ${h.status === 'accepted' ? 'accepted' : 'declined'} the handoff from ${party(h.from)}</div>`);
  }
  if (e.event_type === 'decision') return card('decision', `${head('Decision', e)}<div class="cx-cmdcard__note">${esc(e.text || '')}</div>`);
  if (e.event_type === 'summary_request') {
    return card('summary', `${head('Summary requested', e)}<div>${esc(who)} asked for a summary${e.text ? `: ${esc(e.text)}` : ''}. An online agent can answer it.</div>`);
  }
  if (e.event_type === 'issue_draft' || e.event_type === 'assignment') {
    const d = e.draft || {};
    const issue = d.kind === 'issue';
    const confirm = d.can_confirm ? `<div class="cx-row cx-cmdcard__actions"><button type="button" class="hd-btn hd-btn--sm" data-draft-confirm="${esc(d.draft_id)}" data-draft-kind="${issue ? 'issue' : 'assignment'}" data-draft-where="${issueRef(d.owner, d.repo, issue ? '' : d.number)}"${issue ? ` data-draft-title="${esc(d.title || '')}" data-draft-summary="${esc(summarize(d.body))}"` : ''}>${issue ? 'Create issue on GitHub…' : 'Assign on GitHub…'}</button></div>` : '';
    const done = d.number && (d.status === 'created' || d.status === 'assigned') ? ` · ${issueRef(d.owner, d.repo, d.number)}` : '';
    const body = issue
      ? `<div><b>${esc(d.title || '')}</b> <span class="cx-sub1">${esc(d.owner)}/${esc(d.repo)}${done}</span></div>${typeof d.body === 'string' ? `<details><summary>Issue text</summary><pre class="cx-cmdcard__body">${esc(d.body)}</pre></details>` : '<div class="cx-sub1">It quotes messages from before you joined.</div>'}`
      : `<div>Assign ${issueRef(d.owner, d.repo, d.number)} to ${party(d.person)}${d.login ? ` <span class="cx-sub1">(${esc(d.login)} on GitHub)</span>` : ''}</div>`;
    return card(issue ? 'issue' : 'assign', `${head(issue ? 'Issue draft' : 'Assignment draft', e, status(d.status))}${body}${confirm}`, { 'data-draft': d.draft_id || '' });
  }
  const g = e.github || {};
  const url = safeUrl(g.url);
  const ref = issueRef(g.owner, g.repo, g.number);
  return card('github', `${head(g.kind === 'issue' ? 'Issue created' : 'Issue assigned', e)}<div>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${ref}</a>` : ref}${g.kind === 'issue' ? '' : `${g.login ? ` → ${esc(g.login)}` : ''}`}</div>`);
}

// C7: an agent's presence for the roster ("working…" while it holds an accepted task) and a room's unread
// mention badge (your own count only; the server sends none to a guest).
export function presenceLabel(a) {
  if (a?.presence === 'working') return 'working…';
  if (a?.presence === 'online' || (!a?.presence && a?.online)) return 'online';
  return '';
}
export function unreadBadge(room) {
  const n = Number(room?.unread_mentions) || 0;
  return n > 0 ? `<span class="cx-unread" title="${n >= 99 ? '99 or more' : n} unread mention${n === 1 ? '' : 's'}">${n >= 99 ? '99+' : n}</span>` : '';
}
