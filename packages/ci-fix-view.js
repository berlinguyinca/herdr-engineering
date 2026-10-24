// "Fix in sandbox" on a failed CI run. The control plane only records the request; a runner on one of
// the operator's hosts clones the exact commit into a throwaway sandbox, runs Pi there and, if it
// changed anything, opens a draft pull request. This module renders the button and the status card and
// holds no state: integrations-view.js owns polling, requests and focus.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';
import { attachForms } from './terminal-attach.js';

export const FIX_ACTIVE = ['queued', 'claimed', 'preparing', 'running', 'finishing'];
const TERMINAL = ['succeeded', 'no_change', 'failed', 'cancelled', 'expired'];
const LABELS = {
  queued: 'Queued', claimed: 'Starting', preparing: 'Preparing the sandbox', running: 'Pi is working',
  finishing: 'Checking the result', succeeded: 'Draft pull request opened', no_change: 'Pi made no change',
  failed: 'Failed', cancelled: 'Cancelled', expired: 'Expired',
};

export const fixIsActive = (fix) => FIX_ACTIVE.includes(fix?.state);

export function fixRunKey(repository, number) { return `${repository}:${number}`; }

// The newest fix for a run, from a list the server already ordered newest first.
export function latestFix(response) {
  const rows = Array.isArray(response?.fixes) ? response.fixes.filter((f) => f && typeof f.id === 'string') : [];
  return rows[0] || null;
}

function safeHttps(value) {
  if (typeof value !== 'string' || /[\x00-\x20\x7f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

const when = (iso) => { const t = Date.parse(iso || ''); return Number.isNaN(t) ? null : t; };

export function fixElapsed(fix, now = Date.now()) {
  const start = when(fix?.started_at) ?? when(fix?.requested_at);
  if (start === null) return '';
  const end = TERMINAL.includes(fix.state) ? (when(fix.ended_at) ?? now) : now;
  return formatDuration(Math.max(0, end - start));
}

export function fixError(err) {
  if (err?.status === 404) return 'CI fix agents are not enabled on this control plane.';
  if (err?.status === 401 || err?.status === 403) return 'Starting a fix agent requires operator access.';
  if (err?.status === 400) return `The request was refused: ${err?.body?.error || err?.message || 'invalid request'}`;
  return 'The request was not confirmed; clicking again checks the same operation.';
}

function result(fix) {
  const r = fix.result || {};
  const pr = safeHttps(r.pr_url);
  const stat = ['files', 'insertions', 'deletions'].every((k) => Number.isFinite(r[k]))
    ? `<p class="ig-note">${esc(r.files)} file${r.files === 1 ? '' : 's'} · +${esc(r.insertions)} −${esc(r.deletions)}${Number.isFinite(r.commits) ? ` · ${esc(r.commits)} commit${r.commits === 1 ? '' : 's'}` : ''}</p>` : '';
  const flags = Array.isArray(r.flags) && r.flags.length
    ? `<ul class="ig-fix-flags" aria-label="Flags for review">${r.flags.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>` : '';
  return `${pr ? `<p class="ig-note"><a href="${esc(pr)}" target="_blank" rel="noopener noreferrer" data-fix-pr>Open the draft pull request${Number.isFinite(r.pr_number) ? ` #${esc(r.pr_number)}` : ''}</a> <span>Review it before merging; the agent never merges.</span></p>` : ''}${stat}${flags}${r.summary ? `<p class="ig-note ig-fix-summary">${esc(r.summary)}</p>` : ''}`;
}

function terminal(fix, { terminal: attach, terminalError }) {
  if (!fix.session_id || !fixIsActive(fix) && fix.state !== 'succeeded') return '';
  const forms = attach ? attachForms(attach) : [];
  return `<div class="ig-fix-terminal"><button type="button" class="hd-btn hd-btn--sm" data-fix-terminal>Terminal</button>`
    + (terminalError ? `<p class="ig-note" role="status">${esc(terminalError)}</p>` : '')
    + (attach ? (forms.length ? `<p class="ig-note">Run one of these in a terminal on your machine:</p>${forms.map((f, i) => `<div class="ig-fix-cmd"><span>${esc(f.label)}${f.recommended ? ' <strong>(suggested)</strong>' : ''}</span><code>${esc(f.command)}</code><button type="button" class="hd-btn hd-btn--sm" data-fix-copy="${i}">Copy command</button></div>`).join('')}` : '<p class="ig-note">No attach command available.</p>') : '')
    + '</div>';
}

export function renderCiFix({ available = true, fix = null, message = '', terminal: attach = null, terminalError = '', now = Date.now() } = {}) {
  if (!available) return '';
  const start = `<button type="button" class="hd-btn" data-fix-sandbox>${fix ? 'Fix in sandbox again' : 'Fix in sandbox'}</button>`;
  const note = '<p class="ig-note">Clones this exact commit into a throwaway sandbox on one of your hosts, runs Pi there, and opens a draft pull request if it changes anything. It never merges.</p>';
  const status = message ? `<p class="ig-note" role="status">${esc(message)}</p>` : '';
  if (!fix) return `<section class="ig-fix" data-ci-fix><h3>Fix in sandbox</h3>${note}${start}${status}</section>`;
  const state = String(fix.state || '');
  const label = LABELS[state] || state;
  const elapsed = fixElapsed(fix, now);
  const meta = [fix.host && `Host ${esc(fix.host)}`, fix.model && `Model ${esc(fix.model)}`, elapsed && `${esc(elapsed)} elapsed`].filter(Boolean).join(' · ');
  const agent = fix.session_id ? `<p class="ig-note"><a href="#/herd/session/${esc(encodeURIComponent(fix.session_id))}" data-fix-agent>Open the agent</a></p>` : '';
  const cancel = fixIsActive(fix) && !fix.cancel_requested ? '<button type="button" class="hd-btn hd-btn--sm" data-fix-cancel>Cancel</button>' : '';
  return `<section class="ig-fix" data-ci-fix data-fix-state="${esc(state)}"><h3>Fix in sandbox</h3>`
    + `<p class="ig-fix-state"><strong data-fix-label>${esc(label)}</strong>${fix.cancel_requested && fixIsActive(fix) ? ' <span>· cancel requested</span>' : ''}</p>`
    + (fix.reason ? `<p class="ig-note" data-fix-reason>${esc(fix.reason)}</p>` : '')
    + (meta ? `<p class="ig-note">${meta}</p>` : '')
    + agent + (state === 'succeeded' ? result(fix) : '') + terminal(fix, { terminal: attach, terminalError })
    + `<div>${cancel}${TERMINAL.includes(state) && state !== 'succeeded' ? start : ''}</div>${status}</section>`;
}

export const ciFixStyles = '.ig-fix{margin:16px 0;padding:12px;border:1px solid var(--border);border-radius:var(--radius);font-size:12px}.ig-fix h3{margin:0 0 6px;font-size:14px}.ig-fix-state{margin:4px 0}.ig-fix-flags{margin:6px 0;padding-left:18px;overflow-wrap:anywhere}.ig-fix-cmd{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}.ig-fix-cmd code{flex:1 1 100%;overflow-wrap:anywhere}.ig-fix a{overflow-wrap:anywhere}.ig-fix-summary{white-space:pre-wrap;overflow-wrap:anywhere}';
