// Lineage of the agents that are working now: one lane per mission, one bar per agent from its start to its last report,
// and an arrow wherever a handoff moved work from one agent to another. Built from what the server reports; a lane with no
// handoff edge simply shows its agents side by side, so the chart never implies a transfer that was not recorded.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { prTrailHref } from './entity-links.js';

const MAX_LANES = 8;
const MIN_SPAN_MS = 30 * 60 * 1000;
const ms = value => { const t = Date.parse(value); return Number.isFinite(t) ? t : NaN; };
const text = value => typeof value === 'string' ? value.trim() : '';

// entries: output of sessionEntries() already narrowed to the agents to draw. edges: handoff tree edges (may be empty).
export function lineageModel(entries, edges = [], nodes = [], now = Date.now()) {
  const bySession = new Map(entries.map(e => [e.id, e]));
  const nodeSession = new Map((Array.isArray(nodes) ? nodes : []).map(n => [n.id, text(n.session_id)]));
  const lanes = new Map();
  for (const e of entries) {
    const start = ms(e.record.created_at), end = ms(e.record.updated_at);
    if (!Number.isFinite(start)) continue;
    const key = e.mission?.key || text(e.record.mission_key) || text(e.record.mission_id) || `host:${e.host || 'unknown'}`;
    const label = e.mission?.title || (text(e.record.mission_id) ? text(e.record.mission_id) : e.host || 'No mission');
    if (!lanes.has(key)) lanes.set(key, { key, label, agents: [], mission: !!(e.mission || text(e.record.mission_id)) });
    lanes.get(key).agents.push({ id: e.id, label: e.role, start, end: Number.isFinite(end) && end >= start ? end : now, tab: e.tab });
  }
  const ordered = [...lanes.values()].sort((a, b) => Math.max(...b.agents.map(x => x.end)) - Math.max(...a.agents.map(x => x.end)));
  const shown = ordered.slice(0, MAX_LANES);
  const all = shown.flatMap(l => l.agents);
  const t1 = Math.max(now, ...all.map(a => a.end));
  // At least half an hour is drawn, so agents that only just started are bars with room beside them rather than dots.
  const t0 = Math.min(all.length ? Math.min(...all.map(a => a.start)) : now, t1 - MIN_SPAN_MS);
  const handoffs = (Array.isArray(edges) ? edges : []).map(edge => ({
    from: nodeSession.get(edge.from) || edge.from, to: nodeSession.get(edge.to) || edge.to, state: edge.state || 'unknown', goal: text(edge.goal),
  })).filter(h => bySession.has(h.from) && bySession.has(h.to));
  return { lanes: shown, hidden: ordered.length - shown.length, t0, t1, handoffs, agents: all.length };
}

const TONE = { current: 'var(--status-active)', unconfirmed: 'var(--status-attention)', failed: 'var(--status-failed)', finished: 'var(--text-muted)' };

export function renderLineage(container, model) {
  if (!model.agents) { container.innerHTML = ''; return; }
  const W = 1000, LABEL = 170, ROW = 26, PAD = 8, top = 22;
  const span = Math.max(1, model.t1 - model.t0), plot = W - LABEL - 12;
  const x = t => LABEL + ((t - model.t0) / span) * plot;
  const H = top + model.lanes.length * ROW + PAD;
  const where = new Map();
  let y = top;
  const rows = model.lanes.map(lane => {
    const cy = y + ROW / 2; y += ROW;
    const bars = lane.agents.map((a, i) => {
      const bx = x(a.start), bw = Math.max(6, x(a.end) - bx);
      where.set(a.id, { x1: bx, x2: bx + bw, cy });
      return `<a href="#/herd/session/${esc(encodeURIComponent(a.id))}" data-lineage-agent="${esc(a.id)}"><title>${esc(a.label)}</title>` +
        `<rect x="${bx.toFixed(1)}" y="${cy - 7}" width="${Math.max(bw, 10).toFixed(1)}" height="14" rx="4" fill="${TONE[a.tab] || TONE.finished}" opacity=".85"/>` +
        (lane.agents.length <= 3 ? `<text x="${(bx + Math.max(bw, 10) + 6 > W - 110 ? bx - 6 : bx + Math.max(bw, 10) + 6).toFixed(1)}" y="${cy + 4}" text-anchor="${bx + Math.max(bw, 10) + 6 > W - 110 ? 'end' : 'start'}" class="al-bar">${esc(a.label.length > 22 ? `${a.label.slice(0, 21)}…` : a.label)}</text>` : '') + `</a>`;
    }).join('');
    return `<text x="${LABEL - 8}" y="${cy + 4}" text-anchor="end" class="al-lane">${esc(lane.label.length > 24 ? `${lane.label.slice(0, 23)}…` : lane.label)}</text>` +
      `<line x1="${LABEL}" x2="${W - 12}" y1="${cy}" y2="${cy}" class="al-guide"/>${bars}`;
  }).join('');
  const arrows = model.handoffs.map(h => {
    const a = where.get(h.from), b = where.get(h.to);
    if (!a || !b) return '';
    const mx = (a.x2 + b.x1) / 2;
    return `<path d="M${a.x2.toFixed(1)} ${a.cy} C${mx.toFixed(1)} ${a.cy} ${mx.toFixed(1)} ${b.cy} ${b.x1.toFixed(1)} ${b.cy}" class="al-handoff" data-handoff-state="${esc(h.state)}"><title>${esc(h.goal || 'Handoff')} · ${esc(h.state)}</title></path>`;
  }).join('');
  const ticks = [0, .25, .5, .75, 1].map(f => {
    const t = model.t0 + f * span, ago = Math.round((model.t1 - t) / 60000);
    return `<text x="${(LABEL + f * plot).toFixed(1)}" y="12" text-anchor="${f === 0 ? 'start' : f === 1 ? 'end' : 'middle'}" class="al-tick">${f === 1 ? 'now' : ago >= 120 ? `${Math.round(ago / 60)} h ago` : `${ago} min ago`}</text>`;
  }).join('');
  const more = model.hidden ? `<p class="hd-muted">${model.hidden} more mission${model.hidden === 1 ? '' : 's'} not drawn.</p>` : '';
  const note = model.handoffs.length ? '' : '<p class="hd-muted al-note">No handoff is recorded between these agents, so each lane shows them side by side.</p>';
  container.innerHTML = `<style>
.al-wrap{margin:8px 0 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px}
.al-wrap h2{font-size:13px;margin:0 0 4px}.al-wrap svg{width:100%;height:auto;display:block}
.al-lane{font-size:11px;fill:var(--text-secondary)}.al-tick{font-size:10px;fill:var(--text-muted)}
.al-bar{font-size:10px;fill:var(--text-secondary)}.al-guide{stroke:var(--border-subtle);stroke-width:1}.al-handoff{fill:none;stroke:var(--accent);stroke-width:1.6;marker-end:url(#al-arrow)}
.al-note{font-size:12px;margin:4px 0 0}</style>
<section class="al-wrap" aria-label="Agent lineage"><h2>Working now, by mission</h2>
<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${model.agents} working agents in ${model.lanes.length} lanes, ${model.handoffs.length} handoffs">
<defs><marker id="al-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" fill="var(--accent)"/></marker></defs>
${ticks}${rows}${arrows}</svg>${more}${note}</section>`;
}

// The story of one agent for its detail page: how it began, any handoff into or out of it, and where it stands now.
// links: the server's session->commit record (GET /sessions/<id>/commits) or null while it is unknown. Never guessed client-side.
export function commitSteps(links) {
  if (links === 'loading') return [{ kind: 'unlinked', at: NaN, text: 'Checking which commits this agent produced…' }];
  if (links === 'error') return [{ kind: 'unlinked', at: NaN, text: 'Commit links could not be loaded. Reopen this page to try again.' }];
  if (!links || typeof links !== 'object') return [];
  const list = Array.isArray(links.commits) ? links.commits : [];
  const pulls = (Array.isArray(links.pull_requests) ? links.pull_requests : []).filter(p => Number.isInteger(p?.number));
  if (links.state === 'linked' && (list.length || pulls.length)) {
    const shown = list.slice(0, 5).map(c => ({ kind: 'commit', at: ms(c.at), text: `Commit ${text(c.sha).slice(0, 7) || 'unknown'}${c.pull_request ? ` · PR #${c.pull_request}` : ''} · CI run #${c.run_number ?? '?'} ${text(c.state) || 'unknown'}${c.match === 'commit' ? ' (the commit the agent reported)' : ''}`, url: text(c.ci_url), trail: c.pull_request ? prTrailHref(text(links.repository_id), c.pull_request) : null }));
    const more = list.length - shown.length + (Number.isInteger(links.omitted) ? links.omitted : 0);
    if (more > 0) shown.push({ kind: 'commit', at: NaN, text: `${more} more commits not listed` });
    // A pull request already named on a commit line is not repeated.
    const named = new Set(list.map(c => c.pull_request).filter(Boolean));
    for (const p of pulls.filter(x => !named.has(x.number)).slice(0, 5)) shown.push({ kind: 'commit', at: ms(p.at), text: `PR #${p.number}${text(p.status) ? ` ${text(p.status)}` : ''}`, url: text(p.url), trail: prTrailHref(text(links.repository_id), p.number) });
    return shown;
  }
  return [{ kind: 'unlinked', at: NaN, text: `No commit link recorded: ${text(links.reason) || 'the server found no matching CI run.'}` }];
}

export function agentStory(session, tree = {}, now = Date.now(), links = null) {
  const id = text(session.session_id);
  const nodes = Array.isArray(tree.nodes) ? tree.nodes : [], edges = Array.isArray(tree.edges) ? tree.edges : [];
  const mine = new Set(nodes.filter(n => text(n.session_id) === id || n.id === id).map(n => n.id));
  const label = nodeId => { const n = nodes.find(x => x.id === nodeId) || {}; return text(n.agent_id) || text(n.role) || text(n.harness) || nodeId; };
  const steps = [];
  const created = ms(session.created_at);
  const where = [text(session.host_id), text(session.cwd).split(/[\\/]/).filter(Boolean).at(-1)].filter(Boolean).join(' · ');
  const incoming = edges.filter(e => mine.has(e.to));
  steps.push({ kind: 'start', at: created, text: incoming.length ? `Started by handoff from ${label(incoming[0].from)}${incoming[0].goal ? `: ${incoming[0].goal}` : ''}` : `Started${where ? ` on ${where}` : ''}`, handoff: !!incoming.length, handoffId: incoming.length ? text(incoming[0].handoff_id) : '' });
  for (const e of edges.filter(x => mine.has(x.from))) steps.push({ kind: 'handoff', at: ms(e.updated_at), text: `Handed ${e.state === 'completed' || e.state === 'accepted' ? 'off' : `off (${text(e.state) || 'unknown'})`} to ${label(e.to)}${e.goal ? `: ${e.goal}` : ''}`, handoff: true, handoffId: text(e.handoff_id) });
  const updated = ms(session.updated_at);
  // Lines without a time of their own (unlinked, "more commits") sit just before the last report rather than after it.
  steps.push(...commitSteps(links).map(c => Number.isFinite(c.at) ? c : { ...c, at: Number.isFinite(updated) ? updated - 1 : c.at }));
  steps.push({ kind: 'now', at: updated, text: text(session.summary) ? text(session.summary).replace(/\s+/g, ' ').slice(0, 140) : `Last report ${text(session.status) || 'unknown'}` });
  return { steps: steps.sort((a, b) => (Number.isFinite(a.at) ? a.at : Infinity) - (Number.isFinite(b.at) ? b.at : Infinity) || (a.kind === 'start' ? -1 : 0)), hasHandoff: steps.some(x => x.handoff), now };
}

export function renderStory(story) {
  const fmt = at => Number.isFinite(at) ? new Date(at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  return `<style>.as-story{list-style:none;margin:8px 0 14px;padding:0 0 0 14px;border-left:2px solid var(--border)}.as-story li{position:relative;padding:2px 0 8px 10px;font-size:13px;line-height:1.4;overflow-wrap:anywhere}.as-story li::before{content:"";position:absolute;left:-19px;top:7px;width:8px;height:8px;border-radius:50%;background:var(--text-muted)}.as-story li[data-kind=start]::before{background:var(--accent)}.as-story li[data-kind=now]::before{background:var(--status-ok,#2e9d5b)}.as-story li[data-kind=commit]::before{background:var(--accent)}.as-story li[data-kind=unlinked]{color:var(--text-secondary)}.as-story li[data-kind=unlinked]::before{background:transparent;border:1px solid var(--text-muted)}.as-story time{display:block;font-size:11px;color:var(--text-secondary)}</style>
<ol class="as-story" aria-label="How this agent started and proceeded">${story.steps.map(s => `<li data-kind="${esc(s.kind)}"><time>${esc(fmt(s.at))}</time>${/^https:\/\//.test(s.url || '') ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.text)}</a>` : s.handoffId ? `<a href="#/handoffs?open=${encodeURIComponent(s.handoffId)}" data-handoff-link>${esc(s.text)}</a>` : esc(s.text)}${s.trail ? ` <a href="${esc(s.trail)}" data-pr-trail>how it got here</a>` : ''}</li>`).join('')}</ol>`;
}
