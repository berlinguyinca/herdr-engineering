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
const who = (side) => `${side?.harness || 'unknown'}${side?.agent_id ? ` @${side.agent_id}` : side?.session_id ? ` ${side.session_id}` : ''}`;

export function workspaceCard(w, open) {
  const chips = Object.entries(w.states || {}).map(([s, n]) => `<span class="hd-chip hd-chip--${esc(s)}">${esc(s.replace('_', ' '))} ${n}</span>`).join(' ');
  return `<article class="hd-card hd-ws" data-workspace="${esc(w.workspace)}"><button type="button" class="hd-ws__head" aria-expanded="${open ? 'true' : 'false'}">
    <strong>${esc(w.workspace)}</strong><span>${w.total} handoff${w.total === 1 ? '' : 's'}</span>
    ${w.attention ? `<span class="hd-badge hd-badge--attention">${w.attention} need attention</span>` : '<span class="hd-badge">all clear</span>'}
    <small>${esc(ago(w.latest_at))}</small></button><div class="hd-ws__chips">${chips}</div><div class="hd-ws__body" ${open ? '' : 'hidden'}></div></article>`;
}

export function handoffCard(h) {
  return `<button type="button" class="hd-card hd-row${h.attention ? ' hd-row--attention' : ''}" data-handoff="${esc(h.handoff_id)}">
    <strong>${esc(who(h.source))} → ${esc(who(h.target))}</strong><span>${esc(h.state)}${h.stalled ? ' · stalled' : ''}</span>
    <p>${esc(h.goal || '')}</p><small>${h.completed} done · ${h.remaining} left · ${esc(ago(h.updated_at))} · ${esc(h.handoff_id)}</small></button>`;
}

const list = (items) => (items && items.length ? `<ul>${items.map((i) => `<li>${esc(typeof i === 'string' ? i : JSON.stringify(i))}</li>`).join('')}</ul>` : '<p class="hd-muted">Nothing recorded.</p>');

export function detailHtml(row, chain) {
  const hops = (chain?.more_before ? '<span class="hd-muted" title="Earlier handoffs not shown">…</span><span aria-hidden="true">›</span>' : '') + (chain?.chain || []).map((h, i) => `<button type="button" class="hd-hop${i === chain.position ? ' hd-hop--here' : ''}" data-handoff="${esc(h.handoff_id)}"
    ${i === chain.position ? 'aria-current="true"' : ''}>${esc(h.source?.harness || '?')} → ${esc(h.target?.harness || '?')}<small>${esc(h.state)}</small></button>`).join('<span aria-hidden="true">›</span>') + (chain?.more_after ? '<span aria-hidden="true">›</span><span class="hd-muted" title="Later handoffs not shown">…</span>' : '');
  const history = (row.history || []).map((s) => `<li>${esc(s.state)} <small>${esc(ago(s.at))}${s.actor?.id ? ` · ${esc(s.actor.id)}` : ''}</small></li>`).join('');
  const hint = row.target?.model_hint;
  return `<section class="hd-detail" data-handoff-detail-body aria-label="Handoff ${esc(row.handoff_id)}">
    <header><h2>${esc(who(row.source))} → ${esc(who(row.target))}</h2>
    <p><span class="hd-badge">${esc(String(row.state).replace('_', ' '))}</span> ${esc(SENTENCE[row.state] || '')} · updated ${esc(ago(row.updated_at))}${hint ? ` · model hint <code>${esc(hint)}</code>` : ''}</p>
    ${row.mission_id ? `<p class="hd-muted">Mission <a href="#/missions/${encodeURIComponent(row.mission_id)}">${esc(row.mission_id)}</a></p>` : ''}</header>
    ${hops ? `<nav class="hd-chain" aria-label="Handoff chain">${hops}</nav>` : ''}
    <h3>Goal</h3><p class="hd-goal">${esc(row.goal || '')}</p>
    <div class="hd-cols"><div><h3>Accomplished</h3>${list(row.completed)}</div><div><h3>Missing / what happens next</h3>${list(row.remaining)}</div></div>
    ${(row.decisions || []).length ? `<h3>Decisions</h3>${list(row.decisions)}` : ''}
    ${(row.risks || []).length ? `<h3>Risks</h3>${list(row.risks)}` : ''}
    ${(row.validation || []).length ? `<h3>Validation</h3>${list(row.validation)}` : ''}
    <h3>History</h3><ol class="hd-history">${history}</ol></section>`;
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

export function mountHandoffs(container, client, { freshness = () => null, open = '' } = {}) {
  let disposed = false, controller = null, loaded = false, signature = null, openWs = null, shown = null;
  container.innerHTML = `<p class="hd-muted">The big picture: work passed between agents, grouped by workspace. Open a workspace to see its handoffs; open any handoff, box or arrow for what was accomplished, what is missing and what happens next. Handoff text is untrusted and escaped.</p>
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
  container.addEventListener('click', (ev) => {
    const hit = ev.target.closest?.('[data-handoff]');
    if (hit && container.contains(hit)) { void openHandoff(hit.dataset.handoff); return; }
    const head = ev.target.closest?.('.hd-ws__head');
    if (head) void toggleWorkspace(head.closest('[data-workspace]'));
    const more = ev.target.closest?.('[data-handoff-more]');
    if (more) void loadPage(more.closest('[data-workspace]'), more.dataset.cursor);
  });

  const loadPage = async (card, cursor) => {
    const body = card.querySelector('.hd-ws__body');
    const ws = card.dataset.workspace;
    if (!cursor) body.innerHTML = skeleton('workspace handoffs');
    try {
      const page = await client.pageHandoffs({ workspace: ws, cursor, limit: 50, signal: signalFor() });
      if (disposed) return;
      const rows = Array.isArray(page.handoffs) ? page.handoffs : [];
      card.__rows = (cursor ? card.__rows || [] : []).concat(rows);
      const { flow, latest } = flowFromPage(card.__rows);
      body.innerHTML = `<div class="cx-flow-box" data-handoff-graph></div><div class="hd-rows">${card.__rows.map(handoffCard).join('') || '<p class="hd-muted">No handoffs.</p>'}</div>
        ${page.next_cursor ? `<button type="button" data-handoff-more data-cursor="${esc(page.next_cursor)}">Load more</button>` : ''}`;
      renderFlow(body.querySelector('[data-handoff-graph]'), flow, {
        onEdge: (e) => e && latest.get(e.key) && openHandoff(latest.get(e.key)),
        onNode: (n) => { const h = card.__rows.find((r) => [r.source, r.target].some((s) => (s?.agent_id || s?.session_id) === n?.id)); if (h) void openHandoff(h.handoff_id); },
      });
    } catch (error) {
      if (!disposed) body.innerHTML = `<p class="hd-muted">Unable to load this workspace: ${esc(error.message)}</p>`;
    }
  };
  const toggleWorkspace = async (card) => {
    const body = card.querySelector('.hd-ws__body'), head = card.querySelector('.hd-ws__head');
    const open = body.hidden;
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
