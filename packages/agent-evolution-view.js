// The complete life of one agent (GET /api/v1/sessions/<id>/evolution): the learning record first, then the phases in the order
// they happened (assigned, worked, handed off, produced, verified, outcome, cost). Every item says which rule linked it; a figure the
// server could not support is shown as "unknown" with its reason, never as zero or "no". A phase with no evidence is listed with the
// reason, it is not invented.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';
import { skeleton } from './motion.js';
import { pollWhileVisible } from './refresh-bus.js';
import { signalCards } from './pr-trail-view.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const when = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''; };
const https = (v) => (/^https:\/\//.test(text(v)) ? text(v) : '');
const sessionHref = (id) => (text(id) ? `#/herd/session/${encodeURIComponent(text(id))}` : '');

export const RULE_TEXT = {
  session_record: 'the session record', session_id: 'its session id', session_commits: 'its branch and commits', commit: 'the commit it reported',
  branch_window: 'its branch, while it worked', pull_request: 'its pull request number', tool_gap: 'tool calls less than ten minutes apart', event_stream: 'the recorded status changes',
  last_report: 'its last report', pr_trail: 'the pull request trail', usage_spend: 'recorded usage and the price table',
};

const CARDS = [
  ['outcome', 'Outcome', (r) => r.outcome, (v) => text(v)],
  ['duration_s', 'Time to finish', (r) => r.duration_s, (v) => formatDuration(Number(v) * 1000)],
  ['tool_calls_total', 'Tool calls', (r) => obj(r.tool_calls).total, (v) => String(v)],
  ['tool_calls_failed', 'Failed tool calls', (r) => obj(r.tool_calls).failed, (v) => String(v)],
  ['tool_calls_retried', 'Retried tool calls', (r) => obj(r.tool_calls).retried, (v) => String(v)],
  ['handoffs_in', 'Handoffs in', (r) => r.handoffs_in, (v) => String(v)],
  ['handoffs_out', 'Handoffs out', (r) => obj(r.handoffs_out).total, (v) => String(v)],
  ['handoffs_rejected', 'Handoffs rejected', (r) => obj(r.handoffs_out).rejected, (v) => String(v)],
  ['prs', 'Pull requests', (r) => r.prs, (v) => String(list(v).length)],
  ['cost_usd_estimate', 'Estimated cost', (r) => r.cost_usd_estimate, (v) => (Number(v) > 0 && Number(v) < 0.01 ? '<$0.01' : `$${Number(v).toFixed(2)}`)],
];

/** Learning-record cards: value, or "unknown" with the server's reason. */
export function recordCards(record = {}) {
  const r = obj(record);
  return CARDS.map(([key, label, pick, show]) => {
    const s = obj(pick(r));
    const known = s.value !== null && s.value !== undefined;
    return { key, label, known, value: known ? show(s.value) : 'unknown', reason: text(s.reason) };
  });
}

/** Everything the page needs, from the server's answer; tolerant of a missing or odd body. */
export function evolutionModel(data) {
  const d = obj(data);
  return {
    sources: obj(d.sources), phases: list(d.phases).filter((p) => p && typeof p === 'object').map((p) => ({
      key: text(p.key), title: text(p.title) || text(p.key), state: text(p.state) || 'unknown', reason: text(p.reason), items: list(p.items).filter((i) => i && typeof i === 'object'),
      omitted: Number.isInteger(p.omitted) ? p.omitted : 0, undated: obj(p.undated), unreadablePrs: !!p.pr_signals_unreadable })),
    cards: recordCards(d.learning_record), hints: obj(d.hints), derived: obj(d.derived), record: obj(d.learning_record), restarts: obj(d.restarts),
  };
}

const STYLE = `<style>.ev-sig{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin:12px 0}.ev-card{border:1px solid var(--border);border-radius:8px;padding:8px 12px}.ev-card b{display:block;font-size:16px}.ev-card span{font-size:12px;color:var(--text-secondary);overflow-wrap:anywhere}.ev-card[data-known=false] b{color:var(--text-secondary)}.ev-note{font-size:13px;color:var(--text-secondary);margin:8px 0;overflow-wrap:anywhere}.ev-phases{list-style:none;margin:16px 0;padding:0 0 0 16px;border-left:2px solid var(--border)}.ev-phase{position:relative;padding:0 0 16px 12px}.ev-phase::before{content:"";position:absolute;left:-21px;top:8px;width:8px;height:8px;border-radius:50%;background:var(--text-muted)}.ev-phase[data-state=recorded]::before{background:var(--accent)}.ev-phase h2{font-size:16px;margin:0 0 4px}.ev-phase h2 small{font-size:12px;font-weight:400;color:var(--text-secondary)}.ev-items{list-style:none;margin:8px 0;padding:0}.ev-items li{padding:4px 0 8px;font-size:13px;line-height:1.4;overflow-wrap:anywhere;border-top:1px solid var(--border-subtle)}.ev-items time{display:block;font-size:11px;color:var(--text-secondary)}.ev-items small{color:var(--text-secondary)}.ev-items li[data-problem=true]{border-left:4px solid var(--status-failed);padding-left:8px}.ev-tag{display:inline-block;min-width:80px;font-weight:600}.ev-chips{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0}.ev-chip{border:1px solid var(--border);border-radius:4px;padding:0 8px;font-size:12px}.ev-sub{margin:4px 0 0;padding-left:16px;font-size:12px;color:var(--text-secondary)}.ev-items a{display:inline-flex;align-items:center;min-height:44px}.ev-retry{min-height:44px;min-width:44px}.ev-raw pre{overflow:auto;max-width:100%;font-size:12px}.ev-raw summary{min-height:44px;display:flex;align-items:center}</style>`;

const chips = (items) => `<span class="ev-chips">${items.map((c) => `<span class="ev-chip">${esc(c)}</span>`).join('')}</span>`;
const why = (rule) => (rule ? ` <small>· via ${esc(RULE_TEXT[rule] || text(rule))}</small>` : '');
const sub = (label, lines) => (list(lines).length ? `<ul class="ev-sub" aria-label="${esc(label)}">${list(lines).map((l) => `<li>${esc(label)}: ${esc(text(l))}</li>`).join('')}</ul>` : '');

function agentLink(who) {
  const w = obj(who);
  const label = [text(w.harness), text(w.agent_id)].filter(Boolean).join(' ') || text(w.session_id) || 'unknown';
  const href = sessionHref(w.session_id);
  return href ? `<a href="${esc(href)}">${esc(label)}</a>` : esc(label);
}

/** One phase item as an <li>. Every string is escaped; links are in-app hash routes or https only. */
export function renderItem(item) {
  const i = obj(item), kind = text(i.kind);
  const stamp = (iso) => (iso ? `<time>${esc(when(iso))}</time>` : '');
  const li = (body, problem = false) => `<li data-kind="${esc(kind)}" data-problem="${problem ? 'true' : 'false'}">${body}</li>`;
  if (kind === 'session_record') {
    const bits = [i.name, i.role, i.runtime, i.model, i.host_id].map(text).filter(Boolean);
    const mission = text(i.mission_id) ? `<a href="#/missions/${esc(encodeURIComponent(text(i.mission_id)))}">mission ${esc(text(i.mission_id))}</a>` : '';
    const where = [text(i.repository), text(i.branch)].filter(Boolean).join(' · ');
    return li(`${stamp(i.at)}<span class="ev-tag">Started</span> ${chips(bits)}${where ? `<small>${esc(where)}</small> ` : ''}${mission}${why(i.rule)}`);
  }
  if (kind === 'handoff_in' || kind === 'handoff_out') {
    const accept = Number.isFinite(i.time_to_accept_s) && i.time_to_accept_s > 0 ? ` · accepted after ${esc(formatDuration(i.time_to_accept_s * 1000))}` : '';
    return li(`${stamp(i.at)}<span class="ev-tag">${kind === 'handoff_in' ? 'Handed to this agent' : 'Handed on'}</span> ${agentLink(i.from)} → ${agentLink(i.to)} · <strong>${esc(text(i.state))}</strong>${accept}`
      + `${text(i.goal) ? `<div>${esc(text(i.goal))}</div>` : ''}${sub('Done', i.completed)}${sub('Remaining', i.remaining)}${sub('Decided', i.decisions)}${why(i.rule)}`, !!i.problem);
  }
  if (kind === 'run') {
    const tools = list(i.tools).map((t) => `${text(obj(t).tool)} ×${Number(obj(t).calls) || 0}`);
    const idle = Number.isFinite(i.idle_before_s) ? ` · idle ${esc(formatDuration(i.idle_before_s * 1000))} before` : '';
    return li(`${stamp(i.started_at)}<span class="ev-tag">Tool run</span> ${esc(String(i.calls))} call${i.calls === 1 ? '' : 's'}, ${esc(String(i.failed))} failed${i.running ? `, ${esc(String(i.running))} unfinished` : ''}${idle}`
      + `${chips(tools)}${i.tools_omitted ? `<small>${esc(String(i.tools_omitted))} more tools</small> ` : ''}${why(i.rule)}`, i.failed > 0);
  }
  if (kind === 'status') {
    return li(`${stamp(i.at)}<span class="ev-tag">Status</span> ${esc(text(i.from) || 'start')} → <strong>${esc(text(i.to))}</strong>${i.restart ? ' <small>(restart, derived)</small>' : ''}${why(i.rule)}`, !!i.restart);
  }
  if (kind === 'commit') {
    const link = https(i.ci_url) ? `<a href="${esc(https(i.ci_url))}" target="_blank" rel="noopener noreferrer">run ${esc(text(i.run_number))}</a>` : `run ${esc(text(i.run_number))}`;
    return li(`${stamp(i.at)}<span class="ev-tag">Commit</span> ${esc(text(i.sha).slice(0, 8) || 'unknown')} · ${link}${text(i.branch) ? ` · ${esc(text(i.branch))}` : ''}${why(i.rule)}`);
  }
  if (kind === 'pull_request') {
    const trail = /^#\/pr\//.test(text(i.trail)) ? `<a href="${esc(text(i.trail))}">pull request #${esc(text(i.number))} · how it got here</a>` : `pull request #${esc(text(i.number))}`;
    const ext = https(i.url) ? ` · <a href="${esc(https(i.url))}" target="_blank" rel="noopener noreferrer">on the forge</a>` : '';
    return li(`${stamp(i.at)}<span class="ev-tag">Pull request</span> ${trail} · ${esc(text(i.status) || 'unknown')}${ext}${why(i.rule)}`);
  }
  if (kind === 'ci_run') {
    const link = https(i.ci_url) ? `<a href="${esc(https(i.ci_url))}" target="_blank" rel="noopener noreferrer">run ${esc(text(i.run_number))}</a>` : `run ${esc(text(i.run_number))}`;
    return li(`${stamp(i.at)}<span class="ev-tag">CI</span> ${link} · <strong>${esc(text(i.state) || 'unknown')}</strong>${why(i.rule)}`, /^(failed|failure|error|killed)$/i.test(text(i.state)));
  }
  if (kind === 'review') {
    return li(`${stamp(i.at)}<span class="ev-tag">Review</span> ${esc(text(i.review_kind) || 'code')} · ${esc(text(i.verdict) || text(i.status) || 'no verdict')}${text(i.reviewer) ? ` · ${esc(text(i.reviewer))}` : ''}${why(i.rule)}`, !!i.changes_requested);
  }
  if (kind === 'pull_request_outcome') {
    const cards = signalCards(i.signals).map((c) => `<div class="ev-card" data-signal="${esc(c.key)}" data-known="${c.known}"><b>${esc(c.value)}</b><span>${esc(c.label)}${c.reason ? ` — ${esc(c.reason)}` : ''}</span></div>`).join('');
    const own = i.trail_lists_this_agent === false ? '<p class="ev-note">The pull request trail does not list this agent, so these figures may describe other agents\' work.</p>' : '';
    return li(`<span class="ev-tag">${esc(text(i.ref))}</span> ${esc(text(i.status) || 'unknown')} <small>· covers every agent on that pull request</small>${own}<div class="ev-sig">${cards}</div>${why(i.rule)}`);
  }
  if (kind === 'cost') {
    return li(`<span class="ev-tag">Cost</span> ${Number(i.usd) > 0 && Number(i.usd) < 0.01 ? '&lt;$0.01' : esc(`$${Number(i.usd).toFixed(2)}`)} <small>${esc(text(i.note))}</small>${why(i.rule)}`);
  }
  return li(`${stamp(i.at)}<span class="ev-tag">${esc(kind || 'Item')}</span>${why(i.rule)}`);
}

function renderOutcomeStatus(item) {
  const i = obj(item);
  return `<li data-kind="status" data-problem="${/^failed$/.test(text(i.value)) ? 'true' : 'false'}"><time>${esc(when(i.at))}</time><span class="ev-tag">Last report</span> <strong>${esc(text(i.value) || 'unclassified')}</strong> <small>· reported status "${esc(text(i.last_reported_status))}"</small>${why(i.rule)}</li>`;
}

export function renderEvolution(data) {
  const m = evolutionModel(data);
  const cards = `<div class="ev-sig" role="list" aria-label="Learning record">${m.cards.map((c) => `<div class="ev-card" role="listitem" data-signal="${esc(c.key)}" data-known="${c.known}"><b>${esc(c.value)}</b><span>${esc(c.label)}${c.reason ? ` — ${esc(c.reason)}` : ''}</span></div>`).join('')}</div>`;
  const unread = Object.entries(m.sources).filter(([, v]) => v !== 'ok').map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`);
  const sources = unread.length ? `<p class="ev-note" data-ev-sources>Not all records could be read — ${esc(unread.join(' · '))}. Figures that rest on them are shown as unknown.</p>` : '';
  const phases = m.phases.map((p) => {
    const items = p.items.map((i) => (p.key === 'outcome' && i.kind === 'status' ? renderOutcomeStatus(i) : renderItem(i))).join('');
    const reason = p.reason ? `<p class="ev-note" data-ev-reason>${esc(p.reason)}</p>` : '';
    const more = p.omitted ? `<p class="ev-note" data-ev-omitted>${esc(String(p.omitted))} more not listed.</p>` : '';
    const undated = Object.entries(p.undated).filter(([, n]) => Number.isInteger(n) && n > 0).map(([k, n]) => `${n} ${k.replaceAll('_', ' ')}`);
    const dated = undated.length ? `<p class="ev-note" data-ev-undated>Not placed in time, because they carry no usable time: ${esc(undated.join(', '))}.</p>` : '';
    const label = p.state === 'recorded' ? '' : p.state === 'none' ? 'nothing recorded' : 'unknown';
    return `<li class="ev-phase" data-phase="${esc(p.key)}" data-state="${esc(p.state)}"><h2>${esc(p.title)}${label ? ` <small>· ${esc(label)}</small>` : ''}</h2>${reason}${dated}`
      + `${items ? `<ul class="ev-items" aria-label="${esc(p.title)}">${items}</ul>` : ''}${more}</li>`;
  }).join('');
  const hints = list(obj(m.hints).same_mission_sessions);
  const hint = hints.length ? `<p class="ev-note" data-ev-hints>Possibly related, because they share this agent's mission (not counted anywhere above): ${hints.map((h) => `<a href="${esc(sessionHref(obj(h).session_id))}">${esc(text(obj(h).name) || text(obj(h).session_id))}</a>`).join(', ')}.</p>` : '';
  const derived = Object.entries(m.derived).map(([k, v]) => `${k}: ${v}`);
  const foot = derived.length ? `<p class="ev-note" data-ev-derived>Derived, not reported — ${esc(derived.join(' · '))}</p>` : '';
  const raw = `<details class="ev-raw" data-ev-raw><summary>Learning record (as stored for later analysis)</summary><pre>${esc(JSON.stringify(m.record, null, 2))}</pre></details>`;
  return `${STYLE}${cards}${sources}<ol class="ev-phases" aria-label="The life of this agent, in order">${phases}</ol>${hint}${foot}${raw}`;
}

export function mountAgentEvolution(container, client, { sessionId, isActive = () => true } = {}) {
  let disposed = false, controller = null, loaded = false, signature = null;
  container.innerHTML = '<div class="ev-head"><herdr-freshness interval="30000"></herdr-freshness></div><div data-ev-body></div><p class="ev-note" data-ev-error hidden></p><button type="button" class="ev-retry" data-ev-retry hidden>Retry</button>';
  const body = container.querySelector('[data-ev-body]'), error = container.querySelector('[data-ev-error]'), retry = container.querySelector('[data-ev-retry]');
  const chip = container.querySelector('herdr-freshness');
  const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/evolution`;
  const refresh = async () => {
    if (disposed || controller) return;
    const request = new AbortController(); controller = request;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(20000)]);
    if (!loaded) body.innerHTML = skeleton('agent evolution', { rows: 4, height: 64 });
    error.hidden = true; retry.hidden = true;
    try {
      const data = await client.request(path, { signal });
      if (disposed) return;
      if (!data || typeof data !== 'object' || Array.isArray(data) || data.error) throw new Error(text(data?.error) || 'Invalid evolution response');
      const next = JSON.stringify(data);
      if (next !== signature) { body.innerHTML = renderEvolution(data); signature = next; }
      loaded = true;
      chip?.touch?.(true);
    } catch (err) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) body.innerHTML = '';
        const message = err?.status === 404 ? 'This agent is no longer recorded.' : text(err?.message) || 'request failed';
        error.textContent = `The agent's evolution could not be loaded: ${message}`;
        error.hidden = false; retry.hidden = false;
        chip?.touch?.(false, message);
      }
    } finally { if (controller === request) controller = null; }
  };
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(() => { if (isActive()) return refresh(); return undefined; }, 30000);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); } };
}

/** Tabs for the agent page: Overview (everything that was there) and Evolution (loaded the first time it is opened). */
export const AGENT_TABS = [['overview', 'Overview'], ['evolution', 'Evolution']];

export function tabsHtml(active) {
  return `<div class="ev-tabs" role="tablist" aria-label="Agent views" data-agent-tabs>${AGENT_TABS.map(([key, label]) => `<button type="button" role="tab" id="agent-tab-${key}" aria-controls="agent-panel-${key}" aria-selected="${key === active}" tabindex="${key === active ? 0 : -1}" data-agent-tab="${key}">${esc(label)}</button>`).join('')}</div>`;
}

export const TABS_STYLE = '<style>.ev-tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin:12px 0}.ev-tabs button{min-height:44px;min-width:44px;padding:0 16px;background:none;border:0;border-bottom:2px solid transparent;color:var(--text-secondary);font:inherit;cursor:pointer}.ev-tabs button[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent)}</style>';

/** Wire tab switching: click, arrow keys, Home/End; the hash query follows without re-rendering the page. */
export function wireTabs(root, { onSelect, setQuery } = {}) {
  const tabs = [...root.querySelectorAll('[data-agent-tab]')];
  const select = (key, focus = false) => {
    for (const t of tabs) {
      const on = t.dataset.agentTab === key;
      t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1;
      const panel = root.querySelector(`#agent-panel-${t.dataset.agentTab}`);
      if (panel) panel.hidden = !on;
      if (on && focus) t.focus();
    }
    setQuery?.(key);
    onSelect?.(key);
  };
  const keys = tabs.map((t) => t.dataset.agentTab);
  for (const t of tabs) {
    t.addEventListener('click', () => select(t.dataset.agentTab));
    t.addEventListener('keydown', (e) => {
      const at = keys.indexOf(t.dataset.agentTab);
      const next = e.key === 'ArrowRight' ? (at + 1) % keys.length : e.key === 'ArrowLeft' ? (at - 1 + keys.length) % keys.length : e.key === 'Home' ? 0 : e.key === 'End' ? keys.length - 1 : -1;
      if (next >= 0) { e.preventDefault(); select(keys[next], true); }
    });
  }
  return select;
}
