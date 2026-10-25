// Attention inbox, evidence-based progress and analytics panels (T15). Pure and DOM-free: every renderer takes the
// server's read model and returns escaped HTML, so it is node-testable. Nothing here computes progress or priority:
// the server derives both from visible evidence; the page only shows them and labels what is an estimate.
//
// Accessibility: severity is always written out as text (never colour alone), actions are real buttons with labels
// that name the item, the list is a labelled list, and the empty and "insufficient evidence" states are text.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';

const SEVERITY_LABEL = { error: 'Error', warning: 'Warning', notice: 'Notice', info: 'Info' };
const SNOOZES = [[3600, '1 hour'], [86400, '1 day'], [7 * 86400, '1 week']];
const GITHUB_PR = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[0-9]{1,9}$/;
const REASONS = {
  insufficient_evidence: 'Insufficient evidence',
  no_trusted_evidence_configured: 'No evidence source is configured yet',
};

const str = (v, d = '') => (typeof v === 'string' ? v : d);
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const pct = (v) => (num(v) === null ? 'n/a' : `${Math.round(v * 1000) / 10}%`);

export function safeHref(u) { return typeof u === 'string' && GITHUB_PR.test(u) ? u : null; }

export function duration(sec) {
  const s = num(sec);
  if (s === null || s < 0) return 'n/a';
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 129600) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

function subjectText(it) {
  const s = it.subject || {};
  if (s.hidden) return 'An item you cannot see';
  return str(s.label) || str(s.node_id) || str(s.user_id) || 'Item';
}

function itemRow(it, canAct) {
  const key = esc(str(it.key));
  const sev = str(it.severity, 'info');
  const name = subjectText(it);
  const quiet = it.state === 'acknowledged' || it.state === 'snoozed';
  const flags = [];
  if (it.for_you) flags.push('For you');
  if (it.resurfaced) flags.push('Changed since you dismissed it');
  if (it.state === 'snoozed') flags.push(`Snoozed until ${str(it.snoozed_until)}`);
  if (it.state === 'acknowledged') flags.push('Acknowledged');
  const detail = it.detail && typeof it.detail === 'object' ? Object.entries(it.detail)
    .filter(([, v]) => ['string', 'number', 'boolean'].includes(typeof v))
    .map(([k, v]) => `${esc(k.replace(/_/g, ' '))}: ${esc(String(v))}`).join(' · ') : '';
  const actions = !canAct ? '' : quiet
    ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-attn-act="restore" data-attn-key="${key}" aria-label="Show again: ${esc(name)}">Show again</button>`
    : `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-attn-act="ack" data-attn-key="${key}" data-attn-fp="${esc(str(it.fp))}" aria-label="Acknowledge: ${esc(name)}">Acknowledge</button>
       <label class="cx-msg">Snooze <select data-attn-snooze data-attn-key="${key}" data-attn-fp="${esc(str(it.fp))}" aria-label="Snooze ${esc(name)}"><option value="">...</option>${SNOOZES.map(([s, l]) => `<option value="${s}">${l}</option>`).join('')}</select></label>`;
  return `<li class="cx-attn-item" data-attn-sev="${esc(sev)}" data-attn-key-row="${key}">
    <span class="cx-attn-sev"><b>${esc(SEVERITY_LABEL[sev] || sev)}</b></span>
    <span class="grow"><b>${esc(str(it.title))}</b>: ${esc(name)}${flags.length ? ` <span class="cx-msg">(${flags.map(esc).join('; ')})</span>` : ''}
    ${detail ? `<span class="cx-msg"> ${detail}</span>` : ''}${it.since ? `<span class="cx-msg"> since ${esc(str(it.since))}</span>` : ''}</span>
    <span class="cx-row">${actions}</span></li>`;
}

export function renderInbox(m) {
  if (m.error) return `<p class="hd-empty" role="alert">${esc(m.error)}</p>`;
  if (!m.data) return skeleton('the attention inbox');
  const d = m.data;
  const items = Array.isArray(d.items) ? d.items : [];
  const quiet = Array.isArray(d.quiet_items) ? d.quiet_items : [];
  const c = d.counts || {};
  const caps = Object.entries(c.not_shown_because_of_caps || {}).filter(([, n]) => n > 0);
  const sev = Object.entries(c.by_severity || {}).map(([k, n]) => `${esc(SEVERITY_LABEL[k] || k)} ${Number(n) || 0}`).join(', ');
  return `<section aria-label="Attention inbox">
    ${m.notice ? `<p class="cx-msg" role="alert">${esc(m.notice)}</p>` : ''}
    <p class="cx-msg" role="status">${items.length ? `${items.length} item${items.length === 1 ? '' : 's'} need attention${sev ? ` (${sev})` : ''}.` : 'Nothing needs your attention.'}
      Ordered by severity, then items for you, then oldest first.</p>
    ${items.length ? `<ul class="cx-list" aria-label="Items that need attention">${items.map((i) => itemRow(i, m.canAct !== false)).join('')}</ul>` : ''}
    ${caps.length ? `<p class="cx-msg" role="status">More items exist than are shown: ${caps.map(([k, n]) => `${esc(k.replace(/_/g, ' '))} ${Number(n)}`).join(', ')}.</p>` : ''}
    ${d.truncated || d.timeline_truncated ? '<p class="cx-msg" role="status">The room view was cut by a read budget; some items may be missing.</p>' : ''}
    ${Number(c.acknowledged) || Number(c.snoozed) ? `<p class="cx-msg">${Number(c.acknowledged) || 0} acknowledged, ${Number(c.snoozed) || 0} snoozed. <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-attn-act="toggle-quiet" aria-expanded="${m.showQuiet ? 'true' : 'false'}">${m.showQuiet ? 'Hide them' : 'Show them'}</button></p>` : ''}
    ${m.showQuiet && quiet.length ? `<ul class="cx-list" aria-label="Acknowledged and snoozed items">${quiet.map((i) => itemRow(i, m.canAct !== false)).join('')}</ul>` : ''}
  </section>`;
}

function evidenceList(task) {
  const ev = Array.isArray(task.evidence) ? task.evidence : [];
  if (!ev.length) return '<span class="cx-msg">No evidence</span>';
  return ev.map((e) => {
    const href = safeHref(e.url);
    const label = esc(str(e.label) || str(e.node_id));
    const link = href ? `<a href="${esc(href)}" rel="noopener noreferrer" target="_blank">${label}</a>` : label;
    const why = Array.isArray(e.why_not) && e.why_not.length ? ` (${e.why_not.map((w) => esc(String(w).replace(/_/g, ' '))).join(', ')})` : '';
    return `<span class="cx-msg">${esc(str(e.kind).replace(/_/g, ' '))}: ${link} <b>${e.counted ? 'counted' : 'not counted'}</b>${why}</span>`;
  }).join('<br>');
}

export function renderEstimate(e) {
  if (!e || typeof e !== 'object') return '';
  if (e.status === 'estimate') {
    const f = e.finish || {};
    const i = e.interval_seconds || {};
    return `<p class="cx-msg"><b>Estimate</b> (${esc(str(e.method))}, confidence ${esc(str(e.confidence))}): remaining work likely has evidence around
      ${esc(str(f.likely))}, between ${esc(str(f.earliest))} and ${esc(str(f.latest))}. Based on ${Number(e.n_completed) || 0} completed tasks, typical gap
      ${esc(duration(i.p50))} (middle half ${esc(duration(i.p25))} to ${esc(duration(i.p75))}).
      ${(e.confidence_reasons || []).length ? `Why not higher: ${e.confidence_reasons.map(esc).join('; ')}.` : ''}
      <br>Assumes: ${(e.assumptions || []).map(esc).join('; ')}.</p>`;
  }
  if (e.status === 'not_needed') return `<p class="cx-msg">No estimate needed: ${esc(str(e.reason))}.</p>`;
  return `<p class="cx-msg"><b>Estimate: insufficient evidence.</b> ${esc(str(e.reason))}</p>`;
}

function summaryLine(g) {
  if (g.status && g.status !== 'ok') return `<b>${esc(REASONS[g.status] || 'Insufficient evidence')}</b>. ${esc(str(g.note))}`;
  return `<b>${Number(g.complete) || 0}</b> of ${Number(g.tasks) || 0} tasks have completion evidence (${pct(g.completion)})` +
    `${Number(g.verified_not_merged) ? `, ${Number(g.verified_not_merged)} reviewed or validated but not merged` : ''}` +
    `${Number(g.reported_done_without_evidence) ? `, ${Number(g.reported_done_without_evidence)} reported done without evidence` : ''}`;
}

export function renderProgress(m) {
  if (m.error) return `<p class="hd-empty" role="alert">${esc(m.error)}</p>`;
  if (!m.data) return skeleton('progress');
  const d = m.data;
  const r = d.room || {};
  const tasks = Array.isArray(d.tasks) ? d.tasks : [];
  const ms = Array.isArray(d.milestones) ? d.milestones : [];
  return `<section aria-label="Progress from evidence">
    <p>${summaryLine(r)}</p>
    <progress max="100" value="${num(r.progress) === null ? 0 : Math.round(r.progress * 100)}" aria-label="Evidence-based progress"></progress>
    ${renderEstimate(r.estimate)}
    <p class="cx-msg">${esc(str((d.method || {}).completion))} Agent self-reports (state, labels, cost) are never counted.</p>
    ${ms.length ? `<h3>Milestones</h3><ul class="cx-list" aria-label="Milestones">${ms.map((g) => `<li><span class="grow"><b>${esc(str(g.label) || str(g.milestone))}</b>: ${summaryLine(g)}${renderEstimate(g.estimate)}</span></li>`).join('')}</ul>` : ''}
    <h3>Tasks</h3>
    ${tasks.length ? `<ul class="cx-list" aria-label="Tasks and their evidence">${tasks.map((t) => `<li><span class="grow"><b>${esc(str(t.label) || str(t.task_id))}</b>
      <span class="cx-msg">stage: ${esc(str(t.stage))}${t.reported_done && t.stage === 'none' ? ' (reported done, no evidence)' : ''}</span><br>${evidenceList(t)}</span></li>`).join('')}</ul>` : '<p class="hd-empty">No tasks are visible to you.</p>'}
    ${Number(d.tasks_omitted) ? `<p class="cx-msg">${Number(d.tasks_omitted)} more tasks not listed.</p>` : ''}
    ${d.truncated || d.timeline_truncated ? '<p class="cx-msg" role="status">The room view was cut by a read budget; counts may be low.</p>' : ''}
  </section>`;
}

function metric(label, v, render) {
  if (!v || typeof v !== 'object') return '';
  if (v.suppressed) return `<tr><th scope="row">${esc(label)}</th><td>Hidden: too few items to show without singling one out</td></tr>`;
  if (v.reported === false) return `<tr><th scope="row">${esc(label)}</th><td>Not reported</td></tr>`;
  return `<tr><th scope="row">${esc(label)}</th><td>${render(v)}<br><span class="cx-msg">${esc(str(v.basis))}</span></td></tr>`;
}

export function renderAnalytics(m) {
  if (m.error) return `<p class="hd-empty" role="alert">${esc(m.error)}</p>`;
  if (!m.data) return skeleton('analytics');
  const d = m.data;
  const mix = d.state_mix && !d.state_mix.suppressed ? Object.entries(d.state_mix).map(([k, n]) => `${esc(k)} ${Number(n) || 0}`).join(', ') : '';
  return `<section aria-label="Optimization analytics">
    <p class="cx-msg">${esc(str((d.scope || {}).note))} Groups smaller than ${Number((d.scope || {}).min_group_size) || 0} are hidden.</p>
    <table class="cx-table"><caption class="cx-msg">Aggregates over what you can see</caption><tbody>
      ${metric('Cycle time', d.cycle_time, (v) => `median ${esc(duration(v.median_seconds))}, 75th percentile ${esc(duration(v.p75_seconds))} (n=${Number(v.n)})`)}
      ${metric('Rework rate', d.rework, (v) => `${pct(v.rate)} of ${Number(v.of)} finished tasks`)}
      ${metric('Re-review rate', d.rereview, (v) => `${pct(v.rate)} of ${Number(v.of)} reviewed pull requests`)}
      ${metric('Approach win rate', d.fork_win_rate, (v) => `${pct(v.rate)} of ${Number(v.of)} decided approaches`)}
      ${metric('Blocked time', d.blocked_time, (v) => `${pct(v.share)} of active time (${Number(v.lanes)} lanes)`)}
      ${metric('Idle time', d.idle_time, (v) => `${pct(v.share)} of active time (${Number(v.lanes)} lanes)`)}
      ${d.cost ? (d.cost.suppressed ? metric('Cost', d.cost, () => '') : d.cost.reported === false ? metric('Cost', d.cost, () => '') :
        `<tr><th scope="row">Cost</th><td>${esc(String(d.cost.usd))} USD, ${Number(d.cost.tokens)} tokens, <b>self-reported</b> by ${Number(d.cost.reporting_nodes)} of ${Number(d.cost.of_nodes)} nodes</td></tr>`) : ''}
      <tr><th scope="row">Task states</th><td>${mix || 'Hidden: too few items'}</td></tr>
    </tbody></table>
    ${d.milestones_small_groups_hidden ? '<p class="cx-msg">Some small missions are not listed.</p>' : ''}
    ${d.truncated || d.timeline_truncated ? '<p class="cx-msg" role="status">The room view was cut by a read budget; figures cover part of the history.</p>' : ''}
  </section>`;
}

export const ackBody = (key, fp) => ({ key, fp });
export const snoozeBody = (key, fp, seconds) => ({ key, fp, seconds: Number(seconds) });
export const restoreBody = (key) => ({ key });
export { SNOOZES };
