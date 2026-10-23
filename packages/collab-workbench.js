// Comments and steering controls for the lineage inspector (T12). Pure (DOM-free): the models are plain data and the
// renderers return escaped HTML, so they are node-testable.
//
// Rules (docs/contracts/lineage-events.md, "Comments and steering (T12)"):
//  * everything shown comes from the server's response. A comment's author, a request's proposer/confirmer and every
//    status word come from server-set fields; the free text of a comment or instruction is DATA that reaches the page
//    only through esc() (text and attribute contexts alike) and is labelled untrusted;
//  * discussion and steering are separate sections. Comments are never sent to an agent and say so. Steering is a
//    closed list of actions, needs a second person to confirm, and the confirm step restates exactly what will be sent;
//  * nothing is encoded by colour alone: every status is a glyph plus a word;
//  * there is no terminal, no shell field and no free-form command anywhere. The only free text is the redirect
//    instruction, which the receiving agent is told is untrusted.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const ACTION_LABEL = {
  pause: 'Pause', resume: 'Resume', stop_after_step: 'Stop after the current step', redirect: 'Redirect with an instruction',
  approve: 'Approve this gate', reject: 'Reject this gate',
};
export const ACTION_HELP = {
  pause: 'Ask the agent to pause at its next safe boundary.',
  resume: 'Ask a paused agent to continue.',
  stop_after_step: 'Ask the agent to finish its current step and then stop.',
  redirect: 'Give the agent a short instruction. It is delivered as untrusted text from you, never as a system message.',
  approve: 'Approve the gate at exactly the digest or commit shown.',
  reject: 'Reject the gate at exactly the digest or commit shown.',
};
export const STATUS_LOOK = {
  pending: ['◐', 'Waiting for a room owner to confirm'],
  confirmed: ['●', 'Confirmed, waiting for the agent to collect it'],
  delivered: ['●', 'Delivered, waiting for the agent to answer'],
  acked: ['✓', 'The agent answered'],
  rejected: ['✕', 'Declined or cancelled'],
  withdrawn: ['■', 'Withdrawn by the proposer'],
  fenced: ['■', 'Cancelled: the target changed or was superseded'],
  expired: ['■', 'Expired before it was used'],
};
export const MAX_COMMENT = 2000;
export const MAX_INSTRUCTION = 1000;

const text = (v, fallback = '') => (typeof v === 'string' && v ? v : fallback);
const short = (s, n = 12) => (typeof s === 'string' && s.length > n ? `${s.slice(0, n)}…` : text(s));
const when = (t) => (typeof t === 'string' && t ? `<time datetime="${esc(t)}">${esc(t.replace('T', ' ').replace(/\.\d+/, '').replace('+00:00', ' UTC'))}</time>` : '');

export function statusLook(status) {
  const [glyph, word] = STATUS_LOOK[status] || ['◇', text(status, 'unknown')];
  return { glyph, word };
}

// ---- discussion --------------------------------------------------------------------------------------------------
function renderComment(c, w) {
  const mine = c.author && c.author.id === w.me;
  const editing = w.editing && w.editing.id === c.comment_id;
  const who = `${esc(text(c.author && c.author.id, 'unknown'))}`;
  const meta = `<span class="wb-who">${who}</span> ${when(c.created_at)}${c.edited ? ' <span class="cx-msg">(edited)</span>' : ''}`;
  if (c.retracted) {
    return `<li class="wb-comment" data-wb-comment="${esc(c.comment_id)}"><p class="cx-msg">${meta}: ${c.moderated ? 'removed by a room owner' : 'retracted by the author'} (earlier text remains in history).</p>`
      + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="history" data-wb-id="${esc(c.comment_id)}">History</button>${renderHistory(c, w)}</li>`;
  }
  const body = editing
    ? `<form data-wb-form="edit" data-wb-id="${esc(c.comment_id)}" class="cx-stack"><label class="cx-field">Edit your comment<textarea class="cx-input" name="text" data-wb-field="edit" maxlength="${MAX_COMMENT}" rows="3" required>${esc(w.editing.text)}</textarea></label>`
      + `<div class="cx-row"><button type="submit" class="hd-btn hd-btn--sm"${w.busy ? ' disabled' : ''}>Save edit</button><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="edit-cancel">Cancel</button></div></form>`
    : `<p class="wb-text">${esc(c.text)}</p>`;
  const actions = w.writable ? `<div class="cx-row">${mine && !editing ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="edit" data-wb-id="${esc(c.comment_id)}">Edit</button>` : ''}`
    + `${mine || w.isOwner ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="retract" data-wb-id="${esc(c.comment_id)}">${mine ? 'Retract' : 'Remove (owner)'}</button>` : ''}`
    + `${c.revision > 1 ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="history" data-wb-id="${esc(c.comment_id)}">History (${c.revision})</button>` : ''}</div>` : '';
  return `<li class="wb-comment" data-wb-comment="${esc(c.comment_id)}"><p class="cx-msg">${meta}</p>${body}${actions}${renderHistory(c, w)}</li>`;
}

function renderHistory(c, w) {
  if (!w.history || w.history.id !== c.comment_id) return '';
  const rows = (w.history.items || []).map((h) => `<li><span class="hd-ident">${esc(h.kind)}</span> ${esc(text(h.by && h.by.id, 'unknown'))} ${when(h.at)}`
    + `${h.text === null || h.text === undefined ? ' <span class="cx-msg">(text withheld)</span>' : `<blockquote class="wb-text">${esc(h.text)}</blockquote>`}</li>`).join('');
  return `<div class="wb-history" role="group" aria-label="Edit history"><ol class="cx-list">${rows}</ol></div>`;
}

export const anchorOf = (w) => (w.edge ? { kind: 'edge', ...w.edge } : { kind: 'node', node_id: w.nodeId });
export const anchorLabel = (a) => (a.kind === 'edge' ? `link ${a.from_id} ${a.edge_type} ${a.to_id}` : a.node_id);

export function renderDiscussion(w) {
  const n = esc(anchorLabel(anchorOf(w)));
  const list = (w.comments || []).map((c) => renderComment(c, w)).join('');
  const form = w.writable
    ? `<form data-wb-form="comment" class="cx-stack"><label class="cx-field">Add a comment on <code>${n}</code><textarea class="cx-input" name="text" data-wb-field="comment" maxlength="${MAX_COMMENT}" rows="3" required aria-describedby="wb-comment-help">${esc(w.draft.comment)}</textarea></label>`
      + `<p class="cx-msg" id="wb-comment-help">Plain text, up to ${MAX_COMMENT} characters. Shown to this room's members. Comments are discussion only: they are never sent to an agent and never change a run.</p>`
      + `<button type="submit" class="hd-btn hd-btn--sm"${w.busy ? ' disabled' : ''}>Post comment</button></form>`
    : `<p class="cx-msg">${esc(w.readOnlyReason || 'You can read comments here but not write them.')}</p>`;
  return `<section class="wb-section" aria-label="Discussion on ${n}"><h4>Discussion</h4>`
    + `${w.edge ? `<p class="cx-msg">Showing comments on a link. <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="edge-clear">Back to the item</button></p>` : ''}`
    + `${w.error ? `<p class="cx-msg" role="alert">${esc(w.error)}</p>` : ''}`
    + `${list ? `<ul class="cx-list wb-comments">${list}</ul>` : '<p class="cx-msg">No comments on this item yet.</p>'}${form}</section>`;
}

// ---- steering ----------------------------------------------------------------------------------------------------
function summary(s) {
  const t = s.target || {};
  return `<dl class="lin-dl"><dt>Action</dt><dd><strong>${esc(ACTION_LABEL[s.action] || s.action)}</strong></dd>`
    + `<dt>Target</dt><dd><code>${esc(text(t.node_id))}</code> (${esc(text(t.node_type))})</dd>`
    + `<dt>Delivered only to</dt><dd><code>${esc(text(t.pinned_source, 'unknown'))}</code></dd>`
    + `${t.expected_digest ? `<dt>Digest</dt><dd><code>${esc(t.expected_digest)}</code></dd>` : ''}`
    + `<dt>Proposed by</dt><dd>${esc(text(s.proposed_by && s.proposed_by.id, 'unknown'))} ${when(s.proposed_at)}</dd>`
    + `${s.confirmed_by ? `<dt>Confirmed by</dt><dd>${esc(s.confirmed_by.id)}</dd>` : ''}`
    + `${s.reason ? `<dt>Reason</dt><dd>${esc(s.reason)}</dd>` : ''}</dl>`
    + `${s.instruction ? `<p class="cx-msg">Instruction text (untrusted, written by ${esc(text(s.proposed_by && s.proposed_by.id, 'the proposer'))}):</p><blockquote class="wb-text">${esc(s.instruction)}</blockquote>` : ''}`;
}

function renderSteer(s, w) {
  const look = statusLook(s.status);
  const isMine = s.proposed_by && s.proposed_by.id === w.me;
  const t = w.target || {};
  const canConfirm = s.status === 'pending' && t.can_confirm && (!isMine || t.self_confirm) && w.writable;
  const canClose = ['pending', 'confirmed', 'delivered'].includes(s.status) && w.writable && (isMine || t.can_confirm);
  const open = w.confirming === s.steering_id;
  const ack = s.ack ? ` <strong>${esc(s.ack.outcome)}</strong>${s.ack.note ? `: ${esc(s.ack.note)}` : ''}` : '';
  const confirmPanel = open
    ? `<div class="wb-confirm" role="group" aria-label="Confirm steering request"><p><strong>Check this before you confirm.</strong> Confirming sends exactly this request, once, to the agent that reported the target.</p>${summary(s)}`
      + `<div class="cx-row"><button type="button" class="hd-btn hd-btn--sm" data-wb-act="confirm-go" data-wb-id="${esc(s.steering_id)}"${w.busy ? ' disabled' : ''}>Confirm and send</button>`
      + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="confirm-cancel">Cancel</button></div></div>` : '';
  return `<li class="wb-steer" data-wb-steer="${esc(s.steering_id)}"><p><span aria-hidden="true">${look.glyph}</span> <strong>${esc(ACTION_LABEL[s.action] || s.action)}</strong> <span class="cx-msg">${esc(look.word)}${ack}</span></p>`
    + `${open ? confirmPanel : summary(s)}`
    + `<div class="cx-row">${canConfirm && !open ? `<button type="button" class="hd-btn hd-btn--sm" data-wb-act="confirm" data-wb-id="${esc(s.steering_id)}">Review and confirm</button>` : ''}`
    + `${canClose ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="reject" data-wb-id="${esc(s.steering_id)}">${isMine ? 'Withdraw' : 'Decline'}</button>` : ''}</div></li>`;
}

export function renderProposeForm(w) {
  const t = w.target || {};
  const actions = t.actions || [];
  if (!actions.length) return '';
  const d = w.draft;
  const chosen = actions.includes(d.action) ? d.action : actions[0];
  const radios = actions.map((a) => `<label class="wb-radio"><input type="radio" name="action" value="${esc(a)}" data-wb-field="action"${a === chosen ? ' checked' : ''}> ${esc(ACTION_LABEL[a] || a)}<span class="cx-msg"> ${esc(ACTION_HELP[a] || '')}</span></label>`).join('');
  const redirect = chosen === 'redirect'
    ? `<label class="cx-field">Instruction (untrusted text, up to ${MAX_INSTRUCTION} characters)<textarea class="cx-input" name="instruction" data-wb-field="instruction" maxlength="${MAX_INSTRUCTION}" rows="3" required>${esc(d.instruction)}</textarea></label>` : '';
  const gate = (chosen === 'approve' || chosen === 'reject')
    ? `<label class="cx-field">Digest or commit shown on the gate<input class="cx-input" name="digest" data-wb-field="digest" maxlength="128" required value="${esc(d.digest)}"></label>` : '';
  return `<form data-wb-form="propose" class="cx-stack"><fieldset class="wb-fieldset"><legend>Propose a change to this run</legend>${radios}</fieldset>${redirect}${gate}`
    + `<label class="cx-field">Why (optional)<input class="cx-input" name="reason" data-wb-field="reason" maxlength="300" value="${esc(d.reason)}"></label>`
    + `<p class="cx-msg">${t.self_confirm ? 'A room owner confirms this next.' : 'A different room owner must confirm this before anything is sent.'} The target is fenced at <code>${esc(short(t.fence, 10))}</code>: if it changes first, the request is cancelled.</p>`
    + `<button type="submit" class="hd-btn hd-btn--sm"${w.busy ? ' disabled' : ''}>Propose</button></form>`;
}

export function renderSteering(w) {
  const t = w.target;
  const n = esc(w.nodeId);
  let head;
  if (!t) head = `<p class="cx-msg">${w.error ? '' : 'Checking whether this item can be steered…'}</p>`;
  else if (!t.steering_enabled) head = '<p class="cx-msg">Steering is switched off by the operator.</p>';
  else if (!(t.actions || []).length) head = `<p class="cx-msg">This item cannot be steered${t.superseded ? ': a newer attempt superseded it' : ''}${t.state ? ` (state: ${esc(t.state)})` : ''}. Only a running target that an agent reported can take a request.</p>`;
  else if (!w.writable) head = `<p class="cx-msg">${esc(w.readOnlyReason || 'You cannot propose changes here.')}</p>`;
  else if (!t.can_propose) head = '<p class="cx-msg">Your role in this room cannot propose changes.</p>';
  else head = renderProposeForm(w);
  const list = (w.steers || []).map((s) => renderSteer(s, w)).join('');
  return `<section class="wb-section" aria-label="Steering for ${n}"><h4>Steering</h4>`
    + `<p class="cx-msg">An explicit, two-step request, separate from comments. ${t && t.pinned_source ? `Delivered only to <code>${esc(t.pinned_source)}</code>, the source that reported this item.` : ''}</p>`
    + `${w.notice ? `<p class="cx-msg" role="status">${esc(w.notice)}</p>` : ''}${w.steerError ? `<p class="cx-msg" role="alert">${esc(w.steerError)}</p>` : ''}`
    + `${head}${list ? `<h5>Requests for this item</h5><ul class="cx-list wb-steers">${list}</ul>` : ''}</section>`;
}

export function renderWorkbench(w) {
  if (!w || !w.nodeId) return '';
  return `<div class="wb" aria-label="Comments and steering">${renderDiscussion(w)}${renderSteering(w)}</div>`;
}

export function freshDraft() { return { comment: '', action: 'pause', instruction: '', digest: '', reason: '' }; }

// ---- request bodies (the only place a body is built; never from DOM text beyond these named fields) -------------------
export function commentBody(anchor, textValue, key) {
  return { anchor, text: String(textValue), idempotency_key: key };
}
export function proposalBody(nodeId, draft, target, key) {
  const a = (target.actions || []).includes(draft.action) ? draft.action : target.actions[0];
  const body = { action: a, node_id: nodeId, expected_fence: target.fence, idempotency_key: key };
  if (a === 'redirect') body.instruction = String(draft.instruction);
  if (a === 'approve' || a === 'reject') body.expected_digest = String(draft.digest).trim();
  if (draft.reason && draft.reason.trim()) body.reason = String(draft.reason).trim();
  return body;
}
