// Agent Operations Activity Map (web): the animated map of who is working with whom. All geometry and meaning live in
// agent-ops-model.js; this file turns a scene into SVG/HTML strings (pure, runs under node) and mountAgentOps wires the live
// stream, the replay scrubber, filters and the inspector. Everything the picture says also exists as text (inspector + a visually
// hidden table), and colour is never the only signal.
//
// Gating: /api/v1/agent-ops/* answers 404 until control_plane.agent_ops.enabled; a 404 is shown as "not turned on", not as a failure.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { connectEvents } from './herdr-web-client/events.js';
import { entityHref } from './entity-links.js';
import {
  MODES, FILTER_KEYS, SPEEDS, EDGE_KINDS, KIND_ORDER, NODE_R, RING_R, RING_W, HALO_R, emptyFilters, filterOptions, layoutScene, diffSnapshots,
  describeNode, describeEdge, altTable, neighbour, buildTimeline, replayFrame, indexAtFraction, nextIndex, stepDelay, formatClock, shortId, roomLabel,
  contextInfo, stateInfo, edgeId, hostStrip, driftItems, TIMELINE_CAP,
} from './agent-ops-model.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);
const clip = (s, n) => { const t = text(s); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };
const num = (n) => (Number.isFinite(n) ? n : 0);

export const API = '/api/v1/agent-ops';
export const OPEN_PANE_EVENT = 'agent-ops:open-pane';

// ---- loading ------------------------------------------------------------------------------------------------------------------
const answer = async (call) => {
  try { return { status: 'ok', body: await call() }; } catch (error) {
    if (error?.status === 404) return { status: 'off' };
    // 400 = the server refused our cursor: the owner drops it and re-fetches the snapshot
    return { status: 'error', message: error?.message || 'request failed', ...(error?.status === 400 ? { reset: true } : {}) };
  }
};
export const loadSnapshot = (client, { signal } = {}) => answer(() => client.getJson(`${API}/snapshot`, { signal }));
export const loadReplay = (client, cursor, { signal } = {}) => answer(() => client.getJson(`${API}/replay?until=${encodeURIComponent(cursor)}`, { signal }));
/** Event history for the scrubber. The feed pages forward only, so walk it to its end but keep just the newest `cap` rows: the tail. */
export async function loadTimeline(client, { signal, pages = 40, cap = TIMELINE_CAP } = {}) {
  let cursor = '', out = [];
  for (let i = 0; i < pages; i += 1) {
    const r = await answer(() => client.getJson(`${API}/events?limit=500${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal }));
    if (r.status !== 'ok') return i === 0 ? r : { status: 'ok', body: { events: out } };
    out = out.concat(list(r.body?.events));
    if (out.length > cap) out = out.slice(out.length - cap);
    if (!r.body?.has_more || !r.body?.next_cursor) break;
    cursor = r.body.next_cursor;
  }
  return { status: 'ok', body: { events: out } };
}
export const streamUrl = (base, cursor) => `${base || ''}${API}/stream${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`;

// ---- static markup --------------------------------------------------------------------------------------------------------------
export function renderTabs(mode) {
  return `<div class="ao-tabs" role="tablist" aria-label="Map mode">${MODES.map((m) =>
    `<button type="button" role="tab" class="ao-tab" data-ao-mode="${esc(m.key)}" aria-selected="${m.key === mode}" tabindex="${m.key === mode ? 0 : -1}">${esc(m.label)}</button>`).join('')}</div>`;
}

const FILTER_LABEL = { room: 'Room', task: 'Task', host: 'Host', provider: 'Provider', model: 'Model', kind: 'Kind' };
export function renderFilters(options, filters) {
  const selects = FILTER_KEYS.map((k) => {
    const values = list(options?.[k]);
    const cur = text(filters?.[k]);
    const opts = [`<option value="">All</option>`, ...values.map((v) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(k === 'kind' ? EDGE_KINDS[v]?.label || v : k === 'room' ? shortId(v) : v)}</option>`)];
    if (cur && !values.includes(cur)) opts.push(`<option value="${esc(cur)}" selected>${esc(cur)}</option>`);
    return `<label class="ao-filter">${FILTER_LABEL[k]}<select data-ao-filter="${k}">${opts.join('')}</select></label>`;
  });
  const active = FILTER_KEYS.some((k) => filters?.[k]);
  return `<div class="ao-filters" role="group" aria-label="Filters">${selects.join('')}<button type="button" class="ao-btn ao-btn--quiet" data-ao-clear${active ? '' : ' disabled'}>Clear filters</button></div>`;
}

export function renderLegend() {
  const kinds = KIND_ORDER.map((k) => `<li class="ao-key ao-key--${EDGE_KINDS[k].tone}"><svg width="30" height="8" aria-hidden="true"><line x1="1" y1="4" x2="29" y2="4"/></svg>${esc(EDGE_KINDS[k].label)}</li>`).join('');
  return `<ul class="ao-legend" aria-label="Legend">${kinds}
    <li class="ao-key ao-key--line"><svg width="30" height="8" aria-hidden="true"><line x1="1" y1="4" x2="29" y2="4"/></svg>accepted (solid)</li>
    <li class="ao-key ao-key--line ao-key--planned"><svg width="30" height="8" aria-hidden="true"><line x1="1" y1="4" x2="29" y2="4" stroke-dasharray="6 4"/></svg>planned (dashed)</li>
    <li class="ao-key ao-key--line ao-key--stalled"><svg width="30" height="8" aria-hidden="true"><line x1="1" y1="4" x2="29" y2="4" stroke-dasharray="10 3 2 3"/></svg>stalled handoff (dash-dot)</li>
    <li class="ao-key ao-key--ring">outer ring = context used (amber 70%+, red 90%+, always labelled)</li></ul>`;
}

// ---- the map --------------------------------------------------------------------------------------------------------------------
const f1 = (n) => Math.round(num(n) * 10) / 10;

export function renderNode(n, { selected = false, pulse = false, tab = true } = {}) {
  const ring = n.ring, ctx = n.ctx;
  const level = n.person ? 'person' : ctx.level;
  const arc = n.person ? '' : ring.unknown
    ? `<circle class="ao-ring-unknown" r="${RING_R}" fill="none"/>`
    : `<circle class="ao-ring ao-ring--${level}${pulse ? ' ao-pulse' : ''}" r="${RING_R}" fill="none" stroke-width="${RING_W}" stroke-linecap="round" stroke-dasharray="${ring.dash} ${ring.gap}" transform="rotate(-90)"/>`;
  const track = n.person ? '' : `<circle class="ao-track" r="${RING_R}" fill="none" stroke-width="${RING_W}"/>`;
  const warn = !n.person && (ctx.level === 'critical' ? ' !' : '');
  const inner = n.person
    ? `<text class="ao-name" y="4" text-anchor="middle">${esc(clip(n.name, 12))}</text>`
    : `<text class="ao-name" y="-7" text-anchor="middle">${esc(clip(n.name, 12))}</text>
       <text class="ao-model${n.modelKnown ? '' : ' ao-model--unknown'}" y="9" text-anchor="middle">${esc(clip(n.model, 13))}</text>
       <text class="ao-ctx ao-ctx--${level}" y="25" text-anchor="middle">${esc(ctx.known && ctx.percent != null ? `${ctx.percent}% ctx` : ctx.label)}${warn}</text>`;
  return `<g class="ao-node ao-node--${n.state.tone}${n.person ? ' ao-node--person' : ''}${selected ? ' is-selected' : ''}" data-ao-node="${esc(n.id)}" transform="translate(${n.x} ${n.y})" tabindex="${tab ? 0 : -1}" role="button" aria-pressed="${selected}" aria-label="${esc(describeNode(n))}">
    <circle class="ao-halo ao-halo--${n.state.tone}" r="${HALO_R}"/>${track}${arc}<circle class="ao-body" r="${NODE_R}"/>${inner}
    <text class="ao-state" y="${RING_R + 22}" text-anchor="middle">${esc(n.state.label)}${!n.person && (ctx.level === 'warn' || ctx.level === 'critical') ? ` · ctx ${ctx.level === 'warn' ? 'high' : 'critical'}` : ''}</text></g>`;
}

const PARTICLES = 3;
export function renderEdge(e, scene, { selected = false, flowing = true } = {}) {
  const s = e.style, dash = s.dash ? ` stroke-dasharray="${s.dash}"` : '';
  const live = flowing && s.flowing && !e.dimmed;
  const particles = live ? Array.from({ length: PARTICLES }, (_, i) =>
    `<circle class="ao-particle" r="4" style="offset-path:path('${e.d}');animation-delay:${f1(-(i * 3.2) / PARTICLES)}s"/>`).join('') : '';
  const label = e.label;
  return `<g class="ao-edge ao-edge--${s.tone} ao-edge--${s.line}${e.dimmed ? ' is-dim' : ''}${selected ? ' is-selected' : ''}" data-ao-edge="${esc(e.id)}" tabindex="0" role="button" aria-pressed="${selected}" aria-label="${esc(describeEdge(e, scene))}">
    <path class="ao-edge-hit" d="${e.d}" fill="none"/><path class="ao-edge-line" d="${e.d}" fill="none"${dash} marker-end="url(#ao-arrow-${s.tone})"/>${particles}
    <text class="ao-edge-label${e.labelQuiet ? ' ao-edge-label--quiet' : ''}" x="${(e.labelAt || e.mid).x}" y="${(e.labelAt || e.mid).y - 6}" text-anchor="middle">${esc(label)}</text></g>`;
}

export function renderMarkers() {
  return `<defs>${KIND_ORDER.map((k) => `<marker id="ao-arrow-${EDGE_KINDS[k].tone}" class="ao-arrow ao-arrow--${EDGE_KINDS[k].tone}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z"/></marker>`).join('')}</defs>`;
}

export function renderScene(scene, { selected = null, pulses = new Set(), flowing = true, label = 'Agent activity map', tabId = null } = {}) {
  const vb = scene.viewBox;
  const rooms = scene.rooms.map((r) => `<g class="ao-room" aria-hidden="true"><circle cx="${r.cx}" cy="${r.cy}" r="${r.r}"/><text x="${r.cx}" y="${f1(r.cy - r.r + 22)}" text-anchor="middle">${esc(r.label)}${r.ctx?.known && r.ctx.percent != null ? ` · avg ${r.ctx.percent}% ctx` : ''}</text></g>`).join('');
  const edges = scene.edges.map((e) => renderEdge(e, scene, { selected: selected?.type === 'edge' && selected.id === e.id, flowing })).join('');
  // Roving tabindex: one node is the tab stop (the chosen one, else the first); arrows move between the rest.
  const stop = scene.nodes.some((n) => n.id === tabId) ? tabId : scene.nodes[0]?.id;
  const nodes = scene.nodes.map((n) => renderNode(n, { selected: selected?.type === 'node' && selected.id === n.id, pulse: pulses.has(n.id), tab: n.id === stop })).join('');
  return `<svg class="ao-svg" viewBox="${vb.x} ${vb.y} ${vb.w} ${vb.h}" role="group" aria-label="${esc(label)}" aria-describedby="ao-alt-desc" preserveAspectRatio="xMidYMid meet" data-ao-svg>${renderMarkers()}<g class="ao-rooms">${rooms}</g><g class="ao-edges">${edges}</g><g class="ao-nodes">${nodes}</g></svg>`;
}

export function renderEmpty(snapshot, filtered) {
  if (filtered) return '<div class="ao-empty"><h2>Nothing matches these filters</h2><p>Clear a filter to see the rest of the map.</p></div>';
  return '<div class="ao-empty"><h2>No agent activity yet</h2><p>Agents appear here once they report events to the control plane (POST /api/v1/ingest). Nothing is drawn that was not reported.</p></div>';
}

/** The picture as a table: every node and edge, for screen readers (visually hidden, still in the accessibility tree). */
export function renderAltTable(scene) {
  const t = altTable(scene);
  const nodes = t.nodes.map((n) => `<tr><th scope="row">${esc(n.name)}</th><td>${esc(n.model)}</td><td>${esc(n.context)}</td><td>${esc(n.state)}</td><td>${esc(n.room)}</td><td>${esc(n.host)}</td></tr>`).join('');
  const edges = t.edges.map((e) => `<tr><th scope="row">${esc(e.kind)}</th><td>${esc(e.from)}</td><td>${esc(e.to)}</td><td>${esc(e.state)}</td><td>${esc(e.line)}</td><td>${esc(e.task)}</td></tr>`).join('');
  return `<div class="ao-sr" data-ao-alt><p id="ao-alt-desc">${scene.counts.nodes} agents, ${scene.counts.edges} relationships, ${scene.counts.rooms} rooms. Select an agent or a relationship for details.</p>
    <table><caption>Agents</caption><thead><tr><th>Name</th><th>Model</th><th>Context</th><th>State</th><th>Room</th><th>Host</th></tr></thead><tbody>${nodes}</tbody></table>
    <table><caption>Relationships</caption><thead><tr><th>Kind</th><th>From</th><th>To</th><th>State</th><th>Line</th><th>Task</th></tr></thead><tbody>${edges}</tbody></table></div>`;
}

// ---- fleet hosts ------------------------------------------------------------------------------------------------------------------
export function renderHostStrip(snapshot) {
  const rows = hostStrip(snapshot);
  if (!rows.length) return '';
  const fleet = contextInfo(null, snapshot?.health?.context && Number.isFinite(snapshot.health.context.average_fraction) ? { measured: true, fraction: snapshot.health.context.average_fraction } : null);
  const chips = rows.map((h) => `<li class="ao-host ao-host--${h.state}" data-ao-host="${esc(h.id)}"><span class="ao-host-id">${esc(h.id)}</span>
    <span class="ao-host-count">${h.agents} ${h.agents === 1 ? 'agent' : 'agents'}</span><span class="ao-host-state">${esc(h.state)}</span>
    <span class="ao-badge ao-badge--${h.ctx.level}">${esc(h.ctx.known ? `avg ${h.ctx.label}` : h.ctx.label)}</span></li>`).join('');
  return `<ul class="ao-hosts" aria-label="Fleet hosts">${chips}<li class="ao-host ao-host--fleet"><span class="ao-host-id">Fleet</span><span class="ao-badge ao-badge--${fleet.level}">${esc(fleet.known ? `avg ${fleet.label}` : fleet.label)}</span></li></ul>`;
}

/** Drift signals: always labelled as heuristics, each with where it came from and the events that triggered it. */
export function renderDrift(items) {
  if (!items.length) return '';
  const rows = items.map((d) => `<li class="ao-drift-item"><strong>${esc(d.title)}</strong> <span class="ao-chip ao-chip--static">${esc(d.provenance)}</span>
    ${d.task ? `<span class="ao-note">task ${esc(d.task)}</span>` : ''}${d.evidence.length ? `<span class="ao-note">evidence: ${esc(d.evidence.map(shortId).join(', '))}</span>` : ''}</li>`).join('');
  return `<section class="ao-drift" aria-label="Drift signals"><h4>Drift signals (heuristic)</h4><ul class="ao-drift-list">${rows}</ul>
    <p class="ao-note">Heuristic signals from event metadata (derived or inferred, as labelled). They are not proof of a policy violation.</p></section>`;
}

const EDGE_STATE_NOTE = {
  stalled: 'This handoff stalled: an endpoint disconnected before it finished. It stays stalled until a new handoff event arrives.',
  failed: 'This handoff failed: an endpoint session ended before it finished.',
};

// ---- inspector --------------------------------------------------------------------------------------------------------------------
const dl = (rows) => `<dl class="ao-dl">${rows.filter((r) => r[1] !== '' && r[1] != null).map(([k, v, raw]) => `<dt>${esc(k)}</dt><dd>${raw ? v : esc(v)}</dd>`).join('')}</dl>`;
const nameOf = (scene, id) => scene.nodes.find((n) => n.id === id)?.name || shortId(id);
const taskButtons = (ids) => ids.map((t) => `<button type="button" class="ao-chip" data-ao-task="${esc(t)}">${esc(t)}</button>`).join(' ');

export function renderTask(task, scene) {
  const who = (id) => (id ? `<button type="button" class="ao-linkbtn" data-ao-node-link="${esc(id)}">${esc(nameOf(scene, id))}</button>` : '');
  const contributors = list(task.contributors).map(who).join(', ');
  return `<h3>Task ${esc(task.task_id)}</h3><p class="ao-note">Delegating never moves accountability: the owner below stays accountable.</p>
    ${dl([['Accountable owner', who(task.accountable_owner) || 'not assigned', Boolean(task.accountable_owner)], ['Contributors', contributors || 'none reported', Boolean(contributors)],
      ['Reviewer', who(task.reviewer) || 'none reported', Boolean(task.reviewer)], ['Room', task.room_id ? roomLabel(task.room_id) : ''], ['Updated', task.updated_at]])}${renderDrift(driftItems(task))}`;
}

export function renderInspector(sel, scene, snapshot) {
  const head = '<h2 class="ao-inspector-title">Inspector</h2>';
  if (sel?.type === 'node') {
    const n = scene.nodes.find((x) => x.id === sel.id);
    if (!n) return `${head}<p class="ao-note">That agent is not in the current view.</p>`;
    const a = n.agent, link = a.session_id ? entityHref('session', a.session_id) : null;
    const changes = list(a.model_changes).map((c) => `${c.from || 'unset'} → ${c.to || 'unset'}`).join('; ');
    const tasks = [...new Set(n.roles.map((r) => r.task))];
    return `${head}<h3>${esc(n.name)}</h3>${dl([
      ['Kind', n.person ? 'Person' : 'Agent'], ['Model', n.person ? '' : n.modelKnown ? n.model : 'model unknown'], ['Provider', n.provider || (n.person ? '' : 'unknown')],
      ['Context', n.person ? '' : n.ctx.words], ['State', n.state.label], ['Host', n.host || (n.person ? '' : 'unknown')], ['Room', roomLabel(n.room)],
      ['Roles', n.roles.map((r) => `${r.role} of ${r.task}`).join(', ')], ['Model changes', changes], ['Compactions', n.compactions ? String(n.compactions) : ''],
      ['Drift warnings reported', n.drift ? String(n.drift) : ''], ['Provenance', n.provenance], ['Last event', n.lastEventAt || ''], ['Session', a.session_id ? shortId(a.session_id) : ''],
    ])}${tasks.length ? `<p class="ao-sub">Tasks ${taskButtons(tasks)}</p>` : ''}
      ${renderDrift(n.driftItems || [])}
      <div class="ao-actions">${n.person ? '' : `<button type="button" class="ao-btn" data-ao-open-pane="${esc(n.id)}">Open pane</button>`}${link ? `<a class="ao-btn" href="${esc(link)}">Open session</a>` : ''}</div>
      ${n.person ? '' : '<p class="ao-note">Double-click a node (or press O) to open its pane.</p>'}`;
  }
  if (sel?.type === 'edge') {
    const e = scene.edges.find((x) => x.id === sel.id);
    if (!e) return `${head}<p class="ao-note">That relationship is not in the current view.</p>`;
    return `${head}<h3>${esc(e.style.kindLabel)}</h3><p class="ao-note">${esc(EDGE_KINDS[e.kind].meaning)}. Metadata only: no message content is stored or shown.</p>${dl([
      ['From', nameOf(scene, e.from)], ['To', nameOf(scene, e.to)], ['State', e.style.state], ['Line', e.style.line], ['Task', e.task || ''], ['Handoff', e.handoff || ''],
      ['Room', e.room ? roomLabel(e.room) : ''], ['Provenance', e.provenance], ['Updated', e.updatedAt || ''], ['Event', e.eventId || ''], ['Interrupted by', e.raw?.interrupted_by || ''],
    ])}${EDGE_STATE_NOTE[e.state] ? `<p class="ao-note ao-note--alert">${esc(EDGE_STATE_NOTE[e.state])}</p>` : ''}${e.task ? `<p class="ao-sub">Task ${taskButtons([e.task])}</p>` : ''}`;
  }
  if (sel?.type === 'task') {
    const t = scene.tasks.find((x) => x.task_id === sel.id) || list(snapshot?.tasks).find((x) => x.task_id === sel.id);
    return t ? `${head}${renderTask(t, scene)}` : `${head}<p class="ao-note">That task is not in the current view.</p>`;
  }
  const tasks = list(scene.tasks);
  return `${head}<p class="ao-note">Select an agent or a line. Click a line to read its event; click a node for model, context and roles.</p>
    <h3>Tasks</h3>${tasks.length ? `<ul class="ao-tasks">${tasks.map((t) => `<li><button type="button" class="ao-linkbtn" data-ao-task="${esc(t.task_id)}">${esc(t.task_id)}</button> <span class="ao-note">owner ${esc(t.accountable_owner ? nameOf(scene, t.accountable_owner) : 'not assigned')}</span></li>`).join('')}</ul>` : '<p class="ao-note">No tasks reported.</p>'}`;
}

// ---- timeline ---------------------------------------------------------------------------------------------------------------------
export function renderTimeline(state) {
  const frame = replayFrame(state.timeline, state.index);
  const max = Math.max(0, state.timeline.length - 1);
  const value = frame.live ? max : frame.index;
  const where = frame.live ? 'Live' : `${frame.index + 1} / ${frame.count}`;
  const speeds = SPEEDS.map((s) => `<option value="${s}"${s === state.speed ? ' selected' : ''}>${s}x</option>`).join('');
  return `<div class="ao-timeline-row">
    <button type="button" class="ao-btn" data-ao-play aria-pressed="${state.playing}"${state.timeline.length ? '' : ' disabled'}>${state.playing ? 'Pause' : 'Play'}</button>
    <label class="ao-filter ao-speed">Speed<select data-ao-speed>${speeds}</select></label>
    <input class="ao-scrub" type="range" min="0" max="${max}" value="${value}" step="1" data-ao-scrub aria-label="Execution timeline"${state.timeline.length ? '' : ' disabled'} aria-valuetext="${esc(frame.entry ? `${frame.entry.label}, ${where}` : 'no events yet')}">
    <button type="button" class="ao-btn ao-live${frame.live ? ' is-live' : ''}" data-ao-live aria-pressed="${frame.live}"><span class="ao-live-dot" aria-hidden="true"></span>Live</button></div>
    <p class="ao-timeline-now" data-ao-now>${esc(frame.entry ? `${formatClock(frame.at)} UTC · ${frame.entry.label} · ${where}` : 'No events yet.')}</p>`;
}

export function renderBanner(conn) {
  if (conn === 'live' || conn === 'idle') return '';
  return conn === 'offline'
    ? '<strong>Live updates are paused.</strong> Showing the last known map; reconnecting and resuming from the last event.'
    : '<strong>Connecting to the live stream.</strong>';
}

// ---- mount -------------------------------------------------------------------------------------------------------------------------
const calmNow = () => {
  if (typeof document === 'undefined') return false;
  return document.documentElement?.dataset?.calm === 'on' || Boolean(globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
};

export function mountAgentOps(container, client, opts = {}) {
  const { freshness = () => null, connect = connectEvents, createEventSource, open = (detail) => container.dispatchEvent(new CustomEvent(OPEN_PANE_EVENT, { bubbles: true, detail })) } = opts;
  const state = { phase: 'loading', message: '', live: null, replay: null, mode: opts.mode || 'activity', filters: { ...emptyFilters(), ...(opts.filters || {}) }, selected: null,
    timeline: [], index: null, playing: false, speed: 1, conn: 'idle', pulses: new Set(), scene: null, sig: '', tabNode: null, inspHtml: '', hostHtml: '', streamCursor: '' };
  let disposed = false, controller = null, replayCtl = null, playTimer = null, refreshTimer = null, stop = null;

  container.innerHTML = `<section class="ao" data-agent-ops data-ao-phase="loading">
    <div class="ao-bar"><div data-ao-tabs></div><div data-ao-filters></div></div>
    <div class="ao-banner" data-ao-banner role="status" hidden></div>
    <div data-ao-hosts></div>
    <div class="ao-main"><div class="ao-stage" data-ao-stage></div><aside class="ao-inspector" data-ao-inspector aria-label="Inspector" aria-live="polite"></aside></div>
    <div class="ao-timeline" data-ao-timeline></div><div data-ao-alt-host></div></section>`;
  const $ = (s) => container.querySelector(s);
  const root = $('[data-agent-ops]'), stageEl = $('[data-ao-stage]'), inspEl = $('[data-ao-inspector]'), tlEl = $('[data-ao-timeline]');

  // The inspector is an aria-live region: write it only when its content really changed, so a stream of unrelated events stays silent.
  const setInspector = (html) => { if (html !== state.inspHtml) { state.inspHtml = html; inspEl.innerHTML = html; } };
  const setHosts = (html) => { if (html !== state.hostHtml) { state.hostHtml = html; $('[data-ao-hosts]').innerHTML = html; } };
  const viewSnap = () => (state.index == null ? state.live : state.replay || state.live);
  const flowing = () => !calmNow();

  function paintBar() {
    $('[data-ao-tabs]').innerHTML = renderTabs(state.mode);
    $('[data-ao-filters]').innerHTML = renderFilters(state.live ? filterOptions(state.live) : {}, state.filters);
  }
  function paintBanner() {
    const el = $('[data-ao-banner]'), html = renderBanner(state.conn);
    el.hidden = !html; el.innerHTML = html; el.dataset.conn = state.conn;
  }
  function paintStage({ force = false } = {}) {
    root.dataset.aoPhase = state.phase; root.dataset.aoMode = state.mode; root.classList.toggle('ao--replay', state.index != null || state.mode === 'replay');
    if (state.phase !== 'ok') {
      state.sig = '';
      stageEl.innerHTML = state.phase === 'loading' ? '<div class="ao-state" role="status">Loading agent activity…</div>'
        : state.phase === 'off' ? '<div class="ao-state" role="status"><h2>Agent Operations is not turned on</h2><p>An administrator can enable control_plane.agent_ops in the server configuration.</p></div>'
          : `<div class="ao-state" role="alert"><h2>The map could not be loaded</h2><p>${esc(state.message)}</p><button type="button" class="ao-btn" data-ao-retry>Try again</button></div>`;
      setInspector(''); setHosts('');
      return;
    }
    const snap = viewSnap();
    const scene = layoutScene(snap, { mode: state.mode, filters: state.filters });
    state.scene = scene;
    const sig = JSON.stringify([scene.nodes.map((n) => [n.id, n.x, n.y, n.ctx.label, n.state.key, n.model, n.name, n.roles.length]), scene.edges.map((e) => [e.id, e.d, e.state, e.dimmed]),
      scene.rooms.map((r) => [r.id, r.cx, r.cy, r.r]), state.selected, flowing(), [...state.pulses]]);
    if (force || sig !== state.sig) {
      const active = container.querySelector(':focus');
      const focusKey = active?.dataset?.aoNode ? ['aoNode', active.dataset.aoNode] : active?.dataset?.aoEdge ? ['aoEdge', active.dataset.aoEdge] : null;
      state.sig = sig;
      const filtered = FILTER_KEYS.some((k) => state.filters[k]);
      stageEl.innerHTML = scene.nodes.length
        ? `<div class="ao-canvas">${renderScene(scene, { selected: state.selected, pulses: state.pulses, flowing: flowing(), tabId: state.tabNode })}</div>${renderLegend()}${scene.truncated ? '<p class="ao-note">The server capped this view; some agents are not drawn.</p>' : ''}`
        : renderEmpty(snap, filtered);
      $('[data-ao-alt-host]').innerHTML = renderAltTable(scene);
      if (focusKey) stageEl.querySelector(`[data-${focusKey[0] === 'aoNode' ? 'ao-node' : 'ao-edge'}="${CSS.escape(focusKey[1])}"]`)?.focus({ preventScroll: true });
      state.pulses = new Set();
    }
    setInspector(renderInspector(state.selected, scene, snap));
    setHosts(renderHostStrip(snap));
  }
  function paintTimeline() {
    const hadFocus = tlEl.contains(container.querySelector(':focus')) ? container.querySelector(':focus')?.getAttribute('data-ao-scrub') != null : false;
    tlEl.innerHTML = renderTimeline(state);
    if (hadFocus) tlEl.querySelector('[data-ao-scrub]')?.focus({ preventScroll: true });
  }
  const paintAll = () => { paintBar(); paintBanner(); paintStage({ force: true }); paintTimeline(); };

  // ---- data -----------------------------------------------------------------------------------------------------------------------
  function setLive(next) {
    const d = state.live ? diffSnapshots(state.live, next) : null;
    if (d && state.index == null) state.pulses = new Set(d.modelChanged);
    state.live = next;
  }
  async function load() {
    if (disposed) return;
    controller?.abort(); const ctl = controller = new AbortController();
    const signal = AbortSignal.any([ctl.signal, AbortSignal.timeout(20000)]);
    const snap = await loadSnapshot(client, { signal });
    if (disposed || ctl.signal.aborted) return;
    if (snap.status === 'ok') {
      state.phase = 'ok'; setLive(snap.body); freshness()?.touch(true);
      paintBar(); paintStage(); paintBanner();
      const tl = await loadTimeline(client, { signal });
      if (disposed || ctl.signal.aborted) return;
      if (tl.status === 'ok') state.timeline = buildTimeline(tl.body.events, state.timeline);
      // The stream resumes from the snapshot's own cursor: the snapshot already contains everything up to it.
      if (snap.body?.cursor && snap.body.cursor > state.streamCursor) state.streamCursor = snap.body.cursor;
      paintTimeline(); startStream();
    } else {
      state.phase = snap.status; state.message = snap.message || '';
      if (snap.status === 'off') freshness()?.touch(true); else freshness()?.touch(false, snap.message || 'error');
      paintAll();
    }
  }
  function scheduleRefresh() {
    if (refreshTimer || disposed) return;
    refreshTimer = setTimeout(async () => {
      refreshTimer = null;
      const r = await loadSnapshot(client, { signal: AbortSignal.timeout(15000) });
      if (disposed || r.status !== 'ok') return;
      setLive(r.body); if (state.index == null) { paintBar(); paintStage(); }
    }, 350);
  }
  const lastCursor = () => state.streamCursor || state.timeline.at(-1)?.cursor || state.live?.cursor || '';
  function startStream() {
    if (stop || disposed) return;
    const mk = createEventSource || ((u) => new EventSource(u));
    stop = connect({
      url: streamUrl(client.base, ''), autoReconnectMs: opts.reconnectMs ?? 3000,
      // Every (re)connect resumes from the last event we hold, so a gap never loses an event.
      createEventSource: () => mk(streamUrl(client.base, lastCursor())),
      onContact: () => { if (state.conn !== 'live') { state.conn = 'live'; paintBanner(); } freshness()?.touch(true); },
      onError: () => { state.conn = 'offline'; paintBanner(); freshness()?.touch(false, 'agent-ops stream disconnected'); },
      onEvent: (ev) => {
        state.timeline = buildTimeline([ev], state.timeline);
        if (typeof ev?.cursor === 'string' && ev.cursor > state.streamCursor) state.streamCursor = ev.cursor;
        if (state.index == null) paintTimeline(); else updateScrubMax();
        scheduleRefresh();
      },
    });
  }
  function updateScrubMax() { const s = tlEl.querySelector('[data-ao-scrub]'); if (s) { s.max = String(Math.max(0, state.timeline.length - 1)); s.disabled = !state.timeline.length; } }

  // ---- replay ----------------------------------------------------------------------------------------------------------------------
  async function seek(index) {
    replayCtl?.abort();
    if (index == null) { state.index = null; state.replay = null; stopPlaying(false); paintBar(); paintStage(); paintTimeline(); return; }
    const frame = replayFrame(state.timeline, index);
    if (frame.live) return;
    state.index = frame.index;
    const ctl = replayCtl = new AbortController();
    const r = await loadReplay(client, frame.cursor, { signal: ctl.signal });
    if (disposed || ctl.signal.aborted) return;
    if (r.status === 'ok') {
      state.pulses = new Set(diffSnapshots(viewSnap(), r.body).modelChanged); state.replay = r.body;
      state.conn = state.conn === 'offline' ? 'offline' : state.conn;
    } else if (r.reset) { state.streamCursor = ''; state.index = null; state.replay = null; load(); return; }
    else { state.message = r.message || ''; }
    paintStage(); paintTimeline(); schedulePlay();
  }
  function stopPlaying(repaint = true) { state.playing = false; clearTimeout(playTimer); playTimer = null; if (repaint) paintTimeline(); }
  function schedulePlay() {
    clearTimeout(playTimer);
    if (!state.playing || disposed) return;
    playTimer = setTimeout(() => {
      const n = nextIndex(state.timeline, state.index ?? -1);
      if (n == null) { void seek(null); return; }
      void seek(n);
    }, stepDelay(state.timeline, state.index ?? 0, state.speed));
  }
  function togglePlay() {
    if (state.playing) { stopPlaying(); return; }
    state.playing = true;
    if (state.index == null) { state.mode = 'replay'; void seek(0); paintBar(); } else schedulePlay();
    paintTimeline();
  }

  // ---- interactions -----------------------------------------------------------------------------------------------------------------
  // Selecting must not rebuild the map: a double-click needs the same element under both clicks, and focus must stay put.
  const select = (sel) => {
    state.selected = sel;
    for (const el of stageEl.querySelectorAll('[data-ao-node],[data-ao-edge]')) {
      const on = Boolean(sel) && (sel.type === 'node' ? el.dataset.aoNode === sel.id : sel.type === 'edge' && el.dataset.aoEdge === sel.id);
      el.classList.toggle('is-selected', on); el.setAttribute('aria-pressed', String(on));
    }
    if (state.scene) setInspector(renderInspector(sel, state.scene, viewSnap()));
    state.sig = '';   // the next data change redraws with the selection baked in
  };
  const openPane = (id) => {
    const n = state.scene?.nodes.find((x) => x.id === id);
    if (n && !n.person) open({ node_id: n.id, agent_id: n.agent.agent_id || null, session_id: n.agent.session_id || null, host_id: n.host });
  };

  container.addEventListener('click', (event) => {
    const t = event.target.closest?.('[data-ao-mode],[data-ao-node],[data-ao-edge],[data-ao-task],[data-ao-node-link],[data-ao-open-pane],[data-ao-play],[data-ao-live],[data-ao-clear],[data-ao-retry]');
    if (!t) return;
    const d = t.dataset;
    if (d.aoMode) { state.mode = d.aoMode; if (d.aoMode === 'replay' && state.index == null && state.timeline.length > 1) void seek(state.timeline.length - 2); paintBar(); paintStage({ force: true }); paintTimeline(); }
    else if (d.aoNode) select({ type: 'node', id: d.aoNode });
    else if (d.aoEdge) select({ type: 'edge', id: d.aoEdge });
    else if (d.aoTask) select({ type: 'task', id: d.aoTask });
    else if (d.aoNodeLink) select({ type: 'node', id: d.aoNodeLink });
    else if (d.aoOpenPane) openPane(d.aoOpenPane);
    else if ('aoPlay' in d) togglePlay();
    else if ('aoLive' in d) { stopPlaying(false); if (state.mode === 'replay') { state.mode = 'activity'; paintBar(); } void seek(null); }
    else if ('aoClear' in d) { state.filters = emptyFilters(); paintBar(); paintStage({ force: true }); }
    else if ('aoRetry' in d) { state.phase = 'loading'; paintStage(); void load(); }
  });
  container.addEventListener('focusin', (event) => {
    const n = event.target.closest?.('[data-ao-node]');
    if (!n || state.tabNode === n.dataset.aoNode) return;
    state.tabNode = n.dataset.aoNode;
    for (const el of stageEl.querySelectorAll('[data-ao-node]')) el.setAttribute('tabindex', el === n ? '0' : '-1');
  });
  container.addEventListener('dblclick', (event) => { const n = event.target.closest?.('[data-ao-node]'); if (n) openPane(n.dataset.aoNode); });
  container.addEventListener('change', (event) => {
    const f = event.target.closest?.('[data-ao-filter]');
    if (f) { state.filters[f.dataset.aoFilter] = f.value; paintBar(); paintStage({ force: true }); return; }
    if (event.target.matches?.('[data-ao-speed]')) { state.speed = Number(event.target.value) || 1; if (state.playing) schedulePlay(); }
  });
  container.addEventListener('input', (event) => {
    if (!event.target.matches?.('[data-ao-scrub]')) return;
    const max = state.timeline.length - 1, v = Number(event.target.value);
    stopPlaying(false);
    if (v >= max && state.index == null) return;
    void seek(indexAtFraction(state.timeline, max > 0 ? v / max : 1));
  });
  container.addEventListener('keydown', (event) => {
    const tab = event.target.closest?.('[data-ao-mode]');
    if (tab && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) {
      const i = MODES.findIndex((m) => m.key === state.mode), j = (i + (event.key === 'ArrowRight' ? 1 : MODES.length - 1)) % MODES.length;
      event.preventDefault(); container.querySelector(`[data-ao-mode="${MODES[j].key}"]`)?.click(); container.querySelector(`[data-ao-mode="${MODES[j].key}"]`)?.focus(); return;
    }
    const node = event.target.closest?.('[data-ao-node]'), edge = event.target.closest?.('[data-ao-edge]');
    if (!node && !edge) return;
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); (node || edge).dispatchEvent(new MouseEvent('click', { bubbles: true })); return; }   // SVG groups have no .click()
    if (node && (event.key === 'o' || event.key === 'O')) { event.preventDefault(); openPane(node.dataset.aoNode); return; }
    if (event.key === 'Escape') { select(null); return; }
    if (node && state.scene && event.key.startsWith('Arrow')) {
      const next = neighbour(state.scene, node.dataset.aoNode, event.key);
      if (next) { event.preventDefault(); stageEl.querySelector(`[data-ao-node="${CSS.escape(next)}"]`)?.focus(); }
    }
  });

  paintAll();
  void load();
  const refreshNow = () => { if (state.phase === 'ok') scheduleRefresh(); else void load(); };
  return {
    refresh: refreshNow,
    state: () => state,
    dispose() { disposed = true; controller?.abort(); replayCtl?.abort(); clearTimeout(playTimer); clearTimeout(refreshTimer); stop?.(); container.innerHTML = ''; },
  };
}
