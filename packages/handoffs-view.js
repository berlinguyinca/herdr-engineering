import { escapeHtml as esc } from './herdr-web-components/util.js';
import { renderFlow } from './collab-flow.js';
import { skeleton } from './motion.js';
import { pollWhileVisible } from './refresh-bus.js';

// A handoff is one joint in a longer story. This page is the big picture (workspaces, then the few handoffs that need a
// human); every handoff, box and arrow opens the same detail: what was accomplished, what is missing, what happens next,
// and the hops before and after it. Handoff text is untrusted: every field below goes through esc().

export function handoffGraphSummary(graph = {}) {
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const states = {};
  for (const edge of edges) states[edge.state] = (states[edge.state] || 0) + 1;
  return { nodes: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
    edges: edges.length, states, edgeText: edges[0] ? `${edges[0].handoff_id} · ${edges[0].state}` : '' };
}

export function overviewSummary(ov = {}) {
  const workspaces = Array.isArray(ov.workspaces) ? ov.workspaces.length + (ov.workspaces_omitted || 0) : 0;
  const attention = ov.attention_total || 0;
  return `${ov.total || 0} handoffs · ${workspaces} workspaces${attention ? ` · ${attention} need attention` : ''}`;
}

const SENTENCE = { queued: 'waiting to be picked up', claimed: 'claimed, not yet accepted', accepted: 'accepted', in_progress: 'in progress',
  completed: 'completed', failed: 'failed', rejected: 'rejected by the receiver', cancelled: 'cancelled', expired: 'expired unclaimed' };
const ago = (iso) => {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return '';
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};
const agentName = (side) => (side?.agent_id ? `@${side.agent_id}` : side?.session_id || side?.harness || 'unknown');
const clip = (t, n) => { const x = String(t || '').replace(/\s+/g, ' ').trim(); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };
const who = (side) => `${side?.harness || 'unknown'}${side?.agent_id ? ` @${side.agent_id}` : side?.session_id ? ` ${side.session_id}` : ''}`;

export function workspaceCard(w, open) {
  const chips = Object.entries(w.states || {}).map(([st, n]) => `<span class="hd-chip hd-chip--${esc(st)}">${esc(st.replace('_', ' '))} ${n}</span>`).join(' ');
  return `<article class="hd-ws" data-workspace="${esc(w.workspace)}"><button type="button" class="hd-ws__head" aria-expanded="${open ? 'true' : 'false'}">
    <strong>${esc(w.workspace)}</strong><span class="hd-ws__n">${w.total}</span>
    ${w.attention ? `<span class="hd-badge hd-badge--attention">${w.attention} need attention</span>` : ''}
    <span class="hd-ws__chips">${chips}</span><small>${esc(ago(w.latest_at))}</small></button><div class="hd-ws__body" ${open ? '' : 'hidden'}></div></article>`;
}

const shortModel = (m) => String(m || '').split('/').pop();
// What happened to the model across a Pi handoff (only lineage handoffs know): kept, switched, or the successor was not pinned.
export function modelText(h) {
  if (h.model_change === 'same') return `same model${h.to_model ? ` · ${shortModel(h.to_model)}` : ''}`;
  if (h.model_change === 'switched') return `model changed · ${shortModel(h.from_model)} → ${shortModel(h.to_model)}`;
  if (h.model_change === 'not_pinned') return `not pinned${h.from_model ? ` · was ${shortModel(h.from_model)}` : ''} · routed normally`;
  return h.origin === 'lineage' ? 'model not reported' : '';
}

const CAUSE = { context_pressure: 'context pressure', quota_exhausted: 'quota exhausted', phase_transition: 'phase transition' };

export function handoffCard(h) {
  return `<button type="button" class="hd-row${h.attention ? ' hd-row--attention' : ''}" data-handoff="${esc(h.handoff_id)}" title="${esc(h.attention_reason ? `${h.handoff_id} — ${h.attention_reason}` : h.handoff_id)}">
    <span class="hd-row__state hd-row__state--${esc(h.state)}">${esc(String(h.state).replace('_', ' '))}${h.stalled ? ' · stalled' : ''}</span>
    <strong class="hd-row__who">${esc(agentName(h.source))} → ${esc(agentName(h.target))}</strong>
    <span class="hd-row__goal">${esc(h.origin === 'lineage' ? `${CAUSE[h.cause] || h.cause || 'handoff'} · ${modelText(h)}` : clip(h.goal, 140))}</span>
    <small class="hd-row__meta">${h.origin === 'lineage' ? esc([h.attention_reason, h.updated_at && ago(h.updated_at)].filter(Boolean).join(' · ')) : `${h.completed} done · ${h.remaining} left · ${esc(ago(h.updated_at))}`}</small></button>`;
}

const list = (items) => (items && items.length ? `<ul>${items.map((i) => `<li>${esc(typeof i === 'string' ? i : JSON.stringify(i))}</li>`).join('')}</ul>` : '<p class="hd-muted">Nothing recorded.</p>');

const bullets = (items) => (items && items.length ? items.map((i) => `- ${typeof i === 'string' ? i : JSON.stringify(i)}`).join('\n') : '(none recorded)');

// The handoff exactly as one document: what the next agent was told, plus every recorded field. Plain text, so it can be copied
// into a new session, a PR comment or an issue.
export function handoffText(row) {
  const side = (x) => [x?.harness, x?.agent_id && `@${x.agent_id}`, x?.session_id && `session ${x.session_id}`, x?.model_hint && `model ${x.model_hint}`].filter(Boolean).join(' · ') || 'unknown';
  const refs = (row.artifact_refs || []).map((r) => `- ${typeof r === 'string' ? r : [r.artifact_id, r.version_id].filter(Boolean).join(' @ ') || JSON.stringify(r)}`).join('\n') || '(none)';
  const prov = Object.entries(row.provenance || {}).map(([k, v]) => `- ${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('\n') || '(none)';
  return [`HANDOFF ${row.handoff_id} · ${String(row.state).replace('_', ' ')}`, `From: ${side(row.source)}`, `To:   ${side(row.target)}`,
    row.mission_id ? `Mission: ${row.mission_id}` : '', row.room_id ? `Room: ${row.room_id}` : '',
    `Created: ${row.created_at || '?'}${row.created_by ? ` by ${typeof row.created_by === 'string' ? row.created_by : row.created_by.id || JSON.stringify(row.created_by)}` : ''} · updated ${row.updated_at || '?'}`,
    '', 'GOAL', String(row.goal || ''), '', 'ACCOMPLISHED', bullets(row.completed), '', 'MISSING / NEXT', bullets(row.remaining),
    '', 'DECISIONS', bullets(row.decisions), '', 'RISKS', bullets(row.risks), '', 'VALIDATION', bullets(row.validation),
    '', 'ARTIFACTS', refs, '', 'PROVENANCE', prov, '', 'HISTORY', (row.history || []).map((h) => `- ${h.state} at ${h.at}${h.actor?.id ? ` by ${h.actor.id}` : ''}`).join('\n') || '(none)'].filter((l, i, a) => l !== '' || a[i - 1] !== '').join('\n');
}

export function detailHtml(row, chain) {
  const hopItem = (h, i) => `<li${i === chain.position ? ' aria-current="step"' : ''}${near(i) ? '' : ' class="hd-hop-far" hidden'}><button type="button" class="hd-hop${i === chain.position ? ' hd-hop--here' : ''}" data-handoff="${esc(h.handoff_id)}"
    ${i === chain.position ? 'aria-current="true"' : ''}><span class="hd-hop__who">${esc(agentName(h.source))} → ${esc(agentName(h.target))}</span><span class="hd-hop__state">${esc(String(h.state).replace('_', ' '))}</span>
    <small>${esc(clip(h.goal, 70))}${h.updated_at ? ` · ${esc(ago(h.updated_at))}` : ''}</small></button></li>`;
  const near = (i) => Math.abs(i - (chain?.position ?? 0)) <= 2;
  const far = (chain?.chain || []).filter((_, i) => !near(i)).length;
  const hops = chain?.chain?.length > 1 ? (chain.more_before ? '<li class="hd-muted hd-hop-more">… earlier handoffs not shown</li>' : '') + chain.chain.map(hopItem).join('') + (far ? `<li><button type="button" class="hd-linkish" data-chain-all>Show ${far} more hop${far === 1 ? '' : 's'}</button></li>` : '') + (chain.more_after ? '<li class="hd-muted hd-hop-more">… later handoffs not shown</li>' : '') : '';
  const history = (row.history || []).map((s) => `<li>${esc(s.state)} <small>${esc(ago(s.at))}${s.actor?.id ? ` · ${esc(s.actor.id)}` : ''}</small></li>`).join('');
  const hint = row.target?.model_hint;
  return `<section class="hd-detail" data-handoff-detail-body aria-label="Handoff ${esc(row.handoff_id)}">
    <header><h2>${esc(who(row.source))} → ${esc(who(row.target))}</h2>
    <p><span class="hd-badge">${esc(String(row.state).replace('_', ' '))}</span> ${esc(SENTENCE[row.state] || '')}${ago(row.updated_at) ? ` · updated ${esc(ago(row.updated_at))}` : ''}${hint ? ` · model hint <code>${esc(hint)}</code>` : ''}</p>
    ${row.mission_id ? `<p class="hd-muted">Mission <a href="#/missions/${encodeURIComponent(row.mission_id)}">${esc(row.mission_id)}</a></p>` : ''}</header>
    <h3>Handoff text — what the next agent was told</h3><p class="hd-goal" data-handoff-goal>${esc(row.goal || '')}</p>
    ${row.origin === 'lineage' ? `<h3>Model</h3><p data-handoff-model><strong>${esc(modelText(row))}</strong>${row.cause ? ` · cause <code>${esc(row.cause)}</code>` : ''}${row.checkpoint ? ` · checkpoint <code>${esc(String(row.checkpoint).slice(0, 12))}</code>` : ''}</p>
      <p class="hd-muted">Pi records this handoff and its verified checkpoint, not a written summary: the work done so far is in the checkpoint, and the successor continues from it.</p>`
      : `<div class="hd-cols"><div><h3>Accomplished</h3>${list(row.completed)}</div><div><h3>Missing / what happens next</h3>${list(row.remaining)}</div></div>`}
    ${hops ? `<h3>Where this sits in the work</h3><ol class="hd-chain" aria-label="Handoff chain, oldest first">${hops}</ol>` : ''}
    ${(row.decisions || []).length ? `<h3>Decisions</h3>${list(row.decisions)}` : ''}
    ${(row.risks || []).length ? `<h3>Risks</h3>${list(row.risks)}` : ''}
    ${(row.validation || []).length ? `<h3>Validation</h3>${list(row.validation)}` : ''}
    ${history ? `<h3>History</h3><ol class="hd-history">${history}</ol>` : ''}
    <details class="hd-full" data-handoff-full><summary>Full handoff as text</summary><p><button type="button" data-handoff-copy>Copy to clipboard</button> <small class="hd-muted" data-handoff-copied hidden>Copied</small></p><pre class="hd-pre">${esc(handoffText(row))}</pre></details></section>`;
}

// Nodes are endpoints (agent, else session, else harness); edges are the handoffs between them.
export function flowFromPage(rows) {
  const nodes = new Map(), edges = new Map();
  const node = (side, role) => {
    const key = side?.agent_id || side?.session_id || `${side?.harness || 'unknown'}:${role}`;
    if (!nodes.has(key)) nodes.set(key, { id: key, kind: 'agent', name: side?.agent_id || side?.session_id || side?.harness || 'unknown', family: side?.harness });
    return key;
  };
  const latest = new Map();
  for (const h of rows) {
    const from = node(h.source, 'source'), to = node(h.target, 'target'), key = `${from}>${to}`;
    const bucket = ['queued', 'claimed', 'in_progress'].includes(h.state) ? 'pending' : h.state || 'unknown';
    const edge = edges.get(key) || { from, to, counts: {}, last: h.updated_at };
    edge.counts[bucket] = (edge.counts[bucket] || 0) + 1;
    edges.set(key, edge);
    if (!latest.has(key)) latest.set(key, h.handoff_id);          // rows arrive newest first
  }
  return { flow: { nodes: [...nodes.values()], edges: [...edges.values()], spawns: [] }, latest };
}

const routeKey = (h) => `${agentName(h.source)} → ${agentName(h.target)}`;

// One row per from → to pair: how many handoffs, in which states, and the newest one. Readable at any size, unlike a drawn graph.
export function routeTable(rows) {
  const routes = new Map();
  for (const h of rows) {
    const key = routeKey(h);
    const r = routes.get(key) || { key, from: agentName(h.source), to: agentName(h.target), total: 0, states: {}, newest: h.handoff_id, attention: 0 };
    r.total += 1; r.states[h.state] = (r.states[h.state] || 0) + 1; if (h.attention) r.attention += 1;
    routes.set(key, r);
  }
  return [...routes.values()].sort((a, b) => b.attention - a.attention || b.total - a.total || a.key.localeCompare(b.key));
}

export function routesHtml(routes, active, limit = 12) {
  const rows = routes.slice(0, limit).map((r) => `<li><button type="button" class="hd-route${r.key === active ? ' hd-route--on' : ''}" data-route="${esc(r.key)}" aria-pressed="${r.key === active}">
    <span class="hd-route__who">${esc(r.from)} → ${esc(r.to)}</span><span class="hd-route__n">${r.total}</span>
    <span class="hd-route__states">${Object.entries(r.states).map(([s, n]) => `<span class="hd-chip hd-chip--${esc(s)}">${esc(s.replace('_', ' '))} ${n}</span>`).join(' ')}</span></button></li>`).join('');
  return `<h3 class="hd-routes__title">Who hands work to whom <small>(${routes.length} route${routes.length === 1 ? '' : 's'}${routes.length > limit ? `, top ${limit}` : ''}; pick one to filter)</small></h3><ul class="hd-routes" data-handoff-routes>${rows}</ul>`;
}

export function mountHandoffs(container, client, { freshness = () => null, open = '' } = {}) {
  let disposed = false, controller = null, loaded = false, signature = null, openWs = null, shown = null;
  container.innerHTML = `<p class="hd-muted hd-intro">Work passed between agents, by workspace. Open a workspace, then any handoff. ↑↓ moves through the list. Handoff text is untrusted and escaped.</p>
    <div data-handoff-summary role="status"></div>
    <div class="hd-layout"><div class="hd-main"><section data-handoff-attention aria-label="Needs attention"></section>
    <section data-handoff-list></section></div>
    <aside class="hd-side" aria-label="Handoff detail"><section data-handoff-detail aria-live="polite"><p class="hd-muted">Open a handoff, box or arrow to see what was accomplished, what is missing and what happens next.</p></section></aside></div>
    <button type="button" data-handoff-retry hidden>Retry</button>`;
  const summary = container.querySelector('[data-handoff-summary]');
  const attention = container.querySelector('[data-handoff-attention]');
  const wsList = container.querySelector('[data-handoff-list]');
  const detail = container.querySelector('[data-handoff-detail]');
  const retry = container.querySelector('[data-handoff-retry]');
  const signalFor = () => AbortSignal.timeout(15000);

  const openHandoff = async (id) => {
    shown = id;
    detail.innerHTML = skeleton('handoff detail');
    try {
      const signal = signalFor();
      const [row, chain] = await Promise.all([client.getHandoff(id, { signal }), client.getHandoffChain(id, { signal }).catch(() => null)]);
      if (disposed || shown !== id) return;
      detail.innerHTML = detailHtml(row, chain);
      if (!window.matchMedia?.('(min-width: 1100px)').matches) detail.scrollIntoView?.({ block: 'start' });   // beside the list on wide screens, so no scrolling
    } catch (error) {
      if (!disposed && shown === id) detail.innerHTML = `<p class="hd-muted">Unable to load handoff ${esc(id)}: ${esc(error.message)}</p>`;
    }
  };
  container.addEventListener('keydown', (ev) => {         // up/down walks the list; Enter or Space (a button click) opens it
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
    const here = ev.target.closest?.('.hd-row, .hd-route, .hd-ws__head');
    if (!here) return;
    const items = [...container.querySelectorAll('.hd-row, .hd-route, .hd-ws__head')].filter((el) => el.offsetParent !== null);
    const next = items[items.indexOf(here) + (ev.key === 'ArrowDown' ? 1 : -1)];
    if (next) { ev.preventDefault(); next.focus(); }
  });
  container.addEventListener('click', (ev) => {
    const hit = ev.target.closest?.('[data-handoff]');
    if (hit && container.contains(hit)) { void openHandoff(hit.dataset.handoff); return; }
    const head = ev.target.closest?.('.hd-ws__head');
    if (head) void toggleWorkspace(head.closest('[data-workspace]'));
    const copy = ev.target.closest?.('[data-handoff-copy]');
    if (copy) {
      const text = copy.closest('[data-handoff-full]')?.querySelector('pre')?.textContent || '';
      const done = () => { const note = copy.parentElement.querySelector('[data-handoff-copied]'); if (note) { note.hidden = false; setTimeout(() => { note.hidden = true; }, 2000); } };
      navigator.clipboard?.writeText(text).then(done, () => {}); return;
    }
    const allHops = ev.target.closest?.('[data-chain-all]');
    if (allHops) { allHops.closest('ol').querySelectorAll('.hd-hop-far').forEach((li) => { li.hidden = false; }); allHops.parentElement.remove(); return; }
    const route = ev.target.closest?.('[data-route]');
    if (route) { const card = route.closest('[data-workspace]'); card.__route = card.__route === route.dataset.route ? null : route.dataset.route; paint(card); return; }
    if (ev.target.closest?.('[data-route-clear]')) { const card = ev.target.closest('[data-workspace]'); card.__route = null; paint(card); return; }
    const more = ev.target.closest?.('[data-handoff-more]');
    if (more) void loadPage(more.closest('[data-workspace]'), more.dataset.cursor);
  });

  const SMALL_GRAPH = 6;       // routes a drawn graph can still show legibly
  const paint = (card) => {
    const body = card.querySelector('.hd-ws__body');
    const all = card.__rows || [], active = card.__route || null;
    const routes = routeTable(all);
    const shown = active ? all.filter((h) => routeKey(h) === active) : all;
    const small = routes.length <= SMALL_GRAPH;
    const overview = small ? '<div class="cx-flow-box" data-handoff-graph></div>' : routesHtml(routes, active);
    const bar = active ? `<p class="hd-filter" data-handoff-filter>Showing <strong>${esc(active)}</strong> · ${shown.length} loaded handoff${shown.length === 1 ? '' : 's'} <button type="button" data-route-clear>Show all</button></p>` : '';
    body.innerHTML = `${overview}${bar}<div class="hd-rows">${shown.map(handoffCard).join('') || '<p class="hd-muted">No handoffs.</p>'}</div>
      ${card.__next ? `<button type="button" data-handoff-more data-cursor="${esc(card.__next)}">Load more</button>${active ? '<small class="hd-muted"> Filter applies to handoffs loaded so far.</small>' : ''}` : ''}`;
    if (small) {
      const { flow, latest } = flowFromPage(all);
      renderFlow(body.querySelector('[data-handoff-graph]'), flow, {
        onEdge: (e) => e && latest.get(e.key) && openHandoff(latest.get(e.key)),
        onNode: (n) => { const h = all.find((r) => [r.source, r.target].some((x) => (x?.agent_id || x?.session_id) === n?.id)); if (h) void openHandoff(h.handoff_id); },
      });
    }
  };
  const loadPage = async (card, cursor) => {
    const body = card.querySelector('.hd-ws__body');
    const ws = card.dataset.workspace;
    if (!cursor) body.innerHTML = skeleton('workspace handoffs');
    try {
      const page = await client.pageHandoffs({ workspace: ws, cursor, limit: 25, signal: signalFor() });
      if (disposed) return;
      const rows = Array.isArray(page.handoffs) ? page.handoffs : [];
      card.__rows = (cursor ? card.__rows || [] : []).concat(rows);
      card.__next = page.next_cursor || null;
      paint(card);
    } catch (error) {
      if (!disposed) body.innerHTML = `<p class="hd-muted">Unable to load this workspace: ${esc(error.message)}</p>`;
    }
  };
  const toggleWorkspace = async (card) => {
    const body = card.querySelector('.hd-ws__body'), head = card.querySelector('.hd-ws__head');
    const open = body.hidden;
    if (open) for (const other of wsList.querySelectorAll('.hd-ws__body:not([hidden])')) {      // one workspace at a time keeps the list short
      other.hidden = true; other.closest('[data-workspace]').querySelector('.hd-ws__head').setAttribute('aria-expanded', 'false');
    }
    body.hidden = !open; head.setAttribute('aria-expanded', open ? 'true' : 'false');
    openWs = open ? card.dataset.workspace : null;
    if (open) await loadPage(card);
  };

  const draw = (ov) => {
    summary.textContent = overviewSummary(ov);
    attention.innerHTML = (ov.attention || []).length
      ? `<h2>Needs attention</h2><div class="hd-rows">${ov.attention.map(handoffCard).join('')}</div>${ov.attention_total > ov.attention.length ? `<p class="hd-muted">Showing ${ov.attention.length} of ${ov.attention_total}; open a workspace for the rest.</p>` : ''}` : '';
    const cards = (ov.workspaces || []).map((w) => workspaceCard(w, w.workspace === openWs)).join('');
    wsList.innerHTML = (cards || '<p class="hd-muted">No handoffs recorded. A handoff appears here when one agent passes work to another (for example from one harness to another); until then the <a href="#/herd">Agents</a> page shows each mission\'s agents side by side.</p>')
      + (ov.workspaces_omitted ? `<p class="hd-muted">${ov.workspaces_omitted} more workspaces not shown.</p>` : '');
    const reopen = openWs && wsList.querySelector(`[data-workspace="${CSS.escape(openWs)}"]`);
    if (reopen) void loadPage(reopen);
  };
  const refresh = async () => {
    if (disposed || controller) return;
    const request = new AbortController(); controller = request;
    const signal = AbortSignal.any([request.signal, signalFor()]);
    if (!loaded) wsList.innerHTML = skeleton('handoffs');
    retry.hidden = true;
    try {
      const data = await client.getHandoffOverview({ signal });
      if (disposed) return;
      if (!Array.isArray(data.workspaces)) throw new Error('Invalid handoff overview response');
      const next = JSON.stringify(data);
      if (signature !== next) { draw(data); signature = next; } else summary.textContent = overviewSummary(data);
      loaded = true;
      freshness()?.touch(true);
    } catch (error) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) wsList.innerHTML = '';
        summary.textContent = `Unable to load handoffs: ${error.message}`;
        retry.hidden = false;
        freshness()?.touch(false, error.message);
      }
    } finally { if (controller === request) controller = null; }
  };
  retry.addEventListener('click', refresh);
  if (open) void openHandoff(open);
  const stop = pollWhileVisible(refresh, 30000);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); } };
}
