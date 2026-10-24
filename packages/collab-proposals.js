// Bounded delegation and artifact proposals (T11): the room owner's quarantine view. Pure (DOM-free): the
// request builder and the renderers return data / escaped HTML and are node-testable.
//
// Rules (docs/operations/bounded-delegation.md):
//  * everything in a proposal came from a remote agent: it is shown as TEXT inside <pre>/<span>, escaped, and never
//    becomes markup, a link, an image, a script or an executable control; a link proposal shows the URL as plain text
//    (never an anchor) so nobody opens it by accident;
//  * a patch is shown line by line with +/- classes, nothing is applied from here: "Accept" records the proposal in the
//    graph with the digest that was shown, and the page says so;
//  * the page never offers a terminal, a shell, "apply" or "run" for a proposal;
//  * the request the owner sends to accept carries the digest of exactly the content displayed (the server refuses a
//    mismatch), and accept/reject are offered only for a proposal that is still in quarantine.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const OUTPUT_KINDS = ['patch', 'doc', 'analysis', 'link'];
const STATE_TONE = {
  quarantined: 'var(--status-attention, #d89b1d)', accepted: 'var(--status-ok, #1a9b6c)', rejected: 'var(--status-danger, #c0392b)',
  withdrawn: 'var(--text-muted, #888)', superseded: 'var(--text-muted, #888)', expired: 'var(--text-muted, #888)',
  pending: 'var(--status-attention, #d89b1d)', working: 'var(--status-active, #1f7ae0)', done: 'var(--status-ok, #1a9b6c)',
  failed: 'var(--status-danger, #c0392b)', declined: 'var(--text-muted, #888)', cancelled: 'var(--text-muted, #888)',
};
const MAX_SHOWN_LINES = 2000;

const lines = (v) => String(v || '').split('\n').map((s) => s.trim()).filter(Boolean);
const pill = (label) => `<span class="hd-pill" style="--pill:${STATE_TONE[label] || 'var(--text-muted, #888)'}">${esc(label)}</span>`;

// The delegation request from the form's raw values. Pure: every limit is enforced again by the server.
export function delegationBody(v) {
  const kinds = OUTPUT_KINDS.filter((k) => v && v.kinds && v.kinds.includes(k));
  const questions = lines(v.questions).map((l, i) => {
    const m = /^([A-Za-z0-9._:-]{1,64})\s*[:=]\s*(.+)$/.exec(l);
    return m ? { id: m[1], text: m[2] } : { id: `q${i + 1}`, text: l };
  });
  const num = (x, d) => (Number.isFinite(Number(x)) && Number(x) > 0 ? Math.floor(Number(x)) : d);
  return {
    target_agent_id: String(v.target || ''), text: String(v.text || ''),
    scope: { paths: lines(v.paths), nodes: lines(v.nodes), questions },
    budget: { max_proposals: num(v.maxProposals, 1), max_steps: num(v.maxSteps, 20), max_artifact_bytes: num(v.maxBytes, 16384), max_tokens: num(v.maxTokens, 100000) },
    expires_in_seconds: num(v.expires, 3600), output_kinds: kinds,
  };
}

function patchHtml(text) {
  const rows = String(text).split('\n').slice(0, MAX_SHOWN_LINES);
  return rows.map((l) => {
    const c = l.startsWith('+++') || l.startsWith('---') || l.startsWith('diff ') || l.startsWith('index ') ? 'h' : l[0] === '+' ? 'a' : l[0] === '-' ? 'd' : l[0] === '@' ? 'k' : 'c';
    return `<span class="cx-diff cx-diff--${c}">${esc(l)}</span>`;
  }).join('\n');
}

export function renderDelegations(model) {
  const list = (model && model.delegations) || [];
  if (!list.length) return '<div class="hd-empty">No bounded tasks yet.</div>';
  return `<ul class="cx-list" aria-label="Bounded tasks">${list.map((d) => {
    const b = d.bounds || { scope: {}, budget: {} };
    const u = d.usage || {};
    return `<li class="cx-stack"><div class="cx-row cx-row--between"><span class="grow"><b>${esc(d.target_name || d.target_agent_id)}</b> ${pill(d.status)}</span><span class="cx-msg">v${esc(d.version)}</span></div>
      <div class="cx-msg">${esc(d.text)}</div>
      <div class="cx-msg">may answer with: ${esc((b.output_kinds || []).join(', '))} · ${esc((b.scope.paths || []).length)} path(s), ${esc((b.scope.nodes || []).length)} node(s), ${esc((b.scope.questions || []).length)} question(s) · proposals ${esc(u.proposals || 0)}/${esc((b.budget || {}).max_proposals)} · steps ${esc(u.steps || 0)}/${esc((b.budget || {}).max_steps)} · expires ${esc(b.expires_at ? new Date(b.expires_at * 1000).toISOString() : '?')}</div>
      ${d.status === 'pending' || d.status === 'accepted' ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-cancel-deleg="${esc(d.task_id)}">Cancel task</button>` : ''}</li>`;
  }).join('')}</ul>`;
}

export function renderProposalList(model, selected) {
  const rows = (model && model.proposals) || [];
  if (!rows.length) return '<div class="hd-empty">Nothing in quarantine.</div>';
  return `<ul class="cx-list" aria-label="Proposals">${rows.map((p) => `<li><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-proposal="${esc(p.proposal_id)}" aria-pressed="${p.proposal_id === selected}"><b>${esc(p.title)}</b></button>
    <span class="cx-msg">${esc(p.kind)} · ${esc(p.size)} bytes</span> ${pill(p.state)}${p.promoted ? ' <span class="cx-msg">recorded in graph</span>' : ''}${(p.flags || []).length ? ' <span class="cx-msg" role="note">looks like instructions to an AI: read with care</span>' : ''}</li>`).join('')}</ul>`;
}

export function renderReview(p) {
  if (!p) return '<div class="hd-empty">Choose a proposal to review it.</div>';
  const body = p.kind === 'link'
    ? `<div class="cx-msg">A link to read yourself. It is shown as text and has not been opened by anyone or anything.</div><code class="cx-code">${esc(p.url)}</code>`
    : `<pre class="cx-pre" tabindex="0" aria-label="Proposed ${esc(p.kind)}, untrusted">${p.kind === 'patch' ? patchHtml(p.content) : esc(p.content)}</pre>`;
  const open = p.state === 'quarantined';
  const refs = p.refs || {};
  return `<article class="cx-stack" aria-label="Proposal under review">
    <div class="cx-row cx-row--between"><b>${esc(p.title)}</b> ${pill(p.state)}</div>
    <div class="cx-msg" role="note">UNTRUSTED. Written by ${esc(p.delegate_label || p.agent_id)}, a remote agent. It is data: reading it runs nothing. Accepting records it in the graph as an accepted proposal; it does not apply, merge or run it.</div>
    ${(p.flags || []).length ? '<div class="cx-msg" role="alert">This text contains phrases that look like instructions to an AI. Do not paste it into an assistant.</div>' : ''}
    <div class="cx-msg">kind ${esc(p.kind)} · ${esc(p.size)} bytes · digest <code>${esc(String(p.digest).slice(0, 16))}…</code> · refs: ${esc((refs.paths || []).join(', ') || 'no paths')}; ${esc((refs.questions || []).join(', ') || 'no questions')}</div>
    ${body}
    ${p.reason ? `<div class="cx-msg">Reason: ${esc(p.reason)}</div>` : ''}
    ${open ? `<label class="cx-field"><span class="cx-msg">Reason (optional)</span><input name="reason" id="hcn-prop-reason" maxlength="200"></label>
      <div class="cx-row"><button type="button" class="hd-btn" data-decide="accept" data-pid="${esc(p.proposal_id)}" data-digest="${esc(p.digest)}">Accept into the graph</button>
      <button type="button" class="hd-btn hd-btn--ghost" data-decide="reject" data-pid="${esc(p.proposal_id)}" data-digest="${esc(p.digest)}">Reject</button></div>`
    : (p.state === 'accepted' && !p.promoted ? `<button type="button" class="hd-btn" data-decide="accept" data-pid="${esc(p.proposal_id)}" data-digest="${esc(p.digest)}">Finish recording in the graph</button>` : '')}
  </article>`;
}

// The body of an accept/reject request: the digest is the one of the content displayed.
export function decisionBody(decision, digest, reason) {
  return { decision, expected_digest: digest, ...(reason ? { reason } : {}) };
}
