// The mission page's story (missions rework design 2026-10-24): what it was for, what happened in order,
// how it ended, what needs attention now and where to go next. A pure render of GET /api/v1/missions/<key>/story;
// unknown values show a placeholder, never 0. Every string is untrusted and escaped; link targets are built by
// entity-links.js from structured refs, never from text.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration, formatTimestamp } from './herdr-web-components/format.js';
import { entityHref, forgePrUrl } from './entity-links.js';

const RESULT = {
  succeeded: ['Succeeded', 'ok'], failed: ['Failed', 'bad'], cancelled: ['Cancelled', 'warn'],
  unknown: ['Ended, outcome unknown', 'warn'], running: ['Running', 'live'],
};
const KIND_ICON = { start: '▶', stage: '↳', agent: '●', tests: '✓', review: '☑', pr: '⇄', ci: '⚙', wait: '⏸', end: '■' };
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const arr = (v) => (Array.isArray(v) ? v : []);
const show = (v, fallback = '—') => (v == null || v === '' ? fallback : esc(v));
const compact = (n) => (num(n) == null ? '—' : n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} k` : String(n));
const dur = (s) => (num(s) == null ? '—' : formatDuration(s * 1000));
const human = (v) => String(v ?? '').replaceAll('_', ' ');

export const storyStyles = `<style>.ms{margin:14px 0}.ms h2{font-size:16px;margin:22px 0 8px}.ms-head{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}
.ms-chip{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid var(--border);font-size:13px;font-weight:600}
.ms-chip[data-tone=ok]{color:var(--status-ok,#1a7f37)}.ms-chip[data-tone=bad]{color:var(--status-error,#cf222e)}.ms-chip[data-tone=warn]{color:var(--status-attention,#9a6700)}.ms-chip[data-tone=live]{color:var(--accent)}
.ms-sub{color:var(--text-secondary);font-size:13px;overflow-wrap:anywhere;margin:6px 0}.ms-needs{border:1px solid var(--status-attention,#9a6700);border-radius:var(--radius);padding:10px 14px}
.ms-needs ul,.ms-events,.ms-agents{list-style:none;margin:0;padding:0}.ms-needs li{margin:4px 0}.ms-events li{display:grid;grid-template-columns:auto 12ch minmax(0,1fr);gap:8px;padding:4px 0;border-bottom:1px solid var(--border)}
.ms-events time{color:var(--text-secondary);font-size:12px}.ms-events li>span:last-child{overflow-wrap:anywhere;min-width:0}.ms-agent{padding:8px 0;border-bottom:1px solid var(--border);overflow-wrap:anywhere}.ms-agent p{margin:2px 0;color:var(--text-secondary);font-size:13px}
.ms-grid{display:grid;grid-template-columns:minmax(80px,auto) minmax(0,1fr);gap:6px 14px;margin:0}.ms-grid dt{color:var(--text-secondary)}.ms-grid dd{margin:0;overflow-wrap:anywhere}
.ms-note{color:var(--text-secondary);font-size:13px}@media(max-width:600px){.ms-events li{grid-template-columns:auto minmax(0,1fr)}.ms-events time{grid-column:2}.ms-grid{display:block}.ms-grid dd{margin:2px 0 10px}}</style>`;

function refLink(ref, label) {
  if (!ref || typeof ref !== 'object') return esc(label);
  let href = null;
  if (ref.kind === 'ci') href = entityHref('ci', ref.run_number ?? ref.id, { repository: ref.repo_filter });
  else if (ref.kind === 'pr') {
    const url = forgePrUrl(ref.repository, ref.number);
    return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a> <span class="ms-note">external</span>` : esc(label);
  } else href = entityHref(ref.kind, ref.id);
  return href ? `<a href="${esc(href)}">${esc(label)}</a>` : esc(label);
}

function outcomeLine(summary = {}) {
  const parts = [];
  if (summary.pr) parts.push(`PR #${esc(summary.pr.number)} ${esc(human(summary.pr.state) || 'state not reported')}`);
  if (summary.ci) parts.push(`CI ${esc(({ success: 'passed', failure: 'failed' })[summary.ci] || human(summary.ci))}`);
  if (summary.tests) parts.push(`tests ${esc(summary.tests.passed)} passed${summary.tests.failed ? `, ${esc(summary.tests.failed)} failed` : ''}`);
  return parts.join(' · ');
}

function renderNeeds(needs) {
  if (!needs.length) return '';
  return `<section class="ms-needs" aria-label="Needs you now"><h2 style="margin-top:0">Needs you now</h2><ul>${needs.map((n) => {
    const href = typeof n.href === 'string' && n.href.startsWith('#/') ? n.href : null;
    return `<li><strong>${esc(n.title)}</strong> · ${esc(n.detail)}${href ? ` <a href="${esc(href)}">Open</a>` : ''}</li>`;
  }).join('')}</ul></section>`;
}

function renderEvents(events, more) {
  if (!events.length) return '<p class="ms-note">No events recorded for this mission yet.</p>';
  return `${more ? `<p class="ms-note">${esc(more)} earlier events are not shown; the raw event stream below has everything kept.</p>` : ''}<ol class="ms-events">${events.map((e) => {
    const text = e.kind === 'wait' && num(e.span_s) != null ? `${e.text} (${dur(e.span_s)})` : e.text;
    return `<li><span aria-hidden="true">${esc(KIND_ICON[e.kind] || '·')}</span><time datetime="${esc(e.ts)}">${esc(formatTimestamp(e.ts))}</time><span>${refLink(e.ref, text)}</span></li>`;
  }).join('')}</ol>`;
}

function renderAgents(agents, count) {
  if (agents == null) return '<p class="ms-note">Agents could not be read.</p>';
  if (!agents.length) return '<p class="ms-note">No agents on this mission.</p>';
  const more = num(count) != null && count > agents.length ? `<p class="ms-note">+${esc(count - agents.length)} more agents</p>` : '';
  return `<ul class="ms-agents">${agents.map((a) => {
    const span = a.started_at ? `${formatTimestamp(a.started_at)}${a.ended_at ? ` → ${formatTimestamp(a.ended_at)}` : ''}` : '';
    const open = entityHref('session', a.session_id), usage = entityHref('llm-session', a.session_id);
    return `<li class="ms-agent"><strong>${open ? `<a href="${esc(open)}">${show(a.label || a.role, 'Agent')}</a>` : show(a.label, 'Agent')}</strong> <span class="ms-note">${[a.role, a.model, a.outcome && human(a.outcome)].filter(Boolean).map(esc).join(' · ')}${span ? ` · ${esc(span)}` : ''}</span>
      ${a.summary ? `<p>${esc(a.summary)}</p>` : ''}${usage ? `<p><a href="${esc(usage)}">LLM usage</a></p>` : ''}</li>`;
  }).join('')}</ul>${more}`;
}

function renderOutcome(story) {
  const o = story.outcome || {};
  const unread = (name) => esc(`${name} could not be read`);
  const tests = o.tests ? esc(`${o.tests.runs} runs · ${o.tests.passed} passed · ${o.tests.failed} failed`) : unread('Tests');
  const ci = o.ci
    ? (o.ci.length ? o.ci.map((c) => `${show(c.repository)} ${show(c.branch, '')} run #${show(c.run)} ${show(human(c.state), 'state not reported')}`).join('; ') : 'No CI runs matched this mission')
    : unread('CI');
  const reviews = o.reviews ? esc(`${o.reviews.approved} approved · ${o.reviews.changes_requested} changes requested · ${o.reviews.open} open`) : unread('Reviews');
  const prs = o.prs
    ? (o.prs.length ? o.prs.map((p) => {
      const url = forgePrUrl(p.repository, p.number);
      const label = `${p.repository || 'PR'} #${p.number ?? '?'} ${human(p.state)}`;
      return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label);
    }).join('; ') : 'No pull requests')
    : unread('Pull requests');
  const u = story.usage;
  const usage = u
    ? esc(`${compact(u.requests)} requests · ${compact(u.input_tokens)} in · ${compact(u.output_tokens)} out${u.lower_bound ? ' (at least; older usage is no longer retained)' : ''}`)
    : unread('LLM usage');
  return `<dl class="ms-grid"><dt>Tests</dt><dd>${tests}</dd><dt>CI</dt><dd>${ci}</dd><dt>Reviews</dt><dd>${reviews}</dd><dt>Pull requests</dt><dd>${prs}</dd><dt>LLM usage</dt><dd>${usage}</dd></dl>`;
}

/** `story` is the API body; `related` is the markup for the shared Related panel slot. */
export function renderStory(story) {
  const h = story.header || {};
  const [label, tone] = RESULT[h.result] || RESULT.unknown;
  const where = [h.repository, h.branch, arr(h.hosts).length ? `host ${arr(h.hosts).join(', ')}` : null].filter(Boolean).map(esc).join(' · ');
  const bits = [dur(h.duration_s), `${num(h.agent_count) ?? '—'} agents`, num(h.tokens_out) == null ? null : `${compact(h.tokens_out)} output tokens`].filter(Boolean);
  const notes = [];
  if (h.inferred) notes.push('Closed automatically after a long silence; how it ended is not known.');
  if (h.recorded_before_outcomes) notes.push('Recorded before outcomes were kept; the figures below are read from what is stored now.');
  const partial = arr(story.partial).filter((p) => !p.startsWith('related:'));
  if (partial.length) notes.push(`Could not read: ${partial.join(', ')}. Missing figures are shown as unread, not zero.`);
  return `${storyStyles}<section class="ms" aria-label="Mission story">
    <div class="ms-head"><span class="ms-chip" data-tone="${tone}">${esc(label)}</span><span>${esc(bits.join(' · '))}</span></div>
    ${where ? `<p class="ms-sub">${where}</p>` : ''}${outcomeLine(h.summary) ? `<p class="ms-sub">${outcomeLine(h.summary)}</p>` : ''}
    ${story.objective ? `<p class="ms-sub">${esc(story.objective)}</p>` : ''}${notes.map((n) => `<p class="ms-note" role="status">${esc(n)}</p>`).join('')}
    ${renderNeeds(arr(story.needs))}
    <h2>What happened</h2>${renderEvents(arr(story.events), num(story.more))}
    <h2>Agents</h2>${renderAgents(story.agents, story.outcome?.agent_count)}
    <h2>Outcome and cost</h2>${renderOutcome(story)}
  </section>`;
}

const POLL_MS = 15000;
const signature = (story) => JSON.stringify([story.header, story.needs, story.events, story.agents, story.outcome, story.usage, story.partial]);

/** Mount into `el`. Resolves false when the story is unavailable so the caller keeps the plain page. Returns a disposer via `dispose`. */
export function mountMissionStory(el, client, key) {
  let live = true, timer = null, last = '';
  const controller = new AbortController();
  async function load(first) {
    try {
      const story = await client.missionStory(key, { signal: controller.signal });
      if (!live || !el.isConnected) return false;
      const sig = signature(story);
      if (sig !== last) { last = sig; el.innerHTML = renderStory(story); }
      if (story.header?.result === 'running' && live) timer = setTimeout(() => { if (!document.hidden) load(false); else timer = setTimeout(() => load(false), POLL_MS); }, POLL_MS);
      return true;
    } catch (error) {
      if (first && error?.name !== 'AbortError') el.replaceChildren();
      return false;
    }
  }
  const ready = load(true);
  return { ready, dispose() { live = false; clearTimeout(timer); controller.abort(); } };
}
