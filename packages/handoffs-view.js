import { escapeHtml as esc } from './herdr-web-components/util.js';
import { renderFlow } from './collab-flow.js';
import { skeleton } from './motion.js';
import { pollWhileVisible } from './refresh-bus.js';

export function handoffGraphSummary(graph = {}) {
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const states = {};
  for (const edge of edges) states[edge.state] = (states[edge.state] || 0) + 1;
  return { nodes: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
    edges: edges.length, states, edgeText: edges[0] ? `${edges[0].handoff_id} · ${edges[0].state}` : '' };
}

export function mountHandoffs(container, client, { freshness = () => null } = {}) {
  let disposed = false, controller = null, loaded = false, signature = null;
  container.innerHTML = `<p class="hd-muted">Work passed from one agent to another, between harnesses and hosts: who handed what to whom, and where it stands. Handoff text is untrusted and escaped.</p><div data-handoff-summary role="status"></div><section class="cx-flow-box" data-handoff-graph></section><section data-handoff-list></section><button type="button" data-handoff-retry hidden>Retry</button>`;
  const summary = container.querySelector('[data-handoff-summary]');
  const graph = container.querySelector('[data-handoff-graph]');
  const list = container.querySelector('[data-handoff-list]');
  const retry = container.querySelector('[data-handoff-retry]');
  const draw = data => {
    const stats = handoffGraphSummary(data);
    summary.textContent = `${stats.edges} handoffs · ${stats.nodes} endpoints`;
    const flow = { nodes: (data.nodes || []).map(node => ({ ...node, kind: 'agent', name: node.agent_id || node.session_id || node.role, family: node.harness })),
      edges: (data.edges || []).map(edge => ({ from: edge.from, to: edge.to,
        counts: { [(['queued', 'claimed', 'in_progress'].includes(edge.state) ? 'pending' : edge.state || 'unknown')]: 1 },
        last: edge.updated_at })), spawns: [] };
    renderFlow(graph, flow);
    list.innerHTML = (data.edges || []).map(edge => {
      const from = (data.nodes || []).find(node => node.id === edge.from) || {};
      const to = (data.nodes || []).find(node => node.id === edge.to) || {};
      return `<article class="hd-card"><strong>${esc(from.harness || 'unknown')} → ${esc(to.harness || 'unknown')}</strong><span>${esc(edge.state || 'unknown')}</span><p>${esc(edge.goal || '')}</p><small>${esc(edge.handoff_id || '')}</small></article>`;
    }).join('') || '<p class="hd-muted">No handoffs recorded. A handoff appears here when one agent passes work to another (for example from one harness to another); until then the <a href="#/herd">Agents</a> page shows each mission\'s agents side by side.</p>';
  };
  const refresh = async () => {
    if (disposed || controller) return;
    const request = new AbortController(); controller = request;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(15000)]);
    if (!loaded) list.innerHTML = skeleton('handoffs');
    retry.hidden = true;
    try {
      const data = await client.getHandoffTree({ signal });
      if (disposed) return;
      if (!Array.isArray(data.nodes) || !Array.isArray(data.edges)) throw new Error('Invalid handoff graph response');
      const next = JSON.stringify(data);
      if (signature !== next) { draw(data); signature = next; }
      else { const stats = handoffGraphSummary(data); summary.textContent = `${stats.edges} handoffs · ${stats.nodes} endpoints`; }
      loaded = true;
      freshness()?.touch(true);
    } catch (error) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) list.innerHTML = '';
        summary.textContent = `Unable to load handoffs: ${error.message}`;
        retry.hidden = false;
        freshness()?.touch(false, error.message);
      }
    } finally { if (controller === request) controller = null; }
  };
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(refresh, 30000);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); } };
}
