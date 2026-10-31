// How agents got to one pull request (GET /api/v1/pull-requests/<owner>/<repo>/<number>/trail): the outcome and the learning
// signals first, then the agents, handoffs, reviews and CI runs in the order they happened. Every link the server derived says
// why (its rule); a figure the server could not support is shown as "unknown" with its reason, never as zero or "no".
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';
import { skeleton } from './motion.js';
import { pollWhileVisible } from './refresh-bus.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);
const when = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''; };

export const RULE_TEXT = {
  commit: 'the commit the agent reported', branch_window: 'its branch, while it worked', pull_request_record: 'the pull request record on its branch',
  mission: 'the same mission', session: 'a linked agent', pull_request: 'its pull request number', branch: 'the pull request branch',
};

const SIGNALS = [
  ['merged_without_rework', 'Merged without rework', (v) => (v ? 'yes' : 'no')],
  ['ci_green_first_try', 'CI green first try', (v) => (v ? 'yes' : 'no')],
  ['ci_reruns', 'CI reruns', (v) => String(v)],
  ['cost_usd_estimate', 'Estimated cost', (v) => (Number(v) > 0 && Number(v) < 0.01 ? '<$0.01' : `$${Number(v).toFixed(2)}`)],
  ['duration_s', 'Time to finish', (v) => formatDuration(Number(v) * 1000)],
  ['handoff_problems', 'Problem handoffs', (v) => String(v)],
  ['review_verdicts', 'Review verdicts', (v) => (list(v).length ? list(v).map((r) => text(r.verdict) || 'no verdict').join(', ') : 'none recorded')],
];

/** Signal cards: value, or "unknown" with the server's reason. */
export function signalCards(signals = {}) {
  return SIGNALS.map(([key, label, show]) => {
    const s = signals && typeof signals[key] === 'object' && signals[key] ? signals[key] : {};
    const known = s.value !== null && s.value !== undefined;
    return { key, label, known, value: known ? show(s.value) : 'unknown', reason: text(s.reason) };
  });
}

/** Everything the page needs, from the server's answer; tolerant of a missing or odd body. */
export function trailModel(data) {
  const d = data && typeof data === 'object' ? data : {};
  const pr = d.pull_request && typeof d.pull_request === 'object' ? d.pull_request : {};
  return {
    pr: { number: pr.number, repository: text(pr.repository), url: text(pr.url), status: text(pr.status) || 'unknown', branch: text(pr.branch), opened: text(pr.opened_at) },
    state: text(d.state) || 'unknown', reason: text(d.reason),
    sources: d.sources && typeof d.sources === 'object' ? d.sources : {},
    omitted: d.omitted && typeof d.omitted === 'object' ? d.omitted : {},
    sessions: list(d.sessions), inferred: list(d.inferred_sessions), ambiguous: list(d.ambiguous), handoffs: list(d.handoffs), reviews: list(d.reviews), runs: list(d.ci_runs),
    timeline: list(d.timeline), signals: signalCards(d.signals),
  };
}

const KIND_LABEL = { session: 'Agent', handoff: 'Handoff', review: 'Review', ci: 'CI', pr_opened: 'Pull request', pr_closed: 'Pull request' };

const STYLE = `<style>.pt-sig{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin:12px 0}.pt-card{border:1px solid var(--border);border-radius:8px;padding:8px 12px}.pt-card b{display:block;font-size:16px}.pt-card span{font-size:12px;color:var(--text-secondary);overflow-wrap:anywhere}.pt-card[data-known=false] b{color:var(--text-secondary)}.pt-note{font-size:13px;color:var(--text-secondary);margin:8px 0}.pt-line{list-style:none;margin:8px 0 16px;padding:0 0 0 16px;border-left:2px solid var(--border)}.pt-line li{position:relative;padding:4px 0 8px 12px;font-size:13px;line-height:1.4;overflow-wrap:anywhere}.pt-line li::before{content:"";position:absolute;left:-21px;top:9px;width:8px;height:8px;border-radius:50%;background:var(--text-muted)}.pt-line li[data-kind=session]::before{background:var(--accent)}.pt-line li[data-kind=handoff]::before{background:var(--status-attention)}.pt-line li[data-problem=true]::before{background:var(--status-failed)}.pt-line li[data-kind=pr_closed]::before{background:var(--status-ok)}.pt-line time{display:block;font-size:11px;color:var(--text-secondary)}.pt-line small{color:var(--text-secondary)}.pt-tag{display:inline-block;min-width:80px;font-weight:600}.pt-sources{font-size:12px;color:var(--text-secondary)}.pt-retry{min-height:44px;min-width:44px}.pt-line a{display:inline-flex;align-items:center;min-height:44px}</style>`;

function line(e) {
  const href = /^https:\/\//.test(text(e.url)) ? text(e.url) : '';
  const label = `<span class="pt-tag">${esc(KIND_LABEL[e.kind] || e.kind)}</span> ${esc(text(e.text))}`;
  const why = e.rule ? ` <small>· via ${esc(RULE_TEXT[e.rule] || e.rule)}</small>` : '';
  const state = e.state && e.kind === 'handoff' ? ` <small>· ${esc(text(e.state))}</small>` : '';
  const body = href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
    : e.kind === 'handoff' && text(e.ref) ? `<a href="#/handoffs?open=${encodeURIComponent(text(e.ref))}" data-handoff-link>${label}</a>` : label;
  return `<li data-kind="${esc(text(e.kind))}" data-problem="${e.problem ? 'true' : 'false'}"><time>${esc(when(e.at))}</time>${body}${state}${why}</li>`;
}

export function renderTrail(data) {
  const m = trailModel(data);
  const head = `<p class="pt-note" data-pt-head><strong>${esc(m.pr.repository)} #${esc(text(m.pr.number))}</strong> · ${esc(m.pr.status)}${m.pr.branch ? ` · ${esc(m.pr.branch)}` : ''}${/^https:\/\//.test(m.pr.url) ? ` · <a href="${esc(m.pr.url)}" target="_blank" rel="noopener noreferrer">open on GitHub</a>` : ''}</p>`;
  const cards = `<div class="pt-sig" role="list" aria-label="Signals">${m.signals.map((c) => `<div class="pt-card" role="listitem" data-signal="${esc(c.key)}" data-known="${c.known}"><b>${esc(c.value)}</b><span>${esc(c.label)}${c.reason ? ` — ${esc(c.reason)}` : ''}</span></div>`).join('')}</div>`;
  const unread = Object.entries(m.sources).filter(([, v]) => v !== 'ok').map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`);
  const sources = unread.length ? `<p class="pt-sources" data-pt-sources>Not all records could be read — ${esc(unread.join(' · '))}. Figures that rest on them are shown as unknown.</p>` : '';
  const state = m.state === 'linked' ? '' : `<p class="pt-note" data-pt-state>${esc(m.reason || 'No agent is linked to this pull request.')}</p>`;
  const ambiguous = m.ambiguous.length ? `<p class="pt-note" data-pt-ambiguous>Not named, because they overlapped on the same branch: ${esc(m.ambiguous.map((a) => text(a.session_id)).join(', '))}.</p>` : '';
  const omitted = Object.entries(m.omitted).filter(([, n]) => Number.isInteger(n) && n > 0).map(([k, n]) => `${n} more ${k.replaceAll('_', ' ')}`);
  const more = omitted.length ? `<p class="pt-note" data-pt-omitted>Not listed: ${esc(omitted.join(', '))}.</p>` : '';
  const agents = m.sessions.length ? `<h2>Agents</h2><ul class="pt-line" aria-label="Agents on this pull request">${m.sessions.map((s) => `<li data-kind="session"><time>${esc(when(s.started_at))}</time><a href="#/herd/session/${encodeURIComponent(text(s.session_id))}">${esc(text(s.name) || text(s.role) || text(s.session_id))}</a> <small>${esc([text(s.runtime), text(s.model), text(s.host_id)].filter(Boolean).join(' · '))} · via ${esc(RULE_TEXT[s.rule] || text(s.rule))}</small></li>`).join('')}</ul>` : '';
  const hints = m.inferred.length ? `<h2>Possibly involved</h2><p class="pt-note" data-pt-inferred>These agents share this pull request's mission but nothing links them to its branch or commits. They are not counted in any figure above.</p><ul class="pt-line" aria-label="Agents that share the mission">${m.inferred.map((s) => `<li data-kind="session" data-inferred="true"><time>${esc(when(s.started_at))}</time><a href="#/herd/session/${encodeURIComponent(text(s.session_id))}">${esc(text(s.name) || text(s.role) || text(s.session_id))}</a> <small>inferred · same mission</small></li>`).join('')}</ul>` : '';
  const steps = m.timeline.length ? `<h2>In order</h2><ol class="pt-line" aria-label="What happened, in order">${m.timeline.map(line).join('')}</ol>` : '';
  return `${STYLE}${head}${cards}${sources}${state}${ambiguous}${more}${agents}${hints}${steps}`;
}

export function mountPrTrail(container, client, { owner, repo, number, host = '', freshness = () => null } = {}) {
  let disposed = false, controller = null, loaded = false, signature = null;
  container.innerHTML = '<div data-pt-body></div><p class="pt-note" data-pt-error hidden></p><button type="button" class="pt-retry" data-pt-retry hidden>Retry</button>';
  const body = container.querySelector('[data-pt-body]'), error = container.querySelector('[data-pt-error]'), retry = container.querySelector('[data-pt-retry]');
  const path = `/api/v1/pull-requests/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/${encodeURIComponent(number)}/trail${host ? `?host=${encodeURIComponent(host)}` : ''}`;
  const refresh = async () => {
    if (disposed || controller) return;
    const request = new AbortController(); controller = request;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(20000)]);
    if (!loaded) body.innerHTML = skeleton('pull request trail', { rows: 3, height: 64 });
    error.hidden = true; retry.hidden = true;
    try {
      const data = await client.request(path, { signal });
      if (disposed) return;
      if (!data || typeof data !== 'object' || Array.isArray(data) || data.error) throw new Error(text(data?.error) || 'Invalid trail response');
      const next = JSON.stringify(data);
      if (next !== signature) { body.innerHTML = renderTrail(data); signature = next; }
      loaded = true;
      freshness()?.touch(true);
    } catch (err) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) body.innerHTML = '';
        const message = err?.status === 404 ? 'No pull request with this number is recorded for this repository.' : text(err?.message) || 'request failed';
        error.textContent = `The pull request trail could not be loaded: ${message}`;
        error.hidden = false; retry.hidden = false;
        freshness()?.touch(false, message);
      }
    } finally { if (controller === request) controller = null; }
  };
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(refresh, 30000);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); } };
}
