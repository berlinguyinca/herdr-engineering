// Contextual chat, consultation and cross-workspace links for the room workbench (T14). Pure (DOM-free): the models
// are plain data and the renderers return escaped HTML, so they are node-testable.
//
// Rules (docs/contracts/lineage-events.md, "Contextual chat, consultation and cross-workspace links (T14)"):
//  * a reference chip shows ONLY what the server resolved for this reader. A hidden reference is one fixed phrase and
//    carries no id, kind or label; this module never receives one, so it cannot paint one;
//  * every id, label, question and answer is DATA that reaches the page only through esc(). A question and an answer
//    are labelled untrusted text; neither is a system message, neither runs anything;
//  * consultation is a question and ONE text answer. There is no task, no command and no file field anywhere here;
//  * nothing is encoded by colour alone: every state is a glyph plus a word.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const MAX_QUESTION = 1000;
export const MAX_ANSWER = 2000;
export const MAX_NOTE = 200;
export const STATE_LOOK = {
  open: ['◐', 'Waiting for an answer'],
  answered: ['✓', 'Answered'],
  declined: ['✕', 'Declined'],
  cancelled: ['■', 'Cancelled'],
  expired: ['■', 'Expired before it was answered'],
  void: ['■', 'Void: the asker or the subject is no longer available'],
  active: ['●', 'Linked'],
  revoked: ['■', 'Revoked'],
  lapsed: ['■', 'Lapsed: the room no longer holds a grant to both workspaces'],
};

const text = (v, fallback = '') => (typeof v === 'string' && v ? v : fallback);
const when = (t) => (typeof t === 'string' && t ? `<time datetime="${esc(t)}">${esc(t.replace('T', ' ').replace(/\.\d+/, '').replace('+00:00', ' UTC'))}</time>` : '');
export const stateLook = (s) => { const [glyph, word] = STATE_LOOK[s] || ['◇', text(s, 'unknown')]; return { glyph, word }; };

// ---- a chat message's reference ---------------------------------------------------------------------------------
export function renderContextChip(ctx) {
  if (!ctx || typeof ctx !== 'object') return '';
  if (ctx.hidden !== false) {
    return '<span class="ctx-chip ctx-chip--hidden" role="note"><span aria-hidden="true">🔒</span> Hidden reference <span class="cx-msg">(you cannot see what this points at)</span></span>';
  }
  if (ctx.kind === 'node') {
    return `<button type="button" class="ctx-chip" data-ctx-open="${esc(text(ctx.node_id))}" aria-label="Open ${esc(text(ctx.node_type, 'item'))} ${esc(text(ctx.label, ctx.node_id))} in the lineage">`
      + `<span aria-hidden="true">◆</span> ${esc(text(ctx.node_type, 'item'))}: ${esc(text(ctx.label, ctx.node_id))}</button>`;
  }
  if (ctx.kind === 'edge') {
    return `<span class="ctx-chip" role="note"><span aria-hidden="true">↔</span> link ${esc(text(ctx.from_id))} ${esc(text(ctx.edge_type))} ${esc(text(ctx.to_id))}</span>`;
  }
  if (ctx.kind === 'comment') return `<span class="ctx-chip" role="note"><span aria-hidden="true">💬</span> comment on ${esc(text(ctx.anchor && (ctx.anchor.node_id || ctx.anchor.from_id), 'an item'))}</span>`;
  if (ctx.kind === 'proposal') return `<span class="ctx-chip" role="note"><span aria-hidden="true">📄</span> proposal (${esc(text(ctx.state, 'unknown'))})</span>`;
  return '<span class="ctx-chip ctx-chip--hidden" role="note">Reference</span>';
}

// ---- consultation ------------------------------------------------------------------------------------------------
function who(p) { return esc(text(p && p.id, 'unknown')); }

function renderConsult(c, w) {
  const { glyph, word } = stateLook(c.state);
  const mine = c.asked_by && c.asked_by.id === w.me;
  const subject = c.subject && c.subject.hidden === false
    ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-ctx-open="${esc(text(c.subject.node_id))}">${esc(text(c.subject.label, c.subject.node_id))}</button>`
    : '<span class="cx-msg">a hidden item</span>';
  const answer = c.answer ? `<p class="cx-msg">Answer (untrusted text):</p><blockquote class="wb-text">${esc(c.answer)}</blockquote>` : '';
  const reply = !mine && c.state === 'open' && c.target && c.target.type === 'user' && c.target.id === w.me
    ? `<form data-ctx-form="answer" data-ctx-id="${esc(c.consult_id)}" class="cx-stack"><label class="cx-field">Your answer (plain text, once)<textarea class="cx-input" name="answer" maxlength="${MAX_ANSWER}" rows="3" required></textarea></label>`
      + `<div class="cx-row"><button type="submit" class="hd-btn hd-btn--sm">Answer</button><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-ctx-act="decline" data-ctx-id="${esc(c.consult_id)}">Decline</button></div></form>` : '';
  const cancel = c.state === 'open' && (mine || w.isOwner)
    ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-ctx-act="cancel" data-ctx-id="${esc(c.consult_id)}">Cancel</button>` : '';
  return `<li class="ctx-consult" data-ctx-consult="${esc(c.consult_id)}"><p><span aria-hidden="true">${glyph}</span> <b>${esc(word)}</b> · <span class="wb-who">${who(c.asked_by)}</span> asked <span class="wb-who">${who(c.target)}</span> about ${subject} ${when(c.asked_at)}</p>`
    + `<p class="cx-msg">Question (untrusted text from ${who(c.asked_by)}, never a system message):</p><blockquote class="wb-text">${esc(text(c.question))}</blockquote>${answer}${reply}${cancel}</li>`;
}

export function renderInbox(w) {
  const list = (w.consults || []).filter((c) => c.state === 'open' && c.target && c.target.type === 'user' && c.target.id === w.me);
  if (!w.enabled || !list.length) return '';
  return `<section class="wb-section ctx-inbox" aria-label="Questions for you"><h4>Questions for you</h4>`
    + `<p class="cx-msg">Each is one question with one plain-text answer. Answering runs nothing and creates no task.</p><ul class="cx-list">${list.map((c) => renderConsult(c, w)).join('')}</ul></section>`;
}

export function renderAskForm(w) {
  const people = (w.people || []).filter((p) => p.id !== w.me);
  if (!people.length) return '<p class="cx-msg">There is nobody in this room you can ask.</p>';
  const d = w.askDraft || {};
  const opts = people.map((p) => `<option value="${esc(p.kind)}:${esc(p.id)}"${d.target === `${p.kind}:${p.id}` ? ' selected' : ''}>${esc(text(p.label, p.id))}${p.kind === 'agent' ? ' (remote agent)' : ''}</option>`).join('');
  return `<form data-ctx-form="ask" class="cx-stack"><label class="cx-field">Ask<select class="cx-input" name="target" data-ctx-field="target">${opts}</select></label>`
    + `<label class="cx-field">Question (up to ${MAX_QUESTION} characters)<textarea class="cx-input" name="question" data-ctx-field="question" maxlength="${MAX_QUESTION}" rows="3" required>${esc(d.question || '')}</textarea></label>`
    + `<p class="cx-msg">A remote agent is never shown which item this is about; put what it needs in the question. The answer is plain text only, expires in an hour, and is never executed.</p>`
    + `<button type="submit" class="hd-btn hd-btn--sm"${w.busy ? ' disabled' : ''}>Ask</button></form>`;
}

// ---- cross-workspace links ---------------------------------------------------------------------------------------
function renderLink(l, w) {
  const { glyph, word } = stateLook(l.state);
  const end = (e) => `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-ctx-open="${esc(text(e.node_id))}">${esc(text(e.label, e.node_id))}</button> <span class="cx-msg">(${esc((e.workspaces || []).join(', ') || 'no workspace')})</span>`;
  const revoke = (l.state === 'active' || l.state === 'lapsed') && (l.created_by && l.created_by.id === w.me || w.isOwner)
    ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-ctx-act="revoke" data-ctx-id="${esc(l.link_id)}">Revoke</button>` : '';
  return `<li class="ctx-link"><p><span aria-hidden="true">${glyph}</span> <b>${esc(word)}</b> · ${end(l.from)} ↔ ${end(l.to)}</p>`
    + `<p class="cx-msg">Linked by <span class="wb-who">${who(l.created_by)}</span> ${when(l.created_at)}${l.note ? `: ${esc(l.note)}` : ''} · asserted by a person, not proven by the agents</p>${revoke}</li>`;
}

export function renderLinkForm(w) {
  const others = (w.otherNodes || []).filter((n) => n.id !== w.nodeId).slice(0, 200);
  const list = others.map((n) => `<option value="${esc(n.id)}">${esc(text(n.label, n.id))}</option>`).join('');
  return `<form data-ctx-form="link" class="cx-stack"><label class="cx-field">Link this item to the node (id, from the graph)<input class="cx-input" name="to" list="ctx-nodes" maxlength="512" required autocomplete="off"></label>`
    + `<datalist id="ctx-nodes">${list}</datalist>`
    + `<label class="cx-field">Why (optional, up to ${MAX_NOTE} characters)<input class="cx-input" name="note" maxlength="${MAX_NOTE}"></label>`
    + `<p class="cx-msg">Both items must be visible to you and belong to different workspaces this room holds a grant to. The link is shown to everyone in the room who can see both, and you or a room owner can revoke it.</p>`
    + `<button type="submit" class="hd-btn hd-btn--sm"${w.busy ? ' disabled' : ''}>Link across workspaces</button></form>`;
}

// ---- the inspector section -----------------------------------------------------------------------------------------
export function renderContextPanel(w) {
  if (!w || !w.nodeId || !w.enabled) return '';
  const n = esc(w.nodeId);
  const mine = (w.consults || []).filter((c) => c.subject && c.subject.hidden === false && c.subject.node_id === w.nodeId);
  const links = (w.links || []).filter((l) => l.from.node_id === w.nodeId || l.to.node_id === w.nodeId);
  const flash = `${w.notice ? `<p class="cx-msg" role="status">${esc(w.notice)}</p>` : ''}${w.error ? `<p class="cx-msg" role="alert">${esc(w.error)}</p>` : ''}`;
  const chat = w.chat && w.canPost ? `<button type="button" class="hd-btn hd-btn--sm" data-ctx-act="attach">Discuss this in the room chat</button>` : '';
  const ask = w.consult && w.canPost ? `<h5>Ask someone about this</h5>${renderAskForm(w)}` : '';
  const lk = w.crossLinks
    ? `<h5>Cross-workspace links</h5>${links.length ? `<ul class="cx-list">${links.map((l) => renderLink(l, w)).join('')}</ul>` : '<p class="cx-msg">No link from this item.</p>'}${w.canPost ? renderLinkForm(w) : ''}` : '';
  return `<section class="wb-section ctx" aria-label="Chat, questions and links for ${n}"><h4>Chat, questions and links</h4>${flash}${chat}${ask}`
    + `${mine.length ? `<h5>Questions about this item</h5><ul class="cx-list">${mine.map((c) => renderConsult(c, w)).join('')}</ul>` : ''}${lk}</section>`;
}

// ---- request bodies (the only place a body is built) -------------------------------------------------------------
export function contextBody(nodeId) { return { kind: 'node', node_id: String(nodeId) }; }
export function askBody(nodeId, target, question, key) {
  const i = String(target).indexOf(':');
  return { target: { kind: String(target).slice(0, i), id: String(target).slice(i + 1) }, subject: { kind: 'node', node_id: String(nodeId) },
    question: String(question), idempotency_key: key };
}
export function linkBody(fromId, toId, note, key) {
  const b = { from: { kind: 'node', node_id: String(fromId) }, to: { kind: 'node', node_id: String(toId) }, idempotency_key: key };
  if (note && String(note).trim()) b.note = String(note).trim();
  return b;
}
export const answerBody = (answer) => ({ action: 'answer', answer: String(answer) });
export const declineBody = () => ({ action: 'decline' });
